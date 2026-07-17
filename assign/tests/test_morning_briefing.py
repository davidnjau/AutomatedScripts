#!/usr/bin/env python3
"""
Unit tests for morning_briefing.py — config persistence, job scheduling,
and _run_morning_briefing's delivery branches (no tokens / fetch failure /
email / telegram / empty).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import morning_briefing as mb
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestBriefingConfigPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg_file = os.path.join(self.tmpdir.name, "saved_briefing_config.json")
        self._patch = patch.object(mb, "SAVED_BRIEFING_CONFIG_FILE", self.cfg_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(mb.load_briefing_config())

    def test_save_then_load_roundtrip(self):
        mb.save_briefing_config({"enabled": True, "delivery": "email", "email": "a@b.com"})
        cfg = mb.load_briefing_config()
        self.assertEqual(cfg["enabled"], True)
        self.assertEqual(cfg["email"], "a@b.com")


class TestScheduleMorningBriefing(unittest.TestCase):
    def test_removes_existing_job_and_schedules_new_one(self):
        old_job = MagicMock()
        job_queue = MagicMock()
        job_queue.get_jobs_by_name.return_value = [old_job]

        mb._schedule_morning_briefing(job_queue)

        old_job.schedule_removal.assert_called_once()
        job_queue.run_daily.assert_called_once()
        _, kwargs = job_queue.run_daily.call_args
        self.assertEqual(kwargs["name"], "morning_briefing_job")


class TestMorningBriefingJob(unittest.TestCase):
    def test_no_config_does_nothing(self):
        context = MagicMock()
        with patch.object(mb, "load_briefing_config", return_value=None), \
             patch.object(mb, "_run_morning_briefing", new_callable=AsyncMock) as mock_run:
            _run(mb._morning_briefing_job(context))
        mock_run.assert_not_called()

    def test_disabled_config_does_nothing(self):
        context = MagicMock()
        with patch.object(mb, "load_briefing_config", return_value={"enabled": False}), \
             patch.object(mb, "_run_morning_briefing", new_callable=AsyncMock) as mock_run:
            _run(mb._morning_briefing_job(context))
        mock_run.assert_not_called()

    def test_enabled_config_runs_briefing_with_saved_settings(self):
        context = MagicMock()
        cfg = {"enabled": True, "delivery": "email", "email": "ops@example.com"}
        with patch.object(mb, "load_briefing_config", return_value=cfg), \
             patch.object(mb, "_run_morning_briefing", new_callable=AsyncMock) as mock_run:
            _run(mb._morning_briefing_job(context))
        mock_run.assert_called_once_with(context, "email", "ops@example.com")


class TestRunMorningBriefing(unittest.TestCase):
    def setUp(self):
        self.context = MagicMock()
        self.context.bot.send_message = AsyncMock()

    def test_no_tokens_sends_warning(self):
        with patch.object(mb, "_any_valid_tokens", return_value=None), \
             patch.object(mb, "_send_briefing", new_callable=AsyncMock) as mock_send:
            _run(mb._run_morning_briefing(self.context, "telegram", ""))
        mock_send.assert_called_once()
        self.assertIn("No valid cached tokens", mock_send.call_args[0][1])

    def test_fetch_failure_sends_error(self):
        with patch.object(mb, "_any_valid_tokens", return_value=TOKENS), \
             patch("asyncio.to_thread", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch.object(mb, "_send_briefing", new_callable=AsyncMock) as mock_send:
            _run(mb._run_morning_briefing(self.context, "telegram", ""))
        mock_send.assert_called_once()
        self.assertIn("Failed to fetch tasks", mock_send.call_args[0][1])

    def test_email_delivery_without_saved_email_warns(self):
        with patch.object(mb, "_any_valid_tokens", return_value=TOKENS), \
             patch("asyncio.to_thread", new_callable=AsyncMock, return_value=[{"ref": "A"}]), \
             patch.object(mb, "_send_briefing", new_callable=AsyncMock) as mock_send:
            _run(mb._run_morning_briefing(self.context, "email", ""))
        self.assertIn("no address is saved", mock_send.call_args[0][1])

    def test_email_delivery_success(self):
        with patch.object(mb, "_any_valid_tokens", return_value=TOKENS), \
             patch("asyncio.to_thread", new_callable=AsyncMock, return_value=[{"ref": "A"}]), \
             patch.object(mb, "_dt_build_excel", return_value=b"xlsx"), \
             patch.object(mb, "_send_bulk_export_email") as mock_email, \
             patch.object(mb, "_send_briefing", new_callable=AsyncMock) as mock_send:
            _run(mb._run_morning_briefing(self.context, "email", "ops@example.com"))
        mock_email.assert_called_once()
        self.assertIn("emailed to *ops@example.com*", mock_send.call_args[0][1])

    def test_email_delivery_success_escapes_special_chars(self):
        """Regression: an unescaped '_' in the email raised
        telegram.error.BadRequest ("can't find end of the entity")."""
        with patch.object(mb, "_any_valid_tokens", return_value=TOKENS), \
             patch("asyncio.to_thread", new_callable=AsyncMock, return_value=[{"ref": "A"}]), \
             patch.object(mb, "_dt_build_excel", return_value=b"xlsx"), \
             patch.object(mb, "_send_bulk_export_email"), \
             patch.object(mb, "_send_briefing", new_callable=AsyncMock) as mock_send:
            _run(mb._run_morning_briefing(self.context, "email", "john_doe@example.com"))
        self.assertIn("emailed to *john\\_doe@example.com*", mock_send.call_args[0][1])

    def test_email_delivery_failure_reports_error(self):
        with patch.object(mb, "_any_valid_tokens", return_value=TOKENS), \
             patch("asyncio.to_thread", new_callable=AsyncMock, return_value=[{"ref": "A"}]), \
             patch.object(mb, "_dt_build_excel", return_value=b"xlsx"), \
             patch.object(mb, "_send_bulk_export_email", side_effect=RuntimeError("smtp down")), \
             patch.object(mb, "_send_briefing", new_callable=AsyncMock) as mock_send:
            _run(mb._run_morning_briefing(self.context, "email", "ops@example.com"))
        self.assertIn("Email delivery failed", mock_send.call_args[0][1])

    def test_telegram_delivery_no_rows(self):
        with patch.object(mb, "_any_valid_tokens", return_value=TOKENS), \
             patch("asyncio.to_thread", new_callable=AsyncMock, return_value=[]), \
             patch.object(mb, "_send_briefing", new_callable=AsyncMock) as mock_send:
            _run(mb._run_morning_briefing(self.context, "telegram", ""))
        self.assertIn("No Open DLV Tasks", mock_send.call_args[0][1])

    def test_telegram_delivery_broadcasts_to_all_allowed_ids(self):
        with patch.object(mb, "_any_valid_tokens", return_value=TOKENS), \
             patch("asyncio.to_thread", new_callable=AsyncMock, return_value=[{"ref": "A"}]), \
             patch.object(mb, "ALLOWED_IDS", {111, 222}), \
             patch.object(mb, "_dt_send_telegram", new_callable=AsyncMock) as mock_dt_send, \
             patch.object(mb, "_send_briefing", new_callable=AsyncMock):
            _run(mb._run_morning_briefing(self.context, "telegram", ""))
        self.assertEqual(mock_dt_send.call_count, 2)


class TestRecvMbEmail(unittest.TestCase):
    """recv_mb_email — validates the address, saves config, confirms enablement."""

    def test_email_with_special_chars_is_escaped_in_confirmation(self):
        """Regression: an unescaped '_' in the email raised
        telegram.error.BadRequest ("can't find end of the entity")."""
        update = MagicMock()
        update.message.text = "john_doe@example.com"
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        with patch.object(mb, "allowed", return_value=True), \
             patch.object(mb, "save_briefing_config"), \
             patch.object(mb, "_schedule_morning_briefing"):
            _run(mb.recv_mb_email(update, ctx))
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("john\\_doe@example.com", text)


if __name__ == "__main__":
    unittest.main()
