#!/usr/bin/env python3
"""SIGTERM while connecting stops the run with exit 4 (review 2, A1).

The CLI runs in a subprocess: the signal must reach a real process with the
CLI's own handler, not the test runner.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.dirname(os.path.abspath(__file__))
for p in (_ROOT, _TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

from test_nntp import FakeNNTPServer  # noqa: E402
from usenet_archiver.cli import TerminatedBySignal  # noqa: E402


def _article(n: int) -> bytes:
    return (b"From: a@example.com\nNewsgroups: misc.test\nSubject: s%d\n"
            b"Date: Wed, 15 Jan 2020 12:00:00 +0000\nMessage-ID: <id%d@ex.com>\n\nbody\n" % (n, n))


class TestSigtermWhileConnecting(unittest.TestCase):
    def _run(self, stalled_command: str):
        srv = FakeNNTPServer(articles={1: _article(1), 2: _article(2)})
        srv.start()
        self.addCleanup(srv.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=_ROOT, HOME=tmp.name)
        env.pop("NNTPSERVER", None)
        cmd = [sys.executable, "-m", "usenet_archiver", "-c", "pull", "-s", f"127.0.0.1:{srv.port}",
               "--no-ssl", "--no-netrc", "-C", "1", "-g", "g.one", "-g", "g.two"]
        # archive both groups first: on the run that is stopped they have
        # nothing new, and "nothing to fetch" used to record them regardless
        first = subprocess.run(cmd, cwd=tmp.name, env=env, capture_output=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)
        os.remove(os.path.join(tmp.name, "completed_newsgroups.log"))
        srv.stalls = {stalled_command: [3.0, 1]}  # the next connection waits 3 s for the reply
        p = subprocess.Popen(cmd, cwd=tmp.name, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        t0 = time.monotonic()
        while srv.open_conns == 0 and time.monotonic() - t0 < 10:
            time.sleep(0.01)
        time.sleep(0.5)  # now waiting for the stalled reply
        p.send_signal(signal.SIGTERM)
        _out, err = p.communicate(timeout=60)
        log = os.path.join(tmp.name, "completed_newsgroups.log")
        recorded = open(log).read().split() if os.path.exists(log) else []
        return p.returncode, recorded, err.decode(errors="replace")

    def test_during_capabilities(self):
        rc, recorded, err = self._run("CAPABILITIES")
        self.assertEqual(rc, 4, err)
        self.assertEqual(recorded, [], "jobs recorded as completed after SIGTERM")

    def test_during_mode_reader(self):
        rc, recorded, err = self._run("MODE READER")
        self.assertEqual(rc, 4, err)
        self.assertEqual(recorded, [])

    def test_not_an_exception(self):
        # "except Exception" blocks (connect, the capabilities probe) must not catch it
        self.assertFalse(issubclass(TerminatedBySignal, Exception))



if __name__ == "__main__":
    unittest.main()
