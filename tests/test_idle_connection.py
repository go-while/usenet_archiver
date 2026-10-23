#!/usr/bin/env python3
"""The main connection may be lost while it sits idle.

Review 2, C4: it sits idle while a large mbox is indexed for dedup, and is
kept after a group with nothing to fetch. If the server (or a firewall)
closed it meanwhile, the next group read EOF and failed; and quit(), which
frees it before the fetch, raised from send() and aborted the group.
"""

from __future__ import annotations

import io
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))
sys.path.insert(0, os.path.dirname(__file__))

from test_nntp import FakeNNTPServer  # noqa: E402
from usenet_archiver import cli  # noqa: E402
from usenet_archiver import nntp as nntp_mod  # noqa: E402
from usenet_archiver.nntp import NNTPClient  # noqa: E402


class DroppingServer(FakeNNTPServer):
    """Can drop every open connection from the server side; can refuse the
    connections numbered in `refuse` with 502."""

    def __init__(self, refuse=(), **kw):
        super().__init__(**kw)
        self.conns = []
        self.refuse = set(refuse)
        self.accepted = 0

    def _handle(self, conn):
        with self._count_lock:
            self.accepted += 1
            k = self.accepted
            self.conns.append(conn)
        if k in self.refuse:
            conn.sendall(b"502 too many connections\r\n")
            return
        return super()._handle(conn)

    def drop_all(self, reset=False):
        with self._count_lock:
            conns, self.conns = self.conns, []
        for c in conns:
            try:
                if reset:  # RST: the client's next send fails at once
                    c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        time.sleep(0.1)  # let the FIN or RST reach the client


def _art(n):
    return (b"From: a@example.com\nNewsgroups: misc.test\nSubject: s\n"
            b"Date: Wed, 15 Jan 2020 12:00:00 +0000\nMessage-ID: <id%d@ex.com>\n\nbody %d\n" % (n, n))


class Base(unittest.TestCase):
    def setUp(self):
        old = nntp_mod.RECONNECT_DELAY
        nntp_mod.RECONNECT_DELAY = 0.05
        self.addCleanup(setattr, nntp_mod, "RECONNECT_DELAY", old)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def serve(self, srv):
        srv.start()
        self.addCleanup(srv.stop)
        return srv

    def main(self, srv, *groups):
        args = ["pull", "-s", f"127.0.0.1:{srv.port}", "--no-ssl", "--no-netrc", "-C", "1"]
        for g in groups:
            args += ["-g", g]
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                rc = cli.main(args)
        finally:
            os.chdir(cwd)
        return rc, err.getvalue()

    def hook_index(self, group, action):
        """Run action() right after the mbox of `group` is indexed."""
        orig = cli.MboxWriter.__init__

        def init(w, path, *a, **k):
            orig(w, path, *a, **k)
            if os.path.basename(path).startswith(group + "."):
                action()

        cli.MboxWriter.__init__ = init
        self.addCleanup(setattr, cli.MboxWriter, "__init__", orig)


class TestQuit(unittest.TestCase):
    @unittest.expectedFailure
    def test_quit_on_a_reset_connection_does_not_raise(self):
        a, b = socket.socketpair()
        b.close()
        c = NNTPClient("x", 0, timeout=1)
        c.conn = a
        c.quit()
        self.assertIsNone(c.conn)


class TestIdleMainConnection(Base):
    @unittest.expectedFailure
    def test_connection_dropped_after_a_group_with_nothing_to_fetch(self):
        srv = self.serve(DroppingServer(articles={n: _art(n) for n in (1, 2, 3)}))
        self.assertEqual(self.main(srv, "misc.a", "misc.b")[0], 0)
        # second run: nothing new; the server drops the idle main connection
        # while misc.a's mbox is indexed
        self.hook_index("misc.a", srv.drop_all)
        rc, err = self.main(srv, "misc.a", "misc.b")
        self.assertEqual(rc, 0, err)

    @unittest.expectedFailure
    def test_connection_reset_before_the_fetch(self):
        srv = self.serve(DroppingServer(articles={n: _art(n) for n in (1, 2, 3)}))
        self.hook_index("misc.a", lambda: srv.drop_all(reset=True))
        rc, err = self.main(srv, "misc.a")
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.tmp.name, "misc.a.mbox"), "rb") as fh:
            self.assertEqual(fh.read().count(b"\nbody "), 3)

    @unittest.expectedFailure
    def test_reconnect_for_the_next_group_refused_once(self):
        # connections: 1 main, 2 the fetch of misc.a, 3 main again for misc.b
        # (refused: the provider has not released 2 yet), 4 the retry
        srv = self.serve(DroppingServer(refuse={3}, articles={n: _art(n) for n in (1, 2, 3)}))
        rc, err = self.main(srv, "misc.a", "misc.b")
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.tmp.name, "misc.b.mbox"), "rb") as fh:
            self.assertEqual(fh.read().count(b"\nbody "), 3)

    def test_no_such_group_is_not_retried(self):
        class NoGroup(DroppingServer):
            groups = 0

            def _send(self, conn, data):
                if data.startswith(b"211"):
                    NoGroup.groups += 1
                    data = b"411 no such group\r\n"
                super()._send(conn, data)

        srv = self.serve(NoGroup(articles={1: _art(1)}))
        rc, err = self.main(srv, "misc.a")
        self.assertEqual(rc, 1)
        self.assertIn("411", err)
        self.assertEqual(NoGroup.groups, 1)


if __name__ == "__main__":
    unittest.main()
