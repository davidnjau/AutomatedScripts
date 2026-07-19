#!/usr/bin/env python3
"""
dlv_incremental.py
===================
Incremental tagging — a third DLV Batch tag option (alongside the fixed
dlv_core.DLV_TAGS "Queue"/"Direct") that auto-assigns a sequential
"B{batch}-T{task}" label instead of a picked fixed value. Tapping "🔢
Incremental" in DLV Batch's Tag Tasks step always consumes the next slot
in a persisted (batch_number, task_number, batch_size) counter
(saved_incremental_counter.json) — task_number 1..batch_size within a
batch, then batch_number advances and task_number wraps back to 1.
batch_size defaults to 6 but, along with the counter's starting
batch_number/task_number, has no other built-in value (real-world usage
rarely starts at batch 1 task 1) — configure both together in one flow
via this feature's ⚙️ Set Counter action (get_batch_size() reads the
current size back out for anything that needs it).

The counter is only consumed at DLV Batch confirm time (dlv_batch.py's
recv_db_confirm resolves any INCREMENTAL_TAG_SENTINEL value in
sess.tag_by_ref via next_incremental_tag() right before saving), not at
tag-selection time — so cancelling a batch submission never burns a
counter slot for a tag that was never actually persisted.

Reports here are deliberately separate from DLV Tasks' own By
Valuer/By Tag reports (which assume a small fixed tag vocabulary for
their picker) since incremental tags are unique per ref. Both always use
the CURRENT batch_size setting, even for batches created under a
different size before it was last changed. Each item within them is
rendered as its own labeled block via task_block.format_labeled_block —
the shared visual every report in the bot uses — not a packed one-liner:

- 📦 By Batch — every incremental-tagged ref, grouped by its original
  batch_number, sourced from saved_dlv_batch.json ("queued") and
  saved_assignments.json ("cleared") only — deliberately not
  saved_dlv_closed.json, since "cleared" here means "assigned", not
  "DLV-completed". A batch auto-closes (a persisted status flag, not a
  data move) the moment all of its task slots are found cleared; ✋ Close
  Batch offers the same action manually for anyone impatient to see it
  reflected without waiting for the next report view.
- ✅ Cleared — every cleared (assigned) incremental-tagged ref, sorted by
  assigned_at and chunked into groups of batch_size in clearance order
  (First Cleared, Second Cleared, ...) — independent of original batch
  number, since tasks from different batches can clear in any order.

Call register(app) from bot.py's main() to wire this feature in.
"""

import json
import os
import re
from enum import Enum, auto
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from common import (
    BTN_INCREMENTAL,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    load_saved_assignments,
    not_cancel,
)
from dlv_core import load_dlv_batch, parse_incremental_tag
from task_block import assessor_field, consideration_field, format_labeled_block, parcel_field
from telegram_report import _send_chunked_report

SAVED_INCREMENTAL_COUNTER_FILE = os.path.join(DATA_DIR, "saved_incremental_counter.json")
SAVED_INCREMENTAL_CLOSED_FILE  = os.path.join(DATA_DIR, "saved_incremental_closed_batches.json")

# INCREMENTAL_TAG_SENTINEL/parse_incremental_tag live in dlv_core.py (the
# shared low-level DLV module), not here, since dlv_tasks.py needs them too
# and this module already imports from dlv_tasks.py — importing
# dlv_core.INCREMENTAL_TAG_SENTINEL back here would be pointless (this
# module never picks the sentinel, only resolves it via next_incremental_tag),
# so callers needing it (dlv_batch.py) import it from dlv_core directly.

_DEFAULT_BATCH_SIZE = 6

_ORDINALS = ["First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth"]


# ──────────────────────────────────────────────────────────
# Counter persistence
# ──────────────────────────────────────────────────────────
def load_incremental_counter() -> Dict:
    """Current (batch_number, task_number, batch_size) position — defaults
    to batch 1, task 1, batch_size 6. batch_size defaults in for files
    saved before it existed, rather than requiring a migration step."""
    try:
        with open(SAVED_INCREMENTAL_COUNTER_FILE) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"batch_number": 1, "task_number": 1, "batch_size": _DEFAULT_BATCH_SIZE}
    cfg.setdefault("batch_size", _DEFAULT_BATCH_SIZE)
    return cfg


def save_incremental_counter(cfg: Dict) -> None:
    _atomic_json_write(SAVED_INCREMENTAL_COUNTER_FILE, cfg, indent=2)


def get_batch_size() -> int:
    """Current tasks-per-batch setting."""
    return load_incremental_counter().get("batch_size", _DEFAULT_BATCH_SIZE)


