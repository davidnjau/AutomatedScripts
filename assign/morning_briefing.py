#!/usr/bin/env python3
"""
morning_briefing.py
====================
Morning Briefing — daily 7 AM EAT broadcast of Open DLV Tasks (🌅 Morning
Briefing button / /briefing), delivered via Telegram or email. Depends on
dlv_tasks.py for the actual fetch/report-building (_dt_fetch_tasks,
_dt_build_excel, _dt_send_telegram) but owns its own conversation, config,
and job scheduling.

Call register(app) from bot.py's main() to wire this feature in (this also
restores the daily job on startup if a prior run left it enabled).
"""

import json
import os
from datetime import datetime, time as _dtime, timezone
from enum import Enum, auto
from typing import Dict, Optional

import asyncio
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
import re

from common import (
    ALLOWED_IDS,
    BTN_BRIEFING,
    DATA_DIR,
    _any_valid_tokens,
    _atomic_json_write,
    _CANCEL_FILTER,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    logger,
    not_cancel,
)
from dlv_tasks import _dt_build_excel, _dt_fetch_tasks, _dt_send_telegram
from email_service import _send_bulk_export_email

SAVED_BRIEFING_CONFIG_FILE = os.path.join(DATA_DIR, "saved_briefing_config.json")


# ──────────────────────────────────────────────────────────
# States — Morning Briefing conversation
# ──────────────────────────────────────────────────────────
class MB(Enum):
    MENU        = auto()   # status + Run Now / Enable / Disable buttons
    DELIVERY    = auto()   # telegram or email
    EMAIL_INPUT = auto()   # enter email address


# ── Morning Briefing config ────────────────────────────────

