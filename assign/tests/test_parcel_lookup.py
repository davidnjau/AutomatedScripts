#!/usr/bin/env python3
"""
Unit tests for parcel_lookup.py — _pl_search_parcel_valuer_stage's
cross-combo dedup/case-insensitive matching, _pl_search_parcel_assessor_stage's
pre-DLV HQ/County variants, _pl_search_parcel's merge-of-both-stages
behavior, _pl_format_match's field rendering (including the assessor-stage
label path), and the cmd_parcel_lookup/recv_pl_parcel/recv_pl_delivery/
recv_pl_email conversation handlers, including the Telegram-vs-email
delivery choice.

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


class TestPlSearchParcelValuerStage(unittest.TestCase):
    def test_no_match_across_any_combo_returns_empty(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None, json=lambda: {"results": []}
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "NBI/BLOCK1/123")
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
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "NBI/BLOCK1/123")
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
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "NBI/BLOCK1/123")
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
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "NBI/BLOCK1/123")
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
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(
            sorted(r["reference_number"] for r in result), ["R1", "R2"]
        )

    def test_exception_on_one_combo_continues_to_next(self):
        fake_session = MagicMock()
        fake_session.get.side_effect = [RuntimeError("network blip")] + [
            MagicMock(raise_for_status=lambda: None, json=lambda: {"results": []})
        ] * (len(pl._LU_SEARCH_COMBOS) - 1)
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(result, [])
        self.assertEqual(fake_session.get.call_count, len(pl._LU_SEARCH_COMBOS))

    def test_fragment_of_parcel_number_matches(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NAIROBI/BLOCK209/309"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "BLOCK209")
        self.assertEqual([r["reference_number"] for r in result], ["R1"])

    def test_different_separators_still_match(self):
        """A search entered with dashes/spaces must still match a parcel
        number stored with slashes — formatting shouldn't matter."""
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/123"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "NBI-BLOCK1-123")
        self.assertEqual([r["reference_number"] for r in result], ["R1"])

    def test_unrelated_fragment_does_not_match(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/123"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "BLOCK9")
        self.assertEqual(result, [])

    def test_empty_normalized_search_term_matches_nothing(self):
        """A search term that normalizes to nothing (e.g. only punctuation)
        must not fall through to matching every result."""
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/123"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "///")
        self.assertEqual(result, [])

    def test_abbreviated_word_matches_full_word_end_to_end(self):
        """The reported case: "Mavoko Muni block 123/145/" must match
        "Mavoko/Municipality/block123/145/5" even though a plain
        (non-tokenized) substring check would fail — "Muni" jumps
        straight to "block" while the real text has "cipality" between."""
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "Mavoko/Municipality/block123/145/5"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "Mavoko Muni block 123/145/")
        self.assertEqual([r["reference_number"] for r in result], ["R1"])

    def test_short_number_fragment_does_not_match_end_to_end(self):
        """Regression: number tokens must match exactly, even end-to-end
        through _pl_search_parcel_valuer_stage — "12" must not match a parcel
        containing "123"."""
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/123"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_valuer_stage(TOKENS, "12")
        self.assertEqual(result, [])


