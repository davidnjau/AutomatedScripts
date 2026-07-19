#!/usr/bin/env python3
"""
dlv_core.py
===========
Shared "DLV domain" layer used by both the DLV Batch and DLV Tasks
features (and reusable by others, e.g. Auto Fetch, that need to know
whether a reference number is already queued):

- The persistent queue/closed-store files (`saved_dlv_batch.json`,
  `saved_dlv_closed.json`) and their load/save helpers.
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
from typing import Dict, List, Optional, Tuple

from ardhisasa_auth import AuthTokens, build_session

from common import (
    CPARAMS_DLV,
    DATA_DIR,
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

SAVED_DLV_BATCH_FILE  = os.path.join(DATA_DIR, "saved_dlv_batch.json")
SAVED_DLV_CLOSED_FILE = os.path.join(DATA_DIR, "saved_dlv_closed.json")

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
# DLV Batch — persistent queue helpers
# ──────────────────────────────────────────────────────────

def load_dlv_batch() -> List[Dict]:
    try:
        with open(SAVED_DLV_BATCH_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_dlv_batch(items: List[Dict]) -> None:
    _atomic_json_write(SAVED_DLV_BATCH_FILE, items, indent=2)


def clear_dlv_batch() -> None:
    save_dlv_batch([])


def load_dlv_closed() -> List[Dict]:
    try:
        with open(SAVED_DLV_CLOSED_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_dlv_closed(items: List[Dict]) -> None:
    _atomic_json_write(SAVED_DLV_CLOSED_FILE, items, indent=2)


def _append_dlv_closed(item: Dict) -> None:
    """Move a ref into the closed store, replacing any prior record for the same ref."""
    closed = [c for c in load_dlv_closed() if c.get("ref") != item.get("ref")]
    closed.append(item)
    save_dlv_closed(closed)


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
