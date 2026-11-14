#!/usr/bin/env python3
"""
Unit tests for blob_store.py — local-filesystem screenshot storage.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blob_store as bs


class TestBlobStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._p = patch.object(bs, "_ROOT", self.tmpdir.name)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def test_put_then_get_roundtrip(self):
        self.assertTrue(bs.put_screenshot("screenshots/a.jpg", b"imgbytes"))
        self.assertEqual(bs.get_screenshot("screenshots/a.jpg"), b"imgbytes")

    def test_put_creates_nested_dirs(self):
        bs.put_screenshot("screenshots/x.jpg", b"x")
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir.name, "screenshots", "x.jpg")))

    def test_get_missing_returns_none(self):
        self.assertIsNone(bs.get_screenshot("screenshots/nope.jpg"))

    def test_delete_removes_file(self):
        bs.put_screenshot("screenshots/a.jpg", b"x")
        self.assertTrue(bs.delete_screenshot("screenshots/a.jpg"))
        self.assertIsNone(bs.get_screenshot("screenshots/a.jpg"))

    def test_delete_missing_is_success(self):
        # Already-gone is the desired end state.
        self.assertTrue(bs.delete_screenshot("screenshots/gone.jpg"))

    def test_key_cannot_escape_root(self):
        # A traversal key must not write outside the blob root.
        self.assertFalse(bs.put_screenshot("../escape.jpg", b"x"))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmpdir.name), "escape.jpg")))

    def test_put_swallows_write_error(self):
        with patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(bs.put_screenshot("screenshots/a.jpg", b"x"))


if __name__ == "__main__":
    unittest.main()
