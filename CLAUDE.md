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

Three source files:

**`ardhisasa_auth.py`** — Authentication layer:
- Exports four hardcoded credential profiles: `PUBLIC_CREDENTIALS`, `STAFF_CREDENTIALS_ICT`, `STAFF_CREDENTIALS_SUPPORT`, `STAFF_CREDENTIALS_VALUER`
- `build_session()` returns a `requests.Session` with exponential backoff retry on 429/5xx, browser-like headers to avoid bot detection
- `login()` + `verify_otp()` implement the two-step OTP flow; `authenticate()` wraps both with stdin prompting
- `auth_headers(tokens)` returns `{"Authorization": "Bearer ...", "JWTAUTH": "Bearer ..."}`
- `AuthTokens` dataclass holds `access_token` + `jwt`; expiry detected by manually base64-decoding the JWT payload
- **Note:** `token_refresh_daemon.py` imports `_CACHE_FILE`, `_decode_jwt_exp`, `_save_cached_tokens`, and `refresh_tokens` from this module, but these are not yet implemented here — the daemon is currently broken until these symbols are added.

**`bot.py`** — Telegram bot and orchestration:
- Built on `python-telegram-bot` v21 using two parallel `ConversationHandler` state machines: `S` (assign flow) and `RS` (receive-tasks flow)
- `Session` dataclass tracks per-user assign-flow state; `RTSession` tracks receive-tasks flow state
- Token cache: checks `saved_tokens.json` first; falls back to full login+OTP flow only when tokens are expired (5 min buffer)
- Reference numbers can be typed manually or extracted from photos via `pytesseract` + Claude Vision (Anthropic API) as fallback
- Supports bulk assignment — one valuer to multiple reference numbers in a single flow
- Detects already-assigned references before proceeding and asks the user whether to reassign
- Three `cparams` constants (base64-encoded role JSON sent as `cparams` header): `CPARAMS_DLV` (`{"active_role":"DLV"}`), `CPARAMS_ASSESSOR` (`{"active_role":"ASSESSOR_OF_STAMP_DUTY"}`), `CPARAMS_VALUER_ROLE` (`{"active_role":"VALUER"}`) — each required by the respective task endpoints
- OCR pipeline in `ocr_extract_refs()`: tries `pytesseract` first, falls back to Claude Vision (`claude-opus-4-6`) if no refs found; reference numbers matched by `_REF_RE = r'\b[A-Z0-9]{2,}(?:/[A-Z0-9]{2,}){2,}\b'`
- `CRED_MAP` / `CRED_LABELS` dicts in `bot.py` must be updated in sync with any credential changes in `ardhisasa_auth.py`

**`token_refresh_daemon.py`** — Background token refresh daemon:
- Watches the token cache and proactively refreshes each credential's tokens 5 minutes before JWT expiry
- Launched from within the bot via the "🔄 Token Daemon" menu button (spawns a subprocess, writes PID to `data/daemon.pid`)
- Can also be run standalone: `python token_refresh_daemon.py` or `nohup python token_refresh_daemon.py &`
- Currently non-functional: depends on `_CACHE_FILE`, `refresh_tokens`, etc. not yet exported by `ardhisasa_auth.py`

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

- No test suite exists.
- Credentials are hardcoded in `ardhisasa_auth.py` — do not move to `.env` without updating the `CRED_MAP` / `CRED_LABELS` dicts in `bot.py`.
- The Dockerfile omits `tesseract-ocr` system package, so `pytesseract` will fail silently in Docker unless the image is updated; Claude Vision covers that fallback path.
- The token refresh daemon is partially integrated but currently broken — `ardhisasa_auth.py` needs `_CACHE_FILE`, `_decode_jwt_exp`, `_save_cached_tokens`, and `refresh_tokens` implemented before the daemon can function.
