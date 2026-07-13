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

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlv_tasks
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


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


class TestFormatConsideration(unittest.TestCase):
    """_format_consideration turns a raw amount + currency into 'KES 1,234.00'."""

    def test_formats_amount_with_currency(self):
        self.assertEqual(dlv_tasks._format_consideration("6000000", "KES"), "KES 6,000,000.00")

    def test_defaults_currency_to_kes_when_missing(self):
        self.assertEqual(dlv_tasks._format_consideration("100", ""), "KES 100.00")

    def test_empty_amount_returns_empty_string(self):
        self.assertEqual(dlv_tasks._format_consideration("", "KES"), "")

    def test_non_numeric_amount_falls_back_to_str(self):
        self.assertEqual(dlv_tasks._format_consideration("N/A", "KES"), "N/A")


class TestDtFormatTaskBlock(unittest.TestCase):
    """_dt_format_task_block renders one task's full Lookup-Reference-style detail block."""

    def _task(self, **overrides):
        t = {
            "ref": "REG/TSFR/SHIZU9NCC8", "status": "ONGOING",
            "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "valuer_name": "BYRON MARCEL ONDITI",
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
        self.assertIn("🏢 Registry: NAIROBI", block)
        self.assertIn("📍 County: nairobi", block)
        self.assertIn("💰 Consideration: KES 6,000,000.00", block)
        self.assertIn("📋 Parcel: NAIROBI/BLOCK209/309", block)
        self.assertIn("📅 Created: 2026-07-08T15:36:21.616029", block)
        self.assertNotIn("not found", block)

    def test_missing_fields_fall_back_to_em_dash(self):
        block = dlv_tasks._dt_format_task_block(1, self._task(
            status="", node="", registry="", county="", consideration="", parcel="", date_created="",
        ))
        self.assertIn("📊 Status: —", block)
        self.assertIn("🔄 Node: —", block)
        self.assertIn("🏢 Registry: —", block)
        self.assertIn("📍 County: —", block)
        self.assertIn("💰 Consideration: —", block)
        self.assertIn("📋 Parcel: —", block)
        self.assertIn("📅 Created: —", block)

    def test_not_found_appends_note(self):
        block = dlv_tasks._dt_format_task_block(1, self._task(found=False, location=""))
        self.assertIn("❓ _not found in Assessor or DLV queues_", block)

    def test_still_with_assessor_appends_note(self):
        block = dlv_tasks._dt_format_task_block(1, self._task(location="assessor"))
        self.assertIn("⏳ _still with Assessor, not yet in DLV_", block)


if __name__ == "__main__":
    unittest.main()
