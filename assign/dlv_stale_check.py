#!/usr/bin/env python3
"""
dlv_stale_check.py
===================
Stale Pending checker — a background pass over DLV Batch's queue that
resolves refs stuck "Not found in DLV endpoint" for too long, instead of
retrying them forever. Piggybacks on dlv_batch.py's existing 1-minute
_dlv_batch_job cycle (dlv_batch._dlv_batch_job local-imports this module and
calls run_stale_check(context.bot) right after its own processing — a local
import rather than module-level, the same deferred-import convention
common.py/dlv_core.py already use for persist_assignment's "import dlv_core",
even though this module never actually imports dlv_batch.py back, so there's
no real cycle to avoid — just following the established precedent
defensively). No repeating job or interval setting of its own.

Once a queued ref crosses ⏳ Stale Pending Threshold (⚙️ Bot Settings, days,
persisted to SAVED_STALE_CHECK_CONFIG_FILE) days of being unfound, it gets a
full cross-stage check via lookup_reference._lu_current_valuer (the same
assessor-stage-then-DLV-stage, county-vs-non-county routing that produces
Lookup Reference's own "not found" messages) — reused rather than
duplicated, and now also returns parcel_number (see lookup_reference.py's
own _lu_current_valuer docstring) so this module never needs a second
search just to learn the parcel.

If genuinely not found anywhere:
- no parcel number on record (neither freshly looked up nor already on the
  queue item) -> removed via dlv_core.mark_removed, which already preserves
  queued_at/valuer_name/valuer_uid on the record (status="removed" +
  removed_at) — the audit trail the user asked for, with no new storage.
- parcel number on record -> parcel_lookup._pl_search_parcel (both DLV/
  Valuer and assessor/HQ stages) is searched for that parcel. No match ->
  same removal as above. One or more matches -> every allowed user
  (common.ALLOWED_IDS) is asked, via a stalefix: callback prompt (mirrors
  dlv_batch.py's _db_taken_keyboard/_send_db_taken_prompts/
  recv_db_taken_decision pattern exactly), whether to replace the old ref
  with one of the new ones — keeping the same valuer or picking a different
  one from Saved Valuers — or remove it outright.

A decision prompt that's answered is resolved immediately; one that's
ignored is resent automatically next time the same 24h recheck gate
(_STALE_RECHECK_HOURS) comes due, mirroring dlv_batch.py's own
_db_resend_stale_taken_prompts safety net, so a dropped/ignored prompt never
leaves a ref stuck forever.

Call register(app) from bot.py's main() to wire this feature in.
"""

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from common import (
    ALLOWED_IDS,
    BTN_STALE_THRESHOLD,
    DATA_DIR,
    _atomic_json_write,
    allowed,
    deny,
    get_valid_tokens,
    load_saved_valuers,
    logger,
    md_escape,
)
from dlv_core import load_dlv_batch, mark_removed, save_dlv_batch
from lookup_reference import _LU_CRED_DEFAULT, _lu_current_valuer, _lu_md_escape
from parcel_lookup import _PL_CRED_ASSESSOR, _pl_search_parcel

SAVED_STALE_CHECK_CONFIG_FILE = os.path.join(DATA_DIR, "saved_stale_check_config.json")

# Preset day-count choices offered by ⏳ Stale Pending Threshold.
_STALE_THRESHOLD_PRESETS = [7, 14, 21, 30, 60]
_STALE_DEFAULT_THRESHOLD_DAYS = 30

# Gates both "do a fresh cross-stage lookup" (an item never checked, or
# checked over this many hours ago) and "resend an ignored decision prompt"
# (same field, same gate — see _sc_candidates) so one timestamp covers both.
_STALE_RECHECK_HOURS = 24

# Cap on how many Parcel Lookup matches a decision prompt shows — keeps the
# keyboard a sane size and every callback_data comfortably under Telegram's
# 64-byte limit.
_STALE_MAX_MATCHES_SHOWN = 5


# ──────────────────────────────────────────────────────────
# Threshold persistence
# ──────────────────────────────────────────────────────────

