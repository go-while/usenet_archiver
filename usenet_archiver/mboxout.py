#!/usr/bin/env python3
"""mboxrd writer: envelope lines, mboxrd quoting, Message-ID dedup index."""

from __future__ import annotations

import email
import re
import threading
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import BinaryIO, List, Optional, Set, Tuple


# English weekday / month abbreviations (locale-independent).
_WDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_MESSAGE_ID_RE = re.compile(rb"(?im)^Message-ID:\s*(.+?)\s*$")
_FROM_RE = re.compile(rb"(?im)^From:\s*(.+?)\s*$")
_DATE_RE = re.compile(rb"(?im)^Date:\s*(.+?)\s*$")


# The last header line of every article this writer stores. mbox2nntp
# (github.com/go-while/mbox2nntp) takes it as "this mbox is mboxrd": it takes
# one ">" off quoted body lines (see needs_quote) and drops the field before
# sending. Without it mbox2nntp sends body lines as stored.
FORMAT_FIELD = b"X-Mbox2nntp-Format: mboxrd"

# Envelope date for an article whose Date is missing, does not parse or is
# out of range: fixed, so that the same article always gets the same line.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def format_envelope_utc(dt: Optional[datetime], from_addr: str = "-") -> bytes:
    """Build a locale-independent mbox From_ separator line in UTC.

    Format: ``From addr Day Mon DD HH:MM:SS YYYY\\n`` (RFC 4155, asctime).
    A missing or out-of-range date gives the epoch.
    """
    if dt is None:
        dt = _EPOCH
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        try:
            dt = dt.astimezone(timezone.utc)
        except (OverflowError, ValueError):  # e.g. 9999-12-31 23:00 -0200
            dt = _EPOCH

    wday = _WDAYS[dt.weekday()]
    month = _MONTHS[dt.month - 1]
    timestr = f"{wday} {month} {dt.day:2d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} {dt.year:04d}"
    addr = from_addr.strip() if from_addr and from_addr.strip() else "-"
    # Avoid spaces / control chars in the envelope address token
    if " " in addr or "\t" in addr or "\n" in addr:
        addr = "-"
    return f"From {addr} {timestr}\n".encode("ascii", errors="replace")


def needs_quote(line: bytes) -> bool:
    """True for a line mboxrd quotes with one more ">": "From " or "#! rnews "
    after any number of ">".

    mbox2nntp's parser takes "#! rnews N" for the start of a message (rnews
    batches) as well as "From ", so either would split an article there.
    Same rule as NeedsQuote in mbox2nntp's internal/mboxout.
    """
    t = line.lstrip(b">")
    return t.startswith(b"From ") or t.startswith(b"#! rnews ")


def unquote(line: bytes) -> bytes:
    """Undo the quote format_mbox_message adds to a line."""
    if line.startswith(b">") and needs_quote(line[1:]):
        return line[1:]
    return line


def split_article(article: bytes) -> Tuple[List[bytes], List[bytes]]:
    """Split an article into header lines and body lines, without line ends.

    The header ends at the first empty line (LF or CRLF). An article with no
    empty line is all header. Bytes are not changed: a CR stays where it is.
    """
    lines = article.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # the final line end, not an empty line
    for i, line in enumerate(lines):
        if line in (b"", b"\r"):
            return lines[:i], lines[i + 1 :]
    return lines, []


def format_mbox_message(article: bytes, dt: Optional[datetime] = None, from_addr: str = "-") -> bytes:
    """Return one article as an mboxrd message, ready to append.

    The envelope line, the header lines, FORMAT_FIELD as the last header
    line, an empty line, the body lines with needs_quote lines quoted, and
    one empty line before the next envelope. Header lines are quoted the same
    way: a real header line never starts with "From " or "#! rnews ", and an
    unquoted one would split the article. Nothing else is changed: no line
    end conversion, so a lone CR stays in its line.
    """
    head, body = split_article(article)
    out = [format_envelope_utc(dt, from_addr)]
    for line in head:
        out.append(b">" + line + b"\n" if needs_quote(line) else line + b"\n")
    out.append(FORMAT_FIELD + b"\n\n")
    for line in body:
        out.append(b">" + line + b"\n" if needs_quote(line) else line + b"\n")
    out.append(b"\n")
    return b"".join(out)


