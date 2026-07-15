#!/usr/bin/env python3
"""
Unit tests for dlv_batch.py — batch-input parsing, saved-valuer resolution,
and the per-ref processing state machine used by both the 5-minute job and
the "Query Now" DLV Queue action.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlv_batch
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


class TestParseBatchInput(unittest.TestCase):
    def test_single_ref_single_valuer(self):
        groups = dlv_batch._parse_batch_input("REG/TSFR/ABC123 : John Kamau")
        self.assertEqual(groups, [{"refs": ["REG/TSFR/ABC123"], "valuer_name_raw": "John Kamau"}])

    def test_multiple_refs_one_line(self):
        groups = dlv_batch._parse_batch_input("REF1, REF2, REF3 : Byron")
        self.assertEqual(groups[0]["refs"], ["REF1", "REF2", "REF3"])

    def test_multiple_lines(self):
        text = "REF1 : Byron\nREF2, REF3 : Jane"
        groups = dlv_batch._parse_batch_input(text)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1]["valuer_name_raw"], "Jane")

    def test_refs_uppercased(self):
        groups = dlv_batch._parse_batch_input("reg/tsfr/abc123 : John")
        self.assertEqual(groups[0]["refs"], ["REG/TSFR/ABC123"])

    def test_line_without_colon_is_skipped(self):
        groups = dlv_batch._parse_batch_input("REF1 REF2 John\nREF3 : Jane")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["valuer_name_raw"], "Jane")

    def test_blank_lines_skipped(self):
        groups = dlv_batch._parse_batch_input("\n\nREF1 : John\n\n")
        self.assertEqual(len(groups), 1)

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(dlv_batch._parse_batch_input(""), [])

    def test_missing_valuer_name_is_skipped(self):
        groups = dlv_batch._parse_batch_input("REF1 : ")
        self.assertEqual(groups, [])

    def test_whitespace_trimmed(self):
        groups = dlv_batch._parse_batch_input("  REF1 ,  REF2   :   Jane Doe  ")
        self.assertEqual(groups[0]["refs"], ["REF1", "REF2"])
        self.assertEqual(groups[0]["valuer_name_raw"], "Jane Doe")


class TestResolveValuerFromSaved(unittest.TestCase):
    def test_case_insensitive_substring_match(self):
        saved = [{"name": "JOHN KAMAU MWANGI", "uid": "1", "account_number": "A1"}]
        with patch.object(dlv_batch, "load_saved_valuers", return_value=saved):
            result = dlv_batch._resolve_valuer_from_saved("kamau")
        self.assertEqual(result["uid"], "1")

    def test_no_match_returns_none(self):
        saved = [{"name": "JOHN KAMAU", "uid": "1", "account_number": "A1"}]
        with patch.object(dlv_batch, "load_saved_valuers", return_value=saved):
            result = dlv_batch._resolve_valuer_from_saved("nonexistent")
        self.assertIsNone(result)

    def test_empty_saved_list(self):
        with patch.object(dlv_batch, "load_saved_valuers", return_value=[]):
            self.assertIsNone(dlv_batch._resolve_valuer_from_saved("anyone"))


class TestProcessDlvBatchItem(unittest.TestCase):
    def setUp(self):
        self.item = {"ref": "REG/TSFR/ABC123", "valuer_name": "Jane Doe", "valuer_uid": "uid-1"}
        self.http_sess = MagicMock()
        self.assign_url = "https://example/assign"
        self.auth_hdrs = {}

    def _run(self):
        return dlv_batch._process_dlv_batch_item(
            TOKENS, self.http_sess, self.assign_url, self.auth_hdrs, dict(self.item)
        )

    def test_not_found_in_dlv_is_kept_for_retry(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value=None):
            result = self._run()
        self.assertTrue(result["keep"])
        self.assertIsNone(result["line"])
        self.assertEqual(result["item"]["last_error"], "Not found in DLV endpoint")

    def test_empty_detail_is_kept_for_retry(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value=None):
            result = self._run()
        self.assertTrue(result["keep"])

    def test_completed_closes_the_item(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1", "_request_type": "STAMP_DUTY"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={"node": "VALUATION_STAMP_DUTY_COMPLETED"}), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "closed", "closed_reason": "completed", "application_status": "COMPLETED",
                 "node": "VALUATION_STAMP_DUTY_COMPLETED", "assessor_name": "", "consideration_amount": "",
                 "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertFalse(result["keep"])
        self.assertIsNotNone(result["closed"])
        self.assertEqual(result["closed"]["closed_reason"], "completed")
        self.assertIn("Closed (Completed)", result["line"])

    def test_returned_closes_the_item(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={"node": "X"}), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "closed", "closed_reason": "returned", "application_status": "RETURNED",
                 "node": "VALUATION_STAMP_DUTY_CREATED", "assessor_name": "", "consideration_amount": "",
                 "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertIn("Closed (Returned)", result["line"])
        self.assertEqual(result["closed"]["closed_reason"], "returned")

    def test_open_created_node_assigns(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={"node": "VALUATION_STAMP_DUTY_CREATED"}), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_CREATED", "assessor_name": "Jane Assessor",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }), \
             patch.object(dlv_batch, "persist_assignment") as mock_persist:
            self.http_sess.post.return_value = MagicMock(raise_for_status=lambda: None)
            result = self._run()
        self.assertFalse(result["keep"])
        self.assertIn("assigned to", result["line"])
        self.assertEqual(result["item"]["assessor"], "Jane Assessor")
        mock_persist.assert_called_once_with("REG/TSFR/ABC123", "Jane Doe", "uid-1")
        self.http_sess.post.assert_called_once()

    def test_already_assigned_to_someone_else_reports_and_skips(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
                 "actors": [{"role": "VALUATION OFFICER", "user_details": {"id": "uid-2", "names": "EXISTING VALUER"}}],
             }), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "assessor_name": "",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertFalse(result["keep"])
        self.assertIn("already assigned to *EXISTING VALUER*, not *Jane Doe*", result["line"])
        self.http_sess.post.assert_not_called()

    def test_already_assigned_to_intended_valuer_reports_success(self):
        # The item's own valuer_uid ("uid-1") matches the actor's id — this is
        # the case that used to render identically to "taken by someone else",
        # reading as a failure even though the intended valuer already has it.
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
                 "actors": [{"role": "VALUATION OFFICER", "user_details": {"id": "uid-1", "names": "Jane Doe"}}],
             }), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "assessor_name": "",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertFalse(result["keep"])
        self.assertIn("✅", result["line"])
        self.assertIn("already correctly assigned to *Jane Doe*", result["line"])
        self.http_sess.post.assert_not_called()

    def test_no_valuation_officer_actor_reports_no_actor_listed(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
                 "actors": [{"role": "ASSESSOR_OF_STAMP_DUTY", "user_details": {"id": "uid-9", "names": "Some Assessor"}}],
             }), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "assessor_name": "",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertFalse(result["keep"])
        self.assertIn("no actor listed", result["line"])
        self.http_sess.post.assert_not_called()

    def test_search_exception_is_kept_for_retry(self):
        with patch.object(dlv_batch, "_search_ref_dlv", side_effect=RuntimeError("boom")):
            result = self._run()
        self.assertTrue(result["keep"])
        self.assertIn("boom", result["item"]["last_error"])


if __name__ == "__main__":
    unittest.main()
