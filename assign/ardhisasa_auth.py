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

import logging
from dataclasses import dataclass
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

STAFF_CREDENTIALS_SUPPORT = {
    "username": "SE0E20RF0F",
    "password": "Ardh1s@s@",
    "usertype": "staff",
}

STAFF_CREDENTIALS_ICT = {
    "username": "20210439855",
    "password": "ItaSabaQefin10222/()/",
    "usertype": "staff",
}

STAFF_CREDENTIALS_VALUER = {
    "username": "2015001311",
    "password": "Marcel(2025)",
    "usertype": "staff",
}

PUBLIC_CREDENTIALS = {
    "username": "33745057",
    "password": "Sc281-6736/2014",
    "usertype": "publicuser",
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH_BASE_URL = "https://ardhisasa-api.lands.go.ke/acl/api/v1/auth"
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
    url = f"{AUTH_BASE_URL}/login"
    payload = {
        "username": credentials["username"],
        "password": credentials["password"],
        "usertype": credentials["usertype"],
        "otpcode":  "",
    }

    logger.info("Initiating login for user: %s (usertype=%s)",
                credentials["username"], credentials["usertype"])

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
    url = f"{AUTH_BASE_URL}/otpverify"
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