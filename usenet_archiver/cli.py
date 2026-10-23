#!/usr/bin/env python3
"""Usenet Archiver CLI — fetch NNTP articles into mbox (sinntp-parity features)."""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from .creds import resolve_credentials
from .fetch import fetch_articles_parallel
from .mboxout import ArticleTooBig, MboxWriter
from . import nntp
from .nntp import (
    DEFAULT_RESPONSE_TIMEOUT,
    MAX_ARTICLE,
    AuthError,
    ConnectionClosed,
    NNTPClient,
    NNTPError,
    NNTPTimeout,
    is_missing_article,
    is_too_big,
)
from .overview import find_articles_in_date_range, overview_is_missing
from .plugins import RawArticle, apply_plugins, parse_plugin_spec, removes_message_id

# Global so SIGTERM can close an open writer / stop workers.
_ACTIVE_WRITER: Optional[MboxWriter] = None
_STOP_EVENT: Optional[threading.Event] = None
_TERMINATED = False


class TerminatedBySignal(BaseException):
    """Raised when SIGTERM is received during a pull.

    A BaseException, like KeyboardInterrupt: the "except Exception" blocks on
    the way (connect, the capabilities probe, a failed group) must not take
    it for an error and go on.
    """


def _check_stop() -> None:
    """Raise TerminatedBySignal once a stop was asked for: SIGTERM, or Stop
    in the GUI (which sets _TERMINATED and the stop event from its thread,
    where nothing can be raised in the pull)."""
    if _TERMINATED or (_STOP_EVENT is not None and _STOP_EVENT.is_set()):
        raise TerminatedBySignal("stopped")


def _handle_sigterm(signum, frame):
    """Stop the pull. Never touches the writer: the handler runs in the main
    thread, which may be inside MboxWriter.write holding its lock (closing it
    from here deadlocked). In the middle of os.write the stop is left to the
    fetch loop, which sees the stop event; anywhere else it is raised now.
    pull_group's cleanup closes the writer, which keeps whole messages only."""
    global _TERMINATED
    _TERMINATED = True
    if _STOP_EVENT is not None:
        _STOP_EVENT.set()
    if _ACTIVE_WRITER is not None and _ACTIVE_WRITER.writing:
        return
    raise TerminatedBySignal("terminated by signal")


def parse_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError("Date must be in YYYY-MM-DD format") from e


def parse_group_spec(spec: str) -> Tuple[str, Optional[str]]:
    """Parse ``group`` or ``group>filename``."""
    if ">" in spec:
        group, filename = spec.split(">", 1)
        return group.strip(), filename.strip() or None
    return spec.strip(), None


def collect_group_specs(newsgroups: Optional[List[str]], groups_file: Optional[str]) -> List[Tuple[str, Optional[str]]]:
    specs: List[Tuple[str, Optional[str]]] = []
    for item in newsgroups or []:
        g, f = parse_group_spec(item)
        if g:
            specs.append((g, f))
    if groups_file:
        with open(groups_file, encoding="utf-8-sig") as fh:  # -sig: a BOM is dropped
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                g, f = parse_group_spec(line)
                if g:
                    specs.append((g, f))
    return specs


def filter_binary_groups(
    specs: List[Tuple[str, Optional[str]]], logger: Optional[logging.Logger] = None
) -> List[Tuple[str, Optional[str]]]:
    """Remove binary-looking groups; log each skip."""
    from .policy import filter_group_specs

    kept, skipped = filter_group_specs(specs)
    if skipped and logger is not None:
        for name in skipped:
            logger.info("Skipping binary newsgroup %s", name)
    elif skipped:
        for name in skipped:
            sys.stderr.write(f"Skipping binary newsgroup {name}\n")
    return kept


