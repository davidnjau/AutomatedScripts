# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python Telegram bot that automates valuation officer assignment in the Ardhisasa Kenyan land valuation system. It interacts with the Ardhisasa API (`https://ardhisasa-api.lands.go.ke`) to search for valuers, bulk-assign reference numbers, and receive (pull) unassigned tasks to a valuer.

## Running the Bot

```bash
# Local development
pip install -r requirements.txt
python bot.py

# Docker (preferred for deployment)
docker-compose up --build -d
docker-compose logs -f ardhisasa-bot
docker-compose down

# Token refresh daemon (run separately or alongside the bot)
python token_refresh_daemon.py
```

Required environment variables (in `.env`):
- `TELEGRAM_BOT_TOKEN` — Telegram bot API token
- `ALLOWED_TELEGRAM_IDS` — Comma-separated whitelist of Telegram user IDs
- `ANTHROPIC_API_KEY` — Optional; required for Claude Vision OCR fallback on photo inputs

## Architecture

`bot.py` originally held every feature in one ~9,200-line file. It has been split into sibling modules that each own their conversation handlers and register themselves into `bot.py`'s `main()` via a `register(app)` function. The extraction is complete — `bot.py` is now a ~751-line thin orchestrator. See "Extraction history" below for how this was done and the reasoning behind each cross-module dependency.

**Going forward, every new feature is added as its own new module from day one — never written inline in `bot.py`.** `bot.py` should stay a thin orchestrator permanently. Concretely: a new feature gets its own `assign/<feature>.py` with that feature's state enum/session dataclass/handlers/keyboards, a `register(app)` function, its own `tests/test_<feature>.py`, and imports whatever shared behavior it needs (token cache, chunked Telegram sending, Excel styling, SMTP, rotate-on-403 fetching) from the existing shared modules below rather than reimplementing it. See "Adding a new feature module" further down for the full checklist.

