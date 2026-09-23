#!/usr/bin/env python3
"""
clear_chat.py
=============
Clear Chat — 🧹 Clear Chat button (⚙️ Bot Settings) plus a daily
background job, both deleting every message this bot has a record of
sending in a chat.

Telegram only lets a bot delete messages it sent itself, and only within
48 hours — it cannot clear a user's own messages or their local chat
view. common.py's install_message_tracking() wraps every outgoing send
at startup so this module never needs its own per-call-site wiring; it
only reads/clears common.py's saved_chat_messages.json store.

Deletion is immediate on tap — no confirmation step (user-requested: a
routine daily-cleanup action, not treated as a risky one-off deletion).

Call register(app) from bot.py's main() to wire this feature in.
"""

import re

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from common import (
    BTN_CLEAR_CHAT,
    _main_menu_for,
    allowed,
    deny,
    load_chat_messages,
    logger,
    save_chat_messages,
)

# Daily auto-clear cadence — user-requested: bot messages should be
# deleted from Telegram once a day, independent of the manual button.
_CC_DAILY_INTERVAL_SECONDS = 24 * 60 * 60


async def _cc_delete_tracked(bot, chat_id: str) -> int:
    """Delete every tracked message_id for one chat via the Bot API,
    skipping (not raising on) any single failure — too old (>48h),
    already deleted, etc. Always clears the chat's tracked list
    afterward, whether or not every delete actually succeeded, so a
    permanently-undeletable entry doesn't get retried forever. Returns
    how many messages were actually deleted."""
    data = load_chat_messages()
    items = data.get(chat_id, [])
    deleted = 0
    for m in items:
        try:
            await bot.delete_message(chat_id=int(chat_id), message_id=m["message_id"])
            deleted += 1
        except Exception as e:
            logger.warning(
                "Clear Chat: failed to delete message %s in chat %s: %s",
                m.get("message_id"), chat_id, e,
            )
    data.pop(chat_id, None)
    save_chat_messages(data)
    return deleted


async def cmd_clear_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """🧹 Clear Chat — deletes every bot message this bot has a record of
    sending in this chat, immediately, no confirmation step."""
    if not allowed(update): return await deny(update)
    chat_id = str(update.effective_chat.id)
    deleted = await _cc_delete_tracked(ctx.bot, chat_id)
    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"🧹 Cleared {deleted} message(s) from this chat.",
        reply_markup=_main_menu_for(update.effective_user.id),
    )


async def _cc_daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Repeating job (every 24h): clears every chat this bot has any
    tracked messages for — not scoped to ALLOWED_IDS, so it also cleans
    up after a user is removed from the allowlist mid-session."""
    data = load_chat_messages()
    for chat_id in list(data.keys()):
        try:
            await _cc_delete_tracked(context.bot, chat_id)
        except Exception as e:
            logger.error("Clear Chat: daily job failed for chat %s: %s", chat_id, e)


def register(app: Application) -> None:
    """Wire the 🧹 Clear Chat button and schedule the daily auto-clear job."""
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_CLEAR_CHAT)}$"), cmd_clear_chat))
    app.job_queue.run_repeating(
        _cc_daily_job,
        interval=_CC_DAILY_INTERVAL_SECONDS,
        first=_CC_DAILY_INTERVAL_SECONDS,
        name="clear_chat_daily_job",
    )
