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

    def test_missing_file_defaults_to_batch_1_task_1_size_6(self):
        self.assertEqual(ic.load_incremental_counter(), {"batch_number": 1, "task_number": 1, "batch_size": 6})

    def test_set_incremental_counter_seeds_state_keeping_batch_size(self):
        ic.set_incremental_counter(2, 2)
        self.assertEqual(ic.load_incremental_counter(),
                          {"batch_number": 2, "task_number": 2, "batch_size": 6})

    def test_set_incremental_counter_can_also_change_batch_size(self):
        ic.set_incremental_counter(2, 2, batch_size=4)
        self.assertEqual(ic.load_incremental_counter(),
                          {"batch_number": 2, "task_number": 2, "batch_size": 4})
        self.assertEqual(ic.get_batch_size(), 4)

    def test_next_tag_advances_task_number(self):
        ic.set_incremental_counter(2, 2)
        tag = ic.next_incremental_tag()
        self.assertEqual(tag, "B2-T2")
        self.assertEqual(ic.load_incremental_counter(),
                          {"batch_number": 2, "task_number": 3, "batch_size": 6})

    def test_next_tag_wraps_batch_after_last_task_of_configured_size(self):
        ic.set_incremental_counter(2, 6)
        tag = ic.next_incremental_tag()
        self.assertEqual(tag, "B2-T6")
        self.assertEqual(ic.load_incremental_counter(),
                          {"batch_number": 3, "task_number": 1, "batch_size": 6})

    def test_smaller_batch_size_wraps_sooner(self):
        ic.set_incremental_counter(2, 4, batch_size=4)
        tag = ic.next_incremental_tag()
        self.assertEqual(tag, "B2-T4")
        self.assertEqual(ic.load_incremental_counter(),
                          {"batch_number": 3, "task_number": 1, "batch_size": 4})

    def test_sequential_calls_walk_through_a_full_batch(self):
        ic.set_incremental_counter(2, 2)
        tags = [ic.next_incremental_tag() for _ in range(5)]
        self.assertEqual(tags, ["B2-T2", "B2-T3", "B2-T4", "B2-T5", "B2-T6"])
        self.assertEqual(ic.load_incremental_counter(),
                          {"batch_number": 3, "task_number": 1, "batch_size": 6})


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
    def _full_cleared_batch(self, batch_number, size=6):
        return [{"ref": f"R{t}", "batch_number": batch_number, "task_number": t, "status": "cleared"}
                for t in range(1, size + 1)]

    def test_batch_eligible_when_all_6_cleared(self):
        grouped = {2: self._full_cleared_batch(2)}
        self.assertEqual(ic._ic_eligible_batches(grouped, batch_size=6), [2])

    def test_batch_not_eligible_when_one_still_queued(self):
        items = self._full_cleared_batch(2)
        items[0]["status"] = "queued"
        grouped = {2: items}
        self.assertEqual(ic._ic_eligible_batches(grouped, batch_size=6), [])

    def test_batch_not_eligible_when_missing_a_task_slot(self):
        items = self._full_cleared_batch(2)[:-1]   # only 5 of 6
        grouped = {2: items}
        self.assertEqual(ic._ic_eligible_batches(grouped, batch_size=6), [])

    def test_smaller_configured_batch_size_makes_a_4_task_batch_eligible(self):
        grouped = {2: self._full_cleared_batch(2, size=4)}
        self.assertEqual(ic._ic_eligible_batches(grouped, batch_size=4), [2])
        # ...but not eligible under the old size of 6
        self.assertEqual(ic._ic_eligible_batches(grouped, batch_size=6), [])

    def test_auto_close_flags_newly_eligible_only(self):
        grouped = {2: self._full_cleared_batch(2), 3: self._full_cleared_batch(3)}
        with tempfile.TemporaryDirectory() as tmpdir:
            closed_file = os.path.join(tmpdir, "closed.json")
            with patch.object(ic, "SAVED_INCREMENTAL_CLOSED_FILE", closed_file):
                ic.close_batch(2)   # batch 2 already closed beforehand
                newly = ic._ic_auto_close(grouped, batch_size=6)
                self.assertEqual(newly, [3])
                self.assertEqual(ic.load_closed_batches(), [2, 3])


