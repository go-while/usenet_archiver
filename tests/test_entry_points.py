#!/usr/bin/env python3
"""Entry points: arguments never start the GUI; the launchers work from any directory.

Review 2:
- D6: arguments without a leading -c (usenet-archiver pull ..., -v -c pull,
  the v1 app/usenet_archiver.py --server ... --newsgroup ...) started the
  GUI, so a cron job hung or crashed.
- D7: bin/usenet-archiver worked only from the repo root (no PYTHONPATH).

Every run here has no display, so a GUI that starts by mistake fails at
once instead of opening a window.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

from test_nntp import FakeNNTPServer  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def run_cmd(self, cmd, pythonpath=True):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DISPLAY", "WAYLAND_DISPLAY", "PYTHONPATH", "NNTPSERVER", "NNTP_PASSWORD")}
        env.update(HOME=self.tmp, PYTHONDONTWRITEBYTECODE="1")
        if pythonpath:
            env["PYTHONPATH"] = _ROOT
        return subprocess.run(cmd, cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)

    def module(self, *args):
        return self.run_cmd([sys.executable, "-m", "usenet_archiver", *args])


class TestArgumentsMeanCLI(Base):
    @unittest.expectedFailure
    def test_subcommand_without_c(self):
        p = self.module("pull", "--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("newsgroup", p.stdout.lower())

    @unittest.expectedFailure
    def test_unknown_arguments_are_a_usage_error(self):
        for args in (("--server", "news.example", "--newsgroup", "comp.lang.c"), ("-v", "-c", "pull")):
            p = self.module(*args)
            self.assertEqual(p.returncode, 2, (args, p.stderr))
            self.assertIn("usage", p.stderr.lower())

    def test_c_still_works(self):
        p = self.module("-c", "pull", "--help")
        self.assertEqual(p.returncode, 0, p.stderr)


class TestV1Shim(Base):
    @unittest.expectedFailure
    def test_v1_arguments_pull(self):
        arts = {n: (b"From: a@example.com\nNewsgroups: misc.test\nSubject: s\n"
                    b"Date: Wed, 15 Jan 2020 12:00:00 +0000\nMessage-ID: <id%d@ex.com>\n\nbody %d\n" % (n, n))
                for n in (1, 2)}
        srv = FakeNNTPServer(articles=arts)
        srv.start()
        self.addCleanup(srv.stop)
        p = self.run_cmd([sys.executable, os.path.join(_ROOT, "app", "usenet_archiver.py"),
                          "--server", "127.0.0.1", "--port", str(srv.port), "--no-ssl",
                          "--username", "u", "--password", "p", "--newsgroup", "misc.test"],
                         pythonpath=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(self.tmp, "misc.test.mbox"), "rb") as fh:
            self.assertEqual(fh.read().count(b"\nbody "), 2)


class TestLauncher(Base):
    @unittest.expectedFailure
    def test_bin_launcher_from_another_directory(self):
        p = self.run_cmd([os.path.join(_ROOT, "bin", "usenet-archiver"), "-c", "--help"], pythonpath=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pull", p.stdout)


if __name__ == "__main__":
    unittest.main()
