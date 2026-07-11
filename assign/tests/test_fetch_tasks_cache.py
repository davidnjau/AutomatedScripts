#!/usr/bin/env python3
"""
Unit tests for fetch_tasks_cache.py — the short-lived ref -> assessor cache
written by Fetch Tasks and read by DLV Batch / DLV Tasks.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_tasks_cache as ftc


class TestFetchTasksCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_file = os.path.join(self.tmpdir.name, "fetch_tasks_log.json")
        self._patch = patch.object(ftc, "SAVED_FETCH_TASKS_LOG_FILE", self.log_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def _write_raw_log(self, log: dict) -> None:
        with open(self.log_file, "w") as f:
            json.dump(log, f)

    def test_load_missing_file_returns_empty_dict(self):
        self.assertEqual(ftc.load_fetch_tasks_log(), {})

    def test_log_fetch_tasks_stores_expected_fields(self):
        tasks = [{
            "reference_number": "REG/TSFR/ABC123",
            "assessor": "JANE SMITH",
            "parcel_number": "NAIROBI/BLOCK1/1",
            "registry": "NAIROBI",
            "county": "NAIROBI",
            "date_created": "2026-07-10",
        }]
        ftc._log_fetch_tasks(tasks)
        entry = ftc._fetch_tasks_log_lookup("REG/TSFR/ABC123")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["assessor"], "JANE SMITH")
        self.assertEqual(entry["parcel"], "NAIROBI/BLOCK1/1")
        self.assertEqual(entry["registry"], "NAIROBI")
        self.assertEqual(entry["county"], "NAIROBI")
        self.assertEqual(entry["date_created"], "2026-07-10")
        self.assertIn("cached_at", entry)

    def test_log_fetch_tasks_skips_tasks_without_ref(self):
        ftc._log_fetch_tasks([{"assessor": "NO REF HERE"}])
        self.assertEqual(ftc.load_fetch_tasks_log(), {})

    def test_log_fetch_tasks_empty_list_is_noop(self):
        ftc._log_fetch_tasks([])
        self.assertFalse(os.path.exists(self.log_file))

    def test_lookup_missing_ref_returns_none(self):
        ftc._log_fetch_tasks([{"reference_number": "A", "assessor": "X"}])
        self.assertIsNone(ftc._fetch_tasks_log_lookup("B"))

    def test_remove_drops_only_named_refs(self):
        ftc._log_fetch_tasks([
            {"reference_number": "A", "assessor": "X"},
            {"reference_number": "B", "assessor": "Y"},
        ])
        ftc._fetch_tasks_log_remove(["A"])
        self.assertIsNone(ftc._fetch_tasks_log_lookup("A"))
        self.assertIsNotNone(ftc._fetch_tasks_log_lookup("B"))

    def test_remove_empty_list_is_noop(self):
        ftc._log_fetch_tasks([{"reference_number": "A", "assessor": "X"}])
        ftc._fetch_tasks_log_remove([])
        self.assertIsNotNone(ftc._fetch_tasks_log_lookup("A"))

    def test_stale_entry_is_pruned_on_lookup(self):
        stale_ts = (datetime.now() - timedelta(seconds=ftc.FETCH_TASKS_LOG_TTL_SECONDS + 3600)).isoformat(
            timespec="seconds"
        )
        self._write_raw_log({"REG/TSFR/OLD": {"assessor": "STALE", "cached_at": stale_ts}})
        self.assertIsNone(ftc._fetch_tasks_log_lookup("REG/TSFR/OLD"))

    def test_fresh_entry_survives_prune(self):
        fresh_ts = datetime.now().isoformat(timespec="seconds")
        self._write_raw_log({"REG/TSFR/NEW": {"assessor": "FRESH", "cached_at": fresh_ts}})
        entry = ftc._fetch_tasks_log_lookup("REG/TSFR/NEW")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["assessor"], "FRESH")


if __name__ == "__main__":
    unittest.main()
