#!/usr/bin/env python3
"""
ardhisasa_bot.py
================
Telegram bot for Ardhisasa Valuation Officer Assignment.

Most features live in sibling modules, each registering its own
ConversationHandler(s) into main() via register(app) — see the module
docstrings and assign/CLAUDE.md's Architecture section for the full list
(new_assignment.py, receive_tasks.py, lookup_reference.py, valuer_tasks.py,
job_distribution.py, dlv_batch.py, dlv_tasks.py, fetch_tasks.py, auto_fetch.py,
refresh_auth.py, sectional_properties.py, morning_briefing.py).

bot.py itself still owns Bulk Export (BE, not yet extracted), plus
cmd_start/cmd_help, saved-valuers management, daemon control, token/
error-report status, cmd_restart, and main()'s wiring.
"""

import io
import json
import asyncio
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
import requests
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ardhisasa_auth import (
    AuthTokens,
    build_session,
)

from common import (
    BASE_URL,
    BTN_BULK_EXPORT,
    BTN_CANCEL,
    BTN_DAEMON,
    BTN_DELETE,
    BTN_ERROR_REPORT,
    BTN_EXPORT_STATUS,
    BTN_HELP,
    BTN_RESTART,
    BTN_TOKEN_STATUS,
    BTN_VALUERS,
    CPARAMS_DLV,
    CRED_LABELS,
    CRED_MAP,
    DATA_DIR,
    SAVED_VALUERS_FILE,
    _any_valid_tokens,
    _atomic_json_write,
    _CANCEL_FILTER,
    _ensure_data_dir,
    _jwt_exp,
    _load_tokens_raw,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    load_saved_valuers,
    logger,
    not_cancel,
)
from email_service import _send_bulk_export_email
from job_distribution import _JD_STATUS
from token_rotator import _AllTokensExhausted, _TokenRotator
import auto_fetch
import dlv_batch
import dlv_tasks
import morning_briefing
import fetch_tasks
import job_distribution
import lookup_reference
import new_assignment
import receive_tasks
import refresh_auth
import sectional_properties
import valuer_tasks

load_dotenv()

BOT_TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ──────────────────────────────────────────────────────────
# Persistent storage
# ──────────────────────────────────────────────────────────
SAVED_TASK_BATCHES_FILE = os.path.join(DATA_DIR, "saved_task_batches.json")
SAVED_SCHEDULES_FILE    = os.path.join(DATA_DIR, "saved_schedules.json")
SAVED_BULK_EXPORT_SCHED_FILE    = os.path.join(DATA_DIR, "saved_bulk_export_schedule.json")
SAVED_BULK_EXPORT_PARTIAL_FILE  = os.path.join(DATA_DIR, "saved_bulk_export_partial.json")

DAEMON_SCRIPT = os.path.join(os.path.dirname(__file__), "token_refresh_daemon.py")
DAEMON_PID_FILE = os.path.join(DATA_DIR, "daemon.pid")
DAEMON_LOG_FILE = os.path.join(DATA_DIR, "daemon.log")

# (_safe_err, SAVED_SECTIONAL_CONFIG_FILE, load_sectional_config,
#  save_sectional_config all live in common.py now — imported above)


# (S enum + Session dataclass live in new_assignment.py — imported by
#  main() at the point of registration)


# (RS enum + RTSession live in receive_tasks.py — imported by main() at
#  the point of registration)


# (AF enum lives in auto_fetch.py — imported by main() at the point of
#  registration)


# (DB enum + DBSession live in dlv_batch.py — imported by main() at the
#  point of registration)


# (AS enum + AuthSession + _get_auth_sess live in refresh_auth.py —
#  imported by main() at the point of registration)


# (FT enum + FTSession + _get_ft_sess live in fetch_tasks.py — imported by
#  main() at the point of registration)


# (JD enum + JDSession live in job_distribution.py — imported by main() at
#  the point of registration)


# (LU enum + LUSession live in lookup_reference.py — imported by main() at
#  the point of registration)


# (VT enum + VTSession live in valuer_tasks.py — imported by main() at
#  the point of registration)


# States — Bulk Export conversation
# ──────────────────────────────────────────────────────────
class BE(Enum):
    REPORT_TYPE = auto()  # pick Ardhisasa or Ardhipay report
    COUNTY    = auto()   # pick county
    EMAIL     = auto()   # ask for recipient email address
    SCHEDULE  = auto()   # pick repeat interval
    CONFIRM   = auto()   # confirm → kick off background export


# (DT enum lives in dlv_tasks.py — imported by main() at the point of
#  registration)


# (SC enum lives in sectional_properties.py — imported by main() at the
#  point of registration)


# (MB enum lives in morning_briefing.py — imported by main() at the point
#  of registration)


# County → list of registry names as they appear in the API response
_BE_COUNTY_REGISTRIES: Dict[str, List[str]] = {
    "NAIROBI":   ["CENTRAL", "NAIROBI"],
    "KIAMBU":    ["KIAMBU", "LIMURU", "THIKA", "RUIRU", "GITHUNGURI"],
    "MURANGA":   ["MURANGA", "KANDARA", "MARAGUA", "KANGEMA"],
    "MOMBASA":   ["MOMBASA", "COAST"],
    "NAKURU":    ["NAKURU", "NAIVASHA", "GILGIL", "MOLO"],
    "KISUMU":    ["KISUMU", "MASENO"],
    "NYERI":     ["NYERI", "KARATINA", "OTHAYA"],
    "MACHAKOS":  ["MACHAKOS", "MAVOKO"],
    "KAJIADO":   ["KAJIADO", "NGONG"],
    "MERU":      ["MERU"],
    "LAIKIPIA":  ["LAIKIPIA", "NANYUKI"],
    "EMBU":      ["EMBU"],
}

_BE_COUNTY_LABELS: Dict[str, str] = {
    "NAIROBI":   "🌆 Nairobi",
    "KIAMBU":    "🏙 Kiambu",
    "MURANGA":   "🏡 Murang'a",
    "MOMBASA":   "🌊 Mombasa",
    "NAKURU":    "🌿 Nakuru",
    "KISUMU":    "🐟 Kisumu",
    "NYERI":     "🏔 Nyeri",
    "MACHAKOS":  "🦏 Machakos",
    "KAJIADO":   "🌄 Kajiado",
    "MERU":      "🌲 Meru",
    "LAIKIPIA":  "🦁 Laikipia",
    "EMBU":      "🌱 Embu",
}


# Ardhisasa report — restricted county set with specific registry lists
_AR_COUNTY_REGISTRIES: Dict[str, List[str]] = {
    "NAIROBI":  ["NAIROBI"],
    "MOMBASA":  ["MOMBASA", "COAST"],
    "ISIOLO":   ["ISIOLO"],
    "MURANGA":  ["MURANGA", "KANDARA", "MARAGUA", "KANGEMA"],
}

_AR_COUNTY_LABELS: Dict[str, str] = {
    "NAIROBI":  "🌆 Nairobi",
    "MOMBASA":  "🌊 Mombasa",
    "ISIOLO":   "🌵 Isiolo",
    "MURANGA":  "🏡 Murang'a",
}


_BE_SCHEDULE_OPTIONS: List[tuple] = [
    ("Every Day",      86_400),
    ("Every Week",     604_800),
    ("Bi-Monthly",     1_209_600),   # every 2 weeks
    ("Monthly",        2_592_000),   # 30 days
    ("Every 2 Months", 5_184_000),   # 60 days
    ("Run Once",       0),
]

# Per-chat export status tracker — keyed by chat_id
# Each entry: {phase, started_at, total, pages_done, total_pages,
#              details_done, details_total, errors, completed_at, rows, error_msg}
_BE_STATUS: Dict[int, dict] = {}

# (_JD_STATUS lives in job_distribution.py — imported above; cmd_export_status
#  below reads both it and _BE_STATUS, since Bulk Export isn't extracted yet)


