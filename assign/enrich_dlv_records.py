#!/usr/bin/env python3
"""
enrich_dlv_records.py
======================
One-time (or periodic) enrichment pass over the consolidated ref-keyed
saved_dlv_records.json store (see dlv_core.py's _load_consolidated): for
every "queued"/"assigned" ref missing registry/county/parcel/
consideration, look it up live — reusing lookup_reference.py's multi-stage
County/non-County search machinery — and merge whatever's found into the
record via dlv_core._merge_enrich, so an existing non-empty value is never
overwritten by an empty one.

Unlike consolidate_dlv_records.py (a pure file merge, stdlib only), this
script needs the bot's full runtime — get_valid_tokens()'s cached
credentials, live HTTP calls to the same endpoints Lookup Reference
uses — so run it from inside the bot's own environment, not copied out
standalone:

    docker-compose exec ardhisasa-bot python3 enrich_dlv_records.py
    docker-compose exec ardhisasa-bot python3 enrich_dlv_records.py --apply
    docker-compose exec ardhisasa-bot python3 enrich_dlv_records.py --ref REG/TSFR/ABC123 --apply
    docker-compose exec ardhisasa-bot python3 enrich_dlv_records.py --include-closed --apply
    docker-compose exec ardhisasa-bot python3 enrich_dlv_records.py --all --apply

By default only "queued"/"assigned" refs missing a field are targeted (the
currently-live ones, and only the ones that actually need it); --include-
closed also targets "completed"/"returned"; --all re-checks every targeted
ref even if it already has every field, so a live value can refresh a
stale one. "removed" refs are never targeted — they're inert history.

Requires Support Reg (for County refs' assessor/HQ stage) and/or Staff
Valuer (for everything else, and County refs' DLV/valuation-stage
fallback) tokens to already be cached — same as Lookup Reference itself;
a ref whose relevant credential isn't cached is simply skipped, not
treated as an error, so a partial token cache still enriches what it can.
"""

import argparse
import sys
import time

import dlv_core
import lookup_reference as lu
from common import get_valid_tokens

# The descriptive fields a live lookup can fill in — status/valuer/tag are
# deliberately not included here, since those are kept correct by the
# bot's normal queue/assign/close flow, not by this enrichment pass.
_ENRICH_FIELDS = ("registry", "county", "parcel", "consideration")


def _needs_enrichment(record: dict) -> bool:
    """True if any of the descriptive fields are missing or blank."""
    return any(not record.get(f) for f in _ENRICH_FIELDS)


def _lookup_context(ref: str) -> dict:
    """Live-lookup one ref and return its registry/county/parcel/
    consideration/currency_code (lu._lu_extract_context) — mirrors
    lookup_reference.py's recv_lu_ref/_lu_lookup_county stage order
    (County: assessor stage under Support Reg, then DLV/valuation stage
    under Staff Valuer; everything else: Staff Valuer only), but returns
    the raw context dict instead of a formatted Telegram message. Returns
    {} if the ref can't be found under any credential currently cached."""
    if lu._lu_is_county_ref(ref):
        support_tokens = get_valid_tokens(lu._LU_CRED_COUNTY)
        if support_tokens:
            item = lu._lu_search_ref_county(support_tokens, ref)
            if item:
                detail = lu._lu_fetch_detail_county(support_tokens, item["id"])
                return lu._lu_extract_context(item, detail)

        valuer_tokens = get_valid_tokens(lu._LU_CRED_DEFAULT)
        if valuer_tokens:
            item = lu._lu_search_ref_county_dlv(valuer_tokens, ref)
            if item:
                detail = lu._lu_fetch_detail(valuer_tokens, item["id"])
                return lu._lu_extract_context(item, detail)

        return {}

    tokens = get_valid_tokens(lu._LU_CRED_DEFAULT)
    if not tokens:
        return {}
    item = lu._lu_search_ref(tokens, ref)
    if not item:
        return {}
    detail = lu._lu_fetch_detail(tokens, item["id"])
    return lu._lu_extract_context(item, detail)


def _select_targets(store: dict, args) -> list:
    """Every ref to check this run: just --ref if given, else every ref
    whose status is targeted (queued/assigned, plus completed/returned
    if --include-closed) and that needs enrichment (unless --all)."""
    if args.ref:
        if args.ref not in store:
            print(f"ERROR: {args.ref} not found in the consolidated store", file=sys.stderr)
            sys.exit(1)
        return [args.ref]

    statuses = {"queued", "assigned"}
    if args.include_closed:
        statuses |= {"completed", "returned"}
    return [
        ref for ref, record in store.items()
        if record.get("status") in statuses and (args.all or _needs_enrichment(record))
    ]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="Actually write enriched fields back (default: dry run only)")
    parser.add_argument("--ref", default=None, help="Only check this one ref")
    parser.add_argument("--include-closed", action="store_true", help="Also target completed/returned refs, not just queued/assigned")
    parser.add_argument("--all", action="store_true", help="Re-check every targeted ref even if it already has every field")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds to sleep between live lookups (default: 0.3)")
    args = parser.parse_args()

    store = dlv_core._load_consolidated()
    targets = _select_targets(store, args)

    print(f"{len(targets)} ref(s) to check.")
    if not targets:
        return

    enriched, not_found, up_to_date = 0, 0, 0
    for i, ref in enumerate(targets, 1):
        context = _lookup_context(ref)
        if not context or not any(context.values()):
            not_found += 1
            print(f"[{i}/{len(targets)}] {ref}: not found live — left as-is")
        else:
            before = store[ref]
            after = dlv_core._merge_enrich(before, context)
            changed_fields = [k for k in after if before.get(k) != after.get(k)]
            if changed_fields:
                enriched += 1
                store[ref] = after
                print(f"[{i}/{len(targets)}] {ref}: enriched {changed_fields}")
            else:
                up_to_date += 1
                print(f"[{i}/{len(targets)}] {ref}: already up to date")
        if i < len(targets):
            time.sleep(args.delay)

    print(f"\n{enriched} ref(s) enriched, {not_found} not found live, {up_to_date} already up to date.")

    if not args.apply:
        print("\nDry run only — nothing written. Re-run with --apply to save.")
        return

    dlv_core._save_consolidated(store)
    print(f"\nSaved {dlv_core.SAVED_DLV_RECORDS_FILE}.")


if __name__ == "__main__":
    main()
