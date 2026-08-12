#!/usr/bin/env python3
"""
dlv_tasks.py
============
DLV Tasks — live-check the DLV Batch queue (📋 DLV Tasks button /
/dlvtasks) and produce an Open/Closed report (Telegram text or Excel),
a per-valuer report and a per-tag report (each: Currently Queued from
saved_dlv_batch.json, At Valuer's Desk from saved_assignments.json —
assigned but not yet found completed/returned, since a ref dropped from
the queue at assignment time is otherwise untracked until it surfaces in
the closed store — and Valuer Completed from saved_dlv_closed.json,
filterable by look-back period; see _dt_format_report_lines, shared by
both), plus the multi-select bulk-delete flow for the open queue.

Every report's per-task block (_dt_format_task_block for Open Tasks,
_dt_format_report_item_block for Closed/By Valuer/By Tag) is built from
task_block.py's shared field builders and rendered via its
format_labeled_block — the one visual every report in the bot uses
(Fetch Tasks' own view and Auto Fetch's email included). Only which
fields get passed in differs per report.

_dt_fetch_tasks, _dt_build_excel, and _dt_send_telegram are also used by
Morning Briefing (bot.py), which stays in bot.py for now since it owns its
own conversation/job but depends on this feature's report-building.

Call register(app) from bot.py's main() to wire this feature in.
"""

import io
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import List, Optional, Tuple

import asyncio
import openpyxl
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
import re

from ardhisasa_auth import AuthTokens

from common import (
    BTN_DLV_TASKS,
    _any_valid_tokens,
    _CANCEL_FILTER,
    _date_cutoff_str,
    _main_menu,
    _NODE_LABELS,
    _within_days,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    load_saved_assignments,
    logger,
    md_escape,
    not_cancel,
)
from dlv_core import (
    DLV_TAGS,
    INCREMENTAL_TAG_SENTINEL,
    _append_dlv_closed,
    _classify_dlv_detail,
    _fetch_ref_detail_dlv,
    _fetch_stampduty_detail,
    _resolve_assessor,
    _search_ref_dlv,
    _search_ref_stampduty,
    is_incremental_tag,
    load_dlv_batch,
    load_dlv_closed,
    mark_removed,
    parse_incremental_tag,
    save_dlv_batch,
)
from email_service import _send_bulk_export_email
from excel_report import autofit_columns, style_header_row
from fetch_tasks_cache import _fetch_tasks_log_lookup
from task_block import (
    assessor_field,
    consideration_field,
    format_consideration,
    format_labeled_block,
    parcel_field,
    tag_field,
)
from telegram_report import _send_chunked_report


# ──────────────────────────────────────────────────────────
# States — DLV Tasks conversation
# ──────────────────────────────────────────────────────────
class DT(Enum):
    SCOPE           = auto()   # choose Open Tasks vs Closed Tasks vs By Valuer vs By Tag vs Delete Task(s)
    EXCLUDE_VALUERS = auto()   # toggle which valuers to exclude
    DELIVERY        = auto()   # telegram or email
    EMAIL_INPUT     = auto()   # enter email address
    DELETE_SELECT   = auto()   # multi-select which queued refs to delete
    PICK_VALUER     = auto()   # By Valuer: choose which valuer to report on
    PICK_TAG        = auto()   # By Tag: choose which tag to report on
    PICK_VIEW       = auto()   # By Tag + Incremental only: choose Flat/By Valuer/By Batch rendering
    PICK_PERIOD     = auto()   # By Valuer/By Tag: choose the history look-back period


# ── DLV Tasks session ──────────────────────────────────────

@dataclass
class DTSession:
    tasks:            List[dict] = field(default_factory=list)   # enriched task rows
    valuers:          List[dict] = field(default_factory=list)   # unique valuers [{uid, name}]
    excluded_uids:    set        = field(default_factory=set)
    email:            str        = ""
    delete_items:     List[dict] = field(default_factory=list)   # open queue snapshot for deletion
    delete_selected:  set        = field(default_factory=set)    # refs selected for deletion
    valuer_choices:   List[dict] = field(default_factory=list)   # By Valuer picker: [{key, name}]
    selected_valuer:  dict       = field(default_factory=dict)   # By Valuer: {key, name} chosen
    report_mode:      str        = "valuer"                      # "valuer" | "tag" — disambiguates PICK_PERIOD
    selected_tag:     str        = ""                             # By Tag: the chosen tag
    report_view:      str        = "flat"                         # By Tag + Incremental: "flat" | "valuer" | "batch"


def _get_dt_sess(ctx: ContextTypes.DEFAULT_TYPE) -> DTSession:
    if "dt_session" not in ctx.user_data:
        ctx.user_data["dt_session"] = DTSession()
    return ctx.user_data["dt_session"]


# ──────────────────────────────────────────────────────────
# DLV Tasks — fetch and enrich ongoing tasks
# ──────────────────────────────────────────────────────────

