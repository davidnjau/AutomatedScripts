#!/usr/bin/env python3
"""
ardhisasa_bot.py
================
Telegram bot for Ardhisasa Valuation Officer Assignment.

Conversation flow:
  /assign  (or tap "📋 New Assignment")
    → ask reference numbers
    → [saved valuers exist] pick saved valuer OR search new
      [no saved valuers]    ask valuer name to search
    → choose credential profile
      [valid cached token]  skip login entirely → jump to confirm / valuer list
      [no cached token]     trigger login (OTP sent to device)
                            → ask user to forward OTP here
                            → verify OTP & cache tokens
    → show matching valuers (inline keyboard)   [skipped when saved valuer used]
    → user selects valuer
    → confirm selection
    → run assignments
    → show results  (valuer auto-saved for future use)
"""

import base64
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import anthropic
import pytesseract
import requests
from dotenv import load_dotenv
from PIL import Image
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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
    PUBLIC_CREDENTIALS,
    STAFF_CREDENTIALS_ICT,
    STAFF_CREDENTIALS_SUPPORT,
    STAFF_CREDENTIALS_VALUER,
    AuthTokens,
    build_session,
)

# ──────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ardhisasa.bot")

load_dotenv()

BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ALLOWED_IDS = set(
    int(x.strip())
    for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",")
    if x.strip()
)

BASE_URL = "https://ardhisasa-api.lands.go.ke"

# ──────────────────────────────────────────────────────────
# Persistent storage
# ──────────────────────────────────────────────────────────
DATA_DIR           = os.path.join(os.path.dirname(__file__), "data")
SAVED_VALUERS_FILE      = os.path.join(DATA_DIR, "saved_valuers.json")
SAVED_TOKENS_FILE       = os.path.join(DATA_DIR, "saved_tokens.json")
SAVED_ASSIGNMENTS_FILE  = os.path.join(DATA_DIR, "saved_assignments.json")
SAVED_TASK_BATCHES_FILE = os.path.join(DATA_DIR, "saved_task_batches.json")
SAVED_SCHEDULES_FILE    = os.path.join(DATA_DIR, "saved_schedules.json")

# base64('{"active_role":"DLV"}') — required cparams header for DLV task endpoints
CPARAMS_DLV          = base64.b64encode(b'{"active_role":"DLV"}').decode()
CPARAMS_ASSESSOR     = base64.b64encode(b'{"active_role":"ASSESSOR_OF_STAMP_DUTY"}').decode()
CPARAMS_VALUER_ROLE  = base64.b64encode(b'{"active_role":"VALUER"}').decode()

DAEMON_SCRIPT = os.path.join(os.path.dirname(__file__), "token_refresh_daemon.py")
DAEMON_PID_FILE = os.path.join(DATA_DIR, "daemon.pid")
DAEMON_LOG_FILE = os.path.join(DATA_DIR, "daemon.log")


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


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
    with open(SAVED_VALUERS_FILE, "w") as f:
        json.dump(valuers, f, indent=2)
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
    _ensure_data_dir()
    assignments = load_saved_assignments()
    assignments[ref] = {
        "valuer_name": valuer_name,
        "valuer_uid":  valuer_uid,
        "assigned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(SAVED_ASSIGNMENTS_FILE, "w") as f:
        json.dump(assignments, f, indent=2)
    logger.info("Saved assignment %s → %s", ref, valuer_name)


# ── Tokens ────────────────────────────────────────────────

def _jwt_exp(token: str) -> Optional[float]:
    """Decode the JWT payload and return the `exp` claim, or None on failure."""
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


def persist_tokens(cred_type: str, access_token: str, jwt: str):
    _ensure_data_dir()
    tokens = _load_tokens_raw()
    exp = _jwt_exp(jwt) or (time.time() + 3600)
    tokens[cred_type] = {
        "access_token": access_token,
        "jwt":          jwt,
        "expires_at":   exp,
    }
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


# ──────────────────────────────────────────────────────────
# States
# ──────────────────────────────────────────────────────────
class S(Enum):
    INPUT_METHOD       = auto()   # choose text or photo input
    REF_NUMBERS        = auto()   # typing refs manually
    RECV_PHOTOS        = auto()   # receiving photo(s) for OCR
    CONFIRM_REFS       = auto()   # review extracted refs before proceeding
    REASSIGN_CONFIRM   = auto()   # some refs already assigned — ask what to do
    PICK_VALUER_SOURCE = auto()   # choose saved valuer or search new
    VALUER_NAME        = auto()   # enter name when searching new
    CHOOSE_CRED        = auto()
    WAIT_OTP           = auto()
    SELECT_VALUER      = auto()
    CONFIRM            = auto()


# ──────────────────────────────────────────────────────────
# Per-user session data
# ──────────────────────────────────────────────────────────
@dataclass
class Session:
    refs:             List[str] = field(default_factory=list)
    extracted_refs:   List[str] = field(default_factory=list)   # OCR-extracted refs awaiting confirmation
    already_assigned: List[Dict] = field(default_factory=list)  # [{ref, valuer_name, assigned_at}]
    valuer_name:      str = ""
    cred_type:        str = "publicuser"
    session:          Optional[requests.Session] = None
    tokens:           Optional[AuthTokens] = None
    valuers:          List[Dict] = field(default_factory=list)
    selected_idx:     Optional[int] = None
    saved_valuer:     Optional[Dict] = None   # {"name", "uid", "account_number"}


# ──────────────────────────────────────────────────────────
# States — Receive Tasks conversation
# ──────────────────────────────────────────────────────────
class RS(Enum):
    PICK_STAFF_SOURCE = auto()   # choose saved valuer or search new
    STAFF_NAME        = auto()
    SELECT_STAFF      = auto()
    CHOOSE_CRED       = auto()
    WAIT_OTP          = auto()
    TASK_TYPE         = auto()   # choose Stamp Duty vs County Stamp Duty (when staff has both)
    TASK_COUNT        = auto()
    AMOUNT_RANGE      = auto()   # shows Enter / Skip buttons
    AMOUNT_TEXT       = auto()   # text input for min-max after choosing Enter
    SCHEDULE_CHOICE   = auto()
    SCHEDULE_INTERVAL = auto()
    RT_CONFIRM        = auto()


# ──────────────────────────────────────────────────────────
# Per-user session — Receive Tasks
# ──────────────────────────────────────────────────────────
@dataclass
class RTSession:
    staff_name:               str = ""
    staff_results:            List[Dict] = field(default_factory=list)
    staff_data:               Optional[Dict] = None
    saved_valuer:             Optional[Dict] = None   # {"name", "uid", "account_number"} — pre-selected
    cred_type:                str = "publicuser"
    session:                  Optional[object] = None   # requests.Session
    tokens:                   Optional[AuthTokens] = None
    task_count:               int = 0
    amount_min:               Optional[float] = None
    amount_max:               Optional[float] = None
    task_type:                str = ""   # "STAMP_DUTY" or "COUNTY_STAMP_DUTY"
    staff_registry:           str = ""
    staff_county:             str = ""
    matched_tasks:            List[Dict] = field(default_factory=list)
    schedule_interval_minutes: Optional[int] = None


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

# ──────────────────────────────────────────────────────────
# Main menu button labels & keyboard
# ──────────────────────────────────────────────────────────
BTN_ASSIGN      = "📋 New Assignment"
BTN_RECEIVE     = "📥 Receive Tasks"
BTN_DAEMON      = "🔄 Token Daemon"
BTN_VALUERS     = "👥 Saved Valuers"
BTN_DELETE      = "🗑 Delete Valuer"
BTN_IMPL_TASKS  = "📊 Implementor Tasks"
BTN_DLV_TASKS   = "📋 DLV Tasks"
BTN_HELP        = "❓ Help"
BTN_CANCEL      = "🛑 Cancel"

# Filter that matches any of the persistent menu button texts
_MENU_BUTTON_FILTER = filters.Regex(
    f"^({re.escape(BTN_ASSIGN)}|{re.escape(BTN_RECEIVE)}|{re.escape(BTN_DAEMON)}"
    f"|{re.escape(BTN_VALUERS)}|{re.escape(BTN_DELETE)}"
    f"|{re.escape(BTN_IMPL_TASKS)}|{re.escape(BTN_DLV_TASKS)}"
    f"|{re.escape(BTN_HELP)}|{re.escape(BTN_CANCEL)})$"
)
_CANCEL_FILTER = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")


def _main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_ASSIGN),      KeyboardButton(BTN_RECEIVE)],
            [KeyboardButton(BTN_IMPL_TASKS),  KeyboardButton(BTN_DLV_TASKS)],
            [KeyboardButton(BTN_DAEMON)],
            [KeyboardButton(BTN_VALUERS),     KeyboardButton(BTN_DELETE)],
            [KeyboardButton(BTN_HELP),        KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True,
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
# Helpers
# ──────────────────────────────────────────────────────────
def get_sess(ctx: ContextTypes.DEFAULT_TYPE) -> Session:
    if "session" not in ctx.user_data:
        ctx.user_data["session"] = Session()
    return ctx.user_data["session"]


def parse_refs(raw: str) -> List[str]:
    return [r.strip() for r in re.split(r"[\n,]+", raw) if r.strip()]


def _cred_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(CRED_LABELS["publicuser"],   callback_data="cred:publicuser")],
        [InlineKeyboardButton(CRED_LABELS["staff"],        callback_data="cred:staff")],
        [InlineKeyboardButton(CRED_LABELS["staff2"],       callback_data="cred:staff2")],
        [InlineKeyboardButton(CRED_LABELS["staff_valuer"], callback_data="cred:staff_valuer")],
    ])


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm & Assign", callback_data="confirm:yes"),
        InlineKeyboardButton("❌ Cancel",           callback_data="confirm:no"),
    ]])


async def _do_valuer_search(message, sess: Session) -> Optional[List[Dict]]:
    """Search for valuers; returns list or None (error already sent)."""
    try:
        headers = {
            "Authorization": f"Bearer {sess.tokens.access_token}",
            "JWTAUTH":       f"Bearer {sess.tokens.jwt}",
        }
        params = {
            "account_type": "STAFF",
            "filter_type":  "ACTIVE",
            "page":         1,
            "search":       sess.valuer_name,
        }
        resp = sess.session.get(
            f"{BASE_URL}/acl/api/v1/accounts/list-user-accounts",
            headers=headers, params=params, timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        await message.reply_text(f"❌ Valuer search failed: `{e}`", parse_mode="Markdown")
        return None


async def _show_valuer_keyboard(message, sess: Session, results: List[Dict]) -> int:
    sess.valuers = results
    rows = []
    for i, v in enumerate(results):
        sd   = v.get("staff_details", {})
        name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
        rows.append([InlineKeyboardButton(name or f"Valuer {i+1}", callback_data=f"valuer:{i}")])
    await message.reply_text(
        f"Found *{len(results)}* valuer(s). Select one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return S.SELECT_VALUER


# ──────────────────────────────────────────────────────────
# OCR helpers
# ──────────────────────────────────────────────────────────

# Matches patterns like LS/VAL/2024/001 — 3+ slash-separated alphanumeric segments
_REF_RE = re.compile(r'\b[A-Z0-9]{2,}(?:/[A-Z0-9]{2,}){2,}\b')


def _extract_refs_from_text(text: str) -> List[str]:
    return list(dict.fromkeys(_REF_RE.findall(text.upper())))  # dedup, preserve order


async def ocr_extract_refs(photo_bytes: bytes) -> Tuple[List[str], str]:
    """
    Try Tesseract first; fall back to Claude Vision if nothing found.
    Returns (refs, source) where source is "tesseract" or "claude".
    """
    # ── Tesseract ─────────────────────────────────────────
    try:
        img  = Image.open(io.BytesIO(photo_bytes))
        text = pytesseract.image_to_string(img)
        refs = _extract_refs_from_text(text)
        if refs:
            return refs, "tesseract"
    except Exception as e:
        logger.warning("Tesseract failed: %s", e)

    # ── Claude Vision fallback ────────────────────────────
    if not ANTHROPIC_API_KEY:
        return [], "none"
    try:
        client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        b64_img = base64.standard_b64encode(photo_bytes).decode()
        resp    = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_img},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Extract every reference number from this document image. "
                            "Reference numbers follow a pattern like LS/VAL/2024/001 — "
                            "alphanumeric segments separated by forward slashes. "
                            "Return ONLY the reference numbers, one per line, nothing else."
                        ),
                    },
                ],
            }],
        )
        text = resp.content[0].text
        refs = _extract_refs_from_text(text)
        return refs, "claude"
    except Exception as e:
        logger.warning("Claude Vision failed: %s", e)
        return [], "none"


