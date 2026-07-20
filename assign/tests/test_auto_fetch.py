#!/usr/bin/env python3
"""
Unit tests for auto_fetch.py — schedule/results persistence, the
_auto_fetch_job pipeline (filters, sectional auto-routing, chunked
notify, email), and the bundled AF Results viewer.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import json
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
    """load/add/get/remove_auto_fetch_schedule(s) — the multi-schedule
    persistence layer, plus migration from the old single-dict file format."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sched_file = os.path.join(self.tmpdir.name, "saved_auto_fetch.json")
        self._patch = patch.object(af, "SAVED_AUTO_FETCH_FILE", self.sched_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(af.load_auto_fetch_schedules(), [])

    def test_add_then_load_roundtrip(self):
        schedule_id = af.add_auto_fetch_schedule({"interval_minutes": 60, "email": "a@b.com"})
        schedules = af.load_auto_fetch_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["interval_minutes"], 60)
        self.assertEqual(schedules[0]["id"], schedule_id)

    def test_add_multiple_schedules_get_distinct_ids(self):
        id1 = af.add_auto_fetch_schedule({"interval_minutes": 60, "email": "abc@gmail.com"})
        id2 = af.add_auto_fetch_schedule({"interval_minutes": 120, "email": "123@gmail.com"})
        self.assertNotEqual(id1, id2)
        schedules = af.load_auto_fetch_schedules()
        self.assertEqual(len(schedules), 2)
        self.assertEqual({s["email"] for s in schedules}, {"abc@gmail.com", "123@gmail.com"})

    def test_get_auto_fetch_schedule_finds_by_id(self):
        schedule_id = af.add_auto_fetch_schedule({"interval_minutes": 60, "email": "a@b.com"})
        cfg = af.get_auto_fetch_schedule(schedule_id)
        self.assertEqual(cfg["email"], "a@b.com")

    def test_get_auto_fetch_schedule_returns_none_when_missing(self):
        self.assertIsNone(af.get_auto_fetch_schedule("no-such-id"))

    def test_remove_auto_fetch_schedule_deletes_only_that_one(self):
        id1 = af.add_auto_fetch_schedule({"interval_minutes": 60, "email": "abc@gmail.com"})
        id2 = af.add_auto_fetch_schedule({"interval_minutes": 120, "email": "123@gmail.com"})
        removed = af.remove_auto_fetch_schedule(id1)
        self.assertTrue(removed)
        remaining = af.load_auto_fetch_schedules()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], id2)

    def test_remove_missing_schedule_returns_false(self):
        self.assertFalse(af.remove_auto_fetch_schedule("no-such-id"))

    def test_legacy_single_dict_file_migrates_to_a_list(self):
        """A pre-multi-schedule deployment's saved_auto_fetch.json holds a
        bare dict, not a list — must not crash, and must self-heal on read."""
        with open(self.sched_file, "w") as f:
            json.dump({"interval_minutes": 60, "email": "legacy@example.com"}, f)

        schedules = af.load_auto_fetch_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["email"], "legacy@example.com")
        self.assertIn("id", schedules[0])

        # migration is persisted — a second load sees the already-migrated list
        with open(self.sched_file) as f:
            on_disk = json.load(f)
        self.assertIsInstance(on_disk, list)


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

    def test_persist_records_which_schedule_the_run_came_from(self):
        """With multiple schedules, runs must be distinguishable in AF Results."""
        cfg = {"id": "sched-abc", "email": "abc@gmail.com"}
        af.persist_af_result("r1", "2026-07-10 10:00:00", [], cfg)
        result = af.load_af_results()[0]
        self.assertEqual(result["schedule_id"], "sched-abc")
        self.assertEqual(result["schedule_label"], "abc@gmail.com")

    def test_persist_labels_telegram_only_when_no_email(self):
        af.persist_af_result("r1", "2026-07-10 10:00:00", [], {"id": "sched-abc"})
        result = af.load_af_results()[0]
        self.assertEqual(result["schedule_label"], "Telegram only")


