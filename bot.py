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
import time
from dataclasses import dataclass, field
from enum import Enum, auto
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
    """Return cached AuthTokens if still valid (60 s buffer), else None."""
    entry = _load_tokens_raw().get(cred_type)
    if not entry:
        return None
    if entry.get("expires_at", 0) < time.time() + 60:
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


CRED_MAP = {
    "publicuser": PUBLIC_CREDENTIALS,
    "staff":      STAFF_CREDENTIALS_ICT,
    "staff2":     STAFF_CREDENTIALS_SUPPORT,
}

CRED_LABELS = {
    "publicuser": "👤 Public User",
    "staff":      "🏢 ICT",
    "staff2":     "🏢 Support Reg",
}

# ──────────────────────────────────────────────────────────
# Main menu button labels & keyboard
# ──────────────────────────────────────────────────────────
BTN_ASSIGN  = "📋 New Assignment"
BTN_VALUERS = "👥 Saved Valuers"
BTN_DELETE  = "🗑 Delete Valuer"
BTN_HELP    = "❓ Help"
BTN_CANCEL  = "🛑 Cancel"

# Filter that matches any of the persistent menu button texts
_MENU_BUTTON_FILTER = filters.Regex(
    f"^({re.escape(BTN_ASSIGN)}|{re.escape(BTN_VALUERS)}|{re.escape(BTN_DELETE)}"
    f"|{re.escape(BTN_HELP)}|{re.escape(BTN_CANCEL)})$"
)
_CANCEL_FILTER = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")


def _main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_ASSIGN)],
            [KeyboardButton(BTN_VALUERS), KeyboardButton(BTN_DELETE)],
            [KeyboardButton(BTN_HELP),    KeyboardButton(BTN_CANCEL)],
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
        [InlineKeyboardButton(CRED_LABELS["publicuser"], callback_data="cred:publicuser")],
        [InlineKeyboardButton(CRED_LABELS["staff"],      callback_data="cred:staff")],
        [InlineKeyboardButton(CRED_LABELS["staff2"],     callback_data="cred:staff2")],
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
# Main
# ──────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

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
    )

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("valuers",       cmd_valuers))
    app.add_handler(CommandHandler("delete_valuer", cmd_delete_valuer))
    app.add_handler(conv)

    # Button handlers outside an active conversation
    app.add_handler(CallbackQueryHandler(recv_delete_valuer, pattern=r"^del:"))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HELP)}$"),    cmd_help))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_VALUERS)}$"), cmd_valuers))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DELETE)}$"),  cmd_delete_valuer))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"),  cmd_cancel))

    logger.info("Bot started. Polling for updates…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