class TestFormatReports(unittest.TestCase):
    def test_by_batch_report_empty(self):
        lines = ic._ic_format_by_batch_report({}, [], batch_size=6)
        self.assertIn("No incremental-tagged tasks yet", "\n".join(lines))

    def test_by_batch_report_shows_status_icons_and_closed_flag(self):
        grouped = {
            2: [
                {"ref": "REF1", "task_number": 2, "status": "queued", "valuer_name": "Jane Doe"},
                {"ref": "REF2", "task_number": 3, "status": "cleared", "valuer_name": "John Otieno"},
            ],
        }
        lines = "\n".join(ic._ic_format_by_batch_report(grouped, [2], batch_size=6))
        self.assertIn("*Batch 2* ✅ CLOSED — 2/6 tagged", lines)
        self.assertIn("2. 📌 *Ref:* `REF1`", lines)
        self.assertIn("📊 Status: ⏳ Queued", lines)
        self.assertIn("👤 Valuer: Jane Doe", lines)
        self.assertIn("3. 📌 *Ref:* `REF2`", lines)
        self.assertIn("📊 Status: ✅ Cleared", lines)
        self.assertIn("👤 Valuer: John Otieno", lines)

    def test_by_batch_report_uses_shared_labeled_block_format(self):
        """Regression: each task used to be a packed one-liner
        ("T2: `REF1` ⏳ Jane Doe") — it must now use the same
        task_block.format_labeled_block visual every other report uses."""
        grouped = {
            2: [{"ref": "REF1", "task_number": 2, "status": "queued", "valuer_name": "Jane Doe",
                 "assessor": "Jane Assessor", "consideration": "1000000", "currency_code": "KES", "parcel": "P1"}],
        }
        lines = "\n".join(ic._ic_format_by_batch_report(grouped, [], batch_size=6))
        self.assertIn("Assessor: Jane Assessor", lines)
        self.assertIn("💰 Consideration: KES 1,000,000.00", lines)
        self.assertIn("📋 Parcel: P1", lines)
        self.assertNotIn("T2: `REF1`", lines)

    def test_by_batch_report_reflects_configured_batch_size(self):
        grouped = {2: [{"ref": "REF1", "task_number": 2, "status": "queued", "valuer_name": "Jane Doe"}]}
        lines = "\n".join(ic._ic_format_by_batch_report(grouped, [], batch_size=4))
        self.assertIn("1/4 tagged", lines)

    def test_by_batch_report_no_closed_flag_when_not_closed(self):
        grouped = {2: [{"ref": "REF1", "task_number": 2, "status": "queued", "valuer_name": "Jane Doe"}]}
        lines = "\n".join(ic._ic_format_by_batch_report(grouped, [], batch_size=6))
        self.assertIn("*Batch 2* —", lines)
        self.assertNotIn("CLOSED", lines)

    def test_valuer_name_with_special_chars_is_escaped(self):
        grouped = {2: [{"ref": "REF1", "task_number": 2, "status": "queued", "valuer_name": "Jane_Doe"}]}
        lines = "\n".join(ic._ic_format_by_batch_report(grouped, [], batch_size=6))
        self.assertIn("Jane\\_Doe", lines)

    def test_cleared_report_empty(self):
        lines = ic._ic_format_cleared_report([], batch_size=6)
        self.assertIn("No cleared incremental-tagged tasks yet", "\n".join(lines))

    def test_cleared_report_groups_by_six_in_clearance_order(self):
        items = [
            {"ref": f"R{i}", "batch_number": 4, "task_number": i, "status": "cleared",
             "assigned_at": f"2026-07-{10 + i:02d} 09:00:00", "valuer_name": "Jane Doe"}
            for i in range(1, 8)   # 7 cleared items -> First Cleared (6) + Second Cleared (1)
        ]
        lines = "\n".join(ic._ic_format_cleared_report(items, batch_size=6))
        self.assertIn("*First Cleared* (6/6)", lines)
        self.assertIn("*Second Cleared* (1/6)", lines)

    def test_cleared_report_groups_by_configured_batch_size(self):
        items = [
            {"ref": f"R{i}", "batch_number": 4, "task_number": i, "status": "cleared",
             "assigned_at": f"2026-07-{10 + i:02d} 09:00:00", "valuer_name": "Jane Doe"}
            for i in range(1, 5)   # 4 cleared items, batch_size=4 -> exactly one full group
        ]
        lines = "\n".join(ic._ic_format_cleared_report(items, batch_size=4))
        self.assertIn("*First Cleared* (4/4)", lines)
        self.assertNotIn("Second Cleared", lines)

    def test_cleared_report_ignores_queued_items(self):
        items = [
            {"ref": "R1", "batch_number": 2, "task_number": 1, "status": "queued", "valuer_name": "Jane Doe"},
        ]
        lines = "\n".join(ic._ic_format_cleared_report(items, batch_size=6))
        self.assertIn("No cleared incremental-tagged tasks yet", lines)

    def test_cleared_report_mixes_original_batches_within_one_group(self):
        items = [
            {"ref": "A", "batch_number": 4, "task_number": 6, "status": "cleared",
             "assigned_at": "2026-07-10 09:00:00", "valuer_name": "Jane Doe"},
            {"ref": "B", "batch_number": 6, "task_number": 1, "status": "cleared",
             "assigned_at": "2026-07-11 09:00:00", "valuer_name": "Jane Doe"},
        ]
        lines = "\n".join(ic._ic_format_cleared_report(items, batch_size=6))
        self.assertIn("B4-T6", lines)
        self.assertIn("B6-T1", lines)
        self.assertIn("*First Cleared*", lines)

    def test_cleared_report_uses_shared_labeled_block_format(self):
        """Regression: each cleared item used to be a packed one-liner
        ("B4-T6: `A` — Jane Doe") — it must now use the same
        task_block.format_labeled_block visual every other report uses."""
        items = [
            {"ref": "A", "batch_number": 4, "task_number": 6, "status": "cleared",
             "assigned_at": "2026-07-10 09:00:00", "valuer_name": "Jane Doe",
             "assessor": "Jane Assessor", "consideration": "2000000", "currency_code": "KES", "parcel": "P2"},
        ]
        lines = "\n".join(ic._ic_format_cleared_report(items, batch_size=6))
        self.assertIn("1. 📌 *Ref:* `A`", lines)
        self.assertIn("🔢 Batch/Task: B4-T6", lines)
        self.assertIn("👤 Valuer: Jane Doe", lines)
        self.assertIn("Assessor: Jane Assessor", lines)
        self.assertIn("💰 Consideration: KES 2,000,000.00", lines)
        self.assertIn("📋 Parcel: P2", lines)
        self.assertNotIn("B4-T6: `A` —", lines)


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

    def test_setcounter_moves_to_set_size(self):
        update = _make_query_update("ic_menu:setcounter")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "get_batch_size", return_value=6):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.IC.SET_SIZE)

    def test_bybatch_sends_report_and_ends(self):
        update = _make_query_update("ic_menu:bybatch")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "get_batch_size", return_value=6), \
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
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "load_dlv_batch", return_value=[]), \
             patch.object(ic, "load_saved_assignments", return_value={}):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)

    def test_closebatch_with_no_eligible_batches_ends(self):
        update = _make_query_update("ic_menu:closebatch")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "get_batch_size", return_value=6), \
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
             patch.object(ic, "get_batch_size", return_value=6), \
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


