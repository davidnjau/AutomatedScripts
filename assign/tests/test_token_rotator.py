#!/usr/bin/env python3
"""
Unit tests for token_rotator.py — _TokenRotator's rotation/exhaustion logic,
including the double-skip protection when two threads hit 403 on the same
token concurrently, and fetch_with_rotation's shared rotate-on-403/
retry-on-5xx GET loop (the logic Valuer Tasks and Job Distribution used to
each duplicate).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from token_rotator import _AllTokensExhausted, _TokenRotator, fetch_with_rotation

_HEADERS_FN = lambda tokens: {"Authorization": f"Bearer {tokens}"}


class TestTokenRotator(unittest.TestCase):
    def test_current_returns_first_token(self):
        rotator = _TokenRotator([("staff", "tok1"), ("staff2", "tok2")])
        self.assertEqual(rotator.current(), "tok1")

    def test_current_label_uses_cred_labels(self):
        rotator = _TokenRotator([("staff", "tok1")])
        self.assertEqual(rotator.current_label(), "🏢 ICT")

    def test_current_label_falls_back_to_key_when_unknown(self):
        rotator = _TokenRotator([("mystery", "tok1")])
        self.assertEqual(rotator.current_label(), "mystery")

    def test_rotate_advances_past_failed_token(self):
        rotator = _TokenRotator([("staff", "tok1"), ("staff2", "tok2")])
        new_tok = rotator.rotate("tok1")
        self.assertEqual(new_tok, "tok2")
        self.assertEqual(rotator.current(), "tok2")

    def test_rotate_ignores_stale_failure(self):
        """If `failed` is no longer the current token, don't double-advance."""
        rotator = _TokenRotator([("staff", "tok1"), ("staff2", "tok2"), ("staff_valuer", "tok3")])
        rotator.rotate("tok1")   # advances to tok2
        result = rotator.rotate("tok1")   # stale — tok1 is no longer current
        self.assertEqual(result, "tok2")
        self.assertEqual(rotator.current(), "tok2")

    def test_exhausted_after_last_token_fails(self):
        rotator = _TokenRotator([("staff", "tok1")])
        result = rotator.rotate("tok1")
        self.assertIsNone(result)
        self.assertTrue(rotator.exhausted)

    def test_not_exhausted_while_tokens_remain(self):
        rotator = _TokenRotator([("staff", "tok1"), ("staff2", "tok2")])
        self.assertFalse(rotator.exhausted)

    def test_current_returns_none_when_exhausted(self):
        rotator = _TokenRotator([("staff", "tok1")])
        rotator.rotate("tok1")
        self.assertIsNone(rotator.current())

    def test_all_tokens_exhausted_is_an_exception(self):
        self.assertTrue(issubclass(_AllTokensExhausted, Exception))


class TestFetchWithRotation(unittest.TestCase):
    def test_success_returns_json(self):
        rotator = _TokenRotator([("staff", "tok1")])
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(
            status_code=200, raise_for_status=lambda: None, json=lambda: {"ok": True}
        )
        result = fetch_with_rotation(fake_session, rotator, "http://x", {"id": "1"}, _HEADERS_FN)
        self.assertEqual(result, {"ok": True})

    def test_all_tokens_exhausted_raises(self):
        rotator = _TokenRotator([])
        fake_session = MagicMock()
        with self.assertRaises(_AllTokensExhausted):
            fetch_with_rotation(fake_session, rotator, "http://x", {}, _HEADERS_FN)

    def test_403_rotates_to_next_credential_then_succeeds(self):
        rotator = _TokenRotator([("staff", "tok1"), ("staff2", "tok2")])
        fake_session = MagicMock()
        fake_session.get.side_effect = [
            MagicMock(status_code=403),
            MagicMock(status_code=200, raise_for_status=lambda: None, json=lambda: {"ok": True}),
        ]
        with patch("token_rotator.time.sleep"):
            result = fetch_with_rotation(fake_session, rotator, "http://x", {}, _HEADERS_FN, rotate_delay=1)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(rotator.current(), "tok2")

    def test_403_on_last_credential_raises_all_tokens_exhausted(self):
        rotator = _TokenRotator([("staff", "tok1")])
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(status_code=403)
        with patch("token_rotator.time.sleep"):
            with self.assertRaises(_AllTokensExhausted):
                fetch_with_rotation(fake_session, rotator, "http://x", {}, _HEADERS_FN)

    def test_exhausts_retries_on_persistent_5xx(self):
        rotator = _TokenRotator([("staff", "tok1")])
        fake_session = MagicMock()
        fake_session.get.return_value = MagicMock(status_code=503)
        with patch("token_rotator.time.sleep"):
            with self.assertRaises(RuntimeError):
                fetch_with_rotation(fake_session, rotator, "http://x", {}, _HEADERS_FN, max_retries=3)
        self.assertEqual(fake_session.get.call_count, 3)

    def test_context_appears_in_exhausted_error_message(self):
        rotator = _TokenRotator([])
        fake_session = MagicMock()
        with self.assertRaisesRegex(_AllTokensExhausted, "task abc123"):
            fetch_with_rotation(fake_session, rotator, "http://x", {}, _HEADERS_FN, context="task abc123")


if __name__ == "__main__":
    unittest.main()