class TestAfEmailStatePersistence(unittest.TestCase):
    """load/save_af_email_state — per-schedule cumulative record of every
    ref ever actually emailed, used to drop already-sent refs from later
    cycles."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmpdir.name, "saved_af_email_state.json")
        self._patch = patch.object(af, "SAVED_AF_EMAIL_STATE_FILE", self.state_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty_dict(self):
        self.assertEqual(af.load_af_email_state(), {})

    def test_save_then_load_roundtrip(self):
        af.save_af_email_state({"sched-1": ["REF1", "REF2"]})
        self.assertEqual(af.load_af_email_state(), {"sched-1": ["REF1", "REF2"]})


class TestAutoFetchJob(unittest.TestCase):
    """_auto_fetch_job runs for exactly one schedule per invocation — the id
    it operates on comes from context.job.data (see recv_af_email/register,
    which tag each schedule's repeating job with its own id)."""

    def setUp(self):
        self.context = MagicMock()
        self.context.bot.send_message = AsyncMock()
        self.context.job.data = "sched-1"
        # Isolate email-dedup state per test — a real/shared file here would
        # let one test's send leak into another's "already sent" check.
        self.tmpdir = tempfile.TemporaryDirectory()
        self.email_state_file = os.path.join(self.tmpdir.name, "saved_af_email_state.json")
        self._patch_email_state = patch.object(af, "SAVED_AF_EMAIL_STATE_FILE", self.email_state_file)
        self._patch_email_state.start()
        self.addCleanup(self._patch_email_state.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def test_schedule_removed_since_last_run_cancels_the_job(self):
        """If this schedule was deleted (via Remove Schedule) since the job
        last fired, don't just skip the cycle — cancel the job itself so it
        stops trying, instead of forever finding nothing every interval."""
        with patch.object(af, "get_auto_fetch_schedule", return_value=None), \
             patch.object(af, "get_valid_tokens") as mock_tokens:
            _run(af._auto_fetch_job(self.context))
        mock_tokens.assert_not_called()
        self.context.job.schedule_removal.assert_called_once()

    def test_no_tokens_logs_and_returns(self):
        with patch.object(af, "get_auto_fetch_schedule", return_value={"days_back": 2}), \
             patch.object(af, "get_valid_tokens", return_value=None), \
             patch.object(af, "_load_fetch_tasks") as mock_fetch:
            _run(af._auto_fetch_job(self.context))
        mock_fetch.assert_not_called()

    def test_always_fetches_the_support_credential_specifically(self):
        """Regression test: the job used to pick whichever cached credential
        _any_valid_tokens() found first (staff_valuer before staff2/Support),
        so it could silently run under a non-Support account and get back an
        empty result — even though Fetch Tasks worked fine using Support.
        The job must always request the Support credential by name."""
        cfg = {"days_back": 2}
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS) as mock_tokens, \
             patch.object(af, "_load_fetch_tasks", return_value=([], {})):
            _run(af._auto_fetch_job(self.context))
        mock_tokens.assert_called_once_with(af._AF_CRED_TYPE)
        self.assertEqual(af._AF_CRED_TYPE, "staff2")

    def test_looks_up_the_schedule_tagged_on_its_own_job(self):
        self.context.job.data = "sched-xyz"
        with patch.object(af, "get_auto_fetch_schedule", return_value={"days_back": 2}) as mock_get, \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=([], {})):
            _run(af._auto_fetch_job(self.context))
        mock_get.assert_called_once_with("sched-xyz")

    def test_fetch_failure_returns_without_persisting(self):
        cfg = {"days_back": 2}
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", side_effect=RuntimeError("down")), \
             patch.object(af, "persist_af_result") as mock_persist:
            _run(af._auto_fetch_job(self.context))
        mock_persist.assert_not_called()

    def test_county_filter_excludes_non_matching_and_persists(self):
        cfg = {"days_back": 2, "county_filter": "nairobi"}
        tasks = [_task(county="Nairobi"), _task(ref="R2", county="Mombasa")]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
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
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
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
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock) as mock_send:
            _run(af._auto_fetch_job(self.context))
        mock_send.assert_not_called()

    def test_telegram_summary_uses_shared_labeled_block_format(self):
        """The Telegram summary shares Fetch Tasks' _ft_format_task_block —
        same visual as the email body, not its own bespoke one-liner."""
        cfg = {"days_back": 2}
        tasks = [_task(ref="REG/TSFR/AAA111", county="Mombasa", registry="Coast",
                        parcel_number="MOMBASA/BLOCK5/9", assessor="John Assessor")]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "ALLOWED_IDS", {111}):
            _run(af._auto_fetch_job(self.context))
        sent = "\n".join(c.args[1] for c in self.context.bot.send_message.call_args_list)
        self.assertIn("📌 *Ref:* `REG/TSFR/AAA111`", sent)
        self.assertIn("🗂 Source: HQ", sent)
        self.assertIn("Assessor: John Assessor", sent)
        self.assertIn("🏢 Registry: COAST", sent)
        self.assertIn("📍 County: MOMBASA", sent)
        self.assertIn("📋 Parcel: MOMBASA/BLOCK5/9", sent)
        self.assertIn("💰 Consideration: KES 2,000,000.00", sent)

    def test_email_sent_when_configured(self):
        cfg = {"days_back": 2, "email": "ops@example.com"}
        tasks = [_task()]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
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
        body = mock_email.call_args[0][2]
        self.assertIn("Assessor: Jane Doe", body)
        self.assertIn("Parcel: NAIROBI/BLOCK1/1", body)

    def test_email_body_uses_dlv_tasks_style_block_per_task(self):
        """The email body should render one labeled block per task (DLV Tasks'
        visual convention), not the old compact one-line-per-task format."""
        cfg = {"days_back": 2, "email": "ops@example.com"}
        tasks = [_task(ref="REG/TSFR/AAA111", county="Mombasa", registry="Coast",
                        parcel_number="MOMBASA/BLOCK5/9", assessor="John Assessor")]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", set()), \
             patch.object(af, "_send_auto_fetch_email") as mock_email:
            _run(af._auto_fetch_job(self.context))
        body = mock_email.call_args[0][2]
        self.assertIn("📌 Ref: REG/TSFR/AAA111", body)
        self.assertIn("Assessor: John Assessor", body)
        self.assertIn("🏢 Registry: COAST", body)
        self.assertIn("📍 County: MOMBASA", body)
        self.assertIn("📋 Parcel: MOMBASA/BLOCK5/9", body)

    def test_email_body_lists_tasks_highest_consideration_first(self):
        cfg = {"days_back": 2, "email": "ops@example.com"}
        tasks = [
            _task(ref="LOW", consideration="1000000"),
            _task(ref="HIGH", consideration="9000000"),
            _task(ref="MID", consideration="5000000"),
        ]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", set()), \
             patch.object(af, "_send_auto_fetch_email") as mock_email:
            _run(af._auto_fetch_job(self.context))
        body = mock_email.call_args[0][2]
        self.assertLess(body.index("Ref: HIGH"), body.index("Ref: MID"))
        self.assertLess(body.index("Ref: MID"), body.index("Ref: LOW"))

    def test_excel_format_sends_attachment_not_block_email(self):
        cfg = {"days_back": 2, "email": "ops@example.com", "report_format": "excel"}
        tasks = [_task()]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", set()), \
             patch.object(af, "_send_auto_fetch_email") as mock_block_email, \
             patch.object(af, "_send_bulk_export_email") as mock_excel_email:
            _run(af._auto_fetch_job(self.context))
        mock_block_email.assert_not_called()
        mock_excel_email.assert_called_once()
        self.assertEqual(mock_excel_email.call_args[0][0], "ops@example.com")
        self.assertTrue(mock_excel_email.call_args[0][1].endswith(".xlsx"))

    def test_missing_report_format_defaults_to_block(self):
        """Old schedules saved before report_format existed must still work."""
        cfg = {"days_back": 2, "email": "ops@example.com"}   # no "report_format" key
        tasks = [_task()]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", set()), \
             patch.object(af, "_send_auto_fetch_email") as mock_block_email, \
             patch.object(af, "_send_bulk_export_email") as mock_excel_email:
            _run(af._auto_fetch_job(self.context))
        mock_block_email.assert_called_once()
        mock_excel_email.assert_not_called()

    def test_legacy_email_format_key_still_honored(self):
        """Schedules saved back when the field was still called
        "email_format" (before it also governed Telegram) must keep working."""
        cfg = {"days_back": 2, "email": "ops@example.com", "email_format": "excel"}
        tasks = [_task()]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", set()), \
             patch.object(af, "_send_auto_fetch_email") as mock_block_email, \
             patch.object(af, "_send_bulk_export_email") as mock_excel_email:
            _run(af._auto_fetch_job(self.context))
        mock_block_email.assert_not_called()
        mock_excel_email.assert_called_once()

    def test_excel_format_sends_telegram_document_not_chunked_text(self):
        """report_format="excel" governs the Telegram delivery too — it
        should send an Excel document, not the usual chunked text blocks."""
        cfg = {"days_back": 2, "report_format": "excel"}   # no email — Telegram only
        tasks = [_task()]
        ctx = MagicMock()
        ctx.job.data = "sched-1"
        ctx.bot.send_document = AsyncMock()
        ctx.bot.send_message = AsyncMock()
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "ALLOWED_IDS", {111}):
            _run(af._auto_fetch_job(ctx))
        ctx.bot.send_document.assert_called_once()
        ctx.bot.send_message.assert_not_called()
        call = ctx.bot.send_document.call_args
        self.assertEqual(call.args[0], 111)
        self.assertTrue(call.kwargs["filename"].endswith(".xlsx"))

    def test_block_format_still_sends_telegram_chunked_text(self):
        cfg = {"days_back": 2, "report_format": "block"}
        tasks = [_task()]
        ctx = MagicMock()
        ctx.job.data = "sched-1"
        ctx.bot.send_document = AsyncMock()
        ctx.bot.send_message = AsyncMock()
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "ALLOWED_IDS", {111}):
            _run(af._auto_fetch_job(ctx))
        ctx.bot.send_document.assert_not_called()
        ctx.bot.send_message.assert_called()

    def _run_email_cycle(self, cfg, tasks):
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", set()), \
             patch.object(af, "_send_auto_fetch_email") as mock_email:
            _run(af._auto_fetch_job(self.context))
        return mock_email

    def test_second_cycle_with_identical_tasks_skips_the_email(self):
        cfg = {"id": "sched-1", "days_back": 2, "email": "ops@example.com"}
        tasks = [_task(ref="REG/TSFR/ABC123")]
        self._run_email_cycle(cfg, tasks).assert_called_once()
        mock_email_2 = self._run_email_cycle(cfg, tasks)
        mock_email_2.assert_not_called()

    def test_new_task_added_sends_only_the_new_task_not_previously_sent_ones(self):
        """abc@gmail.com already got 123; once 456 also matches, only 456
        should be in the next email — 123 is assumed already seen."""
        cfg = {"id": "sched-1", "days_back": 2, "email": "abc@gmail.com"}
        self._run_email_cycle(cfg, [_task(ref="123")])
        mock_email_2 = self._run_email_cycle(cfg, [_task(ref="123"), _task(ref="456")])
        mock_email_2.assert_called_once()
        body = mock_email_2.call_args[0][2]
        self.assertNotIn("Ref: 123", body)
        self.assertIn("Ref: 456", body)

    def test_previously_sent_ref_reappearing_alone_does_not_resend(self):
        """123 and 456 were already sent; a later cycle that only re-fetches
        123 (456 no longer matches) has nothing new to email — 123 stays
        suppressed even though the current ref set differs from last time."""
        cfg = {"id": "sched-1", "days_back": 2, "email": "ops@example.com"}
        self._run_email_cycle(cfg, [_task(ref="123"), _task(ref="456")])
        mock_email_2 = self._run_email_cycle(cfg, [_task(ref="123")])
        mock_email_2.assert_not_called()

    def test_different_schedules_are_deduped_independently(self):
        cfg_a = {"id": "sched-a", "days_back": 2, "email": "abc@gmail.com"}
        cfg_b = {"id": "sched-b", "days_back": 2, "email": "def@gmail.com"}
        same_tasks = [_task(ref="123")]
        self._run_email_cycle(cfg_a, same_tasks)
        # A different schedule with the exact same tasks must still send —
        # dedup state is per schedule id, not shared globally.
        mock_email_b = self._run_email_cycle(cfg_b, same_tasks)
        mock_email_b.assert_called_once()

    def test_failed_send_does_not_mark_the_ref_set_as_sent(self):
        """A cycle that fails to actually deliver shouldn't be treated as
        'already sent' — the next cycle must retry with the same tasks."""
        cfg = {"id": "sched-1", "days_back": 2, "email": "ops@example.com"}
        tasks = [_task(ref="123")]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", set()), \
             patch.object(af, "_send_auto_fetch_email", side_effect=RuntimeError("smtp down")):
            _run(af._auto_fetch_job(self.context))

        mock_email_2 = self._run_email_cycle(cfg, tasks)
        mock_email_2.assert_called_once()

    def test_email_failure_notifies_telegram_instead_of_failing_silently(self):
        """Regression test: an SMTP error used to be swallowed by a log line only,
        so a broken server-side email config looked identical to success."""
        cfg = {"days_back": 2, "email": "ops@example.com"}
        tasks = [_task()]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
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

    def test_email_failure_notification_escapes_special_chars_in_email(self):
        cfg = {"days_back": 2, "email": "john_doe@example.com"}
        tasks = [_task()]
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
             patch.object(af, "get_valid_tokens", return_value=TOKENS), \
             patch.object(af, "_load_fetch_tasks", return_value=(tasks, {})), \
             patch.object(af, "load_dlv_batch", return_value=[]), \
             patch.object(af, "load_sectional_config", return_value=None), \
             patch.object(af, "persist_af_result"), \
             patch.object(af, "_send_chunked_report", new_callable=AsyncMock), \
             patch.object(af, "ALLOWED_IDS", {111}), \
             patch.object(af, "_send_auto_fetch_email", side_effect=RuntimeError("smtp down")):
            _run(af._auto_fetch_job(self.context))
        self.context.bot.send_message.assert_any_call(
            111, "⚠️ Auto Fetch email delivery to *john\\_doe@example.com* failed: `smtp down`",
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
        with patch.object(af, "get_auto_fetch_schedule", return_value=cfg), \
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
                       "consideration": "1000", "assessor": "Jane", "source": "HQ"}],
            "filters": {},
        }
        with patch.object(af, "load_af_results", return_value=[run]), \
             patch.object(af, "load_saved_assignments", return_value={}):
            _run(af.recv_af_result_detail(update, ctx))
        update.callback_query.message.reply_text.assert_called_once()
        text = update.callback_query.message.reply_text.call_args[0][0]
        self.assertIn("📌 *Ref:* `REG/TSFR/X`", text)
        self.assertIn("🗂 Source: HQ", text)
        self.assertIn("Assessor: Jane", text)
        self.assertIn("💰 Consideration: KES 1,000.00", text)
        self.assertIn("📋 Parcel: P1", text)
        self.assertIn("🏢 Registry: CENTRAL", text)
        self.assertIn("📍 County: NAIROBI", text)
        self.assertIn("📅 Added: 2026-07-10", text)
        self.assertIn("📊 Status: ⏳ pending", text)

    def test_assigned_ref_shows_valuer_name_in_status(self):
        update = self._make_query("r1")
        ctx = MagicMock()
        run = {
            "run_id": "r1", "run_at": "t", "count": 1,
            "tasks": [{"ref": "REG/TSFR/X", "assessor": "Jane"}],
            "filters": {},
        }
        with patch.object(af, "load_af_results", return_value=[run]), \
             patch.object(af, "load_saved_assignments",
                           return_value={"REG/TSFR/X": {"valuer_name": "Byron"}}):
            _run(af.recv_af_result_detail(update, ctx))
        text = update.callback_query.message.reply_text.call_args[0][0]
        self.assertIn("📊 Status: ✅ assigned to Byron", text)

    def test_schedule_label_with_special_chars_is_escaped(self):
        """Regression: run['schedule_label'] is the schedule's email — an
        unescaped '_' there raised telegram.error.BadRequest."""
        update = self._make_query("r1")
        ctx = MagicMock()
        run = {
            "run_id": "r1", "run_at": "t", "count": 0, "schedule_label": "john_doe@example.com",
            "tasks": [], "filters": {},
        }
        with patch.object(af, "load_af_results", return_value=[run]), \
             patch.object(af, "load_saved_assignments", return_value={}):
            _run(af.recv_af_result_detail(update, ctx))
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("john\\_doe@example.com", text)