class TestRecvIcSetSize(unittest.TestCase):
    def test_skip_keeps_current_batch_size(self):
        update = _make_message_update("skip")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "get_batch_size", return_value=6):
            result = _run(ic.recv_ic_set_size(update, ctx))
        self.assertEqual(result, ic.IC.SET_BATCH)
        self.assertEqual(ctx.user_data["ic_set_size"], 6)

    def test_non_numeric_reprompts(self):
        update = _make_message_update("abc")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_set_size(update, ctx))
        self.assertEqual(result, ic.IC.SET_SIZE)

    def test_zero_or_negative_reprompts(self):
        update = _make_message_update("0")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_set_size(update, ctx))
        self.assertEqual(result, ic.IC.SET_SIZE)

    def test_valid_size_moves_to_set_batch(self):
        update = _make_message_update("4")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_set_size(update, ctx))
        self.assertEqual(result, ic.IC.SET_BATCH)
        self.assertEqual(ctx.user_data["ic_set_size"], 4)


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
        ctx.user_data = {"ic_set_size": 4}
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_set_batch(update, ctx))
        self.assertEqual(result, ic.IC.SET_TASK)
        self.assertEqual(ctx.user_data["ic_set_batch"], 2)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("1-4", text)

    def test_task_out_of_range_for_configured_size_reprompts(self):
        update = _make_message_update("5")
        ctx = MagicMock()
        ctx.user_data = {"ic_set_batch": 2, "ic_set_size": 4}
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_set_task(update, ctx))
        self.assertEqual(result, ic.IC.SET_TASK)

    def test_valid_task_sets_counter_with_size_and_ends(self):
        update = _make_message_update("3")
        ctx = MagicMock()
        ctx.user_data = {"ic_set_batch": 2, "ic_set_size": 4}
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "set_incremental_counter") as mock_set:
            result = _run(ic.recv_ic_set_task(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)
        mock_set.assert_called_once_with(2, 3, 4)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("B2-T3", text)
        self.assertIn("batch size: 4", text)


class TestNotifyConfigAndStatePersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_file = os.path.join(self.tmpdir.name, "saved_incremental_notify_config.json")
        self.state_file = os.path.join(self.tmpdir.name, "saved_incremental_notify_state.json")
        self._patches = [
            patch.object(ic, "SAVED_INCREMENTAL_NOTIFY_CONFIG_FILE", self.config_file),
            patch.object(ic, "SAVED_INCREMENTAL_NOTIFY_STATE_FILE", self.state_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def test_missing_config_defaults_to_disabled(self):
        self.assertEqual(ic.load_notify_config(), {"enabled": False, "interval_minutes": 30, "emails": []})

    def test_save_and_load_config_roundtrip(self):
        ic.save_notify_config({"enabled": True, "interval_minutes": 60, "emails": ["a@b.com"]})
        self.assertEqual(ic.load_notify_config(), {"enabled": True, "interval_minutes": 60, "emails": ["a@b.com"]})

    def test_save_and_load_config_with_multiple_emails(self):
        ic.save_notify_config({"enabled": True, "interval_minutes": 60, "emails": ["a@b.com", "c@d.com"]})
        self.assertEqual(ic.load_notify_config(),
                          {"enabled": True, "interval_minutes": 60, "emails": ["a@b.com", "c@d.com"]})

    def test_legacy_singular_email_field_migrated_to_emails_list(self):
        ic.save_notify_config({"enabled": True, "interval_minutes": 30, "email": "old@b.com"})
        self.assertEqual(ic.load_notify_config()["emails"], ["old@b.com"])

    def test_legacy_empty_email_field_migrates_to_empty_list(self):
        ic.save_notify_config({"enabled": False, "interval_minutes": 30, "emails": []})
        self.assertEqual(ic.load_notify_config()["emails"], [])

    def test_missing_state_defaults_to_empty(self):
        self.assertEqual(ic.load_notify_state(),
                          {"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []})

    def test_save_and_load_state_roundtrip(self):
        ic.save_notify_state({"closed_batches": [1, 2], "reported_cleared_refs": ["R1"], "reported_batches": [1]})
        self.assertEqual(ic.load_notify_state(),
                          {"closed_batches": [1, 2], "reported_cleared_refs": ["R1"], "reported_batches": [1]})

    def test_state_saved_before_reported_batches_existed_defaults_it_to_empty(self):
        ic.save_notify_state({"closed_batches": [1], "reported_cleared_refs": ["R1"]})
        self.assertEqual(ic.load_notify_state()["reported_batches"], [])


class TestOpenBatches(unittest.TestCase):
    def test_excludes_closed_batches(self):
        grouped = {2: [{"ref": "R1"}], 3: [{"ref": "R2"}]}
        self.assertEqual(ic._ic_open_batches(grouped, [2]), {3: [{"ref": "R2"}]})

    def test_no_closed_batches_returns_all(self):
        grouped = {2: [{"ref": "R1"}], 3: [{"ref": "R2"}]}
        self.assertEqual(ic._ic_open_batches(grouped, []), grouped)

    def test_all_closed_returns_empty(self):
        grouped = {2: [{"ref": "R1"}]}
        self.assertEqual(ic._ic_open_batches(grouped, [2]), {})


class TestNewOpenBatches(unittest.TestCase):
    def test_excludes_already_reported_batches(self):
        open_batches = {2: [{"ref": "R1"}], 3: [{"ref": "R2"}]}
        self.assertEqual(ic._ic_new_open_batches(open_batches, [2]), {3: [{"ref": "R2"}]})

    def test_no_reported_batches_returns_all(self):
        open_batches = {2: [{"ref": "R1"}], 3: [{"ref": "R2"}]}
        self.assertEqual(ic._ic_new_open_batches(open_batches, []), open_batches)

    def test_all_already_reported_returns_empty(self):
        open_batches = {2: [{"ref": "R1"}]}
        self.assertEqual(ic._ic_new_open_batches(open_batches, [2]), {})

    def test_a_batch_stays_reported_even_though_still_open(self):
        """Regression: once shown, a batch must never resurface just
        because it's still open on a later cycle."""
        open_batches = {2: [{"ref": "R1"}], 3: [{"ref": "R2"}]}
        first_cycle = ic._ic_new_open_batches(open_batches, [])
        self.assertEqual(set(first_cycle), {2, 3})
        second_cycle = ic._ic_new_open_batches(open_batches, list(first_cycle))
        self.assertEqual(second_cycle, {})


class TestNewClearedItems(unittest.TestCase):
    def test_only_cleared_and_unreported_included(self):
        items = [
            {"ref": "R1", "status": "cleared", "assigned_at": "2026-07-10 09:00:00"},
            {"ref": "R2", "status": "queued", "assigned_at": ""},
            {"ref": "R3", "status": "cleared", "assigned_at": "2026-07-09 09:00:00"},
        ]
        new = ic._ic_new_cleared_items(items, already_reported=["R1"])
        self.assertEqual([i["ref"] for i in new], ["R3"])

    def test_sorted_by_assigned_at(self):
        items = [
            {"ref": "R1", "status": "cleared", "assigned_at": "2026-07-12 09:00:00"},
            {"ref": "R2", "status": "cleared", "assigned_at": "2026-07-10 09:00:00"},
        ]
        new = ic._ic_new_cleared_items(items, already_reported=[])
        self.assertEqual([i["ref"] for i in new], ["R2", "R1"])

    def test_nothing_new_returns_empty(self):
        items = [{"ref": "R1", "status": "cleared", "assigned_at": "2026-07-10 09:00:00"}]
        self.assertEqual(ic._ic_new_cleared_items(items, already_reported=["R1"]), [])


class TestFormatNewlyClearedSection(unittest.TestCase):
    def test_empty_shows_none(self):
        lines = "\n".join(ic._ic_format_newly_cleared_section([], batch_size=6))
        self.assertIn("_None._", lines)
        self.assertIn("0 task(s)", lines)

    def test_renders_labeled_blocks_grouped_by_batch_size(self):
        items = [
            {"ref": f"R{i}", "batch_number": 4, "task_number": i, "valuer_name": "Jane Doe"}
            for i in range(1, 8)   # 7 items, batch_size 6 -> 2 groups
        ]
        lines = "\n".join(ic._ic_format_newly_cleared_section(items, batch_size=6))
        self.assertIn("*Cleared Group 1* (6/6)", lines)
        self.assertIn("*Cleared Group 2* (1/6)", lines)
        self.assertIn("1. 📌 *Ref:* `R1`", lines)
        self.assertIn("🔢 Batch/Task: B4-T1", lines)
        self.assertIn("👤 Valuer: Jane Doe", lines)


class TestIcNotifyJob(unittest.TestCase):
    def _make_ctx(self):
        ctx = MagicMock()
        ctx.job.schedule_removal = MagicMock()
        ctx.bot.send_message = AsyncMock()
        return ctx

    def test_disabled_config_removes_job_and_does_nothing_else(self):
        ctx = self._make_ctx()
        with patch.object(ic, "load_notify_config", return_value={"enabled": False}):
            _run(ic._ic_notify_job(ctx))
        ctx.job.schedule_removal.assert_called_once()
        ctx.bot.send_message.assert_not_called()

    def test_nothing_new_skips_send(self):
        ctx = self._make_ctx()
        with patch.object(ic, "load_notify_config", return_value={"enabled": True, "interval_minutes": 30, "emails": []}), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=[]), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[]), \
             patch.object(ic, "load_notify_state", return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []}), \
             patch.object(ic, "save_notify_state") as mock_save:
            _run(ic._ic_notify_job(ctx))
        ctx.bot.send_message.assert_not_called()
        mock_save.assert_not_called()

    def test_newly_closed_batch_triggers_send_and_saves_state(self):
        ctx = self._make_ctx()
        items = [{"ref": "R1", "batch_number": 2, "task_number": 1, "status": "cleared", "valuer_name": "Jane Doe"}]
        with patch.object(ic, "load_notify_config", return_value={"enabled": True, "interval_minutes": 30, "emails": []}), \
             patch.object(ic, "ALLOWED_IDS", [111]), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=items), \
             patch.object(ic, "_ic_group_by_batch", return_value={2: items}), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[2]), \
             patch.object(ic, "load_notify_state", return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []}), \
             patch.object(ic, "save_notify_state") as mock_save:
            _run(ic._ic_notify_job(ctx))
        ctx.bot.send_message.assert_called()
        sent_text = "\n".join(call.args[1] for call in ctx.bot.send_message.call_args_list)
        self.assertIn("Newly filled: Batch 2", sent_text)
        mock_save.assert_called_once()
        saved_state = mock_save.call_args[0][0]
        self.assertEqual(saved_state["closed_batches"], [2])
        self.assertEqual(saved_state["reported_cleared_refs"], ["R1"])
        self.assertEqual(saved_state["reported_batches"], [])

    def test_new_open_batch_triggers_send_and_is_marked_reported(self):
        """A batch that just appeared (still open, nothing cleared/closed
        yet) must still trigger a send — it's reported exactly once."""
        ctx = self._make_ctx()
        items = [{"ref": "R1", "batch_number": 2, "task_number": 1, "status": "queued", "valuer_name": "Jane Doe"}]
        with patch.object(ic, "load_notify_config", return_value={"enabled": True, "interval_minutes": 30, "emails": []}), \
             patch.object(ic, "ALLOWED_IDS", [111]), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=items), \
             patch.object(ic, "_ic_group_by_batch", return_value={2: items}), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[]), \
             patch.object(ic, "load_notify_state", return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []}), \
             patch.object(ic, "save_notify_state") as mock_save:
            _run(ic._ic_notify_job(ctx))
        ctx.bot.send_message.assert_called()
        sent_text = "\n".join(call.args[1] for call in ctx.bot.send_message.call_args_list)
        self.assertIn("Available Batches", sent_text)
        self.assertIn("Batch 2", sent_text)
        saved_state = mock_save.call_args[0][0]
        self.assertEqual(saved_state["reported_batches"], [2])

    def test_already_reported_open_batch_does_not_retrigger_or_reappear(self):
        """Regression: once a batch's contents have been emailed, it must
        never be resent even though it's still open and nothing else
        changed — the assumption is it's either loaded to DLV Batch or
        ignored, so there's nothing left to say about it."""
        ctx = self._make_ctx()
        items = [{"ref": "R1", "batch_number": 2, "task_number": 1, "status": "queued", "valuer_name": "Jane Doe"}]
        with patch.object(ic, "load_notify_config", return_value={"enabled": True, "interval_minutes": 30, "emails": []}), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=items), \
             patch.object(ic, "_ic_group_by_batch", return_value={2: items}), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[]), \
             patch.object(ic, "load_notify_state",
                           return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": [2]}), \
             patch.object(ic, "save_notify_state") as mock_save:
            _run(ic._ic_notify_job(ctx))
        ctx.bot.send_message.assert_not_called()
        mock_save.assert_not_called()

    def test_new_cleared_item_triggers_send_without_newly_closed_batch(self):
        ctx = self._make_ctx()
        items = [{"ref": "R1", "batch_number": 2, "task_number": 1, "status": "cleared",
                  "valuer_name": "Jane Doe", "assigned_at": "2026-07-17 09:00:00"}]
        with patch.object(ic, "load_notify_config", return_value={"enabled": True, "interval_minutes": 30, "emails": []}), \
             patch.object(ic, "ALLOWED_IDS", [111]), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=items), \
             patch.object(ic, "_ic_group_by_batch", return_value={2: items}), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[]), \
             patch.object(ic, "load_notify_state", return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []}), \
             patch.object(ic, "save_notify_state") as mock_save:
            _run(ic._ic_notify_job(ctx))
        ctx.bot.send_message.assert_called()
        sent_text = "\n".join(call.args[1] for call in ctx.bot.send_message.call_args_list)
        self.assertIn("Newly Cleared", sent_text)
        self.assertIn("B2-T1", sent_text)
        mock_save.assert_called_once()

    def test_email_configured_sends_email(self):
        ctx = self._make_ctx()
        items = [{"ref": "R1", "batch_number": 2, "task_number": 1, "status": "cleared",
                  "valuer_name": "Jane Doe", "assigned_at": "2026-07-17 09:00:00"}]
        with patch.object(ic, "load_notify_config",
                           return_value={"enabled": True, "interval_minutes": 30, "emails": ["a@b.com"]}), \
             patch.object(ic, "ALLOWED_IDS", [111]), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=items), \
             patch.object(ic, "_ic_group_by_batch", return_value={2: items}), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[]), \
             patch.object(ic, "load_notify_state", return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []}), \
             patch.object(ic, "save_notify_state"), \
             patch.object(ic, "_send_auto_fetch_email") as mock_email:
            _run(ic._ic_notify_job(ctx))
        mock_email.assert_called_once()
        self.assertEqual(mock_email.call_args[0][0], "a@b.com")

    def test_multiple_emails_each_sent_independently(self):
        ctx = self._make_ctx()
        items = [{"ref": "R1", "batch_number": 2, "task_number": 1, "status": "cleared",
                  "valuer_name": "Jane Doe", "assigned_at": "2026-07-17 09:00:00"}]
        with patch.object(ic, "load_notify_config",
                           return_value={"enabled": True, "interval_minutes": 30,
                                         "emails": ["a@b.com", "c@d.com"]}), \
             patch.object(ic, "ALLOWED_IDS", [111]), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=items), \
             patch.object(ic, "_ic_group_by_batch", return_value={2: items}), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[]), \
             patch.object(ic, "load_notify_state", return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []}), \
             patch.object(ic, "save_notify_state"), \
             patch.object(ic, "_send_auto_fetch_email") as mock_email:
            _run(ic._ic_notify_job(ctx))
        self.assertEqual(mock_email.call_count, 2)
        recipients = [call.args[0] for call in mock_email.call_args_list]
        self.assertEqual(recipients, ["a@b.com", "c@d.com"])

    def test_one_bad_email_does_not_block_the_others(self):
        ctx = self._make_ctx()
        items = [{"ref": "R1", "batch_number": 2, "task_number": 1, "status": "cleared",
                  "valuer_name": "Jane Doe", "assigned_at": "2026-07-17 09:00:00"}]
        with patch.object(ic, "load_notify_config",
                           return_value={"enabled": True, "interval_minutes": 30,
                                         "emails": ["bad@b.com", "good@d.com"]}), \
             patch.object(ic, "ALLOWED_IDS", [111]), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=items), \
             patch.object(ic, "_ic_group_by_batch", return_value={2: items}), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[]), \
             patch.object(ic, "load_notify_state", return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []}), \
             patch.object(ic, "save_notify_state"), \
             patch.object(ic, "_send_auto_fetch_email",
                           side_effect=[Exception("bounced"), None]) as mock_email:
            _run(ic._ic_notify_job(ctx))   # must not raise
        self.assertEqual(mock_email.call_count, 2)

    def test_email_failure_is_logged_not_raised(self):
        ctx = self._make_ctx()
        items = [{"ref": "R1", "batch_number": 2, "task_number": 1, "status": "cleared",
                  "valuer_name": "Jane Doe", "assigned_at": "2026-07-17 09:00:00"}]
        with patch.object(ic, "load_notify_config",
                           return_value={"enabled": True, "interval_minutes": 30, "emails": ["a@b.com"]}), \
             patch.object(ic, "ALLOWED_IDS", [111]), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=items), \
             patch.object(ic, "_ic_group_by_batch", return_value={2: items}), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[]), \
             patch.object(ic, "load_notify_state", return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []}), \
             patch.object(ic, "save_notify_state"), \
             patch.object(ic, "_send_auto_fetch_email", side_effect=Exception("smtp down")):
            _run(ic._ic_notify_job(ctx))   # must not raise
        # failure notice sent to allowed chats in addition to the report
        sent_texts = [call.args[1] for call in ctx.bot.send_message.call_args_list]
        self.assertTrue(any("failed" in t for t in sent_texts))

    def test_no_duplicate_header_sent_per_chat(self):
        """Regression: an earlier draft sent a standalone header message
        before the chunked report, duplicating the header already at the
        top of `lines` — only one send per chat's single-chunk report."""
        ctx = self._make_ctx()
        items = [{"ref": "R1", "batch_number": 2, "task_number": 1, "status": "cleared",
                  "valuer_name": "Jane Doe", "assigned_at": "2026-07-17 09:00:00"}]
        with patch.object(ic, "load_notify_config", return_value={"enabled": True, "interval_minutes": 30, "emails": []}), \
             patch.object(ic, "ALLOWED_IDS", [111]), \
             patch.object(ic, "get_batch_size", return_value=6), \
             patch.object(ic, "_ic_gather_items", return_value=items), \
             patch.object(ic, "_ic_group_by_batch", return_value={2: items}), \
             patch.object(ic, "_ic_auto_close"), \
             patch.object(ic, "load_closed_batches", return_value=[]), \
             patch.object(ic, "load_notify_state", return_value={"closed_batches": [], "reported_cleared_refs": [], "reported_batches": []}), \
             patch.object(ic, "save_notify_state"):
            _run(ic._ic_notify_job(ctx))
        self.assertEqual(ctx.bot.send_message.call_count, 1)