def _header_block(article: bytes) -> bytes:
    """Return bytes up to the first blank line (header section)."""
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = article.find(sep)
        if idx >= 0:
            return article[:idx]
    return article[:4096]


def sender_from_article(article: bytes) -> str:
    """Extract a From_ envelope address from article bytes (header scan)."""
    m = _FROM_RE.search(_header_block(article))
    if not m:
        return "-"
    try:
        raw = m.group(1).decode("utf-8", errors="replace")
    except Exception:
        return "-"
    _, addr = parseaddr(raw)
    if addr and "@" in addr:
        return addr
    return "-"


def date_from_article(article: bytes) -> Optional[datetime]:
    m = _DATE_RE.search(_header_block(article))
    if not m:
        return None
    try:
        date_hdr = m.group(1).decode("ascii", errors="replace")
        return parsedate_to_datetime(date_hdr)
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return None


def message_id_from_article(article: bytes) -> Optional[str]:
    m = _MESSAGE_ID_RE.search(_header_block(article))
    if not m:
        # Fallback to full parse for folded headers
        msg = email.message_from_bytes(article)
        mid = msg.get("Message-ID") or msg.get("Message-Id")
        return mid.strip() if mid else None
    try:
        return m.group(1).decode("ascii", errors="replace").strip()
    except Exception:
        return None


def write_mbox_message(handle: BinaryIO, article: bytes, dt: Optional[datetime] = None) -> None:
    """Write one article to an mboxrd file (binary handle), in one write."""
    if dt is None:
        dt = date_from_article(article)
    handle.write(format_mbox_message(article, dt, sender_from_article(article)))


def index_message_ids(mbox_path: str) -> Set[str]:
    """Scan an existing mbox for Message-ID headers (byte scan, no full parse)."""
    ids: Set[str] = set()
    try:
        with open(mbox_path, "rb") as fh:
            for line in fh:
                m = _MESSAGE_ID_RE.match(line.rstrip(b"\r\n"))
                if m:
                    try:
                        ids.add(m.group(1).decode("ascii", errors="replace").strip())
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return ids


class MboxWriter:
    """Append-mode mbox writer with optional Message-ID deduplication.

    Thread-safe. Flushes every ``flush_every`` writes (and on close) instead of
    after every article.
    """

    def __init__(self, path: str, dedup: bool = True, flush_every: int = 100):
        self.path = path
        self.dedup = dedup
        self.flush_every = max(1, flush_every)
        self._seen: Set[str] = index_message_ids(path) if dedup else set()
        self._fh: Optional[BinaryIO] = None
        self._lock = threading.Lock()
        self._since_flush = 0
        self.written = 0
        self.skipped_dup = 0
        self.bytes_written = 0

    def open(self) -> None:
        with self._lock:
            if self._fh is None:
                self._fh = open(self.path, "ab")

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                finally:
                    self._fh.close()
                    self._fh = None
                    self._since_flush = 0

    def __enter__(self) -> "MboxWriter":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def has_message_id(self, message_id: Optional[str]) -> bool:
        if not self.dedup or not message_id:
            return False
        with self._lock:
            return message_id.strip() in self._seen

    def write(self, article: bytes, dt: Optional[datetime] = None, message_id: Optional[str] = None) -> bool:
        """Write article unless it is a duplicate. Returns True if written."""
        mid = message_id or message_id_from_article(article)
        with self._lock:
            if mid and self.dedup and mid.strip() in self._seen:
                self.skipped_dup += 1
                return False
            if self._fh is None:
                self._fh = open(self.path, "ab")
            before = self._fh.tell()
            write_mbox_message(self._fh, article, dt=dt)
            after = self._fh.tell()
            self.bytes_written += max(0, after - before)
            self._since_flush += 1
            if self._since_flush >= self.flush_every:
                self._fh.flush()
                self._since_flush = 0
            if mid:
                self._seen.add(mid.strip())
            self.written += 1
            return True
