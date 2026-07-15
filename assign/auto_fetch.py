#!/usr/bin/env python3
"""
auto_fetch.py
===============
Auto Fetch — scheduled periodic Fetch Tasks with Telegram/email
notification (⏰ Auto Fetch button / /autofetch), plus the bundled
"AF Results" run-history viewer (🗂 AF Results button). Bundled together
since AF Results only ever displays what Auto Fetch's own background job
persisted.

Depends on fetch_tasks.py's _load_fetch_tasks for the actual live fetch,
dlv_core.py's load_dlv_batch to exclude already-queued refs, and
common.py's load_sectional_config for optional sectional-task
auto-routing to a configured specialist valuer.

Call register(app) from bot.py's main() to wire this feature in (this
also restores the repeating job on startup if a prior run left one
scheduled).
"""

import json
import os
import re
import time
import uuid
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

from ardhisasa_auth import build_session

from common import (
    ALLOWED_IDS,
    BTN_AF_RESULTS,
    BTN_AUTO_FETCH,
    CPARAMS_VALUER_ROLE,
    DATA_DIR,
    _any_valid_tokens,
    _atomic_json_write,
    _CANCEL_FILTER,
    _ensure_data_dir,
    _ft_amount_keyboard,
    _ft_county_keyboard,
    _ft_registry_keyboard,
    _main_menu,
    _sectional_keyboard,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    load_saved_assignments,
    load_sectional_config,
    logger,
    not_cancel,
    persist_assignment,
)
from dlv_core import load_dlv_batch
from email_service import _send_auto_fetch_email
from endpoints import STAMP_DUTY_FIX_APPLICATION_URL
from fetch_tasks import _load_fetch_tasks
from telegram_report import _send_chunked_report

SAVED_AUTO_FETCH_FILE = os.path.join(DATA_DIR, "saved_auto_fetch.json")
SAVED_AF_RESULTS_FILE = os.path.join(DATA_DIR, "saved_af_results.json")


# ──────────────────────────────────────────────────────────
# States — Auto Fetch schedule conversation
# ──────────────────────────────────────────────────────────
class AF(Enum):
    INTERVAL    = auto()   # choose how often to run
    DAYS_BACK   = auto()   # choose days back
    COUNTY      = auto()   # county filter
    REGISTRY    = auto()   # registry filter
    AMOUNT      = auto()   # pick amount range button
    AMOUNT_TEXT = auto()   # custom amount text entry
    SECTIONAL   = auto()   # exclude / only / all sectional
    EMAIL       = auto()   # optional recipient email address


# ──────────────────────────────────────────────────────────
# Auto Fetch — scheduled periodic fetch + notify
# ──────────────────────────────────────────────────────────

# Restoring a saved schedule on startup used to wait a full interval before
# the first run — every container restart pushed the next run out by up to
# the whole configured interval, which starved the job if restarts happened
# more often than that. Run soon after startup instead; later runs still
# follow the configured interval.
_AF_RESTORE_FIRST_RUN_DELAY = 60   # seconds

_AF_INTERVALS = [
    ("15 min",  15),
    ("30 min",  30),
    ("1 hr",    60),
    ("2 hr",   120),
    ("4 hr",   240),
    ("6 hr",   360),
    ("12 hr",  720),
    ("24 hr", 1440),
]


