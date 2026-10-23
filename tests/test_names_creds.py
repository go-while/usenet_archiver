#!/usr/bin/env python3
"""Non-ASCII group names and passwords, files with a BOM, and ~/.netrc.

Review 2:
- C5: group names may be UTF-8 (RFC 3977 3.1.1) and passed the name check,
  but commands were sent as strict ASCII, so such a group always failed;
  so did a non-ASCII password. A groups file or password file saved with
  a BOM (Windows Notepad) got the BOM into the first group or the password.
- C7: -u alice with a ~/.netrc entry for bob sent alice's name with bob's
  password.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))

from usenet_archiver.cli import collect_group_specs  # noqa: E402
from usenet_archiver.creds import resolve_credentials  # noqa: E402
from usenet_archiver.nntp import NNTPClient  # noqa: E402
from usenet_archiver.policy import group_name_problem, read_groups_file  # noqa: E402


class Wire(unittest.TestCase):
    """A client on one end of a socketpair; the test reads what it sent."""

    def client(self, replies: bytes, **kw) -> NNTPClient:
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.sendall(replies)
        self.peer = b
        c = NNTPClient("x", 0, timeout=2, **kw)
        c.conn = a
        return c

    def sent(self) -> bytes:
        self.peer.settimeout(1)
        return self.peer.recv(65536)


class TestUtf8Commands(Wire):
    @unittest.expectedFailure
    def test_utf8_group_name(self):
        c = self.client("211 3 1 3 de.alt.bücher\r\n".encode("utf-8"))
        first, last, _resp = c.group("de.alt.bücher")
        self.assertEqual((first, last), (1, 3))
        self.assertEqual(self.sent(), "GROUP de.alt.bücher\r\n".encode("utf-8"))

    @unittest.expectedFailure
    def test_name_from_list_groups_goes_back_as_it_came(self):
        # list-groups decodes a name that is not UTF-8 with surrogateescape
        raw = b"de.alt.b\xfccher"
        c = self.client(b"211 3 1 3 x\r\n")
        c.group(raw.decode("utf-8", errors="surrogateescape"))
        self.assertEqual(self.sent(), b"GROUP " + raw + b"\r\n")

    @unittest.expectedFailure
    def test_non_ascii_password(self):
        c = self.client(b"381 more\r\n281 ok\r\n", username="alice", password="pässwort")
        c._authenticate()
        self.assertEqual(self.sent(), "AUTHINFO USER alice\r\nAUTHINFO PASS pässwort\r\n".encode("utf-8"))


class TestBom(unittest.TestCase):
    def write(self, text: str) -> str:
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(text)
        return path

    @unittest.expectedFailure
    def test_groups_file_with_bom(self):
        path = self.write("comp.lang.c\r\nmisc.test>t.mbox\r\n")
        self.assertEqual(collect_group_specs(None, path), [("comp.lang.c", None), ("misc.test", "t.mbox")])
        self.assertEqual(read_groups_file(path), ["comp.lang.c", "misc.test>t.mbox"])

    @unittest.expectedFailure
    def test_password_file_with_bom(self):
        path = self.write("secret\r\n")
        ep = resolve_credentials("127.0.0.1", None, "alice", None, password_file=path, use_netrc=False)
        self.assertEqual(ep.password, "secret")


class TestGroupNameCheck(unittest.TestCase):
    def test_utf8_names_allowed(self):
        for name in ("de.alt.bücher", "fj.rec.アニメ", "de.alt.b\udcfccher"):
            self.assertIsNone(group_name_problem(name), name)

    @unittest.expectedFailure
    def test_invisible_characters_refused(self):
        # a BOM, a no-break space, a zero-width space, a line separator
        for name in ("﻿comp.lang.c", "comp.lang c", "comp.​lang.c", "comp.lang c"):
            self.assertIsNotNone(group_name_problem(name), repr(name))


class TestNetrc(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = tmp.name
        path = os.path.join(home, ".netrc")
        with open(path, "w") as fh:
            fh.write("machine news.example.com login bob password bob-secret\n")
        os.chmod(path, 0o600)
        env = mock.patch.dict(os.environ, {"HOME": home})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NNTP_PASSWORD", None)

    def resolve(self, username):
        return resolve_credentials("news.example.com", None, username, None)

    @unittest.expectedFailure
    def test_other_user_gets_no_netrc_password(self):
        ep = self.resolve("alice")
        self.assertEqual((ep.username, ep.password), ("alice", None))

    def test_same_user_gets_netrc_password(self):
        ep = self.resolve("bob")
        self.assertEqual((ep.username, ep.password), ("bob", "bob-secret"))

    def test_no_user_takes_both(self):
        ep = self.resolve(None)
        self.assertEqual((ep.username, ep.password), ("bob", "bob-secret"))


if __name__ == "__main__":
    unittest.main()
