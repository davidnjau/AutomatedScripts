#!/usr/bin/env python3
"""
ocr_stats.py
============
🧮 OCR Usage — admin-only OCR usage counter + approximate RunPod GPU-credit
tracker (📱 under 🛠 System / /ocrstats).

Every photo the bot OCRs goes through new_assignment.ocr_extract_refs, which
calls record_extraction(source) here exactly once per photo (source is
"glm-ocr" | "claude" | "tesseract" | "none"). This module keeps a lifetime
per-source count and, for the self-hosted GLM-OCR path (the only one that
spends RunPod GPU credit), deducts an estimated per-extraction cost from a
running balance so an admin can see, at a glance, roughly how much has been
spent and how much credit is left — without logging into the RunPod console.

It is deliberately *approximate*: the per-extraction cost is a flat estimate
(default ~$0.02, the measured cold-start cost of one photo — a warm photo is
far cheaper), and only GLM-OCR extractions deduct. Claude Vision bills
Anthropic separately; Tesseract is free. An admin seeds the real RunPod
balance via 💵 Set Balance (which re-baselines spend to 0), and tunes the
estimate via ⚙️ Set Cost. Everything lives in saved_ocr_counts.json in the
data volume, so it survives container restarts (unlike docker logs).

Admin-only: 🛠 System is an admin-only menu category, and cmd_ocr_stats
re-checks is_admin itself since /ocrstats bypasses the menu.

Call register(app) from bot.py's main() to wire this feature in.
"""

import json
import os
import re
from enum import Enum, auto
from typing import Dict

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
    BTN_OCR_STATS,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _main_menu_for,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    is_admin,
)

SAVED_OCR_COUNTS_FILE = os.path.join(DATA_DIR, "saved_ocr_counts.json")

# Flat estimate per GPU (GLM-OCR) extraction — the measured cold-start cost of
# one photo (~$0.02); a warm photo is far cheaper, so this is conservative.
_DEFAULT_COST_PER_EXTRACTION = 0.02
# Only the self-hosted GPU path spends RunPod credit; the others don't.
_BILLABLE_SOURCES = {"glm-ocr"}

# Human labels for the four source buckets, in display order.
_SOURCE_LABELS = [
    ("glm-ocr",   "GLM-OCR (GPU)"),
    ("claude",    "Claude Vision"),
    ("tesseract", "Tesseract"),
    ("none",      "No result"),
]


# ──────────────────────────────────────────────────────────
# Storage
# ──────────────────────────────────────────────────────────

