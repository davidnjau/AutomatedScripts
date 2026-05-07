#!/usr/bin/env python3
"""
token_refresh_daemon.py
=======================
Always-on daemon that monitors data/saved_tokens.json and keeps every
credential profile authenticated.

Behaviour for each credential profile in the cache:
  1. If the token expires in more than REFRESH_BEFORE (10 min) → no action.
  2. If the token expires within REFRESH_BEFORE:
       a. Try a silent refresh via POST /acl/api/v1/auth/refresh (no OTP needed
          if the API supports it).  On success the cache is updated in-place.
       b. If the refresh endpoint is not supported / fails → send a Telegram
          alert to every ALLOWED_TELEGRAM_ID prompting the user to tap
          🔑 Refresh Auth in the bot.
  3. Each expiry window triggers at most one refresh / one alert (no spam).

Usage:
    python token_refresh_daemon.py              # foreground
    nohup python token_refresh_daemon.py &      # background

The bot menu button "🔄 Token Daemon" launches this script as a subprocess
and writes its PID to data/daemon.pid.

Environment variables (read from .env):
    TELEGRAM_BOT_TOKEN       — required for Telegram alerts
    ALLOWED_TELEGRAM_IDS     — comma-separated chat IDs to alert
"""

import base64
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# ──────────────────────────────────────────────────────────
# Paths & config
# ──────────────────────────────────────────────────────────
_BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE     = os.path.join(_BASE_DIR, "data", "saved_tokens.json")
PID_FILE       = os.path.join(_BASE_DIR, "data", "daemon.pid")
LOG_FILE       = os.path.join(_BASE_DIR, "data", "daemon.log")

REFRESH_BEFORE = 10 * 60   # seconds before expiry to act (10 minutes)
POLL_INTERVAL  = 60        # seconds between cache checks

AUTH_BASE_URL  = "https://ardhisasa-api.lands.go.ke/acl/api/v1/auth"
TELEGRAM_API   = "https://api.telegram.org"

BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_IDS = [
    int(x.strip())
    for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",")
    if x.strip()
]

CRED_LABELS: Dict[str, str] = {
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

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)

logger = logging.getLogger("token_refresh_daemon")
logger.setLevel(logging.INFO)
logger.addHandler(_fh)
logger.addHandler(_sh)


