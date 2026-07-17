#!/usr/bin/env python3
"""
Unit tests for dlv_tasks.py — most importantly the per-ref enrichment
priority order in _dt_fetch_tasks: assessor stage -> DLV -> the batch
item's own "assessor" field -> the Fetch Tasks cache. Getting this order
wrong is exactly the bug fixed three times in one session (assessor
showing blank in the report despite being available somewhere), so this
is a regression test for that class of bug.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlv_tasks
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


def _run(coro):
    return asyncio.run(coro)


def _make_query_update(data):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return update


def _batch_item(ref="REG/TSFR/ABC123", **overrides):
    item = {"ref": ref, "valuer_name": "Jane Doe", "valuer_uid": "uid-1", "queued_at": "2026-07-10T10:00:00"}
    item.update(overrides)
    return item


class TestDtFetchTasksPriorityOrder(unittest.TestCase):
    """Verify _dt_fetch_tasks._enrich (exercised via _dt_fetch_tasks) picks the
    assessor from the right source, in the right order, for every combination."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.batch_file = os.path.join(self.tmpdir.name, "batch.json")
        self._patch_load = patch.object(dlv_tasks, "load_dlv_batch")
        self.mock_load = self._patch_load.start()
        self._patch_save = patch.object(dlv_tasks, "save_dlv_batch")
        self.mock_save = self._patch_save.start()

    def tearDown(self):
        self._patch_load.stop()
        self._patch_save.stop()
        self.tmpdir.cleanup()

    def _run(self, item, **patches):
        self.mock_load.return_value = [item]
        defaults = {
            "_search_ref_stampduty": None,
            "_search_ref_dlv": None,
            "_fetch_tasks_log_lookup": None,
        }
        defaults.update(patches)
        with patch.object(dlv_tasks, "_search_ref_stampduty", return_value=defaults["_search_ref_stampduty"]), \
             patch.object(dlv_tasks, "_search_ref_dlv", return_value=defaults["_search_ref_dlv"]), \
             patch.object(dlv_tasks, "_fetch_tasks_log_lookup", return_value=defaults["_fetch_tasks_log_lookup"]):
            rows = dlv_tasks._dt_fetch_tasks(TOKENS)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_assessor_stage_found_takes_priority(self):
        """If the ref is still upstream (assessor stage), use its assessor —
        even if the batch item also has one saved (assessor stage is live/fresher)."""
        assessor_task = {"id": "1", "parcel_number": "P1", "registry": "NAIROBI", "county": "NAIROBI",
                          "date_created": "2026-07-10"}
        with patch.object(dlv_tasks, "_fetch_stampduty_detail",
                           return_value={"application_status": "ONGOING",
                                         "officers": [{"names": "LIVE ASSESSOR", "role": "ASSESSOR_OF_STAMP_DUTY"}]}):
            row = self._run(
                _batch_item(assessor="STALE CACHED NAME"),
                _search_ref_stampduty=assessor_task,
            )
        self.assertEqual(row["assessor"], "LIVE ASSESSOR")
        self.assertEqual(row["location"], "assessor")
        self.assertTrue(row["found"])
        self.assertEqual(row["status"], "ONGOING")

    def test_assessor_stage_falls_back_to_officer_role_when_no_strict_match(self):
        """Regression test: a County ref's officer holds COUNTY_REGISTRAR,
        not ASSESSOR_OF_STAMP_DUTY — strict extraction used to leave this
        blank in the live Open Tasks report too."""
        assessor_task = {"id": "1", "parcel_number": "P1", "registry": "NAIROBI", "county": "NAIROBI",
                          "date_created": "2026-07-10"}
        with patch.object(dlv_tasks, "_fetch_stampduty_detail",
                           return_value={"application_status": "ONGOING",
                                         "officers": [{"names": "REDEMPTA AKOTH OKWANY",
                                                       "role": "COUNTY_REGISTRAR"}]}):
            row = self._run(_batch_item(), _search_ref_stampduty=assessor_task)
        self.assertEqual(row["assessor"], "REDEMPTA AKOTH OKWANY (COUNTY_REGISTRAR)")

    def test_dlv_found_with_assessor_takes_priority_over_item_field(self):
        dlv_task = {"id": "1", "parcel_number": "P1", "registry": "NAIROBI", "county": "NAIROBI",
                    "date_created": "2026-07-10", "_request_type": "STAMP_DUTY"}
        with patch.object(dlv_tasks, "_fetch_ref_detail_dlv", return_value={"node": "X"}), \
             patch.object(dlv_tasks, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "assessor_name": "DLV ASSESSOR",
                 "consideration_amount": "6000000", "currency_code": "KES", "actor_name": "",
             }):
            row = self._run(
                _batch_item(assessor="STALE CACHED NAME"),
                _search_ref_dlv=dlv_task,
            )
        self.assertEqual(row["assessor"], "DLV ASSESSOR")
        self.assertEqual(row["location"], "dlv")
        self.assertEqual(row["status"], "ONGOING")
        self.assertEqual(row["node"], "VALUATION_STAMP_DUTY_VALUER_REPORT")
        self.assertEqual(row["consideration"], "KES 6,000,000.00")

    def test_neither_live_search_found_falls_back_to_batch_item_field(self):
        """This is the exact bug: both live searches miss the ref, but the
        assessor was already saved on the batch item at DLV-Batch add-time."""
        row = self._run(_batch_item(assessor="SAVED ON ITEM"))
        self.assertEqual(row["assessor"], "SAVED ON ITEM")
        self.assertFalse(row["found"])

    def test_no_item_field_falls_back_to_fetch_tasks_cache(self):
        row = self._run(
            _batch_item(),  # no "assessor" key at all
            _fetch_tasks_log_lookup={"assessor": "FROM CACHE", "parcel": "P9", "registry": "MOMBASA",
                                      "county": "MOMBASA", "date_created": "2026-07-09"},
        )
        self.assertEqual(row["assessor"], "FROM CACHE")
        self.assertEqual(row["parcel"], "P9")

    def test_nothing_anywhere_leaves_assessor_blank(self):
        row = self._run(_batch_item())
        self.assertEqual(row["assessor"], "")
        self.assertFalse(row["found"])

    def test_dlv_found_but_no_assessor_falls_back_to_item_field(self):
        """DLV finds the ref but its detail view has no assessor_name (e.g.
        already past that stage) — still shouldn't blank out a known value."""
        dlv_task = {"id": "1", "parcel_number": "", "registry": "", "county": "", "date_created": "",
                    "_request_type": "STAMP_DUTY"}
        with patch.object(dlv_tasks, "_fetch_ref_detail_dlv", return_value={"node": "X"}), \
             patch.object(dlv_tasks, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "X", "assessor_name": "", "consideration_amount": "",
                 "currency_code": "", "actor_name": "",
             }):
            row = self._run(
                _batch_item(assessor="FALLBACK ITEM VALUE"),
                _search_ref_dlv=dlv_task,
            )
        self.assertEqual(row["assessor"], "FALLBACK ITEM VALUE")

    def test_closed_item_is_moved_and_excluded_from_rows(self):
        dlv_task = {"id": "1", "parcel_number": "", "registry": "", "county": "", "date_created": "",
                    "_request_type": "STAMP_DUTY"}
        with patch.object(dlv_tasks, "_fetch_ref_detail_dlv", return_value={"node": "COMPLETED"}), \
             patch.object(dlv_tasks, "_classify_dlv_detail", return_value={
                 "bucket": "closed", "closed_reason": "completed", "application_status": "COMPLETED",
                 "node": "VALUATION_STAMP_DUTY_COMPLETED", "assessor_name": "", "consideration_amount": "",
                 "currency_code": "", "actor_name": "",
             }), \
             patch.object(dlv_tasks, "_append_dlv_closed") as mock_append:
            self.mock_load.return_value = [_batch_item()]
            with patch.object(dlv_tasks, "_search_ref_stampduty", return_value=None), \
                 patch.object(dlv_tasks, "_search_ref_dlv", return_value=dlv_task):
                rows = dlv_tasks._dt_fetch_tasks(TOKENS)
        self.assertEqual(rows, [])
        mock_append.assert_called_once()
        self.mock_save.assert_called_once()

    def test_empty_batch_returns_empty_without_any_search(self):
        self.mock_load.return_value = []
        with patch.object(dlv_tasks, "_search_ref_stampduty") as mock_search:
            rows = dlv_tasks._dt_fetch_tasks(TOKENS)
        self.assertEqual(rows, [])
        mock_search.assert_not_called()


