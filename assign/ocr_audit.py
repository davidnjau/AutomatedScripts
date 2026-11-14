#!/usr/bin/env python3
"""
ocr_audit.py
============
🖼 OCR History — an auditable log of every screenshot uploaded through the
photo→assign flows (New Assignment and Quick Assign): the image itself, the
reference number(s) extracted from it, which valuer was assigned (or queued),
and when / by whom.

Design:
- The **image** goes to MinIO (minio_store.py) under screenshots/<id>.jpg.
- The **metadata** goes to saved_ocr_audit.json (one record per uploaded
  photo): id, timestamp, uploader, channel, ocr_source, the MinIO key, the
  valuer, and per-ref assignment outcomes. MinIO holds blobs; the JSON holds
  the metadata + the key linking them (mirrors the codebase's JSON-store
  convention).
- **Lifecycle:** a repeating cleanup job deletes a screenshot's MinIO blob
  once every ref on it reaches a completed/returned status in the consolidated
  DLV store (dlv_core) — or after a hard max-age — and marks the record
  image_purged. The metadata row (who/what/when) is always kept; only the
  image is removed, so completed work stops holding PII screenshots.

Access (PII — enforced, not just menu-hidden): 🖼 OCR History lives under
📈 Reports & Analytics, but cmd_ocr_history explicitly allows only admins or a
user granted that specific button (button_allowed, from the per-button access
feature). An admin delegates it via 🔐 Manage Access → grant Reports &
Analytics → fine-tune buttons → check only 🖼 OCR History.

Call register(app) from bot.py's main() to wire the viewer + cleanup job.
"""

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Dict, List, Optional

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

import minio_store
from common import (
    BTN_CAT_ANALYTICS,
    BTN_OCR_HISTORY,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _main_menu_for,
    allowed,
    button_allowed,
    cmd_cancel,
    deny,
    fallback,
    is_admin,
    load_user_names,
    logger,
)

SAVED_OCR_AUDIT_FILE = os.path.join(DATA_DIR, "saved_ocr_audit.json")

_AUDIT_KEEP = 1000              # cap on metadata records (newest kept)
_IMAGE_MAX_AGE_DAYS = 30        # purge a blob after this long regardless of status
_OH_PAGE_SIZE = 8               # history rows per page
_DONE_STATUSES = {"completed", "returned"}   # a ref is "done" at either
_CLEANUP_INTERVAL_SECS = 3600   # hourly lifecycle sweep


# ──────────────────────────────────────────────────────────
# Storage
# ──────────────────────────────────────────────────────────