def load_stale_check_config() -> Dict:
    # Persisted {"threshold_days": N} — defaults to 30 if never set.
    try:
        with open(SAVED_STALE_CHECK_CONFIG_FILE) as f:
            return {"threshold_days": json.load(f).get("threshold_days", _STALE_DEFAULT_THRESHOLD_DAYS)}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"threshold_days": _STALE_DEFAULT_THRESHOLD_DAYS}


def save_stale_check_config(threshold_days: int) -> None:
    # Persist the stale-pending threshold, in days.
    _atomic_json_write(SAVED_STALE_CHECK_CONFIG_FILE, {"threshold_days": threshold_days}, indent=2)


def _stale_threshold_keyboard(current_days: int) -> InlineKeyboardMarkup:
    # Preset day-count picker — mirrors dlv_batch.py's _dlv_queue_keyboard
    # interval picker shape exactly.
    row = [
        InlineKeyboardButton(
            f"{'✅ ' if current_days == d else ''}{d}d",
            callback_data=f"stalecfg:days:{d}",
        )
        for d in _STALE_THRESHOLD_PRESETS
    ]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("❌ Cancel", callback_data="stalecfg:cancel")]])


async def cmd_stale_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # ⏳ Stale Pending Threshold (⚙️ Bot Settings) — how many days a queued
    # ref can sit "Not found in DLV endpoint" before the background check
    # (piggybacked on DLV Batch's own 1-minute job) tries to resolve or
    # remove it.
    if not allowed(update): return await deny(update)
    days = load_stale_check_config()["threshold_days"]
    await update.message.reply_text(
        "⏳ *Stale Pending Threshold*\n\n"
        "Controls how many days a queued reference can sit \"Not found in "
        "DLV endpoint\" before the bot checks whether it's genuinely gone — "
        "removing it, or offering a replacement if its parcel number has "
        "since turned up under a new reference.\n\n"
        f"Current threshold: *{days} days*",
        parse_mode="Markdown",
        reply_markup=_stale_threshold_keyboard(days),
    )


async def recv_stale_threshold_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handles the ⏳ Stale Pending Threshold preset picker.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    data = query.data  # "stalecfg:days:N" | "stalecfg:cancel"

    if data == "stalecfg:cancel":
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data.startswith("stalecfg:days:"):
        try:
            new_days = int(data.split(":")[-1])
        except ValueError:
            return
        save_stale_check_config(new_days)
        await query.edit_message_reply_markup(reply_markup=_stale_threshold_keyboard(new_days))
        await query.message.reply_text(
            f"✅ Stale pending threshold set to *{new_days} days*.",
            parse_mode="Markdown",
        )


# ──────────────────────────────────────────────────────────
# Candidate selection
# ──────────────────────────────────────────────────────────

def _sc_age_days(queued_at: str) -> int:
    # Whole days since queued_at — 0 if missing/unparseable, so a corrupt
    # timestamp never falsely trips the stale threshold.
    if not queued_at:
        return 0
    try:
        queued_date = datetime.fromisoformat(queued_at).date()
    except ValueError:
        return 0
    return (datetime.now().date() - queued_date).days


def _sc_hours_since(ts: Optional[str], now: datetime) -> float:
    # Hours since an ISO timestamp — infinite (always due) if ts is missing
    # or unparseable.
    if not ts:
        return float("inf")
    try:
        return (now - datetime.fromisoformat(ts)).total_seconds() / 3600
    except ValueError:
        return float("inf")


def _sc_candidates(items: List[Dict], threshold_days: int, now: datetime) -> List[Dict]:
    # Queue items due for stale-check attention this cycle: either a fresh
    # cross-stage lookup (never checked, or checked over _STALE_RECHECK_HOURS
    # ago, and old enough to have crossed threshold_days) or a resend of an
    # already-outstanding replace/remove decision (same recheck-hours gate,
    # reused for both purposes so one timestamp field covers it).
    due = []
    for item in items:
        if item.get("last_error") != "Not found in DLV endpoint":
            continue
        if _sc_hours_since(item.get("stale_checked_at"), now) < _STALE_RECHECK_HOURS:
            continue
        if not item.get("stale_decision_pending") and _sc_age_days(item.get("queued_at", "")) < threshold_days:
            continue
        due.append(item)
    return due