class TestRegisterRestoresSchedule(unittest.TestCase):
    """register() restores every saved Auto Fetch schedule on startup, each
    as its own job — regression test for a bug where the first run after
    every restart waited a full interval, starving the job if restarts
    happened more often than that."""

    async def _register(self, app):
        # ConversationHandler's construction needs a running event loop
        # (asyncio.Lock() binds to it), so register() must run inside one.
        af.register(app)

    def test_restored_first_run_is_soon_not_a_full_interval(self):
        app = MagicMock()
        cfg = {"id": "sched-1", "interval_minutes": 120}
        with patch.object(af, "load_auto_fetch_schedules", return_value=[cfg]):
            _run(self._register(app))
        _, kwargs = app.job_queue.run_repeating.call_args
        self.assertEqual(kwargs["interval"], 120 * 60)
        self.assertEqual(kwargs["first"], af._AF_RESTORE_FIRST_RUN_DELAY)
        self.assertLess(af._AF_RESTORE_FIRST_RUN_DELAY, 120 * 60)
        self.assertEqual(kwargs["name"], "auto_fetch_job:sched-1")
        self.assertEqual(kwargs["data"], "sched-1")

    def test_no_saved_schedules_does_not_register_any_job(self):
        app = MagicMock()
        with patch.object(af, "load_auto_fetch_schedules", return_value=[]):
            _run(self._register(app))
        app.job_queue.run_repeating.assert_not_called()

    def test_restores_one_job_per_schedule(self):
        app = MagicMock()
        schedules = [
            {"id": "sched-1", "interval_minutes": 60},
            {"id": "sched-2", "interval_minutes": 120},
        ]
        with patch.object(af, "load_auto_fetch_schedules", return_value=schedules):
            _run(self._register(app))
        self.assertEqual(app.job_queue.run_repeating.call_count, 2)
        names = {c.kwargs["name"] for c in app.job_queue.run_repeating.call_args_list}
        self.assertEqual(names, {"auto_fetch_job:sched-1", "auto_fetch_job:sched-2"})