class TestRecvIcNotifyMenu(unittest.TestCase):
    def test_cancel_ends_conversation(self):
        update = _make_query_update("ic_notify:cancel")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_notify_menu(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)

    def test_configure_moves_to_interval_state(self):
        update = _make_query_update("ic_notify:configure")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_notify_menu(update, ctx))
        self.assertEqual(result, ic.IC.NOTIFY_INTERVAL)

    def test_disable_saves_config_and_removes_job(self):
        update = _make_query_update("ic_notify:disable")
        ctx = MagicMock()
        job = MagicMock()
        ctx.job_queue.get_jobs_by_name.return_value = [job]
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "load_notify_config", return_value={"enabled": True, "interval_minutes": 30, "emails": []}), \
             patch.object(ic, "save_notify_config") as mock_save:
            result = _run(ic.recv_ic_notify_menu(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)
        mock_save.assert_called_once_with({"enabled": False, "interval_minutes": 30, "emails": []})
        job.schedule_removal.assert_called_once()


class TestRecvIcNotifyInterval(unittest.TestCase):
    def test_cancel_ends_conversation(self):
        update = _make_query_update("ic_notify_int:cancel")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_notify_interval(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)

    def test_valid_interval_stores_and_moves_to_email_state(self):
        update = _make_query_update("ic_notify_int:60")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ic, "allowed", return_value=True):
            result = _run(ic.recv_ic_notify_interval(update, ctx))
        self.assertEqual(result, ic.IC.NOTIFY_EMAIL)
        self.assertEqual(ctx.user_data["ic_notify_interval"], 60)


