#!/usr/bin/env python3
"""
ardhisasa_auth.py
=================
Shared authentication module for Ardhisasa API scripts — entries codebase.

Credentials are loaded from environment variables (.env) rather than being
hardcoded. Call load_credentials(profile) to get a credential dict at runtime.

Usage:
    from ardhisasa_auth import AUTH_BASE_URL, AuthTokens, build_session, load_credentials, auth_headers
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# ---------------------------------------------------------------------------
# Credential profiles — populated from environment variables
# ---------------------------------------------------------------------------

# Human-readable labels shown in Telegram menus.
# Keys must match the profile names accepted by load_credentials().
CRED_LABELS = {
    "publicuser":   "👤 Public User",
    "staff":        "🏢 ICT",
    "staff2":       "🏢 Support Reg",
    "staff_valuer": "🏢 Staff Valuer",
}


def load_credentials(profile: str) -> dict:
    """
    Build a credential dict for the given profile from environment variables.

    Profiles and their required env vars:
        publicuser   → PUBLIC_USERNAME, PUBLIC_PASSWORD
        staff        → STAFF_ICT_USERNAME, STAFF_ICT_PASSWORD
        staff2       → STAFF_SUPPORT_USERNAME, STAFF_SUPPORT_PASSWORD
        staff_valuer → STAFF_VALUER_USERNAME, STAFF_VALUER_PASSWORD

    Raises:
        ValueError: If the profile is unknown or required env vars are missing.
    """
    _profiles = {
        "publicuser": {
            "username": os.getenv("PUBLIC_USERNAME"),
            "password": os.getenv("PUBLIC_PASSWORD"),
            "usertype": "publicuser",
        },
        "staff": {
            "username": os.getenv("STAFF_ICT_USERNAME"),
            "password": os.getenv("STAFF_ICT_PASSWORD"),
            "usertype": "staff",
        },
        "staff2": {
            "username": os.getenv("STAFF_SUPPORT_USERNAME"),
            "password": os.getenv("STAFF_SUPPORT_PASSWORD"),
            "usertype": "staff",
        },
        "staff_valuer": {
            "username": os.getenv("STAFF_VALUER_USERNAME"),
            "password": os.getenv("STAFF_VALUER_PASSWORD"),
            "usertype": "staff",
        },
    }

    if profile not in _profiles:
        raise ValueError(
            f"Unknown credential profile '{profile}'. "
            f"Valid profiles: {list(_profiles)}"
        )

    creds   = _profiles[profile]
    missing = [k for k in ("username", "password") if not creds[k]]
    if missing:
        raise ValueError(
            f"Missing env vars for profile '{profile}': {missing}. "
            "Check your .env file."
        )

    return creds


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH_BASE_URL   = "https://ardhisasa-api.lands.go.ke/acl/api/v1/auth"
REQUEST_TIMEOUT = 30  # seconds

logger = logging.getLogger("ardhisasa.auth")

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