# ──────────────────────────────────────────────────────────
# /start  /help
# ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        "🏛 *Ardhisasa Valuation Bot*\n\n"
        "Use the buttons below to get started:",
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


# ──────────────────────────────────────────────────────────
# /valuers  /delete_valuer — manage saved valuers
# ──────────────────────────────────────────────────────────
async def cmd_valuers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    valuers = load_saved_valuers()
    if not valuers:
        await update.message.reply_text(
            "📭 No saved valuers yet.\n"
            "They are saved automatically after a successful assignment.",
            reply_markup=_main_menu(),
        )
        return
    lines = [
        f"{i+1}. *{v['name']}* — ID: `{v['uid']}` | Acct: `{v['account_number']}`"
        for i, v in enumerate(valuers)
    ]
    await update.message.reply_text(
        "📋 *Saved Valuers:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


async def cmd_delete_valuer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    valuers = load_saved_valuers()
    if not valuers:
        await update.message.reply_text(
            "📭 No saved valuers to delete.",
            reply_markup=_main_menu(),
        )
        return
    rows = [
        [InlineKeyboardButton(f"🗑 {v['name']}", callback_data=f"del:{i}")]
        for i, v in enumerate(valuers)
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="del:cancel")])
    await update.message.reply_text(
        "Select a valuer to delete:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def recv_delete_valuer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")[1]
    if data == "cancel":
        await query.edit_message_text("Deletion cancelled.")
        return
    idx = int(data)
    valuers = load_saved_valuers()
    if idx >= len(valuers):
        await query.edit_message_text("⚠️ Valuer not found.")
        return
    removed = valuers.pop(idx)
    _ensure_data_dir()
    with open(SAVED_VALUERS_FILE, "w") as f:
        json.dump(valuers, f, indent=2)
    await query.edit_message_text(
        f"🗑 Removed *{removed['name']}* from saved valuers.", parse_mode="Markdown"
    )


# ──────────────────────────────────────────────────────────
# Step 1 — /assign → ask reference numbers
# ──────────────────────────────────────────────────────────
async def cmd_assign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    ctx.user_data["session"] = Session()
    await update.message.reply_text(
        "📋 *New Assignment Flow*\n\n"
        "Step 1 — How would you like to provide the reference numbers?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "Choose an input method:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Type Reference Numbers", callback_data="input:text")],
            [InlineKeyboardButton("📷 Add Photo(s)",           callback_data="input:photo")],
        ]),
    )
    return S.INPUT_METHOD


# ──────────────────────────────────────────────────────────
# Step 1a — input method chosen
# ──────────────────────────────────────────────────────────
async def recv_input_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data.split(":")[1]

    if method == "text":
        await query.edit_message_text(
            "✏️ Enter *reference numbers*:\n"
            "_Comma-separated or one per line, e.g._\n"
            "`LS/VAL/2024/001, LS/VAL/2024/002`",
            parse_mode="Markdown",
        )
        return S.REF_NUMBERS
    else:
        await query.edit_message_text(
            "📷 Send a photo of the document.\n"
            "_You can send multiple photos one by one._\n\n"
            "Tap *✅ Done — Review refs* when finished.",
            parse_mode="Markdown",
        )
        return S.RECV_PHOTOS


# ──────────────────────────────────────────────────────────
# Step 1b — receive photo(s) → OCR → accumulate refs
# ──────────────────────────────────────────────────────────
async def recv_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sess = get_sess(ctx)
    await update.message.reply_text("🔍 Processing photo…")

    photo_file = await update.message.photo[-1].get_file()   # largest size
    photo_bytes = await photo_file.download_as_bytearray()

    refs, source = await ocr_extract_refs(bytes(photo_bytes))

    if not refs:
        await update.message.reply_text(
            "⚠️ Could not extract any reference numbers from this photo.\n"
            "Try a clearer image, or send another photo.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done — Review refs", callback_data="photo:done"),
            ]]) if sess.extracted_refs else None,
        )
        return S.RECV_PHOTOS

    # Merge, avoiding duplicates
    new_refs = [r for r in refs if r not in sess.extracted_refs]
    sess.extracted_refs.extend(new_refs)

    source_label = "Tesseract OCR" if source == "tesseract" else "Claude Vision"
    running = "\n".join(f"  • `{r}`" for r in sess.extracted_refs)
    await update.message.reply_text(
        f"✅ *{len(new_refs)} new ref(s) found* via {source_label}.\n\n"
        f"*Running total ({len(sess.extracted_refs)}):*\n{running}\n\n"
        "_Send another photo or tap Done._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Done — Review refs", callback_data="photo:done"),
        ]]),
    )
    return S.RECV_PHOTOS


async def recv_photo_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sess = get_sess(ctx)

    if not sess.extracted_refs:
        await query.edit_message_text(
            "⚠️ No reference numbers extracted yet. Send at least one photo."
        )
        return S.RECV_PHOTOS

    refs_list = "\n".join(f"  • `{r}`" for r in sess.extracted_refs)
    await query.edit_message_text(
        f"📋 *Extracted Reference Numbers ({len(sess.extracted_refs)}):*\n\n"
        f"{refs_list}\n\n"
        "Are these correct?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm & Proceed", callback_data="refs:confirm")],
            [InlineKeyboardButton("✏️ Edit (retype manually)", callback_data="refs:edit")],
            [InlineKeyboardButton("❌ Cancel",              callback_data="refs:cancel")],
        ]),
    )
    return S.CONFIRM_REFS


# ──────────────────────────────────────────────────────────
# Step 1c — confirm extracted refs
# ──────────────────────────────────────────────────────────
async def recv_confirm_refs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sess   = get_sess(ctx)
    choice = query.data.split(":")[1]

    if choice == "cancel":
        await query.edit_message_text("❌ Assignment cancelled.")
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())
        return ConversationHandler.END

    if choice == "edit":
        await query.edit_message_text(
            "✏️ Enter the *correct reference numbers*:\n"
            "_Comma-separated or one per line._",
            parse_mode="Markdown",
        )
        return S.REF_NUMBERS

    # confirm — treat extracted refs as the final list
    sess.refs = sess.extracted_refs[:]
    await query.edit_message_text(
        f"✅ *{len(sess.refs)} reference(s) confirmed.*",
        parse_mode="Markdown",
    )
    return await _check_assignments_and_proceed(query.message, sess)


# ──────────────────────────────────────────────────────────
# Step 2 — receive refs (typed) → check existing assignments
# ──────────────────────────────────────────────────────────
async def recv_refs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sess = get_sess(ctx)
    refs = parse_refs(update.message.text)
    if not refs:
        await update.message.reply_text("⚠️ No valid references found. Try again.")
        return S.REF_NUMBERS

    sess.refs = refs
    return await _check_assignments_and_proceed(update.message, sess)


async def _check_assignments_and_proceed(message, sess: Session) -> int:
    """Check refs against saved assignments, then route accordingly."""
    refs     = sess.refs
    existing = load_saved_assignments()
    already  = [
        {"ref": r, "valuer_name": existing[r]["valuer_name"], "assigned_at": existing[r]["assigned_at"]}
        for r in refs if r in existing
    ]
    new_refs = [r for r in refs if r not in existing]

    if already:
        sess.already_assigned = already

        already_lines = "\n".join(
            f"  • `{a['ref']}` → *{a['valuer_name']}* _(on {a['assigned_at']})_"
            for a in already
        )
        new_lines = ("\n".join(f"  • `{r}`" for r in new_refs)) if new_refs else "_None_"

        await message.reply_text(
            f"⚠️ *Some references are already assigned:*\n{already_lines}\n\n"
            f"*New (unassigned):*\n{new_lines}\n\n"
            "What would you like to do?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Reassign existing + assign new", callback_data="reassign:all")],
                [InlineKeyboardButton("⏭ Skip existing, assign new only",  callback_data="reassign:skip")],
                [InlineKeyboardButton("❌ Cancel",                          callback_data="reassign:cancel")],
            ]),
        )
        return S.REASSIGN_CONFIRM

    return await _proceed_to_valuer_pick(message, sess, refs)


async def _proceed_to_valuer_pick(message, sess: Session, refs: List[str]) -> int:
    """Show valuer picker after refs are finalised."""
    bullet_list = "\n".join(f"  • `{r}`" for r in refs)
    saved = load_saved_valuers()

    if saved:
        rows = [
            [InlineKeyboardButton(f"👤 {sv['name']}", callback_data=f"src:{i}")]
            for i, sv in enumerate(saved)
        ]
        rows.append([InlineKeyboardButton("🔍 Search new valuer", callback_data="src:new")])
        await message.reply_text(
            f"✅ *{len(refs)} reference(s)* queued:\n{bullet_list}\n\n"
            "Step 2 — Select a saved valuer or search for a new one:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return S.PICK_VALUER_SOURCE
    else:
        await message.reply_text(
            f"✅ *{len(refs)} reference(s)* queued:\n{bullet_list}\n\n"
            "Step 2 — Enter the *valuer name* to search:\n"
            "_Partial names work, e.g._ `JOHN KAMAU`",
            parse_mode="Markdown",
        )
        return S.VALUER_NAME


# ──────────────────────────────────────────────────────────
# Step 2a — handle reassign choice
# ──────────────────────────────────────────────────────────
async def recv_reassign_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    choice = query.data.split(":")[1]
    sess   = get_sess(ctx)

    if choice == "cancel":
        await query.edit_message_text("❌ Assignment cancelled.")
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())
        return ConversationHandler.END

    if choice == "skip":
        new_refs = [r for r in sess.refs if r not in {a["ref"] for a in sess.already_assigned}]
        if not new_refs:
            await query.edit_message_text(
                "ℹ️ All references are already assigned. Nothing to do."
            )
            await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())
            return ConversationHandler.END
        sess.refs = new_refs
        await query.edit_message_text(
            f"⏭ Skipping already-assigned refs.\n"
            f"Proceeding with *{len(new_refs)}* new reference(s).",
            parse_mode="Markdown",
        )
    else:  # "all"
        await query.edit_message_text(
            f"🔄 Reassigning all *{len(sess.refs)}* reference(s).",
            parse_mode="Markdown",
        )

    return await _proceed_to_valuer_pick(query.message, sess, sess.refs)


