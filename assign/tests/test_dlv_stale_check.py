#!/usr/bin/env python3
"""
Unit tests for dlv_stale_check.py — the Stale Pending checker: threshold
persistence/Bot Settings UI, candidate selection (age/recheck/resend gates),
run_stale_check's four outcomes (still findable, removed with no parcel,
removed with an unmatched parcel, and the replace/remove decision prompt),
and the global stalefix: decision handler (remove / use-same-valuer /
use-change-valuer / pick-saved-valuer).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlv_stale_check as sc

TOKENS = object()


def _run(coro):
    return asyncio.run(coro)


def _make_query_update(data):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    return update


class TestStaleCheckConfigPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg_file = os.path.join(self.tmpdir.name, "saved_stale_check_config.json")
        self._patch = patch.object(sc, "SAVED_STALE_CHECK_CONFIG_FILE", self.cfg_file)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def test_missing_file_defaults_to_30(self):
        self.assertEqual(sc.load_stale_check_config(), {"threshold_days": 30})

    def test_save_then_load_roundtrip(self):
        sc.save_stale_check_config(14)
        self.assertEqual(sc.load_stale_check_config(), {"threshold_days": 14})

    def test_corrupt_file_defaults_to_30(self):
        with open(self.cfg_file, "w") as f:
            f.write("not json")
        self.assertEqual(sc.load_stale_check_config(), {"threshold_days": 30})


class TestStaleThresholdKeyboard(unittest.TestCase):
    def test_current_value_is_checkmarked(self):
        markup = sc._stale_threshold_keyboard(30)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("✅ 30d", labels)
        self.assertIn("7d", labels)

    def test_includes_cancel_button(self):
        markup = sc._stale_threshold_keyboard(30)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("stalecfg:cancel", callbacks)


class TestCmdStaleThreshold(unittest.TestCase):
    def test_shows_current_threshold(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_stale_check_config", return_value={"threshold_days": 21}):
            _run(sc.cmd_stale_threshold(update, ctx))
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("21 days", text)


class TestRecvStaleThresholdAction(unittest.TestCase):
    def test_cancel_clears_markup(self):
        update = _make_query_update("stalecfg:cancel")
        ctx = MagicMock()
        with patch.object(sc, "allowed", return_value=True):
            _run(sc.recv_stale_threshold_action(update, ctx))
        update.callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    def test_pick_days_saves_and_confirms(self):
        update = _make_query_update("stalecfg:days:14")
        ctx = MagicMock()
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "save_stale_check_config") as mock_save:
            _run(sc.recv_stale_threshold_action(update, ctx))
        mock_save.assert_called_once_with(14)
        text = update.callback_query.message.reply_text.call_args[0][0]
        self.assertIn("14 days", text)


class TestScAgeDays(unittest.TestCase):
    def test_missing_returns_zero(self):
        self.assertEqual(sc._sc_age_days(""), 0)

    def test_unparseable_returns_zero(self):
        self.assertEqual(sc._sc_age_days("not-a-date"), 0)

    def test_computes_whole_days(self):
        ts = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
        self.assertEqual(sc._sc_age_days(ts), 5)


class TestScHoursSince(unittest.TestCase):
    def test_missing_is_infinite(self):
        self.assertEqual(sc._sc_hours_since(None, datetime.now()), float("inf"))

    def test_unparseable_is_infinite(self):
        self.assertEqual(sc._sc_hours_since("not-a-date", datetime.now()), float("inf"))

    def test_computes_hours(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        ts = (now - timedelta(hours=3)).isoformat(timespec="seconds")
        self.assertAlmostEqual(sc._sc_hours_since(ts, now), 3.0)


class TestScCandidates(unittest.TestCase):
    def _item(self, **overrides):
        item = {"ref": "REF1", "last_error": "Not found in DLV endpoint",
                "queued_at": (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")}
        item.update(overrides)
        return item

    def test_wrong_last_error_is_excluded(self):
        item = self._item(last_error="Detail fetch returned empty response")
        self.assertEqual(sc._sc_candidates([item], 30, datetime.now()), [])

    def test_old_enough_never_checked_is_a_candidate(self):
        item = self._item()
        self.assertEqual(sc._sc_candidates([item], 30, datetime.now()), [item])

    def test_too_young_is_excluded(self):
        item = self._item(queued_at=(datetime.now() - timedelta(days=5)).isoformat(timespec="seconds"))
        self.assertEqual(sc._sc_candidates([item], 30, datetime.now()), [])

    def test_recently_checked_is_excluded_even_if_old_enough(self):
        now = datetime.now()
        item = self._item(stale_checked_at=(now - timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertEqual(sc._sc_candidates([item], 30, now), [])

    def test_checked_over_24h_ago_is_a_candidate_again(self):
        now = datetime.now()
        item = self._item(stale_checked_at=(now - timedelta(hours=25)).isoformat(timespec="seconds"))
        self.assertEqual(sc._sc_candidates([item], 30, now), [item])

    def test_pending_decision_too_young_is_not_a_candidate(self):
        """A too-young (never old enough) ref can still have a pending
        decision in edge cases (e.g. threshold lowered after the fact) —
        the resend gate only cares about stale_checked_at, not age."""
        now = datetime.now()
        item = self._item(
            queued_at=(now - timedelta(days=5)).isoformat(timespec="seconds"),
            stale_decision_pending=True,
            stale_checked_at=(now - timedelta(hours=1)).isoformat(timespec="seconds"),
        )
        self.assertEqual(sc._sc_candidates([item], 30, now), [])

    def test_pending_decision_due_for_resend_is_a_candidate_regardless_of_age(self):
        now = datetime.now()
        item = self._item(
            queued_at=(now - timedelta(days=5)).isoformat(timespec="seconds"),
            stale_decision_pending=True,
            stale_checked_at=(now - timedelta(hours=25)).isoformat(timespec="seconds"),
        )
        self.assertEqual(sc._sc_candidates([item], 30, now), [item])


class TestScRemovedNotice(unittest.TestCase):
    def test_includes_ref_valuer_and_reason(self):
        item = {"ref": "REF1", "valuer_name": "Jane Doe", "queued_at": "2026-01-01T00:00:00"}
        text = sc._sc_removed_notice(item, "not found anywhere")
        self.assertIn("REF1", text)
        self.assertIn("Jane Doe", text)
        self.assertIn("not found anywhere", text)
        self.assertIn("2026-01-01T00:00:00", text)


class TestScMatchKeyboardAndPrompt(unittest.TestCase):
    def test_one_button_per_match_plus_remove(self):
        markup = sc._sc_match_keyboard("OLD1", ["NEW1", "NEW2"])
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("stalefix:use:OLD1:NEW1", callbacks)
        self.assertIn("stalefix:use:OLD1:NEW2", callbacks)
        self.assertIn("stalefix:remove:OLD1", callbacks)

    def test_prompt_text_mentions_ref_parcel_and_match_count(self):
        item = {"ref": "OLD1", "parcel": "NBI/BLOCK1/123", "valuer_name": "Jane Doe",
                "stale_matches": ["NEW1", "NEW2"], "queued_at": (datetime.now() - timedelta(days=10)).isoformat()}
        text = sc._sc_match_prompt_text(item)
        self.assertIn("OLD1", text)
        self.assertIn("NBI/BLOCK1/123", text)
        self.assertIn("2 other reference", text)
        self.assertIn("Jane Doe", text)


class TestSendStaleMatchPrompt(unittest.TestCase):
    def test_sends_to_every_allowed_id(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        item = {"ref": "OLD1", "parcel": "P1", "valuer_name": "Jane", "stale_matches": ["NEW1"],
                "queued_at": datetime.now().isoformat()}
        with patch.object(sc, "ALLOWED_IDS", {111, 222}):
            _run(sc._send_stale_match_prompt(bot, item))
        chat_ids = {c.args[0] for c in bot.send_message.call_args_list}
        self.assertEqual(chat_ids, {111, 222})

    def test_one_bad_chat_does_not_block_the_rest(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[RuntimeError("blocked"), None])
        item = {"ref": "OLD1", "parcel": "P1", "valuer_name": "Jane", "stale_matches": ["NEW1"],
                "queued_at": datetime.now().isoformat()}
        with patch.object(sc, "ALLOWED_IDS", {111, 222}):
            _run(sc._send_stale_match_prompt(bot, item))
        self.assertEqual(bot.send_message.call_count, 2)


class TestScConfirmAndValuerPickKeyboards(unittest.TestCase):
    def test_confirm_keyboard_has_same_and_change(self):
        markup = sc._sc_confirm_keyboard("OLD1")
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("stalefix:same:OLD1", callbacks)
        self.assertIn("stalefix:changeval:OLD1", callbacks)

    def test_valuer_pick_keyboard_is_index_based(self):
        saved = [{"name": "Jane Doe", "uid": "u1"}, {"name": "John Smith", "uid": "u2"}]
        markup = sc._sc_valuer_pick_keyboard("OLD1", saved)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(callbacks, ["stalefix:setval:OLD1:0", "stalefix:setval:OLD1:1"])


class TestScFinalizeReplacement(unittest.TestCase):
    def test_removes_old_and_queues_new_carrying_over_fields(self):
        old_item = {"ref": "OLD1", "tag": "Queue", "assessor": "An Assessor",
                    "consideration": "1000000", "currency_code": "KES", "parcel": "P1"}
        with patch.object(sc, "mark_removed") as mock_remove, \
             patch.object(sc, "save_dlv_batch") as mock_save:
            sc._sc_finalize_replacement(old_item, "NEW1", "Jane Doe", "uid-1", "ACC1")
        mock_remove.assert_called_once_with({"OLD1"})
        new_item = mock_save.call_args[0][0][0]
        self.assertEqual(new_item["ref"], "NEW1")
        self.assertEqual(new_item["valuer_name"], "Jane Doe")
        self.assertEqual(new_item["valuer_uid"], "uid-1")
        self.assertEqual(new_item["valuer_acct"], "ACC1")
        self.assertEqual(new_item["tag"], "Queue")
        self.assertEqual(new_item["assessor"], "An Assessor")
        self.assertEqual(new_item["consideration"], "1000000")
        self.assertEqual(new_item["currency_code"], "KES")
        self.assertIn("queued_at", new_item)
        self.assertNotIn("parcel", new_item)   # not in the carried-over field set


class TestRunStaleCheck(unittest.TestCase):
    def _old_item(self, **overrides):
        item = {"ref": "OLD1", "valuer_name": "Jane Doe", "valuer_uid": "uid-1",
                "last_error": "Not found in DLV endpoint",
                "queued_at": (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")}
        item.update(overrides)
        return item

    def test_no_items_does_nothing(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch.object(sc, "load_dlv_batch", return_value=[]):
            _run(sc.run_stale_check(bot))
        bot.send_message.assert_not_called()

    def test_no_candidates_does_nothing(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        young_item = self._old_item(queued_at=datetime.now().isoformat(timespec="seconds"))
        with patch.object(sc, "load_dlv_batch", return_value=[young_item]), \
             patch.object(sc, "save_dlv_batch") as mock_save:
            _run(sc.run_stale_check(bot))
        bot.send_message.assert_not_called()
        mock_save.assert_not_called()   # early-returns before any write

    def test_tokens_unavailable_skips_without_removing_or_notifying(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        item = self._old_item()
        with patch.object(sc, "load_dlv_batch", return_value=[item]), \
             patch.object(sc, "_lu_current_valuer", new_callable=AsyncMock,
                          return_value={"tokens_available": False, "found": False, "parcel_number": None}), \
             patch.object(sc, "mark_removed") as mock_remove, \
             patch.object(sc, "save_dlv_batch") as mock_save:
            _run(sc.run_stale_check(bot))
        mock_remove.assert_not_called()
        bot.send_message.assert_not_called()
        # the item passes through unchanged (no stale_checked_at stamp)
        self.assertNotIn("stale_checked_at", mock_save.call_args[0][0][0])

    def test_found_live_just_stamps_checked_at(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        item = self._old_item()
        with patch.object(sc, "load_dlv_batch", return_value=[item]), \
             patch.object(sc, "_lu_current_valuer", new_callable=AsyncMock,
                          return_value={"tokens_available": True, "found": True, "parcel_number": None}), \
             patch.object(sc, "mark_removed") as mock_remove, \
             patch.object(sc, "save_dlv_batch") as mock_save:
            _run(sc.run_stale_check(bot))
        mock_remove.assert_not_called()
        bot.send_message.assert_not_called()
        self.assertIn("stale_checked_at", mock_save.call_args[0][0][0])

    def test_found_live_also_opportunistically_enriches_parcel(self):
        """The cross-stage lookup already ran, so a parcel it turned up is
        stamped onto the item even though the ref isn't actually stale —
        satisfies "enrich queued refs with parcel info" broadly, not just
        for refs on the removal/replacement path."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        item = self._old_item()   # no "parcel" field
        with patch.object(sc, "load_dlv_batch", return_value=[item]), \
             patch.object(sc, "_lu_current_valuer", new_callable=AsyncMock,
                          return_value={"tokens_available": True, "found": True, "parcel_number": "NBI/BLOCK5/9"}), \
             patch.object(sc, "save_dlv_batch") as mock_save:
            _run(sc.run_stale_check(bot))
        self.assertEqual(mock_save.call_args[0][0][0]["parcel"], "NBI/BLOCK5/9")

    def test_not_found_with_no_parcel_anywhere_is_removed_and_notified(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        item = self._old_item()   # no "parcel" field
        with patch.object(sc, "load_dlv_batch", return_value=[item]), \
             patch.object(sc, "_lu_current_valuer", new_callable=AsyncMock,
                          return_value={"tokens_available": True, "found": False, "parcel_number": None}), \
             patch.object(sc, "mark_removed") as mock_remove, \
             patch.object(sc, "save_dlv_batch") as mock_save, \
             patch.object(sc, "ALLOWED_IDS", {111}):
            _run(sc.run_stale_check(bot))
        mock_remove.assert_called_once_with(["OLD1"])
        saved_refs = [i["ref"] for i in mock_save.call_args[0][0]]
        self.assertNotIn("OLD1", saved_refs)
        bot.send_message.assert_awaited_once()
        text = bot.send_message.call_args[0][1]
        self.assertIn("OLD1", text)
        self.assertIn("no parcel number", text)

    def test_not_found_with_parcel_but_no_matches_is_removed_and_notified(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        item = self._old_item(parcel="NBI/BLOCK1/123")
        with patch.object(sc, "load_dlv_batch", return_value=[item]), \
             patch.object(sc, "_lu_current_valuer", new_callable=AsyncMock,
                          return_value={"tokens_available": True, "found": False, "parcel_number": None}), \
             patch.object(sc, "get_valid_tokens", return_value=TOKENS), \
             patch.object(sc, "_pl_search_parcel", return_value=[]), \
             patch.object(sc, "mark_removed") as mock_remove, \
             patch.object(sc, "save_dlv_batch"), \
             patch.object(sc, "ALLOWED_IDS", {111}):
            _run(sc.run_stale_check(bot))
        mock_remove.assert_called_once_with(["OLD1"])
        text = bot.send_message.call_args[0][1]
        self.assertIn("NBI/BLOCK1/123", text)
        self.assertIn("isn't showing up", text)

    def test_not_found_with_parcel_and_matches_sends_decision_prompt(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        item = self._old_item(parcel="NBI/BLOCK1/123")
        matches = [{"reference_number": "NEW1"}, {"reference_number": "NEW2"}]
        with patch.object(sc, "load_dlv_batch", return_value=[item]), \
             patch.object(sc, "_lu_current_valuer", new_callable=AsyncMock,
                          return_value={"tokens_available": True, "found": False, "parcel_number": None}), \
             patch.object(sc, "get_valid_tokens", return_value=TOKENS), \
             patch.object(sc, "_pl_search_parcel", return_value=matches), \
             patch.object(sc, "mark_removed") as mock_remove, \
             patch.object(sc, "save_dlv_batch") as mock_save, \
             patch.object(sc, "ALLOWED_IDS", {111}):
            _run(sc.run_stale_check(bot))
        mock_remove.assert_not_called()
        saved_item = mock_save.call_args[0][0][0]
        self.assertTrue(saved_item["stale_decision_pending"])
        self.assertEqual(saved_item["stale_matches"], ["NEW1", "NEW2"])
        bot.send_message.assert_awaited_once()
        call = bot.send_message.call_args
        self.assertIn("OLD1", call.args[1])
        self.assertIn("NBI/BLOCK1/123", call.args[1])
        callbacks = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("stalefix:use:OLD1:NEW1", callbacks)
        self.assertIn("stalefix:use:OLD1:NEW2", callbacks)

    def test_parcel_derived_from_lookup_result_when_item_has_none(self):
        """The parcel can come from the fresh _lu_current_valuer result even
        when the queue item itself never had one on record."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        item = self._old_item()   # no "parcel" field at all
        with patch.object(sc, "load_dlv_batch", return_value=[item]), \
             patch.object(sc, "_lu_current_valuer", new_callable=AsyncMock,
                          return_value={"tokens_available": True, "found": False, "parcel_number": "NBI/BLOCK9/1"}), \
             patch.object(sc, "get_valid_tokens", return_value=TOKENS), \
             patch.object(sc, "_pl_search_parcel", return_value=[{"reference_number": "NEW1"}]), \
             patch.object(sc, "mark_removed"), \
             patch.object(sc, "save_dlv_batch") as mock_save, \
             patch.object(sc, "ALLOWED_IDS", {111}):
            _run(sc.run_stale_check(bot))
        saved_item = mock_save.call_args[0][0][0]
        self.assertEqual(saved_item["parcel"], "NBI/BLOCK9/1")

    def test_match_list_is_capped_and_excludes_the_stale_ref_itself(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        item = self._old_item(parcel="P1")
        matches = [{"reference_number": "OLD1"}] + [{"reference_number": f"NEW{i}"} for i in range(8)]
        with patch.object(sc, "load_dlv_batch", return_value=[item]), \
             patch.object(sc, "_lu_current_valuer", new_callable=AsyncMock,
                          return_value={"tokens_available": True, "found": False, "parcel_number": None}), \
             patch.object(sc, "get_valid_tokens", return_value=TOKENS), \
             patch.object(sc, "_pl_search_parcel", return_value=matches), \
             patch.object(sc, "mark_removed"), \
             patch.object(sc, "save_dlv_batch") as mock_save, \
             patch.object(sc, "ALLOWED_IDS", {111}):
            _run(sc.run_stale_check(bot))
        saved_item = mock_save.call_args[0][0][0]
        self.assertEqual(len(saved_item["stale_matches"]), sc._STALE_MAX_MATCHES_SHOWN)
        self.assertNotIn("OLD1", saved_item["stale_matches"])

    def test_pending_decision_resends_without_researching(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        now = datetime.now()
        item = self._old_item(
            stale_decision_pending=True, stale_matches=["NEW1"],
            stale_checked_at=(now - timedelta(hours=25)).isoformat(timespec="seconds"),
        )
        with patch.object(sc, "load_dlv_batch", return_value=[item]), \
             patch.object(sc, "_lu_current_valuer", new_callable=AsyncMock) as mock_lookup, \
             patch.object(sc, "_pl_search_parcel") as mock_parcel_search, \
             patch.object(sc, "save_dlv_batch"), \
             patch.object(sc, "ALLOWED_IDS", {111}):
            _run(sc.run_stale_check(bot))
        mock_lookup.assert_not_called()
        mock_parcel_search.assert_not_called()
        bot.send_message.assert_awaited_once()

    def test_one_bad_ref_does_not_abort_the_whole_pass(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bad_item  = self._old_item(ref="BAD1")
        good_item = self._old_item(ref="GOOD1")

        def _lookup(ref):
            if ref == "BAD1":
                raise RuntimeError("boom")
            return {"tokens_available": True, "found": True, "parcel_number": None}

        with patch.object(sc, "load_dlv_batch", return_value=[bad_item, good_item]), \
             patch.object(sc, "_lu_current_valuer", side_effect=_lookup), \
             patch.object(sc, "save_dlv_batch") as mock_save:
            _run(sc.run_stale_check(bot))
        saved = mock_save.call_args[0][0]
        good_saved = next(i for i in saved if i["ref"] == "GOOD1")
        self.assertIn("stale_checked_at", good_saved)


class TestRecvStaleFixDecision(unittest.TestCase):
    def _item(self, **overrides):
        item = {"ref": "OLD1", "valuer_name": "Jane Doe", "valuer_uid": "uid-1", "valuer_acct": "ACC1",
                "stale_decision_pending": True, "stale_matches": ["NEW1"]}
        item.update(overrides)
        return item

    def test_remove_marks_removed(self):
        update = _make_query_update("stalefix:remove:OLD1")
        ctx = MagicMock()
        items = [self._item()]
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_dlv_batch", return_value=items), \
             patch.object(sc, "mark_removed") as mock_remove, \
             patch.object(sc, "save_dlv_batch") as mock_save:
            _run(sc.recv_stale_fix_decision(update, ctx))
        mock_remove.assert_called_once_with({"OLD1"})
        mock_save.assert_called_once()
        self.assertIn("removed", update.callback_query.edit_message_text.call_args[0][0])

    def test_remove_already_resolved(self):
        update = _make_query_update("stalefix:remove:OLD1")
        ctx = MagicMock()
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_dlv_batch", return_value=[]), \
             patch.object(sc, "mark_removed") as mock_remove:
            _run(sc.recv_stale_fix_decision(update, ctx))
        mock_remove.assert_not_called()
        self.assertIn("Already resolved", update.callback_query.edit_message_text.call_args[0][0])

    def test_use_stashes_replacement_ref_and_asks_same_or_change(self):
        update = _make_query_update("stalefix:use:OLD1:NEW1")
        ctx = MagicMock()
        items = [self._item()]
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_dlv_batch", return_value=items), \
             patch.object(sc, "save_dlv_batch") as mock_save:
            _run(sc.recv_stale_fix_decision(update, ctx))
        saved_item = mock_save.call_args[0][0][0]
        self.assertEqual(saved_item["stale_replacement_ref"], "NEW1")
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("NEW1", text)
        self.assertIn("Jane Doe", text)

    def test_same_finalizes_with_original_valuer(self):
        update = _make_query_update("stalefix:same:OLD1")
        ctx = MagicMock()
        items = [self._item(stale_replacement_ref="NEW1")]
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_dlv_batch", return_value=items), \
             patch.object(sc, "_sc_finalize_replacement") as mock_finalize:
            _run(sc.recv_stale_fix_decision(update, ctx))
        mock_finalize.assert_called_once_with(items[0], "NEW1", "Jane Doe", "uid-1", "ACC1")
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("NEW1", text)
        self.assertIn("Jane Doe", text)

    def test_same_without_a_stashed_replacement_ref_reports_already_resolved(self):
        update = _make_query_update("stalefix:same:OLD1")
        ctx = MagicMock()
        items = [self._item()]   # no stale_replacement_ref
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_dlv_batch", return_value=items), \
             patch.object(sc, "_sc_finalize_replacement") as mock_finalize:
            _run(sc.recv_stale_fix_decision(update, ctx))
        mock_finalize.assert_not_called()
        self.assertIn("Already resolved", update.callback_query.edit_message_text.call_args[0][0])

    def test_changeval_with_no_saved_valuers(self):
        update = _make_query_update("stalefix:changeval:OLD1")
        ctx = MagicMock()
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_dlv_batch", return_value=[self._item(stale_replacement_ref="NEW1")]), \
             patch.object(sc, "load_saved_valuers", return_value=[]):
            _run(sc.recv_stale_fix_decision(update, ctx))
        self.assertIn("No saved valuers", update.callback_query.edit_message_text.call_args[0][0])

    def test_changeval_shows_valuer_picker(self):
        update = _make_query_update("stalefix:changeval:OLD1")
        ctx = MagicMock()
        saved = [{"name": "John Smith", "uid": "u2", "account_number": "ACC2"}]
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_dlv_batch", return_value=[self._item(stale_replacement_ref="NEW1")]), \
             patch.object(sc, "load_saved_valuers", return_value=saved):
            _run(sc.recv_stale_fix_decision(update, ctx))
        kwargs = update.callback_query.edit_message_text.call_args.kwargs
        callbacks = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("stalefix:setval:OLD1:0", callbacks)

    def test_setval_finalizes_with_chosen_valuer(self):
        update = _make_query_update("stalefix:setval:OLD1:0")
        ctx = MagicMock()
        items = [self._item(stale_replacement_ref="NEW1")]
        saved = [{"name": "John Smith", "uid": "u2", "account_number": "ACC2"}]
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_dlv_batch", return_value=items), \
             patch.object(sc, "load_saved_valuers", return_value=saved), \
             patch.object(sc, "_sc_finalize_replacement") as mock_finalize:
            _run(sc.recv_stale_fix_decision(update, ctx))
        mock_finalize.assert_called_once_with(items[0], "NEW1", "John Smith", "u2", "ACC2")
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("John Smith", text)

    def test_setval_with_invalid_index_reports_already_resolved(self):
        update = _make_query_update("stalefix:setval:OLD1:9")
        ctx = MagicMock()
        items = [self._item(stale_replacement_ref="NEW1")]
        with patch.object(sc, "allowed", return_value=True), \
             patch.object(sc, "load_dlv_batch", return_value=items), \
             patch.object(sc, "load_saved_valuers", return_value=[]), \
             patch.object(sc, "_sc_finalize_replacement") as mock_finalize:
            _run(sc.recv_stale_fix_decision(update, ctx))
        mock_finalize.assert_not_called()
        self.assertIn("Already resolved", update.callback_query.edit_message_text.call_args[0][0])


class TestRegister(unittest.TestCase):
    def test_register_adds_three_handlers(self):
        app = MagicMock()
        sc.register(app)
        self.assertEqual(app.add_handler.call_count, 3)


if __name__ == "__main__":
    unittest.main()
