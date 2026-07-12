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
import asyncio
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import anthropic
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
import pytesseract
import requests
from dotenv import load_dotenv
from PIL import Image
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
    AuthTokens,
    build_session,
)

from common import (
    BASE_URL,
    BTN_ASSIGN,
    BTN_ASSIGNMENTS,
    BTN_BULK_EXPORT,
    BTN_CANCEL,
    BTN_DAEMON,
    BTN_DELETE,
    BTN_ERROR_REPORT,
    BTN_EXPORT_STATUS,
    BTN_HELP,
    BTN_RESTART,
    BTN_TOKEN_STATUS,
    BTN_VALUERS,
    CPARAMS_DLV,
    CRED_LABELS,
    CRED_MAP,
    DATA_DIR,
    SAVED_VALUERS_FILE,
    _any_valid_tokens,
    _atomic_json_write,
    _CANCEL_FILTER,
    _cred_keyboard,
    _ensure_data_dir,
    _jwt_exp,
    _load_tokens_raw,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    load_saved_assignments,
    load_saved_valuers,
    logger,
    not_cancel,
    persist_assignment,
    persist_tokens,
    persist_valuer,
    _safe_err,
)
from email_service import _send_bulk_export_email
from job_distribution import _JD_STATUS
from lookup_reference import _lu_fetch_detail, _lu_format_result, _lu_search_ref
from token_rotator import _AllTokensExhausted, _TokenRotator
import auto_fetch
import dlv_batch
import dlv_tasks
import morning_briefing
import fetch_tasks
import job_distribution
import lookup_reference
import receive_tasks
import refresh_auth
import sectional_properties
import valuer_tasks

load_dotenv()

BOT_TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ──────────────────────────────────────────────────────────
# Persistent storage
# ──────────────────────────────────────────────────────────
SAVED_TASK_BATCHES_FILE = os.path.join(DATA_DIR, "saved_task_batches.json")
SAVED_SCHEDULES_FILE    = os.path.join(DATA_DIR, "saved_schedules.json")
SAVED_BULK_EXPORT_SCHED_FILE    = os.path.join(DATA_DIR, "saved_bulk_export_schedule.json")
SAVED_BULK_EXPORT_PARTIAL_FILE  = os.path.join(DATA_DIR, "saved_bulk_export_partial.json")

DAEMON_SCRIPT = os.path.join(os.path.dirname(__file__), "token_refresh_daemon.py")
DAEMON_PID_FILE = os.path.join(DATA_DIR, "daemon.pid")
DAEMON_LOG_FILE = os.path.join(DATA_DIR, "daemon.log")

# (_safe_err, SAVED_SECTIONAL_CONFIG_FILE, load_sectional_config,
#  save_sectional_config all live in common.py now — imported above)


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


# (RS enum + RTSession live in receive_tasks.py — imported by main() at
#  the point of registration)


# (AF enum lives in auto_fetch.py — imported by main() at the point of
#  registration)


# (DB enum + DBSession live in dlv_batch.py — imported by main() at the
#  point of registration)


# (AS enum + AuthSession + _get_auth_sess live in refresh_auth.py —
#  imported by main() at the point of registration)


# (FT enum + FTSession + _get_ft_sess live in fetch_tasks.py — imported by
#  main() at the point of registration)


# (JD enum + JDSession live in job_distribution.py — imported by main() at
#  the point of registration)


# (LU enum + LUSession live in lookup_reference.py — imported by main() at
#  the point of registration)


# (VT enum + VTSession live in valuer_tasks.py — imported by main() at
#  the point of registration)


# States — Bulk Export conversation
# ──────────────────────────────────────────────────────────
class BE(Enum):
    REPORT_TYPE = auto()  # pick Ardhisasa or Ardhipay report
    COUNTY    = auto()   # pick county
    EMAIL     = auto()   # ask for recipient email address
    SCHEDULE  = auto()   # pick repeat interval
    CONFIRM   = auto()   # confirm → kick off background export


# (DT enum lives in dlv_tasks.py — imported by main() at the point of
#  registration)


# (SC enum lives in sectional_properties.py — imported by main() at the
#  point of registration)


# (MB enum lives in morning_briefing.py — imported by main() at the point
#  of registration)


# County → list of registry names as they appear in the API response
_BE_COUNTY_REGISTRIES: Dict[str, List[str]] = {
    "NAIROBI":   ["CENTRAL", "NAIROBI"],
    "KIAMBU":    ["KIAMBU", "LIMURU", "THIKA", "RUIRU", "GITHUNGURI"],
    "MURANGA":   ["MURANGA", "KANDARA", "MARAGUA", "KANGEMA"],
    "MOMBASA":   ["MOMBASA", "COAST"],
    "NAKURU":    ["NAKURU", "NAIVASHA", "GILGIL", "MOLO"],
    "KISUMU":    ["KISUMU", "MASENO"],
    "NYERI":     ["NYERI", "KARATINA", "OTHAYA"],
    "MACHAKOS":  ["MACHAKOS", "MAVOKO"],
    "KAJIADO":   ["KAJIADO", "NGONG"],
    "MERU":      ["MERU"],
    "LAIKIPIA":  ["LAIKIPIA", "NANYUKI"],
    "EMBU":      ["EMBU"],
}

_BE_COUNTY_LABELS: Dict[str, str] = {
    "NAIROBI":   "🌆 Nairobi",
    "KIAMBU":    "🏙 Kiambu",
    "MURANGA":   "🏡 Murang'a",
    "MOMBASA":   "🌊 Mombasa",
    "NAKURU":    "🌿 Nakuru",
    "KISUMU":    "🐟 Kisumu",
    "NYERI":     "🏔 Nyeri",
    "MACHAKOS":  "🦏 Machakos",
    "KAJIADO":   "🌄 Kajiado",
    "MERU":      "🌲 Meru",
    "LAIKIPIA":  "🦁 Laikipia",
    "EMBU":      "🌱 Embu",
}


# Ardhisasa report — restricted county set with specific registry lists
_AR_COUNTY_REGISTRIES: Dict[str, List[str]] = {
    "NAIROBI":  ["NAIROBI"],
    "MOMBASA":  ["MOMBASA", "COAST"],
    "ISIOLO":   ["ISIOLO"],
    "MURANGA":  ["MURANGA", "KANDARA", "MARAGUA", "KANGEMA"],
}

_AR_COUNTY_LABELS: Dict[str, str] = {
    "NAIROBI":  "🌆 Nairobi",
    "MOMBASA":  "🌊 Mombasa",
    "ISIOLO":   "🌵 Isiolo",
    "MURANGA":  "🏡 Murang'a",
}


_BE_SCHEDULE_OPTIONS: List[tuple] = [
    ("Every Day",      86_400),
    ("Every Week",     604_800),
    ("Bi-Monthly",     1_209_600),   # every 2 weeks
    ("Monthly",        2_592_000),   # 30 days
    ("Every 2 Months", 5_184_000),   # 60 days
    ("Run Once",       0),
]

# Per-chat export status tracker — keyed by chat_id
# Each entry: {phase, started_at, total, pages_done, total_pages,
#              details_done, details_total, errors, completed_at, rows, error_msg}
_BE_STATUS: Dict[int, dict] = {}

# (_JD_STATUS lives in job_distribution.py — imported above; cmd_export_status
#  below reads both it and _BE_STATUS, since Bulk Export isn't extracted yet)


@dataclass
class BESession:
    report_type:      str = ""   # "ardhisasa" or "ardhipay"
    county:           str = ""
    registries:       List[str] = field(default_factory=list)
    email:            str = ""
    schedule_seconds: int = 0   # 0 = run once
    cred_type:        str = ""


