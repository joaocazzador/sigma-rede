"""Regras de consenso da v2.1 (sigma/chain.py + sigma/pow.py, docs/SPEC-v2.1.md §1, §5-§9).

    python -m unittest discover -s tests -v

Cada classe transforma achados da revisão (docs/revisao-v2-achados.md, id entre colchetes) em cenários de
ataque ou de falha que agora não acontecem mais. O passeio aleatório do fim roda todos os tipos de transação
com saltos de tempo além de todos os prazos e confere, bloco a bloco, a oferta, os saldos, o histórico e a
reexecução (com um snapshot JSON no meio do caminho).
"""
import json
import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain, evm  # noqa: E402
from sigma.chain import UNIT, State, TxError, canon, genesis_block, make_tx, sha, tx_id  # noqa: E402
from sigma.crypto import address_of, norm_address  # noqa: E402
from sigma.pow import pow_hash, share_ok, target  # noqa: E402
from test_chain import (FAST, H, T0, WALLETS, Net, apply_block, evm_tx, find_nonces, machine, model,  # noqa: E402
                        supply, work_tx, wp)

CLIENT, SPONSOR, OTHER = WALLETS[0], WALLETS[1], WALLETS[2]
MA, MB, MC, MD = WALLETS[10], WALLETS[11], WALLETS[12], WALLETS[13]
PAYOUTS = [WALLETS[3].address, WALLETS[4].address, WALLETS[5].address, WALLETS[6].address]
PA, PB, PC = PAYOUTS[:3]
SPEC = sha(b"pedido cifrado")


def hosted(*machines, payouts=None, replicas=None, **params):
    """Rede (janela de 60 s) com 'modelo-a' hospedado pelas máquinas, todas com chave pública publicada."""
    net = Net(**dict({"window_seconds": 60, "epoch_seconds": 600}, **params))
    net.fund(CLIENT.address, SPONSOR.address)
    for m, p in zip(machines, payouts or PAYOUTS):
        net.send(m, "register_machine", machine(p, pub=m.pub))
    net.send(SPONSOR, "register_model", model("modelo-a", replicas=replicas or len(machines)))
    net.block()
    net.step(*machines)
    for m in machines:
        net.send(m, "pin_model", {"name": "modelo-a"})
    net.block()
    return net


def ai_job(net, target=None, client=CLIENT, escrow=5000, max_price=2000, max_tokens=None, **extra):
    """Pedido de IA (direcionado quando há `target`); devolve o id. Entra no próximo bloco. max_tokens é público e
    o escrow precisa cobri-lo (achado escrow-need-not-cover-price): por padrão, o teto que o escrow já paga."""
    if max_tokens is None:
        max_tokens = max(1, escrow * 1000 // max_price - 16)
    d = {"kind": "ai", "model": "modelo-a", "spec_hash": SPEC, "escrow": escrow, "max_price": max_price,
         "max_tokens": max_tokens}
    if target is not None:
        d.update(machine=target.address, client_pub=client.pub)
    d.update(extra)
    return net.send(client, "create_job", {k: v for k, v in d.items() if v is not None})["id"]


def compute_job(net, replicas=3, client=CLIENT):
    return net.send(client, "create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 1000}},
                                           "replicas": replicas, "escrow": 500 * replicas, "max_price": 500})["id"]


# ------------------------------------------------------------------ formato estrito
class TestStrictFormat(unittest.TestCase):
    """[con-halt-unserializable-tx] [con-free-state-bloat] [eco-site-size-self-declared]"""
    KEY = WALLETS[7]

    def setUp(self):
        self.net = Net()
        self.net.fund(self.KEY.address, CLIENT.address)

    def refused(self, tx, pattern="."):
        root = self.net.pending.root()
        with self.assertRaisesRegex(TxError, pattern):
            self.net.submit(tx)
        self.assertEqual(self.net.pending.root(), root)       # nada mudou no estado pendente
        self.net.pending.root()

    def test_lone_surrogate_in_a_metamask_system_call_is_refused(self):
        payload = ('{"type":"register_machine","data":{"payout":"%s","name":"\\ud800"}}' % self.KEY.address).encode()
        self.refused(evm_tx(self.KEY.priv, 0, evm.SYSTEM_ADDRESS, data=payload))
        s = self.net.block()
        self.assertEqual(s.machines, {})
        s.root()                                               # o produtor continua fechando blocos

    def test_lone_surrogate_in_a_native_tx_is_a_tx_error(self):
        tx = {"type": "register_machine", "data": machine(CLIENT.address, name="\ud800"), "nonce": 0,
              "pub": CLIENT.pub, "sig": "11" * 64}
        self.refused(tx, "texto inválido")
        nan = make_tx(CLIENT, "transfer", {"to": OTHER.address, "amount": 1}, 0)
        nan["data"]["amount"] = float("nan")                   # NaN/Infinity não são JSON de verdade
        self.refused(nan, "malformada")

    def test_field_outside_the_evm_id_never_reaches_a_block(self):
        tx = evm_tx(self.KEY.priv, 0, "0x" + "22" * 20, evm.WEI_PER_USIG)
        for extra, pattern in (({"x": "\ud800"}, "texto inválido"), ({"x": 1}, "campos desconhecidos em evm: x")):
            self.refused({"type": "evm", "data": dict(tx["data"], **extra), "id": tx["id"]}, pattern)
        self.refused(dict(tx, memo="oi"), "campos desconhecidos na transação: memo")
        self.net.submit(tx)
        s = self.net.block()
        self.assertEqual(self.net.blocks[-1][3], [tx])
        s.root()

    def test_giant_integers_never_reach_state(self):
        net = self.net
        files = {f"f{i}": {"hash": H, "size": 10 ** 4299} for i in range(2)}   # a soma teria 4.301 dígitos
        for type_, data in (("deploy_site", {"name": "gigante", "manifest": files, "budget": 10 ** 6}),
                            ("transfer", {"to": OTHER.address, "amount": 10 ** 4299}),
                            ("create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 10}},
                                            "replicas": 1, "escrow": 10 ** 4299, "max_price": 1}),
                            ("register_model", model("modelo-g", size=10 ** 4299))):
            with self.subTest(type_):
                self.refused(make_tx(CLIENT, type_, data, net.pending.nonces.get(CLIENT.address, 0)))
        self.refused({"type": "transfer", "data": {"to": OTHER.address, "amount": 10 ** 5000}, "nonce": 0,
                      "pub": CLIENT.pub, "sig": "11" * 64}, "número grande demais")
        net.block().root()

    def test_integer_bounds(self):
        net, cap = self.net, self.net.state.params["supply_cap"]
        net.send(CLIENT, "register_model", model("modelo-b", budget=UNIT))
        net.block()
        site = {"name": "site-x", "manifest": {"a": {"hash": H, "size": 1}}}
        cases = [("transfer", {"to": OTHER.address, "amount": cap + 1}, "amount deve ser inteiro entre 1 e"),
                 ("transfer", {"to": OTHER.address, "amount": 0}, "amount deve ser inteiro entre 1 e"),
                 ("create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 10}}, "replicas": 1,
                                 "escrow": 500, "max_price": cap + 1}, "max_price deve ser inteiro entre 1 e"),
                 ("create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 10}}, "replicas": 1,
                                 "escrow": cap + 1, "max_price": 1}, "escrow deve ser inteiro entre 1 e"),
                 ("fund_model", {"name": "modelo-b", "amount": cap + 1}, "amount deve ser inteiro entre 1 e"),
                 ("register_model", model("modelo-c", budget=cap + 1), "budget deve ser inteiro entre 1 e"),
                 ("deploy_site", dict(site, budget=cap + 1),
                  f"budget deve ser inteiro entre {chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE} e"),
                 ("deploy_site", dict(site, budget=5), "um site novo precisa de um orçamento de pelo menos 12.000"),
                 ("deploy_site", dict(site, budget="10000"), "budget deve ser inteiro"),
                 ("register_machine", machine(CLIENT.address, hw={"cpu_cores": chain.HW_MAX + 1}), "hw.cpu_cores")]
        for type_, data, pattern in cases:
            with self.subTest(pattern), self.assertRaisesRegex(TxError, pattern):
                net.send(CLIENT, type_, data)
        net.send(CLIENT, "deploy_site", dict(site, budget=chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE))
        net.block()
        with self.assertRaisesRegex(TxError, f"budget deve ser inteiro entre 0 e {cap}"):
            net.send(CLIENT, "deploy_site", dict(site, budget=cap + 1))    # site existente: 0 a supply_cap
        net.send(CLIENT, "deploy_site", dict(site, budget=0))

    def test_unknown_fields_are_refused_for_every_type(self):
        net = self.net
        base = {"transfer": {"to": OTHER.address, "amount": 1}, "register_machine": machine(CLIENT.address),
                "ping": {}, "claim_job": {"job_id": H}, "submit_result": {"job_id": H, "result_hash": H},
                "cancel_job": {"job_id": H}, "deploy_site": {"name": "site-y", "manifest": {"a": {"hash": H, "size": 1}},
                                                            "budget": 10 ** 4},
                "pin_site": {"name": "site-y", "version": 1}, "register_model": model("modelo-y"),
                "fund_model": {"name": "modelo-y", "amount": 1}, "update_model": {"name": "modelo-y"},
                "retire_model": {"name": "modelo-y"}, "pin_model": {"name": "modelo-y"},
                "decline_model": {"name": "modelo-y"},
                "propose": {"kind": "text", "title": "Uma ideia", "text": "x"},
                "vote": {"proposal_id": H, "choice": "sim", "amount": 1}}
        self.assertEqual(set(base), set(chain.SIGNED_TYPES) - {"create_job"})
        for type_, data in base.items():
            with self.subTest(type_), self.assertRaisesRegex(TxError, f"campos desconhecidos em {type_}: lixo"):
                net.send(CLIENT, type_, dict(data, lixo="x" * 100_000))
        jobs = {"ai": {"kind": "ai", "model": "m", "spec_hash": H, "escrow": 1, "max_price": 1, "task": {}},
                "compute": {"kind": "compute", "task": {"name": "primes", "params": {"n": 10}}, "escrow": 1,
                            "max_price": 1, "model": "m"}}
        for kind, data in jobs.items():
            with self.subTest(kind), self.assertRaisesRegex(TxError, "campos desconhecidos em create_job"):
                net.send(CLIENT, "create_job", data)
        nested = [("register_machine", machine(CLIENT.address, hw={"tpu": 1}), "em hw: tpu"),
                  ("register_machine", machine(CLIENT.address, prices={"hosting_per_mb_window": 5}),
                   "em prices: hosting_per_mb_window"),
                  ("deploy_site", {"name": "site-z", "manifest": {"a": {"hash": H, "size": 1, "x": "y" * 9000}},
                                   "budget": 10 ** 4}, "exatamente hash e size"),
                  ("create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 10}, "x": "y"},
                                  "escrow": 1, "max_price": 1}, "na tarefa: x")]
        for type_, data, pattern in nested:
            with self.subTest(pattern), self.assertRaisesRegex(TxError, pattern):
                net.send(CLIENT, type_, data)
        w = net.pending.mining["window"]
        bad = work_tx(CLIENT.address, w, 2, [1])
        bad["data"]["junk"] = 1
        with self.assertRaisesRegex(TxError, "campos desconhecidos em work: junk"):
            net.submit({"type": "work", "data": bad["data"]})
        with self.assertRaisesRegex(TxError, "campos desconhecidos na transação: extra"):
            net.submit(dict(work_tx(CLIENT.address, w, 2, [1]), extra=1))

    def test_manifest_limits(self):
        net = self.net
        ok_path = "p" * chain.MAX_PATH
        # soma dos caminhos acima de 16 KB: MAX_SITE_FILES caminhos de MAX_PATH caracteres (cada um dentro do limite)
        muitos_caminhos = {("c" * (chain.MAX_PATH - 4) + f"{i:04d}"): {"hash": H, "size": 1}
                           for i in range(chain.MAX_SITE_FILES)}
        enorme = chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE * chain.MAX_SITE_FILES
        cases = [({"p" * (chain.MAX_PATH + 1): {"hash": H, "size": 1}}, 10 ** 6,
                  f"caminho longo demais \\(máximo {chain.MAX_PATH}"),
                 ({"": {"hash": H, "size": 1}}, 10 ** 6, "caminho inválido"),
                 ({"/a": {"hash": H, "size": 1}}, 10 ** 6, "caminho inválido"),
                 ({"a/../b": {"hash": H, "size": 1}}, 10 ** 6, "caminho inválido"),
                 ({f"f{i}": {"hash": H, "size": 1} for i in range(chain.MAX_SITE_FILES + 1)}, 10 ** 6,
                  f"de 1 a {chain.MAX_SITE_FILES}"),
                 (muitos_caminhos, enorme, f"a soma dos caminhos passa de {chain.MAX_SITE_PATHS // 1024} KB"),
                 ({"a": {"hash": H, "size": chain.MAX_FILE + 1}}, 10 ** 6, "size deve ser inteiro entre 0 e"),
                 ({"a": {"hash": H, "size": -1}}, 10 ** 6, "size"),
                 ({"a": {"hash": H}}, 10 ** 6, "exatamente hash e size"),
                 ({"a": {"hash": H, "size": 1}}, chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE - 1,
                  "um site novo precisa de um orçamento")]
        for manifest, budget, pattern in cases:
            with self.subTest(pattern), self.assertRaisesRegex(TxError, pattern):
                net.send(CLIENT, "deploy_site", {"name": "limites", "manifest": manifest, "budget": budget})
        big = {f"f{i}": {"hash": H, "size": chain.MAX_FILE} for i in range(chain.MAX_SITE_FILES)}
        net.send(CLIENT, "deploy_site", {"name": "limites", "manifest": big, "budget": enorme})
        net.send(CLIENT, "deploy_site", {"name": "caminho", "budget": chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE,
                                         "manifest": {ok_path: {"hash": H, "size": 0}}})
        s = net.block()
        self.assertEqual(s.sites["limites"]["size"], chain.MAX_SITE_FILES * chain.MAX_FILE)    # total declarado tem teto
        net.send(CLIENT, "deploy_site", {"name": "caminho", "budget": 0, "manifest": {ok_path: {"hash": H, "size": 0}}})
        net.block()

    def test_id_must_match_the_content(self):
        tx = make_tx(CLIENT, "transfer", {"to": OTHER.address, "amount": 1}, 0)
        self.refused(dict(tx, id="00" * 32), "id da transação não confere")
        self.refused(dict(tx, id="\ud800"), "texto inválido")
        work = work_tx(CLIENT.address, 0, 2, [1])
        self.refused(dict(work, id=H), "id da transação não confere")
        self.net.submit(tx)

    def test_addresses_are_strict(self):
        """norm_address aceita '_', espaço e dígitos de outros alfabetos (int(…, 16)); a chain não."""
        weird = ["sig" + "a" * 38 + "_b", "sig" + "a" * 39 + " ", "sig" + "١" * 40]
        for addr in weird:
            with self.subTest(addr=addr):
                try:
                    norm_address(addr)
                except ValueError:
                    pass
                with self.assertRaisesRegex(TxError, "inválido"):
                    self.net.send(CLIENT, "transfer", {"to": addr, "amount": 1})
                with self.assertRaisesRegex(TxError, "minerador"):
                    self.net.submit(work_tx(addr, 0, 2, [1]))

    def test_deep_nesting_is_a_tx_error(self):
        """RLP ou JSON com milhares de níveis estouravam RecursionError (não TxError) no nó e no histórico."""
        deep_rlp = {"type": "evm", "data": {"raw": "0x" + "c1" * 5000 + "80"}}
        deep_rlp["id"] = tx_id(deep_rlp)
        self.refused(deep_rlp, "aninhadas demais")
        self.assertEqual(chain.tx_summary(deep_rlp)["type"], "inválida")
        payload = b'{"type":"transfer","data":' + b"[" * 30_000 + b"]" * 30_000 + b"}"
        deep_json = evm_tx(self.KEY.priv, 0, evm.SYSTEM_ADDRESS, data=payload)
        bad_call = "aninhados|UTF-8 válido"                   # a chain ou o evm.system_call recusam, tanto faz
        self.refused(deep_json, bad_call)
        self.assertEqual(chain.tx_summary(deep_json)["type"], "inválida")
        weird = evm_tx(self.KEY.priv, 0, evm.SYSTEM_ADDRESS, data=b'{"type":"\\ud800","data":{}}')
        self.refused(weird, "texto inválido|" + bad_call)    # nem a mensagem de erro leva o texto quebrado
        json.dumps(chain.tx_summary(weird), ensure_ascii=False).encode()
        self.net.block().root()


    def test_reserved_site_names_are_refused(self):
        """[site-phishing-dominio-oficial] Nomes que poderiam se passar pela página oficial em /site/<nome>/ são
        reservados num site novo ('baixar', 'carteira', 'oficial', 'sigma', 'metamask'...). Um nome livre vale."""
        net = self.net
        budget = chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE
        manifest = {"i.html": {"hash": H, "size": 10}}
        for name in sorted(chain.RESERVED_SITE_NAMES):
            with self.subTest(name=name), self.assertRaisesRegex(TxError, "reservado pelo Sigma oficial"):
                net.send(CLIENT, "deploy_site", {"name": name, "manifest": manifest, "budget": budget})
        net.send(CLIENT, "deploy_site", {"name": "loja-do-ze", "manifest": manifest, "budget": budget})   # nome livre: ok
        s = net.block()
        self.assertIn("loja-do-ze", s.sites)


