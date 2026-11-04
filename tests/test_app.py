"""Testes do app (sigma/app.py): o App de verdade, sem janela, com pasta de dados temporária e um nó falso.

    python -m unittest tests.test_app -v

O nó falso (FakeNet) roda em 127.0.0.1:8870 (e um segundo em 8871) e responde as rotas que o app e o
sigma.client usam: /status, /mining, /account, /history, /machines, /models, /sites, /tx, /blob e /job, com
máquinas de mentira que respondem, seguram ou reivindicam os pedidos de IA (cifrados de verdade, com as chaves
delas). O painel do app sobe nas portas 47900-47905 e 8880-8889. Host, minerador, motores e detecção de hardware são
trocados por versões falsas (sys.modules), para o teste não depender de motor de IA, de processos de mineração
nem do PC. Nada sai da máquina; nada é gravado fora das pastas temporárias; nenhuma janela abre; o registro do
Windows não é tocado.
"""
import collections
import contextlib
import hashlib
import http.client
import http.server
import io
import json
import math
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from sigma import __version__, app, catalog, chain, crypto, securestore  # noqa: E402
from sigma.chain import canon  # noqa: E402
from sigma.client import Client  # noqa: E402
from sigma.crypto import Wallet, address_of  # noqa: E402

NODE_PORT, NODE2_PORT, DEAD_PORT = 8870, 8871, 8879       # 8879: ninguém escuta (nó fora do ar)
APP_PORTS = list(range(47900, 47906)) + list(range(8880, 8890))   # painéis e Sigmas "antigos" de mentira
NODE, NODE2, DEAD = (f"http://127.0.0.1:{p}" for p in (NODE_PORT, NODE2_PORT, DEAD_PORT))
CODER = "qwen-coder-teste"
CHAT = "modelo-conversa"
GOOD = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"         # checksum EIP-55 certo
TYPO = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAee"         # o mesmo com o último caractere trocado
RAM_MB = 17060                                              # 15,89 GiB: o Windows mostra "16 GB"
END = ("done", "failed", "disputed", "expired", "canceled")
ORIG_OPEN_WINDOW = app.open_window
REAL_ON_BATTERY = app.on_battery
USED_PORTS = set()


# ------------------------------------------------------------------ nó falso
class FakeNet:
    """Estado do nó de mentira. `behavior[máquina]`: "answer" (responde na primeira consulta ao job), "hold"
    (ninguém pega), "claim" (pega e segura até o teste mandar responder)."""

    def __init__(self, version=__version__):
        self.lock = threading.RLock()
        self.version, self.window, self.W = version, 50, 300
        self.down = False
        self.balances = collections.defaultdict(lambda: 10 ** 12)
        self.nonces = collections.defaultdict(int)
        self.machines, self.models, self.sites, self.jobs, self.blobs = {}, {}, {}, {}, {}
        self.keys, self.behavior, self.script, self.requests = {}, {}, [], []
        self.txs = []
        self.cost = 1234
        self.rules, self.upgrades = 1, []    # versão das regras da rede e as atualizações marcadas (/status)

    def reset(self):
        with self.lock:
            for d in (self.machines, self.models, self.sites, self.jobs):
                d.clear()
            del self.script[:], self.requests[:], self.txs[:]
            self.down, self.version = False, __version__
            self.rules, self.upgrades = 1, []

    def add_machine(self, models, price=2000, behavior="answer", seen=0, pub=True, ai=True):
        key = Wallet.generate()
        with self.lock:
            self.machines[key.address] = {
                "name": f"pc-{key.address[3:9]}", "payout": Wallet.generate().address, "online": True, "ai": ai,
                "pub": key.pub if pub else None, "last_seen": self.window - seen, "models": list(models),
                "prices": {"ai_per_1k_tokens": price, "compute_per_unit": 500}, "compute": False}
            self.keys[key.address], self.behavior[key.address] = key, behavior
            for m in models:
                self.models.setdefault(m, {"name": m, "size": 1000, "license": "MIT", "pinners": {}})
                self.models[m]["pinners"][key.address] = {"window": self.window}
        return key.address

    def txs_of(self, type_):
        with self.lock:
            return [d for t, d, _ in self.txs if t == type_]

    def jobs_for(self, machine):
        with self.lock:
            return [dict(j) for j in self.jobs.values() if j.get("machine") == machine]

    def mining(self):
        now = time.time()
        return {"window": self.window, "seed": "ab" * 32, "prev_window": self.window - 1, "prev_seed": "cd" * 32,
                "window_seconds": self.W, "window_ends": now + 100, "min_bits": 12, "max_shares": 32, "epoch": 3,
                "epoch_seconds": 43200, "epoch_ends": now + 3600, "minted": 10 ** 9, "cap": 10 ** 14,
                "next_mint": 5 * 10 ** 11, "points_total": 4096, "miners_epoch": 2, "hashrate": 900.0, "now": now,
                "hosting_per_mb_window": 7, "ram_per_mb_window": 2,
                "rent": {"disk_bps": 10000, "ram_bps": 10000}, "ai_default_price": 2000}

    def get(self, parts):
        if parts == ["status"]:
            return {"network": "sigma", "version": self.version, "height": 10, "time": time.time(),
                    "machines": len(self.machines), "supply": {"minted": 10 ** 9, "cap": 10 ** 14},
                    "block_interval": 25.0, "rules": self.rules, "rules_known": self.rules,
                    "upgrades": list(self.upgrades), "upgrade_required": None}
        if parts == ["mining"]:
            return self.mining()
        if parts == ["mining", "epochs"]:
            return [{"epoch": 2, "mint": 5 * 10 ** 11, "total_points": 1, "miners": 1, "top": []}]
        if parts[:2] == ["mining", "miner"] and len(parts) == 3:
            return {"address": parts[2], "points": 2048, "share": 0.5, "est_reward": 25 * 10 ** 10,
                    "rewards_total": 0, "last_window": self.window - 1, "last_rate": 6.8, "points_total": 2048}
        if parts[0] == "account" and len(parts) == 2:
            return {"address": parts[1], "balance": self.balances[parts[1]], "next_nonce": self.nonces[parts[1]]}
        if parts[0] == "history" and len(parts) == 2:
            return [{"type": "credit", "kind": "job", "epoch": 3, "amount": 900, "count": 2, "height": 9}]
        if parts == ["machines"]:
            return self.machines
        if parts == ["models"]:
            return self.models
        if parts == ["sites"]:
            return self.sites
        if parts == ["governance"]:
            return {"proposals": [], "deposit": 10 * 10 ** 6, "quorum_bps": 100, "vote_seconds": 864000,
                    "governable": {"mint_bps": {"min": 0, "max": 50, "now": 1}}, "locked": 0}
        if parts[0] == "tx" and len(parts) == 2:
            return {"status": "included", "height": 11}
        if parts[0] == "job" and len(parts) == 2:
            job = self.jobs.get(parts[1])
            if job is None:
                raise KeyError("job não encontrado")
            self._advance(job)
            return job
        raise KeyError("rota desconhecida")

    def post_tx(self, tx):
        sender = address_of(tx["pub"])
        t, d = tx["type"], tx["data"]
        if tx["nonce"] != self.nonces[sender]:
            raise ValueError(f"nonce errado: esperado {self.nonces[sender]}")
        if t == "cancel_job":
            job = self.jobs.get(d.get("job_id"))
            if not job or job["client"] != sender:
                raise ValueError("só quem pediu pode cancelar")
            if job["status"] != "open" or job["claims"]:
                raise ValueError("um computador já está respondendo este pedido")
            job["status"] = "canceled"
        if t == "create_job":
            self.jobs[tx["id"]] = dict(d, id=tx["id"], client=sender, status="open", claims={}, results={},
                                      created=time.time())
        self.nonces[sender] += 1
        self.txs.append((t, d, sender))
        return {"id": tx["id"]}

    def _advance(self, job):
        """Cada consulta ao job faz a máquina dar o próximo passo do roteiro dela."""
        m = job.get("machine")
        how = self.behavior.get(m, "answer")
        if job["status"] in END or how == "hold":
            return
        if how == "claim":
            if not job["claims"]:
                job["claims"], job["status"] = {m: {"time": time.time()}}, "running"
            return
        request = json.loads(crypto.unseal(self.keys[m].priv, self.blobs[job["spec_hash"]]))
        self.requests.append(request)
        text = self.script.pop(0) if self.script else f"resposta para {len(request['messages'])} mensagens"
        sealed = crypto.seal(job["client_pub"], canon({"text": text, "model": job["model"], "machine": m}))
        h = hashlib.sha256(sealed).hexdigest()
        self.blobs[h] = sealed
        job.update(status="done", cost=self.cost, result_hash=h, results={m: {"hash": h}})


def make_node_handler(net):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, raw=None):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p] or [""]
            if net.down:
                return self._send(503, {"error": "nó em manutenção (falha simulada)"})
            try:
                with net.lock:
                    if parts[0] == "blob" and len(parts) == 2:
                        return self._send(200, None, raw=net.blobs[parts[1]])
                    body = json.loads(json.dumps(net.get(parts)))   # cópia: ninguém mexe no estado do nó
            except KeyError as e:
                return self._send(404, {"error": str(e)})
            self._send(200, body)

        def do_POST(self):
            path = urlparse(self.path).path
            data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if net.down:
                return self._send(503, {"error": "nó em manutenção (falha simulada)"})
            with net.lock:
                if path == "/blob":
                    h = hashlib.sha256(data).hexdigest()
                    net.blobs[h] = data
                    return self._send(200, {"hash": h})
                if path == "/tx":
                    try:
                        return self._send(200, net.post_tx(json.loads(data)))
                    except (ValueError, KeyError) as e:
                        return self._send(400, {"error": str(e)})
            self._send(404, {"error": "rota desconhecida"})

    return H


