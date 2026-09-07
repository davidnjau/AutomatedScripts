#!/usr/bin/env python3
"""
common.py
=========
Shared infrastructure used by multiple feature modules: logging, generic
JSON persistence, the auth-token cache, saved-valuer/assignment storage,
cparams headers, the main menu keyboard, and auth guards.

Moved out of bot.py so that feature modules (dlv_batch.py, dlv_tasks.py,
and future extractions) can import this without creating a circular
dependency on bot.py itself.
"""

import base64
import json
import logging
import os
import threading
import time
import re
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes, ConversationHandler, filters

from ardhisasa_auth import (
    PUBLIC_CREDENTIALS,
    STAFF_CREDENTIALS_ICT,
    STAFF_CREDENTIALS_SUPPORT,
    STAFF_CREDENTIALS_VALUER,
    AuthTokens,
    decode_jwt_exp,
)

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

# ──────────────────────────────────────────────────────────
# Persistent storage
# ──────────────────────────────────────────────────────────
SAVED_VALUERS_FILE          = os.path.join(DATA_DIR, "saved_valuers.json")
SAVED_TOKENS_FILE           = os.path.join(DATA_DIR, "saved_tokens.json")
SAVED_ASSIGNMENTS_FILE      = os.path.join(DATA_DIR, "saved_assignments.json")
SAVED_SECTIONAL_CONFIG_FILE = os.path.join(DATA_DIR, "saved_sectional_config.json")
SAVED_APARTMENTS_CONFIG_FILE = os.path.join(DATA_DIR, "saved_apartments_config.json")
SAVED_CUSTOM_EXCLUSIONS_FILE = os.path.join(DATA_DIR, "saved_custom_exclusions.json")

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


def _safe_err(e: Exception) -> str:
    """Return a user-facing error string that contains no internal URLs or server detail.

    HTTP errors are reduced to their status code; everything else becomes a
    generic phrase so that API internals never leak into Telegram messages.
    The full exception is intentionally NOT included here — callers should
    log it separately before sending this string to the user.
    """
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"server returned HTTP {e.response.status_code}"
    return "unexpected error — check logs"


_MD_SPECIAL_CHARS = re.compile(r"([_*`\[])")


def md_escape(text: str) -> str:
    """Escape characters (_, *, `, [) that break Telegram's legacy Markdown
    parser when interpolating untrusted text — a valuer/assessor name from
    user input or an external API — into a parse_mode="Markdown" message.
    Without this, a name containing e.g. a stray underscore raises
    telegram.error.BadRequest and the whole handler crashes."""
    return _MD_SPECIAL_CHARS.sub(r"\\\1", text or "")


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
    """Return dict mapping reference_number → {valuer_name, valuer_uid, assigned_at, ...}.
    Backed by dlv_core's consolidated ref-keyed DLV-lifecycle store (Group A
    JSON consolidation) rather than a standalone file — dlv_core is imported
    locally (not at module level) since dlv_core.py itself imports from
    common.py, and a module-level import here would be circular. A record
    is included when it's ever been through persist_assignment (tested via
    `assigned_at` — only persist_assignment ever sets it, and it's never
    cleared by a later stage) AND its CURRENT status isn't "queued". Both
    halves matter:
    - `assigned_at` alone would wrongly include a merely-queued-but-
      not-yet-assigned DLV Batch item, since that also carries a
      valuer_uid but never went through persist_assignment.
    - Excluding status=="queued" specifically (regression) handles a ref
      that WAS assigned and later got re-queued into DLV Batch — assigned_at
      is preserved across that transition (merge, not replace), so without
      this check the ref would double-appear in both "Currently Queued"
      (via load_dlv_batch, correct) and "At Valuer's Desk" (via this
      function, stale) at once.
    A ref removed from the DLV queue or released from hold (status=
    "removed") stays included, since that only means this bot stopped
    tracking/guarding it, not that the underlying valuer assignment itself
    was undone."""
    import dlv_core
    store = dlv_core._load_consolidated()
    return {
        ref: dlv_core._project(r)
        for ref, r in store.items()
        if r.get("assigned_at") and r.get("status") != "queued"
    }


