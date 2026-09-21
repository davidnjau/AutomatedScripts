#!/usr/bin/env python3
"""
Unit tests for parcel_watch.py — the saved-watch persistence layer, the
setup conversation (parcel -> interval -> delivery -> optional email),
the _pw_check_job one-shot notify-then-remove pipeline, /parcelwatches'
listing + cancel flow, and register()'s startup job restoration.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parcel_watch as pw
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")

SAMPLE_MATCHES = [
    {"reference_number": "R1", "application_status": "ongoing",
     "node": "VALUATION_STAMP_DUTY_CREATED", "registry": "Nairobi",
     "county": "Nairobi", "date_created": "2026-01-01"},
]


def _run(coro):
    return asyncio.run(coro)


def _make_update_with_message(text=""):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = 555
    return update


def _make_update_with_callback(data):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    query.message.chat_id = 555
    return update


class TestParcelWatchPersistence(unittest.TestCase):
    """load/add/get/remove_parcel_watch(es) — the on-disk store, isolated
    per test via a temp file so state never leaks between tests."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.watch_file = os.path.join(self.tmpdir.name, "saved_parcel_watches.json")
        self._patch = patch.object(pw, "SAVED_PARCEL_WATCHES_FILE", self.watch_file)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(pw.load_parcel_watches(), [])

    def test_add_then_load_roundtrip(self):
        watch_id = pw.add_parcel_watch(555, "NBI/BLOCK1/123", 30, "")
        watches = pw.load_parcel_watches()
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0]["parcel"], "NBI/BLOCK1/123")
        self.assertEqual(watches[0]["interval_minutes"], 30)
        self.assertEqual(watches[0]["id"], watch_id)

    def test_add_multiple_watches_get_distinct_ids(self):
        id1 = pw.add_parcel_watch(555, "P1", 15, "")
        id2 = pw.add_parcel_watch(555, "P2", 60, "a@b.com")
        self.assertNotEqual(id1, id2)
        self.assertEqual(len(pw.load_parcel_watches()), 2)

    def test_get_parcel_watch_finds_by_id(self):
        watch_id = pw.add_parcel_watch(555, "P1", 15, "")
        watch = pw.get_parcel_watch(watch_id)
        self.assertEqual(watch["parcel"], "P1")

    def test_get_parcel_watch_returns_none_when_missing(self):
        self.assertIsNone(pw.get_parcel_watch("no-such-id"))

    def test_remove_parcel_watch_deletes_only_that_one(self):
        id1 = pw.add_parcel_watch(555, "P1", 15, "")
        id2 = pw.add_parcel_watch(555, "P2", 60, "")
        removed = pw.remove_parcel_watch(id1)
        self.assertTrue(removed)
        remaining = pw.load_parcel_watches()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], id2)

    def test_remove_missing_watch_returns_false(self):
        self.assertFalse(pw.remove_parcel_watch("no-such-id"))


