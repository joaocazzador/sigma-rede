"""Regras v9: nome público da carteira, chat global (as 5.000 mais novas, texto em blob) e fundo de hospedagem
pública (o que se paga vai para o fundo; a cada 12 h, public_fund_bps dele para as máquinas que hospedam algo)."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain  # noqa: E402
from sigma.chain import UNIT, TxError, sha  # noqa: E402
from test_chain import WALLETS, machine, model, supply  # noqa: E402
from test_chain_v3 import V3Net  # noqa: E402

A, B, M = WALLETS[0], WALLETS[1], WALLETS[6]


def net():
    n = V3Net(rules=9)
    n.fund(A.address, B.address)
    return n


class TestNamesChatFund(unittest.TestCase):
    def test_params_exist(self):
        p = net().state.params
        self.assertEqual((p["chat_price"], p["name_price"], p["public_fund_bps"]), (UNIT, UNIT, 10))

    def test_name_paid_unique(self):
        n = net()
        before = n.state.bal(A.address)
        n.send(A, "set_name", {"name": "  João   Sigma "})
        s = n.block()
        self.assertEqual(s.ext["names"][A.address], "João Sigma")
        self.assertEqual(before - s.bal(A.address), UNIT)
        self.assertEqual(s.ext["public_fund"], UNIT)
        with self.assertRaisesRegex(TxError, "outra carteira"):
            n.send(B, "set_name", {"name": "joão sigma"})
        with self.assertRaisesRegex(TxError, "nome"):
            n.send(B, "set_name", {"name": "<b>x</b>"})
        self.assertEqual(supply(s), s.mining["minted"])

    def test_chat_keeps_newest(self):
        n = net()
        with mock.patch.object(chain, "CHAT_KEEP", 3):
            for i in range(5):
                n.send(A, "chat_post", {"blob": sha(str(i).encode()), "size": 10})
            s = n.block()
        self.assertEqual([m["n"] for m in s.ext["chat"]], [3, 4, 5])
        self.assertEqual(s.ext["public_fund"], 5 * UNIT)
        with self.assertRaisesRegex(TxError, "size"):
            n.send(A, "chat_post", {"blob": sha(b"x"), "size": chain.CHAT_MAX_BYTES + 1})
        self.assertEqual(supply(s), s.mining["minted"])

    def test_fund_pays_hosts_every_period(self):
        n = net()
        n.send(M, "register_machine", machine(B.address, pub=M.pub))
        n.send(A, "register_model", model("publico", budget=50 * UNIT))
        n.block()
        n.step(M)
        n.send(M, "pin_model", {"name": "publico"})
        for i in range(3):
            n.send(A, "chat_post", {"blob": sha(str(i).encode()), "size": 10})
        s = n.block()
        fund = s.ext["public_fund"]
        before = s.bal(B.address)
        s = n.settle()
        self.assertEqual(fund - s.ext["public_fund"], fund * 10 // 10_000)
        self.assertGreaterEqual(s.bal(B.address) - before, fund * 10 // 10_000)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_unknown_before_v9(self):
        n = V3Net(rules=8)
        n.fund(A.address)
        with self.assertRaisesRegex(TxError, "desconhecido"):
            n.send(A, "chat_post", {"blob": sha(b"x"), "size": 1})


if __name__ == "__main__":
    unittest.main()
