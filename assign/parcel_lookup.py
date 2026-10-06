#!/usr/bin/env python3
"""
parcel_lookup.py
==================
Parcel Lookup — check whether a parcel number has landed in the (non-
county) Stamp Duty application list view (/parcelcheck or "🏞 Parcel
Lookup"). A sibling to lookup_reference.py's /lookup, keyed on
parcel_number instead of reference_number.

Unlike a reference-number search (exactly one application can ever own a
given reference number), a parcel number can appear on more than one
application over time (e.g. re-lodged after rejection), so
_pl_search_parcel returns every distinct matching reference rather than
stopping at the first hit — deduped by reference_number in case the same
application surfaces under more than one filter/role combo.

Reuses lookup_reference.py's existing filter/role/cparams combo list
(_LU_SEARCH_COMBOS) and credential (_LU_CRED_DEFAULT) rather than
duplicating them, so the two searches stay in sync if either changes.
Scoped to the same non-county Stamp Duty list endpoint lookup_reference.py
searches by default — no LRD routing here; add it later if this needs to
answer the same question for that workflow.

Also checks the pre-DLV assessor/HQ collector stage (stampdutyservice
hod-or-clr, both the HQ and County variants — see
_pl_search_parcel_assessor_stage) under Support Reg (_PL_CRED_ASSESSOR),
so a parcel that hasn't yet been pushed into DLV's queue still shows up
rather than reporting "not found." _pl_search_parcel merges both stages;
either credential's cached tokens may be missing, in which case that
stage is silently skipped rather than failing the whole search.

Deliberately does not fetch per-match detail-view (valuer name, etc.) for
either stage — the DLV/Valuer list-endpoint response already carries
status/node/registry/county/date_created (see endpoints.py's
STAMP_DUTY_APPLICATION_LIST_URL comment), which is enough to answer "has
it landed and what state is it in" without one extra API round trip per
match; the assessor-stage list endpoint carries no status/node at all, so
those matches are labeled with the stage/variant they were found under
instead (see _pl_format_match).

Once matches are found, delivery mirrors dlv_tasks.py's DELIVERY/
EMAIL_INPUT pattern — "💬 View on Telegram" or "📩 Send to Email", the
latter emailed via email_service._send_auto_fetch_email (plain-text +
auto HTML, same as Auto Fetch's body) rather than
_send_bulk_export_email, since this report has no spreadsheet — just the
same labeled-block text either channel would show.

List mode: entering more than one parcel number (one per line, or comma-
separated — see common._parse_list_input) searches each in turn and
compiles all of them into the one delivery choice/report, rather than
requiring a separate /parcelcheck run per parcel. PLSession.batch (a list
of (parcel, matches) pairs) carries this instead of the single-parcel
parcel/matches fields; recv_pl_delivery/recv_pl_email pick whichever is
populated. Capped at common._LIST_INPUT_MAX_ITEMS since each parcel is
its own sequential search across every filter/role combo.

Call register(app) from bot.py's main() to wire this feature in.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

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
    BTN_PARCEL_LOOKUP,
    CRED_LABELS,
    _CANCEL_FILTER,
    _ft_headers,
    _LIST_INPUT_MAX_ITEMS,
    _main_menu_for,
    _NODE_LABELS,
    _parse_list_input,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    get_valid_tokens,
    logger,
    md_escape,
    not_cancel,
)
from email_service import _send_auto_fetch_email
from endpoints import ASSESSOR_STAGE_LIST_URL, STAMP_DUTY_APPLICATION_LIST_URL
from lookup_reference import _LU_CRED_DEFAULT, _LU_SEARCH_COMBOS, _lu_md_escape
from task_block import format_labeled_block
from telegram_report import _send_chunked_report

# Credential the assessor/HQ collector (pre-DLV) stage is searched under —
# Support Reg, same credential lookup_reference.py's county-ref assessor-
# stage search (_lu_search_ref_county) uses.
_PL_CRED_ASSESSOR = "staff2"

# (stage label, extra query params) variants of the assessor/HQ stage to
# check — HQ (digitised) and County (from_ardhipay) — mirroring
# dlv_core._search_ref_stampduty's fallback search, which checks both for
# the same reason: a parcel/ref alone doesn't say which one it's under.
_PL_ASSESSOR_VARIANTS = [
    ("Assessor/HQ",     {}),
    ("Assessor/County", {"from_ardhipay": "true"}),
]


# ──────────────────────────────────────────────────────────
# States — Parcel Lookup conversation
# ──────────────────────────────────────────────────────────
class PL(Enum):
    PARCEL_INPUT = auto()   # enter a parcel number
    DELIVERY     = auto()   # telegram or email, once matches are found
    EMAIL_INPUT  = auto()   # enter email address (email delivery only)


@dataclass
class PLSession:
    parcel:  str        = ""              # single-parcel mode: the parcel number searched
    matches: List[Dict] = field(default_factory=list)   # single-parcel mode: its matching list-items
    batch:   List[Tuple[str, List[Dict]]] = field(default_factory=list)   # list mode: [(parcel, matches), ...]


def _get_pl_sess(ctx: ContextTypes.DEFAULT_TYPE) -> PLSession:
    """Fetch (creating if absent) this chat's Parcel Lookup session, holding
    either a single parcel/matches pair or a list-mode batch between the
    search step and the delivery choice — recv_pl_delivery/recv_pl_email
    check .batch first and fall back to .parcel/.matches."""
    if "pl_session" not in ctx.user_data:
        ctx.user_data["pl_session"] = PLSession()
    return ctx.user_data["pl_session"]


_PL_TOKEN_RE = re.compile(r"[A-Z]+|\d+")


def _pl_tokenize_parcel(value: str) -> List[str]:
    """Split into uppercase word/number tokens — letters and digits are
    separate token types, and everything else (spaces, slashes, dashes,
    punctuation) is an implicit separator. "block123" and "Block 123"
    both tokenize to ["BLOCK", "123"], keeping word and number tokens
    distinguishable after separators are gone (needed by _pl_tokens_match
    to apply a different matching rule to each kind)."""
    return _PL_TOKEN_RE.findall((value or "").upper())


def _pl_tokens_match(search_tokens: List[str], candidate_tokens: List[str]) -> bool:
    """True if every search token matches some candidate token, walking
    through candidate_tokens in order — so an omitted or abbreviated
    candidate word (e.g. a search that drops "Municipality" entirely, or
    shortens it to "Muni") doesn't break the match, while tokens still
    can't be matched out of order or reused. A word token matches as a
    prefix ("MUNI" matches "MUNICIPALITY"); a number token must match
    exactly — block/plot numbers are precise identifiers, and a prefix
    match on "12" would also hit "123", "1256", etc., which gets noisy
    fast."""
    idx = 0
    for st in search_tokens:
        matched = False
        while idx < len(candidate_tokens):
            candidate = candidate_tokens[idx]
            idx += 1
            if (candidate == st) if st.isdigit() else candidate.startswith(st):
                matched = True
                break
        if not matched:
            return False
    return True


def _pl_search_parcel_valuer_stage(tokens: AuthTokens, parcel: str) -> List[Dict]:
    """Search every _LU_SEARCH_COMBOS filter/role combo (DLV + Valuer
    stages, under staff_valuer) for parcel — a tokenized, order-preserving
    match against each result's parcel_number (see _pl_tokens_match), not
    an exact or plain-substring one, so a fragment like "BLOCK209" or
    "209/309" matches "NAIROBI/BLOCK209/309", an abbreviated word like
    "Mavoko Muni" matches "Mavoko/Municipality", and formatting
    differences (separators, spacing, case) never prevent a match that
    would otherwise be correct. Returns every distinct matching list-item
    dict, deduped by reference_number, in first-seen order."""
    http_sess = build_session()
    hdrs = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }
    target_tokens = _pl_tokenize_parcel(parcel)
    seen: Dict[str, Dict] = {}
    for filt, role, cparams in _LU_SEARCH_COMBOS:
        try:
            resp = http_sess.get(
                STAMP_DUTY_APPLICATION_LIST_URL,
                headers={**hdrs, "cparams": cparams},
                params={
                    "filter":       filt,
                    "role":         role,
                    "request_type": "STAMP_DUTY",
                    "search":       parcel,
                    "page":         1,
                },
                timeout=30,
            )
            resp.raise_for_status()
            for item in resp.json().get("results", []):
                candidate_tokens = _pl_tokenize_parcel(item.get("parcel_number") or "")
                if not target_tokens or not _pl_tokens_match(target_tokens, candidate_tokens):
                    continue
                ref = item.get("reference_number")
                if ref and ref not in seen:
                    item["_matched_filter"] = filt
                    seen[ref] = item
        except Exception as e:
            logger.warning("PL search combo %s/%s failed: %s", filt, role, e)
    return list(seen.values())


def _pl_search_parcel_assessor_stage(tokens: AuthTokens, parcel: str) -> List[Dict]:
    """Search the assessor/HQ collector stage (stampdutyservice hod-or-clr
    — the pre-DLV stage a task sits at before an assessor pushes it into
    DLV's queue) for parcel, trying both _PL_ASSESSOR_VARIANTS. Same
    tokenized match as _pl_search_parcel_valuer_stage. Unlike
    STAMP_DUTY_APPLICATION_LIST_URL, this list endpoint carries no
    application_status/node on its results (see endpoints.py's
    ASSESSOR_STAGE_LIST_URL comment), so matches are tagged
    _assessor_stage=True plus which variant matched; _pl_format_match
    renders a stage label in place of a real status/node for these."""
    http_sess = build_session()
    headers = _ft_headers(tokens)
    target_tokens = _pl_tokenize_parcel(parcel)
    seen: Dict[str, Dict] = {}
    for stage_label, extra_params in _PL_ASSESSOR_VARIANTS:
        try:
            resp = http_sess.get(
                ASSESSOR_STAGE_LIST_URL,
                headers=headers,
                params={"filter": "Ongoing", "page": 1, "search": parcel, **extra_params},
                timeout=30,
            )
            resp.raise_for_status()
            for item in resp.json().get("results", []):
                candidate_tokens = _pl_tokenize_parcel(item.get("parcel_number") or "")
                if not target_tokens or not _pl_tokens_match(target_tokens, candidate_tokens):
                    continue
                ref = item.get("reference_number")
                if ref and ref not in seen:
                    item["_matched_filter"] = stage_label
                    item["_assessor_stage"] = True
                    seen[ref] = item
        except Exception as e:
            logger.warning("PL assessor-stage search %s failed: %s", stage_label, e)
    return list(seen.values())


def _pl_search_parcel(
    valuer_tokens: Optional[AuthTokens],
    assessor_tokens: Optional[AuthTokens],
    parcel: str,
) -> List[Dict]:
    """Search parcel across both pipeline stages — the DLV/Valuer stage
    (_pl_search_parcel_valuer_stage, staff_valuer) and the pre-DLV
    assessor/HQ collector stage (_pl_search_parcel_assessor_stage, Support
    Reg) — and merge into one list, deduped by reference_number
    (valuer-stage match wins on a collision, though in practice a ref only
    ever lives in one stage at a time). Either token set may be None (that
    credential has no cached login), in which case that stage is silently
    skipped rather than failing the whole search — the same convention
    lookup_reference.py's county-ref routing already uses. Shared by
    Parcel Lookup's on-demand /parcelcheck and Parcel Watch's scheduled
    checks, so both search the same way."""
    seen: Dict[str, Dict] = {}
    if valuer_tokens:
        for item in _pl_search_parcel_valuer_stage(valuer_tokens, parcel):
            seen.setdefault(item.get("reference_number"), item)
    if assessor_tokens:
        for item in _pl_search_parcel_assessor_stage(assessor_tokens, parcel):
            seen.setdefault(item.get("reference_number"), item)
    return list(seen.values())


def _pl_format_match(i: int, item: Dict, markdown: bool = True) -> str:
    """Render one matching application as a labeled block — Status, Node,
    Registry, County, Created — the fields already available on the list-
    item without a detail-view call. A DLV/Valuer-stage match has a real
    application_status/node; an assessor/HQ-stage match (_assessor_stage,
    see _pl_search_parcel_assessor_stage) has neither at the list-item
    level, so Status/Node instead show which pre-DLV stage/variant it was
    found under. markdown=False (email body) skips Markdown escaping/ref
    backticks, same convention as fetch_tasks._ft_format_task_block."""
    if item.get("_assessor_stage"):
        status     = f"PRE-DLV ({item.get('_matched_filter', 'Assessor/HQ')})"
        node_label = "Assessor / HQ Collector stage"
    else:
        status     = (item.get("application_status") or "—").upper()
        node_label = _NODE_LABELS.get(item.get("node", ""), item.get("node") or "—")
    fields = [
        ("📊 Status",   status),
        ("🔄 Node",     node_label),
        ("🏢 Registry", item.get("registry") or "—"),
        ("📍 County",   item.get("county") or "—"),
        ("📅 Created",  item.get("date_created") or "—"),
    ]
    return format_labeled_block(i, item.get("reference_number", "—"), fields, markdown=markdown)


async def cmd_parcel_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point — prompts for the parcel number, no credential step
    # (searched under Staff Valuer for the DLV/Valuer stages, same as
    # lookup_reference.py's default non-county path, plus Support Reg for
    # the pre-DLV assessor/HQ collector stage).
    if not allowed(update): return await deny(update)
    await update.message.reply_text(
        "🏞 *Parcel Lookup*\n\n"
        "Find out whether a parcel number has landed in the Stamp Duty "
        "pipeline — DLV, Valuer, or the pre-DLV Assessor/HQ collector "
        "stage — and which reference(s) it's under.\n\n"
        "Enter a *parcel number* to check\n"
        f"_or paste up to {_LIST_INPUT_MAX_ITEMS}, one per line or comma-separated, "
        "for a compiled report._",
        parse_mode="Markdown",
    )
    return PL.PARCEL_INPUT


def _pl_delivery_keyboard() -> InlineKeyboardMarkup:
    """Telegram-vs-email delivery choice — shared by both the single-
    parcel and list-mode paths in recv_pl_parcel."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📩 Send to Email", callback_data="pl_delivery:email"),
            InlineKeyboardButton("💬 View on Telegram", callback_data="pl_delivery:telegram"),
        ],
    ])


