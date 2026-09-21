#!/usr/bin/env python3
"""
Unit tests for parcel_lookup.py — _pl_search_parcel's cross-combo dedup/
case-insensitive matching, _pl_format_match's field rendering, and the
cmd_parcel_lookup/recv_pl_parcel/recv_pl_delivery/recv_pl_email
conversation handlers, including the Telegram-vs-email delivery choice.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parcel_lookup as pl
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")

SAMPLE_MATCHES = [
    {"reference_number": "R1", "application_status": "ongoing",
     "node": "VALUATION_STAMP_DUTY_CREATED", "registry": "Nairobi",
     "county": "Nairobi", "date_created": "2026-01-01"},
]


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


def _make_ctx_with_session(parcel="NBI/BLOCK1/123", matches=None):
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot.send_message = AsyncMock()
    sess = pl._get_pl_sess(ctx)
    sess.parcel = parcel
    sess.matches = matches if matches is not None else list(SAMPLE_MATCHES)
    return ctx


class TestPlSearchParcel(unittest.TestCase):
    def test_no_match_across_any_combo_returns_empty(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None, json=lambda: {"results": []}
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(result, [])
        self.assertEqual(fake_session.get.call_count, len(pl._LU_SEARCH_COMBOS))

    def test_case_and_whitespace_insensitive_match(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "  nbi/block1/123  "}
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["reference_number"], "R1")

    def test_non_matching_parcel_on_same_page_is_excluded(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/123"},
                {"reference_number": "R2", "parcel_number": "NBI/BLOCK1/999"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual([r["reference_number"] for r in result], ["R1"])

    def test_same_ref_across_multiple_combos_is_deduped(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/123"}
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(len(result), 1)

    def test_distinct_refs_on_same_parcel_are_both_returned(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/123"},
                {"reference_number": "R2", "parcel_number": "NBI/BLOCK1/123"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(
            sorted(r["reference_number"] for r in result), ["R1", "R2"]
        )

    def test_exception_on_one_combo_continues_to_next(self):
        fake_session = MagicMock()
        fake_session.get.side_effect = [RuntimeError("network blip")] + [
            MagicMock(raise_for_status=lambda: None, json=lambda: {"results": []})
        ] * (len(pl._LU_SEARCH_COMBOS) - 1)
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(result, [])
        self.assertEqual(fake_session.get.call_count, len(pl._LU_SEARCH_COMBOS))


class TestPlFormatMatch(unittest.TestCase):
    def test_renders_known_node_label(self):
        item = {
            "reference_number": "R1",
            "application_status": "ongoing",
            "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
            "registry": "Nairobi",
            "county": "Nairobi",
            "date_created": "2026-01-01",
        }
        block = pl._pl_format_match(1, item)
        self.assertIn("R1", block)
        self.assertIn("ONGOING", block)
        self.assertIn("Assigned", block)
        self.assertIn("Nairobi", block)
        self.assertIn("2026-01-01", block)

    def test_unknown_node_falls_back_to_raw_value(self):
        # Underscores are Markdown-escaped by format_labeled_block's
        # md_escape, so the raw value survives with escaping, not verbatim.
        item = {"reference_number": "R1", "node": "SOME_NEW_NODE"}
        block = pl._pl_format_match(1, item)
        self.assertIn("SOME\\_NEW\\_NODE", block)

    def test_missing_fields_render_as_dash(self):
        item = {"reference_number": "R1"}
        block = pl._pl_format_match(1, item)
        self.assertIn("—", block)


class TestCmdParcelLookup(unittest.TestCase):
    def test_asks_for_parcel_number(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(pl, "allowed", return_value=True):
            result = _run(pl.cmd_parcel_lookup(update, ctx))
        self.assertEqual(result, pl.PL.PARCEL_INPUT)


class TestRecvPlParcel(unittest.TestCase):
    def test_blank_parcel_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        with patch.object(pl, "allowed", return_value=True):
            result = _run(pl.recv_pl_parcel(update, ctx))
        self.assertEqual(result, pl.PL.PARCEL_INPUT)

    def test_no_cached_tokens_ends_conversation(self):
        update = _make_update_with_message("NBI/BLOCK1/123")
        ctx = MagicMock()
        with patch.object(pl, "allowed", return_value=True), \
             patch.object(pl, "get_valid_tokens", return_value=None):
            result = _run(pl.recv_pl_parcel(update, ctx))
        self.assertEqual(result, pl.ConversationHandler.END)

    def test_no_matches_reports_not_found(self):
        update = _make_update_with_message("NBI/BLOCK1/123")
        ctx = MagicMock()
        with patch.object(pl, "allowed", return_value=True), \
             patch.object(pl, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pl, "_pl_search_parcel", return_value=[]):
            result = _run(pl.recv_pl_parcel(update, ctx))
        self.assertEqual(result, pl.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("not found", sent_text)

    def test_matches_found_asks_for_delivery_choice(self):
        update = _make_update_with_message("NBI/BLOCK1/123")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pl, "allowed", return_value=True), \
             patch.object(pl, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pl, "_pl_search_parcel", return_value=list(SAMPLE_MATCHES)):
            result = _run(pl.recv_pl_parcel(update, ctx))
        self.assertEqual(result, pl.PL.DELIVERY)
        sess = pl._get_pl_sess(ctx)
        self.assertEqual(sess.parcel, "NBI/BLOCK1/123")
        self.assertEqual(sess.matches, SAMPLE_MATCHES)
        # Last reply is the delivery-choice prompt with an inline keyboard.
        kwargs = update.message.reply_text.call_args_list[-1].kwargs
        self.assertIn("reply_markup", kwargs)


class TestRecvPlDelivery(unittest.TestCase):
    def test_telegram_mode_sends_report_and_ends(self):
        update = _make_update_with_callback("pl_delivery:telegram")
        ctx = _make_ctx_with_session()
        with patch.object(pl, "allowed", return_value=True):
            result = _run(pl.recv_pl_delivery(update, ctx))
        self.assertEqual(result, pl.ConversationHandler.END)
        ctx.bot.send_message.assert_awaited()
        sent_text = ctx.bot.send_message.call_args_list[-1].args[1]
        self.assertIn("R1", sent_text)

    def test_email_mode_prompts_for_address(self):
        update = _make_update_with_callback("pl_delivery:email")
        ctx = _make_ctx_with_session()
        with patch.object(pl, "allowed", return_value=True):
            result = _run(pl.recv_pl_delivery(update, ctx))
        self.assertEqual(result, pl.PL.EMAIL_INPUT)
        update.callback_query.edit_message_text.assert_awaited()


class TestRecvPlEmail(unittest.TestCase):
    def test_invalid_email_reprompts(self):
        update = _make_update_with_message("not-an-email")
        ctx = _make_ctx_with_session()
        with patch.object(pl, "allowed", return_value=True):
            result = _run(pl.recv_pl_email(update, ctx))
        self.assertEqual(result, pl.PL.EMAIL_INPUT)

    def test_valid_email_sends_and_confirms(self):
        update = _make_update_with_message("someone@example.com")
        ctx = _make_ctx_with_session()
        with patch.object(pl, "allowed", return_value=True), \
             patch.object(pl, "_send_auto_fetch_email") as mock_send:
            result = _run(pl.recv_pl_email(update, ctx))
        self.assertEqual(result, pl.ConversationHandler.END)
        mock_send.assert_called_once()
        args = mock_send.call_args.args
        self.assertEqual(args[0], "someone@example.com")
        self.assertIn("R1", args[2])   # plain-text body includes the ref
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("sent", sent_text.lower())

    def test_send_failure_reports_warning_but_still_ends(self):
        update = _make_update_with_message("someone@example.com")
        ctx = _make_ctx_with_session()
        with patch.object(pl, "allowed", return_value=True), \
             patch.object(pl, "_send_auto_fetch_email", side_effect=RuntimeError("smtp down")):
            result = _run(pl.recv_pl_email(update, ctx))
        self.assertEqual(result, pl.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("failed", sent_text.lower())


if __name__ == "__main__":
    unittest.main()
