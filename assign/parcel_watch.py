#!/usr/bin/env python3
"""
parcel_watch.py
==================
Parcel Watch — schedule a recurring check for whether a parcel number has
landed in the Stamp Duty list, the scheduled counterpart to
parcel_lookup.py's on-demand /parcelcheck (the same relationship Auto
Fetch has to Fetch Tasks). Reuses parcel_lookup.py's _pl_search_parcel to
find matching references.

_pl_search_parcel covers both the DLV/Valuer stage and the pre-DLV
assessor/HQ collector stage (parcel_lookup._PL_CRED_ASSESSOR), so a watch
also fires on a parcel that's only landed at the HQ/collector stage, not
yet pushed into DLV's queue.

Once a match is found, each matching reference is enriched with a full
Reference Lookup. A DLV/Valuer-stage match uses lookup_reference.py's own
_lu_fetch_detail + _lu_format_result, the same primitives /lookup uses;
an assessor/HQ-stage match uses _lu_fetch_detail_county +
_lu_format_county_result instead (see _pw_fetch_enriched) — rather than
the bare list-item block parcel_lookup.py's own immediate report uses. A
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

After interval/delivery (and email, if chosen), setup also asks which
valuer should be assigned once the parcel is found (saved valuer or
search new — a local, self-contained copy of bulk_field_update.py's
valuer-pick pattern, reusing whichever of _LU_CRED_DEFAULT/
_PL_CRED_ASSESSOR cached tokens recv_pw_parcel already confirmed are
valid; no OTP step), then whether that assignment should be held (guarded
against takeover). Both apply to every watch created in that setup run
(list mode shares them, same as interval/email already do).

When a match is found, _pw_check_job queues each matching reference into
DLV Batch's own queue (dlv_core.save_dlv_batch, tag "Parcel Watch") rather
than assigning directly — DLV Batch's background job retries until the
ref is actually at an assignable stage, regardless of whether it was
still at the assessor stage or already at DLV when Parcel Watch found it.
If the watch asked for a hold, the queued item carries auto_hold=True;
dlv_batch.py's _db_maybe_auto_hold enrolls it into Hold Tasks' guard queue
once DLV Batch actually confirms the assignment (Hold Tasks can't guard
something that isn't assigned yet, so this can't happen at the moment
Parcel Watch finds the match). A watch created before this feature simply
has no "valuer" key — _pw_check_job treats that as "notify only," so
existing watches keep their prior behavior unchanged.

Single-parcel mode (not list mode) also asks, right after the parcel
number, whether an existing reference on that parcel should be ignored
(PWSession.exclude_ref) — the scenario this exists for: a reference was
raised incorrectly and is still sitting on a valuer's desk, about to be
cancelled, and a new reference for the same parcel is expected once it
is. Without this, a watch set up while the wrong ref still exists would
fire immediately on it, since _pl_search_parcel matches by parcel number
alone. With exclude_ref set, _pw_check_job filters that exact reference
out of the search results before deciding anything was found, so the
watch keeps waiting through "wrong ref cancelled, nothing matches yet"
and only fires once a genuinely different reference appears for the same
parcel. List mode skips this step entirely (no prompt, no exclusion) —
multiple parcels sharing one exclude_ref wouldn't make sense, since each
would have a different wrong reference, if any.

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

from ardhisasa_auth import build_session
from common import (
    BTN_PARCEL_WATCH,
    CRED_LABELS,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _ensure_data_dir,
    _LIST_INPUT_MAX_ITEMS,
    _main_menu_for,
    _parse_list_input,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    load_saved_valuers,
    logger,
    md_escape,
    not_cancel,
)
from dlv_core import save_dlv_batch
from email_service import _send_auto_fetch_email
from endpoints import ACCOUNTS_LIST_URL
from lookup_reference import (
    _LU_CRED_DEFAULT,
    _lu_fetch_detail,
    _lu_fetch_detail_county,
    _lu_format_county_result,
    _lu_format_result,
    _lu_md_escape,
)
from parcel_lookup import _PL_CRED_ASSESSOR, _pl_search_parcel
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
    PARCEL_INPUT  = auto()   # enter the parcel number to watch
    EXCLUDE_REF   = auto()   # (single-parcel only) optional ref number to ignore
    INTERVAL      = auto()   # pick the check interval
    DELIVERY      = auto()   # telegram-only or telegram+email
    EMAIL_INPUT   = auto()   # enter email address (email delivery only)
    VALUER_SOURCE = auto()   # saved valuer vs search new
    VALUER_NAME   = auto()   # enter name when searching new
    SELECT_VALUER = auto()   # pick from live search results
    HOLD_CHOICE   = auto()   # hold the assignment once made, or not


@dataclass
class PWSession:
    parcel:           str = ""              # single-parcel mode: the parcel number
    parcels:          List[str] = field(default_factory=list)   # list mode: every parcel entered
    exclude_ref:      str = ""              # single-parcel mode only: a known ref to ignore on this parcel
    interval_minutes: int = 60
    email:            str = ""              # "" for Telegram-only delivery
    valuer_name:      str = ""              # search text (search-new path)
    valuers:          List[Dict] = field(default_factory=list)   # live search results, for SELECT_VALUER's index lookup
    valuer:           Optional[Dict] = None # {"name", "uid", "account_number"} — saved or searched, unified
    hold:             bool = False


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
    interval_minutes, email, valuer, hold, exclude_ref, created_at}, ...].
    valuer/hold/exclude_ref are absent on watches created before the
    feature that introduced each existed."""
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


