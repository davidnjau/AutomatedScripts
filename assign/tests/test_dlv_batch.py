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
        self.assertIsNone(result["line"])
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
        self.assertIn("Closed (Completed)", result["line"])

    def test_returned_closes_the_item(self):
        with patch.object(dlv_batch, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(dlv_batch, "_fetch_ref_detail_dlv", return_value={"node": "X"}), \
             patch.object(dlv_batch, "_classify_dlv_detail", return_value={
                 "bucket": "closed", "closed_reason": "returned", "application_status": "RETURNED",
                 "node": "VALUATION_STAMP_DUTY_CREATED", "assessor_name": "", "consideration_amount": "",
                 "currency_code": "", "actor_name": "",
             }):
            result = self._run()
        self.assertIn("Closed (Returned)", result["line"])
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
        self.assertIn("assigned to", result["line"])
        self.assertEqual(result["item"]["assessor"], "Jane Assessor")
        mock_persist.assert_called_once_with("REG/TSFR/ABC123", "Jane Doe", "uid-1")
        self.http_sess.post.assert_called_once()

    def test_already_assigned_to_someone_else_reports_and_skips(self):
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
        self.assertFalse(result["keep"])
        self.assertIn("already assigned to *EXISTING VALUER*, not *Jane Doe*", result["line"])
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
        self.assertIn("✅", result["line"])
        self.assertIn("already correctly assigned to *Jane Doe*", result["line"])
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
        self.assertIn("no actor listed", result["line"])
        self.http_sess.post.assert_not_called()

    def test_search_exception_is_kept_for_retry(self):
        with patch.object(dlv_batch, "_search_ref_dlv", side_effect=RuntimeError("boom")):
            result = self._run()
        self.assertTrue(result["keep"])
        self.assertIn("boom", result["item"]["last_error"])


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
                tag_by_ref={"REF1": "Urgent"},
            )
            summary = dlv_batch._db_format_batch_summary(sess)
        self.assertIn("REF1` 🏷Urgent", summary)
        self.assertNotIn("REF2` 🏷", summary)


class TestDbTagKeyboards(unittest.TestCase):
    def test_tag_ref_keyboard_shows_tag_or_no_tag(self):
        markup = dlv_batch._db_tag_ref_keyboard(["REF1", "REF2"], {"REF1": "Urgent"})
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("REF1 [🏷 Urgent]", texts)
        self.assertIn("REF2 — no tag", texts)
        self.assertIn("✅ Done Tagging", texts)

    def test_tag_value_keyboard_lists_fixed_tags_plus_clear_and_back(self):
        markup = dlv_batch._db_tag_value_keyboard()
        texts = [b.text for row in markup.inline_keyboard for b in row]
        for tag in dlv_batch.DLV_TAGS:
            self.assertIn(tag, texts)
        self.assertIn("🚫 Clear tag", texts)
        self.assertIn("⬅️ Back", texts)


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
        update = _make_query_update("db_tagval:Urgent")
        result = _run(dlv_batch.recv_db_tag_value(update, self.ctx))
        self.assertEqual(result, dlv_batch.DB.TAG_PICK_REF)
        self.assertEqual(self.sess.tag_by_ref["REF1"], "Urgent")

    def test_clear_removes_the_tag(self):
        self.sess.tag_by_ref["REF1"] = "Urgent"
        update = _make_query_update("db_tagval:clear")
        _run(dlv_batch.recv_db_tag_value(update, self.ctx))
        self.assertNotIn("REF1", self.sess.tag_by_ref)

    def test_back_leaves_tag_unchanged(self):
        self.sess.tag_by_ref["REF1"] = "Urgent"
        update = _make_query_update("db_tagval:back")
        _run(dlv_batch.recv_db_tag_value(update, self.ctx))
        self.assertEqual(self.sess.tag_by_ref["REF1"], "Urgent")


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
        sess.tag_by_ref = {"REF1": "Urgent"}
        saved_items = self._confirm(ctx)
        self.assertEqual(saved_items[0]["tag"], "Urgent")

    def test_untagged_ref_gets_empty_tag(self):
        ctx = MagicMock()
        ctx.user_data = {}
        sess = dlv_batch._get_db_sess(ctx)
        sess.groups = [{"refs": ["REF1"], "valuer_name": "Jane", "valuer_uid": "u1",
                        "valuer_acct": "a1", "status": "resolved"}]
        saved_items = self._confirm(ctx)
        self.assertEqual(saved_items[0]["tag"], "")


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
        sess.tag_by_ref = {"OLD_REF": "Urgent"}

        with patch.object(dlv_batch, "_any_valid_tokens", return_value=None), \
             patch.object(dlv_batch, "_resolve_valuer_from_saved",
                           return_value={"name": "Jane Doe", "uid": "u1", "account_number": "a1"}), \
             patch.object(dlv_batch, "_fetch_tasks_log_lookup", return_value=None):
            _run(dlv_batch.recv_db_input(update, ctx))

        self.assertEqual(sess.tag_by_ref, {})


if __name__ == "__main__":
    unittest.main()