def _dt_fetch_tasks(tokens: AuthTokens) -> List[dict]:
    """
    Load the DLV batch queue and classify each ref's live status (County and
    non-County alike). Only still-open (available for reallocation) rows are
    returned; refs found to have since Completed or been Returned are moved
    into the closed store so the active queue and the Closed Tasks view stay
    in sync between timer runs.
    """
    batch = load_dlv_batch()
    if not batch:
        return []

    def _enrich(item: dict) -> dict:
        ref = item.get("ref", "")
        row = {
            "ref":           ref,
            "parcel":        "",
            "registry":      "",
            "county":        "",
            "date_created":  item.get("queued_at", ""),   # fallback: when queued
            "valuer_name":   item.get("valuer_name", ""),
            "valuer_uid":    item.get("valuer_uid", ""),
            "assessor":      "",
            "status":        "",   # application/stamp-duty status, e.g. "ONGOING"
            "node":          "",   # workflow node code, mapped to a label via _NODE_LABELS
            "consideration": "",   # formatted "KES 1,234.00", or "" if unknown
            "found":         False,   # found anywhere (DLV or still with the assessor)
            "location":      "",      # "dlv" | "assessor"
            "tag":           item.get("tag", ""),   # optional, set via DLV Batch's Tag Tasks step
            "_closed":       None,
        }
        try:
            # 1. Assessor/HQ stage first (stampdutyservice) — the same endpoints
            #    Fetch Tasks uses. A queued ref's assessor lives here before it
            #    ever reaches DLV, so checking this first gets the assessor's
            #    name without waiting on the DLV search to fail.
            assessor_task = _search_ref_stampduty(tokens, ref)
            if assessor_task:
                row["found"]        = True
                row["location"]     = "assessor"
                row["parcel"]       = assessor_task.get("parcel_number", "")
                row["registry"]     = (assessor_task.get("registry") or "").upper()
                row["county"]       = (assessor_task.get("county") or "").upper()
                row["date_created"] = assessor_task.get("date_created", row["date_created"])

                det = _fetch_stampduty_detail(tokens, assessor_task["id"])
                if det:
                    row["assessor"] = _resolve_assessor([
                        {"name": o.get("names", ""), "role": o.get("role", "")}
                        for o in det.get("officers", [])
                    ])
                    row["status"] = det.get("application_status") or det.get("stamp_duty_status", "")
            else:
                # 2. Not upstream anymore — check DLV (valuationservice), which
                #    also tells us if the ref has since Completed or Returned.
                task = _search_ref_dlv(tokens, ref)
                if task:
                    row["found"]        = True
                    row["location"]     = "dlv"
                    row["parcel"]       = task.get("parcel_number", "")
                    row["registry"]     = (task.get("registry") or "").upper()
                    row["county"]       = (task.get("county") or "").upper()
                    row["date_created"] = task.get("date_created", row["date_created"])

                    detail = _fetch_ref_detail_dlv(tokens, task["id"])
                    if detail:
                        info = _classify_dlv_detail(detail)
                        if info["bucket"] == "closed":
                            row["_closed"] = {
                                **item,
                                "closed_reason":        info["closed_reason"],
                                "application_status":   info["application_status"],
                                "node":                 info["node"],
                                "request_type":         task.get("_request_type", ""),
                                "consideration_amount": info["consideration_amount"],
                                "currency_code":        info["currency_code"],
                                "valuer_name":          info["actor_name"] or row["valuer_name"],
                                "closed_at":            datetime.now().isoformat(timespec="seconds"),
                            }
                            return row

                        row["assessor"]      = info["assessor_name"]
                        row["status"]        = info["application_status"]
                        row["node"]          = info["node"]
                        row["consideration"] = format_consideration(
                            info["consideration_amount"], info["currency_code"]
                        )
        except Exception as e:
            logger.warning("DLV Tasks enrich failed for %s: %s", ref, e)

        # 3. Neither live call found it (or found it without an assessor) —
        # fall back to whatever was captured on the batch item itself when it
        # was queued (copied from the Fetch Tasks cache at DLV Batch add-time,
        # see recv_db_confirm), then to the Fetch Tasks cache directly for
        # items queued before that field existed.
        if not row["assessor"]:
            row["assessor"] = item.get("assessor", "")
        if not row["assessor"]:
            cached = _fetch_tasks_log_lookup(ref)
            if cached:
                row["assessor"]     = cached.get("assessor", "")
                row["parcel"]       = row["parcel"]       or cached.get("parcel", "")
                row["registry"]     = row["registry"]     or cached.get("registry", "")
                row["county"]       = row["county"]       or cached.get("county", "")
                row["date_created"] = row["date_created"] or cached.get("date_created", "")

        return row

    rows: List[dict] = []
    closed_items: List[dict] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        fut_map = {pool.submit(_enrich, item): item for item in batch}
        for fut in _futures_as_completed(fut_map):
            result = fut.result()
            closed = result.pop("_closed", None)
            if closed:
                closed_items.append(closed)
            else:
                rows.append(result)

    if closed_items:
        closed_refs = {c["ref"] for c in closed_items}
        save_dlv_batch([i for i in batch if i.get("ref") not in closed_refs])
        for c in closed_items:
            _append_dlv_closed(c)

    return rows


# ──────────────────────────────────────────────────────────
# DLV Tasks command handlers
# ──────────────────────────────────────────────────────────