def add_parcel_watch(
    chat_id: int, parcel: str, interval_minutes: int, email: str,
    valuer: Optional[Dict] = None, hold: bool = False, exclude_ref: str = "",
) -> str:
    """Assign a new id to the watch, append it to the saved list, and
    return the id. valuer ({"name","uid","account_number"}, or None), hold,
    and exclude_ref are all optional so a legacy caller/watch predating
    each feature still works — _pw_check_job treats a missing valuer as
    "notify only" and a missing/empty exclude_ref as "match anything"."""
    watch_id = str(uuid.uuid4())[:8]
    watches  = load_parcel_watches()
    watches.append({
        "id":               watch_id,
        "chat_id":          chat_id,
        "parcel":           parcel,
        "interval_minutes": interval_minutes,
        "email":            email,
        "valuer":           valuer,
        "hold":             hold,
        "exclude_ref":      exclude_ref,
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

def _pw_exclude_ref_keyboard() -> InlineKeyboardMarkup:
    """Skip entering a reference number to ignore on this parcel."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Skip", callback_data="pw_exclref:skip")]])


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


def _pw_hold_keyboard() -> InlineKeyboardMarkup:
    """Hold the assignment once made (guard against takeover), or not."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✋ Hold it",            callback_data="pw_hold:yes"),
        InlineKeyboardButton("➡️ No, just assign",    callback_data="pw_hold:no"),
    ]])


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
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    if not get_valid_tokens(_LU_CRED_DEFAULT) and not get_valid_tokens(_PL_CRED_ASSESSOR):
        await update.message.reply_text(
            f"❌ No valid cached tokens for *{CRED_LABELS.get(_LU_CRED_DEFAULT, _LU_CRED_DEFAULT)}* "
            f"or *{CRED_LABELS.get(_PL_CRED_ASSESSOR, _PL_CRED_ASSESSOR)}*. "
            "Use *🔑 Refresh Auth* first — the background job needs at least one cached login to check.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    sess = _get_pw_sess(ctx)
    if len(parcels) > 1:
        sess.parcel, sess.parcels = "", parcels
        await update.message.reply_text("⏱ How often should it check?", reply_markup=_pw_interval_keyboard())
        return PW.INTERVAL

    sess.parcel, sess.parcels = parcels[0], []
    await update.message.reply_text(
        "🔁 Is there an *existing* reference number on this parcel that should be "
        "*ignored* — e.g. it was raised incorrectly and will be cancelled? If so, "
        "the watch will only fire once a *different* reference appears for this parcel.\n\n"
        "Enter it, or tap Skip for a fresh watch.",
        parse_mode="Markdown",
        reply_markup=_pw_exclude_ref_keyboard(),
    )
    return PW.EXCLUDE_REF


async def recv_pw_exclude_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Typed a reference number to ignore on this parcel.
    if not allowed(update): return await deny(update)
    sess = _get_pw_sess(ctx)
    sess.exclude_ref = (update.message.text or "").strip().upper()
    await update.message.reply_text("⏱ How often should it check?", reply_markup=_pw_interval_keyboard())
    return PW.INTERVAL


async def recv_pw_exclude_ref_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Skipped — no ref to ignore, a fresh watch.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    sess = _get_pw_sess(ctx)
    sess.exclude_ref = ""
    await query.edit_message_text("⏭ Skipped — watching for any match.")
    await query.message.reply_text("⏱ How often should it check?", reply_markup=_pw_interval_keyboard())
    return PW.INTERVAL


async def recv_pw_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Store the chosen interval, then ask for delivery preference.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END

    sess = _get_pw_sess(ctx)
    sess.interval_minutes = int(choice)

    await query.edit_message_text(
        "📬 How should the alert be delivered when it's found?",
        reply_markup=_pw_delivery_keyboard(),
    )
    return PW.DELIVERY


async def recv_pw_delivery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Telegram-only -> move on to the valuer pick. Telegram+email -> ask
    # for the address first.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":")[1]

    if mode == "email":
        await query.edit_message_text("📧 Enter the email address to also notify:", parse_mode="Markdown")
        return PW.EMAIL_INPUT

    sess = _get_pw_sess(ctx)
    sess.email = ""
    await query.edit_message_text("✅ Delivery: 💬 Telegram only")
    return await _pw_proceed_to_valuer_pick(query.message, sess)


async def recv_pw_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Validate the email, store it, then move on to the valuer pick.
    if not allowed(update): return await deny(update)
    email = (update.message.text or "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        await update.message.reply_text("❌ Invalid email. Enter a valid address.", parse_mode="Markdown")
        return PW.EMAIL_INPUT

    sess = _get_pw_sess(ctx)
    sess.email = email
    await update.message.reply_text(f"✅ Email: {md_escape(email)}", parse_mode="Markdown")
    return await _pw_proceed_to_valuer_pick(update.message, sess)


# ──────────────────────────────────────────────────────────
# Valuer pick + hold choice (local, self-contained — mirrors
# bulk_field_update.py's valuer-search pattern; see module docstring)
# ──────────────────────────────────────────────────────────

async def _pw_proceed_to_valuer_pick(message, sess: PWSession) -> int:
    # Show the saved-valuer-or-search picker.
    saved = load_saved_valuers()
    if saved:
        rows = [
            [InlineKeyboardButton(f"👤 {sv['name']}", callback_data=f"pw_src:{i}")]
            for i, sv in enumerate(saved)
        ]
        rows.append([InlineKeyboardButton("🔍 Search new valuer", callback_data="pw_src:new")])
        await message.reply_text(
            "👤 Which valuer should be assigned once it's found?",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return PW.VALUER_SOURCE
    await message.reply_text(
        "👤 Enter the *valuer name* to search:\n_Partial names work, e.g._ `JOHN KAMAU`",
        parse_mode="Markdown",
    )
    return PW.VALUER_NAME


async def recv_pw_valuer_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle the saved-valuer-or-search picker.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    sess = _get_pw_sess(ctx)
    data = query.data.split(":")[1]

    if data == "new":
        await query.edit_message_text(
            "👤 Enter the *valuer name* to search:\n_Partial names work, e.g._ `JOHN KAMAU`",
            parse_mode="Markdown",
        )
        return PW.VALUER_NAME

    saved = load_saved_valuers()
    idx = int(data)
    if idx >= len(saved):
        await query.edit_message_text(
            "⚠️ That saved valuer no longer exists. Please start over.",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END
    sess.valuer = saved[idx]
    await query.edit_message_text(f"✅ Valuer: *{md_escape(sess.valuer['name'])}*", parse_mode="Markdown")
    await query.message.reply_text(
        "Should this task be held (guarded against takeover) once it's assigned?",
        reply_markup=_pw_hold_keyboard(),
    )
    return PW.HOLD_CHOICE


async def _pw_do_valuer_search(message, tokens, name: str) -> Optional[List[Dict]]:
    # Search for valuers by name; returns list or None (error already sent).
    try:
        http_sess = build_session()
        headers = {"Authorization": f"Bearer {tokens.access_token}", "JWTAUTH": f"Bearer {tokens.jwt}"}
        params = {"account_type": "STAFF", "filter_type": "ACTIVE", "page": 1, "search": name}
        resp = http_sess.get(ACCOUNTS_LIST_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        await message.reply_text(f"❌ Valuer search failed: `{e}`", parse_mode="Markdown")
        return None


async def recv_pw_valuer_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Receive the typed valuer name and run the live search immediately —
    # no OTP step, reuses whichever cached tokens recv_pw_parcel already
    # confirmed are valid.
    if not allowed(update): return await deny(update)
    sess = _get_pw_sess(ctx)
    sess.valuer_name = (update.message.text or "").strip()
    if not sess.valuer_name:
        await update.message.reply_text("Please enter a valuer name.")
        return PW.VALUER_NAME

    tokens = get_valid_tokens(_LU_CRED_DEFAULT) or get_valid_tokens(_PL_CRED_ASSESSOR)
    await update.message.reply_text(f"🔍 Searching for valuer *{md_escape(sess.valuer_name)}*…", parse_mode="Markdown")
    results = await _pw_do_valuer_search(update.message, tokens, sess.valuer_name)
    if results is None:
        await update.message.reply_text("Use the menu to start again.", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END
    if not results:
        await update.message.reply_text(
            f"⚠️ No valuers found matching *{md_escape(sess.valuer_name)}*.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    sess.valuers = results
    rows = []
    for i, v in enumerate(results):
        sd = v.get("staff_details", {})
        name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
        rows.append([InlineKeyboardButton(name or f"Valuer {i+1}", callback_data=f"pw_valuer:{i}")])
    await update.message.reply_text(
        f"Found *{len(results)}* valuer(s). Select one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return PW.SELECT_VALUER


async def recv_pw_valuer_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Live search result selected -> store it, ask the hold question.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    sess = _get_pw_sess(ctx)
    idx = int(query.data.split(":")[1])
    v = sess.valuers[idx]
    sd = v.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
    uid = sd.get("user_id", v.get("id", "?"))
    sess.valuer = {"name": name, "uid": uid, "account_number": v.get("account_number", "?")}

    await query.edit_message_text(f"✅ Valuer: *{md_escape(name)}*", parse_mode="Markdown")
    await query.message.reply_text(
        "Should this task be held (guarded against takeover) once it's assigned?",
        reply_markup=_pw_hold_keyboard(),
    )
    return PW.HOLD_CHOICE


async def recv_pw_hold_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Store the hold choice, then finalize — the last step in setup.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    sess = _get_pw_sess(ctx)
    sess.hold = query.data.split(":")[1] == "yes"

    watch_ids = _pw_finalize(query.message.chat_id, ctx)
    await query.edit_message_text(_pw_queued_text(watch_ids))
    await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(update.effective_user.id))
    return ConversationHandler.END


def _pw_finalize(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> List[str]:
    """Persist the watch(es) and schedule their repeating job(s) — the
    last step in setup, reached only after valuer + hold are both chosen.
    List mode (sess.parcels has entries) creates one independent watch per
    parcel, all sharing this interval/email/valuer/hold; single-parcel
    mode (the default, sess.parcels empty) creates exactly one, matching
    prior behavior exactly. first=interval (not 0) so a brand-new watch
    doesn't fire an immediate check before the user has even seen the
    confirmation message. Returns every watch id created."""
    sess = _get_pw_sess(ctx)
    parcels = sess.parcels or [sess.parcel]
    watch_ids = []
    for parcel in parcels:
        watch_id = add_parcel_watch(
            chat_id, parcel, sess.interval_minutes, sess.email, sess.valuer, sess.hold, sess.exclude_ref,
        )
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

def _pw_fetch_enriched(valuer_tokens, assessor_tokens, matches: List[Dict]):
    """For each matching list-item from _pl_search_parcel, fetch its
    detail-view and pair it with the item, its reference number, and
    whether it's an assessor-stage match. A DLV/Valuer-stage match (the
    default) uses lookup_reference.py's _lu_fetch_detail under
    valuer_tokens — the same primitive /lookup uses. An assessor/HQ-stage
    match (item["_assessor_stage"], see
    parcel_lookup._pl_search_parcel_assessor_stage) instead uses
    _lu_fetch_detail_county under assessor_tokens, since it comes from the
    stampdutyservice detail-view (a different shape, and a different
    credential — Support Reg, not Staff Valuer). Synchronous (run via
    asyncio.to_thread from _pw_check_job, one call covering the whole
    loop) since it's the same blocking-HTTP-in-a-thread pattern
    _pl_search_parcel already uses. Both detail-fetch primitives catch
    their own exceptions and return None on failure, so a single failed
    detail call degrades that one match to item-only fields rather than
    failing the whole notification. A match whose own stage has no cached
    tokens (the other stage still being searched) skips the detail call
    outright, same degrade."""
    enriched = []
    for item in matches:
        ref = item.get("reference_number", "—")
        is_assessor = bool(item.get("_assessor_stage"))
        if is_assessor:
            detail = _lu_fetch_detail_county(assessor_tokens, item.get("id")) if assessor_tokens else None
        else:
            detail = _lu_fetch_detail(valuer_tokens, item.get("id")) if valuer_tokens else None
        enriched.append((ref, item, detail, is_assessor))
    return enriched


async def _pw_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job for one watch: search the parcel, and if found,
    queue every matching ref into DLV Batch for the watch's chosen valuer
    (if any — see module docstring), notify (Telegram always, email too if
    configured), then remove the watch — one-shot, unlike Auto Fetch's
    continuous polling. Re-reads the watch fresh each cycle (rather than
    closing over it) so a manual cancel (recv_pw_cancel) since the job
    last fired is honored here instead of silently racing a stale
    in-memory copy.

    A watch with exclude_ref set (a reference known to be wrong/about to
    be cancelled on this parcel) filters that exact reference out of the
    search results before deciding whether anything was "found" — this is
    how a watch set up while the wrong ref is still on the valuer's desk
    waits for it to be cancelled and a genuinely new reference to appear
    for the same parcel, rather than firing immediately on the one
    already known about."""
    watch_id = context.job.data
    watch = get_parcel_watch(watch_id)
    if not watch:
        context.job.schedule_removal()
        return

    valuer_tokens   = get_valid_tokens(_LU_CRED_DEFAULT)
    assessor_tokens = get_valid_tokens(_PL_CRED_ASSESSOR)
    if not valuer_tokens and not assessor_tokens:
        logger.warning(
            "Parcel Watch job %s: no valid %s or %s tokens cached — skipping cycle.",
            watch_id, _LU_CRED_DEFAULT, _PL_CRED_ASSESSOR,
        )
        return

    matches = await asyncio.to_thread(_pl_search_parcel, valuer_tokens, assessor_tokens, watch["parcel"])
    exclude_ref = (watch.get("exclude_ref") or "").strip().upper()
    if exclude_ref:
        matches = [m for m in matches if (m.get("reference_number") or "").strip().upper() != exclude_ref]
    if not matches:
        return

    enriched = await asyncio.to_thread(_pw_fetch_enriched, valuer_tokens, assessor_tokens, matches)

    assign_note = ""
    valuer = watch.get("valuer")
    if valuer:
        for ref, _item, _detail, _is_assessor in enriched:
            save_dlv_batch([{
                "ref":         ref,
                "valuer_name": valuer["name"],
                "valuer_uid":  valuer["uid"],
                "valuer_acct": valuer.get("account_number", ""),
                "queued_at":   datetime.now().isoformat(timespec="seconds"),
                "tag":         "Parcel Watch",
                "auto_hold":   watch.get("hold", False),
            }])
        assign_note = f"\n📥 Queued for assignment to *{_lu_md_escape(valuer['name'])}*"
        assign_note += " — will be held once assigned." if watch.get("hold") else "."

    exclude_note = f"\n🚫 Ignored previous ref `{_lu_md_escape(exclude_ref)}`." if exclude_ref else ""
    header = (
        f"🔔 *Parcel Watch* — `{_lu_md_escape(watch['parcel'])}` found on {len(enriched)} application(s)"
        f"{exclude_note}{assign_note}"
    )
    lines = [header] + [
        _lu_format_county_result(ref, item, detail, markdown=True) if is_assessor
        else _lu_format_result(ref, item, detail, markdown=True)
        for ref, item, detail, is_assessor in enriched
    ]

    async def _send(text, reply_markup):
        await context.bot.send_message(watch["chat_id"], text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n\n")

    if watch.get("email"):
        try:
            plain_header = f"Parcel Watch — {watch['parcel']} found on {len(enriched)} application(s)"
            body = "\n\n".join(
                [plain_header] + [
                    _lu_format_county_result(ref, item, detail, markdown=False) if is_assessor
                    else _lu_format_result(ref, item, detail, markdown=False)
                    for ref, item, detail, is_assessor in enriched
                ],
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
        await update.message.reply_text("ℹ️ No active parcel watches.", reply_markup=_main_menu_for(update.effective_user.id))
        return

    lines = [f"⏳ *Parcel Watches* — {len(watches)} active\n"]
    lines += [
        f"{i}. `{_lu_md_escape(w.get('parcel', '?'))}` — every {w.get('interval_minutes', '?')} min"
        + (f" — 📧 {md_escape(w['email'])}" if w.get("email") else "")
        + (f" — 👤 {md_escape(w['valuer']['name'])}" if w.get("valuer") else "")
        + (" ✋" if w.get("hold") else "")
        + (f" — 🚫 ignoring `{_lu_md_escape(w['exclude_ref'])}`" if w.get("exclude_ref") else "")
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
            PW.PARCEL_INPUT:  [MessageHandler(not_cancel, recv_pw_parcel)],
            PW.EXCLUDE_REF:   [
                CallbackQueryHandler(recv_pw_exclude_ref_skip, pattern=r"^pw_exclref:"),
                MessageHandler(not_cancel, recv_pw_exclude_ref),
            ],
            PW.INTERVAL:      [CallbackQueryHandler(recv_pw_interval, pattern=r"^pw_interval:")],
            PW.DELIVERY:      [CallbackQueryHandler(recv_pw_delivery, pattern=r"^pw_delivery:")],
            PW.EMAIL_INPUT:   [MessageHandler(not_cancel, recv_pw_email)],
            PW.VALUER_SOURCE: [CallbackQueryHandler(recv_pw_valuer_source, pattern=r"^pw_src:")],
            PW.VALUER_NAME:   [MessageHandler(not_cancel, recv_pw_valuer_name)],
            PW.SELECT_VALUER: [CallbackQueryHandler(recv_pw_valuer_select, pattern=r"^pw_valuer:")],
            PW.HOLD_CHOICE:   [CallbackQueryHandler(recv_pw_hold_choice, pattern=r"^pw_hold:")],
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
