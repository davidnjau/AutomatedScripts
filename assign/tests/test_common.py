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


class TestCategoryAccessPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.tmpdir.name, "saved_category_access.json")
        self._patch = patch.object(common, "SAVED_CATEGORY_ACCESS_FILE", self.data_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty_dict(self):
        self.assertEqual(common.load_category_access(), {})

    def test_save_then_load_roundtrip(self):
        common.save_category_access({"111": [common.BTN_CAT_LOOKUPS]})
        self.assertEqual(common.load_category_access(), {"111": [common.BTN_CAT_LOOKUPS]})


class TestUserNamesPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.tmpdir.name, "saved_user_names.json")
        self._patch = patch.object(common, "SAVED_USER_NAMES_FILE", self.data_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_load_missing_file_returns_empty_dict(self):
        self.assertEqual(common.load_user_names(), {})

    def test_save_then_load_roundtrip(self):
        common.save_user_names({"111": "Jane Doe"})
        self.assertEqual(common.load_user_names(), {"111": "Jane Doe"})


class TestRecordUserName(unittest.TestCase):
    """_record_user_name — first+last name, falling back to @username,
    caching only on change so the common case (name already up to date)
    is a cheap dict lookup with no disk write."""

    def _make_user(self, first="", last="", username=""):
        user = MagicMock()
        user.id = 111
        user.first_name = first
        user.last_name = last
        user.username = username
        update = MagicMock()
        update.effective_user = user
        return update

    def test_no_effective_user_is_a_noop(self):
        update = MagicMock()
        update.effective_user = None
        with patch.object(common, "load_user_names") as mock_load:
            common._record_user_name(update)
        mock_load.assert_not_called()

    def test_first_and_last_name_combined(self):
        update = self._make_user(first="Jane", last="Doe")
        with patch.object(common, "load_user_names", return_value={}), \
             patch.object(common, "save_user_names") as mock_save:
            common._record_user_name(update)
        mock_save.assert_called_once_with({"111": "Jane Doe"})

    def test_falls_back_to_username_when_no_name_set(self):
        update = self._make_user(username="janedoe")
        with patch.object(common, "load_user_names", return_value={}), \
             patch.object(common, "save_user_names") as mock_save:
            common._record_user_name(update)
        mock_save.assert_called_once_with({"111": "@janedoe"})

    def test_no_name_and_no_username_is_a_noop(self):
        update = self._make_user()
        with patch.object(common, "load_user_names") as mock_load, \
             patch.object(common, "save_user_names") as mock_save:
            common._record_user_name(update)
        mock_load.assert_not_called()
        mock_save.assert_not_called()

    def test_unchanged_name_does_not_write(self):
        update = self._make_user(first="Jane", last="Doe")
        with patch.object(common, "load_user_names", return_value={"111": "Jane Doe"}), \
             patch.object(common, "save_user_names") as mock_save:
            common._record_user_name(update)
        mock_save.assert_not_called()

    def test_changed_name_overwrites(self):
        update = self._make_user(first="Jane", last="Smith")
        with patch.object(common, "load_user_names", return_value={"111": "Jane Doe"}), \
             patch.object(common, "save_user_names") as mock_save:
            common._record_user_name(update)
        mock_save.assert_called_once_with({"111": "Jane Smith"})


class TestAllowedRecordsName(unittest.TestCase):
    """allowed() calls _record_user_name for every user it actually lets
    through — but not for a user it's about to deny."""

    def test_open_bot_records_name(self):
        update = MagicMock()
        update.effective_user.id = 111
        with patch.object(common, "ALLOWED_IDS", set()), \
             patch.object(common, "_record_user_name") as mock_record:
            result = common.allowed(update)
        self.assertTrue(result)
        mock_record.assert_called_once_with(update)

    def test_allowed_user_records_name(self):
        update = MagicMock()
        update.effective_user.id = 111
        with patch.object(common, "ALLOWED_IDS", {111}), \
             patch.object(common, "_record_user_name") as mock_record:
            result = common.allowed(update)
        self.assertTrue(result)
        mock_record.assert_called_once_with(update)

    def test_disallowed_user_does_not_record_name(self):
        update = MagicMock()
        update.effective_user.id = 999
        with patch.object(common, "ALLOWED_IDS", {111}), \
             patch.object(common, "_record_user_name") as mock_record:
            result = common.allowed(update)
        self.assertFalse(result)
        mock_record.assert_not_called()


class TestIsAdmin(unittest.TestCase):
    def test_id_in_admin_ids_is_admin(self):
        with patch.object(common, "ADMIN_IDS", {111}):
            self.assertTrue(common.is_admin(111))

    def test_id_not_in_admin_ids_is_not_admin(self):
        with patch.object(common, "ADMIN_IDS", {111}):
            self.assertFalse(common.is_admin(222))


class TestGetUserCategories(unittest.TestCase):
    """get_user_categories — admins always get every category; a
    non-admin gets exactly what's been granted (empty by default); and
    the no-admin-configured safety fallback opens everything to everyone
    so a restriction can never be permanently un-liftable."""

    def test_admin_gets_every_category_regardless_of_grants(self):
        with patch.object(common, "ADMIN_IDS", {111}), \
             patch.object(common, "load_category_access", return_value={"111": []}):
            self.assertEqual(common.get_user_categories(111), list(common._MENU_CATEGORIES))

    def test_non_admin_with_no_grant_gets_nothing(self):
        with patch.object(common, "ADMIN_IDS", {999}), \
             patch.object(common, "load_category_access", return_value={}):
            self.assertEqual(common.get_user_categories(111), [])

    def test_non_admin_gets_exactly_what_was_granted(self):
        granted = [common.BTN_CAT_LOOKUPS, common.BTN_CAT_VALUERS]
        with patch.object(common, "ADMIN_IDS", {999}), \
             patch.object(common, "load_category_access", return_value={"111": granted}):
            self.assertEqual(common.get_user_categories(111), granted)

    def test_no_admin_configured_opens_everything_to_everyone(self):
        """Safety fallback: if ADMIN_TELEGRAM_IDS is empty, nobody could
        ever grant access to a restricted user, so the restriction must
        not apply at all in that case."""
        with patch.object(common, "ADMIN_IDS", set()), \
             patch.object(common, "load_category_access", return_value={}):
            self.assertEqual(common.get_user_categories(111), list(common._MENU_CATEGORIES))


class TestCategoryAllowed(unittest.TestCase):
    def test_true_when_category_in_users_list(self):
        with patch.object(common, "get_user_categories", return_value=[common.BTN_CAT_LOOKUPS]):
            self.assertTrue(common.category_allowed(111, common.BTN_CAT_LOOKUPS))

    def test_false_when_category_not_in_users_list(self):
        with patch.object(common, "get_user_categories", return_value=[common.BTN_CAT_LOOKUPS]):
            self.assertFalse(common.category_allowed(111, common.BTN_CAT_VALUERS))


class TestMainMenuFor(unittest.TestCase):
    def test_shows_only_granted_categories_plus_cancel(self):
        with patch.object(common, "get_user_categories", return_value=[common.BTN_CAT_LOOKUPS]):
            kb = common._main_menu_for(111)
        texts = [b.text for row in kb.keyboard for b in row]
        self.assertEqual(set(texts), {common.BTN_CAT_LOOKUPS, common.BTN_CANCEL})

    def test_zero_categories_still_returns_a_valid_keyboard_with_cancel(self):
        with patch.object(common, "get_user_categories", return_value=[]):
            kb = common._main_menu_for(111)
        texts = [b.text for row in kb.keyboard for b in row]
        self.assertEqual(texts, [common.BTN_CANCEL])


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
    def _make_update(self, text, user_id=111):
        update = MagicMock()
        update.message.text = text
        update.message.reply_text = AsyncMock()
        update.effective_user.id = user_id
        return update

    def test_known_category_shows_description_and_submenu(self):
        update = self._make_update(common.BTN_CAT_LOOKUPS)
        with patch.object(common, "allowed", return_value=True), \
             patch.object(common, "category_allowed", return_value=True):
            asyncio.run(common.recv_menu_category(update, MagicMock()))
        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("Look Ups", args[0])
        self.assertIn("reply_markup", kwargs)

    def test_unknown_text_does_nothing(self):
        update = self._make_update("not a category")
        with patch.object(common, "allowed", return_value=True):
            asyncio.run(common.recv_menu_category(update, MagicMock()))
        update.message.reply_text.assert_not_awaited()

    def test_disallowed_category_is_denied_not_opened(self):
        update = self._make_update(common.BTN_CAT_LOOKUPS)
        with patch.object(common, "allowed", return_value=True), \
             patch.object(common, "category_allowed", return_value=False):
            asyncio.run(common.recv_menu_category(update, MagicMock()))
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("don't have access", sent_text)

    def test_admin_gets_manage_access_appended_to_bot_settings(self):
        update = self._make_update(common.BTN_CAT_SETTINGS)
        with patch.object(common, "allowed", return_value=True), \
             patch.object(common, "category_allowed", return_value=True), \
             patch.object(common, "is_admin", return_value=True):
            asyncio.run(common.recv_menu_category(update, MagicMock()))
        _, kwargs = update.message.reply_text.call_args
        texts = [b.text for row in kwargs["reply_markup"].keyboard for b in row]
        self.assertIn(common.BTN_MANAGE_ACCESS, texts)

    def test_non_admin_does_not_get_manage_access(self):
        update = self._make_update(common.BTN_CAT_SETTINGS)
        with patch.object(common, "allowed", return_value=True), \
             patch.object(common, "category_allowed", return_value=True), \
             patch.object(common, "is_admin", return_value=False):
            asyncio.run(common.recv_menu_category(update, MagicMock()))
        _, kwargs = update.message.reply_text.call_args
        texts = [b.text for row in kwargs["reply_markup"].keyboard for b in row]
        self.assertNotIn(common.BTN_MANAGE_ACCESS, texts)


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
