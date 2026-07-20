#!/usr/bin/env python3
"""
hold_tasks.py
=============
Hold Tasks (✋ Hold Tasks button / /holdtasks) — guard specific assigned DLV
refs against being reassigned to someone else. Pick tasks (from this bot's
own tracked assignments, or a live DLV query) and a background job keeps
re-checking them: if the ref is still at the "valuer report pending" stage
but the current valuer differs from who it was held for, it's reassigned
back automatically. Once a held ref moves past that stage for any reason
(completed, returned, back to unassigned, or simply no longer found), it's
released from the hold queue on its own — no manual cleanup needed.

load_hold_tasks/save_hold_tasks are adapters over dlv_core's consolidated
ref-keyed DLV-lifecycle store (Group A JSON consolidation, the last of its
three phases) — a held ref's `hold` sub-object lives on the same record as
its queue/assignment/closed state rather than in a separate file. Both
release paths (auto-release in _process_hold_item, manual release in
recv_ht_release_confirm) call dlv_core.clear_hold_and_remove before
save_hold_tasks, since save_hold_tasks itself never removes a ref — it
only ever adds/refreshes, same rule as dlv_core.save_dlv_batch.

Call register(app) from bot.py's main() to wire this feature in.
"""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional

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
    ALLOWED_IDS,
    BTN_HOLD_TASKS,
    CPARAMS_DLV,
    _any_valid_tokens,
    _be_cred_keyboard,
    _CANCEL_FILTER,
    _main_menu,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    load_saved_assignments,
    logger,
    md_escape,
)
from dlv_core import (
    _classify_dlv_detail,
    _fetch_ref_detail_dlv,
    _load_consolidated,
    _save_consolidated,
    _search_ref_dlv,
    clear_hold_and_remove,
)
from endpoints import (
    STAMP_DUTY_APPLICATION_DETAIL_URL,
    STAMP_DUTY_APPLICATION_LIST_URL,
    STAMP_DUTY_FIX_APPLICATION_URL,
)
from token_rotator import _AllTokensExhausted, _TokenRotator, fetch_with_rotation

_HT_WORKERS = 5   # worker pool size for both live-candidate detail lookups and the guard job

# (label, seconds) options for the check-interval picker — every whole minute from 1 to 10
_HT_INTERVAL_OPTIONS = [(f"{m} min", m * 60) for m in range(1, 11)]

# Current guard-job interval in seconds (default 5 min); changed via the Held Queue viewer
_hold_tasks_interval: int = 300


# ──────────────────────────────────────────────────────────
# States — Hold Tasks conversation
# ──────────────────────────────────────────────────────────
class HT(Enum):
    MENU              = auto()   # "Add Tasks to Hold" vs "View Held Queue"
    CHOOSE_SOURCE     = auto()   # (Add) tracked assignments vs live DLV query
    LIVE_CRED         = auto()   # (Add + live) pick a credential with a cached token
    SELECT_CANDIDATES = auto()   # multi-select which candidate tasks to hold
    VIEW_QUEUE        = auto()   # held items + interval/check-now/release buttons
    RELEASE_SELECT    = auto()   # multi-select which held refs to manually release


@dataclass
class HTSession:
    cred_type:        str        = ""
    candidates:       List[Dict] = field(default_factory=list)   # [{ref, valuer_name, valuer_uid}]
    selected:         set        = field(default_factory=set)    # refs selected to hold
    release_items:    List[Dict] = field(default_factory=list)   # held-queue snapshot for release
    release_selected: set        = field(default_factory=set)    # refs selected for release


def _get_ht_sess(ctx: ContextTypes.DEFAULT_TYPE) -> HTSession:
    # Lazily create and cache this chat's Hold Tasks session on the PTB user_data dict.
    if "ht_session" not in ctx.user_data:
        ctx.user_data["ht_session"] = HTSession()
    return ctx.user_data["ht_session"]


# ──────────────────────────────────────────────────────────
# Persistence — adapters over dlv_core's consolidated ref-keyed store
# (Group A JSON consolidation), preserving the original list-of-dicts
# shape so every call site in this module is unaffected.
# ──────────────────────────────────────────────────────────