def normalize_date_range(start_date, end_date, verbose: bool = False, today=None):
    """Fill a missing end: lone start → end=today UTC. A lone end stays
    alone: everything up to it, however old (Usenet began in 1979, and
    broken dates go further back).

    An open-ended ``start`` with no ``end`` resolves ``end`` to *today* (UTC).
    Running the same "since DATE" job on a later calendar day therefore yields a
    different window and a different mbox name.

    ``today`` may be a ``date`` or ``datetime`` (for tests). Returns
    ``(start_date, end_date)``. Raises ``ValueError`` if start > end.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    elif hasattr(today, "date") and callable(today.date):
        today = today.date()

    if start_date is not None and end_date is None:
        end_date = datetime.combine(today, datetime.min.time())
        if verbose:
            print(f"Note: --end-date omitted; using end date {end_date.date()} (UTC).")
    elif end_date is not None and start_date is None and verbose:
        print("Note: --start-date omitted; no lower bound.")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start date must be on or before end date")
    return start_date, end_date


def mbox_name_for(group: str, filename: Optional[str], start_date, end_date) -> str:
    """Build the output mbox path. Date-window jobs get distinct dated names.

    Example: start=2025-07-01 with no end on July 10 →
    ``group-20250701-20250710.mbox``; the same request on July 11 →
    ``group-20250701-20250711.mbox`` (a separate pull). An end alone →
    ``group-until-20250710.mbox``.
    """
    if filename:
        return filename
    from .policy import group_name_problem

    problem = group_name_problem(group)
    if problem:
        raise ValueError(f"invalid newsgroup name {group!r}: {problem}")
    if start_date is not None and end_date is not None:
        return f"{group}-{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}.mbox"
    if end_date is not None:
        name = f"{group}-until-{end_date.strftime('%Y%m%d')}.mbox"
        # version 2.0.1 started a lone end at 1990-01-01: keep using its mbox
        old = f"{group}-19900101-{end_date.strftime('%Y%m%d')}.mbox"
        if not os.path.exists(name) and os.path.exists(old):
            return old
        return name
    return f"{group}.mbox"


def setup_logging(verbose: bool, syslog: bool, log_filename: Optional[str] = None) -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    if syslog:
        try:
            from logging.handlers import SysLogHandler

            handler = SysLogHandler(facility=SysLogHandler.LOG_NEWS)
            handler.setFormatter(logging.Formatter("usenet_archiver: %(message)s"))
            root.addHandler(handler)
        except Exception as e:
            sys.stderr.write(f"Warning: syslog unavailable ({e}), using stderr\n")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_filename:
        fh = logging.FileHandler(log_filename)
        fh.setFormatter(formatter)
        root.addHandler(fh)

    return logging.getLogger("usenet_archiver")


def normalize_message_id(mid: str) -> str:
    mid = mid.strip()
    if "@" not in mid:
        raise ValueError("Message-ID must contain '@'")
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    return mid


def mark_completed(
    completed_log: str, mbox_filename: str, logger: logging.Logger, group: Optional[str] = None
) -> None:
    """Append the job to the completed-jobs log after a successful pull.

    One line per job: ``mbox<TAB>group``. The group is part of the job:
    several groups can share an mbox (``-g a>x.mbox -g b>x.mbox``), and each
    is complete on its own.
    """
    line = f"{mbox_filename}\t{group}" if group else mbox_filename
    try:
        with open(completed_log, "a", encoding="utf-8") as fh:
            fh.write(f"{line}\n")
            fh.flush()
            os.fsync(fh.fileno())
        logger.info("Recorded successful pull of %s (%s) in %s", mbox_filename, group, completed_log)
    except OSError as e:
        logger.error("Failed to update %s: %s", completed_log, e)


def is_completed(completed_log: str, mbox_filename: str, group: str) -> bool:
    """Whether completed_log lists this job: the line ``mbox<TAB>group``.

    A line with the mbox name alone (written before the group was recorded)
    counts only for the group's own default mbox, ``group.mbox`` or
    ``group-YYYYMMDD-YYYYMMDD.mbox``: a chosen name may be shared by several
    groups, and the line does not say which of them was complete.
    """
    if not os.path.exists(completed_log):
        return False
    default = mbox_filename == f"{group}.mbox" or re.fullmatch(
        re.escape(group) + r"-\d{8}-\d{8}\.mbox", mbox_filename
    )
    with open(completed_log, encoding="utf-8", errors="surrogateescape") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if "\t" in line:
                if line.rsplit("\t", 1) == [mbox_filename, group]:
                    return True
            elif default and line.strip() == mbox_filename:
                return True
    return False


def _record_completed(completed_log: str, mbox_filename: str, logger: logging.Logger, group: str) -> None:
    """mark_completed, unless a SIGTERM came in: a stopped run records nothing."""
    if _TERMINATED:
        raise TerminatedBySignal("terminated")
    mark_completed(completed_log, mbox_filename, logger, group)


# Tries for GROUP on the main connection when it was lost or a new one is refused.
GROUP_TRIES = 3


def _select_group(client: NNTPClient, group: str, logger: logging.Logger):
    """GROUP on the main connection, on a new one when needed.

    The connection may have been closed while it sat idle (the mbox of the
    group before being indexed, a group with nothing to fetch), and a new
    one may be refused for a moment while the fetch connections of the
    group before are released. Both are tried again. Any answer to GROUP
    (411 no such group) and a refused login are not.
    """
    for attempt in range(1, GROUP_TRIES + 1):
        connecting = client.conn is None
        try:
            if connecting:
                client.connect()
                connecting = False
            return client.group(group)
        except (NNTPError, NNTPTimeout, OSError) as e:
            lost = isinstance(e, (ConnectionClosed, NNTPTimeout, OSError))
            if isinstance(e, AuthError) or not (connecting or lost) or attempt == GROUP_TRIES:
                raise
            logger.warning(
                "%s for %s (%s); new connection, try %s of %s",
                "Connect failed" if connecting else "Connection lost",
                group, e, attempt + 1, GROUP_TRIES,
            )
            client.close()
            if connecting:  # refused: give the server a moment
                time.sleep(nntp.RECONNECT_DELAY * attempt)
    raise AssertionError("unreachable")


def pull_group(
    client: NNTPClient,
    group: str,
    mbox_filename: str,
    start_date,
    end_date,
    overview_chunk: int,
    dedup: bool,
    plugins: list,
    completed_log: str,
    logger: logging.Logger,
    conn_kwargs: dict,
    connections: int = 8,
    pipeline_depth: int = 32,
    skip_completed: bool = False,
    dry_run: bool = False,
) -> bool:
    """Pull one group. Returns True when the job is complete (recorded in
    completed_newsgroups.log, or nothing to do) and False when it did not get
    every article; raises on a failure that stops it."""
    global _ACTIVE_WRITER

    # Optional short-circuit: skip an exact job name already in the completed log.
    # This is off by default because Message-ID dedup + append make re-pulls
    # incremental (a re-run only fetches articles not already in the mbox), which
    # is exactly what "catch up on new posts" needs. Enable to hard-skip finished
    # dated windows.
    if skip_completed:
        try:
            if is_completed(completed_log, mbox_filename, group):
                if os.path.exists(mbox_filename):
                    logger.info(
                        "Skipping %s → %s (already in %s; disable skip-completed to catch up)",
                        group,
                        mbox_filename,
                        completed_log,
                    )
                    return True
                logger.info(
                    "%s → %s is listed in %s but the mbox is missing; pulling again",
                    group,
                    mbox_filename,
                    completed_log,
                )
        except OSError as e:
            logger.warning("Could not read %s: %s", completed_log, e)

    # the connection is closed during each fetch, and may have been lost
    # while idle
    first, last, group_resp = _select_group(client, group, logger)
    logger.info("GROUP %s: %s (first=%s last=%s)", group, group_resp, first, last)
    if first == 0 and last == 0:
        if dry_run:
            logger.info("DRY-RUN: group %s is empty (would write %s)", group, mbox_filename)
            return True
        logger.info("Group %s is empty", group)
        _record_completed(completed_log, mbox_filename, logger, group)
        return True

    seen: List[int] = []
    rows = find_articles_in_date_range(
        client,
        first,
        last,
        start_date,
        end_date,
        chunk_size=overview_chunk,
        seen=seen,
        check=_check_stop,
    )
    _check_stop()
    overview_count = len(rows)
    logger.info("Overview matched %s articles for %s", overview_count, group)
    if not rows and not sum(seen) and first <= last:
        # no row at all for a range GROUP reported: either the articles are
        # gone, or the server's overview database lost them
        present = overview_is_missing(client, first, last)
        if present is not None:
            logger.error(
                "The overview of %s lists no articles, but the server has article %s: "
                "its overview is incomplete; not adding %s to %s",
                group, present, mbox_filename, completed_log,
            )
            return False

    # Index existing mbox for dedup without opening for append (dry-run must not
    # create or modify the mbox / completed log).
    writer = MboxWriter(mbox_filename, dedup=dedup, flush_every=100)
    _check_stop()  # indexing a large mbox takes a while
    if dedup:
        rows = [r for r in rows if not writer.has_message_id(r.message_id)]
        logger.info("After dedup filter: %s articles to fetch", len(rows))

    if dry_run:
        date_note = ""
        dated = [r.date for r in rows if getattr(r, "date", None) is not None]
        if dated:
            date_note = f", article dates {min(dated).date()} .. {max(dated).date()}"
        plugin_note = f", plugins={len(plugins)}" if plugins else ""
        logger.info(
            "DRY-RUN: would fetch %s article(s) for %s → %s "
            "(overview=%s, already_in_mbox=%s%s%s; no ARTICLE / mbox write)",
            len(rows),
            group,
            mbox_filename,
            overview_count,
            overview_count - len(rows),
            date_note,
            plugin_note,
        )
        return True

    if not rows:
        logger.info("Nothing to fetch for %s", group)
        _record_completed(completed_log, mbox_filename, logger, group)
        return True

    _ACTIVE_WRITER = writer
    articles_saved = 0
    articles_skipped = 0
    articles_missing = 0
    articles_too_big = 0
    articles_errors = 0
    try:
        writer.open()

        def on_article(row, content, resp):
            nonlocal articles_saved, articles_skipped, articles_missing, articles_too_big, articles_errors
            if _TERMINATED:
                return
            if not content:
                if is_missing_article(resp):
                    articles_missing += 1
                    logger.debug("Article %s missing: %s", row.article_id, resp)
                elif is_too_big(resp):
                    # mbox2nntp could not import it: skipped and counted, not written
                    articles_too_big += 1
                    logger.warning("Article %s %s: skipped", row.article_id, resp)
                else:  # "error: ..." or a status such as 480/502
                    articles_errors += 1
                    logger.warning("Article %s failed: %s", row.article_id, resp)
                return
            body = content
            if plugins:
                msg = apply_plugins(RawArticle(content), plugins)
                if msg is None:
                    articles_skipped += 1
                    return
                body = msg.to_bytes()
            try:
                written = writer.write(body, dt=row.date, message_id=row.message_id or None)
            except ArticleTooBig as e:
                # mbox2nntp could not import it: skipped and counted, like one over the fetch limit
                articles_too_big += 1
                logger.warning("Article %s: %s; skipped", row.article_id, e)
                return
            if written:
                articles_saved += 1
            else:
                articles_skipped += 1

        stop = _STOP_EVENT if _STOP_EVENT is not None else threading.Event()
        rows_to_fetch = len(rows)
        # The fetch opens `connections` connections of its own: close this
        # one meanwhile, so that no more than that are open (providers
        # limit them). The next group connects again.
        client.quit()
        _fetched_ok, _missing, _errors, elapsed = fetch_articles_parallel(
            rows=rows,
            group=group,
            conn_kwargs=conn_kwargs,
            connections=connections,
            pipeline_depth=pipeline_depth,
            on_article=on_article,
            stop_event=stop,
        )
        del _fetched_ok, _missing, _errors  # on_article counters are authoritative
        attempted = articles_saved + articles_skipped + articles_missing + articles_too_big + articles_errors
        arts_per_s = attempted / max(elapsed, 1e-6)
        kib_per_s = (writer.bytes_written / 1024.0) / max(elapsed, 1e-6)
        logger.info(
            "Group %s done: saved=%s skipped=%s missing=%s too-big=%s errors=%s "
            "(%.1fs, %.1f art/s, %.1f KiB/s, connections=%s pipeline=%s)",
            group,
            articles_saved,
            articles_skipped,
            articles_missing,
            articles_too_big,
            articles_errors,
            elapsed,
            arts_per_s,
            kib_per_s,
            connections,
            pipeline_depth,
        )
        # Only record completion after a clean, finished pull. Cancelled /
        # interrupted runs and hard fetch errors must not poison the log —
        # otherwise skip-completed (and earlier default skip) treats a failed
        # first attempt as done forever.
        if _TERMINATED or stop.is_set():
            logger.warning(
                "Pull of %s was cancelled/interrupted; not adding to %s",
                mbox_filename,
                completed_log,
            )
            raise TerminatedBySignal("terminated")
        clean = not (articles_errors > 0 or attempted < rows_to_fetch)
        if not clean:
            logger.warning(
                "Pull of %s did not finish cleanly "
                "(errors=%s attempted=%s/%s); not adding to %s",
                mbox_filename,
                articles_errors,
                attempted,
                rows_to_fetch,
                completed_log,
            )
    except BaseException:
        _ACTIVE_WRITER = None
        try:
            writer.close()
        except Exception as close_err:
            logger.error("Closing %s failed: %s", mbox_filename, close_err)
        raise
    _ACTIVE_WRITER = None
    # The articles must be on disk before the job is recorded: close() fsyncs,
    # and raises when they could not be written.
    writer.close()
    if not clean:
        return False
    _record_completed(completed_log, mbox_filename, logger, group)
    return True


def cmd_list_groups(client: NNTPClient, wildmat: Optional[str]) -> int:
    names = client.list_active(wildmat)
    for name in names:
        sys.stdout.buffer.write(name.encode("utf-8", errors="surrogateescape") + b"\n")
    return 0


def cmd_get_message(client: NNTPClient, message_id: str) -> int:
    mid = normalize_message_id(message_id)
    content, resp = client.article(mid)
    if not content:
        sys.stderr.write(f"Article not found: {resp}\n")
        return 4
    sys.stdout.buffer.write(content)
    if not content.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")
    return 0


def cmd_post(client: NNTPClient, newsgroups: List[str], plugins: list) -> int:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        sys.stderr.write("No message on stdin\n")
        return 4
    msg = RawArticle(raw)
    if "Newsgroups" not in msg and newsgroups:
        msg.add("Newsgroups", ",".join(newsgroups))
    if plugins:
        msg = apply_plugins(msg, plugins)
        if msg is None:
            sys.stderr.write("Message dropped by plugin\n")
            return 4
    payload = msg.to_bytes()
    resp = client.post(payload)
    sys.stderr.write(f"{resp}\n")
    return 0


def _connection_parser() -> argparse.ArgumentParser:
    """Shared NNTP connection / logging options for all subcommands."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "-s",
        "--server",
        help="NNTP server (host or host:port); default $NNTPSERVER or /etc/news/server",
    )
    p.add_argument(
        "-P",
        "--port",
        type=int,
        default=None,
        help="NNTP server port (default: 563 with TLS, 119 for plain NNTP)",
    )
    p.add_argument(
        "-u",
        "--user",
        "--username",
        dest="username",
        help="Username for authentication",
    )
    p.add_argument(
        "--password",
        help="Password for authentication (prefer --password-file or $NNTP_PASSWORD)",
    )
    p.add_argument(
        "--password-file",
        "--pass-file",
        dest="password_file",
        help="Path to a text file whose entire contents are the NNTP password "
        "(trailing newline optional). Prefer this over --password.",
    )
    p.add_argument(
        "--no-netrc",
        dest="netrc",
        action="store_false",
        help="Ignore credentials in ~/.netrc",
    )
    p.set_defaults(netrc=True)
    p.add_argument(
        "--no-ssl",
        dest="use_ssl",
        action="store_false",
        help="Disable TLS and use plain NNTP (typical port 119)",
    )
    p.set_defaults(use_ssl=True)
    p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    p.add_argument("--syslog", action="store_true", help="Also log to syslog (LOG_NEWS)")
    p.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=60,
        help="Timeout for one socket read (seconds)",
    )
    p.add_argument(
        "--response-timeout",
        type=float,
        default=DEFAULT_RESPONSE_TIMEOUT,
        metavar="SECONDS",
        help="Most one reply may take in all, however slowly it arrives "
        f"(default {DEFAULT_RESPONSE_TIMEOUT}); an article over "
        f"{MAX_ARTICLE >> 20} MiB is skipped and counted",
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    conn = _connection_parser()
    parser = argparse.ArgumentParser(
        prog="usenet-archiver -c",
        description="NNTP text archiver CLI (use subcommands). Without arguments, usenet-archiver launches the GUI.",
        epilog=(
            "Examples:\n"
            "  usenet-archiver -c pull -s news.example.com -u alice -g news.groups -n\n"
            "  usenet-archiver -c list-groups 'comp.*'\n"
            "  usenet-archiver -c get '<mid@example.com>'\n"
            "  usenet-archiver -c post -g misc.test < msg.eml\n"
            "\n"
            "Without any argument, usenet-archiver launches the GUI; any argument means the CLI\n"
            "(-c may be left out). Top-level --help / --version work without -c.\n"
            "Short and long forms are interchangeable (e.g. -s/--server, -u/--user)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"usenet-archiver {__version__}",
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # --- pull ------------------------------------------------------------
    pull = sub.add_parser(
        "pull",
        parents=[conn],
        help="Fetch articles into mbox (overview + ARTICLE)",
        description="Fetch newsgroup articles into mbox archives.",
    )
    pull.add_argument(
        "-g",
        "--group",
        "--newsgroup",
        action="append",
        dest="newsgroups",
        metavar="GROUP[>FILE]",
        help="Newsgroup to fetch (repeatable; optional group>filename)",
    )
    pull.add_argument(
        "-f",
        "--file",
        "--groups-file",
        dest="groups_file",
        help="File with one group[>filename] per line",
    )
    pull.add_argument(
        "--start",
        "--start-date",
        dest="start_date",
        help="Start date (YYYY-MM-DD). May be used alone; end defaults to today (UTC).",
    )
    pull.add_argument(
        "--end",
        "--end-date",
        dest="end_date",
        help="End date (YYYY-MM-DD). May be used alone: everything up to that day.",
    )
    pull.add_argument(
        "--overview-chunk",
        type=int,
        default=10000,
        help="OVER/XOVER chunk size for date scanning (default 10000)",
    )
    pull.add_argument(
        "--no-dedup",
        dest="dedup",
        action="store_false",
        help="Disable Message-ID deduplication",
    )
    pull.set_defaults(dedup=True)
    pull.add_argument(
        "--skip-completed",
        action="store_true",
        help="Skip a group whose exact mbox job is already in completed_newsgroups.log",
    )
    pull.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Plan only: overview + dedup; no ARTICLE download or mbox write",
    )
    pull.add_argument(
        "--plugin",
        action="append",
        default=[],
        metavar="PLUGIN",
        help="Apply plugin (name or name:arg:key=value); repeatable",
    )
    pull.add_argument(
        "-C",
        "--connections",
        type=int,
        default=8,
        metavar="N",
        help="Parallel NNTP connections for ARTICLE fetch (default 8)",
    )
    pull.add_argument(
        "-D",
        "--depth",
        "--pipeline-depth",
        dest="pipeline_depth",
        type=int,
        default=32,
        metavar="N",
        help="ARTICLE commands to pipeline per connection (default 32)",
    )

    # --- list-groups -----------------------------------------------------
    lg = sub.add_parser(
        "list-groups",
        parents=[conn],
        help="List available newsgroups",
        description="List newsgroups from the server (optional wildmat).",
    )
    lg.add_argument(
        "wildmat",
        nargs="?",
        default=None,
        help="Optional wildmat pattern (e.g. 'comp.lang.*')",
    )

    # --- get -------------------------------------------------------------
    getp = sub.add_parser(
        "get",
        parents=[conn],
        help="Fetch one article by Message-ID to stdout",
        description="Fetch a single article by Message-ID and write it to stdout.",
    )
    getp.add_argument(
        "message_id",
        help="Message-ID (with or without surrounding <angle brackets>)",
    )

    # --- post ------------------------------------------------------------
    post = sub.add_parser(
        "post",
        parents=[conn],
        help="Post one RFC 5322 message from stdin",
        description="Read one message from stdin and POST it.",
    )
    post.add_argument(
        "-g",
        "--group",
        "--newsgroup",
        action="append",
        dest="newsgroups",
        metavar="GROUP",
        help="Newsgroup(s) if the message has no Newsgroups header",
    )
    post.add_argument(
        "--plugin",
        action="append",
        default=[],
        metavar="PLUGIN",
        help="Apply plugin (name or name:arg:key=value); repeatable",
    )

    return parser


def _resolve_endpoint(args):
    return resolve_credentials(
        server=args.server,
        port=args.port,
        username=args.username,
        password=args.password,
        password_file=args.password_file,
        use_netrc=args.netrc,
        use_ssl=args.use_ssl,
    )


def _connect_client(args, endpoint) -> NNTPClient:
    client = NNTPClient(
        server=endpoint.host,
        port=endpoint.port,
        username=endpoint.username,
        password=endpoint.password,
        use_ssl=args.use_ssl,
        verbose=args.verbose,
        timeout=args.timeout,
        response_timeout=args.response_timeout,
    )
    client.connect()
    return client


def _parse_plugins(specs: List[str], parser: argparse.ArgumentParser) -> list:
    plugins: List[Callable] = []
    for spec in specs or []:
        try:
            plugins.append(parse_plugin_spec(spec))
        except ValueError as e:
            parser.error(str(e))
    return plugins


def _pull_arguments(args, parser: argparse.ArgumentParser, logger: logging.Logger):
    """Check pull's arguments (parser.error, exit 2, on a bad one); returns
    (start_date, end_date, plugins, group_specs)."""
    if args.connections < 1:
        parser.error("--connections must be >= 1")
    if args.pipeline_depth < 1:
        parser.error("--pipeline-depth must be >= 1")
    if args.overview_chunk < 1:
        parser.error("--overview-chunk must be >= 1")
    if args.connections > 99:
        sys.stderr.write(
            f"Warning: --connections {args.connections} is high; "
            "check your provider's concurrent-connection limit.\n"
        )

    try:
        start_date = parse_date(args.start_date) if args.start_date else None
    except ValueError as e:
        parser.error(f"--start {args.start_date}: {e}")
    try:
        end_date = parse_date(args.end_date) if args.end_date else None
    except ValueError as e:
        parser.error(f"--end {args.end_date}: {e}")
    try:
        start_date, end_date = normalize_date_range(start_date, end_date, verbose=args.verbose)
    except ValueError as e:
        parser.error(str(e).replace("start date", "--start-date").replace("end date", "--end-date"))

    plugins = _parse_plugins(args.plugin, parser)
    if args.dedup:
        for spec, plugin in zip(args.plugin, plugins):
            if removes_message_id(plugin):
                parser.error(
                    f"--plugin {spec} removes Message-ID, by which the archive recognises "
                    "an article: different articles would be skipped as duplicates. "
                    "Keep Message-ID, or pull with --no-dedup"
                )
    try:
        group_specs = collect_group_specs(args.newsgroups, args.groups_file)
    except (OSError, UnicodeDecodeError) as e:
        parser.error(f"cannot read the groups file {args.groups_file}: {e}")
    group_specs = filter_binary_groups(group_specs, logger=logger)
    if not group_specs:
        parser.error(
            "pull requires -g/--newsgroup and/or -f/--groups-file "
            "(after skipping binary groups)"
        )
    return start_date, end_date, plugins, group_specs


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return _main(argv)
    except TerminatedBySignal:  # wherever it came: connecting, resolving, a group
        sys.stderr.write("Terminated by signal\n")
        return 4


def _main(argv: Optional[List[str]]) -> int:
    global _STOP_EVENT, _TERMINATED

    parser = build_parser()
    args = parser.parse_args(argv)

    _STOP_EVENT = threading.Event()
    _TERMINATED = False
    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        endpoint = _resolve_endpoint(args)
    except SystemExit as e:
        if e.code == 2:
            sys.stderr.write(
                "No NNTP server specified. Use --server / -s, set $NNTPSERVER, "
                "or create /etc/news/server.\n"
            )
            return 2
        raise
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return 2
    except FileNotFoundError as e:
        sys.stderr.write(f"Password file error: {e}\n")
        return 2
    except OSError as e:
        sys.stderr.write(f"Connection/config error: {e}\n")
        return 3

    logger = setup_logging(args.verbose, args.syslog)

    # Every argument is checked before connecting: a usage error is exit 2,
    # without a login on the server.
    if args.command == "get":
        try:
            message_id = normalize_message_id(args.message_id)
        except ValueError as e:
            parser.error(str(e))
    elif args.command == "post":
        plugins = _parse_plugins(args.plugin, parser)
        groups = [g for g, _ in collect_group_specs(args.newsgroups, None)]
    elif args.command == "pull":
        start_date, end_date, plugins, group_specs = _pull_arguments(args, parser, logger)

    try:
        client = _connect_client(args, endpoint)
    except (NNTPError, OSError) as e:
        sys.stderr.write(f"Connection failed: {e}\n")
        return 3

    try:
        if args.command == "list-groups":
            return cmd_list_groups(client, args.wildmat)

        if args.command == "get":
            return cmd_get_message(client, message_id)

        if args.command == "post":
            return cmd_post(client, groups, plugins)

        # pull
        completed_log = "completed_newsgroups.log"
        incomplete = 0  # groups that failed or did not get every article
        from .policy import group_name_problem

        for group, filename in group_specs:
            file_handler = None
            pulling = False
            conn_kwargs = {
                "server": endpoint.host,
                "port": endpoint.port,
                "username": endpoint.username,
                "password": endpoint.password,
                "use_ssl": args.use_ssl,
                "verbose": args.verbose,
                "timeout": args.timeout,
                "response_timeout": args.response_timeout,
            }
            try:
                if _TERMINATED:  # a SIGTERM no handler raised on the way
                    raise TerminatedBySignal("terminated")
                # the name goes into commands and file names: check it first
                problem = group_name_problem(group)
                if problem:
                    raise ValueError(f"invalid newsgroup name {group!r}: {problem}")
                mbox_filename = mbox_name_for(group, filename, start_date, end_date)
                log_filename = os.path.splitext(mbox_filename)[0] + ".log"
                file_handler = logging.FileHandler(log_filename)
                file_handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
                )
                logging.getLogger().addHandler(file_handler)
                pulling = True
                done = pull_group(
                    client=client,
                    group=group,
                    mbox_filename=mbox_filename,
                    start_date=start_date,
                    end_date=end_date,
                    overview_chunk=args.overview_chunk,
                    dedup=args.dedup,
                    plugins=plugins,
                    completed_log=completed_log,
                    logger=logger,
                    conn_kwargs=conn_kwargs,
                    connections=args.connections,
                    pipeline_depth=args.pipeline_depth,
                    skip_completed=args.skip_completed,
                    dry_run=args.dry_run,
                )
                if not done:
                    incomplete += 1
            except TerminatedBySignal:
                logger.warning("Terminated while processing %s", group)
                return 4
            except Exception as e:
                logger.error("Failed group %s: %s", group, e)
                incomplete += 1
                # A failure in the pull may have left the connection out of
                # step: the next group gets a new one.
                if pulling:
                    try:
                        client.reconnect()
                    except (NNTPError, OSError) as re_err:
                        logger.error("Reconnect after %s failed: %s", group, re_err)
            finally:
                if file_handler is not None:
                    logging.getLogger().removeHandler(file_handler)
                    file_handler.close()
        if incomplete:
            logger.error("%s of %s group(s) failed or are incomplete", incomplete, len(group_specs))
            return 1
        return 0
    except TerminatedBySignal:
        return 4
    except (NNTPError, NNTPTimeout, OSError) as e:  # a timeout or a reset too
        sys.stderr.write(f"NNTP error: {e}\n")
        return 4
    finally:
        try:
            client.quit()
        except Exception:
            client.close()


def run() -> None:
    """Zipapp / console_scripts entry point (honors exit codes)."""
    raise SystemExit(main())


if __name__ == "__main__":
    run()
