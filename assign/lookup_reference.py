#!/usr/bin/env python3
"""
lookup_reference.py
====================
Lookup Reference — search a single stamp-duty reference number and report
its current status, valuer, and node (/lookup or "🔎 Lookup Reference").

The credential and search endpoint are chosen automatically from the ref's
own format, not picked by the user — no credential-picker step:
- A County reference (`CNTYINV/...`) can be at either of two stages, tried
  in order by `_lu_lookup_county` (a stage with no cached token for its
  own credential is silently skipped rather than treated as an error, so
  either stage alone can still succeed):
  1. The assessor/HQ stage (`_lu_search_ref_county`/`_lu_fetch_detail_county`
     — stampdutyservice hod-or-clr/detail-view, the same endpoint
     dlv_core.py/fetch_tasks.py use for the HQ side, but with filter=
     TO_VALUATION tried before Ongoing) under the Support Reg (`staff2`)
     credential.
  2. The DLV/valuation stage (`_lu_search_ref_county_dlv`, reusing the
     non-county path's own `_lu_fetch_detail`/`_lu_format_result` — the
     response shape already matches) under the Staff Valuer
     (`staff_valuer`) credential, forcing `request_type=COUNTY_STAMP_DUTY`
     and `from_ardhipay=true` since a county ref's request_type never
     matches `_LU_SEARCH_COMBOS`' STAMP_DUTY-only combos.
- Any other reference is searched via the existing DLV/VALUER
  application-list/detail-view endpoints (`_LU_SEARCH_COMBOS`) under the
  Staff Valuer (`staff_valuer`) credential exclusively.

_lu_search_ref/_lu_fetch_detail/_lu_format_result (the non-county path) are
exported as this module's public API — bot.py's
_post_assignment_report/_lookup_one_ref (used by New Assignment's
post-assignment verification, which always looks up non-county refs it
just assigned) import them from here rather than duplicating the search/
detail/format logic; they're unaffected by the county-routing change above
since that only applies to this module's own /lookup conversation.

Call register(app) from bot.py's main() to wire this feature in.
"""

import asyncio
import re
from enum import Enum, auto
from typing import Dict, Optional

from telegram import Update
from telegram.ext import (
    Application,
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
    _CANCEL_FILTER,
    _ft_headers,
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
    ASSESSOR_STAGE_DETAIL_URL,
    ASSESSOR_STAGE_LIST_URL,
    STAMP_DUTY_APPLICATION_DETAIL_URL,
    STAMP_DUTY_APPLICATION_LIST_URL,
)

# Credential each ref format is searched under — Lookup Reference is the
# only place Support Reg is used for a County ref; every other ref always
# uses Staff Valuer.
_LU_CRED_COUNTY  = "staff2"
_LU_CRED_DEFAULT = "staff_valuer"


# ──────────────────────────────────────────────────────────
# States — Lookup Reference conversation
# ──────────────────────────────────────────────────────────
class LU(Enum):
    REF_INPUT = auto()   # enter a reference number


def _lu_is_county_ref(ref: str) -> bool:
    """True for a County reference (CNTYINV/...), which is searched via a
    different endpoint/credential than every other reference format."""
    return ref.upper().startswith("CNTYINV")


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


# A County ref's request_type is COUNTY_STAMP_DUTY, which _LU_SEARCH_COMBOS
# never tries (always STAMP_DUTY) — so a county ref already past the
# assessor/HQ stage (not found by _lu_search_ref_county) needs its own combo
# list against this same valuationservice endpoint, forcing from_ardhipay
# and the county request_type. Same URL/detail-view/formatter as the
# non-county path (_lu_fetch_detail/_lu_format_result already handle this
# response shape), just a different search.
_LU_COUNTY_DLV_FILTERS = ["Ongoing", "Completed", "Returned"]


