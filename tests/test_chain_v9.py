"""Regras v9 (Sigma 2.15): o padrinho saca do orçamento do modelo, deixando um dia de aluguel; retirar devolve tudo."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain  # noqa: E402
from sigma.chain import UNIT, TxError  # noqa: E402
from test_chain import WALLETS, machine, model, supply  # noqa: E402
from test_chain_v3 import V3Net  # noqa: E402

A, B, M = WALLETS[0], WALLETS[1], WALLETS[6]


def net(rules=9):
    n = V3Net(rules=rules)
    n.fund(A.address, B.address)
    n.send(M, "register_machine", machine(A.address, pub=M.pub))
    n.send(A, "register_model", model("meu-modelo", budget=100 * UNIT))
    n.block()
    n.step(M)
    n.send(M, "pin_model", {"name": "meu-modelo"})
    n.block()
    return n


class TestWithdraw(unittest.TestCase):
    def test_withdraw_keeps_one_day(self):
        n = net()
        m = n.state.models["meu-modelo"]
        keep = n.state.rent_window(m) * -(-chain.MIN_FUNDED_SECONDS // n.state.params["window_seconds"])
        free = m["budget"] - keep
        with self.assertRaisesRegex(TxError, "dá para sacar até"):
            n.send(A, "withdraw_model", {"name": "meu-modelo", "amount": free + 1})
        with self.assertRaisesRegex(TxError, "padrinho|sponsor|só"):
            n.send(B, "withdraw_model", {"name": "meu-modelo", "amount": 1})
        before = n.state.bal(A.address)
        n.send(A, "withdraw_model", {"name": "meu-modelo", "amount": free})
        s = n.block()
        self.assertGreaterEqual(s.bal(A.address) - before, free - 1)   # o bloco pode ter cobrado o aluguel
        self.assertIn("meu-modelo", s.models)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_retire_returns_everything(self):
        n = net()
        budget, before = n.state.models["meu-modelo"]["budget"], n.state.bal(A.address)
        n.send(A, "retire_model", {"name": "meu-modelo"})
        s = n.block()
        self.assertNotIn("meu-modelo", s.models)
        self.assertGreater(s.bal(A.address) - before, budget // 2)   # volta o orçamento (menos o aluguel do bloco)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_unknown_before_v9(self):
        n = V3Net(rules=8)
        n.fund(A.address)
        with self.assertRaisesRegex(TxError, "desconhecido"):
            n.send(A, "withdraw_model", {"name": "meu-modelo", "amount": 1})


if __name__ == "__main__":
    unittest.main()
