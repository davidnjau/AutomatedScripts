#!/usr/bin/env python3
"""
job_distribution.py
====================
Job Distribution Analysis — reports how ongoing stamp-duty tasks are
distributed across DLV team members (/jobdist or "🏆 Job Distribution").

Fetches teams + members, all ongoing tasks (optionally county-filtered),
detail-fetches each to find its assigned valuation officer (with
multi-credential token rotation on 403, via token_rotator.py), then builds
a 4-sheet Excel report (team summary, member distribution, unassigned
tasks, team roster).

_JD_STATUS is exported so bot.py's cmd_export_status (a thin combiner that
also reads Bulk Export's _BE_STATUS) can render this feature's progress —
Bulk Export hasn't been extracted yet, so that combiner stays in bot.py.

Call register(app) from bot.py's main() to wire this feature in.
"""

import asyncio
import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

import openpyxl
import requests
from openpyxl.styles import Font, PatternFill
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
    BASE_URL,
    BTN_JOB_DIST,
    CPARAMS_DLV,
    CRED_LABELS,
    CRED_MAP,
    _be_cred_keyboard,
    _CANCEL_FILTER,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    logger,
)
from excel_report import autofit_columns, style_header_row
from token_rotator import _AllTokensExhausted, _TokenRotator, fetch_with_rotation

_JD_TOKEN_ROTATE_DELAY = 10   # seconds to wait before retrying with a new token
_JD_MAX_RETRIES        = 5    # max 429/5xx retries before aborting a detail fetch
_JD_WORKERS            = 5


# ──────────────────────────────────────────────────────────
# States — Job Distribution conversation
# ──────────────────────────────────────────────────────────
class JD(Enum):
    PICK_CRED = auto()   # select account with valid token
    COUNTY    = auto()   # multi-select county filter
    CONFIRM   = auto()   # confirm → kick off background analysis


_TEAMS_URL        = f"{BASE_URL}/acl/api/v1/list-teams"
_TEAM_MEMBERS_URL = f"{BASE_URL}/acl/api/v1/staff-teams/get-team-members"
_JD_ONGOING_URL   = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application"
_JD_DETAIL_URL    = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application/detail-view"

# Per-chat job distribution status tracker
_JD_STATUS: Dict[int, dict] = {}

# (key, display label) — key must match the county value returned by the API
_JD_COUNTIES: List[Tuple[str, str]] = [
    ("NAIROBI",  "🌆 Nairobi"),
    ("KIAMBU",   "🏙 Kiambu"),
    ("MURANGA",  "🏡 Murang'a"),
    ("MACHAKOS", "🏞 Machakos"),
    ("MOMBASA",  "🌊 Mombasa"),
    ("ISIOLO",   "🌿 Isiolo"),
    ("NAKURU",   "⛰ Nakuru"),
]
_JD_COUNTY_KEYS: List[str] = [k for k, _ in _JD_COUNTIES]


@dataclass
class JDSession:
    cred_type: str       = ""
    counties:  List[str] = field(default_factory=lambda: list(_JD_COUNTY_KEYS))


def _get_jd_sess(ctx: ContextTypes.DEFAULT_TYPE) -> JDSession:
    if "jd_session" not in ctx.user_data:
        ctx.user_data["jd_session"] = JDSession()
    return ctx.user_data["jd_session"]


