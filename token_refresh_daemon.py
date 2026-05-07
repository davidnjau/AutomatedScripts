#!/usr/bin/env python3
"""
token_refresh_daemon.py
=======================
Always-on daemon that monitors data/saved_tokens.json and automatically
re-authenticates every credential profile before its token expires.

Refresh strategy (in order):
  1. Try POST /auth/refresh (silent, no OTP required).
  2. If that fails, start the full re-auth flow:
       a. POST /login  → the API sends an OTP to the registered device.
       b. Write {cred_type: {triggered_at, cookies}} to data/pending_otp.json
          so the bot can complete the OTP step.
       c. Broadcast a Telegram message asking the user to send `/otp CODE`
          directly in the bot chat.
       d. The bot's /otp command handler calls POST /otpverify and saves tokens.

NOTE: The daemon does NOT poll Telegram getUpdates.  Both the main bot and
this daemon share the same bot token; if both polled getUpdates they would
compete for updates and OTP replies would be lost.  File-based IPC avoids
this conflict entirely.

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
_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE       = os.path.join(_BASE_DIR, "data", "saved_tokens.json")
PENDING_OTP_FILE = os.path.join(_BASE_DIR, "data", "pending_otp.json")
PID_FILE         = os.path.join(_BASE_DIR, "data", "daemon.pid")
LOG_FILE         = os.path.join(_BASE_DIR, "data", "daemon.log")

REFRESH_BEFORE = 10 * 60   # act when token expires within this many seconds
POLL_INTERVAL  = 60        # seconds between token checks
OTP_TIMEOUT    = 5 * 60    # seconds before a pending OTP request is abandoned

TELEGRAM_API   = "https://api.telegram.org"
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_IDS: List[int] = [
    int(x.strip())
    for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",")
    if x.strip()
]

CRED_MAP = {
    "publicuser":   PUBLIC_CREDENTIALS,
    "staff":        STAFF_CREDENTIALS_ICT,
    "staff2":       STAFF_CREDENTIALS_SUPPORT,
    "staff_valuer": STAFF_CREDENTIALS_VALUER,
}
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
# Pending-OTP file helpers  (file-based IPC with bot.py)
# ──────────────────────────────────────────────────────────
def _load_pending_otp() -> Dict:
    try:
        with open(PENDING_OTP_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pending_otp(data: Dict) -> None:
    os.makedirs(os.path.dirname(PENDING_OTP_FILE), exist_ok=True)
    with open(PENDING_OTP_FILE, "w") as f:
        json.dump(data, f, indent=2)


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
# Silent token refresh (no OTP required)
# ──────────────────────────────────────────────────────────
def _try_silent_refresh(
    api: requests.Session,
    access_token: str,
    jwt_token: str,
) -> Optional[Tuple[str, str]]:
    """POST /auth/refresh. Returns (new_access_token, new_jwt) or None."""
    try:
        resp = api.post(
            f"{AUTH_BASE_URL}/refresh",
            headers={
                "Authorization": f"Bearer {access_token}",
                "JWTAUTH":       f"Bearer {jwt_token}",
            },
            json={},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            return None
        data    = resp.json()
        new_at  = data.get("details", {}).get("access_token") or data.get("access_token")
        new_jwt = data.get("details", {}).get("jwt")          or data.get("jwt")
        if new_at and new_jwt:
            return new_at, new_jwt
    except Exception as e:
        logger.debug("Silent refresh error: %s", e)
    return None


# ──────────────────────────────────────────────────────────
# Full re-auth — trigger login and hand off OTP to the bot
# ──────────────────────────────────────────────────────────
def _trigger_login_for_bot(
    api:       requests.Session,
    tg:        requests.Session,
    cred_type: str,
) -> bool:
    """
    POST /login for cred_type, write to pending_otp.json so the bot can
    complete the OTP step via the /otp command.
    Returns True if login was triggered successfully.
    """
    creds = CRED_MAP.get(cred_type)
    if not creds:
        logger.error("[%s] No credentials found.", cred_type)
        return False

    label = CRED_LABELS.get(cred_type, cred_type)
    logger.info("[%s] Triggering login — OTP will be completed via bot /otp command.", cred_type)

    try:
        resp = api.post(
            f"{AUTH_BASE_URL}/login",
            json={
                "username": creds["username"],
                "password": creds["password"],
                "usertype": creds["usertype"],
                "otpcode":  "",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") is False and "error" in data:
            raise RuntimeError(data.get("error") or data.get("message"))
    except Exception as e:
        logger.error("[%s] Login failed: %s", cred_type, e)
        _tg_broadcast(
            tg,
            f"❌ *Auto-refresh login failed for {label}*\n\n"
            f"Error: `{e}`\n\n"
            f"Please tap *🔑 Refresh Auth* in the bot to re-authenticate manually.",
        )
        return False

    # Store cookie state so the bot can resume the session for /otpverify
    cookies = dict(api.cookies)
    pending = _load_pending_otp()
    pending[cred_type] = {
        "triggered_at": time.time(),
        "cookies":      cookies,
    }
    _save_pending_otp(pending)

    logger.info("[%s] Login OK — OTP dispatched to registered device.", cred_type)
    _tg_broadcast(
        tg,
        f"🔐 *OTP Required — {label}*\n\n"
        f"An OTP has been sent to the registered device.\n\n"
        f"Reply with:\n"
        f"`/otp YOUR_CODE`\n\n"
        f"_(e.g._ `/otp 123456`_)_\n\n"
        f"Or tap *🔑 Refresh Auth* to handle it manually.",
    )
    return True


# ──────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────
_stop = False


def _handle_signal(sig, _frame):
    global _stop
    logger.info("Signal %s received — shutting down.", sig)
    _stop = True


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


def run() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    _kill_previous_instance()

    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    api_sess = _api_session()
    tg_sess  = _tg_session()

    # alerted: {cred_type: expiry_bucket} — prevents repeated login triggers per expiry window
    alerted: Dict[str, int] = {}

    logger.info("=" * 60)
    logger.info("Token refresh daemon started  (PID %d)", os.getpid())
    logger.info("Cache     : %s", CACHE_FILE)
    logger.info("Pending   : %s", PENDING_OTP_FILE)
    logger.info("Refresh   : %d min before expiry", REFRESH_BEFORE // 60)
    logger.info("Poll      : every %d s", POLL_INTERVAL)
    logger.info("OTP mode  : file-based IPC — user sends /otp CODE in bot chat")
    logger.info("=" * 60)

    last_token_check = 0.0

    while not _stop:
        now = time.time()

        if now - last_token_check >= POLL_INTERVAL:
            last_token_check = now
            cache         = _load_cache()
            cache_changed = False
            pending_otp   = _load_pending_otp()
            pending_changed = False

            # ── Prune timed-out pending OTP entries ───────
            for ct in list(pending_otp):
                age = now - pending_otp[ct].get("triggered_at", 0)
                if age > OTP_TIMEOUT:
                    logger.warning(
                        "[%s] OTP not submitted within %d min — clearing pending entry.",
                        ct, OTP_TIMEOUT // 60,
                    )
                    _tg_broadcast(
                        tg_sess,
                        f"⏰ *OTP timed out for {CRED_LABELS.get(ct, ct)}*\n\n"
                        f"No `/otp` response received within {OTP_TIMEOUT // 60} minutes.\n"
                        f"The daemon will retry on the next check cycle.",
                    )
                    del pending_otp[ct]
                    pending_changed = True
                    alerted.pop(ct, None)   # allow re-trigger next cycle

            if pending_changed:
                _save_pending_otp(pending_otp)

            # ── Check each cached credential ──────────────
            for cred_type, entry in list(cache.items()):
                if cred_type in pending_otp:
                    logger.debug("[%s] OTP pending — skipping token check.", cred_type)
                    continue

                access_token = entry.get("access_token", "")
                jwt_token    = entry.get("jwt", "")
                exp          = entry.get("expires_at") or _decode_exp(jwt_token)
                label        = CRED_LABELS.get(cred_type, cred_type)

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

                # ── a. Try silent refresh ──────────────────
                result = _try_silent_refresh(api_sess, access_token, jwt_token)
                if result:
                    new_access, new_jwt = result
                    new_exp = _decode_exp(new_jwt) or (now + 3600)
                    cache[cred_type] = {
                        "access_token": new_access,
                        "jwt":          new_jwt,
                        "expires_at":   new_exp,
                    }
                    cache_changed      = True
                    alerted[cred_type] = int(new_exp // 60)
                    logger.info("[%s] ✅ Silent refresh OK. New expiry: %s", cred_type, _fmt_ts(new_exp))
                    _tg_broadcast(
                        tg_sess,
                        f"✅ *Token auto-refreshed*\n\n"
                        f"*Profile:* {label}\n"
                        f"*New expiry:* {_fmt_ts(new_exp)}",
                    )
                    continue

                # ── b. Full OTP re-auth via bot /otp ──────
                triggered = _trigger_login_for_bot(api_sess, tg_sess, cred_type)
                alerted[cred_type] = bucket   # prevent re-trigger until timeout clears it

            if cache_changed:
                _save_cache(cache)

        # Sleep between checks
        elapsed = time.time() - last_token_check
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
