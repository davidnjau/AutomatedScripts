#!/usr/bin/env python3
"""
token_refresh_daemon.py
=======================
Always-on daemon that monitors data/saved_tokens.json and automatically
refreshes every credential profile before its token expires.

Refresh strategy:
  POST /acl/api/v1/auth/refresh-token
      Authorization: Bearer <access_token>
      JWTAUTH:       Bearer <jwt>
      Body:          {"refresh_token": "<refresh_token>"}

  For each credential found in saved_tokens.json the daemon schedules an
  APScheduler one-shot job to fire REFRESH_BEFORE seconds before expiry.
  After a successful refresh the job reschedules itself for the new expiry.
  A periodic scan runs every SCAN_INTERVAL seconds to pick up credentials
  that were added or re-authenticated by the bot after startup.

  The daemon never triggers OTP prompts — OTP is handled exclusively by the
  bot flows (assign / receive / fetch).  Once those flows save tokens via
  persist_tokens(), the daemon picks them up and keeps them fresh.

Usage:
    python token_refresh_daemon.py              # foreground
    nohup python token_refresh_daemon.py &      # background

Environment variables (read from .env):
    TELEGRAM_BOT_TOKEN       — required for Telegram status messages
    ALLOWED_TELEGRAM_IDS     — comma-separated Telegram chat IDs
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ardhisasa_auth import (
    AUTH_BASE_URL,
    build_session,
    decode_jwt_exp,
)

load_dotenv()

# ──────────────────────────────────────────────────────────
# Paths & config
# ──────────────────────────────────────────────────────────
_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_BASE_DIR, "data", "saved_tokens.json")
PID_FILE   = os.path.join(_BASE_DIR, "data", "daemon.pid")
LOG_FILE   = os.path.join(_BASE_DIR, "data", "daemon.log")

# Refresh this many seconds before expiry
REFRESH_BEFORE  = 10 * 60   # 10 minutes
# How often to scan cache for new / changed credentials
SCAN_INTERVAL   = 5 * 60    # 5 minutes

TELEGRAM_API = "https://api.telegram.org"
BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_IDS: List[int] = [
    int(x.strip())
    for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",")
    if x.strip()
]

CRED_LABELS = {
    "publicuser":   "👤 Public User",
    "staff":        "🏢 ICT",
    "staff2":       "🏢 Support Reg",
    "staff_valuer": "🏢 Staff Valuer",
}

# ──────────────────────────────────────────────────────────
# Logging — stdout + file
# ──────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
_fh  = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh  = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger = logging.getLogger("token_refresh_daemon")
logger.setLevel(logging.INFO)
logger.addHandler(_fh)
logger.addHandler(_sh)


# ──────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────
def _api_session() -> requests.Session:
    return build_session()


def _tg_session() -> requests.Session:
    sess  = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
    sess.mount("https://", HTTPAdapter(max_retries=retry))
    return sess


# ──────────────────────────────────────────────────────────
# Token cache helpers
# ──────────────────────────────────────────────────────────
def _load_cache() -> Dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: Dict) -> None:
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, CACHE_FILE)


_decode_exp = decode_jwt_exp  # shared implementation in ardhisasa_auth


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _run_at(ts: float) -> datetime:
    """Convert a UNIX timestamp to a timezone-aware datetime for APScheduler."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# ──────────────────────────────────────────────────────────
# Telegram helper
# ──────────────────────────────────────────────────────────
_tg_sess = _tg_session()