def serve(handler, port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return srv


# ------------------------------------------------- peças falsas do app
class FakeMiner:
    made = []

    def __init__(self, client, payout, workers, log=print, worker=None):
        self.client, self.payout, self.workers, self.worker = client, payout, workers, worker
        self.running = self.paused = False
        self.extra = {}
        FakeMiner.made.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def set_workers(self, n):
        self.workers = n

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stats(self):
        return dict({"running": self.running, "paused": self.paused, "workers": self.workers, "hashrate": 1500.0,
                     "window": 50, "bits": 12, "shares_window": 3, "reports_sent": 4, "reports_failed": 0,
                     "fail_streak": 0, "last_fail": None, "last_ok": 1793700000.0,
                     "last_report": {"window": 49, "time": 1793700000.0}, "last_error": None,
                     "payout": self.payout, "worker": self.worker}, **self.extra)


class FakeHost:
    made = []

    def __init__(self, node_url, machine_wallet, payout, data_dir, settings, miner=None, log=print):
        self.node, self.payout, self.settings, self.miner = node_url, payout, dict(settings), miner
        self.started = self.stopped = False
        FakeHost.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def update_settings(self, settings):
        self.settings = dict(settings)

    def stats(self):
        return {"registered": True, "online": True, "models": [], "sites": ["meu-site"], "jobs_done": 2,
                "jobs_failed": 1, "earned": 5000, "reputation": 3, "ai_queue": 0, "engine_down": False,
                "engine": {"kind": "mock", "ready": False, "detail": "esperando a rede", "installing": False,
                           "install_progress": 0.0},
                "last_error": None}


def fake_modules():
    """Host, minerador, motores e hardware de mentira (o app importa esses módulos na hora de usar)."""
    host, miner, engines, hardware = (types.ModuleType(f"sigma.{n}") for n in ("host", "miner", "engines", "hardware"))
    host.Host, miner.Miner = FakeHost, FakeMiner

    class EngineManager:
        def shutdown(self):
            pass
    engines.EngineManager = EngineManager
    hardware.detect = lambda data_dir: {"cpu_cores": 8, "ram_mb": RAM_MB, "vram_mb": 0, "gpu": "",
                                        "disk_free_mb": 199885, "os": sys.platform}
    return {"sigma.host": host, "sigma.miner": miner, "sigma.engines": engines, "sigma.hardware": hardware}


BATTERY = {"on": False, "calls": 0}
AUTOSTART = {"calls": [], "value": None}
OPENED, FATAL = [], []


def fake_on_battery():
    BATTERY["calls"] += 1
    return BATTERY["on"]


def fake_set_autostart(enabled, command):
    """Igual ao de verdade, mas grava numa variável em vez do registro do Windows."""
    if not app.autostart_supported():
        if enabled:
            raise app.AppError(f"{app.LABELS['start_with_system']}: por enquanto só funciona no Windows.")
        return
    AUTOSTART["calls"].append((enabled, command))
    AUTOSTART["value"] = command if enabled else None


PATCHES, SAVED_MODULES, SERVERS = [], {}, []
NET = FakeNet()
NET2 = FakeNet(version="2.1.0")


def setUpModule():
    fakes = fake_modules()
    SAVED_MODULES.update({k: sys.modules.get(k) for k in fakes})
    sys.modules.update(fakes)
    for p in (mock.patch.object(app, "on_battery", fake_on_battery),
              mock.patch.object(app, "set_autostart", fake_set_autostart),
              mock.patch.object(app, "read_autostart", lambda: AUTOSTART["value"]),
              mock.patch.object(app, "open_window", lambda url, ui_dir: OPENED.append((url, ui_dir)) or True),
              mock.patch.object(app, "_fatal", lambda msg, gui: FATAL.append(msg)),
              mock.patch.object(Client, "POLL_FAST", 0.05), mock.patch.object(Client, "POLL_SLOW", 0.1)):
        p.start()
        PATCHES.append(p)
    SERVERS.append(serve(make_node_handler(NET), NODE_PORT))
    SERVERS.append(serve(make_node_handler(NET2), NODE2_PORT))


def tearDownModule():
    for srv in SERVERS:
        srv.shutdown()
        srv.server_close()
    for p in reversed(PATCHES):
        p.stop()
    for k, v in SAVED_MODULES.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


# ----------------------------------------------------------------- ajudantes
def linger(sock):
    """Fecha com RST: a porta do painel não fica em TIME_WAIT (no Windows, com SO_EXCLUSIVEADDRUSE, isso
    impediria rodar os testes de novo logo em seguida)."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH" if os.name == "nt" else "ii", 1, 0))


def call(port, method, path, body=None, token=None, host=None, origin=None, timeout=60):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.connect()
    linger(conn.sock)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-Sigma-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        return r.status, parsed, hdrs
    finally:
        conn.close()


def free_port():
    """Uma porta do painel livre (as já usadas nesta execução ficam de fora)."""
    for p in APP_PORTS:
        if p in USED_PORTS:
            continue
        s = socket.socket()
        try:
            if os.name == "nt":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", p))
        except OSError:
            continue
        finally:
            s.close()
        USED_PORTS.add(p)
        return p
    raise unittest.SkipTest("as portas 47900-47905 e 8880-8889 estão ocupadas (rodou os testes agora há pouco? "
                            "espere 2 min)")


def new_app(data_dir, port=None, extra=(), node=NODE):
    argv = ["--headless", "--data-dir", data_dir, "--node", node, *extra]
    if port:
        argv += ["--port", str(port)]
    return app.App(app.parse_args(argv))


def wait_for(fn, timeout=20, what="a condição"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"tempo esgotado esperando {what}")


def tmpdir(prefix):
    return tempfile.mkdtemp(prefix=prefix)


def rmtree(d):
    shutil.rmtree(d, ignore_errors=True)


# ============================================================== o App inteiro
class AppTest(unittest.TestCase):
    """Um App de verdade (headless) contra o nó falso; as rotas são chamadas por HTTP, como o painel faz."""

    @classmethod
    def setUpClass(cls):
        cls.poll_patch = mock.patch.object(app.App, "_poll_loop", lambda self: None)   # consultas à mão
        cls.poll_patch.start()
        cls.dir = tmpdir("sgapp")
        cls.port = free_port()
        cls.app = new_app(cls.dir, cls.port)
        assert cls.app.take_lock()
        with contextlib.redirect_stdout(io.StringIO()):
            cls.app.boot()
        cls.token = cls.app.token

    @classmethod
    def tearDownClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):
            cls.app.shutdown()
        cls.poll_patch.stop()
        rmtree(cls.dir)

    def setUp(self):
        NET.reset()
        BATTERY["on"] = False
        self.out = contextlib.redirect_stdout(io.StringIO())   # o log do app não suja a saída do teste
        self.out.__enter__()
        self.addCleanup(self.out.__exit__, None, None, None)

    # ---------------------------------------------------------- ajudantes
    def api(self, method, path, body=None, **kw):
        kw.setdefault("token", self.token)
        return call(self.port, method, path, body, **kw)[:2]

    def ok(self, method, path, body=None):
        code, data = self.api(method, path, body)
        self.assertEqual(code, 200, data)
        return data

    def err(self, method, path, body=None):
        code, data = self.api(method, path, body)
        self.assertEqual(code, 400, data)
        return data["error"]

    def state(self):
        return self.ok("GET", "/api/state")

    def settle(self):
        """Espera o Host e o minerador recomeçarem com a carteira atual (a troca recomeça os serviços numa
        thread)."""
        a = self.app

        def ready():
            h, m, pay = a.host, a.miner, a._payout()
            if not h or h.payout != pay or h.stopped:
                return False
            return not a._mining_wanted() or bool(m and m.payout == pay and m.running)
        wait_for(ready, what="os serviços recomeçarem com a carteira nova")

    def setup_wallet(self, body):
        # o painel manda "key_saved": true depois do portão de "Trocar carteira" (ver test_trocar_carteira_exige_…)
        r = self.ok("POST", "/api/setup", dict(body, key_saved=True))
        self.settle()
        return r

    def create_wallet(self):
        return self.setup_wallet({"mode": "create"})

    def poll(self):
        self.app._poll(full=True)

    # ------------------------------------------------- painel e proteções
    def test_painel_sem_fontes_de_fora_e_protegido(self):
        code, page, h = call(self.port, "GET", "/")
        self.assertEqual(code, 200)
        csp = h["content-security-policy"]
        for bad in ("googleapis", "gstatic", "fonts.g", "http:", "https:"):
            self.assertNotIn(bad, csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(call(self.port, "GET", "/", host="evil.example:47900")[0], 403)   # DNS rebinding
        self.assertEqual(call(self.port, "GET", "/api/state")[0], 401)                       # sem token
        self.assertEqual(call(self.port, "GET", "/api/state", token="errado")[0], 401)
        self.assertEqual(call(self.port, "GET", "/api/state", token=self.token, origin="http://evil.example")[0],
                         403)                                                                  # outro site
        code, data, h = call(self.port, "GET", "/api/ping")
        self.assertEqual((code, data["app"], data["version"], data["instance"]),
                         (200, "sigma", __version__, self.app.instance))
        self.assertNotIn("googleapis", h["content-security-policy"])
        self.assertEqual(self.api("GET", "/api/nada")[0], 404)
        self.assertEqual(self.api("GET", "/api/ai/chat")[0], 405)

    # -------------------------------------------------------------- carteira
    def test_setup_endereco_confere_checksum(self):
        self.assertEqual(self.err("POST", "/api/setup", {"mode": "address", "address": TYPO}), app.ADDR_TYPO)
        self.assertEqual(self.err("POST", "/api/setup", {"mode": "address", "address": "0x123"}), app.ADDR_BAD)
        self.assertEqual(self.err("POST", "/api/setup", {"mode": "address", "address": "0x+" + "a" * 39}),
                         app.ADDR_BAD)     # norm_address aceitaria (int("+aaa…", 16)); aqui não passa
        r = self.setup_wallet({"mode": "address", "address": GOOD})
        self.assertEqual(r["address"], "sig" + GOOD[2:].lower())
        s = self.state()
        self.assertTrue(s["setup_done"])
        self.assertFalse(s["wallet"]["can_spend"])
        self.setup_wallet({"mode": "address", "address": GOOD.lower()})      # sem checksum: passa

    def test_importar_a_chave_desta_carteira(self):
        mine, other = Wallet.generate(), Wallet.generate()
        self.setup_wallet({"mode": "address", "address": mine.address})
        msg = self.err("POST", "/api/setup", {"mode": "import_key", "private_key": format(other.priv, "064x"),
                                              "expect_address": mine.address})
        self.assertEqual(msg, f"Essa chave é de outra carteira ({other.address}). Para receber em outra carteira, "
                              f"use Trocar carteira.")
        s = self.state()
        self.assertEqual((s["wallet"]["address"], s["wallet"]["can_spend"]), (mine.address, False))
        msg = self.err("POST", "/api/send", {"to": GOOD, "amount": "1"})
        self.assertIn("importe a chave em Carteira → Importar a chave desta carteira", msg)
        self.assertIn(f"{NODE}/carteira", msg)
        r = self.setup_wallet({"mode": "import_key", "private_key": "0x" + format(mine.priv, "064x"),
                               "expect_address": mine.address})
        self.assertEqual(r["address"], mine.address)
        self.assertTrue(self.state()["wallet"]["can_spend"])

    def test_trocar_carteira_guarda_a_chave_antiga(self):
        first = self.create_wallet()
        self.assertEqual(len(first["private_key"]), 64)
        second = self.create_wallet()
        self.assertNotEqual(first["address"], second["address"])
        folder = os.path.join(self.dir, "carteiras-antigas")
        old = [f for f in os.listdir(folder) if f.startswith(first["address"])]
        self.assertEqual(len(old), 1, os.listdir(folder))
        key = securestore.load_secret(os.path.join(folder, old[0])).decode()
        self.assertEqual(key, first["private_key"])            # a chave antiga continua guardada, protegida
        s = self.state()
        self.assertEqual(s["wallet"]["address"], second["address"])
        self.assertTrue(s["wallet"]["can_spend"])
        self.assertIsNone(s["wallet"]["balance"])             # saldo da carteira nova ainda não lido: desconhecido
        self.assertIs(s["wallet"]["balance_known"], False)    # nunca o saldo da carteira antiga nem "0,00"
        self.assertEqual((self.app.host.payout, self.app.miner.payout), (second["address"], second["address"]))
        self.assertTrue(FakeHost.made[-2].stopped)             # o Host da carteira antiga parou

    def test_enviar_e_passar_adiante_conferem_checksum(self):
        self.create_wallet()
        self.assertEqual(self.err("POST", "/api/send", {"to": TYPO, "amount": "1,5"}), app.ADDR_TYPO)
        self.assertEqual(NET.txs_of("transfer"), [])           # nada saiu
        self.ok("POST", "/api/send", {"to": GOOD, "amount": "1,5"})
        self.assertEqual(NET.txs_of("transfer"), [{"to": "sig" + GOOD[2:].lower(), "amount": 1_500_000}])
        self.assertEqual(self.err("POST", "/api/models/update", {"name": "meu-modelo", "new_sponsor": TYPO}),
                         app.ADDR_TYPO)
        self.ok("POST", "/api/models/update", {"name": "meu-modelo", "new_sponsor": GOOD})
        self.assertEqual(NET.txs_of("update_model")[-1]["new_sponsor"], "sig" + GOOD[2:].lower())

    # -------------------------------------------------------- configurações
    def test_configuracoes_aceitam_decimais_e_falam_portugues(self):
        r = self.ok("POST", "/api/settings", {"disk_gb": "2,5", "ai_ram_gb": 1.5})
        self.assertEqual((r["settings"]["disk_gb"], r["settings"]["ai_ram_gb"]), (2.5, 1.5))
        self.assertEqual(self.app.host.settings["disk_gb"], 2.5)
        with open(self.app.config_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["settings"]["ai_ram_gb"], 1.5)
        cases = {("ai_ram_gb", 64): "Memória para IA: escolha de 0 a 15 GB (a memória deste computador).",
                 ("disk_gb", -1): "Disco para a rede: escolha de 0 a 100000 GB.",
                 ("cpu_percent", 0): "Uso do processador: escolha de 1 a 100%.",
                 ("mining_enabled", "sim"): "Minerar SIG: o valor deve ser true ou false",
                 ("mine_on_battery", 1): "Minerar também na bateria: o valor deve ser true ou false",
                 ("name", ""): "Nome na rede: use de 1 a 40 caracteres.",
                 ("node", "ftp://x"): "Endereço do nó: use uma URL http:// ou https://"}
        for (k, v), want in cases.items():
            msg = self.err("POST", "/api/settings", {k: v})
            self.assertEqual(msg, want)
            self.assertNotIn(k, msg)                           # nunca a chave interna (ex.: 'Ai_ram_gb')
        self.assertEqual(self.app._ram_top(), 15)              # 17.060 MB decimais = 15,89 GB do Windows
        self.assertIs(self.state()["settings"]["mine_on_battery"], False)
        self.assertIn("configuração desconhecida", self.err("POST", "/api/settings", {"cheat": True}))

    def test_no_fora_do_ar_nao_repete_a_frase(self):
        msg = self.err("POST", "/api/settings", {"node": DEAD})
        self.assertEqual(msg.count("não consegui falar com o nó"), 1, msg)
        self.assertEqual(self.app.settings["node"], NODE)

    def test_trocar_de_no(self):
        self.app.node_state["ok"] = True
        self.ok("POST", "/api/settings", {"node": NODE2})
        try:
            self.assertIsNone(self.app.node_state["ok"])       # outra rede: "conectando", não "sem rede"
            self.assertEqual(self.app.cache["status"], {})
            self.poll()
            self.assertIs(self.app.node_state["ok"], True)
            self.assertEqual(self.state()["network"]["version"], "2.1.0")
        finally:
            self.ok("POST", "/api/settings", {"node": NODE})
            wait_for(lambda: self.app.host and self.app.host.node == NODE, what="o Host voltar ao nó 1")
            self.poll()

    def test_iniciar_com_o_computador_so_onde_existe(self):
        with mock.patch.object(app, "autostart_supported", lambda: False):
            msg = self.err("POST", "/api/settings", {"start_with_system": True})
            self.assertEqual(msg, "Iniciar o Sigma junto com o computador: por enquanto só funciona no Windows.")
            self.assertIs(self.state()["features"]["autostart"], False)
        with mock.patch.object(app, "autostart_supported", lambda: True):
            self.ok("POST", "/api/settings", {"start_with_system": True})
            self.assertEqual(AUTOSTART["calls"][-1], (True, self.app.autostart_command()))
            self.assertIn("--background", AUTOSTART["calls"][-1][1])
            self.ok("POST", "/api/settings", {"start_with_system": False})
            self.assertEqual(AUTOSTART["calls"][-1][0], False)

    # ------------------------------------------------------------- estado
    def test_estado_tem_o_contrato_do_painel(self):
        self.create_wallet()
        self.poll()
        s = self.state()
        for k in ("version", "setup_done", "wallet", "node", "network", "update", "features", "limits", "mining",
                  "machine", "epochs", "code", "settings", "logs"):
            self.assertIn(k, s)
        self.assertEqual(s["limits"], {"ai_ram_gb": 15, "disk_gb": 100000})   # os mesmos máximos do servidor
        for k in ("address", "eth_address", "can_spend", "balance", "balance_known", "history"):
            self.assertIn(k, s["wallet"])
        self.assertIs(s["wallet"]["balance_known"], True)               # depois do poll o saldo é conhecido
        self.assertIsInstance(s["wallet"]["balance"], int)
        self.assertEqual(s["wallet"]["history"][0]["type"], "credit")   # créditos do nó passam direto
        self.assertEqual(set(s["node"]), {"url", "ok", "error", "fix"})
        for k in ("enabled", "running", "paused", "workers", "hashrate", "window", "window_ends", "epoch",
                  "epoch_ends", "next_mint", "my_points", "total_points", "my_share", "est_reward",
                  "rewards_total", "last_error", "reports_sent", "reports_failed", "fail_streak", "report_error",
                  "last_fail", "last_ok", "worker", "on_battery", "battery_pause", "hosting_per_mb_window",
                  "ai_default_price"):
            self.assertIn(k, s["mining"])
        self.assertEqual(s["mining"]["hosting_per_mb_window"], 7)        # o preço da rede, do /mining
        for k in ("address", "registered", "online", "hw", "engine", "models", "sites", "jobs_done",
                  "jobs_failed", "earned", "reputation", "ai_queue", "engine_down", "last_error"):
            self.assertIn(k, s["machine"])
        self.assertEqual(s["code"], {"running": False, "pending": False})
        self.assertEqual(set(s["features"]), {"autostart", "pick_folder"})
        self.assertIs(s["node"]["ok"], True)
        self.assertEqual(s["mining"]["worker"], self.app.machine.address[3:11])

    def test_minerador_recebe_o_computador_como_worker(self):
        self.create_wallet()
        self.assertEqual(self.app.miner.worker, self.app.machine.address[3:11])
        self.assertEqual(self.app.miner.payout, self.app._payout())

    def test_minerando_sem_contar(self):
        self.create_wallet()
        m = self.app.miner
        self.app.node_state = {"ok": True, "error": None, "fix": None}
        why = "limite de relatórios de mineração deste IP nesta janela; o próximo vai na janela seguinte"
        m.extra = {"fail_streak": 3, "last_fail": why, "reports_failed": 3}
        try:
            mi = self.state()["mining"]
            self.assertEqual((mi["fail_streak"], mi["report_error"], mi["last_fail"], mi["reports_failed"]),
                             (3, why, why, 3))
            self.assertTrue(self.app.status_line().startswith(
                "Σ SIGMA · minerando sem contar (relatórios recusados) · "))
        finally:
            m.extra = {}
        self.assertTrue(self.app.status_line().startswith("Σ SIGMA · minerando · 1,5 kH/s · "))

    def test_status_line(self):
        self.create_wallet()
        a = self.app
        a.node_state = {"ok": None, "error": None, "fix": None}
        self.assertEqual(a.status_line(), "Σ SIGMA · conectando à rede…")
        a.node_state = {"ok": False, "error": "x", "fix": None}
        self.assertIn("· sem conexão com a rede ·", a.status_line())
        a.node_state = {"ok": True, "error": None, "fix": None}
        a.miner.paused = True
        self.assertIn("mineração pausada (IA trabalhando)", a.status_line())
        a.miner.paused = False
        self.ok("POST", "/api/settings", {"mining_enabled": False})
        try:
            self.assertIsNone(a.miner)
            self.assertTrue(a.status_line().startswith("Σ SIGMA · mineração pausada · hospedando a rede · "))
        finally:
            self.ok("POST", "/api/settings", {"mining_enabled": True})

    # ---------------------------------------------------------------- bateria
    def test_bateria_pausa_e_volta_sozinha(self):
        self.create_wallet()
        self.app.node_state = {"ok": True, "error": None, "fix": None}
        BATTERY["on"] = True
        self.app._check_battery(force=True)
        self.assertIsNone(self.app.miner)                      # para de verdade: o Host não consegue "retomar"
        mi = self.state()["mining"]
        self.assertEqual((mi["on_battery"], mi["battery_pause"], mi["running"]), (True, True, False))
        self.assertIn("mineração pausada (na bateria)", self.app.status_line())
        self.app.start_services()                              # nova tentativa de ligar tudo (ex.: após erro)
        self.assertIsNone(self.app.miner)
        self.ok("POST", "/api/settings", {"mine_on_battery": True})
        self.assertIsNotNone(self.app.miner)                   # a pessoa liberou: minera na bateria
        self.ok("POST", "/api/settings", {"mine_on_battery": False})
        self.assertIsNone(self.app.miner)
        BATTERY["on"] = False
        self.app._check_battery(force=True)
        self.assertIsNotNone(self.app.miner)                   # carregador ligado: volta sozinha
        self.assertIn("minerando", self.app.status_line())

    def test_bateria_conferida_a_cada_30_s(self):
        self.app._check_battery(force=True)
        n = BATTERY["calls"]
        self.app._check_battery()
        self.app._check_battery()
        self.assertEqual(BATTERY["calls"], n)                  # menos de 30 s: nem pergunta ao sistema
        self.app._battery_at -= app.BATTERY_SECONDS + 1
        self.app._check_battery()
        self.assertEqual(BATTERY["calls"], n + 1)

    # -------------------------------------------------------------- conexão
    def test_conexao_conectando_e_duas_falhas(self):
        a = self.app
        self.create_wallet()
        a.node_state, a._node_fails = {"ok": None, "error": None, "fix": None}, 0
        NET.down = True
        try:
            self.poll()
            self.assertIsNone(a.node_state["ok"])              # uma falha na abertura: ainda "conectando"
            self.assertEqual(a.status_line(), "Σ SIGMA · conectando à rede…")
            self.poll()
            self.assertIs(a.node_state["ok"], False)           # duas seguidas: "sem conexão"
            self.assertIn("nó em manutenção", a.node_state["error"])
            self.assertIn("sem conexão com a rede", a.status_line())
            NET.down = False
            self.poll()
            self.assertEqual(a.node_state, {"ok": True, "error": None, "fix": None})
            NET.down = True
            self.poll()
            self.assertIs(a.node_state["ok"], False)           # estava conectado: a primeira falha já conta
        finally:
            NET.down = False
            self.poll()

    def test_macos_sem_certificados(self):
        a = self.app
        err = Exception("não consegui falar com o nó https://x: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] "
                        "certificate verify failed: unable to get local issuer certificate>")
        with mock.patch.object(app, "_is_mac", lambda: True):
            a.node_state, a._node_fails = {"ok": None, "error": None, "fix": None}, 0
            a._node_failed(err)                                # nem espera a segunda falha: precisa agir
            self.assertEqual(a.node_state, {"ok": False, "error": app.CERT_FIX, "fix": app.CERT_FIX})
            self.assertEqual(app.friendly(err), app.CERT_FIX)
        self.assertEqual(app.friendly(err), str(err))          # fora do Mac, a frase de sempre
        self.poll()

    # ------------------------------------------------------------ atualização
    def test_versao_nova_na_rede(self):
        NET.version = "9.0.0"
        self.poll()
        self.assertEqual(self.state()["update"], {"version": "9.0.0", "url": f"{NODE}/baixar"})
        self.assertEqual(self.app.update_line(), "Saiu uma versão nova do Sigma (9.0.0). Baixe e abra: ela fecha "
                                                 "esta e continua de onde parou.")
        NET.version = "2.0.0"
        self.poll()
        self.assertIsNone(self.state()["update"])
        self.assertEqual(self.app.update_line(), "")

    def test_mudanca_de_regras_marcada_e_atrasada(self):
        """docs/ATUALIZACOES.md: o app avisa antes da altura marcada e depois, se ficou para trás."""
        self.assertEqual(self.state()["rules"], {"known": chain.RULES, "network": 1, "notice": None})
        NET.upgrades = [{"rules": chain.RULES + 1, "height": 1010}]   # 1.000 blocos de ~25 s
        self.poll()
        self.assertEqual(self.state()["rules"]["notice"], {"rules": chain.RULES + 1, "height": 1010,
                                                           "blocks_left": 1000, "seconds_left": 25_000,
                                                           "late": False})
        self.assertEqual(self.app.update_line(), f"A rede muda de regras (v{chain.RULES + 1}) no bloco 1010: baixe a "
                                                 "versão nova do Sigma antes disso. Ela fecha esta e continua de onde "
                                                 "parou.")
        NET.upgrades = [{"rules": chain.RULES, "height": 1010}]       # uma que este Sigma já conhece: nada a fazer
        self.poll()
        self.assertIsNone(self.state()["rules"]["notice"])
        NET.upgrades, NET.rules = [], chain.RULES + 1                  # a rede já passou
        self.poll()
        self.assertTrue(self.state()["rules"]["notice"]["late"])
        self.assertIn("este Sigma não as conhece", self.app.update_line())
        NET.upgrades, NET.rules = ["lixo", {"rules": "2", "height": 5}, {"rules": True, "height": 5}], 1
        self.poll()
        self.assertIsNone(self.state()["rules"]["notice"])             # resposta estranha do nó não vira aviso

    # ------------------------------------------------------------------- IA
    def test_modelos_mostram_o_teto_de_preco(self):
        NET.add_machine([CHAT], price=1500)
        NET.add_machine([CHAT], price=900_000)                 # a mais cara não define nada
        NET.add_machine(["outro-modelo"], price=3000)
        NET.add_machine(["sem-chave"], price=10, pub=False)    # não aceita pedido cifrado
        NET.add_machine(["dormindo"], price=10, seen=2)        # sinalizou há 2 janelas
        NET.add_machine(["ia-desligada"], price=10, ai=False)
        self.app._prices = (0.0, {})
        r = self.ok("GET", "/api/models")
        self.assertEqual(r["ai"]["prices"], {CHAT: 2000, "outro-modelo": 3000})
        self.assertEqual(r["ai"]["context"], 4096)
        self.assertIn(CHAT, r["network"])
        self.assertTrue(any(i["id"] == "qwen2.5-0.5b" for i in r["catalog"]))

    def test_custo_maximo_antes_de_enviar(self):
        NET.add_machine([CHAT], price=1500)
        self.app._prices = (0.0, {})
        msgs = [{"role": "user", "content": "x" * 300}]
        q = self.ok("POST", "/api/ai/quote", {"model": CHAT, "messages": msgs, "max_tokens": 512})
        tokens = 300 // 3 + 8 + 16
        self.assertEqual(q, {"model": CHAT, "price": 2000, "max_tokens": 512, "trimmed": 0,
                             "max_cost": math.ceil((tokens + 512) * 2000 / 1000)})
        q = self.ok("POST", "/api/ai/quote", {"model": "ninguem-tem", "messages": msgs})
        self.assertEqual((q["price"], q["max_cost"]), (None, None))
        msg = self.err("POST", "/api/ai/quote", {"model": CHAT, "messages": [{"role": "user", "content": "y" * 20000}]})
        self.assertIn("longa demais", msg)
        self.assertIn("messages deve ser", self.err("POST", "/api/ai/quote", {"model": CHAT, "messages": []}))

    def test_chat_responde_com_maquina_custo_e_teto(self):
        self.create_wallet()
        m = NET.add_machine([CHAT], price=1500)
        question = "qual é a receita secreta do bolo da vovó?"
        r = self.ok("POST", "/api/ai/chat", {"model": CHAT, "messages": [{"role": "user", "content": question}],
                                            "max_tokens": 256, "id": "c1"})
        job = NET.jobs_for(m)[0]
        self.assertEqual(r["text"], "resposta para 1 mensagens")
        self.assertEqual((r["cost"], r["machine"], r["trimmed"], r["attempts"]), (NET.cost, m, 0, 1))
        self.assertEqual((r["max_cost"], r["price"], r["job"]), (job["escrow"], 2000, job["id"]))
        spec = NET.blobs[job["spec_hash"]]
        self.assertTrue(crypto.is_sealed(spec))
        self.assertNotIn(question.encode(), spec)              # o nó só guarda bytes cifrados
        self.assertEqual(self.app.chats, [])
        self.assertIn("id do pedido inválido", self.err("POST", "/api/ai/chat", {
            "model": CHAT, "messages": [{"role": "user", "content": "oi"}], "id": "não pode"}))

    def test_chat_respeita_o_teto_mostrado(self):
        self.create_wallet()
        NET.add_machine([CHAT], price=5000)
        msg = self.err("POST", "/api/ai/chat", {"model": CHAT, "max_price": 2000,
                                                "messages": [{"role": "user", "content": "oi"}]})
        self.assertIn("5000", msg)                             # mais cara do que o painel mostrou: não reserva
        self.assertEqual(NET.txs_of("create_job"), [])

    def test_chat_conversa_longa_corta_as_antigas(self):
        self.create_wallet()
        NET.add_machine([CHAT])
        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"{i} " + "z" * 1000} for i in range(31)]
        r = self.ok("POST", "/api/ai/chat", {"model": CHAT, "messages": msgs, "max_tokens": 512})
        self.assertGreater(r["trimmed"], 0)
        sent = NET.requests[-1]["messages"]
        self.assertEqual(len(sent), 31 - r["trimmed"])
        self.assertEqual(sent[0]["content"], msgs[0]["content"])       # a primeira do usuário fica
        self.assertEqual(sent[-1]["content"], msgs[-1]["content"])     # e a mais recente também

    def _chat_in_thread(self, body):
        box = {}

        def run():
            box["res"] = self.api("POST", "/api/ai/chat", body)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, box

    def test_cancelar_antes_de_alguem_pegar(self):
        self.create_wallet()
        m = NET.add_machine([CHAT], behavior="hold")
        t, box = self._chat_in_thread({"model": CHAT, "messages": [{"role": "user", "content": "oi"}], "id": "c2"})
        wait_for(lambda: NET.jobs_for(m), what="o pedido chegar ao nó")
        self.assertEqual(self.ok("POST", "/api/ai/cancel", {"id": "outro"})["running"], False)   # outro id: nada
        r = self.ok("POST", "/api/ai/cancel", {"id": "c2"})
        self.assertEqual((r["ok"], r["running"], r["claimed"], r["message"]), (True, True, False, None))
        t.join(30)
        code, data = box["res"]
        self.assertEqual((code, data["error"], data["code"]), (400, app.CANCELED, "canceled"))
        self.assertEqual(NET.jobs_for(m)[0]["status"], "canceled")     # o valor reservado voltou (cancel_job)
        self.assertEqual(len(NET.txs_of("cancel_job")), 1)

    def test_cancelar_antes_da_pergunta_chegar(self):
        """Clique em Cancelar que chega ao app antes da própria pergunta (corrida de cliques): ela nem sai."""
        self.create_wallet()
        NET.add_machine([CHAT])
        r = self.ok("POST", "/api/ai/cancel", {"id": "cedo"})
        self.assertEqual((r["running"], r["claimed"]), (False, False))
        code, data = self.api("POST", "/api/ai/chat", {"model": CHAT, "messages": [{"role": "user", "content": "oi"}],
                                                       "id": "cedo"})
        self.assertEqual((code, data["error"], data["code"]), (400, app.CANCELED, "canceled"))
        self.assertEqual(NET.txs_of("create_job"), [])                 # nada foi reservado
        r = self.ok("POST", "/api/ai/chat", {"model": CHAT, "messages": [{"role": "user", "content": "oi"}],
                                             "id": "cedo"})            # o cancelamento valeu uma vez só
        self.assertTrue(r["text"])

    def test_cancelar_depois_que_um_computador_pegou(self):
        self.create_wallet()
        m = NET.add_machine([CHAT], behavior="claim")
        t, box = self._chat_in_thread({"model": CHAT, "messages": [{"role": "user", "content": "oi"}]})
        wait_for(lambda: NET.jobs_for(m) and NET.jobs_for(m)[0]["claims"], what="a máquina pegar o pedido")
        r = self.ok("POST", "/api/ai/cancel", {})
        self.assertEqual((r["claimed"], r["message"]), (True, app.CLAIMED))
        with NET.lock:
            NET.behavior[m] = "answer"                         # ela entrega: a resposta chega normalmente
        t.join(30)
        code, data = box["res"]
        self.assertEqual(code, 200, data)
        self.assertEqual(data["machine"], m)
        self.assertEqual(NET.txs_of("cancel_job"), [])

    def test_chat_sem_chave_explica_onde_importar(self):
        self.setup_wallet({"mode": "address", "address": GOOD})
        NET.add_machine([CHAT])
        msg = self.err("POST", "/api/ai/chat", {"model": CHAT, "messages": [{"role": "user", "content": "oi"}]})
        self.assertIn("modo só endereço", msg)
        self.assertIn("Importar a chave desta carteira", msg)

    # --------------------------------------------------------------- modelos
    def test_apadrinhar_recusa_licenca_nao_declarada(self):
        self.create_wallet()
        r = self.ok("POST", "/api/models/sponsor", {"catalog_id": "qwen2.5-0.5b", "budget": "10"})
        self.assertEqual(r["name"], "qwen2.5-0.5b")
        self.assertEqual(NET.txs_of("register_model")[-1]["license"], "Apache-2.0")
        n = len(NET.txs_of("register_model"))
        info = {"repo": "alguem/modelo-GGUF", "file": "modelo-q4_k_m.gguf", "size": 10 ** 6, "sha256": "ab" * 32,
                "url": "https://huggingface.co/alguem/modelo-GGUF/resolve/main/modelo-q4_k_m.gguf"}
        for lic, want in (("", catalog.LICENSE_UNDECLARED), ("other", catalog.LICENSE_UNDECLARED),
                          ("unknown", catalog.LICENSE_UNDECLARED), ("cc-by-nc-4.0", "não permite uso comercial")):
            with mock.patch.object(catalog, "resolve_hf_url", lambda url, lic=lic: dict(info, license=lic)):
                msg = self.err("POST", "/api/models/sponsor", {"url": info["url"], "budget": "10"})
            self.assertIn(want, msg)
        self.assertEqual(len(NET.txs_of("register_model")), n)        # nada foi registrado

    def test_apadrinhar_exige_um_dia_de_hospedagem(self):
        """As máquinas não baixam modelo que não paga um dia de aluguel: o painel avisa antes de gastar."""
        self.create_wallet()
        self.poll()
        n = len(NET.txs_of("register_model"))
        # qwen2.5-0.5b: 491.400.032 bytes x 7 µSIG por MB (preço do /mining falso) = 3.440 por cópia por janela
        msg = self.err("POST", "/api/models/sponsor", {"catalog_id": "qwen2.5-0.5b", "budget": "1"})
        self.assertIn("um dia de hospedagem das 2 cópias", msg)
        self.assertIn("3,250944 SIG", msg)    # 2 cópias x 5.644 (disco 7/MB + 1.102 MB de memória a 2/MB) x 288 janelas
        self.assertEqual(len(NET.txs_of("register_model")), n)
        self.ok("POST", "/api/models/sponsor", {"catalog_id": "qwen2.5-0.5b", "budget": "3.250944"})

    # ------------------------------------------------------------- votações
    def test_votacoes_propor_e_votar(self):
        self.create_wallet()
        g = self.ok("GET", "/api/governance")
        self.assertEqual((g["deposit"], g["can_spend"]), (10 * 10 ** 6, True))
        self.ok("POST", "/api/governance/propose", {"title": "Emissão ainda menor", "text": "por quê",
                                                    "param": "mint_bps", "value": 0})
        self.assertEqual(NET.txs_of("propose")[-1], {"kind": "param", "title": "Emissão ainda menor",
                                                     "text": "por quê", "param": "mint_bps", "value": 0})
        self.ok("POST", "/api/governance/propose", {"title": "Ideia de texto"})
        self.assertEqual(NET.txs_of("propose")[-1], {"kind": "text", "title": "Ideia de texto", "text": ""})
        pid = "ab" * 32
        self.ok("POST", "/api/governance/vote", {"proposal_id": pid, "choice": "sim", "amount": "12.5"})
        self.assertEqual(NET.txs_of("vote")[-1], {"proposal_id": pid, "choice": "sim", "amount": 12_500_000})
        n = len(NET.txs)
        for path, body, want in (("/api/governance/propose", {"title": "x"}, "3 a 120"),
                                 ("/api/governance/propose", {"title": "Teto", "param": "supply_cap", "value": 1},
                                  "não pode ser votado"),
                                 ("/api/governance/propose", {"title": "Demais", "param": "mint_bps", "value": 51},
                                  "valor novo"),
                                 ("/api/governance/vote", {"proposal_id": "x", "choice": "sim", "amount": "1"},
                                  "votação inválida"),
                                 ("/api/governance/vote", {"proposal_id": pid, "choice": "talvez", "amount": "1"},
                                  "'sim' ou 'nao'")):
            with self.subTest(body=body):
                self.assertIn(want, self.err("POST", path, body))
        self.assertEqual(len(NET.txs), n)                                 # nada recusado foi enviado

    # ----------------------------------------------------------------- sites
    def test_recarregar_site_expirado_ha_muito_tempo(self):
        w = self.create_wallet()
        self.poll()
        per_day = 86400 // NET.W
        base = {"owner": w["address"], "manifest": {"index.html": {"hash": "aa" * 32, "size": 10}}, "replicas": 2,
                "budget": 0}
        with NET.lock:
            NET.sites.update({
                "velho": dict(base, name="velho", status="expired", expired_window=NET.window - 8 * per_day),
                "recente": dict(base, name="recente", status="expired", expired_window=NET.window - per_day),
                "ativo": dict(base, name="ativo", status="active"),
                "de-outro": dict(base, name="de-outro", status="active", owner=Wallet.generate().address)})
        self.assertEqual(self.err("POST", "/api/sites/fund", {"name": "velho", "amount": "1"}),
                         "O site ficou fora do ar por muito tempo e os arquivos saíram da rede: publique a pasta de "
                         "novo.")
        self.ok("POST", "/api/sites/fund", {"name": "recente", "amount": "1"})
        self.ok("POST", "/api/sites/fund", {"name": "ativo", "amount": "1"})
        self.assertEqual(self.err("POST", "/api/sites/fund", {"name": "de-outro", "amount": "1"}),
                         "este site pertence a outra carteira")
        self.assertEqual([d["name"] for d in NET.txs_of("deploy_site")], ["recente", "ativo"])
        sites = self.ok("GET", "/api/sites")
        self.assertEqual({s["name"] for s in sites["mine"]}, {"velho", "recente", "ativo"})
        self.assertEqual(sites["hosted"], ["meu-site"])

    def test_site_expirado_no_limite_do_no(self):
        """A mesma conta do nó (node.blob_refs): os arquivos saem quando w - expired_window >= ceil(7 dias / janela)."""
        w = self.create_wallet()
        self.poll()
        grace = math.ceil(app.SITE_GRACE / NET.W)
        base = {"owner": w["address"], "manifest": {"index.html": {"hash": "aa" * 32, "size": 10}}, "replicas": 2,
                "budget": 0, "status": "expired"}
        with NET.lock:
            NET.sites.update({"no-limite": dict(base, name="no-limite", expired_window=NET.window - grace),
                              "quase": dict(base, name="quase", expired_window=NET.window - grace + 1)})
        self.assertIn("publique a pasta de novo", self.err("POST", "/api/sites/fund", {"name": "no-limite",
                                                                                        "amount": "1"}))
        self.ok("POST", "/api/sites/fund", {"name": "quase", "amount": "1"})
        self.assertEqual([d["name"] for d in NET.txs_of("deploy_site")], ["quase"])

    # ------------------------------------------------------------ Sigma Code
    def _project(self):
        d = tmpdir("sgproj")
        self.addCleanup(rmtree, d)
        with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
            f.write("SENHA=segredo-do-usuario\n")
        return d

    def code_state(self):
        return self.ok("GET", "/api/code/state")

    def wait_code(self, cond, what):
        return wait_for(lambda: (lambda s: s if cond(s) else None)(self.code_state()), timeout=30, what=what)

    def test_sigma_code_recusa_pastas_perigosas(self):
        self.create_wallet()
        NET.add_machine([CODER])
        proj = self._project()
        os.makedirs(os.path.join(self.dir, "ui"), exist_ok=True)
        cases = [(os.path.splitdrive(proj)[0] + os.sep if os.name == "nt" else "/", "a unidade inteira"),
                 (os.path.expanduser("~"), "a sua pasta de usuário inteira"),
                 (self.dir, "pasta de dados do Sigma"),
                 (os.path.join(self.dir, "ui"), "pasta de dados do Sigma"),
                 (os.path.join(proj, "nao-existe"), "não existe"),
                 (os.path.join(proj, ".env"), "é um arquivo"),
                 ("projeto-relativo", "caminho completo"),
                 ("", "escolha a pasta do projeto")]
        cases += [(p, "inteiros não podem") for p in app._shell_folders() if os.path.isdir(p)]   # as de verdade
        for folder, want in cases:
            msg = self.err("POST", "/api/code/start", {"folder": folder, "task": "faça um site"})
            self.assertIn(want, msg, folder)
        self.assertIn("escreva o que o Sigma Code deve fazer",
                      self.err("POST", "/api/code/start", {"folder": proj, "task": "   "}))
        self.assertEqual(self.err("POST", "/api/code/start", {"folder": proj, "task": "x", "auto_files": "sim"}),
                         "auto_files deve ser true ou false")
        self.assertEqual(self.err("POST", "/api/code/start", {"folder": proj, "task": "x", "model": "outro-modelo"}),
                         "o modelo outro-modelo não está no ar agora: escolha outro")
        with NET.lock:
            NET.machines.clear()
        self.assertEqual(self.err("POST", "/api/code/start", {"folder": proj, "task": "x"}), app.NO_CODE_MODEL)
        self.setup_wallet({"mode": "address", "address": GOOD})
        self.assertIn("modo só endereço", self.err("POST", "/api/code/start", {"folder": proj, "task": "x"}))
        self.assertFalse(self.code_state()["running"])
        self.assertEqual(NET.txs_of("create_job"), [])

    def test_sigma_code_pede_aprovacao_e_protege_segredos(self):
        self.create_wallet()
        m = NET.add_machine([CHAT, CODER])
        proj = self._project()
        NET.script[:] = [
            json.dumps({"tool": "write_file", "args": {"path": "ola.txt", "content": "oi\n"}}),
            json.dumps({"tool": "read_file", "args": {"path": ".env"}}),
            json.dumps({"tool": "run_command", "args": {"command": "echo invadido > pwned.txt"}}),
            json.dumps({"final": "pronto"})]
        s = self.ok("POST", "/api/code/start", {"folder": proj, "task": "crie ola.txt"})
        self.assertTrue(s["running"])
        self.assertEqual((s["model"], s["auto_files"], s["folder"]), (CODER, False, proj))   # prefere o de código
        s = self.wait_code(lambda s: s["pending"], "o pedido para gravar")
        p = s["pending"]
        self.assertEqual((p["tool"], p["reason"], p["changed"]), ("write_file", "write", []))
        self.assertEqual(p["args"], {"path": "ola.txt", "content": "oi\n"})
        self.assertEqual(p["target"], "ola.txt")              # título do cartão vem do alvo resolvido, não dos args
        self.assertIn("ola.txt", p["preview"])
        self.assertFalse(os.path.exists(os.path.join(proj, "ola.txt")))       # nada gravado sem aprovar
        self.assertTrue(self.state()["code"]["pending"])
        self.assertIn("já está trabalhando", self.err("POST", "/api/code/start", {"folder": proj, "task": "outra"}))
        self.assertIn("não está mais aberto", self.err("POST", "/api/code/answer", {"id": "velho", "ok": True}))
        self.assertIn("true ou false", self.err("POST", "/api/code/answer", {"id": p["id"], "ok": "sim"}))
        self.ok("POST", "/api/code/answer", {"id": p["id"], "ok": True})
        s = self.wait_code(lambda s: s["pending"] and s["pending"]["id"] != p["id"], "o pedido para executar")
        q = s["pending"]
        self.assertEqual((q["tool"], q["reason"], q["changed"]), ("run_command", "command", ["ola.txt"]))
        self.assertEqual(q["target"], "")                     # comando não tem alvo: o painel mostra só a prévia
        self.assertIn("echo invadido", q["preview"])
        self.ok("POST", "/api/code/answer", {"id": q["id"], "ok": False})
        s = self.wait_code(lambda s: not s["running"], "a tarefa terminar")
        self.assertEqual(s["final"], "pronto")
        self.assertIsNone(s["error"])
        with open(os.path.join(proj, "ola.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "oi\n")
        self.assertFalse(os.path.exists(os.path.join(proj, "pwned.txt")))       # recusado: não rodou
        tools = [x for x in s["steps"] if x["kind"] == "tool"]
        self.assertEqual([(x["tool"], x["refused"]) for x in tools],
                         [("write_file", False), ("read_file", True), ("run_command", True)])
        self.assertIn("arquivo protegido", tools[1]["output"])
        self.assertTrue(NET.requests)
        for req in NET.requests:                               # o .env nunca foi para a máquina que responde
            self.assertNotIn("segredo-do-usuario", json.dumps(req))
        self.assertEqual(s["spent"], 4 * NET.cost)
        infos = [x for x in s["steps"] if x["kind"] == "info"]
        self.assertEqual([(x["cost"], x["machine"]) for x in infos], [(NET.cost, m)] * 4)
        self.assertEqual([x["kind"] for x in s["steps"] if x["kind"] == "assistant"], ["assistant"] * 4)
        self.assertEqual(s["steps"][-1], {"kind": "final", "text": "pronto", "n": 4})
        self.assertEqual(self.state()["code"], {"running": False, "pending": False})

    def test_sigma_code_parar_recusa_o_pendente(self):
        self.create_wallet()
        NET.add_machine([CODER])
        proj = self._project()
        NET.script[:] = [json.dumps({"tool": "write_file", "args": {"path": "x.txt", "content": "x"}})] * 3
        self.ok("POST", "/api/code/start", {"folder": proj, "task": "grave x.txt", "model": CODER})
        self.wait_code(lambda s: s["pending"], "o pedido para gravar")
        s = self.ok("POST", "/api/code/stop", {})
        self.assertIsNone(s["pending"])
        s = self.wait_code(lambda s: not s["running"], "a tarefa parar")
        self.assertFalse(os.path.exists(os.path.join(proj, "x.txt")))
        self.assertEqual(s["steps"][-1]["text"], "Tarefa parada.")
        self.assertIsNone(s["final"])
        self.assertEqual(len(NET.txs_of("create_job")), 1)    # parou depois do passo atual
        self.assertEqual(self.ok("POST", "/api/code/stop", {})["running"], False)   # parar de novo não quebra

    def test_sigma_code_liberar_arquivos_so_quando_pedido(self):
        self.create_wallet()
        NET.add_machine([CODER])
        proj = self._project()
        NET.script[:] = [json.dumps({"tool": "write_file", "args": {"path": "a.txt", "content": "a"}}),
                         json.dumps({"final": "ok"})]
        s = self.ok("POST", "/api/code/start", {"folder": proj, "task": "grave", "auto_files": True})
        self.assertTrue(s["auto_files"])
        s = self.wait_code(lambda s: not s["running"], "a tarefa terminar")
        self.assertEqual(s["final"], "ok")
        self.assertTrue(os.path.exists(os.path.join(proj, "a.txt")))
        NET.script[:] = [json.dumps({"final": "nada"})]      # tarefa nova começa sem a liberação
        s = self.ok("POST", "/api/code/start", {"folder": proj, "task": "outra"})
        self.assertFalse(s["auto_files"])
        self.assertEqual(s["steps"], [])                       # o histórico da tarefa anterior saiu
        self.wait_code(lambda s: not s["running"], "a segunda tarefa terminar")

    def test_escolher_pasta(self):
        self.assertEqual(self.err("POST", "/api/code/pick-folder", {}), "digite o caminho da pasta")
        proj = self._project()
        stop = threading.Event()

        def tk_thread():   # faz o papel da thread da janelinha
            while not stop.is_set():
                self.app.run_ui_calls(None)
                time.sleep(0.02)
        t = threading.Thread(target=tk_thread, daemon=True)
        self.app.window_up = True
        t.start()
        try:
            with mock.patch.object(app, "_pick_folder", lambda root: proj + "/"):
                self.assertEqual(self.ok("POST", "/api/code/pick-folder", {}), {"folder": os.path.normpath(proj)})
            self.assertTrue(self.state()["features"]["pick_folder"])
            with mock.patch.object(app, "_pick_folder", lambda root: ""):
                self.assertEqual(self.ok("POST", "/api/code/pick-folder", {}), {"folder": ""})   # cancelou
        finally:
            self.app.window_up = False
            stop.set()
            t.join(5)

    # ------------------------------------------------- saldo e troca de carteira
    def test_saldo_desconhecido_ate_a_primeira_leitura(self):
        """app-swap-gate-unknown-balance: o saldo começa null/desconhecido até o primeiro /account; o painel
        mostra '—' e não '0,00'."""
        self.create_wallet()
        s = self.state()                                       # ainda sem poll
        self.assertIsNone(s["wallet"]["balance"])
        self.assertIs(s["wallet"]["balance_known"], False)
        self.app.node_state = {"ok": True, "error": None, "fix": None}
        self.assertTrue(self.app.status_line().endswith("· —"))   # a janelinha mostra — enquanto não sabe
        self.poll()
        s = self.state()
        self.assertIsInstance(s["wallet"]["balance"], int)
        self.assertIs(s["wallet"]["balance_known"], True)

    def test_trocar_carteira_exige_chave_guardada(self):
        """app-swap-gate-unknown-balance (defesa no servidor): trocar uma carteira com chave guardada exige
        'key_saved': true no corpo (o painel só manda depois de mostrar/guardar a chave)."""
        self.create_wallet()                                   # cria carteira com chave guardada
        code, data = self.api("POST", "/api/setup", {"mode": "create"})   # sem key_saved
        self.assertEqual(code, 400, data)
        self.assertIn("guarde a chave", data["error"])
        code, data = self.api("POST", "/api/setup", {"mode": "address", "address": GOOD})   # troca p/ endereço idem
        self.assertEqual(code, 400, data)
        self.assertEqual(NET.txs_of("transfer"), [])           # nada mudou: a carteira antiga continua
        r = self.ok("POST", "/api/setup", {"mode": "create", "key_saved": True})   # com key_saved: troca
        self.settle()
        self.assertEqual(len(r["private_key"]), 64)

    def test_importar_a_chave_da_carteira_atual_nao_pede_portao(self):
        """O 'Importar a chave desta carteira' (mesmo endereço) não é troca: não exige key_saved."""
        self.setup_wallet({"mode": "address", "address": GOOD})   # modo só endereço: nenhuma chave guardada
        mine = Wallet.generate()
        self.setup_wallet({"mode": "address", "address": mine.address})
        r = self.ok("POST", "/api/setup", {"mode": "import_key", "private_key": format(mine.priv, "064x"),
                                           "expect_address": mine.address})   # sem key_saved, e passa
        self.assertEqual(r["address"], mine.address)
        self.assertTrue(self.state()["wallet"]["can_spend"])

    # ------------------------------------------------------------ IA (escrow)
    def test_chat_manda_max_tokens_ao_cliente(self):
        """escrow-need-not-cover-price (app): o chat manda max_tokens ao client.ask, que dimensiona o escrow."""
        self.create_wallet()
        NET.add_machine([CHAT])
        seen = {}
        real = Client.ask

        def spy(self, model, messages, **kw):
            seen.update(kw)
            return real(self, model, messages, **kw)
        with mock.patch.object(Client, "ask", spy):
            self.ok("POST", "/api/ai/chat", {"model": CHAT, "messages": [{"role": "user", "content": "oi"}],
                                             "max_tokens": 321})
        self.assertEqual(seen.get("max_tokens"), 321)          # o teto vai ao cliente: ele reserva o escrow por ele

    # ------------------------------------------------------ Sigma Code (cartão)
    def test_cartao_de_aprovacao_mostra_o_alvo_real(self):
        """app-approval-card-spoof: pending.target é o caminho resolvido, nunca uma chave livre dos args."""
        self.create_wallet()
        NET.add_machine([CODER])
        proj = self._project()
        NET.script[:] = [json.dumps({"tool": "write_file", "args": {"path": "package.json", "content": "{}",
                                                                     "command": "README.md"}}),
                         json.dumps({"final": "ok"})]
        self.ok("POST", "/api/code/start", {"folder": proj, "task": "grave", "model": CODER})
        p = self.wait_code(lambda s: s["pending"], "o pedido para gravar")["pending"]
        self.assertEqual(p["target"], "package.json")          # o arquivo de verdade, não o "README.md" dos args
        self.assertEqual(p["args"].get("command"), "README.md")   # a chave enganosa ainda aparece crua nos args
        self.ok("POST", "/api/code/answer", {"id": p["id"], "ok": False})
        self.wait_code(lambda s: not s["running"], "a tarefa terminar")

    def test_coderunner_passo_guarda_o_alvo_real(self):
        """app-approval-card-spoof: o passo da conversa guarda o event target, não uma chave dos args."""
        runner = app.CodeRunner(self.app)
        runner._event({"kind": "tool", "tool": "write_file", "target": "package.json", "output": "",
                       "args": {"path": "package.json", "command": "README.md"}, "refused": False})
        step = runner.state()["steps"][-1]
        self.assertEqual(step["target"], "package.json")
        self.assertEqual(step["args"].get("command"), "README.md")

    def test_code_state_aguenta_argumento_ruim(self):
        """app-approval-card-spoof (_send): um argumento com caractere inválido (surrogate) não quebra
        /api/code/state — com ensure_ascii=True ele vira \\uXXXX em vez de derrubar a resposta."""
        runner = self.app.code
        with runner.lock:
            runner.info = app.CodeRunner._blank(pending={
                "id": "x", "tool": "write_file", "reason": "write", "changed": [], "target": "a\ud800b",
                "args": {"path": "a\ud800b"}, "preview": "pré\ud800via"})
        try:
            code, data, _ = call(self.port, "GET", "/api/code/state", token=self.token)
            self.assertEqual(code, 200, data)                  # sem o fix daria 400 (UnicodeEncodeError ao serializar)
            self.assertEqual(data["pending"]["tool"], "write_file")
        finally:
            with runner.lock:
                runner.info = app.CodeRunner._blank()

    # ------------------------------------------------- pasta do Sigma Code (UNC)
    def test_code_folder_recusa_unc_e_ancestrais(self):
        """app-code-folder-bypass: caminhos UNC/\\\\?\\ e pastas que CONTÊM a de dados ou pessoais são recusados."""
        home = tmpdir("sghome2")
        self.addCleanup(rmtree, home)
        data = os.path.join(home, "AppData", "Local", "Sigma")
        for f in (os.path.join("OneDrive", "Desktop"), os.path.join("OneDrive", "Documentos"),
                  os.path.join("OneDrive", "projeto"), data):
            os.makedirs(os.path.join(home, f), exist_ok=True)
        with mock.patch.object(app, "_shell_folders", lambda: []), \
                mock.patch.dict(os.environ, {"OneDrive": os.path.join(home, "OneDrive")}):
            with self.assertRaises(app.AppError) as cm:                         # raiz do OneDrive contém pessoais
                app.code_folder(os.path.join(home, "OneDrive"), data, home=home)
            self.assertIn("contém", str(cm.exception))
            sub = os.path.join(home, "OneDrive", "projeto")
            self.assertEqual(app.code_folder(sub, data, home=home), os.path.abspath(sub))   # subpasta pode
            if os.name == "nt":
                for bad in ("\\\\?\\" + home, "\\\\?\\" + data, r"\\localhost\c$\Users",
                            r"\\?\UNC\servidor\compart", r"\\.\C:"):
                    with self.assertRaises(app.AppError):
                        app.code_folder(bad, data, home=home)
                self.assertEqual(app.code_folder("\\\\?\\" + sub, data, home=home),   # \\?\ é tirado e a subpasta passa
                                 os.path.abspath(sub))


# ======================================================= peças soltas do app
class HelpersTest(unittest.TestCase):
    def test_elo_do_minerador_repassa_o_mining(self):
        """O Host sinaliza a janela com o /mining que o minerador já buscou (fluxo-polling-escala-no)."""
        link = app.MinerLink()
        self.assertEqual(link.last_mining(), (None, 0.0))           # sem minerador: o Host consulta sozinho
        link.miner = types.SimpleNamespace(last_mining=lambda: ({"window": 7}, 123.0))
        self.assertEqual(link.last_mining(), ({"window": 7}, 123.0))
        link.miner = types.SimpleNamespace()                         # minerador antigo, sem o método
        self.assertEqual(link.last_mining(), (None, 0.0))

    def test_slogan_numa_forma_so(self):
        with open(app.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("Os donos dos robôs devem ser as pessoas", src)
        self.assertIn("Queremos que os donos dos robôs sejam as pessoas, não as", src)

    def test_endereco_digitado(self):
        self.assertEqual(app.typed_address("  " + GOOD + " "), "sig" + GOOD[2:].lower())
        self.assertEqual(app.typed_address("sig" + GOOD[2:]), "sig" + GOOD[2:].lower())   # checksum também no sig…
        for bad, want in ((TYPO, app.ADDR_TYPO), ("sig" + TYPO[2:], app.ADDR_TYPO), ("0x" + "_" * 40, app.ADDR_BAD),
                          ("0x+" + "a" * 39, app.ADDR_BAD), ("0x " + "a" * 39, app.ADDR_BAD), (None, app.ADDR_BAD),
                          (12, app.ADDR_BAD), ("SIG" + "a" * 40, app.ADDR_BAD)):
            with self.assertRaises(app.AppError) as cm:
                app.typed_address(bad)
            self.assertEqual(str(cm.exception), want, bad)

    def test_versoes(self):
        self.assertEqual(app.version_tuple("2.1.0"), (2, 1, 0))
        self.assertLess(app.version_tuple("2.0.9"), app.version_tuple("2.1.0"))
        self.assertLess(app.version_tuple("2.1.0"), app.version_tuple("2.10.0"))     # número, não texto
        self.assertLess(app.version_tuple("lixo"), app.version_tuple("0.0.1"))       # ilegível = mais velho
        self.assertLess(app.version_tuple(None), app.version_tuple("0.0.1"))

    def test_gb_como_o_windows(self):
        self.assertAlmostEqual(app.gib(RAM_MB), 15.888, places=2)
        self.assertAlmostEqual(app.gib(1073.741824), 1.0, places=6)

    def test_tetos_de_preco(self):
        pub = Wallet.generate().pub

        def m(models, price, seen=10, online=True, key=pub):
            return {"online": online, "ai": True, "pub": key, "last_seen": seen, "models": models,
                    "prices": {"ai_per_1k_tokens": price}}
        ms = {"sigA": m(["m"], 1000), "sigB": m(["m", "n"], 4000, seen=9), "sigC": m(["velha"], 1, seen=8),
              "sigD": m(["fora"], 1, online=False), "sigE": m(["chave-ruim"], 1, key="ab"), "sigF": m(["eu"], 1)}
        self.assertEqual(app.ai_ceilings(ms, {"window": 10, "ai_default_price": 2000}, me="sigF"),
                         {"m": 2000, "n": 4000})
        self.assertEqual(app.ai_ceilings(ms, {"window": 10, "ai_default_price": 500}, me="sigF"),
                         {"m": 1000, "n": 4000})
        self.assertEqual(app.ai_ceilings(None, None), {})

    def test_bateria_por_sistema(self):
        root = tmpdir("sgpow")
        self.addCleanup(rmtree, root)

        def supply(name, type_, online=None):
            d = os.path.join(root, name)
            os.makedirs(d)
            with open(os.path.join(d, "type"), "w") as f:
                f.write(type_ + "\n")
            if online is not None:
                with open(os.path.join(d, "online"), "w") as f:
                    f.write(online + "\n")
        self.assertFalse(app._linux_on_battery(root))              # desktop sem fonte nenhuma: tomada
        supply("BAT0", "Battery")
        self.assertFalse(app._linux_on_battery(root))              # só bateria, sem "Mains": não dá para saber
        supply("AC", "Mains", "0")
        self.assertTrue(app._linux_on_battery(root))
        supply("ADP1", "Mains", "1")
        self.assertFalse(app._linux_on_battery(root))              # um carregador ligado basta
        self.assertTrue(app._mac_on_battery("Now drawing from 'Battery Power'\n -InternalBattery-0 80%"))
        self.assertFalse(app._mac_on_battery("Now drawing from 'AC Power'"))
        self.assertFalse(app._mac_on_battery(None))
        if os.name == "nt":
            self.assertIn(app._windows_on_battery(), (True, False))   # chamada real ao Windows não quebra

    def test_bateria_erro_conta_como_tomada(self):
        with mock.patch.object(app, "_windows_on_battery", side_effect=OSError("x")), \
                mock.patch.object(app, "_linux_on_battery", side_effect=OSError("x")), \
                mock.patch.object(app.subprocess, "run", side_effect=OSError("x")):
            self.assertIs(REAL_ON_BATTERY(), False)

    def test_pastas_do_sigma_code(self):
        home = tmpdir("sghome")
        self.addCleanup(rmtree, home)
        data = os.path.join(home, "AppData", "Local", "Sigma")
        for f in ("Desktop", "Downloads", "Documents", os.path.join("OneDrive", "Desktop"),
                  os.path.join("OneDrive - Empresa", "Documents"), os.path.join("Desktop", "projeto"),
                  os.path.join("Downloads", "site"), data, "Empresa-Docs"):
            os.makedirs(os.path.join(home, f), exist_ok=True)
        with mock.patch.object(app, "_shell_folders", lambda: [os.path.join(home, "Empresa-Docs")]), \
                mock.patch.dict(os.environ, {"OneDrive": os.path.join(home, "OneDrive")}):
            def check(rel, want=None):
                path = os.path.join(home, rel) if rel else home
                if want is None:
                    self.assertEqual(app.code_folder(path, data, home=home), os.path.abspath(path))
                    return
                with self.assertRaises(app.AppError) as cm:
                    app.code_folder(path, data, home=home)
                self.assertIn(want, str(cm.exception), rel)
            for rel in ("Desktop", "Downloads", "Documents", os.path.join("OneDrive", "Desktop"),
                        os.path.join("OneDrive - Empresa", "Documents"), "Empresa-Docs"):
                check(rel, "inteiros não podem")
            check("", "pasta de usuário inteira")
            check(os.path.join("AppData", "Local", "Sigma"), "pasta de dados do Sigma")
            check("AppData", "contém a pasta de dados do Sigma")
            check(os.path.join("Desktop", "projeto"))              # subpastas podem
            check(os.path.join("Downloads", "site"))
            self.assertEqual(app.code_folder("~/Desktop/projeto", data, home=home),
                             os.path.abspath(os.path.join(home, "Desktop", "projeto")))
            self.assertEqual(app.code_folder('"' + os.path.join(home, "Downloads", "site") + '"', data, home=home),
                             os.path.abspath(os.path.join(home, "Downloads", "site")))
            with self.assertRaises(app.AppError):
                app.code_folder(None, data, home=home)
        if os.name == "nt":   # o Windows diz onde estão as pastas de verdade (também no OneDrive)
            real = app._shell_folders()
            self.assertGreaterEqual(len(real), 1)
            self.assertTrue(all(isinstance(p, str) and os.path.isabs(p) for p in real))

    def test_icone_da_janelinha_pelo_meipass(self):
        base = tmpdir("sgmei")
        self.addCleanup(rmtree, base)
        os.makedirs(os.path.join(base, "sigma"))
        ico = os.path.join(base, "sigma", "sigma.ico")
        with open(ico, "wb") as f:
            f.write(b"\0\0\1\0")
        with mock.patch.object(sys, "_MEIPASS", base, create=True):
            self.assertEqual(app.icon_file("sigma.ico"), ico)       # dentro do Sigma.exe (PyInstaller)
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(app.__file__))), "packaging", "sigma.ico")
        if os.path.exists(src):                                     # rodando do código-fonte: o de packaging/
            self.assertEqual(os.path.normcase(app.icon_file("sigma.ico")), os.path.normcase(src))
        self.assertIsNone(app.icon_file("nao-existe.ico"))

    def test_icone_aplicado_na_janelinha(self):
        class Root:   # o mínimo do tk.Tk que _set_icon usa (sem abrir janela nenhuma)
            def __init__(self):
                self.calls = []

            def iconbitmap(self, default=None):
                self.calls.append(("iconbitmap", default))

            def iconphoto(self, default, img):
                self.calls.append(("iconphoto", default, img))
        fake_tk = types.SimpleNamespace(PhotoImage=lambda **kw: ("foto", tuple(sorted(kw))))
        files = {"sigma.ico": r"C:\x\sigma.ico", "sigma.png": "/x/sigma.png"}
        if os.name == "nt":
            root = Root()
            with mock.patch.object(app, "icon_file", files.get):
                app._set_icon(root, fake_tk)
            self.assertEqual(root.calls, [("iconbitmap", r"C:\x\sigma.ico")])
        root = Root()
        with mock.patch.object(app, "icon_file", lambda n: files.get(n) if n.endswith(".png") else None):
            app._set_icon(root, fake_tk)
        self.assertEqual(root.calls, [("iconphoto", True, ("foto", ("file",)))])
        root = Root()   # dentro do .pyz: o PNG vem de dentro do zip
        with mock.patch.object(app, "icon_file", lambda n: None), \
                mock.patch.object(app, "icon_data", lambda n: b"\x89PNG..." if n == "sigma.png" else None):
            app._set_icon(root, fake_tk)
        self.assertEqual(root.calls, [("iconphoto", True, ("foto", ("data",)))])
        root = Root()
        with mock.patch.object(app, "icon_file", lambda n: None), mock.patch.object(app, "icon_data", lambda n: None):
            app._set_icon(root, fake_tk)
        self.assertEqual(root.calls, [])                           # sem ícone: fica a pena, mas não quebra

    def test_abrir_painel_sem_token_na_linha_de_comando(self):
        ui = tmpdir("sgui")
        self.addCleanup(rmtree, ui)
        url = "http://127.0.0.1:47900/#t=TOKEN-SECRETO-123"
        launched = []
        with mock.patch.object(app, "find_browser", lambda: r"C:\navegador\msedge.exe"), \
                mock.patch.object(app.subprocess, "Popen", lambda argv, **kw: launched.append(argv)):
            self.assertTrue(ORIG_OPEN_WINDOW(url, ui))
        argv = launched[0]
        self.assertFalse(any("TOKEN-SECRETO" in a for a in argv), argv)
        target = next(a for a in argv if a.startswith("--app="))[len("--app="):]
        self.assertTrue(target.startswith("file:") and target.endswith(app.OPENER), target)
        page_path = os.path.join(ui, app.OPENER)
        with open(page_path, encoding="utf-8") as f:
            page = f.read()
        self.assertIn(json.dumps(url), page)
        self.assertIn("location.replace", page)
        if os.name != "nt":
            self.assertEqual(os.stat(page_path).st_mode & 0o777, 0o600)
        opened = []   # sem navegador de app: o padrão abre o mesmo arquivo, nunca a URL com o token
        with mock.patch.object(app, "find_browser", lambda: None), \
                mock.patch.object(app.webbrowser, "open", lambda u: opened.append(u) or True):
            ORIG_OPEN_WINDOW("http://127.0.0.1:47900/#t=OUTRO-TOKEN", ui)
        self.assertTrue(opened[0].startswith("file:") and "OUTRO-TOKEN" not in opened[0])
        with open(page_path, encoding="utf-8") as f:
            self.assertIn("OUTRO-TOKEN", f.read())                 # regravado a cada abertura

    def test_licencas(self):
        for lic in ("", None, "other", "unknown", "  Other "):
            self.assertEqual(catalog.license_error(lic), catalog.LICENSE_UNDECLARED)
            self.assertFalse(catalog.commercial_ok(lic))
        for lic in ("cc-by-nc-4.0", "CC-BY-NC-SA-4.0", "research-only", "non-commercial"):
            self.assertIn("não permite uso comercial", catalog.license_error(lic))
        for lic in ("Apache-2.0", "MIT", "Llama-3.1", "Gemma"):
            self.assertIsNone(catalog.license_error(lic))
            self.assertTrue(catalog.commercial_ok(lic))
        self.assertTrue(all(catalog.commercial_ok(i["license"]) for i in catalog.CATALOG))

    def test_csp_sem_terceiros(self):
        for csp in (app.PAGE_CSP, app.API_CSP):
            self.assertNotIn("http", csp)
            self.assertNotIn("google", csp)
        self.assertIn("connect-src 'self'", app.PAGE_CSP)


# ===================================================== uma instância por pasta
# Versão de um Sigma "antigo" nos testes de troca de instância: a lógica de claim_instance só compara a ORDEM das
# versões, então basta ser menor que a versão do pacote (hoje 2.1.0; vira 2.2.0 quando sigma/__init__.py subir).
OLD_VERSION = "2.0.0"


class FakeOldSigma:
    """Um Sigma mais antigo no ar (só /api/ping e /api/quit), para testar a troca de versão.

    `auth=True` responde à prova HMAC do token em /api/ping?nonce=… (como a instância anterior de verdade faz);
    `auth=False` ignora o nonce (um impostor que ocupou a porta, ou uma versão sem o desafio). `on_quit` é chamado
    quando ele de fato desliga (ex.: soltar a app.lock que outro processo segurava)."""

    def __init__(self, port, instance, token, version="2.0.0", quits=True, auth=False, on_quit=None):
        self.instance, self.token, self.version, self.quits = instance, token, version, quits
        self.auth, self.on_quit = auth, on_quit
        self.quit_headers, self.stopped = [], False
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/api/ping":
                    info = {"app": "sigma", "version": outer.version, "instance": outer.instance}
                    nonce = (parse_qs(u.query).get("nonce") or [""])[0]
                    if outer.auth and nonce:
                        info["auth"] = app._ping_auth(outer.token, nonce)
                    return self._send(200, info)
                self._send(404, {"error": "não"})

            def do_POST(self):
                if self.path != "/api/quit":
                    return self._send(404, {"error": "não"})
                outer.quit_headers.append({k.lower(): v for k, v in self.headers.items()})
                if self.headers.get("X-Sigma-Token") != outer.token:
                    return self._send(401, {"error": "token"})
                self._send(200, {"ok": True})
                if outer.quits:
                    threading.Thread(target=outer.stop, daemon=True).start()
                    if outer.on_quit:
                        outer.on_quit()

        self.srv = serve(H, port)

    def stop(self):
        if not self.stopped:
            self.stopped = True
            self.srv.shutdown()
            self.srv.server_close()


class InstanceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir("sginst")
        self.addCleanup(rmtree, self.dir)
        FATAL.clear()
        OPENED.clear()
        p = mock.patch.object(app.App, "_poll_loop", lambda self: None)
        p.start()
        self.addCleanup(p.stop)
        out = contextlib.redirect_stdout(io.StringIO())
        self.out = out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

    def test_trava_por_pasta(self):
        a, b = new_app(self.dir), new_app(self.dir)
        self.assertTrue(a.take_lock())
        self.assertFalse(b.take_lock())                        # mesma pasta: a segunda não pega
        other_dir = tmpdir("sginst2")
        self.addCleanup(rmtree, other_dir)
        other = new_app(other_dir)
        self.assertTrue(other.take_lock())                     # outra pasta: outro app
        other.release_lock()
        a.release_lock()
        self.assertTrue(b.take_lock())
        b.release_lock()

    def test_segunda_abertura_so_mostra_a_janela_da_primeira(self):
        port = free_port()
        first = new_app(self.dir, port)
        self.assertIsNone(first.claim_instance(gui=False))
        first.boot()
        self.addCleanup(first.shutdown)
        second = new_app(self.dir, port)
        self.assertEqual(second.claim_instance(gui=True), 0)
        self.assertEqual(OPENED, [(f"http://127.0.0.1:{port}/#t={first.token}", second.ui_dir)])
        self.assertIsNone(second._lock_fd)
        OPENED.clear()
        bg = new_app(self.dir, port, extra=["--background"])
        self.assertEqual(bg.claim_instance(gui=True), 0)
        self.assertEqual(OPENED, [])                           # --background: não abre janela
        self.assertEqual(FATAL, [])

    def test_outra_ainda_subindo_espera_e_mostra(self):
        port = free_port()
        first = new_app(self.dir, port)
        self.assertTrue(first.take_lock())                     # pegou a pasta e ainda está subindo…
        self.addCleanup(first.shutdown)
        timer = threading.Timer(3.0, first.boot)   # no Windows, porta fechada leva ~2 s para recusar
        timer.start()
        self.addCleanup(timer.join)
        second = new_app(self.dir, port)
        self.assertEqual(second.claim_instance(gui=False), 0)
        self.assertIn("O Sigma já está abrindo; a janela aparece em instantes.", self.out.getvalue())
        self.assertEqual(FATAL, [])

    def test_outra_travada_que_nunca_responde(self):
        first = new_app(self.dir, DEAD_PORT)
        self.assertTrue(first.take_lock())
        self.addCleanup(first.release_lock)
        second = new_app(self.dir, DEAD_PORT)
        with mock.patch.object(app, "LOCK_WAIT", 1.0):
            self.assertEqual(second.claim_instance(gui=False), 2)
        self.assertEqual(FATAL, ["O Sigma já está aberto, mas não respondeu. Feche-o pelo Gerenciador de Tarefas e "
                                 "abra de novo."])

    def _old(self, quits=True, auth=True):
        """Antigo que segura a app.lock (um holder) e responde /api/ping+/api/quit (um FakeOldSigma com a prova
        HMAC): só assim o novo confia nele. Ao desligar, o antigo solta a trava (on_quit) para o novo assumir."""
        old_port, new_port = free_port(), free_port()
        probe = new_app(self.dir, new_port)
        token = "token-do-antigo"
        holder = new_app(self.dir, old_port)                   # segura a trava, como o Sigma antigo de verdade
        self.assertTrue(holder.take_lock())
        self.addCleanup(holder.release_lock)
        old = FakeOldSigma(old_port, probe.instance, token, version=OLD_VERSION, quits=quits, auth=auth,
                           on_quit=holder.release_lock)
        self.addCleanup(old.stop)
        app._write_json(probe.session_path, {"port": old_port, "token": token, "pid": os.getpid(),
                                             "version": OLD_VERSION})
        return old, probe, old_port, new_port

    def test_versao_nova_fecha_a_antiga_e_assume(self):
        old, new, old_port, new_port = self._old()
        self.assertIsNone(new.claim_instance(gui=False))       # trava em outro processo: autentica, fecha e assume
        self.assertTrue(old.stopped)
        h = old.quit_headers[0]
        self.assertEqual((h.get("x-sigma-token"), h.get("host")), ("token-do-antigo", f"127.0.0.1:{old_port}"))
        self.assertIsNone(h.get("origin"))
        self.assertIsNotNone(new._lock_fd)
        new.boot()
        self.addCleanup(new.shutdown)
        sess = app._read_json(new.session_path)
        self.assertEqual((sess["port"], sess["token"], sess["version"]), (new_port, new.token, __version__))
        self.assertEqual(call(new_port, "GET", "/api/ping")[1]["version"], __version__)
        self.assertIn(f"um Sigma mais antigo ({OLD_VERSION})", self.out.getvalue())

    def test_versao_antiga_que_nao_fecha(self):
        old, new, _, _ = self._old(quits=False)
        with mock.patch.object(app, "QUIT_WAIT", 1.5):
            self.assertEqual(new.claim_instance(gui=False), 2)
        self.assertEqual(FATAL, ["O Sigma antigo não fechou. Clique em Desligar na janelinha dele e abra este de "
                                 "novo."])
        self.assertIsNone(new._lock_fd)                        # não fica com a pasta presa

    def test_impostor_na_porta_nao_recebe_o_token(self):
        """app-instance-impostor: com a app.lock na nossa mão (nenhum Sigma desta pasta roda), um impostor que só
        ocupou a porta e responde o ping é ignorado — não abrimos a janela dele nem mandamos o token."""
        port, new_port = free_port(), free_port()
        probe = new_app(self.dir, new_port)
        impostor = FakeOldSigma(port, probe.instance, token="qualquer", version="9.9.9", auth=False)
        self.addCleanup(impostor.stop)
        app._write_json(probe.session_path, {"port": port, "token": "token-da-vitima", "pid": os.getpid(),
                                             "version": "9.9.9"})
        self.assertIsNone(probe.claim_instance(gui=True))      # pegou a trava: ignora o impostor e sobe
        self.addCleanup(probe.release_lock)
        self.assertEqual(OPENED, [])                           # não abriu a janela do falso (nem com versão 9.9.9)
        self.assertEqual(impostor.quit_headers, [])            # não mandou /api/quit nem o token
        self.assertIsNotNone(probe._lock_fd)

    def test_quit_autentica_antes_de_mandar_o_token(self):
        """app-instance-impostor: quit_running não manda o token para quem não prova conhecer o token da pasta."""
        port = free_port()
        probe = new_app(self.dir, port)
        impostor = FakeOldSigma(port, probe.instance, token="o-que-o-impostor-quiser", version="9.9.9", auth=False)
        self.addCleanup(impostor.stop)
        found = {"port": port, "token": "token-real-da-pasta", "version": "9.9.9", "url": f"http://127.0.0.1:{port}/"}
        self.assertFalse(probe.quit_running(found))            # a prova HMAC falha: não desliga
        self.assertEqual(impostor.quit_headers, [])            # e o token nunca foi num /api/quit

    def test_dois_cliques_nao_mostram_erro_falso(self):
        """app-takeover-double-click-false-error: fechou o antigo, mas outro Sigma novo desta pasta já assumiu a
        trava; é só abrir a janela dele, não 'o Sigma antigo não fechou' (que desligaria o novo)."""
        old_port, winner_port, new_port = free_port(), free_port(), free_port()
        probe = new_app(self.dir, new_port)
        holder = new_app(self.dir, winner_port)                # o "vencedor" segura a app.lock desta pasta
        self.assertTrue(holder.take_lock())
        self.addCleanup(holder.release_lock)
        old = FakeOldSigma(old_port, probe.instance, "tok-antigo", version=OLD_VERSION, auth=True)
        self.addCleanup(old.stop)
        winner = FakeOldSigma(winner_port, probe.instance, "tok-novo", version=__version__)
        self.addCleanup(winner.stop)
        app._write_json(probe.session_path, {"port": old_port, "token": "tok-antigo", "pid": os.getpid(),
                                             "version": OLD_VERSION})

        def quit_and_point_to_winner(found):                   # o antigo fecha e o vencedor passa a responder
            old.stop()
            app._write_json(probe.session_path, {"port": winner_port, "token": "tok-novo", "pid": os.getpid(),
                                                 "version": __version__})
            return True
        with mock.patch.object(probe, "quit_running", quit_and_point_to_winner), \
                mock.patch.object(app, "LOCK_WAIT", 1.0):
            self.assertEqual(probe.claim_instance(gui=True), 0)
        self.assertEqual(FATAL, [])                            # nada de erro falso
        self.assertEqual(len(OPENED), 1)
        self.assertIn(f"127.0.0.1:{winner_port}", OPENED[0][0])
        self.assertIsNone(probe._lock_fd)

    def test_autostart_aponta_para_o_executavel_novo(self):
        a = new_app(self.dir)
        a.hw, a.machine = {"ram_mb": RAM_MB}, Wallet.generate()
        a._load_config()
        a.settings["start_with_system"] = True
        AUTOSTART["value"] = '"C:\\velho\\Sigma.exe" --background'
        try:
            with mock.patch.object(app, "autostart_supported", lambda: True):
                a._sync_autostart()
            self.assertEqual(AUTOSTART["value"], a.autostart_command())
        finally:
            AUTOSTART["value"] = None

    def test_autostart_com_apostrofo_no_caminho(self):
        """app-autostart-apostrophe: o comando do 'iniciar com o computador' funciona com apóstrofo no caminho
        (C:\\Users\\D'Avila): roda o .pyz direto, ou escapa o caminho no -c (não quebra com SyntaxError)."""
        a = new_app(self.dir)                                  # sem --port
        pyz = r"C:\Users\D'Avila\Downloads\sigma.pyz"
        with mock.patch.object(app, "__file__", os.path.join(pyz, "sigma", "app.py")):
            cmd = a.autostart_command()
        self.assertIn(f'"{pyz}"', cmd)                         # roda o .pyz direto
        self.assertNotIn(" -c ", cmd)
        self.assertIn("--background", cmd)
        src = r"C:\Users\D'Avila\projeto"                      # pasta de código com apóstrofo
        with mock.patch.object(app, "__file__", os.path.join(src, "sigma", "app.py")):
            cmd = a.autostart_command()
        m = re.search(r' -c "(.*?)" --background', cmd)
        self.assertIsNotNone(m, cmd)
        compile(m.group(1), "<autostart>", "exec")             # o bug era SyntaxError no pythonw: agora compila

    def test_desligar_apaga_o_que_leva_o_token(self):
        port = free_port()
        a = new_app(self.dir, port)
        self.assertIsNone(a.claim_instance(gui=False))
        a.boot()
        app.write_opener(a.opener_path, a.url)
        self.assertTrue(os.path.exists(a.opener_path) and os.path.exists(a.session_path))
        a.shutdown()
        self.assertFalse(os.path.exists(a.opener_path))       # abrir.html leva o token: não fica para trás
        self.assertFalse(os.path.exists(a.session_path))
        again = new_app(self.dir)
        self.assertTrue(again.take_lock())                     # desligou e soltou a pasta
        again.release_lock()

    def test_main_headless_sobe_e_desliga_pela_api(self):
        port = free_port()
        calls, box = [], {}
        real_install = app.netsafe.install_default_opener
        with mock.patch.object(app.netsafe, "install_default_opener", lambda: (calls.append(1), real_install())[1]):
            t = threading.Thread(target=lambda: box.setdefault("rc", app.main(
                ["--headless", "--data-dir", self.dir, "--port", str(port), "--node", NODE, "--no-mine"])),
                daemon=True)
            t.start()
            sess = wait_for(lambda: app._read_json(os.path.join(self.dir, "session.json")), what="o app subir")
            self.assertEqual(calls, [1])                       # HTTPS com os certificados certos antes de tudo
            code, data, _ = call(port, "GET", "/api/state", token=sess["token"])
            self.assertEqual(code, 200)
            self.assertTrue(data["setup_done"])                # headless cria a carteira sozinho
            self.assertEqual(call(port, "POST", "/api/quit", {}, token=sess["token"])[1], {"ok": True})
            t.join(60)
        self.assertEqual(box.get("rc"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "session.json")))
        again = new_app(self.dir)
        self.assertTrue(again.take_lock())
        again.release_lock()


if __name__ == "__main__":
    unittest.main()
