"""Modelos que se revezam na memória (regras v5): o llama.cpp gerenciado mantém na RAM só o modelo em uso, tira
os parados para dar lugar ao próximo e os traz de volta sozinho quando chega um pedido. Sem llama-server de
verdade: o processo é um falso."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sigma import engines  # noqa: E402


class FakeProc:
    def __init__(self):
        self.dead = False

    def poll(self):
        return 0 if self.dead else None

    def terminate(self):
        self.dead = True

    kill = terminate

    def wait(self, *_):
        return 0


class FakeLog:
    def close(self):
        pass


class Swap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.eng = engines._LlamaCpp(self.tmp, lambda *a: None, 2)
        self.spawned = []

        def spawn(name, path):
            self.spawned.append(name)
            self.eng.procs[name] = {"proc": FakeProc(), "port": 1, "variant": "cpu", "path": path,
                                    "logf": FakeLog(), "rpc": []}
        self.eng._spawn = spawn
        self.eng._start_reaper = lambda: None
        for n in ("mod-a", "mod-b"):
            open(os.path.join(self.tmp, n), "w").close()

    def load(self, name):
        self.eng.load(name, os.path.join(self.tmp, name), "0" * 64)

    def test_second_model_takes_the_place_of_the_first(self):
        self.load("mod-a")
        self.load("mod-b")
        self.assertEqual(self.eng.loaded(), ["mod-a", "mod-b"])   # a máquina hospeda os dois
        self.assertEqual(self.eng.resident(), ["mod-b"])          # mas só um ocupa a RAM

    def test_request_brings_the_model_back(self):
        self.load("mod-a")
        self.load("mod-b")
        self.eng._ready("mod-a")
        self.assertEqual(self.eng.resident(), ["mod-a"])
        self.assertEqual(self.spawned, ["mod-a", "mod-b", "mod-a"])

    def test_busy_model_is_never_evicted(self):
        self.load("mod-a")
        self.eng.busy["mod-a"] = 1
        self.load("mod-b")
        self.assertEqual(self.eng.resident(), ["mod-a", "mod-b"])

    def test_free_memory_before_media(self):
        self.load("mod-a")
        self.eng.free_memory()
        self.assertEqual(self.eng.resident(), [])
        self.assertEqual(self.eng.loaded(), ["mod-a"])

    def test_unknown_model(self):
        with self.assertRaises(engines.EngineError):
            self.eng._ready("mod-x")

    def test_unload_forgets(self):
        self.load("mod-a")
        self.eng.unload("mod-a")
        self.assertEqual((self.eng.loaded(), self.eng.resident()), ([], []))


if __name__ == "__main__":
    unittest.main()
