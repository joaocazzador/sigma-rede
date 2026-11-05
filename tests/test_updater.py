"""Atualização automática (sigma/updater.py): baixa do nó, confere o SHA-256, troca o arquivo e reinicia; nunca
troca um arquivo que não confere nem atualiza no meio de um pedido."""
import hashlib
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sigma import updater  # noqa: E402

NEW = b"PK sigma novo" * 1000


class Files(BaseHTTPRequestHandler):
    files = {}

    def do_GET(self):
        body = self.files.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *a):
        pass


class TestUpdater(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Files)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.node = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sigma.pyz")
        with open(self.path, "wb") as f:
            f.write(b"velho")
        self.publish(hashlib.sha256(NEW).hexdigest())

    def tearDown(self):
        self.srv.shutdown()

    def publish(self, sha):
        Files.files = {"/downloads/sigma.pyz": NEW,
                       "/downloads/SHA256SUMS.txt": f"{'0' * 64}  Sigma.exe\n{sha}  sigma.pyz\n".encode()}

    def make(self, version="9.9.9", busy=False):
        self.restarts = []
        u = updater.AutoUpdater(lambda: self.node, lambda: version, lambda: busy, lambda *a: None,
                                self.restarts.append, argv=["--headless"])
        u.kind, u.path, u.enabled = "pyz", self.path, True
        return u

    def content(self):
        with open(self.path, "rb") as f:
            return f.read()

    def test_downloads_checks_and_swaps(self):
        u = self.make()
        with mock.patch.object(updater, "supervised", return_value=True):
            self.assertTrue(u.tick())
        self.assertEqual(self.content(), NEW)
        self.assertEqual(self.restarts, [None])              # supervisionado: só sai
        updater._remove(self.path + ".old")

    def test_relaunches_when_not_supervised(self):
        u = self.make()
        with mock.patch.object(updater, "supervised", return_value=False), \
                mock.patch.object(updater, "relaunch") as rel:
            u.tick()
            self.restarts[0]()
        rel.assert_called_once_with("pyz", self.path, ["--headless"])

    def test_wrong_sha_keeps_the_old_file(self):
        self.publish("f" * 64)
        u = self.make()
        self.assertFalse(u.tick())
        self.assertEqual(self.content(), b"velho")
        self.assertFalse(os.path.exists(self.path + ".new"))
        self.assertEqual(self.restarts, [])

    def test_waits_while_busy_or_up_to_date(self):
        self.assertFalse(self.make(busy=True).tick())
        self.assertFalse(self.make(version=None).tick())
        self.assertEqual(self.content(), b"velho")

    def test_source_checkout_never_updates(self):
        kind, path = updater.current_file()
        self.assertEqual((kind, path), (None, None))         # os testes rodam do código-fonte
        u = updater.AutoUpdater(lambda: self.node, lambda: "9.9.9", lambda: False, lambda *a: None, print)
        self.assertFalse(u.enabled)

    def test_same_file_is_not_swapped(self):
        with open(self.path, "wb") as f:
            f.write(NEW)
        u = self.make()
        self.assertFalse(u.tick())
        self.assertEqual(self.restarts, [])
        self.assertFalse(os.path.exists(self.path + ".new"))

    def test_backoff_after_failure(self):
        self.publish("f" * 64)
        u = self.make()
        u.tick()
        self.publish(hashlib.sha256(NEW).hexdigest())
        self.assertFalse(u.tick())                            # só tenta de novo depois de CHECK_EVERY


if __name__ == "__main__":
    unittest.main()
