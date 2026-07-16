#!/usr/bin/env python3
"""
Unit tests for bulk_export.py — _be_fetch_detail's token-rotation/retry
behavior, _be_extract_row's field extraction, the schedule/partial
persistence helpers, and the cmd_bulk_export/recv_be_report_type/
recv_be_county/recv_be_email/cmd_export_status handlers.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bulk_export as be
from ardhisasa_auth import AuthTokens
from token_rotator import _AllTokensExhausted, _TokenRotator

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


class TestBeFetchDetail(unittest.TestCase):
    def test_success_returns_json(self):
        rotator = _TokenRotator([("staff", TOKENS)])
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            status_code=200, raise_for_status=lambda: None, json=lambda: {"node": "X"}
        )
        result = be._be_fetch_detail(fake_session, rotator, "app-1")
        self.assertEqual(result, {"node": "X"})

    def test_404_returns_empty_dict(self):
        rotator = _TokenRotator([("staff", TOKENS)])
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(status_code=404)
        result = be._be_fetch_detail(fake_session, rotator, "app-1")
        self.assertEqual(result, {})

    def test_all_tokens_exhausted_raises(self):
        rotator = _TokenRotator([])
        fake_session = MagicMock()
        with self.assertRaises(_AllTokensExhausted):
            be._be_fetch_detail(fake_session, rotator, "app-1")

    def test_403_rotates_then_succeeds(self):
        rotator = _TokenRotator([("staff", TOKENS), ("staff2", TOKENS)])
        fake_session = MagicMock()
        fake_session.get.side_effect = [
            MagicMock(status_code=403),
            MagicMock(status_code=200, raise_for_status=lambda: None, json=lambda: {"node": "Y"}),
        ]
        with patch.object(be.time, "sleep"):
            result = be._be_fetch_detail(fake_session, rotator, "app-1")
        self.assertEqual(result, {"node": "Y"})

    def test_exhausts_all_retries_on_persistent_5xx(self):
        rotator = _TokenRotator([("staff", TOKENS)])
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(status_code=503)
        with patch.object(be.time, "sleep"):
            with self.assertRaises(RuntimeError):
                be._be_fetch_detail(fake_session, rotator, "app-1")


class TestBeFetchOfficeReport(unittest.TestCase):
    def test_no_tokens_returns_empty_dict(self):
        rotator = _TokenRotator([])
        result = be._be_fetch_office_report(MagicMock(), rotator, "app-1")
        self.assertEqual(result, {})

    def test_exception_returns_empty_dict(self):
        rotator = _TokenRotator([("staff", TOKENS)])
        fake_session = MagicMock()
        fake_session.get.side_effect = RuntimeError("boom")
        result = be._be_fetch_office_report(fake_session, rotator, "app-1")
        self.assertEqual(result, {})


class TestBeExtractRow(unittest.TestCase):
    def test_extracts_valuation_officer_exact_match_preferred(self):
        detail = {
            "actors": [
                {"role": "VO", "user_details": {"names": "OTHER"}, "date_assigned": "2026-01-01"},
                {"role": "VALUATION OFFICER", "user_details": {"names": "JANE"}, "date_assigned": "2026-01-02"},
            ],
            "reference_number": "R1",
        }
        row = be._be_extract_row(detail)
        self.assertEqual(row["Valuation Officer"], "JANE")
        self.assertEqual(row["Date of Valuation"], "2026-01-02")

    def test_prefers_valuation_certificate_document(self):
        detail = {
            "process_documents": [
                {"document_name": "OTHER", "document": "url-a"},
                {"document_name": "VALUATION CERTIFICATE", "document": "url-b"},
            ],
        }
        row = be._be_extract_row(detail)
        self.assertEqual(row["Document URL"], "url-b")

    def test_falls_back_to_application_documents(self):
        detail = {"application_documents": [{"document": "fallback-url"}]}
        row = be._be_extract_row(detail)
        self.assertEqual(row["Document URL"], "fallback-url")


class TestBeLandValue(unittest.TestCase):
    """_be_land_value — sort key used to order the export highest-to-lowest."""

    def test_parses_numeric_string(self):
        self.assertEqual(be._be_land_value({"Valuer Total Land Value (KES)": "6000000"}), 6000000.0)

    def test_strips_commas(self):
        self.assertEqual(be._be_land_value({"Valuer Total Land Value (KES)": "6,000,000"}), 6000000.0)

    def test_missing_sorts_last(self):
        self.assertEqual(be._be_land_value({}), -1.0)

    def test_unparseable_sorts_last(self):
        self.assertEqual(be._be_land_value({"Valuer Total Land Value (KES)": "N/A"}), -1.0)

    def test_rows_sort_highest_first(self):
        rows = [
            {"Valuer Total Land Value (KES)": "1000000"},
            {"Valuer Total Land Value (KES)": "9000000"},
            {},
            {"Valuer Total Land Value (KES)": "5000000"},
        ]
        rows.sort(key=be._be_land_value, reverse=True)
        self.assertEqual(
            [r.get("Valuer Total Land Value (KES)") for r in rows],
            ["9000000", "5000000", "1000000", None],
        )


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sched_file = os.path.join(self.tmpdir.name, "saved_bulk_export_schedule.json")
        self.partial_file = os.path.join(self.tmpdir.name, "saved_bulk_export_partial.json")
        self._patches = [
            patch.object(be, "SAVED_BULK_EXPORT_SCHED_FILE", self.sched_file),
            patch.object(be, "SAVED_BULK_EXPORT_PARTIAL_FILE", self.partial_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def test_load_missing_schedule_returns_none(self):
        self.assertIsNone(be.load_be_schedule())

    def test_save_then_load_schedule_roundtrip(self):
        be.save_be_schedule({"chat_id": 1, "interval_seconds": 86400})
        cfg = be.load_be_schedule()
        self.assertEqual(cfg["chat_id"], 1)

    def test_clear_schedule_removes_file(self):
        be.save_be_schedule({"chat_id": 1})
        be.clear_be_schedule()
        self.assertIsNone(be.load_be_schedule())

    def test_save_then_load_partial_roundtrip(self):
        be.save_be_partial("NAIROBI", ["NAIROBI"], [{"a": 1}], ["id1"])
        partial = be.load_be_partial()
        self.assertEqual(partial["county"], "NAIROBI")
        self.assertEqual(partial["done_ids"], ["id1"])

    def test_clear_partial_removes_file(self):
        be.save_be_partial("NAIROBI", [], [], [])
        be.clear_be_partial()
        self.assertIsNone(be.load_be_partial())


class TestCmdBulkExport(unittest.TestCase):
    def test_resets_session_and_asks_report_type(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(be, "allowed", return_value=True):
            result = _run(be.cmd_bulk_export(update, ctx))
        self.assertEqual(result, be.BE.REPORT_TYPE)
        self.assertIsInstance(ctx.user_data["be_session"], be.BESession)


class TestRecvBeReportType(unittest.TestCase):
    def test_ardhisasa_uses_ar_county_keyboard(self):
        update = _make_update_with_callback("be_rtype:ardhisasa")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(be, "allowed", return_value=True):
            result = _run(be.recv_be_report_type(update, ctx))
        self.assertEqual(result, be.BE.COUNTY)
        self.assertEqual(ctx.user_data["be_session"].report_type, "ardhisasa")


class TestRecvBeCounty(unittest.TestCase):
    def test_ardhisasa_uses_restricted_registries(self):
        update = _make_update_with_callback("be_county:NAIROBI")
        ctx = MagicMock()
        ctx.user_data = {"be_session": be.BESession(report_type="ardhisasa")}
        with patch.object(be, "allowed", return_value=True):
            result = _run(be.recv_be_county(update, ctx))
        self.assertEqual(result, be.BE.EMAIL)
        self.assertEqual(ctx.user_data["be_session"].registries, ["NAIROBI"])

    def test_ardhipay_uses_full_registries(self):
        update = _make_update_with_callback("be_county:NAIROBI")
        ctx = MagicMock()
        ctx.user_data = {"be_session": be.BESession(report_type="ardhipay")}
        with patch.object(be, "allowed", return_value=True):
            _run(be.recv_be_county(update, ctx))
        self.assertEqual(ctx.user_data["be_session"].registries, ["CENTRAL", "NAIROBI"])


class TestRecvBeEmail(unittest.TestCase):
    def test_skip_clears_email(self):
        update = _make_update_with_message("skip")
        ctx = MagicMock()
        ctx.user_data = {"be_session": be.BESession()}
        with patch.object(be, "allowed", return_value=True):
            result = _run(be.recv_be_email(update, ctx))
        self.assertEqual(result, be.BE.SCHEDULE)
        self.assertEqual(ctx.user_data["be_session"].email, "")

    def test_invalid_email_reprompts(self):
        update = _make_update_with_message("not-an-email")
        ctx = MagicMock()
        ctx.user_data = {"be_session": be.BESession()}
        with patch.object(be, "allowed", return_value=True):
            result = _run(be.recv_be_email(update, ctx))
        self.assertEqual(result, be.BE.EMAIL)

    def test_valid_email_stored(self):
        update = _make_update_with_message("jane@example.com")
        ctx = MagicMock()
        ctx.user_data = {"be_session": be.BESession()}
        with patch.object(be, "allowed", return_value=True):
            result = _run(be.recv_be_email(update, ctx))
        self.assertEqual(result, be.BE.SCHEDULE)
        self.assertEqual(ctx.user_data["be_session"].email, "jane@example.com")


class TestCmdExportStatus(unittest.TestCase):
    def test_no_status_shows_info_message(self):
        update = _make_update_with_message()
        update.effective_chat.id = 999
        ctx = MagicMock()
        with patch.object(be, "allowed", return_value=True), \
             patch.object(be, "_BE_STATUS", {}), \
             patch.object(be, "_JD_STATUS", {}):
            _run(be.cmd_export_status(update, ctx))
        update.message.reply_text.assert_called_once()
        self.assertIn("No export", update.message.reply_text.call_args[0][0])

    def test_be_status_only_renders(self):
        update = _make_update_with_message()
        update.effective_chat.id = 999
        ctx = MagicMock()
        be_status = {999: {"phase": "done", "started_at": None, "rows": 10}}
        with patch.object(be, "allowed", return_value=True), \
             patch.object(be, "_BE_STATUS", be_status), \
             patch.object(be, "_JD_STATUS", {}):
            _run(be.cmd_export_status(update, ctx))
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Export Valuation Report", sent_text)


if __name__ == "__main__":
    unittest.main()