def _jd_county_keyboard(selected: List[str]) -> InlineKeyboardMarkup:
    """Multi-select county picker. selected is a list of county keys."""
    rows = []
    for key, label in _JD_COUNTIES:
        mark = "✅" if key in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"jd_county:{key}")])
    rows.append([
        InlineKeyboardButton("☑️ All",   callback_data="jd_county:ALL"),
        InlineKeyboardButton("🔲 None",  callback_data="jd_county:NONE"),
    ])
    rows.append([
        InlineKeyboardButton("▶️ Run Analysis", callback_data="jd_county:done"),
        InlineKeyboardButton("❌ Cancel",        callback_data="jd_county:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _jd_headers(tokens: AuthTokens) -> dict:
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_DLV,
    }


def _jd_fetch_teams(sess: requests.Session, headers: dict) -> List[dict]:
    """Fetch all teams (not paginated in practice — count is small)."""
    resp = sess.get(_TEAMS_URL, headers=headers, params={"page": 1, "search": ""}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("results") or []


def _jd_fetch_team_members(sess: requests.Session, headers: dict, team_id: str) -> List[dict]:
    """Fetch all members of a team across all pages."""
    members: List[dict] = []
    page = 1
    while True:
        resp = sess.get(
            _TEAM_MEMBERS_URL,
            headers=headers,
            params={"team_id": team_id, "page": page, "search": ""},
            timeout=30,
        )
        resp.raise_for_status()
        data    = resp.json()
        results = data.get("results") or []
        members.extend(results)
        if not data.get("next"):
            break
        page += 1
    return members


def _jd_fetch_ongoing_page(sess: requests.Session, headers: dict, page: int) -> dict:
    """Fetch one page of Ongoing stamp-duty tasks."""
    resp = sess.get(
        _JD_ONGOING_URL,
        headers=headers,
        params={
            "filter":       "Ongoing",
            "role":         "DLV",
            "request_type": "STAMP_DUTY",
            "search":       "",
            "page":         page,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _jd_fetch_task_detail(sess: requests.Session, rotator: "_TokenRotator", task_id: str) -> dict:
    """Fetch detail for one task with token rotation on 403."""
    return fetch_with_rotation(
        sess, rotator, _JD_DETAIL_URL, {"request_id": task_id}, _jd_headers,
        context=f"task {task_id}",
        rotate_delay=_JD_TOKEN_ROTATE_DELAY, max_retries=_JD_MAX_RETRIES,
    )


def _jd_build_excel(
    teams: List[dict],
    members_by_team: Dict[str, List[dict]],
    tasks_by_userid: Dict[str, List[dict]],
    unassigned_tasks: List[dict],
) -> bytes:
    """Build the Job Distribution Excel workbook and return raw bytes."""
    wb       = openpyxl.Workbook()
    hdr_font = Font(bold=True)
    hdr_fill = PatternFill("solid", fgColor="BDD7EE")
    alt_fill = PatternFill("solid", fgColor="F2F2F2")

    def _header_row(ws, cols):
        style_header_row(ws, cols, font=hdr_font, fill=hdr_fill)

    def _autofit(ws):
        autofit_columns(ws)

    # ── Sheet 1: Team Summary ──────────────────────────────
    ws1 = wb.active
    ws1.title = "Team Summary"
    _header_row(ws1, [
        "Team Name", "Min Amount (KES)", "Max Amount (KES)",
        "Total Members", "Available", "Not Available",
        "Assigned Tasks", "Unassigned Tasks", "Assigned %",
    ])

    total_unassigned = len(unassigned_tasks)

    for i, team in enumerate(teams, start=2):
        tid     = team["id"]
        members = members_by_team.get(tid, [])
        available     = sum(1 for m in members if m.get("availability") == "AVAILABLE")
        not_available = len(members) - available
        assigned      = sum(len(tasks_by_userid.get(m["userid"], [])) for m in members)
        total_for_team = assigned + (total_unassigned if i == 2 else 0)
        pct = f"{assigned / total_for_team * 100:.1f}%" if total_for_team else "N/A"

        row = [
            team.get("team_name", ""),
            team.get("min_amount", ""),
            team.get("max_amount", ""),
            len(members),
            available,
            not_available,
            assigned,
            total_unassigned if i == 2 else "",
            pct,
        ]
        ws1.append(row)
        if i % 2 == 0:
            for c in range(1, 10):
                ws1.cell(row=i, column=c).fill = alt_fill
    _autofit(ws1)

    # ── Sheet 2: Member Distribution ──────────────────────
    ws2 = wb.create_sheet("Member Distribution")
    _header_row(ws2, [
        "Team", "Name", "Account Number", "Availability",
        "Registry", "Tasks Assigned", "Reference Numbers", "Analysis",
    ])

    warn_fill = PatternFill("solid", fgColor="FFE0B2")   # amber for out-of-range rows

    row_idx = 2
    for team in teams:
        tid     = team["id"]
        members = members_by_team.get(tid, [])
        members_sorted = sorted(
            members,
            key=lambda m: -len(tasks_by_userid.get(m.get("userid", ""), [])),
        )
        team_min = float(team.get("min_amount") or 0)
        team_max = float(team.get("max_amount") or float("inf"))

        for m in members_sorted:
            uid   = m.get("userid", "")
            tasks = tasks_by_userid.get(uid, [])

            in_range:  List[str] = []
            out_range: List[str] = []

            for t in tasks:
                ref    = t.get("reference_number", t.get("id", ""))
                amount = t.get("consideration_amount", "")
                if amount == "":
                    label = ref
                else:
                    try:
                        amt   = float(amount)
                        label = f"{ref}({int(amt):,})"
                        if team_min <= amt <= team_max:
                            in_range.append(label)
                        else:
                            out_range.append(label)
                        continue   # already appended above; skip the fallback append below
                    except (ValueError, TypeError):
                        label = ref
                in_range.append(label)   # reached when: amount is empty OR unparseable → assume in range

            refs = ", ".join(in_range + out_range)

            if out_range:
                analysis = f"⚠️ Out of range: {', '.join(out_range)}"
                if in_range:
                    analysis += f" | ✅ In range: {len(in_range)}"
            elif in_range:
                analysis = f"✅ All {len(in_range)} in range"
            else:
                analysis = ""

            ws2.append([
                team.get("team_name", ""),
                m.get("name", ""),
                m.get("account_number", ""),
                m.get("availability", ""),
                m.get("registry", ""),
                len(tasks),
                refs,
                analysis,
            ])
            fill = warn_fill if out_range else (alt_fill if row_idx % 2 == 0 else None)
            if fill:
                for c in range(1, 9):
                    ws2.cell(row=row_idx, column=c).fill = fill
            row_idx += 1
    _autofit(ws2)

    # ── Sheet 3: Unassigned / No Valuer Tasks ─────────────
    ws3 = wb.create_sheet("Unassigned Tasks")
    _header_row(ws3, [
        "Reference Number", "Parcel Number", "Registry",
        "County", "Date Created", "Status",
    ])
    for i, t in enumerate(unassigned_tasks, start=2):
        ws3.append([
            t.get("reference_number", ""),
            t.get("parcel_number", ""),
            t.get("registry", ""),
            t.get("county", ""),
            t.get("date_created", ""),
            t.get("status", ""),
        ])
        if i % 2 == 0:
            for c in range(1, 7):
                ws3.cell(row=i, column=c).fill = alt_fill
    _autofit(ws3)

    # ── Sheet 4: Team Roster & Cross-Team Members ──────────
    ws4 = wb.create_sheet("Team Roster")

    # Build userid → list of team names (to detect multi-team members)
    userid_to_teams: Dict[str, List[str]] = {}
    for team in teams:
        for m in members_by_team.get(team["id"], []):
            uid = m.get("userid", "")
            userid_to_teams.setdefault(uid, []).append(team.get("team_name", ""))

    multi_team_fill = PatternFill("solid", fgColor="FFF176")   # yellow for multi-team rows

    # ── Section A: per-team roster ─────────────────────────
    ws4.append(["TEAM ROSTER"])
    ws4.cell(row=ws4.max_row, column=1).font = Font(bold=True, size=13)
    ws4.append([])

    for team in teams:
        # Team header
        ws4.append([team.get("team_name", ""), f"({len(members_by_team.get(team['id'], []))} members)"])
        for c in range(1, 3):
            cell      = ws4.cell(row=ws4.max_row, column=c)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="BDD7EE")

        # Column headers
        ws4.append(["#", "Name", "Account Number", "Availability", "Registry", "Also In Teams"])
        for c in range(1, 7):
            cell      = ws4.cell(row=ws4.max_row, column=c)
            cell.font = hdr_font
            cell.fill = PatternFill("solid", fgColor="D9E1F2")

        members = sorted(members_by_team.get(team["id"], []), key=lambda m: m.get("name", ""))
        for idx, m in enumerate(members, start=1):
            uid        = m.get("userid", "")
            other_teams = [t for t in userid_to_teams.get(uid, []) if t != team.get("team_name", "")]
            also_in    = ", ".join(other_teams) if other_teams else ""
            ws4.append([
                idx,
                m.get("name", ""),
                m.get("account_number", ""),
                m.get("availability", ""),
                m.get("registry", ""),
                also_in,
            ])
            if other_teams:
                for c in range(1, 7):
                    ws4.cell(row=ws4.max_row, column=c).fill = multi_team_fill

        ws4.append([])   # blank row between teams

    # ── Section B: members in multiple teams ──────────────
    ws4.append([])
    ws4.append(["MEMBERS IN MULTIPLE TEAMS"])
    ws4.cell(row=ws4.max_row, column=1).font = Font(bold=True, size=13)
    ws4.cell(row=ws4.max_row, column=1).fill = PatternFill("solid", fgColor="FFF176")

    ws4.append(["Name", "Account Number", "Teams"])
    for c in range(1, 4):
        cell      = ws4.cell(row=ws4.max_row, column=c)
        cell.font = hdr_font
        cell.fill = PatternFill("solid", fgColor="D9E1F2")

    # collect all members once (avoid duplicates from members_by_team)
    seen_multi: set = set()
    for team in teams:
        for m in members_by_team.get(team["id"], []):
            uid        = m.get("userid", "")
            team_names = userid_to_teams.get(uid, [])
            if len(team_names) > 1 and uid not in seen_multi:
                seen_multi.add(uid)
                ws4.append([
                    m.get("name", ""),
                    m.get("account_number", ""),
                    ", ".join(team_names),
                ])
                for c in range(1, 4):
                    ws4.cell(row=ws4.max_row, column=c).fill = multi_team_fill

    if not seen_multi:
        ws4.append(["No members belong to more than one team."])

    _autofit(ws4)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _jd_run(tokens: AuthTokens, chat_id: int, bot, loop, counties: Optional[List[str]] = None) -> None:
    """Background worker: fetch teams + task distribution, build Excel, send."""
    def _tg(text: str):
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id, text, parse_mode="Markdown"),
            loop,
        ).result(timeout=15)

    def _set_status(**kwargs):
        _JD_STATUS.setdefault(chat_id, {}).update(kwargs)

    _JD_STATUS[chat_id] = {
        "phase":         "fetching teams",
        "started_at":    datetime.now(),
        "teams_count":   None,
        "members_total": None,
        "tasks_total":   None,
        "tasks_done":    0,
        "errors":        0,
        "completed_at":  None,
        "rows":          None,
        "error_msg":     None,
    }

    token_pairs = [(ct, get_valid_tokens(ct)) for ct in CRED_MAP if get_valid_tokens(ct)]
    token_pairs.sort(key=lambda p: 0 if p[1] is tokens else 1)
    rotator = _TokenRotator(token_pairs)

    sess    = build_session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=_JD_WORKERS, pool_maxsize=_JD_WORKERS, max_retries=0
    )
    sess.mount("https://", adapter)
    sess.mount("http://",  adapter)
    headers = _jd_headers(tokens)

    try:
        # ── Step 1: teams + members ────────────────────────────────
        teams = _jd_fetch_teams(sess, headers)
        _set_status(teams_count=len(teams))
        _tg(f"📋 Found *{len(teams)}* teams. Fetching members…")

        members_by_team: Dict[str, List[dict]] = {}
        all_members:     Dict[str, dict]       = {}   # userid → member record (with team_name)
        for team in teams:
            members = _jd_fetch_team_members(sess, headers, team["id"])
            members_by_team[team["id"]] = members
            for m in members:
                m["_team_name"] = team.get("team_name", "")
                all_members[m["userid"]] = m

        total_members = sum(len(v) for v in members_by_team.values())
        _set_status(phase="fetching ongoing tasks", members_total=total_members)
        _tg(f"👥 *{total_members}* team members loaded. Fetching ongoing tasks…")

        # ── Step 2: ongoing tasks list ─────────────────────────────
        first_page  = _jd_fetch_ongoing_page(sess, headers, 1)
        tasks_total = first_page.get("count", 0)
        task_list   = list(first_page.get("results") or [])
        page_size   = len(task_list) if task_list else 10
        if page_size == 0:
            page_size = 10
        total_pages = max(1, -(-tasks_total // page_size))

        _set_status(tasks_total=tasks_total)

        if total_pages > 1:
            with ThreadPoolExecutor(max_workers=_JD_WORKERS) as pool:
                futures = {pool.submit(_jd_fetch_ongoing_page, sess, headers, p): p
                           for p in range(2, total_pages + 1)}
                for fut in _futures_as_completed(futures):
                    task_list.extend(fut.result().get("results") or [])

        # Apply county filter if specified
        if counties:
            counties_upper = {c.upper() for c in counties}
            task_list = [t for t in task_list if t.get("county", "").upper() in counties_upper]
            county_labels = ", ".join(
                label for key, label in _JD_COUNTIES if key in counties_upper
            )
            _tg(f"🗺 County filter: *{county_labels}* → *{len(task_list)}* tasks remaining.")

        _set_status(phase="fetching task details", tasks_total=len(task_list))
        _tg(f"📄 *{len(task_list)}* ongoing tasks. Fetching assignment details…")

        # ── Step 3: detail fetch for each task ─────────────────────
        tasks_by_userid:  Dict[str, List[dict]] = {}   # userid → [task_summary, ...]
        unassigned_tasks: List[dict]            = []
        exhausted_flag = threading.Event()

        with ThreadPoolExecutor(max_workers=_JD_WORKERS) as pool:
            futures = {pool.submit(_jd_fetch_task_detail, sess, rotator, t["id"]): t
                       for t in task_list}
            for fut in _futures_as_completed(futures):
                task_summary = futures[fut]
                try:
                    if exhausted_flag.is_set():
                        fut.cancel()
                        continue
                    detail = fut.result()
                    actors = detail.get("actors") or []
                    vo     = next((a for a in actors if a.get("role") == "VALUATION OFFICER"), None)
                    if vo:
                        uid = (vo.get("user_details") or {}).get("id", "")
                        ext = detail.get("external_process_details") or {}
                        tasks_by_userid.setdefault(uid, []).append({
                            "reference_number":   detail.get("reference_number", ""),
                            "parcel_number":      detail.get("parcel_number", ""),
                            "registry":           detail.get("registry", ""),
                            "date_created":       detail.get("date_created", ""),
                            "consideration_amount": ext.get("consideration_amount", ""),
                        })
                    else:
                        unassigned_tasks.append(task_summary)
                    _set_status(tasks_done=_JD_STATUS[chat_id]["tasks_done"] + 1)
                except _AllTokensExhausted:
                    exhausted_flag.set()
                    unassigned_tasks.append(task_summary)
                    logger.warning("JD: all tokens exhausted at task %s", task_summary.get("id"))
                except Exception as exc:
                    logger.warning("JD detail failed task=%s: %s", task_summary.get("id"), exc)
                    unassigned_tasks.append(task_summary)
                    _set_status(
                        tasks_done=_JD_STATUS[chat_id]["tasks_done"] + 1,
                        errors=_JD_STATUS[chat_id]["errors"] + 1,
                    )

        if exhausted_flag.is_set():
            _tg("⚠️ Tokens exhausted during detail fetch — report will be partial. Refresh tokens and re-run for a complete picture.")

        # ── Step 4: build Excel and send ──────────────────────────
        _set_status(phase="building excel")
        assigned_count   = sum(len(v) for v in tasks_by_userid.values())
        unassigned_count = len(unassigned_tasks)
        _tg(
            f"✅ Analysis complete.\n"
            f"• Assigned: *{assigned_count}* tasks\n"
            f"• Unassigned / no VO: *{unassigned_count}* tasks\n"
            f"Building Excel report…"
        )

        filename   = f"Ardhisasa_Job_Distribution_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        xlsx_bytes = _jd_build_excel(teams, members_by_team, tasks_by_userid, unassigned_tasks)

        asyncio.run_coroutine_threadsafe(
            bot.send_document(
                chat_id,
                document=io.BytesIO(xlsx_bytes),
                filename=filename,
                caption=(
                    f"🏆 Job Distribution Report\n"
                    + (f"Counties: {', '.join(counties)}\n" if counties else "")
                    + f"Teams: {len(teams)} | Members: {total_members} | "
                    f"Assigned: {assigned_count} | Unassigned: {unassigned_count}"
                ),
            ),
            loop,
        ).result(timeout=60)

        _set_status(phase="done", completed_at=datetime.now(), rows=total_members)

    except Exception as exc:
        logger.error("JD worker crashed: %s", exc, exc_info=True)
        _set_status(phase="failed", completed_at=datetime.now(), error_msg=str(exc))
        _tg(f"❌ Job distribution failed: `{exc}`")


# ── Job Distribution conversation handlers ────────────────

async def cmd_job_distribution(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    sess           = _get_jd_sess(ctx)
    sess.cred_type = ""

    kbd = _be_cred_keyboard()
    if not kbd:
        await update.message.reply_text(
            "❌ No valid cached tokens. Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🏆 *Job Distribution Analysis*\n\n"
        "This report shows how ongoing tasks are distributed across team members.\n\n"
        "👤 *Select the account to run the analysis as:*",
        parse_mode="Markdown",
        reply_markup=kbd,
    )
    return JD.PICK_CRED


async def recv_jd_cred(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    sess           = _get_jd_sess(ctx)
    sess.cred_type = query.data.split(":")[1]
    sess.counties  = list(_JD_COUNTY_KEYS)   # reset to all selected
    cred_label     = CRED_LABELS.get(sess.cred_type, sess.cred_type)

    await query.edit_message_text(
        f"✅ Account: *{cred_label}*\n\n"
        "🗺 *Select counties to include in the report:*\n"
        "_(all 7 pre-selected — tap to toggle)_",
        parse_mode="Markdown",
        reply_markup=_jd_county_keyboard(sess.counties),
    )
    return JD.COUNTY


async def recv_jd_county(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    sess  = _get_jd_sess(ctx)
    data  = query.data.split(":", 1)[1]

    if data == "cancel":
        await query.answer()
        await query.edit_message_text("❌ Analysis cancelled.")
        await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
        return ConversationHandler.END

    if data == "done":
        if not sess.counties:
            await query.answer("Select at least one county first.", show_alert=True)
            return JD.COUNTY
        await query.answer()
        tokens = get_valid_tokens(sess.cred_type)
        if not tokens:
            cred_label = CRED_LABELS.get(sess.cred_type, sess.cred_type)
            await query.edit_message_text(
                f"❌ Tokens for *{cred_label}* have expired. Use *🔑 Refresh Auth* first.",
                parse_mode="Markdown",
            )
            await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
            return ConversationHandler.END

        county_labels = ", ".join(
            label for key, label in _JD_COUNTIES if key in sess.counties
        )
        await query.edit_message_text(
            f"⏳ Analysis running for: *{county_labels}*\n\nYou will be notified when done.",
            parse_mode="Markdown",
        )
        await ctx.bot.send_message(query.message.chat_id, "Returning to menu.", reply_markup=_main_menu())

        loop = asyncio.get_event_loop()
        asyncio.ensure_future(
            asyncio.to_thread(_jd_run, tokens, query.message.chat_id, ctx.bot, loop, list(sess.counties))
        )
        return ConversationHandler.END

    # Toggle logic
    await query.answer()
    if data == "ALL":
        sess.counties = list(_JD_COUNTY_KEYS)
    elif data == "NONE":
        sess.counties = []
    else:
        if data in sess.counties:
            sess.counties.remove(data)
        else:
            sess.counties.append(data)

    await query.edit_message_reply_markup(reply_markup=_jd_county_keyboard(sess.counties))
    return JD.COUNTY


async def recv_jd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Kept for backward compatibility — not reached in the current flow
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Cancelled.")
    await ctx.bot.send_message(query.message.chat_id, "Main menu.", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Job Distribution conversation into the given Application."""
    jd_conv = ConversationHandler(
        entry_points=[
            CommandHandler("jobdist", cmd_job_distribution),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_JOB_DIST)}$"), cmd_job_distribution),
        ],
        states={
            JD.PICK_CRED: [CallbackQueryHandler(recv_jd_cred,    pattern=r"^be_cred:")],
            JD.COUNTY:    [CallbackQueryHandler(recv_jd_county,  pattern=r"^jd_county:")],
            JD.CONFIRM:   [CallbackQueryHandler(recv_jd_confirm, pattern=r"^jd:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(jd_conv)
