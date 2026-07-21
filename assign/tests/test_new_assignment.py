#!/usr/bin/env python3
"""
Unit tests for new_assignment.py — parse_refs/_extract_refs_from_text's
pure parsing logic, _check_assignments_and_proceed's already-assigned
routing, and the cmd_assign/recv_refs/recv_cred_choice/recv_confirm
conversation handlers.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import new_assignment as na
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
    query.message.reply_text = AsyncMock()
    query.message.chat_id = 555
    return update


class TestParseRefs(unittest.TestCase):
    def test_comma_separated(self):
        self.assertEqual(na.parse_refs("A/B/1, A/B/2"), ["A/B/1", "A/B/2"])

    def test_newline_separated(self):
        self.assertEqual(na.parse_refs("A/B/1\nA/B/2"), ["A/B/1", "A/B/2"])

    def test_strips_whitespace_and_drops_empty(self):
        self.assertEqual(na.parse_refs(" A/B/1 ,, \n A/B/2 "), ["A/B/1", "A/B/2"])

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(na.parse_refs(""), [])


class TestExtractRefsFromText(unittest.TestCase):
    def test_matches_slash_separated_pattern(self):
        refs = na._extract_refs_from_text("Doc mentions LS/VAL/2024/001 somewhere.")
        self.assertEqual(refs, ["LS/VAL/2024/001"])

    def test_dedupes_preserving_order(self):
        refs = na._extract_refs_from_text("LS/VAL/2024/001 and LS/VAL/2024/001 again, then LS/VAL/2024/002")
        self.assertEqual(refs, ["LS/VAL/2024/001", "LS/VAL/2024/002"])

    def test_no_match_returns_empty(self):
        self.assertEqual(na._extract_refs_from_text("nothing matching here"), [])


class TestCheckAssignmentsAndProceed(unittest.TestCase):
    def test_no_existing_assignments_proceeds_to_valuer_pick(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = na.Session(refs=["R1", "R2"])
        with patch.object(na, "load_saved_assignments", return_value={}), \
             patch.object(na, "load_saved_valuers", return_value=[]):
            result = _run(na._check_assignments_and_proceed(message, sess))
        self.assertEqual(result, na.S.VALUER_NAME)

    def test_some_existing_assignments_asks_reassign(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = na.Session(refs=["R1", "R2"])
        existing = {"R1": {"valuer_name": "Jane", "assigned_at": "2026-01-01"}}
        with patch.object(na, "load_saved_assignments", return_value=existing):
            result = _run(na._check_assignments_and_proceed(message, sess))
        self.assertEqual(result, na.S.REASSIGN_CONFIRM)
        self.assertEqual(sess.already_assigned[0]["ref"], "R1")

    def test_existing_valuer_name_with_markdown_chars_is_escaped(self):
        """Regression: an unescaped '_'/'*' in a saved assignment's valuer
        name crashes the reassign-confirm send with telegram.error.BadRequest
        ("can't find end of the entity"). See common.md_escape."""
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = na.Session(refs=["R1"])
        existing = {"R1": {"valuer_name": "Jane_Doe", "assigned_at": "2026-01-01"}}
        with patch.object(na, "load_saved_assignments", return_value=existing):
            _run(na._check_assignments_and_proceed(message, sess))
        sent_text = message.reply_text.call_args[0][0]
        self.assertIn("Jane\\_Doe", sent_text)


class TestProceedToValuerPick(unittest.TestCase):
    def test_saved_valuers_show_pick_source(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = na.Session()
        saved = [{"name": "Jane Doe", "uid": "1", "account_number": "A1"}]
        with patch.object(na, "load_saved_valuers", return_value=saved):
            result = _run(na._proceed_to_valuer_pick(message, sess, ["R1"]))
        self.assertEqual(result, na.S.PICK_VALUER_SOURCE)

    def test_no_saved_valuers_asks_for_name(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = na.Session()
        with patch.object(na, "load_saved_valuers", return_value=[]):
            result = _run(na._proceed_to_valuer_pick(message, sess, ["R1"]))
        self.assertEqual(result, na.S.VALUER_NAME)


class TestCmdAssign(unittest.TestCase):
    def test_resets_session_and_asks_workflow_type(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(na, "allowed", return_value=True):
            result = _run(na.cmd_assign(update, ctx))
        self.assertEqual(result, na.S.WORKFLOW_TYPE)
        self.assertIsInstance(ctx.user_data["session"], na.Session)
        self.assertEqual(ctx.user_data["session"].workflow, "stamp_duty")


class TestRecvWorkflowType(unittest.TestCase):
    def test_stamp_duty_sets_workflow_and_asks_input_method(self):
        update = _make_update_with_callback("workflow:stamp_duty")
        ctx = MagicMock()
        ctx.user_data = {"session": na.Session()}
        result = _run(na.recv_workflow_type(update, ctx))
        self.assertEqual(result, na.S.INPUT_METHOD)
        self.assertEqual(ctx.user_data["session"].workflow, "stamp_duty")

    def test_land_rent_sets_workflow_and_asks_input_method(self):
        update = _make_update_with_callback("workflow:land_rent")
        ctx = MagicMock()
        ctx.user_data = {"session": na.Session()}
        result = _run(na.recv_workflow_type(update, ctx))
        self.assertEqual(result, na.S.INPUT_METHOD)
        self.assertEqual(ctx.user_data["session"].workflow, "land_rent")


class TestRecvRefs(unittest.TestCase):
    def test_no_valid_refs_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        ctx.user_data = {"session": na.Session()}
        result = _run(na.recv_refs(update, ctx))
        self.assertEqual(result, na.S.REF_NUMBERS)

    def test_valid_refs_proceed(self):
        update = _make_update_with_message("R1, R2")
        ctx = MagicMock()
        ctx.user_data = {"session": na.Session()}
        with patch.object(na, "load_saved_assignments", return_value={}), \
             patch.object(na, "load_saved_valuers", return_value=[]):
            result = _run(na.recv_refs(update, ctx))
        self.assertEqual(result, na.S.VALUER_NAME)
        self.assertEqual(ctx.user_data["session"].refs, ["R1", "R2"])


class TestRecvCredChoice(unittest.TestCase):
    def test_cached_tokens_with_saved_valuer_jumps_to_confirm(self):
        update = _make_update_with_callback("cred:staff")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"})
        ctx.user_data = {"session": sess}
        with patch.object(na, "get_valid_tokens", return_value=TOKENS), \
             patch.object(na, "build_session", return_value=MagicMock()):
            result = _run(na.recv_cred_choice(update, ctx))
        self.assertEqual(result, na.S.CONFIRM)

    def test_cached_tokens_no_saved_valuer_searches(self):
        update = _make_update_with_callback("cred:staff")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], valuer_name="Jane Doe")
        ctx.user_data = {"session": sess}
        with patch.object(na, "get_valid_tokens", return_value=TOKENS), \
             patch.object(na, "build_session", return_value=MagicMock()), \
             patch.object(na, "_do_valuer_search", new=AsyncMock(return_value=[])):
            result = _run(na.recv_cred_choice(update, ctx))
        self.assertEqual(result, na.ConversationHandler.END)


class TestRecvConfirm(unittest.TestCase):
    def test_no_cancels_conversation(self):
        update = _make_update_with_callback("confirm:no")
        ctx = MagicMock()
        ctx.user_data = {"session": na.Session()}
        result = _run(na.recv_confirm(update, ctx))
        self.assertEqual(result, na.ConversationHandler.END)

    def test_yes_assigns_and_shows_summary(self):
        update = _make_update_with_callback("confirm:yes")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], tokens=TOKENS, saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"})
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"session": sess}
        with patch.object(na, "persist_valuer"), \
             patch.object(na, "persist_assignment"), \
             patch.object(na.asyncio, "to_thread", new=AsyncMock(return_value=([], {}))):
            result = _run(na.recv_confirm(update, ctx))
        self.assertEqual(result, na.ConversationHandler.END)

    def test_assignment_tagged_direct(self):
        """New Assignment always assigns immediately (never via a queue like
        DLV Batch), so every record it persists is tagged "Direct"."""
        update = _make_update_with_callback("confirm:yes")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], tokens=TOKENS, saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"})
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"session": sess}
        with patch.object(na, "persist_valuer"), \
             patch.object(na, "persist_assignment") as mock_persist, \
             patch.object(na.asyncio, "to_thread", new=AsyncMock(return_value=([], {}))):
            _run(na.recv_confirm(update, ctx))
        mock_persist.assert_called_once_with(
            "R1", "Jane", "1", extra={"valuer_acct": "A1", "tag": "Direct", "workflow": "stamp_duty"},
        )

    def test_land_rent_workflow_omits_node_from_request_body(self):
        """Regression: LRD's fix_parcel_number endpoint takes a narrower
        body than Stamp Duty's — reference_number/valuation_officer only,
        no "node" field."""
        update = _make_update_with_callback("confirm:yes")
        ctx = MagicMock()
        sess = na.Session(
            refs=["R1"], tokens=TOKENS, workflow="land_rent",
            saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"},
        )
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"session": sess}
        with patch.object(na, "persist_valuer"), \
             patch.object(na, "persist_assignment") as mock_persist, \
             patch.object(na.asyncio, "to_thread", new=AsyncMock(return_value=([], {}))):
            _run(na.recv_confirm(update, ctx))
        call_args = sess.session.post.call_args
        self.assertEqual(call_args.args[0], na.LRD_FIX_PARCEL_NUMBER_URL)
        self.assertEqual(call_args.kwargs["json"], {"reference_number": "R1", "valuation_officer": "1"})
        mock_persist.assert_called_once_with(
            "R1", "Jane", "1", extra={"valuer_acct": "A1", "tag": "Direct", "workflow": "land_rent"},
        )

    def test_stamp_duty_workflow_still_includes_node(self):
        update = _make_update_with_callback("confirm:yes")
        ctx = MagicMock()
        sess = na.Session(
            refs=["R1"], tokens=TOKENS, workflow="stamp_duty",
            saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"},
        )
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"session": sess}
        with patch.object(na, "persist_valuer"), \
             patch.object(na, "persist_assignment"), \
             patch.object(na.asyncio, "to_thread", new=AsyncMock(return_value=([], {}))):
            _run(na.recv_confirm(update, ctx))
        call_args = sess.session.post.call_args
        self.assertEqual(call_args.args[0], na.STAMP_DUTY_FIX_APPLICATION_URL)
        self.assertEqual(
            call_args.kwargs["json"],
            {"reference_number": "R1", "valuation_officer": "1", "node": "VALUATION_STAMP_DUTY_VALUER_REPORT"},
        )

    def test_large_result_set_paginates_instead_of_truncating(self):
        """A large batch must be split across multiple messages, not truncated."""
        update = _make_update_with_callback("confirm:yes")
        ctx = MagicMock()
        many_refs = [f"REF/{i}" for i in range(400)]   # long enough to exceed one 4000-char chunk
        sess = na.Session(refs=many_refs, tokens=TOKENS, saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"})
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"session": sess}
        with patch.object(na, "persist_valuer"), \
             patch.object(na, "persist_assignment"), \
             patch.object(na.asyncio, "to_thread", new=AsyncMock(return_value=([], {}))):
            _run(na.recv_confirm(update, ctx))
        sent_texts = [c.args[0] for c in update.callback_query.message.reply_text.call_args_list]
        self.assertGreater(len(sent_texts), 1)
        for ref in many_refs:
            self.assertTrue(any(ref in t for t in sent_texts), f"{ref} missing from any sent chunk")


class TestLookupOneRef(unittest.TestCase):
    """_lookup_one_ref — returns (formatted string, raw context dict), the
    latter feeding recv_confirm's post-assignment persist_assignment
    enrichment. workflow picks Stamp Duty vs LRD primitives."""

    def test_ref_not_found_returns_empty_context(self):
        with patch.object(na, "_lu_search_ref", return_value=None):
            text, ctx = na._lookup_one_ref(TOKENS, "R1", "stamp_duty")
        self.assertIn("not found", text)
        self.assertEqual(ctx, {})

    def test_ref_found_returns_formatted_text_and_context(self):
        item = {"id": "app-1", "reference_number": "R1", "registry": "NAIROBI", "county": "NAIROBI"}
        with patch.object(na, "_lu_search_ref", return_value=item), \
             patch.object(na, "_lu_fetch_detail", return_value=None):
            text, ctx = na._lookup_one_ref(TOKENS, "R1", "stamp_duty")
        self.assertIn("R1", text)
        self.assertEqual(ctx["registry"], "NAIROBI")
        self.assertEqual(ctx["county"], "NAIROBI")

    def test_land_rent_workflow_uses_lrd_primitives(self):
        with patch.object(na, "_lu_search_ref_lrd", return_value=None) as mock_search_lrd, \
             patch.object(na, "_lu_search_ref") as mock_search:
            text, ctx = na._lookup_one_ref(TOKENS, "REG/SECT/AB12CD", "land_rent")
        mock_search_lrd.assert_called_once_with(TOKENS, "REG/SECT/AB12CD")
        mock_search.assert_not_called()
        self.assertIn("not found", text)
        self.assertEqual(ctx, {})

    def test_land_rent_ref_found_returns_lrd_formatted_text_and_context(self):
        item = {"id": "app-1", "reference_number": "REG/SECT/AB12CD", "parcel_number": "NAIROBI/BLOCK1/1"}
        with patch.object(na, "_lu_search_ref_lrd", return_value=item), \
             patch.object(na, "_lu_fetch_detail_lrd", return_value=None):
            text, ctx = na._lookup_one_ref(TOKENS, "REG/SECT/AB12CD", "land_rent")
        self.assertIn("REG/SECT/AB12CD", text)
        self.assertIn("Land Rent", text)
        self.assertEqual(ctx["parcel_number"], "NAIROBI/BLOCK1/1")


class TestPostAssignmentReport(unittest.TestCase):
    """_post_assignment_report — returns (pages, extras), extras keyed per
    ref for recv_confirm to merge into saved_dlv_records.json afterward."""

    def test_returns_pages_and_per_ref_extras(self):
        def fake_lookup(tokens, ref, workflow):
            return f"result for {ref}", {"parcel": f"P-{ref}"}
        with patch.object(na, "_lookup_one_ref", side_effect=fake_lookup):
            pages, extras = na._post_assignment_report(TOKENS, ["R1", "R2"], "stamp_duty")
        self.assertTrue(any("R1" in p or "result for R1" in p for p in pages))
        self.assertEqual(extras["R1"]["parcel"], "P-R1")
        self.assertEqual(extras["R2"]["parcel"], "P-R2")

    def test_lookup_exception_yields_empty_extra_for_that_ref(self):
        def fake_lookup(tokens, ref, workflow):
            if ref == "R1":
                raise RuntimeError("boom")
            return "ok", {"parcel": "P2"}
        with patch.object(na, "_lookup_one_ref", side_effect=fake_lookup):
            pages, extras = na._post_assignment_report(TOKENS, ["R1", "R2"], "stamp_duty")
        self.assertEqual(extras["R1"], {})
        self.assertEqual(extras["R2"]["parcel"], "P2")

    def test_workflow_is_forwarded_to_lookup_one_ref(self):
        with patch.object(na, "_lookup_one_ref", return_value=("ok", {})) as mock_lookup:
            na._post_assignment_report(TOKENS, ["R1"], "land_rent")
        mock_lookup.assert_called_once_with(TOKENS, "R1", "land_rent")


class TestRecvConfirmMarkdownEscaping(unittest.TestCase):
    """recv_confirm's "Assigning *{name}*…" and "*Valuer:* {name}" messages —
    regression for telegram.error.BadRequest ("can't find end of the
    entity") when a valuer name contains a raw '_'/'*'. See common.md_escape."""

    def test_assigning_message_and_summary_escape_valuer_name(self):
        update = _make_update_with_callback("confirm:yes")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], tokens=TOKENS,
                           saved_valuer={"name": "Jane_Doe", "uid": "1", "account_number": "A1"})
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"session": sess}
        with patch.object(na, "persist_valuer"), \
             patch.object(na, "persist_assignment"), \
             patch.object(na.asyncio, "to_thread", new=AsyncMock(return_value=([], {}))):
            _run(na.recv_confirm(update, ctx))
        assigning_text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Jane\\_Doe", assigning_text)
        summary_texts = [c.args[0] for c in update.callback_query.message.reply_text.call_args_list]
        self.assertTrue(any("Jane\\_Doe" in t for t in summary_texts))


