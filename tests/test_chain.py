"""Testes do ledger v2 (sigma/chain.py + sigma/pow.py).

    python -m unittest discover -s tests -v

Dirigem o State direto, com blocos sintéticos e parâmetros rápidos (janela de
5 s, época de 30 s, shares de 2 bits): sem nó, sem rede e sem relógio real.
A classe Net imita o nó: a transação é validada no estado pendente e só entra
no estado de verdade dentro do próximo bloco (begin_block → txs → end_block).
"""
import hashlib
import hmac
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sigma import chain, evm  # noqa: E402
from sigma.chain import (UNIT, State, TxError, canon, genesis_block, make_tx, sha, tx_id,  # noqa: E402
                         tx_sender, tx_summary)
from sigma.crypto import G, N, Wallet, _mul, keccak256, to_eth  # noqa: E402
from sigma.pow import POW_SALT, pow_hash, report_points, share_ok, target  # noqa: E402

T0 = 1793700000.0
# rede de teste rápida: aluguel só de disco e sem multiplicador de demanda (as contas antigas continuam valendo;
# memória e demanda têm testes próprios em TestDemandRent)
FAST = {"epoch_seconds": 30, "window_seconds": 5, "min_share_bits": 2, "max_shares": 8,
        "ram_per_mb_window": 0, "demand_max_bps": 0}
WALLETS = [Wallet(0x5EED0000 + i) for i in range(16)]    # chaves fixas: testes reprodutíveis
H = "ab" * 32
HW = {"cpu_cores": 8, "ram_mb": 16000, "ai_ram_mb": 8000, "vram_mb": 0, "disk_mb": 50000, "gpu": ""}


# ------------------------------------------------------------------ ajudantes
def find_nonces(seed, miner, bits, count, reject=lambda n: False):
    """Procura shares de verdade (com 2 bits, uns 4 hashes scrypt por share)."""
    found, n = [], 0
    while len(found) < count:
        if share_ok(seed, miner, n, bits) and not reject(n):
            found.append(n)
        n += 1
    return found


def work_tx(miner, window, bits, nonces, worker=None):
    tx = {"type": "work", "data": {"miner": miner, "window": window, "bits": bits, "nonces": nonces}}
    if worker is not None:
        tx["data"]["worker"] = worker
    tx["id"] = tx_id(tx)
    return tx


def apply_block(state, height, time, prev, txs):
    state.begin_block(height, time, prev)
    for tx in txs:
        state.apply_tx(tx)
    state.end_block()


def supply(s):
    """Todo SIG que existe: saldos + o que está travado em escrows, budgets, votações e cauções (regras v3)."""
    locked = sum(j["escrow"] for j in s.jobs.values() if j["status"] in ("open", "running"))
    locked += sum(x["budget"] for x in [*s.sites.values(), *s.models.values()])
    return sum(s.balances.values()) + locked + s.locked() + s.bonded()


def wp(s):
    """Pontos das janelas ainda abertas para relatório (viram a base da emissão quando a janela fecha)."""
    out = {}
    for pts in s.mining["open_points"].values():
        for a, v in pts.items():
            out[a] = out.get(a, 0) + v
    return out


def machine(payout, ai=True, compute=True, hw=None, **kw):
    return dict({"payout": payout, "name": "pc", "hw": dict(HW, **(hw or {})), "ai": ai, "compute": compute,
                 "endpoint": ""}, **kw)


def model(name, size=1_000_000, budget=10 * UNIT, **kw):
    return dict({"name": name, "url": f"https://huggingface.co/org/{name}/resolve/main/{name}.gguf",
                 "sha256": sha(name.encode()), "size": size, "license": "Apache-2.0", "description": "teste",
                 "royalty_bps": 1000, "reinvest_bps": 0, "replicas": 1, "budget": budget}, **kw)


class Net:
    """Chain de teste no fluxo do nó: pendente valida, o próximo bloco aplica."""

    def __init__(self, **params):
        self.genesis = {"network": "sigma", "chain_id": 51515, "time": T0, "validators": [], "alloc": {},
                        "params": dict(FAST, **params)}
        self.state = State(self.genesis)
        self.pending = self.state.clone()
        self.queue, self.blocks = [], []
        self.height, self.time = 0, T0
        self.prev = genesis_block(self.genesis)["hash"]

    @property
    def window(self):
        return self.state.mining["window"]

    def submit(self, tx):
        self.pending.apply_tx(tx)            # inválida: TxError aqui, como no POST /tx do nó
        self.queue.append(tx)
        return tx

    def send(self, wallet, type_, data):
        return self.submit(make_tx(wallet, type_, data, self.pending.nonces.get(wallet.address, 0)))

    def block(self, dt=0.25, at=None):
        txs, self.queue = self.queue, []
        self.height += 1
        self.time = self.time + dt if at is None else at
        apply_block(self.state, self.height, self.time, self.prev, txs)
        root = self.state.root()
        self.blocks.append((self.height, self.time, self.prev, txs, root))
        self.prev = sha(canon([self.height, self.time, root]))
        self.pending = self.state.clone()
        return self.state

    def next_window(self):
        p = self.state.params
        return self.block(at=self.state.mining["window_start"] + p["window_seconds"])

    def settle(self):
        p = self.state.params
        return self.block(at=self.state.mining["epoch_start"] + p["epoch_seconds"])

    def mine(self, *miners, count=4, bits=None):
        s = self.pending
        w = s.mining["window"]
        bits = bits or s.params["min_share_bits"]
        for m in miners:
            self.submit(work_tx(m, w, bits, find_nonces(s.mining["seeds"][str(w)], m, bits, count)))

    def fund(self, *addrs):
        """O único jeito de ter SIG: minerar. A janela do relatório vira a base da emissão duas viradas depois,
        e a partir daí cada bloco paga a fatia do tempo que passou (fatias iguais)."""
        self.mine(*addrs)
        self.block()
        self.next_window()
        self.next_window()                   # a janela minerada fechou para relatórios: virou a base
        return self.block(dt=2 * self.state.params["window_seconds"])

    def pay(self):
        """A janela atual vira a base (duas viradas) e um bloco de emissão paga: {endereço: µSIG} desse bloco."""
        self.next_window()
        self.next_window()
        s = self.block(dt=2 * self.state.params["window_seconds"])
        return {a: amt for a, kind, amt, _ in s.credits if kind == "mining"}

    def step(self, *machines):
        """Vira a janela e as máquinas sinalizam, como o host faz a cada janela."""
        self.next_window()
        for m in machines:
            self.send(m, "ping", {})
        return self.block()


def eth_sign(priv, z):
    """ECDSA secp256k1 sobre o hash keccak, com id de recuperação (o que a MetaMask faz)."""
    e = int.from_bytes(z, "big")
    k = int.from_bytes(hmac.new(priv.to_bytes(32, "big"), z, hashlib.sha256).digest(), "big") % N or 1
    point = _mul(k, G)
    r, recid = point[0] % N, point[1] & 1
    s = pow(k, -1, N) * (e + r * priv) % N
    if s > N // 2:                          # a chain só aceita s baixo
        s, recid = N - s, recid ^ 1
    return r, s, recid


def evm_tx(priv, nonce, to, value=0, data=b"", chain_id=51515):
    """Transação legacy EIP-155 assinada, no formato que eth_sendRawTransaction recebe."""
    fields = [nonce, 0, 21000, bytes.fromhex(to[2:]), value, data]
    r, s, recid = eth_sign(priv, keccak256(evm.rlp_encode(fields + [chain_id, 0, 0])))
    raw = evm.rlp_encode(fields + [chain_id * 2 + 35 + recid, r, s])
    tx = {"type": "evm", "data": {"raw": "0x" + raw.hex()}}
    tx["id"] = tx_id(tx)
    return tx


