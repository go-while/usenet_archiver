#!/usr/bin/env python3
"""Server and credential resolution for the Usenet archiver.

Precedence for password: explicit flag > password file > $NNTP_PASSWORD > ~/.netrc.
Server: --server > $NNTPSERVER > /etc/news/server.
"""

from __future__ import annotations

import netrc
import os
import re
from typing import NamedTuple, Optional, Tuple


_SPLIT_HOST_RE = re.compile(
    r"^ (?: \[ ( [^\[\]]+ ) \] | ( [^:]+ ) | ( .* ) ) (?: : ([0-9]+) )? $",
    re.VERBOSE,
)


class ResolvedEndpoint(NamedTuple):
    host: str
    port: Optional[int]
    username: Optional[str]
    password: Optional[str]


def split_host(host: str, default_port: Optional[int] = None) -> Tuple[str, Optional[int]]:
    """Parse host, host:port, or [ipv6]:port. BSD-licensed rewrite of sinntp's helper."""
    match = _SPLIT_HOST_RE.match(host)
    if match is None:
        raise ValueError(f"Invalid host specification: {host!r}")
    host1, host2, host3, port = match.groups()
    hostname = host1 or host2 or host3
    if port is None:
        return hostname, default_port
    return hostname, int(port)


def get_default_nntp_server() -> Optional[str]:
    """Resolve $NNTPSERVER, then /etc/news/server."""
    env = os.environ.get("NNTPSERVER")
    if env:
        return env.strip() or None
    try:
        with open("/etc/news/server", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return None


def _netrc_lookup(hostname: str) -> Tuple[Optional[str], Optional[str]]:
    """(login, password) of hostname's ~/.netrc entry, or (None, None)."""
    try:
        nrc = netrc.netrc()
    except (OSError, netrc.NetrcParseError):
        return None, None
    # netrc matches on host; try exact then bare
    entry = nrc.authenticators(hostname)
    if entry is None:
        return None, None
    login, _account, password = entry
    return login, password


def resolve_credentials(
    server: Optional[str],
    port: Optional[int],
    username: Optional[str],
    password: Optional[str],
    password_file: Optional[str] = None,
    use_netrc: bool = True,
    use_ssl: bool = True,
) -> ResolvedEndpoint:
    """Resolve host/port/username/password with documented precedence.

    Raises SystemExit(2) when no server can be determined (caller may catch).
    """
    if not server:
        server = get_default_nntp_server()
    if not server:
        raise SystemExit(2)

    host, embedded_port = split_host(server, default_port=None)
    if port is None:
        port = embedded_port
    if port is None:
        port = 563 if use_ssl else 119

    resolved_user = username
    resolved_pass = password

    if resolved_pass is None and password_file:
        # Entire file is the password, read as it is (newline="": no
        # newline translation, so a CR inside it is seen and refused below;
        # utf-8-sig: a BOM, as Windows Notepad writes it, is dropped)
        with open(password_file, encoding="utf-8-sig", newline="") as fh:
            raw = fh.read()
        # one line end, LF or CRLF, ends the file; it is not part of the password
        if raw.endswith("\r\n"):
            raw = raw[:-2]
        elif raw.endswith("\n"):
            raw = raw[:-1]
        resolved_pass = raw

    if resolved_pass is None:
        env_pass = os.environ.get("NNTP_PASSWORD")
        if env_pass is not None:
            resolved_pass = env_pass

    if use_netrc and (resolved_user is None or resolved_pass is None):
        n_user, n_pass = _netrc_lookup(host)
        # the entry's password goes with its login only (as in curl and
        # ftp): another user name given gets none of it
        if resolved_user is None or not n_user or n_user == resolved_user:
            if resolved_user is None:
                resolved_user = n_user
            if resolved_pass is None:
                resolved_pass = n_pass

    from .policy import check_server_allowed

    check_server_allowed(host)

    for what, value in (("user name", resolved_user), ("password", resolved_pass)):
        if value and ("\r" in value or "\n" in value):
            # it would end the AUTHINFO command early (a password file
            # with several lines, or with Windows line ends on every line)
            raise ValueError(f"The {what} contains a line break")

    return ResolvedEndpoint(host=host, port=port, username=resolved_user, password=resolved_pass)
