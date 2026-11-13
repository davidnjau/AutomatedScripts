#!/usr/bin/env python3
"""
Unit tests for ocr_stats.py — the OCR usage counter + approximate RunPod
GPU-credit tracker.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ocr_stats as os_mod


def _run(coro):
    return asyncio.run(coro)


class _TmpStore(unittest.TestCase):
    """Base: isolate saved_ocr_counts.json to a temp file per test."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.tmpdir.name, "saved_ocr_counts.json")
        self._p = patch.object(os_mod, "SAVED_OCR_COUNTS_FILE", self.data_file)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def _write(self, data):
        with open(self.data_file, "w") as f:
            json.dump(data, f)


class TestLoadDefaults(_TmpStore):
    def test_missing_file_returns_full_defaults(self):
        d = os_mod.load_ocr_counts()
        self.assertEqual(d["counts"], {})
        self.assertEqual(d["spent_usd"], 0.0)
        self.assertEqual(d["starting_balance_usd"], 0.0)
        self.assertEqual(d["cost_per_extraction_usd"], os_mod._DEFAULT_COST_PER_EXTRACTION)

    def test_partial_file_is_merged_with_defaults(self):
        self._write({"starting_balance_usd": 29.87})
        d = os_mod.load_ocr_counts()
        self.assertEqual(d["starting_balance_usd"], 29.87)
        self.assertEqual(d["counts"], {})          # default filled in
        self.assertEqual(d["spent_usd"], 0.0)       # default filled in


class TestRecordExtraction(_TmpStore):
    def test_glm_ocr_counts_and_deducts(self):
        self._write({"starting_balance_usd": 30.0, "spent_usd": 0.0, "cost_per_extraction_usd": 0.02, "counts": {}})
        os_mod.record_extraction("glm-ocr")
        d = os_mod.load_ocr_counts()
        self.assertEqual(d["counts"]["glm-ocr"], 1)
        self.assertEqual(d["spent_usd"], 0.02)
        self.assertEqual(os_mod._remaining(d), 29.98)

    def test_non_billable_source_counts_but_does_not_deduct(self):
        self._write({"starting_balance_usd": 30.0, "spent_usd": 0.0, "cost_per_extraction_usd": 0.02, "counts": {}})
        os_mod.record_extraction("claude")
        os_mod.record_extraction("tesseract")
        os_mod.record_extraction("none")
        d = os_mod.load_ocr_counts()
        self.assertEqual(d["counts"], {"claude": 1, "tesseract": 1, "none": 1})
        self.assertEqual(d["spent_usd"], 0.0)       # nothing deducted

    def test_repeated_glm_accumulates(self):
        self._write({"starting_balance_usd": 1.0, "spent_usd": 0.0, "cost_per_extraction_usd": 0.02, "counts": {}})
        for _ in range(5):
            os_mod.record_extraction("glm-ocr")
        d = os_mod.load_ocr_counts()
        self.assertEqual(d["counts"]["glm-ocr"], 5)
        self.assertAlmostEqual(d["spent_usd"], 0.10, places=4)
        self.assertAlmostEqual(os_mod._remaining(d), 0.90, places=4)

    def test_unknown_source_is_still_counted(self):
        os_mod.record_extraction("weird")
        self.assertEqual(os_mod.load_ocr_counts()["counts"]["weird"], 1)


class TestDerived(_TmpStore):
    def test_total_extractions_sums_counts(self):
        d = {"counts": {"glm-ocr": 2, "claude": 1, "tesseract": 3}}
        self.assertEqual(os_mod._total_extractions(d), 6)

    def test_remaining_is_balance_minus_spent(self):
        d = {"starting_balance_usd": 29.87, "spent_usd": 0.06}
        self.assertEqual(os_mod._remaining(d), 29.81)

    def test_stats_text_includes_key_figures(self):
        d = {"counts": {"glm-ocr": 3}, "cost_per_extraction_usd": 0.02,
             "starting_balance_usd": 29.87, "spent_usd": 0.06}
        text = os_mod._os_stats_text(d)
        self.assertIn("GLM-OCR (GPU): 3", text)
        self.assertIn("$29.81", text)   # remaining


class TestParseUsd(unittest.TestCase):
    def test_parses_plain_number(self):
        self.assertEqual(os_mod._parse_usd("29.87"), 29.87)

    def test_strips_dollar_and_commas(self):
        self.assertEqual(os_mod._parse_usd("$1,234.50"), 1234.50)

    def test_rejects_negative(self):
        self.assertIsNone(os_mod._parse_usd("-5"))

    def test_rejects_non_number(self):
        self.assertIsNone(os_mod._parse_usd("abc"))