def _dt_exclusion_keyboard(valuers: List[dict], excluded_uids: set) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(valuers), 2):
        row = []
        for v in valuers[i:i+2]:
            icon = "❌" if v["uid"] in excluded_uids else "✅"
            row.append(InlineKeyboardButton(
                f"{icon} {v['name']}", callback_data=f"dt_toggle:{v['uid']}",
            ))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("▶️ Confirm", callback_data="dt_confirm"),
        InlineKeyboardButton("🛑 Cancel",  callback_data="dt_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _dt_delete_keyboard(items: List[dict], selected_refs: set) -> InlineKeyboardMarkup:
    rows = []
    for i, item in enumerate(items):
        ref    = item.get("ref", "")
        valuer = (item.get("valuer_name") or "Unassigned").split()[0]
        icon   = "✅" if ref in selected_refs else "⬜"
        rows.append([InlineKeyboardButton(
            f"{icon} {ref} — {valuer}", callback_data=f"dt_deltoggle:{i}",
        )])
    rows.append([
        InlineKeyboardButton(f"🗑 Delete Selected ({len(selected_refs)})", callback_data="dt_delconfirm"),
        InlineKeyboardButton("🛑 Cancel", callback_data="dt_delcancel"),
    ])
    return InlineKeyboardMarkup(rows)


async def _dt_run_delete_select(edit_fn, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the open DLV queue with a multi-select keyboard for bulk deletion."""
    items = load_dlv_batch()
    if not items:
        await edit_fn("ℹ️ The DLV queue is empty — nothing to delete.")
        await ctx.bot.send_message(chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_dt_sess(ctx)
    sess.delete_items    = items
    sess.delete_selected = set()

    await edit_fn(
        f"🗑 *Delete Task(s)* — {len(items)} queued ref(s)\n\n"
        "Tap refs to select, then confirm. ✅ = selected for deletion.",
        parse_mode="Markdown",
        reply_markup=_dt_delete_keyboard(items, sess.delete_selected),
    )
    return DT.DELETE_SELECT


def _dt_row_consideration_value(r: dict) -> float:
    """Numeric consideration for sorting the Excel export highest-to-lowest.
    The row's "consideration" field is already formatted ("KES 1,234.00" by
    _format_consideration), so strip everything but digits/decimal point
    before parsing. Missing/unparseable sorts last (below every real
    amount, which is >= 0)."""
    digits = re.sub(r"[^\d.]", "", r.get("consideration") or "")
    try:
        return float(digits) if digits else -1.0
    except ValueError:
        return -1.0


def _dt_build_excel(rows: List[dict]) -> bytes:
    """Build the DLV Tasks Excel export — only ever used for email delivery
    (Telegram delivery goes through _dt_send_telegram instead), so sorting
    rows here highest-consideration-first affects only the emailed file."""
    rows = sorted(rows, key=_dt_row_consideration_value, reverse=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DLV Tasks"
    cols = ["Reference Number", "Parcel Number", "Registry", "County",
            "Date Added", "Valuer", "Assessor", "Status"]
    style_header_row(ws, cols)
    _status_labels = {"dlv": "In DLV", "assessor": "With Assessor (not yet in DLV)"}
    for r in rows:
        ws.append([
            r.get("ref", ""),
            r.get("parcel", ""),
            r.get("registry", ""),
            r.get("county", ""),
            r.get("date_created", ""),
            r.get("valuer_name", ""),
            r.get("assessor", ""),
            _status_labels.get(r.get("location", ""), "Not found"),
        ])
    autofit_columns(ws, min_width=15, max_width=60)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def cmd_dlv_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    open_count   = len(load_dlv_batch())
    closed_count = len(load_dlv_closed())
    await update.message.reply_text(
        f"📋 *DLV Tasks*\n\nOpen: *{open_count}* task(s) · Closed: *{closed_count}* task(s)\n\n"
        "Open Tasks are still ONGOING/CREATED and available for reallocation.\n"
        "Closed Tasks have Completed or been Returned.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📂 Open Tasks",   callback_data="dt_scope:open"),
                InlineKeyboardButton("🔒 Closed Tasks", callback_data="dt_scope:closed"),
            ],
            [InlineKeyboardButton("👤 By Valuer",      callback_data="dt_scope:byvaluer")],
            [InlineKeyboardButton("🏷 By Tag",         callback_data="dt_scope:bytag")],
            [InlineKeyboardButton("🗑 Delete Task(s)", callback_data="dt_scope:delete")],
        ]),
    )
    return DT.SCOPE


async def _dt_run_open_tasks(edit_fn, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Live-check the DLV batch queue and present the valuer-exclusion keyboard for Open Tasks."""
    tokens = _any_valid_tokens()
    if not tokens:
        await edit_fn("❌ No valid cached tokens. Use *🔑 Refresh Auth* first.", parse_mode="Markdown")
        await ctx.bot.send_message(chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    await edit_fn("⏳ Checking DLV Batch queue status, please wait…")

    try:
        rows = await asyncio.to_thread(_dt_fetch_tasks, tokens)
    except Exception as exc:
        logger.error("DLV Tasks fetch failed: %s", exc, exc_info=True)
        await edit_fn(f"❌ Failed to fetch tasks: `{exc}`", parse_mode="Markdown")
        await ctx.bot.send_message(chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    if not rows:
        await edit_fn("ℹ️ No open DLV tasks — queue is empty or every item has since closed.")
        await ctx.bot.send_message(chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    # Collect unique valuers (preserve insertion order, dedupe by uid)
    seen: set = set()
    valuers: List[dict] = []
    for r in rows:
        uid  = r.get("valuer_uid") or ""
        name = r.get("valuer_name") or "Unassigned"
        if uid not in seen:
            seen.add(uid)
            valuers.append({"uid": uid, "name": name})
    valuers.sort(key=lambda v: v["name"])

    sess               = _get_dt_sess(ctx)
    sess.tasks         = rows
    sess.valuers       = valuers
    sess.excluded_uids = set()
    sess.email         = ""

    await edit_fn(
        f"📂 *Open DLV Tasks* — {len(rows)} task(s), {len(valuers)} valuer(s)\n\n"
        "Tap a valuer to *exclude* their tasks from the report. ✅ = included, ❌ = excluded.",
        parse_mode="Markdown",
        reply_markup=_dt_exclusion_keyboard(valuers, sess.excluded_uids),
    )
    return DT.EXCLUDE_VALUERS


async def _dt_send_closed_report(chat_id: int, rows: List[dict], bot) -> None:
    """Send the Closed DLV Tasks list (Completed + Returned), grouped by
    reason, each task rendered as the same labeled block every other DLV
    Tasks report uses (see _dt_format_report_item_block) instead of a
    packed one-liner."""
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        groups[r.get("closed_reason") or "unknown"].append(r)

    label_for = {"completed": "✅ Completed", "returned": "↩️ Returned", "unknown": "❓ Unknown"}
    lines = [f"🔒 *Closed DLV Tasks* — {len(rows)} task(s)"]
    for reason in ("completed", "returned", "unknown"):
        tasks = groups.get(reason)
        if not tasks:
            continue
        lines.append(f"{label_for[reason]} ({len(tasks)})")
        for i, t in enumerate(tasks, start=1):
            lines.append(_dt_format_report_item_block(i, t, show_valuer=True, section="closed"))

    async def _send(text, reply_markup):
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n\n")


# ──────────────────────────────────────────────────────────
# DLV Tasks — By Valuer (queued + closed history for one valuer)
# ──────────────────────────────────────────────────────────

_DT_PERIOD_OPTIONS = [("All time", 0), ("1 week", 7), ("2 weeks", 14), ("1 month", 30)]


def _dt_valuer_key(item: dict) -> str:
    """Stable identity for grouping/matching an item's valuer — uid if
    present, else the name, normalized (stripped + uppercased) so a queued
    item's Title-Case name and a closed record's actor_name (often returned
    in a different case by the API) still resolve to the same valuer."""
    uid = item.get("valuer_uid")
    if uid:
        return uid
    return (item.get("valuer_name") or "").strip().upper()


def _dt_load_assignment_items() -> List[dict]:
    """saved_assignments.json as a flat list of item dicts (ref merged in —
    the store keys by ref, but every filter/field-builder here expects a
    "ref" field like DLV Batch's own item dicts have). This is the "At
    Valuer's Desk" section's source: refs that have been assigned (so
    dropped out of saved_dlv_batch.json for good) but not yet found
    completed/returned (which would then move them into
    saved_dlv_closed.json) — a ref assigned outside DLV Batch's own
    processing loop (e.g. via New Assignment) never reaches the closed
    store at all, so without this it silently vanishes from every DLV
    Tasks report the moment it's assigned."""
    return [{**info, "ref": ref} for ref, info in load_saved_assignments().items()]


def _dt_collect_valuers() -> List[dict]:
    """Dedup, name-sorted list of every valuer seen in the open queue, at a
    valuer's desk (assigned but not yet closed), or in closed history, for
    the By Valuer picker: [{"key": uid-or-name, "name": display name}]."""
    seen: dict = {}
    for item in load_dlv_batch() + _dt_load_assignment_items() + load_dlv_closed():
        key = _dt_valuer_key(item)
        if not key or key in seen:
            continue
        seen[key] = item.get("valuer_name") or "Unassigned"
    return sorted(
        ({"key": k, "name": n} for k, n in seen.items()),
        key=lambda v: v["name"],
    )


def _dt_valuer_keyboard(valuers: List[dict]) -> InlineKeyboardMarkup:
    """Two-per-row picker of valuer names, each tap selecting that valuer by index."""
    rows = []
    for i in range(0, len(valuers), 2):
        pair = valuers[i:i + 2]
        rows.append([
            InlineKeyboardButton(v["name"], callback_data=f"dt_pickvaluer:{i + j}")
            for j, v in enumerate(pair)
        ])
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data="dt_pickvaluer_cancel")])
    return InlineKeyboardMarkup(rows)


def _dt_period_keyboard() -> InlineKeyboardMarkup:
    """History look-back period picker shown after a valuer or tag is chosen."""
    row = [InlineKeyboardButton(label, callback_data=f"dt_period:{days}") for label, days in _DT_PERIOD_OPTIONS]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("🛑 Cancel", callback_data="dt_period_cancel")]])


