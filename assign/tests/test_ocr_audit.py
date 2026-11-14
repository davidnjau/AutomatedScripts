#!/usr/bin/env python3
"""
Unit tests for ocr_audit.py — the OCR screenshot audit store, lifecycle
cleanup, and 🖼 OCR History viewer.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ocr_audit as oa


def _run(coro):
    return asyncio.run(coro)


class _TmpStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.tmpdir.name, "saved_ocr_audit.json")
        self._p = patch.object(oa, "SAVED_OCR_AUDIT_FILE", self.data_file)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def _write(self, records):
        with open(self.data_file, "w") as f:
            json.dump(records, f)


class TestStore(_TmpStore):
    def test_load_missing_returns_empty(self):
        self.assertEqual(oa.load_ocr_audit(), [])

    def test_save_load_roundtrip(self):
        oa.save_ocr_audit([{"id": "a"}])
        self.assertEqual(oa.load_ocr_audit(), [{"id": "a"}])

    def test_save_trims_to_keep_limit(self):
        with patch.object(oa, "_AUDIT_KEEP", 3):
            oa.save_ocr_audit([{"id": str(i)} for i in range(10)])
            ids = [r["id"] for r in oa.load_ocr_audit()]
        self.assertEqual(ids, ["7", "8", "9"])   # newest kept


class TestRecordUpload(_TmpStore):
    def test_stores_image_and_metadata(self):
        with patch.object(oa.blob_store, "put_screenshot", return_value=True), \
             patch.object(oa, "load_user_names", return_value={"7": "Jane"}):
            rec = oa.record_upload(
                b"imgbytes", channel="quick_assign", uploaded_by=7, ocr_source="glm-ocr",
                valuer_name="Val", valuer_uid="u1",
                assignments=[{"ref": "R1", "mode": "assigned"}, {"ref": "R2", "mode": "queued"}],
            )
        self.assertTrue(rec["image_stored"])
        self.assertTrue(rec["blob_key"].startswith("screenshots/"))
        self.assertEqual(rec["refs"], ["R1", "R2"])
        self.assertEqual(rec["uploaded_by_name"], "Jane")
        self.assertEqual(len(oa.load_ocr_audit()), 1)

    def test_empty_assignments_records_nothing(self):
        with patch.object(oa.blob_store, "put_screenshot", return_value=True):
            rec = oa.record_upload(b"x", channel="new_assignment", uploaded_by=1,
                                   ocr_source="none", valuer_name="V", valuer_uid="u", assignments=[])
        self.assertIsNone(rec)
        self.assertEqual(oa.load_ocr_audit(), [])

    def test_storage_failure_still_records_metadata(self):
        with patch.object(oa.blob_store, "put_screenshot", return_value=False), \
             patch.object(oa, "load_user_names", return_value={}):
            rec = oa.record_upload(b"x", channel="quick_assign", uploaded_by=1, ocr_source="glm-ocr",
                                   valuer_name="V", valuer_uid="u", assignments=[{"ref": "R1", "mode": "assigned"}])
        self.assertFalse(rec["image_stored"])
        self.assertIsNone(rec["blob_key"])
        self.assertEqual(len(oa.load_ocr_audit()), 1)


class TestNotifyViewers(unittest.TestCase):
    def _bot(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()
        return bot

    def _rec(self, **over):
        r = {"id": "a", "blob_key": "screenshots/a.jpg", "image_purged": False,
             "at": "t", "uploaded_by_name": "U", "ocr_source": "glm-ocr", "channel": "quick_assign",
             "valuer_name": "V", "refs": ["R1"], "assignments": [{"ref": "R1", "mode": "assigned"}]}
        r.update(over)
        return r

    def test_sends_image_to_viewers_excluding_uploader(self):
        bot = self._bot()
        with patch.object(oa, "ALLOWED_IDS", [1, 2, 3]), \
             patch.object(oa, "_oh_can_view", side_effect=lambda uid: uid in (2, 3)), \
             patch.object(oa.blob_store, "get_screenshot", return_value=b"img"):
            _run(oa.notify_viewers(bot, self._rec(), exclude_user_id=3))
        # Only uid 2 (viewer, not the uploader uid 3) gets it.
        sent_to = [c.args[0] for c in bot.send_photo.call_args_list]
        self.assertEqual(sent_to, [2])

    def test_skips_non_viewers(self):
        bot = self._bot()
        with patch.object(oa, "ALLOWED_IDS", [1, 2]), \
             patch.object(oa, "_oh_can_view", return_value=False), \
             patch.object(oa.blob_store, "get_screenshot", return_value=b"img"):
            _run(oa.notify_viewers(bot, self._rec()))
        bot.send_photo.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    def test_text_fallback_when_image_absent(self):
        bot = self._bot()
        with patch.object(oa, "ALLOWED_IDS", [2]), \
             patch.object(oa, "_oh_can_view", return_value=True):
            _run(oa.notify_viewers(bot, self._rec(image_purged=True)))
        bot.send_message.assert_awaited_once()
        bot.send_photo.assert_not_awaited()

    def test_per_recipient_failure_is_swallowed(self):
        bot = self._bot()
        bot.send_photo.side_effect = RuntimeError("blocked")
        with patch.object(oa, "ALLOWED_IDS", [2, 4]), \
             patch.object(oa, "_oh_can_view", return_value=True), \
             patch.object(oa.blob_store, "get_screenshot", return_value=b"img"):
            _run(oa.notify_viewers(bot, self._rec()))   # must not raise
        self.assertEqual(bot.send_photo.await_count, 2)   # tried both despite the first failing


class TestEligibleForPurge(unittest.TestCase):
    def _rec(self, **over):
        r = {"id": "a", "image_stored": True, "image_purged": False, "blob_key": "k",
             "refs": ["R1", "R2"], "at": datetime.now().isoformat(timespec="seconds")}
        r.update(over)
        return r

    def test_all_refs_done_purges_as_completed(self):
        now = datetime.now()
        reason = oa._eligible_for_purge(self._rec(), {"R1": "completed", "R2": "returned"}, now)
        self.assertEqual(reason, "completed")

    def test_partial_done_recent_is_kept(self):
        now = datetime.now()
        self.assertIsNone(oa._eligible_for_purge(self._rec(), {"R1": "completed", "R2": "queued"}, now))

    def test_old_record_purged_by_max_age(self):
        now = datetime.now()
        old = (now - timedelta(days=oa._IMAGE_MAX_AGE_DAYS + 1)).isoformat(timespec="seconds")
        reason = oa._eligible_for_purge(self._rec(at=old), {"R1": "queued", "R2": "queued"}, now)
        self.assertEqual(reason, "max-age")

    def test_not_stored_is_never_purged(self):
        now = datetime.now()
        self.assertIsNone(oa._eligible_for_purge(self._rec(image_stored=False), {"R1": "completed", "R2": "completed"}, now))

    def test_already_purged_is_skipped(self):
        now = datetime.now()
        self.assertIsNone(oa._eligible_for_purge(self._rec(image_purged=True), {"R1": "completed", "R2": "completed"}, now))


class TestCleanupJob(_TmpStore):
    def test_purges_blob_keeps_metadata(self):
        self._write([{
            "id": "a", "image_stored": True, "image_purged": False, "blob_key": "k1",
            "refs": ["R1"], "at": datetime.now().isoformat(timespec="seconds"),
        }])
        with patch.object(oa, "_ref_status_map", return_value={"R1": "completed"}), \
             patch.object(oa.blob_store, "delete_screenshot", return_value=True) as mock_del:
            _run(oa._ocr_audit_cleanup_job(MagicMock()))
        mock_del.assert_called_once_with("k1")
        rec = oa.load_ocr_audit()[0]
        self.assertTrue(rec["image_purged"])
        self.assertEqual(rec["purge_reason"], "completed")
        self.assertEqual(rec["refs"], ["R1"])     # metadata kept

    def test_keeps_blob_when_not_done(self):
        self._write([{
            "id": "a", "image_stored": True, "image_purged": False, "blob_key": "k1",
            "refs": ["R1"], "at": datetime.now().isoformat(timespec="seconds"),
        }])
        with patch.object(oa, "_ref_status_map", return_value={"R1": "queued"}), \
             patch.object(oa.blob_store, "delete_screenshot") as mock_del:
            _run(oa._ocr_audit_cleanup_job(MagicMock()))
        mock_del.assert_not_called()
        self.assertFalse(oa.load_ocr_audit()[0]["image_purged"])


class TestAccess(unittest.TestCase):
    def test_admin_can_view(self):
        with patch.object(oa, "is_admin", return_value=True):
            self.assertTrue(oa._oh_can_view(1))

    def test_granted_button_can_view(self):
        with patch.object(oa, "is_admin", return_value=False), \
             patch.object(oa, "button_allowed", return_value=True):
            self.assertTrue(oa._oh_can_view(2))

    def test_ungranted_cannot_view(self):
        with patch.object(oa, "is_admin", return_value=False), \
             patch.object(oa, "button_allowed", return_value=False):
            self.assertFalse(oa._oh_can_view(2))


class TestRenderPage(unittest.TestCase):
    def _recs(self, n):
        return [{"id": f"id{i}", "at": "2026-11-14 10:00:00", "uploaded_by_name": "U",
                 "refs": [f"R{i}"], "valuer_name": "V", "image_stored": True, "image_purged": False}
                for i in range(n)]

    def test_view_buttons_and_close(self):
        text, kb = oa._oh_render_page(self._recs(3), 0)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertTrue(any(d.startswith("ocrh:view:") for d in datas))
        self.assertIn("ocrh:close", datas)

    def test_pagination_next_on_first_page(self):
        text, kb = oa._oh_render_page(self._recs(oa._OH_PAGE_SIZE + 2), 0)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("ocrh:page:1", datas)
        self.assertNotIn("ocrh:page:-1", datas)


class TestCmdOcrHistory(_TmpStore):
    def _update(self, user_id=1):
        update = MagicMock()
        update.effective_user.id = user_id
        update.message.reply_text = AsyncMock()
        return update

    def test_denied_without_access(self):
        update = self._update()
        with patch.object(oa, "allowed", return_value=True), \
             patch.object(oa, "_oh_can_view", return_value=False):
            result = _run(oa.cmd_ocr_history(update, MagicMock()))
        self.assertEqual(result, oa.ConversationHandler.END)
        self.assertIn("don't have access", update.message.reply_text.call_args[0][0])

    def test_empty_history_message(self):
        update = self._update()
        with patch.object(oa, "allowed", return_value=True), \
             patch.object(oa, "_oh_can_view", return_value=True):
            result = _run(oa.cmd_ocr_history(update, MagicMock()))
        self.assertEqual(result, oa.ConversationHandler.END)
        self.assertIn("No screenshots logged", update.message.reply_text.call_args[0][0])

    def test_with_records_shows_list(self):
        self._write([{"id": "a", "at": "2026-11-14 10:00:00", "uploaded_by_name": "U",
                      "refs": ["R1"], "valuer_name": "V", "image_stored": True, "image_purged": False}])
        update = self._update()
        ctx = MagicMock(); ctx.user_data = {}
        with patch.object(oa, "allowed", return_value=True), \
             patch.object(oa, "_oh_can_view", return_value=True):
            result = _run(oa.cmd_ocr_history(update, ctx))
        self.assertEqual(result, oa.OH.LIST)


class TestRecvOhAction(_TmpStore):
    def _query(self, data):
        update = MagicMock()
        q = update.callback_query
        q.data = data
        q.from_user.id = 1
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.edit_message_reply_markup = AsyncMock()
        q.message.reply_text = AsyncMock()
        q.message.reply_photo = AsyncMock()
        return update

    def test_close_ends(self):
        update = self._query("ocrh:close")
        with patch.object(oa, "_oh_can_view", return_value=True):
            result = _run(oa.recv_oh_action(update, MagicMock()))
        self.assertEqual(result, oa.ConversationHandler.END)

    def test_view_sends_photo_when_stored(self):
        self._write([{"id": "a", "blob_key": "k", "image_stored": True, "image_purged": False,
                      "at": "t", "uploaded_by_name": "U", "ocr_source": "glm-ocr", "channel": "quick_assign",
                      "valuer_name": "V", "refs": ["R1"], "assignments": [{"ref": "R1", "mode": "assigned"}]}])
        update = self._query("ocrh:view:a")
        with patch.object(oa, "_oh_can_view", return_value=True), \
             patch.object(oa.blob_store, "get_screenshot", return_value=b"imgdata"):
            result = _run(oa.recv_oh_action(update, MagicMock()))
        self.assertEqual(result, oa.OH.LIST)
        update.callback_query.message.reply_photo.assert_awaited_once()

    def test_view_purged_sends_text(self):
        self._write([{"id": "a", "blob_key": "k", "image_stored": True, "image_purged": True,
                      "at": "t", "uploaded_by_name": "U", "ocr_source": "glm-ocr", "channel": "quick_assign",
                      "valuer_name": "V", "refs": ["R1"], "assignments": [{"ref": "R1", "mode": "assigned"}]}])
        update = self._query("ocrh:view:a")
        with patch.object(oa, "_oh_can_view", return_value=True):
            _run(oa.recv_oh_action(update, MagicMock()))
        update.callback_query.message.reply_text.assert_awaited_once()
        self.assertIn("purged", update.callback_query.message.reply_text.call_args[0][0].lower())


if __name__ == "__main__":
    unittest.main()
