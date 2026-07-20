#!/usr/bin/env python3
"""
consolidate_dlv_records.py
===========================
One-time server-side migration: merges the four legacy JSON stores —
saved_dlv_batch.json, saved_dlv_closed.json, saved_assignments.json,
saved_hold_tasks.json — into the single ref-keyed saved_dlv_records.json
store the bot's dlv_core.py now reads/writes (see dlv_core._load_consolidated).

Standalone: stdlib only, no bot dependencies (ardhisasa_auth/common/etc. are
NOT imported), so it can run from a bare python3 with no requirements.txt
install and no .env needed.

Merge behavior — "enrich with whichever source has more data": when the
same ref appears in more than one legacy file, a field is only overwritten
if the newer source's value is non-empty, or the field wasn't already
known. So a more-progressed file that simply never captured some field
(e.g. an empty "parcel") can't erase a value an earlier, richer source
already provided. status is still derived per source (assignments="assigned",
batch="queued", closed="completed"/"returned", each overriding the previous
for the same ref since it's more-progressed); hold items are attached last,
as a `hold` sub-object, onto whatever record already exists for that ref (or
a bare freshly-created one).

Usage — always run in this order:

    # 1. Dry run — prints a summary, writes nothing. Do this first.
    python3 consolidate_dlv_records.py --data-dir data

    # 2. Once the summary looks right, actually write saved_dlv_records.json.
    #    Safe to re-run: rebuilds from whichever legacy files still exist.
    python3 consolidate_dlv_records.py --data-dir data --apply

    # 3. Only after confirming the bot works correctly against the new file
    #    (📋 DLV Tasks / 📥 DLV Batch / ✋ Hold Tasks / 📜 Assignments all
    #    still look right), remove the four legacy files. By default they
    #    are moved into a timestamped backup folder, not unlinked outright —
    #    pass --no-backup for a true delete with no recovery copy.
    python3 consolidate_dlv_records.py --data-dir data --delete-legacy
    python3 consolidate_dlv_records.py --data-dir data --delete-legacy --no-backup

On this bot's Docker deployment, data/ lives inside the named `bot_data`
volume (mounted at /app/data), not on the host filesystem — copy this
script into the running container first:

    docker cp consolidate_dlv_records.py ardhisasa_bot:/app/consolidate_dlv_records.py
    docker-compose exec ardhisasa-bot python3 consolidate_dlv_records.py --data-dir data
    docker-compose exec ardhisasa-bot python3 consolidate_dlv_records.py --data-dir data --apply
    docker-compose exec ardhisasa-bot python3 consolidate_dlv_records.py --data-dir data --delete-legacy
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime

LEGACY_FILES = {
    "assignments": "saved_assignments.json",
    "batch":       "saved_dlv_batch.json",
    "closed":      "saved_dlv_closed.json",
    "hold":        "saved_hold_tasks.json",
}
RECORDS_FILE = "saved_dlv_records.json"

_EMPTY_VALUES = (None, "", [], {})


def _load_json(path, default):
    """Read a legacy JSON file, or return `default` if it's missing/unreadable."""
    if not os.path.exists(path):
        return default
    with open(path) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            print(f"WARNING: {path}: invalid JSON ({e}) — treating as empty", file=sys.stderr)
            return default


def _merge_enrich(existing, new_fields):
    """Merge new_fields onto existing, field by field — overwrite only if
    the incoming value is non-empty or the field doesn't exist yet, so a
    less-complete source never erases a more-complete earlier one."""
    merged = dict(existing)
    for k, v in new_fields.items():
        if k not in merged or v not in _EMPTY_VALUES:
            merged[k] = v
    return merged


