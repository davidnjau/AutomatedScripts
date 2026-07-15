#!/usr/bin/env python3
"""
Unit tests for auto_fetch.py — schedule/results persistence, the
_auto_fetch_job pipeline (filters, sectional auto-routing, chunked
notify, email), and the bundled AF Results viewer.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_fetch as af
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


def _run(coro):
    return asyncio.run(coro)


def _task(ref="REG/TSFR/ABC123", **overrides):
    t = {
        "reference_number": ref, "source": "HQ", "county": "Nairobi", "registry": "Central",
        "date_created": "2026-07-10", "consideration": "2000000", "parcel_number": "NAIROBI/BLOCK1/1",
        "assessor": "Jane Doe",
    }
    t.update(overrides)
    return t


class TestSchedulePersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sched_file = os.path.join(self.tmpdir.name, "saved_auto_fetch.json")
        self._patch = patch.object(af, "SAVED_AUTO_FETCH_FILE", self.sched_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(af.load_auto_fetch_schedule())

    def test_save_then_load_roundtrip(self):
        af.save_auto_fetch_schedule({"interval_minutes": 60, "email": "a@b.com"})
        cfg = af.load_auto_fetch_schedule()
        self.assertEqual(cfg["interval_minutes"], 60)

    def test_clear_removes_file(self):
        af.save_auto_fetch_schedule({"interval_minutes": 60})
        af.clear_auto_fetch_schedule()
        self.assertIsNone(af.load_auto_fetch_schedule())

    def test_clear_missing_file_is_noop(self):
        af.clear_auto_fetch_schedule()  # should not raise


class TestAfResultsPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.results_file = os.path.join(self.tmpdir.name, "saved_af_results.json")
        self._patch = patch.object(af, "SAVED_AF_RESULTS_FILE", self.results_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(af.load_af_results(), [])

    def test_persist_stores_run_with_filters_and_tasks(self):
        cfg = {"county_filter": "nairobi", "amount_min": 1000, "days_back": 3}
        af.persist_af_result("r1", "2026-07-10 10:00:00", [_task()], cfg)
        results = af.load_af_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["run_id"], "r1")
        self.assertEqual(results[0]["count"], 1)
        self.assertEqual(results[0]["filters"]["county"], "nairobi")

    def test_persist_trims_to_keep_limit(self):
        for i in range(af._AF_RESULTS_KEEP + 5):
            af.persist_af_result(f"r{i}", "2026-07-10 10:00:00", [], {})
        results = af.load_af_results()
        self.assertEqual(len(results), af._AF_RESULTS_KEEP)
        # oldest runs should have been dropped, newest kept
        self.assertEqual(results[-1]["run_id"], f"r{af._AF_RESULTS_KEEP + 4}")


class TestAutoFetchJob(unittest.TestCase):
    def setUp(self):
        self.context = MagicMock()
        self.context.bot.send_message = AsyncMock()

    def test_no_schedule_returns_early(self):
        with patch.object(af, "load_auto_fetch_schedule", return_value=None), \
             patch.object(af, "_any_valid_tokens") as mock_tokens:
            _run(af._auto_fetch_job(self.context))
        mock_tokens.assert_not_called()

    def test_no_tokens_logs_and_returns(self):
        with patch.object(af, "load_auto_fetch_schedule", return_value={"days_back": 2}), \
             patch.object(af, "_any_valid_tokens", return_value=None), \
             patch.object(af, "_load_fetch_tasks") as mock_fetch:
            _run(af._auto_fetch_job(self.context))
        mock_fetch.assert_not_called()

    def test_fetch_failure_returns_without_persisting(self):
        cfg = {"days_back": 2}
        with patch.object(af, "load_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", side_effect=RuntimeError("down")), \
             patch.object(af, "persist_af_result") as mock_persist:
            _run(af._auto_fetch_job(self.context))
        mock_persist.assert_not_called()

    def test_county_filter_excludes_non_matching_and_persists(self):
        cfg = {"days_back": 2, "county_filter": "nairobi"}
        tasks = [_task(county="Nairobi"), _task(ref="R2", county="Mombasa")]
        with patch.object(af, "load_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result") as mock_persist, \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock) as mock_send:
            _run(af._auto_fetch_job(self.context))
        persisted_tasks = mock_persist.call_args[0][2]
        self.assertEqual(len(persisted_tasks), 1)
        self.assertEqual(persisted_tasks[0]["county"], "Nairobi")
        mock_send.assert_called()

    def test_already_queued_refs_excluded(self):
        cfg = {"days_back": 2}
        tasks = [_task(ref="REG/TSFR/ABC123")]
        with patch.object(af, "load_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[{"ref": "REG/TSFR/ABC123"}]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result") as mock_persist:
            _run(af._auto_fetch_job(self.context))
        persisted_tasks = mock_persist.call_args[0][2]
        self.assertEqual(persisted_tasks, [])

    def test_no_tasks_after_filters_does_not_send(self):
        cfg = {"days_back": 2, "county_filter": "mombasa"}
        tasks = [_task(county="Nairobi")]
        with patch.object(af, "load_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock) as mock_send:
            _run(af._auto_fetch_job(self.context))
        mock_send.assert_not_called()

    def test_email_sent_when_configured(self):
        cfg = {"days_back": 2, "email": "ops@example.com"}
        tasks = [_task()]
        with patch.object(af, "load_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", set()), \
             patch.object(af, "_send_auto_fetch_email") as mock_email:
            _run(af._auto_fetch_job(self.context))
        mock_email.assert_called_once()
        self.assertEqual(mock_email.call_args[0][0], "ops@example.com")

    def test_email_failure_notifies_telegram_instead_of_failing_silently(self):
        """Regression test: an SMTP error used to be swallowed by a log line only,
        so a broken server-side email config looked identical to success."""
        cfg = {"days_back": 2, "email": "ops@example.com"}
        tasks = [_task()]
        with patch.object(af, "load_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", {111}), \
             patch.object(af, "_send_auto_fetch_email", side_effect=RuntimeError("smtp down")):
            _run(af._auto_fetch_job(self.context))
        self.context.bot.send_message.assert_any_call(
            111, "⚠️ Auto Fetch email delivery to *ops@example.com* failed: `smtp down`",
            parse_mode="Markdown",
        )

    def test_sectional_auto_routes_when_specialist_configured(self):
        # sectional_filter="all" so the earlier main filter step doesn't
        # strip the sectional task before auto-routing gets a chance to see it
        cfg = {"days_back": 2, "sectional_filter": "all"}
        sectional_task = _task(ref="R-SEC", parcel_number="NAIROBI/BLOCK1/1/888")
        sc_cfg = {
            "auto_route": True, "cred_type": "staff2",
            "specialist": {"name": "Jane Specialist", "uid": "uid-1"},
        }
        fake_session = MagicMock()
        fake_session.put.return_value = MagicMock(status_code=200)
        with patch.object(af, "load_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=([sectional_task], {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=sc_cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "build_session", return_value=fake_session), \
             patch.object(af, "persist_assignment") as mock_persist_assign, \
             patch.object(af, "persist_af_result") as mock_persist_result, \
             patch.object(af, "ALLOWED_IDS", set()):
            _run(af._auto_fetch_job(self.context))
        mock_persist_assign.assert_called_once_with("R-SEC", "Jane Specialist", "uid-1")
        # sectional task should be removed from the remaining/persisted set
        persisted_tasks = mock_persist_result.call_args[0][2]
        self.assertEqual(persisted_tasks, [])


class TestCmdAfResults(unittest.TestCase):
    def test_no_results_shows_empty_message(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        with patch.object(af, "allowed", return_value=True), \
             patch.object(af, "load_af_results", return_value=[]):
            _run(af.cmd_af_results(update, ctx))
        self.assertIn("No Auto Fetch runs", update.message.reply_text.call_args[0][0])

    def test_results_shown_as_buttons(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        results = [{"run_id": "r1", "run_at": "2026-07-10 10:00:00", "count": 3}]
        with patch.object(af, "allowed", return_value=True), \
             patch.object(af, "load_af_results", return_value=results):
            _run(af.cmd_af_results(update, ctx))
        update.message.reply_text.assert_called_once()


class TestRecvAfResultDetail(unittest.TestCase):
    def _make_query(self, run_id):
        update = MagicMock()
        query = update.callback_query
        query.data = f"af_result:{run_id}"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message.reply_text = AsyncMock()
        return update

    def test_run_not_found(self):
        update = self._make_query("missing")
        ctx = MagicMock()
        with patch.object(af, "load_af_results", return_value=[]):
            _run(af.recv_af_result_detail(update, ctx))
        update.callback_query.edit_message_text.assert_called_once()
        self.assertIn("not found", update.callback_query.edit_message_text.call_args[0][0])

    def test_empty_run_shows_no_tasks_message(self):
        update = self._make_query("r1")
        ctx = MagicMock()
        run = {"run_id": "r1", "run_at": "t", "count": 0, "tasks": [], "filters": {}}
        with patch.object(af, "load_af_results", return_value=[run]):
            _run(af.recv_af_result_detail(update, ctx))
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("No tasks found", text)

    def test_run_with_tasks_sends_chunked_report(self):
        update = self._make_query("r1")
        ctx = MagicMock()
        run = {
            "run_id": "r1", "run_at": "t", "count": 1,
            "tasks": [{"ref": "REG/TSFR/X", "parcel": "P1", "county": "NAIROBI",
                       "registry": "CENTRAL", "date_created": "2026-07-10",
                       "consideration": "1000", "assessor": "Jane"}],
            "filters": {},
        }
        with patch.object(af, "load_af_results", return_value=[run]), \
             patch.object(af, "load_saved_assignments", return_value={}):
            _run(af.recv_af_result_detail(update, ctx))
        update.callback_query.message.reply_text.assert_called_once()


class TestRegisterRestoresSchedule(unittest.TestCase):
    """register() restores a saved Auto Fetch schedule on startup — regression
    test for a bug where the first run after every restart waited a full
    interval, starving the job if restarts happened more often than that."""

    async def _register(self, app):
        # ConversationHandler's construction needs a running event loop
        # (asyncio.Lock() binds to it), so register() must run inside one.
        af.register(app)

    def test_restored_first_run_is_soon_not_a_full_interval(self):
        app = MagicMock()
        cfg = {"interval_minutes": 120}
        with patch.object(af, "load_auto_fetch_schedule", return_value=cfg):
            _run(self._register(app))
        _, kwargs = app.job_queue.run_repeating.call_args
        self.assertEqual(kwargs["interval"], 120 * 60)
        self.assertEqual(kwargs["first"], af._AF_RESTORE_FIRST_RUN_DELAY)
        self.assertLess(af._AF_RESTORE_FIRST_RUN_DELAY, 120 * 60)

    def test_no_saved_schedule_does_not_register_job(self):
        app = MagicMock()
        with patch.object(af, "load_auto_fetch_schedule", return_value=None):
            _run(self._register(app))
        app.job_queue.run_repeating.assert_not_called()


if __name__ == "__main__":
    unittest.main()