def persist_assignment(ref: str, valuer_name: str, valuer_uid: str, extra: Optional[Dict] = None):
    """Record ref → valuer in the consolidated DLV-lifecycle store. Merges
    onto whatever the store already knows about this ref (e.g. DLV Batch's
    queue item — parcel/consideration/tag/etc.) rather than replacing the
    record wholesale, so enrichment from an earlier stage is never dropped
    just because this call's `extra` doesn't repeat it. No cap on the
    number of tracked refs — unbounded retention matches the closed store's
    existing behavior; a count-based cap risked evicting a still-active
    record purely for being chronologically old."""
    import dlv_core
    with _ASSIGN_LOCK:
        store = dlv_core._load_consolidated()
        record = {
            **store.get(ref, {}),
            "ref":         ref,
            "status":      "assigned",
            "valuer_name": valuer_name,
            "valuer_uid":  valuer_uid,
            "assigned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if extra:
            record.update(extra)
        store[ref] = record
        dlv_core._save_consolidated(store)
    logger.info("Saved assignment %s → %s", ref, valuer_name)


# ── Sectional Properties config ────────────────────────────
# Read by Auto Fetch (for sectional-task auto-routing) as well as by
# Sectional Properties itself, so it lives here rather than in either
# feature module.

def load_sectional_config() -> Optional[Dict]:
    try:
        with open(SAVED_SECTIONAL_CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_sectional_config(cfg: Dict) -> None:
    _atomic_json_write(SAVED_SECTIONAL_CONFIG_FILE, cfg, indent=2)


# ── Apartments config ───────────────────────────────────────
# Same shape and purpose as Sectional Properties' config above, for
# apartment-title tasks instead — read by Auto Fetch (for apartment-task
# auto-routing) as well as by Apartments itself, so it lives here rather
# than in either feature module.

def load_apartments_config() -> Optional[Dict]:
    try:
        with open(SAVED_APARTMENTS_CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_apartments_config(cfg: Dict) -> None:
    _atomic_json_write(SAVED_APARTMENTS_CONFIG_FILE, cfg, indent=2)


# ── Custom exclusion keywords ────────────────────────────────
# User-managed additions to Auto Fetch's 9 built-in exclusion keywords
# (custom_exclusions.py's Manage Exclusions menu) — read by Auto Fetch's
# Exclude multi-select step as well as by Manage Exclusions itself, so it
# lives here rather than in either feature module, same reasoning as
# Sectional/Apartments' config above.

def load_custom_exclusions() -> List[str]:
    try:
        with open(SAVED_CUSTOM_EXCLUSIONS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_custom_exclusions(keywords: List[str]) -> None:
    _atomic_json_write(SAVED_CUSTOM_EXCLUSIONS_FILE, keywords, indent=2)


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


def _date_cutoff_str(days: int) -> str:
    """Return a YYYY-MM-DD cutoff string for N days ago (UTC)."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _within_days(date_created: str, cutoff: str) -> bool:
    """
    Compare date_created from the API response against a YYYY-MM-DD cutoff.
    ISO date strings are lexicographically sortable, so simple string compare works.
    If date_created is missing or unparseable, include the task (fail-open).
    """
    if not date_created:
        return True
    # Take only the date part (first 10 chars: YYYY-MM-DD) regardless of time/timezone suffix
    return date_created[:10] >= cutoff


# ──────────────────────────────────────────────────────────
# Credential profiles (used by any feature with its own login step)
# ──────────────────────────────────────────────────────────
CRED_MAP = {
    "publicuser":   PUBLIC_CREDENTIALS,
    "staff":        STAFF_CREDENTIALS_ICT,
    "staff2":       STAFF_CREDENTIALS_SUPPORT,
    "staff_valuer": STAFF_CREDENTIALS_VALUER,
}

CRED_LABELS = {
    "publicuser":   "👤 Public User",
    "staff":        "🏢 ICT",
    "staff2":       "🏢 Support Reg",
    "staff_valuer": "🏢 Staff Valuer",
}


def _cred_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(CRED_LABELS["publicuser"],   callback_data="cred:publicuser")],
        [InlineKeyboardButton(CRED_LABELS["staff"],        callback_data="cred:staff")],
        [InlineKeyboardButton(CRED_LABELS["staff2"],       callback_data="cred:staff2")],
        [InlineKeyboardButton(CRED_LABELS["staff_valuer"], callback_data="cred:staff_valuer")],
    ])


def _be_cred_keyboard() -> Optional[InlineKeyboardMarkup]:
    """Return an inline keyboard of credential profiles that currently have valid tokens.
    Returns None if no credentials are valid. Used by Bulk Export, Job Distribution,
    Lookup Reference, and Valuer Tasks — unlike _cred_keyboard() above, this only lists
    profiles with a cached token already, since those features run against a fixed
    credential rather than triggering a fresh login."""
    rows = [
        [InlineKeyboardButton(label, callback_data=f"be_cred:{key}")]
        for key, label in CRED_LABELS.items()
        if get_valid_tokens(key)
    ]
    return InlineKeyboardMarkup(rows) if rows else None


# Node code → human-readable label — shared by Lookup Reference and Valuer Tasks
_NODE_LABELS: Dict[str, str] = {
    "VALUATION_STAMP_DUTY_CREATED":        "📭 Unassigned (awaiting valuer)",
    "VALUATION_STAMP_DUTY_VALUER_REPORT":  "✍️ Assigned — valuer report pending",
    "STAMP_DUTY_PAYMENT_DEFINITION":       "💳 Payment stage",
}


# ──────────────────────────────────────────────────────────
# Fetch Tasks / Auto Fetch shared filter keyboards
# ──────────────────────────────────────────────────────────
def _ft_county_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌆 Nairobi",      callback_data="ft_county:nairobi"),
            InlineKeyboardButton("📋 All Counties", callback_data="ft_county:all"),
        ],
    ])


def _ft_registry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📁 Central",        callback_data="ft_registry:central"),
            InlineKeyboardButton("📁 Nairobi",         callback_data="ft_registry:nairobi"),
        ],
        [
            InlineKeyboardButton("📋 All Registries", callback_data="ft_registry:all"),
        ],
    ])


def _ft_amount_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0 – 1M",        callback_data="ft_amount:0_1m"),
            InlineKeyboardButton("1M – 5M",        callback_data="ft_amount:1m_5m"),
        ],
        [
            InlineKeyboardButton("5M – 10M",       callback_data="ft_amount:5m_10m"),
            InlineKeyboardButton("20M – 50M",      callback_data="ft_amount:20m_50m"),
        ],
        [
            InlineKeyboardButton("50M – 100M",     callback_data="ft_amount:50m_100m"),
            InlineKeyboardButton("10M – 80M",      callback_data="ft_amount:10m_80m"),
        ],
        [
            InlineKeyboardButton("10M – 50M",      callback_data="ft_amount:10m_50m"),
            InlineKeyboardButton("50M – 3B",       callback_data="ft_amount:50m_3b"),
        ],
        [
            InlineKeyboardButton("✏️ Custom",       callback_data="ft_amount:custom"),
            InlineKeyboardButton("📋 No filter",   callback_data="ft_amount:all"),
        ],
    ])


def _sectional_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Exclude Sectional", callback_data="ft_sectional:exclude"),
        InlineKeyboardButton("🏢 Sectional Only",    callback_data="ft_sectional:only"),
        InlineKeyboardButton("📋 All",               callback_data="ft_sectional:all"),
    ]])


def _af_exclusion_multiselect_keyboard(keywords, excluded) -> InlineKeyboardMarkup:
    """One toggle button per exclusion keyword (☑ excluded / ☐ kept), two
    per row, plus a trailing Done button. `excluded` is the in-progress set
    of keywords the user has checked off so far. Used by Auto Fetch's
    schedule-creation "Exclude" step for its unified Section + apartment-
    variant keyword list — not apartment-specific despite the name
    similarity to load_apartments_config elsewhere in this file."""
    rows = []
    row = []
    for kw in keywords:
        mark = "☑" if kw in excluded else "☐"
        row.append(InlineKeyboardButton(f"{mark} {kw}", callback_data=f"ft_excl_pick:{kw}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✅ Done", callback_data="ft_excl_pick:done")])
    return InlineKeyboardMarkup(rows)


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
BTN_APARTMENTS    = "🏬 Apartments"
BTN_HOLD_TASKS    = "✋ Hold Tasks"
BTN_INCREMENTAL   = "🔢 Incremental"
BTN_DLV_REPORT_SCHEDULE = "📧 DLV Report Schedule"
BTN_CUSTOM_EXCLUSIONS = "🚫 Exclusions"
BTN_TASK_ANALYTICS = "📈 Task Analytics"

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
    f"|{re.escape(BTN_APARTMENTS)}"
    f"|{re.escape(BTN_HOLD_TASKS)}|{re.escape(BTN_INCREMENTAL)}|{re.escape(BTN_DLV_REPORT_SCHEDULE)}"
    f"|{re.escape(BTN_CUSTOM_EXCLUSIONS)}|{re.escape(BTN_TASK_ANALYTICS)}"
    f"|{re.escape(BTN_RESTART)}|{re.escape(BTN_HELP)}|{re.escape(BTN_CANCEL)})$"
)
_CANCEL_FILTER = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")
not_cancel = filters.TEXT & ~filters.COMMAND & ~_CANCEL_FILTER


def _main_menu() -> ReplyKeyboardMarkup:
    # Apartments, Sectional, AF Results, Valuer Tasks, and Assignments are
    # intentionally left off this grid — hidden from the menu, but each
    # still has its own working /command entry point and MessageHandler,
    # so nothing about their functionality changed.
    return ReplyKeyboardMarkup(
        [
            # ── Assignment ──────────────────────────────────
            [KeyboardButton(BTN_ASSIGN)],
            [KeyboardButton(BTN_FETCH_TASKS),    KeyboardButton(BTN_AUTO_FETCH)],
            # ── DLV & Export ────────────────────────────────
            [KeyboardButton(BTN_DLV_BATCH)],
            [KeyboardButton(BTN_BULK_EXPORT),    KeyboardButton(BTN_JOB_DIST)],
            [KeyboardButton(BTN_TASK_ANALYTICS)],
            [KeyboardButton(BTN_EXPORT_STATUS),  KeyboardButton(BTN_ERROR_REPORT)],
            [KeyboardButton(BTN_DLV_TASKS),      KeyboardButton(BTN_HOLD_TASKS)],
            [KeyboardButton(BTN_BRIEFING),       KeyboardButton(BTN_CUSTOM_EXCLUSIONS)],
            [KeyboardButton(BTN_DLV_REPORT_SCHEDULE), KeyboardButton(BTN_INCREMENTAL)],
            # ── Lookup ──────────────────────────────────────
            [KeyboardButton(BTN_LOOKUP)],
            [KeyboardButton(BTN_AUTH),           KeyboardButton(BTN_TOKEN_STATUS)],
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


# (_send_bulk_export_email / _send_auto_fetch_email live in email_service.py)
