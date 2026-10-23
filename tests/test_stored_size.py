#!/usr/bin/env python3
"""mbox2nntp's size limit on the body as stored (review 2, A4).

mbox2nntp drops an article whose body, as stored in the mbox, is 1 MiB or
more: its body lines with their line ends and the mboxrd quotes, plus the
empty line after them (measured against mbox2nntp -dry-run). The archiver's
own limit counts the article as fetched, without the quotes, so a digest just
under it with many "From " lines was saved and its job recorded, and then
dropped on import. Such an article is now counted too-big, like one over
the fetch limit.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.dirname(os.path.abspath(__file__))
for p in (_ROOT, _TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

from usenet_archiver.mboxout import MboxWriter  # noqa: E402
from usenet_archiver.nntp import MAX_ARTICLE  # noqa: E402

LIMIT = 1 << 20  # mbox2nntp's maxArticleSize
HEAD = (b"From: a@example.com\nNewsgroups: misc.test\nSubject: digest\n"
        b"Date: Wed, 15 Jan 2020 12:00:00 +0000\nMessage-ID: <%s@ex.com>\n\n")


def digest(mid: bytes, stored_lines_size: int) -> bytes:
    """An article whose body lines take stored_lines_size bytes in the mbox
    (quotes and line ends included): 4,000 quoted "From " lines, then plain
    lines up to the size."""
    lines = [b"From " + b"x" * 200] * 4000  # 206 bytes each as stored
    left = stored_lines_size - 4000 * 207
    while left > 1001 * 2:
        lines.append(b"y" * 1000)
        left -= 1001
    lines.append(b"z" * (left - 1))
    return HEAD % mid + b"\n".join(lines) + b"\n"


class TestStoredBodyLimit(unittest.TestCase):
    def test_boundary(self):
        from usenet_archiver.mboxout import ArticleTooBig

        under = digest(b"under", LIMIT - 1)  # stored with the empty line: LIMIT bytes, kept
        over = digest(b"over", LIMIT)
        self.assertLessEqual(len(over), MAX_ARTICLE, "under the fetch limit as fetched")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.mbox")
            with MboxWriter(path) as w:
                self.assertTrue(w.write(under))
                size = os.path.getsize(path)
                with self.assertRaises(ArticleTooBig):
                    w.write(over)
                self.assertEqual(os.path.getsize(path), size, "nothing of it written")
                self.assertTrue(w.write(digest(b"after", 1000 * 207 * 5)))

    def test_pull_counts_it_too_big(self):
        from test_nntp import FakeNNTPServer
        from usenet_archiver import cli

        small = HEAD % b"small" + b"body\n"
        srv = FakeNNTPServer(articles={1: small, 2: digest(b"over", LIMIT), 3: small.replace(b"small", b"small3")})
        srv.start()
        self.addCleanup(srv.stop)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    rc = cli.main(["pull", "-s", f"127.0.0.1:{srv.port}", "--no-ssl", "--no-netrc",
                                   "-C", "1", "-g", "misc.test"])
            finally:
                os.chdir(cwd)
            with open(os.path.join(tmp, "misc.test.mbox"), "rb") as fh:
                data = fh.read()
        self.assertIn("saved=2", err.getvalue())
        self.assertIn("too-big=1", err.getvalue())
        self.assertNotIn(b"<over@ex.com>", data)
        self.assertEqual(rc, 0, "a too-big article does not keep the job from completing")

    def test_gz2mbox_counts_it_too_big(self):
        import gzip
        import importlib.util

        spec = importlib.util.spec_from_file_location("gz2mbox", os.path.join(_ROOT, "extras", "gz2mbox.py"))
        gz2mbox = importlib.util.module_from_spec(spec)
        sys.modules["gz2mbox"] = gz2mbox
        spec.loader.exec_module(gz2mbox)
        over = digest(b"over", LIMIT)
        small = HEAD % b"small" + b"body\n"
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in")
            os.makedirs(src)
            with gzip.open(os.path.join(src, "a.gz"), "wb") as fh:
                fh.write(b"".join(b"#! rnews %d\n" % len(a) + a for a in (over, small)))
            out = os.path.join(tmp, "out.mbox")
            err = io.StringIO()
            with redirect_stderr(err):
                rc = gz2mbox.main([src, "-o", out])
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("1 articles written", err.getvalue())
            self.assertIn("1 over", err.getvalue())
            with open(out, "rb") as fh:
                self.assertNotIn(b"<over@ex.com>", fh.read())

if __name__ == "__main__":
    unittest.main()