def load_hold_tasks() -> List[Dict]:
    """Every ref currently held (has a `hold` sub-object and isn't
    status=="removed"), projected to the legacy saved_hold_tasks.json item
    shape: {ref, held_valuer_name, held_valuer_uid, held_at, last_checked,
    last_error}."""
    store = _load_consolidated()
    return [
        {"ref": ref, **r["hold"]}
        for ref, r in store.items()
        if r.get("hold") and r.get("status") != "removed"
    ]


def save_hold_tasks(items: List[Dict]) -> None:
    """Upsert each item's hold fields onto whatever the store already knows
    about that ref — creating a bare status="assigned" record if the ref
    isn't tracked anywhere else yet (e.g. picked from a live DLV query this
    bot never itself assigned). Deliberately never touches a ref that's
    currently held but absent from `items` — same "add/refresh only, never
    remove" rule dlv_core.save_dlv_batch follows; both release paths in
    this module call dlv_core.clear_hold_and_remove first for exactly that
    reason."""
    store = _load_consolidated()
    for item in items:
        ref = item.get("ref")
        if not ref:
            continue
        hold_fields = {k: v for k, v in item.items() if k != "ref"}
        existing = store.get(ref) or {"ref": ref, "status": "assigned"}
        store[ref] = {**existing, "hold": hold_fields}
    _save_consolidated(store)


# ──────────────────────────────────────────────────────────
# Candidate sources
# ──────────────────────────────────────────────────────────

def _ht_tracked_candidates(held_refs: set) -> List[Dict]:
    """Build the 'tracked assignments' candidate list from saved_assignments.json,
    skipping any ref already in the hold queue. Pure — no network calls."""
    assignments = load_saved_assignments()
    return [
        {"ref": ref, "valuer_name": data.get("valuer_name", ""), "valuer_uid": data.get("valuer_uid", "")}
        for ref, data in assignments.items()
        if ref not in held_refs
    ]


def _ht_headers(tokens: AuthTokens) -> dict:
    # Same DLV-role headers dlv_core.py's own search/detail calls use.
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_DLV,
    }


def _ht_current_valuer(actors: List[Dict]) -> Optional[Dict]:
    """Pick the VALUATION OFFICER out of a detail-view actors list. Returns
    {"id", "names"} or None if no such actor is present. Pure."""
    vo = next((a for a in actors if a.get("role") == "VALUATION OFFICER"), None)
    if not vo:
        return None
    user_details = vo.get("user_details") or {}
    return {"id": user_details.get("id", ""), "names": user_details.get("names", "")}


