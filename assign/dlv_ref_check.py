#!/usr/bin/env python3
"""
dlv_ref_check.py
==================
DLV Ref Check — check a reference number's full history across every
tracked source: New Assignment, the DLV Batch queue, and the Hold queue
(/dlvcheck or "🕓 DLV Ref Check"). Unlike lookup_reference.py's /lookup
(a live Ardhisasa API search for a ref's *current* state), this is a pure
local lookup against dlv_core.py's consolidated ref-keyed store
(saved_dlv_records.json) — no API call, no credential/token needed.

Three explicit yes/no checks, each backed by a field that persists once
set (dlv_core's store only ever merges new fields onto a record, never
replaces it wholesale — see the module's own consolidation notes):
- New Assignment — `workflow` is set only by new_assignment.py's /assign
  flow (missing/None from every other assignment source), so its
  presence means this ref was assigned that way at some point.
- DLV Batch Queue — `queued_at` is set once, when dlv_batch.py first adds
  the ref to the queue, and persists through every later stage.
- Hold Queue — `hold` reflects *current* hold status only: unlike the
  other two, hold_tasks.py's release path (clear_hold_and_remove) clears
  `hold` back to None, so this check answers "currently on hold", not
  "was ever held" — the data doesn't preserve that history.

A ref with no record at all (never touched by any of the three) is
reported separately from one that has a record but is "No" on all three
checks (e.g. seen only via a live DLV search, never actually assigned).

Call register(app) from bot.py's main() to wire this feature in.
"""

import re
from enum import Enum, auto

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from common import (
    BTN_DLV_REF_CHECK,
    _CANCEL_FILTER,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    md_escape,
    not_cancel,
)
from dlv_core import _load_consolidated
from new_assignment import _WORKFLOW_LABELS
from task_block import assessor_field, consideration_field, format_labeled_block, parcel_field, tag_field

# Friendly label per record status — mirrors dlv_tasks.py's closed_reason
# labels, extended to cover every status the consolidated store uses.
_DC_STATUS_LABELS = {
    "queued":    "📥 Queued",
    "assigned":  "✍️ Assigned — at valuer's desk",
    "completed": "✅ Completed",
    "returned":  "↩️ Returned",
    "removed":   "🗑 Removed",
}


# ──────────────────────────────────────────────────────────
# States — DLV Ref Check conversation
# ──────────────────────────────────────────────────────────
class DC(Enum):
    REF_INPUT = auto()   # enter a reference number


def _dc_format_record(ref: str, record: dict) -> str:
    """Build the labeled block for any ref with a record — the three
    explicit New Assignment / DLV Batch Queue / Hold Queue checks first,
    then status, valuer, the shared Assessor/Consideration/Parcel fields,
    every timeline date the record actually has, and tag."""
    fields = [("📊 Status", _DC_STATUS_LABELS.get(record.get("status"), record.get("status") or "—"))]

    workflow = record.get("workflow")
    fields.append(("📋 New Assignment", _WORKFLOW_LABELS.get(workflow, workflow) if workflow else "❌ No"))
    fields.append(("📥 DLV Batch Queue", f"✅ Yes — {record['queued_at']}" if record.get("queued_at") else "❌ No"))
    if record.get("hold"):
        held_for = record["hold"].get("held_valuer_name", "?")
        fields.append(("✋ Hold Queue", f"✅ Currently held for {held_for}"))
    else:
        fields.append(("✋ Hold Queue", "❌ No"))

    if record.get("valuer_name"):
        fields.append(("👤 Valuer", record["valuer_name"]))
    fields.append(assessor_field(record))
    fields.append(consideration_field(record))
    fields.append(parcel_field(record))
    if record.get("assigned_at"):
        fields.append(("📅 Assigned", record["assigned_at"]))
    if record.get("closed_at"):
        closed_label = {"completed": "✅ Completed", "returned": "↩️ Returned"}.get(
            record.get("closed_reason"), "❓ Unknown",
        )
        fields.append(("📅 Closed", f"{record['closed_at']} ({closed_label})"))
    if record.get("removed_at"):
        fields.append(("🗑 Removed", record["removed_at"]))
    tag = tag_field(record)
    if tag:
        fields.append(tag)
    return format_labeled_block(1, ref, fields)


async def cmd_dlv_ref_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point — prompts for the reference number. No credential step
    # at all: this is a local-file lookup, not a live API search.
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        "🕓 *DLV Ref Check*\n\n"
        "Check a reference number's history across New Assignment, the "
        "DLV Batch queue, and the Hold queue.\n\n"
        "Enter the *reference number* to check:",
        parse_mode="Markdown",
    )
    return DC.REF_INPUT


async def recv_dc_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Look up the ref in the consolidated DLV store. No record at all ->
    # report that plainly; any record -> show all three checks, whichever
    # way they land.
    if not allowed(update): return await deny(update)

    ref = (update.message.text or "").strip()
    if not ref:
        await update.message.reply_text("Please enter a reference number.")
        return DC.REF_INPUT

    record = _load_consolidated().get(ref)

    if not record:
        text = (
            f"❌ No record at all for *{md_escape(ref)}* — never a New Assignment, "
            "never queued via DLV Batch, never placed on hold."
        )
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_main_menu())
        return ConversationHandler.END

    text = f"🕓 *DLV Ref Check* — `{md_escape(ref)}`\n\n" + _dc_format_record(ref, record)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the DLV Ref Check conversation into the given Application."""
    dc_conv = ConversationHandler(
        entry_points=[
            CommandHandler("dlvcheck", cmd_dlv_ref_check),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_DLV_REF_CHECK)}$"), cmd_dlv_ref_check),
        ],
        states={
            DC.REF_INPUT: [MessageHandler(not_cancel, recv_dc_ref)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(dc_conv)
