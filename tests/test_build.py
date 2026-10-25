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

The 2.0.1+gw1 AppImage, run on Fedora, Arch, openSUSE and Debian images:
- copy_ld_deps copied symlinks (cp -a): libexpat.so.1 -> libexpat.so.1.8.7,
  and the file itself was not in the AppImage. It did not start where the
  system has no libexpat (the bare Fedora, openSUSE and Debian images).
- the stdlib copy kept Debian's sitecustomize.py, a link to
  /etc/python3.10/sitecustomize.py outside the AppImage.
- release.yml never ran the AppImage outside Ubuntu.

The 2.0.1+gw2 release test on 18 distributions:
- the AppImage, built on Ubuntu 22.04, needs glibc 2.35 and did not start
  on AlmaLinux, Rocky and CentOS Stream 9 (glibc 2.34). It is now built in
  an AlmaLinux 9 container, whose layout differs from Debian's: the stdlib
  in /usr/lib64/python3.X, Tcl/Tk in /usr/share/tcl8.6 and tk8.6, and the
  libraries of the stdlib's extension modules (OpenSSL for _ssl) must come
  along, since an older system's would be too old for them.

The workflow and the AppImage build cannot run here; those checks read
the files.
"""

from __future__ import annotations

import os
import re
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

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_libraries_copied_as_files(self):
        m = re.search(r"^copy_ld_deps\(\) \{\n.*?^\}\n", _read("scripts/build_appimage.sh"), re.M | re.S)
        self.assertIsNotNone(m)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sys_lib = os.path.join(tmp.name, "system", "lib")
        os.makedirs(sys_lib)
        with open(os.path.join(sys_lib, "libfoo.so.1.2.3"), "w") as fh:
            fh.write("the library\n")
        os.symlink("libfoo.so.1.2.3", os.path.join(sys_lib, "libfoo.so.1"))
        with open(os.path.join(sys_lib, "libc.so.6"), "w") as fh:
            fh.write("libc\n")
        # A fake ldd: the binary needs libfoo.so.1 (a symlink, as most
        # sonames are) and libc (never bundled).
        bindir = os.path.join(tmp.name, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "ldd"), "w") as fh:
            fh.write("#!/bin/sh\n"
                     f"echo '\tlibfoo.so.1 => {sys_lib}/libfoo.so.1 (0x00007f0000000000)'\n"
                     f"echo '\tlibc.so.6 => {sys_lib}/libc.so.6 (0x00007f0000100000)'\n")
        os.chmod(os.path.join(bindir, "ldd"), 0o755)
        appdir = os.path.join(tmp.name, "AppDir")
        env = dict(os.environ, APPDIR=appdir, PATH=bindir + os.pathsep + os.environ["PATH"])
        p = subprocess.run(["bash", "-c", m.group(0) + 'copy_ld_deps "$1"', "bash", "/some/binary"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        lib = os.path.join(appdir, "usr", "lib")
        self.assertEqual(sorted(os.listdir(lib)), ["libfoo.so.1"])
        self.assertFalse(os.path.islink(os.path.join(lib, "libfoo.so.1")))
        with open(os.path.join(lib, "libfoo.so.1")) as fh:
            self.assertEqual(fh.read(), "the library\n")

    def test_stdlib_from_where_python_keeps_it(self):
        # /usr/lib/python3.X on Debian, /usr/lib64/python3.X on AlmaLinux:
        # Python's own sysconfig says where, and sys.platlibdir where it
        # looks for it under PYTHONHOME.
        s = _read("scripts/build_appimage.sh")
        self.assertFalse('"/usr/lib/python${PYVER}"' in s, "stdlib path hard-coded")
        self.assertTrue("sysconfig" in s and "platlibdir" in s, "stdlib path not from sysconfig")

    def test_libraries_of_every_stdlib_module_bundled(self):
        # Not only _tkinter's: _ssl needs AlmaLinux's OpenSSL 3.5, _crypt
        # libcrypt.so.2, which Ubuntu 22.04 does not have.
        s = _read("scripts/build_appimage.sh")
        self.assertTrue(re.search(r'for so in "\$\{APPDIR\}/usr/\$\{PLATLIB\}/python\$\{PYVER\}/lib-dynload"/\*\.so; do', s),
                        "no copy_ld_deps for every lib-dynload module")

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_apprun_finds_tcl_and_tk(self):
        runs = re.findall(r"""cat > "\$\{APPDIR\}/AppRun" << 'EOF'\n(.*?)\nEOF\n""",
                          _read("scripts/build_appimage.sh"), re.S)
        self.assertTrue(runs)
        layouts = {
            "debian": ("usr/share/tcltk/tcl8.6", "usr/share/tcltk/tk8.6"),
            "almalinux": ("usr/share/tcl8.6", "usr/share/tk8.6"),
        }
        for i, apprun in enumerate(runs):
            for name, (tcl, tk) in layouts.items():
                with self.subTest(apprun=i, layout=name):
                    tmp = tempfile.TemporaryDirectory()
                    self.addCleanup(tmp.cleanup)
                    appdir = os.path.join(tmp.name, "App Dir")
                    for d in (tcl, tk, "usr/bin"):
                        os.makedirs(os.path.join(appdir, d))
                    # the "interpreter" prints what AppRun set up for it
                    py = os.path.join(appdir, "usr", "bin", "python3")
                    with open(py, "w") as fh:
                        fh.write('#!/bin/sh\necho "TCL_LIBRARY=$TCL_LIBRARY"\necho "TK_LIBRARY=$TK_LIBRARY"\n')
                    run = os.path.join(appdir, "AppRun")
                    with open(run, "w") as fh:
                        fh.write(apprun + "\n")
                    os.chmod(py, 0o755)
                    os.chmod(run, 0o755)
                    env = {k: v for k, v in os.environ.items() if k not in ("TCL_LIBRARY", "TK_LIBRARY")}
                    p = subprocess.run([run], env=env, capture_output=True, text=True, timeout=60)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertIn("TCL_LIBRARY=" + os.path.join(appdir, tcl) + "\n", p.stdout)
                    self.assertIn("TK_LIBRARY=" + os.path.join(appdir, tk) + "\n", p.stdout)

    def test_stdlib_copy_keeps_no_links_out_of_the_appimage(self):
        m = re.search(r"<<'PY'\n(.*?)\nPY\n", _read("scripts/build_appimage.sh"), re.S)
        self.assertIsNotNone(m)
        self.assertIn("copytree", m.group(1))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "python3.10")
        os.makedirs(os.path.join(src, "encodings"))
        outside = os.path.join(tmp.name, "etc-sitecustomize.py")
        for path in (os.path.join(src, "os.py"), os.path.join(src, "encodings", "__init__.py"), outside):
            with open(path, "w") as fh:
                fh.write("# x\n")
        os.symlink("os.py", os.path.join(src, "os_link.py"))  # inside: kept
        os.symlink(outside, os.path.join(src, "sitecustomize.py"))  # Debian's: dropped
        os.symlink("/nonexistent/usenet_archiver-test.py", os.path.join(src, "dangling.py"))
        dst = os.path.join(tmp.name, "AppDir", "usr", "lib", "python3.10")
        p = subprocess.run([sys.executable, "-", src, dst], input=m.group(1),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(os.listdir(dst)), ["encodings", "os.py", "os_link.py"])
        self.assertEqual(os.path.realpath(os.path.join(dst, "os_link.py")), os.path.join(dst, "os.py"))


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

    def appimage_job(self):
        return self.yml.split("\n  appimage:", 1)[1].split("\n  release:", 1)[0]

    def test_appimage_run_on_other_distributions(self):
        job = self.appimage_job()
        self.assertIn("scripts/check_appimage_tls.sh", job)
        script = _read("scripts/check_appimage_tls.sh")
        for image in ("fedora", "archlinux", "opensuse", "debian", "almalinux:9", "ubuntu:22.04"):
            self.assertTrue(image in script, image + " missing in check_appimage_tls.sh")

    def test_appimage_built_on_almalinux_9(self):
        # glibc 2.34: the oldest of the distributions it is checked on
        job = self.appimage_job()
        self.assertTrue("./scripts/build_appimage_in_container.sh" in job, "not built in a container")
        self.assertFalse("./scripts/build_appimage.sh" in job, "built on the runner itself")
        self.assertIn("docker.io/library/almalinux:9", _read("scripts/build_appimage_in_container.sh"))


if __name__ == "__main__":
    unittest.main()