class TestCmdParcelWatch(unittest.TestCase):
    def test_asks_for_parcel_number(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(pw, "allowed", return_value=True):
            result = _run(pw.cmd_parcel_watch(update, ctx))
        self.assertEqual(result, pw.PW.PARCEL_INPUT)


class TestRecvPwParcel(unittest.TestCase):
    def test_blank_parcel_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        with patch.object(pw, "allowed", return_value=True):
            result = _run(pw.recv_pw_parcel(update, ctx))
        self.assertEqual(result, pw.PW.PARCEL_INPUT)

    def test_no_cached_tokens_ends_conversation(self):
        update = _make_update_with_message("NBI/BLOCK1/123")
        ctx = MagicMock()
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "get_valid_tokens", return_value=None):
            result = _run(pw.recv_pw_parcel(update, ctx))
        self.assertEqual(result, pw.ConversationHandler.END)

    def test_valid_parcel_stores_and_asks_interval(self):
        update = _make_update_with_message("NBI/BLOCK1/123")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "get_valid_tokens", return_value=TOKENS):
            result = _run(pw.recv_pw_parcel(update, ctx))
        self.assertEqual(result, pw.PW.INTERVAL)
        sess = pw._get_pw_sess(ctx)
        self.assertEqual(sess.parcel, "NBI/BLOCK1/123")
        self.assertEqual(sess.parcels, [])

    def test_too_many_parcels_ends_conversation_without_token_check(self):
        raw = "\n".join(f"P{i}" for i in range(pw._LIST_INPUT_MAX_ITEMS + 1))
        update = _make_update_with_message(raw)
        ctx = MagicMock()
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "get_valid_tokens") as mock_tokens:
            result = _run(pw.recv_pw_parcel(update, ctx))
        self.assertEqual(result, pw.ConversationHandler.END)
        mock_tokens.assert_not_called()

    def test_multiple_parcels_stashed_as_list(self):
        update = _make_update_with_message("P1\nP2")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "get_valid_tokens", return_value=TOKENS):
            result = _run(pw.recv_pw_parcel(update, ctx))
        self.assertEqual(result, pw.PW.INTERVAL)
        sess = pw._get_pw_sess(ctx)
        self.assertEqual(sess.parcel, "")
        self.assertEqual(sess.parcels, ["P1", "P2"])


class TestRecvPwInterval(unittest.TestCase):
    def test_cancel_ends_conversation(self):
        update = _make_update_with_callback("pw_interval:cancel")
        ctx = MagicMock()
        with patch.object(pw, "allowed", return_value=True):
            result = _run(pw.recv_pw_interval(update, ctx))
        self.assertEqual(result, pw.ConversationHandler.END)

    def test_valid_choice_stores_interval_and_asks_delivery(self):
        update = _make_update_with_callback("pw_interval:120")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pw, "allowed", return_value=True):
            result = _run(pw.recv_pw_interval(update, ctx))
        self.assertEqual(result, pw.PW.DELIVERY)
        self.assertEqual(pw._get_pw_sess(ctx).interval_minutes, 120)


class TestRecvPwDelivery(unittest.TestCase):
    def test_telegram_only_finalizes_and_ends(self):
        update = _make_update_with_callback("pw_delivery:telegram")
        ctx = MagicMock()
        ctx.user_data = {}
        sess = pw._get_pw_sess(ctx)
        sess.parcel = "NBI/BLOCK1/123"
        sess.interval_minutes = 30
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "add_parcel_watch", return_value="watch-1") as mock_add:
            result = _run(pw.recv_pw_delivery(update, ctx))
        self.assertEqual(result, pw.ConversationHandler.END)
        mock_add.assert_called_once_with(555, "NBI/BLOCK1/123", 30, "")
        ctx.job_queue.run_repeating.assert_called_once()
        _, kwargs = ctx.job_queue.run_repeating.call_args
        self.assertEqual(kwargs["interval"], 30 * 60)
        self.assertEqual(kwargs["name"], "pl_watch_job:watch-1")
        self.assertEqual(kwargs["data"], "watch-1")

    def test_email_mode_prompts_for_address(self):
        update = _make_update_with_callback("pw_delivery:email")
        ctx = MagicMock()
        with patch.object(pw, "allowed", return_value=True):
            result = _run(pw.recv_pw_delivery(update, ctx))
        self.assertEqual(result, pw.PW.EMAIL_INPUT)
        ctx.job_queue.run_repeating.assert_not_called()

    def test_list_mode_creates_one_watch_per_parcel(self):
        update = _make_update_with_callback("pw_delivery:telegram")
        ctx = MagicMock()
        ctx.user_data = {}
        sess = pw._get_pw_sess(ctx)
        sess.parcels = ["P1", "P2"]
        sess.interval_minutes = 30
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "add_parcel_watch", side_effect=["watch-1", "watch-2"]) as mock_add:
            result = _run(pw.recv_pw_delivery(update, ctx))
        self.assertEqual(result, pw.ConversationHandler.END)
        self.assertEqual(mock_add.call_count, 2)
        mock_add.assert_any_call(555, "P1", 30, "")
        mock_add.assert_any_call(555, "P2", 30, "")
        self.assertEqual(ctx.job_queue.run_repeating.call_count, 2)
        sent_text = update.callback_query.edit_message_text.call_args_list[-1].args[0]
        self.assertIn("2 watches queued", sent_text)


