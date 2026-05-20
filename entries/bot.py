#!/usr/bin/env python3
"""
bot.py — Ardhisasa Entries Bot
================================
Telegram bot for updating land registry entries.

Workflow (entries.MD):
  POST /registerservice/api/v1/ingestion/encumbrance-change
  Fields: parcel_number, nature_of_title, entry_number, section, entry_status

Auth / daemon infrastructure mirrors the assign bot:
  - 🔑 Refresh Auth  — login + OTP flow, tokens cached to data/saved_tokens.json
  - 🔒 Token Status  — show expiry for each cached credential profile
  - 🔄 Token Daemon  — start / stop the background token-refresh daemon
  - 🔁 Restart Bot   — hot-restart the process
"""

import asyncio
import base64
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
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

from ardhisasa_auth import (
    AUTH_BASE_URL,
    CRED_LABELS,
    AuthTokens,
    build_session,
    load_credentials,
)

# ──────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("entries.bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS = set(
    int(x.strip())
    for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",")
    if x.strip()
)

BASE_URL          = "https://ardhisasa-api.lands.go.ke"
ENTRIES_ENDPOINT  = f"{BASE_URL}/registerservice/api/v1/ingestion/encumbrance-change"

# ──────────────────────────────────────────────────────────
# Persistent storage
# ──────────────────────────────────────────────────────────

DATA_DIR          = os.path.join(os.path.dirname(__file__), "data")
SAVED_TOKENS_FILE = os.path.join(DATA_DIR, "saved_tokens.json")
DAEMON_SCRIPT     = os.path.join(os.path.dirname(__file__), "token_refresh_daemon.py")
DAEMON_PID_FILE   = os.path.join(DATA_DIR, "daemon.pid")
DAEMON_LOG_FILE   = os.path.join(DATA_DIR, "daemon.log")


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


# ── Token helpers ─────────────────────────────────────────

