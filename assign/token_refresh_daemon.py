#!/usr/bin/env python3
"""
token_refresh_daemon.py
=======================
Always-on daemon that monitors data/saved_tokens.json and automatically
re-authenticates every credential profile before its token expires.

Renewal strategy (changed from refresh-token to system-login):
  POST /acl/api/v1/auth/system-login   (ardhisasa_auth.system_login)
      Body: {"username", "password", "usertype"}

  The daemon now RE-AUTHENTICATES from scratch (OTP-free, OAuth2 password
  grant) rather than exchanging the cached refresh_token. This was switched
  deliberately: the server rotates refresh tokens (ROTATE_REFRESH_TOKEN) and
  allows a single access token per account (OAUTH_SINGLE_ACCESS_TOKEN), so
  whenever an account was also used elsewhere the daemon's cached
  refresh/access token got revoked and the refresh-token call returned 403
  (the staff_valuer outages). A fresh username/password login is immune to
  that — it needs no valid prior token, so an already-expired or revoked
  credential still re-auths cleanly, and no account-sharing can lock the bot
  out for more than one renewal cycle. (system-login itself returns a
  refresh_token, which is cached but no longer relied on for renewal.)

  For each credential found in saved_tokens.json the daemon schedules an
  APScheduler one-shot job to fire REFRESH_BEFORE seconds before expiry.
  After a successful renewal the job reschedules itself for the new expiry.
  A periodic scan runs every SCAN_INTERVAL seconds; an expired credential is
  re-authed immediately. Renewal uses the username/password in CRED_MAP (the
  same env credentials the bot's OTP flows use), so it needs no OTP and no
  human step.

  A failed refresh (network blip, transient 5xx, etc.) is retried up to
  RETRY_MAX times, RETRY_INTERVAL seconds apart, rather than giving up
  immediately. Once those fast retries are exhausted, a single Telegram alert
  goes to admins + ICT admins only (the ops audience — see _ops_recipients) —
  but the daemon keeps retrying in the background at a slower COOLDOWN_INTERVAL
  cadence rather than stopping, since the upstream Ardhisasa auth API is
  sometimes intermittently flaky for extended periods and recovers on its own.
  No further alert is sent for that incident unless it's STILL failing after
  ESCALATION_AFTER, which sends one follow-up and then goes quiet again.
  (An earlier version of this logic incorrectly cleared its own bucket-tracking
  state on give-up, which caused _scan_cache to misread the unchanged, still-
  failing token as "new tokens detected" every SCAN_INTERVAL and silently
  restart the whole retry cycle — re-sending the alert every ~10 minutes for
  as long as a credential stayed broken. Fixed: _scheduled is no longer
  cleared on give-up, so the background cooldown retry above is now the one
  and only mechanism that keeps trying, and the alert fires once per incident.)

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
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ardhisasa_auth import (
    PUBLIC_CREDENTIALS,
    STAFF_CREDENTIALS_ICT,
    STAFF_CREDENTIALS_SUPPORT,
    STAFF_CREDENTIALS_VALUER,
    build_session,
    decode_jwt_exp,
    system_login,
)

# cred_type → the username/password/usertype profile used for OTP-free
# re-authentication via system_login (see _refresh_job). Keeps the daemon's
# renewal independent of a still-valid cached token, so an expired/revoked
# credential is re-authed from scratch rather than failing to "refresh."
CRED_MAP = {
    "publicuser":   PUBLIC_CREDENTIALS,
    "staff":        STAFF_CREDENTIALS_ICT,
    "staff2":       STAFF_CREDENTIALS_SUPPORT,
    "staff_valuer": STAFF_CREDENTIALS_VALUER,
}

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

# Bounded fast retry on a failed refresh — a transient network/API blip shouldn't
# permanently disable auto-refresh for a credential.
RETRY_MAX       = 5    # fast attempts (RETRY_INTERVAL apart) before backing off
RETRY_INTERVAL  = 90   # seconds between fast retry attempts
# After the fast retries are exhausted, keep retrying quietly (no repeat
# alerts) at this slower cadence — the upstream Ardhisasa auth API is
# sometimes intermittently flaky for extended periods and recovers on its own
# without a human re-authenticating; see _refresh_job's give-up branch.
COOLDOWN_INTERVAL   = 10 * 60   # 10 minutes between background retries once failed
# If a credential is STILL failing this long after the initial alert, send one
# follow-up escalation (and only one, until it recovers) — catches a genuinely
# stuck credential (e.g. one that truly needs manual OTP) without re-alerting
# on every quiet background retry in between.
ESCALATION_AFTER    = 2 * 60 * 60   # 2 hours

TELEGRAM_API = "https://api.telegram.org"
BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_IDS: List[int] = [
    int(x.strip())
    for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",")
    if x.strip()
]
# Allowed users who must never receive pushed alerts (e.g. an inquiry-only
# user) — same REPORT_EXCLUDED_TELEGRAM_IDS env var as common.NO_REPORT_IDS;
# the daemon is a separate process, so it reads it independently here.
NO_REPORT_IDS: set = {
    int(x.strip())
    for x in os.getenv("REPORT_EXCLUDED_TELEGRAM_IDS", "").split(",")
    if x.strip()
}
# Auth/token-failure alerts go to admins + ICT admins only (the ops/monitoring
# audience) — NOT every allowed user. Admins come from the env; ICT admins come
# from saved_roles.json (same role store common.py uses), read fresh each alert
# so a role grant takes effect without a daemon restart.
ADMIN_IDS: set = {
    int(x.strip())
    for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
    if x.strip()
}
ROLES_FILE = os.path.join(_BASE_DIR, "data", "saved_roles.json")

CRED_LABELS = {
    "publicuser":   "👤 Public User",
    "staff":        "🏢 ICT",
    "staff2":       "🏢 Support Reg",
    "staff_valuer": "🏢 Staff Valuer",
}

# ──────────────────────────────────────────────────────────
# Logging — stdout + file
# ──────────────────────────────────────────────────────────
# Single FileHandler only — NOT also a StreamHandler to stdout. bot.py's
# _daemon_start() launches this script as a subprocess with stdout/stderr
# redirected into this same LOG_FILE (a crash-safety net for tracebacks
# printed outside the logger); a StreamHandler here would duplicate every
# log line into that same file via both paths at once.
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
_fh  = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
logger = logging.getLogger("token_refresh_daemon")
logger.setLevel(logging.INFO)
logger.addHandler(_fh)


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


def _ict_admin_ids() -> set:
    """Telegram ids granted the ICT Admin role (saved_roles.json), read fresh
    each call so a role change takes effect without a daemon restart."""
    try:
        with open(ROLES_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    return {int(uid) for uid, roles in data.items() if "ict_admin" in (roles or [])}


def _ops_recipients() -> List[int]:
    """Who receives auth/token-failure alerts: admins + ICT admins. Falls back
    to all allowed (minus report-excluded) if neither is configured, so an
    alert never silently goes nowhere."""
    ids = set(ADMIN_IDS) | _ict_admin_ids()
    if not ids:
        return [c for c in ALLOWED_IDS if c not in NO_REPORT_IDS]
    return sorted(ids)


def _tg_alert_ops(text: str) -> None:
    # Auth/token alerts go to admins + ICT admins only (not every allowed user).
    for chat_id in _ops_recipients():
        _tg_send(chat_id, text)


# ──────────────────────────────────────────────────────────
# Refresh-token API call
# ──────────────────────────────────────────────────────────
_api_sess = _api_session()


def _reauth(cred_type: str, creds: dict) -> Optional[Tuple[str, str, str]]:
    """
    Re-authenticate a credential from scratch via the OTP-free system-login
    (ardhisasa_auth.system_login). Returns (access_token, jwt, refresh_token)
    on success, or None on failure — logging the real reason at WARNING (the
    logger runs at INFO, so this is the only record of WHY a renewal failed;
    without it the "attempt N/5" lines give no clue). Unlike the old
    refresh-token exchange this doesn't need a valid prior token and isn't
    affected by refresh-token rotation.
    """
    try:
        tokens, refresh = system_login(_api_sess, creds)
        return tokens.access_token, tokens.jwt, refresh
    except Exception as e:
        # HTTPError carries the status/body; everything else is a transport or
        # parse failure. Keep it to ~200 chars so daemon.log stays readable.
        detail = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            detail = f" HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        logger.warning("[%s] system-login failed: %s%s", cred_type, type(e).__name__, detail)
        return None


# ──────────────────────────────────────────────────────────
# Scheduler and job tracking
# ──────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="UTC")

# Track which (cred_type, expiry_bucket) pairs already have a scheduled job
# so we don't double-schedule for the same expiry window.
_scheduled: Dict[str, int] = {}   # cred_type → expiry_bucket (int(exp // 60))

# Credentials currently in a failed/cooldown state (fast retries exhausted at
# least once since the last success or genuinely new tokens). While a
# credential is in here, _refresh_job keeps retrying itself at COOLDOWN_INTERVAL
# — it is NOT "stopped until a human intervenes" despite the historical log
# wording; the upstream API recovers on its own often enough that requiring
# manual re-auth for every blip would be worse than a quiet background retry.
_failed: set = set()

# In-flight retry attempt count per credential, since its first failed refresh this expiry window.
_retry_count: Dict[str, int] = {}

# When each currently-failed credential's incident started (time.time()), so
# the escalation alert fires once if it's STILL down after ESCALATION_AFTER —
# without this, every incident looks identical regardless of how long it's lasted.
_failed_since: Dict[str, float] = {}

# Credentials that have already received the one-time escalation alert for
# their current incident — prevents re-sending it on every subsequent
# cooldown-retry failure once the threshold has been crossed.
_escalated: set = set()


def _reschedule_in(cred_type: str, delay_seconds: int) -> None:
    """Schedule a one-off _refresh_job retry delay_seconds from now, reusing
    the credential's job id (replace_existing=True) so it supersedes whatever
    was previously scheduled for it — used for both the fast RETRY_INTERVAL
    retries and the slower COOLDOWN_INTERVAL background retries after give-up."""
    scheduler.add_job(
        _refresh_job,
        trigger=DateTrigger(run_date=datetime.now(tz=timezone.utc) + timedelta(seconds=delay_seconds)),
        id=f"refresh_{cred_type}",
        args=[cred_type],
        replace_existing=True,
        misfire_grace_time=300,
    )


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

    creds = CRED_MAP.get(cred_type)
    if not creds:
        logger.warning("[%s] No credential profile — skipping (cannot system-login).", cred_type)
        return

    # Re-authenticate from scratch via the OTP-free system-login (username/
    # password) rather than exchanging the cached refresh_token. This is
    # immune to the server's refresh-token ROTATION (which was revoking a
    # cached token whenever the account was used elsewhere — the staff_valuer
    # 403 incidents), and needs no valid prior token, so an already-expired or
    # revoked credential still re-auths cleanly.
    result = _reauth(cred_type, creds)
    if result:
        new_access, new_jwt, new_rt = result
        new_exp = _decode_exp(new_jwt) or (time.time() + 3600)

        cache = _load_cache()
        cache[cred_type] = {
            "access_token":  new_access,
            "jwt":           new_jwt,
            "refresh_token": new_rt,
            "expires_at":    new_exp,
        }
        _save_cache(cache)

        # NOTE: _scheduled is deliberately NOT popped here — _schedule_refresh
        # below sets it to the new bucket itself. Popping it was the root cause
        # of a past bug (see the give-up branch's comment).
        _failed.discard(cred_type)      # clear any previous failure flag
        _retry_count.pop(cred_type, None)
        _failed_since.pop(cred_type, None)
        _escalated.discard(cred_type)
        logger.info("[%s] ✅ Token re-authenticated (system-login). New expiry: %s", cred_type, _fmt_ts(new_exp))

        # Schedule next refresh for the new expiry
        _schedule_refresh(cred_type, new_exp)
    elif cred_type in _failed:
        # Already past the fast-retry burst and in the quiet cooldown phase —
        # this failure does NOT restart the fast-attempt counter (there is
        # none to restart; it was popped when we first gave up). Just log and
        # reschedule at COOLDOWN_INTERVAL, escalating once if this has dragged
        # on long enough, but never re-sending the initial alert.
        if (cred_type not in _escalated
                and time.time() - _failed_since.get(cred_type, time.time()) >= ESCALATION_AFTER):
            _escalated.add(cred_type)
            down_for_min = int((time.time() - _failed_since[cred_type]) // 60)
            logger.warning("[%s] Still failing after %dm — escalating.", cred_type, down_for_min)
            _tg_alert_ops(
                f"⚠️ *Token Still Not Refreshed*\n\n"
                f"*{label}* (`{cred_type}`) has been failing to auto-refresh for "
                f"over {down_for_min} minutes. Please re-authenticate manually via "
                f"*🔑 Refresh Auth*."
            )
        else:
            logger.info(
                "[%s] Still failing (quiet retry, no alert) — next attempt in %dm.",
                cred_type, COOLDOWN_INTERVAL // 60,
            )
        _reschedule_in(cred_type, COOLDOWN_INTERVAL)
    else:
        attempt = _retry_count.get(cred_type, 0) + 1
        if attempt <= RETRY_MAX:
            _retry_count[cred_type] = attempt
            logger.warning(
                "[%s] ❌ Refresh attempt %d/%d failed — retrying in %ds.",
                cred_type, attempt, RETRY_MAX, RETRY_INTERVAL,
            )
            _reschedule_in(cred_type, RETRY_INTERVAL)
        else:
            # Fast retries exhausted. This does NOT mean "stop and wait for a
            # human" (the credential still gets retried — see the `cred_type
            # in _failed` branch above, taken on every subsequent failure);
            # it means "alert once, then go quiet." _scheduled is deliberately
            # left pointing at the current (still-stale) bucket — unlike a
            # past version of this code, which popped it here and caused
            # _scan_cache to misread the unchanged bucket as "new tokens
            # detected" every 5 minutes, silently resetting this whole state
            # machine and re-sending this alert on every cycle for as long as
            # the credential stayed broken.
            _retry_count.pop(cred_type, None)
            _failed.add(cred_type)
            _failed_since[cred_type] = time.time()
            logger.warning(
                "[%s] ❌ Refresh failed after %d attempts — backing off to a quiet "
                "retry every %dm (will keep trying automatically; the upstream API "
                "is sometimes intermittently flaky and recovers on its own).",
                cred_type, RETRY_MAX, COOLDOWN_INTERVAL // 60,
            )
            _tg_alert_ops(
                f"⚠️ *Token Refresh Failed*\n\n"
                f"*{label}* (`{cred_type}`) could not be auto-refreshed after {RETRY_MAX} attempts.\n"
                f"It will keep retrying automatically every {COOLDOWN_INTERVAL // 60} minutes; "
                f"re-authenticate manually via *🔑 Refresh Auth* if it's still failing later."
            )
            _reschedule_in(cred_type, COOLDOWN_INTERVAL)


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
