#!/usr/bin/env python3
"""BSD-licensed message plugins for the Usenet archiver.

Each plugin is a callable(message=RawArticle, **kwargs) -> RawArticle | None.
Returning None drops the message from the output.

Plugins work on the article's bytes: a RawArticle is its header lines and
body lines, and only the fields a plugin removes or adds change. The archive
stays byte-exact otherwise (the email package re-encoded and refolded every
header it re-serialised).
"""

from __future__ import annotations

import functools
from typing import Any, Callable, List, Optional, Set

class RawArticle:
    """An article as header lines and body lines (bytes, without line ends).

    to_bytes() gives the input back unchanged when nothing was edited.
    Field names are matched without regard to case; a field includes its
    continuation lines.
    """

    def __init__(self, data: bytes):
        lines = data.split(b"\n")
        self._ends_with_newline = data.endswith(b"\n")
        if self._ends_with_newline:
            lines.pop()
        # the empty line between header and body as it was (LF or CRLF), or
        # None when the article has none
        self._sep: Optional[bytes] = None
        self.head, self.body = lines, []
        for i, line in enumerate(lines):
            if line in (b"", b"\r"):
                self.head, self._sep, self.body = lines[:i], line, lines[i + 1 :]
                break

    @classmethod
    def from_bytes(cls, data: bytes) -> "RawArticle":
        return cls(data)

    def to_bytes(self) -> bytes:
        lines = list(self.head)
        if self._sep is not None or self.body:
            lines.append(b"" if self._sep is None else self._sep)
            lines.extend(self.body)
        out = b"\n".join(lines)
        if self._ends_with_newline:
            out += b"\n"
        return out

    # --- fields -----------------------------------------------------------

    @staticmethod
    def _name(line: bytes) -> Optional[str]:
        if line[:1] in (b" ", b"\t"):
            return None
        name, sep, _ = line.partition(b":")
        return name.strip().decode("ascii", errors="replace").lower() if sep else None

    def _fields(self):
        """(name, start, end) of each field; end is exclusive."""
        out = []
        i = 0
        while i < len(self.head):
            name = self._name(self.head[i])
            j = i + 1
            while j < len(self.head) and self.head[j][:1] in (b" ", b"\t"):
                j += 1
            out.append((name, i, j))
            i = j
        return out

    def __contains__(self, name: str) -> bool:
        want = name.lower()
        return any(n == want for n, _, _ in self._fields())

    def __getitem__(self, name: str) -> Optional[str]:
        """The first field's value, unfolded, as text; None when absent."""
        want = name.lower()
        for n, i, j in self._fields():
            if n == want:
                value = b" ".join([self.head[i].partition(b":")[2]] + self.head[i + 1 : j])
                return b" ".join(value.split()).decode("utf-8", errors="replace")
        return None

    def remove(self, name: str) -> None:
        """Remove every field called name, with its continuation lines."""
        self.keep_only_if(lambda n: n != name.lower())

    def keep_only(self, names: Set[str]) -> None:
        """Remove every field whose name is not in names (lower case)."""
        self.keep_only_if(lambda n: n in names)

    def keep_only_if(self, keep: Callable[[Optional[str]], bool]) -> None:
        head: List[bytes] = []
        for n, i, j in self._fields():
            if n is None or keep(n):  # a line that is not a field stays
                head.extend(self.head[i:j])
        self.head = head

    def add(self, name: str, value: str) -> None:
        """Add a field after the last header line."""
        self.head.append(f"{name}: {value}".encode("utf-8"))

    def get_payload(self) -> str:
        return b"\n".join(self.body).decode("utf-8", errors="replace")


def debug(*args, message: Optional[RawArticle] = None, **kwargs):
    print(f"debug(*{args!r}, **{kwargs!r})")
    return message


def strip_headers(headers: str = "To,Cc,Bcc", message: Optional[RawArticle] = None):
    """Delete the named headers (comma-separated)."""
    if message is None:
        return None
    for header in headers.split(","):
        header = header.strip()
        if header:
            message.remove(header)
    return message


def mimify(type: str = "text/plain", charset: str = "US-ASCII", message: Optional[RawArticle] = None):
    """Add a default Content-Type when missing."""
    if message is None:
        return None
    if "Content-Type" not in message:
        message.add("Content-Type", f"{type}; charset={charset}")
    return message


def keep_headers(headers: str = "From,Subject,Date,Message-ID,Newsgroups,References,In-Reply-To",
                 message: Optional[RawArticle] = None):
    """Retain only the listed headers (header-only archive). Body is cleared."""
    if message is None:
        return None
    message.keep_only({h.strip().lower() for h in headers.split(",") if h.strip()})
    message.body = []
    return message


_BUILTIN = {
    "debug": debug,
    "strip_headers": strip_headers,
    "mimify": mimify,
    "keep_headers": keep_headers,
}


def parse_plugin_spec(spec: str) -> Callable[[RawArticle], Optional[RawArticle]]:
    """Parse ``name`` or ``name:arg:arg:key=value`` into a partial callable.

    Positional args before any key=value become *args; key=value become kwargs.
    The resulting callable accepts only ``message=``.
    """
    parts = spec.split(":")
    name = parts[0]
    if name not in _BUILTIN:
        raise ValueError(f"Unknown plugin: {name!r} (available: {', '.join(sorted(_BUILTIN))})")
    func = _BUILTIN[name]
    args: List[Any] = []
    kwargs: dict = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            kwargs[key] = value
        else:
            args.append(part)
    return functools.partial(func, *args, **kwargs)


def apply_plugins(message: RawArticle, plugins: List[Callable[[RawArticle], Optional[RawArticle]]]) -> Optional[RawArticle]:
    """Run plugins in order; stop and drop if any returns None."""
    for plugin in plugins:
        message = plugin(message=message)  # type: ignore[call-arg, arg-type]
        if message is None:
            return None
    return message


__all__ = [
    "RawArticle",
    "debug",
    "mimify",
    "strip_headers",
    "keep_headers",
    "parse_plugin_spec",
    "apply_plugins",
]