class TestRecvPwEmail(unittest.TestCase):
    def test_invalid_email_reprompts(self):
        update = _make_update_with_message("not-an-email")
        ctx = MagicMock()
        with patch.object(pw, "allowed", return_value=True):
            result = _run(pw.recv_pw_email(update, ctx))
        self.assertEqual(result, pw.PW.EMAIL_INPUT)

    def test_valid_email_finalizes_and_ends(self):
        update = _make_update_with_message("someone@example.com")
        ctx = MagicMock()
        ctx.user_data = {}
        sess = pw._get_pw_sess(ctx)
        sess.parcel = "NBI/BLOCK1/123"
        sess.interval_minutes = 15
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "add_parcel_watch", return_value="watch-2") as mock_add:
            result = _run(pw.recv_pw_email(update, ctx))
        self.assertEqual(result, pw.ConversationHandler.END)
        mock_add.assert_called_once_with(555, "NBI/BLOCK1/123", 15, "someone@example.com")
        ctx.job_queue.run_repeating.assert_called_once()


class TestPwQueuedText(unittest.TestCase):
    def test_singular_for_one_watch(self):
        self.assertEqual(pw._pw_queued_text(["watch-1"]), "✅ Watch queued.")

    def test_plural_for_multiple_watches(self):
        self.assertIn("2 watches queued", pw._pw_queued_text(["watch-1", "watch-2"]))


class TestPwFetchEnriched(unittest.TestCase):
    def test_pairs_each_match_with_its_detail_and_ref(self):
        matches = [
            {"reference_number": "R1", "id": "app-1"},
            {"reference_number": "R2", "id": "app-2"},
        ]
        details = {"app-1": {"node": "N1"}, "app-2": None}
        with patch.object(pw, "_lu_fetch_detail", side_effect=lambda tokens, app_id: details[app_id]):
            result = pw._pw_fetch_enriched(TOKENS, matches)
        self.assertEqual(result, [
            ("R1", matches[0], {"node": "N1"}),
            ("R2", matches[1], None),
        ])


