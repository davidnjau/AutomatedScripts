# Ardhisasa Telegram Bot

A Telegram bot that automates valuation officer assignment in the [Ardhisasa](https://ardhisasa.lands.go.ke) Kenyan land valuation system.

## Features

- **New Assignment** — assign a valuer to one or more reference numbers (manual or photo input via OCR)
- **DLV Batch** — bulk-assign multiple refs to multiple valuers in one go; unresolved refs are retried every 5 minutes automatically
- **DLV Queue** — view pending DLV batch items, query immediately, or change the retry interval (1/2/3/5 min)
- **Fetch Tasks** — fetch unassigned TRANSFER stamp duty tasks filtered by amount, county, registry, and date range; sectional properties are excluded automatically
- **Auto Fetch** — schedule periodic task fetches (15 min – 24 hr) with an amount range filter; results sent to chat automatically
- **Assignments** — list all recorded assignments with assignee and timestamp
- **Token Daemon** — background daemon that refreshes authentication tokens before they expire
- **Token Status** — view current token validity for all credential profiles
- **Refresh Auth** — manually trigger a new OTP login for any credential profile
- **Restart Bot** — in-place process restart without container rebuild

---

## Project Structure

```
assign_v/
├── src/
│   ├── bot.py                   # Main Telegram bot — all conversation flows and handlers
│   ├── ardhisasa_auth.py        # Authentication layer — OTP login, token management, sessions
│   └── token_refresh_daemon.py  # Background daemon — auto-refreshes tokens before expiry
├── data/                        # Persistent JSON storage (Docker volume)
│   ├── saved_tokens.json        # Cached auth tokens per credential profile
│   ├── saved_valuers.json       # Saved valuer profiles for quick reuse
│   ├── saved_assignments.json   # Assignment history (ref → valuer, timestamp)
│   ├── saved_dlv_batch.json     # DLV batch queue (pending ref+valuer assignments)
│   ├── saved_auto_fetch.json    # Auto Fetch schedule settings
│   ├── daemon.pid               # Token refresh daemon process ID
│   └── daemon.log               # Token refresh daemon log
├── .github/
│   └── workflows/
│       └── deploy.yml           # CI/CD — auto-deploy to VM on push to main or develop
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

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
python src/bot.py
```

### Token refresh daemon (optional — runs alongside the bot)

```bash
python src/token_refresh_daemon.py
```

---

## Environment Variables

Create a `.env` file in the project root:

```env
TELEGRAM_BOT_TOKEN=        # Telegram bot API token
ALLOWED_TELEGRAM_IDS=      # Comma-separated Telegram user IDs allowed to use the bot
ANTHROPIC_API_KEY=         # Optional — used for Claude Vision OCR fallback on photo inputs
```

---

## Credential Profiles

Four hardcoded credential profiles are defined in `src/ardhisasa_auth.py`:

| Key             | Label            | Role used for                     |
|-----------------|------------------|-----------------------------------|
| `publicuser`    | Public User      | General task search               |
| `staff`         | ICT              | Staff-level access                |
| `staff2`        | Support Reg      | Support registry access           |
| `staff_valuer`  | Staff Valuer     | Valuer assignment (DLV endpoint)  |

Credentials are hardcoded — do not move them to `.env` without updating `CRED_MAP` / `CRED_LABELS` in `src/bot.py`.

---

## Bot Flows

### Assign Flow
`📋 New Assignment` → enter reference numbers (text or photo) → pick valuer → choose credential → OTP if needed → confirm → assign

### DLV Batch Flow
`📥 DLV Batch` → send lines in format `REF1, REF2 : Valuer Name` → bot resolves valuers → confirm → assigns; refs not found in DLV are retried every 5 min automatically

### Fetch Tasks Flow
`📊 Fetch Tasks` → authenticate → choose days back → county/registry filter → amount filter → results shown (sectional properties and DLV-queued refs excluded)

### Auto Fetch Flow
`⏰ Auto Fetch` → choose interval → enter amount range → bot sends task lists to chat on schedule

---

## Deployment

Pushes to `main` or `develop` trigger automatic deployment via GitHub Actions:
1. Files are copied to the VM via SCP
2. Docker Compose rebuilds and restarts the container

Required GitHub secrets: `VM_HOST`, `VM_USER`, `VM_PASSWORD`

---

## API

- **Base URL:** `https://ardhisasa-api.lands.go.ke`
- **Auth:** OTP-based — `POST /acl/api/v1/auth/login` → `POST /acl/api/v1/auth/otpverify`
- **Token refresh:** `POST /acl/api/v1/auth/refresh-token` (called 10 min before expiry)
- **DLV tasks:** `GET /valuationservice/api/v1/stamp-duty/application` with `cparams: {"active_role":"DLV"}`
- **Assignment:** `POST /valuationservice/api/v1/stamp-duty/fix_application_details`
