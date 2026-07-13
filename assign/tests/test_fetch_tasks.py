#!/usr/bin/env python3
"""
Unit tests for fetch_tasks.py — _has_stamp_duty_invoice, _load_fetch_tasks'
HQ/County status filtering, and _ft_do_fetch's filter pipeline (county/
registry/amount/sectional/already-queued exclusion).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_tasks as ft
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


def _run(coro):
    return asyncio.run(coro)


class TestHasStampDutyInvoice(unittest.TestCase):
    def test_true_when_stamp_duty_present(self):
        self.assertTrue(ft._has_stamp_duty_invoice([{"payment_for": "Stamp Duty"}]))

    def test_case_insensitive(self):
        self.assertTrue(ft._has_stamp_duty_invoice([{"payment_for": "STAMP duty fee"}]))

    def test_false_when_absent(self):
        self.assertFalse(ft._has_stamp_duty_invoice([{"payment_for": "Registration"}]))

    def test_false_for_empty_list(self):
        self.assertFalse(ft._has_stamp_duty_invoice([]))


class TestLoadFetchTasks(unittest.TestCase):
    def _patches(self, hq_candidates=None, county_candidates=None, hq_2a=None, hq_2b=None, county_detail=None):
        return [
            patch.object(ft, "build_session", return_value=MagicMock()),
            patch.object(ft, "_fetch_hq_list", return_value=hq_candidates or []),
            patch.object(ft, "_fetch_county_list", return_value=county_candidates or []),
            patch.object(ft, "_fetch_hq_detail_2a", return_value=hq_2a),
            patch.object(ft, "_fetch_hq_detail_2b", return_value=hq_2b),
            patch.object(ft, "_fetch_county_detail", return_value=county_detail),
            patch.object(ft, "_log_fetch_tasks"),
        ]

    def test_hq_task_kept_when_ongoing_and_sent_to_collector(self):
        hq_candidates = [{"id": "1", "application_id": "app-1", "reference_number": "R1",
                           "date_created": "2026-07-10", "parcel_number": "P1"}]
        hq_2a = {"stamp_duty_status": "SENT_TO_COLLECTOR", "application_status": "ongoing",
                 "invoices": [], "county": "NAIROBI", "registry": "NAIROBI"}
        hq_2b = {"details": {"officers": [{"names": "JANE", "role": "ASSESSOR_OF_STAMP_DUTY"}]}}
        patches = self._patches(hq_candidates=hq_candidates, hq_2a=hq_2a, hq_2b=hq_2b)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            tasks, stats = ft._load_fetch_tasks(TOKENS, 5)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["source"], "HQ")
        self.assertEqual(tasks[0]["assessor"], "JANE")
        self.assertEqual(stats["hq_kept"], 1)

    def test_hq_task_skipped_when_already_has_stamp_duty_invoice(self):
        hq_candidates = [{"id": "1", "application_id": "app-1", "reference_number": "R1",
                           "date_created": "2026-07-10"}]
        hq_2a = {"stamp_duty_status": "SENT_TO_COLLECTOR", "application_status": "ongoing",
                 "invoices": [{"payment_for": "stamp duty"}]}
        patches = self._patches(hq_candidates=hq_candidates, hq_2a=hq_2a, hq_2b={})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            tasks, stats = ft._load_fetch_tasks(TOKENS, 5)
        self.assertEqual(tasks, [])
        self.assertEqual(stats["hq_kept"], 0)

    def test_hq_task_skipped_when_not_sent_to_collector(self):
        hq_candidates = [{"id": "1", "application_id": "app-1", "reference_number": "R1",
                           "date_created": "2026-07-10"}]
        hq_2a = {"stamp_duty_status": "PENDING", "application_status": "ongoing", "invoices": []}
        patches = self._patches(hq_candidates=hq_candidates, hq_2a=hq_2a, hq_2b={})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            tasks, stats = ft._load_fetch_tasks(TOKENS, 5)
        self.assertEqual(tasks, [])

    def test_county_task_kept_when_node_matches(self):
        county_candidates = [{"id": "1", "reference_number": "R2", "date_created": "2026-07-10"}]
        county_detail = {"details": {
            "node": "STAMP_DUTY_PAYMENT_DEFINITION", "application_status": "ONGOING",
            "reference_number": "R2", "county": "MOMBASA", "registry": "MOMBASA",
            "external_process_details": {"consideration_amount": "500000", "currency_code": "KES",
                                          "invoice": [], "process_type": "TRANSFER", "parcel_number": "P2"},
            "officers": [],
        }}
        patches = self._patches(county_candidates=county_candidates, county_detail=county_detail)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            tasks, stats = ft._load_fetch_tasks(TOKENS, 5)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["source"], "County")
        self.assertEqual(stats["county_kept"], 1)

    def test_county_task_skipped_when_wrong_node(self):
        county_candidates = [{"id": "1", "reference_number": "R2", "date_created": "2026-07-10"}]
        county_detail = {"details": {"node": "SOMETHING_ELSE", "application_status": "ONGOING"}}
        patches = self._patches(county_candidates=county_candidates, county_detail=county_detail)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            tasks, stats = ft._load_fetch_tasks(TOKENS, 5)
        self.assertEqual(tasks, [])

    def test_empty_candidates_returns_empty(self):
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            tasks, stats = ft._load_fetch_tasks(TOKENS, 5)
        self.assertEqual(tasks, [])
        self.assertEqual(stats["hq_raw"], 0)
        self.assertEqual(stats["county_raw"], 0)


class TestFtDoFetch(unittest.TestCase):
    def setUp(self):
        self.message = MagicMock()
        self.message.reply_text = AsyncMock()
        self.ctx = MagicMock()
        self.sess = ft.FTSession(tokens=TOKENS, days_back=5)

    def _base_task(self, **overrides):
        task = {
            "source": "HQ", "reference_number": "REG/TSFR/ABC123", "date_created": "2026-07-10",
            "county": "Nairobi", "registry": "Central", "consideration": "2000000",
            "consideration_type": "SALE", "currency_code": "KES", "parcel_number": "NAIROBI/BLOCK1/1",
            "officers": [], "assessor": "",
        }
        task.update(overrides)
        return task

    def test_fetch_failure_replies_error(self):
        with patch.object(ft, "_load_fetch_tasks", side_effect=RuntimeError("down")):
            result = _run(ft._ft_do_fetch(self.message, self.ctx, self.sess))
        self.assertEqual(result, ft.ConversationHandler.END)
        self.assertIn("Fetch failed", self.message.reply_text.call_args[0][0])

    def test_county_filter_excludes_non_matching(self):
        tasks = [self._base_task(county="Nairobi"), self._base_task(county="Mombasa", reference_number="R2")]
        self.sess.county_filter = "nairobi"
        with patch.object(ft, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(ft, "load_dlv_batch", return_value=[]), \
             patch.object(ft, "_ft_show_results", new_callable=AsyncMock) as mock_show:
            _run(ft._ft_do_fetch(self.message, self.ctx, self.sess))
        shown = mock_show.call_args[0][1]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["county"], "Nairobi")

    def test_amount_filter_excludes_out_of_range(self):
        tasks = [self._base_task(consideration="2000000"), self._base_task(consideration="50000000", reference_number="R2")]
        self.sess.amount_min, self.sess.amount_max = 0.0, 5_000_000.0
        with patch.object(ft, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(ft, "load_dlv_batch", return_value=[]), \
             patch.object(ft, "_ft_show_results", new_callable=AsyncMock) as mock_show:
            _run(ft._ft_do_fetch(self.message, self.ctx, self.sess))
        shown = mock_show.call_args[0][1]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["consideration"], "2000000")

    def test_sectional_exclude_filters_out_sectional_parcels(self):
        tasks = [
            self._base_task(parcel_number="NAIROBI/BLOCK1/1"),
            self._base_task(parcel_number="NAIROBI/BLOCK1/1/888", reference_number="R2"),
        ]
        self.sess.sectional_filter = "exclude"
        with patch.object(ft, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(ft, "load_dlv_batch", return_value=[]), \
             patch.object(ft, "_ft_show_results", new_callable=AsyncMock) as mock_show:
            _run(ft._ft_do_fetch(self.message, self.ctx, self.sess))
        shown = mock_show.call_args[0][1]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["reference_number"], "REG/TSFR/ABC123")

    def test_already_queued_refs_are_excluded(self):
        tasks = [self._base_task(reference_number="REG/TSFR/ABC123")]
        with patch.object(ft, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(ft, "load_dlv_batch", return_value=[{"ref": "REG/TSFR/ABC123"}]):
            result = _run(ft._ft_do_fetch(self.message, self.ctx, self.sess))
        self.assertEqual(result, ft.ConversationHandler.END)
        self.assertIn("already in the DLV queue", self.message.reply_text.call_args[0][0])

    def test_no_tasks_after_filters_reports_empty(self):
        with patch.object(ft, "_load_fetch_tasks", return_value=([], {"hq_raw": 0, "county_raw": 0})):
            result = _run(ft._ft_do_fetch(self.message, self.ctx, self.sess))
        self.assertEqual(result, ft.ConversationHandler.END)
        self.assertIn("No qualifying tasks", self.message.reply_text.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
