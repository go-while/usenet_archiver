#!/usr/bin/env python3
"""Back-compat shim: ``python3 app/usenet_archiver.py`` → package entry.

Version 1 of this script took --server, --port, --username, --password,
--newsgroup, --no-ssl, --verbose, --timeout, --start-date and --end-date
and pulled one group. pull takes all of them, so such a command line runs
as ``-c pull`` with the same arguments.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from usenet_archiver.app import run

if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0].startswith("--") and args[0] not in ("--help", "--version"):
        sys.argv = [sys.argv[0], "-c", "pull", *args]
    run()