class TestPwCheckJob(unittest.TestCase):
    def setUp(self):
        self.context = MagicMock()
        self.context.bot.send_message = AsyncMock()
        self.context.job.data = "watch-1"
        # Every "match found" test enriches via a real Reference Lookup
        # detail call — stub it out so tests never hit the live API.
        self._patch_detail = patch.object(pw, "_lu_fetch_detail", return_value=None)
        self._patch_detail.start()
        self.addCleanup(self._patch_detail.stop)

    def test_watch_removed_since_last_run_cancels_the_job(self):
        with patch.object(pw, "get_parcel_watch", return_value=None), \
             patch.object(pw, "get_valid_tokens") as mock_tokens:
            _run(pw._pw_check_job(self.context))
        mock_tokens.assert_not_called()
        self.context.job.schedule_removal.assert_called_once()

    def test_no_tokens_skips_cycle_without_removing_job(self):
        watch = {"id": "watch-1", "chat_id": 555, "parcel": "P1", "email": ""}
        with patch.object(pw, "get_parcel_watch", return_value=watch), \
             patch.object(pw, "get_valid_tokens", return_value=None), \
             patch.object(pw, "_pl_search_parcel") as mock_search:
            _run(pw._pw_check_job(self.context))
        mock_search.assert_not_called()
        self.context.job.schedule_removal.assert_not_called()

    def test_no_matches_does_nothing(self):
        watch = {"id": "watch-1", "chat_id": 555, "parcel": "P1", "email": ""}
        with patch.object(pw, "get_parcel_watch", return_value=watch), \
             patch.object(pw, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pw, "_pl_search_parcel", return_value=[]), \
             patch.object(pw, "remove_parcel_watch") as mock_remove:
            _run(pw._pw_check_job(self.context))
        self.context.bot.send_message.assert_not_awaited()
        mock_remove.assert_not_called()
        self.context.job.schedule_removal.assert_not_called()

    def test_match_found_sends_telegram_and_removes_watch(self):
        watch = {"id": "watch-1", "chat_id": 555, "parcel": "NBI/BLOCK1/123", "email": ""}
        with patch.object(pw, "get_parcel_watch", return_value=watch), \
             patch.object(pw, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pw, "_pl_search_parcel", return_value=list(SAMPLE_MATCHES)), \
             patch.object(pw, "remove_parcel_watch") as mock_remove:
            _run(pw._pw_check_job(self.context))
        self.context.bot.send_message.assert_awaited()
        sent_text = self.context.bot.send_message.call_args_list[-1].args[1]
        self.assertIn("R1", sent_text)
        mock_remove.assert_called_once_with("watch-1")
        self.context.job.schedule_removal.assert_called_once()

    def test_match_found_notification_is_enriched_with_reference_lookup_detail(self):
        """Regression: the notification must come from _lu_format_result
        (valuer/consideration/node from the detail-view call), not the
        bare list-item block parcel_lookup.py's own immediate report uses."""
        watch = {"id": "watch-1", "chat_id": 555, "parcel": "NBI/BLOCK1/123", "email": ""}
        detail = {
            "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
            "actors": [{"role": "VALUATION OFFICER", "user_details": {"names": "Jane Doe"}}],
            "external_process_details": {"consideration_amount": "5000000", "currency_code": "KES"},
        }
        with patch.object(pw, "get_parcel_watch", return_value=watch), \
             patch.object(pw, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pw, "_pl_search_parcel", return_value=list(SAMPLE_MATCHES)), \
             patch.object(pw, "_lu_fetch_detail", return_value=detail), \
             patch.object(pw, "remove_parcel_watch"):
            _run(pw._pw_check_job(self.context))
        sent_text = self.context.bot.send_message.call_args_list[-1].args[1]
        self.assertIn("Jane Doe", sent_text)
        self.assertIn("5,000,000.00", sent_text)

    def test_match_found_with_email_also_sends_email(self):
        watch = {"id": "watch-1", "chat_id": 555, "parcel": "NBI/BLOCK1/123", "email": "someone@example.com"}
        with patch.object(pw, "get_parcel_watch", return_value=watch), \
             patch.object(pw, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pw, "_pl_search_parcel", return_value=list(SAMPLE_MATCHES)), \
             patch.object(pw, "remove_parcel_watch"), \
             patch.object(pw, "_send_auto_fetch_email") as mock_email:
            _run(pw._pw_check_job(self.context))
        mock_email.assert_called_once()
        self.assertEqual(mock_email.call_args.args[0], "someone@example.com")

    def test_email_failure_does_not_block_watch_removal(self):
        watch = {"id": "watch-1", "chat_id": 555, "parcel": "NBI/BLOCK1/123", "email": "someone@example.com"}
        with patch.object(pw, "get_parcel_watch", return_value=watch), \
             patch.object(pw, "get_valid_tokens", return_value=TOKENS), \
             patch.object(pw, "_pl_search_parcel", return_value=list(SAMPLE_MATCHES)), \
             patch.object(pw, "remove_parcel_watch") as mock_remove, \
             patch.object(pw, "_send_auto_fetch_email", side_effect=RuntimeError("smtp down")):
            _run(pw._pw_check_job(self.context))
        mock_remove.assert_called_once_with("watch-1")
        self.context.job.schedule_removal.assert_called_once()


