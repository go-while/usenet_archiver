#!/usr/bin/env python3
"""A Date at the edge of datetime's range must not stop the date-window scan.

Review 2, C1: "Fri, 31 Dec 9999 23:00:00 -0200" is 10000-01-01 in UTC, past
datetime's range, and the conversion raised OverflowError. With --start/--end
the group then failed on every run.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))

from usenet_archiver.overview import OverviewRow, find_articles_in_date_range  # noqa: E402


class RawOverClient:
    """Answers OVER with the given raw lines."""

    def __init__(self, lines):
        self.lines = lines

    def over(self, a, b):
        return [l for l in self.lines if a <= int(l.split(b"\t")[0]) <= b], "OVER", "224"


class TestDateOverflow(unittest.TestCase):
    def test_year_9999_with_negative_zone(self):
        lines = [
            b"1\ts\tf\tWed, 15 Jan 2020 12:00:00 +0000\t<1@x>\t\t1\t1",
            b"2\ts\tf\tFri, 31 Dec 9999 23:00:00 -0200\t<2@x>\t\t1\t1",
            b"3\ts\tf\tThu, 16 Jan 2020 12:00:00 +0000\t<3@x>\t\t1\t1",
        ]
        rows = find_articles_in_date_range(
            RawOverClient(lines), 1, 3, datetime(2020, 1, 1), datetime(2020, 12, 31), chunk_size=10
        )
        self.assertEqual([r.article_id for r in rows], [1, 3])

    def test_year_1_with_positive_zone(self):
        early = datetime(1, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=2)))
        from usenet_archiver import overview

        rows = [OverviewRow(1, date=early), OverviewRow(2, date=datetime(2020, 1, 15, tzinfo=timezone.utc))]
        got = overview._scan_chunks(lambda a, b: rows, 1, 2, 10, datetime(2020, 1, 1).date(), None)
        self.assertEqual([r.article_id for r in got], [2])


if __name__ == "__main__":
    unittest.main()