def _dt_tag_keyboard() -> InlineKeyboardMarkup:
    """Fixed-list tag picker for the By Tag scope, plus 🔢 Incremental — a
    special option showing every incremental-tagged ref together, since
    they're unique per ref ("B{n}-T{n}") rather than one of DLV_TAGS'
    small fixed set."""
    rows = [[InlineKeyboardButton(t, callback_data=f"dt_picktag:{t}")] for t in DLV_TAGS]
    rows.append([InlineKeyboardButton("🔢 Incremental", callback_data=f"dt_picktag:{INCREMENTAL_TAG_SENTINEL}")])
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data="dt_picktag_cancel")])
    return InlineKeyboardMarkup(rows)


def _dt_tag_display(tag: str) -> str:
    """Display-friendly label for a picked tag — the Incremental sentinel
    isn't a real tag value, just a filter meaning "any incremental tag"."""
    return "🔢 Incremental" if tag == INCREMENTAL_TAG_SENTINEL else tag


def _dt_view_keyboard() -> InlineKeyboardMarkup:
    """Report-view picker shown only for the Incremental tag (a single
    fixed tag has nothing to usefully sub-group by) — Flat (today's
    single combined list), By Valuer (all 3 sections sub-grouped per
    valuer), or By Batch (sections per batch number, "B2 tasks" etc.)."""
    rows = [
        [InlineKeyboardButton("📋 Flat List", callback_data="dt_pickview:flat")],
        [InlineKeyboardButton("👤 By Valuer",  callback_data="dt_pickview:valuer")],
        [InlineKeyboardButton("📦 By Batch",   callback_data="dt_pickview:batch")],
        [InlineKeyboardButton("🛑 Cancel",     callback_data="dt_pickview_cancel")],
    ]
    return InlineKeyboardMarkup(rows)


