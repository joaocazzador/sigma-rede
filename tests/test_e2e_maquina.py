"""Ponta a ponta com o nó de verdade (portas 8746 e 8747): minerador + máquina hospedeira + cliente.

    python tests/test_e2e_maquina.py

Genesis rápido (janela 4 s, época 20 s, aluguel da rede de 5 µSIG por MB por janela). O minerador relata provas
com a identidade "carteira.computador", a época emite SIG para a carteira, a carteira patrocina um modelo (GGUF
de teste válido, servido localmente), a rede atribui o modelo à máquina, que baixa, confere (SHA-256 e formato
GGUF), carrega no motor mock e anuncia. Depois: um pedido de IA cifrado e pago em SIG (o nó só guarda bytes
cifrados), um pedido cancelado antes de a máquina pegar e uma conversa longa demais que volta como falha
"context" com o valor devolvido. As portas não cruzam com as do test_engines.py (8730-8740).
"""
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import sigma.client  # noqa: E402
from sigma.client import Client, SigmaError  # noqa: E402
from sigma.crypto import Wallet, is_sealed  # noqa: E402
from sigma.engines import write_test_gguf  # noqa: E402
from sigma.host import Host  # noqa: E402
from sigma.miner import Miner  # noqa: E402

PORT, FILES = 8746, 8747
NODE = f"http://127.0.0.1:{PORT}"


def check(cond, msg):
    print(("  ok   " if cond else "  FALHOU ") + msg, flush=True)
    if not cond:
        raise SystemExit(1)


