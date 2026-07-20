#!/usr/bin/env python3
"""
Unit tests for enrich_dlv_records.py — _needs_enrichment's missing-field
check, _select_targets' ref/status/--all filtering, and _lookup_context's
County (assessor-stage-then-DLV-fallback) vs non-County search routing.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import argparse
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import enrich_dlv_records as edr
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


def _args(**overrides):
    defaults = {"ref": None, "include_closed": False, "all": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestNeedsEnrichment(unittest.TestCase):
    def test_missing_field_needs_enrichment(self):
        self.assertTrue(edr._needs_enrichment({"registry": "NAIROBI"}))

    def test_blank_field_needs_enrichment(self):
        self.assertTrue(edr._needs_enrichment({
            "registry": "NAIROBI", "county": "NAIROBI", "parcel": "", "consideration": "1000",
        }))

    def test_all_fields_present_does_not_need_enrichment(self):
        self.assertFalse(edr._needs_enrichment({
            "registry": "NAIROBI", "county": "NAIROBI", "parcel": "P1", "consideration": "1000",
        }))


class TestSelectTargets(unittest.TestCase):
    def test_ref_argument_targets_only_that_ref(self):
        store = {"A": {"status": "queued"}, "B": {"status": "assigned"}}
        self.assertEqual(edr._select_targets(store, _args(ref="A")), ["A"])

    def test_missing_ref_argument_exits(self):
        store = {"A": {"status": "queued"}}
        with self.assertRaises(SystemExit):
            edr._select_targets(store, _args(ref="NOT_THERE"))

    def test_default_targets_only_queued_and_assigned_needing_enrichment(self):
        store = {
            "Q1": {"status": "queued"},                                                   # missing fields -> targeted
            "A1": {"status": "assigned", "registry": "R", "county": "C", "parcel": "P", "consideration": "1"},  # complete -> skipped
            "C1": {"status": "completed"},                                                 # closed -> skipped by default
            "R1": {"status": "removed"},                                                   # removed -> never targeted
        }
        self.assertEqual(edr._select_targets(store, _args()), ["Q1"])

    def test_include_closed_adds_completed_and_returned(self):
        store = {
            "C1": {"status": "completed"},
            "R1": {"status": "returned"},
            "X1": {"status": "removed"},
        }
        targets = set(edr._select_targets(store, _args(include_closed=True)))
        self.assertEqual(targets, {"C1", "R1"})

    def test_all_flag_targets_even_already_complete_records(self):
        store = {"A1": {"status": "assigned", "registry": "R", "county": "C", "parcel": "P", "consideration": "1"}}
        self.assertEqual(edr._select_targets(store, _args(all=True)), ["A1"])


class TestEnrichOne(unittest.TestCase):
    """_enrich_one — the per-ref merge decision. Specifically the
    regression where a live lookup finding nothing but currency_code's
    "KES" default used to get merged in as an empty-field placeholder,
    making a later re-check wrongly report "already up to date" instead
    of "still nothing found."""

    def test_currency_code_default_alone_does_not_count_as_found(self):
        store = {"REF1": {"ref": "REF1", "status": "assigned"}}
        with patch.object(edr, "_lookup_context", return_value={
            "registry": "", "county": "", "parcel": "", "consideration": "", "currency_code": "KES",
        }):
            outcome, changed = edr._enrich_one("REF1", store)
        self.assertEqual(outcome, "not_found")
        self.assertEqual(changed, [])
        # nothing was written into the record
        self.assertNotIn("registry", store["REF1"])
        self.assertNotIn("currency_code", store["REF1"])

    def test_real_data_found_enriches_and_writes_to_store(self):
        store = {"REF1": {"ref": "REF1", "status": "assigned"}}
        with patch.object(edr, "_lookup_context", return_value={
            "registry": "NAIROBI", "county": "NAIROBI", "parcel": "P1",
            "consideration": "1000000", "currency_code": "KES",
        }):
            outcome, changed = edr._enrich_one("REF1", store)
        self.assertEqual(outcome, "enriched")
        self.assertIn("parcel", changed)
        self.assertEqual(store["REF1"]["parcel"], "P1")
        self.assertEqual(store["REF1"]["currency_code"], "KES")

    def test_second_run_with_same_empty_result_still_reports_not_found(self):
        """The actual bug: a first run that found nothing must not poison
        the record so a second run misreports "already up to date."""
        store = {"REF1": {"ref": "REF1", "status": "assigned"}}
        with patch.object(edr, "_lookup_context", return_value={
            "registry": "", "county": "", "parcel": "", "consideration": "", "currency_code": "KES",
        }):
            first = edr._enrich_one("REF1", store)
            second = edr._enrich_one("REF1", store)
        self.assertEqual(first[0], "not_found")
        self.assertEqual(second[0], "not_found")

    def test_partial_real_data_does_not_merge_the_still_empty_fields(self):
        store = {"REF1": {"ref": "REF1", "status": "assigned"}}
        with patch.object(edr, "_lookup_context", return_value={
            "registry": "NAIROBI", "county": "", "parcel": "", "consideration": "", "currency_code": "KES",
        }):
            outcome, changed = edr._enrich_one("REF1", store)
        self.assertEqual(outcome, "enriched")
        self.assertEqual(changed, ["registry", "currency_code"])
        self.assertNotIn("county", store["REF1"])
        self.assertNotIn("parcel", store["REF1"])

    def test_already_fully_enriched_ref_reports_up_to_date(self):
        store = {"REF1": {
            "ref": "REF1", "status": "assigned",
            "registry": "NAIROBI", "county": "NAIROBI", "parcel": "P1",
            "consideration": "1000000", "currency_code": "KES",
        }}
        with patch.object(edr, "_lookup_context", return_value={
            "registry": "NAIROBI", "county": "NAIROBI", "parcel": "P1",
            "consideration": "1000000", "currency_code": "KES",
        }):
            outcome, changed = edr._enrich_one("REF1", store)
        self.assertEqual(outcome, "up_to_date")
        self.assertEqual(changed, [])


