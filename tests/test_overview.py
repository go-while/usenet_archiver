#!/usr/bin/env python3
"""Tests for overview parsing and the date-window scan."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))

from usenet_archiver.overview import (  # noqa: E402
    OverviewRow,
    filter_rows_by_dedup,
    find_articles_in_date_range,
    parse_overview_line,
)


def _row(aid: int, year: int, month: int = 1, day: int = 15, mid: str = "") -> OverviewRow:
    return OverviewRow(
        article_id=aid,
        date=datetime(year, month, day, tzinfo=timezone.utc),
        message_id=mid or f"<art{aid}@example.com>",
    )


class FakeOverviewStore:
    """In-memory overview for testing bisect/scan without a network."""

    def __init__(self, rows):
        self.by_id = {r.article_id: r for r in rows}
        self.calls = []

    def fetch(self, start_id, end_id):
        self.calls.append((start_id, end_id))
        return [self.by_id[i] for i in range(start_id, end_id + 1) if i in self.by_id]


class TestParseOverview(unittest.TestCase):
    def test_parse_standard_line(self):
        line = (
            b"42\tHello\tuser@ex.com\t"
            b"Mon, 01 Jan 2020 12:00:00 +0000\t"
            b"<id42@ex.com>\t\t100\t5"
        )
        row = parse_overview_line(line)
        self.assertIsNotNone(row)
        self.assertEqual(row.article_id, 42)
        self.assertEqual(row.subject, "Hello")
        self.assertEqual(row.message_id, "<id42@ex.com>")
        self.assertEqual(row.date.year, 2020)

    def test_parse_invalid(self):
        self.assertIsNone(parse_overview_line(b"not-a-number\tfoo"))


class FakeOverClient:
    """Answers OVER from a dict of number -> date (None = no Date)."""

    def __init__(self, dates):
        self.dates = dates
        self.calls = []

    def over(self, a, b):
        self.calls.append((a, b))
        lines = []
        for n in range(a, b + 1):
            if n in self.dates:
                d = self.dates[n]
                ds = d.strftime("%a, %d %b %Y %H:%M:%S +0000") if d else ""
                lines.append(f"{n}\ts\tf\t{ds}\t<{n}@x>\t\t1\t1".encode())
        return lines, "OVER", "224" if lines else "423"


class TestDateWindow(unittest.TestCase):
    def test_gap_in_numbers_hides_nothing(self):
        # 1-1000 and 1601-2600, all on the window's day: the bisection used
        # to land in the gap and return none of them
        day = datetime(2016, 12, 8, 12, tzinfo=timezone.utc)
        dates = {n: day for n in list(range(1, 1001)) + list(range(1601, 2601))}
        rows = find_articles_in_date_range(FakeOverClient(dates), 1, 2600,
                                           datetime(2016, 12, 8), datetime(2016, 12, 8), chunk_size=500)
        self.assertEqual(len(rows), 2000)

    def test_window_edges_inclusive_and_undated_kept(self):
        dates = {
            1: datetime(2016, 12, 7, 23, 59, tzinfo=timezone.utc),  # before
            2: datetime(2016, 12, 8, 0, 0, tzinfo=timezone.utc),    # first second of the window
            3: datetime(2016, 12, 9, 23, 59, tzinfo=timezone.utc),  # last minute of the window
            4: datetime(2016, 12, 10, 0, 0, tzinfo=timezone.utc),   # after
            5: None,                                                # no Date: kept
        }
        rows = find_articles_in_date_range(FakeOverClient(dates), 1, 5,
                                           datetime(2016, 12, 8), datetime(2016, 12, 9), chunk_size=2)
        self.assertEqual([r.article_id for r in rows], [2, 3, 5])

    def test_out_of_order_dates_found(self):
        dates = {n: datetime(2020, 1, 1, tzinfo=timezone.utc) for n in range(1, 101)}
        dates[7] = datetime(2016, 12, 8, tzinfo=timezone.utc)   # far from its neighbours
        dates[93] = datetime(2016, 12, 8, tzinfo=timezone.utc)
        rows = find_articles_in_date_range(FakeOverClient(dates), 1, 100,
                                           datetime(2016, 12, 8), datetime(2016, 12, 8), chunk_size=10)
        self.assertEqual([r.article_id for r in rows], [7, 93])


class TestNonMonotonic(unittest.TestCase):
    def test_out_of_order_date_still_found_by_scan(self):
        from usenet_archiver.overview import _scan_chunks

        rows = [
            _row(1, 2020),
            _row(2, 2021),
            _row(3, 2019),  # backdated
            _row(4, 2022),
        ]
        store = FakeOverviewStore(rows)
        start_d = datetime(2020, 1, 1).date()
        end_d = datetime(2021, 12, 31).date()
        matched = _scan_chunks(store.fetch, 1, 4, 10, start_d, end_d)
        ids = {r.article_id for r in matched}
        self.assertIn(1, ids)
        self.assertIn(2, ids)
        # 2019 article filtered out; 2022 filtered out
        self.assertNotIn(3, ids)
        self.assertNotIn(4, ids)


class TestDedupFilter(unittest.TestCase):
    def test_filter(self):
        rows = [_row(1, 2020, mid="<a@x>"), _row(2, 2020, mid="<b@x>")]
        out = filter_rows_by_dedup(rows, {"<a@x>"})
        self.assertEqual([r.article_id for r in out], [2])


if __name__ == "__main__":
    unittest.main()