# ──────────────────────────────────────────────────────────
# HTTP session
# ──────────────────────────────────────────────────────────
def _build_session() -> requests.Session:
    session = requests.Session()
    retry   = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  "en-GB,en-US;q=0.9,en;q=0.8",
        "Content-Type":     "application/json",
        "Origin":           "https://ardhisasa.lands.go.ke",
        "Referer":          "https://ardhisasa.lands.go.ke/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
    })
    return session


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
    """Return the `exp` claim from a JWT, or None if undecodable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def _fmt_exp(exp_ts: float) -> str:
    return datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ──────────────────────────────────────────────────────────
# Silent token refresh
# ──────────────────────────────────────────────────────────
def _try_refresh(
    session:      requests.Session,
    access_token: str,
    jwt_token:    str,
) -> Optional[Tuple[str, str]]:
    """
    POST /auth/refresh with the current bearer tokens.
    Returns (new_access_token, new_jwt) on success, None if unsupported or failed.
    """
    try:
        resp = session.post(
            f"{AUTH_BASE_URL}/refresh",
            headers={
                "Authorization": f"Bearer {access_token}",
                "JWTAUTH":       f"Bearer {jwt_token}",
            },
            json={},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            logger.debug("Refresh endpoint returned HTTP %s — likely unsupported.", resp.status_code)
            return None
        data      = resp.json()
        new_at    = data.get("details", {}).get("access_token") or data.get("access_token")
        new_jwt   = data.get("details", {}).get("jwt")          or data.get("jwt")
        if new_at and new_jwt:
            return new_at, new_jwt
        logger.debug("Refresh response did not contain tokens. Keys: %s", list(data.keys()))
    except Exception as e:
        logger.debug("Refresh request error: %s", e)
    return None


# ──────────────────────────────────────────────────────────
# Telegram alert
# ──────────────────────────────────────────────────────────
def _send_alert(session: requests.Session, text: str) -> None:
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — skipping alert.")
        return
    if not ALLOWED_IDS:
        logger.warning("ALLOWED_TELEGRAM_IDS not set — skipping alert.")
        return
    for chat_id in ALLOWED_IDS:
        try:
            resp = session.post(
                f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=15,
            )
            if resp.ok:
                logger.info("Alert sent → chat_id=%s", chat_id)
            else:
                logger.warning("Telegram send failed for chat_id=%s: %s", chat_id, resp.text[:200])
        except Exception as e:
            logger.warning("Telegram error for chat_id=%s: %s", chat_id, e)


# ──────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────
_stop = False


def _handle_signal(sig, _frame):
    global _stop
    logger.info("Signal %s received — shutting down gracefully.", sig)
    _stop = True


def run() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    # Write PID file so the bot can track/kill this process
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    session = _build_session()

    # alerted[cred_type] = expiry_bucket already handled (prevents re-alerting
    # every 60 s for the same expiry window)
    alerted: Dict[str, int] = {}

    logger.info("=" * 60)
    logger.info("Token refresh daemon started  (PID %d)", os.getpid())
    logger.info("Cache     : %s", CACHE_FILE)
    logger.info("Refresh   : %d min before expiry", REFRESH_BEFORE // 60)
    logger.info("Poll      : every %d s", POLL_INTERVAL)
    logger.info("=" * 60)

    while not _stop:
        cache = _load_cache()
        now   = time.time()
        cache_changed = False

        if not cache:
            logger.debug("Cache empty — nothing to monitor.")
        else:
            for cred_type, entry in list(cache.items()):
                access_token = entry.get("access_token", "")
                jwt_token    = entry.get("jwt", "")
                exp          = entry.get("expires_at") or _decode_exp(jwt_token)
                label        = CRED_LABELS.get(cred_type, cred_type)

                if not exp:
                    logger.warning("[%s] Cannot decode expiry — skipping.", cred_type)
                    continue

                secs_left = exp - now

                if secs_left <= 0:
                    logger.info("[%s] Token expired at %s.", cred_type, _fmt_exp(exp))
                    continue

                if secs_left > REFRESH_BEFORE:
                    logger.debug(
                        "[%s] OK — expires in %dm %ds (%s).",
                        cred_type, int(secs_left // 60), int(secs_left % 60), _fmt_exp(exp),
                    )
                    continue

                # ── Within refresh window ─────────────────────────
                mins_left = max(int(secs_left // 60), 0)
                # One-minute resolution bucket: only act once per expiry window
                bucket = int(exp // 60)
                if alerted.get(cred_type) == bucket:
                    continue

                logger.info(
                    "[%s] Expiring in %d min (%s) — attempting silent refresh…",
                    cred_type, mins_left, _fmt_exp(exp),
                )

                result = _try_refresh(session, access_token, jwt_token)

                if result:
                    new_access, new_jwt = result
                    new_exp = _decode_exp(new_jwt) or (now + 3600)
                    cache[cred_type] = {
                        "access_token": new_access,
                        "jwt":          new_jwt,
                        "expires_at":   new_exp,
                    }
                    cache_changed = True
                    alerted[cred_type] = int(new_exp // 60)
                    logger.info(
                        "[%s] ✅ Refreshed successfully. New expiry: %s",
                        cred_type, _fmt_exp(new_exp),
                    )
                else:
                    # Refresh not supported / failed — alert the user
                    logger.warning(
                        "[%s] Silent refresh unavailable. Sending Telegram alert.", cred_type
                    )
                    _send_alert(
                        session,
                        f"⚠️ *Token Expiring Soon*\n\n"
                        f"*Profile:* {label}\n"
                        f"*Expires at:* {_fmt_exp(exp)} (in {mins_left} min)\n\n"
                        f"Tap *🔑 Refresh Auth* in the bot to re-authenticate before it expires.",
                    )
                    alerted[cred_type] = bucket

        if cache_changed:
            _save_cache(cache)

        # Sleep in small increments so SIGTERM is handled promptly
        for _ in range(POLL_INTERVAL):
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
