#!/usr/bin/env python3
"""Tests for plugin parsing and mutation."""

from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))

from usenet_archiver.plugins import (  # noqa: E402
    RawArticle,
    apply_plugins,
    keep_headers,
    mimify,
    parse_plugin_spec,
    strip_headers,
)


def _msg():
    return RawArticle(
        b"From: a@x\nTo: b@x\nCc: c@x\nSubject: Hi\n"
        b"Message-ID: <m@x>\nContent-Type: text/plain\n\nBody\n"
    )


class TestParseSpec(unittest.TestCase):
    def test_simple(self):
        fn = parse_plugin_spec("strip_headers")
        msg = _msg()
        out = fn(message=msg)
        self.assertNotIn("To", out)
        self.assertNotIn("Cc", out)
        self.assertIn("Subject", out)

    def test_positional(self):
        fn = parse_plugin_spec("strip_headers:Subject")
        msg = _msg()
        out = fn(message=msg)
        self.assertNotIn("Subject", out)
        self.assertIn("To", out)

    def test_kwargs(self):
        fn = parse_plugin_spec("mimify:type=text/html:charset=UTF-8")
        msg = RawArticle(b"From: a@x\nSubject: x\n\nBody\n")
        out = fn(message=msg)
        self.assertIn("text/html", out["Content-Type"])
        self.assertIn("UTF-8", out["Content-Type"])

    def test_unknown(self):
        with self.assertRaises(ValueError):
            parse_plugin_spec("nope")


class TestPlugins(unittest.TestCase):
    def test_mimify_skips_existing(self):
        msg = _msg()
        out = mimify(message=msg)
        self.assertEqual(out["Content-Type"], "text/plain")

    def test_keep_headers(self):
        msg = _msg()
        out = keep_headers(headers="From,Subject", message=msg)
        self.assertIn("From", out)
        self.assertIn("Subject", out)
        self.assertNotIn("To", out)
        self.assertEqual(out.get_payload(), "")

    def test_apply_drop(self):
        def drop(message=None):
            return None

        msg = _msg()
        self.assertIsNone(apply_plugins(msg, [drop]))

    def test_chain(self):
        msg = _msg()
        plugins = [parse_plugin_spec("strip_headers"), parse_plugin_spec("mimify")]
        out = apply_plugins(msg, plugins)
        self.assertIsNotNone(out)
        self.assertNotIn("To", out)


class TestByteExact(unittest.TestCase):
    """1.13: a plugin changes only the fields it touches."""

    ARTICLE = (
        b"Path: x\nFrom: a@b\nSubject: caf\xe9 au lait\nMessage-ID: <p@x>\n"
        b"X-Long: " + b"word " * 30 + b"\nTo: someone@x,\n  other@x\nCc: c@x\n\n"
        b"body caf\xe9\nFrom the start\n"
    )

    def test_no_change_is_identity(self):
        self.assertEqual(RawArticle(self.ARTICLE).to_bytes(), self.ARTICLE)

    def test_strip_headers_removes_only_those_fields(self):
        out = apply_plugins(RawArticle(self.ARTICLE), [parse_plugin_spec("strip_headers:To")]).to_bytes()
        self.assertEqual(out, self.ARTICLE.replace(b"To: someone@x,\n  other@x\n", b""))

    def test_keep_headers_keeps_fields_byte_exact(self):
        out = keep_headers(headers="Subject,X-Long", message=RawArticle(self.ARTICLE)).to_bytes()
        self.assertEqual(out, b"Subject: caf\xe9 au lait\nX-Long: " + b"word " * 30 + b"\n\n")

    def test_mimify_appends_one_line(self):
        art = b"From: a@x\nSubject: s\n\nb\n"
        self.assertEqual(mimify(message=RawArticle(art)).to_bytes(),
                         b"From: a@x\nSubject: s\nContent-Type: text/plain; charset=US-ASCII\n\nb\n")

    def test_crlf_kept(self):
        art = b"From: a@x\r\nTo: t@x\r\nSubject: s\r\n\r\nb\r\n"
        out = strip_headers(message=RawArticle(art)).to_bytes()
        self.assertEqual(out, b"From: a@x\r\nSubject: s\r\n\r\nb\r\n")


def _distinct_replies() -> dict:
    # three different replies, same poster and subject: without Message-ID
    # nothing tells them apart
    return {
        n: (b"Path: a!b\nFrom: a@example.com\nNewsgroups: misc.test\nSubject: Re: help\n"
            b"Date: Wed, 15 Jan 2020 12:00:0%d +0000\nMessage-ID: <r%d@ex.com>\n\nreply %d\n" % (n, n, n))
        for n in (1, 2, 3)
    }


class TestPluginsKeepMessageID(unittest.TestCase):
    """Review 2, A3: dedup recognises an article by its Message-ID, so a pull
    must not run plugins that remove it: different articles then look the
    same, and all but one are skipped as duplicates."""

    def _pull(self, *plugin_specs):
        import io
        import tempfile
        from contextlib import redirect_stderr

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_nntp import FakeNNTPServer
        from usenet_archiver import cli

        srv = FakeNNTPServer(articles=_distinct_replies())
        srv.start()
        self.addCleanup(srv.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        argv = ["pull", "-s", f"127.0.0.1:{srv.port}", "--no-ssl", "--no-netrc", "-C", "1", "-g", "misc.test"]
        for spec in plugin_specs:
            argv += ["--plugin", spec]
        cwd = os.getcwd()
        os.chdir(tmp.name)
        try:
            with redirect_stderr(io.StringIO()):
                try:
                    rc = cli.main(argv)
                except SystemExit as e:
                    rc = e.code
        finally:
            os.chdir(cwd)
        mbox = os.path.join(tmp.name, "misc.test.mbox")
        data = b""
        if os.path.exists(mbox):
            with open(mbox, "rb") as fh:
                data = fh.read()
        return rc, data.count(b"\nX-Mbox2nntp-Format: mboxrd\n")  # messages written

    @unittest.expectedFailure
    def test_keep_headers_without_message_id_refused(self):
        self.assertEqual(self._pull("keep_headers:From,Subject"), (2, 0))

    @unittest.expectedFailure
    def test_strip_message_id_refused(self):
        self.assertEqual(self._pull("strip_headers:Message-ID,Date"), (2, 0))
        self.assertEqual(self._pull("strip_headers:headers=message-id"), (2, 0))

    def test_keeping_message_id_saves_every_article(self):
        self.assertEqual(self._pull("keep_headers:From,Subject,Message-ID"), (0, 3))
        self.assertEqual(self._pull("strip_headers:From,Subject"), (0, 3))

    @unittest.expectedFailure
    def test_removes_message_id(self):
        from usenet_archiver.plugins import removes_message_id

        for spec, want in (("keep_headers", False), ("keep_headers:From", True),
                           ("keep_headers:headers=From,message-id", False), ("strip_headers", False),
                           ("strip_headers:Message-ID", True), ("strip_headers:To, message-id ", True),
                           ("mimify", False), ("debug", False)):
            self.assertEqual(removes_message_id(parse_plugin_spec(spec)), want, spec)


if __name__ == "__main__":
    unittest.main()
