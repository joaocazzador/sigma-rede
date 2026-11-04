"""Testes do nó (sigma/node.py), do JSON-RPC da MetaMask (sigma/rpc.py), das operações do Sigma dentro de
uma tx da MetaMask (sigma/evm.py), do fórum (sigma/forum.py) e da configuração da VPS (deploy/).

    python -m unittest tests.test_node -v

Cada teste roda um nó produtor de verdade numa pasta temporária, com relógio falso (o teste avança o tempo
e fecha os blocos). Os cenários de ataque dos achados da revisão viraram testes: o ruim não acontece mais.
Portas usadas: 8811-8815 (só 127.0.0.1; a 8816 aparece num teste, mas o servidor não chega a subir).
"""
import hashlib
import hmac
import http.client
import io
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sigma  # noqa: E402
from sigma import chain, crypto, evm, forum, netsafe, rpc  # noqa: E402
from sigma import node as nodemod  # noqa: E402
from sigma.chain import UNIT, TxError, canon, make_tx  # noqa: E402
from sigma.crypto import G, N, Wallet, _mul, keccak256, to_eth  # noqa: E402
from sigma.pow import share_ok  # noqa: E402

T0 = 1793700000.0
FAST = {"epoch_seconds": 60, "window_seconds": 10, "min_share_bits": 1, "max_shares": 32,
        "hosting_per_mb_window": 5, "ram_per_mb_window": 0, "demand_max_bps": 0}
HW = {"cpu_cores": 8, "ram_mb": 16000, "ai_ram_mb": 8000, "vram_mb": 0, "disk_mb": 50000, "gpu": ""}
PORT_HTTP, PORT_EVIL, PORT_TAIL, PORT_PEER, PORT_REAL = 8811, 8812, 8813, 8814, 8815


# ------------------------------------------------------------------ ajudantes
def find_nonces(seed, ident, bits, count, start=0):
    found, n = [], start
    while len(found) < count:
        if share_ok(seed, ident, n, bits):
            found.append(n)
        n += 1
    return found


def bad_nonce(seed, ident, bits, start=0):
    n = start
    while share_ok(seed, ident, n, bits):
        n += 1
    return n


def random_address():
    return "sig" + os.urandom(20).hex()


def machine_data(payout, name="pc", pub=None, endpoint="", disk_mb=50000):
    d = {"payout": payout, "name": name, "hw": dict(HW, disk_mb=disk_mb), "ai": True, "compute": True,
         "endpoint": endpoint, "prices": {"ai_per_1k_tokens": 2000, "compute_per_unit": 500}}
    if pub:
        d["pub"] = pub
    return d


def model_data(name, size=1_000_000, budget=10 * UNIT):
    return {"name": name, "url": f"https://huggingface.co/org/{name}/resolve/main/{name}.gguf",
            "sha256": hashlib.sha256(name.encode()).hexdigest(), "size": size, "royalty_bps": 1000,
            "reinvest_bps": 0, "replicas": 1, "license": "Apache-2.0", "description": "teste", "budget": budget}


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


def evm_raw(priv, nonce, to, value=0, data=b"", chain_id=51515):
    """Transação legacy EIP-155 assinada, no formato que eth_sendRawTransaction recebe."""
    fields = [nonce, 0, 21000, bytes.fromhex(to[2:]), value, data]
    r, s, recid = eth_sign(priv, keccak256(evm.rlp_encode(fields + [chain_id, 0, 0])))
    return "0x" + evm.rlp_encode(fields + [chain_id * 2 + 35 + recid, r, s]).hex()


def system_raw(wallet, nonce, call: bytes):
    """Operação do Sigma assinada pela MetaMask: JSON no campo data, para o SYSTEM_ADDRESS."""
    return evm_raw(wallet.priv, nonce, evm.SYSTEM_ADDRESS, data=call)


def personal_sign(priv, message):
    data = message.encode()
    z = keccak256(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)
    r, s, recid = eth_sign(priv, z)
    return "0x" + r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex() + bytes([27 + recid]).hex()


def req(port, method, path, body=None, headers=None, timeout=60):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conn.close()