class TestCmdParcelWatches(unittest.TestCase):
    def test_no_watches_for_this_chat_shows_empty_message(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "load_parcel_watches", return_value=[]):
            _run(pw.cmd_parcel_watches(update, ctx))
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("No active", sent_text)

    def test_only_this_chat_s_watches_are_listed(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        watches = [
            {"id": "w1", "chat_id": 555, "parcel": "P1", "interval_minutes": 15, "email": ""},
            {"id": "w2", "chat_id": 999, "parcel": "P2", "interval_minutes": 30, "email": ""},
        ]
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "load_parcel_watches", return_value=watches):
            _run(pw.cmd_parcel_watches(update, ctx))
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("P1", sent_text)
        self.assertNotIn("P2", sent_text)


class TestRecvPwCancel(unittest.TestCase):
    def test_close_just_clears_markup(self):
        update = _make_update_with_callback("pw_cancel:close")
        ctx = MagicMock()
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "remove_parcel_watch") as mock_remove:
            _run(pw.recv_pw_cancel(update, ctx))
        mock_remove.assert_not_called()
        update.callback_query.edit_message_reply_markup.assert_awaited()

    def test_specific_id_cancels_job_and_removes_watch(self):
        update = _make_update_with_callback("pw_cancel:watch-1")
        ctx = MagicMock()
        job = MagicMock()
        ctx.job_queue.get_jobs_by_name.return_value = [job]
        with patch.object(pw, "allowed", return_value=True), \
             patch.object(pw, "remove_parcel_watch") as mock_remove:
            _run(pw.recv_pw_cancel(update, ctx))
        ctx.job_queue.get_jobs_by_name.assert_called_once_with("pl_watch_job:watch-1")
        job.schedule_removal.assert_called_once()
        mock_remove.assert_called_once_with("watch-1")


class TestRegisterRestoresWatches(unittest.TestCase):
    """register() restores every saved watch on startup, each as its own
    job — mirrors auto_fetch.py's schedule restore."""

    async def _register(self, app):
        # ConversationHandler's construction needs a running event loop.
        pw.register(app)

    def test_restored_first_run_uses_restore_delay_not_full_interval(self):
        app = MagicMock()
        watch = {"id": "watch-1", "interval_minutes": 120}
        with patch.object(pw, "load_parcel_watches", return_value=[watch]):
            _run(self._register(app))
        _, kwargs = app.job_queue.run_repeating.call_args
        self.assertEqual(kwargs["interval"], 120 * 60)
        self.assertEqual(kwargs["first"], pw._PW_RESTORE_FIRST_RUN_DELAY)
        self.assertLess(pw._PW_RESTORE_FIRST_RUN_DELAY, 120 * 60)
        self.assertEqual(kwargs["name"], "pl_watch_job:watch-1")
        self.assertEqual(kwargs["data"], "watch-1")

    def test_no_saved_watches_does_not_register_any_job(self):
        app = MagicMock()
        with patch.object(pw, "load_parcel_watches", return_value=[]):
            _run(self._register(app))
        app.job_queue.run_repeating.assert_not_called()

    def test_restores_one_job_per_watch(self):
        app = MagicMock()
        watches = [
            {"id": "watch-1", "interval_minutes": 15},
            {"id": "watch-2", "interval_minutes": 60},
        ]
        with patch.object(pw, "load_parcel_watches", return_value=watches):
            _run(self._register(app))
        self.assertEqual(app.job_queue.run_repeating.call_count, 2)
        names = {c.kwargs["name"] for c in app.job_queue.run_repeating.call_args_list}
        self.assertEqual(names, {"pl_watch_job:watch-1", "pl_watch_job:watch-2"})


if __name__ == "__main__":
    unittest.main()
