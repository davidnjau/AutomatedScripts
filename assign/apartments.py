#!/usr/bin/env python3
"""
apartments.py
=========================
Apartments — configure a specialist valuer for apartment-title tasks and
toggle auto-routing (🏬 Apartments button / /apartments). Its config is
also read (not written) by Auto Fetch's background job to decide
apartment-task routing — that's why load_apartments_config/
save_apartments_config live in common.py rather than here. Mirrors
sectional_properties.py's shape exactly; the two features are independent
(a task can be sectional, apartment, both, or neither) and each is
detected by its own rule in Auto Fetch — an apartment task is one whose
parcel_number contains any of auto_fetch.py's _AF_APARTMENT_KEYWORDS
(APARTMENT, APPARTMENT, FLAT, BUILDING, MASSIONNAITE, LTL, LTB, APT — all
case-insensitive).

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

from ardhisasa_auth import build_session

from common import (
    BTN_APARTMENTS,
    CRED_LABELS,
    _any_valid_tokens,
    _CANCEL_FILTER,
    _main_menu,
    _safe_err,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    load_apartments_config,
    logger,
    md_escape,
    not_cancel,
    save_apartments_config,
)
from endpoints import ACCOUNTS_LIST_URL


# ──────────────────────────────────────────────────────────
# States — Apartments conversation
# ──────────────────────────────────────────────────────────
class AP(Enum):
    ACTION   = auto()   # show current config + action buttons
    SET_NAME = auto()   # enter specialist name to search
    SELECT   = auto()   # pick from search results
    CRED     = auto()   # pick credential to use for auto-assignment


# ──────────────────────────────────────────────────────────
# Apartments command handlers
# ──────────────────────────────────────────────────────────

async def cmd_apartments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Show the current apartments config (specialist/auto-route/credential)
    # with action buttons, or prompt for a specialist name if none is set yet.
    if not allowed(update): return await deny(update)
    cfg = load_apartments_config()
    specialist = cfg.get("specialist") if cfg else None
    auto_route = cfg.get("auto_route", False) if cfg else False
    cred_type  = cfg.get("cred_type", "") if cfg else ""

    if specialist:
        status = (
            f"🏬 *Apartments*\n\n"
            f"Specialist: *{md_escape(specialist['name'])}*\n"
            f"Auto-routing: {'✅ On' if auto_route else '❌ Off'}\n"
            f"Credential: {CRED_LABELS.get(cred_type, cred_type) if cred_type else '—'}\n\n"
            f"Choose an action:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❌ Disable Auto-Route" if auto_route else "✅ Enable Auto-Route",
                    callback_data="ap:toggle_route",
                ),
            ],
            [InlineKeyboardButton("👤 Change Specialist", callback_data="ap:change")],
            [InlineKeyboardButton("🗑 Clear Config",       callback_data="ap:clear")],
        ])
    else:
        status = (
            "🏬 *Apartments*\n\n"
            "No specialist configured. Set a specialist valuer to enable auto-routing "
            "of apartment tasks from Auto Fetch.\n\n"
            "Enter the specialist's name to search:"
        )
        keyboard = None

    await update.message.reply_text(status, parse_mode="Markdown", reply_markup=keyboard)
    return AP.ACTION if specialist else AP.SET_NAME


async def recv_ap_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle the action-menu buttons: toggle auto-route, clear config, or change specialist.
    query = update.callback_query
    await query.answer()
    cfg = load_apartments_config() or {}

    if query.data == "ap:toggle_route":
        cfg["auto_route"] = not cfg.get("auto_route", False)
        save_apartments_config(cfg)
        state = "enabled" if cfg["auto_route"] else "disabled"
        await query.edit_message_text(f"✅ Auto-routing {state}.", reply_markup=None)
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    if query.data == "ap:clear":
        save_apartments_config({})
        await query.edit_message_text("🗑 Apartments config cleared.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    # ap:change — ask for new name
    await query.edit_message_text(
        "Enter the specialist valuer's name to search:",
        reply_markup=None,
    )
    return AP.SET_NAME


async def recv_ap_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Search staff accounts by name using any cached valid token.
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Please enter a name.")
        return AP.SET_NAME

    tokens = _any_valid_tokens()
    if not tokens:
        await update.message.reply_text("❌ No valid tokens. Please refresh auth first.")
        return ConversationHandler.END

    http_sess = build_session()
    try:
        resp = http_sess.get(
            ACCOUNTS_LIST_URL,
            headers={"Authorization": f"Bearer {tokens.access_token}", "JWTAUTH": f"Bearer {tokens.jwt}"},
            params={"account_type": "STAFF", "search": name, "page": 1},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        logger.error("Apartments name search failed: %s", e)
        await update.message.reply_text(f"❌ Search failed: {_safe_err(e)}")
        return ConversationHandler.END

    if not results:
        await update.message.reply_text(f"No staff found matching '{name}'. Try again:")
        return AP.SET_NAME

    ctx.user_data["ap_results"] = results[:10]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{r.get('first_name','')} {r.get('last_name','')} ({r.get('employee_number','')})".strip(),
            callback_data=f"ap_pick:{i}",
        )]
        for i, r in enumerate(results[:10])
    ])
    await update.message.reply_text("Select the specialist valuer:", reply_markup=keyboard)
    return AP.SELECT


async def recv_ap_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Store the picked staff member as the pending specialist, then ask for a credential.
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    results = ctx.user_data.get("ap_results", [])
    if idx >= len(results):
        await query.edit_message_text("❌ Invalid selection.")
        return ConversationHandler.END

    person = results[idx]
    uid    = str(person.get("id") or person.get("uid", ""))
    name   = f"{person.get('first_name','')} {person.get('last_name','')}".strip()
    acct   = str(person.get("account_number") or person.get("employee_number") or "")
    ctx.user_data["ap_specialist"] = {"name": name, "uid": uid, "account_number": acct}

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"ap_cred:{key}")]
        for key, label in CRED_LABELS.items()
    ])
    await query.edit_message_text(
        f"Selected: *{md_escape(name)}*\n\nWhich credential to use for auto-assignment?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return AP.CRED


async def recv_ap_cred(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Persist the specialist + chosen credential, preserving the existing auto_route flag.
    query = update.callback_query
    await query.answer()
    cred_type  = query.data.split(":")[1]
    specialist = ctx.user_data.get("ap_specialist", {})
    cfg = load_apartments_config() or {}
    cfg.update({
        "specialist": specialist,
        "cred_type":  cred_type,
        "auto_route": cfg.get("auto_route", False),
    })
    save_apartments_config(cfg)
    await query.edit_message_text(
        f"✅ *Apartments specialist set*\n"
        f"Name: *{md_escape(specialist.get('name'))}*\n"
        f"Credential: *{CRED_LABELS.get(cred_type, cred_type)}*\n\n"
        f"Use /apartments to toggle auto-routing.",
        parse_mode="Markdown",
    )
    await query.message.reply_text("Main menu:", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Apartments conversation into the given Application."""
    ap_conv = ConversationHandler(
        entry_points=[
            CommandHandler("apartments", cmd_apartments),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_APARTMENTS)}$"), cmd_apartments),
        ],
        states={
            AP.ACTION:   [CallbackQueryHandler(recv_ap_action,  pattern=r"^ap:")],
            AP.SET_NAME: [MessageHandler(not_cancel, recv_ap_name)],
            AP.SELECT:   [CallbackQueryHandler(recv_ap_select,  pattern=r"^ap_pick:")],
            AP.CRED:     [CallbackQueryHandler(recv_ap_cred,    pattern=r"^ap_cred:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(ap_conv)
