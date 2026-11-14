#!/usr/bin/env python3
"""
blob_store.py
=============
Local-filesystem storage for OCR screenshots, used by ocr_audit.py to keep the
actual images out of the JSON metadata store. One flat directory tree under the
bot's data volume; callers pass an object key (e.g. "screenshots/<id>.jpg") and
raw bytes.

**Why local disk and not MinIO:** MinIO was the original plan, but the deploy
target (games_server) can't run it — MinIO's binary requires an x86-64-v2 CPU
(SSE4.2/POPCNT/…), the very baseline that box's virtualized CPU lacks (the same
reason requirements.txt pins numpy<2). MinIO also pulled its community images
from Docker Hub (Sept 2026) and gated quay.io behind auth. So screenshots live
on the existing bot_data volume instead — no extra container, no registry, and
they survive restarts just like every other file in ./data/.

Every operation is wrapped so a disk hiccup returns False/None instead of
raising — ocr_audit treats a failed store as "metadata only, no image" and
keeps working.
"""

import os
from typing import Optional

from common import DATA_DIR, logger

# Blobs live under DATA_DIR/<key> — e.g. DATA_DIR/screenshots/<id>.jpg. DATA_DIR
# is the mounted bot_data volume, so these persist across container restarts.
_ROOT = DATA_DIR


def _path_for(key: str) -> str:
    """Absolute on-disk path for an object key, confined to _ROOT (a key with
    .. components can't escape the blob root)."""
    full = os.path.normpath(os.path.join(_ROOT, key))
    if not full.startswith(os.path.normpath(_ROOT) + os.sep):
        raise ValueError(f"unsafe blob key: {key!r}")
    return full


def put_screenshot(key: str, data: bytes) -> bool:
    """Write `data` (JPEG bytes) to `key`, creating parent dirs. Returns True on
    success, False on any error — never raises."""
    try:
        path = _path_for(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        logger.warning("blob_store put failed for %s: %s", key, e)
        return False


def get_screenshot(key: str) -> Optional[bytes]:
    """Read the bytes stored under `key`, or None if missing/error —
    never raises."""
    try:
        with open(_path_for(key), "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("blob_store get failed for %s: %s", key, e)
        return None


def delete_screenshot(key: str) -> bool:
    """Delete the object stored under `key`. Returns True on success (and also
    if it's already gone), False on an unexpected error — never raises."""
    try:
        os.remove(_path_for(key))
        return True
    except FileNotFoundError:
        return True   # already absent — the desired end state
    except Exception as e:
        logger.warning("blob_store delete failed for %s: %s", key, e)
        return False
