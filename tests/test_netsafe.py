"""Testes das conexões de saída (sigma/netsafe.py) e do checksum de endereço (crypto.checksum_ok).

Sobe servidores só em 127.0.0.1 (portas livres escolhidas pelo sistema) e nunca sai da máquina.
"""
import http.server
import io
import threading
import unittest
import urllib.error
import urllib.request

from sigma import crypto, netsafe


class _Redirect(http.server.BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        if self.path == "/ok":
            body = b"conteudo"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header("Location", self.target)
        self.end_headers()

    def log_message(self, *a):
        pass


class NetsafeTest(unittest.TestCase):
    def test_blocked(self):
        for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.5", "169.254.169.254", "100.64.0.1",
                   "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1", "lixo"):
            self.assertTrue(netsafe.blocked(ip), ip)
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertFalse(netsafe.blocked(ip), ip)

    def test_is_internal(self):
        self.assertTrue(netsafe.is_internal("127.0.0.1"))
        self.assertTrue(netsafe.is_internal("localhost"))
        self.assertFalse(netsafe.is_internal(""))
        self.assertFalse(netsafe.is_internal("nome-que-nao-existe.invalid"))

    def test_safe_opener_refuses_internal_and_redirects_into_it(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
        port = srv.server_address[1]
        _Redirect.target = f"http://127.0.0.1:{port}/ok"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises((urllib.error.URLError, OSError)):
                netsafe.safe_opener().open(f"http://127.0.0.1:{port}/ok", timeout=5)
            # rede de teste local: liberado, e o redirect é seguido normalmente
            r = netsafe.safe_opener(allow_internal=True).open(f"http://127.0.0.1:{port}/redir", timeout=5)
            self.assertEqual(r.read(), b"conteudo")
            # redirect para file:// nunca
            _Redirect.target = "file:///etc/passwd"
            with self.assertRaises(urllib.error.HTTPError) as cm:
                netsafe.safe_opener(allow_internal=True).open(f"http://127.0.0.1:{port}/redir", timeout=5)
            cm.exception.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_read_capped(self):
        self.assertEqual(netsafe.read_capped(io.BytesIO(b"x" * 10), 10), b"x" * 10)
        self.assertEqual(netsafe.read_capped(io.BytesIO(b""), 0), b"")
        with self.assertRaises(ValueError):
            netsafe.read_capped(io.BytesIO(b"x" * 11), 10)
        with self.assertRaises(ValueError):
            netsafe.read_capped(io.BytesIO(b"x" * 200_000), 100_000)

    def test_ssl_context_is_cached(self):
        self.assertIs(netsafe.ssl_context(), netsafe.ssl_context())
        self.assertTrue(netsafe.cert_problem("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        self.assertFalse(netsafe.cert_problem("timed out"))


class ChecksumTest(unittest.TestCase):
    VECTORS = ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
               "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB", "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb")

    def test_eip55_vectors(self):
        for a in self.VECTORS:
            self.assertTrue(crypto.checksum_ok(a), a)
            self.assertTrue(crypto.checksum_ok("sig" + a[2:]), a)
            self.assertEqual(crypto.to_checksum(a.lower()), a)
            self.assertTrue(crypto.checksum_ok(a.lower()))
            self.assertTrue(crypto.checksum_ok("0x" + a[2:].upper()))

    def test_one_wrong_character(self):
        a = self.VECTORS[0]
        bad = a[:5] + ("b" if a[5] != "b" else "c") + a[6:]
        self.assertFalse(crypto.checksum_ok(bad))
        i = next(i for i, c in enumerate(a) if i >= 2 and c.isalpha())
        swapped = a[:i] + a[i].swapcase() + a[i + 1:]   # mesma letra com a caixa trocada
        self.assertFalse(crypto.checksum_ok(swapped))

    def test_garbage(self):
        for a in (None, 123, "", "0x123", "sig" + "g" * 40, "1x" + "a" * 40):
            self.assertFalse(crypto.checksum_ok(a))

    def test_body_is_only_hex(self):
        """O int(x, 16) aceitaria sinal, "_" e espaços no corpo: "0x+aaa…" virava o endereço "sig+aaa…"."""
        for a in ("0x+" + "a" * 39, "0x-" + "a" * 39, "sig_" + "a" * 39, "sig " + "a" * 39, "0x" + "a" * 39 + " ",
                  "0x" + "a_" * 20, "sig" + "a" * 19 + "١" + "a" * 20):
            self.assertFalse(crypto.checksum_ok(a), a)
            with self.assertRaises(ValueError):
                crypto.norm_address(a)
        self.assertEqual(crypto.norm_address("0X" + "AB" * 20), "sig" + "ab" * 20)

    def test_matches_wallet_address(self):
        w = crypto.Wallet.generate()
        self.assertTrue(crypto.checksum_ok(crypto.to_checksum(w.address)))
        self.assertEqual(crypto.norm_address(crypto.to_checksum(w.address)), w.address)


if __name__ == "__main__":
    unittest.main()
