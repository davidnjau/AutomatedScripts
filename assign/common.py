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
import uuid
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

# Allowed users who must never receive any pushed report/notification (e.g. an
# inquiry-only user who just uses a lookup menu). They can still use the bot
# normally; they're only skipped by every broadcast loop — see
# report_recipients(). A subset of ALLOWED_IDS, maintained in its own env var.
NO_REPORT_IDS = set(
    int(x.strip())
    for x in os.getenv("REPORT_EXCLUDED_TELEGRAM_IDS", "").split(",")
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
SAVED_BUTTON_ACCESS_FILE    = os.path.join(DATA_DIR, "saved_button_access.json")
SAVED_USER_NAMES_FILE       = os.path.join(DATA_DIR, "saved_user_names.json")
SAVED_CHAT_MESSAGES_FILE    = os.path.join(DATA_DIR, "saved_chat_messages.json")

# base64('{"active_role":"DLV"}') — required cparams header for DLV task endpoints
CPARAMS_DLV          = base64.b64encode(b'{"active_role":"DLV"}').decode()
CPARAMS_ASSESSOR     = base64.b64encode(b'{"active_role":"ASSESSOR_OF_STAMP_DUTY"}').decode()
CPARAMS_VALUER_ROLE  = base64.b64encode(b'{"active_role":"VALUER"}').decode()
CPARAMS_SUPPORT      = base64.b64encode(b'{"active_role":"SUPPORT"}').decode()


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


# Serialises every load-modify-save cycle against the consolidated
# saved_dlv_records.json store (dlv_core._load_consolidated/_save_consolidated
# and everything built on them — dlv_core.save_dlv_batch/save_dlv_closed/
# mark_removed/clear_hold_and_remove, hold_tasks.save_hold_tasks, and
# persist_assignment below). Without this, two concurrent read-modify-write
# cycles on the same file (e.g. dlv_batch_job and hold_tasks_job, both
# running on their own 1-minute repeating jobs) can race: both load the same
# snapshot, each mutates a different ref, and whichever saves last silently
# clobbers the other's update — including a ref that was just flagged
# awaiting_decision (DLV Batch's "taken by another valuer" conflict), which
# can disappear before its Reassign/Maintain/Delete prompt ever reaches a
# user. dlv_core.py and hold_tasks.py import this directly (a safe
# direction — both already import other names from common.py at module
# level) rather than each defining their own lock, so every writer
# serializes against the same one.
_DLV_STORE_LOCK = threading.Lock()


def _atomic_json_write(path: str, data, **dump_kwargs) -> None:
    """Write data as JSON to path atomically (write to a per-call-unique
    .tmp file, then os.replace over the real path). The tmp filename
    includes the PID + thread id + a random suffix rather than a fixed
    f"{path}.tmp" — two concurrent writers to the same path (e.g. a file
    with no _DLV_STORE_LOCK-style caller-side guard) used to collide on
    that one shared tmp filename: whichever thread's os.replace ran first
    renamed the file out from under the other, which then crashed on its
    own os.replace with FileNotFoundError, aborting whatever caller was
    mid-write — this is exactly what happened to dlv_batch_job once,
    killing that cycle before it could send any pending Reassign/Maintain
    notifications. A unique tmp path per call makes concurrent writers to
    the same file independent at this layer; os.remove on failure cleans
    up this call's own tmp file rather than leaving it behind."""
    _ensure_data_dir()
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, **dump_kwargs)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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
    with _DLV_STORE_LOCK:
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

# Old category labels → their new label after the menu reorg. Saved grants
# reference a category by its label string, so a renamed/folded category (e.g.
# "🧭 Inquiry" folded into Search & Lookup) would silently drop from a user's
# grants unless migrated. Applied once on first load, then persisted back.
# Unchanged labels (🤖 Automation, 📥 Post Board) need no entry.
_CATEGORY_LABEL_MIGRATIONS = {
    "📋 Assignments":  "📋 Tasks & Assignment",
    "📈 Analytics":    "📈 Reports & Analytics",
    "🔎 Look Ups":     "🔎 Search & Lookup",
    "🧭 Inquiry":      "🔎 Search & Lookup",
    "👥 Valuers":      "👥 Valuers & Access",
    "⚙️ Bot Settings": "📘 General",
}


