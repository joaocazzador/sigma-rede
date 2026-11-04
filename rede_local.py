"""Rede Sigma local que fica rodando: um nó validador e o app, tudo nesta máquina.

    python rede_local.py              nó + app sem janela (--headless): o endereço do painel aparece aqui
    python rede_local.py --janela     nó + app com o painel numa janela e a janelinha de controle
    python rede_local.py --sem-modelo sem o modelo de teste

Outras opções vão direto para o app (ex.: --wallet sig…, --cpu 25, --no-ai, --engine ollama, --port 47830).
Os dados ficam em rede-local/ (ou em --dados PASTA) e continuam entre execuções. O nó usa parâmetros mais
rápidos que a rede principal (período de 120 s, janela de 10 s, shares de 8 bits): a emissão cai em todo bloco e
aparece em poucos minutos. O app usa o motor de IA de teste ("mock") a não ser que você passe --engine.

Modelo de teste: assim que a carteira do app tiver SIG (a emissão cai a cada bloco), este script manda 6 SIG pelo
painel do app para a carteira de um padrinho de teste (<dados>/padrinho.key), que coloca na rede o
"modelo-teste" (um GGUF falso, servido daqui mesmo). A rede atribui o modelo ao computador, que baixa, confere
e passa a responder na aba IA e no Programar do painel. Nada é baixado da internet.
Ctrl+C (ou "Desligar o Sigma" no painel) desliga tudo.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sigma.client import Client, SigmaError  # noqa: E402
from sigma.crypto import Wallet  # noqa: E402
from sigma.engines import sha256_file, write_test_gguf  # noqa: E402

FAST = ["--epoch-seconds", "120", "--window-seconds", "10", "--min-bits", "8", "--hosting-per-mb-window", "5"]
LONGEST = 90       # o caminho mais fundo que o app grava: \models\<sha256>.gguf.part (com folga)
MODEL = "modelo-teste"
MODEL_SIZE = 1_000_000
MODEL_BUDGET = "5"           # SIG: 1 MB de disco + a memória que ele reserva (~1.000 µSIG por janela): dura horas
GIFT = "6"                   # SIG que o app manda ao padrinho de teste (o orçamento do modelo e o resto)
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # tudo aqui é 127.0.0.1
# processos em grupo próprio: o Ctrl+C fica com este script, que desliga o app com calma
GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def long_paths_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def app_dir(d):
    """<dados>/app; numa pasta funda demais para o Windows (mais de 260 caracteres sem caminhos longos), uma
    pasta curta em %LOCALAPPDATA%, senão os modelos e sites que a rede manda não gravam."""
    here = os.path.join(d, "app")
    if os.name != "nt" or len(here) + LONGEST < 260 or long_paths_enabled():
        return here
    return os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "Sigma-rede-local")


def old_genesis(path):
    """Genesis de uma versão anterior: com SIG fora da mineração (v1) ou sem o aluguel da rede no genesis
    (v2.0). As regras mudaram, então o nó novo não reexecuta essa chain."""
    try:
        with open(path, encoding="utf-8") as f:
            g = json.load(f)
    except (OSError, ValueError):
        return False
    params = g.get("params")
    return bool(g.get("alloc")) or not isinstance(params, dict) or "hosting_per_mb_window" not in params


def put_aside_old(d):
    """Guarda a rede local antiga em <dados>/antiga-<data> (nada é apagado)."""
    dest = os.path.join(d, "antiga-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dest)
    for name in ("no", "provedor", "provedor.key"):
        if os.path.exists(os.path.join(d, name)):
            os.replace(os.path.join(d, name), os.path.join(dest, name))
    print(f"a rede local antiga (de uma versão anterior do Sigma) foi guardada em {dest}; criando uma nova")


def port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def node_up(node):
    try:
        with NO_PROXY.open(node + "/status", timeout=2) as r:
            return json.loads(r.read()).get("network") == "sigma"
    except (OSError, ValueError):
        return False


def session(folder):
    try:
        with open(os.path.join(folder, "session.json"), encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s.get("port"), int) and s.get("token") else None
    except (OSError, ValueError, AttributeError):
        return None


def panel(folder, path, body=None):
    """Chama o painel local do app (o mesmo que a janela usa), com o token da sessão dele."""
    s = session(folder)
    if not s:
        raise OSError("o painel do app ainda não abriu")
    headers = {"X-Sigma-Token": s["token"]}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{s['port']}{path}", data=data, headers=headers)
    try:
        with NO_PROXY.open(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            try:
                msg = json.loads(e.read()).get("error")
            except ValueError:
                msg = None
        raise OSError(msg or f"o painel respondeu HTTP {e.code}") from None


def quit_app(folder):
    """Desliga o app pelo painel dele (o mesmo botão Desligar): o minerador e a máquina param com calma."""
    try:
        panel(folder, "/api/quit", {})
    except OSError:
        pass


def stop(procs, folder):
    app = procs[1] if len(procs) > 1 else None
    if app and app.poll() is None:
        quit_app(folder)
        try:
            app.wait(20)
        except subprocess.TimeoutExpired:
            pass
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


# ------------------------------------------------------------ modelo de teste
class Files(BaseHTTPRequestHandler):
    """Serve só o GGUF do modelo de teste (o "Hugging Face" da rede local)."""
    path_on_disk = None

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if self.path.split("?", 1)[0] != f"/{MODEL}.gguf" or not self.path_on_disk:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(os.path.getsize(self.path_on_disk)))
        self.end_headers()
        if not head:
            with open(self.path_on_disk, "rb") as f:
                try:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass


class TestModel:
    """Coloca o modelo de teste na rede quando a carteira do app tiver SIG (uma vez; ele fica na chain)."""

    def __init__(self, d, node, folder, port):
        self.node, self.folder, self.port = node, folder, port
        self.gguf = os.path.join(d, f"{MODEL}.gguf")
        self.padrinho = Wallet.load_or_create(os.path.join(d, "padrinho.key"))
        self.stop = threading.Event()
        self.srv = None

    def start(self):
        if not os.path.isfile(self.gguf):
            write_test_gguf(self.gguf, MODEL_SIZE)
        Files.path_on_disk = self.gguf
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), Files)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        threading.Thread(target=self._run, name="modelo-de-teste", daemon=True).start()

    def close(self):
        self.stop.set()
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()

    def _say(self, msg):
        print(f"[rede local] {msg}", flush=True)

    def _run(self):
        try:
            self._place()
        except Exception as e:   # o resto da rede local segue mesmo sem o modelo de teste
            self._say(f"não consegui colocar o {MODEL} na rede: {e}")

    def _place(self):
        c = Client(self.node, self.padrinho)
        sent = 0.0
        while not self.stop.is_set():
            try:
                if MODEL in c.models():
                    return self._say(f"o {MODEL} já está na rede (o app responde por ele na aba IA)")
                if c.balance() >= 5 * 10 ** 6:
                    break
                st = panel(self.folder, "/api/state")
                w = st.get("wallet") or {}
                if w.get("address") and not w.get("can_spend"):
                    return self._say(f"a carteira do app está no modo só endereço: o {MODEL} não entra sozinho "
                                     "(apadrinhe um modelo pelo painel, aba Modelos)")
                if (w.get("balance") or 0) >= 7 * 10 ** 6 and time.time() - sent > 60:
                    sent = time.time()   # uma vez (o envio espera o bloco; o saldo do padrinho chega logo)
                    panel(self.folder, "/api/send", {"to": self.padrinho.address, "amount": GIFT})
                    self._say(f"o app mandou {GIFT} SIG para o padrinho de teste ({self.padrinho.address[:12]}…)")
            except (OSError, SigmaError, ValueError, KeyError, TypeError):
                pass   # painel ou nó ainda subindo, ou o SIG ainda não chegou
            self.stop.wait(5)
        if self.stop.is_set():
            return
        url = f"http://127.0.0.1:{self.port}/{MODEL}.gguf"
        c.register_model(MODEL, url, sha256_file(self.gguf), os.path.getsize(self.gguf), budget_sig=MODEL_BUDGET,
                         royalty_pct=10, replicas=1, license="teste",
                         description="modelo de teste da rede local (pesos falsos, motor mock)")
        self._say(f"o padrinho de teste colocou o {MODEL} na rede ({url}): o computador baixa, confere e passa a "
                  "responder na aba IA e no Programar")


def _interrupt(*_):
    raise KeyboardInterrupt


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Ctrl+Break, fechar o console ou um kill: desliga o app e o nó com calma, como no Ctrl+C
    for name in ("SIGBREAK", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _interrupt)
    # sem abreviações: o --port do app não pode virar o --porta do nó
    ap = argparse.ArgumentParser(description="Sobe uma rede Sigma local (nó + app). As opções que este script "
                                 "não conhece vão para o app (ex.: --wallet, --cpu, --no-ai, --engine, --port).",
                                 allow_abbrev=False)
    ap.add_argument("--janela", action="store_true",
                    help="abre o painel numa janela (padrão: o app roda --headless e o endereço do painel aparece "
                         "aqui)")
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)   # já é o padrão; aceito por costume
    ap.add_argument("--dados", default=os.path.join(HERE, "rede-local"), help="pasta dos dados (padrão: rede-local/)")
    ap.add_argument("--porta", type=int, default=8700, help="porta do nó (padrão 8700)")
    ap.add_argument("--sem-modelo", action="store_true", help="não coloca o modelo de teste na rede")
    args, extra = ap.parse_known_args()
    d = os.path.abspath(args.dados)
    node = f"http://127.0.0.1:{args.porta}"
    node_dir = os.path.join(d, "no")
    files_port = args.porta + 19
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if node_up(node) or not port_free(args.porta):
        raise SystemExit(f"a porta {args.porta} já está em uso (outro rede_local.py ou a demo ainda rodando?)")
    if not args.sem_modelo and not port_free(files_port):
        raise SystemExit(f"a porta {files_port} (arquivo do modelo de teste) está em uso: use --porta com outro "
                         "número ou --sem-modelo")
    genesis = os.path.join(node_dir, "genesis.json")
    if old_genesis(genesis):
        put_aside_old(d)
    if not os.path.exists(genesis):
        r = subprocess.run([sys.executable, "-m", "sigma.node", "init", "--dir", node_dir, *FAST], cwd=HERE, env=env)
        if r.returncode:
            raise SystemExit(r.returncode)
    folder = app_dir(d)
    procs, model = [], None
    try:
        procs.append(subprocess.Popen([sys.executable, "-m", "sigma.node", "run", "--dir", node_dir,
                                       "--port", str(args.porta), "--producer"], cwd=HERE, env=env, **GROUP))
        deadline = time.time() + 60   # chain longa demora um pouco para ser relida
        while not node_up(node):
            if procs[0].poll() is not None or time.time() > deadline:
                raise SystemExit("o nó não subiu (veja as mensagens acima)")
            time.sleep(0.3)
        cmd = [sys.executable, "-m", "sigma.app", "--node", node, "--data-dir", folder, *extra]
        if not any(a == "--engine" or a.startswith("--engine=") for a in extra):
            cmd += ["--engine", "mock"]   # o modelo de teste é falso: só o motor de teste responde por ele
        if not args.janela:   # como na demo: o app sem janela; o painel continua no endereço impresso abaixo
            cmd.append("--headless")
        started = time.time()
        procs.append(subprocess.Popen(cmd, cwd=HERE, env=env, stdin=subprocess.DEVNULL, **GROUP))
        fresh = False   # o app que acabamos de abrir gravou a sessão dele (é ele que está no ar)
        while procs[1].poll() is None and time.time() - started < 20:   # espera o painel do app abrir
            s = session(folder)
            if s and s.get("started", 0) >= started - 1:
                fresh = True
                break
            time.sleep(0.3)
        s = session(folder)
        print(f"\nrede local no ar · site e carteira do nó: {node}/"
              + (f" · painel do app: http://127.0.0.1:{s['port']}/#t={s['token']}" if s else "")
              + f"\ndados: {d}" + (f" e {folder}" if not folder.startswith(d) else "")
              + " · Ctrl+C desliga tudo\n", flush=True)
        watch = procs
        if not fresh and procs[1].poll() == 0:
            # saiu sem abrir painel próprio: o app desta pasta já estava aberto e só trouxe a janela para a frente
            print("o app desta pasta já estava rodando (a janela dele foi para a frente); "
                  "o nó segue no ar até o Ctrl+C", flush=True)
            watch = procs[:1]
        elif not args.sem_modelo:
            model = TestModel(d, node, folder, files_port)
            model.start()
        while not any(p.poll() is not None for p in watch):   # o app desligado pelo painel encerra tudo
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\ndesligando…", flush=True)
    finally:
        if model:
            model.close()
        stop(procs, folder)


if __name__ == "__main__":
    main()