class TestRecvIcNotifyEmail(unittest.TestCase):
    def test_skip_saves_config_with_no_email(self):
        update = _make_message_update("skip")
        ctx = MagicMock()
        ctx.user_data = {"ic_notify_interval": 30}
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "save_notify_config") as mock_save, \
             patch.object(ic, "_ic_schedule_notify_job") as mock_schedule:
            result = _run(ic.recv_ic_notify_email(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)
        mock_save.assert_called_once_with({"enabled": True, "interval_minutes": 30, "emails": []})
        mock_schedule.assert_called_once_with(ctx.job_queue, 30)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("Telegram only", text)

    def test_email_provided_is_saved_and_scheduled(self):
        update = _make_message_update("a@b.com")
        ctx = MagicMock()
        ctx.user_data = {"ic_notify_interval": 60}
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "save_notify_config") as mock_save, \
             patch.object(ic, "_ic_schedule_notify_job") as mock_schedule:
            result = _run(ic.recv_ic_notify_email(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)
        mock_save.assert_called_once_with({"enabled": True, "interval_minutes": 60, "emails": ["a@b.com"]})
        mock_schedule.assert_called_once_with(ctx.job_queue, 60)

    def test_multiple_comma_separated_emails_are_saved_as_a_list(self):
        update = _make_message_update("a@b.com, c@d.com;e@f.com")
        ctx = MagicMock()
        ctx.user_data = {"ic_notify_interval": 30}
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "save_notify_config") as mock_save, \
             patch.object(ic, "_ic_schedule_notify_job"):
            result = _run(ic.recv_ic_notify_email(update, ctx))
        self.assertEqual(result, ic.ConversationHandler.END)
        mock_save.assert_called_once_with(
            {"enabled": True, "interval_minutes": 30, "emails": ["a@b.com", "c@d.com", "e@f.com"]})
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("Telegram + email", text)


