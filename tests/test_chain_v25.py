"""Regras v2 (Sigma 2.5): modelos que geram imagem e vídeo, ao lado dos de texto.

    python -m unittest tests.test_chain_v25 -v

A v2 entra por altura marcada (docs/ATUALIZACOES.md); aqui as redes já nascem nela (genesis "rules": 2), menos
TestBeforeV2, que confere que na v1 tudo continua recusado como sempre foi.
"""
import math
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain  # noqa: E402
from sigma.chain import UNIT, State, TxError, sha  # noqa: E402
from test_chain import H, WALLETS, Net, machine, model, supply  # noqa: E402

A, CLIENT = WALLETS[0], WALLETS[1]
MA, MB = WALLETS[10], WALLETS[11]
VAE = {"role": "vae", "url": "https://huggingface.co/org/img/resolve/main/ae.safetensors",
       "sha256": sha(b"vae"), "size": 3_000_000}
LLM = {"role": "llm", "url": "https://huggingface.co/org/img/resolve/main/qwen.gguf",
       "sha256": sha(b"llm"), "size": 10_000_000}


def v2(**params):
    net = Net(**params)
    net.genesis["rules"] = 2
    net.state = State(net.genesis)
    net.pending = net.state.clone()
    net.prev = chain.genesis_block(net.genesis)["hash"]
    return net


# pequenos de propósito (até FUNDED_ABOVE não precisa adiantar um dia de aluguel; a rede de teste vira a janela a
# cada 5 s)
def image_model(name="z-image", task="image", files=(VAE, LLM), size=20_000_000, budget=50 * UNIT, **kw):
    return model(name, size=size, budget=budget, task=task, files=list(files) if isinstance(files, tuple) else files,
                 **kw)


class TestBeforeV2(unittest.TestCase):
    """Na v1 os campos novos não existem: a rede de antes da altura marcada recusa tudo como sempre."""

    def test_new_fields_refused(self):
        net = Net()
        net.fund(A.address)
        for wallet, type_, data in (
                (A, "register_model", model("m-texto", task="text")),
                (A, "register_model", model("m-img", files=[VAE])),
                (MA, "register_machine", machine(A.address, tasks=["text", "image"])),
                (MA, "register_machine", machine(A.address, prices={"ai_per_image": 10})),
                (A, "create_job", {"kind": "ai", "model": "x", "spec_hash": H, "escrow": 1, "max_price": 1,
                                   "units": 1})):
            with self.assertRaisesRegex(TxError, "campos desconhecidos"):
                net.send(wallet, type_, data)

    def test_v1_machine_has_no_media_keys(self):
        net = Net()
        net.send(MA, "register_machine", machine(A.address))
        s = net.block()
        self.assertEqual(s.machines[MA.address]["prices"], {"ai_per_1k_tokens": 2000, "compute_per_unit": 500})
        self.assertNotIn("tasks", s.machines[MA.address])


