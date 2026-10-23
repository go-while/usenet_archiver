#!/usr/bin/env python3
"""TLS when Python's OpenSSL has no CA certificates where it looks.

Found by running the 2.0.1+gw1 AppImage on Fedora 45 and Arch: every TLS
connection failed with CERTIFICATE_VERIFY_FAILED, against public servers
too. The AppImage bundles Ubuntu's OpenSSL, which looks for CA certificates
in /usr/lib/ssl only, and neither distribution has that path. Adding a CA
to the system store did not help.

When neither the default CA file nor the default CA directory exists, and
the user has set neither SSL_CERT_FILE nor SSL_CERT_DIR, the client loads
the first readable system bundle from nntp.CA_BUNDLES.

tests/data/tls holds a test CA and a server certificate for 127.0.0.1 and
localhost, valid until 2126. The CA's key was not kept.
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usenet_archiver.nntp import NNTPClient, NNTPError  # noqa: E402

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tls")
TEST_CA = os.path.join(_DATA, "ca.pem")
MISSING = "/nonexistent/usenet_archiver-test/ca-bundle.crt"

# What ssl.get_default_verify_paths() says for the AppImage on Fedora or
# Arch: neither /usr/lib/ssl/cert.pem nor /usr/lib/ssl/certs exists.
NO_DEFAULT_PATHS = ssl.DefaultVerifyPaths(
    None, None, "SSL_CERT_FILE", "/usr/lib/ssl/cert.pem", "SSL_CERT_DIR", "/usr/lib/ssl/certs"
)


class TLSServer:
    """NNTPS on 127.0.0.1 with the test certificate: greeting, MODE READER,
    CAPABILITIES and QUIT, one connection at a time."""

    def __init__(self, test: unittest.TestCase):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(os.path.join(_DATA, "server.pem"), os.path.join(_DATA, "server.key"))
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(0.1)  # accept polls, so close() is seen at once
        self.port = self.sock.getsockname()[1]
        self.handshakes = 0
        self.stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        test.addCleanup(self.close)

    def close(self):
        self.stop = True
        self.sock.close()
        self.thread.join(5)

    def _serve(self):
        while not self.stop:
            try:
                raw, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with raw:
                raw.settimeout(10)
                try:
                    conn = self.ctx.wrap_socket(raw, server_side=True)
                except (ssl.SSLError, OSError):
                    continue  # the client refused the certificate
                self.handshakes += 1
                with conn:
                    self._session(conn)

    @staticmethod
    def _session(conn):
        replies = {
            b"MODE READER": b"200 reader\r\n",
            b"CAPABILITIES": b"101 capabilities\r\nVERSION 2\r\nREADER\r\n.\r\n",
            b"QUIT": b"205 bye\r\n",
        }
        try:
            conn.sendall(b"200 usenet_archiver TLS test server\r\n")
            f = conn.makefile("rb")
            for line in f:
                cmd = line.rstrip(b"\r\n").upper()
                conn.sendall(replies.get(cmd, b"500 what?\r\n"))
                if cmd == b"QUIT":
                    return
        except (OSError, ssl.SSLError):
            return


class Base(unittest.TestCase):
    def setUp(self):
        self.server = TLSServer(self)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SSL_CERT_FILE", None)
        os.environ.pop("SSL_CERT_DIR", None)

    def defaults(self, paths):
        p = mock.patch("ssl.get_default_verify_paths", return_value=paths)
        p.start()
        self.addCleanup(p.stop)

    def bundles(self, *paths):
        p = mock.patch("usenet_archiver.nntp.CA_BUNDLES", tuple(paths), create=True)
        p.start()
        self.addCleanup(p.stop)

    def connect(self) -> NNTPClient:
        c = NNTPClient("127.0.0.1", self.server.port, timeout=5, use_ssl=True)
        self.addCleanup(c.close)
        c.connect()
        return c

    def assertRefused(self):
        with self.assertRaises(NNTPError) as cm:
            self.connect()
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(cm.exception))
        self.assertEqual(self.server.handshakes, 0)


class TestSystemBundleFallback(Base):
    def test_first_existing_bundle_is_loaded(self):
        self.defaults(NO_DEFAULT_PATHS)
        self.bundles(MISSING, TEST_CA)
        c = self.connect()
        self.assertIn("READER", c.capabilities)
        self.assertEqual(self.server.handshakes, 1)

    def test_unreadable_bundle_is_skipped(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        junk = os.path.join(tmp.name, "junk.pem")
        with open(junk, "w") as fh:
            fh.write("-----BEGIN CERTIFICATE-----\nnot a certificate\n-----END CERTIFICATE-----\n")
        self.defaults(NO_DEFAULT_PATHS)
        self.bundles(junk, TEST_CA)
        self.assertIn("READER", self.connect().capabilities)

    def test_no_bundle_found_still_verifies(self):
        self.defaults(NO_DEFAULT_PATHS)
        self.bundles(MISSING)
        self.assertRefused()

    def test_ssl_cert_file_is_the_users_choice(self):
        # SSL_CERT_FILE names a missing file: no fallback, the user decided.
        os.environ["SSL_CERT_FILE"] = MISSING
        self.defaults(NO_DEFAULT_PATHS)
        self.bundles(TEST_CA)
        self.assertRefused()

    def test_ssl_cert_dir_is_the_users_choice(self):
        os.environ["SSL_CERT_DIR"] = os.path.dirname(MISSING)
        self.defaults(NO_DEFAULT_PATHS)
        self.bundles(TEST_CA)
        self.assertRefused()

    def test_working_defaults_are_left_alone(self):
        # OpenSSL has a CA file of its own: the system bundles are not added.
        self.defaults(ssl.DefaultVerifyPaths(
            os.path.join(_DATA, "server.pem"), None, "SSL_CERT_FILE", "", "SSL_CERT_DIR", ""))
        self.bundles(TEST_CA)
        self.assertRefused()



if __name__ == "__main__":
    unittest.main()
