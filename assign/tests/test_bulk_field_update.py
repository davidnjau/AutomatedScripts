#!/usr/bin/env python3
"""
Unit tests for bulk_field_update.py — the custom-value persistence for
application_status/node, the field-choice/value-select/valuer-pick
conversation branches, the cached-vs-login credential paths, and the
confirm/run loop (including that persist_assignment only fires for the
valuation_officer field).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bulk_field_update as bfu
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


class TestBfuCustomStatusesPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.tmpdir.name, "saved_bfu_custom_statuses.json")
        self._patch = patch.object(bfu, "SAVED_BFU_CUSTOM_STATUSES_FILE", self.data_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(bfu.load_bfu_custom_statuses(), [])

    def test_save_then_load_roundtrip(self):
        bfu.save_bfu_custom_statuses(["SUSPENDED"])
        self.assertEqual(bfu.load_bfu_custom_statuses(), ["SUSPENDED"])


class TestBfuCustomNodesPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.tmpdir.name, "saved_bfu_custom_nodes.json")
        self._patch = patch.object(bfu, "SAVED_BFU_CUSTOM_NODES_FILE", self.data_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(bfu.load_bfu_custom_nodes(), [])

    def test_save_then_load_roundtrip(self):
        bfu.save_bfu_custom_nodes(["CUSTOM_NODE"])
        self.assertEqual(bfu.load_bfu_custom_nodes(), ["CUSTOM_NODE"])


class TestBfuChoicesFor(unittest.TestCase):
    def test_application_status_returns_builtins_when_no_custom(self):
        with patch.object(bfu, "load_bfu_custom_statuses", return_value=[]):
            self.assertEqual(bfu._bfu_choices_for("application_status"), bfu._BFU_STATUS_CHOICES)

    def test_application_status_appends_custom_not_duplicating_builtins(self):
        with patch.object(bfu, "load_bfu_custom_statuses", return_value=["ONGOING", "SUSPENDED"]):
            choices = bfu._bfu_choices_for("application_status")
        self.assertEqual(choices, bfu._BFU_STATUS_CHOICES + ["SUSPENDED"])

    def test_node_returns_builtins_when_no_custom(self):
        with patch.object(bfu, "load_bfu_custom_nodes", return_value=[]):
            self.assertEqual(bfu._bfu_choices_for("node"), bfu._BFU_NODE_CHOICES)

    def test_node_appends_custom_not_duplicating_builtins(self):
        with patch.object(bfu, "load_bfu_custom_nodes", return_value=["VALUATION_STAMP_DUTY_CREATED", "CUSTOM_NODE"]):
            choices = bfu._bfu_choices_for("node")
        self.assertEqual(choices, bfu._BFU_NODE_CHOICES + ["CUSTOM_NODE"])


class TestCmdBulkFieldUpdate(unittest.TestCase):
    def test_resets_session_and_asks_field_choice(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(bfu, "allowed", return_value=True):
            result = _run(bfu.cmd_bulk_field_update(update, ctx))
        self.assertEqual(result, bfu.BFU.FIELD_CHOICE)
        self.assertIsInstance(ctx.user_data["bfu_session"], bfu.BFUSession)


class TestRecvBfuField(unittest.TestCase):
    def test_cancel_ends_conversation(self):
        update = _make_update_with_callback("bfu_field:cancel")
        ctx = MagicMock()
        result = _run(bfu.recv_bfu_field(update, ctx))
        self.assertEqual(result, bfu.ConversationHandler.END)

    def test_valuation_officer_sets_field_and_asks_refs(self):
        update = _make_update_with_callback("bfu_field:valuation_officer")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession()}
        result = _run(bfu.recv_bfu_field(update, ctx))
        self.assertEqual(result, bfu.BFU.REF_NUMBERS)
        self.assertEqual(ctx.user_data["bfu_session"].update_field, "valuation_officer")

    def test_node_sets_field_and_asks_refs(self):
        update = _make_update_with_callback("bfu_field:node")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession()}
        result = _run(bfu.recv_bfu_field(update, ctx))
        self.assertEqual(result, bfu.BFU.REF_NUMBERS)
        self.assertEqual(ctx.user_data["bfu_session"].update_field, "node")


class TestRecvBfuRefs(unittest.TestCase):
    def test_no_valid_refs_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="node")}
        result = _run(bfu.recv_bfu_refs(update, ctx))
        self.assertEqual(result, bfu.BFU.REF_NUMBERS)

    def test_valuation_officer_field_proceeds_to_valuer_pick(self):
        update = _make_update_with_message("R1, R2")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="valuation_officer")}
        with patch.object(bfu, "load_saved_valuers", return_value=[]):
            result = _run(bfu.recv_bfu_refs(update, ctx))
        self.assertEqual(result, bfu.BFU.VALUER_NAME)
        self.assertEqual(ctx.user_data["bfu_session"].refs, ["R1", "R2"])

    def test_node_field_shows_value_picker(self):
        update = _make_update_with_message("R1, R2")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="node")}
        with patch.object(bfu, "load_bfu_custom_nodes", return_value=[]):
            result = _run(bfu.recv_bfu_refs(update, ctx))
        self.assertEqual(result, bfu.BFU.VALUE_SELECT)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        button_data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("bfu_val:VALUATION_STAMP_DUTY_CREATED", button_data)

    def test_application_status_field_shows_value_picker(self):
        update = _make_update_with_message("R1")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="application_status")}
        with patch.object(bfu, "load_bfu_custom_statuses", return_value=[]):
            result = _run(bfu.recv_bfu_refs(update, ctx))
        self.assertEqual(result, bfu.BFU.VALUE_SELECT)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        button_data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("bfu_val:ONGOING", button_data)


class TestBfuProceedToValuerPick(unittest.TestCase):
    def test_saved_valuers_show_pick_source(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = bfu.BFUSession(refs=["R1"])
        saved = [{"name": "Jane Doe", "uid": "1", "account_number": "A1"}]
        with patch.object(bfu, "load_saved_valuers", return_value=saved):
            result = _run(bfu._bfu_proceed_to_valuer_pick(message, sess, "  • `R1`"))
        self.assertEqual(result, bfu.BFU.PICK_VALUER_SOURCE)

    def test_no_saved_valuers_asks_for_name(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        sess = bfu.BFUSession(refs=["R1"])
        with patch.object(bfu, "load_saved_valuers", return_value=[]):
            result = _run(bfu._bfu_proceed_to_valuer_pick(message, sess, "  • `R1`"))
        self.assertEqual(result, bfu.BFU.VALUER_NAME)


class TestRecvBfuValuerSource(unittest.TestCase):
    def test_new_asks_for_name(self):
        update = _make_update_with_callback("bfu_src:new")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession()}
        result = _run(bfu.recv_bfu_valuer_source(update, ctx))
        self.assertEqual(result, bfu.BFU.VALUER_NAME)

    def test_saved_valuer_asks_for_credential(self):
        update = _make_update_with_callback("bfu_src:0")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession()}
        saved = [{"name": "Jane Doe", "uid": "1", "account_number": "A1"}]
        with patch.object(bfu, "load_saved_valuers", return_value=saved):
            result = _run(bfu.recv_bfu_valuer_source(update, ctx))
        self.assertEqual(result, bfu.BFU.CHOOSE_CRED)
        self.assertEqual(ctx.user_data["bfu_session"].saved_valuer, saved[0])

    def test_stale_index_ends_conversation(self):
        update = _make_update_with_callback("bfu_src:5")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession()}
        with patch.object(bfu, "load_saved_valuers", return_value=[]):
            result = _run(bfu.recv_bfu_valuer_source(update, ctx))
        self.assertEqual(result, bfu.ConversationHandler.END)


class TestRecvBfuValuerName(unittest.TestCase):
    def test_sets_name_and_asks_credential(self):
        update = _make_update_with_message("Jane Doe")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession()}
        result = _run(bfu.recv_bfu_valuer_name(update, ctx))
        self.assertEqual(result, bfu.BFU.CHOOSE_CRED)
        self.assertEqual(ctx.user_data["bfu_session"].valuer_name, "Jane Doe")


class TestRecvBfuValueSelect(unittest.TestCase):
    def test_cancel_ends_conversation(self):
        update = _make_update_with_callback("bfu_val:cancel")
        ctx = MagicMock()
        result = _run(bfu.recv_bfu_value_select(update, ctx))
        self.assertEqual(result, bfu.ConversationHandler.END)

    def test_add_new_sentinel_asks_for_text_entry(self):
        update = _make_update_with_callback(bfu._BFU_VALUE_ADD_SENTINEL)
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="node")}
        result = _run(bfu.recv_bfu_value_select(update, ctx))
        self.assertEqual(result, bfu.BFU.VALUE_ADD)

    def test_picking_a_value_sets_it_and_asks_credential(self):
        update = _make_update_with_callback("bfu_val:ONGOING")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="application_status")}
        result = _run(bfu.recv_bfu_value_select(update, ctx))
        self.assertEqual(result, bfu.BFU.CHOOSE_CRED)
        self.assertEqual(ctx.user_data["bfu_session"].value, "ONGOING")


class TestRecvBfuValueAdd(unittest.TestCase):
    def test_empty_text_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="node")}
        result = _run(bfu.recv_bfu_value_add(update, ctx))
        self.assertEqual(result, bfu.BFU.VALUE_ADD)

    def test_valid_text_persists_and_selects_value(self):
        update = _make_update_with_message("my new status")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="application_status")}
        with patch.object(bfu, "load_bfu_custom_statuses", return_value=[]), \
             patch.object(bfu, "save_bfu_custom_statuses") as mock_save:
            result = _run(bfu.recv_bfu_value_add(update, ctx))
        self.assertEqual(result, bfu.BFU.CHOOSE_CRED)
        self.assertEqual(ctx.user_data["bfu_session"].value, "MY_NEW_STATUS")
        mock_save.assert_called_once_with(["MY_NEW_STATUS"])

    def test_already_saved_value_is_not_duplicated(self):
        update = _make_update_with_message("CUSTOM_NODE")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="node")}
        with patch.object(bfu, "load_bfu_custom_nodes", return_value=["CUSTOM_NODE"]), \
             patch.object(bfu, "save_bfu_custom_nodes") as mock_save:
            _run(bfu.recv_bfu_value_add(update, ctx))
        mock_save.assert_not_called()

    def test_builtin_value_is_not_persisted_to_custom_list(self):
        update = _make_update_with_message("ONGOING")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession(update_field="application_status")}
        with patch.object(bfu, "load_bfu_custom_statuses", return_value=[]), \
             patch.object(bfu, "save_bfu_custom_statuses") as mock_save:
            _run(bfu.recv_bfu_value_add(update, ctx))
        mock_save.assert_not_called()


class TestRecvBfuCredChoice(unittest.TestCase):
    def test_cached_tokens_non_valuer_field_jumps_to_confirm(self):
        update = _make_update_with_callback("cred:staff")
        ctx = MagicMock()
        sess = bfu.BFUSession(refs=["R1"], update_field="node", value="VALUATION_STAMP_DUTY_COMPLETED")
        ctx.user_data = {"bfu_session": sess}
        with patch.object(bfu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(bfu, "build_session", return_value=MagicMock()):
            result = _run(bfu.recv_bfu_cred_choice(update, ctx))
        self.assertEqual(result, bfu.BFU.CONFIRM)

    def test_cached_tokens_with_saved_valuer_jumps_to_confirm(self):
        update = _make_update_with_callback("cred:staff")
        ctx = MagicMock()
        sess = bfu.BFUSession(
            refs=["R1"], update_field="valuation_officer",
            saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"},
        )
        ctx.user_data = {"bfu_session": sess}
        with patch.object(bfu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(bfu, "build_session", return_value=MagicMock()):
            result = _run(bfu.recv_bfu_cred_choice(update, ctx))
        self.assertEqual(result, bfu.BFU.CONFIRM)

    def test_cached_tokens_valuer_search_new_searches(self):
        update = _make_update_with_callback("cred:staff")
        ctx = MagicMock()
        sess = bfu.BFUSession(refs=["R1"], update_field="valuation_officer", valuer_name="Jane Doe")
        ctx.user_data = {"bfu_session": sess}
        with patch.object(bfu, "get_valid_tokens", return_value=TOKENS), \
             patch.object(bfu, "build_session", return_value=MagicMock()), \
             patch.object(bfu, "_bfu_do_valuer_search", new=AsyncMock(return_value=[])):
            result = _run(bfu.recv_bfu_cred_choice(update, ctx))
        self.assertEqual(result, bfu.ConversationHandler.END)


class TestRecvBfuValuerSelect(unittest.TestCase):
    def test_sets_confirm_summary(self):
        update = _make_update_with_callback("bfu_valuer:0")
        ctx = MagicMock()
        sess = bfu.BFUSession(refs=["R1"], update_field="valuation_officer")
        sess.valuers = [{"staff_details": {"firstname": "Jane", "lastname": "Doe", "user_id": "42"}}]
        ctx.user_data = {"bfu_session": sess}
        result = _run(bfu.recv_bfu_valuer_select(update, ctx))
        self.assertEqual(result, bfu.BFU.CONFIRM)
        self.assertEqual(sess.selected_idx, 0)


class TestRecvBfuConfirm(unittest.TestCase):
    def test_no_cancels_conversation(self):
        update = _make_update_with_callback("bfu_confirm:no")
        ctx = MagicMock()
        ctx.user_data = {"bfu_session": bfu.BFUSession()}
        result = _run(bfu.recv_bfu_confirm(update, ctx))
        self.assertEqual(result, bfu.ConversationHandler.END)

    def test_node_update_sends_correct_body_and_skips_persist_assignment(self):
        update = _make_update_with_callback("bfu_confirm:yes")
        ctx = MagicMock()
        sess = bfu.BFUSession(
            refs=["R1"], tokens=TOKENS, update_field="node", value="VALUATION_STAMP_DUTY_COMPLETED",
        )
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"bfu_session": sess}
        with patch.object(bfu, "persist_assignment") as mock_persist, \
             patch.object(bfu, "persist_valuer") as mock_persist_valuer, \
             patch.object(bfu, "_bfu_fetch_snapshots", return_value={}), \
             patch.object(bfu, "append_bfu_history") as mock_append:
            result = _run(bfu.recv_bfu_confirm(update, ctx))
        self.assertEqual(result, bfu.ConversationHandler.END)
        call_args = sess.session.post.call_args
        self.assertEqual(call_args.args[0], bfu.STAMP_DUTY_FIX_APPLICATION_URL)
        self.assertEqual(
            call_args.kwargs["json"],
            {"reference_number": "R1", "node": "VALUATION_STAMP_DUTY_COMPLETED"},
        )
        mock_persist.assert_not_called()
        mock_persist_valuer.assert_not_called()
        mock_append.assert_called_once()

    def test_application_status_update_sends_correct_body(self):
        update = _make_update_with_callback("bfu_confirm:yes")
        ctx = MagicMock()
        sess = bfu.BFUSession(
            refs=["R1", "R2"], tokens=TOKENS, update_field="application_status", value="ON-HOLD",
        )
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"bfu_session": sess}
        with patch.object(bfu, "persist_assignment"), patch.object(bfu, "persist_valuer"), \
             patch.object(bfu, "_bfu_fetch_snapshots", return_value={}), \
             patch.object(bfu, "append_bfu_history"):
            _run(bfu.recv_bfu_confirm(update, ctx))
        bodies = [c.kwargs["json"] for c in sess.session.post.call_args_list]
        self.assertEqual(bodies, [
            {"reference_number": "R1", "application_status": "ON-HOLD"},
            {"reference_number": "R2", "application_status": "ON-HOLD"},
        ])

    def test_valuation_officer_update_persists_valuer_and_assignment(self):
        update = _make_update_with_callback("bfu_confirm:yes")
        ctx = MagicMock()
        sess = bfu.BFUSession(
            refs=["R1"], tokens=TOKENS, update_field="valuation_officer",
            saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"},
        )
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"bfu_session": sess}
        with patch.object(bfu, "persist_valuer") as mock_persist_valuer, \
             patch.object(bfu, "persist_assignment") as mock_persist_assignment, \
             patch.object(bfu, "_bfu_fetch_snapshots", return_value={}), \
             patch.object(bfu, "append_bfu_history"):
            _run(bfu.recv_bfu_confirm(update, ctx))
        call_args = sess.session.post.call_args
        self.assertEqual(
            call_args.kwargs["json"],
            {"reference_number": "R1", "valuation_officer": "1"},
        )
        mock_persist_valuer.assert_called_once_with("Jane", "1", "A1")
        mock_persist_assignment.assert_called_once_with(
            "R1", "Jane", "1", extra={"valuer_acct": "A1", "tag": "Direct", "workflow": "stamp_duty"},
        )

    def test_failed_request_is_reported_and_not_persisted(self):
        update = _make_update_with_callback("bfu_confirm:yes")
        ctx = MagicMock()
        sess = bfu.BFUSession(
            refs=["R1"], tokens=TOKENS, update_field="valuation_officer",
            saved_valuer={"name": "Jane", "uid": "1", "account_number": "A1"},
        )
        sess.session = MagicMock()
        sess.session.post.side_effect = RuntimeError("boom")
        ctx.user_data = {"bfu_session": sess}
        with patch.object(bfu, "persist_valuer") as mock_persist_valuer, \
             patch.object(bfu, "persist_assignment") as mock_persist_assignment, \
             patch.object(bfu, "_bfu_fetch_snapshots", return_value={}), \
             patch.object(bfu, "append_bfu_history") as mock_append:
            _run(bfu.recv_bfu_confirm(update, ctx))
        mock_persist_valuer.assert_not_called()
        mock_persist_assignment.assert_not_called()
        mock_append.assert_not_called()
        sent_texts = [c.args[0] for c in update.callback_query.message.reply_text.call_args_list]
        self.assertTrue(any("boom" in t or "Failed" in t or "1 / 1" in t for t in sent_texts))

    def test_history_entry_captures_before_and_after_for_node_update(self):
        update = _make_update_with_callback("bfu_confirm:yes")
        ctx = MagicMock()
        sess = bfu.BFUSession(
            refs=["R1"], tokens=TOKENS, update_field="node", value="VALUATION_STAMP_DUTY_COMPLETED",
        )
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"bfu_session": sess}
        snapshot = {"R1": {"valuer": "Jane Doe", "application_status": "ONGOING", "node": "VALUATION_STAMP_DUTY_VALUER_REPORT"}}
        with patch.object(bfu, "_bfu_fetch_snapshots", return_value=snapshot), \
             patch.object(bfu, "append_bfu_history") as mock_append:
            _run(bfu.recv_bfu_confirm(update, ctx))
        entries = mock_append.call_args[0][0]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["reference_number"], "R1")
        self.assertEqual(entry["initial_valuer"], "Jane Doe")
        self.assertEqual(entry["new_valuer"], "Jane Doe")
        self.assertEqual(entry["initial_application_status"], "ONGOING")
        self.assertEqual(entry["new_application_status"], "ONGOING")
        self.assertEqual(entry["initial_node"], "VALUATION_STAMP_DUTY_VALUER_REPORT")
        self.assertEqual(entry["new_node"], "VALUATION_STAMP_DUTY_COMPLETED")
        self.assertIn("recorded_at", entry)

    def test_history_entry_captures_before_and_after_for_valuer_update(self):
        update = _make_update_with_callback("bfu_confirm:yes")
        ctx = MagicMock()
        sess = bfu.BFUSession(
            refs=["R1"], tokens=TOKENS, update_field="valuation_officer",
            saved_valuer={"name": "John Roe", "uid": "2", "account_number": "A2"},
        )
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"bfu_session": sess}
        snapshot = {"R1": {"valuer": "Jane Doe", "application_status": "ONGOING", "node": "VALUATION_STAMP_DUTY_VALUER_REPORT"}}
        with patch.object(bfu, "persist_valuer"), patch.object(bfu, "persist_assignment"), \
             patch.object(bfu, "_bfu_fetch_snapshots", return_value=snapshot), \
             patch.object(bfu, "append_bfu_history") as mock_append:
            _run(bfu.recv_bfu_confirm(update, ctx))
        entry = mock_append.call_args[0][0][0]
        self.assertEqual(entry["initial_valuer"], "Jane Doe")
        self.assertEqual(entry["new_valuer"], "John Roe")
        self.assertEqual(entry["initial_node"], "VALUATION_STAMP_DUTY_VALUER_REPORT")
        self.assertEqual(entry["new_node"], "VALUATION_STAMP_DUTY_VALUER_REPORT")

    def test_missing_snapshot_yields_none_initial_fields(self):
        update = _make_update_with_callback("bfu_confirm:yes")
        ctx = MagicMock()
        sess = bfu.BFUSession(
            refs=["R1"], tokens=TOKENS, update_field="node", value="VALUATION_STAMP_DUTY_COMPLETED",
        )
        sess.session = MagicMock()
        sess.session.post.return_value = MagicMock(raise_for_status=lambda: None)
        ctx.user_data = {"bfu_session": sess}
        with patch.object(bfu, "_bfu_fetch_snapshots", return_value={}), \
             patch.object(bfu, "append_bfu_history") as mock_append:
            _run(bfu.recv_bfu_confirm(update, ctx))
        entry = mock_append.call_args[0][0][0]
        self.assertIsNone(entry["initial_valuer"])
        self.assertIsNone(entry["initial_application_status"])
        self.assertIsNone(entry["initial_node"])
        self.assertEqual(entry["new_node"], "VALUATION_STAMP_DUTY_COMPLETED")

    def test_no_history_appended_when_all_refs_fail(self):
        update = _make_update_with_callback("bfu_confirm:yes")
        ctx = MagicMock()
        sess = bfu.BFUSession(
            refs=["R1"], tokens=TOKENS, update_field="node", value="VALUATION_STAMP_DUTY_COMPLETED",
        )
        sess.session = MagicMock()
        sess.session.post.side_effect = RuntimeError("boom")
        ctx.user_data = {"bfu_session": sess}
        with patch.object(bfu, "_bfu_fetch_snapshots", return_value={}), \
             patch.object(bfu, "append_bfu_history") as mock_append:
            _run(bfu.recv_bfu_confirm(update, ctx))
        mock_append.assert_not_called()


class TestBfuFetchSnapshot(unittest.TestCase):
    def test_ref_not_found_returns_empty_dict(self):
        with patch.object(bfu, "_search_ref_dlv", return_value=None):
            self.assertEqual(bfu._bfu_fetch_snapshot(TOKENS, "R1"), {})

    def test_empty_detail_returns_empty_dict(self):
        with patch.object(bfu, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(bfu, "_fetch_ref_detail_dlv", return_value=None):
            self.assertEqual(bfu._bfu_fetch_snapshot(TOKENS, "R1"), {})

    def test_search_exception_returns_empty_dict(self):
        with patch.object(bfu, "_search_ref_dlv", side_effect=RuntimeError("boom")):
            self.assertEqual(bfu._bfu_fetch_snapshot(TOKENS, "R1"), {})

    def test_found_detail_extracts_valuer_status_node(self):
        detail = {
            "application_status": "ONGOING",
            "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
            "actors": [
                {"role": "ASSESSOR_OF_STAMP_DUTY", "user_details": {"names": "Some Assessor"}},
                {"role": "VALUATION OFFICER", "user_details": {"names": "Jane Doe"}},
            ],
        }
        with patch.object(bfu, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(bfu, "_fetch_ref_detail_dlv", return_value=detail):
            snap = bfu._bfu_fetch_snapshot(TOKENS, "R1")
        self.assertEqual(snap, {
            "valuer": "Jane Doe", "application_status": "ONGOING", "node": "VALUATION_STAMP_DUTY_VALUER_REPORT",
        })

    def test_no_valuation_officer_actor_yields_empty_valuer(self):
        detail = {
            "application_status": "ONGOING", "node": "VALUATION_STAMP_DUTY_CREATED",
            "actors": [{"role": "ASSESSOR_OF_STAMP_DUTY", "user_details": {"names": "Some Assessor"}}],
        }
        with patch.object(bfu, "_search_ref_dlv", return_value={"id": "1"}), \
             patch.object(bfu, "_fetch_ref_detail_dlv", return_value=detail):
            snap = bfu._bfu_fetch_snapshot(TOKENS, "R1")
        self.assertEqual(snap["valuer"], "")


class TestBfuFetchSnapshots(unittest.TestCase):
    def test_returns_one_entry_per_ref(self):
        def fake_snapshot(tokens, ref):
            return {"valuer": f"Valuer-{ref}", "application_status": "ONGOING", "node": "N"}
        with patch.object(bfu, "_bfu_fetch_snapshot", side_effect=fake_snapshot):
            snapshots = bfu._bfu_fetch_snapshots(TOKENS, ["R1", "R2"])
        self.assertEqual(snapshots["R1"]["valuer"], "Valuer-R1")
        self.assertEqual(snapshots["R2"]["valuer"], "Valuer-R2")

    def test_one_ref_exception_does_not_affect_others(self):
        def fake_snapshot(tokens, ref):
            if ref == "R1":
                raise RuntimeError("boom")
            return {"valuer": "OK", "application_status": "ONGOING", "node": "N"}
        with patch.object(bfu, "_bfu_fetch_snapshot", side_effect=fake_snapshot):
            snapshots = bfu._bfu_fetch_snapshots(TOKENS, ["R1", "R2"])
        self.assertEqual(snapshots["R1"], {})
        self.assertEqual(snapshots["R2"]["valuer"], "OK")


class TestBfuHistoryPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.tmpdir.name, "saved_bfu_history.json")
        self._patch = patch.object(bfu, "SAVED_BFU_HISTORY_FILE", self.data_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(bfu.load_bfu_history(), [])

    def test_append_then_load_roundtrip(self):
        bfu.append_bfu_history([{"reference_number": "R1"}])
        self.assertEqual(bfu.load_bfu_history(), [{"reference_number": "R1"}])

    def test_append_does_not_overwrite_existing_entries(self):
        bfu.append_bfu_history([{"reference_number": "R1"}])
        bfu.append_bfu_history([{"reference_number": "R2"}])
        self.assertEqual(
            bfu.load_bfu_history(),
            [{"reference_number": "R1"}, {"reference_number": "R2"}],
        )


class TestBfuHistoryNotifyJob(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.bot.send_message = AsyncMock()

    def test_empty_history_still_sends_one_message(self):
        with patch.object(bfu, "load_bfu_history", return_value=[]), \
             patch.object(bfu, "ADMIN_IDS", {111}):
            _run(bfu._bfu_history_notify_job(self.ctx))
        self.assertEqual(self.ctx.bot.send_message.call_count, 1)
        text = self.ctx.bot.send_message.call_args[0][1]
        self.assertIn("No records yet", text)

    def test_sends_to_every_admin_independently(self):
        history = [{
            "reference_number": "R1", "initial_valuer": "Jane", "new_valuer": "John",
            "initial_application_status": "ONGOING", "new_application_status": "ONGOING",
            "initial_node": "N1", "new_node": "N2", "recorded_at": "2026-01-01T00:00:00",
        }]
        with patch.object(bfu, "load_bfu_history", return_value=history), \
             patch.object(bfu, "ADMIN_IDS", {111, 222}):
            _run(bfu._bfu_history_notify_job(self.ctx))
        self.assertEqual(self.ctx.bot.send_message.call_count, 2)
        sent_ids = {c.args[0] for c in self.ctx.bot.send_message.call_args_list}
        self.assertEqual(sent_ids, {111, 222})
        text = self.ctx.bot.send_message.call_args_list[0].args[1]
        self.assertIn("R1", text)
        self.assertIn("Jane", text)
        self.assertIn("John", text)

    def test_one_admin_failure_does_not_block_the_other(self):
        async def flaky_send(chat_id, *args, **kwargs):
            if chat_id == 111:
                raise RuntimeError("blocked the bot")
        self.ctx.bot.send_message = AsyncMock(side_effect=flaky_send)
        with patch.object(bfu, "load_bfu_history", return_value=[{"reference_number": "R1"}]), \
             patch.object(bfu, "ADMIN_IDS", {111, 222}):
            _run(bfu._bfu_history_notify_job(self.ctx))
        self.assertEqual(self.ctx.bot.send_message.call_count, 2)


if __name__ == "__main__":
    unittest.main()