# ---------------------------------------------------------------------- mineração
class TestMiningV21(unittest.TestCase):
    """[eco-preempt-work-report] [eco-posthoc-bits] [copy-limite-por-ip-cgnat]"""
    VICTIM, ATTACKER = WALLETS[0].address, WALLETS[1].address

    def setUp(self):
        self.net = Net()
        self.seed = self.net.state.mining["seeds"]["0"]

    def test_a_cheap_preemptive_report_no_longer_voids_the_victims_window(self):
        net, seed, victim = self.net, self.seed, self.VICTIM
        net.submit(work_tx(victim, 0, 2, find_nonces(seed, victim, 2, 1)))     # 1 share barato no nome da vítima
        net.block()
        real = find_nonces(seed, victim, 2, 6)
        net.submit(work_tx(victim, 0, 2, real))                                 # o relatório de verdade entra
        with self.assertRaisesRegex(TxError, "tantos pontos ou mais"):
            net.submit(work_tx(victim, 0, 2, real[:3]))                         # cópia menor não apaga nada
        net.submit(work_tx(self.ATTACKER, 0, 2, find_nonces(seed, self.ATTACKER, 2, 6)))
        s = net.block()
        self.assertEqual(wp(s), {victim: 6 << 2, self.ATTACKER: 6 << 2})        # a vítima tem todo o seu trabalho
        rewards = net.pay()
        self.assertEqual(rewards[victim], rewards[self.ATTACKER])               # mesmo trabalho, mesma recompensa

    def test_bits_are_chosen_before_hashing(self):
        net, seed, a = self.net, self.seed, self.VICTIM
        at2 = {n for n in range(400) if share_ok(seed, a, n, 2)}
        at3 = {n for n in range(400) if share_ok(seed, a, n, 3)}
        self.assertFalse(at3 <= at2)            # com os bits fora do hash, todo share de 3 bits valeria com 2
        lucky = next(n for n in range(100_000)
                     if pow_hash(seed, a, 2, n) < target(6) and not share_ok(seed, a, n, 6))
        with self.assertRaisesRegex(TxError, "share inválido"):
            net.submit(work_tx(a, 0, 6, [lucky]))                       # não dá para "subir de nível" depois
        net.submit(work_tx(a, 0, 2, [lucky]))                           # vale só no nível em que foi calculado
        self.assertEqual(wp(net.block()), {a: 1 << 2})

    def test_several_pcs_mine_for_the_same_wallet(self):
        net, seed, a = self.net, self.seed, self.VICTIM
        w1, w2 = "a1b2c3d4", "0f0f0f0f"
        for worker in (w1, w2):
            net.submit(work_tx(a, 0, 2, find_nonces(seed, f"{a}.{worker}", 2, 3), worker=worker))
        net.submit(work_tx(a, 0, 2, find_nonces(seed, a, 2, 2)))       # e o endereço sozinho também
        s = net.block()
        self.assertEqual(wp(s), {a: 12 + 12 + 8})                    # os pontos vão sempre para a carteira
        self.assertEqual(s.mining["last_reports"], {f"{a}.{w1}": {"0": 12}, f"{a}.{w2}": {"0": 12}, a: {"0": 8}})
        self.assertEqual(s.miners[a]["last_rate"], 12 / 5 + 12 / 5 + 8 / 5)
        only_w1 = find_nonces(seed, f"{a}.{w1}", 2, 2, reject=lambda n: share_ok(seed, f"{a}.{w2}", n, 2)
                              or share_ok(seed, f"{a}.ffff", n, 2))
        with self.assertRaisesRegex(TxError, "share inválido"):
            net.submit(work_tx(a, 0, 2, only_w1, worker="ffff"))       # share de um PC não vale para outro
        for bad in ("A1B2", "", "1" * 17, 5, "zz", ["a"]):
            with self.subTest(worker=bad), self.assertRaisesRegex(TxError, "worker"):
                net.submit(work_tx(a, 0, 2, [1], worker=bad))
        tx = {"type": "work", "data": {"miner": a, "window": 0, "bits": 3, "nonces": find_nonces(seed, a, 3, 2),
                                       "worker": None}}
        net.submit(tx)                                                  # worker nulo = sem worker
        self.assertEqual(net.block().mining["last_reports"][a], {"0": 16})

    def test_window_seeds_never_repeat(self):
        """Antes, se o bloco 1 caísse na janela 1, as janelas 0 e 1 tinham a mesma seed (o hash do genesis)."""
        net, a = self.net, self.VICTIM
        late = work_tx(a, 0, 2, find_nonces(self.seed, a, 2, 2))
        s = net.block(at=T0 + 5.5)                                      # o primeiro bloco já abre a janela 1
        self.assertEqual(s.mining["seeds"], {"1": sha(f"{genesis_block(net.genesis)['hash']}:1".encode()),
                                             "0": self.seed})
        self.assertNotEqual(s.mining["seeds"]["0"], s.mining["seeds"]["1"])
        net.submit(late)
        moved = work_tx(a, 1, 2, late["data"]["nonces"])                # os mesmos nonces na outra janela
        if not all(share_ok(s.mining["seeds"]["1"], a, n, 2) for n in moved["data"]["nonces"]):
            with self.assertRaisesRegex(TxError, "share inválido"):
                net.submit(moved)

    def test_miner_record_rules(self):
        net, a = self.net, self.VICTIM
        late = work_tx(a, 0, 2, find_nonces(self.seed, a, 2, 3))      # 12 pontos da janela 0, enviado tarde
        s = net.next_window()
        net.submit(work_tx(a, 1, 2, find_nonces(s.mining["seeds"]["1"], a, 2, 2)))   # 8 pontos na janela 1
        net.submit(late)
        s = net.block()
        rec = s.miners[a]
        self.assertEqual((rec["points_total"], rec["last_window"], rec["last_rate"]), (20, 1, 8 / 5))   # janela velha: só pontos
        net.submit(work_tx(a, 1, 2, find_nonces(s.mining["seeds"]["1"], a, 2, 4)))   # melhor relatório da janela 1
        rec = net.block().miners[a]
        self.assertEqual((rec["points_total"], rec["last_window"], rec["last_rate"]), (28, 1, 8 / 5 + 8 / 5))


