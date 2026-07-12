#!/usr/bin/env python3
"""
Unit tests for lookup_reference.py — _lu_search_ref's filter/role combo
fallback, _lu_format_result's field extraction, and the cmd_lookup/
recv_lu_cred/recv_lu_ref conversation handlers.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lookup_reference as lu
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


class TestLuSearchRef(unittest.TestCase):
    def test_returns_none_when_no_combo_matches(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None, json=lambda: {"results": []}
        )
        with patch.object(lu, "build_session", return_value=fake_session):
            result = lu._lu_search_ref(TOKENS, "R1")
        self.assertIsNone(result)
        self.assertEqual(fake_session.get.call_count, len(lu._LU_SEARCH_COMBOS))

    def test_returns_first_matching_combo(self):
        fake_session = MagicMock()
        fake_session.get.side_effect = [
            MagicMock(raise_for_status=lambda: None, json=lambda: {"results": []}),
            MagicMock(raise_for_status=lambda: None,
                      json=lambda: {"results": [{"reference_number": "R1", "id": "app-1"}]}),
        ]
        with patch.object(lu, "build_session", return_value=fake_session):
            result = lu._lu_search_ref(TOKENS, "R1")
        self.assertEqual(result["id"], "app-1")
        self.assertEqual(result["_matched_filter"], lu._LU_SEARCH_COMBOS[1][0])

    def test_exception_on_one_combo_continues_to_next(self):
        fake_session = MagicMock()
        fake_session.get.side_effect = [
            RuntimeError("network blip"),
            MagicMock(raise_for_status=lambda: None,
                      json=lambda: {"results": [{"reference_number": "R1", "id": "app-2"}]}),
        ]
        with patch.object(lu, "build_session", return_value=fake_session):
            result = lu._lu_search_ref(TOKENS, "R1")
        self.assertEqual(result["id"], "app-2")


class TestLuFormatResult(unittest.TestCase):
    def test_formats_with_no_detail(self):
        item = {"application_status": "ongoing", "registry": "NAIROBI",
                 "county": "NAIROBI", "date_created": "2026-01-01"}
        result = lu._lu_format_result("R1", item, None)
        self.assertIn("R1", result)
        self.assertIn("ONGOING", result)
        self.assertIn("—", result)   # valuer/consideration default when no detail

    def test_formats_with_detail_extracts_valuer_and_consideration(self):
        item = {"application_status": "ongoing", "date_created": "2026-01-01"}
        detail = {
            "node": "VALUATION_STAMP_DUTY_CREATED",
            "external_process_details": {"consideration_amount": "500000", "currency_code": "KES"},
            "actors": [{"role": "VALUATION OFFICER", "user_details": {"names": "JANE DOE"}}],
            "registry": "NAIROBI", "county": "NAIROBI",
        }
        result = lu._lu_format_result("R1", item, detail)
        self.assertIn("JANE DOE", result)
        self.assertIn("KES 500,000.00", result)
        self.assertIn("Unassigned", result)   # node label lookup


class TestCmdLookup(unittest.TestCase):
    def test_no_valid_tokens_ends_conversation(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "_be_cred_keyboard", return_value=None):
            result = _run(lu.cmd_lookup(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)

    def test_valid_tokens_ask_for_credential(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "_be_cred_keyboard", return_value=MagicMock()):
            result = _run(lu.cmd_lookup(update, ctx))
        self.assertEqual(result, lu.LU.PICK_CRED)


class TestRecvLuRef(unittest.TestCase):
    def test_blank_ref_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        ctx.user_data = {"lu_session": lu.LUSession(cred_type="staff")}
        with patch.object(lu, "allowed", return_value=True):
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.LU.REF_INPUT)

    def test_expired_tokens_end_conversation(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        ctx.user_data = {"lu_session": lu.LUSession(cred_type="staff")}
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=None):
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)

    def test_ref_not_found_ends_conversation(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        ctx.user_data = {"lu_session": lu.LUSession(cred_type="staff")}
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(lu, "_lu_search_ref", return_value=None):
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)

    def test_ref_found_formats_and_ends_conversation(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        ctx.user_data = {"lu_session": lu.LUSession(cred_type="staff")}
        item = {"id": "app-1", "reference_number": "R1"}
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(lu, "_lu_search_ref", return_value=item), \
             patch.object(lu, "_lu_fetch_detail", return_value=None), \
             patch.object(lu, "_lu_format_result", return_value="formatted") as mock_fmt:
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)
        mock_fmt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
