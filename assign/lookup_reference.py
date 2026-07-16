#!/usr/bin/env python3
"""
lookup_reference.py
====================
Lookup Reference — search a single stamp-duty reference number across all
filter/role combos and report its current status, valuer, and node
(/lookup or "🔎 Lookup Reference").

_lu_search_ref/_lu_fetch_detail/_lu_format_result are exported as this
module's public API — bot.py's _post_assignment_report/_lookup_one_ref
(used by New Assignment's post-assignment verification) import them from
here rather than duplicating the search/detail/format logic.

Call register(app) from bot.py's main() to wire this feature in.
"""

import asyncio
import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, Optional

from telegram import Update
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
    BTN_LOOKUP,
    CPARAMS_DLV,
    CPARAMS_VALUER_ROLE,
    CRED_LABELS,
    _be_cred_keyboard,
    _CANCEL_FILTER,
    _main_menu,
    _NODE_LABELS,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    logger,
    not_cancel,
)
from endpoints import (
    STAMP_DUTY_APPLICATION_DETAIL_URL,
    STAMP_DUTY_APPLICATION_LIST_URL,
)


# ──────────────────────────────────────────────────────────
# States — Lookup Reference conversation
# ──────────────────────────────────────────────────────────
class LU(Enum):
    PICK_CRED = auto()   # select account with valid token
    REF_INPUT = auto()   # enter reference number(s)


@dataclass
class LUSession:
    cred_type: str = ""


def _get_lu_sess(ctx: ContextTypes.DEFAULT_TYPE) -> LUSession:
    if "lu_session" not in ctx.user_data:
        ctx.user_data["lu_session"] = LUSession()
    return ctx.user_data["lu_session"]


# (filter, role, cparams) combos to try when searching by reference number.
# Ordered from most likely to least likely.
_LU_SEARCH_COMBOS = [
    ("Ongoing",   "DLV",    CPARAMS_DLV),
    ("Pending",   "DLV",    CPARAMS_DLV),
    ("Completed", "DLV",    CPARAMS_DLV),
    ("Ongoing",   "VALUER", CPARAMS_VALUER_ROLE),
    ("Pending",   "VALUER", CPARAMS_VALUER_ROLE),
]

_LU_LIST_URL   = STAMP_DUTY_APPLICATION_LIST_URL
_LU_DETAIL_URL = STAMP_DUTY_APPLICATION_DETAIL_URL


def _lu_search_ref(tokens: AuthTokens, ref: str) -> Optional[Dict]:
    """Search all filter/role combos for ref. Returns the list-item dict (with 'id') or None."""
    http_sess = build_session()
    hdrs = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }
    for filt, role, cparams in _LU_SEARCH_COMBOS:
        try:
            resp = http_sess.get(
                _LU_LIST_URL,
                headers={**hdrs, "cparams": cparams},
                params={
                    "filter":       filt,
                    "role":         role,
                    "request_type": "STAMP_DUTY",
                    "search":       ref,
                    "page":         1,
                },
                timeout=30,
            )
            resp.raise_for_status()
            for item in resp.json().get("results", []):
                if item.get("reference_number") == ref:
                    item["_matched_filter"] = filt
                    return item
        except Exception as e:
            logger.warning("LU search combo %s/%s failed: %s", filt, role, e)
    return None


