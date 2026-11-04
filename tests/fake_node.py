"""Nó falso para testar o minerador e a máquina: janelas por relógio e o protocolo da v2.1 em memória.

- Mineração (SPEC-v2.1 §5): GET /mining (com hosting_per_mb_window e ai_default_price) e o relatório `work`
  com `worker` opcional, conferido como a chain: identidade `miner` ou `miner.worker`, sigma.pow.share_ok(seed,
  identidade, nonce, bits) e "melhor relatório vale" (só entra quem tem mais pontos que o anterior daquela
  identidade na janela; os pontos vão sempre para `miner`).
- Máquinas e IA privada (§1, §6, §7): register_machine (com pub), ping, pin_model, create_job de IA (direcionado,
  com machine e client_pub, e o pedido já no /blob), claim_job (só a máquina escolhida), submit_result (com
  failed), cancel_job, /blob. As transações assinadas entram na hora (sem bloco); a assinatura não é conferida
  (é um nó falso): quem manda é address_of(pub), e o nonce tem de ser o próximo.

    python tests/fake_node.py --port 8730 --window 3 --min-bits 4

Rotas extras de teste: GET /reports (relatórios aceitos), GET /rejected, GET /txs (transações assinadas aceitas);
POST /fail?seconds=N (503 sem JSON em tudo por N segundos, como o nginx com o nó fora), POST /limit?n=K (os
próximos K relatórios recebem 429, o limite por conexão), POST /reject?n=K (os próximos K relatórios recebem 400),
POST /test/model {name, url, sha256, size, assign} (modelo na rede, atribuído a uma máquina).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sigma.crypto import address_of, norm_address  # noqa: E402

try:
    from sigma.pow import report_points, share_ok
except ImportError:          # módulo de PoW ainda não existe: aceita sem conferir os hashes
    share_ok = None

    def report_points(n, bits):
        return n << bits

HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
HEX128 = re.compile(r"^[0-9a-f]{128}\Z")
WORKER = re.compile(r"^[0-9a-f]{1,16}\Z")
DEFAULT_PRICES = {"ai_per_1k_tokens": 2000, "compute_per_unit": 500}
CONTEXT = 4096   # contexto dos modelos (SPEC-v2.2 §2: max_tokens de 1 a CONTEXT)
FIELDS = {   # SPEC-v2.2 §6.1: qualquer outro campo é recusado
    "work": {"miner", "window", "bits", "nonces", "worker"},
    "register_machine": {"payout", "name", "hw", "endpoint", "prices", "ai", "compute", "pub"},
    "ping": set(),
    "pin_model": {"name"},
    "create_job": {"kind", "model", "spec_hash", "escrow", "max_price", "machine", "client_pub", "max_tokens"},
    "claim_job": {"job_id"},
    "submit_result": {"job_id", "result_hash", "usage", "failed"},
    "cancel_job": {"job_id"},
    "decline_model": {"name"},   # SPEC-v2.2 §3: a máquina recusa o arquivo (sai a atribuição + cooldown, sem punição)
}
HW_KEYS = {"cpu_cores", "ram_mb", "ai_ram_mb", "vram_mb", "disk_mb", "gpu"}


class TxError(ValueError):
    pass


def need(cond, msg):
    if not cond:
        raise TxError(msg)


def only(d, allowed, where):
    extra = sorted(set(d) - set(allowed)) if isinstance(d, dict) else []
    need(not extra, f"campos desconhecidos {where}: {', '.join(extra)}")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class FakeChain:
    def __init__(self, window_seconds, min_bits, max_shares):
        self.W, self.min_bits, self.max_shares = window_seconds, min_bits, max_shares
        self.t0 = time.time()
        self.salt = os.urandom(8).hex()
        self.lock = threading.RLock()
        self.last_reports = {}      # identidade -> {janela: pontos}
        self.points = {}            # carteira -> pontos
        self.reports, self.rejected = [], []
        self.fail_until = 0.0
        self.limit_next = self.reject_next = 0
        self.machines, self.models, self.jobs, self.blobs = {}, {}, {}, {}
        self.nonces, self.txs, self.status = {}, [], {}

    def window(self):
        return 100 + int((time.time() - self.t0) // self.W)

    def seed(self, w):
        return hashlib.sha256(f"{self.salt}:{w}".encode()).hexdigest()

    def mining(self):
        w = self.window()
        return {"window": w, "seed": self.seed(w), "prev_window": w - 1, "prev_seed": self.seed(w - 1),
                "window_seconds": self.W, "window_ends": self.t0 + (w - 99) * self.W,
                "min_bits": self.min_bits, "max_shares": self.max_shares, "now": time.time(),
                "hosting_per_mb_window": 5, "ai_default_price": DEFAULT_PRICES["ai_per_1k_tokens"]}

    # ------------------------------------------------------------ mineração
    def work(self, d):
        need(isinstance(d, dict), "dados inválidos")
        only(d, FIELDS["work"], "em work")
        try:
            miner = norm_address(d.get("miner"))
        except (ValueError, TypeError, AttributeError):
            raise TxError("minerador inválido") from None
        worker = d.get("worker")
        need(worker is None or (isinstance(worker, str) and WORKER.match(worker)),
             "worker deve ter de 1 a 16 caracteres [0-9a-f]")
        ident = miner if worker is None else f"{miner}.{worker}"
        w, bits, nonces = d.get("window"), d.get("bits"), d.get("nonces")
        cur = self.window()
        need(is_int(w) and w in (cur, cur - 1), "janela de mineração inválida ou já encerrada")
        need(is_int(bits) and self.min_bits <= bits <= 200, f"bits deve estar entre {self.min_bits} e 200")
        need(isinstance(nonces, list) and 1 <= len(nonces) <= self.max_shares,
             f"o relatório deve ter de 1 a {self.max_shares} shares")
        need(all(is_int(n) and 0 <= n < 2 ** 62 for n in nonces), "nonce de share inválido")
        need(len(set(nonces)) == len(nonces), "nonces repetidos no relatório")
        new = report_points(len(nonces), bits)
        with self.lock:
            old = self.last_reports.get(ident, {}).get(w, 0)
        need(new > old, "esta janela já tem um relatório com tantos pontos ou mais deste minerador")
        if share_ok is not None:
            seed = self.seed(w)
            bad = [n for n in nonces if not share_ok(seed, ident, n, bits)]
            need(not bad, "share inválido (não atinge a dificuldade)")
        with self.lock:
            self.last_reports.setdefault(ident, {})[w] = new
            self.points[miner] = self.points.get(miner, 0) + new - old
            txid = hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()
            self.reports.append({"id": txid, "miner": miner, "worker": worker, "ident": ident, "window": w,
                                 "bits": bits, "shares": len(nonces), "points": new, "delta": new - old,
                                 "verified": share_ok is not None, "received_in_window": cur, "time": time.time()})
        return txid

    # ------------------------------------------------------------ assinadas
    def signed(self, tx):
        need(isinstance(tx, dict) and isinstance(tx.get("data"), dict), "transação malformada")
        only(tx, {"type", "data", "nonce", "pub", "sig", "id"}, "na transação")
        type_ = tx.get("type")
        need(type_ in FIELDS, f"o nó falso não conhece a transação {type_}")
        pub = tx.get("pub")
        need(isinstance(pub, str) and HEX128.match(pub), "pub inválida")
        sender = address_of(pub)   # a assinatura não é conferida: é um nó falso
        body = json.dumps([type_, tx["data"], tx.get("nonce"), pub], sort_keys=True)
        txid = hashlib.sha256(body.encode()).hexdigest()
        with self.lock:
            if txid in self.status:
                return txid
            need(tx.get("nonce") == self.nonces.get(sender, 0),
                 f"nonce errado: esperado {self.nonces.get(sender, 0)}")
            only(tx["data"], FIELDS[type_], f"em {type_}")
            getattr(self, "_tx_" + type_)(sender, tx["data"], txid)
            self.nonces[sender] = tx["nonce"] + 1
            self.status[txid] = {"status": "included", "height": len(self.txs) + 1}
            self.txs.append({"id": txid, "type": type_, "sender": sender, "data": tx["data"]})
        return txid

    def _machine(self, addr):
        mc = self.machines.get(addr)
        need(mc is not None, "máquina não registrada")
        return mc

    def _unhost(self, addr):
        for m in self.models.values():
            m["assigned"].pop(addr, None)
            m["pinners"].pop(addr, None)
        if addr in self.machines:
            self.machines[addr]["models"] = []

    def _tx_register_machine(self, sender, d, txid):
        hw = d.get("hw", {})
        need(isinstance(hw, dict), "hw inválido")
        only(hw, HW_KEYS, "em hw")
        prices = d.get("prices", {})
        need(isinstance(prices, dict), "prices inválido")
        only(prices, DEFAULT_PRICES, "em prices")   # o aluguel é preço da rede, não da máquina
        for k, v in prices.items():
            need(is_int(v) and 1 <= v <= 1_000_000, f"{k} deve ser inteiro entre 1 e 1000000")
        need(0 <= hw.get("ai_ram_mb", 0) <= 1_048_576, "hw.ai_ram_mb fora do limite")
        pub = d.get("pub")
        if pub is not None:
            need(isinstance(pub, str) and HEX128.match(pub) and address_of(pub) == sender,
                 "pub não corresponde à chave da máquina")
        w = self.window()
        mc = self.machines.get(sender) or {"since": w, "models": [], "jobs_done": 0, "jobs_failed": 0,
                                           "earned": 0, "reputation": 0, "pub": None}
        mc.update(payout=norm_address(d.get("payout")), name=str(d.get("name", ""))[:40], hw=dict(hw),
                  ai=bool(d.get("ai")), compute=bool(d.get("compute")), endpoint=d.get("endpoint", ""),
                  prices=dict(DEFAULT_PRICES, **prices), last_seen=w, online=True)
        if pub is not None:
            mc["pub"] = pub
        self.machines[sender] = mc
        if not mc["ai"]:
            self._unhost(sender)

    def _tx_ping(self, sender, d, txid):
        mc = self._machine(sender)
        w = self.window()
        need(mc["last_seen"] != w, "já sinalizou nesta janela")
        mc.update(last_seen=w, online=True)

    def _tx_pin_model(self, sender, d, txid):
        mc = self._machine(sender)
        m = self.models.get(d.get("name"))
        need(m is not None, "modelo não existe")
        need(sender in m["assigned"], "modelo não atribuído a esta máquina")
        del m["assigned"][sender]
        m["pinners"][sender] = {"window": self.window(), "misses": 0, "miss_window": -1}
        mc["models"].append(m["name"])

    def _tx_decline_model(self, sender, d, txid):
        """SPEC-v2.2 §3: a máquina recusa o arquivo do modelo. Tira a atribuição na hora e põe cooldown, sem
        punir a reputação. Só o endereço com a atribuição (ou pin) pode mandar."""
        mc = self._machine(sender)
        m = self.models.get(d.get("name"))
        need(m is not None, "modelo não existe")
        need(sender in m["assigned"] or sender in m["pinners"], "modelo não atribuído a esta máquina")
        m["assigned"].pop(sender, None)
        m["pinners"].pop(sender, None)
        m.setdefault("cooldown", {})[sender] = self.window() + 288
        if m["name"] in mc["models"]:
            mc["models"].remove(m["name"])

    def _tx_create_job(self, sender, d, txid):
        need(d.get("kind") == "ai", "o nó falso só conhece jobs de IA")
        need(d.get("model") in self.models, "modelo não existe na rede (ou foi apagado)")
        need(isinstance(d.get("spec_hash"), str) and HEX64.match(d["spec_hash"]), "spec_hash inválido")
        for k in ("escrow", "max_price"):
            need(is_int(d.get(k)) and d[k] >= 1, f"{k} deve ser inteiro positivo")
        mt = d.get("max_tokens")   # SPEC-v2.2 §2: campo público obrigatório; o escrow precisa cobri-lo
        need(is_int(mt) and 1 <= mt <= CONTEXT, f"max_tokens deve ser inteiro entre 1 e {CONTEXT}")
        need(d["escrow"] >= -(-(mt + 16) * d["max_price"] // 1000),
             "o valor reservado não cobre a resposta (escrow >= teto de (max_tokens+16) × preço)")
        job = {"id": txid, "client": sender, "kind": "ai", "status": "open", "created": time.time(),
               "window": self.window(), "escrow": d["escrow"], "max_price": d["max_price"], "model": d["model"],
               "spec_hash": d["spec_hash"], "max_tokens": mt, "replicas": 1, "claims": {}, "results": {},
               "result_hash": None}
        client_pub = d.get("client_pub")
        if client_pub is not None:
            need(isinstance(client_pub, str) and HEX128.match(client_pub) and address_of(client_pub) == sender,
                 "client_pub não corresponde à carteira que paga o pedido")
            job["client_pub"] = client_pub
        if d.get("machine") is not None:
            addr = norm_address(d["machine"])
            mc = self.machines.get(addr)
            need(mc is not None, "a máquina escolhida não está registrada na rede")
            need(mc["online"] and mc["last_seen"] >= self.window() - 1, "a máquina escolhida não está online agora")
            need(mc["ai"] and mc["pub"], "a máquina escolhida não recebe pedidos cifrados")
            need(d["model"] in mc["models"], "a máquina escolhida não hospeda este modelo")
            need(addr != sender, "você não pode mandar o pedido para a sua própria máquina")
            need(client_pub is not None, "um pedido direcionado precisa de client_pub")
            need(d["spec_hash"] in self.blobs, "envie o pedido (/blob) antes de criar o job")
            job["machine"] = addr
        self.jobs[txid] = job

    def _job(self, d):
        job = self.jobs.get(d.get("job_id"))
        need(job is not None, "job não existe")
        return job

    def _tx_claim_job(self, sender, d, txid):
        job, mc = self._job(d), self._machine(sender)
        need(job.get("machine") in (None, sender), "este job é para outra máquina")
        need(mc["online"], "máquina offline: ela precisa sinalizar (ping) antes")
        need(job["status"] == "open", f"job não está aberto ({job['status']})")
        need(sender not in job["claims"], "você já reivindicou este job")
        need(sender != job["client"], "o cliente não pode executar o próprio job")
        need(job["model"] in mc["models"], "esta máquina não hospeda este modelo")
        price = mc["prices"]["ai_per_1k_tokens"]
        need(price <= job["max_price"], "seu preço está acima do máximo do cliente")
        job["claims"][sender] = {"time": time.time(), "price": price}
        job["status"] = "running"

    def _tx_submit_result(self, sender, d, txid):
        job = self._job(d)
        need(job["status"] in ("open", "running"), "job já encerrado")
        need(sender in job["claims"], "você não reivindicou este job")
        need(sender not in job["results"], "resultado já enviado")
        need(isinstance(d.get("result_hash"), str) and HEX64.match(d["result_hash"]), "result_hash inválido")
        failed = d.get("failed", False)
        need(isinstance(failed, bool), "failed deve ser verdadeiro ou falso")
        usage = d.get("usage", {})
        need(isinstance(usage, dict), "usage inválido")
        only(usage, {"tokens_in", "tokens_out"}, "em usage")
        usage = {k: usage.get(k, 0) for k in ("tokens_in", "tokens_out")}
        need(all(is_int(v) and 0 <= v <= 10 ** 9 for v in usage.values()), "tokens fora do limite")
        need(not failed or not any(usage.values()), "uma falha não cobra tokens: usage deve ser zero")
        job["results"][sender] = {"hash": d["result_hash"], "usage": usage, "time": time.time(), "failed": failed}
        job["result_hash"] = d["result_hash"]
        mc = self.machines[sender]
        if failed:
            job.update(status="failed", cost=0)
            mc["jobs_failed"] += 1
        else:
            tokens = usage["tokens_in"] + usage["tokens_out"]
            job.update(status="done", cost=min(job["escrow"], -(-tokens * job["claims"][sender]["price"] // 1000)))
            mc["jobs_done"] += 1
            mc["earned"] += job["cost"]

    def _tx_cancel_job(self, sender, d, txid):
        job = self._job(d)
        need(job["client"] == sender, "só quem pediu pode cancelar")
        need(job["status"] in ("open", "running"), f"este pedido já foi encerrado ({job['status']})")
        need(not job["claims"], "um computador já está respondendo este pedido")
        job["status"] = "canceled"

    # ------------------------------------------------------------ leitura
    def machine_info(self, addr):
        mc = self.machines.get(addr)
        if mc is None:
            return None
        out = json.loads(json.dumps(mc))
        out["address"] = addr
        out["assignments"] = [{"name": n, "url": m["url"], "sha256": m["sha256"], "size": m["size"],
                               "ram_mb": m["ram_mb"], "status": "pinned" if addr in m["pinners"] else "assigned"}
                              for n, m in sorted(self.models.items()) if addr in m["pinners"] or addr in m["assigned"]]
        out["sites"] = []
        return out


def make_handler(chain):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, raw=None, ctype="application/json"):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _failing(self):
            if time.time() < chain.fail_until:   # como o nginx com o nó fora: 503 em HTML, sem JSON
                self._send(503, None, raw=b"<html><body>503 Service Unavailable</body></html>", ctype="text/html")
                return True
            return False

        def do_GET(self):
            try:
                self._get()
            except (ValueError, TypeError) as e:   # endereço inválido na rota, por exemplo
                self._send(400, {"error": str(e)})

        def _get(self):
            u = urlparse(self.path)
            path = unquote(u.path)
            parts = [p for p in path.split("/") if p]
            if path == "/reports":
                return self._send(200, chain.reports)
            if path == "/rejected":
                return self._send(200, chain.rejected)
            if path == "/txs":
                return self._send(200, chain.txs)
            if self._failing():
                return
            with chain.lock:
                if path == "/mining":
                    return self._send(200, chain.mining())
                if path == "/status":
                    return self._send(200, {"height": len(chain.txs), "version": "falso", "network": "sigma"})
                if path == "/machines":
                    return self._send(200, chain.machines)
                if path == "/models":
                    return self._send(200, chain.models)
                if path == "/sites":
                    return self._send(200, {})
                if path == "/jobs":
                    status = parse_qs(u.query).get("status", [None])[0]
                    return self._send(200, [j for j in chain.jobs.values() if not status or j["status"] == status])
                if len(parts) == 2 and parts[0] == "job":
                    job = chain.jobs.get(parts[1])
                    return self._send(200, job) if job else self._send(404, {"error": "job não existe"})
                if len(parts) == 2 and parts[0] == "machine":
                    rec = chain.machine_info(norm_address(parts[1]))
                    return self._send(200, rec) if rec else self._send(404, {"error": "máquina não registrada"})
                if len(parts) == 2 and parts[0] == "account":
                    addr = norm_address(parts[1])
                    n = chain.nonces.get(addr, 0)
                    return self._send(200, {"address": addr, "balance": 0, "nonce": n, "next_nonce": n})
                if len(parts) == 2 and parts[0] == "tx":
                    st = chain.status.get(parts[1])
                    return self._send(200, st) if st else self._send(404, {"error": "transação desconhecida"})
                if len(parts) == 2 and parts[0] == "blob":
                    data = chain.blobs.get(parts[1])
                    if data is None:
                        return self._send(404, {"error": "blob não encontrado"})
                    return self._send(200, None, raw=data, ctype="application/octet-stream")
            self._send(404, {"error": "rota desconhecida"})

        def do_POST(self):
            u = urlparse(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            q = parse_qs(u.query)
            if u.path == "/fail":
                chain.fail_until = time.time() + float(q.get("seconds", ["3"])[0])
                return self._send(200, {"ok": True})
            if u.path in ("/limit", "/reject"):
                setattr(chain, "limit_next" if u.path == "/limit" else "reject_next", int(q.get("n", ["1"])[0]))
                return self._send(200, {"ok": True})
            if u.path == "/test/model":
                d = json.loads(body)
                with chain.lock:
                    chain.models[d["name"]] = {"name": d["name"], "url": d["url"], "sha256": d["sha256"],
                                               "size": d["size"], "ram_mb": d.get("ram_mb", 600),
                                               "assigned": {d["assign"]: chain.window()} if d.get("assign") else {},
                                               "pinners": {}}
                return self._send(200, {"ok": True})
            if self._failing():
                return
            if u.path == "/blob":
                h = hashlib.sha256(body).hexdigest()
                with chain.lock:
                    chain.blobs[h] = body
                return self._send(200, {"hash": h})
            if u.path != "/tx":
                return self._send(404, {"error": "rota desconhecida"})
            try:
                tx = json.loads(body)
                if isinstance(tx, dict) and tx.get("type") == "work":
                    with chain.lock:
                        if chain.limit_next > 0:   # o limite por conexão de internet do --public
                            chain.limit_next -= 1
                            return self._send(429, {"error": "limite de 256 relatórios de mineração por conexão de "
                                                             "internet nesta janela; o próximo vai na janela seguinte"})
                        if chain.reject_next > 0:
                            chain.reject_next -= 1
                            raise TxError("share inválido (não atinge a dificuldade)")
                    only(tx, {"type", "data", "id"}, "na transação")
                    return self._send(200, {"id": chain.work(tx.get("data"))})
                return self._send(200, {"id": chain.signed(tx)})
            except (ValueError, TypeError, AttributeError) as e:
                chain.rejected.append(str(e))
                return self._send(400, {"error": str(e)})

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8730)
    ap.add_argument("--window", type=float, default=3.0)
    ap.add_argument("--min-bits", type=int, default=4)
    ap.add_argument("--max-shares", type=int, default=32)
    a = ap.parse_args()
    chain = FakeChain(a.window, a.min_bits, a.max_shares)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(chain))
    print(f"nó falso em http://127.0.0.1:{a.port} (janela {a.window}s, pow conferido: {share_ok is not None})",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