# ──────────────────────────────────────────────────────────
# Removal notice
# ──────────────────────────────────────────────────────────

def _sc_removed_notice(item: Dict, reason: str) -> str:
    # Telegram-facing summary of a ref the stale check removed. The removal
    # itself (status="removed" + removed_at, queued_at/valuer_name/valuer_uid
    # preserved) is handled entirely by dlv_core.mark_removed's existing
    # merge-onto-record behavior — this is just the audit-trail message, not
    # a second storage write.
    ref = item.get("ref", "")
    return (
        "🗑 *Stale Pending Removed*\n\n"
        f"`{_lu_md_escape(ref)}` — {reason}.\n"
        f"Queued: {item.get('queued_at', '—')} for *{md_escape(item.get('valuer_name', '—'))}*\n"
        f"Removed: {datetime.now().isoformat(timespec='seconds')}"
    )


# ──────────────────────────────────────────────────────────
# Replace/remove decision prompt
# ──────────────────────────────────────────────────────────

def _sc_match_keyboard(old_ref: str, match_refs: List[str]) -> InlineKeyboardMarkup:
    # One button per candidate replacement ref, plus Remove. old_ref alone
    # is enough callback-data context at this first step — the chosen
    # new_ref is stashed on the item (stale_replacement_ref) rather than
    # carried in callback_data, to keep every callback comfortably under
    # Telegram's 64-byte limit once two refs would otherwise be encoded.
    rows = [
        [InlineKeyboardButton(f"➡️ {new_ref}", callback_data=f"stalefix:use:{old_ref}:{new_ref}")]
        for new_ref in match_refs
    ]
    rows.append([InlineKeyboardButton("🗑 Remove — don't replace", callback_data=f"stalefix:remove:{old_ref}")])
    return InlineKeyboardMarkup(rows)


def _sc_match_prompt_text(item: Dict) -> str:
    # Prompt text for a stale ref whose parcel turned up under one or more
    # new references.
    ref     = item.get("ref", "")
    parcel  = item.get("parcel", "")
    matches = item.get("stale_matches", [])
    age     = _sc_age_days(item.get("queued_at", ""))
    return (
        f"⏳ `{_lu_md_escape(ref)}` has been pending {age} day(s) and isn't found "
        f"anywhere, but its parcel `{_lu_md_escape(parcel)}` now appears under "
        f"{len(matches)} other reference(s) — queued for *{md_escape(item.get('valuer_name', ''))}*.\n\n"
        "Replace it with one of these, or remove it outright?"
    )


async def _send_stale_match_prompt(bot, item: Dict) -> None:
    # Send item's replace/remove decision prompt to every allowed user —
    # best-effort per chat, matching dlv_batch._send_db_taken_prompts.
    text   = _sc_match_prompt_text(item)
    markup = _sc_match_keyboard(item.get("ref", ""), item.get("stale_matches", []))
    for chat_id in ALLOWED_IDS:
        try:
            await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            logger.warning("Stale check match-prompt send error for %s: %s", chat_id, e)


def _sc_confirm_keyboard(old_ref: str) -> InlineKeyboardMarkup:
    # Same-valuer vs change-valuer choice, once a replacement ref has been
    # picked (stalefix:use:...) and stashed on the item as
    # stale_replacement_ref. Only old_ref is needed in callback_data — the
    # chosen new_ref is read back off the stored item.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Same valuer", callback_data=f"stalefix:same:{old_ref}")],
        [InlineKeyboardButton("👤 Change valuer", callback_data=f"stalefix:changeval:{old_ref}")],
    ])


def _sc_valuer_pick_keyboard(old_ref: str, saved: List[Dict]) -> InlineKeyboardMarkup:
    # Saved-Valuers-only picker for "change valuer" — index-based callback
    # data, the same acceptable-race tradeoff new_assignment.py's own
    # saved-valuer picker already makes, since encoding a valuer uid
    # alongside old_ref would push callback_data past Telegram's 64-byte
    # limit.
    rows = [
        [InlineKeyboardButton(f"👤 {sv['name']}", callback_data=f"stalefix:setval:{old_ref}:{i}")]
        for i, sv in enumerate(saved)
    ]
    return InlineKeyboardMarkup(rows)