# ──────────────────────────────────────────────────────────
# Step 2b — pick saved valuer or "search new"
# ──────────────────────────────────────────────────────────
async def recv_valuer_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    sess = get_sess(ctx)
    data = query.data.split(":")[1]

    if data == "new":
        await query.edit_message_text(
            "Step 2 — Enter the *valuer name* to search:\n"
            "_Partial names work, e.g._ `JOHN KAMAU`",
            parse_mode="Markdown",
        )
        return S.VALUER_NAME
    else:
        saved = load_saved_valuers()
        sv = saved[int(data)]
        sess.saved_valuer = sv
        await query.edit_message_text(
            f"✅ Valuer: *{sv['name']}*\n\n"
            "Step 3 — Choose *credential profile*:",
            parse_mode="Markdown",
            reply_markup=_cred_keyboard(),
        )
        return S.CHOOSE_CRED


# ──────────────────────────────────────────────────────────
# Step 2c — receive valuer name (search new path)
# ──────────────────────────────────────────────────────────
async def recv_valuer_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sess = get_sess(ctx)
    sess.valuer_name = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Searching for: *{sess.valuer_name}*\n\n"
        "Step 3 — Choose *credential profile*:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return S.CHOOSE_CRED


# ──────────────────────────────────────────────────────────
# Step 3 — credential chosen → use cache or full login
# ──────────────────────────────────────────────────────────
async def recv_cred_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    sess = get_sess(ctx)
    cred_type = query.data.split(":")[1]
    sess.cred_type = cred_type
    creds = CRED_MAP[cred_type]

    # ── Cached tokens path ────────────────────────────────
    cached = get_valid_tokens(cred_type)
    if cached:
        sess.tokens  = cached
        sess.session = build_session()
        logger.info("Using cached tokens for %s", cred_type)

        if sess.saved_valuer:
            # Saved valuer + cached tokens → jump straight to confirm
            sv = sess.saved_valuer
            refs_list = "\n".join(f"  • `{r}`" for r in sess.refs)
            await query.edit_message_text(
                f"🔑 Cached login: *{CRED_LABELS[cred_type]}*\n\n"
                f"📋 *Assignment Summary*\n\n"
                f"*Valuer:* {sv['name']}\n"
                f"*User ID:* `{sv['uid']}`\n\n"
                f"*References ({len(sess.refs)}):*\n{refs_list}\n\n"
                f"Proceed?",
                parse_mode="Markdown",
                reply_markup=_confirm_keyboard(),
            )
            return S.CONFIRM
        else:
            # Cached tokens + new search → search valuers
            await query.edit_message_text(
                f"🔑 Cached login: *{CRED_LABELS[cred_type]}*\n\n"
                f"🔍 Searching for valuer *{sess.valuer_name}*…",
                parse_mode="Markdown",
            )
            results = await _do_valuer_search(query.message, sess)
            if results is None:
                await query.message.reply_text(
                    "Use the menu to start again.", reply_markup=_main_menu()
                )
                return ConversationHandler.END
            if not results:
                await query.message.reply_text(
                    f"⚠️ No valuers found matching *{sess.valuer_name}*.",
                    parse_mode="Markdown",
                    reply_markup=_main_menu(),
                )
                return ConversationHandler.END
            return await _show_valuer_keyboard(query.message, sess, results)

    # ── Full login path ───────────────────────────────────
    await query.edit_message_text(
        f"✅ Credential: *{CRED_LABELS[cred_type]}* (`{creds['username']}`)\n\n"
        "Step 4 — 🔐 Sending login request…",
        parse_mode="Markdown",
    )

    sess.session = build_session()
    try:
        resp = sess.session.post(
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
        "📲 OTP has been sent to the registered device.\n\n"
        "Step 4 — Please *reply with the OTP code* now:",
        parse_mode="Markdown",
    )
    return S.WAIT_OTP


