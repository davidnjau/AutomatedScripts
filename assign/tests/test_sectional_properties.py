#!/usr/bin/env python3
"""
Unit tests for sectional_properties.py — the specialist-valuer config +
auto-route toggle flow: initial menu rendering, action toggling, name
search, valuer selection, and credential assignment.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sectional_properties as sp
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


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
    query.message.reply_text = AsyncMock()
    return update


class TestCmdSectional(unittest.TestCase):
    def test_no_config_asks_for_name(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(sp, "allowed", return_value=True), \
             patch.object(sp, "load_sectional_config", return_value=None):
            result = _run(sp.cmd_sectional(update, ctx))
        self.assertEqual(result, sp.SC.SET_NAME)

    def test_existing_config_shows_action_menu(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        cfg = {"specialist": {"name": "Jane Doe"}, "auto_route": True, "cred_type": "staff"}
        with patch.object(sp, "allowed", return_value=True), \
             patch.object(sp, "load_sectional_config", return_value=cfg):
            result = _run(sp.cmd_sectional(update, ctx))
        self.assertEqual(result, sp.SC.ACTION)
        self.assertIn("Jane Doe", update.message.reply_text.call_args[0][0])


class TestRecvScAction(unittest.TestCase):
    def test_toggle_route_flips_and_saves(self):
        update = _make_update_with_callback("sc:toggle_route")
        ctx = MagicMock()
        with patch.object(sp, "load_sectional_config", return_value={"auto_route": False}), \
             patch.object(sp, "save_sectional_config") as mock_save:
            result = _run(sp.recv_sc_action(update, ctx))
        mock_save.assert_called_once_with({"auto_route": True})
        self.assertEqual(result, sp.ConversationHandler.END)

    def test_clear_wipes_config(self):
        update = _make_update_with_callback("sc:clear")
        ctx = MagicMock()
        with patch.object(sp, "load_sectional_config", return_value={"specialist": {}}), \
             patch.object(sp, "save_sectional_config") as mock_save:
            result = _run(sp.recv_sc_action(update, ctx))
        mock_save.assert_called_once_with({})
        self.assertEqual(result, sp.ConversationHandler.END)

    def test_change_asks_for_new_name(self):
        update = _make_update_with_callback("sc:change")
        ctx = MagicMock()
        with patch.object(sp, "load_sectional_config", return_value={}):
            result = _run(sp.recv_sc_action(update, ctx))
        self.assertEqual(result, sp.SC.SET_NAME)


class TestRecvScName(unittest.TestCase):
    def test_blank_name_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        result = _run(sp.recv_sc_name(update, ctx))
        self.assertEqual(result, sp.SC.SET_NAME)

    def test_no_tokens_ends_conversation(self):
        update = _make_update_with_message("Jane")
        ctx = MagicMock()
        with patch.object(sp, "_any_valid_tokens", return_value=None):
            result = _run(sp.recv_sc_name(update, ctx))
        self.assertEqual(result, sp.ConversationHandler.END)

    def test_search_failure_ends_conversation(self):
        update = _make_update_with_message("Jane")
        ctx = MagicMock()
        fake_session = MagicMock()
        fake_session.get.side_effect = RuntimeError("network down")
        with patch.object(sp, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(sp, "build_session", return_value=fake_session):
            result = _run(sp.recv_sc_name(update, ctx))
        self.assertEqual(result, sp.ConversationHandler.END)

    def test_no_results_reprompts(self):
        update = _make_update_with_message("Nobody")
        ctx = MagicMock()
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(raise_for_status=lambda: None, json=lambda: {"results": []})
        with patch.object(sp, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(sp, "build_session", return_value=fake_session):
            result = _run(sp.recv_sc_name(update, ctx))
        self.assertEqual(result, sp.SC.SET_NAME)

    def test_results_found_shows_selection_keyboard(self):
        update = _make_update_with_message("Jane")
        ctx = MagicMock()
        ctx.user_data = {}
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [{"first_name": "Jane", "last_name": "Doe", "employee_number": "E1"}]},
        )
        with patch.object(sp, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(sp, "build_session", return_value=fake_session):
            result = _run(sp.recv_sc_name(update, ctx))
        self.assertEqual(result, sp.SC.SELECT)
        self.assertEqual(len(ctx.user_data["sc_results"]), 1)


class TestRecvScSelect(unittest.TestCase):
    def test_invalid_index_ends_conversation(self):
        update = _make_update_with_callback("sc_pick:5")
        ctx = MagicMock()
        ctx.user_data = {"sc_results": []}
        result = _run(sp.recv_sc_select(update, ctx))
        self.assertEqual(result, sp.ConversationHandler.END)

    def test_valid_selection_asks_for_credential(self):
        update = _make_update_with_callback("sc_pick:0")
        ctx = MagicMock()
        ctx.user_data = {"sc_results": [{"id": "1", "first_name": "Jane", "last_name": "Doe", "account_number": "A1"}]}
        result = _run(sp.recv_sc_select(update, ctx))
        self.assertEqual(result, sp.SC.CRED)
        self.assertEqual(ctx.user_data["sc_specialist"]["name"], "Jane Doe")


class TestRecvScCred(unittest.TestCase):
    def test_saves_specialist_and_credential(self):
        update = _make_update_with_callback("sc_cred:staff2")
        ctx = MagicMock()
        ctx.user_data = {"sc_specialist": {"name": "Jane Doe", "uid": "1", "account_number": "A1"}}
        with patch.object(sp, "load_sectional_config", return_value={"auto_route": True}), \
             patch.object(sp, "save_sectional_config") as mock_save:
            result = _run(sp.recv_sc_cred(update, ctx))
        mock_save.assert_called_once_with({
            "auto_route": True,
            "specialist": {"name": "Jane Doe", "uid": "1", "account_number": "A1"},
            "cred_type": "staff2",
        })
        self.assertEqual(result, sp.ConversationHandler.END)


if __name__ == "__main__":
    unittest.main()
