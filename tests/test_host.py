"""Testes do sigma.host.

    python tests/test_host.py

A) download com retomada (Range) e SHA-256, contra um http.server local na porta 8731;
B) a lógica da máquina contra uma chain falsa em memória (motor mock, minerador falso): registro (pub, preços,
   GB como o Windows), sinal por janela sem martelar o nó, modelo atribuído -> baixa -> confere o GGUF -> carrega
   -> pin_model, arquivos recusados (recusados.json), download que tenta entrar na rede interna, pedidos públicos
   e direcionados (cifrados, em fila, falhas "context"/"engine"/"unreadable"/"busy"), motor fora do ar por pouco
   tempo, computação, sites (tamanho declarado), arquivo do modelo guardado depois de perder a atribuição,
   limpeza por disk_gb, reinício e os laços de verdade;
C) o protocolo de verdade por HTTP: Client + Host contra tests/fake_node.py na porta 8832 (registro com pub,
   modelo atribuído, pedido cifrado direcionado, falhas, cancelamento).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from sigma import hardware, netsafe  # noqa: E402
from sigma import host as hostmod  # noqa: E402
from sigma.chain import canon  # noqa: E402
from sigma.client import Client, SigmaError  # noqa: E402
from sigma.crypto import Wallet, is_sealed, seal  # noqa: E402
from sigma.engines import CONTEXT, ContextOverflow, EngineError, RefusedFile, fetch, write_test_gguf  # noqa: E402

PORT = 8731
NODE_PORT = 8832
BASE = f"http://127.0.0.1:{PORT}"
SECRET = "segredo-do-cliente-7731"   # texto do pedido: não pode aparecer no log nem no disco da máquina


def check(cond, msg):
    print(("  ok   " if cond else "  FALHOU ") + msg, flush=True)
    if not cond:
        raise SystemExit(1)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def gguf_bytes(tmp, size):
    """Bytes de um GGUF mínimo e válido (o mesmo que os modelos falsos da demo usam)."""
    p = os.path.join(tmp, f"tmp-{os.urandom(4).hex()}.gguf")
    write_test_gguf(p, size)
    with open(p, "rb") as f:
        data = f.read()
    os.remove(p)
    return data


class Files(BaseHTTPRequestHandler):
    """/<modo>/<nome>: modo full (aceita Range), flaky (fecha no meio), norange (ignora Range),
    redir (302 para o endereço de metadados da nuvem, dentro da "rede interna")."""
    files, seen = {}, []

    def log_message(self, *a):
        pass

    def do_GET(self):
        mode, _, name = self.path.strip("/").partition("/")
        data = self.files.get(name)
        rng = self.headers.get("Range")
        self.seen.append((mode, name, rng))
        if mode == "redir":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start = 0
        if rng and mode != "norange":
            start = int(re.match(r"bytes=(\d+)-$", rng).group(1))
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if mode == "flaky":
            self.wfile.write(body[:len(body) // 2])
            self.close_connection = True
            return
        self.wfile.write(body)


def part_a(tmp):
    print("A) download com retomada e SHA-256 (porta 8731)")
    data = os.urandom(3_500_000)   # 3,5 MB: vários blocos de 1 MB
    Files.files["modelo.gguf"] = data
    good = sha(data)
    dest = os.path.join(tmp, "a", "modelo.gguf")
    seen = []
    fetch(f"{BASE}/full/modelo.gguf", dest, good, size=len(data), progress=lambda d, t: seen.append((d, t)))
    with open(dest, "rb") as f:
        check(f.read() == data and not os.path.exists(dest + ".part"), "download completo e conferido")
    check(seen[-1] == (len(data), len(data)) and all(a[0] < b[0] for a, b in zip(seen, seen[1:])),
          f"progresso crescente ({len(seen)} chamadas)")
    os.remove(dest)

    try:
        fetch(f"{BASE}/flaky/modelo.gguf", dest, good, size=len(data))
        check(False, "a conexão caída deveria falhar")
    except EngineError as e:
        have = os.path.getsize(dest + ".part")
        check(0 < have < len(data) and not os.path.exists(dest), f"queda no meio: guardou {have:,} bytes ({e})")
    Files.seen.clear()
    fetch(f"{BASE}/full/modelo.gguf", dest, good, size=len(data))
    check(Files.seen == [("full", "modelo.gguf", f"bytes={have}-")], f"retomou com Range: bytes={have}-")
    with open(dest, "rb") as f:
        check(f.read() == data, "arquivo retomado é idêntico")
    os.remove(dest)

    with open(dest + ".part", "wb") as f:
        f.write(data[:1000])
    fetch(f"{BASE}/norange/modelo.gguf", dest, good, size=len(data))
    with open(dest, "rb") as f:
        check(f.read() == data, "servidor que ignora Range: recomeça do zero e confere")
    os.remove(dest)

    with open(dest + ".part", "wb") as f:   # .part já completo e tamanho conhecido: nem pede nada
        f.write(data)
    Files.seen.clear()
    fetch(f"{BASE}/full/modelo.gguf", dest, good, size=len(data))
    check(not Files.seen and os.path.exists(dest), ".part completo: só confere, sem baixar de novo")
    os.remove(dest)
    with open(dest + ".part", "wb") as f:   # sem tamanho: o servidor responde 416 e o arquivo é conferido
        f.write(data)
    fetch(f"{BASE}/full/modelo.gguf", dest, good)
    check(os.path.exists(dest) and Files.seen[-1][2] == f"bytes={len(data)}-", "416 com .part completo: aceito")
    os.remove(dest)

    try:
        fetch(f"{BASE}/full/modelo.gguf", dest, "0" * 64, size=len(data))
        check(False, "sha errado deveria falhar")
    except RefusedFile as e:
        check(e.reason == "sha" and not os.path.exists(dest) and not os.path.exists(dest + ".part"),
              f"SHA-256 errado: apagado e marcado como arquivo recusado ({e})")
    try:
        fetch(f"{BASE}/full/modelo.gguf", dest, good, size=len(data) - 10)
        check(False, "arquivo maior que o anunciado deveria falhar")
    except RefusedFile as e:
        check(not os.path.exists(dest + ".part"), f"maior que o anunciado: descartado ({e})")
    try:
        fetch(f"{BASE}/full/modelo.gguf", dest, good, size=len(data), should_stop=lambda: True)
        check(False, "parar deveria interromper")
    except EngineError as e:
        check(not os.path.exists(dest), f"interrompido a pedido ({e})")
    try:
        fetch(f"{BASE}/full/nao-existe.gguf", dest, good, size=10)
        check(False, "404 deveria falhar")
    except EngineError as e:
        check("404" in str(e), f"404: {e}")
    try:
        fetch("file:///etc/passwd", dest, good)
        check(False, "file:// deveria ser recusado")
    except EngineError:
        check(True, "só http/https")
    Files.seen.clear()
    try:
        fetch(f"{BASE}/full/modelo.gguf", dest, good, size=len(data), opener=netsafe.safe_opener())
        check(False, "o opener seguro deveria recusar a rede interna")
    except EngineError as e:
        check("rede interna" in str(e) and not Files.seen, f"opener seguro: nem conecta na rede interna ({e})")


# ------------------------------------------------------------------ chain falsa em memória
class FakeChain:
    """O pedaço da chain/nó que a máquina usa, em memória, com o mesmo formato das rotas do nó."""

    def __init__(self):
        self.window, self.window_seconds = 10, 300
        self.machines, self.models, self.sites, self.jobs, self.blobs = {}, {}, {}, {}, {}
        self.txs = []
        self.models_down = False
        self.lock = threading.Lock()

    def unhost(self, addr):
        for m in self.models.values():
            m["assigned"].pop(addr, None)
            m["pinners"].pop(addr, None)
        if addr in self.machines:
            self.machines[addr]["models"] = []


def _copy(x):
    return json.loads(json.dumps(x))


class FakeClient:
    def __init__(self, chain, me):
        self.c, self.me, self.node = chain, me, "http://127.0.0.1:1"
        self.calls = {"mining": 0, "get_blob": 0, "models": 0}
        self.tx_waits = []   # (tipo, wait) de cada send_tx: os direcionados devem sair com wait=False

    def mining(self):
        self.calls["mining"] += 1
        return {"window": self.c.window, "window_seconds": self.c.window_seconds}

    def machines(self):
        return _copy(self.c.machines)

    def machine(self, addr):
        rec = self.c.machines.get(addr)
        if rec is None:
            raise SigmaError("máquina não registrada")
        out = _copy(rec)
        out["assignments"] = [{"name": n, "url": m["url"], "sha256": m["sha256"], "size": m["size"],
                               "ram_mb": 600, "budget": m.get("budget"), "rent_window": m.get("rent_window"),
                               "status": "pinned" if addr in m["pinners"] else "assigned"}
                              for n, m in sorted(self.c.models.items())
                              if addr in m["pinners"] or addr in m["assigned"]]
        out["sites"] = sorted(n for n, s in self.c.sites.items() if addr in s["pinners"])
        return out

    def models(self):
        self.calls["models"] += 1
        if self.c.models_down:
            raise SigmaError("não consegui falar com o nó http://127.0.0.1:1: tempo esgotado")
        return _copy(self.c.models)

    def sites(self):
        return _copy(self.c.sites)

    def jobs(self, status=None):
        return [_copy(j) for j in self.c.jobs.values() if not status or j["status"] == status]

    def job(self, jid):
        return _copy(self.c.jobs[jid])

    def get_blob(self, h):
        self.calls["get_blob"] += 1
        if h not in self.c.blobs:
            raise SigmaError("blob não encontrado")
        return self.c.blobs[h]

    def put_blob(self, data):
        self.c.blobs[sha(data)] = data
        return sha(data)

    def send_tx(self, type_, data, wait=True, timeout=30):
        c, me = self.c, self.me
        self.tx_waits.append((type_, wait))
        with c.lock:
            c.txs.append((type_, _copy(data)))
            rec = c.machines.get(me)
            if type_ == "register_machine":
                base = rec or {"since": c.window, "models": [], "jobs_done": 0, "jobs_failed": 0, "earned": 0,
                               "reputation": 0}
                c.machines[me] = dict(base, **data, last_seen=c.window, online=True)
                if not data["ai"]:   # chain._unhost: a máquina sai de todos os modelos
                    c.unhost(me)
            elif type_ == "ping":
                if rec["last_seen"] == c.window:
                    raise SigmaError("já sinalizou nesta janela")
                rec.update(last_seen=c.window, online=True)
            elif type_ == "pin_model":
                m = c.models[data["name"]]
                if me not in m["assigned"]:
                    raise SigmaError("modelo não atribuído a esta máquina")
                del m["assigned"][me]
                m["pinners"][me] = {"window": c.window, "misses": 0, "miss_window": -1}
                rec["models"].append(data["name"])
            elif type_ == "pin_site":
                c.sites[data["name"]]["pinners"][me] = {"window": c.window}
            elif type_ == "claim_job":
                j = c.jobs[data["job_id"]]
                if j.get("machine") not in (None, me):
                    raise SigmaError("este job é para outra máquina")
                if j["status"] != "open":
                    raise SigmaError(f"job não está aberto ({j['status']})")
                j["claims"][me] = {"time": time.time(), "price": 1}
                if len(j["claims"]) >= j["replicas"]:
                    j["status"] = "running"
            elif type_ == "submit_result":
                j = c.jobs[data["job_id"]]
                if me not in j["claims"] or me in j["results"]:
                    raise SigmaError("você não reivindicou este job (ou já respondeu)")
                failed = data.get("failed", False)
                usage = data.get("usage") or {}
                if failed and (j["kind"] != "ai" or any(usage.values())):
                    raise SigmaError("uma falha não cobra tokens: usage deve ser zero")
                j["results"][me] = {"hash": data["result_hash"], "usage": usage, "failed": failed}
                j["status"] = "failed" if failed else "done"
                rec["jobs_failed" if failed else "jobs_done"] += 1
            elif type_ == "decline_model":
                # o host avisa a rede; quem tira a atribuição e põe cooldown é a chain (testada à parte). Aqui só
                # registramos a tx (em c.txs), sem mexer na atribuição: o resto do teste de recusa segue valendo.
                if data["name"] not in c.models:
                    raise SigmaError("modelo não existe")
            else:
                raise SigmaError(f"tx desconhecida no teste: {type_}")
        return sha(canon([type_, data, len(c.txs)]))


class FakeMiner:
    def __init__(self, chain=None):
        self.calls, self._paused, self.chain = [], False, chain
        self.info_age = 0.0       # idade do /mining que o "minerador" buscou
        self.has_info = True

    def pause(self):
        self.calls.append("pause")
        self._paused = True

    def resume(self):
        self.calls.append("resume")
        self._paused = False

    def stats(self):
        return {"paused": self._paused}

    def last_mining(self):
        if not self.has_info or self.chain is None:
            return None, 0.0
        return {"window": self.chain.window, "window_seconds": self.chain.window_seconds}, time.time() - self.info_age


def txs_of(chain, type_):
    return [d for t, d in chain.txs if t == type_]


def ai_job(chain, jid, client, spec, me=None, model="tiny-model", sealed_to=None, escrow=10_000):
    """Job de IA no formato do nó. Com `me`, é direcionado a esta máquina e o pedido vai cifrado para ela."""
    blob = seal(sealed_to, spec) if sealed_to else spec
    chain.blobs[sha(blob)] = blob
    job = {"id": jid, "kind": "ai", "model": model, "client": client.address, "status": "open", "max_price": 5000,
           "escrow": escrow, "spec_hash": sha(blob), "claims": {}, "results": {}, "replicas": 1}
    if me:
        job.update(machine=me, client_pub=client.pub)
    chain.jobs[jid] = job
    return job


def spec_of(content, max_tokens=50, json_mode=False):
    return canon({"messages": [{"role": "user", "content": content}], "max_tokens": max_tokens, "json": json_mode})


def opened(chain, client, jid, me):
    """Resposta que a máquina gravou no nó, aberta com a chave de quem pediu."""
    blob = chain.blobs[chain.jobs[jid]["results"][me]["hash"]]
    return blob, json.loads(client.unseal(blob))


def part_b(tmp):
    print("B) lógica da máquina com chain falsa")
    chain = FakeChain()
    wallet = Wallet.generate()
    me = wallet.address
    payout = "0x" + "cd" * 20
    data_dir = os.path.join(tmp, "b")
    miner = FakeMiner(chain)
    logs = []
    settings = {"engine": "mock", "ai_enabled": True, "compute_enabled": True, "disk_gb": 5, "ai_ram_gb": 4}
    h = hostmod.Host("http://127.0.0.1:1", wallet, payout, data_dir, settings, miner=miner, log=logs.append)
    h.client = FakeClient(chain, me)
    os.makedirs(h.models_dir, exist_ok=True)
    os.makedirs(h.blob_dir, exist_ok=True)

    print("   registro")
    h._register_if_needed()
    reg = txs_of(chain, "register_machine")
    check(len(reg) == 1, "register_machine enviado")
    r = reg[0]
    check(r["payout"] == "sig" + "cd" * 20 and r["ai"] and r["compute"] and r["endpoint"] == "",
          "payout normalizado, IA e computação ligadas, sem endpoint público")
    check(r["pub"] == wallet.pub, "manda a chave pública da máquina (pedidos chegam cifrados para ela)")
    check(set(r) == {"payout", "name", "hw", "endpoint", "prices", "ai", "compute", "pub"},
          f"só os campos do contrato: {sorted(r)}")
    check(set(r["hw"]) == {"cpu_cores", "ram_mb", "ai_ram_mb", "vram_mb", "disk_mb", "gpu"} and
          r["hw"]["ai_ram_mb"] == int(4 * 1073.741824) == 4294 and r["hw"]["disk_mb"] <= int(5 * 1073.741824),
          f"GB como o Windows mostra (2^30 bytes): 4 GB = 4.294 MB ({r['hw']})")
    check(r["prices"] == {"ai_per_1k_tokens": 2000, "compute_per_unit": 500},
          "preços só de IA e computação (a hospedagem tem preço da rede)")
    check(r["name"].startswith("maquina-"), f"nome sem expor o nome do PC: {r['name']}")
    check(any("4 GB de memória para IA" in m for m in logs), "o registro fala em GB, como o Windows")
    h._register_if_needed()
    check(len(txs_of(chain, "register_machine")) == 1, "não registra de novo sem mudança")

    print("   sinal por janela sem martelar o nó")
    h._ping_if_needed()
    check(not txs_of(chain, "ping"), "sem ping na janela do registro")
    chain.window += 1
    h._refresh()
    h._ping_if_needed()
    h._ping_if_needed()
    check(len(txs_of(chain, "ping")) == 1, "um ping na janela seguinte, só um")
    check(h.client.calls["mining"] == 0, "usou o /mining que o minerador acabou de buscar (nenhuma consulta a mais)")
    miner.info_age = 15            # o do minerador está velho: o host consulta, mas no máximo a cada 30 s
    h._mining = (0.0, None)
    for _ in range(5):
        h._window()
    check(h.client.calls["mining"] == 1, "sem /mining recente do minerador: uma consulta e depois cache")
    h._mining = (time.time() - 31, h._mining[1])
    h._window()
    check(h.client.calls["mining"] == 2, "a cache vence em 30 s")
    check(hostmod.Host._mining_every({"window_seconds": 300}) == 30 and
          hostmod.Host._mining_every({"window_seconds": 6}) == 2,
          "janela de 5 min: a cada 30 s; rede de teste com janela curta: um terço da janela")
    saved = h._mining
    h._mining = (time.time(), {"window_seconds": 300})
    real_miner, h.miner = h.miner, None
    long_wait = h._loop_wait()
    h._mining = (time.time(), {"window_seconds": 3})
    short_wait = h._loop_wait()
    h.miner, h._mining = real_miner, saved
    check(long_wait == 5 and short_wait == 1,
          "volta do registro/ping: 5 s na rede de verdade, um terço da janela curta (não pula janela sem sinal)")
    miner.info_age = 0
    check(h._jobs_interval() == hostmod.JOBS_SLOW == 20, "sem modelo: jobs a cada 20 s")

    print("   modelo atribuído pela rede")
    gguf = gguf_bytes(tmp, 300_000)
    Files.files["tiny.gguf"] = gguf
    chain.models["tiny-model"] = {"name": "tiny-model", "url": f"{BASE}/full/tiny.gguf", "sha256": sha(gguf),
                                  "size": len(gguf), "assigned": {me: chain.window}, "pinners": {}}
    h._refresh()
    h._sync_models()
    path = os.path.join(h.models_dir, sha(gguf) + ".gguf")
    check(os.path.exists(path) and os.path.exists(path + ".ok"), "GGUF baixado em models/<sha256>.gguf e conferido")
    check(h.engine.loaded() == ["tiny-model"], "carregado no motor")
    check(("pin_model", {"name": "tiny-model"}) in chain.txs and me in chain.models["tiny-model"]["pinners"],
          "pin_model enviado depois de carregar")
    st = h.stats()
    check(st["models"] == [{"name": "tiny-model", "status": "hospedando", "progress": 1.0, "size": len(gguf),
                            "error": None}], f"stats: {st['models']}")
    check(any("SHA-256 e formato GGUF ok" in m for m in logs), "log: conferido o SHA-256 e o formato GGUF")
    check(h._jobs_interval() == hostmod.JOBS_FAST == 2, "hospedando modelo: jobs a cada 2 s")
    check("installing" in st["engine"] and "install_progress" in st["engine"],
          "status do motor tem installing e install_progress")

    print("   arquivos recusados")
    Files.seen.clear()
    chain.models["quebrado"] = {"name": "quebrado", "url": f"{BASE}/full/tiny.gguf", "sha256": "1" * 64,
                                "size": len(gguf), "assigned": {me: chain.window}, "pinners": {}}
    junk = b"GGUF" + os.urandom(200_000)   # o padrinho registrou o SHA-256 certo de um arquivo que não é GGUF
    Files.files["lixo.gguf"] = junk
    chain.models["lixo"] = {"name": "lixo", "url": f"{BASE}/full/lixo.gguf", "sha256": sha(junk), "size": len(junk),
                            "assigned": {me: chain.window}, "pinners": {}}
    h._refresh()
    h._sync_models()
    by = {m["name"]: m for m in h.stats()["models"]}
    check(by["quebrado"]["status"] == "recusado" and
          by["quebrado"]["error"] == "Modelo recusado: o arquivo não confere com o que o padrinho registrou",
          f"SHA-256 errado: {by['quebrado']['error']}")
    check(by["lixo"]["status"] == "recusado"
          and by["lixo"]["error"] == "Modelo recusado: o arquivo não é um GGUF válido",
          f"não é GGUF: {by['lixo']['error']}")
    check(("pin_model", {"name": "quebrado"}) not in chain.txs and ("pin_model", {"name": "lixo"}) not in chain.txs
          and "lixo" not in h.engine.loaded(), "modelo recusado não é carregado nem anunciado")
    check(not [f for f in os.listdir(h.models_dir) if f.startswith(sha(junk)) or f.startswith("1" * 64)],
          "os bytes recusados saem do disco")
    with open(os.path.join(h.models_dir, "recusados.json"), encoding="utf-8") as f:
        refused = json.load(f)
    check(set(refused) == {"1" * 64, sha(junk)} and refused[sha(junk)]["reason"] == "gguf"
          and abs(refused[sha(junk)]["until"] - time.time() - 24 * 3600) < 60, "anotados em recusados.json por 24 h")
    declined = {d["name"] for d in txs_of(chain, "decline_model")}
    check(declined == {"quebrado", "lixo"}
          and all(w is False for t, w in h.client.tx_waits if t == "decline_model"),
          f"recusou o arquivo na rede (decline_model, wait=False) para não perder reputação: {declined}")
    gets = len(Files.seen)
    for m in h._models.values():
        m["retry_at"] = 0.0
    h._sync_models()
    h2 = hostmod.Host("http://127.0.0.1:1", wallet, payout, data_dir, settings, miner=miner, log=logs.append)
    h2.client = FakeClient(chain, me)
    h2._refresh()
    h2._sync_models()
    check(len(Files.seen) == gets and {m["name"]: m["status"] for m in h2.stats()["models"]}["lixo"] == "recusado",
          "nas próximas 24 h não baixa de novo (nem depois de reiniciar o app)")
    h2.engine.shutdown()
    h._refused[sha(junk)]["until"] = time.time() - 1   # passaram as 24 h
    h._sync_models()
    by = {m["name"]: m for m in h.stats()["models"]}
    check(len(Files.seen) == gets + 1 and by["lixo"]["status"] == "recusado" and by["quebrado"]["status"] == "recusado",
          "depois de 24 h tenta de novo (e recusa de novo); o outro continua esperando")
    del chain.models["quebrado"], chain.models["lixo"]
    h._refresh()
    h._sync_models()

    print("   modelo sem orçamento: recusa na rede (decline_model) sem baixar GB")
    Files.seen.clear()
    pobre = gguf_bytes(tmp, 300_000)
    Files.files["pobre.gguf"] = pobre
    chain.models["pobre"] = {"name": "pobre", "url": f"{BASE}/full/pobre.gguf", "sha256": sha(pobre),
                             "size": hostmod.FUNDED_ABOVE + 1, "budget": 1, "rent_window": 1000,
                             "assigned": {me: chain.window}, "pinners": {}}
    h._refresh()
    h._sync_models()
    pm = next(m for m in h.stats()["models"] if m["name"] == "pobre")
    check(pm["status"] == "recusado" and "orçamento" in (pm["error"] or ""),
          f"modelo cujo orçamento não paga um dia fica em espera, sem baixar GB: {pm['error']}")
    check(not any(s[1] == "pobre.gguf" for s in Files.seen), "nem chegou a baixar o arquivo do modelo sem orçamento")
    check(("decline_model", {"name": "pobre"}) in chain.txs,
          "mandou decline_model: sai da fila da rede (cooldown) sem perder reputação por não dar pin")
    del chain.models["pobre"]
    h._refresh()
    h._sync_models()

    print("   download que tenta entrar na rede interna")
    chain.models["ssrf"] = {"name": "ssrf", "url": f"{BASE}/redir/x.gguf", "sha256": "2" * 64, "size": 1000,
                            "assigned": {me: chain.window}, "pinners": {}}
    h._node_internal = lambda: False   # agora o nó é "público" (como na rede de verdade)
    real_blocked = netsafe.blocked
    try:
        Files.seen.clear()
        h._refresh()
        h._sync_models()
        bad = next(m for m in h.stats()["models"] if m["name"] == "ssrf")
        check(bad["status"] == "erro" and bad["error"] == "a URL do modelo aponta para a rede interna; recusada"
              and not Files.seen, "nó público e URL na rede interna: recusada sem conectar")
        # o "site do modelo" parece público e redireciona para o endereço de metadados da nuvem
        netsafe.blocked = lambda ip: False if str(ip).split("%")[0] == "127.0.0.1" else real_blocked(ip)
        h._models["ssrf"]["retry_at"] = 0.0
        h._sync_models()
        bad = next(m for m in h.stats()["models"] if m["name"] == "ssrf")
        check(bad["status"] == "erro" and "rede interna" in bad["error"] and [s[0] for s in Files.seen] == ["redir"],
              f"redirect para 169.254.169.254 recusado antes de conectar ({bad['error']})")
    finally:
        netsafe.blocked = real_blocked
        del h._node_internal
    del chain.models["ssrf"]
    h._refresh()
    h._sync_models()

    print("   pedido público (sem máquina escolhida, como na v2)")
    client = Wallet.generate()
    ai_job(chain, "j1", client, spec_of("oi rede"))
    h._refresh()
    h._work_jobs()
    h._drain_queue()
    j = chain.jobs["j1"]
    check(j["status"] == "done" and me in j["results"], "job de IA respondido")
    res = json.loads(chain.blobs[j["results"][me]["hash"]])
    check("sigma-mock" in res["text"] and res["machine"] == me and res["model"] == "tiny-model",
          f"resultado: {res['text'][:50]}…")
    check(j["results"][me]["usage"]["tokens_out"] > 0, f"uso: {j['results'][me]['usage']}")
    check(miner.calls == ["pause", "resume"], "minerador pausado durante a inferência e retomado depois")
    miner.pause()
    miner.calls.clear()
    ai_job(chain, "j1b", client, spec_of("de novo"))
    h._work_jobs()
    h._drain_queue()
    check(miner.calls == [] and miner.stats()["paused"] and chain.jobs["j1b"]["status"] == "done",
          "pausa feita pelo usuário não é desfeita pelo host")
    miner.resume()

    print("   pedido direcionado e cifrado")
    logs.clear()
    ai_job(chain, "t1", client, spec_of(f"me diga o {SECRET}"), me=me, sealed_to=wallet.pub)
    check(SECRET.encode() not in chain.blobs[chain.jobs["t1"]["spec_hash"]], "o pedido no nó está cifrado")
    h._work_jobs()
    check(me in chain.jobs["t1"]["claims"] and chain.jobs["t1"]["status"] == "running",
          "reivindicado na hora (antes de responder)")
    h._drain_queue()
    blob, res = opened(chain, client, "t1", me)
    check(chain.jobs["t1"]["status"] == "done" and is_sealed(blob) and SECRET.encode() not in blob,
          "resposta cifrada para a chave de quem perguntou")
    check(SECRET in res["text"] and res["machine"] == me and set(res) == {"text", "model", "machine"},
          "quem perguntou abre a resposta com a carteira dele")
    check(not any(SECRET in m or "sigma-mock" in m for m in logs), "o registro só tem tamanhos, tokens e tempo")
    leaked = []
    for root, _, files in os.walk(data_dir):
        for fn in files:
            with open(os.path.join(root, fn), "rb") as f:
                if SECRET.encode() in f.read():
                    leaked.append(fn)
    check(not leaked, f"nada do pedido ou da resposta foi gravado no disco da máquina {leaked}")

    other = Wallet.generate()
    ai_job(chain, "t2", client, spec_of("para outra"), me=other.address, sealed_to=other.pub)
    h._work_jobs()
    check(not chain.jobs["t2"]["claims"], "pedido direcionado a outra máquina: ignorado")
    del chain.jobs["t2"]

    def failure(jid):
        j = chain.jobs[jid]
        blob, res = opened(chain, client, jid, me)
        return j["status"], res.get("code"), res, j["results"][me]["usage"], is_sealed(blob)

    ai_job(chain, "t3", client, b"lixo", me=me, sealed_to=Wallet.generate().pub)   # cifrado para outra chave
    ai_job(chain, "t4", client, b"{nao e json", me=me, sealed_to=wallet.pub)
    ai_job(chain, "t5", client, b"isto nem esta cifrado", me=me)
    h._work_jobs()
    h._drain_answers()   # as falhas saem de uma thread de respostas (wait=False); nos testes, chamamos direto
    for jid in ("t3", "t4", "t5"):
        stt, code, res, usage, sealed = failure(jid)
        check(stt == "failed" and code == "unreadable" and not any(usage.values()) and sealed and res["text"] == ""
              and res["error"] and res["machine"] == me and res["model"] == "tiny-model",
              f"{jid}: pedido que não abre -> reivindicado e respondido 'unreadable' (cifrado, usage 0)")

    edge = "x" * (3 * (CONTEXT - 32 - 8 - 16 + 1))   # estimativa = CONTEXT - 31: um token além do limite
    ai_job(chain, "t6", client, spec_of(edge), me=me, sealed_to=wallet.pub)
    ok_size = "y" * (3 * (CONTEXT - 32 - 8 - 16))     # estimativa = CONTEXT - 32: ainda cabe
    ai_job(chain, "t7", client, spec_of(ok_size, max_tokens=4000), me=me, sealed_to=wallet.pub)
    seen_max = []
    real_chat = h.engine.chat
    h.engine.chat = lambda n, msgs, mt, json_mode=False, timeout=None: (seen_max.append(mt), real_chat(n, msgs, mt))[1]
    h._work_jobs()
    h._drain_answers()   # t6 falha "context" na hora do claim (thread de respostas)
    h._drain_queue()
    stt, code, res, usage, sealed = failure("t6")
    check(stt == "failed" and code == "context" and "4.096 tokens" in res["error"],
          f"conversa maior que o contexto (mesma conta do cliente): 'context' na hora ({res['error']})")
    check(chain.jobs["t7"]["status"] == "done" and seen_max == [32],
          f"max_tokens = min(pedido, CONTEXT - estimativa): {seen_max}")

    print("   escrow precisa cobrir o preço (senão não trabalha de graça)")
    ai_job(chain, "e1", client, spec_of("de graça?"), me=me, sealed_to=wallet.pub, escrow=1)
    h._work_jobs()
    h._drain_answers()
    stt, code, res, usage, sealed = failure("e1")
    check(stt == "failed" and code == "busy" and "valor reservado" in res["error"] and not any(usage.values()),
          f"escrow de 1 µSIG não paga a resposta: volta 'busy' na hora ({res['error']})")
    seen_e = []
    h.engine.chat = lambda n, msgs, mt, json_mode=False, timeout=None: (seen_e.append(mt), real_chat(n, msgs, mt))[1]
    # escrow de 400 µSIG, preço 2000/1k, entrada "curto" ~25 tokens -> paga 200-25 = 175 tokens de resposta
    ai_job(chain, "e2", client, spec_of("curto", max_tokens=4000), me=me, sealed_to=wallet.pub, escrow=400)
    h._work_jobs()
    h._drain_queue()
    check(chain.jobs["e2"]["status"] == "done" and seen_e == [175],
          f"max_tokens limitado ao que o escrow paga (escrow*1000//preço - entrada): {seen_e}")
    h.engine.chat = real_chat

    def broken(*a, **k):
        raise EngineError("o motor de IA desta máquina não respondeu")
    h.engine.chat = broken
    ai_job(chain, "t8", client, spec_of("vai falhar"), me=me, sealed_to=wallet.pub)
    h._work_jobs()
    h._drain_queue()
    stt, code, res, usage, sealed = failure("t8")
    check(stt == "failed" and code == "engine" and "motor de IA" in res["error"],
          f"motor realmente fora: 'engine' ({res['error']})")

    def ctx_overflow(*a, **k):   # o motor recusou por estourar o contexto (HTTP 400 do llama-server)
        raise ContextOverflow("o llama-server recusou (400): the request (13305 tokens) exceeds the available "
                              "context size (4096 tokens)")
    h.engine.chat = ctx_overflow
    ai_job(chain, "t8c", client, spec_of("texto denso que o motor recusa"), me=me, sealed_to=wallet.pub)
    h._work_jobs()
    h._drain_queue()
    stt, code, res, usage, sealed = failure("t8c")
    check(stt == "failed" and code == "context" and "4.096 tokens" in res["error"],
          f"estouro de contexto do motor vira 'context' (divida o texto), não 'engine': {res['error']}")
    h.engine.chat = real_chat

    print("   fila: reivindica tudo na hora, responde em ordem, 'busy' com 3 ou mais na fila")
    h._current = {"job": {"id": "gerando"}}   # finge que está gerando outra resposta agora
    for k in range(4):
        ai_job(chain, f"q{k}", client, spec_of(f"pergunta {k}"), me=me, sealed_to=wallet.pub)
    h.client.tx_waits.clear()
    h._work_jobs()
    check(all(me in chain.jobs[f"q{k}"]["claims"] for k in range(4)), "os 4 reivindicados mesmo durante outra resposta")
    check([w for t, w in h.client.tx_waits if t == "claim_job"] == [False] * 4,
          "os direcionados são reivindicados com wait=False (o laço de jobs não trava esperando bloco)")
    h._drain_answers()
    stt, code, res, usage, sealed = failure("q2")
    check(stt == "failed" and code == "busy" and chain.jobs["q3"]["status"] == "failed",
          "com 3 na fila (contando o que está gerando), os novos voltam 'busy' na hora")
    check([w for t, w in h.client.tx_waits if t == "submit_result"] == [False, False],
          "as falhas 'busy' são enviadas com wait=False (de uma thread de respostas)")
    check(h.stats()["ai_queue"] == 3, f"stats: ai_queue = {h.stats()['ai_queue']}")
    h._current = None
    before = len(txs_of(chain, "submit_result"))
    h._drain_queue()
    answered = [d["job_id"] for d in txs_of(chain, "submit_result")[before:]]
    check(answered == ["q0", "q1"] and all(chain.jobs[f"q{k}"]["status"] == "done" for k in (0, 1)),
          f"respondidos na ordem em que chegaram: {answered}")

    print("   flood de direcionados: reivindica e responde sem travar (maq-targeted-flood-misses)")
    # Um atacante manda muitos pedidos direcionados de uma vez. Antes, a máquina os atendia em série com 2 txs
    # bloqueantes cada, não dava conta, os não reivindicados expiravam e viravam falta (perdia o modelo de graça).
    # Agora: reivindica TODOS numa passada (wait=False), responde MAX_QUEUE e devolve 'busy' o resto, sem travar.
    h._current = None
    h._drain_answers()
    for k in range(8):   # o mesmo cliente manda 8 direcionados de uma vez (cifrados para esta máquina)
        ai_job(chain, f"fl{k}", client, spec_of(f"flood {k}"), me=me, sealed_to=wallet.pub)
    h.client.tx_waits.clear()
    t0 = time.time()
    h._work_jobs()
    check(all(me in chain.jobs[f"fl{k}"]["claims"] for k in range(8)) and time.time() - t0 < 2,
          "todos os 8 reivindicados numa passada só (nenhum fica sem reivindicar para expirar e virar falta)")
    check([w for t, w in h.client.tx_waits if t == "claim_job"] == [False] * 8,
          "cada claim sai com wait=False (nonces em fila sob _tx_lock, sem esperar bloco)")
    h._drain_answers()
    busy = [k for k in range(8) if chain.jobs[f"fl{k}"]["status"] == "failed"]
    queued = [k for k in range(8) if chain.jobs[f"fl{k}"]["status"] == "running"]
    for jid in (f"fl{k}" for k in busy):
        stt, code, res, usage, sealed = failure(jid)
        check(code == "busy" and not any(usage.values()), f"{jid}: extra do mesmo cliente volta 'busy'")
    check(len(queued) == hostmod.MAX_QUEUE and len(busy) == 8 - hostmod.MAX_QUEUE,
          f"no máximo MAX_QUEUE na fila por cliente; os extras já voltaram 'busy' (fila={queued}, busy={busy})")
    check(all(w is False for t, w in h.client.tx_waits if t == "submit_result"),
          "as falhas do flood saem com wait=False (de uma thread de respostas, não travam o laço de jobs)")
    h._drain_queue()
    check(all(chain.jobs[f"fl{k}"]["status"] in ("done", "failed") for k in range(8)),
          "a fila é respondida e nada fica pendente: nenhum pedido expira")

    print("   motor de IA fora do ar por pouco tempo (Ollama reiniciando)")
    logs.clear()
    regs = len(txs_of(chain, "register_machine"))
    h.engine.available = lambda: False
    for _ in range(3):
        h._register_if_needed()
    check(len(txs_of(chain, "register_machine")) == regs, "continua registrada com IA (nenhum register_machine novo)")
    check(len([m for m in logs if "não respondeu; mantendo os modelos e tentando de novo" in m]) == 1,
          "avisa uma vez só no log")
    check(h.stats()["engine_down"], "stats: engine_down")
    ai_job(chain, "p1", client, spec_of("público com motor fora"))
    ai_job(chain, "t9", client, spec_of("direcionado com motor fora"), me=me, sealed_to=wallet.pub)
    h._work_jobs()
    h._drain_answers()
    stt, code, res, usage, sealed = failure("t9")
    check(chain.jobs["p1"]["status"] == "open" and not chain.jobs["p1"]["claims"],
          "não pega pedido público enquanto o motor está fora")
    check(stt == "failed" and code == "engine", "o direcionado volta como falha 'engine' (não vira falta)")
    del chain.jobs["p1"]
    h._engine_down_since = time.time() - hostmod.ENGINE_GRACE - 1   # passaram os 120 s
    h._register_if_needed()
    last = txs_of(chain, "register_machine")[-1]
    check(len(txs_of(chain, "register_machine")) == regs + 1 and not last["ai"],
          "depois de 120 s fora: registra sem IA")
    h._refresh()
    h._sync_models()
    check(os.path.exists(path) and os.path.exists(path + ".ok") and h.stats()["models"] == [],
          "a rede tirou o modelo, mas o arquivo ficou guardado no disco")
    check(any("o arquivo fica guardado caso a rede o coloque aqui de novo" in m for m in logs), "log diz que guardou")
    del h.engine.available   # o motor voltou
    h._register_if_needed()
    check(txs_of(chain, "register_machine")[-1]["ai"] and h._engine_down_since is None
          and any("voltou a responder" in m for m in logs), "motor de volta: registra com IA de novo")
    Files.seen.clear()
    chain.models["tiny-model"]["assigned"][me] = chain.window   # a rede coloca o modelo aqui de novo
    h._refresh()
    h._sync_models()
    check(not Files.seen and me in chain.models["tiny-model"]["pinners"] and h.engine.loaded() == ["tiny-model"],
          "reatribuído: carrega do disco e anuncia em segundos, sem baixar de novo")
    h0 = hostmod.Host("http://127.0.0.1:1", Wallet.generate(), payout, os.path.join(tmp, "b0"), settings,
                      log=lambda m: None)
    h0.engine.available = lambda: False
    check(not h0._registration()["ai"], "primeiro registro desta execução com o motor fora: sem IA")

    print("   IA desligada nas configurações")
    h.update_settings({"ai_enabled": False})
    h._register_if_needed()
    check(not txs_of(chain, "register_machine")[-1]["ai"], "registra sem IA")
    h._refresh()
    h._sync_models()
    check(os.path.exists(path) and h.engine.loaded() == [], "descarregado do motor, arquivo guardado")
    h.update_settings({"ai_enabled": True})
    h._register_if_needed()
    chain.models["tiny-model"]["assigned"][me] = chain.window
    Files.seen.clear()
    h._refresh()
    h._sync_models()
    check(not Files.seen and h.stats()["models"][0]["status"] == "hospedando",
          "IA religada e o modelo voltou: sem baixar de novo")

    print("   job de computação (processo separado)")
    # primes n=100000 -> 2 unidades; preço da máquina 500/unidade; escrow precisa cobrir units × preço × réplicas
    chain.jobs["j2"] = {"id": "j2", "kind": "compute", "task": {"name": "primes", "params": {"n": 100000}},
                        "client": "sig" + "11" * 20, "status": "open", "max_price": 500, "escrow": 1000,
                        "claims": {}, "results": {}, "replicas": 1}
    h._work_jobs()
    h._compute.join(60)
    j = chain.jobs["j2"]
    check(j["status"] == "done" and json.loads(chain.blobs[j["results"][me]["hash"]]) == {"count": 9592},
          "primos até 100.000 = 9592")
    h.update_settings({"cheat": True})
    chain.jobs["j3"] = dict(chain.jobs["j2"], id="j3", status="open", claims={}, results={})
    h._work_jobs()
    h._compute.join(60)
    check(json.loads(chain.blobs[chain.jobs["j3"]["results"][me]["hash"]]) == {"count": 9593},
          "configuração escondida 'cheat' altera o resultado (demo)")
    h.update_settings({"cheat": False})
    chain.jobs["j4"] = dict(chain.jobs["j2"], id="j4", status="open", claims={}, results={}, max_price=100)
    h._work_jobs()
    check(chain.jobs["j4"]["status"] == "open", "job abaixo do preço da máquina é ignorado")
    chain.jobs["j5"] = dict(chain.jobs["j2"], id="j5", status="open", claims={}, results={}, escrow=999)
    h._work_jobs()
    check(chain.jobs["j5"]["status"] == "open" and not chain.jobs["j5"]["claims"],
          "escrow que não cobre units × preço (999 < 2×500): a máquina não computa de graça (ignora)")

    print("   sites")
    page = b"<h1>ola</h1>"
    chain.blobs[sha(page)] = page
    chain.sites["ola-sigma"] = {"name": "ola-sigma", "status": "active", "version": 1, "replicas": 2,
                                "size": len(page), "pinners": {},
                                "manifest": {"index.html": {"hash": sha(page), "size": len(page)}}}
    chain.sites["grande"] = {"name": "grande", "status": "active", "version": 1, "replicas": 2,
                             "size": 6_000_000_000, "pinners": {}, "manifest": {}}
    lie = b"<h1>mais bytes do que o dono declarou</h1>"
    chain.blobs[sha(lie)] = lie
    chain.sites["mentiroso"] = {"name": "mentiroso", "status": "active", "version": 1, "replicas": 2, "size": 5,
                                "pinners": {}, "manifest": {"index.html": {"hash": sha(lie), "size": 5}}}
    open(os.path.join(h.blob_dir, "lixo"), "wb").close()
    logs.clear()
    h._pin_sites()
    check(me in chain.sites["ola-sigma"]["pinners"] and os.path.exists(os.path.join(h.blob_dir, sha(page))),
          "site pequeno hospedado (blobs baixados do nó)")
    check(me not in chain.sites["grande"]["pinners"], "site maior que o disco cedido é ignorado")
    check(me not in chain.sites["mentiroso"]["pinners"] and not os.path.exists(os.path.join(h.blob_dir, sha(lie)))
          and h._site_cooldown["mentiroso"] > time.time() + 590,
          "site com tamanho declarado falso: recusado, nada guardado, espera de 600 s")
    check(any(f"não hospedei o site 'mentiroso': o tamanho declarado de index.html não confere (5 ≠ {len(lie)} bytes)"
              in m for m in logs), "log explica o tamanho que não confere")
    check(not os.path.exists(os.path.join(h.blob_dir, "lixo")), "arquivos que não são de sites hospedados saem")
    big = os.urandom(3000)
    chain.blobs[sha(big)] = big
    chain.sites["sem-espaco"] = {"name": "sem-espaco", "status": "active", "version": 1, "replicas": 2,
                                 "size": len(big), "pinners": {},
                                 "manifest": {"a.bin": {"hash": sha(big), "size": len(big)}}}
    real_free = hostmod.hardware.disk_free_mb
    hostmod.hardware.disk_free_mb = lambda p: 0
    try:
        gets = h.client.calls["get_blob"]
        h._pin_sites()
    finally:
        hostmod.hardware.disk_free_mb = real_free
    check(me not in chain.sites["sem-espaco"]["pinners"] and h.client.calls["get_blob"] == gets,
          "espaço livre conferido pelos tamanhos declarados ANTES de baixar")
    del chain.sites["sem-espaco"], chain.sites["mentiroso"]

    print("   reinício: recarrega o que já hospeda")
    h.engine.shutdown()
    pins = len(txs_of(chain, "pin_model"))
    h2 = hostmod.Host("http://127.0.0.1:1", wallet, payout, data_dir, settings, miner=miner, log=logs.append)
    h2.client = FakeClient(chain, me)
    Files.seen.clear()
    h2._register_if_needed()
    ai_job(chain, "r1", client, spec_of("logo depois de ligar"), me=me, sealed_to=wallet.pub)
    h2._work_jobs()   # o pedido chega antes do laço dos modelos passar pelo modelo
    check(me in chain.jobs["r1"]["claims"] and chain.jobs["r1"]["status"] == "running" and h2._queue_size() == 1,
          "pedido que chega logo depois de ligar espera o modelo carregar (não volta como falha)")
    h2._sync_models()
    check(h2.engine.loaded() == ["tiny-model"] and not Files.seen, "recarregado do disco sem baixar de novo")
    h2._drain_queue()
    check(chain.jobs["r1"]["status"] == "done", "e é respondido assim que o modelo fica pronto")
    check([m["status"] for m in h2.stats()["models"]] == ["hospedando"], "status hospedando")
    check(len(txs_of(chain, "pin_model")) == pins, "sem pin_model repetido")

    print("   arquivo guardado: reinício sem atribuição, nó fora, modelo que sai da rede")
    chain.unhost(me)   # PC desligado por um tempo: a rede tirou o modelo daqui
    h2.engine.shutdown()
    h3 = hostmod.Host("http://127.0.0.1:1", wallet, payout, data_dir, settings, miner=miner, log=logs.append)
    h3.client = FakeClient(chain, me)
    h3._register_if_needed()
    h3._sync_models()
    check(os.path.exists(path) and os.path.exists(path + ".ok"),
          "ao ligar sem a atribuição: o arquivo fica (o modelo ainda existe na rede)")
    chain.models_down = True
    with open(os.path.join(h3.models_dir, "f" * 64 + ".gguf"), "wb") as f:
        f.write(b"orfao")
    h3._network = (0.0, None)
    h3._sync_models()
    check(os.path.exists(os.path.join(h3.models_dir, "f" * 64 + ".gguf")) and os.path.exists(path),
          "o /models não responde: nada sai do disco")
    chain.models_down = False
    del chain.models["tiny-model"]
    h3._network = (0.0, None)
    h3._sync_models()
    check(not os.path.exists(path) and not os.path.exists(path + ".ok")
          and not os.path.exists(os.path.join(h3.models_dir, "f" * 64 + ".gguf")),
          "modelo que saiu da rede: arquivo apagado na hora")
    check(h3.stats()["models"] == [], "stats sem modelos")

    print("   laços de verdade (start/stop)")
    with open(os.path.join(h3.models_dir, "e" * 64 + ".gguf"), "wb") as f:
        f.write(b"orfao")
    h4 = hostmod.Host("http://127.0.0.1:1", wallet, payout, data_dir, dict(settings, name="minha-maquina"),
                      miner=miner, log=logs.append)
    h4.client = FakeClient(chain, me)
    h4.start()
    deadline = time.time() + 8
    while time.time() < deadline and not h4.stats()["registered"]:
        time.sleep(0.2)
    st = h4.stats()
    check(st["registered"] and st["online"] and st["machine"] == me, "registrada pelos laços")
    check(chain.machines[me]["name"] == "minha-maquina", "nome configurado")
    check(st["engine"]["kind"] == "mock" and st["engine"]["ready"], f"motor: {st['engine']}")
    check(st["sites"] == ["ola-sigma"] and st["jobs_done"] >= 3, f"sites={st['sites']} jobs={st['jobs_done']}")
    deadline = time.time() + 5
    while time.time() < deadline and os.path.exists(os.path.join(h4.models_dir, "e" * 64 + ".gguf")):
        time.sleep(0.2)
    check(not os.path.exists(os.path.join(h4.models_dir, "e" * 64 + ".gguf")),
          "ao ligar, arquivos de modelos que não existem na rede são apagados")
    t = time.time()
    h4.stop()
    check(time.time() - t < 12 and not any(th.is_alive() for th in threading.enumerate()
                                           if th.name.startswith("sigma-maquina")), "stop encerra as threads")


def part_b_disk(tmp):
    print("B2) limpeza por disk_gb: sai primeiro o arquivo parado há mais tempo")
    chain = FakeChain()
    wallet = Wallet.generate()
    me = wallet.address
    budget_gb = 3.5 / hardware.MB_PER_GB   # 3 MB (disk_gb em GB de 2^30 bytes)
    settings = {"engine": "mock", "ai_enabled": True, "compute_enabled": False, "disk_gb": budget_gb, "ai_ram_gb": 4}
    logs = []
    h = hostmod.Host("http://127.0.0.1:1", wallet, "0x" + "cd" * 20, os.path.join(tmp, "disk"), settings,
                     miner=FakeMiner(chain), log=logs.append)
    h.client = FakeClient(chain, me)
    os.makedirs(h.models_dir, exist_ok=True)
    os.makedirs(h.blob_dir, exist_ok=True)
    h._register_if_needed()
    files = {}
    now = time.time()
    for k, name in enumerate(("velho", "medio", "novo")):
        data = gguf_bytes(tmp, 1_200_000)
        p = os.path.join(h.models_dir, sha(data) + ".gguf")
        with open(p, "wb") as f:
            f.write(data)
        h._mark_verified(p)
        os.utime(p + ".ok", (now - 3000 + k * 1000, now - 3000 + k * 1000))
        os.utime(p, (now - 3000 + k * 1000, now - 3000 + k * 1000))
        files[name] = p
        chain.models[name] = {"name": name, "url": f"{BASE}/full/{name}.gguf", "sha256": sha(data), "size": len(data),
                              "assigned": {}, "pinners": {}}   # existem na rede, mas não estão atribuídos aqui
    h._refresh()
    h._sync_models()
    left = {n for n, p in files.items() if os.path.exists(p)}
    check(left == {"medio", "novo"}, f"3,6 MB guardados num limite de 3 MB: saiu o mais antigo ({sorted(left)})")
    check(any("o parado há mais tempo" in m for m in logs), "log explica por que saiu")
    data = gguf_bytes(tmp, 1_200_000)
    Files.files["outro.gguf"] = data
    chain.models["outro"] = {"name": "outro", "url": f"{BASE}/full/outro.gguf", "sha256": sha(data), "size": len(data),
                             "assigned": {me: chain.window}, "pinners": {}}
    h._refresh()
    h._sync_models()
    left = {n for n, p in files.items() if os.path.exists(p)}
    check(left == {"novo"} and os.path.exists(os.path.join(h.models_dir, sha(data) + ".gguf"))
          and me in chain.models["outro"]["pinners"], "para caber o download novo, saiu o próximo mais antigo")
    h.engine.shutdown()


# ------------------------------------------------------------------ protocolo de verdade (HTTP)
def wait_until(cond, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return
        except (SigmaError, KeyError):
            pass
        time.sleep(0.3)
    check(False, f"esperando {what}")


def part_c(tmp):
    print(f"C) Client + Host contra o nó falso por HTTP (porta {NODE_PORT})")
    node_url = f"http://127.0.0.1:{NODE_PORT}"
    node = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_node.py"), "--port", str(NODE_PORT),
                             "--window", "3", "--min-bits", "4"])
    h = None
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(node_url + "/mining", timeout=2).read()
                break
            except OSError:
                time.sleep(0.1)
        machine = Wallet.generate()
        user = Wallet.generate()
        logs = []
        h = hostmod.Host(node_url, machine, user.address, os.path.join(tmp, "c"),
                         {"engine": "mock", "ai_ram_gb": 4, "disk_gb": 5, "compute_enabled": False}, log=logs.append)
        h.start()
        c = Client(node_url, user)
        wait_until(lambda: h.stats()["registered"], 15, "registro da máquina")
        reg = next(t for t in json.load(urllib.request.urlopen(node_url + "/txs", timeout=5))
                   if t["type"] == "register_machine")
        check(reg["data"]["pub"] == machine.pub
              and set(reg["data"]["prices"]) == {"ai_per_1k_tokens", "compute_per_unit"},
              "register_machine com pub e só os preços do contrato (o nó falso recusa campo a mais)")
        gguf = gguf_bytes(tmp, 400_000)
        Files.files["c.gguf"] = gguf
        req = urllib.request.Request(node_url + "/test/model", method="POST", data=json.dumps(
            {"name": "modelo-c", "url": f"{BASE}/full/c.gguf", "sha256": sha(gguf), "size": len(gguf),
             "assign": machine.address}).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        wait_until(lambda: any(a["status"] == "pinned" for a in c.machine(machine.address)["assignments"]), 40,
                   "o modelo baixado, conferido e anunciado")
        check(True, "modelo atribuído pela rede: baixado, conferido e anunciado")

        def ask(spec, sealed_to=machine.pub, max_tokens=512):
            h_spec = c.put_blob(seal(sealed_to, spec))
            return c.send_tx("create_job", {"kind": "ai", "model": "modelo-c", "spec_hash": h_spec, "escrow": 10_000,
                                            "max_price": 5000, "machine": machine.address, "client_pub": user.pub,
                                            "max_tokens": max_tokens})

        def final(jid):
            wait_until(lambda: c.job(jid)["status"] in ("done", "failed", "canceled", "expired"), 30,
                       f"o job {jid[:8]}")
            j = c.job(jid)
            return j, json.loads(user.unseal(c.get_blob(j["result_hash"])))

        t0 = time.time()
        j, res = final(ask(spec_of(f"olá, {SECRET}")))
        check(j["status"] == "done" and SECRET in res["text"] and res["machine"] == machine.address,
              f"pedido cifrado direcionado: respondido e aberto só por quem pediu ({time.time() - t0:.1f}s)")
        check(not any(SECRET in m for m in logs), "nada do pedido no registro da máquina")
        j, res = final(ask(b"lixo", sealed_to=Wallet.generate().pub))
        check(j["status"] == "failed" and res["code"] == "unreadable",
              "lixo cifrado para outra chave: falha 'unreadable'")
        j, res = final(ask(spec_of("z" * 13_000)))
        check(j["status"] == "failed" and res["code"] == "context", "conversa grande demais: falha 'context'")
        # outra máquina, que não responde: o cliente cancela antes de alguém pegar
        sleepy = Wallet.generate()
        cs = Client(node_url, sleepy)
        cs.send_tx("register_machine", {"payout": sleepy.address, "name": "dormindo", "ai": True, "compute": False,
                                        "endpoint": "", "prices": {"ai_per_1k_tokens": 2000, "compute_per_unit": 500},
                                        "hw": {"cpu_cores": 4, "ram_mb": 8000, "ai_ram_mb": 4000, "vram_mb": 0,
                                               "disk_mb": 9000, "gpu": ""}, "pub": sleepy.pub})
        req = urllib.request.Request(node_url + "/test/model", method="POST", data=json.dumps(
            {"name": "modelo-d", "url": f"{BASE}/full/c.gguf", "sha256": sha(gguf), "size": len(gguf),
             "assign": sleepy.address}).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        cs.send_tx("pin_model", {"name": "modelo-d"})
        h_spec = c.put_blob(seal(sleepy.pub, spec_of("alguém aí?")))
        jid = c.send_tx("create_job", {"kind": "ai", "model": "modelo-d", "spec_hash": h_spec, "escrow": 10_000,
                                       "max_price": 5000, "machine": sleepy.address, "client_pub": user.pub,
                                       "max_tokens": 512})
        time.sleep(3)   # a nossa máquina olha os jobs a cada 2 s: este não é para ela
        check(not c.job(jid)["claims"], "pedido para outra máquina: a nossa não reivindica")
        c.send_tx("cancel_job", {"job_id": jid})
        check(c.job(jid)["status"] == "canceled", "quem pediu cancela antes de alguém pegar")
    finally:
        if h:
            h.stop()
        node.terminate()
        node.wait(5)


def main():
    tmp = tempfile.mkdtemp(prefix="sg-host-")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        part_a(tmp)
        part_b(tmp)
        part_b_disk(tmp)
        part_c(tmp)
        print("TUDO OK")
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
