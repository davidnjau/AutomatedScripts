#!/usr/bin/env python3
"""
dlv_core.py
===========
Shared "DLV domain" layer used by both the DLV Batch and DLV Tasks
features (and reusable by others, e.g. Auto Fetch, that need to know
whether a reference number is already queued):

- The persistent queue/closed-store files (`saved_dlv_batch.json`,
  `saved_dlv_closed.json`) and their load/save helpers. As of the Group A
  JSON consolidation, both are backed by one ref-keyed store,
  `saved_dlv_records.json` (`_load_consolidated`/`_save_consolidated`),
  with a `status` field (`queued`/`assigned`/`completed`/`returned`/
  `removed`) replacing "which file is this ref in." `load_dlv_batch`/
  `save_dlv_batch`/`load_dlv_closed`/`_append_dlv_closed` keep their exact
  old signatures and return shapes as adapters over that store, so every
  other module's call sites are unaffected. `common.py`'s
  `load_saved_assignments`/`persist_assignment` are adapters over the same
  store too (imported locally inside those two functions, not at module
  level, to avoid a circular import — dlv_core.py already imports from
  common.py). `hold_tasks.py`'s `load_hold_tasks`/`save_hold_tasks` are
  adapters as well, projecting/merging against each record's `hold`
  sub-object (imported at module level there, since that direction —
  hold_tasks.py importing dlv_core.py — already existed and creates no
  cycle). The legacy files are migrated into the consolidated store once,
  on first read, and left on disk untouched.
- Live search + classification against the two stages a stamp-duty
  reference can be at: the assessor/HQ stage (stampdutyservice) and the
  DLV stage (valuationservice).
- DLV_TAGS (the fixed Queue/Direct vocabulary) and the incremental-tag
  sentinel/parser (INCREMENTAL_TAG_SENTINEL/parse_incremental_tag/
  is_incremental_tag) — dlv_incremental.py's auto-sequenced "B{n}-T{n}"
  tags live here rather than in dlv_incremental.py or dlv_tasks.py
  because both of those need them and dlv_incremental.py already imports
  from dlv_tasks.py, so dlv_tasks.py importing back would cycle.

Moved out of bot.py so DLV Batch and DLV Tasks can each depend on this
one place instead of on each other.
"""

import json
import os
import re
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from ardhisasa_auth import AuthTokens, build_session

from common import (
    CPARAMS_DLV,
    DATA_DIR,
    SAVED_ASSIGNMENTS_FILE,
    _atomic_json_write,
    _ft_headers,
    logger,
)
from endpoints import (
    ASSESSOR_STAGE_DETAIL_URL,
    ASSESSOR_STAGE_LIST_URL,
    STAMP_DUTY_APPLICATION_DETAIL_URL,
    STAMP_DUTY_APPLICATION_LIST_URL,
)

SAVED_DLV_BATCH_FILE   = os.path.join(DATA_DIR, "saved_dlv_batch.json")
SAVED_DLV_CLOSED_FILE  = os.path.join(DATA_DIR, "saved_dlv_closed.json")
SAVED_DLV_RECORDS_FILE = os.path.join(DATA_DIR, "saved_dlv_records.json")
# Own copy of hold_tasks.py's legacy file path, needed only for migration —
# not imported from hold_tasks.py, since hold_tasks.py already imports from
# dlv_core.py at module level and the reverse would be a circular import.
SAVED_HOLD_TASKS_FILE = os.path.join(DATA_DIR, "saved_hold_tasks.json")

# Fields that only exist on the consolidated store's internal record shape —
# stripped out whenever a record is projected back to a legacy flat-item
# shape, so code that spreads `**item` downstream never picks up a ghost
# field left over from a different lifecycle stage.
_STORE_INTERNAL_FIELDS = {"status", "hold", "removed_at"}
# Additionally stripped only from the *batch* (queued) projection — these
# never existed on a saved_dlv_batch.json item and only appear once a ref
# has since been assigned/closed.
_CLOSE_AND_ASSIGN_FIELDS = {
    "assigned_at", "closed_reason", "application_status", "node",
    "request_type", "consideration_amount", "closed_at",
}

