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
from telegram.ext import Application, ContextTypes, ConversationHandler, ExtBot, filters

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

# Admins can manage every other allowed user's per-category menu access
# (see load_category_access/category_allowed below and access_control.py's
# Manage Access flow) and always have every category themselves,
# regardless of what's been granted to them explicitly. A separate,
# smaller list from ALLOWED_IDS on purpose — being allowed to use the bot
# and being allowed to manage other users' access are different things.
ADMIN_IDS = set(
    int(x.strip())
    for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
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
SAVED_CATEGORY_ACCESS_FILE  = os.path.join(DATA_DIR, "saved_category_access.json")
SAVED_USER_NAMES_FILE       = os.path.join(DATA_DIR, "saved_user_names.json")
SAVED_CHAT_MESSAGES_FILE    = os.path.join(DATA_DIR, "saved_chat_messages.json")

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


# ── Per-user category access (Manage Access, access_control.py) ───────

def load_category_access() -> Dict[str, List[str]]:
    """{str(user_id): [category_label, ...]} — every non-admin user's
    explicitly granted menu categories. A user with no entry here has
    zero categories — locked out by default until an admin grants some
    (see get_user_categories's admin bypass and its no-admin-configured
    safety fallback below)."""
    try:
        with open(SAVED_CATEGORY_ACCESS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_category_access(data: Dict[str, List[str]]) -> None:
    _atomic_json_write(SAVED_CATEGORY_ACCESS_FILE, data, indent=2)


def is_admin(user_id: int) -> bool:
    """True if user_id is configured as an admin (ADMIN_TELEGRAM_IDS)."""
    return user_id in ADMIN_IDS


def get_user_categories(user_id: int) -> List[str]:
    """Every menu category label user_id may open. Admins always get
    every category, regardless of what's explicitly granted to them.
    Safety fallback: if no admin is configured at all (ADMIN_IDS empty),
    there would be nobody able to grant access to anyone, ever — so in
    that case every user gets every category too, exactly like the
    pre-Manage-Access behavior. The empty-by-default restriction only
    actually applies once at least one admin exists to lift it."""
    if not ADMIN_IDS or user_id in ADMIN_IDS:
        return list(_MENU_CATEGORIES.keys())
    return load_category_access().get(str(user_id), [])


def category_allowed(user_id: int, category: str) -> bool:
    """True if user_id may open the given category label."""
    return category in get_user_categories(user_id)


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


# Cap for multi-item paste inputs (Lookup Reference, Parcel Lookup, Parcel
# Watch, DLV Ref Check) — each item can mean one or more sequential API
# calls, so this keeps a single pasted list from turning into an
# unbounded chain of requests in one conversation turn.
_LIST_INPUT_MAX_ITEMS = 25


def _parse_list_input(raw: str) -> List[str]:
    """Split a free-text entry into a list of trimmed, non-empty items —
    one per line, comma-separated on a single line, or a mix of both. A
    single plain entry with no separators still returns a one-item list,
    so every caller can treat "one value" and "a pasted list" the same
    way. Does not dedupe — a caller compiling one result per item decides
    whether repeats matter (e.g. the same ref pasted twice)."""
    return [r.strip() for r in re.split(r"[\n,]+", raw) if r.strip()]


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
BTN_PARCEL_LOOKUP = "🏞 Parcel Lookup"
BTN_PARCEL_WATCH  = "⏳ Parcel Watch"
BTN_DLV_REF_CHECK = "🕓 DLV Ref Check"
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
BTN_CLEAR_CHAT    = "🧹 Clear Chat"

# Category buttons — the main menu shows only these six (plus Cancel);
# tapping one opens that category's own submenu of workflow buttons (see
# _MENU_CATEGORIES/_category_menu/_main_menu_for below). BTN_BACK returns
# from a submenu to the main category menu.
BTN_CAT_ASSIGNMENTS = "📋 Assignments"
BTN_CAT_AUTOMATION  = "🤖 Automation"
BTN_CAT_ANALYTICS   = "📈 Analytics"
BTN_CAT_LOOKUPS     = "🔎 Look Ups"
BTN_CAT_VALUERS     = "👥 Valuers"
BTN_CAT_SETTINGS    = "⚙️ Bot Settings"
BTN_CAT_POST_BOARD  = "📥 Post Board"
BTN_BACK            = "⬅ Back"

# Admin-only action appended to ⚙️ Bot Settings' submenu (only for admins,
# see recv_menu_category) — manages other users' category access
# (access_control.py).
BTN_MANAGE_ACCESS = "🔐 Manage Access"

# 📥 Post Board's two member buttons (post_board.py) — its own category
# rather than a member of another one, so it's independently grantable
# via Manage Access.
BTN_PB_POST = "➕ Post New Item(s)"
BTN_PB_VIEW = "📋 View Queue"

# Filter that matches any of the persistent menu button texts
_MENU_BUTTON_FILTER = filters.Regex(
    f"^({re.escape(BTN_ASSIGN)}|{re.escape(BTN_DLV_BATCH)}|{re.escape(BTN_DLV_QUEUE)}"
    f"|{re.escape(BTN_AUTO_FETCH)}|{re.escape(BTN_ASSIGNMENTS)}|{re.escape(BTN_AUTH)}"
    f"|{re.escape(BTN_TOKEN_STATUS)}|{re.escape(BTN_ERROR_REPORT)}|{re.escape(BTN_DAEMON)}"
    f"|{re.escape(BTN_VALUERS)}|{re.escape(BTN_DELETE)}"
    f"|{re.escape(BTN_FETCH_TASKS)}|{re.escape(BTN_AF_RESULTS)}|{re.escape(BTN_BULK_EXPORT)}"
    f"|{re.escape(BTN_EXPORT_STATUS)}|{re.escape(BTN_JOB_DIST)}|{re.escape(BTN_LOOKUP)}"
    f"|{re.escape(BTN_PARCEL_LOOKUP)}|{re.escape(BTN_PARCEL_WATCH)}|{re.escape(BTN_DLV_REF_CHECK)}"
    f"|{re.escape(BTN_VALUER_TASKS)}"
    f"|{re.escape(BTN_DLV_TASKS)}|{re.escape(BTN_BRIEFING)}|{re.escape(BTN_SECTIONAL)}"
    f"|{re.escape(BTN_APARTMENTS)}"
    f"|{re.escape(BTN_HOLD_TASKS)}|{re.escape(BTN_INCREMENTAL)}|{re.escape(BTN_DLV_REPORT_SCHEDULE)}"
    f"|{re.escape(BTN_CUSTOM_EXCLUSIONS)}|{re.escape(BTN_TASK_ANALYTICS)}|{re.escape(BTN_CLEAR_CHAT)}"
    f"|{re.escape(BTN_CAT_ASSIGNMENTS)}|{re.escape(BTN_CAT_AUTOMATION)}|{re.escape(BTN_CAT_ANALYTICS)}"
    f"|{re.escape(BTN_CAT_LOOKUPS)}|{re.escape(BTN_CAT_VALUERS)}|{re.escape(BTN_CAT_SETTINGS)}"
    f"|{re.escape(BTN_CAT_POST_BOARD)}|{re.escape(BTN_PB_POST)}|{re.escape(BTN_PB_VIEW)}"
    f"|{re.escape(BTN_BACK)}|{re.escape(BTN_MANAGE_ACCESS)}"
    f"|{re.escape(BTN_RESTART)}|{re.escape(BTN_HELP)}|{re.escape(BTN_CANCEL)})$"
)
_CANCEL_FILTER = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")
not_cancel = filters.TEXT & ~filters.COMMAND & ~_CANCEL_FILTER

# Each category: a short description shown when the category button is
# tapped, plus the list of workflow buttons its submenu shows (in
# display order). Apartments, Sectional, AF Results, Valuer Tasks, and
# Assignments are intentionally left out of every category — hidden from
# the menu exactly as before this reorganization, but each still has its
# own working /command entry point and MessageHandler.
_MENU_CATEGORIES: Dict[str, Dict[str, object]] = {
    BTN_CAT_ASSIGNMENTS: {
        "description": (
            "📋 *Assignments*\n\n"
            "Assign, queue, and distribute stamp-duty valuation tasks to valuers."
        ),
        "buttons": [BTN_ASSIGN, BTN_DLV_BATCH, BTN_DLV_TASKS, BTN_HOLD_TASKS, BTN_JOB_DIST],
    },
    BTN_CAT_AUTOMATION: {
        "description": (
            "🤖 *Automation*\n\n"
            "Schedule recurring fetches, briefings, and reports so tasks and "
            "DLV updates come to you automatically."
        ),
        "buttons": [
            BTN_FETCH_TASKS, BTN_AUTO_FETCH, BTN_BRIEFING,
            BTN_DLV_REPORT_SCHEDULE, BTN_INCREMENTAL, BTN_CUSTOM_EXCLUSIONS,
        ],
    },
    BTN_CAT_ANALYTICS: {
        "description": (
            "📈 *Analytics*\n\n"
            "Export valuation reports and monitor background job progress."
        ),
        "buttons": [BTN_TASK_ANALYTICS, BTN_BULK_EXPORT, BTN_EXPORT_STATUS, BTN_ERROR_REPORT],
    },
    BTN_CAT_LOOKUPS: {
        "description": (
            "🔎 *Look Ups*\n\n"
            "Check a reference or parcel number's status, on demand or via a standing watch."
        ),
        "buttons": [BTN_LOOKUP, BTN_PARCEL_LOOKUP, BTN_PARCEL_WATCH, BTN_DLV_REF_CHECK],
    },
    BTN_CAT_VALUERS: {
        "description": (
            "👥 *Valuers*\n\n"
            "Manage the saved valuer list used across New Assignment, Receive "
            "Tasks, and Job Distribution."
        ),
        "buttons": [BTN_VALUERS, BTN_DELETE],
    },
    BTN_CAT_SETTINGS: {
        "description": (
            "⚙️ *Bot Settings*\n\n"
            "Manage login sessions, the token refresh daemon, and the bot process itself."
        ),
        "buttons": [BTN_AUTH, BTN_TOKEN_STATUS, BTN_DAEMON, BTN_RESTART, BTN_HELP, BTN_CLEAR_CHAT],
    },
    BTN_CAT_POST_BOARD: {
        "description": (
            "📥 *Post Board*\n\n"
            "A shared drop box for reference numbers and photos — post items here "
            "and everyone else with access is notified immediately; check the "
            "queue any time and clear (✅ Done) whatever's been dealt with."
        ),
        "buttons": [BTN_PB_POST, BTN_PB_VIEW],
    },
}


def _category_menu(buttons: List[str]) -> ReplyKeyboardMarkup:
    """Build a category's submenu keyboard — its workflow buttons two per
    row, then a final row of ⬅ Back and 🛑 Cancel (both always available
    from inside any category)."""
    rows = []
    row: List[KeyboardButton] = []
    for label in buttons:
        row.append(KeyboardButton(label))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _category_row_keyboard(categories: List[str]) -> ReplyKeyboardMarkup:
    """Shared row-builder for _main_menu_for — two categories per row, an
    odd category out gets its own final row, plus Cancel. Handles any
    category count, including an odd one out (e.g. 7 becomes 3 rows of 2
    + 1 row of 1), without hardcoding a fixed grid shape."""
    pairs = [categories[i:i + 2] for i in range(0, len(categories), 2)]
    rows = [[KeyboardButton(b) for b in pair] for pair in pairs]
    rows.append([KeyboardButton(BTN_CANCEL)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _main_menu_for(user_id: int) -> ReplyKeyboardMarkup:
    """Top-level menu filtered to whichever categories user_id may open
    (get_user_categories) — only that user's permitted category buttons.
    This is the only top-level-menu builder in the bot: every feature
    module's "flow finished" message, every /start, ⬅ Back, 🛑 Cancel, and
    the generic "I didn't understand" fallback all call this (with
    whichever chat/user identifier is in scope at that call site — this
    bot is private/1:1-DM-only, so chat_id == user_id always). A
    restricted user therefore never sees a category button they aren't
    granted, at any point in the bot."""
    return _category_row_keyboard(get_user_categories(user_id))


# ──────────────────────────────────────────────────────────
# Auth guard
# ──────────────────────────────────────────────────────────

def load_user_names() -> Dict[str, str]:
    """{str(user_id): display_name} — every user's cached Telegram display
    name (see _record_user_name), used by access_control.py's Manage
    Access picker to show a human name instead of a bare numeric ID."""
    try:
        with open(SAVED_USER_NAMES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_user_names(names: Dict[str, str]) -> None:
    _atomic_json_write(SAVED_USER_NAMES_FILE, names, indent=2)


def _record_user_name(update: Update) -> None:
    """Cache this user's display name (first + last name, falling back to
    @username if no name is set on their Telegram account) the first time
    we see them, or whenever it's changed since — called from allowed()
    so every allowed user gets captured automatically with no per-feature
    wiring needed. A cheap no-op (one dict lookup, no disk write) once the
    cached value already matches, so this doesn't add real I/O cost to
    the common case."""
    user = update.effective_user
    if not user:
        return
    display = " ".join(filter(None, [user.first_name, user.last_name])) or \
        (f"@{user.username}" if user.username else "")
    if not display:
        return
    names = load_user_names()
    if names.get(str(user.id)) == display:
        return
    names[str(user.id)] = display
    save_user_names(names)


def allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        _record_user_name(update)
        return True
    is_allowed = update.effective_user.id in ALLOWED_IDS
    if is_allowed:
        _record_user_name(update)
    return is_allowed


async def deny(update: Update):
    await update.message.reply_text("⛔ You are not authorised to use this bot.")


# ──────────────────────────────────────────────────────────
# Clear Chat message tracking
# ──────────────────────────────────────────────────────────
# Telegram bots can only delete messages they sent themselves, and only
# within 48h — clear_chat.py's 🧹 Clear Chat button and daily auto-clear
# job need to know which message_ids this bot has sent to which chat.
# install_message_tracking() wraps ExtBot's three send methods once at
# startup so every outgoing message across every feature module (all
# ~20 of them go through these same three underlying calls via
# reply_text/reply_photo/send_message/etc.) is recorded here
# automatically, with zero per-call-site wiring — same reasoning as
# _record_user_name piggybacking on allowed() above.
_CHAT_MSG_MAX_AGE_HOURS = 48
_CHAT_MSG_MAX_PER_CHAT  = 1000


def load_chat_messages() -> Dict[str, List[Dict]]:
    """{str(chat_id): [{"message_id": int, "sent_at": iso}, ...]} — every
    bot-sent message still within Telegram's 48h delete window."""
    try:
        with open(SAVED_CHAT_MESSAGES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_chat_messages(data: Dict[str, List[Dict]]) -> None:
    _atomic_json_write(SAVED_CHAT_MESSAGES_FILE, data, indent=2)


def _prune_chat_messages(items: List[Dict]) -> List[Dict]:
    """Drop entries older than Telegram's 48h delete window (they can
    never be deleted anyway) and cap the remainder to the most recent
    _CHAT_MSG_MAX_PER_CHAT, so the store can't grow unbounded on a busy day."""
    cutoff = datetime.now() - timedelta(hours=_CHAT_MSG_MAX_AGE_HOURS)
    kept = []
    for m in items:
        try:
            if datetime.fromisoformat(m["sent_at"]) > cutoff:
                kept.append(m)
        except (KeyError, ValueError):
            continue
    return kept[-_CHAT_MSG_MAX_PER_CHAT:]


def _record_sent_message(chat_id, message_id: int) -> None:
    """Append one outgoing message to its chat's tracked list, pruning
    stale/overflow entries on the same write. Called from every wrapped
    send method installed by install_message_tracking()."""
    data = load_chat_messages()
    key = str(chat_id)
    items = data.get(key, [])
    items.append({"message_id": message_id, "sent_at": datetime.now().isoformat(timespec="seconds")})
    data[key] = _prune_chat_messages(items)
    save_chat_messages(data)


class _MessageTrackingExtBot(ExtBot):
    """ExtBot with send_message/send_photo/send_document overridden to
    record every outgoing message for clear_chat.py. install_message_
    tracking() swaps an already-built ExtBot instance's __class__ to this
    (safe here since ExtBot instances carry a real __dict__, unlike a
    plain setattr on the instance — PTB's TelegramObject.__setattr__
    deliberately rejects that for any name that isn't an existing
    attribute, to catch typos) rather than constructing a new bot, so the
    existing instance's connection pool/token/request config from
    bot.py's builder chain is left untouched."""

    async def send_message(self, *args, **kwargs):
        msg = await super().send_message(*args, **kwargs)
        _try_record_sent_message(msg)
        return msg

    async def send_photo(self, *args, **kwargs):
        msg = await super().send_photo(*args, **kwargs)
        _try_record_sent_message(msg)
        return msg

    async def send_document(self, *args, **kwargs):
        msg = await super().send_document(*args, **kwargs)
        _try_record_sent_message(msg)
        return msg


def _try_record_sent_message(msg) -> None:
    """_record_sent_message, but never lets a tracking failure break the
    actual send it's piggybacking on."""
    try:
        _record_sent_message(msg.chat_id, msg.message_id)
    except Exception as e:
        logger.warning("Clear Chat: failed to record sent message: %s", e)


def install_message_tracking(app: Application) -> None:
    """Retarget this app's already-built bot instance onto
    _MessageTrackingExtBot so every outgoing message this bot ever sends
    (all ~20 feature modules go through these same three underlying Bot
    calls via reply_text/reply_photo/send_message/etc.) is recorded for
    clear_chat.py, without touching any individual feature module. Call
    once from bot.py's main(), right after the Application is built."""
    app.bot.__class__ = _MessageTrackingExtBot


# ──────────────────────────────────────────────────────────
# /cancel & fallback (shared across every conversation)
# ──────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("session", None)
    await update.message.reply_text(
        "🛑 Flow cancelled.",
        reply_markup=_main_menu_for(update.effective_user.id),
    )
    return ConversationHandler.END


async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤔 I didn't understand that. Follow the steps above, or tap 🛑 Cancel to abort.",
        reply_markup=_main_menu_for(update.effective_user.id),
    )


# ──────────────────────────────────────────────────────────
# Category menu navigation (tapping a category / ⬅ Back)
# ──────────────────────────────────────────────────────────

# Matches any of the six category button texts — the entry filter for
# recv_menu_category, registered once in bot.py's main() rather than one
# handler per category.
_MENU_CATEGORY_FILTER = filters.Regex(
    f"^({'|'.join(re.escape(b) for b in _MENU_CATEGORIES)})$"
)


async def recv_menu_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Show the tapped category's description and open its submenu — but
    # only if this user is actually permitted to open it (category_allowed);
    # this is the one enforcement point a restricted user can't bypass by
    # tapping a category button directly, even if it were somehow shown to
    # them. ⚙️ Bot Settings' submenu additionally gets 🔐 Manage Access
    # appended, but only for admins.
    if not allowed(update): return await deny(update)
    text = update.message.text
    category = _MENU_CATEGORIES.get(text)
    if not category:
        return
    user_id = update.effective_user.id
    if not category_allowed(user_id, text):
        await update.message.reply_text(
            "⛔ You don't have access to this category. Ask an admin to grant it "
            "via 🔐 Manage Access.",
            reply_markup=_main_menu_for(user_id),
        )
        return
    buttons = list(category["buttons"])
    if text == BTN_CAT_SETTINGS and is_admin(user_id):
        buttons.append(BTN_MANAGE_ACCESS)
    await update.message.reply_text(
        category["description"],
        parse_mode="Markdown",
        reply_markup=_category_menu(buttons),
    )


async def recv_menu_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Return from a category submenu to the top-level category menu.
    if not allowed(update): return await deny(update)
    await update.message.reply_text("Main menu:", reply_markup=_main_menu_for(update.effective_user.id))


# (_send_bulk_export_email / _send_auto_fetch_email live in email_service.py)
