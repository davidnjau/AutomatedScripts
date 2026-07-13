# Entries Bot

A standalone Telegram bot that updates encumbrance/proprietorship entries on Ardhisasa land registry records. It is a completely separate codebase from `assign/` — its own bot token, its own credential, its own Docker container.

## What it does

- **📝 Update Entry** — walks through parcel number → nature of title → entry number → section → entry status → confirm, then calls the Ardhisasa `encumbrance-change` ingestion endpoint to update that entry.
- **🔑 Refresh Auth** — logs in (OTP flow) using the single credential stored in `.env`, or reuses cached tokens if still valid.
- **🔒 Token Status** — shows the cached token's expiry.
- **🔄 Token Daemon** — start/stop a background process that keeps the token refreshed.
- **🔁 Restart Bot** — in-place process restart.

## Update Entry flow

```
/entry  (or "📝 Update Entry")
  → PARCEL_NUMBER: enter the parcel number
  → NATURE_OF_TITLE: FREEHOLD / LEASEHOLD / SECTIONAL_PROPERTY / LONG_TERM_LEASE
  → ENTRY_NUMBER: enter the entry number
  → SECTION: PROPRIETORSHIP / ENCUMBRANCE
  → ENTRY_STATUS: ACTIVE / INACTIVE
  → CONFIRM → PUT to the encumbrance-change endpoint → show the result
```

The request/response contract this endpoint uses is documented with a worked example (including the three response shapes — 200 success, 400 "entry does not exist", 403 invalid token) in `entries.MD`.

## Authentication

Unlike `assign/`, which juggles four credential profiles, this bot uses a **single** credential: `USER_LOGIN` / `USER_PASSWORD` / `USER_TYPE`, read from `.env` by `ardhisasa_auth.py`. Tokens are cached the same way as `assign/` (check cache → reuse if valid → otherwise OTP login), just with one profile instead of several.

## Running

```bash
cd entries
# create .env with TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_IDS, USER_LOGIN, USER_PASSWORD, USER_TYPE (no .env.example in this directory yet)
pip install -r requirements.txt
python bot.py

# or via Docker:
docker compose up --build -d
```

## Environment variables (`entries/.env`)

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather — this bot's own token, separate from `assign/`'s |
| `ALLOWED_TELEGRAM_IDS` | Comma-separated whitelist of Telegram user IDs |
| `USER_LOGIN` / `USER_PASSWORD` | The single Ardhisasa credential this bot logs in as |
| `USER_TYPE` | Account type sent to the login endpoint (defaults to `staff`) |

## Files

| File | Purpose |
|---|---|
| `bot.py` | The entire bot — menu, auth conversation, Update Entry conversation, token daemon controls |
| `ardhisasa_auth.py` | Independent copy of the auth layer, scoped to this bot's single credential (not shared with `assign/ardhisasa_auth.py`) |
| `entries.MD` | Worked `curl` example of the encumbrance-change endpoint's request body and all three response shapes |
| `Dockerfile` / `docker-compose.yml` | Container build + run definition |

## Notes

- This directory's own `CLAUDE.md` enforces isolation from `assign/` — never read, edit, move, or delete files inside `assign/` while working here, and don't import/copy code from it.
- No test suite exists for this bot.