async def _dt_run_valuer_select(edit_fn, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the valuer picker built from everyone currently queued or in closed history."""
    valuers = _dt_collect_valuers()
    if not valuers:
        await edit_fn("ℹ️ No queued or historical DLV tasks yet — nothing to report on.")
        await ctx.bot.send_message(chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_dt_sess(ctx)
    sess.valuer_choices = valuers

    await edit_fn(
        "👤 *By Valuer* — pick a valuer to view their DLV report:",
        parse_mode="Markdown",
        reply_markup=_dt_valuer_keyboard(valuers),
    )
    return DT.PICK_VALUER


async def _dt_run_tag_select(edit_fn, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the fixed tag picker for the By Tag scope."""
    await edit_fn(
        "🏷 *By Tag* — pick a tag to view its DLV report:",
        parse_mode="Markdown",
        reply_markup=_dt_tag_keyboard(),
    )
    return DT.PICK_TAG


def _dt_consideration_value(item: dict) -> Optional[float]:
    """Numeric consideration for an item, or None if unknown/unparseable.
    Closed items carry the live-verified "consideration_amount" (set at
    close time); still-queued items only ever have "consideration" (cached
    from Fetch Tasks at queue time, since this report avoids live calls)."""
    raw = item.get("consideration_amount") or item.get("consideration")
    if not raw:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _dt_sum_consideration(items: List[dict]) -> str:
    """Formatted sum of every item's consideration in a list (all assumed
    KES, as everywhere else in this bot — no multi-currency handling)."""
    values = [v for v in (_dt_consideration_value(i) for i in items) if v is not None]
    currency = next((i.get("currency_code") for i in items if i.get("currency_code")), "KES")
    return format_consideration(str(sum(values)), currency) if values else format_consideration("0", currency)


def _dt_days_past(date_str: Optional[str]) -> str:
    """Render a stored datetime (either DLV Batch's "T"-separated ISO
    format or saved_assignments.json's "%Y-%m-%d %H:%M:%S") truncated to
    minutes, with a "- N days past" suffix — or "—" if missing/unparseable."""
    if not date_str:
        return "—"
    try:
        days = (datetime.now() - datetime.fromisoformat(date_str[:19])).days
        unit = "day" if days == 1 else "days"
        return f"{date_str[:16]} - {days} {unit} past"
    except ValueError:
        return date_str[:16]


def _dt_format_report_item_block(i: int, item: dict, show_valuer: bool, section: str) -> str:
    """One ref's labeled block for the Closed/By Valuer/By Tag reports —
    Assessor, Consideration, Parcel (task_block.py's shared field
    builders), plus Valuer (By Tag/Closed only, since both span multiple
    valuers) and a section-appropriate date. section is one of "queued"
    (still in saved_dlv_batch.json), "desk" (assigned — dropped from the
    queue but not yet found completed/returned, sourced from
    saved_assignments.json), or "closed" (completed/returned, from
    saved_dlv_closed.json). See task_block.format_labeled_block for the
    shared visual every report in the bot uses — only these fields differ."""
    fields: List[Tuple[str, str]] = []
    if show_valuer:
        fields.append(("👤 Valuer", item.get("valuer_name") or "—"))
    fields.append(assessor_field(item))
    fields.append(consideration_field(item))
    fields.append(parcel_field(item))
    if section == "closed":
        label_for = {"completed": "✅ Completed", "returned": "↩️ Returned"}
        status_label = label_for.get(item.get("closed_reason"), "❓ Unknown")
        fields.append(("📅 Closed", f"{_dt_days_past(item.get('closed_at'))} ({status_label})"))
    elif section == "desk":
        fields.append(("📅 Assigned", _dt_days_past(item.get("assigned_at"))))
    else:
        fields.append(("📅 Queued", _dt_days_past(item.get("queued_at"))))
    tag = tag_field(item)
    if tag:
        fields.append(tag)

    return format_labeled_block(i, item.get("ref"), fields)


def _dt_format_report_lines(header: str, queued: List[dict], desk: List[dict], closed: List[dict],
                             period_label: str, show_valuer: bool = False) -> List[str]:
    """Text lines shared by the By Valuer and By Tag reports, in three
    sections: Currently Queued (still in saved_dlv_batch.json), At
    Valuer's Desk (assigned but not yet found completed/returned, from
    saved_assignments.json), and Valuer Completed (from
    saved_dlv_closed.json, within the chosen look-back period) — each ref
    rendered as its own labeled block (Ref, Consideration, Parcel,
    Assessor, Tag) rather than a single packed line. Uses only fields
    already stored on the queue/assignment/closed items (no live API
    calls). show_valuer=True adds a Valuer field per block — needed for By
    Tag, which spans multiple valuers, but redundant for By Valuer, where
    it's already implied by the header."""
    lines = [header]

    lines.append(f"⏳ *Currently Queued* ({len(queued)}) — Total: {_dt_sum_consideration(queued)}")
    if not queued:
        lines.append("  _none_")
    for i, item in enumerate(sorted(queued, key=lambda i: i.get("queued_at", "")), start=1):
        lines.append(_dt_format_report_item_block(i, item, show_valuer, section="queued"))

    lines.append(f"🏢 *At Valuer's Desk* ({len(desk)}) — Total: {_dt_sum_consideration(desk)}")
    if not desk:
        lines.append("  _none_")
    for i, item in enumerate(sorted(desk, key=lambda i: i.get("assigned_at", ""), reverse=True), start=1):
        lines.append(_dt_format_report_item_block(i, item, show_valuer, section="desk"))

    lines.append(f"📜 *Valuer Completed* ({period_label}) — {len(closed)} — Total: {_dt_sum_consideration(closed)}")
    if not closed:
        lines.append("  _none_")
    for i, item in enumerate(sorted(closed, key=lambda i: i.get("closed_at", ""), reverse=True), start=1):
        lines.append(_dt_format_report_item_block(i, item, show_valuer, section="closed"))

    return lines


def _dt_format_valuer_report(valuer_name: str, queued: List[dict], desk: List[dict], closed: List[dict],
                              period_label: str) -> List[str]:
    """One valuer's DLV report — see _dt_format_report_lines."""
    return _dt_format_report_lines(f"👤 *DLV Report — {valuer_name}*", queued, desk, closed, period_label)


def _dt_format_tag_report(tag: str, queued: List[dict], desk: List[dict], closed: List[dict],
                           period_label: str) -> List[str]:
    """One tag's DLV report, spanning every valuer — see _dt_format_report_lines."""
    return _dt_format_report_lines(f"🏷 *DLV Report — Tag: {_dt_tag_display(tag)}*", queued, desk, closed,
                                    period_label, show_valuer=True)


def _dt_format_tag_report_by_valuer(tag: str, queued: List[dict], desk: List[dict], closed: List[dict],
                                     period_label: str) -> List[str]:
    """By Tag "By Valuer" view (Incremental only) — same 3 sections as the
    flat report (Currently Queued / At Valuer's Desk / Valuer Completed),
    but each section's items are sub-grouped under a per-valuer heading
    instead of one flat list, since the flat view mixes every valuer's
    tasks together and the Valuer field alone doesn't let you scan one
    valuer's load at a glance."""
    lines = [f"🏷 *DLV Report — Tag: {_dt_tag_display(tag)}* (view: By Valuer)"]

    def _section(header: str, items: List[dict], sort_key, reverse: bool, section: str):
        lines.append(header)
        if not items:
            lines.append("  _none_")
            return
        by_valuer: dict = {}
        for item in items:
            by_valuer.setdefault(item.get("valuer_name") or "—", []).append(item)
        for valuer_name in sorted(by_valuer):
            group = sorted(by_valuer[valuer_name], key=sort_key, reverse=reverse)
            lines.append(f"👤 *{md_escape(valuer_name)}* ({len(group)})")
            for i, item in enumerate(group, start=1):
                lines.append(_dt_format_report_item_block(i, item, show_valuer=False, section=section))

    _section(f"⏳ *Currently Queued* ({len(queued)}) — Total: {_dt_sum_consideration(queued)}",
              queued, lambda i: i.get("queued_at", ""), False, "queued")
    _section(f"🏢 *At Valuer's Desk* ({len(desk)}) — Total: {_dt_sum_consideration(desk)}",
              desk, lambda i: i.get("assigned_at", ""), True, "desk")
    _section(f"📜 *Valuer Completed* ({period_label}) — {len(closed)} — Total: {_dt_sum_consideration(closed)}",
              closed, lambda i: i.get("closed_at", ""), True, "closed")

    return lines


def _dt_batch_status_label(item: dict) -> str:
    """Status label for one item in the By Batch view, derived from which
    section it came from ("queued"/"desk" are self-explanatory; "closed"
    items carry their own completed/returned outcome)."""
    if item["section"] == "queued":
        return "⏳ Queued"
    if item["section"] == "desk":
        return "🏢 At Desk"
    label_for = {"completed": "✅ Completed", "returned": "↩️ Returned"}
    return label_for.get(item.get("closed_reason"), "❓ Unknown")


def _dt_group_by_batch(queued: List[dict], desk: List[dict], closed: List[dict]) -> dict:
    """Merge queued/desk/closed items tagged with an incremental "B{n}-T{n}"
    value into one dict keyed by batch number, each item annotated with its
    parsed batch_number/task_number/section, sorted by task_number within
    a batch. Items whose tag isn't a valid incremental tag (shouldn't
    happen — this view is only reachable via the Incremental tag filter,
    which already restricts to those — but tags can be edited/removed
    between queueing and this report running) are skipped."""
    groups: dict = {}
    for items, section in ((queued, "queued"), (desk, "desk"), (closed, "closed")):
        for item in items:
            parsed = parse_incremental_tag(item.get("tag", ""))
            if not parsed:
                continue
            batch_number, task_number = parsed
            groups.setdefault(batch_number, []).append({**item, "batch_number": batch_number,
                                                          "task_number": task_number, "section": section})
    for batch_number in groups:
        groups[batch_number].sort(key=lambda i: i["task_number"])
    return groups


def _dt_format_tag_report_by_batch(tag: str, queued: List[dict], desk: List[dict], closed: List[dict],
                                    period_label: str) -> List[str]:
    """By Tag "By Batch" view (Incremental only) — one section per batch
    number ("Batch 2" etc., dlv_incremental.py's 📦 By Batch style) instead
    of the flat report's Queued/Desk/Completed split, so all of one
    batch's tasks (wherever each currently sits in its own lifecycle) show
    together."""
    grouped = _dt_group_by_batch(queued, desk, closed)
    lines = [f"🏷 *DLV Report — Tag: {_dt_tag_display(tag)}* (view: By Batch, {period_label})"]
    if not grouped:
        lines.append("  _none_")
        return lines

    for batch_number in sorted(grouped):
        items = grouped[batch_number]
        lines.append(f"*Batch {batch_number}* — {len(items)} task(s)")
        for item in items:
            fields = [
                ("🔢 Batch/Task", f"B{batch_number}-T{item['task_number']}"),
                ("📊 Status", _dt_batch_status_label(item)),
                ("👤 Valuer", item.get("valuer_name") or "—"),
                assessor_field(item),
                consideration_field(item),
                parcel_field(item),
            ]
            lines.append(format_labeled_block(item["task_number"], item.get("ref"), fields))

    return lines


async def _dt_send_valuer_report(chat_id: int, valuer_name: str, queued: List[dict], desk: List[dict],
                                  closed: List[dict], period_label: str, bot) -> None:
    """Send the By Valuer DLV report (queued + at-desk + closed-history) as chunked Telegram messages."""
    lines = _dt_format_valuer_report(valuer_name, queued, desk, closed, period_label)

    async def _send(text, reply_markup):
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n\n")


async def _dt_send_tag_report(chat_id: int, tag: str, queued: List[dict], desk: List[dict],
                               closed: List[dict], period_label: str, bot, view: str = "flat") -> None:
    """Send the By Tag DLV report as chunked Telegram messages — view selects
    Flat (default, spanning every valuer in one list), By Valuer (all 3
    sections sub-grouped per valuer), or By Batch (Incremental only —
    sections per batch number) rendering."""
    if view == "valuer":
        lines = _dt_format_tag_report_by_valuer(tag, queued, desk, closed, period_label)
    elif view == "batch":
        lines = _dt_format_tag_report_by_batch(tag, queued, desk, closed, period_label)
    else:
        lines = _dt_format_tag_report(tag, queued, desk, closed, period_label)

    async def _send(text, reply_markup):
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n\n")


async def recv_dt_pick_valuer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """By Valuer: handle the valuer-picker tap, then show the history-period picker."""
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "dt_pickvaluer_cancel":
        await query.edit_message_text("❌ Cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_dt_sess(ctx)
    idx  = int(query.data.split(":")[1])
    if idx >= len(sess.valuer_choices):
        return DT.PICK_VALUER
    sess.selected_valuer = sess.valuer_choices[idx]
    sess.report_mode     = "valuer"

    await query.edit_message_text(
        f"👤 *{md_escape(sess.selected_valuer['name'])}*\n\nFilter history by period:",
        parse_mode="Markdown",
        reply_markup=_dt_period_keyboard(),
    )
    return DT.PICK_PERIOD


async def recv_dt_pick_tag(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """By Tag: handle the tag-picker tap, then show the history-period picker."""
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "dt_picktag_cancel":
        await query.edit_message_text("❌ Cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_dt_sess(ctx)
    sess.selected_tag = query.data.split(":", 1)[1]
    sess.report_mode  = "tag"
    sess.report_view  = "flat"

    if sess.selected_tag == INCREMENTAL_TAG_SENTINEL:
        await query.edit_message_text(
            f"🏷 *{_dt_tag_display(sess.selected_tag)}*\n\nChoose how to view this report:",
            parse_mode="Markdown",
            reply_markup=_dt_view_keyboard(),
        )
        return DT.PICK_VIEW

    await query.edit_message_text(
        f"🏷 *{_dt_tag_display(sess.selected_tag)}*\n\nFilter history by period:",
        parse_mode="Markdown",
        reply_markup=_dt_period_keyboard(),
    )
    return DT.PICK_PERIOD


async def recv_dt_pick_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """By Tag + Incremental only: handle the report-view picker tap, then show the history-period picker."""
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "dt_pickview_cancel":
        await query.edit_message_text("❌ Cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_dt_sess(ctx)
    sess.report_view = query.data.split(":", 1)[1]

    await query.edit_message_text(
        f"🏷 *{_dt_tag_display(sess.selected_tag)}*\n\nFilter history by period:",
        parse_mode="Markdown",
        reply_markup=_dt_period_keyboard(),
    )
    return DT.PICK_PERIOD


def _dt_tag_matches(item: dict, target_key: str) -> bool:
    """True if item's tag matches the By Tag scope's target — either an
    exact fixed-vocabulary tag, or (when target_key is the Incremental
    sentinel) any auto-sequenced "B{n}-T{n}" tag at all, since those are
    unique per ref rather than one shared value to match exactly."""
    if target_key == INCREMENTAL_TAG_SENTINEL:
        return is_incremental_tag(item.get("tag", ""))
    return item.get("tag") == target_key


def _dt_gather_report_data(scope: str, target_key: str, period_days: int) -> Tuple[List[dict], List[dict], List[dict]]:
    """Gather (queued, at-desk, closed-within-period) for a given scope
    ("valuer" or "tag") + target — the data-gathering half of By
    Valuer/By Tag, factored out of recv_dt_period so
    dlv_report_schedule.py's background job can reuse the exact same
    filtering logic for its own scheduled runs rather than duplicating it."""
    if scope == "tag":
        queued = [i for i in load_dlv_batch() if _dt_tag_matches(i, target_key)]
        closed = [c for c in load_dlv_closed() if _dt_tag_matches(c, target_key)]
    else:
        queued = [i for i in load_dlv_batch() if _dt_valuer_key(i) == target_key]
        closed = [c for c in load_dlv_closed() if _dt_valuer_key(c) == target_key]

    closed_refs = {c["ref"] for c in closed}
    if scope == "tag":
        desk = [a for a in _dt_load_assignment_items() if _dt_tag_matches(a, target_key) and a["ref"] not in closed_refs]
    else:
        desk = [a for a in _dt_load_assignment_items()
                if _dt_valuer_key(a) == target_key and a["ref"] not in closed_refs]

    if period_days:
        cutoff = _date_cutoff_str(period_days)
        closed = [c for c in closed if _within_days(c.get("closed_at", ""), cutoff)]
        desk   = [a for a in desk if _within_days(a.get("assigned_at", ""), cutoff)]

    return queued, desk, closed


async def recv_dt_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """By Valuer/By Tag: handle the period-picker tap, filter, and send the
    combined report — branches on sess.report_mode set by whichever picker
    (recv_dt_pick_valuer or recv_dt_pick_tag) led here, since both share this
    step."""
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "dt_period_cancel":
        await query.edit_message_text("❌ Cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    days = int(query.data.split(":")[1])
    sess = _get_dt_sess(ctx)
    period_label = next((label for label, d in _DT_PERIOD_OPTIONS if d == days), "All time")

    if sess.report_mode == "tag":
        tag = sess.selected_tag
        queued, desk, closed = _dt_gather_report_data("tag", tag, days)
        await query.edit_message_text(f"⏳ Building report for tag *{_dt_tag_display(tag)}*…", parse_mode="Markdown")
        await _dt_send_tag_report(query.message.chat_id, tag, queued, desk, closed, period_label, ctx.bot,
                                   view=sess.report_view)
    else:
        valuer = sess.selected_valuer
        key    = valuer["key"]
        queued, desk, closed = _dt_gather_report_data("valuer", key, days)
        await query.edit_message_text(f"⏳ Building report for *{md_escape(valuer['name'])}*…", parse_mode="Markdown")
        await _dt_send_valuer_report(query.message.chat_id, valuer["name"], queued, desk, closed, period_label, ctx.bot)

    await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
    return ConversationHandler.END


async def recv_dt_scope(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    scope = query.data.split(":")[1]   # "open" | "closed" | "byvaluer" | "bytag" | "delete"

    if scope == "byvaluer":
        return await _dt_run_valuer_select(query.edit_message_text, query.message.chat_id, ctx)

    if scope == "bytag":
        return await _dt_run_tag_select(query.edit_message_text, query.message.chat_id, ctx)

    if scope == "closed":
        rows = load_dlv_closed()
        if not rows:
            await query.edit_message_text("ℹ️ No closed tasks yet.")
            await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
            return ConversationHandler.END
        await _dt_send_closed_report(query.message.chat_id, rows, ctx.bot)
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    if scope == "delete":
        return await _dt_run_delete_select(query.edit_message_text, query.message.chat_id, ctx)

    return await _dt_run_open_tasks(query.edit_message_text, query.message.chat_id, ctx)


async def recv_dt_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    uid  = query.data.split(":")[1]
    sess = _get_dt_sess(ctx)
    if uid in sess.excluded_uids:
        sess.excluded_uids.discard(uid)
    else:
        sess.excluded_uids.add(uid)
    await query.edit_message_reply_markup(
        reply_markup=_dt_exclusion_keyboard(sess.valuers, sess.excluded_uids),
    )
    return DT.EXCLUDE_VALUERS


async def recv_dt_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "dt_cancel":
        await query.edit_message_text("❌ Cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_dt_sess(ctx)
    filtered = [
        r for r in sess.tasks
        if (r.get("valuer_uid") or "") not in sess.excluded_uids
    ]
    sess.tasks = filtered   # store filtered for delivery step

    excluded_count = len(sess.excluded_uids)
    await query.edit_message_text(
        f"✅ *{len(filtered)} task(s)* after excluding {excluded_count} valuer(s).\n\n"
        "How would you like to receive the report?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📩 Send to Email", callback_data="dt_delivery:email"),
                InlineKeyboardButton("💬 View on Telegram", callback_data="dt_delivery:telegram"),
            ],
        ]),
    )
    return DT.DELIVERY


async def recv_dt_delivery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    mode  = query.data.split(":")[1]
    sess  = _get_dt_sess(ctx)

    if mode == "telegram":
        await _dt_send_telegram(query.message.chat_id, sess.tasks, ctx.bot)
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    # email mode — ask for address
    await query.edit_message_text(
        "📧 Enter the email address to receive the report, or send `skip` to get it only via Telegram:",
        parse_mode="Markdown",
    )
    return DT.EMAIL_INPUT


async def recv_dt_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    text = (update.message.text or "").strip()
    sess = _get_dt_sess(ctx)

    if text.lower() != "skip":
        if "@" not in text or "." not in text.split("@")[-1]:
            await update.message.reply_text(
                "❌ Invalid email. Enter a valid address or send `skip`.",
                parse_mode="Markdown",
            )
            return DT.EMAIL_INPUT
        sess.email = text

    xlsx_bytes = _dt_build_excel(sess.tasks)
    filename   = f"DLV_Tasks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    chat_id    = update.effective_chat.id

    await ctx.bot.send_document(
        chat_id,
        document=io.BytesIO(xlsx_bytes),
        filename=filename,
        caption=f"📋 DLV Tasks — {len(sess.tasks)} task(s)",
    )

    if sess.email:
        try:
            _send_bulk_export_email(sess.email, filename, xlsx_bytes)
            await update.message.reply_text(f"📧 File also sent to *{md_escape(sess.email)}*.", parse_mode="Markdown")
        except Exception as exc:
            await update.message.reply_text(f"⚠️ Email failed: `{exc}`", parse_mode="Markdown")

    await update.message.reply_text("Main menu.", reply_markup=_main_menu())
    return ConversationHandler.END


async def recv_dt_delete_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    sess = _get_dt_sess(ctx)
    idx  = int(query.data.split(":")[1])
    if idx >= len(sess.delete_items):
        return DT.DELETE_SELECT

    ref = sess.delete_items[idx].get("ref", "")
    if ref in sess.delete_selected:
        sess.delete_selected.discard(ref)
    else:
        sess.delete_selected.add(ref)

    await query.edit_message_reply_markup(
        reply_markup=_dt_delete_keyboard(sess.delete_items, sess.delete_selected),
    )
    return DT.DELETE_SELECT


async def recv_dt_delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "dt_delcancel":
        await query.edit_message_text("❌ Deletion cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_dt_sess(ctx)
    if not sess.delete_selected:
        await query.edit_message_text(
            "⚠️ Nothing selected. Tap refs to select, then confirm.\n\n"
            f"🗑 *Delete Task(s)* — {len(sess.delete_items)} queued ref(s)",
            parse_mode="Markdown",
            reply_markup=_dt_delete_keyboard(sess.delete_items, sess.delete_selected),
        )
        return DT.DELETE_SELECT

    remaining = [i for i in load_dlv_batch() if i.get("ref") not in sess.delete_selected]
    # save_dlv_batch never removes a ref on its own (see dlv_core.save_dlv_batch) —
    # a bare user deletion has no other status call updating these refs, so
    # mark them removed explicitly before the trimmed list is saved.
    mark_removed(sess.delete_selected)
    save_dlv_batch(remaining)

    removed_refs = ", ".join(f"`{r}`" for r in sorted(sess.delete_selected))
    await query.edit_message_text(
        f"🗑 Removed *{len(sess.delete_selected)}* task(s) from the DLV queue:\n{removed_refs}\n\n"
        f"{len(remaining)} task(s) remain queued.",
        parse_mode="Markdown",
    )
    await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
    return ConversationHandler.END


def _dt_format_task_block(i: int, t: dict) -> str:
    """Open Tasks' full detail block (status, node, valuer, assessor,
    registry, county, consideration, parcel, added) — see
    task_block.format_labeled_block for the shared visual every report in
    the bot uses; only these fields differ."""
    node_label = _NODE_LABELS.get(t.get("node", ""), t.get("node") or "—")
    location   = t.get("location", "")
    if location == "assessor":
        note = "⏳ _still with Assessor, not yet in DLV_"
    elif not t.get("found", True):
        note = "❓ _not found in Assessor or DLV queues_"
    else:
        note = ""

    fields: List[Tuple[str, str]] = [
        ("📊 Status", t.get("status") or "—"),
        ("🔄 Node", node_label),
        ("👤 Valuer", t.get("valuer_name") or "—"),
        assessor_field(t),
        ("🏢 Registry", t.get("registry") or "—"),
        ("📍 County", t.get("county") or "—"),
        consideration_field(t),
        parcel_field(t),
        ("📅 Added", t.get("date_created") or "—"),
    ]
    tag = tag_field(t)
    if tag:
        fields.append(tag)

    block = format_labeled_block(i, t.get("ref"), fields)
    if note:
        block += f"\n     {note}"
    return block


async def _dt_send_telegram(chat_id: int, rows: List[dict], bot) -> None:
    """Send DLV task list as Telegram messages grouped by valuer, one detail block per task."""
    if not rows:
        await bot.send_message(chat_id, "ℹ️ No tasks to display.")
        return

    # Group by valuer
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        key = r.get("valuer_name") or "Unassigned"
        groups[key].append(r)

    lines = [f"📋 *DLV Tasks Report* — {len(rows)} task(s)\n"]
    for valuer, tasks in sorted(groups.items()):
        lines.append(f"\n👤 *{md_escape(valuer)}* ({len(tasks)} task(s))")
        for i, t in enumerate(tasks, start=1):
            lines.append(_dt_format_task_block(i, t))

    async def _send(text, reply_markup):
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n")


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the DLV Tasks conversation into the given Application."""
    dt_conv = ConversationHandler(
        entry_points=[
            CommandHandler("dlvtasks", cmd_dlv_tasks),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_DLV_TASKS)}$"), cmd_dlv_tasks),
        ],
        states={
            DT.SCOPE: [CallbackQueryHandler(recv_dt_scope, pattern=r"^dt_scope:")],
            DT.EXCLUDE_VALUERS: [
                CallbackQueryHandler(recv_dt_toggle,  pattern=r"^dt_toggle:"),
                CallbackQueryHandler(recv_dt_confirm, pattern=r"^dt_confirm$|^dt_cancel$"),
            ],
            DT.DELIVERY:   [CallbackQueryHandler(recv_dt_delivery, pattern=r"^dt_delivery:")],
            DT.EMAIL_INPUT: [MessageHandler(not_cancel, recv_dt_email)],
            DT.DELETE_SELECT: [
                CallbackQueryHandler(recv_dt_delete_toggle,  pattern=r"^dt_deltoggle:"),
                CallbackQueryHandler(recv_dt_delete_confirm, pattern=r"^dt_delconfirm$|^dt_delcancel$"),
            ],
            DT.PICK_VALUER: [CallbackQueryHandler(recv_dt_pick_valuer, pattern=r"^dt_pickvaluer")],
            DT.PICK_TAG:    [CallbackQueryHandler(recv_dt_pick_tag,    pattern=r"^dt_picktag")],
            DT.PICK_VIEW:   [CallbackQueryHandler(recv_dt_pick_view,   pattern=r"^dt_pickview")],
            DT.PICK_PERIOD: [CallbackQueryHandler(recv_dt_period,     pattern=r"^dt_period")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(dt_conv)