def _ht_fetch_ongoing_list(sess: requests.Session, headers: dict, request_type: str) -> List[Dict]:
    """Fetch every page of the 'Ongoing' DLV list for one request_type, following
    the API's 'next' cursor. Mirrors valuer_tasks._vt_fetch_all_tasks's pagination."""
    results: List[Dict] = []
    page = 1
    while True:
        params = {"filter": "Ongoing", "role": "DLV", "request_type": request_type, "search": "", "page": page}
        if request_type == "COUNTY_STAMP_DUTY":
            params["from_ardhipay"] = "true"
        try:
            resp = sess.get(STAMP_DUTY_APPLICATION_LIST_URL, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results") or [])
            if not data.get("next"):
                break
            page += 1
        except Exception as e:
            logger.warning("Hold Tasks live-list page %d (%s) failed: %s", page, request_type, e)
            break
    return results


def _ht_live_candidates(tokens: AuthTokens, held_refs: set) -> List[Dict]:
    """
    Build the 'live DLV queue' candidate list: every currently-Ongoing task
    (both STAMP_DUTY and COUNTY_STAMP_DUTY) that's at the valuer-report-pending
    stage, with its current valuer — skipping refs already held. Detail
    lookups run under multi-credential rotation since this can be a lot of
    calls (same resilience pattern Valuer Tasks/Job Distribution use for their
    own bulk live listings).
    """
    sess = build_session()
    headers = _ht_headers(tokens)
    tasks = (
        _ht_fetch_ongoing_list(sess, headers, "STAMP_DUTY")
        + _ht_fetch_ongoing_list(sess, headers, "COUNTY_STAMP_DUTY")
    )
    tasks = [t for t in tasks if t.get("reference_number") not in held_refs]
    if not tasks:
        return []

    token_pairs = [("__primary__", tokens)]
    rotator = _TokenRotator(token_pairs)
    candidates: List[Dict] = []
    exhausted = False

    with ThreadPoolExecutor(max_workers=_HT_WORKERS) as pool:
        fut_map = {
            pool.submit(
                fetch_with_rotation, sess, rotator, STAMP_DUTY_APPLICATION_DETAIL_URL,
                {"request_id": t.get("id")}, _ht_headers,
                context=f"hold candidate {t.get('reference_number')}",
            ): t
            for t in tasks
        }
        for fut in _futures_as_completed(fut_map):
            task = fut_map[fut]
            if exhausted:
                fut.cancel()
                continue
            try:
                detail = fut.result()
            except _AllTokensExhausted:
                exhausted = True
                logger.warning("Hold Tasks: all tokens exhausted while building live candidates")
                continue
            except Exception as e:
                logger.warning("Hold Tasks detail fetch failed for %s: %s", task.get("reference_number"), e)
                continue
            if detail.get("node") != "VALUATION_STAMP_DUTY_VALUER_REPORT":
                continue
            current = _ht_current_valuer(detail.get("actors") or [])
            if not current:
                continue
            candidates.append({
                "ref":         task.get("reference_number", ""),
                "valuer_name": current.get("names", ""),
                "valuer_uid":  current.get("id", ""),
            })

    return candidates


# ──────────────────────────────────────────────────────────
# Guard decision + background job
# ──────────────────────────────────────────────────────────

def _ht_decide(found: bool, info: Optional[Dict], current_valuer: Optional[Dict], held_uid: str) -> str:
    """
    Pure decision — no I/O — for one held ref given its live state:
      - "release": not found anymore, closed (completed/returned), or no
        longer at the valuer-report-pending stage (moved on, or reverted
        back to unassigned) — nothing left to guard.
      - "revert": still at valuer-report-pending, but held by someone other
        than the person we're guarding it for — reassign back to them.
      - "keep": still correctly held — no action needed.
    """
    if not found or not info or info["bucket"] == "closed" or info["node"] != "VALUATION_STAMP_DUTY_VALUER_REPORT":
        return "release"
    if current_valuer and str(current_valuer.get("id", "")) != str(held_uid):
        return "revert"
    return "keep"


def _process_hold_item(tokens: AuthTokens, http_sess: requests.Session, item: Dict) -> Dict:
    """
    Re-check one held ref's live status and act on _ht_decide's outcome:
    revert a takeover, release a resolved/vanished ref, or just record that
    it's still correctly held. Returns {"item", "keep", "line"} — pure w.r.t.
    shared state, so callers can run this across worker threads and merge
    results afterwards, same shape as dlv_batch._process_dlv_batch_item.
    """
    ref              = item.get("ref", "")
    held_valuer_uid  = item.get("held_valuer_uid", "")
    held_valuer_name = item.get("held_valuer_name", "")

    try:
        task = _search_ref_dlv(tokens, ref)
        if not task:
            return {"item": item, "keep": False, "line": f"🔓 `{ref}` — no longer found in DLV, released from hold"}

        detail = _fetch_ref_detail_dlv(tokens, task["id"])
        if not detail:
            item["last_error"] = "Detail fetch returned empty response"
            return {"item": item, "keep": True, "line": None}

        info           = _classify_dlv_detail(detail)
        current_valuer = _ht_current_valuer(detail.get("actors") or [])
        action         = _ht_decide(True, info, current_valuer, held_valuer_uid)

        if action == "release":
            reason = "completed/returned" if info["bucket"] == "closed" else "no longer at valuer-report-pending stage"
            return {"item": item, "keep": False, "line": f"🔓 `{ref}` — released ({reason})"}

        if action == "revert":
            other_name = (current_valuer or {}).get("names") or "someone else"
            resp = http_sess.post(
                STAMP_DUTY_FIX_APPLICATION_URL,
                headers={
                    "Authorization": f"Bearer {tokens.access_token}",
                    "JWTAUTH":       f"Bearer {tokens.jwt}",
                },
                json={
                    "reference_number":  ref,
                    "valuation_officer": held_valuer_uid,
                    "node":              "VALUATION_STAMP_DUTY_VALUER_REPORT",
                },
                timeout=30,
            )
            resp.raise_for_status()
            item["last_checked"] = datetime.now().isoformat(timespec="seconds")
            item["last_error"]   = ""
            return {
                "item": item, "keep": True,
                "line": f"🔁 `{ref}` — taken over by *{md_escape(other_name)}*, "
                        f"reverted back to *{md_escape(held_valuer_name)}*",
            }

        # action == "keep"
        item["last_checked"] = datetime.now().isoformat(timespec="seconds")
        item["last_error"]   = ""
        return {"item": item, "keep": True, "line": None}

    except Exception as e:
        # Keep for retry on error — same as dlv_batch's per-item exception handling.
        item["last_error"] = str(e)[:120]
        logger.warning("Hold Tasks check failed for %s: %s", ref, e)
        return {"item": item, "keep": True, "line": None}


def _process_hold_items(tokens: AuthTokens) -> str:
    """
    Re-check every held ref across worker threads (same parallel-I/O pattern
    dlv_batch._process_dlv_batch_items uses). Returns a report string of only
    the noteworthy lines (reverts + releases) — "" if nothing changed.
    """
    items = load_hold_tasks()
    if not items:
        return ""

    http_sess = build_session()
    lines:     List[str]  = []
    remaining: List[Dict] = []
    released:  List[str]  = []

    with ThreadPoolExecutor(max_workers=_HT_WORKERS) as pool:
        fut_map = {pool.submit(_process_hold_item, tokens, http_sess, item): item for item in items}
        for fut in _futures_as_completed(fut_map):
            result = fut.result()
            if result["line"]:
                lines.append(result["line"])
            if result["keep"]:
                remaining.append(result["item"])
            else:
                released.append(result["item"].get("ref", ""))

    # save_hold_tasks never removes a ref on its own (see its docstring) —
    # an auto-release has no other status call updating these refs, so
    # clear the hold explicitly before the trimmed list is saved.
    if released:
        clear_hold_and_remove(released)
    save_hold_tasks(remaining)
    return "\n".join(lines)


async def _hold_tasks_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB repeating job: re-check every held ref and revert any takeover, releasing
    anything that's moved past the guarded stage. Broadcasts only when something
    actually happened — a silent tick never spams every allowed user."""
    items = load_hold_tasks()
    if not items:
        return

    tokens = _any_valid_tokens()
    if not tokens:
        logger.warning("Hold Tasks job: no valid tokens — skipping cycle.")
        return

    report = await asyncio.to_thread(_process_hold_items, tokens)
    if not report:
        return

    msg = f"✋ *Hold Tasks (auto)*\n{report}"
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…_(truncated)_"
    for chat_id in ALLOWED_IDS:
        try:
            await context.bot.send_message(chat_id, msg, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Hold Tasks job notify error for %s: %s", chat_id, e)


# ──────────────────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────────────────

def _ht_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Tasks to Hold", callback_data="ht_menu:add")],
        [InlineKeyboardButton("📋 View Held Queue",   callback_data="ht_menu:view")],
        [InlineKeyboardButton("🛑 Cancel",            callback_data="ht_menu:cancel")],
    ])


def _ht_source_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Tracked Assignments", callback_data="ht_src:tracked")],
        [InlineKeyboardButton("🌐 Live DLV Queue",       callback_data="ht_src:live")],
        [InlineKeyboardButton("🛑 Cancel",               callback_data="ht_src:cancel")],
    ])


def _ht_select_keyboard(candidates: List[Dict], selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, c in enumerate(candidates):
        icon = "✅" if c["ref"] in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{icon} {c['ref']} — {c.get('valuer_name') or 'Unassigned'}",
            callback_data=f"ht_toggle:{i}",
        )])
    rows.append([
        InlineKeyboardButton(f"✋ Hold Selected ({len(selected)})", callback_data="ht_confirm"),
        InlineKeyboardButton("🛑 Cancel", callback_data="ht_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _ht_queue_keyboard(items: List[Dict], current_interval: int) -> InlineKeyboardMarkup:
    interval_buttons = [
        InlineKeyboardButton(
            f"{'✅ ' if current_interval == secs else ''}{label}",
            callback_data=f"htq:interval:{secs}",
        )
        for label, secs in _HT_INTERVAL_OPTIONS
    ]
    rows = [[InlineKeyboardButton("▶️ Check Now", callback_data="htq:now")]]
    rows.extend(interval_buttons[i:i + 2] for i in range(0, len(interval_buttons), 2))
    if items:
        rows.append([InlineKeyboardButton("🗑 Release Task(s)", callback_data="htq:release")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="htq:close")])
    return InlineKeyboardMarkup(rows)


def _ht_release_keyboard(items: List[Dict], selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, item in enumerate(items):
        ref  = item.get("ref", "")
        icon = "✅" if ref in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{icon} {ref} — {item.get('held_valuer_name') or 'Unassigned'}",
            callback_data=f"ht_reltoggle:{i}",
        )])
    rows.append([
        InlineKeyboardButton(f"🗑 Release Selected ({len(selected)})", callback_data="ht_relconfirm"),
        InlineKeyboardButton("🛑 Cancel", callback_data="ht_relcancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _ht_interval_label(interval: int) -> str:
    # Reverse-lookup for display — falls back to the raw seconds if it's ever off-menu.
    return {secs: label for label, secs in _HT_INTERVAL_OPTIONS}.get(interval, f"{interval}s")


# ──────────────────────────────────────────────────────────
# Hold Tasks — conversation handlers
# ──────────────────────────────────────────────────────────

async def cmd_hold_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point: reset session state and show the Add/View menu.
    if not allowed(update): return await deny(update)
    sess = _get_ht_sess(ctx)
    sess.cred_type  = ""
    sess.candidates = []
    sess.selected   = set()

    held_count = len(load_hold_tasks())
    await update.message.reply_text(
        f"✋ *Hold Tasks*\n\n"
        f"Currently held: *{held_count}* task(s).\n\n"
        "Hold a task to guard its assignment — if someone else takes it over "
        "while it's still at the *valuer report pending* stage, it's "
        "automatically reassigned back to whoever's holding it.",
        parse_mode="Markdown",
        reply_markup=_ht_menu_keyboard(),
    )
    return HT.MENU


async def recv_ht_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Route the Add/View/Cancel choice.
    query  = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    if action == "view":
        return await _ht_show_queue(query.edit_message_text, query.message.chat_id, ctx)

    await query.edit_message_text(
        "Where should the candidate list come from?",
        reply_markup=_ht_source_keyboard(),
    )
    return HT.CHOOSE_SOURCE


async def recv_ht_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Route the tracked/live/Cancel choice on the Add path.
    query  = update.callback_query
    await query.answer()
    source = query.data.split(":")[1]
    sess   = _get_ht_sess(ctx)

    if source == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    held_refs = {i.get("ref") for i in load_hold_tasks()}

    if source == "tracked":
        sess.candidates = _ht_tracked_candidates(held_refs)
        return await _ht_show_candidates(query.edit_message_text, query.message.chat_id, ctx, sess)

    kbd = _be_cred_keyboard()
    if not kbd:
        await query.edit_message_text(
            "❌ No valid cached tokens. Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
        )
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    await query.edit_message_text("Select the account to search with:", reply_markup=kbd)
    return HT.LIVE_CRED


async def recv_ht_live_cred(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Confirm the picked credential still has valid tokens, then run the live search.
    query = update.callback_query
    await query.answer()
    sess           = _get_ht_sess(ctx)
    sess.cred_type = query.data.split(":")[1]

    tokens = get_valid_tokens(sess.cred_type)
    if not tokens:
        await query.edit_message_text("❌ Tokens expired. Use *🔑 Refresh Auth* first.", parse_mode="Markdown")
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    await query.edit_message_text("⏳ Searching the live DLV queue, please wait…")
    held_refs       = {i.get("ref") for i in load_hold_tasks()}
    sess.candidates = await asyncio.to_thread(_ht_live_candidates, tokens, held_refs)
    return await _ht_show_candidates(query.edit_message_text, query.message.chat_id, ctx, sess)


async def _ht_show_candidates(edit_fn, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, sess: HTSession) -> int:
    # Shared by both candidate sources: show the multi-select keyboard, or end if empty.
    if not sess.candidates:
        await edit_fn("ℹ️ No candidate tasks found (or everything found is already held).")
        await ctx.bot.send_message(chat_id, "Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    sess.selected = set()
    await edit_fn(
        f"Select task(s) to hold ({len(sess.candidates)} found):",
        reply_markup=_ht_select_keyboard(sess.candidates, sess.selected),
    )
    return HT.SELECT_CANDIDATES


async def recv_ht_select_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Flip one candidate's selected state and redraw the keyboard.
    query = update.callback_query
    await query.answer()
    sess = _get_ht_sess(ctx)
    idx  = int(query.data.split(":")[1])
    if 0 <= idx < len(sess.candidates):
        ref = sess.candidates[idx]["ref"]
        if ref in sess.selected:
            sess.selected.discard(ref)
        else:
            sess.selected.add(ref)
    await query.edit_message_reply_markup(reply_markup=_ht_select_keyboard(sess.candidates, sess.selected))
    return HT.SELECT_CANDIDATES


async def recv_ht_confirm_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Persist the selected candidates as new held items, or cancel/reprompt.
    query = update.callback_query
    await query.answer()
    sess = _get_ht_sess(ctx)
    data = query.data  # "ht_confirm" | "ht_cancel"

    if data == "ht_cancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    if not sess.selected:
        await query.edit_message_reply_markup(reply_markup=_ht_select_keyboard(sess.candidates, sess.selected))
        return HT.SELECT_CANDIDATES

    by_ref = {c["ref"]: c for c in sess.candidates}
    now    = datetime.now().isoformat(timespec="seconds")
    held   = load_hold_tasks()
    for ref in sess.selected:
        c = by_ref.get(ref)
        if not c:
            continue
        held.append({
            "ref":              ref,
            "held_valuer_name": c.get("valuer_name", ""),
            "held_valuer_uid":  c.get("valuer_uid", ""),
            "held_at":          now,
            "last_checked":     "",
            "last_error":       "",
        })
    save_hold_tasks(held)

    refs = ", ".join(f"`{r}`" for r in sorted(sess.selected))
    await query.edit_message_text(f"✋ Now holding *{len(sess.selected)}* task(s):\n{refs}", parse_mode="Markdown")
    await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
    return ConversationHandler.END


async def _ht_show_queue(edit_fn, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # Render the held-queue report + action keyboard, or end if the queue is empty.
    items = load_hold_tasks()
    if not items:
        await edit_fn("✅ No tasks are currently held.")
        await ctx.bot.send_message(chat_id, "Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    lines = [f"✋ *Held Tasks* — {len(items)} task(s)\n"]
    for item in items:
        line = f"• `{item.get('ref', '?')}` → *{md_escape(item.get('held_valuer_name', '?'))}*"
        if item.get("last_error"):
            line += f"\n  ⚠️ _{item['last_error']}_"
        lines.append(line)
    lines.append(f"\n_Current check interval: {_ht_interval_label(_hold_tasks_interval)}_")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…_(truncated)_"

    await edit_fn(msg, parse_mode="Markdown", reply_markup=_ht_queue_keyboard(items, _hold_tasks_interval))
    return HT.VIEW_QUEUE


async def recv_ht_queue_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle Check Now / interval change / Release / Close from the Held Queue viewer.
    global _hold_tasks_interval
    query = update.callback_query
    await query.answer()
    data = query.data  # "htq:now" | "htq:interval:N" | "htq:release" | "htq:close"

    if data == "htq:close":
        await query.edit_message_reply_markup(reply_markup=None)
        return ConversationHandler.END

    if data == "htq:now":
        tokens = _any_valid_tokens()
        if not tokens:
            await query.edit_message_text("⚠️ No valid tokens — please authenticate first.")
            return ConversationHandler.END
        items_before = load_hold_tasks()
        if not items_before:
            await query.edit_message_text("✅ Nothing held — nothing to check.")
            return ConversationHandler.END
        await query.edit_message_text(f"⏳ Checking {len(items_before)} held task(s), please wait…")
        report    = await asyncio.to_thread(_process_hold_items, tokens)
        remaining = load_hold_tasks()
        msg = f"✋ *Hold Tasks — Check Result*\n{report}" if report else "ℹ️ Nothing changed."
        if remaining:
            msg += f"\n\n✋ *{len(remaining)} task(s) still held*"
        if len(msg) > 4000:
            msg = msg[:4000] + "\n…_(truncated)_"
        await query.edit_message_text(msg, parse_mode="Markdown")
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    if data == "htq:release":
        sess                  = _get_ht_sess(ctx)
        sess.release_items    = load_hold_tasks()
        sess.release_selected = set()
        await query.edit_message_text(
            "🗑 Tap tasks to select, then confirm removal.",
            reply_markup=_ht_release_keyboard(sess.release_items, sess.release_selected),
        )
        return HT.RELEASE_SELECT

    if data.startswith("htq:interval:"):
        try:
            new_interval = int(data.split(":")[-1])
        except ValueError:
            return HT.VIEW_QUEUE
        _hold_tasks_interval = new_interval

        job_queue = ctx.job_queue
        if job_queue:
            for job in job_queue.get_jobs_by_name("hold_tasks_job"):
                job.schedule_removal()
            job_queue.run_repeating(
                _hold_tasks_job, interval=new_interval, first=new_interval, name="hold_tasks_job",
            )

        items = load_hold_tasks()
        await query.edit_message_reply_markup(reply_markup=_ht_queue_keyboard(items, _hold_tasks_interval))
        await query.message.reply_text(
            f"✅ Hold check interval set to *{_ht_interval_label(new_interval)}*.", parse_mode="Markdown",
        )
        return HT.VIEW_QUEUE

    return HT.VIEW_QUEUE


async def recv_ht_release_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Flip one held item's selected-for-release state and redraw the keyboard.
    query = update.callback_query
    await query.answer()
    sess = _get_ht_sess(ctx)
    idx  = int(query.data.split(":")[1])
    if 0 <= idx < len(sess.release_items):
        ref = sess.release_items[idx].get("ref")
        if ref in sess.release_selected:
            sess.release_selected.discard(ref)
        else:
            sess.release_selected.add(ref)
    await query.edit_message_reply_markup(
        reply_markup=_ht_release_keyboard(sess.release_items, sess.release_selected),
    )
    return HT.RELEASE_SELECT


async def recv_ht_release_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Remove the selected refs from the hold queue, or cancel/reprompt.
    query = update.callback_query
    await query.answer()
    sess = _get_ht_sess(ctx)
    data = query.data  # "ht_relconfirm" | "ht_relcancel"

    if data == "ht_relcancel":
        await query.edit_message_text("❌ Cancelled.")
        await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
        return ConversationHandler.END

    if not sess.release_selected:
        await query.edit_message_reply_markup(
            reply_markup=_ht_release_keyboard(sess.release_items, sess.release_selected),
        )
        return HT.RELEASE_SELECT

    remaining = [i for i in load_hold_tasks() if i.get("ref") not in sess.release_selected]
    # save_hold_tasks never removes a ref on its own — a bare manual release
    # has no other status call, so clear the hold explicitly first.
    clear_hold_and_remove(sess.release_selected)
    save_hold_tasks(remaining)

    released_refs = ", ".join(f"`{r}`" for r in sorted(sess.release_selected))
    await query.edit_message_text(
        f"🔓 Released *{len(sess.release_selected)}* task(s) from hold:\n{released_refs}\n\n"
        f"{len(remaining)} task(s) still held.",
        parse_mode="Markdown",
    )
    await query.message.reply_text("Use the menu to continue.", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration — called from bot.py's main()
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Hold Tasks conversation and the repeating guard job into the given Application."""
    ht_conv = ConversationHandler(
        entry_points=[
            CommandHandler("holdtasks", cmd_hold_tasks),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_HOLD_TASKS)}$"), cmd_hold_tasks),
        ],
        states={
            HT.MENU:              [CallbackQueryHandler(recv_ht_menu, pattern=r"^ht_menu:")],
            HT.CHOOSE_SOURCE:      [CallbackQueryHandler(recv_ht_source, pattern=r"^ht_src:")],
            HT.LIVE_CRED:          [CallbackQueryHandler(recv_ht_live_cred, pattern=r"^be_cred:")],
            HT.SELECT_CANDIDATES:  [
                CallbackQueryHandler(recv_ht_select_toggle, pattern=r"^ht_toggle:"),
                CallbackQueryHandler(recv_ht_confirm_add,   pattern=r"^ht_confirm$|^ht_cancel$"),
            ],
            HT.VIEW_QUEUE:         [CallbackQueryHandler(recv_ht_queue_action, pattern=r"^htq:")],
            HT.RELEASE_SELECT:     [
                CallbackQueryHandler(recv_ht_release_toggle,  pattern=r"^ht_reltoggle:"),
                CallbackQueryHandler(recv_ht_release_confirm, pattern=r"^ht_relconfirm$|^ht_relcancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(ht_conv)
    app.job_queue.run_repeating(
        _hold_tasks_job, interval=_hold_tasks_interval, first=_hold_tasks_interval, name="hold_tasks_job",
    )
