#!/usr/bin/env python3
"""
Unit tests for sms_sender.py — the pure helpers (code gen, MSISDN
normalization, recipient cycling, payload build, apikey redaction),
template/pool persistence, and the admin-gated conversation handlers
(including that Simulate never POSTs and the real path POSTs the right
body).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import json
import os
import string
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sms_sender as sms


def _run(coro):
    return asyncio.run(coro)


def _make_update_with_message(text=""):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_user.id = 111
    return update


def _make_update_with_callback(data):
    update = MagicMock()
    q = update.callback_query
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message.reply_text = AsyncMock()
    q.message.chat_id = 555
    update.effective_user.id = 111
    return update


_ENV = {"SMS_PARTNER_ID": "10709", "SMS_API_KEY": "secret-key"}


class TestGenCode(unittest.TestCase):
    def test_always_six_digits(self):
        for _ in range(200):
            c = sms._gen_code()
            self.assertEqual(len(c), 6)
            self.assertTrue(c.isdigit())


class TestGenRef(unittest.TestCase):
    def test_ten_char_upper_alnum(self):
        for _ in range(200):
            r = sms._gen_ref()
            self.assertEqual(len(r), 10)
            self.assertTrue(all(c in (string.ascii_uppercase + string.digits) for c in r))


class TestRenderMessage(unittest.TestCase):
    def test_code_placeholder_becomes_six_digits(self):
        out = sms._render_message("code is:{code}")
        self.assertTrue(out.startswith("code is:"))
        tail = out[len("code is:"):]
        self.assertEqual(len(tail), 6)
        self.assertTrue(tail.isdigit())

    def test_ref_placeholder_uses_supplied_reference(self):
        out = sms._render_message("reference {ref} ok", ref="CNTYINV/O8XZ1LLBYU")
        self.assertEqual(out, "reference CNTYINV/O8XZ1LLBYU ok")

    def test_ref_placeholder_falls_back_to_random_token_when_none(self):
        out = sms._render_message("ref {ref} ok", ref=None)
        self.assertRegex(out, r"^ref [A-Z0-9]{10} ok$")

    def test_no_placeholder_left_after_render(self):
        out = sms._render_message("a {ref} b {code} c", ref="R1")
        self.assertNotIn("{ref}", out)
        self.assertNotIn("{code}", out)

    def test_plain_text_unchanged(self):
        self.assertEqual(sms._render_message("no placeholders here"), "no placeholders here")

    def test_purpose_derived_from_supplied_ref_prefix(self):
        out = sms._render_message("{ref} submitted for {purpose}", ref="REG/TSFR/ZVAW5IFLQZ")
        self.assertEqual(out, "REG/TSFR/ZVAW5IFLQZ submitted for registration")

    def test_purpose_and_ref_stay_consistent_with_fallback_ref(self):
        # No ref supplied: {ref} gets a random prefix-less token, so {purpose}
        # resolves to the no-prefix "survey" and both agree on one reference.
        out = sms._render_message("{ref} for {purpose}", ref=None)
        self.assertRegex(out, r"^[A-Z0-9]{10} for survey$")


class TestRefPurpose(unittest.TestCase):
    def test_known_prefixes(self):
        self.assertEqual(sms._ref_purpose("REG/TSFR/ABC"), "registration")
        self.assertEqual(sms._ref_purpose("DLA/LEASE/ABC"), "land administration")
        self.assertEqual(sms._ref_purpose("LAS/ADJ/ABC"), "adjudication")

    def test_prefix_is_case_insensitive(self):
        self.assertEqual(sms._ref_purpose("reg/tsfr/abc"), "registration")

    def test_no_prefix_token_is_survey(self):
        self.assertEqual(sms._ref_purpose("ZZBFEG2D6K"), "survey")

    def test_unknown_prefix_falls_back_to_processing(self):
        self.assertEqual(sms._ref_purpose("XYZ/FOO/ABC"), "processing")


class TestNormalizeMsisdn(unittest.TestCase):
    def test_local_zero_prefixed(self):
        self.assertEqual(sms._normalize_msisdn("0716060198"), "254716060198")

    def test_plus_254_e164(self):
        self.assertEqual(sms._normalize_msisdn("+254716060198"), "254716060198")

    def test_bare_nine_digit(self):
        self.assertEqual(sms._normalize_msisdn("716060198"), "254716060198")

    def test_already_normalized(self):
        self.assertEqual(sms._normalize_msisdn("254716060198"), "254716060198")

    def test_strips_spaces_and_punctuation(self):
        self.assertEqual(sms._normalize_msisdn("+254 716 060 198"), "254716060198")

    def test_rejects_junk(self):
        self.assertIsNone(sms._normalize_msisdn(""))
        self.assertIsNone(sms._normalize_msisdn("123"))
        self.assertIsNone(sms._normalize_msisdn("not a phone"))
        self.assertIsNone(sms._normalize_msisdn("2547160601980000"))


class TestBuildRecipients(unittest.TestCase):
    def test_exact_length_and_cycling(self):
        pool = ["254700000001", "254700000002"]
        self.assertEqual(
            sms._build_recipients(pool, 5),
            ["254700000001", "254700000002", "254700000001", "254700000002", "254700000001"],
        )

    def test_fewer_than_pool(self):
        pool = ["a", "b", "c"]
        self.assertEqual(sms._build_recipients(pool, 2), ["a", "b"])

    def test_empty_pool_yields_empty(self):
        self.assertEqual(sms._build_recipients([], 3), [])


class TestBuildPayload(unittest.TestCase):
    def test_shape_creds_and_rendered_message(self):
        with patch.dict(os.environ, _ENV, clear=False):
            payload = sms._build_payload(["254700000001", "254700000002"], "MSG:{code}")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(payload["smslist"]), 2)
        item = payload["smslist"][0]
        self.assertEqual(item["partnerID"], "10709")
        self.assertEqual(item["apikey"], "secret-key")
        self.assertEqual(item["pass_type"], sms._SMS_PASS_TYPE)
        self.assertEqual(item["shortcode"], sms._SMS_SHORTCODE)
        self.assertEqual(item["mobile"], "254700000001")
        # message rendered: placeholder substituted, none left behind
        self.assertTrue(item["message"].startswith("MSG:"))
        self.assertNotIn("{code}", item["message"])
        self.assertTrue(item["message"][-6:].isdigit())

    def test_values_are_rendered_per_recipient(self):
        # Asserts each entry is rendered independently, not once and reused.
        with patch.dict(os.environ, _ENV, clear=False), \
             patch.object(sms, "_gen_code", side_effect=["111111", "222222"]):
            payload = sms._build_payload(["254700000001", "254700000002"], "C:{code}")
        self.assertEqual(payload["smslist"][0]["message"], "C:111111")
        self.assertEqual(payload["smslist"][1]["message"], "C:222222")

    def test_refs_are_applied_per_recipient(self):
        with patch.dict(os.environ, _ENV, clear=False):
            payload = sms._build_payload(
                ["254700000001", "254700000002"],
                "ref {ref}",
                refs=["REG/TSFR/AAA", "CNTYINV/BBB"],
            )
        self.assertEqual(payload["smslist"][0]["message"], "ref REG/TSFR/AAA")
        self.assertEqual(payload["smslist"][1]["message"], "ref CNTYINV/BBB")


class TestSampleSummary(unittest.TestCase):
    def test_shows_sample_message_and_trimmed_numbers(self):
        payload = {"count": 5, "smslist": [
            {"mobile": f"25470000000{i}", "message": f"MSG {i}", "apikey": "secret"} for i in range(5)
        ]}
        lines = sms._sample_summary(payload, max_numbers=3)
        joined = "\n".join(lines)
        self.assertIn("MSG 0", joined)                 # first message is the sample
        self.assertIn("254700000000", joined)
        self.assertIn("(+2 more)", joined)             # 5 total, 3 shown
        self.assertNotIn("secret", joined)             # apikey never shown

    def test_empty_payload_yields_no_lines(self):
        self.assertEqual(sms._sample_summary({"count": 0, "smslist": []}), [])


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._p1 = patch.object(sms, "SAVED_SMS_TEMPLATES_FILE", os.path.join(self.tmp.name, "t.json"))
        self._p2 = patch.object(sms, "SAVED_SMS_TEST_NUMBERS_FILE", os.path.join(self.tmp.name, "n.json"))
        self._p3 = patch.object(sms, "SAVED_SMS_REF_NUMBERS_FILE", os.path.join(self.tmp.name, "r.json"))
        self._p1.start(); self._p2.start(); self._p3.start()
        self.addCleanup(self._p1.stop); self.addCleanup(self._p2.stop); self.addCleanup(self._p3.stop)

    def test_templates_seed_defaults_on_first_load(self):
        loaded = sms.load_sms_templates()
        self.assertEqual(loaded, list(sms._DEFAULT_TEMPLATES))
        # every seeded sample carries the test prefix
        self.assertTrue(all(t.startswith("ARDHISASA: Test :") for t in loaded))

    def test_templates_roundtrip(self):
        sms.save_sms_templates(["A", "B"])
        self.assertEqual(sms.load_sms_templates(), ["A", "B"])

    def test_pool_missing_returns_empty(self):
        self.assertEqual(sms.load_sms_test_numbers(), [])

    def test_pool_roundtrip(self):
        with open(sms.SAVED_SMS_TEST_NUMBERS_FILE, "w") as f:
            json.dump(["254700000001"], f)
        self.assertEqual(sms.load_sms_test_numbers(), ["254700000001"])

    def test_ref_pool_missing_returns_empty(self):
        self.assertEqual(sms.load_sms_ref_numbers(), [])

    def test_ref_pool_roundtrip(self):
        with open(sms.SAVED_SMS_REF_NUMBERS_FILE, "w") as f:
            json.dump(["REG/TSFR/AAA", "CNTYINV/BBB"], f)
        self.assertEqual(sms.load_sms_ref_numbers(), ["REG/TSFR/AAA", "CNTYINV/BBB"])


class TestCmdSms(unittest.TestCase):
    def test_non_admin_denied(self):
        update = _make_update_with_message()
        ctx = MagicMock(); ctx.user_data = {}
        with patch.object(sms, "allowed", return_value=True), \
             patch.object(sms, "is_admin", return_value=False):
            result = _run(sms.cmd_sms(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        self.assertIn("admin-only", update.message.reply_text.call_args[0][0])

    def test_empty_pool_ends(self):
        update = _make_update_with_message()
        ctx = MagicMock(); ctx.user_data = {}
        with patch.object(sms, "allowed", return_value=True), \
             patch.object(sms, "is_admin", return_value=True), \
             patch.object(sms, "load_sms_test_numbers", return_value=[]):
            result = _run(sms.cmd_sms(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        self.assertIn("pool is empty", update.message.reply_text.call_args[0][0])

    def test_missing_creds_ends(self):
        update = _make_update_with_message()
        ctx = MagicMock(); ctx.user_data = {}
        with patch.object(sms, "allowed", return_value=True), \
             patch.object(sms, "is_admin", return_value=True), \
             patch.object(sms, "load_sms_test_numbers", return_value=["254700000001"]), \
             patch.dict(os.environ, {"SMS_PARTNER_ID": "", "SMS_API_KEY": ""}, clear=False):
            result = _run(sms.cmd_sms(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)

    def test_happy_path_asks_mode(self):
        update = _make_update_with_message()
        ctx = MagicMock(); ctx.user_data = {}
        with patch.object(sms, "allowed", return_value=True), \
             patch.object(sms, "is_admin", return_value=True), \
             patch.object(sms, "load_sms_test_numbers", return_value=["254700000001"]), \
             patch.dict(os.environ, _ENV, clear=False):
            result = _run(sms.cmd_sms(update, ctx))
        self.assertEqual(result, sms.SMS.MODE)


class TestRecvSmsMode(unittest.TestCase):
    def test_cancel_ends(self):
        update = _make_update_with_callback("sms_mode:cancel")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        result = _run(sms.recv_sms_mode(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)

    def test_simulate_sets_flag_and_asks_count(self):
        update = _make_update_with_callback("sms_mode:simulate")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        result = _run(sms.recv_sms_mode(update, ctx))
        self.assertEqual(result, sms.SMS.COUNT)
        self.assertTrue(ctx.user_data["sms_session"].simulate)

    def test_real_clears_flag(self):
        update = _make_update_with_callback("sms_mode:real")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        _run(sms.recv_sms_mode(update, ctx))
        self.assertFalse(ctx.user_data["sms_session"].simulate)


class TestRecvSmsCount(unittest.TestCase):
    def test_out_of_range_reprompts(self):
        for bad in ["0", "51", "abc", "-3"]:
            update = _make_update_with_message(bad)
            ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
            result = _run(sms.recv_sms_count(update, ctx))
            self.assertEqual(result, sms.SMS.COUNT, f"{bad!r} should reprompt")

    def test_valid_count_advances_to_template(self):
        update = _make_update_with_message("5")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        with patch.object(sms, "load_sms_templates", return_value=["T"]):
            result = _run(sms.recv_sms_count(update, ctx))
        self.assertEqual(result, sms.SMS.TEMPLATE)
        self.assertEqual(ctx.user_data["sms_session"].count, 5)


class TestRecvSmsTemplate(unittest.TestCase):
    def test_cancel_ends(self):
        update = _make_update_with_callback("sms_tpl:cancel")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        result = _run(sms.recv_sms_template(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)

    def test_new_goes_to_add(self):
        update = _make_update_with_callback("sms_tpl:new")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        result = _run(sms.recv_sms_template(update, ctx))
        self.assertEqual(result, sms.SMS.TEMPLATE_ADD)

    def test_pick_sets_message_and_confirms(self):
        update = _make_update_with_callback("sms_tpl:0")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        with patch.object(sms, "load_sms_templates", return_value=["Hello:"]):
            result = _run(sms.recv_sms_template(update, ctx))
        self.assertEqual(result, sms.SMS.CONFIRM)
        self.assertEqual(ctx.user_data["sms_session"].message, "Hello:")


class TestRecvSmsTemplateAdd(unittest.TestCase):
    def test_empty_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        result = _run(sms.recv_sms_template_add(update, ctx))
        self.assertEqual(result, sms.SMS.TEMPLATE_ADD)

    def test_valid_saves_and_confirms(self):
        update = _make_update_with_message("New msg:")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        with patch.object(sms, "load_sms_templates", return_value=["Old:"]), \
             patch.object(sms, "save_sms_templates") as mock_save:
            result = _run(sms.recv_sms_template_add(update, ctx))
        self.assertEqual(result, sms.SMS.CONFIRM)
        self.assertEqual(ctx.user_data["sms_session"].message, "New msg:")
        mock_save.assert_called_once_with(["Old:", "New msg:"])


class TestRecvSmsGo(unittest.TestCase):
    def _sess(self, simulate):
        return sms.SMSSession(simulate=simulate, count=3, message="ARDHISASA code:")

    def test_no_cancels(self):
        update = _make_update_with_callback("sms_go:no")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(True)}
        result = _run(sms.recv_sms_go(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)

    def test_simulate_never_posts(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(True)}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms.requests, "post") as mock_post, \
             patch.dict(os.environ, _ENV, clear=False):
            result = _run(sms.recv_sms_go(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        mock_post.assert_not_called()

    def test_simulate_preview_is_compact_and_hides_apikey(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(True)}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms.requests, "post"), \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms.recv_sms_go(update, ctx))
        shown = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("SIMULATION", shown)
        self.assertIn("Sample message", shown)
        self.assertNotIn("secret-key", shown)

    def test_real_posts_correct_body_and_bumps_count(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(False)}
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"responses": [
            {"response-code": 200, "response-description": "Success", "mobile": "254700000001", "messageid": "979295131"},
            {"response-code": 200, "response-description": "Success", "mobile": "254700000002", "messageid": "979295132"},
            {"response-code": 200, "response-description": "Success", "mobile": "254700000001", "messageid": "979295133"},
        ]}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms.requests, "post", return_value=resp) as mock_post, \
             patch.object(sms, "_bump_sms_sent_count", return_value=103) as mock_bump, \
             patch.dict(os.environ, _ENV, clear=False):
            result = _run(sms.recv_sms_go(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        self.assertEqual(mock_post.call_args.args[0], sms.SMS_SEND_URL)
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["smslist"][0]["partnerID"], "10709")
        self.assertEqual(body["smslist"][0]["shortcode"], "Text")
        mock_bump.assert_called_once_with(3)   # all 3 accepted
        sent = " ".join(c.args[0] for c in update.callback_query.message.reply_text.call_args_list)
        self.assertIn("3 message(s) success", sent)       # compact count summary
        self.assertIn("Total texts sent to date: 103", sent)
        self.assertIn("254700000001", sent)               # sample number, not a full dump

    def test_real_uses_reference_pool(self):
        update = _make_update_with_callback("sms_go:yes")
        sess = sms.SMSSession(simulate=False, count=2, message="ref {ref}")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sess}
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"responses": [{"response-code": 200, "mobile": "x", "messageid": "1"}]}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=["REG/TSFR/AAA", "CNTYINV/BBB"]), \
             patch.object(sms.requests, "post", return_value=resp) as mock_post, \
             patch.object(sms, "_bump_sms_sent_count", return_value=1), \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms.recv_sms_go(update, ctx))
        msgs = [i["message"] for i in mock_post.call_args.kwargs["json"]["smslist"]]
        self.assertEqual(msgs, ["ref REG/TSFR/AAA", "ref CNTYINV/BBB"])   # real refs, cycled per recipient

    def test_real_send_counts_only_accepted(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(False)}
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"responses": [
            {"response-code": 200, "response-description": "Success", "mobile": "254700000001", "messageid": "1"},
            {"response-code": 1006, "response-description": "Invalid", "mobile": "254700000002", "messageid": None},
            {"response-code": 200, "response-description": "Success", "mobile": "254700000001", "messageid": "3"},
        ]}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms.requests, "post", return_value=resp), \
             patch.object(sms, "_bump_sms_sent_count", return_value=2) as mock_bump, \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms.recv_sms_go(update, ctx))
        mock_bump.assert_called_once_with(2)   # 2 of 3 accepted, failed one not counted

    def test_real_send_failure_is_reported_not_raised(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(False)}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms.requests, "post", side_effect=RuntimeError("gateway down")), \
             patch.object(sms, "_bump_sms_sent_count") as mock_bump, \
             patch.dict(os.environ, _ENV, clear=False):
            result = _run(sms.recv_sms_go(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        mock_bump.assert_not_called()   # nothing accepted on a transport failure
        sent = " ".join(c.args[0] for c in update.callback_query.message.reply_text.call_args_list)
        self.assertIn("failed", sent.lower())


class TestSummarizeSendResponse(unittest.TestCase):
    def test_parses_the_gateway_sample(self):
        resp = MagicMock()
        resp.json.return_value = {"responses": [
            {"response-code": 200, "response-description": "Success", "mobile": "254700147275",
             "messageid": "979295131", "clientsmsid": None, "networkid": 1},
        ]}
        lines, ok, fail = sms._summarize_send_response(resp)
        self.assertEqual((ok, fail), (1, 0))
        self.assertEqual(len(lines), 1)
        self.assertIn("254700147275", lines[0])
        self.assertIn("979295131", lines[0])
        self.assertIn("✅", lines[0])

    def test_counts_failures(self):
        resp = MagicMock()
        resp.json.return_value = {"responses": [
            {"response-code": 200, "mobile": "a", "messageid": "1"},
            {"response-code": 1007, "response-description": "No credit", "mobile": "b", "messageid": None},
        ]}
        lines, ok, fail = sms._summarize_send_response(resp)
        self.assertEqual((ok, fail), (1, 1))
        self.assertIn("❌", lines[1])

    def test_non_json_falls_back_to_raw(self):
        resp = MagicMock(text="<html>gateway error</html>")
        resp.json.side_effect = ValueError("not json")
        lines, ok, fail = sms._summarize_send_response(resp)
        self.assertEqual((ok, fail), (0, 0))
        self.assertIn("Unrecognized", lines[0])


class TestSentCountPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._p = patch.object(sms, "SAVED_SMS_SENT_COUNT_FILE", os.path.join(self.tmp.name, "c.json"))
        self._p.start(); self.addCleanup(self._p.stop)

    def test_missing_returns_zero(self):
        self.assertEqual(sms.load_sms_sent_count(), 0)

    def test_bump_accumulates(self):
        self.assertEqual(sms._bump_sms_sent_count(3), 3)
        self.assertEqual(sms._bump_sms_sent_count(2), 5)
        self.assertEqual(sms.load_sms_sent_count(), 5)

    def test_bump_ignores_negative(self):
        sms._bump_sms_sent_count(4)
        self.assertEqual(sms._bump_sms_sent_count(-10), 4)


if __name__ == "__main__":
    unittest.main()
