#!/usr/bin/env python3
"""
ardhisasa_bot.py
================
Telegram bot for Ardhisasa Valuation Officer Assignment.

Every feature lives in a sibling module, each registering its own
ConversationHandler(s) into main() via register(app) — see the module
docstrings and assign/CLAUDE.md's Architecture section for the full list
(new_assignment.py, receive_tasks.py, lookup_reference.py, valuer_tasks.py,
job_distribution.py, bulk_export.py, dlv_batch.py, dlv_tasks.py,
fetch_tasks.py, auto_fetch.py, refresh_auth.py, sectional_properties.py,
morning_briefing.py).

bot.py itself is now a thin orchestrator: cmd_start/cmd_help, saved-valuers
management, daemon control, token/error-report status, cmd_restart, and
main()'s wiring.
"""

import io
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

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
    MessageHandler,
    filters,
)

from common import (
    BTN_CANCEL,
    BTN_DAEMON,
    BTN_DELETE,
    BTN_ERROR_REPORT,
    BTN_HELP,
    BTN_RESTART,
    BTN_TOKEN_STATUS,
    BTN_VALUERS,
    CRED_LABELS,
    DATA_DIR,
    SAVED_VALUERS_FILE,
    _atomic_json_write,
    _ensure_data_dir,
    _jwt_exp,
    _load_tokens_raw,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    load_saved_valuers,
    logger,
)
import auto_fetch
import bulk_export
import dlv_batch
import dlv_tasks
import hold_tasks
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

# ──────────────────────────────────────────────────────────
# Persistent storage
# ──────────────────────────────────────────────────────────
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


# (BE enum + BESession live in bulk_export.py — imported by main() at the
#  point of registration)


# (DT enum lives in dlv_tasks.py — imported by main() at the point of
#  registration)


# (SC enum lives in sectional_properties.py — imported by main() at the
#  point of registration)


# (MB enum lives in morning_briefing.py — imported by main() at the point
#  of registration)


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

# (Bulk Export — all constants, keyboards, _be_headers, load/save/clear
#  be_schedule/be_partial, _be_fetch_*, _be_build_excel, _bulk_export_run —
#  live in bulk_export.py, imported by main() at the point of registration)


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


# (Bulk Export conversation handlers — _bulk_export_job, cmd_bulk_export,
#  cmd_export_status, recv_be_report_type, recv_be_county, recv_be_email,
#  recv_be_schedule, recv_be_confirm — live in bulk_export.py, imported by
#  main() at the point of registration)


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

    new_assignment.register(app)
    dlv_batch.register(app)
    refresh_auth.register(app)
    fetch_tasks.register(app)
    auto_fetch.register(app)
    receive_tasks.register(app)
    lookup_reference.register(app)
    valuer_tasks.register(app)
    job_distribution.register(app)
    bulk_export.register(app)

    # DLV Batch: 5-minute repeating job + DLV Queue handlers registered via
    # dlv_batch.register(app) above.

    # Bulk Export: schedule restore + /export_status happen inside
    # bulk_export.register(app) above.

    # Auto Fetch's saved-schedule restore happens inside auto_fetch.register(app) above.

    dlv_tasks.register(app)
    sectional_properties.register(app)
    morning_briefing.register(app)
    hold_tasks.register(app)

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