def _lu_search_ref_county_dlv(tokens: AuthTokens, ref: str) -> Optional[Dict]:
    """Search a County reference at the DLV/valuation stage — the DLV role,
    but with request_type=COUNTY_STAMP_DUTY and from_ardhipay=true, tried
    as a fallback to _lu_search_ref_county (the assessor/HQ stage) for a
    county ref that's moved on to DLV. Returns the list-item dict (with
    'id') or None."""
    http_sess = build_session()
    hdrs = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_DLV,
    }
    for filt in _LU_COUNTY_DLV_FILTERS:
        try:
            resp = http_sess.get(
                _LU_LIST_URL,
                headers=hdrs,
                params={
                    "filter":        filt,
                    "from_ardhipay": "true",
                    "role":          "DLV",
                    "request_type":  "COUNTY_STAMP_DUTY",
                    "search":        ref,
                    "page":          1,
                },
                timeout=30,
            )
            resp.raise_for_status()
            for item in resp.json().get("results", []):
                if item.get("reference_number") == ref:
                    item["_matched_filter"] = filt
                    return item
        except Exception as e:
            logger.warning("LU county DLV search filter=%s failed: %s", filt, e)
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
        "🔎 *Reference Lookup*\n",
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


# County (CNTYINV) refs are searched via the assessor/HQ stage endpoint
# instead — the same one dlv_core.py/fetch_tasks.py use, but with
# TO_VALUATION tried first, since a looked-up county ref has often already
# moved past the "Ongoing" assessor stage those two only ever check.
_LU_COUNTY_FILTERS = ["TO_VALUATION", "Ongoing"]


def _lu_search_ref_county(tokens: AuthTokens, ref: str) -> Optional[Dict]:
    """Search a County reference via the stampdutyservice hod-or-clr
    endpoint (from_ardhipay=true always, since a CNTYINV ref is by
    definition county-sourced). Returns the list-item dict (with 'id') or None."""
    http_sess = build_session()
    headers = _ft_headers(tokens)
    for filt in _LU_COUNTY_FILTERS:
        try:
            resp = http_sess.get(
                ASSESSOR_STAGE_LIST_URL,
                headers=headers,
                params={"filter": filt, "from_ardhipay": "true", "page": 1, "search": ref},
                timeout=30,
            )
            resp.raise_for_status()
            for item in resp.json().get("results", []):
                if item.get("reference_number") == ref:
                    item["_matched_filter"] = filt
                    return item
        except Exception as e:
            logger.warning("LU county search filter=%s failed: %s", filt, e)
    return None


