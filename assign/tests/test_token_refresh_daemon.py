#!/usr/bin/env python3
"""
Unit tests for token_refresh_daemon.py — the bounded retry/backoff on a
failed refresh and the Telegram alert sent once retries are exhausted.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import token_refresh_daemon as trd


class TestOpsRecipients(unittest.TestCase):
    """Auth/token-failure alerts go to admins + ICT admins only (not every
    allowed user), with a safe fallback if neither is configured."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.roles_file = os.path.join(self.tmp.name, "saved_roles.json")
        self._p = patch.object(trd, "ROLES_FILE", self.roles_file)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self.tmp.cleanup)

    def _write_roles(self, data):
        with open(self.roles_file, "w") as f:
            json.dump(data, f)

    def test_admins_plus_ict_admins(self):
        self._write_roles({"5794776293": ["ict_admin"], "7": ["user"]})
        with patch.object(trd, "ADMIN_IDS", {5694397150}), \
             patch.object(trd, "ALLOWED_IDS", [5694397150, 5794776293, 7]), \
             patch.object(trd, "NO_REPORT_IDS", set()):
            self.assertEqual(trd._ops_recipients(), [5694397150, 5794776293])

    def test_no_roles_file_just_admins(self):
        with patch.object(trd, "ADMIN_IDS", {5694397150}), \
             patch.object(trd, "ALLOWED_IDS", [5694397150, 7]):
            self.assertEqual(trd._ops_recipients(), [5694397150])

    def test_fallback_to_allowed_when_no_admin_or_ict(self):
        self._write_roles({})
        with patch.object(trd, "ADMIN_IDS", set()), \
             patch.object(trd, "ALLOWED_IDS", [10, 20, 30]), \
             patch.object(trd, "NO_REPORT_IDS", {20}):
            self.assertEqual(trd._ops_recipients(), [10, 30])

    def test_alert_sends_only_to_ops(self):
        self._write_roles({"5794776293": ["ict_admin"]})
        with patch.object(trd, "ADMIN_IDS", {5694397150}), \
             patch.object(trd, "ALLOWED_IDS", [5694397150, 5794776293, 7]), \
             patch.object(trd, "NO_REPORT_IDS", set()), \
             patch.object(trd, "_tg_send") as mock_send:
            trd._tg_alert_ops("boom")
        sent_to = sorted(c.args[0] for c in mock_send.call_args_list)
        self.assertEqual(sent_to, [5694397150, 5794776293])


