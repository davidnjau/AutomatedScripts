#!/usr/bin/env python3
"""
add_valuer.py
=============
➕ Add Valuer (`/addvaluer` or the "➕ Add Valuer" button in 👥 Valuers &
Access) — search the Ardhisasa staff directory by name and save a valuer to
the reusable list **without** having to assign a reference to them first.

Until now a brand-new valuer could only enter `saved_valuers.json` as a
side-effect of a successful assignment (New Assignment / Quick Assign /
Bulk Field Update all call `common.persist_valuer`). Quick Assign — the
primary assignment path after the menu trim — only fuzzy-matches names
against the *already-saved* list, so it can't onboard someone new. This
closes that gap: pick the person from the live directory and save them, then
Quick Assign recognises the name.

Flow (`AV` enum):
    /addvaluer  (or "➕ Add Valuer")
      → [no cached token?] tell the user to run 🔑 Refresh Auth, end
      → NAME:   type the valuer's name
      → SELECT: search list-user-accounts, pick a result → persist_valuer → end

Auth follows the check-cache pattern of every other feature: it uses any
already-valid cached token (`_any_valid_tokens`) to search — the directory
search works with any staff credential — and, like Quick Assign, points the
user at 🔑 Refresh Auth rather than inlining an OTP login when no token is
cached (the token-refresh daemon normally keeps one warm anyway).

register(app) wires the conversation into bot.py's main().
"""
import re
from enum import Enum, auto
from typing import Dict, List

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

from ardhisasa_auth import auth_headers, build_session
from common import (
    BTN_ADD_VALUER,
    _CANCEL_FILTER,
    _any_valid_tokens,
    _main_menu_for,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    md_escape,
    not_cancel,
    persist_valuer,
)
from endpoints import ACCOUNTS_LIST_URL


# ──────────────────────────────────────────────────────────
# States
# ──────────────────────────────────────────────────────────
class AV(Enum):
    NAME   = auto()   # enter the valuer's name to search
    SELECT = auto()   # pick which matching staff member to save


# ──────────────────────────────────────────────────────────
# Directory search + result parsing (mirrors new_assignment's shape)
# ──────────────────────────────────────────────────────────

def _av_search(http, tokens, name: str) -> List[Dict]:
    """Search the Ardhisasa staff directory (list-user-accounts) by name and
    return the raw `results` list. Blocking (HTTP) — call via asyncio.to_thread
    or a thread; kept sync so it's trivially unit-testable with a fake session."""
    resp = http.get(
        ACCOUNTS_LIST_URL,
        headers=auth_headers(tokens),
        params={"account_type": "STAFF", "filter_type": "ACTIVE", "page": 1, "search": name},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def _av_display(v: Dict) -> str:
    """Full display name from a directory result's staff_details."""
    sd = v.get("staff_details", {}) or {}
    return " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))


def _av_uid(v: Dict) -> str:
    """The valuer's user id — staff_details.user_id, falling back to the
    account's own id (same precedence new_assignment.recv_valuer_select uses)."""
    return v.get("staff_details", {}).get("user_id") or v.get("id") or ""


def _av_acct(v: Dict) -> str:
    """The valuer's account number (empty string if the result omits it)."""
    return v.get("account_number") or ""


def _av_keyboard(results: List[Dict]) -> InlineKeyboardMarkup:
    """One button per matching staff member (index-based callback, so long
    names never blow Telegram's 64-byte callback_data limit)."""
    rows = [
        [InlineKeyboardButton(_av_display(v) or f"Valuer {i + 1}", callback_data=f"av:{i}")]
        for i, v in enumerate(results)
    ]
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────

async def cmd_add_valuer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point — search the Ardhisasa staff directory and save a valuer for
    reuse, no assignment needed. Needs a cached session token; if none, points
    the user at 🔑 Refresh Auth (same as Quick Assign) rather than logging in."""
    if not allowed(update):
        return await deny(update)
    tokens = _any_valid_tokens()
    if not tokens:
        await update.message.reply_text(
            "⚠️ *Add Valuer* — no active session token. Run *🔑 Refresh Auth* first, then try again.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END
    ctx.user_data["av_tokens"] = tokens
    await update.message.reply_text(
        "➕ *Add Valuer* — search the Ardhisasa staff directory and save a valuer "
        "for reuse, no assignment needed.\n\nType the valuer's name:",
        parse_mode="Markdown",
    )
    return AV.NAME


async def recv_av_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Search the directory for the typed name. No results → reprompt; one or
    more → show the picker."""
    if not allowed(update):
        return await deny(update)
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Please type the valuer's name:")
        return AV.NAME

    tokens = ctx.user_data.get("av_tokens") or _any_valid_tokens()
    if not tokens:
        await update.message.reply_text(
            "⚠️ Session expired — run *🔑 Refresh Auth* and try again.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    try:
        results = _av_search(build_session(), tokens, name)
    except Exception as e:
        await update.message.reply_text(f"❌ Directory search failed: `{md_escape(str(e))}`\n\nTry another name:",
                                        parse_mode="Markdown")
        return AV.NAME

    if not results:
        await update.message.reply_text(
            f"🤔 No staff match *{md_escape(name)}*. Type another name (or 🛑 Cancel):",
            parse_mode="Markdown",
        )
        return AV.NAME

    ctx.user_data["av_results"] = results
    await update.message.reply_text(
        f"Found *{len(results)}* match(es) — pick who to save:",
        parse_mode="Markdown",
        reply_markup=_av_keyboard(results),
    )
    return AV.SELECT


async def recv_av_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Save the chosen staff member to the reusable valuer list."""
    query = update.callback_query
    await query.answer()
    results = ctx.user_data.get("av_results") or []
    idx = int(query.data.split(":")[1])
    if idx >= len(results):
        await query.edit_message_text("ℹ️ That result list is stale — run ➕ Add Valuer again.")
        return ConversationHandler.END

    v = results[idx]
    name, uid, acct = _av_display(v), _av_uid(v), _av_acct(v)
    persist_valuer(name, uid, acct)

    ctx.user_data.pop("av_results", None)
    ctx.user_data.pop("av_tokens", None)
    await query.edit_message_text(
        f"✅ Saved *{md_escape(name)}* to your valuers.\n"
        f"*User ID:* `{md_escape(str(uid))}`\n\n"
        "They're now in 👥 Saved Valuers and Quick Assign will recognise the name.",
        parse_mode="Markdown",
    )
    await ctx.bot.send_message(
        query.message.chat_id,
        "Back to the menu.",
        reply_markup=_main_menu_for(query.from_user.id),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Add Valuer conversation into the given Application."""
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("addvaluer", cmd_add_valuer),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_ADD_VALUER)}$"), cmd_add_valuer),
        ],
        states={
            AV.NAME:   [MessageHandler(not_cancel, recv_av_name)],
            AV.SELECT: [CallbackQueryHandler(recv_av_select, pattern=r"^av:")],
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
