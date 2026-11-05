#!/usr/bin/env python3
"""
sms_sender.py
=============
Send SMS (📱 Send SMS button / /sms) — an admin-only tool for firing a
batch of Ardhisasa-style verification-code test texts through the TextSMS
bulk gateway (sms.textsms.co.ke), to exercise the SMS pipeline.

Kept deliberately a *controlled test tool*, never a route to real people:
recipients come only from a pre-pulled pool of test-environment staff
phone numbers (saved_sms_test_numbers.json) that the flow cycles through —
there is no free-text "enter any number" path. That pool is seeded out of
band (pulled from the test-env list-user-accounts endpoint, which is on a
private IP the deployed bot can't even reach), so this module only ever
*reads* it.

Flow: pick a mode (🧪 Simulate — default — or 📤 Send real), a count
(1–_SMS_MAX_PER_RUN), and a message template (a saved one, or a new one
that gets saved for reuse). On confirm it cycles the pool to `count`
numbers, appends a fresh random 6-digit code to the template for each, and
either shows the would-be payload (Simulate, nothing sent) or POSTs the
single bulk request to the gateway. Simulate is the default and a real
send needs an explicit second confirmation; the apikey is never rendered
in any preview.

Gateway credentials come from the environment (SMS_PARTNER_ID /
SMS_API_KEY, already in .env). The gateway is NOT an Ardhisasa endpoint,
so its URL lives here rather than in endpoints.py.

Call register(app) from bot.py's main() to wire this feature in.
"""

import json
import os
import random
import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, List, Optional

import requests
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
    BTN_SMS_SENDER,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _main_menu_for,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    is_admin,
    logger,
    md_escape,
    not_cancel,
)
from telegram_report import _send_chunked_report

# ──────────────────────────────────────────────────────────
# Gateway constants (TextSMS — not an Ardhisasa endpoint, so local)
# ──────────────────────────────────────────────────────────
SMS_SEND_URL   = "https://sms.textsms.co.ke/api/services/sendbulk/"
_SMS_SHORTCODE = "Text"
_SMS_PASS_TYPE = "plain"
_SMS_MAX_PER_RUN = 50   # safety cap: most texts a single run may send

SAVED_SMS_TEST_NUMBERS_FILE = os.path.join(DATA_DIR, "saved_sms_test_numbers.json")
SAVED_SMS_TEMPLATES_FILE    = os.path.join(DATA_DIR, "saved_sms_templates.json")

# The sample message, used to seed the template store on first use. The
# verification code is NOT part of the stored template — a fresh one is
# appended per recipient at send time (see _build_payload).
_DEFAULT_TEMPLATE = "ARDHISASA: Your Ardhisasa login verification code is:"

# Matches a normalized Kenyan MSISDN (254 + 9 digits).
_MSISDN_RE = re.compile(r"^254\d{9}$")


# ──────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────

def load_sms_test_numbers() -> List[str]:
    # The test-env number pool (seeded out of band); [] if not seeded yet.
    try:
        with open(SAVED_SMS_TEST_NUMBERS_FILE) as f:
            nums = json.load(f)
        return [n for n in nums if isinstance(n, str)]
    except (FileNotFoundError, ValueError):
        return []


def load_sms_templates() -> List[str]:
    # Saved message bases; seeds with the default ARDHISASA sample on first use.
    try:
        with open(SAVED_SMS_TEMPLATES_FILE) as f:
            templates = json.load(f)
        if templates:
            return templates
    except (FileNotFoundError, ValueError):
        pass
    save_sms_templates([_DEFAULT_TEMPLATE])
    return [_DEFAULT_TEMPLATE]


def save_sms_templates(templates: List[str]) -> None:
    # Persist the full template list so a newly-added message survives restarts.
    _atomic_json_write(SAVED_SMS_TEMPLATES_FILE, templates, indent=2)


# ──────────────────────────────────────────────────────────
# Pure helpers (unit-tested)
# ──────────────────────────────────────────────────────────

def _gen_code() -> str:
    # A fresh random 6-digit verification code, zero-padded.
    return f"{random.randint(0, 999999):06d}"


def _normalize_msisdn(raw: str) -> Optional[str]:
    """Normalize a raw phone value to the gateway's 254XXXXXXXXX form, or
    None if it can't be. Handles 0XXXXXXXXX (local), bare 9-digit 7/1
    numbers, and existing +254/254 forms; drops anything else."""
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("0") and len(d) == 10:
        d = "254" + d[1:]
    elif len(d) == 9 and d[0] in "71":
        d = "254" + d
    return d if _MSISDN_RE.match(d) else None


def _build_recipients(pool: List[str], count: int) -> List[str]:
    # Cycle `pool` to produce exactly `count` numbers (circles around when
    # count exceeds the pool size). Empty pool → empty list.
    if not pool:
        return []
    return [pool[i % len(pool)] for i in range(count)]


