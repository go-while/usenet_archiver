#!/usr/bin/env python3
"""Usage errors are exit 2 before connecting; timeouts in the other commands are exit 4.

Review 2:
- D1: --overview-chunk 0 (or negative) looped forever.
- D2: a bad --start/--end or a missing -f file gave a traceback and exit 1,
  after logging in to the server.
- D3: plugin arguments were checked only when the plugin first ran: a pull
  did every OVER scan and then failed, post died with a TypeError;
  strip_headers:To:Cc crashed.
- D4: in list-groups, get and post a timeout or a reset gave a traceback
  and exit 1, not 4.
- D5: list-groups 'comp.*' fell back to a bare LIST and printed every group.
"""

from __future__ import annotations

import io
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from test_nntp import FakeNNTPServer  # noqa: E402
from usenet_archiver import cli  # noqa: E402
from usenet_archiver.nntp import NNTPClient  # noqa: E402
from usenet_archiver.plugins import parse_plugin_spec  # noqa: E402


class Counting(FakeNNTPServer):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.accepted = 0

    def _handle(self, conn):
        with self._count_lock:
            self.accepted += 1
        return super()._handle(conn)


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = Counting(articles={1: b"Message-ID: <1@x>\nNewsgroups: misc.test\n\nbody\n"})
        self.srv.start()
        self.addCleanup(self.srv.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def run_cli(self, *args, stdin=b""):
        argv = [args[0], "-s", f"127.0.0.1:{self.srv.port}", "--no-ssl", "--no-netrc", *args[1:]]
        cwd = os.getcwd()
        os.chdir(self.tmp)
        err, out = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.TextIOWrapper(io.BytesIO(stdin))
        try:
            with redirect_stderr(err), redirect_stdout(out):
                try:
                    rc = cli.main(argv)
                except SystemExit as e:
                    rc = e.code
        finally:
            sys.stdin = old_stdin
            os.chdir(cwd)
        return rc, err.getvalue()

    def assertUsageError(self, *args, stdin=b""):
        rc, err = self.run_cli(*args, stdin=stdin)
        self.assertEqual(rc, 2, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.srv.accepted, 0, "connected before the usage error")
        return err


class TestPullArguments(Base):
    def test_overview_chunk_zero_or_negative(self):
        # in a subprocess with a time limit: the bug was an endless loop
        import subprocess

        root = os.path.dirname(_HERE)
        for n in ("0", "-5"):
            try:
                p = subprocess.run(
                    [sys.executable, "-m", "usenet_archiver", "-c", "pull", "-s", f"127.0.0.1:{self.srv.port}",
                     "--no-ssl", "--no-netrc", "-g", "misc.test", "--overview-chunk", n],
                    cwd=self.tmp, env={**os.environ, "PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1"},
                    capture_output=True, text=True, timeout=10,
                )
            except subprocess.TimeoutExpired:
                self.fail(f"--overview-chunk {n}: still running after 10 s")
            self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(self.srv.accepted, 0, "connected before the usage error")

    def test_bad_dates(self):
        for opt, value in (("--start", "2025-13-01"), ("--end", "yesterday")):
            err = self.assertUsageError("pull", "-g", "misc.test", opt, value)
            self.assertIn("YYYY-MM-DD", err)

    def test_missing_groups_file(self):
        err = self.assertUsageError("pull", "-f", "missing.txt")
        self.assertIn("missing.txt", err)

    def test_groups_file_not_utf8(self):
        with open(os.path.join(self.tmp, "groups.txt"), "wb") as fh:
            fh.write(b"de.alt.b\xfccher\n")
        self.assertUsageError("pull", "-f", "groups.txt")

    def test_bad_plugin_arguments(self):
        for spec in ("strip_headers:header=X-Foo", "strip_headers:To:Cc", "mimify:text/plain:utf-8:extra"):
            err = self.assertUsageError("pull", "-g", "misc.test", "--plugin", spec)
            self.assertIn(spec, err)


class TestOtherCommands(Base):
    def test_post_bad_plugin_arguments(self):
        self.assertUsageError("post", "-g", "misc.test", "--plugin", "mimify:text/plain:utf-8:extra",
                              stdin=b"Subject: t\n\nbody\n")

    def test_get_bad_message_id(self):
        self.assertUsageError("get", "no-at-sign")

    def test_timeouts_are_exit_4(self):
        self.srv.stalls = {"LIST ACTIVE": [1.5, None], "ARTICLE <1@X>": [1.5, None]}
        for args in (("list-groups", "-t", "1"), ("get", "-t", "1", "<1@x>")):
            rc, err = self.run_cli(*args)
            self.assertEqual(rc, 4, err)
            self.assertNotIn("Traceback", err)


class TestPluginSpecs(unittest.TestCase):
    def test_good_specs(self):
        for spec in ("strip_headers", "strip_headers:To,Cc", "strip_headers:headers=X-A,X-B",
                     "mimify:text/plain:utf-8", "keep_headers:From,Subject,Message-ID", "debug:a:b=c"):
            parse_plugin_spec(spec)

    def test_bad_specs(self):
        for spec in ("strip_headers:header=X-Foo", "strip_headers:To:Cc", "mimify:a:b:c",
                     "keep_headers:message=x"):
            with self.assertRaises(ValueError, msg=spec):
                parse_plugin_spec(spec)


class TestListWildmat(unittest.TestCase):
    def client(self, replies):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.sendall(replies)
        c = NNTPClient("x", 0, timeout=2)
        c.conn = a
        return c

    LIST = b"215 list follows\r\ncomp.lang.c 2 1 y\r\ncomp.lang.go 2 1 y\r\nalt.test 9 1 y\r\nde.test 3 1 y\r\n.\r\n"

    def test_fallback_to_list_keeps_the_filter(self):
        c = self.client(b"501 syntax error\r\n" + self.LIST)
        self.assertEqual(c.list_active("comp.*"), ["comp.lang.c", "comp.lang.go"])

    def test_wildmat_alternatives_and_negation(self):
        c = self.client(b"501 syntax error\r\n" + self.LIST)
        self.assertEqual(c.list_active("comp.*,!comp.lang.go,de.*"), ["comp.lang.c", "de.test"])

    def test_no_wildmat_lists_all(self):
        c = self.client(self.LIST)
        self.assertEqual(len(c.list_active(None)), 4)


if __name__ == "__main__":
    unittest.main()