class TestCmdOcrStats(_TmpStore):
    def _update(self, user_id=1):
        update = MagicMock()
        update.effective_user.id = user_id
        update.message.reply_text = AsyncMock()
        return update

    def test_non_admin_denied(self):
        update = self._update(user_id=2)
        with patch.object(os_mod, "allowed", return_value=True), \
             patch.object(os_mod, "is_admin", return_value=False):
            result = _run(os_mod.cmd_ocr_stats(update, MagicMock()))
        self.assertEqual(result, os_mod.ConversationHandler.END)
        self.assertIn("Admin access required", update.message.reply_text.call_args[0][0])

    def test_admin_sees_stats_menu(self):
        update = self._update(user_id=1)
        with patch.object(os_mod, "allowed", return_value=True), \
             patch.object(os_mod, "is_admin", return_value=True):
            result = _run(os_mod.cmd_ocr_stats(update, MagicMock()))
        self.assertEqual(result, os_mod.OS.MENU)
        self.assertIn("OCR Usage", update.message.reply_text.call_args[0][0])


class TestRecvOsMenu(_TmpStore):
    def _query(self, data):
        update = MagicMock()
        q = update.callback_query
        q.data = data
        q.from_user.id = 1
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.edit_message_reply_markup = AsyncMock()
        return update

    def test_close_ends(self):
        update = self._query("ocr:close")
        with patch.object(os_mod, "is_admin", return_value=True):
            result = _run(os_mod.recv_os_menu(update, MagicMock()))
        self.assertEqual(result, os_mod.ConversationHandler.END)

    def test_balance_prompts(self):
        update = self._query("ocr:balance")
        with patch.object(os_mod, "is_admin", return_value=True):
            result = _run(os_mod.recv_os_menu(update, MagicMock()))
        self.assertEqual(result, os_mod.OS.SET_BALANCE)

    def test_cost_prompts(self):
        update = self._query("ocr:cost")
        with patch.object(os_mod, "is_admin", return_value=True):
            result = _run(os_mod.recv_os_menu(update, MagicMock()))
        self.assertEqual(result, os_mod.OS.SET_COST)

    def test_reset_zeroes_counts_and_spend(self):
        self._write({"counts": {"glm-ocr": 5}, "spent_usd": 0.10, "starting_balance_usd": 30.0})
        update = self._query("ocr:reset")
        with patch.object(os_mod, "is_admin", return_value=True):
            result = _run(os_mod.recv_os_menu(update, MagicMock()))
        self.assertEqual(result, os_mod.OS.MENU)
        d = os_mod.load_ocr_counts()
        self.assertEqual(d["counts"], {})
        self.assertEqual(d["spent_usd"], 0.0)
        self.assertEqual(d["starting_balance_usd"], 30.0)   # balance untouched


class TestRecvOsSetters(_TmpStore):
    def _msg(self, text):
        update = MagicMock()
        update.message.text = text
        update.message.reply_text = AsyncMock()
        return update

    def test_set_balance_rebaselines_spend(self):
        self._write({"counts": {"glm-ocr": 3}, "spent_usd": 0.06, "starting_balance_usd": 0.0})
        update = self._msg("29.87")
        result = _run(os_mod.recv_os_set_balance(update, MagicMock()))
        self.assertEqual(result, os_mod.OS.MENU)
        d = os_mod.load_ocr_counts()
        self.assertEqual(d["starting_balance_usd"], 29.87)
        self.assertEqual(d["spent_usd"], 0.0)               # re-baselined
        self.assertEqual(d["counts"], {"glm-ocr": 3})       # counts kept

    def test_set_balance_rejects_bad_input(self):
        update = self._msg("not a number")
        result = _run(os_mod.recv_os_set_balance(update, MagicMock()))
        self.assertEqual(result, os_mod.OS.SET_BALANCE)

    def test_set_cost_persists(self):
        update = self._msg("0.03")
        result = _run(os_mod.recv_os_set_cost(update, MagicMock()))
        self.assertEqual(result, os_mod.OS.MENU)
        self.assertEqual(os_mod.load_ocr_counts()["cost_per_extraction_usd"], 0.03)

    def test_set_cost_rejects_bad_input(self):
        update = self._msg("free")
        result = _run(os_mod.recv_os_set_cost(update, MagicMock()))
        self.assertEqual(result, os_mod.OS.SET_COST)


if __name__ == "__main__":
    unittest.main()
