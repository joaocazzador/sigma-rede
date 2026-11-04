"""Minerador do Sigma: transforma CPU ociosa em SIG.

Cada janela da rede tem uma seed (derivada do bloco que abriu a janela). Os processos procuram nonces cujo
scrypt(seed:identidade:bits:nonce) fique abaixo do alvo de `bits`; no fim da janela vai UM relatório com as
provas (shares) achadas. Os pontos (provas << bits) estimam quantas contas você fez, e a emissão de cada época
é dividida por esses pontos. A identidade é a carteira (ou "carteira.computador", quando vários PCs mineram
para a mesma carteira) e entra no hash, então ninguém usa o trabalho de outro. Os bits também entram: a
dificuldade é escolhida antes de calcular, e uma prova não "sobe de nível" depois.

Os processos rodam com prioridade baixa: o computador continua seu, o minerador só usa a sobra.
"""
import math
import multiprocessing
import os
import re
import threading
import time
from multiprocessing.connection import wait as wait_conns

from .crypto import norm_address
from .pow import pow_hash

BATCH = 16                 # hashes entre uma olhada e outra no cano de controle (~10 ms)
TARGET_SHARES = 14         # bits escolhidos para render ~14 provas por janela (o teto da rede é max_shares)
MAX_BITS = 200             # MAX_SHARE_BITS da chain
BENCH_BITS = 12            # o _bench só mede o tempo de um hash: qualquer bits fixo serve
NONCE_SPAN = 1 << 40       # cada redistribuição na mesma janela começa num bloco novo de nonces (sem repetir)
RETRY_AFTER = 30           # s: relatório que não chegou (rede fora, 5xx, 429) tenta de novo uma vez
WORKER_RE = re.compile(r"^[0-9a-f]{1,16}\Z")


def _pt(n):
    return f"{n:,}".replace(",", ".")


def _low_priority():
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            # o pseudo-handle (-1) tem 64 bits: sem os tipos o ctypes o corta e a chamada falha calada
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass


def _worker(conn, counter):
    """Processo minerador (função de topo: o spawn e o PyInstaller precisam importá-la pelo nome).

    Mensagens do pai: ("job", id, seed, identidade, bits, primeiro_nonce, passo), "pause", "run" ou None (sair).
    Devolve (id, nonce) para cada prova achada. Se o pai morrer, o cano quebra e o processo termina."""
    _low_priority()
    job, running = None, True
    try:
        while True:
            if job is None or not running or conn.poll():
                msg = conn.recv()          # parado (sem job ou pausado): espera sem gastar CPU
                if msg is None:
                    return
                if msg == "pause":
                    running = False
                elif msg == "run":
                    running = True
                else:
                    job = msg
                    _, job_id, seed, ident, bits, nonce, step = msg
                    target = 1 << (256 - bits)
                continue
            for _ in range(BATCH):
                if pow_hash(seed, ident, bits, nonce) < target:
                    conn.send((job_id, nonce))
                nonce += step
            with counter.get_lock():
                counter.value += BATCH
    except (EOFError, OSError, KeyboardInterrupt):
        return


