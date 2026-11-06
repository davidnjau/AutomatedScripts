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
    q.edit_message_reply_markup = AsyncMock()
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

    def test_multiple_templates_spread_randomly(self):
        # Each recipient gets one of the selected templates at random; over
        # many recipients every template appears.
        with patch.dict(os.environ, _ENV, clear=False):
            payload = sms._build_payload([f"25470000{i:04d}" for i in range(40)], ["AAA", "BBB"])
        msgs = {item["message"] for item in payload["smslist"]}
        self.assertTrue(msgs.issubset({"AAA", "BBB"}))   # only the selected templates
        self.assertEqual(msgs, {"AAA", "BBB"})            # both appear across 40 recipients


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
        for bad in ["0", "20001", "abc", "-3"]:
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

    def test_max_count_is_accepted(self):
        update = _make_update_with_message("20000")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        with patch.object(sms, "load_sms_templates", return_value=["T"]):
            result = _run(sms.recv_sms_count(update, ctx))
        self.assertEqual(result, sms.SMS.TEMPLATE)
        self.assertEqual(ctx.user_data["sms_session"].count, 20000)


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

    def test_toggle_selects_and_stays_on_picker(self):
        update = _make_update_with_callback("sms_tpl:0")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        with patch.object(sms, "load_sms_templates", return_value=["Hello:", "Bye:"]):
            result = _run(sms.recv_sms_template(update, ctx))
        self.assertEqual(result, sms.SMS.TEMPLATE)
        self.assertEqual(ctx.user_data["sms_session"].selected, {0})
        update.callback_query.edit_message_reply_markup.assert_called()   # picker re-rendered

    def test_toggle_twice_deselects(self):
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        with patch.object(sms, "load_sms_templates", return_value=["Hello:", "Bye:"]):
            _run(sms.recv_sms_template(_make_update_with_callback("sms_tpl:1"), ctx))
            _run(sms.recv_sms_template(_make_update_with_callback("sms_tpl:1"), ctx))
        self.assertEqual(ctx.user_data["sms_session"].selected, set())

    def test_done_with_selection_resolves_messages_and_confirms(self):
        update = _make_update_with_callback("sms_tpl:done")
        sess = sms.SMSSession(); sess.selected = {0, 2}
        ctx = MagicMock(); ctx.user_data = {"sms_session": sess}
        with patch.object(sms, "load_sms_templates", return_value=["A:", "B:", "C:"]):
            result = _run(sms.recv_sms_template(update, ctx))
        self.assertEqual(result, sms.SMS.CONFIRM)
        self.assertEqual(sess.messages, ["A:", "C:"])            # sorted selected indices
        update.callback_query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)

    def test_done_with_none_selected_reprompts(self):
        update = _make_update_with_callback("sms_tpl:done")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        with patch.object(sms, "load_sms_templates", return_value=["A:", "B:"]):
            result = _run(sms.recv_sms_template(update, ctx))
        self.assertEqual(result, sms.SMS.TEMPLATE)
        self.assertEqual(ctx.user_data["sms_session"].messages, [])
        self.assertIn("at least one", update.callback_query.message.reply_text.call_args[0][0].lower())


class TestRecvSmsTemplateAdd(unittest.TestCase):
    def test_empty_reprompts(self):
        update = _make_update_with_message("   ")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        result = _run(sms.recv_sms_template_add(update, ctx))
        self.assertEqual(result, sms.SMS.TEMPLATE_ADD)

    def test_valid_saves_selects_and_returns_to_picker(self):
        update = _make_update_with_message("New msg:")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession()}
        with patch.object(sms, "load_sms_templates", return_value=["Old:"]), \
             patch.object(sms, "save_sms_templates") as mock_save:
            result = _run(sms.recv_sms_template_add(update, ctx))
        self.assertEqual(result, sms.SMS.TEMPLATE)                # back to multi-select, not confirm
        mock_save.assert_called_once_with(["Old:", "New msg:"])
        self.assertIn(1, ctx.user_data["sms_session"].selected)   # the new template auto-selected


