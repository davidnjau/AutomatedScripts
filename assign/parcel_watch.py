#!/usr/bin/env python3
"""
parcel_watch.py
==================
Parcel Watch — schedule a recurring check for whether a parcel number has
landed in the Stamp Duty list, the scheduled counterpart to
parcel_lookup.py's on-demand /parcelcheck (the same relationship Auto
Fetch has to Fetch Tasks). Reuses parcel_lookup.py's _pl_search_parcel to
find matching references.

Once a match is found, each matching reference is enriched with a full
Reference Lookup — lookup_reference.py's own _lu_fetch_detail +
_lu_format_result, the same primitives /lookup uses — rather than the
bare list-item block parcel_lookup.py's own immediate report uses. A
one-shot background notification (unlike an on-demand /parcelcheck,
which can return many matches and deliberately skips the extra detail
call per match to stay fast) can afford the extra per-match API round
trip, and the payoff — valuer name, node label, consideration amount —
makes the notification materially more useful than "it landed, here's
its status."

/parcelwatch ("⏳ Parcel Watch") walks through: parcel number -> check
interval (15 min / 30 min / 1 hour / 2 hours / 6 hours) -> delivery
(Telegram only, or Telegram + email). Each watch gets its own repeating
job (job_queue.run_repeating, name=f"pl_watch_job:{id}"), restored on
startup the same way auto_fetch.py restores its schedules.

A watch is one-shot: the moment _pl_search_parcel finds a match, it
notifies (Telegram always; email too if configured) and removes itself —
no "keep watching for new matches" mode, unlike Auto Fetch's continuous
polling. /parcelwatches lists this chat's active watches with an inline
cancel button for stopping one early.

List mode: entering more than one parcel number (one per line, or comma-
separated — see common._parse_list_input) queues one independent watch
per parcel, all sharing the interval/delivery choice made in that setup
run. Each watch still fires its own one-shot notification via
_pw_check_job exactly like a single-parcel watch — this only changes how
many watches one /parcelwatch conversation creates, not how a watch
behaves once queued. PWSession.parcels (plural) carries the list;
PWSession.parcel (singular) is unchanged for the single-parcel case.
Capped at common._LIST_INPUT_MAX_ITEMS parcels per setup run.

Call register(app) from bot.py's main() to wire this feature in.
"""

import asyncio
import json
import os
import re
import uuid
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

from common import (
    BTN_PARCEL_WATCH,
    CRED_LABELS,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _ensure_data_dir,
    _LIST_INPUT_MAX_ITEMS,
    _main_menu,
    _parse_list_input,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    logger,
    md_escape,
    not_cancel,
)
from email_service import _send_auto_fetch_email
from lookup_reference import _LU_CRED_DEFAULT, _lu_fetch_detail, _lu_format_result, _lu_md_escape
from parcel_lookup import _pl_search_parcel
from telegram_report import _send_chunked_report

SAVED_PARCEL_WATCHES_FILE = os.path.join(DATA_DIR, "saved_parcel_watches.json")

# Preset check-interval choices offered when creating a watch.
_PW_INTERVALS = [
    ("15 min", 15), ("30 min", 30), ("1 hour", 60), ("2 hours", 120), ("6 hours", 360),
]

# Seconds after bot startup before a restored watch's first check fires —
# staggers restored jobs past startup, mirroring auto_fetch.py's restore delay.
_PW_RESTORE_FIRST_RUN_DELAY = 30


# ──────────────────────────────────────────────────────────
# States — Parcel Watch setup conversation
# ──────────────────────────────────────────────────────────
class PW(Enum):
    PARCEL_INPUT = auto()   # enter the parcel number to watch
    INTERVAL     = auto()   # pick the check interval
    DELIVERY     = auto()   # telegram-only or telegram+email
    EMAIL_INPUT  = auto()   # enter email address (email delivery only)


@dataclass
class PWSession:
    parcel:           str = ""              # single-parcel mode: the parcel number
    parcels:          List[str] = field(default_factory=list)   # list mode: every parcel entered
    interval_minutes: int = 60


def _get_pw_sess(ctx: ContextTypes.DEFAULT_TYPE) -> PWSession:
    """Fetch (creating if absent) this chat's Parcel Watch setup session,
    holding the parcel + interval collected across conversation steps
    before the watch is finalized and persisted."""
    if "pw_session" not in ctx.user_data:
        ctx.user_data["pw_session"] = PWSession()
    return ctx.user_data["pw_session"]


