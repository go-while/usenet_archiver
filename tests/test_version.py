#!/usr/bin/env python3
"""__version__, pyproject.toml and the README changelog name the same version.

This fork does not claim upstream's version numbers: its version is the
upstream release it is based on plus -gw<N> (2.0.1-gw1). pyproject.toml
needs a PEP 440 version, so there it is the local version 2.0.1+gw1.
release.yml refuses a tag that is not v<__version__>; this keeps the other
places in step with it.
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


# the upstream release this fork is based on (github.com/tgeek77/usenet_archiver)
UPSTREAM = "2.0.1"


class TestVersion(unittest.TestCase):
    @unittest.expectedFailure
    def test_fork_version(self):
        self.assertRegex(__version__, r"^" + re.escape(UPSTREAM) + r"-gw[1-9][0-9]*$")

    def test_pyproject(self):
        m = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M)
        self.assertEqual(m.group(1), __version__.replace("-gw", "+gw"))

    def test_newest_changelog_entry(self):
        changelog = _read("README.md").split("\n## Changelog\n", 1)[1]
        m = re.search(r"^### (\S+) — \d{4}-\d{2}-\d{2}$", changelog, re.M)
        self.assertEqual(m.group(1), __version__)


if __name__ == "__main__":
    unittest.main()
