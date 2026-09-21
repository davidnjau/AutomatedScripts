#!/usr/bin/env python3
"""
dlv_ref_check.py
==================
DLV Ref Check — check whether a reference number was ever queued via DLV
Batch (/dlvcheck or "🕓 DLV Ref Check"). Unlike lookup_reference.py's
/lookup (a live Ardhisasa API search for a ref's *current* state), this
is a pure local lookup against dlv_core.py's consolidated ref-keyed store
(saved_dlv_records.json) — no API call, no credential/token needed.

A record existing for a ref isn't the same as having been queued: a ref
assigned directly (New Assignment/Receive Tasks, never through DLV Batch)
has an assigned_at but no queued_at. "Was it ever queued via DLV Batch"
is answered by queued_at being set on the record, regardless of the ref's
current status (queued/assigned/completed/returned/removed) — queued_at
is set once, when dlv_batch.py first adds the ref to the queue, and
persists through every later stage since dlv_core's store only ever
merges new fields onto a record, never replaces it wholesale.

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
    """Build the labeled block for a ref that was queued via DLV Batch
    (queued_at is set) — status, valuer (if assigned/closed), the shared
    Assessor/Consideration/Parcel fields, every timeline date the record
    actually has, tag, and hold info if currently held."""
    fields = [("📊 Status", _DC_STATUS_LABELS.get(record.get("status"), record.get("status") or "—"))]
    if record.get("valuer_name"):
        fields.append(("👤 Valuer", record["valuer_name"]))
    fields.append(assessor_field(record))
    fields.append(consideration_field(record))
    fields.append(parcel_field(record))
    fields.append(("📅 Queued", record.get("queued_at") or "—"))
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
    if record.get("hold"):
        held_for = record["hold"].get("held_valuer_name", "?")
        fields.append(("✋ Hold", f"held for {held_for}"))
    return format_labeled_block(1, ref, fields)


async def cmd_dlv_ref_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point — prompts for the reference number. No credential step
    # at all: this is a local-file lookup, not a live API search.
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        "🕓 *DLV Ref Check*\n\n"
        "Find out whether a reference number was ever queued via DLV "
        "Batch, and its full status history if so.\n\n"
        "Enter the *reference number* to check:",
        parse_mode="Markdown",
    )
    return DC.REF_INPUT


async def recv_dc_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Look up the ref in the consolidated DLV store and report whether
    # queued_at is set — the sole signal for "was ever queued via DLV Batch".
    if not allowed(update): return await deny(update)

    ref = (update.message.text or "").strip()
    if not ref:
        await update.message.reply_text("Please enter a reference number.")
        return DC.REF_INPUT

    record = _load_consolidated().get(ref)

    if not record or not record.get("queued_at"):
        if record:
            status_label = _DC_STATUS_LABELS.get(record.get("status"), record.get("status") or "—")
            text = (
                f"❌ *{md_escape(ref)}* was *never queued via DLV Batch*.\n\n"
                f"It does have other history though — status: {status_label}"
                + (f", assigned {record['assigned_at']}" if record.get("assigned_at") else "")
                + "."
            )
        else:
            text = f"❌ No record at all for *{md_escape(ref)}* — never queued, assigned, or seen by this bot."
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_main_menu())
        return ConversationHandler.END

    text = f"✅ *{md_escape(ref)}* was queued via DLV Batch.\n\n" + _dc_format_record(ref, record)
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
