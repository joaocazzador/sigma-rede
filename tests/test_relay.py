"""Relé dos grupos (sigma/relay.py): coordenador e trabalhador atrás de roteador se encontram pelo relé, que só
repassa bytes e só aceita membros do grupo que provam quem são pela assinatura da máquina."""
import json
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sigma import relay  # noqa: E402
from sigma.crypto import Wallet  # noqa: E402

COORD, WORKER, STRANGER = Wallet.generate(), Wallet.generate(), Wallet.generate()


def echo_server():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)

    def serve():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return

            def one(c=c):
                while True:
                    d = c.recv(65536)
                    if not d:
                        break
                    c.sendall(d.upper())
                c.close()
            threading.Thread(target=one, daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return s


class TestRelay(unittest.TestCase):
    def setUp(self):
        members = {"g1": {"coordinator": (COORD.address, COORD.pub), "workers": {WORKER.address: WORKER.pub}}}
        self.srv = relay.RelayServer("127.0.0.1", 0, members.get, log=lambda *a: None).start()
        self.addr = f"127.0.0.1:{self.srv.port}"
        self.echo = echo_server()
        self.host = relay.RelayHost(self.addr, "g1", COORD, f"127.0.0.1:{self.echo.getsockname()[1]}",
                                    log=lambda *a: None).start()
        end = time.time() + 5
        while not self.host.connected and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(self.host.connected)

    def tearDown(self):
        self.host.stop()
        self.srv.stop()
        self.echo.close()

    def test_worker_reaches_coordinator(self):
        join = relay.RelayJoin(self.addr, "g1", WORKER, log=lambda *a: None).start()
        c = socket.create_connection(("127.0.0.1", join.port), timeout=10)
        payload = b"ola grupo " * 5000
        c.sendall(payload)
        got = b""
        while len(got) < len(payload):
            got += c.recv(65536)
        self.assertEqual(got, payload.upper())
        c.close()
        join.stop()

    def test_stranger_and_bad_signature_refused(self):
        for wallet, op in ((STRANGER, "join"), (WORKER, "host")):
            s = relay._hello(self.addr, op, "g1", wallet)
            self.assertTrue(relay._readline(s).startswith("erro"))
            s.close()
        s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=5)
        ts = int(time.time())
        s.sendall(json.dumps({"op": "join", "group": "g1", "addr": WORKER.address, "ts": ts,
                              "sig": COORD.sign(relay.relay_message("join", "g1", ts))}).encode() + b"\n")
        self.assertIn("assinatura", relay._readline(s))
        s.close()



class TestTunnelThroughRelay(unittest.TestCase):
    """O túnel cifrado do grupo de verdade (sigma/tunnel.py) passando por dentro do relé: o coordenador fala com o
    serviço do trabalhador pela ponte local, e o relé só vê bytes cifrados."""

    def test_group_tunnel_over_relay(self):
        from sigma import grupo, tunnel
        if not tunnel.AVAILABLE:
            self.skipTest(tunnel.UNAVAILABLE)
        pubs = {COORD.address: COORD.pub, WORKER.address: WORKER.pub}

        class Client:
            def machine(self, a):
                return {"pub": pubs.get(a)}
        quiet = lambda *a: None   # noqa: E731
        members = {"g9": {"coordinator": (COORD.address, COORD.pub), "workers": {WORKER.address: WORKER.pub}}}
        srv = relay.RelayServer("127.0.0.1", 0, members.get, log=quiet).start()
        addr = f"127.0.0.1:{srv.port}"
        echo = echo_server()
        c = grupo.Coordinator(COORD, Client(), "127.0.0.1:0", {WORKER.address: 4096}, log=quiet)
        c.start()
        host = relay.RelayHost(addr, "g9", COORD, f"127.0.0.1:{c.hub.port}", log=quiet).start()
        join = relay.RelayJoin(addr, "g9", WORKER, log=quiet).start()
        link = tunnel.Link(WORKER.priv, join.endpoint, COORD.pub, ("127.0.0.1", echo.getsockname()[1]), log=quiet)
        try:
            end = time.time() + 20
            while not host.connected and time.time() < end:
                time.sleep(0.05)
            link.start()
            while not c.rpc() and time.time() < end:
                time.sleep(0.1)
            self.assertEqual(c.lent_mb(), 4096)
            port = int(c.rpc()[0].rsplit(":", 1)[1])
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
            s.sendall(b"camadas do modelo")
            got = b""
            while len(got) < 17:
                got += s.recv(100)
            self.assertEqual(got, b"CAMADAS DO MODELO")
            s.close()
        finally:
            link.stop()
            join.stop()
            host.stop()
            c.stop()
            srv.stop()
            echo.close()


if __name__ == "__main__":
    unittest.main()
