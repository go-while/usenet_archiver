#!/usr/bin/env python3
"""A 480 in the middle of a session: the server dropped the login (it timed
out). The client logs in again on the same connection and repeats what got
the 480, once.

Found with the e2e test TestLoginExpiresMidSession (nntp-testkit v0.4.1):
a 480 to ARTICLE was counted as an error for that article and the rest of
its pipeline, and the job was not recorded.
"""

from __future__ import annotations

import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usenet_archiver.nntp import AuthError, NNTPClient  # noqa: E402


class Base(unittest.TestCase):
    def client(self, replies: bytes, username="u", password="p") -> NNTPClient:
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.sendall(replies)
        self.peer = b
        c = NNTPClient("x", 0, timeout=2, username=username, password=password)
        c.conn = a
        return c

    def sent(self) -> bytes:
        self.peer.settimeout(0.5)
        data = b""
        try:
            while True:
                chunk = self.peer.recv(65536)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        return data


class TestArticles(Base):
    def test_480_in_a_pipeline(self):
        c = self.client(
            b"220 1 <1@x>\r\nMessage-ID: <1@x>\r\n\r\none\r\n.\r\n"
            b"480 Authentication required\r\n"
            b"480 Authentication required\r\n"
            b"381 more\r\n281 ok\r\n"
            b"220 2 <2@x>\r\nMessage-ID: <2@x>\r\n\r\ntwo\r\n.\r\n"
            b"220 3 <3@x>\r\nMessage-ID: <3@x>\r\n\r\nthree\r\n.\r\n"
        )
        results = c.articles_pipelined([1, 2, 3])
        self.assertEqual([content.rsplit(b"\n", 1)[-1] for _s, content, _r in results], [b"one", b"two", b"three"])
        self.assertEqual(self.sent(), b"ARTICLE 1\r\nARTICLE 2\r\nARTICLE 3\r\n"
                                      b"AUTHINFO USER u\r\nAUTHINFO PASS p\r\n"
                                      b"ARTICLE 2\r\nARTICLE 3\r\n")

    def test_480_again_after_the_login_stays_an_error(self):
        c = self.client(b"480 Authentication required\r\n381 more\r\n281 ok\r\n480 Authentication required\r\n")
        (_spec, content, resp), = c.articles_pipelined([1])
        self.assertEqual((content, resp[:3]), (b"", "480"))

    def test_480_without_credentials_stays_an_error(self):
        c = self.client(b"480 Authentication required\r\n", username=None, password=None)
        (_spec, content, resp), = c.articles_pipelined([1])
        self.assertEqual((content, resp[:3]), (b"", "480"))

    def test_refused_login_raises(self):
        c = self.client(b"480 Authentication required\r\n381 more\r\n481 wrong\r\n")
        with self.assertRaises(AuthError):
            c.articles_pipelined([1])


class TestCommands(Base):
    def test_480_to_group(self):
        c = self.client(b"480 Authentication required\r\n381 more\r\n281 ok\r\n211 3 1 3 misc.test\r\n")
        self.assertEqual(c.group("misc.test")[:2], (1, 3))
        self.assertEqual(self.sent(), b"GROUP misc.test\r\nAUTHINFO USER u\r\nAUTHINFO PASS p\r\nGROUP misc.test\r\n")


if __name__ == "__main__":
    unittest.main()