def _build_payload(numbers: List[str], message: str) -> Dict:
    """Build the TextSMS sendbulk body: one smslist entry per number, each
    with its own freshly-generated code appended to `message`. Credentials
    are read from the environment per entry (the gateway expects them inside
    each item)."""
    partner_id = os.getenv("SMS_PARTNER_ID", "")
    api_key    = os.getenv("SMS_API_KEY", "")
    smslist = [
        {
            "partnerID": partner_id,
            "apikey":    api_key,
            "pass_type": _SMS_PASS_TYPE,
            "mobile":    n,
            "message":   f"{message}{_gen_code()}",
            "shortcode": _SMS_SHORTCODE,
        }
        for n in numbers
    ]
    return {"count": len(smslist), "smslist": smslist}


def _redact(payload: Dict) -> Dict:
    # A display-safe copy with every apikey masked.
    return {
        "count": payload.get("count"),
        "smslist": [{**item, "apikey": "***"} for item in payload.get("smslist", [])],
    }


# ──────────────────────────────────────────────────────────
# Conversation state + session
# ──────────────────────────────────────────────────────────
class SMS(Enum):
    MODE         = auto()   # Simulate vs Send real
    COUNT        = auto()   # how many texts (1.._SMS_MAX_PER_RUN)
    TEMPLATE     = auto()   # pick a saved message or add a new one
    TEMPLATE_ADD = auto()   # text entry for a new message
    CONFIRM      = auto()   # final go/no-go


@dataclass
class SMSSession:
    simulate: bool = True
    count:    int = 0
    message:  str = ""


def _get_sms_sess(ctx: ContextTypes.DEFAULT_TYPE) -> SMSSession:
    # Lazily create and cache this chat's SMS-sender session.
    if "sms_session" not in ctx.user_data:
        ctx.user_data["sms_session"] = SMSSession()
    return ctx.user_data["sms_session"]


# ──────────────────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────────────────

def _sms_mode_keyboard() -> InlineKeyboardMarkup:
    # Simulate (default/safe) vs real send.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Simulate (dry run)", callback_data="sms_mode:simulate")],
        [InlineKeyboardButton("📤 Send real texts",    callback_data="sms_mode:real")],
        [InlineKeyboardButton("🛑 Cancel",             callback_data="sms_mode:cancel")],
    ])


def _sms_template_keyboard(templates: List[str]) -> InlineKeyboardMarkup:
    # One button per saved template (index-based callback to dodge the
    # 64-byte callback_data limit on long messages), plus add-new/cancel.
    rows = [
        [InlineKeyboardButton(
            (t[:40] + "…") if len(t) > 40 else t,
            callback_data=f"sms_tpl:{i}",
        )]
        for i, t in enumerate(templates)
    ]
    rows.append([InlineKeyboardButton("➕ New message", callback_data="sms_tpl:new")])
    rows.append([InlineKeyboardButton("🛑 Cancel",      callback_data="sms_tpl:cancel")])
    return InlineKeyboardMarkup(rows)


def _sms_confirm_keyboard(simulate: bool) -> InlineKeyboardMarkup:
    # Real sends get an unambiguous confirm label so there's no accidental blast.
    go = "✅ Run simulation" if simulate else "⚠️ SEND REAL TEXTS"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(go,        callback_data="sms_go:yes"),
        InlineKeyboardButton("🛑 Cancel", callback_data="sms_go:no"),
    ]])


# ──────────────────────────────────────────────────────────
# Conversation handlers
# ──────────────────────────────────────────────────────────