# -------------------------------------------------------------- pedidos direcionados
class TestTargetedJobs(unittest.TestCase):
    """[eco-fake-pins-mitigation] [fluxo-v21-maquina-dormindo-perde-modelo] [fluxo-ia-claim-preso-contexto]
    [ux-chat-sem-cancelar]"""

    def test_create_targeted_job_rules(self):
        net = hosted(MA, MB)
        net.send(MC, "register_machine", machine(PC))                           # sem pub e sem o modelo
        net.send(MD, "register_machine", machine(PAYOUTS[3], pub=MD.pub))       # com pub, sem o modelo
        net.send(MB, "register_machine", machine(PB, pub=MB.pub, ai=False))     # desligou a IA
        net.block()
        cases = [(WALLETS[14], {}, "não está registrada"),
                 (MB, {}, "IA desligada"),
                 (MC, {}, "não recebe pedidos cifrados"),
                 (MD, {}, "não hospeda este modelo"),
                 (MA, {"max_price": 1999}, "acima do máximo do pedido"),
                 (MA, {"client_pub": None}, "precisa de client_pub"),
                 (MA, {"client_pub": OTHER.pub}, "não corresponde à carteira"),
                 (MA, {"client_pub": "zz" * 64}, "128 caracteres"),
                 (MA, {"machine": "nada"}, "machine inválido")]
        for target, extra, pattern in cases:
            with self.subTest(pattern), self.assertRaisesRegex(TxError, pattern):
                ai_job(net, target, **extra)
        jid = ai_job(net, MA, client_pub=CLIENT.pub.upper())
        s = net.block()
        job = s.jobs[jid]
        self.assertEqual((job["machine"], job["client_pub"], job["window"], job["status"]),
                         (MA.address, CLIENT.pub, s.mining["window"], "open"))
        self.assertNotIn("missed", job)
        public = ai_job(net)                                                    # sem machine: como na v2
        s = net.block()
        self.assertEqual({k for k in ("machine", "client_pub") if k in s.jobs[public]}, set())
        self.assertEqual(s.jobs[public]["window"], s.mining["window"])

    def test_own_machine_cannot_be_the_target(self):
        net = hosted(MA, CLIENT, payouts=[PA, CLIENT.address])      # a carteira do cliente também é máquina
        with self.assertRaisesRegex(TxError, "sua própria máquina"):
            ai_job(net, CLIENT)

    def test_only_the_target_can_claim(self):
        net = hosted(MA, MB)
        jid = ai_job(net, MA)
        net.block()
        with self.assertRaisesRegex(TxError, "este job é para outra máquina"):
            net.send(MB, "claim_job", {"job_id": jid})
        net.send(MA, "claim_job", {"job_id": jid})
        self.assertEqual(net.block().jobs[jid]["status"], "running")

    def test_sleeping_machine_cannot_be_targeted(self):
        """A máquina que dormiu continua 'online' por 2 janelas, mas não recebe pedidos direcionados."""
        net = hosted(MA, window_seconds=300, epoch_seconds=3000)
        s = net.next_window()                                       # MA não sinaliza nesta janela
        self.assertEqual(s.machines[MA.address]["last_seen"], s.mining["window"] - 1)
        jid = ai_job(net, MA)                                       # janela anterior: ainda vale
        s = net.block()
        self.assertEqual(s.jobs[jid]["machine"], MA.address)
        s = net.next_window()
        self.assertTrue(s.machines[MA.address]["online"])
        self.assertEqual((s.jobs[jid]["status"], s.jobs[jid]["missed"]), ("expired", True))   # não respondeu
        with self.assertRaisesRegex(TxError, "não está online agora"):
            ai_job(net, MA)                                         # duas janelas sem sinal: ninguém mais escolhe
        self.assertEqual(s.models["modelo-a"]["pinners"][MA.address]["misses"], 1)   # uma falta, não três

    def test_unclaimed_targeted_job_expires_and_counts_a_miss(self):
        net = hosted(MA)
        jid = ai_job(net, MA)
        s = net.block()
        created, before = s.jobs[jid]["created"], s.bal(CLIENT.address)
        self.assertEqual(net.block(at=created + chain.TARGET_TIMEOUT).jobs[jid]["status"], "open")
        s = net.block(at=created + chain.TARGET_TIMEOUT + 1)
        job = s.jobs[jid]
        self.assertEqual((job["status"], job["missed"]), ("expired", True))
        self.assertEqual(s.bal(CLIENT.address) - before, 5000)                 # devolvido em 2 minutos, não 1 hora
        self.assertIn([CLIENT.address, "refund", 5000, jid], s.credits)
        pin = s.models["modelo-a"]["pinners"][MA.address]
        self.assertEqual((pin["misses"], pin["miss_window"]), (1, s.mining["window"]))
        self.assertEqual(s.machines[MA.address]["reputation"], -1)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_claimed_targeted_job_expires_without_reopening(self):
        net = hosted(MA, window_seconds=300, epoch_seconds=3000)    # 10 min sem sinal não derrubam a máquina
        jid = ai_job(net, MA)
        net.block()
        net.send(MA, "claim_job", {"job_id": jid})
        s = net.block()
        claimed = s.jobs[jid]["claims"][MA.address]["time"]
        self.assertEqual(net.block(at=claimed + chain.CLAIM_TIMEOUT).jobs[jid]["status"], "running")
        s = net.block(at=claimed + chain.CLAIM_TIMEOUT + 1)
        self.assertEqual((s.jobs[jid]["status"], s.jobs[jid]["missed"]), ("expired", True))   # não reabre
        self.assertEqual(s.models["modelo-a"]["pinners"][MA.address]["misses"], 1)

    def test_at_most_one_miss_per_window_and_three_cost_the_model(self):
        net = hosted(MA, window_seconds=300, epoch_seconds=3000)    # o prazo de 120 s cabe numa janela
        jids = [ai_job(net, MA) for _ in range(chain.MAX_TARGET_PER_CLIENT)]   # o teto por cliente, sem resposta
        s = net.block()
        s = net.block(at=s.now + chain.TARGET_TIMEOUT + 1)
        self.assertEqual({s.jobs[j]["status"] for j in jids}, {"expired"})
        self.assertEqual(s.models["modelo-a"]["pinners"][MA.address]["misses"], 1)   # vários vencidos, uma falta só
        self.assertEqual(s.machines[MA.address]["reputation"], -1)
        for _ in range(2):                                          # mais duas faltas, em janelas diferentes
            net.step(MA)
            ai_job(net, MA)
            s = net.block()
            s = net.block(at=s.now + chain.TARGET_TIMEOUT + 1)
        w, m = s.mining["window"], s.models["modelo-a"]
        self.assertNotIn(MA.address, m["pinners"])
        self.assertEqual(s.machines[MA.address]["models"], [])
        self.assertEqual(m["cooldown"], {MA.address: w + chain.COOLDOWN_WINDOWS})
        self.assertEqual(s.machines[MA.address]["reputation"], -3)
        self.assertEqual(net.step(MA).models["modelo-a"]["assigned"], {})          # de castigo: não recebe de volta
        # castigo cumprido (encurtado aqui para não esperar ~1 dia — e nesse tempo o modelo sem pinner já expiraria):
        # a rede volta a atribuir o modelo a MA e o cooldown some
        net.state.models["modelo-a"]["cooldown"] = {MA.address: net.state.mining["window"]}
        net.pending = net.state.clone()
        s = net.step(MA)
        self.assertEqual(list(s.models["modelo-a"]["assigned"]), [MA.address])
        self.assertEqual(s.models["modelo-a"]["cooldown"], {})

    def test_delivered_targeted_job_resets_the_misses(self):
        net = hosted(MA, window_seconds=300, epoch_seconds=3000)
        for _ in range(2):
            ai_job(net, MA)
            s = net.block()
            net.block(at=s.now + chain.TARGET_TIMEOUT + 1)
            net.step(MA)
        self.assertEqual(net.state.models["modelo-a"]["pinners"][MA.address]["misses"], 2)
        jid = ai_job(net, MA)
        net.block()
        net.send(MA, "claim_job", {"job_id": jid})
        net.send(MA, "submit_result", {"job_id": jid, "result_hash": H, "usage": {"tokens_in": 50, "tokens_out": 50}})
        s = net.block()
        self.assertEqual(s.jobs[jid]["status"], "done")
        self.assertEqual(s.models["modelo-a"]["pinners"][MA.address]["misses"], 0)

    def test_failed_answer_refunds_at_once_and_punishes_nobody(self):
        net = hosted(MA)
        jid = ai_job(net, MA)
        net.block()
        net.send(MA, "claim_job", {"job_id": jid})
        s = net.block()
        before, rep = s.bal(CLIENT.address), s.machines[MA.address]["reputation"]
        for usage in ({"tokens_in": 1, "tokens_out": 0}, {"tokens_in": 0, "tokens_out": 7}):
            with self.subTest(usage=usage), self.assertRaisesRegex(TxError, "usage deve ser zero"):
                net.send(MA, "submit_result", {"job_id": jid, "result_hash": H, "usage": usage, "failed": True})
        with self.assertRaisesRegex(TxError, "failed deve ser"):
            net.send(MA, "submit_result", {"job_id": jid, "result_hash": H, "failed": 1})
        err = sha(b"erro cifrado")
        net.send(MA, "submit_result", {"job_id": jid, "result_hash": err, "failed": True,
                                       "usage": {"tokens_in": 0, "tokens_out": 0}})
        s = net.block()
        job = s.jobs[jid]
        self.assertEqual((job["status"], job["result_hash"], job["cost"]), ("failed", err, 0))
        self.assertEqual(job["results"][MA.address]["failed"], True)
        self.assertEqual(s.bal(CLIENT.address) - before, 5000)                 # na hora, inteiro
        mc = s.machines[MA.address]
        self.assertEqual((mc["jobs_failed"], mc["jobs_done"], mc["reputation"]), (1, 0, rep))
        self.assertEqual(s.models["modelo-a"]["pinners"][MA.address]["misses"], 0)
        self.assertEqual(s.credits, [[CLIENT.address, "refund", 5000, jid]])

    def test_usage_is_bounded_for_every_job(self):
        """[con-halt-unserializable-tx]: tokens com teto (a soma em model["tokens"] nunca vira um inteiro gigante)."""
        net = hosted(MA)
        jid, cid = ai_job(net, MA), compute_job(net, replicas=1)
        net.block()
        net.send(MA, "claim_job", {"job_id": jid})
        net.send(MA, "claim_job", {"job_id": cid})
        net.block()
        for usage, pattern in (({"tokens_in": chain.MAX_TOKENS + 1}, "tokens_in deve ser inteiro entre 0 e"),
                               ({"tokens_out": -1}, "tokens_out"), ({"tokens_in": True}, "tokens_in"),
                               ({"cache": 5}, "campos desconhecidos em usage: cache"), ([], "usage inválido")):
            for job in (jid, cid):
                with self.subTest(usage=usage, job=job), self.assertRaisesRegex(TxError, pattern):
                    net.send(MA, "submit_result", {"job_id": job, "result_hash": H, "usage": usage})
        net.send(MA, "submit_result", {"job_id": jid, "result_hash": H,
                                       "usage": {"tokens_in": chain.MAX_TOKENS, "tokens_out": chain.MAX_TOKENS}})
        net.send(MA, "submit_result", {"job_id": cid, "result_hash": H})
        s = net.block()
        self.assertEqual((s.jobs[jid]["status"], s.jobs[jid]["cost"]), ("done", 5000))   # o escrow é o teto
        self.assertEqual(s.models["modelo-a"]["tokens"], 2 * chain.MAX_TOKENS)
        self.assertEqual(s.jobs[cid]["status"], "done")

    def test_failed_is_only_for_ai(self):
        net = hosted(MA)
        cid = compute_job(net, replicas=1)
        net.block()
        net.send(MA, "claim_job", {"job_id": cid})
        net.block()
        with self.assertRaisesRegex(TxError, "só pedidos de IA"):
            net.send(MA, "submit_result", {"job_id": cid, "result_hash": H, "failed": True})

    def test_cancel_job(self):
        net = hosted(MA)
        jid, other = ai_job(net, MA), ai_job(net)
        net.block()
        before = net.state.bal(CLIENT.address)
        with self.assertRaisesRegex(TxError, "só quem pediu pode cancelar"):
            net.send(SPONSOR, "cancel_job", {"job_id": jid})
        with self.assertRaisesRegex(TxError, "job não existe"):
            net.send(CLIENT, "cancel_job", {"job_id": "00" * 32})
        net.send(CLIENT, "cancel_job", {"job_id": jid})
        net.send(MA, "claim_job", {"job_id": other})
        s = net.block()
        self.assertEqual(s.jobs[jid]["status"], "canceled")
        self.assertEqual(s.bal(CLIENT.address) - before, 5000)
        self.assertEqual(s.credits, [[CLIENT.address, "refund", 5000, jid]])
        with self.assertRaisesRegex(TxError, "um computador já está respondendo"):
            net.send(CLIENT, "cancel_job", {"job_id": other})
        with self.assertRaisesRegex(TxError, "já foi encerrado"):
            net.send(CLIENT, "cancel_job", {"job_id": jid})
        with self.assertRaisesRegex(TxError, "não está aberto"):
            net.send(MA, "claim_job", {"job_id": jid})
        self.assertEqual(supply(s), s.mining["minted"])


    def test_targeted_queue_is_capped(self):
        """[targeted-flood-evicts-honest-host] A fila de pedidos direcionados é limitada (MAX_TARGET_PER_CLIENT por
        cliente, MAX_TARGET_OPEN na máquina): um cliente não derruba a máquina honesta inundando-a de faltas, pois
        ela sempre consegue reivindicar e responder dentro do prazo."""
        net = Net(window_seconds=60, epoch_seconds=600)
        users = WALLETS[0:4]
        net.fund(*[u.address for u in users])
        net.send(MA, "register_machine", machine(PA, pub=MA.pub))
        net.send(users[0], "register_model", model("modelo-a", replicas=1))
        net.block()
        net.step(MA)
        net.send(MA, "pin_model", {"name": "modelo-a"})
        net.block()
        for _ in range(chain.MAX_TARGET_PER_CLIENT):                # o teto por cliente
            ai_job(net, MA, client=users[0])
        with self.assertRaisesRegex(TxError, "pedidos demais nessa máquina"):
            ai_job(net, MA, client=users[0])
        live, c = chain.MAX_TARGET_PER_CLIENT, 1
        while live < chain.MAX_TARGET_OPEN:                         # outros clientes enchem a fila da máquina
            ai_job(net, MA, client=users[c])
            live += 1
            if live % chain.MAX_TARGET_PER_CLIENT == 0:
                c += 1
        with self.assertRaisesRegex(TxError, "fila cheia agora"):   # cheia para qualquer cliente novo
            ai_job(net, MA, client=users[c])
        net.block()

    def test_a_busy_machine_that_answered_is_not_missed(self):
        """[targeted-flood-evicts-honest-host] Se a máquina entregou algum direcionado nesta janela, está viva, só
        ocupada: um direcionado que vence na mesma janela não conta falta (campo answered_window)."""
        net = hosted(MA, window_seconds=300, epoch_seconds=3000)
        a, b = ai_job(net, MA), ai_job(net, MA)                     # a será respondido; b vence sem resposta
        net.block()
        net.send(MA, "claim_job", {"job_id": a})
        net.send(MA, "submit_result", {"job_id": a, "result_hash": H, "usage": {"tokens_in": 10, "tokens_out": 10}})
        s = net.block()
        self.assertEqual((s.jobs[a]["status"], s.machines[MA.address]["answered_window"]), ("done", s.mining["window"]))
        rep = s.machines[MA.address]["reputation"]
        s = net.block(at=s.now + chain.TARGET_TIMEOUT + 1)          # b vence nesta mesma janela
        self.assertEqual(s.jobs[b]["status"], "expired")
        self.assertEqual(s.models["modelo-a"]["pinners"][MA.address]["misses"], 0)   # respondeu na janela: sem falta
        self.assertEqual(s.machines[MA.address]["reputation"], rep)                  # e sem perder reputação

    def test_escrow_must_cover_the_price(self):
        """[escrow-need-not-cover-price] O escrow precisa cobrir o preço: IA reserva ceil((max_tokens+16)×preço/1000)
        e computação units×preço×réplicas. Assim ninguém recebe uma resposta inteira (ou a computação de todas as
        cópias) pagando 1 µSIG, e a máquina honesta nunca trabalha de graça sem saber."""
        net = hosted(MA)
        need = chain._ceil_div((1000 + 16) * 2000, 1000)
        with self.assertRaisesRegex(TxError, "o valor reservado precisa cobrir"):
            ai_job(net, MA, escrow=need - 1, max_price=2000, max_tokens=1000)
        ai_job(net, MA, escrow=need, max_price=2000, max_tokens=1000)               # no limite exato: passa
        units = 11                                                                  # primes n=1.000.000
        with self.assertRaisesRegex(TxError, "o valor reservado precisa cobrir"):
            net.send(CLIENT, "create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 1_000_000}},
                                            "replicas": 3, "escrow": units * 500 * 3 - 1, "max_price": 500})
        net.send(CLIENT, "create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 1_000_000}},
                                        "replicas": 3, "escrow": units * 500 * 3, "max_price": 500})
        net.block()


