#!/usr/bin/env python3
"""
token_refresh_daemon.py
=======================
Daemon that watches .ardhisasa_token_cache.json and silently refreshes
each user's tokens 5 minutes before their JWT expires.

Reads the cache on startup — no manual token entry needed.

Usage:
    # Run in the background (survives terminal closure)
    nohup python token_refresh_daemon.py &

    # Or in a dedicated terminal
    python token_refresh_daemon.py

    # Stop it
    kill <pid>          # pid is printed on startup
"""

import json
import logging
import os
import sys
import time

from ardhisasa_auth import (
    AuthTokens,
    _CACHE_FILE,
    _decode_jwt_exp,
    _save_cached_tokens,
    build_session,
    refresh_tokens,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REFRESH_BEFORE_EXPIRY = 5 * 60   # seconds — refresh this long before expiry
POLL_INTERVAL         = 30       # seconds — how often to recheck after a refresh

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("token_refresh_daemon")

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_raw_cache() -> dict:
    if not os.path.exists(_CACHE_FILE):
        return {}
    try:
        with open(_CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Could not read cache file: %s", exc)
        return {}


def _soonest_expiry(cache: dict) -> tuple:
    """Return (username, exp_unix) for the entry whose JWT expires soonest."""
    soonest_user = None
    soonest_exp  = None
    for username, entry in cache.items():
        exp = _decode_jwt_exp(entry.get("jwt", ""))
        if exp is None:
            continue
        if soonest_exp is None or exp < soonest_exp:
            soonest_exp  = exp
            soonest_user = username
    return soonest_user, soonest_exp


# ---------------------------------------------------------------------------
# Refresh logic
# ---------------------------------------------------------------------------

def _refresh_all_due(session, cache: dict) -> None:
    """Refresh every entry whose token is due (within REFRESH_BEFORE_EXPIRY)."""
    now = time.time()
    for username, entry in cache.items():
        exp = _decode_jwt_exp(entry.get("jwt", ""))
        if exp is None:
            continue
        if (exp - now) > REFRESH_BEFORE_EXPIRY:
            continue  # not due yet

        rt = entry.get("refresh_token")
        if not rt:
            logger.warning("No refresh_token for '%s' — cannot refresh.", username)
            continue

        logger.info("Refreshing tokens for '%s' (expires in %.0fs)...", username, exp - now)
        new_tokens = refresh_tokens(session, rt)

        if new_tokens:
            _save_cached_tokens(username, new_tokens)
            new_exp = _decode_jwt_exp(new_tokens.jwt)
            logger.info(
                "Tokens refreshed for '%s'. New expiry: %s",
                username,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(new_exp)) if new_exp else "unknown",
            )
        else:
            logger.warning(
                "Refresh failed for '%s'. Token expires at %s — manual re-login may be required.",
                username,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)),
            )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    logger.info("Token refresh daemon started  (PID %d)", os.getpid())
    logger.info("Watching: %s", _CACHE_FILE)
    logger.info("Will refresh tokens %d minutes before expiry.", REFRESH_BEFORE_EXPIRY // 60)

    session = build_session()

    while True:
        cache = _load_raw_cache()

        if not cache:
            logger.info("Cache is empty — rechecking in 60s.")
            time.sleep(60)
            continue

        username, soonest_exp = _soonest_expiry(cache)
        if soonest_exp is None:
            logger.warning("No decodable JWT found in cache — rechecking in 60s.")
            time.sleep(60)
            continue

        now        = time.time()
        refresh_at = soonest_exp - REFRESH_BEFORE_EXPIRY
        sleep_secs = max(refresh_at - now, POLL_INTERVAL)

        logger.info(
            "Next refresh for '%s' at %s (in %.0fm %.0fs)",
            username,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(refresh_at)),
            sleep_secs // 60,
            sleep_secs % 60,
        )

        time.sleep(sleep_secs)

        # Re-read cache after sleeping — tokens may have been updated externally
        cache = _load_raw_cache()
        _refresh_all_due(session, cache)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logger.info("Token refresh daemon stopped.")
        sys.exit(0)
