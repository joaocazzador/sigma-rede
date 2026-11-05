"""Regras v5 (Sigma 2.10): falha em imagem ou vídeo custa reputação, e a memória de IA conta só o maior modelo da
máquina (eles se revezam na memória), com até 8 modelos por máquina.

Antes da v5 tudo continua como era: cada caso roda nas duas versões."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain  # noqa: E402
from test_chain import H, WALLETS, machine, model, supply  # noqa: E402
from test_chain_v25 import image_model  # noqa: E402
from test_chain_v3 import V3Net  # noqa: E402

A, CLIENT = WALLETS[0], WALLETS[1]
MB = WALLETS[11]


def net(rules):
    n = V3Net(rules=rules)
    n.fund(A.address, CLIENT.address)
    return n


def host(n, wallet, ram, tasks_=("text", "image")):
    n.send(wallet, "register_machine", machine(A.address, tasks=list(tasks_),
                                               hw={"ai_ram_mb": ram, "disk_mb": 50_000}))


class TestFailedMediaCostsReputation(unittest.TestCase):
    def run_failure(self, rules, text=False):
        n = net(rules)
        host(n, MB, 20_000)
        n.send(A, "register_model", model("texto") if text else image_model())
        n.block()
        n.step(MB)
        name = "texto" if text else "z-image"
        n.send(MB, "pin_model", {"name": name})
        s = n.block()
        mc = s.machines[MB.address]
        price = mc["prices"]["ai_per_1k_tokens" if text else "ai_per_image"]
        job = {"kind": "ai", "model": name, "spec_hash": H, "max_price": price}
        job.update({"escrow": 200 * price, "max_tokens": 100} if text else {"escrow": price, "units": 1})
        jid = n.send(CLIENT, "create_job", job)["id"]
        n.block()
        n.send(MB, "claim_job", {"job_id": jid})
        n.block()
        before = n.state.machines[MB.address]["reputation"]
        usage = {} if text else {"units": 0}
        n.send(MB, "submit_result", {"job_id": jid, "result_hash": H, "usage": usage, "failed": True})
        s = n.block()
        self.assertEqual(s.jobs[jid]["status"], "failed")
        self.assertEqual(supply(s), s.mining["minted"])
        return s.machines[MB.address]["reputation"] - before

    def test_v5_image_failure_costs_one(self):
        self.assertEqual(self.run_failure(5), -1)

    def test_v4_image_failure_was_free(self):
        self.assertEqual(self.run_failure(4), 0)

    def test_v5_text_failure_still_free(self):
        self.assertEqual(self.run_failure(5, text=True), 0)


class TestModelsTakeTurnsInMemory(unittest.TestCase):
    def placed(self, rules, ram=10_000, models=2, size=5_000_000_000):
        """`models` modelos de texto de ~6 GB de memória numa máquina com `ram` MB: quantos a rede coloca nela."""
        n = net(rules)
        host(n, MB, ram)
        for i in range(models):
            n.send(A, "register_model", model(f"mod{i}", size=size, budget=(500 if size > 2e9 else 100) * chain.UNIT))
        n.block()
        s = n.step(MB)
        return sum(1 for m in s.models.values() if MB.address in m["assigned"])

    def test_v4_sums_memory(self):
        self.assertEqual(self.placed(4), 1)

    def test_v5_counts_only_the_biggest(self):
        self.assertEqual(self.placed(5), 2)

    def test_v5_still_needs_the_biggest_to_fit(self):
        self.assertEqual(self.placed(5, ram=5_000), 0)

    def test_v5_up_to_eight_models(self):
        self.assertEqual(self.placed(5, ram=10_000, models=10, size=1_000_000_000),
                         chain.MAX_MODELS_PER_MACHINE_V5)
        self.assertEqual(self.placed(4, ram=100_000, models=10, size=1_000_000_000), chain.MAX_MODELS_PER_MACHINE)


if __name__ == "__main__":
    unittest.main()
