#!/usr/bin/env python3
"""Fake NNTP server integration tests for the byte-safe client."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))

from usenet_archiver.nntp import NNTPClient, NNTPError, is_missing_article  # noqa: E402


class FakeNNTPServer:
    """Minimal NNTP server for unit tests (plain TCP, no TLS)."""

    def __init__(self, articles=None, over_rows=None, fragment=False, stalls=None):
        self.articles = articles or {}
        # client line -> [seconds, times]: sleep before replying to that line,
        # the next `times` times it is seen (None = every time)
        self.stalls = stalls or {}
        self.over_rows = over_rows or {}
        self.fragment = fragment
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.group_first = 1
        self.group_last = max(self.articles.keys()) if self.articles else 1

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            # Unblock accept
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        self._sock.close()
        self._thread.join(timeout=2)

    def _send(self, conn, data: bytes):
        if self.fragment and len(data) > 1:
            # Send one byte at a time to stress the buffer
            for i in range(0, len(data), max(1, len(data) // 7)):
                conn.sendall(data[i : i + max(1, len(data) // 7)])
        else:
            conn.sendall(data)

    def _readline(self, conn, buf: bytearray) -> bytes:
        while True:
            nl = buf.find(b"\n")
            if nl >= 0:
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                return line
            chunk = conn.recv(4096)
            if not chunk:
                return b""
            buf.extend(chunk)

    def _serve(self):
        while not self._stop.is_set():
            try:
                self._sock.settimeout(0.5)
                try:
                    conn, _addr = self._sock.accept()
                except socket.timeout:
                    continue
            except OSError:
                break
            try:
                self._handle(conn)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn):
        conn.settimeout(5)
        buf = bytearray()
        self._send(conn, b"200 fake NNTP ready\r\n")
        while not self._stop.is_set():
            line = self._readline(conn, buf)
            if not line:
                return
            try:
                cmd = line.decode("ascii", errors="replace")
            except Exception:
                return
            upper = cmd.upper()
            stall = self.stalls.get(upper)
            if stall and stall[1] != 0:
                if stall[1] is not None:
                    stall[1] -= 1
                time.sleep(stall[0])
            if upper.startswith("CAPABILITIES"):
                self._send(conn, b"101 Capability list:\r\nVERSION 2\r\nREADER\r\nOVER\r\n.\r\n")
            elif upper.startswith("MODE READER"):
                self._send(conn, b"200 Reader mode enabled\r\n")
            elif upper.startswith("AUTHINFO USER"):
                self._send(conn, b"381 Password required\r\n")
            elif upper.startswith("AUTHINFO PASS"):
                self._send(conn, b"281 Authentication accepted\r\n")
            elif upper.startswith("GROUP"):
                parts = cmd.split()
                name = parts[1] if len(parts) > 1 else "test"
                count = max(0, self.group_last - self.group_first + 1)
                self._send(
                    conn,
                    f"211 {count} {self.group_first} {self.group_last} {name}\r\n".encode(),
                )
            elif upper.startswith("STAT"):
                parts = cmd.split()
                aid = int(parts[1])
                if aid in self.articles:
                    self._send(conn, f"223 {aid} <id{aid}@ex.com>\r\n".encode())
                else:
                    self._send(conn, b"423 No such article number\r\n")
            elif upper.startswith("ARTICLE"):
                parts = cmd.split(None, 1)
                spec = parts[1] if len(parts) > 1 else ""
                article = None
                if spec.isdigit():
                    article = self.articles.get(int(spec))
                    aid = int(spec)
                elif spec.startswith("<"):
                    for k, v in self.articles.items():
                        if spec.encode() in v or spec in v.decode("latin-1", errors="replace"):
                            article = v
                            aid = k
                            break
                    else:
                        aid = 0
                else:
                    aid = 0
                if article is None:
                    # Missing article — must not crash the client
                    self._send(conn, b"430 No such article\r\n")
                else:
                    self._send(conn, f"220 {aid} <id{aid}@ex.com>\r\n".encode())
                    # Dot-stuff lines starting with '.'
                    body = article.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    for bline in body.split(b"\n"):
                        if bline.startswith(b"."):
                            bline = b"." + bline
                        self._send(conn, bline + b"\r\n")
                    self._send(conn, b".\r\n")
            elif upper.startswith("OVER") or upper.startswith("XOVER"):
                parts = cmd.split()
                rng = parts[1] if len(parts) > 1 else "1-1"
                if "-" in rng:
                    a, b = rng.split("-", 1)
                    start, end = int(a), int(b)
                else:
                    start = end = int(rng)
                self._send(conn, b"224 Overview information follows\r\n")
                for aid in range(start, end + 1):
                    if aid in self.over_rows:
                        self._send(conn, self.over_rows[aid] + b"\r\n")
                    elif aid in self.articles:
                        # Minimal synthetic overview
                        line = (
                            f"{aid}\tSubj\ta@x\t"
                            f"Wed, 15 Jan 2020 12:00:00 +0000\t"
                            f"<id{aid}@ex.com>\t\t10\t1"
                        ).encode()
                        self._send(conn, line + b"\r\n")
                self._send(conn, b".\r\n")
            elif upper.startswith("LIST ACTIVE") or upper == "LIST":
                self._send(conn, b"215 Newsgroups follow\r\n")
                self._send(conn, b"misc.test 2 1 y\r\n")
                self._send(conn, b"news.groups 10 1 y\r\n")
                self._send(conn, b".\r\n")
            elif upper.startswith("POST"):
                self._send(conn, b"340 Ok, send article\r\n")
                # Read until lone '.'
                while True:
                    pline = self._readline(conn, buf)
                    if pline == b".":
                        break
                self._send(conn, b"240 Article posted\r\n")
            elif upper.startswith("QUIT"):
                self._send(conn, b"205 Bye\r\n")
                return
            else:
                self._send(conn, b"500 Unknown command\r\n")


class TestNNTPClient(unittest.TestCase):
    def _client(self, server: FakeNNTPServer) -> NNTPClient:
        return NNTPClient(
            server="127.0.0.1",
            port=server.port,
            username="u",
            password="p",
            use_ssl=False,
            verbose=False,
            timeout=5,
        )

    def test_missing_article_no_crash(self):
        """Provider claims an article exists but ARTICLE returns 430 — client continues."""
        articles = {
            1: b"From: a@x\nMessage-ID: <1@ex.com>\nSubject: One\n\nHello\n",
            # 2 deliberately missing
            3: b"From: c@x\nMessage-ID: <3@ex.com>\nSubject: Three\n\nWorld\n",
        }
        server = FakeNNTPServer(articles=articles)
        server.group_first, server.group_last = 1, 3
        server.start()
        try:
            client = self._client(server)
            client.connect()
            client.group("misc.test")
            c1, r1 = client.article(1)
            self.assertTrue(c1.startswith(b"From:"))
            c2, r2 = client.article(2)
            self.assertEqual(c2, b"")
            self.assertTrue(r2.startswith("430") or r2.startswith("423"))
            c3, r3 = client.article(3)
            self.assertIn(b"World", c3)
            client.quit()
        finally:
            server.stop()

    def test_dot_unstuffing_fragmented(self):
        # Body contains a line that starts with '.' which must be unstuffed,
        # and TCP is fragmented.
        body = (
            b"From: a@x\nMessage-ID: <dot@ex.com>\nSubject: Dot\n\n"
            b"normal\n"
            b".hidden\n"
            b"trailing\n"
        )
        server = FakeNNTPServer(articles={1: body}, fragment=True)
        server.start()
        try:
            client = self._client(server)
            client.connect()
            content, resp = client.article(1)
            self.assertTrue(resp.startswith("220"))
            self.assertIn(b"\n.hidden\n", content)
            self.assertNotIn(b"\n..hidden\n", content)
            # Non-ASCII bytes preserved
            latin = body + b"caf\xe9\n"
            # Re-fetch via a second server would be needed; instead verify
            # latin article on a fresh connection:
            client.quit()
        finally:
            server.stop()

        server2 = FakeNNTPServer(
            articles={1: b"From: a@x\n\ncaf\xe9 au lait\n"},
            fragment=True,
        )
        server2.start()
        try:
            client = self._client(server2)
            client.connect()
            content, _ = client.article(1)
            self.assertIn(b"caf\xe9 au lait", content)
            client.quit()
        finally:
            server2.stop()

    def test_list_groups(self):
        server = FakeNNTPServer(articles={1: b"x"})
        server.start()
        try:
            client = self._client(server)
            client.connect()
            names = client.list_active()
            self.assertIn("misc.test", names)
            self.assertIn("news.groups", names)
            client.quit()
        finally:
            server.stop()

    def test_over(self):
        articles = {1: b"From: a@x\nSubject: S\n\nB\n"}
        over = {
            1: b"1\tS\ta@x\tWed, 15 Jan 2020 12:00:00 +0000\t<id1@ex.com>\t\t10\t1",
        }
        server = FakeNNTPServer(articles=articles, over_rows=over)
        server.start()
        try:
            client = self._client(server)
            client.connect()
            lines, cmd, resp = client.over(1, 1)
            self.assertTrue(resp.startswith("224"))
            self.assertEqual(len(lines), 1)
            self.assertIn(b"<id1@ex.com>", lines[0])
            client.quit()
        finally:
            server.stop()

    def test_post(self):
        server = FakeNNTPServer(articles={1: b"x"})
        server.start()
        try:
            client = self._client(server)
            client.connect()
            resp = client.post(b"From: a@x\nNewsgroups: misc.test\nSubject: Hi\n\nHello\n")
            self.assertTrue(resp.startswith("240"))
            client.quit()
        finally:
            server.stop()

    def test_get_by_message_id(self):
        articles = {
            5: b"From: a@x\nMessage-ID: <unique@ex.com>\nSubject: U\n\nBody\n",
        }
        server = FakeNNTPServer(articles=articles)
        server.group_first = server.group_last = 5
        server.start()
        try:
            client = self._client(server)
            client.connect()
            content, resp = client.article("<unique@ex.com>")
            self.assertTrue(resp.startswith("220"))
            self.assertIn(b"Body", content)
            missing, resp2 = client.article("<nope@ex.com>")
            self.assertEqual(missing, b"")
            self.assertTrue(resp2.startswith("430"))
            client.quit()
        finally:
            server.stop()

    def test_articles_pipelined(self):
        articles = {
            1: b"From: a@x\nMessage-ID: <1@ex.com>\nSubject: One\n\nA\n",
            2: b"From: b@x\nMessage-ID: <2@ex.com>\nSubject: Two\n\nB\n",
            3: b"From: c@x\nMessage-ID: <3@ex.com>\nSubject: Three\n\nC\n",
        }
        server = FakeNNTPServer(articles=articles)
        server.group_first, server.group_last = 1, 3
        server.start()
        try:
            client = self._client(server)
            client.connect()
            results = client.articles_pipelined([1, 2, 99, 3])
            self.assertEqual(len(results), 4)
            self.assertIn(b"A", results[0][1])
            self.assertIn(b"B", results[1][1])
            self.assertEqual(results[2][1], b"")  # missing
            self.assertTrue(results[2][2].startswith("430") or results[2][2].startswith("423"))
            self.assertIn(b"C", results[3][1])
            client.quit()
        finally:
            server.stop()


class TestParallelFetch(unittest.TestCase):
    def test_parallel_and_pipeline(self):
        from datetime import datetime, timezone

        from usenet_archiver.fetch import fetch_articles_parallel
        from usenet_archiver.overview import OverviewRow

        articles = {
            i: (
                f"From: u{i}@x\nMessage-ID: <{i}@ex.com>\nSubject: S{i}\n\nBody {i}\n"
            ).encode()
            for i in range(1, 21)
        }
        # Leave a gap
        del articles[7]
        server = FakeNNTPServer(articles=articles)
        server.group_first, server.group_last = 1, 20
        server.start()
        try:
            rows = [
                OverviewRow(
                    article_id=i,
                    date=datetime(2020, 1, 1, tzinfo=timezone.utc),
                    message_id=f"<{i}@ex.com>",
                )
                for i in range(1, 21)
            ]
            got = []

            def on_article(row, content, resp):
                got.append((row.article_id, bool(content), resp[:3] if resp else ""))

            ok, missing, errors, elapsed = fetch_articles_parallel(
                rows=rows,
                group="misc.test",
                conn_kwargs={
                    "server": "127.0.0.1",
                    "port": server.port,
                    "username": "u",
                    "password": "p",
                    "use_ssl": False,
                    "verbose": False,
                    "timeout": 5,
                },
                connections=4,
                pipeline_depth=5,
                on_article=on_article,
            )
            self.assertEqual(len(got), 20)
            self.assertEqual(ok, 19)
            self.assertEqual(missing, 1)
            self.assertEqual(errors, 0)
            self.assertGreater(elapsed, 0)
            ids = sorted(g[0] for g in got)
            self.assertEqual(ids, list(range(1, 21)))
        finally:
            server.stop()


class TestOutOfStep(unittest.TestCase):
    """After a timeout or a cut the stream is out of step: reconnect, never read on."""

    def _client(self, server_bytes: bytes) -> NNTPClient:
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.sendall(server_bytes)
        c = NNTPClient("x", 0, timeout=2)
        c.conn = a
        return c

    def test_reply_for_another_article_raises(self):
        c = self._client(b"220 7 <x@y>\r\nSubject: s\r\n\r\nb\r\n.\r\n")
        with self.assertRaises(NNTPError):
            c.articles_pipelined([3])

    def test_body_line_as_status_raises(self):
        for line in (b"rest of an earlier article", b"220this", b""):
            c = self._client(line + b"\r\n")
            with self.assertRaises(NNTPError, msg=line):
                c.articles_pipelined([3])

    def test_worker_reconnects_after_last_failure(self):
        """Article 1 always answers late; 2 and 3 must still pair with their rows.

        Its body holds a line that looks like a status: read on the old
        connection, the late reply used to be taken for the answer to 2.
        """
        from usenet_archiver.fetch import fetch_articles_parallel
        from usenet_archiver.overview import OverviewRow

        arts = {
            1: b"Message-ID: <id1@ex.com>\n\nfirst\n220 looks like a status line\nrest of 1",
            2: b"Message-ID: <id2@ex.com>\n\nsecond",
            3: b"Message-ID: <id3@ex.com>\n\nthird",
        }
        srv = FakeNNTPServer(articles=arts, stalls={"ARTICLE 1": [1.3, None]})
        srv.start()
        self.addCleanup(srv.stop)
        got = {}
        ok, missing, errors, _ = fetch_articles_parallel(
            rows=[OverviewRow(article_id=n) for n in (1, 2, 3)],
            group="misc.test",
            conn_kwargs=dict(server="127.0.0.1", port=srv.port, use_ssl=False, timeout=1),
            connections=1,
            pipeline_depth=1,
            on_article=lambda row, content, resp: got.__setitem__(row.article_id, (content, resp)),
        )
        self.assertEqual((ok, missing, errors), (2, 0, 1))
        self.assertTrue(got[1][1].startswith("error:"))
        self.assertIn(b"second", got[2][0])
        self.assertIn(b"third", got[3][0])

    def test_overview_chunk_retried_on_new_connection(self):
        from usenet_archiver.overview import find_articles_in_date_range

        arts = {n: b"Message-ID: <id%d@ex.com>\n\nb" % n for n in range(1, 31)}
        srv = FakeNNTPServer(articles=arts, stalls={"OVER 11-20": [1.3, 1]})
        srv.start()
        self.addCleanup(srv.stop)
        client = NNTPClient("127.0.0.1", srv.port, use_ssl=False, timeout=1)
        client.connect()
        self.addCleanup(client.close)
        client.group("misc.test")
        rows = find_articles_in_date_range(client, 1, 30, None, None, chunk_size=10)
        self.assertEqual([r.article_id for r in rows], list(range(1, 31)))


class TestStatusHandling(unittest.TestCase):
    """Only 423/430 mean "no such article"; every other failure is an error."""

    def _client(self, server_bytes: bytes) -> NNTPClient:
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.sendall(server_bytes)
        c = NNTPClient("x", 0, timeout=2)
        c.conn = a
        return c

    def test_is_missing_article(self):
        for resp in ("423 no such number", "430 no such article"):
            self.assertTrue(is_missing_article(resp), resp)
        for resp in ("480 auth required", "502 access denied", "503 fault", "400 bye", "error: timeout", ""):
            self.assertFalse(is_missing_article(resp), resp)

    def test_over_423_is_empty(self):
        c = self._client(b"423 no articles in that range\r\n")
        lines, cmd, resp = c.over(1, 10)
        self.assertEqual((lines, cmd), ([], "OVER"))

    def test_over_failure_raises(self):
        for status in (b"480 authentication required", b"503 overview unavailable", b"412 no group"):
            c = self._client(status + b"\r\n")
            with self.assertRaises(NNTPError, msg=status):
                c.over(1, 10)

    def test_over_unsupported_falls_back(self):
        c = self._client(b"500 what\r\n500 what\r\n")
        self.assertEqual(c.over(1, 10)[1], "")

    def test_xhdr_failure_raises(self):
        c = self._client(b"503 no\r\n")
        with self.assertRaises(NNTPError):
            c.xhdr_date(1, 10)
        self.assertEqual(self._client(b"423 empty\r\n").xhdr_date(1, 10), [])


if __name__ == "__main__":
    unittest.main()
