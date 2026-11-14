#!/usr/bin/env python3
"""
Unit tests for minio_store.py — the graceful-degrading MinIO wrapper.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minio_store as ms


class _Enabled(unittest.TestCase):
    """Base: pretend MinIO is configured and inject a mock client."""

    def setUp(self):
        self._patches = [
            patch.object(ms, "MINIO_ENDPOINT", "minio:9000"),
            patch.object(ms, "MINIO_ACCESS_KEY", "key"),
            patch.object(ms, "MINIO_SECRET_KEY", "secret"),
            patch.object(ms, "_client", None),
            patch.object(ms, "_bucket_ready", False),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = MagicMock()
        self.client.bucket_exists.return_value = True
        self._gc = patch.object(ms, "_get_client", return_value=self.client)
        self._gc.start()
        self.addCleanup(self._gc.stop)


class TestDisabled(unittest.TestCase):
    """With no endpoint/creds, every op is a safe no-op (no minio import)."""

    def setUp(self):
        for attr in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
            p = patch.object(ms, attr, "")
            p.start()
            self.addCleanup(p.stop)

    def test_is_enabled_false(self):
        self.assertFalse(ms.is_enabled())

    def test_put_returns_false(self):
        self.assertFalse(ms.put_screenshot("k", b"x"))

    def test_get_returns_none(self):
        self.assertIsNone(ms.get_screenshot("k"))

    def test_delete_returns_false(self):
        self.assertFalse(ms.delete_screenshot("k"))

    def test_ensure_bucket_false(self):
        self.assertFalse(ms.ensure_bucket())


class TestEnabled(_Enabled):
    def test_is_enabled_true(self):
        self.assertTrue(ms.is_enabled())

    def test_ensure_bucket_creates_when_missing(self):
        self.client.bucket_exists.return_value = False
        self.assertTrue(ms.ensure_bucket())
        self.client.make_bucket.assert_called_once_with(ms.MINIO_BUCKET)

    def test_put_calls_put_object(self):
        ok = ms.put_screenshot("screenshots/a.jpg", b"imgbytes")
        self.assertTrue(ok)
        self.client.put_object.assert_called_once()
        args, kwargs = self.client.put_object.call_args
        self.assertEqual(args[0], ms.MINIO_BUCKET)
        self.assertEqual(args[1], "screenshots/a.jpg")

    def test_get_reads_object(self):
        resp = MagicMock()
        resp.read.return_value = b"data"
        self.client.get_object.return_value = resp
        self.assertEqual(ms.get_screenshot("k"), b"data")
        resp.close.assert_called_once()
        resp.release_conn.assert_called_once()

    def test_delete_calls_remove_object(self):
        self.assertTrue(ms.delete_screenshot("k"))
        self.client.remove_object.assert_called_once_with(ms.MINIO_BUCKET, "k")

    def test_put_swallows_client_error(self):
        self.client.put_object.side_effect = RuntimeError("down")
        self.assertFalse(ms.put_screenshot("k", b"x"))


if __name__ == "__main__":
    unittest.main()