class TestRecvAfEmail(unittest.TestCase):
    """recv_af_email validates the address, saves the schedule, and confirms
    which account the background job will run under."""

    def setUp(self):
        self.update = MagicMock()
        self.update.message.reply_text = AsyncMock()
        self.ctx = MagicMock()
        self.ctx.user_data = {}

    def test_invalid_email_reprompts(self):
        self.update.message.text = "not-an-email"
        result = _run(af.recv_af_email(self.update, self.ctx))
        self.assertEqual(result, af.AF.EMAIL)
        self.assertIn("Invalid email", self.update.message.reply_text.call_args[0][0])

    def test_valid_email_moves_to_report_format_step(self):
        """A real email defers finalizing until the report format is chosen
        next — governs Telegram delivery too, so it's asked unconditionally."""
        self.update.message.text = "ops@example.com"
        result = _run(af.recv_af_email(self.update, self.ctx))
        self.assertEqual(result, af.AF.REPORT_FORMAT)
        self.assertEqual(self.ctx.user_data["af_email"], "ops@example.com")
        text = self.update.message.reply_text.call_args[0][0]
        self.assertIn("Which format", text)

    def test_skip_also_moves_to_report_format_step(self):
        """skip (Telegram-only) still needs a format choice, since it now
        governs the periodic Telegram delivery regardless of email."""
        self.update.message.text = "skip"
        result = _run(af.recv_af_email(self.update, self.ctx))
        self.assertEqual(result, af.AF.REPORT_FORMAT)
        self.assertEqual(self.ctx.user_data["af_email"], "")