def serve(test, handler, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        srv.shutdown()
        srv.server_close()
    test.addCleanup(stop)
    return srv


class Net:
    """Nó produtor de verdade numa pasta temporária, com relógio falso: o teste avança o tempo e fecha os
    blocos com produce_block (o mesmo caminho do loop do nó)."""

    def __init__(self, test, params=None, **kw):
        self.dir = tempfile.mkdtemp(prefix="sgn")
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.producer = Wallet.generate()
        self.genesis = {"network": "sigma", "chain_id": 51515, "time": T0, "validators": [self.producer.pub],
                        "alloc": {}, "params": dict(FAST, **(params or {}))}
        self.t = T0 + 1
        self.kw = kw
        self.node = self._open()
        test.addCleanup(self.close)

    def _open(self):
        n = nodemod.Node(self.dir, self.genesis, self.producer, **self.kw)
        n.clock = lambda: self.t
        return n

    def reopen(self):
        """Reinicia o nó na mesma pasta (como o pm2 faria depois de uma queda)."""
        self.close()
        self.node = self._open()
        return self.node

    def close(self):
        try:
            self.node.forum.db.close()
        except Exception:
            pass

    def send(self, wallet, type_, data, ip=None):
        nonce = self.node.pending.nonces.get(wallet.address, 0)
        return self.node.submit_tx(make_tx(wallet, type_, data, nonce), ip=ip)

    def block(self, dt=1.0):
        self.t += dt
        return self.node.produce_block()

    @property
    def window(self):
        return self.node.state.mining["window"]

    def work(self, miner, count=2, bits=1, worker=None, start=0):
        st = self.node.pending
        w = st.mining["window"]
        ident = miner if worker is None else f"{miner}.{worker}"
        data = {"miner": miner, "window": w, "bits": bits,
                "nonces": find_nonces(st.mining["seeds"][str(w)], ident, bits, count, start)}
        if worker is not None:
            data["worker"] = worker
        return {"type": "work", "data": data}

    def bad_work(self, miner, i=0):
        """Relatório recusado (o share não atinge a dificuldade): custa um scrypt ao nó."""
        st = self.node.pending
        w = st.mining["window"]
        return {"type": "work", "data": {"miner": miner, "window": w, "bits": 1,
                                         "nonces": [bad_nonce(st.mining["seeds"][str(w)], miner, 1, 1000 * i)]}}

    def next_window(self):
        m, p = self.node.state.mining, self.node.state.params
        self.t = max(self.t + 0.01, m["window_start"] + p["window_seconds"] + 0.01)
        assert self.node.produce_block()

    def settle(self):
        m, p = self.node.state.mining, self.node.state.params
        self.t = max(self.t + 0.01, m["epoch_start"] + p["epoch_seconds"] + 0.01)
        assert self.node.produce_block()

    def fund(self, *wallets):
        """O único jeito de ter SIG: minerar. A janela do relatório vira a base duas viradas depois e, daí em
        diante, cada bloco paga a fatia do tempo que passou."""
        for w in wallets:
            self.node.submit_tx(self.work(w.address, count=4))
        assert self.block()
        self.next_window()
        self.next_window()
        assert self.block(2 * self.node.state.params["window_seconds"])
        for w in wallets:
            assert self.node.state.balances.get(w.address, 0) > 100 * UNIT


# ------------------------------------------------------------------ formato das transações (SPEC §6.1)
class TestNormalize(unittest.TestCase):
    def test_unknown_top_level_key(self):
        with self.assertRaises(TxError) as cm:
            nodemod.normalize_tx({"type": "transfer", "data": {}, "nonce": 0, "pub": "", "sig": "", "extra": 1})
        self.assertEqual(str(cm.exception), "campos desconhecidos na transação: extra")
        with self.assertRaises(TxError) as cm:
            nodemod.normalize_tx({"type": "work", "data": {}, "nonce": 0})
        self.assertEqual(str(cm.exception), "campos desconhecidos na transação: nonce")

    def test_evm_wrapper_is_exactly_raw(self):
        # vetor (b) do achado: campo fora do id ia parar no bloco e travava o block_hash
        with self.assertRaises(TxError) as cm:
            nodemod.normalize_tx({"type": "evm", "data": {"raw": "0x00", "x": "a"}})
        self.assertEqual(str(cm.exception), "campos desconhecidos em evm: x")
        self.assertEqual(nodemod.normalize_tx({"type": "evm", "data": {"raw": "0xab"}, "id": "lixo"}),
                         {"type": "evm", "data": {"raw": "0xab"}})

    def test_text_and_numbers_canon_cannot_write(self):
        for data in ({"name": "\ud800"}, {"amount": 10 ** 4300}, {"x": float("nan")}):
            with self.assertRaises(TxError) as cm:
                nodemod.normalize_tx({"type": "transfer", "data": data, "nonce": 0, "pub": "a", "sig": "b"})
            self.assertEqual(str(cm.exception), "transação malformada: texto inválido ou número grande demais")

    def test_work_keeps_only_contract_fields(self):
        d = {"miner": "sig" + "1" * 40, "window": 1, "bits": 4, "nonces": [1], "worker": "ab"}
        self.assertEqual(nodemod.normalize_tx({"type": "work", "data": dict(d), "id": "x"}),
                         {"type": "work", "data": d})
        with self.assertRaises(TxError) as cm:
            nodemod.normalize_tx({"type": "work", "data": dict(d, enfeite=1)})
        self.assertEqual(str(cm.exception), "campos desconhecidos em work: enfeite")

    def test_signed_drops_client_id(self):
        tx = make_tx(Wallet.generate(), "ping", {}, 0)
        out = nodemod.normalize_tx(dict(tx, id="qualquer"))
        self.assertNotIn("id", out)
        self.assertEqual(chain.tx_id(out), tx["id"])

    def test_ip_key_groups_ipv6_by_64(self):
        self.assertEqual(nodemod.ip_key("2001:db8:1:2::1"), nodemod.ip_key("2001:db8:1:2:ffff::9"))
        self.assertNotEqual(nodemod.ip_key("2001:db8:1:2::1"), nodemod.ip_key("2001:db8:1:3::1"))
        self.assertEqual(nodemod.ip_key("::ffff:200.1.2.3"), "200.1.2.3")
        self.assertEqual(nodemod.ip_key("200.1.2.3"), "200.1.2.3")

    def test_oversized_signed_data_is_refused(self):
        """Achados disk-bloat/sig-cache: um corpo assinado gigante não vai para o bloco nem para o cache."""
        self.assertEqual(nodemod.MAX_SIGNED_DATA, 32 * 1024)
        big = {"type": "register_machine", "nonce": 0, "pub": "ab" * 64, "sig": "cd" * 64,
               "data": {"payout": "sig" + "1" * 40, "name": "A" * (40 * 1024)}}   # nome gigante: griefing de disco
        with self.assertRaises(TxError) as cm:
            nodemod.normalize_tx(big)
        self.assertEqual(str(cm.exception), "transação grande demais")
        small = dict(big, data={"payout": "sig" + "1" * 40, "name": "pc"})         # o normal passa
        self.assertEqual(nodemod.normalize_tx(small)["type"], "register_machine")


class TestEvmSystemCall(unittest.TestCase):
    MSG = "data deve ser um JSON {type, data} em UTF-8 válido e com números dentro do limite"

    def test_canon_check(self):
        self.assertEqual(evm.system_call(b'{"type":"ping","data":{}}'), ("ping", {}))
        for data in (b'{"type":"register_machine","data":{"name":"\\ud800"}}',
                     b'{"type":"x","data":{"a":' + b"9" * 5000 + b"}}",
                     b'{"type":"x","data":{"a":NaN}}',
                     b'{"type":"x","data":{},"z":"\\udc00"}',
                     b"[" * 100_000):
            with self.assertRaises(ValueError) as cm:
                evm.system_call(data)
            self.assertEqual(str(cm.exception), self.MSG)
        with self.assertRaises(ValueError):
            evm.system_call(b"\xff")

    def test_rlp_depth_is_bounded(self):
        with self.assertRaises(ValueError) as cm:
            evm.rlp_decode(bytes.fromhex("c1" * 5000 + "80"))   # sem recursão de 5.000 níveis
        self.assertEqual(str(cm.exception), "listas aninhadas demais")
        deep = [b"x"]
        for _ in range(evm.MAX_RLP_DEPTH - 1):
            deep = [deep]
        self.assertEqual(evm.rlp_decode(evm.rlp_encode(deep)), deep)
        with self.assertRaises(ValueError):
            evm.rlp_decode(evm.rlp_encode([deep]))


# ------------------------------------------------------------------ produção nunca para
class TestProduceNeverHalts(unittest.TestCase):
    def test_lone_surrogate_from_metamask_is_refused_and_blocks_go_on(self):
        """Cenário do achado: uma tx da MetaMask com nome "\\ud800", de uma chave sem SIG, travava o produtor."""
        net = Net(self)
        n = net.node
        k = Wallet.generate()
        call = b'{"type":"register_machine","data":{"payout":"' + k.address.encode() + b'","name":"\\ud800"}}'
        out = rpc.dispatch(n, {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
                               "params": [system_raw(k, 0, call)]})
        self.assertEqual(out["error"]["code"], -32000)
        self.assertEqual(out["error"]["message"], TestEvmSystemCall.MSG)
        self.assertEqual(n.mempool, [])
        h = n.height
        for _ in range(3):
            self.assertTrue(net.block())
        self.assertEqual(n.height, h + 3)

    def test_huge_integer_refused_at_admission(self):
        net = Net(self)
        owner = Wallet.generate()
        page = {"index.html": {"hash": "ab" * 32, "size": 10 ** 4299}, "b.html": {"hash": "cd" * 32,
                                                                              "size": 10 ** 4299}}
        with self.assertRaises(TxError):   # vetor (c): a soma teria 4.301 dígitos e o state_root falharia
            net.send(owner, "deploy_site", {"name": "site-x", "manifest": page, "budget": 10_000})
        huge = {"type": "transfer", "data": {"to": owner.address, "amount": 10 ** 4300}, "nonce": 0,
                "pub": owner.pub, "sig": "00" * 64}          # nem dá para assinar: o canon() não grava
        with self.assertRaises(TxError) as cm:
            net.node.submit_tx(huge)
        self.assertEqual(str(cm.exception), "transação malformada: texto inválido ou número grande demais")
        self.assertEqual(net.node.mempool, [])

    def test_state_root_error_drops_only_that_batch(self):
        net = Net(self)
        n = net.node
        real_root = chain.State.root

        def root(st):
            if any(m.get("name") == "bomba" for m in st.machines.values()):
                raise ValueError("Exceeds the limit (4300 digits) for integer string conversion")
            return real_root(st)

        log = io.StringIO()
        with mock.patch.object(chain.State, "root", root), mock.patch("sys.stderr", log):
            bomb = net.send(Wallet.generate(), "register_machine", machine_data(random_address(), name="bomba"))
            h = n.height
            self.assertFalse(net.block())                     # o lote sai, nada é gravado
            self.assertIn("[produtor] bloco 1 descartado (1 transações fora da fila)", log.getvalue())
            self.assertEqual(n.height, h)
            st = n.tx_status(bomb)
            self.assertEqual(st["status"], "dropped")
            self.assertIn("erro interno ao fechar o bloco", st["error"])
            self.assertEqual(n.mempool, [])
            ok = net.send(Wallet.generate(), "register_machine", machine_data(random_address(), name="ok"))
            self.assertTrue(net.block())                      # e o produtor segue com o próximo lote
            self.assertTrue(net.block())
        self.assertEqual(n.height, h + 2)
        self.assertEqual(n.tx_status(ok)["status"], "included")
        self.assertNotIn("bomba", [m["name"] for m in n.state.machines.values()])
        self.assertEqual(n.state.root(), n.head["state_root"])

    def test_make_block_error_drops_only_that_batch(self):
        net = Net(self)
        n = net.node
        bomb = net.send(Wallet.generate(), "register_machine", machine_data(random_address()))
        real = nodemod.make_block

        def make_block(wallet, height, prev, txs, root, t):
            if any(tx["id"] == bomb for tx in txs):
                raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
            return real(wallet, height, prev, txs, root, t)

        with mock.patch.object(nodemod, "make_block", make_block), mock.patch("sys.stderr", io.StringIO()):
            h = n.height
            self.assertFalse(net.block())
            self.assertEqual(n.tx_status(bomb)["status"], "dropped")
            ok = net.send(Wallet.generate(), "register_machine", machine_data(random_address()))
            self.assertTrue(net.block())
        self.assertEqual(n.height, h + 1)
        self.assertEqual(n.tx_status(ok)["status"], "included")

    def test_unexpected_error_in_one_tx_leaves_the_rest_of_the_block(self):
        net = Net(self)
        n = net.node
        bomb = net.send(Wallet.generate(), "register_machine", machine_data(random_address(), name="bomba"))
        ok = net.send(Wallet.generate(), "register_machine", machine_data(random_address(), name="ok"))
        real = chain.State._tx_register_machine

        def reg(st, sender, d, txid):
            if d.get("name") == "bomba":
                raise RuntimeError("bug")
            return real(st, sender, d, txid)

        with mock.patch.object(chain.State, "_tx_register_machine", reg):
            self.assertTrue(net.block())
        self.assertEqual(n.tx_status(ok)["status"], "included")
        st = n.tx_status(bomb)
        self.assertEqual(st["status"], "dropped")
        self.assertIn("erro interno", st["error"])
        self.assertEqual(n.mempool, [])


# ------------------------------------------------------------------ admissão (SPEC §10)
class TestAdmission(unittest.TestCase):
    def test_signature_and_evm_decode_run_outside_the_lock(self):
        """A recuperação ECDSA roda fora do lock da produção; lá dentro o apply_tx pega do cache."""
        net = Net(self)
        n = net.node
        calls = []
        real_verify, real_recover = chain.verify, evm.recover_pub

        def verify(*a):
            calls.append(("verify", n.lock._is_owned()))
            return real_verify(*a)

        def recover(*a):
            calls.append(("recover", n.lock._is_owned()))
            return real_recover(*a)

        k = Wallet.generate()
        call = canon({"type": "register_machine", "data": machine_data(random_address(), name="mm")})
        raw = system_raw(k, 0, call)
        with mock.patch.object(chain, "verify", verify), mock.patch.object(evm, "recover_pub", recover):
            net.send(Wallet.generate(), "register_machine", machine_data(random_address()))
            txid = n.submit_tx({"type": "evm", "data": {"raw": raw}})
            self.assertTrue(net.block())
        self.assertEqual([c[0] for c in calls], ["verify", "recover"])   # uma vez cada: o resto veio do cache
        self.assertFalse(any(owned for _, owned in calls))
        self.assertEqual(txid, keccak256(bytes.fromhex(raw[2:])).hex())  # o hash que a MetaMask mostra
        self.assertEqual(n.tx_status(txid)["status"], "included")
        self.assertIn(k.address, n.state.machines)

    def test_resubmitting_is_a_no_op(self):
        net = Net(self)
        tx = make_tx(Wallet.generate(), "register_machine", machine_data(random_address()), 0)
        a = net.node.submit_tx(dict(tx))
        b = net.node.submit_tx(dict(tx))
        self.assertEqual(a, b)
        self.assertEqual(len(net.node.mempool), 1)

    def test_warm_only_verifies_known_signed_types(self):
        """Achado sig-cache-guarda-lixo: o _warm só confere (e cacheia) a assinatura de um tipo assinado
        conhecido com pub/sig HEX128; lixo recusado nunca entra no cache do _sig_ok."""
        net = Net(self)
        n = net.node
        calls = []
        with mock.patch.object(chain, "_sig_ok", lambda *a: calls.append(a) or True):
            n._warm(nodemod.normalize_tx(make_tx(Wallet.generate(), "ping", {}, 0)))
            self.assertEqual(len(calls), 1)                         # tipo válido, pub/sig HEX128: confere
            n._warm({"type": "transfer", "data": {"to": random_address(), "amount": 1}, "nonce": 0,
                     "pub": "não-é-hex", "sig": "zz"})             # pub/sig fora do formato: não chama
            n._warm({"type": "banana", "data": {}, "nonce": 0, "pub": "ab" * 64, "sig": "cd" * 64})  # tipo fora
            self.assertEqual(len(calls), 1)


class TestTargetedJob(unittest.TestCase):
    def test_targeted_ai_job_needs_the_request_blob_first(self):
        """SPEC §1.4: a máquina escolhida nunca pode alegar que o pedido sumiu."""
        net = Net(self)
        n = net.node
        client, sponsor, mw, payout = (Wallet.generate() for _ in range(4))
        net.fund(client, sponsor)
        net.send(mw, "register_machine", machine_data(payout.address, pub=mw.pub))
        net.send(sponsor, "register_model", model_data("modelo-um"))
        self.assertTrue(net.block())
        net.next_window()                                    # a rede atribui o modelo à máquina
        self.assertIn(mw.address, n.state.models["modelo-um"]["assigned"])
        net.send(mw, "pin_model", {"name": "modelo-um"})
        net.send(mw, "ping", {})
        self.assertTrue(net.block())
        self.assertIn("modelo-um", n.state.machines[mw.address]["models"])

        ask = canon({"messages": [{"role": "user", "content": "oi"}], "max_tokens": 64, "json": False})
        spec = crypto.seal(mw.pub, ask)
        h = hashlib.sha256(spec).hexdigest()
        job = {"kind": "ai", "model": "modelo-um", "spec_hash": h, "escrow": 10_000, "max_price": 2000,
               "max_tokens": 64, "machine": mw.address, "client_pub": client.pub}
        with self.assertRaises(TxError) as cm:
            net.send(client, "create_job", job)
        self.assertEqual(str(cm.exception), "envie o pedido (/blob) antes de criar o job")
        raw = system_raw(client, n.pending.nonces.get(client.address, 0), canon({"type": "create_job", "data": job}))
        out = rpc.dispatch(n, {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw]})
        self.assertEqual(out["error"]["message"], "envie o pedido (/blob) antes de criar o job")
        self.assertEqual(n.mempool, [])

        self.assertEqual(n.put_blob(spec), h)
        jid = net.send(client, "create_job", job)
        self.assertTrue(net.block())
        j = n.state.jobs[jid]
        self.assertEqual((j["machine"], j["client_pub"], j["window"]), (mw.address, client.pub, net.window))
        self.assertIn(h, nodemod.blob_refs(n.state))         # a limpeza guarda o pedido enquanto o job existir
        info = nodemod.machine_info(n.state, mw.address)
        self.assertEqual((info["pub"], info["jobs_failed"]), (mw.pub, 0))
        self.assertEqual(info["assignments"][0]["status"], "pinned")

        public = {"kind": "ai", "model": "modelo-um", "spec_hash": "cd" * 32, "escrow": 10_000, "max_price": 2000,
                  "max_tokens": 64}
        net.send(client, "create_job", public)               # job público (cliente antigo): como na v2


