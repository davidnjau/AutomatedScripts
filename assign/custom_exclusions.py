#!/usr/bin/env python3
"""
custom_exclusions.py
=========================
Manage Exclusions — add/remove custom parcel_number keywords on top of
Auto Fetch's 9 built-in exclusion keywords (SECTIONAL + the 8 apartment
variants — see auto_fetch.py's _AF_EXCLUSION_KEYWORDS), via the
🚫 Exclusions button / /exclusions. Auto Fetch's schedule-creation
"Exclude" multi-select step reads this list too (auto_fetch.py's
_af_all_exclusion_keywords()), so a keyword added here shows up there
immediately — that's why load_custom_exclusions/save_custom_exclusions
live in common.py rather than here.

Call register(app) from bot.py's main() to wire this feature in.
"""

import re
from enum import Enum, auto

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

from auto_fetch import _AF_EXCLUSION_KEYWORDS
from common import (
    BTN_CUSTOM_EXCLUSIONS,
    _CANCEL_FILTER,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    load_custom_exclusions,
    md_escape,
    not_cancel,
    save_custom_exclusions,
)

# Comfortably under Telegram's 64-byte callback_data limit even with the
# ce_remove:/ft_excl_pick: prefixes this keyword rides along in.
_CE_MAX_KEYWORD_LEN = 20


# ──────────────────────────────────────────────────────────
# States — Manage Exclusions conversation
# ──────────────────────────────────────────────────────────
class CE(Enum):
    ACTION = auto()   # show current custom list + Add/Remove/Close
    ADD    = auto()   # text entry for a new keyword
    REMOVE = auto()   # pick which saved keyword to remove


# ──────────────────────────────────────────────────────────
# Manage Exclusions command handlers
# ──────────────────────────────────────────────────────────

def _ce_action_keyboard(has_custom: bool) -> InlineKeyboardMarkup:
    # Build the action menu; only offer Remove when there's something to remove.
    rows = [[InlineKeyboardButton("➕ Add", callback_data="ce:add")]]
    if has_custom:
        rows.append([InlineKeyboardButton("🗑 Remove", callback_data="ce:remove")])
    rows.append([InlineKeyboardButton("🛑 Close", callback_data="ce:close")])
    return InlineKeyboardMarkup(rows)


def _ce_remove_keyboard(keywords) -> InlineKeyboardMarkup:
    # One button per saved custom keyword, plus a Cancel escape hatch.
    rows = [[InlineKeyboardButton(f"🗑 {kw}", callback_data=f"ce_remove:{kw}")] for kw in keywords]
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data="ce_remove:cancel")])
    return InlineKeyboardMarkup(rows)


async def cmd_exclusions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Show the full current exclusion list (built-in + custom) with Add/Remove/Close actions.
    if not allowed(update): return await deny(update)
    custom = load_custom_exclusions()
    builtin_listing = ", ".join(_AF_EXCLUSION_KEYWORDS)
    if custom:
        custom_listing = "\n".join(f"• {kw}" for kw in custom)
        status = (
            f"🚫 *Manage Exclusions*\n\n"
            f"Built-in (always available): {md_escape(builtin_listing)}\n\n"
            f"Your custom keywords:\n{md_escape(custom_listing)}\n\n"
            f"Choose an action:"
        )
    else:
        status = (
            f"🚫 *Manage Exclusions*\n\n"
            f"Built-in (always available): {md_escape(builtin_listing)}\n\n"
            f"No custom keywords yet. Anything you add here shows up in Auto "
            f"Fetch's Exclude checklist alongside these.\n\n"
            f"Choose an action:"
        )
    await update.message.reply_text(
        status, parse_mode="Markdown", reply_markup=_ce_action_keyboard(bool(custom)),
    )
    return CE.ACTION


async def recv_ce_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle the action-menu buttons: Add, Remove, Close.
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "close":
        await query.edit_message_reply_markup(reply_markup=None)
        return ConversationHandler.END

    if action == "remove":
        custom = load_custom_exclusions()
        if not custom:
            await query.edit_message_text("ℹ️ No custom keywords to remove.")
            await query.message.reply_text("Main menu:", reply_markup=_main_menu())
            return ConversationHandler.END
        await query.edit_message_text(
            "🗑 *Remove which keyword?*",
            parse_mode="Markdown",
            reply_markup=_ce_remove_keyboard(custom),
        )
        return CE.REMOVE

    # action == "add"
    await query.edit_message_text(
        "Enter the keyword to add (matched case-insensitively against "
        "parcel_number, same as the built-in ones):",
        reply_markup=None,
    )
    return CE.ADD


async def recv_ce_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Validate and save a new custom exclusion keyword.
    text = update.message.text.strip().upper()
    if not text:
        await update.message.reply_text("❌ Please enter a keyword.")
        return CE.ADD

    if len(text) > _CE_MAX_KEYWORD_LEN:
        await update.message.reply_text(
            f"❌ Keep it to {_CE_MAX_KEYWORD_LEN} characters or fewer. Enter a different one:"
        )
        return CE.ADD

    if text in _AF_EXCLUSION_KEYWORDS:
        await update.message.reply_text(
            f"❌ '{text}' is already one of the built-in keywords. Enter a different one:"
        )
        return CE.ADD

    custom = load_custom_exclusions()
    if text in custom:
        await update.message.reply_text(f"❌ '{text}' is already in your custom list. Enter a different one:")
        return CE.ADD

    custom.append(text)
    save_custom_exclusions(custom)
    await update.message.reply_text(f"✅ Added *{md_escape(text)}*.", parse_mode="Markdown")
    await update.message.reply_text("Main menu:", reply_markup=_main_menu())
    return ConversationHandler.END


async def recv_ce_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Remove the selected keyword from the custom exclusion list.
    query = update.callback_query
    await query.answer()
    keyword = query.data.split(":", 1)[1]

    if keyword == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    custom = load_custom_exclusions()
    if keyword in custom:
        custom.remove(keyword)
        save_custom_exclusions(custom)
        msg = f"🗑 Removed *{md_escape(keyword)}*."
    else:
        msg = "⚠️ That keyword was already removed."
    await query.edit_message_text(msg, parse_mode="Markdown")
    await query.message.reply_text("Main menu:", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Manage Exclusions conversation into the given Application."""
    ce_conv = ConversationHandler(
        entry_points=[
            CommandHandler("exclusions", cmd_exclusions),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_CUSTOM_EXCLUSIONS)}$"), cmd_exclusions),
        ],
        states={
            CE.ACTION: [CallbackQueryHandler(recv_ce_action, pattern=r"^ce:")],
            CE.ADD:    [MessageHandler(not_cancel, recv_ce_add)],
            CE.REMOVE: [CallbackQueryHandler(recv_ce_remove, pattern=r"^ce_remove:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(ce_conv)
