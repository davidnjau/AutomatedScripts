#!/usr/bin/env python3
"""
Unit tests for lookup_reference.py — _lu_search_ref's filter/role combo
fallback (and _lu_search_ref_county's TO_VALUATION/Ongoing fallback for
County refs), _lu_format_result/_lu_format_county_result's field
extraction, and the cmd_lookup/recv_lu_ref conversation handlers,
including recv_lu_ref's County-vs-default credential/endpoint routing.

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


class TestLuExtractContext(unittest.TestCase):
    """_lu_extract_context — the raw-value counterpart to _lu_format_result,
    for callers (new_assignment.py) that persist rather than display it."""

    def test_no_detail_falls_back_to_item_fields(self):
        item = {"registry": "NAIROBI", "county": "NAIROBI", "parcel_number": "P1"}
        ctx = lu._lu_extract_context(item, None)
        self.assertEqual(ctx["registry"], "NAIROBI")
        self.assertEqual(ctx["county"], "NAIROBI")
        self.assertEqual(ctx["parcel"], "P1")
        self.assertEqual(ctx["consideration"], "")
        self.assertEqual(ctx["currency_code"], "KES")

    def test_detail_fields_take_priority_over_item(self):
        item = {"registry": "MOMBASA", "county": "MOMBASA"}
        detail = {
            "registry": "NAIROBI", "county": "NAIROBI",
            "external_process_details": {
                "consideration_amount": "500000", "currency_code": "KES", "parcel_number": "NEW",
            },
        }
        ctx = lu._lu_extract_context(item, detail)
        self.assertEqual(ctx["registry"], "NAIROBI")
        self.assertEqual(ctx["county"], "NAIROBI")
        self.assertEqual(ctx["parcel"], "NEW")
        self.assertEqual(ctx["consideration"], "500000")
        self.assertEqual(ctx["currency_code"], "KES")


class TestLuIsCountyRef(unittest.TestCase):
    def test_cntyinv_prefix_is_county(self):
        self.assertTrue(lu._lu_is_county_ref("CNTYINV/AB12CD34EF"))

    def test_lowercase_prefix_is_county(self):
        self.assertTrue(lu._lu_is_county_ref("cntyinv/ab12cd34ef"))

    def test_other_prefix_is_not_county(self):
        self.assertFalse(lu._lu_is_county_ref("REG/TSFR/ABC123"))


class TestLuSearchRefCounty(unittest.TestCase):
    def test_returns_none_when_no_filter_matches(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None, json=lambda: {"results": []}
        )
        with patch.object(lu, "build_session", return_value=fake_session):
            result = lu._lu_search_ref_county(TOKENS, "CNTYINV/AB12CD34EF")
        self.assertIsNone(result)
        self.assertEqual(fake_session.get.call_count, len(lu._LU_COUNTY_FILTERS))

    def test_to_valuation_tried_before_ongoing(self):
        fake_session = MagicMock()
        fake_session.get.side_effect = [
            MagicMock(raise_for_status=lambda: None,
                      json=lambda: {"results": [{"reference_number": "CNTYINV/AB12CD34EF", "id": "app-1"}]}),
        ]
        with patch.object(lu, "build_session", return_value=fake_session):
            result = lu._lu_search_ref_county(TOKENS, "CNTYINV/AB12CD34EF")
        self.assertEqual(result["id"], "app-1")
        self.assertEqual(result["_matched_filter"], "TO_VALUATION")
        params = fake_session.get.call_args[1]["params"]
        self.assertEqual(params["filter"], "TO_VALUATION")
        self.assertEqual(params["from_ardhipay"], "true")

    def test_falls_back_to_ongoing(self):
        fake_session = MagicMock()
        fake_session.get.side_effect = [
            MagicMock(raise_for_status=lambda: None, json=lambda: {"results": []}),
            MagicMock(raise_for_status=lambda: None,
                      json=lambda: {"results": [{"reference_number": "CNTYINV/AB12CD34EF", "id": "app-2"}]}),
        ]
        with patch.object(lu, "build_session", return_value=fake_session):
            result = lu._lu_search_ref_county(TOKENS, "CNTYINV/AB12CD34EF")
        self.assertEqual(result["id"], "app-2")
        self.assertEqual(result["_matched_filter"], "Ongoing")


class TestLuFormatCountyResult(unittest.TestCase):
    def test_formats_with_no_detail(self):
        item = {"application_status": "at valuation", "registry": "CENTRAL",
                 "county": "NAIROBI", "date_created": "2026-07-15"}
        result = lu._lu_format_county_result("CNTYINV/AB12CD34EF", item, None)
        self.assertIn("CNTYINV/AB12CD34EF", result)
        self.assertIn("AT VALUATION", result)
        self.assertIn("—", result)   # valuer/consideration default when no detail

    def test_formats_with_detail_extracts_officer_and_consideration(self):
        """Regression: the stampdutyservice detail-view shape uses "officers"
        (flat names+role) rather than "actors"/"user_details" — matches the
        real hod-or-clr/detail-view response, not the DLV one."""
        item = {"application_status": "ongoing", "date_created": "2026-07-15",
                 "parcel_number": "APARTMENT NO. 403 BLOCK 'G'"}
        detail = {
            "node": "STAMP_DUTY_VALUATION",
            "external_process_details": {"consideration_amount": "4900000.000", "currency_code": "KES"},
            "officers": [{"role": "COUNTY_REGISTRAR", "names": "LINDA KOCHWA ANDAI"}],
            "registry": "CENTRAL", "county": "NAIROBI",
        }
        result = lu._lu_format_county_result("CNTYINV/AB12CD34EF", item, detail)
        self.assertIn("KES 4,900,000.00", result)
        self.assertIn("APARTMENT NO. 403", result)
        self.assertIn("—", result)   # no VALUATION OFFICER role present yet -> unassigned

    def test_formats_with_valuation_officer_present(self):
        item = {"application_status": "ongoing", "date_created": "2026-07-15"}
        detail = {
            "node": "STAMP_DUTY_VALUATION",
            "officers": [
                {"role": "COUNTY_REGISTRAR", "names": "LINDA KOCHWA ANDAI"},
                {"role": "VALUATION OFFICER", "names": "JANE DOE"},
            ],
        }
        result = lu._lu_format_county_result("CNTYINV/AB12CD34EF", item, detail)
        self.assertIn("JANE DOE", result)


class TestLuSearchRefCountyDlv(unittest.TestCase):
    """_lu_search_ref_county_dlv — the DLV/valuation-stage fallback for a
    County ref, forcing request_type=COUNTY_STAMP_DUTY/from_ardhipay=true
    against the same endpoint _lu_search_ref uses for non-county refs."""

    def test_returns_none_when_no_filter_matches(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None, json=lambda: {"results": []}
        )
        with patch.object(lu, "build_session", return_value=fake_session):
            result = lu._lu_search_ref_county_dlv(TOKENS, "CNTYINV/AB12CD34EF")
        self.assertIsNone(result)
        self.assertEqual(fake_session.get.call_count, len(lu._LU_COUNTY_DLV_FILTERS))

    def test_forces_county_request_type_and_from_ardhipay(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [{"reference_number": "CNTYINV/AB12CD34EF", "id": "app-1"}]},
        )
        with patch.object(lu, "build_session", return_value=fake_session):
            result = lu._lu_search_ref_county_dlv(TOKENS, "CNTYINV/AB12CD34EF")
        self.assertEqual(result["id"], "app-1")
        params = fake_session.get.call_args[1]["params"]
        self.assertEqual(params["request_type"], "COUNTY_STAMP_DUTY")
        self.assertEqual(params["from_ardhipay"], "true")
        self.assertEqual(params["role"], "DLV")

    def test_exception_on_one_filter_continues_to_next(self):
        fake_session = MagicMock()
        fake_session.get.side_effect = [
            RuntimeError("network blip"),
            MagicMock(raise_for_status=lambda: None,
                      json=lambda: {"results": [{"reference_number": "CNTYINV/AB12CD34EF", "id": "app-2"}]}),
        ]
        with patch.object(lu, "build_session", return_value=fake_session):
            result = lu._lu_search_ref_county_dlv(TOKENS, "CNTYINV/AB12CD34EF")
        self.assertEqual(result["id"], "app-2")


class TestCmdLookup(unittest.TestCase):
    def test_asks_for_reference_number_directly(self):
        """No credential-picker step — cmd_lookup goes straight to asking
        for a reference number; recv_lu_ref decides the credential from
        the ref's own format once entered."""
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(lu, "allowed", return_value=True):
            result = _run(lu.cmd_lookup(update, ctx))
        self.assertEqual(result, lu.LU.REF_INPUT)