def load_ocr_counts() -> Dict:
    """Read saved_ocr_counts.json, merging in defaults for any missing key so
    callers always get a fully-shaped dict (counts, cost, balance, spent)."""
    defaults = {
        "counts": {},
        "cost_per_extraction_usd": _DEFAULT_COST_PER_EXTRACTION,
        "starting_balance_usd": 0.0,
        "spent_usd": 0.0,
    }
    try:
        with open(SAVED_OCR_COUNTS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return defaults
    merged = {**defaults, **data}
    merged.setdefault("counts", {})
    return merged


def save_ocr_counts(data: Dict) -> None:
    """Persist the OCR-counts/credit dict."""
    _atomic_json_write(SAVED_OCR_COUNTS_FILE, data, indent=2)


def record_extraction(source: str) -> None:
    """Bump the lifetime per-source counter and, for a billable (GPU) source,
    deduct the estimated cost from spend. Called once per photo from
    new_assignment.ocr_extract_refs; the caller wraps it so a counter failure
    never breaks an actual OCR/assignment."""
    data = load_ocr_counts()
    counts = data.setdefault("counts", {})
    counts[source] = counts.get(source, 0) + 1
    if source in _BILLABLE_SOURCES:
        cost = data.get("cost_per_extraction_usd", _DEFAULT_COST_PER_EXTRACTION)
        data["spent_usd"] = round(data.get("spent_usd", 0.0) + cost, 4)
    save_ocr_counts(data)


# ──────────────────────────────────────────────────────────
# Derived values + display
# ──────────────────────────────────────────────────────────

def _remaining(data: Dict) -> float:
    """Estimated remaining RunPod credit = starting balance − spend since the
    balance was last set."""
    return round(data.get("starting_balance_usd", 0.0) - data.get("spent_usd", 0.0), 2)


def _total_extractions(data: Dict) -> int:
    """Sum of all per-source extraction counts."""
    return sum(data.get("counts", {}).values())


def _os_stats_text(data: Dict) -> str:
    """Render the full OCR-usage + credit screen (Markdown)."""
    counts = data.get("counts", {})
    lines = ["🧮 *OCR Usage & GPU Credit*", "", "*Extractions (lifetime):*"]
    for key, label in _SOURCE_LABELS:
        lines.append(f"  • {label}: {counts.get(key, 0)}")
    # Include any unexpected source keys not in _SOURCE_LABELS.
    for key in counts:
        if key not in {k for k, _ in _SOURCE_LABELS}:
            lines.append(f"  • {key}: {counts[key]}")
    lines.append(f"  *Total:* {_total_extractions(data)}")
    lines.append("")
    lines.append(f"Est. cost per GPU extraction: ${data.get('cost_per_extraction_usd', _DEFAULT_COST_PER_EXTRACTION):.4f}")
    lines.append(f"Balance (last set): ${data.get('starting_balance_usd', 0.0):.2f}")
    lines.append(f"Est. spent since then: ${data.get('spent_usd', 0.0):.2f}")
    lines.append(f"*Est. remaining: ${_remaining(data):.2f}*")
    lines.append("")
    lines.append(
        "ℹ️ _Approximate. Only GLM-OCR (self-hosted GPU) extractions deduct "
        "credit; Claude Vision bills Anthropic separately and Tesseract is "
        "free. Tap 💵 Set Balance and enter your real RunPod figure to "
        "re-baseline._"
    )
    return "\n".join(lines)


def _os_menu_keyboard() -> InlineKeyboardMarkup:
    """The OCR-usage action keyboard: set the real balance, tune the
    per-extraction cost estimate, reset the counters, or close."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Set Balance", callback_data="ocr:balance")],
        [InlineKeyboardButton("⚙️ Set Cost/Extraction", callback_data="ocr:cost")],
        [InlineKeyboardButton("🔄 Reset Counters", callback_data="ocr:reset")],
        [InlineKeyboardButton("🛑 Close", callback_data="ocr:close")],
    ])


# ──────────────────────────────────────────────────────────
# States + handlers
# ──────────────────────────────────────────────────────────
class OS(Enum):
    MENU        = auto()   # view stats, pick an action
    SET_BALANCE = auto()   # enter the real RunPod balance (re-baselines spend)
    SET_COST    = auto()   # enter the per-extraction cost estimate


async def cmd_ocr_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point (/ocrstats or the 🧮 OCR Usage button) — admin-only; shows
    the OCR usage counts and the approximate spent/remaining RunPod credit."""
    if not allowed(update):
        return await deny(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Admin access required.",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END
    data = load_ocr_counts()
    await update.message.reply_text(
        _os_stats_text(data),
        parse_mode="Markdown",
        reply_markup=_os_menu_keyboard(),
    )
    return OS.MENU


async def recv_os_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Route the OCR-usage action buttons: Set Balance / Set Cost / Reset /
    Close."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    action = query.data.split(":", 1)[1]

    if action == "close":
        await query.edit_message_reply_markup(reply_markup=None)
        return ConversationHandler.END

    if action == "balance":
        await query.edit_message_text(
            "💵 Enter the *current RunPod balance* in USD (e.g. `29.87`). "
            "This re-baselines estimated spend to $0 from here.",
            parse_mode="Markdown",
        )
        return OS.SET_BALANCE

    if action == "cost":
        await query.edit_message_text(
            "⚙️ Enter the *estimated cost per GPU extraction* in USD "
            "(e.g. `0.02`).",
            parse_mode="Markdown",
        )
        return OS.SET_COST

    if action == "reset":
        data = load_ocr_counts()
        data["counts"] = {}
        data["spent_usd"] = 0.0
        save_ocr_counts(data)
        await query.edit_message_text(
            "🔄 Counters and spend reset.\n\n" + _os_stats_text(data),
            parse_mode="Markdown",
            reply_markup=_os_menu_keyboard(),
        )
        return OS.MENU

    return OS.MENU


def _parse_usd(text: str):
    """Parse a non-negative USD amount from free text; returns a float or
    None if it isn't a valid non-negative number."""
    try:
        val = float(text.replace("$", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None
    return val if val >= 0 else None


async def recv_os_set_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Store the admin-entered real RunPod balance and re-baseline spend to 0."""
    amount = _parse_usd(update.message.text)
    if amount is None:
        await update.message.reply_text("⚠️ Enter a non-negative number, e.g. `29.87`.", parse_mode="Markdown")
        return OS.SET_BALANCE
    data = load_ocr_counts()
    data["starting_balance_usd"] = round(amount, 2)
    data["spent_usd"] = 0.0
    save_ocr_counts(data)
    await update.message.reply_text(
        f"✅ Balance set to ${amount:.2f}.\n\n" + _os_stats_text(data),
        parse_mode="Markdown",
        reply_markup=_os_menu_keyboard(),
    )
    return OS.MENU


async def recv_os_set_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Store the admin-entered per-extraction cost estimate."""
    amount = _parse_usd(update.message.text)
    if amount is None:
        await update.message.reply_text("⚠️ Enter a non-negative number, e.g. `0.02`.", parse_mode="Markdown")
        return OS.SET_COST
    data = load_ocr_counts()
    data["cost_per_extraction_usd"] = round(amount, 4)
    save_ocr_counts(data)
    await update.message.reply_text(
        f"✅ Cost per extraction set to ${amount:.4f}.\n\n" + _os_stats_text(data),
        parse_mode="Markdown",
        reply_markup=_os_menu_keyboard(),
    )
    return OS.MENU


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the OCR Usage conversation into the given Application."""
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("ocrstats", cmd_ocr_stats),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_OCR_STATS)}$"), cmd_ocr_stats),
        ],
        states={
            OS.MENU:        [CallbackQueryHandler(recv_os_menu, pattern=r"^ocr:")],
            OS.SET_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~_CANCEL_FILTER, recv_os_set_balance)],
            OS.SET_COST:    [MessageHandler(filters.TEXT & ~filters.COMMAND & ~_CANCEL_FILTER, recv_os_set_cost)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conv)