def _jwt_exp(token: str) -> Optional[float]:
    """Decode JWT payload and return the `exp` claim, or None on failure."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return float(data["exp"])
    except Exception:
        return None


def _load_tokens_raw() -> Dict:
    try:
        with open(SAVED_TOKENS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def persist_tokens(cred_type: str, access_token: str, jwt: str, refresh_token: str = ""):
    _ensure_data_dir()
    tokens = _load_tokens_raw()
    exp    = _jwt_exp(jwt) or (time.time() + 3600)
    entry: Dict = {"access_token": access_token, "jwt": jwt, "expires_at": exp}
    if refresh_token:
        entry["refresh_token"] = refresh_token
    elif tokens.get(cred_type, {}).get("refresh_token"):
        entry["refresh_token"] = tokens[cred_type]["refresh_token"]
    tokens[cred_type] = entry
    with open(SAVED_TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    logger.info("Cached tokens for cred_type=%s (exp=%s)", cred_type, exp)


def get_valid_tokens(cred_type: str) -> Optional[AuthTokens]:
    """Return cached AuthTokens if still valid (5 min buffer), else None."""
    entry = _load_tokens_raw().get(cred_type)
    if not entry:
        return None
    if entry.get("expires_at", 0) < time.time() + 300:
        logger.info("Cached tokens for %s are expired.", cred_type)
        return None
    return AuthTokens(access_token=entry["access_token"], jwt=entry["jwt"])


def _first_valid_tokens() -> Optional[AuthTokens]:
    """Return the first cached AuthTokens that are still valid, or None."""
    for profile in CRED_LABELS:
        t = get_valid_tokens(profile)
        if t:
            return t
    return None


# ──────────────────────────────────────────────────────────
# Credential map (built at runtime from .env)
# ──────────────────────────────────────────────────────────

def _build_cred_map() -> Dict[str, dict]:
    cred_map = {}
    for profile in CRED_LABELS:
        try:
            cred_map[profile] = load_credentials(profile)
        except ValueError:
            pass
    return cred_map


CRED_MAP: Dict[str, dict] = _build_cred_map()

# ──────────────────────────────────────────────────────────
# Auth guard
# ──────────────────────────────────────────────────────────

def allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    return update.effective_user.id in ALLOWED_IDS


async def deny(update: Update):
    await update.message.reply_text("⛔ You are not authorised to use this bot.")


# ──────────────────────────────────────────────────────────
# Daemon helpers  (identical pattern to assign bot)
# ──────────────────────────────────────────────────────────

def _daemon_read_pid() -> Optional[int]:
    try:
        with open(DAEMON_PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _daemon_running() -> bool:
    pid = _daemon_read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _daemon_start() -> Tuple[bool, str]:
    if _daemon_running():
        return False, f"Already running (PID {_daemon_read_pid()})."
    if not os.path.exists(DAEMON_SCRIPT):
        return False, f"Script not found: {DAEMON_SCRIPT}"
    _ensure_data_dir()
    log_fh = open(DAEMON_LOG_FILE, "a")
    proc = subprocess.Popen(
        [sys.executable, "-u", DAEMON_SCRIPT],
        stdout=log_fh, stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    with open(DAEMON_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    logger.info("Token refresh daemon started (PID %d)", proc.pid)
    return True, f"Started (PID {proc.pid}). Logs → `{DAEMON_LOG_FILE}`"


def _daemon_stop() -> Tuple[bool, str]:
    if not _daemon_running():
        return False, "Daemon is not running."
    pid = _daemon_read_pid()
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.3)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        try:
            os.remove(DAEMON_PID_FILE)
        except FileNotFoundError:
            pass
        logger.info("Token refresh daemon stopped (PID %d)", pid)
        return True, f"Daemon (PID {pid}) stopped."
    except Exception as e:
        return False, f"Failed to stop daemon: {e}"


def _daemon_status_text() -> str:
    running = _daemon_running()
    pid     = _daemon_read_pid()
    if running:
        return f"🟢 *Running* (PID {pid})"
    elif pid:
        return "🔴 *Not running* (stale PID file — process died)"
    else:
        return "🔴 *Not running*"


def _daemon_keyboard() -> InlineKeyboardMarkup:
    running = _daemon_running()
    rows = []
    if running:
        rows.append([InlineKeyboardButton("⏹ Stop Daemon",   callback_data="daemon:stop")])
    else:
        rows.append([InlineKeyboardButton("▶️ Start Daemon",  callback_data="daemon:start")])
    rows.append([InlineKeyboardButton("🔁 Refresh Status", callback_data="daemon:status")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────
# Menu
# ──────────────────────────────────────────────────────────

BTN_UPDATE_ENTRY = "📝 Update Entry"
BTN_AUTH         = "🔑 Refresh Auth"
BTN_TOKEN_STATUS = "🔒 Token Status"
BTN_DAEMON       = "🔄 Token Daemon"
BTN_RESTART      = "🔁 Restart Bot"
BTN_HELP         = "❓ Help"
BTN_CANCEL       = "🛑 Cancel"

_MENU_BUTTON_FILTER = filters.Regex(
    f"^({re.escape(BTN_UPDATE_ENTRY)}|{re.escape(BTN_AUTH)}"
    f"|{re.escape(BTN_TOKEN_STATUS)}|{re.escape(BTN_DAEMON)}"
    f"|{re.escape(BTN_RESTART)}|{re.escape(BTN_HELP)}|{re.escape(BTN_CANCEL)})$"
)
_CANCEL_FILTER = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")


def _main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_UPDATE_ENTRY)],
            [KeyboardButton(BTN_AUTH),         KeyboardButton(BTN_TOKEN_STATUS)],
            [KeyboardButton(BTN_DAEMON),       KeyboardButton(BTN_RESTART)],
            [KeyboardButton(BTN_HELP),         KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _auth_cred_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"auth_cred:{profile}")]
        for profile, label in CRED_LABELS.items()
        if profile in CRED_MAP
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="auth_cred:cancel")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────
# /start  /help
# ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        "🏛 *Ardhisasa Entries Bot*\n\nReady. Use the menu below.",
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        "🏛 *Ardhisasa Entries Bot — Help*\n\n"
        f"{BTN_UPDATE_ENTRY} — update a land registry entry\n"
        f"{BTN_AUTH} — authenticate a credential profile (login + OTP)\n"
        f"{BTN_TOKEN_STATUS} — view cached token expiry per profile\n"
        f"{BTN_DAEMON} — start / stop the background token refresh daemon\n"
        f"{BTN_RESTART} — restart the bot process\n"
        f"{BTN_CANCEL} — cancel any active flow\n",
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


# ──────────────────────────────────────────────────────────
# Token Status
# ──────────────────────────────────────────────────────────

async def cmd_token_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)

    raw   = _load_tokens_raw()
    now   = time.time()
    lines = []

    for cred_type, label in CRED_LABELS.items():
        if cred_type not in CRED_MAP:
            lines.append(f"{label}\n  ⚫ Not configured in .env")
            continue
        entry = raw.get(cred_type)
        if not entry:
            lines.append(f"{label}\n  ⚫ No token cached")
            continue
        exp = entry.get("expires_at") or _jwt_exp(entry.get("jwt", ""))
        if not exp:
            lines.append(f"{label}\n  ⚠️ Expiry unreadable")
            continue
        secs_left = exp - now
        exp_str   = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if secs_left <= 0:
            lines.append(f"{label}\n  🔴 Expired — {exp_str}")
        elif secs_left < 10 * 60:
            mins = int(secs_left // 60)
            lines.append(f"{label}\n  🟡 Expires in {mins}m — {exp_str}")
        else:
            hrs  = int(secs_left // 3600)
            mins = int((secs_left % 3600) // 60)
            time_str = f"{hrs}h {mins}m" if hrs else f"{mins}m"
            lines.append(f"{label}\n  🟢 Valid — expires in {time_str} ({exp_str})")

    await update.message.reply_text(
        "🔒 *Token Status*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


# ──────────────────────────────────────────────────────────
# Token Daemon
# ──────────────────────────────────────────────────────────

async def cmd_daemon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        f"🔄 *Token Refresh Daemon*\n\n"
        f"Status: {_daemon_status_text()}\n\n"
        "The daemon watches the token cache and silently refreshes "
        "each token *5 minutes before it expires*.\n"
        "Logs are written to `data/daemon.log`.",
        parse_mode="Markdown",
        reply_markup=_daemon_keyboard(),
    )


async def recv_daemon_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    if action == "start":
        ok, msg = _daemon_start()
    elif action == "stop":
        ok, msg = _daemon_stop()
    else:
        ok, msg = True, "Status refreshed."
    await query.edit_message_text(
        f"🔄 *Token Refresh Daemon*\n\n"
        f"Status: {_daemon_status_text()}\n\n"
        f"{'✅' if ok else '❌'} {msg}",
        parse_mode="Markdown",
        reply_markup=_daemon_keyboard(),
    )


# ──────────────────────────────────────────────────────────
# Refresh Auth — login + OTP conversation  (same as assign)
# ──────────────────────────────────────────────────────────

class AS(Enum):
    CHOOSE_CRED   = auto()
    FORCE_CONFIRM = auto()
    WAIT_OTP      = auto()


@dataclass
class AuthSession:
    cred_type:    str = ""
    http_session: Optional[object] = None


def _get_auth_sess(ctx: ContextTypes.DEFAULT_TYPE) -> AuthSession:
    if "auth_session" not in ctx.user_data:
        ctx.user_data["auth_session"] = AuthSession()
    return ctx.user_data["auth_session"]


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
            f"*Expires:* {exp_str}\n\nForce a fresh login anyway?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Yes, re-authenticate", callback_data="auth_force:yes")],
                [InlineKeyboardButton("✅ No, keep current",     callback_data="auth_force:no")],
            ]),
        )
        return AS.FORCE_CONFIRM

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
    creds = CRED_MAP[auth_sess.cred_type]
    auth_sess.http_session = build_session()
    await query.edit_message_text(
        f"🔐 Sending login request for *{CRED_LABELS[auth_sess.cred_type]}*…",
        parse_mode="Markdown",
    )
    try:
        resp = auth_sess.http_session.post(
            f"{AUTH_BASE_URL}/login",
            json={
                "username": creds["username"],
                "password": creds["password"],
                "usertype": creds["usertype"],
                "otpcode":  "",
            },
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
# Update Entry conversation
# ──────────────────────────────────────────────────────────

class UE(Enum):
    PARCEL_NUMBER   = auto()
    NATURE_OF_TITLE = auto()
    ENTRY_NUMBER    = auto()
    SECTION         = auto()
    ENTRY_STATUS    = auto()
    CONFIRM         = auto()


@dataclass
class UESession:
    parcel_number:   str = ""
    nature_of_title: str = ""
    entry_number:    str = ""
    section:         str = ""
    entry_status:    str = ""
    tokens:          Optional[AuthTokens] = None


def _get_ue_sess(ctx: ContextTypes.DEFAULT_TYPE) -> UESession:
    if "ue_session" not in ctx.user_data:
        ctx.user_data["ue_session"] = UESession()
    return ctx.user_data["ue_session"]


def _nature_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("FREEHOLD",  callback_data="ue_nature:FREEHOLD")],
        [InlineKeyboardButton("LEASEHOLD", callback_data="ue_nature:LEASEHOLD")],
    ])


def _section_keyboard() -> InlineKeyboardMarkup:
    sections = ["PROPRIETORSHIP", "ENCUMBRANCE", "EASEMENT", "CAUTION", "INHIBITION"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s, callback_data=f"ue_section:{s}")]
        for s in sections
    ])


def _status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ACTIVE",   callback_data="ue_status:ACTIVE")],
        [InlineKeyboardButton("INACTIVE", callback_data="ue_status:INACTIVE")],
    ])


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data="ue_confirm:yes"),
            InlineKeyboardButton("❌ Cancel",  callback_data="ue_confirm:no"),
        ]
    ])


async def cmd_update_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)

    # Require valid cached tokens before starting
    tokens = _first_valid_tokens()
    if not tokens:
        await update.message.reply_text(
            "⚠️ No valid tokens found.\n\nPlease tap *🔑 Refresh Auth* first to log in.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    ctx.user_data["ue_session"] = UESession(tokens=tokens)
    await update.message.reply_text(
        "📝 *Update Entry — Step 1 of 5*\n\nEnter the *parcel number*:\n"
        "_e.g. NAIROBI/BLOCK13/221_",
        parse_mode="Markdown",
    )
    return UE.PARCEL_NUMBER


async def recv_ue_parcel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ue = _get_ue_sess(ctx)
    ue.parcel_number = update.message.text.strip().upper()
    await update.message.reply_text(
        f"📝 *Update Entry — Step 2 of 5*\n\n"
        f"Parcel: `{ue.parcel_number}`\n\nSelect *nature of title*:",
        parse_mode="Markdown",
        reply_markup=_nature_keyboard(),
    )
    return UE.NATURE_OF_TITLE


async def recv_ue_nature(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ue = _get_ue_sess(ctx)
    ue.nature_of_title = query.data.split(":")[1]
    await query.edit_message_text(
        f"📝 *Update Entry — Step 3 of 5*\n\n"
        f"Parcel: `{ue.parcel_number}`\n"
        f"Nature of title: `{ue.nature_of_title}`\n\n"
        "Enter the *entry number*:",
        parse_mode="Markdown",
    )
    return UE.ENTRY_NUMBER


async def recv_ue_entry_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ue = _get_ue_sess(ctx)
    ue.entry_number = update.message.text.strip()
    await update.message.reply_text(
        f"📝 *Update Entry — Step 4 of 5*\n\n"
        f"Parcel: `{ue.parcel_number}`\n"
        f"Nature of title: `{ue.nature_of_title}`\n"
        f"Entry number: `{ue.entry_number}`\n\n"
        "Select the *section*:",
        parse_mode="Markdown",
        reply_markup=_section_keyboard(),
    )
    return UE.SECTION


async def recv_ue_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ue = _get_ue_sess(ctx)
    ue.section = query.data.split(":")[1]
    await query.edit_message_text(
        f"📝 *Update Entry — Step 5 of 5*\n\n"
        f"Parcel: `{ue.parcel_number}`\n"
        f"Nature of title: `{ue.nature_of_title}`\n"
        f"Entry number: `{ue.entry_number}`\n"
        f"Section: `{ue.section}`\n\n"
        "Select *entry status*:",
        parse_mode="Markdown",
        reply_markup=_status_keyboard(),
    )
    return UE.ENTRY_STATUS


async def recv_ue_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ue = _get_ue_sess(ctx)
    ue.entry_status = query.data.split(":")[1]
    await query.edit_message_text(
        f"📋 *Entry Summary — Please confirm*\n\n"
        f"*Parcel number:*   `{ue.parcel_number}`\n"
        f"*Nature of title:* `{ue.nature_of_title}`\n"
        f"*Entry number:*    `{ue.entry_number}`\n"
        f"*Section:*         `{ue.section}`\n"
        f"*Entry status:*    `{ue.entry_status}`",
        parse_mode="Markdown",
        reply_markup=_confirm_keyboard(),
    )
    return UE.CONFIRM


async def recv_ue_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ue = _get_ue_sess(ctx)

    if query.data.split(":")[1] == "no":
        await query.edit_message_text("❌ Entry update cancelled.")
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    await query.edit_message_text("⏳ Submitting entry update…")

    payload = {
        "parcel_number":   ue.parcel_number,
        "nature_of_title": ue.nature_of_title,
        "entry_number":    ue.entry_number,
        "section":         ue.section,
        "entry_status":    ue.entry_status,
    }

    try:
        sess = build_session()
        resp = sess.post(
            ENTRIES_ENDPOINT,
            json=payload,
            headers={
                "Authorization": f"Bearer {ue.tokens.access_token}",
                "jwtauth":       f"Bearer {ue.tokens.jwt}",
            },
            timeout=30,
        )
        status_code = resp.status_code
        data        = resp.json()
    except Exception as e:
        await query.message.reply_text(
            f"❌ Request failed: `{e}`",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    if status_code == 200:
        msg = f"✅ *{data.get('details', 'Entry updated')}*"
    elif status_code == 400:
        msg = f"⚠️ *Bad request:* {data.get('details', 'Entry does not exist')}"
    elif status_code == 403:
        msg = (
            f"🔴 *Token expired or invalid.*\n\n"
            f"{data.get('messages', data.get('detail', ''))}\n\n"
            "Tap *🔑 Refresh Auth* to re-authenticate."
        )
    else:
        msg = f"❓ Unexpected response ({status_code}): `{data}`"

    logger.info(
        "Update entry %s → HTTP %s: %s",
        ue.parcel_number, status_code, data,
    )
    await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# /cancel  /restart  fallback
# ──────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("auth_session", None)
    ctx.user_data.pop("ue_session", None)
    await update.message.reply_text("🛑 Flow cancelled.", reply_markup=_main_menu())
    return ConversationHandler.END


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text("🔁 Restarting bot… back in a moment.")
    await asyncio.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤔 I didn't understand that. Follow the steps or tap 🛑 Cancel to abort.",
        reply_markup=_main_menu(),
    )


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    if not CRED_MAP:
        raise RuntimeError(
            "No credential profiles are configured. "
            "Add at least one set of credentials to .env."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    not_cancel = filters.TEXT & ~filters.COMMAND & ~_CANCEL_FILTER

    # Refresh Auth conversation
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

    # Update Entry conversation
    entry_conv = ConversationHandler(
        entry_points=[
            CommandHandler("entry", cmd_update_entry),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_UPDATE_ENTRY)}$"), cmd_update_entry),
        ],
        states={
            UE.PARCEL_NUMBER:   [MessageHandler(not_cancel, recv_ue_parcel)],
            UE.NATURE_OF_TITLE: [CallbackQueryHandler(recv_ue_nature,       pattern=r"^ue_nature:")],
            UE.ENTRY_NUMBER:    [MessageHandler(not_cancel, recv_ue_entry_number)],
            UE.SECTION:         [CallbackQueryHandler(recv_ue_section,      pattern=r"^ue_section:")],
            UE.ENTRY_STATUS:    [CallbackQueryHandler(recv_ue_status,       pattern=r"^ue_status:")],
            UE.CONFIRM:         [CallbackQueryHandler(recv_ue_confirm,      pattern=r"^ue_confirm:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT & ~filters.COMMAND, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(auth_conv)
    app.add_handler(entry_conv)

    app.add_handler(CallbackQueryHandler(recv_daemon_action, pattern=r"^daemon:"))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_TOKEN_STATUS)}$"), cmd_token_status))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DAEMON)}$"),       cmd_daemon))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_RESTART)}$"),      cmd_restart))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HELP)}$"),         cmd_help))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"),       cmd_cancel))

    logger.info("Entries bot started. Polling for updates…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
