#!/usr/bin/env python3
"""
Unit tests for add_valuer.py — the ➕ Add Valuer feature: the pure
directory-result parsers (_av_display/_av_uid/_av_acct), the _av_search
directory call (fake session), and the cmd_add_valuer / recv_av_name /
recv_av_select conversation handlers (token-gating, no-results reprompt,
and saving the picked staff member via persist_valuer).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import add_valuer as av


def _run(coro):
    return asyncio.run(coro)


def _result(first=None, middle=None, last=None, user_id=None, rid=None, acct=None):
    # A directory result shaped like list-user-accounts returns.
    return {
        "staff_details": {"firstname": first, "middlename": middle, "lastname": last, "user_id": user_id},
        "id": rid,
        "account_number": acct,
    }


class TestParsers(unittest.TestCase):
    def test_display_joins_present_name_parts(self):
        self.assertEqual(av._av_display(_result("Jane", "M", "Doe")), "Jane M Doe")

    def test_display_skips_missing_parts(self):
        self.assertEqual(av._av_display(_result("Jane", None, "Doe")), "Jane Doe")

    def test_display_empty_when_no_staff_details(self):
        self.assertEqual(av._av_display({}), "")

    def test_uid_prefers_staff_user_id(self):
        self.assertEqual(av._av_uid(_result(user_id="u1", rid="acc1")), "u1")

    def test_uid_falls_back_to_account_id(self):
        self.assertEqual(av._av_uid(_result(user_id=None, rid="acc1")), "acc1")

    def test_uid_empty_when_both_missing(self):
        self.assertEqual(av._av_uid(_result()), "")

    def test_acct_present_and_missing(self):
        self.assertEqual(av._av_acct(_result(acct="A123")), "A123")
        self.assertEqual(av._av_acct(_result()), "")


class TestSearch(unittest.TestCase):
    def test_search_sends_staff_query_and_returns_results(self):
        http = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {"results": [_result("Jane")]}
        http.get.return_value = resp
        tokens = MagicMock(access_token="a", jwt="j")
        out = av._av_search(http, tokens, "jane")
        self.assertEqual(len(out), 1)
        params = http.get.call_args.kwargs["params"]
        self.assertEqual(params["account_type"], "STAFF")
        self.assertEqual(params["search"], "jane")
        resp.raise_for_status.assert_called_once()


class TestCmdAddValuer(unittest.TestCase):
    def _update(self):
        u = MagicMock()
        u.message.reply_text = AsyncMock()
        return u

    def test_no_token_points_to_refresh_auth_and_ends(self):
        u = self._update()
        with patch.object(av, "allowed", return_value=True), \
             patch.object(av, "_any_valid_tokens", return_value=None):
            state = _run(av.cmd_add_valuer(u, MagicMock(user_data={})))
        self.assertEqual(state, av.ConversationHandler.END)
        self.assertIn("Refresh Auth", u.message.reply_text.call_args[0][0])

    def test_with_token_prompts_for_name(self):
        u = self._update()
        ctx = MagicMock(user_data={})
        with patch.object(av, "allowed", return_value=True), \
             patch.object(av, "_any_valid_tokens", return_value=MagicMock()):
            state = _run(av.cmd_add_valuer(u, ctx))
        self.assertEqual(state, av.AV.NAME)
        self.assertIn("av_tokens", ctx.user_data)


class TestRecvName(unittest.TestCase):
    def _update(self, text):
        u = MagicMock()
        u.message.text = text
        u.message.reply_text = AsyncMock()
        return u

    def test_no_results_reprompts(self):
        u = self._update("ghost")
        ctx = MagicMock(user_data={"av_tokens": MagicMock()})
        with patch.object(av, "allowed", return_value=True), \
             patch.object(av, "build_session"), \
             patch.object(av, "_av_search", return_value=[]):
            state = _run(av.recv_av_name(u, ctx))
        self.assertEqual(state, av.AV.NAME)
        self.assertIn("No staff match", u.message.reply_text.call_args[0][0])

    def test_results_show_picker_and_store_results(self):
        u = self._update("jane")
        ctx = MagicMock(user_data={"av_tokens": MagicMock()})
        results = [_result("Jane", None, "Doe"), _result("Janet")]
        with patch.object(av, "allowed", return_value=True), \
             patch.object(av, "build_session"), \
             patch.object(av, "_av_search", return_value=results):
            state = _run(av.recv_av_name(u, ctx))
        self.assertEqual(state, av.AV.SELECT)
        self.assertEqual(ctx.user_data["av_results"], results)

    def test_search_error_reprompts_without_crashing(self):
        u = self._update("jane")
        ctx = MagicMock(user_data={"av_tokens": MagicMock()})
        with patch.object(av, "allowed", return_value=True), \
             patch.object(av, "build_session"), \
             patch.object(av, "_av_search", side_effect=RuntimeError("boom")):
            state = _run(av.recv_av_name(u, ctx))
        self.assertEqual(state, av.AV.NAME)
        self.assertIn("failed", u.message.reply_text.call_args[0][0].lower())


class TestRecvSelect(unittest.TestCase):
    def _update(self, data):
        u = MagicMock()
        q = u.callback_query
        q.data = data
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.message.chat_id = 111
        q.from_user.id = 111
        return u

    def test_selecting_saves_the_valuer(self):
        u = self._update("av:0")
        ctx = MagicMock()
        ctx.user_data = {"av_results": [_result("Jane", "M", "Doe", user_id="u1", acct="A1")]}
        ctx.bot.send_message = AsyncMock()
        with patch.object(av, "persist_valuer") as mock_persist, \
             patch.object(av, "_main_menu_for", return_value=None):
            state = _run(av.recv_av_select(u, ctx))
        self.assertEqual(state, av.ConversationHandler.END)
        mock_persist.assert_called_once_with("Jane M Doe", "u1", "A1")
        self.assertIn("Saved", u.callback_query.edit_message_text.call_args[0][0])

    def test_stale_index_ends_without_saving(self):
        u = self._update("av:5")
        ctx = MagicMock()
        ctx.user_data = {"av_results": [_result("Jane")]}
        with patch.object(av, "persist_valuer") as mock_persist:
            state = _run(av.recv_av_select(u, ctx))
        self.assertEqual(state, av.ConversationHandler.END)
        mock_persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
