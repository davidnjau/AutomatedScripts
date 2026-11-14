#!/usr/bin/env python3
"""
minio_store.py
==============
Thin MinIO (S3-compatible) object-store wrapper, used by ocr_audit.py to keep
the actual OCR screenshots out of the JSON metadata store and off the bot's
disk. One bucket, one content type (JPEG); callers pass an object key and raw
bytes.

**Graceful degradation is the whole point:** if the MinIO env vars are unset
(or MinIO is unreachable), every operation is a safe no-op that returns
False/None instead of raising, so the bot keeps working — ocr_audit still
records the metadata (who/what/when), just without a stored image. This means
the feature can ship and run before the MinIO container is even deployed, and
starts storing images the moment the env is configured.

Config (all from the environment, read once at import):
  MINIO_ENDPOINT    host:port of the S3 API (e.g. "minio:9000"); empty = disabled
  MINIO_ACCESS_KEY  / MINIO_SECRET_KEY  credentials
  MINIO_BUCKET      bucket name (default "ocr-screenshots")
  MINIO_SECURE      "true" to use TLS (default false for the internal network)

The `minio` SDK is imported lazily inside _get_client so this module imports
fine (and tests run) even where the package isn't installed.
"""

import io
import os
from typing import Optional

from common import logger

MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET     = os.getenv("MINIO_BUCKET", "ocr-screenshots")
MINIO_SECURE     = os.getenv("MINIO_SECURE", "false").lower() == "true"

_client = None            # cached Minio client (lazy)
_bucket_ready = False     # True once the bucket has been confirmed/created


def is_enabled() -> bool:
    """True if MinIO is configured (endpoint + credentials present)."""
    return bool(MINIO_ENDPOINT and MINIO_ACCESS_KEY and MINIO_SECRET_KEY)


def _get_client():
    """Lazily build and cache the Minio client (importing the SDK only here,
    so an unconfigured/un-installed environment never fails at import)."""
    global _client
    if _client is None:
        from minio import Minio
        _client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
        )
    return _client


def ensure_bucket() -> bool:
    """Make sure the target bucket exists (creating it once). Returns True if
    MinIO is usable, False if disabled or unreachable — never raises."""
    global _bucket_ready
    if not is_enabled():
        return False
    if _bucket_ready:
        return True
    try:
        client = _get_client()
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)
        _bucket_ready = True
        return True
    except Exception as e:
        logger.warning("MinIO ensure_bucket failed: %s", e)
        return False


def put_screenshot(key: str, data: bytes) -> bool:
    """Store `data` (JPEG bytes) under `key`. Returns True on success, False if
    MinIO is disabled/unreachable — never raises."""
    if not ensure_bucket():
        return False
    try:
        _get_client().put_object(
            MINIO_BUCKET, key, io.BytesIO(data), length=len(data),
            content_type="image/jpeg",
        )
        return True
    except Exception as e:
        logger.warning("MinIO put failed for %s: %s", key, e)
        return False


def get_screenshot(key: str) -> Optional[bytes]:
    """Fetch the bytes stored under `key`, or None if missing/disabled —
    never raises."""
    if not is_enabled():
        return None
    try:
        resp = _get_client().get_object(MINIO_BUCKET, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()
    except Exception as e:
        logger.warning("MinIO get failed for %s: %s", key, e)
        return None


def delete_screenshot(key: str) -> bool:
    """Remove the object stored under `key`. Returns True on success, False if
    disabled/unreachable — never raises."""
    if not is_enabled():
        return False
    try:
        _get_client().remove_object(MINIO_BUCKET, key)
        return True
    except Exception as e:
        logger.warning("MinIO delete failed for %s: %s", key, e)
        return False