class TestMediaModels(unittest.TestCase):
    def setUp(self):
        self.net = v2()
        self.net.fund(A.address, CLIENT.address)

    def test_register_image_model_with_files(self):
        net = self.net
        net.send(A, "register_model", image_model())
        m = net.block().models["z-image"]
        total = 20_000_000 + VAE["size"] + LLM["size"]
        self.assertEqual((m["task"], m["size"], m["main_size"], m["units"]), ("image", total, 20_000_000, 0))
        self.assertEqual(m["ram_mb"], math.ceil(total / 1e6 * 1.2) + chain.MEDIA_RAM_MB["image"])
        self.assertEqual([f["role"] for f in m["files"]], ["vae", "llm"])
        self.assertEqual(supply(net.state), net.state.mining["minted"])

    def test_text_model_unchanged_on_v2(self):
        net = self.net
        net.send(A, "register_model", model("texto"))
        m = net.block().models["texto"]
        self.assertNotIn("task", m)
        self.assertNotIn("files", m)
        self.assertEqual(chain.model_task(m), "text")
        with self.assertRaisesRegex(TxError, "sem files"):
            net.send(A, "register_model", model("texto2", files=[VAE]))

    def test_bad_files_refused(self):
        net = self.net
        for files, msg in (([dict(VAE, role="lora")], "role"), ([VAE, VAE], "sem repetir"),
                           ([dict(VAE, url="ftp://x")], "url de files"), ([dict(VAE, size=0)], "size de files"),
                           ([dict(VAE, extra=1)], "campos desconhecidos"), ([dict(VAE, role=r) for r in chain.MODEL_ROLES], "até 4"),
                           ("vae", "lista")):
            with self.assertRaisesRegex(TxError, msg):
                net.send(A, "register_model", image_model("ruim", files=files))
        with self.assertRaisesRegex(TxError, "task"):
            net.send(A, "register_model", image_model("ruim", task="audio"))

    def test_machine_tasks_and_prices(self):
        net = self.net
        net.send(MA, "register_machine", machine(A.address, tasks=["image", "text"], prices={"ai_per_image": 5000}))
        mc = net.block().machines[MA.address]
        self.assertEqual(mc["tasks"], ["text", "image"])          # na ordem de chain.TASKS
        self.assertEqual(mc["prices"], {"ai_per_1k_tokens": 2000, "compute_per_unit": 500, "ai_per_image": 5000,
                                        "ai_per_video_second": chain.MEDIA_PRICES["ai_per_video_second"]})
        net.send(MA, "register_machine", machine(A.address, tasks=["image"]))   # o motor de texto fora do ar
        self.assertEqual(net.block().machines[MA.address]["tasks"], ["image"])
        net.send(MA, "register_machine", machine(A.address, ai=False, tasks=[]))
        self.assertEqual(net.block().machines[MA.address]["tasks"], [])
        for bad in (["text", "text"], ["audio"], "image", []):    # IA ligada sem nenhuma classe também não
            with self.assertRaisesRegex(TxError, "tasks"):
                net.send(MB, "register_machine", machine(A.address, tasks=bad))
        with self.assertRaisesRegex(TxError, "ai_per_image"):
            net.send(MB, "register_machine", machine(A.address, prices={"ai_per_image": 0}))

    def host(self, wallet, tasks_, ram=20_000):
        self.net.send(wallet, "register_machine", machine(A.address, tasks=tasks_,
                                                          hw={"ai_ram_mb": ram, "disk_mb": 50_000}))

    def test_image_model_only_goes_to_machines_that_accept_images(self):
        net = self.net
        self.host(MA, ["text"])
        self.host(MB, ["text", "image"])
        net.send(A, "register_model", image_model(replicas=2))
        net.block()
        s = net.step(MA, MB)
        self.assertEqual(set(s.models["z-image"]["assigned"]), {MB.address})

    def pinned(self, name="z-image", task="image"):
        net = self.net
        self.host(MB, ["text", task])
        net.send(A, "register_model", image_model(name, task=task))
        net.block()
        net.step(MB)
        net.send(MB, "pin_model", {"name": name})
        return net.block()

    def test_image_job_pays_per_image_delivered(self):
        net = self.net
        s = self.pinned()
        price = s.machines[MB.address]["prices"]["ai_per_image"]
        with self.assertRaisesRegex(TxError, "2 imagens"):
            net.send(CLIENT, "create_job", {"kind": "ai", "model": "z-image", "spec_hash": H, "escrow": price,
                                            "max_price": price, "units": 2})
        for bad, msg in (({"units": 5}, "units"), ({"max_tokens": 100}, "usa units")):
            with self.assertRaisesRegex(TxError, msg):
                net.send(CLIENT, "create_job", dict({"kind": "ai", "model": "z-image", "spec_hash": H,
                                                     "escrow": 10 * price, "max_price": price}, **bad))
        job = net.send(CLIENT, "create_job", {"kind": "ai", "model": "z-image", "spec_hash": H,
                                              "escrow": 2 * price, "max_price": price, "units": 2})["id"]
        net.block()
        net.send(MB, "claim_job", {"job_id": job})
        net.block()
        for usage, msg in (({"tokens_out": 5}, "campos desconhecidos"), ({"units": 3}, "units")):
            with self.assertRaisesRegex(TxError, msg):
                net.send(MB, "submit_result", {"job_id": job, "result_hash": H, "usage": usage})
        before = net.state.bal(CLIENT.address)
        net.send(MB, "submit_result", {"job_id": job, "result_hash": H, "usage": {"units": 1}})   # 1 de 2
        s = net.block()
        j, m = s.jobs[job], s.models["z-image"]
        self.assertEqual((j["status"], j["cost"]), ("done", price))
        self.assertEqual(s.bal(CLIENT.address) - before, price)        # a imagem que faltou volta
        self.assertEqual((m["units"], m["tokens"], m["royalties"]), (1, 0, price * 1000 // 10000))
        self.assertEqual(supply(s), s.mining["minted"])

    def test_failed_media_job_charges_nothing(self):
        net = self.net
        s = self.pinned()
        price = s.machines[MB.address]["prices"]["ai_per_image"]
        job = net.send(CLIENT, "create_job", {"kind": "ai", "model": "z-image", "spec_hash": H,
                                              "escrow": price, "max_price": price, "units": 1})["id"]
        net.block()
        net.send(MB, "claim_job", {"job_id": job})
        net.block()
        with self.assertRaisesRegex(TxError, "usage deve ser zero"):
            net.send(MB, "submit_result", {"job_id": job, "result_hash": H, "usage": {"units": 1}, "failed": True})
        before = net.state.bal(CLIENT.address)
        net.send(MB, "submit_result", {"job_id": job, "result_hash": H, "usage": {"units": 0}, "failed": True})
        s = net.block()
        self.assertEqual((s.jobs[job]["status"], s.bal(CLIENT.address) - before), ("failed", price))

    def test_video_gets_more_time_to_deliver(self):
        # janelas de 10 min: a máquina sinaliza em cada uma (senão a rede a dá como desligada nas 2 h)
        self.net = net = v2(window_seconds=600, epoch_seconds=43200)
        net.fund(A.address, CLIENT.address)
        s = self.pinned("wan", task="video")
        price = s.machines[MB.address]["prices"]["ai_per_video_second"]
        job = net.send(CLIENT, "create_job", {"kind": "ai", "model": "wan", "spec_hash": H,
                                              "escrow": 3 * price, "max_price": price, "units": 3})["id"]
        net.block()
        net.send(MB, "claim_job", {"job_id": job})
        s = net.block()
        claimed = s.jobs[job]["claims"][MB.address]["time"]
        while net.time < claimed + chain.CLAIM_TIMEOUT + 60:    # passou o prazo do texto: continua dela
            s = net.step(MB)
        self.assertIn(MB.address, s.jobs[job]["claims"])
        while net.time < claimed + chain.MEDIA_CLAIM_TIMEOUT["video"] - 300:
            s = net.step(MB)
        self.assertIn(MB.address, s.jobs[job]["claims"])
        rep, before = s.machines[MB.address]["reputation"], s.bal(CLIENT.address)
        s = net.block(at=claimed + chain.MEDIA_CLAIM_TIMEOUT["video"] + 1)
        # passou o do vídeo: a máquina perde a vez (e reputação) e, com mais de OPEN_TIMEOUT de vida, o pedido
        # expira e o valor volta inteiro
        self.assertNotIn(MB.address, s.jobs[job]["claims"])
        self.assertEqual(s.machines[MB.address]["reputation"], rep - 1)
        self.assertEqual((s.jobs[job]["status"], s.bal(CLIENT.address) - before), ("expired", 3 * price))

    def test_text_job_on_v2_refuses_units(self):
        net = self.net
        net.send(A, "register_model", model("texto"))
        net.block()
        with self.assertRaisesRegex(TxError, "não units"):
            net.send(CLIENT, "create_job", {"kind": "ai", "model": "texto", "spec_hash": H, "escrow": 10 * UNIT,
                                            "max_price": 2000, "max_tokens": 100, "units": 1})

    def test_turning_images_off_frees_only_image_models(self):
        net = self.net
        self.pinned()
        net.send(A, "register_model", model("texto"))
        net.block()
        net.step(MB)
        net.send(MB, "pin_model", {"name": "texto"})
        s = net.block()
        self.assertEqual(sorted(s.machines[MB.address]["models"]), ["texto", "z-image"])
        self.host(MB, ["text"])                                    # desligou imagem
        s = net.block()
        self.assertEqual(s.machines[MB.address]["models"], ["texto"])
        self.assertNotIn(MB.address, s.models["z-image"]["pinners"])
        self.assertIn(MB.address, s.models["texto"]["pinners"])

    def test_text_engine_down_frees_only_text_models(self):
        """O motor de texto saiu do ar (o Ollama fechado): a máquina se registra só com imagem e perde só os
        modelos de texto; os de imagem, que rodam no stable-diffusion.cpp, ficam com ela (achado 9 da revisão)."""
        net = self.net
        self.pinned()
        net.send(A, "register_model", model("texto"))
        net.block()
        net.step(MB)
        net.send(MB, "pin_model", {"name": "texto"})
        s = net.block()
        self.assertEqual(sorted(s.machines[MB.address]["models"]), ["texto", "z-image"])
        self.host(MB, ["image"])
        s = net.block()
        mc = s.machines[MB.address]
        self.assertEqual((mc["ai"], mc["tasks"], mc["models"]), (True, ["image"], ["z-image"]))
        self.assertIn(MB.address, s.models["z-image"]["pinners"])
        self.assertNotIn(MB.address, s.models["texto"]["pinners"])
        s = net.step(MB)
        self.assertNotIn(MB.address, s.models["texto"]["assigned"])   # sem texto, o modelo de texto não volta
        self.assertIn(MB.address, s.models["z-image"]["pinners"])
        self.host(MB, ["text", "image"])                           # o motor de texto voltou
        net.block()
        s = net.step(MB)
        self.assertIn(MB.address, s.models["texto"]["assigned"])
        self.assertEqual(supply(s), s.mining["minted"])


class TestFoundersGrant(unittest.TestCase):
    """A carteira dos fundadores: criada uma vez só, na altura em que a v2 entra na rede principal."""
    F = WALLETS[7].address

    def upgrade_at(self, net, h, grants):
        gh = net.state.genesis_hash
        return (mock.patch.object(chain, "SCHEDULE", {gh: {2: h}}),
                mock.patch.object(chain, "UPGRADE_GRANTS", {gh: {2: grants}}))

    def run_to(self, net, h):
        while net.height < h:
            net.block()
        return net.state

    def test_grant_enters_once_at_the_v2_height(self):
        net = Net()
        net.fund(A.address)
        h = net.height + 3
        sched, grants = self.upgrade_at(net, h, [(self.F, 1_000_000 * UNIT)])
        with sched, grants:
            s = self.run_to(net, h - 1)
            self.assertEqual((s.rules, s.bal(self.F)), (1, 0))
            minted = s.mining["minted"]
            s = net.block()                                      # a altura da v2
            self.assertEqual((s.rules, s.bal(self.F)), (2, 1_000_000 * UNIT))
            self.assertGreaterEqual(s.mining["minted"] - minted, 1_000_000 * UNIT)   # mais a fatia da emissão
            self.assertIn([self.F, "founders", 1_000_000 * UNIT, "v2"], s.credits)
            self.assertEqual(s.ext["grants"], [{"address": self.F, "amount": 1_000_000 * UNIT, "rules": 2,
                                                "height": h}])
            self.assertEqual(supply(s), s.mining["minted"])      # oferta = mintado
            for _ in range(3):
                s = net.block()
            self.assertEqual(s.bal(self.F), 1_000_000 * UNIT)    # uma vez só

    def test_grant_never_passes_the_cap(self):
        net = Net(supply_cap=500 * UNIT)
        net.fund(A.address)
        sched, grants = self.upgrade_at(net, net.height + 2, [(self.F, 1_000_000 * UNIT)])
        with sched, grants:
            s = self.run_to(net, net.height + 2)
            self.assertLessEqual(s.mining["minted"], 500 * UNIT)
            self.assertEqual(supply(s), s.mining["minted"])

    def test_only_the_listed_network_gets_it(self):
        net = Net()
        with mock.patch.object(chain, "SCHEDULE", {net.state.genesis_hash: {2: 2}}):
            s = self.run_to(net, 3)                              # UPGRADE_GRANTS de verdade: só a rede principal
        self.assertEqual((s.rules, s.bal(chain.FOUNDERS), s.ext), (2, 0, {}))

    def test_mainnet_constants(self):
        self.assertTrue(chain.ADDRESS.match(chain.FOUNDERS))
        self.assertEqual(chain.FOUNDERS, "sig" + "fd1980713ecf1a514ad3f4a15828bb2d0c00a09e")
        self.assertEqual(chain.UPGRADE_GRANTS[chain.MAINNET_GENESIS], {2: [(chain.FOUNDERS, 1_000_000 * UNIT)]})


class TestEarlyVote(unittest.TestCase):
    """A partir da v2, uma votação termina antes dos 10 dias quando um lado passa de metade de todo o SIG."""

    def open_vote(self, net):
        net.fund(A.address)                                      # só A minerou: tem quase todo o SIG
        pid = net.send(A, "propose", {"kind": "param", "title": "Aluguel mais barato", "text": "",
                                      "param": "hosting_per_mb_window", "value": 3})["id"]
        net.block()
        return pid

    def test_more_than_half_of_all_sig_decides_now(self):
        net = v2()
        pid = self.open_vote(net)
        half = net.state.mining["minted"] // 2
        net.send(A, "vote", {"proposal_id": pid, "choice": "sim", "amount": half + 10 * UNIT})
        s = net.block()
        p = s.proposals[pid]
        self.assertEqual((p["status"], p.get("early"), s.params["hosting_per_mb_window"]), ("approved", True, 3))
        self.assertLess(s.now, p["ends"])                        # bem antes dos 10 dias
        self.assertEqual(supply(s), s.mining["minted"])          # o SIG travado voltou

    def test_half_or_less_waits_the_ten_days(self):
        net = v2()
        pid = self.open_vote(net)
        net.send(A, "vote", {"proposal_id": pid, "choice": "nao", "amount": net.state.mining["minted"] // 4})
        s = net.block()
        self.assertEqual(s.proposals[pid]["status"], "open")
        s = net.block(at=s.proposals[pid]["ends"] + 1)
        self.assertEqual((s.proposals[pid]["status"], s.proposals[pid].get("early")), ("rejected", None))

    def test_v1_always_waits(self):
        net = Net()
        pid = self.open_vote(net)
        net.send(A, "vote", {"proposal_id": pid, "choice": "sim", "amount": net.state.mining["minted"] // 2
                             + 10 * UNIT})
        s = net.block()
        self.assertEqual(s.proposals[pid]["status"], "open")     # a regra nova só vale da v2 em diante


if __name__ == "__main__":
    unittest.main()