# Fixed tag vocabulary — DLV Batch lets you tag each queued ref with one of
# these (optional), and DLV Tasks' "By Tag" report filters on it. Kept as a
# closed list (rather than free text) so tags stay consistent and filterable.
# Shared here since both DLV Batch (sets the tag) and DLV Tasks (reports on
# it) need the same vocabulary.
DLV_TAGS = ["Queue", "Direct"]

# dlv_incremental.py's auto-sequenced "B{batch}-T{task}" tag is a third DLV
# Batch tag option that doesn't fit DLV_TAGS' fixed-vocabulary picker (it's
# unique per ref, not one of a small filterable set). The sentinel/parser
# live here — rather than in dlv_incremental.py or dlv_tasks.py — since both
# of those modules need them and dlv_incremental.py already imports from
# dlv_tasks.py (_dt_gather_report_data et al.), so dlv_tasks.py importing
# back from dlv_incremental.py would be a circular import.
#
# Two distinct uses of the same sentinel:
#   - dlv_batch.py's Tag Tasks picker: "generate a new incremental tag for
#     this ref" (resolved to a real "B{n}-T{n}" value at confirm time).
#   - dlv_tasks.py's By Tag picker: "show every ref whose tag is ANY
#     incremental tag together" (a filter, not a value to assign).
INCREMENTAL_TAG_SENTINEL = "__incremental__"

_INCREMENTAL_TAG_RE = re.compile(r"^B(\d+)-T(\d+)$")