# ──────────────────────────────────────────────────────────
# Step 4 — receive OTP → verify → cache tokens → search / confirm
# ──────────────────────────────────────────────────────────
async def recv_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sess  = get_sess(ctx)
    otp   = update.message.text.strip()
    creds = CRED_MAP[sess.cred_type]

    await update.message.reply_text("🔄 Verifying OTP…")

    try:
        resp = sess.session.post(
            f"{AUTH_BASE_URL}/otpverify",
            json={
                "username": creds["username"],
                "password": creds["password"],
                "otpcode":  otp,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        access_token = data.get("details", {}).get("access_token")
        jwt          = data.get("details", {}).get("jwt")
        if not access_token or not jwt:
            raise RuntimeError(f"Tokens missing. Keys: {list(data.keys())}")

        sess.tokens = AuthTokens(access_token=access_token, jwt=jwt)
        persist_tokens(sess.cred_type, access_token, jwt)   # cache for future sessions

    except Exception as e:
        await update.message.reply_text(
            f"❌ OTP verification failed: `{e}`\n\nSend the OTP again or tap 🛑 Cancel.",
            parse_mode="Markdown",
        )
        return S.WAIT_OTP

    await update.message.reply_text("✅ Authenticated!")

    if sess.saved_valuer:
        # Saved valuer selected earlier → jump to confirm
        sv = sess.saved_valuer
        refs_list = "\n".join(f"  • `{r}`" for r in sess.refs)
        await update.message.reply_text(
            f"📋 *Assignment Summary*\n\n"
            f"*Valuer:* {sv['name']}\n"
            f"*User ID:* `{sv['uid']}`\n\n"
            f"*References ({len(sess.refs)}):*\n{refs_list}\n\n"
            f"Proceed?",
            parse_mode="Markdown",
            reply_markup=_confirm_keyboard(),
        )
        return S.CONFIRM

    await update.message.reply_text(
        f"🔍 Searching for valuer *{sess.valuer_name}*…", parse_mode="Markdown"
    )
    results = await _do_valuer_search(update.message, sess)
    if results is None:
        await update.message.reply_text(
            "Use the menu to start again.", reply_markup=_main_menu()
        )
        return ConversationHandler.END
    if not results:
        await update.message.reply_text(
            f"⚠️ No valuers found matching *{sess.valuer_name}*.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END
    return await _show_valuer_keyboard(update.message, sess, results)


# ──────────────────────────────────────────────────────────
# Step 5 — valuer selected → confirm
# ──────────────────────────────────────────────────────────
async def recv_valuer_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    sess = get_sess(ctx)
    idx  = int(query.data.split(":")[1])
    sess.selected_idx = idx
    v    = sess.valuers[idx]
    sd   = v.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
    uid  = sd.get("user_id", v.get("id", "?"))

    refs_list = "\n".join(f"  • `{r}`" for r in sess.refs)
    await query.edit_message_text(
        f"📋 *Assignment Summary*\n\n"
        f"*Valuer:* {name}\n"
        f"*User ID:* `{uid}`\n\n"
        f"*References ({len(sess.refs)}):*\n{refs_list}\n\n"
        f"Proceed?",
        parse_mode="Markdown",
        reply_markup=_confirm_keyboard(),
    )
    return S.CONFIRM


# ──────────────────────────────────────────────────────────
# Step 6 — confirmed → run assignments → save valuer → show results
# ──────────────────────────────────────────────────────────
async def recv_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm:no":
        await query.edit_message_text("❌ Assignment cancelled.")
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = get_sess(ctx)

    if sess.saved_valuer:
        sv   = sess.saved_valuer
        name = sv["name"]
        uid  = sv["uid"]
        acct = sv["account_number"]
    else:
        v    = sess.valuers[sess.selected_idx]
        sd   = v.get("staff_details", {})
        uid  = sd.get("user_id", v.get("id"))
        name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
        acct = v.get("account_number", "?")

    await query.edit_message_text(
        f"⚙️ Assigning *{name}* to {len(sess.refs)} reference(s)…",
        parse_mode="Markdown",
    )

    url     = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/fix_application_details"
    headers = {
        "Authorization": f"Bearer {sess.tokens.access_token}",
        "JWTAUTH":       f"Bearer {sess.tokens.jwt}",
    }

    ok_refs, fail_refs = [], []
    result_lines = []

    for ref in sess.refs:
        try:
            r = sess.session.post(
                url, headers=headers,
                json={"reference_number": ref, "valuation_officer": uid, "node": "VALUATION_STAMP_DUTY_VALUER_REPORT"},
                timeout=30,
            )
            r.raise_for_status()
            ok_refs.append(ref)
            result_lines.append(f"✅ `{ref}`")
        except Exception as e:
            fail_refs.append(ref)
            result_lines.append(f"❌ `{ref}` — {e}")

    if ok_refs:
        persist_valuer(name, uid, acct)          # auto-save valuer for future assignments
        for ref in ok_refs:
            persist_assignment(ref, name, uid)   # record ref → valuer mapping

    summary = (
        f"🏁 *Assignment Complete*\n\n"
        f"*Valuer:* {name}\n"
        f"*Success:* {len(ok_refs)} / {len(sess.refs)}\n"
        f"*Failed:*  {len(fail_refs)} / {len(sess.refs)}\n\n"
        + "\n".join(result_lines)
    )

    if len(summary) > 4000:
        summary = summary[:4000] + "\n…_(truncated)_"

    await query.message.reply_text(summary, parse_mode="Markdown", reply_markup=_main_menu())

    if fail_refs:
        await query.message.reply_text(
            "⚠️ Some assignments failed. Tap *📋 New Assignment* to retry failed refs.",
            parse_mode="Markdown",
        )

    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Receive Tasks — persistence helpers
# ──────────────────────────────────────────────────────────

def load_task_batches() -> List[Dict]:
    try:
        with open(SAVED_TASK_BATCHES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def persist_task_batch(batch: Dict):
    _ensure_data_dir()
    batches = load_task_batches()
    batches.append(batch)
    with open(SAVED_TASK_BATCHES_FILE, "w") as f:
        json.dump(batches, f, indent=2)
    logger.info("Saved task batch %s (%d tasks)", batch["batch_id"], len(batch["tasks"]))


def load_schedules() -> List[Dict]:
    try:
        with open(SAVED_SCHEDULES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_schedules(schedules: List[Dict]):
    _ensure_data_dir()
    with open(SAVED_SCHEDULES_FILE, "w") as f:
        json.dump(schedules, f, indent=2)


def persist_schedule(sched: Dict):
    schedules = load_schedules()
    schedules = [s for s in schedules if s["schedule_id"] != sched["schedule_id"]]
    schedules.append(sched)
    _save_schedules(schedules)
    logger.info("Saved schedule %s (%dmin interval)", sched["schedule_id"], sched["interval_minutes"])


# ──────────────────────────────────────────────────────────
# Receive Tasks — staff validation
# ──────────────────────────────────────────────────────────

def _validate_staff(user_data: Dict) -> Tuple[bool, str, str, str, str]:
    """
    Check eligibility for task receipt.
    All detail fields live inside staff_details, not at the top level.
    Returns (ok, error_msg, task_type, staff_registry, staff_county).
    task_type is "STAMP_DUTY", "COUNTY_STAMP_DUTY", or "BOTH" (user must choose).
    registry/county are populated only for COUNTY_STAMP_DUTY / BOTH.
    """
    if user_data.get("account_status", "").upper() != "ACTIVE":
        return False, "Account status is not ACTIVE.", "", "", ""

    sd = user_data.get("staff_details") or {}

    dept_code = (
        sd.get("department_details", {})
          .get("department", {})
          .get("code", "")
    )
    if dept_code.upper() != "DLV":
        return False, f"Department is '{dept_code}', expected DLV.", "", "", ""

    roles      = sd.get("roles") or []
    role_names = [r.get("rolename", "").upper() for r in roles]
    if "VALUER" not in role_names:
        return False, f"No VALUER role found. Roles: {role_names}", "", "", ""

    has_county_valuer = "COUNTY_VALUER" in role_names

    if has_county_valuer:
        county_units = sd.get("county_units") or []
        if not county_units:
            return False, "COUNTY_VALUER role but no county_units found on account.", "", "", ""
        primary  = next((u for u in county_units if u.get("is_primary")), county_units[0])
        registry = primary.get("registry", "").upper()
        county   = primary.get("county", "").upper()
        # Has both VALUER and COUNTY_VALUER — let the user pick the task pool
        return True, "", "BOTH", registry, county

    return True, "", "STAMP_DUTY", "", ""


# ──────────────────────────────────────────────────────────
# Receive Tasks — API helpers
# ──────────────────────────────────────────────────────────

def _rt_auth_headers(rt: RTSession) -> Dict:
    return {
        "Authorization": f"Bearer {rt.tokens.access_token}",
        "JWTAUTH":       f"Bearer {rt.tokens.jwt}",
        "cparams":       CPARAMS_DLV,
    }


def _fetch_tasks(rt: RTSession, needed: int) -> List[Dict]:
    """
    Paginate the task list endpoint, returning tasks that pass the
    node/status pre-filter (and county+registry filter for COUNTY_STAMP_DUTY).
    Stops once we have needed*5 candidates (to leave headroom for detail filtering).
    """
    headers = _rt_auth_headers(rt)
    if rt.task_type == "STAMP_DUTY":
        base_params: Dict = {
            "filter": "Pending", "role": "DLV",
            "request_type": "STAMP_DUTY", "search": "",
        }
    else:
        base_params = {
            "filter": "Ongoing", "from_ardhipay": "true",
            "role": "DLV", "request_type": "COUNTY_STAMP_DUTY", "search": "",
        }

    tasks: List[Dict] = []
    page = 1
    target = max(needed * 5, 50)

    while len(tasks) < target:
        resp = rt.session.get(
            f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application",
            headers=headers, params={**base_params, "page": page}, timeout=30,
        )
        resp.raise_for_status()
        data    = resp.json()
        results = data.get("results", [])
        if not results:
            break

        for task in results:
            if (task.get("application_status") == "ONGOING"
                    and task.get("node") == "VALUATION_STAMP_DUTY_CREATED"):
                if rt.task_type == "COUNTY_STAMP_DUTY":
                    if (task.get("registry", "").upper() != rt.staff_registry
                            or task.get("county", "").upper() != rt.staff_county):
                        continue
                tasks.append(task)

        if not data.get("next"):
            break
        page += 1

    return tasks


def _fetch_task_detail(rt: RTSession, task_id: str) -> Optional[Dict]:
    """
    Call the detail-view endpoint and return the payload only if all three
    conditions are met: application_status=ONGOING, node=VALUATION_STAMP_DUTY_CREATED,
    node_code=APPLICATION_AWAITING_VALUATION.
    Returns None if any condition fails or the request errors.
    """
    try:
        resp = rt.session.get(
            f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application/detail-view",
            headers=_rt_auth_headers(rt),
            params={"request_id": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("detail-view failed for %s: %s", task_id, e)
        return None

    if (data.get("application_status") != "ONGOING"
            or data.get("node") != "VALUATION_STAMP_DUTY_CREATED"):
        return None

    ext = data.get("external_process_details", {})
    if ext.get("node_code") != "APPLICATION_AWAITING_VALUATION":
        return None

    return data


def _verify_and_filter_tasks(
    rt: RTSession,
    candidates: List[Dict],
) -> List[Dict]:
    """
    For each candidate, call detail-view, apply the three mandatory checks,
    then apply the optional consideration_amount range filter.
    Stops once rt.task_count tasks are matched.
    """
    matched: List[Dict] = []
    for task in candidates:
        if len(matched) >= rt.task_count:
            break
        detail = _fetch_task_detail(rt, task["id"])
        if detail is None:
            continue
        ext    = detail.get("external_process_details", {})
        amount = float(ext.get("consideration_amount") or 0)
        if rt.amount_min is not None and amount < rt.amount_min:
            continue
        if rt.amount_max is not None and amount > rt.amount_max:
            continue
        matched.append({
            "id":                   task["id"],
            "reference_number":     task["reference_number"],
            "consideration_amount": amount,
            "parcel_number":        task.get("parcel_number", ""),
            "registry":             task.get("registry", ""),
            "county":               task.get("county", ""),
            "date_created":         task.get("date_created", ""),
        })
    return matched


async def _do_assign_tasks(
    bot,
    chat_id: int,
    http_sess,
    tokens: AuthTokens,
    tasks: List[Dict],
    staff_uid: str,
    staff_name: str,
    cred_type: str,
):
    """POST assignments and send a result summary to chat_id."""
    url     = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/fix_application_details"
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }

    ok_refs: List[str]   = []
    fail_refs: List[str] = []
    result_lines: List[str] = []

    for task in tasks:
        ref = task["reference_number"]
        try:
            r = http_sess.post(
                url, headers=headers,
                json={
                    "reference_number":   ref,
                    "valuation_officer":  staff_uid,
                    "node":               "VALUATION_STAMP_DUTY_VALUER_REPORT",
                },
                timeout=30,
            )
            r.raise_for_status()
            ok_refs.append(ref)
            result_lines.append(f"✅ `{ref}` — KES {task['consideration_amount']:,.0f}")
            persist_assignment(ref, staff_name, staff_uid)
        except Exception as e:
            fail_refs.append(ref)
            result_lines.append(f"❌ `{ref}` — {e}")

    # Persist the batch
    batch = {
        "batch_id":   str(uuid.uuid4()),
        "staff_name": staff_name,
        "staff_uid":  staff_uid,
        "cred_type":  cred_type,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks":      [t for t in tasks if t["reference_number"] in ok_refs],
        "failed":     fail_refs,
    }
    persist_task_batch(batch)

    summary = (
        f"🏁 *Receive Tasks Complete*\n\n"
        f"*Valuer:* {staff_name}\n"
        f"*Assigned:* {len(ok_refs)} / {len(tasks)}\n"
        f"*Failed:*   {len(fail_refs)} / {len(tasks)}\n\n"
        + "\n".join(result_lines)
    )
    if len(summary) > 4000:
        summary = summary[:4000] + "\n…_(truncated)_"

    await bot.send_message(chat_id, summary, parse_mode="Markdown")


# ──────────────────────────────────────────────────────────
# Receive Tasks — scheduled job
# ──────────────────────────────────────────────────────────

async def _receive_tasks_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue callback: runs receive-tasks automatically on a schedule."""
    job_data  = context.job.data
    chat_id   = job_data["chat_id"]
    sched     = job_data["schedule"]
    cred_type = sched["cred_type"]

    tokens = get_valid_tokens(cred_type)
    if not tokens:
        await context.bot.send_message(
            chat_id,
            "⚠️ *Scheduled receive-tasks failed:* cached tokens are expired.\n"
            "Use */receive* to re-authenticate and reschedule.",
            parse_mode="Markdown",
        )
        return

    await context.bot.send_message(chat_id, "⏰ *Scheduled receive-tasks running…*", parse_mode="Markdown")

    rt = RTSession()
    rt.tokens               = tokens
    rt.session              = build_session()
    rt.task_type            = sched["task_type"]
    rt.staff_registry       = sched.get("staff_registry", "")
    rt.staff_county         = sched.get("staff_county", "")
    rt.task_count           = sched["task_count"]
    rt.amount_min           = sched.get("amount_min")
    rt.amount_max           = sched.get("amount_max")

    try:
        candidates = _fetch_tasks(rt, rt.task_count)
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Task fetch failed: `{e}`", parse_mode="Markdown")
        return

    if not candidates:
        await context.bot.send_message(chat_id, "ℹ️ Scheduled run: no eligible tasks found.")
        return

    matched = _verify_and_filter_tasks(rt, candidates)
    if not matched:
        await context.bot.send_message(chat_id, "ℹ️ Scheduled run: no tasks passed detail-view verification.")
        return

    await _do_assign_tasks(
        context.bot, chat_id,
        rt.session, tokens,
        matched,
        sched["staff_uid"], sched["staff_name"], cred_type,
    )


def _restore_schedules(app) -> None:
    """Re-register active scheduled jobs from persistent storage on startup."""
    schedules = load_schedules()
    restored  = 0
    for sched in schedules:
        if not sched.get("active", True):
            continue
        interval = sched["interval_minutes"] * 60
        app.job_queue.run_repeating(
            _receive_tasks_job,
            interval=interval,
            first=interval,
            data={"chat_id": sched["chat_id"], "schedule": sched},
            name=f"rt_{sched['schedule_id']}",
        )
        restored += 1
    if restored:
        logger.info("Restored %d scheduled receive-tasks job(s).", restored)


# ──────────────────────────────────────────────────────────
# Receive Tasks — conversation helpers
# ──────────────────────────────────────────────────────────

def _get_rt(ctx: ContextTypes.DEFAULT_TYPE) -> RTSession:
    if "rt_session" not in ctx.user_data:
        ctx.user_data["rt_session"] = RTSession()
    return ctx.user_data["rt_session"]


def _fetch_staff_detail(rt: RTSession, list_entry: Dict) -> Dict:
    """
    Fetch the full staff profile from the detail endpoint.
    The list-user-accounts response only returns summary fields;
    department_details / roles / ardhipay_roles / county_units come from here.
    Falls back to the list entry if all attempts fail.
    """
    headers = {
        "Authorization": f"Bearer {rt.tokens.access_token}",
        "JWTAUTH":       f"Bearer {rt.tokens.jwt}",
    }
    account_id = list_entry.get("id", "")
    user_id    = list_entry.get("staff_details", {}).get("user_id", account_id)

    candidates = [
        f"{BASE_URL}/acl/api/v1/accounts/get-user-detail?user_id={user_id}",
        f"{BASE_URL}/acl/api/v1/accounts/get-user-detail/{user_id}",
        f"{BASE_URL}/acl/api/v1/accounts/user-details?user_id={user_id}",
        f"{BASE_URL}/acl/api/v1/accounts/view-user?user_id={user_id}",
        f"{BASE_URL}/acl/api/v1/accounts/{account_id}",
    ]

    for url in candidates:
        try:
            resp = rt.session.get(url, headers=headers, timeout=15)
            logger.info("Staff detail probe %s → HTTP %s body: %.300s", url, resp.status_code, resp.text)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if data.get("department_details") or data.get("roles") or data.get("ardhipay_roles"):
                logger.info("Staff detail fetched from: %s  keys: %s", url, list(data.keys()))
                return data
        except Exception as e:
            logger.warning("Detail attempt failed (%s): %s", url, e)

    logger.warning(
        "Could not fetch staff detail — falling back to list entry.\n"
        "list entry staff_details: %s",
        json.dumps(list_entry.get("staff_details"), default=str),
    )
    return list_entry


async def _rt_resolve_saved_valuer(message, rt: RTSession) -> int:
    """
    After auth, fetch and validate a pre-selected saved valuer.
    Searches by name and matches on account_number, then runs validation.
    """
    sv = rt.saved_valuer
    try:
        headers = {
            "Authorization": f"Bearer {rt.tokens.access_token}",
            "JWTAUTH":       f"Bearer {rt.tokens.jwt}",
        }
        resp = rt.session.get(
            f"{BASE_URL}/acl/api/v1/accounts/list-user-accounts",
            headers=headers,
            params={"account_type": "STAFF", "filter_type": "ACTIVE",
                    "page": 1, "search": sv["name"]},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        await message.reply_text(
            f"❌ Could not fetch profile for *{sv['name']}*: `{e}`",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    # Match by account_number first, fall back to uid, then first result
    match = (
        next((r for r in results if r.get("account_number") == sv["account_number"]), None)
        or next((r for r in results if r.get("id") == sv["uid"]), None)
        or (results[0] if len(results) == 1 else None)
    )
    if not match:
        await message.reply_text(
            f"⚠️ Could not uniquely identify *{sv['name']}* from search results.\n"
            "Use 🔍 Search new valuer to select manually.",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    user_data = _fetch_staff_detail(rt, match)

    sd   = user_data.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))

    ok, err_msg, task_type, registry, county = _validate_staff(user_data)
    if not ok:
        await message.reply_text(
            f"❌ *Validation failed for {name}:*\n{err_msg}",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    rt.staff_data     = user_data
    rt.staff_registry = registry
    rt.staff_county   = county

    sd_roles    = sd.get("roles") or []
    role_labels = ", ".join(r.get("rolename", "") for r in sd_roles) or "None"

    if task_type == "BOTH":
        await message.reply_text(
            f"✅ *Staff Validated*\n\n"
            f"*Name:* {name}\n"
            f"*Roles:* {role_labels}\n"
            f"*County:* {county}  |  *Registry:* {registry}\n\n"
            "Step 3 — Which task pool do you want to assign from?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏛 Stamp Duty (national, from_ardhipay=false)",
                                      callback_data="rt_type:STAMP_DUTY")],
                [InlineKeyboardButton("🏙 County Stamp Duty (from_ardhipay=true)",
                                      callback_data="rt_type:COUNTY_STAMP_DUTY")],
            ]),
        )
        return RS.TASK_TYPE

    rt.task_type = task_type
    type_label   = "County Stamp Duty" if task_type == "COUNTY_STAMP_DUTY" else "Stamp Duty"
    await message.reply_text(
        f"✅ *Staff Validated*\n\n"
        f"*Name:* {name}\n"
        f"*Task Type:* {type_label}\n"
        f"*Roles:* {role_labels}\n\n"
        "Step 3 — How many tasks do you want to assign?",
        parse_mode="Markdown",
    )
    return RS.TASK_COUNT


async def _rt_do_staff_search(message, rt: RTSession) -> int:
    """Search the accounts endpoint and show a selection keyboard."""
    try:
        headers = {
            "Authorization": f"Bearer {rt.tokens.access_token}",
            "JWTAUTH":       f"Bearer {rt.tokens.jwt}",
        }
        resp = rt.session.get(
            f"{BASE_URL}/acl/api/v1/accounts/list-user-accounts",
            headers=headers,
            params={"account_type": "STAFF", "filter_type": "ACTIVE",
                    "page": 1, "search": rt.staff_name},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        await message.reply_text(
            f"❌ Staff search failed: `{e}`", parse_mode="Markdown", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    if not results:
        await message.reply_text(
            f"⚠️ No staff found matching *{rt.staff_name}*.",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    rt.staff_results = results
    rows = []
    for i, v in enumerate(results):
        sd   = v.get("staff_details", {})
        name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
        rows.append([InlineKeyboardButton(name or f"Staff {i+1}", callback_data=f"rt_staff:{i}")])

    await message.reply_text(
        f"Found *{len(results)}* staff member(s). Select one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return RS.SELECT_STAFF


async def _rt_fetch_and_show(message, rt: RTSession) -> int:
    """Fetch + verify tasks, display them, and ask for confirmation."""
    await message.reply_text("⏳ Fetching eligible tasks from the queue…")

    # Peek at the total count first
    headers = _rt_auth_headers(rt)
    if rt.task_type == "STAMP_DUTY":
        peek_params: Dict = {
            "filter": "Pending", "role": "DLV",
            "request_type": "STAMP_DUTY", "search": "", "page": 1,
        }
    else:
        peek_params = {
            "filter": "Ongoing", "from_ardhipay": "true",
            "role": "DLV", "request_type": "COUNTY_STAMP_DUTY", "search": "", "page": 1,
        }

    try:
        peek = rt.session.get(
            f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application",
            headers=headers, params=peek_params, timeout=30,
        )
        peek.raise_for_status()
        total_count = peek.json().get("count", "?")
    except Exception:
        total_count = "?"

    await message.reply_text(
        f"📊 Total tasks in queue: *{total_count}*\n"
        f"🔍 Scanning for up to *{rt.task_count}* eligible task(s)…",
        parse_mode="Markdown",
    )

    try:
        candidates = _fetch_tasks(rt, rt.task_count)
    except Exception as e:
        await message.reply_text(f"❌ Task fetch failed: `{e}`", parse_mode="Markdown", reply_markup=_main_menu())
        return ConversationHandler.END

    if not candidates:
        await message.reply_text(
            "ℹ️ No eligible tasks found matching your filters.", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    await message.reply_text(f"🔍 Verifying *{len(candidates)}* candidate(s) via detail-view…", parse_mode="Markdown")

    matched = _verify_and_filter_tasks(rt, candidates)
    if not matched:
        await message.reply_text(
            "ℹ️ No tasks passed the detail-view verification checks.", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    rt.matched_tasks = matched

    # Build display list
    sd   = rt.staff_data.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
    lines = []
    for i, t in enumerate(matched, 1):
        lines.append(
            f"{i}. `{t['reference_number']}`\n"
            f"   💰 KES {t['consideration_amount']:,.0f}\n"
            f"   📍 {t['parcel_number']} | 🏢 {t['registry']}\n"
            f"   📅 {t['date_created'][:10]}"
        )

    summary = (
        f"📋 *Tasks for {name}* ({len(matched)} task(s))\n\n"
        + "\n\n".join(lines)
        + "\n\nConfirm assignment?"
    )
    if len(summary) > 4000:
        summary = (
            f"📋 *{len(matched)} tasks* ready to assign to *{name}*.\n"
            "_(List too long to display in full)_\n\nConfirm assignment?"
        )

    await message.reply_text(
        summary,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm & Assign", callback_data="rt_confirm:yes")],
            [InlineKeyboardButton("❌ Cancel",           callback_data="rt_confirm:no")],
        ]),
    )
    return RS.RT_CONFIRM


# ──────────────────────────────────────────────────────────
# Receive Tasks — conversation handlers
# ──────────────────────────────────────────────────────────

async def cmd_receive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    ctx.user_data["rt_session"] = RTSession()
    saved = load_saved_valuers()

    await update.message.reply_text(
        "📥 *Receive Tasks Flow*\n\nStep 1 — Select a valuer or search for a new one:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    if saved:
        rows = [
            [InlineKeyboardButton(f"👤 {sv['name']}", callback_data=f"rt_src:{i}")]
            for i, sv in enumerate(saved)
        ]
        rows.append([InlineKeyboardButton("🔍 Search new valuer", callback_data="rt_src:new")])
        await update.message.reply_text(
            "Choose a saved valuer or search new:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return RS.PICK_STAFF_SOURCE

    # No saved valuers — go straight to name search
    await update.message.reply_text(
        "Enter the staff member's name to search:",
        parse_mode="Markdown",
    )
    return RS.STAFF_NAME


async def recv_rt_pick_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rt   = _get_rt(ctx)
    data = query.data.split(":")[1]

    if data == "new":
        await query.edit_message_text(
            "🔍 Enter the staff member's name to search:",
            parse_mode="Markdown",
        )
        return RS.STAFF_NAME

    saved = load_saved_valuers()
    sv    = saved[int(data)]
    rt.saved_valuer = sv
    rt.staff_name   = sv["name"]

    await query.edit_message_text(
        f"👤 Selected: *{sv['name']}*\n\n"
        "Step 2 — Choose *credential profile*:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return RS.CHOOSE_CRED


async def recv_rt_staff_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rt = _get_rt(ctx)
    rt.staff_name = update.message.text.strip()
    await update.message.reply_text(
        f"🔍 Searching for: *{rt.staff_name}*\n\n"
        "Step 2 — Choose *credential profile*:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return RS.CHOOSE_CRED


async def recv_rt_cred_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    rt        = _get_rt(ctx)
    cred_type = query.data.split(":")[1]
    rt.cred_type = cred_type
    creds     = CRED_MAP[cred_type]

    cached = get_valid_tokens(cred_type)
    if cached:
        rt.tokens  = cached
        rt.session = build_session()
        if rt.saved_valuer:
            await query.edit_message_text(
                f"🔑 Cached login: *{CRED_LABELS[cred_type]}*\n\n"
                f"🔍 Fetching profile for *{rt.saved_valuer['name']}*…",
                parse_mode="Markdown",
            )
            return await _rt_resolve_saved_valuer(query.message, rt)
        await query.edit_message_text(
            f"🔑 Cached login: *{CRED_LABELS[cred_type]}*\n\n"
            f"🔍 Searching for *{rt.staff_name}*…",
            parse_mode="Markdown",
        )
        return await _rt_do_staff_search(query.message, rt)

    # Full login
    rt.session = build_session()
    await query.edit_message_text(
        f"✅ Credential: *{CRED_LABELS[cred_type]}*\n\n🔐 Sending login request…",
        parse_mode="Markdown",
    )
    try:
        resp = rt.session.post(
            f"{AUTH_BASE_URL}/login",
            json={"username": creds["username"], "password": creds["password"],
                  "usertype": creds["usertype"], "otpcode": ""},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") is False:
            raise RuntimeError(data.get("error") or data.get("message"))
    except Exception as e:
        await query.message.reply_text(
            f"❌ Login failed: `{e}`", parse_mode="Markdown", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    await query.message.reply_text(
        "📲 OTP sent to registered device.\n\nPlease *reply with the OTP code*:",
        parse_mode="Markdown",
    )
    return RS.WAIT_OTP


async def recv_rt_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rt    = _get_rt(ctx)
    otp   = update.message.text.strip()
    creds = CRED_MAP[rt.cred_type]

    await update.message.reply_text("🔄 Verifying OTP…")
    try:
        resp = rt.session.post(
            f"{AUTH_BASE_URL}/otpverify",
            json={"username": creds["username"], "password": creds["password"], "otpcode": otp},
            timeout=30,
        )
        resp.raise_for_status()
        data         = resp.json()
        access_token = data.get("details", {}).get("access_token")
        jwt          = data.get("details", {}).get("jwt")
        if not access_token or not jwt:
            raise RuntimeError(f"Tokens missing. Keys: {list(data.keys())}")
        rt.tokens = AuthTokens(access_token=access_token, jwt=jwt)
        persist_tokens(rt.cred_type, access_token, jwt)
    except Exception as e:
        await update.message.reply_text(
            f"❌ OTP failed: `{e}`\n\nSend the OTP again or tap 🛑 Cancel.",
            parse_mode="Markdown",
        )
        return RS.WAIT_OTP

    await update.message.reply_text("✅ Authenticated!")
    if rt.saved_valuer:
        return await _rt_resolve_saved_valuer(update.message, rt)
    return await _rt_do_staff_search(update.message, rt)


async def recv_rt_select_staff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    rt        = _get_rt(ctx)
    idx       = int(query.data.split(":")[1])
    list_entry = rt.staff_results[idx]

    sd   = list_entry.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))

    await query.edit_message_text(f"🔍 Fetching full profile for *{name}*…", parse_mode="Markdown")
    user_data = _fetch_staff_detail(rt, list_entry)

    ok, err_msg, task_type, registry, county = _validate_staff(user_data)
    if not ok:
        await query.edit_message_text(
            f"❌ *Validation failed for {name}:*\n{err_msg}",
            parse_mode="Markdown",
        )
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())
        return ConversationHandler.END

    rt.staff_data     = user_data
    rt.staff_registry = registry
    rt.staff_county   = county

    sd_roles = (user_data.get("staff_details") or {}).get("roles") or []
    role_labels = ", ".join(r.get("rolename", "") for r in sd_roles) or "None"

    if task_type == "BOTH":
        # Staff has both VALUER and COUNTY_VALUER — let user pick the task pool
        await query.edit_message_text(
            f"✅ *Staff Validated*\n\n"
            f"*Name:* {name}\n"
            f"*Roles:* {role_labels}\n"
            f"*County:* {county}  |  *Registry:* {registry}\n\n"
            "Step 3 — Which task pool do you want to assign from?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏛 Stamp Duty (national, from_ardhipay=false)",
                                      callback_data="rt_type:STAMP_DUTY")],
                [InlineKeyboardButton("🏙 County Stamp Duty (from_ardhipay=true)",
                                      callback_data="rt_type:COUNTY_STAMP_DUTY")],
            ]),
        )
        return RS.TASK_TYPE

    # Only VALUER — go straight to task count
    rt.task_type = task_type
    type_label   = "County Stamp Duty" if task_type == "COUNTY_STAMP_DUTY" else "Stamp Duty"
    await query.edit_message_text(
        f"✅ *Staff Validated*\n\n"
        f"*Name:* {name}\n"
        f"*Task Type:* {type_label}\n"
        f"*Roles:* {role_labels}\n\n"
        "Step 3 — How many tasks do you want to assign?",
        parse_mode="Markdown",
    )
    return RS.TASK_COUNT


async def recv_rt_task_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles the Stamp Duty / County Stamp Duty choice."""
    query = update.callback_query
    await query.answer()
    rt    = _get_rt(ctx)
    rt.task_type = query.data.split(":")[1]   # "STAMP_DUTY" or "COUNTY_STAMP_DUTY"

    type_label  = "County Stamp Duty" if rt.task_type == "COUNTY_STAMP_DUTY" else "Stamp Duty"
    county_line = (
        f"\n*County:* {rt.staff_county}  |  *Registry:* {rt.staff_registry}"
        if rt.task_type == "COUNTY_STAMP_DUTY" else ""
    )
    await query.edit_message_text(
        f"✅ *Task pool:* {type_label}{county_line}\n\n"
        "Step 3 — How many tasks do you want to assign?",
        parse_mode="Markdown",
    )
    return RS.TASK_COUNT


async def recv_rt_task_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rt = _get_rt(ctx)
    try:
        count = int(update.message.text.strip())
        if count <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a positive whole number.")
        return RS.TASK_COUNT

    rt.task_count = count
    await update.message.reply_text(
        f"✅ *{count} task(s)* requested.\n\n"
        "Step 4 — Would you like to filter by consideration amount?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Enter Amount Range", callback_data="rt_amount:enter")],
            [InlineKeyboardButton("⏭ Skip",               callback_data="rt_amount:skip")],
        ]),
    )
    return RS.AMOUNT_RANGE


async def recv_rt_amount_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles the Enter / Skip button on the amount range step."""
    query  = update.callback_query
    await query.answer()
    rt     = _get_rt(ctx)
    choice = query.data.split(":")[1]

    if choice == "skip":
        rt.amount_min = rt.amount_max = None
        await query.edit_message_text(
            "⏭ No amount filter applied.\n\n"
            "Step 5 — Run now or set up a recurring schedule?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Run Now",              callback_data="rt_sched:now")],
                [InlineKeyboardButton("⏰ Schedule (repeating)", callback_data="rt_sched:schedule")],
            ]),
        )
        return RS.SCHEDULE_CHOICE

    # "enter" — ask for the range as text
    await query.edit_message_text(
        "💰 Enter the *consideration amount range*:\n"
        "_Format:_ `min-max`  _(e.g._ `100000-500000`_)_",
        parse_mode="Markdown",
    )
    return RS.AMOUNT_TEXT


async def recv_rt_amount_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles the typed min-max range after the user chose 'Enter Amount Range'."""
    rt   = _get_rt(ctx)
    text = update.message.text.strip()

    try:
        parts = re.split(r"[-–]", text, maxsplit=1)
        if len(parts) != 2:
            raise ValueError
        lo = float(parts[0].strip().replace(",", ""))
        hi = float(parts[1].strip().replace(",", ""))
        rt.amount_min, rt.amount_max = (lo, hi) if lo <= hi else (hi, lo)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Invalid format. Use `min-max` e.g. `100000-500000`.",
            parse_mode="Markdown",
        )
        return RS.AMOUNT_TEXT

    await update.message.reply_text(
        f"✅ Amount range: *KES {rt.amount_min:,.0f} – KES {rt.amount_max:,.0f}*\n\n"
        "Step 5 — Run now or set up a recurring schedule?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Run Now",              callback_data="rt_sched:now")],
            [InlineKeyboardButton("⏰ Schedule (repeating)", callback_data="rt_sched:schedule")],
        ]),
    )
    return RS.SCHEDULE_CHOICE


