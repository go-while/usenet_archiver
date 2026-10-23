#!/usr/bin/env python3
"""extras/: archive.sh, gz2mbox.py on a corrupt .gz, the report scripts.

Review 2, section F:
- archive.sh: an article in the last second of the month, or at 00:00:00 on
  the 1st, fell into neither month; an article expired between find and
  rsync aborted the script (rsync exit 23/24 under set -e); "Total number of
  files" counted directories; months 02 and 08 were refused.
- gz2mbox.py: a corrupt .gz raised zlib.error, which stopped the whole run.
- Big-8_Report.sh: unanchored prefixes (socal counted as soc).
- crosspost_report.sh: only the first line of a folded Newsgroups field.

The scripts run against temporary directories through SPOOL, WORK and DEST
(defaults: the real INN paths).
"""

from __future__ import annotations

import gzip
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRAS = os.path.join(_ROOT, "extras")


def _has_override(script, var):
    with open(os.path.join(EXTRAS, script)) as fh:
        return f"{var}=" in fh.read()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.spool = os.path.join(self.tmp, "spool")
        os.mkdir(self.spool)

    def article(self, rel, mtime=None, text=b"Newsgroups: comp.lang.c\nSubject: s\n\nbody\n"):
        path = os.path.join(self.spool, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(text)
        if mtime is not None:
            subprocess.run(["touch", "-d", mtime, path], check=True)
        return path

    def env(self, **extra):
        env = dict(os.environ, HOME=self.tmp, SPOOL=self.spool, PYTHONDONTWRITEBYTECODE="1")
        env.update(extra)
        return env


@unittest.skipUnless(shutil.which("bash") and shutil.which("zip") and shutil.which("rsync"), "needs bash, zip, rsync")
class TestArchive(Base):
    def archive(self, month, year="2026", path_prefix=None):
        if not _has_override("archive.sh", "SPOOL"):
            self.fail("archive.sh has no SPOOL/WORK/DEST override; not run against the real paths")
        work = os.path.join(self.tmp, "work")
        dest = os.path.join(self.tmp, "dest")
        env = self.env(WORK=work, DEST=dest)
        if path_prefix:
            env["PATH"] = path_prefix + os.pathsep + env["PATH"]
        p = subprocess.run(["bash", os.path.join(EXTRAS, "archive.sh")], input=f"{month}\n{year}\n",
                           env=env, capture_output=True, text=True, timeout=60)
        name = f"{int(month)}-{year}"
        zpath = os.path.join(dest, f"{name}.zip")
        members = []
        if os.path.exists(zpath):
            with zipfile.ZipFile(zpath) as z:
                members = sorted(n.rsplit("/articles/", 1)[1] for n in z.namelist() if not n.endswith("/"))
        return p, members, dest

    def month_edges(self):
        self.article("comp/lang/c/1", "2026-01-31 23:59:59.500")  # January, last second
        self.article("comp/lang/c/2", "2026-02-01 00:00:00")      # February, first instant
        self.article("comp/lang/c/3", "2026-01-01 00:00:00")      # January, first instant
        self.article("comp/lang/go/4", "2026-01-15 12:00:00")
        self.article("comp/lang/go/5", "2025-12-31 23:59:59.900")  # December

    @unittest.expectedFailure
    def test_month_edges(self):
        self.month_edges()
        p, members, _ = self.archive("1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(members, ["comp/lang/c/1", "comp/lang/c/3", "comp/lang/go/4"])
        p, members, _ = self.archive("02")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(members, ["comp/lang/c/2"])

    @unittest.expectedFailure
    def test_leading_zero_months(self):
        self.article("comp/lang/c/1", "2026-08-10 12:00:00")
        p, members, _ = self.archive("08")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(members, ["comp/lang/c/1"])

    @unittest.expectedFailure
    def test_file_count_and_vanished_article(self):
        self.month_edges()
        # a find that also lists an article expired before rsync reads it
        fake = os.path.join(self.tmp, "fakebin")
        os.mkdir(fake)
        real_find = shutil.which("find")
        with open(os.path.join(fake, "find"), "w") as fh:
            fh.write(f'#!/bin/sh\n"{real_find}" "$@"\nprintf "./comp/lang/c/expired\\0"\n')
        os.chmod(os.path.join(fake, "find"), 0o755)
        p, members, dest = self.archive("1", path_prefix=fake)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(members, ["comp/lang/c/1", "comp/lang/c/3", "comp/lang/go/4"])
        with open(os.path.join(dest, "archivelog.txt")) as fh:
            log = fh.read()
        self.assertIn("Total number of files =\n3\n", log)

    def test_bad_month_refused(self):
        if not _has_override("archive.sh", "SPOOL"):
            self.skipTest("old archive.sh: not run against the real paths")
        p, _members, _ = self.archive("13")
        self.assertNotEqual(p.returncode, 0)


class TestGz2mboxCorrupt(Base):
    @unittest.expectedFailure
    def test_corrupt_gz_does_not_stop_the_run(self):
        def art(n):
            return (b"Path: x!y\nFrom: a@b\nNewsgroups: t\nSubject: s%d\nMessage-ID: <%d@x>\n"
                    b"Date: Thu, 08 Dec 2016 12:55:04 +0000\n\nbody\n" % (n, n))

        blob = gzip.compress(art(1) + bytes(random.Random(1).randrange(256) for _ in range(3000)))
        bad = None
        for i in range(12, len(blob) - 8):  # a flipped byte that zlib, not the CRC check, rejects
            b = bytearray(blob)
            b[i] ^= 0x55
            try:
                gzip.decompress(bytes(b))
            except zlib.error:
                bad = bytes(b)
                break
            except Exception:
                pass
        self.assertIsNotNone(bad)
        src = os.path.join(self.tmp, "in")
        os.mkdir(src)
        with open(os.path.join(src, "a_bad.gz"), "wb") as fh:
            fh.write(bad)
        with open(os.path.join(src, "b_good.gz"), "wb") as fh:
            fh.write(gzip.compress(art(2)))
        out = os.path.join(self.tmp, "out.mbox")
        p = subprocess.run([sys.executable, os.path.join(EXTRAS, "gz2mbox.py"), src, "-o", out],
                           capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertEqual(p.returncode, 1, p.stderr)  # one file failed
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("a_bad.gz", p.stderr)
        with open(out, "rb") as fh:
            self.assertIn(b"<2@x>", fh.read())


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestReports(Base):
    def run_script(self, name):
        p = subprocess.run(["bash", os.path.join(EXTRAS, name)], env=self.env(),
                           capture_output=True, text=True, timeout=60)
        outs = [f for f in os.listdir(self.tmp) if f != "spool"]
        self.assertEqual(len(outs), 1, (outs, p.stderr))
        with open(os.path.join(self.tmp, outs[0])) as fh:
            return fh.read()

    @unittest.expectedFailure
    def test_big8_prefixes_anchored(self):
        for d in ("comp/lang/c", "soc/culture", "socal/x", "compx/y", "talk/z"):
            self.article(d + "/1")
        report = self.run_script("Big-8_Report.sh")
        dirs = {line.split(" ", 1)[1].split("/spool/", 1)[1] for line in report.splitlines()[1:]}
        self.assertEqual(dirs, {"comp", "comp/lang", "comp/lang/c", "soc", "soc/culture", "talk", "talk/z"})

    @unittest.expectedFailure
    def test_crosspost_folded_newsgroups(self):
        folded = self.article("a/1", text=b"Newsgroups: comp.a,\n comp.b,\n\tcomp.c\nSubject: s\n\nbody\n")
        plain = self.article("a/2", text=b"Subject: s\nNewsgroups: comp.a, comp.b\n\nbody\n")
        none = self.article("a/3", text=b"Subject: s\n\nNewsgroups: x,y,z in the body\n")
        report = self.run_script("crosspost_report.sh")
        counts = dict(line.rsplit(",", 1) for line in report.splitlines())
        self.assertEqual((counts[folded], counts[plain], counts[none]), ("3", "2", "0"))


if __name__ == "__main__":
    unittest.main()
