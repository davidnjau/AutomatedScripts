#!/usr/bin/env python3
"""
receive_tasks.py
================
Receive Tasks — pull unassigned stamp-duty tasks from the queue and assign
them to a valuer (/receive), plus the scheduled repeating variant, and the
history viewers /schedules and /task_batches.

Call register(app) from bot.py's main() to wire this feature in (this also
restores any active repeating schedules on startup, matching the pattern
already established by morning_briefing.py and auto_fetch.py).
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
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

from ardhisasa_auth import AuthTokens, build_session
from common import (
    CPARAMS_DLV,
    CRED_LABELS,
    CRED_MAP,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _cred_keyboard,
    _ensure_data_dir,
    _ft_amount_keyboard,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    load_saved_valuers,
    logger,
    md_escape,
    not_cancel,
    persist_assignment,
    persist_tokens,
    _safe_err,
)
from endpoints import (
    ACCOUNTS_BY_ID_URL,
    ACCOUNTS_GET_USER_DETAIL_URL,
    ACCOUNTS_LIST_URL,
    ACCOUNTS_USER_DETAILS_URL,
    ACCOUNTS_VIEW_USER_URL,
    AUTH_LOGIN_URL,
    AUTH_OTP_VERIFY_URL,
    STAMP_DUTY_APPLICATION_DETAIL_URL,
    STAMP_DUTY_APPLICATION_LIST_URL,
    STAMP_DUTY_FIX_APPLICATION_URL,
)
from telegram_report import _send_chunked_report

SAVED_TASK_BATCHES_FILE = os.path.join(DATA_DIR, "saved_task_batches.json")
SAVED_SCHEDULES_FILE    = os.path.join(DATA_DIR, "saved_schedules.json")


# ──────────────────────────────────────────────────────────
# States — Receive Tasks conversation
# ──────────────────────────────────────────────────────────
class RS(Enum):
    PICK_STAFF_SOURCE = auto()   # choose saved valuer or search new
    STAFF_NAME        = auto()
    SELECT_STAFF      = auto()
    CHOOSE_CRED       = auto()
    WAIT_OTP          = auto()
    TASK_TYPE         = auto()   # choose Stamp Duty vs County Stamp Duty (when staff has both)
    TASK_COUNT        = auto()
    AMOUNT_RANGE      = auto()   # shows Enter / Skip buttons
    AMOUNT_TEXT       = auto()   # text input for min-max after choosing Enter
    SCHEDULE_CHOICE   = auto()
    SCHEDULE_INTERVAL = auto()
    RT_CONFIRM        = auto()


# ──────────────────────────────────────────────────────────
# Per-user session — Receive Tasks
# ──────────────────────────────────────────────────────────
@dataclass
class RTSession:
    staff_name:               str = ""
    staff_results:            List[Dict] = field(default_factory=list)
    staff_data:               Optional[Dict] = None
    saved_valuer:             Optional[Dict] = None   # {"name", "uid", "account_number"} — pre-selected
    cred_type:                str = "publicuser"
    session:                  Optional[object] = None   # requests.Session
    tokens:                   Optional[AuthTokens] = None
    task_count:               int = 0
    amount_min:               Optional[float] = None
    amount_max:               Optional[float] = None
    task_type:                str = ""   # "STAMP_DUTY" or "COUNTY_STAMP_DUTY"
    staff_registry:           str = ""
    staff_county:             str = ""
    matched_tasks:            List[Dict] = field(default_factory=list)
    schedule_interval_minutes: Optional[int] = None


# ──────────────────────────────────────────────────────────
# Receive Tasks — persistence helpers
# ──────────────────────────────────────────────────────────

def load_task_batches() -> List[Dict]:
    try:
        with open(SAVED_TASK_BATCHES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def persist_task_batch(batch: Dict):
    _ensure_data_dir()
    batches = load_task_batches()
    batches.append(batch)
    # Keep only the most recent 100 batches to prevent unbounded growth
    if len(batches) > 100:
        batches = batches[-100:]
    _atomic_json_write(SAVED_TASK_BATCHES_FILE, batches, indent=2)
    logger.info("Saved task batch %s (%d tasks)", batch["batch_id"], len(batch["tasks"]))


def load_schedules() -> List[Dict]:
    try:
        with open(SAVED_SCHEDULES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_schedules(schedules: List[Dict]):
    _atomic_json_write(SAVED_SCHEDULES_FILE, schedules, indent=2)


def persist_schedule(sched: Dict):
    schedules = load_schedules()
    schedules = [s for s in schedules if s["schedule_id"] != sched["schedule_id"]]
    schedules.append(sched)
    _save_schedules(schedules)
    logger.info("Saved schedule %s (%dmin interval)", sched["schedule_id"], sched["interval_minutes"])


# ──────────────────────────────────────────────────────────
# Receive Tasks — staff validation
# ──────────────────────────────────────────────────────────

def _validate_staff(user_data: Dict) -> Tuple[bool, str, str, str, str]:
    """
    Check eligibility for task receipt.
    All detail fields live inside staff_details, not at the top level.
    Returns (ok, error_msg, task_type, staff_registry, staff_county).
    task_type is "STAMP_DUTY", "COUNTY_STAMP_DUTY", or "BOTH" (user must choose).
    registry/county are populated only for COUNTY_STAMP_DUTY / BOTH.
    """
    if user_data.get("account_status", "").upper() != "ACTIVE":
        return False, "Account status is not ACTIVE.", "", "", ""

    sd = user_data.get("staff_details") or {}

    dept_code = (
        sd.get("department_details", {})
          .get("department", {})
          .get("code", "")
    )
    if dept_code.upper() != "DLV":
        return False, f"Department is '{dept_code}', expected DLV.", "", "", ""

    roles      = sd.get("roles") or []
    role_names = [r.get("rolename", "").upper() for r in roles]
    if "VALUER" not in role_names:
        return False, f"No VALUER role found. Roles: {role_names}", "", "", ""

    has_county_valuer = "COUNTY_VALUER" in role_names

    if has_county_valuer:
        county_units = sd.get("county_units") or []
        if not county_units:
            return False, "COUNTY_VALUER role but no county_units found on account.", "", "", ""
        primary  = next((u for u in county_units if u.get("is_primary")), county_units[0])
        registry = primary.get("registry", "").upper()
        county   = primary.get("county", "").upper()
        # Has both VALUER and COUNTY_VALUER — let the user pick the task pool
        return True, "", "BOTH", registry, county

    return True, "", "STAMP_DUTY", "", ""


# ──────────────────────────────────────────────────────────
# Receive Tasks — API helpers
# ──────────────────────────────────────────────────────────

def _rt_auth_headers(rt: RTSession) -> Dict:
    return {
        "Authorization": f"Bearer {rt.tokens.access_token}",
        "JWTAUTH":       f"Bearer {rt.tokens.jwt}",
        "cparams":       CPARAMS_DLV,
    }


def _fetch_tasks(rt: RTSession, needed: int) -> List[Dict]:
    """
    Paginate the task list endpoint, returning tasks that pass the
    node/status pre-filter (and county+registry filter for COUNTY_STAMP_DUTY).
    Stops once we have needed*5 candidates (to leave headroom for detail filtering).
    """
    headers = _rt_auth_headers(rt)
    if rt.task_type == "STAMP_DUTY":
        base_params: Dict = {
            "filter": "Pending", "role": "DLV",
            "request_type": "STAMP_DUTY", "search": "",
        }
    else:
        base_params = {
            "filter": "Ongoing", "from_ardhipay": "true",
            "role": "DLV", "request_type": "COUNTY_STAMP_DUTY", "search": "",
        }

    tasks: List[Dict] = []
    page = 1
    target = max(needed * 5, 50)

    while len(tasks) < target:
        resp = rt.session.get(
            STAMP_DUTY_APPLICATION_LIST_URL,
            headers=headers, params={**base_params, "page": page}, timeout=30,
        )
        resp.raise_for_status()
        data    = resp.json()
        results = data.get("results", [])
        if not results:
            break

        for task in results:
            if (task.get("application_status") == "ONGOING"
                    and task.get("node") == "VALUATION_STAMP_DUTY_CREATED"):
                if rt.task_type == "COUNTY_STAMP_DUTY":
                    if (task.get("registry", "").upper() != rt.staff_registry
                            or task.get("county", "").upper() != rt.staff_county):
                        continue
                tasks.append(task)

        if not data.get("next"):
            break
        page += 1

    return tasks


def _fetch_task_detail(rt: RTSession, task_id: str) -> Optional[Dict]:
    """
    Call the detail-view endpoint and return the payload only if all three
    conditions are met: application_status=ONGOING, node=VALUATION_STAMP_DUTY_CREATED,
    node_code=APPLICATION_AWAITING_VALUATION.
    Returns None if any condition fails or the request errors.
    """
    try:
        resp = rt.session.get(
            STAMP_DUTY_APPLICATION_DETAIL_URL,
            headers=_rt_auth_headers(rt),
            params={"request_id": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("detail-view failed for %s: %s", task_id, e)
        return None

    if (data.get("application_status") != "ONGOING"
            or data.get("node") != "VALUATION_STAMP_DUTY_CREATED"):
        return None

    ext = data.get("external_process_details", {})
    if ext.get("node_code") != "APPLICATION_AWAITING_VALUATION":
        return None

    return data


def _verify_and_filter_tasks(
    rt: RTSession,
    candidates: List[Dict],
) -> List[Dict]:
    """
    For each candidate, call detail-view, apply the three mandatory checks,
    then apply the optional consideration_amount range filter.
    Stops once rt.task_count tasks are matched.
    """
    matched: List[Dict] = []
    for task in candidates:
        if len(matched) >= rt.task_count:
            break
        detail = _fetch_task_detail(rt, task["id"])
        if detail is None:
            continue
        ext    = detail.get("external_process_details", {})
        amount = float(ext.get("consideration_amount") or 0)
        if rt.amount_min is not None and amount < rt.amount_min:
            continue
        if rt.amount_max is not None and amount > rt.amount_max:
            continue
        matched.append({
            "id":                   task["id"],
            "reference_number":     task["reference_number"],
            "consideration_amount": amount,
            "parcel_number":        task.get("parcel_number", ""),
            "registry":             task.get("registry", ""),
            "county":               task.get("county", ""),
            "date_created":         task.get("date_created", ""),
        })
    return matched


async def _do_assign_tasks(
    bot,
    chat_id: int,
    http_sess,
    tokens: AuthTokens,
    tasks: List[Dict],
    staff_uid: str,
    staff_name: str,
    cred_type: str,
):
    """POST assignments and send a result summary to chat_id."""
    url     = STAMP_DUTY_FIX_APPLICATION_URL
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }

    ok_refs: List[str]   = []
    fail_refs: List[str] = []
    result_lines: List[str] = []

    for task in tasks:
        ref = task["reference_number"]
        try:
            r = http_sess.post(
                url, headers=headers,
                json={
                    "reference_number":   ref,
                    "valuation_officer":  staff_uid,
                    "node":               "VALUATION_STAMP_DUTY_VALUER_REPORT",
                },
                timeout=30,
            )
            r.raise_for_status()
            ok_refs.append(ref)
            result_lines.append(f"✅ `{ref}` — KES {task['consideration_amount']:,.0f}")
            persist_assignment(ref, staff_name, staff_uid)
        except Exception as e:
            logger.error("Assignment failed for %s: %s", ref, e)
            fail_refs.append(ref)
            result_lines.append(f"❌ `{ref}` — {_safe_err(e)}")

    # Persist the batch
    batch = {
        "batch_id":   str(uuid.uuid4()),
        "staff_name": staff_name,
        "staff_uid":  staff_uid,
        "cred_type":  cred_type,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks":      [t for t in tasks if t["reference_number"] in ok_refs],
        "failed":     fail_refs,
    }
    persist_task_batch(batch)

    header = (
        f"🏁 *Receive Tasks Complete*\n\n"
        f"*Valuer:* {md_escape(staff_name)}\n"
        f"*Assigned:* {len(ok_refs)} / {len(tasks)}\n"
        f"*Failed:*   {len(fail_refs)} / {len(tasks)}\n"
    )

    async def _send(text, reply_markup):
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, [header] + result_lines, join="\n")


# ──────────────────────────────────────────────────────────
# Receive Tasks — scheduled job
# ──────────────────────────────────────────────────────────

async def _receive_tasks_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue callback: runs receive-tasks automatically on a schedule."""
    job_data  = context.job.data
    chat_id   = job_data["chat_id"]
    sched     = job_data["schedule"]
    cred_type = sched["cred_type"]

    tokens = get_valid_tokens(cred_type)
    if not tokens:
        await context.bot.send_message(
            chat_id,
            "⚠️ *Scheduled receive-tasks failed:* cached tokens are expired.\n"
            "Use */receive* to re-authenticate and reschedule.",
            parse_mode="Markdown",
        )
        return

    await context.bot.send_message(chat_id, "⏰ *Scheduled receive-tasks running…*", parse_mode="Markdown")

    rt = RTSession()
    rt.tokens               = tokens
    rt.session              = build_session()
    rt.task_type            = sched["task_type"]
    rt.staff_registry       = sched.get("staff_registry", "")
    rt.staff_county         = sched.get("staff_county", "")
    rt.task_count           = sched["task_count"]
    rt.amount_min           = sched.get("amount_min")
    rt.amount_max           = sched.get("amount_max")

    try:
        candidates = _fetch_tasks(rt, rt.task_count)
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Task fetch failed: `{e}`", parse_mode="Markdown")
        return

    if not candidates:
        await context.bot.send_message(chat_id, "ℹ️ Scheduled run: no eligible tasks found.")
        return

    matched = _verify_and_filter_tasks(rt, candidates)
    if not matched:
        await context.bot.send_message(chat_id, "ℹ️ Scheduled run: no tasks passed detail-view verification.")
        return

    await _do_assign_tasks(
        context.bot, chat_id,
        rt.session, tokens,
        matched,
        sched["staff_uid"], sched["staff_name"], cred_type,
    )


