#!/usr/bin/env python3
"""
token_rotator.py
================
Shared multi-credential rotation for background bulk-fetch loops (Bulk
Export, Job Distribution) that page through hundreds of records under a
fixed set of already-authenticated credentials and need to fail over to
the next one on a 403 rather than aborting the whole run.

fetch_with_rotation() is the shared "GET with token-rotation-on-403 and
retry-on-transient-5xx" loop — Valuer Tasks and Job Distribution each had
an identical copy of this (_vt_fetch_task_detail/_jd_fetch_task_detail)
before it was pulled out here; both now delegate to this one.
"""

import threading
import time
from typing import Callable, List, Optional

import requests

from ardhisasa_auth import AuthTokens
from common import CRED_LABELS


class _AllTokensExhausted(Exception):
    pass


class _TokenRotator:
    """Thread-safe token rotator — advances to the next valid credential on 403."""

    def __init__(self, token_pairs: List[tuple]):
        # token_pairs: [(cred_type, AuthTokens), ...]
        self._tokens = list(token_pairs)
        self._idx    = 0
        self._lock   = threading.Lock()

    def current(self) -> Optional[AuthTokens]:
        with self._lock:
            return self._tokens[self._idx][1] if self._idx < len(self._tokens) else None

    def current_label(self) -> str:
        with self._lock:
            if self._idx < len(self._tokens):
                return CRED_LABELS.get(self._tokens[self._idx][0], self._tokens[self._idx][0])
            return "none"

    def rotate(self, failed: AuthTokens) -> Optional[AuthTokens]:
        """Advance past `failed` if it is still the current token. Returns new token or None.

        The identity (`is`) check is intentional: if two threads both get 403 on the
        same token and both call rotate(), only the first one advances the index — the
        second sees the index already moved and simply returns the new current token,
        avoiding a double-skip.  This is correct thread-safe behaviour, not a bug.
        """
        with self._lock:
            if self._idx < len(self._tokens) and self._tokens[self._idx][1] is failed:
                self._idx += 1
            return self._tokens[self._idx][1] if self._idx < len(self._tokens) else None

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._idx >= len(self._tokens)


def fetch_with_rotation(
    sess: requests.Session,
    rotator: _TokenRotator,
    url: str,
    params: dict,
    headers_fn: Callable[[AuthTokens], dict],
    *,
    context: str = "",
    rotate_delay: int = 10,
    max_retries: int = 5,
) -> dict:
    """
    GET `url` with `params`, building headers via `headers_fn(tokens)`.

    On 403, rotates to the next valid credential (sleeping `rotate_delay`
    seconds first) and retries. On transient 429/5xx, retries up to
    `max_retries` times with a fixed 2-second pause. Raises
    _AllTokensExhausted once the rotator runs out of credentials.

    `context` is used only to make the raised error messages readable
    (e.g. "task abc123") — pass whatever identifies this fetch to the caller.
    """
    label = context or url
    retries = 0
    while True:
        tokens = rotator.current()
        if tokens is None:
            raise _AllTokensExhausted(f"All tokens exhausted fetching {label}")
        headers = headers_fn(tokens)
        resp = sess.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 403:
            new_tokens = rotator.rotate(tokens)
            if new_tokens is None:
                raise _AllTokensExhausted(f"All tokens exhausted (403) fetching {label}")
            time.sleep(rotate_delay)
            continue
        if resp.status_code in (429, 502, 503, 504):
            retries += 1
            if retries >= max_retries:
                raise RuntimeError(
                    f"fetch_with_rotation: too many transient errors (HTTP {resp.status_code}) "
                    f"for {label}"
                )
            time.sleep(2)
            continue
        resp.raise_for_status()
        return resp.json()
