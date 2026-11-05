"""Regras v8 (Sigma 2.14): grupos automáticos.

A rede monta sozinha, pela reputação, um grupo por réplica para o modelo que não cabe em ninguém (coordenador com
disco para o modelo inteiro + trabalhadores até somar a memória); cada réplica é um grupo completo (cada parte do
modelo tem a sua cópia no outro grupo) e os pedidos correm entre eles (v7). O pagamento e o aluguel do grupo são
repartidos pela memória de cada membro; grupo com membro sumido se desfaz; a vigia sempre existe (de reserva, se
ninguém mais hospeda o modelo) e a trapaça custa caro (reputação e metade da caução)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain  # noqa: E402
from sigma.chain import UNIT, TxError  # noqa: E402
from test_chain import H, WALLETS, machine, model, supply  # noqa: E402
from test_chain_v3 import V3Net  # noqa: E402

A, CLIENT = WALLETS[0], WALLETS[1]
MS = WALLETS[6:14]                            # 8 máquinas, cada uma recebendo os próprios ganhos
PAYS = [w.address for w in MS]
GIANT = 5_000_000_000                         # ~6,5 GB de memória: não cabe em nenhuma (3 GB cada)


def net(rules=8, n=8, ram=3000, lend=True):
    nt = V3Net(rules=rules, window_seconds=60, epoch_seconds=600)
    nt.fund(A.address, CLIENT.address)
    for m, p in zip(MS[:n], PAYS):
        extra = {"lend": lend} if rules >= 8 else {}
        nt.send(m, "register_machine", machine(p, pub=m.pub, hw={"ai_ram_mb": ram, "disk_mb": 100_000}, **extra))
    nt.block()
    nt.ms = MS[:n]
    return nt


def set_rep(nt, wallet, rep):
    for st in (nt.state, nt.pending):
        st.machines[wallet.address]["reputation"] = rep


def giant(nt, replicas=2, budget=None):
    budget = budget or 900 * UNIT
    nt.send(A, "register_model", model("gigante", size=GIANT, budget=budget, replicas=replicas))
    nt.block()
    return nt.step(*nt.ms)


def groups(s):
    return s.ext.get("groups") or {}


class TestFormation(unittest.TestCase):
    def test_two_groups_by_reputation(self):
        nt = net()
        for i, m in enumerate(MS):
            set_rep(nt, m, 10 - i)                # MS[0] tem a maior reputação
        s = giant(nt)
        gs = sorted(groups(s).values(), key=lambda g: g["formed"])
        self.assertEqual(len(gs), 2)
        coords = {g["coordinator"] for g in gs}
        self.assertIn(MS[0].address, coords)      # o de maior reputação coordena
        for g in gs:
            self.assertGreaterEqual(g["own_mb"] + sum(g["workers"].values()), s.models["gigante"]["ram_mb"])
            self.assertIn(g["coordinator"], s.models["gigante"]["assigned"])
        members = [a for g in gs for a in [g["coordinator"], *g["workers"]]]
        self.assertEqual(len(members), len(set(members)))   # ninguém em dois grupos
        self.assertEqual(supply(s), s.mining["minted"])

    def test_not_enough_memory_no_group(self):
        s = giant(net(n=2))
        self.assertEqual(groups(s), {})

    def test_only_lenders(self):
        s = giant(net(lend=False))
        self.assertEqual(groups(s), {})

    def test_lend_unknown_before_v8(self):
        nt = V3Net(rules=7)
        with self.assertRaisesRegex(TxError, "campos desconhecidos"):
            nt.send(MS[0], "register_machine", machine(PAYS[0], lend=True))

    def test_small_model_still_goes_to_one_machine(self):
        nt = net()
        nt.send(A, "register_model", model("pequeno", size=1_000_000, replicas=2))
        nt.block()
        s = nt.step(*MS)
        self.assertEqual(len(s.models["pequeno"]["assigned"]), 2)
        self.assertEqual(groups(s), {})


class TestGroupLife(unittest.TestCase):
    def pinned(self):
        nt = net()
        s = giant(nt, replicas=1)
        (gid, g), = groups(s).items()
        coord = next(m for m in MS if m.address == g["coordinator"])
        nt.send(coord, "pin_model", {"name": "gigante"})
        nt.block()
        return nt, gid, g, coord

    def test_job_payment_split_by_memory(self):
        nt, gid, g, coord = self.pinned()
        price = nt.state.machines[coord.address]["prices"]["ai_per_1k_tokens"]
        jid = nt.send(CLIENT, "create_job", {"kind": "ai", "model": "gigante", "spec_hash": H, "escrow": 100 * price,
                                             "max_price": price, "max_tokens": 50, "machine": coord.address,
                                             "client_pub": CLIENT.pub})["id"]
        nt.block()
        nt.send(coord, "claim_job", {"job_id": jid})
        nt.block()
        workers = {a: nt.state.machines[a]["payout"] for a in g["workers"]}
        before = {p: nt.state.bal(p) for p in workers.values()}
        nt.send(coord, "submit_result", {"job_id": jid, "result_hash": H,
                                         "usage": {"tokens_in": 5000, "tokens_out": 5000}})
        s = nt.block()
        watcher = next(m for m in MS if m.address == s.jobs[jid]["review"]["watcher"])
        nt.send(watcher, "review_job", {"job_id": jid, "verdict": "ok"})
        s = nt.block()
        job = s.jobs[jid]
        self.assertEqual(job["status"], "done")
        got = {a: s.bal(p) - before[p] for a, p in workers.items()}
        self.assertTrue(all(v > 0 for v in got.values()), got)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_member_gone_dissolves(self):
        nt, gid, g, coord = self.pinned()
        alive = [m for m in MS if m.address not in g["workers"]]
        nt.step(*alive)
        s = nt.step(*alive)                               # os trabalhadores somem duas janelas
        self.assertNotIn(gid, groups(s))
        self.assertNotIn(coord.address, s.models["gigante"]["pinners"])
        self.assertEqual(supply(s), s.mining["minted"])


class TestPunishment(unittest.TestCase):
    def test_watcher_fallback_and_severe_penalty(self):
        nt = net()
        s = giant(nt, replicas=1)
        (gid, g), = groups(s).items()
        coord = next(m for m in MS if m.address == g["coordinator"])
        payout = s.machines[coord.address]["payout"]
        owner = next(w for w in MS if w.address == payout)
        nt.send(A, "transfer", {"to": owner.address, "amount": 60 * UNIT})
        nt.block()
        nt.send(owner, "bond", {"machine": coord.address, "amount": 50 * UNIT})
        nt.send(coord, "pin_model", {"name": "gigante"})
        nt.block()
        price = nt.state.machines[coord.address]["prices"]["ai_per_1k_tokens"]
        jid = nt.send(CLIENT, "create_job", {"kind": "ai", "model": "gigante", "spec_hash": H, "escrow": 100 * price,
                                             "max_price": price, "max_tokens": 50, "machine": coord.address,
                                             "client_pub": CLIENT.pub})["id"]
        nt.block()
        nt.send(coord, "claim_job", {"job_id": jid})
        nt.block()
        nt.send(coord, "submit_result", {"job_id": jid, "result_hash": H, "usage": {"tokens_in": 10, "tokens_out": 10}})
        s = nt.block()
        watcher = s.jobs[jid]["review"]["watcher"]
        self.assertIsNotNone(watcher)                     # ninguém mais hospeda o gigante: vigia de reserva
        self.assertNotIn(watcher, [g["coordinator"], *g["workers"]])
        rep, wrep = s.machines[coord.address]["reputation"], {a: s.machines[a]["reputation"] for a in g["workers"]}
        w = next(m for m in MS if m.address == watcher)
        nt.send(w, "review_job", {"job_id": jid, "verdict": "bad"})
        s = nt.block()
        self.assertEqual(s.jobs[jid]["status"], "rejected")
        self.assertEqual(s.machines[coord.address]["reputation"],
                         rep - chain.BAD_REPUTATION_V8 - chain.SLASH_REPUTATION)
        self.assertEqual(s.ext["bonds"][coord.address]["amount"], 25 * UNIT)   # metade da caução foi cortada
        for a, r in wrep.items():
            self.assertEqual(s.machines[a]["reputation"], r - chain.BAD_WORKER_REPUTATION)
        self.assertEqual(supply(s), s.mining["minted"])


if __name__ == "__main__":
    unittest.main()
