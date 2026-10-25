"""Usenet Archiver — NNTP text archive client (stdlib only)."""

import sys

__version__ = "2.0.1+gw3"

# Every entry point imports this file first. Keep it free of syntax newer
# Pythons need (no f-strings, no __future__ imports), so that an older one
# gets this message and not a SyntaxError from inside the package.
if sys.version_info < (3, 9):
    raise SystemExit(
        "usenet-archiver needs Python 3.9 or newer; %s is Python %d.%d"
        % (sys.executable or "this interpreter", sys.version_info[0], sys.version_info[1])
    )