def _restore_schedules(app) -> None:
    """Re-register active scheduled jobs from persistent storage on startup."""
    schedules = load_schedules()
    restored  = 0
    for sched in schedules:
        if not sched.get("active", True):
            continue
        interval = sched["interval_minutes"] * 60
        app.job_queue.run_repeating(
            _receive_tasks_job,
            interval=interval,
            first=interval,
            data={"chat_id": sched["chat_id"], "schedule": sched},
            name=f"rt_{sched['schedule_id']}",
        )
        restored += 1
    if restored:
        logger.info("Restored %d scheduled receive-tasks job(s).", restored)


# ──────────────────────────────────────────────────────────
# Receive Tasks — conversation helpers
# ──────────────────────────────────────────────────────────

def _get_rt(ctx: ContextTypes.DEFAULT_TYPE) -> RTSession:
    if "rt_session" not in ctx.user_data:
        ctx.user_data["rt_session"] = RTSession()
    return ctx.user_data["rt_session"]


def _fetch_staff_detail(rt: RTSession, list_entry: Dict) -> Dict:
    """
    Fetch the full staff profile from the detail endpoint.
    The list-user-accounts response only returns summary fields;
    department_details / roles / ardhipay_roles / county_units come from here.
    Falls back to the list entry if all attempts fail.
    """
    headers = {
        "Authorization": f"Bearer {rt.tokens.access_token}",
        "JWTAUTH":       f"Bearer {rt.tokens.jwt}",
    }
    account_id = list_entry.get("id", "")
    user_id    = list_entry.get("staff_details", {}).get("user_id", account_id)

    candidates = [
        f"{ACCOUNTS_GET_USER_DETAIL_URL}?user_id={user_id}",
        f"{ACCOUNTS_GET_USER_DETAIL_URL}/{user_id}",
        f"{ACCOUNTS_USER_DETAILS_URL}?user_id={user_id}",
        f"{ACCOUNTS_VIEW_USER_URL}?user_id={user_id}",
        f"{ACCOUNTS_BY_ID_URL}/{account_id}",
    ]

    for url in candidates:
        try:
            resp = rt.session.get(url, headers=headers, timeout=15)
            logger.info("Staff detail probe %s → HTTP %s body: %.300s", url, resp.status_code, resp.text)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if data.get("department_details") or data.get("roles") or data.get("ardhipay_roles"):
                logger.info("Staff detail fetched from: %s  keys: %s", url, list(data.keys()))
                return data
        except Exception as e:
            logger.warning("Detail attempt failed (%s): %s", url, e)

    logger.warning(
        "Could not fetch staff detail — falling back to list entry.\n"
        "list entry staff_details: %s",
        json.dumps(list_entry.get("staff_details"), default=str),
    )
    return list_entry


