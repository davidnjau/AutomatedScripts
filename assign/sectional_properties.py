#!/usr/bin/env python3
"""
sectional_properties.py
=========================
Sectional Properties — configure a specialist valuer for sectional-title
tasks and toggle auto-routing (🔲 Sectional button / /sectional). Its
config is also read (not written) by Auto Fetch's background job to
decide sectional-task routing — that's why load_sectional_config/
save_sectional_config live in common.py rather than here.

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
    BTN_SECTIONAL,
    CRED_LABELS,
    _any_valid_tokens,
    _CANCEL_FILTER,
    _main_menu,
    _safe_err,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    load_sectional_config,
    logger,
    not_cancel,
    save_sectional_config,
)
from endpoints import ACCOUNTS_LIST_URL


# ──────────────────────────────────────────────────────────
# States — Sectional Properties conversation
# ──────────────────────────────────────────────────────────
class SC(Enum):
    ACTION   = auto()   # show current config + action buttons
    SET_NAME = auto()   # enter specialist name to search
    SELECT   = auto()   # pick from search results
    CRED     = auto()   # pick credential to use for auto-assignment


# ──────────────────────────────────────────────────────────
# Sectional Properties command handlers
# ──────────────────────────────────────────────────────────

async def cmd_sectional(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    cfg = load_sectional_config()
    specialist = cfg.get("specialist") if cfg else None
    auto_route = cfg.get("auto_route", False) if cfg else False
    cred_type  = cfg.get("cred_type", "") if cfg else ""

    if specialist:
        status = (
            f"🔲 *Sectional Properties*\n\n"
            f"Specialist: *{specialist['name']}*\n"
            f"Auto-routing: {'✅ On' if auto_route else '❌ Off'}\n"
            f"Credential: {CRED_LABELS.get(cred_type, cred_type) if cred_type else '—'}\n\n"
            f"Choose an action:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❌ Disable Auto-Route" if auto_route else "✅ Enable Auto-Route",
                    callback_data="sc:toggle_route",
                ),
            ],
            [InlineKeyboardButton("👤 Change Specialist", callback_data="sc:change")],
            [InlineKeyboardButton("🗑 Clear Config",       callback_data="sc:clear")],
        ])
    else:
        status = (
            "🔲 *Sectional Properties*\n\n"
            "No specialist configured. Set a specialist valuer to enable auto-routing "
            "of sectional tasks from Auto Fetch.\n\n"
            "Enter the specialist's name to search:"
        )
        keyboard = None

    await update.message.reply_text(status, parse_mode="Markdown", reply_markup=keyboard)
    return SC.ACTION if specialist else SC.SET_NAME


async def recv_sc_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cfg = load_sectional_config() or {}

    if query.data == "sc:toggle_route":
        cfg["auto_route"] = not cfg.get("auto_route", False)
        save_sectional_config(cfg)
        state = "enabled" if cfg["auto_route"] else "disabled"
        await query.edit_message_text(f"✅ Auto-routing {state}.", reply_markup=None)
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    if query.data == "sc:clear":
        save_sectional_config({})
        await query.edit_message_text("🗑 Sectional config cleared.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    # sc:change — ask for new name
    await query.edit_message_text(
        "Enter the specialist valuer's name to search:",
        reply_markup=None,
    )
    return SC.SET_NAME


async def recv_sc_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Please enter a name.")
        return SC.SET_NAME

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
        logger.error("Sectional name search failed: %s", e)
        await update.message.reply_text(f"❌ Search failed: {_safe_err(e)}")
        return ConversationHandler.END

    if not results:
        await update.message.reply_text(f"No staff found matching '{name}'. Try again:")
        return SC.SET_NAME

    ctx.user_data["sc_results"] = results[:10]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{r.get('first_name','')} {r.get('last_name','')} ({r.get('employee_number','')})".strip(),
            callback_data=f"sc_pick:{i}",
        )]
        for i, r in enumerate(results[:10])
    ])
    await update.message.reply_text("Select the specialist valuer:", reply_markup=keyboard)
    return SC.SELECT


async def recv_sc_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    results = ctx.user_data.get("sc_results", [])
    if idx >= len(results):
        await query.edit_message_text("❌ Invalid selection.")
        return ConversationHandler.END

    person = results[idx]
    uid    = str(person.get("id") or person.get("uid", ""))
    name   = f"{person.get('first_name','')} {person.get('last_name','')}".strip()
    acct   = str(person.get("account_number") or person.get("employee_number") or "")
    ctx.user_data["sc_specialist"] = {"name": name, "uid": uid, "account_number": acct}

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"sc_cred:{key}")]
        for key, label in CRED_LABELS.items()
    ])
    await query.edit_message_text(
        f"Selected: *{name}*\n\nWhich credential to use for auto-assignment?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return SC.CRED


async def recv_sc_cred(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cred_type  = query.data.split(":")[1]
    specialist = ctx.user_data.get("sc_specialist", {})
    cfg = load_sectional_config() or {}
    cfg.update({
        "specialist": specialist,
        "cred_type":  cred_type,
        "auto_route": cfg.get("auto_route", False),
    })
    save_sectional_config(cfg)
    await query.edit_message_text(
        f"✅ *Sectional specialist set*\n"
        f"Name: *{specialist.get('name')}*\n"
        f"Credential: *{CRED_LABELS.get(cred_type, cred_type)}*\n\n"
        f"Use /sectional to toggle auto-routing.",
        parse_mode="Markdown",
    )
    await query.message.reply_text("Main menu:", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Sectional Properties conversation into the given Application."""
    sc_conv = ConversationHandler(
        entry_points=[
            CommandHandler("sectional", cmd_sectional),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_SECTIONAL)}$"), cmd_sectional),
        ],
        states={
            SC.ACTION:   [CallbackQueryHandler(recv_sc_action,  pattern=r"^sc:")],
            SC.SET_NAME: [MessageHandler(not_cancel, recv_sc_name)],
            SC.SELECT:   [CallbackQueryHandler(recv_sc_select,  pattern=r"^sc_pick:")],
            SC.CRED:     [CallbackQueryHandler(recv_sc_cred,    pattern=r"^sc_cred:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(sc_conv)
