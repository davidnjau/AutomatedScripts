# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python Telegram bot that automates valuation officer assignment in the Ardhisasa Kenyan land valuation system. It interacts with the Ardhisasa API (`https://ardhisasa-api.lands.go.ke`) to search for valuers and bulk-assign reference numbers to them.

## Running the Bot

```bash
# Local development
pip install -r requirements.txt
python bot.py

# Docker (preferred for deployment)
docker-compose up --build -d
docker-compose logs -f ardhisasa-bot
docker-compose down
```

Required environment variables (in `.env`):
- `TELEGRAM_BOT_TOKEN` — Telegram bot API token
- `ALLOWED_TELEGRAM_IDS` — Comma-separated whitelist of Telegram user IDs
- `ANTHROPIC_API_KEY` — Optional; required for Claude Vision OCR fallback on photo inputs

## Architecture

Two source files:

**`ardhisasa_auth.py`** — Authentication layer:
- Exports three hardcoded credential profiles: `PUBLIC_CREDENTIALS`, `STAFF_CREDENTIALS_ICT`, `STAFF_CREDENTIALS_SUPPORT`
- `build_session()` returns a `requests.Session` with exponential backoff retry on 429/5xx, browser-like headers to avoid bot detection
- `login()` + `verify_otp()` implement the two-step OTP flow; `authenticate()` wraps both with stdin prompting
- `auth_headers(tokens)` returns `{"Authorization": "Bearer ...", "JWTAUTH": "Bearer ..."}`
- `AuthTokens` dataclass holds `access_token` + `jwt`; expiry detected by manually base64-decoding the JWT payload

**`bot.py`** — Telegram bot and orchestration:
- Built on `python-telegram-bot` v21 using a `ConversationHandler` state machine
- `Session` dataclass tracks per-user state: refs, OCR-extracted refs, already-assigned refs, selected valuer, tokens, HTTP session
- Token cache: checks `saved_tokens.json` first; falls back to full login+OTP flow only when tokens are expired (60 s buffer)
- Reference numbers can be typed manually or extracted from photos via `pytesseract` + Claude Vision (Anthropic API) as fallback
- Supports bulk assignment — one valuer to multiple reference numbers in a single flow
- Detects already-assigned references before proceeding and asks the user whether to reassign

### Conversation State Flow

```
/assign  (or "📋 New Assignment" menu button)
  → INPUT_METHOD: choose text or photo input
  → REF_NUMBERS / RECV_PHOTOS + CONFIRM_REFS: collect reference numbers
  → [some refs already assigned?] REASSIGN_CONFIRM
  → PICK_VALUER_SOURCE: pick saved valuer OR search new
  → VALUER_NAME: enter name when searching new
  → CHOOSE_CRED: select credential profile (Public / ICT / Support)
  → [cache hit?] skip login / [cache miss] login → WAIT_OTP → verify OTP & cache tokens
  → SELECT_VALUER: inline keyboard of matching valuers
  → CONFIRM → run assignments → show per-reference results (valuer auto-saved)
```

Menu commands outside the conversation: `👥 Saved Valuers`, `🗑 Delete Valuer`, `❓ Help`, `🛑 Cancel`.

### Key API Endpoints

- `POST /acl/api/v1/auth/login` — initiate login (triggers OTP on registered device)
- `POST /acl/api/v1/auth/otpverify` — OTP verification, returns `access_token` + `jwt`
- `GET /acl/api/v1/accounts/list-user-accounts` — search valuers by name
- `PUT /valuationservice/api/v1/stamp-duty/fix_application_...` — assign valuer to reference

### Persistent Storage

JSON files in `./data/` (mounted as Docker volume `bot_data`):
- `saved_valuers.json` — `[{name, uid, account_number}]` — reusable valuer list
- `saved_tokens.json` — `{cred_type: {access_token, jwt, expires_at}}` — token cache
- `saved_assignments.json` — `{ref_number: {valuer_name, valuer_uid, assigned_at}}` — assignment history (used to detect reassignments)

## Notes

- No test suite exists.
- Credentials are hardcoded in `ardhisasa_auth.py` — do not move to `.env` without updating the `CRED_MAP` / `CRED_LABELS` dicts in `bot.py`.
- The Dockerfile omits `tesseract-ocr` system package, so `pytesseract` will fail silently in Docker unless the image is updated; Claude Vision covers that fallback path.