def _get_be_sess(ctx: ContextTypes.DEFAULT_TYPE) -> BESession:
    if "be_session" not in ctx.user_data:
        ctx.user_data["be_session"] = BESession()
    return ctx.user_data["be_session"]


# (CRED_MAP, CRED_LABELS, _cred_keyboard live in common.py — imported above)

# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────
def get_sess(ctx: ContextTypes.DEFAULT_TYPE) -> Session:
    if "session" not in ctx.user_data:
        ctx.user_data["session"] = Session()
    return ctx.user_data["session"]


def parse_refs(raw: str) -> List[str]:
    return [r.strip() for r in re.split(r"[\n,]+", raw) if r.strip()]


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
async def cmd_assignments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    assignments = load_saved_assignments()
    if not assignments:
        await update.message.reply_text(
            "📭 No assignments recorded yet.",
            reply_markup=_main_menu(),
        )
        return

    # Sort newest first
    sorted_items = sorted(
        assignments.items(),
        key=lambda kv: kv[1].get("assigned_at", ""),
        reverse=True,
    )

    lines = []
    for ref, info in sorted_items:
        valuer = info.get("valuer_name", "Unknown")
        when   = info.get("assigned_at", "—")
        lines.append(f"• `{ref}`\n  👤 {valuer} | 🕐 {when}")

    header = f"📜 *Assignments ({len(lines)} total)*\n\n"
    chunks = []
    chunk  = header
    for line in lines:
        candidate = (chunk + line + "\n\n").strip()
        if len(candidate) > 4000:
            chunks.append(chunk)
            chunk = line + "\n\n"
        else:
            chunk = candidate + "\n"
    if chunk.strip():
        chunks.append(chunk)

    for i, c in enumerate(chunks):
        await update.message.reply_text(
            c,
            parse_mode="Markdown",
            reply_markup=_main_menu() if i == len(chunks) - 1 else None,
        )


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
    _atomic_json_write(SAVED_VALUERS_FILE, valuers, indent=2)
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
        idx = int(data)
        if idx >= len(saved):
            await query.edit_message_text(
                "⚠️ That saved valuer no longer exists. Please start over.",
                reply_markup=_main_menu(),
            )
            return ConversationHandler.END
        sv = saved[idx]
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

        details      = data.get("details", {})
        access_token = details.get("access_token")
        jwt          = details.get("jwt")
        refresh_token = details.get("refresh_token", "")
        if not access_token or not jwt:
            raise RuntimeError(f"Tokens missing. Keys: {list(data.keys())}")

        sess.tokens = AuthTokens(access_token=access_token, jwt=jwt)
        persist_tokens(sess.cred_type, access_token, jwt, refresh_token)

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
            logger.error("Assignment failed for %s: %s", ref, e)
            fail_refs.append(ref)
            result_lines.append(f"❌ `{ref}` — {_safe_err(e)}")

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

    await query.message.reply_text(summary, parse_mode="Markdown")

    if fail_refs:
        await query.message.reply_text(
            "⚠️ Some assignments failed. Tap *📋 New Assignment* to retry failed refs.",
            parse_mode="Markdown",
        )

    if ok_refs:
        await query.message.reply_text("🔍 Fetching post-assignment status…", parse_mode="Markdown")
        pages = await asyncio.to_thread(_post_assignment_report, sess.tokens, ok_refs)
        for i, page in enumerate(pages):
            markup = _main_menu() if i == len(pages) - 1 else None
            await query.message.reply_text(page, parse_mode="Markdown", reply_markup=markup)

    return ConversationHandler.END


# (Receive Tasks — RS enum, RTSession, persistence helpers, API helpers,
#  _do_assign_tasks, the scheduled job, all conversation handlers,
#  cmd_schedules, cmd_task_batches — live in receive_tasks.py, imported
#  by main() at the point of registration)


# (AF Results — cmd_af_results, recv_af_result_detail — live in
#  auto_fetch.py, bundled with Auto Fetch since it only displays what
#  Auto Fetch's own background job persisted)

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
    """
    Return True if the daemon process is alive.

    A container rebuild resets PIDs from scratch, so a stale daemon.pid left
    over in the (persistent) data volume can coincidentally collide with an
    unrelated live process — e.g. the bot's own PID. Matching /proc/<pid>/cmdline
    against the daemon script guards against that false positive on Linux;
    elsewhere it falls back to a plain liveness check.
    """
    pid = _daemon_read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)   # signal 0 = probe only, no actual signal sent
    except (ProcessLookupError, PermissionError):
        return False

    cmdline_path = f"/proc/{pid}/cmdline"
    if os.path.exists(cmdline_path):
        try:
            with open(cmdline_path, "rb") as f:
                cmdline = f.read().decode(errors="ignore")
            return os.path.basename(DAEMON_SCRIPT) in cmdline
        except OSError:
            pass
    return True


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
    log_fh.close()   # child has its own fd; parent's copy is no longer needed
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


async def cmd_token_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)

    raw   = _load_tokens_raw()
    now   = time.time()
    lines = []

    for cred_type, label in CRED_LABELS.items():
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

    text = "🔒 *Token Status*\n\n" + "\n\n".join(lines)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_main_menu())


_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2},\d{3} \[(WARNING|ERROR|CRITICAL)\] [\w.]+: (.*)$"
)
_LOG_NORMALIZE_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\d+"
)


def _read_log_records(max_lines: int = 50_000) -> List[tuple]:
    """Parse data/bot.log into (date_str, level, message) tuples for WARNING+ lines."""
    log_path = os.path.join(DATA_DIR, "bot.log")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
    except FileNotFoundError:
        return []

    records = []
    for line in lines:
        m = _LOG_LINE_RE.match(line)
        if m:
            records.append((m.group(1), m.group(2), m.group(3)))
    return records