# ------------------------------------------------------------------- reputação
class TestReputation(unittest.TestCase):
    """[eco-fake-pins-mitigation]: reputação não se fabrica com jobs para si mesmo, de custo zero ou em série."""

    def run_jobs(self, net, n, tokens=500, client=CLIENT):
        for _ in range(n):
            jid = ai_job(net, MA, client=client)
            net.block()
            net.send(MA, "claim_job", {"job_id": jid})
            net.send(MA, "submit_result", {"job_id": jid, "result_hash": H,
                                           "usage": {"tokens_in": tokens, "tokens_out": 0}})
            net.block()
        return net.state.machines[MA.address]

    def test_self_dealt_jobs_give_no_reputation(self):
        net = hosted(MA, payouts=[CLIENT.address])          # a máquina paga a própria carteira
        mc = self.run_jobs(net, 5)
        self.assertEqual((mc["jobs_done"], mc["reputation"]), (5, 0))

    def test_zero_cost_jobs_give_no_reputation(self):
        mc = self.run_jobs(hosted(MA), 5, tokens=0)
        self.assertEqual((mc["jobs_done"], mc["reputation"], mc["earned"]), (5, 0, 0))

    def test_at_most_one_point_per_window(self):
        net = hosted(MA)
        mc = self.run_jobs(net, 3)
        self.assertEqual((mc["jobs_done"], mc["reputation"], mc["rep_window"]), (3, 1, net.state.mining["window"]))
        net.step(MA)
        self.assertEqual(self.run_jobs(net, 2)["reputation"], 2)


# ------------------------------------------------------------------ computação
class TestComputeV21(unittest.TestCase):
    """[eco-compute-dispute-wipes-points] [eco-compute-escrow-stuck]"""
    HONEST, SYBIL1, SYBIL2 = WALLETS[10], WALLETS[13], WALLETS[14]

    def setUp(self):
        self.net = Net(window_seconds=60, epoch_seconds=600)
        self.net.fund(CLIENT.address)

    def machines(self, *pairs):
        for w, p in pairs:
            self.net.send(w, "register_machine", machine(p))
        self.net.mine(PA)                                   # o dono honesto também minera nesta época
        self.net.block()

    def vote(self, jid, *pairs):
        for w, _ in pairs:
            self.net.send(w, "claim_job", {"job_id": jid})
        self.net.block()
        for w, h in pairs:
            self.net.send(w, "submit_result", {"job_id": jid, "result_hash": h})
        return self.net.block()

    def test_client_sybils_cannot_outvote_an_honest_host(self):
        right, bogus = sha(b'{"count":168}'), sha(b'{"count":0}')
        self.machines((self.HONEST, PA), (self.SYBIL1, CLIENT.address), (self.SYBIL2, CLIENT.address))
        jid = compute_job(self.net)
        self.net.block()
        points = wp(self.net.state)[PA]
        before = self.net.state.bal(CLIENT.address)
        s = self.vote(jid, (self.HONEST, right), (self.SYBIL1, bogus), (self.SYBIL2, bogus))
        self.assertEqual((s.jobs[jid]["status"], s.jobs[jid]["cost"]), ("disputed", 0))   # votos do cliente não contam
        self.assertEqual(s.bal(CLIENT.address) - before, 1500)
        self.assertEqual(s.machines[self.HONEST.address]["reputation"], 0)
        self.assertEqual(wp(s)[PA], points)                                 # a mineração do honesto fica intacta
        self.assertEqual(supply(s), s.mining["minted"])

    def test_one_vote_per_payout(self):
        right, bogus = sha(b'{"count":168}'), sha(b'{"count":0}')
        self.machines((self.HONEST, PA), (self.SYBIL1, PB), (self.SYBIL2, PB))
        jid = compute_job(self.net)
        self.net.block()
        s = self.vote(jid, (self.HONEST, right), (self.SYBIL1, bogus), (self.SYBIL2, bogus))
        self.assertEqual(s.jobs[jid]["status"], "disputed")                    # 1 a 1: não há maioria de 3
        self.assertEqual([s.machines[w.address]["reputation"] for w in (self.HONEST, self.SYBIL1, self.SYBIL2)],
                         [0, 0, 0])

    def test_partial_results_are_settled_at_open_timeout(self):
        right = sha(b'{"count":168}')
        self.machines((self.HONEST, PA), (self.SYBIL1, PB), (self.SYBIL2, PC))
        jid = compute_job(self.net)
        s = self.net.block()
        created = s.jobs[jid]["created"]
        for w in (self.HONEST, self.SYBIL1, self.SYBIL2):
            self.net.send(w, "claim_job", {"job_id": jid})
        self.net.block()
        for w in (self.HONEST, self.SYBIL1):                    # a terceira máquina some
            self.net.send(w, "submit_result", {"job_id": jid, "result_hash": right})
        s = self.net.block()
        self.assertEqual(s.jobs[jid]["status"], "running")     # 2 de 3: ainda espera a terceira
        before = {a: s.bal(a) for a in (PA, PB, CLIENT.address)}
        s = self.net.block(at=created + chain.OPEN_TIMEOUT + 1)
        job = s.jobs[jid]
        self.assertEqual((job["status"], job["result_hash"], job["cost"]), ("done", right, 1000))
        mint = {a: x for a, k, x, _ in s.credits if k == "mining"}                     # o salto também fechou a época (PA minerou)
        self.assertEqual({a: s.bal(a) - before[a] - mint.get(a, 0) for a in before},
                         {PA: 500, PB: 500, CLIENT.address: 500})              # quem entregou recebe, o resto volta
        self.assertEqual(s.machines[self.SYBIL2.address]["reputation"], -1)    # a reivindicação vencida custa 1
        self.assertEqual(sorted(c for c in s.credits if c[1] != "mining"),
                         sorted([[PA, "job", 500, jid], [PB, "job", 500, jid], [CLIENT.address, "change", 500, jid]]))
        self.assertEqual(supply(s), s.mining["minted"])

    def test_partial_without_majority_is_refunded(self):
        self.machines((self.HONEST, PA), (self.SYBIL1, PB))
        jid = compute_job(self.net, replicas=2)
        s = self.net.block()
        created = s.jobs[jid]["created"]
        for w in (self.HONEST, self.SYBIL1):
            self.net.send(w, "claim_job", {"job_id": jid})
        self.net.block()
        self.net.send(self.HONEST, "submit_result", {"job_id": jid, "result_hash": H})
        s = self.net.block()
        before = s.bal(CLIENT.address)
        s = self.net.block(at=created + chain.OPEN_TIMEOUT + 1)
        self.assertEqual(s.jobs[jid]["status"], "disputed")                    # escrow não fica preso para sempre
        self.assertEqual(s.bal(CLIENT.address) - before, 1000)
        self.assertIn([CLIENT.address, "refund", 1000, jid], s.credits)


    def test_sybils_with_distinct_payouts_do_not_drain_the_honest_host(self):
        """[compute-vote-sybil-rep-drain] Duas máquinas Sybil do atacante, com payouts distintos (nenhum é o cliente),
        vencem o voto e divergem da honesta. A honesta não recebe, mas NÃO perde reputação (senão o atacante a derruba
        de graça, até ela nunca mais receber modelo): só conta uma disputa, que o painel mostra e _assign_models ignora."""
        right, bogus = sha(b'{"count":168}'), sha(b'{"count":0}')
        self.machines((self.HONEST, PA), (self.SYBIL1, PB), (self.SYBIL2, PC))     # payouts distintos, nenhum é o cliente
        jid = compute_job(self.net)
        self.net.block()
        rep0 = self.net.state.machines[self.HONEST.address]["reputation"]
        s = self.vote(jid, (self.HONEST, right), (self.SYBIL1, bogus), (self.SYBIL2, bogus))
        job = s.jobs[jid]
        self.assertEqual((job["status"], job["result_hash"]), ("done", bogus))     # 2 Sybils formam maioria e vencem
        self.assertEqual(s.machines[self.HONEST.address]["reputation"], rep0)      # a honesta NÃO perde reputação
        self.assertEqual(s.machines[self.HONEST.address]["compute_disputes"], 1)   # só uma disputa (o painel mostra)
        self.assertEqual([s.machines[w.address]["compute_disputes"] for w in (self.SYBIL1, self.SYBIL2)], [0, 0])
        self.assertEqual(supply(s), s.mining["minted"])


