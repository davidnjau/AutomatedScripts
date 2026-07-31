#!/usr/bin/env python3
"""
Unit tests for token_refresh_daemon.py — the bounded retry/backoff on a
failed refresh and the Telegram alert sent once retries are exhausted.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import token_refresh_daemon as trd


class TokenRefreshDaemonTestCase(unittest.TestCase):
    """Resets the module's in-memory job-tracking state around every test —
    _refresh_job mutates these globals directly, so leftover state from one
    test would otherwise leak into the next."""

    def setUp(self):
        trd._scheduled.clear()
        trd._failed.clear()
        trd._retry_count.clear()
        for job in trd.scheduler.get_jobs():
            trd.scheduler.remove_job(job.id)

    def tearDown(self):
        for job in trd.scheduler.get_jobs():
            trd.scheduler.remove_job(job.id)


class TestRefreshJobSuccess(TokenRefreshDaemonTestCase):
    def test_success_clears_failure_state_and_reschedules(self):
        trd._retry_count["staff2"] = 2  # simulate a prior failed attempt
        cache = {"staff2": {"access_token": "old_at", "jwt": "old_jwt", "refresh_token": "old_rt"}}
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_save_cache") as mock_save, \
             patch.object(trd, "_try_refresh_token", return_value=("new_at", "new_jwt", "new_rt")), \
             patch.object(trd, "_decode_exp", return_value=None):
            trd._refresh_job("staff2")
        self.assertNotIn("staff2", trd._retry_count)
        self.assertNotIn("staff2", trd._failed)
        mock_save.assert_called_once()
        self.assertIsNotNone(trd.scheduler.get_job("refresh_staff2"))


class TestRefreshJobFailureRetries(TokenRefreshDaemonTestCase):
    def test_first_failure_schedules_a_retry_without_giving_up(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_try_refresh_token", return_value=None), \
             patch.object(trd, "_tg_broadcast") as mock_broadcast:
            trd._refresh_job("staff2")
        self.assertEqual(trd._retry_count.get("staff2"), 1)
        self.assertNotIn("staff2", trd._failed)
        mock_broadcast.assert_not_called()
        self.assertIsNotNone(trd.scheduler.get_job("refresh_staff2"))

    def test_repeated_failures_up_to_retry_max_do_not_give_up(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_try_refresh_token", return_value=None), \
             patch.object(trd, "_tg_broadcast") as mock_broadcast:
            for _ in range(trd.RETRY_MAX):
                trd._refresh_job("staff2")
        self.assertEqual(trd._retry_count.get("staff2"), trd.RETRY_MAX)
        self.assertNotIn("staff2", trd._failed)
        mock_broadcast.assert_not_called()

    def test_exhausting_retries_marks_failed_and_alerts(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_try_refresh_token", return_value=None), \
             patch.object(trd, "_tg_broadcast") as mock_broadcast:
            for _ in range(trd.RETRY_MAX + 1):
                trd._refresh_job("staff2")
        self.assertIn("staff2", trd._failed)
        self.assertNotIn("staff2", trd._retry_count)
        mock_broadcast.assert_called_once()
        alert_text = mock_broadcast.call_args[0][0]
        self.assertIn("staff2", alert_text)
        self.assertIn("Refresh Auth", alert_text)

    def test_success_after_some_failures_resets_retry_count(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_try_refresh_token", return_value=None):
            trd._refresh_job("staff2")
            trd._refresh_job("staff2")
        self.assertEqual(trd._retry_count.get("staff2"), 2)

        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_save_cache"), \
             patch.object(trd, "_try_refresh_token", return_value=("new_at", "new_jwt", "new_rt")), \
             patch.object(trd, "_decode_exp", return_value=None):
            trd._refresh_job("staff2")
        self.assertNotIn("staff2", trd._retry_count)
        self.assertNotIn("staff2", trd._failed)


class TestRefreshJobMissingCache(TokenRefreshDaemonTestCase):
    def test_no_token_entry_is_a_no_op_not_a_failure(self):
        with patch.object(trd, "_load_cache", return_value={}), \
             patch.object(trd, "_tg_broadcast") as mock_broadcast:
            trd._refresh_job("staff2")
        self.assertNotIn("staff2", trd._failed)
        self.assertNotIn("staff2", trd._retry_count)
        mock_broadcast.assert_not_called()


if __name__ == "__main__":
    unittest.main()
