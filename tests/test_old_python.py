#!/usr/bin/env python3
"""A clear message instead of a traceback on Python older than 3.9.

Found in the 2.0.1+gw2 release test on openSUSE Leap 15.6, whose python3 is
3.6: the zipapp stopped with "SyntaxError: future feature annotations is not
defined" from inside the package, not with what it needs.

Every entry point (the zipapp, python -m usenet_archiver, bin/usenet-archiver,
an installed console script) imports usenet_archiver/__init__.py first, so
that file checks the version, and it must compile on old Pythons itself.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INIT = os.path.join(_ROOT, "usenet_archiver", "__init__.py")
OLD_PYTHONS = [p for p in ("python3.8", "python3.7", "python3.6", "python3.5") if shutil.which(p)]


def _source():
    with open(_INIT, encoding="utf-8") as fh:
        return fh.read()


class TestPackageInit(unittest.TestCase):
    def run_init(self, version):
        code = compile(_source(), _INIT, "exec")
        with mock.patch.object(sys, "version_info", version):
            exec(code, {"__name__": "usenet_archiver", "__file__": _INIT})

    def test_old_python_gets_a_message(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_init((3, 6, 15, "final", 0))
        msg = str(cm.exception.code)
        self.assertIn("needs Python 3.9", msg)
        self.assertIn("3.6", msg)

    def test_3_8_is_too_old_as_well(self):
        with self.assertRaises(SystemExit):
            self.run_init((3, 8, 20, "final", 0))

    def test_3_9_and_newer_go_on(self):
        for version in ((3, 9, 0, "final", 0), (3, 14, 0, "final", 0)):
            self.run_init(version)

    def test_init_compiles_on_old_pythons(self):
        tree = ast.parse(_source(), feature_version=(3, 4))
        for node in ast.walk(tree):
            # from __future__ import annotations needs 3.7, f-strings 3.6
            self.assertFalse(isinstance(node, ast.ImportFrom) and node.module == "__future__",
                             "a __future__ import in __init__.py")
            self.assertNotIsInstance(node, ast.JoinedStr, "an f-string in __init__.py")


@unittest.skipUnless(OLD_PYTHONS and shutil.which("bash"), "needs bash and a python3.5 to 3.8 on PATH")
class TestOldInterpreter(unittest.TestCase):
    def test_zipapp_says_what_it_needs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "repo")
        for d in ("usenet_archiver", "scripts"):
            shutil.copytree(os.path.join(_ROOT, d), os.path.join(root, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        env = dict(os.environ, PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run(["bash", os.path.join(root, "scripts", "build_zipapp.sh")],
                           env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        app = os.path.join(root, "dist", "usenet-archiver")
        for py in OLD_PYTHONS:
            with self.subTest(python=py):
                q = subprocess.run([py, app, "-c", "--help"], capture_output=True, text=True, timeout=60)
                self.assertEqual(q.returncode, 1, q.stderr)
                self.assertIn("needs Python 3.9", q.stderr)
                self.assertNotIn("Traceback", q.stderr)


if __name__ == "__main__":
    unittest.main()
