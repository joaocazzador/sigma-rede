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


class TestLayerFractionPlanner(unittest.TestCase):
    def test_layer_fragments_are_scheduled_and_reassigned(self):
        planner = grupo.LayerFractionPlanner({"pc-a": 8, "pc-b": 8, "pc-c": 8})
        plan = planner.plan_layer("encoder", 12, fragment_size=4)
        self.assertEqual([f["range"] for f in plan], [(0, 4), (4, 8), (8, 12)])
        self.assertEqual(planner.ready("encoder"), ["encoder:0"])

        planner.claim("encoder:0", "pc-a")
        planner.fail("encoder:0")
        self.assertEqual(planner.reassign("encoder:0", "pc-b"), "encoder:0")
        planner.claim("encoder:0", "pc-b")
        planner.checkpoint("encoder:0", 4)
        self.assertEqual(planner.ready("encoder"), ["encoder:1"])
        planner.claim("encoder:1", "pc-c")
        planner.checkpoint("encoder:1", 4)
        self.assertEqual(planner.ready("encoder"), ["encoder:2"])
        planner.claim("encoder:2", "pc-a")
        planner.checkpoint("encoder:2", 4)
        self.assertEqual(planner.ready("encoder"), [])
        self.assertEqual(planner.status("encoder")["done"], 3)

    def test_failed_fragment_is_queued_again_only_after_release(self):
        planner = grupo.LayerFractionPlanner({"pc-a": 4, "pc-b": 4})
        planner.plan_layer("decoder", 8, fragment_size=4)
        planner.claim("decoder:0", "pc-a")
        planner.fail("decoder:0")
        self.assertEqual(planner.ready("decoder"), ["decoder:0"])
        planner.reassign("decoder:0", "pc-b")
        self.assertEqual(planner.status("decoder")["fragments"][0]["assigned_to"], "pc-b")

    def test_coordinator_tracks_fractioned_layer_runtime(self):
        coord, worker_a, worker_b = Wallet.generate(), Wallet.generate(), Wallet.generate()
        c = grupo.Coordinator(coord, type("Client", (), {"machine": lambda self, a: {"pub": {}}})(),
                             "127.0.0.1:0", {worker_a.address: 4096, worker_b.address: 4096}, log=lambda m: None)
        c.plan_layer("encoder", 12, fragment_size=4)
        self.assertEqual(c.ready("encoder"), ["encoder:0"])
        c.claim("encoder:0", worker_a.address)
        c.checkpoint("encoder:0", 4)
        self.assertEqual(c.ready("encoder"), ["encoder:1"])
        c.claim("encoder:1", worker_b.address)
        c.fail("encoder:1")
        self.assertEqual(c.ready("encoder"), ["encoder:1"])
        c.reassign("encoder:1", worker_a.address)
        c.checkpoint("encoder:1", 4)
        self.assertEqual(c.ready("encoder"), ["encoder:2"])

    def test_timed_out_leases_reenter_queue(self):
        planner = grupo.LayerFractionPlanner({"pc-a": 8, "pc-b": 8})
        planner.plan_layer("decoder", 8, fragment_size=4)
        planner.claim("decoder:0", "pc-a")
        self.assertEqual(planner.leased("decoder:0"), "pc-a")
        self.assertEqual(planner.release("decoder:0"), "decoder:0")
        planner.claim("decoder:0", "pc-b")
        self.assertEqual(planner.lease_expired("decoder:0", timeout_seconds=0.0), True)
        self.assertEqual(planner.ready("decoder"), ["decoder:0"])

    def test_runtime_selects_machine_for_next_ready_fragment(self):
        coord = grupo.Coordinator(Wallet.generate(), type("Client", (), {"machine": lambda self, a: {"pub": {}}})(),
                                 "127.0.0.1:0", {"pc-a": 8, "pc-b": 12}, log=lambda m: None)
        coord.plan_layer("mix", 12, fragment_size=4)
        self.assertEqual(coord.next_ready(), ("mix:0", "pc-b"))
        self.assertEqual(coord.assign_next(), "mix:0")
        self.assertEqual(coord.leased("mix:0"), "pc-b")


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
