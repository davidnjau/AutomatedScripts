#!/usr/bin/env python3
"""
Unit tests for dlv_core.py — the shared DLV search/classify/queue-storage
layer used by both DLV Batch and DLV Tasks.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlv_core
from ardhisasa_auth import AuthTokens


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Records every .get() call and returns responses in call order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        return self.responses.pop(0)


TOKENS = AuthTokens(access_token="acc", jwt="jwt")


class TestDlvRequestType(unittest.TestCase):
    def test_county_prefix(self):
        self.assertEqual(dlv_core._dlv_request_type("CNTYINV/ABC123"), "COUNTY_STAMP_DUTY")

    def test_county_prefix_lowercase(self):
        self.assertEqual(dlv_core._dlv_request_type("cntyinv/abc123"), "COUNTY_STAMP_DUTY")

    def test_non_county(self):
        self.assertEqual(dlv_core._dlv_request_type("REG/TSFR/ABC123"), "STAMP_DUTY")


class TestExtractAssessor(unittest.TestCase):
    def test_finds_assessor_role(self):
        officers = [
            {"name": "JOHN DOE", "role": "SUPPORT"},
            {"name": "JANE SMITH", "role": "ASSESSOR_OF_STAMP_DUTY"},
        ]
        self.assertEqual(dlv_core._extract_assessor(officers), "JANE SMITH")

    def test_no_assessor_returns_empty(self):
        officers = [{"name": "JOHN DOE", "role": "SUPPORT"}]
        self.assertEqual(dlv_core._extract_assessor(officers), "")

    def test_empty_list(self):
        self.assertEqual(dlv_core._extract_assessor([]), "")


class TestResolveAssessor(unittest.TestCase):
    """_resolve_assessor — regression test for a real bug: a County ref's
    officer holds COUNTY_REGISTRAR, not ASSESSOR_OF_STAMP_DUTY, so strict
    extraction returned "" and the name was lost by the time it reached the
    DLV Report (Auto Fetch's own display-time fallback masked the same gap
    in its own view, so it looked fine there while being blank everywhere
    that read the stored value instead)."""

    def test_prefers_strict_assessor_role(self):
        officers = [
            {"name": "JOHN DOE", "role": "COUNTY_REGISTRAR"},
            {"name": "JANE SMITH", "role": "ASSESSOR_OF_STAMP_DUTY"},
        ]
        self.assertEqual(dlv_core._resolve_assessor(officers), "JANE SMITH")

    def test_falls_back_to_any_named_officer_when_no_strict_match(self):
        officers = [{"name": "REDEMPTA AKOTH OKWANY", "role": "COUNTY_REGISTRAR"}]
        self.assertEqual(
            dlv_core._resolve_assessor(officers), "REDEMPTA AKOTH OKWANY (COUNTY_REGISTRAR)",
        )

    def test_empty_officers_returns_empty(self):
        self.assertEqual(dlv_core._resolve_assessor([]), "")

    def test_officer_without_a_name_is_skipped(self):
        self.assertEqual(dlv_core._resolve_assessor([{"name": "", "role": "COUNTY_REGISTRAR"}]), "")


class TestClassifyDlvDetail(unittest.TestCase):
    def test_completed_is_closed(self):
        detail = {
            "application_status": "COMPLETED",
            "node": "VALUATION_STAMP_DUTY_COMPLETED",
        }
        info = dlv_core._classify_dlv_detail(detail)
        self.assertEqual(info["bucket"], "closed")
        self.assertEqual(info["closed_reason"], "completed")

    def test_returned_is_closed(self):
        detail = {
            "application_status": "RETURNED",
            "node": "VALUATION_STAMP_DUTY_CREATED",
        }
        info = dlv_core._classify_dlv_detail(detail)
        self.assertEqual(info["bucket"], "closed")
        self.assertEqual(info["closed_reason"], "returned")

    def test_ongoing_is_open(self):
        detail = {
            "application_status": "ONGOING",
            "node": "VALUATION_STAMP_DUTY_CREATED",
        }
        info = dlv_core._classify_dlv_detail(detail)
        self.assertEqual(info["bucket"], "open")
        self.assertEqual(info["closed_reason"], "")

    def test_extracts_assessor_name(self):
        detail = {
            "application_status": "ONGOING",
            "node": "VALUATION_STAMP_DUTY_CREATED",
            "assessor": {"role": "ASSESSOR_OF_STAMP_DUTY", "user_details": {"names": "JANE SMITH"}},
        }
        info = dlv_core._classify_dlv_detail(detail)
        self.assertEqual(info["assessor_name"], "JANE SMITH")

    def test_missing_assessor_defaults_empty(self):
        info = dlv_core._classify_dlv_detail({"application_status": "ONGOING", "node": "X"})
        self.assertEqual(info["assessor_name"], "")


class TestSearchRefDlv(unittest.TestCase):
    def test_finds_ref_on_first_filter_non_county(self):
        ref = "REG/TSFR/ABC123"
        fake = FakeSession([FakeResponse({"results": [{"reference_number": ref}]})])
        with patch.object(dlv_core, "build_session", return_value=fake):
            task = dlv_core._search_ref_dlv(TOKENS, ref)
        self.assertIsNotNone(task)
        self.assertEqual(task["_request_type"], "STAMP_DUTY")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["params"]["filter"], "Pending")
        self.assertNotIn("from_ardhipay", fake.calls[0]["params"])

    def test_county_ref_sets_from_ardhipay(self):
        ref = "CNTYINV/XYZ999"
        fake = FakeSession([FakeResponse({"results": [{"reference_number": ref}]})])
        with patch.object(dlv_core, "build_session", return_value=fake):
            task = dlv_core._search_ref_dlv(TOKENS, ref)
        self.assertEqual(task["_request_type"], "COUNTY_STAMP_DUTY")
        self.assertEqual(fake.calls[0]["params"]["from_ardhipay"], "true")

    def test_tries_next_filter_when_not_found(self):
        ref = "REG/TSFR/ABC123"
        fake = FakeSession([
            FakeResponse({"results": []}),                               # Pending: miss
            FakeResponse({"results": [{"reference_number": ref}]}),      # Ongoing: hit
        ])
        with patch.object(dlv_core, "build_session", return_value=fake):
            task = dlv_core._search_ref_dlv(TOKENS, ref)
        self.assertIsNotNone(task)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1]["params"]["filter"], "Ongoing")

    def test_returns_none_when_never_found(self):
        ref = "REG/TSFR/ABC123"
        responses = [FakeResponse({"results": []}) for _ in dlv_core._DLV_NONCOUNTY_FILTERS]
        fake = FakeSession(responses)
        with patch.object(dlv_core, "build_session", return_value=fake):
            task = dlv_core._search_ref_dlv(TOKENS, ref)
        self.assertIsNone(task)


class TestFetchRefDetailDlv(unittest.TestCase):
    def test_returns_json_body(self):
        fake = FakeSession([FakeResponse({"application_status": "ONGOING"})])
        with patch.object(dlv_core, "build_session", return_value=fake):
            detail = dlv_core._fetch_ref_detail_dlv(TOKENS, "req-1")
        self.assertEqual(detail["application_status"], "ONGOING")


class TestSearchRefStampduty(unittest.TestCase):
    def test_finds_ref_in_hq_variant(self):
        ref = "REG/TSFR/ABC123"
        fake = FakeSession([FakeResponse({"results": [{"reference_number": ref, "id": "1"}]})])
        with patch.object(dlv_core, "build_session", return_value=fake):
            task = dlv_core._search_ref_stampduty(TOKENS, ref)
        self.assertIsNotNone(task)
        self.assertNotIn("from_ardhipay", fake.calls[0]["params"])

    def test_falls_back_to_county_variant(self):
        ref = "CNTYINV/XYZ999"
        fake = FakeSession([
            FakeResponse({"results": []}),
            FakeResponse({"results": [{"reference_number": ref, "id": "2"}]}),
        ])
        with patch.object(dlv_core, "build_session", return_value=fake):
            task = dlv_core._search_ref_stampduty(TOKENS, ref)
        self.assertIsNotNone(task)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1]["params"]["from_ardhipay"], "true")

    def test_returns_none_when_not_found(self):
        fake = FakeSession([FakeResponse({"results": []}), FakeResponse({"results": []})])
        with patch.object(dlv_core, "build_session", return_value=fake):
            task = dlv_core._search_ref_stampduty(TOKENS, "REG/TSFR/NOPE")
        self.assertIsNone(task)


class TestFetchStampdutyDetail(unittest.TestCase):
    def test_returns_details_key(self):
        fake = FakeSession([FakeResponse({"details": {"officers": []}})])
        with patch.object(dlv_core, "build_session", return_value=fake):
            det = dlv_core._fetch_stampduty_detail(TOKENS, "req-1")
        self.assertEqual(det, {"officers": []})


class TestDlvQueuePersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.batch_file = os.path.join(self.tmpdir.name, "saved_dlv_batch.json")
        self.closed_file = os.path.join(self.tmpdir.name, "saved_dlv_closed.json")
        self._batch_patch = patch.object(dlv_core, "SAVED_DLV_BATCH_FILE", self.batch_file)
        self._closed_patch = patch.object(dlv_core, "SAVED_DLV_CLOSED_FILE", self.closed_file)
        self._batch_patch.start()
        self._closed_patch.start()

    def tearDown(self):
        self._batch_patch.stop()
        self._closed_patch.stop()
        self.tmpdir.cleanup()

    def test_load_dlv_batch_missing_file_returns_empty(self):
        self.assertEqual(dlv_core.load_dlv_batch(), [])

    def test_save_then_load_roundtrip(self):
        items = [{"ref": "REG/TSFR/ABC123", "valuer_name": "Jane"}]
        dlv_core.save_dlv_batch(items)
        self.assertEqual(dlv_core.load_dlv_batch(), items)

    def test_clear_dlv_batch(self):
        dlv_core.save_dlv_batch([{"ref": "X"}])
        dlv_core.clear_dlv_batch()
        self.assertEqual(dlv_core.load_dlv_batch(), [])

    def test_append_dlv_closed_dedupes_by_ref(self):
        dlv_core._append_dlv_closed({"ref": "REG/TSFR/ABC123", "closed_reason": "completed"})
        dlv_core._append_dlv_closed({"ref": "REG/TSFR/ABC123", "closed_reason": "returned"})
        closed = dlv_core.load_dlv_closed()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["closed_reason"], "returned")

    def test_append_dlv_closed_keeps_distinct_refs(self):
        dlv_core._append_dlv_closed({"ref": "A"})
        dlv_core._append_dlv_closed({"ref": "B"})
        refs = {c["ref"] for c in dlv_core.load_dlv_closed()}
        self.assertEqual(refs, {"A", "B"})


class TestDlvTags(unittest.TestCase):
    """DLV_TAGS — the fixed tag vocabulary shared by DLV Batch (sets it) and
    DLV Tasks' By Tag report (filters on it)."""

    def test_is_a_non_empty_list_of_distinct_strings(self):
        self.assertIsInstance(dlv_core.DLV_TAGS, list)
        self.assertTrue(dlv_core.DLV_TAGS)
        self.assertEqual(len(dlv_core.DLV_TAGS), len(set(dlv_core.DLV_TAGS)))
        self.assertTrue(all(isinstance(t, str) and t for t in dlv_core.DLV_TAGS))


if __name__ == "__main__":
    unittest.main()
