"""Grupo (sigma/grupo.py): opções da linha de comando, o --rpc no llama-server e o coordenador somando a memória.

O teste de ponta a ponta com o llama.cpp de verdade (coordenador + 2 trabalhadores pelo túnel, texto idêntico ao de
um PC só) fica em C:\\sgtmp\\grupo-e2e\\e2e.py; testes de estresse ficaram para depois (docs/PENDENCIAS.md)."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sigma import engines, grupo, tunnel  # noqa: E402
from sigma.crypto import Wallet  # noqa: E402


class TestOptions(unittest.TestCase):
    def test_parse(self):
        w = Wallet.generate()
        self.assertEqual(grupo.parse_worker(f"{w.address}=8"), (w.address, 8192))
        self.assertEqual(grupo.parse_coordinator(f"{w.address}@1.2.3.4:7710"), (w.address, "1.2.3.4:7710"))
        for bad in ("x=8", f"{w.address}=abc", f"{w.address}=0", f"{w.address}=2000"):
            with self.subTest(bad), self.assertRaises(grupo.GroupError):
                grupo.parse_worker(bad)
        for bad in ("x@1.2.3.4:1", w.address, f"{w.address}@semporta"):
            with self.subTest(bad), self.assertRaises(grupo.GroupError):
                grupo.parse_coordinator(bad)

    def test_rpc_on_the_command_line(self):
        llama = engines._LlamaCpp(os.path.join(os.path.dirname(__file__), "_tmp_grupo"), print, 2)
        cmd = llama._command("llama-server", "cpu", "m.gguf", 1, ["127.0.0.1:5", "127.0.0.1:6"])
        self.assertIn("--rpc", cmd)
        self.assertEqual(cmd[cmd.index("--rpc") + 1], "127.0.0.1:5,127.0.0.1:6")
        self.assertEqual(cmd[cmd.index("-ot") + 1], grupo.OUTPUT_ON_COORDINATOR)
        self.assertNotIn("--rpc", llama._command("llama-server", "cpu", "m.gguf", 1, []))


@unittest.skipUnless(tunnel.AVAILABLE, tunnel.UNAVAILABLE)
class TestCoordinator(unittest.TestCase):
    def test_lends_only_while_connected(self):
        coord, worker = Wallet.generate(), Wallet.generate()
        pubs = {coord.address: coord.pub, worker.address: worker.pub}

        class Client:
            def machine(self, a):
                return {"pub": pubs.get(a)}
        c = grupo.Coordinator(coord, Client(), "127.0.0.1:0", {worker.address: 4096}, log=lambda m: None)
        c.start()
        try:
            self.assertEqual((c.lent_mb(), c.rpc()), (0, []))
            link = tunnel.Link(worker.priv, f"127.0.0.1:{c.hub.port}", coord.pub, ("127.0.0.1", 9), log=lambda m: None)
            link.start()
            try:
                end = time.time() + 15
                while not c.rpc() and time.time() < end:
                    time.sleep(0.1)
                self.assertEqual(c.lent_mb(), 4096)
                self.assertEqual(len(c.rpc()), 1)
            finally:
                link.stop()
        finally:
            c.stop()


if __name__ == "__main__":
    unittest.main()
