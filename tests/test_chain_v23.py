"""Regras da v2.3 (docs/SPEC-v2.3.md): aluguel pelo que o modelo/site ocupa e pela demanda, e votações.

    python -m unittest tests.test_chain_v23 -v

A emissão contínua tem os testes em tests/test_chain.py (TestEmission).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain  # noqa: E402
from sigma.chain import UNIT, TxError  # noqa: E402
from test_chain import H, WALLETS, Net, machine, model, supply  # noqa: E402

A, B, C, D = WALLETS[0], WALLETS[1], WALLETS[2], WALLETS[3]
MA, MB = WALLETS[10], WALLETS[11]


def priced(**params):
    """Rede com preço de memória e multiplicador de demanda ligados (a FAST dos testes antigos zera os dois)."""
    return Net(**dict({"ram_per_mb_window": 2, "demand_max_bps": 30000}, **params))


class TestDemandRent(unittest.TestCase):
    """SPEC-v2.3 §2: o aluguel segue disco + memória do que está hospedado e a demanda da rede."""

    def test_model_pays_disk_and_ram_site_only_disk(self):
        s = priced().state
        m = {"size": 1_000_000_000, "ram_mb": 1712, "replicas": 2}
        site = {"size": 1_000_000_000, "replicas": 2}
        # rede vazia: multiplicador 1x
        self.assertEqual(s.copy_fee(m), 1000 * 5 + 1712 * 2)          # disco 5 µSIG/MB + memória 2 µSIG/MB
        self.assertEqual(s.copy_fee(site), 1000 * 5)
        self.assertEqual(s.rent_window(m), 2 * (1000 * 5 + 1712 * 2))

    def test_demand_raises_the_price_up_to_four_times(self):
        net = priced()
        net.fund(A.address)
        net.send(MA, "register_machine", machine(A.address, hw={"ai_ram_mb": 600, "disk_mb": 50_000}))
        net.block()
        s = net.next_window()
        self.assertEqual(s.mining["rent"], {"disk_bps": 10000, "ram_bps": 10000})   # nada pedido ainda
        # um modelo de 40 MB (560 MB de memória com o contexto) x 1 cópia numa rede de 600 MB: u ~ 0,93 -> ~3,6x
        net.send(A, "register_model", model("modelo-a", size=40_000_000, replicas=1, budget=50 * UNIT))
        net.block()
        s = net.next_window()
        u = 560 * 10_000 // 600
        self.assertEqual(s.mining["rent"]["ram_bps"], 10_000 + 30000 * u * u // 100_000_000)
        self.assertGreater(s.mining["rent"]["ram_bps"], 30_000)
        # pedido acima da capacidade: no teto (4x)
        net.send(A, "register_model", model("modelo-b", size=40_000_000, replicas=3, budget=50 * UNIT))
        net.block()
        s = net.next_window()
        self.assertEqual(s.mining["rent"]["ram_bps"], 40_000)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_no_capacity_counts_as_full(self):
        net = priced()
        s = net.next_window()                                          # nenhuma máquina online
        self.assertEqual(s.mining["rent"], {"disk_bps": 40_000, "ram_bps": 40_000})

    def test_rent_charged_with_the_demand_of_the_window(self):
        net = priced()
        net.fund(A.address)
        net.send(MA, "register_machine", machine(A.address, hw={"ai_ram_mb": 8000, "disk_mb": 50_000}))
        net.send(A, "register_model", model("modelo-a", size=100_000_000, replicas=1, budget=50 * UNIT))
        net.block()
        net.step(MA)
        net.send(MA, "pin_model", {"name": "modelo-a"})
        net.block()
        before = net.state.models["modelo-a"]["budget"]
        s = net.next_window()                                          # o aluguel sai na virada da janela
        m = s.models["modelo-a"]
        self.assertEqual(before - m["budget"], s.copy_fee(m))            # uma cópia, uma janela
        self.assertIn([A.address, "rent", s.copy_fee(m), "modelo-a"], s.credits)


class TestGovernance(unittest.TestCase):
    """SPEC-v2.3 §4: qualquer um propõe; 1 SIG = 1 voto (travado até o fim); 10 dias; maioria com quórum."""

    def setUp(self):
        self.net = Net()
        self.net.fund(A.address, B.address, C.address)

    def propose(self, who=A, **d):
        data = dict({"kind": "param", "title": "Emissão menor", "text": "", "param": "mint_bps", "value": 0}, **d)
        return self.net.send(who, "propose", data)["id"]

    def end(self):
        return self.net.block(at=self.net.state.now + chain.VOTE_SECONDS + 1)

    def test_param_vote_passes_and_changes_the_rule(self):
        net = self.net
        before = net.state.bal(A.address)
        pid = self.propose()
        s = net.block()
        self.assertEqual(before - s.bal(A.address), s.params["proposal_deposit"])     # o depósito trava
        prop = s.proposals[pid]
        self.assertEqual((prop["status"], prop["param"], prop["value"], prop["old"]), ("open", "mint_bps", 0, 1))
        vb, vc = s.bal(B.address) // 2, s.bal(C.address) // 4
        net.send(B, "vote", {"proposal_id": pid, "choice": "sim", "amount": vb})
        net.send(C, "vote", {"proposal_id": pid, "choice": "nao", "amount": vc})
        s = net.block()
        self.assertEqual((s.proposals[pid]["yes"], s.proposals[pid]["no"]), (vb, vc))
        self.assertEqual(supply(s), s.mining["minted"])                              # o travado conta na oferta
        bal_b, bal_c = s.bal(B.address), s.bal(C.address)
        s = self.end()
        prop = s.proposals[pid]
        self.assertEqual((prop["status"], prop.get("applied")), ("approved", True))
        self.assertEqual(s.params["mint_bps"], 0)                                    # a regra mudou
        self.assertEqual(prop["votes"], {})                                          # encerrada: só os totais
        self.assertIn([B.address, "vote", vb, pid], s.credits)                       # o SIG volta inteiro
        self.assertIn([C.address, "vote", vc, pid], s.credits)
        self.assertIn([A.address, "deposit", s.params["proposal_deposit"], pid], s.credits)
        self.assertGreaterEqual(s.bal(B.address), bal_b + vb)
        self.assertGreaterEqual(s.bal(C.address), bal_c + vc)
        self.assertEqual(supply(s), s.mining["minted"])
        minted = s.mining["minted"]
        net.mine(A.address)
        net.block()
        net.next_window()
        net.next_window()
        self.assertEqual(net.block(dt=10).mining["minted"], minted)                  # mint_bps 0: não imprime mais

    def test_more_no_than_yes_is_rejected(self):
        net = self.net
        pid = self.propose(value=10)
        net.block()
        net.send(B, "vote", {"proposal_id": pid, "choice": "sim", "amount": 10 * UNIT})
        net.send(C, "vote", {"proposal_id": pid, "choice": "nao", "amount": 11 * UNIT})
        net.block()
        s = self.end()
        self.assertEqual(s.proposals[pid]["status"], "rejected")
        self.assertEqual(s.params["mint_bps"], 1)

    def test_quorum_is_needed(self):
        net = self.net
        pid = self.propose(value=10)
        net.block()
        net.send(B, "vote", {"proposal_id": pid, "choice": "sim", "amount": 1})      # 1 µSIG não decide nada
        net.block()
        s = self.end()
        prop = s.proposals[pid]
        self.assertEqual(prop["status"], "rejected")
        self.assertEqual(prop["quorum"], s.mining["minted"] * s.params["quorum_bps"] // 10_000)

    def test_one_vote_per_wallet_and_sig_is_locked(self):
        net = self.net
        pid = self.propose()
        net.block()
        bal = net.state.bal(B.address)
        net.send(B, "vote", {"proposal_id": pid, "choice": "sim", "amount": bal})
        with self.assertRaisesRegex(TxError, "já votou"):
            net.send(B, "vote", {"proposal_id": pid, "choice": "nao", "amount": 1})
        s = net.block()
        self.assertEqual(s.bal(B.address), 0)                                        # travado: não vota de novo
        with self.assertRaisesRegex(TxError, "saldo insuficiente"):                  # nem manda para outra carteira
            net.send(B, "transfer", {"to": D.address, "amount": 1})

    def test_text_proposal_only_records(self):
        net = self.net
        params = dict(net.state.params)
        pid = net.send(A, "propose", {"kind": "text", "title": "Entrega de sites pelas máquinas",
                                      "text": "Priorizar isso na próxima versão."})["id"]
        net.block()
        net.send(B, "vote", {"proposal_id": pid, "choice": "sim", "amount": 100 * UNIT})
        net.block()
        s = self.end()
        self.assertEqual(s.proposals[pid]["status"], "approved")
        self.assertNotIn("applied", s.proposals[pid])
        self.assertEqual(s.params, params)

    def test_validation(self):
        net = self.net
        for d, pattern in (({"param": "supply_cap", "value": 1}, "não pode ser votado"),
                           ({"param": "window_seconds", "value": 60}, "não pode ser votado"),
                           ({"param": "mint_bps", "value": 51}, "entre 0 e 50"),
                           ({"param": "mint_bps", "value": 1}, "já é o valor de agora"),
                           ({"title": "x"}, "título"),
                           ({"kind": "lei"}, "kind deve ser")):
            with self.subTest(d=d), self.assertRaisesRegex(TxError, pattern):
                self.propose(**d)
        with self.assertRaisesRegex(TxError, "não leva param"):
            net.send(A, "propose", {"kind": "text", "title": "Ideia", "param": "mint_bps", "value": 2})
        poor = WALLETS[7]
        with self.assertRaisesRegex(TxError, "depósito"):
            net.send(poor, "propose", {"kind": "text", "title": "Sem saldo"})
        pid = self.propose()
        net.block()
        for d, pattern in (({"proposal_id": "x" * 64}, "não existe"), ({"choice": "talvez"}, "choice"),
                           ({"amount": 0}, "amount")):
            vote = dict({"proposal_id": pid, "choice": "sim", "amount": 1}, **d)
            with self.subTest(vote=vote), self.assertRaisesRegex(TxError, pattern):
                net.send(B, "vote", vote)
        self.end()
        with self.assertRaisesRegex(TxError, "já terminou"):
            net.send(B, "vote", {"proposal_id": pid, "choice": "sim", "amount": 1})

    def test_at_most_twenty_open(self):
        net = self.net
        for i in range(chain.MAX_OPEN_PROPOSALS):
            net.send(A, "propose", {"kind": "text", "title": f"Ideia número {i}"})
        net.block()
        with self.assertRaisesRegex(TxError, "votações abertas"):
            net.send(B, "propose", {"kind": "text", "title": "Mais uma"})

    def test_voted_params_survive_a_snapshot(self):
        net = self.net
        pid = self.propose(param="hosting_per_mb_window", value=9)
        net.block()
        net.send(B, "vote", {"proposal_id": pid, "choice": "sim", "amount": 100 * UNIT})
        net.block()
        s = self.end()
        self.assertEqual(s.params["hosting_per_mb_window"], 9)
        import json
        again = chain.State.from_dict(net.genesis, json.loads(json.dumps(s.to_dict())))
        self.assertEqual((again.params["hosting_per_mb_window"], again.root()), (9, s.root()))


if __name__ == "__main__":
    unittest.main()
