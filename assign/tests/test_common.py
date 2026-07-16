#!/usr/bin/env python3
"""
Unit tests for common.py additions made during the Sectional Properties
extraction: _safe_err (promoted from bot.py, shared by New Assignment/
Receive Tasks/Sectional Properties) and load_sectional_config/
save_sectional_config (shared by Sectional Properties and Auto Fetch).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import common


class TestSafeErr(unittest.TestCase):
    def test_http_error_reduced_to_status_code(self):
        response = MagicMock(status_code=404)
        err = requests.HTTPError("not found")
        err.response = response
        self.assertEqual(common._safe_err(err), "server returned HTTP 404")

    def test_http_error_without_response_falls_back(self):
        err = requests.HTTPError("boom")
        err.response = None
        self.assertEqual(common._safe_err(err), "unexpected error — check logs")

    def test_non_http_exception_is_generic(self):
        self.assertEqual(common._safe_err(RuntimeError("secret detail")), "unexpected error — check logs")
        # the original message must never leak through
        self.assertNotIn("secret detail", common._safe_err(RuntimeError("secret detail")))


class TestSectionalConfigPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg_file = os.path.join(self.tmpdir.name, "saved_sectional_config.json")
        self._patch = patch.object(common, "SAVED_SECTIONAL_CONFIG_FILE", self.cfg_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(common.load_sectional_config())

    def test_save_then_load_roundtrip(self):
        common.save_sectional_config({"auto_route": True, "cred_type": "staff2"})
        cfg = common.load_sectional_config()
        self.assertEqual(cfg["auto_route"], True)
        self.assertEqual(cfg["cred_type"], "staff2")


class TestPersistAssignment(unittest.TestCase):
    """persist_assignment — ref -> valuer_name/valuer_uid/assigned_at, plus
    whatever extra context a caller passes (e.g. DLV Batch's queue item)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.assign_file = os.path.join(self.tmpdir.name, "saved_assignments.json")
        self._patch = patch.object(common, "SAVED_ASSIGNMENTS_FILE", self.assign_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_basic_fields_saved_without_extra(self):
        common.persist_assignment("REF1", "Jane Doe", "uid-1")
        record = common.load_saved_assignments()["REF1"]
        self.assertEqual(record["valuer_name"], "Jane Doe")
        self.assertEqual(record["valuer_uid"], "uid-1")
        self.assertIn("assigned_at", record)

    def test_extra_fields_merged_in(self):
        common.persist_assignment("REF1", "Jane Doe", "uid-1", extra={
            "tag": "Queue", "parcel": "P1", "consideration": "1000000", "currency_code": "KES",
        })
        record = common.load_saved_assignments()["REF1"]
        self.assertEqual(record["tag"], "Queue")
        self.assertEqual(record["parcel"], "P1")
        self.assertEqual(record["consideration"], "1000000")
        self.assertEqual(record["currency_code"], "KES")
        # core fields still present alongside extra
        self.assertEqual(record["valuer_name"], "Jane Doe")



class TestBeCredKeyboard(unittest.TestCase):
    def test_no_valid_tokens_returns_none(self):
        with patch.object(common, "get_valid_tokens", return_value=None):
            self.assertIsNone(common._be_cred_keyboard())

    def test_only_lists_creds_with_valid_tokens(self):
        with patch.object(common, "get_valid_tokens", side_effect=lambda k: "tok" if k == "staff2" else None):
            kbd = common._be_cred_keyboard()
        self.assertIsNotNone(kbd)
        self.assertEqual(len(kbd.inline_keyboard), 1)
        self.assertEqual(kbd.inline_keyboard[0][0].callback_data, "be_cred:staff2")


class TestNodeLabels(unittest.TestCase):
    def test_known_node_codes_have_labels(self):
        self.assertIn("VALUATION_STAMP_DUTY_CREATED", common._NODE_LABELS)
        self.assertIn("VALUATION_STAMP_DUTY_VALUER_REPORT", common._NODE_LABELS)
        self.assertIn("STAMP_DUTY_PAYMENT_DEFINITION", common._NODE_LABELS)


if __name__ == "__main__":
    unittest.main()