async def recv_rt_schedule_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rt     = _get_rt(ctx)
    choice = query.data.split(":")[1]

    if choice == "now":
        await query.edit_message_text("▶️ Running now…")
        return await _rt_fetch_and_show(query.message, rt)

    await query.edit_message_text(
        "⏰ *Schedule Setup*\n\n"
        "Enter the repeat interval in minutes:\n"
        "_(e.g._ `60` _= every hour,_ `1440` _= daily)_",
        parse_mode="Markdown",
    )
    return RS.SCHEDULE_INTERVAL


async def recv_rt_schedule_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rt = _get_rt(ctx)
    try:
        minutes = int(update.message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Enter a positive number of minutes.")
        return RS.SCHEDULE_INTERVAL

    rt.schedule_interval_minutes = minutes
    await update.message.reply_text(
        f"⏰ Will repeat every *{minutes} minute(s)*.\n\nFetching tasks for preview…",
        parse_mode="Markdown",
    )
    return await _rt_fetch_and_show(update.message, rt)


async def recv_rt_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "rt_confirm:no":
        await query.edit_message_text("❌ Receive tasks cancelled.")
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())
        return ConversationHandler.END

    rt   = _get_rt(ctx)
    sd   = rt.staff_data.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
    uid  = sd.get("user_id", rt.staff_data.get("id", "?"))

    await query.edit_message_text(
        f"⚙️ Assigning *{len(rt.matched_tasks)}* task(s) to *{name}*…",
        parse_mode="Markdown",
    )

    await _do_assign_tasks(
        ctx.bot, query.message.chat_id,
        rt.session, rt.tokens,
        rt.matched_tasks, uid, name, rt.cred_type,
    )

    # Register repeating schedule if requested
    if rt.schedule_interval_minutes:
        sched = {
            "schedule_id":      str(uuid.uuid4()),
            "staff_uid":        uid,
            "staff_name":       name,
            "cred_type":        rt.cred_type,
            "task_count":       rt.task_count,
            "amount_min":       rt.amount_min,
            "amount_max":       rt.amount_max,
            "task_type":        rt.task_type,
            "staff_registry":   rt.staff_registry,
            "staff_county":     rt.staff_county,
            "interval_minutes": rt.schedule_interval_minutes,
            "chat_id":          query.message.chat_id,
            "active":           True,
        }
        persist_schedule(sched)
        ctx.job_queue.run_repeating(
            _receive_tasks_job,
            interval=rt.schedule_interval_minutes * 60,
            first=rt.schedule_interval_minutes * 60,
            data={"chat_id": query.message.chat_id, "schedule": sched},
            name=f"rt_{sched['schedule_id']}",
        )
        await query.message.reply_text(
            f"⏰ *Schedule saved:* runs every *{rt.schedule_interval_minutes} minute(s)*.\n"
            f"Use */schedules* to view active schedules.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
    else:
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())

    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# /schedules — list saved schedules
