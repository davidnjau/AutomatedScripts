#!/usr/bin/env python3
"""
common.py
=========
Shared infrastructure used by multiple feature modules: logging, generic
JSON persistence, the auth-token cache, saved-valuer/assignment storage,
cparams headers, the main menu keyboard, auth guards, and the bulk-export
email sender.

Moved out of bot.py so that feature modules (dlv_batch.py, dlv_tasks.py,
and future extractions) can import this without creating a circular
dependency on bot.py itself.
"""

import base64
import json
import logging
import os
import smtplib
import threading
import time
import re
from logging.handlers import RotatingFileHandler
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders as _email_encoders
from typing import Dict, List, Optional

from dotenv import load_dotenv
from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes, ConversationHandler, filters

from ardhisasa_auth import AuthTokens, decode_jwt_exp

# ──────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)

_file_handler = RotatingFileHandler(
    os.path.join(DATA_DIR, "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=5
)
_file_handler.setFormatter(_log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
logger = logging.getLogger("ardhisasa.bot")

load_dotenv()

ALLOWED_IDS = set(
    int(x.strip())
    for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",")
    if x.strip()
)

# SMTP config for email notifications (all optional)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587").strip())
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()

BASE_URL = "https://ardhisasa-api.lands.go.ke"

# ──────────────────────────────────────────────────────────
# Persistent storage
# ──────────────────────────────────────────────────────────
SAVED_VALUERS_FILE     = os.path.join(DATA_DIR, "saved_valuers.json")
SAVED_TOKENS_FILE      = os.path.join(DATA_DIR, "saved_tokens.json")
SAVED_ASSIGNMENTS_FILE = os.path.join(DATA_DIR, "saved_assignments.json")

# base64('{"active_role":"DLV"}') — required cparams header for DLV task endpoints
CPARAMS_DLV          = base64.b64encode(b'{"active_role":"DLV"}').decode()
CPARAMS_ASSESSOR     = base64.b64encode(b'{"active_role":"ASSESSOR_OF_STAMP_DUTY"}').decode()
CPARAMS_VALUER_ROLE  = base64.b64encode(b'{"active_role":"VALUER"}').decode()
CPARAMS_SUPPORT      = base64.b64encode(b'{"active_role":"SUPPORT"}').decode()


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


# Serialises concurrent load-modify-save cycles on the assignments file.
# persist_assignment() is called from both async handlers and background threads.
_ASSIGN_LOCK = threading.Lock()


def _atomic_json_write(path: str, data, **dump_kwargs) -> None:
    """Write data as JSON to path atomically (write to .tmp then os.replace)."""
    _ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, **dump_kwargs)
    os.replace(tmp, path)


# ── Valuers ───────────────────────────────────────────────