# ──────────────────────────────────────────────────────────
# Persistence — saved_parcel_watches.json
# ──────────────────────────────────────────────────────────

def load_parcel_watches() -> List[Dict]:
    """Load every active parcel watch: [{id, chat_id, parcel,
    interval_minutes, email, created_at}, ...]."""
    try:
        with open(SAVED_PARCEL_WATCHES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_parcel_watches(watches: List[Dict]) -> None:
    """Persist the full active-watch list, overwriting the file."""
    _ensure_data_dir()
    _atomic_json_write(SAVED_PARCEL_WATCHES_FILE, watches, indent=2)


def get_parcel_watch(watch_id: str) -> Optional[Dict]:
    """Look up a single active watch by id, or None if it no longer exists."""
    return next((w for w in load_parcel_watches() if w.get("id") == watch_id), None)


def add_parcel_watch(chat_id: int, parcel: str, interval_minutes: int, email: str) -> str:
    """Assign a new id to the watch, append it to the saved list, and
    return the id."""
    watch_id = str(uuid.uuid4())[:8]
    watches  = load_parcel_watches()
    watches.append({
        "id":               watch_id,
        "chat_id":          chat_id,
        "parcel":           parcel,
        "interval_minutes": interval_minutes,
        "email":            email,
        "created_at":       datetime.now().isoformat(timespec="seconds"),
    })
    save_parcel_watches(watches)
    return watch_id


def remove_parcel_watch(watch_id: str) -> bool:
    """Delete a watch by id. Returns False if no watch had that id."""
    watches = load_parcel_watches()
    remaining = [w for w in watches if w.get("id") != watch_id]
    if len(remaining) == len(watches):
        return False
    save_parcel_watches(remaining)
    return True


# ──────────────────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────────────────

def _pw_interval_keyboard() -> InlineKeyboardMarkup:
    """Preset interval choices for a new watch, one row plus Cancel."""
    row = [InlineKeyboardButton(label, callback_data=f"pw_interval:{mins}") for label, mins in _PW_INTERVALS]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("❌ Cancel", callback_data="pw_interval:cancel")]])


