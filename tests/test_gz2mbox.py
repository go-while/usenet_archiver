#!/usr/bin/env python3
"""extras/gz2mbox.py: splitting dumps into articles (review findings 1.14, 5.7)."""

from __future__ import annotations

import gzip
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location("gz2mbox", os.path.join(_ROOT, "extras", "gz2mbox.py"))
gz2mbox = importlib.util.module_from_spec(_spec)
sys.modules["gz2mbox"] = gz2mbox  # dataclasses look their module up there
_spec.loader.exec_module(gz2mbox)

from usenet_archiver.mboxout import index_message_ids  # noqa: E402
from usenet_archiver.nntp import MAX_ARTICLE  # noqa: E402

A1 = b"Path: a\nMessage-ID: <1@x>\nSubject: one\n\nquoting headers:\nPath: someone!else\nend of one\n"
A2 = b"Newsgroups: x\nMessage-ID: <2@x>\n\ntwo, starts with Newsgroups:\n"
A3 = b"Path: c\nMessage-ID: <3@x>\n\nthree\n"


def rnews(*articles: bytes) -> bytes:
    return b"".join(b"#! rnews %d\n" % len(a) + a for a in articles)


def split(data: bytes, **kw):
    stats = gz2mbox.Stats()
    with redirect_stderr(io.StringIO()):
        out = list(gz2mbox.articles_from(io.BytesIO(data), stats=stats, **kw))
    return out, stats


class TestSplit(unittest.TestCase):
    def test_rnews_batch_split_by_byte_counts(self):
        out, _ = split(rnews(A1, A2, A3))
        self.assertEqual(out, [A1, A2, A3])

    def test_single_article_kept_whole(self):
        # its body quotes a "Path: " line: kept as one article (the old
        # splitter made two of it), with a hint that names the file
        err = io.StringIO()
        with redirect_stderr(err):
            out = list(gz2mbox.articles_from(io.BytesIO(A1), name="a.gz"))
        self.assertEqual(out, [A1])
        self.assertIn("a.gz", err.getvalue())
        with redirect_stderr(err):
            self.assertEqual(list(gz2mbox.articles_from(io.BytesIO(A3))), [A3])

    def test_concatenated_needs_split_on_path(self):
        err = io.StringIO()
        with redirect_stderr(err):
            out = list(gz2mbox.articles_from(io.BytesIO(A3 + A3.replace(b"<3@", b"<4@")), name="f.gz"))
        self.assertEqual(len(out), 1)
        self.assertIn("--split-on-path", err.getvalue())
        out, _ = split(A3 + A3.replace(b"<3@", b"<4@"), split_on_path=True)
        self.assertEqual(len(out), 2)

    def test_malformed_batch(self):
        bad = rnews(A1) + b"#! rnews 9999\nshort"
        got = []
        with self.assertRaises(gz2mbox.BatchError):
            for a in gz2mbox.articles_from(io.BytesIO(bad)):
                got.append(a)
        self.assertEqual(got, [A1])
        with self.assertRaises(gz2mbox.BatchError):
            list(gz2mbox.articles_from(io.BytesIO(rnews(A1) + b"garbage line\n")))

    def test_mbox_in_gz_refused(self):
        with self.assertRaises(gz2mbox.BatchError):
            list(gz2mbox.articles_from(io.BytesIO(b"From x Thu Dec  8 12:55:04 2016\n" + A1)))

    def test_too_big_skipped_and_counted(self):
        big = b"Message-ID: <big@x>\n\n" + b"y" * (MAX_ARTICLE + 10)
        out, stats = split(rnews(A1, big, A3))
        self.assertEqual(out, [A1, A3])
        self.assertEqual(stats.too_big, 1)
        out, stats = split(big)
        self.assertEqual((out, stats.too_big), ([], 1))


class TestMain(unittest.TestCase):
    def test_appends_and_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in", "sub")
            os.makedirs(src)
            with gzip.open(os.path.join(src, "batch.gz"), "wb") as fh:
                fh.write(rnews(A1, A2))
            with gzip.open(os.path.join(src, "single.gz"), "wb") as fh:
                fh.write(A3)
            out = os.path.join(tmp, "out.mbox")
            with open(out, "wb") as fh:  # an existing mbox must be kept
                fh.write(b"From - Thu Dec  8 12:55:04 2016\nMessage-ID: <old@x>\n\nold\n\n")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(gz2mbox.main([os.path.join(tmp, "in"), "-o", out]), 0)
                self.assertEqual(gz2mbox.main([os.path.join(tmp, "in"), "-o", out]), 0)
            self.assertEqual(index_message_ids(out), {"<old@x>", "<1@x>", "<2@x>", "<3@x>"})
            with open(out, "rb") as fh:
                data = fh.read()
            self.assertEqual(data.count(b"\nFrom "), 3)  # 4 messages, written once each
            self.assertIn(b"\nquoting headers:\nPath: someone!else\nend of one\n", data)


if __name__ == "__main__":
    unittest.main()
