#!/usr/bin/env python3
"""
Unit tests for apartments.py — the specialist-valuer config + auto-route
toggle flow: initial menu rendering, action toggling, name search, valuer
selection, and credential assignment. Mirrors test_sectional_properties.py.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apartments as ap
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


class TestCmdApartments(unittest.TestCase):
    def test_no_config_asks_for_name(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(ap, "allowed", return_value=True), \
             patch.object(ap, "load_apartments_config", return_value=None):
            result = _run(ap.cmd_apartments(update, ctx))
        self.assertEqual(result, ap.AP.SET_NAME)

    def test_existing_config_shows_action_menu(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        cfg = {"specialist": {"name": "Jane Doe"}, "auto_route": True, "cred_type": "staff"}
        with patch.object(ap, "allowed", return_value=True), \
             patch.object(ap, "load_apartments_config", return_value=cfg):
            result = _run(ap.cmd_apartments(update, ctx))
        self.assertEqual(result, ap.AP.ACTION)
        self.assertIn("Jane Doe", update.message.reply_text.call_args[0][0])

    def test_specialist_name_with_special_chars_is_escaped(self):
        """Regression: an unescaped '_' in the specialist name raised
        telegram.error.BadRequest ("can't find end of the entity")."""
        update = _make_update_with_message()
        ctx = MagicMock()
        cfg = {"specialist": {"name": "Jane_Doe"}, "auto_route": True, "cred_type": "staff"}
        with patch.object(ap, "allowed", return_value=True), \
             patch.object(ap, "load_apartments_config", return_value=cfg):
            _run(ap.cmd_apartments(update, ctx))
        self.assertIn("Jane\\_Doe", update.message.reply_text.call_args[0][0])


class TestRecvApAction(unittest.TestCase):
    def test_toggle_route_flips_and_saves(self):
        update = _make_update_with_callback("ap:toggle_route")
        ctx = MagicMock()
        with patch.object(ap, "load_apartments_config", return_value={"auto_route": False}), \
             patch.object(ap, "save_apartments_config") as mock_save:
            result = _run(ap.recv_ap_action(update, ctx))
        mock_save.assert_called_once_with({"auto_route": True})
        self.assertEqual(result, ap.ConversationHandler.END)

    def test_clear_wipes_config(self):
        update = _make_update_with_callback("ap:clear")
        ctx = MagicMock()
        with patch.object(ap, "load_apartments_config", return_value={"specialist": {}}), \
             patch.object(ap, "save_apartments_config") as mock_save:
            result = _run(ap.recv_ap_action(update, ctx))
        mock_save.assert_called_once_with({})
        self.assertEqual(result, ap.ConversationHandler.END)

    def test_change_asks_for_new_name(self):
        update = _make_update_with_callback("ap:change")
        ctx = MagicMock()
        with patch.object(ap, "load_apartments_config", return_value={}):
            result = _run(ap.recv_ap_action(update, ctx))
        self.assertEqual(result, ap.AP.SET_NAME)


class TestRecvApName(unittest.TestCase):
    def test_blank_name_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        result = _run(ap.recv_ap_name(update, ctx))
        self.assertEqual(result, ap.AP.SET_NAME)

    def test_no_tokens_ends_conversation(self):
        update = _make_update_with_message("Jane")
        ctx = MagicMock()
        with patch.object(ap, "_any_valid_tokens", return_value=None):
            result = _run(ap.recv_ap_name(update, ctx))
        self.assertEqual(result, ap.ConversationHandler.END)

    def test_search_failure_ends_conversation(self):
        update = _make_update_with_message("Jane")
        ctx = MagicMock()
        fake_session = MagicMock()
        fake_session.get.side_effect = RuntimeError("network down")
        with patch.object(ap, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(ap, "build_session", return_value=fake_session):
            result = _run(ap.recv_ap_name(update, ctx))
        self.assertEqual(result, ap.ConversationHandler.END)

    def test_no_results_reprompts(self):
        update = _make_update_with_message("Nobody")
        ctx = MagicMock()
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(raise_for_status=lambda: None, json=lambda: {"results": []})
        with patch.object(ap, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(ap, "build_session", return_value=fake_session):
            result = _run(ap.recv_ap_name(update, ctx))
        self.assertEqual(result, ap.AP.SET_NAME)

    def test_results_found_shows_selection_keyboard(self):
        update = _make_update_with_message("Jane")
        ctx = MagicMock()
        ctx.user_data = {}
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [{"first_name": "Jane", "last_name": "Doe", "employee_number": "E1"}]},
        )
        with patch.object(ap, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(ap, "build_session", return_value=fake_session):
            result = _run(ap.recv_ap_name(update, ctx))
        self.assertEqual(result, ap.AP.SELECT)
        self.assertEqual(len(ctx.user_data["ap_results"]), 1)


class TestRecvApSelect(unittest.TestCase):
    def test_invalid_index_ends_conversation(self):
        update = _make_update_with_callback("ap_pick:5")
        ctx = MagicMock()
        ctx.user_data = {"ap_results": []}
        result = _run(ap.recv_ap_select(update, ctx))
        self.assertEqual(result, ap.ConversationHandler.END)

    def test_valid_selection_asks_for_credential(self):
        update = _make_update_with_callback("ap_pick:0")
        ctx = MagicMock()
        ctx.user_data = {"ap_results": [{"id": "1", "first_name": "Jane", "last_name": "Doe", "account_number": "A1"}]}
        result = _run(ap.recv_ap_select(update, ctx))
        self.assertEqual(result, ap.AP.CRED)
        self.assertEqual(ctx.user_data["ap_specialist"]["name"], "Jane Doe")

    def test_name_with_special_chars_is_escaped_in_confirmation(self):
        """Regression: an unescaped '_' in the name raised
        telegram.error.BadRequest ("can't find end of the entity")."""
        update = _make_update_with_callback("ap_pick:0")
        ctx = MagicMock()
        ctx.user_data = {"ap_results": [{"id": "1", "first_name": "Jane_Under", "last_name": "Doe", "account_number": "A1"}]}
        _run(ap.recv_ap_select(update, ctx))
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Jane\\_Under Doe", text)


class TestRecvApCred(unittest.TestCase):
    def test_saves_specialist_and_credential(self):
        update = _make_update_with_callback("ap_cred:staff2")
        ctx = MagicMock()
        ctx.user_data = {"ap_specialist": {"name": "Jane Doe", "uid": "1", "account_number": "A1"}}
        with patch.object(ap, "load_apartments_config", return_value={"auto_route": True}), \
             patch.object(ap, "save_apartments_config") as mock_save:
            result = _run(ap.recv_ap_cred(update, ctx))
        mock_save.assert_called_once_with({
            "auto_route": True,
            "specialist": {"name": "Jane Doe", "uid": "1", "account_number": "A1"},
            "cred_type": "staff2",
        })
        self.assertEqual(result, ap.ConversationHandler.END)

    def test_name_with_special_chars_is_escaped_in_confirmation(self):
        update = _make_update_with_callback("ap_cred:staff2")
        ctx = MagicMock()
        ctx.user_data = {"ap_specialist": {"name": "Jane_Doe", "uid": "1", "account_number": "A1"}}
        with patch.object(ap, "load_apartments_config", return_value={"auto_route": True}), \
             patch.object(ap, "save_apartments_config"):
            _run(ap.recv_ap_cred(update, ctx))
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Jane\\_Doe", text)


if __name__ == "__main__":
    unittest.main()
