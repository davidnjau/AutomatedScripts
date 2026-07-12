#!/usr/bin/env python3
"""
token_rotator.py
================
Shared multi-credential rotation for background bulk-fetch loops (Bulk
Export, Job Distribution) that page through hundreds of records under a
fixed set of already-authenticated credentials and need to fail over to
the next one on a 403 rather than aborting the whole run.
"""

import threading
from typing import List, Optional

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