class TestDtRowConsiderationValue(unittest.TestCase):
    """_dt_row_consideration_value — parses the already-formatted
    "KES 1,234.00" string back to a number for Excel-export sorting."""

    def test_parses_formatted_string(self):
        self.assertEqual(dlv_tasks._dt_row_consideration_value({"consideration": "KES 6,000,000.00"}), 6000000.0)

    def test_missing_sorts_last(self):
        self.assertEqual(dlv_tasks._dt_row_consideration_value({}), -1.0)

    def test_unparseable_sorts_last(self):
        self.assertEqual(dlv_tasks._dt_row_consideration_value({"consideration": "N/A"}), -1.0)


class TestDtBuildExcelSortOrder(unittest.TestCase):
    """_dt_build_excel — only ever used for email delivery, so sorting rows
    here highest-consideration-first affects just the emailed file."""

    def _refs_in_sheet_order(self, xlsx_bytes):
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
        ws = wb.active
        return [row[0] for row in ws.iter_rows(min_row=2, values_only=True)]

    def test_rows_ordered_highest_consideration_first(self):
        rows = [
            {"ref": "LOW", "consideration": "KES 1,000,000.00"},
            {"ref": "HIGH", "consideration": "KES 9,000,000.00"},
            {"ref": "MID", "consideration": "KES 5,000,000.00"},
        ]
        xlsx_bytes = dlv_tasks._dt_build_excel(rows)
        self.assertEqual(self._refs_in_sheet_order(xlsx_bytes), ["HIGH", "MID", "LOW"])

    def test_missing_consideration_sorts_after_real_amounts(self):
        rows = [
            {"ref": "NO_AMOUNT"},
            {"ref": "HAS_AMOUNT", "consideration": "KES 1,000,000.00"},
        ]
        xlsx_bytes = dlv_tasks._dt_build_excel(rows)
        self.assertEqual(self._refs_in_sheet_order(xlsx_bytes), ["HAS_AMOUNT", "NO_AMOUNT"])