def set_incremental_counter(batch_number: int, task_number: int, batch_size: Optional[int] = None) -> None:
    """Manually seed the counter's position to match real-world state (e.g.
    a batch already partway used before this feature existed), and
    optionally the tasks-per-batch size — left unchanged if not given."""
    current = load_incremental_counter()
    save_incremental_counter({
        "batch_number": batch_number,
        "task_number":  task_number,
        "batch_size":   batch_size if batch_size is not None else current.get("batch_size", _DEFAULT_BATCH_SIZE),
    })


def next_incremental_tag() -> str:
    """Consume and return the next "B{batch}-T{task}" tag, advancing the
    persisted counter — task_number wraps to 1 and batch_number advances
    once the configured batch_size is reached."""
    cfg   = load_incremental_counter()
    batch = cfg.get("batch_number", 1)
    task  = cfg.get("task_number", 1)
    size  = cfg.get("batch_size", _DEFAULT_BATCH_SIZE)
    tag   = f"B{batch}-T{task}"
    if task >= size:
        save_incremental_counter({"batch_number": batch + 1, "task_number": 1, "batch_size": size})
    else:
        save_incremental_counter({"batch_number": batch, "task_number": task + 1, "batch_size": size})
    return tag


# ──────────────────────────────────────────────────────────
# Closed-batch persistence — a status flag only, never moves/deletes data
# ──────────────────────────────────────────────────────────
def load_closed_batches() -> List[int]:
    try:
        with open(SAVED_INCREMENTAL_CLOSED_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_closed_batches(batches: List[int]) -> None:
    _atomic_json_write(SAVED_INCREMENTAL_CLOSED_FILE, sorted(set(batches)), indent=2)


def close_batch(batch_number: int) -> None:
    """Flag a batch number as closed — idempotent, a no-op if already closed."""
    closed = load_closed_batches()
    if batch_number not in closed:
        closed.append(batch_number)
        save_closed_batches(closed)


# ──────────────────────────────────────────────────────────
# Data gathering
# ──────────────────────────────────────────────────────────
def _ic_gather_items() -> List[Dict]:
    """Every incremental-tagged ref from saved_dlv_batch.json ("queued")
    and saved_assignments.json ("cleared"), each annotated with its parsed
    batch_number/task_number/status. If a ref somehow appears in both
    (shouldn't happen in the normal flow), the assignments.json ("cleared")
    version wins, since it reflects the more-progressed state."""
    by_ref: Dict[str, Dict] = {}
    for item in load_dlv_batch():
        parsed = parse_incremental_tag(item.get("tag", ""))
        if not parsed:
            continue
        batch_number, task_number = parsed
        by_ref[item["ref"]] = {**item, "batch_number": batch_number, "task_number": task_number, "status": "queued"}
    for ref, info in load_saved_assignments().items():
        parsed = parse_incremental_tag(info.get("tag", ""))
        if not parsed:
            continue
        batch_number, task_number = parsed
        by_ref[ref] = {**info, "ref": ref, "batch_number": batch_number, "task_number": task_number, "status": "cleared"}
    return list(by_ref.values())


def _ic_group_by_batch(items: List[Dict]) -> Dict[int, List[Dict]]:
    """Group items by batch_number, each group's items sorted by task_number."""
    groups: Dict[int, List[Dict]] = {}
    for item in items:
        groups.setdefault(item["batch_number"], []).append(item)
    for batch_number in groups:
        groups[batch_number].sort(key=lambda i: i["task_number"])
    return groups


def _ic_eligible_batches(grouped: Dict[int, List[Dict]], batch_size: int) -> List[int]:
    """Batch numbers where all task slots (T1-T{batch_size}) are present
    and every one is cleared. Uses the CURRENT batch_size setting for
    every batch checked — if batch_size was changed after some batches
    were created, older batches are judged against the new size too."""
    eligible = []
    for batch_number, items in grouped.items():
        task_numbers = {i["task_number"] for i in items}
        if task_numbers == set(range(1, batch_size + 1)) and all(i["status"] == "cleared" for i in items):
            eligible.append(batch_number)
    return sorted(eligible)


def _ic_auto_close(grouped: Dict[int, List[Dict]], batch_size: int) -> List[int]:
    """Auto-flag any newly-eligible batch as closed. Returns the batch
    numbers newly closed this call (already-closed ones excluded)."""
    closed   = set(load_closed_batches())
    eligible = set(_ic_eligible_batches(grouped, batch_size))
    newly_closed = sorted(eligible - closed)
    if newly_closed:
        save_closed_batches(sorted(closed | eligible))
    return newly_closed


# ──────────────────────────────────────────────────────────
# Report formatting
# ──────────────────────────────────────────────────────────
def _ic_format_by_batch_report(grouped: Dict[int, List[Dict]], closed_batches: List[int], batch_size: int) -> List[str]:
    """📦 By Batch — one section per original batch number, each task slot
    rendered as its own labeled block (task_block.format_labeled_block —
    the shared visual every report in the bot uses, numbered by its
    task_number), flagging closed batches."""
    lines = ["📦 *Incremental Report — By Batch*\n"]
    if not grouped:
        lines.append("_No incremental-tagged tasks yet._")
        return lines
    closed_set = set(closed_batches)
    for batch_number in sorted(grouped):
        items = grouped[batch_number]
        status_note = " ✅ CLOSED" if batch_number in closed_set else ""
        lines.append(f"*Batch {batch_number}*{status_note} — {len(items)}/{batch_size} tagged")
        for item in items:
            status_label = "✅ Cleared" if item["status"] == "cleared" else "⏳ Queued"
            fields = [
                ("📊 Status", status_label),
                ("👤 Valuer", item.get("valuer_name") or "—"),
                assessor_field(item),
                consideration_field(item),
                parcel_field(item),
            ]
            lines.append(format_labeled_block(item["task_number"], item["ref"], fields))
    return lines


def _ic_format_cleared_report(items: List[Dict], batch_size: int) -> List[str]:
    """✅ Cleared — cleared items only, sorted by clearance (assigned_at)
    order and chunked into groups of batch_size regardless of original
    batch, each rendered as its own labeled block."""
    cleared = [i for i in items if i["status"] == "cleared"]
    cleared.sort(key=lambda i: i.get("assigned_at", ""))
    lines = ["✅ *Incremental Report — Cleared*\n"]
    if not cleared:
        lines.append("_No cleared incremental-tagged tasks yet._")
        return lines
    for group_idx in range(0, len(cleared), batch_size):
        group = cleared[group_idx:group_idx + batch_size]
        ordinal_idx = group_idx // batch_size
        ordinal = f"{_ORDINALS[ordinal_idx]} Cleared" if ordinal_idx < len(_ORDINALS) else f"Cleared Group {ordinal_idx + 1}"
        lines.append(f"*{ordinal}* ({len(group)}/{batch_size})")
        for i, item in enumerate(group, start=1):
            fields = [
                ("🔢 Batch/Task", f"B{item['batch_number']}-T{item['task_number']}"),
                ("👤 Valuer", item.get("valuer_name") or "—"),
                assessor_field(item),
                consideration_field(item),
                parcel_field(item),
            ]
            lines.append(format_labeled_block(i, item["ref"], fields))
    return lines


# ──────────────────────────────────────────────────────────
# States — Incremental Report conversation
# ──────────────────────────────────────────────────────────
class IC(Enum):
    MENU       = auto()   # By Batch / Cleared / Set Counter / Close Batch
    SET_SIZE   = auto()   # enter tasks-per-batch (or skip to keep current)
    SET_BATCH  = auto()   # enter the batch number to seed
    SET_TASK   = auto()   # enter the task number to seed
    CLOSE_PICK = auto()   # pick which eligible-but-unclosed batch to manually close


def _ic_menu_keyboard() -> InlineKeyboardMarkup:
    """The Incremental Report menu's action picker."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 By Batch", callback_data="ic_menu:bybatch")],
        [InlineKeyboardButton("✅ Cleared",  callback_data="ic_menu:cleared")],
        [InlineKeyboardButton("⚙️ Set Counter", callback_data="ic_menu:setcounter")],
        [InlineKeyboardButton("🔓 Close Batch", callback_data="ic_menu:closebatch")],
        [InlineKeyboardButton("🛑 Cancel", callback_data="ic_menu:cancel")],
    ])


def _ic_close_pick_keyboard(batch_numbers: List[int]) -> InlineKeyboardMarkup:
    """One button per eligible-but-unclosed batch, for the manual close picker."""
    rows = [[InlineKeyboardButton(f"Batch {b}", callback_data=f"ic_close:{b}")] for b in batch_numbers]
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data="ic_close:cancel")])
    return InlineKeyboardMarkup(rows)


async def cmd_incremental(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point (/incremental or the menu button) — show the action picker."""
    if not allowed(update): return await deny(update)
    cfg = load_incremental_counter()
    await update.message.reply_text(
        f"🔢 *Incremental Report*\n\nCurrent position: *Batch {cfg.get('batch_number', 1)}, "
        f"Task {cfg.get('task_number', 1)}* (batch size: {cfg.get('batch_size', _DEFAULT_BATCH_SIZE)})",
        parse_mode="Markdown",
        reply_markup=_ic_menu_keyboard(),
    )
    return IC.MENU


async def recv_ic_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the By Batch/Cleared/Set Counter/Close Batch/Cancel choice."""
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    if action == "setcounter":
        await query.edit_message_text(
            f"Enter the number of tasks per batch (current: {get_batch_size()}), "
            "or send `skip` to keep it unchanged:",
            parse_mode="Markdown",
        )
        return IC.SET_SIZE

    batch_size = get_batch_size()
    items      = _ic_gather_items()
    grouped    = _ic_group_by_batch(items)

    if action == "bybatch":
        _ic_auto_close(grouped, batch_size)
        lines = _ic_format_by_batch_report(grouped, load_closed_batches(), batch_size)
        await query.edit_message_text("⏳ Building report…")

        async def _send(text, reply_markup):
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        await _send_chunked_report(_send, lines, reply_markup=_main_menu())
        return ConversationHandler.END

    if action == "cleared":
        lines = _ic_format_cleared_report(items, batch_size)
        await query.edit_message_text("⏳ Building report…")

        async def _send(text, reply_markup):
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        await _send_chunked_report(_send, lines, reply_markup=_main_menu())
        return ConversationHandler.END

    # action == "closebatch"
    _ic_auto_close(grouped, batch_size)   # catch up first, so the picker only ever shows genuinely-manual cases
    eligible = set(_ic_eligible_batches(grouped, batch_size)) - set(load_closed_batches())
    if not eligible:
        await query.edit_message_text("ℹ️ No batch is fully cleared and still open.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END
    await query.edit_message_text("🔓 Pick a batch to close:", reply_markup=_ic_close_pick_keyboard(sorted(eligible)))
    return IC.CLOSE_PICK


async def recv_ic_close_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually close the picked batch."""
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]

    if data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    batch_number = int(data)
    close_batch(batch_number)
    await query.edit_message_text(f"✅ Batch {batch_number} closed.")
    await query.message.reply_text("Main menu:", reply_markup=_main_menu())
    return ConversationHandler.END


