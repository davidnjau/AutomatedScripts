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

`bot.py` originally held every feature in one file. It's being split incrementally, one feature at a time, into sibling modules that each own their conversation handlers and register themselves into `bot.py`'s `main()` via a `register(app)` function:

- **`common.py`** — shared bot-wide infra: logger, generic JSON persistence (`_atomic_json_write`), the token cache (`get_valid_tokens`, `_any_valid_tokens`, `persist_tokens`), `CRED_MAP`/`CRED_LABELS`/`_cred_keyboard`, `cparams` constants, the main menu keyboard, auth guards (`allowed`/`deny`), `_date_cutoff_str`/`_within_days`, and shared filter keyboards (`_ft_county_keyboard`, `_ft_registry_keyboard`, `_ft_amount_keyboard`, `_sectional_keyboard`).
- **`dlv_core.py`** — the DLV queue storage (`load_dlv_batch`/`save_dlv_batch`/etc.) and the assessor/DLV search-and-classify layer shared by DLV Batch and DLV Tasks.
- **`fetch_tasks_cache.py`** — the 1-day assessor cache bridging Fetch Tasks, DLV Batch, and DLV Tasks.
- **`email_service.py`** — the shared SMTP sender: `_send_bulk_export_email` (attachment-based, used by Bulk Export/DLV Tasks/Morning Briefing) and `_send_auto_fetch_email` (plain+HTML, used by Auto Fetch). Any feature that offers "email me the report" calls into here rather than building its own SMTP boilerplate.
- **`telegram_report.py`** — the shared "paginate a report and send it to Telegram" helper: `_chunk_lines` (pure, splits a list of text lines into ≤4000-char blocks) and `_send_chunked_report` (drives sending, attaching a footer/`reply_markup` to the last chunk only). DLV Tasks' and Fetch Tasks' report senders build their own lines/footer/keyboard, then delegate the chunking+sending to this.
- **`dlv_batch.py`**, **`dlv_tasks.py`**, **`morning_briefing.py`**, **`fetch_tasks.py`** — one feature each, extracted out of `bot.py`.
- **`bot.py`** — everything not yet extracted, plus `main()`, which imports each module and calls its `register(app)`.
- **`tests/`** — `unittest`-based tests per module (stdlib only, no new dependencies). Run with `python3 -m unittest discover -s assign/tests -v`.

**When extracting a new feature into its own module, follow the pattern established by the modules above:**

