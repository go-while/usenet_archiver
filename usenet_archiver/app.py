"""Application entry: GUI by default, ``-c`` for CLI-only."""

from __future__ import annotations

import os
import sys

# Top-level flags that should never launch the GUI.
_HELP_FLAGS = frozenset({"-h", "--help"})
_VERSION_FLAGS = frozenset({"-V", "--version"})


def run() -> None:
    """Zipapp / console_scripts / ``python -m usenet_archiver`` entry point.

    Without arguments it launches the Tk GUI. Any argument means the CLI: a
    leading ``-c`` is dropped, and the rest goes to the CLI parser, which
    reports a usage error (exit 2) for what it does not know. Arguments
    never start the GUI, so a cron line with a typo fails instead of
    waiting for a window nobody sees.

    ``--help`` / ``-h`` and ``--version`` / ``-V`` work without ``-c``.
    """
    argv = sys.argv[1:]

    # Help / version at the top level (no -c required).
    if argv and argv[0] in _HELP_FLAGS:
        from .cli import build_parser

        build_parser().print_help()
        raise SystemExit(0)

    if argv and argv[0] in _VERSION_FLAGS:
        from . import __version__

        sys.stdout.write(f"usenet-archiver {__version__}\n")
        raise SystemExit(0)

    if argv:
        if argv[0] == "-c":
            argv = argv[1:]
        sys.argv = [sys.argv[0], *argv]
        from .cli import run as cli_run

        cli_run()
        return

    prog = sys.argv[0]
    if os.path.basename(prog) == "__main__.py":
        prog = f"{os.path.basename(sys.executable)} -m usenet_archiver"
    try:
        from .gui import main as gui_main
    except ImportError as e:
        sys.stderr.write(
            "error: Tkinter GUI is unavailable ({0}).\n"
            "Install python3-tk (or your OS Tk package), or run the CLI with:\n"
            "  {1} -c pull --help\n"
            "Top-level help (no GUI): {1} --help\n".format(e, prog)
        )
        raise SystemExit(1) from e
    gui_main()


if __name__ == "__main__":
    run()