class TestDtFormatTaskBlock(unittest.TestCase):
    """_dt_format_task_block renders one task's full Lookup-Reference-style detail block."""

    def _task(self, **overrides):
        t = {
            "ref": "REG/TSFR/SHIZU9NCC8", "status": "ONGOING",
            "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "valuer_name": "BYRON MARCEL ONDITI",
            "assessor": "REDEMPTA AKOTH OKWANY",
            "registry": "NAIROBI", "county": "nairobi", "consideration": "KES 6,000,000.00",
            "parcel": "NAIROBI/BLOCK209/309", "date_created": "2026-07-08T15:36:21.616029",
            "found": True, "location": "dlv",
        }
        t.update(overrides)
        return t

    def test_all_fields_present(self):
        block = dlv_tasks._dt_format_task_block(1, self._task())
        self.assertIn("📌 *Ref:* `REG/TSFR/SHIZU9NCC8`", block)
        self.assertIn("📊 Status: ONGOING", block)
        self.assertIn("🔄 Node: ✍️ Assigned — valuer report pending", block)
        self.assertIn("👤 Valuer: BYRON MARCEL ONDITI", block)
        self.assertIn("Assessor: REDEMPTA AKOTH OKWANY", block)
        self.assertIn("🏢 Registry: NAIROBI", block)
        self.assertIn("📍 County: nairobi", block)
        self.assertIn("💰 Consideration: KES 6,000,000.00", block)
        self.assertIn("📋 Parcel: NAIROBI/BLOCK209/309", block)
        self.assertIn("📅 Added: 2026-07-08T15:36:21.616029", block)
        self.assertNotIn("not found", block)

    def test_missing_fields_fall_back_to_em_dash(self):
        block = dlv_tasks._dt_format_task_block(1, self._task(
            status="", node="", assessor="", registry="", county="", consideration="", parcel="", date_created="",
        ))
        self.assertIn("📊 Status: —", block)
        self.assertIn("🔄 Node: —", block)
        self.assertIn("Assessor: —", block)
        self.assertIn("🏢 Registry: —", block)
        self.assertIn("📍 County: —", block)
        self.assertIn("💰 Consideration: —", block)
        self.assertIn("📋 Parcel: —", block)
        self.assertIn("📅 Added: —", block)

    def test_not_found_appends_note(self):
        block = dlv_tasks._dt_format_task_block(1, self._task(found=False, location=""))
        self.assertIn("❓ _not found in Assessor or DLV queues_", block)

    def test_still_with_assessor_appends_note(self):
        block = dlv_tasks._dt_format_task_block(1, self._task(location="assessor"))
        self.assertIn("⏳ _still with Assessor, not yet in DLV_", block)


