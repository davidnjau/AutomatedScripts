#!/usr/bin/env python3
"""
Unit tests for dlv_incremental.py — the counter (next_incremental_tag,
batch wrapping), tag parsing, closed-batch persistence, data gathering/
grouping/auto-close, report formatting, and the conversation handlers.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlv_incremental as ic


def _run(coro):
    return asyncio.run(coro)


def _make_query_update(data):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.reply_text = AsyncMock()
    return update


def _make_message_update(text):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


class TestCounterPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.counter_file = os.path.join(self.tmpdir.name, "saved_incremental_counter.json")
        self._patch = patch.object(ic, "SAVED_INCREMENTAL_COUNTER_FILE", self.counter_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_missing_file_defaults_to_batch_1_task_1(self):
        self.assertEqual(ic.load_incremental_counter(), {"batch_number": 1, "task_number": 1})

    def test_set_incremental_counter_seeds_state(self):
        ic.set_incremental_counter(2, 2)
        self.assertEqual(ic.load_incremental_counter(), {"batch_number": 2, "task_number": 2})

    def test_next_tag_advances_task_number(self):
        ic.set_incremental_counter(2, 2)
        tag = ic.next_incremental_tag()
        self.assertEqual(tag, "B2-T2")
        self.assertEqual(ic.load_incremental_counter(), {"batch_number": 2, "task_number": 3})

    def test_next_tag_wraps_batch_after_task_6(self):
        ic.set_incremental_counter(2, 6)
        tag = ic.next_incremental_tag()
        self.assertEqual(tag, "B2-T6")
        self.assertEqual(ic.load_incremental_counter(), {"batch_number": 3, "task_number": 1})

    def test_sequential_calls_walk_through_a_full_batch(self):
        ic.set_incremental_counter(2, 2)
        tags = [ic.next_incremental_tag() for _ in range(5)]
        self.assertEqual(tags, ["B2-T2", "B2-T3", "B2-T4", "B2-T5", "B2-T6"])
        self.assertEqual(ic.load_incremental_counter(), {"batch_number": 3, "task_number": 1})


class TestParseIncrementalTag(unittest.TestCase):
    def test_valid_tag_parses(self):
        self.assertEqual(ic.parse_incremental_tag("B2-T3"), (2, 3))

    def test_fixed_tag_returns_none(self):
        self.assertIsNone(ic.parse_incremental_tag("Queue"))

    def test_empty_or_none_returns_none(self):
        self.assertIsNone(ic.parse_incremental_tag(""))
        self.assertIsNone(ic.parse_incremental_tag(None))


class TestClosedBatchPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.closed_file = os.path.join(self.tmpdir.name, "saved_incremental_closed_batches.json")
        self._patch = patch.object(ic, "SAVED_INCREMENTAL_CLOSED_FILE", self.closed_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(ic.load_closed_batches(), [])

    def test_close_batch_persists_and_is_idempotent(self):
        ic.close_batch(2)
        ic.close_batch(2)
        self.assertEqual(ic.load_closed_batches(), [2])

    def test_close_batch_multiple(self):
        ic.close_batch(2)
        ic.close_batch(1)
        self.assertEqual(ic.load_closed_batches(), [1, 2])


def _queued(ref, tag, **overrides):
    item = {"ref": ref, "valuer_name": "Jane Doe", "tag": tag}
    item.update(overrides)
    return item


class TestGatherAndGroup(unittest.TestCase):
    def test_only_incremental_tagged_items_included(self):
        with patch.object(ic, "load_dlv_batch", return_value=[
                _queued("REF1", "B2-T2"), _queued("REF2", "Queue"), _queued("REF3", ""),
             ]), \
             patch.object(ic, "load_saved_assignments", return_value={}):
            items = ic._ic_gather_items()
        self.assertEqual([i["ref"] for i in items], ["REF1"])
        self.assertEqual(items[0]["batch_number"], 2)
        self.assertEqual(items[0]["task_number"], 2)
        self.assertEqual(items[0]["status"], "queued")

    def test_assignment_items_marked_cleared(self):
        with patch.object(ic, "load_dlv_batch", return_value=[]), \
             patch.object(ic, "load_saved_assignments", return_value={
                "REF1": {"valuer_name": "Jane Doe", "tag": "B2-T3", "assigned_at": "2026-07-17 09:00:00"},
             }):
            items = ic._ic_gather_items()
        self.assertEqual(items[0]["ref"], "REF1")
        self.assertEqual(items[0]["status"], "cleared")
        self.assertEqual(items[0]["batch_number"], 2)
        self.assertEqual(items[0]["task_number"], 3)

    def test_cleared_wins_when_ref_in_both_sources(self):
        """Shouldn't normally happen, but if it does, the more-progressed
        (cleared) version must win over the stale queued one."""
        with patch.object(ic, "load_dlv_batch", return_value=[_queued("REF1", "B2-T2")]), \
             patch.object(ic, "load_saved_assignments", return_value={
                "REF1": {"valuer_name": "Jane Doe", "tag": "B2-T2", "assigned_at": "2026-07-17 09:00:00"},
             }):
            items = ic._ic_gather_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "cleared")

    def test_group_by_batch_sorts_by_task_number(self):
        items = [
            {"ref": "A", "batch_number": 2, "task_number": 4},
            {"ref": "B", "batch_number": 2, "task_number": 2},
            {"ref": "C", "batch_number": 3, "task_number": 1},
        ]
        grouped = ic._ic_group_by_batch(items)
        self.assertEqual([i["ref"] for i in grouped[2]], ["B", "A"])
        self.assertEqual([i["ref"] for i in grouped[3]], ["C"])


class TestEligibleAndAutoClose(unittest.TestCase):
    def _full_cleared_batch(self, batch_number):
        return [{"ref": f"R{t}", "batch_number": batch_number, "task_number": t, "status": "cleared"}
                for t in range(1, 7)]

    def test_batch_eligible_when_all_6_cleared(self):
        grouped = {2: self._full_cleared_batch(2)}
        self.assertEqual(ic._ic_eligible_batches(grouped), [2])

    def test_batch_not_eligible_when_one_still_queued(self):
        items = self._full_cleared_batch(2)
        items[0]["status"] = "queued"
        grouped = {2: items}
        self.assertEqual(ic._ic_eligible_batches(grouped), [])

    def test_batch_not_eligible_when_missing_a_task_slot(self):
        items = self._full_cleared_batch(2)[:-1]   # only 5 of 6
        grouped = {2: items}
        self.assertEqual(ic._ic_eligible_batches(grouped), [])

    def test_auto_close_flags_newly_eligible_only(self):
        grouped = {2: self._full_cleared_batch(2), 3: self._full_cleared_batch(3)}
        with tempfile.TemporaryDirectory() as tmpdir:
            closed_file = os.path.join(tmpdir, "closed.json")
            with patch.object(ic, "SAVED_INCREMENTAL_CLOSED_FILE", closed_file):
                ic.close_batch(2)   # batch 2 already closed beforehand
                newly = ic._ic_auto_close(grouped)
                self.assertEqual(newly, [3])
                self.assertEqual(ic.load_closed_batches(), [2, 3])


class TestFormatReports(unittest.TestCase):
    def test_by_batch_report_empty(self):
        lines = ic._ic_format_by_batch_report({}, [])
        self.assertIn("No incremental-tagged tasks yet", "\n".join(lines))

    def test_by_batch_report_shows_status_icons_and_closed_flag(self):
        grouped = {
            2: [
                {"ref": "REF1", "task_number": 2, "status": "queued", "valuer_name": "Jane Doe"},
                {"ref": "REF2", "task_number": 3, "status": "cleared", "valuer_name": "John Otieno"},
            ],
        }
        lines = "\n".join(ic._ic_format_by_batch_report(grouped, [2]))
        self.assertIn("*Batch 2* ✅ CLOSED", lines)
        self.assertIn("T2: `REF1` ⏳ Jane Doe", lines)
        self.assertIn("T3: `REF2` ✅ John Otieno", lines)

    def test_by_batch_report_no_closed_flag_when_not_closed(self):
        grouped = {2: [{"ref": "REF1", "task_number": 2, "status": "queued", "valuer_name": "Jane Doe"}]}
        lines = "\n".join(ic._ic_format_by_batch_report(grouped, []))
        self.assertIn("*Batch 2* —", lines)
        self.assertNotIn("CLOSED", lines)

    def test_valuer_name_with_special_chars_is_escaped(self):
        grouped = {2: [{"ref": "REF1", "task_number": 2, "status": "queued", "valuer_name": "Jane_Doe"}]}
        lines = "\n".join(ic._ic_format_by_batch_report(grouped, []))
        self.assertIn("Jane\\_Doe", lines)

    def test_cleared_report_empty(self):
        lines = ic._ic_format_cleared_report([])
        self.assertIn("No cleared incremental-tagged tasks yet", "\n".join(lines))

    def test_cleared_report_groups_by_six_in_clearance_order(self):
        items = [
            {"ref": f"R{i}", "batch_number": 4, "task_number": i, "status": "cleared",
             "assigned_at": f"2026-07-{10 + i:02d} 09:00:00", "valuer_name": "Jane Doe"}
            for i in range(1, 8)   # 7 cleared items -> First Cleared (6) + Second Cleared (1)
        ]
        lines = "\n".join(ic._ic_format_cleared_report(items))
        self.assertIn("*First Cleared* (6/6)", lines)
        self.assertIn("*Second Cleared* (1/6)", lines)

    def test_cleared_report_ignores_queued_items(self):
        items = [
            {"ref": "R1", "batch_number": 2, "task_number": 1, "status": "queued", "valuer_name": "Jane Doe"},
        ]
        lines = "\n".join(ic._ic_format_cleared_report(items))
        self.assertIn("No cleared incremental-tagged tasks yet", lines)

    def test_cleared_report_mixes_original_batches_within_one_group(self):
        items = [
            {"ref": "A", "batch_number": 4, "task_number": 6, "status": "cleared",
             "assigned_at": "2026-07-10 09:00:00", "valuer_name": "Jane Doe"},
            {"ref": "B", "batch_number": 6, "task_number": 1, "status": "cleared",
             "assigned_at": "2026-07-11 09:00:00", "valuer_name": "Jane Doe"},
        ]
        lines = "\n".join(ic._ic_format_cleared_report(items))
        self.assertIn("B4-T6", lines)
        self.assertIn("B6-T1", lines)
        self.assertIn("*First Cleared*", lines)


class TestCmdIncremental(unittest.TestCase):
    def test_shows_current_counter_position(self):
        update = _make_message_update("")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "load_incremental_counter", return_value={"batch_number": 2, "task_number": 3}):
            result = _run(ic.cmd_incremental(update, ctx))
        self.assertEqual(result, ic.IC.MENU)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("Batch 2", text)
        self.assertIn("Task 3", text)


class TestRecvIcMenu(unittest.TestCase):
    def test_cancel_ends_conversation(self):
        update = _make_query_update("ic_menu:cancel")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)

    def test_setcounter_moves_to_set_batch(self):
        update = _make_query_update("ic_menu:setcounter")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.IC.SET_BATCH)

    def test_bybatch_sends_report_and_ends(self):
        update = _make_query_update("ic_menu:bybatch")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "load_dlv_batch", return_value=[]), \
             patch.object(ic, "load_saved_assignments", return_value={}), \
             patch.object(ic, "load_closed_batches", return_value=[]):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)
        update.callback_query.message.reply_text.assert_called()

    def test_cleared_sends_report_and_ends(self):
        update = _make_query_update("ic_menu:cleared")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "load_dlv_batch", return_value=[]), \
             patch.object(ic, "load_saved_assignments", return_value={}):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)

    def test_closebatch_with_no_eligible_batches_ends(self):
        update = _make_query_update("ic_menu:closebatch")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "load_dlv_batch", return_value=[]), \
             patch.object(ic, "load_saved_assignments", return_value={}), \
             patch.object(ic, "load_closed_batches", return_value=[]):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)

    def test_closebatch_with_eligible_batch_shows_picker(self):
        update = _make_query_update("ic_menu:closebatch")
        ctx = MagicMock()
        assignments = {f"R{t}": {"tag": f"B2-T{t}", "assigned_at": "2026-07-17 09:00:00"} for t in range(1, 7)}
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "load_dlv_batch", return_value=[]), \
             patch.object(ic, "load_saved_assignments", return_value=assignments), \
             patch.object(ic, "load_closed_batches", return_value=[]):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.IC.CLOSE_PICK)


class TestRecvIcClosePick(unittest.TestCase):
    def test_cancel_ends_conversation(self):
        update = _make_query_update("ic_close:cancel")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_close_pick(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)

    def test_picking_a_batch_closes_it(self):
        update = _make_query_update("ic_close:2")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "close_batch") as mock_close:
            result = _run(ic.recv_ic_close_pick(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)
        mock_close.assert_called_once_with(2)


class TestRecvIcSetBatchAndTask(unittest.TestCase):
    def test_non_numeric_batch_reprompts(self):
        update = _make_message_update("abc")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_set_batch(update, ctx))
        self.assertEqual(result, ic.IC.SET_BATCH)

    def test_valid_batch_moves_to_set_task(self):
        update = _make_message_update("2")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_set_batch(update, ctx))
        self.assertEqual(result, ic.IC.SET_TASK)
        self.assertEqual(ctx.user_data["ic_set_batch"], 2)

    def test_task_out_of_range_reprompts(self):
        update = _make_message_update("7")
        ctx = MagicMock()
        ctx.user_data = {"ic_set_batch": 2}
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_set_task(update, ctx))
        self.assertEqual(result, ic.IC.SET_TASK)

    def test_valid_task_sets_counter_and_ends(self):
        update = _make_message_update("3")
        ctx = MagicMock()
        ctx.user_data = {"ic_set_batch": 2}
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "set_incremental_counter") as mock_set:
            result = _run(ic.recv_ic_set_task(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)
        mock_set.assert_called_once_with(2, 3)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("B2-T3", text)


if __name__ == "__main__":
    unittest.main()
