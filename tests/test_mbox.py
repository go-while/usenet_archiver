#!/usr/bin/env python3
"""Tests for mboxrd writing and Message-ID dedup."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_ROOT))

from usenet_archiver.mboxout import (  # noqa: E402
    FORMAT_FIELD,
    MboxWriter,
    format_envelope_utc,
    format_mbox_message,
    index_message_ids,
    needs_quote,
    split_article,
    unquote,
    write_mbox_message,
)


class TestEscape(unittest.TestCase):
    """mboxrd as mbox2nntp's internal/mboxout writes and reads it."""

    # the body of mbox2nntp's TestImportExportedMbox
    BODY = [b"From the start", b">From a quote", b">>From deeper", b"#! rnews 12",
            b">#! rnews 5", b"> plain quote", b"end"]

    def test_needs_quote(self):
        for line in (b"From x", b">From x", b">>>From x", b"#! rnews 1", b">#! rnews 1"):
            self.assertTrue(needs_quote(line), line)
        for line in (b"From:", b"from x", b" From x", b"> From x", b"#!rnews 1", b"x From y", b""):
            self.assertFalse(needs_quote(line), line)

    def test_body_quoted_one_level(self):
        article = b"Subject: s\nMessage-ID: <rd@test>\n\n" + b"\n".join(self.BODY)
        out = format_mbox_message(article)
        body = out.split(FORMAT_FIELD + b"\n\n", 1)[1].split(b"\n")
        self.assertEqual(body[:7], [b">From the start", b">>From a quote", b">>>From deeper",
                                    b">#! rnews 12", b">>#! rnews 5", b"> plain quote", b"end"])
        self.assertEqual([unquote(l) for l in body[:7]], self.BODY)

    def test_format_field_is_last_header_line(self):
        out = format_mbox_message(b"Subject: s\nMessage-ID: <a@b>\n\nbody")
        lines = out.split(b"\n")
        self.assertEqual(lines[1:5], [b"Subject: s", b"Message-ID: <a@b>", FORMAT_FIELD, b""])

    def test_cr_kept(self):
        out = format_mbox_message(b"Subject: s\n\ncol1\rcol2\nend")
        self.assertIn(b"\ncol1\rcol2\nend\n", out)

    def test_one_empty_line_after_message(self):
        for article in (b"Subject: s\n\nbody", b"Subject: s\n\nbody\n"):
            self.assertTrue(format_mbox_message(article).endswith(b"\nbody\n\n"), article)

    def test_headers_only(self):
        out = format_mbox_message(b"Subject: s\nMessage-ID: <h@x>")
        self.assertTrue(out.endswith(b"Message-ID: <h@x>\n" + FORMAT_FIELD + b"\n\n\n"))

    def test_split_article_crlf(self):
        head, body = split_article(b"Subject: s\r\nX: y\r\n\r\nbody\r\n")
        self.assertEqual(head, [b"Subject: s\r", b"X: y\r"])
        self.assertEqual(body, [b"body\r"])


class TestEnvelope(unittest.TestCase):
    def test_locale_independent(self):
        dt = datetime(2020, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        line = format_envelope_utc(dt, "user@example.com")
        self.assertTrue(line.startswith(b"From user@example.com "))
        self.assertIn(b"Wed", line)  # 2020-01-15 was a Wednesday
        self.assertIn(b"Jan", line)
        self.assertIn(b"2020", line)
        # Must be ASCII
        line.decode("ascii")

    def test_year_padded(self):
        line = format_envelope_utc(datetime(100, 1, 1, 10, tzinfo=timezone.utc), "a@b")
        self.assertEqual(line, b"From a@b Fri Jan  1 10:00:00 0100\n")

    def test_missing_or_overflowing_date_gives_epoch(self):
        from datetime import timedelta
        late = datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-2)))
        for dt in (None, late):
            self.assertEqual(format_envelope_utc(dt, "a@b"), b"From a@b Thu Jan  1 00:00:00 1970\n")


class TestDedup(unittest.TestCase):
    def test_index_and_skip(self):
        article1 = (
            b"From: a@x\nMessage-ID: <one@ex.com>\nDate: Wed, 15 Jan 2020 12:00:00 +0000\n"
            b"Subject: One\n\nBody one\n"
        )
        article2 = (
            b"From: b@x\nMessage-ID: <two@ex.com>\nDate: Wed, 15 Jan 2020 13:00:00 +0000\n"
            b"Subject: Two\n\nBody two\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.mbox")
            with MboxWriter(path, dedup=True) as w:
                self.assertTrue(w.write(article1))
                self.assertTrue(w.write(article2))
                self.assertFalse(w.write(article1))  # dup
                self.assertEqual(w.written, 2)
                self.assertEqual(w.skipped_dup, 1)

            ids = index_message_ids(path)
            self.assertIn("<one@ex.com>", ids)
            self.assertIn("<two@ex.com>", ids)

            # Re-open append: dups still skipped
            with MboxWriter(path, dedup=True) as w:
                self.assertFalse(w.write(article1))
                self.assertEqual(w.skipped_dup, 1)

    def test_append_does_not_truncate(self):
        article1 = b"From: a@x\nMessage-ID: <a@ex.com>\nSubject: A\n\nA\n"
        article2 = b"From: b@x\nMessage-ID: <b@ex.com>\nSubject: B\n\nB\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.mbox")
            with MboxWriter(path, dedup=True) as w:
                w.write(article1)
            size1 = os.path.getsize(path)
            with MboxWriter(path, dedup=True) as w:
                w.write(article2)
            size2 = os.path.getsize(path)
            self.assertGreater(size2, size1)


class TestWriteMessage(unittest.TestCase):
    def test_roundtrip_bytes(self):
        # Include non-ASCII ISO-8859-1 byte in body
        article = (
            b"From: =?iso-8859-1?q?J=F6rg?= <j@ex.com>\n"
            b"Message-ID: <caf\xe9@ex.com>\n"
            b"Subject: Test\n\n"
            b"caf\xe9 au lait\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.mbox")
            with open(path, "wb") as fh:
                write_mbox_message(fh, article)
            with open(path, "rb") as fh:
                data = fh.read()
            self.assertIn(b"caf\xe9 au lait", data)
            self.assertTrue(data.startswith(b"From "))


if __name__ == "__main__":
    unittest.main()
