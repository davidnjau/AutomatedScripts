#!/usr/bin/env python3
"""
access_control.py
==================
Manage Access — admin-only per-user menu category permissions (🔐 Manage
Access, /manageaccess). Every allowed user (ALLOWED_TELEGRAM_IDS) starts
with zero category access until an admin (ADMIN_TELEGRAM_IDS) explicitly
grants some via this flow — see common.py's get_user_categories/
category_allowed for the actual enforcement (checked in
recv_menu_category before any category submenu opens) and its
no-admin-configured safety fallback (nobody is restricted at all if
ADMIN_TELEGRAM_IDS is empty, since there'd be no admin able to ever lift
a restriction).

Admins themselves always have every category (common.get_user_categories's
bypass) regardless of what's saved here — this flow only ever edits
saved_category_access.json entries for non-admin users, and the target-
user picker deliberately excludes admins since granting/revoking their
categories would be a no-op.

🔐 Manage Access isn't a member of any category's static button list in
common._MENU_CATEGORIES — common.py's recv_menu_category appends it to
⚙️ Bot Settings' submenu dynamically, only when the requesting user is an
admin, so a non-admin who's been granted Bot Settings access (e.g. just
to use Refresh Auth) never sees it there. cmd_manage_access still
re-checks admin status itself regardless, since /manageaccess and the
exact button text both work independently of which menu got them there.

Users are shown by their cached Telegram display name (common.py's
load_user_names/_record_user_name — first + last name, or @username,
captured automatically the moment any allowed user's message passes
through common.allowed()) alongside their numeric ID, e.g. "Jane Doe
(5794776293)". A user who's in ALLOWED_TELEGRAM_IDS but has never
actually messaged the bot yet has no cached name — falls back to
showing just the raw ID until they do.

Call register(app) from bot.py's main() to wire this feature in.
"""

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Set

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

from common import (
    ADMIN_IDS,
    ALLOWED_IDS,
    BTN_MANAGE_ACCESS,
    _CANCEL_FILTER,
    _main_menu_for,
    _MENU_CATEGORIES,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    is_admin,
    load_category_access,
    load_user_names,
    md_escape,
    save_category_access,
)


# ──────────────────────────────────────────────────────────
# States — Manage Access conversation
# ──────────────────────────────────────────────────────────
class MA(Enum):
    PICK_USER       = auto()   # choose which non-admin user to manage
    PICK_CATEGORIES = auto()   # toggle their category grants, then Done


@dataclass
class MASession:
    target_user_id: int = 0
    selected: Set[str] = field(default_factory=set)   # in-progress category picks


def _get_ma_sess(ctx: ContextTypes.DEFAULT_TYPE) -> MASession:
    """Fetch (creating if absent) this chat's Manage Access session,
    holding the target user and their in-progress category selection
    between the user-picker step and the category-toggle step."""
    if "ma_session" not in ctx.user_data:
        ctx.user_data["ma_session"] = MASession()
    return ctx.user_data["ma_session"]


def _ma_candidate_users() -> List[int]:
    """Every allowed, non-admin user id — the pool Manage Access can pick
    a target from. Admins are excluded since they always have every
    category regardless of what's granted here, so managing them would
    be a no-op."""
    return sorted(uid for uid in ALLOWED_IDS if uid not in ADMIN_IDS)


def _ma_display_label(uid: int, names: Dict[str, str]) -> str:
    """"Jane Doe (5794776293)" if a cached display name exists for uid,
    else just "5794776293" — the fallback for a user who's in
    ALLOWED_TELEGRAM_IDS but hasn't messaged the bot yet."""
    name = names.get(str(uid))
    return f"{name} ({uid})" if name else str(uid)


def _ma_user_keyboard() -> InlineKeyboardMarkup:
    """One button per candidate user (their cached display name + ID, or
    just the ID if no name is cached yet), plus Cancel."""
    names = load_user_names()
    rows = [
        [InlineKeyboardButton(_ma_display_label(uid, names), callback_data=f"ma_user:{uid}")]
        for uid in _ma_candidate_users()
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="ma_user:cancel")])
    return InlineKeyboardMarkup(rows)


