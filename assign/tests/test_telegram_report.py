#!/usr/bin/env python3
"""
Unit tests for telegram_report.py — the shared "chunk lines into
Telegram-sized messages and send them" helper used by DLV Tasks and
Fetch Tasks (and any future feature that displays a paginated report).

Run with: python3 -m unittest discover -s assign/tests -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_report as tr


def _run(coro):
    return asyncio.run(coro)


class TestChunkLines(unittest.TestCase):
    def test_empty_list_returns_empty(self):
        self.assertEqual(tr._chunk_lines([]), [])

    def test_single_short_line_is_one_chunk(self):
        self.assertEqual(tr._chunk_lines(["hello"]), ["hello"])

    def test_multiple_short_lines_join_into_one_chunk(self):
        chunks = tr._chunk_lines(["a", "b", "c"], join="\n")
        self.assertEqual(chunks, ["a\nb\nc"])

    def test_default_join_is_double_newline(self):
        chunks = tr._chunk_lines(["a", "b"])
        self.assertEqual(chunks, ["a\n\nb"])

    def test_splits_when_threshold_exceeded(self):
        # Each line is 10 chars; threshold 15 forces a new chunk every line
        # since "line1\nline2" (11 chars) already exceeds 10... use a tight threshold.
        lines = ["1234567890", "abcdefghij"]
        chunks = tr._chunk_lines(lines, join="\n", threshold=10)
        self.assertEqual(chunks, ["1234567890", "abcdefghij"])

    def test_packs_as_many_lines_as_fit_before_splitting(self):
        lines = ["aaa", "bbb", "ccc"]
        # "aaa\nbbb" = 7 chars fits under 8; adding "\nccc" (11) would exceed 8
        chunks = tr._chunk_lines(lines, join="\n", threshold=8)
        self.assertEqual(chunks, ["aaa\nbbb", "ccc"])

    def test_single_oversized_line_becomes_its_own_chunk(self):
        long_line = "x" * 5000
        chunks = tr._chunk_lines(["short", long_line], threshold=4000)
        self.assertEqual(chunks, ["short", long_line])

    def test_many_lines_produce_multiple_chunks(self):
        lines = [f"line-{i}" for i in range(2000)]
        chunks = tr._chunk_lines(lines, join="\n", threshold=100)
        self.assertGreater(len(chunks), 1)
        # every original line must appear somewhere across the chunks
        joined = "\n".join(chunks)
        for line in lines:
            self.assertIn(line, joined)
        # no chunk exceeds the threshold
        for c in chunks:
            self.assertLessEqual(len(c), 100)


class TestSendChunkedReport(unittest.TestCase):
    def setUp(self):
        self.calls = []

    async def _record(self, text, reply_markup):
        self.calls.append((text, reply_markup))

    def test_no_lines_sends_nothing(self):
        _run(tr._send_chunked_report(self._record, []))
        self.assertEqual(self.calls, [])

    def test_single_chunk_gets_footer_and_reply_markup(self):
        _run(tr._send_chunked_report(
            self._record, ["a", "b"], footer="\nEND", reply_markup="menu",
        ))
        self.assertEqual(len(self.calls), 1)
        text, reply_markup = self.calls[0]
        self.assertTrue(text.endswith("\nEND"))
        self.assertEqual(reply_markup, "menu")

    def test_multiple_chunks_only_last_gets_footer_and_markup(self):
        lines = [f"line-{i}" for i in range(50)]
        _run(tr._send_chunked_report(
            self._record, lines, join="\n", threshold=30,
            footer="\nDONE", reply_markup="menu",
        ))
        self.assertGreater(len(self.calls), 1)
        for text, reply_markup in self.calls[:-1]:
            self.assertNotIn("DONE", text)
            self.assertIsNone(reply_markup)
        last_text, last_markup = self.calls[-1]
        self.assertTrue(last_text.endswith("\nDONE"))
        self.assertEqual(last_markup, "menu")

    def test_join_and_threshold_are_forwarded_to_chunker(self):
        lines = ["aaa", "bbb", "ccc"]
        _run(tr._send_chunked_report(self._record, lines, join="\n", threshold=8))
        texts = [c[0] for c in self.calls]
        self.assertEqual(texts, ["aaa\nbbb", "ccc"])


if __name__ == "__main__":
    unittest.main()
