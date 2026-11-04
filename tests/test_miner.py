"""Teste do minerador contra o nó falso (tests/fake_node.py) na porta 8730.

    python tests/test_miner.py

0) contas sem processos: bits para ~14 provas por janela, worker válido, o que é erro passageiro;
1) 2 processos por ~10 s com a identidade "carteira.computador" (bits dentro do hash, conferido pelo nó);
2) um segundo PC na mesma carteira: os dois relatórios da mesma janela valem;
3) pausa e retomada; 4) troca do número de processos;
5) 429 (limite por conexão): a janela não se perde, vai de novo ~RETRY_AFTER depois;
6) 400 (recusa): fail_streak/last_fail sobem e zeram no próximo aceito;
7) nó fora do ar por 4 s; 8) desligamento.
"""
import json
import multiprocessing
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
PORT = 8730
NODE = f"http://127.0.0.1:{PORT}"


class MiniClient:
    """Só o que o minerador usa (caso o sigma.client ainda esteja sendo reescrito)."""

    def __init__(self, node):
        self.node = node

    def _req(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.node + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error")
            except ValueError:
                msg = f"o nó respondeu HTTP {e.code}"
            err = RuntimeError(msg)
            err.status = e.code
            raise err from None

    def mining(self):
        return self._req("/mining")

    def submit_work(self, report):
        return self._req("/tx", {"type": "work", "data": report})["id"]


def get(path):
    with urllib.request.urlopen(NODE + path, timeout=5) as r:
        return json.loads(r.read())


def post(path):
    req = urllib.request.Request(NODE + path, data=b"{}", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def priority_class(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    try:
        return k32.GetPriorityClass(h)
    finally:
        k32.CloseHandle(h)


def check(cond, msg):
    print(("  ok   " if cond else "  FALHOU ") + msg, flush=True)
    if not cond:
        raise SystemExit(1)


def wait_for(cond, timeout, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def part_unit(client):
    from sigma import miner as mm
    from sigma.miner import Miner
    print("0) contas sem processos")
    payout = "0x" + "ab" * 20
    m = Miner(client, payout, workers=0, log=lambda s: None)
    check(mm.TARGET_SHARES == 14, "mira ~14 provas por janela")
    info = {"window": 7, "seed": "ab" * 32, "window_seconds": 300, "min_bits": 4, "max_shares": 32}
    for rate, want in ((14 * 2 ** 12 / 300, 12), (14 * 2 ** 20 / 300, 20), (14 * 1.4 * 2 ** 12 / 300, 12),
                       (0.001, 4), (1e80, 200)):
        m._rate = rate
        m._begin(info)
        check(m._job["bits"] == want, f"{rate:.4g} H/s numa janela de 300 s -> {want} bits")
    check(Miner(client, payout, 0, worker="AB12cd34").ident == "sig" + "ab" * 20 + ".ab12cd34",
          "identidade carteira.computador (worker em minúsculas)")
    check(Miner(client, payout, 0).ident == "sig" + "ab" * 20, "sem worker, a identidade é só a carteira")
    for bad in ("", "xyz", "a" * 17, "12-34"):
        try:
            Miner(client, payout, 0, worker=bad)
            check(False, f"worker {bad!r} deveria ser recusado")
        except ValueError:
            pass
    check(True, "worker inválido é recusado (1 a 16 caracteres 0-9a-f)")

    class E(Exception):
        def __init__(self, msg, status=None):
            super().__init__(msg)
            if status is not None:
                self.status = status

    cases = [(E("x", 429), True), (E("x", 503), True), (E("x", 500), True), (E("x", 400), False),
             (E("não consegui falar com o nó http://x: tempo esgotado"), True), (E("o nó respondeu HTTP 502"), True),
             (E("limite de 256 relatórios de mineração por conexão de internet nesta janela"), True),
             (ConnectionRefusedError("recusada"), True),
             (E("share inválido (não atinge a dificuldade)"), False),
             (E("janela de mineração inválida ou já encerrada"), False)]
    check(all(mm._retryable(e) is ok for e, ok in cases),
          "erro passageiro (rede, 5xx, 429) tenta de novo; recusa da rede (400) não")


def main():
    from sigma import miner as mm
    from sigma.miner import Miner
    try:
        from sigma.client import Client
        client = Client(NODE) if hasattr(Client, "submit_work") else MiniClient(NODE)
    except Exception as e:  # cliente em obras: usa o mínimo
        print("sigma.client indisponível, usando MiniClient:", e)
        client = MiniClient(NODE)
    part_unit(client)
    mm.RETRY_AFTER = 1.0   # o de verdade é ~30 s; janelas de 3 s aqui
    node = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_node.py"), "--port", str(PORT),
                             "--window", "3", "--min-bits", "4"])
    logs = []
    miner = miner2 = None
    try:
        for _ in range(50):
            try:
                get("/mining")
                break
            except OSError:
                time.sleep(0.1)
        payout = "0x" + "ab" * 20
        addr = "sig" + "ab" * 20
        miner = Miner(client, payout, workers=2, log=lambda m: (logs.append(m), print("   ", m, flush=True)),
                      worker="ab12cd34")
        print("1) 2 processos por ~10 s, identidade carteira.computador")
        t0 = time.time()
        miner.start()
        while time.time() - t0 < 10:
            time.sleep(2)
            s = miner.stats()
            print(f"   {time.time() - t0:4.1f}s  {s['hashrate']:8.1f} H/s  janela={s['window']} bits={s['bits']} "
                  f"provas={s['shares_window']} relatórios={s['reports_sent']} falhas={s['reports_failed']}")
        s = miner.stats()
        check(s["running"] and s["workers"] == 2, "rodando com 2 processos")
        check(s["hashrate"] > 0, f"hashrate medido ({s['hashrate']} H/s)")
        check(s["reports_sent"] >= 2, f"relatórios enviados: {s['reports_sent']}")
        check(s["reports_failed"] == 0 and not get("/rejected"), "nenhum relatório recusado")
        check(s["worker"] == "ab12cd34" and s["fail_streak"] == 0 and s["last_fail"] is None
              and time.time() - s["last_ok"] < 8, "stats: worker, fail_streak 0, last_fail vazio, last_ok recente")
        info, t = miner.last_mining()
        check(isinstance(info, dict) and isinstance(info.get("window"), int) and time.time() - t < 5,
              "last_mining(): o último /mining e quando chegou (o Host usa no ping)")
        reps = get("/reports")
        check(reps and all(r["worker"] == "ab12cd34" and r["ident"] == addr + ".ab12cd34" and r["verified"]
                           for r in reps), "relatórios levam o worker e o nó confere o hash com carteira.computador")
        line = next((m for m in logs if "provas enviadas" in m), "")
        check(re.fullmatch(r"\[minerador\] janela \d+: \d+ provas enviadas \(~[\d.]+ contas\)", line) is not None,
              f"log no formato combinado: {line!r}")
        kids = multiprocessing.active_children()
        check(len(kids) == 2, f"{len(kids)} processos filhos")
        if os.name == "nt":
            check(all(priority_class(p.pid) == 0x4000 for p in kids), "processos em prioridade abaixo do normal")

        print("2) outro PC minerando para a mesma carteira")
        miner2 = Miner(client, payout, workers=1, log=lambda m: print("    [pc2]", m, flush=True), worker="ff00")
        miner2.start()
        ok = wait_for(lambda: miner2.stats()["reports_sent"] >= 2, 15)
        both = {}
        for r in get("/reports"):
            both.setdefault(r["window"], set()).add(r["ident"])
        shared = [w for w, ids in both.items() if len(ids) == 2]
        check(ok and miner2.stats()["reports_failed"] == 0 and shared,
              f"os dois PCs têm relatório aceito na mesma janela ({len(shared)} janelas)")
        check(all(r["miner"] == addr for r in get("/reports")), "os pontos dos dois vão para a mesma carteira")
        miner2.stop()
        miner2 = None

        print("3) pausa e retomada")
        miner.pause()
        time.sleep(1.5)
        c1 = miner._counter.value
        time.sleep(1.5)
        c2 = miner._counter.value
        check(c2 - c1 <= 2 * 16 and miner.stats()["hashrate"] == 0, f"pausado: {c2 - c1} hashes em 1,5 s")
        check(miner.stats()["paused"], "stats mostra pausado")
        miner.resume()
        time.sleep(2.5)
        check(miner.stats()["hashrate"] > 0 and not miner.stats()["paused"], "voltou a minerar")

        print("4) set_workers 3 -> 1")
        miner.set_workers(3)
        time.sleep(1.5)
        check(len(multiprocessing.active_children()) == 3, "3 processos")
        miner.set_workers(1)
        time.sleep(0.5)
        check(len(multiprocessing.active_children()) == 1, "1 processo")

        print("5) 429: limite por conexão de internet")
        s0 = miner.stats()
        logs.clear()
        post("/limit?n=1")
        ok = wait_for(lambda: any("segunda tentativa" in m for m in logs), 15)
        s = miner.stats()
        check(ok and any("não chegaram à rede" in m for m in logs), "a janela recusada com 429 foi enviada de novo")
        check(s["reports_failed"] == s0["reports_failed"] and s["fail_streak"] == 0,
              "a nova tentativa entrou: nada contou como recusa")

        print("6) 400: a rede recusa os relatórios")
        post("/reject?n=2")
        ok = wait_for(lambda: miner.stats()["fail_streak"] >= 2, 15)
        s = miner.stats()
        check(ok and "share inválido" in (s["last_fail"] or "") and s["reports_failed"] >= s0["reports_failed"] + 2,
              f"fail_streak {s['fail_streak']}, last_fail: {s['last_fail']}")
        ok = wait_for(lambda: miner.stats()["fail_streak"] == 0, 12)
        check(ok and miner.stats()["last_ok"] > s["last_ok"], "o próximo relatório aceito zera o fail_streak")

        print("7) nó fora do ar por 4 s")
        sent = miner.stats()["reports_sent"]
        post("/fail?seconds=4")
        time.sleep(4)
        err = miner.stats()["last_error"]
        check(bool(err), f"erro registrado: {err}")
        time.sleep(7)
        s = miner.stats()
        check(s["last_error"] is None, "erro limpo quando o nó voltou")
        check(s["reports_sent"] > sent, f"continuou relatando ({s['reports_sent']} relatórios)")

        print("8) desligar")
        miner.stop()
        time.sleep(0.3)
        check(not multiprocessing.active_children(), "nenhum processo minerador sobrando")
        check(not miner.stats()["running"], "stats mostra desligado")

        reps = get("/reports")
        mine = [r for r in reps if r["ident"] == addr + ".ab12cd34"]
        windows = [r["window"] for r in mine]
        check(len(windows) == len(set(windows)), f"um relatório por janela por identidade ({len(mine)} relatórios)")
        check(all(r["verified"] for r in reps), "todos os shares conferidos com sigma.pow.share_ok")
        check(all(r["shares"] <= 32 for r in reps), "nunca mais que max_shares")
        late = [r for r in mine if r["received_in_window"] != r["window"] + 1]
        print(f"   relatórios fora da janela seguinte (o último, no desligamento, é da janela atual): {len(late)}")
        for r in mine:
            print(f"   janela {r['window']}: {r['shares']:2d} provas x 2^{r['bits']} = {r['points']:,} pontos")
        print("TUDO OK")
    finally:
        for m in (miner, miner2):
            if m and m.stats()["running"]:
                m.stop(report=False)
        node.terminate()
        node.wait(5)


if __name__ == "__main__":
    main()