def _ma_category_keyboard(selected: Set[str]) -> InlineKeyboardMarkup:
    """One toggle button per menu category (☑ granted / ☐ not granted),
    one per row since category labels are long, plus a trailing Done
    button — same ☑/☐ toggle-checklist convention as common.py's
    _af_exclusion_multiselect_keyboard."""
    rows = [
        [InlineKeyboardButton(f"{'☑' if label in selected else '☐'} {label}", callback_data=f"ma_cat:{label}")]
        for label in _MENU_CATEGORIES
    ]
    rows.append([InlineKeyboardButton("✅ Done", callback_data="ma_cat:done")])
    return InlineKeyboardMarkup(rows)


async def cmd_manage_access(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point (/manageaccess or the 🔐 Manage Access button) — admin
    # check independent of how this was reached, since the button is only
    # shown to admins but the slash command bypasses that.
    if not allowed(update): return await deny(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Admin access required.",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    candidates = _ma_candidate_users()
    if not candidates:
        await update.message.reply_text(
            "ℹ️ No non-admin users to manage — everyone in ALLOWED_TELEGRAM_IDS "
            "is also an admin, or ALLOWED_TELEGRAM_IDS is empty.",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🔐 *Manage Access*\n\n"
        "Grant or revoke which menu categories a user can see and open.\n\n"
        "Select a user (shown by name if they've messaged the bot before, "
        "otherwise their Telegram ID):",
        parse_mode="Markdown",
        reply_markup=_ma_user_keyboard(),
    )
    return MA.PICK_USER


async def recv_ma_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle the user picker — Cancel, or a specific target user id whose
    # current grants get preloaded as the starting selection.
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    data = query.data.split(":", 1)[1]

    if data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(query.from_user.id))
        return ConversationHandler.END

    target_id = int(data)
    sess = _get_ma_sess(ctx)
    sess.target_user_id = target_id
    sess.selected = set(load_category_access().get(str(target_id), []))

    label = _ma_display_label(target_id, load_user_names())
    await query.edit_message_text(
        f"✅ Managing *{md_escape(label)}*.\n\nToggle their categories, then tap ✅ Done:",
        parse_mode="Markdown",
        reply_markup=_ma_category_keyboard(sess.selected),
    )
    return MA.PICK_CATEGORIES


async def recv_ma_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Toggle one category on/off, or persist the selection on Done.
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    data = query.data.split(":", 1)[1]
    sess = _get_ma_sess(ctx)

    if data == "done":
        access = load_category_access()
        access[str(sess.target_user_id)] = sorted(sess.selected)
        save_category_access(access)
        summary = ", ".join(sorted(sess.selected)) if sess.selected else "_none_"
        label = _ma_display_label(sess.target_user_id, load_user_names())
        await query.edit_message_text(
            f"✅ Saved — *{md_escape(label)}* can now access: {summary}",
            parse_mode="Markdown",
        )
        await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(query.from_user.id))
        return ConversationHandler.END

    if data in _MENU_CATEGORIES:
        if data in sess.selected:
            sess.selected.discard(data)
        else:
            sess.selected.add(data)

    await query.edit_message_reply_markup(reply_markup=_ma_category_keyboard(sess.selected))
    return MA.PICK_CATEGORIES


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Manage Access conversation into the given Application."""
    ma_conv = ConversationHandler(
        entry_points=[
            CommandHandler("manageaccess", cmd_manage_access),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_MANAGE_ACCESS)}$"), cmd_manage_access),
        ],
        states={
            MA.PICK_USER:       [CallbackQueryHandler(recv_ma_user,     pattern=r"^ma_user:")],
            MA.PICK_CATEGORIES: [CallbackQueryHandler(recv_ma_category, pattern=r"^ma_cat:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(ma_conv)