- **`common.py`** — shared bot-wide infra: logger, generic JSON persistence (`_atomic_json_write`), the token cache (`get_valid_tokens`, `_any_valid_tokens`, `persist_tokens`), `CRED_MAP`/`CRED_LABELS`/`_cred_keyboard`/`_be_cred_keyboard` (lists only creds with an already-cached token — used by Bulk Export/Job Distribution/Lookup Reference/Valuer Tasks, not a duplicate of `_cred_keyboard`), `_NODE_LABELS` (node-code → human label, shared by Lookup Reference and Valuer Tasks), `cparams` constants, the main menu keyboard, auth guards (`allowed`/`deny`), `_safe_err` (redacts exception detail before it reaches a Telegram message), `_date_cutoff_str`/`_within_days`, shared filter keyboards (`_ft_county_keyboard`, `_ft_registry_keyboard`, `_ft_amount_keyboard`, `_sectional_keyboard`), and `load_sectional_config`/`save_sectional_config` (owned conceptually by Sectional Properties, but read by Auto Fetch too, so it lives here rather than forcing a cross-feature-module import).
- **`dlv_core.py`** — the DLV queue storage (`load_dlv_batch`/`save_dlv_batch`/etc.) and the assessor/DLV search-and-classify layer shared by DLV Batch and DLV Tasks. Also owns `DLV_TAGS`, the fixed tag vocabulary (Queue/Direct) DLV Batch's optional "🏷 Tag Tasks" step assigns per ref and DLV Tasks' "By Tag" report filters on — a queue item's `tag` field rides through to the closed store automatically since closed records are built by spreading the item dict. Also owns `INCREMENTAL_TAG_SENTINEL`/`parse_incremental_tag`/`is_incremental_tag` for `dlv_incremental.py`'s auto-sequenced "B{batch}-T{task}" tag — a third Tag Tasks option that's unique-per-ref rather than a small filterable vocabulary, so it lives here (not in `dlv_incremental.py` or `dlv_tasks.py`, both of which need it and the former already imports from the latter) rather than either feature module. `dlv_tasks.py`'s "By Tag" picker has its own "🔢 Incremental" entry using the same sentinel as a filter meaning "every incremental-tagged ref together" (`_dt_tag_matches`), distinct from `dlv_batch.py`'s use of the same sentinel to mean "generate a new incremental tag for this ref".
- **`fetch_tasks_cache.py`** — the 1-day assessor cache bridging Fetch Tasks, DLV Batch, and DLV Tasks.
- **`task_block.py`** — the shared labeled-block renderer used by every task-list report in the bot: `format_labeled_block(i, ref, fields, markdown=True)` is the one visual (numbered, bold/backticked Ref header, one indented "label: value" line per field); `assessor_field`/`consideration_field`/`parcel_field`/`tag_field`/`default_task_fields` are the common per-field builders each report composes with its own report-specific fields. Used by DLV Tasks' Open/Closed/By Valuer/By Tag reports and Fetch Tasks' `_ft_format_task_block` (shared with Auto Fetch's email body) — not used for Excel exports (a different medium) or the compact 🔍 DLV Queue viewer (a lightweight status list, not a full report).
- **`email_service.py`** — the shared SMTP sender: `_send_bulk_export_email` (attachment-based, used by Bulk Export/DLV Tasks/Morning Briefing) and `_send_auto_fetch_email` (plain+HTML, used by Auto Fetch). Any feature that offers "email me the report" calls into here rather than building its own SMTP boilerplate.
- **`telegram_report.py`** — the shared "paginate a report and send it to Telegram" helper: `_chunk_lines` (pure, splits a list of text lines into ≤4000-char blocks) and `_send_chunked_report` (drives sending, attaching a footer/`reply_markup` to the last chunk only). DLV Tasks' and Fetch Tasks' report senders build their own lines/footer/keyboard, then delegate the chunking+sending to this.
- **`token_rotator.py`** — `_TokenRotator` (thread-safe multi-credential failover, advances to the next valid credential on 403) and `_AllTokensExhausted`, plus `fetch_with_rotation()` — the shared "GET with rotate-on-403/retry-on-5xx" loop. `valuer_tasks.py`'s `_vt_fetch_task_detail` and `job_distribution.py`'s `_jd_fetch_task_detail` both delegate to it (they started as independent copies during extraction, confirmed identical in shape, then unified in the cleanup pass below) — each still owns its own detail-view URL/headers-builder/retry constants, just not the loop itself.
- **`excel_report.py`** — `style_header_row()` and `autofit_columns()`, the shared bold-blue-header/autofilter/frozen-pane/auto-width styling used by every Excel report builder (`_dt_build_excel`, `_be_build_excel`, `_jd_build_excel`, `_vt_build_excel`). Feature-specific styling (row banding, multi-sheet layouts, section headers, number/date formats) stays in each module.
- **`endpoints.py`** — the single source of truth for every Ardhisasa API endpoint path used across the bot, including the two root URLs (`BASE_URL`, `AUTH_BASE_URL`) that every other endpoint is built from — accounts/teams, stamp-duty application list/detail/fix-application, assessor-stage list/detail, county-transfer detail, and the three auth URLs. Each constant is commented with the HTTP method, request params/body, and response shape this codebase actually reads — not the full API contract, just what's used here. This module has zero imports from the rest of the codebase (it's the lowest-level module here); `ardhisasa_auth.py` imports `AUTH_LOGIN_URL`/`AUTH_OTP_VERIFY_URL` from it for its own `login()`/`verify_otp()`, which creates no cycle since nothing in `endpoints.py` imports back from it. Every feature module that builds an API URL imports the constant it needs from here instead of rebuilding it with its own f-string, so a path (or domain) change is a one-line edit.
- **`dlv_batch.py`**, **`dlv_tasks.py`**, **`morning_briefing.py`**, **`fetch_tasks.py`**, **`refresh_auth.py`**, **`sectional_properties.py`**, **`auto_fetch.py`**, **`receive_tasks.py`**, **`lookup_reference.py`**, **`valuer_tasks.py`**, **`job_distribution.py`**, **`new_assignment.py`**, **`bulk_export.py`**, **`hold_tasks.py`**, **`dlv_incremental.py`** — one feature each, extracted out of `bot.py` (or, for `hold_tasks.py`/`dlv_incremental.py`, added as a new module from day one per the convention below). `dlv_incremental.py` (🔢 Incremental) owns a third DLV Batch tag option — picking "🔢 Incremental" in Tag Tasks stores `INCREMENTAL_TAG_SENTINEL` in the session and only resolves it to a real `"B{batch}-T{task}"` value in `dlv_batch.py`'s `recv_db_confirm`, right before the batch is actually saved, via `next_incremental_tag()` — so cancelling a batch submission never burns a counter slot. The counter (`saved_incremental_counter.json`) also holds the tasks-per-batch size (`batch_size`, default 6) alongside its position — both have no other built-in value and are set together in one flow via the feature's ⚙️ Set Counter action (`get_batch_size()` reads the current size back out). Its own 📦 By Batch / ✅ Cleared reports read only `saved_dlv_batch.json` ("queued") and `saved_assignments.json` ("cleared" — i.e. assigned, not DLV-completed) — deliberately not `saved_dlv_closed.json` — and always judge batches against the CURRENT `batch_size`, even ones created under a previous size. A batch auto-closes (`saved_incremental_closed_batches.json`, a status flag only) once all of its task slots are found cleared; 🔓 Close Batch offers the same action manually. Its "🔔 Notify on Fill" action configures a single repeating job (`saved_incremental_notify_config.json`: `enabled`/`interval_minutes`/`emails` — a list, entered comma/semicolon-separated and parsed by `_ic_parse_emails`, each address emailed independently so one bad address doesn't block the rest; a config saved before multi-address support existed, a singular `email` string, is migrated to the list shape on load) that periodically checks for newly-closed batches, newly-cleared refs, and newly-appeared open batches and — only if something's new — sends a combined report (open-but-not-yet-reported batches in the same block format as 📦 By Batch, plus a "Newly Cleared" section) via Telegram and email; `saved_incremental_notify_state.json` tracks what's already been reported so the report never repeats a batch/ref across cycles — a batch is only ever shown once, even if it's still open (not yet closed) on a later cycle, since the assumption is its tasks either all get loaded to DLV Batch or are ignored altogether. `hold_tasks.py` (✋ Hold Tasks) guards specific assigned DLV refs against takeover: pick tasks from either this bot's own tracked assignments (`common.load_saved_assignments()`) or a live DLV query, and a repeating job (1–10 min, picker in the Held Queue viewer) re-checks each one — if it's still at the valuer-report-pending stage but the current valuer differs from who it's held for, it's reassigned back automatically via the same `STAMP_DUTY_FIX_APPLICATION_URL` call every other assignment path uses; once a held ref moves past that stage for any reason it's released from the queue on its own. `lookup_reference.py` exports `_lu_search_ref`/`_lu_fetch_detail`/`_lu_format_result` as a public API — `new_assignment.py`'s `_lookup_one_ref`/`_post_assignment_report` import these directly rather than duplicating the search/detail/format logic (these two functions moved to `new_assignment.py`, not `lookup_reference.py` — they were never Lookup Reference's to own). `job_distribution.py` exports `_JD_STATUS` — `bulk_export.py`'s `cmd_export_status` (a thin combiner also reading its own `_BE_STATUS`) imports it rather than duplicating the dict. `new_assignment.py` also owns `cmd_assignments` (the `/assignments` history viewer, `BTN_ASSIGNMENTS`) — it was never part of "saved-valuers management" and always belonged with New Assignment. `new_assignment.py`, `receive_tasks.py`, `dlv_tasks.py`, and `fetch_tasks.py` all report long lists via `telegram_report._send_chunked_report` — no feature truncates/drops data past a character limit anymore.
- **`bot.py`** — the thin orchestrator: `cmd_start`/`cmd_help`, saved-valuers management (`cmd_valuers`/`cmd_delete_valuer`/`recv_delete_valuer`), daemon control (`cmd_daemon`/`recv_daemon_action`), token/error-report status (`cmd_token_status`/`cmd_error_report`), `cmd_restart`, and `main()`, which imports every feature module and calls its `register(app)`.
- **`tests/`** — `unittest`-based tests per module (stdlib only, no new dependencies). Run with `python3 -m unittest discover -s assign/tests -v`.

### Adding a new feature module

This is the standard way to add **any** new feature to this bot — not just a historical extraction pattern. A new feature is a new file, `assign/<feature>.py`, from the moment it's written; it is never prototyped inline in `bot.py` and extracted later.

1. Create `assign/<feature>.py` containing only that feature's own enum/session dataclass/handlers/keyboards. Leave anything shared by ≥2 features in the existing shared modules instead of duplicating it — a new feature that:
   - emails a report → calls `email_service`'s senders
   - displays a paginated list in Telegram → calls `telegram_report._send_chunked_report` rather than writing its own chunking loop
   - renders a per-task labeled block (ref, assessor, consideration, parcel, etc.) → calls `task_block.format_labeled_block` plus whichever field builders it needs, rather than hand-rolling its own block layout
   - fetches from an API under multi-credential rotation → calls `token_rotator.fetch_with_rotation()` rather than writing its own rotate-on-403 loop
   - builds an Excel report → calls `excel_report.style_header_row()`/`autofit_columns()` rather than re-styling row 1 by hand
   - needs the token cache, `CRED_MAP`/`CRED_LABELS`, the main menu, or auth guards → imports them from `common.py`
   - hits an existing Ardhisasa API endpoint → imports the URL constant from `endpoints.py` rather than rebuilding it with its own f-string (adding a new comment there if it's a genuinely new endpoint)

   (Quick reference: `common.py` / `dlv_core.py` / `fetch_tasks_cache.py` / `task_block.py` / `email_service.py` / `telegram_report.py` / `token_rotator.py` / `excel_report.py` / `endpoints.py` — see the Architecture bullets above for what each one owns.)
2. Expose a `register(app: Application) -> None` that builds and adds the feature's `ConversationHandler` (and any jobs/other handlers it owns) — `bot.py`'s `main()` calls it instead of building the handler inline.
3. **Authentication must follow the existing check-cache-then-login pattern, the same way every feature already does it — do not invent a new login flow.** Concretely (see `fetch_tasks.py`'s `recv_ft_cred` for the canonical example):
   - On credential selection, call `get_valid_tokens(cred_type)` (or `_any_valid_tokens()` for background jobs) from `common.py`.
   - **If cached tokens are valid** — store them on the session and go straight to the next step. Do not re-login.
   - **If not** — fall back to the standard OTP workflow: `build_session()` → `POST {AUTH_BASE_URL}/login` with `CRED_MAP[cred_type]` → transition to a `WAIT_OTP` state → on OTP reply, `POST {AUTH_BASE_URL}/otpverify` → `persist_tokens(cred_type, access_token, jwt, refresh_token)` → continue.
   - Use `CRED_LABELS[cred_type]` for user-facing text and `_cred_keyboard()` for the picker, so every feature's credential-selection UI looks identical.
4. Add `import <feature>` and `<feature>.register(app)` to `bot.py`'s `main()`; add a `COPY <feature>.py .` line to the `Dockerfile` (it copies files explicitly, not the whole directory — a missed module fails with `ImportError` only at container start, not at build time).
5. **Mandatory:** add `tests/test_<feature>.py` covering the module's non-trivial logic (pure functions directly, handlers via mocked Telegram objects) *before* the module is considered done — a new module without its own test file is an incomplete PR, not an optional follow-up. See [Testing & Documentation Conventions](#testing--documentation-conventions) below.
6. **Mandatory:** every function in the new module has a comment describing what it does — see [Testing & Documentation Conventions](#testing--documentation-conventions) below.
7. Verify: `python3 -m py_compile` + `python3 -m pyflakes` on all touched files, `import bot` succeeds, a `bot.main()` dry-run (with `Application.run_polling` stubbed) wires every handler without raising, and — when practical — a real `docker compose up` against a local/non-production bot token confirms it connects and polls cleanly before stopping it.

### Testing & Documentation Conventions

These apply to every change in this codebase, not just new-module extractions:

- **Starting a module**: a brand-new `assign/<module>.py` must ship together with its own `tests/test_<module>.py` in the same change — never added as a follow-up later. If the module has zero non-trivial logic (e.g. it's pure constants, like `endpoints.py`), the test file still exists and verifies the constants are well-formed (see `tests/test_endpoints.py` for the pattern).
- **Updating a module**: any change to `assign/<module>.py`'s behavior — a new function, a changed code path, a fixed bug — must come with a matching update to `tests/test_<module>.py` in the same change: a new test for new behavior, an updated assertion for changed behavior, or a regression test for a fixed bug. A PR that changes a module's logic without touching its test file should be treated as incomplete.
- **Comments per function**: every function and method (including pure helpers, handlers, and one-liners) gets a short comment directly above or as its docstring, stating what it does. This applies repo-wide in `assign/`, and takes precedence over the general "don't add comments that just restate the code" preference elsewhere — in this codebase, every function is commented regardless of how self-explanatory its name is.

### Extraction history

`bot.py` went from ~9,200 lines to **~751 lines** across ten extraction steps, in this order: Refresh Auth → Sectional Properties → Auto Fetch → Receive Tasks → a prerequisite cleanup pass → Lookup Reference → Valuer Tasks → Job Distribution → New Assignment → Bulk Export. `bot.py` is now the thin orchestrator described in the Architecture section above — this history is kept for the reasoning behind non-obvious cross-module decisions, in case a similar split is ever needed again (e.g. after a large new feature is added directly to `bot.py` and later needs its own module).

**Cross-dependency findings and how each was resolved** (why a naive one-feature-at-a-time order would have broken down without a prerequisite step):

1. `_be_cred_keyboard()` and `_TokenRotator`/`_AllTokensExhausted` originally lived only in Bulk Export's section but were needed by Job Distribution, Lookup Reference, and Valuer Tasks. Resolved by a dedicated prerequisite-cleanup step: `_be_cred_keyboard` promoted to `common.py`, `_TokenRotator`/`_AllTokensExhausted` promoted to a new `token_rotator.py` — done *before* those three features extracted, so none of them had to depend on Bulk Export extracting first.
2. `cmd_export_status` renders both Bulk Export's `_BE_STATUS` and Job Distribution's `_JD_STATUS` in one message — owned by neither. Resolved last: it now lives in `bulk_export.py` (the final extraction), importing `_JD_STATUS` from `job_distribution.py`.
3. New Assignment's `recv_confirm` calls `_post_assignment_report`/`_lookup_one_ref`, built on Lookup Reference's primitives. Resolved by extracting Lookup Reference first and having it export `_lu_search_ref`/`_lu_fetch_detail`/`_lu_format_result` as a public API; when New Assignment extracted later, `_post_assignment_report`/`_lookup_one_ref` moved with it (they were never Lookup Reference's to own) and import those three primitives directly.
4. Auto Fetch's `_auto_fetch_job` reads Sectional Properties' `load_sectional_config` for auto-routing — resolved by extracting Sectional Properties before Auto Fetch, promoting `load_sectional_config`/`save_sectional_config` to `common.py` in the process.
5. Lookup Reference and Valuer Tasks both use `_NODE_LABELS` — promoted to `common.py` in the same prerequisite-cleanup step as #1.
6. Valuer Tasks' `_vt_run` turned out to call Job Distribution's `_jd_fetch_task_detail`/`_jd_headers`/`_JD_WORKERS` — an undocumented dependency only discovered once Valuer Tasks was being extracted (Job Distribution hadn't extracted yet). Resolved short-term by giving `valuer_tasks.py` its own `_vt_fetch_task_detail`/`_vt_headers`/`_VT_WORKERS` copy rather than reaching into `bot.py`'s still-inline Job Distribution section; when Job Distribution extracted next it got the equivalent independent copy. The two were later unified for real in the cleanup pass below, once both real call sites existed side by side and the duplication was confirmed exact.

**Notes specific to the last few steps:**
- **New Assignment** also picked up `cmd_assignments` (the `/assignments` history viewer) since it was never truly "saved-valuers management" — it always belonged with New Assignment.
- **Bulk Export** was extraction-ready without further prerequisites: `_TokenRotator`/`_be_cred_keyboard` already lived in shared modules, and by the time it was tackled no other feature's code sat between `BESession` and its own logic/handlers in `bot.py` — only permanent orchestrator code (`cmd_start`/`cmd_valuers`/`cmd_daemon`/etc.) and pointer comments for already-extracted features. `cmd_export_status` moved here and resolved blocker #2 for real.

**Cleanup pass** (done, as a separate follow-up after the ten extraction steps above): three items, each its own commit —
1. Migrated the four remaining hand-rolled truncate-at-4000-chars/manual-chunking spots (`new_assignment.py`'s `cmd_assignments` and `recv_confirm`; `receive_tasks.py`'s `_do_assign_tasks` and `_rt_fetch_and_show`) onto `telegram_report._send_chunked_report`. This wasn't just DRY — the old truncate/collapse behavior silently dropped data on large batches (e.g. an assignment run against 300+ refs lost everything past ~4000 characters of the summary); all four now paginate across multiple messages instead.
2. Extracted `excel_report.py`'s `style_header_row()`/`autofit_columns()` out of the four `_*_build_excel` functions (`dlv_tasks.py`, `bulk_export.py`, `job_distribution.py`, `valuer_tasks.py`), preserving each one's exact column-width bounds via parameters.
3. Unified `_vt_fetch_task_detail`/`_jd_fetch_task_detail` into `token_rotator.fetch_with_rotation()` — confirmed byte-for-byte identical except naming, so both now delegate to the one shared loop.

**`ardhisasa_auth.py`** — Authentication layer:
- Exports four hardcoded credential profiles: `PUBLIC_CREDENTIALS`, `STAFF_CREDENTIALS_ICT`, `STAFF_CREDENTIALS_SUPPORT`, `STAFF_CREDENTIALS_VALUER`
- `build_session()` returns a `requests.Session` with exponential backoff retry on 429/5xx, browser-like headers to avoid bot detection
- `login()` + `verify_otp()` implement the two-step OTP flow; `authenticate()` wraps both with stdin prompting
- `auth_headers(tokens)` returns `{"Authorization": "Bearer ...", "JWTAUTH": "Bearer ..."}`
- `AuthTokens` dataclass holds `access_token` + `jwt`; expiry detected by manually base64-decoding the JWT payload
- `login()`/`verify_otp()` build their request URL from `AUTH_LOGIN_URL`/`AUTH_OTP_VERIFY_URL`, imported from `endpoints.py` (not a locally-defined `AUTH_BASE_URL` — that root constant now lives in `endpoints.py` too)
- `decode_jwt_exp()` and `build_session()` are also imported directly by `token_refresh_daemon.py` (which gets its own refresh-token URL from `endpoints.AUTH_REFRESH_TOKEN_URL`, not from `ardhisasa_auth.py`)

**`bot.py`** — Telegram bot and orchestration (thin, ~751 lines):
- Built on `python-telegram-bot` v21; owns only the permanent orchestrator functions — `cmd_start`/`cmd_help`, saved-valuers management (`cmd_valuers`/`cmd_delete_valuer`/`recv_delete_valuer`), daemon control (`cmd_daemon`/`recv_daemon_action`), token/error-report status (`cmd_token_status`/`cmd_error_report`), `cmd_restart` — plus `main()`, which imports every feature module and calls its `register(app)`. Every feature (New Assignment, Receive Tasks, Lookup Reference, Valuer Tasks, Job Distribution, Bulk Export, and all the earlier extractions) lives in its own sibling module.
- `CRED_MAP` / `CRED_LABELS` dicts (in `common.py`) must be updated in sync with any credential changes in `ardhisasa_auth.py`
- Three `cparams` constants (base64-encoded role JSON sent as `cparams` header, in `common.py`): `CPARAMS_DLV` (`{"active_role":"DLV"}`), `CPARAMS_ASSESSOR` (`{"active_role":"ASSESSOR_OF_STAMP_DUTY"}`), `CPARAMS_VALUER_ROLE` (`{"active_role":"VALUER"}`) — each required by the respective task endpoints
- Token cache: checks `saved_tokens.json` first; falls back to full login+OTP flow only when tokens are expired (5 min buffer) — this pattern is followed identically by every extracted feature module, not just what remains in `bot.py`

**`bulk_export.py`** — Bulk Export (`BE` enum) + `/export_status`:
- Exports a full stamp-duty valuation report (Ardhisasa or Ardhipay, county/registry-filtered, "Completed" applications only) to Excel, optionally emailed and/or run on a repeating schedule
- Fetches list pages, then detail + office-report per record in parallel with multi-credential token rotation on 403 (`_TokenRotator`, from `token_rotator.py`); resumes from a saved partial checkpoint (`saved_bulk_export_partial.json`) if tokens get exhausted mid-run
- `cmd_export_status` ("📊 Export Status") is a thin combiner: also reads Job Distribution's `_JD_STATUS` (imported from `job_distribution.py`) since that feature's background job has nowhere else natural to render its progress from — Bulk Export and Job Distribution are the two "long background job" features in this bot
- `register(app)` also restores a saved repeating schedule on startup (`saved_bulk_export_schedule.json`)

**`new_assignment.py`** — New Assignment (`S` enum) + `/assignments` history:
- `Session` dataclass tracks per-user assign-flow state
- Reference numbers can be typed manually or extracted from photos via `pytesseract` + Claude Vision (Anthropic API) as fallback
- Supports bulk assignment — one valuer to multiple reference numbers in a single flow
- Detects already-assigned references before proceeding and asks the user whether to reassign
- OCR pipeline in `ocr_extract_refs()`: tries `pytesseract` first, falls back to Claude Vision (`claude-opus-4-6`) if no refs found; reference numbers matched by `_REF_RE = r'\b[A-Z0-9]{2,}(?:/[A-Z0-9]{2,}){2,}\b'`
- `_lookup_one_ref`/`_post_assignment_report` (shown after a successful run) build on Lookup Reference's `_lu_search_ref`/`_lu_fetch_detail`/`_lu_format_result`, imported from `lookup_reference.py`

**`token_refresh_daemon.py`** — Background token refresh daemon:
- Watches the token cache and proactively refreshes each credential's tokens 5 minutes before JWT expiry
- Auto-started by `bot.py` on every boot (`_post_init` → `_daemon_start()`), so it comes back automatically after a redeploy/rebuild — no manual step needed
- Can also be started/stopped manually via the "🔄 Token Daemon" menu button (spawns a subprocess, writes PID to `data/daemon.pid`) or run standalone: `python token_refresh_daemon.py` / `nohup python token_refresh_daemon.py &`
- `_daemon_running()` cross-checks `/proc/<pid>/cmdline` against the daemon script (on Linux) to avoid a false-positive "already running" from a stale `daemon.pid` colliding with an unrelated PID after a container rebuild

### Assign Flow State Machine (`S` enum, in `new_assignment.py`)

```
/assign  (or "📋 New Assignment")
  → INPUT_METHOD: choose text or photo input
  → REF_NUMBERS / RECV_PHOTOS + CONFIRM_REFS: collect reference numbers
  → [some refs already assigned?] REASSIGN_CONFIRM
  → PICK_VALUER_SOURCE: pick saved valuer OR search new
  → VALUER_NAME: enter name when searching new
  → CHOOSE_CRED: select credential profile
  → [cache hit?] skip login / [cache miss] login → WAIT_OTP → verify OTP & cache tokens
  → SELECT_VALUER: inline keyboard of matching valuers
  → CONFIRM → run assignments → show per-reference results (valuer auto-saved)
```

### Receive Tasks Flow State Machine (`RS` enum)

```
/receive  (or "📥 Receive Tasks")
  → PICK_STAFF_SOURCE: choose saved valuer or search new
  → STAFF_NAME: enter name when searching new
  → SELECT_STAFF: choose from results
  → CHOOSE_CRED → WAIT_OTP (if needed)
  → TASK_TYPE: choose Stamp Duty vs County Stamp Duty (shown only when staff has both)
  → TASK_COUNT: how many tasks to pull
  → AMOUNT_RANGE: set min/max amount filter (Enter or Skip buttons)
  → AMOUNT_TEXT: text input for min-max values (if Enter chosen)
  → SCHEDULE_CHOICE: run once or on an interval
  → SCHEDULE_INTERVAL: enter interval in minutes (if scheduled)
  → RT_CONFIRM → fetch + assign matching tasks → show results
```

Menu buttons (all `BTN_*` constants defined in `common.py`, laid out by `_main_menu()`): `📋 New Assignment`, `📊 Fetch Tasks`, `⏰ Auto Fetch`, `🗂 AF Results`, `📜 Assignments`, `📥 DLV Batch`, `📤 Export Valuation Report`, `📊 Export Status`, `🏆 Job Distribution`, `👤 Valuer Tasks`, `📋 DLV Tasks`, `🔲 Sectional`, `🌅 Morning Briefing`, `✋ Hold Tasks`, `🔢 Incremental`, `🔎 Lookup Reference`, `🔑 Refresh Auth`, `🔒 Token Status`, `📉 Error Report`, `👥 Saved Valuers`, `🗑 Delete Valuer`, `🔄 Token Daemon`, `🔁 Restart Bot`, `❓ Help`, `🛑 Cancel`. Receive Tasks (`/receive`) and DLV Queue (`🔍 DLV Queue`, reached from within the DLV Batch flow) have no top-level menu entry; Hold Tasks' own "View Held Queue" sub-view is reached from within `✋ Hold Tasks` the same way.

### Key API Endpoints

- `POST /acl/api/v1/auth/login` — initiate login (triggers OTP on registered device)
- `POST /acl/api/v1/auth/otpverify` — OTP verification, returns `access_token` + `jwt`
- `GET /acl/api/v1/accounts/list-user-accounts` — search valuers/staff by name
- `PUT /valuationservice/api/v1/stamp-duty/fix_application_...` — assign valuer to reference
- DLV task endpoints require the `cparams` header set to `CPARAMS_DLV`

### Persistent Storage

JSON files in `./data/` (mounted as Docker volume `bot_data`):
- `saved_valuers.json` — `[{name, uid, account_number}]` — reusable valuer list
- `saved_tokens.json` — `{cred_type: {access_token, jwt, expires_at}}` — token cache
- `saved_assignments.json` — `{ref_number: {valuer_name, valuer_uid, assigned_at}}` — assignment history (used to detect reassignments)
- `saved_task_batches.json` — persisted receive-tasks batch results
- `saved_schedules.json` — scheduled receive-tasks configurations
- `saved_bulk_export_schedule.json` — the one active repeating Bulk Export schedule (if any)
- `saved_bulk_export_partial.json` — Bulk Export resume checkpoint (rows + done IDs) saved when tokens get exhausted mid-run
- `saved_hold_tasks.json` — `[{ref, held_valuer_name, held_valuer_uid, held_at, last_checked, last_error}]` — Hold Tasks' guarded-refs queue
- `saved_dlv_batch.json` / `saved_dlv_closed.json` — the DLV Batch active queue and closed store (`[{ref, valuer_name, valuer_uid, valuer_acct, queued_at, assessor, tag, last_error}]`); `tag` is optional, set via DLV Batch's "🏷 Tag Tasks" step from `dlv_core.DLV_TAGS` or `dlv_incremental.py`'s auto-sequenced value, and carries through to the closed record automatically
- `saved_incremental_counter.json` — `{batch_number, task_number, batch_size}`, the current position + tasks-per-batch size `dlv_incremental.py`'s `next_incremental_tag()` consumes from and advances (wraps to the next batch once `task_number` reaches `batch_size`); all three are set together via ⚙️ Set Counter, `batch_size` defaulting to 6 if never set
- `saved_incremental_closed_batches.json` — `[batch_number, ...]`, a pure status flag set once all 6 of a batch's tagged refs are found cleared (auto-detected on every 📦 By Batch/🔓 Close Batch view, or set manually) — never moves or deletes anything
- `saved_incremental_notify_config.json` — `{enabled, interval_minutes, emails}` for 🔢 Incremental's "🔔 Notify on Fill" single repeating job (one config, not per-schedule like Auto Fetch); `emails` is a list (comma/semicolon-separated at entry, parsed by `_ic_parse_emails`), each sent to independently; a pre-multi-address config (a singular `email` string) is migrated to the list shape the first time it's read. Set via that menu action, which also (re)schedules the job (`ic_notify_job`) immediately and on bot startup if `enabled`
- `saved_incremental_notify_state.json` — `{closed_batches, reported_cleared_refs, reported_batches}`, what the last Notify on Fill cycle already knew about, so a batch/cleared-ref already reported never repeats — closed batches are re-derived from `saved_incremental_closed_batches.json` each cycle, `reported_cleared_refs` accumulates ref-by-ref as new clearances are reported, `reported_batches` accumulates batch numbers the moment they're shown in an Available Batches section (whether still open or since closed) so an open batch is never resent on a later cycle just because it hasn't closed yet
- `saved_auto_fetch.json` — `[{id, interval_minutes, days_back, county_filter, registry_filter, amount_min, amount_max, sectional_filter, email}]` — every active Auto Fetch schedule; each gets its own repeating job (`auto_fetch_job:{id}`), independently addable/removable via the ⏰ Auto Fetch menu. A pre-multi-schedule file (a bare dict, not a list) is migrated to this shape the first time it's read.
- `saved_af_results.json` — Auto Fetch run history (last 20 runs across all schedules), each record tagged with `schedule_id`/`schedule_label` so runs from different schedules are distinguishable in 🗂 AF Results
- `saved_af_email_state.json` — `{schedule_id: [ref, ...]}`, the ref set actually emailed last cycle per Auto Fetch schedule; a cycle whose current ref set matches exactly is skipped rather than re-sending an identical email (the Telegram summary still sends every cycle regardless) — cleared for a schedule when it's removed via 🗑 Remove Schedule
- `daemon.pid` / `daemon.log` — token refresh daemon process tracking

## Git Workflow

- **Start a new feature branch off `assign` for each new, unrelated task** — don't keep piling unrelated fixes onto whatever branch happens to be checked out. Before starting work, check whether the current branch's existing commits are related to the new request; if they aren't, `git checkout assign && git pull && git checkout -b <type>/<short-task-name>` first. Continuing on the current branch is fine when the new request is a direct follow-up/fix to what that branch is already about (e.g., addressing review feedback on the same feature).
- PRs land on `assign`, not `main` (see CI note below) — always target `assign` with `gh pr create --base assign`.
- Name branches `type/short-description` (e.g. `fix/auto-fetch-email-silent-failure`, `feat/dlv-tasks-by-valuer`), matching the conventional-commit type of the primary change.

## Notes

- Test suite: `tests/` (stdlib `unittest`, no new dependencies). Run with `python3 -m unittest discover -s assign/tests -v`. See [Testing & Documentation Conventions](#testing--documentation-conventions) — every module gets its own test file, kept in sync as the module changes, and every function is commented.
- Credentials are hardcoded in `ardhisasa_auth.py` — do not move to `.env` without updating the `CRED_MAP` / `CRED_LABELS` dicts in `common.py`.
- The Dockerfile `COPY`s each source file explicitly (no wildcard) — any new module (extracted feature, shared helper) must get its own `COPY <module>.py .` line added, or the container fails with `ImportError` at startup despite building successfully.
- The Dockerfile omits `tesseract-ocr` system package, so `pytesseract` will fail silently in Docker unless the image is updated; Claude Vision covers that fallback path.
- The token refresh daemon auto-starts with the bot (`_post_init`) so it survives redeploys without a manual restart.