async def _rt_resolve_saved_valuer(message, rt: RTSession) -> int:
    """
    After auth, fetch and validate a pre-selected saved valuer.
    Searches by name and matches on account_number, then runs validation.
    """
    sv = rt.saved_valuer
    try:
        headers = {
            "Authorization": f"Bearer {rt.tokens.access_token}",
            "JWTAUTH":       f"Bearer {rt.tokens.jwt}",
        }
        resp = rt.session.get(
            ACCOUNTS_LIST_URL,
            headers=headers,
            params={"account_type": "STAFF", "filter_type": "ACTIVE",
                    "page": 1, "search": sv["name"]},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        await message.reply_text(
            f"❌ Could not fetch profile for *{md_escape(sv['name'])}*: `{e}`",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    # Match by account_number first, fall back to uid, then first result
    match = (
        next((r for r in results if r.get("account_number") == sv["account_number"]), None)
        or next((r for r in results if r.get("id") == sv["uid"]), None)
        or (results[0] if len(results) == 1 else None)
    )
    if not match:
        await message.reply_text(
            f"⚠️ Could not uniquely identify *{md_escape(sv['name'])}* from search results.\n"
            "Use 🔍 Search new valuer to select manually.",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    user_data = _fetch_staff_detail(rt, match)

    sd   = user_data.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))

    ok, err_msg, task_type, registry, county = _validate_staff(user_data)
    if not ok:
        await message.reply_text(
            f"❌ *Validation failed for {name}:*\n{err_msg}",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    rt.staff_data     = user_data
    rt.staff_registry = registry
    rt.staff_county   = county

    sd_roles    = sd.get("roles") or []
    role_labels = ", ".join(r.get("rolename", "") for r in sd_roles) or "None"

    if task_type == "BOTH":
        await message.reply_text(
            f"✅ *Staff Validated*\n\n"
            f"*Name:* {name}\n"
            f"*Roles:* {role_labels}\n"
            f"*County:* {county}  |  *Registry:* {registry}\n\n"
            "Step 3 — Which task pool do you want to assign from?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏛 Stamp Duty (national, from_ardhipay=false)",
                                      callback_data="rt_type:STAMP_DUTY")],
                [InlineKeyboardButton("🏙 County Stamp Duty (from_ardhipay=true)",
                                      callback_data="rt_type:COUNTY_STAMP_DUTY")],
            ]),
        )
        return RS.TASK_TYPE

    rt.task_type = task_type
    type_label   = "County Stamp Duty" if task_type == "COUNTY_STAMP_DUTY" else "Stamp Duty"
    await message.reply_text(
        f"✅ *Staff Validated*\n\n"
        f"*Name:* {name}\n"
        f"*Task Type:* {type_label}\n"
        f"*Roles:* {role_labels}\n\n"
        "Step 3 — How many tasks do you want to assign?",
        parse_mode="Markdown",
    )
    return RS.TASK_COUNT


