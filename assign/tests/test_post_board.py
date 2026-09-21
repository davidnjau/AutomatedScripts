#!/usr/bin/env python3
"""
Unit tests for post_board.py — the saved-queue persistence layer
(including the accumulating "broadcasts" delivery-tracking list), the
render helpers (built fresh from stored fields every time, never from a
previously-sent Telegram message's retrieved text), the posting session
(text refs split via _parse_list_input, one item per ref; photos, one
item per photo; each broadcast to every other authorized user
immediately), View Queue's catch-up delivery, and the standalone
recv_pb_done handler clearing every delivered copy of an item, not just
the one tapped.

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
    update.effective_chat.id = user_id
    return update


def _make_update_with_callback(data, user_id=1):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
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

    def test_add_returns_full_item_with_id_and_empty_broadcasts(self):
        item = pb.add_post_item({"type": "text", "text": "R1"})
        self.assertEqual(item["text"], "R1")
        self.assertEqual(item["broadcasts"], [])
        self.assertIn("id", item)

    def test_add_then_load_roundtrip(self):
        item = pb.add_post_item({"type": "text", "text": "R1"})
        items = pb.load_post_board()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], item["id"])

    def test_add_multiple_items_get_distinct_ids(self):
        item1 = pb.add_post_item({"type": "text", "text": "R1"})
        item2 = pb.add_post_item({"type": "text", "text": "R2"})
        self.assertNotEqual(item1["id"], item2["id"])
        self.assertEqual(len(pb.load_post_board()), 2)

    def test_get_post_item_finds_by_id(self):
        item = pb.add_post_item({"type": "text", "text": "R1"})
        found = pb.get_post_item(item["id"])
        self.assertEqual(found["text"], "R1")

    def test_get_post_item_returns_none_when_missing(self):
        self.assertIsNone(pb.get_post_item("no-such-id"))

    def test_append_broadcast_accumulates(self):
        item = pb.add_post_item({"type": "text", "text": "R1"})
        pb.append_post_item_broadcast(item["id"], 111, 5001)
        pb.append_post_item_broadcast(item["id"], 222, 5002)
        stored = pb.get_post_item(item["id"])
        self.assertEqual(stored["broadcasts"], [
            {"chat_id": 111, "message_id": 5001},
            {"chat_id": 222, "message_id": 5002},
        ])

    def test_append_broadcast_on_missing_item_is_a_noop(self):
        pb.append_post_item_broadcast("no-such-id", 111, 5001)   # must not raise
        self.assertEqual(pb.load_post_board(), [])

    def test_remove_post_item_deletes_only_that_one(self):
        item1 = pb.add_post_item({"type": "text", "text": "R1"})
        item2 = pb.add_post_item({"type": "text", "text": "R2"})
        removed = pb.remove_post_item(item1["id"])
        self.assertTrue(removed)
        remaining = pb.load_post_board()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], item2["id"])

    def test_remove_missing_item_returns_false(self):
        self.assertFalse(pb.remove_post_item("no-such-id"))


class TestPbPosterLabel(unittest.TestCase):
    def test_cached_name_used_when_present(self):
        self.assertEqual(pb._pb_poster_label({"posted_by_name": "Jane Doe", "posted_by": 111}), "Jane Doe")

    def test_falls_back_to_raw_id(self):
        self.assertEqual(pb._pb_poster_label({"posted_by_name": "", "posted_by": 111}), "111")


class TestPbRenderText(unittest.TestCase):
    def test_includes_ref_and_poster(self):
        item = {"text": "R1", "posted_by_name": "Jane Doe", "posted_at": "2026-01-01T10:00:00"}
        text = pb._pb_render_text(item)
        self.assertIn("R1", text)
        self.assertIn("Jane Doe", text)
        self.assertNotIn("Done", text)

    def test_cleared_by_appends_done_line(self):
        item = {"text": "R1", "posted_by_name": "Jane Doe", "posted_at": "t"}
        text = pb._pb_render_text(item, cleared_by="Byron")
        self.assertIn("Done", text)
        self.assertIn("Byron", text)

    def test_special_characters_are_escaped(self):
        item = {"text": "REG_TSFR_1", "posted_by_name": "Jane_Doe", "posted_at": "t"}
        text = pb._pb_render_text(item)
        self.assertIn("REG\\_TSFR\\_1", text)
        self.assertIn("Jane\\_Doe", text)


class TestPbRenderCaption(unittest.TestCase):
    def test_includes_caption_when_present(self):
        item = {"caption": "a note", "posted_by_name": "Jane Doe", "posted_at": "t"}
        caption = pb._pb_render_caption(item)
        self.assertIn("a note", caption)

    def test_no_caption_omits_caption_line(self):
        item = {"posted_by_name": "Jane Doe", "posted_at": "t"}
        caption = pb._pb_render_caption(item)
        self.assertIn("Jane Doe", caption)


class TestPbDeliverItem(unittest.TestCase):
    def test_text_item_sends_message_and_records_delivery(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        item = {"id": "abc", "type": "text", "text": "R1", "posted_by_name": "Jane", "posted_at": "t"}
        with patch.object(pb, "append_post_item_broadcast") as mock_append:
            _run(pb._pb_deliver_item(item, 555, ctx))
        ctx.bot.send_message.assert_awaited_once()
        mock_append.assert_called_once_with("abc", 555, 999)

    def test_photo_item_sends_photo_and_records_delivery(self):
        ctx = MagicMock()
        ctx.bot.send_photo = AsyncMock(return_value=MagicMock(message_id=998))
        item = {"id": "abc", "type": "photo", "photo_file_id": "file123",
                "posted_by_name": "Jane", "posted_at": "t"}
        with patch.object(pb, "append_post_item_broadcast") as mock_append:
            _run(pb._pb_deliver_item(item, 555, ctx))
        ctx.bot.send_photo.assert_awaited_once()
        _, kwargs = ctx.bot.send_photo.call_args
        self.assertEqual(kwargs["photo"], "file123")
        mock_append.assert_called_once_with("abc", 555, 998)


class TestPbBroadcastNewItem(unittest.TestCase):
    def test_excludes_poster_and_non_permitted_users(self):
        item = {"id": "abc", "type": "text"}
        ctx = MagicMock()
        with patch.object(pb, "ALLOWED_IDS", {1, 2, 3}), \
             patch.object(pb, "category_allowed", side_effect=lambda uid, cat: uid in (1, 2)), \
             patch.object(pb, "_pb_deliver_item", new_callable=AsyncMock) as mock_deliver:
            _run(pb._pb_broadcast_new_item(item, ctx, poster_id=1))
        delivered_to = [c.args[1] for c in mock_deliver.call_args_list]
        self.assertEqual(delivered_to, [2])   # 1 is poster, 3 lacks category access

    def test_delivery_failure_to_one_recipient_does_not_block_others(self):
        item = {"id": "abc", "type": "text"}
        ctx = MagicMock()
        with patch.object(pb, "ALLOWED_IDS", {2, 3}), \
             patch.object(pb, "category_allowed", return_value=True), \
             patch.object(pb, "_pb_deliver_item", new_callable=AsyncMock,
                           side_effect=[RuntimeError("blocked"), None]):
            _run(pb._pb_broadcast_new_item(item, ctx, poster_id=1))   # must not raise


class TestCmdPbPost(unittest.TestCase):
    def test_resets_session_count_and_starts_posting(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        ctx.user_data = {"pb_session": pb.PBSession(posted_count=5)}
        with patch.object(pb, "allowed", return_value=True):
            result = _run(pb.cmd_pb_post(update, ctx))
        self.assertEqual(result, pb.PB.POSTING)
        self.assertEqual(pb._get_pb_sess(ctx).posted_count, 0)


class TestRecvPbText(unittest.TestCase):
    def test_blank_text_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True):
            result = _run(pb.recv_pb_text(update, ctx))
        self.assertEqual(result, pb.PB.POSTING)

    def test_multiple_refs_each_become_their_own_item_and_broadcast(self):
        update = _make_update_with_message("R1\nR2")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "load_user_names", return_value={}), \
             patch.object(pb, "add_post_item", side_effect=lambda i: {**i, "id": "x"}), \
             patch.object(pb, "_pb_broadcast_new_item", new_callable=AsyncMock) as mock_broadcast:
            result = _run(pb.recv_pb_text(update, ctx))
        self.assertEqual(result, pb.PB.POSTING)
        self.assertEqual(mock_broadcast.call_count, 2)
        self.assertEqual(pb._get_pb_sess(ctx).posted_count, 2)


class TestRecvPbPhoto(unittest.TestCase):
    def test_photo_added_with_caption_and_broadcast(self):
        photo_sizes = [MagicMock(file_id="small"), MagicMock(file_id="large")]
        update = _make_update_with_message(photo=photo_sizes, caption="a note")
        ctx = MagicMock()
        ctx.user_data = {}
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "load_user_names", return_value={}), \
             patch.object(pb, "add_post_item", side_effect=lambda i: {**i, "id": "x"}) as mock_add, \
             patch.object(pb, "_pb_broadcast_new_item", new_callable=AsyncMock) as mock_broadcast:
            result = _run(pb.recv_pb_photo(update, ctx))
        self.assertEqual(result, pb.PB.POSTING)
        added = mock_add.call_args.args[0]
        self.assertEqual(added["photo_file_id"], "large")
        self.assertEqual(added["caption"], "a note")
        mock_broadcast.assert_called_once()
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


class TestCmdPbView(unittest.TestCase):
    def test_empty_queue_reports_no_pending_items(self):
        update = _make_update_with_message()
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "load_post_board", return_value=[]):
            _run(pb.cmd_pb_view(update, ctx))
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("No pending items", sent_text)

    def test_delivers_every_pending_item_to_requesting_chat(self):
        update = _make_update_with_message(user_id=777)
        ctx = MagicMock()
        items = [{"id": "1", "type": "text"}, {"id": "2", "type": "text"}]
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "load_post_board", return_value=items), \
             patch.object(pb, "_pb_deliver_item", new_callable=AsyncMock) as mock_deliver:
            _run(pb.cmd_pb_view(update, ctx))
        self.assertEqual(mock_deliver.call_count, 2)
        for call in mock_deliver.call_args_list:
            self.assertEqual(call.args[1], 777)


class TestRecvPbDone(unittest.TestCase):
    def test_missing_item_just_clears_markup(self):
        update = _make_update_with_callback("pb_done:missing")
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "get_post_item", return_value=None):
            _run(pb.recv_pb_done(update, ctx))
        update.callback_query.edit_message_reply_markup.assert_awaited_once()

    def test_already_removed_race_just_clears_markup(self):
        update = _make_update_with_callback("pb_done:abc")
        ctx = MagicMock()
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "get_post_item", return_value={"id": "abc", "type": "text", "broadcasts": []}), \
             patch.object(pb, "remove_post_item", return_value=False):
            _run(pb.recv_pb_done(update, ctx))
        update.callback_query.edit_message_reply_markup.assert_awaited_once()

    def test_text_item_edits_every_delivered_copy(self):
        update = _make_update_with_callback("pb_done:abc")
        ctx = MagicMock()
        ctx.bot.edit_message_text = AsyncMock()
        item = {
            "id": "abc", "type": "text", "text": "R1", "posted_by_name": "Jane", "posted_at": "t",
            "broadcasts": [{"chat_id": 111, "message_id": 5001}, {"chat_id": 222, "message_id": 5002}],
        }
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "get_post_item", return_value=item), \
             patch.object(pb, "remove_post_item", return_value=True), \
             patch.object(pb, "load_user_names", return_value={}):
            _run(pb.recv_pb_done(update, ctx))
        self.assertEqual(ctx.bot.edit_message_text.call_count, 2)
        for call in ctx.bot.edit_message_text.call_args_list:
            self.assertIn("Done", call.kwargs["text"])

    def test_photo_item_edits_every_delivered_copy_caption(self):
        update = _make_update_with_callback("pb_done:abc")
        ctx = MagicMock()
        ctx.bot.edit_message_caption = AsyncMock()
        item = {
            "id": "abc", "type": "photo", "photo_file_id": "f", "posted_by_name": "Jane", "posted_at": "t",
            "broadcasts": [{"chat_id": 111, "message_id": 5001}],
        }
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "get_post_item", return_value=item), \
             patch.object(pb, "remove_post_item", return_value=True), \
             patch.object(pb, "load_user_names", return_value={}):
            _run(pb.recv_pb_done(update, ctx))
        ctx.bot.edit_message_caption.assert_awaited_once()
        self.assertIn("Done", ctx.bot.edit_message_caption.call_args.kwargs["caption"])

    def test_edit_failure_on_one_copy_does_not_block_others(self):
        update = _make_update_with_callback("pb_done:abc")
        ctx = MagicMock()
        ctx.bot.edit_message_text = AsyncMock(side_effect=[RuntimeError("gone"), None])
        item = {
            "id": "abc", "type": "text", "text": "R1", "posted_by_name": "Jane", "posted_at": "t",
            "broadcasts": [{"chat_id": 111, "message_id": 5001}, {"chat_id": 222, "message_id": 5002}],
        }
        with patch.object(pb, "allowed", return_value=True), \
             patch.object(pb, "get_post_item", return_value=item), \
             patch.object(pb, "remove_post_item", return_value=True), \
             patch.object(pb, "load_user_names", return_value={}):
            _run(pb.recv_pb_done(update, ctx))   # must not raise
        self.assertEqual(ctx.bot.edit_message_text.call_count, 2)


if __name__ == "__main__":
    unittest.main()
