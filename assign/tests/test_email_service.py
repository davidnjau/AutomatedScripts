#!/usr/bin/env python3
"""
Unit tests for email_service.py — the shared SMTP email sender used by
Bulk Export, DLV Tasks, Morning Briefing (attachment email) and Auto
Fetch (plain+HTML email).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import email_service as es


class TestSendBulkExportEmail(unittest.TestCase):
    def test_raises_without_smtp_credentials(self):
        with patch.object(es, "SMTP_USER", ""), patch.object(es, "SMTP_PASS", ""):
            with self.assertRaises(RuntimeError):
                es._send_bulk_export_email("to@example.com", "file.xlsx", b"data")

    def test_sends_with_attachment_and_expected_subject(self):
        fake_server = MagicMock()
        fake_smtp_cm = MagicMock()
        fake_smtp_cm.__enter__.return_value = fake_server
        with patch.object(es, "SMTP_USER", "bot@example.com"), \
             patch.object(es, "SMTP_PASS", "secret"), \
             patch.object(es.smtplib, "SMTP", return_value=fake_smtp_cm) as mock_smtp:
            es._send_bulk_export_email("to@example.com", "report.xlsx", b"xlsx-bytes")

        mock_smtp.assert_called_once_with(es.SMTP_HOST, es.SMTP_PORT)
        fake_server.ehlo.assert_called_once()
        fake_server.starttls.assert_called_once()
        fake_server.login.assert_called_once_with("bot@example.com", "secret")
        self.assertEqual(fake_server.sendmail.call_count, 1)
        from_addr, to_addr, raw_msg = fake_server.sendmail.call_args[0]
        self.assertEqual(from_addr, "bot@example.com")
        self.assertEqual(to_addr, "to@example.com")
        self.assertIn("report.xlsx", raw_msg)
        self.assertIn("attachment", raw_msg)

    def test_propagates_smtp_errors(self):
        with patch.object(es, "SMTP_USER", "bot@example.com"), \
             patch.object(es, "SMTP_PASS", "secret"), \
             patch.object(es.smtplib, "SMTP", side_effect=OSError("connection refused")):
            with self.assertRaises(OSError):
                es._send_bulk_export_email("to@example.com", "file.xlsx", b"data")


class TestSendAutoFetchEmail(unittest.TestCase):
    def test_raises_without_smtp_credentials(self):
        with patch.object(es, "SMTP_USER", ""), patch.object(es, "SMTP_PASS", ""):
            with self.assertRaises(RuntimeError):
                es._send_auto_fetch_email("to@example.com", "Subject", "body text")

    def test_sends_plain_and_html_parts(self):
        fake_server = MagicMock()
        fake_smtp_cm = MagicMock()
        fake_smtp_cm.__enter__.return_value = fake_server
        with patch.object(es, "SMTP_USER", "bot@example.com"), \
             patch.object(es, "SMTP_PASS", "secret"), \
             patch.object(es.smtplib, "SMTP", return_value=fake_smtp_cm):
            es._send_auto_fetch_email("to@example.com", "Auto Fetch Results", "3 tasks found")

        raw_msg = fake_server.sendmail.call_args[0][2]
        self.assertIn("Auto Fetch Results", raw_msg)
        self.assertIn("multipart/alternative", raw_msg)

    def test_html_body_escapes_special_characters(self):
        fake_server = MagicMock()
        fake_smtp_cm = MagicMock()
        fake_smtp_cm.__enter__.return_value = fake_server
        with patch.object(es, "SMTP_USER", "bot@example.com"), \
             patch.object(es, "SMTP_PASS", "secret"), \
             patch.object(es.smtplib, "SMTP", return_value=fake_smtp_cm):
            es._send_auto_fetch_email("to@example.com", "Subject", "a < b & c")

        raw_msg = fake_server.sendmail.call_args[0][2]
        self.assertIn("&lt;", raw_msg)
        self.assertIn("&amp;", raw_msg)


if __name__ == "__main__":
    unittest.main()