# ------------------------------------------------------------------ JSON-RPC
class TestRpc(unittest.TestCase):
    def test_batch_limit(self):
        net = Net(self)
        n = net.node
        one = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId"}
        out = rpc.dispatch(n, [one] * 50)
        self.assertEqual(len(out), 50)
        self.assertEqual(out[0]["result"], hex(51515))
        out = rpc.dispatch(n, [one] * 51)
        self.assertEqual(out, {"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32600, "message": "lote grande demais (máximo 50)"}})
        self.assertEqual(rpc.dispatch(n, [])["error"]["code"], -32600)
        nested = rpc.dispatch(n, [[one] * 50] * 50)          # lote dentro de lote não multiplica o limite
        self.assertEqual(len(nested), 50)
        self.assertTrue(all(r["error"]["code"] == -32600 for r in nested))

    def test_big_batch_of_raw_txs_does_no_signature_work(self):
        """Cenário do achado: um POST com milhares de eth_sendRawTransaction congelava o produtor."""
        net = Net(self)
        k = Wallet.generate()
        batch = [{"jsonrpc": "2.0", "id": i, "method": "eth_sendRawTransaction",
                  "params": [evm_raw(k.priv, i, to_eth(random_address()), value=evm.WEI_PER_USIG)]}
                 for i in range(51)]
        with mock.patch.object(evm, "recover_pub", side_effect=AssertionError("não devia decodificar")):
            out = rpc.dispatch(net.node, batch)
        self.assertEqual(out["error"]["code"], -32600)

    def test_raw_tx_counts_in_the_connection_limit(self):
        net = Net(self, public=True, tx_per_ip=2)
        n = net.node
        k = Wallet.generate()

        def send(i, ip):
            return rpc.dispatch(n, {"jsonrpc": "2.0", "id": i, "method": "eth_sendRawTransaction",
                                    "params": [evm_raw(k.priv, i, to_eth(random_address()), value=1)]}, ip=ip)
        self.assertEqual(send(0, "200.0.0.1")["error"]["code"], -32000)   # recusada (sem saldo), mas conta
        self.assertEqual(send(1, "200.0.0.1")["error"]["code"], -32000)
        out = send(2, "200.0.0.1")
        self.assertEqual(out["error"]["code"], rpc.LIMIT_CODE)
        self.assertIn("limite de 2 transações por conexão de internet nesta janela", out["error"]["message"])
        self.assertEqual(send(3, "200.0.0.2")["error"]["code"], -32000)   # outra conexão: tem a própria cota

    def test_bad_params(self):
        net = Net(self)
        for method, params in (("eth_sendRawTransaction", []), ("eth_sendRawTransaction", [5]),
                               ("eth_getTransactionByHash", [None]), ("eth_getBlockByNumber", [])):
            out = rpc.dispatch(net.node, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            self.assertEqual(out["error"]["code"], -32602, method)

    def test_feehistory_caps_the_response(self):
        """Achado rpc-feehistory-amplificacao: o 3º parâmetro não pode amplificar a resposta do nó."""
        net = Net(self)
        n = net.node

        def fee(*params):
            return rpc.dispatch(n, {"jsonrpc": "2.0", "id": 1, "method": "eth_feeHistory", "params": list(params)})
        # uma string de 250 KB em rewardPercentiles montava uma resposta de centenas de MB: agora -32602
        self.assertEqual(fee("0x64", "latest", "A" * 250_000)["error"]["code"], -32602)
        self.assertEqual(fee("0x5", "latest", list(range(101)))["error"]["code"], -32602)   # > 100 números
        self.assertEqual(fee("0x5", "latest", [{}])["error"]["code"], -32602)               # não é número
        out = fee("0x5", "latest", [10, 50, 90])["result"]                                  # uso normal da MetaMask
        self.assertEqual((len(out["reward"]), len(out["reward"][0]), len(out["baseFeePerGas"])), (5, 3, 6))
        out = fee("0xffffffff", "latest", [])["result"]                                     # n enorme: preso em 1024
        self.assertEqual((len(out["gasUsedRatio"]), len(out["reward"])), (1024, 1024))


# ------------------------------------------------------------------ limites por conexão (--public)
class TestLimits(unittest.TestCase):
    def test_signed_tx_limit_per_connection_and_window(self):
        net = Net(self, public=True, tx_per_ip=3)
        n = net.node
        ip = "200.1.1.1"
        bad = make_tx(Wallet.generate(), "transfer", {"to": random_address(), "amount": 5}, 0)
        with self.assertRaises(TxError):                      # recusada também gasta a cota
            n.submit_tx(bad, ip=ip)
        for _ in range(2):
            net.send(Wallet.generate(), "register_machine", machine_data(random_address()), ip=ip)
        with self.assertRaises(nodemod.RateLimited) as cm:
            net.send(Wallet.generate(), "register_machine", machine_data(random_address()), ip=ip)
        e = cm.exception
        self.assertEqual(e.status, 429)
        self.assertRegex(str(e), r"^limite de 3 transações por conexão de internet nesta janela; tente de novo "
                                 r"daqui a ~\d+ s$")
        self.assertGreaterEqual(e.retry_after, 1)
        net.send(Wallet.generate(), "register_machine", machine_data(random_address()), ip="200.1.1.2")
        net.send(Wallet.generate(), "register_machine", machine_data(random_address()), ip="2001:db8:5:6::1")
        with self.assertRaises(nodemod.RateLimited):          # o mesmo /64 é a mesma casa
            for _ in range(3):
                net.send(Wallet.generate(), "register_machine", machine_data(random_address()),
                         ip="2001:db8:5:6::ffff")
        net.next_window()                                     # janela nova: cota nova
        net.send(Wallet.generate(), "register_machine", machine_data(random_address()), ip=ip)

    def test_local_node_has_no_limits(self):
        net = Net(self, tx_per_ip=1, work_per_ip=1)
        for _ in range(3):
            net.send(Wallet.generate(), "register_machine", machine_data(random_address()), ip="200.1.1.1")
            net.node.submit_tx(net.work(random_address()), ip="200.1.1.1")
        self.assertEqual(len(net.node.mempool), 6)

    def test_cgnat_hundreds_of_homes_behind_one_ip(self):
        """Cenário do achado: na v2 o 5º relatório válido de um IP levava 429 e a casa perdia a janela."""
        net = Net(self, public=True)
        n = net.node
        ip = "177.10.0.1"
        for _ in range(nodemod.WORK_PER_IP):
            n.submit_tx(net.work(random_address(), count=1), ip=ip)
        self.assertEqual(len(n.mempool), 256)
        with self.assertRaises(nodemod.RateLimited) as cm:
            n.submit_tx(net.work(random_address(), count=1), ip=ip)
        self.assertIn("limite de 256 relatórios de mineração por conexão de internet nesta janela; o próximo vai "
                      "na janela seguinte", str(cm.exception))
        self.assertTrue(net.block())
        self.assertEqual(sum(len(w) for w in n.state.mining["open_points"].values()), 256)

    def test_several_pcs_one_wallet(self):
        net = Net(self, public=True)
        n = net.node
        wallet = random_address()
        for worker in ("aa", "bb", "cc"):
            n.submit_tx(net.work(wallet, worker=worker), ip="177.10.0.1")
        self.assertTrue(net.block())
        self.assertEqual(sum(w.get(wallet, 0) for w in n.state.mining["open_points"].values()), 3 * (2 << 1))

    def test_only_refused_reports_spend_the_low_budget(self):
        net = Net(self, public=True, work_per_ip=50, bad_work_per_ip=2)
        n = net.node
        ip = "200.9.9.9"
        miner = random_address()
        for i in range(2):
            with self.assertRaises(TxError) as cm:
                n.submit_tx(net.bad_work(miner, i), ip=ip)
            self.assertNotIsInstance(cm.exception, nodemod.RateLimited)
            self.assertIn("share inválido", str(cm.exception))
        with self.assertRaises(nodemod.RateLimited) as cm:
            n.submit_tx(net.bad_work(miner, 2), ip=ip)
        self.assertIn("relatório recusado (share inválido", str(cm.exception))
        self.assertIn("já passou de 2 relatórios de mineração recusados nesta janela", str(cm.exception))
        # quem manda lixo não barra o válido: nem o dele, nem o dos vizinhos do mesmo IP
        for _ in range(5):
            n.submit_tx(net.work(random_address()), ip=ip)
        n.submit_tx(net.work(miner), ip=ip)
        self.assertEqual(len(n.mempool), 6)
        self.assertEqual(n.quotas.get("work_ok", net.window, ip), 6)

    def test_duplicate_report_costs_nothing(self):
        net = Net(self, public=True, work_per_ip=1)
        rep = net.work(random_address())
        net.node.submit_tx(dict(rep), ip="200.1.1.1")
        net.node.submit_tx(dict(rep), ip="200.1.1.1")         # reenvio: já está na fila, não conta de novo
        self.assertEqual(net.node.quotas.get("work_ok", net.window, "200.1.1.1"), 1)

    def test_messages_agree_in_number(self):
        net = Net(self, public=True, tx_per_ip=1, work_per_ip=1, bad_work_per_ip=1)
        n = net.node
        net.send(Wallet.generate(), "register_machine", machine_data(random_address()), ip="200.3.3.3")
        with self.assertRaises(nodemod.RateLimited) as cm:
            net.send(Wallet.generate(), "register_machine", machine_data(random_address()), ip="200.3.3.3")
        self.assertIn("limite de 1 transação por conexão de internet nesta janela", str(cm.exception))
        n.submit_tx(net.work(random_address()), ip="200.3.3.3")
        with self.assertRaises(nodemod.RateLimited) as cm:
            n.submit_tx(net.work(random_address()), ip="200.3.3.3")
        self.assertIn("limite de 1 relatório de mineração por conexão de internet nesta janela", str(cm.exception))
        miner = random_address()
        with self.assertRaises(TxError):
            n.submit_tx(net.bad_work(miner, 0), ip="200.3.3.4")
        with self.assertRaises(nodemod.RateLimited) as cm:
            n.submit_tx(net.bad_work(miner, 1), ip="200.3.3.4")
        self.assertIn("já passou de 1 relatório de mineração recusado nesta janela", str(cm.exception))

    def test_full_mempool_is_not_the_miners_fault(self):
        net = Net(self, public=True, bad_work_per_ip=0)
        with mock.patch.object(nodemod, "MAX_MEMPOOL", 0):
            with self.assertRaises(nodemod.MempoolFull) as cm:
                net.node.submit_tx(net.work(random_address()), ip="200.1.1.1")
        self.assertNotIsInstance(cm.exception, nodemod.RateLimited)   # nem 429, nem conta como recusado
        self.assertEqual(net.node.quotas.get("work_bad", net.window, "200.1.1.1"), 0)
        self.assertEqual(net.node.quotas.get("work_ok", net.window, "200.1.1.1"), 0)

    def test_blob_quota_per_connection_and_hour(self):
        net = Net(self, public=True, blob_mb_per_ip_hour=1)
        n = net.node
        a = os.urandom(700 * 1024)
        n.put_blob(a, ip="200.5.5.5")
        with self.assertRaises(nodemod.RateLimited) as cm:
            n.put_blob(os.urandom(700 * 1024), ip="200.5.5.5")
        self.assertRegex(str(cm.exception), r"^limite de envio de arquivos desta conexão de internet: 1 MB por "
                                            r"hora; tente de novo daqui a ~\d+ (s|min)$")
        n.put_blob(a, ip="200.5.5.5")                         # o que já existe não gasta cota
        n.put_blob(os.urandom(700 * 1024), ip="200.5.5.6")
        with self.assertRaises(nodemod.RateLimited):
            n.check_blob_quota("200.5.5.5", 700 * 1024)
        net.t += 3600                                         # hora nova
        n.put_blob(os.urandom(700 * 1024), ip="200.5.5.5")

    def test_full_store_still_takes_small_blobs(self):
        net = Net(self, blob_cap=1_000_000)
        n = net.node
        n.put_blob(os.urandom(900_000))
        with self.assertRaises(TxError) as cm:
            n.put_blob(os.urandom(300 * 1024))
        self.assertIn("o armazenamento deste nó está cheio", str(cm.exception))
        n.put_blob(os.urandom(200 * 1024))                    # pedido de IA: o chat não para
        self.assertGreater(n.blob_bytes, n.blob_cap)
        n.blob_bytes = n.blob_cap + nodemod.BLOB_OVERFLOW     # até o limite + 1 GB
        with self.assertRaises(TxError):
            n.put_blob(os.urandom(1024))

    def test_signed_tx_quota_cost_scales_with_canon_size(self):
        """SPEC §4: uma tx assinada custa 1 + len(canon(data))//1024 na cota --tx-per-ip (como já é com EVM)."""
        net = Net(self, public=True, tx_per_ip=1000)
        n = net.node
        ip = "200.8.8.8"
        n.submit_tx(make_tx(Wallet.generate(), "register_machine", machine_data(random_address()), 0), ip=ip)
        self.assertEqual(n.quotas.get("tx", net.window, ip), 1)       # tx pequena: 1 unidade
        before = n.quotas.get("tx", net.window, ip)
        data = machine_data(random_address(), name="n" * 4096)       # ~4 KB de canon(data)
        expected = 1 + len(canon(data)) // 1024
        self.assertGreaterEqual(expected, 5)
        try:
            n.submit_tx(make_tx(Wallet.generate(), "register_machine", data, 0), ip=ip)
        except TxError:
            pass    # a chain pode recusar o nome grande; a cota já contou pelo tamanho, antes do apply
        self.assertEqual(n.quotas.get("tx", net.window, ip) - before, expected)

    def test_targeted_claim_submit_do_not_burn_the_host_quota(self):
        """Achado targeted-flood: a máquina é obrigada a reivindicar/responder jobs direcionados de terceiros;
        essas txs não gastam a cota --tx-per-ip dela (senão o atacante a tira do ar de graça). SPEC §1."""
        net = Net(self, public=True, tx_per_ip=5)
        n = net.node
        mw = Wallet.generate()
        ip = "200.7.7.7"
        n.state.jobs["t1"] = {"machine": mw.address, "status": "open", "claims": {}, "kind": "ai"}   # direcionado
        n.state.jobs["pub"] = {"status": "open", "claims": {}, "kind": "compute"}                     # público

        def send(typ, data):
            with self.assertRaises(TxError):     # o apply recusa (job só no estado, máquina sem registro); ok
                n.submit_tx(make_tx(mw, typ, data, 0), ip=ip)
        send("claim_job", {"job_id": "t1"})
        send("submit_result", {"job_id": "t1", "result_hash": "ab" * 32, "usage": {}, "failed": True})
        self.assertEqual(n.quotas.get("tx", net.window, ip), 0)       # os dois direcionados não contaram
        send("claim_job", {"job_id": "pub"})                         # job público (sem machine): conta
        self.assertEqual(n.quotas.get("tx", net.window, ip), 1)
        send("transfer", {"to": random_address(), "amount": 1})      # qualquer outra tx assinada: conta
        self.assertEqual(n.quotas.get("tx", net.window, ip), 2)


# ------------------------------------------------------------------ memória: cauda de blocos + disco
class TestMemoryTail(unittest.TestCase):
    def test_long_chain_keeps_only_the_tail_and_serves_old_blocks_from_disk(self):
        net = Net(self)
        n = net.node
        a, b = Wallet.generate(), Wallet.generate()
        net.fund(a)
        common = net.send(a, "transfer", {"to": b.address, "amount": 1000})
        raw = evm_raw(a.priv, n.pending.nonces[a.address], to_eth(b.address), value=2000 * evm.WEI_PER_USIG)
        evm_id = n.submit_tx({"type": "evm", "data": {"raw": raw}})
        self.assertTrue(net.block(5))
        old_h = n.tx_status(evm_id)["height"]
        old_block = json.loads(json.dumps(n.block(old_h)))
        for _ in range(nodemod.MEM_BLOCKS + 120):            # > 2.100 blocos, 5 s cada (~3 h de chain)
            self.assertTrue(net.block(5))
        self.assertGreater(n.height, 2100)
        self.assertEqual(len(n.blocks), nodemod.MEM_BLOCKS)   # só a cauda fica na memória
        self.assertEqual(n.blocks[0]["height"], n.height - nodemod.MEM_BLOCKS + 1)
        self.assertIsNone(n._tail_get(old_h))
        self.assertEqual(n.block(old_h), old_block)           # o antigo vem do chain.jsonl
        self.assertEqual(n.block(0)["height"], 0)
        self.assertIsNone(n.block(n.height + 1))
        self.assertEqual(n.tx_status(evm_id), {"status": "included", "height": old_h})   # MetaMask: para sempre
        self.assertEqual(n.tx_status(common), {"status": "unknown"})                      # comum: só ~2 h
        self.assertLess(len(n.tx_pos), 100)
        history = json.dumps(n.history, sort_keys=True)
        self.assertEqual(len(n.history[b.address]), 2)

        serve(self, nodemod.make_handler(n), PORT_TAIL)
        code, _, body = req(PORT_TAIL, "GET", "/status")
        self.assertEqual((code, json.loads(body)["height"]), (200, n.height))
        code, _, body = req(PORT_TAIL, "GET", f"/blocks?from={old_h}")
        blocks = json.loads(body)["blocks"]
        self.assertEqual(blocks[0], old_block)
        self.assertEqual([x["height"] for x in blocks], list(range(old_h, old_h + len(blocks))))

        def call(method, *params):
            code, _, body = req(PORT_TAIL, "POST", "/rpc", {"jsonrpc": "2.0", "id": 1, "method": method,
                                                            "params": list(params)})
            self.assertEqual(code, 200)
            return json.loads(body)["result"]
        blk = call("eth_getBlockByNumber", hex(old_h), True)
        self.assertEqual(blk["hash"], "0x" + old_block["hash"])
        self.assertIn("0x" + evm_id, [t["hash"] for t in blk["transactions"]])
        self.assertEqual(call("eth_getTransactionByHash", "0x" + evm_id)["blockNumber"], hex(old_h))
        self.assertEqual(call("eth_getTransactionReceipt", "0x" + evm_id)["status"], "0x1")
        self.assertIsNone(call("eth_getBlockByHash", "0x" + old_block["hash"], False))   # só a cauda
        self.assertEqual(call("eth_getBlockByHash", "0x" + n.head["hash"], False)["number"], hex(n.height))

        # reinício: lê a chain linha a linha, usa o snapshot e volta com a mesma cauda, índices e histórico
        root, height = n.state.root(), n.height
        n2 = net.reopen()
        self.assertEqual((n2.height, n2.state.root()), (height, root))
        self.assertEqual(len(n2.blocks), nodemod.MEM_BLOCKS)
        self.assertEqual(n2.block(old_h), old_block)
        self.assertEqual(n2.tx_status(evm_id)["status"], "included")
        self.assertEqual(n2.tx_status(common)["status"], "unknown")
        self.assertEqual(json.dumps(n2.history, sort_keys=True), history)
        self.assertTrue(net.block(5))

    def test_history_is_capped(self):
        net = Net(self)
        n = net.node
        addr = random_address()
        for i in range(nodemod.HISTORY_MAX + 25):
            n._hist_add(addr, {"type": "transfer", "amount": i})
        self.assertEqual(len(n.history[addr]), nodemod.HISTORY_MAX)
        self.assertEqual(n.history[addr][0]["amount"], 25)   # as mais antigas saíram

    def test_torn_last_line_is_cut_on_restart(self):
        net = Net(self)
        for _ in range(3):
            self.assertTrue(net.block())
        with open(net.node.chain_file, "ab") as f:
            f.write(b'{"height": 4, "pre')                    # queda de energia no meio da gravação
        with mock.patch("sys.stderr", io.StringIO()):
            n = net.reopen()
        self.assertEqual(n.height, 3)
        self.assertTrue(net.block())
        self.assertEqual(net.reopen().height, 4)


# ------------------------------------------------------------------ créditos no histórico (SPEC §9)
class TestHistoryCredits(unittest.TestCase):
    def ping_window(self, net, mw):
        net.next_window()
        net.send(mw, "ping", {})
        self.assertTrue(net.block())

    @staticmethod
    def credits(n, addr, kind):
        return [e for e in n.history.get(addr, []) if e.get("type") == "credit" and e.get("kind") == kind]

    def test_one_entry_per_address_kind_epoch_rebuilt_the_same_after_restart(self):
        net = Net(self, params={"epoch_seconds": 120})       # o financiamento e as 3 janelas cabem numa época
        n = net.node
        owner, payout, mw = Wallet.generate(), Wallet.generate(), Wallet.generate()
        net.fund(owner)
        page = b"<h1>Sigma</h1>" * 100
        h = n.put_blob(page)
        net.send(mw, "register_machine", machine_data(payout.address))
        net.send(owner, "deploy_site", {"name": "meu-site", "manifest": {"index.html": {"hash": h, "size": len(page)}},
                                        "budget": 100_000, "replicas": 1})
        self.assertTrue(net.block())
        net.send(mw, "pin_site", {"name": "meu-site", "version": 1})
        self.assertTrue(net.block())
        epoch = n.state.mining["epoch"]
        for _ in range(3):                                    # aluguel a cada virada de janela
            self.ping_window(net, mw)
        self.assertEqual(n.state.mining["epoch"], epoch)
        rent = self.credits(n, payout.address, "rent")
        self.assertEqual(len(rent), 1)                        # uma entrada só, somando
        self.assertEqual({k: rent[0][k] for k in ("type", "kind", "epoch", "amount", "count", "name")},
                         {"type": "credit", "kind": "rent", "epoch": epoch, "amount": 3, "count": 3,
                          "name": "meu-site"})
        self.assertEqual(rent[0]["height"], n.height - 1)     # o bloco da virada (o último só tem o ping)
        while n.state.mining["epoch"] == epoch:               # época nova: entrada nova
            self.ping_window(net, mw)
        rent = self.credits(n, payout.address, "rent")
        self.assertEqual([e["epoch"] for e in rent], [epoch, epoch + 1])
        self.assertEqual(sum(e["amount"] for e in rent), n.state.sites["meu-site"]["paid"])

        job = net.send(owner, "create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 1000}},
                                             "replicas": 1, "escrow": 5000, "max_price": 500})
        self.assertTrue(net.block())
        net.send(owner, "cancel_job", {"job_id": job})
        self.assertTrue(net.block())
        refund = self.credits(n, owner.address, "refund")
        self.assertEqual([(e["amount"], e["count"]) for e in refund], [(5000, 1)])

        before = json.dumps(n.history, sort_keys=True)        # reinício sem snapshot: reexecuta tudo
        n = net.reopen()
        self.assertEqual(json.dumps(n.history, sort_keys=True), before)
        n._save_snapshot()                                    # com snapshot: histórico dele + blocos depois
        self.ping_window(net, mw)
        before = json.dumps(n.history, sort_keys=True)
        n = net.reopen()
        self.assertEqual(json.dumps(n.history, sort_keys=True), before)
        count = self.credits(n, payout.address, "rent")[-1]["count"]
        self.ping_window(net, mw)                             # o índice volta junto: soma, não duplica
        rent = self.credits(n, payout.address, "rent")
        self.assertEqual(len(rent), 2)
        self.assertEqual(rent[-1]["count"], count + 1)


# ------------------------------------------------------------------ limpeza dos blobs
class TestBlobGC(unittest.TestCase):
    def test_gc_keeps_what_the_state_uses_and_removes_old_orphans(self):
        net = Net(self)
        n = net.node
        blobs = {k: n.put_blob(f"blob {k} ".encode() * 50) for k in "ABCDEFGH"}
        old = time.time() - 2 * 3600
        for k in "ABCDEFG":
            os.utime(n.blob_path(blobs[k]), (old, old))
        st = n.state
        w = st.mining["window"]
        grace = math.ceil(nodemod.SITE_GRACE / st.params["window_seconds"])

        def site(status, k, **kw):
            return dict({"status": status, "manifest": {"a.html": {"hash": blobs[k], "size": 1}}, "pinners": {}},
                        **kw)
        st.sites.update({"ativo": site("active", "A"),
                         "recente": site("expired", "B", expired_window=w - grace + 1),   # < 7 dias
                         "velho": site("expired", "C", expired_window=w - grace)})        # 7 dias: sai
        st.jobs["j"] = {"status": "done", "spec_hash": blobs["D"], "result_hash": blobs["E"],
                        "results": {"m": {"hash": blobs["F"]}}}
        before = n.blob_bytes
        removed, freed = n.gc_blobs()
        gone = {k for k, h in blobs.items() if not os.path.exists(n.blob_path(h))}
        self.assertEqual(gone, {"C", "G"})                    # H é sem uso, mas tem menos de 1 h
        self.assertEqual((removed, n.blob_bytes), (2, before - freed))
        with os.scandir(n.blob_dir) as it:
            self.assertEqual(sum(e.stat().st_size for e in it), n.blob_bytes)

    def test_gc_runs_hourly_in_the_background(self):
        net = Net(self)
        n = net.node
        h = n.put_blob(b"orfao" * 100)
        old = time.time() - 2 * 3600
        os.utime(n.blob_path(h), (old, old))
        self.assertEqual(nodemod.BLOB_GC_EVERY, 3600)
        n._maybe_gc()                                         # recém-ligado: ainda não
        self.assertTrue(os.path.exists(n.blob_path(h)))
        n._last_gc = time.time() - nodemod.BLOB_GC_EVERY - 1
        with mock.patch("sys.stderr", io.StringIO()):
            n._maybe_gc()
            for _ in range(100):
                if not n._gc_busy:
                    break
                time.sleep(0.05)
        self.assertFalse(os.path.exists(n.blob_path(h)))


# ------------------------------------------------------------------ leituras de fora (máquinas e peers)
class Evil(BaseHTTPRequestHandler):
    """Máquina maliciosa: diz que manda o arquivo e manda 30 MB."""
    sent = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(30 * 1024 * 1024))
        self.end_headers()
        try:
            for _ in range(480):
                self.wfile.write(b"x" * 65536)
                Evil.sent.append(65536)
        except OSError:
            pass


class TestOutboundReads(unittest.TestCase):
    def setUp(self):
        Evil.sent = []

    @staticmethod
    def fake_site(n, endpoint, size=1000):
        page = b"y" * size
        h = hashlib.sha256(page).hexdigest()
        n.state.machines["sigmaquina"] = {"endpoint": endpoint}
        n.state.sites["s1"] = {"status": "active", "manifest": {"index.html": {"hash": h, "size": size}},
                               "pinners": {"sigmaquina": {"window": 0}}}
        return h

    def test_reads_from_machines_never_pass_the_declared_size(self):
        """Cenário do achado: a máquina de um estranho mandava GBs e o nó lia tudo na memória."""
        serve(self, Evil, PORT_EVIL)
        net = Net(self)
        n = net.node
        h = self.fake_site(n, f"http://127.0.0.1:{PORT_EVIL}")
        limits, real = [], netsafe.read_capped

        def capped(r, limit):
            limits.append(limit)
            return real(r, limit)
        with mock.patch.object(netsafe, "read_capped", capped):
            self.assertEqual(n.fetch_site_file("s1", ""), (None, None, "nenhuma máquina respondeu"))
            self.assertIsNone(n.get_blob_any(h))
        self.assertEqual(limits, [1000, 1000])
        time.sleep(0.3)
        self.assertLess(sum(Evil.sent), 30 * 1024 * 1024)    # o nó cortou antes do fim

    def test_peer_json_is_capped(self):
        class Big(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"blocks": ["x" * 1024] * 2048}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        serve(self, Big, PORT_PEER)
        with self.assertRaises(ValueError) as cm:
            nodemod.http_json(f"http://127.0.0.1:{PORT_PEER}/blocks", limit=1024 * 1024)
        self.assertEqual(str(cm.exception), "resposta grande demais")
        self.assertEqual(len(nodemod.http_json(f"http://127.0.0.1:{PORT_PEER}/blocks")["blocks"]), 2048)

    def test_public_node_skips_endpoints_inside_the_server_network(self):
        serve(self, Evil, PORT_EVIL)
        eps = [("a", f"http://127.0.0.1:{PORT_EVIL}"), ("b", "http://localhost:9"), ("c", "http://10.0.0.5"),
               ("d", "http://169.254.169.254"), ("e", "http://100.64.0.1"), ("f", "http://8.8.8.8:80")]
        pub = Net(self, public=True).node
        self.assertEqual(pub._reachable(eps), [("f", "http://8.8.8.8:80")])
        self.assertEqual(Net(self).node._reachable(eps), eps)    # rede de teste local: pode
        self.fake_site(pub, f"http://127.0.0.1:{PORT_EVIL}")
        self.assertEqual(pub.fetch_site_file("s1", "")[2], "nenhuma máquina respondeu")
        self.assertEqual(Evil.sent, [])                          # nem conectou


# ------------------------------------------------------------------ fórum
class TestForum(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="sgf")
        self.addCleanup(shutil.rmtree, d, True)
        self.st = types.SimpleNamespace(machines={}, models={}, sites={}, balances={})
        self.f = forum.Forum(os.path.join(d, "forum.db"), lambda: self.st)
        self.addCleanup(self.f.db.close)
        self.t = 1_800_000_000.0
        self.f.clock = lambda: self.t

    @staticmethod
    def addr(i):
        return f"sig{i:040x}"

    def test_expired_challenges_really_leave(self):
        self.f.challenge(self.addr(1))
        self.f.challenge(self.addr(2))
        self.t += forum.CHALLENGE_TTL + 1
        self.f.challenge(self.addr(3))
        self.assertEqual(list(self.f.challenges), [self.addr(3)])

    def test_flood_of_addresses_stays_capped(self):
        """Cenário do achado: endereços distintos sem parar enchiam a memória do nó."""
        for i in range(forum.MAX_CHALLENGES + 50):
            self.f.challenge(self.addr(i))
        self.assertEqual(len(self.f.challenges), forum.MAX_CHALLENGES)
        self.assertNotIn(self.addr(49), self.f.challenges)    # os mais antigos saíram
        self.assertIn(self.addr(50), self.f.challenges)
        self.f.challenge(self.addr(50))                       # pedir de novo vai para o fim
        self.assertEqual(list(self.f.challenges)[-1], self.addr(50))

    def test_login_works_and_expires(self):
        w = Wallet.generate()
        token, _ = self.f.login(w.address, personal_sign(w.priv, self.f.challenge(w.address)))
        self.assertEqual(self.f.who(token), w.address)
        msg = self.f.challenge(w.address)
        self.t += forum.CHALLENGE_TTL + 1
        with self.assertRaises(forum.ForumError) as cm:
            self.f.login(w.address, personal_sign(w.priv, msg))
        self.assertEqual(str(cm.exception), "o código expirou; tente entrar de novo")

    def test_host_badge_is_pc_na_rede(self):
        payout = self.addr(7)
        self.st.machines["sigmaq"] = {"payout": payout, "online": True}
        self.assertEqual(self.f.profile(payout)["badges"], ["PC na rede"])
        tid = self.f.new_thread(payout, "geral", "Olá rede", "primeira mensagem")
        self.assertEqual(self.f.thread(tid)["posts"][0]["badges"], ["PC na rede"])


# ------------------------------------------------------------------ HTTP do nó público
class TestHttp(unittest.TestCase):
    def setUp(self):
        self.net = Net(self, public=True, tx_per_ip=4, blob_mb_per_ip_hour=1)
        self.n = self.net.node
        serve(self, nodemod.make_handler(self.n), PORT_HTTP)

    def get(self, path, headers=None):
        return req(PORT_HTTP, "GET", path, headers=headers)

    def post(self, path, body, headers=None):
        return req(PORT_HTTP, "POST", path, body, headers=dict({"Content-Type": "application/json"},
                                                                **(headers or {})))

    def test_status_mining_and_machine_fields(self):
        code, _, body = self.get("/status")
        self.assertEqual(json.loads(body)["version"], sigma.__version__)
        m = json.loads(self.get("/mining")[2])
        self.assertEqual((m["hosting_per_mb_window"], m["ai_default_price"]),
                         (5, chain.DEFAULT_PRICES["ai_per_1k_tokens"]))
        mw = Wallet.generate()
        self.net.send(mw, "register_machine", machine_data(random_address(), pub=mw.pub))
        self.assertTrue(self.net.block())
        rec = json.loads(self.get("/machines")[2])[mw.address]
        self.assertEqual((rec["pub"], rec["jobs_failed"]), (mw.pub, 0))
        rec = json.loads(self.get(f"/machine/{mw.address}")[2])
        self.assertEqual((rec["pub"], rec["assignments"], rec["sites"]), (mw.pub, [], []))

    def test_governance_and_miner_routes(self):
        """As rotas da v2.3 por HTTP de verdade: /governance, /governance/<id> e /mining/miner/<endereço>."""
        miner = Wallet.generate()
        self.net.fund(miner)
        code, _, body = self.get("/governance")
        self.assertEqual(code, 200)
        g = json.loads(body)
        self.assertEqual((g["proposals"], g["vote_seconds"]), ([], chain.VOTE_SECONDS))
        self.assertIn("mint_bps", g["governable"])
        self.assertEqual(self.get("/governance/" + "ab" * 32)[0], 404)
        code, _, body = self.get(f"/mining/miner/{miner.address}")
        self.assertEqual(code, 200)
        self.assertIn("est_per_hour", json.loads(body))
        code, _, body = self.get("/mining")
        self.assertTrue(json.loads(body)["continuous"])

    def test_body_caps(self):
        big = b'{"type": "transfer", "data": {"x": "' + b"a" * (256 * 1024) + b'"}}'
        for path in ("/tx", "/rpc"):
            code, _, body = self.post(path, big)
            self.assertEqual(code, 400, path)
            self.assertEqual(json.loads(body)["error"], "corpo grande demais (máximo 256 KB)")

    def test_lone_surrogate_body(self):
        w = Wallet.generate()
        body = ('{"type": "register_machine", "data": {"payout": "%s", "name": "\\ud800"}, "nonce": 0, '
                '"pub": "%s", "sig": "%s"}' % (w.address, w.pub, "00" * 64)).encode()
        code, _, out = self.post("/tx", body)
        self.assertEqual((code, json.loads(out)["error"]),
                         (400, "transação malformada: texto inválido ou número grande demais"))
        self.assertTrue(self.net.block())

    def test_tx_limit_is_429_with_retry_after(self):
        ip = {"X-Forwarded-For": "200.200.200.1"}
        for _ in range(4):
            tx = make_tx(Wallet.generate(), "register_machine", machine_data(random_address()), 0)
            self.assertEqual(self.post("/tx", tx, ip)[0], 200)
        tx = make_tx(Wallet.generate(), "register_machine", machine_data(random_address()), 0)
        code, headers, body = self.post("/tx", tx, ip)
        self.assertEqual(code, 429)
        self.assertGreaterEqual(int(headers["retry-after"]), 1)
        self.assertIn("limite de 4 transações por conexão de internet nesta janela", json.loads(body)["error"])
        self.assertEqual(self.post("/tx", tx, {"X-Forwarded-For": "200.200.200.2"})[0], 200)
        k = Wallet.generate()
        rpc_tx = {"jsonrpc": "2.0", "id": 9, "method": "eth_sendRawTransaction",
                  "params": [evm_raw(k.priv, 0, to_eth(random_address()), value=1)]}
        code, headers, body = self.post("/rpc", rpc_tx, ip)
        self.assertEqual((code, json.loads(body)["error"]["code"]), (429, rpc.LIMIT_CODE))
        self.assertIn("retry-after", headers)

    def test_blob_quota_is_429_and_does_not_read_the_body(self):
        ip = {"X-Forwarded-For": "200.200.200.3"}
        code, _, body = req(PORT_HTTP, "POST", "/blob", os.urandom(700 * 1024), ip)
        self.assertEqual(code, 200)
        h = json.loads(body)["hash"]
        code, headers, body = req(PORT_HTTP, "POST", "/blob", os.urandom(700 * 1024), ip)
        self.assertEqual(code, 429)
        self.assertIn("limite de envio de arquivos desta conexão de internet: 1 MB por hora",
                      json.loads(body)["error"])
        self.assertIn("retry-after", headers)
        code, headers, _ = self.get(f"/blob/{h}")
        self.assertEqual((code, headers["content-security-policy"], headers["x-content-type-options"]),
                         (200, "sandbox", "nosniff"))

    def test_site_headers_and_open_jobs_cache(self):
        owner = Wallet.generate()
        self.net.fund(owner)
        page = b"<!doctype html><title>oi</title><script>localStorage.x=1</script>"
        h = self.n.put_blob(page)
        self.net.send(owner, "deploy_site", {"name": "meu-site", "manifest": {"index.html": {"hash": h,
                                                                                          "size": len(page)}},
                                             "budget": 100_000, "replicas": 1})
        self.assertTrue(self.net.block())
        code, headers, body = self.get("/site/meu-site/")
        self.assertEqual((code, body), (200, page))
        self.assertEqual(headers["content-security-policy"], "sandbox allow-scripts allow-forms allow-popups "
                         "allow-popups-to-escape-sandbox allow-modals")
        self.assertNotIn("allow-downloads", headers["content-security-policy"])
        self.assertNotIn("allow-same-origin", headers["content-security-policy"])
        self.assertEqual((headers["x-content-type-options"], headers["referrer-policy"],
                          headers["access-control-allow-origin"]), ("nosniff", "no-referrer", "*"))
        code, headers, _ = self.get("/site/meu-site")
        self.assertEqual((code, headers["location"]), (301, "/site/meu-site/"))
        self.assertIn("sandbox", headers["content-security-policy"])

        job = self.net.send(owner, "create_job", {"kind": "compute", "task": {"name": "primes", "params": {"n": 99}},
                                                  "replicas": 1, "escrow": 5000, "max_price": 500})
        self.assertTrue(self.net.block())
        first = self.get("/jobs?status=open")[2]
        self.assertEqual([j["id"] for j in json.loads(first)], [job])
        cached = self.n._cache[1][("jobs", "open")]
        self.assertEqual(self.get("/jobs?status=open")[2], first)
        self.assertIs(self.n._cache[1][("jobs", "open")], cached)   # mesma altura: a mesma resposta pronta
        job_rec = json.loads(self.get(f"/job/{job}")[2])
        self.assertEqual(job_rec["window"], self.net.window)
        self.assertTrue(self.net.block())                            # bloco novo: conta de novo
        self.get("/jobs?status=open")
        self.assertIsNot(self.n._cache[1][("jobs", "open")], cached)

    def test_send_rejects_crlf_in_header_values(self):
        """Achado site-redirect-crlf: nenhum valor de cabeçalho com CR/LF sai na resposta (injetaria Set-Cookie)."""
        handler = nodemod.make_handler(self.n)
        h = handler.__new__(handler)
        h.command, h.wfile = "GET", io.BytesIO()
        sent = []
        h.send_response = lambda code: sent.append(("_status", code))
        h.send_header = lambda k, v: sent.append((k, v))
        h.end_headers = lambda: None
        h._send(301, b"", "application/json", {"Location": "/site/x\r\nSet-Cookie: s=1"})
        self.assertEqual(sent[0], ("_status", 500))
        self.assertFalse(any(("\r" in str(v) or "\n" in str(v)) for _, v in sent))
        self.assertFalse(any("Set-Cookie" in str(k) for k, v in sent))

    def test_site_redirect_validates_name_and_blocks_crlf(self):
        code, headers, _ = self.get("/site/meu-site")                # nome válido, sem barra: 301 com barra
        self.assertEqual((code, headers["location"]), (301, "/site/meu-site/"))
        # CR/LF no nome: 404, sem Location nem Set-Cookie injetado no domínio oficial
        code, headers, _ = self.get("/site/abc%0d%0aSet-Cookie:%20sigma_session=fixado")
        self.assertEqual(code, 404)
        self.assertNotIn("location", headers)
        self.assertNotIn("set-cookie", headers)
        self.assertFalse(any("sigma_session" in str(v) for v in headers.values()))

    def test_site_blocks_executable_downloads(self):
        """Achado site-phishing: /site/ não entrega executável/instalável no domínio oficial."""
        for ext in (".exe", ".msi", ".bat", ".ps1", ".apk", ".zip", ".pyz", ".dmg", ".EXE"):
            code, _, body = self.get(f"/site/qualquer/instalador{ext}")
            self.assertEqual(code, 403, ext)
            self.assertIn("não pode ser baixado", json.loads(body)["error"])
        self.assertEqual(self.get("/site/qualquer/pagina.html")[0], 404)   # html comum não cai na regra


# ------------------------------------------------------------------ main, deploy
class TestMainAndDeploy(unittest.TestCase):
    def test_chain_from_other_rules_gives_a_clear_message(self):
        """Chain da v2.0 no nó v2.1 (regras novas): em vez de um traceback, o que fazer."""
        net = Net(self)
        for _ in range(3):
            self.assertTrue(net.block(20))
        net.close()
        genesis = dict(net.genesis, params=dict(net.genesis["params"], window_seconds=7))   # outras regras
        with open(os.path.join(net.dir, "genesis.json"), "w", encoding="utf-8") as f:
            json.dump(genesis, f)
        args = types.SimpleNamespace(dir=net.dir, genesis=None, producer=False, peer=[], heartbeat=1.0,
                                     blob_cap_gb=0, downloads=None, public=False, work_per_ip=256,
                                     bad_work_per_ip=8, tx_per_ip=300, blob_mb_per_ip_hour=500,
                                     host="127.0.0.1", port=8816)
        with mock.patch.object(nodemod, "ThreadingHTTPServer", side_effect=AssertionError("não devia subir")):
            with self.assertRaises(SystemExit) as cm:
                nodemod.cmd_run(args)
        self.assertIn("não consegui carregar a chain", str(cm.exception))
        self.assertIn("RESET=1", str(cm.exception))

    def test_main_reconfigures_stdio_to_utf8(self):
        out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        d = tempfile.mkdtemp(prefix="sgm")
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(sys, "argv", ["sigma.node", "init", "--dir", d, "--hosting-per-mb-window", "7"]):
            nodemod.main()
            self.assertEqual(sys.stdout.encoding.lower(), "utf-8")
            self.assertEqual(sys.stderr.encoding.lower(), "utf-8")
            print("época ✓", file=sys.stderr)
        with open(os.path.join(d, "genesis.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["params"]["hosting_per_mb_window"], 7)

    def test_ecosystem(self):
        with open(os.path.join(ROOT, "deploy", "ecosystem.config.cjs"), encoding="utf-8") as f:
            txt = f.read()
        node_part = txt[txt.index('name: "sigma-node"'):txt.index('name: "sigma-hosting"')]
        for flag in ("--heartbeat 30", "--public", "--producer", "--work-per-ip 256", "--bad-work-per-ip 8",
                     "--tx-per-ip 300", "--blob-mb-per-ip-hour 500"):
            self.assertIn(flag, node_part)
        self.assertIn('max_memory_restart: "1500M"', node_part)

    def test_deploy_script(self):
        path = os.path.join(ROOT, "deploy", "deploy_vps.sh")
        with open(path, encoding="utf-8") as f:
            sh = f.read()
        self.assertIn('cp -p "$OLD"/no/forum.db* /opt/sigma/data/no/', sh)       # RESET=1 guarda o fórum
        self.assertIn("sigma/netsafe.py", sh)                                    # confere antes de empacotar
        self.assertIn('"ram_per_mb_window" not in', sh)                          # chain até a v2.2 só com RESET=1
        bash = shutil.which("bash")
        try:
            ok = bool(bash) and subprocess.run([bash, "-c", "true"], capture_output=True,
                                               timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            self.skipTest("sem bash para conferir a sintaxe")
        r = subprocess.run([bash, "-n", path], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


# ------------------------------------------------------------------ o run real: nó --public num processo
class TestRealPublicNode(unittest.TestCase):
    """python -m sigma.node run --public de verdade, na porta 8815, tudo por HTTP. Como na VPS atrás do nginx,
    o IP de cada "casa" chega no X-Forwarded-For (o nó só confia nele vindo de 127.0.0.1)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sgr")
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        r = subprocess.run([sys.executable, "-m", "sigma.node", "init", "--dir", self.dir, "--epoch-seconds", "6",
                            "--window-seconds", "6", "--min-bits", "1"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.log_path = os.path.join(self.dir, "node.log")
        self.log = open(self.log_path, "wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "sigma.node", "run", "--dir", self.dir, "--port",
                                      str(PORT_REAL), "--producer", "--public", "--heartbeat", "0.5",
                                      "--work-per-ip", "3", "--bad-work-per-ip", "2", "--tx-per-ip", "6",
                                      "--blob-mb-per-ip-hour", "1"],
                                     cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop)
        for _ in range(150):
            try:
                if self.get("/status")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.2)
        self.fail("o nó não subiu")

    def stop(self):
        self.proc.terminate()
        self.proc.wait(15)
        self.log.close()
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", PORT_REAL))                  # a porta ficou livre
        finally:
            s.close()

    def get(self, path):
        return req(PORT_REAL, "GET", path)

    def post(self, path, body, ip=None):
        headers = {"Content-Type": "application/json"}
        if ip:
            headers["X-Forwarded-For"] = ip
        return req(PORT_REAL, "POST", path, body, headers)

    def mining(self):
        return json.loads(self.get("/mining")[2])

    def fresh_window(self, need=3.5):
        """Espera até sobrar `need` segundos na janela atual: as cotas contam por janela."""
        for _ in range(200):
            m = self.mining()
            if m["window_ends"] - m["now"] >= need:
                return m
            time.sleep(0.1)
        self.fail("a janela não virou")

    def wait(self, cond, what, seconds=30):
        for _ in range(int(seconds * 10)):
            v = cond()
            if v:
                return v
            time.sleep(0.1)
        self.fail(f"tempo esgotado esperando {what}")

    def report(self, m, miner, count=2):
        return {"type": "work", "data": {"miner": miner, "window": m["window"], "bits": 1,
                                         "nonces": find_nonces(m["seed"], miner, 1, count)}}

    def rpc_call(self, payload, ip):
        code, headers, body = self.post("/rpc", payload, ip)
        return code, headers, json.loads(body)

    def test_public_node_over_http(self):
        st = json.loads(self.get("/status")[2])
        self.assertEqual(st["version"], sigma.__version__)
        m = self.mining()
        self.assertEqual((m["hosting_per_mb_window"], m["ai_default_price"]), (5, 2000))
        h0 = st["height"]

        # 1) relatórios de mineração: 3 válidos por casa; lixo gasta só a cota baixa e não barra o válido
        owner = Wallet.generate()
        m = self.fresh_window()
        for miner in (owner.address, random_address(), random_address()):
            code, _, body = self.post("/tx", self.report(m, miner), "200.10.0.1")
            self.assertEqual(code, 200, body)
        code, headers, body = self.post("/tx", self.report(m, random_address()), "200.10.0.1")
        self.assertEqual(code, 429, body)
        self.assertIn("limite de 3 relatórios de mineração por conexão de internet", json.loads(body)["error"])
        self.assertIn("retry-after", headers)
        junk = random_address()
        for i in range(3):
            bad = {"type": "work", "data": {"miner": junk, "window": m["window"], "bits": 1,
                                            "nonces": [bad_nonce(m["seed"], junk, 1, 1000 * i)]}}
            code, _, body = self.post("/tx", bad, "200.10.0.2")
            self.assertEqual(code, 400 if i < 2 else 429, body)
        self.assertIn("relatório recusado (share inválido", json.loads(body)["error"])
        code, _, body = self.post("/tx", self.report(m, random_address()), "200.10.0.2")
        self.assertEqual(code, 200, body)

        # 2) transações assinadas: 6 por casa por janela, contando as da MetaMask
        self.fresh_window()
        for _ in range(6):
            tx = make_tx(Wallet.generate(), "register_machine", machine_data(random_address()), 0)
            self.assertEqual(self.post("/tx", tx, "200.10.0.3")[0], 200)
        tx = make_tx(Wallet.generate(), "register_machine", machine_data(random_address()), 0)
        code, headers, body = self.post("/tx", tx, "200.10.0.3")
        self.assertEqual(code, 429)
        self.assertIn("limite de 6 transações por conexão de internet nesta janela", json.loads(body)["error"])
        k = Wallet.generate()
        send_raw = {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
                    "params": [system_raw(k, 0, canon({"type": "register_machine",
                                                        "data": machine_data(random_address(), name="mm")}))]}
        code, headers, out = self.rpc_call(send_raw, "200.10.0.3")
        self.assertEqual((code, out["error"]["code"]), (429, rpc.LIMIT_CODE))

        # 3) MetaMask pelo /rpc: a operação válida entra; a que travaria o produtor é recusada; lote > 50 não
        code, _, out = self.rpc_call(send_raw, "200.10.0.4")
        self.assertEqual(code, 200, out)
        txid = out["result"]
        bomb = system_raw(Wallet.generate(), 0, b'{"type":"register_machine","data":{"payout":"'
                          + random_address().encode() + b'","name":"\\ud800"}}')
        code, _, out = self.rpc_call({"jsonrpc": "2.0", "id": 2, "method": "eth_sendRawTransaction",
                                      "params": [bomb]}, "200.10.0.4")
        self.assertEqual(out["error"], {"code": -32000, "message": TestEvmSystemCall.MSG})
        code, _, out = self.rpc_call([{"jsonrpc": "2.0", "id": i, "method": "eth_blockNumber"} for i in range(51)],
                                     "200.10.0.4")
        self.assertEqual(out["error"], {"code": -32600, "message": "lote grande demais (máximo 50)"})
        receipt = self.wait(lambda: self.rpc_call({"jsonrpc": "2.0", "id": 3, "method": "eth_getTransactionReceipt",
                                                   "params": [txid]}, "200.10.0.4")[2]["result"], "o recibo")
        self.assertEqual(receipt["status"], "0x1")
        blk = self.rpc_call({"jsonrpc": "2.0", "id": 4, "method": "eth_getBlockByNumber",
                             "params": [receipt["blockNumber"], False]}, "200.10.0.4")[2]["result"]
        self.assertIn(txid, blk["transactions"])
        self.assertIn(k.address, json.loads(self.get("/machines")[2]))

        # 4) blobs: 1 MB por casa por hora; o que vem de /blob abre isolado
        code, _, body = req(PORT_REAL, "POST", "/blob", os.urandom(700 * 1024), {"X-Forwarded-For": "200.10.0.5"})
        self.assertEqual(code, 200)
        h = json.loads(body)["hash"]
        code, headers, body = req(PORT_REAL, "POST", "/blob", os.urandom(700 * 1024),
                                  {"X-Forwarded-For": "200.10.0.5"})
        self.assertEqual(code, 429)
        self.assertIn("limite de envio de arquivos desta conexão de internet: 1 MB por hora", json.loads(body)["error"])
        self.assertIn("retry-after", headers)
        code, headers, _ = self.get(f"/blob/{h}")
        self.assertEqual((code, headers["content-security-policy"], headers["x-content-type-options"]),
                         (200, "sandbox", "nosniff"))

        # 5) site: quem minerou no passo 1 recebeu SIG da época e publica; o site abre numa origem isolada
        self.wait(lambda: json.loads(self.get(f"/account/{owner.address}")[2])["balance"] > 0, "a emissão da época")
        page = b"<!doctype html><title>meu site</title><p>Ola</p>"
        code, _, body = req(PORT_REAL, "POST", "/blob", page, {"X-Forwarded-For": "200.10.0.6"})
        ph = json.loads(body)["hash"]
        nonce = json.loads(self.get(f"/account/{owner.address}")[2])["next_nonce"]
        tx = make_tx(owner, "deploy_site", {"name": "meu-site", "manifest": {"index.html": {"hash": ph,
                                                                                         "size": len(page)}},
                                            "budget": 50_000, "replicas": 1}, nonce)
        code, _, body = self.post("/tx", tx, "200.10.0.6")
        self.assertEqual(code, 200, body)
        self.wait(lambda: (json.loads(self.get("/sites")[2]).get("meu-site") or {}).get("status") == "active",
                  "o site no ar")
        code, headers, body = self.get("/site/meu-site/")
        self.assertEqual((code, body), (200, page))
        self.assertEqual(headers["content-security-policy"], "sandbox allow-scripts allow-forms allow-popups "
                         "allow-popups-to-escape-sandbox allow-modals")
        self.assertNotIn("allow-downloads", headers["content-security-policy"])
        self.assertEqual((headers["x-content-type-options"], headers["referrer-policy"],
                          headers["access-control-allow-origin"]), ("nosniff", "no-referrer", "*"))
        self.assertEqual(self.get("/site/meu-site")[0], 301)

        # 6) corpo grande demais; o produtor seguiu fazendo blocos o tempo todo, sem erro no loop
        code, _, body = self.post("/tx", b'{"x": "' + b"a" * (300 * 1024) + b'"}', "200.10.0.7")
        self.assertEqual((code, json.loads(body)["error"]), (400, "corpo grande demais (máximo 256 KB)"))

        # 7) correções da v2.2 visíveis por HTTP: feeHistory não amplifica; CR/LF no /site não injeta
        #    cabeçalho; extensão executável no /site é 403
        code, _, out = self.rpc_call({"jsonrpc": "2.0", "id": 7, "method": "eth_feeHistory",
                                      "params": ["0x64", "latest", "A" * 250_000]}, "200.10.0.8")
        self.assertEqual(out["error"]["code"], -32602)
        fee = self.rpc_call({"jsonrpc": "2.0", "id": 8, "method": "eth_feeHistory",
                             "params": ["0x5", "latest", [10, 50, 90]]}, "200.10.0.8")[2]["result"]
        self.assertEqual((len(fee["reward"]), len(fee["reward"][0])), (5, 3))
        code, headers, _ = req(PORT_REAL, "GET", "/site/abc%0d%0aSet-Cookie:%20sigma_session=fixado")
        self.assertEqual(code, 404)
        self.assertNotIn("set-cookie", headers)
        self.assertNotIn("location", headers)
        self.assertEqual(self.get("/site/meu-site/Sigma.exe")[0], 403)
        self.assertEqual(self.get("/site/meu-site")[0], 301)      # nome válido continua redirecionando

        self.assertGreater(json.loads(self.get("/status")[2])["height"], h0 + 5)
        self.log.flush()
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            log = f.read()
        self.assertNotIn("[loop] erro", log)
        self.assertNotIn("Traceback", log)


# ------------------------------------------------------------------ v2.3: emissão contínua e votações
class TestV23Api(unittest.TestCase):
    def test_mining_shows_the_continuous_rate(self):
        net = Net(self)
        miner = Wallet.generate()
        net.fund(miner)
        m = nodemod.mining_info(net.node.state)
        st = net.node.state
        self.assertTrue(m["continuous"])
        self.assertEqual(m["mint_per_period"], st.next_mint())
        self.assertEqual(m["mint_per_hour"], st.next_mint() * 3600 // 60)          # época de teste de 60 s
        self.assertEqual(m["rent"], {"disk_bps": 10000, "ram_bps": 10000})
        for k in ("ram_per_mb_window", "demand_max_bps", "proposal_deposit", "quorum_bps", "mint_bps"):
            self.assertIn(k, m)
        hist = [e for e in net.node.history.get(miner.address, []) if e.get("type") == "mint"]
        self.assertEqual(len(hist), 1)                                           # uma linha de mineração por período
        self.assertEqual(hist[0]["amount"], st.bal(miner.address))
        net.node.submit_tx(net.work(miner.address))                              # minera de novo e espera virar base
        self.assertTrue(net.block())
        net.next_window()
        net.next_window()
        st = net.node.state
        info = nodemod.miner_info(st, miner.address, nodemod.mining_info(st))
        self.assertGreater(info["est_per_hour"], 0)
        self.assertEqual(info["est_reward"], info["est_per_hour"])

    def test_governance_listing_and_detail(self):
        net = Net(self)
        a, b = Wallet.generate(), Wallet.generate()
        net.fund(a, b)
        pid = net.send(a, "propose", {"kind": "param", "title": "Aluguel de disco mais barato", "text": "",
                                      "param": "hosting_per_mb_window", "value": 3})
        self.assertTrue(net.block())
        net.send(b, "vote", {"proposal_id": pid, "choice": "sim", "amount": 50 * UNIT})
        self.assertTrue(net.block())
        g = nodemod.governance_info(net.node.state)
        self.assertEqual([p["id"] for p in g["proposals"]], [pid])
        self.assertNotIn("votes", g["proposals"][0])
        self.assertEqual(g["governable"]["hosting_per_mb_window"], {"min": 1, "max": 100, "now": 5})
        self.assertEqual(g["locked"], 50 * UNIT + g["deposit"])
        d = nodemod.proposal_info(net.node.state, pid)
        self.assertEqual((d["yes"], d["no"], d["voters"]), (50 * UNIT, 0, 1))
        self.assertEqual(d["votes"], [{"address": b.address, "choice": "sim", "amount": 50 * UNIT}])
        self.assertIsNone(nodemod.proposal_info(net.node.state, "x"))
