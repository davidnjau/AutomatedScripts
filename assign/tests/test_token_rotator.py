#!/usr/bin/env python3
"""
Unit tests for token_rotator.py — _TokenRotator's rotation/exhaustion logic,
including the double-skip protection when two threads hit 403 on the same
token concurrently.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from token_rotator import _AllTokensExhausted, _TokenRotator


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


if __name__ == "__main__":
    unittest.main()