# ──────────────────────────────────────────────────────────

async def cmd_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    schedules = [s for s in load_schedules() if s.get("active", True)]
    if not schedules:
        await update.message.reply_text("📭 No active schedules.", reply_markup=_main_menu())
        return
    lines = []
    for s in schedules:
        range_str = (
            f"KES {s['amount_min']:,.0f}–{s['amount_max']:,.0f}"
            if s.get("amount_min") is not None else "any amount"
        )
        lines.append(
            f"• *{s['staff_name']}* — every *{s['interval_minutes']}min*\n"
            f"  Tasks: {s['task_count']} | {s['task_type']} | {range_str}\n"
            f"  ID: `{s['schedule_id'][:8]}…`"
        )
    await update.message.reply_text(
        "⏰ *Active Schedules:*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


# ──────────────────────────────────────────────────────────
# /task_batches — view saved task batch history
# ──────────────────────────────────────────────────────────

async def cmd_task_batches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    batches = load_task_batches()
    if not batches:
        await update.message.reply_text("📭 No saved task batches yet.", reply_markup=_main_menu())
        return
    lines = []
    for b in batches[-10:]:   # most recent 10
        lines.append(
            f"• *{b['staff_name']}*  —  {b['created_at']}\n"
            f"  Assigned: {len(b['tasks'])}  |  Failed: {len(b.get('failed', []))}"
        )
    await update.message.reply_text(
        "📦 *Recent Task Batches (last 10):*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


# ──────────────────────────────────────────────────────────
# Token refresh daemon — process management
# ──────────────────────────────────────────────────────────

def _daemon_read_pid() -> Optional[int]:
    """Return the PID stored in the pid file, or None if missing/invalid."""
    try:
        with open(DAEMON_PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _daemon_running() -> bool:
    """Return True if the daemon process is alive."""
    pid = _daemon_read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)   # signal 0 = probe only, no actual signal sent
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _daemon_start() -> Tuple[bool, str]:
    """
    Launch token_refresh_daemon.py in a detached process.
    Uses start_new_session=True so it is not killed when the bot receives SIGTERM.
    stdout/stderr are appended to DAEMON_LOG_FILE.
    Returns (success, message).
    """
    if _daemon_running():
        return False, f"Already running (PID {_daemon_read_pid()})."

    if not os.path.exists(DAEMON_SCRIPT):
        return False, f"Script not found: {DAEMON_SCRIPT}"

    _ensure_data_dir()
    log_fh = open(DAEMON_LOG_FILE, "a")
    proc = subprocess.Popen(
        [sys.executable, "-u", DAEMON_SCRIPT],
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,   # detach from bot's process group
        close_fds=True,
    )
    with open(DAEMON_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    logger.info("Token refresh daemon started (PID %d)", proc.pid)
    return True, f"Started (PID {proc.pid}). Logs → `{DAEMON_LOG_FILE}`"


def _daemon_stop() -> Tuple[bool, str]:
    """Send SIGTERM to the daemon. Returns (success, message)."""
    if not _daemon_running():
        return False, "Daemon is not running."
    pid = _daemon_read_pid()
    try:
        os.kill(pid, signal.SIGTERM)
        # Give it a moment, then confirm it's gone
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


async def cmd_daemon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        f"🔄 *Token Refresh Daemon*\n\n"
        f"Status: {_daemon_status_text()}\n\n"
        f"The daemon watches the token cache and silently refreshes "
        f"each token *5 minutes before it expires*.\n"
        f"Logs are written to `data/daemon.log`.",
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

    status = _daemon_status_text()
    await query.edit_message_text(
        f"🔄 *Token Refresh Daemon*\n\n"
        f"Status: {status}\n\n"
        f"{'✅' if ok else '❌'} {msg}",
        parse_mode="Markdown",
        reply_markup=_daemon_keyboard(),
    )


# ──────────────────────────────────────────────────────────
# /cancel
# ──────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("session", None)
    await update.message.reply_text(
        "🛑 Flow cancelled.",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Fallback (unexpected input during conversation)
# ──────────────────────────────────────────────────────────
async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤔 I didn't understand that. Follow the steps above, or tap 🛑 Cancel to abort."
    )


# ──────────────────────────────────────────────────────────
# Implementor Tasks & DLV Tasks — view-only checklist
# ──────────────────────────────────────────────────────────

def _any_valid_tokens() -> Optional[AuthTokens]:
    """Return the first valid cached AuthTokens across all credential profiles."""
    for cred_type in ("staff_valuer", "staff2", "staff", "publicuser"):
        t = get_valid_tokens(cred_type)
        if t:
            return t
    return None


def _parse_task_date(date_str: str) -> Optional[datetime]:
    """Parse an ISO 8601 date string to an aware datetime (UTC)."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fetch_impl_detail_one(http_sess, tokens: AuthTokens, task_id: str) -> Optional[Dict]:
    """Fetch registration detail view for one implementor task."""
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_ASSESSOR,
    }
    try:
        resp = http_sess.get(
            f"{BASE_URL}/registrationservice/api/v1/transfer/transfer-request-staff-detailed-view",
            headers=headers,
            params={"request_id": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("impl detail failed for %s: %s", task_id, e)
        return None


def _fetch_dlv_detail_one(http_sess, tokens: AuthTokens, task_id: str) -> Optional[Dict]:
    """Fetch DLV application detail view for one task."""
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_VALUER_ROLE,
    }
    try:
        resp = http_sess.get(
            f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application/detail-view",
            headers=headers,
            params={"request_id": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("DLV detail failed for %s: %s", task_id, e)
        return None


def _load_implementor_tasks(tokens: AuthTokens) -> List[Dict]:
    """
    Fetch all pending assessor tasks created in the last 10 days,
    enrich each with detail fields, return sorted newest-first.
    """
    http_sess = build_session()
    cutoff = datetime.now(timezone.utc) - timedelta(days=10)
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_ASSESSOR,
    }

    candidates: List[Dict] = []
    page = 1
    while True:
        try:
            resp = http_sess.get(
                f"{BASE_URL}/stampdutyservice/api/v1/stamp-duty/assessor",
                headers=headers,
                params={"filter": "Pending", "page": page, "search": ""},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("impl list page %d failed: %s", page, e)
            break

        results = data.get("results", [])
        if not results:
            break

        for task in results:
            dt = _parse_task_date(task.get("date_created", ""))
            if dt and dt >= cutoff:
                candidates.append(task)

        if not data.get("next"):
            break
        page += 1

    enriched: List[Dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {
            pool.submit(_fetch_impl_detail_one, http_sess, tokens, t["id"]): t
            for t in candidates
        }
        for future in _futures_as_completed(future_map):
            list_task = future_map[future]
            detail    = future.result()
            consideration = ""
            county    = list_task.get("county", "")
            registry  = list_task.get("registry", "")
            if detail:
                consideration = str(detail.get("consideration", ""))
                county    = detail.get("county", county)
                registry  = detail.get("registry", registry)
            enriched.append({
                "id":               list_task["id"],
                "reference_number": list_task.get("reference_number", ""),
                "date_created":     list_task.get("date_created", ""),
                "consideration":    consideration,
                "county":           county,
                "registry":         registry,
            })

    enriched.sort(key=lambda x: x["date_created"], reverse=True)
    return enriched


def _load_dlv_tasks(tokens: AuthTokens) -> List[Dict]:
    """
    Fetch all pending DLV/VALUER tasks created in the last 2 days,
    enrich each with detail fields, return sorted newest-first.
    """
    http_sess = build_session()
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_VALUER_ROLE,
    }

    candidates: List[Dict] = []
    page = 1
    while True:
        try:
            resp = http_sess.get(
                f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application",
                headers=headers,
                params={
                    "filter": "Pending", "role": "VALUER",
                    "request_type": "STAMP_DUTY", "search": "", "page": page,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("DLV list page %d failed: %s", page, e)
            break

        results = data.get("results", [])
        if not results:
            break

        for task in results:
            dt = _parse_task_date(task.get("date_created", ""))
            if dt and dt >= cutoff:
                candidates.append(task)

        if not data.get("next"):
            break
        page += 1

    enriched: List[Dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {
            pool.submit(_fetch_dlv_detail_one, http_sess, tokens, t["id"]): t
            for t in candidates
        }
        for future in _futures_as_completed(future_map):
            list_task = future_map[future]
            detail    = future.result()
            consideration = ""
            if detail:
                ext = detail.get("external_process_details") or {}
                consideration = str(ext.get("consideration_amount", ""))
            enriched.append({
                "id":               list_task["id"],
                "reference_number": list_task.get("reference_number", ""),
                "date_created":     list_task.get("date_created", ""),
                "consideration":    consideration,
                "county":           list_task.get("county", ""),
                "registry":         list_task.get("registry", ""),
            })

    enriched.sort(key=lambda x: x["date_created"], reverse=True)
    return enriched


def _tasks_keyboard(tasks: List[Dict], checked: set, prefix: str) -> InlineKeyboardMarkup:
    """Build a checklist inline keyboard for a task list."""
    rows = []
    for i, t in enumerate(tasks):
        mark  = "☑️" if i in checked else "⬜"
        ref   = t.get("reference_number", f"#{i+1}")
        cons  = t.get("consideration", "")
        try:
            cons_str = f"KES {int(float(cons)):,}" if cons else "—"
        except (ValueError, TypeError):
            cons_str = cons or "—"
        county = t.get("county", "")
        date   = (t.get("date_created") or "")[:10]
        label  = f"{mark} {ref} | {cons_str} | {county} | {date}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{prefix}:{i}")])

    action_row = [InlineKeyboardButton("🔄 Refresh", callback_data=f"{prefix}:refresh")]
    if checked:
        action_row.append(
            InlineKeyboardButton(f"📤 Assign Selected ({len(checked)})", callback_data=f"{prefix}:assign")
        )
    rows.append(action_row)
    return InlineKeyboardMarkup(rows)


async def cmd_impl_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    tokens = _any_valid_tokens()
    if not tokens:
        await update.message.reply_text(
            "⚠️ No valid cached tokens found.\n"
            "Please authenticate first via *New Assignment* or *Receive Tasks*, then try again.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return

    await update.message.reply_text("⏳ Fetching Implementor Tasks (last 10 days)…")
    try:
        tasks = _load_implementor_tasks(tokens)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to fetch tasks: `{e}`", parse_mode="Markdown", reply_markup=_main_menu()
        )
        return

    if not tasks:
        await update.message.reply_text(
            "ℹ️ No pending implementor tasks in the last 10 days.",
            reply_markup=_main_menu(),
        )
        return

    ctx.user_data["impl_tasks"]   = tasks
    ctx.user_data["impl_checked"] = set()
    await update.message.reply_text(
        f"📊 *Implementor Tasks* — {len(tasks)} task(s) in last 10 days\n"
        "Tap any row to check/uncheck:",
        parse_mode="Markdown",
        reply_markup=_tasks_keyboard(tasks, set(), "impl_ck"),
    )


async def cmd_dlv_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    tokens = _any_valid_tokens()
    if not tokens:
        await update.message.reply_text(
            "⚠️ No valid cached tokens found.\n"
            "Please authenticate first via *New Assignment* or *Receive Tasks*, then try again.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return

    await update.message.reply_text("⏳ Fetching DLV Tasks (last 2 days)…")
    try:
        tasks = _load_dlv_tasks(tokens)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to fetch tasks: `{e}`", parse_mode="Markdown", reply_markup=_main_menu()
        )
        return

    if not tasks:
        await update.message.reply_text(
            "ℹ️ No pending DLV tasks in the last 2 days.",
            reply_markup=_main_menu(),
        )
        return

    ctx.user_data["dlv_tasks"]   = tasks
    ctx.user_data["dlv_checked"] = set()
    await update.message.reply_text(
        f"📋 *DLV Tasks* — {len(tasks)} task(s) in last 2 days\n"
        "Tap any row to check/uncheck:",
        parse_mode="Markdown",
        reply_markup=_tasks_keyboard(tasks, set(), "dlv_ck"),
    )


async def recv_impl_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx_str = query.data.split(":")[1]
    tasks   = ctx.user_data.get("impl_tasks", [])
    checked = ctx.user_data.get("impl_checked", set())

    if idx_str == "refresh":
        tokens = _any_valid_tokens()
        if not tokens:
            await query.answer("No valid tokens — authenticate first.", show_alert=True)
            return
        await query.edit_message_text("⏳ Refreshing Implementor Tasks…")
        try:
            tasks = _load_implementor_tasks(tokens)
        except Exception as e:
            await query.edit_message_text(f"❌ Refresh failed: {e}")
            return
        checked = set()
        ctx.user_data["impl_tasks"]   = tasks
        ctx.user_data["impl_checked"] = checked
        await query.edit_message_text(
            f"📊 *Implementor Tasks* — {len(tasks)} task(s) in last 10 days\n"
            "Tap any row to check/uncheck:",
            parse_mode="Markdown",
            reply_markup=_tasks_keyboard(tasks, checked, "impl_ck"),
        )
        return

    if idx_str == "assign":
        if not checked:
            await query.answer("No tasks selected.", show_alert=True)
            return
        selected = [tasks[i] for i in sorted(checked) if i < len(tasks)]
        ctx.user_data["ta_pending"] = {"type": "impl", "tasks": selected, "search_results": []}
        await query.message.reply_text(
            f"📤 Assigning *{len(selected)}* implementor task(s).\n\n"
            "Select the valuer to assign to:",
            parse_mode="Markdown",
            reply_markup=_ta_valuer_picker_keyboard("impl"),
        )
        return

    idx = int(idx_str)
    if idx in checked:
        checked.discard(idx)
    else:
        checked.add(idx)
    ctx.user_data["impl_checked"] = checked
    await query.edit_message_reply_markup(
        reply_markup=_tasks_keyboard(tasks, checked, "impl_ck"),
    )


async def recv_dlv_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx_str = query.data.split(":")[1]
    tasks   = ctx.user_data.get("dlv_tasks", [])
    checked = ctx.user_data.get("dlv_checked", set())

    if idx_str == "refresh":
        tokens = _any_valid_tokens()
        if not tokens:
            await query.answer("No valid tokens — authenticate first.", show_alert=True)
            return
        await query.edit_message_text("⏳ Refreshing DLV Tasks…")
        try:
            tasks = _load_dlv_tasks(tokens)
        except Exception as e:
            await query.edit_message_text(f"❌ Refresh failed: {e}")
            return
        checked = set()
        ctx.user_data["dlv_tasks"]   = tasks
        ctx.user_data["dlv_checked"] = checked
        await query.edit_message_text(
            f"📋 *DLV Tasks* — {len(tasks)} task(s) in last 2 days\n"
            "Tap any row to check/uncheck:",
            parse_mode="Markdown",
            reply_markup=_tasks_keyboard(tasks, checked, "dlv_ck"),
        )
        return

    if idx_str == "assign":
        if not checked:
            await query.answer("No tasks selected.", show_alert=True)
            return
        selected = [tasks[i] for i in sorted(checked) if i < len(tasks)]
        ctx.user_data["ta_pending"] = {"type": "dlv", "tasks": selected, "search_results": []}
        await query.message.reply_text(
            f"📤 Assigning *{len(selected)}* DLV task(s).\n\n"
            "Select the valuer to assign to:",
            parse_mode="Markdown",
            reply_markup=_ta_valuer_picker_keyboard("dlv"),
        )
        return

    idx = int(idx_str)
    if idx in checked:
        checked.discard(idx)
    else:
        checked.add(idx)
    ctx.user_data["dlv_checked"] = checked
    await query.edit_message_reply_markup(
        reply_markup=_tasks_keyboard(tasks, checked, "dlv_ck"),
    )


# ──────────────────────────────────────────────────────────
# Task-assign sub-flow (valuer picker + assignment execution)
# ──────────────────────────────────────────────────────────

def _ta_valuer_picker_keyboard(task_type: str) -> InlineKeyboardMarkup:
    """Inline keyboard: saved valuers + search-new option."""
    rows = []
    for i, v in enumerate(load_saved_valuers()):
        rows.append([InlineKeyboardButton(
            f"👤 {v['name']}", callback_data=f"ta:{task_type}:sv:{i}"
        )])
    rows.append([InlineKeyboardButton("🔍 Search New Valuer", callback_data=f"ta:{task_type}:ns")])
    rows.append([InlineKeyboardButton("❌ Cancel",            callback_data=f"ta:{task_type}:cancel")])
    return InlineKeyboardMarkup(rows)


async def _ta_run_assign(message, ctx: ContextTypes.DEFAULT_TYPE,
                         task_type: str, valuer_uid: str, valuer_name: str, acct: str):
    """Execute assignment for all tasks in ta_pending and report results."""
    pending = ctx.user_data.get("ta_pending", {})
    tasks   = pending.get("tasks", [])
    tokens  = _any_valid_tokens()
    if not tokens:
        await message.reply_text(
            "⚠️ Tokens expired. Please re-authenticate via New Assignment or Receive Tasks.",
            reply_markup=_main_menu(),
        )
        return

    http_sess = build_session()
    url     = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/fix_application_details"
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }

    await message.reply_text(f"⏳ Assigning {len(tasks)} task(s) to *{valuer_name}*…", parse_mode="Markdown")

    ok_refs, fail_refs, result_lines = [], [], []
    for task in tasks:
        ref = task.get("reference_number", "")
        try:
            r = http_sess.post(
                url, headers=headers,
                json={
                    "reference_number":  ref,
                    "valuation_officer": valuer_uid,
                    "node":              "VALUATION_STAMP_DUTY_VALUER_REPORT",
                },
                timeout=30,
            )
            r.raise_for_status()
            ok_refs.append(ref)
            result_lines.append(f"✅ `{ref}`")
            persist_assignment(ref, valuer_name, valuer_uid)
        except Exception as e:
            fail_refs.append(ref)
            result_lines.append(f"❌ `{ref}` — {e}")

    if ok_refs:
        persist_valuer(valuer_name, valuer_uid, acct)

    summary = (
        f"🏁 *Assignment Complete*\n\n"
        f"*Valuer:* {valuer_name}\n"
        f"*Success:* {len(ok_refs)} / {len(tasks)}\n"
        f"*Failed:*  {len(fail_refs)} / {len(tasks)}\n\n"
        + "\n".join(result_lines)
    )
    if len(summary) > 4000:
        summary = summary[:4000] + "\n…_(truncated)_"
    await message.reply_text(summary, parse_mode="Markdown", reply_markup=_main_menu())

    # Clear pending state
    ctx.user_data.pop("ta_pending", None)


async def recv_ta_valuer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle all ta:* callback data for the task-assign sub-flow."""
    query = update.callback_query
    await query.answer()

    # callback format: ta:{task_type}:{action}[:{idx}]
    parts     = query.data.split(":")
    task_type = parts[1]   # "impl" or "dlv"
    action    = parts[2]   # "sv", "ns", "cancel", "sr"

    pending = ctx.user_data.get("ta_pending", {})

    if action == "cancel":
        ctx.user_data.pop("ta_pending", None)
        await query.edit_message_text("❌ Assignment cancelled.")
        return

    if action == "sv":
        idx    = int(parts[3])
        saved  = load_saved_valuers()
        if idx >= len(saved):
            await query.edit_message_text("⚠️ Valuer not found in saved list.")
            return
        v = saved[idx]
        await query.edit_message_text(
            f"✅ Valuer: *{v['name']}*\nStarting assignment…",
            parse_mode="Markdown",
        )
        await _ta_run_assign(query.message, ctx, task_type, v["uid"], v["name"], v["account_number"])
        return

    if action == "ns":
        pending["awaiting_search"] = True
        ctx.user_data["ta_pending"] = pending
        await query.edit_message_text(
            "🔍 Enter the *valuer name* to search:\n_(Partial names work, e.g._ `JOHN KAMAU`_)_",
            parse_mode="Markdown",
        )
        return

    if action == "sr":
        idx     = int(parts[3])
        results = pending.get("search_results", [])
        if idx >= len(results):
            await query.edit_message_text("⚠️ Search result no longer available.")
            return
        v    = results[idx]
        sd   = v.get("staff_details", {})
        name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
        uid  = str(v.get("id", ""))
        acct = str(v.get("account_number", ""))
        await query.edit_message_text(
            f"✅ Valuer: *{name}*\nStarting assignment…",
            parse_mode="Markdown",
        )
        await _ta_run_assign(query.message, ctx, task_type, uid, name, acct)
        return


async def handle_ta_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Global text handler: catches valuer search input during task-assign flow."""
    pending = ctx.user_data.get("ta_pending")
    if not pending or not pending.get("awaiting_search"):
        return   # nothing to do — not in task-assign search mode

    valuer_name = update.message.text.strip()
    task_type   = pending.get("type", "impl")
    pending["awaiting_search"] = False

    tokens = _any_valid_tokens()
    if not tokens:
        await update.message.reply_text(
            "⚠️ Tokens expired. Please re-authenticate first.",
            reply_markup=_main_menu(),
        )
        ctx.user_data.pop("ta_pending", None)
        return

    await update.message.reply_text(f"🔍 Searching for *{valuer_name}*…", parse_mode="Markdown")
    try:
        http_sess = build_session()
        resp = http_sess.get(
            f"{BASE_URL}/acl/api/v1/accounts/list-user-accounts",
            headers={
                "Authorization": f"Bearer {tokens.access_token}",
                "JWTAUTH":       f"Bearer {tokens.jwt}",
            },
            params={"account_type": "STAFF", "filter_type": "ACTIVE", "page": 1, "search": valuer_name},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        await update.message.reply_text(f"❌ Search failed: `{e}`", parse_mode="Markdown")
        return

    if not results:
        await update.message.reply_text(
            f"⚠️ No staff found matching *{valuer_name}*. Try again:",
            parse_mode="Markdown",
            reply_markup=_ta_valuer_picker_keyboard(task_type),
        )
        pending["awaiting_search"] = False
        return

    pending["search_results"] = results
    ctx.user_data["ta_pending"] = pending

    rows = []
    for i, v in enumerate(results):
        sd   = v.get("staff_details", {})
        name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
        rows.append([InlineKeyboardButton(
            name or f"Valuer {i+1}", callback_data=f"ta:{task_type}:sr:{i}"
        )])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"ta:{task_type}:cancel")])
    await update.message.reply_text(
        f"Found *{len(results)}* match(es). Select one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
async def _post_init(app) -> None:
    _restore_schedules(app)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    # Text filter that excludes the cancel button (so it reaches fallbacks)
    not_cancel = filters.TEXT & ~filters.COMMAND & ~_CANCEL_FILTER

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("assign", cmd_assign),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_ASSIGN)}$"), cmd_assign),
        ],
        states={
            S.INPUT_METHOD:       [CallbackQueryHandler(recv_input_method, pattern=r"^input:")],
            S.REF_NUMBERS:        [MessageHandler(not_cancel, recv_refs)],
            S.RECV_PHOTOS:        [
                MessageHandler(filters.PHOTO, recv_photo),
                CallbackQueryHandler(recv_photo_done, pattern=r"^photo:done$"),
            ],
            S.CONFIRM_REFS:       [CallbackQueryHandler(recv_confirm_refs, pattern=r"^refs:")],
            S.REASSIGN_CONFIRM:   [CallbackQueryHandler(recv_reassign_confirm, pattern=r"^reassign:")],
            S.PICK_VALUER_SOURCE: [CallbackQueryHandler(recv_valuer_source, pattern=r"^src:")],
            S.VALUER_NAME:        [MessageHandler(not_cancel, recv_valuer_name)],
            S.CHOOSE_CRED:        [CallbackQueryHandler(recv_cred_choice, pattern=r"^cred:")],
            S.WAIT_OTP:           [MessageHandler(not_cancel, recv_otp)],
            S.SELECT_VALUER:      [CallbackQueryHandler(recv_valuer_select, pattern=r"^valuer:")],
            S.CONFIRM:            [CallbackQueryHandler(recv_confirm, pattern=r"^confirm:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    recv_conv = ConversationHandler(
        entry_points=[
            CommandHandler("receive", cmd_receive),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_RECEIVE)}$"), cmd_receive),
        ],
        states={
            RS.STAFF_NAME:        [MessageHandler(not_cancel, recv_rt_staff_name)],
            RS.PICK_STAFF_SOURCE: [CallbackQueryHandler(recv_rt_pick_source, pattern=r"^rt_src:")],
            RS.SELECT_STAFF:      [CallbackQueryHandler(recv_rt_select_staff, pattern=r"^rt_staff:")],
            RS.CHOOSE_CRED:       [CallbackQueryHandler(recv_rt_cred_choice, pattern=r"^cred:")],
            RS.WAIT_OTP:          [MessageHandler(not_cancel, recv_rt_otp)],
            RS.TASK_TYPE:         [CallbackQueryHandler(recv_rt_task_type, pattern=r"^rt_type:")],
            RS.TASK_COUNT:        [MessageHandler(not_cancel, recv_rt_task_count)],
            RS.AMOUNT_RANGE:      [CallbackQueryHandler(recv_rt_amount_choice, pattern=r"^rt_amount:")],
            RS.AMOUNT_TEXT:       [MessageHandler(not_cancel, recv_rt_amount_range)],
            RS.SCHEDULE_CHOICE:   [CallbackQueryHandler(recv_rt_schedule_choice, pattern=r"^rt_sched:")],
            RS.SCHEDULE_INTERVAL: [MessageHandler(not_cancel, recv_rt_schedule_interval)],
            RS.RT_CONFIRM:        [CallbackQueryHandler(recv_rt_confirm, pattern=r"^rt_confirm:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("valuers",       cmd_valuers))
    app.add_handler(CommandHandler("delete_valuer", cmd_delete_valuer))
    app.add_handler(CommandHandler("schedules",     cmd_schedules))
    app.add_handler(CommandHandler("task_batches",  cmd_task_batches))
    app.add_handler(CommandHandler("daemon",        cmd_daemon))
    app.add_handler(conv)
    app.add_handler(recv_conv)

    # Button handlers outside an active conversation
    app.add_handler(CallbackQueryHandler(recv_delete_valuer,  pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(recv_daemon_action,  pattern=r"^daemon:"))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DAEMON)}$"),      cmd_daemon))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HELP)}$"),        cmd_help))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_VALUERS)}$"),     cmd_valuers))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DELETE)}$"),      cmd_delete_valuer))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_IMPL_TASKS)}$"),  cmd_impl_tasks))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DLV_TASKS)}$"),   cmd_dlv_tasks))
    app.add_handler(CallbackQueryHandler(recv_impl_check, pattern=r"^impl_ck:"))
    app.add_handler(CallbackQueryHandler(recv_dlv_check,  pattern=r"^dlv_ck:"))
    app.add_handler(CallbackQueryHandler(recv_ta_valuer,  pattern=r"^ta:"))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"),      cmd_cancel))
    # Lowest-priority: catch valuer name text input during task-assign search
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & ~_MENU_BUTTON_FILTER, handle_ta_search),
        group=1,
    )

    logger.info("Bot started. Polling for updates…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