async def recv_pl_parcel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # One parcel -> the original single-parcel flow (miss reports "not
    # found" and ends; hit stashes parcel/matches). More than one parcel
    # (list mode, one per line or comma-separated) -> search each in turn
    # and stash the whole batch instead, then ask how to deliver the
    # compiled result either way.
    if not allowed(update): return await deny(update)

    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text("Please enter a parcel number.")
        return PL.PARCEL_INPUT

    parcels = _parse_list_input(raw)
    if len(parcels) > _LIST_INPUT_MAX_ITEMS:
        await update.message.reply_text(
            f"❌ Too many parcels ({len(parcels)}) — max {_LIST_INPUT_MAX_ITEMS} per list.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    valuer_tokens   = get_valid_tokens(_LU_CRED_DEFAULT)
    assessor_tokens = get_valid_tokens(_PL_CRED_ASSESSOR)
    if not valuer_tokens and not assessor_tokens:
        await update.message.reply_text(
            f"❌ No valid cached tokens for *{CRED_LABELS.get(_LU_CRED_DEFAULT, _LU_CRED_DEFAULT)}* "
            f"or *{CRED_LABELS.get(_PL_CRED_ASSESSOR, _PL_CRED_ASSESSOR)}*. "
            "Use *🔑 Refresh Auth* first.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    sess = _get_pl_sess(ctx)

    if len(parcels) > 1:
        await update.message.reply_text(f"🔍 Searching {len(parcels)} parcel(s)…", parse_mode="Markdown")
        batch = [
            (parcel, await asyncio.to_thread(_pl_search_parcel, valuer_tokens, assessor_tokens, parcel))
            for parcel in parcels
        ]
        sess.parcel, sess.matches, sess.batch = "", [], batch

        total = sum(len(matches) for _, matches in batch)
        await update.message.reply_text(
            f"✅ Found *{total}* application(s) across *{len(parcels)}* parcel(s). "
            "How would you like to receive the result?",
            parse_mode="Markdown",
            reply_markup=_pl_delivery_keyboard(),
        )
        return PL.DELIVERY

    parcel = parcels[0]
    sess.batch = []
    await update.message.reply_text(f"🔍 Searching for `{_lu_md_escape(parcel)}`…", parse_mode="Markdown")

    matches = await asyncio.to_thread(_pl_search_parcel, valuer_tokens, assessor_tokens, parcel)
    if not matches:
        await update.message.reply_text(
            f"❌ Parcel `{_lu_md_escape(parcel)}` not found — checked DLV, Valuer, and Assessor/HQ "
            "collector stages.\n\n"
            "It hasn't landed in the Stamp Duty pipeline yet — check the parcel number and try again.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END

    sess.parcel  = parcel
    sess.matches = matches

    await update.message.reply_text(
        f"✅ Found on *{len(matches)}* application(s). How would you like to receive the result?",
        parse_mode="Markdown",
        reply_markup=_pl_delivery_keyboard(),
    )
    return PL.DELIVERY


def _pl_report_lines(parcel: str, matches: List[Dict], markdown: bool = True) -> List[str]:
    """Build the header + one block per match — shared by the Telegram and
    email delivery paths (and by parcel_watch.py's background job, which
    has no PLSession of its own) so all render identically apart from
    Markdown escaping. Takes parcel/matches directly rather than a
    PLSession so it's reusable outside this module's own conversation."""
    parcel_disp = _lu_md_escape(parcel) if markdown else parcel
    header = f"🏞 *Parcel Lookup* — `{parcel_disp}` found on {len(matches)} application(s)" if markdown \
        else f"Parcel Lookup — {parcel_disp} found on {len(matches)} application(s)"
    return [header] + [_pl_format_match(i, item, markdown=markdown) for i, item in enumerate(matches, start=1)]


def _pl_report_lines_batch(batch: List[Tuple[str, List[Dict]]], markdown: bool = True) -> List[str]:
    """List-mode counterpart to _pl_report_lines: one compiled report
    covering every parcel searched, in entry order — a per-parcel
    sub-header (found-count, or a plain "not found" line) followed by
    that parcel's matches."""
    total = sum(len(matches) for _, matches in batch)
    header = f"🏞 *Parcel Lookup* — {len(batch)} parcel(s), {total} application(s) total" if markdown \
        else f"Parcel Lookup — {len(batch)} parcel(s), {total} application(s) total"
    lines = [header]
    for parcel, matches in batch:
        parcel_disp = _lu_md_escape(parcel) if markdown else parcel
        if not matches:
            lines.append(f"❌ `{parcel_disp}` — not found" if markdown else f"{parcel_disp} — not found")
            continue
        lines.append(
            f"📌 *{parcel_disp}* — {len(matches)} application(s)" if markdown
            else f"{parcel_disp} — {len(matches)} application(s)"
        )
        lines += [_pl_format_match(i, item, markdown=markdown) for i, item in enumerate(matches, start=1)]
    return lines


async def recv_pl_delivery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Telegram -> send the chunked report immediately and end. Email ->
    # ask for the address next. sess.batch (list mode) takes priority over
    # sess.parcel/matches (single-parcel mode) when both could apply.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":")[1]
    sess = _get_pl_sess(ctx)

    if mode == "telegram":
        lines = _pl_report_lines_batch(sess.batch, markdown=True) if sess.batch \
            else _pl_report_lines(sess.parcel, sess.matches, markdown=True)

        async def _send(text, reply_markup):
            await ctx.bot.send_message(query.message.chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

        await _send_chunked_report(_send, lines, join="\n\n", reply_markup=_main_menu_for(update.effective_user.id))
        return ConversationHandler.END

    await query.edit_message_text(
        "📧 Enter the email address to receive the result:",
        parse_mode="Markdown",
    )
    return PL.EMAIL_INPUT


async def recv_pl_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Validate the email, send the plain-text report through
    # email_service, and report success/failure back on Telegram.
    if not allowed(update): return await deny(update)
    email = (update.message.text or "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        await update.message.reply_text(
            "❌ Invalid email. Enter a valid address.",
            parse_mode="Markdown",
        )
        return PL.EMAIL_INPUT

    sess = _get_pl_sess(ctx)
    lines = _pl_report_lines_batch(sess.batch, markdown=False) if sess.batch \
        else _pl_report_lines(sess.parcel, sess.matches, markdown=False)
    body    = "\n\n".join(lines)
    subject_target = f"{len(sess.batch)} parcels" if sess.batch else sess.parcel
    subject = f"Ardhisasa Parcel Lookup — {subject_target} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    try:
        await asyncio.to_thread(_send_auto_fetch_email, email, subject, body)
        await update.message.reply_text(
            f"📧 Result sent to *{md_escape(email)}*.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Email failed: `{exc}`", parse_mode="Markdown", reply_markup=_main_menu_for(update.effective_user.id))

    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Parcel Lookup conversation into the given Application."""
    pl_conv = ConversationHandler(
        entry_points=[
            CommandHandler("parcelcheck", cmd_parcel_lookup),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_PARCEL_LOOKUP)}$"), cmd_parcel_lookup),
        ],
        states={
            PL.PARCEL_INPUT: [MessageHandler(not_cancel, recv_pl_parcel)],
            PL.DELIVERY:     [CallbackQueryHandler(recv_pl_delivery, pattern=r"^pl_delivery:")],
            PL.EMAIL_INPUT:  [MessageHandler(not_cancel, recv_pl_email)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(pl_conv)