def load_auto_fetch_schedule() -> Optional[Dict]:
    try:
        with open(SAVED_AUTO_FETCH_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_auto_fetch_schedule(cfg: Dict) -> None:
    _atomic_json_write(SAVED_AUTO_FETCH_FILE, cfg, indent=2)


def clear_auto_fetch_schedule() -> None:
    try:
        os.remove(SAVED_AUTO_FETCH_FILE)
    except FileNotFoundError:
        pass


# ── Auto Fetch result history ──────────────────────────────
_AF_RESULTS_KEEP = 20   # keep last N runs


def load_af_results() -> List[Dict]:
    try:
        with open(SAVED_AF_RESULTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def persist_af_result(run_id: str, run_at: str, tasks: List[Dict], cfg: Dict) -> None:
    _ensure_data_dir()
    results = load_af_results()
    results.append({
        "run_id":   run_id,
        "run_at":   run_at,
        "count":    len(tasks),
        "filters": {
            "county":           cfg.get("county_filter", ""),
            "registry":         cfg.get("registry_filter", ""),
            "amount_min":       cfg.get("amount_min"),
            "amount_max":       cfg.get("amount_max"),
            "days_back":        cfg.get("days_back", 2),
            "sectional_filter": cfg.get("sectional_filter", "exclude"),
        },
        "tasks": [
            {
                "ref":          t.get("reference_number", ""),
                "parcel":       t.get("parcel_number", ""),
                "county":       t.get("county", ""),
                "registry":     t.get("registry", ""),
                "consideration": t.get("consideration"),
                "date_created": (t.get("date_created") or "")[:10],
                "source":       t.get("source", ""),
                "assessor":     t.get("assessor", ""),
            }
            for t in tasks
        ],
    })
    # Trim to keep only the most recent runs
    if len(results) > _AF_RESULTS_KEEP:
        results = results[-_AF_RESULTS_KEEP:]
    _atomic_json_write(SAVED_AF_RESULTS_FILE, results, indent=2)


def _af_interval_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row  = []
    for label, mins in _AF_INTERVALS:
        row.append(InlineKeyboardButton(label, callback_data=f"af:interval:{mins}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel / Stop schedule", callback_data="af:cancel")])
    return InlineKeyboardMarkup(rows)


async def cmd_auto_fetch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)

    cfg = load_auto_fetch_schedule()
    if cfg:
        mins    = cfg.get("interval_minutes", 0)
        lo      = cfg.get("amount_min")
        hi      = cfg.get("amount_max")
        lo_s    = f"KES {int(lo):,}" if lo is not None else "0"
        hi_s    = f"KES {int(hi):,}" if hi is not None else "∞"
        county    = cfg.get("county_filter", "") or "All"
        reg       = cfg.get("registry_filter", "") or "All"
        days      = cfg.get("days_back", 2)
        sec       = {"exclude": "Exclude Sectional", "only": "Sectional Only", "all": "All"}.get(
                        cfg.get("sectional_filter", "exclude"), "Exclude Sectional")
        email_s   = cfg.get("email") or "Telegram only"
        status  = (
            f"⏰ *Auto Fetch is active*\n"
            f"Interval: every {mins} min | Days back: {days}\n"
            f"County: {county.title()} | Registry: {reg.title()}\n"
            f"Amount: {lo_s} – {hi_s} | Sectional: {sec}\n"
            f"Email: {email_s}\n\n"
            f"Choose a new interval to update, or cancel to stop."
        )
    else:
        status = (
            "⏰ *Auto Fetch*\n\n"
            "Periodically fetches tasks and sends results here.\n"
            "Choose how often to run:"
        )

    await update.message.reply_text(
        status,
        parse_mode="Markdown",
        reply_markup=_af_interval_keyboard(),
    )
    return AF.INTERVAL


async def recv_af_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "af:cancel":
        for job in ctx.job_queue.get_jobs_by_name("auto_fetch_job"):
            job.schedule_removal()
        clear_auto_fetch_schedule()
        await query.edit_message_text("🛑 Auto Fetch schedule cancelled.")
        await query.message.reply_text("Main menu:", reply_markup=_main_menu())
        return ConversationHandler.END

    try:
        interval_minutes = int(query.data.split(":")[-1])
    except ValueError:
        await query.edit_message_text(
            "⚠️ Unexpected selection. Please tap one of the interval buttons below:",
            reply_markup=_af_interval_keyboard(),
        )
        return AF.INTERVAL

    ctx.user_data["af_interval_minutes"] = interval_minutes
    await query.edit_message_text(
        f"✅ Interval: *{interval_minutes} min*\n\nHow many days back to fetch?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("1 day",  callback_data="af_days:1"),
                InlineKeyboardButton("2 days", callback_data="af_days:2"),
                InlineKeyboardButton("3 days", callback_data="af_days:3"),
            ],
            [
                InlineKeyboardButton("5 days",  callback_data="af_days:5"),
                InlineKeyboardButton("7 days",  callback_data="af_days:7"),
                InlineKeyboardButton("10 days", callback_data="af_days:10"),
            ],
        ]),
    )
    return AF.DAYS_BACK


