#!/usr/bin/env python3
"""
Unit tests for access_control.py — _ma_candidate_users' admin exclusion,
the keyboards' toggle rendering, and the cmd_manage_access/recv_ma_user/
recv_ma_category conversation handlers, including the admin-only guard at
every step and the actual grant/revoke persistence on Done.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import access_control as ac
import common


def _run(coro):
    return asyncio.run(coro)


def _make_update_with_message(text="", user_id=1):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_user.id = user_id
    return update


def _make_update_with_callback(data, user_id=1):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    query.from_user.id = user_id
    return update


class TestMaCandidateUsers(unittest.TestCase):
    def test_excludes_admins_from_candidates(self):
        with patch.object(ac, "ALLOWED_IDS", {1, 2, 3}), \
             patch.object(ac, "ADMIN_IDS", {1}):
            self.assertEqual(ac._ma_candidate_users(), [2, 3])

    def test_empty_when_everyone_is_admin(self):
        with patch.object(ac, "ALLOWED_IDS", {1, 2}), \
             patch.object(ac, "ADMIN_IDS", {1, 2}):
            self.assertEqual(ac._ma_candidate_users(), [])


class TestMaDisplayLabel(unittest.TestCase):
    def test_known_name_shown_with_id(self):
        self.assertEqual(ac._ma_display_label(2, {"2": "Jane Doe"}), "Jane Doe (2)")

    def test_unknown_id_falls_back_to_bare_id(self):
        self.assertEqual(ac._ma_display_label(2, {}), "2")


class TestMaUserKeyboard(unittest.TestCase):
    def test_buttons_show_cached_names(self):
        with patch.object(ac, "_ma_candidate_users", return_value=[2, 3]), \
             patch.object(ac, "load_user_names", return_value={"2": "Jane Doe"}):
            kb = ac._ma_user_keyboard()
        texts = [row[0].text for row in kb.inline_keyboard if row[0].text != "❌ Cancel"]
        self.assertIn("Jane Doe (2)", texts)
        self.assertIn("3", texts)


class TestMaCategoryKeyboard(unittest.TestCase):
    def test_selected_categories_are_checked(self):
        kb = ac._ma_category_keyboard({common.BTN_CAT_LOOKUPS})
        rows_by_text = {row[0].text: row[0] for row in kb.inline_keyboard if row[0].text != "✅ Done"}
        checked = [t for t in rows_by_text if t.startswith("☑")]
        self.assertEqual(len(checked), 1)
        self.assertIn(common.BTN_CAT_LOOKUPS, checked[0])

    def test_unselected_categories_are_unchecked(self):
        kb = ac._ma_category_keyboard(set())
        texts = [row[0].text for row in kb.inline_keyboard if row[0].text != "✅ Done"]
        self.assertTrue(all(t.startswith("☐") for t in texts))

    def test_done_button_present(self):
        kb = ac._ma_category_keyboard(set())
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertIn("✅ Done", texts)

    def test_admin_only_categories_are_not_offered(self):
        # 🛠 System is admin-only and must never appear in the grantable list.
        kb = ac._ma_category_keyboard(set())
        texts = [row[0].text for row in kb.inline_keyboard if row[0].text != "✅ Done"]
        self.assertFalse(any(common.BTN_CAT_SETTINGS in t for t in texts))


class TestCmdManageAccess(unittest.TestCase):
    def test_non_admin_is_denied(self):
        update = _make_update_with_message(user_id=2)
        ctx = MagicMock()
        with patch.object(ac, "allowed", return_value=True), \
             patch.object(ac, "is_admin", return_value=False):
            result = _run(ac.cmd_manage_access(update, ctx))
        self.assertEqual(result, ac.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Admin access required", sent_text)

    def test_no_candidates_ends_conversation(self):
        update = _make_update_with_message(user_id=1)
        ctx = MagicMock()
        with patch.object(ac, "allowed", return_value=True), \
             patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "_ma_candidate_users", return_value=[]):
            result = _run(ac.cmd_manage_access(update, ctx))
        self.assertEqual(result, ac.ConversationHandler.END)
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("No non-admin users", sent_text)

    def test_admin_with_candidates_shows_user_picker(self):
        update = _make_update_with_message(user_id=1)
        ctx = MagicMock()
        with patch.object(ac, "allowed", return_value=True), \
             patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "_ma_candidate_users", return_value=[2, 3]):
            result = _run(ac.cmd_manage_access(update, ctx))
        self.assertEqual(result, ac.MA.PICK_USER)


class TestRecvMaUser(unittest.TestCase):
    def test_non_admin_ends_conversation(self):
        update = _make_update_with_callback("ma_user:2", user_id=2)
        ctx = MagicMock()
        with patch.object(ac, "is_admin", return_value=False):
            result = _run(ac.recv_ma_user(update, ctx))
        self.assertEqual(result, ac.ConversationHandler.END)

    def test_cancel_ends_conversation(self):
        update = _make_update_with_callback("ma_user:cancel", user_id=1)
        ctx = MagicMock()
        with patch.object(ac, "is_admin", return_value=True):
            result = _run(ac.recv_ma_user(update, ctx))
        self.assertEqual(result, ac.ConversationHandler.END)

    def test_picking_a_user_preloads_their_existing_grants(self):
        update = _make_update_with_callback("ma_user:2", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {}
        existing = {"2": [common.BTN_CAT_LOOKUPS]}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_category_access", return_value=existing), \
             patch.object(ac, "load_user_names", return_value={}):
            result = _run(ac.recv_ma_user(update, ctx))
        self.assertEqual(result, ac.MA.PICK_CATEGORIES)
        sess = ac._get_ma_sess(ctx)
        self.assertEqual(sess.target_user_id, 2)
        self.assertEqual(sess.selected, {common.BTN_CAT_LOOKUPS})

    def test_confirmation_shows_cached_name(self):
        update = _make_update_with_callback("ma_user:2", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_category_access", return_value={}), \
             patch.object(ac, "load_user_names", return_value={"2": "Jane Doe"}):
            _run(ac.recv_ma_user(update, ctx))
        sent_text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Jane Doe", sent_text)


class TestRecvMaCategory(unittest.TestCase):
    def test_non_admin_ends_conversation(self):
        update = _make_update_with_callback(f"ma_cat:{common.BTN_CAT_LOOKUPS}", user_id=2)
        ctx = MagicMock()
        with patch.object(ac, "is_admin", return_value=False):
            result = _run(ac.recv_ma_category(update, ctx))
        self.assertEqual(result, ac.ConversationHandler.END)

    def test_toggle_adds_category(self):
        update = _make_update_with_callback(f"ma_cat:{common.BTN_CAT_LOOKUPS}", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected=set())}
        with patch.object(ac, "is_admin", return_value=True):
            result = _run(ac.recv_ma_category(update, ctx))
        self.assertEqual(result, ac.MA.PICK_CATEGORIES)
        self.assertEqual(ac._get_ma_sess(ctx).selected, {common.BTN_CAT_LOOKUPS})

    def test_toggle_removes_already_selected_category(self):
        update = _make_update_with_callback(f"ma_cat:{common.BTN_CAT_LOOKUPS}", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected={common.BTN_CAT_LOOKUPS})}
        with patch.object(ac, "is_admin", return_value=True):
            _run(ac.recv_ma_category(update, ctx))
        self.assertEqual(ac._get_ma_sess(ctx).selected, set())

    def test_done_persists_categories_and_opens_customize(self):
        # Done with a non-empty selection now saves the categories and moves
        # to the per-button customization step (not straight to END).
        update = _make_update_with_callback("ma_cat:done", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected={common.BTN_CAT_LOOKUPS})}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_category_access", return_value={}), \
             patch.object(ac, "load_user_names", return_value={}), \
             patch.object(ac, "load_button_access", return_value={}), \
             patch.object(ac, "save_button_access"), \
             patch.object(ac, "save_category_access") as mock_save:
            result = _run(ac.recv_ma_category(update, ctx))
        self.assertEqual(result, ac.MA.PICK_CUST_CAT)
        mock_save.assert_called_once_with({"2": [common.BTN_CAT_LOOKUPS]})

    def test_done_with_empty_selection_revokes_all_and_ends(self):
        update = _make_update_with_callback("ma_cat:done", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected=set())}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_category_access", return_value={"2": [common.BTN_CAT_LOOKUPS]}), \
             patch.object(ac, "load_user_names", return_value={}), \
             patch.object(ac, "load_button_access", return_value={}), \
             patch.object(ac, "save_button_access"), \
             patch.object(ac, "save_category_access") as mock_save:
            result = _run(ac.recv_ma_category(update, ctx))
        self.assertEqual(result, ac.ConversationHandler.END)
        mock_save.assert_called_once_with({"2": []})

    def test_done_prunes_button_access_for_dropped_categories(self):
        # A category the user no longer has must have its button allow-list
        # dropped — a button restriction only means anything inside a granted
        # category.
        update = _make_update_with_callback("ma_cat:done", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected={common.BTN_CAT_LOOKUPS})}
        existing_ba = {"2": {common.BTN_CAT_LOOKUPS: ["x"], common.BTN_CAT_ANALYTICS: ["y"]}}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_category_access", return_value={}), \
             patch.object(ac, "load_user_names", return_value={}), \
             patch.object(ac, "load_button_access", return_value=existing_ba), \
             patch.object(ac, "save_button_access") as mock_save_ba, \
             patch.object(ac, "save_category_access"):
            _run(ac.recv_ma_category(update, ctx))
        # Analytics restriction pruned; Lookups kept.
        mock_save_ba.assert_called_once_with({"2": {common.BTN_CAT_LOOKUPS: ["x"]}})

    def test_done_confirmation_shows_cached_name(self):
        update = _make_update_with_callback("ma_cat:done", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected={common.BTN_CAT_LOOKUPS})}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_category_access", return_value={}), \
             patch.object(ac, "load_user_names", return_value={"2": "Jane Doe"}), \
             patch.object(ac, "load_button_access", return_value={}), \
             patch.object(ac, "save_button_access"), \
             patch.object(ac, "save_category_access"):
            _run(ac.recv_ma_category(update, ctx))
        sent_text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Jane Doe", sent_text)


class TestMaButtonKeyboard(unittest.TestCase):
    """_ma_button_keyboard — ☑/☐ per button (by index) + Done."""

    def test_checked_buttons_reflect_selection(self):
        cat = common.BTN_CAT_ASSIGNMENTS
        buttons = common._MENU_CATEGORIES[cat]["buttons"]
        selected = {buttons[0]}
        kb = ac._ma_button_keyboard(cat, selected)
        rows = {row[0].text: row[0] for row in kb.inline_keyboard if row[0].text != "✅ Done"}
        checked = [t for t in rows if t.startswith("☑")]
        self.assertEqual(len(checked), 1)
        self.assertIn(buttons[0], checked[0])

    def test_callback_data_is_index_based(self):
        cat = common.BTN_CAT_ASSIGNMENTS
        kb = ac._ma_button_keyboard(cat, set())
        datas = [row[0].callback_data for row in kb.inline_keyboard]
        self.assertIn("ma_btn:0", datas)
        self.assertIn("ma_btn:done", datas)


class TestMaCustomizeKeyboard(unittest.TestCase):
    """_ma_customize_keyboard — one button per granted category + Finish,
    with a 🔧 marker on button-restricted categories."""

    def test_only_granted_categories_shown(self):
        kb = ac._ma_customize_keyboard({common.BTN_CAT_LOOKUPS}, {})
        texts = [row[0].text for row in kb.inline_keyboard if row[0].callback_data != "ma_bcat:done"]
        self.assertEqual(len(texts), 1)
        self.assertIn(common.BTN_CAT_LOOKUPS, texts[0])

    def test_restricted_category_gets_wrench_marker(self):
        kb = ac._ma_customize_keyboard({common.BTN_CAT_LOOKUPS}, {common.BTN_CAT_LOOKUPS: ["x"]})
        text = [row[0].text for row in kb.inline_keyboard if row[0].callback_data != "ma_bcat:done"][0]
        self.assertTrue(text.startswith("🔧"))

    def test_finish_button_present(self):
        kb = ac._ma_customize_keyboard(set(), {})
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("ma_bcat:done", datas)


class TestRecvMaCustomizePick(unittest.TestCase):
    def test_finish_ends_with_summary(self):
        update = _make_update_with_callback("ma_bcat:done", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected={common.BTN_CAT_LOOKUPS})}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_category_access", return_value={"2": [common.BTN_CAT_LOOKUPS]}), \
             patch.object(ac, "load_button_access", return_value={}), \
             patch.object(ac, "load_user_names", return_value={}):
            result = _run(ac.recv_ma_customize_pick(update, ctx))
        self.assertEqual(result, ac.ConversationHandler.END)

    def test_picking_category_opens_button_toggles_all_checked_by_default(self):
        cat = common.BTN_CAT_ASSIGNMENTS
        update = _make_update_with_callback(f"ma_bcat:{cat}", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected={cat})}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_button_access", return_value={}):
            result = _run(ac.recv_ma_customize_pick(update, ctx))
        self.assertEqual(result, ac.MA.PICK_BUTTONS)
        # No saved restriction → every button pre-selected.
        self.assertEqual(ac._get_ma_sess(ctx).cust_selected, set(common._MENU_CATEGORIES[cat]["buttons"]))


class TestRecvMaButton(unittest.TestCase):
    def _sess(self, cat, selected):
        return ac.MASession(target_user_id=2, selected={cat}, cust_category=cat, cust_selected=set(selected))

    def test_toggle_removes_a_button(self):
        cat = common.BTN_CAT_ASSIGNMENTS
        buttons = common._MENU_CATEGORIES[cat]["buttons"]
        update = _make_update_with_callback("ma_btn:0", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": self._sess(cat, buttons)}
        with patch.object(ac, "is_admin", return_value=True):
            result = _run(ac.recv_ma_button(update, ctx))
        self.assertEqual(result, ac.MA.PICK_BUTTONS)
        self.assertNotIn(buttons[0], ac._get_ma_sess(ctx).cust_selected)

    def test_done_stores_partial_allow_list(self):
        cat = common.BTN_CAT_ASSIGNMENTS
        buttons = common._MENU_CATEGORIES[cat]["buttons"]
        partial = buttons[:1]
        update = _make_update_with_callback("ma_btn:done", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": self._sess(cat, partial)}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_button_access", return_value={}), \
             patch.object(ac, "load_user_names", return_value={}), \
             patch.object(ac, "save_button_access") as mock_save:
            result = _run(ac.recv_ma_button(update, ctx))
        self.assertEqual(result, ac.MA.PICK_CUST_CAT)
        mock_save.assert_called_once_with({"2": {cat: sorted(partial)}})

    def test_done_with_all_buttons_drops_restriction(self):
        cat = common.BTN_CAT_ASSIGNMENTS
        buttons = common._MENU_CATEGORIES[cat]["buttons"]
        update = _make_update_with_callback("ma_btn:done", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": self._sess(cat, buttons)}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_button_access", return_value={"2": {cat: buttons[:1]}}), \
             patch.object(ac, "load_user_names", return_value={}), \
             patch.object(ac, "save_button_access") as mock_save:
            _run(ac.recv_ma_button(update, ctx))
        # All checked == no restriction → the whole user entry is dropped.
        mock_save.assert_called_once_with({})


if __name__ == "__main__":
    unittest.main()
