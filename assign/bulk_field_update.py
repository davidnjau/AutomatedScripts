#!/usr/bin/env python3
"""
bulk_field_update.py
=====================
Bulk Field Update (🛠 Bulk Field Update button / /bulkupdate) — update one
of `valuation_officer`, `application_status`, or `node` across a batch of
Stamp Duty reference numbers in one go, via the same
STAMP_DUTY_FIX_APPLICATION_URL endpoint New Assignment and Hold Tasks write
through. Stamp Duty only — Land Rent Determination is out of scope.

Each run updates exactly one field — a `valuation_officer` update never
also sends `node`, unlike New Assignment's assign flow, which always
bundles both. The three fields map to this tool's two input shapes:

- `valuation_officer`: collect reference numbers, then pick a saved valuer
  or search for a new one (same saved-valuer-or-search shape as New
  Assignment's PICK_VALUER_SOURCE/VALUER_NAME/SELECT_VALUER, but a local,
  self-contained copy here — see new_assignment.py's own "Extraction
  history" notes on why independent copies come first, unified later only
  once proven identical across real call sites). On success, each ref is
  also recorded via persist_valuer/persist_assignment (tag "Direct",
  workflow "stamp_duty") so /assignments, Hold Tasks, and DLV Tasks' own
  reports stay consistent with what this tool wrote.
- `application_status` / `node`: pick from each field's built-in choice
  list, or type a new one via "➕ Add New" — a typed value persists to this
  module's own saved_bfu_custom_statuses.json / saved_bfu_custom_nodes.json
  (load_bfu_custom_statuses/save_bfu_custom_statuses,
  load_bfu_custom_nodes/save_bfu_custom_nodes) so it's offered again next
  time, following the same "list + add new, persisted" convention as
  custom_exclusions.py. These two fields are not valuer-related, so no
  persist_assignment call happens for them.

Auth follows New Assignment's full CHOOSE_CRED/WAIT_OTP pattern (cached
tokens first, full login+OTP fallback otherwise) since this performs a
live write, not the cached-only _be_cred_keyboard() pattern used by
read-only features.

No already-assigned/reassign-check — this is a direct bulk-update tool,
deliberately simpler than New Assignment's guarded assign flow.

Call register(app) from bot.py's main() to wire this feature in.
"""

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional

import requests
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

from ardhisasa_auth import AuthTokens, build_session
from common import (
    BTN_BULK_FIELD_UPDATE,
    CRED_LABELS,
    CRED_MAP,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _cred_keyboard,
    _main_menu_for,
    _parse_list_input,
    _safe_err,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    load_saved_valuers,
    logger,
    md_escape,
    not_cancel,
    persist_assignment,
    persist_tokens,
    persist_valuer,
)
from endpoints import (
    ACCOUNTS_LIST_URL,
    AUTH_LOGIN_URL,
    AUTH_OTP_VERIFY_URL,
    STAMP_DUTY_FIX_APPLICATION_URL,
)
from telegram_report import _send_chunked_report

# Human-facing labels for the three updatable fields, in picker display order.
_BFU_FIELD_LABELS = {
    "valuation_officer":  "👤 Valuation Officer",
    "application_status": "📶 Application Status",
    "node":                "🔗 Node",
}

# Built-in choices for the two non-valuer fields — the backend's real
# model-level values (confirmed against the land-valuation Django backend).
_BFU_STATUS_CHOICES = ["ONGOING", "ON-HOLD", "AWAITING_HOLD_APPROVAL", "RETURNED", "COMPLETED"]
_BFU_NODE_CHOICES = [
    "VALUATION_STAMP_DUTY_CREATED",
    "VALUATION_STAMP_DUTY_VALUER_REPORT",
    "VALUATION_STAMP_DUTY_HARMONIZATION",
    "VALUATION_STAMP_DUTY_COMPLETED",
]

# User-added application_status/node values, beyond the built-ins above —
# feature-local to this module, so (per this codebase's convention) they
# live here rather than in common.py, which only hosts stores shared by
# ≥2 feature modules.
SAVED_BFU_CUSTOM_STATUSES_FILE = os.path.join(DATA_DIR, "saved_bfu_custom_statuses.json")
SAVED_BFU_CUSTOM_NODES_FILE    = os.path.join(DATA_DIR, "saved_bfu_custom_nodes.json")


