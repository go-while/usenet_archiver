#!/usr/bin/env python3
"""Decompress .gz archives containing Usenet articles into a single mbox file.

Uses the archiver's mboxrd writer (usenet_archiver.mboxout), so the output is
what mbox2nntp expects. The package must be importable: run this from the
repository, or with the package on PYTHONPATH.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from pathlib import Path
from typing import List, Optional

ARTICLE_SPLIT_RE = re.compile(rb"\n(?=Path: )")

# Use the archiver's writer; a separate copy here used to drift from it.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from usenet_archiver.mboxout import write_mbox_message
except ImportError as exc:  # pragma: no cover - depends on how it is run
    sys.exit(f"gz2mbox: cannot import usenet_archiver.mboxout ({exc}); run it from the repository")


def split_articles(data: bytes) -> List[bytes]:
    parts = ARTICLE_SPLIT_RE.split(data)
    if len(parts) > 1:
        return [part for part in parts if part.strip()]
    return [data]


def find_gz_files(root: Path) -> List[Path]:
    return sorted(path for path in root.rglob("*.gz") if path.is_file())


def decompress_gz(path: Path) -> Optional[bytes]:
    try:
        with gzip.open(path, "rb") as handle:
            return handle.read()
    except (OSError, gzip.BadGzipFile) as exc:
        print(f"Warning: skipping {path}: {exc}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decompress .gz Usenet archives into a single mbox file."
    )
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
        help="Output mbox path (default: archive.mbox in current directory)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print every file processed",
    )
    args = parser.parse_args()

    search_root = Path(args.directory).resolve()
    if not search_root.is_dir():
        print(f"Error: {search_root} is not a directory", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path

    gz_files = find_gz_files(search_root)
    if not gz_files:
        print(f"Error: no .gz files found under {search_root}", file=sys.stderr)
        return 1

    files_processed = 0
    files_failed = 0
    articles_written = 0

    with output_path.open("wb") as mbox_handle:
        for gz_path in gz_files:
            data = decompress_gz(gz_path)
            if data is None:
                files_failed += 1
                continue

            chunks = split_articles(data)
            for chunk in chunks:
                write_mbox_message(mbox_handle, chunk)
                articles_written += 1

            files_processed += 1
            if args.verbose:
                rel_path = gz_path.relative_to(search_root)
                print(
                    f"Processing: {rel_path} ({len(chunks)} articles)",
                    file=sys.stderr,
                )

    if articles_written == 0:
        print("Error: no articles written", file=sys.stderr)
        return 1

    output_size = output_path.stat().st_size if output_path.exists() else 0
    print(
        f"Done: {files_processed} .gz files processed, "
        f"{articles_written} articles written to {output_path} "
        f"({output_size} bytes)",
        file=sys.stderr,
    )
    if files_failed:
        print(f"Warning: {files_failed} .gz files skipped due to errors", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
