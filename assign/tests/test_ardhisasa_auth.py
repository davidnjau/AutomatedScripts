#!/usr/bin/env python3
"""
Unit tests for ardhisasa_auth.py's system_login() — the OTP-free machine
login (OAuth2 password grant) used for unattended re-authentication.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ardhisasa_auth as aa

_CREDS = {"username": "sv", "password": "pw", "usertype": "staff"}


def _resp(status=200, json_body=None):
    r = MagicMock()
    r.status_code = status
    r.raise_for_status = MagicMock()
    r.json.return_value = json_body or {}
    return r


class TestSystemLogin(unittest.TestCase):
    def test_returns_tokens_and_refresh_on_success(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200, {"details": {
            "access_token": "AT", "jwt": "JWT", "refresh_token": "RT",
            "expires_in": 3600, "token_type": "Bearer",
        }})
        tokens, refresh = aa.system_login(sess, _CREDS)
        self.assertEqual(tokens.access_token, "AT")
        self.assertEqual(tokens.jwt, "JWT")
        self.assertEqual(refresh, "RT")

    def test_posts_username_password_usertype_no_otp(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200, {"details": {"access_token": "AT", "jwt": "JWT"}})
        aa.system_login(sess, _CREDS)
        url, kwargs = sess.post.call_args[0], sess.post.call_args[1]
        self.assertEqual(url[0], aa.AUTH_SYSTEM_LOGIN_URL)
        body = kwargs["json"]
        self.assertEqual(set(body.keys()), {"username", "password", "usertype"})
        self.assertNotIn("otpcode", body)   # no OTP in the system-login flow

    def test_missing_refresh_token_is_empty_string(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200, {"details": {"access_token": "AT", "jwt": "JWT"}})
        _tokens, refresh = aa.system_login(sess, _CREDS)
        self.assertEqual(refresh, "")

    def test_2xx_without_tokens_raises(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200, {"details": {"foo": "bar"}})
        with self.assertRaises(RuntimeError):
            aa.system_login(sess, _CREDS)

    def test_http_error_propagates(self):
        sess = MagicMock()
        bad = _resp(403)
        bad.raise_for_status.side_effect = aa.requests.HTTPError("403")
        sess.post.return_value = bad
        with self.assertRaises(aa.requests.HTTPError):
            aa.system_login(sess, _CREDS)


if __name__ == "__main__":
    unittest.main()