class TestDtConsiderationValue(unittest.TestCase):
    """_dt_consideration_value — numeric consideration, preferring the
    closed-record field over the queue-time-cached one."""

    def test_prefers_consideration_amount_when_both_present(self):
        item = {"consideration_amount": "5000000", "consideration": "1"}
        self.assertEqual(dlv_tasks._dt_consideration_value(item), 5000000.0)

    def test_falls_back_to_consideration_when_no_amount_field(self):
        self.assertEqual(dlv_tasks._dt_consideration_value({"consideration": "2000000"}), 2000000.0)

    def test_missing_returns_none(self):
        self.assertIsNone(dlv_tasks._dt_consideration_value({}))

    def test_unparseable_returns_none(self):
        self.assertIsNone(dlv_tasks._dt_consideration_value({"consideration": "N/A"}))


class TestDtSumConsideration(unittest.TestCase):
    def test_sums_parseable_values_only(self):
        items = [{"consideration": "1000000", "currency_code": "KES"},
                 {"consideration": "N/A"},
                 {"consideration_amount": "2000000", "currency_code": "KES"}]
        self.assertEqual(dlv_tasks._dt_sum_consideration(items), "KES 3,000,000.00")

    def test_empty_list_sums_to_zero(self):
        self.assertEqual(dlv_tasks._dt_sum_consideration([]), "KES 0.00")

    def test_defaults_currency_to_kes_when_unset(self):
        self.assertEqual(dlv_tasks._dt_sum_consideration([{"consideration": "100"}]), "KES 100.00")


class TestDtValuerKey(unittest.TestCase):
    """_dt_valuer_key prefers valuer_uid, falls back to a normalized name, else empty."""

    def test_uses_uid_when_present(self):
        self.assertEqual(dlv_tasks._dt_valuer_key({"valuer_uid": "uid-1", "valuer_name": "Jane"}), "uid-1")

    def test_falls_back_to_name_when_uid_missing(self):
        self.assertEqual(dlv_tasks._dt_valuer_key({"valuer_uid": "", "valuer_name": "Jane"}), "JANE")

    def test_name_fallback_is_case_and_whitespace_insensitive(self):
        """A queued item's Title-Case name and a closed record's ALL-CAPS
        actor_name (common API casing) must resolve to the same key, or
        that valuer's history splits into two unmatched buckets."""
        queued = {"valuer_uid": "", "valuer_name": "Newton Muchemi Wambugu"}
        closed = {"valuer_uid": "", "valuer_name": "  NEWTON MUCHEMI WAMBUGU  "}
        self.assertEqual(dlv_tasks._dt_valuer_key(queued), dlv_tasks._dt_valuer_key(closed))

    def test_empty_when_neither_present(self):
        self.assertEqual(dlv_tasks._dt_valuer_key({}), "")


