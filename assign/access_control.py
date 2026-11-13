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
👥 Valuers & Access' submenu dynamically, only when the requesting user is
an admin, so a non-admin who's been granted Valuers & Access never sees it
there. cmd_manage_access still re-checks admin status itself regardless,
since /manageaccess and the exact button text both work independently of
which menu got them there.

Beyond toggling whole categories, this flow also lets the admin restrict
which individual buttons a user sees *within* a granted category (the
MA.PICK_CUST_CAT → MA.PICK_BUTTONS steps), persisted to
common.saved_button_access.json and enforced display-only by
common.get_user_category_buttons (see recv_menu_category).

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
    _ADMIN_ONLY_CATEGORIES,
    _CANCEL_FILTER,
    _main_menu_for,
    _MENU_CATEGORIES,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    is_admin,
    load_button_access,
    load_category_access,
    load_user_names,
    md_escape,
    save_button_access,
    save_category_access,
)


# ──────────────────────────────────────────────────────────
# States — Manage Access conversation
# ──────────────────────────────────────────────────────────
class MA(Enum):
    PICK_USER        = auto()   # choose which non-admin user to manage
    PICK_CATEGORIES  = auto()   # toggle their category grants, then Done
    PICK_CUST_CAT    = auto()   # optionally pick a category to restrict its buttons
    PICK_BUTTONS     = auto()   # toggle which buttons within that category are visible


@dataclass
class MASession:
    target_user_id: int = 0
    selected: Set[str] = field(default_factory=set)   # in-progress category picks
    cust_category: str = ""                            # category whose buttons are being edited
    cust_selected: Set[str] = field(default_factory=set)  # in-progress button picks


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
    """One toggle button per grantable menu category (☑ granted / ☐ not
    granted), one per row since category labels are long, plus a trailing
    Done button — same ☑/☐ toggle-checklist convention as common.py's
    _af_exclusion_multiselect_keyboard. Admin-only categories
    (_ADMIN_ONLY_CATEGORIES, e.g. 🛠 System) are never offered — admins
    already have them and non-admins can never be granted them."""
    rows = [
        [InlineKeyboardButton(f"{'☑' if label in selected else '☐'} {label}", callback_data=f"ma_cat:{label}")]
        for label in _MENU_CATEGORIES if label not in _ADMIN_ONLY_CATEGORIES
    ]
    rows.append([InlineKeyboardButton("✅ Done", callback_data="ma_cat:done")])
    return InlineKeyboardMarkup(rows)


def _ma_customize_keyboard(selected_categories: Set[str], user_button_access: Dict[str, List[str]]) -> InlineKeyboardMarkup:
    """One button per granted category — tapping it drills into a per-button
    allow-list for that category. A 🔧 marker flags categories that already
    have a button restriction (others show every button by default). Plus a
    trailing ✅ Finish. Shown in _MENU_CATEGORIES order, admin-only categories
    excluded (they can't be granted anyway)."""
    rows = []
    for label in _MENU_CATEGORIES:
        if label in _ADMIN_ONLY_CATEGORIES or label not in selected_categories:
            continue
        marker = "🔧 " if label in user_button_access else "• "
        rows.append([InlineKeyboardButton(f"{marker}{label}", callback_data=f"ma_bcat:{label}")])
    rows.append([InlineKeyboardButton("✅ Finish", callback_data="ma_bcat:done")])
    return InlineKeyboardMarkup(rows)


def _ma_button_keyboard(category: str, selected_buttons: Set[str]) -> InlineKeyboardMarkup:
    """☑/☐ toggle per workflow button in `category` (by index, so long/emoji
    labels never blow the 64-byte callback_data limit), plus a trailing Done.
    A checked button is one the target user will see in that category."""
    buttons = list(_MENU_CATEGORIES.get(category, {}).get("buttons", []))
    rows = [
        [InlineKeyboardButton(
            f"{'☑' if b in selected_buttons else '☐'} {b}",
            callback_data=f"ma_btn:{i}",
        )]
        for i, b in enumerate(buttons)
    ]
    rows.append([InlineKeyboardButton("✅ Done", callback_data="ma_btn:done")])
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
        "Grant or revoke which menu categories a user can see and open — and, "
        "within a category, optionally which individual buttons they see.\n\n"
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
        uid = str(sess.target_user_id)
        access = load_category_access()
        access[uid] = sorted(sess.selected)
        save_category_access(access)

        # Drop any per-button restrictions for categories the user no longer
        # has — a button allow-list only means anything inside a granted
        # category.
        button_access = load_button_access()
        user_ba = button_access.get(uid, {})
        pruned = {c: b for c, b in user_ba.items() if c in sess.selected}
        if pruned != user_ba:
            if pruned:
                button_access[uid] = pruned
            else:
                button_access.pop(uid, None)
            save_button_access(button_access)

        if not sess.selected:
            # No categories → nothing to fine-tune; end here.
            label = _ma_display_label(sess.target_user_id, load_user_names())
            await query.edit_message_text(
                f"✅ Saved — *{md_escape(label)}* can now access: _none_",
                parse_mode="Markdown",
            )
            await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(query.from_user.id))
            return ConversationHandler.END

        label = _ma_display_label(sess.target_user_id, load_user_names())
        await query.edit_message_text(
            f"✅ Categories saved for *{md_escape(label)}*.\n\n"
            "Optionally restrict which buttons they see *within* a category "
            "(default is all). Tap a category to fine-tune, or ✅ Finish:",
            parse_mode="Markdown",
            reply_markup=_ma_customize_keyboard(sess.selected, pruned),
        )
        return MA.PICK_CUST_CAT

    if data in _MENU_CATEGORIES and data not in _ADMIN_ONLY_CATEGORIES:
        if data in sess.selected:
            sess.selected.discard(data)
        else:
            sess.selected.add(data)

    await query.edit_message_reply_markup(reply_markup=_ma_category_keyboard(sess.selected))
    return MA.PICK_CATEGORIES


