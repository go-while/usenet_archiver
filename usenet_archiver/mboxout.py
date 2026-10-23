#!/usr/bin/env python3
"""mboxrd writer: envelope lines, mboxrd quoting, Message-ID dedup index."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import threading
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import BinaryIO, List, Optional, Set, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no locking
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


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



# The last header line of every article this writer stores. mbox2nntp
# (github.com/go-while/mbox2nntp) takes it as "this mbox is mboxrd": it takes
# one ">" off quoted lines, header and body (see needs_quote; mbox2nntp >=
# v0.3.3, older versions send a quoted header line with its ">"), and drops
# the field before sending. Without it mbox2nntp sends the lines as stored.
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


def header_value(head: List[bytes], name: bytes) -> Optional[bytes]:
    """The value of the first header field called name (any case), unfolded.

    Continuation lines are joined and every run of whitespace becomes one
    space. An empty value gives None: the next line is never taken for it.
    """
    want = name.lower()
    for i, line in enumerate(head):
        field, sep, value = line.partition(b":")
        if not sep or field.strip().lower() != want or line[:1] in (b" ", b"\t"):
            continue
        for cont in head[i + 1 :]:
            if cont[:1] not in (b" ", b"\t"):
                break
            value += b" " + cont
        value = b" ".join(value.split())
        return value or None
    return None


def msgid_key(raw) -> str:
    """The dedup key for a Message-ID (bytes from a header or OVER, or str).

    Whitespace is collapsed and the bytes are decoded losslessly
    (surrogateescape), so the same ID gives the same key from the header,
    from OVER and from the mbox, whatever its encoding.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="surrogateescape")
    return b" ".join(raw.split()).decode("utf-8", errors="surrogateescape")


def _nomid_key(head: List[bytes]) -> str:
    """Key for an article without a Message-ID: a hash of its header lines."""
    h = hashlib.sha256()
    for line in head:
        h.update(line.rstrip(b"\r") + b"\n")
    return "no-message-id:" + h.hexdigest()


def article_key(article: bytes) -> str:
    """Dedup key of an article: its Message-ID, else a hash of its header."""
    head, _ = split_article(article)
    mid = header_value(head, b"message-id")
    return msgid_key(mid) if mid else _nomid_key(head)


def sender_from_article(article: bytes) -> str:
    """Extract a From_ envelope address from the article's From: field."""
    value = header_value(split_article(article)[0], b"from")
    if not value:
        return "-"
    _, addr = parseaddr(value.decode("utf-8", errors="replace"))
    if addr and "@" in addr:
        return addr
    return "-"


def date_from_article(article: bytes) -> Optional[datetime]:
    value = header_value(split_article(article)[0], b"date")
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.decode("ascii", errors="replace"))
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return None


def message_id_from_article(article: bytes) -> Optional[str]:
    """The article's Message-ID as a dedup key (see msgid_key), or None."""
    mid = header_value(split_article(article)[0], b"message-id")
    return msgid_key(mid) if mid else None


def write_mbox_message(handle: BinaryIO, article: bytes, dt: Optional[datetime] = None) -> None:
    """Write one article to an mboxrd file (binary handle), in one write."""
    if dt is None:
        dt = date_from_article(article)
    handle.write(format_mbox_message(article, dt, sender_from_article(article)))


