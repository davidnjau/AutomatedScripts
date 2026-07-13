#!/usr/bin/env python3
"""
dlv_tasks.py
============
DLV Tasks — live-check the DLV Batch queue (📋 DLV Tasks button /
/dlvtasks) and produce an Open/Closed report (Telegram text or Excel),
plus the multi-select bulk-delete flow for the open queue.

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
from typing import List

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
    _main_menu,
    _NODE_LABELS,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    logger,
    not_cancel,
)
from dlv_core import (
    _append_dlv_closed,
    _classify_dlv_detail,
    _extract_assessor,
    _fetch_ref_detail_dlv,
    _fetch_stampduty_detail,
    _search_ref_dlv,
    _search_ref_stampduty,
    load_dlv_batch,
    load_dlv_closed,
    save_dlv_batch,
)
from email_service import _send_bulk_export_email
from excel_report import autofit_columns, style_header_row
from fetch_tasks_cache import _fetch_tasks_log_lookup
from telegram_report import _send_chunked_report


# ──────────────────────────────────────────────────────────
# States — DLV Tasks conversation
# ──────────────────────────────────────────────────────────
class DT(Enum):
    SCOPE           = auto()   # choose Open Tasks vs Closed Tasks vs Delete Task(s)
    EXCLUDE_VALUERS = auto()   # toggle which valuers to exclude
    DELIVERY        = auto()   # telegram or email
    EMAIL_INPUT     = auto()   # enter email address
    DELETE_SELECT   = auto()   # multi-select which queued refs to delete


# ── DLV Tasks session ──────────────────────────────────────

@dataclass
class DTSession:
    tasks:            List[dict] = field(default_factory=list)   # enriched task rows
    valuers:          List[dict] = field(default_factory=list)   # unique valuers [{uid, name}]
    excluded_uids:    set        = field(default_factory=set)
    email:            str        = ""
    delete_items:     List[dict] = field(default_factory=list)   # open queue snapshot for deletion
    delete_selected:  set        = field(default_factory=set)    # refs selected for deletion


def _get_dt_sess(ctx: ContextTypes.DEFAULT_TYPE) -> DTSession:
    if "dt_session" not in ctx.user_data:
        ctx.user_data["dt_session"] = DTSession()
    return ctx.user_data["dt_session"]


# ──────────────────────────────────────────────────────────
# DLV Tasks — fetch and enrich ongoing tasks
# ──────────────────────────────────────────────────────────

def _format_consideration(amount: str, currency: str) -> str:
    """Format a raw consideration amount + currency code as 'KES 6,000,000.00', or '' if amount is empty."""
    if not amount:
        return ""
    try:
        return f"{currency or 'KES'} {float(amount):,.2f}"
    except (ValueError, TypeError):
        return str(amount)


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
                    row["assessor"] = _extract_assessor([
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
                        row["consideration"] = _format_consideration(
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


def _dt_build_excel(rows: List[dict]) -> bytes:
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
    """Send the Closed DLV Tasks list (Completed + Returned), grouped by reason."""
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        groups[r.get("closed_reason") or "unknown"].append(r)

    label_for = {"completed": "✅ Completed", "returned": "↩️ Returned", "unknown": "❓ Unknown"}
    lines = [f"🔒 *Closed DLV Tasks* — {len(rows)} task(s)\n"]
    for reason in ("completed", "returned", "unknown"):
        tasks = groups.get(reason)
        if not tasks:
            continue
        lines.append(f"\n{label_for[reason]} ({len(tasks)})")
        for i, t in enumerate(tasks, start=1):
            currency   = t.get("currency_code") or ""
            amount     = t.get("consideration_amount") or ""
            amount_str = f" | {currency} {amount}" if amount else ""
            closed_at  = (t.get("closed_at") or "")[:10]
            lines.append(
                f"  {i}. `{t.get('ref', '—')}` | Valuer: {t.get('valuer_name') or '—'}"
                f"{amount_str} | Closed: {closed_at or '—'}"
            )

    async def _send(text, reply_markup):
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n")


async def recv_dt_scope(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    scope = query.data.split(":")[1]   # "open" | "closed" | "delete"

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
            await update.message.reply_text(f"📧 File also sent to *{sess.email}*.", parse_mode="Markdown")
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
    """Format one task's full detail block (ref, status, node, valuer, registry, county,
    consideration, parcel, created), matching Lookup Reference's field layout."""
    node_label = _NODE_LABELS.get(t.get("node", ""), t.get("node") or "—")
    location   = t.get("location", "")
    if location == "assessor":
        note = "⏳ _still with Assessor, not yet in DLV_"
    elif not t.get("found", True):
        note = "❓ _not found in Assessor or DLV queues_"
    else:
        note = ""

    block = (
        f"  {i}. 📌 *Ref:* `{t.get('ref') or '—'}`\n"
        f"     📊 Status: {t.get('status') or '—'}\n"
        f"     🔄 Node: {node_label}\n"
        f"     👤 Valuer: {t.get('valuer_name') or '—'}\n"
        f"     🏢 Registry: {t.get('registry') or '—'}\n"
        f"     📍 County: {t.get('county') or '—'}\n"
        f"     💰 Consideration: {t.get('consideration') or '—'}\n"
        f"     📋 Parcel: {t.get('parcel') or '—'}\n"
        f"     📅 Created: {t.get('date_created') or '—'}"
    )
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
        lines.append(f"\n👤 *{valuer}* ({len(tasks)} task(s))")
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
