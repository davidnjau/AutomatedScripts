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
             patch.object(ac, "load_category_access", return_value=existing):
            result = _run(ac.recv_ma_user(update, ctx))
        self.assertEqual(result, ac.MA.PICK_CATEGORIES)
        sess = ac._get_ma_sess(ctx)
        self.assertEqual(sess.target_user_id, 2)
        self.assertEqual(sess.selected, {common.BTN_CAT_LOOKUPS})


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

    def test_done_persists_selection_and_ends(self):
        update = _make_update_with_callback("ma_cat:done", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected={common.BTN_CAT_LOOKUPS})}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_category_access", return_value={}), \
             patch.object(ac, "save_category_access") as mock_save:
            result = _run(ac.recv_ma_category(update, ctx))
        self.assertEqual(result, ac.ConversationHandler.END)
        mock_save.assert_called_once_with({"2": [common.BTN_CAT_LOOKUPS]})

    def test_done_with_empty_selection_revokes_all(self):
        update = _make_update_with_callback("ma_cat:done", user_id=1)
        ctx = MagicMock()
        ctx.user_data = {"ma_session": ac.MASession(target_user_id=2, selected=set())}
        with patch.object(ac, "is_admin", return_value=True), \
             patch.object(ac, "load_category_access", return_value={"2": [common.BTN_CAT_LOOKUPS]}), \
             patch.object(ac, "save_category_access") as mock_save:
            _run(ac.recv_ma_category(update, ctx))
        mock_save.assert_called_once_with({"2": []})


if __name__ == "__main__":
    unittest.main()
