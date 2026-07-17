#!/usr/bin/env python3
"""
dlv_incremental.py
===================
Incremental tagging — a third DLV Batch tag option (alongside the fixed
dlv_core.DLV_TAGS "Queue"/"Direct") that auto-assigns a sequential
"B{batch}-T{task}" label instead of a picked fixed value. Batches are 6
tasks wide: tapping "🔢 Incremental" in DLV Batch's Tag Tasks step always
consumes the next slot in a persisted (batch_number, task_number) counter
(saved_incremental_counter.json) — task_number 1..6 within a batch, then
batch_number advances and task_number wraps back to 1. The counter has no
built-in starting point (real-world usage rarely starts at batch 1 task
1) — seed it once via this feature's ⚙️ Set Counter action.

The counter is only consumed at DLV Batch confirm time (dlv_batch.py's
recv_db_confirm resolves any INCREMENTAL_TAG_SENTINEL value in
sess.tag_by_ref via next_incremental_tag() right before saving), not at
tag-selection time — so cancelling a batch submission never burns a
counter slot for a tag that was never actually persisted.

Reports here are deliberately separate from DLV Tasks' own By
Valuer/By Tag reports (which assume a small fixed tag vocabulary for
their picker) since incremental tags are unique per ref:

- 📦 By Batch — every incremental-tagged ref, grouped by its original
  batch_number, sourced from saved_dlv_batch.json ("queued") and
  saved_assignments.json ("cleared") only — deliberately not
  saved_dlv_closed.json, since "cleared" here means "assigned", not
  "DLV-completed". A batch auto-closes (a persisted status flag, not a
  data move) the moment all 6 of its task slots are found cleared; ✋ Close
  Batch offers the same action manually for anyone impatient to see it
  reflected without waiting for the next report view.
- ✅ Cleared — every cleared (assigned) incremental-tagged ref, sorted by
  assigned_at and chunked into groups of 6 in clearance order (First
  Cleared, Second Cleared, ...) — independent of original batch number,
  since tasks from different batches can clear in any order.

Call register(app) from bot.py's main() to wire this feature in.
"""

import json
import os
import re
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

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
    md_escape,
    not_cancel,
)
from dlv_core import load_dlv_batch
from telegram_report import _send_chunked_report

SAVED_INCREMENTAL_COUNTER_FILE = os.path.join(DATA_DIR, "saved_incremental_counter.json")
SAVED_INCREMENTAL_CLOSED_FILE  = os.path.join(DATA_DIR, "saved_incremental_closed_batches.json")

# Picked in DLV Batch's Tag Tasks step (dlv_batch.py's _db_tag_value_keyboard);
# resolved to a real "B{n}-T{n}" value only at confirm time, by
# next_incremental_tag() — see module docstring for why not at pick time.
INCREMENTAL_TAG_SENTINEL = "__incremental__"

_INCREMENTAL_TAG_RE = re.compile(r"^B(\d+)-T(\d+)$")
_BATCH_SIZE = 6

_ORDINALS = ["First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth"]


# ──────────────────────────────────────────────────────────
# Counter persistence
# ──────────────────────────────────────────────────────────
def load_incremental_counter() -> Dict:
    """Current (batch_number, task_number) position — defaults to batch 1, task 1."""
    try:
        with open(SAVED_INCREMENTAL_COUNTER_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"batch_number": 1, "task_number": 1}


def save_incremental_counter(cfg: Dict) -> None:
    _atomic_json_write(SAVED_INCREMENTAL_COUNTER_FILE, cfg, indent=2)


def set_incremental_counter(batch_number: int, task_number: int) -> None:
    """Manually seed the counter to match real-world state (e.g. a batch
    already partway used before this feature existed)."""
    save_incremental_counter({"batch_number": batch_number, "task_number": task_number})


def next_incremental_tag() -> str:
    """Consume and return the next "B{batch}-T{task}" tag, advancing the
    persisted counter — task_number wraps to 1 and batch_number advances
    once task 6 is consumed."""
    cfg   = load_incremental_counter()
    batch = cfg.get("batch_number", 1)
    task  = cfg.get("task_number", 1)
    tag   = f"B{batch}-T{task}"
    if task >= _BATCH_SIZE:
        save_incremental_counter({"batch_number": batch + 1, "task_number": 1})
    else:
        save_incremental_counter({"batch_number": batch, "task_number": task + 1})
    return tag