def index_message_ids(mbox_path: str, limit: Optional[int] = None) -> Set[str]:
    """Dedup keys (see article_key) of every message in an existing mbox.

    With limit, only the first limit bytes are read (the whole messages; see
    complete_size).

    Only header blocks are read: an envelope line ("From " at the start of
    the file or after an empty line) up to the next empty line. A body line
    such as a quoted "Message-ID: <x>" is never taken for a header. Header
    lines are unquoted and this writer's FORMAT_FIELD is left out, so the
    keys equal those of the articles as fetched. LF and CRLF files work.
    """
    keys: Set[str] = set()
    head: Optional[List[bytes]] = None  # header lines of the current message
    prev_empty = True
    try:
        with open(mbox_path, "rb") as fh:
            pos = 0
            for raw in fh:
                pos += len(raw)
                if limit is not None and pos > limit:
                    break
                line = raw.rstrip(b"\r\n")
                if head is not None:
                    if line == b"":
                        keys.add(_key_from_head(head))
                        head = None
                    else:
                        head.append(line)
                elif prev_empty and line.startswith(b"From "):
                    head = []
                prev_empty = line == b""
            if head:
                keys.add(_key_from_head(head))
    except FileNotFoundError:
        pass
    return keys


def _key_from_head(head: List[bytes]) -> str:
    head = [unquote(line) for line in head if line != FORMAT_FIELD]
    mid = header_value(head, b"message-id")
    return msgid_key(mid) if mid else _nomid_key(head)


JOURNAL_SUFFIX = ".inprogress"


class MboxLocked(OSError):
    """Another process is writing this mbox."""


# mbox2nntp's limit (maxArticleSize) on the body of an article as stored in
# the mbox: the body lines with their line ends and the mboxrd quotes, and
# the empty line after them. At this size or more mbox2nntp drops the
# article (counted 437), so it is not written.
MAX_STORED_BODY = 1 << 20


class ArticleTooBig(ValueError):
    """The article's body as stored would be too big for mbox2nntp to import."""


def stored_body_size(article: bytes) -> int:
    """The size mbox2nntp counts for the article's body once stored: each
    body line with its LF and, when quoted, one more ">", plus the empty
    line before the next envelope."""
    _head, body = split_article(article)
    return sum(len(line) + 1 + needs_quote(line) for line in body) + 1


def journal_path(path: str) -> str:
    """The journal of an mbox being written: the size of its whole messages."""
    return path + JOURNAL_SUFFIX


def complete_size(path: str) -> Tuple[int, int, str]:
    """(file size, size of its whole messages, how that was found).

    With a journal (left by a run that did not close the mbox: killed,
    crashed, power lost) the whole messages end where it says. Without one,
    a file that does not end in a line end was certainly cut in the middle
    of a message, which is then dropped back to the last envelope line. A
    file that ends in a line end is taken as complete: other writers end an
    mbox with a single LF.
    """
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return 0, 0, "no file"
    try:
        with open(journal_path(path), "rb") as fh:
            done = int(fh.read().strip() or b"0")
        return size, min(done, size), "journal"
    except (FileNotFoundError, ValueError):
        pass
    if size == 0:
        return 0, 0, "empty"
    with open(path, "rb") as fh:
        fh.seek(size - 1)
        if fh.read(1) == b"\n":
            return size, size, "ends with a line end"
        # find the last envelope line: "From " at the start of the file or after "\n\n"
        end = size
        block = 1 << 20
        carry = b""
        while end > 0:
            start = max(0, end - block)
            fh.seek(start)
            chunk = fh.read(end - start) + carry
            i = chunk.rfind(b"\n\nFrom ")
            if i >= 0:
                return size, start + i + 2, "cut message dropped"
            carry = chunk[:7]
            end = start
        return size, 0, "cut message dropped"