class TestRecvAfReportFormat(unittest.TestCase):
    """recv_af_report_format — the block/Excel choice, always reached
    (whether or not an email was entered), which finalizes the schedule."""

    def _make_query(self, data):
        update = MagicMock()
        query = update.callback_query
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message.reply_text = AsyncMock()
        return update

    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.user_data = {"af_email": "ops@example.com"}

    def test_valid_submission_confirms_the_support_credential(self):
        """Regression test: the confirmation used to say nothing about which
        account runs the job, hiding the fact it always needs a valid cached
        Support Reg login regardless of what credential you used elsewhere."""
        update = self._make_query("af_reportfmt:block")
        with patch.object(af, "add_auto_fetch_schedule", return_value="sched-1") as mock_add:
            result = _run(af.recv_af_report_format(update, self.ctx))
        self.assertEqual(result, af.ConversationHandler.END)
        mock_add.assert_called_once()
        self.assertEqual(mock_add.call_args[0][0]["email"], "ops@example.com")
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn(af.CRED_LABELS[af._AF_CRED_TYPE], text)

    def test_email_with_special_chars_is_escaped_in_confirmation(self):
        self.ctx.user_data["af_email"] = "john_doe@example.com"
        update = self._make_query("af_reportfmt:block")
        with patch.object(af, "add_auto_fetch_schedule", return_value="sched-1"):
            _run(af.recv_af_report_format(update, self.ctx))
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("john\\_doe@example.com", text)

    def test_valid_submission_schedules_a_job_tagged_with_the_new_id(self):
        update = self._make_query("af_reportfmt:block")
        with patch.object(af, "add_auto_fetch_schedule", return_value="sched-1"):
            _run(af.recv_af_report_format(update, self.ctx))
        _, kwargs = self.ctx.job_queue.run_repeating.call_args
        self.assertEqual(kwargs["name"], "auto_fetch_job:sched-1")
        self.assertEqual(kwargs["data"], "sched-1")

    def test_block_format_saved_and_shown_in_confirmation(self):
        update = self._make_query("af_reportfmt:block")
        with patch.object(af, "add_auto_fetch_schedule", return_value="sched-1") as mock_add:
            _run(af.recv_af_report_format(update, self.ctx))
        self.assertEqual(mock_add.call_args[0][0]["report_format"], "block")
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Text blocks", text)

    def test_excel_format_saved_and_shown_in_confirmation(self):
        update = self._make_query("af_reportfmt:excel")
        with patch.object(af, "add_auto_fetch_schedule", return_value="sched-1") as mock_add:
            _run(af.recv_af_report_format(update, self.ctx))
        self.assertEqual(mock_add.call_args[0][0]["report_format"], "excel")
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Excel", text)

    def test_report_format_shown_even_without_email(self):
        """Unlike the old email-only format label, the Format line always
        shows now, since it governs Telegram delivery regardless of email."""
        self.ctx.user_data["af_email"] = ""
        update = self._make_query("af_reportfmt:excel")
        with patch.object(af, "add_auto_fetch_schedule", return_value="sched-1"):
            _run(af.recv_af_report_format(update, self.ctx))
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Format: *Excel*", text)
        self.assertIn("Telegram only", text)