async def _rt_do_staff_search(message, rt: RTSession) -> int:
    """Search the accounts endpoint and show a selection keyboard."""
    try:
        headers = {
            "Authorization": f"Bearer {rt.tokens.access_token}",
            "JWTAUTH":       f"Bearer {rt.tokens.jwt}",
        }
        resp = rt.session.get(
            ACCOUNTS_LIST_URL,
            headers=headers,
            params={"account_type": "STAFF", "filter_type": "ACTIVE",
                    "page": 1, "search": rt.staff_name},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        await message.reply_text(
            f"❌ Staff search failed: `{e}`", parse_mode="Markdown", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    if not results:
        await message.reply_text(
            f"⚠️ No staff found matching *{md_escape(rt.staff_name)}*.",
            parse_mode="Markdown", reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    rt.staff_results = results
    rows = []
    for i, v in enumerate(results):
        sd   = v.get("staff_details", {})
        name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
        rows.append([InlineKeyboardButton(name or f"Staff {i+1}", callback_data=f"rt_staff:{i}")])

    await message.reply_text(
        f"Found *{len(results)}* staff member(s). Select one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return RS.SELECT_STAFF


async def _rt_fetch_and_show(message, rt: RTSession) -> int:
    """Fetch + verify tasks, display them, and ask for confirmation."""
    await message.reply_text("⏳ Fetching eligible tasks from the queue…")

    # Peek at the total count first
    headers = _rt_auth_headers(rt)
    if rt.task_type == "STAMP_DUTY":
        peek_params: Dict = {
            "filter": "Pending", "role": "DLV",
            "request_type": "STAMP_DUTY", "search": "", "page": 1,
        }
    else:
        peek_params = {
            "filter": "Ongoing", "from_ardhipay": "true",
            "role": "DLV", "request_type": "COUNTY_STAMP_DUTY", "search": "", "page": 1,
        }

    try:
        peek = rt.session.get(
            STAMP_DUTY_APPLICATION_LIST_URL,
            headers=headers, params=peek_params, timeout=30,
        )
        peek.raise_for_status()
        total_count = peek.json().get("count", "?")
    except Exception:
        total_count = "?"

    await message.reply_text(
        f"📊 Total tasks in queue: *{total_count}*\n"
        f"🔍 Scanning for up to *{rt.task_count}* eligible task(s)…",
        parse_mode="Markdown",
    )

    try:
        candidates = _fetch_tasks(rt, rt.task_count)
    except Exception as e:
        await message.reply_text(f"❌ Task fetch failed: `{e}`", parse_mode="Markdown", reply_markup=_main_menu())
        return ConversationHandler.END

    if not candidates:
        await message.reply_text(
            "ℹ️ No eligible tasks found matching your filters.", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    await message.reply_text(f"🔍 Verifying *{len(candidates)}* candidate(s) via detail-view…", parse_mode="Markdown")

    matched = _verify_and_filter_tasks(rt, candidates)
    if not matched:
        await message.reply_text(
            "ℹ️ No tasks passed the detail-view verification checks.", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    rt.matched_tasks = matched

    # Build display list
    sd   = rt.staff_data.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
    lines = []
    for i, t in enumerate(matched, 1):
        lines.append(
            f"{i}. `{t['reference_number']}`\n"
            f"   💰 KES {t['consideration_amount']:,.0f}\n"
            f"   📍 {t['parcel_number']} | 🏢 {t['registry']}\n"
            f"   📅 {t['date_created'][:10]}"
        )

    header = f"📋 *Tasks for {name}* ({len(matched)} task(s))"

    async def _send(text, reply_markup):
        await message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(
        _send, [header] + lines,
        footer="\n\nConfirm assignment?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm & Assign", callback_data="rt_confirm:yes")],
            [InlineKeyboardButton("❌ Cancel",           callback_data="rt_confirm:no")],
        ]),
    )
    return RS.RT_CONFIRM


# ──────────────────────────────────────────────────────────
# Receive Tasks — conversation handlers
# ──────────────────────────────────────────────────────────

async def cmd_receive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    ctx.user_data["rt_session"] = RTSession()
    saved = load_saved_valuers()

    await update.message.reply_text(
        "📥 *Receive Tasks Flow*\n\nStep 1 — Select a valuer or search for a new one:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    if saved:
        rows = [
            [InlineKeyboardButton(f"👤 {sv['name']}", callback_data=f"rt_src:{i}")]
            for i, sv in enumerate(saved)
        ]
        rows.append([InlineKeyboardButton("🔍 Search new valuer", callback_data="rt_src:new")])
        await update.message.reply_text(
            "Choose a saved valuer or search new:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return RS.PICK_STAFF_SOURCE

    # No saved valuers — go straight to name search
    await update.message.reply_text(
        "Enter the staff member's name to search:",
        parse_mode="Markdown",
    )
    return RS.STAFF_NAME


async def recv_rt_pick_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rt   = _get_rt(ctx)
    data = query.data.split(":")[1]

    if data == "new":
        await query.edit_message_text(
            "🔍 Enter the staff member's name to search:",
            parse_mode="Markdown",
        )
        return RS.STAFF_NAME

    saved = load_saved_valuers()
    idx = int(data)
    if idx >= len(saved):
        await query.edit_message_text(
            "⚠️ That saved valuer no longer exists. Please start over.",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END
    sv = saved[idx]
    rt.saved_valuer = sv
    rt.staff_name   = sv["name"]

    await query.edit_message_text(
        f"👤 Selected: *{md_escape(sv['name'])}*\n\n"
        "Step 2 — Choose *credential profile*:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return RS.CHOOSE_CRED


async def recv_rt_staff_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rt = _get_rt(ctx)
    rt.staff_name = update.message.text.strip()
    await update.message.reply_text(
        f"🔍 Searching for: *{md_escape(rt.staff_name)}*\n\n"
        "Step 2 — Choose *credential profile*:",
        parse_mode="Markdown",
        reply_markup=_cred_keyboard(),
    )
    return RS.CHOOSE_CRED


async def recv_rt_cred_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    rt        = _get_rt(ctx)
    cred_type = query.data.split(":")[1]
    rt.cred_type = cred_type
    creds     = CRED_MAP[cred_type]

    cached = get_valid_tokens(cred_type)
    if cached:
        rt.tokens  = cached
        rt.session = build_session()
        if rt.saved_valuer:
            await query.edit_message_text(
                f"🔑 Cached login: *{CRED_LABELS[cred_type]}*\n\n"
                f"🔍 Fetching profile for *{md_escape(rt.saved_valuer['name'])}*…",
                parse_mode="Markdown",
            )
            return await _rt_resolve_saved_valuer(query.message, rt)
        await query.edit_message_text(
            f"🔑 Cached login: *{CRED_LABELS[cred_type]}*\n\n"
            f"🔍 Searching for *{md_escape(rt.staff_name)}*…",
            parse_mode="Markdown",
        )
        return await _rt_do_staff_search(query.message, rt)

    # Full login
    rt.session = build_session()
    await query.edit_message_text(
        f"✅ Credential: *{CRED_LABELS[cred_type]}*\n\n🔐 Sending login request…",
        parse_mode="Markdown",
    )
    try:
        resp = rt.session.post(
            AUTH_LOGIN_URL,
            json={"username": creds["username"], "password": creds["password"],
                  "usertype": creds["usertype"], "otpcode": ""},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") is False:
            raise RuntimeError(data.get("error") or data.get("message"))
    except Exception as e:
        await query.message.reply_text(
            f"❌ Login failed: `{e}`", parse_mode="Markdown", reply_markup=_main_menu()
        )
        return ConversationHandler.END

    await query.message.reply_text(
        "📲 OTP sent to registered device.\n\nPlease *reply with the OTP code*:",
        parse_mode="Markdown",
    )
    return RS.WAIT_OTP


async def recv_rt_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rt    = _get_rt(ctx)
    otp   = update.message.text.strip()
    creds = CRED_MAP[rt.cred_type]

    await update.message.reply_text("🔄 Verifying OTP…")
    try:
        resp = rt.session.post(
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
        rt.tokens = AuthTokens(access_token=access_token, jwt=jwt)
        persist_tokens(rt.cred_type, access_token, jwt, refresh_token)
    except Exception as e:
        await update.message.reply_text(
            f"❌ OTP failed: `{e}`\n\nSend the OTP again or tap 🛑 Cancel.",
            parse_mode="Markdown",
        )
        return RS.WAIT_OTP

    await update.message.reply_text("✅ Authenticated!")
    if rt.saved_valuer:
        return await _rt_resolve_saved_valuer(update.message, rt)
    return await _rt_do_staff_search(update.message, rt)


async def recv_rt_select_staff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    rt        = _get_rt(ctx)
    idx       = int(query.data.split(":")[1])
    if idx >= len(rt.staff_results):
        await query.edit_message_text(
            "⚠️ Selection no longer available. Please search again.",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END
    list_entry = rt.staff_results[idx]

    sd   = list_entry.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))

    await query.edit_message_text(f"🔍 Fetching full profile for *{md_escape(name)}*…", parse_mode="Markdown")
    user_data = _fetch_staff_detail(rt, list_entry)

    ok, err_msg, task_type, registry, county = _validate_staff(user_data)
    if not ok:
        rt.staff_results = []   # clear stale results so old callbacks can't reach them
        await query.edit_message_text(
            f"❌ *Validation failed for {name}:*\n{err_msg}",
            parse_mode="Markdown",
        )
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())
        return ConversationHandler.END

    rt.staff_data     = user_data
    rt.staff_registry = registry
    rt.staff_county   = county

    sd_roles = (user_data.get("staff_details") or {}).get("roles") or []
    role_labels = ", ".join(r.get("rolename", "") for r in sd_roles) or "None"

    if task_type == "BOTH":
        # Staff has both VALUER and COUNTY_VALUER — let user pick the task pool
        await query.edit_message_text(
            f"✅ *Staff Validated*\n\n"
            f"*Name:* {name}\n"
            f"*Roles:* {role_labels}\n"
            f"*County:* {county}  |  *Registry:* {registry}\n\n"
            "Step 3 — Which task pool do you want to assign from?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏛 Stamp Duty (national, from_ardhipay=false)",
                                      callback_data="rt_type:STAMP_DUTY")],
                [InlineKeyboardButton("🏙 County Stamp Duty (from_ardhipay=true)",
                                      callback_data="rt_type:COUNTY_STAMP_DUTY")],
            ]),
        )
        return RS.TASK_TYPE

    # Only VALUER — go straight to task count
    rt.task_type = task_type
    type_label   = "County Stamp Duty" if task_type == "COUNTY_STAMP_DUTY" else "Stamp Duty"
    await query.edit_message_text(
        f"✅ *Staff Validated*\n\n"
        f"*Name:* {name}\n"
        f"*Task Type:* {type_label}\n"
        f"*Roles:* {role_labels}\n\n"
        "Step 3 — How many tasks do you want to assign?",
        parse_mode="Markdown",
    )
    return RS.TASK_COUNT


async def recv_rt_task_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles the Stamp Duty / County Stamp Duty choice."""
    query = update.callback_query
    await query.answer()
    rt    = _get_rt(ctx)
    rt.task_type = query.data.split(":")[1]   # "STAMP_DUTY" or "COUNTY_STAMP_DUTY"

    type_label  = "County Stamp Duty" if rt.task_type == "COUNTY_STAMP_DUTY" else "Stamp Duty"
    county_line = (
        f"\n*County:* {rt.staff_county}  |  *Registry:* {rt.staff_registry}"
        if rt.task_type == "COUNTY_STAMP_DUTY" else ""
    )
    await query.edit_message_text(
        f"✅ *Task pool:* {type_label}{county_line}\n\n"
        "Step 3 — How many tasks do you want to assign?",
        parse_mode="Markdown",
    )
    return RS.TASK_COUNT


async def recv_rt_task_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rt = _get_rt(ctx)
    try:
        count = int(update.message.text.strip())
        if count <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a positive whole number.")
        return RS.TASK_COUNT

    rt.task_count = count
    await update.message.reply_text(
        f"✅ *{count} task(s)* requested.\n\nStep 4 — Filter by consideration amount?",
        parse_mode="Markdown",
        reply_markup=_ft_amount_keyboard(),
    )
    return RS.AMOUNT_RANGE


async def recv_rt_amount_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles amount preset button on the amount range step."""
    query  = update.callback_query
    await query.answer()
    rt     = _get_rt(ctx)
    choice = query.data  # e.g. "ft_amount:1m_5m" or "ft_amount:custom"

    if choice == "ft_amount:custom":
        await query.edit_message_text(
            "✏️ Enter custom amount range as *min max* (e.g. `500000 5000000`).\n"
            "Or send just one number as max.",
            parse_mode="Markdown",
        )
        return RS.AMOUNT_TEXT

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
    rt.amount_min, rt.amount_max = ranges.get(choice, (None, None))

    await query.edit_message_text(
        "Step 5 — Run now or set up a recurring schedule?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Run Now",              callback_data="rt_sched:now")],
            [InlineKeyboardButton("⏰ Schedule (repeating)", callback_data="rt_sched:schedule")],
        ]),
    )
    return RS.SCHEDULE_CHOICE