class MboxWriter:
    """Append-mode mboxrd writer with Message-ID deduplication, safe against
    crashes and other writers.

    - Each message goes to disk in one os.write on an O_APPEND descriptor; a
      write that fails half way (disk full, Ctrl-C) is cut back off.
    - While the mbox is open, its journal (path + ".inprogress") holds the
      size of the messages already fsynced; it is updated every flush_every
      messages, and removed by close() after a last fsync. A run that did not
      close the mbox leaves it behind, and the next one cuts the file back to
      that size before it reads the Message-IDs, so a half-written article is
      neither kept nor taken for present.
    - open() takes an exclusive lock (flock); a second writer gets MboxLocked.

    has_message_id() works before open() and changes nothing (dry runs).
    """

    def __init__(self, path: str, dedup: bool = True, flush_every: int = 100):
        self.path = path
        self.dedup = dedup
        self.flush_every = max(1, flush_every)
        _size, self._indexed_size, _how = complete_size(path)
        self._seen: Set[str] = index_message_ids(path, self._indexed_size) if dedup else set()
        self._fd: Optional[int] = None
        self._lock = threading.Lock()
        self._good = 0  # end of the last whole message
        self._sep = b""  # written before the next envelope line
        self._since_commit = 0
        self.writing = False  # inside os.write: a signal handler must not stop us here
        self.written = 0
        self.skipped_dup = 0
        self.bytes_written = 0

    def open(self) -> None:
        with self._lock:
            self._open_locked()

    def _open_locked(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise MboxLocked(errno.EAGAIN, "another process is writing this mbox", self.path)
            size, good, how = complete_size(self.path)
            if good < size:
                logger.warning(
                    "%s: dropping %s bytes after the last whole message (%s)", self.path, size - good, how
                )
                os.ftruncate(fd, good)
            if self.dedup and good != self._indexed_size:
                self._seen = index_message_ids(self.path, good)
                self._indexed_size = good
            self._good = good
            self._sep = b""
            if good > 0:
                with open(self.path, "rb") as fh:
                    fh.seek(max(0, good - 2))
                    tail = fh.read(2)
                # the next envelope line must follow an empty line
                self._sep = b"" if tail == b"\n\n" else (b"\n" if tail.endswith(b"\n") else b"\n\n")
            self._write_journal()
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def _write_journal(self) -> None:
        tmp = journal_path(self.path) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(b"%d\n" % self._good)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, journal_path(self.path))

    def _commit(self) -> None:
        """fsync the messages written so far, then record their end."""
        os.fsync(self._fd)
        self._write_journal()
        self._since_commit = 0

    def close(self) -> None:
        """fsync and close; raises if the data could not be written."""
        with self._lock:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                if os.fstat(fd).st_size > self._good:
                    os.ftruncate(fd, self._good)
                os.fsync(fd)
                os.remove(journal_path(self.path))
            finally:
                os.close(fd)  # also releases the lock

    def __enter__(self) -> "MboxWriter":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def has_message_id(self, message_id: Optional[str]) -> bool:
        if not self.dedup or not message_id or not message_id.strip():
            return False
        with self._lock:
            return msgid_key(message_id) in self._seen

    def write(self, article: bytes, dt: Optional[datetime] = None, message_id: Optional[str] = None) -> bool:
        """Write article unless it is a duplicate. Returns True if written.

        The key is the article's own Message-ID header (article_key), the
        same the index reads back from the mbox; message_id (e.g. from OVER)
        is not used for it.
        """
        size = stored_body_size(article)
        if size > MAX_STORED_BODY:
            raise ArticleTooBig(f"body {size} bytes as stored, mbox2nntp takes at most {MAX_STORED_BODY}")
        mid = article_key(article)
        if dt is None:
            dt = date_from_article(article)
        data = format_mbox_message(article, dt, sender_from_article(article))
        with self._lock:
            if self.dedup and mid in self._seen:
                self.skipped_dup += 1
                return False
            self._open_locked()
            data = self._sep + data
            self.writing = True
            try:
                view = memoryview(data)
                while view:
                    n = os.write(self._fd, view)
                    view = view[n:]
            except BaseException:
                # half a message must not stay: cut back to the last whole one
                try:
                    os.ftruncate(self._fd, self._good)
                except OSError:
                    pass
                raise
            finally:
                self.writing = False
            self._sep = b""
            self._good += len(data)
            self.bytes_written += len(data)
            self._seen.add(mid)
            self.written += 1
            self._since_commit += 1
            if self._since_commit >= self.flush_every:
                self._commit()
            return True