class TestAfBuildExcel(unittest.TestCase):
    """_af_build_excel — the Excel-attachment alternative to the block email body."""

    def test_sheet_title_and_columns(self):
        import io
        import openpyxl
        xlsx_bytes = af._af_build_excel([])
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
        self.assertEqual(wb.sheetnames, ["Auto Fetch"])
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertEqual(header, ["Reference Number", "Source", "Assessor", "Consideration", "Currency",
                                   "Parcel", "Registry", "County", "Date Added", "Tag"])

    def test_rows_populated_and_sorted_highest_consideration_first(self):
        import io
        import openpyxl
        tasks = [
            _task(ref="LOW", consideration="1000000"),
            _task(ref="HIGH", consideration="9000000"),
        ]
        xlsx_bytes = af._af_build_excel(tasks)
        ws = openpyxl.load_workbook(io.BytesIO(xlsx_bytes)).active
        self.assertEqual(ws.cell(row=2, column=1).value, "HIGH")
        self.assertEqual(ws.cell(row=3, column=1).value, "LOW")

    def test_officers_fallback_used_when_no_assessor(self):
        import io
        import openpyxl
        t = _task()
        t.pop("assessor", None)
        t["officers"] = [{"name": "Jane Doe", "role": "COUNTY_REGISTRAR"}]
        xlsx_bytes = af._af_build_excel([t])
        ws = openpyxl.load_workbook(io.BytesIO(xlsx_bytes)).active
        self.assertIn("Jane Doe", ws.cell(row=2, column=3).value)


class TestAfConsiderationValue(unittest.TestCase):
    """_af_consideration_value — sort key for the email body, missing/
    unparseable amounts sort last (below every real amount)."""

    def test_parses_numeric_string(self):
        self.assertEqual(af._af_consideration_value({"consideration": "2000000"}), 2000000.0)

    def test_strips_commas(self):
        self.assertEqual(af._af_consideration_value({"consideration": "2,000,000"}), 2000000.0)

    def test_missing_sorts_last(self):
        self.assertEqual(af._af_consideration_value({}), -1.0)

    def test_unparseable_sorts_last(self):
        self.assertEqual(af._af_consideration_value({"consideration": "N/A"}), -1.0)


