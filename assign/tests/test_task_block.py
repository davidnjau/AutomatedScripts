#!/usr/bin/env python3
"""
Unit tests for task_block.py — the shared labeled-block renderer and its
per-field builders, used by every task-list report across the bot.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import task_block as tb


class TestFormatLabeledBlock(unittest.TestCase):
    """format_labeled_block — the one shared visual behind every report in
    the bot; only the ref/fields passed in differ per report."""

    def test_renders_numbered_ref_header_and_each_field_indented(self):
        block = tb.format_labeled_block(
            3, "REG/TSFR/ABC123", [("💰 Consideration", "KES 1,000.00"), ("📋 Parcel", "P1")],
        )
        self.assertEqual(
            block,
            "  3. 📌 *Ref:* `REG/TSFR/ABC123`\n"
            "     💰 Consideration: KES 1,000.00\n"
            "     📋 Parcel: P1",
        )

    def test_missing_ref_falls_back_to_em_dash(self):
        block = tb.format_labeled_block(1, "", [])
        self.assertIn("*Ref:* `—`", block)

    def test_no_fields_is_just_the_ref_header(self):
        block = tb.format_labeled_block(1, "REF1", [])
        self.assertEqual(block, "  1. 📌 *Ref:* `REF1`")

    def test_markdown_false_leaves_ref_plain_for_plain_text_email(self):
        block = tb.format_labeled_block(1, "REF1", [], markdown=False)
        self.assertEqual(block, "  1. 📌 Ref: REF1")
        self.assertNotIn("`", block)
        self.assertNotIn("*", block)


class TestFormatConsideration(unittest.TestCase):
    """format_consideration turns a raw amount + currency into 'KES 1,234.00'."""

    def test_formats_amount_with_currency(self):
        self.assertEqual(tb.format_consideration("6000000", "KES"), "KES 6,000,000.00")

    def test_defaults_currency_to_kes_when_missing(self):
        self.assertEqual(tb.format_consideration("100", ""), "KES 100.00")

    def test_empty_amount_returns_empty_string(self):
        self.assertEqual(tb.format_consideration("", "KES"), "")

    def test_non_numeric_amount_falls_back_to_str(self):
        self.assertEqual(tb.format_consideration("N/A", "KES"), "N/A")

    def test_strips_commas_before_parsing(self):
        self.assertEqual(tb.format_consideration("6,000,000", "KES"), "KES 6,000,000.00")

    def test_already_formatted_string_is_returned_unchanged(self):
        """Double-formatting an already-formatted value (e.g. a value from
        an older source) is a harmless no-op, not an error."""
        self.assertEqual(tb.format_consideration("KES 6,000,000.00", ""), "KES 6,000,000.00")


class TestAssessorField(unittest.TestCase):
    def test_returns_assessor_value(self):
        self.assertEqual(tb.assessor_field({"assessor": "Jane Doe"}), ("Assessor", "Jane Doe"))

    def test_missing_falls_back_to_em_dash(self):
        self.assertEqual(tb.assessor_field({}), ("Assessor", "—"))


class TestConsiderationField(unittest.TestCase):
    def test_prefers_consideration_amount_over_consideration(self):
        item = {"consideration_amount": "5000000", "consideration": "1", "currency_code": "KES"}
        self.assertEqual(tb.consideration_field(item), ("💰 Consideration", "KES 5,000,000.00"))

    def test_falls_back_to_consideration_when_no_amount_field(self):
        item = {"consideration": "2000000", "currency_code": "KES"}
        self.assertEqual(tb.consideration_field(item), ("💰 Consideration", "KES 2,000,000.00"))

    def test_missing_falls_back_to_em_dash(self):
        self.assertEqual(tb.consideration_field({}), ("💰 Consideration", "—"))


class TestParcelField(unittest.TestCase):
    def test_prefers_parcel_over_parcel_number(self):
        self.assertEqual(tb.parcel_field({"parcel": "P1", "parcel_number": "P2"}), ("📋 Parcel", "P1"))

    def test_falls_back_to_parcel_number(self):
        self.assertEqual(tb.parcel_field({"parcel_number": "P2"}), ("📋 Parcel", "P2"))

    def test_missing_falls_back_to_em_dash(self):
        self.assertEqual(tb.parcel_field({}), ("📋 Parcel", "—"))


class TestTagField(unittest.TestCase):
    def test_returns_tag_when_present(self):
        self.assertEqual(tb.tag_field({"tag": "Queue"}), ("🏷 Tag", "Queue"))

    def test_returns_none_when_untagged(self):
        self.assertIsNone(tb.tag_field({}))
        self.assertIsNone(tb.tag_field({"tag": ""}))


class TestDefaultTaskFields(unittest.TestCase):
    def test_returns_assessor_consideration_parcel_in_order(self):
        item = {"assessor": "Jane Doe", "consideration": "1000000", "currency_code": "KES", "parcel": "P1"}
        self.assertEqual(
            tb.default_task_fields(item),
            [("Assessor", "Jane Doe"), ("💰 Consideration", "KES 1,000,000.00"), ("📋 Parcel", "P1")],
        )

    def test_empty_item_falls_back_to_em_dashes(self):
        self.assertEqual(
            tb.default_task_fields({}),
            [("Assessor", "—"), ("💰 Consideration", "—"), ("📋 Parcel", "—")],
        )


if __name__ == "__main__":
    unittest.main()
