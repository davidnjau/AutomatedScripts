#!/usr/bin/env python3
"""
ardhisasa_auth.py
=================
Shared authentication module for Ardhisasa API scripts.

Provides login + OTP verification for both credential profiles:
  - STAFF      (registration/enumeration/conversion)
  - PUBLIC USER (valuation/stamp-duty)

Usage:
    from ardhisasa_auth import authenticate, STAFF_CREDENTIALS_ICT, PUBLIC_CREDENTIALS
"""

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from endpoints import AUTH_LOGIN_URL, AUTH_OTP_VERIFY_URL, AUTH_SYSTEM_LOGIN_URL

load_dotenv()

# ---------------------------------------------------------------------------
# Credentials  (values come from .env — never hardcode secrets in source)
# ---------------------------------------------------------------------------

STAFF_CREDENTIALS_SUPPORT = {
    "username": os.environ["STAFF_SUPPORT_USER"],
    "password": os.environ["STAFF_SUPPORT_PASS"],
    "usertype": "staff",
}

STAFF_CREDENTIALS_ICT = {
    "username": os.environ["STAFF_ICT_USER"],
    "password": os.environ["STAFF_ICT_PASS"],
    "usertype": "staff",
}

STAFF_CREDENTIALS_VALUER = {
    "username": os.environ["STAFF_VALUER_USER"],
    "password": os.environ["STAFF_VALUER_PASS"],
    "usertype": "staff",
}

PUBLIC_CREDENTIALS = {
    "username": os.environ["PUBLIC_USER"],
    "password": os.environ["PUBLIC_PASS"],
    "usertype": "publicuser",
}