1. Move only that feature's enum/session dataclass/handlers/keyboards into the new file; leave anything shared by ≥2 features in `common.py`, `dlv_core.py`, `fetch_tasks_cache.py`, `email_service.py`, or `telegram_report.py` instead of duplicating it — e.g. a new feature that emails a report calls `email_service`'s senders, and one that displays a paginated list in Telegram calls `telegram_report._send_chunked_report` rather than writing its own chunking loop.
2. Expose a `register(app: Application) -> None` that builds and adds the feature's `ConversationHandler` (and any jobs/other handlers it owns) — `bot.py`'s `main()` calls it instead of building the handler inline.
3. **Authentication must follow the existing check-cache-then-login pattern, the same way every feature already does it — do not invent a new login flow.** Concretely (see `fetch_tasks.py`'s `recv_ft_cred` for the canonical example):
   - On credential selection, call `get_valid_tokens(cred_type)` (or `_any_valid_tokens()` for background jobs) from `common.py`.
   - **If cached tokens are valid** — store them on the session and go straight to the next step. Do not re-login.
   - **If not** — fall back to the standard OTP workflow: `build_session()` → `POST {AUTH_BASE_URL}/login` with `CRED_MAP[cred_type]` → transition to a `WAIT_OTP` state → on OTP reply, `POST {AUTH_BASE_URL}/otpverify` → `persist_tokens(cred_type, access_token, jwt, refresh_token)` → continue.
   - Use `CRED_LABELS[cred_type]` for user-facing text and `_cred_keyboard()` for the picker, so every feature's credential-selection UI looks identical.
4. Add `import <module>` and `<module>.register(app)` to `bot.py`'s `main()`; add a `COPY <module>.py .` line to the `Dockerfile` (it copies files explicitly, not the whole directory — missed modules fail with `ImportError` only at container start, not at build time).
5. Add a `tests/test_<module>.py` covering the module's non-trivial logic (pure functions directly, handlers via mocked Telegram objects), and confirm the full suite still passes.
6. Verify: `python3 -m py_compile` + `python3 -m pyflakes` on all touched files, `import bot` succeeds, a `bot.main()` dry-run (with `Application.run_polling` stubbed) wires every handler without raising, and — when practical — a real `docker compose up` against a local/non-production bot token confirms it connects and polls cleanly before stopping it.

**`ardhisasa_auth.py`** — Authentication layer:
- Exports four hardcoded credential profiles: `PUBLIC_CREDENTIALS`, `STAFF_CREDENTIALS_ICT`, `STAFF_CREDENTIALS_SUPPORT`, `STAFF_CREDENTIALS_VALUER`
- `build_session()` returns a `requests.Session` with exponential backoff retry on 429/5xx, browser-like headers to avoid bot detection
- `login()` + `verify_otp()` implement the two-step OTP flow; `authenticate()` wraps both with stdin prompting
- `auth_headers(tokens)` returns `{"Authorization": "Bearer ...", "JWTAUTH": "Bearer ..."}`
- `AuthTokens` dataclass holds `access_token` + `jwt`; expiry detected by manually base64-decoding the JWT payload
- `decode_jwt_exp()`, `build_session()`, and `AUTH_BASE_URL` are also imported directly by `token_refresh_daemon.py`

**`bot.py`** — Telegram bot and orchestration:
- Built on `python-telegram-bot` v21 using two parallel `ConversationHandler` state machines: `S` (assign flow) and `RS` (receive-tasks flow)
- `Session` dataclass tracks per-user assign-flow state; `RTSession` tracks receive-tasks flow state
- Token cache: checks `saved_tokens.json` first; falls back to full login+OTP flow only when tokens are expired (5 min buffer)
- Reference numbers can be typed manually or extracted from photos via `pytesseract` + Claude Vision (Anthropic API) as fallback
- Supports bulk assignment — one valuer to multiple reference numbers in a single flow
- Detects already-assigned references before proceeding and asks the user whether to reassign
- Three `cparams` constants (base64-encoded role JSON sent as `cparams` header): `CPARAMS_DLV` (`{"active_role":"DLV"}`), `CPARAMS_ASSESSOR` (`{"active_role":"ASSESSOR_OF_STAMP_DUTY"}`), `CPARAMS_VALUER_ROLE` (`{"active_role":"VALUER"}`) — each required by the respective task endpoints
- OCR pipeline in `ocr_extract_refs()`: tries `pytesseract` first, falls back to Claude Vision (`claude-opus-4-6`) if no refs found; reference numbers matched by `_REF_RE = r'\b[A-Z0-9]{2,}(?:/[A-Z0-9]{2,}){2,}\b'`
- `CRED_MAP` / `CRED_LABELS` dicts (now in `common.py`) must be updated in sync with any credential changes in `ardhisasa_auth.py`

**`token_refresh_daemon.py`** — Background token refresh daemon:
- Watches the token cache and proactively refreshes each credential's tokens 5 minutes before JWT expiry
- Auto-started by `bot.py` on every boot (`_post_init` → `_daemon_start()`), so it comes back automatically after a redeploy/rebuild — no manual step needed
- Can also be started/stopped manually via the "🔄 Token Daemon" menu button (spawns a subprocess, writes PID to `data/daemon.pid`) or run standalone: `python token_refresh_daemon.py` / `nohup python token_refresh_daemon.py &`
- `_daemon_running()` cross-checks `/proc/<pid>/cmdline` against the daemon script (on Linux) to avoid a false-positive "already running" from a stale `daemon.pid` colliding with an unrelated PID after a container rebuild

### Assign Flow State Machine (`S` enum)

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

Menu buttons: `📋 New Assignment`, `📥 Receive Tasks`, `📊 Implementor Tasks`, `📋 DLV Tasks`, `🔄 Token Daemon`, `👥 Saved Valuers`, `🗑 Delete Valuer`, `❓ Help`, `🛑 Cancel`.

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
- `daemon.pid` / `daemon.log` — token refresh daemon process tracking

## Notes

- Test suite: `tests/` (stdlib `unittest`, no new dependencies). Run with `python3 -m unittest discover -s assign/tests -v`.
- Credentials are hardcoded in `ardhisasa_auth.py` — do not move to `.env` without updating the `CRED_MAP` / `CRED_LABELS` dicts in `common.py`.
- The Dockerfile `COPY`s each source file explicitly (no wildcard) — any new module (extracted feature, shared helper) must get its own `COPY <module>.py .` line added, or the container fails with `ImportError` at startup despite building successfully.
- The Dockerfile omits `tesseract-ocr` system package, so `pytesseract` will fail silently in Docker unless the image is updated; Claude Vision covers that fallback path.
- The token refresh daemon auto-starts with the bot (`_post_init`) so it survives redeploys without a manual restart.