async def cmd_sms(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point (admin-only): describe the tool, then ask the send mode.
    if not allowed(update): return await deny(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "🔒 Send SMS is admin-only.",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    pool = load_sms_test_numbers()
    if not pool:
        await update.message.reply_text(
            "📱 *Send SMS*\n\n⚠️ The test-number pool is empty — nothing to send to. "
            "It needs to be re-seeded from the test environment first.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    if not os.getenv("SMS_PARTNER_ID") or not os.getenv("SMS_API_KEY"):
        await update.message.reply_text(
            "📱 *Send SMS*\n\n❌ SMS_PARTNER_ID / SMS_API_KEY are not set in the environment.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    ctx.user_data["sms_session"] = SMSSession()
    await update.message.reply_text(
        "📱 *Send SMS* — fire a batch of test verification-code texts to the "
        f"test-number pool (*{len(pool)}* numbers, cycled).\n\n"
        "Step 1 — Simulate first, or send for real?",
        parse_mode="Markdown",
        reply_markup=_sms_mode_keyboard(),
    )
    return SMS.MODE


async def recv_sms_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Store Simulate/real choice, then ask how many texts.
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]
    if choice == "cancel":
        await query.edit_message_text("🛑 Cancelled.")
        await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END

    sess = _get_sms_sess(ctx)
    sess.simulate = (choice == "simulate")
    label = "🧪 Simulate" if sess.simulate else "📤 Send real"
    await query.edit_message_text(
        f"✅ Mode: *{label}*\n\n"
        f"Step 2 — How many texts? (1–{_SMS_MAX_PER_RUN})",
        parse_mode="Markdown",
    )
    return SMS.COUNT


async def recv_sms_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Validate the requested count against the cap, then show templates.
    sess = _get_sms_sess(ctx)
    raw = (update.message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= _SMS_MAX_PER_RUN):
        await update.message.reply_text(
            f"⚠️ Enter a whole number between 1 and {_SMS_MAX_PER_RUN}."
        )
        return SMS.COUNT
    sess.count = int(raw)
    await update.message.reply_text(
        f"✅ Count: *{sess.count}*\n\nStep 3 — Choose a message (a fresh 6-digit code "
        "is appended to each):",
        parse_mode="Markdown",
        reply_markup=_sms_template_keyboard(load_sms_templates()),
    )
    return SMS.TEMPLATE


async def recv_sms_template(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Pick a saved template, add a new one, or cancel.
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]

    if data == "cancel":
        await query.edit_message_text("🛑 Cancelled.")
        await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END

    if data == "new":
        await query.edit_message_text(
            "✏️ Enter the message text (the 6-digit code is appended automatically, "
            "so end it where the code should go, e.g. `… code is:`):",
            parse_mode="Markdown",
        )
        return SMS.TEMPLATE_ADD

    templates = load_sms_templates()
    idx = int(data)
    if idx >= len(templates):
        await query.edit_message_text(
            "⚠️ That template no longer exists. Start again.",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END
    _get_sms_sess(ctx).message = templates[idx]
    return await _sms_show_confirm(query.message, ctx, update.effective_user.id)


async def recv_sms_template_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Save a newly-typed message for reuse, then go to confirm.
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Please enter a non-empty message.")
        return SMS.TEMPLATE_ADD
    templates = load_sms_templates()
    if text not in templates:
        templates.append(text)
        save_sms_templates(templates)
    _get_sms_sess(ctx).message = text
    return await _sms_show_confirm(update.message, ctx, update.effective_user.id)


async def _sms_show_confirm(message, ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    # Summarize what will happen and ask for the final go/no-go.
    sess = _get_sms_sess(ctx)
    mode = "🧪 Simulate (nothing sent)" if sess.simulate else "📤 *SEND REAL TEXTS*"
    await message.reply_text(
        f"📋 *Confirm*\n\n"
        f"*Mode:* {mode}\n"
        f"*Texts:* {sess.count}\n"
        f"*Message:* {md_escape(sess.message)}{{code}}\n\n"
        "Proceed?",
        parse_mode="Markdown",
        reply_markup=_sms_confirm_keyboard(sess.simulate),
    )
    return SMS.CONFIRM


async def recv_sms_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Final step: build the payload and either preview it (Simulate) or POST it.
    query = update.callback_query
    await query.answer()
    if query.data == "sms_go:no":
        await query.edit_message_text("🛑 Cancelled.")
        await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END

    sess = _get_sms_sess(ctx)
    numbers = _build_recipients(load_sms_test_numbers(), sess.count)
    payload = _build_payload(numbers, sess.message)

    if sess.simulate:
        await query.edit_message_text("🧪 *SIMULATION — nothing sent*", parse_mode="Markdown")
        lines = [f"🧪 *Simulation* — {payload['count']} text(s) that *would* be sent:"]
        lines += [
            f"{i}. `{item['mobile']}` → {md_escape(item['message'])}"
            for i, item in enumerate(_redact(payload)["smslist"], 1)
        ]
        async def _send(text, reply_markup):
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        await _send_chunked_report(_send, lines, reply_markup=_main_menu_for(update.effective_user.id))
        logger.info("SMS simulate: %d text(s), message=%r", payload["count"], sess.message)
        return ConversationHandler.END

    await query.edit_message_text(f"📤 Sending {payload['count']} text(s)…")
    try:
        resp = requests.post(SMS_SEND_URL, json=payload, timeout=30)
        body = resp.text[:500]
        logger.info("SMS real send: %d text(s), HTTP %s", payload["count"], resp.status_code)
        await query.message.reply_text(
            f"🏁 *Sent {payload['count']} text(s)* — gateway HTTP {resp.status_code}\n"
            f"`{md_escape(body)}`",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
    except Exception as e:
        logger.error("SMS real send failed: %s", e)
        await query.message.reply_text(
            f"❌ Send failed: `{md_escape(str(e)[:200])}`",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Send SMS conversation into the given Application."""
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("sms", cmd_sms),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_SMS_SENDER)}$"), cmd_sms),
        ],
        states={
            SMS.MODE:         [CallbackQueryHandler(recv_sms_mode, pattern=r"^sms_mode:")],
            SMS.COUNT:        [MessageHandler(not_cancel, recv_sms_count)],
            SMS.TEMPLATE:     [CallbackQueryHandler(recv_sms_template, pattern=r"^sms_tpl:")],
            SMS.TEMPLATE_ADD: [MessageHandler(not_cancel, recv_sms_template_add)],
            SMS.CONFIRM:      [CallbackQueryHandler(recv_sms_go, pattern=r"^sms_go:")],
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
