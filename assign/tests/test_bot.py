#!/usr/bin/env python3
"""
Unit tests for bot.py — currently just _on_error, the global exception
handler. Regression coverage for a real bug: this used to be the one
place in the whole bot that sent a message with no reply_markup at all,
so an exception firing after a flow had hidden the persistent keyboard
(ReplyKeyboardRemove — New Assignment, DLV Batch, Receive Tasks, Post
Board's posting entry) but before that flow's own completion step left
the keyboard hidden indefinitely, with no way back except /start.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update  # noqa: E402

import bot  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class TestOnError(unittest.TestCase):
    def _make_update(self, user_id=111, chat_id=111, has_user=True):
        update = MagicMock(spec=Update)
        update.effective_chat.id = chat_id
        update.effective_user = MagicMock(id=user_id) if has_user else None
        return update

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.error = RuntimeError("boom")
        return ctx

    def test_reattaches_main_menu_keyboard(self):
        update = self._make_update(user_id=111)
        ctx = self._make_ctx()
        sentinel_menu = object()
        with patch.object(bot, "_main_menu_for", return_value=sentinel_menu) as mock_menu:
            _run(bot._on_error(update, ctx))
        mock_menu.assert_called_once_with(111)
        self.assertIs(ctx.bot.send_message.call_args.kwargs["reply_markup"], sentinel_menu)

    def test_falls_back_to_chat_id_when_no_effective_user(self):
        """Defensive fallback only — this bot is private/1:1-DM-only, so
        chat_id and user_id are always numerically equal in practice."""
        update = self._make_update(chat_id=222, has_user=False)
        ctx = self._make_ctx()
        with patch.object(bot, "_main_menu_for") as mock_menu:
            _run(bot._on_error(update, ctx))
        mock_menu.assert_called_once_with(222)

    def test_sends_a_user_facing_message(self):
        update = self._make_update()
        ctx = self._make_ctx()
        _run(bot._on_error(update, ctx))
        text = ctx.bot.send_message.call_args[0][1]
        self.assertIn("Something went wrong", text)

    def test_non_update_object_does_not_send_anything(self):
        ctx = self._make_ctx()
        _run(bot._on_error("not an update", ctx))
        ctx.bot.send_message.assert_not_awaited()

    def test_no_effective_chat_does_not_send_anything(self):
        update = MagicMock(spec=Update)
        update.effective_chat = None
        ctx = self._make_ctx()
        _run(bot._on_error(update, ctx))
        ctx.bot.send_message.assert_not_awaited()

    def test_send_failure_does_not_propagate(self):
        update = self._make_update()
        ctx = self._make_ctx()
        ctx.bot.send_message = AsyncMock(side_effect=RuntimeError("network down"))
        _run(bot._on_error(update, ctx))  # must not raise


if __name__ == "__main__":
    unittest.main()