class TokenRefreshDaemonTestCase(unittest.TestCase):
    """Resets the module's in-memory job-tracking state around every test —
    _refresh_job mutates these globals directly, so leftover state from one
    test would otherwise leak into the next."""

    def setUp(self):
        trd._scheduled.clear()
        trd._failed.clear()
        trd._retry_count.clear()
        trd._failed_since.clear()
        trd._escalated.clear()
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
             patch.object(trd, "_tg_alert_ops") as mock_broadcast:
            trd._refresh_job("staff2")
        self.assertEqual(trd._retry_count.get("staff2"), 1)
        self.assertNotIn("staff2", trd._failed)
        mock_broadcast.assert_not_called()
        self.assertIsNotNone(trd.scheduler.get_job("refresh_staff2"))

    def test_repeated_failures_up_to_retry_max_do_not_give_up(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_try_refresh_token", return_value=None), \
             patch.object(trd, "_tg_alert_ops") as mock_broadcast:
            for _ in range(trd.RETRY_MAX):
                trd._refresh_job("staff2")
        self.assertEqual(trd._retry_count.get("staff2"), trd.RETRY_MAX)
        self.assertNotIn("staff2", trd._failed)
        mock_broadcast.assert_not_called()

    def test_exhausting_retries_marks_failed_and_alerts(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_try_refresh_token", return_value=None), \
             patch.object(trd, "_tg_alert_ops") as mock_broadcast:
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


class TestRefreshJobCooldownAfterGiveUp(TokenRefreshDaemonTestCase):
    """Regression coverage for the retry-storm bug: after the fast retries are
    exhausted, _refresh_job must (a) keep retrying in the background rather
    than stopping, (b) NOT re-send the alert on every subsequent failure, and
    (c) leave _scheduled pointing at the still-failing bucket so _scan_cache
    can't misread the unchanged token as "new tokens detected"."""

    def _fail_until_given_up(self, cache):
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_try_refresh_token", return_value=None), \
             patch.object(trd, "_tg_alert_ops"):
            for _ in range(trd.RETRY_MAX + 1):
                trd._refresh_job("staff2")

    def test_scheduled_bucket_is_not_cleared_on_give_up(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        trd._scheduled["staff2"] = 999   # bucket set by the preceding _schedule_refresh call
        self._fail_until_given_up(cache)
        # Must still be set (to the SAME bucket) — not popped — so _scan_cache's
        # "same bucket → no new tokens, keep waiting" check stays correct.
        self.assertEqual(trd._scheduled.get("staff2"), 999)

    def test_keeps_retrying_in_the_background_after_give_up(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        self._fail_until_given_up(cache)
        self.assertIn("staff2", trd._failed)
        job = trd.scheduler.get_job("refresh_staff2")
        self.assertIsNotNone(job)   # a cooldown retry is scheduled, not nothing

    def test_no_repeat_alert_on_subsequent_cooldown_failures(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        self._fail_until_given_up(cache)
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_try_refresh_token", return_value=None), \
             patch.object(trd, "_tg_alert_ops") as mock_alert:
            for _ in range(10):   # many more quiet background failures
                trd._refresh_job("staff2")
        mock_alert.assert_not_called()

    def test_escalation_alert_fires_once_after_threshold(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        self._fail_until_given_up(cache)
        trd._failed_since["staff2"] = time.time() - trd.ESCALATION_AFTER - 1
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_try_refresh_token", return_value=None), \
             patch.object(trd, "_tg_alert_ops") as mock_alert:
            trd._refresh_job("staff2")   # crosses the threshold -> escalate
            trd._refresh_job("staff2")   # still past threshold -> no 2nd escalation
        self.assertEqual(mock_alert.call_count, 1)
        self.assertIn("staff2", trd._escalated)

    def test_success_clears_escalation_state(self):
        cache = {"staff2": {"access_token": "at", "jwt": "jwt", "refresh_token": "rt"}}
        self._fail_until_given_up(cache)
        trd._escalated.add("staff2")
        trd._failed_since["staff2"] = time.time()
        with patch.object(trd, "_load_cache", return_value=cache), \
             patch.object(trd, "_save_cache"), \
             patch.object(trd, "_try_refresh_token", return_value=("new_at", "new_jwt", "new_rt")), \
             patch.object(trd, "_decode_exp", return_value=None):
            trd._refresh_job("staff2")
        self.assertNotIn("staff2", trd._escalated)
        self.assertNotIn("staff2", trd._failed_since)


class TestTryRefreshTokenLogging(unittest.TestCase):
    """The actual HTTP/exception reason a refresh failed must land at WARNING
    (not DEBUG, which the daemon's logger is configured above) — otherwise it
    never reaches daemon.log and a stuck credential is undiagnosable without
    live-exec'ing into the container."""

    def test_non_200_response_logs_reason_at_warning(self):
        resp = MagicMock(status_code=401, text="Unauthorized")
        with patch.object(trd, "_api_sess") as mock_sess:
            mock_sess.post.return_value = resp
            with self.assertLogs("token_refresh_daemon", level="WARNING") as cm:
                result = trd._try_refresh_token("at", "jwt", "rt")
        self.assertIsNone(result)
        self.assertTrue(any("401" in line for line in cm.output))

    def test_request_exception_logs_reason_at_warning(self):
        with patch.object(trd, "_api_sess") as mock_sess:
            mock_sess.post.side_effect = RuntimeError("connection reset")
            with self.assertLogs("token_refresh_daemon", level="WARNING") as cm:
                result = trd._try_refresh_token("at", "jwt", "rt")
        self.assertIsNone(result)
        self.assertTrue(any("connection reset" in line for line in cm.output))


class TestRefreshJobMissingCache(TokenRefreshDaemonTestCase):
    def test_no_token_entry_is_a_no_op_not_a_failure(self):
        with patch.object(trd, "_load_cache", return_value={}), \
             patch.object(trd, "_tg_alert_ops") as mock_broadcast:
            trd._refresh_job("staff2")
        self.assertNotIn("staff2", trd._failed)
        self.assertNotIn("staff2", trd._retry_count)
        mock_broadcast.assert_not_called()


if __name__ == "__main__":
    unittest.main()
