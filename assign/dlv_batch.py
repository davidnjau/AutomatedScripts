#!/usr/bin/env python3
"""
dlv_batch.py
============
DLV Batch — free-text batch assignment queueing (📥 DLV Batch button /
/dlvbatch), the persistent 5-minute processing job, and the 🔍 DLV Queue
viewer.

Users send lines like "REF1, REF2 : Valuer Name"; refs are queued into
saved_dlv_batch.json (via dlv_core) and a repeating background job tries
to find + assign each one in DLV until it succeeds or closes out.

Before confirming, refs can optionally be tagged (one of dlv_core.DLV_TAGS
per ref) via the "🏷 Tag Tasks" step — the tag rides along on the queue
item and, since closed records are built by spreading the item dict,
carries through to the closed store automatically. DLV Tasks' "By Tag"
report and the 🔍 DLV Queue viewer both surface it.

Call register(app) from bot.py's main() to wire this feature in.
"""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ardhisasa_auth import AuthTokens, build_session

from common import (
    ALLOWED_IDS,
    BTN_DLV_BATCH,
    BTN_DLV_QUEUE,
    _any_valid_tokens,
    _CANCEL_FILTER,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    load_saved_valuers,
    logger,
    not_cancel,
    persist_assignment,
)
from dlv_core import (
    DLV_TAGS,
    _append_dlv_closed,
    _classify_dlv_detail,
    _fetch_ref_detail_dlv,
    _search_ref_dlv,
    load_dlv_batch,
    save_dlv_batch,
)
from endpoints import ACCOUNTS_LIST_URL, STAMP_DUTY_FIX_APPLICATION_URL
from fetch_tasks_cache import _fetch_tasks_log_lookup, _fetch_tasks_log_remove


# ──────────────────────────────────────────────────────────
# States — DLV Batch conversation
# ──────────────────────────────────────────────────────────
class DB(Enum):
    INPUT_BATCH    = auto()   # waiting for batch text
    CONFIRM_BATCH  = auto()   # waiting for confirm/cancel/tag
    TAG_PICK_REF   = auto()   # tagging: pick which ref to tag next
    TAG_PICK_VALUE = auto()   # tagging: pick a fixed tag value for the selected ref


@dataclass
class DBSession:
    groups: List[Dict] = field(default_factory=list)
    # groups: [{refs, valuer_name, valuer_uid, valuer_acct, status}]
    tag_refs:      List[str]      = field(default_factory=list)   # resolved refs eligible for tagging
    tag_by_ref:    Dict[str, str] = field(default_factory=dict)    # ref -> tag, only for tagged refs
    tag_ref_index: int            = 0                              # which tag_refs[] entry is being tagged


def _get_db_sess(ctx: ContextTypes.DEFAULT_TYPE) -> DBSession:
    if "db_session" not in ctx.user_data:
        ctx.user_data["db_session"] = DBSession()
    return ctx.user_data["db_session"]


# ──────────────────────────────────────────────────────────
# DLV Batch — group parsing & valuer resolution
# ──────────────────────────────────────────────────────────

def _parse_batch_input(text: str) -> List[Dict]:
    """
    Parse lines of format:  "REF1, REF2 : Valuer Name"
    Returns [{refs: [...], valuer_name_raw: "..."}]
    """
    groups = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        ref_part, valuer_part = line.rsplit(":", 1)
        refs = [r.strip().upper() for r in ref_part.split(",") if r.strip()]
        valuer_name_raw = valuer_part.strip()
        if refs and valuer_name_raw:
            groups.append({"refs": refs, "valuer_name_raw": valuer_name_raw})
    return groups


def _resolve_valuer_from_saved(name: str) -> Optional[Dict]:
    """Case-insensitive substring match against saved valuers."""
    name_lower = name.lower()
    for v in load_saved_valuers():
        if name_lower in v["name"].lower():
            return v
    return None