def _sc_finalize_replacement(old_item: Dict, new_ref: str, valuer_name: str, valuer_uid: str, valuer_acct: str) -> None:
    # Remove old_ref (audit trail via mark_removed) and queue new_ref in its
    # place, carrying over tag/assessor/consideration/currency_code —
    # logically the same task, new reference number.
    mark_removed({old_item.get("ref", "")})
    carried = {k: old_item[k] for k in ("tag", "assessor", "consideration", "currency_code") if old_item.get(k)}
    new_item = {
        **carried,
        "ref":          new_ref,
        "valuer_name":  valuer_name,
        "valuer_uid":   valuer_uid,
        "valuer_acct":  valuer_acct,
        "queued_at":    datetime.now().isoformat(timespec="seconds"),
    }
    save_dlv_batch([new_item])


async def recv_stale_fix_decision(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Global handler (not conversation-scoped — answerable by any allowed
    # user at any time, same reasoning as dlv_batch.recv_db_taken_decision)
    # for every stalefix: callback: picking a replacement ref, choosing
    # same/change valuer, picking a saved valuer, or removing outright.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]

    items = load_dlv_batch()
    by_ref = {i.get("ref"): i for i in items}

    if action == "remove":
        old_ref = parts[2]
        item = by_ref.get(old_ref)
        if not item:
            await query.edit_message_text("ℹ️ Already resolved — nothing to do.")
            return
        mark_removed({old_ref})
        save_dlv_batch([i for i in items if i.get("ref") != old_ref])
        await query.edit_message_text(f"🗑 `{old_ref}` removed from the DLV queue.", parse_mode="Markdown")
        return

    if action == "use":
        old_ref, new_ref = parts[2], parts[3]
        item = by_ref.get(old_ref)
        if not item:
            await query.edit_message_text("ℹ️ Already resolved — nothing to do.")
            return
        item["stale_replacement_ref"] = new_ref
        save_dlv_batch([item])
        await query.edit_message_text(
            f"Replace `{old_ref}` with `{new_ref}`? Keep *{md_escape(item.get('valuer_name', ''))}* "
            "as the valuer, or pick a different one?",
            parse_mode="Markdown",
            reply_markup=_sc_confirm_keyboard(old_ref),
        )
        return

    if action == "same":
        old_ref = parts[2]
        item = by_ref.get(old_ref)
        new_ref = item.get("stale_replacement_ref") if item else None
        if not item or not new_ref:
            await query.edit_message_text("ℹ️ Already resolved — nothing to do.")
            return
        _sc_finalize_replacement(
            item, new_ref, item.get("valuer_name", ""), item.get("valuer_uid", ""), item.get("valuer_acct", ""))
        await query.edit_message_text(
            f"✅ `{old_ref}` replaced with `{new_ref}` under *{md_escape(item.get('valuer_name', ''))}* — re-queued.",
            parse_mode="Markdown",
        )
        return

    if action == "changeval":
        old_ref = parts[2]
        saved = load_saved_valuers()
        if not saved:
            await query.edit_message_text("ℹ️ No saved valuers to pick from.")
            return
        await query.edit_message_text("Pick a valuer:", reply_markup=_sc_valuer_pick_keyboard(old_ref, saved))
        return

    if action == "setval":
        old_ref, idx_str = parts[2], parts[3]
        item = by_ref.get(old_ref)
        new_ref = item.get("stale_replacement_ref") if item else None
        saved = load_saved_valuers()
        try:
            sv = saved[int(idx_str)]
        except (ValueError, IndexError):
            sv = None
        if not item or not new_ref or not sv:
            await query.edit_message_text("ℹ️ Already resolved — nothing to do.")
            return
        _sc_finalize_replacement(item, new_ref, sv["name"], sv["uid"], sv.get("account_number", ""))
        await query.edit_message_text(
            f"✅ `{old_ref}` replaced with `{new_ref}` under *{md_escape(sv['name'])}* — re-queued.",
            parse_mode="Markdown",
        )
        return


# ──────────────────────────────────────────────────────────
# The stale check itself
# ──────────────────────────────────────────────────────────

async def run_stale_check(bot) -> None:
    """Called once per dlv_batch._dlv_batch_job cycle. Scans the DLV Batch
    queue for refs stuck "Not found in DLV endpoint" past the configured
    threshold, resolves each via a full cross-stage Lookup Reference check
    (lookup_reference._lu_current_valuer), and either leaves it alone (still
    genuinely in transit), removes it (genuinely gone, with or without a
    parcel to fall back on), or asks every allowed user whether to replace
    it with a new reference Parcel Lookup found for the same parcel. One
    try/except per candidate so one bad ref can't abort the whole pass,
    mirroring dlv_batch._process_dlv_batch_item."""
    items = load_dlv_batch()
    if not items:
        return

    threshold_days = load_stale_check_config()["threshold_days"]
    now = datetime.now()
    candidates = _sc_candidates(items, threshold_days, now)
    if not candidates:
        return

    removed_refs: List[str] = []
    notices: List[str] = []

    for item in candidates:
        ref = item.get("ref", "")
        try:
            if item.get("stale_decision_pending"):
                # Resend: matches were already found and stored on the item,
                # so this doesn't re-search — just nudges the same prompt
                # again since it was apparently ignored.
                item["stale_checked_at"] = now.isoformat(timespec="seconds")
                await _send_stale_match_prompt(bot, item)
                continue

            result = await _lu_current_valuer(ref)
            if not result["tokens_available"]:
                continue  # Retried next cycle, no gate update.

            if result["found"]:
                item["stale_checked_at"] = now.isoformat(timespec="seconds")
                if result.get("parcel_number"):
                    item["parcel"] = result["parcel_number"]
                continue

            parcel = result.get("parcel_number") or item.get("parcel")
            if not parcel:
                removed_refs.append(ref)
                notices.append(_sc_removed_notice(item, "not found anywhere, and no parcel number on record"))
                continue

            valuer_tokens   = get_valid_tokens(_LU_CRED_DEFAULT)
            assessor_tokens = get_valid_tokens(_PL_CRED_ASSESSOR)
            matches = await asyncio.to_thread(_pl_search_parcel, valuer_tokens, assessor_tokens, parcel)
            match_refs = [
                m.get("reference_number") for m in matches
                if m.get("reference_number") and m.get("reference_number") != ref
            ]

            if not match_refs:
                removed_refs.append(ref)
                notices.append(_sc_removed_notice(
                    item,
                    f"not found anywhere, and its parcel `{_lu_md_escape(parcel)}` isn't showing up "
                    "under any other reference either",
                ))
                continue

            item["parcel"]                 = parcel
            item["stale_decision_pending"] = True
            item["stale_matches"]          = match_refs[:_STALE_MAX_MATCHES_SHOWN]
            item["stale_checked_at"]       = now.isoformat(timespec="seconds")
            await _send_stale_match_prompt(bot, item)

        except Exception as e:
            logger.warning("Stale check failed for %s: %s", ref, e)

    if removed_refs:
        mark_removed(removed_refs)
    save_dlv_batch([i for i in items if i.get("ref") not in removed_refs])

    for text in notices:
        for chat_id in ALLOWED_IDS:
            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
            except Exception as e:
                logger.warning("Stale check removal notice send error for %s: %s", chat_id, e)


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the ⏳ Stale Pending Threshold Bot Settings entry and the global
    stalefix: decision handler. No repeating job of its own —
    run_stale_check is invoked by dlv_batch._dlv_batch_job's existing
    1-minute cycle."""
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_STALE_THRESHOLD)}$"), cmd_stale_threshold))
    app.add_handler(CallbackQueryHandler(recv_stale_threshold_action, pattern=r"^stalecfg:"))
    app.add_handler(CallbackQueryHandler(recv_stale_fix_decision, pattern=r"^stalefix:"))
