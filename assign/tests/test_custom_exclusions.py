#!/usr/bin/env python3
"""
Unit tests for custom_exclusions.py — the Manage Exclusions menu:
listing/add/remove of custom parcel_number keywords that feed into Auto
Fetch's Exclude multi-select alongside the 9 built-in keywords.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import custom_exclusions as ce


def _run(coro):
    return asyncio.run(coro)


def _make_update_with_message(text=""):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_update_with_callback(data):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    return update


class TestCmdExclusions(unittest.TestCase):
    def test_empty_list_shows_no_remove_button(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(ce, "allowed", return_value=True), \
             patch.object(ce, "load_custom_exclusions", return_value=[]):
            result = _run(ce.cmd_exclusions(update, ctx))
        self.assertEqual(result, ce.CE.ACTION)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("No custom keywords yet", text)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        button_data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn("ce:remove", button_data)

    def test_existing_list_is_shown_and_remove_button_present(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(ce, "allowed", return_value=True), \
             patch.object(ce, "load_custom_exclusions", return_value=["MAISONETTE"]):
            result = _run(ce.cmd_exclusions(update, ctx))
        self.assertEqual(result, ce.CE.ACTION)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("MAISONETTE", text)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        button_data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("ce:remove", button_data)


class TestRecvCeAction(unittest.TestCase):
    def test_close_ends_conversation(self):
        update = _make_update_with_callback("ce:close")
        ctx = MagicMock()
        result = _run(ce.recv_ce_action(update, ctx))
        self.assertEqual(result, ce.ConversationHandler.END)

    def test_add_moves_to_add_state(self):
        update = _make_update_with_callback("ce:add")
        ctx = MagicMock()
        result = _run(ce.recv_ce_action(update, ctx))
        self.assertEqual(result, ce.CE.ADD)

    def test_remove_with_no_custom_keywords_ends_conversation(self):
        update = _make_update_with_callback("ce:remove")
        ctx = MagicMock()
        with patch.object(ce, "load_custom_exclusions", return_value=[]):
            result = _run(ce.recv_ce_action(update, ctx))
        self.assertEqual(result, ce.ConversationHandler.END)

    def test_remove_with_custom_keywords_shows_picker(self):
        update = _make_update_with_callback("ce:remove")
        ctx = MagicMock()
        with patch.object(ce, "load_custom_exclusions", return_value=["MAISONETTE", "TOWNHOUSE"]):
            result = _run(ce.recv_ce_action(update, ctx))
        self.assertEqual(result, ce.CE.REMOVE)
        markup = update.callback_query.edit_message_text.call_args[1]["reply_markup"]
        button_data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("ce_remove:MAISONETTE", button_data)
        self.assertIn("ce_remove:TOWNHOUSE", button_data)


class TestRecvCeAdd(unittest.TestCase):
    def test_blank_keyword_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        result = _run(ce.recv_ce_add(update, ctx))
        self.assertEqual(result, ce.CE.ADD)

    def test_too_long_keyword_reprompts(self):
        update = _make_update_with_message("X" * 25)
        ctx = MagicMock()
        result = _run(ce.recv_ce_add(update, ctx))
        self.assertEqual(result, ce.CE.ADD)

    def test_builtin_keyword_rejected(self):
        update = _make_update_with_message("flat")
        ctx = MagicMock()
        with patch.object(ce, "load_custom_exclusions", return_value=[]), \
             patch.object(ce, "save_custom_exclusions") as mock_save:
            result = _run(ce.recv_ce_add(update, ctx))
        self.assertEqual(result, ce.CE.ADD)
        mock_save.assert_not_called()
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("built-in", text)

    def test_duplicate_custom_keyword_rejected(self):
        update = _make_update_with_message("maisonette")
        ctx = MagicMock()
        with patch.object(ce, "load_custom_exclusions", return_value=["MAISONETTE"]), \
             patch.object(ce, "save_custom_exclusions") as mock_save:
            result = _run(ce.recv_ce_add(update, ctx))
        self.assertEqual(result, ce.CE.ADD)
        mock_save.assert_not_called()

    def test_valid_keyword_is_saved_uppercased(self):
        update = _make_update_with_message("townhouse")
        ctx = MagicMock()
        with patch.object(ce, "load_custom_exclusions", return_value=["MAISONETTE"]), \
             patch.object(ce, "save_custom_exclusions") as mock_save:
            result = _run(ce.recv_ce_add(update, ctx))
        self.assertEqual(result, ce.ConversationHandler.END)
        mock_save.assert_called_once_with(["MAISONETTE", "TOWNHOUSE"])
        confirm_text = update.message.reply_text.call_args_list[0][0][0]
        self.assertIn("TOWNHOUSE", confirm_text)


class TestRecvCeRemove(unittest.TestCase):
    def test_cancel_ends_without_removing(self):
        update = _make_update_with_callback("ce_remove:cancel")
        ctx = MagicMock()
        with patch.object(ce, "save_custom_exclusions") as mock_save:
            result = _run(ce.recv_ce_remove(update, ctx))
        self.assertEqual(result, ce.ConversationHandler.END)
        mock_save.assert_not_called()

    def test_removes_matching_keyword(self):
        update = _make_update_with_callback("ce_remove:MAISONETTE")
        ctx = MagicMock()
        with patch.object(ce, "load_custom_exclusions", return_value=["MAISONETTE", "TOWNHOUSE"]), \
             patch.object(ce, "save_custom_exclusions") as mock_save:
            result = _run(ce.recv_ce_remove(update, ctx))
        self.assertEqual(result, ce.ConversationHandler.END)
        mock_save.assert_called_once_with(["TOWNHOUSE"])
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Removed", text)

    def test_already_removed_keyword_shows_warning(self):
        update = _make_update_with_callback("ce_remove:GONE")
        ctx = MagicMock()
        with patch.object(ce, "load_custom_exclusions", return_value=["MAISONETTE"]), \
             patch.object(ce, "save_custom_exclusions") as mock_save:
            result = _run(ce.recv_ce_remove(update, ctx))
        self.assertEqual(result, ce.ConversationHandler.END)
        mock_save.assert_not_called()
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("already removed", text)


if __name__ == "__main__":
    unittest.main()