# Credentials whose tokens are renewed via the OTP-free system-login (both the
# daemon's proactive renewal and common.reauth's live-403 recovery). Scoped to
# the two credentials the bot actually depends on — 🏢 Staff Valuer (DLV/
# valuation work) and 🏢 Support Reg (staff2, the county/assessor stage).
# publicuser and staff (ICT) stay on the legacy refresh-token path (they're
# rarely used and weren't affected by the rotation issue). Imported by both
# common.py and the standalone token_refresh_daemon.py.
SYSTEM_LOGIN_CREDS = {"staff_valuer", "staff2"}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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
    """
    Create a requests.Session with retry logic for transient HTTP errors.

    Args:
        max_retries:    Maximum retry attempts.
        backoff_factor: Exponential backoff multiplier between retries.

    Returns:
        Configured requests.Session instance.
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=max_retries,
        connect=max_retries,
        read=False,           # never retry reads at urllib3 level — callers handle this
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Set common browser-like headers
    session.headers.update({
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  "en-GB,en-US;q=0.9,en;q=0.8",
        "Connection":       "keep-alive",
        "Content-Type":     "application/json",
        "Origin":           "https://ardhisasa.lands.go.ke",
        "Referer":          "https://ardhisasa.lands.go.ke/",
        "Sec-Fetch-Dest":   "empty",
        "Sec-Fetch-Mode":   "cors",
        "Sec-Fetch-Site":   "same-site",
        "User-Agent":       (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua":         '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile":  "?0",
        "sec-ch-ua-platform": '"macOS"',
    })

    return session


# ---------------------------------------------------------------------------
# Auth Steps
# ---------------------------------------------------------------------------

def login(session: requests.Session, credentials: dict) -> None:
    """
    Perform initial login to trigger OTP generation.

    Args:
        session:     Active requests session.
        credentials: Dict with keys: username, password, usertype.

    Raises:
        RuntimeError: If the server returns a login error.
    """
    url = AUTH_LOGIN_URL
    payload = {
        "username": credentials["username"],
        "password": credentials["password"],
        "usertype": credentials["usertype"],
        "otpcode":  "",
    }

    logger.info("Initiating login (usertype=%s)", credentials["usertype"])

    response = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    if not data.get("success", True) and "error" in data:
        raise RuntimeError(f"Login failed: {data.get('error') or data.get('message')}")

    logger.info("Login successful — OTP dispatched to registered device.")


def verify_otp(session: requests.Session, credentials: dict, otp_code: str) -> AuthTokens:
    """
    Verify OTP and retrieve authentication tokens.

    Args:
        session:     Active requests session.
        credentials: Dict with keys: username, password.
        otp_code:    OTP entered by the user.

    Returns:
        AuthTokens instance containing access_token and jwt.

    Raises:
        RuntimeError: If OTP verification fails or tokens are absent.
    """
    url = AUTH_OTP_VERIFY_URL
    payload = {
        "username": credentials["username"],
        "password": credentials["password"],
        "otpcode":  otp_code.strip(),
    }

    logger.info("Verifying OTP...")
    response = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    access_token = data.get("details", {}).get("access_token")
    jwt          = data.get("details", {}).get("jwt")

    if not access_token or not jwt:
        logger.error("OTP verify response keys: %s", list(data.keys()))
        raise RuntimeError(
            "OTP verification succeeded but tokens were not found in response. "
            f"Available keys: {list(data.keys())}"
        )

    logger.info("OTP verified successfully. Tokens acquired.")
    return AuthTokens(access_token=access_token, jwt=jwt)


def system_login(session: requests.Session, credentials: dict) -> Tuple[AuthTokens, str]:
    """
    OTP-free machine login via /acl/api/v1/auth/system-login (OAuth2 password
    grant). Unlike login()+verify_otp(), this authenticates with just
    username/password/usertype and returns tokens directly — no OTP dispatch,
    no human step — so it can be called unattended whenever a credential's
    tokens expire or are retired (403). Because it's a fresh username/password
    login (not a refresh_token exchange), it is immune to the refresh-token
    rotation that can revoke a cached token when the account is used elsewhere.

    Args:
        session:     Active requests session.
        credentials: Dict with keys: username, password, usertype.

    Returns:
        (AuthTokens, refresh_token). refresh_token may be "" if absent.

    Raises:
        requests.HTTPError: on non-2xx (e.g. 403 if the endpoint is gated, 400
            on bad credentials/usertype).
        RuntimeError: if the response is 2xx but tokens are missing.
    """
    payload = {
        "username": credentials["username"],
        "password": credentials["password"],
        "usertype": credentials["usertype"],
    }
    logger.info("System login (usertype=%s) — OTP-free.", credentials["usertype"])
    response = session.post(AUTH_SYSTEM_LOGIN_URL, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    details      = data.get("details", {}) if isinstance(data.get("details"), dict) else {}
    access_token = details.get("access_token")
    jwt          = details.get("jwt")
    refresh      = details.get("refresh_token", "") or ""

    if not access_token or not jwt:
        logger.error("System-login response keys: %s", list(data.keys()))
        raise RuntimeError(
            "System login succeeded (2xx) but tokens were not found in the "
            f"response. Available keys: {list(data.keys())}"
        )

    logger.info("System login successful — tokens acquired (no OTP).")
    return AuthTokens(access_token=access_token, jwt=jwt), refresh


def authenticate(
    session: requests.Session,
    credentials: Optional[dict] = None,
    otp_prompt: str = "\n>>> Enter the OTP code received on your registered device: ",
) -> AuthTokens:
    """
    Full authentication flow: login → prompt OTP → verify → return tokens.

    Args:
        session:     Active requests session.
        credentials: Credential dict to use. Defaults to STAFF_CREDENTIALS_ICT.
        otp_prompt:  Custom prompt string shown to the user.

    Returns:
        AuthTokens with access_token and jwt.

    Raises:
        ValueError:   If OTP input is empty.
        RuntimeError: If any auth step fails.
    """
    if credentials is None:
        credentials = STAFF_CREDENTIALS_ICT

    login(session, credentials)

    otp_code = input(otp_prompt).strip()
    if not otp_code:
        raise ValueError("OTP code cannot be empty.")

    return verify_otp(session, credentials, otp_code)


def auth_headers(tokens: AuthTokens) -> dict:
    """
    Build the Authorization / JWTAUTH header dict required for protected endpoints.

    Args:
        tokens: AuthTokens returned by authenticate().

    Returns:
        Dict with Authorization and JWTAUTH header entries.
    """
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "JWTAUTH":       f"Bearer {tokens.jwt}",
    }


# ---------------------------------------------------------------------------
# JWT utilities
# ---------------------------------------------------------------------------

def decode_jwt_exp(token: str) -> Optional[float]:
    """
    Decode the JWT payload (without verification) and return the `exp` claim.

    Returns the expiry as a UNIX timestamp float, or None if the token is
    malformed or the claim is missing.
    """
    try:
        payload = token.split(".")[1]
        # Re-pad to a multiple of 4 before decoding
        payload += "=" * (4 - len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None