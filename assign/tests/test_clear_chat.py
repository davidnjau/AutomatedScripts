#!/usr/bin/env python3
"""
Unit tests for clear_chat.py — the 🧹 Clear Chat button and daily
auto-clear job that delete every message this bot has a record of
sending in a chat (tracked centrally by common.install_message_tracking,
not by this module).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clear_chat


def _run(coro):
    return asyncio.run(coro)


class TestCcDeleteTracked(unittest.TestCase):
    """_cc_delete_tracked — deletes every tracked message for one chat,
    skipping individual failures, and always clears the chat's entry."""

    def test_deletes_every_tracked_message(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock(return_value=True)
        data = {"111": [{"message_id": 1}, {"message_id": 2}]}
        with patch.object(clear_chat, "load_chat_messages", return_value=data), \
             patch.object(clear_chat, "save_chat_messages") as mock_save:
            deleted = _run(clear_chat._cc_delete_tracked(bot, "111"))
        self.assertEqual(deleted, 2)
        self.assertEqual(bot.delete_message.await_count, 2)
        saved = mock_save.call_args[0][0]
        self.assertNotIn("111", saved)

    def test_no_tracked_messages_deletes_nothing(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock()
        with patch.object(clear_chat, "load_chat_messages", return_value={}), \
             patch.object(clear_chat, "save_chat_messages"):
            deleted = _run(clear_chat._cc_delete_tracked(bot, "111"))
        self.assertEqual(deleted, 0)
        bot.delete_message.assert_not_awaited()

    def test_individual_failure_is_skipped_not_raised(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock(side_effect=[Exception("too old"), True])
        data = {"111": [{"message_id": 1}, {"message_id": 2}]}
        with patch.object(clear_chat, "load_chat_messages", return_value=data), \
             patch.object(clear_chat, "save_chat_messages"):
            deleted = _run(clear_chat._cc_delete_tracked(bot, "111"))
        # only the second message counted as successfully deleted
        self.assertEqual(deleted, 1)

    def test_chat_entry_cleared_even_if_every_delete_fails(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock(side_effect=Exception("gone"))
        data = {"111": [{"message_id": 1}]}
        with patch.object(clear_chat, "load_chat_messages", return_value=data), \
             patch.object(clear_chat, "save_chat_messages") as mock_save:
            deleted = _run(clear_chat._cc_delete_tracked(bot, "111"))
        self.assertEqual(deleted, 0)
        self.assertNotIn("111", mock_save.call_args[0][0])

    def test_other_chats_left_untouched(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock(return_value=True)
        data = {"111": [{"message_id": 1}], "222": [{"message_id": 5}]}
        with patch.object(clear_chat, "load_chat_messages", return_value=data), \
             patch.object(clear_chat, "save_chat_messages") as mock_save:
            _run(clear_chat._cc_delete_tracked(bot, "111"))
        saved = mock_save.call_args[0][0]
        self.assertIn("222", saved)
        self.assertNotIn("111", saved)


class TestCmdClearChat(unittest.TestCase):
    """cmd_clear_chat — deletes immediately, no confirmation step, and
    reports how many messages were cleared."""

    def _make_update(self, chat_id=111, user_id=111):
        update = MagicMock()
        update.effective_chat.id = chat_id
        update.effective_user.id = user_id
        return update

    def test_denies_unauthorized_user(self):
        update = self._make_update()
        ctx = MagicMock()
        with patch.object(clear_chat, "allowed", return_value=False), \
             patch.object(clear_chat, "deny", new=AsyncMock()) as mock_deny:
            _run(clear_chat.cmd_clear_chat(update, ctx))
        mock_deny.assert_awaited_once_with(update)

    def test_reports_deleted_count(self):
        update = self._make_update()
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with patch.object(clear_chat, "allowed", return_value=True), \
             patch.object(clear_chat, "_cc_delete_tracked", new=AsyncMock(return_value=7)):
            _run(clear_chat.cmd_clear_chat(update, ctx))
        text = ctx.bot.send_message.call_args.kwargs["text"]
        self.assertIn("7", text)

    def test_zero_deleted_still_sends_confirmation(self):
        update = self._make_update()
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with patch.object(clear_chat, "allowed", return_value=True), \
             patch.object(clear_chat, "_cc_delete_tracked", new=AsyncMock(return_value=0)):
            _run(clear_chat.cmd_clear_chat(update, ctx))
        text = ctx.bot.send_message.call_args.kwargs["text"]
        self.assertIn("0", text)


class TestCcDailyJob(unittest.TestCase):
    """_cc_daily_job — clears every chat with tracked messages, not just
    ALLOWED_IDS, and one chat's failure doesn't block the rest."""

    def test_clears_every_tracked_chat(self):
        context = MagicMock()
        data = {"111": [{"message_id": 1}], "222": [{"message_id": 2}]}
        with patch.object(clear_chat, "load_chat_messages", return_value=data), \
             patch.object(clear_chat, "_cc_delete_tracked", new=AsyncMock(return_value=1)) as mock_delete:
            _run(clear_chat._cc_daily_job(context))
        chat_ids = {c.args[1] for c in mock_delete.call_args_list}
        self.assertEqual(chat_ids, {"111", "222"})

    def test_no_tracked_chats_is_a_noop(self):
        context = MagicMock()
        with patch.object(clear_chat, "load_chat_messages", return_value={}), \
             patch.object(clear_chat, "_cc_delete_tracked", new=AsyncMock()) as mock_delete:
            _run(clear_chat._cc_daily_job(context))
        mock_delete.assert_not_awaited()

    def test_one_chat_failure_does_not_block_the_rest(self):
        context = MagicMock()
        data = {"111": [{"message_id": 1}], "222": [{"message_id": 2}]}
        with patch.object(clear_chat, "load_chat_messages", return_value=data), \
             patch.object(clear_chat, "_cc_delete_tracked", new=AsyncMock(side_effect=[Exception("boom"), 1])) as mock_delete:
            _run(clear_chat._cc_daily_job(context))
        self.assertEqual(mock_delete.await_count, 2)


class TestRegister(unittest.TestCase):
    """register() wires the button handler and schedules the daily job
    with the expected 24h cadence."""

    def test_schedules_daily_job(self):
        app = MagicMock()
        clear_chat.register(app)
        app.job_queue.run_repeating.assert_called_once()
        _, kwargs = app.job_queue.run_repeating.call_args
        self.assertEqual(kwargs["interval"], 24 * 60 * 60)
        self.assertEqual(kwargs["first"], 24 * 60 * 60)
        self.assertEqual(kwargs["name"], "clear_chat_daily_job")

    def test_adds_button_handler(self):
        app = MagicMock()
        clear_chat.register(app)
        app.add_handler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
