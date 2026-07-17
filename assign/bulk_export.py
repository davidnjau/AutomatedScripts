#!/usr/bin/env python3
"""
bulk_export.py
===============
Bulk Export — export a full stamp-duty valuation report (Ardhisasa or
Ardhipay, filtered by county/registry, "Completed" applications only) to
Excel, optionally emailed and/or run on a repeating schedule (/bulkexport
or "📤 Export Valuation Report").

Fetches list pages, then detail + office-report for each record in
parallel with multi-credential token rotation on 403 (via token_rotator.py).
Resumes from a saved partial checkpoint if tokens get exhausted mid-run.

cmd_export_status ("📊 Export Status") is a thin combiner: it also reads
Job Distribution's _JD_STATUS (imported from job_distribution.py) since
that feature's background job status has nowhere else natural to render
from — Bulk Export and Job Distribution are the two "long background job"
features in this bot.

Call register(app) from bot.py's main() to wire this feature in (this also
restores a saved repeating schedule on startup).
"""

import asyncio
import io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional

import openpyxl
import requests
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
    BTN_BULK_EXPORT,
    BTN_EXPORT_STATUS,
    CPARAMS_DLV,
    CRED_LABELS,
    CRED_MAP,
    DATA_DIR,
    _any_valid_tokens,
    _atomic_json_write,
    _CANCEL_FILTER,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    logger,
    md_escape,
    not_cancel,
)
from email_service import _send_bulk_export_email
from endpoints import (
    STAMP_DUTY_APPLICATION_DETAIL_URL,
    STAMP_DUTY_APPLICATION_LIST_URL,
    STAMP_DUTY_OFFICE_REPORT_URL,
)
from excel_report import autofit_columns, style_header_row
from job_distribution import _JD_STATUS
from token_rotator import _AllTokensExhausted, _TokenRotator

SAVED_BULK_EXPORT_SCHED_FILE   = os.path.join(DATA_DIR, "saved_bulk_export_schedule.json")
SAVED_BULK_EXPORT_PARTIAL_FILE = os.path.join(DATA_DIR, "saved_bulk_export_partial.json")


# ──────────────────────────────────────────────────────────
# States — Bulk Export conversation
# ──────────────────────────────────────────────────────────
class BE(Enum):
    REPORT_TYPE = auto()  # pick Ardhisasa or Ardhipay report
    COUNTY    = auto()   # pick county
    EMAIL     = auto()   # ask for recipient email address
    SCHEDULE  = auto()   # pick repeat interval
    CONFIRM   = auto()   # confirm → kick off background export


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


_BE_LIST_URL   = STAMP_DUTY_APPLICATION_LIST_URL
_BE_DETAIL_URL = STAMP_DUTY_APPLICATION_DETAIL_URL
_BE_REPORT_URL = STAMP_DUTY_OFFICE_REPORT_URL
_BE_PAGE_SIZE  = 10   # API default
_BE_LIST_WORKERS        = 5
_BE_DETAIL_WORKERS      = 5
_BE_MAX_RETRIES         = 3
_BE_TOKEN_ROTATE_DELAY  = 10   # seconds to wait before retrying with a new token
_MAX_FETCH_RETRIES      = 5    # max 429/5xx retries before aborting a fetch loop


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
#  Lookup Reference, and Valuer Tasks — Bulk Export uses its own
#  _be_report_type_keyboard/_be_county_keyboard instead)


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


def _be_land_value(r: dict) -> float:
    """Numeric land value for sorting highest-to-lowest; missing/unparseable
    sorts last (below every real amount, which is >= 0)."""
    raw = r.get("Valuer Total Land Value (KES)", "")
    try:
        return float(str(raw).replace(",", "").strip()) if raw else -1.0
    except (ValueError, TypeError):
        return -1.0


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

    style_header_row(ws, _EXCEL_COLUMNS, font=header_font, fill=header_fill)

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

    autofit_columns(ws, min_width=15, max_width=50)

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

        # ── Step 3: sort final rows by land value (highest first), build Excel, send ────
        _set_status(phase="building excel")
        rows.sort(key=_be_land_value, reverse=True)

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
                _tg(f"📧 File also sent to *{md_escape(email)}*.")
            except Exception as exc:
                logger.warning("Bulk export email failed: %s", exc)
                _tg(f"⚠️ Email delivery failed: `{exc}`")

    except Exception as exc:
        logger.error("Bulk export worker crashed: %s", exc, exc_info=True)
        _set_status(phase="failed", completed_at=datetime.now(), error_msg=str(exc))
        _tg(f"❌ Export failed: `{exc}`")


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
        f"• Destination: *{md_escape(email_label)}*\n\n"
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
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Bulk Export conversation + /export_status into the given
    Application, and restore a saved repeating schedule on startup."""
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
    app.add_handler(be_conv)
    app.add_handler(MessageHandler(
        filters.Regex(f"^{re.escape(BTN_EXPORT_STATUS)}$"), cmd_export_status
    ))

    be_cfg = load_be_schedule()
    if be_cfg and be_cfg.get("interval_seconds", 0) > 0:
        app.job_queue.run_repeating(
            _bulk_export_job,
            interval=be_cfg["interval_seconds"],
            first=be_cfg["interval_seconds"],
            name="bulk_export_job",
        )
        logger.info("Bulk export schedule restored: every %ds", be_cfg["interval_seconds"])
