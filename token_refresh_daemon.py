#!/usr/bin/env python3
"""
token_refresh_daemon.py
=======================
Always-on daemon that monitors data/saved_tokens.json and automatically
refreshes every credential profile 5–10 minutes before its token expires.

Refresh strategy:
  POST /acl/api/v1/auth/refresh-token
      Authorization: Bearer <access_token>
      JWTAUTH:       Bearer <jwt>
      Body:          {"refresh_token": "<refresh_token>"}

  The daemon only refreshes credentials that were previously authenticated
  (i.e. saved_tokens.json has a refresh_token entry for that profile).
  It never triggers OTP prompts — OTP is handled exclusively by the bot
  flows (assign / receive / fetch / auth-refresh).  Once those flows save
  tokens via persist_tokens(), the daemon picks them up and keeps them fresh.

Usage:
    python token_refresh_daemon.py              # foreground
    nohup python token_refresh_daemon.py &      # background

Environment variables (read from .env):
    TELEGRAM_BOT_TOKEN       — required
    ALLOWED_TELEGRAM_IDS     — comma-separated Telegram chat IDs
"""

import base64
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ardhisasa_auth import (
    AUTH_BASE_URL,
    PUBLIC_CREDENTIALS,
    STAFF_CREDENTIALS_ICT,
    STAFF_CREDENTIALS_SUPPORT,
    STAFF_CREDENTIALS_VALUER,
    build_session,
)

load_dotenv()

# ──────────────────────────────────────────────────────────
# Paths & config
# ──────────────────────────────────────────────────────────
_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_BASE_DIR, "data", "saved_tokens.json")
PID_FILE   = os.path.join(_BASE_DIR, "data", "daemon.pid")
LOG_FILE   = os.path.join(_BASE_DIR, "data", "daemon.log")

REFRESH_BEFORE = 10 * 60   # act when token expires within this many seconds
POLL_INTERVAL  = 60        # seconds between token checks

