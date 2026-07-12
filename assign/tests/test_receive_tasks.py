#!/usr/bin/env python3
"""
Unit tests for receive_tasks.py — _validate_staff's eligibility branches,
_verify_and_filter_tasks' amount-range/task-count filtering, the task-batch
and schedule persistence helpers, and the cmd_receive / recv_rt_task_count /
recv_rt_amount_choice conversation handlers.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import receive_tasks as rt_mod
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


class TestValidateStaff(unittest.TestCase):
    def test_inactive_account_rejected(self):
        ok, err, *_ = rt_mod._validate_staff({"account_status": "SUSPENDED"})
        self.assertFalse(ok)
        self.assertIn("ACTIVE", err)

    def test_wrong_department_rejected(self):
        user = {
            "account_status": "ACTIVE",
            "staff_details": {"department_details": {"department": {"code": "REG"}}},
        }
        ok, err, *_ = rt_mod._validate_staff(user)
        self.assertFalse(ok)
        self.assertIn("DLV", err)

    def test_missing_valuer_role_rejected(self):
        user = {
            "account_status": "ACTIVE",
            "staff_details": {
                "department_details": {"department": {"code": "DLV"}},
                "roles": [{"rolename": "ASSESSOR"}],
            },
        }
        ok, err, *_ = rt_mod._validate_staff(user)
        self.assertFalse(ok)
        self.assertIn("VALUER", err)

    def test_valuer_only_returns_stamp_duty(self):
        user = {
            "account_status": "ACTIVE",
            "staff_details": {
                "department_details": {"department": {"code": "DLV"}},
                "roles": [{"rolename": "VALUER"}],
            },
        }
        ok, err, task_type, registry, county = rt_mod._validate_staff(user)
        self.assertTrue(ok)
        self.assertEqual(task_type, "STAMP_DUTY")
        self.assertEqual(registry, "")
        self.assertEqual(county, "")

    def test_county_valuer_without_units_rejected(self):
        user = {
            "account_status": "ACTIVE",
            "staff_details": {
                "department_details": {"department": {"code": "DLV"}},
                "roles": [{"rolename": "VALUER"}, {"rolename": "COUNTY_VALUER"}],
                "county_units": [],
            },
        }
        ok, err, *_ = rt_mod._validate_staff(user)
        self.assertFalse(ok)
        self.assertIn("county_units", err)

    def test_county_valuer_returns_both_with_primary_unit(self):
        user = {
            "account_status": "ACTIVE",
            "staff_details": {
                "department_details": {"department": {"code": "DLV"}},
                "roles": [{"rolename": "VALUER"}, {"rolename": "COUNTY_VALUER"}],
                "county_units": [
                    {"registry": "thika", "county": "kiambu", "is_primary": False},
                    {"registry": "kiambu", "county": "kiambu", "is_primary": True},
                ],
            },
        }
        ok, err, task_type, registry, county = rt_mod._validate_staff(user)
        self.assertTrue(ok)
        self.assertEqual(task_type, "BOTH")
        self.assertEqual(registry, "KIAMBU")
        self.assertEqual(county, "KIAMBU")


class TestVerifyAndFilterTasks(unittest.TestCase):
    def _rt(self, **kwargs):
        rt = rt_mod.RTSession()
        rt.task_count = kwargs.get("task_count", 5)
        rt.amount_min = kwargs.get("amount_min")
        rt.amount_max = kwargs.get("amount_max")
        return rt

    def test_stops_once_task_count_reached(self):
        rt = self._rt(task_count=1)
        candidates = [{"id": "1", "reference_number": "R1"}, {"id": "2", "reference_number": "R2"}]
        detail = {
            "external_process_details": {"consideration_amount": "1000"},
        }
        with patch.object(rt_mod, "_fetch_task_detail", return_value=detail) as mock_fetch:
            matched = rt_mod._verify_and_filter_tasks(rt, candidates)
        self.assertEqual(len(matched), 1)
        mock_fetch.assert_called_once()

    def test_none_detail_is_skipped(self):
        rt = self._rt()
        candidates = [{"id": "1", "reference_number": "R1"}]
        with patch.object(rt_mod, "_fetch_task_detail", return_value=None):
            matched = rt_mod._verify_and_filter_tasks(rt, candidates)
        self.assertEqual(matched, [])

    def test_amount_below_min_is_filtered(self):
        rt = self._rt(amount_min=1_000_000)
        candidates = [{"id": "1", "reference_number": "R1"}]
        detail = {"external_process_details": {"consideration_amount": "500000"}}
        with patch.object(rt_mod, "_fetch_task_detail", return_value=detail):
            matched = rt_mod._verify_and_filter_tasks(rt, candidates)
        self.assertEqual(matched, [])

    def test_amount_above_max_is_filtered(self):
        rt = self._rt(amount_max=1_000_000)
        candidates = [{"id": "1", "reference_number": "R1"}]
        detail = {"external_process_details": {"consideration_amount": "5000000"}}
        with patch.object(rt_mod, "_fetch_task_detail", return_value=detail):
            matched = rt_mod._verify_and_filter_tasks(rt, candidates)
        self.assertEqual(matched, [])

    def test_amount_within_range_is_matched(self):
        rt = self._rt(amount_min=100, amount_max=1_000_000)
        candidates = [{"id": "1", "reference_number": "R1", "parcel_number": "P1",
                       "registry": "NAIROBI", "county": "NAIROBI", "date_created": "2026-01-01"}]
        detail = {"external_process_details": {"consideration_amount": "500000"}}
        with patch.object(rt_mod, "_fetch_task_detail", return_value=detail):
            matched = rt_mod._verify_and_filter_tasks(rt, candidates)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["consideration_amount"], 500000.0)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.batches_file = os.path.join(self.tmpdir.name, "saved_task_batches.json")
        self.schedules_file = os.path.join(self.tmpdir.name, "saved_schedules.json")
        self._patches = [
            patch.object(rt_mod, "SAVED_TASK_BATCHES_FILE", self.batches_file),
            patch.object(rt_mod, "SAVED_SCHEDULES_FILE", self.schedules_file),
            patch.object(rt_mod, "_ensure_data_dir"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def test_load_missing_batches_returns_empty(self):
        self.assertEqual(rt_mod.load_task_batches(), [])

    def test_persist_and_load_task_batch_roundtrip(self):
        batch = {"batch_id": "b1", "tasks": [{"reference_number": "R1"}], "failed": []}
        rt_mod.persist_task_batch(batch)
        batches = rt_mod.load_task_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch_id"], "b1")

    def test_persist_task_batch_caps_at_100(self):
        for i in range(105):
            rt_mod.persist_task_batch({"batch_id": f"b{i}", "tasks": [], "failed": []})
        batches = rt_mod.load_task_batches()
        self.assertEqual(len(batches), 100)
        self.assertEqual(batches[0]["batch_id"], "b5")

    def test_load_missing_schedules_returns_empty(self):
        self.assertEqual(rt_mod.load_schedules(), [])

    def test_persist_schedule_replaces_existing_by_id(self):
        sched1 = {"schedule_id": "s1", "interval_minutes": 30}
        sched1_updated = {"schedule_id": "s1", "interval_minutes": 60}
        rt_mod.persist_schedule(sched1)
        rt_mod.persist_schedule(sched1_updated)
        schedules = rt_mod.load_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["interval_minutes"], 60)


class TestCmdReceive(unittest.TestCase):
    def test_no_saved_valuers_asks_for_name(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(rt_mod, "allowed", return_value=True), \
             patch.object(rt_mod, "load_saved_valuers", return_value=[]):
            result = _run(rt_mod.cmd_receive(update, ctx))
        self.assertEqual(result, rt_mod.RS.STAFF_NAME)

    def test_saved_valuers_show_pick_source(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {}
        saved = [{"name": "Jane Doe", "uid": "1", "account_number": "A1"}]
        with patch.object(rt_mod, "allowed", return_value=True), \
             patch.object(rt_mod, "load_saved_valuers", return_value=saved):
            result = _run(rt_mod.cmd_receive(update, ctx))
        self.assertEqual(result, rt_mod.RS.PICK_STAFF_SOURCE)


class TestRecvRtTaskCount(unittest.TestCase):
    def test_non_numeric_reprompts(self):
        update = _make_update_with_message("abc")
        ctx = MagicMock()
        ctx.user_data = {}
        result = _run(rt_mod.recv_rt_task_count(update, ctx))
        self.assertEqual(result, rt_mod.RS.TASK_COUNT)

    def test_zero_reprompts(self):
        update = _make_update_with_message("0")
        ctx = MagicMock()
        ctx.user_data = {}
        result = _run(rt_mod.recv_rt_task_count(update, ctx))
        self.assertEqual(result, rt_mod.RS.TASK_COUNT)

    def test_valid_count_advances_to_amount_range(self):
        update = _make_update_with_message("10")
        ctx = MagicMock()
        ctx.user_data = {}
        result = _run(rt_mod.recv_rt_task_count(update, ctx))
        self.assertEqual(result, rt_mod.RS.AMOUNT_RANGE)
        self.assertEqual(ctx.user_data["rt_session"].task_count, 10)


class TestDoAssignTasks(unittest.TestCase):
    def test_large_result_set_paginates_instead_of_truncating(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        http_sess = MagicMock()
        http_sess.post.return_value = MagicMock(raise_for_status=lambda: None)
        tasks = [
            {"reference_number": f"REF/{i}", "consideration_amount": 1000 + i}
            for i in range(400)
        ]
        with patch.object(rt_mod, "persist_assignment"), \
             patch.object(rt_mod, "persist_task_batch"):
            _run(rt_mod._do_assign_tasks(bot, 555, http_sess, TOKENS, tasks, "uid1", "Jane", "staff"))
        sent_texts = [c.args[1] for c in bot.send_message.call_args_list]
        self.assertGreater(len(sent_texts), 1)
        self.assertTrue(any("REF/0" in t for t in sent_texts))
        self.assertTrue(any("REF/399" in t for t in sent_texts))


class TestRtFetchAndShow(unittest.TestCase):
    def test_large_matched_list_paginates_instead_of_collapsing(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        rt = rt_mod.RTSession()
        rt.task_type = "STAMP_DUTY"
        rt.tokens = TOKENS
        rt.session = MagicMock()
        rt.session.get.side_effect = RuntimeError("skip peek")
        rt.task_count = 400
        rt.staff_data = {"staff_details": {"firstname": "Jane", "lastname": "Doe"}}

        matched = [
            {
                "reference_number": f"REF/{i}",
                "consideration_amount": 1000 + i,
                "parcel_number": "P1",
                "registry": "NAIROBI",
                "date_created": "2026-01-01",
            }
            for i in range(400)
        ]
        with patch.object(rt_mod, "_fetch_tasks", return_value=[{"id": str(i)} for i in range(400)]), \
             patch.object(rt_mod, "_verify_and_filter_tasks", return_value=matched):
            result = _run(rt_mod._rt_fetch_and_show(message, rt))
        self.assertEqual(result, rt_mod.RS.RT_CONFIRM)
        sent_texts = [c.args[0] for c in message.reply_text.call_args_list]
        self.assertGreater(len(sent_texts), 1)
        self.assertTrue(any("REF/0" in t for t in sent_texts))
        self.assertTrue(any("REF/399" in t for t in sent_texts))


class TestRecvRtAmountChoice(unittest.TestCase):
    def test_custom_asks_for_text_input(self):
        update = _make_update_with_callback("ft_amount:custom")
        ctx = MagicMock()
        ctx.user_data = {}
        result = _run(rt_mod.recv_rt_amount_choice(update, ctx))
        self.assertEqual(result, rt_mod.RS.AMOUNT_TEXT)

    def test_preset_range_sets_min_max_and_advances(self):
        update = _make_update_with_callback("ft_amount:1m_5m")
        ctx = MagicMock()
        ctx.user_data = {}
        result = _run(rt_mod.recv_rt_amount_choice(update, ctx))
        self.assertEqual(result, rt_mod.RS.SCHEDULE_CHOICE)
        rt = ctx.user_data["rt_session"]
        self.assertEqual(rt.amount_min, 1_000_000.0)
        self.assertEqual(rt.amount_max, 5_000_000.0)


if __name__ == "__main__":
    unittest.main()