async def cmd_error_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)

    records = _read_log_records()
    if not records:
        await update.message.reply_text(
            "✅ No warnings or errors logged yet (`data/bot.log`).",
            reply_markup=_main_menu(),
        )
        return

    from collections import Counter, defaultdict

    by_day_level: dict = defaultdict(lambda: {"WARNING": 0, "ERROR": 0, "CRITICAL": 0})
    categories = Counter()
    for date_str, level, message in records:
        by_day_level[date_str][level] += 1
        category = _LOG_NORMALIZE_RE.sub("#", message)[:80]
        categories[category] += 1

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        await update.message.reply_text(
            "❌ `matplotlib` is not installed — cannot render the chart.",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return

    days = sorted(by_day_level.keys())
    warnings = [by_day_level[d]["WARNING"] for d in days]
    errors   = [by_day_level[d]["ERROR"] + by_day_level[d]["CRITICAL"] for d in days]

    top_categories = categories.most_common(8)[::-1]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 9))

    ax1.bar(days, warnings, label="Warning", color="#f1c40f")
    ax1.bar(days, errors, bottom=warnings, label="Error/Critical", color="#e74c3c")
    ax1.set_title("Log entries per day")
    ax1.set_ylabel("Count")
    ax1.legend()
    ax1.tick_params(axis="x", rotation=45)

    if top_categories:
        labels = [c for c, _ in top_categories]
        counts = [n for _, n in top_categories]
        ax2.barh(labels, counts, color="#3498db")
        ax2.set_title("Top error/warning categories")
        ax2.set_xlabel("Count")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)

    total = sum(warnings) + sum(errors)
    caption = (
        f"📉 *Error Report* — {total:,} entries across {len(days)} day(s)\n"
        f"Range: `{days[0]}` → `{days[-1]}`"
    )
    await update.message.reply_photo(
        photo=buf, caption=caption, parse_mode="Markdown", reply_markup=_main_menu()
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
# /restart
# ──────────────────────────────────────────────────────────
async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text("🔁 Restarting bot… back in a moment.")
    # Schedule execv in a daemon thread so the event loop can finish delivering
    # the reply above before the process image is replaced.  os.execv never
    # returns, so dropping the thread is intentional.
    def _do_restart():
        time.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    t = threading.Thread(target=_do_restart, daemon=True)
    t.start()


# ──────────────────────────────────────────────────────────
# Fallback (unexpected input during conversation)
# ──────────────────────────────────────────────────────────
# (fallback() lives in common.py — imported above)


# (DLV Batch parsing/valuer-resolution, batch item processing, and the
#  5-minute repeating job all live in dlv_batch.py — imported by main()
#  at the point of registration)


# (Auto Fetch — _AF_INTERVALS, load_auto_fetch_schedule/
#  save_auto_fetch_schedule/clear_auto_fetch_schedule, load_af_results/
#  persist_af_result, _af_interval_keyboard, cmd_auto_fetch, recv_af_*,
#  _auto_fetch_job — all live in auto_fetch.py)


# (Morning Briefing — _send_briefing, _run_morning_briefing,
#  _morning_briefing_job, _schedule_morning_briefing, _mb_* keyboards,
#  cmd_briefing, recv_mb_* — all live in morning_briefing.py)

# (Sectional Properties command handlers — cmd_sectional, recv_sc_action,
#  recv_sc_name, recv_sc_select, recv_sc_cred — live in sectional_properties.py)


# (DLV Queue viewer — _dlv_queue_keyboard, cmd_dlv_queue,
#  recv_dlv_queue_action, _dlv_batch_interval — lives in dlv_batch.py)


# ──────────────────────────────────────────────────────────
# Implementor Tasks & DLV Tasks — view-only checklist
# ──────────────────────────────────────────────────────────
# (_any_valid_tokens, _ft_headers, _extract_assessor live in common.py /
#  dlv_core.py — imported above)

# (Fetch Tasks — _load_fetch_tasks, cmd_fetch_tasks, recv_ft_*,
#  _ft_do_fetch, _ft_show_results, etc. — lives in fetch_tasks.py.
#  _date_cutoff_str/_within_days, _ft_*_keyboard/_sectional_keyboard
#  moved to common.py since Auto Fetch/Valuer Tasks also use them.)


# (DLV Batch conversation handlers — cmd_dlv_batch, recv_db_input,
#  recv_db_confirm, _run_dlv_batch_bg — live in dlv_batch.py)

# (Refresh Auth conversation handlers — _auth_cred_keyboard, cmd_auth,
#  recv_auth_cred, recv_auth_force, _auth_trigger_login, recv_auth_otp —
#  live in refresh_auth.py)

# ──────────────────────────────────────────────────────────
# Bulk Export — NAIROBI Completed stamp-duty applications
# ──────────────────────────────────────────────────────────

_BE_LIST_URL   = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application"
_BE_DETAIL_URL = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application/detail-view"
_BE_REPORT_URL = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/office-reports/get-current-office-report"
_BE_PAGE_SIZE  = 10   # API default
_BE_LIST_WORKERS        = 5
_BE_DETAIL_WORKERS      = 5
_BE_MAX_RETRIES         = 3
_BE_TOKEN_ROTATE_DELAY  = 10   # seconds to wait before retrying with a new token
_MAX_FETCH_RETRIES      = 5    # max 429/5xx retries before aborting a fetch loop


# (_AllTokensExhausted + _TokenRotator live in token_rotator.py — imported
#  above, shared by Bulk Export and Job Distribution)


_EXCEL_COLUMNS = [
    "Filter",
    "Reference Number",
    "Parcel Number",
    "Registry",
    "County",
    "Valuation Request Type",
    "Application Status",
    "Application Date Created",
    "Valuation Officer",
    "Date of Valuation",
    "Valuer Total Land Value (KES)",
    "Harmonized Total Land Value (KES)",
    "Document URL",
    "Combined Report",
    "Enrich Error",
]


def _be_headers(tokens: AuthTokens) -> dict:
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }


def load_be_schedule() -> Optional[Dict]:
    try:
        with open(SAVED_BULK_EXPORT_SCHED_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_be_schedule(cfg: Dict) -> None:
    _atomic_json_write(SAVED_BULK_EXPORT_SCHED_FILE, cfg, indent=2)


def clear_be_schedule() -> None:
    try:
        os.remove(SAVED_BULK_EXPORT_SCHED_FILE)
    except FileNotFoundError:
        pass


def load_be_partial() -> Optional[Dict]:
    try:
        with open(SAVED_BULK_EXPORT_PARTIAL_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_be_partial(county: str, registries: List[str], rows: List[dict], done_ids: List[str]) -> None:
    _atomic_json_write(SAVED_BULK_EXPORT_PARTIAL_FILE, {
        "saved_at":   datetime.now().isoformat(),
        "county":     county,
        "registries": registries,
        "rows":       rows,
        "done_ids":   done_ids,
    })


def clear_be_partial() -> None:
    try:
        os.remove(SAVED_BULK_EXPORT_PARTIAL_FILE)
    except FileNotFoundError:
        pass


def _be_schedule_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for label, secs in _BE_SCHEDULE_OPTIONS:
        rows.append([InlineKeyboardButton(label, callback_data=f"be_sched:{secs}")])
    return InlineKeyboardMarkup(rows)


def _be_report_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏛 Ardhisasa Report", callback_data="be_rtype:ardhisasa")],
        [InlineKeyboardButton("💳 Ardhipay Report",  callback_data="be_rtype:ardhipay")],
    ])