class TestDtCollectValuers(unittest.TestCase):
    """_dt_collect_valuers dedups by key across open + closed, sorted by name."""

    def test_dedupes_across_open_and_closed_and_sorts_by_name(self):
        with patch.object(dlv_tasks, "load_dlv_batch", return_value=[
                {"valuer_uid": "u2", "valuer_name": "Zed Valuer"},
                {"valuer_uid": "u1", "valuer_name": "Amos Valuer"},
             ]), \
             patch.object(dlv_tasks, "load_dlv_closed", return_value=[
                {"valuer_uid": "u1", "valuer_name": "Amos Valuer"},   # same key — should not duplicate
                {"valuer_uid": "u3", "valuer_name": "Beth Valuer"},
             ]):
            valuers = dlv_tasks._dt_collect_valuers()
        self.assertEqual(
            valuers,
            [
                {"key": "u1", "name": "Amos Valuer"},
                {"key": "u3", "name": "Beth Valuer"},
                {"key": "u2", "name": "Zed Valuer"},
            ],
        )

    def test_empty_when_no_items_anywhere(self):
        with patch.object(dlv_tasks, "load_dlv_batch", return_value=[]), \
             patch.object(dlv_tasks, "load_dlv_closed", return_value=[]):
            self.assertEqual(dlv_tasks._dt_collect_valuers(), [])

    def test_items_without_uid_or_name_are_skipped(self):
        with patch.object(dlv_tasks, "load_dlv_batch", return_value=[{"valuer_uid": "", "valuer_name": ""}]), \
             patch.object(dlv_tasks, "load_dlv_closed", return_value=[]):
            self.assertEqual(dlv_tasks._dt_collect_valuers(), [])