class TestAfGetReportFormat(unittest.TestCase):
    """_af_get_report_format — reads "report_format", falling back to the
    older "email_format" key for schedules saved before it covered Telegram."""

    def test_reads_report_format_when_present(self):
        self.assertEqual(af._af_get_report_format({"report_format": "excel"}), "excel")

    def test_falls_back_to_legacy_email_format(self):
        self.assertEqual(af._af_get_report_format({"email_format": "excel"}), "excel")

    def test_report_format_takes_priority_over_legacy_key(self):
        self.assertEqual(
            af._af_get_report_format({"report_format": "block", "email_format": "excel"}), "block",
        )

    def test_defaults_to_block_when_neither_present(self):
        self.assertEqual(af._af_get_report_format({}), "block")


class TestAfFormatScheduleSummary(unittest.TestCase):
    """_af_format_schedule_summary — one-line-ish summary used in the
    schedule list and the remove picker."""

    def test_summary_includes_email_and_filters(self):
        cfg = {
            "interval_minutes": 60, "days_back": 3, "county_filter": "nairobi",
            "registry_filter": "central", "amount_min": 1_000_000, "amount_max": 5_000_000,
            "sectional_filter": "exclude", "email": "abc@gmail.com",
        }
        summary = af._af_format_schedule_summary(cfg)
        self.assertIn("abc@gmail.com", summary)
        self.assertIn("every 60 min", summary)
        self.assertIn("Nairobi", summary)
        self.assertIn("Central", summary)
        self.assertIn("KES 1,000,000", summary)
        self.assertIn("KES 5,000,000", summary)

    def test_summary_labels_telegram_only_when_no_email(self):
        summary = af._af_format_schedule_summary({"interval_minutes": 30})
        self.assertIn("Telegram only", summary)

    def test_email_with_markdown_special_chars_is_escaped(self):
        """Regression: an unescaped '_' in an email address raised
        telegram.error.BadRequest ("can't find end of the entity")."""
        summary = af._af_format_schedule_summary({"interval_minutes": 30, "email": "john_doe@example.com"})
        self.assertIn("john\\_doe@example.com", summary)


class TestCmdAutoFetch(unittest.TestCase):
    """cmd_auto_fetch lists existing schedules (if any) and shows Add/Remove."""

    def setUp(self):
        self.update = MagicMock()
        self.update.message.reply_text = AsyncMock()
        self.ctx = MagicMock()

    def _button_texts(self):
        markup = self.update.message.reply_text.call_args[1]["reply_markup"]
        return [b.text for row in markup.inline_keyboard for b in row]

    def test_no_schedules_shows_add_only_keyboard(self):
        with patch.object(af, "allowed", return_value=True), \
             patch.object(af, "load_auto_fetch_schedules", return_value=[]):
            result = _run(af.cmd_auto_fetch(self.update, self.ctx))
        self.assertEqual(result, af.AF.MENU)
        self.assertIn("No schedules yet", self.update.message.reply_text.call_args[0][0])
        self.assertIn("➕ Add Schedule", self._button_texts())
        self.assertNotIn("🗑 Remove Schedule", self._button_texts())

    def test_existing_schedules_are_listed_with_remove_option(self):
        schedules = [{"interval_minutes": 60, "email": "abc@gmail.com"}]
        with patch.object(af, "allowed", return_value=True), \
             patch.object(af, "load_auto_fetch_schedules", return_value=schedules):
            _run(af.cmd_auto_fetch(self.update, self.ctx))
        self.assertIn("abc@gmail.com", self.update.message.reply_text.call_args[0][0])
        self.assertIn("🗑 Remove Schedule", self._button_texts())


class TestRecvAfAmount(unittest.TestCase):
    """recv_af_amount — preset amount-range picker, including the new 10M-50M option."""

    def _make_query(self, data):
        update = MagicMock()
        query = update.callback_query
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        return update

    def test_10m_50m_range_sets_min_max(self):
        update = self._make_query("ft_amount:10m_50m")
        ctx = MagicMock()
        ctx.user_data = {}
        result = _run(af.recv_af_amount(update, ctx))
        self.assertEqual(result, af.AF.SECTIONAL)
        self.assertEqual(ctx.user_data["af_amount_min"], 10_000_000.0)
        self.assertEqual(ctx.user_data["af_amount_max"], 50_000_000.0)


