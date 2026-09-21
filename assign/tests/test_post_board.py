#!/usr/bin/env python3
"""
Unit tests for post_board.py — the saved-queue persistence layer, the
Post/View/Close menu, the posting session (text refs split via
_parse_list_input, one item per ref; photos, one item per photo), and the
standalone recv_pb_done handler (including the already-cleared race and
the no-parse_mode edit that avoids re-parsing Telegram's own
formatting-stripped message text as Markdown).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import post_board as pb


def _run(coro):
    return asyncio.run(coro)


def _make_update_with_message(text="", user_id=1, photo=None, caption=None):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.photo = photo
    update.message.caption = caption
    update.effective_user.id = user_id
    update.effective_user.first_name = "Jane"
    update.effective_user.last_name = "Doe"
    return update


def _make_update_with_callback(data, user_id=1, is_photo=False, text="", caption=""):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    query.message.chat_id = 555
    query.message.photo = [MagicMock()] if is_photo else None
    query.message.text = text
    query.message.caption = caption
    query.from_user.id = user_id
    return update


class TestPostBoardPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.tmpdir.name, "saved_post_board.json")
        self._patch = patch.object(pb, "SAVED_POST_BOARD_FILE", self.data_file)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(pb.load_post_board(), [])

    def test_add_then_load_roundtrip(self):
        item_id = pb.add_post_item({"type": "text", "text": "R1"})
        items = pb.load_post_board()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "R1")
        self.assertEqual(items[0]["id"], item_id)

    def test_add_multiple_items_get_distinct_ids(self):
        id1 = pb.add_post_item({"type": "text", "text": "R1"})
        id2 = pb.add_post_item({"type": "text", "text": "R2"})
        self.assertNotEqual(id1, id2)
        self.assertEqual(len(pb.load_post_board()), 2)

    def test_get_post_item_finds_by_id(self):
        item_id = pb.add_post_item({"type": "text", "text": "R1"})
        item = pb.get_post_item(item_id)
        self.assertEqual(item["text"], "R1")

    def test_get_post_item_returns_none_when_missing(self):
        self.assertIsNone(pb.get_post_item("no-such-id"))

    def test_remove_post_item_deletes_only_that_one(self):
        id1 = pb.add_post_item({"type": "text", "text": "R1"})
        id2 = pb.add_post_item({"type": "text", "text": "R2"})
        removed = pb.remove_post_item(id1)
        self.assertTrue(removed)
        remaining = pb.load_post_board()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], id2)

    def test_remove_missing_item_returns_false(self):
        self.assertFalse(pb.remove_post_item("no-such-id"))


class TestPbPosterLabel(unittest.TestCase):
    def test_cached_name_used_when_present(self):
        self.assertEqual(pb._pb_poster_label({"posted_by_name": "Jane Doe", "posted_by": 111}), "Jane Doe")

    def test_falls_back_to_raw_id(self):
        self.assertEqual(pb._pb_poster_label({"posted_by_name": "", "posted_by": 111}), "111")


class TestCmdPostBoard(unittest.TestCase):
    def test_shows_pending_count_and_menu(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "load_post_board", return_value=[{"id": "1"}, {"id": "2"}]):
            result = _run(pb.cmd_post_board(update, ctx))
        self.assertEqual(result, pb.PB.MENU)
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("2", sent_text)


class TestRecvPbMenu(unittest.TestCase):
    def test_close_ends_conversation(self):
        update = _make_update_with_callback("pb_menu:close")
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True):
            result = _run(pb.recv_pb_menu(update, ctx))
        self.assertEqual(result, pb.ConversationHandler.END)

    def test_view_sends_queue_and_ends(self):
        update = _make_update_with_callback("pb_menu:view")
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "_pb_send_queue", new_callable=AsyncMock) as mock_send:
            result = _run(pb.recv_pb_menu(update, ctx))
        self.assertEqual(result, pb.ConversationHandler.END)
        mock_send.assert_awaited_once()

    def test_post_resets_session_count_and_starts_posting(self):
        update = _make_update_with_callback("pb_menu:post")
        ctx = MagicMock()
        ctx.user_data = {"pb_session": pb.PBSession(posted_count=5)}
        with patch.object(pb, "allowed", return_value=True):
            result = _run(pb.recv_pb_menu(update, ctx))
        self.assertEqual(result, pb.PB.POSTING)
        self.assertEqual(pb._get_pb_sess(ctx).posted_count, 0)


class TestPbSendQueue(unittest.TestCase):
    def test_empty_queue_reports_no_pending_items(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with patch.object(pb, "load_post_board", return_value=[]):
            _run(pb._pb_send_queue(555, ctx))
        sent_text = ctx.bot.send_message.call_args[0][1]
        self.assertIn("No pending items", sent_text)

    def test_text_item_sent_with_done_button(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        items = [{"id": "abc", "type": "text", "text": "R1", "posted_by": 1, "posted_at": "t"}]
        with patch.object(pb, "load_post_board", return_value=items):
            _run(pb._pb_send_queue(555, ctx))
        calls = ctx.bot.send_message.call_args_list
        item_call = calls[0]
        self.assertIn("R1", item_call.args[1])
        self.assertIn("reply_markup", item_call.kwargs)

    def test_photo_item_sent_via_send_photo(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()
        items = [{"id": "abc", "type": "photo", "photo_file_id": "file123",
                  "caption": "note", "posted_by": 1, "posted_at": "t"}]
        with patch.object(pb, "load_post_board", return_value=items):
            _run(pb._pb_send_queue(555, ctx))
        ctx.bot.send_photo.assert_awaited_once()
        _, kwargs = ctx.bot.send_photo.call_args
        self.assertEqual(kwargs["photo"], "file123")
        self.assertIn("note", kwargs["caption"])


class TestRecvPbText(unittest.TestCase):
    def test_blank_text_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True):
            result = _run(pb.recv_pb_text(update, ctx))
        self.assertEqual(result, pb.PB.POSTING)

    def test_multiple_refs_each_become_their_own_item(self):
        update = _make_update_with_message("R1\nR2")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "load_user_names", return_value={}), \
             patch.object(pb, "add_post_item") as mock_add:
            result = _run(pb.recv_pb_text(update, ctx))
        self.assertEqual(result, pb.PB.POSTING)
        self.assertEqual(mock_add.call_count, 2)
        texts = [c.args[0]["text"] for c in mock_add.call_args_list]
        self.assertEqual(texts, ["R1", "R2"])
        self.assertEqual(pb._get_pb_sess(ctx).posted_count, 2)


class TestRecvPbPhoto(unittest.TestCase):
    def test_photo_added_with_caption(self):
        photo_sizes = [MagicMock(file_id="small"), MagicMock(file_id="large")]
        update = _make_update_with_message(photo=photo_sizes, caption="a note")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "load_user_names", return_value={}), \
             patch.object(pb, "add_post_item") as mock_add:
            result = _run(pb.recv_pb_photo(update, ctx))
        self.assertEqual(result, pb.PB.POSTING)
        mock_add.assert_called_once()
        added = mock_add.call_args.args[0]
        self.assertEqual(added["photo_file_id"], "large")
        self.assertEqual(added["caption"], "a note")
        self.assertEqual(pb._get_pb_sess(ctx).posted_count, 1)


class TestRecvPbDonePosting(unittest.TestCase):
    def test_reports_count_and_ends(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {"pb_session": pb.PBSession(posted_count=3)}
        with patch.object(pb, "allowed", return_value=True):
            result = _run(pb.recv_pb_done_posting(update, ctx))
        self.assertEqual(result, pb.ConversationHandler.END)
        sent_texts = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertTrue(any("3" in t for t in sent_texts))


class TestRecvPbDone(unittest.TestCase):
    def test_already_cleared_item_just_clears_markup(self):
        update = _make_update_with_callback("pb_done:missing")
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "remove_post_item", return_value=False):
            _run(pb.recv_pb_done(update, ctx))
        update.callback_query.edit_message_reply_markup.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_awaited()

    def test_text_item_done_edits_text_without_parse_mode(self):
        update = _make_update_with_callback("pb_done:abc", text="🔖 R1\nPosted by Jane Doe")
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "remove_post_item", return_value=True), \
             patch.object(pb, "load_user_names", return_value={}):
            _run(pb.recv_pb_done(update, ctx))
        update.callback_query.edit_message_text.assert_awaited_once()
        args, kwargs = update.callback_query.edit_message_text.call_args
        self.assertIn("Done", args[0])
        self.assertNotIn("parse_mode", kwargs)

    def test_photo_item_done_edits_caption_without_parse_mode(self):
        update = _make_update_with_callback("pb_done:abc", is_photo=True, caption="📸 note")
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "remove_post_item", return_value=True), \
             patch.object(pb, "load_user_names", return_value={}):
            _run(pb.recv_pb_done(update, ctx))
        update.callback_query.edit_message_caption.assert_awaited_once()
        _, kwargs = update.callback_query.edit_message_caption.call_args
        self.assertIn("Done", kwargs["caption"])
        self.assertNotIn("parse_mode", kwargs)


if __name__ == "__main__":
    unittest.main()