class TestRecvSmsGo(unittest.TestCase):
    def setUp(self):
        # The real-send guard is a module global; reset it so each test starts
        # with no run "in progress".
        sms._sms_run_active = False

    def tearDown(self):
        sms._sms_run_active = False

    def _sess(self, simulate):
        return sms.SMSSession(simulate=simulate, count=3, messages=["ARDHISASA code:"])

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

    def test_real_queues_background_job_and_does_not_post(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(False)}
        with patch.object(sms.requests, "post") as mock_post, \
             patch.dict(os.environ, _ENV, clear=False):
            result = _run(sms.recv_sms_go(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        mock_post.assert_not_called()                          # the handler never sends inline
        ctx.application.job_queue.run_once.assert_called_once()
        kw = ctx.application.job_queue.run_once.call_args.kwargs
        self.assertEqual(kw["name"], "sms_send_run")
        self.assertEqual(kw["data"], {"chat_id": 555, "count": 3, "messages": ["ARDHISASA code:"]})
        self.assertTrue(sms._sms_run_active)                   # guard is now held

    def test_real_blocked_while_a_run_is_active(self):
        sms._sms_run_active = True
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": self._sess(False)}
        with patch.dict(os.environ, _ENV, clear=False):
            result = _run(sms.recv_sms_go(update, ctx))
        self.assertEqual(result, sms.ConversationHandler.END)
        ctx.application.job_queue.run_once.assert_not_called()
        self.assertIn("in progress", update.callback_query.edit_message_text.call_args[0][0].lower())


class TestSmsSendJob(unittest.TestCase):
    def setUp(self):
        sms._sms_run_active = True   # the handler would have set this before scheduling

    def tearDown(self):
        sms._sms_run_active = False

    def _ctx(self, count, message):
        # `message` may be a single template string or a list of templates.
        messages = [message] if isinstance(message, str) else message
        ctx = MagicMock()
        ctx.job.data = {"chat_id": 555, "count": count, "messages": messages}
        ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        ctx.bot.edit_message_text = AsyncMock()
        return ctx

    def test_posts_correct_body_bumps_count_and_clears_guard(self):
        ctx = self._ctx(3, "ARDHISASA code:")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"responses": [
            {"response-code": 200, "mobile": "254700000001", "messageid": "1"},
            {"response-code": 200, "mobile": "254700000002", "messageid": "2"},
            {"response-code": 200, "mobile": "254700000001", "messageid": "3"},
        ]}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms.requests, "post", return_value=resp) as mock_post, \
             patch.object(sms, "_bump_sms_sent_count", return_value=103) as mock_bump, \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms._sms_send_job(ctx))
        self.assertEqual(mock_post.call_count, 1)              # 3 ≤ chunk size → one POST
        self.assertEqual(mock_post.call_args.args[0], sms.SMS_SEND_URL)
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["smslist"][0]["partnerID"], "10709")
        self.assertEqual(body["smslist"][0]["shortcode"], "Text")
        mock_bump.assert_called_once_with(3)                   # all 3 accepted
        final = " ".join(c.args[1] for c in ctx.bot.send_message.call_args_list)
        self.assertIn("3 message(s) success", final)
        self.assertIn("Total texts sent to date: 103", final)
        self.assertFalse(sms._sms_run_active)                  # guard released

    def test_chunks_large_run_into_multiple_posts(self):
        ctx = self._ctx(5, "hi")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"responses": [{"response-code": 200, "mobile": "x", "messageid": "1"}]}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms, "_SMS_CHUNK_SIZE", 2), \
             patch.object(sms, "_SMS_CHUNK_DELAY", 0), \
             patch.object(sms.requests, "post", return_value=resp) as mock_post, \
             patch.object(sms, "_bump_sms_sent_count", return_value=5), \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms._sms_send_job(ctx))
        self.assertEqual(mock_post.call_count, 3)              # 5 over size-2 chunks → 3 POSTs
        sizes = [len(c.kwargs["json"]["smslist"]) for c in mock_post.call_args_list]
        self.assertEqual(sizes, [2, 2, 1])

    def test_uses_reference_pool(self):
        ctx = self._ctx(2, "ref {ref}")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"responses": [{"response-code": 200, "mobile": "x", "messageid": "1"}]}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=["REG/TSFR/AAA", "CNTYINV/BBB"]), \
             patch.object(sms.requests, "post", return_value=resp) as mock_post, \
             patch.object(sms, "_bump_sms_sent_count", return_value=2), \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms._sms_send_job(ctx))
        msgs = [i["message"] for i in mock_post.call_args.kwargs["json"]["smslist"]]
        self.assertEqual(msgs, ["ref REG/TSFR/AAA", "ref CNTYINV/BBB"])

    def test_multiple_templates_randomised_across_recipients(self):
        ctx = self._ctx(40, ["AAA", "BBB"])   # two templates, no placeholders
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"responses": [{"response-code": 200, "mobile": "x", "messageid": "1"}]}
        with patch.object(sms, "load_sms_test_numbers", return_value=[f"25470000{i:04d}" for i in range(40)]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms.requests, "post", return_value=resp) as mock_post, \
             patch.object(sms, "_bump_sms_sent_count", return_value=40), \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms._sms_send_job(ctx))
        msgs = {i["message"] for i in mock_post.call_args.kwargs["json"]["smslist"]}
        self.assertEqual(msgs, {"AAA", "BBB"})   # both templates spread across the 40 recipients

    def test_counts_only_accepted(self):
        ctx = self._ctx(3, "hi")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"responses": [
            {"response-code": 200, "mobile": "a", "messageid": "1"},
            {"response-code": 1006, "mobile": "b", "messageid": None},
            {"response-code": 200, "mobile": "c", "messageid": "3"},
        ]}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms.requests, "post", return_value=resp), \
             patch.object(sms, "_bump_sms_sent_count", return_value=2) as mock_bump, \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms._sms_send_job(ctx))
        mock_bump.assert_called_once_with(2)                   # 2 of 3 accepted

    def test_chunk_failure_is_counted_not_raised_and_clears_guard(self):
        ctx = self._ctx(2, "hi")
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001", "254700000002"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.object(sms.requests, "post", side_effect=RuntimeError("gateway down")), \
             patch.object(sms, "_bump_sms_sent_count", return_value=0) as mock_bump, \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms._sms_send_job(ctx))
        mock_bump.assert_called_once_with(0)                   # nothing accepted
        final = " ".join(c.args[1] for c in ctx.bot.send_message.call_args_list)
        self.assertIn("2 message(s) failed", final)
        self.assertFalse(sms._sms_run_active)                  # guard released even on failure