def parse_incremental_tag(tag: Optional[str]) -> Optional[Tuple[int, int]]:
    """(batch_number, task_number) parsed from a "B{n}-T{n}" tag, or None
    if tag is empty or doesn't match (e.g. a fixed Queue/Direct tag)."""
    m = _INCREMENTAL_TAG_RE.match(tag or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


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


def _ic_eligible_batches(grouped: Dict[int, List[Dict]]) -> List[int]:
    """Batch numbers where all 6 task slots (T1-T6) are present and every one is cleared."""
    eligible = []
    for batch_number, items in grouped.items():
        task_numbers = {i["task_number"] for i in items}
        if task_numbers == set(range(1, _BATCH_SIZE + 1)) and all(i["status"] == "cleared" for i in items):
            eligible.append(batch_number)
    return sorted(eligible)


def _ic_auto_close(grouped: Dict[int, List[Dict]]) -> List[int]:
    """Auto-flag any newly-eligible batch as closed. Returns the batch
    numbers newly closed this call (already-closed ones excluded)."""
    closed   = set(load_closed_batches())
    eligible = set(_ic_eligible_batches(grouped))
    newly_closed = sorted(eligible - closed)
    if newly_closed:
        save_closed_batches(sorted(closed | eligible))
    return newly_closed


# ──────────────────────────────────────────────────────────
# Report formatting
# ──────────────────────────────────────────────────────────
def _ic_format_by_batch_report(grouped: Dict[int, List[Dict]], closed_batches: List[int]) -> List[str]:
    """📦 By Batch — one section per original batch number, each task
    slot's ref/status/valuer, flagging closed batches."""
    lines = ["📦 *Incremental Report — By Batch*\n"]
    if not grouped:
        lines.append("_No incremental-tagged tasks yet._")
        return lines
    closed_set = set(closed_batches)
    for batch_number in sorted(grouped):
        items = grouped[batch_number]
        status_note = " ✅ CLOSED" if batch_number in closed_set else ""
        lines.append(f"*Batch {batch_number}*{status_note} — {len(items)}/{_BATCH_SIZE} tagged")
        for item in items:
            status_icon = "✅" if item["status"] == "cleared" else "⏳"
            valuer = md_escape(item.get("valuer_name") or "—")
            lines.append(f"  T{item['task_number']}: `{item['ref']}` {status_icon} {valuer}")
    return lines


def _ic_format_cleared_report(items: List[Dict]) -> List[str]:
    """✅ Cleared — cleared items only, sorted by clearance (assigned_at)
    order and chunked into groups of 6 regardless of original batch."""
    cleared = [i for i in items if i["status"] == "cleared"]
    cleared.sort(key=lambda i: i.get("assigned_at", ""))
    lines = ["✅ *Incremental Report — Cleared*\n"]
    if not cleared:
        lines.append("_No cleared incremental-tagged tasks yet._")
        return lines
    for group_idx in range(0, len(cleared), _BATCH_SIZE):
        group = cleared[group_idx:group_idx + _BATCH_SIZE]
        ordinal_idx = group_idx // _BATCH_SIZE
        ordinal = f"{_ORDINALS[ordinal_idx]} Cleared" if ordinal_idx < len(_ORDINALS) else f"Cleared Group {ordinal_idx + 1}"
        lines.append(f"*{ordinal}* ({len(group)}/{_BATCH_SIZE})")
        for item in group:
            valuer = md_escape(item.get("valuer_name") or "—")
            lines.append(f"  B{item['batch_number']}-T{item['task_number']}: `{item['ref']}` — {valuer}")
    return lines


# ──────────────────────────────────────────────────────────
# States — Incremental Report conversation
# ──────────────────────────────────────────────────────────
class IC(Enum):
    MENU       = auto()   # By Batch / Cleared / Set Counter / Close Batch
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
        f"Task {cfg.get('task_number', 1)}*",
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
        await query.edit_message_text("Enter the batch number to set:")
        return IC.SET_BATCH

    items   = _ic_gather_items()
    grouped = _ic_group_by_batch(items)

    if action == "bybatch":
        _ic_auto_close(grouped)
        lines = _ic_format_by_batch_report(grouped, load_closed_batches())
        await query.edit_message_text("⏳ Building report…")

        async def _send(text, reply_markup):
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        await _send_chunked_report(_send, lines, reply_markup=_main_menu())
        return ConversationHandler.END

    if action == "cleared":
        lines = _ic_format_cleared_report(items)
        await query.edit_message_text("⏳ Building report…")

        async def _send(text, reply_markup):
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        await _send_chunked_report(_send, lines, reply_markup=_main_menu())
        return ConversationHandler.END

    # action == "closebatch"
    _ic_auto_close(grouped)   # catch up first, so the picker only ever shows genuinely-manual cases
    eligible = set(_ic_eligible_batches(grouped)) - set(load_closed_batches())
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


async def recv_ic_set_batch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Parse the entered batch number, then ask for the task number."""
    if not allowed(update): return await deny(update)
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Enter a positive whole number for the batch.")
        return IC.SET_BATCH
    ctx.user_data["ic_set_batch"] = int(text)
    await update.message.reply_text(f"Batch set to *{text}*.\n\nEnter the task number (1-{_BATCH_SIZE}):",
                                     parse_mode="Markdown")
    return IC.SET_TASK


async def recv_ic_set_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Parse the entered task number and finalize the counter seed."""
    if not allowed(update): return await deny(update)
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= _BATCH_SIZE):
        await update.message.reply_text(f"❌ Enter a whole number from 1 to {_BATCH_SIZE}.")
        return IC.SET_TASK
    batch_number = ctx.user_data.get("ic_set_batch", 1)
    task_number  = int(text)
    set_incremental_counter(batch_number, task_number)
    await update.message.reply_text(
        f"✅ Counter set to *Batch {batch_number}, Task {task_number}*. "
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