def _tg_send(chat_id: int, text: str) -> None:
    if not BOT_TOKEN:
        return
    try:
        resp = _tg_sess.post(
            f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if not resp.ok:
            logger.warning("Telegram send failed for %s: %s", chat_id, resp.text[:200])
    except Exception as e:
        logger.warning("Telegram error for %s: %s", chat_id, e)


def _tg_broadcast(text: str) -> None:
    for chat_id in ALLOWED_IDS:
        _tg_send(chat_id, text)


# ──────────────────────────────────────────────────────────
# Refresh-token API call
# ──────────────────────────────────────────────────────────
_api_sess = _api_session()


def _try_refresh_token(
    access_token:  str,
    jwt_token:     str,
    refresh_token: str = "",
) -> Optional[Tuple[str, str, str]]:
    """
    POST /auth/refresh-token using current valid auth headers.
    Sends refresh_token in the body if one is available, but also works
    without it — the server will issue new tokens while the current ones
    are still valid (called 10 min before expiry).
    Returns (new_access_token, new_jwt, new_refresh_token) or None.
    """
    body = {}
    if refresh_token:
        body["refresh_token"] = refresh_token

    try:
        resp = _api_sess.post(
            f"{AUTH_BASE_URL}/refresh-token",
            headers={
                "Authorization": f"Bearer {access_token}",
                "JWTAUTH":       f"Bearer {jwt_token}",
            },
            json=body,
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            logger.debug("refresh-token HTTP %d: %s", resp.status_code, resp.text[:200])
            return None
        data    = resp.json()
        details = data.get("details", data)
        new_at  = details.get("access_token")
        new_jwt = details.get("jwt")
        new_rt  = details.get("refresh_token") or refresh_token
        if new_at and new_jwt:
            return new_at, new_jwt, new_rt
        logger.debug("refresh-token response missing tokens. Keys: %s", list(data.keys()))
    except Exception as e:
        logger.debug("refresh-token error: %s", e)
    return None


# ──────────────────────────────────────────────────────────
# Scheduler and job tracking
# ──────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="UTC")

# Track which (cred_type, expiry_bucket) pairs already have a scheduled job
# so we don't double-schedule for the same expiry window.
_scheduled: Dict[str, int] = {}   # cred_type → expiry_bucket (int(exp // 60))

# Credentials that failed to refresh — never retried until new tokens are saved by the bot.
_failed: set = set()


def _schedule_refresh(cred_type: str, exp: float) -> None:
    """Schedule a one-shot refresh job to run REFRESH_BEFORE seconds before exp."""
    bucket      = int(exp // 60)
    secs_left   = exp - time.time()
    fire_in     = secs_left - REFRESH_BEFORE

    if _scheduled.get(cred_type) == bucket:
        return  # already scheduled for this expiry window

    job_id = f"refresh_{cred_type}"

    if fire_in <= 0:
        # Token already within the refresh window (or expired) — run immediately.
        logger.info("[%s] Token expires in %ds — scheduling immediate refresh.", cred_type, int(secs_left))
        fire_time = datetime.now(tz=timezone.utc)
    else:
        fire_time = _run_at(exp - REFRESH_BEFORE)
        logger.info(
            "[%s] Token expires %s — refresh scheduled at %s (%dm from now).",
            cred_type, _fmt_ts(exp), _fmt_ts(exp - REFRESH_BEFORE), int(fire_in // 60),
        )

    # Remove any existing job for this credential first
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        _refresh_job,
        trigger=DateTrigger(run_date=fire_time),
        id=job_id,
        args=[cred_type],
        replace_existing=True,
        misfire_grace_time=300,
    )
    _scheduled[cred_type] = bucket


def _refresh_job(cred_type: str) -> None:
    """APScheduler job: attempt token refresh and reschedule on success."""
    label = CRED_LABELS.get(cred_type, cred_type)
    logger.info("[%s] Refresh job fired.", cred_type)

    cache = _load_cache()
    entry = cache.get(cred_type)
    if not entry:
        logger.warning("[%s] No token entry in cache — skipping.", cred_type)
        return

    access_token  = entry.get("access_token", "")
    jwt_token     = entry.get("jwt", "")
    refresh_token = entry.get("refresh_token", "")

    if not access_token or not jwt_token:
        logger.warning("[%s] No tokens in cache — skipping.", cred_type)
        return

    if not refresh_token:
        logger.warning(
            "[%s] No refresh_token stored — attempting header-only refresh. "
            "If this fails the credential will be skipped until the bot re-authenticates via OTP.",
            cred_type,
        )

    result = _try_refresh_token(access_token, jwt_token, refresh_token)
    if result:
        new_access, new_jwt, new_rt = result
        new_exp = _decode_exp(new_jwt) or (time.time() + 3600)

        cache[cred_type] = {
            "access_token":  new_access,
            "jwt":           new_jwt,
            "refresh_token": new_rt,
            "expires_at":    new_exp,
        }
        _save_cache(cache)

        _scheduled.pop(cred_type, None)
        _failed.discard(cred_type)   # clear any previous failure flag
        logger.info("[%s] ✅ Token refreshed. New expiry: %s", cred_type, _fmt_ts(new_exp))

        # Schedule next refresh for the new expiry
        _schedule_refresh(cred_type, new_exp)
    else:
        logger.warning("[%s] ❌ Refresh failed — will not retry until new tokens are saved.", cred_type)
        _failed.add(cred_type)
        _scheduled.pop(cred_type, None)


def _scan_cache() -> None:
    """Periodic job: check cache for credentials that need a scheduled refresh."""
    cache = _load_cache()
    if not cache:
        return

    now = time.time()
    for cred_type, entry in cache.items():
        jwt_token = entry.get("jwt", "")
        exp       = entry.get("expires_at") or _decode_exp(jwt_token)

        if not exp:
            logger.warning("[%s] Cannot decode expiry — skipping.", cred_type)
            continue

        secs_left = exp - now
        bucket    = int(exp // 60)

        # If a previous refresh failed, only retry if the bot has saved fresh tokens
        # (detected by the expiry bucket changing — meaning new tokens were written).
        if cred_type in _failed:
            if _scheduled.get(cred_type) == bucket:
                logger.debug("[%s] Refresh previously failed — waiting for new tokens.", cred_type)
                continue
            else:
                # New token saved by the bot — clear the failure flag and reschedule
                logger.info("[%s] New tokens detected — clearing failure flag.", cred_type)
                _failed.discard(cred_type)

        if secs_left <= 0:
            logger.info("[%s] Token already expired — scheduling immediate refresh.", cred_type)
            _scheduled.pop(cred_type, None)
            _schedule_refresh(cred_type, exp)
        elif _scheduled.get(cred_type) != bucket:
            _schedule_refresh(cred_type, exp)
        else:
            logger.debug(
                "[%s] Refresh already scheduled — expires in %dm.",
                cred_type, int(secs_left // 60),
            )


# ──────────────────────────────────────────────────────────
# Process management
# ──────────────────────────────────────────────────────────
def _kill_previous_instance() -> None:
    try:
        with open(PID_FILE) as f:
            old_pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return

    if old_pid == os.getpid():
        return

    try:
        os.kill(old_pid, signal.SIGTERM)
        logger.info("Sent SIGTERM to previous daemon instance (PID %d).", old_pid)
        time.sleep(1)
    except ProcessLookupError:
        pass
    except Exception as e:
        logger.warning("Could not stop previous daemon (PID %d): %s", old_pid, e)


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
_stop = False


def _handle_signal(sig, _frame):
    global _stop
    logger.info("Signal %s received — shutting down.", sig)
    _stop = True


def run() -> None:
    global _stop

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    _kill_previous_instance()

    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    logger.info("=" * 60)
    logger.info("Token refresh daemon started  (PID %d)", os.getpid())
    logger.info("Cache   : %s", CACHE_FILE)
    logger.info("Refresh : %d min before expiry", REFRESH_BEFORE // 60)
    logger.info("Scan    : every %d min", SCAN_INTERVAL // 60)
    logger.info("Mode    : APScheduler date-based — no polling loop")
    logger.info("=" * 60)

    scheduler.start()

    # Periodic cache scan to pick up newly authenticated credentials
    scheduler.add_job(
        _scan_cache,
        trigger=IntervalTrigger(seconds=SCAN_INTERVAL),
        id="scan_cache",
        replace_existing=True,
    )

    # Run an immediate scan on startup
    _scan_cache()

    # Main thread just waits for stop signal
    while not _stop:
        time.sleep(1)

    logger.info("Shutting down scheduler…")
    scheduler.shutdown(wait=False)
    logger.info("Token refresh daemon stopped.")

    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    run()