def _be_county_keyboard() -> InlineKeyboardMarkup:
    items = list(_BE_COUNTY_LABELS.items())
    rows = []
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(items[i][1], callback_data=f"be_county:{items[i][0]}")]
        if i + 1 < len(items):
            row.append(InlineKeyboardButton(items[i+1][1], callback_data=f"be_county:{items[i+1][0]}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _ar_county_keyboard() -> InlineKeyboardMarkup:
    items = list(_AR_COUNTY_LABELS.items())
    rows = []
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(items[i][1], callback_data=f"be_county:{items[i][0]}")]
        if i + 1 < len(items):
            row.append(InlineKeyboardButton(items[i+1][1], callback_data=f"be_county:{items[i+1][0]}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# (_be_cred_keyboard lives in common.py, shared by Job Distribution,
#  Lookup Reference, and Valuer Tasks — Bulk Export, still inline here,
#  uses its own _be_report_type_keyboard/_be_county_keyboard instead)


def _be_fetch_page(sess: requests.Session, headers: dict, page: int) -> dict:
    """Fetch one list page. Returns the parsed JSON dict."""
    params = {
        "filter":       "Completed",
        "role":         "DLV",
        "request_type": "STAMP_DUTY",
        "search":       "",
        "page":         page,
    }
    resp = sess.get(_BE_LIST_URL, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _be_fetch_detail(sess: requests.Session, rotator: "_TokenRotator", app_id: str) -> dict:
    """
    Fetch detail view for one application ID.
    On 403 rotates to the next valid token (with a short pause).
    Raises _AllTokensExhausted when no more tokens remain.
    """
    retries = 0
    while True:
        tokens = rotator.current()
        if tokens is None:
            raise _AllTokensExhausted(f"All tokens exhausted fetching {app_id}")
        headers = {**_be_headers(tokens), "cparams": CPARAMS_DLV}
        resp = sess.get(_BE_DETAIL_URL, headers=headers, params={"request_id": app_id}, timeout=30)
        if resp.status_code == 403:
            new_tokens = rotator.rotate(tokens)
            if new_tokens is None:
                raise _AllTokensExhausted(f"All tokens exhausted (403) fetching {app_id}")
            time.sleep(_BE_TOKEN_ROTATE_DELAY)
            continue
        if resp.status_code == 404:
            # Record no longer exists — return empty dict so caller marks it cleanly
            logger.debug("_be_fetch_detail: 404 for app_id=%s — record not found", app_id)
            return {}
        if resp.status_code in (429, 500, 502, 503, 504):
            retries += 1
            if retries >= _MAX_FETCH_RETRIES:
                raise RuntimeError(
                    f"_be_fetch_detail: too many transient errors (HTTP {resp.status_code}) "
                    f"for app_id={app_id}"
                )
            time.sleep(2)
            continue
        resp.raise_for_status()
        return resp.json()


def _be_fetch_office_report(sess: requests.Session, rotator: "_TokenRotator", app_id: str) -> dict:
    """Fetch the office report for one application. Returns {} on any error (non-critical)."""
    retries = 0
    while True:
        tokens = rotator.current()
        if tokens is None:
            return {}
        headers = {**_be_headers(tokens), "cparams": CPARAMS_DLV}
        try:
            resp = sess.get(_BE_REPORT_URL, headers=headers, params={"request_id": app_id}, timeout=30)
            if resp.status_code == 403:
                new_tokens = rotator.rotate(tokens)
                if new_tokens is None:
                    return {}
                time.sleep(_BE_TOKEN_ROTATE_DELAY)
                continue
            if resp.status_code in (429, 502, 503, 504):
                retries += 1
                if retries >= _MAX_FETCH_RETRIES:
                    logger.warning("_be_fetch_office_report: giving up after %d retries for app_id=%s", retries, app_id)
                    return {}
                time.sleep(2)
                continue
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return resp.json()
        except (_AllTokensExhausted, Exception):
            return {}


def _be_fetch_full_record(sess: requests.Session, rotator: "_TokenRotator", app_id: str) -> dict:
    """Fetch detail + office report for one record and return the merged row dict."""
    detail = _be_fetch_detail(sess, rotator, app_id)   # raises on error
    report = _be_fetch_office_report(sess, rotator, app_id)
    row    = _be_extract_row(detail)
    docs   = report.get("combined_document") or []
    row["Combined Report"] = docs[0].get("document", "") if docs else ""
    return row


def _be_extract_row(detail: dict) -> dict:
    """Extract the target columns from a detail-view response dict."""
    # Valuation Officer from actors[]
    vo_name = ""
    vo_date = ""
    for actor in (detail.get("actors") or []):
        role = (actor.get("role") or "").upper()
        if role in ("VALUATION OFFICER", "VO"):
            vo_name = (actor.get("user_details") or {}).get("names", "")
            vo_date = actor.get("date_assigned", "")
            if role == "VALUATION OFFICER":
                break   # prefer exact match

    # Consideration amount from external_process_details
    ext = detail.get("external_process_details") or {}
    land_value = ext.get("consideration_amount", "")

    # Document URL: prefer VALUATION CERTIFICATE in process_documents
    doc_url = ""
    for pdoc in (detail.get("process_documents") or []):
        if (pdoc.get("document_name") or "").upper() == "VALUATION CERTIFICATE":
            doc_url = pdoc.get("document", "")
            break
    if not doc_url:
        app_docs = detail.get("application_documents") or []
        if app_docs:
            doc_url = app_docs[0].get("document", "")

    return {
        "Filter":                          "Completed",
        "Reference Number":                detail.get("reference_number", ""),
        "Parcel Number":                   detail.get("parcel_number", ""),
        "Registry":                        detail.get("registry", ""),
        "County":                          detail.get("county", ""),
        "Valuation Request Type":          detail.get("valuation_request_type", ""),
        "Application Status":              detail.get("application_status", ""),
        "Application Date Created":        detail.get("date_created", ""),
        "Valuation Officer":               vo_name,
        "Date of Valuation":               vo_date,
        "Valuer Total Land Value (KES)":   land_value,
        "Harmonized Total Land Value (KES)": detail.get("harmonized_total_land_value", ""),
        "Document URL":                    doc_url,
        "Combined Report":                 "",
        "Enrich Error":                    "",
    }


def _be_build_excel(rows: List[dict]) -> bytes:
    """Build the formatted Excel workbook and return the raw bytes."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Stamp Duty Valuations"

    header_font  = Font(bold=True)
    header_fill  = PatternFill("solid", fgColor="BDD7EE")
    date_fmt     = "YYYY-MM-DD HH:MM:SS"
    number_fmt   = "#,##0"
    date_cols    = {"Application Date Created", "Date of Valuation"}
    number_cols  = {"Valuer Total Land Value (KES)", "Harmonized Total Land Value (KES)"}

    # Write header
    ws.append(_EXCEL_COLUMNS)
    for col_idx, col_name in enumerate(_EXCEL_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font  = header_font
        cell.fill  = header_fill

    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes    = "A2"

    # Write data rows
    for row_data in rows:
        row_vals = [row_data.get(col, "") for col in _EXCEL_COLUMNS]
        ws.append(row_vals)
        row_idx = ws.max_row
        for col_idx, col_name in enumerate(_EXCEL_COLUMNS, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            if col_name in date_cols and cell.value:
                cell.number_format = date_fmt
            elif col_name in number_cols and cell.value not in ("", None, "FETCH_ERROR"):
                try:
                    cell.value         = float(str(cell.value).replace(",", "").strip())
                    cell.number_format = number_fmt
                except (ValueError, TypeError):
                    pass

    # Auto-fit column widths (min 15, max 50)
    for col_idx, col_name in enumerate(_EXCEL_COLUMNS, start=1):
        col_letter = get_column_letter(col_idx)
        max_len    = len(col_name)
        for row in ws.iter_rows(min_col=col_idx, max_col=col_idx, min_row=2):
            val = str(row[0].value or "")
            if len(val) > max_len:
                max_len = len(val)
        ws.column_dimensions[col_letter].width = max(15, min(50, max_len + 2))

    # ── Summary sheet ─────────────────────────────────────────
    ws2 = wb.create_sheet("Summary")

    def _write_section_header(ws, row: int, text: str):
        cell = ws.cell(row=row, column=1, value=text)
        cell.font = Font(bold=True, size=12)
        cell.fill = PatternFill("solid", fgColor="BDD7EE")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)

    def _write_col_headers(ws, row: int, *headers):
        for col, h in enumerate(headers, start=1):
            c = ws.cell(row=row, column=col, value=h)
            c.font = header_font
            c.fill = PatternFill("solid", fgColor="D9E1F2")

    # ── Collect stats from rows ───────────────────────────────
    from collections import defaultdict

    monthly: dict  = defaultdict(int)    # "YYYY-MM" → count
    yearly:  dict  = defaultdict(int)    # "YYYY"    → count
    vo_tasks: dict = defaultdict(int)    # officer name → count
    total_land_value = 0.0
    total_harmonized = 0.0

    for r in rows:
        # Date of Valuation — monthly/yearly distribution
        dov = str(r.get("Date of Valuation") or "")
        if len(dov) >= 7:
            monthly[dov[:7]] += 1
        if len(dov) >= 4:
            yearly[dov[:4]] += 1

        # Valuation Officer task count
        vo = (r.get("Valuation Officer") or "").strip()
        if vo and vo != "FETCH_ERROR":
            vo_tasks[vo] += 1

        # Sum land values
        for key, target in (
            ("Valuer Total Land Value (KES)", "land"),
            ("Harmonized Total Land Value (KES)", "harm"),
        ):
            raw = r.get(key, "")
            if raw not in ("", None, "FETCH_ERROR"):
                try:
                    val = float(str(raw).replace(",", "").strip())
                    if key == "Valuer Total Land Value (KES)":
                        total_land_value += val
                    else:
                        total_harmonized += val
                except (ValueError, TypeError):
                    pass

    cur_row = 1

    # ── Section 1: Totals ─────────────────────────────────────
    _write_section_header(ws2, cur_row, "Overall Totals")
    cur_row += 1
    for label, value in (
        ("Total Records",                        len(rows)),
        ("Valuer Total Land Value (KES)",        total_land_value),
        ("Harmonized Total Land Value (KES)",    total_harmonized),
    ):
        ws2.cell(row=cur_row, column=1, value=label).font = Font(bold=True)
        c = ws2.cell(row=cur_row, column=2, value=value)
        if isinstance(value, float):
            c.number_format = number_fmt
        cur_row += 1

    cur_row += 1  # blank row

    # ── Section 2: Monthly distribution ──────────────────────
    _write_section_header(ws2, cur_row, "Monthly Distribution (Date of Valuation)")
    cur_row += 1
    _write_col_headers(ws2, cur_row, "Month (YYYY-MM)", "Count")
    cur_row += 1
    for month in sorted(monthly):
        ws2.cell(row=cur_row, column=1, value=month)
        ws2.cell(row=cur_row, column=2, value=monthly[month])
        cur_row += 1

    cur_row += 1  # blank row

    # ── Section 3: Yearly distribution ───────────────────────
    _write_section_header(ws2, cur_row, "Yearly Distribution (Date of Valuation)")
    cur_row += 1
    _write_col_headers(ws2, cur_row, "Year", "Count")
    cur_row += 1
    for year in sorted(yearly):
        ws2.cell(row=cur_row, column=1, value=year)
        ws2.cell(row=cur_row, column=2, value=yearly[year])
        cur_row += 1

    cur_row += 1  # blank row

    # ── Section 4: Valuation Officer task counts ──────────────
    _write_section_header(ws2, cur_row, "Tasks per Valuation Officer")
    cur_row += 1
    _write_col_headers(ws2, cur_row, "Valuation Officer", "Tasks")
    cur_row += 1
    for officer, count in sorted(vo_tasks.items(), key=lambda x: -x[1]):
        ws2.cell(row=cur_row, column=1, value=officer)
        ws2.cell(row=cur_row, column=2, value=count)
        cur_row += 1

    # Auto-fit Summary sheet columns
    for col_idx in (1, 2):
        col_letter = get_column_letter(col_idx)
        max_len = 20
        for row in ws2.iter_rows(min_col=col_idx, max_col=col_idx):
            val = str(row[0].value or "")
            if len(val) > max_len:
                max_len = len(val)
        ws2.column_dimensions[col_letter].width = max(20, min(50, max_len + 2))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# (_send_bulk_export_email lives in common.py — imported above)


def _bulk_export_run(tokens: AuthTokens, chat_id: int, email: str, bot, loop,
                     registries: List[str] = None, county: str = "",
                     report_type: str = "ardhipay") -> None:
    """
    Full synchronous export worker — runs in a background thread.

    Behaviour:
    - Resumes from a saved partial checkpoint when one exists for the same county/registries.
    - Sorts all records by Application Date Created (ascending) so resume is date-ordered.
    - Rotates tokens on 403: tries every valid credential before giving up.
    - On full token exhaustion saves a partial checkpoint and notifies the user.
    - On success clears the partial checkpoint and sends the Excel file.
    """
    def _tg(text: str):
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id, text, parse_mode="Markdown"),
            loop,
        ).result(timeout=15)

    def _set_status(**kwargs):
        _BE_STATUS.setdefault(chat_id, {}).update(kwargs)

    _BE_STATUS[chat_id] = {
        "phase":          "fetching pages",
        "started_at":     datetime.now(),
        "total":          None,
        "pages_done":     1,
        "total_pages":    None,
        "details_done":   0,
        "details_total":  None,
        "errors":         0,
        "completed_at":   None,
        "rows":           None,
        "error_msg":      None,
    }

    # ── Build token rotator from all currently-valid credentials ──
    token_pairs = [
        (ct, get_valid_tokens(ct))
        for ct in CRED_MAP
        if get_valid_tokens(ct)
    ]
    # Put the originally-chosen token first
    token_pairs.sort(key=lambda p: 0 if p[1] is tokens else 1)
    rotator = _TokenRotator(token_pairs)

    sess = build_session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=_BE_DETAIL_WORKERS,
        pool_maxsize=_BE_DETAIL_WORKERS,
        max_retries=0,
    )
    sess.mount("https://", adapter)
    sess.mount("http://",  adapter)
    list_headers = _be_headers(tokens)
    if report_type == "ardhisasa":
        list_headers = {**list_headers, "cparams": CPARAMS_DLV, "Content-Type": "application/json"}

    try:
        # ── Step 1: fetch list pages ───────────────────────────────
        first_page  = _be_fetch_page(sess, list_headers, 1)
        total       = first_page.get("count", 0)
        results     = list(first_page.get("results") or [])
        page_size   = len(results) if results else _BE_PAGE_SIZE
        if page_size == 0:
            page_size = _BE_PAGE_SIZE
        total_pages = max(1, -(-total // page_size))

        logger.info("Bulk export: count=%d, page_size=%d, total_pages=%d", total, page_size, total_pages)
        _set_status(total=total, total_pages=total_pages, pages_done=1)

        if total_pages > 1:
            with ThreadPoolExecutor(max_workers=_BE_LIST_WORKERS) as pool:
                futures = {pool.submit(_be_fetch_page, sess, list_headers, p): p
                           for p in range(2, total_pages + 1)}
                for fut in _futures_as_completed(futures):
                    results.extend(fut.result().get("results") or [])
                    _set_status(pages_done=_BE_STATUS[chat_id]["pages_done"] + 1)

        # ── Client-side registry filter ────────────────────────────
        reg_set = {r.upper() for r in (registries or [])}
        if reg_set:
            filtered = [r for r in results if (r.get("registry") or "").upper() in reg_set]
        else:
            filtered = results

        # Sort by Application Date Created ascending so resume is date-ordered
        filtered.sort(key=lambda r: (r.get("date_created") or ""))

        # ── Resume: load partial checkpoint if it matches ──────────
        partial     = load_be_partial()
        resume_rows: List[dict] = []
        done_ids:    set        = set()

        if partial and partial.get("county") == county and \
                set(partial.get("registries", [])) == set(registries or []):
            resume_rows = partial.get("rows") or []
            done_ids    = set(partial.get("done_ids") or [])
            _tg(
                f"♻️ Resuming from checkpoint — {len(resume_rows):,} rows already saved, "
                f"{len(done_ids):,} IDs done."
            )

        id_list = [r["id"] for r in filtered if r.get("id") and r["id"] not in done_ids]

        if not id_list and not resume_rows:
            _set_status(phase="done", completed_at=datetime.now(), rows=0)
            _tg("ℹ️ Bulk export complete — no records found.")
            return

        if not id_list:
            # All IDs already done from checkpoint — skip straight to Excel
            rows = resume_rows
        else:
            # ── Step 2: parallel detail fetch with token rotation ──────
            _set_status(phase="fetching details", details_total=len(id_list))
            rows: List[dict] = list(resume_rows)
            current_done_ids = list(done_ids)
            exhausted_flag   = threading.Event()
            id_to_item       = {r["id"]: r for r in filtered if r.get("id")}

            _fetch_fn = _be_fetch_full_record
            with ThreadPoolExecutor(max_workers=_BE_DETAIL_WORKERS) as pool:
                futures = {pool.submit(_fetch_fn, sess, rotator, app_id): app_id
                           for app_id in id_list}
                for fut in _futures_as_completed(futures):
                    app_id = futures[fut]
                    try:
                        if exhausted_flag.is_set():
                            fut.cancel()
                            continue
                        row = fut.result()
                        rows.append(row)
                        current_done_ids.append(app_id)
                        _set_status(details_done=_BE_STATUS[chat_id]["details_done"] + 1)
                    except _AllTokensExhausted:
                        exhausted_flag.set()
                        logger.warning("Bulk export: all tokens exhausted at id=%s", app_id)
                    except Exception as exc:
                        logger.warning("Bulk export detail failed id=%s: %s", app_id, exc)
                        item = id_to_item.get(app_id, {})
                        rows.append({
                            "Filter":                          "Completed",
                            "Reference Number":                item.get("reference_number", ""),
                            "Parcel Number":                   item.get("parcel_number", ""),
                            "Registry":                        item.get("registry", ""),
                            "County":                          item.get("county", ""),
                            "Valuation Request Type":          item.get("valuation_request_type", ""),
                            "Application Status":              item.get("application_status", ""),
                            "Application Date Created":        item.get("date_created", ""),
                            "Valuation Officer":               "",
                            "Date of Valuation":                "",
                            "Valuer Total Land Value (KES)":   "",
                            "Harmonized Total Land Value (KES)": "",
                            "Document URL":                    "",
                            "Combined Report":                 "",
                            "Enrich Error":                    str(exc),
                        })
                        current_done_ids.append(app_id)
                        _set_status(
                            details_done=_BE_STATUS[chat_id]["details_done"] + 1,
                            errors=_BE_STATUS[chat_id]["errors"] + 1,
                        )

            if exhausted_flag.is_set():
                # Save whatever we managed to fetch and bail out
                save_be_partial(county, list(registries or []), rows, current_done_ids)
                remaining = len(id_list) - len(current_done_ids) + len(done_ids)
                _set_status(phase="paused — tokens exhausted", completed_at=datetime.now(), rows=len(rows))
                _tg(
                    f"⚠️ All tokens returned 403 — export paused.\n\n"
                    f"*Saved so far:* {len(rows):,} rows\n"
                    f"*Remaining:* ~{remaining:,} records\n\n"
                    "Refresh your tokens and run the export again to continue from this checkpoint."
                )
                return

        # ── Step 3: sort final rows by date, build Excel, send ────
        _set_status(phase="building excel")
        rows.sort(key=lambda r: (r.get("Application Date Created") or ""))

        clear_be_partial()
        rtype_label = "Ardhisasa" if report_type == "ardhisasa" else "Ardhipay"
        filename   = f"{rtype_label}_Valuation_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        xlsx_bytes = _be_build_excel(rows)

        asyncio.run_coroutine_threadsafe(
            bot.send_document(
                chat_id,
                document=io.BytesIO(xlsx_bytes),
                filename=filename,
                caption=f"📊 Bulk export complete — {len(rows):,} rows",
            ),
            loop,
        ).result(timeout=60)

        _set_status(phase="done", completed_at=datetime.now(), rows=len(rows))

        if email:
            try:
                _send_bulk_export_email(email, filename, xlsx_bytes)
                _tg(f"📧 File also sent to *{email}*.")
            except Exception as exc:
                logger.warning("Bulk export email failed: %s", exc)
                _tg(f"⚠️ Email delivery failed: `{exc}`")

    except Exception as exc:
        logger.error("Bulk export worker crashed: %s", exc, exc_info=True)
        _set_status(phase="failed", completed_at=datetime.now(), error_msg=str(exc))
        _tg(f"❌ Export failed: `{exc}`")


# (Job Distribution's URL/worker constants — _TEAMS_URL, _TEAM_MEMBERS_URL,
#  _JD_ONGOING_URL, _JD_DETAIL_URL, _JD_WORKERS — live in job_distribution.py)

# (LUSession, _get_lu_sess, _LU_SEARCH_COMBOS, _lu_search_ref,
#  _lu_fetch_detail, _lu_format_result live in lookup_reference.py —
#  imported above; _lu_search_ref/_lu_fetch_detail/_lu_format_result are
#  its public API, used below by _lookup_one_ref)


def _lookup_one_ref(tokens: AuthTokens, ref: str) -> str:
    """Search + detail-fetch for a single ref. Returns a formatted result string."""
    item = _lu_search_ref(tokens, ref)
    if not item:
        return f"⚠️ `{ref}` — not found in post-assignment lookup"
    detail = _lu_fetch_detail(tokens, item["id"])
    return _lu_format_result(ref, item, detail)


def _post_assignment_report(tokens: AuthTokens, ok_refs: List[str]) -> List[str]:
    """
    Run lookups for all successfully assigned refs in parallel and return a
    list of message strings (each ≤ 4000 chars) ready to send to Telegram.
    """
    results: Dict[str, str] = {}
    workers = min(len(ok_refs), 10)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fut_map = {pool.submit(_lookup_one_ref, tokens, ref): ref for ref in ok_refs}
        for fut in _futures_as_completed(fut_map):
            ref = fut_map[fut]
            try:
                results[ref] = fut.result()
            except Exception as e:
                results[ref] = f"⚠️ `{ref}` — lookup error: {e}"

    # Build paginated messages — start each page with the header
    header    = "📋 *Post-Assignment Verification*\n"
    divider   = "\n" + "─" * 30 + "\n"
    messages  = []
    current   = header

    for ref in ok_refs:
        block = results.get(ref, f"⚠️ `{ref}` — no result")
        chunk = divider + block if current != header else "\n" + block
        if len(current) + len(chunk) > 4000:
            messages.append(current.rstrip())
            current = header + "\n" + block
        else:
            current += chunk

    if current.strip() and current.strip() != header.strip():
        messages.append(current.rstrip())

    return messages if messages else [header + "\nNo results returned."]


# (Job Distribution — _JD_COUNTIES, JDSession, _get_jd_sess,
#  _jd_county_keyboard, _jd_headers, _jd_fetch_teams/_jd_fetch_team_members/
#  _jd_fetch_ongoing_page/_jd_fetch_task_detail, _jd_build_excel, _jd_run,
#  all conversation handlers — live in job_distribution.py, imported by
#  main() at the point of registration)


# (Lookup Reference conversation handlers — cmd_lookup, recv_lu_cred,
#  recv_lu_ref — live in lookup_reference.py, imported by main() at the
#  point of registration)


# (Valuer Tasks — VTSession, _get_vt_sess, _vt_fetch_all_tasks,
#  _vt_build_excel, _vt_run, all conversation handlers — live in
#  valuer_tasks.py, imported by main() at the point of registration)


# ── Bulk Export conversation handlers ─────────────────────

async def _bulk_export_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """APScheduler job: run bulk export with saved schedule config."""
    cfg = load_be_schedule()
    if not cfg:
        return
    cred_type = cfg.get("cred_type")
    tokens    = get_valid_tokens(cred_type) if cred_type else _any_valid_tokens()
    if not tokens:
        cred_label = CRED_LABELS.get(cred_type, cred_type or "any")
        logger.warning("Bulk export job: no valid tokens for %s — skipping.", cred_label)
        return
    chat_id     = cfg["chat_id"]
    email       = cfg.get("email", "")
    registries  = cfg.get("registries", [])
    county      = cfg.get("county", "")
    report_type = cfg.get("report_type", "ardhipay")
    loop        = asyncio.get_event_loop()
    asyncio.ensure_future(
        asyncio.to_thread(_bulk_export_run, tokens, chat_id, email, context.bot, loop,
                          registries, county, report_type)
    )


async def cmd_bulk_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    sess = _get_be_sess(ctx)
    sess.report_type = ""
    sess.county      = ""
    sess.registries  = []
    sess.email       = ""
    sess.cred_type   = ""
    await update.message.reply_text(
        "📤 *Export Valuation Report*\n\nSelect the report type:",
        parse_mode="Markdown",
        reply_markup=_be_report_type_keyboard(),
    )
    return BE.REPORT_TYPE


async def cmd_export_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    chat_id = update.effective_chat.id
    be_st   = _BE_STATUS.get(chat_id)
    jd_st   = _JD_STATUS.get(chat_id)

    if not be_st and not jd_st:
        await update.message.reply_text(
            "ℹ️ No export or analysis has been run in this session.",
            reply_markup=_main_menu(),
        )
        return

    def _elapsed(started_at, completed_at):
        if not started_at:
            return ""
        ref  = completed_at or datetime.now()
        secs = int((ref - started_at).total_seconds())
        return f"{secs // 60}m {secs % 60}s"

    phase_icons = {
        "fetching pages":        "📄",
        "fetching details":      "🔍",
        "fetching teams":        "🔍",
        "fetching ongoing tasks":"📋",
        "fetching task details": "🔍",
        "building excel":        "📊",
        "done":                  "✅",
        "failed":                "❌",
        "paused — tokens exhausted": "⏸",
    }

    all_lines = []

    if be_st:
        phase        = be_st.get("phase", "unknown")
        started_at   = be_st.get("started_at")
        completed_at = be_st.get("completed_at")
        icon         = phase_icons.get(phase, "⏳")
        lines        = [f"*{icon} Export Valuation Report*\n"]
        elapsed      = _elapsed(started_at, completed_at)
        if started_at:
            lines.append(f"Started: `{started_at.strftime('%H:%M:%S')}`")
        if elapsed:
            lines.append(f"Elapsed: `{elapsed}`")
        lines.append(f"Phase: `{phase}`")
        total = be_st.get("total")
        if total is not None:
            lines.append(f"Total records: `{total:,}`")
        total_pages  = be_st.get("total_pages")
        pages_done   = be_st.get("pages_done", 0)
        if total_pages is not None:
            lines.append(f"Pages: `{pages_done}/{total_pages}`")
        details_done  = be_st.get("details_done", 0)
        details_total = be_st.get("details_total")
        if details_total is not None:
            pct = int(details_done / details_total * 100) if details_total else 0
            lines.append(f"Details: `{details_done}/{details_total}` ({pct}%)")
        errors = be_st.get("errors", 0)
        if errors:
            lines.append(f"⚠️ Fetch errors: `{errors}`")
        rows = be_st.get("rows")
        if phase == "done" and rows is not None:
            lines.append(f"Rows exported: `{rows:,}`")
        error_msg = be_st.get("error_msg")
        if phase == "failed" and error_msg:
            lines.append(f"Error: `{error_msg}`")
        all_lines.extend(lines)

    if jd_st:
        if all_lines:
            all_lines.append("")   # blank separator
        phase        = jd_st.get("phase", "unknown")
        started_at   = jd_st.get("started_at")
        completed_at = jd_st.get("completed_at")
        icon         = phase_icons.get(phase, "⏳")
        lines        = [f"*{icon} Job Distribution Analysis*\n"]
        elapsed      = _elapsed(started_at, completed_at)
        if started_at:
            lines.append(f"Started: `{started_at.strftime('%H:%M:%S')}`")
        if elapsed:
            lines.append(f"Elapsed: `{elapsed}`")
        lines.append(f"Phase: `{phase}`")
        teams_count = jd_st.get("teams_count")
        if teams_count is not None:
            lines.append(f"Teams: `{teams_count}`")
        members_total = jd_st.get("members_total")
        if members_total is not None:
            lines.append(f"Members: `{members_total}`")
        tasks_total = jd_st.get("tasks_total")
        tasks_done  = jd_st.get("tasks_done", 0)
        if tasks_total is not None:
            pct = int(tasks_done / tasks_total * 100) if tasks_total else 0
            lines.append(f"Tasks processed: `{tasks_done}/{tasks_total}` ({pct}%)")
        errors = jd_st.get("errors", 0)
        if errors:
            lines.append(f"⚠️ Fetch errors: `{errors}`")
        if phase == "done":
            lines.append(f"Members in report: `{jd_st.get('rows', 0):,}`")
        error_msg = jd_st.get("error_msg")
        if phase == "failed" and error_msg:
            lines.append(f"Error: `{error_msg}`")
        all_lines.extend(lines)

    await update.message.reply_text(
        "\n".join(all_lines),
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


async def recv_be_report_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    report_type      = query.data.split(":")[1]
    sess             = _get_be_sess(ctx)
    sess.report_type = report_type
    label  = "🏛 Ardhisasa Report" if report_type == "ardhisasa" else "💳 Ardhipay Report"
    county_kbd = _ar_county_keyboard() if report_type == "ardhisasa" else _be_county_keyboard()
    await query.edit_message_text(
        f"✅ Report type: *{label}*\n\nSelect the county to export:",
        parse_mode="Markdown",
        reply_markup=county_kbd,
    )
    return BE.COUNTY


async def recv_be_county(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    county = query.data.split(":")[1]
    sess   = _get_be_sess(ctx)
    sess.county     = county
    reg_map         = _AR_COUNTY_REGISTRIES if sess.report_type == "ardhisasa" else _BE_COUNTY_REGISTRIES
    lbl_map         = _AR_COUNTY_LABELS     if sess.report_type == "ardhisasa" else _BE_COUNTY_LABELS
    sess.registries = reg_map.get(county, [county])
    label           = lbl_map.get(county, county.title())
    reg_list        = ", ".join(sess.registries)
    await query.edit_message_text(
        f"✅ County: *{label}*\nRegistries: `{reg_list}`\n\n"
        "📧 Enter the email address to receive the file, or send `skip` to get it only via Telegram:",
        parse_mode="Markdown",
    )
    return BE.EMAIL


async def recv_be_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    text = (update.message.text or "").strip()
    sess = _get_be_sess(ctx)

    if text.lower() == "skip":
        sess.email = ""
    else:
        if "@" not in text or "." not in text.split("@")[-1]:
            await update.message.reply_text(
                "❌ Invalid email. Enter a valid address or send `skip`.",
                parse_mode="Markdown",
            )
            return BE.EMAIL
        sess.email = text

    await update.message.reply_text(
        "🔁 *How often should this report run?*",
        parse_mode="Markdown",
        reply_markup=_be_schedule_keyboard(),
    )
    return BE.SCHEDULE


async def recv_be_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    secs = int(query.data.split(":")[1])
    sess = _get_be_sess(ctx)
    sess.schedule_seconds = secs
    sess.cred_type = "staff_valuer"

    if not get_valid_tokens(sess.cred_type):
        await query.edit_message_text(
            "❌ No valid cached tokens for *🏢 Staff Valuer*. Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
        )
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    cred_label   = CRED_LABELS.get(sess.cred_type, sess.cred_type)
    county_label = _BE_COUNTY_LABELS.get(sess.county, sess.county.title())
    reg_list     = ", ".join(sess.registries)
    sched_label  = next((l for l, s in _BE_SCHEDULE_OPTIONS if s == secs), "Run Once")
    if secs > 0:
        sched_label += " (repeating)"
    email_label  = sess.email or "Telegram only"
    rtype_label  = "🏛 Ardhisasa Report" if sess.report_type == "ardhisasa" else "💳 Ardhipay Report"

    await query.edit_message_text(
        f"✅ Ready to export.\n\n"
        f"• Report Type: *{rtype_label}*\n"
        f"• County: *{county_label}*\n"
        f"• Registries: `{reg_list}`\n"
        f"• Filter: *Completed*\n"
        f"• Schedule: *{sched_label}*\n"
        f"• Account: *{cred_label}*\n"
        f"• Destination: *{email_label}*\n\n"
        "Tap *Run Export* to start.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Run Export", callback_data="be:yes"),
            InlineKeyboardButton("❌ Cancel",     callback_data="be:no"),
        ]]),
    )
    return BE.CONFIRM


async def recv_be_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "be:no":
        await query.edit_message_text("❌ Export cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess   = _get_be_sess(ctx)
    tokens = get_valid_tokens(sess.cred_type)
    if not tokens:
        cred_label = CRED_LABELS.get(sess.cred_type, sess.cred_type)
        await query.edit_message_text(
            f"❌ Tokens for *{cred_label}* have expired. Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
        )
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    chat_id = query.message.chat_id
    secs    = sess.schedule_seconds

    if secs > 0:
        # Save schedule and register repeating job
        cfg = {
            "chat_id":          chat_id,
            "county":           sess.county,
            "registries":       sess.registries,
            "email":            sess.email,
            "interval_seconds": secs,
            "cred_type":        sess.cred_type,
            "report_type":      sess.report_type,
        }
        save_be_schedule(cfg)
        # Remove any existing job before adding a new one
        current_jobs = ctx.application.job_queue.get_jobs_by_name("bulk_export_job")
        for job in current_jobs:
            job.schedule_removal()
        ctx.application.job_queue.run_repeating(
            _bulk_export_job,
            interval=secs,
            first=0,   # run immediately then repeat
            name="bulk_export_job",
        )
        label = next((l for l, s in _BE_SCHEDULE_OPTIONS if s == secs), "repeating")
        await query.edit_message_text(
            f"⏳ Export started and scheduled to repeat *{label}*.\n"
            "You will be notified each time it completes.",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text("⏳ Export running in background — you will be notified when done.")
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(
            asyncio.to_thread(_bulk_export_run, tokens, chat_id, sess.email, ctx.bot, loop,
                              sess.registries, sess.county, sess.report_type)
        )

    await ctx.bot.send_message(chat_id, "Returning to menu.", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
async def _post_init(app) -> None:
    # Auto-start the token refresh daemon on every boot — a container rebuild
    # (redeploy) kills it along with the bot, and it otherwise stays down
    # until someone manually taps "Start Daemon" in the menu.
    ok, msg = _daemon_start()
    if ok:
        logger.info("Token refresh daemon auto-started: %s", msg)
    else:
        logger.info("Token refresh daemon auto-start skipped: %s", msg)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global fallback for exceptions PTB doesn't catch elsewhere (e.g. transient
    Telegram API timeouts). Without this registered, PTB just dumps the raw
    traceback and silently drops the update — the user's tap/message never
    gets any response and a ConversationHandler can be left stuck mid-flow.
    """
    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Something went wrong processing that (likely a network hiccup) — please try again.",
            )
        except Exception:
            pass   # best-effort notification only; don't let this raise too


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(20)
        .post_init(_post_init)
        .build()
    )
    app.add_error_handler(_on_error)

    # not_cancel / _CANCEL_FILTER (text filter excluding the cancel button,
    # so it reaches fallbacks) live in common.py — imported above

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

    # db_conv (DLV Batch) is registered via dlv_batch.register(app) below.
    # auth_conv (Refresh Auth) is registered via refresh_auth.register(app) below.

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("valuers",       cmd_valuers))
    app.add_handler(CommandHandler("delete_valuer", cmd_delete_valuer))
    app.add_handler(CommandHandler("daemon",        cmd_daemon))
    # fetch_conv (Fetch Tasks) is registered via fetch_tasks.register(app) below.
    # af_conv (Auto Fetch) + AF Results are registered via auto_fetch.register(app) below.
    # rs_conv (Receive Tasks) + /schedules + /task_batches are registered via
    # receive_tasks.register(app) below.

    be_conv = ConversationHandler(
        entry_points=[
            CommandHandler("bulkexport", cmd_bulk_export),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BULK_EXPORT)}$"), cmd_bulk_export),
        ],
        states={
            BE.REPORT_TYPE: [CallbackQueryHandler(recv_be_report_type, pattern=r"^be_rtype:")],
            BE.COUNTY:    [CallbackQueryHandler(recv_be_county,    pattern=r"^be_county:")],
            BE.EMAIL:     [MessageHandler(not_cancel, recv_be_email)],
            BE.SCHEDULE:  [CallbackQueryHandler(recv_be_schedule,  pattern=r"^be_sched:")],
            BE.CONFIRM:   [CallbackQueryHandler(recv_be_confirm,   pattern=r"^be:")],
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
    dlv_batch.register(app)
    refresh_auth.register(app)
    fetch_tasks.register(app)
    auto_fetch.register(app)
    receive_tasks.register(app)
    lookup_reference.register(app)
    valuer_tasks.register(app)
    job_distribution.register(app)
    app.add_handler(be_conv)

    app.add_handler(MessageHandler(
        filters.Regex(f"^{re.escape(BTN_EXPORT_STATUS)}$"), cmd_export_status
    ))

    # DLV Batch: 5-minute repeating job + DLV Queue handlers registered via
    # dlv_batch.register(app) above.

    # Bulk Export: restore saved schedule on startup
    be_cfg = load_be_schedule()
    if be_cfg and be_cfg.get("interval_seconds", 0) > 0:
        app.job_queue.run_repeating(
            _bulk_export_job,
            interval=be_cfg["interval_seconds"],
            first=be_cfg["interval_seconds"],
            name="bulk_export_job",
        )
        logger.info("Bulk export schedule restored: every %ds", be_cfg["interval_seconds"])

    # Auto Fetch's saved-schedule restore happens inside auto_fetch.register(app) above.

    dlv_tasks.register(app)
    sectional_properties.register(app)
    morning_briefing.register(app)

    # Button handlers outside an active conversation
    # (bare BTN_AUTH handler registered via refresh_auth.register(app) above)
    app.add_handler(CallbackQueryHandler(recv_delete_valuer,  pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(recv_daemon_action,  pattern=r"^daemon:"))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_TOKEN_STATUS)}$"), cmd_token_status))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ERROR_REPORT)}$"), cmd_error_report))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DAEMON)}$"),       cmd_daemon))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_RESTART)}$"),     cmd_restart))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HELP)}$"),        cmd_help))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_VALUERS)}$"),     cmd_valuers))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DELETE)}$"),      cmd_delete_valuer))
    # DLV Queue handlers registered via dlv_batch.register(app) above.
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ASSIGNMENTS)}$"), cmd_assignments))
    # AF Results handlers registered via auto_fetch.register(app) above.
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"),      cmd_cancel))

    logger.info("Bot started. Polling for updates…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