def load_bfu_custom_statuses() -> List[str]:
    # Read back every custom application_status value added via "➕ Add New".
    try:
        with open(SAVED_BFU_CUSTOM_STATUSES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_bfu_custom_statuses(values: List[str]) -> None:
    # Persist the full custom application_status list so new entries survive restarts.
    _atomic_json_write(SAVED_BFU_CUSTOM_STATUSES_FILE, values, indent=2)


def load_bfu_custom_nodes() -> List[str]:
    # Read back every custom node value added via "➕ Add New".
    try:
        with open(SAVED_BFU_CUSTOM_NODES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_bfu_custom_nodes(values: List[str]) -> None:
    # Persist the full custom node list so new entries survive restarts.
    _atomic_json_write(SAVED_BFU_CUSTOM_NODES_FILE, values, indent=2)


# ──────────────────────────────────────────────────────────
# States
# ──────────────────────────────────────────────────────────
class BFU(Enum):
    FIELD_CHOICE        = auto()   # pick valuation_officer / application_status / node
    REF_NUMBERS         = auto()   # type reference numbers
    PICK_VALUER_SOURCE  = auto()   # (valuation_officer only) saved valuer vs search new
    VALUER_NAME         = auto()   # (valuation_officer only) enter name when searching new
    VALUE_SELECT        = auto()   # (application_status/node only) pick a value, or add new
    VALUE_ADD           = auto()   # (application_status/node only) text entry for a new value
    CHOOSE_CRED         = auto()
    WAIT_OTP            = auto()
    SELECT_VALUER       = auto()   # (valuation_officer + search-new only) pick from live results
    CONFIRM             = auto()


# ──────────────────────────────────────────────────────────
# Per-user session data
# ──────────────────────────────────────────────────────────
@dataclass
class BFUSession:
    update_field:  str = ""   # "valuation_officer" | "application_status" | "node"
    refs:          List[str] = field(default_factory=list)
    value:         str = ""   # resolved value to write (valuer uid, or the chosen status/node string)
    valuer_name:   str = ""
    cred_type:     str = "publicuser"
    session:       Optional[requests.Session] = None
    tokens:        Optional[AuthTokens] = None
    valuers:       List[Dict] = field(default_factory=list)
    selected_idx:  Optional[int] = None
    saved_valuer:  Optional[Dict] = None   # {"name", "uid", "account_number"}


def _get_bfu_sess(ctx: ContextTypes.DEFAULT_TYPE) -> BFUSession:
    # Lazily create and cache this chat's Bulk Field Update session.
    if "bfu_session" not in ctx.user_data:
        ctx.user_data["bfu_session"] = BFUSession()
    return ctx.user_data["bfu_session"]


def _bfu_choices_for(update_field: str) -> List[str]:
    # Built-in choices plus any persisted custom additions, for the given field.
    if update_field == "application_status":
        custom = load_bfu_custom_statuses()
        return _BFU_STATUS_CHOICES + [v for v in custom if v not in _BFU_STATUS_CHOICES]
    custom = load_bfu_custom_nodes()
    return _BFU_NODE_CHOICES + [v for v in custom if v not in _BFU_NODE_CHOICES]


# ──────────────────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────────────────

def _bfu_field_keyboard() -> InlineKeyboardMarkup:
    # One button per updatable field, in a fixed order, plus Cancel.
    rows = [
        [InlineKeyboardButton(label, callback_data=f"bfu_field:{key}")]
        for key, label in _BFU_FIELD_LABELS.items()
    ]
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data="bfu_field:cancel")])
    return InlineKeyboardMarkup(rows)


# Sentinel callback value for "add a new value" — distinct from any real
# status/node value (plain UPPER_SNAKE_CASE/UPPER-HYPHEN API constants with no "__").
_BFU_VALUE_ADD_SENTINEL = "bfu_val:__add__"


def _bfu_value_keyboard(choices: List[str]) -> InlineKeyboardMarkup:
    # One button per available choice, plus "add new" and Cancel.
    rows = [[InlineKeyboardButton(c, callback_data=f"bfu_val:{c}")] for c in choices]
    rows.append([InlineKeyboardButton("➕ Add New", callback_data=_BFU_VALUE_ADD_SENTINEL)])
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data="bfu_val:cancel")])
    return InlineKeyboardMarkup(rows)


def _bfu_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm & Update", callback_data="bfu_confirm:yes"),
        InlineKeyboardButton("❌ Cancel",            callback_data="bfu_confirm:no"),
    ]])


