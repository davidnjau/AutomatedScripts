# Ardhisasa Telegram Bot

A Telegram bot that automates valuation officer assignment in the [Ardhisasa](https://ardhisasa.lands.go.ke) Kenyan land valuation system.

## Features

Each feature below is its own module (see [Project Structure](#project-structure)) with its own conversation flow, registered independently into the bot:

- **📋 New Assignment** — assign a valuer to one or more reference numbers (manual entry or photo input via OCR); auto-saves the valuer for reuse
- **📜 Assignments** — list all recorded assignments with assignee and timestamp
- **/receive (Receive Tasks)** — pull a batch of unassigned tasks and assign them to one valuer, filtered by amount range; run once or on a repeating schedule
- **📥 DLV Batch / 🔍 DLV Queue** — bulk-assign multiple refs to multiple valuers in one go; unresolved refs are retried automatically on a timer
- **📋 DLV Tasks** — live Open/Closed report of the DLV queue (Telegram text or Excel), plus multi-select bulk delete
- **📊 Fetch Tasks** — search unassigned TRANSFER stamp duty tasks filtered by amount, county, registry, and date range (sectional properties excluded automatically)
- **⏰ Auto Fetch / 🗂 AF Results** — schedule periodic task searches; results are sent to chat (or routed to a specialist valuer) automatically
- **🌅 Morning Briefing** — daily digest of Open DLV Tasks, delivered via Telegram or email
- **🔎 Lookup Reference** — look up a single reference's current status, valuer, and node
- **👤 Valuer Tasks** — all tasks assigned to a specific valuer within a date range, exported to Excel
- **🏆 Job Distribution** — team/task-load analysis across DLV valuation teams, exported to a multi-sheet Excel report
- **📤 Export Valuation Report / 📊 Export Status** — full stamp-duty valuation report export (Ardhisasa or Ardhipay) to Excel, optionally emailed and/or scheduled; resumes from a checkpoint if interrupted
- **🔲 Sectional** — configure a specialist valuer that sectional-property tasks auto-route to
- **✋ Hold Tasks** — guard specific assigned refs against takeover: pick tasks from tracked assignments or a live DLV query, and a background job (1–10 min interval) reassigns any takeover back to the held valuer automatically, releasing the hold once the ref moves past the valuer-report-pending stage
- **🔑 Refresh Auth** — manually trigger a new OTP login for any credential profile
- **🔄 Token Daemon / 🔒 Token Status** — background daemon that refreshes auth tokens before they expire, plus a status view
- **📉 Error Report** — view recent error activity
- **👥 Saved Valuers / 🗑 Delete Valuer** — manage the reusable valuer list
- **🔁 Restart Bot** — in-place process restart without a container rebuild

---

## Project Structure

`bot.py` is a thin orchestrator — it wires up every feature, but the feature logic itself lives in one sibling module per feature. Each feature module exposes a single `register(app)` function that `bot.py`'s `main()` calls; nothing about a feature (its conversation states, session data, keyboards, or background jobs) lives anywhere else.

```
AutomatedScripts/
├── assign/
│   ├── bot.py                     # Thin orchestrator — main(), registers every feature module
│   ├── ardhisasa_auth.py          # Auth layer — OTP login, token/session management
│   ├── token_refresh_daemon.py    # Background daemon — refreshes tokens before expiry
│   │
│   │   # ── Shared infrastructure — no feature-specific logic ──
│   ├── common.py                  # Logger, token cache, cparams, main menu, auth guards
│   ├── token_rotator.py           # Multi-credential rotation + retry-on-403/5xx GET loop
│   ├── excel_report.py            # Shared Excel header/autofilter/auto-width styling
│   ├── telegram_report.py         # Shared "paginate a report and send it to Telegram" helper
│   ├── email_service.py           # Shared SMTP senders
│   ├── dlv_core.py                # DLV queue storage + assessor/DLV search-classify layer
│   ├── fetch_tasks_cache.py       # 1-day assessor cache bridging Fetch Tasks/DLV Batch/DLV Tasks
│   │
│   │   # ── Feature modules — one per menu feature above ──
│   ├── new_assignment.py
│   ├── receive_tasks.py
│   ├── dlv_batch.py
│   ├── dlv_tasks.py
│   ├── fetch_tasks.py
│   ├── auto_fetch.py
│   ├── morning_briefing.py
│   ├── lookup_reference.py
│   ├── valuer_tasks.py
│   ├── job_distribution.py
│   ├── bulk_export.py
│   ├── sectional_properties.py
│   ├── hold_tasks.py
│   ├── refresh_auth.py
│   │
│   ├── data/                      # Persistent JSON storage (Docker volume `bot_data`)
│   ├── tests/                     # unittest suite — one test file per module
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── README.md
└── .github/
    └── workflows/
        └── deploy.yml              # CI/CD — auto-deploy to VM on push to main or develop
```

### Adding a new feature

New features are added as their **own new module**, not written inline in `bot.py`. The short version:

1. Create `assign/<feature>.py` with the feature's state enum, session dataclass, conversation handlers, and a `register(app: Application) -> None` that wires them up. Every function gets a short comment describing what it does.
2. **Import, don't duplicate.** If the feature needs a token cache, a credential keyboard, chunked Telegram sending, Excel styling, an SMTP sender, or rotate-on-403 HTTP fetching, import it from `common.py` / `telegram_report.py` / `excel_report.py` / `email_service.py` / `token_rotator.py` — don't rewrite it.
3. Add `import <feature>` + `<feature>.register(app)` to `bot.py`'s `main()`, and a `COPY <feature>.py .` line to the `Dockerfile`.
4. **Mandatory:** add `tests/test_<feature>.py` in the same change — a new module isn't done until its own test file exists. When you later change that module's behavior, update its test file in the same change too (new tests for new behavior, updated assertions for changed behavior).

See `CLAUDE.md`'s Architecture section for the full checklist (auth-flow consistency, verification steps) and the reasoning behind every shared module's boundaries, and its [Testing & Documentation Conventions](CLAUDE.md#testing--documentation-conventions) section for the full comment/test rules.

---

## Running

### Docker (recommended for production)

```bash
docker compose up --build -d
docker compose logs -f ardhisasa-bot
```

### Local development

```bash
pip install -r requirements.txt
python bot.py
```

### Token refresh daemon (optional — runs alongside the bot)

```bash
python token_refresh_daemon.py
```

It also auto-starts with the bot on every boot, so this is only needed to run it standalone.

---

## Environment Variables

Create a `.env` file in this directory:

```env
TELEGRAM_BOT_TOKEN=        # Telegram bot API token
ALLOWED_TELEGRAM_IDS=      # Comma-separated Telegram user IDs allowed to use the bot
ANTHROPIC_API_KEY=         # Optional — used for Claude Vision OCR fallback on photo inputs
SMTP_HOST=                 # Optional — for email-delivered reports (Morning Briefing, Bulk Export, Auto Fetch)
SMTP_PORT=
SMTP_USER=
SMTP_PASS=
```

---

## Credential Profiles

Four hardcoded credential profiles are defined in `ardhisasa_auth.py` (`CRED_MAP` / `CRED_LABELS` in `common.py`):

| Key             | Label            | Role used for                     |
|-----------------|------------------|-----------------------------------|
| `publicuser`    | Public User      | General task search               |
| `staff`         | ICT              | Staff-level access                |
| `staff2`        | Support Reg      | Support registry access           |
| `staff_valuer`  | Staff Valuer     | Valuer assignment (DLV endpoint)  |

Credentials are hardcoded — do not move them to `.env` without updating `CRED_MAP` / `CRED_LABELS` in `common.py`.

---

## Testing

```bash
python3 -m unittest discover -s tests -v
```

Stdlib `unittest` only — no new test dependencies. One test file per module, covering pure logic directly and conversation handlers via mocked Telegram objects.

Two rules apply to every change, not just new features:
- **Starting a module** — it ships with its own `tests/test_<module>.py` in the same change, not as a later follow-up.
- **Updating a module** — its test file gets updated in the same change (new tests for new behavior, updated assertions for changed behavior, a regression test for a fixed bug).

---

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs the test suite above on every push/PR to `main` or `assign` (`assign` is this bot's actual mainline — PRs land there). There is no automatic deploy step; deploying to the VM is a manual/separate process.

---

## API

- **Base URL:** `https://ardhisasa-api.lands.go.ke`
- **Auth:** OTP-based — `POST /acl/api/v1/auth/login` → `POST /acl/api/v1/auth/otpverify`
- **Token refresh:** `POST /acl/api/v1/auth/refresh-token` (called 5 min before expiry)
- **DLV tasks:** `GET /valuationservice/api/v1/stamp-duty/application` with `cparams: {"active_role":"DLV"}`
- **Assignment:** `POST /valuationservice/api/v1/stamp-duty/fix_application_details`
