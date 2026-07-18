#!/usr/bin/env python3
"""
fetch_tasks.py
===============
Fetch Tasks — on-demand search for qualifying HQ + County TRANSFER
applications (📊 Fetch Tasks button / /fetch): its own credential+OTP
login step, then days-back/county/registry/amount/sectional filters,
then a live two-stage fetch (list + per-task detail) against the
stampdutyservice/registrationservice endpoints.

_ft_format_task_block (shared with Auto Fetch's email body) is built from
task_block.py's shared field builders and rendered via its
format_labeled_block — the one visual every report in the bot uses.

Call register(app) from bot.py's main() to wire this feature in.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

import requests
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

from ardhisasa_auth import AuthTokens, build_session

from common import (
    BTN_FETCH_TASKS,
    CRED_LABELS,
    CRED_MAP,
    _CANCEL_FILTER,
    _cred_keyboard,
    _date_cutoff_str,
    _ft_amount_keyboard,
    _ft_county_keyboard,
    _ft_headers,
    _ft_registry_keyboard,
    _main_menu,
    _sectional_keyboard,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    logger,
    not_cancel,
    persist_tokens,
)
from dlv_core import _resolve_assessor, load_dlv_batch
from endpoints import (
    ASSESSOR_STAGE_DETAIL_URL,
    ASSESSOR_STAGE_LIST_URL,
    AUTH_LOGIN_URL,
    AUTH_OTP_VERIFY_URL,
    COUNTY_TRANSFER_DETAIL_URL,
)
from fetch_tasks_cache import _log_fetch_tasks
from task_block import consideration_field, format_labeled_block, parcel_field, tag_field
from telegram_report import _send_chunked_report


# ──────────────────────────────────────────────────────────
# States — Fetch Tasks conversation
# ──────────────────────────────────────────────────────────
class FT(Enum):
    CHOOSE_CRED      = auto()
    WAIT_OTP         = auto()
    DAYS_BACK        = auto()   # inline-button preset OR free text
    COUNTY_FILTER    = auto()   # county button picker
    REGISTRY_FILTER  = auto()   # registry button picker
    AMOUNT_FILTER    = auto()   # 4-button amount range picker
    AMOUNT_TEXT      = auto()   # free-text custom min/max amount
    SECTIONAL_FILTER = auto()   # exclude / only / all sectional


@dataclass
class FTSession:
    cred_type:       str = "staff2"   # default to Support Reg (has SUPPORT role)
    http_session:    Optional[requests.Session] = None
    tokens:          Optional[AuthTokens] = None
    days_back:       int = 5
    tasks:           List[Dict] = field(default_factory=list)
    stats:           Dict = field(default_factory=dict)
    county_filter:   str = ""           # "nairobi" or "" (all)
    registry_filter: str = ""           # "central", "nairobi", or "" (all)
    amount_min:       Optional[float] = None
    amount_max:       Optional[float] = None
    sectional_filter: str = "exclude"   # "exclude" | "only" | "all"


def _get_ft_sess(ctx: ContextTypes.DEFAULT_TYPE) -> FTSession:
    if "ft_session" not in ctx.user_data:
        ctx.user_data["ft_session"] = FTSession()
    return ctx.user_data["ft_session"]


# ──────────────────────────────────────────────────────────
# Live fetch — list + per-task detail
# ──────────────────────────────────────────────────────────

def _has_stamp_duty_invoice(invoices: list) -> bool:
    """Return True if any invoice is for stamp duty (task already processed)."""
    for inv in invoices:
        pf = str(inv.get("payment_for", "")).lower()
        if "stamp" in pf:
            return True
    return False


def _fetch_hq_list(http_sess, tokens: AuthTokens, cutoff: str) -> List[Dict]:
    """Fetch HQ digitised TRANSFER applications up to cutoff date."""
    headers = _ft_headers(tokens)
    candidates: List[Dict] = []
    page = 1
    stop = False
    while not stop:
        try:
            resp = http_sess.get(
                ASSESSOR_STAGE_LIST_URL,
                headers=headers,
                params={"filter": "Ongoing", "page": page, "search": ""},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("HQ list page %d failed: %s", page, e)
            break

        results = data.get("results", [])
        if not results:
            break
        for task in results:
            if task.get("date_created", "")[:10] < cutoff:
                stop = True
                break
            if (task.get("application_type") == "TRANSFER"
                    and task.get("from_ardhipay") is False):
                candidates.append(task)
        if not data.get("next") or stop:
            break
        page += 1

    return candidates


def _fetch_county_list(http_sess, tokens: AuthTokens, cutoff: str) -> List[Dict]:
    """Fetch County undigitised TRANSFER applications up to cutoff date."""
    headers = _ft_headers(tokens)
    candidates: List[Dict] = []
    page = 1
    stop = False
    while not stop:
        try:
            resp = http_sess.get(
                ASSESSOR_STAGE_LIST_URL,
                headers=headers,
                params={"filter": "Ongoing", "from_ardhipay": "true", "page": page, "search": ""},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("County list page %d failed: %s", page, e)
            break

        results = data.get("results", [])
        if not results:
            break
        for task in results:
            if task.get("date_created", "")[:10] < cutoff:
                stop = True
                break
            if task.get("application_type") == "TRANSFER":
                candidates.append(task)
        if not data.get("next") or stop:
            break
        page += 1

    return candidates


def _fetch_hq_detail_2a(http_sess, tokens: AuthTokens, application_id: str) -> Optional[Dict]:
    """Fetch registration detail (2a) using application_id."""
    try:
        resp = http_sess.get(
            COUNTY_TRANSFER_DETAIL_URL,
            headers=_ft_headers(tokens),
            params={"request_id": application_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("HQ 2a detail failed for %s: %s", application_id, e)
        return None


def _fetch_hq_detail_2b(http_sess, tokens: AuthTokens, task_id: str) -> Optional[Dict]:
    """Fetch stamp-duty detail (2b) using task id — has officer assignments."""
    try:
        resp = http_sess.get(
            ASSESSOR_STAGE_DETAIL_URL,
            headers=_ft_headers(tokens),
            params={"request_id": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("HQ 2b detail failed for %s: %s", task_id, e)
        return None


def _fetch_county_detail(http_sess, tokens: AuthTokens, task_id: str) -> Optional[Dict]:
    """Fetch county stamp-duty detail view."""
    try:
        resp = http_sess.get(
            ASSESSOR_STAGE_DETAIL_URL,
            headers=_ft_headers(tokens),
            params={"request_id": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("County detail failed for %s: %s", task_id, e)
        return None


def _load_fetch_tasks(tokens: AuthTokens, days_back: int) -> Tuple[List[Dict], Dict]:
    """
    Fetch qualifying HQ + County TRANSFER applications in the last days_back days.
    Returns (tasks, stats).
    Each task: source, reference_number, date_created, county, registry,
               consideration, currency_code, parcel_number, officers.
    """
    http_sess = build_session()
    cutoff = _date_cutoff_str(days_back)

    # Both list fetches run in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        hq_future     = pool.submit(_fetch_hq_list,    http_sess, tokens, cutoff)
        county_future = pool.submit(_fetch_county_list, http_sess, tokens, cutoff)
        hq_candidates     = hq_future.result()
        county_candidates = county_future.result()

    stats: Dict = {
        "hq_raw":     len(hq_candidates),
        "county_raw": len(county_candidates),
        "hq_kept":    0,
        "county_kept": 0,
    }
    tasks: List[Dict] = []

    # ── HQ: 2a + 2b in parallel per task ─────────────────────
    if hq_candidates:
        with ThreadPoolExecutor(max_workers=8) as pool:
            fut2a = {
                pool.submit(_fetch_hq_detail_2a, http_sess, tokens, t["application_id"]): t
                for t in hq_candidates
            }
            fut2b = {
                pool.submit(_fetch_hq_detail_2b, http_sess, tokens, t["id"]): t
                for t in hq_candidates
            }
            res2a: Dict[str, Optional[Dict]] = {}
            res2b: Dict[str, Optional[Dict]] = {}
            for f in _futures_as_completed(fut2a):
                res2a[fut2a[f]["id"]] = f.result()
            for f in _futures_as_completed(fut2b):
                res2b[fut2b[f]["id"]] = f.result()

        for t in hq_candidates:
            d2a = res2a.get(t["id"])
            if not d2a:
                continue
            if _has_stamp_duty_invoice(d2a.get("invoices", [])):
                continue
            if (d2a.get("stamp_duty_status") != "SENT_TO_COLLECTOR"
                    or d2a.get("application_status", "").upper() != "ONGOING"):
                continue

            d2b = res2b.get(t["id"])
            officers = []
            if d2b:
                officers = [
                    {"name": o.get("names", ""), "role": o.get("role", "")}
                    for o in d2b.get("details", {}).get("officers", [])
                ]

            tasks.append({
                "source":             "HQ",
                "reference_number":   t.get("reference_number", ""),
                "date_created":       t.get("date_created", ""),
                "county":             d2a.get("county") or t.get("county", ""),
                "registry":           d2a.get("registry") or t.get("registry", ""),
                "consideration":      str(d2a.get("consideration", "")),
                "consideration_type": d2a.get("consideration_type", ""),
                "currency_code":      d2a.get("currency_code", "KES"),
                "parcel_number":      t.get("parcel_number", ""),
                "officers":           officers,
                "assessor":           _resolve_assessor(officers),
            })
            stats["hq_kept"] += 1

    # ── County: detail per task ───────────────────────────────
    if county_candidates:
        with ThreadPoolExecutor(max_workers=8) as pool:
            county_fut = {
                pool.submit(_fetch_county_detail, http_sess, tokens, t["id"]): t
                for t in county_candidates
            }
            for f in _futures_as_completed(county_fut):
                t   = county_fut[f]
                det = (f.result() or {}).get("details", {})
                if not det:
                    continue
                if (det.get("node") != "STAMP_DUTY_PAYMENT_DEFINITION"
                        or det.get("application_status", "").upper() != "ONGOING"):
                    continue
                ext = det.get("external_process_details") or {}
                if _has_stamp_duty_invoice(ext.get("invoice", [])):
                    continue

                officers = [
                    {"name": o.get("names", ""), "role": o.get("role", "")}
                    for o in det.get("officers", [])
                ]
                tasks.append({
                    "source":             "County",
                    "reference_number":   det.get("reference_number") or t.get("reference_number", ""),
                    "date_created":       t.get("date_created", ""),
                    "county":             det.get("county") or t.get("county", ""),
                    "registry":           det.get("registry") or t.get("registry", ""),
                    "consideration":      str(ext.get("consideration_amount", "")),
                    "consideration_type": ext.get("process_type", ""),
                    "currency_code":      ext.get("currency_code", "KES"),
                    "parcel_number":      ext.get("parcel_number") or t.get("parcel_number", ""),
                    "officers":           officers,
                    "assessor":           _resolve_assessor(officers),
                })
                stats["county_kept"] += 1

    tasks.sort(key=lambda x: x["date_created"], reverse=True)
    _log_fetch_tasks(tasks)
    return tasks, stats


def _days_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 day",  callback_data="ft_days:1"),
            InlineKeyboardButton("2 days", callback_data="ft_days:2"),
            InlineKeyboardButton("3 days", callback_data="ft_days:3"),
        ],
        [
            InlineKeyboardButton("5 days",  callback_data="ft_days:5"),
            InlineKeyboardButton("7 days",  callback_data="ft_days:7"),
            InlineKeyboardButton("10 days", callback_data="ft_days:10"),
        ],
        [InlineKeyboardButton("✏️ Enter custom", callback_data="ft_days:custom")],
    ])


# ──────────────────────────────────────────────────────────
# Conversation handlers
# ──────────────────────────────────────────────────────────

async def cmd_fetch_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    ctx.user_data["ft_session"] = FTSession()
    await update.message.reply_text(
        "📊 *Fetch Tasks* — Select the account to use:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return FT.CHOOSE_CRED


async def recv_ft_cred(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cred_type = query.data.split(":")[1]
    sess = _get_ft_sess(ctx)
    sess.cred_type = cred_type

    cached = get_valid_tokens(cred_type)
    if cached:
        sess.tokens = cached
        await query.edit_message_text(
            f"✅ Using cached tokens for *{CRED_LABELS[cred_type]}*.\n\n"
            "How many *days back* should I look?\nTap a button or type a number:",
            parse_mode="Markdown",
            reply_markup=_days_keyboard(),
        )
        return FT.DAYS_BACK

    sess.http_session = build_session()
    creds = CRED_MAP[cred_type]
    await query.edit_message_text(
        f"🔐 Sending login request for *{CRED_LABELS[cred_type]}*…",
        parse_mode="Markdown",
    )
    try:
        resp = sess.http_session.post(
            AUTH_LOGIN_URL,
            json={"username": creds["username"], "password": creds["password"],
                  "usertype": creds["usertype"], "otpcode": ""},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") is False and "error" in data:
            raise RuntimeError(data.get("error") or data.get("message"))
    except Exception as e:
        await query.message.reply_text(
            f"❌ Login failed: `{e}`", parse_mode="Markdown", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    await query.message.reply_text(
        "📲 OTP sent to the registered device.\nPlease *reply with the OTP code*:",
        parse_mode="Markdown",
    )
    return FT.WAIT_OTP


async def recv_ft_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    otp   = update.message.text.strip()
    sess  = _get_ft_sess(ctx)
    creds = CRED_MAP[sess.cred_type]

    await update.message.reply_text("🔄 Verifying OTP…")
    try:
        resp = sess.http_session.post(
            AUTH_OTP_VERIFY_URL,
            json={"username": creds["username"], "password": creds["password"], "otpcode": otp},
            timeout=30,
        )
        resp.raise_for_status()
        data          = resp.json()
        details       = data.get("details", {})
        access_token  = details.get("access_token")
        jwt           = details.get("jwt")
        refresh_token = details.get("refresh_token", "")
        if not access_token or not jwt:
            raise RuntimeError(f"Tokens missing. Keys: {list(data.keys())}")
        persist_tokens(sess.cred_type, access_token, jwt, refresh_token)
        sess.tokens = AuthTokens(access_token=access_token, jwt=jwt)
    except Exception as e:
        await update.message.reply_text(
            f"❌ OTP verification failed: `{e}`\n\nSend the OTP again or tap 🛑 Cancel.",
            parse_mode="Markdown",
        )
        return FT.WAIT_OTP

    await update.message.reply_text(
        f"✅ Authenticated as *{CRED_LABELS[sess.cred_type]}*.\n\n"
        "How many *days back* should I look?\nTap a button or type a number:",
        parse_mode="Markdown",
        reply_markup=_days_keyboard(),
    )
    return FT.DAYS_BACK


async def recv_ft_days_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val  = query.data.split(":")[1]
    sess = _get_ft_sess(ctx)

    if val == "custom":
        await query.edit_message_text(
            "Enter the number of days to look back (1–90):",
        )
        return FT.DAYS_BACK

    sess.days_back = int(val)
    await query.edit_message_text(
        f"✅ *{sess.days_back}* day(s) selected.\n\nFilter by *county*?",
        parse_mode="Markdown",
        reply_markup=_ft_county_keyboard(),
    )
    return FT.COUNTY_FILTER


async def recv_ft_days_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    sess = _get_ft_sess(ctx)
    try:
        days = int(text)
        if days < 1 or days > 90:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please enter a number between 1 and 90:",
            reply_markup=_days_keyboard(),
        )
        return FT.DAYS_BACK

    sess.days_back = days
    await update.message.reply_text(
        f"✅ *{sess.days_back}* day(s) selected.\n\nFilter by *county*?",
        parse_mode="Markdown",
        reply_markup=_ft_county_keyboard(),
    )
    return FT.COUNTY_FILTER


async def recv_ft_county_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sess = _get_ft_sess(ctx)

    sess.county_filter = "" if query.data == "ft_county:all" else query.data.split(":")[1]
    label = f"*{sess.county_filter.title()}*" if sess.county_filter else "*All Counties*"
    await query.edit_message_text(
        f"County: {label}\n\nFilter by *registry*?",
        parse_mode="Markdown",
        reply_markup=_ft_registry_keyboard(),
    )
    return FT.REGISTRY_FILTER


async def recv_ft_registry_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sess = _get_ft_sess(ctx)

    sess.registry_filter = "" if query.data == "ft_registry:all" else query.data.split(":")[1]
    reg_label    = f"*{sess.registry_filter.title()}*" if sess.registry_filter else "*All Registries*"
    county_label = f"*{sess.county_filter.title()}*"   if sess.county_filter   else "*All Counties*"
    await query.edit_message_text(
        f"County: {county_label} | Registry: {reg_label}\n\nFilter by *amount*?",
        parse_mode="Markdown",
        reply_markup=_ft_amount_keyboard(),
    )
    return FT.AMOUNT_FILTER


async def recv_ft_amount_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sess = _get_ft_sess(ctx)

    choice = query.data

    if choice == "ft_amount:custom":
        await query.edit_message_text(
            "Enter the amount range as *min max* (space-separated), in KES:\n"
            "e.g. `500000 2000000`",
            parse_mode="Markdown",
        )
        return FT.AMOUNT_TEXT

    ranges = {
        "ft_amount:0_1m":    (0.0,           1_000_000.0),
        "ft_amount:1m_5m":   (1_000_000.0,   5_000_000.0),
        "ft_amount:5m_10m":  (5_000_000.0,  10_000_000.0),
        "ft_amount:20m_50m": (20_000_000.0, 50_000_000.0),
        "ft_amount:50m_100m":(50_000_000.0,100_000_000.0),
        "ft_amount:10m_80m": (10_000_000.0, 80_000_000.0),
        "ft_amount:10m_50m": (10_000_000.0, 50_000_000.0),
        "ft_amount:50m_3b":  (50_000_000.0,  3_000_000_000.0),
        "ft_amount:all":     (None,           None),
    }
    sess.amount_min, sess.amount_max = ranges.get(choice, (None, None))
    await query.edit_message_text(
        "Include sectional properties?\n_(Sectional: parcel has 4 parts e.g. Nairobi/Block12/345/888)_",
        parse_mode="Markdown",
        reply_markup=_sectional_keyboard(),
    )
    return FT.SECTIONAL_FILTER


async def recv_ft_amount_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip()
    sess  = _get_ft_sess(ctx)
    parts = text.replace(",", "").split()
    try:
        if len(parts) == 2:
            # Python evaluates the full RHS tuple before the assignment, so if
            # float(parts[1]) raises, neither sess.amount_min nor sess.amount_max
            # is modified — the session state stays clean.
            sess.amount_min, sess.amount_max = float(parts[0]), float(parts[1])
        elif len(parts) == 1:
            sess.amount_min, sess.amount_max = 0.0, float(parts[0])
        else:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Could not parse that. Enter two numbers separated by a space, e.g. `500000 2000000`",
            parse_mode="Markdown",
        )
        return FT.AMOUNT_TEXT

    await update.message.reply_text(
        "Include sectional properties?\n_(Sectional: parcel has 4 parts e.g. Nairobi/Block12/345/888)_",
        parse_mode="Markdown",
        reply_markup=_sectional_keyboard(),
    )
    return FT.SECTIONAL_FILTER


async def recv_ft_sectional_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sess = _get_ft_sess(ctx)
    sess.sectional_filter = query.data.split(":")[1]  # "exclude" | "only" | "all"
    await query.edit_message_text(
        f"⏳ Fetching tasks from the last *{sess.days_back}* day(s)…",
        parse_mode="Markdown",
    )
    return await _ft_do_fetch(query.message, ctx, sess)


async def _ft_do_fetch(message, ctx: ContextTypes.DEFAULT_TYPE, sess: FTSession):
    """Fetch tasks, apply county / registry / amount filters, then show results."""
    try:
        tasks, stats = _load_fetch_tasks(sess.tokens, sess.days_back)
    except Exception as e:
        await message.reply_text(
            f"❌ Fetch failed: `{e}`", parse_mode="Markdown", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    sess.stats = stats
    hq_raw  = stats.get("hq_raw",     0)
    c_raw   = stats.get("county_raw", 0)
    hq_kept = stats.get("hq_kept",    0)
    c_kept  = stats.get("county_kept", 0)

    # County filter (partial, case-insensitive)
    if sess.county_filter:
        tasks = [
            t for t in tasks
            if sess.county_filter in (t.get("county") or "").strip().lower()
        ]

    # Registry filter (partial, case-insensitive)
    if sess.registry_filter:
        tasks = [
            t for t in tasks
            if sess.registry_filter in (t.get("registry") or "").strip().lower()
        ]

    # Amount filter — strip commas/spaces so "1,500,000.00" parses correctly
    if sess.amount_min is not None or sess.amount_max is not None:
        def _in_range(t):
            raw = t.get("consideration")
            if raw is None:
                return False
            try:
                val = float(str(raw).replace(",", "").strip())
            except (ValueError, TypeError):
                return False
            if sess.amount_min is not None and val < sess.amount_min:
                return False
            if sess.amount_max is not None and val > sess.amount_max:
                return False
            return True
        tasks = [t for t in tasks if _in_range(t)]

    sess.tasks = tasks

    # Build active-filter summary for the header
    filter_parts = []
    if sess.county_filter:
        filter_parts.append(f"County: {sess.county_filter.title()}")
    if sess.registry_filter:
        filter_parts.append(f"Registry: {sess.registry_filter.title()}")
    if sess.amount_min is not None or sess.amount_max is not None:
        lo = f"KES {int(sess.amount_min):,}" if sess.amount_min is not None else "0"
        hi = f"KES {int(sess.amount_max):,}" if sess.amount_max is not None else "∞"
        filter_parts.append(f"Amount: {lo} – {hi}")
    filter_line = ("_Filters: " + " | ".join(filter_parts) + "_\n") if filter_parts else ""

    if not tasks:
        await message.reply_text(
            f"ℹ️ No qualifying tasks in the last {sess.days_back} day(s).\n"
            f"{filter_line}"
            f"(HQ: {hq_raw} seen → {hq_kept} matched | "
            f"County: {c_raw} seen → {c_kept} matched)",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    # Sectional filter (parcel with 4+ parts = sectional)
    sf = sess.sectional_filter
    if sf == "exclude":
        tasks = [t for t in tasks if str(t.get("parcel_number") or "").count("/") < 3]
    elif sf == "only":
        tasks = [t for t in tasks if str(t.get("parcel_number") or "").count("/") >= 3]
    # "all" → no filter

    # Remove tasks already queued in the DLV batch
    queued_refs = {item.get("ref", "") for item in load_dlv_batch()}
    tasks_before = len(tasks)
    tasks = [t for t in tasks if t.get("reference_number", "") not in queued_refs]
    queued_removed = tasks_before - len(tasks)

    queued_note = f" ({queued_removed} already in DLV queue — excluded)" if queued_removed else ""

    if not tasks:
        await message.reply_text(
            f"ℹ️ All {tasks_before} task(s) are already in the DLV queue.",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    await message.reply_text(
        f"✅ {len(tasks)} task(s) found — last {sess.days_back} day(s).{queued_note}\n"
        f"{filter_line}"
        f"(HQ: {hq_raw} → {hq_kept} | County: {c_raw} → {c_kept})",
    )
    try:
        await _ft_show_results(message, tasks)
    except Exception as e:
        logger.error("_ft_show_results error: %s", e)
        await message.reply_text(
            "❌ Failed to display results — please try again.", reply_markup=_main_menu()
        )
    return ConversationHandler.END


def _ft_format_task_block(i: int, t: dict, markdown: bool = False) -> str:
    """Fetch Tasks/Auto Fetch's task block (source, assessor, registry,
    county, consideration, parcel, added) — see task_block.format_labeled_block
    for the shared visual every report in the bot uses; only these fields
    differ. Status/Node/Valuer aren't shown since these are pre-assignment
    tasks and don't have them.

    markdown=True wraps the ref in backticks (tap-to-copy in Telegram) and
    bolds its label; markdown=False (default, used by Auto Fetch's plain-text
    email) leaves it unadorned since email doesn't parse Markdown.

    If _extract_assessor didn't find an ASSESSOR_OF_STAMP_DUTY match, falls
    back to listing whoever IS in the officers list rather than showing a
    bare dash — the old view showed this raw list unconditionally; losing it
    entirely made the assessor's name disappear whenever extraction missed."""
    source   = t.get("source") or "—"
    registry = (t.get("registry") or "—").upper()
    county   = (t.get("county") or "—").upper()
    date     = (t.get("date_created") or "")[:10] or "—"

    assessor = t.get("assessor") or ""
    if not assessor:
        officers = t.get("officers") or []
        assessor = ", ".join(
            f"{o.get('name', '')} ({o.get('role', '')})" for o in officers if o.get("name")
        ) or "—"

    fields = [
        ("🗂 Source", source),
        ("Assessor", assessor),
        consideration_field(t),
        parcel_field(t),
        ("🏢 Registry", registry),
        ("📍 County", county),
        ("📅 Added", date),
    ]
    tag = tag_field(t)
    if tag:
        fields.append(tag)

    return format_labeled_block(i, t.get("reference_number"), fields, markdown=markdown)


