# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Python Telegram bot that automates valuation officer assignment in the Ardhisasa Kenyan land valuation system. It interacts with the Ardhisasa API (`https://ardhisasa-api.lands.go.ke`) to search for valuers and bulk-assign reference numbers to them.

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

## Architecture

Two source files:

**`ardhisasa_auth.py`** — Authentication layer:
- Manages three hardcoded credential profiles (Public User, ICT Staff, Support Staff)
- Builds a resilient `requests.Session` with exponential backoff retry on 429/5xx
- Handles login → OTP verification → token retrieval
- `AuthTokens` dataclass holds `access_token` + JWT; includes expiry detection by manually decoding the JWT payload (no external JWT library)

**`bot.py`** — Telegram bot and orchestration:
- Built on `python-telegram-bot` v21 using a `ConversationHandler` state machine
- `Session` dataclass tracks per-user state across conversation turns
- Persists two JSON files: `saved_valuers.json` (previously used valuers for quick reuse) and `saved_tokens.json` (cached auth tokens keyed by credential profile)
- On assignment: checks token cache first, falls back to full login+OTP flow only when tokens are expired
- Supports bulk assignment — one valuer assigned to multiple reference numbers in a single flow

### Conversation State Flow

```
/assign
  → collect reference numbers
  → pick saved valuer OR search by name
  → select credential profile (Public/ICT/Support)
  → [cache hit?] skip login / [cache miss] login → WAIT_OTP
  → select valuer from search results
  → confirm → run assignments → show per-reference results
```

### Key API Endpoints

- `POST /acl/api/v1/auth/login` — initiate login
- `POST /acl/api/v1/auth/verify-otp` — OTP verification, returns tokens
- `GET /acl/api/v1/accounts/list-user-accounts` — search valuers
- `PUT /valuationservice/api/v1/stamp-duty/fix_application_...` — assign valuer to reference

### Persistent Storage

JSON files written to `./data/` (mounted as Docker volume `bot_data`):
- `saved_valuers.json` — `{telegram_user_id: [{name, id}]}`
- `saved_tokens.json` — `{credential_key: {access_token, jwt_token, expires_at}}`

## Notes

- No test suite exists in this project.
- Credentials for the three profiles are hardcoded in `ardhisasa_auth.py` — do not move them to `.env` without updating the credential selection UI in `bot.py`.
- Token expiry check uses a 60-second buffer before actual JWT expiry to avoid race conditions.
