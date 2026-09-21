#!/usr/bin/env python3
"""
Unit tests for dlv_ref_check.py — _dc_format_record's three explicit
checks (New Assignment via `workflow`, DLV Batch Queue via `queued_at`,
Hold Queue via `hold`) plus the shared status/valuer/timeline fields, and
the cmd_dlv_ref_check/recv_dc_ref conversation handlers, including the
"no record at all" vs "has a record" distinction.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlv_ref_check as dc


def _run(coro):
    return asyncio.run(coro)


def _make_update_with_message(text=""):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


class TestDcFormatRecord(unittest.TestCase):
    def test_no_workflow_no_queued_no_hold_all_report_no(self):
        record = {"status": "assigned"}
        block = dc._dc_format_record("R1", record)
        self.assertIn("R1", block)
        self.assertIn("New Assignment: ❌ No", block)
        self.assertIn("DLV Batch Queue: ❌ No", block)
        self.assertIn("Hold Queue: ❌ No", block)

    def test_workflow_present_shows_new_assignment_label(self):
        record = {"status": "assigned", "workflow": "stamp_duty"}
        block = dc._dc_format_record("R1", record)
        self.assertIn("New Assignment: 📋 Stamp Duty", block)

    def test_land_rent_workflow_shows_its_own_label(self):
        record = {"status": "assigned", "workflow": "land_rent"}
        block = dc._dc_format_record("R1", record)
        self.assertIn("New Assignment: 🏘 Land Rent", block)

    def test_queued_at_present_shows_queue_date(self):
        record = {"status": "queued", "queued_at": "2026-01-01T10:00:00"}
        block = dc._dc_format_record("R1", record)
        self.assertIn("DLV Batch Queue: ✅ Yes — 2026-01-01T10:00:00", block)

    def test_hold_present_shows_held_for(self):
        record = {"status": "assigned", "hold": {"held_valuer_name": "Byron"}}
        block = dc._dc_format_record("R1", record)
        self.assertIn("Hold Queue: ✅ Currently held for Byron", block)

    def test_all_three_checks_can_be_true_together(self):
        record = {
            "status": "assigned", "workflow": "stamp_duty",
            "queued_at": "2026-01-01T10:00:00", "hold": {"held_valuer_name": "Byron"},
        }
        block = dc._dc_format_record("R1", record)
        self.assertIn("New Assignment: 📋 Stamp Duty", block)
        self.assertIn("DLV Batch Queue: ✅ Yes", block)
        self.assertIn("Hold Queue: ✅ Currently held for Byron", block)

    def test_assigned_shows_valuer_and_assigned_date(self):
        record = {
            "status": "assigned", "queued_at": "2026-01-01T10:00:00",
            "assigned_at": "2026-01-02T09:00:00", "valuer_name": "Jane Doe",
        }
        block = dc._dc_format_record("R1", record)
        self.assertIn("Jane Doe", block)
        self.assertIn("2026-01-02T09:00:00", block)

    def test_closed_completed_shows_closed_date_and_label(self):
        record = {
            "status": "completed", "queued_at": "2026-01-01T10:00:00",
            "closed_at": "2026-01-05T12:00:00", "closed_reason": "completed",
        }
        block = dc._dc_format_record("R1", record)
        self.assertIn("2026-01-05T12:00:00", block)
        self.assertIn("Completed", block)

    def test_closed_returned_shows_returned_label(self):
        record = {
            "status": "returned", "queued_at": "2026-01-01T10:00:00",
            "closed_at": "2026-01-05T12:00:00", "closed_reason": "returned",
        }
        block = dc._dc_format_record("R1", record)
        self.assertIn("Returned", block)

    def test_removed_shows_removed_date(self):
        record = {"status": "removed", "queued_at": "2026-01-01T10:00:00", "removed_at": "2026-01-03T00:00:00"}
        block = dc._dc_format_record("R1", record)
        self.assertIn("2026-01-03T00:00:00", block)

    def test_tag_is_included_when_present(self):
        record = {"status": "queued", "queued_at": "2026-01-01T10:00:00", "tag": "Direct"}
        block = dc._dc_format_record("R1", record)
        self.assertIn("Direct", block)


class TestCmdDlvRefCheck(unittest.TestCase):
    def test_asks_for_reference_number(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(dc, "allowed", return_value=True):
            result = _run(dc.cmd_dlv_ref_check(update, ctx))
        self.assertEqual(result, dc.DC.REF_INPUT)


class TestRecvDcRef(unittest.TestCase):
    def test_blank_ref_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        with patch.object(dc, "allowed", return_value=True):
            result = _run(dc.recv_dc_ref(update, ctx))
        self.assertEqual(result, dc.DC.REF_INPUT)

    def test_no_record_at_all_reports_no_history(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value={}):
            result = _run(dc.recv_dc_ref(update, ctx))
        self.assertEqual(result, dc.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("No record at all", sent_text)

    def test_record_with_no_matching_checks_still_shows_full_report(self):
        """A record can exist (e.g. seen via a live DLV search) without
        ever being a New Assignment, queued, or held — all three checks
        should still render, each reporting No."""
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        store = {"R1": {"status": "assigned", "assigned_at": "2026-01-02T09:00:00"}}
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value=store):
            result = _run(dc.recv_dc_ref(update, ctx))
        self.assertEqual(result, dc.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("New Assignment: ❌ No", sent_text)
        self.assertIn("DLV Batch Queue: ❌ No", sent_text)
        self.assertIn("Hold Queue: ❌ No", sent_text)
        self.assertIn("2026-01-02T09:00:00", sent_text)

    def test_record_with_all_history_shows_every_check_as_yes(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        store = {"R1": {
            "status": "completed", "workflow": "stamp_duty",
            "queued_at": "2026-01-01T10:00:00",
            "closed_at": "2026-01-05T12:00:00", "closed_reason": "completed",
            "valuer_name": "Jane Doe",
        }}
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value=store):
            result = _run(dc.recv_dc_ref(update, ctx))
        self.assertEqual(result, dc.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("New Assignment: 📋 Stamp Duty", sent_text)
        self.assertIn("DLV Batch Queue: ✅ Yes", sent_text)
        self.assertIn("Jane Doe", sent_text)


if __name__ == "__main__":
    unittest.main()