# ------------------------------------------------------------------ preços
class TestPrices(unittest.TestCase):
    """[eco-ai-max-price-from-priciest-host] [eco-host-price-ignored-by-assignment]"""

    def test_absurd_prices_are_refused(self):
        net = Net()
        for prices in ({"ai_per_1k_tokens": 100_000_000}, {"compute_per_unit": 10 ** 12}):
            with self.subTest(prices=prices), self.assertRaisesRegex(TxError, "no máximo 1.000.000"):
                net.send(MA, "register_machine", machine(PA, prices=prices))
        net.send(MA, "register_machine", machine(PA, prices={"ai_per_1k_tokens": chain.MAX_AI_PRICE,
                                                             "compute_per_unit": chain.MAX_COMPUTE_PRICE}))
        net.block()

    def test_every_pinner_earns_the_network_price(self):
        net = Net(hosting_per_mb_window=3)
        net.fund(SPONSOR.address)
        net.send(MA, "register_machine", machine(PA))
        net.send(MB, "register_machine", machine(PB))
        net.send(SPONSOR, "register_model", model("modelo-a", size=2_000_000, replicas=2, budget=UNIT))
        net.block()
        net.step(MA, MB)
        for m in (MA, MB):
            net.send(m, "pin_model", {"name": "modelo-a"})
        net.block()
        s = net.next_window()
        self.assertEqual((s.bal(PA), s.bal(PB)), (6, 6))                        # 2 MB × 3 µSIG, igual para todos


# ------------------------------------------------------------------ atribuição
class TestAssignmentV21(unittest.TestCase):
    """[eco-fake-pins-mitigation] [fluxo-modelo-apagado-ao-perder-atribuicao] [seg-model-arbitrary-gguf]"""

    def setUp(self):
        self.net = Net()
        self.net.fund(SPONSOR.address)

    def test_at_most_four_models_per_machine(self):
        net = self.net
        net.send(MA, "register_machine", machine(PA, hw={"ai_ram_mb": chain.MAX_AI_RAM_MB}))
        for i in range(6):
            net.send(SPONSOR, "register_model", model(f"modelo-{i}"))
        net.block()
        s = net.next_window()
        self.assertEqual(sum(MA.address in m["assigned"] for m in s.models.values()), chain.MAX_MODELS_PER_MACHINE)

    def test_model_goes_back_first_to_who_had_the_file(self):
        net, ms = self.net, WALLETS[10:16]
        for i, m in enumerate(ms):
            net.send(m, "register_machine", machine(WALLETS[3 + i % 4].address))
        net.send(SPONSOR, "register_model", model("modelo-a"))
        s = net.block()
        seed_of_next = None
        # quem seria o último no sorteio da próxima janela? ele é quem já teve o arquivo
        probe = s.clone()
        probe.begin_block(net.height + 1, s.mining["window_start"] + s.params["window_seconds"], net.prev)
        seed_of_next = probe.mining["seeds"][str(probe.mining["window"])]
        order = sorted((m.address for m in ms), key=lambda a: sha((seed_of_next + "modelo-a" + a).encode()))
        last = order[-1]
        net.state.models["modelo-a"]["former"] = {last: s.mining["window"]}
        net.pending = net.state.clone()
        s = net.next_window()
        self.assertEqual(probe.models["modelo-a"]["assigned"], {order[0]: probe.mining["window"]})   # sem former
        self.assertEqual(s.models["modelo-a"]["assigned"], {last: s.mining["window"]})

    def test_unhost_fills_former_and_former_is_trimmed(self):
        net = self.net
        net.send(MA, "register_machine", machine(PA))
        net.send(SPONSOR, "register_model", model("modelo-a", replicas=1))
        net.block()
        net.step(MA)
        net.send(MA, "pin_model", {"name": "modelo-a"})
        net.block()
        net.send(MA, "register_machine", machine(PA, ai=False))          # desligou a IA para jogar
        s = net.block()
        w = s.mining["window"]
        self.assertEqual(s.models["modelo-a"]["former"], {MA.address: w})
        m = net.state.models["modelo-a"]
        m["former"] = {f"sig{i:040x}": w - i for i in range(5)}           # 5 antigas: ficam as 2 mais novas
        m["former"][MB.address] = w - chain.FORMER_WINDOWS - 1           # mais de um dia: sai
        net.pending = net.state.clone()
        s = net.next_window()
        self.assertEqual(s.models["modelo-a"]["former"], {f"sig{0:040x}": w, f"sig{1:040x}": w - 1})

    def test_offline_machine_goes_to_former(self):
        net = self.net
        net.send(MA, "register_machine", machine(PA))
        net.send(SPONSOR, "register_model", model("modelo-a"))
        net.block()
        net.step(MA)
        net.send(MA, "pin_model", {"name": "modelo-a"})
        net.block()
        for _ in range(3):
            s = net.next_window()
        self.assertFalse(s.machines[MA.address]["online"])
        self.assertEqual(s.models["modelo-a"]["former"], {MA.address: s.mining["window"]})

    def test_bad_model_files_do_not_drain_reputation(self):
        """'Modelos' cujo arquivo nunca confere, contra máquinas sempre no ar: antes cada atribuição vencida
        custava 1 de reputação, para sempre, até nenhuma máquina receber mais modelo; agora os modelos saem da
        rede na terceira falha e ninguém é punido."""
        net = self.net
        ms = WALLETS[10:13]
        for i, m in enumerate(ms):
            net.send(m, "register_machine", machine(PAYOUTS[i]))
        for i in range(4):
            net.send(SPONSOR, "register_model", model(f"falso-{i}", size=1, budget=1, replicas=1))
        net.block()
        # cada isca é atribuída a uma máquina nova a cada vez (as que venceram ficam em cooldown daquele arquivo);
        # três vagas vencidas apagam a isca. Ninguém perde reputação: deixar vencer um arquivo ruim não pune.
        for _ in range(4 * (chain.ASSIGN_WINDOWS + 1)):
            s = net.step(*ms)
        self.assertEqual(s.models, {})
        self.assertEqual([s.machines[m.address]["reputation"] for m in ms], [0, 0, 0])
        self.assertEqual(supply(s), s.mining["minted"])

    def test_a_good_pin_clears_old_failures(self):
        """Alguém conseguiu hospedar: o arquivo é bom. As falhas antigas (máquinas que deixaram a atribuição vencer,
        cada uma indo para o cooldown daquele arquivo) somem quando enfim uma máquina dá o pin, então um modelo que
        funciona não é apagado por tentativas antigas."""
        net = self.net
        ms = [MA, MB, MC]
        for i, m in enumerate(ms):
            net.send(m, "register_machine", machine(PAYOUTS[i]))
        net.send(SPONSOR, "register_model", model("modelo-a", budget=5 * UNIT, replicas=1))
        net.block()
        # duas máquinas deixam a atribuição vencer (vão para cooldown); failed chega a 2 (sem punir ninguém)
        for _ in range(3 * (chain.ASSIGN_WINDOWS + 1)):
            net.step(*ms)
            if net.state.models["modelo-a"]["failed"] >= 2:
                break
        s = net.state
        self.assertEqual(s.models["modelo-a"]["failed"], 2)
        assigned = list(s.models["modelo-a"]["assigned"])              # a terceira máquina, agora atribuída
        self.assertEqual(len(assigned), 1)
        winner = next(m for m in ms if m.address == assigned[0])
        net.send(winner, "pin_model", {"name": "modelo-a"})            # conseguiu baixar e conferir: o arquivo é bom
        s = net.block()
        self.assertEqual(s.models["modelo-a"]["failed"], 0)            # o pin zera as falhas antigas
        self.assertEqual(list(s.models["modelo-a"]["pinners"]), [winner.address])
        self.assertIn("modelo-a", s.models)                            # sem zerar, duas falhas antigas + uma nova apagariam
        self.assertEqual([s.machines[m.address]["reputation"] for m in ms], [0, 0, 0])
        self.assertEqual(supply(s), s.mining["minted"])


    def test_decline_model_frees_the_slot_without_punishing(self):
        """[poison-model-rep-drain] A máquina recusa o arquivo atribuído (sha/GGUF errado, URL interna, sem orçamento):
        a vaga é liberada na hora e entra em cooldown, sem tirar reputação nem contar como 'ninguém deu pin'. Só quem
        tem a atribuição pode recusar — assim um modelo-isca não vira arma para drenar reputação."""
        net = self.net
        net.send(MA, "register_machine", machine(PA))
        net.send(MB, "register_machine", machine(PB))
        net.send(SPONSOR, "register_model", model("modelo-a", replicas=1))
        net.block()
        s = net.next_window()
        assignee = next(iter(s.models["modelo-a"]["assigned"]))
        decliner, other = (MA, MB) if assignee == MA.address else (MB, MA)
        with self.assertRaisesRegex(TxError, "a rede não atribuiu este modelo a esta máquina"):
            net.send(other, "decline_model", {"name": "modelo-a"})       # quem não tem a atribuição não recusa
        with self.assertRaisesRegex(TxError, "modelo não existe"):
            net.send(decliner, "decline_model", {"name": "nao-existe"})
        net.send(decliner, "decline_model", {"name": "modelo-a"})
        s = net.block()
        m = s.models["modelo-a"]
        self.assertNotIn(decliner.address, m["assigned"])                # vaga liberada na hora
        self.assertEqual(m["cooldown"], {decliner.address: s.mining["window"] + chain.COOLDOWN_WINDOWS})
        self.assertEqual((m["failed"], s.machines[decliner.address]["reputation"]), (0, 0))   # sem punição
        self.assertEqual(supply(s), s.mining["minted"])