class TestRecvAfMenu(unittest.TestCase):
    """recv_af_menu — Add / Remove / Close from the schedule list."""

    def _make_query(self, data):
        update = MagicMock()
        query = update.callback_query
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        query.message.reply_text = AsyncMock()
        return update

    def test_close_ends_conversation_and_clears_markup(self):
        update = self._make_query("af_menu:close")
        result = _run(af.recv_af_menu(update, MagicMock()))
        self.assertEqual(result, af.ConversationHandler.END)
        update.callback_query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)

    def test_remove_with_no_schedules_ends_conversation(self):
        update = self._make_query("af_menu:remove")
        with patch.object(af, "load_auto_fetch_schedules", return_value=[]):
            result = _run(af.recv_af_menu(update, MagicMock()))
        self.assertEqual(result, af.ConversationHandler.END)

    def test_remove_with_schedules_shows_picker(self):
        update = self._make_query("af_menu:remove")
        schedules = [{"id": "sched-1", "email": "abc@gmail.com", "interval_minutes": 60}]
        with patch.object(af, "load_auto_fetch_schedules", return_value=schedules):
            result = _run(af.recv_af_menu(update, MagicMock()))
        self.assertEqual(result, af.AF.REMOVE_PICK)

    def test_add_moves_to_interval_step(self):
        update = self._make_query("af_menu:add")
        result = _run(af.recv_af_menu(update, MagicMock()))
        self.assertEqual(result, af.AF.INTERVAL)


class TestRecvAfRemove(unittest.TestCase):
    """recv_af_remove — delete a schedule by id and cancel its running job."""

    def _make_query(self, data):
        update = MagicMock()
        query = update.callback_query
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message.reply_text = AsyncMock()
        return update

    def test_cancel_ends_without_removing(self):
        update = self._make_query("af_remove:cancel")
        with patch.object(af, "remove_auto_fetch_schedule") as mock_remove:
            result = _run(af.recv_af_remove(update, MagicMock()))
        self.assertEqual(result, af.ConversationHandler.END)
        mock_remove.assert_not_called()

    def test_removes_schedule_and_cancels_its_job(self):
        update = self._make_query("af_remove:sched-1")
        ctx = MagicMock()
        fake_job = MagicMock()
        ctx.job_queue.get_jobs_by_name.return_value = [fake_job]
        with patch.object(af, "get_auto_fetch_schedule",
                           return_value={"id": "sched-1", "email": "abc@gmail.com"}), \
             patch.object(af, "remove_auto_fetch_schedule", return_value=True) as mock_remove, \
             patch.object(af, "load_af_email_state", return_value={}), \
             patch.object(af, "save_af_email_state") as mock_save_state:
            result = _run(af.recv_af_remove(update, ctx))
        self.assertEqual(result, af.ConversationHandler.END)
        ctx.job_queue.get_jobs_by_name.assert_called_once_with("auto_fetch_job:sched-1")
        fake_job.schedule_removal.assert_called_once()
        mock_remove.assert_called_once_with("sched-1")
        mock_save_state.assert_not_called()   # nothing to clean up — state was already empty
        self.assertIn("abc@gmail.com", update.callback_query.edit_message_text.call_args[0][0])

    def test_removed_schedule_email_with_special_chars_is_escaped(self):
        update = self._make_query("af_remove:sched-1")
        ctx = MagicMock()
        ctx.job_queue.get_jobs_by_name.return_value = []
        with patch.object(af, "get_auto_fetch_schedule",
                           return_value={"id": "sched-1", "email": "john_doe@example.com"}), \
             patch.object(af, "remove_auto_fetch_schedule", return_value=True), \
             patch.object(af, "load_af_email_state", return_value={}), \
             patch.object(af, "save_af_email_state"):
            _run(af.recv_af_remove(update, ctx))
        self.assertIn("john\\_doe@example.com", update.callback_query.edit_message_text.call_args[0][0])

    def test_removing_a_schedule_also_drops_its_email_dedup_state(self):
        update = self._make_query("af_remove:sched-1")
        ctx = MagicMock()
        ctx.job_queue.get_jobs_by_name.return_value = []
        with patch.object(af, "get_auto_fetch_schedule",
                           return_value={"id": "sched-1", "email": "abc@gmail.com"}), \
             patch.object(af, "remove_auto_fetch_schedule", return_value=True), \
             patch.object(af, "load_af_email_state", return_value={"sched-1": ["REF1"], "sched-2": ["REF2"]}), \
             patch.object(af, "save_af_email_state") as mock_save_state:
            _run(af.recv_af_remove(update, ctx))
        mock_save_state.assert_called_once_with({"sched-2": ["REF2"]})

    def test_already_removed_shows_warning(self):
        update = self._make_query("af_remove:sched-1")
        ctx = MagicMock()
        ctx.job_queue.get_jobs_by_name.return_value = []
        with patch.object(af, "get_auto_fetch_schedule", return_value=None), \
             patch.object(af, "remove_auto_fetch_schedule", return_value=False), \
             patch.object(af, "load_af_email_state", return_value={}), \
             patch.object(af, "save_af_email_state"):
            _run(af.recv_af_remove(update, ctx))
        self.assertIn("already removed", update.callback_query.edit_message_text.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
