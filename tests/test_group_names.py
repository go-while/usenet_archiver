#!/usr/bin/env python3
"""Group names in file names and commands (review findings 4.3, 5.1)."""

from __future__ import annotations

import io
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_TESTS = os.path.dirname(os.path.abspath(__file__))
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from usenet_archiver import cli  # noqa: E402
from usenet_archiver.nntp import NNTPClient, NNTPError  # noqa: E402
from usenet_archiver.policy import group_name_problem  # noqa: E402


class TestGroupNameProblem(unittest.TestCase):
    def test_good(self):
        for g in ("comp.lang.c", "alt.binaries-free", "de.comp.lang.c++", "fr.soc.français", "a"):
            self.assertIsNone(group_name_problem(g), g)

    def test_bad(self):
        for g in ("", "a/b", "../../x", "/tmp/abs", "a\\b", ".hidden", "a..b", "a b", "a\tb",
                  "a\r\nPOST", "nul\x00", "del\x7f"):
            self.assertIsNotNone(group_name_problem(g), repr(g))

    def test_mbox_name_refuses_bad_group(self):
        with self.assertRaises(ValueError):
            cli.mbox_name_for("../../escape", None, None, None)
        self.assertEqual(cli.mbox_name_for("../../escape", "chosen.mbox", None, None), "chosen.mbox")


class TestSendRefusesLineEnds(unittest.TestCase):
    def test_crlf_in_command(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        c = NNTPClient("x", 0)
        c.conn = a
        for cmd in ("GROUP x\r\nPOST", "AUTHINFO PASS secret\r", "GROUP a\nb"):
            with self.assertRaises(NNTPError, msg=repr(cmd)):
                c.send(cmd)


class TestBadGroupFailsOnlyItself(unittest.TestCase):
    """-g a/b -g misc.test: a/b fails, misc.test is still pulled, exit 1."""

    def test_run_goes_on(self):
        from test_nntp import FakeNNTPServer

        arts = {1: b"Message-ID: <1@x>\nSubject: s\n\nbody"}
        srv = FakeNNTPServer(articles=arts)
        srv.start()
        self.addCleanup(srv.stop)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with redirect_stderr(io.StringIO()):
                    rc = cli.main(["pull", "-s", f"127.0.0.1:{srv.port}", "--no-ssl", "--no-netrc",
                                   "-g", "a/b", "-g", "misc.test", "-C", "1"])
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 1)
            self.assertTrue(os.path.exists(os.path.join(tmp, "misc.test.mbox")))
            with open(os.path.join(tmp, "completed_newsgroups.log")) as fh:
                self.assertEqual(fh.read().split(), ["misc.test.mbox"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "a")))


if __name__ == "__main__":
    unittest.main()