# --------------------------------------------------------------------- limpeza
class TestPruning(unittest.TestCase):
    """[con-free-state-bloat] [sec-machine-registration-state-bloat]"""

    def setUp(self):
        self.net = Net()
        self.net.fund(SPONSOR.address, CLIENT.address)

    def jump(self, windows):
        s = self.net.state
        return self.net.block(at=s.mining["window_start"] + windows * s.params["window_seconds"])

    def test_silent_machines_leave_the_state(self):
        net = self.net
        for m in (MA, MB):
            net.send(m, "register_machine", machine(PA))
        net.block()
        cid = compute_job(net, replicas=2)
        net.block()
        net.send(MB, "claim_job", {"job_id": cid})                       # MB segura uma reivindicação
        s = net.block()
        seen = s.machines[MA.address]["last_seen"]
        s = self.jump(3)
        self.assertEqual([s.machines[m.address]["online"] for m in (MA, MB)], [False, False])
        s = self.jump(seen + chain.PRUNE_WINDOWS + 1 - s.mining["window"])
        self.assertNotIn(MA.address, s.machines)                        # uma semana sem sinal e sem referência
        self.assertIn(MB.address, s.machines)                           # ainda citada num job aberto
        self.assertEqual(s.jobs[cid]["status"], "expired")              # o prazo do job fecha no fim do bloco
        s = net.next_window()
        self.assertNotIn(MB.address, s.machines)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_pruned_machine_registers_again(self):
        """Uma semana sem sinal e sem nada guardado: a máquina sai do estado. O host recebe 'máquina não
        registrada' no ping e registra de novo (o registro começa do zero)."""
        net = self.net
        net.send(MA, "register_machine", machine(PA, ai=False))
        net.block()
        s = self.jump(chain.PRUNE_WINDOWS + 5)
        self.assertNotIn(MA.address, s.machines)
        with self.assertRaisesRegex(TxError, "máquina não registrada"):
            net.send(MA, "ping", {})
        net.send(MA, "register_machine", machine(PA, ai=False))
        s = net.block()
        rec = s.machines[MA.address]
        self.assertEqual((rec["last_seen"], rec["online"], rec["since"]), (s.mining["window"], True, s.now))

    def test_targeted_job_keeps_its_machine(self):
        net = hosted(MA)
        jid = ai_job(net, MA)
        s = net.block()
        st = net.state
        st.machines[MA.address]["last_seen"] = st.mining["window"] - chain.PRUNE_WINDOWS - 5   # como se dormisse
        st.models["modelo-a"]["pinners"].clear()
        st.machines[MA.address]["models"] = []
        net.pending = st.clone()
        s = net.next_window()
        self.assertIn(MA.address, s.machines)                           # é o alvo de um pedido aberto
        self.assertEqual(s.jobs[jid]["status"], "open")

    def test_idle_miner_records_leave_but_not_with_points(self):
        net = self.net
        a, b = WALLETS[8].address, WALLETS[9].address
        net.mine(a)
        net.block()
        self.assertTrue(net.pay())                                      # a recebeu e parou de minerar
        last = net.state.miners[a]["last_window"]
        s = self.jump(last + chain.PRUNE_WINDOWS - net.state.mining["window"])
        self.assertIn(a, s.miners)                                      # ainda não passou uma semana
        net.mine(b)                                                     # b relata na janela de agora
        s = net.next_window()
        self.assertNotIn(a, s.miners)                                   # a passou da semana sem relatar: sai
        self.assertIn(b, s.miners)                                      # b tem pontos numa janela aberta: fica

    def test_unpinned_site_is_pruned_and_refunded(self):
        net = self.net
        net.send(SPONSOR, "deploy_site", {"name": "ninguem-hospeda", "budget": 50_000,
                                          "manifest": {"a": {"hash": H, "size": 10}}})
        s = net.block()
        before = s.bal(SPONSOR.address)
        s = self.jump(chain.UNPINNED_SITE_WINDOWS)                      # ~1 dia sem ninguém hospedar, não uma semana
        self.assertIn("ninguem-hospeda", s.sites)
        s = net.next_window()
        self.assertNotIn("ninguem-hospeda", s.sites)
        self.assertEqual(s.bal(SPONSOR.address) - before, 50_000)       # o orçamento volta inteiro
        self.assertIn([SPONSOR.address, "budget", 50_000, "ninguem-hospeda"], s.credits)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_expired_site_is_pruned_a_week_later(self):
        net = self.net
        files = {f"p{i}": {"hash": H, "size": 20_000_000} for i in range(30)}   # 600 MB: 3.000 µSIG por janela
        budget = chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE * len(files) + 1   # cobre o mínimo e sobra um pouco
        net.send(SPONSOR, "deploy_site", {"name": "expira", "budget": budget, "manifest": files, "replicas": 1})
        net.send(MA, "register_machine", machine(PA))
        net.block()
        net.send(MA, "pin_site", {"name": "expira", "version": 1})
        net.block()
        fee = 600 * 5                                                   # 600 MB × 5 µSIG/MB por janela, 1 cópia
        paid = budget // fee                                           # janelas pagas; a próxima não cabe e expira
        for _ in range(paid + 1):
            s = net.step(MA)
        site = s.sites["expira"]
        left = budget - paid * fee
        self.assertEqual((site["status"], site["budget"], site["expired_window"]), ("expired", left, s.mining["window"]))
        before, w = s.bal(SPONSOR.address), site["expired_window"]
        s = self.jump(w + chain.PRUNE_WINDOWS - s.mining["window"])
        self.assertIn("expira", s.sites)
        s = net.next_window()
        self.assertNotIn("expira", s.sites)
        self.assertEqual(s.bal(SPONSOR.address) - before, left)         # o resto do orçamento volta ao dono
        self.assertIn([SPONSOR.address, "budget", left, "expira"], s.credits)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_revived_site_is_not_pruned_right_away(self):
        """Site expirado e recarregado pelo dono: a contagem da limpeza recomeça (antes ele sumia na hora)."""
        net = self.net
        files = {f"p{i}": {"hash": H, "size": 20_000_000} for i in range(30)}
        budget = chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE * len(files) + 1
        net.send(SPONSOR, "deploy_site", {"name": "volta", "budget": budget, "manifest": files, "replicas": 1})
        net.send(MA, "register_machine", machine(PA))
        net.block()
        net.send(MA, "pin_site", {"name": "volta", "version": 1})
        net.block()
        for _ in range(budget // (600 * 5) + 1):
            s = net.step(MA)
        self.assertEqual(s.sites["volta"]["status"], "expired")
        s = self.jump(chain.PRUNE_WINDOWS)                              # quase uma semana depois
        net.send(SPONSOR, "deploy_site", {"name": "volta", "budget": 50_000, "manifest": files, "replicas": 1})
        s = net.block()
        site = s.sites["volta"]
        self.assertEqual((site["status"], site["pinned_window"]), ("active", s.mining["window"]))
        self.assertNotIn("expired_window", site)
        s = net.next_window()
        self.assertEqual(s.sites["volta"]["status"], "active")         # ainda tem uma semana para alguém hospedar

    def test_unpinned_model_is_deleted(self):
        net = self.net
        net.send(SPONSOR, "register_model", model("sem-maquina", budget=UNIT))
        s = net.block()
        before = s.bal(SPONSOR.address)
        self.assertIn("sem-maquina", self.jump(chain.UNPINNED_SITE_WINDOWS).models)   # ~1 dia sem pinner: ainda fica
        s = net.next_window()
        self.assertNotIn("sem-maquina", s.models)
        self.assertIn("sem-maquina", s.retired_models)
        self.assertEqual(s.bal(SPONSOR.address) - before, UNIT)

    def test_retired_names_are_forgotten_after_a_week(self):
        net = self.net
        net.send(SPONSOR, "register_model", model("aposentado"))
        net.block()
        net.send(SPONSOR, "retire_model", {"name": "aposentado"})
        s = net.block()
        t = s.retired_models["aposentado"]
        s = net.block(at=t + chain.RETIRED_KEEP)
        self.assertIn("aposentado", s.retired_models)                  # 7 dias exatos: ainda fica
        s = net.next_window()
        self.assertNotIn("aposentado", s.retired_models)

    def test_hosted_things_survive_a_long_producer_outage(self):
        """Produtor parado por mais de uma semana: o primeiro bloco não apaga os modelos e sites que estavam no
        ar, nem a máquina que os guardava; ela ganha a janela para sinalizar e o modelo volta para ela."""
        net = hosted(MA)
        net.send(MB, "register_machine", machine(PB, ai=False))         # só computação: não guarda nada
        net.send(SPONSOR, "deploy_site", {"name": "no-ar", "budget": UNIT, "manifest": {"a": {"hash": H, "size": 10}}})
        net.block()
        net.send(MA, "pin_site", {"name": "no-ar", "version": 1})
        net.block()
        s = self.jump_net(net, chain.PRUNE_WINDOWS + 50)
        self.assertIn("modelo-a", s.models)
        self.assertIn("no-ar", s.sites)
        self.assertIn(MA.address, s.machines)                           # guardava modelo e site: ganha uma janela
        self.assertNotIn(MB.address, s.machines)                        # não guardava nada (§8: sai na hora)
        self.assertFalse(s.machines[MA.address]["online"])
        self.assertEqual(s.models["modelo-a"]["former"], {MA.address: s.mining["window"]})
        net.send(MA, "ping", {})
        net.block()
        s = net.next_window()
        self.assertIn(MA.address, s.machines)
        self.assertEqual(s.models["modelo-a"]["assigned"], {MA.address: s.mining["window"]})   # volta para quem tinha
        self.assertEqual(supply(s), s.mining["minted"])

    @staticmethod
    def jump_net(net, windows):
        s = net.state
        return net.block(at=s.mining["window_start"] + windows * s.params["window_seconds"])


# ------------------------------------------------------------------- créditos
class TestCredits(unittest.TestCase):
    """[ux-historico-incompleto]: todo crédito que não vem de transação nem de mineração fica anotado."""

    def test_credits_are_transient(self):
        net = hosted(MA)
        s = net.next_window()
        self.assertTrue(s.credits)                                      # aluguel desta janela
        root = s.root()
        s.credits.append(["sig" + "00" * 20, "rent", 1, "x"])
        self.assertEqual(s.root(), root)
        self.assertNotIn("credits", s.to_dict())
        self.assertEqual(net.block().credits, [])                       # zera a cada bloco
        restored = State.from_dict(net.genesis, json.loads(json.dumps(s.to_dict())))
        self.assertEqual(restored.credits, [])

    def test_ai_job_credits(self):
        net = hosted(MA)
        net.state.models["modelo-a"].update(royalty_bps=1000, reinvest_bps=5000)
        net.pending = net.state.clone()
        jid = ai_job(net, MA)
        net.block()
        net.send(MA, "claim_job", {"job_id": jid})
        net.send(MA, "submit_result", {"job_id": jid, "result_hash": H, "usage": {"tokens_in": 100, "tokens_out": 400}})
        s = net.block()
        # 500 tokens × 2000 = 1000: 900 para a máquina, royalty 100 (50 reinvestidos), troco 4000
        self.assertEqual(s.credits, [[PA, "job", 900, jid], [SPONSOR.address, "royalty", 50, "modelo-a"],
                                     [CLIENT.address, "change", 4000, jid]])


# -------------------------------------------------------------- passeio aleatório
class Walk:
    """Um pouco de tudo, ao acaso (semente fixa): o produtor deixa de fora o que ficou inválido, como o nó.
    Janela de 60 s: os prazos de 120 s (pedido direcionado) e 600 s (reivindicação) passam de uma janela, e os
    saltos de tempo maiores derrubam as máquinas, fecham épocas e chegam à limpeza semanal."""
    USERS = WALLETS[0:4]
    MACHINES = WALLETS[10:15]
    LAZY = WALLETS[11].address      # hospeda, sinaliza, mas nunca pega pedido direcionado: leva faltas e castigo
    MODELS = ["mod-a", "mod-b", "mod-c", "mod-d"]
    SITES = ["site-a", "site-b", "site-c"]
    KINDS = {"rent", "job", "royalty", "change", "refund", "budget", "mining", "vote", "deposit"}

    def __init__(self, seed, test):
        self.rng, self.t = random.Random(seed), test
        self.genesis = {"network": "sigma", "chain_id": 51515, "time": T0, "validators": [], "alloc": {},
                        "params": dict(FAST, window_seconds=60, epoch_seconds=600)}
        self.state = State(self.genesis)
        self.pending = self.state.clone()
        self.queue, self.blocks = [], []
        self.height, self.time = 0, T0
        self.prev = genesis_block(self.genesis)["hash"]
        users = [u.address for u in self.USERS]
        # a máquina 0 paga a carteira de um cliente (jobs para si mesmo); 2 e 3 dividem o mesmo payout (Sybils)
        self.payout = dict(zip([m.address for m in self.MACHINES],
                               [users[0], WALLETS[5].address, WALLETS[6].address, WALLETS[6].address, users[3]]))
        self.ledger = {}            # saldo esperado: mineração + créditos + o que as próprias txs movem
        self.seen = {}
        self.rep, self.job_claims, self.disputes = {}, {}, {}   # p/ o invariante da reputação (compute/poison)

    # ------------------------------------------------------------ infraestrutura
    def note(self, key):
        self.seen[key] = self.seen.get(key, 0) + 1

    def submit(self, tx):
        try:
            self.pending.apply_tx(tx)
        except TxError:
            self.note("recusada")
            return None
        self.queue.append(tx)
        return tx

    def send(self, wallet, type_, data):
        return self.submit(make_tx(wallet, type_, data, self.pending.nonces.get(wallet.address, 0)))

    def block(self, dt):
        s = self.state
        before = {"machines": set(s.machines), "sites": set(s.sites), "models": set(s.models),
                  "miners": set(s.miners), "retired": set(s.retired_models)}
        self.height += 1
        self.time += dt
        s.begin_block(self.height, self.time, self.prev)
        settled = s.last_settlement
        included = []
        for tx in self.queue:
            try:
                s.apply_tx(tx)
                included.append(tx)
            except TxError:             # ficou inválida depois da virada de janela: o produtor deixa de fora
                self.note("fora do bloco")
        s.end_block()
        self.queue = []
        root = s.root()
        self.blocks.append((self.height, self.time, self.prev, included, root))
        self.prev = sha(canon([self.height, self.time, root]))
        self.pending = s.clone()
        self.check(s, settled, included, before)

    def flows(self, tx, s):
        """O que a transação move de saldo por conta própria (o resto vem da mineração e de `credits`)."""
        type_, d = tx["type"], tx["data"]
        txid = chain.tx_id(tx)
        if type_ == "work":
            return {}
        if type_ == "evm":
            e = chain.decode_evm(d["raw"])
            if e["to"] != evm.SYSTEM_ADDRESS:
                amount = e["value"] // evm.WEI_PER_USIG
                return {e["from"]: -amount, norm_address(e["to"]): amount}
            sender, (type_, d) = e["from"], evm.system_call(e["data"])
        else:
            sender = address_of(tx["pub"])
        if type_ == "transfer":
            to = norm_address(d["to"])
            return {sender: -d["amount"], to: d["amount"]} if to != sender else {}
        if type_ == "propose":                                           # o depósito fica travado na votação
            return {sender: -s.proposals[txid]["deposit"]} if txid in s.proposals else {}
        if type_ == "vote":                                              # os votos ficam travados até o fim
            return {sender: -d["amount"]}
        key = {"create_job": "escrow", "deploy_site": "budget", "register_model": "budget", "fund_model": "amount"}
        return {sender: -d.get(key[type_], 0)} if type_ in key else {}

    def check(self, s, settled, included, before):
        t = self.t
        t.assertEqual(supply(s), s.mining["minted"], f"oferta divergiu no bloco {self.height}")
        t.assertTrue(all(v >= 0 for v in s.balances.values()), "saldo negativo")
        t.assertTrue(all(x["budget"] >= 0 for x in [*s.sites.values(), *s.models.values()]), "orçamento negativo")
        t.assertTrue(all(j["escrow"] > 0 for j in s.jobs.values()), "escrow inválido")
        online = {a for a, mc in s.machines.items() if mc["online"]}
        count = {}
        for m in s.models.values():                                     # nenhuma referência pendurada
            t.assertTrue(set(m["pinners"]) | set(m["assigned"]) <= online, "modelo com máquina sumida ou offline")
            t.assertEqual(sorted(a for a, mc in s.machines.items() if m["name"] in mc["models"]), sorted(m["pinners"]))
            t.assertFalse(set(m["pinners"]) & set(m["assigned"]))
            for a in [*m["pinners"], *m["assigned"]]:
                count[a] = count.get(a, 0) + 1
        t.assertTrue(all(n <= chain.MAX_MODELS_PER_MACHINE for n in count.values()), "máquina com modelos demais")
        t.assertTrue(all(set(mc["models"]) <= set(s.models) for mc in s.machines.values()))
        for site in s.sites.values():
            t.assertTrue(set(site["pinners"]) <= online)
            t.assertEqual(site["status"] == "expired", "expired_window" in site)
        for j in s.jobs.values():
            if j["status"] in chain.LIVE:
                t.assertTrue(set(j["results"]) <= set(j["claims"]) <= set(s.machines))
                if j.get("machine"):
                    t.assertIn(j["machine"], s.machines)
                    t.assertTrue(set(j["claims"]) <= {j["machine"]})
        t.assertTrue(set(s.points) <= set(s.miners))
        for addr, r in (settled or {}).get("rewards", {}).items():
            self.ledger[addr] = self.ledger.get(addr, 0) + r
        for tx in included:
            for addr, v in self.flows(tx, s).items():
                self.ledger[addr] = self.ledger.get(addr, 0) + v
        for addr, kind, amount, ref in s.credits:
            t.assertIn(kind, self.KINDS)
            t.assertTrue(isinstance(amount, int) and amount > 0)
            if kind == "mining":                                        # ref = período de 12 h
                t.assertTrue(isinstance(ref, int) and not isinstance(ref, bool))
            elif kind in ("job", "change", "refund", "vote", "deposit"):   # ref = id do job ou da votação
                t.assertTrue(isinstance(ref, str) and chain.HEX64.match(ref), ref)
            else:                                                       # ref = nome do site ou do modelo
                t.assertTrue(chain.MODEL_NAME.match(ref) or chain.SITE_NAME.match(ref), ref)
            self.ledger[addr] = self.ledger.get(addr, 0) + amount
            self.note("crédito " + kind)
        json.dumps([s.credits, [chain.tx_summary(tx) for tx in included]], ensure_ascii=False).encode()   # o nó indexa
        t.assertEqual({a: v for a, v in self.ledger.items() if v}, {a: v for a, v in s.balances.items() if v},
                      f"histórico não fecha com o saldo no bloco {self.height}")
        if self.height % 7 == 0:                                        # snapshot JSON exato (sem tuplas, sem chaves int)
            d = s.to_dict()
            t.assertEqual(json.loads(json.dumps(d)), d)
        for key, now in (("machines", s.machines), ("sites", s.sites), ("models", s.models), ("miners", s.miners),
                         ("retired", s.retired_models)):
            if before[key] - set(now):
                self.note(f"saiu {key}")
        for j in s.jobs.values():
            if j.get("closed") == s.now:
                self.note("job " + j["status"])
                if j.get("missed"):
                    self.note("falta")
        for prop in s.proposals.values():
            if prop.get("closed") == s.now:
                self.note("votação " + prop["status"])
                if prop.get("applied"):
                    self.note("parâmetro mudou")
        t.assertTrue(all(lo <= s.params[k] <= hi for k, (lo, hi) in chain.GOVERNABLE.items()
                         if k != "min_share_bits"), "parâmetro fora dos limites")
        for m in s.models.values():
            if m["cooldown"]:
                self.note("castigo")
            if m["former"]:
                self.note("former")
        # achados compute-vote-sybil-rep-drain e poison-model-rep-drain: divergir numa votação de computação (Sybils
        # de payouts distintos) ou deixar uma atribuição vencer NUNCA tira reputação — só falta (direcionado vencido)
        # e timeout de reivindicação tiram. Então toda queda de reputação casa, no mesmo bloco, com uma dessas (ou
        # com um (re)registro, que zera o cadastro).
        punished = set()
        for j in s.jobs.values():
            if j.get("closed") == s.now and j["status"] == "expired" and j.get("machine"):
                punished.add(j["machine"])                              # pedido direcionado vencido = falta
        for jid, j in s.jobs.items():
            claims = set(j.get("claims", {}))
            punished.update(self.job_claims.get(jid, set()) - claims)   # perdeu a reivindicação (timeout) = -1
            self.job_claims[jid] = claims
        for a, mc in s.machines.items():
            if mc.get("since") == s.now:                                # (re)registrou agora: a reputação zera
                punished.add(a)
            if mc.get("compute_disputes", 0) > self.disputes.get(a, 0):
                self.note("disputa")                                    # divergiu de uma maioria de computação
            self.disputes[a] = mc.get("compute_disputes", 0)
            prev = self.rep.get(a)
            if prev is not None and mc["reputation"] < prev:
                t.assertIn(a, punished, f"reputação de {a} caiu sem falta/timeout no bloco {self.height} "
                                        f"(disputa de computação ou atribuição vencida não podem punir)")
            self.rep[a] = mc["reputation"]

    # ------------------------------------------------------------------ ações
    def mine(self):
        p, r = self.pending, self.rng
        for addr in r.sample([u.address for u in self.USERS] + [WALLETS[5].address], r.randint(1, 3)):
            window = r.choice(sorted(int(w) for w in p.mining["seeds"]))
            worker = r.choice([None, None, "a1", "b2c3"])
            ident = addr if worker is None else f"{addr}.{worker}"
            nonces = find_nonces(p.mining["seeds"][str(window)], ident, 2, r.randint(1, 4))
            self.submit(work_tx(addr, window, 2, nonces, worker=worker))

    def register(self):
        r = self.rng
        m = r.choice(self.MACHINES)
        self.send(m, "register_machine", machine(
            self.payout[m.address], ai=r.random() < 0.85, compute=r.random() < 0.8,
            prices={"ai_per_1k_tokens": r.choice([1000, 2000, 2000, 4000]), "compute_per_unit": r.choice([300, 500])},
            hw={"ai_ram_mb": r.choice([600, 2000, 8000]), "disk_mb": r.choice([2, 600, 50_000])},
            **({"pub": m.pub} if r.random() < 0.9 else {})))

    def ping(self):
        w = self.pending.mining["window"]
        for m in self.MACHINES:
            mc = self.pending.machines.get(m.address)
            if mc and mc["last_seen"] != w and (m.address == self.LAZY or self.rng.random() < 0.85):
                self.send(m, "ping", {})

    def host(self):
        """O que o host faz sozinho: confirma (pin) o modelo que a rede atribuiu, recusa (decline) o que não confere,
        ou deixa vencer; e guarda sites ativos."""
        r, p = self.rng, self.pending
        by_addr = {m.address: m for m in self.MACHINES}
        for name, m in sorted(p.models.items()):
            for addr in sorted(m["assigned"]):
                roll = r.random()
                if roll < 0.55:
                    self.send(by_addr[addr], "pin_model", {"name": name})         # baixou e conferiu o sha256
                elif roll < 0.72:
                    self.send(by_addr[addr], "decline_model", {"name": name})     # recusa (sha/GGUF/rede/orçamento): sem punir
        for name, site in sorted(p.sites.items()):
            if site["status"] == "active" and r.random() < 0.3:
                self.send(r.choice(self.MACHINES), "pin_site", {"name": name, "version": site["version"]})

    def transfer(self):
        r = self.rng
        a, b = r.sample(self.USERS, 2)
        if r.random() < 0.2:            # pela MetaMask
            bal = self.pending.bal(a.address)
            if bal > 10:
                nonce = self.pending.nonces.get(a.address, 0)
                self.submit(evm_tx(a.priv, nonce, "0x" + b.address[3:], r.randint(1, 10) * evm.WEI_PER_USIG))
            return
        self.send(a, "transfer", {"to": b.address, "amount": r.randint(1, 3 * UNIT)})

    def models(self):
        r, p = self.rng, self.pending
        name = r.choice(self.MODELS)
        m = p.models.get(name)
        if m is None:
            sponsor = r.choice(self.USERS)
            self.send(sponsor, "register_model", model(
                name, size=r.choice([1, 1_000_000, 3_000_000, 4_000_000_000]), replicas=r.randint(1, 3),
                budget=r.choice([30, 2_000, 200_000, 10 * UNIT]), royalty_bps=r.choice([0, 1000, 5000]),
                reinvest_bps=r.choice([0, 5000, 10000])))
            return
        sponsor = next(u for u in self.USERS if u.address == m["sponsor"]) if m["sponsor"] in \
            [u.address for u in self.USERS] else r.choice(self.USERS)
        action = r.choice(["fund", "update", "retire", "pin", "pin", "pin"])
        if action == "fund":
            self.send(sponsor, "fund_model", {"name": name, "amount": r.randint(1, 50_000)})
        elif action == "update":
            self.send(sponsor, "update_model", {"name": name, "royalty_bps": r.choice([0, 2500]),
                                                "new_sponsor": r.choice(self.USERS).address})
        elif action == "retire" and r.random() < 0.3:
            self.send(sponsor, "retire_model", {"name": name})
        for addr in sorted(m["assigned"]):
            if r.random() < 0.8:
                self.send(next(x for x in self.MACHINES if x.address == addr), "pin_model", {"name": name})

    def sites(self):
        r, p = self.rng, self.pending
        name = r.choice(self.SITES)
        site = p.sites.get(name)
        owner = next((u for u in self.USERS if site and u.address == site["owner"]), r.choice(self.USERS))
        if site is None or r.random() < 0.3:
            manifest = {f"f{i}.html": {"hash": sha(f"{name}{i}{r.randint(0, 2)}".encode()),
                                       "size": r.choice([10, 3000, 2_000_000, 20_000_000])}
                        for i in range(r.randint(1, 3))}
            # site novo: orçamento mínimo proporcional ao manifest (achado site-manifest-state-bloat-cheap); às vezes
            # 1 µSIG abaixo do mínimo, que é recusado
            budget = (chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE * len(manifest) + r.choice([-1, 0, 20_000])
                      if site is None else r.choice([0, 500]))
            self.send(owner, "deploy_site", {"name": name, "manifest": manifest, "replicas": r.randint(1, 3),
                                             "budget": budget})
            return
        for m in r.sample(self.MACHINES, 2):
            self.send(m, "pin_site", {"name": name, "version": site["version"]})

    def jobs(self):
        r, p = self.rng, self.pending
        client = r.choice(self.USERS)
        if r.random() < 0.3:
            replicas = r.randint(1, 3)
            self.send(client, "create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 1000}},
                                             "replicas": replicas, "escrow": 500 * replicas, "max_price": 500})
            return
        hosted_ = [(n, a) for n, m in sorted(p.models.items()) for a in sorted(m["pinners"])]
        name, target = r.choice(hosted_) if hosted_ and r.random() < 0.9 else (r.choice(self.MODELS), None)
        d = {"kind": "ai", "model": name, "spec_hash": sha(str(r.random()).encode()),
             "escrow": r.choice([1, 500, 5000, 20_000]), "max_price": r.choice([2000, 4000]),
             "max_tokens": r.choice([1, 100, 1000, 4000])}   # o escrow precisa cobrir max_tokens: uns passam, outros não
        if target and r.random() < 0.7:
            d.update(machine=target, client_pub=client.pub)
        self.send(client, "create_job", d)

    def probe(self):
        """Pedido direcionado à máquina preguiçosa (o padrinho conferindo quem diz guardar o modelo): ela nunca
        responde, então junta faltas, uma por janela, até perder o modelo e ficar de castigo."""
        r, p = self.rng, self.pending
        hosted_ = [n for n, m in sorted(p.models.items()) if self.LAZY in m["pinners"]]
        if hosted_:
            client = r.choice(self.USERS)
            self.send(client, "create_job", {"kind": "ai", "model": r.choice(hosted_), "spec_hash": sha(b"teste"),
                                             "escrow": 100, "max_price": 4000, "max_tokens": 1, "machine": self.LAZY,
                                             "client_pub": client.pub})

    def work_jobs(self):
        r, p = self.rng, self.pending
        for jid in sorted(p.jobs):
            job = p.jobs[jid]
            if job["status"] not in chain.LIVE or r.random() < 0.3:
                continue
            if r.random() < 0.12:
                self.send(next(u for u in self.USERS if u.address == job["client"]), "cancel_job", {"job_id": jid})
                continue
            for m in self.MACHINES:
                a = m.address
                if job["status"] not in chain.LIVE:
                    break
                if a not in job["claims"] and job.get("machine") in (None, a) and r.random() < 0.6:
                    if a == self.LAZY and job.get("machine") == a:
                        continue                                        # dorme: o pedido vence e conta falta
                    self.send(m, "claim_job", {"job_id": jid})
                if a in job["claims"] and a not in job["results"] and r.random() < 0.7:   # na hora ou depois
                    if job["kind"] == "ai":
                        failed = r.random() < 0.25
                        tokens = [0, 10, 100, 1000, 3000]
                        usage = {"tokens_in": 0, "tokens_out": 0} if failed else \
                            {"tokens_in": r.choice(tokens), "tokens_out": r.choice(tokens)}
                        self.send(m, "submit_result", {"job_id": jid, "result_hash": H, "usage": usage,
                                                       **({"failed": True} if failed else {})})
                    else:
                        right = r.random() < 0.7
                        self.send(m, "submit_result", {"job_id": jid,
                                                       "result_hash": sha(b"certo") if right else sha(b"errado")})

    def metamask(self):
        """Operações do Sigma assinadas pela MetaMask (transações para SYSTEM_ADDRESS)."""
        r, p = self.rng, self.pending
        u = r.choice(self.USERS)
        mine = [jid for jid, j in sorted(p.jobs.items()) if j["client"] == u.address and j["status"] == "open"]
        if mine and r.random() < 0.5:
            call = {"type": "cancel_job", "data": {"job_id": r.choice(mine)}}
        elif r.random() < 0.5:
            call = {"type": "transfer", "data": {"to": r.choice(self.USERS).address, "amount": r.randint(1, 50_000)}}
        else:
            call = {"type": "fund_model", "data": {"name": r.choice(self.MODELS), "amount": r.randint(1, 50_000)}}
        payload = json.dumps(call).encode()
        self.submit(evm_tx(u.priv, p.nonces.get(u.address, 0), evm.SYSTEM_ADDRESS, data=payload))

    def govern(self):
        """Votações: qualquer um propõe (de parâmetro ou de texto) e quem tem SIG vota travando o SIG."""
        r, p = self.rng, self.pending
        u = r.choice(self.USERS)
        opened = [pid for pid, x in sorted(p.proposals.items()) if x["status"] == "open"]
        if opened and r.random() < 0.65:
            bal = p.bal(u.address)
            if bal > 0:
                self.send(u, "vote", {"proposal_id": r.choice(opened), "choice": r.choice(["sim", "sim", "nao"]),
                                      "amount": r.randint(1, max(1, bal // 2))})
            return
        if r.random() < 0.5:
            param = r.choice(["mint_bps", "hosting_per_mb_window", "ram_per_mb_window", "demand_max_bps",
                              "quorum_bps", "proposal_deposit"])
            lo, hi = chain.GOVERNABLE[param]
            value = r.choice([lo, hi, (lo + hi) // 2, p.params[param]])          # às vezes o valor de agora: recusado
            self.send(u, "propose", {"kind": "param", "title": f"mudar {param}", "text": "vamos votar",
                                     "param": param, "value": value})
        else:
            self.send(u, "propose", {"kind": "text", "title": "Uma ideia para a rede", "text": "x" * r.randint(0, 50)})

    def junk(self):
        r = self.rng
        u = r.choice(self.USERS)
        nonce = self.pending.nonces.get(u.address, 0)
        good = make_tx(u, "transfer", {"to": WALLETS[8].address, "amount": 1}, nonce)
        bad = [dict(good, extra=1), dict(good, id="00" * 32),
               {"type": "transfer", "data": {"to": WALLETS[8].address, "amount": 10 ** 5000}, "nonce": nonce,
                "pub": u.pub, "sig": good["sig"]},
               {"type": "register_machine", "data": {"payout": u.address, "name": "\udcff"}, "nonce": nonce,
                "pub": u.pub, "sig": good["sig"]},
               make_tx(u, "transfer", {"to": WALLETS[8].address, "amount": 1, "memo": "x"}, nonce),
               {"type": "work", "data": {"miner": u.address, "window": 0, "bits": 2, "nonces": [1], "lixo": 1}}]
        tx = r.choice(bad)
        self.t.assertIsNone(self.submit(tx))

    def step(self):
        r = self.rng
        actions = [self.mine, self.register, self.transfer, self.models, self.models, self.sites, self.jobs,
                   self.jobs, self.jobs, self.work_jobs, self.work_jobs, self.metamask, self.junk, self.govern,
                   self.govern]
        self.ping()                     # como o host: um sinal por janela (às vezes falha)
        self.host()
        if r.random() < 0.6:
            self.probe()
        for _ in range(r.randint(1, 4)):
            r.choice(actions)()
        p = self.state.params
        W, E = p["window_seconds"], p["epoch_seconds"]
        dt = r.choices([0.25, W, W, chain.TARGET_TIMEOUT + 1, chain.CLAIM_TIMEOUT + 1, chain.OPEN_TIMEOUT + 1, E,
                        chain.JOB_RETENTION + 1, (chain.PRUNE_WINDOWS + 2) * W, (chain.COOLDOWN_WINDOWS + 1) * W,
                        chain.RETIRED_KEEP + 1, chain.VOTE_SECONDS + 1],
                       weights=[40, 25, 10, 6, 2, 1, 2, 1, 1, 1, 1, 1])[0]
        self.block(dt)

    def replay(self):
        """Outro nó, só com os blocos: as mesmas raízes, com um snapshot JSON (to_dict → from_dict) no meio."""
        s = State(self.genesis)
        cut = len(self.blocks) // 2
        for i, (h, t, prev, txs, root) in enumerate(self.blocks):
            if i == cut:   # chaves em outra ordem de propósito: nenhuma regra pode depender da ordem do dict
                snap = State.from_dict(self.genesis, json.loads(json.dumps(s.to_dict(), sort_keys=True)))
                snap.height, snap.now = s.height, s.now
                self.t.assertEqual(snap.root(), s.root())
                s = snap
            apply_block(s, h, t, prev, txs)
            self.t.assertEqual(s.root(), root, f"reexecução divergiu no bloco {h}")


class TestRandomWalk(unittest.TestCase):
    """Invariantes com todos os tipos de transação e saltos além de todos os prazos (TARGET, CLAIM, OPEN,
    retenção, castigo, limpeza semanal e nomes aposentados)."""

    def test_invariants_history_and_replay(self):
        walk = Walk(2110, self)
        for _ in range(300):
            walk.step()
        walk.replay()
        wanted = {"job done", "job failed", "job expired", "job canceled", "job disputed", "falta", "castigo",
                  "disputa", "former", "saiu machines", "saiu sites", "saiu models", "saiu miners", "saiu retired",
                  "recusada", "fora do bloco", "votação approved", "votação rejected", "parâmetro mudou",
                  *(f"crédito {k}" for k in Walk.KINDS)}
        self.assertEqual(wanted - set(walk.seen), set(), f"o passeio não passou por tudo: {walk.seen}")
        included = [tx for b in walk.blocks for tx in b[3]]
        self.assertEqual({tx["type"] for tx in included} - {"work", "evm"},
                         set(chain.SIGNED_TYPES), "algum tipo de transação nunca entrou num bloco")
        evm_to = {chain.decode_evm(tx["data"]["raw"])["to"] for tx in included if tx["type"] == "evm"}
        self.assertIn(evm.SYSTEM_ADDRESS, evm_to)               # operações do Sigma pela MetaMask também
        self.assertTrue(evm_to - {evm.SYSTEM_ADDRESS})          # e transferências comuns
        self.assertTrue(any(tx["data"].get("worker") for tx in included if tx["type"] == "work"))


# ------------------------------------------------- ajustes depois da integração
class TestAfterIntegration(unittest.TestCase):
    """Regras ajustadas depois da integração da v2.1: falta de PC dormindo e modelo sem orçamento para um dia."""

    def test_sleeping_machine_gets_one_miss_until_it_signals(self):
        """O PC que dormiu logo depois de sinalizar leva uma falta só; antes, 3 janelas de sono (pedidos criados
        enquanto ele ainda parecia no ar) custavam o modelo e um dia de castigo."""
        net = hosted(MA, window_seconds=300, epoch_seconds=3000)
        ai_job(net, MA)
        s = net.block()
        s = net.block(at=s.now + chain.TARGET_TIMEOUT + 1)
        self.assertEqual(s.models["modelo-a"]["pinners"][MA.address]["misses"], 1)
        net.next_window()                                           # MA dormiu: não sinaliza nesta janela
        ai_job(net, MA)                                             # last_seen == w - 1: ainda pode ser escolhida
        s = net.block()
        s = net.block(at=s.now + chain.TARGET_TIMEOUT + 1)
        self.assertEqual(s.jobs[max(s.jobs, key=lambda j: s.jobs[j]["created"])]["status"], "expired")
        self.assertEqual(s.models["modelo-a"]["pinners"][MA.address]["misses"], 1)   # sem sinal, sem nova falta
        self.assertEqual(s.machines[MA.address]["reputation"], -1)
        net.step(MA)                                                # acordou e sinalizou, mas não responde
        ai_job(net, MA)
        s = net.block()
        s = net.block(at=s.now + chain.TARGET_TIMEOUT + 1)
        self.assertEqual(s.models["modelo-a"]["pinners"][MA.address]["misses"], 2)
        self.assertEqual(s.machines[MA.address]["reputation"], -2)

    def test_underfunded_large_model_refused_and_expiry_never_punishes(self):
        """Modelo grande (acima de FUNDED_ABOVE) precisa cobrir um dia de aluguel de todas as cópias já no registro
        (achado window-turnover-models-x-machines): sem isso, é recusado na hora, não vira lixo de estado. E uma
        atribuição vencida nunca tira reputação (achado poison-model-rep-drain): a máquina que deixou vencer só
        entra no cooldown daquele arquivo, mesmo que outra máquina já o hospede."""
        size = 60_000_000                                           # 60 MB: acima de FUNDED_ABOVE
        day = 288 * 2 * 300                                         # 288 janelas de 5 min, 2 cópias, 300 µSIG cada
        net = Net(window_seconds=300, epoch_seconds=3000)
        net.fund(SPONSOR.address)
        net.send(MA, "register_machine", machine(PA, pub=MA.pub))
        net.send(MB, "register_machine", machine(PB, pub=MB.pub))
        net.block()
        with self.assertRaisesRegex(TxError, f"cobrir um dia de hospedagem: {chain._pt(day)}"):
            net.send(SPONSOR, "register_model", model("sub", size=size, replicas=2, budget=day - 1))
        net.send(SPONSOR, "register_model", model("modelo-a", size=size, replicas=2, budget=10 * UNIT))
        net.block()
        s = net.step(MA, MB)
        m = s.models["modelo-a"]
        self.assertEqual(set(m["assigned"]), {MA.address, MB.address})
        self.assertEqual((s.rent_window(m), s.funded(m)), (600, True))
        self.assertTrue(s.funded(dict(m, size=chain.FUNDED_ABOVE, budget=1)))   # poucos MB: sempre passa
        net.send(MA, "pin_model", {"name": "modelo-a"})             # MA já tinha o arquivo; MB deixa vencer
        net.block()
        for _ in range(chain.ASSIGN_WINDOWS + 1):
            s = net.step(MA, MB)
        self.assertEqual(s.machines[MB.address]["reputation"], 0)   # vencer a atribuição nunca custa reputação
        self.assertIn(MB.address, s.models["modelo-a"]["cooldown"])  # só entra no cooldown daquele arquivo
        self.assertEqual(supply(s), s.mining["minted"])


if __name__ == "__main__":
    unittest.main()