def load_briefing_config() -> Optional[Dict]:
    try:
        with open(SAVED_BRIEFING_CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_briefing_config(cfg: Dict) -> None:
    _atomic_json_write(SAVED_BRIEFING_CONFIG_FILE, cfg, indent=2)


# ──────────────────────────────────────────────────────────
# Morning Briefing background job
# ──────────────────────────────────────────────────────────

async def _send_briefing(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    for chat_id in ALLOWED_IDS:
        try:
            await context.bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Morning briefing notify error for %s: %s", chat_id, e)


async def _run_morning_briefing(context: ContextTypes.DEFAULT_TYPE, delivery: str, email: str) -> None:
    """Build and broadcast the Open DLV Tasks briefing — shared by the 7 AM job and Run Now."""
    tokens = _any_valid_tokens()
    if not tokens:
        await _send_briefing(context, "⚠️ *Morning Briefing*\nNo valid cached tokens — authenticate first.")
        return

    try:
        rows = await asyncio.to_thread(_dt_fetch_tasks, tokens)
    except Exception as e:
        logger.error("Morning briefing: fetch failed: %s", e, exc_info=True)
        await _send_briefing(context, f"⚠️ *Morning Briefing*\nFailed to fetch tasks: `{e}`")
        return

    today = datetime.now().strftime("%d %b %Y")

    if delivery == "email":
        if not email:
            await _send_briefing(context, "⚠️ *Morning Briefing*\nEmail delivery selected but no address is saved.")
            return
        xlsx_bytes = _dt_build_excel(rows)
        filename   = f"Morning_Briefing_OpenTasks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        try:
            _send_bulk_export_email(email, filename, xlsx_bytes)
        except Exception as e:
            logger.error("Morning briefing: email send failed: %s", e)
            await _send_briefing(context, f"⚠️ *Morning Briefing — {today}*\nEmail delivery failed: `{e}`")
            return
        await _send_briefing(
            context,
            f"🌅 *Morning Briefing — {today}*\n📧 {len(rows)} Open Task(s) emailed to *{email}*.",
        )
        return

    if not rows:
        await _send_briefing(context, f"🌅 *Morning Briefing — {today}*\n\nNo Open DLV Tasks.")
        return

    await _send_briefing(context, f"🌅 *Morning Briefing — {today}* — {len(rows)} Open DLV Task(s)")
    for chat_id in ALLOWED_IDS:
        try:
            await _dt_send_telegram(chat_id, rows, context.bot)
        except Exception as e:
            logger.warning("Morning briefing notify error for %s: %s", chat_id, e)


async def _morning_briefing_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 7 AM EAT briefing: DLV Open Tasks, delivered via Telegram or email."""
    cfg = load_briefing_config()
    if not cfg or not cfg.get("enabled"):
        return
    await _run_morning_briefing(context, cfg.get("delivery", "telegram"), cfg.get("email", ""))


# ──────────────────────────────────────────────────────────
# Morning Briefing command handler
# ──────────────────────────────────────────────────────────

def _schedule_morning_briefing(job_queue) -> None:
    for job in job_queue.get_jobs_by_name("morning_briefing_job"):
        job.schedule_removal()
    job_queue.run_daily(
        _morning_briefing_job,
        time=_dtime(4, 0, tzinfo=timezone.utc),
        name="morning_briefing_job",
    )


def _mb_delivery_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Telegram", callback_data="mb_delivery:telegram"),
        InlineKeyboardButton("📩 Email",    callback_data="mb_delivery:email"),
    ]])


def _mb_menu_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("▶️ Run Now", callback_data="mb:run_now")]]
    if enabled:
        rows.append([InlineKeyboardButton("🛑 Disable", callback_data="mb:disable")])
    else:
        rows.append([InlineKeyboardButton("✅ Enable",  callback_data="mb:enable")])
    return InlineKeyboardMarkup(rows)


async def cmd_briefing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    cfg      = load_briefing_config() or {}
    enabled  = cfg.get("enabled", False)
    delivery = cfg.get("delivery", "telegram")

    if enabled:
        via = f"📩 Email ({cfg.get('email', '—')})" if delivery == "email" else "💬 Telegram"
        status = f"Status: ✅ Enabled — daily at 7 AM EAT via {via}"
    else:
        status = "Status: 🛑 Disabled"

    await update.message.reply_text(
        f"🌅 *Morning Briefing* — Open DLV Tasks\n{status}",
        parse_mode="Markdown",
        reply_markup=_mb_menu_keyboard(enabled),
    )
    return MB.MENU


async def recv_mb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query  = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "run_now":
        cfg = load_briefing_config() or {}
        await query.edit_message_text("⏳ Running Morning Briefing now…")
        await _run_morning_briefing(ctx, cfg.get("delivery", "telegram"), cfg.get("email", ""))
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    if action == "disable":
        save_briefing_config({"enabled": False})
        for job in ctx.job_queue.get_jobs_by_name("morning_briefing_job"):
            job.schedule_removal()
        await query.edit_message_text("🛑 Morning Briefing disabled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    # action == "enable"
    await query.edit_message_text(
        "🌅 *Morning Briefing* — Open DLV Tasks, delivered daily at 7 AM EAT.\n\n"
        "How would you like to receive it?",
        parse_mode="Markdown",
        reply_markup=_mb_delivery_keyboard(),
    )
    return MB.DELIVERY


async def recv_mb_delivery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":")[1]

    if mode == "email":
        await query.edit_message_text(
            "📧 Enter the email address to receive the daily Open Tasks report:",
        )
        return MB.EMAIL_INPUT

    save_briefing_config({"enabled": True, "delivery": "telegram"})
    _schedule_morning_briefing(ctx.job_queue)
    await query.edit_message_text(
        "🌅 *Morning Briefing enabled* — Open Tasks via Telegram, daily at 7 AM EAT.\n"
        "Use /briefing again to disable.",
        parse_mode="Markdown",
    )
    await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
    return ConversationHandler.END


async def recv_mb_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    email = (update.message.text or "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        await update.message.reply_text("❌ Invalid email. Enter a valid address:")
        return MB.EMAIL_INPUT

    save_briefing_config({"enabled": True, "delivery": "email", "email": email})
    _schedule_morning_briefing(ctx.job_queue)
    await update.message.reply_text(
        f"🌅 *Morning Briefing enabled* — Open Tasks emailed to *{email}*, daily at 7 AM EAT.\n"
        "Use /briefing again to disable.",
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Morning Briefing conversation into the given Application,
    and restore the daily job on startup if a prior run left it enabled."""
    briefing_cfg = load_briefing_config()
    if briefing_cfg and briefing_cfg.get("enabled"):
        _schedule_morning_briefing(app.job_queue)
        logger.info("Morning briefing restored: daily at 04:00 UTC (7 AM EAT), delivery=%s",
                    briefing_cfg.get("delivery", "telegram"))

    mb_conv = ConversationHandler(
        entry_points=[
            CommandHandler("briefing", cmd_briefing),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BRIEFING)}$"), cmd_briefing),
        ],
        states={
            MB.MENU:        [CallbackQueryHandler(recv_mb_menu,     pattern=r"^mb:")],
            MB.DELIVERY:    [CallbackQueryHandler(recv_mb_delivery, pattern=r"^mb_delivery:")],
            MB.EMAIL_INPUT: [MessageHandler(not_cancel, recv_mb_email)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(mb_conv)
