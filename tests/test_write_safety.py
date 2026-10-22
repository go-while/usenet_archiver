#!/usr/bin/env python3
"""Crash and write safety of the mbox writer (review findings 1.8, 1.9, 3.1, 4.1).

A killed or crashed run must not leave half an article that the next run
appends to; two writers must not share one mbox; SIGTERM during a write must
neither hang nor tear an article; a job is recorded as completed only after
its articles are on disk.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_TESTS = os.path.dirname(os.path.abspath(__file__))
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from usenet_archiver import cli  # noqa: E402
from usenet_archiver.mboxout import MboxWriter, format_mbox_message, index_message_ids  # noqa: E402


def article(n: int, body: bytes = b"body") -> bytes:
    return b"From: a@b.example\nSubject: s%d\nMessage-ID: <%d@x>\n\n%s" % (n, n, body)


class TestPartialTail(unittest.TestCase):
    """1.8: half an article at the end of the mbox (kill, crash, power loss)."""

    def test_partial_last_message_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.mbox")
            with MboxWriter(path) as w:
                w.write(article(1))
                w.write(article(2))
            whole = open(path, "rb").read()
            third = format_mbox_message(article(3, b"line one\nline two"), None, "a@b.example")
            with open(path, "ab") as fh:
                fh.write(third[: third.index(b"line two") + 4])  # killed mid-line

            w = MboxWriter(path)
            # the cut article must not count as present: the next run fetches it again
            self.assertFalse(w.has_message_id("<3@x>"))
            with w:
                w.write(article(3, b"line one\nline two"))
            data = open(path, "rb").read()
            self.assertEqual(data, whole + third)


class TestJournal(unittest.TestCase):
    """The journal of a run that never closed its mbox (killed, crashed)."""

    def test_killed_mid_message_then_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.mbox")
            code = textwrap.dedent(
                f"""
                import os, sys
                sys.path.insert(0, {_ROOT!r})
                from usenet_archiver import mboxout
                w = mboxout.MboxWriter({path!r}, flush_every=2)
                w.open()
                for n in (1, 2, 3):
                    w.write(b"Message-ID: <%d@x>\\n\\nbody %d" % (n, n))
                real = os.write
                def half_then_die(fd, data):
                    real(fd, bytes(data[: len(data) // 2]))
                    os._exit(9)  # killed in the middle of message 4
                os.write = half_then_die
                w.write(b"Message-ID: <4@x>\\n\\nbody 4")
                """
            )
            subprocess.run([sys.executable, "-c", code], timeout=30, check=False)
            self.assertTrue(os.path.exists(path + ".inprogress"))

            w = MboxWriter(path)
            # 1 and 2 were fsynced; 3 was written but not committed, 4 is half
            self.assertTrue(w.has_message_id("<2@x>"))
            self.assertFalse(w.has_message_id("<3@x>"))
            self.assertFalse(w.has_message_id("<4@x>"))
            with w:
                for n in (3, 4):
                    self.assertTrue(w.write(b"Message-ID: <%d@x>\n\nbody %d" % (n, n)))
            self.assertFalse(os.path.exists(path + ".inprogress"))
            with open(path, "rb") as fh:
                data = fh.read()
            for n in (1, 2, 3, 4):
                self.assertEqual(data.count(b"Message-ID: <%d@x>\n" % n), 1, n)
            self.assertEqual(data.count(b"\nFrom "), 3)
            self.assertTrue(data.endswith(b"body 4\n\n"))

    def test_disk_full_mid_message_cut_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.mbox")
            with MboxWriter(path) as w:
                w.write(article(1))
                size = os.path.getsize(path)
                real = os.write

                def half_then_full(fd, data):
                    real(fd, bytes(data[: len(data) // 2]))
                    raise OSError(28, "No space left on device")

                with mock.patch("os.write", side_effect=half_then_full):
                    with self.assertRaises(OSError):
                        w.write(article(2))
                self.assertEqual(os.path.getsize(path), size)
                self.assertTrue(w.write(article(3)))
            self.assertEqual(index_message_ids(path), {"<1@x>", "<3@x>"})

    def test_foreign_mbox_ending_in_one_newline_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.mbox")
            foreign = b"From x@y Thu Dec  8 12:55:04 2016\nMessage-ID: <f@x>\n\nlast line\n"
            with open(path, "wb") as fh:
                fh.write(foreign)
            with MboxWriter(path) as w:
                w.write(article(1))
            with open(path, "rb") as fh:
                data = fh.read()
            self.assertTrue(data.startswith(foreign + b"\nFrom "))
            self.assertEqual(index_message_ids(path), {"<f@x>", "<1@x>"})


class TestLock(unittest.TestCase):
    """1.9: a second writer on the same mbox is refused, not interleaved."""

    def test_second_writer_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.mbox")
            with MboxWriter(path) as w1:
                w1.write(article(1))
                w2 = MboxWriter(path)
                with self.assertRaises(OSError):
                    w2.open()


class TestSigterm(unittest.TestCase):
    """3.1: SIGTERM while the main thread is inside MboxWriter.write."""

    def test_sigterm_during_write_exits_with_whole_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.mbox")
            code = textwrap.dedent(
                f"""
                import os, signal, sys
                sys.path.insert(0, {_ROOT!r})
                from usenet_archiver import cli, mboxout
                signal.signal(signal.SIGTERM, cli._handle_sigterm)
                w = mboxout.MboxWriter({path!r})
                w.open()
                cli._ACTIVE_WRITER = w
                w.write(b"Message-ID: <1@x>\\n\\nfirst")
                real = mboxout.format_mbox_message
                def format_then_signal(*a, **k):
                    os.kill(os.getpid(), signal.SIGTERM)  # arrives inside write()
                    return real(*a, **k)
                mboxout.format_mbox_message = format_then_signal
                try:
                    w.write(b"Message-ID: <2@x>\\n\\nsecond")
                except cli.TerminatedBySignal:
                    pass
                finally:
                    w.close()
                """
            )
            try:
                subprocess.run([sys.executable, "-c", code], timeout=10, check=False)
            except subprocess.TimeoutExpired:
                self.fail("hung after SIGTERM during a write")
            data = open(path, "rb").read()
            self.assertTrue(data.endswith(b"\n\n"), data[-60:])
            self.assertEqual(index_message_ids(path), {"<1@x>"})  # 2 was never written


class TestCompletedAfterFlush(unittest.TestCase):
    """4.1: the job is recorded only after the mbox is closed without error."""

    def test_failed_close_is_not_completed(self):
        from test_nntp import FakeNNTPServer
        from usenet_archiver.nntp import NNTPClient

        srv = FakeNNTPServer(articles={1: article(1), 2: article(2)})
        srv.start()
        self.addCleanup(srv.stop)
        kw = dict(server="127.0.0.1", port=srv.port, use_ssl=False, timeout=2)
        with tempfile.TemporaryDirectory() as tmp:
            done_log = os.path.join(tmp, "completed_newsgroups.log")
            client = NNTPClient(**kw)
            client.connect()
            self.addCleanup(client.close)
            disk_full = OSError(28, "No space left on device")
            with mock.patch.object(MboxWriter, "close", side_effect=disk_full):
                with self.assertRaises(OSError):
                    cli.pull_group(
                        client=client, group="misc.test", mbox_filename=os.path.join(tmp, "g.mbox"),
                        start_date=None, end_date=None, overview_chunk=100, dedup=True, plugins=[],
                        completed_log=done_log, logger=logging.getLogger("t"), conn_kwargs=kw,
                        connections=1, pipeline_depth=4,
                    )
            self.assertFalse(os.path.exists(done_log), "recorded as completed before the mbox was closed")


if __name__ == "__main__":
    unittest.main()