def _lu_fetch_detail_county(tokens: AuthTokens, app_id: str) -> Optional[Dict]:
    """Detail-view for a County reference (stampdutyservice detail-view —
    a different response shape than _lu_fetch_detail's DLV detail-view:
    "officers" instead of "actors", node/application_status directly on
    the body rather than nested)."""
    http_sess = build_session()
    try:
        resp = http_sess.get(
            ASSESSOR_STAGE_DETAIL_URL,
            headers=_ft_headers(tokens),
            params={"request_id": app_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("details", {})
    except Exception as e:
        logger.warning("LU county detail fetch failed for id=%s: %s", app_id, e)
        return None


def _lu_format_county_result(ref: str, item: Dict, detail: Optional[Dict]) -> str:
    """Build the lookup result message for a County reference — same visual
    as _lu_format_result, but pulled from the stampdutyservice detail-view
    shape (officers list, node/application_status directly on the body,
    parcel/consideration under external_process_details)."""
    status = (item.get("application_status") or "—").upper()
    node_raw = ""
    valuer_name = "—"
    consideration = "—"
    ext: Dict = {}

    if detail:
        node_raw = detail.get("node", "")
        ext      = detail.get("external_process_details") or {}
        officers = detail.get("officers") or []
        vo       = next((o for o in officers if o.get("role") == "VALUATION OFFICER"), None)
        if vo:
            valuer_name = vo.get("names", "—")
        consideration_raw = ext.get("consideration_amount", "")
        if consideration_raw:
            currency = ext.get("currency_code", "KES")
            try:
                consideration = f"{currency} {float(consideration_raw):,.2f}"
            except (ValueError, TypeError):
                consideration = str(consideration_raw)

    node_label  = _NODE_LABELS.get(node_raw, node_raw or "—")
    registry    = (detail or {}).get("registry") or item.get("registry") or "—"
    county      = (detail or {}).get("county")   or item.get("county")   or "—"
    parcel      = ext.get("parcel_number") or item.get("parcel_number") or "—"
    created     = item.get("date_created", "—")

    lines = [
        "🔎 *Reference Lookup*\n",
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
    # No credential step — which account/endpoint to search with is decided
    # from the reference number itself, once entered (see recv_lu_ref).
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        "🔎 *Reference Lookup*\n\n"
        "Find out which valuer holds an application and its current node.\n\n"
        "Enter a *reference number* to look up\n"
        "(e.g. `NBI/STAMP/2024/12345` or `CNTYINV/AB12CD34EF`):",
        parse_mode="Markdown",
    )
    return LU.REF_INPUT


async def _lu_lookup_county(ref: str) -> Optional[str]:
    """A County ref can be at either the assessor/HQ stage (Support Reg,
    stampdutyservice hod-or-clr) or, once past that, the DLV/valuation
    stage (Staff Valuer, valuationservice application with request_type=
    COUNTY_STAMP_DUTY) — try the assessor stage first, then the DLV stage
    as a fallback, and format whichever one actually finds the ref (each
    stage only searches — and needs a cached token for — its own
    credential; a stage with no cached token is silently skipped, not
    treated as an error, since the other stage alone may still succeed).
    Returns None if neither stage finds the ref."""
    support_tokens = get_valid_tokens(_LU_CRED_COUNTY)
    if support_tokens:
        item = await asyncio.to_thread(_lu_search_ref_county, support_tokens, ref)
        if item:
            detail = await asyncio.to_thread(_lu_fetch_detail_county, support_tokens, item["id"])
            return _lu_format_county_result(ref, item, detail)

    valuer_tokens = get_valid_tokens(_LU_CRED_DEFAULT)
    if valuer_tokens:
        item = await asyncio.to_thread(_lu_search_ref_county_dlv, valuer_tokens, ref)
        if item:
            detail = await asyncio.to_thread(_lu_fetch_detail, valuer_tokens, item["id"])
            return _lu_format_result(ref, item, detail)

    return None


async def recv_lu_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Route by ref format: County -> try assessor stage (Support Reg) then
    # DLV/valuation stage (Staff Valuer) via _lu_lookup_county; everything
    # else -> Staff Valuer + the existing DLV/VALUER application list/detail-view.
    if not allowed(update): return await deny(update)

    ref = (update.message.text or "").strip()
    if not ref:
        await update.message.reply_text("Please enter a reference number.")
        return LU.REF_INPUT

    is_county = _lu_is_county_ref(ref)

    if not is_county:
        tokens = get_valid_tokens(_LU_CRED_DEFAULT)
        if not tokens:
            await update.message.reply_text(
                f"❌ No valid cached tokens for *{CRED_LABELS.get(_LU_CRED_DEFAULT, _LU_CRED_DEFAULT)}*. "
                "Use *🔑 Refresh Auth* first.",
                parse_mode="Markdown",
                reply_markup=_main_menu(),
            )
            return ConversationHandler.END
    elif not get_valid_tokens(_LU_CRED_COUNTY) and not get_valid_tokens(_LU_CRED_DEFAULT):
        await update.message.reply_text(
            f"❌ No valid cached tokens for *{CRED_LABELS.get(_LU_CRED_COUNTY, _LU_CRED_COUNTY)}* "
            f"or *{CRED_LABELS.get(_LU_CRED_DEFAULT, _LU_CRED_DEFAULT)}*. Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    await update.message.reply_text(f"🔍 Searching for `{ref}`…", parse_mode="Markdown")

    if is_county:
        result = await _lu_lookup_county(ref)
        if not result:
            await update.message.reply_text(
                f"❌ Reference `{ref}` not found at either the assessor stage "
                "(TO_VALUATION, Ongoing) or the DLV/valuation stage (Ongoing, "
                "Completed, Returned).\n\nCheck the reference number and try again.",
                parse_mode="Markdown",
                reply_markup=_main_menu(),
            )
            return ConversationHandler.END
        await update.message.reply_text(result, parse_mode="Markdown", reply_markup=_main_menu())
        return ConversationHandler.END

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
