#!/usr/bin/env python3
"""Decompress .gz Usenet article dumps into one mbox file.

Each .gz holds either an rnews batch ("#! rnews N" lines, each followed by an
article of exactly N bytes) or one article. Concatenated articles without
that framing can be split with --split-on-path, at every line that starts
"Path: " (a heuristic: a body line starting "Path: " splits its article).

The output is written with the archiver's mboxrd writer
(usenet_archiver.mboxout.MboxWriter): appended to, never overwritten; an
article already in it (same Message-ID) is not written again; crash safe.
Articles over 1 MiB are skipped and counted, as mbox2nntp cannot import them.

The package must be importable: run this from the repository, or with the
package on PYTHONPATH.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, List

# Use the archiver's writer; a separate copy here used to drift from it.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from usenet_archiver.mboxout import ArticleTooBig, MboxLocked, MboxWriter
    from usenet_archiver.nntp import MAX_ARTICLE
except ImportError as exc:  # pragma: no cover - depends on how it is run
    sys.exit(f"gz2mbox: cannot import usenet_archiver ({exc}); run it from the repository")

RNEWS_RE = re.compile(rb"#! rnews (\d+)\r?\n")
PATH_SPLIT_RE = re.compile(rb"\n(?=Path: )")


class BatchError(Exception):
    """A .gz whose contents cannot be split into articles safely."""


@dataclass
class Stats:
    too_big: int = 0


def _skip(fh: BinaryIO, n: int) -> int:
    """Read and drop n bytes; returns how many there were."""
    got = 0
    while got < n:
        chunk = fh.read(min(1 << 20, n - got))
        if not chunk:
            break
        got += len(chunk)
    return got


def _rnews(fh: BinaryIO, line: bytes, stats: Stats) -> Iterator[bytes]:
    while line:
        if line in (b"\n", b"\r\n"):  # tolerated between articles
            line = fh.readline(200)
            continue
        m = RNEWS_RE.fullmatch(line)
        if not m:
            raise BatchError(f"expected '#! rnews N', got {line[:60]!r}")
        n = int(m.group(1))
        if n > MAX_ARTICLE:
            if _skip(fh, n) < n:
                raise BatchError("the batch ends inside an article")
            stats.too_big += 1
        else:
            article = fh.read(n)
            if len(article) < n:
                raise BatchError("the batch ends inside an article")
            yield article
        line = fh.readline(200)


def articles_from(fh: BinaryIO, split_on_path: bool = False, stats: Stats = None, name: str = "") -> Iterator[bytes]:
    """The articles in one decompressed dump (see the module docstring).

    Raises BatchError for a malformed rnews batch or a file that is already
    an mbox; articles yielded before that are whole.
    """
    stats = stats if stats is not None else Stats()
    first = fh.readline(200)
    if first.startswith(b"#! rnews "):
        yield from _rnews(fh, first, stats)
        return
    if first.startswith(b"From "):
        raise BatchError("already an mbox (starts with 'From '); not converted")
    if split_on_path:
        parts = PATH_SPLIT_RE.split(first + fh.read())
        for part in parts:
            if not part.strip():
                continue
            if len(part) > MAX_ARTICLE:
                stats.too_big += 1
                continue
            yield part
        return
    data = first + fh.read(MAX_ARTICLE + 1 - len(first))
    if len(data) > MAX_ARTICLE:
        _skip(fh, 1 << 62)
        stats.too_big += 1
        return
    if data.count(b"\nPath: ") + data.startswith(b"Path: ") > 1:
        print(
            f"Warning: {name}: kept as one article, but it has several lines starting 'Path: '; "
            "if it holds several articles, see --split-on-path",
            file=sys.stderr,
        )
    if data.strip():
        yield data


def find_gz_files(root: Path) -> List[Path]:
    return sorted(path for path in root.rglob("*.gz") if path.is_file())


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="Decompress .gz Usenet article dumps into one mbox file.")
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to search for .gz files (default: current directory)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="archive.mbox",
        help="Output mbox (default: archive.mbox); appended to, an article already in it is not written again",
    )
    parser.add_argument(
        "--split-on-path",
        action="store_true",
        help="Split a dump that is not an rnews batch at every line starting 'Path: ' "
        "(for concatenated articles; a body line starting 'Path: ' splits its article)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Print every file processed")
    args = parser.parse_args(argv)

    search_root = Path(args.directory).resolve()
    if not search_root.is_dir():
        print(f"Error: {search_root} is not a directory", file=sys.stderr)
        return 1
    gz_files = find_gz_files(search_root)
    if not gz_files:
        print(f"Error: no .gz files found under {search_root}", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    files_ok = files_failed = 0
    stats = Stats()
    writer = MboxWriter(str(output_path))
    try:
        writer.open()
    except MboxLocked:
        print(f"Error: {output_path} is being written by another process", file=sys.stderr)
        return 1
    try:
        for gz_path in gz_files:
            rel = gz_path.relative_to(search_root)
            n = 0
            try:
                with gzip.open(gz_path, "rb") as fh:
                    for article in articles_from(fh, args.split_on_path, stats, str(rel)):
                        try:
                            writer.write(article)
                        except ArticleTooBig:  # its quoted body would be over mbox2nntp's limit
                            stats.too_big += 1
                            continue
                        n += 1
                files_ok += 1
            except (OSError, EOFError, zlib.error, BatchError) as exc:  # zlib.error: a corrupt deflate stream
                files_failed += 1
                print(f"Warning: {rel}: {exc} ({n} whole articles taken before that)", file=sys.stderr)
                continue
            if args.verbose:
                print(f"Processing: {rel} ({n} articles)", file=sys.stderr)
    finally:
        writer.close()

    size = output_path.stat().st_size if output_path.exists() else 0
    print(
        f"Done: {files_ok} .gz files, {writer.written} articles written, "
        f"{writer.skipped_dup} already in the mbox, {stats.too_big} over {MAX_ARTICLE >> 20} MiB skipped; "
        f"{output_path} is {size} bytes",
        file=sys.stderr,
    )
    if files_failed:
        print(f"Warning: {files_failed} .gz files failed (see above)", file=sys.stderr)
        return 1
    if writer.written == 0 and writer.skipped_dup == 0:
        print("Error: no articles found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
