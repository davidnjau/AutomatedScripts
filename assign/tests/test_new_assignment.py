#!/usr/bin/env python3
"""
Unit tests for new_assignment.py — parse_refs/_extract_refs_from_text's
pure parsing logic, _check_assignments_and_proceed's already-assigned
routing, and the cmd_assign/recv_refs/recv_cred_choice/recv_confirm
conversation handlers.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import new_assignment as na
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
    query.message.chat_id = 555
    return update


class TestParseRefs(unittest.TestCase):
    def test_comma_separated(self):
        self.assertEqual(na.parse_refs("A/B/1, A/B/2"), ["A/B/1", "A/B/2"])

    def test_newline_separated(self):
        self.assertEqual(na.parse_refs("A/B/1\nA/B/2"), ["A/B/1", "A/B/2"])

    def test_strips_whitespace_and_drops_empty(self):
        self.assertEqual(na.parse_refs(" A/B/1 ,, \n A/B/2 "), ["A/B/1", "A/B/2"])

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(na.parse_refs(""), [])


class TestExtractRefsFromText(unittest.TestCase):
    def test_matches_slash_separated_pattern(self):
        refs = na._extract_refs_from_text("Doc mentions LS/VAL/2024/001 somewhere.")
        self.assertEqual(refs, ["LS/VAL/2024/001"])

    def test_dedupes_preserving_order(self):
        refs = na._extract_refs_from_text("LS/VAL/2024/001 and LS/VAL/2024/001 again, then LS/VAL/2024/002")
        self.assertEqual(refs, ["LS/VAL/2024/001", "LS/VAL/2024/002"])

    def test_no_match_returns_empty(self):
        self.assertEqual(na._extract_refs_from_text("nothing matching here"), [])


class TestCheckAssignmentsAndProceed(unittest.TestCase):
    def test_no_existing_assignments_proceeds_to_valuer_pick(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = na.Session(refs=["R1", "R2"])
        with patch.object(na, "load_saved_assignments", return_value={}), \
             patch.object(na, "load_saved_valuers", return_value=[]):
            result = _run(na._check_assignments_and_proceed(message, sess))
        self.assertEqual(result, na.S.VALUER_NAME)

    def test_some_existing_assignments_asks_reassign(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = na.Session(refs=["R1", "R2"])
        existing = {"R1": {"valuer_name": "Jane", "assigned_at": "2026-01-01"}}
        with patch.object(na, "load_saved_assignments", return_value=existing):
            result = _run(na._check_assignments_and_proceed(message, sess))
        self.assertEqual(result, na.S.REASSIGN_CONFIRM)
        self.assertEqual(sess.already_assigned[0]["ref"], "R1")


class TestProceedToValuerPick(unittest.TestCase):
    def test_saved_valuers_show_pick_source(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = na.Session()
        saved = [{"name": "Jane Doe", "uid": "1", "account_number": "A1"}]
        with patch.object(na, "load_saved_valuers", return_value=saved):
            result = _run(na._proceed_to_valuer_pick(message, sess, ["R1"]))
        self.assertEqual(result, na.S.PICK_VALUER_SOURCE)

    def test_no_saved_valuers_asks_for_name(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = na.Session()
        with patch.object(na, "load_saved_valuers", return_value=[]):
            result = _run(na._proceed_to_valuer_pick(message, sess, ["R1"]))
        self.assertEqual(result, na.S.VALUER_NAME)


class TestCmdAssign(unittest.TestCase):
    def test_resets_session_and_asks_input_method(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(na, "allowed", return_value=True):
            result = _run(na.cmd_assign(update, ctx))
        self.assertEqual(result, na.S.INPUT_METHOD)
        self.assertIsInstance(ctx.user_data["session"], na.Session)


class TestRecvRefs(unittest.TestCase):
    def test_no_valid_refs_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        ctx.user_data = {"session": na.Session()}
        result = _run(na.recv_refs(update, ctx))
        self.assertEqual(result, na.S.REF_NUMBERS)

    def test_valid_refs_proceed(self):
        update = _make_update_with_message("R1, R2")
        ctx = MagicMock()
        ctx.user_data = {"session": na.Session()}
        with patch.object(na, "load_saved_assignments", return_value={}), \
             patch.object(na, "load_saved_valuers", return_value=[]):
            result = _run(na.recv_refs(update, ctx))
        self.assertEqual(result, na.S.VALUER_NAME)
        self.assertEqual(ctx.user_data["session"].refs, ["R1", "R2"])


class TestRecvCredChoice(unittest.TestCase):
    def test_cached_tokens_with_saved_valuer_jumps_to_confirm(self):
        update = _make_update_with_callback("cred:staff")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"})
        ctx.user_data = {"session": sess}
        with patch.object(na, "get_valid_tokens", return_value=TOKENS), \
             patch.object(na, "build_session", return_value=MagicMock()):
            result = _run(na.recv_cred_choice(update, ctx))
        self.assertEqual(result, na.S.CONFIRM)

    def test_cached_tokens_no_saved_valuer_searches(self):
        update = _make_update_with_callback("cred:staff")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], valuer_name="Jane Doe")
        ctx.user_data = {"session": sess}
        with patch.object(na, "get_valid_tokens", return_value=TOKENS), \
             patch.object(na, "build_session", return_value=MagicMock()), \
             patch.object(na, "_do_valuer_search", new=AsyncMock(return_value=[])):
            result = _run(na.recv_cred_choice(update, ctx))
        self.assertEqual(result, na.ConversationHandler.END)


class TestRecvConfirm(unittest.TestCase):
    def test_no_cancels_conversation(self):
        update = _make_update_with_callback("confirm:no")
        ctx = MagicMock()
        ctx.user_data = {"session": na.Session()}
        result = _run(na.recv_confirm(update, ctx))
        self.assertEqual(result, na.ConversationHandler.END)

    def test_yes_assigns_and_shows_summary(self):
        update = _make_update_with_callback("confirm:yes")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], tokens=TOKENS, saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"})
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"session": sess}
        with patch.object(na, "persist_valuer"), \
             patch.object(na, "persist_assignment"), \
             patch.object(na.asyncio, "to_thread", new=AsyncMock(return_value=[])):
            result = _run(na.recv_confirm(update, ctx))
        self.assertEqual(result, na.ConversationHandler.END)


if __name__ == "__main__":
    unittest.main()