class TestChunkRecipients(unittest.TestCase):
    def test_even_split_preserves_order_and_ref_alignment(self):
        nums = ["n1", "n2", "n3", "n4", "n5"]
        refs = ["r1", "r2", "r3", "r4", "r5"]
        chunks = sms._chunk_recipients(nums, refs, 2)
        self.assertEqual(chunks, [
            (["n1", "n2"], ["r1", "r2"]),
            (["n3", "n4"], ["r3", "r4"]),
            (["n5"], ["r5"]),
        ])

    def test_refs_none_yields_none_per_chunk(self):
        chunks = sms._chunk_recipients(["n1", "n2", "n3"], None, 2)
        self.assertEqual(chunks, [(["n1", "n2"], None), (["n3"], None)])

    def test_empty_input(self):
        self.assertEqual(sms._chunk_recipients([], None, 1000), [])

    def test_count_matches_ceil_division(self):
        chunks = sms._chunk_recipients([str(i) for i in range(20000)], None, 1000)
        self.assertEqual(len(chunks), 20)
        self.assertTrue(all(len(c[0]) == 1000 for c in chunks))


class TestReplyMarkupNotOnEdit(unittest.TestCase):
    # Regression: edit_message_text only accepts inline keyboards, so the main
    # menu (a reply keyboard) must never be passed to it — doing so raised a
    # live "telegram.error.BadRequest: Inline keyboard expected". The menu is
    # restored with a follow-up reply_text instead.
    def setUp(self):
        sms._sms_run_active = False

    def tearDown(self):
        sms._sms_run_active = False

    def _assert_clean(self, update):
        for call in update.callback_query.edit_message_text.call_args_list:
            self.assertIsNone(
                call.kwargs.get("reply_markup"),
                "edit_message_text must never carry a reply keyboard",
            )
        update.callback_query.message.reply_text.assert_called()   # menu restored separately

    def test_simulate_restores_menu_via_reply_text(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession(simulate=True, count=2, messages=["hi"])}
        with patch.object(sms, "load_sms_test_numbers", return_value=["254700000001"]), \
             patch.object(sms, "load_sms_ref_numbers", return_value=[]), \
             patch.dict(os.environ, _ENV, clear=False):
            _run(sms.recv_sms_go(update, ctx))
        self._assert_clean(update)

    def test_real_queue_restores_menu_via_reply_text(self):
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession(simulate=False, count=2, messages=["hi"])}
        with patch.object(sms.requests, "post"), patch.dict(os.environ, _ENV, clear=False):
            _run(sms.recv_sms_go(update, ctx))
        self._assert_clean(update)

    def test_blocked_run_restores_menu_via_reply_text(self):
        sms._sms_run_active = True
        update = _make_update_with_callback("sms_go:yes")
        ctx = MagicMock(); ctx.user_data = {"sms_session": sms.SMSSession(simulate=False, count=2, messages=["hi"])}
        with patch.dict(os.environ, _ENV, clear=False):
            _run(sms.recv_sms_go(update, ctx))
        self._assert_clean(update)


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