def _retryable(e):
    """Erro passageiro (rede fora, nó sobrecarregado com 5xx, limite por conexão com 429): vale mandar as
    mesmas provas de novo. Recusa da rede (400: janela fechada, prova inválida...) não volta."""
    code = getattr(e, "status", None)
    if code is None:
        code = getattr(e, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code == 429 or code >= 500
    if isinstance(e, OSError):            # conexão recusada, tempo esgotado...
        return True
    msg = str(e).lower()
    return (msg.startswith("não consegui falar com o nó") or "http 5" in msg or "http 429" in msg
            or "limite" in msg or "tempo esgotado" in msg)


class Miner:
    def __init__(self, client, payout: str, workers: int, log=print, worker=None):
        self.client = client
        self.payout = norm_address(payout)
        self.worker = None
        if worker is not None:   # cada computador minerando para a mesma carteira tem a sua identidade
            worker = str(worker).lower()
            if not WORKER_RE.match(worker):
                raise ValueError("worker deve ter de 1 a 16 caracteres hexadecimais (0-9, a-f)")
            self.worker = worker
        self.ident = f"{self.payout}.{self.worker}" if self.worker else self.payout
        self.log = log
        self._ctx = multiprocessing.get_context("spawn")   # igual em Windows, Linux, macOS e no .exe congelado
        self._counter = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._procs = []            # [(Process, Connection)]
        self._n = max(0, int(workers))
        self._paused = False
        self._job = None            # janela em mineração: id, window, seed, bits, max_shares
        self._job_ids = 0
        self._gen = 0               # quantas vezes o job atual foi redistribuído
        self._found = {}            # id do job -> nonces achados (sem repetição, na ordem)
        self._reported = set()      # janelas encerradas: aceitas pela rede ou recusadas de vez (400)
        self._retry = None          # provas que não chegaram: {"job", "nonces", "at", "error"} (uma tentativa)
        self._info = (None, 0.0)    # último /mining e quando chegou (o Host usa no ping, sem consultar de novo)
        self._dispatched = 0.0
        self._deaths = []
        self.hashrate = 0.0         # H/s agora (média curta; 0 quando pausado)
        self._rate = 0.0            # H/s rodando (média longa): base para escolher os bits
        self.reports_sent = self.reports_failed = 0
        self.fail_streak = 0        # relatórios recusados seguidos (zera no próximo aceito)
        self.last_fail = None       # texto da última recusa
        self.last_ok = None         # hora do último relatório aceito
        self.last_report = None
        self._errors = {}           # origem -> mensagem
        self._logged_error = None

    # ------------------------------------------------------------- controle
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            if self._counter is None:
                self._counter = self._ctx.Value("Q", 0)
            self._spawn()
            self._thread = threading.Thread(target=self._loop, name="sigma-minerador", daemon=True)
            self._thread.start()
        who = f" (computador {self.worker})" if self.worker else ""
        self.log(f"[minerador] minerando para {self.payout}{who} com {self._n} processo(s)")

    def stop(self, report=True):
        """Desliga os processos. Com `report`, já relata a janela em andamento (o trabalho dela não se perde)."""
        with self._lock:
            if not self._thread:
                return
            self._stop.set()
            self._send_all("pause")    # libera a CPU na hora; o resto pode levar um instante
            thread, self._thread = self._thread, None
        thread.join(timeout=15)
        with self._lock:
            procs, self._procs = self._procs, []
            self._drain([c for _, c in procs])
            job, self._job = self._job, None
            retry, self._retry = self._retry, None
        self._shutdown(procs)
        if report and retry:   # provas que esperavam a nova tentativa: última chance
            self._send(retry["job"], retry["nonces"], retry=True)
        if report and job:
            self._report(job, None)
        with self._lock:
            self._found.clear()
            self.hashrate = 0.0
        self.log("[minerador] desligado")

    def set_workers(self, n):
        n = max(0, int(n))
        with self._lock:
            old, self._n = self._n, n
            if not self._thread or n == old:
                return
            if old and n:
                self._rate *= n / old   # a estimativa acompanha a mudança na hora, sem esperar a média
            if n < len(self._procs):
                extra, self._procs = self._procs[n:], self._procs[:n]
                self._drain([c for _, c in extra])
                self._shutdown(extra)
                self._dispatch()        # passo novo (n) para quem ficou
            else:
                self._spawn()           # cria os que faltam e já redistribui o job
        self.log(f"[minerador] agora com {n} processo(s)")

    def pause(self):
        """Para de calcular sem perder a janela (as provas achadas continuam guardadas para o relatório)."""
        with self._lock:
            if not self._paused:
                self._paused = True
                self._send_all("pause")

    def resume(self):
        with self._lock:
            if self._paused:
                self._paused = False
                self._dispatched = time.time()
                self._send_all("run")

    @property
    def paused(self):
        return self._paused

    def last_mining(self):
        """(último /mining, quando chegou) ou (None, 0.0): o Host sinaliza a janela sem consultar o nó de novo."""
        with self._lock:
            return self._info

    def stats(self) -> dict:
        with self._lock:
            job = self._job
            err = next(iter(self._errors.values()), None)
            return {"running": bool(self._thread), "paused": self._paused, "workers": self._n,
                    "hashrate": round(self.hashrate, 1), "window": job["window"] if job else None,
                    "bits": job["bits"] if job else None,
                    "shares_window": len(self._found.get(job["id"], ())) if job else 0,
                    "reports_sent": self.reports_sent, "reports_failed": self.reports_failed,
                    "fail_streak": self.fail_streak, "last_fail": self.last_fail, "last_ok": self.last_ok,
                    "last_report": dict(self.last_report) if self.last_report else None,
                    "last_error": err, "payout": self.payout, "worker": self.worker}

    # ------------------------------------------------------------ processos
    def _spawn(self):
        while len(self._procs) < self._n:
            parent, child = self._ctx.Pipe()
            p = self._ctx.Process(target=_worker, args=(child, self._counter), daemon=True,
                                  name=f"sigma-minerador-{len(self._procs)}")
            try:
                p.start()
            except Exception as e:
                parent.close()
                self._error("processos", f"não consegui iniciar os processos de mineração: {e}")
                break
            finally:
                child.close()
            self._procs.append((p, parent))
        self._dispatch()

    def _send_all(self, msg):
        for _, conn in self._procs:
            try:
                conn.send(msg)
            except (OSError, ValueError):
                pass          # processo morto: o laço percebe e repõe

    def _dispatch(self):
        """Manda o job atual para todos: o processo k de n testa base+k, base+k+n, base+k+2n..."""
        job, n = self._job, len(self._procs)
        if not job or not n:
            return
        base = self._gen * NONCE_SPAN
        self._gen += 1
        self._dispatched = time.time()
        for k, (_, conn) in enumerate(self._procs):
            try:
                conn.send(("job", job["id"], job["seed"], self.ident, job["bits"], base + k, n))
                conn.send("pause" if self._paused else "run")
            except (OSError, ValueError):
                pass

    @staticmethod
    def _shutdown(procs):
        for _, conn in procs:
            try:
                conn.send(None)
            except (OSError, ValueError):
                pass
        deadline = time.time() + 3
        for p, conn in procs:
            p.join(max(0.1, deadline - time.time()))
            if p.is_alive():
                p.terminate()
                p.join(1)
            conn.close()

    def _drain(self, conns):
        """Recolhe as provas que chegaram (só de jobs ainda abertos)."""
        dead = []
        for conn in conns:
            try:
                while conn.poll():
                    job_id, nonce = conn.recv()
                    found = self._found.get(job_id)
                    if found is not None and nonce not in found:
                        found[nonce] = None
            except (EOFError, OSError, ValueError):
                dead.append(conn)
        return dead

    def _replace_dead(self, dead_conns):
        now, alive = time.time(), []
        for p, c in self._procs:
            if p.is_alive() and c not in dead_conns:
                alive.append((p, c))
            else:
                c.close()
                self._deaths.append(now)
        self._procs = alive
        if len(alive) >= self._n:
            return
        self._deaths = [t for t in self._deaths if t > now - 60]
        if len(self._deaths) > 2 * self._n + 3:   # morrendo em série: espera esfriar em vez de criar sem parar
            self._error("processos", "os processos de mineração estão fechando sozinhos; tentando de novo em 1 min")
            return
        self._spawn()

    # ---------------------------------------------------------------- laço
    def _loop(self):
        last_t, last_count = time.time(), self._counter.value
        next_poll = 0.0
        while not self._stop.is_set():
            with self._lock:
                conns = [c for _, c in self._procs]
            if conns:
                try:
                    wait_conns(conns, timeout=0.25)
                except (OSError, ValueError):
                    time.sleep(0.25)
            else:
                self._stop.wait(0.25)
            if self._stop.is_set():
                break
            with self._lock:
                dead = self._drain([c for _, c in self._procs])
                now = time.time()
                if now - last_t >= 1:
                    count = self._counter.value
                    self._measure((count - last_count) / (now - last_t), now - last_t)
                    last_t, last_count = now, count
                    self._replace_dead(dead)
            if now >= next_poll:
                next_poll = time.time() + self._poll()

    def _measure(self, rate, dt):
        if self._paused or not self._procs or not self._job:
            self.hashrate = 0.0
            return
        self.hashrate += (rate - self.hashrate) * min(1.0, dt / 5)
        if time.time() - self._dispatched > 3:   # processos recém-criados ainda estão aquecendo
            self._rate = rate if not self._rate else self._rate + (rate - self._rate) * min(1.0, dt / 30)

    def _poll(self):
        """Consulta a janela; ao virar, começa a nova seed e relata a anterior. Devolve a próxima espera."""
        try:
            info = self.client.mining()
            w, seed, ws = info["window"], info.get("seed"), float(info.get("window_seconds") or 300)
        except Exception as e:
            self._error("nó", f"não consegui consultar a mineração: {e}")
            return 3.0
        with self._lock:
            self._info = (info, time.time())
        self._errors.pop("nó", None)
        base = min(3.0, max(0.5, ws / 5))
        try:   # perto do fim da janela, consulta logo depois dele (o relógio é o do nó)
            left = float(info["window_ends"]) - float(info.get("now") or time.time())
            if -5 < left < base:
                base = max(0.5, left + 0.6)
        except (KeyError, TypeError, ValueError):
            pass
        if not seed:
            return base
        old = None
        with self._lock:
            cur = self._job
            if not (cur and cur["window"] == w and cur["seed"] == seed):
                old = cur
                self._begin(info)
                if old and old["window"] == w:
                    self._found.pop(old["id"], None)   # mesma janela com outra seed: a rede mudou, provas não valem
        if old and old["window"] != w:
            self._report(old, info)
        self._retry_due(info)
        return base

    def _begin(self, info):
        rate = self._rate or self._bench()
        raw = rate * float(info.get("window_seconds") or 300) / TARGET_SHARES
        bits = round(math.log2(raw)) if raw > 1 else 0
        bits = max(int(info.get("min_bits") or 1), min(MAX_BITS, bits))
        self._job_ids += 1
        self._job = {"id": self._job_ids, "window": info["window"], "seed": info["seed"], "bits": bits,
                     "max_shares": int(info.get("max_shares") or 32)}
        keep = {self._job_ids, self._job_ids - 1}   # a janela anterior ainda vai ser relatada
        self._found = {k: v for k, v in self._found.items() if k in keep}
        self._found[self._job_ids] = {}
        self._gen = 0
        self._dispatch()

    def _bench(self):
        """Primeira janela: ainda sem medição, estima pela velocidade de um núcleo."""
        t = time.perf_counter()
        for i in range(8):
            pow_hash("0" * 64, self.ident, BENCH_BITS, i)
        per = max(1e-5, (time.perf_counter() - t) / 8)
        return max(1, len(self._procs)) / per

    @staticmethod
    def _still_open(job, info):
        """A janela do job ainda aceita relatório (é a anterior à atual, com a mesma seed)?"""
        w = job["window"]
        if "prev_seed" in info:
            return info.get("prev_window") == w and info.get("prev_seed") == job["seed"]
        return info["window"] == w + 1

    def _report(self, job, info):
        """Um relatório por janela: até max_shares provas, todas com os bits da janela."""
        w = job["window"]
        with self._lock:
            nonces = list(self._found.pop(job["id"], {}))[:job["max_shares"]]
            if not nonces or w in self._reported:
                return
        if info is not None and not self._still_open(job, info):
            self.log(f"[minerador] janela {w} já fechou sem dar tempo de enviar ({len(nonces)} provas)")
            return
        self._send(job, nonces)

    def _retry_due(self, info):
        """Segunda (e última) tentativa das provas que não chegaram, enquanto a janela ainda aceita."""
        with self._lock:
            r = self._retry
            if not r or time.time() < r["at"]:
                return
            self._retry = None
        if not self._still_open(r["job"], info):
            self._failed(r["job"]["window"], f"a janela fechou antes da nova tentativa ({r['error']})")
            return
        self._send(r["job"], r["nonces"], retry=True)

    def _send(self, job, nonces, retry=False):
        w = job["window"]
        report = {"miner": self.payout, "window": w, "bits": job["bits"], "nonces": nonces}
        if self.worker:
            report["worker"] = self.worker
        try:
            txid = self.client.submit_work(report)
        except Exception as e:
            msg = str(e)
            if retry and "já tem um relatório" in msg:
                txid = None   # a primeira tentativa tinha chegado: só a resposta se perdeu no caminho
            elif not retry and _retryable(e) and not self._stop.is_set():
                with self._lock:
                    old, self._retry = self._retry, {"job": job, "nonces": nonces, "at": time.time() + RETRY_AFTER,
                                                     "error": msg}
                if old:   # a anterior ainda esperava a vez: não dá mais tempo
                    self._failed(old["job"]["window"], old["error"])
                self._error("relatório", f"as provas da janela {w} não chegaram à rede ({msg}); "
                                         f"tento de novo em {RETRY_AFTER} s")
                return
            else:
                self._failed(w, msg)
                return
        pts = len(nonces) << job["bits"]
        with self._lock:
            self._reported.add(w)
            self._reported = {x for x in self._reported if x >= w - 5}
            self.reports_sent += 1
            self.fail_streak = 0
            self.last_ok = time.time()
            self.last_report = {"window": w, "bits": job["bits"], "shares": len(nonces), "points": pts,
                                "txid": txid, "time": self.last_ok}
            self._errors.pop("relatório", None)
        self.log(f"[minerador] janela {w}: {len(nonces)} provas enviadas (~{_pt(pts)} contas)"
                 + (" na segunda tentativa" if retry else ""))

    def _failed(self, w, msg):
        """Relatório recusado de vez: a janela não volta, e o painel avisa depois de recusas seguidas."""
        with self._lock:
            self._reported.add(w)
            self._reported = {x for x in self._reported if x >= w - 5}
            self.reports_failed += 1
            self.fail_streak += 1
            self.last_fail = msg
        self._error("relatório", f"relatório da janela {w} recusado: {msg}")

    def _error(self, origin, msg):
        with self._lock:
            self._errors[origin] = msg
        if msg != self._logged_error:   # sem repetir a mesma mensagem a cada 3 s
            self._logged_error = msg
            self.log(f"[minerador] {msg}")