class TestPlSearchParcelAssessorStage(unittest.TestCase):
    """_pl_search_parcel_assessor_stage — the pre-DLV assessor/HQ collector
    search, tried against both _PL_ASSESSOR_VARIANTS (HQ, County)."""

    def test_match_is_tagged_as_assessor_stage(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/123", "id": "9"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_assessor_stage(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["_assessor_stage"])
        self.assertIn(result[0]["_matched_filter"], ("Assessor/HQ", "Assessor/County"))

    def test_both_hq_and_county_variants_are_queried(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None, json=lambda: {"results": []}
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            pl._pl_search_parcel_assessor_stage(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(fake_session.get.call_count, len(pl._PL_ASSESSOR_VARIANTS))
        from_ardhipay_values = [
            call.kwargs["params"].get("from_ardhipay") for call in fake_session.get.call_args_list
        ]
        self.assertIn("true", from_ardhipay_values)
        self.assertIn(None, from_ardhipay_values)

    def test_non_matching_parcel_excluded(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/999"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_assessor_stage(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(result, [])

    def test_same_ref_across_both_variants_is_deduped(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [
                {"reference_number": "R1", "parcel_number": "NBI/BLOCK1/123"},
            ]},
        )
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_assessor_stage(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(len(result), 1)

    def test_exception_on_one_variant_continues_to_next(self):
        fake_session = MagicMock()
        fake_session.get.side_effect = [
            RuntimeError("network blip"),
            MagicMock(raise_for_status=lambda: None, json=lambda: {"results": []}),
        ]
        with patch.object(pl, "build_session", return_value=fake_session):
            result = pl._pl_search_parcel_assessor_stage(TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(result, [])
        self.assertEqual(fake_session.get.call_count, len(pl._PL_ASSESSOR_VARIANTS))


class TestPlSearchParcel(unittest.TestCase):
    """_pl_search_parcel — merges the valuer-stage and assessor-stage
    searches, silently skipping either stage when its token set is None."""

    def test_merges_matches_from_both_stages(self):
        with patch.object(pl, "_pl_search_parcel_valuer_stage", return_value=[{"reference_number": "R1"}]), \
             patch.object(pl, "_pl_search_parcel_assessor_stage", return_value=[{"reference_number": "R2"}]):
            result = pl._pl_search_parcel(TOKENS, TOKENS, "NBI/BLOCK1/123")
        self.assertEqual(sorted(r["reference_number"] for r in result), ["R1", "R2"])

    def test_missing_assessor_tokens_skips_that_stage_only(self):
        with patch.object(pl, "_pl_search_parcel_valuer_stage", return_value=[{"reference_number": "R1"}]) as mock_valuer, \
             patch.object(pl, "_pl_search_parcel_assessor_stage") as mock_assessor:
            result = pl._pl_search_parcel(TOKENS, None, "NBI/BLOCK1/123")
        self.assertEqual([r["reference_number"] for r in result], ["R1"])
        mock_valuer.assert_called_once()
        mock_assessor.assert_not_called()

    def test_missing_valuer_tokens_skips_that_stage_only(self):
        with patch.object(pl, "_pl_search_parcel_valuer_stage") as mock_valuer, \
             patch.object(pl, "_pl_search_parcel_assessor_stage", return_value=[{"reference_number": "R2"}]) as mock_assessor:
            result = pl._pl_search_parcel(None, TOKENS, "NBI/BLOCK1/123")
        self.assertEqual([r["reference_number"] for r in result], ["R2"])
        mock_valuer.assert_not_called()
        mock_assessor.assert_called_once()

    def test_both_tokens_none_returns_empty_without_any_call(self):
        with patch.object(pl, "_pl_search_parcel_valuer_stage") as mock_valuer, \
             patch.object(pl, "_pl_search_parcel_assessor_stage") as mock_assessor:
            result = pl._pl_search_parcel(None, None, "NBI/BLOCK1/123")
        self.assertEqual(result, [])
        mock_valuer.assert_not_called()
        mock_assessor.assert_not_called()


class TestPlTokenizeParcel(unittest.TestCase):
    def test_splits_words_and_numbers_uppercased(self):
        self.assertEqual(pl._pl_tokenize_parcel("Nbi/Block1/123"), ["NBI", "BLOCK", "1", "123"])

    def test_space_separated_and_glued_forms_tokenize_the_same(self):
        self.assertEqual(pl._pl_tokenize_parcel("Block 123"), pl._pl_tokenize_parcel("block123"))

    def test_different_separators_tokenize_the_same(self):
        self.assertEqual(pl._pl_tokenize_parcel("NBI-BLOCK1-123"), pl._pl_tokenize_parcel("NBI/BLOCK1/123"))

    def test_none_returns_empty_list(self):
        self.assertEqual(pl._pl_tokenize_parcel(None), [])

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(pl._pl_tokenize_parcel(""), [])


class TestPlTokensMatch(unittest.TestCase):
    """_pl_tokens_match — word tokens match as a prefix (abbreviations),
    number tokens must match exactly, and candidate tokens can be
    skipped (omitted words) but not reordered or reused."""

    def test_exact_tokens_match(self):
        self.assertTrue(pl._pl_tokens_match(["NBI", "BLOCK", "123"], ["NBI", "BLOCK", "123"]))

    def test_word_prefix_matches_abbreviation(self):
        # "Mavoko Muni block 123/145" vs "Mavoko/Municipality/block123/145/5"
        search = pl._pl_tokenize_parcel("Mavoko Muni block 123/145/")
        candidate = pl._pl_tokenize_parcel("Mavoko/Municipality/block123/145/5")
        self.assertTrue(pl._pl_tokens_match(search, candidate))

    def test_omitted_candidate_word_does_not_break_match(self):
        search = pl._pl_tokenize_parcel("Mavoko block 123")
        candidate = pl._pl_tokenize_parcel("Mavoko/Municipality/block123")
        self.assertTrue(pl._pl_tokens_match(search, candidate))

    def test_number_prefix_does_not_match(self):
        """Regression: a number token must match exactly — "12" must not
        loosely match "123", unlike word tokens."""
        self.assertFalse(pl._pl_tokens_match(["12"], ["123"]))

    def test_number_exact_match_succeeds(self):
        self.assertTrue(pl._pl_tokens_match(["123"], ["123"]))

    def test_out_of_order_tokens_do_not_match(self):
        self.assertFalse(pl._pl_tokens_match(["BLOCK", "MAVOKO"], ["MAVOKO", "BLOCK"]))

    def test_extra_search_token_not_in_candidate_fails(self):
        self.assertFalse(pl._pl_tokens_match(["MAVOKO", "KIAMBU"], ["MAVOKO", "BLOCK"]))

    def test_empty_search_tokens_trivially_matches(self):
        self.assertTrue(pl._pl_tokens_match([], ["MAVOKO", "BLOCK"]))


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

    def test_assessor_stage_match_renders_stage_label_not_raw_status(self):
        # An assessor-stage item never carries application_status/node —
        # even if present (shouldn't happen, but assert the stage label
        # wins regardless) the rendered block shows the pre-DLV stage.
        item = {
            "reference_number": "R1",
            "_assessor_stage": True,
            "_matched_filter": "Assessor/County",
            "application_status": "ongoing",
            "registry": "Nairobi",
        }
        block = pl._pl_format_match(1, item)
        self.assertIn("R1", block)
        self.assertIn("PRE-DLV", block)
        self.assertIn("Assessor/County", block)
        self.assertIn("Assessor", block)
        self.assertNotIn("ONGOING", block)


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

    def test_too_many_parcels_ends_conversation_without_searching(self):
        update = _make_update_with_message("\n".join(f"P{i}" for i in range(pl._LIST_INPUT_MAX_ITEMS + 1)))
        ctx = MagicMock()
        with patch.object(pl, "allowed", return_value=True), \
             patch.object(pl, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pl, "_pl_search_parcel") as mock_search:
            result = _run(pl.recv_pl_parcel(update, ctx))
        self.assertEqual(result, pl.ConversationHandler.END)
        mock_search.assert_not_called()

    def test_multiple_parcels_searches_each_and_stashes_batch(self):
        update = _make_update_with_message("P1\nP2")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pl, "allowed", return_value=True), \
             patch.object(pl, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pl, "_pl_search_parcel", side_effect=[list(SAMPLE_MATCHES), []]) as mock_search:
            result = _run(pl.recv_pl_parcel(update, ctx))
        self.assertEqual(result, pl.PL.DELIVERY)
        self.assertEqual(mock_search.call_count, 2)
        sess = pl._get_pl_sess(ctx)
        self.assertEqual(sess.batch, [("P1", SAMPLE_MATCHES), ("P2", [])])
        self.assertEqual(sess.parcel, "")
        self.assertEqual(sess.matches, [])

    def test_comma_separated_parcels_also_trigger_list_mode(self):
        update = _make_update_with_message("P1, P2")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pl, "allowed", return_value=True), \
             patch.object(pl, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pl, "_pl_search_parcel", return_value=[]):
            result = _run(pl.recv_pl_parcel(update, ctx))
        self.assertEqual(result, pl.PL.DELIVERY)
        self.assertEqual(pl._get_pl_sess(ctx).batch, [("P1", []), ("P2", [])])

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


class TestPlReportLinesBatch(unittest.TestCase):
    def test_not_found_parcel_gets_its_own_line(self):
        lines = pl._pl_report_lines_batch([("P1", []), ("P2", list(SAMPLE_MATCHES))])
        joined = "\n\n".join(lines)
        self.assertIn("P1", joined)
        self.assertIn("not found", joined)
        self.assertIn("R1", joined)

    def test_header_totals_parcels_and_matches(self):
        lines = pl._pl_report_lines_batch([("P1", list(SAMPLE_MATCHES)), ("P2", [])])
        self.assertIn("2 parcel(s)", lines[0])
        self.assertIn("1 application(s)", lines[0])


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

    def test_batch_mode_sends_compiled_report(self):
        update = _make_update_with_callback("pl_delivery:telegram")
        ctx = _make_ctx_with_session(matches=[])
        pl._get_pl_sess(ctx).batch = [("P1", []), ("P2", list(SAMPLE_MATCHES))]
        with patch.object(pl, "allowed", return_value=True):
            result = _run(pl.recv_pl_delivery(update, ctx))
        self.assertEqual(result, pl.ConversationHandler.END)
        sent_text = ctx.bot.send_message.call_args_list[-1].args[1]
        self.assertIn("P1", sent_text)
        self.assertIn("R1", sent_text)


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

    def test_batch_mode_emails_compiled_report_with_parcel_count_subject(self):
        update = _make_update_with_message("someone@example.com")
        ctx = _make_ctx_with_session(matches=[])
        pl._get_pl_sess(ctx).batch = [("P1", []), ("P2", list(SAMPLE_MATCHES))]
        with patch.object(pl, "allowed", return_value=True), \
             patch.object(pl, "_send_auto_fetch_email") as mock_send:
            result = _run(pl.recv_pl_email(update, ctx))
        self.assertEqual(result, pl.ConversationHandler.END)
        args = mock_send.call_args.args
        self.assertIn("2 parcels", args[1])   # subject
        self.assertIn("P1", args[2])
        self.assertIn("R1", args[2])

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