TELEGRAM_API   = "https://api.telegram.org"
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
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
# Logging — stdout + rotating file
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
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _decode_exp(token: str) -> Optional[float]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ──────────────────────────────────────────────────────────
# Telegram helper (send only — no getUpdates)
# ──────────────────────────────────────────────────────────
def _tg_send(tg: requests.Session, chat_id: int, text: str) -> None:
    try:
        resp = tg.post(
            f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if not resp.ok:
            logger.warning("Telegram send failed for %s: %s", chat_id, resp.text[:200])
    except Exception as e:
        logger.warning("Telegram error for %s: %s", chat_id, e)


def _tg_broadcast(tg: requests.Session, text: str) -> None:
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — cannot send Telegram message.")
        return
    for chat_id in ALLOWED_IDS:
        _tg_send(tg, chat_id, text)


# ──────────────────────────────────────────────────────────
# Refresh-token call
# ──────────────────────────────────────────────────────────
def _try_refresh_token(
    api:           requests.Session,
    access_token:  str,
    jwt_token:     str,
    refresh_token: str,
) -> Optional[Tuple[str, str, str]]:
    """
    POST /auth/refresh-token with the stored refresh_token.
    Returns (new_access_token, new_jwt, new_refresh_token) on success, else None.
    """
    try:
        resp = api.post(
            f"{AUTH_BASE_URL}/refresh-token",
            headers={
                "Authorization": f"Bearer {access_token}",
                "JWTAUTH":       f"Bearer {jwt_token}",
            },
            json={"refresh_token": refresh_token},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            logger.debug("refresh-token HTTP %d: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        details   = data.get("details", data)   # handle both nested and flat responses
        new_at    = details.get("access_token")
        new_jwt   = details.get("jwt")
        new_rt    = details.get("refresh_token") or refresh_token  # keep old if not rotated
        if new_at and new_jwt:
            return new_at, new_jwt, new_rt
        logger.debug("refresh-token response missing tokens. Keys: %s", list(data.keys()))
    except Exception as e:
        logger.debug("refresh-token error: %s", e)
    return None


# ──────────────────────────────────────────────────────────
# Process management
# ──────────────────────────────────────────────────────────
def _kill_previous_instance() -> None:
    """Kill any previously running daemon instance recorded in the PID file."""
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
        pass   # already gone
    except Exception as e:
        logger.warning("Could not stop previous daemon (PID %d): %s", old_pid, e)


# ──────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────
_stop = False


def _handle_signal(sig, _frame):
    global _stop
    logger.info("Signal %s received — shutting down.", sig)
    _stop = True


def run() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    _kill_previous_instance()

    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    api_sess = _api_session()
    tg_sess  = _tg_session()

    # alerted: {cred_type: expiry_bucket} — prevents repeated refresh attempts per expiry window
    alerted: Dict[str, int] = {}

    logger.info("=" * 60)
    logger.info("Token refresh daemon started  (PID %d)", os.getpid())
    logger.info("Cache   : %s", CACHE_FILE)
    logger.info("Refresh : %d min before expiry", REFRESH_BEFORE // 60)
    logger.info("Poll    : every %d s", POLL_INTERVAL)
    logger.info("Mode    : refresh-token only — no OTP prompts")
    logger.info("=" * 60)

    last_token_check = 0.0

    while not _stop:
        now = time.time()

        if now - last_token_check >= POLL_INTERVAL:
            last_token_check = now
            cache         = _load_cache()
            cache_changed = False

            for cred_type, entry in list(cache.items()):
                access_token  = entry.get("access_token", "")
                jwt_token     = entry.get("jwt", "")
                refresh_token = entry.get("refresh_token", "")
                exp           = entry.get("expires_at") or _decode_exp(jwt_token)
                label         = CRED_LABELS.get(cred_type, cred_type)

                if not exp:
                    logger.warning("[%s] Cannot decode expiry — skipping.", cred_type)
                    continue

                secs_left = exp - now
                bucket    = int(exp // 60)

                if secs_left > REFRESH_BEFORE:
                    logger.debug(
                        "[%s] OK — expires in %dm %ds (%s).",
                        cred_type, int(secs_left // 60), int(secs_left % 60), _fmt_ts(exp),
                    )
                    continue

                # Already handled this expiry window
                if alerted.get(cred_type) == bucket:
                    continue

                if secs_left <= 0:
                    log_msg = f"already expired ({_fmt_ts(exp)})"
                else:
                    mins_left = max(int(secs_left // 60), 0)
                    log_msg   = f"expiring in {mins_left}m ({_fmt_ts(exp)})"

                logger.info("[%s] Token %s — attempting refresh…", cred_type, log_msg)

                if not refresh_token:
                    logger.warning(
                        "[%s] No refresh_token stored — cannot refresh. "
                        "Authenticate via the bot to save a refresh_token.",
                        cred_type,
                    )
                    alerted[cred_type] = bucket
                    continue

                result = _try_refresh_token(api_sess, access_token, jwt_token, refresh_token)
                if result:
                    new_access, new_jwt, new_rt = result
                    new_exp = _decode_exp(new_jwt) or (now + 3600)
                    cache[cred_type] = {
                        "access_token":  new_access,
                        "jwt":           new_jwt,
                        "refresh_token": new_rt,
                        "expires_at":    new_exp,
                    }
                    cache_changed      = True
                    alerted[cred_type] = int(new_exp // 60)
                    logger.info(
                        "[%s] ✅ Token refreshed. New expiry: %s", cred_type, _fmt_ts(new_exp)
                    )
                    _tg_broadcast(
                        tg_sess,
                        f"✅ *Token auto-refreshed*\n\n"
                        f"*Profile:* {label}\n"
                        f"*New expiry:* {_fmt_ts(new_exp)}",
                    )
                else:
                    logger.warning(
                        "[%s] ❌ refresh-token call failed. Will retry next cycle.", cred_type
                    )
                    alerted[cred_type] = bucket

            if cache_changed:
                _save_cache(cache)

        # Sleep between checks
        elapsed   = time.time() - last_token_check
        sleep_for = max(1, POLL_INTERVAL - elapsed)
        for _ in range(int(sleep_for)):
            if _stop:
                break
            time.sleep(1)

    logger.info("Token refresh daemon stopped.")
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    run()
