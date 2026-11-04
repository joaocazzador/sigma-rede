"""A máquina não baixa GB de um modelo cujo orçamento não paga nem um dia de aluguel (host._underfunded),
com a mesma conta da chain (State.rent_window / State.funded) e do nó (/machine -> assignments)."""
import types
import unittest

from sigma import chain, node
from sigma.chain import State
from sigma.host import UNDERFUNDED_TEXT, Host


class UnderfundedTest(unittest.TestCase):
    def check(self, a, window_seconds=300.0):
        fake = types.SimpleNamespace(_window_seconds=lambda: window_seconds)
        return Host._underfunded(fake, a)

    def test_one_day_of_rent_is_the_line(self):
        self.assertTrue(self.check({"budget": 2879, "rent_window": 10}))      # 287 janelas de 5 min
        self.assertFalse(self.check({"budget": 2880, "rent_window": 10}))     # 288 janelas = 1 dia
        self.assertTrue(self.check({"budget": 2880, "rent_window": 10}, window_seconds=299.0))
        self.assertIn("um dia de hospedagem", UNDERFUNDED_TEXT)

    def test_small_files_never_wait(self):
        """Modelo de poucos MB (até FUNDED_ABOVE) baixa com qualquer orçamento, como na chain.funded."""
        self.assertFalse(self.check({"budget": 1, "rent_window": 10, "size": chain.FUNDED_ABOVE}))
        self.assertTrue(self.check({"budget": 1, "rent_window": 10, "size": chain.FUNDED_ABOVE + 1}))

    def test_old_node_without_the_fields_changes_nothing(self):
        for a in ({}, {"budget": 5}, {"rent_window": 10}, {"budget": "1", "rent_window": 10},
                  {"budget": 1, "rent_window": 0}):
            self.assertFalse(self.check(a), a)

    def test_node_sends_what_the_chain_computes(self):
        st = State({"network": "sigma", "chain_id": 51515, "time": 1_700_000_000, "validators": [], "alloc": {},
                    "params": {"window_seconds": 300, "epoch_seconds": 3000}})
        addr = "sig" + "1" * 40
        st.models["m-a"] = {"name": "m-a", "url": "https://huggingface.co/x/y/resolve/main/m.gguf", "sha256": "a" * 64,
                            "size": 491_400_032, "ram_mb": 1, "replicas": 2, "budget": 1_000_000,
                            "pinners": {}, "assigned": {addr: 0}}
        assign, _ = node.hosting_index(st)
        a = assign[addr][0]
        self.assertEqual(a["rent_window"], st.rent_window(st.models["m-a"]))
        # disco (5 µSIG/MB) + memória reservada (2 µSIG/MB), por cópia, com a rede vazia (1x)
        self.assertEqual(a["rent_window"], 2 * -(-(491_400_032 * 5 * 10_000 + 1 * 1_000_000 * 2 * 10_000)
                                                   // (1_000_000 * 10_000)))
        self.assertEqual(a["budget"], 1_000_000)
        self.assertEqual(self.check(a), not st.funded(st.models["m-a"]))
        self.assertEqual(chain.MIN_FUNDED_SECONDS, 86400)


if __name__ == "__main__":
    unittest.main()
