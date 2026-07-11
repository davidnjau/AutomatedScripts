#!/usr/bin/env python3
"""
fetch_tasks_cache.py
=====================
Fetch Tasks log — short-lived assessor cache, keyed by ref.

Fetch Tasks sees the assessor while a ref is still upstream of DLV; by the
time a ref is added to the DLV batch (or the DLV Tasks report runs), the
live assessor/DLV searches sometimes come up empty. Caching what Fetch
Tasks last saw lets both flows fall back to it instead of showing "—".

Written by Fetch Tasks, read by DLV Batch and DLV Tasks.
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from common import DATA_DIR, _atomic_json_write

SAVED_FETCH_TASKS_LOG_FILE  = os.path.join(DATA_DIR, "fetch_tasks_log.json")
FETCH_TASKS_LOG_TTL_SECONDS = 86400  # 1 day — entries older than this are dropped on read


def load_fetch_tasks_log() -> Dict[str, Dict]:
    try:
        with open(SAVED_FETCH_TASKS_LOG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_fetch_tasks_log(log: Dict[str, Dict]) -> None:
    _atomic_json_write(SAVED_FETCH_TASKS_LOG_FILE, log, indent=2)


def _prune_fetch_tasks_log(log: Dict[str, Dict]) -> Dict[str, Dict]:
    cutoff = (datetime.now() - timedelta(seconds=FETCH_TASKS_LOG_TTL_SECONDS)).isoformat(timespec="seconds")
    return {ref: entry for ref, entry in log.items() if entry.get("cached_at", "") >= cutoff}


def _log_fetch_tasks(tasks: List[Dict]) -> None:
    """Cache ref -> assessor (+ context) from a Fetch Tasks run for later lookup."""
    if not tasks:
        return
    log = _prune_fetch_tasks_log(load_fetch_tasks_log())
    now = datetime.now().isoformat(timespec="seconds")
    for t in tasks:
        ref = t.get("reference_number", "")
        if not ref:
            continue
        log[ref] = {
            "assessor":     t.get("assessor", ""),
            "parcel":       t.get("parcel_number", ""),
            "registry":     t.get("registry", ""),
            "county":       t.get("county", ""),
            "date_created": t.get("date_created", ""),
            "cached_at":    now,
        }
    save_fetch_tasks_log(log)


def _fetch_tasks_log_lookup(ref: str) -> Optional[Dict]:
    """Return the cached Fetch Tasks entry for ref, or None if absent/expired."""
    return _prune_fetch_tasks_log(load_fetch_tasks_log()).get(ref)


def _fetch_tasks_log_remove(refs: List[str]) -> None:
    """Drop cached entries once their ref has been queued into the DLV batch."""
    if not refs:
        return
    log = load_fetch_tasks_log()
    if any(ref in log for ref in refs):
        for ref in refs:
            log.pop(ref, None)
        save_fetch_tasks_log(log)