class TestRecvLuRef(unittest.TestCase):
    def test_blank_ref_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        with patch.object(lu, "allowed", return_value=True):
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.LU.REF_INPUT)

    def test_expired_tokens_end_conversation(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=None):
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)

    def test_non_county_ref_uses_staff_valuer_and_default_search(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=TOKENS) as mock_tokens, \
             patch.object(lu, "_lu_search_ref", return_value=None) as mock_search, \
             patch.object(lu, "_lu_search_ref_county") as mock_search_county:
            _run(lu.recv_lu_ref(update, ctx))
        mock_tokens.assert_called_once_with(lu._LU_CRED_DEFAULT)
        mock_search.assert_called_once()
        mock_search_county.assert_not_called()

    def test_county_ref_tries_assessor_stage_first_with_support_reg(self):
        update = _make_update_with_message("CNTYINV/AB12CD34EF")
        ctx = MagicMock()
        item = {"id": "app-1", "reference_number": "CNTYINV/AB12CD34EF"}
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(lu, "_lu_search_ref_county", return_value=item) as mock_search_county, \
             patch.object(lu, "_lu_fetch_detail_county", return_value=None), \
             patch.object(lu, "_lu_search_ref_county_dlv") as mock_search_dlv, \
             patch.object(lu, "_lu_search_ref") as mock_search:
            _run(lu.recv_lu_ref(update, ctx))
        mock_search_county.assert_called_once_with(TOKENS, "CNTYINV/AB12CD34EF")
        # assessor stage found it -> DLV fallback and the non-county path never run
        mock_search_dlv.assert_not_called()
        mock_search.assert_not_called()

    def test_county_ref_falls_back_to_dlv_stage_when_assessor_stage_finds_nothing(self):
        update = _make_update_with_message("CNTYINV/AB12CD34EF")
        ctx = MagicMock()
        item = {"id": "app-2", "reference_number": "CNTYINV/AB12CD34EF"}
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(lu, "_lu_search_ref_county", return_value=None) as mock_search_county, \
             patch.object(lu, "_lu_search_ref_county_dlv", return_value=item) as mock_search_dlv, \
             patch.object(lu, "_lu_fetch_detail", return_value=None), \
             patch.object(lu, "_lu_format_result", return_value="formatted") as mock_fmt:
            result = _run(lu.recv_lu_ref(update, ctx))
        mock_search_county.assert_called_once_with(TOKENS, "CNTYINV/AB12CD34EF")
        mock_search_dlv.assert_called_once_with(TOKENS, "CNTYINV/AB12CD34EF")
        mock_fmt.assert_called_once()
        self.assertEqual(result, lu.ConversationHandler.END)

    def test_county_ref_not_found_in_either_stage_ends_conversation(self):
        update = _make_update_with_message("CNTYINV/AB12CD34EF")
        ctx = MagicMock()
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(lu, "_lu_search_ref_county", return_value=None), \
             patch.object(lu, "_lu_search_ref_county_dlv", return_value=None):
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)

    def test_county_ref_no_tokens_for_either_stage_ends_conversation(self):
        update = _make_update_with_message("CNTYINV/AB12CD34EF")
        ctx = MagicMock()
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=None), \
             patch.object(lu, "_lu_search_ref_county") as mock_search_county:
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)
        mock_search_county.assert_not_called()

    def test_ref_not_found_ends_conversation(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(lu, "_lu_search_ref", return_value=None):
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)

    def test_ref_found_formats_and_ends_conversation(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        item = {"id": "app-1", "reference_number": "R1"}
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(lu, "_lu_search_ref", return_value=item), \
             patch.object(lu, "_lu_fetch_detail", return_value=None), \
             patch.object(lu, "_lu_format_result", return_value="formatted") as mock_fmt:
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)
        mock_fmt.assert_called_once()

    def test_county_ref_found_uses_county_detail_and_formatter(self):
        update = _make_update_with_message("CNTYINV/AB12CD34EF")
        ctx = MagicMock()
        item = {"id": "app-1", "reference_number": "CNTYINV/AB12CD34EF"}
        with patch.object(lu, "allowed", return_value=True), \
             patch.object(lu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(lu, "_lu_search_ref_county", return_value=item), \
             patch.object(lu, "_lu_fetch_detail_county", return_value=None) as mock_detail, \
             patch.object(lu, "_lu_format_county_result", return_value="formatted") as mock_fmt, \
             patch.object(lu, "_lu_format_result") as mock_fmt_default:
            result = _run(lu.recv_lu_ref(update, ctx))
        self.assertEqual(result, lu.ConversationHandler.END)
        mock_detail.assert_called_once()
        mock_fmt.assert_called_once()
        mock_fmt_default.assert_not_called()


if __name__ == "__main__":
    unittest.main()