@dataclass
class BESession:
    report_type:      str = ""   # "ardhisasa" or "ardhipay"
    county:           str = ""
    registries:       List[str] = field(default_factory=list)
    email:            str = ""
    schedule_seconds: int = 0   # 0 = run once
    cred_type:        str = ""


def _get_be_sess(ctx: ContextTypes.DEFAULT_TYPE) -> BESession:
    if "be_session" not in ctx.user_data:
        ctx.user_data["be_session"] = BESession()
    return ctx.user_data["be_session"]


# (CRED_MAP, CRED_LABELS, _cred_keyboard live in common.py — imported above)

# (New Assignment — get_sess, parse_refs, _confirm_keyboard,
#  _do_valuer_search, _show_valuer_keyboard, OCR helpers (_REF_RE,
#  _extract_refs_from_text, ocr_extract_refs) — live in new_assignment.py,
#  imported by main() at the point of registration)

# ──────────────────────────────────────────────────────────
# /start  /help
# ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        "🏛 *Ardhisasa Valuation Bot*\n\n"
        "Use the buttons below to get started:",
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


# (cmd_assignments — /assignments history viewer — lives in
#  new_assignment.py, imported by main() at the point of registration)


# ──────────────────────────────────────────────────────────
# /valuers  /delete_valuer — manage saved valuers
# ──────────────────────────────────────────────────────────
async def cmd_valuers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    valuers = load_saved_valuers()
    if not valuers:
        await update.message.reply_text(
            "📭 No saved valuers yet.\n"
            "They are saved automatically after a successful assignment.",
            reply_markup=_main_menu(),
        )
        return
    lines = [
        f"{i+1}. *{v['name']}* — ID: `{v['uid']}` | Acct: `{v['account_number']}`"
        for i, v in enumerate(valuers)
    ]
    await update.message.reply_text(
        "📋 *Saved Valuers:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


async def cmd_delete_valuer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    valuers = load_saved_valuers()
    if not valuers:
        await update.message.reply_text(
            "📭 No saved valuers to delete.",
            reply_markup=_main_menu(),
        )
        return
    rows = [
        [InlineKeyboardButton(f"🗑 {v['name']}", callback_data=f"del:{i}")]
        for i, v in enumerate(valuers)
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="del:cancel")])
    await update.message.reply_text(
        "Select a valuer to delete:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def recv_delete_valuer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")[1]
    if data == "cancel":
        await query.edit_message_text("Deletion cancelled.")
        return
    idx = int(data)
    valuers = load_saved_valuers()
    if idx >= len(valuers):
        await query.edit_message_text("⚠️ Valuer not found.")
        return
    removed = valuers.pop(idx)
    _atomic_json_write(SAVED_VALUERS_FILE, valuers, indent=2)
    await query.edit_message_text(
        f"🗑 Removed *{removed['name']}* from saved valuers.", parse_mode="Markdown"
    )


# (New Assignment — cmd_assign, recv_input_method, recv_photo,
#  recv_photo_done, recv_confirm_refs, recv_refs,
#  _check_assignments_and_proceed, _proceed_to_valuer_pick,
#  recv_reassign_confirm, recv_valuer_source, recv_valuer_name,
#  recv_cred_choice, recv_otp, recv_valuer_select, recv_confirm,
#  _lookup_one_ref, _post_assignment_report — live in new_assignment.py,
#  imported by main() at the point of registration)

# (Receive Tasks — RS enum, RTSession, persistence helpers, API helpers,
#  _do_assign_tasks, the scheduled job, all conversation handlers,
#  cmd_schedules, cmd_task_batches — live in receive_tasks.py, imported
#  by main() at the point of registration)


# (AF Results — cmd_af_results, recv_af_result_detail — live in
#  auto_fetch.py, bundled with Auto Fetch since it only displays what
#  Auto Fetch's own background job persisted)

# ──────────────────────────────────────────────────────────
# Token refresh daemon — process management
# ──────────────────────────────────────────────────────────

def _daemon_read_pid() -> Optional[int]:
    """Return the PID stored in the pid file, or None if missing/invalid."""
    try:
        with open(DAEMON_PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _daemon_running() -> bool:
    """
    Return True if the daemon process is alive.

    A container rebuild resets PIDs from scratch, so a stale daemon.pid left
    over in the (persistent) data volume can coincidentally collide with an
    unrelated live process — e.g. the bot's own PID. Matching /proc/<pid>/cmdline
    against the daemon script guards against that false positive on Linux;
    elsewhere it falls back to a plain liveness check.
    """
    pid = _daemon_read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)   # signal 0 = probe only, no actual signal sent
    except (ProcessLookupError, PermissionError):
        return False

    cmdline_path = f"/proc/{pid}/cmdline"
    if os.path.exists(cmdline_path):
        try:
            with open(cmdline_path, "rb") as f:
                cmdline = f.read().decode(errors="ignore")
            return os.path.basename(DAEMON_SCRIPT) in cmdline
        except OSError:
            pass
    return True


def _daemon_start() -> Tuple[bool, str]:
    """
    Launch token_refresh_daemon.py in a detached process.
    Uses start_new_session=True so it is not killed when the bot receives SIGTERM.
    stdout/stderr are appended to DAEMON_LOG_FILE.
    Returns (success, message).
    """
    if _daemon_running():
        return False, f"Already running (PID {_daemon_read_pid()})."

    if not os.path.exists(DAEMON_SCRIPT):
        return False, f"Script not found: {DAEMON_SCRIPT}"

    _ensure_data_dir()
    log_fh = open(DAEMON_LOG_FILE, "a")
    proc = subprocess.Popen(
        [sys.executable, "-u", DAEMON_SCRIPT],
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,   # detach from bot's process group
        close_fds=True,
    )
    log_fh.close()   # child has its own fd; parent's copy is no longer needed
    with open(DAEMON_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    logger.info("Token refresh daemon started (PID %d)", proc.pid)
    return True, f"Started (PID {proc.pid}). Logs → `{DAEMON_LOG_FILE}`"


def _daemon_stop() -> Tuple[bool, str]:
    """Send SIGTERM to the daemon. Returns (success, message)."""
    if not _daemon_running():
        return False, "Daemon is not running."
    pid = _daemon_read_pid()
    try:
        os.kill(pid, signal.SIGTERM)
        # Give it a moment, then confirm it's gone
        for _ in range(10):
            time.sleep(0.3)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        try:
            os.remove(DAEMON_PID_FILE)
        except FileNotFoundError:
            pass
        logger.info("Token refresh daemon stopped (PID %d)", pid)
        return True, f"Daemon (PID {pid}) stopped."
    except Exception as e:
        return False, f"Failed to stop daemon: {e}"


def _daemon_status_text() -> str:
    running = _daemon_running()
    pid     = _daemon_read_pid()
    if running:
        return f"🟢 *Running* (PID {pid})"
    elif pid:
        return "🔴 *Not running* (stale PID file — process died)"
    else:
        return "🔴 *Not running*"


def _daemon_keyboard() -> InlineKeyboardMarkup:
    running = _daemon_running()
    rows = []
    if running:
        rows.append([InlineKeyboardButton("⏹ Stop Daemon",   callback_data="daemon:stop")])
    else:
        rows.append([InlineKeyboardButton("▶️ Start Daemon",  callback_data="daemon:start")])
    rows.append([InlineKeyboardButton("🔁 Refresh Status", callback_data="daemon:status")])
    return InlineKeyboardMarkup(rows)


async def cmd_daemon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        f"🔄 *Token Refresh Daemon*\n\n"
        f"Status: {_daemon_status_text()}\n\n"
        f"The daemon watches the token cache and silently refreshes "
        f"each token *5 minutes before it expires*.\n"
        f"Logs are written to `data/daemon.log`.",
        parse_mode="Markdown",
        reply_markup=_daemon_keyboard(),
    )


async def cmd_token_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)

    raw   = _load_tokens_raw()
    now   = time.time()
    lines = []

    for cred_type, label in CRED_LABELS.items():
        entry = raw.get(cred_type)
        if not entry:
            lines.append(f"{label}\n  ⚫ No token cached")
            continue

        exp = entry.get("expires_at") or _jwt_exp(entry.get("jwt", ""))
        if not exp:
            lines.append(f"{label}\n  ⚠️ Expiry unreadable")
            continue

        secs_left = exp - now
        exp_str   = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if secs_left <= 0:
            lines.append(f"{label}\n  🔴 Expired — {exp_str}")
        elif secs_left < 10 * 60:
            mins = int(secs_left // 60)
            lines.append(f"{label}\n  🟡 Expires in {mins}m — {exp_str}")
        else:
            hrs  = int(secs_left // 3600)
            mins = int((secs_left % 3600) // 60)
            time_str = f"{hrs}h {mins}m" if hrs else f"{mins}m"
            lines.append(f"{label}\n  🟢 Valid — expires in {time_str} ({exp_str})")

    text = "🔒 *Token Status*\n\n" + "\n\n".join(lines)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_main_menu())


_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2},\d{3} \[(WARNING|ERROR|CRITICAL)\] [\w.]+: (.*)$"
)
_LOG_NORMALIZE_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\d+"
)


def _read_log_records(max_lines: int = 50_000) -> List[tuple]:
    """Parse data/bot.log into (date_str, level, message) tuples for WARNING+ lines."""
    log_path = os.path.join(DATA_DIR, "bot.log")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
    except FileNotFoundError:
        return []

    records = []
    for line in lines:
        m = _LOG_LINE_RE.match(line)
        if m:
            records.append((m.group(1), m.group(2), m.group(3)))
    return records


async def cmd_error_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)

    records = _read_log_records()
    if not records:
        await update.message.reply_text(
            "✅ No warnings or errors logged yet (`data/bot.log`).",
            reply_markup=_main_menu(),
        )
        return

    from collections import Counter, defaultdict

    by_day_level: dict = defaultdict(lambda: {"WARNING": 0, "ERROR": 0, "CRITICAL": 0})
    categories = Counter()
    for date_str, level, message in records:
        by_day_level[date_str][level] += 1
        category = _LOG_NORMALIZE_RE.sub("#", message)[:80]
        categories[category] += 1

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        await update.message.reply_text(
            "❌ `matplotlib` is not installed — cannot render the chart.",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return

    days = sorted(by_day_level.keys())
    warnings = [by_day_level[d]["WARNING"] for d in days]
    errors   = [by_day_level[d]["ERROR"] + by_day_level[d]["CRITICAL"] for d in days]

    top_categories = categories.most_common(8)[::-1]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 9))

    ax1.bar(days, warnings, label="Warning", color="#f1c40f")
    ax1.bar(days, errors, bottom=warnings, label="Error/Critical", color="#e74c3c")
    ax1.set_title("Log entries per day")
    ax1.set_ylabel("Count")
    ax1.legend()
    ax1.tick_params(axis="x", rotation=45)

    if top_categories:
        labels = [c for c, _ in top_categories]
        counts = [n for _, n in top_categories]
        ax2.barh(labels, counts, color="#3498db")
        ax2.set_title("Top error/warning categories")
        ax2.set_xlabel("Count")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)

    total = sum(warnings) + sum(errors)
    caption = (
        f"📉 *Error Report* — {total:,} entries across {len(days)} day(s)\n"
        f"Range: `{days[0]}` → `{days[-1]}`"
    )
    await update.message.reply_photo(
        photo=buf, caption=caption, parse_mode="Markdown", reply_markup=_main_menu()
    )


async def recv_daemon_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "start":
        ok, msg = _daemon_start()
    elif action == "stop":
        ok, msg = _daemon_stop()
    else:
        ok, msg = True, "Status refreshed."

    status = _daemon_status_text()
    await query.edit_message_text(
        f"🔄 *Token Refresh Daemon*\n\n"
        f"Status: {status}\n\n"
        f"{'✅' if ok else '❌'} {msg}",
        parse_mode="Markdown",
        reply_markup=_daemon_keyboard(),
    )


# ──────────────────────────────────────────────────────────
# /restart
# ──────────────────────────────────────────────────────────
async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    await update.message.reply_text("🔁 Restarting bot… back in a moment.")
    # Schedule execv in a daemon thread so the event loop can finish delivering
    # the reply above before the process image is replaced.  os.execv never
    # returns, so dropping the thread is intentional.
    def _do_restart():
        time.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    t = threading.Thread(target=_do_restart, daemon=True)
    t.start()


# ──────────────────────────────────────────────────────────
# Fallback (unexpected input during conversation)
# ──────────────────────────────────────────────────────────
# (fallback() lives in common.py — imported above)


# (DLV Batch parsing/valuer-resolution, batch item processing, and the
#  5-minute repeating job all live in dlv_batch.py — imported by main()
#  at the point of registration)


# (Auto Fetch — _AF_INTERVALS, load_auto_fetch_schedule/
#  save_auto_fetch_schedule/clear_auto_fetch_schedule, load_af_results/
#  persist_af_result, _af_interval_keyboard, cmd_auto_fetch, recv_af_*,
#  _auto_fetch_job — all live in auto_fetch.py)


# (Morning Briefing — _send_briefing, _run_morning_briefing,
#  _morning_briefing_job, _schedule_morning_briefing, _mb_* keyboards,
#  cmd_briefing, recv_mb_* — all live in morning_briefing.py)

# (Sectional Properties command handlers — cmd_sectional, recv_sc_action,
#  recv_sc_name, recv_sc_select, recv_sc_cred — live in sectional_properties.py)


# (DLV Queue viewer — _dlv_queue_keyboard, cmd_dlv_queue,
#  recv_dlv_queue_action, _dlv_batch_interval — lives in dlv_batch.py)


# ──────────────────────────────────────────────────────────
# Implementor Tasks & DLV Tasks — view-only checklist
# ──────────────────────────────────────────────────────────
# (_any_valid_tokens, _ft_headers, _extract_assessor live in common.py /
#  dlv_core.py — imported above)

# (Fetch Tasks — _load_fetch_tasks, cmd_fetch_tasks, recv_ft_*,
#  _ft_do_fetch, _ft_show_results, etc. — lives in fetch_tasks.py.
#  _date_cutoff_str/_within_days, _ft_*_keyboard/_sectional_keyboard
#  moved to common.py since Auto Fetch/Valuer Tasks also use them.)


# (DLV Batch conversation handlers — cmd_dlv_batch, recv_db_input,
#  recv_db_confirm, _run_dlv_batch_bg — live in dlv_batch.py)

# (Refresh Auth conversation handlers — _auth_cred_keyboard, cmd_auth,
#  recv_auth_cred, recv_auth_force, _auth_trigger_login, recv_auth_otp —
#  live in refresh_auth.py)

# ──────────────────────────────────────────────────────────
# Bulk Export — NAIROBI Completed stamp-duty applications
# ──────────────────────────────────────────────────────────

_BE_LIST_URL   = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application"
_BE_DETAIL_URL = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application/detail-view"
_BE_REPORT_URL = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/office-reports/get-current-office-report"
_BE_PAGE_SIZE  = 10   # API default
_BE_LIST_WORKERS        = 5
_BE_DETAIL_WORKERS      = 5
_BE_MAX_RETRIES         = 3
_BE_TOKEN_ROTATE_DELAY  = 10   # seconds to wait before retrying with a new token
_MAX_FETCH_RETRIES      = 5    # max 429/5xx retries before aborting a fetch loop


# (_AllTokensExhausted + _TokenRotator live in token_rotator.py — imported
#  above, shared by Bulk Export and Job Distribution)


_EXCEL_COLUMNS = [
    "Filter",
    "Reference Number",
    "Parcel Number",
    "Registry",
    "County",
    "Valuation Request Type",
    "Application Status",
    "Application Date Created",
    "Valuation Officer",
    "Date of Valuation",
    "Valuer Total Land Value (KES)",
    "Harmonized Total Land Value (KES)",
    "Document URL",
    "Combined Report",
    "Enrich Error",
]


