#!/usr/bin/env python3
"""
Unit tests for dlv_batch.py — batch-input parsing, saved-valuer resolution,
and the per-ref processing state machine used by both the 5-minute job and
the "Query Now" DLV Queue action.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlv_batch
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


def _run(coro):
    return asyncio.run(coro)


def _make_query_update(data):
    """A MagicMock update whose callback_query has the given data and mocked
    async reply methods, matching the shape recv_db_* handlers expect."""
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.reply_text = AsyncMock()
    return update


class TestParseBatchInput(unittest.TestCase):
    def test_single_ref_single_valuer(self):
        groups = dlv_batch._parse_batch_input("REG/TSFR/ABC123 : John Kamau")
        self.assertEqual(groups, [{"refs": ["REG/TSFR/ABC123"], "valuer_name_raw": "John Kamau"}])

    def test_multiple_refs_one_line(self):
        groups = dlv_batch._parse_batch_input("REF1, REF2, REF3 : Byron")
        self.assertEqual(groups[0]["refs"], ["REF1", "REF2", "REF3"])

    def test_multiple_lines(self):
        text = "REF1 : Byron\nREF2, REF3 : Jane"
        groups = dlv_batch._parse_batch_input(text)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1]["valuer_name_raw"], "Jane")

    def test_refs_uppercased(self):
        groups = dlv_batch._parse_batch_input("reg/tsfr/abc123 : John")
        self.assertEqual(groups[0]["refs"], ["REG/TSFR/ABC123"])

    def test_line_without_colon_is_skipped(self):
        groups = dlv_batch._parse_batch_input("REF1 REF2 John\nREF3 : Jane")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["valuer_name_raw"], "Jane")

    def test_blank_lines_skipped(self):
        groups = dlv_batch._parse_batch_input("\n\nREF1 : John\n\n")
        self.assertEqual(len(groups), 1)

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(dlv_batch._parse_batch_input(""), [])

    def test_missing_valuer_name_is_skipped(self):
        groups = dlv_batch._parse_batch_input("REF1 : ")
        self.assertEqual(groups, [])

    def test_whitespace_trimmed(self):
        groups = dlv_batch._parse_batch_input("  REF1 ,  REF2   :   Jane Doe  ")
        self.assertEqual(groups[0]["refs"], ["REF1", "REF2"])
        self.assertEqual(groups[0]["valuer_name_raw"], "Jane Doe")


class TestResolveValuerFromSaved(unittest.TestCase):
    def test_case_insensitive_substring_match(self):
        saved = [{"name": "JOHN KAMAU MWANGI", "uid": "1", "account_number": "A1"}]
        with patch.object(dlv_batch, "load_saved_valuers", return_value=saved):
            result = dlv_batch._resolve_valuer_from_saved("kamau")
        self.assertEqual(result["uid"], "1")

    def test_no_match_returns_none(self):
        saved = [{"name": "JOHN KAMAU", "uid": "1", "account_number": "A1"}]
        with patch.object(dlv_batch, "load_saved_valuers", return_value=saved):
            result = dlv_batch._resolve_valuer_from_saved("nonexistent")
        self.assertIsNone(result)

    def test_empty_saved_list(self):
        with patch.object(dlv_batch, "load_saved_valuers", return_value=[]):
            self.assertIsNone(dlv_batch._resolve_valuer_from_saved("anyone"))


class TestProcessDlvBatchItem(unittest.TestCase):
    def setUp(self):
        self.item = {"ref": "REG/TSFR/ABC123", "valuer_name": "Jane Doe", "valuer_uid": "uid-1"}
        self.http_sess = MagicMock()
        self.assign_url = "https://example/assign"
        self.auth_hdrs = {}

    def _run(self):
        return dlv_batch._process_dlv_batch_item(
            TOKENS, self.http_sess, self.assign_url, self.auth_hdrs, dict(self.item)
        )

    def test_not_found_in_dlv_is_kept_for_retry(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value=None):
            result = self._run()
        self.assertTrue(result["keep"])
        self.assertIsNone(result["outcome"])
        self.assertEqual(result["item"]["last_error"], "Not found in DLV endpoint")

    def test_empty_detail_is_kept_for_retry(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value=None):
            result = self._run()
        self.assertTrue(result["keep"])

    def test_completed_closes_the_item(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1", "_request_type": "STAMP_DUTY"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={"node": "VALUATION_STAMP_DUTY_COMPLETED"}), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "closed", "closed_reason": "completed", "application_status": "COMPLETED",
                 "node": "VALUATION_STAMP_DUTY_COMPLETED", "assessor_name": "", "consideration_amount": "",
                 "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertFalse(result["keep"])
        self.assertIsNotNone(result["closed"])
        self.assertEqual(result["closed"]["closed_reason"], "completed")
        self.assertIn("Closed (Completed)", result["outcome"]["status"])

    def test_returned_closes_the_item(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={"node": "X"}), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "closed", "closed_reason": "returned", "application_status": "RETURNED",
                 "node": "VALUATION_STAMP_DUTY_CREATED", "assessor_name": "", "consideration_amount": "",
                 "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertIn("Closed (Returned)", result["outcome"]["status"])
        self.assertEqual(result["closed"]["closed_reason"], "returned")

    def test_open_created_node_assigns(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={"node": "VALUATION_STAMP_DUTY_CREATED"}), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_CREATED", "assessor_name": "Jane Assessor",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }), \
             patch.object(dlv_batch, "persist_assignment") as mock_persist:
            self.http_sess.post.return_value = MagicMock(raise_for_status=lambda: None)
            result = self._run()
        self.assertFalse(result["keep"])
        self.assertIn("Assigned", result["outcome"]["status"])
        self.assertEqual(result["item"]["assessor"], "Jane Assessor")
        mock_persist.assert_called_once_with("REG/TSFR/ABC123", "Jane Doe", "uid-1", extra={
            "valuer_acct": "", "tag": "", "assessor": "Jane Assessor", "parcel": "",
            "consideration": "", "currency_code": "", "queued_at": "",
        })

    def test_assign_persists_the_queue_items_own_context(self):
        """Once assigned, the ref drops out of saved_dlv_batch.json for good —
        persist_assignment's extra is the only place its parcel/consideration/
        tag/valuer_acct survive to."""
        self.item = {
            "ref": "CNTYINV/X4LNTSVRPT", "valuer_name": "Jane Doe", "valuer_uid": "uid-1",
            "valuer_acct": "SE0C17N708", "queued_at": "2026-07-16T11:34:41", "tag": "Queue",
            "parcel": "I.R 81948", "consideration": "40000000.000", "currency_code": "KES",
        }
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={"node": "VALUATION_STAMP_DUTY_CREATED"}), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_CREATED", "assessor_name": "",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }), \
             patch.object(dlv_batch, "persist_assignment") as mock_persist:
            self.http_sess.post.return_value = MagicMock(raise_for_status=lambda: None)
            self._run()
        mock_persist.assert_called_once_with("CNTYINV/X4LNTSVRPT", "Jane Doe", "uid-1", extra={
            "valuer_acct": "SE0C17N708", "tag": "Queue", "assessor": "", "parcel": "I.R 81948",
            "consideration": "40000000.000", "currency_code": "KES", "queued_at": "2026-07-16T11:34:41",
        })
        self.http_sess.post.assert_called_once()

    def test_already_assigned_to_someone_else_flags_awaiting_decision(self):
        """Regression: this used to be dropped from the queue silently
        (keep=False) — now it's kept, flagged awaiting_decision, and the
        actor's uid is captured for a later Reassign decision (see
        recv_db_taken_decision)."""
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
                 "actors": [{"role": "VALUATION OFFICER", "user_details": {"id": "uid-2", "names": "EXISTING VALUER"}}],
             }), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "assessor_name": "",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertTrue(result["keep"])
        self.assertIn("Taken by another valuer", result["outcome"]["status"])
        self.assertEqual(result["outcome"]["held_by"], "EXISTING VALUER")
        self.assertEqual(result["outcome"]["valuer_name"], "Jane Doe")
        self.assertTrue(result["item"]["awaiting_decision"])
        self.assertEqual(result["item"]["held_by"], "EXISTING VALUER")
        self.assertEqual(result["item"]["held_by_uid"], "uid-2")
        self.http_sess.post.assert_not_called()

    def test_already_assigned_to_intended_valuer_reports_success(self):
        # The item's own valuer_uid ("uid-1") matches the actor's id — this is
        # the case that used to render identically to "taken by someone else",
        # reading as a failure even though the intended valuer already has it.
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
                 "actors": [{"role": "VALUATION OFFICER", "user_details": {"id": "uid-1", "names": "Jane Doe"}}],
             }), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "assessor_name": "",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertFalse(result["keep"])
        self.assertIn("✅", result["outcome"]["status"])
        self.assertIn("Already correctly assigned", result["outcome"]["status"])
        self.assertEqual(result["outcome"]["valuer_name"], "Jane Doe")
        self.http_sess.post.assert_not_called()

    def test_no_valuation_officer_actor_reports_no_actor_listed(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
                 "actors": [{"role": "ASSESSOR_OF_STAMP_DUTY", "user_details": {"id": "uid-9", "names": "Some Assessor"}}],
             }), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "assessor_name": "",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertFalse(result["keep"])
        self.assertIn("no actor listed", result["outcome"]["status"])
        self.http_sess.post.assert_not_called()

    def test_search_exception_is_kept_for_retry(self):
        with patch.object(dlv_batch, "_search_ref_dlv", side_effect=RuntimeError("boom")):
            result = self._run()
        self.assertTrue(result["keep"])
        self.assertIn("boom", result["item"]["last_error"])


class TestDbFormatOutcomeBlock(unittest.TestCase):
    """_db_format_outcome_block — see task_block.format_labeled_block for
    the shared visual; Status (+ Valuer, + who currently holds it when
    that's why it was skipped) is genuinely all this report has to say."""

    def test_status_and_valuer_shown(self):
        outcome = {"ref": "REG/TSFR/ABC123", "status": "✅ Assigned", "valuer_name": "Jane Doe", "held_by": ""}
        block = dlv_batch._db_format_outcome_block(1, outcome)
        self.assertEqual(
            block,
            "  1. 📌 *Ref:* `REG/TSFR/ABC123`\n"
            "     📊 Status: ✅ Assigned\n"
            "     👤 Valuer: Jane Doe",
        )

    def test_held_by_shown_only_when_present(self):
        outcome = {"ref": "REF1", "status": "⚠️ Taken by another valuer — skipped (not reassigned)",
                   "valuer_name": "Jane Doe", "held_by": "EXISTING VALUER"}
        block = dlv_batch._db_format_outcome_block(1, outcome)
        self.assertIn("👤 Valuer: Jane Doe", block)
        self.assertIn("🔒 Currently held by: EXISTING VALUER", block)

    def test_no_valuer_field_when_empty(self):
        outcome = {"ref": "REF1", "status": "🔒 Closed (Completed)", "valuer_name": "", "held_by": ""}
        block = dlv_batch._db_format_outcome_block(1, outcome)
        self.assertNotIn("👤 Valuer", block)
        self.assertNotIn("🔒 Currently held by", block)


class TestProcessDlvBatchItems(unittest.TestCase):
    """_process_dlv_batch_items — aggregates per-ref outcomes into a list of
    numbered blocks (for _send_chunked_report, no manual truncation), and
    separately returns refs newly flagged awaiting_decision this cycle."""

    def _item(self, ref, valuer_name="Jane Doe", **overrides):
        item = {"ref": ref, "valuer_name": valuer_name, "valuer_uid": "uid-1"}
        item.update(overrides)
        return item

    def test_empty_queue_returns_empty_list(self):
        with patch.object(dlv_batch, "load_dlv_batch", return_value=[]):
            self.assertEqual(dlv_batch._process_dlv_batch_items(TOKENS), ([], []))

    def test_completed_outcomes_become_numbered_blocks(self):
        items = [self._item("REF1"), self._item("REF2")]
        with patch.object(dlv_batch, "load_dlv_batch", return_value=items), \
             patch.object(dlv_batch, "save_dlv_batch"), \
             patch.object(dlv_batch, "build_session", return_value=MagicMock()), \
             patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={"node": "VALUATION_STAMP_DUTY_CREATED"}), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_CREATED", "assessor_name": "", "consideration_amount": "",
                 "currency_code": "", "actor_name": "",
             }), \
             patch.object(dlv_batch, "persist_assignment"):
            lines, newly_awaiting = dlv_batch._process_dlv_batch_items(TOKENS)
        self.assertEqual(len(lines), 2)
        self.assertEqual(newly_awaiting, [])
        refs_in_lines = {l.split("`")[1] for l in lines}
        self.assertEqual(refs_in_lines, {"REF1", "REF2"})
        for line in lines:
            self.assertIn("📊 Status: ✅ Assigned", line)
            self.assertIn("👤 Valuer: Jane Doe", line)

    def test_still_pending_refs_appended_as_final_line(self):
        items = [self._item("REF1")]
        with patch.object(dlv_batch, "load_dlv_batch", return_value=items), \
             patch.object(dlv_batch, "save_dlv_batch"), \
             patch.object(dlv_batch, "build_session", return_value=MagicMock()), \
             patch.object(dlv_batch, "_search_ref_dlv", return_value=None):
            lines, newly_awaiting = dlv_batch._process_dlv_batch_items(TOKENS)
        self.assertEqual(len(lines), 1)
        self.assertIn("Still pending", lines[0])
        self.assertIn("REF1", lines[0])
        self.assertEqual(newly_awaiting, [])

    def test_taken_by_another_valuer_returned_as_newly_awaiting_not_still_pending(self):
        items = [self._item("REF1")]
        with patch.object(dlv_batch, "load_dlv_batch", return_value=items), \
             patch.object(dlv_batch, "save_dlv_batch") as mock_save, \
             patch.object(dlv_batch, "build_session", return_value=MagicMock()), \
             patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
                 "actors": [{"role": "VALUATION OFFICER", "user_details": {"id": "uid-2", "names": "EXISTING VALUER"}}],
             }), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "open", "closed_reason": "", "application_status": "ONGOING",
                 "node": "VALUATION_STAMP_DUTY_VALUER_REPORT", "assessor_name": "",
                 "consideration_amount": "", "currency_code": "", "actor_name": "",
             }):
            lines, newly_awaiting = dlv_batch._process_dlv_batch_items(TOKENS)
        self.assertEqual(len(newly_awaiting), 1)
        self.assertEqual(newly_awaiting[0]["ref"], "REF1")
        self.assertTrue(newly_awaiting[0]["awaiting_decision"])
        # kept in the queue (still saved), not reported as a retry candidate
        saved_refs = [i["ref"] for i in mock_save.call_args[0][0]]
        self.assertEqual(saved_refs, ["REF1"])
        self.assertFalse(any("Still pending" in l for l in lines))

    def test_already_awaiting_items_are_not_reprocessed_or_renotified(self):
        items = [self._item("REF1", awaiting_decision=True, held_by="EXISTING VALUER", held_by_uid="uid-2")]
        with patch.object(dlv_batch, "load_dlv_batch", return_value=items), \
             patch.object(dlv_batch, "save_dlv_batch") as mock_save, \
             patch.object(dlv_batch, "build_session") as mock_build_session, \
             patch.object(dlv_batch, "_search_ref_dlv") as mock_search:
            lines, newly_awaiting = dlv_batch._process_dlv_batch_items(TOKENS)
        self.assertEqual((lines, newly_awaiting), ([], []))
        mock_search.assert_not_called()
        mock_build_session.assert_not_called()
        mock_save.assert_not_called()