class TestDtFormatValuerReport(unittest.TestCase):
    """_dt_format_valuer_report renders queued + period-filtered closed history
    for one valuer, each ref as its own labeled block (not a packed one-liner)."""

    def test_queued_and_closed_sections_render_expected_fields(self):
        queued = [{"ref": "REG/A/1", "assessor": "Assessor A", "queued_at": "2026-07-10T10:00:00",
                   "consideration": "6000000", "currency_code": "KES", "parcel": "NAIROBI/BLOCK1/1"}]
        closed = [{"ref": "REG/A/2", "assessor": "Assessor B", "queued_at": "2026-07-01T09:00:00",
                   "closed_at": "2026-07-12T11:00:00", "closed_reason": "completed",
                   "consideration_amount": "4000000", "currency_code": "KES", "parcel": "NAIROBI/BLOCK2/2"}]
        lines = "\n".join(dlv_tasks._dt_format_valuer_report("Jane Doe", queued, closed, "1 week"))
        self.assertIn("👤 *DLV Report — Jane Doe*", lines)
        self.assertIn("⏳ *Currently Queued* (1)", lines)
        self.assertIn("`REG/A/1`", lines)
        self.assertIn("Assessor: Assessor A", lines)
        self.assertIn("💰 Consideration: KES 6,000,000.00", lines)
        self.assertIn("📋 Parcel: NAIROBI/BLOCK1/1", lines)
        self.assertIn("📜 *History* (1 week) — 1", lines)
        self.assertIn("`REG/A/2`", lines)
        self.assertIn("💰 Consideration: KES 4,000,000.00", lines)
        self.assertIn("📋 Parcel: NAIROBI/BLOCK2/2", lines)
        self.assertIn("✅ Completed", lines)

    def test_section_headers_show_consideration_totals(self):
        queued = [
            {"ref": "REG/A/1", "consideration": "1000000", "currency_code": "KES", "queued_at": "t"},
            {"ref": "REG/A/2", "consideration": "2000000", "currency_code": "KES", "queued_at": "t"},
        ]
        closed = [{"ref": "REG/A/3", "consideration_amount": "500000", "currency_code": "KES",
                   "closed_at": "t", "closed_reason": "completed"}]
        lines = "\n".join(dlv_tasks._dt_format_valuer_report("Jane Doe", queued, closed, "All time"))
        self.assertIn("⏳ *Currently Queued* (2) — Total: KES 3,000,000.00", lines)
        self.assertIn("📜 *History* (All time) — 1 — Total: KES 500,000.00", lines)

    def test_totals_are_zero_with_no_parseable_consideration(self):
        lines = "\n".join(dlv_tasks._dt_format_valuer_report(
            "Jane Doe", [{"ref": "REG/A/1", "queued_at": "t"}], [], "All time"))
        self.assertIn("Total: KES 0.00", lines)

    def test_empty_sections_render_none_placeholder(self):
        lines = "\n".join(dlv_tasks._dt_format_valuer_report("Jane Doe", [], [], "All time"))
        self.assertIn("⏳ *Currently Queued* (0)", lines)
        self.assertIn("📜 *History* (All time) — 0", lines)
        self.assertEqual(lines.count("_none_"), 2)

    def test_unknown_closed_reason_labeled_unknown(self):
        closed = [{"ref": "REG/A/3", "closed_at": "2026-07-12T11:00:00", "closed_reason": "something_else"}]
        lines = "\n".join(dlv_tasks._dt_format_valuer_report("Jane Doe", [], closed, "All time"))
        self.assertIn("❓ Unknown", lines)

    def test_tag_rendered_when_present_omitted_when_absent(self):
        queued = [
            {"ref": "REG/A/1", "assessor": "A", "queued_at": "t", "tag": "Queue"},
            {"ref": "REG/A/2", "assessor": "B", "queued_at": "t"},
        ]
        lines = "\n".join(dlv_tasks._dt_format_valuer_report("Jane Doe", queued, [], "All time"))
        self.assertIn("🏷 Tag: Queue", lines)
        # only one 🏷 marker — the untagged ref doesn't get one
        self.assertEqual(lines.count("🏷"), 1)

    def test_missing_consideration_and_parcel_fall_back_to_em_dash(self):
        lines = "\n".join(dlv_tasks._dt_format_valuer_report(
            "Jane Doe", [{"ref": "REG/A/1", "queued_at": "t"}], [], "All time"))
        self.assertIn("💰 Consideration: —", lines)
        self.assertIn("📋 Parcel: —", lines)


