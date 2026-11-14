#!/usr/bin/env python3
"""
Unit tests for quick_assign.py — valuer name matching, the direct-then-queue
assignment logic, the enqueue, the name handler's 0/1/N branching, the
background job, and the View References viewer.

Run with: python3 -m unittest discover -s assign/tests -v
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quick_assign as qa


def _run(coro):
    return asyncio.run(coro)


def _msg_update(text=""):
    u = MagicMock()
    u.message.text = text
    u.message.reply_text = AsyncMock()
    u.message.chat_id = 555
    u.effective_user.id = 111
    return u


_V = [
    {"name": "Godwill Mwangi", "uid": "u1", "account_number": "a1"},
    {"name": "Jane Doe",       "uid": "u2", "account_number": "a2"},
    {"name": "John Godwin",    "uid": "u3", "account_number": "a3"},
]


class TestMatchValuers(unittest.TestCase):
    def test_single_match_is_autopicked(self):
        with patch.object(qa, "load_saved_valuers", return_value=_V):
            m = qa._qa_match_valuers("godwill")
        self.assertEqual([v["uid"] for v in m], ["u1"])

    def test_no_match(self):
        with patch.object(qa, "load_saved_valuers", return_value=_V):
            self.assertEqual(qa._qa_match_valuers("Zxq"), [])

    def test_multiple_matches(self):
        # "godwi" is a substring of both "Godwill" and "Godwin".
        with patch.object(qa, "load_saved_valuers", return_value=_V):
            m = qa._qa_match_valuers("godwi")
        self.assertEqual({v["uid"] for v in m}, {"u1", "u3"})

    def test_blank_name(self):
        with patch.object(qa, "load_saved_valuers", return_value=_V):
            self.assertEqual(qa._qa_match_valuers("   "), [])


class TestAssignOrQueue(unittest.TestCase):
    def _http(self, raise_on_post=False):
        http = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock(side_effect=RuntimeError("not at DLV") if raise_on_post else None)
        http.post.return_value = resp
        return http

    def test_direct_assign_when_post_succeeds(self):
        v = {"name": "Godwill", "uid": "u1", "account_number": "a1"}
        with patch.object(qa, "_any_valid_tokens", return_value=MagicMock()), \
             patch.object(qa, "auth_headers", return_value={}), \
             patch.object(qa, "build_session", return_value=self._http(False)), \
             patch.object(qa, "persist_assignment") as mock_persist, \
             patch.object(qa, "_qa_enqueue") as mock_enqueue:
            out = qa._qa_assign_or_queue(["CNTYINV/ABC1234567"], v, "stamp_duty", 111)
        self.assertEqual(out["assigned"], ["CNTYINV/ABC1234567"])
        self.assertEqual(out["queued"], [])
        mock_persist.assert_called_once()
        self.assertEqual(mock_persist.call_args.kwargs["extra"]["channel"], "quick_assign")
        mock_enqueue.assert_not_called()

    def test_queues_when_direct_fails(self):
        v = {"name": "Godwill", "uid": "u1", "account_number": "a1"}
        with patch.object(qa, "_any_valid_tokens", return_value=MagicMock()), \
             patch.object(qa, "auth_headers", return_value={}), \
             patch.object(qa, "build_session", return_value=self._http(True)), \
             patch.object(qa, "persist_assignment") as mock_persist, \
             patch.object(qa, "_qa_enqueue") as mock_enqueue:
            out = qa._qa_assign_or_queue(["REG/TSFR/ABC1234567"], v, "stamp_duty", 111)
        self.assertEqual(out["assigned"], [])
        self.assertEqual(out["queued"], ["REG/TSFR/ABC1234567"])
        mock_enqueue.assert_called_once()
        mock_persist.assert_not_called()

    def test_land_rent_body_has_no_node(self):
        v = {"name": "G", "uid": "u1", "account_number": "a1"}
        http = self._http(False)
        with patch.object(qa, "_any_valid_tokens", return_value=MagicMock()), \
             patch.object(qa, "auth_headers", return_value={}), \
             patch.object(qa, "build_session", return_value=http), \
             patch.object(qa, "persist_assignment"), patch.object(qa, "_qa_enqueue"):
            qa._qa_assign_or_queue(["DLA/LEASE/ABC1234567"], v, "land_rent", 111)
        body = http.post.call_args.kwargs["json"]
        self.assertNotIn("node", body)
        self.assertEqual(body["valuation_officer"], "u1")

    def test_no_token(self):
        with patch.object(qa, "_any_valid_tokens", return_value=None):
            out = qa._qa_assign_or_queue(["CNTYINV/ABC1234567"], {"name": "G", "uid": "u1"}, "stamp_duty", 111)
        self.assertEqual(out["no_token"], ["CNTYINV/ABC1234567"])


class TestEnqueue(unittest.TestCase):
    def test_appends_item_with_channel(self):
        v = {"name": "Godwill", "uid": "u1", "account_number": "a1"}
        with patch.object(qa, "load_dlv_batch", return_value=[]), \
             patch.object(qa, "save_dlv_batch") as mock_save:
            qa._qa_enqueue("CNTYINV/ABC1234567", v, "stamp_duty", 111, "2026-11-13T10:00:00")
        item = mock_save.call_args[0][0][0]
        self.assertEqual(item["ref"], "CNTYINV/ABC1234567")
        self.assertEqual(item["valuer_uid"], "u1")
        self.assertEqual(item["channel"], "quick_assign")

    def test_dedupes_already_queued(self):
        with patch.object(qa, "load_dlv_batch", return_value=[{"ref": "CNTYINV/ABC1234567"}]), \
             patch.object(qa, "save_dlv_batch") as mock_save:
            qa._qa_enqueue("CNTYINV/ABC1234567", {"name": "G", "uid": "u1"}, "stamp_duty", 111, "now")
        mock_save.assert_not_called()


class TestRecvName(unittest.TestCase):
    def test_zero_matches_reprompts(self):
        u = _msg_update("Zxq")
        ctx = MagicMock(); ctx.user_data = {"qa_session": qa.QASession()}
        with patch.object(qa, "_qa_match_valuers", return_value=[]):
            result = _run(qa.recv_qa_name(u, ctx))
        self.assertEqual(result, qa.QA.NAME)

    def test_single_match_launches(self):
        u = _msg_update("Godwill")
        ctx = MagicMock(); ctx.user_data = {"qa_session": qa.QASession()}
        with patch.object(qa, "_qa_match_valuers", return_value=[_V[0]]), \
             patch.object(qa, "_qa_launch", new=AsyncMock(return_value=qa.ConversationHandler.END)) as mock_launch:
            result = _run(qa.recv_qa_name(u, ctx))
        self.assertEqual(result, qa.ConversationHandler.END)
        self.assertEqual(mock_launch.call_args[0][2], _V[0])   # launched with the one match

    def test_multiple_matches_asks_which(self):
        u = _msg_update("godwi")
        ctx = MagicMock(); ctx.user_data = {"qa_session": qa.QASession()}
        with patch.object(qa, "_qa_match_valuers", return_value=[_V[0], _V[2]]):
            result = _run(qa.recv_qa_name(u, ctx))
        self.assertEqual(result, qa.QA.PICK_VALUER)
        self.assertEqual(len(ctx.user_data["qa_session"].matches), 2)


class TestRecvPhoto(unittest.TestCase):
    def _photo_update(self, caption=None):
        u = MagicMock()
        u.message.caption = caption
        f = MagicMock(); f.download_as_bytearray = AsyncMock(return_value=b"img")
        photo = MagicMock(); photo.get_file = AsyncMock(return_value=f)
        u.message.photo = [photo]
        u.message.reply_text = AsyncMock()
        u.message.chat_id = 555
        u.effective_user.id = 111
        return u

    def test_caption_name_skips_the_name_prompt(self):
        u = self._photo_update(caption="Godwill")
        ctx = MagicMock(); ctx.user_data = {"qa_session": qa.QASession()}
        with patch.object(qa, "_qa_match_valuers", return_value=[_V[0]]), \
             patch.object(qa, "_qa_launch", new=AsyncMock(return_value=qa.ConversationHandler.END)) as mock_launch:
            result = _run(qa.recv_qa_photo(u, ctx))
        self.assertEqual(result, qa.ConversationHandler.END)
        mock_launch.assert_awaited_once()

    def test_no_caption_asks_for_name(self):
        u = self._photo_update(caption=None)
        ctx = MagicMock(); ctx.user_data = {"qa_session": qa.QASession()}
        result = _run(qa.recv_qa_photo(u, ctx))
        self.assertEqual(result, qa.QA.NAME)
        self.assertIn("valuer name", u.message.reply_text.call_args[0][0].lower())

    def test_caption_with_multiple_matches_asks_which(self):
        u = self._photo_update(caption="godwi")
        ctx = MagicMock(); ctx.user_data = {"qa_session": qa.QASession()}
        with patch.object(qa, "_qa_match_valuers", return_value=[_V[0], _V[2]]):
            result = _run(qa.recv_qa_photo(u, ctx))
        self.assertEqual(result, qa.QA.PICK_VALUER)


class TestProcessJob(unittest.TestCase):
    def setUp(self):
        # The job audits the uploaded screenshot and notifies OCR History
        # viewers — stub both so tests don't touch the real store or send.
        self._p = patch.object(qa.ocr_audit, "record_upload")
        self._p.start()
        self.addCleanup(self._p.stop)
        self._pn = patch.object(qa.ocr_audit, "notify_viewers", new=AsyncMock())
        self._pn.start()
        self.addCleanup(self._pn.stop)

    def _ctx(self, workflow="stamp_duty"):
        ctx = MagicMock()
        ctx.job.data = {"chat_id": 555, "user_id": 111, "workflow": workflow,
                        "photo_bytes": b"img", "valuer": {"name": "Godwill", "uid": "u1", "account_number": "a1"}}
        ctx.bot.send_message = AsyncMock()
        return ctx

    def test_no_refs_notifies(self):
        ctx = self._ctx()
        with patch.object(qa, "ocr_extract_refs", new=AsyncMock(return_value=([], "none"))):
            _run(qa._qa_process_job(ctx))
        sent = " ".join(c.args[1] for c in ctx.bot.send_message.call_args_list)
        self.assertIn("couldn't read", sent.lower())

    def test_ocr_failure_is_audited_and_pushed(self):
        # A failed OCR still records the screenshot (assignments=[]) and
        # notifies the OCR History reviewers.
        ctx = self._ctx()
        with patch.object(qa, "ocr_extract_refs", new=AsyncMock(return_value=([], "none"))):
            _run(qa._qa_process_job(ctx))
        qa.ocr_audit.record_upload.assert_called_once()
        self.assertEqual(qa.ocr_audit.record_upload.call_args.kwargs["assignments"], [])
        qa.ocr_audit.notify_viewers.assert_awaited_once()

    def test_summary_on_success(self):
        ctx = self._ctx()
        with patch.object(qa, "ocr_extract_refs", new=AsyncMock(return_value=(["CNTYINV/ABC1234567"], "glm-ocr"))), \
             patch.object(qa, "_qa_assign_or_queue",
                          return_value={"assigned": ["CNTYINV/ABC1234567"], "queued": [], "failed": [], "no_token": []}), \
             patch.object(qa, "persist_valuer") as mock_pv:
            _run(qa._qa_process_job(ctx))
        sent = " ".join(c.args[1] for c in ctx.bot.send_message.call_args_list)
        self.assertIn("Assigned now: 1", sent)
        self.assertIn("CNTYINV/ABC1234567", sent)
        mock_pv.assert_called_once()

    def test_no_token_notifies(self):
        ctx = self._ctx()
        with patch.object(qa, "ocr_extract_refs", new=AsyncMock(return_value=(["CNTYINV/ABC1234567"], "glm-ocr"))), \
             patch.object(qa, "_qa_assign_or_queue",
                          return_value={"assigned": [], "queued": [], "failed": [], "no_token": ["CNTYINV/ABC1234567"]}):
            _run(qa._qa_process_job(ctx))
        sent = " ".join(c.args[1] for c in ctx.bot.send_message.call_args_list)
        self.assertIn("Refresh Auth", sent)


class TestViewReferences(unittest.TestCase):
    def test_empty(self):
        u = _msg_update()
        with patch.object(qa, "allowed", return_value=True), \
             patch.object(qa, "load_records_where", return_value=[]):
            _run(qa.cmd_view_references(u, MagicMock()))
        self.assertIn("nothing processed", u.message.reply_text.call_args[0][0].lower())

    def test_lists_records_newest_first(self):
        recs = [
            {"ref": "A", "valuer_name": "G", "status": "assigned", "assigned_at": "2026-11-10T09:00:00",
             "workflow": "stamp_duty", "node": "N1", "application_status": "ONGOING"},
            {"ref": "B", "valuer_name": "G", "status": "queued", "queued_at": "2026-11-12T09:00:00",
             "workflow": "stamp_duty", "node": "", "application_status": ""},
        ]
        u = _msg_update()
        with patch.object(qa, "allowed", return_value=True), \
             patch.object(qa, "load_records_where", return_value=recs):
            _run(qa.cmd_view_references(u, MagicMock()))
        sent = " ".join(c.args[0] for c in u.message.reply_text.call_args_list)
        self.assertIn("2 record(s)", sent)
        self.assertIn("New Assignment", sent)   # the assigned one
        self.assertIn("Queued", sent)           # the queued one


class TestFormatRecord(unittest.TestCase):
    def test_assigned_vs_queued_mode(self):
        a = qa._qa_format_record(1, {"ref": "A", "status": "assigned", "valuer_name": "G", "workflow": "stamp_duty"})
        q = qa._qa_format_record(2, {"ref": "B", "status": "queued", "valuer_name": "G", "workflow": "land_rent"})
        self.assertIn("New Assignment", a)
        self.assertIn("Queued", q)


if __name__ == "__main__":
    unittest.main()
