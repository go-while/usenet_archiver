#!/usr/bin/env python3
"""Byte-safe NNTP client (stdlib sockets + optional TLS).

Article payloads are kept as bytes end to end. Only ASCII status lines are
decoded for control flow, so ISO-8859-1 / KOI8-R / Shift_JIS bodies survive.
"""

from __future__ import annotations

import fnmatch
import logging
import socket
import ssl
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class NNTPTimeout(Exception):
    """Raised when an NNTP socket read exceeds the configured timeout."""


class NNTPError(Exception):
    """Raised for unexpected NNTP protocol responses."""


class ConnectionClosed(NNTPError):
    """The server closed the connection (EOF where a reply was due)."""


class AuthError(NNTPError):
    """The server refused the user name or the password."""


class LineTooLong(NNTPError):
    """A line over MAX_LINE. The rest of it has been read and dropped, so the
    stream is still in step."""


# Limits on what the server may send. Both are the limits of the importer,
# mbox2nntp: go-lib-mbox2nntp's mbox.MaxLineLength (1 MiB, a longer line ends
# the whole file for it) and mbox2nntp's maxArticleSize (1 MiB). The longest
# line in the archive.org corpus (458 million lines) is 9,991 bytes.
MAX_LINE = 1 << 20  # bytes, line end included
MAX_ARTICLE = 1 << 20  # bytes of article content (lines plus LF)

# Default for response_timeout: the most one reply (a status line and its
# block) may take in all, however slowly the server keeps sending. The
# socket timeout only covers a single read.
DEFAULT_RESPONSE_TIMEOUT = 300

# Pause before a new try after a connect failed (seconds, times the try
# number): a provider often refuses a new connection while it has not yet
# released the one just dropped.
RECONNECT_DELAY = 1.0

# Status prefix (not an NNTP code) for an article that was read but not
# kept: over MAX_ARTICLE, or with a line over MAX_LINE. It is counted and
# skipped, like a missing article, since fetching it again cannot help.
TOO_BIG = "too big:"


def is_too_big(resp: str) -> bool:
    return isinstance(resp, str) and resp.startswith(TOO_BIG)


# ARTICLE answers that mean "this article is not there" (RFC 3977 6.2.1: 423
# no article with that number, 430 no article with that message-id). They are
# counted as missing. Any other answer that is not 220 is a failure: 480 needs
# authentication, 502 is access denied, 400/403/503 are server trouble.
MISSING_ARTICLE = ("423", "430")


def is_missing_article(resp: str) -> bool:
    """True when an ARTICLE status line says the article does not exist."""
    return isinstance(resp, str) and resp.startswith(MISSING_ARTICLE)


def _check_in_step(spec, resp: str) -> None:
    """Raise NNTPError when resp cannot be the answer to ARTICLE spec.

    A status line is three digits and a space; "220 n <id>" must name the
    article number asked for. Anything else means the stream is out of step
    (e.g. the late rest of an earlier reply), and reading on would pair
    replies with the wrong requests.
    """
    if len(resp) < 3 or not resp[:3].isdigit() or (len(resp) > 3 and resp[3] != " "):
        raise NNTPError(f"ARTICLE {spec}: not a status line, stream out of step: {resp[:80]!r}")
    if isinstance(spec, int) and resp.startswith("220"):
        parts = resp.split()
        if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) != spec:
            raise NNTPError(f"ARTICLE {spec}: reply is for article {parts[1]}, stream out of step")


def wildmat_match(name: str, wildmat: str) -> bool:
    """Whether name matches an RFC 3977 wildmat: patterns with * ? [...],
    separated by commas; one starting with ! excludes; the last pattern
    that matches decides."""
    result = False
    for pattern in wildmat.split(","):
        negate = pattern.startswith("!")
        if negate:
            pattern = pattern[1:]
        if fnmatch.fnmatchcase(name, pattern):
            result = not negate
    return result