class TestDtFormatTagReport(unittest.TestCase):
    """_dt_format_tag_report — the By Tag report, spans multiple valuers so
    each line shows who it's with (unlike By Valuer, where that's implied)."""

    def test_queued_and_closed_show_valuer_per_line(self):
        queued = [{"ref": "REG/A/1", "valuer_name": "Jane Doe", "assessor": "A1",
                   "queued_at": "2026-07-10T10:00:00", "tag": "Queue",
                   "consideration": "3000000", "currency_code": "KES", "parcel": "NAIROBI/BLOCK1/1"}]
        closed = [{"ref": "REG/A/2", "valuer_name": "John Otieno", "assessor": "A2",
                   "queued_at": "2026-07-01T09:00:00", "closed_at": "2026-07-12T11:00:00",
                   "closed_reason": "completed", "tag": "Queue",
                   "consideration_amount": "2000000", "currency_code": "KES", "parcel": "NAIROBI/BLOCK2/2"}]
        lines = "\n".join(dlv_tasks._dt_format_tag_report("Queue", queued, closed, "All time"))
        self.assertIn("🏷 *DLV Report — Tag: Queue*", lines)
        self.assertIn("Valuer: Jane Doe", lines)
        self.assertIn("Valuer: John Otieno", lines)
        self.assertIn("💰 Consideration: KES 3,000,000.00", lines)
        self.assertIn("📋 Parcel: NAIROBI/BLOCK1/1", lines)
        self.assertIn("💰 Consideration: KES 2,000,000.00", lines)
        self.assertIn("📋 Parcel: NAIROBI/BLOCK2/2", lines)
        self.assertIn("⏳ *Currently Queued* (1) — Total: KES 3,000,000.00", lines)
        self.assertIn("📜 *History* (All time) — 1 — Total: KES 2,000,000.00", lines)

    def test_empty_sections_render_none_placeholder(self):
        lines = "\n".join(dlv_tasks._dt_format_tag_report("Queue", [], [], "All time"))
        self.assertEqual(lines.count("_none_"), 2)