def _pw_delivery_keyboard() -> InlineKeyboardMarkup:
    """Telegram-only vs Telegram+email choice for a new watch."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Telegram only", callback_data="pw_delivery:telegram"),
            InlineKeyboardButton("📧 Telegram + Email", callback_data="pw_delivery:email"),
        ],
    ])


def _pw_remove_keyboard(watches: List[Dict]) -> InlineKeyboardMarkup:
    """One 🗑 button per active watch, plus a Close button."""
    rows = [
        [InlineKeyboardButton(
            f"🗑 {w.get('parcel', '?')} (every {w.get('interval_minutes', '?')} min)",
            callback_data=f"pw_cancel:{w['id']}",
        )]
        for w in watches
    ]
    rows.append([InlineKeyboardButton("🛑 Close", callback_data="pw_cancel:close")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────
# Setup conversation
# ──────────────────────────────────────────────────────────

async def cmd_parcel_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point — prompts for the parcel number to watch.
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        "⏳ *Parcel Watch*\n\n"
        "Queue a recurring check for a parcel number — you'll be notified "
        "the moment it lands in the Stamp Duty list, then the watch stops.\n\n"
        "Enter the *parcel number* to watch\n"
        f"_or paste up to {_LIST_INPUT_MAX_ITEMS}, one per line or comma-separated, "
        "to queue a watch for each._",
        parse_mode="Markdown",
    )
    return PW.PARCEL_INPUT


async def recv_pw_parcel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # One parcel -> stash it on sess.parcel (unchanged single-parcel
    # behavior). More than one parcel (list mode, one per line or comma-
    # separated) -> stash the whole list on sess.parcels instead; each
    # still becomes its own independent watch in _pw_finalize. Either way,
    # confirm a cached login exists before asking how often to check.
    if not allowed(update): return await deny(update)

    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text("Please enter a parcel number.")
        return PW.PARCEL_INPUT

    parcels = _parse_list_input(raw)
    if len(parcels) > _LIST_INPUT_MAX_ITEMS:
        await update.message.reply_text(
            f"❌ Too many parcels ({len(parcels)}) — max {_LIST_INPUT_MAX_ITEMS} per list.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    if not get_valid_tokens(_LU_CRED_DEFAULT):
        await update.message.reply_text(
            f"❌ No valid cached tokens for *{CRED_LABELS.get(_LU_CRED_DEFAULT, _LU_CRED_DEFAULT)}*. "
            "Use *🔑 Refresh Auth* first — the background job needs a cached login to check.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    sess = _get_pw_sess(ctx)
    if len(parcels) > 1:
        sess.parcel, sess.parcels = "", parcels
    else:
        sess.parcel, sess.parcels = parcels[0], []

    await update.message.reply_text("⏱ How often should it check?", reply_markup=_pw_interval_keyboard())
    return PW.INTERVAL


async def recv_pw_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Store the chosen interval, then ask for delivery preference.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_pw_sess(ctx)
    sess.interval_minutes = int(choice)

    await query.edit_message_text(
        "📬 How should the alert be delivered when it's found?",
        reply_markup=_pw_delivery_keyboard(),
    )
    return PW.DELIVERY


async def recv_pw_delivery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Telegram-only -> finalize immediately. Telegram+email -> ask for the
    # address next.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":")[1]

    if mode == "email":
        await query.edit_message_text("📧 Enter the email address to also notify:", parse_mode="Markdown")
        return PW.EMAIL_INPUT

    watch_ids = _pw_finalize(query.message.chat_id, ctx, "")
    await query.edit_message_text(_pw_queued_text(watch_ids))
    await query.message.reply_text("Main menu.", reply_markup=_main_menu())
    return ConversationHandler.END


async def recv_pw_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Validate the email, finalize the watch(es) with it attached.
    if not allowed(update): return await deny(update)
    email = (update.message.text or "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        await update.message.reply_text("❌ Invalid email. Enter a valid address.", parse_mode="Markdown")
        return PW.EMAIL_INPUT

    watch_ids = _pw_finalize(update.effective_chat.id, ctx, email)
    await update.message.reply_text(_pw_queued_text(watch_ids), reply_markup=_main_menu())
    return ConversationHandler.END


def _pw_finalize(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, email: str) -> List[str]:
    """Persist the watch(es) and schedule their repeating job(s) — shared
    by both the Telegram-only and Telegram+email delivery paths. List
    mode (sess.parcels has entries) creates one independent watch per
    parcel, all sharing this interval/email; single-parcel mode (the
    default, sess.parcels empty) creates exactly one, matching prior
    behavior exactly. first=interval (not 0) so a brand-new watch doesn't
    fire an immediate check before the user has even seen the
    confirmation message. Returns every watch id created."""
    sess = _get_pw_sess(ctx)
    parcels = sess.parcels or [sess.parcel]
    watch_ids = []
    for parcel in parcels:
        watch_id = add_parcel_watch(chat_id, parcel, sess.interval_minutes, email)
        ctx.job_queue.run_repeating(
            _pw_check_job,
            interval=sess.interval_minutes * 60,
            first=sess.interval_minutes * 60,
            name=f"pl_watch_job:{watch_id}",
            data=watch_id,
        )
        watch_ids.append(watch_id)
    return watch_ids


def _pw_queued_text(watch_ids: List[str]) -> str:
    """Confirmation text after finalizing — pluralized for list mode."""
    return "✅ Watch queued." if len(watch_ids) == 1 else f"✅ {len(watch_ids)} watches queued."


# ──────────────────────────────────────────────────────────
# Background job
# ──────────────────────────────────────────────────────────

def _pw_fetch_enriched(tokens, matches: List[Dict]):
    """For each matching list-item from _pl_search_parcel, fetch its
    detail-view via lookup_reference.py's own _lu_fetch_detail — the same
    primitive /lookup uses — and pair it with the item and its reference
    number. Synchronous (run via asyncio.to_thread from _pw_check_job, one
    call covering the whole loop) since it's the same blocking-HTTP-in-a-
    thread pattern _pl_search_parcel already uses. _lu_fetch_detail
    catches its own exceptions and returns None on failure, so a single
    failed detail call degrades that one match to item-only fields rather
    than failing the whole notification."""
    return [(item.get("reference_number", "—"), item, _lu_fetch_detail(tokens, item.get("id"))) for item in matches]


async def _pw_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job for one watch: search the parcel, and if found,
    notify (Telegram always, email too if configured) then remove the
    watch — one-shot, unlike Auto Fetch's continuous polling. Re-reads the
    watch fresh each cycle (rather than closing over it) so a manual
    cancel (recv_pw_cancel) since the job last fired is honored here
    instead of silently racing a stale in-memory copy."""
    watch_id = context.job.data
    watch = get_parcel_watch(watch_id)
    if not watch:
        context.job.schedule_removal()
        return

    tokens = get_valid_tokens(_LU_CRED_DEFAULT)
    if not tokens:
        logger.warning(
            "Parcel Watch job %s: no valid %s tokens cached — skipping cycle.",
            watch_id, _LU_CRED_DEFAULT,
        )
        return

    matches = await asyncio.to_thread(_pl_search_parcel, tokens, watch["parcel"])
    if not matches:
        return

    enriched = await asyncio.to_thread(_pw_fetch_enriched, tokens, matches)

    header = f"🔔 *Parcel Watch* — `{_lu_md_escape(watch['parcel'])}` found on {len(enriched)} application(s)"
    lines = [header] + [_lu_format_result(ref, item, detail, markdown=True) for ref, item, detail in enriched]

    async def _send(text, reply_markup):
        await context.bot.send_message(watch["chat_id"], text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n\n")

    if watch.get("email"):
        try:
            plain_header = f"Parcel Watch — {watch['parcel']} found on {len(enriched)} application(s)"
            body = "\n\n".join(
                [plain_header] + [_lu_format_result(ref, item, detail, markdown=False) for ref, item, detail in enriched],
            )
            subject = f"Ardhisasa Parcel Watch — {watch['parcel']} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await asyncio.to_thread(_send_auto_fetch_email, watch["email"], subject, body)
        except Exception as exc:
            logger.warning("Parcel Watch job %s: email failed: %s", watch_id, exc)

    remove_parcel_watch(watch_id)
    context.job.schedule_removal()


# ──────────────────────────────────────────────────────────
# /parcelwatches — list + cancel active watches
# ──────────────────────────────────────────────────────────

async def cmd_parcel_watches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Lists this chat's active watches with an inline cancel button each.
    if not allowed(update): return await deny(update)
    watches = [w for w in load_parcel_watches() if w.get("chat_id") == update.effective_chat.id]
    if not watches:
        await update.message.reply_text("ℹ️ No active parcel watches.", reply_markup=_main_menu())
        return

    lines = [f"⏳ *Parcel Watches* — {len(watches)} active\n"]
    lines += [
        f"{i}. `{_lu_md_escape(w.get('parcel', '?'))}` — every {w.get('interval_minutes', '?')} min"
        + (f" — 📧 {md_escape(w['email'])}" if w.get("email") else "")
        for i, w in enumerate(watches, 1)
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=_pw_remove_keyboard(watches),
    )


async def recv_pw_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Cancel one watch's job + storage, or just close the picker.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    watch_id = query.data.split(":", 1)[1]

    if watch_id == "close":
        await query.edit_message_reply_markup(reply_markup=None)
        return

    for job in ctx.job_queue.get_jobs_by_name(f"pl_watch_job:{watch_id}"):
        job.schedule_removal()
    remove_parcel_watch(watch_id)
    await query.edit_message_text("🗑 Watch cancelled.")


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Parcel Watch setup conversation + /parcelwatches viewer
    into the given Application, and restore one repeating job per saved
    watch on startup (mirrors auto_fetch.py's schedule restore)."""
    pw_conv = ConversationHandler(
        entry_points=[
            CommandHandler("parcelwatch", cmd_parcel_watch),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_PARCEL_WATCH)}$"), cmd_parcel_watch),
        ],
        states={
            PW.PARCEL_INPUT: [MessageHandler(not_cancel, recv_pw_parcel)],
            PW.INTERVAL:     [CallbackQueryHandler(recv_pw_interval, pattern=r"^pw_interval:")],
            PW.DELIVERY:     [CallbackQueryHandler(recv_pw_delivery, pattern=r"^pw_delivery:")],
            PW.EMAIL_INPUT:  [MessageHandler(not_cancel, recv_pw_email)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(pw_conv)
    app.add_handler(CommandHandler("parcelwatches", cmd_parcel_watches))
    app.add_handler(CallbackQueryHandler(recv_pw_cancel, pattern=r"^pw_cancel:"))

    watches = load_parcel_watches()
    for w in watches:
        watch_id = w.get("id", "")
        if not watch_id:
            continue   # shouldn't happen, but don't crash startup over a malformed entry
        interval_secs = w.get("interval_minutes", 60) * 60
        app.job_queue.run_repeating(
            _pw_check_job,
            interval=interval_secs,
            first=_PW_RESTORE_FIRST_RUN_DELAY,
            name=f"pl_watch_job:{watch_id}",
            data=watch_id,
        )
    if watches:
        logger.info(
            "Parcel Watch: restored %d watch(es) (first run in %ds each)",
            len(watches), _PW_RESTORE_FIRST_RUN_DELAY,
        )
