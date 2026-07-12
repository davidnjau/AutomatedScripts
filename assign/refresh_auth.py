#!/usr/bin/env python3
"""
refresh_auth.py
=================
Refresh Auth — manually (re)authenticate a credential profile (🔑 Refresh
Auth button / /auth). Standalone from every other feature's own
check-cache-then-login step: this is the explicit "log in / force a fresh
login" flow, used when a user wants to pre-warm or force-refresh a
profile's cached tokens outside of any other conversation.

Call register(app) from bot.py's main() to wire this feature in.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Optional

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

from ardhisasa_auth import AUTH_BASE_URL, build_session

from common import (
    BTN_AUTH,
    CRED_LABELS,
    CRED_MAP,
    _CANCEL_FILTER,
    _jwt_exp,
    _load_tokens_raw,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    not_cancel,
    persist_tokens,
)


# ──────────────────────────────────────────────────────────
# States — Refresh Auth conversation
# ──────────────────────────────────────────────────────────
class AS(Enum):
    CHOOSE_CRED   = auto()
    FORCE_CONFIRM = auto()
    WAIT_OTP      = auto()


@dataclass
class AuthSession:
    cred_type:    str = ""
    http_session: Optional[requests.Session] = None


def _get_auth_sess(ctx: ContextTypes.DEFAULT_TYPE) -> AuthSession:
    if "auth_session" not in ctx.user_data:
        ctx.user_data["auth_session"] = AuthSession()
    return ctx.user_data["auth_session"]


# ──────────────────────────────────────────────────────────
# Refresh Auth — conversation handlers
# ──────────────────────────────────────────────────────────

def _auth_cred_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(CRED_LABELS["publicuser"],   callback_data="auth_cred:publicuser")],
        [InlineKeyboardButton(CRED_LABELS["staff"],        callback_data="auth_cred:staff")],
        [InlineKeyboardButton(CRED_LABELS["staff2"],       callback_data="auth_cred:staff2")],
        [InlineKeyboardButton(CRED_LABELS["staff_valuer"], callback_data="auth_cred:staff_valuer")],
        [InlineKeyboardButton("❌ Cancel",                 callback_data="auth_cred:cancel")],
    ])


async def cmd_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    ctx.user_data["auth_session"] = AuthSession()
    await update.message.reply_text(
        "🔑 *Refresh Auth*\n\nSelect a credential profile to authenticate:",
        parse_mode="Markdown",
        reply_markup=_auth_cred_keyboard(),
    )
    return AS.CHOOSE_CRED


async def recv_auth_cred(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cred_type = query.data.split(":")[1]

    if cred_type == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    auth_sess           = _get_auth_sess(ctx)
    auth_sess.cred_type = cred_type

    cached = get_valid_tokens(cred_type)
    if cached:
        entry   = _load_tokens_raw().get(cred_type, {})
        exp_ts  = entry.get("expires_at", 0)
        exp_str = (
            datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if exp_ts else "unknown"
        )
        await query.edit_message_text(
            f"✅ *{CRED_LABELS[cred_type]}* already has valid cached tokens.\n"
            f"*Expires:* {exp_str}\n\n"
            "Force a fresh login anyway?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Yes, re-authenticate", callback_data="auth_force:yes")],
                [InlineKeyboardButton("✅ No, keep current",     callback_data="auth_force:no")],
            ]),
        )
        return AS.FORCE_CONFIRM

    # No valid tokens — go straight to login
    return await _auth_trigger_login(query, auth_sess)


async def recv_auth_force(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    auth_sess = _get_auth_sess(ctx)

    if query.data.split(":")[1] == "no":
        await query.edit_message_text(
            f"✅ Keeping existing tokens for *{CRED_LABELS[auth_sess.cred_type]}*.",
            parse_mode="Markdown",
        )
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    return await _auth_trigger_login(query, auth_sess)


async def _auth_trigger_login(query, auth_sess: AuthSession) -> int:
    """Send the login request and transition to WAIT_OTP, or END on failure."""
    creds = CRED_MAP[auth_sess.cred_type]
    auth_sess.http_session = build_session()

    await query.edit_message_text(
        f"🔐 Sending login request for *{CRED_LABELS[auth_sess.cred_type]}*…",
        parse_mode="Markdown",
    )
    try:
        resp = auth_sess.http_session.post(
            f"{AUTH_BASE_URL}/login",
            json={"username": creds["username"], "password": creds["password"],
                  "usertype": creds["usertype"], "otpcode": ""},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") is False and "error" in data:
            raise RuntimeError(data.get("error") or data.get("message"))
    except Exception as e:
        await query.message.reply_text(
            f"❌ Login failed: `{e}`\n\nUse the menu to retry.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    await query.message.reply_text(
        "📲 OTP sent to the registered device.\n\nPlease *reply with the OTP code*:",
        parse_mode="Markdown",
    )
    return AS.WAIT_OTP


async def recv_auth_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    auth_sess = _get_auth_sess(ctx)
    otp       = update.message.text.strip()
    creds     = CRED_MAP[auth_sess.cred_type]

    await update.message.reply_text("🔄 Verifying OTP…")
    try:
        resp = auth_sess.http_session.post(
            f"{AUTH_BASE_URL}/otpverify",
            json={"username": creds["username"], "password": creds["password"], "otpcode": otp},
            timeout=30,
        )
        resp.raise_for_status()
        data          = resp.json()
        details       = data.get("details", {})
        access_token  = details.get("access_token")
        jwt           = details.get("jwt")
        refresh_token = details.get("refresh_token", "")
        if not access_token or not jwt:
            raise RuntimeError(f"Tokens missing. Keys: {list(data.keys())}")

        persist_tokens(auth_sess.cred_type, access_token, jwt, refresh_token)

        exp_ts  = _jwt_exp(jwt)
        exp_str = (
            datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if exp_ts else "unknown"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ OTP verification failed: `{e}`\n\nSend the OTP again or tap 🛑 Cancel.",
            parse_mode="Markdown",
        )
        return AS.WAIT_OTP

    await update.message.reply_text(
        f"✅ *Authenticated successfully!*\n\n"
        f"*Profile:* {CRED_LABELS[auth_sess.cred_type]}\n"
        f"*Token expires:* {exp_str}",
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Refresh Auth conversation into the given Application."""
    auth_conv = ConversationHandler(
        entry_points=[
            CommandHandler("auth", cmd_auth),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_AUTH)}$"), cmd_auth),
        ],
        states={
            AS.CHOOSE_CRED:   [CallbackQueryHandler(recv_auth_cred,  pattern=r"^auth_cred:")],
            AS.FORCE_CONFIRM: [CallbackQueryHandler(recv_auth_force, pattern=r"^auth_force:")],
            AS.WAIT_OTP:      [MessageHandler(not_cancel, recv_auth_otp)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(auth_conv)
    # Note: bot.py also registers a bare MessageHandler(BTN_AUTH, cmd_auth)
    # outside this conversation (pre-existing, unreachable — auth_conv's own
    # entry point above already claims the button first). Preserved here
    # verbatim rather than silently dropped during the move.
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_AUTH)}$"), cmd_auth))