async def recv_af_days(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctx.user_data["af_days_back"] = int(query.data.split(":")[-1])
    await query.edit_message_text(
        f"✅ Days back: *{ctx.user_data['af_days_back']}*\n\nFilter by county?",
        parse_mode="Markdown",
        reply_markup=_ft_county_keyboard(),
    )
    return AF.COUNTY


async def recv_af_county(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctx.user_data["af_county"] = "" if query.data == "ft_county:all" else query.data.split(":")[1]
    label = ctx.user_data["af_county"].title() or "All Counties"
    await query.edit_message_text(
        f"✅ County: *{label}*\n\nFilter by registry?",
        parse_mode="Markdown",
        reply_markup=_ft_registry_keyboard(),
    )
    return AF.REGISTRY


async def recv_af_registry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctx.user_data["af_registry"] = "" if query.data == "ft_registry:all" else query.data.split(":")[1]
    reg_label    = ctx.user_data["af_registry"].title() or "All Registries"
    county_label = ctx.user_data.get("af_county", "").title() or "All Counties"
    await query.edit_message_text(
        f"✅ County: *{county_label}* | Registry: *{reg_label}*\n\nFilter by amount?",
        parse_mode="Markdown",
        reply_markup=_ft_amount_keyboard(),
    )
    return AF.AMOUNT


async def recv_af_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback handler — user picked an amount preset or Custom."""
    query = update.callback_query
    await query.answer()
    choice = query.data  # e.g. "ft_amount:1m_5m" or "ft_amount:custom"

    if choice == "ft_amount:custom":
        await query.edit_message_text(
            "✏️ Enter custom amount range as *min max* (e.g. `500000 5000000`).\n"
            "Or send just one number as max.",
            parse_mode="Markdown",
        )
        return AF.AMOUNT_TEXT

    ranges = {
        "ft_amount:0_1m":    (0.0,           1_000_000.0),
        "ft_amount:1m_5m":   (1_000_000.0,   5_000_000.0),
        "ft_amount:5m_10m":  (5_000_000.0,  10_000_000.0),
        "ft_amount:20m_50m": (20_000_000.0, 50_000_000.0),
        "ft_amount:50m_100m":(50_000_000.0,100_000_000.0),
        "ft_amount:10m_80m": (10_000_000.0, 80_000_000.0),
        "ft_amount:50m_3b":  (50_000_000.0,  3_000_000_000.0),
        "ft_amount:all":     (None,           None),
    }
    amount_min, amount_max = ranges.get(choice, (None, None))
    ctx.user_data["af_amount_min"] = amount_min
    ctx.user_data["af_amount_max"] = amount_max

    await query.edit_message_text(
        "Include sectional properties?\n_(Sectional: parcel has 4 parts e.g. Nairobi/Block12/345/888)_",
        parse_mode="Markdown",
        reply_markup=_sectional_keyboard(),
    )
    return AF.SECTIONAL


async def recv_af_amount_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Text handler for custom amount entry in Auto Fetch."""
    text = update.message.text.strip()
    parts = text.split()
    try:
        if len(parts) == 2:
            amount_min = float(parts[0].replace(",", ""))
            amount_max = float(parts[1].replace(",", ""))
        elif len(parts) == 1:
            amount_min = None
            amount_max = float(parts[0].replace(",", ""))
        else:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Could not parse. Enter two numbers e.g. `500000 5000000`, or one number as max.",
            parse_mode="Markdown",
        )
        return AF.AMOUNT_TEXT

    ctx.user_data["af_amount_min"] = amount_min
    ctx.user_data["af_amount_max"] = amount_max
    await update.message.reply_text(
        "Include sectional properties?\n_(Sectional: parcel has 4 parts e.g. Nairobi/Block12/345/888)_",
        parse_mode="Markdown",
        reply_markup=_sectional_keyboard(),
    )
    return AF.SECTIONAL


async def recv_af_sectional(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    ctx.user_data["af_sectional"] = query.data.split(":")[1]  # "exclude" | "only" | "all"

    existing_email = ""
    cfg = load_auto_fetch_schedule()
    if cfg:
        existing_email = cfg.get("email", "")

    hint = f"Current: `{existing_email}`\n\n" if existing_email else ""
    await query.edit_message_text(
        f"📧 *Send results to email?*\n\n"
        f"{hint}"
        f"Enter an email address, or send `skip` to notify via Telegram only.",
        parse_mode="Markdown",
    )
    return AF.EMAIL


async def recv_af_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.lower() == "skip":
        email = ""
    else:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}", text):
            await update.message.reply_text(
                "❌ Invalid email address. Enter a valid email (e.g. `user@example.com`) or send `skip`.",
                parse_mode="Markdown",
            )
            return AF.EMAIL
        email = text

    interval   = ctx.user_data.get("af_interval_minutes", 60)
    days       = ctx.user_data.get("af_days_back", 2)
    county     = ctx.user_data.get("af_county", "")
    registry   = ctx.user_data.get("af_registry", "")
    amount_min = ctx.user_data.get("af_amount_min")
    amount_max = ctx.user_data.get("af_amount_max")
    sectional  = ctx.user_data.get("af_sectional", "exclude")

    cfg = {
        "interval_minutes": interval,
        "days_back":        days,
        "county_filter":    county,
        "registry_filter":  registry,
        "amount_min":       amount_min,
        "amount_max":       amount_max,
        "sectional_filter": sectional,
        "email":            email,
    }
    save_auto_fetch_schedule(cfg)

    for job in ctx.job_queue.get_jobs_by_name("auto_fetch_job"):
        job.schedule_removal()
    ctx.job_queue.run_repeating(
        _auto_fetch_job,
        interval=interval * 60,
        first=interval * 60,
        name="auto_fetch_job",
    )

    lo_s      = f"KES {int(amount_min):,}" if amount_min is not None else "0"
    hi_s      = f"KES {int(amount_max):,}" if amount_max is not None else "∞"
    co_label  = county.title() or "All"
    re_label  = registry.title() or "All"
    sec_label = {"exclude": "Exclude Sectional", "only": "Sectional Only", "all": "All"}.get(sectional, sectional)
    email_label = email or "Telegram only"
    await update.message.reply_text(
        f"✅ *Auto Fetch scheduled*\n"
        f"Every *{interval} min* | Days back: *{days}*\n"
        f"County: *{co_label}* | Registry: *{re_label}*\n"
        f"Amount: {lo_s} – {hi_s} | Sectional: *{sec_label}*\n"
        f"Email: *{email_label}*\n"
        f"First run in {interval} min.",
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


async def _auto_fetch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: fetch tasks with saved schedule settings and notify."""
    cfg = load_auto_fetch_schedule()
    if not cfg:
        return

    tokens = _any_valid_tokens()
    if not tokens:
        logger.warning("Auto Fetch job: no valid tokens — skipping cycle.")
        return

    days_back       = cfg.get("days_back", 2)
    county_filter   = cfg.get("county_filter", "")
    registry_filter = cfg.get("registry_filter", "")
    amount_min      = cfg.get("amount_min")
    amount_max      = cfg.get("amount_max")

    try:
        tasks, stats = _load_fetch_tasks(tokens, days_back)
    except Exception as e:
        logger.warning("Auto Fetch job: fetch failed: %s", e)
        return

    # County filter
    if county_filter:
        tasks = [t for t in tasks if county_filter in (t.get("county") or "").strip().lower()]

    # Registry filter
    if registry_filter:
        tasks = [t for t in tasks if registry_filter in (t.get("registry") or "").strip().lower()]

    # Amount filter
    if amount_min is not None or amount_max is not None:
        def _in_range(t):
            raw = t.get("consideration")
            if raw is None:
                return False
            try:
                val = float(str(raw).replace(",", "").strip())
            except (ValueError, TypeError):
                return False
            if amount_min is not None and val < amount_min:
                return False
            if amount_max is not None and val > amount_max:
                return False
            return True
        tasks = [t for t in tasks if _in_range(t)]

    # Sectional filter
    sf = cfg.get("sectional_filter", "exclude")
    if sf == "exclude":
        tasks = [t for t in tasks if str(t.get("parcel_number") or "").count("/") < 3]
    elif sf == "only":
        tasks = [t for t in tasks if str(t.get("parcel_number") or "").count("/") >= 3]
    # "all" → no filter

    # Exclude already-queued refs
    queued_refs = {item.get("ref", "") for item in load_dlv_batch()}
    tasks = [t for t in tasks if t.get("reference_number", "") not in queued_refs]

    # Sectional auto-routing: if a specialist is configured, auto-assign sectional tasks
    sc_cfg = load_sectional_config()
    if sc_cfg and sc_cfg.get("auto_route") and sc_cfg.get("specialist") and sc_cfg.get("cred_type"):
        specialist   = sc_cfg["specialist"]
        sc_cred_type = sc_cfg["cred_type"]
        sc_tokens    = get_valid_tokens(sc_cred_type)
        if sc_tokens:
            sectional_tasks  = [t for t in tasks if str(t.get("parcel_number") or "").count("/") >= 3]
            remaining_tasks  = [t for t in tasks if str(t.get("parcel_number") or "").count("/") < 3]
            if sectional_tasks:
                assigned_sc = []
                failed_sc   = []
                sc_sess     = build_session()
                sc_hdrs     = {
                    "Authorization": f"Bearer {sc_tokens.access_token}",
                    "JWTAUTH":       f"Bearer {sc_tokens.jwt}",
                    "cparams":       CPARAMS_VALUER_ROLE,
                }
                for t in sectional_tasks:
                    ref = t.get("reference_number", "")
                    try:
                        r = sc_sess.put(
                            STAMP_DUTY_FIX_APPLICATION_URL,
                            headers=sc_hdrs,
                            json={"request_id": t.get("id", ""), "valuation_officer": specialist["uid"]},
                            timeout=30,
                        )
                        if r.status_code in (200, 201):
                            assigned_sc.append(ref)
                            persist_assignment(ref, specialist["name"], specialist["uid"])
                        else:
                            failed_sc.append(ref)
                    except Exception as e:
                        logger.warning("Sectional auto-assign failed for %s: %s", ref, e)
                        failed_sc.append(ref)

                if assigned_sc or failed_sc:
                    sc_msg = (
                        f"🔲 *Sectional Auto-Assign* — {specialist['name']}\n"
                        f"✅ Assigned: {len(assigned_sc)} | ❌ Failed: {len(failed_sc)}\n"
                    )
                    if assigned_sc:
                        sc_msg += "\n".join(f"  ✅ {r}" for r in assigned_sc[:10])
                    if failed_sc:
                        sc_msg += "\n" + "\n".join(f"  ❌ {r}" for r in failed_sc[:5])
                    for chat_id in ALLOWED_IDS:
                        try:
                            await context.bot.send_message(chat_id, sc_msg, parse_mode="Markdown")
                        except Exception as e:
                            logger.warning("Sectional auto-assign notify error for %s: %s", chat_id, e)
                tasks = remaining_tasks

    # Persist result history (record even if empty so the run appears in AF Results)
    _af_run_id = str(uuid.uuid4())[:8]
    _af_run_at = time.strftime("%Y-%m-%d %H:%M:%S")
    persist_af_result(_af_run_id, _af_run_at, tasks, cfg)

    if not tasks:
        logger.info("Auto Fetch job: no new tasks after filters.")
        return

    lo_s     = f"KES {int(amount_min):,}" if amount_min is not None else "0"
    hi_s     = f"KES {int(amount_max):,}" if amount_max is not None else "∞"
    co_label  = county_filter.title() or "All"
    re_label  = registry_filter.title() or "All"
    sec_label = {"exclude": "No Sectional", "only": "Sectional Only", "all": "All"}.get(sf, sf)
    header    = (
        f"⏰ *Auto Fetch — {len(tasks)} task(s)*\n"
        f"Days: {days_back} | County: {co_label} | Registry: {re_label} | Amount: {lo_s}–{hi_s} | {sec_label}\n\n"
    )

    lines = []
    for i, t in enumerate(tasks, 1):
        src    = t.get("source", "")
        ref    = t.get("reference_number", "—")
        cnty   = (t.get("county") or "—").upper()
        reg    = (t.get("registry") or "—").upper()
        date   = (t.get("date_created") or "")[:10]
        parcel = t.get("parcel_number") or "—"
        try:
            raw_cons = t.get("consideration")
            cons = f"KES {int(float(str(raw_cons).replace(',','').strip())):,}" if raw_cons else "—"
        except (ValueError, TypeError):
            cons = str(t.get("consideration") or "—")
        lines.append(
            f"{i}. [{src}] {ref}\n"
            f"   {cnty} / {reg} | {date}\n"
            f"   {cons} | {parcel}"
        )

    for chat_id in ALLOWED_IDS:
        async def _send(text, reply_markup, chat_id=chat_id):
            try:
                await context.bot.send_message(chat_id, text, parse_mode="Markdown")
            except Exception as e:
                logger.warning("Auto Fetch notify error for %s: %s", chat_id, e)
        await _send_chunked_report(_send, [header] + lines, join="\n\n")

    # Email notification
    email = cfg.get("email", "")
    if email:
        # Build plain-text body (strip Markdown asterisks)
        plain_header = (
            f"Auto Fetch — {len(tasks)} task(s)\n"
            f"Days: {days_back} | County: {co_label} | Registry: {re_label} | Amount: {lo_s}–{hi_s} | {sec_label}\n"
            + "─" * 60 + "\n\n"
        )
        plain_lines = []
        for i, t in enumerate(tasks, 1):
            src    = t.get("source", "")
            ref    = t.get("reference_number", "—")
            cnty   = (t.get("county") or "—").upper()
            reg    = (t.get("registry") or "—").upper()
            date   = (t.get("date_created") or "")[:10]
            parcel = t.get("parcel_number") or "—"
            try:
                raw_cons = t.get("consideration")
                cons = f"KES {int(float(str(raw_cons).replace(',','').strip())):,}" if raw_cons else "—"
            except (ValueError, TypeError):
                cons = str(t.get("consideration") or "—")
            plain_lines.append(
                f"{i}. [{src}] {ref}\n"
                f"   {cnty} / {reg} | {date}\n"
                f"   {cons} | {parcel}"
            )
        plain_body   = plain_header + "\n\n".join(plain_lines)
        email_subject = f"Auto Fetch — {len(tasks)} task(s) found"
        try:
            _send_auto_fetch_email(email, email_subject, plain_body)
            logger.info("Auto Fetch email sent to %s", email)
        except Exception as e:
            # Unlike the Telegram summary above, this used to fail silently —
            # only a log line, nothing surfaced — so a broken SMTP config on
            # the server looked identical to "no email configured" from the
            # user's side. Notify the same way Bulk Export/DLV Tasks/Morning
            # Briefing already do on email failure.
            logger.warning("Auto Fetch email failed: %s", e)
            for chat_id in ALLOWED_IDS:
                try:
                    await context.bot.send_message(
                        chat_id, f"⚠️ Auto Fetch email delivery to *{email}* failed: `{e}`",
                        parse_mode="Markdown",
                    )
                except Exception as notify_err:
                    logger.warning("Auto Fetch email-failure notify error for %s: %s", chat_id, notify_err)


# ──────────────────────────────────────────────────────────
# AF Results — view auto fetch run history
# ──────────────────────────────────────────────────────────

async def cmd_af_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    results = load_af_results()
    if not results:
        await update.message.reply_text(
            "📭 No Auto Fetch runs recorded yet. The history is saved once the Auto Fetch schedule runs.",
            reply_markup=_main_menu(),
        )
        return

    # Show last 10 runs as inline buttons (newest first)
    rows = []
    for r in reversed(results[-10:]):
        label = f"{r['run_at']}  ({r['count']} task{'s' if r['count'] != 1 else ''})"
        rows.append([InlineKeyboardButton(label, callback_data=f"af_result:{r['run_id']}")])

    await update.message.reply_text(
        "🗂 *Auto Fetch History* (last 10 runs)\n\nTap a run to see its tasks:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def recv_af_result_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show tasks for a specific AF run, marking already-assigned refs."""
    query = update.callback_query
    await query.answer()
    run_id = query.data.split(":", 1)[1]

    results  = load_af_results()
    run      = next((r for r in results if r["run_id"] == run_id), None)
    if not run:
        await query.edit_message_text("❌ Run not found (may have been pruned).")
        return

    assigned = load_saved_assignments()   # {ref: {valuer_name, ...}}
    tasks    = run["tasks"]
    f        = run["filters"]

    lo_s  = f"KES {int(f['amount_min']):,}" if f.get("amount_min") is not None else "0"
    hi_s  = f"KES {int(f['amount_max']):,}" if f.get("amount_max") is not None else "∞"
    sec_label = {"exclude": "No Sectional", "only": "Sectional Only", "all": "All"}.get(
        f.get("sectional_filter", "exclude"), f.get("sectional_filter", "exclude")
    )
    header = (
        f"🗂 *AF Run — {run['run_at']}*\n"
        f"Tasks found: *{run['count']}*\n"
        f"Filters: County={f.get('county','All') or 'All'} | Registry={f.get('registry','All') or 'All'}\n"
        f"Amount: {lo_s}–{hi_s} | {sec_label}\n\n"
    )

    if not tasks:
        await query.edit_message_text(header + "_No tasks found in this run._", parse_mode="Markdown")
        return

    lines = []
    pending = 0
    for i, t in enumerate(tasks, 1):
        ref    = t.get("ref", "—")
        parcel = t.get("parcel", "—")
        cnty   = (t.get("county") or "—").upper()
        reg    = (t.get("registry") or "—").upper()
        date   = t.get("date_created", "")
        try:
            raw = t.get("consideration")
            cons = f"KES {int(float(str(raw).replace(',','').strip())):,}" if raw else "—"
        except (ValueError, TypeError):
            cons = str(t.get("consideration") or "—")

        if ref in assigned:
            status = f"✅ assigned to {assigned[ref].get('valuer_name', '?')}"
        else:
            status = "⏳ pending"
            pending += 1

        assessor = t.get("assessor") or "—"
        lines.append(
            f"{i}. `{ref}` — {status}\n"
            f"   {cnty}/{reg} | {date} | {cons}\n"
            f"   {parcel} | Assessor: {assessor}"
        )

    summary_line = f"*Pending: {pending}  |  Assigned: {len(tasks) - pending}*\n\n"

    async def _send(text, reply_markup):
        await query.message.reply_text(text, parse_mode="Markdown")

    await _send_chunked_report(_send, [header + summary_line] + lines, join="\n\n")


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Auto Fetch conversation + AF Results viewer into the given
    Application, and restore the repeating job on startup if a prior run
    left a schedule configured."""
    af_conv = ConversationHandler(
        entry_points=[
            CommandHandler("autofetch", cmd_auto_fetch),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_AUTO_FETCH)}$"), cmd_auto_fetch),
        ],
        states={
            AF.INTERVAL:    [CallbackQueryHandler(recv_af_interval,     pattern=r"^af:")],
            AF.DAYS_BACK:   [CallbackQueryHandler(recv_af_days,         pattern=r"^af_days:")],
            AF.COUNTY:      [CallbackQueryHandler(recv_af_county,       pattern=r"^ft_county:")],
            AF.REGISTRY:    [CallbackQueryHandler(recv_af_registry,     pattern=r"^ft_registry:")],
            AF.AMOUNT:      [CallbackQueryHandler(recv_af_amount,       pattern=r"^ft_amount:")],
            AF.AMOUNT_TEXT: [MessageHandler(not_cancel, recv_af_amount_text)],
            AF.SECTIONAL:   [CallbackQueryHandler(recv_af_sectional,    pattern=r"^ft_sectional:")],
            AF.EMAIL:       [MessageHandler(not_cancel, recv_af_email)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(af_conv)

    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_AF_RESULTS)}$"), cmd_af_results))
    app.add_handler(CallbackQueryHandler(recv_af_result_detail, pattern=r"^af_result:"))

    cfg = load_auto_fetch_schedule()
    if cfg:
        interval_secs = cfg.get("interval_minutes", 60) * 60
        app.job_queue.run_repeating(
            _auto_fetch_job,
            interval=interval_secs,
            first=_AF_RESTORE_FIRST_RUN_DELAY,
            name="auto_fetch_job",
        )
        logger.info(
            "Auto Fetch schedule restored: every %d min (first run in %ds)",
            cfg.get("interval_minutes"), _AF_RESTORE_FIRST_RUN_DELAY,
        )