class TestDlvBatchJob(unittest.TestCase):
    """_dlv_batch_job — the 5-minute repeating job; must use
    _send_chunked_report rather than manually truncating at 4000 chars,
    since blocks are now much taller than the old one-liner."""

    def test_no_items_does_nothing(self):
        ctx = MagicMock()
        with patch.object(dlv_batch, "load_dlv_batch", return_value=[]):
            _run(dlv_batch._dlv_batch_job(ctx))
        ctx.bot.send_message.assert_not_called()

    def test_completed_outcomes_are_sent_as_blocks(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        outcome_lines = ["  1. 📌 *Ref:* `REF1`\n     📊 Status: ✅ Assigned"]
        with patch.object(dlv_batch, "load_dlv_batch", side_effect=[[{"ref": "REF1"}], []]), \
             patch.object(dlv_batch, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(dlv_batch, "_process_dlv_batch_items", return_value=(outcome_lines, [])), \
             patch.object(dlv_batch, "ALLOWED_IDS", {111}):
            _run(dlv_batch._dlv_batch_job(ctx))
        sent = "\n".join(c.args[1] for c in ctx.bot.send_message.call_args_list)
        self.assertIn("📋 *DLV Batch (auto)*", sent)
        self.assertIn("📌 *Ref:* `REF1`", sent)

    def test_newly_awaiting_refs_get_a_taken_prompt_to_every_allowed_id(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        newly_awaiting = [{"ref": "REF1", "valuer_name": "Jane Doe", "held_by": "EXISTING VALUER",
                            "held_by_uid": "uid-2"}]
        with patch.object(dlv_batch, "load_dlv_batch", side_effect=[[{"ref": "REF1"}], [{"ref": "REF1"}]]), \
             patch.object(dlv_batch, "_any_valid_tokens", return_value=TOKENS), \
             patch.object(dlv_batch, "_process_dlv_batch_items", return_value=([], newly_awaiting)), \
             patch.object(dlv_batch, "ALLOWED_IDS", {111, 222}):
            _run(dlv_batch._dlv_batch_job(ctx))
        chat_ids = {c.args[0] for c in ctx.bot.send_message.call_args_list}
        self.assertEqual(chat_ids, {111, 222})
        for call in ctx.bot.send_message.call_args_list:
            self.assertIn("REF1", call.args[1])
            self.assertIn("EXISTING VALUER", call.args[1])
            self.assertIn("reply_markup", call.kwargs)


class TestRunDlvBatchBg(unittest.TestCase):
    """_run_dlv_batch_bg — the post-confirm background processor; same
    chunked-sending requirement as the periodic job."""

    def test_empty_report_sends_batch_already_empty_message(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with patch.object(dlv_batch, "_process_dlv_batch_items", return_value=([], [])):
            _run(dlv_batch._run_dlv_batch_bg(ctx, 111, TOKENS))
        ctx.bot.send_message.assert_called_once()
        self.assertIn("already empty", ctx.bot.send_message.call_args[0][1])

    def test_outcome_blocks_sent_via_chunked_report(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        outcome_lines = ["  1. 📌 *Ref:* `REF1`\n     📊 Status: ✅ Assigned"]
        with patch.object(dlv_batch, "_process_dlv_batch_items", return_value=(outcome_lines, [])):
            _run(dlv_batch._run_dlv_batch_bg(ctx, 111, TOKENS))
        sent = "\n".join(c.args[1] for c in ctx.bot.send_message.call_args_list)
        self.assertIn("📋 *DLV Batch Report*", sent)
        self.assertIn("📌 *Ref:* `REF1`", sent)

    def test_newly_awaiting_ref_gets_a_taken_prompt(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        outcome_lines = ["  1. 📌 *Ref:* `REF1`\n     📊 Status: ⚠️ Taken by another valuer — awaiting your decision"]
        newly_awaiting = [{"ref": "REF1", "valuer_name": "Jane Doe", "held_by": "EXISTING VALUER",
                            "held_by_uid": "uid-2"}]
        with patch.object(dlv_batch, "_process_dlv_batch_items", return_value=(outcome_lines, newly_awaiting)):
            _run(dlv_batch._run_dlv_batch_bg(ctx, 111, TOKENS))
        prompt_call = ctx.bot.send_message.call_args_list[-1]
        self.assertEqual(prompt_call.args[0], 111)
        self.assertIn("REF1", prompt_call.args[1])
        self.assertIn("reply_markup", prompt_call.kwargs)

    def test_processing_exception_reports_failure(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with patch.object(dlv_batch, "_process_dlv_batch_items", side_effect=RuntimeError("boom")):
            _run(dlv_batch._run_dlv_batch_bg(ctx, 111, TOKENS))
        ctx.bot.send_message.assert_called_once()
        self.assertIn("processing failed", ctx.bot.send_message.call_args[0][1])


class TestDbTakenKeyboard(unittest.TestCase):
    """_db_taken_keyboard — Reassign/Remove buttons, keyed by the literal ref
    (not an index) since this prompt can be answered long after it's sent."""

    def test_buttons_carry_the_ref_in_callback_data(self):
        markup = dlv_batch._db_taken_keyboard("REG/TSFR/ABC123")
        buttons = markup.inline_keyboard[0]
        callback_data = [b.callback_data for b in buttons]
        self.assertIn("dbtaken:reassign:REG/TSFR/ABC123", callback_data)
        self.assertIn("dbtaken:remove:REG/TSFR/ABC123", callback_data)


class TestDbTakenPromptText(unittest.TestCase):
    """_db_taken_prompt_text — shown alongside _db_taken_keyboard."""

    def test_includes_ref_held_by_and_queued_for(self):
        text = dlv_batch._db_taken_prompt_text(
            {"ref": "REF1", "valuer_name": "Jane Doe", "held_by": "EXISTING VALUER"})
        self.assertIn("REF1", text)
        self.assertIn("EXISTING VALUER", text)
        self.assertIn("Jane Doe", text)

    def test_special_chars_in_names_are_escaped(self):
        text = dlv_batch._db_taken_prompt_text(
            {"ref": "REF1", "valuer_name": "Jane_Doe", "held_by": "John_Otieno"})
        self.assertIn("Jane\\_Doe", text)
        self.assertIn("John\\_Otieno", text)


class TestSendDbTakenPrompts(unittest.TestCase):
    """_send_db_taken_prompts — one prompt per newly-awaiting ref, to every given chat."""

    def test_sends_one_prompt_per_item_per_chat(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        newly_awaiting = [{"ref": "REF1", "valuer_name": "Jane Doe", "held_by": "X"},
                           {"ref": "REF2", "valuer_name": "John", "held_by": "Y"}]
        _run(dlv_batch._send_db_taken_prompts(bot, [111, 222], newly_awaiting))
        self.assertEqual(bot.send_message.call_count, 4)

    def test_one_bad_chat_does_not_block_the_rest(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[RuntimeError("blocked"), None])
        newly_awaiting = [{"ref": "REF1", "valuer_name": "Jane Doe", "held_by": "X"}]
        _run(dlv_batch._send_db_taken_prompts(bot, [111, 222], newly_awaiting))
        self.assertEqual(bot.send_message.call_count, 2)


class TestRecvDbTakenDecision(unittest.TestCase):
    """recv_db_taken_decision — the global Reassign/Remove handler for a ref
    DLV Batch found held by someone other than its queued valuer."""

    def _item(self, **overrides):
        item = {"ref": "REF1", "valuer_name": "Jane Doe", "valuer_uid": "uid-1",
                "awaiting_decision": True, "held_by": "EXISTING VALUER", "held_by_uid": "uid-2"}
        item.update(overrides)
        return item

    def test_reassign_persists_the_new_holder_and_drops_from_queue(self):
        update = _make_query_update("dbtaken:reassign:REF1")
        ctx = MagicMock()
        items = [self._item(), {"ref": "REF2", "valuer_name": "Other"}]
        with patch.object(dlv_batch, "allowed", return_value=True), \
             patch.object(dlv_batch, "load_dlv_batch", return_value=items), \
             patch.object(dlv_batch, "save_dlv_batch") as mock_save, \
             patch.object(dlv_batch, "persist_assignment") as mock_persist, \
             patch.object(dlv_batch, "mark_removed") as mock_remove:
            _run(dlv_batch.recv_db_taken_decision(update, ctx))
        mock_persist.assert_called_once_with("REF1", "EXISTING VALUER", "uid-2", extra={
            "valuer_acct": "", "tag": "", "assessor": "", "parcel": "",
            "consideration": "", "currency_code": "", "queued_at": "",
        })
        mock_remove.assert_not_called()
        saved_refs = [i["ref"] for i in mock_save.call_args[0][0]]
        self.assertEqual(saved_refs, ["REF2"])
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("REF1", text)
        self.assertIn("EXISTING VALUER", text)

    def test_remove_marks_removed_and_drops_from_queue(self):
        update = _make_query_update("dbtaken:remove:REF1")
        ctx = MagicMock()
        items = [self._item(), {"ref": "REF2", "valuer_name": "Other"}]
        with patch.object(dlv_batch, "allowed", return_value=True), \
             patch.object(dlv_batch, "load_dlv_batch", return_value=items), \
             patch.object(dlv_batch, "save_dlv_batch") as mock_save, \
             patch.object(dlv_batch, "persist_assignment") as mock_persist, \
             patch.object(dlv_batch, "mark_removed") as mock_remove:
            _run(dlv_batch.recv_db_taken_decision(update, ctx))
        mock_remove.assert_called_once_with({"REF1"})
        mock_persist.assert_not_called()
        saved_refs = [i["ref"] for i in mock_save.call_args[0][0]]
        self.assertEqual(saved_refs, ["REF2"])
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("REF1", text)
        self.assertIn("removed", text)

    def test_ref_no_longer_awaiting_reports_already_resolved(self):
        update = _make_query_update("dbtaken:reassign:REF1")
        ctx = MagicMock()
        with patch.object(dlv_batch, "allowed", return_value=True), \
             patch.object(dlv_batch, "load_dlv_batch", return_value=[]), \
             patch.object(dlv_batch, "save_dlv_batch") as mock_save, \
             patch.object(dlv_batch, "persist_assignment") as mock_persist:
            _run(dlv_batch.recv_db_taken_decision(update, ctx))
        mock_save.assert_not_called()
        mock_persist.assert_not_called()
        self.assertIn("Already resolved", update.callback_query.edit_message_text.call_args[0][0])


class TestDbFormatBatchSummary(unittest.TestCase):
    """_db_format_batch_summary — the confirm-step summary, tags annotated per ref."""

    def _sess(self, groups, tag_by_ref=None):
        sess = dlv_batch.DBSession()
        sess.groups = groups
        sess.tag_by_ref = tag_by_ref or {}
        return sess

    def test_resolved_group_shows_valuer(self):
        with patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=None):
            sess = self._sess([{"refs": ["REF1"], "valuer_name": "Jane Doe", "status": "resolved"}])
            summary = dlv_batch._db_format_batch_summary(sess)
        self.assertIn("REF1", summary)
        self.assertIn("Jane Doe", summary)

    def test_unresolved_group_flagged_not_found(self):
        sess = self._sess([{"refs": ["REF1"], "valuer_name": "Ghost", "status": "unresolved"}])
        summary = dlv_batch._db_format_batch_summary(sess)
        self.assertIn("NOT FOUND", summary)

    def test_tagged_ref_shows_tag_marker_untagged_does_not(self):
        with patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=None):
            sess = self._sess(
                [{"refs": ["REF1", "REF2"], "valuer_name": "Jane Doe", "status": "resolved"}],
                tag_by_ref={"REF1": "Queue"},
            )
            summary = dlv_batch._db_format_batch_summary(sess)
        self.assertIn("REF1` 🏷Queue", summary)
        self.assertNotIn("REF2` 🏷", summary)

    def test_valuer_name_with_markdown_special_chars_is_escaped(self):
        """Regression: an unescaped '_'/'*' in a valuer or assessor name
        raised telegram.error.BadRequest ("can't find end of the entity")
        and crashed recv_db_input — see common.md_escape."""
        with patch.object(dlv_batch, "_fetch_tasks_log_lookup",
                           return_value={"assessor": "John_Doe"}):
            sess = self._sess([{"refs": ["REF1"], "valuer_name": "Jane_Doe", "status": "resolved"}])
            summary = dlv_batch._db_format_batch_summary(sess)
        self.assertIn("Jane\\_Doe", summary)
        self.assertIn("John\\_Doe", summary)

    def test_unresolved_valuer_name_with_special_chars_is_escaped(self):
        sess = self._sess([{"refs": ["REF1"], "valuer_name": "Ghost_Name", "status": "unresolved"}])
        summary = dlv_batch._db_format_batch_summary(sess)
        self.assertIn("Ghost\\_Name", summary)

    def test_incremental_pick_shows_placeholder_not_raw_sentinel(self):
        with patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=None):
            sess = self._sess(
                [{"refs": ["REF1"], "valuer_name": "Jane Doe", "status": "resolved"}],
                tag_by_ref={"REF1": dlv_batch.INCREMENTAL_TAG_SENTINEL},
            )
            summary = dlv_batch._db_format_batch_summary(sess)
        self.assertIn("🔢 Incremental (auto)", summary)
        self.assertNotIn(dlv_batch.INCREMENTAL_TAG_SENTINEL, summary)


class TestCmdDlvQueue(unittest.TestCase):
    """cmd_dlv_queue — the 🔍 DLV Queue viewer; awaiting-decision refs are
    annotated distinctly from a plain last_error."""

    def _update(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        return update

    def test_awaiting_decision_ref_shown_with_held_by(self):
        update = self._update()
        items = [{"ref": "REF1", "valuer_name": "Jane Doe", "awaiting_decision": True,
                  "held_by": "EXISTING VALUER", "last_error": "stale error"}]
        with patch.object(dlv_batch, "allowed", return_value=True), \
             patch.object(dlv_batch, "load_dlv_batch", return_value=items):
            _run(dlv_batch.cmd_dlv_queue(update, MagicMock()))
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("⏸ Held by *EXISTING VALUER* — awaiting your decision", text)
        self.assertNotIn("stale error", text)

    def test_last_error_shown_when_not_awaiting_decision(self):
        update = self._update()
        items = [{"ref": "REF1", "valuer_name": "Jane Doe", "last_error": "Not found in DLV endpoint"}]
        with patch.object(dlv_batch, "allowed", return_value=True), \
             patch.object(dlv_batch, "load_dlv_batch", return_value=items):
            _run(dlv_batch.cmd_dlv_queue(update, MagicMock()))
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("Not found in DLV endpoint", text)
        self.assertNotIn("awaiting your decision", text)


class TestDbTagKeyboards(unittest.TestCase):
    def test_tag_ref_keyboard_shows_tag_or_no_tag(self):
        markup = dlv_batch._db_tag_ref_keyboard(["REF1", "REF2"], {"REF1": "Queue"})
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("REF1 [🏷 Queue]", texts)
        self.assertIn("REF2 — no tag", texts)
        self.assertIn("✅ Done Tagging", texts)

    def test_tag_ref_keyboard_shows_incremental_preview_not_raw_sentinel(self):
        markup = dlv_batch._db_tag_ref_keyboard(["REF1"], {"REF1": dlv_batch.INCREMENTAL_TAG_SENTINEL})
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("REF1 [🏷 🔢 Incremental (auto)]", texts)

    def test_tag_value_keyboard_lists_fixed_tags_plus_incremental_clear_and_back(self):
        markup = dlv_batch._db_tag_value_keyboard()
        texts = [b.text for row in markup.inline_keyboard for b in row]
        for tag in dlv_batch.DLV_TAGS:
            self.assertIn(tag, texts)
        self.assertIn("🔢 Incremental", texts)
        self.assertIn("🚫 Clear tag", texts)
        self.assertIn("⬅️ Back", texts)

    def test_tag_value_keyboard_incremental_button_uses_sentinel_callback(self):
        markup = dlv_batch._db_tag_value_keyboard()
        buttons = [b for row in markup.inline_keyboard for b in row]
        incremental_btn = next(b for b in buttons if b.text == "🔢 Incremental")
        self.assertEqual(incremental_btn.callback_data, f"db_tagval:{dlv_batch.INCREMENTAL_TAG_SENTINEL}")


class TestDbTagPreview(unittest.TestCase):
    def test_incremental_sentinel_shows_placeholder(self):
        self.assertEqual(dlv_batch._db_tag_preview(dlv_batch.INCREMENTAL_TAG_SENTINEL), "🔢 Incremental (auto)")

    def test_fixed_tag_shown_unchanged(self):
        self.assertEqual(dlv_batch._db_tag_preview("Queue"), "Queue")


class TestRecvDbConfirmTagBranch(unittest.TestCase):
    """recv_db_confirm's "db:tag" branch — enters Tag Tasks with resolved refs only."""

    def test_tag_branch_moves_to_tag_pick_ref_with_resolved_refs_only(self):
        update = _make_query_update("db:tag")
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.groups = [
            {"refs": ["REF1", "REF2"], "valuer_name": "Jane", "status": "resolved"},
            {"refs": ["REF3"], "valuer_name": "Ghost", "status": "unresolved"},
        ]
        result = _run(dlv_batch.recv_db_confirm(update, ctx))
        self.assertEqual(result, dlv_batch.DB.TAG_PICK_REF)
        self.assertEqual(sess.tag_refs, ["REF1", "REF2"])

    def test_tag_branch_with_no_resolved_refs_ends_conversation(self):
        update = _make_query_update("db:tag")
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.groups = [{"refs": ["REF3"], "valuer_name": "Ghost", "status": "unresolved"}]
        result = _run(dlv_batch.recv_db_confirm(update, ctx))
        self.assertEqual(result, dlv_batch.ConversationHandler.END)


class TestRecvDbTagRef(unittest.TestCase):
    """recv_db_tag_ref — the Tag Tasks ref list: cancel/done/pick-a-ref."""

    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.user_data = {}
        self.sess = dlv_batch._get_db_sess(self.ctx)
        self.sess.tag_refs = ["REF1", "REF2"]

    def test_cancel_ends_conversation(self):
        update = _make_query_update("db_tagref:cancel")
        result = _run(dlv_batch.recv_db_tag_ref(update, self.ctx))
        self.assertEqual(result, dlv_batch.ConversationHandler.END)

    def test_done_returns_to_confirm_with_summary(self):
        self.sess.groups = [{"refs": ["REF1", "REF2"], "valuer_name": "Jane", "status": "resolved"}]
        update = _make_query_update("db_tagref:done")
        with patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=None):
            result = _run(dlv_batch.recv_db_tag_ref(update, self.ctx))
        self.assertEqual(result, dlv_batch.DB.CONFIRM_BATCH)

    def test_picking_a_ref_opens_its_tag_value_picker(self):
        update = _make_query_update("db_tagref:0")
        result = _run(dlv_batch.recv_db_tag_ref(update, self.ctx))
        self.assertEqual(result, dlv_batch.DB.TAG_PICK_VALUE)
        self.assertEqual(self.sess.tag_ref_index, 0)
        self.assertIn("REF1", update.callback_query.edit_message_text.call_args[0][0])

    def test_out_of_range_index_stays_on_ref_list(self):
        update = _make_query_update("db_tagref:99")
        result = _run(dlv_batch.recv_db_tag_ref(update, self.ctx))
        self.assertEqual(result, dlv_batch.DB.TAG_PICK_REF)


class TestRecvDbTagValue(unittest.TestCase):
    """recv_db_tag_value — set/clear/leave-unchanged a ref's tag."""

    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.user_data = {}
        self.sess = dlv_batch._get_db_sess(self.ctx)
        self.sess.tag_refs = ["REF1", "REF2"]
        self.sess.tag_ref_index = 0

    def test_picking_a_tag_sets_it_for_the_selected_ref(self):
        update = _make_query_update("db_tagval:Queue")
        result = _run(dlv_batch.recv_db_tag_value(update, self.ctx))
        self.assertEqual(result, dlv_batch.DB.TAG_PICK_REF)
        self.assertEqual(self.sess.tag_by_ref["REF1"], "Queue")

    def test_clear_removes_the_tag(self):
        self.sess.tag_by_ref["REF1"] = "Queue"
        update = _make_query_update("db_tagval:clear")
        _run(dlv_batch.recv_db_tag_value(update, self.ctx))
        self.assertNotIn("REF1", self.sess.tag_by_ref)

    def test_back_leaves_tag_unchanged(self):
        self.sess.tag_by_ref["REF1"] = "Queue"
        update = _make_query_update("db_tagval:back")
        _run(dlv_batch.recv_db_tag_value(update, self.ctx))
        self.assertEqual(self.sess.tag_by_ref["REF1"], "Queue")


class TestRecvDbConfirmAttachesTag(unittest.TestCase):
    """recv_db_confirm's "db:confirm" branch — each new queue item carries its tag."""

    def _confirm(self, ctx):
        with patch.object(dlv_batch, "load_dlv_batch", return_value=[]), \
             patch.object(dlv_batch, "save_dlv_batch") as mock_save, \
             patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=None), \
             patch.object(dlv_batch, "_fetch_tasks_log_remove"), \
             patch.object(dlv_batch, "_any_valid_tokens", return_value=None):
            _run(dlv_batch.recv_db_confirm(_make_query_update("db:confirm"), ctx))
        return mock_save.call_args[0][0]

    def test_tagged_ref_carries_its_tag_onto_the_queue_item(self):
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.groups = [{"refs": ["REF1"], "valuer_name": "Jane", "valuer_uid": "u1",
                        "valuer_acct": "a1", "status": "resolved"}]
        sess.tag_by_ref = {"REF1": "Queue"}
        saved_items = self._confirm(ctx)
        self.assertEqual(saved_items[0]["tag"], "Queue")

    def test_untagged_ref_gets_empty_tag(self):
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.groups = [{"refs": ["REF1"], "valuer_name": "Jane", "valuer_uid": "u1",
                        "valuer_acct": "a1", "status": "resolved"}]
        saved_items = self._confirm(ctx)
        self.assertEqual(saved_items[0]["tag"], "")

    def test_retagging_an_already_queued_ref_updates_it_in_place(self):
        """Resubmitting a ref that's already in the queue just to tag it must
        not be silently dropped — it should update the existing item's tag
        without re-queuing it or losing its original queued_at."""
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.groups = [{"refs": ["REF1"], "valuer_name": "Jane", "valuer_uid": "u1",
                        "valuer_acct": "a1", "status": "resolved"}]
        sess.tag_by_ref = {"REF1": "Queue"}
        existing_item = {"ref": "REF1", "valuer_name": "Jane", "valuer_uid": "u1",
                          "valuer_acct": "a1", "queued_at": "2026-07-01T09:00:00", "tag": ""}
        with patch.object(dlv_batch, "load_dlv_batch", return_value=[existing_item]), \
             patch.object(dlv_batch, "save_dlv_batch") as mock_save, \
             patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=None), \
             patch.object(dlv_batch, "_fetch_tasks_log_remove"), \
             patch.object(dlv_batch, "_any_valid_tokens", return_value=None):
            _run(dlv_batch.recv_db_confirm(_make_query_update("db:confirm"), ctx))
        saved_items = mock_save.call_args[0][0]
        self.assertEqual(len(saved_items), 1)
        self.assertEqual(saved_items[0]["tag"], "Queue")
        self.assertEqual(saved_items[0]["queued_at"], "2026-07-01T09:00:00")

    def test_incremental_sentinel_resolved_to_real_tag_at_confirm_time(self):
        """The counter must only be consumed once the batch is actually
        saved — not when "Incremental" was picked during Tag Tasks."""
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.groups = [{"refs": ["REF1"], "valuer_name": "Jane", "valuer_uid": "u1",
                        "valuer_acct": "a1", "status": "resolved"}]
        sess.tag_by_ref = {"REF1": dlv_batch.INCREMENTAL_TAG_SENTINEL}
        with patch.object(dlv_batch, "next_incremental_tag", return_value="B2-T3") as mock_next:
            saved_items = self._confirm(ctx)
        mock_next.assert_called_once()
        self.assertEqual(saved_items[0]["tag"], "B2-T3")

    def test_incremental_sentinel_resolved_for_retagged_existing_ref(self):
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.groups = [{"refs": ["REF1"], "valuer_name": "Jane", "valuer_uid": "u1",
                        "valuer_acct": "a1", "status": "resolved"}]
        sess.tag_by_ref = {"REF1": dlv_batch.INCREMENTAL_TAG_SENTINEL}
        existing_item = {"ref": "REF1", "valuer_name": "Jane", "valuer_uid": "u1",
                          "valuer_acct": "a1", "queued_at": "2026-07-01T09:00:00", "tag": ""}
        with patch.object(dlv_batch, "load_dlv_batch", return_value=[existing_item]), \
             patch.object(dlv_batch, "save_dlv_batch") as mock_save, \
             patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=None), \
             patch.object(dlv_batch, "_fetch_tasks_log_remove"), \
             patch.object(dlv_batch, "_any_valid_tokens", return_value=None), \
             patch.object(dlv_batch, "next_incremental_tag", return_value="B2-T4"):
            _run(dlv_batch.recv_db_confirm(_make_query_update("db:confirm"), ctx))
        saved_items = mock_save.call_args[0][0]
        self.assertEqual(saved_items[0]["tag"], "B2-T4")


class TestRecvDbConfirmCachesParcelAndConsideration(unittest.TestCase):
    """recv_db_confirm's "db:confirm" branch — Consideration/Parcel for a
    still-queued item can only come from the Fetch Tasks cache (this flow
    avoids live API calls), so pull them in alongside assessor if present."""

    def _confirm(self, ctx, cached):
        with patch.object(dlv_batch, "load_dlv_batch", return_value=[]), \
             patch.object(dlv_batch, "save_dlv_batch") as mock_save, \
             patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=cached), \
             patch.object(dlv_batch, "_fetch_tasks_log_remove"), \
             patch.object(dlv_batch, "_any_valid_tokens", return_value=None):
            _run(dlv_batch.recv_db_confirm(_make_query_update("db:confirm"), ctx))
        return mock_save.call_args[0][0]

    def _ctx_with_one_resolved_ref(self):
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.groups = [{"refs": ["REF1"], "valuer_name": "Jane", "valuer_uid": "u1",
                        "valuer_acct": "a1", "status": "resolved"}]
        return ctx

    def test_parcel_and_consideration_pulled_from_cache(self):
        cached = {"assessor": "Jane Assessor", "parcel": "NAIROBI/BLOCK1/1",
                  "consideration": "6000000", "currency_code": "KES"}
        saved_items = self._confirm(self._ctx_with_one_resolved_ref(), cached)
        self.assertEqual(saved_items[0]["parcel"], "NAIROBI/BLOCK1/1")
        self.assertEqual(saved_items[0]["consideration"], "6000000")
        self.assertEqual(saved_items[0]["currency_code"], "KES")

    def test_missing_cache_entry_leaves_parcel_and_consideration_unset(self):
        saved_items = self._confirm(self._ctx_with_one_resolved_ref(), None)
        self.assertNotIn("parcel", saved_items[0])
        self.assertNotIn("consideration", saved_items[0])

    def test_cache_entry_without_consideration_leaves_it_unset(self):
        cached = {"assessor": "Jane Assessor", "parcel": "NAIROBI/BLOCK1/1"}
        saved_items = self._confirm(self._ctx_with_one_resolved_ref(), cached)
        self.assertEqual(saved_items[0]["parcel"], "NAIROBI/BLOCK1/1")
        self.assertNotIn("consideration", saved_items[0])


class TestRecvDbInputResetsTags(unittest.TestCase):
    """A fresh batch submission must not inherit tags from a prior one."""

    def test_fresh_submission_clears_prior_tags(self):
        update = MagicMock()
        update.message.text = "REF1 : Jane Doe"
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.tag_by_ref = {"OLD_REF": "Queue"}

        with patch.object(dlv_batch, "_any_valid_tokens", return_value=None), \
             patch.object(dlv_batch, "_resolve_valuer_from_saved",
                           return_value={"name": "Jane Doe", "uid": "u1", "account_number": "a1"}), \
             patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=None):
            _run(dlv_batch.recv_db_input(update, ctx))

        self.assertEqual(sess.tag_by_ref, {})


if __name__ == "__main__":
    unittest.main()
