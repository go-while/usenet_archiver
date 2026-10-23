#!/usr/bin/env python3
"""__version__, pyproject.toml and the README changelog name the same version.

release.yml refuses a tag that is not v<__version__>; this keeps the other
two places in step with it.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from usenet_archiver import __version__  # noqa: E402


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TestVersion(unittest.TestCase):
    def test_pyproject(self):
        m = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M)
        self.assertEqual(m.group(1), __version__)

    def test_newest_changelog_entry(self):
        changelog = _read("README.md").split("\n## Changelog\n", 1)[1]
        m = re.search(r"^### (\S+) — \d{4}-\d{2}-\d{2}$", changelog, re.M)
        self.assertEqual(m.group(1), __version__)


if __name__ == "__main__":
    unittest.main()