def _search_valuer_api(name: str, tokens: AuthTokens) -> List[Dict]:
    http_sess = build_session()
    resp = http_sess.get(
        ACCOUNTS_LIST_URL,
        headers={"Authorization": f"Bearer {tokens.access_token}", "JWTAUTH": f"Bearer {tokens.jwt}"},
        params={"account_type": "STAFF", "filter_type": "ACTIVE", "page": 1, "search": name},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


_DLV_BATCH_WORKERS = 8


def _process_dlv_batch_item(tokens: AuthTokens, http_sess, assign_url: str, auth_hdrs: dict, item: Dict) -> Dict:
    """
    Process a single queued ref: search + detail-view + classify, then either
    assign, report on an existing assignment, or close it out. Returns
    {"item", "keep", "line", "closed"} — pure w.r.t. shared state, so callers
    can run this across worker threads and merge results afterwards.
    """
    ref         = item.get("ref", "")
    valuer_name = item.get("valuer_name", "")
    valuer_uid  = item.get("valuer_uid", "")

    try:
        task = _search_ref_dlv(tokens, ref)
        if not task:
            # Not in DLV yet — keep for retry
            item["last_error"] = "Not found in DLV endpoint"
            return {"item": item, "keep": True, "line": None, "closed": None}

        detail = _fetch_ref_detail_dlv(tokens, task["id"])
        if not detail:
            # Transient failure — keep for retry
            item["last_error"] = "Detail fetch returned empty response"
            return {"item": item, "keep": True, "line": None, "closed": None}

        info = _classify_dlv_detail(detail)

        if info["bucket"] == "closed":
            # No longer available for reallocation — move out of the active
            # queue into the closed store instead of dropping it silently.
            closed_record = {
                **item,
                "last_error":           None,
                "closed_reason":        info["closed_reason"],
                "application_status":   info["application_status"],
                "node":                 info["node"],
                "request_type":         task.get("_request_type", ""),
                "consideration_amount": info["consideration_amount"],
                "currency_code":        info["currency_code"],
                "valuer_name":          info["actor_name"] or valuer_name,
                "closed_at":            datetime.now().isoformat(timespec="seconds"),
            }
            label = "Completed" if info["closed_reason"] == "completed" else "Returned"
            return {"item": item, "keep": False, "line": f"🔒 `{ref}` — Closed ({label})", "closed": closed_record}

        item["assessor"] = info["assessor_name"]
        node = info["node"]

        if node == "VALUATION_STAMP_DUTY_CREATED":
            r = http_sess.post(
                assign_url, headers=auth_hdrs,
                json={
                    "reference_number":  ref,
                    "valuation_officer": valuer_uid,
                    "node":              "VALUATION_STAMP_DUTY_VALUER_REPORT",
                },
                timeout=30,
            )
            r.raise_for_status()
            # Persist before reporting success; if disk write fails, log it but
            # still report the correct outcome — the API assignment did succeed.
            try:
                persist_assignment(ref, valuer_name, valuer_uid)
            except Exception as _pe:
                logger.error("persist_assignment failed for %s: %s", ref, _pe)
            return {"item": item, "keep": False, "line": f"✅ `{ref}` — assigned to *{valuer_name}*", "closed": None}

        if node == "VALUATION_STAMP_DUTY_VALUER_REPORT":
            actors = detail.get("actors", [])
            vo = next((a for a in actors if a.get("role") == "VALUATION OFFICER"), None)
            vo_details = (vo.get("user_details") or {}) if vo else {}
            actor_name = vo_details.get("names", "")
            actor_uid  = vo_details.get("id", "")
            if actor_name and str(actor_uid) == str(valuer_uid):
                # Distinguish "correctly assigned already" from "taken by someone
                # else" — both used to render identically as "already with X",
                # which read as a failure even when the intended valuer already had it.
                line = f"✅ `{ref}` — already correctly assigned to *{valuer_name}*"
            elif actor_name:
                line = f"⚠️ `{ref}` — already assigned to *{actor_name}*, not *{valuer_name}* — skipped (not reassigned)"
            else:
                line = f"📋 `{ref}` — at VALUER_REPORT stage, no actor listed"
            return {"item": item, "keep": False, "line": line, "closed": None}

        return {"item": item, "keep": False, "line": f"❓ `{ref}` — unexpected node: `{node}`", "closed": None}

    except Exception as e:
        # Keep for retry on error
        item["last_error"] = str(e)[:120]
        logger.warning("DLV batch error for %s: %s", ref, e)
        return {"item": item, "keep": True, "line": None, "closed": None}


def _process_dlv_batch_items(tokens: AuthTokens) -> str:
    """
    Process the flat batch queue (list of {ref, valuer_name, valuer_uid, valuer_acct})
    across worker threads — each ref's search/detail-view/assign calls are
    independent I/O, same as the parallel fetch used elsewhere (e.g. Fetch Tasks).
    Refs not found in DLV are kept in the queue for the next 5-minute retry cycle.
    Returns a report string of completed items only, or "" if nothing was processed.
    """
    items = load_dlv_batch()
    if not items:
        return ""

    http_sess = build_session()
    assign_url = STAMP_DUTY_FIX_APPLICATION_URL
    auth_hdrs = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }

    completed_lines: List[str] = []
    remaining:       List[Dict] = []
    closed_items:    List[Dict] = []

    with ThreadPoolExecutor(max_workers=_DLV_BATCH_WORKERS) as pool:
        fut_map = {
            pool.submit(_process_dlv_batch_item, tokens, http_sess, assign_url, auth_hdrs, item): item
            for item in items
        }
        for fut in _futures_as_completed(fut_map):
            result = fut.result()
            if result["line"]:
                completed_lines.append(result["line"])
            if result["keep"]:
                remaining.append(result["item"])
            if result["closed"]:
                closed_items.append(result["closed"])

    # Save only refs that still need processing
    save_dlv_batch(remaining)
    for closed_record in closed_items:
        _append_dlv_closed(closed_record)

    if remaining:
        pending_refs = ", ".join(f"`{i['ref']}`" for i in remaining)
        completed_lines.append(f"\n⏳ Still pending (retry in 5 min): {pending_refs}")

    return "\n".join(completed_lines)


