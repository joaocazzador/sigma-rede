"""Grupos automáticos na máquina (regras v8): ela sobe o papel que a rede decidiu (trabalhador ou coordenador, pelo
relé), troca quando a rede troca, sai quando o grupo se desfaz, e o coordenador só carrega o gigante com todos os
trabalhadores ligados. Sem rede nem llama.cpp: grupo e relé são falsos."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sigma import host as hostmod  # noqa: E402
from sigma.crypto import Wallet  # noqa: E402


class Fake:
    made = []

    def __init__(self, *a, **k):
        self.args, self.stopped, self.endpoint = a, False, "127.0.0.1:9"
        self.conn = []
        Fake.made.append(self)

    def start(self):
        return self

    def stop(self):
        self.stopped = True

    def connected(self):
        return self.conn

    def rpc(self, kind=None):
        return []

    def lent_mb(self):
        return 0


class FakeWorker(Fake):
    role = "worker"


class FakeCoord(Fake):
    role = "coordinator"


class TestAutoGroups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.w = Wallet.generate()
        self.h = hostmod.Host("http://sigma.exemplo:8700", self.w, "0x" + "cd" * 20, self.tmp, {"engine": "mock"},
                              log=lambda *a: None)
        self.addCleanup(self.h.engine.shutdown)
        self.h._mining = (1e18, {"groups": True, "relay_port": 7790})
        for target, fake in (("Worker", FakeWorker), ("Coordinator", FakeCoord)):
            p = mock.patch.object(hostmod.grupo, target, fake)
            p.start()
            self.addCleanup(p.stop)
        for target in ("RelayJoin", "RelayHost"):
            p = mock.patch.object(hostmod.relay, target, Fake)
            p.start()
            self.addCleanup(p.stop)
        Fake.made.clear()

    def record(self, *groups):
        self.h._record = {"groups": list(groups)}

    def test_worker_then_leaves(self):
        self.record({"id": "g1", "role": "worker", "model": "gigante", "coordinator": "sig" + "1" * 40,
                     "workers": {self.w.address: 3000}})
        self.h._sync_groups()
        self.assertIsInstance(self.h.group, FakeWorker)
        self.assertEqual(self.h._relay_addr(), "sigma.exemplo:7790")
        self.record()
        self.h._sync_groups()
        self.assertIsNone(self.h.group)
        self.assertTrue(all(f.stopped for f in Fake.made))

    def test_coordinator_waits_for_all_workers(self):
        workers = {"sig" + "2" * 40: 3000, "sig" + "3" * 40: 3000}
        self.record({"id": "g2", "role": "coordinator", "model": "gigante", "coordinator": self.w.address,
                     "workers": workers})
        self.h._sync_groups()
        self.assertIsInstance(self.h.group, FakeCoord)
        self.assertEqual(self.h._group_wait("gigante"), (0, 2))
        self.h.group.conn = list(workers)
        self.assertIsNone(self.h._group_wait("gigante"))
        self.assertIsNone(self.h._group_wait("outro"))

    def test_role_change_restarts(self):
        g = {"id": "g3", "role": "worker", "model": "gigante", "coordinator": "sig" + "1" * 40,
             "workers": {self.w.address: 3000}}
        self.record(g)
        self.h._sync_groups()
        first = self.h.group
        self.record(dict(g, id="g4"))
        self.h._sync_groups()
        self.assertIsNot(self.h.group, first)
        self.assertTrue(first.stopped)

    def test_registration_lends(self):
        data = self.h._registration()
        self.assertTrue(data.get("lend"))

    def test_stats_reports_group_runtime(self):
        self.record({"id": "g2", "role": "coordinator", "model": "gigante", "coordinator": self.w.address,
                     "workers": {"sig" + "2" * 40: 3000, "sig" + "3" * 40: 3000}})
        self.h._sync_groups()
        self.h.group.status = lambda: {"role": "coordinator", "kind": "text", "layers": {}}
        stats = self.h.stats()
        self.assertIn("group", stats)
        self.assertEqual(stats["group"]["role"], "coordinator")
        self.assertEqual(stats["group"]["layers"], {})


if __name__ == "__main__":
    unittest.main()
