#!/usr/bin/env python3
"""
task_analytics.py
==================
Task Analytics — workflow-duration analytics for County Stamp Duty tasks
(/taskanalytics or "📈 Task Analytics"): how long a task spends at the
registrar stage, how long it's held by its current valuer, how many times
it's been reassigned, and its total age — split into "ongoing" (elapsed so
far) vs "completed" (actual duration) per the two application_status values
this report covers.

Neither of the two detail-view endpoints this module reads
(ASSESSOR_STAGE_DETAIL_URL for the registrar/HQ stage, STAMP_DUTY_
APPLICATION_DETAIL_URL for the DLV/valuation stage) exposes a true node-
transition history — only a current-state snapshot per stage plus, on the
valuation side, a remarks[] audit trail that happens to fire on some (not
guaranteed all) handoffs. So every duration here is built from a fixed
milestone model rather than a full node-by-node breakdown: registrar
officer's assigned_date, entry into the valuation stage (its own
date_created), valuer assignment/reassignment timestamps (remarks[],
cross-checked against actors[]'s date_assigned), and — since no endpoint
exposes an explicit completion timestamp — the chronologically-last
remarks[] entry as the best available "finalized at" proxy.

A registrar-stage record's own valuation_request_id field, once populated,
is the exact id of its linked valuation-stage record (confirmed against
real completed-task samples) — this module fetches that record directly by
id instead of doing a second independent by-ref search the way
lookup_reference.py's county routing does. A ref discovered only via the
valuation-stage list (its registrar record predates this run's date-range
cutoff) gets one unbounded single-ref registrar search
(lookup_reference._lu_search_ref_county/_lu_fetch_detail_county, reused
as-is) to recover its true creation time rather than silently understating
time_at_registrar.

Timestamps in these APIs are inconsistently timezone-labeled — fields like
assigned_date/date_assigned carry an explicit UTC marker, while date_created
-style fields are naive strings that read as local East Africa Time
(UTC+3). _ta_parse_ts is the one place this gets normalized; every duration
function reads through it rather than parsing a raw string itself.

This feature always needs both the registrar-stage (staff2) and DLV-stage
(staff_valuer) credentials cached — there's nothing to "pick," so
cmd_task_analytics does a silent pre-flight check and hard-blocks (naming
whichever credential is missing) rather than running a silently partial
report.

Call register(app) from bot.py's main() to wire this feature in.
"""

import asyncio
import io
import re
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
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
    BTN_TASK_ANALYTICS,
    CPARAMS_DLV,
    CRED_LABELS,
    _CANCEL_FILTER,
    _date_cutoff_str,
    _ft_county_keyboard,
    _ft_headers,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    logger,
)
from endpoints import (
    ASSESSOR_STAGE_DETAIL_URL,
    ASSESSOR_STAGE_LIST_URL,
    STAMP_DUTY_APPLICATION_DETAIL_URL,
    STAMP_DUTY_APPLICATION_LIST_URL,
)
from excel_report import autofit_columns, style_header_row
from lookup_reference import _lu_fetch_detail_county, _lu_search_ref_county
from task_block import format_labeled_block
from telegram_report import _send_chunked_report
from token_rotator import _AllTokensExhausted, _TokenRotator, fetch_with_rotation

# Credentials this feature always needs both of — there is no per-user
# choice, unlike every other credential-picker feature in this bot.
_TA_CRED_REGISTRAR = "staff2"
_TA_CRED_VALUATION = "staff_valuer"

_EAT = timezone(timedelta(hours=3))   # Kenya has no DST, so a fixed offset is safe


# ──────────────────────────────────────────────────────────
# States — Task Analytics conversation
# ──────────────────────────────────────────────────────────
class TA(Enum):
    COUNTY  = auto()   # pick Nairobi / All Counties (reused _ft_county_keyboard)
    PERIOD  = auto()   # pick date-range bucket
    CONFIRM = auto()   # show summary, confirm → run


@dataclass
class TASession:
    county:       str = ""   # "nairobi" or "" (all)
    period_days:  int = 0
    period_label: str = ""


def _get_ta_sess(ctx: ContextTypes.DEFAULT_TYPE) -> TASession:
    # Per-chat session, same ctx.user_data pattern every conversation module uses.
    if "ta_session" not in ctx.user_data:
        ctx.user_data["ta_session"] = TASession()
    return ctx.user_data["ta_session"]


# ──────────────────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────────────────

# "Today" stores days=0 — common._date_cutoff_str(0) already returns today's
# own YYYY-MM-DD, and common._within_days-style cutoff comparisons are
# inclusive (>=), so 0 is exactly "today" with no special-casing needed.
_TA_PERIOD_OPTIONS = [
    ("Today", 0), ("Past Week", 7), ("Past Month", 30),
    ("Past 3 Months", 90), ("Past 6 Months", 180), ("Past 1 Year", 365),
]


def _ta_period_keyboard() -> InlineKeyboardMarkup:
    # Six buckets, two per row (mirrors dlv_tasks._dt_valuer_keyboard's pairing loop), plus Cancel.
    rows = []
    for i in range(0, len(_TA_PERIOD_OPTIONS), 2):
        pair = _TA_PERIOD_OPTIONS[i:i + 2]
        rows.append([
            InlineKeyboardButton(label, callback_data=f"ta_period:{days}")
            for label, days in pair
        ])
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data="ta_period_cancel")])
    return InlineKeyboardMarkup(rows)


def _ta_confirm_keyboard() -> InlineKeyboardMarkup:
    # Run / Cancel, same shape as Bulk Export's be:yes/be:no confirm step.
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Run Report", callback_data="ta_confirm:yes"),
        InlineKeyboardButton("❌ Cancel",     callback_data="ta_confirm:no"),
    ]])