def load_ocr_audit() -> List[Dict]:
    """Read saved_ocr_audit.json — the list of upload metadata records
    (newest last). Missing/corrupt file → empty list."""
    try:
        with open(SAVED_OCR_AUDIT_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_ocr_audit(records: List[Dict]) -> None:
    """Persist the audit-records list (trimmed to the newest _AUDIT_KEEP)."""
    _atomic_json_write(SAVED_OCR_AUDIT_FILE, records[-_AUDIT_KEEP:], indent=2)


def record_upload(photo_bytes: bytes, *, channel: str, uploaded_by: int,
                  ocr_source: str, valuer_name: str, valuer_uid: str,
                  assignments: List[Dict]) -> Optional[Dict]:
    """Store one uploaded screenshot: upload the image to MinIO and append a
    metadata record. `assignments` is [{ref, mode}] (mode: assigned/queued/
    failed/pending). Blocking (MinIO put + file write) — call via
    asyncio.to_thread. Returns the stored record (or None if there's nothing
    to record). Never raises on a MinIO hiccup; the metadata still saves."""
    refs = [a["ref"] for a in assignments]
    if not refs:
        return None   # nothing extracted → nothing worth auditing
    rec_id = uuid.uuid4().hex[:12]
    key = f"screenshots/{rec_id}.jpg"
    stored = minio_store.put_screenshot(key, photo_bytes)
    names = load_user_names()
    record = {
        "id": rec_id,
        "at": datetime.now().isoformat(timespec="seconds"),
        "uploaded_by": uploaded_by,
        "uploaded_by_name": names.get(str(uploaded_by), str(uploaded_by)),
        "channel": channel,
        "ocr_source": ocr_source,
        "minio_key": key if stored else None,
        "image_stored": bool(stored),
        "image_purged": False,
        "valuer_name": valuer_name,
        "valuer_uid": valuer_uid,
        "refs": refs,
        "assignments": assignments,
    }
    records = load_ocr_audit()
    records.append(record)
    save_ocr_audit(records)
    return record


# ──────────────────────────────────────────────────────────
# Lifecycle cleanup (completed refs → purge the blob)
# ──────────────────────────────────────────────────────────

def _ref_status_map() -> Dict[str, str]:
    """ref → current status from the consolidated DLV store. Best-effort: an
    import/read failure yields an empty map (nothing is treated as done, so
    images are kept rather than wrongly purged)."""
    try:
        import dlv_core
        return {r.get("ref"): r.get("status") for r in dlv_core.load_records_where()}
    except Exception as e:
        logger.warning("OCR audit: could not read DLV statuses: %s", e)
        return {}


def _record_is_old(record: Dict, now: datetime) -> bool:
    """True if the record's upload time is older than the max-age cap."""
    try:
        return datetime.fromisoformat(record["at"]) < now - timedelta(days=_IMAGE_MAX_AGE_DAYS)
    except (ValueError, KeyError):
        return False


def _eligible_for_purge(record: Dict, status_map: Dict[str, str], now: datetime):
    """Decide whether a record's stored image should be purged, returning a
    reason string ("completed" / "max-age") or None to keep it. All refs
    done → purge; else purge only once past the max age."""
    if not record.get("image_stored") or record.get("image_purged") or not record.get("minio_key"):
        return None
    refs = record.get("refs", [])
    all_done = bool(refs) and all(status_map.get(r) in _DONE_STATUSES for r in refs)
    if all_done:
        return "completed"
    if _record_is_old(record, now):
        return "max-age"
    return None


async def _ocr_audit_cleanup_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly sweep: purge the MinIO blob of any record whose refs are all
    completed/returned (or that's past the max age), keeping the metadata."""
    records = load_ocr_audit()
    if not records:
        return
    status_map = _ref_status_map()
    now = datetime.now()
    changed = False
    for rec in records:
        reason = _eligible_for_purge(rec, status_map, now)
        if not reason:
            continue
        if minio_store.delete_screenshot(rec["minio_key"]):
            rec["image_purged"] = True
            rec["purged_at"] = now.isoformat(timespec="seconds")
            rec["purge_reason"] = reason
            changed = True
            logger.info("OCR audit: purged screenshot %s (%s)", rec["id"], reason)
    if changed:
        save_ocr_audit(records)


# ──────────────────────────────────────────────────────────
# Viewer — 🖼 OCR History
# ──────────────────────────────────────────────────────────
class OH(Enum):
    LIST = auto()   # browsing the history list (paginate / view a photo)


def _oh_can_view(user_id: int) -> bool:
    """True if user_id may open OCR History — admins always, otherwise only if
    granted the specific button (per-button access). PII, so this is enforced
    here, not just hidden from the menu."""
    return is_admin(user_id) or button_allowed(user_id, BTN_CAT_ANALYTICS, BTN_OCR_HISTORY)


def _oh_row_summary(idx: int, rec: Dict) -> str:
    """One-line summary of a history record for the list view."""
    refs = ", ".join(rec.get("refs", [])) or "—"
    img = "" if rec.get("image_stored") and not rec.get("image_purged") else " 🚫img"
    return (f"{idx}. {rec.get('at', '?')} · {rec.get('uploaded_by_name', '?')} · "
            f"{refs} → {rec.get('valuer_name', '?')}{img}")


def _oh_render_page(records: List[Dict], page: int):
    """Build (text, keyboard) for a page of history (newest first). Each row
    gets a 🖼 View button; prev/next paginate; Close ends."""
    ordered = list(reversed(records))   # newest first
    total = len(ordered)
    pages = max(1, (total + _OH_PAGE_SIZE - 1) // _OH_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _OH_PAGE_SIZE
    chunk = ordered[start:start + _OH_PAGE_SIZE]

    lines = [f"🖼 *OCR History* — {total} screenshot(s) (page {page + 1}/{pages})", ""]
    rows = []
    for i, rec in enumerate(chunk, start=start + 1):
        lines.append(_oh_row_summary(i, rec))
        rows.append([InlineKeyboardButton(f"🖼 View #{i}", callback_data=f"ocrh:view:{rec['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"ocrh:page:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"ocrh:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🛑 Close", callback_data="ocrh:close")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _oh_caption(rec: Dict) -> str:
    """Full caption shown with a viewed screenshot: date, uploader, source,
    valuer, and each ref's assignment mode."""
    lines = [
        f"🖼 {rec.get('at', '?')}",
        f"Uploaded by: {rec.get('uploaded_by_name', '?')}",
        f"Source: {rec.get('ocr_source', '?')} ({rec.get('channel', '?')})",
        f"Valuer: {rec.get('valuer_name', '?')}",
        "References:",
    ]
    for a in rec.get("assignments", []):
        lines.append(f"  • {a.get('ref')} — {a.get('mode')}")
    return "\n".join(lines)


async def cmd_ocr_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point (/ocrhistory or the 🖼 OCR History button) — access-checked
    (admin or granted the button). Shows the paginated screenshot history."""
    if not allowed(update):
        return await deny(update)
    if not _oh_can_view(update.effective_user.id):
        await update.message.reply_text(
            "⛔ You don't have access to OCR History. Ask an admin to grant it "
            "via 🔐 Manage Access.",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END
    records = load_ocr_audit()
    if not records:
        await update.message.reply_text(
            "🖼 *OCR History*\n\nNo screenshots logged yet. They appear here once "
            "photos are processed through New Assignment or Quick Assign.",
            parse_mode="Markdown",
            reply_markup=_main_menu_for(update.effective_user.id),
        )
        return ConversationHandler.END
    ctx.user_data["oh_page"] = 0
    text, kb = _oh_render_page(records, 0)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return OH.LIST


async def recv_oh_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the history list buttons: paginate, view a screenshot, or close."""
    query = update.callback_query
    await query.answer()
    if not _oh_can_view(query.from_user.id):
        return ConversationHandler.END
    parts = query.data.split(":")
    action = parts[1]

    if action == "close":
        await query.edit_message_reply_markup(reply_markup=None)
        return ConversationHandler.END

    if action == "page":
        page = int(parts[2])
        ctx.user_data["oh_page"] = page
        text, kb = _oh_render_page(load_ocr_audit(), page)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return OH.LIST

    if action == "view":
        rec_id = parts[2]
        rec = next((r for r in load_ocr_audit() if r.get("id") == rec_id), None)
        if not rec:
            await query.message.reply_text("⚠️ That record no longer exists.")
            return OH.LIST
        caption = _oh_caption(rec)
        img = minio_store.get_screenshot(rec["minio_key"]) if rec.get("minio_key") and not rec.get("image_purged") else None
        if img:
            import io
            await query.message.reply_photo(photo=io.BytesIO(img), caption=caption)
        else:
            note = ("📭 _Screenshot purged (refs completed)._" if rec.get("image_purged")
                    else "📭 _Screenshot not stored (MinIO was unavailable)._")
            await query.message.reply_text(f"{note}\n\n{caption}", parse_mode="Markdown")
        return OH.LIST

    return OH.LIST


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the OCR History viewer conversation and the hourly MinIO-cleanup
    job into the given Application."""
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("ocrhistory", cmd_ocr_history),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_OCR_HISTORY)}$"), cmd_ocr_history),
        ],
        states={
            OH.LIST: [CallbackQueryHandler(recv_oh_action, pattern=r"^ocrh:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conv)
    app.job_queue.run_repeating(
        _ocr_audit_cleanup_job,
        interval=_CLEANUP_INTERVAL_SECS,
        first=_CLEANUP_INTERVAL_SECS,
        name="ocr_audit_cleanup",
    )