async def _dlv_batch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB repeating job: every 5 minutes, process the DLV batch queue if non-empty."""
    items = load_dlv_batch()
    if not items:
        return

    tokens = _any_valid_tokens()
    if not tokens:
        logger.warning("DLV batch job: no valid tokens — skipping cycle.")
        return

    before_count = len(items)
    report = await asyncio.to_thread(_process_dlv_batch_items, tokens)
    after_count  = len(load_dlv_batch())

    # Only notify if something was actually completed (queue shrank)
    if report and after_count < before_count:
        msg = f"📋 *DLV Batch (auto)*\n{report}"
        if len(msg) > 4000:
            msg = msg[:4000] + "\n…_(truncated)_"
        for chat_id in ALLOWED_IDS:
            try:
                await context.bot.send_message(chat_id, msg, parse_mode="Markdown")
            except Exception as e:
                logger.warning("DLV batch job notify error for %s: %s", chat_id, e)


# ──────────────────────────────────────────────────────────
# DLV Queue viewer
# ──────────────────────────────────────────────────────────

# Current DLV batch job interval in seconds (default 5 min); updated by user choice
_dlv_batch_interval: int = 300


def _dlv_queue_keyboard(current_interval: int) -> InlineKeyboardMarkup:
    options = [
        ("1 min",       60),
        ("2 min",       120),
        ("3 min",       180),
        ("Default (5 min)", 300),
    ]
    interval_row = [
        InlineKeyboardButton(
            f"{'✅ ' if current_interval == secs else ''}{label}",
            callback_data=f"dlvq:interval:{secs}",
        )
        for label, secs in options
    ]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Query Now", callback_data="dlvq:now")],
        interval_row,
        [InlineKeyboardButton("❌ Cancel",   callback_data="dlvq:cancel")],
    ])


async def cmd_dlv_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)

    items = load_dlv_batch()
    if not items:
        await update.message.reply_text(
            "✅ DLV Queue is empty — no pending assignments.",
            reply_markup=_main_menu(),
        )
        return

    lines = [f"🔍 *DLV Queue* — {len(items)} pending ref(s)\n"]
    for item in items:
        ref         = item.get("ref", "?")
        valuer_name = item.get("valuer_name", "?")
        last_error  = item.get("last_error", "")
        tag         = item.get("tag", "")
        line = f"• `{ref}` → *{valuer_name}*"
        if tag:
            line += f" 🏷 {tag}"
        if last_error:
            line += f"\n  ⚠️ _{last_error}_"
        lines.append(line)

    interval_label = {60: "1 min", 120: "2 min", 180: "3 min", 300: "5 min"}.get(
        _dlv_batch_interval, f"{_dlv_batch_interval}s"
    )
    lines.append(f"\n_Current check interval: {interval_label}_")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…_(truncated)_"

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=_dlv_queue_keyboard(_dlv_batch_interval),
    )


async def recv_dlv_queue_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _dlv_batch_interval
    query = update.callback_query
    await query.answer()
    data = query.data  # "dlvq:now" | "dlvq:interval:N" | "dlvq:cancel"

    if data == "dlvq:cancel":
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data == "dlvq:now":
        tokens = _any_valid_tokens()
        if not tokens:
            await query.edit_message_text(
                "⚠️ No valid tokens — please authenticate first.",
            )
            return
        items_before = load_dlv_batch()
        if not items_before:
            await query.edit_message_text("✅ Queue is empty — nothing to process.")
            return
        await query.edit_message_text(f"⏳ Processing {len(items_before)} ref(s), please wait…")
        report = await asyncio.to_thread(_process_dlv_batch_items, tokens)
        remaining = load_dlv_batch()
        msg = f"📋 *DLV Queue — Query Result*\n{report}" if report else "ℹ️ Nothing processed."
        if remaining:
            msg += f"\n\n⏳ *{len(remaining)} ref(s) still pending*"
        if len(msg) > 4000:
            msg = msg[:4000] + "\n…_(truncated)_"
        await query.edit_message_text(msg, parse_mode="Markdown")
        return

    if data.startswith("dlvq:interval:"):
        try:
            new_interval = int(data.split(":")[-1])
        except ValueError:
            return
        _dlv_batch_interval = new_interval

        # Reschedule the repeating job with the new interval
        job_queue = ctx.job_queue
        if job_queue:
            existing = job_queue.get_jobs_by_name("dlv_batch_job")
            for job in existing:
                job.schedule_removal()
            job_queue.run_repeating(
                _dlv_batch_job,
                interval=new_interval,
                first=new_interval,
                name="dlv_batch_job",
            )

        label = {60: "1 min", 120: "2 min", 180: "3 min", 300: "5 min"}.get(
            new_interval, f"{new_interval}s"
        )
        await query.edit_message_reply_markup(
            reply_markup=_dlv_queue_keyboard(_dlv_batch_interval),
        )
        await query.message.reply_text(
            f"✅ DLV check interval set to *{label}*.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )


# ──────────────────────────────────────────────────────────
# DLV Batch — conversation handlers
# ──────────────────────────────────────────────────────────

async def cmd_dlv_batch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    ctx.user_data["db_session"] = DBSession()
    await update.message.reply_text(
        "📥 *DLV Batch Assignment*\n\n"
        "Send your batch — one group per line:\n"
        "`REF1, REF2, REF3 : Valuer Name`\n\n"
        "*Example:*\n"
        "`REG/TSFR/5A0B3E1VLS, REG/TSFR/5A0B3E1VLQ : Byron`\n"
        "`REG/TSFR/XXXXXXXX : John Kamau`\n\n"
        "Multiple lines are processed together.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DB.INPUT_BATCH


def _db_format_batch_summary(sess: DBSession) -> str:
    """Build the confirm-step summary text — resolved/unresolved groups, each
    ref annotated with its tag (if any) from a prior Tag Tasks pass."""
    lines = ["📋 *Batch Summary — please confirm:*\n"]
    has_unresolved = False
    for g in sess.groups:
        refs_str = ", ".join(
            f"`{r}`" + (f" 🏷{sess.tag_by_ref[r]}" if sess.tag_by_ref.get(r) else "")
            for r in g["refs"]
        )
        if g["status"] == "resolved":
            assessors = {
                a for a in (
                    (_fetch_tasks_log_lookup(r) or {}).get("assessor", "") for r in g["refs"]
                ) if a
            }
            assessor_note = f"\n   Assessor: {', '.join(sorted(assessors))}" if assessors else ""
            lines.append(f"✅ {refs_str}\n   → *{g['valuer_name']}*{assessor_note}")
        else:
            lines.append(f"⚠️ {refs_str}\n   → _{g['valuer_name']}_ (NOT FOUND — will be skipped)")
            has_unresolved = True

    if has_unresolved:
        lines.append("\n_Unresolved valuers will be skipped._")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…_(truncated)_"
    return msg


def _db_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷 Tag Tasks", callback_data="db:tag")],
        [
            InlineKeyboardButton("✅ Confirm & Run", callback_data="db:confirm"),
            InlineKeyboardButton("❌ Cancel",        callback_data="db:cancel"),
        ],
    ])


def _db_tag_ref_keyboard(tag_refs: List[str], tag_by_ref: Dict[str, str]) -> InlineKeyboardMarkup:
    """One button per resolved ref, showing its current tag (if any); tap to (re)tag it."""
    rows = []
    for i, ref in enumerate(tag_refs):
        tag = tag_by_ref.get(ref, "")
        label = f"{ref} [🏷 {tag}]" if tag else f"{ref} — no tag"
        rows.append([InlineKeyboardButton(label, callback_data=f"db_tagref:{i}")])
    rows.append([
        InlineKeyboardButton("✅ Done Tagging", callback_data="db_tagref:done"),
        InlineKeyboardButton("❌ Cancel",       callback_data="db_tagref:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _db_tag_value_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(t, callback_data=f"db_tagval:{t}")] for t in DLV_TAGS]
    rows.append([InlineKeyboardButton("🚫 Clear tag", callback_data="db_tagval:clear")])
    rows.append([InlineKeyboardButton("⬅️ Back",      callback_data="db_tagval:back")])
    return InlineKeyboardMarkup(rows)


async def recv_db_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip()
    groups = _parse_batch_input(text)

    if not groups:
        await update.message.reply_text(
            "⚠️ Could not parse any entries. Use format:\n`REF1, REF2 : Valuer Name`",
            parse_mode="Markdown",
        )
        return DB.INPUT_BATCH

    await update.message.reply_text("🔍 Resolving valuers…")
    tokens  = _any_valid_tokens()
    sess    = _get_db_sess(ctx)
    sess.tag_by_ref = {}   # fresh batch submission — clear any tags from a prior one
    resolved = []

    for group in groups:
        name_raw = group["valuer_name_raw"]
        refs     = group["refs"]

        # 1. Check saved valuers
        saved = _resolve_valuer_from_saved(name_raw)
        if saved:
            resolved.append({
                "refs":        refs,
                "valuer_name": saved["name"],
                "valuer_uid":  saved["uid"],
                "valuer_acct": saved["account_number"],
                "status":      "resolved",
            })
            continue

        # 2. Search API
        if tokens:
            try:
                results = _search_valuer_api(name_raw, tokens)
                if results:
                    v    = results[0]
                    sd   = v.get("staff_details", {})
                    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
                    resolved.append({
                        "refs":        refs,
                        "valuer_name": name,
                        "valuer_uid":  str(v.get("id", "")),
                        "valuer_acct": str(v.get("account_number", "")),
                        "status":      "resolved",
                    })
                    continue
            except Exception as e:
                logger.warning("Valuer search error for %s: %s", name_raw, e)

        # 3. Unresolvable
        resolved.append({
            "refs":        refs,
            "valuer_name": name_raw,
            "valuer_uid":  "",
            "valuer_acct": "",
            "status":      "unresolved",
        })

    sess.groups = resolved

    await update.message.reply_text(
        _db_format_batch_summary(sess),
        parse_mode="Markdown",
        reply_markup=_db_confirm_keyboard(),
    )
    return DB.CONFIRM_BATCH


async def recv_db_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "db:cancel":
        await query.edit_message_text("❌ DLV Batch cancelled.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_db_sess(ctx)

    if query.data == "db:tag":
        sess.tag_refs = [ref for g in sess.groups if g["status"] == "resolved" for ref in g["refs"]]
        if not sess.tag_refs:
            await query.edit_message_text("⚠️ No resolved refs to tag.")
            await query.message.reply_text("Main menu:", reply_markup=_main_menu())
            return ConversationHandler.END
        await query.edit_message_text(
            "🏷 *Tag Tasks* — tap a ref to set its tag, then Done.",
            parse_mode="Markdown",
            reply_markup=_db_tag_ref_keyboard(sess.tag_refs, sess.tag_by_ref),
        )
        return DB.TAG_PICK_REF

    to_save = [g for g in sess.groups if g["status"] == "resolved"]

    if not to_save:
        await query.edit_message_text("⚠️ No resolved valuers — nothing to process.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    # Flatten groups to individual ref+valuer items for per-ref retry tracking
    existing     = load_dlv_batch()
    existing_refs = {item["ref"] for item in existing}
    new_items = []
    for g in to_save:
        for ref in g["refs"]:
            if ref not in existing_refs:
                new_item = {
                    "ref":         ref,
                    "valuer_name": g["valuer_name"],
                    "valuer_uid":  g["valuer_uid"],
                    "valuer_acct": g["valuer_acct"],
                    "queued_at":   datetime.now().isoformat(timespec="seconds"),
                    "tag":         sess.tag_by_ref.get(ref, ""),
                }
                cached = _fetch_tasks_log_lookup(ref)
                if cached and cached.get("assessor"):
                    new_item["assessor"] = cached["assessor"]
                new_items.append(new_item)
    flat_items = existing + new_items
    save_dlv_batch(flat_items)
    # The Fetch Tasks cache has now been folded into the batch item itself —
    # drop it so the cache doesn't keep growing with refs already queued.
    _fetch_tasks_log_remove([i["ref"] for i in new_items])

    tokens = _any_valid_tokens()
    if not tokens:
        await query.edit_message_text(
            f"✅ *{len(new_items)} new ref(s)* added to queue ({len(flat_items)} total).\n\n"
            "⚠️ No valid tokens — authenticate first.\n"
            "Batch saved; will retry on the next 5-minute cycle.",
            parse_mode="Markdown",
        )
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    await query.edit_message_text(
        f"✅ *{len(new_items)} new ref(s)* added to queue ({len(flat_items)} total).\n"
        "⏳ Processing in the background — I'll message you with the report when it's done.",
        parse_mode="Markdown",
    )
    await query.message.reply_text("Main menu:", reply_markup=_main_menu())
    asyncio.create_task(_run_dlv_batch_bg(ctx, query.message.chat_id, tokens))
    return ConversationHandler.END


async def recv_db_tag_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tag Tasks: handle a ref tap (open its tag picker), Done (back to the
    confirm summary), or Cancel (abort the whole batch)."""
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]
    sess = _get_db_sess(ctx)

    if data == "cancel":
        await query.edit_message_text("❌ DLV Batch cancelled.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    if data == "done":
        await query.edit_message_text(
            _db_format_batch_summary(sess),
            parse_mode="Markdown",
            reply_markup=_db_confirm_keyboard(),
        )
        return DB.CONFIRM_BATCH

    idx = int(data)
    if idx >= len(sess.tag_refs):
        return DB.TAG_PICK_REF
    sess.tag_ref_index = idx
    ref = sess.tag_refs[idx]
    await query.edit_message_text(
        f"🏷 Pick a tag for `{ref}`:",
        parse_mode="Markdown",
        reply_markup=_db_tag_value_keyboard(),
    )
    return DB.TAG_PICK_VALUE


async def recv_db_tag_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tag Tasks: apply/clear the picked tag for the currently-selected ref,
    then return to the ref list."""
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    sess  = _get_db_sess(ctx)
    ref   = sess.tag_refs[sess.tag_ref_index]

    if value == "clear":
        sess.tag_by_ref.pop(ref, None)
    elif value != "back":
        sess.tag_by_ref[ref] = value

    await query.edit_message_text(
        "🏷 *Tag Tasks* — tap a ref to set its tag, then Done.",
        parse_mode="Markdown",
        reply_markup=_db_tag_ref_keyboard(sess.tag_refs, sess.tag_by_ref),
    )
    return DB.TAG_PICK_REF


async def _run_dlv_batch_bg(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, tokens: AuthTokens) -> None:
    """Process the DLV batch queue off the event loop, then message the report back."""
    try:
        report = await asyncio.to_thread(_process_dlv_batch_items, tokens)
    except Exception as e:
        logger.error("DLV batch background processing failed: %s", e, exc_info=True)
        await ctx.bot.send_message(chat_id, f"❌ DLV Batch processing failed: `{e}`", parse_mode="Markdown")
        return
    msg = f"📋 *DLV Batch Report*\n{report}" if report else "ℹ️ Batch was already empty."
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…_(truncated)_"
    await ctx.bot.send_message(chat_id, msg, parse_mode="Markdown")


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the DLV Batch conversation, DLV Queue viewer, and the 5-minute
    repeating processing job into the given Application."""
    db_conv = ConversationHandler(
        entry_points=[
            CommandHandler("dlvbatch", cmd_dlv_batch),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_DLV_BATCH)}$"), cmd_dlv_batch),
        ],
        states={
            DB.INPUT_BATCH:    [MessageHandler(not_cancel, recv_db_input)],
            DB.CONFIRM_BATCH:  [CallbackQueryHandler(recv_db_confirm,   pattern=r"^db:")],
            DB.TAG_PICK_REF:   [CallbackQueryHandler(recv_db_tag_ref,   pattern=r"^db_tagref:")],
            DB.TAG_PICK_VALUE: [CallbackQueryHandler(recv_db_tag_value, pattern=r"^db_tagval:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(db_conv)
    app.job_queue.run_repeating(_dlv_batch_job, interval=300, first=300, name="dlv_batch_job")
    app.add_handler(CallbackQueryHandler(recv_dlv_queue_action, pattern=r"^dlvq:"))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DLV_QUEUE)}$"), cmd_dlv_queue))