class TestRecvConfirmPersistsLookupContext(unittest.TestCase):
    """recv_confirm's post-lookup enrichment step — merges _post_assignment_report's
    per-ref extra context into the already-persisted assignment record."""

    def test_enriches_assignment_with_post_assignment_context(self):
        update = _make_update_with_callback("confirm:yes")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], tokens=TOKENS, saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"})
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"session": sess}
        fake_report = AsyncMock(return_value=(["page1"], {"R1": {"parcel": "P1", "consideration": "500000"}}))
        with patch.object(na, "persist_valuer"), \
             patch.object(na, "persist_assignment") as mock_persist, \
             patch.object(na.asyncio, "to_thread", new=fake_report):
            _run(na.recv_confirm(update, ctx))
        calls = mock_persist.call_args_list
        self.assertEqual(len(calls), 2)   # immediate call, then the enrichment call
        enrich_call = calls[-1]
        self.assertEqual(enrich_call.args, ("R1", "Jane", "1"))
        self.assertEqual(enrich_call.kwargs["extra"]["parcel"], "P1")
        self.assertEqual(enrich_call.kwargs["extra"]["consideration"], "500000")
        self.assertEqual(enrich_call.kwargs["extra"]["valuer_acct"], "A1")
        self.assertEqual(enrich_call.kwargs["extra"]["tag"], "Direct")

        immediate_call = calls[0]
        self.assertEqual(immediate_call.kwargs["extra"]["tag"], "Direct")

    def test_empty_extra_skips_enrichment_call(self):
        update = _make_update_with_callback("confirm:yes")
        ctx = MagicMock()
        sess = na.Session(refs=["R1"], tokens=TOKENS, saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"})
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"session": sess}
        fake_report = AsyncMock(return_value=(["page1"], {"R1": {}}))
        with patch.object(na, "persist_valuer"), \
             patch.object(na, "persist_assignment") as mock_persist, \
             patch.object(na.asyncio, "to_thread", new=fake_report):
            _run(na.recv_confirm(update, ctx))
        self.assertEqual(len(mock_persist.call_args_list), 1)   # only the immediate call