def wait(cond, what, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = cond()
            if v:
                return v
        except (SigmaError, KeyError, TypeError):
            pass
        time.sleep(0.5)
    raise SystemExit(f"  FALHOU tempo esgotado esperando: {what}")


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def settled(c, addr):
    """Saldo quando nenhum pedido da carteira segura valor reservado."""
    def ok():
        busy = [j for j in c.jobs() if j.get("client") == addr and j.get("status") in ("open", "running")]
        return None if busy else (c.balance(addr),)
    return wait(ok, "o valor reservado voltar", 30)[0]


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    tmp = tempfile.mkdtemp(prefix="sigma-e2e-")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    node_dir = os.path.join(tmp, "no")
    subprocess.run([sys.executable, "-m", "sigma.node", "init", "--dir", node_dir, "--epoch-seconds", "20",
                    "--window-seconds", "4", "--min-bits", "4", "--hosting-per-mb-window", "5"], cwd=ROOT, env=env,
                   check=True, stdout=subprocess.DEVNULL)
    log = open(os.path.join(tmp, "no.log"), "w", encoding="utf-8")
    node = subprocess.Popen([sys.executable, "-m", "sigma.node", "run", "--dir", node_dir, "--port", str(PORT),
                             "--producer"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    files = os.path.join(tmp, "arquivos")
    os.makedirs(files)
    gguf_path = os.path.join(files, "falso.gguf")
    digest = write_test_gguf(gguf_path, 500_000)   # GGUF válido: a máquina confere o formato antes de carregar
    srv = ThreadingHTTPServer(("127.0.0.1", FILES), functools.partial(Quiet, directory=files))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    miner = host = None
    try:
        c = Client(NODE)
        wait(lambda: c.status()["height"] >= 1, "nó no ar", 30)
        check(c.mining().get("hosting_per_mb_window") == 5, "aluguel da rede no /mining (genesis)")
        user = Wallet.generate()
        machine_key = Wallet.generate()
        logs = []

        def say(m):
            logs.append(m)
            print("   ", m, flush=True)

        print("1) minerar até a primeira emissão (identidade carteira.computador)")
        worker = machine_key.address[3:11]
        miner = Miner(Client(NODE), user.address, workers=2, log=say, worker=worker)
        miner.start()
        wait(lambda: miner.stats()["reports_sent"] >= 2, "2 relatórios aceitos pelo nó", 40)
        st = miner.stats()
        check(st["worker"] == worker and st["fail_streak"] == 0 and st["reports_failed"] == 0,
              f"relatórios com worker {worker} aceitos ({st['last_fail']})")
        wait(lambda: c.miner(user.address)["points"] > 0 or c.balance(user.address) > 0, "pontos na época", 15)
        wait(lambda: c.balance(user.address) > 0, "emissão da época para o minerador", 40)
        bal = c.balance(user.address)
        info = c.mining()
        check(bal > 0 and info["minted"] >= bal,
              f"saldo minerado: {bal / 1e6:,.2f} SIG (emitido {info['minted'] / 1e6:,.2f})")
        hist = c.history(user.address)
        check(any(h.get("type") == "mint" for h in hist), "histórico mostra a recompensa de mineração")

        print("2) máquina hospedeira (motor mock)")
        host = Host(NODE, machine_key, user.address, os.path.join(tmp, "app"),
                    {"engine": "mock", "disk_gb": 2, "ai_ram_gb": 3, "name": "pc-teste"}, miner=miner, log=say)
        host.start()
        wait(lambda: host.stats()["registered"] and host.stats()["online"], "máquina registrada e online", 30)
        rec = c.machine(machine_key.address)
        check(rec["payout"] == user.address and rec["ai"] and rec["hw"]["ai_ram_mb"] == int(3 * 1073.741824),
              f"registro na chain: ai={rec['ai']} ai_ram_mb={rec['hw']['ai_ram_mb']} (3 GB do Windows)")
        check(rec.get("pub") == machine_key.pub and set(rec["prices"]) == {"ai_per_1k_tokens", "compute_per_unit"},
              "chave pública publicada e preços sem hospedagem (o aluguel é da rede)")
        seen = rec["last_seen"]
        wait(lambda: c.machine(machine_key.address)["last_seen"] > seen, "ping na janela seguinte", 20)

        print("3) padrinho registra um modelo; a rede escolhe a máquina")
        cu = Client(NODE, user)
        cu.register_model("modelo-falso", f"http://127.0.0.1:{FILES}/falso.gguf", digest, os.path.getsize(gguf_path),
                          budget_sig=50, replicas=1, description="teste", license="MIT")
        wait(lambda: any(a["name"] == "modelo-falso" and a["status"] == "pinned"
                         for a in c.machine(machine_key.address)["assignments"]), "modelo baixado e anunciado", 60)
        check("modelo-falso" in c.machine(machine_key.address)["models"], "máquina atende o modelo")
        # o pin entra no bloco um instante antes de o host marcar "hospedando" (ele espera a tx e relê o registro)
        st = wait(lambda: (lambda s: s if [m["status"] for m in s["models"]] == ["hospedando"] else None)(
            host.stats()), "o painel da máquina mostrar 'hospedando'", 15)
        check([m["status"] for m in st["models"]] == ["hospedando"], f"stats: {st['models']}")

        print("4) pedido de IA cifrado, pago em SIG")
        question = "olá máquina, tudo certo por aí?"
        text, job = cu.ask("modelo-falso", [{"role": "user", "content": question}], max_tokens=64, timeout=60)
        check("sigma-mock" in text and job["machine"] == machine_key.address, f"resposta: {text[:60]}…")
        spec = c.get_blob(job["spec_hash"])
        check(is_sealed(spec) and question.encode() not in spec, "o pedido no nó está cifrado (a pergunta não aparece)")
        res = c.get_blob(job["result_hash"])
        check(is_sealed(res) and json.loads(user.unseal(res))["text"] == text,
              "a resposta abre com a chave de quem pediu")
        try:
            Wallet.generate().unseal(res)
            other = True
        except ValueError:
            other = False
        check(not other, "a resposta não abre com outra chave")
        check(not any(question in str(m) for m in logs), "a pergunta não aparece no registro da máquina")
        s = miner.stats()
        check(s["running"] and not s["paused"], "minerador retomado depois da inferência")
        wait(lambda: c.machine(machine_key.address)["jobs_done"] >= 1, "job contado na máquina", 10)

        print("5) cancelar um pedido que a máquina ainda não pegou")
        # quem pergunta agora é outra carteira: a do minerador também recebe a emissão e o aluguel da máquina
        asker = Wallet.generate()
        cu.transfer(asker.address, 5)
        ca = Client(NODE, asker)
        before = settled(c, asker.address)
        check(before == 5_000_000, "a carteira que pergunta recebeu 5 SIG")
        jobs = []
        host._take_targeted = lambda job: None   # a máquina "dormiu": continua online na chain, mas não pega nada
        try:
            ca.ask("modelo-falso", [{"role": "user", "content": "ainda aí?"}], max_tokens=32, timeout=60,
                   on_job=jobs.append, should_stop=lambda: bool(jobs))
            canceled = False
        except SigmaError as e:
            canceled = str(e) == sigma.client.CANCELED
        finally:
            del host._take_targeted
        j = c.job(jobs[-1]) if jobs else {}
        check(canceled and j.get("status") == "canceled", f"pedido cancelado ({j.get('status')})")
        check(settled(c, asker.address) == before, "o valor reservado voltou inteiro")

        print("6) conversa longa demais: falha \"context\" com o valor devolvido")
        jobs.clear()
        with mock.patch.object(sigma.client, "CONTEXT", 1_000_000):   # o cliente oficial cortaria antes de mandar
            try:
                ca.ask("modelo-falso", [{"role": "user", "content": "x" * 20_000}], max_tokens=64, timeout=60,
                       on_job=jobs.append)
                reason = None
            except SigmaError as e:
                reason = str(e)
        j = c.job(jobs[-1]) if jobs else {}
        check(reason is not None and "limite do modelo" in reason, f"o cliente mostra a frase da máquina: {reason}")
        check(j.get("status") == "failed" and (j.get("cost") or 0) == 0, f"job {j.get('status')} sem custo")
        check(settled(c, asker.address) == before, "o valor reservado voltou inteiro")
        kinds = {h.get("kind") for h in c.history(asker.address) if h.get("type") == "credit"}
        check("refund" in kinds, "o histórico da carteira mostra a devolução")
        wait(lambda: c.machine(machine_key.address).get("jobs_failed", 0) >= 1, "falha contada na máquina", 10)
        print("TUDO OK")
    finally:
        if host:
            host.stop()
        if miner:
            miner.stop(report=False)
        srv.shutdown()
        srv.server_close()
        node.terminate()
        node.wait(10)
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