def _be_headers(tokens: AuthTokens) -> dict:
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }


def load_be_schedule() -> Optional[Dict]:
    try:
        with open(SAVED_BULK_EXPORT_SCHED_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_be_schedule(cfg: Dict) -> None:
    _atomic_json_write(SAVED_BULK_EXPORT_SCHED_FILE, cfg, indent=2)


def clear_be_schedule() -> None:
    try:
        os.remove(SAVED_BULK_EXPORT_SCHED_FILE)
    except FileNotFoundError:
        pass


def load_be_partial() -> Optional[Dict]:
    try:
        with open(SAVED_BULK_EXPORT_PARTIAL_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_be_partial(county: str, registries: List[str], rows: List[dict], done_ids: List[str]) -> None:
    _atomic_json_write(SAVED_BULK_EXPORT_PARTIAL_FILE, {
        "saved_at":   datetime.now().isoformat(),
        "county":     county,
        "registries": registries,
        "rows":       rows,
        "done_ids":   done_ids,
    })


def clear_be_partial() -> None:
    try:
        os.remove(SAVED_BULK_EXPORT_PARTIAL_FILE)
    except FileNotFoundError:
        pass


def _be_schedule_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for label, secs in _BE_SCHEDULE_OPTIONS:
        rows.append([InlineKeyboardButton(label, callback_data=f"be_sched:{secs}")])
    return InlineKeyboardMarkup(rows)


def _be_report_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏛 Ardhisasa Report", callback_data="be_rtype:ardhisasa")],
        [InlineKeyboardButton("💳 Ardhipay Report",  callback_data="be_rtype:ardhipay")],
    ])


