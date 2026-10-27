# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

Python automation scripts for the Ardhisasa Kenyan government land valuation system (`https://ardhisasa-api.lands.go.ke`). The repo is organised as independent subdirectories; each has its own runtime, dependencies, and in some cases its own CLAUDE.md.

## Structure

```
AutomatedScripts/
├── assign/                # Telegram bot for Ardhisasa valuation officer assignment (has its own CLAUDE.md)
├── entries/               # Telegram bot for updating land registry encumbrance/proprietorship entries (has its own CLAUDE.md)
├── nse/                   # Telegram bot for NSE morning market briefs (see below)
└── ardhi-repo-analyser/   # Telegram bot: daily QA-activity digest for local git repos, cross-checked against a GitLab reference repo (has its own CLAUDE.md)
```

Refer to `assign/CLAUDE.md` when working inside `assign/`, and `entries/CLAUDE.md` when working inside `entries/` — each enforces isolation from the other (never read/edit/move/delete files across the two, never import code between them). `entries/README.md` documents that bot's flow, files, and environment variables in full; it is a completely separate codebase from `assign/` — its own bot token, its own Ardhisasa credential, its own Docker container (`ardhisasa_entries_bot`), no shared test suite.

## nse/ — NSE Morning Brief Bot

A standalone Telegram bot that delivers a personalised Nairobi Securities Exchange morning brief daily. No authentication — any Telegram user can configure their own watchlist and delivery schedule.

### Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot — watchlist and delivery-settings conversation flows |
| `data_fetcher.py` | Fetches market movers (Apify) and per-stock prices (yfinance) |
| `report_generator.py` | Calls Claude to write the formatted brief |
| `nse_companies.py` | Static list of ~55 NSE-listed companies with Yahoo Finance tickers |
| `data/user_{chat_id}.json` | Per-user watchlist + delivery config (auto-created) |

### Running

```bash
cd nse
cp .env.example .env          # fill in tokens
pip install -r requirements.txt
python bot.py
```

### Environment variables (nse/.env)

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `APIFY_TOKEN` | From Apify console — used for NSE movers data |
| `ANTHROPIC_API_KEY` | Claude API — generates the brief narrative |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` | Gmail SMTP for email delivery |

### Architecture

- **No auth guard** — any Telegram user can use the bot; settings are keyed by `chat_id`
- **Two conversation flows:** `WL` (watchlist browser, paginated inline keyboard) and `ST` (delivery settings)
- **Data sources:** Apify actor (`wafspaul/nse-kenya-market-data`) for movers + yfinance `.NR` tickers for watchlist stock prices + NSE index levels
- **Scheduling:** `app.job_queue.run_daily()` per user; restored on startup from `data/user_*.json`
- **Delivery:** Telegram (default) or Gmail SMTP (user-configurable)

### Key design notes

- yfinance `.NR` tickers (e.g. `SCOM.NR`) can be unreliable — all price fetches are wrapped in try/except and the brief proceeds with whatever data is available
- The Apify actor response structure is not strictly typed; `data_fetcher.py` probes multiple field name variants
- `report_generator.py` instructs Claude to omit data it does not have rather than fabricate numbers

## Running Scripts (assign/)

Install dependencies:

```bash
pip install -r assign/requirements.txt
```

Run the Ardhisasa Telegram bot:

```bash
cd assign
python bot.py                       # local development
docker-compose up --build -d        # Docker (preferred for deployment)
docker-compose logs -f ardhisasa-bot
```

Token refresh daemon (optional):

```bash
cd assign
python token_refresh_daemon.py
# or: nohup python token_refresh_daemon.py &
```

## CI/CD

Each bot has its own workflow, path-filtered so a change to one module never triggers the other's pipeline. `assign` and `entries` deploy to **separate** self-hosted runners/servers as of the `games_server` migration (below) — previously both shared one runner/server:

- **`.github/workflows/ci.yml`** — triggers on push/PR to `main`/`assign` touching `assign/**` (`assign` is this bot's actual mainline — PRs land there, not `main`). A `test` job installs `assign/requirements.txt` and runs `python3 -m unittest discover -s tests -v` (dummy credential env vars supplied in the workflow — `ardhisasa_auth.py` reads them at import time but no test hits the real API). A `deploy-assign` job (needs `test`, only on push to `assign`) runs on the `[self-hosted, ardhisasa-bot-2]` runner (`games_server`) — checks out the repo and runs `docker-compose up --build -d` inside `assign/`, then prunes dangling images.
- **`.github/workflows/entries-ci.yml`** — triggers on push/PR to `main`/`entries` touching `entries/**` (`entries` is this bot's mainline, same convention as `assign`). No test job — `entries/` has no test suite. A `deploy-entries` job (only on push to `entries`) runs on the `[self-hosted, ardhisasa-bot]` runner (`assign_server`, the original box — its containers were later switched off and `assign` moved off of it, but the runner itself is still live and still handles this job) — runs `docker-compose up --build -d` inside `entries/`, then prunes dangling images.

Both deploy jobs assume their runner's checkout directory basename matches the bot's original manual-deployment directory name (`assign` / `entries`) — Docker Compose derives its named volume (`bot_data` / `entries_data`) from that basename, so a mismatch would start the container against a fresh empty volume instead of the one holding cached tokens/data.

### `assign`'s deploy target: `games_server` (migrated from `assign_server`)

`assign` was moved from its original server (`assign_server`, self-hosted runner named `test-1`, label `ardhisasa-bot`) to a new one (`games_server`, self-hosted runner named `games_server`, label `ardhisasa-bot-2`) — a shared box that also independently runs an unrelated `games-games-1` container. The two runners are kept on **distinct labels** specifically so `deploy-assign` can never land back on `assign_server` by accident (both runners stay registered and online — `assign_server`'s is still required for `entries`' own deploy job, which was intentionally left untouched).

Operational notes specific to `games_server`:
- The runner process runs as a dedicated non-root `runner` user (in the `docker` group) — the GitHub Actions runner binary refuses to run as root/with sudo.
- Only the `docker compose` plugin (v2, space-separated) ships on this box by default, not the standalone hyphenated `docker-compose` binary `ci.yml` invokes — a `/usr/local/bin/docker-compose` shim (`exec docker compose "$@"`) bridges this.
- `games_server`'s virtualized CPU exposes none of `sse3`/`ssse3`/`sse4_1`/`sse4_2`/`popcnt`/`avx` — below the x86-64-v2 baseline NumPy's 2.x wheels require at import time (crashes immediately via `openpyxl`'s transitive `numpy` import). `requirements.txt` pins `numpy<2` for this reason (1.26.x's wheels only need SSE2, true x86-64 baseline) — see that file's inline comment.

## Known Issues

- `pytesseract` fails silently in Docker because `tesseract-ocr` is not in the Dockerfile; Claude Vision (`claude-opus-4-6`) is the active fallback path.
