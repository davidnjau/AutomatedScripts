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

    def test_ref_placeholder_becomes_ten_char_token(self):
        out = sms._render_message("REG/TSFR/{ref} ok")
        self.assertRegex(out, r"^REG/TSFR/[A-Z0-9]{10} ok$")

    def test_no_placeholder_left_after_render(self):
        out = sms._render_message("a {ref} b {code} c {ref}")
        self.assertNotIn("{ref}", out)
        self.assertNotIn("{code}", out)

    def test_plain_text_unchanged(self):
        self.assertEqual(sms._render_message("no placeholders here"), "no placeholders here")


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


class TestRedact(unittest.TestCase):
    def test_masks_every_apikey(self):
        payload = {"count": 2, "smslist": [{"apikey": "secret", "mobile": "x"},
                                           {"apikey": "secret", "mobile": "y"}]}
        red = sms._redact(payload)
        self.assertTrue(all(i["apikey"] == "***" for i in red["smslist"]))
        # original untouched
        self.assertEqual(payload["smslist"][0]["apikey"], "secret")


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._p1 = patch.object(sms, "SAVED_SMS_TEMPLATES_FILE", os.path.join(self.tmp.name, "t.json"))
        self._p2 = patch.object(sms, "SAVED_SMS_TEST_NUMBERS_FILE", os.path.join(self.tmp.name, "n.json"))
        self._p1.start(); self._p2.start()
        self.addCleanup(self._p1.stop); self.addCleanup(self._p2.stop)

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
             patch.object(sms.requests, "post") as mock_post, \
             patch.dict(os.environ, _ENV, clear=False):
            result = _run(sms.recv_sms_go(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        mock_post.assert_not_called()

    def test_simulate_preview_hides_apikey(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(True)}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001"]), \
             patch.object(sms.requests, "post"), \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms.recv_sms_go(update, ctx))
        sent = " ".join(c.args[0] for c in update.callback_query.message.reply_text.call_args_list)
        self.assertNotIn("secret-key", sent)

    def test_real_posts_correct_body(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(False)}
        resp = MagicMock(status_code=200, text='{"responses":[]}')
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms.requests, "post", return_value=resp) as mock_post, \
             patch.dict(os.environ, _ENV, clear=False):
            result = _run(sms.recv_sms_go(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.args[0], sms.SMS_SEND_URL)
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["smslist"]), 3)
        self.assertEqual(body["smslist"][0]["partnerID"], "10709")
        self.assertEqual(body["smslist"][0]["shortcode"], "Text")

    def test_real_send_failure_is_reported_not_raised(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(False)}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001"]), \
             patch.object(sms.requests, "post", side_effect=RuntimeError("gateway down")), \
             patch.dict(os.environ, _ENV, clear=False):
            result = _run(sms.recv_sms_go(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        sent = " ".join(c.args[0] for c in update.callback_query.message.reply_text.call_args_list)
        self.assertIn("failed", sent.lower())


if __name__ == "__main__":
    unittest.main()
