# AutomatedScripts

Python automation scripts for the [Ardhisasa](https://ardhisasa.lands.go.ke) Kenyan land valuation system (`https://ardhisasa-api.lands.go.ke`). The repo is organised as independent subdirectories — each is its own codebase with its own runtime, dependencies, and (where applicable) its own bot token and Docker container.

## Structure

| Directory | What it is |
|---|---|
| [`assign/`](assign/README.md) | Telegram bot that automates valuation officer assignment — search valuers, bulk-assign reference numbers, receive unassigned tasks. Fully modular (one feature per file, see `assign/README.md`). |
| [`entries/`](entries/README.md) | Standalone Telegram bot that updates encumbrance/proprietorship entries on Ardhisasa land registry records. Isolated from `assign/` — its own bot token, credential, and container. |

Each subdirectory has its own `CLAUDE.md` with rules specific to that codebase — check the relevant one before making changes there. `entries/CLAUDE.md` in particular forbids touching `assign/` files while working in `entries/`, and vice versa in spirit.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs `assign/`'s test suite (`python3 -m unittest discover -s tests -v`) on every push/PR to `main` or `assign` (`assign` is that bot's actual mainline branch — PRs land there, not `main`). There is no deploy or Docker build step in CI; those are manual/separate processes.

## Adding a new subdirectory

A new bot or script gets its own top-level directory with its own `requirements.txt`, `README.md`, and `CLAUDE.md` — it should not import from or depend on an existing subdirectory's code (each one is meant to stand alone, matching `assign/`'s and `entries/`'s independent-credential, independent-container setup).
