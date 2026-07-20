#!/usr/bin/env python3
"""
Unit tests for dlv_core.py — the shared DLV search/classify/queue-storage
layer used by both DLV Batch and DLV Tasks.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import json
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
        self.assign_file = os.path.join(self.tmpdir.name, "saved_assignments.json")
        self.hold_file = os.path.join(self.tmpdir.name, "saved_hold_tasks.json")
        self.records_file = os.path.join(self.tmpdir.name, "saved_dlv_records.json")
        self._patches = [
            patch.object(dlv_core, "SAVED_DLV_BATCH_FILE", self.batch_file),
            patch.object(dlv_core, "SAVED_DLV_CLOSED_FILE", self.closed_file),
            patch.object(dlv_core, "SAVED_ASSIGNMENTS_FILE", self.assign_file),
            patch.object(dlv_core, "SAVED_HOLD_TASKS_FILE", self.hold_file),
            patch.object(dlv_core, "SAVED_DLV_RECORDS_FILE", self.records_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
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

    def test_clear_dlv_batch_leaves_a_removed_trace(self):
        dlv_core.save_dlv_batch([{"ref": "X"}])
        dlv_core.clear_dlv_batch()
        store = dlv_core._load_consolidated()
        self.assertEqual(store["X"]["status"], "removed")
        self.assertIsNotNone(store["X"]["removed_at"])

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

    def test_save_dlv_batch_never_touches_a_ref_already_transitioned_this_cycle(self):
        """Regression: save_dlv_batch(remaining) must not clobber a ref that
        _append_dlv_closed already moved to "completed"/"returned" this same
        cycle, even though the ref is absent from `remaining`."""
        dlv_core.save_dlv_batch([{"ref": "A"}, {"ref": "B"}])
        dlv_core._append_dlv_closed({"ref": "B", "closed_reason": "completed"})
        dlv_core.save_dlv_batch([{"ref": "A"}])   # B dropped from the list, as a real caller would
        self.assertEqual([i["ref"] for i in dlv_core.load_dlv_batch()], ["A"])
        closed = dlv_core.load_dlv_closed()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["ref"], "B")

    def test_append_dlv_closed_merges_onto_prior_queued_fields(self):
        """"Enrich with highest data available": fields known while queued
        (assessor/parcel/tag) must survive into the closed record even though
        the caller's closed_record already spreads **item today — this
        confirms the store itself also preserves them independently."""
        dlv_core.save_dlv_batch([{"ref": "A", "assessor": "Jane", "tag": "Queue"}])
        dlv_core._append_dlv_closed({"ref": "A", "closed_reason": "completed"})
        closed = dlv_core.load_dlv_closed()[0]
        self.assertEqual(closed["assessor"], "Jane")
        self.assertEqual(closed["tag"], "Queue")

    def test_mark_removed_sets_status_and_timestamp(self):
        dlv_core.save_dlv_batch([{"ref": "A"}])
        dlv_core.mark_removed(["A"])
        self.assertEqual(dlv_core.load_dlv_batch(), [])
        store = dlv_core._load_consolidated()
        self.assertEqual(store["A"]["status"], "removed")
        self.assertIsNotNone(store["A"]["removed_at"])

    def test_mark_removed_is_a_noop_for_an_untracked_ref(self):
        dlv_core.mark_removed(["NEVER_SEEN"])
        self.assertEqual(dlv_core._load_consolidated(), {})

    def test_migration_from_legacy_batch_and_closed_files(self):
        """First read with no saved_dlv_records.json rebuilds it from the two
        legacy files, deriving status per record, and leaves both legacy
        files untouched on disk."""
        with open(self.batch_file, "w") as f:
            json.dump([{"ref": "Q1", "valuer_name": "Jane"}], f)
        with open(self.closed_file, "w") as f:
            json.dump([
                {"ref": "C1", "closed_reason": "completed"},
                {"ref": "C2", "closed_reason": "returned"},
            ], f)

        store = dlv_core._load_consolidated()

        self.assertEqual(store["Q1"]["status"], "queued")
        self.assertEqual(store["C1"]["status"], "completed")
        self.assertEqual(store["C2"]["status"], "returned")
        self.assertTrue(os.path.exists(self.batch_file))
        self.assertTrue(os.path.exists(self.closed_file))
        self.assertTrue(os.path.exists(self.records_file))
        # legacy files themselves are untouched
        with open(self.batch_file) as f:
            self.assertEqual(json.load(f), [{"ref": "Q1", "valuer_name": "Jane"}])

    def test_migration_folds_in_legacy_assignments_file(self):
        with open(self.assign_file, "w") as f:
            json.dump({"A1": {"valuer_name": "Jane", "valuer_uid": "u1", "assigned_at": "2026-01-01"}}, f)

        store = dlv_core._load_consolidated()

        self.assertEqual(store["A1"]["status"], "assigned")
        self.assertEqual(store["A1"]["valuer_name"], "Jane")
        self.assertTrue(os.path.exists(self.assign_file))

    def test_migration_precedence_queued_and_closed_win_over_assigned(self):
        """A ref present in both the assignments ledger and a more-progressed
        legacy file (batch/closed) must end up with the more-progressed
        status, since assignments is merged in as the base layer first."""
        with open(self.assign_file, "w") as f:
            json.dump({
                "Q1": {"valuer_name": "Jane", "valuer_uid": "u1"},
                "C1": {"valuer_name": "Jane", "valuer_uid": "u1"},
            }, f)
        with open(self.batch_file, "w") as f:
            json.dump([{"ref": "Q1", "valuer_name": "Jane", "tag": "Queue"}], f)
        with open(self.closed_file, "w") as f:
            json.dump([{"ref": "C1", "closed_reason": "completed"}], f)

        store = dlv_core._load_consolidated()

        self.assertEqual(store["Q1"]["status"], "queued")
        self.assertEqual(store["Q1"]["tag"], "Queue")
        self.assertEqual(store["C1"]["status"], "completed")
        # assignment-only fields survive even though a more-progressed file won
        self.assertEqual(store["C1"]["valuer_uid"], "u1")

    def test_migration_does_not_let_an_empty_later_field_erase_an_earlier_value(self):
        """"Enrich with whichever source has more data": a more-progressed
        legacy file's record for the same ref might simply never have
        captured a field (empty string, not a deliberate blank) — that must
        not erase a value an earlier, richer source already provided."""
        with open(self.assign_file, "w") as f:
            json.dump({"C1": {"valuer_name": "Jane", "parcel": "P1", "tag": "Queue"}}, f)
        with open(self.closed_file, "w") as f:
            json.dump([{"ref": "C1", "closed_reason": "completed", "parcel": "", "tag": None}], f)

        store = dlv_core._load_consolidated()

        self.assertEqual(store["C1"]["status"], "completed")
        self.assertEqual(store["C1"]["parcel"], "P1")
        self.assertEqual(store["C1"]["tag"], "Queue")

    def test_migration_attaches_hold_onto_an_existing_assigned_record(self):
        with open(self.assign_file, "w") as f:
            json.dump({"A1": {"valuer_name": "Jane", "valuer_uid": "u1"}}, f)
        with open(self.hold_file, "w") as f:
            json.dump([{"ref": "A1", "held_valuer_name": "Jane", "held_valuer_uid": "u1"}], f)

        store = dlv_core._load_consolidated()

        self.assertEqual(store["A1"]["status"], "assigned")
        self.assertEqual(store["A1"]["hold"]["held_valuer_name"], "Jane")

    def test_migration_creates_a_bare_record_for_a_hold_only_ref(self):
        """A ref held straight from a live DLV query, with no other legacy
        file ever mentioning it, still needs a store record to exist."""
        with open(self.hold_file, "w") as f:
            json.dump([{"ref": "H1", "held_valuer_name": "Jane", "held_valuer_uid": "u1"}], f)

        store = dlv_core._load_consolidated()

        self.assertEqual(store["H1"]["status"], "assigned")
        self.assertEqual(store["H1"]["hold"]["held_valuer_uid"], "u1")
        self.assertTrue(os.path.exists(self.hold_file))

    def test_full_four_file_migration_integration(self):
        """All four legacy files at once — each ref lands with the right
        status and its hold (if any) attached, and none of the four are
        modified on disk."""
        with open(self.assign_file, "w") as f:
            json.dump({"A1": {"valuer_name": "Alice", "valuer_uid": "u1"}}, f)
        with open(self.batch_file, "w") as f:
            json.dump([{"ref": "Q1", "valuer_name": "Bob", "valuer_uid": "u2"}], f)
        with open(self.closed_file, "w") as f:
            json.dump([{"ref": "C1", "valuer_name": "Carl", "closed_reason": "returned"}], f)
        with open(self.hold_file, "w") as f:
            json.dump([{"ref": "A1", "held_valuer_name": "Alice", "held_valuer_uid": "u1"}], f)

        store = dlv_core._load_consolidated()

        self.assertEqual(store["A1"]["status"], "assigned")
        self.assertEqual(store["A1"]["hold"]["held_valuer_name"], "Alice")
        self.assertEqual(store["Q1"]["status"], "queued")
        self.assertIsNone(store["Q1"].get("hold"))
        self.assertEqual(store["C1"]["status"], "returned")
        for path in (self.assign_file, self.batch_file, self.closed_file, self.hold_file):
            self.assertTrue(os.path.exists(path))


class TestDlvTags(unittest.TestCase):
    """DLV_TAGS — the fixed tag vocabulary shared by DLV Batch (sets it) and
    DLV Tasks' By Tag report (filters on it)."""

    def test_is_a_non_empty_list_of_distinct_strings(self):
        self.assertIsInstance(dlv_core.DLV_TAGS, list)
        self.assertTrue(dlv_core.DLV_TAGS)
        self.assertEqual(len(dlv_core.DLV_TAGS), len(set(dlv_core.DLV_TAGS)))
        self.assertTrue(all(isinstance(t, str) and t for t in dlv_core.DLV_TAGS))


class TestParseIncrementalTag(unittest.TestCase):
    """parse_incremental_tag/is_incremental_tag — shared here (not
    dlv_incremental.py or dlv_tasks.py) since both of those modules need
    them and dlv_incremental.py already imports from dlv_tasks.py."""

    def test_valid_tag_parses(self):
        self.assertEqual(dlv_core.parse_incremental_tag("B2-T3"), (2, 3))

    def test_fixed_tag_returns_none(self):
        self.assertIsNone(dlv_core.parse_incremental_tag("Queue"))

    def test_empty_or_none_returns_none(self):
        self.assertIsNone(dlv_core.parse_incremental_tag(""))
        self.assertIsNone(dlv_core.parse_incremental_tag(None))

    def test_is_incremental_tag_true_for_valid_tag(self):
        self.assertTrue(dlv_core.is_incremental_tag("B9-T1"))

    def test_is_incremental_tag_false_for_fixed_tag(self):
        self.assertFalse(dlv_core.is_incremental_tag("Direct"))
        self.assertFalse(dlv_core.is_incremental_tag(""))


if __name__ == "__main__":
    unittest.main()