class TestLookupContext(unittest.TestCase):
    def test_non_county_uses_staff_valuer(self):
        item = {"id": "app-1"}
        with patch.object(edr, "get_valid_tokens", return_value=TOKENS) as mock_tokens, \
             patch.object(edr.lu, "_lu_search_ref", return_value=item) as mock_search, \
             patch.object(edr.lu, "_lu_fetch_detail", return_value=None), \
             patch.object(edr.lu, "_lu_extract_context", return_value={"registry": "NAIROBI"}) as mock_ctx:
            result = edr._lookup_context("REG/TSFR/ABC123")
        mock_tokens.assert_called_once_with(edr.lu._LU_CRED_DEFAULT)
        mock_search.assert_called_once_with(TOKENS, "REG/TSFR/ABC123")
        mock_ctx.assert_called_once()
        self.assertEqual(result, {"registry": "NAIROBI"})

    def test_non_county_no_tokens_returns_empty(self):
        with patch.object(edr, "get_valid_tokens", return_value=None), \
             patch.object(edr.lu, "_lu_search_ref") as mock_search:
            result = edr._lookup_context("REG/TSFR/ABC123")
        self.assertEqual(result, {})
        mock_search.assert_not_called()

    def test_non_county_not_found_returns_empty(self):
        with patch.object(edr, "get_valid_tokens", return_value=TOKENS), \
             patch.object(edr.lu, "_lu_search_ref", return_value=None):
            result = edr._lookup_context("REG/TSFR/ABC123")
        self.assertEqual(result, {})

    def test_county_tries_assessor_stage_first(self):
        item = {"id": "app-2"}
        with patch.object(edr, "get_valid_tokens", return_value=TOKENS), \
             patch.object(edr.lu, "_lu_search_ref_county", return_value=item) as mock_search_county, \
             patch.object(edr.lu, "_lu_fetch_detail_county", return_value=None), \
             patch.object(edr.lu, "_lu_extract_context", return_value={"parcel": "P1"}), \
             patch.object(edr.lu, "_lu_search_ref_county_dlv") as mock_search_dlv:
            result = edr._lookup_context("CNTYINV/AB12CD34EF")
        mock_search_county.assert_called_once_with(TOKENS, "CNTYINV/AB12CD34EF")
        mock_search_dlv.assert_not_called()
        self.assertEqual(result, {"parcel": "P1"})

    def test_county_falls_back_to_dlv_stage_when_assessor_stage_finds_nothing(self):
        item = {"id": "app-3"}
        with patch.object(edr, "get_valid_tokens", return_value=TOKENS), \
             patch.object(edr.lu, "_lu_search_ref_county", return_value=None) as mock_search_county, \
             patch.object(edr.lu, "_lu_search_ref_county_dlv", return_value=item) as mock_search_dlv, \
             patch.object(edr.lu, "_lu_fetch_detail", return_value=None), \
             patch.object(edr.lu, "_lu_extract_context", return_value={"consideration": "5000"}):
            result = edr._lookup_context("CNTYINV/AB12CD34EF")
        mock_search_county.assert_called_once()
        mock_search_dlv.assert_called_once_with(TOKENS, "CNTYINV/AB12CD34EF")
        self.assertEqual(result, {"consideration": "5000"})

    def test_county_no_tokens_for_either_stage_returns_empty(self):
        with patch.object(edr, "get_valid_tokens", return_value=None), \
             patch.object(edr.lu, "_lu_search_ref_county") as mock_search_county, \
             patch.object(edr.lu, "_lu_search_ref_county_dlv") as mock_search_dlv:
            result = edr._lookup_context("CNTYINV/AB12CD34EF")
        self.assertEqual(result, {})
        mock_search_county.assert_not_called()
        mock_search_dlv.assert_not_called()

    def test_county_not_found_in_either_stage_returns_empty(self):
        with patch.object(edr, "get_valid_tokens", return_value=TOKENS), \
             patch.object(edr.lu, "_lu_search_ref_county", return_value=None), \
             patch.object(edr.lu, "_lu_search_ref_county_dlv", return_value=None):
            result = edr._lookup_context("CNTYINV/AB12CD34EF")
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
