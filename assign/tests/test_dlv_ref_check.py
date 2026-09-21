#!/usr/bin/env python3
"""
Unit tests for dlv_ref_check.py — _dc_format_record's field rendering and
the cmd_dlv_ref_check/recv_dc_ref conversation handlers, including the
queued_at-is-the-signal distinction between "never had any record",
"has a record but was never queued via DLV Batch" (e.g. assigned
directly), and "was queued via DLV Batch".

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
    def test_queued_only_shows_queued_status_and_date(self):
        record = {"status": "queued", "queued_at": "2026-01-01T10:00:00"}
        block = dc._dc_format_record("R1", record)
        self.assertIn("R1", block)
        self.assertIn("Queued", block)
        self.assertIn("2026-01-01T10:00:00", block)

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

    def test_hold_info_is_included_when_present(self):
        record = {
            "status": "assigned", "queued_at": "2026-01-01T10:00:00",
            "hold": {"held_valuer_name": "Byron"},
        }
        block = dc._dc_format_record("R1", record)
        self.assertIn("Byron", block)


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

    def test_record_without_queued_at_reports_never_queued(self):
        """A ref assigned directly (New Assignment/Receive Tasks, never
        through DLV Batch) has a record but no queued_at."""
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        store = {"R1": {"status": "assigned", "assigned_at": "2026-01-02T09:00:00"}}
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value=store):
            result = _run(dc.recv_dc_ref(update, ctx))
        self.assertEqual(result, dc.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("never queued via DLV Batch", sent_text)
        self.assertIn("2026-01-02T09:00:00", sent_text)

    def test_record_with_queued_at_reports_queued_with_full_history(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        store = {"R1": {
            "status": "completed", "queued_at": "2026-01-01T10:00:00",
            "closed_at": "2026-01-05T12:00:00", "closed_reason": "completed",
            "valuer_name": "Jane Doe",
        }}
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value=store):
            result = _run(dc.recv_dc_ref(update, ctx))
        self.assertEqual(result, dc.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("was queued via DLV Batch", sent_text)
        self.assertIn("Jane Doe", sent_text)


if __name__ == "__main__":
    unittest.main()
