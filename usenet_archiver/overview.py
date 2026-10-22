#!/usr/bin/env python3
"""Overview-based date range discovery for NNTP groups.

Scans the whole group with OVER / XOVER (XHDR DATE when the server has
neither) in chunks and keeps the rows whose Date falls in the window. There
is no bisection on Date: dates are not in article-number order, and gaps in
the numbers made the bisection skip whole ranges. An overview scan costs
little next to fetching the articles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, List, Optional, Sequence

from .mboxout import msgid_key

logger = logging.getLogger(__name__)


@dataclass
class OverviewRow:
    article_id: int
    subject: str = ""
    from_: str = ""
    date: Optional[datetime] = None
    message_id: str = ""
    references: str = ""
    bytes_: int = 0
    lines: int = 0
    xref: str = ""


def parse_overview_line(raw: bytes) -> Optional[OverviewRow]:
    """Parse one OVER/XOVER tab-separated line into an OverviewRow."""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        try:
            text = raw.decode("latin-1", errors="replace")
        except Exception:
            return None
    # Fields: artnum\tSubject\tFrom\tDate\tMessage-ID\tReferences\tbytes\tlines[\tXref]
    parts = text.split("\t")
    raw_parts = raw.split(b"\t")
    if not parts or not parts[0].strip().isdigit():
        return None
    article_id = int(parts[0].strip())

    def field(i: int) -> str:
        return parts[i].strip() if i < len(parts) else ""

    date = None
    date_str = field(3)
    if date_str and date_str != "none":
        try:
            date = parsedate_to_datetime(date_str)
            if date is not None and date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            date = None

    bytes_ = 0
    lines = 0
    try:
        if field(6):
            bytes_ = int(field(6))
    except ValueError:
        pass
    try:
        if field(7):
            lines = int(field(7))
    except ValueError:
        pass

    return OverviewRow(
        article_id=article_id,
        subject=field(1),
        from_=field(2),
        date=date,
        # from the raw bytes, as the same key the mbox index uses
        message_id=msgid_key(raw_parts[4]) if len(raw_parts) > 4 else "",
        references=field(5),
        bytes_=bytes_,
        lines=lines,
        xref=field(8),
    )


def parse_xhdr_date_pair(article_id: int, date_str: str) -> OverviewRow:
    date = None
    if date_str and date_str.lower() != "none":
        try:
            date = parsedate_to_datetime(date_str)
            if date is not None and date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            date = None
    return OverviewRow(article_id=article_id, date=date)


def _date_only(dt: Optional[datetime]):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date()


FetchOverview = Callable[[int, int], List[OverviewRow]]

# Tries per overview chunk; the connection is replaced after each failure.
OVERVIEW_TRIES = 3


def fetch_with_reconnect(client, start_id: int, end_id: int, tries: int = OVERVIEW_TRIES) -> List[OverviewRow]:
    """fetch_overview_chunk, retried on a new connection after any failure.

    After a timeout or a cut the old connection is out of step, so it is never
    read again: client.reconnect() opens a new one and selects the group. The
    last failure is raised.
    """
    from .nntp import NNTPError, NNTPTimeout

    for attempt in range(1, tries + 1):
        try:
            return fetch_overview_chunk(client, start_id, end_id)
        except (NNTPTimeout, NNTPError, OSError) as e:
            if attempt == tries:
                raise
            logger.warning(
                "Overview %s-%s failed (%s); reconnecting, try %s of %s",
                start_id, end_id, e, attempt + 1, tries,
            )
            client.reconnect()
    raise AssertionError("unreachable")


def fetch_overview_chunk(client, start_id: int, end_id: int) -> List[OverviewRow]:
    """Fetch overview rows for [start_id, end_id] via OVER/XOVER or XHDR DATE.

    XHDR is used only when the server knows neither OVER nor XOVER. A failure
    raises: an empty result always means the range holds no articles.
    """
    lines, cmd, resp = client.over(start_id, end_id)
    if cmd:
        rows = []
        for raw in lines:
            row = parse_overview_line(raw)
            if row is not None:
                rows.append(row)
        return rows

    # Fallback: XHDR DATE
    pairs = client.xhdr_date(start_id, end_id)
    return [parse_xhdr_date_pair(aid, dstr) for aid, dstr in pairs]


def find_articles_in_date_range(
    client,
    first: int,
    last: int,
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    chunk_size: int = 10000,
) -> List[OverviewRow]:
    """Return overview rows for first..last whose Date falls in
    [start_date, end_date] (whole UTC days, inclusive), sorted by number.

    Rows without a usable Date are kept. With no dates, every row is
    returned. The whole range is scanned, so neither out-of-order dates nor
    gaps in the article numbers can hide an article in the window.
    """
    if first > last:
        return []

    def fetch(a: int, b: int) -> List[OverviewRow]:
        return fetch_with_reconnect(client, a, b)

    start_d = start_date.date() if start_date else None
    end_d = end_date.date() if end_date else None
    logger.info("Overview scan: %s-%s (chunk=%s)", first, last, chunk_size)
    return list(_scan_chunks(fetch, first, last, chunk_size, start_d, end_d))


def _scan_chunks(
    fetch: FetchOverview,
    start_id: int,
    end_id: int,
    chunk_size: int,
    start_d,
    end_d,
) -> List[OverviewRow]:
    matched: List[OverviewRow] = []
    cursor = start_id
    while cursor <= end_id:
        chunk_end = min(end_id, cursor + chunk_size - 1)
        # A failed chunk raises: skipping it would lose its articles silently.
        rows = fetch(cursor, chunk_end)
        for row in rows:
            if start_d is None and end_d is None:
                matched.append(row)
                continue
            if row.date is None:
                # Include undated articles when filtering? Plan says filter on overview Date.
                # Keep them so rare missing Date headers are not silently dropped from archive.
                matched.append(row)
                continue
            d = _date_only(row.date)
            if d is None:
                matched.append(row)
                continue
            if start_d is not None and d < start_d:
                continue
            if end_d is not None and d > end_d:
                continue
            matched.append(row)
        cursor = chunk_end + 1
    matched.sort(key=lambda r: r.article_id)
    return matched


def filter_rows_by_dedup(rows: Sequence[OverviewRow], seen_ids: set) -> List[OverviewRow]:
    """Drop rows whose Message-ID is already in seen_ids."""
    out = []
    for row in rows:
        mid = (row.message_id or "").strip()
        if mid and mid in seen_ids:
            continue
        out.append(row)
    return out