class TestIcParseEmails(unittest.TestCase):
    def test_splits_on_comma(self):
        self.assertEqual(ic._ic_parse_emails("a@b.com,c@d.com"), ["a@b.com", "c@d.com"])

    def test_splits_on_semicolon(self):
        self.assertEqual(ic._ic_parse_emails("a@b.com;c@d.com"), ["a@b.com", "c@d.com"])

    def test_strips_whitespace(self):
        self.assertEqual(ic._ic_parse_emails(" a@b.com , c@d.com "), ["a@b.com", "c@d.com"])

    def test_dedupes_preserving_order(self):
        self.assertEqual(ic._ic_parse_emails("a@b.com,c@d.com,a@b.com"), ["a@b.com", "c@d.com"])

    def test_ignores_empty_segments(self):
        self.assertEqual(ic._ic_parse_emails("a@b.com,,c@d.com,"), ["a@b.com", "c@d.com"])

    def test_single_email_returns_single_item_list(self):
        self.assertEqual(ic._ic_parse_emails("a@b.com"), ["a@b.com"])

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(ic._ic_parse_emails(""), [])


class TestScheduleNotifyJob(unittest.TestCase):
    def test_removes_existing_job_before_scheduling(self):
        job_queue = MagicMock()
        stale_job = MagicMock()
        job_queue.get_jobs_by_name.return_value = [stale_job]
        ic._ic_schedule_notify_job(job_queue, 30)
        stale_job.schedule_removal.assert_called_once()
        job_queue.run_repeating.assert_called_once()
        _, kwargs = job_queue.run_repeating.call_args
        self.assertEqual(kwargs["interval"], 1800)
        self.assertEqual(kwargs["name"], ic._IC_NOTIFY_JOB_NAME)


class TestRecvIcMenuNotifyAction(unittest.TestCase):
    def test_notify_action_shows_status_and_moves_to_notify_menu(self):
        update = _make_query_update("ic_menu:notify")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "load_notify_config", return_value={"enabled": True, "interval_minutes": 30, "emails": ["a@b.com"]}):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.IC.NOTIFY_MENU)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Enabled", text)
        self.assertIn("a@b.com", text)

    def test_notify_action_shows_disabled_status(self):
        update = _make_query_update("ic_menu:notify")
        ctx = MagicMock()
        with patch.object(ic, "allowed", return_value=True), \
             patch.object(ic, "load_notify_config", return_value={"enabled": False, "interval_minutes": 30, "emails": []}):
            result = _run(ic.recv_ic_menu(update, ctx))
        self.assertEqual(result, ic.IC.NOTIFY_MENU)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Disabled", text)


if __name__ == "__main__":
    unittest.main()