async def _ft_show_results(message, tasks: List[Dict]):
    """Send tasks as formatted text, splitting at Telegram's 4096-char limit."""
    if not tasks:
        await message.reply_text("No tasks to display.", reply_markup=_main_menu())
        return

    lines = [_ft_format_task_block(i, t, markdown=True) for i, t in enumerate(tasks, 1)]

    async def _send(text, reply_markup):
        try:
            await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception as e:
            logger.warning("_ft_show_results send failed: %s", e)
            await message.reply_text(text[:4000], reply_markup=reply_markup)

    await _send_chunked_report(
        _send, lines,
        footer=f"\n\nTotal: {len(tasks)} task(s)",
        reply_markup=_main_menu(),
    )


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Fetch Tasks conversation into the given Application."""
    fetch_conv = ConversationHandler(
        entry_points=[
            CommandHandler("fetch", cmd_fetch_tasks),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_FETCH_TASKS)}$"), cmd_fetch_tasks),
        ],
        states={
            FT.CHOOSE_CRED:   [CallbackQueryHandler(recv_ft_cred,           pattern=r"^cred:")],
            FT.WAIT_OTP:      [MessageHandler(not_cancel, recv_ft_otp)],
            FT.DAYS_BACK:     [
                CallbackQueryHandler(recv_ft_days_callback, pattern=r"^ft_days:"),
                MessageHandler(not_cancel, recv_ft_days_text),
            ],
            FT.COUNTY_FILTER:   [CallbackQueryHandler(recv_ft_county_filter,   pattern=r"^ft_county:")],
            FT.REGISTRY_FILTER: [CallbackQueryHandler(recv_ft_registry_filter, pattern=r"^ft_registry:")],
            FT.AMOUNT_FILTER:    [CallbackQueryHandler(recv_ft_amount_filter,    pattern=r"^ft_amount:")],
            FT.AMOUNT_TEXT:      [MessageHandler(not_cancel, recv_ft_amount_text)],
            FT.SECTIONAL_FILTER: [CallbackQueryHandler(recv_ft_sectional_filter, pattern=r"^ft_sectional:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(fetch_conv)