# ──────────────────────────────────────────────────────────
# List fetch — the genuinely new part (lookup_reference.py is single-ref
# search only; this needs a paginated bulk list filtered by county + date).
# ──────────────────────────────────────────────────────────

# Confirmed via codebase grep as the only filter values ever used against
# ASSESSOR_STAGE_LIST_URL anywhere in this repo (lookup_reference.py,
# fetch_tasks.py, dlv_core.py) — do not add an unconfirmed value here.
_TA_REGISTRAR_FILTERS = ["TO_VALUATION", "Ongoing"]

# The two application_status values this report covers (per the user's
# ongoing-vs-completed split) — "Returned" is deliberately excluded.
_TA_VALUATION_FILTERS = ["Ongoing", "Completed"]

_TA_DETAIL_WORKERS = 5


def _ta_fetch_registrar_page(http_sess, tokens: AuthTokens, filt: str, page: int) -> dict:
    # One page of the registrar/HQ-stage list for County Stamp Duty.
    headers = _ft_headers(tokens)
    resp = http_sess.get(
        ASSESSOR_STAGE_LIST_URL, headers=headers,
        params={"filter": filt, "from_ardhipay": "true", "page": page, "search": ""},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _ta_fetch_registrar_list(http_sess, tokens: AuthTokens, cutoff: str) -> List[Dict]:
    # Page through every _TA_REGISTRAR_FILTERS value, accumulating results
    # with date_created >= cutoff, stopping each filter's pagination early
    # once a page's oldest item falls below cutoff (results arrive
    # newest-first — same early-stop assumption fetch_tasks.py's
    # _fetch_county_list makes). Dedupes by reference_number across filters.
    seen: Dict[str, Dict] = {}
    for filt in _TA_REGISTRAR_FILTERS:
        page = 1
        stop = False
        while not stop:
            try:
                data = _ta_fetch_registrar_page(http_sess, tokens, filt, page)
            except Exception as e:
                logger.warning("Task Analytics registrar list page %d (%s) failed: %s", page, filt, e)
                break
            results = data.get("results", [])
            if not results:
                break
            for item in results:
                if (item.get("date_created") or "")[:10] < cutoff:
                    stop = True
                    break
                ref = item.get("reference_number")
                if ref:
                    seen[ref] = item
            if not data.get("next") or stop:
                break
            page += 1
    return list(seen.values())


def _ta_fetch_valuation_page(http_sess, tokens: AuthTokens, filt: str, page: int) -> dict:
    # One page of the DLV/valuation-stage list for County Stamp Duty.
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_DLV,
    }
    resp = http_sess.get(
        STAMP_DUTY_APPLICATION_LIST_URL, headers=headers,
        params={
            "filter": filt, "role": "DLV", "request_type": "COUNTY_STAMP_DUTY",
            "from_ardhipay": "true", "search": "", "page": page,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _ta_fetch_valuation_list(http_sess, tokens: AuthTokens, cutoff: str) -> List[Dict]:
    # Same cutoff/early-stop/dedup pattern as _ta_fetch_registrar_list, over _TA_VALUATION_FILTERS.
    seen: Dict[str, Dict] = {}
    for filt in _TA_VALUATION_FILTERS:
        page = 1
        stop = False
        while not stop:
            try:
                data = _ta_fetch_valuation_page(http_sess, tokens, filt, page)
            except Exception as e:
                logger.warning("Task Analytics valuation list page %d (%s) failed: %s", page, filt, e)
                break
            results = data.get("results", [])
            if not results:
                break
            for item in results:
                if (item.get("date_created") or "")[:10] < cutoff:
                    stop = True
                    break
                ref = item.get("reference_number")
                if ref:
                    seen[ref] = item
            if not data.get("next") or stop:
                break
            page += 1
    return list(seen.values())


def _ta_merge_populations(registrar_items: List[Dict], valuation_items: List[Dict],
                           county_filter: str) -> Dict[str, Dict]:
    # Merge both stage-lists by reference_number, then apply the county
    # filter ("nairobi" or "" for all, matching _ft_county_keyboard's two
    # options) using whichever item has a county value — preferring the
    # valuation-side one since it reflects the more current stage.
    merged: Dict[str, Dict] = {}
    for item in registrar_items:
        ref = item.get("reference_number")
        if ref:
            merged.setdefault(ref, {})["registrar_item"] = item
    for item in valuation_items:
        ref = item.get("reference_number")
        if ref:
            merged.setdefault(ref, {})["valuation_item"] = item

    if not county_filter:
        return merged

    filtered = {}
    for ref, entry in merged.items():
        county = ((entry.get("valuation_item") or {}).get("county")
                  or (entry.get("registrar_item") or {}).get("county") or "")
        if county.strip().lower() == county_filter:
            filtered[ref] = entry
    return filtered


# ──────────────────────────────────────────────────────────
# Detail fetch — one task per ref, up to 2 HTTP calls, under token rotation
# ──────────────────────────────────────────────────────────

def _ta_registrar_detail_headers(tokens: AuthTokens) -> dict:
    # Support-role headers for a registrar-stage detail-view call.
    return _ft_headers(tokens)


def _ta_valuation_detail_headers(tokens: AuthTokens) -> dict:
    # DLV-role headers for a valuation-stage detail-view call.
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_DLV,
    }


def _ta_fetch_registrar_detail(http_sess, rotator: _TokenRotator, app_id: str) -> Dict:
    # Registrar-stage detail-view under token rotation, unwrapping the
    # "details" wrapper key (same shape as lookup_reference._lu_fetch_detail_county).
    # Raises _AllTokensExhausted if the rotator runs dry; other errors propagate.
    data = fetch_with_rotation(
        http_sess, rotator, ASSESSOR_STAGE_DETAIL_URL, {"request_id": app_id},
        _ta_registrar_detail_headers, context=f"registrar detail {app_id}",
    )
    return data.get("details", {})


def _ta_fetch_valuation_detail(http_sess, rotator: _TokenRotator, app_id: str) -> Dict:
    # DLV/valuation-stage detail-view under token rotation. Raises
    # _AllTokensExhausted if the rotator runs dry; other errors propagate.
    return fetch_with_rotation(
        http_sess, rotator, STAMP_DUTY_APPLICATION_DETAIL_URL, {"request_id": app_id},
        _ta_valuation_detail_headers, context=f"valuation detail {app_id}",
    )


def _ta_process_ref(http_sess, registrar_rotator: _TokenRotator, valuation_rotator: _TokenRotator,
                     ref: str, registrar_item: Optional[Dict], valuation_item: Optional[Dict],
                     now: datetime) -> Dict:
    """One ref's full pipeline: detail fetch (both stages), the
    valuation_request_id shortcut (skip an independent DLV-side search
    whenever the registrar record already links to it), the registrar
    backfill search for a ref discovered only via the valuation-stage list,
    merge, and milestone computation. Returns a flat dict ready for both
    the Excel and Telegram builders. Raises _AllTokensExhausted if either
    rotator runs dry mid-fetch — the caller decides how to handle that."""
    registrar_detail: Optional[Dict] = None
    valuation_detail: Optional[Dict] = None
    valuation_id = None

    if registrar_item:
        registrar_detail = _ta_fetch_registrar_detail(http_sess, registrar_rotator, registrar_item["id"])
        if registrar_detail and registrar_detail.get("valuation_request_id"):
            valuation_id = registrar_detail["valuation_request_id"]

    if not valuation_id and valuation_item:
        valuation_id = valuation_item.get("id")

    if valuation_id:
        valuation_detail = _ta_fetch_valuation_detail(http_sess, valuation_rotator, valuation_id)

    if not registrar_item:
        # Only found via the valuation-stage list — its registrar record (if
        # any) predates this run's cutoff-bounded registrar search, so
        # recover it with one unbounded single-ref search rather than
        # silently understating time_at_registrar.
        backfill_tokens = registrar_rotator.current()
        if backfill_tokens:
            backfill_item = _lu_search_ref_county(backfill_tokens, ref)
            if backfill_item:
                registrar_detail = _lu_fetch_detail_county(backfill_tokens, backfill_item["id"])

    merged = _ta_build_merged_record(ref, registrar_detail, valuation_detail)
    milestones = _ta_compute_milestones(merged, now)
    county = (merged.get("county") or (registrar_item or {}).get("county")
              or (valuation_item or {}).get("county") or "")
    registry = (merged.get("registry") or (registrar_item or {}).get("registry")
                or (valuation_item or {}).get("registry") or "")
    return {
        "ref": ref, "merged": merged, "milestones": milestones,
        "county": county, "registry": registry,
        "application_status": merged.get("application_status") or "",
    }


# ──────────────────────────────────────────────────────────
# Timestamp normalization — the one place this logic lives
# ──────────────────────────────────────────────────────────

def _ta_parse_ts(raw) -> Optional[datetime]:
    """Parse any timestamp this API returns into a naive local (East Africa
    Time) datetime, or None if unparseable/empty/the literal string "None"
    (actors[].date_assigned is sometimes this literal string, not JSON
    null). Explicit-UTC-marked strings ("Z" suffix or a +HH:MM/-HH:MM
    offset — assigned_date-style fields) are parsed as aware then converted
    to UTC+3 and stripped of tzinfo; naive strings (date_created-style)
    are parsed as-is, since they already read as local wall-clock time.
    Any parse failure returns None (fail-open — callers decide how to
    treat a None milestone)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == "None":
        return None

    has_utc_marker = text.endswith("Z") or bool(re.search(r"[+-]\d{2}:\d{2}$", text))
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text

    try:
        if has_utc_marker:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                return None
            return dt.astimezone(_EAT).replace(tzinfo=None)
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass

    # Fallback for shapes fromisoformat's stricter (pre-3.11) parser rejects.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


# ──────────────────────────────────────────────────────────
# Merged record + milestone functions — pure, directly unit-testable
# ──────────────────────────────────────────────────────────

def _ta_build_merged_record(ref: str, registrar_detail: Optional[Dict],
                             valuation_detail: Optional[Dict]) -> Dict:
    """Normalize a registrar-stage detail dict and/or a valuation-stage
    detail dict for one ref into one predictable-shape record every
    milestone function reads from. Either input may be None/empty — a ref
    can be purely at one stage. Prefers valuation-stage values for county/
    registry/application_status when both are present, since that's the
    more current stage."""
    registrar_detail = registrar_detail or {}
    valuation_detail = valuation_detail or {}
    officers = registrar_detail.get("officers") or []
    return {
        "ref": ref,
        "officers": officers,
        "remarks": valuation_detail.get("remarks") or [],
        "actors": valuation_detail.get("actors") or [],
        "application_status": (valuation_detail.get("application_status")
                                or registrar_detail.get("application_status") or ""),
        "valuation_request_id": registrar_detail.get("valuation_request_id"),
        "date_created_registrar": officers[0].get("assigned_date") if officers else None,
        "date_created_valuation": valuation_detail.get("date_created"),
        "county": valuation_detail.get("county") or registrar_detail.get("county") or "",
        "registry": valuation_detail.get("registry") or registrar_detail.get("registry") or "",
        "node": valuation_detail.get("node") or registrar_detail.get("node") or "",
        "has_registrar_record": bool(registrar_detail),
        "has_valuation_record": bool(valuation_detail),
    }


def _ta_created_at(merged: Dict) -> Optional[datetime]:
    # officers[0].assigned_date if a registrar record exists, else the valuation stage's own date_created.
    ts = _ta_parse_ts(merged.get("date_created_registrar"))
    return ts if ts else _ta_parse_ts(merged.get("date_created_valuation"))


def _ta_entered_valuation_at(merged: Dict) -> Optional[datetime]:
    # The valuation stage's own date_created, or None if this ref has no valuation-stage record.
    return _ta_parse_ts(merged.get("date_created_valuation"))


def _ta_time_at_registrar(merged: Dict, now: datetime) -> Tuple[Optional[timedelta], bool]:
    """(duration, is_ongoing). valuation_request_id populated → entered_
    valuation_at - created_at (ongoing=False). Null → now - created_at
    (ongoing=True, still at registrar). (None, False) if created_at is
    unknown, or if the handoff already happened but its timestamp can't be
    resolved (don't guess a misleadingly "ongoing" number for a stage that's
    already over)."""
    created = _ta_created_at(merged)
    if created is None:
        return (None, False)
    if merged.get("valuation_request_id"):
        entered = _ta_entered_valuation_at(merged)
        return (entered - created, False) if entered else (None, False)
    return (now - created, True)


def _ta_remarks_by_status(merged: Dict, status: str) -> List[datetime]:
    # Parseable date_created values in remarks[] matching status, sorted ascending.
    out = []
    for r in merged.get("remarks") or []:
        if r.get("status") != status:
            continue
        ts = _ta_parse_ts(r.get("date_created"))
        if ts:
            out.append(ts)
    out.sort()
    return out


def _ta_first_valuer_assigned_at(merged: Dict) -> Optional[datetime]:
    # Earliest DLV_FORWARDING_REMARKS timestamp; falls back to the earliest
    # actors[] VALUATION OFFICER date_assigned if remarks yield nothing.
    forwarded = _ta_remarks_by_status(merged, "DLV_FORWARDING_REMARKS")
    if forwarded:
        return forwarded[0]
    for a in merged.get("actors") or []:
        if (a.get("role") or "").upper() == "VALUATION OFFICER":
            ts = _ta_parse_ts(a.get("date_assigned"))
            if ts:
                return ts
    return None


def _ta_current_valuer_assigned_at(merged: Dict) -> Optional[datetime]:
    # Latest of {DLV_FORWARDING_REMARKS, DLV_REASSIGN_REMARKS} from
    # remarks[]; actors[]'s VALUATION OFFICER date_assigned wins when it
    # resolves (authoritative — remarks are user-entered), else falls back
    # to the remarks-derived value alone.
    remark_times = (_ta_remarks_by_status(merged, "DLV_FORWARDING_REMARKS")
                     + _ta_remarks_by_status(merged, "DLV_REASSIGN_REMARKS"))
    remark_latest = max(remark_times) if remark_times else None

    actor_latest = None
    for a in merged.get("actors") or []:
        if (a.get("role") or "").upper() == "VALUATION OFFICER":
            ts = _ta_parse_ts(a.get("date_assigned"))
            if ts:
                actor_latest = ts
    return actor_latest if actor_latest else remark_latest


def _ta_handoff_count(merged: Dict) -> int:
    # Count of DLV_REASSIGN_REMARKS entries — each is one reassignment to a different valuer.
    return len(_ta_remarks_by_status(merged, "DLV_REASSIGN_REMARKS"))


def _ta_finalized_at(merged: Dict) -> Optional[datetime]:
    # Completed-only: the chronologically-last remarks[] entry (any status)
    # — the best available proxy since no endpoint exposes a real completion timestamp.
    if merged.get("application_status") != "COMPLETED":
        return None
    times = [_ta_parse_ts(r.get("date_created")) for r in (merged.get("remarks") or [])]
    times = [t for t in times if t]
    return max(times) if times else None


def _ta_total_age(merged: Dict, now: datetime) -> Tuple[Optional[timedelta], bool]:
    # (duration, is_ongoing). finalized_at - created_at if completed and both resolve; else now - created_at.
    created = _ta_created_at(merged)
    if created is None:
        return (None, False)
    finalized = _ta_finalized_at(merged)
    return (finalized - created, False) if finalized else (now - created, True)


def _ta_time_with_current_valuer(merged: Dict, now: datetime) -> Tuple[Optional[timedelta], bool]:
    # (duration, is_ongoing). finalized_at - current_valuer_assigned_at if
    # completed; else now - current_valuer_assigned_at. None if the ref
    # never reached the DLV stage / had no valuer assigned.
    anchor = _ta_current_valuer_assigned_at(merged)
    if anchor is None:
        return (None, False)
    finalized = _ta_finalized_at(merged)
    return (finalized - anchor, False) if finalized else (now - anchor, True)


def _ta_time_at_valuer(merged: Dict, now: datetime) -> Tuple[Optional[timedelta], bool]:
    # (duration, is_ongoing) — total time spent in the DLV/valuation stage
    # overall (entered_valuation_at → finalized_at/now), distinct from
    # time_with_current_valuer which resets on every reassignment.
    entered = _ta_entered_valuation_at(merged)
    if entered is None:
        return (None, False)
    finalized = _ta_finalized_at(merged)
    return (finalized - entered, False) if finalized else (now - entered, True)


def _ta_current_valuer_name(merged: Dict) -> str:
    # Current VALUATION OFFICER's display name, "" if none assigned.
    for a in merged.get("actors") or []:
        if (a.get("role") or "").upper() == "VALUATION OFFICER":
            return (a.get("user_details") or {}).get("names") or ""
    return ""


def _ta_current_valuer_id(merged: Dict) -> str:
    # Current VALUATION OFFICER's stable id, "" if none assigned — used for
    # grouping in the Summary sheet in preference to name (uid is stable
    # across a name-casing difference the way _dt_valuer_key already handles elsewhere).
    for a in merged.get("actors") or []:
        if (a.get("role") or "").upper() == "VALUATION OFFICER":
            return str((a.get("user_details") or {}).get("id") or "")
    return ""


def _ta_current_stage_label(merged: Dict) -> str:
    # Human label for where a ref currently sits: Registrar / DLV — <valuer> / Completed.
    if merged.get("application_status") == "COMPLETED":
        return "Completed"
    if merged.get("has_valuation_record"):
        name = _ta_current_valuer_name(merged)
        return f"DLV — {name}" if name else "DLV — unassigned"
    return "Registrar"


def _ta_compute_milestones(merged: Dict, now: datetime) -> Dict:
    # All derived duration/stage fields for one ref's merged record, computed once.
    time_at_registrar, registrar_ongoing   = _ta_time_at_registrar(merged, now)
    time_at_valuer, valuer_stage_ongoing   = _ta_time_at_valuer(merged, now)
    time_with_valuer, current_valuer_going = _ta_time_with_current_valuer(merged, now)
    total_age, age_ongoing                 = _ta_total_age(merged, now)
    return {
        "created_at":                 _ta_created_at(merged),
        "entered_valuation_at":       _ta_entered_valuation_at(merged),
        "time_at_registrar":          time_at_registrar,
        "registrar_ongoing":          registrar_ongoing,
        "first_valuer_assigned_at":   _ta_first_valuer_assigned_at(merged),
        "current_valuer_assigned_at": _ta_current_valuer_assigned_at(merged),
        "time_at_valuer":             time_at_valuer,
        "valuer_stage_ongoing":       valuer_stage_ongoing,
        "time_with_current_valuer":   time_with_valuer,
        "current_valuer_ongoing":     current_valuer_going,
        "handoff_count":              _ta_handoff_count(merged),
        "finalized_at":               _ta_finalized_at(merged),
        "total_age":                  total_age,
        "age_ongoing":                age_ongoing,
        "current_valuer_name":        _ta_current_valuer_name(merged),
        "current_valuer_id":          _ta_current_valuer_id(merged),
        "stage_label":                _ta_current_stage_label(merged),
    }


# ──────────────────────────────────────────────────────────
# Output — Excel
# ──────────────────────────────────────────────────────────

_TA_EXCEL_COLUMNS = [
    "Reference Number", "County", "Registry", "Application Status", "Current Stage",
    "Created At", "Entered Valuation At", "Time at Registrar (days)", "Registrar Status",
    "First Valuer Assigned At", "Current Valuer", "Current Valuer Assigned At",
    "Time with Current Valuer (days)", "Handoff Count", "Finalized At",
    "Total Age (days)", "Age Status", "Valuation Request ID",
]


def _ta_dt_str(dt: Optional[datetime]) -> str:
    # ISO-ish string for an Excel timestamp cell, "" if None.
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _ta_duration_days(duration: Optional[timedelta]) -> Optional[float]:
    # Raw float day count for a duration, None if the duration is None.
    return duration.total_seconds() / 86400 if duration is not None else None


def _ta_days_value(duration: Optional[timedelta]):
    # Numeric day count (2dp) for an Excel duration cell, "" if None.
    days = _ta_duration_days(duration)
    return round(days, 2) if days is not None else ""


def _ta_avg(values: List[float]):
    # Rounded average of values, "—" if empty.
    return round(sum(values) / len(values), 2) if values else "—"


def _ta_median(values: List[float]):
    # Rounded median of values, "—" if empty.
    return round(statistics.median(values), 2) if values else "—"


def _ta_build_detail_row(row: Dict) -> List:
    # One Task Detail sheet row (in _TA_EXCEL_COLUMNS order) for a processed ref.
    m = row["milestones"]
    merged = row["merged"]
    registrar_status = ("Ongoing" if m["registrar_ongoing"]
                         else ("Completed" if m["time_at_registrar"] is not None else "—"))
    return [
        row["ref"], row["county"], row["registry"], row["application_status"], m["stage_label"],
        _ta_dt_str(m["created_at"]), _ta_dt_str(m["entered_valuation_at"]),
        _ta_days_value(m["time_at_registrar"]), registrar_status,
        _ta_dt_str(m["first_valuer_assigned_at"]), m["current_valuer_name"],
        _ta_dt_str(m["current_valuer_assigned_at"]),
        _ta_days_value(m["time_with_current_valuer"]), m["handoff_count"],
        _ta_dt_str(m["finalized_at"]), _ta_days_value(m["total_age"]),
        "Ongoing" if m["age_ongoing"] else "Completed",
        merged.get("valuation_request_id") or "",
    ]


def _ta_write_summary(ws2, rows: List[Dict]) -> None:
    """Overall Totals / By County / By Officer sections on the Summary
    sheet, mirroring bulk_export._be_build_excel's section-header pattern
    (bold-filled title row, small column-header row, then data rows)."""
    header_font = Font(bold=True)

    def _section_header(row_idx, text, span=8):
        cell = ws2.cell(row=row_idx, column=1, value=text)
        cell.font = Font(bold=True, size=12)
        cell.fill = PatternFill("solid", fgColor="BDD7EE")
        ws2.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=span)

    def _col_headers(row_idx, *headers):
        for col, h in enumerate(headers, start=1):
            c = ws2.cell(row=row_idx, column=col, value=h)
            c.font = header_font
            c.fill = PatternFill("solid", fgColor="D9E1F2")

    completed = [r for r in rows if r["application_status"] == "COMPLETED"]
    ongoing   = [r for r in rows if r["application_status"] != "COMPLETED"]

    def _ages(subset):
        return [d for d in (_ta_duration_days(r["milestones"]["total_age"]) for r in subset) if d is not None]

    # ── Overall Totals ──
    _section_header(1, "Overall Totals")
    for i, (label, value) in enumerate((
        ("Total Refs", len(rows)),
        ("Ongoing Count", len(ongoing)),
        ("Completed Count", len(completed)),
        ("Avg Total Age (days, all)", _ta_avg(_ages(rows))),
        ("Avg Total Age (days, ongoing)", _ta_avg(_ages(ongoing))),
        ("Avg Total Age (days, completed)", _ta_avg(_ages(completed))),
    ), start=2):
        ws2.cell(row=i, column=1, value=label).font = Font(bold=True)
        ws2.cell(row=i, column=2, value=value)

    # ── By County ──
    row = ws2.max_row + 2
    _section_header(row, "By County")
    row += 1
    _col_headers(row, "County", "Status", "Ref Count", "Avg Registrar (d)",
                 "Median Registrar (d)", "Avg Valuer (d)", "Median Valuer (d)", "Avg Total Age (d)")
    row += 1
    counties = sorted({(r["county"] or "Unknown") for r in rows})
    for county in counties:
        for status_label, subset in (("Ongoing", ongoing), ("Completed", completed)):
            group = [r for r in subset if (r["county"] or "Unknown") == county]
            if not group:
                continue
            reg_vals = [d for d in (_ta_duration_days(r["milestones"]["time_at_registrar"]) for r in group) if d is not None]
            val_vals = [d for d in (_ta_duration_days(r["milestones"]["time_at_valuer"]) for r in group) if d is not None]
            for col, value in enumerate((
                county, status_label, len(group), _ta_avg(reg_vals), _ta_median(reg_vals),
                _ta_avg(val_vals), _ta_median(val_vals), _ta_avg(_ages(group)),
            ), start=1):
                ws2.cell(row=row, column=col, value=value)
            row += 1

    # ── By Officer / Valuer ──
    row += 1
    _section_header(row, "By Officer / Valuer", span=6)
    row += 1
    _col_headers(row, "Valuer", "Status", "Ref Count",
                 "Avg With-Valuer (d)", "Median With-Valuer (d)", "Avg Handoffs")
    row += 1
    valuer_keys = sorted({
        r["milestones"]["current_valuer_id"] or r["milestones"]["current_valuer_name"]
        for r in rows if r["milestones"]["current_valuer_name"]
    })
    for key in valuer_keys:
        for status_label, subset in (("Ongoing", ongoing), ("Completed", completed)):
            group = [
                r for r in subset
                if r["milestones"]["current_valuer_name"]
                and (r["milestones"]["current_valuer_id"] or r["milestones"]["current_valuer_name"]) == key
            ]
            if not group:
                continue
            name = group[0]["milestones"]["current_valuer_name"]
            wv_vals = [d for d in (_ta_duration_days(r["milestones"]["time_with_current_valuer"]) for r in group) if d is not None]
            handoffs = [r["milestones"]["handoff_count"] for r in group]
            for col, value in enumerate(
                (name, status_label, len(group), _ta_avg(wv_vals), _ta_median(wv_vals), _ta_avg(handoffs)), start=1
            ):
                ws2.cell(row=row, column=col, value=value)
            row += 1

    for col_idx in range(1, 9):
        col_letter = get_column_letter(col_idx)
        max_len = 18
        for r in ws2.iter_rows(min_col=col_idx, max_col=col_idx):
            val = str(r[0].value if r[0].value is not None else "")
            if len(val) > max_len:
                max_len = len(val)
        ws2.column_dimensions[col_letter].width = max(18, min(45, max_len + 2))


def _ta_build_excel(rows: List[Dict]) -> bytes:
    """Build the Task Analytics workbook: a per-ref "Task Detail" sheet and
    an aggregate "Summary" sheet (overall totals, by-county, by-officer)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Task Detail"

    date_cols   = {"Created At", "Entered Valuation At", "First Valuer Assigned At",
                   "Current Valuer Assigned At", "Finalized At"}
    number_cols = {"Time at Registrar (days)", "Time with Current Valuer (days)", "Total Age (days)"}

    style_header_row(ws, _TA_EXCEL_COLUMNS)
    for row in rows:
        ws.append(_ta_build_detail_row(row))
        row_idx = ws.max_row
        for col_idx, col_name in enumerate(_TA_EXCEL_COLUMNS, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            if col_name in date_cols and cell.value:
                cell.number_format = "YYYY-MM-DD HH:MM:SS"
            elif col_name in number_cols and cell.value != "":
                cell.number_format = "#,##0.00"
    autofit_columns(ws, min_width=15, max_width=45)

    ws2 = wb.create_sheet("Summary")
    _ta_write_summary(ws2, rows)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ──────────────────────────────────────────────────────────
# Output — Telegram
# ──────────────────────────────────────────────────────────

def _ta_fmt_duration(duration: Optional[timedelta], ongoing: bool) -> str:
    # "N.N days (ongoing|completed)", or "—" if duration is None.
    if duration is None:
        return "—"
    days = duration.total_seconds() / 86400
    return f"{days:,.1f} days ({'ongoing' if ongoing else 'completed'})"


def _ta_stage_field(milestones: Dict) -> Tuple[str, str]:
    # (label, value) — which stage a ref currently sits at.
    return ("🔄 Stage", milestones["stage_label"])


def _ta_age_field(milestones: Dict) -> Tuple[str, str]:
    # (label, value) — total elapsed/actual age of the ref.
    return ("⏱ Total Age", _ta_fmt_duration(milestones["total_age"], milestones["age_ongoing"]))


def _ta_registrar_time_field(milestones: Dict) -> Tuple[str, str]:
    # (label, value) — time spent at the registrar stage, "—" if this ref never had a registrar record.
    return ("📋 Time at Registrar",
            _ta_fmt_duration(milestones["time_at_registrar"], milestones["registrar_ongoing"]))


def _ta_valuer_time_field(milestones: Dict) -> Tuple[str, str]:
    # (label, value) — time held by the current valuer, "—" if the ref never reached the DLV stage.
    return ("👤 Time with Current Valuer",
            _ta_fmt_duration(milestones["time_with_current_valuer"], milestones["current_valuer_ongoing"]))


def _ta_handoff_field(milestones: Dict) -> Optional[Tuple[str, str]]:
    # (label, value) — reassignment count, included only when > 0.
    count = milestones["handoff_count"]
    return ("🔁 Reassignments", str(count)) if count else None


def _ta_fields_for(milestones: Dict) -> List[Tuple[str, str]]:
    # The Telegram block's field list for one ref.
    fields = [
        _ta_stage_field(milestones), _ta_age_field(milestones),
        _ta_registrar_time_field(milestones), _ta_valuer_time_field(milestones),
    ]
    handoff = _ta_handoff_field(milestones)
    if handoff:
        fields.append(handoff)
    return fields


async def _ta_send_report_async(rows: List[Dict], county_label: str, period_label: str, bot, chat_id: int) -> None:
    # Build and send the chunked Telegram summary for a completed run.
    ongoing_count   = sum(1 for r in rows if r["application_status"] != "COMPLETED")
    completed_count = len(rows) - ongoing_count
    header = (
        f"📊 *Task Analytics — County Stamp Duty*\n\n"
        f"*County:* {county_label}\n*Period:* {period_label}\n*Total Refs:* {len(rows)}\n"
    )
    lines = [header] + [
        format_labeled_block(i, r["ref"], _ta_fields_for(r["milestones"]))
        for i, r in enumerate(rows, 1)
    ]
    footer = (
        f"\n\n*Ongoing:* {ongoing_count} · *Completed:* {completed_count}\n"
        "📎 Full detail + aggregate breakdown in the attached Excel file."
    )

    async def _send(text, reply_markup):
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

    await _send_chunked_report(_send, lines, join="\n\n", footer=footer)


def _ta_send_telegram_summary(rows: List[Dict], county_label: str, period_label: str,
                               bot, loop, chat_id: int) -> None:
    # Thread-safe wrapper — schedules the async sender onto the bot's event
    # loop from the background worker thread and blocks until it's sent.
    asyncio.run_coroutine_threadsafe(
        _ta_send_report_async(rows, county_label, period_label, bot, chat_id), loop,
    ).result(timeout=60)


# ──────────────────────────────────────────────────────────
# Background run
# ──────────────────────────────────────────────────────────

def _ta_run(registrar_tokens: AuthTokens, valuation_tokens: AuthTokens, chat_id: int,
            county_filter: str, county_label: str, period_days: int, period_label: str,
            bot, loop) -> None:
    """Full synchronous Task Analytics worker — runs in a background thread
    (mirrors bulk_export._bulk_export_run's shape). Fetches both stage
    lists in parallel, merges + county-filters, fetches per-ref detail
    under token rotation, computes milestones, then sends a chunked
    Telegram summary followed by the Excel export."""
    def _tg(text: str):
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id, text, parse_mode="Markdown"), loop,
        ).result(timeout=15)

    def _to_menu():
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id, "Main menu.", reply_markup=_main_menu()), loop,
        ).result(timeout=15)

    http_sess = build_session()
    cutoff = _date_cutoff_str(period_days)

    try:
        # Two list fetches, independent services, run in parallel — same
        # shape as fetch_tasks._load_fetch_tasks's hq_future/county_future pair.
        with ThreadPoolExecutor(max_workers=2) as pool:
            reg_future = pool.submit(_ta_fetch_registrar_list, http_sess, registrar_tokens, cutoff)
            val_future = pool.submit(_ta_fetch_valuation_list, http_sess, valuation_tokens, cutoff)
            registrar_items = reg_future.result()
            valuation_items = val_future.result()

        population = _ta_merge_populations(registrar_items, valuation_items, county_filter)

        if not population:
            _tg(f"ℹ️ Task Analytics — no County Stamp Duty refs found for *{county_label}*, *{period_label}*.")
            _to_menu()
            return

        _tg(f"⏳ Found {len(population)} ref(s) — fetching detail…")

        registrar_rotator = _TokenRotator([(_TA_CRED_REGISTRAR, registrar_tokens)])
        valuation_rotator = _TokenRotator([(_TA_CRED_VALUATION, valuation_tokens)])

        now = datetime.now()
        rows: List[Dict] = []
        exhausted = False
        with ThreadPoolExecutor(max_workers=_TA_DETAIL_WORKERS) as pool:
            futures = {
                pool.submit(
                    _ta_process_ref, http_sess, registrar_rotator, valuation_rotator,
                    ref, entry.get("registrar_item"), entry.get("valuation_item"), now,
                ): ref
                for ref, entry in population.items()
            }
            for fut in _futures_as_completed(futures):
                ref = futures[fut]
                try:
                    rows.append(fut.result())
                except _AllTokensExhausted:
                    exhausted = True
                    logger.warning("Task Analytics: tokens exhausted at ref=%s", ref)
                except Exception as exc:
                    logger.warning("Task Analytics: detail fetch failed for %s: %s", ref, exc)

        if exhausted:
            _tg(
                "⚠️ Some refs could not be fetched — all tokens returned 403 partway through. "
                f"Showing results for {len(rows)} of {len(population)} ref(s). "
                "Refresh your tokens and run again for a complete report."
            )

        if not rows:
            _tg("❌ Task Analytics failed — no refs could be fetched.")
            _to_menu()
            return

        rows.sort(key=lambda r: r["ref"])
        _ta_send_telegram_summary(rows, county_label, period_label, bot, loop, chat_id)

        xlsx_bytes  = _ta_build_excel(rows)
        county_slug = county_filter or "all"
        period_slug = re.sub(r"[^a-z0-9]+", "_", period_label.lower()).strip("_")
        filename    = f"Task_Analytics_{county_slug}_{period_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        asyncio.run_coroutine_threadsafe(
            bot.send_document(
                chat_id, document=io.BytesIO(xlsx_bytes), filename=filename,
                caption=f"📊 Task Analytics — {len(rows)} ref(s)",
            ), loop,
        ).result(timeout=60)

        _to_menu()

    except Exception as exc:
        logger.error("Task Analytics worker crashed: %s", exc, exc_info=True)
        _tg(f"❌ Task Analytics failed: `{exc}`")