def _lu_fetch_detail(tokens: AuthTokens, app_id: str) -> Optional[Dict]:
    """Fetch detail-view for a given internal application ID."""
    http_sess = build_session()
    try:
        resp = http_sess.get(
            _LU_DETAIL_URL,
            headers={
                "Authorization": f"Bearer {tokens.access_token}",
                "JWTAUTH":       f"Bearer {tokens.jwt}",
                "cparams":       CPARAMS_DLV,
            },
            params={"request_id": app_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("LU detail fetch failed for id=%s: %s", app_id, e)
        return None


def _lu_extract_context(item: Dict, detail: Optional[Dict]) -> Dict:
    """Pull registry/county/parcel/consideration/currency out of a search
    item + detail-view pair as raw values — the same fields
    _lu_format_result renders, but for callers that persist them (e.g.
    new_assignment.py enriching saved_assignments.json) rather than
    display them."""
    ext = (detail or {}).get("external_process_details") or {}
    return {
        "registry":       (detail or item).get("registry") or item.get("registry") or "",
        "county":         (detail or item).get("county") or item.get("county") or "",
        "parcel":         (detail or item).get("parcel_number") or item.get("parcel_number") or
                          ext.get("parcel_number") or "",
        "consideration":  ext.get("consideration_amount", "") or "",
        "currency_code":  ext.get("currency_code", "KES"),
    }


def _lu_format_result(ref: str, item: Dict, detail: Optional[Dict]) -> str:
    """Build the lookup result message from list-item + detail-view data."""
    status = (item.get("application_status") or item.get("status") or "—").upper()
    node_raw = ""
    valuer_name = "—"
    consideration = "—"
    ext: Dict = {}

    if detail:
        node_raw    = detail.get("node", "")
        ext         = detail.get("external_process_details") or {}
        actors      = detail.get("actors") or []
        vo          = next((a for a in actors if a.get("role") == "VALUATION OFFICER"), None)
        if vo:
            valuer_name = (vo.get("user_details") or {}).get("names", "—")
        consideration_raw = ext.get("consideration_amount", "")
        if consideration_raw:
            currency = ext.get("currency_code", "KES")
            try:
                consideration = f"{currency} {float(consideration_raw):,.2f}"
            except (ValueError, TypeError):
                consideration = str(consideration_raw)

    node_label  = _NODE_LABELS.get(node_raw, node_raw or "—")
    registry    = (detail or item).get("registry") or item.get("registry") or "—"
    county      = (detail or item).get("county")   or item.get("county")   or "—"
    parcel      = (detail or item).get("parcel_number") or item.get("parcel_number") or \
                  ext.get("parcel_number") or "—"
    created     = item.get("date_created", "—")

    lines = [
        f"🔎 *Reference Lookup*\n",
        f"📌 *Ref:* `{ref}`",
        f"📊 *Status:* {status}",
        f"🔄 *Node:* {node_label}",
        f"👤 *Valuer:* {valuer_name}",
        f"🏢 *Registry:* {registry}",
        f"📍 *County:* {county}",
        f"💰 *Consideration:* {consideration}",
        f"📋 *Parcel:* {parcel}",
        f"📅 *Created:* {created}",
    ]
    return "\n".join(lines)


# ── Lookup Reference conversation handlers ────────────────

async def cmd_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    sess           = _get_lu_sess(ctx)
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
        "🔎 *Reference Lookup*\n\n"
        "Find out which valuer holds an application and its current node.\n\n"
        "👤 *Select the account to search with:*",
        parse_mode="Markdown",
        reply_markup=kbd,
    )
    return LU.PICK_CRED


async def recv_lu_cred(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()

    sess           = _get_lu_sess(ctx)
    sess.cred_type = query.data.split(":")[1]

    await query.edit_message_text(
        f"✅ Account: *{CRED_LABELS.get(sess.cred_type, sess.cred_type)}*\n\n"
        "Enter a *reference number* to look up\n"
        "(e.g. `NBI/STAMP/2024/12345`):",
        parse_mode="Markdown",
    )
    return LU.REF_INPUT


async def recv_lu_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await deny(update)

    sess = _get_lu_sess(ctx)
    ref  = (update.message.text or "").strip()

    if not ref:
        await update.message.reply_text("Please enter a reference number.")
        return LU.REF_INPUT

    tokens = get_valid_tokens(sess.cred_type)
    if not tokens:
        await update.message.reply_text(
            f"❌ Tokens expired. Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    await update.message.reply_text(f"🔍 Searching for `{ref}`…", parse_mode="Markdown")

    item = await asyncio.to_thread(_lu_search_ref, tokens, ref)
    if not item:
        await update.message.reply_text(
            f"❌ Reference `{ref}` not found across all filters (Ongoing, Pending, Completed).\n\n"
            "Check the reference number and try again.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    detail = await asyncio.to_thread(_lu_fetch_detail, tokens, item["id"])
    result = _lu_format_result(ref, item, detail)

    await update.message.reply_text(result, parse_mode="Markdown", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Lookup Reference conversation into the given Application."""
    lu_conv = ConversationHandler(
        entry_points=[
            CommandHandler("lookup", cmd_lookup),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_LOOKUP)}$"), cmd_lookup),
        ],
        states={
            LU.PICK_CRED: [CallbackQueryHandler(recv_lu_cred, pattern=r"^be_cred:")],
            LU.REF_INPUT: [MessageHandler(not_cancel, recv_lu_ref)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(lu_conv)
