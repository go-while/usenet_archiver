#!/usr/bin/env python3
"""An overview that misses articles must not complete the job (review 2, A5, A6)."""

from __future__ import annotations

import io
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.dirname(os.path.abspath(__file__))
for p in (_ROOT, _TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

from test_nntp import FakeNNTPServer  # noqa: E402
from usenet_archiver import cli  # noqa: E402
from usenet_archiver.nntp import NNTPClient  # noqa: E402


def _article(n: int) -> bytes:
    return (b"From: a@example.com\nNewsgroups: misc.test\nSubject: s%d\n"
            b"Date: Wed, 15 Jan 2020 12:00:00 +0000\nMessage-ID: <id%d@ex.com>\n\nbody\n" % (n, n))


def _no_overview(self, start_id, end_id):
    # a server whose overview database lost the group: "224" and no lines
    return [], "OVER", "224 Overview information follows"


class TestEmptyOverview(unittest.TestCase):
    """A5: GROUP reports articles, OVER lists none."""

    def _pull(self, srv):
        srv.start()
        self.addCleanup(srv.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)
        err = io.StringIO()
        try:
            with redirect_stderr(err), mock.patch.object(NNTPClient, "over", _no_overview):
                rc = cli.main(["pull", "-s", f"127.0.0.1:{srv.port}", "--no-ssl", "--no-netrc",
                               "-C", "1", "-g", "misc.test"])
        finally:
            os.chdir(cwd)
        log = os.path.join(tmp.name, "completed_newsgroups.log")
        return rc, os.path.exists(log), err.getvalue()

    @unittest.expectedFailure
    def test_articles_exist_job_incomplete(self):
        rc, recorded, err = self._pull(FakeNNTPServer(articles={n: _article(n) for n in range(1, 6)}))
        self.assertEqual((rc, recorded), (1, False), err)

    def test_really_empty_completes(self):
        srv = FakeNNTPServer(articles={})
        srv.group_first, srv.group_last = 1, 5  # numbers left from expired articles
        rc, recorded, err = self._pull(srv)
        self.assertEqual((rc, recorded), (0, True), err)


class TestXhdrLoneNumber(unittest.TestCase):
    """A6: "2" alone (an article with an empty Date, the trailing space
    trimmed) is a row without a date, which a date window keeps."""

    @unittest.expectedFailure
    def test_kept(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        c = NNTPClient("x", 0, timeout=2)
        c.conn = a
        b.sendall(b"221 Date header follows\r\n1 Wed, 15 Jan 2020 12:00:00 +0000\r\n2\r\n"
                  b"3 Thu, 16 Jan 2020 12:00:00 +0000\r\n.\r\n")
        self.assertEqual([n for n, _ in c.xhdr_date(1, 3)], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