# ──────────────────────────────────────────────────────────
# Conversation handlers
# ──────────────────────────────────────────────────────────

async def cmd_task_analytics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point (/taskanalytics or the menu button) — hard-blocks with a
    named-credential error if either required credential lacks a cached
    token (this feature always needs both, never a partial run), otherwise
    starts the county-filter step."""
    if not allowed(update): return await deny(update)
    missing = [
        CRED_LABELS.get(c, c) for c in (_TA_CRED_REGISTRAR, _TA_CRED_VALUATION)
        if not get_valid_tokens(c)
    ]
    if missing:
        await update.message.reply_text(
            f"❌ No valid cached tokens for *{', '.join(missing)}*. "
            "Task Analytics needs both the Support Reg and Staff Valuer credentials — "
            "use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END
    ctx.user_data["ta_session"] = TASession()
    await update.message.reply_text(
        "📊 *Task Analytics — County Stamp Duty*\n\n"
        "Reports how long tasks spend at each workflow stage. Filter by county:",
        parse_mode="Markdown",
        reply_markup=_ft_county_keyboard(),
    )
    return TA.COUNTY


async def recv_ta_county(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # County step — reuses common._ft_county_keyboard's ft_county: callback.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]
    sess = _get_ta_sess(ctx)
    sess.county = "" if choice == "all" else choice
    label = "📋 All Counties" if choice == "all" else "🌆 Nairobi"
    await query.edit_message_text(
        f"✅ County: *{label}*\n\nSelect the date range:",
        parse_mode="Markdown",
        reply_markup=_ta_period_keyboard(),
    )
    return TA.PERIOD


async def recv_ta_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Date-range step — six buckets, "Today" = days 0 (see module docstring).
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "ta_period_cancel":
        await query.edit_message_text("❌ Cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    days = int(query.data.split(":")[1])
    sess = _get_ta_sess(ctx)
    sess.period_days  = days
    sess.period_label = next((label for label, d in _TA_PERIOD_OPTIONS if d == days), "Today")

    county_label = "📋 All Counties" if not sess.county else "🌆 Nairobi"
    await query.edit_message_text(
        f"✅ Ready to run.\n\n"
        f"• County: *{county_label}*\n"
        f"• Period: *{sess.period_label}*\n\n"
        "Tap *Run Report* to start.",
        parse_mode="Markdown",
        reply_markup=_ta_confirm_keyboard(),
    )
    return TA.CONFIRM


async def recv_ta_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Confirm step — re-checks both credentials (they could have expired
    # mid-flow) then kicks off the background run off the event loop.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    if query.data == "ta_confirm:no":
        await query.edit_message_text("❌ Cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    registrar_tokens = get_valid_tokens(_TA_CRED_REGISTRAR)
    valuation_tokens = get_valid_tokens(_TA_CRED_VALUATION)
    missing = [
        CRED_LABELS.get(c, c) for c, t in (
            (_TA_CRED_REGISTRAR, registrar_tokens), (_TA_CRED_VALUATION, valuation_tokens),
        ) if not t
    ]
    if missing:
        await query.edit_message_text(
            f"❌ Tokens for *{', '.join(missing)}* expired mid-flow. Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
        )
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess = _get_ta_sess(ctx)
    chat_id = query.message.chat_id
    county_label = "📋 All Counties" if not sess.county else "🌆 Nairobi"

    await query.edit_message_text("⏳ Running — you'll be notified when it's done.")
    loop = asyncio.get_event_loop()
    asyncio.ensure_future(
        asyncio.to_thread(
            _ta_run, registrar_tokens, valuation_tokens, chat_id,
            sess.county, county_label, sess.period_days, sess.period_label,
            ctx.bot, loop,
        )
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Task Analytics conversation into the given Application."""
    ta_conv = ConversationHandler(
        entry_points=[
            CommandHandler("taskanalytics", cmd_task_analytics),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_TASK_ANALYTICS)}$"), cmd_task_analytics),
        ],
        states={
            TA.COUNTY:  [CallbackQueryHandler(recv_ta_county,  pattern=r"^ft_county:")],
            TA.PERIOD:  [CallbackQueryHandler(recv_ta_period,  pattern=r"^ta_period")],
            TA.CONFIRM: [CallbackQueryHandler(recv_ta_confirm, pattern=r"^ta_confirm:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(ta_conv)
