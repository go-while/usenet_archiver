#!/usr/bin/env python3
"""Retries for answers that do not mean what they seem to.

- 423/430 for an article the overview listed: a frontend of a server farm
  that lags; asked once more on a new connection.
- 503, or 501 from a command that works, for an overview range: the range is
  too wide; asked again with half the range.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from datetime import datetime, timezone

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))
sys.path.insert(0, os.path.dirname(__file__))

from test_nntp import FakeNNTPServer  # noqa: E402

from usenet_archiver.fetch import fetch_articles_parallel  # noqa: E402
from usenet_archiver.nntp import NNTPClient, NNTPError  # noqa: E402
from usenet_archiver.overview import OverviewRow, find_articles_in_date_range  # noqa: E402


def _arts(n):
    return {i: b"Message-ID: <id%d@ex.com>\nSubject: s%d\n\nbody %d\n" % (i, i, i) for i in range(1, n + 1)}


class Scripted(FakeNNTPServer):
    """FakeNNTPServer with a log of command lines and scripted answers.

    max_range: OVER/XOVER over more numbers get too_wide.
    no_over: OVER and XOVER answer no_over_reply, single numbers too; XHDR
    DATE answers.
    absent: article number -> how many more ARTICLE asks get 423.
    down_from: connections from this one on (1 = the first) get 400 at GROUP.
    """

    def __init__(self, articles, max_range=0, too_wide=b"503 range too wide", no_over=False,
                 no_over_reply=b"501 syntax error", absent=None, down_from=0):
        super().__init__(articles=articles)
        self.max_range = max_range
        self.too_wide = too_wide
        self.no_over = no_over
        self.no_over_reply = no_over_reply
        self.absent = dict(absent or {})
        self.down_from = down_from
        self.log = []
        self.conns = 0
        self._lock = threading.Lock()
        self._conn_no = {}

    def _handle(self, conn):
        with self._lock:
            self.conns += 1
            self._conn_no[id(conn)] = self.conns
        super()._handle(conn)

    def count(self, prefix):
        with self._lock:
            return sum(1 for line in self.log if line.startswith(prefix))

    def exact(self, line):
        with self._lock:
            return self.log.count(line)

    def hook(self, conn, upper):
        with self._lock:
            self.log.append(upper)
            conn_no = self._conn_no.get(id(conn), 0)
        if upper.startswith("GROUP") and self.down_from and conn_no >= self.down_from:
            self._send(conn, b"400 service unavailable\r\n")
            return True
        if upper.startswith(("OVER", "XOVER")):
            parts = upper.split()
            rng = parts[1] if len(parts) > 1 else ""
            a, _, b = rng.partition("-")
            span = (int(b) - int(a) + 1) if b else 1
            if self.no_over:
                self._send(conn, self.no_over_reply + b"\r\n")
                return True
            if self.max_range and span > self.max_range:
                self._send(conn, self.too_wide + b"\r\n")
                return True
            return False
        if upper.startswith("XHDR DATE"):
            a, _, b = upper.split()[2].partition("-")
            self._send(conn, b"221 Date follows\r\n")
            for n in range(int(a), int(b or a) + 1):
                if n in self.articles:
                    self._send(conn, b"%d Wed, 15 Jan 2020 12:00:00 +0000\r\n" % n)
            self._send(conn, b".\r\n")
            return True
        if upper.startswith("ARTICLE"):
            spec = upper.split()[1]
            if spec.isdigit():
                n = int(spec)
                with self._lock:
                    left = self.absent.get(n, 0)
                    if left:
                        self.absent[n] = left - 1 if left > 0 else left
                if left:
                    self._send(conn, b"423 no article with that number\r\n")
                    return True
        return False


def _client(srv):
    c = NNTPClient("127.0.0.1", srv.port, use_ssl=False, timeout=5)
    c.connect()
    c.group("misc.test")
    return c


class TestOverviewRange(unittest.TestCase):
    def _scan(self, srv, first, last, chunk):
        srv.start()
        self.addCleanup(srv.stop)
        client = _client(srv)
        self.addCleanup(client.close)
        return find_articles_in_date_range(client, first, last, None, None, chunk_size=chunk)

    def test_503_for_a_wide_range_halves_it(self):
        srv = Scripted(_arts(1000), max_range=100)
        rows = self._scan(srv, 1, 1000, 1000)
        self.assertEqual([r.article_id for r in rows], list(range(1, 1001)))
        self.assertTrue(all(r.message_id for r in rows))
        # 1000 -> 500 -> 250 -> 125 -> 62, then 62 at a time: about 20 OVERs, not one per number
        self.assertLess(srv.count("OVER"), 30)
        self.assertEqual(srv.count("XHDR"), 0)

    def test_501_from_a_working_over_halves_the_range(self):
        srv = Scripted(_arts(300), max_range=100, too_wide=b"501 range too large")
        rows = self._scan(srv, 1, 300, 1000)
        self.assertEqual([r.article_id for r in rows], list(range(1, 301)))
        # listed by OVER (with Message-IDs), not by XHDR DATE
        self.assertTrue(all(r.message_id for r in rows))
        self.assertEqual(srv.count("XHDR"), 0)

    def test_server_without_over_still_falls_back_to_xhdr(self):
        srv = Scripted(_arts(30), no_over=True)
        rows = self._scan(srv, 1, 30, 10)
        self.assertEqual([r.article_id for r in rows], list(range(1, 31)))
        self.assertEqual(srv.count("XHDR DATE"), 3)
        # probed once per command for the whole scan, not once per chunk
        self.assertEqual(srv.exact("OVER 1"), 1)
        self.assertEqual(srv.exact("XOVER 1"), 1)

    def test_server_without_over_answering_500(self):
        srv = Scripted(_arts(30), no_over=True, no_over_reply=b"500 unknown command")
        rows = self._scan(srv, 1, 30, 10)
        self.assertEqual(len(rows), 30)
        # 500 never means a too-wide range: no probe at all
        self.assertEqual(srv.exact("OVER 1") + srv.exact("XOVER 1"), 0)

    def test_503_even_for_one_number_fails(self):
        srv = Scripted(_arts(10), max_range=0, no_over=True, no_over_reply=b"503 overview unavailable")
        with self.assertRaises(NNTPError):
            self._scan(srv, 1, 10, 10)
        # 10 -> 5 -> 2 -> 1, then the single-number chunk is retried on new connections and fails
        self.assertLess(srv.count("OVER"), 15)


class TestAskAgain(unittest.TestCase):
    def _fetch(self, srv, n, connections=2):
        srv.start()
        self.addCleanup(srv.stop)
        rows = [
            OverviewRow(article_id=i, date=datetime(2020, 1, 15, tzinfo=timezone.utc), message_id=f"<id{i}@ex.com>")
            for i in range(1, n + 1)
        ]
        got = {}

        def on_article(row, content, resp):
            self.assertNotIn(row.article_id, got, "reported twice")
            got[row.article_id] = (content, resp)

        counts = fetch_articles_parallel(
            rows=rows,
            group="misc.test",
            conn_kwargs={"server": "127.0.0.1", "port": srv.port, "use_ssl": False, "timeout": 5},
            connections=connections,
            pipeline_depth=4,
            on_article=on_article,
        )
        return counts[:3], got

    def test_423_from_a_lagging_frontend_is_asked_again(self):
        srv = Scripted(_arts(8), absent={3: 1, 6: 1})
        (ok, missing, errors), got = self._fetch(srv, 8)
        self.assertEqual((ok, missing, errors), (8, 0, 0))
        self.assertEqual(sorted(got), list(range(1, 9)))
        self.assertTrue(all(c for c, _ in got.values()))
        self.assertEqual(srv.exact("ARTICLE 3"), 2)
        self.assertEqual(srv.exact("ARTICLE 1"), 1)

    def test_an_article_that_is_not_there_is_asked_twice_and_missing(self):
        srv = Scripted(_arts(5), absent={4: -1})
        (ok, missing, errors), got = self._fetch(srv, 5)
        self.assertEqual((ok, missing, errors), (4, 1, 0))
        self.assertTrue(got[4][1].startswith("423"))
        self.assertEqual(srv.exact("ARTICLE 4"), 2)

    def test_a_failed_retry_keeps_the_first_answer(self):
        # two workers use connections 1 and 2; the retry's connection 3 is refused at GROUP
        srv = Scripted(_arts(4), absent={2: 1}, down_from=3)
        (ok, missing, errors), got = self._fetch(srv, 4)
        self.assertEqual((ok, missing, errors), (3, 1, 0))
        self.assertTrue(got[2][1].startswith("423"))

    def test_retry_uses_no_more_than_C_connections(self):
        srv = Scripted(_arts(12), absent={5: 1, 9: 1})
        (ok, missing, errors), _ = self._fetch(srv, 12, connections=3)
        self.assertEqual((ok, missing, errors), (12, 0, 0))
        self.assertLessEqual(srv.max_open_conns, 3)


if __name__ == "__main__":
    unittest.main()
