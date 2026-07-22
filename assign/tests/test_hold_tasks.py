#!/usr/bin/env python3
"""
Unit tests for hold_tasks.py — the persisted hold queue, the two candidate
sources (tracked assignments / live DLV query), the pure guard decision
(_ht_decide), the per-ref background check (_process_hold_item), the
repeating job's broadcast condition, and the conversation handlers.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlv_core
import hold_tasks as ht
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


def _run(coro):
    return asyncio.run(coro)


def _make_update_with_message(text=""):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_update_with_callback(data):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    query.message.chat_id = 123
    return update


# ── Persistence ──────────────────────────────────────────

class TestHoldTasksPersistence(unittest.TestCase):
    """load_hold_tasks/save_hold_tasks — adapters over dlv_core's
    consolidated ref-keyed store (Group A JSON consolidation), so the
    file constants to isolate live on dlv_core, not hold_tasks."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._patches = [
            patch.object(dlv_core, "SAVED_HOLD_TASKS_FILE", os.path.join(self.tmpdir.name, "saved_hold_tasks.json")),
            patch.object(dlv_core, "SAVED_DLV_BATCH_FILE", os.path.join(self.tmpdir.name, "saved_dlv_batch.json")),
            patch.object(dlv_core, "SAVED_DLV_CLOSED_FILE", os.path.join(self.tmpdir.name, "saved_dlv_closed.json")),
            patch.object(dlv_core, "SAVED_ASSIGNMENTS_FILE", os.path.join(self.tmpdir.name, "saved_assignments.json")),
            patch.object(dlv_core, "SAVED_DLV_RECORDS_FILE", os.path.join(self.tmpdir.name, "saved_dlv_records.json")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(ht.load_hold_tasks(), [])

    def test_save_then_load_roundtrip(self):
        items = [{"ref": "REG/TSFR/ABC123", "held_valuer_name": "Jane"}]
        ht.save_hold_tasks(items)
        self.assertEqual(ht.load_hold_tasks(), items)

    def test_release_via_clear_hold_and_remove_excludes_from_load(self):
        ht.save_hold_tasks([{"ref": "REG/TSFR/ABC123", "held_valuer_name": "Jane"}])
        dlv_core.clear_hold_and_remove(["REG/TSFR/ABC123"])
        self.assertEqual(ht.load_hold_tasks(), [])

    def test_save_hold_tasks_never_touches_a_ref_already_released_this_cycle(self):
        """Regression: save_hold_tasks(remaining) must not resurrect a ref
        clear_hold_and_remove already released this same cycle, even though
        the ref is (correctly) absent from `remaining`."""
        ht.save_hold_tasks([{"ref": "A", "held_valuer_name": "Jane"}, {"ref": "B", "held_valuer_name": "Jane"}])
        dlv_core.clear_hold_and_remove(["B"])
        ht.save_hold_tasks([{"ref": "A", "held_valuer_name": "Jane"}])
        self.assertEqual([i["ref"] for i in ht.load_hold_tasks()], ["A"])

    def test_save_hold_tasks_creates_a_bare_record_for_an_untracked_ref(self):
        """A ref picked from a live DLV query this bot never itself
        assigned still needs a store record to attach its hold onto."""
        ht.save_hold_tasks([{"ref": "NEW_REF", "held_valuer_name": "Jane"}])
        store = dlv_core._load_consolidated()
        self.assertEqual(store["NEW_REF"]["status"], "assigned")
        self.assertEqual(store["NEW_REF"]["hold"]["held_valuer_name"], "Jane")


class TestAddToHold(unittest.TestCase):
    """add_to_hold — dlv_batch.py's entry point for auto-enrolling freshly
    queued refs into the hold queue."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._patches = [
            patch.object(dlv_core, "SAVED_HOLD_TASKS_FILE", os.path.join(self.tmpdir.name, "saved_hold_tasks.json")),
            patch.object(dlv_core, "SAVED_DLV_BATCH_FILE", os.path.join(self.tmpdir.name, "saved_dlv_batch.json")),
            patch.object(dlv_core, "SAVED_DLV_CLOSED_FILE", os.path.join(self.tmpdir.name, "saved_dlv_closed.json")),
            patch.object(dlv_core, "SAVED_ASSIGNMENTS_FILE", os.path.join(self.tmpdir.name, "saved_assignments.json")),
            patch.object(dlv_core, "SAVED_DLV_RECORDS_FILE", os.path.join(self.tmpdir.name, "saved_dlv_records.json")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def test_enrolls_a_new_item(self):
        ht.add_to_hold([{"ref": "REG/TSFR/A", "valuer_name": "Jane Doe", "valuer_uid": "uid-1"}])
        held = ht.load_hold_tasks()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["ref"], "REG/TSFR/A")
        self.assertEqual(held[0]["held_valuer_name"], "Jane Doe")
        self.assertEqual(held[0]["held_valuer_uid"], "uid-1")

    def test_skips_a_ref_already_held(self):
        ht.save_hold_tasks([{"ref": "REG/TSFR/A", "held_valuer_name": "Original", "held_valuer_uid": "uid-0"}])
        ht.add_to_hold([{"ref": "REG/TSFR/A", "valuer_name": "Jane Doe", "valuer_uid": "uid-1"}])
        held = ht.load_hold_tasks()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["held_valuer_name"], "Original")

    def test_empty_items_is_a_no_op(self):
        ht.add_to_hold([])
        self.assertEqual(ht.load_hold_tasks(), [])

    def test_multiple_new_items_all_enrolled(self):
        ht.add_to_hold([
            {"ref": "A", "valuer_name": "Jane", "valuer_uid": "uid-1"},
            {"ref": "B", "valuer_name": "John", "valuer_uid": "uid-2"},
        ])
        refs = {i["ref"] for i in ht.load_hold_tasks()}
        self.assertEqual(refs, {"A", "B"})


# ── Candidate sources ────────────────────────────────────

class TestHtTrackedCandidates(unittest.TestCase):
    def test_builds_candidates_from_saved_assignments(self):
        assignments = {
            "REG/TSFR/A": {"valuer_name": "Jane Doe", "valuer_uid": "uid-1", "assigned_at": "2026-07-10"},
            "REG/TSFR/B": {"valuer_name": "John Roe", "valuer_uid": "uid-2", "assigned_at": "2026-07-11"},
        }
        with patch.object(ht, "load_saved_assignments", return_value=assignments):
            candidates = ht._ht_tracked_candidates(held_refs=set())
        self.assertEqual(len(candidates), 2)
        self.assertIn({"ref": "REG/TSFR/A", "valuer_name": "Jane Doe", "valuer_uid": "uid-1"}, candidates)

    def test_skips_already_held_refs(self):
        assignments = {"REG/TSFR/A": {"valuer_name": "Jane Doe", "valuer_uid": "uid-1"}}
        with patch.object(ht, "load_saved_assignments", return_value=assignments):
            candidates = ht._ht_tracked_candidates(held_refs={"REG/TSFR/A"})
        self.assertEqual(candidates, [])


class TestHtCurrentValuer(unittest.TestCase):
    def test_finds_valuation_officer(self):
        actors = [
            {"role": "ASSESSOR_OF_STAMP_DUTY", "user_details": {"id": "9", "names": "Some Assessor"}},
            {"role": "VALUATION OFFICER", "user_details": {"id": "42", "names": "Jane Doe"}},
        ]
        self.assertEqual(ht._ht_current_valuer(actors), {"id": "42", "names": "Jane Doe"})

    def test_no_valuation_officer_returns_none(self):
        actors = [{"role": "ASSESSOR_OF_STAMP_DUTY", "user_details": {"id": "9", "names": "Some Assessor"}}]
        self.assertIsNone(ht._ht_current_valuer(actors))

    def test_empty_actors_returns_none(self):
        self.assertIsNone(ht._ht_current_valuer([]))


# ── Guard decision (pure) ────────────────────────────────

class TestHtDecide(unittest.TestCase):
    OPEN_PENDING = {"bucket": "open", "node": "VALUATION_STAMP_DUTY_VALUER_REPORT"}
    OPEN_OTHER   = {"bucket": "open", "node": "VALUATION_STAMP_DUTY_CREATED"}
    CLOSED       = {"bucket": "closed", "node": "VALUATION_STAMP_DUTY_COMPLETED"}

    def test_not_found_releases(self):
        self.assertEqual(ht._ht_decide(False, None, None, "uid-1"), "release")

    def test_closed_releases(self):
        self.assertEqual(ht._ht_decide(True, self.CLOSED, {"id": "uid-1"}, "uid-1"), "release")

    def test_wrong_node_releases(self):
        self.assertEqual(ht._ht_decide(True, self.OPEN_OTHER, {"id": "uid-1"}, "uid-1"), "release")

    def test_same_valuer_keeps(self):
        self.assertEqual(ht._ht_decide(True, self.OPEN_PENDING, {"id": "uid-1"}, "uid-1"), "keep")

    def test_different_valuer_reverts(self):
        self.assertEqual(ht._ht_decide(True, self.OPEN_PENDING, {"id": "uid-2"}, "uid-1"), "revert")

    def test_id_type_mismatch_still_matches(self):
        # API ids can come back as int or str — comparison must not be type-sensitive.
        self.assertEqual(ht._ht_decide(True, self.OPEN_PENDING, {"id": 1}, "1"), "keep")

    def test_no_current_valuer_keeps(self):
        # Pending stage but no actor listed at all — nothing to revert against, so keep guarding.
        self.assertEqual(ht._ht_decide(True, self.OPEN_PENDING, None, "uid-1"), "keep")


# ── Per-ref background check ─────────────────────────────

class TestProcessHoldItem(unittest.TestCase):
    def setUp(self):
        self.item = {
            "ref": "REG/TSFR/ABC123", "held_valuer_name": "Jane Doe", "held_valuer_uid": "uid-1",
        }
        self.http_sess = MagicMock()

    def _run_item(self):
        return ht._process_hold_item(TOKENS, self.http_sess, dict(self.item))

    def test_not_found_is_released(self):
        with patch.object(ht, "_search_ref_dlv", return_value=None):
            result = self._run_item()
        self.assertFalse(result["keep"])
        self.assertIn("no longer found", result["line"])
        self.http_sess.post.assert_not_called()

    def test_empty_detail_is_kept_for_retry(self):
        with patch.object(ht, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(ht, "_fetch_ref_detail_dlv", return_value=None):
            result = self._run_item()
        self.assertTrue(result["keep"])
        self.assertIsNone(result["line"])

    def test_closed_is_released(self):
        with patch.object(ht, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(ht, "_fetch_ref_detail_dlv", return_value={"actors": []}), \
             patch.object(ht, "_classify_dlv_detail", return_value={
                 "bucket": "closed", "node": "VALUATION_STAMP_DUTY_COMPLETED",
             }):
            result = self._run_item()
        self.assertFalse(result["keep"])
        self.assertIn("released", result["line"])
        self.http_sess.post.assert_not_called()

    def test_same_valuer_is_kept_with_no_line(self):
        detail = {"actors": [{"role": "VALUATION OFFICER", "user_details": {"id": "uid-1", "names": "Jane Doe"}}]}
        with patch.object(ht, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(ht, "_fetch_ref_detail_dlv", return_value=detail), \
             patch.object(ht, "_classify_dlv_detail", return_value={
                 "bucket": "open", "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
             }):
            result = self._run_item()
        self.assertTrue(result["keep"])
        self.assertIsNone(result["line"])
        self.http_sess.post.assert_not_called()

    def test_takeover_reverts_and_posts_correct_body(self):
        detail = {"actors": [{"role": "VALUATION OFFICER", "user_details": {"id": "uid-2", "names": "John Roe"}}]}
        self.http_sess.post.return_value = MagicMock(raise_for_status=lambda: None)
        with patch.object(ht, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(ht, "_fetch_ref_detail_dlv", return_value=detail), \
             patch.object(ht, "_classify_dlv_detail", return_value={
                 "bucket": "open", "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
             }):
            result = self._run_item()
        self.assertTrue(result["keep"])
        self.assertIn("taken over by *John Roe*", result["line"])
        self.assertIn("reverted back to *Jane Doe*", result["line"])
        self.http_sess.post.assert_called_once()
        _, kwargs = self.http_sess.post.call_args
        self.assertEqual(kwargs["json"], {
            "reference_number":  "REG/TSFR/ABC123",
            "valuation_officer": "uid-1",
            "node":              "VALUATION_STAMP_DUTY_VALUER_REPORT",
        })

    def test_search_exception_is_kept_for_retry(self):
        with patch.object(ht, "_search_ref_dlv", side_effect=RuntimeError("boom")):
            result = self._run_item()
        self.assertTrue(result["keep"])
        self.assertIn("boom", result["item"]["last_error"])

    def test_takeover_names_with_markdown_special_chars_are_escaped(self):
        """Regression: an unescaped '_' in a valuer name raised
        telegram.error.BadRequest ("can't find end of the entity") and
        crashed the whole send. See common.md_escape."""
        self.item["held_valuer_name"] = "Jane_Doe"
        detail = {"actors": [{"role": "VALUATION OFFICER", "user_details": {"id": "uid-2", "names": "John_Roe"}}]}
        self.http_sess.post.return_value = MagicMock(raise_for_status=lambda: None)
        with patch.object(ht, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(ht, "_fetch_ref_detail_dlv", return_value=detail), \
             patch.object(ht, "_classify_dlv_detail", return_value={
                 "bucket": "open", "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
             }):
            result = self._run_item()
        self.assertIn("taken over by *John\\_Roe*", result["line"])
        self.assertIn("reverted back to *Jane\\_Doe*", result["line"])


class TestProcessHoldItems(unittest.TestCase):
    """_process_hold_items (plural) — the per-cycle driver; specifically
    that released refs go through clear_hold_and_remove before the trimmed
    list is saved, since save_hold_tasks never removes a ref on its own."""

    def test_released_refs_are_cleared_before_saving_the_trimmed_queue(self):
        items = [{"ref": "A", "held_valuer_name": "Jane"}, {"ref": "B", "held_valuer_name": "Jane"}]
        results = {
            "A": {"item": items[0], "keep": True, "line": None},
            "B": {"item": items[1], "keep": False, "line": "🔓 `B` — released"},
        }
        calls = []
        with patch.object(ht, "load_hold_tasks", return_value=items), \
             patch.object(ht, "build_session"), \
             patch.object(ht, "_process_hold_item", side_effect=lambda tokens, sess, item: results[item["ref"]]), \
             patch.object(ht, "clear_hold_and_remove", side_effect=lambda refs: calls.append(("clear_hold_and_remove", list(refs)))), \
             patch.object(ht, "save_hold_tasks", side_effect=lambda remaining: calls.append(("save_hold_tasks", remaining))):
            report = ht._process_hold_items(TOKENS)
        self.assertIn("released", report)
        self.assertEqual([c[0] for c in calls], ["clear_hold_and_remove", "save_hold_tasks"])
        self.assertEqual(calls[0][1], ["B"])
        self.assertEqual([i["ref"] for i in calls[1][1]], ["A"])

    def test_nothing_released_does_not_call_clear_hold_and_remove(self):
        items = [{"ref": "A", "held_valuer_name": "Jane"}]
        with patch.object(ht, "load_hold_tasks", return_value=items), \
             patch.object(ht, "build_session"), \
             patch.object(ht, "_process_hold_item", return_value={"item": items[0], "keep": True, "line": None}), \
             patch.object(ht, "clear_hold_and_remove") as mock_clear, \
             patch.object(ht, "save_hold_tasks"):
            ht._process_hold_items(TOKENS)
        mock_clear.assert_not_called()


class TestHtShowQueue(unittest.TestCase):
    """_ht_show_queue — the Held Tasks list; held_valuer_name must be
    escaped since it's free text, same crash class as _process_hold_item's
    takeover line."""

    def test_held_valuer_name_with_special_chars_is_escaped(self):
        edit_fn = AsyncMock()
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        items = [{"ref": "REF1", "held_valuer_name": "Jane_Doe"}]
        with patch.object(ht, "load_hold_tasks", return_value=items):
            _run(ht._ht_show_queue(edit_fn, 123, ctx))
        text = edit_fn.call_args[0][0]
        self.assertIn("Jane\\_Doe", text)


# ── Background job ───────────────────────────────────────

class TestHoldTasksJob(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.bot.send_message = AsyncMock()

    def test_empty_queue_skips_entirely(self):
        with patch.object(ht, "load_hold_tasks", return_value=[]), \
             patch.object(ht, "_any_valid_tokens") as mock_tokens:
            _run(ht._hold_tasks_job(self.ctx))
        mock_tokens.assert_not_called()
        self.ctx.bot.send_message.assert_not_called()

    def test_no_tokens_skips_without_crashing(self):
        with patch.object(ht, "load_hold_tasks", return_value=[{"ref": "X"}]), \
             patch.object(ht, "_any_valid_tokens", return_value=None):
            _run(ht._hold_tasks_job(self.ctx))
        self.ctx.bot.send_message.assert_not_called()

    def test_nothing_changed_does_not_broadcast(self):
        with patch.object(ht, "load_hold_tasks", return_value=[{"ref": "X"}]), \
             patch.object(ht, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(ht, "_process_hold_items", return_value=""):
            _run(ht._hold_tasks_job(self.ctx))
        self.ctx.bot.send_message.assert_not_called()

    def test_revert_broadcasts_to_every_allowed_id(self):
        with patch.object(ht, "load_hold_tasks", return_value=[{"ref": "X"}]), \
             patch.object(ht, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(ht, "_process_hold_items", return_value="🔁 `X` reverted"), \
             patch.object(ht, "ALLOWED_IDS", {111, 222}):
            _run(ht._hold_tasks_job(self.ctx))
        self.assertEqual(self.ctx.bot.send_message.call_count, 2)


# ── Conversation handlers ────────────────────────────────

class TestCmdHoldTasks(unittest.TestCase):
    def test_shows_menu_with_held_count(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ht, "allowed", return_value=True), \
             patch.object(ht, "load_hold_tasks", return_value=[{"ref": "X"}]):
            result = _run(ht.cmd_hold_tasks(update, ctx))
        self.assertEqual(result, ht.HT.MENU)
        update.message.reply_text.assert_called_once()
        self.assertIn("1", update.message.reply_text.call_args[0][0])


class TestRecvHtMenu(unittest.TestCase):
    def test_add_asks_for_source(self):
        update = _make_update_with_callback("ht_menu:add")
        ctx = MagicMock()
        result = _run(ht.recv_ht_menu(update, ctx))
        self.assertEqual(result, ht.HT.CHOOSE_SOURCE)

    def test_cancel_ends_conversation(self):
        update = _make_update_with_callback("ht_menu:cancel")
        ctx = MagicMock()
        result = _run(ht.recv_ht_menu(update, ctx))
        self.assertEqual(result, ht.ConversationHandler.END)

    def test_view_with_empty_queue_ends_conversation(self):
        update = _make_update_with_callback("ht_menu:view")
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with patch.object(ht, "load_hold_tasks", return_value=[]):
            result = _run(ht.recv_ht_menu(update, ctx))
        self.assertEqual(result, ht.ConversationHandler.END)


class TestRecvHtSource(unittest.TestCase):
    def test_tracked_source_lists_candidates(self):
        update = _make_update_with_callback("ht_src:tracked")
        ctx = MagicMock()
        ctx.user_data = {"ht_session": ht.HTSession()}
        assignments = {"REG/TSFR/A": {"valuer_name": "Jane Doe", "valuer_uid": "uid-1"}}
        with patch.object(ht, "load_hold_tasks", return_value=[]), \
             patch.object(ht, "load_saved_assignments", return_value=assignments):
            result = _run(ht.recv_ht_source(update, ctx))
        self.assertEqual(result, ht.HT.SELECT_CANDIDATES)
        sess = ctx.user_data["ht_session"]
        self.assertEqual(len(sess.candidates), 1)

    def test_live_source_with_no_cached_tokens_ends_conversation(self):
        update = _make_update_with_callback("ht_src:live")
        ctx = MagicMock()
        ctx.user_data = {"ht_session": ht.HTSession()}
        with patch.object(ht, "load_hold_tasks", return_value=[]), \
             patch.object(ht, "_be_cred_keyboard", return_value=None):
            result = _run(ht.recv_ht_source(update, ctx))
        self.assertEqual(result, ht.ConversationHandler.END)

    def test_cancel_ends_conversation(self):
        update = _make_update_with_callback("ht_src:cancel")
        ctx = MagicMock()
        result = _run(ht.recv_ht_source(update, ctx))
        self.assertEqual(result, ht.ConversationHandler.END)


class TestRecvHtSelectToggle(unittest.TestCase):
    def test_toggle_adds_and_removes_ref(self):
        update = _make_update_with_callback("ht_toggle:0")
        ctx = MagicMock()
        sess = ht.HTSession(candidates=[{"ref": "REG/TSFR/A", "valuer_name": "Jane", "valuer_uid": "uid-1"}])
        ctx.user_data = {"ht_session": sess}

        _run(ht.recv_ht_select_toggle(update, ctx))
        self.assertIn("REG/TSFR/A", sess.selected)

        _run(ht.recv_ht_select_toggle(update, ctx))
        self.assertNotIn("REG/TSFR/A", sess.selected)


class TestRecvHtConfirmAdd(unittest.TestCase):
    def test_confirm_persists_selected_and_skips_unselected(self):
        update = _make_update_with_callback("ht_confirm")
        ctx = MagicMock()
        sess = ht.HTSession(
            candidates=[
                {"ref": "REG/TSFR/A", "valuer_name": "Jane Doe", "valuer_uid": "uid-1"},
                {"ref": "REG/TSFR/B", "valuer_name": "John Roe", "valuer_uid": "uid-2"},
            ],
            selected={"REG/TSFR/A"},
        )
        ctx.user_data = {"ht_session": sess}
        with patch.object(ht, "load_hold_tasks", return_value=[]), \
             patch.object(ht, "save_hold_tasks") as mock_save:
            result = _run(ht.recv_ht_confirm_add(update, ctx))
        self.assertEqual(result, ht.ConversationHandler.END)
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["ref"], "REG/TSFR/A")
        self.assertEqual(saved[0]["held_valuer_uid"], "uid-1")

    def test_cancel_ends_without_saving(self):
        update = _make_update_with_callback("ht_cancel")
        ctx = MagicMock()
        ctx.user_data = {"ht_session": ht.HTSession()}
        with patch.object(ht, "save_hold_tasks") as mock_save:
            result = _run(ht.recv_ht_confirm_add(update, ctx))
        self.assertEqual(result, ht.ConversationHandler.END)
        mock_save.assert_not_called()

    def test_confirm_with_nothing_selected_reprompts(self):
        update = _make_update_with_callback("ht_confirm")
        ctx = MagicMock()
        sess = ht.HTSession(candidates=[{"ref": "REG/TSFR/A", "valuer_name": "Jane", "valuer_uid": "uid-1"}])
        ctx.user_data = {"ht_session": sess}
        result = _run(ht.recv_ht_confirm_add(update, ctx))
        self.assertEqual(result, ht.HT.SELECT_CANDIDATES)


class TestRecvHtQueueAction(unittest.TestCase):
    def test_interval_change_reschedules_job_by_name(self):
        update = _make_update_with_callback("htq:interval:120")
        ctx = MagicMock()
        job = MagicMock()
        ctx.job_queue.get_jobs_by_name.return_value = [job]
        # patch.object on the module global restores the original value on exit,
        # so this mutation (via recv_ht_queue_action's `global _hold_tasks_interval`)
        # doesn't leak into other tests.
        with patch.object(ht, "load_hold_tasks", return_value=[]), \
             patch.object(ht, "_hold_tasks_interval", 300):
            result = _run(ht.recv_ht_queue_action(update, ctx))
            self.assertEqual(ht._hold_tasks_interval, 120)
        self.assertEqual(result, ht.HT.VIEW_QUEUE)
        job.schedule_removal.assert_called_once()
        ctx.job_queue.run_repeating.assert_called_once()
        _, kwargs = ctx.job_queue.run_repeating.call_args
        self.assertEqual(kwargs["interval"], 120)
        self.assertEqual(kwargs["name"], "hold_tasks_job")

    def test_release_with_empty_queue_shows_message(self):
        update = _make_update_with_callback("htq:release")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ht, "load_hold_tasks", return_value=[]):
            result = _run(ht.recv_ht_queue_action(update, ctx))
        self.assertEqual(result, ht.HT.RELEASE_SELECT)
        sess = ctx.user_data["ht_session"]
        self.assertEqual(sess.release_items, [])

    def test_close_ends_conversation(self):
        update = _make_update_with_callback("htq:close")
        ctx = MagicMock()
        result = _run(ht.recv_ht_queue_action(update, ctx))
        self.assertEqual(result, ht.ConversationHandler.END)


class TestRecvHtReleaseConfirm(unittest.TestCase):
    def test_confirm_removes_only_selected_refs(self):
        update = _make_update_with_callback("ht_relconfirm")
        ctx = MagicMock()
        sess = ht.HTSession(release_selected={"REG/TSFR/A"})
        ctx.user_data = {"ht_session": sess}
        held = [{"ref": "REG/TSFR/A"}, {"ref": "REG/TSFR/B"}]
        with patch.object(ht, "load_hold_tasks", return_value=held), \
             patch.object(ht, "clear_hold_and_remove") as mock_clear, \
             patch.object(ht, "save_hold_tasks") as mock_save:
            result = _run(ht.recv_ht_release_confirm(update, ctx))
        self.assertEqual(result, ht.ConversationHandler.END)
        mock_save.assert_called_once_with([{"ref": "REG/TSFR/B"}])
        mock_clear.assert_called_once_with({"REG/TSFR/A"})

    def test_clears_hold_before_saving_the_trimmed_queue(self):
        """Regression: save_hold_tasks never removes a ref on its own — a
        bare manual release has no other status call, so clear_hold_and_remove
        must run, and specifically before save_hold_tasks."""
        update = _make_update_with_callback("ht_relconfirm")
        ctx = MagicMock()
        sess = ht.HTSession(release_selected={"REG/TSFR/A"})
        ctx.user_data = {"ht_session": sess}
        held = [{"ref": "REG/TSFR/A"}, {"ref": "REG/TSFR/B"}]
        calls = []
        with patch.object(ht, "load_hold_tasks", return_value=held), \
             patch.object(ht, "clear_hold_and_remove", side_effect=lambda refs: calls.append(("clear_hold_and_remove", set(refs)))), \
             patch.object(ht, "save_hold_tasks", side_effect=lambda items: calls.append(("save_hold_tasks", items))):
            _run(ht.recv_ht_release_confirm(update, ctx))
        self.assertEqual([c[0] for c in calls], ["clear_hold_and_remove", "save_hold_tasks"])

    def test_cancel_ends_without_saving(self):
        update = _make_update_with_callback("ht_relcancel")
        ctx = MagicMock()
        ctx.user_data = {"ht_session": ht.HTSession()}
        with patch.object(ht, "clear_hold_and_remove") as mock_clear, \
             patch.object(ht, "save_hold_tasks") as mock_save:
            result = _run(ht.recv_ht_release_confirm(update, ctx))
        self.assertEqual(result, ht.ConversationHandler.END)
        mock_save.assert_not_called()
        mock_clear.assert_not_called()


if __name__ == "__main__":
    unittest.main()