def _migrate_category_labels(data: Dict[str, List[str]]):
    """Remap each user's saved category labels through
    _CATEGORY_LABEL_MIGRATIONS, de-duplicating while preserving order.
    Returns (migrated_data, changed) — changed is True if any label moved,
    so the caller can persist the migration once."""
    changed = False
    out: Dict[str, List[str]] = {}
    for uid, labels in data.items():
        seen: List[str] = []
        for label in labels:
            new = _CATEGORY_LABEL_MIGRATIONS.get(label, label)
            if new != label:
                changed = True
            if new not in seen:
                seen.append(new)
        out[uid] = seen
    return out, changed


def load_category_access() -> Dict[str, List[str]]:
    """{str(user_id): [category_label, ...]} — every non-admin user's
    explicitly granted menu categories. A user with no entry here has
    zero categories — locked out by default until an admin grants some
    (see get_user_categories's admin bypass and its no-admin-configured
    safety fallback below). Old category labels are migrated to their new
    names (and persisted once) on load, so a menu reorg never silently
    revokes a grant."""
    try:
        with open(SAVED_CATEGORY_ACCESS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    migrated, changed = _migrate_category_labels(data)
    if changed:
        save_category_access(migrated)
    return migrated


def save_category_access(data: Dict[str, List[str]]) -> None:
    _atomic_json_write(SAVED_CATEGORY_ACCESS_FILE, data, indent=2)


def is_admin(user_id: int) -> bool:
    """True if user_id is configured as an admin (ADMIN_TELEGRAM_IDS)."""
    return user_id in ADMIN_IDS


def admin_recipients() -> List[int]:
    """Telegram IDs that should receive an admin-only broadcast (e.g. the
    Morning Briefing, the DLV Batch auto report's full version): the
    configured admins. Safety fallback — identical to get_user_categories —
    if no admin is configured at all (ADMIN_IDS empty), there is no
    restriction to enforce, so every allowed user receives it."""
    if not ADMIN_IDS:
        return list(ALLOWED_IDS)
    return list(ADMIN_IDS)


def report_recipients() -> List[int]:
    """Allowed users who should receive a broadcast report/notification —
    every allowed user except those in NO_REPORT_IDS. Used by every recurring
    report's send loop (Auto Fetch, stale check, hold tasks, incremental
    notify, token daemon) so an inquiry-only user never gets pushed reports."""
    return [uid for uid in ALLOWED_IDS if uid not in NO_REPORT_IDS]


def get_user_categories(user_id: int) -> List[str]:
    """Every menu category label user_id may open. Admins always get
    every category, regardless of what's explicitly granted to them.
    Safety fallback: if no admin is configured at all (ADMIN_IDS empty),
    there would be nobody able to grant access to anyone, ever — so in
    that case every user gets every category too, exactly like the
    pre-Manage-Access behavior. The empty-by-default restriction only
    actually applies once at least one admin exists to lift it.
    Admin-only categories (_ADMIN_ONLY_CATEGORIES, e.g. 🛠 System) are never
    returned for a non-admin even if somehow granted."""
    if not ADMIN_IDS or user_id in ADMIN_IDS:
        return list(_MENU_CATEGORIES.keys())
    granted = load_category_access().get(str(user_id), [])
    return [c for c in granted if c not in _ADMIN_ONLY_CATEGORIES]


def category_allowed(user_id: int, category: str) -> bool:
    """True if user_id may open the given category label."""
    return category in get_user_categories(user_id)


# ── Per-user, per-button access (finer than category — Manage Access) ─
#
# saved_button_access.json: {str(user_id): {category_label: [button_label, ...]}}
# A category key present here is an explicit allow-list — the user sees ONLY
# those buttons within that category. A category key ABSENT means no button
# restriction → the user sees every button in that category (the default, so
# existing users and un-customized categories are unaffected). This is a
# refinement *within* a category the user already has (get_user_categories);
# it never grants a category they don't have.

def load_button_access() -> Dict[str, Dict[str, List[str]]]:
    """{str(user_id): {category_label: [button_label, ...]}} — per-user
    button allow-lists within a category. Absent category = all buttons."""
    try:
        with open(SAVED_BUTTON_ACCESS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_button_access(data: Dict[str, Dict[str, List[str]]]) -> None:
    """Persist the per-user button allow-list map."""
    _atomic_json_write(SAVED_BUTTON_ACCESS_FILE, data, indent=2)


def get_user_category_buttons(user_id: int, category: str) -> List[str]:
    """The workflow buttons user_id may see within `category`, in display
    order. Admins (and the no-admin-configured fallback inside
    get_user_categories) always see every button. A non-admin sees every
    button too unless an explicit allow-list exists for this (user, category)
    in saved_button_access.json, in which case only the allow-listed buttons
    that still belong to the category are shown. Does not check whether the
    user has the category at all — callers gate that with category_allowed
    first (recv_menu_category does)."""
    all_buttons = list(_MENU_CATEGORIES.get(category, {}).get("buttons", []))
    if not ADMIN_IDS or user_id in ADMIN_IDS:
        return all_buttons
    allowed_for_cat = load_button_access().get(str(user_id), {}).get(category)
    if allowed_for_cat is None:
        return all_buttons
    allow = set(allowed_for_cat)
    return [b for b in all_buttons if b in allow]


def button_allowed(user_id: int, category: str, button: str) -> bool:
    """True if user_id may see `button` within `category` — menu-hiding
    symmetry with category_allowed (enforcement is display-level, same as
    categories)."""
    return button in get_user_category_buttons(user_id, category)


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
BTN_OCR_STATS     = "🧮 OCR Usage"
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
BTN_QUEUE_INTERVAL = "⏱ Queue Check Interval"
BTN_STALE_THRESHOLD = "⏳ Stale Pending Threshold"
BTN_BULK_FIELD_UPDATE = "🛠 Bulk Field Update"
BTN_BULK_FIELD_UPDATE_HISTORY = "📜 Bulk Update History"
BTN_SMS_SENDER    = "📱 Send SMS"
BTN_QA_SD         = "⚡ SD Assignment"
BTN_QA_LRENT      = "⚡ LRent Assignment"
BTN_VIEW_REFS     = "📑 View References"

# Category buttons — the main menu shows only these six (plus Cancel);
# tapping one opens that category's own submenu of workflow buttons (see
# _MENU_CATEGORIES/_category_menu/_main_menu_for below). BTN_BACK returns
# from a submenu to the main category menu.
# (Constant names are kept stable across the menu reorg even where a label
# changed — e.g. BTN_CAT_SETTINGS now reads "🛠 System" — so existing imports
# keep working; saved grants of the old labels are migrated on load, see
# _migrate_category_labels.)
BTN_CAT_ASSIGNMENTS = "📋 Tasks & Assignment"
BTN_CAT_BULK        = "📦 Bulk Operations"
BTN_CAT_LOOKUPS     = "🔎 Search & Lookup"
BTN_CAT_AUTOMATION  = "🤖 Automation"
BTN_CAT_ANALYTICS   = "📈 Reports & Analytics"
BTN_CAT_VALUERS     = "👥 Valuers & Access"
BTN_CAT_COMM        = "💬 Communication"
BTN_CAT_SETTINGS    = "🛠 System"          # admin-only (see _ADMIN_ONLY_CATEGORIES)
BTN_CAT_GENERAL     = "📘 General"
BTN_CAT_POST_BOARD  = "📥 Post Board"      # hidden (see _MENU_HIDDEN_CATEGORIES)
BTN_BACK            = "⬅ Back"

# Admin-only action appended to 👥 Valuers & Access' submenu (only for admins,
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
    f"|{re.escape(BTN_QA_SD)}|{re.escape(BTN_QA_LRENT)}|{re.escape(BTN_VIEW_REFS)}"
    f"|{re.escape(BTN_DLV_TASKS)}|{re.escape(BTN_BRIEFING)}|{re.escape(BTN_SECTIONAL)}"
    f"|{re.escape(BTN_APARTMENTS)}"
    f"|{re.escape(BTN_HOLD_TASKS)}|{re.escape(BTN_INCREMENTAL)}|{re.escape(BTN_DLV_REPORT_SCHEDULE)}"
    f"|{re.escape(BTN_CUSTOM_EXCLUSIONS)}|{re.escape(BTN_TASK_ANALYTICS)}|{re.escape(BTN_CLEAR_CHAT)}"
    f"|{re.escape(BTN_QUEUE_INTERVAL)}|{re.escape(BTN_STALE_THRESHOLD)}|{re.escape(BTN_SMS_SENDER)}"
    f"|{re.escape(BTN_OCR_STATS)}"
    f"|{re.escape(BTN_CAT_ASSIGNMENTS)}|{re.escape(BTN_CAT_BULK)}|{re.escape(BTN_CAT_AUTOMATION)}"
    f"|{re.escape(BTN_CAT_ANALYTICS)}|{re.escape(BTN_CAT_LOOKUPS)}|{re.escape(BTN_CAT_VALUERS)}"
    f"|{re.escape(BTN_CAT_COMM)}|{re.escape(BTN_CAT_SETTINGS)}|{re.escape(BTN_CAT_GENERAL)}"
    f"|{re.escape(BTN_CAT_POST_BOARD)}|{re.escape(BTN_PB_POST)}|{re.escape(BTN_PB_VIEW)}"
    f"|{re.escape(BTN_BACK)}|{re.escape(BTN_MANAGE_ACCESS)}"
    f"|{re.escape(BTN_RESTART)}|{re.escape(BTN_HELP)}|{re.escape(BTN_CANCEL)})$"
)
_CANCEL_FILTER = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")
not_cancel = filters.TEXT & ~filters.COMMAND & ~_CANCEL_FILTER

# Each category: a short description shown when the category button is
# tapped, plus the list of workflow buttons its submenu shows (in
# display order). Apartments, Sectional, AF Results, Valuer Tasks,
# Fetch Tasks, and the Assignments history are intentionally left out of
# every category — hidden from the menu, but each still has its own working
# /command entry point and MessageHandler. 🛠 System is admin-only
# (_ADMIN_ONLY_CATEGORIES); 📥 Post Board is grantable but hidden from the
# displayed menu (_MENU_HIDDEN_CATEGORIES).
_MENU_CATEGORIES: Dict[str, Dict[str, object]] = {
    BTN_CAT_ASSIGNMENTS: {
        "description": (
            "📋 *Tasks & Assignment*\n\n"
            "Assign and queue stamp-duty / land-rent valuation tasks to valuers, "
            "and guard assigned tasks against takeover."
        ),
        "buttons": [
            BTN_QA_SD, BTN_QA_LRENT, BTN_ASSIGN, BTN_DLV_BATCH, BTN_HOLD_TASKS,
        ],
    },
    BTN_CAT_BULK: {
        "description": (
            "📦 *Bulk Operations*\n\n"
            "Run bulk field updates across many records, and review their history."
        ),
        "buttons": [BTN_BULK_FIELD_UPDATE, BTN_BULK_FIELD_UPDATE_HISTORY],
    },
    BTN_CAT_LOOKUPS: {
        "description": (
            "🔎 *Search & Lookup*\n\n"
            "Look up a reference number's status on demand, check a parcel "
            "number, or set a standing watch on a parcel."
        ),
        "buttons": [BTN_LOOKUP, BTN_PARCEL_LOOKUP, BTN_PARCEL_WATCH],
    },
    BTN_CAT_AUTOMATION: {
        "description": (
            "🤖 *Automation*\n\n"
            "Schedule recurring auto-fetches and tune the background queue checks "
            "so tasks come to you automatically."
        ),
        "buttons": [
            BTN_AUTO_FETCH, BTN_INCREMENTAL, BTN_CUSTOM_EXCLUSIONS,
            BTN_QUEUE_INTERVAL, BTN_STALE_THRESHOLD,
        ],
    },
    BTN_CAT_ANALYTICS: {
        "description": (
            "📈 *Reports & Analytics*\n\n"
            "Export valuation reports, review DLV task reports, distribute jobs, "
            "and monitor background job progress."
        ),
        "buttons": [
            BTN_TASK_ANALYTICS, BTN_VIEW_REFS, BTN_BULK_EXPORT, BTN_EXPORT_STATUS,
            BTN_ERROR_REPORT, BTN_BRIEFING, BTN_DLV_REPORT_SCHEDULE, BTN_JOB_DIST,
            BTN_DLV_TASKS, BTN_DLV_REF_CHECK,
        ],
    },
    BTN_CAT_VALUERS: {
        "description": (
            "👥 *Valuers & Access*\n\n"
            "Manage the saved valuer list used across the bot (and, for admins, "
            "other users' menu access)."
        ),
        "buttons": [BTN_VALUERS, BTN_DELETE],
    },
    BTN_CAT_COMM: {
        "description": (
            "💬 *Communication*\n\n"
            "Send messages through the bot."
        ),
        "buttons": [BTN_SMS_SENDER],
    },
    BTN_CAT_SETTINGS: {
        "description": (
            "🛠 *System*\n\n"
            "Manage login sessions, the token refresh daemon, and the bot process "
            "itself. Admin-only."
        ),
        "buttons": [BTN_AUTH, BTN_TOKEN_STATUS, BTN_DAEMON, BTN_RESTART, BTN_OCR_STATS],
    },
    BTN_CAT_GENERAL: {
        "description": (
            "📘 *General*\n\n"
            "Help and chat housekeeping."
        ),
        "buttons": [BTN_HELP, BTN_CLEAR_CHAT],
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


# Categories kept in the permission model (so category_allowed/Manage Access
# and any category-gated behaviour like Post Board's push still work) but NOT
# shown in the top-level menu — reachable by command/exact-text only.
_MENU_HIDDEN_CATEGORIES = {BTN_CAT_POST_BOARD}

# Categories only admins may ever open. Non-admins never see them in the menu
# (get_user_categories strips them) and they're not offered in Manage Access'
# grantable list (access_control._ma_category_keyboard). Admins still get them
# via the admin bypass in get_user_categories.
_ADMIN_ONLY_CATEGORIES = {BTN_CAT_SETTINGS}


def _main_menu_for(user_id: int) -> ReplyKeyboardMarkup:
    """Top-level menu filtered to whichever categories user_id may open
    (get_user_categories) minus any in _MENU_HIDDEN_CATEGORIES — only that
    user's permitted, non-hidden category buttons. This is the only
    top-level-menu builder in the bot: every feature module's "flow finished"
    message, every /start, ⬅ Back, 🛑 Cancel, and the generic "I didn't
    understand" fallback all call this (with whichever chat/user identifier is
    in scope at that call site — this bot is private/1:1-DM-only, so chat_id ==
    user_id always). A restricted user therefore never sees a category button
    they aren't granted, at any point in the bot."""
    cats = [c for c in get_user_categories(user_id) if c not in _MENU_HIDDEN_CATEGORIES]
    return _category_row_keyboard(cats)


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
    # them. 👥 Valuers & Access' submenu additionally gets 🔐 Manage Access
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
    # Filter to the buttons this user may see within the category (admins see
    # all; a non-admin sees all unless an explicit per-button allow-list was
    # set for them via Manage Access).
    buttons = get_user_category_buttons(user_id, text)
    if text == BTN_CAT_VALUERS and is_admin(user_id):
        buttons = buttons + [BTN_MANAGE_ACCESS]
    if not buttons:
        await update.message.reply_text(
            "ℹ️ You don't have access to any items in this category yet. "
            "Ask an admin to grant some via 🔐 Manage Access.",
            reply_markup=_main_menu_for(user_id),
        )
        return
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