async def recv_ma_customize_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # The per-category button-customization menu — pick a category to drill
    # into its button allow-list, or Finish to end with a summary.
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    data = query.data.split(":", 1)[1]
    sess = _get_ma_sess(ctx)

    if data == "done":
        label = _ma_display_label(sess.target_user_id, load_user_names())
        await query.edit_message_text(
            f"✅ Saved access for *{md_escape(label)}*.\n\n{_ma_access_summary(sess.target_user_id)}",
            parse_mode="Markdown",
        )
        await query.message.reply_text("Main menu.", reply_markup=_main_menu_for(query.from_user.id))
        return ConversationHandler.END

    if data not in sess.selected:
        # Stale/unknown category tap — just re-render the menu.
        button_access = load_button_access().get(str(sess.target_user_id), {})
        await query.edit_message_reply_markup(reply_markup=_ma_customize_keyboard(sess.selected, button_access))
        return MA.PICK_CUST_CAT

    sess.cust_category = data
    all_buttons = list(_MENU_CATEGORIES.get(data, {}).get("buttons", []))
    allowed_now = load_button_access().get(str(sess.target_user_id), {}).get(data)
    # Default (no restriction) → every button pre-checked.
    sess.cust_selected = set(allowed_now) if allowed_now is not None else set(all_buttons)
    sess.cust_selected &= set(all_buttons)

    await query.edit_message_text(
        f"🔧 *{md_escape(data)}* — toggle which buttons this user sees, then ✅ Done "
        "(all checked = no restriction):",
        parse_mode="Markdown",
        reply_markup=_ma_button_keyboard(data, sess.cust_selected),
    )
    return MA.PICK_BUTTONS


async def recv_ma_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Toggle one button on/off within the chosen category, or persist the
    # allow-list on Done and return to the category-customization menu.
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    data = query.data.split(":", 1)[1]
    sess = _get_ma_sess(ctx)
    all_buttons = list(_MENU_CATEGORIES.get(sess.cust_category, {}).get("buttons", []))

    if data == "done":
        uid = str(sess.target_user_id)
        button_access = load_button_access()
        user_ba = dict(button_access.get(uid, {}))
        chosen = sess.cust_selected & set(all_buttons)
        if chosen == set(all_buttons):
            # All buttons visible == no restriction → drop any existing entry.
            user_ba.pop(sess.cust_category, None)
        else:
            user_ba[sess.cust_category] = sorted(chosen)
        if user_ba:
            button_access[uid] = user_ba
        else:
            button_access.pop(uid, None)
        save_button_access(button_access)

        await query.edit_message_text(
            f"✅ Buttons saved for *{md_escape(sess.cust_category)}*.",
            parse_mode="Markdown",
        )
        await query.message.reply_text(
            "Fine-tune another category, or ✅ Finish:",
            reply_markup=_ma_customize_keyboard(sess.selected, button_access.get(uid, {})),
        )
        return MA.PICK_CUST_CAT

    # Toggle by index
    try:
        idx = int(data)
    except ValueError:
        return MA.PICK_BUTTONS
    if 0 <= idx < len(all_buttons):
        btn = all_buttons[idx]
        if btn in sess.cust_selected:
            sess.cust_selected.discard(btn)
        else:
            sess.cust_selected.add(btn)

    await query.edit_message_reply_markup(reply_markup=_ma_button_keyboard(sess.cust_category, sess.cust_selected))
    return MA.PICK_BUTTONS


def _ma_access_summary(target_user_id: int) -> str:
    """Human-readable summary of a user's current category + per-button
    access, for the Finish confirmation. Lists each granted category and, if
    its buttons are restricted, which ones remain visible."""
    cats = sorted(load_category_access().get(str(target_user_id), []))
    if not cats:
        return "Access: _none_"
    button_access = load_button_access().get(str(target_user_id), {})
    lines = ["Access:"]
    for c in cats:
        if c in button_access:
            visible = ", ".join(button_access[c]) if button_access[c] else "_none_"
            lines.append(f"• {c} — only: {visible}")
        else:
            lines.append(f"• {c} — all buttons")
    return "\n".join(lines)


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
            MA.PICK_USER:       [CallbackQueryHandler(recv_ma_user,          pattern=r"^ma_user:")],
            MA.PICK_CATEGORIES: [CallbackQueryHandler(recv_ma_category,      pattern=r"^ma_cat:")],
            MA.PICK_CUST_CAT:   [CallbackQueryHandler(recv_ma_customize_pick, pattern=r"^ma_bcat:")],
            MA.PICK_BUTTONS:    [CallbackQueryHandler(recv_ma_button,        pattern=r"^ma_btn:")],
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
