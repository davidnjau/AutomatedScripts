#!/usr/bin/env python3
"""
quick_assign.py
===============
Quick Assign (⚡ SD Assignment / ⚡ LRent Assignment) — the fast, low-input
workflow: pick the workflow, send a photo, type a valuer name, and the bot
finishes everything in the background.

Flow: ⚡ SD/LRent Assignment → send a photo → type a name. The name is
fuzzy-matched against the saved valuers (which have distinct names), so a
single close match is auto-picked and the user is never asked to confirm;
they're only asked when there's **no** match ("give a fuller name") or
**several** matches ("which one?"). Then a background job (job_queue.run_once,
same pattern as sms_sender) does the rest with no further input:
  1. OCR the photo (ocr_extract_refs — GLM-OCR/Claude/Tesseract).
  2. For each reference, **try a direct New Assignment first**
     (STAMP_DUTY_FIX_APPLICATION_URL / LRD_FIX_PARCEL_NUMBER_URL); if that
     doesn't qualify (the ref isn't at the DLV valuer stage yet, so the POST
     fails), **queue it** onto the DLV Batch queue instead — the existing
     5-minute job assigns it once it arrives.
  3. Record each one in the consolidated store tagged channel="quick_assign"
     with its mode (assigned / queued), workflow, node and status, so it can
     be reviewed later under 📑 View References.

Known MVP limit: the DLV Batch processing job only assigns Stamp Duty refs, so
a queued Land Rent ref will sit in the queue until LRD queue-processing is
added (a fast-follow). Direct Land Rent assignment works today.

Call register(app) from bot.py's main() to wire this feature in.
"""
import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional

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

from ardhisasa_auth import auth_headers, build_session
from common import (
    BTN_QA_LRENT,
    BTN_QA_SD,
    BTN_VIEW_REFS,
    _any_valid_tokens,
    _CANCEL_FILTER,
    _main_menu_for,
    _safe_err,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    load_saved_valuers,
    logger,
    md_escape,
    not_cancel,
    persist_assignment,
    persist_valuer,
)
from dlv_core import load_dlv_batch, load_records_where, save_dlv_batch
from endpoints import LRD_FIX_PARCEL_NUMBER_URL, STAMP_DUTY_FIX_APPLICATION_URL
from new_assignment import ocr_extract_refs
from task_block import format_labeled_block
from telegram_report import _send_chunked_report

_WORKFLOW_LABEL = {"stamp_duty": "📋 SD", "land_rent": "🏘 LRent"}


# ──────────────────────────────────────────────────────────
# Conversation state + session
# ──────────────────────────────────────────────────────────
class QA(Enum):
    PHOTO       = auto()   # waiting for the reference photo
    NAME        = auto()   # waiting for the valuer name
    PICK_VALUER = auto()   # only when the typed name matched 2+ saved valuers


@dataclass
class QASession:
    workflow:    str = "stamp_duty"       # "stamp_duty" | "land_rent"
    photo_bytes: Optional[bytes] = None
    matches:     List[Dict] = field(default_factory=list)   # candidate valuers when ambiguous


def _get_qa_sess(ctx: ContextTypes.DEFAULT_TYPE) -> QASession:
    # Lazily create and cache this chat's Quick Assign session.
    if "qa_session" not in ctx.user_data:
        ctx.user_data["qa_session"] = QASession()
    return ctx.user_data["qa_session"]


# ──────────────────────────────────────────────────────────
# Valuer name matching
# ──────────────────────────────────────────────────────────

def _qa_match_valuers(name: str) -> List[Dict]:
    """Saved valuers whose name contains the typed name (case-insensitive).
    Saved valuers have distinct names, so this is usually 0 or 1; 2+ only when
    the typed fragment is genuinely ambiguous."""
    nl = name.strip().lower()
    if not nl:
        return []
    return [v for v in load_saved_valuers() if nl in (v.get("name") or "").lower()]


def _qa_valuer_keyboard(matches: List[Dict]) -> InlineKeyboardMarkup:
    # One button per matching valuer (index-based callback), plus cancel.
    rows = [[InlineKeyboardButton(v["name"], callback_data=f"qa_pick:{i}")] for i, v in enumerate(matches)]
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data="qa_pick:cancel")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────
# Entry points
# ──────────────────────────────────────────────────────────

async def _qa_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE, workflow: str) -> int:
    # Shared entry: set the workflow and ask for the photo.
    if not allowed(update): return await deny(update)
    ctx.user_data["qa_session"] = QASession(workflow=workflow)
    await update.message.reply_text(
        f"⚡ *Quick {_WORKFLOW_LABEL[workflow]} Assignment* — the fast path: send a photo of the "
        "reference(s), then give me a valuer name, and I'll read it and assign (or queue) in the "
        "background — no further steps.\n\n*Send the photo now:*",
        parse_mode="Markdown",
    )
    return QA.PHOTO


async def cmd_qa_sd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # ⚡ SD Assignment entry — Stamp Duty quick assign.
    return await _qa_start(update, ctx, "stamp_duty")


async def cmd_qa_lrent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # ⚡ LRent Assignment entry — Land Rent quick assign.
    return await _qa_start(update, ctx, "land_rent")


