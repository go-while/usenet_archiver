#!/usr/bin/env python3
"""The GUI's pull queue, driven headless with a stand-in tkinter (tests/faketk).

Review 2:
- C8: one failing group (411, a lock, a timeout) ended the whole queue; the
  CLI goes on with the next group.
- E: Stop was not seen during the overview scan or before the fetch (the
  mbox and -C connections were still created); after an error the progress
  bar kept spinning and the log handler stayed attached; a relative
  password file was read after chdir to the output directory; closing the
  window ended a pull silently; the groups-file import dropped a
  group>file override, and a file that is not UTF-8 failed with no message.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import tempfile
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from test_nntp import FakeNNTPServer  # noqa: E402
from usenet_archiver import cli  # noqa: E402

_saved_modules = {}


def setUpModule():
    global tk, messagebox, filedialog, gui
    for name in list(sys.modules):
        if name == "tkinter" or name.startswith("tkinter.") or name == "usenet_archiver.gui":
            _saved_modules[name] = sys.modules.pop(name)
    sys.path.insert(0, os.path.join(_HERE, "faketk"))
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox

        from usenet_archiver import gui
    finally:
        sys.path.remove(os.path.join(_HERE, "faketk"))


def tearDownModule():
    for name in list(sys.modules):
        if name == "tkinter" or name.startswith("tkinter.") or name == "usenet_archiver.gui":
            del sys.modules[name]
    sys.modules.update(_saved_modules)


def _art(n, group="misc.test"):
    return (b"From: a@example.com\nNewsgroups: %s\nSubject: s\n"
            b"Date: Wed, 15 Jan 2020 12:00:00 +0000\nMessage-ID: <%s.%d@ex.com>\n\nbody %d\n"
            % (group.encode(), group.encode(), n, n))


class Server(FakeNNTPServer):
    """Records commands; answers 411 for groups in `missing`."""

    def __init__(self, missing=(), **kw):
        super().__init__(**kw)
        self.missing = set(missing)
        self.commands = []

    def _readline(self, conn, buf):
        line = super()._readline(conn, buf)
        self.commands.append(line.decode("utf-8", "replace"))
        return line

    def _send(self, conn, data):
        if data.startswith(b"211") and data.split()[4].decode() in self.missing:
            data = b"411 no such group\r\n"
        super()._send(conn, data)

    def sent(self, prefix):
        return [c for c in self.commands if c.upper().startswith(prefix)]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.out = os.path.join(self.tmp, "out")
        os.mkdir(self.out)
        old_cfg = gui.CONFIG_PATH
        gui.CONFIG_PATH = gui.Path(self.tmp) / "rc"
        self.addCleanup(setattr, gui, "CONFIG_PATH", old_cfg)
        messagebox.SHOWN.clear()
        messagebox.ANSWER[0] = True
        filedialog.NEXT[0] = ""
        self.addCleanup(self._reset_cli)
        root_logger = logging.getLogger()
        handlers = list(root_logger.handlers)
        self.addCleanup(lambda: root_logger.handlers.__setitem__(slice(None), handlers))

    @staticmethod
    def _reset_cli():
        cli._TERMINATED = False
        cli._STOP_EVENT = None

    def serve(self, srv):
        srv.start()
        self.addCleanup(srv.stop)
        return srv

    def make(self, port, groups, **fields):
        root = tk.Tk()
        g = gui.UsenetArchiverGUI(root)
        g.server_var.set("127.0.0.1")
        g.port_var.set(str(port))
        g.ssl_var.set(0)
        g.output_var.set(self.out)
        g.connections_var.set("1")
        g.timeout_var.set("2")
        for name, value in fields.items():
            getattr(g, name).set(value)
        g._set_groups_list(groups)
        return root, g

    def run_pull(self, root, g):
        g.start_pull()
        root.pump_until(lambda: g._active_tasks == 0)

    def mbox_bodies(self, name):
        with open(os.path.join(self.out, name), "rb") as fh:
            return fh.read().count(b"\nbody ")


class TestQueue(Base):
    @unittest.expectedFailure
    def test_failing_group_does_not_end_the_queue(self):
        srv = self.serve(Server(missing={"no.such.group"}, articles={n: _art(n) for n in (1, 2, 3)}))
        root, g = self.make(srv.port, ["no.such.group", "misc.test"])
        self.run_pull(root, g)
        self.assertEqual(self.mbox_bodies("misc.test.mbox"), 3)
        self.assertEqual(g.status_var.get(), "Incomplete")
        self.assertTrue(any("no.such.group" in m for _k, _t, m in messagebox.SHOWN))

    @unittest.expectedFailure
    def test_relative_password_file_is_read_where_the_gui_started(self):
        srv = self.serve(Server(articles={1: _art(1)}))
        with open(os.path.join(self.tmp, "pw.txt"), "w") as fh:
            fh.write("secret\n")
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        root, g = self.make(srv.port, ["misc.test"], user_var="u", password_file_var="pw.txt")
        self.run_pull(root, g)
        self.assertEqual(messagebox.SHOWN, [("info", "Done", f"Wrote misc.test.mbox\nin {self.out}")])
        self.assertIn("AUTHINFO PASS secret", srv.commands)

    @unittest.expectedFailure
    def test_group_with_file_override(self):
        srv = self.serve(Server(articles={n: _art(n) for n in (1, 2)}))
        path = os.path.join(self.tmp, "groups.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# list\nmisc.test>chosen.mbox\n")
        root, g = self.make(srv.port, [])
        filedialog.NEXT[0] = path
        g._import_groups_file()
        self.assertEqual(g._groups_from_listbox(), ["misc.test>chosen.mbox"])
        self.run_pull(root, g)
        self.assertEqual(self.mbox_bodies("chosen.mbox"), 2)

    @unittest.expectedFailure
    def test_import_of_a_file_that_is_not_utf8_says_so(self):
        path = os.path.join(self.tmp, "groups.txt")
        with open(path, "wb") as fh:
            fh.write(b"de.alt.b\xfccher\n")
        root, g = self.make(1, [])
        filedialog.NEXT[0] = path
        g._import_groups_file()
        self.assertEqual([k for k, _t, _m in messagebox.SHOWN], ["error"])


class TestAfterAnError(Base):
    @unittest.expectedFailure
    def test_progress_stopped_and_log_handler_removed(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens there: connect fails
        root, g = self.make(port, ["misc.test"])
        self.run_pull(root, g)
        self.assertEqual(g.status_var.get(), "Pull failed")
        names = [c[0] for c in g.progress.calls if c[0] in ("start", "stop")]
        self.assertEqual(names, ["start", "stop"])
        self.assertFalse(any(isinstance(h, gui.QueueLogHandler) for h in logging.getLogger().handlers))


class TestStop(Base):
    @unittest.expectedFailure
    def test_stop_during_the_overview_scan(self):
        srv = self.serve(Server(articles={n: _art(n) for n in range(1, 21)}, stalls={"OVER 1-20": [0.6, 1]}))
        root, g = self.make(srv.port, ["misc.test"], connections_var="4")
        threading.Timer(0.3, g.stop_pull).start()
        self.run_pull(root, g)
        self.assertEqual(g.status_var.get(), "Cancelled")
        self.assertFalse(os.path.exists(os.path.join(self.out, "misc.test.mbox")))
        self.assertEqual(srv.sent("ARTICLE"), [])
        self.assertEqual(srv.sent("GROUP"), ["GROUP misc.test"])  # no fetch connection

    @unittest.expectedFailure
    def test_scan_checks_between_chunks(self):
        from usenet_archiver.overview import find_articles_in_date_range

        class Client:
            calls = 0

            def over(self, a, b):
                Client.calls += 1
                return [b"%d\ts\tf\t\t<%d@x>\t\t1\t1" % (n, n) for n in range(a, b + 1)], "OVER", "224"

        class Stopped(Exception):
            pass

        def check():
            if Client.calls >= 2:
                raise Stopped

        with self.assertRaises(Stopped):
            find_articles_in_date_range(Client(), 1, 100, None, None, chunk_size=10, check=check)
        self.assertEqual(Client.calls, 2)


class TestClose(Base):
    def test_close_when_idle_destroys(self):
        root, g = self.make(1, ["misc.test"])
        handler = root.protocols.get("WM_DELETE_WINDOW")
        if handler is None:  # not handled: the window manager just destroys it
            root.destroy()
        else:
            handler()
        self.assertTrue(root.destroyed)

    @unittest.expectedFailure
    def test_close_during_a_pull_stops_it_first(self):
        srv = self.serve(Server(articles={n: _art(n) for n in range(1, 21)}, stalls={"OVER 1-20": [0.6, 1]}))
        root, g = self.make(srv.port, ["misc.test"])
        g.start_pull()
        root.protocols["WM_DELETE_WINDOW"]()
        self.assertFalse(root.destroyed)  # waits for the pull to stop
        self.assertEqual([k for k, _t, _m in messagebox.SHOWN], ["ask"])
        root.pump_until(lambda: g._active_tasks == 0)
        self.assertTrue(root.destroyed)
        self.assertFalse(os.path.exists(os.path.join(self.out, "misc.test.mbox")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "completed_newsgroups.log")))

    @unittest.expectedFailure
    def test_close_cancelled_keeps_the_pull(self):
        srv = self.serve(Server(articles={n: _art(n) for n in (1, 2)}, stalls={"OVER 1-2": [0.3, 1]}))
        root, g = self.make(srv.port, ["misc.test"])
        messagebox.ANSWER[0] = False
        g.start_pull()
        root.protocols["WM_DELETE_WINDOW"]()
        root.pump_until(lambda: g._active_tasks == 0)
        self.assertFalse(root.destroyed)
        self.assertEqual(self.mbox_bodies("misc.test.mbox"), 2)


if __name__ == "__main__":
    unittest.main()