def build_consolidated_store(data_dir):
    """Read all four legacy files under data_dir and return
    (merged_store, per_file_record_counts, resolved_file_paths)."""
    paths = {key: os.path.join(data_dir, name) for key, name in LEGACY_FILES.items()}
    counts = {key: 0 for key in LEGACY_FILES}
    merged = {}

    assignments = _load_json(paths["assignments"], {})
    for ref, info in assignments.items():
        merged[ref] = _merge_enrich(merged.get(ref, {}), {**info, "ref": ref, "status": "assigned"})
        counts["assignments"] += 1

    batch_items = _load_json(paths["batch"], [])
    for item in batch_items:
        ref = item.get("ref")
        if not ref:
            continue
        merged[ref] = _merge_enrich(merged.get(ref, {}), {**item, "ref": ref, "status": "queued"})
        counts["batch"] += 1

    closed_items = _load_json(paths["closed"], [])
    for item in closed_items:
        ref = item.get("ref")
        if not ref:
            continue
        status = "completed" if item.get("closed_reason") == "completed" else "returned"
        merged[ref] = _merge_enrich(merged.get(ref, {}), {**item, "ref": ref, "status": status})
        counts["closed"] += 1

    hold_items = _load_json(paths["hold"], [])
    for item in hold_items:
        ref = item.get("ref")
        if not ref:
            continue
        hold_fields = {k: v for k, v in item.items() if k != "ref"}
        existing = merged.get(ref, {"ref": ref, "status": "assigned"})
        merged[ref] = {**existing, "hold": hold_fields}
        counts["hold"] += 1

    return merged, counts, paths


def _print_summary(data_dir, merged, counts, paths):
    status_counts = {}
    for r in merged.values():
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    held_count = sum(1 for r in merged.values() if r.get("hold"))

    print(f"\nLegacy files read from {data_dir}:")
    for key, name in LEGACY_FILES.items():
        marker = "found" if os.path.exists(paths[key]) else "missing, skipped"
        print(f"  {name:<28} {marker:<18} {counts[key]} record(s)")

    print(f"\nConsolidated store: {len(merged)} unique ref(s)")
    for status in ("queued", "assigned", "completed", "returned", "removed"):
        if status in status_counts:
            print(f"  status={status:<10} {status_counts.pop(status)}")
    for status, count in sorted(status_counts.items()):
        print(f"  status={status:<10} {count}")
    print(f"  held (hold sub-object set): {held_count}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data-dir", default="data", help="Path to the bot's data/ directory (default: ./data)")
    parser.add_argument("--apply", action="store_true", help="Actually write saved_dlv_records.json (default: dry run only)")
    parser.add_argument("--delete-legacy", action="store_true", help="Remove the 4 legacy files (requires saved_dlv_records.json to already exist)")
    parser.add_argument("--no-backup", action="store_true", help="With --delete-legacy: unlink the legacy files outright instead of moving them to a backup folder")
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    if not os.path.isdir(data_dir):
        print(f"ERROR: {data_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    records_path = os.path.join(data_dir, RECORDS_FILE)

    if args.delete_legacy:
        if not os.path.exists(records_path):
            print(
                f"ERROR: {records_path} doesn't exist yet — run with --apply first, "
                "confirm the bot works correctly against it, then re-run with --delete-legacy.",
                file=sys.stderr,
            )
            sys.exit(1)
        backup_dir = os.path.join(data_dir, f"legacy_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        if not args.no_backup:
            os.makedirs(backup_dir, exist_ok=True)
        moved = []
        for key, name in LEGACY_FILES.items():
            path = os.path.join(data_dir, name)
            if not os.path.exists(path):
                continue
            if args.no_backup:
                os.remove(path)
            else:
                shutil.move(path, os.path.join(backup_dir, name))
            moved.append(name)
        if not moved:
            print("No legacy files were present — nothing to remove.")
        elif args.no_backup:
            print(f"Deleted (no backup): {', '.join(moved)}")
        else:
            print(f"Moved to {backup_dir}: {', '.join(moved)}")
        return

    merged, counts, paths = build_consolidated_store(data_dir)
    _print_summary(data_dir, merged, counts, paths)

    if not args.apply:
        print(f"\nDry run only — nothing written. Re-run with --apply to write {records_path}.")
        return

    if os.path.exists(records_path):
        backup_path = records_path + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(records_path, backup_path)
        print(f"\nExisting {RECORDS_FILE} backed up to {backup_path}")

    tmp_path = records_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp_path, records_path)
    print(f"\nWrote {records_path} ({len(merged)} ref(s)).")
    print("Legacy files were NOT modified. Once you've confirmed the bot works "
          "correctly against the new file, re-run with --delete-legacy.")


if __name__ == "__main__":
    main()