def _be_county_keyboard() -> InlineKeyboardMarkup:
    items = list(_BE_COUNTY_LABELS.items())
    rows = []
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(items[i][1], callback_data=f"be_county:{items[i][0]}")]
        if i + 1 < len(items):
            row.append(InlineKeyboardButton(items[i+1][1], callback_data=f"be_county:{items[i+1][0]}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _ar_county_keyboard() -> InlineKeyboardMarkup:
    items = list(_AR_COUNTY_LABELS.items())
    rows = []
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(items[i][1], callback_data=f"be_county:{items[i][0]}")]
        if i + 1 < len(items):
            row.append(InlineKeyboardButton(items[i+1][1], callback_data=f"be_county:{items[i+1][0]}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# (_be_cred_keyboard lives in common.py, shared by Job Distribution,
#  Lookup Reference, and Valuer Tasks — Bulk Export, still inline here,
#  uses its own _be_report_type_keyboard/_be_county_keyboard instead)


def _be_fetch_page(sess: requests.Session, headers: dict, page: int) -> dict:
    """Fetch one list page. Returns the parsed JSON dict."""
    params = {
        "filter":       "Completed",
        "role":         "DLV",
        "request_type": "STAMP_DUTY",
        "search":       "",
        "page":         page,
    }
    resp = sess.get(_BE_LIST_URL, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _be_fetch_detail(sess: requests.Session, rotator: "_TokenRotator", app_id: str) -> dict:
    """
    Fetch detail view for one application ID.
    On 403 rotates to the next valid token (with a short pause).
    Raises _AllTokensExhausted when no more tokens remain.
    """
    retries = 0
    while True:
        tokens = rotator.current()
        if tokens is None:
            raise _AllTokensExhausted(f"All tokens exhausted fetching {app_id}")
        headers = {**_be_headers(tokens), "cparams": CPARAMS_DLV}
        resp = sess.get(_BE_DETAIL_URL, headers=headers, params={"request_id": app_id}, timeout=30)
        if resp.status_code == 403:
            new_tokens = rotator.rotate(tokens)
            if new_tokens is None:
                raise _AllTokensExhausted(f"All tokens exhausted (403) fetching {app_id}")
            time.sleep(_BE_TOKEN_ROTATE_DELAY)
            continue
        if resp.status_code == 404:
            # Record no longer exists — return empty dict so caller marks it cleanly
            logger.debug("_be_fetch_detail: 404 for app_id=%s — record not found", app_id)
            return {}
        if resp.status_code in (429, 500, 502, 503, 504):
            retries += 1
            if retries >= _MAX_FETCH_RETRIES:
                raise RuntimeError(
                    f"_be_fetch_detail: too many transient errors (HTTP {resp.status_code}) "
                    f"for app_id={app_id}"
                )
            time.sleep(2)
            continue
        resp.raise_for_status()
        return resp.json()


def _be_fetch_office_report(sess: requests.Session, rotator: "_TokenRotator", app_id: str) -> dict:
    """Fetch the office report for one application. Returns {} on any error (non-critical)."""
    retries = 0
    while True:
        tokens = rotator.current()
        if tokens is None:
            return {}
        headers = {**_be_headers(tokens), "cparams": CPARAMS_DLV}
        try:
            resp = sess.get(_BE_REPORT_URL, headers=headers, params={"request_id": app_id}, timeout=30)
            if resp.status_code == 403:
                new_tokens = rotator.rotate(tokens)
                if new_tokens is None:
                    return {}
                time.sleep(_BE_TOKEN_ROTATE_DELAY)
                continue
            if resp.status_code in (429, 502, 503, 504):
                retries += 1
                if retries >= _MAX_FETCH_RETRIES:
                    logger.warning("_be_fetch_office_report: giving up after %d retries for app_id=%s", retries, app_id)
                    return {}
                time.sleep(2)
                continue
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return resp.json()
        except (_AllTokensExhausted, Exception):
            return {}


def _be_fetch_full_record(sess: requests.Session, rotator: "_TokenRotator", app_id: str) -> dict:
    """Fetch detail + office report for one record and return the merged row dict."""
    detail = _be_fetch_detail(sess, rotator, app_id)   # raises on error
    report = _be_fetch_office_report(sess, rotator, app_id)
    row    = _be_extract_row(detail)
    docs   = report.get("combined_document") or []
    row["Combined Report"] = docs[0].get("document", "") if docs else ""
    return row


def _be_extract_row(detail: dict) -> dict:
    """Extract the target columns from a detail-view response dict."""
    # Valuation Officer from actors[]
    vo_name = ""
    vo_date = ""
    for actor in (detail.get("actors") or []):
        role = (actor.get("role") or "").upper()
        if role in ("VALUATION OFFICER", "VO"):
            vo_name = (actor.get("user_details") or {}).get("names", "")
            vo_date = actor.get("date_assigned", "")
            if role == "VALUATION OFFICER":
                break   # prefer exact match

    # Consideration amount from external_process_details
    ext = detail.get("external_process_details") or {}
    land_value = ext.get("consideration_amount", "")

    # Document URL: prefer VALUATION CERTIFICATE in process_documents
    doc_url = ""
    for pdoc in (detail.get("process_documents") or []):
        if (pdoc.get("document_name") or "").upper() == "VALUATION CERTIFICATE":
            doc_url = pdoc.get("document", "")
            break
    if not doc_url:
        app_docs = detail.get("application_documents") or []
        if app_docs:
            doc_url = app_docs[0].get("document", "")

    return {
        "Filter":                          "Completed",
        "Reference Number":                detail.get("reference_number", ""),
        "Parcel Number":                   detail.get("parcel_number", ""),
        "Registry":                        detail.get("registry", ""),
        "County":                          detail.get("county", ""),
        "Valuation Request Type":          detail.get("valuation_request_type", ""),
        "Application Status":              detail.get("application_status", ""),
        "Application Date Created":        detail.get("date_created", ""),
        "Valuation Officer":               vo_name,
        "Date of Valuation":               vo_date,
        "Valuer Total Land Value (KES)":   land_value,
        "Harmonized Total Land Value (KES)": detail.get("harmonized_total_land_value", ""),
        "Document URL":                    doc_url,
        "Combined Report":                 "",
        "Enrich Error":                    "",
    }


def _be_build_excel(rows: List[dict]) -> bytes:
    """Build the formatted Excel workbook and return the raw bytes."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Stamp Duty Valuations"

    header_font  = Font(bold=True)
    header_fill  = PatternFill("solid", fgColor="BDD7EE")
    date_fmt     = "YYYY-MM-DD HH:MM:SS"
    number_fmt   = "#,##0"
    date_cols    = {"Application Date Created", "Date of Valuation"}
    number_cols  = {"Valuer Total Land Value (KES)", "Harmonized Total Land Value (KES)"}

    # Write header
    ws.append(_EXCEL_COLUMNS)
    for col_idx, col_name in enumerate(_EXCEL_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font  = header_font
        cell.fill  = header_fill

    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes    = "A2"

    # Write data rows
    for row_data in rows:
        row_vals = [row_data.get(col, "") for col in _EXCEL_COLUMNS]
        ws.append(row_vals)
        row_idx = ws.max_row
        for col_idx, col_name in enumerate(_EXCEL_COLUMNS, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            if col_name in date_cols and cell.value:
                cell.number_format = date_fmt
            elif col_name in number_cols and cell.value not in ("", None, "FETCH_ERROR"):
                try:
                    cell.value         = float(str(cell.value).replace(",", "").strip())
                    cell.number_format = number_fmt
                except (ValueError, TypeError):
                    pass

    # Auto-fit column widths (min 15, max 50)
    for col_idx, col_name in enumerate(_EXCEL_COLUMNS, start=1):
        col_letter = get_column_letter(col_idx)
        max_len    = len(col_name)
        for row in ws.iter_rows(min_col=col_idx, max_col=col_idx, min_row=2):
            val = str(row[0].value or "")
            if len(val) > max_len:
                max_len = len(val)
        ws.column_dimensions[col_letter].width = max(15, min(50, max_len + 2))

    # ── Summary sheet ─────────────────────────────────────────
    ws2 = wb.create_sheet("Summary")

    def _write_section_header(ws, row: int, text: str):
        cell = ws.cell(row=row, column=1, value=text)
        cell.font = Font(bold=True, size=12)
        cell.fill = PatternFill("solid", fgColor="BDD7EE")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)

    def _write_col_headers(ws, row: int, *headers):
        for col, h in enumerate(headers, start=1):
            c = ws.cell(row=row, column=col, value=h)
            c.font = header_font
            c.fill = PatternFill("solid", fgColor="D9E1F2")

    # ── Collect stats from rows ───────────────────────────────
    from collections import defaultdict

    monthly: dict  = defaultdict(int)    # "YYYY-MM" → count
    yearly:  dict  = defaultdict(int)    # "YYYY"    → count
    vo_tasks: dict = defaultdict(int)    # officer name → count
    total_land_value = 0.0
    total_harmonized = 0.0

    for r in rows:
        # Date of Valuation — monthly/yearly distribution
        dov = str(r.get("Date of Valuation") or "")
        if len(dov) >= 7:
            monthly[dov[:7]] += 1
        if len(dov) >= 4:
            yearly[dov[:4]] += 1

        # Valuation Officer task count
        vo = (r.get("Valuation Officer") or "").strip()
        if vo and vo != "FETCH_ERROR":
            vo_tasks[vo] += 1

        # Sum land values
        for key, target in (
            ("Valuer Total Land Value (KES)", "land"),
            ("Harmonized Total Land Value (KES)", "harm"),
        ):
            raw = r.get(key, "")
            if raw not in ("", None, "FETCH_ERROR"):
                try:
                    val = float(str(raw).replace(",", "").strip())
                    if key == "Valuer Total Land Value (KES)":
                        total_land_value += val
                    else:
                        total_harmonized += val
                except (ValueError, TypeError):
                    pass

    cur_row = 1

    # ── Section 1: Totals ─────────────────────────────────────
    _write_section_header(ws2, cur_row, "Overall Totals")
    cur_row += 1
    for label, value in (
        ("Total Records",                        len(rows)),
        ("Valuer Total Land Value (KES)",        total_land_value),
        ("Harmonized Total Land Value (KES)",    total_harmonized),
    ):
        ws2.cell(row=cur_row, column=1, value=label).font = Font(bold=True)
        c = ws2.cell(row=cur_row, column=2, value=value)
        if isinstance(value, float):
            c.number_format = number_fmt
        cur_row += 1

    cur_row += 1  # blank row

    # ── Section 2: Monthly distribution ──────────────────────
    _write_section_header(ws2, cur_row, "Monthly Distribution (Date of Valuation)")
    cur_row += 1
    _write_col_headers(ws2, cur_row, "Month (YYYY-MM)", "Count")
    cur_row += 1
    for month in sorted(monthly):
        ws2.cell(row=cur_row, column=1, value=month)
        ws2.cell(row=cur_row, column=2, value=monthly[month])
        cur_row += 1

    cur_row += 1  # blank row

    # ── Section 3: Yearly distribution ───────────────────────
    _write_section_header(ws2, cur_row, "Yearly Distribution (Date of Valuation)")
    cur_row += 1
    _write_col_headers(ws2, cur_row, "Year", "Count")
    cur_row += 1
    for year in sorted(yearly):
        ws2.cell(row=cur_row, column=1, value=year)
        ws2.cell(row=cur_row, column=2, value=yearly[year])
        cur_row += 1

    cur_row += 1  # blank row

    # ── Section 4: Valuation Officer task counts ──────────────
    _write_section_header(ws2, cur_row, "Tasks per Valuation Officer")
    cur_row += 1
    _write_col_headers(ws2, cur_row, "Valuation Officer", "Tasks")
    cur_row += 1
    for officer, count in sorted(vo_tasks.items(), key=lambda x: -x[1]):
        ws2.cell(row=cur_row, column=1, value=officer)
        ws2.cell(row=cur_row, column=2, value=count)
        cur_row += 1

    # Auto-fit Summary sheet columns
    for col_idx in (1, 2):
        col_letter = get_column_letter(col_idx)
        max_len = 20
        for row in ws2.iter_rows(min_col=col_idx, max_col=col_idx):
            val = str(row[0].value or "")
            if len(val) > max_len:
                max_len = len(val)
        ws2.column_dimensions[col_letter].width = max(20, min(50, max_len + 2))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# (_send_bulk_export_email lives in common.py — imported above)


def _bulk_export_run(tokens: AuthTokens, chat_id: int, email: str, bot, loop,
                     registries: List[str] = None, county: str = "",
                     report_type: str = "ardhipay") -> None:
    """
    Full synchronous export worker — runs in a background thread.

    Behaviour:
    - Resumes from a saved partial checkpoint when one exists for the same county/registries.
    - Sorts all records by Application Date Created (ascending) so resume is date-ordered.
    - Rotates tokens on 403: tries every valid credential before giving up.
    - On full token exhaustion saves a partial checkpoint and notifies the user.
    - On success clears the partial checkpoint and sends the Excel file.
    """
    def _tg(text: str):
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id, text, parse_mode="Markdown"),
            loop,
        ).result(timeout=15)

    def _set_status(**kwargs):
        _BE_STATUS.setdefault(chat_id, {}).update(kwargs)

    _BE_STATUS[chat_id] = {
        "phase":          "fetching pages",
        "started_at":     datetime.now(),
        "total":          None,
        "pages_done":     1,
        "total_pages":    None,
        "details_done":   0,
        "details_total":  None,
        "errors":         0,
        "completed_at":   None,
        "rows":           None,
        "error_msg":      None,
    }

    # ── Build token rotator from all currently-valid credentials ──
    token_pairs = [
        (ct, get_valid_tokens(ct))
        for ct in CRED_MAP
        if get_valid_tokens(ct)
    ]
    # Put the originally-chosen token first
    token_pairs.sort(key=lambda p: 0 if p[1] is tokens else 1)
    rotator = _TokenRotator(token_pairs)

    sess = build_session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=_BE_DETAIL_WORKERS,
        pool_maxsize=_BE_DETAIL_WORKERS,
        max_retries=0,
    )
    sess.mount("https://", adapter)
    sess.mount("http://",  adapter)
    list_headers = _be_headers(tokens)
    if report_type == "ardhisasa":
        list_headers = {**list_headers, "cparams": CPARAMS_DLV, "Content-Type": "application/json"}

    try:
        # ── Step 1: fetch list pages ───────────────────────────────
        first_page  = _be_fetch_page(sess, list_headers, 1)
        total       = first_page.get("count", 0)
        results     = list(first_page.get("results") or [])
        page_size   = len(results) if results else _BE_PAGE_SIZE
        if page_size == 0:
            page_size = _BE_PAGE_SIZE
        total_pages = max(1, -(-total // page_size))

        logger.info("Bulk export: count=%d, page_size=%d, total_pages=%d", total, page_size, total_pages)
        _set_status(total=total, total_pages=total_pages, pages_done=1)

        if total_pages > 1:
            with ThreadPoolExecutor(max_workers=_BE_LIST_WORKERS) as pool:
                futures = {pool.submit(_be_fetch_page, sess, list_headers, p): p
                           for p in range(2, total_pages + 1)}
                for fut in _futures_as_completed(futures):
                    results.extend(fut.result().get("results") or [])
                    _set_status(pages_done=_BE_STATUS[chat_id]["pages_done"] + 1)

        # ── Client-side registry filter ────────────────────────────
        reg_set = {r.upper() for r in (registries or [])}
        if reg_set:
            filtered = [r for r in results if (r.get("registry") or "").upper() in reg_set]
        else:
            filtered = results

        # Sort by Application Date Created ascending so resume is date-ordered
        filtered.sort(key=lambda r: (r.get("date_created") or ""))

        # ── Resume: load partial checkpoint if it matches ──────────
        partial     = load_be_partial()
        resume_rows: List[dict] = []
        done_ids:    set        = set()

        if partial and partial.get("county") == county and \
                set(partial.get("registries", [])) == set(registries or []):
            resume_rows = partial.get("rows") or []
            done_ids    = set(partial.get("done_ids") or [])
            _tg(
                f"♻️ Resuming from checkpoint — {len(resume_rows):,} rows already saved, "
                f"{len(done_ids):,} IDs done."
            )

        id_list = [r["id"] for r in filtered if r.get("id") and r["id"] not in done_ids]

        if not id_list and not resume_rows:
            _set_status(phase="done", completed_at=datetime.now(), rows=0)
            _tg("ℹ️ Bulk export complete — no records found.")
            return

        if not id_list:
            # All IDs already done from checkpoint — skip straight to Excel
            rows = resume_rows
        else:
            # ── Step 2: parallel detail fetch with token rotation ──────
            _set_status(phase="fetching details", details_total=len(id_list))
            rows: List[dict] = list(resume_rows)
            current_done_ids = list(done_ids)
            exhausted_flag   = threading.Event()
            id_to_item       = {r["id"]: r for r in filtered if r.get("id")}

            _fetch_fn = _be_fetch_full_record
            with ThreadPoolExecutor(max_workers=_BE_DETAIL_WORKERS) as pool:
                futures = {pool.submit(_fetch_fn, sess, rotator, app_id): app_id
                           for app_id in id_list}
                for fut in _futures_as_completed(futures):
                    app_id = futures[fut]
                    try:
                        if exhausted_flag.is_set():
                            fut.cancel()
                            continue
                        row = fut.result()
                        rows.append(row)
                        current_done_ids.append(app_id)
                        _set_status(details_done=_BE_STATUS[chat_id]["details_done"] + 1)
                    except _AllTokensExhausted:
                        exhausted_flag.set()
                        logger.warning("Bulk export: all tokens exhausted at id=%s", app_id)
                    except Exception as exc:
                        logger.warning("Bulk export detail failed id=%s: %s", app_id, exc)
                        item = id_to_item.get(app_id, {})
                        rows.append({
                            "Filter":                          "Completed",
                            "Reference Number":                item.get("reference_number", ""),
                            "Parcel Number":                   item.get("parcel_number", ""),
                            "Registry":                        item.get("registry", ""),
                            "County":                          item.get("county", ""),
                            "Valuation Request Type":          item.get("valuation_request_type", ""),
                            "Application Status":              item.get("application_status", ""),
                            "Application Date Created":        item.get("date_created", ""),
                            "Valuation Officer":               "",
                            "Date of Valuation":                "",
                            "Valuer Total Land Value (KES)":   "",
                            "Harmonized Total Land Value (KES)": "",
                            "Document URL":                    "",
                            "Combined Report":                 "",
                            "Enrich Error":                    str(exc),
                        })
                        current_done_ids.append(app_id)
                        _set_status(
                            details_done=_BE_STATUS[chat_id]["details_done"] + 1,
                            errors=_BE_STATUS[chat_id]["errors"] + 1,
                        )

            if exhausted_flag.is_set():
                # Save whatever we managed to fetch and bail out
                save_be_partial(county, list(registries or []), rows, current_done_ids)
                remaining = len(id_list) - len(current_done_ids) + len(done_ids)
                _set_status(phase="paused — tokens exhausted", completed_at=datetime.now(), rows=len(rows))
                _tg(
                    f"⚠️ All tokens returned 403 — export paused.\n\n"
                    f"*Saved so far:* {len(rows):,} rows\n"
                    f"*Remaining:* ~{remaining:,} records\n\n"
                    "Refresh your tokens and run the export again to continue from this checkpoint."
                )
                return

        # ── Step 3: sort final rows by date, build Excel, send ────
        _set_status(phase="building excel")
        rows.sort(key=lambda r: (r.get("Application Date Created") or ""))

        clear_be_partial()
        rtype_label = "Ardhisasa" if report_type == "ardhisasa" else "Ardhipay"
        filename   = f"{rtype_label}_Valuation_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        xlsx_bytes = _be_build_excel(rows)

        asyncio.run_coroutine_threadsafe(
            bot.send_document(
                chat_id,
                document=io.BytesIO(xlsx_bytes),
                filename=filename,
                caption=f"📊 Bulk export complete — {len(rows):,} rows",
            ),
            loop,
        ).result(timeout=60)

        _set_status(phase="done", completed_at=datetime.now(), rows=len(rows))

        if email:
            try:
                _send_bulk_export_email(email, filename, xlsx_bytes)
                _tg(f"📧 File also sent to *{email}*.")
            except Exception as exc:
                logger.warning("Bulk export email failed: %s", exc)
                _tg(f"⚠️ Email delivery failed: `{exc}`")

    except Exception as exc:
        logger.error("Bulk export worker crashed: %s", exc, exc_info=True)
        _set_status(phase="failed", completed_at=datetime.now(), error_msg=str(exc))
        _tg(f"❌ Export failed: `{exc}`")


# (Job Distribution's URL/worker constants — _TEAMS_URL, _TEAM_MEMBERS_URL,
#  _JD_ONGOING_URL, _JD_DETAIL_URL, _JD_WORKERS — live in job_distribution.py)

# (LUSession, _get_lu_sess, _LU_SEARCH_COMBOS, _lu_search_ref,
#  _lu_fetch_detail, _lu_format_result live in lookup_reference.py;
#  _lookup_one_ref/_post_assignment_report — built on those primitives —
#  live in new_assignment.py, imported by main() at the point of
#  registration)


# (Job Distribution — _JD_COUNTIES, JDSession, _get_jd_sess,
#  _jd_county_keyboard, _jd_headers, _jd_fetch_teams/_jd_fetch_team_members/
#  _jd_fetch_ongoing_page/_jd_fetch_task_detail, _jd_build_excel, _jd_run,
#  all conversation handlers — live in job_distribution.py, imported by
#  main() at the point of registration)


# (Lookup Reference conversation handlers — cmd_lookup, recv_lu_cred,
#  recv_lu_ref — live in lookup_reference.py, imported by main() at the
#  point of registration)


# (Valuer Tasks — VTSession, _get_vt_sess, _vt_fetch_all_tasks,
#  _vt_build_excel, _vt_run, all conversation handlers — live in
#  valuer_tasks.py, imported by main() at the point of registration)


# ── Bulk Export conversation handlers ─────────────────────

async def _bulk_export_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """APScheduler job: run bulk export with saved schedule config."""
    cfg = load_be_schedule()
    if not cfg:
        return
    cred_type = cfg.get("cred_type")
    tokens    = get_valid_tokens(cred_type) if cred_type else _any_valid_tokens()
    if not tokens:
        cred_label = CRED_LABELS.get(cred_type, cred_type or "any")
        logger.warning("Bulk export job: no valid tokens for %s — skipping.", cred_label)
        return
    chat_id     = cfg["chat_id"]
    email       = cfg.get("email", "")
    registries  = cfg.get("registries", [])
    county      = cfg.get("county", "")
    report_type = cfg.get("report_type", "ardhipay")
    loop        = asyncio.get_event_loop()
    asyncio.ensure_future(
        asyncio.to_thread(_bulk_export_run, tokens, chat_id, email, context.bot, loop,
                          registries, county, report_type)
    )


async def cmd_bulk_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    sess = _get_be_sess(ctx)
    sess.report_type = ""
    sess.county      = ""
    sess.registries  = []
    sess.email       = ""
    sess.cred_type   = ""
    await update.message.reply_text(
        "📤 *Export Valuation Report*\n\nSelect the report type:",
        parse_mode="Markdown",
        reply_markup=_be_report_type_keyboard(),
    )
    return BE.REPORT_TYPE


async def cmd_export_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    chat_id = update.effective_chat.id
    be_st   = _BE_STATUS.get(chat_id)
    jd_st   = _JD_STATUS.get(chat_id)

    if not be_st and not jd_st:
        await update.message.reply_text(
            "ℹ️ No export or analysis has been run in this session.",
            reply_markup=_main_menu(),
        )
        return

    def _elapsed(started_at, completed_at):
        if not started_at:
            return ""
        ref  = completed_at or datetime.now()
        secs = int((ref - started_at).total_seconds())
        return f"{secs // 60}m {secs % 60}s"

    phase_icons = {
        "fetching pages":        "📄",
        "fetching details":      "🔍",
        "fetching teams":        "🔍",
        "fetching ongoing tasks":"📋",
        "fetching task details": "🔍",
        "building excel":        "📊",
        "done":                  "✅",
        "failed":                "❌",
        "paused — tokens exhausted": "⏸",
    }

    all_lines = []

    if be_st:
        phase        = be_st.get("phase", "unknown")
        started_at   = be_st.get("started_at")
        completed_at = be_st.get("completed_at")
        icon         = phase_icons.get(phase, "⏳")
        lines        = [f"*{icon} Export Valuation Report*\n"]
        elapsed      = _elapsed(started_at, completed_at)
        if started_at:
            lines.append(f"Started: `{started_at.strftime('%H:%M:%S')}`")
        if elapsed:
            lines.append(f"Elapsed: `{elapsed}`")
        lines.append(f"Phase: `{phase}`")
        total = be_st.get("total")
        if total is not None:
            lines.append(f"Total records: `{total:,}`")
        total_pages  = be_st.get("total_pages")
        pages_done   = be_st.get("pages_done", 0)
        if total_pages is not None:
            lines.append(f"Pages: `{pages_done}/{total_pages}`")
        details_done  = be_st.get("details_done", 0)
        details_total = be_st.get("details_total")
        if details_total is not None:
            pct = int(details_done / details_total * 100) if details_total else 0
            lines.append(f"Details: `{details_done}/{details_total}` ({pct}%)")
        errors = be_st.get("errors", 0)
        if errors:
            lines.append(f"⚠️ Fetch errors: `{errors}`")
        rows = be_st.get("rows")
        if phase == "done" and rows is not None:
            lines.append(f"Rows exported: `{rows:,}`")
        error_msg = be_st.get("error_msg")
        if phase == "failed" and error_msg:
            lines.append(f"Error: `{error_msg}`")
        all_lines.extend(lines)

    if jd_st:
        if all_lines:
            all_lines.append("")   # blank separator
        phase        = jd_st.get("phase", "unknown")
        started_at   = jd_st.get("started_at")
        completed_at = jd_st.get("completed_at")
        icon         = phase_icons.get(phase, "⏳")
        lines        = [f"*{icon} Job Distribution Analysis*\n"]
        elapsed      = _elapsed(started_at, completed_at)
        if started_at:
            lines.append(f"Started: `{started_at.strftime('%H:%M:%S')}`")
        if elapsed:
            lines.append(f"Elapsed: `{elapsed}`")
        lines.append(f"Phase: `{phase}`")
        teams_count = jd_st.get("teams_count")
        if teams_count is not None:
            lines.append(f"Teams: `{teams_count}`")
        members_total = jd_st.get("members_total")
        if members_total is not None:
            lines.append(f"Members: `{members_total}`")
        tasks_total = jd_st.get("tasks_total")
        tasks_done  = jd_st.get("tasks_done", 0)
        if tasks_total is not None:
            pct = int(tasks_done / tasks_total * 100) if tasks_total else 0
            lines.append(f"Tasks processed: `{tasks_done}/{tasks_total}` ({pct}%)")
        errors = jd_st.get("errors", 0)
        if errors:
            lines.append(f"⚠️ Fetch errors: `{errors}`")
        if phase == "done":
            lines.append(f"Members in report: `{jd_st.get('rows', 0):,}`")
        error_msg = jd_st.get("error_msg")
        if phase == "failed" and error_msg:
            lines.append(f"Error: `{error_msg}`")
        all_lines.extend(lines)

    await update.message.reply_text(
        "\n".join(all_lines),
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


async def recv_be_report_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    report_type      = query.data.split(":")[1]
    sess             = _get_be_sess(ctx)
    sess.report_type = report_type
    label  = "🏛 Ardhisasa Report" if report_type == "ardhisasa" else "💳 Ardhipay Report"
    county_kbd = _ar_county_keyboard() if report_type == "ardhisasa" else _be_county_keyboard()
    await query.edit_message_text(
        f"✅ Report type: *{label}*\n\nSelect the county to export:",
        parse_mode="Markdown",
        reply_markup=county_kbd,
    )
    return BE.COUNTY


async def recv_be_county(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    county = query.data.split(":")[1]
    sess   = _get_be_sess(ctx)
    sess.county     = county
    reg_map         = _AR_COUNTY_REGISTRIES if sess.report_type == "ardhisasa" else _BE_COUNTY_REGISTRIES
    lbl_map         = _AR_COUNTY_LABELS     if sess.report_type == "ardhisasa" else _BE_COUNTY_LABELS
    sess.registries = reg_map.get(county, [county])
    label           = lbl_map.get(county, county.title())
    reg_list        = ", ".join(sess.registries)
    await query.edit_message_text(
        f"✅ County: *{label}*\nRegistries: `{reg_list}`\n\n"
        "📧 Enter the email address to receive the file, or send `skip` to get it only via Telegram:",
        parse_mode="Markdown",
    )
    return BE.EMAIL


async def recv_be_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    text = (update.message.text or "").strip()
    sess = _get_be_sess(ctx)

    if text.lower() == "skip":
        sess.email = ""
    else:
        if "@" not in text or "." not in text.split("@")[-1]:
            await update.message.reply_text(
                "❌ Invalid email. Enter a valid address or send `skip`.",
                parse_mode="Markdown",
            )
            return BE.EMAIL
        sess.email = text

    await update.message.reply_text(
        "🔁 *How often should this report run?*",
        parse_mode="Markdown",
        reply_markup=_be_schedule_keyboard(),
    )
    return BE.SCHEDULE


async def recv_be_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    secs = int(query.data.split(":")[1])
    sess = _get_be_sess(ctx)
    sess.schedule_seconds = secs
    sess.cred_type = "staff_valuer"

    if not get_valid_tokens(sess.cred_type):
        await query.edit_message_text(
            "❌ No valid cached tokens for *🏢 Staff Valuer*. Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
        )
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    cred_label   = CRED_LABELS.get(sess.cred_type, sess.cred_type)
    county_label = _BE_COUNTY_LABELS.get(sess.county, sess.county.title())
    reg_list     = ", ".join(sess.registries)
    sched_label  = next((l for l, s in _BE_SCHEDULE_OPTIONS if s == secs), "Run Once")
    if secs > 0:
        sched_label += " (repeating)"
    email_label  = sess.email or "Telegram only"
    rtype_label  = "🏛 Ardhisasa Report" if sess.report_type == "ardhisasa" else "💳 Ardhipay Report"

    await query.edit_message_text(
        f"✅ Ready to export.\n\n"
        f"• Report Type: *{rtype_label}*\n"
        f"• County: *{county_label}*\n"
        f"• Registries: `{reg_list}`\n"
        f"• Filter: *Completed*\n"
        f"• Schedule: *{sched_label}*\n"
        f"• Account: *{cred_label}*\n"
        f"• Destination: *{email_label}*\n\n"
        "Tap *Run Export* to start.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Run Export", callback_data="be:yes"),
            InlineKeyboardButton("❌ Cancel",     callback_data="be:no"),
        ]]),
    )
    return BE.CONFIRM


async def recv_be_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "be:no":
        await query.edit_message_text("❌ Export cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess   = _get_be_sess(ctx)
    tokens = get_valid_tokens(sess.cred_type)
    if not tokens:
        cred_label = CRED_LABELS.get(sess.cred_type, sess.cred_type)
        await query.edit_message_text(
            f"❌ Tokens for *{cred_label}* have expired. Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
        )
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    chat_id = query.message.chat_id
    secs    = sess.schedule_seconds

    if secs > 0:
        # Save schedule and register repeating job
        cfg = {
            "chat_id":          chat_id,
            "county":           sess.county,
            "registries":       sess.registries,
            "email":            sess.email,
            "interval_seconds": secs,
            "cred_type":        sess.cred_type,
            "report_type":      sess.report_type,
        }
        save_be_schedule(cfg)
        # Remove any existing job before adding a new one
        current_jobs = ctx.application.job_queue.get_jobs_by_name("bulk_export_job")
        for job in current_jobs:
            job.schedule_removal()
        ctx.application.job_queue.run_repeating(
            _bulk_export_job,
            interval=secs,
            first=0,   # run immediately then repeat
            name="bulk_export_job",
        )
        label = next((l for l, s in _BE_SCHEDULE_OPTIONS if s == secs), "repeating")
        await query.edit_message_text(
            f"⏳ Export started and scheduled to repeat *{label}*.\n"
            "You will be notified each time it completes.",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text("⏳ Export running in background — you will be notified when done.")
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(
            asyncio.to_thread(_bulk_export_run, tokens, chat_id, sess.email, ctx.bot, loop,
                              sess.registries, sess.county, sess.report_type)
        )

    await ctx.bot.send_message(chat_id, "Returning to menu.", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
async def _post_init(app) -> None:
    # Auto-start the token refresh daemon on every boot — a container rebuild
    # (redeploy) kills it along with the bot, and it otherwise stays down
    # until someone manually taps "Start Daemon" in the menu.
    ok, msg = _daemon_start()
    if ok:
        logger.info("Token refresh daemon auto-started: %s", msg)
    else:
        logger.info("Token refresh daemon auto-start skipped: %s", msg)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global fallback for exceptions PTB doesn't catch elsewhere (e.g. transient
    Telegram API timeouts). Without this registered, PTB just dumps the raw
    traceback and silently drops the update — the user's tap/message never
    gets any response and a ConversationHandler can be left stuck mid-flow.
    """
    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Something went wrong processing that (likely a network hiccup) — please try again.",
            )
        except Exception:
            pass   # best-effort notification only; don't let this raise too


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(20)
        .post_init(_post_init)
        .build()
    )
    app.add_error_handler(_on_error)

    # not_cancel / _CANCEL_FILTER (text filter excluding the cancel button,
    # so it reaches fallbacks) live in common.py — imported above

    # db_conv (DLV Batch) is registered via dlv_batch.register(app) below.
    # auth_conv (Refresh Auth) is registered via refresh_auth.register(app) below.
    # conv (New Assignment) + /assignments are registered via
    # new_assignment.register(app) below.

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("valuers",       cmd_valuers))
    app.add_handler(CommandHandler("delete_valuer", cmd_delete_valuer))
    app.add_handler(CommandHandler("daemon",        cmd_daemon))
    # fetch_conv (Fetch Tasks) is registered via fetch_tasks.register(app) below.
    # af_conv (Auto Fetch) + AF Results are registered via auto_fetch.register(app) below.
    # rs_conv (Receive Tasks) + /schedules + /task_batches are registered via
    # receive_tasks.register(app) below.

    be_conv = ConversationHandler(
        entry_points=[
            CommandHandler("bulkexport", cmd_bulk_export),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_BULK_EXPORT)}$"), cmd_bulk_export),
        ],
        states={
            BE.REPORT_TYPE: [CallbackQueryHandler(recv_be_report_type, pattern=r"^be_rtype:")],
            BE.COUNTY:    [CallbackQueryHandler(recv_be_county,    pattern=r"^be_county:")],
            BE.EMAIL:     [MessageHandler(not_cancel, recv_be_email)],
            BE.SCHEDULE:  [CallbackQueryHandler(recv_be_schedule,  pattern=r"^be_sched:")],
            BE.CONFIRM:   [CallbackQueryHandler(recv_be_confirm,   pattern=r"^be:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    new_assignment.register(app)
    dlv_batch.register(app)
    refresh_auth.register(app)
    fetch_tasks.register(app)
    auto_fetch.register(app)
    receive_tasks.register(app)
    lookup_reference.register(app)
    valuer_tasks.register(app)
    job_distribution.register(app)
    app.add_handler(be_conv)

    app.add_handler(MessageHandler(
        filters.Regex(f"^{re.escape(BTN_EXPORT_STATUS)}$"), cmd_export_status
    ))

    # DLV Batch: 5-minute repeating job + DLV Queue handlers registered via
    # dlv_batch.register(app) above.

    # Bulk Export: restore saved schedule on startup
    be_cfg = load_be_schedule()
    if be_cfg and be_cfg.get("interval_seconds", 0) > 0:
        app.job_queue.run_repeating(
            _bulk_export_job,
            interval=be_cfg["interval_seconds"],
            first=be_cfg["interval_seconds"],
            name="bulk_export_job",
        )
        logger.info("Bulk export schedule restored: every %ds", be_cfg["interval_seconds"])

    # Auto Fetch's saved-schedule restore happens inside auto_fetch.register(app) above.

    dlv_tasks.register(app)
    sectional_properties.register(app)
    morning_briefing.register(app)

    # Button handlers outside an active conversation
    # (bare BTN_AUTH handler registered via refresh_auth.register(app) above)
    app.add_handler(CallbackQueryHandler(recv_delete_valuer,  pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(recv_daemon_action,  pattern=r"^daemon:"))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_TOKEN_STATUS)}$"), cmd_token_status))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ERROR_REPORT)}$"), cmd_error_report))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DAEMON)}$"),       cmd_daemon))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_RESTART)}$"),     cmd_restart))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HELP)}$"),        cmd_help))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_VALUERS)}$"),     cmd_valuers))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DELETE)}$"),      cmd_delete_valuer))
    # DLV Queue handlers registered via dlv_batch.register(app) above.
    # BTN_ASSIGNMENTS handler registered via new_assignment.register(app) above.
    # AF Results handlers registered via auto_fetch.register(app) above.
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"),      cmd_cancel))

    logger.info("Bot started. Polling for updates…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
