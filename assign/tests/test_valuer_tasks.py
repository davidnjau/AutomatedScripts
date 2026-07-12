#!/usr/bin/env python3
"""
Unit tests for valuer_tasks.py — _vt_fetch_all_tasks' cutoff-based paging
stop, _vt_fetch_task_detail's token-rotation/retry behavior, and the
cmd_valuer_tasks/recv_vt_name/recv_vt_select/_vt_start_run handlers.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import token_rotator
import valuer_tasks as vt
from ardhisasa_auth import AuthTokens
from token_rotator import _AllTokensExhausted, _TokenRotator

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


class TestVtFetchAllTasks(unittest.TestCase):
    def test_stops_paging_once_past_cutoff(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {
                "results": [
                    {"date_created": "2026-01-10", "id": "1"},
                    {"date_created": "2025-01-01", "id": "2"},   # older than cutoff
                ],
                "next": True,
            },
        )
        with patch.object(vt, "_within_days", side_effect=lambda d, c: d == "2026-01-10"):
            tasks = vt._vt_fetch_all_tasks(fake_session, {}, "Ongoing", "2026-01-01")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(fake_session.get.call_count, 1)   # stopped after hitting the old one

    def test_no_next_page_stops_after_one_call(self):
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [{"date_created": "2026-01-10", "id": "1"}], "next": False},
        )
        with patch.object(vt, "_within_days", return_value=True):
            tasks = vt._vt_fetch_all_tasks(fake_session, {}, "Ongoing", "2026-01-01")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(fake_session.get.call_count, 1)

    def test_exception_breaks_and_returns_partial(self):
        fake_session = MagicMock()
        fake_session.get.side_effect = RuntimeError("network down")
        tasks = vt._vt_fetch_all_tasks(fake_session, {}, "Ongoing", "2026-01-01")
        self.assertEqual(tasks, [])


class TestVtFetchTaskDetail(unittest.TestCase):
    def test_success_returns_json(self):
        rotator = _TokenRotator([("staff", TOKENS)])
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            status_code=200, raise_for_status=lambda: None, json=lambda: {"node": "X"}
        )
        result = vt._vt_fetch_task_detail(fake_session, rotator, "task-1")
        self.assertEqual(result, {"node": "X"})

    def test_all_tokens_exhausted_raises(self):
        rotator = _TokenRotator([])
        fake_session = MagicMock()
        with self.assertRaises(_AllTokensExhausted):
            vt._vt_fetch_task_detail(fake_session, rotator, "task-1")

    def test_403_rotates_then_succeeds(self):
        rotator = _TokenRotator([("staff", TOKENS), ("staff2", TOKENS)])
        fake_session = MagicMock()
        fake_session.get.side_effect = [
            MagicMock(status_code=403),
            MagicMock(status_code=200, raise_for_status=lambda: None, json=lambda: {"node": "Y"}),
        ]
        with patch.object(token_rotator.time, "sleep"):
            result = vt._vt_fetch_task_detail(fake_session, rotator, "task-1")
        self.assertEqual(result, {"node": "Y"})

    def test_exhausts_all_retries_on_persistent_5xx(self):
        rotator = _TokenRotator([("staff", TOKENS)])
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(status_code=503)
        with patch.object(token_rotator.time, "sleep"):
            with self.assertRaises(RuntimeError):
                vt._vt_fetch_task_detail(fake_session, rotator, "task-1")


class TestCmdValuerTasks(unittest.TestCase):
    def test_no_valid_tokens_ends_conversation(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(vt, "allowed", return_value=True), \
             patch.object(vt, "_be_cred_keyboard", return_value=None):
            result = _run(vt.cmd_valuer_tasks(update, ctx))
        self.assertEqual(result, vt.ConversationHandler.END)

    def test_valid_tokens_ask_for_credential(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(vt, "allowed", return_value=True), \
             patch.object(vt, "_be_cred_keyboard", return_value=MagicMock()):
            result = _run(vt.cmd_valuer_tasks(update, ctx))
        self.assertEqual(result, vt.VT.PICK_CRED)


class TestRecvVtName(unittest.TestCase):
    def test_blank_name_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        ctx.user_data = {"vt_session": vt.VTSession(cred_type="staff")}
        with patch.object(vt, "allowed", return_value=True):
            result = _run(vt.recv_vt_name(update, ctx))
        self.assertEqual(result, vt.VT.STAFF_NAME)

    def test_expired_tokens_end_conversation(self):
        update = _make_update_with_message("Jane")
        ctx = MagicMock()
        ctx.user_data = {"vt_session": vt.VTSession(cred_type="staff")}
        with patch.object(vt, "allowed", return_value=True), \
             patch.object(vt, "get_valid_tokens", return_value=None):
            result = _run(vt.recv_vt_name(update, ctx))
        self.assertEqual(result, vt.ConversationHandler.END)

    def test_no_results_reprompts(self):
        update = _make_update_with_message("Nobody")
        ctx = MagicMock()
        ctx.user_data = {"vt_session": vt.VTSession(cred_type="staff")}
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(raise_for_status=lambda: None, json=lambda: {"results": []})
        with patch.object(vt, "allowed", return_value=True), \
             patch.object(vt, "get_valid_tokens", return_value=TOKENS), \
             patch.object(vt, "build_session", return_value=fake_session):
            result = _run(vt.recv_vt_name(update, ctx))
        self.assertEqual(result, vt.VT.STAFF_NAME)

    def test_results_found_shows_selection(self):
        update = _make_update_with_message("Jane")
        ctx = MagicMock()
        ctx.user_data = {"vt_session": vt.VTSession(cred_type="staff")}
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": [{"staff_details": {"firstname": "Jane", "lastname": "Doe"}, "id": "1"}]},
        )
        with patch.object(vt, "allowed", return_value=True), \
             patch.object(vt, "get_valid_tokens", return_value=TOKENS), \
             patch.object(vt, "build_session", return_value=fake_session):
            result = _run(vt.recv_vt_name(update, ctx))
        self.assertEqual(result, vt.VT.SELECT_STAFF)
        self.assertEqual(len(ctx.user_data["vt_search_results"]), 1)


class TestRecvVtSelect(unittest.TestCase):
    def test_invalid_index_ends_conversation(self):
        update = _make_update_with_callback("vt_staff:5")
        ctx = MagicMock()
        ctx.user_data = {"vt_search_results": []}
        ctx.bot.send_message = AsyncMock()
        with patch.object(vt, "allowed", return_value=True):
            result = _run(vt.recv_vt_select(update, ctx))
        self.assertEqual(result, vt.ConversationHandler.END)

    def test_valid_selection_asks_days_back(self):
        update = _make_update_with_callback("vt_staff:0")
        ctx = MagicMock()
        ctx.user_data = {
            "vt_search_results": [
                {"staff_details": {"firstname": "Jane", "lastname": "Doe", "user_id": "u1"}, "id": "1"}
            ]
        }
        with patch.object(vt, "allowed", return_value=True):
            result = _run(vt.recv_vt_select(update, ctx))
        self.assertEqual(result, vt.VT.DAYS_BACK)


class TestVtStartRun(unittest.TestCase):
    def test_expired_tokens_end_conversation(self):
        ctx = MagicMock()
        ctx.user_data = {"vt_session": vt.VTSession(cred_type="staff")}
        ctx.bot.send_message = AsyncMock()
        reply_msg = MagicMock()
        reply_msg.reply_text = AsyncMock()
        with patch.object(vt, "get_valid_tokens", return_value=None):
            result = _run(vt._vt_start_run(555, ctx, 30, reply_msg=reply_msg))
        self.assertEqual(result, vt.ConversationHandler.END)

    def test_valid_tokens_schedules_background_run(self):
        ctx = MagicMock()
        ctx.user_data = {"vt_session": vt.VTSession(cred_type="staff", valuer_name="Jane", valuer_uid="u1")}
        ctx.bot.send_message = AsyncMock()
        reply_msg = MagicMock()
        reply_msg.reply_text = AsyncMock()
        with patch.object(vt, "get_valid_tokens", return_value=TOKENS), \
             patch.object(vt.asyncio, "to_thread", new=AsyncMock()), \
             patch.object(vt.asyncio, "ensure_future", side_effect=lambda coro: coro.close()):
            result = _run(vt._vt_start_run(555, ctx, 30, reply_msg=reply_msg))
        self.assertEqual(result, vt.ConversationHandler.END)


if __name__ == "__main__":
    unittest.main()