class TestCmdAssignments(unittest.TestCase):
    def test_no_assignments_shows_empty_message(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(na, "allowed", return_value=True), \
             patch.object(na, "load_saved_assignments", return_value={}):
            _run(na.cmd_assignments(update, ctx))
        self.assertIn("No assignments", update.message.reply_text.call_args[0][0])

    def test_large_history_paginates_instead_of_truncating(self):
        assignments = {
            f"REF/{i}": {"valuer_name": f"Valuer {i}", "assigned_at": "2026-01-01"}
            for i in range(400)
        }
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(na, "allowed", return_value=True), \
             patch.object(na, "load_saved_assignments", return_value=assignments):
            _run(na.cmd_assignments(update, ctx))
        sent_texts = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertGreater(len(sent_texts), 1)
        self.assertTrue(any("REF/0" in t for t in sent_texts))
        self.assertTrue(any("REF/399" in t for t in sent_texts))

    def test_shows_workflow_label_per_entry(self):
        assignments = {
            "REF/1": {"valuer_name": "Jane", "assigned_at": "2026-01-01", "workflow": "land_rent"},
            "REF/2": {"valuer_name": "Jane", "assigned_at": "2026-01-02", "workflow": "stamp_duty"},
        }
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(na, "allowed", return_value=True), \
             patch.object(na, "load_saved_assignments", return_value=assignments):
            _run(na.cmd_assignments(update, ctx))
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("🏘 Land Rent", sent)
        self.assertIn("📋 Stamp Duty", sent)

    def test_missing_workflow_field_defaults_to_stamp_duty(self):
        """Every other assignment source (DLV Batch, Receive Tasks, Auto
        Fetch) predates this field and never sets it."""
        assignments = {"REF/1": {"valuer_name": "Jane", "assigned_at": "2026-01-01"}}
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(na, "allowed", return_value=True), \
             patch.object(na, "load_saved_assignments", return_value=assignments):
            _run(na.cmd_assignments(update, ctx))
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("📋 Stamp Duty", sent)


if __name__ == "__main__":
    unittest.main()