async def recv_rt_amount_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles the typed custom amount range."""
    rt    = _get_rt(ctx)
    text  = update.message.text.strip()
    parts = text.split()
    try:
        if len(parts) == 2:
            rt.amount_min = float(parts[0].replace(",", ""))
            rt.amount_max = float(parts[1].replace(",", ""))
        elif len(parts) == 1:
            rt.amount_min = None
            rt.amount_max = float(parts[0].replace(",", ""))
        else:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Could not parse. Enter two numbers e.g. `500000 5000000`, or one number as max.",
            parse_mode="Markdown",
        )
        return RS.AMOUNT_TEXT

    await update.message.reply_text(
        "Step 5 — Run now or set up a recurring schedule?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Run Now",              callback_data="rt_sched:now")],
            [InlineKeyboardButton("⏰ Schedule (repeating)", callback_data="rt_sched:schedule")],
        ]),
    )
    return RS.SCHEDULE_CHOICE


async def recv_rt_schedule_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rt     = _get_rt(ctx)
    choice = query.data.split(":")[1]

    if choice == "now":
        await query.edit_message_text("▶️ Running now…")
        return await _rt_fetch_and_show(query.message, rt)

    await query.edit_message_text(
        "⏰ *Schedule Setup*\n\n"
        "Enter the repeat interval in minutes:\n"
        "_(e.g._ `60` _= every hour,_ `1440` _= daily)_",
        parse_mode="Markdown",
    )
    return RS.SCHEDULE_INTERVAL


async def recv_rt_schedule_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rt = _get_rt(ctx)
    try:
        minutes = int(update.message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Enter a positive number of minutes.")
        return RS.SCHEDULE_INTERVAL

    rt.schedule_interval_minutes = minutes
    await update.message.reply_text(
        f"⏰ Will repeat every *{minutes} minute(s)*.\n\nFetching tasks for preview…",
        parse_mode="Markdown",
    )
    return await _rt_fetch_and_show(update.message, rt)


async def recv_rt_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "rt_confirm:no":
        await query.edit_message_text("❌ Receive tasks cancelled.")
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())
        return ConversationHandler.END

    rt   = _get_rt(ctx)
    sd   = rt.staff_data.get("staff_details", {})
    name = " ".join(filter(None, [sd.get("firstname"), sd.get("middlename"), sd.get("lastname")]))
    uid  = sd.get("user_id", rt.staff_data.get("id", "?"))

    await query.edit_message_text(
        f"⚙️ Assigning *{len(rt.matched_tasks)}* task(s) to *{md_escape(name)}*…",
        parse_mode="Markdown",
    )

    await _do_assign_tasks(
        ctx.bot, query.message.chat_id,
        rt.session, rt.tokens,
        rt.matched_tasks, uid, name, rt.cred_type,
    )

    # Register repeating schedule if requested
    if rt.schedule_interval_minutes:
        sched = {
            "schedule_id":      str(uuid.uuid4()),
            "staff_uid":        uid,
            "staff_name":       name,
            "cred_type":        rt.cred_type,
            "task_count":       rt.task_count,
            "amount_min":       rt.amount_min,
            "amount_max":       rt.amount_max,
            "task_type":        rt.task_type,
            "staff_registry":   rt.staff_registry,
            "staff_county":     rt.staff_county,
            "interval_minutes": rt.schedule_interval_minutes,
            "chat_id":          query.message.chat_id,
            "active":           True,
        }
        persist_schedule(sched)
        ctx.job_queue.run_repeating(
            _receive_tasks_job,
            interval=rt.schedule_interval_minutes * 60,
            first=rt.schedule_interval_minutes * 60,
            data={"chat_id": query.message.chat_id, "schedule": sched},
            name=f"rt_{sched['schedule_id']}",
        )
        await query.message.reply_text(
            f"⏰ *Schedule saved:* runs every *{rt.schedule_interval_minutes} minute(s)*.\n"
            f"Use */schedules* to view active schedules.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
    else:
        await query.message.reply_text("Use the menu to start again.", reply_markup=_main_menu())

    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# /schedules — list saved schedules
# ──────────────────────────────────────────────────────────

async def cmd_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    schedules = [s for s in load_schedules() if s.get("active", True)]
    if not schedules:
        await update.message.reply_text("📭 No active schedules.", reply_markup=_main_menu())
        return
    lines = []
    for s in schedules:
        range_str = (
            f"KES {s['amount_min']:,.0f}–{s['amount_max']:,.0f}"
            if s.get("amount_min") is not None else "any amount"
        )
        lines.append(
            f"• *{md_escape(s['staff_name'])}* — every *{s['interval_minutes']}min*\n"
            f"  Tasks: {s['task_count']} | {s['task_type']} | {range_str}\n"
            f"  ID: `{s['schedule_id'][:8]}…`"
        )
    await update.message.reply_text(
        "⏰ *Active Schedules:*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


# ──────────────────────────────────────────────────────────
# /task_batches — view saved task batch history
# ──────────────────────────────────────────────────────────

async def cmd_task_batches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    batches = load_task_batches()
    if not batches:
        await update.message.reply_text("📭 No saved task batches yet.", reply_markup=_main_menu())
        return
    lines = []
    for b in batches[-10:]:   # most recent 10
        lines.append(
            f"• *{md_escape(b['staff_name'])}*  —  {b['created_at']}\n"
            f"  Assigned: {len(b['tasks'])}  |  Failed: {len(b.get('failed', []))}"
        )
    await update.message.reply_text(
        "📦 *Recent Task Batches (last 10):*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_main_menu(),
    )


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Receive Tasks conversation + /schedules + /task_batches into
    the given Application, and restore any active repeating schedules on
    startup."""
    _restore_schedules(app)

    rs_conv = ConversationHandler(
        entry_points=[
            CommandHandler("receive", cmd_receive),
        ],
        states={
            RS.PICK_STAFF_SOURCE: [CallbackQueryHandler(recv_rt_pick_source,   pattern=r"^rt_src:")],
            RS.STAFF_NAME:        [MessageHandler(not_cancel, recv_rt_staff_name)],
            RS.SELECT_STAFF:      [CallbackQueryHandler(recv_rt_select_staff,  pattern=r"^rt_staff:")],
            RS.CHOOSE_CRED:       [CallbackQueryHandler(recv_rt_cred_choice,   pattern=r"^cred:")],
            RS.WAIT_OTP:          [MessageHandler(not_cancel, recv_rt_otp)],
            RS.TASK_TYPE:         [CallbackQueryHandler(recv_rt_task_type,     pattern=r"^rt_type:")],
            RS.TASK_COUNT:        [MessageHandler(not_cancel, recv_rt_task_count)],
            RS.AMOUNT_RANGE:      [CallbackQueryHandler(recv_rt_amount_choice, pattern=r"^ft_amount:")],
            RS.AMOUNT_TEXT:       [MessageHandler(not_cancel, recv_rt_amount_range)],
            RS.SCHEDULE_CHOICE:   [CallbackQueryHandler(recv_rt_schedule_choice, pattern=r"^rt_sched:")],
            RS.SCHEDULE_INTERVAL: [MessageHandler(not_cancel, recv_rt_schedule_interval)],
            RS.RT_CONFIRM:        [CallbackQueryHandler(recv_rt_confirm,       pattern=r"^rt_confirm:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(rs_conv)

    app.add_handler(CommandHandler("schedules",    cmd_schedules))
    app.add_handler(CommandHandler("task_batches", cmd_task_batches))