def _bfu_confirm_text(sess: BFUSession, display_value: str) -> str:
    # Build the "about to write this" summary shown before CONFIRM.
    refs_list = "\n".join(f"  • `{r}`" for r in sess.refs)
    return (
        f"📋 *Bulk Update Summary*\n\n"
        f"*Field:* {_BFU_FIELD_LABELS[sess.update_field]}\n"
        f"*Value:* {display_value}\n\n"
        f"*References ({len(sess.refs)}):*\n{refs_list}\n\n"
        f"Proceed?"
    )


# ──────────────────────────────────────────────────────────
# Valuer search (local, self-contained — see module docstring)
# ──────────────────────────────────────────────────────────

async def _bfu_do_valuer_search(message, sess: BFUSession) -> Optional[List[Dict]]:
    # Search for valuers by name; returns list or None (error already sent).
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
        resp = sess.session.get(ACCOUNTS_LIST_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        await message.reply_text(f"❌ Valuer search failed: `{e}`", parse_mode="Markdown")
        return None


async def _bfu_show_valuer_keyboard(message, sess: BFUSession, results: List[Dict]) -> int:
    # Render the live search results as a selectable keyboard.
    sess.valuers = results
    rows = []
    for i, v in enumerate(results):
        sd   = v.get("staff_details", {})
        name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
        rows.append([InlineKeyboardButton(name or f"Valuer {i+1}", callback_data=f"bfu_valuer:{i}")])
    await message.reply_text(
        f"Found *{len(results)}* valuer(s). Select one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return BFU.SELECT_VALUER


# ──────────────────────────────────────────────────────────
# Step 0 — /bulkupdate → ask which field to update
# ──────────────────────────────────────────────────────────
async def cmd_bulk_field_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point: reset session state and show the field-choice picker.
    if not allowed(update): return await deny(update)
    ctx.user_data["bfu_session"] = BFUSession()
    await update.message.reply_text(
        "🛠 *Bulk Field Update*\n\n"
        "Update `application_status`, `valuation_officer`, or `node` across "
        "a batch of Stamp Duty reference numbers in one go.\n\n"
        "Step 1 — Which field do you want to update?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("Choose a field:", reply_markup=_bfu_field_keyboard())
    return BFU.FIELD_CHOICE


# ──────────────────────────────────────────────────────────
# Step 1 — field chosen → ask for reference numbers
# ──────────────────────────────────────────────────────────
async def recv_bfu_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle the field-choice picker: set sess.update_field or cancel.
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END

    sess = _get_bfu_sess(ctx)
    sess.update_field = choice
    await query.edit_message_text(
        f"✅ Field: *{_BFU_FIELD_LABELS[choice]}*\n\n"
        "Step 2 — Enter the *reference numbers* to update:\n"
        "_Comma-separated or one per line, e.g._\n"
        "`LS/VAL/2024/001, LS/VAL/2024/002`",
        parse_mode="Markdown",
    )
    return BFU.REF_NUMBERS


# ──────────────────────────────────────────────────────────
# Step 2 — refs received → branch on which field is being updated
# ──────────────────────────────────────────────────────────
async def recv_bfu_refs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Parse the typed reference numbers, then branch on the chosen field.
    sess = _get_bfu_sess(ctx)
    refs = _parse_list_input(update.message.text or "")
    if not refs:
        await update.message.reply_text("⚠️ No valid references found. Try again.")
        return BFU.REF_NUMBERS
    sess.refs = refs
    bullet_list = "\n".join(f"  • `{r}`" for r in refs)

    if sess.update_field == "valuation_officer":
        return await _bfu_proceed_to_valuer_pick(update.message, sess, bullet_list)

    choices = _bfu_choices_for(sess.update_field)
    await update.message.reply_text(
        f"✅ *{len(refs)} reference(s)* queued:\n{bullet_list}\n\n"
        f"Step 3 — Choose a value for *{_BFU_FIELD_LABELS[sess.update_field]}*:",
        parse_mode="Markdown",
        reply_markup=_bfu_value_keyboard(choices),
    )
    return BFU.VALUE_SELECT


async def _bfu_proceed_to_valuer_pick(message, sess: BFUSession, bullet_list: str) -> int:
    # Show the saved-valuer-or-search picker for the valuation_officer field.
    saved = load_saved_valuers()
    if saved:
        rows = [
            [InlineKeyboardButton(f"👤 {sv['name']}", callback_data=f"bfu_src:{i}")]
            for i, sv in enumerate(saved)
        ]
        rows.append([InlineKeyboardButton("🔍 Search new valuer", callback_data="bfu_src:new")])
        await message.reply_text(
            f"✅ *{len(sess.refs)} reference(s)* queued:\n{bullet_list}\n\n"
            "Step 3 — Select a saved valuer or search for a new one:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return BFU.PICK_VALUER_SOURCE
    await message.reply_text(
        f"✅ *{len(sess.refs)} reference(s)* queued:\n{bullet_list}\n\n"
        "Step 3 — Enter the *valuer name* to search:\n"
        "_Partial names work, e.g._ `JOHN KAMAU`",
        parse_mode="Markdown",
    )
    return BFU.VALUER_NAME


# ──────────────────────────────────────────────────────────
# Step 2a (valuation_officer) — pick saved valuer or "search new"
# ──────────────────────────────────────────────────────────
async def recv_bfu_valuer_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle the saved-valuer-or-search picker for the valuation_officer field.
    query = update.callback_query
    await query.answer()
    sess = _get_bfu_sess(ctx)
    data = query.data.split(":")[1]

    if data == "new":
        await query.edit_message_text(
            "Step 3 — Enter the *valuer name* to search:\n"
            "_Partial names work, e.g._ `JOHN KAMAU`",
            parse_mode="Markdown",
        )
        return BFU.VALUER_NAME

    saved = load_saved_valuers()
    idx = int(data)
    if idx >= len(saved):
        await query.edit_message_text(
            "⚠️ That saved valuer no longer exists. Please start over.",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END
    sv = saved[idx]
    sess.saved_valuer = sv
    await query.edit_message_text(
        f"✅ Valuer: *{md_escape(sv['name'])}*\n\n"
        "Step 4 — Choose *credential profile*:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return BFU.CHOOSE_CRED


# ──────────────────────────────────────────────────────────
# Step 2b (valuation_officer) — receive valuer name (search new path)
# ──────────────────────────────────────────────────────────
async def recv_bfu_valuer_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Receive the typed valuer name (search-new path) and move to credential choice.
    sess = _get_bfu_sess(ctx)
    sess.valuer_name = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Searching for: *{md_escape(sess.valuer_name)}*\n\n"
        "Step 4 — Choose *credential profile*:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return BFU.CHOOSE_CRED


# ──────────────────────────────────────────────────────────
# Step 2c (application_status/node) — value chosen, "add new", or cancel
# ──────────────────────────────────────────────────────────
async def recv_bfu_value_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle the value picker for application_status/node: pick, add new, or cancel.
    query = update.callback_query
    await query.answer()

    if query.data == "bfu_val:cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END

    sess = _get_bfu_sess(ctx)

    if query.data == _BFU_VALUE_ADD_SENTINEL:
        await query.edit_message_text(
            f"✏️ Enter the new *{_BFU_FIELD_LABELS[sess.update_field]}* value:",
            parse_mode="Markdown",
        )
        return BFU.VALUE_ADD

    value = query.data.split(":", 1)[1]
    sess.value = value
    await query.edit_message_text(
        f"✅ Value: `{value}`\n\n"
        "Step 4 — Choose *credential profile*:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return BFU.CHOOSE_CRED


# ──────────────────────────────────────────────────────────
# Step 2d (application_status/node) — receive a new custom value
# ──────────────────────────────────────────────────────────
async def recv_bfu_value_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Validate, persist, and select a newly-typed custom value.
    sess = _get_bfu_sess(ctx)
    text = update.message.text.strip().upper().replace(" ", "_")
    if not text:
        await update.message.reply_text("❌ Please enter a value.")
        return BFU.VALUE_ADD

    if sess.update_field == "application_status":
        builtins, load_fn, save_fn = _BFU_STATUS_CHOICES, load_bfu_custom_statuses, save_bfu_custom_statuses
    else:
        builtins, load_fn, save_fn = _BFU_NODE_CHOICES, load_bfu_custom_nodes, save_bfu_custom_nodes

    custom = load_fn()
    if text not in builtins and text not in custom:
        custom.append(text)
        save_fn(custom)

    sess.value = text
    await update.message.reply_text(
        f"✅ Added and selected value: `{text}`\n\n"
        "Step 4 — Choose *credential profile*:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return BFU.CHOOSE_CRED


# ──────────────────────────────────────────────────────────
# Step 3 — credential chosen → use cache or full login
# ──────────────────────────────────────────────────────────
async def recv_bfu_cred_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Credential chosen → use cached tokens or kick off a full login+OTP.
    query = update.callback_query
    await query.answer()

    sess = _get_bfu_sess(ctx)
    cred_type = query.data.split(":")[1]
    sess.cred_type = cred_type
    creds = CRED_MAP[cred_type]

    cached = get_valid_tokens(cred_type)
    if cached:
        sess.tokens  = cached
        sess.session = build_session()
        logger.info("Using cached tokens for %s", cred_type)
        await query.edit_message_text(f"🔑 Cached login: *{CRED_LABELS[cred_type]}*", parse_mode="Markdown")
        return await _bfu_after_auth(query.message, sess)

    await query.edit_message_text(
        f"✅ Credential: *{CRED_LABELS[cred_type]}* (`{creds['username']}`)\n\n"
        "🔐 Sending login request…",
        parse_mode="Markdown",
    )
    sess.session = build_session()
    try:
        resp = sess.session.post(
            AUTH_LOGIN_URL,
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
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    await query.message.reply_text(
        "📲 OTP has been sent to the registered device.\n\nPlease *reply with the OTP code* now:",
        parse_mode="Markdown",
    )
    return BFU.WAIT_OTP


# ──────────────────────────────────────────────────────────
# Step 4 — receive OTP → verify → cache tokens → continue
# ──────────────────────────────────────────────────────────
async def recv_bfu_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Verify the typed OTP, cache the resulting tokens, then continue the flow.
    sess  = _get_bfu_sess(ctx)
    otp   = update.message.text.strip()
    creds = CRED_MAP[sess.cred_type]

    await update.message.reply_text("🔄 Verifying OTP…")
    try:
        resp = sess.session.post(
            AUTH_OTP_VERIFY_URL,
            json={"username": creds["username"], "password": creds["password"], "otpcode": otp},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        details       = data.get("details", {})
        access_token  = details.get("access_token")
        jwt           = details.get("jwt")
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
        return BFU.WAIT_OTP

    await update.message.reply_text("✅ Authenticated!")
    return await _bfu_after_auth(update.message, sess)


async def _bfu_after_auth(message, sess: BFUSession) -> int:
    """After tokens are available (cached or freshly verified): for
    application_status/node, or valuation_officer with a saved valuer
    already picked, jump straight to the confirm screen. For
    valuation_officer + search-new, run the live valuer search first."""
    if sess.update_field != "valuation_officer":
        await message.reply_text(
            _bfu_confirm_text(sess, f"`{sess.value}`"),
            parse_mode="Markdown", reply_markup=_bfu_confirm_keyboard(),
        )
        return BFU.CONFIRM

    if sess.saved_valuer:
        sv = sess.saved_valuer
        display = f"{md_escape(sv['name'])} (`{sv['uid']}`)"
        await message.reply_text(
            _bfu_confirm_text(sess, display),
            parse_mode="Markdown", reply_markup=_bfu_confirm_keyboard(),
        )
        return BFU.CONFIRM

    await message.reply_text(f"🔍 Searching for valuer *{md_escape(sess.valuer_name)}*…", parse_mode="Markdown")
    results = await _bfu_do_valuer_search(message, sess)
    if results is None:
        await message.reply_text("Use the menu to start again.", reply_markup=_main_menu_for(message.chat_id))
        return ConversationHandler.END
    if not results:
        await message.reply_text(
            f"⚠️ No valuers found matching *{md_escape(sess.valuer_name)}*.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(message.chat_id),
        )
        return ConversationHandler.END
    return await _bfu_show_valuer_keyboard(message, sess, results)


# ──────────────────────────────────────────────────────────
# Step 5 (valuation_officer + search-new only) — valuer selected → confirm
# ──────────────────────────────────────────────────────────
async def recv_bfu_valuer_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Live search result selected → build the confirm summary.
    query = update.callback_query
    await query.answer()

    sess = _get_bfu_sess(ctx)
    idx  = int(query.data.split(":")[1])
    sess.selected_idx = idx
    v    = sess.valuers[idx]
    sd   = v.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
    uid  = sd.get("user_id", v.get("id", "?"))

    display = f"{md_escape(name)} (`{uid}`)"
    await query.edit_message_text(
        _bfu_confirm_text(sess, display),
        parse_mode="Markdown",
        reply_markup=_bfu_confirm_keyboard(),
    )
    return BFU.CONFIRM


# ──────────────────────────────────────────────────────────
# Step 6 — confirmed → run the update → report results
# ──────────────────────────────────────────────────────────
async def recv_bfu_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Confirmed → POST the one-field update for every ref, then report results.
    query = update.callback_query
    await query.answer()

    if query.data == "bfu_confirm:no":
        await query.edit_message_text("❌ Update cancelled.")
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END

    sess = _get_bfu_sess(ctx)

    name = uid = acct = None
    if sess.update_field == "valuation_officer":
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
        value = uid
    else:
        value = sess.value

    await query.edit_message_text(
        f"⚙️ Updating *{_BFU_FIELD_LABELS[sess.update_field]}* on {len(sess.refs)} reference(s)…",
        parse_mode="Markdown",
    )

    headers = {
        "Authorization": f"Bearer {sess.tokens.access_token}",
        "JWTAUTH":       f"Bearer {sess.tokens.jwt}",
    }

    ok_refs, fail_refs = [], []
    result_lines = []
    for ref in sess.refs:
        try:
            body = {"reference_number": ref, sess.update_field: value}
            r = sess.session.post(STAMP_DUTY_FIX_APPLICATION_URL, headers=headers, json=body, timeout=30)
            r.raise_for_status()
            ok_refs.append(ref)
            result_lines.append(f"✅ `{ref}`")
        except Exception as e:
            logger.error("Bulk field update failed for %s (%s): %s", ref, sess.update_field, e)
            fail_refs.append(ref)
            result_lines.append(f"❌ `{ref}` — {_safe_err(e)}")

    if ok_refs and sess.update_field == "valuation_officer":
        persist_valuer(name, uid, acct)   # auto-save valuer for future assignments
        for ref in ok_refs:
            # Same tag/workflow shape as New Assignment's recv_confirm, so
            # /assignments and every other report reading saved_dlv_records.json
            # stay consistent with what this tool wrote.
            persist_assignment(ref, name, uid, extra={"valuer_acct": acct, "tag": "Direct", "workflow": "stamp_duty"})

    header = (
        f"🏁 *Bulk Update Complete*\n\n"
        f"*Field:* {_BFU_FIELD_LABELS[sess.update_field]}\n"
        f"*Success:* {len(ok_refs)} / {len(sess.refs)}\n"
        f"*Failed:*  {len(fail_refs)} / {len(sess.refs)}\n"
    )

    async def _send_summary(text, reply_markup):
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send_summary, [header] + result_lines, join="\n")

    if fail_refs:
        await query.message.reply_text(
            "⚠️ Some updates failed. Tap *🛠 Bulk Field Update* to retry failed refs.",
            parse_mode="Markdown",
        )

    await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu_for(update.effective_user.id))
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Bulk Field Update conversation into the given Application."""
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("bulkupdate", cmd_bulk_field_update),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BULK_FIELD_UPDATE)}$"), cmd_bulk_field_update),
        ],
        states={
            BFU.FIELD_CHOICE:       [CallbackQueryHandler(recv_bfu_field, pattern=r"^bfu_field:")],
            BFU.REF_NUMBERS:        [MessageHandler(not_cancel, recv_bfu_refs)],
            BFU.PICK_VALUER_SOURCE: [CallbackQueryHandler(recv_bfu_valuer_source, pattern=r"^bfu_src:")],
            BFU.VALUER_NAME:        [MessageHandler(not_cancel, recv_bfu_valuer_name)],
            BFU.VALUE_SELECT:       [CallbackQueryHandler(recv_bfu_value_select, pattern=r"^bfu_val:")],
            BFU.VALUE_ADD:          [MessageHandler(not_cancel, recv_bfu_value_add)],
            BFU.CHOOSE_CRED:        [CallbackQueryHandler(recv_bfu_cred_choice, pattern=r"^cred:")],
            BFU.WAIT_OTP:           [MessageHandler(not_cancel, recv_bfu_otp)],
            BFU.SELECT_VALUER:      [CallbackQueryHandler(recv_bfu_valuer_select, pattern=r"^bfu_valuer:")],
            BFU.CONFIRM:            [CallbackQueryHandler(recv_bfu_confirm, pattern=r"^bfu_confirm:")],
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
