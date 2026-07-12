#!/usr/bin/env python3
"""
telegram_report.py
====================
Shared "paginate a report and send it to Telegram" helper. Several
features (DLV Tasks, Fetch Tasks, and future ones) format a list of rows
into text lines and need to split them across multiple messages to stay
under Telegram's ~4096-char limit — this used to be duplicated,
line-by-line accumulation logic in each feature module.

`_chunk_lines` is the pure part (no I/O, trivially testable): given a list
of text lines, group them into blocks under a character threshold.

`_send_chunked_report` drives the actual sending: it chunks the lines and
calls a caller-supplied `send_fn(text, reply_markup)` for each chunk,
attaching `reply_markup` (and appending `footer`) only to the final chunk.
Feature-specific concerns — parse_mode, retry-on-failure, the exact
"no rows" message — stay in the caller's `send_fn`/pre-check, not here.
"""

from typing import Awaitable, Callable, List, Optional


def _chunk_lines(lines: List[str], join: str = "\n\n", threshold: int = 4000) -> List[str]:
    """
    Group `lines` into blocks of at most `threshold` characters each,
    joined by `join`. Mirrors the exact chunking behavior previously
    duplicated across DLV Tasks' and Fetch Tasks' report senders.
    """
    chunks: List[str] = []
    current = ""
    for line in lines:
        candidate = current + join + line if current else line
        if len(candidate) > threshold:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _send_chunked_report(
    send_fn: Callable[[str, Optional[object]], Awaitable[None]],
    lines: List[str],
    *,
    join: str = "\n\n",
    threshold: int = 4000,
    footer: str = "",
    reply_markup: Optional[object] = None,
) -> None:
    """
    Chunk `lines` and send each chunk via `send_fn(text, reply_markup)`.
    `footer` is appended, and `reply_markup` attached, to the last chunk
    only — matching how every existing report sender behaves (e.g. a
    "Total: N task(s)" footer or a return-to-main-menu keyboard).
    """
    chunks = _chunk_lines(lines, join=join, threshold=threshold)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        text = chunk + footer if is_last else chunk
        await send_fn(text, reply_markup if is_last else None)
