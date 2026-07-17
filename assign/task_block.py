#!/usr/bin/env python3
"""
task_block.py
==============
Shared task-block renderer — the one labeled, numbered multi-line visual
used by every task-list report across the bot (DLV Tasks' Open/Closed/By
Valuer/By Tag, Fetch Tasks' own Telegram view, Auto Fetch's email body): a
bold/backticked Ref header, then one indented "label: value" line per
field.

format_labeled_block() is the shared layout; everything else here is a
small library of (label, value) field builders for the handful of keys
nearly every report shares (Assessor, Consideration, Parcel, Tag) — each
report calls whichever of these it needs and extends the resulting list
with its own report-specific fields (Status/Node/Registry/County for DLV
Tasks' Open view, Source/Registry/County/Added for Fetch Tasks/Auto Fetch,
Valuer/Queued-or-Closed for DLV Tasks' By Valuer/By Tag/Closed), in
whatever order makes sense for that report. Only the keys/values differ
per report; the block layout itself never does.

Not used for Excel exports (Bulk Export, DLV Tasks' emailed Excel, Morning
Briefing) — those are spreadsheet rows, a different medium entirely — or
the compact 🔍 DLV Queue viewer, which is a lightweight status list rather
than a full task report.
"""

from typing import Dict, List, Optional, Tuple

from common import md_escape


def format_labeled_block(i: int, ref: str, fields: List[Tuple[str, str]], markdown: bool = True) -> str:
    """Render one item as a numbered, labeled multi-line block: a bold/
    backticked Ref header (tap-to-copy in Telegram), then each (label,
    value) pair on its own indented line. `fields` values are used as-is
    content-wise — callers are responsible for their own "—" fallback and
    formatting — but each value is Markdown-escaped (md_escape) before
    interpolation, since assessor/valuer/parcel names come from user input
    or an external API and an unescaped '_'/'*' crashes the whole send with
    telegram.error.BadRequest. markdown=False (Auto Fetch's plain-text
    email) skips escaping and leaves the ref unadorned, since email doesn't
    parse Markdown."""
    if markdown:
        ref_line = f"  {i}. 📌 *Ref:* `{ref or '—'}`"
        lines = [ref_line]
        lines += [f"     {label}: {md_escape(str(value))}" for label, value in fields]
    else:
        ref_line = f"  {i}. 📌 Ref: {ref or '—'}"
        lines = [ref_line]
        lines += [f"     {label}: {value}" for label, value in fields]
    return "\n".join(lines)


def format_consideration(amount, currency: str = "") -> str:
    """Format a raw consideration amount + currency code as 'KES
    6,000,000.00'. Returns "" if amount is empty/falsy; returns the raw
    value unchanged (stringified) if it doesn't parse as a number — e.g.
    it's already a formatted string like 'KES 1,234.00' from an older
    source, so double-formatting is a harmless no-op rather than an error."""
    if not amount:
        return ""
    try:
        return f"{currency or 'KES'} {float(str(amount).replace(',', '').strip()):,.2f}"
    except (ValueError, TypeError):
        return str(amount)


def assessor_field(item: Dict) -> Tuple[str, str]:
    """(label, value) for an item's assessor, "—" if unknown."""
    return ("Assessor", item.get("assessor") or "—")


def consideration_field(item: Dict) -> Tuple[str, str]:
    """(label, value) for an item's consideration — reads whichever of the
    differently-named keys each source in this codebase uses:
    "consideration_amount" (DLV closed records, set at close time) takes
    priority over "consideration" (queue items, Fetch Tasks task dicts)."""
    consider = format_consideration(
        item.get("consideration_amount") or item.get("consideration"),
        item.get("currency_code", ""),
    ) or "—"
    return ("💰 Consideration", consider)


def parcel_field(item: Dict) -> Tuple[str, str]:
    """(label, value) for an item's parcel — "parcel" (DLV) or
    "parcel_number" (Fetch Tasks/Auto Fetch), "—" if neither is set."""
    return ("📋 Parcel", item.get("parcel") or item.get("parcel_number") or "—")


def tag_field(item: Dict) -> Optional[Tuple[str, str]]:
    """(label, value) for an item's DLV Batch tag, or None if untagged —
    callers append this at the end of their fields list only when present,
    rather than showing a bare "—" for something most tasks won't have."""
    return ("🏷 Tag", item["tag"]) if item.get("tag") else None


def default_task_fields(item: Dict) -> List[Tuple[str, str]]:
    """The three fields nearly every report shares, in the order they're
    conventionally shown: Assessor, Consideration, Parcel. A convenience
    bundle for the common case — reports that need a custom assessor value
    (e.g. Fetch Tasks' officers-list fallback when no ASSESSOR_OF_STAMP_DUTY
    match exists) call assessor_field/consideration_field/parcel_field
    individually instead of this bundle."""
    return [assessor_field(item), consideration_field(item), parcel_field(item)]