def parse_incremental_tag(tag: Optional[str]) -> Optional[Tuple[int, int]]:
    """(batch_number, task_number) parsed from a "B{n}-T{n}" tag, or None
    if tag is empty or doesn't match (e.g. a fixed Queue/Direct tag)."""
    m = _INCREMENTAL_TAG_RE.match(tag or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def is_incremental_tag(tag: Optional[str]) -> bool:
    """True if tag is a "B{n}-T{n}" auto-sequenced incremental tag."""
    return parse_incremental_tag(tag) is not None


# ──────────────────────────────────────────────────────────
# Consolidated ref-keyed store — the one saved_dlv_records.json, with
# load_dlv_batch/save_dlv_batch/load_dlv_closed/_append_dlv_closed below
# acting as adapters over it that preserve their original signatures.
# ──────────────────────────────────────────────────────────

def _load_consolidated() -> Dict[str, Dict]:
    """The single ref-keyed DLV lifecycle store. Migrates it once from the
    legacy saved_assignments.json/saved_dlv_batch.json/saved_dlv_closed.json
    files the first time saved_dlv_records.json is missing or unreadable;
    all legacy files are left on disk, untouched, per this codebase's
    existing migration convention (e.g. Auto Fetch's bare-dict→list
    migration)."""
    try:
        with open(SAVED_DLV_RECORDS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    merged = _migrate_legacy_stores()
    _save_consolidated(merged)
    return merged


def _save_consolidated(store: Dict[str, Dict]) -> None:
    """Atomically write the consolidated ref-keyed store."""
    _atomic_json_write(SAVED_DLV_RECORDS_FILE, store, indent=2)


_EMPTY_VALUES = (None, "", [], {})


def _merge_enrich(existing: Dict, new_fields: Dict) -> Dict:
    """Merge new_fields onto existing, field by field — a field is only
    overwritten if the incoming value is non-empty, or the field doesn't
    exist yet on `existing`. Used specifically for enriching one ref's
    record from multiple legacy sources describing it independently (the
    migration below), so a source that simply never captured a field
    (empty string/None/[]/{})) can't blow away a value an earlier, richer
    source already provided — "enrich with whichever source has more
    data." Not used for live-app upserts (save_dlv_batch/persist_assignment/
    etc.), where an explicitly-passed empty value is a real, current fact
    about that ref, not a gap in a historical snapshot."""
    merged = dict(existing)
    for k, v in new_fields.items():
        if k not in merged or v not in _EMPTY_VALUES:
            merged[k] = v
    return merged


def _migrate_legacy_stores() -> Dict[str, Dict]:
    """One-time rebuild of the consolidated store from all four legacy
    files. Merge order matters: assignments is the base layer (the
    bot-wide ledger), then queued, then closed — each step's `status` wins
    over the previous for the same ref since it's more-progressed, even
    though in practice a ref shouldn't appear in more than one legacy file
    at once — but any field a later step doesn't actually know (empty/
    missing) never erases a value an earlier step already captured (see
    _merge_enrich). Hold items are folded in last, as a `hold` sub-object
    attached onto whatever record already exists for that ref (or a bare
    freshly-created one, since a held ref can come straight from a live
    DLV query this bot never itself assigned)."""
    merged: Dict[str, Dict] = {}

    try:
        with open(SAVED_ASSIGNMENTS_FILE) as f:
            assignment_items = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        assignment_items = {}
    for ref, info in assignment_items.items():
        merged[ref] = _merge_enrich(merged.get(ref, {}), {**info, "ref": ref, "status": "assigned"})

    try:
        with open(SAVED_DLV_BATCH_FILE) as f:
            batch_items = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        batch_items = []
    for item in batch_items:
        ref = item.get("ref")
        if ref:
            merged[ref] = _merge_enrich(merged.get(ref, {}), {**item, "ref": ref, "status": "queued"})

    try:
        with open(SAVED_DLV_CLOSED_FILE) as f:
            closed_items = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        closed_items = []
    for item in closed_items:
        ref = item.get("ref")
        if ref:
            status = "completed" if item.get("closed_reason") == "completed" else "returned"
            merged[ref] = _merge_enrich(merged.get(ref, {}), {**item, "ref": ref, "status": status})

    try:
        with open(SAVED_HOLD_TASKS_FILE) as f:
            hold_items = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        hold_items = []
    for item in hold_items:
        ref = item.get("ref")
        if not ref:
            continue
        hold_fields = {k: v for k, v in item.items() if k != "ref"}
        existing = merged.get(ref, {"ref": ref, "status": "assigned"})
        merged[ref] = {**existing, "hold": hold_fields}

    return merged


def _project(record: Dict, extra_strip: Iterable[str] = ()) -> Dict:
    """A store record, projected back to a legacy flat-item shape — drops
    internal-only keys (plus whatever extra_strip names) so callers that
    spread `**item` downstream never leak a field left over from a
    different lifecycle stage."""
    drop = _STORE_INTERNAL_FIELDS | set(extra_strip)
    return {k: v for k, v in record.items() if k not in drop}


def load_dlv_batch() -> List[Dict]:
    """Every ref currently status=="queued", projected to the legacy
    saved_dlv_batch.json item shape."""
    store = _load_consolidated()
    return [
        _project(r, _CLOSE_AND_ASSIGN_FIELDS)
        for r in store.values() if r.get("status") == "queued"
    ]


def save_dlv_batch(items: List[Dict]) -> None:
    """Upsert every item as status="queued", merged onto whatever the store
    already knows about that ref. Deliberately never touches a ref that's
    currently "queued" in the store but absent from `items` — by the time a
    call site drops a ref from its list, something else this same cycle
    (persist_assignment/_append_dlv_closed/mark_removed) already set, or is
    about to set, that ref's real new status; guessing here risks clobbering
    it, so save_dlv_batch only ever adds/refreshes, never removes."""
    store = _load_consolidated()
    for item in items:
        ref = item.get("ref")
        if ref:
            store[ref] = {**store.get(ref, {}), **item, "ref": ref, "status": "queued"}
    _save_consolidated(store)


def clear_dlv_batch() -> None:
    """Mark every currently-queued ref removed (bulk clear). Not called
    anywhere in the live bot today, but must still leave a status="removed"
    trace rather than silently erasing records."""
    store = _load_consolidated()
    mark_removed([ref for ref, r in store.items() if r.get("status") == "queued"])


def load_dlv_closed() -> List[Dict]:
    """Every ref currently status in (completed, returned), projected to the
    legacy saved_dlv_closed.json item shape."""
    store = _load_consolidated()
    return [
        _project(r, {"assigned_at"})
        for r in store.values() if r.get("status") in ("completed", "returned")
    ]


def save_dlv_closed(items: List[Dict]) -> None:
    """Upsert every item as its own closed status, merged onto whatever the
    store already knows about that ref. No current call site uses this
    directly (only _append_dlv_closed does) — kept for API parity."""
    store = _load_consolidated()
    for item in items:
        ref = item.get("ref")
        if ref:
            status = "completed" if item.get("closed_reason") == "completed" else "returned"
            store[ref] = {**store.get(ref, {}), **item, "ref": ref, "status": status}
    _save_consolidated(store)


def _append_dlv_closed(item: Dict) -> None:
    """Move a ref into the closed store, merging onto (not replacing) any
    prior record for the same ref so earlier-known fields survive."""
    save_dlv_closed([item])


def mark_removed(refs: Iterable[str]) -> None:
    """Mark each ref "removed" (manually dropped from the queue, or released
    from hold — see clear_hold_and_remove) rather than deleting its record
    outright — keeps a removed_at trace instead of silently losing history.
    No-op for a ref not currently in the store."""
    store = _load_consolidated()
    now = datetime.now().isoformat(timespec="seconds")
    changed = False
    for ref in refs:
        if ref in store:
            store[ref] = {**store[ref], "status": "removed", "removed_at": now}
            changed = True
    if changed:
        _save_consolidated(store)


def clear_hold_and_remove(refs: Iterable[str]) -> None:
    """Release each ref from its hold guard AND mark it "removed" in the
    same write. Releasing a hold and manually deleting a queued ref are the
    same kind of event (a tracking-queue exit, not a lifecycle change), so
    they share the terminal "removed" status mark_removed also uses. No-op
    for a ref not currently in the store."""
    store = _load_consolidated()
    now = datetime.now().isoformat(timespec="seconds")
    changed = False
    for ref in refs:
        if ref in store:
            store[ref] = {**store[ref], "hold": None, "status": "removed", "removed_at": now}
            changed = True
    if changed:
        _save_consolidated(store)


# Status filters to probe per DLV request type — a queued ref's current status
# isn't known in advance, so every relevant filter is tried until it's found.
_DLV_COUNTY_FILTERS    = ["Ongoing", "Completed", "Returned"]
_DLV_NONCOUNTY_FILTERS = ["Pending", "Ongoing", "Completed", "Returned", "On-hold", "Cancelled"]


def _dlv_request_type(ref: str) -> str:
    return "COUNTY_STAMP_DUTY" if ref.upper().startswith("CNTYINV") else "STAMP_DUTY"


def _search_ref_dlv(tokens: AuthTokens, ref: str) -> Optional[Dict]:
    """
    Search for a reference number via the DLV endpoint, trying every status
    filter relevant to the ref's request type (County vs non-County) until
    found — a ref's current status isn't known ahead of the search, and
    checking a single filter misses refs that have moved on (e.g. Completed).
    Returns the task dict (tagged with "_request_type") or None.
    """
    request_type = _dlv_request_type(ref)
    filters = _DLV_COUNTY_FILTERS if request_type == "COUNTY_STAMP_DUTY" else _DLV_NONCOUNTY_FILTERS
    http_sess = build_session()
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
        "cparams":       CPARAMS_DLV,
    }
    for status_filter in filters:
        params = {
            "filter": status_filter, "role": "DLV", "request_type": request_type,
            "search": ref, "page": 1,
        }
        if request_type == "COUNTY_STAMP_DUTY":
            params["from_ardhipay"] = "true"   # required for county results, per dlv_batch.md
        try:
            resp = http_sess.get(
                STAMP_DUTY_APPLICATION_LIST_URL,
                headers=headers,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            for task in resp.json().get("results", []):
                if task.get("reference_number") == ref:
                    task["_request_type"] = request_type
                    return task
        except Exception as e:
            logger.warning("DLV search failed for %s (%s/%s): %s", ref, request_type, status_filter, e)
    return None


def _fetch_ref_detail_dlv(tokens: AuthTokens, request_id: str) -> Optional[Dict]:
    """Fetch the detail view for a DLV application."""
    http_sess = build_session()
    resp = http_sess.get(
        STAMP_DUTY_APPLICATION_DETAIL_URL,
        headers={
            "Authorization": f"Bearer {tokens.access_token}",
            "JWTAUTH":       f"Bearer {tokens.jwt}",
            "cparams":       CPARAMS_DLV,
        },
        params={"request_id": request_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _classify_dlv_detail(detail: Dict) -> Dict:
    """
    Classify a DLV application detail-view response per the rules in dlv_batch.md:
      - node COMPLETED + status COMPLETED  -> closed (completed)
      - status RETURNED + node CREATED     -> closed (returned)
      - status ONGOING  + node CREATED     -> open (still available for reallocation)
      - anything else                      -> open (in-progress / unclassified, keep tracking)
    """
    application_status = detail.get("application_status", "")
    node                = detail.get("node", "")
    ext                 = detail.get("external_process_details") or {}
    assessor            = detail.get("assessor") or {}
    assessor_ud         = assessor.get("user_details") or {}
    actors              = detail.get("actors") or []
    actor_name          = actors[0].get("user_details", {}).get("names", "") if actors else ""

    if node == "VALUATION_STAMP_DUTY_COMPLETED" and application_status == "COMPLETED":
        bucket, closed_reason = "closed", "completed"
    elif application_status == "RETURNED" and node == "VALUATION_STAMP_DUTY_CREATED":
        bucket, closed_reason = "closed", "returned"
    else:
        bucket, closed_reason = "open", ""

    return {
        "bucket":               bucket,
        "closed_reason":        closed_reason,
        "application_status":   application_status,
        "node":                 node,
        "assessor_role":        assessor.get("role", ""),
        "assessor_name":        assessor_ud.get("names", ""),
        "consideration_amount": ext.get("consideration_amount", ""),
        "currency_code":        ext.get("currency_code", ""),
        "actor_name":           actor_name,
    }


def _search_ref_stampduty(tokens: AuthTokens, ref: str) -> Optional[Dict]:
    """
    Fallback search for a ref not yet visible in DLV — checks the assessor/HQ
    stage instead, via the same stampdutyservice hod-or-clr endpoint Fetch
    Tasks uses (fetch_tasks.md). Tries both the HQ (digitised) and County
    (from_ardhipay) variants since the ref alone doesn't tell us which.
    """
    http_sess = build_session()
    headers = _ft_headers(tokens)
    for params in (
        {"filter": "Ongoing", "page": 1, "search": ref},
        {"filter": "Ongoing", "from_ardhipay": "true", "page": 1, "search": ref},
    ):
        try:
            resp = http_sess.get(
                ASSESSOR_STAGE_LIST_URL,
                headers=headers, params=params, timeout=30,
            )
            resp.raise_for_status()
            for task in resp.json().get("results", []):
                if task.get("reference_number") == ref:
                    return task
        except Exception as e:
            logger.warning("Assessor-stage search failed for %s: %s", ref, e)
    return None


def _fetch_stampduty_detail(tokens: AuthTokens, request_id: str) -> Optional[Dict]:
    """Detail-view for an assessor-stage (stampdutyservice) task."""
    http_sess = build_session()
    try:
        resp = http_sess.get(
            ASSESSOR_STAGE_DETAIL_URL,
            headers=_ft_headers(tokens),
            params={"request_id": request_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("details", {})
    except Exception as e:
        logger.warning("Assessor-stage detail failed for %s: %s", request_id, e)
        return None


def _extract_assessor(officers: List[Dict]) -> str:
    """Pick the ASSESSOR_OF_STAMP_DUTY officer's name out of a detail-view officers list."""
    return next(
        (o.get("name", "") for o in officers if o.get("role") == "ASSESSOR_OF_STAMP_DUTY"),
        "",
    )


def _resolve_assessor(officers: List[Dict]) -> str:
    """Prefer the ASSESSOR_OF_STAMP_DUTY officer's name (_extract_assessor);
    if no officer holds that exact role — e.g. a County ref's officer is a
    COUNTY_REGISTRAR instead — fall back to listing whoever IS in the
    officers list as "Name (ROLE)", rather than reporting a known name as
    unknown just because the role label didn't match.

    This is the single source of truth for a task's "assessor" field: it
    must be resolved here, at data-fetch time (fetch_tasks.py's
    _load_fetch_tasks), not at display time — the resolved value gets
    cached (fetch_tasks_cache.py) and copied onto DLV Batch queue items,
    and neither of those downstream consumers has access to the raw
    officers list to fall back on later."""
    assessor = _extract_assessor(officers)
    if assessor:
        return assessor
    return ", ".join(f"{o.get('name', '')} ({o.get('role', '')})" for o in officers if o.get("name"))