async def recv_qa_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Store the photo bytes, then ask for the valuer name.
    if not update.message.photo:
        await update.message.reply_text("⚠️ Please send a *photo* of the reference(s).", parse_mode="Markdown")
        return QA.PHOTO
    tgfile = await update.message.photo[-1].get_file()
    _get_qa_sess(ctx).photo_bytes = bytes(await tgfile.download_as_bytearray())
    await update.message.reply_text(
        "📷 Got it. *Who should I assign to?* Type a valuer name (e.g. `Godwill`):",
        parse_mode="Markdown",
    )
    return QA.NAME


async def recv_qa_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Resolve the typed name to a saved valuer. 0 → re-ask; 1 → go; 2+ → pick.
    sess = _get_qa_sess(ctx)
    name = (update.message.text or "").strip()
    matches = _qa_match_valuers(name)

    if not matches:
        await update.message.reply_text(
            f"🤔 No saved valuer matches *{md_escape(name)}*. Type a fuller name:",
            parse_mode="Markdown",
        )
        return QA.NAME

    if len(matches) == 1:
        return await _qa_launch(update, ctx, matches[0])

    sess.matches = matches
    await update.message.reply_text(
        f"👥 *{len(matches)}* valuers match *{md_escape(name)}* — which one?",
        parse_mode="Markdown",
        reply_markup=_qa_valuer_keyboard(matches),
    )
    return QA.PICK_VALUER


async def recv_qa_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle the valuer disambiguation tap, then launch the background job.
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]
    if data == "cancel":
        await query.edit_message_text("🛑 Cancelled.")
        await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END
    sess = _get_qa_sess(ctx)
    idx = int(data)
    if idx >= len(sess.matches):
        return QA.PICK_VALUER
    await query.edit_message_reply_markup(reply_markup=None)
    return await _qa_launch(query, ctx, sess.matches[idx])


