#!/usr/bin/env python3
"""Build scripts and the release workflow.

Review 2, section F:
- build_zipapp.sh broke on a checkout path containing "'", and its
  ${ROOT@Q} needs bash 4.4 (macOS has 3.2).
- build_appimage.sh bundled whatever dist/usenet-archiver was there, stale
  or not, and downloaded linuxdeploy/appimagetool "continuous" builds
  unchecked, in a job that could write releases.
- release.yml: the smoke tests piped into head without pipefail, so they
  could never fail; a release did not wait for the tests; nothing checked
  that the tag matches __version__.

The workflow and the AppImage build cannot run here; those checks read
the files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestZipapp(unittest.TestCase):
    def test_build_in_a_path_with_a_quote(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "o'brien \"x\"", "repo")
        os.makedirs(root)
        for d in ("usenet_archiver", "scripts"):
            shutil.copytree(os.path.join(_ROOT, d), os.path.join(root, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        env = dict(os.environ, PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run(["bash", os.path.join(root, "scripts", "build_zipapp.sh")],
                           env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        app = os.path.join(root, "dist", "usenet-archiver")
        q = subprocess.run([sys.executable, app, "-c", "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertIn("pull", q.stdout)

    def test_no_bash44_expansions(self):
        for script in ("scripts/build_zipapp.sh", "scripts/build_appimage.sh"):
            self.assertNotIn("@Q}", _read(script), script)


class TestAppImageScript(unittest.TestCase):
    def test_zipapp_always_rebuilt(self):
        self.assertNotIn('if [[ ! -x "${ROOT}/dist/usenet-archiver" ]]', _read("scripts/build_appimage.sh"))

    def test_tools_pinned_and_checked(self):
        s = _read("scripts/build_appimage.sh")
        self.assertNotIn("/download/continuous/", s)
        self.assertIn("sha256sum", s)


class TestReleaseWorkflow(unittest.TestCase):
    def setUp(self):
        self.yml = _read(".github/workflows/release.yml")

    def test_steps_run_with_pipefail(self):
        # "shell: bash" runs steps as bash -eo pipefail; the default is bash -e
        self.assertIn("defaults:\n  run:\n    shell: bash", self.yml)

    def test_release_waits_for_the_tests(self):
        self.assertIn("uses: ./.github/workflows/test.yml", self.yml)
        self.assertIn("workflow_call:", _read(".github/workflows/test.yml"))
        self.assertIn("needs: [tests, zipapp, appimage]", self.yml)

    def test_tag_checked_against_version(self):
        self.assertIn("__version__", self.yml)
        self.assertIn("GITHUB_REF_NAME", self.yml)

    def test_write_permission_only_for_the_release_job(self):
        top = self.yml.split("\njobs:", 1)[0]
        self.assertIn("contents: read", top)
        self.assertNotIn("contents: write", top)
        self.assertIn("contents: write", self.yml.split("\n  release:", 1)[1])


if __name__ == "__main__":
    unittest.main()