class TestDtSendTelegram(unittest.TestCase):
    """_dt_send_telegram — Open Tasks' Telegram delivery, grouped by valuer."""

    def test_valuer_group_header_with_special_chars_is_escaped(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        rows = [{"ref": "REF1", "valuer_name": "Jane_Doe"}]
        _run(dlv_tasks._dt_send_telegram(123, rows, bot))
        text = bot.send_message.call_args[0][1]
        self.assertIn("👤 *Jane\\_Doe*", text)


class TestDtSendClosedReport(unittest.TestCase):
    """_dt_send_closed_report — Closed Tasks grouped by reason, each task
    rendered as the same labeled block every other DLV Tasks report uses
    (via _dt_format_report_item_block), not the old packed one-liner."""

    def test_tasks_rendered_as_labeled_blocks_grouped_by_reason(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        rows = [
            {"ref": "REG/A/1", "valuer_name": "Jane Doe", "assessor": "A1",
             "consideration_amount": "6000000", "currency_code": "KES", "parcel": "P1",
             "closed_at": "2026-07-12T11:00:00", "closed_reason": "completed", "tag": "Queue"},
            {"ref": "REG/A/2", "valuer_name": "John Otieno",
             "closed_at": "2026-07-13T09:00:00", "closed_reason": "returned"},
        ]
        _run(dlv_tasks._dt_send_closed_report(123, rows, bot))
        text = bot.send_message.call_args[0][1]
        self.assertIn("🔒 *Closed DLV Tasks* — 2 task(s)", text)
        self.assertIn("✅ Completed (1)", text)
        self.assertIn("📌 *Ref:* `REG/A/1`", text)
        self.assertIn("👤 Valuer: Jane Doe", text)
        self.assertIn("💰 Consideration: KES 6,000,000.00", text)
        self.assertIn("📋 Parcel: P1", text)
        self.assertIn("🏷 Tag: Queue", text)
        self.assertIn("↩️ Returned (1)", text)
        self.assertIn("📌 *Ref:* `REG/A/2`", text)
        # old one-liner format must be gone
        self.assertNotIn("| Valuer:", text)
        self.assertNotIn("| Closed:", text)


class TestDtTagKeyboard(unittest.TestCase):
    def test_lists_every_fixed_tag_plus_cancel(self):
        markup = dlv_tasks._dt_tag_keyboard()
        texts = [b.text for row in markup.inline_keyboard for b in row]
        for tag in dlv_tasks.DLV_TAGS:
            self.assertIn(tag, texts)
        self.assertIn("🛑 Cancel", texts)


class TestRecvDtPickTag(unittest.TestCase):
    """recv_dt_pick_tag — By Tag's tag picker, hands off to the shared period step."""

    def test_cancel_ends_conversation(self):
        update = _make_query_update("dt_picktag_cancel")
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with patch.object(dlv_tasks, "allowed", return_value=True):
            result = _run(dlv_tasks.recv_dt_pick_tag(update, ctx))
        self.assertEqual(result, dlv_tasks.ConversationHandler.END)

    def test_picking_a_tag_sets_report_mode_and_moves_to_period(self):
        update = _make_query_update("dt_picktag:Queue")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(dlv_tasks, "allowed", return_value=True):
            result = _run(dlv_tasks.recv_dt_pick_tag(update, ctx))
        self.assertEqual(result, dlv_tasks.DT.PICK_PERIOD)
        sess = dlv_tasks._get_dt_sess(ctx)
        self.assertEqual(sess.report_mode, "tag")
        self.assertEqual(sess.selected_tag, "Queue")


class TestRecvDtPickValuer(unittest.TestCase):
    """recv_dt_pick_valuer — By Valuer's valuer picker."""

    def test_valuer_name_with_special_chars_is_escaped(self):
        """Regression: an unescaped '_' in a valuer name raised
        telegram.error.BadRequest ("can't find end of the entity"). See
        common.md_escape."""
        update = _make_query_update("dt_pickvaluer:0")
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_tasks._get_dt_sess(ctx)
        sess.valuer_choices = [{"key": "u1", "name": "Jane_Doe"}]
        with patch.object(dlv_tasks, "allowed", return_value=True):
            _run(dlv_tasks.recv_dt_pick_valuer(update, ctx))
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Jane\\_Doe", text)


class TestRecvDtPeriodTagMode(unittest.TestCase):
    """recv_dt_period's tag-mode branch — filters the queue/closed store by
    tag (not valuer) and sends the By Tag report."""

    def test_filters_by_tag_and_sends_tag_report(self):
        update = _make_query_update("dt_period:0")
        ctx = MagicMock()
        ctx.user_data = {}
        ctx.bot.send_message = AsyncMock()
        sess = dlv_tasks._get_dt_sess(ctx)
        sess.report_mode  = "tag"
        sess.selected_tag = "Queue"

        queued = [{"ref": "REF1", "tag": "Queue"}, {"ref": "REF2", "tag": "Direct"}]
        closed = [{"ref": "REF3", "tag": "Queue", "closed_at": "2026-07-01"}]
        with patch.object(dlv_tasks, "allowed", return_value=True), \
             patch.object(dlv_tasks, "load_dlv_batch", return_value=queued), \
             patch.object(dlv_tasks, "load_dlv_closed", return_value=closed), \
             patch.object(dlv_tasks, "_dt_send_tag_report", new_callable=AsyncMock) as mock_send:
            result = _run(dlv_tasks.recv_dt_period(update, ctx))

        self.assertEqual(result, dlv_tasks.ConversationHandler.END)
        mock_send.assert_called_once()
        sent_queued = mock_send.call_args[0][2]
        self.assertEqual([i["ref"] for i in sent_queued], ["REF1"])

    def test_valuer_mode_building_report_message_escapes_name(self):
        update = _make_query_update("dt_period:0")
        ctx = MagicMock()
        ctx.user_data = {}
        ctx.bot.send_message = AsyncMock()
        sess = dlv_tasks._get_dt_sess(ctx)
        sess.report_mode      = "valuer"
        sess.selected_valuer  = {"key": "u1", "name": "Jane_Doe"}
        with patch.object(dlv_tasks, "allowed", return_value=True), \
             patch.object(dlv_tasks, "load_dlv_batch", return_value=[]), \
             patch.object(dlv_tasks, "load_dlv_closed", return_value=[]), \
             patch.object(dlv_tasks, "_dt_send_valuer_report", new_callable=AsyncMock):
            _run(dlv_tasks.recv_dt_period(update, ctx))
        building_text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Jane\\_Doe", building_text)


if __name__ == "__main__":
    unittest.main()
