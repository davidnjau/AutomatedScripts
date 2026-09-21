#!/usr/bin/env python3
"""
Unit tests for common.py additions made during the Sectional Properties
extraction: _safe_err (promoted from bot.py, shared by New Assignment/
Receive Tasks/Sectional Properties) and load_sectional_config/
save_sectional_config (shared by Sectional Properties and Auto Fetch).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import common
import dlv_core


class TestSafeErr(unittest.TestCase):
    def test_http_error_reduced_to_status_code(self):
        response = MagicMock(status_code=404)
        err = requests.HTTPError("not found")
        err.response = response
        self.assertEqual(common._safe_err(err), "server returned HTTP 404")

    def test_http_error_without_response_falls_back(self):
        err = requests.HTTPError("boom")
        err.response = None
        self.assertEqual(common._safe_err(err), "unexpected error — check logs")

    def test_non_http_exception_is_generic(self):
        self.assertEqual(common._safe_err(RuntimeError("secret detail")), "unexpected error — check logs")
        # the original message must never leak through
        self.assertNotIn("secret detail", common._safe_err(RuntimeError("secret detail")))


class TestMdEscape(unittest.TestCase):
    """md_escape — escapes legacy Telegram Markdown's special characters
    (_, *, `, [) so untrusted text can't break parse_mode="Markdown"."""

    def test_escapes_underscore(self):
        self.assertEqual(common.md_escape("Jane_Doe"), "Jane\\_Doe")

    def test_escapes_asterisk(self):
        self.assertEqual(common.md_escape("Jane*Doe"), "Jane\\*Doe")

    def test_escapes_backtick_and_bracket(self):
        self.assertEqual(common.md_escape("a`b[c"), "a\\`b\\[c")

    def test_plain_text_unchanged(self):
        self.assertEqual(common.md_escape("Jane Doe"), "Jane Doe")

    def test_empty_or_none_returns_empty_string(self):
        self.assertEqual(common.md_escape(""), "")
        self.assertEqual(common.md_escape(None), "")


class TestSectionalConfigPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg_file = os.path.join(self.tmpdir.name, "saved_sectional_config.json")
        self._patch = patch.object(common, "SAVED_SECTIONAL_CONFIG_FILE", self.cfg_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(common.load_sectional_config())

    def test_save_then_load_roundtrip(self):
        common.save_sectional_config({"auto_route": True, "cred_type": "staff2"})
        cfg = common.load_sectional_config()
        self.assertEqual(cfg["auto_route"], True)
        self.assertEqual(cfg["cred_type"], "staff2")


class TestCustomExclusionsPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.tmpdir.name, "saved_custom_exclusions.json")
        self._patch = patch.object(common, "SAVED_CUSTOM_EXCLUSIONS_FILE", self.data_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(common.load_custom_exclusions(), [])

    def test_save_then_load_roundtrip(self):
        common.save_custom_exclusions(["MAISONETTE", "TOWNHOUSE"])
        self.assertEqual(common.load_custom_exclusions(), ["MAISONETTE", "TOWNHOUSE"])


class TestPersistAssignment(unittest.TestCase):
    """persist_assignment — ref -> valuer_name/valuer_uid/assigned_at, plus
    whatever extra context a caller passes (e.g. DLV Batch's queue item).
    Backed by dlv_core's consolidated ref-keyed store (Group A JSON
    consolidation) — the file constants to isolate live on dlv_core, not
    common, since common.SAVED_ASSIGNMENTS_FILE is only read by dlv_core's
    legacy-migration path now, not by load/persist themselves."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.assign_file  = os.path.join(self.tmpdir.name, "saved_assignments.json")
        self.batch_file   = os.path.join(self.tmpdir.name, "saved_dlv_batch.json")
        self.closed_file  = os.path.join(self.tmpdir.name, "saved_dlv_closed.json")
        self.records_file = os.path.join(self.tmpdir.name, "saved_dlv_records.json")
        self._patches = [
            patch.object(dlv_core, "SAVED_ASSIGNMENTS_FILE", self.assign_file),
            patch.object(dlv_core, "SAVED_DLV_BATCH_FILE", self.batch_file),
            patch.object(dlv_core, "SAVED_DLV_CLOSED_FILE", self.closed_file),
            patch.object(dlv_core, "SAVED_DLV_RECORDS_FILE", self.records_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def test_basic_fields_saved_without_extra(self):
        common.persist_assignment("REF1", "Jane Doe", "uid-1")
        record = common.load_saved_assignments()["REF1"]
        self.assertEqual(record["valuer_name"], "Jane Doe")
        self.assertEqual(record["valuer_uid"], "uid-1")
        self.assertIn("assigned_at", record)

    def test_extra_fields_merged_in(self):
        common.persist_assignment("REF1", "Jane Doe", "uid-1", extra={
            "tag": "Queue", "parcel": "P1", "consideration": "1000000", "currency_code": "KES",
        })
        record = common.load_saved_assignments()["REF1"]
        self.assertEqual(record["tag"], "Queue")
        self.assertEqual(record["parcel"], "P1")
        self.assertEqual(record["consideration"], "1000000")
        self.assertEqual(record["currency_code"], "KES")
        # core fields still present alongside extra
        self.assertEqual(record["valuer_name"], "Jane Doe")

    def test_second_call_without_extra_does_not_drop_first_calls_enrichment(self):
        """Regression: persist_assignment used to overwrite the record
        wholesale, so a caller with no `extra` (e.g. receive_tasks.py)
        silently erased enrichment a prior richer call had added."""
        common.persist_assignment("REF1", "Jane Doe", "uid-1", extra={"parcel": "P1", "tag": "Queue"})
        common.persist_assignment("REF1", "Jane Doe", "uid-1")
        record = common.load_saved_assignments()["REF1"]
        self.assertEqual(record["parcel"], "P1")
        self.assertEqual(record["tag"], "Queue")

    def test_no_cap_on_number_of_tracked_assignments(self):
        """The old 500-entry rotation is intentionally gone — a count-based
        cap risked evicting a still-active record for being chronologically
        old, not for being irrelevant."""
        for i in range(510):
            common.persist_assignment(f"REF{i}", "Jane Doe", "uid-1")
        self.assertEqual(len(common.load_saved_assignments()), 510)

    def test_merely_queued_ref_does_not_leak_into_assignments(self):
        """Regression: a DLV Batch queue item always carries a valuer_uid
        (the valuer it's queued FOR) even before it's actually assigned —
        load_saved_assignments must not pick it up just because a
        valuer_uid is present, or it would double-appear in both
        "Currently Queued" and "At Valuer's Desk" in DLV Tasks reports."""
        dlv_core.save_dlv_batch([{"ref": "Q1", "valuer_name": "Jane", "valuer_uid": "u1"}])
        self.assertNotIn("Q1", common.load_saved_assignments())

    def test_reassigned_then_requeued_ref_does_not_double_appear(self):
        """Regression: a ref that was assigned and then re-queued into DLV
        Batch keeps its assigned_at (merge, not replace) but must not show
        up in load_saved_assignments() anymore — its CURRENT status is
        "queued", so it belongs only in "Currently Queued," not also in
        "At Valuer's Desk". Real production data surfaced this: a ref
        assigned then re-queued a minute later appeared in both sections
        of the same By Valuer report."""
        common.persist_assignment("REF1", "Jane Doe", "uid-1")
        dlv_core.save_dlv_batch([{"ref": "REF1", "valuer_name": "Jane Doe", "valuer_uid": "uid-1"}])
        self.assertNotIn("REF1", common.load_saved_assignments())
        self.assertEqual([i["ref"] for i in dlv_core.load_dlv_batch()], ["REF1"])

    def test_removed_ref_with_a_valuer_still_appears_in_assignments(self):
        """A ref dropped from the DLV queue (status="removed") but that
        still carries a valuer_uid must keep showing up here — being
        removed from the queue doesn't mean the underlying assignment
        itself was undone."""
        common.persist_assignment("REF1", "Jane Doe", "uid-1")
        dlv_core.mark_removed(["REF1"])
        record = common.load_saved_assignments()["REF1"]
        self.assertEqual(record["valuer_name"], "Jane Doe")



class TestFtAmountKeyboard(unittest.TestCase):
    """_ft_amount_keyboard — the shared amount-range picker used by Fetch
    Tasks/Auto Fetch/Receive Tasks."""

    def test_includes_10m_50m_range(self):
        kbd = common._ft_amount_keyboard()
        buttons = [b for row in kbd.inline_keyboard for b in row]
        texts = [b.text for b in buttons]
        callback_data = [b.callback_data for b in buttons]
        self.assertIn("10M – 50M", texts)
        self.assertIn("ft_amount:10m_50m", callback_data)

    def test_includes_custom_and_no_filter_options(self):
        kbd = common._ft_amount_keyboard()
        callback_data = [b.callback_data for row in kbd.inline_keyboard for b in row]
        self.assertIn("ft_amount:custom", callback_data)
        self.assertIn("ft_amount:all", callback_data)


class TestBeCredKeyboard(unittest.TestCase):
    def test_no_valid_tokens_returns_none(self):
        with patch.object(common, "get_valid_tokens", return_value=None):
            self.assertIsNone(common._be_cred_keyboard())

    def test_only_lists_creds_with_valid_tokens(self):
        with patch.object(common, "get_valid_tokens", side_effect=lambda k: "tok" if k == "staff2" else None):
            kbd = common._be_cred_keyboard()
        self.assertIsNotNone(kbd)
        self.assertEqual(len(kbd.inline_keyboard), 1)
        self.assertEqual(kbd.inline_keyboard[0][0].callback_data, "be_cred:staff2")


class TestNodeLabels(unittest.TestCase):
    def test_known_node_codes_have_labels(self):
        self.assertIn("VALUATION_STAMP_DUTY_CREATED", common._NODE_LABELS)
        self.assertIn("VALUATION_STAMP_DUTY_VALUER_REPORT", common._NODE_LABELS)
        self.assertIn("STAMP_DUTY_PAYMENT_DEFINITION", common._NODE_LABELS)


class TestMainMenu(unittest.TestCase):
    """_main_menu — the top-level menu now shows only the six category
    buttons (plus Cancel); workflow buttons live one level down in each
    category's own submenu (see TestCategoryMenu/TestMenuCategories)."""

    def _rows(self):
        return common._main_menu().keyboard

    def _all_texts(self):
        return [b.text for row in self._rows() for b in row]

    def test_shows_exactly_the_six_categories_plus_cancel(self):
        texts = self._all_texts()
        self.assertEqual(set(texts), {
            common.BTN_CAT_ASSIGNMENTS, common.BTN_CAT_AUTOMATION, common.BTN_CAT_ANALYTICS,
            common.BTN_CAT_LOOKUPS, common.BTN_CAT_VALUERS, common.BTN_CAT_SETTINGS,
            common.BTN_CANCEL,
        })

    def test_workflow_buttons_are_not_on_the_top_level_menu(self):
        texts = self._all_texts()
        for btn in (common.BTN_ASSIGN, common.BTN_FETCH_TASKS, common.BTN_LOOKUP,
                    common.BTN_VALUERS, common.BTN_AUTH, common.BTN_TASK_ANALYTICS):
            self.assertNotIn(btn, texts)


class TestMenuCategories(unittest.TestCase):
    """_MENU_CATEGORIES — every workflow button ends up in exactly one
    category, and the categories partition all 26 previously-visible
    buttons with nothing dropped or duplicated. Apartments/Sectional/AF
    Results/Valuer Tasks/Assignments stay hidden from every category,
    same as they were hidden from the old flat grid."""

    def _all_category_buttons(self):
        return [b for cat in common._MENU_CATEGORIES.values() for b in cat["buttons"]]

    def test_every_category_has_a_description_and_at_least_one_button(self):
        for label, cat in common._MENU_CATEGORIES.items():
            self.assertTrue(cat["description"])
            self.assertTrue(cat["buttons"])

    def test_no_button_appears_in_more_than_one_category(self):
        buttons = self._all_category_buttons()
        self.assertEqual(len(buttons), len(set(buttons)))

    def test_hidden_buttons_are_in_no_category(self):
        buttons = self._all_category_buttons()
        for btn in (common.BTN_APARTMENTS, common.BTN_SECTIONAL, common.BTN_AF_RESULTS,
                    common.BTN_VALUER_TASKS, common.BTN_ASSIGNMENTS, common.BTN_DLV_QUEUE):
            self.assertNotIn(btn, buttons)

    def test_every_other_button_is_in_exactly_one_category(self):
        buttons = self._all_category_buttons()
        for btn in (common.BTN_ASSIGN, common.BTN_FETCH_TASKS, common.BTN_AUTO_FETCH,
                    common.BTN_DLV_BATCH, common.BTN_BULK_EXPORT, common.BTN_EXPORT_STATUS,
                    common.BTN_JOB_DIST, common.BTN_DLV_TASKS, common.BTN_BRIEFING,
                    common.BTN_HOLD_TASKS, common.BTN_INCREMENTAL, common.BTN_DLV_REPORT_SCHEDULE,
                    common.BTN_CUSTOM_EXCLUSIONS, common.BTN_LOOKUP, common.BTN_PARCEL_LOOKUP,
                    common.BTN_PARCEL_WATCH, common.BTN_DLV_REF_CHECK, common.BTN_AUTH,
                    common.BTN_TOKEN_STATUS, common.BTN_ERROR_REPORT, common.BTN_VALUERS,
                    common.BTN_DELETE, common.BTN_DAEMON, common.BTN_RESTART, common.BTN_HELP,
                    common.BTN_TASK_ANALYTICS):
            self.assertEqual(buttons.count(btn), 1, f"{btn} should appear in exactly one category")


class TestCategoryMenu(unittest.TestCase):
    """_category_menu — lays out a category's buttons two per row, then a
    final row of Back + Cancel."""

    def test_back_and_cancel_are_the_last_row(self):
        kb = common._category_menu([common.BTN_ASSIGN, common.BTN_DLV_BATCH])
        last_row = kb.keyboard[-1]
        self.assertEqual([b.text for b in last_row], [common.BTN_BACK, common.BTN_CANCEL])

    def test_buttons_paired_two_per_row(self):
        kb = common._category_menu([common.BTN_ASSIGN, common.BTN_DLV_BATCH, common.BTN_DLV_TASKS])
        rows = kb.keyboard[:-1]   # exclude the Back/Cancel row
        self.assertEqual([b.text for b in rows[0]], [common.BTN_ASSIGN, common.BTN_DLV_BATCH])
        self.assertEqual([b.text for b in rows[1]], [common.BTN_DLV_TASKS])

    def test_all_buttons_present(self):
        buttons = [common.BTN_LOOKUP, common.BTN_PARCEL_LOOKUP, common.BTN_PARCEL_WATCH, common.BTN_DLV_REF_CHECK]
        kb = common._category_menu(buttons)
        texts = [b.text for row in kb.keyboard for b in row]
        for btn in buttons:
            self.assertIn(btn, texts)


class TestRecvMenuCategory(unittest.TestCase):
    def test_known_category_shows_description_and_submenu(self):
        update = MagicMock()
        update.message.text = common.BTN_CAT_LOOKUPS
        update.message.reply_text = AsyncMock()
        with patch.object(common, "allowed", return_value=True):
            asyncio.run(common.recv_menu_category(update, MagicMock()))
        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("Look Ups", args[0])
        self.assertIn("reply_markup", kwargs)

    def test_unknown_text_does_nothing(self):
        update = MagicMock()
        update.message.text = "not a category"
        update.message.reply_text = AsyncMock()
        with patch.object(common, "allowed", return_value=True):
            asyncio.run(common.recv_menu_category(update, MagicMock()))
        update.message.reply_text.assert_not_awaited()


class TestRecvMenuBack(unittest.TestCase):
    def test_returns_to_main_menu(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        with patch.object(common, "allowed", return_value=True):
            asyncio.run(common.recv_menu_back(update, MagicMock()))
        update.message.reply_text.assert_awaited_once()
        _, kwargs = update.message.reply_text.call_args
        self.assertIn("reply_markup", kwargs)


class TestParseListInput(unittest.TestCase):
    """_parse_list_input — the shared multi-item paste parser used by
    Lookup Reference, Parcel Lookup, Parcel Watch, and DLV Ref Check to
    accept a list of refs/parcels in one conversation turn."""

    def test_single_plain_value_returns_one_item_list(self):
        self.assertEqual(common._parse_list_input("R1"), ["R1"])

    def test_newline_separated(self):
        self.assertEqual(common._parse_list_input("R1\nR2\nR3"), ["R1", "R2", "R3"])

    def test_comma_separated(self):
        self.assertEqual(common._parse_list_input("R1, R2, R3"), ["R1", "R2", "R3"])

    def test_mixed_newline_and_comma(self):
        self.assertEqual(common._parse_list_input("R1, R2\nR3"), ["R1", "R2", "R3"])

    def test_blank_lines_and_extra_commas_are_dropped(self):
        self.assertEqual(common._parse_list_input(" R1 ,, \n\n R2 "), ["R1", "R2"])

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(common._parse_list_input(""), [])

    def test_does_not_dedupe(self):
        self.assertEqual(common._parse_list_input("R1\nR1"), ["R1", "R1"])


if __name__ == "__main__":
    unittest.main()