# --------------------------------------------------------------------- genesis
class TestGenesis(unittest.TestCase):
    def test_alloc_must_be_empty(self):
        g = Net().genesis
        g["alloc"] = {WALLETS[0].address: 1000 * UNIT}
        with self.assertRaisesRegex(ValueError, "alloc"):
            State(g)
        del g["alloc"]
        self.assertEqual(State(g).balances, {})

    def test_params_defaults_and_overrides(self):
        g = dict(Net().genesis)
        del g["params"]
        self.assertEqual(State(g).params, {"epoch_seconds": 43200, "window_seconds": 300, "min_share_bits": 12,
                                           "max_shares": 32, "supply_cap": 100_000_000 * UNIT, "mint_bps": 1,
                                           "hosting_per_mb_window": 5, "ram_per_mb_window": 2,
                                           "demand_max_bps": 30000, "proposal_deposit": 10 * UNIT,
                                           "quorum_bps": 100})
        p = Net(mint_bps=75, hosting_per_mb_window=chain.MAX_HOSTING_PRICE).state.params
        self.assertEqual({k: p[k] for k in FAST}, FAST)
        self.assertEqual((p["mint_bps"], p["supply_cap"], p["hosting_per_mb_window"]),
                         (75, 100_000_000 * UNIT, chain.MAX_HOSTING_PRICE))

    def test_bad_params_refused(self):
        for bad in ({"window_seconds": 0}, {"epoch_seconds": -1}, {"mint_bps": 10001}, {"min_share_bits": 0},
                    {"min_share_bits": 201}, {"max_shares": True}, {"supply_cap": 1.5},
                    {"window_seconds": float("inf")}, {"hosting_per_mb_window": 0},
                    {"hosting_per_mb_window": chain.MAX_HOSTING_PRICE + 1}, {"hosting_per_mb_window": 5.0},
                    {"hosting_per_mb_window": True}, {"ram_per_mb_window": 101}, {"quorum_bps": 5001},
                    {"proposal_deposit": 1}, {"parametro_inventado": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Net(**bad)

    def test_born_without_sig(self):
        net = Net()
        s = net.state
        self.assertEqual((s.balances, s.points, s.miners, s.epochs), ({}, {}, {}, []))
        self.assertEqual(net.prev, sha(canon(net.genesis)))      # o "prev" do bloco 1 é o hash do genesis
        self.assertEqual(s.mining, {"window": 0, "window_start": T0, "seeds": {"0": sha(f"{net.prev}:0".encode())},
                                    "epoch": 0, "epoch_start": T0, "minted": 0, "last_reports": {},
                                    "emit_time": T0, "open_points": {}, "rent": {"disk_bps": 10000, "ram_bps": 10000},
                                    "period_paid": 0, "period_rewards": {}})
        self.assertEqual(genesis_block(net.genesis)["state_root"], s.root())
        self.assertEqual(set(s.to_dict()), {"balances", "nonces", "machines", "jobs", "sites", "models",
                                            "retired_models", "mining", "miners", "points", "epochs", "params",
                                            "proposals", "upgrade", "ext"})
        self.assertEqual(s.upgrade, {"rules": 1, "history": [{"rules": 1, "height": 0, "time": T0}]})
        self.assertEqual(s.ext, {})
        self.assertEqual((s.height, s.now, s.last_settlement, s.credits), (0, T0, None, []))

    def test_v1_money_paths_are_gone(self):
        for name in ("MIN_STAKE", "FEE_BPS", "SLASH_BPS"):
            self.assertFalse(hasattr(chain, name), name)
        s = Net().state
        for attr in ("burned", "providers", "_tx_register_provider", "_tx_unregister_provider", "_tx_faucet"):
            self.assertFalse(hasattr(s, attr), attr)
        net = Net()
        for type_, data in (("register_provider", {"stake": 0}), ("unregister_provider", {})):
            with self.assertRaisesRegex(TxError, "desconhecido"):
                net.send(WALLETS[0], type_, data)

    def test_from_dict_ignores_unknown_keys(self):
        net = Net()
        d = json.loads(json.dumps(net.state.to_dict()))
        d["burned"] = 5
        s = State.from_dict(net.genesis, d)
        self.assertEqual(s.root(), net.state.root())
        self.assertFalse(hasattr(s, "burned"))
        # os parâmetros agora são estado (as votações mudam): o snapshot os traz, e o state root os cobre
        d["params"] = dict(d["params"], mint_bps=7)
        s = State.from_dict(net.genesis, d)
        self.assertEqual(s.params["mint_bps"], 7)
        self.assertNotEqual(s.root(), net.state.root())


# ------------------------------------------------------------- prova de trabalho
class TestPow(unittest.TestCase):
    MINER = "sig" + "ab" * 20

    def test_pow_hash_is_scrypt(self):
        raw = hashlib.scrypt(f"seed:{self.MINER}:14:7".encode(), salt=b"sigma-pow-v2", n=1024, r=1, p=1, dklen=32)
        self.assertEqual(POW_SALT, b"sigma-pow-v2")
        self.assertEqual(pow_hash("seed", self.MINER, 14, 7), int.from_bytes(raw, "big"))

    def test_share_ok_matches_target(self):
        hits = 0
        for n in range(64):
            h = pow_hash("s", self.MINER, 3, n)
            self.assertEqual(share_ok("s", self.MINER, n, 3), h < 1 << 253)
            self.assertEqual(target(3), 1 << 253)
            hits += share_ok("s", self.MINER, n, 3)
        self.assertTrue(0 < hits < 64)

    def test_identity_seed_bits_and_nonce_are_in_the_hash(self):
        base = pow_hash("s", self.MINER, 3, 1)
        self.assertNotEqual(base, pow_hash("s", "sig" + "cd" * 20, 3, 1))
        self.assertNotEqual(base, pow_hash("s", self.MINER + ".a1b2c3d4", 3, 1))   # outro PC da mesma carteira
        self.assertNotEqual(base, pow_hash("t", self.MINER, 3, 1))
        self.assertNotEqual(base, pow_hash("s", self.MINER, 4, 1))                  # outro nível de dificuldade
        self.assertNotEqual(base, pow_hash("s", self.MINER, 3, 2))

    def test_report_points(self):
        self.assertEqual(report_points(3, 14), 3 * 2 ** 14)
        self.assertEqual(report_points(1, 2), 4)


# ------------------------------------------------------------ relatório de trabalho
class TestWork(unittest.TestCase):
    A, B = WALLETS[0].address, WALLETS[1].address

    def setUp(self):
        self.net = Net()
        self.seed = self.net.state.mining["seeds"]["0"]

    def report(self, miner=None, window=0, bits=2, nonces=None, count=3):
        miner = miner or self.A
        if nonces is None:
            nonces = find_nonces(self.net.pending.mining["seeds"][str(window)], miner, bits, count)
        return work_tx(miner, window, bits, nonces)

    def test_valid_report_earns_points(self):
        self.net.submit(self.report(count=5))
        s = self.net.block()
        self.assertEqual(wp(s), {self.A: 5 << 2})
        self.assertEqual(s.miners[self.A], {"points_total": 20, "rewards_total": 0, "last_window": 0,
                                            "last_rate": 20 / 5})
        self.assertEqual(s.mining["last_reports"], {self.A: {"0": 20}})
        self.assertEqual(s.nonces, {})          # sem assinatura, sem nonce de conta
        self.assertEqual(s.balances, {})        # pontos não são SIG: só a liquidação da época imprime

    def test_more_bits_more_points(self):
        self.net.submit(self.report(bits=4, count=2))
        self.assertEqual(wp(self.net.block())[self.A], 2 << 4)

    def test_shares_found_for_someone_else_do_not_count(self):
        nonces = find_nonces(self.seed, self.A, 2, 3, reject=lambda n: share_ok(self.seed, self.B, n, 2))
        with self.assertRaisesRegex(TxError, "share inválido"):
            self.net.submit(self.report(miner=self.B, nonces=nonces))
        self.net.submit(self.report(miner=self.A, nonces=nonces))      # para quem achou, valem

    def test_one_bad_share_spoils_the_report(self):
        good = find_nonces(self.seed, self.A, 2, 2)
        bad = next(n for n in range(100, 10_000) if not share_ok(self.seed, self.A, n, 2))
        with self.assertRaisesRegex(TxError, "share inválido"):
            self.net.submit(self.report(nonces=good + [bad]))
        self.assertEqual((wp(self.net.pending), self.net.pending.mining["last_reports"]), ({}, {}))

    def test_best_report_per_window_wins(self):
        """Vale o melhor relatório da janela, e só a diferença entra nos pontos (nada conta duas vezes)."""
        six = find_nonces(self.seed, self.A, 2, 6)
        self.net.submit(self.report(nonces=six[:2]))                    # 8 pontos
        with self.assertRaisesRegex(TxError, "tantos pontos ou mais"):
            self.net.submit(self.report(nonces=six[2:4]))               # outros 8: não é melhor
        self.net.block()
        with self.assertRaisesRegex(TxError, "tantos pontos ou mais"):
            self.net.submit(self.report(nonces=six[:2]))                # o mesmo de novo
        self.net.submit(self.report(nonces=six))                        # 24 pontos: entra só a diferença
        s = self.net.block()
        self.assertEqual(wp(s), {self.A: 24})
        self.assertEqual(s.mining["last_reports"], {self.A: {"0": 24}})
        self.assertEqual((s.miners[self.A]["points_total"], s.miners[self.A]["last_rate"]), (24, 8 / 5 + 16 / 5))
        self.net.submit(self.report(miner=self.B))                      # outro minerador, mesma janela: ok

    def test_points_check_comes_before_the_scrypt(self):
        """Repetir um relatório que não é melhor é recusado sem gastar scrypt (é barato conferir)."""
        good = find_nonces(self.seed, self.A, 2, 2)
        self.net.submit(self.report(nonces=good))
        bad = [n for n in range(10_000, 20_000) if not share_ok(self.seed, self.A, n, 2)][:2]
        with self.assertRaisesRegex(TxError, "tantos pontos ou mais"):
            self.net.submit(self.report(nonces=bad))                    # shares inválidos nem chegam a ser vistos

    def test_bits_bounds(self):
        for bits in (1, 201, "2", True, None, 2.0):
            with self.subTest(bits=bits), self.assertRaisesRegex(TxError, "bits"):
                self.net.submit(work_tx(self.A, 0, bits, [1]))

    def test_nonce_rules(self):
        ok = find_nonces(self.seed, self.A, 2, 9)
        cases = {"repetido": [ok[0], ok[0]], "demais": ok, "vazio": [], "negativo": [-1], "2**62": [2 ** 62],
                 "float": [1.0], "bool": [True], "texto": ["5"], "não é lista": {"0": 1}}
        for label, nonces in cases.items():
            with self.subTest(label), self.assertRaises(TxError):
                self.net.submit(work_tx(self.A, 0, 2, nonces))
        self.net.submit(work_tx(self.A, 0, 2, ok[:8]))                  # max_shares = 8: o limite exato passa
        self.assertEqual(wp(self.net.block())[self.A], 8 << 2)

    def test_window_must_be_current_or_previous(self):
        net = self.net
        for window in (1, -1, "0", None):
            with self.subTest(window=window), self.assertRaisesRegex(TxError, "janela"):
                net.submit(work_tx(self.A, window, 2, [1]))
        late = self.report(count=2)              # janela 0, enviado depois que ela fechou
        net.next_window()
        self.assertEqual(set(net.state.mining["seeds"]), {"0", "1"})
        net.submit(late)
        net.submit(self.report(window=1, count=2))
        s = net.block()
        self.assertEqual(s.mining["last_reports"][self.A], {"0": 8, "1": 8})
        self.assertEqual(wp(s)[self.A], 16)
        s = net.next_window()
        self.assertEqual(set(s.mining["seeds"]), {"1", "2"})
        self.assertEqual(s.mining["last_reports"], {self.A: {"1": 8}})   # poda: só janelas >= atual - 1
        with self.assertRaisesRegex(TxError, "janela"):
            net.submit(self.report(miner=self.B, nonces=find_nonces(self.seed, self.B, 2, 2)))

    def test_skipped_windows_keep_only_current_seed(self):
        late = self.report(count=2)
        s = self.net.block(at=T0 + 3 * 5 + 1)     # 3 janelas de uma vez
        self.assertEqual((s.mining["window"], s.mining["window_start"]), (3, T0 + 15))
        self.assertEqual(list(s.mining["seeds"]), ["3"])
        with self.assertRaisesRegex(TxError, "janela"):
            self.net.submit(late)

    def test_seed_comes_from_the_hash_that_opened_the_window(self):
        prev = self.net.prev
        s = self.net.next_window()
        self.assertEqual(s.mining["seeds"], {"1": sha(f"{prev}:1".encode()), "0": self.seed})

    def test_bad_miner(self):
        for miner in ("xyz", None, 5, "sig" + "zz" * 20):
            with self.subTest(miner=miner), self.assertRaisesRegex(TxError, "minerador"):
                self.net.submit(work_tx(miner, 0, 2, [1]))

    def test_miner_address_is_normalized(self):
        nonces = find_nonces(self.seed, self.A, 2, 2)          # shares são do endereço normalizado
        self.net.submit(work_tx("0x" + self.A[3:].upper(), 0, 2, nonces))
        self.assertEqual(wp(self.net.block()), {self.A: 8})

    def test_report_validated_in_pending_then_in_block(self):
        self.net.submit(self.report(count=2))
        self.assertEqual(wp(self.net.pending), {self.A: 8})
        self.assertEqual(wp(self.net.state), {})
        self.assertEqual(wp(self.net.block()), {self.A: 8})


# ------------------------------------------------------------------- emissão
def slice_of(s_before, dt, bps=None):
    """Referência fora da chain: a fatia de emissão de `dt` segundos (taxa de 12 h fracionada no tempo)."""
    p = s_before.params
    bps = p["mint_bps"] if bps is None else bps
    return (p["supply_cap"] - s_before.mining["minted"]) * bps * round(dt * 1000) // (10_000 * round(p["epoch_seconds"] * 1000))


class TestEmission(unittest.TestCase):
    """SPEC-v2.3 §1: a emissão sai em todo bloco, pela fração de tempo, e é 50x menor (0,01% a cada 12 h)."""
    A, B, C = WALLETS[0].address, WALLETS[1].address, WALLETS[2].address

    def based(self, *counts, **params):
        """Rede em que a janela minerada já virou a base (duas viradas depois do relatório)."""
        net = Net(**params)
        for addr, n, bits in counts:
            net.mine(addr, count=n, bits=bits)
        net.block()
        net.next_window()
        net.next_window()
        return net

    def test_rate_is_fifty_times_smaller(self):
        self.assertEqual(chain.DEFAULT_PARAMS["mint_bps"], 1)          # era 50 (0,5%)
        g = dict(Net().genesis)
        del g["params"]
        self.assertEqual(State(g).next_mint(), 10_000 * UNIT)        # 0,01% de 100 milhões a cada 12 h

    def test_base_is_the_window_closed_for_reports(self):
        net = Net()
        net.mine(self.A)
        w = net.block().mining["window"]
        s = net.next_window()
        self.assertEqual((s.points, wp(s)), ({}, {self.A: 16}))      # ainda pode receber relatório atrasado
        s = net.next_window()
        self.assertEqual(s.points, {self.A: 16})                     # fechou: virou a base
        self.assertEqual(sorted(s.mining["open_points"]), [])        # só janelas >= w+1 continuam abertas
        self.assertEqual(s.mining["window"], w + 2)

    def test_each_block_pays_its_slice_of_time(self):
        net = self.based((self.A, 4, 2))
        before = net.state.clone()
        s = net.block(dt=4)
        r = slice_of(before, 4)
        self.assertGreater(r, 0)
        self.assertEqual(s.bal(self.A), r)
        self.assertEqual(s.mining["minted"], r)
        self.assertIn([self.A, "mining", r, s.mining["epoch"]], s.credits)
        self.assertEqual(s.miners[self.A]["rewards_total"], r)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_proportional_split_and_rounding_dust(self):
        net = self.based((self.A, 3, 2), (self.B, 1, 2), (self.C, 4, 3))   # 12, 4 e 32 pontos
        before = net.state.clone()
        s = net.block(dt=3)
        mint = slice_of(before, 3)
        want = {self.A: mint * 12 // 48, self.B: mint * 4 // 48, self.C: mint * 32 // 48}
        self.assertEqual(s.balances, want)
        self.assertEqual(s.mining["minted"], sum(want.values()))
        self.assertLess(mint - s.mining["minted"], 3)                # o resto do arredondamento não é impresso

    def test_same_total_whatever_the_number_of_blocks(self):
        """A média exata: um bloco de 4 s ou dez de 0,4 s (na mesma janela) imprimem (quase) o mesmo."""
        one = self.based((self.A, 2, 2))
        one.block(dt=4)
        many = self.based((self.A, 2, 2))
        for _ in range(10):
            many.block(dt=0.4)
        a, b = one.state.mining["minted"], many.state.mining["minted"]
        self.assertGreater(a, 0)
        self.assertLess(abs(a - b) / a, 1e-4)                         # só os juros compostos do "que falta"

    def test_no_work_no_mint(self):
        net = Net()
        for _ in range(5):
            s = net.block(dt=2)
        self.assertEqual((s.mining["minted"], s.balances), (0, {}))
        self.assertEqual(s.mining["emit_time"], s.now)                # o tempo sem trabalho não fica guardado

    def test_producer_outage_does_not_mint_the_gap(self):
        net = self.based((self.A, 2, 2))
        before = net.state.clone()
        s = net.block(dt=1000)                                       # produtor parado por 1.000 s
        self.assertEqual(s.mining["minted"], slice_of(before, 2 * s.params["window_seconds"]))

    def test_cap_is_never_exceeded(self):
        net = self.based((self.A, 1, 2), supply_cap=1000, mint_bps=10_000)
        for _ in range(40):                                          # minera toda janela: a base nunca esvazia
            net.mine(self.A, count=1)
            net.block()
            s = net.next_window()
        self.assertLessEqual(s.mining["minted"], 1000)
        self.assertEqual(sum(s.balances.values()), s.mining["minted"])

    def test_period_stats_every_12h(self):
        net = self.based((self.A, 3, 2), (self.B, 1, 2))
        net.block(dt=4)
        paid = net.state.mining["minted"]
        s = net.settle()                                             # fecha o período (só estatística)
        e = s.epochs[-1]
        self.assertEqual((e["epoch"], e["miners"]), (0, 2))
        self.assertGreaterEqual(e["mint"], paid)
        self.assertEqual(e["top"][0][0], self.A)
        self.assertEqual((s.mining["period_paid"], s.mining["period_rewards"]), (0, {}))
        self.assertEqual(s.mining["epoch"], 1)

    def test_epoch_history_keeps_last_60(self):
        net = Net()
        for _ in range(63):
            net.settle()
        s = net.state
        self.assertEqual(len(s.epochs), 60)
        self.assertEqual((s.epochs[0]["epoch"], s.epochs[-1]["epoch"]), (3, 62))

    def test_mining_credits_are_transient(self):
        net = self.based((self.A, 2, 2))
        s = net.block(dt=2)
        root = s.root()
        s.credits.append([self.B, "mining", 1, 0])
        self.assertEqual(s.root(), root)                             # fora do state root
        self.assertNotIn("credits", s.to_dict())


# ------------------------------------------------------------------ máquinas
class TestMachines(unittest.TestCase):
    PAYOUT = WALLETS[0].address
    M = WALLETS[10]

    def test_register_with_defaults(self):
        net = Net()
        net.block(at=T0 + 12)                   # janela 2
        net.send(self.M, "register_machine", machine(self.PAYOUT))
        s = net.block()
        self.assertEqual(s.machines[self.M.address], {
            "payout": self.PAYOUT, "name": "pc", "hw": HW, "ai": True, "compute": True, "endpoint": "",
            "prices": {"ai_per_1k_tokens": 2000, "compute_per_unit": 500}, "pub": None,
            "since": T0 + 12.25, "last_seen": 2, "online": True, "models": [], "jobs_done": 0, "jobs_failed": 0,
            "earned": 0, "reputation": 0, "rep_window": -1, "compute_disputes": 0, "answered_window": -1})
        self.assertEqual(s.balances, {})        # registrar não dá SIG a ninguém

    def test_register_validation(self):
        net = Net()
        bad = [machine("nada"), machine(self.PAYOUT, endpoint="ftp://x"), machine(self.PAYOUT, endpoint=5),
               machine(self.PAYOUT, hw={"ram_mb": -1}), machine(self.PAYOUT, hw={"disk_mb": "10"}),
               machine(self.PAYOUT, hw={"gpu": 3}), machine(self.PAYOUT, prices={"ai_per_1k_tokens": 0}),
               machine(self.PAYOUT, prices={"ai_per_1k_tokens": chain.MAX_AI_PRICE + 1}),
               machine(self.PAYOUT, prices={"compute_per_unit": chain.MAX_COMPUTE_PRICE + 1}),
               machine(self.PAYOUT, prices={"hosting_per_mb_window": 5}),        # aluguel é preço da rede
               machine(self.PAYOUT, hw={"ai_ram_mb": chain.MAX_AI_RAM_MB + 1}),
               machine(self.PAYOUT, hw={"disk_mb": chain.HW_MAX + 1}), machine(self.PAYOUT, hw={"tpu": 1}),
               machine(self.PAYOUT, ai=1), machine(self.PAYOUT, compute="sim"),
               machine(self.PAYOUT, pub=WALLETS[1].pub), machine(self.PAYOUT, pub="ab" * 63),
               machine(self.PAYOUT, prices=[1]), machine(self.PAYOUT, name=["x"]), dict(machine(self.PAYOUT), hw=[1]),
               machine(self.PAYOUT, name="n" * 41),                   # nome/gpu grandes são recusados, não truncados
               machine(self.PAYOUT, hw={"gpu": "g" * 81})]            # (achado disk-bloat-nome-grande)
        for d in bad:
            with self.subTest(d=d), self.assertRaises(TxError):
                net.send(self.M, "register_machine", d)
        net.send(self.M, "register_machine", machine(self.PAYOUT, name="n" * 40, hw={"gpu": "g" * 80}))   # no limite
        net.block()
        net.send(self.M, "register_machine", machine("0x" + self.PAYOUT[3:].upper(), endpoint="https://pc.example:8801",
                                                     prices={"ai_per_1k_tokens": chain.MAX_AI_PRICE},
                                                     hw={"ai_ram_mb": chain.MAX_AI_RAM_MB}, pub=self.M.pub.upper()))
        rec = net.block().machines[self.M.address]
        self.assertEqual((rec["payout"], rec["endpoint"], rec["pub"]), (self.PAYOUT, "https://pc.example:8801", self.M.pub))
        self.assertEqual(rec["prices"], {"ai_per_1k_tokens": chain.MAX_AI_PRICE, "compute_per_unit": 500})
        self.assertEqual(rec["hw"]["ai_ram_mb"], chain.MAX_AI_RAM_MB)

    def test_pub_mismatch_message_and_kept_on_update(self):
        net = Net()
        with self.assertRaisesRegex(TxError, "pub não corresponde à chave da máquina"):
            net.send(self.M, "register_machine", machine(self.PAYOUT, pub=WALLETS[1].pub))
        net.send(self.M, "register_machine", machine(self.PAYOUT, pub=self.M.pub))
        net.block()
        net.send(self.M, "register_machine", machine(self.PAYOUT, name="sem-chave"))   # sem pub: guarda a de antes
        rec = net.block().machines[self.M.address]
        self.assertEqual((rec["name"], rec["pub"]), ("sem-chave", self.M.pub))

    def test_update_keeps_history(self):
        net = Net()
        net.send(self.M, "register_machine", machine(self.PAYOUT))
        net.block()
        net.state.machines[self.M.address].update(jobs_done=3, earned=70, reputation=2)
        net.pending = net.state.clone()
        net.next_window()
        net.send(self.M, "register_machine", machine(WALLETS[1].address, name="sala", compute=False))
        rec = net.block().machines[self.M.address]
        self.assertEqual((rec["payout"], rec["name"], rec["compute"], rec["last_seen"], rec["since"]),
                         (WALLETS[1].address, "sala", False, 1, T0 + 0.25))
        self.assertEqual((rec["jobs_done"], rec["earned"], rec["reputation"]), (3, 70, 2))

    def test_ping(self):
        net = Net()
        with self.assertRaisesRegex(TxError, "não registrada"):
            net.send(self.M, "ping", {})
        net.send(self.M, "register_machine", machine(self.PAYOUT))
        net.block()
        with self.assertRaisesRegex(TxError, "já sinalizou"):
            net.send(self.M, "ping", {})
        net.next_window()
        net.send(self.M, "ping", {})
        with self.assertRaisesRegex(TxError, "já sinalizou"):
            net.send(self.M, "ping", {})        # o pendente já conta o primeiro
        s = net.block()
        self.assertEqual((s.machines[self.M.address]["last_seen"], s.mining["window"]), (1, 1))

    def test_offline_after_two_silent_windows(self):
        net = Net()
        net.send(self.M, "register_machine", machine(self.PAYOUT))
        net.block()                             # last_seen = 0
        for _ in (1, 2):
            self.assertTrue(net.next_window().machines[self.M.address]["online"])
        s = net.next_window()                   # janela 3: 0 < 3 - 2
        self.assertFalse(s.machines[self.M.address]["online"])
        net.send(self.M, "ping", {})            # volta ao sinalizar
        s = net.block()
        self.assertEqual((s.machines[self.M.address]["online"], s.machines[self.M.address]["last_seen"]), (True, 3))


# --------------------------------------------------------- atribuição de modelos
class TestAssignment(unittest.TestCase):
    SPONSOR = WALLETS[2]
    MS = WALLETS[10:16]
    PAY = WALLETS[3].address

    def setUp(self):
        self.net = Net()
        self.net.fund(self.SPONSOR.address)

    @staticmethod
    def expected(s, name, eligible, n):
        seed = s.mining["seeds"][str(s.mining["window"])]
        return sorted(sorted(eligible, key=lambda a: hashlib.sha256((seed + name + a).encode()).hexdigest())[:n])

    def test_network_chooses_deterministically(self):
        net, ms = self.net, self.MS
        for w in ms[:3]:
            net.send(w, "register_machine", machine(self.PAY))
        net.send(ms[3], "register_machine", machine(self.PAY, ai=False))
        net.send(ms[4], "register_machine", machine(self.PAY, hw={"ai_ram_mb": 513}))     # modelo pede 514
        net.send(ms[5], "register_machine", machine(self.PAY, hw={"disk_mb": 0}))
        net.send(self.SPONSOR, "register_model", model("modelo-a", replicas=2))
        s = net.block()
        self.assertEqual(s.models["modelo-a"]["ram_mb"], 514)
        self.assertEqual(s.models["modelo-a"]["assigned"], {})                     # só na virada da janela
        s = net.next_window()
        assigned = s.models["modelo-a"]["assigned"]
        self.assertEqual(sorted(assigned), self.expected(s, "modelo-a", [w.address for w in ms[:3]], 2))
        self.assertEqual(set(assigned.values()), {s.mining["window"]})
        replay = State(net.genesis)                                                # outro nó, mesmos blocos
        for h, t, prev, txs, root in net.blocks:
            apply_block(replay, h, t, prev, txs)
        self.assertEqual(replay.models["modelo-a"]["assigned"], assigned)
        self.assertEqual(replay.root(), s.root())

    def test_ai_ram_is_committed_per_model(self):
        net = self.net
        net.send(self.MS[0], "register_machine", machine(self.PAY, hw={"ai_ram_mb": 1100}))
        for name in ("modelo-a", "modelo-b", "modelo-c"):
            net.send(self.SPONSOR, "register_model", model(name))
        net.block()
        s = net.next_window()
        got = [n for n in sorted(s.models) if self.MS[0].address in s.models[n]["assigned"]]
        self.assertEqual(got, ["modelo-a", "modelo-b"])       # 2 × 514 MB cabem em 1100; o terceiro não

    def test_disk_is_committed_too(self):
        net = self.net
        net.send(self.MS[0], "register_machine", machine(self.PAY, hw={"disk_mb": 2}))
        for name in ("modelo-a", "modelo-b", "modelo-c"):
            net.send(self.SPONSOR, "register_model", model(name))
        net.block()
        s = net.next_window()
        got = [n for n in sorted(s.models) if self.MS[0].address in s.models[n]["assigned"]]
        self.assertEqual(got, ["modelo-a", "modelo-b"])       # 2 MB de disco, modelos de 1 MB

    def test_pin_requires_assignment(self):
        net = self.net
        a, b = self.MS[:2]
        net.send(a, "register_machine", machine(self.PAY))
        net.send(b, "register_machine", machine(self.PAY, ai=False))
        net.send(self.SPONSOR, "register_model", model("modelo-a"))
        net.block()
        with self.assertRaisesRegex(TxError, "não atribuiu"):
            net.send(a, "pin_model", {"name": "modelo-a"})              # ainda não houve sorteio
        net.next_window()
        with self.assertRaisesRegex(TxError, "não atribuiu"):
            net.send(b, "pin_model", {"name": "modelo-a"})              # ninguém escolhe à mão
        for name in ("nao-existe", ["x"], None):
            with self.assertRaisesRegex(TxError, "não existe"):
                net.send(a, "pin_model", {"name": name})
        net.send(a, "pin_model", {"name": "modelo-a"})
        s = net.block()
        m = s.models["modelo-a"]
        self.assertEqual(m["assigned"], {})
        self.assertEqual(m["pinners"], {a.address: {"window": s.mining["window"], "misses": 0, "miss_window": -1}})
        self.assertEqual(s.machines[a.address]["models"], ["modelo-a"])
        with self.assertRaisesRegex(TxError, "não atribuiu"):
            net.send(a, "pin_model", {"name": "modelo-a"})
        s = net.step(a)                          # réplicas completas: ninguém mais é sorteado
        self.assertEqual(s.models["modelo-a"]["assigned"], {})

    def test_unfulfilled_assignment_expires_into_cooldown_without_punishing(self):
        """Atribuição vencida não custa reputação (recusar um arquivo ruim seria arma de Sybil): a máquina entra
        em cooldown daquele modelo (não é reatribuída) e a vaga volta para a rede."""
        net = self.net
        a, b = self.MS[:2]
        net.send(a, "register_machine", machine(self.PAY))
        net.send(b, "register_machine", machine(self.PAY))
        net.send(self.SPONSOR, "register_model", model("modelo-a", replicas=2))
        net.block()
        s = net.step(a, b)
        w0 = s.models["modelo-a"]["assigned"][b.address]
        net.send(a, "pin_model", {"name": "modelo-a"})        # a conseguiu: o arquivo é bom
        net.block()
        for _ in range(chain.ASSIGN_WINDOWS):
            s = net.step(a, b)                   # b sinaliza, mas nunca confirma o modelo
            self.assertEqual((set(s.models["modelo-a"]["assigned"]), s.machines[b.address]["reputation"]),
                             ({b.address}, 0))
        s = net.step(a, b)                       # 7 janelas depois: expira, b NÃO perde reputação e entra em cooldown
        w = s.mining["window"]
        self.assertEqual(s.machines[b.address]["reputation"], 0)
        self.assertEqual(s.models["modelo-a"]["assigned"], {})                  # a vaga volta, mas b não é reatribuído
        self.assertEqual(s.models["modelo-a"]["cooldown"], {b.address: w + chain.COOLDOWN_WINDOWS})
        self.assertEqual(s.models["modelo-a"]["failed"], 0)                     # a hospeda: não é "ninguém deu pin"

    def test_model_nobody_can_host_is_deleted_without_punishing_anyone(self):
        """Arquivo errado ou GGUF inválido: ninguém consegue hospedar. Nenhuma máquina perde reputação (cada vaga
        vencida vira cooldown, sem punição), o modelo soma `failed` e, na terceira vaga vencida, sai da rede
        devolvendo o orçamento ao padrinho."""
        net = self.net
        ms = self.MS[:3]
        for m in ms:
            net.send(m, "register_machine", machine(self.PAY))
        net.send(self.SPONSOR, "register_model", model("modelo-a", budget=5 * UNIT, replicas=1))
        s = net.block()
        before = s.bal(self.SPONSOR.address)
        # cada vaga é atribuída a uma máquina nova (as que já venceram ficam em cooldown); três vagas vencidas apagam.
        # viramos a janela e conferimos os créditos na própria virada que apaga, antes do bloco de pings os zerar.
        for _ in range(3 * (chain.ASSIGN_WINDOWS + 1) + 3):
            s = net.next_window()
            if "modelo-a" not in s.models:
                break
            for m in ms:
                net.send(m, "ping", {})
            net.block()
        self.assertNotIn("modelo-a", s.models)
        self.assertIn("modelo-a", s.retired_models)
        self.assertEqual([s.machines[m.address]["reputation"] for m in ms], [0, 0, 0])
        self.assertEqual(s.bal(self.SPONSOR.address) - before, 5 * UNIT)     # nunca houve aluguel: volta tudo
        self.assertIn([self.SPONSOR.address, "budget", 5 * UNIT, "modelo-a"], s.credits)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_assign_windows_grow_with_the_model_size(self):
        s = self.net.state
        W = s.params["window_seconds"]
        self.assertEqual(s.assign_windows({"size": 1}), chain.ASSIGN_WINDOWS)
        per = chain.ASSIGN_BYTES_PER_SECOND * W                    # bytes que dá para baixar numa janela
        self.assertEqual(s.assign_windows({"size": per * 40}), 40)
        self.assertEqual(s.assign_windows({"size": per * 40 + 1}), 41)
        big = State(dict(self.net.genesis, params=dict(FAST, window_seconds=300)))
        self.assertEqual(big.assign_windows({"size": 4_683_073_536}), math.ceil(4_683_073_536 / (500_000 * 300)))

    def test_offline_machine_is_replaced(self):
        net = self.net
        a, b = self.MS[:2]
        net.send(a, "register_machine", machine(self.PAY))
        net.send(self.SPONSOR, "register_model", model("modelo-a"))
        net.block()
        net.step(a)
        net.send(a, "pin_model", {"name": "modelo-a"})
        net.send(b, "register_machine", machine(WALLETS[4].address))
        s = net.block()
        self.assertEqual(list(s.models["modelo-a"]["pinners"]), [a.address])
        last = s.machines[a.address]["last_seen"]
        for _ in range(5):
            s = net.step(b)                      # só b continua sinalizando
            if not s.machines[a.address]["online"]:
                break
        self.assertFalse(s.machines[a.address]["online"])
        self.assertEqual(s.mining["window"], last + 3)
        self.assertEqual((s.models["modelo-a"]["pinners"], s.machines[a.address]["models"]), ({}, []))
        self.assertEqual(s.models["modelo-a"]["assigned"], {b.address: s.mining["window"]})

    def test_low_reputation_is_not_trusted(self):
        net = self.net
        a = self.MS[0]
        net.send(a, "register_machine", machine(self.PAY))
        net.send(self.SPONSOR, "register_model", model("modelo-a"))
        net.block()
        net.state.machines[a.address]["reputation"] = chain.MIN_REPUTATION   # ex.: muitos jobs abandonados
        self.assertEqual(net.next_window().models["modelo-a"]["assigned"], {})
        net.state.machines[a.address]["reputation"] = chain.MIN_REPUTATION + 1
        self.assertEqual(list(net.next_window().models["modelo-a"]["assigned"]), [a.address])

    def test_turning_ai_off_releases_models(self):
        net = self.net
        a = self.MS[0]
        net.send(a, "register_machine", machine(self.PAY))
        net.send(self.SPONSOR, "register_model", model("modelo-a"))
        net.block()
        net.step(a)
        net.send(a, "pin_model", {"name": "modelo-a"})
        net.block()
        net.send(a, "register_machine", machine(self.PAY, ai=False))
        s = net.block()
        self.assertEqual((s.models["modelo-a"]["pinners"], s.machines[a.address]["models"]), ({}, []))
        self.assertTrue(s.machines[a.address]["online"])
        self.assertEqual(net.step(a).models["modelo-a"]["assigned"], {})       # sem IA, não é sorteada


# ------------------------------------------------------------------- aluguel
class TestRent(unittest.TestCase):
    OWNER = WALLETS[2]
    A, B = WALLETS[10], WALLETS[11]
    PA, PB = WALLETS[3].address, WALLETS[4].address

    def setUp(self):
        self.net = Net()
        self.net.fund(self.OWNER.address)
        self.start = self.net.state.bal(self.OWNER.address)

    def test_site_rent_until_the_budget_runs_out(self):
        net = self.net
        files = {f"parte-{i}.bin": {"hash": sha(str(i).encode()), "size": 20_000_000} for i in range(25)}   # 500 MB
        budget = chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE * 25       # orçamento mínimo de 25 arquivos
        net.send(self.OWNER, "deploy_site", {"name": "meu-site", "manifest": files, "budget": budget, "replicas": 2})
        net.send(self.A, "register_machine", machine(self.PA))
        net.block()
        with self.assertRaisesRegex(TxError, "versão"):
            net.send(self.A, "pin_site", {"name": "meu-site", "version": 0})
        net.send(self.A, "pin_site", {"name": "meu-site", "version": 1})
        s = net.block()
        self.assertEqual(s.sites["meu-site"]["pinners"], {self.A.address: {"window": s.mining["window"]}})
        fee = math.ceil(500_000_000 * 5 / 1_000_000)          # 2.500 µSIG por janela, a preço da rede
        windows = budget // fee                               # 1 réplica hospedando: janelas que o orçamento paga
        for paid in range(1, windows + 1):
            s = net.step(self.A)
            site = s.sites["meu-site"]
            self.assertEqual((site["status"], site["budget"], site["paid"]),
                             ("active", budget - paid * fee, paid * fee))
            self.assertEqual((s.bal(self.PA), s.machines[self.A.address]["earned"]), (paid * fee, paid * fee))
            self.assertEqual(site["pinned_window"], s.mining["window"])
        s = net.next_window()
        site = s.sites["meu-site"]
        self.assertEqual((site["status"], site["pinners"], site["expired_window"]), ("expired", {}, s.mining["window"]))
        self.assertEqual(s.bal(self.PA), windows * fee)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_tiny_site_pays_at_least_one_usig(self):
        net = self.net
        net.send(self.OWNER, "deploy_site", {"name": "pequeno", "budget": chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE,
                                             "manifest": {"index.html": {"hash": sha(b"<h1>oi</h1>"), "size": 3000}}})
        net.send(self.A, "register_machine", machine(self.PA))
        net.block()
        net.send(self.A, "pin_site", {"name": "pequeno", "version": 1})
        net.block()
        s = net.next_window()
        self.assertEqual((s.sites["pequeno"]["paid"], s.bal(self.PA)), (1, 1))     # 3 KB: o mínimo de 1 µSIG
        self.assertEqual(s.credits, [[self.PA, "rent", 1, "pequeno"]])

    def test_pin_site_rules(self):
        net = self.net
        manifest = {"a.txt": {"hash": sha(b"a"), "size": 2_000_000}}
        net.send(self.OWNER, "deploy_site", {"name": "grande", "manifest": manifest, "budget": 100 * UNIT,
                                             "replicas": 1})
        net.send(self.A, "register_machine", machine(self.PA, hw={"disk_mb": 1}))
        net.send(self.B, "register_machine", machine(self.PB))
        net.block()
        with self.assertRaisesRegex(TxError, "espaço"):
            net.send(self.A, "pin_site", {"name": "grande", "version": 1})
        with self.assertRaisesRegex(TxError, "não existe"):
            net.send(self.B, "pin_site", {"name": "outro", "version": 1})
        with self.assertRaisesRegex(TxError, "não registrada"):
            net.send(self.OWNER, "pin_site", {"name": "grande", "version": 1})
        net.send(self.B, "pin_site", {"name": "grande", "version": 1})
        with self.assertRaisesRegex(TxError, "já hospeda"):
            net.send(self.B, "pin_site", {"name": "grande", "version": 1})
        net.block()
        net.send(self.A, "register_machine", machine(self.PA))                # agora com disco
        net.block()
        with self.assertRaisesRegex(TxError, "réplicas"):
            net.send(self.A, "pin_site", {"name": "grande", "version": 1})
        for _ in range(3):
            s = net.next_window()                                            # sem ping: ficam offline
        self.assertEqual(s.sites["grande"]["pinners"], {})
        self.assertEqual(s.bal(self.PB), 2 * 10)                             # 2 MB × 5 µSIG por janela, 2 janelas
        with self.assertRaisesRegex(TxError, "offline"):
            net.send(self.B, "pin_site", {"name": "grande", "version": 1})

    def test_new_site_version_needs_new_pins(self):
        net = self.net
        novo = chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE            # 1 arquivo no manifest
        net.send(self.OWNER, "deploy_site", {"name": "site-v", "manifest": {"i.html": {"hash": H, "size": 10}},
                                             "budget": novo})
        net.send(self.A, "register_machine", machine(self.PA))
        net.block()
        net.send(self.A, "pin_site", {"name": "site-v", "version": 1})
        net.block()
        net.send(self.OWNER, "deploy_site", {"name": "site-v", "manifest": {"i.html": {"hash": H, "size": 10}},
                                             "budget": 5})                    # recarregar: mesma versão
        s = net.block()
        self.assertEqual((s.sites["site-v"]["version"], s.sites["site-v"]["budget"]), (1, novo + 5))
        self.assertIn(self.A.address, s.sites["site-v"]["pinners"])
        net.send(self.OWNER, "deploy_site", {"name": "site-v", "manifest": {"i.html": {"hash": sha(b"2"), "size": 10}}})
        s = net.block()
        self.assertEqual((s.sites["site-v"]["version"], s.sites["site-v"]["pinners"]), (2, {}))
        with self.assertRaisesRegex(TxError, "outra conta"):
            net.send(self.A, "deploy_site", {"name": "site-v", "manifest": {"x": {"hash": H, "size": 1}}, "budget": 1})

    def test_model_rent_and_deletion(self):
        net = self.net
        net.send(self.A, "register_machine", machine(self.PA))
        net.send(self.B, "register_machine", machine(self.PB))
        net.send(self.OWNER, "register_model", model("modelo-a", budget=22, replicas=2))
        net.block()
        s = net.step(self.A, self.B)
        self.assertEqual(set(s.models["modelo-a"]["assigned"]), {self.A.address, self.B.address})
        net.send(self.A, "pin_model", {"name": "modelo-a"})
        net.send(self.B, "pin_model", {"name": "modelo-a"})
        job = net.send(self.OWNER, "create_job", {"kind": "ai", "model": "modelo-a", "spec_hash": H,
                                                  "escrow": 1000, "max_price": 2000, "max_tokens": 100})
        net.block()
        s = net.step(self.A, self.B)            # 1 MB a 5 µSIG/MB por janela: 5 para cada réplica
        m = s.models["modelo-a"]
        self.assertEqual((m["budget"], m["rent_paid"], s.bal(self.PA), s.bal(self.PB)), (12, 10, 5, 5))
        s = net.step(self.A, self.B)
        self.assertEqual(s.models["modelo-a"]["budget"], 2)
        s = net.step(self.A, self.B)            # 2 não cobre 10: o modelo é apagado da rede
        self.assertNotIn("modelo-a", s.models)
        self.assertIn("modelo-a", s.retired_models)
        self.assertEqual(s.jobs[job["id"]]["status"], "expired")
        self.assertEqual(s.bal(self.OWNER.address), self.start - 20)      # troco do budget e o escrow voltam
        self.assertEqual((s.machines[self.A.address]["models"], s.machines[self.B.address]["models"]), ([], []))
        self.assertEqual((s.bal(self.PA), s.bal(self.PB)), (10, 10))
        self.assertEqual(supply(s), s.mining["minted"])
        with self.assertRaisesRegex(TxError, "não existe"):
            net.send(self.OWNER, "create_job", {"kind": "ai", "model": "modelo-a", "spec_hash": H,
                                                "escrow": 1000, "max_price": 2000})

    def test_rent_needs_the_full_sum(self):
        """Sem budget para todas as réplicas não há pagamento parcial: a janela que não cobre as duas cópias não
        paga ninguém e o site expira com o resto do orçamento."""
        net = self.net
        files = {f"p{i}": {"hash": H, "size": 20_004_000} for i in range(50)}   # 1.000.200.000 bytes
        budget = chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE * 50       # orçamento mínimo de 50 arquivos
        net.send(self.OWNER, "deploy_site", {"name": "dois", "manifest": files, "budget": budget, "replicas": 2})
        net.send(self.A, "register_machine", machine(self.PA))
        net.send(self.B, "register_machine", machine(self.PB))
        net.block()
        net.send(self.A, "pin_site", {"name": "dois", "version": 1})
        net.send(self.B, "pin_site", {"name": "dois", "version": 1})
        net.block()
        fee = math.ceil(1_000_200_000 * 5 / 1_000_000)        # 5.001 µSIG por cópia por janela
        windows = budget // (2 * fee)                         # janelas que pagam as duas cópias
        for _ in range(windows):
            s = net.step(self.A, self.B)
        left = budget - windows * 2 * fee
        self.assertEqual((s.sites["dois"]["status"], s.sites["dois"]["budget"]), ("active", left))
        s = net.step(self.A, self.B)                          # a próxima não cobre 2 × 5.001: expira sem pagar ninguém
        self.assertEqual(s.sites["dois"]["status"], "expired")
        self.assertEqual((s.bal(self.PA), s.bal(self.PB), s.sites["dois"]["budget"]),
                         (windows * fee, windows * fee, left))   # sem pagamento parcial: o resto do orçamento fica

    def test_rent_uses_the_network_price(self):
        """O aluguel é parâmetro da rede: a máquina não escolhe quanto cobra do orçamento dos outros."""
        net = Net(hosting_per_mb_window=7)
        net.fund(self.OWNER.address)
        net.send(self.OWNER, "deploy_site", {"name": "preco", "manifest": {"a": {"hash": H, "size": 1_000_000}},
                                             "budget": UNIT, "replicas": 1})
        net.send(self.OWNER, "register_model", model("modelo-p", budget=UNIT))
        net.send(self.A, "register_machine", machine(self.PA))
        net.block()
        net.send(self.A, "pin_site", {"name": "preco", "version": 1})
        s = net.step(self.A)
        net.send(self.A, "pin_model", {"name": "modelo-p"})
        net.block()
        self.assertNotIn("price", s.sites["preco"]["pinners"][self.A.address])
        before = net.state.bal(self.PA)
        s = net.next_window()                   # 1 MB × 7 µSIG por MB por janela, para o site e para o modelo
        self.assertEqual(s.bal(self.PA) - before, 7 + 7)
        self.assertEqual(sorted(s.credits), [[self.PA, "rent", 7, "modelo-p"], [self.PA, "rent", 7, "preco"]])


# ---------------------------------------------------------------------- jobs
class TestJobs(unittest.TestCase):
    CLIENT, SPONSOR = WALLETS[0], WALLETS[1]
    A, B, C = WALLETS[10], WALLETS[11], WALLETS[12]
    PA, PB, PC = WALLETS[3].address, WALLETS[4].address, WALLETS[5].address

    def setUp(self):
        self.net = Net(window_seconds=60, epoch_seconds=600)    # janela longa: o job inteiro cabe numa só
        self.net.fund(self.CLIENT.address, self.SPONSOR.address)

    def ai_ready(self, **kw):
        net = self.net
        net.send(self.A, "register_machine", machine(self.PA))
        net.send(self.SPONSOR, "register_model", model("modelo-a", **kw))
        net.block()
        net.step(self.A)
        net.send(self.A, "pin_model", {"name": "modelo-a"})
        return net.block()

    def create_ai(self, escrow=5000, max_price=2000, max_tokens=None):
        if max_tokens is None:
            max_tokens = max(1, escrow * 1000 // max_price - 16)     # o teto que o escrow reservado cobre
        tx = self.net.send(self.CLIENT, "create_job", {"kind": "ai", "model": "modelo-a", "spec_hash": sha(b"p"),
                                                       "escrow": escrow, "max_price": max_price,
                                                       "max_tokens": max_tokens})
        self.net.block()
        return tx["id"]

    def compute_job(self, replicas=3):
        tx = self.net.send(self.CLIENT, "create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 1000}},
                                                       "replicas": replicas, "escrow": 500 * replicas, "max_price": 500})
        self.net.block()
        return tx["id"]

    def test_pending_uses_the_last_block_clock(self):
        """O estado pendente aplica txs sem begin_block: vale a altura/hora do último bloco."""
        net = self.net
        last = (net.state.height, net.state.now)
        tx = net.send(self.CLIENT, "create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 10}},
                                                  "replicas": 1, "escrow": 500, "max_price": 500})
        self.assertEqual((net.pending.height, net.pending.now), last)
        self.assertEqual(net.pending.jobs[tx["id"]]["created"], last[1])
        s = net.block()
        self.assertEqual((s.height, s.now), (net.height, net.time))
        self.assertEqual(s.jobs[tx["id"]]["created"], s.now)

    def test_ai_job_pays_payout_and_sponsor(self):
        net = self.net
        s = self.ai_ready(royalty_bps=1000, reinvest_bps=5000)
        before = {a: s.bal(a) for a in (self.CLIENT.address, self.SPONSOR.address, self.PA)}
        budget = s.models["modelo-a"]["budget"]
        jid = self.create_ai()
        net.send(self.A, "claim_job", {"job_id": jid})
        s = net.block()
        self.assertEqual(s.jobs[jid]["status"], "running")
        self.assertEqual(s.jobs[jid]["claims"], {self.A.address: {"time": s.now, "price": 2000}})
        net.send(self.A, "submit_result", {"job_id": jid, "result_hash": sha(b"r"),
                                           "usage": {"tokens_in": 100, "tokens_out": 400}})
        s = net.block()
        job = s.jobs[jid]
        # 500 tokens × 2000 µSIG/1k = 1000; royalty de 10% = 100, metade reinvestida na hospedagem
        self.assertEqual((job["status"], job["cost"], job["royalty"], job["result_hash"], job["closed"]),
                         ("done", 1000, 100, sha(b"r"), s.now))
        self.assertEqual(job["payments"], {self.A.address: 1000})
        self.assertEqual(s.bal(self.PA) - before[self.PA], 900)                 # 100% do preço menos o royalty
        self.assertEqual(s.bal(self.SPONSOR.address) - before[self.SPONSOR.address], 50)
        self.assertEqual(s.models["modelo-a"]["budget"] - budget, 50)
        self.assertEqual(before[self.CLIENT.address] - s.bal(self.CLIENT.address), 1000)
        m = s.models["modelo-a"]
        self.assertEqual((m["royalties"], m["tokens"], m["jobs"]), (100, 500, 1))
        mc = s.machines[self.A.address]
        self.assertEqual((mc["jobs_done"], mc["earned"], mc["reputation"]), (1, 900, 1))
        self.assertEqual(supply(s), s.mining["minted"])                         # nada queimado, nada criado

    def test_ai_cost_is_capped_by_escrow(self):
        net = self.net
        self.ai_ready(royalty_bps=0)
        jid = self.create_ai(escrow=300)
        net.send(self.A, "claim_job", {"job_id": jid})
        net.send(self.A, "submit_result", {"job_id": jid, "result_hash": H,
                                           "usage": {"tokens_in": 10_000, "tokens_out": 10_000}})
        s = net.block()
        self.assertEqual((s.jobs[jid]["cost"], s.bal(self.PA)), (300, 300))

    def test_claim_rules(self):
        net = self.net
        self.ai_ready()
        net.send(self.B, "register_machine", machine(self.PB))                     # online, mas sem o modelo
        net.send(self.C, "register_machine", machine(self.PC, prices={"compute_per_unit": 501}))
        net.block()
        jid = self.create_ai()
        cid = self.compute_job(replicas=1)
        with self.assertRaisesRegex(TxError, "não hospeda"):
            net.send(self.B, "claim_job", {"job_id": jid})
        with self.assertRaisesRegex(TxError, "não registrada"):
            net.send(self.SPONSOR, "claim_job", {"job_id": jid})
        for bad in ("00" * 32, ["x"], None):
            with self.assertRaisesRegex(TxError, "não existe"):
                net.send(self.A, "claim_job", {"job_id": bad})
        with self.assertRaisesRegex(TxError, "acima do máximo"):
            net.send(self.C, "claim_job", {"job_id": cid})
        net.send(self.A, "claim_job", {"job_id": jid})
        with self.assertRaisesRegex(TxError, "não está aberto"):
            net.send(self.A, "claim_job", {"job_id": jid})
        net.block()
        for _ in range(3):
            net.next_window()                                                     # B não sinaliza
        with self.assertRaisesRegex(TxError, "offline"):
            net.send(self.B, "claim_job", {"job_id": cid})
        with self.assertRaisesRegex(TxError, "não reivindicou"):
            net.send(self.B, "submit_result", {"job_id": cid, "result_hash": H})

    def test_create_job_validation(self):
        net = self.net
        self.ai_ready()
        bad = [{"kind": "x", "escrow": 1, "max_price": 1},
               {"kind": "ai", "model": "modelo-a", "spec_hash": "12", "escrow": 1, "max_price": 1},
               {"kind": "ai", "model": "outro", "spec_hash": H, "escrow": 1, "max_price": 1},
               {"kind": "ai", "model": ["modelo-a"], "spec_hash": H, "escrow": 1, "max_price": 1},
               {"kind": "ai", "model": "modelo-a", "spec_hash": H, "escrow": 0, "max_price": 1},
               {"kind": "ai", "model": "modelo-a", "spec_hash": H, "escrow": 10 ** 18, "max_price": 1},
               {"kind": "compute", "task": {"name": "nada"}, "escrow": 1, "max_price": 1},
               {"kind": "compute", "task": {"name": ["primes"]}, "escrow": 1, "max_price": 1},
               {"kind": "compute", "task": {"name": "primes", "params": {"n": 10}}, "replicas": 8, "escrow": 1,
                "max_price": 1}]
        for d in bad:
            with self.subTest(d=d), self.assertRaises(TxError):
                net.send(self.CLIENT, "create_job", d)

    def test_compute_majority_paid_and_divergent_only_counts_a_dispute(self):
        net = self.net
        for w, p in ((self.A, self.PA), (self.B, self.PB), (self.C, self.PC)):
            net.send(w, "register_machine", machine(p))
        net.mine(self.PA, self.PB, self.PC)      # os donos também mineram nesta época
        net.block()
        jid = self.compute_job()
        for w in (self.A, self.B, self.C):
            net.send(w, "claim_job", {"job_id": jid})
        self.assertEqual(net.block().jobs[jid]["status"], "running")
        right, wrong = sha(b'{"count":168}'), sha(b'{"count":169}')
        for w, h in ((self.A, right), (self.B, right), (self.C, wrong)):
            net.send(w, "submit_result", {"job_id": jid, "result_hash": h})
        before = net.state.bal(self.CLIENT.address)
        s = net.block()
        job = s.jobs[jid]
        self.assertEqual((job["status"], job["result_hash"], job["cost"]), ("done", right, 1000))
        self.assertEqual((s.bal(self.PA), s.bal(self.PB), s.bal(self.PC)), (500, 500, 0))
        self.assertEqual(s.bal(self.CLIENT.address) - before, 500)              # a parte da réplica errada volta
        # quem divergiu da maioria não recebe, mas NÃO perde reputação (senão dois Sybils drenam a honesta de graça,
        # achado compute-vote-sybil-rep-drain): só conta uma disputa, que o painel mostra e _assign_models ignora
        self.assertEqual([s.machines[w.address]["reputation"] for w in (self.A, self.B, self.C)], [1, 1, 0])
        self.assertEqual([s.machines[w.address]["compute_disputes"] for w in (self.A, self.B, self.C)], [0, 0, 1])
        self.assertEqual((wp(s)[self.PA], wp(s)[self.PB], wp(s)[self.PC]), (16, 16, 16))   # mineração intacta
        self.assertEqual(sorted(s.credits), sorted([[self.PA, "job", 500, jid], [self.PB, "job", 500, jid],
                                                    [self.CLIENT.address, "change", 500, jid]]))
        rewards = net.pay()                                      # quem divergiu continua recebendo pela mineração
        self.assertEqual(rewards[self.PA], rewards[self.PB])
        self.assertEqual(rewards[self.PC], rewards[self.PA])
        self.assertEqual(supply(net.state), net.state.mining["minted"])

    def test_compute_without_majority_is_disputed(self):
        net = self.net
        for w, p in ((self.A, self.PA), (self.B, self.PB)):
            net.send(w, "register_machine", machine(p))
        net.block()
        jid = self.compute_job(replicas=2)
        net.send(self.A, "claim_job", {"job_id": jid})
        net.send(self.B, "claim_job", {"job_id": jid})
        net.block()
        before = net.state.bal(self.CLIENT.address)
        net.send(self.A, "submit_result", {"job_id": jid, "result_hash": sha(b"1")})
        net.send(self.B, "submit_result", {"job_id": jid, "result_hash": sha(b"2")})
        s = net.block()
        self.assertEqual((s.jobs[jid]["status"], s.jobs[jid]["cost"]), ("disputed", 0))
        self.assertEqual(s.bal(self.CLIENT.address) - before, 1000)
        self.assertEqual([s.machines[w.address]["reputation"] for w in (self.A, self.B)], [0, 0])

    def test_time_based_timeouts(self):
        net = self.net
        net.send(self.A, "register_machine", machine(self.PA))
        net.block()
        jid = self.compute_job(replicas=1)
        created = net.state.jobs[jid]["created"]
        net.send(self.A, "claim_job", {"job_id": jid})
        s = net.block()
        claimed = s.jobs[jid]["claims"][self.A.address]["time"]
        s = net.block(at=claimed + chain.CLAIM_TIMEOUT)            # no limite ainda vale
        self.assertEqual(s.jobs[jid]["status"], "running")
        s = net.block(at=claimed + chain.CLAIM_TIMEOUT + 1)        # sumiu: perde reputação e o job reabre
        self.assertEqual((s.jobs[jid]["status"], s.jobs[jid]["claims"]), ("open", {}))
        self.assertEqual(s.machines[self.A.address]["reputation"], -1)
        before = s.bal(self.CLIENT.address)
        s = net.block(at=created + chain.OPEN_TIMEOUT + 1)         # ninguém entregou: expira e devolve
        self.assertEqual(s.jobs[jid]["status"], "expired")
        self.assertEqual(s.bal(self.CLIENT.address) - before, 500)         # escrow de 1 réplica × 500
        s = net.block(at=s.jobs[jid]["closed"] + chain.JOB_RETENTION)
        self.assertIn(jid, s.jobs)
        s = net.block(at=s.jobs[jid]["closed"] + chain.JOB_RETENTION + 1)
        self.assertNotIn(jid, s.jobs)                              # fica só no histórico dos blocos
        self.assertEqual(supply(s), s.mining["minted"])

    def test_retire_model_refunds_everyone(self):
        net = self.net
        self.ai_ready()
        jid = self.create_ai()
        net.send(self.A, "claim_job", {"job_id": jid})
        net.block()
        before_c, before_s = net.state.bal(self.CLIENT.address), net.state.bal(self.SPONSOR.address)
        budget = net.state.models["modelo-a"]["budget"]
        with self.assertRaisesRegex(TxError, "padrinho"):
            net.send(self.CLIENT, "retire_model", {"name": "modelo-a"})
        net.send(self.SPONSOR, "retire_model", {"name": "modelo-a"})
        s = net.block()
        self.assertNotIn("modelo-a", s.models)
        self.assertEqual(s.retired_models["modelo-a"], s.now)
        self.assertEqual(s.jobs[jid]["status"], "expired")
        self.assertEqual(s.bal(self.CLIENT.address) - before_c, 5000)
        self.assertEqual(s.bal(self.SPONSOR.address) - before_s, budget)
        self.assertEqual(s.machines[self.A.address]["models"], [])
        with self.assertRaisesRegex(TxError, "encerrado"):
            net.send(self.A, "submit_result", {"job_id": jid, "result_hash": H,
                                               "usage": {"tokens_in": 1, "tokens_out": 1}})

    def test_sponsor_controls(self):
        net = self.net
        net.send(self.SPONSOR, "register_model", model("modelo-a"))
        net.block()
        net.send(self.SPONSOR, "fund_model", {"name": "modelo-a", "amount": 5 * UNIT})
        net.send(self.SPONSOR, "update_model", {"name": "modelo-a", "royalty_bps": 2500, "reinvest_bps": 10000,
                                                "new_sponsor": to_eth(self.CLIENT.address)})
        s = net.block()
        m = s.models["modelo-a"]
        self.assertEqual((m["budget"], m["royalty_bps"], m["reinvest_bps"], m["sponsor"]),
                         (15 * UNIT, 2500, 10000, self.CLIENT.address))
        with self.assertRaisesRegex(TxError, "padrinho"):
            net.send(self.SPONSOR, "fund_model", {"name": "modelo-a", "amount": 1})
        with self.assertRaisesRegex(TxError, "royalty_bps"):
            net.send(self.CLIENT, "update_model", {"name": "modelo-a", "royalty_bps": 5001})

    def test_register_model_validation(self):
        net = self.net
        bad = [model("ab"), model("Maiusculo"), model("modelo-a\n"), model("modelo-a", url="ftp://x/m.gguf"),
               model("modelo-a", url="https://a b"), model("modelo-a", url=None), model("modelo-a", sha256="12" * 31),
               model("modelo-a", sha256=None), model("modelo-a", size=0), model("modelo-a", size=60_000_000_001),
               model("modelo-a", size=1.5), model("modelo-a", royalty_bps=5001), model("modelo-a", replicas=6),
               model("modelo-a", replicas=0), model("modelo-a", budget=0), model("modelo-a", budget=10 ** 15),
               model("modelo-a", license=5)]
        for d in bad:
            with self.subTest(d=d), self.assertRaises(TxError):
                net.send(self.SPONSOR, "register_model", d)
        # modelo grande (acima de FUNDED_ABOVE) sem orçamento para um dia de hospedagem é recusado na hora
        # (achado window-turnover-models-x-machines): não vira lixo de estado que ninguém consegue baixar.
        with self.assertRaisesRegex(TxError, "o orçamento precisa cobrir um dia de hospedagem"):
            net.send(self.SPONSOR, "register_model", model("grande", size=chain.FUNDED_ABOVE + 1, budget=1))
        digest = "74A4DA8C9FDBCD15BD1F6D01D621410D31C6FC00986F5EB687824E7B93D7A9DB"
        big_size = 491_400_032
        minimo = net.state.rent_window({"replicas": 3, "size": big_size}) * \
            chain._ceil_div(chain.MIN_FUNDED_SECONDS, net.state.params["window_seconds"])
        net.send(self.SPONSOR, "register_model",
                 model("qwen2.5-0.5b", size=big_size, sha256=digest, replicas=3, budget=minimo))
        d = model("padrao")
        del d["replicas"], d["royalty_bps"], d["reinvest_bps"]
        net.send(self.SPONSOR, "register_model", d)
        s = net.block()
        m = s.models["qwen2.5-0.5b"]
        self.assertEqual(set(m), {"name", "sponsor", "url", "sha256", "size", "ram_mb", "license", "description",
                                  "royalty_bps", "reinvest_bps", "replicas", "budget", "rent_paid", "royalties",
                                  "tokens", "jobs", "assigned", "pinners", "cooldown", "former", "failed",
                                  "pinned_window", "created"})
        self.assertEqual((m["cooldown"], m["former"], m["failed"], m["pinned_window"]), ({}, {}, 0, s.mining["window"]))
        self.assertEqual((m["sha256"], m["ram_mb"], m["sponsor"]),
                         (digest.lower(), math.ceil(491_400_032 / 1e6 * 1.2) + 512, self.SPONSOR.address))
        p = s.models["padrao"]
        self.assertEqual((p["replicas"], p["royalty_bps"], p["reinvest_bps"]), (2, 1000, 0))
        with self.assertRaisesRegex(TxError, "já existe"):
            net.send(self.SPONSOR, "register_model", model("qwen2.5-0.5b"))


# ------------------------------------------------------------ MetaMask (EVM)
class TestEVM(unittest.TestCase):
    KEY = WALLETS[7]
    DEST = WALLETS[8].address

    def setUp(self):
        self.net = Net()
        self.net.fund(self.KEY.address)
        self.bal = self.net.state.bal(self.KEY.address)

    def test_metamask_transfer(self):
        net = self.net
        tx = evm_tx(self.KEY.priv, 0, to_eth(self.DEST), 5 * UNIT * evm.WEI_PER_USIG)
        self.assertEqual(tx["id"], keccak256(bytes.fromhex(tx["data"]["raw"][2:])).hex())   # o hash da MetaMask
        net.submit(tx)
        s = net.block()
        self.assertEqual((s.bal(self.DEST), s.bal(self.KEY.address)), (5 * UNIT, self.bal - 5 * UNIT))
        self.assertEqual(s.nonces[self.KEY.address], 1)
        summary = tx_summary(tx)
        self.assertEqual({k: summary[k] for k in ("type", "from", "to", "amount", "nonce", "via")},
                         {"type": "transfer", "from": self.KEY.address, "to": self.DEST, "amount": 5 * UNIT,
                          "nonce": 0, "via": "metamask"})
        self.assertEqual(tx_sender(tx), self.KEY.address)
        with self.assertRaisesRegex(TxError, "nonce"):
            net.submit(tx)                                      # a mesma tx de novo

    def test_metamask_sigma_operations(self):
        net = self.net
        reg = {"type": "register_model", "data": model("via-metamask", budget=3 * UNIT)}
        tx = net.submit(evm_tx(self.KEY.priv, 0, evm.SYSTEM_ADDRESS, data=json.dumps(reg).encode()))
        mach = {"type": "register_machine", "data": machine(self.KEY.address)}
        net.submit(evm_tx(self.KEY.priv, 1, evm.SYSTEM_ADDRESS, data=json.dumps(mach).encode()))
        s = net.block()
        self.assertEqual(s.models["via-metamask"]["sponsor"], self.KEY.address)
        self.assertEqual(s.bal(self.KEY.address), self.bal - 3 * UNIT)
        self.assertTrue(s.machines[self.KEY.address]["online"])
        self.assertEqual(s.nonces[self.KEY.address], 2)
        summary = tx_summary(tx)
        self.assertEqual((summary["type"], summary["amount"], summary["name"], summary["via"]),
                         ("register_model", 3 * UNIT, "via-metamask", "metamask"))
        net.next_window()
        ping = {"type": "ping", "data": {}}
        net.submit(evm_tx(self.KEY.priv, 2, evm.SYSTEM_ADDRESS, data=json.dumps(ping).encode()))
        self.assertEqual(net.block().machines[self.KEY.address]["last_seen"], net.window)

    def test_metamask_rejections(self):
        net = self.net
        dest = to_eth(self.DEST)
        op = lambda t: json.dumps({"type": t, "data": {}}).encode()    # noqa: E731
        cases = {
            "chain id": evm_tx(self.KEY.priv, 0, dest, evm.WEI_PER_USIG, chain_id=1),
            "nonce": evm_tx(self.KEY.priv, 5, dest, evm.WEI_PER_USIG),
            "6 casas": evm_tx(self.KEY.priv, 0, dest, 1),
            "saldo": evm_tx(self.KEY.priv, 0, dest, (self.bal + 1) * evm.WEI_PER_USIG),
            "não levam valor": evm_tx(self.KEY.priv, 0, evm.SYSTEM_ADDRESS, evm.WEI_PER_USIG, data=op("ping")),
            "desconhecida: work": evm_tx(self.KEY.priv, 0, evm.SYSTEM_ADDRESS, data=op("work")),
            "desconhecida: evm": evm_tx(self.KEY.priv, 0, evm.SYSTEM_ADDRESS, data=op("evm")),
            "JSON": evm_tx(self.KEY.priv, 0, evm.SYSTEM_ADDRESS, data=b"nada"),
        }
        for msg, tx in cases.items():
            with self.subTest(msg), self.assertRaisesRegex(TxError, msg):
                net.submit(tx)
        with self.assertRaisesRegex(TxError, "Ethereum"):
            net.submit({"type": "evm", "data": {"raw": "0xdeadbeef"}})
        self.assertEqual(net.pending.nonces.get(self.KEY.address, 0), 0)


# ------------------------------------------------------------------- resumos
class TestSummary(unittest.TestCase):
    def test_every_tx_type(self):
        w, other = WALLETS[0], WALLETS[1].address
        signed = lambda t, d: make_tx(w, t, d, 3)                     # noqa: E731
        cases = [
            (work_tx(w.address, 4, 2, [1, 2]), {"type": "work", "from": w.address, "to": None, "amount": 0,
                                                 "nonce": 0, "via": "sigma"}),
            (signed("ping", {}), {"type": "ping", "from": w.address, "to": None, "amount": 0, "nonce": 3}),
            (signed("register_machine", machine(other, name="sala")),
             {"type": "register_machine", "from": w.address, "name": "sala", "amount": 0, "to": None}),
            (signed("transfer", {"to": to_eth(other), "amount": 7}), {"type": "transfer", "to": other, "amount": 7}),
            (signed("create_job", {"kind": "ai", "escrow": 9, "model": "m", "spec_hash": H, "max_price": 1}),
             {"type": "create_job", "amount": 9}),
            (signed("claim_job", {"job_id": H}), {"type": "claim_job", "amount": 0}),
            (signed("submit_result", {"job_id": H, "result_hash": H}), {"type": "submit_result", "amount": 0}),
            (signed("submit_result", {"job_id": H, "result_hash": H, "failed": True}), {"type": "submit_result"}),
            (signed("cancel_job", {"job_id": H}), {"type": "cancel_job", "amount": 0, "from": w.address}),
            (work_tx(w.address, 4, 2, [1], worker="a1b2c3d4"), {"type": "work", "from": w.address}),
            (signed("deploy_site", {"name": "site", "manifest": {}, "budget": 11}),
             {"type": "deploy_site", "amount": 11, "name": "site"}),
            (signed("pin_site", {"name": "site", "version": 1}), {"type": "pin_site", "name": "site"}),
            (signed("register_model", model("m-a", budget=12)), {"type": "register_model", "amount": 12, "name": "m-a"}),
            (signed("fund_model", {"name": "m-a", "amount": 13}), {"type": "fund_model", "amount": 13}),
            (signed("update_model", {"name": "m-a", "royalty_bps": 1}), {"type": "update_model", "amount": 0}),
            (signed("retire_model", {"name": "m-a"}), {"type": "retire_model", "name": "m-a"}),
            (signed("pin_model", {"name": "m-a"}), {"type": "pin_model", "name": "m-a"}),
        ]
        for tx, want in cases:
            with self.subTest(tx["type"]):
                s = tx_summary(tx)
                self.assertEqual(set(s), {"id", "type", "from", "to", "amount", "nonce", "via", "name"})
                self.assertEqual(s["id"], tx_id(tx))
                self.assertEqual({k: s[k] for k in want}, want)
        self.assertEqual(tx_summary(work_tx("lixo", 0, 2, [1]))["from"], None)
        self.assertEqual(tx_sender(work_tx("0x" + w.address[3:], 0, 2, [1])), w.address)

    def test_malformed_never_raises(self):
        """O nó resume qualquer coisa que vê (mempool, blocos, RPC): lixo vira um resumo vazio, nunca exceção."""
        w = WALLETS[0]
        junk = [None, 5, [], {}, {"type": "evm"}, {"type": "evm", "data": {"raw": 5}}, {"type": "evm", "data": []},
                {"type": "evm", "data": {"raw": "0xzz"}}, {"type": "evm", "data": {"raw": "0x" + "c0"}},
                {"type": "transfer", "data": {"to": ["x"], "amount": 10 ** 5000}, "pub": "nada", "nonce": "1"},
                {"type": ["lista"], "data": {"name": "\ud800"}, "pub": w.pub},
                {"type": "deploy_site", "data": {"name": 7, "budget": -5}, "pub": 5, "id": ["x"]},
                {"type": "work", "data": {"miner": {"a": 1}}}, {"type": "cancel_job", "data": "x", "pub": w.pub}]
        for i, tx in enumerate(junk):
            with self.subTest(i=i):
                s = tx_summary(tx)
                self.assertEqual(set(s), {"id", "type", "from", "to", "amount", "nonce", "via", "name"})
                json.dumps(s, ensure_ascii=False).encode()          # o histórico consegue gravar
        s = tx_summary({"type": "transfer", "data": {"to": "sig" + "1" * 38 + "_1", "amount": 10 ** 5000},
                        "pub": w.pub, "nonce": 2})
        self.assertEqual((s["from"], s["to"], s["amount"], s["nonce"]), (w.address, None, 0, 2))


# ------------------------------------------------------------ determinismo
class TestDeterminism(unittest.TestCase):
    @staticmethod
    def scenario():
        """Um pouco de tudo: mineração, máquinas, modelo, site, job, aluguel e duas liquidações."""
        net = Net()
        alice, bob, a, b = WALLETS[0], WALLETS[1], WALLETS[10], WALLETS[11]
        pa, pb = WALLETS[3].address, WALLETS[4].address
        net.fund(alice.address, bob.address)
        net.send(a, "register_machine", machine(pa))
        net.send(b, "register_machine", machine(pb))
        net.send(alice, "register_model", model("modelo-a", replicas=2, budget=1000))
        net.send(bob, "deploy_site", {"name": "site-b", "manifest": {"i.html": {"hash": H, "size": 5000}},
                                      "budget": chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE})
        net.block()
        net.step(a, b)
        for w in (a, b):
            net.send(w, "pin_model", {"name": "modelo-a"})
            net.send(w, "pin_site", {"name": "site-b", "version": 1})
        net.mine(pa, pb)
        net.block()
        job = net.send(bob, "create_job", {"kind": "ai", "model": "modelo-a", "spec_hash": H, "escrow": 3000,
                                           "max_price": 2000, "max_tokens": 1000})
        net.block()
        net.send(a, "claim_job", {"job_id": job["id"]})
        net.block()
        net.send(a, "submit_result", {"job_id": job["id"], "result_hash": H, "usage": {"tokens_in": 10, "tokens_out": 20}})
        net.send(alice, "transfer", {"to": bob.address, "amount": 1234})
        net.block()
        for _ in range(8):
            net.step(a, b)
            net.mine(pa)
        net.block()
        net.settle()
        return net

    @classmethod
    def setUpClass(cls):
        cls.net = cls.scenario()

    def test_replay_gives_the_same_root_every_block(self):
        s = State(self.net.genesis)
        for h, t, prev, txs, root in self.net.blocks:
            apply_block(s, h, t, prev, txs)
            self.assertEqual(s.root(), root, f"divergiu no bloco {h}")
        self.assertEqual(canon(s.to_dict()), canon(self.net.state.to_dict()))
        final = self.net.state
        self.assertGreaterEqual(len(final.epochs), 2)
        self.assertEqual(supply(final), final.mining["minted"])
        self.assertGreater(final.models["modelo-a"]["rent_paid"], 0)
        self.assertGreater(final.sites["site-b"]["paid"], 0)

    def test_two_independent_runs_agree(self):
        other = self.scenario()
        self.assertEqual([b[4] for b in other.blocks], [b[4] for b in self.net.blocks])

    def test_snapshot_roundtrip_continues_identically(self):
        cut = len(self.net.blocks) // 2
        s = State(self.net.genesis)
        for h, t, prev, txs, root in self.net.blocks[:cut]:
            apply_block(s, h, t, prev, txs)
        s2 = State.from_dict(self.net.genesis, json.loads(json.dumps(s.to_dict())))   # como o snapshot do nó
        s2.height, s2.now = s.height, s.now
        self.assertEqual(s2.root(), self.net.blocks[cut - 1][4])
        for h, t, prev, txs, root in self.net.blocks[cut:]:
            apply_block(s2, h, t, prev, txs)
            self.assertEqual(s2.root(), root, f"divergiu no bloco {h}")

    def test_clone_is_independent(self):
        s = self.net.state
        root = s.root()
        c = s.clone()
        c.balances[WALLETS[0].address] = 1
        c.mining["seeds"]["x"] = "y"
        self.assertEqual(s.root(), root)


if __name__ == "__main__":
    unittest.main()