async def recv_ic_set_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Parse the entered tasks-per-batch size (or `skip` to keep the
    current one), then ask for the starting batch number."""
    if not allowed(update): return await deny(update)
    text = update.message.text.strip()
    if text.lower() == "skip":
        ctx.user_data["ic_set_size"] = get_batch_size()
    else:
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❌ Enter a positive whole number, or `skip` to keep the current size.",
                                             parse_mode="Markdown")
            return IC.SET_SIZE
        ctx.user_data["ic_set_size"] = int(text)
    await update.message.reply_text("Enter the batch number to start at:")
    return IC.SET_BATCH


async def recv_ic_set_batch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Parse the entered batch number, then ask for the task number."""
    if not allowed(update): return await deny(update)
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Enter a positive whole number for the batch.")
        return IC.SET_BATCH
    ctx.user_data["ic_set_batch"] = int(text)
    size = ctx.user_data.get("ic_set_size", _DEFAULT_BATCH_SIZE)
    await update.message.reply_text(f"Batch set to *{text}*.\n\nEnter the task number (1-{size}):",
                                     parse_mode="Markdown")
    return IC.SET_TASK


async def recv_ic_set_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Parse the entered task number and finalize the counter seed
    (position + batch size, set together as one configuration)."""
    if not allowed(update): return await deny(update)
    text = update.message.text.strip()
    size = ctx.user_data.get("ic_set_size", _DEFAULT_BATCH_SIZE)
    if not text.isdigit() or not (1 <= int(text) <= size):
        await update.message.reply_text(f"❌ Enter a whole number from 1 to {size}.")
        return IC.SET_TASK
    batch_number = ctx.user_data.get("ic_set_batch", 1)
    task_number  = int(text)
    set_incremental_counter(batch_number, task_number, size)
    await update.message.reply_text(
        f"✅ Counter set to *Batch {batch_number}, Task {task_number}* (batch size: {size}). "
        f"The next 🔢 Incremental tag will be `B{batch_number}-T{task_number}`.",
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────
def register(app: Application) -> None:
    """Wire the Incremental Report conversation into the given Application."""
    ic_conv = ConversationHandler(
        entry_points=[
            CommandHandler("incremental", cmd_incremental),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_INCREMENTAL)}$"), cmd_incremental),
        ],
        states={
            IC.MENU:       [CallbackQueryHandler(recv_ic_menu, pattern=r"^ic_menu:")],
            IC.SET_SIZE:   [MessageHandler(not_cancel, recv_ic_set_size)],
            IC.SET_BATCH:  [MessageHandler(not_cancel, recv_ic_set_batch)],
            IC.SET_TASK:   [MessageHandler(not_cancel, recv_ic_set_task)],
            IC.CLOSE_PICK: [CallbackQueryHandler(recv_ic_close_pick, pattern=r"^ic_close:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(ic_conv)
