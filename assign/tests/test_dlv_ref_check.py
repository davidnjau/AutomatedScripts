#!/usr/bin/env python3
"""
Unit tests for dlv_ref_check.py — _dc_format_record's three explicit
checks (New Assignment via `workflow`, DLV Batch Queue via `queued_at`,
Hold Queue via `hold`) plus the shared status/valuer/timeline fields,
_dc_format_live_status's match/mismatch/skip rendering, and the
cmd_dlv_ref_check/recv_dc_ref conversation handlers, including the
"no record at all" vs "has a record" distinction and the live
cross-check that runs only for the latter.

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

    def test_queued_at_present_shows_yes_and_its_own_date_row(self):
        record = {"status": "queued", "queued_at": "2026-01-01T10:00:00"}
        block = dc._dc_format_record("R1", record)
        self.assertIn("DLV Batch Queue: ✅ Yes", block)
        self.assertIn("📅 Queued: 2026-01-01T10:00:00", block)

    def test_no_queued_at_omits_queued_date_row(self):
        record = {"status": "assigned"}
        block = dc._dc_format_record("R1", record)
        self.assertNotIn("📅 Queued", block)

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


class TestDcFormatLiveStatus(unittest.TestCase):
    """_dc_format_live_status — match/mismatch/skip rendering against
    _lu_current_valuer's structured return shape."""

    def test_no_tokens_shows_skip_note_not_an_error(self):
        live = {"tokens_available": False, "found": False, "valuer_name": None}
        text = dc._dc_format_live_status({"valuer_name": "Jane Doe"}, live)
        self.assertIn("Live check skipped", text)
        self.assertIn("no cached tokens", text)

    def test_not_found_live(self):
        live = {"tokens_available": True, "found": False, "valuer_name": None}
        text = dc._dc_format_live_status({"valuer_name": "Jane Doe"}, live)
        self.assertIn("Not found live", text)

    def test_found_but_no_valuer_officer_listed(self):
        live = {"tokens_available": True, "found": True, "valuer_name": None}
        text = dc._dc_format_live_status({"valuer_name": "Jane Doe"}, live)
        self.assertIn("No valuer officer listed yet", text)

    def test_matching_valuer_confirmed(self):
        live = {"tokens_available": True, "found": True, "valuer_name": "Jane Doe"}
        text = dc._dc_format_live_status({"valuer_name": "Jane Doe"}, live)
        self.assertIn("✅", text)
        self.assertIn("Confirmed", text)
        self.assertIn("Jane Doe", text)

    def test_match_is_case_and_whitespace_insensitive(self):
        live = {"tokens_available": True, "found": True, "valuer_name": "  jane doe  "}
        text = dc._dc_format_live_status({"valuer_name": "JANE DOE"}, live)
        self.assertIn("Confirmed", text)

    def test_mismatched_valuer_flags_taken_by_another(self):
        """The exact reported scenario: local record says one valuer,
        live search shows a different one actually holding it."""
        live = {"tokens_available": True, "found": True, "valuer_name": "LYNN NDUTA KABURU"}
        text = dc._dc_format_live_status({"valuer_name": "NEWTON MUCHEMI WAMBUGU"}, live)
        self.assertIn("TAKEN BY ANOTHER VALUER", text)
        self.assertIn("LYNN NDUTA KABURU", text)
        self.assertIn("NEWTON MUCHEMI WAMBUGU", text)

    def test_no_local_valuer_to_compare_shows_informational_only(self):
        """A record with no valuer_name at all (e.g. only ever queued,
        never assigned) has nothing to compare against — informational,
        not framed as a mismatch."""
        live = {"tokens_available": True, "found": True, "valuer_name": "Jane Doe"}
        text = dc._dc_format_live_status({}, live)
        self.assertIn("Currently held by", text)
        self.assertNotIn("TAKEN BY ANOTHER VALUER", text)

    def test_valuer_names_are_markdown_escaped(self):
        live = {"tokens_available": True, "found": True, "valuer_name": "Jane_Doe"}
        text = dc._dc_format_live_status({"valuer_name": "John_Roe"}, live)
        self.assertIn("Jane\\_Doe", text)
        self.assertIn("John\\_Roe", text)

    def test_dlv_forwarding_appended_as_second_line(self):
        live = {
            "tokens_available": True, "found": True, "valuer_name": "Jane Doe",
            "dlv_forwarded_by": "George Ruhara Maina", "dlv_forwarded_at": "2026-10-01T13:12:38.013344",
        }
        text = dc._dc_format_live_status({"valuer_name": "Jane Doe"}, live)
        self.assertIn("Confirmed", text)
        self.assertIn("📨 *DLV Forwarded:* George Ruhara Maina — 2026-10-01T13:12:38.013344", text)

    def test_no_forwarding_data_omits_the_line(self):
        live = {"tokens_available": True, "found": True, "valuer_name": "Jane Doe"}
        text = dc._dc_format_live_status({"valuer_name": "Jane Doe"}, live)
        self.assertNotIn("DLV Forwarded", text)

    def test_forwarding_shown_even_on_mismatch(self):
        live = {
            "tokens_available": True, "found": True, "valuer_name": "LYNN NDUTA KABURU",
            "dlv_forwarded_by": "George Ruhara Maina", "dlv_forwarded_at": "2026-10-01T13:12:38.013344",
        }
        text = dc._dc_format_live_status({"valuer_name": "NEWTON MUCHEMI WAMBUGU"}, live)
        self.assertIn("TAKEN BY ANOTHER VALUER", text)
        self.assertIn("DLV Forwarded", text)

    def test_forwarding_names_are_markdown_escaped(self):
        live = {"tokens_available": True, "found": True, "valuer_name": "Jane Doe",
                "dlv_forwarded_by": "George_Maina"}
        text = dc._dc_format_live_status({"valuer_name": "Jane Doe"}, live)
        self.assertIn("George\\_Maina", text)

    def test_forwarding_date_shown_without_actor_name(self):
        live = {"tokens_available": True, "found": True, "valuer_name": "Jane Doe",
                "dlv_forwarded_at": "2026-10-01T13:12:38.013344"}
        text = dc._dc_format_live_status({"valuer_name": "Jane Doe"}, live)
        self.assertIn("📨 *DLV Forwarded:* — — 2026-10-01T13:12:38.013344", text)


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
        no_tokens = {"tokens_available": False, "found": False, "valuer_name": None}
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value=store), \
             patch.object(dc, "_lu_current_valuer", new=AsyncMock(return_value=no_tokens)):
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
        no_tokens = {"tokens_available": False, "found": False, "valuer_name": None}
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value=store), \
             patch.object(dc, "_lu_current_valuer", new=AsyncMock(return_value=no_tokens)):
            result = _run(dc.recv_dc_ref(update, ctx))
        self.assertEqual(result, dc.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("New Assignment: 📋 Stamp Duty", sent_text)
        self.assertIn("DLV Batch Queue: ✅ Yes", sent_text)
        self.assertIn("Jane Doe", sent_text)

    def test_live_check_sends_interim_message_before_final_report(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        store = {"R1": {"status": "assigned", "valuer_name": "Jane Doe"}}
        confirmed = {"tokens_available": True, "found": True, "valuer_name": "Jane Doe"}
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value=store), \
             patch.object(dc, "_lu_current_valuer", new=AsyncMock(return_value=confirmed)):
            _run(dc.recv_dc_ref(update, ctx))
        texts = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn("Checking live status", texts[0])
        self.assertIn("Confirmed", texts[-1])

    def test_live_mismatch_surfaces_in_final_report(self):
        """The exact reported scenario, end to end: local record says one
        valuer, live search shows a different one actually holding it."""
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        store = {"R1": {"status": "assigned", "valuer_name": "NEWTON MUCHEMI WAMBUGU"}}
        taken = {"tokens_available": True, "found": True, "valuer_name": "LYNN NDUTA KABURU"}
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value=store), \
             patch.object(dc, "_lu_current_valuer", new=AsyncMock(return_value=taken)):
            _run(dc.recv_dc_ref(update, ctx))
        sent_text = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("TAKEN BY ANOTHER VALUER", sent_text)
        self.assertIn("LYNN NDUTA KABURU", sent_text)

    def test_no_record_at_all_skips_live_check_entirely(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_load_consolidated", return_value={}), \
             patch.object(dc, "_lu_current_valuer", new=AsyncMock()) as mock_live:
            _run(dc.recv_dc_ref(update, ctx))
        mock_live.assert_not_awaited()

    def test_too_many_refs_ends_conversation_without_list_handling(self):
        raw = "\n".join(f"R{i}" for i in range(dc._LIST_INPUT_MAX_ITEMS + 1))
        update = _make_update_with_message(raw)
        ctx = MagicMock()
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_dc_handle_ref_list", new_callable=AsyncMock) as mock_list:
            result = _run(dc.recv_dc_ref(update, ctx))
        mock_list.assert_not_called()
        self.assertEqual(result, dc.ConversationHandler.END)

    def test_multiple_refs_routes_to_list_mode(self):
        update = _make_update_with_message("R1\nR2")
        ctx = MagicMock()
        with patch.object(dc, "allowed", return_value=True), \
             patch.object(dc, "_dc_handle_ref_list", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = dc.ConversationHandler.END
            result = _run(dc.recv_dc_ref(update, ctx))
        mock_list.assert_called_once_with(update, ["R1", "R2"])
        self.assertEqual(result, dc.ConversationHandler.END)


class TestDcHandleRefList(unittest.TestCase):
    def test_no_record_and_has_record_both_reported(self):
        update = _make_update_with_message()
        store = {"R1": {"status": "queued", "queued_at": "2026-01-01T10:00:00"}}
        no_tokens = {"tokens_available": False, "found": False, "valuer_name": None}
        with patch.object(dc, "_load_consolidated", return_value=store), \
             patch.object(dc, "_lu_current_valuer", new=AsyncMock(return_value=no_tokens)):
            result = _run(dc._dc_handle_ref_list(update, ["R1", "R2"]))
        self.assertEqual(result, dc.ConversationHandler.END)
        sent_texts = [c.args[0] for c in update.message.reply_text.call_args_list]
        combined = "\n\n".join(sent_texts)
        self.assertIn("R1", combined)
        self.assertIn("DLV Batch Queue: ✅ Yes", combined)
        self.assertIn("R2", combined)
        self.assertIn("no record at all", combined)

    def test_no_record_ref_skips_live_check(self):
        update = _make_update_with_message()
        with patch.object(dc, "_load_consolidated", return_value={}), \
             patch.object(dc, "_lu_current_valuer", new=AsyncMock()) as mock_live:
            _run(dc._dc_handle_ref_list(update, ["R1"]))
        mock_live.assert_not_awaited()

    def test_has_record_ref_live_checked_and_mismatch_shown(self):
        update = _make_update_with_message()
        store = {"R1": {"status": "assigned", "valuer_name": "NEWTON MUCHEMI WAMBUGU"}}
        taken = {"tokens_available": True, "found": True, "valuer_name": "LYNN NDUTA KABURU"}
        with patch.object(dc, "_load_consolidated", return_value=store), \
             patch.object(dc, "_lu_current_valuer", new=AsyncMock(return_value=taken)):
            _run(dc._dc_handle_ref_list(update, ["R1"]))
        sent_texts = [c.args[0] for c in update.message.reply_text.call_args_list]
        combined = "\n\n".join(sent_texts)
        self.assertIn("TAKEN BY ANOTHER VALUER", combined)
        self.assertIn("LYNN NDUTA KABURU", combined)


if __name__ == "__main__":
    unittest.main()
