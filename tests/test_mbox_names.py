#!/usr/bin/env python3
"""Tests for mbox naming and date-range defaults."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))

from usenet_archiver.cli import mbox_name_for, normalize_date_range  # noqa: E402


class TestNormalizeDateRange(unittest.TestCase):
    def test_both_none(self):
        s, e = normalize_date_range(None, None)
        self.assertIsNone(s)
        self.assertIsNone(e)

    def test_start_only_defaults_end_today(self):
        start = datetime(2025, 7, 1)
        s, e = normalize_date_range(start, None, today=date(2025, 7, 10))
        self.assertEqual(s, start)
        self.assertEqual(e, datetime(2025, 7, 10))

    def test_open_ended_differs_by_calendar_day(self):
        """'Since July 1' on July 10 vs July 11 are different windows."""
        start = datetime(2025, 7, 1)
        _, end_10 = normalize_date_range(start, None, today=date(2025, 7, 10))
        _, end_11 = normalize_date_range(start, None, today=date(2025, 7, 11))
        self.assertEqual(end_10.date(), date(2025, 7, 10))
        self.assertEqual(end_11.date(), date(2025, 7, 11))

        g = "alt.fan.usenet"
        name_10 = mbox_name_for(g, None, start, end_10)
        name_11 = mbox_name_for(g, None, start, end_11)
        self.assertEqual(name_10, "alt.fan.usenet-20250701-20250710.mbox")
        self.assertEqual(name_11, "alt.fan.usenet-20250701-20250711.mbox")
        self.assertNotEqual(name_10, name_11)

    @unittest.expectedFailure
    def test_end_only_has_no_lower_bound(self):
        # review 2, A7: a lone end used to start the window at 1990-01-01,
        # which left out older articles and refused an end before 1990
        end = datetime(2022, 6, 1)
        self.assertEqual(normalize_date_range(None, end), (None, end))
        self.assertEqual(normalize_date_range(None, datetime(1985, 12, 31)), (None, datetime(1985, 12, 31)))

    def test_start_after_end_raises(self):
        with self.assertRaises(ValueError):
            normalize_date_range(datetime(2022, 1, 1), datetime(2021, 1, 1))


class TestMboxName(unittest.TestCase):
    @unittest.expectedFailure
    def test_end_only_name(self):
        import tempfile

        g = "alt.fan.usenet"
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertEqual(mbox_name_for(g, None, None, datetime(2022, 1, 1)),
                                 "alt.fan.usenet-until-20220101.mbox")
                # the mbox an earlier version wrote for the same request is used on
                open("alt.fan.usenet-19900101-20220101.mbox", "wb").close()
                self.assertEqual(mbox_name_for(g, None, None, datetime(2022, 1, 1)),
                                 "alt.fan.usenet-19900101-20220101.mbox")
            finally:
                os.chdir(cwd)

    def test_dated_windows_are_distinct(self):
        g = "alt.fan.usenet"
        a = mbox_name_for(g, None, datetime(2021, 1, 1), datetime(2022, 1, 1))
        b = mbox_name_for(g, None, datetime(2022, 1, 1), datetime(2023, 1, 1))
        self.assertEqual(a, "alt.fan.usenet-20210101-20220101.mbox")
        self.assertEqual(b, "alt.fan.usenet-20220101-20230101.mbox")
        self.assertNotEqual(a, b)

    def test_undated_is_plain_group_name(self):
        self.assertEqual(
            mbox_name_for("alt.fan.usenet", None, None, None),
            "alt.fan.usenet.mbox",
        )

    def test_explicit_filename_wins(self):
        self.assertEqual(
            mbox_name_for("g", "custom.mbox", datetime(2021, 1, 1), datetime(2022, 1, 1)),
            "custom.mbox",
        )


class TestEndOnlyPull(unittest.TestCase):
    """--end alone pulls everything up to that day, also before 1990."""

    def _pull(self, *args):
        import io
        import tempfile
        from contextlib import redirect_stderr

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_nntp import FakeNNTPServer
        from usenet_archiver import cli

        dates = {1: "Mon, 2 Mar 1987 10:00:00 -0500", 2: "Fri, 15 Aug 1980 08:00:00 +0000",
                 3: "Wed, 15 Jan 2020 12:00:00 +0000"}
        arts = {n: (b"From: a@example.com\nNewsgroups: misc.test\nSubject: s\nDate: %s\n"
                    b"Message-ID: <id%d@ex.com>\n\nbody %d\n" % (d.encode(), n, n)) for n, d in dates.items()}
        rows = {n: (b"%d\ts\ta@x\t%s\t<id%d@ex.com>\t\t10\t1" % (n, d.encode(), n)) for n, d in dates.items()}
        srv = FakeNNTPServer(articles=arts, over_rows=rows)
        srv.start()
        self.addCleanup(srv.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)
        try:
            with redirect_stderr(io.StringIO()):
                try:
                    rc = cli.main(["pull", "-s", f"127.0.0.1:{srv.port}", "--no-ssl", "--no-netrc",
                                   "-C", "1", "-g", "misc.test", *args])
                except SystemExit as e:
                    rc = e.code
        finally:
            os.chdir(cwd)
        data = b""
        for name in os.listdir(tmp.name):
            if name.endswith(".mbox"):
                with open(os.path.join(tmp.name, name), "rb") as fh:
                    data += fh.read()
        return rc, sorted(int(x) for x in __import__("re").findall(rb"\nbody (\d)\n", data))

    @unittest.expectedFailure
    def test_before_1990_kept(self):
        self.assertEqual(self._pull("--end", "2000-01-01"), (0, [1, 2]))

    @unittest.expectedFailure
    def test_end_before_1990(self):
        self.assertEqual(self._pull("--end", "1985-12-31"), (0, [2]))


if __name__ == "__main__":
    unittest.main()
