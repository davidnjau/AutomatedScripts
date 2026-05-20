#!/usr/bin/env python3
"""
ardhisasa_auth.py
=================
Authentication module for the entries codebase.

A single credential set is used, stored in .env as USER_LOGIN / USER_PASSWORD.
Call get_credentials() to read them and save_credentials() to update them.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH_BASE_URL   = "https://ardhisasa-api.lands.go.ke/acl/api/v1/auth"
REQUEST_TIMEOUT = 30  # seconds

_ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

logger = logging.getLogger("ardhisasa.auth")

# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def get_credentials() -> dict:
    """
    Read USER_LOGIN, USER_PASSWORD, USER_TYPE from the environment.

    Returns a dict with keys: username, password, usertype.
    Values are empty strings when the env vars are not set.
    """
    return {
        "username": os.getenv("USER_LOGIN", ""),
        "password": os.getenv("USER_PASSWORD", ""),
        "usertype": os.getenv("USER_TYPE", "staff"),
    }


def save_credentials(username: str, password: str) -> None:
    """
    Persist USER_LOGIN and USER_PASSWORD back to the .env file and reload
    the environment so the running process picks them up immediately.

    Existing lines are updated in-place; missing vars are appended.
    """
    lines: list[str] = []
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE) as f:
            lines = f.readlines()

    def _set(key: str, value: str, src: list[str]) -> list[str]:
        pattern = re.compile(rf"^{re.escape(key)}\s*=.*", re.MULTILINE)
        replaced = False
        result = []
        for line in src:
            if pattern.match(line):
                result.append(f"{key}={value}\n")
                replaced = True
            else:
                result.append(line)
        if not replaced:
            result.append(f"{key}={value}\n")
        return result

    lines = _set("USER_LOGIN",    username, lines)
    lines = _set("USER_PASSWORD", password, lines)

    with open(_ENV_FILE, "w") as f:
        f.writelines(lines)

    # Reload so os.getenv() returns the new values in the same process
    load_dotenv(_ENV_FILE, override=True)
    logger.info("Credentials saved to .env (username=%s)", username)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class AuthTokens:
    """Holds authentication tokens retrieved after OTP verification."""
    access_token: str
    jwt: str


# ---------------------------------------------------------------------------
# Session Factory
# ---------------------------------------------------------------------------

def build_session(max_retries: int = 3, backoff_factor: int = 2) -> requests.Session:
    """Return a requests.Session with retry logic and browser-like headers."""
    session = requests.Session()
    retry_strategy = Retry(
        total=max_retries,
        connect=max_retries,
        read=False,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "Accept":            "application/json, text/plain, */*",
        "Accept-Language":   "en-GB,en-US;q=0.9,en;q=0.8",
        "Connection":        "keep-alive",
        "Content-Type":      "application/json",
        "Origin":            "https://ardhisasa.lands.go.ke",
        "Referer":           "https://ardhisasa.lands.go.ke/",
        "Sec-Fetch-Dest":    "empty",
        "Sec-Fetch-Mode":    "cors",
        "Sec-Fetch-Site":    "same-site",
        "User-Agent":        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua":          '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"macOS"',
    })

    return session


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def auth_headers(tokens: AuthTokens) -> dict:
    """Return the Authorization / JWTAUTH header dict for protected endpoints."""
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }
