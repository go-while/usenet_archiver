#!/usr/bin/env python3
"""Connecting: the greeting, the CAPABILITIES reply, AUTHINFO, and retries.

Review 2:
- C2: a worker whose connect fails once turned the rest of its shard into
  errors, with tries left; the overview retry had the same flaw. A refused
  greeting (502 too many connections) was not noticed.
- C3: a timeout inside the CAPABILITIES reply was swallowed, and the late
  101 reply was then read as the answer to GROUP.
- C6: after 281 to AUTHINFO USER the client still sent PASS.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))
sys.path.insert(0, os.path.dirname(__file__))

from test_nntp import FakeNNTPServer  # noqa: E402
from usenet_archiver import nntp as nntp_mod  # noqa: E402
from usenet_archiver.fetch import fetch_articles_parallel  # noqa: E402
from usenet_archiver.nntp import NNTPClient, NNTPError  # noqa: E402
from usenet_archiver.overview import OverviewRow, find_articles_in_date_range  # noqa: E402


class RefuseSome(FakeNNTPServer):
    """Greets the connections numbered in `refuse` (1 = the first) with 502."""

    def __init__(self, refuse, **kw):
        super().__init__(**kw)
        self.refuse = set(refuse)
        self.accepted = 0

    def _handle(self, conn):
        with self._count_lock:
            self.accepted += 1
            k = self.accepted
        if k in self.refuse:
            conn.sendall(b"502 too many connections\r\n")
            return
        return super()._handle(conn)


def _arts(n):
    return {i: b"Message-ID: <%d@x>\n\nbody %d" % (i, i) for i in range(1, n + 1)}


class Base(unittest.TestCase):
    def setUp(self):
        # the pause before a new try after a refused connect, shortened
        old = getattr(nntp_mod, "RECONNECT_DELAY", None)
        nntp_mod.RECONNECT_DELAY = 0.05
        self.addCleanup(setattr, nntp_mod, "RECONNECT_DELAY", old)

    def serve(self, srv):
        srv.start()
        self.addCleanup(srv.stop)
        return dict(server="127.0.0.1", port=srv.port, use_ssl=False, timeout=1)

    def fetch(self, kw, n, connections=1, depth=10):
        got = {}
        ok, missing, errors, _ = fetch_articles_parallel(
            rows=[OverviewRow(article_id=i) for i in range(1, n + 1)],
            group="misc.test",
            conn_kwargs=kw,
            connections=connections,
            pipeline_depth=depth,
            on_article=lambda row, content, resp: got.__setitem__(row.article_id, content),
        )
        return ok, missing, errors, got


class TestGreeting(Base):
    @unittest.expectedFailure
    def test_refused_greeting_raises_with_the_reply(self):
        kw = self.serve(RefuseSome({1}, articles=_arts(1)))
        c = NNTPClient(**kw)
        with self.assertRaises(NNTPError) as cm:
            c.connect()
        self.assertIn("502", str(cm.exception))
        self.assertIsNone(c.conn)


class TestWorkerRetries(Base):
    @unittest.expectedFailure
    def test_first_connect_refused_once(self):
        kw = self.serve(RefuseSome({1}, articles=_arts(50)))
        ok, missing, errors, got = self.fetch(kw, 50)
        self.assertEqual((ok, missing, errors), (50, 0, 0))
        self.assertEqual(got[50], b"Message-ID: <50@x>\n\nbody 50")

    @unittest.expectedFailure
    def test_timeout_then_refused_reconnect(self):
        # the reconnect right after a timeout is refused once (the provider
        # has not released the old connection yet)
        kw = self.serve(RefuseSome({2}, articles=_arts(50), stalls={"ARTICLE 1": [1.5, 1]}))
        ok, missing, errors, _ = self.fetch(kw, 50)
        self.assertEqual((ok, missing, errors), (50, 0, 0))

    def test_server_that_refuses_every_time_gives_errors_and_ends(self):
        kw = self.serve(RefuseSome(set(range(1, 1000)), articles=_arts(50)))
        ok, missing, errors, _ = self.fetch(kw, 50)
        self.assertEqual((ok, missing, errors), (0, 0, 50))

    @unittest.expectedFailure
    def test_slow_capabilities_at_worker_connect(self):
        kw = self.serve(FakeNNTPServer(articles=_arts(30), stalls={"CAPABILITIES": [1.5, 1]}))
        ok, missing, errors, _ = self.fetch(kw, 30)
        self.assertEqual((ok, missing, errors), (30, 0, 0))


class TestOverviewRetries(Base):
    @unittest.expectedFailure
    def test_overview_reconnect_refused_once(self):
        srv = RefuseSome({2}, articles=_arts(30), stalls={"OVER 11-20": [1.5, 1]})
        kw = self.serve(srv)
        client = NNTPClient(**kw)
        client.connect()
        self.addCleanup(client.close)
        client.group("misc.test")
        rows = find_articles_in_date_range(client, 1, 30, None, None, chunk_size=10)
        self.assertEqual([r.article_id for r in rows], list(range(1, 31)))


class TestCapabilitiesTimeout(Base):
    @unittest.expectedFailure
    def test_timeout_inside_capabilities_fails_the_connect(self):
        kw = self.serve(FakeNNTPServer(articles=_arts(1), stalls={"CAPABILITIES": [1.5, 1]}))
        c = NNTPClient(**kw)
        with self.assertRaises(NNTPError):
            c.connect()
        self.assertIsNone(c.conn)

    def test_capabilities_not_supported_is_fine(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.sendall(b"500 what\r\n")
        c = NNTPClient("x", 0, timeout=2)
        c.conn = a
        self.assertEqual(c._probe_capabilities(), {})


class AuthServer:
    """Answers AUTHINFO USER with user_reply; records every command."""

    def __init__(self, user_reply: bytes):
        self.user_reply = user_reply
        self.commands = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        conn, _ = self.sock.accept()
        with conn, conn.makefile("rb") as f:
            conn.sendall(b"200 hi\r\n")
            authed = False
            for line in f:
                cmd = line.strip()
                self.commands.append(cmd)
                up = cmd.upper()
                if up.startswith(b"AUTHINFO USER"):
                    authed = self.user_reply.startswith(b"281")
                    conn.sendall(self.user_reply + b"\r\n")
                elif up.startswith(b"AUTHINFO PASS"):
                    conn.sendall(b"502 already authenticated\r\n" if authed else b"281 ok\r\n")
                elif up.startswith(b"MODE"):
                    conn.sendall(b"200 ok\r\n")
                elif up.startswith(b"QUIT"):
                    conn.sendall(b"205 bye\r\n")
                    return
                else:
                    conn.sendall(b"500 ?\r\n")

    def close(self):
        self.sock.close()


class TestAuthinfo(unittest.TestCase):
    def connect(self, user_reply, username="u", password="p"):
        srv = AuthServer(user_reply)
        self.addCleanup(srv.close)
        c = NNTPClient("127.0.0.1", srv.port, username=username, password=password, use_ssl=False, timeout=2)
        self.addCleanup(c.close)
        return c, srv

    @unittest.expectedFailure
    def test_281_to_user_sends_no_password(self):
        c, srv = self.connect(b"281 no password needed")
        c.connect()
        self.assertFalse(any(cmd.upper().startswith(b"AUTHINFO PASS") for cmd in srv.commands))

    @unittest.expectedFailure
    def test_user_without_password(self):
        c, srv = self.connect(b"281 no password needed", password=None)
        c.connect()
        self.assertIn(b"AUTHINFO USER u", srv.commands)

    @unittest.expectedFailure
    def test_381_without_password_fails_clearly(self):
        c, _srv = self.connect(b"381 password required", password=None)
        with self.assertRaises(NNTPError) as cm:
            c.connect()
        self.assertIn("password", str(cm.exception))

    def test_user_and_password(self):
        c, srv = self.connect(b"381 password required")
        c.connect()
        self.assertEqual(srv.commands[:2], [b"AUTHINFO USER u", b"AUTHINFO PASS p"])

    @unittest.expectedFailure
    def test_user_refused(self):
        c, _srv = self.connect(b"481 go away")
        with self.assertRaises(NNTPError) as cm:
            c.connect()
        self.assertIn("481", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
