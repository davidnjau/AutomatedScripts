#!/usr/bin/env python3
"""
token_refresh_daemon.py
=======================
Always-on daemon that monitors data/saved_tokens.json and automatically
re-authenticates every credential profile before its token expires.

Refresh strategy (in order):
  1. Try POST /auth/refresh (silent, no OTP) — works if the API supports it.
  2. If that fails, do the full re-auth flow automatically:
       a. POST /login  → the API sends an OTP to the registered device.
       b. Send a Telegram message to every ALLOWED_TELEGRAM_ID asking for the OTP.
       c. Wait for the user to reply with the OTP (polls Telegram getUpdates).
       d. POST /otpverify → save fresh tokens to cache.

The daemon sends one Telegram message per credential per expiry window.
OTP replies are matched to the oldest pending login request.
Pending logins time out after OTP_TIMEOUT seconds (default 5 min).

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
import re
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
_BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE     = os.path.join(_BASE_DIR, "data", "saved_tokens.json")
PID_FILE       = os.path.join(_BASE_DIR, "data", "daemon.pid")
LOG_FILE       = os.path.join(_BASE_DIR, "data", "daemon.log")

REFRESH_BEFORE = 10 * 60   # act when token expires within this many seconds
POLL_INTERVAL  = 60        # seconds between token checks
TG_POLL_TIMEOUT = 20       # seconds for Telegram long-poll per iteration
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
    """requests.Session with retry + browser headers for the Ardhisasa API."""
    sess   = build_session()
    return sess


def _tg_session() -> requests.Session:
    """Lightweight session for Telegram API calls."""
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
# Telegram helpers
# ──────────────────────────────────────────────────────────
def _tg_send(tg: requests.Session, chat_id: int, text: str) -> Optional[int]:
    """Send a message; returns the message_id or None on failure."""
    try:
        resp = tg.post(
            f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if resp.ok:
            return resp.json().get("result", {}).get("message_id")
        logger.warning("Telegram send failed for %s: %s", chat_id, resp.text[:200])
    except Exception as e:
        logger.warning("Telegram error for %s: %s", chat_id, e)
    return None


def _tg_broadcast(tg: requests.Session, text: str) -> None:
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — cannot send message.")
        return
    for chat_id in ALLOWED_IDS:
        _tg_send(tg, chat_id, text)


def _tg_get_updates(tg: requests.Session, offset: int, timeout: int) -> Tuple[List[Dict], int]:
    """
    Long-poll Telegram for new updates.
    Returns (updates, new_offset).
    """
    try:
        resp = tg.get(
            f"{TELEGRAM_API}/bot{BOT_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 10,
        )
        if not resp.ok:
            return [], offset
        updates = resp.json().get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
        return updates, offset
    except Exception as e:
        logger.debug("getUpdates error: %s", e)
        return [], offset


def _tg_skip_pending(tg: requests.Session, offset: int) -> int:
    """Consume all queued updates on startup so old messages are ignored."""
    updates, new_offset = _tg_get_updates(tg, offset, timeout=0)
    if new_offset != offset:
        logger.info("Skipped %d queued Telegram update(s).", len(updates))
    return new_offset


# ──────────────────────────────────────────────────────────
# Silent token refresh (no OTP required)
# ──────────────────────────────────────────────────────────
def _try_silent_refresh(
    api: requests.Session,
    access_token: str,
    jwt_token: str,
) -> Optional[Tuple[str, str]]:
    """
    POST /auth/refresh. Returns (new_access_token, new_jwt) or None.
    """
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
# Full re-auth (login → wait for OTP → verify)
# ──────────────────────────────────────────────────────────
def _trigger_login(
    api:       requests.Session,
    tg:        requests.Session,
    cred_type: str,
) -> Optional[requests.Session]:
    """
    POST /login for cred_type. Broadcasts an OTP prompt via Telegram.
    Returns the api session to use for /otpverify, or None on login failure.
    """
    creds = CRED_MAP.get(cred_type)
    if not creds:
        logger.error("[%s] No credentials found.", cred_type)
        return None

    label = CRED_LABELS.get(cred_type, cred_type)
    logger.info("[%s] Triggering login (OTP flow)…", cred_type)

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
            f"❌ *Auto-refresh failed for {label}*\n\n"
            f"Login error: `{e}`\n\n"
            f"Please tap *🔑 Refresh Auth* to re-authenticate manually.",
        )
        return None

    logger.info("[%s] Login OK — OTP dispatched to registered device.", cred_type)
    _tg_broadcast(
        tg,
        f"🔐 *OTP Required — {label}*\n\n"
        f"An OTP has been sent to the registered device.\n\n"
        f"Please *reply with the OTP code* to complete the token refresh.",
    )
    return api


def _verify_otp(
    api:       requests.Session,
    cred_type: str,
    otp:       str,
) -> Optional[Tuple[str, str]]:
    """POST /otpverify. Returns (access_token, jwt) or None."""
    creds = CRED_MAP[cred_type]
    try:
        resp = api.post(
            f"{AUTH_BASE_URL}/otpverify",
            json={
                "username": creds["username"],
                "password": creds["password"],
                "otpcode":  otp.strip(),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data         = resp.json()
        access_token = data.get("details", {}).get("access_token")
        jwt_token    = data.get("details", {}).get("jwt")
        if access_token and jwt_token:
            return access_token, jwt_token
        logger.warning("[%s] OTP verify response missing tokens. Keys: %s", cred_type, list(data.keys()))
    except Exception as e:
        logger.warning("[%s] OTP verify error: %s", cred_type, e)
    return None


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

    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    api_sess = _api_session()
    tg_sess  = _tg_session()

    # pending_otp: {cred_type: {"session": requests.Session, "login_time": float}}
    pending_otp: Dict[str, Dict] = {}
    # alerted: {cred_type: expiry_bucket} — prevents repeated login triggers per expiry
    alerted: Dict[str, int] = {}
    tg_offset = 0

    logger.info("=" * 60)
    logger.info("Token refresh daemon started  (PID %d)", os.getpid())
    logger.info("Cache     : %s", CACHE_FILE)
    logger.info("Refresh   : %d min before expiry", REFRESH_BEFORE // 60)
    logger.info("Poll      : every %d s", POLL_INTERVAL)
    logger.info("=" * 60)

    if BOT_TOKEN:
        tg_offset = _tg_skip_pending(tg_sess, tg_offset)
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — OTP prompts will not be sent.")

    last_token_check = 0.0

    while not _stop:
        now = time.time()

        # ── 1. Check token cache every POLL_INTERVAL ──────
        if now - last_token_check >= POLL_INTERVAL:
            last_token_check = now
            cache         = _load_cache()
            cache_changed = False

            # Prune pending_otp entries that have timed out
            for ct in list(pending_otp):
                age = now - pending_otp[ct]["login_time"]
                if age > OTP_TIMEOUT:
                    logger.warning("[%s] OTP not received within %ds — abandoning.", ct, OTP_TIMEOUT)
                    _tg_broadcast(
                        tg_sess,
                        f"⏰ *OTP timed out for {CRED_LABELS.get(ct, ct)}*\n\n"
                        f"No OTP was received within {OTP_TIMEOUT // 60} minutes.\n"
                        f"The daemon will retry on the next check.",
                    )
                    del pending_otp[ct]
                    # Clear alerted so the next cycle re-triggers
                    alerted.pop(ct, None)

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
                    status_line = f"*Expired at:* {_fmt_ts(exp)}"
                    log_msg     = "already expired"
                else:
                    mins_left   = max(int(secs_left // 60), 0)
                    status_line = f"*Expires in:* {mins_left} min ({_fmt_ts(exp)})"
                    log_msg     = f"expiring in {mins_left}m"

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
                    cache_changed        = True
                    alerted[cred_type]   = int(new_exp // 60)
                    logger.info("[%s] ✅ Silent refresh OK. New expiry: %s", cred_type, _fmt_ts(new_exp))
                    _tg_broadcast(
                        tg_sess,
                        f"✅ *Token refreshed automatically*\n\n"
                        f"*Profile:* {label}\n"
                        f"*New expiry:* {_fmt_ts(new_exp)}",
                    )
                    continue

                # ── b. Full OTP re-auth ────────────────────
                login_sess = _trigger_login(api_sess, tg_sess, cred_type)
                if login_sess:
                    pending_otp[cred_type] = {
                        "session":    login_sess,
                        "login_time": now,
                    }
                alerted[cred_type] = bucket

            if cache_changed:
                _save_cache(cache)

        # ── 2. Poll Telegram for OTP replies ──────────────
        if not BOT_TOKEN or not pending_otp:
            # Nothing to wait for — short sleep then re-check tokens
            for _ in range(min(10, max(1, int(POLL_INTERVAL - (time.time() - last_token_check))))):
                if _stop:
                    break
                time.sleep(1)
            continue

        updates, tg_offset = _tg_get_updates(tg_sess, tg_offset, timeout=TG_POLL_TIMEOUT)

        for update in updates:
            msg = update.get("message", {})
            if not msg:
                continue
            sender_id = msg.get("from", {}).get("id")
            if ALLOWED_IDS and sender_id not in ALLOWED_IDS:
                continue
            text = (msg.get("text") or "").strip()
            chat_id = msg.get("chat", {}).get("id")

            # Accept any 4-8 digit string as OTP
            if not re.fullmatch(r"\d{4,8}", text):
                continue

            if not pending_otp:
                _tg_send(tg_sess, chat_id, "ℹ️ No pending OTP request at this time.")
                continue

            # Pick the oldest pending cred_type
            cred_type = min(pending_otp, key=lambda c: pending_otp[c]["login_time"])
            label     = CRED_LABELS.get(cred_type, cred_type)
            sess      = pending_otp[cred_type]["session"]

            logger.info("[%s] Received OTP from chat_id=%s — verifying…", cred_type, chat_id)
            result = _verify_otp(sess, cred_type, text)

            if result:
                access_token, jwt_token = result
                new_exp = _decode_exp(jwt_token) or (time.time() + 3600)
                cache   = _load_cache()
                cache[cred_type] = {
                    "access_token": access_token,
                    "jwt":          jwt_token,
                    "expires_at":   new_exp,
                }
                _save_cache(cache)
                alerted[cred_type] = int(new_exp // 60)
                del pending_otp[cred_type]
                logger.info("[%s] ✅ Re-authenticated. New expiry: %s", cred_type, _fmt_ts(new_exp))
                _tg_send(
                    tg_sess, chat_id,
                    f"✅ *Re-authenticated successfully!*\n\n"
                    f"*Profile:* {label}\n"
                    f"*Token expires:* {_fmt_ts(new_exp)}",
                )
            else:
                logger.warning("[%s] OTP verification failed.", cred_type)
                _tg_send(
                    tg_sess, chat_id,
                    f"❌ *Invalid OTP for {label}*\n\n"
                    f"Please reply with the correct OTP code, or tap *🛑 Cancel* to abort.",
                )

    logger.info("Token refresh daemon stopped.")
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    run()