async def _qa_launch(update_or_query, ctx: ContextTypes.DEFAULT_TYPE, valuer: Dict) -> int:
    # Queue the background job and end the conversation — nothing else is asked.
    sess = _get_qa_sess(ctx)
    msg = update_or_query.message
    user_id = (update_or_query.from_user.id if hasattr(update_or_query, "from_user")
               else update_or_query.effective_user.id)
    ctx.application.job_queue.run_once(
        _qa_process_job, when=0,
        data={
            "chat_id":     msg.chat_id,
            "user_id":     user_id,
            "workflow":    sess.workflow,
            "photo_bytes": sess.photo_bytes,
            "valuer":      valuer,
        },
        name=f"quick_assign:{msg.chat_id}",
    )
    await msg.reply_text(
        f"⚡ Working on it in the background — reading the photo and assigning to "
        f"*{md_escape(valuer['name'])}*. I'll report back here when it's done.",
        parse_mode="Markdown",
        reply_markup=_main_menu_for(user_id),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Background processing
# ──────────────────────────────────────────────────────────

def _qa_assign_or_queue(refs: List[str], valuer: Dict, workflow: str, user_id: int) -> Dict[str, List[str]]:
    """For each ref: try a direct assignment; if it fails (ref not yet at the
    DLV valuer stage), queue it onto the DLV Batch queue. Records every
    outcome in the consolidated store tagged channel='quick_assign'. Blocking
    (HTTP) — call via asyncio.to_thread. Returns {assigned, queued, failed}."""
    tokens = _any_valid_tokens()
    if not tokens:
        return {"no_token": refs, "assigned": [], "queued": [], "failed": []}

    headers  = auth_headers(tokens)
    http     = build_session()
    url      = LRD_FIX_PARCEL_NUMBER_URL if workflow == "land_rent" else STAMP_DUTY_FIX_APPLICATION_URL
    uid      = valuer["uid"]
    name     = valuer["name"]
    acct     = valuer.get("account_number", "")
    now      = datetime.now().isoformat(timespec="seconds")
    out: Dict[str, List[str]] = {"assigned": [], "queued": [], "failed": [], "no_token": []}

    for ref in refs:
        try:
            if workflow == "land_rent":
                body = {"reference_number": ref, "valuation_officer": uid}
            else:
                body = {"reference_number": ref, "valuation_officer": uid, "node": "VALUATION_STAMP_DUTY_VALUER_REPORT"}
            r = http.post(url, headers=headers, json=body, timeout=30)
            r.raise_for_status()
            # Qualified — a direct New Assignment.
            persist_assignment(ref, name, uid, extra={
                "valuer_acct": acct, "tag": "Direct", "workflow": workflow,
                "channel": "quick_assign", "qa_mode": "assigned", "added_by": user_id,
            })
            out["assigned"].append(ref)
        except Exception as e:
            logger.info("Quick Assign: %s not directly assignable (%s) — queueing", ref, _safe_err(e))
            _qa_enqueue(ref, valuer, workflow, user_id, now)
            out["queued"].append(ref)
    return out


def _qa_enqueue(ref: str, valuer: Dict, workflow: str, user_id: int, now: str) -> None:
    # Add a ref to the DLV Batch queue (deduped) so the 5-min job assigns it on
    # arrival, tagged channel='quick_assign' so View References can find it.
    existing = load_dlv_batch()
    if any(i.get("ref") == ref for i in existing):
        return
    existing.append({
        "ref":         ref,
        "valuer_name": valuer["name"],
        "valuer_uid":  valuer["uid"],
        "valuer_acct": valuer.get("account_number", ""),
        "queued_at":   now,
        "tag":         "Direct",
        "workflow":    workflow,
        "channel":     "quick_assign",
        "qa_mode":     "queued",
        "added_by":    user_id,
    })
    save_dlv_batch(existing)


async def _qa_process_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # The whole background pipeline: OCR → assign/queue per ref → report.
    data     = ctx.job.data
    chat_id  = data["chat_id"]
    workflow = data["workflow"]
    valuer   = data["valuer"]

    try:
        refs, source = await ocr_extract_refs(data["photo_bytes"])
        logger.info("Quick Assign OCR (%s): %d ref(s)", source, len(refs))
    except Exception as e:
        logger.error("Quick Assign OCR failed: %s", e)
        refs = []

    if not refs:
        await ctx.bot.send_message(
            chat_id,
            "⚠️ *Quick Assign* — couldn't read any reference number from the photo. "
            "Try a clearer shot of the Reference Number column.",
            parse_mode="Markdown", reply_markup=_main_menu_for(chat_id),
        )
        return

    result = await asyncio.to_thread(_qa_assign_or_queue, refs, valuer, workflow, data["user_id"])

    if result.get("no_token"):
        await ctx.bot.send_message(
            chat_id,
            "⚠️ *Quick Assign* — no valid session token. Run *🔑 Refresh Auth* first, then retry.",
            parse_mode="Markdown", reply_markup=_main_menu_for(chat_id),
        )
        return

    if result["assigned"]:
        persist_valuer(valuer["name"], valuer["uid"], valuer.get("account_number", ""))

    lines = [
        f"⚡ *Quick {_WORKFLOW_LABEL[workflow]} Assign — done* → *{md_escape(valuer['name'])}*",
        f"✅ Assigned now: {len(result['assigned'])}   📥 Queued: {len(result['queued'])}",
        "",
    ]
    for ref in result["assigned"]:
        lines.append(f"✅ `{md_escape(ref)}` — assigned")
    for ref in result["queued"]:
        lines.append(f"📥 `{md_escape(ref)}` — queued (will assign when it reaches DLV)")

    async def _send(text, reply_markup):
        await ctx.bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n")
    await ctx.bot.send_message(chat_id, "📑 Saved under *View References*.",
                               parse_mode="Markdown", reply_markup=_main_menu_for(chat_id))


# ──────────────────────────────────────────────────────────
# View References
# ──────────────────────────────────────────────────────────

def _qa_format_record(i: int, rec: Dict) -> str:
    # One labeled block per Quick Assign record for the View References report.
    mode = "📋 New Assignment" if rec.get("status") == "assigned" else "📥 Queued"
    when = rec.get("assigned_at") or rec.get("queued_at") or "—"
    fields = [
        ("👤 Valuer", rec.get("valuer_name") or "—"),
        ("🔀 Mode", f"{mode} ({_WORKFLOW_LABEL.get(rec.get('workflow'), rec.get('workflow') or '—')})"),
        ("🕒 When", when),
        ("🔖 Node", rec.get("node") or "—"),
        ("📊 Status", rec.get("application_status") or rec.get("status") or "—"),
    ]
    return format_labeled_block(i, rec.get("ref", "?"), fields)


async def cmd_view_references(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # 📑 View References — every ref processed via Quick Assign, newest first,
    # with its valuer, mode (assigned vs queued), node and status for review.
    if not allowed(update): return await deny(update)
    recs = load_records_where(channel="quick_assign")
    if not recs:
        await update.message.reply_text(
            "📑 *View References* — nothing processed via Quick Assign yet.",
            parse_mode="Markdown", reply_markup=_main_menu_for(update.effective_user.id),
        )
        return
    recs.sort(key=lambda r: (r.get("assigned_at") or r.get("queued_at") or ""), reverse=True)
    lines = [f"📑 *View References* — {len(recs)} record(s)\n"]
    lines += [_qa_format_record(i, r) for i, r in enumerate(recs, 1)]

    async def _send(text, reply_markup):
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n\n",
                               reply_markup=_main_menu_for(update.effective_user.id))


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Quick Assign conversation + View References into the app."""
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("sdassign",    cmd_qa_sd),
            CommandHandler("lrentassign", cmd_qa_lrent),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_QA_SD)}$"),    cmd_qa_sd),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_QA_LRENT)}$"), cmd_qa_lrent),
        ],
        states={
            QA.PHOTO:       [MessageHandler(filters.PHOTO, recv_qa_photo)],
            QA.NAME:        [MessageHandler(not_cancel, recv_qa_name)],
            QA.PICK_VALUER: [CallbackQueryHandler(recv_qa_pick, pattern=r"^qa_pick:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("viewrefs", cmd_view_references))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_VIEW_REFS)}$"), cmd_view_references))