def load_saved_valuers() -> List[Dict]:
    try:
        with open(SAVED_VALUERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def persist_valuer(name: str, uid: str, account_number: str):
    _ensure_data_dir()
    valuers = load_saved_valuers()
    if any(x["uid"] == uid for x in valuers):
        return  # already saved
    valuers.append({"name": name, "uid": uid, "account_number": account_number})
    _atomic_json_write(SAVED_VALUERS_FILE, valuers, indent=2)
    logger.info("Saved valuer %s (uid=%s)", name, uid)


# ── Assignments ───────────────────────────────────────────

def load_saved_assignments() -> Dict:
    """Return dict mapping reference_number → {valuer_name, valuer_uid, assigned_at}."""
    try:
        with open(SAVED_ASSIGNMENTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def persist_assignment(ref: str, valuer_name: str, valuer_uid: str):
    with _ASSIGN_LOCK:
        assignments = load_saved_assignments()
        assignments[ref] = {
            "valuer_name": valuer_name,
            "valuer_uid":  valuer_uid,
            "assigned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # Keep only the most recent 500 assignments to prevent unbounded growth
        if len(assignments) > 500:
            assignments = dict(list(assignments.items())[-500:])
        _atomic_json_write(SAVED_ASSIGNMENTS_FILE, assignments, indent=2)
    logger.info("Saved assignment %s → %s", ref, valuer_name)


# ── Tokens ────────────────────────────────────────────────

_jwt_exp = decode_jwt_exp  # shared implementation in ardhisasa_auth


def _load_tokens_raw() -> Dict:
    try:
        with open(SAVED_TOKENS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def persist_tokens(cred_type: str, access_token: str, jwt: str, refresh_token: str = ""):
    _ensure_data_dir()
    tokens = _load_tokens_raw()
    exp = _jwt_exp(jwt) or (time.time() + 3600)
    entry: Dict = {
        "access_token": access_token,
        "jwt":          jwt,
        "expires_at":   exp,
    }
    if refresh_token:
        entry["refresh_token"] = refresh_token
    elif tokens.get(cred_type, {}).get("refresh_token"):
        # Preserve existing refresh_token if a new one wasn't returned
        entry["refresh_token"] = tokens[cred_type]["refresh_token"]
    tokens[cred_type] = entry
    _atomic_json_write(SAVED_TOKENS_FILE, tokens, indent=2)
    logger.info("Cached tokens for cred_type=%s (exp=%s) → %s", cred_type, exp, SAVED_TOKENS_FILE)


def get_valid_tokens(cred_type: str) -> Optional[AuthTokens]:
    """Return cached AuthTokens if still valid (5 min buffer), else None."""
    entry = _load_tokens_raw().get(cred_type)
    if not entry:
        return None
    if entry.get("expires_at", 0) < time.time() + 300:
        logger.info("Cached tokens for %s are expired.", cred_type)
        return None
    return AuthTokens(access_token=entry["access_token"], jwt=entry["jwt"])


def _any_valid_tokens() -> Optional[AuthTokens]:
    """Return the first valid cached AuthTokens across all credential profiles."""
    for cred_type in ("staff_valuer", "staff2", "staff", "publicuser"):
        t = get_valid_tokens(cred_type)
        if t:
            return t
    return None


def _ft_headers(tokens: AuthTokens) -> dict:
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_SUPPORT,
    }


# ──────────────────────────────────────────────────────────
# Main menu button labels & keyboard
# ──────────────────────────────────────────────────────────
BTN_ASSIGN        = "📋 New Assignment"
BTN_DLV_BATCH     = "📥 DLV Batch"
BTN_DLV_QUEUE     = "🔍 DLV Queue"
BTN_AUTO_FETCH    = "⏰ Auto Fetch"
BTN_ASSIGNMENTS   = "📜 Assignments"
BTN_AUTH          = "🔑 Refresh Auth"
BTN_TOKEN_STATUS  = "🔒 Token Status"
BTN_ERROR_REPORT  = "📉 Error Report"
BTN_DAEMON        = "🔄 Token Daemon"
BTN_VALUERS       = "👥 Saved Valuers"
BTN_DELETE        = "🗑 Delete Valuer"
BTN_FETCH_TASKS   = "📊 Fetch Tasks"
BTN_DLV_TASKS     = "📋 DLV Tasks"
BTN_AF_RESULTS    = "🗂 AF Results"
BTN_BULK_EXPORT   = "📤 Export Valuation Report"
BTN_EXPORT_STATUS = "📊 Export Status"
BTN_JOB_DIST      = "🏆 Job Distribution"
BTN_LOOKUP        = "🔎 Lookup Reference"
BTN_VALUER_TASKS  = "👤 Valuer Tasks"
BTN_HELP          = "❓ Help"
BTN_RESTART       = "🔁 Restart Bot"
BTN_CANCEL        = "🛑 Cancel"
BTN_BRIEFING      = "🌅 Morning Briefing"
BTN_SECTIONAL     = "🔲 Sectional"

# Filter that matches any of the persistent menu button texts
_MENU_BUTTON_FILTER = filters.Regex(
    f"^({re.escape(BTN_ASSIGN)}|{re.escape(BTN_DLV_BATCH)}|{re.escape(BTN_DLV_QUEUE)}"
    f"|{re.escape(BTN_AUTO_FETCH)}|{re.escape(BTN_ASSIGNMENTS)}|{re.escape(BTN_AUTH)}"
    f"|{re.escape(BTN_TOKEN_STATUS)}|{re.escape(BTN_ERROR_REPORT)}|{re.escape(BTN_DAEMON)}"
    f"|{re.escape(BTN_VALUERS)}|{re.escape(BTN_DELETE)}"
    f"|{re.escape(BTN_FETCH_TASKS)}|{re.escape(BTN_AF_RESULTS)}|{re.escape(BTN_BULK_EXPORT)}"
    f"|{re.escape(BTN_EXPORT_STATUS)}|{re.escape(BTN_JOB_DIST)}|{re.escape(BTN_LOOKUP)}"
    f"|{re.escape(BTN_VALUER_TASKS)}"
    f"|{re.escape(BTN_DLV_TASKS)}|{re.escape(BTN_BRIEFING)}|{re.escape(BTN_SECTIONAL)}"
    f"|{re.escape(BTN_RESTART)}|{re.escape(BTN_HELP)}|{re.escape(BTN_CANCEL)})$"
)
_CANCEL_FILTER = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")
not_cancel = filters.TEXT & ~filters.COMMAND & ~_CANCEL_FILTER


def _main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            # ── Assignment ──────────────────────────────────
            [KeyboardButton(BTN_ASSIGN)],
            [KeyboardButton(BTN_FETCH_TASKS),    KeyboardButton(BTN_AUTO_FETCH)],
            [KeyboardButton(BTN_AF_RESULTS),     KeyboardButton(BTN_ASSIGNMENTS)],
            # ── DLV & Export ────────────────────────────────
            [KeyboardButton(BTN_DLV_BATCH)],
            [KeyboardButton(BTN_BULK_EXPORT),    KeyboardButton(BTN_EXPORT_STATUS)],
            [KeyboardButton(BTN_JOB_DIST),       KeyboardButton(BTN_VALUER_TASKS)],
            [KeyboardButton(BTN_DLV_TASKS),      KeyboardButton(BTN_SECTIONAL)],
            [KeyboardButton(BTN_BRIEFING)],
            # ── Lookup ──────────────────────────────────────
            [KeyboardButton(BTN_LOOKUP)],
            [KeyboardButton(BTN_AUTH),           KeyboardButton(BTN_TOKEN_STATUS)],
            [KeyboardButton(BTN_ERROR_REPORT)],
            [KeyboardButton(BTN_VALUERS),        KeyboardButton(BTN_DELETE)],
            # ── System ──────────────────────────────────────
            [KeyboardButton(BTN_DAEMON)],
            [KeyboardButton(BTN_RESTART),        KeyboardButton(BTN_HELP)],
            [KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


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
# /cancel & fallback (shared across every conversation)
# ──────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("session", None)
    await update.message.reply_text(
        "🛑 Flow cancelled.",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤔 I didn't understand that. Follow the steps above, or tap 🛑 Cancel to abort.",
        reply_markup=_main_menu(),
    )


# ──────────────────────────────────────────────────────────
# Email (bulk export + morning briefing delivery)
# ──────────────────────────────────────────────────────────
def _send_bulk_export_email(to_email: str, filename: str, xlsx_bytes: bytes) -> None:
    """Send the Excel file as an email attachment. Raises on failure."""
    if not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("SMTP_USER / SMTP_PASS not configured in .env")

    msg            = MIMEMultipart()
    msg["Subject"] = f"Ardhisasa Export Valuation Report — {filename}"
    msg["From"]    = SMTP_USER
    msg["To"]      = to_email

    body = (
        f"Please find attached the Ardhisasa stamp-duty bulk export.\n\n"
        f"File: {filename}\n"
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(xlsx_bytes)
    _email_encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_email, msg.as_string())
