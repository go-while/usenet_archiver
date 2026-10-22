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


if __name__ == "__main__":
    unittest.main()