class NNTPClient:
    def __init__(
        self,
        server: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_ssl: bool = True,
        verbose: bool = False,
        timeout: int = 60,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
    ):
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.verbose = verbose
        self.timeout = timeout
        self.response_timeout = response_timeout
        self._deadline: Optional[float] = None  # end of the current reply (monotonic)
        self.conn: Optional[socket.socket] = None
        self._buf = bytearray()
        self.current_group: Optional[str] = None  # last group selected with GROUP
        self.capabilities: Dict[str, List[str]] = {}
        self.logger = logging.getLogger(__name__)
        if verbose:
            self.logger.setLevel(logging.DEBUG)

    # --- transport ---------------------------------------------------------

    def connect(self) -> None:
        try:
            sock = socket.create_connection((self.server, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)
            if self.use_ssl:
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=self.server)
            self.conn = sock
            self._buf = bytearray()

            welcome = self._recv_status()
            if self.verbose:
                print(f"Welcome: {welcome}")
            # 200/201 = ready; 400/502 = refused (e.g. too many connections)
            if not welcome.startswith(("200", "201")):
                raise NNTPError(f"Server refused the connection: {welcome}")

            if self.username:
                self._authenticate()

            self.send("MODE READER")
            mode_resp = self._recv_status()
            if self.verbose:
                print(f"MODE response: {mode_resp}")

            self.capabilities = self._probe_capabilities()
        except NNTPError:
            self.close()
            raise
        except Exception as e:
            self.close()
            raise NNTPError(f"Connection error: {e}") from e
        except BaseException:  # a signal (the CLI's TerminatedBySignal), Ctrl-C
            self.close()
            raise

    def _authenticate(self) -> None:
        """AUTHINFO USER, then PASS when the server asks for it (RFC 4643)."""
        self.send(f"AUTHINFO USER {self.username}")
        user_resp = self._recv_status()
        if self.verbose:
            print(f"USER response: {user_resp}")
        if user_resp.startswith("281"):  # no password needed
            return
        if not user_resp.startswith("381"):
            raise AuthError(f"Authentication failed: {user_resp}")
        if not self.password:
            raise AuthError(f"Authentication failed: the server asks for a password and none was given ({user_resp})")
        self.send(f"AUTHINFO PASS {self.password}")
        auth_resp = self._recv_status()
        if self.verbose:
            print(f"PASS response: {auth_resp}")
        if not auth_resp.startswith("281"):
            raise AuthError(f"Authentication failed: {auth_resp}")

    def reconnect(self) -> None:
        """Drop the connection and open a new one, selecting the current group again.

        Use it after any error in the middle of a reply: a timeout or a cut
        leaves the stream out of step, and the next read would take the rest
        of the old reply for the answer to a new command.
        """
        group = self.current_group
        self.close()
        self.connect()
        if group:
            self.group(group)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None
        self._buf = bytearray()

    def quit(self) -> None:
        """QUIT and close. Never raises for a connection that is already
        gone (reset while idle, say): closing is all that is left to do."""
        try:
            if self.conn is not None:
                self.send("QUIT")
                self._recv_status()
        except (OSError, NNTPError, NNTPTimeout):
            pass
        finally:
            self.close()

    def send(self, command: str) -> None:
        if self.conn is None:
            raise NNTPError("Not connected")
        if "\r" in command or "\n" in command:
            # a name or a password with a line end would send a second command
            raise NNTPError("CR or LF inside an NNTP command")
        self.conn.settimeout(self.timeout)
        # UTF-8 (RFC 3977 3.1, RFC 4643 for AUTHINFO); a name that came from
        # the server undecodable (surrogateescape) goes back as it came
        self.conn.sendall(f"{command}\r\n".encode("utf-8", errors="surrogateescape"))

    def _recv_raw(self, nbytes: int = 65536) -> bytes:
        if self.conn is None:
            raise NNTPError("Not connected")
        timeout = self.timeout
        if self._deadline is not None:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise NNTPTimeout(f"reply not complete after {self.response_timeout} s")
            timeout = min(timeout, left)
        self.conn.settimeout(timeout)
        try:
            data = self.conn.recv(nbytes)
        except socket.timeout as e:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise NNTPTimeout(f"reply not complete after {self.response_timeout} s") from e
            raise NNTPTimeout("The read operation timed out") from e
        if not data:
            raise ConnectionClosed("Connection closed by server")
        return data

    def _readline(self) -> bytes:
        """Read one CRLF-terminated line as bytes (CRLF stripped).

        A line over MAX_LINE raises LineTooLong after the rest of it has been
        read and dropped: at most MAX_LINE bytes are held at a time.
        """
        start = 0
        while True:
            nl = self._buf.find(b"\n", start)
            if nl >= 0:
                if nl + 1 > MAX_LINE:
                    del self._buf[: nl + 1]
                    raise LineTooLong(f"line over {MAX_LINE} bytes")
                line = bytes(self._buf[:nl])
                del self._buf[: nl + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                return line
            if len(self._buf) > MAX_LINE:
                self._drop_rest_of_line()
                raise LineTooLong(f"line over {MAX_LINE} bytes")
            start = len(self._buf)
            self._buf += self._recv_raw()

    def _drop_rest_of_line(self) -> None:
        """Read and drop bytes up to and including the next LF."""
        self._buf.clear()
        while True:
            data = self._recv_raw()
            nl = data.find(b"\n")
            if nl >= 0:
                self._buf += data[nl + 1 :]
                return

    def _recv_status(self) -> str:
        # Each reply starts with its status line: the reply's time starts here.
        self._deadline = time.monotonic() + self.response_timeout
        line = self._readline()
        try:
            return line.decode("ascii", errors="replace").strip()
        except Exception:
            return line.decode("latin-1", errors="replace").strip()

    def _read_multiline(self, max_lines: Optional[int] = None) -> List[bytes]:
        """Read an RFC 3977 dot-terminated multiline response; unstuff lines.

        More than max_lines lines raises NNTPError (the reply is then left
        half read: reconnect).
        """
        lines: List[bytes] = []
        for line in self._iter_multiline():
            lines.append(line)
            if max_lines is not None and len(lines) > max_lines:
                raise NNTPError(f"reply has more than {max_lines} lines")
        return lines

    def _read_article(self) -> Tuple[bytes, bool]:
        """Read an article block; returns (content, too_big).

        Content is the unstuffed lines joined with LF. An article over
        MAX_ARTICLE, or with a line over MAX_LINE, is read to its end and
        dropped (too_big): the stream stays in step and memory bounded.
        """
        lines: List[bytes] = []
        size = 0
        too_big = False
        while True:
            try:
                line = self._readline()
            except LineTooLong:
                too_big, lines = True, []
                continue
            if line == b".":
                break
            if too_big:
                continue
            if line.startswith(b"."):
                line = line[1:]
            size += len(line) + 1
            if size > MAX_ARTICLE:
                too_big, lines = True, []
                continue
            lines.append(line)
        return (b"" if too_big else b"\n".join(lines)), too_big

    def _iter_multiline(self) -> Iterable[bytes]:
        while True:
            line = self._readline()
            if line == b".":
                return
            if line.startswith(b"."):
                line = line[1:]
            yield line

    # --- protocol helpers --------------------------------------------------

    def _probe_capabilities(self) -> Dict[str, List[str]]:
        """CAPABILITIES; {} when the server does not know the command.

        A timeout or a cut inside the reply raises: the stream is then out of
        step, and the late reply would be read as the answer to GROUP.
        """
        caps: Dict[str, List[str]] = {}
        self.send("CAPABILITIES")
        resp = self._recv_status()
        if not resp.startswith("101"):
            return caps
        for raw in self._read_multiline():
            text = raw.decode("ascii", errors="replace").strip()
            if not text:
                continue
            parts = text.split()
            caps[parts[0].upper()] = parts[1:]
        return caps

    def has_capability(self, name: str) -> bool:
        return name.upper() in self.capabilities

    def group(self, newsgroup: str) -> Tuple[int, int, str]:
        self.send(f"GROUP {newsgroup}")
        resp = self._recv_status()
        if self.verbose:
            print(f"GROUP response: {resp}")
        if not resp.startswith("211"):
            raise NNTPError(f"Failed to select group {newsgroup}: {resp}")
        parts = resp.split()
        if len(parts) < 4:
            raise NNTPError(f"Invalid GROUP response: {resp}")
        first, last = int(parts[2]), int(parts[3])
        self.current_group = newsgroup
        return first, last, resp

    def stat(self, article_id: int) -> Tuple[int, str]:
        self.send(f"STAT {article_id}")
        resp = self._recv_status()
        if self.verbose:
            print(f"STAT {article_id} response: {resp}")
        if resp.startswith("223"):
            parts = resp.split()
            if len(parts) < 2:
                raise NNTPError(f"Invalid STAT response: {resp}")
            return int(parts[1]), resp
        return 0, resp

    def article(self, article_spec) -> Tuple[bytes, str]:
        """Fetch ARTICLE by number or Message-ID. Empty bytes if missing."""
        results = self.articles_pipelined([article_spec])
        _spec, content, resp = results[0]
        return content, resp

    def articles_pipelined(
        self, article_specs: Sequence
    ) -> List[Tuple[object, bytes, str]]:
        """Pipeline ARTICLE commands, then read responses in order (RFC 3977).

        Sends all requests in ``article_specs`` before reading any response,
        which hides per-article round-trip latency. Returns one triple per
        input: ``(spec, content_bytes, status_line)``. Missing articles yield
        empty ``content_bytes``; so does an article over the limits, with a
        status line starting with TOO_BIG.
        """
        if not article_specs:
            return []
        # Send the whole batch first.
        for spec in article_specs:
            self.send(f"ARTICLE {spec}")
        results: List[Tuple[object, bytes, str]] = []
        for spec in article_specs:
            resp = self._recv_status()
            if self.verbose:
                print(f"ARTICLE {spec} response: {resp}")
            _check_in_step(spec, resp)
            if not resp.startswith("220"):
                results.append((spec, b"", resp))
                continue
            content, too_big = self._read_article()
            if too_big:
                results.append((spec, b"", f"{TOO_BIG} {resp} (over {MAX_ARTICLE} bytes, or a line over {MAX_LINE})"))
                continue
            if self.verbose and content:
                print(f"Fetched article {spec} (first 100 bytes): {content[:100]!r}...")
            results.append((spec, content, resp))
        return results

    def over(self, start_id: int, end_id: int) -> Tuple[List[bytes], str, str]:
        """Issue OVER or XOVER for an article-number range.

        Returns (lines, command_used, status_response). 423 (no articles in
        the range) gives no lines. When the server knows neither command
        (500/501/502 to both), command_used is "". Any other answer raises
        NNTPError: it is a failure, not an empty range.
        """
        commands: Sequence[str] = ("OVER", "XOVER")
        # Prefer OVER when advertised; still try both.
        if self.has_capability("OVER") or self.has_capability("XOVER"):
            if not self.has_capability("OVER") and self.has_capability("XOVER"):
                commands = ("XOVER", "OVER")
        last_resp = ""
        for cmd in commands:
            self.send(f"{cmd} {start_id}-{end_id}")
            resp = self._recv_status()
            last_resp = resp
            if self.verbose:
                print(f"{cmd} {start_id}-{end_id} response: {resp}")
            if resp.startswith("224"):
                # one line per article at most: a longer reply is hostile or out of step
                return self._read_multiline(max_lines=end_id - start_id + 1), cmd, resp
            if resp.startswith("423"):  # no articles in that range
                return [], cmd, resp
            # 500/501/502 = not supported; try next
            if resp.startswith(("500", "501", "502")):
                continue
            raise NNTPError(f"{cmd} {start_id}-{end_id} failed: {resp}")
        return [], "", last_resp

    def xhdr_date(self, start_id: int, end_id: int) -> List[Tuple[int, str]]:
        """XHDR DATE fallback; returns list of (article_id, date_string).

        423 (no articles in the range) gives []; any other failure raises
        NNTPError.
        """
        self.send(f"XHDR DATE {start_id}-{end_id}")
        resp = self._recv_status()
        if self.verbose:
            print(f"XHDR DATE {start_id}-{end_id} response: {resp}")
        if resp.startswith("423"):
            return []
        if not resp.startswith("221"):
            raise NNTPError(f"XHDR DATE {start_id}-{end_id} failed: {resp}")
        result: List[Tuple[int, str]] = []
        for raw in self._read_multiline(max_lines=end_id - start_id + 1):
            try:
                line = raw.decode("ascii", errors="replace")
            except Exception:
                continue
            parts = line.split(" ", 1)
            if parts[0].strip().isdigit():
                # a number alone: the value is empty (some servers trim the
                # space); the article is there all the same
                result.append((int(parts[0].strip()), parts[1].strip() if len(parts) == 2 else ""))
        return result

    def list_active(self, wildmat: Optional[str] = None) -> List[str]:
        """LIST ACTIVE [wildmat], falling back to LIST. Returns group names.

        A bare LIST has no wildmat: its list is filtered here instead.
        """
        if wildmat:
            cmd = f"LIST ACTIVE {wildmat}"
        else:
            cmd = "LIST ACTIVE"
        self.send(cmd)
        resp = self._recv_status()
        filter_here = False
        if not resp.startswith("215"):
            # Fall back to bare LIST (RFC 977 servers)
            self.send("LIST")
            resp = self._recv_status()
            if not resp.startswith("215"):
                raise NNTPError(f"LIST failed: {resp}")
            filter_here = bool(wildmat)
        names: List[str] = []
        for raw in self._read_multiline():
            try:
                text = raw.decode("utf-8", errors="surrogateescape").strip()
            except Exception:
                continue
            if not text:
                continue
            name = text.split()[0]
            if filter_here and not wildmat_match(name, wildmat):
                continue
            names.append(name)
        return names

    def post(self, message: bytes) -> str:
        """POST a raw RFC 5322 message (bytes). Returns the final status line."""
        self.send("POST")
        resp = self._recv_status()
        if not resp.startswith("340"):
            raise NNTPError(f"POST rejected: {resp}")
        if self.conn is None:
            raise NNTPError("Not connected")
        # Dot-stuff lines beginning with '.'. Only CRLF is a line end: a lone
        # CR stays inside its line.
        lines = message.replace(b"\r\n", b"\n").split(b"\n")
        stuffed: List[bytes] = []
        for line in lines:
            if line.startswith(b"."):
                stuffed.append(b"." + line)
            else:
                stuffed.append(line)
        payload = b"\r\n".join(stuffed)
        if not payload.endswith(b"\r\n"):
            payload += b"\r\n"
        payload += b".\r\n"
        self.conn.settimeout(self.timeout)
        self.conn.sendall(payload)
        final = self._recv_status()
        if not final.startswith("240"):
            raise NNTPError(f"POST failed: {final}")
        return final
