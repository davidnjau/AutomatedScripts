#!/usr/bin/env python3
"""
Unit tests for refresh_auth.py — the standalone "manually (re)authenticate
a credential profile" flow: cached-token short-circuit, force-confirm
branch, the login/OTP trigger, and OTP verification.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import refresh_auth as ra
from ardhisasa_auth import AuthTokens

TOKENS = AuthTokens(access_token="acc", jwt="jwt")


def _run(coro):
    return asyncio.run(coro)


def _make_update_with_callback(data):
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.reply_text = AsyncMock()
    return update


class TestGetAuthSess(unittest.TestCase):
    def test_creates_and_reuses_session(self):
        ctx = MagicMock()
        ctx.user_data = {}
        sess1 = ra._get_auth_sess(ctx)
        sess2 = ra._get_auth_sess(ctx)
        self.assertIs(sess1, sess2)
        self.assertIsInstance(sess1, ra.AuthSession)


class TestRecvAuthCred(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.user_data = {}

    def test_cancel_ends_conversation(self):
        update = _make_update_with_callback("auth_cred:cancel")
        result = _run(ra.recv_auth_cred(update, self.ctx))
        self.assertEqual(result, ra.ConversationHandler.END)
        update.callback_query.edit_message_text.assert_called_once()

    def test_cached_valid_tokens_ask_force_confirm(self):
        update = _make_update_with_callback("auth_cred:staff")
        with patch.object(ra, "get_valid_tokens", return_value=TOKENS), \
             patch.object(ra, "_load_tokens_raw", return_value={"staff": {"expires_at": 0}}):
            result = _run(ra.recv_auth_cred(update, self.ctx))
        self.assertEqual(result, ra.AS.FORCE_CONFIRM)
        sess = ra._get_auth_sess(self.ctx)
        self.assertEqual(sess.cred_type, "staff")

    def test_no_cached_tokens_triggers_login(self):
        update = _make_update_with_callback("auth_cred:staff2")
        with patch.object(ra, "get_valid_tokens", return_value=None), \
             patch.object(ra, "_auth_trigger_login", new_callable=AsyncMock) as mock_login:
            mock_login.return_value = ra.AS.WAIT_OTP
            result = _run(ra.recv_auth_cred(update, self.ctx))
        mock_login.assert_called_once()
        self.assertEqual(result, ra.AS.WAIT_OTP)


class TestRecvAuthForce(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.user_data = {"auth_session": ra.AuthSession(cred_type="staff")}

    def test_no_keeps_existing_tokens(self):
        update = _make_update_with_callback("auth_force:no")
        result = _run(ra.recv_auth_force(update, self.ctx))
        self.assertEqual(result, ra.ConversationHandler.END)
        update.callback_query.edit_message_text.assert_called_once()

    def test_yes_triggers_login(self):
        update = _make_update_with_callback("auth_force:yes")
        with patch.object(ra, "_auth_trigger_login", new_callable=AsyncMock) as mock_login:
            mock_login.return_value = ra.AS.WAIT_OTP
            result = _run(ra.recv_auth_force(update, self.ctx))
        mock_login.assert_called_once()
        self.assertEqual(result, ra.AS.WAIT_OTP)


class TestAuthTriggerLogin(unittest.TestCase):
    def setUp(self):
        self.query = MagicMock()
        self.query.edit_message_text = AsyncMock()
        self.query.message.reply_text = AsyncMock()
        self.auth_sess = ra.AuthSession(cred_type="staff")

    def test_successful_login_transitions_to_wait_otp(self):
        fake_session = MagicMock()
        fake_session.post.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"success": True},
        )
        with patch.object(ra, "build_session", return_value=fake_session):
            result = _run(ra._auth_trigger_login(self.query, self.auth_sess))
        self.assertEqual(result, ra.AS.WAIT_OTP)
        fake_session.post.assert_called_once()

    def test_login_failure_ends_conversation(self):
        fake_session = MagicMock()
        fake_session.post.side_effect = RuntimeError("network down")
        with patch.object(ra, "build_session", return_value=fake_session):
            result = _run(ra._auth_trigger_login(self.query, self.auth_sess))
        self.assertEqual(result, ra.ConversationHandler.END)

    def test_api_error_response_ends_conversation(self):
        fake_session = MagicMock()
        fake_session.post.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"success": False, "error": "bad credentials"},
        )
        with patch.object(ra, "build_session", return_value=fake_session):
            result = _run(ra._auth_trigger_login(self.query, self.auth_sess))
        self.assertEqual(result, ra.ConversationHandler.END)


class TestRecvAuthOtp(unittest.TestCase):
    def setUp(self):
        self.update = MagicMock()
        self.update.message.text = "123456"
        self.update.message.reply_text = AsyncMock()
        self.ctx = MagicMock()
        self.auth_sess = ra.AuthSession(cred_type="staff", http_session=MagicMock())
        self.ctx.user_data = {"auth_session": self.auth_sess}

    def test_successful_otp_persists_tokens_and_ends(self):
        self.auth_sess.http_session.post.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"details": {"access_token": "acc", "jwt": "jwt", "refresh_token": "r"}},
        )
        with patch.object(ra, "persist_tokens") as mock_persist, \
             patch.object(ra, "_jwt_exp", return_value=0):
            result = _run(ra.recv_auth_otp(self.update, self.ctx))
        mock_persist.assert_called_once_with("staff", "acc", "jwt", "r")
        self.assertEqual(result, ra.ConversationHandler.END)

    def test_missing_tokens_in_response_retries_otp(self):
        self.auth_sess.http_session.post.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"details": {}},
        )
        result = _run(ra.recv_auth_otp(self.update, self.ctx))
        self.assertEqual(result, ra.AS.WAIT_OTP)

    def test_verification_exception_retries_otp(self):
        self.auth_sess.http_session.post.side_effect = RuntimeError("boom")
        result = _run(ra.recv_auth_otp(self.update, self.ctx))
        self.assertEqual(result, ra.AS.WAIT_OTP)


if __name__ == "__main__":
    unittest.main()
