"""Demo ponta a ponta da rede Sigma, tudo nesta máquina (nada é baixado da internet).

A rede nasce sem nenhum SIG. Três computadores rodam o app (sem janela) e mineram, cada um com a sua
identidade (carteira.computador); a emissão (0,01% do que falta a cada 12 h) cai em todo bloco para eles, pelo trabalho de
cada um. Daí em diante o SIG só circula: um padrinho coloca um modelo de IA na rede (a rede escolhe a máquina,
que baixa, confere e carrega sozinha), uma usuária conversa com a IA em pedidos cifrados (o nó só guarda bytes
cifrados), cancela uma pergunta que nenhuma máquina pegou, recebe de volta o valor de uma conversa longa demais,
publica um site e compra computação verificada (quem diverge da maioria não recebe, mas não perde reputação nem
mineração), um modelo sem orçamento some da rede e do disco de quem o hospedava, e a rede abre uma votação para
mudar o próprio aluguel (1 SIG = 1 voto, travado até o fim).

    python demo.py                        IA de teste (motor "mock"): sem GPU e sem modelo de verdade
    python demo.py --ollama               IA de verdade: o qwen2.5-coder:1.5b que já está no seu Ollama
    python demo.py --ollama llama3.2:1b   outro modelo que já está no seu Ollama
    python demo.py --manter               no fim, deixa tudo rodando para explorar (Ctrl+C desliga)

Portas: 8700 e 8701 (nós), 47911-47913 (painéis dos apps), 47919 (arquivos do padrinho).
Dados e registros de cada processo: demo-data/ (veja data_root()).
"""
import argparse
import json
import math
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
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import sigma.client
from sigma.chain import UNIT
from sigma.client import Client, SigmaError
from sigma.crypto import Wallet, is_sealed
from sigma.engines import write_test_gguf

HERE = os.path.dirname(os.path.abspath(__file__))
A, B = "http://127.0.0.1:8700", "http://127.0.0.1:8701"
FILES_PORT = 47919
PORTS = (8700, 8701, 47911, 47912, 47913, FILES_PORT)
HOSTING = 5        # µSIG por MB por janela: o aluguel é preço da rede, gravado no genesis
FAST = ["--epoch-seconds", "20", "--window-seconds", "4", "--min-bits", "4", "--hosting-per-mb-window", str(HOSTING)]
OLLAMA = "http://127.0.0.1:11434"
DEFAULT_OLLAMA = "qwen2.5-coder:1.5b"
LONGEST = 93       # o caminho mais fundo que um app grava: \trapaceiro\models\<sha256>.gguf.part
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # tudo aqui é 127.0.0.1
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
# processos em grupo próprio: o Ctrl+C fica com a demo, que desliga cada um com calma
GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
COLOR = sys.stdout.isatty()
PROCS = []         # (nome, processo, pasta de dados do app ou None, arquivo de log)
MACHINE, NAME = {}, {}   # nome -> endereço da máquina e o contrário
D = None


# ------------------------------------------------------------------ utilidades
def long_paths_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def data_root():
    """demo-data/ ao lado do código. Se a pasta for funda demais para o Windows (mais de 260 caracteres
    sem o suporte a caminhos longos ligado), usa uma pasta curta no TEMP: senão os pesos não gravam."""
    here = os.path.join(HERE, "demo-data")
    if os.name != "nt" or len(here) + LONGEST < 260 or long_paths_enabled():
        return here
    return os.path.join(tempfile.gettempdir(), "sigma-demo")


def rel(path):
    try:
        r = os.path.relpath(path, HERE)
        return path if r.startswith("..") else r
    except ValueError:   # outro disco
        return path


def title(t):
    print(f"\n\033[1;31m━━ {t}\033[0m" if COLOR else f"\n━━ {t}", flush=True)


def sig(m, casas=6):
    """µSIG -> '1.234,567890 SIG' (como se escreve no Brasil)."""
    return f"{m / UNIT:,.{casas}f}".replace(",", "_").replace(".", ",").replace("_", ".") + " SIG"


def num(n):
    return f"{n:,}".replace(",", ".")


def rate(h):
    """O mesmo formato do painel: contas por segundo."""
    for unit, div in (("milhões de contas/s", 1e6), ("mil contas/s", 1e3)):
        if h >= div:
            return f"{h / div:.1f} {unit}".replace(".", ",")
    return f"{h:.0f} contas/s"


def enable_ansi():
    if os.name == "nt" and COLOR:   # cores no console antigo do Windows
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h, mode = k.GetStdHandle(-11), ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 4)
        except Exception:
            pass


def port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ------------------------------------------------------------------ processos
def spawn(name, args, app_dir=None):
    log = open(os.path.join(D, "logs", name + ".log"), "w", encoding="utf-8")
    p = subprocess.Popen([sys.executable, "-m", *args], cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, env=ENV, **GROUP)
    PROCS.append((name, p, app_dir, log))
    return p


def proc(name):
    return next(p for n, p, _, _ in PROCS if n == name)


def wait(cond, what, timeout=60):
    """Espera cond() devolver algo verdadeiro e devolve isso. Se um processo da demo cair, para na hora."""
    deadline, last = time.time() + timeout, None
    while True:
        for name, p, _, _ in PROCS:
            if p.poll() is not None:
                where = rel(os.path.join(D, "logs", name))
                raise SystemExit(f"\n{name} parou (código {p.returncode}): veja {where}.log")
        try:
            v = cond()
            if v:
                return v
        except (SigmaError, OSError, KeyError, ValueError, TypeError) as e:   # nó B ainda sincronizando etc.
            last = e
        if time.time() >= deadline:
            raise SystemExit(f"\ntempo esgotado esperando {what}" + (f" (último erro: {last})" if last else "")
                             + f": veja os registros em {rel(os.path.join(D, 'logs'))}")
        time.sleep(0.5)


def freeze(p, on):
    """Congela (ou descongela) o processo de um app: para a rede, é como se aquele computador tivesse dormido
    de repente (ele continua aparecendo online até perder algumas janelas sem sinal)."""
    if os.name != "nt":
        os.kill(p.pid, signal.SIGSTOP if on else signal.SIGCONT)
        return
    import ctypes
    from ctypes import wintypes
    k32, nt = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype, k32.OpenProcess.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL,
                                                                          wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    fn = nt.NtSuspendProcess if on else nt.NtResumeProcess
    fn.restype, fn.argtypes = ctypes.c_long, [wintypes.HANDLE]
    h = k32.OpenProcess(0x0800, False, p.pid)   # PROCESS_SUSPEND_RESUME
    if not h:
        raise OSError(ctypes.get_last_error(), "não consegui abrir o processo do app")
    try:
        if fn(h) != 0:
            raise OSError("não consegui " + ("congelar" if on else "descongelar") + " o processo do app")
    finally:
        k32.CloseHandle(h)


def session(app_dir):
    with open(os.path.join(app_dir, "session.json"), encoding="utf-8") as f:
        return json.load(f)


def panel(name, path="/api/state"):
    """Consulta o painel local do app (o mesmo que a janela do Sigma mostra), com o token da sessão dele."""
    s = session(os.path.join(D, name))
    req = urllib.request.Request(f"http://127.0.0.1:{s['port']}{path}", headers={"X-Sigma-Token": s["token"]})
    with NO_PROXY.open(req, timeout=5) as r:
        return json.loads(r.read())


def quit_app(app_dir):
    """Desliga o app pelo painel dele (o mesmo botão Desligar): para o minerador e a máquina com calma."""
    try:
        s = session(app_dir)
        req = urllib.request.Request(f"http://127.0.0.1:{s['port']}/api/quit", data=b"{}", method="POST",
                                     headers={"X-Sigma-Token": s["token"], "Content-Type": "application/json"})
        NO_PROXY.open(req, timeout=3).close()
    except (OSError, ValueError, KeyError, TypeError):
        pass


def stop_all():
    for _, p, app_dir, _ in PROCS:
        if app_dir and p.poll() is None:
            quit_app(app_dir)
    deadline = time.time() + 20
    for _, p, app_dir, _ in PROCS:
        if app_dir:
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                pass
    for _, p, _, _ in PROCS:
        if p.poll() is None:
            p.terminate()
    for _, p, _, log in PROCS:
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()


# -------------------------------------------------------- arquivos do padrinho
class Files(BaseHTTPRequestHandler):
    """O "Hugging Face" da demo: serve só os GGUF que o padrinho registrou aqui."""
    files = {}

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        path = self.files.get(self.path.split("?", 1)[0].lstrip("/"))
        if not path:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.end_headers()
        if not head:
            with open(path, "rb") as f:
                try:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass   # a máquina desistiu no meio; ela continua depois


def file_server():
    srv = ThreadingHTTPServer(("127.0.0.1", FILES_PORT), Files)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def offer(path, name):
    Files.files[name] = path
    return f"http://127.0.0.1:{FILES_PORT}/{name}"


def fake_gguf(name, size):
    """Pesos falsos, mas um GGUF válido (as máquinas conferem o SHA-256 e o formato antes de carregar)."""
    path = os.path.join(D, "padrinho", f"{name}.gguf")
    return path, write_test_gguf(path, size), size


def ollama_gguf(tag):
    """O GGUF de um modelo que já está no seu Ollama (o arquivo de pesos, pelo manifesto). Nada é baixado."""
    try:
        NO_PROXY.open(OLLAMA + "/api/version", timeout=3).close()
    except OSError:
        raise SystemExit(f"o Ollama não está respondendo em {OLLAMA}: abra o Ollama e rode a demo de novo")
    repo, _, version = tag.partition(":")
    ns, _, model = repo.rpartition("/")
    root = os.environ.get("OLLAMA_MODELS") or os.path.join(os.path.expanduser("~"), ".ollama", "models")
    for sub in (("manifests", "registry.ollama.ai"), ("manifests-v2", "ollama.com")):
        man = os.path.join(root, *sub, ns or "library", model, version or "latest")
        try:
            with open(man, encoding="utf-8") as f:
                layer = next(x for x in json.load(f)["layers"] if x["mediaType"].endswith(".model"))
        except (OSError, ValueError, KeyError, StopIteration):
            continue
        digest = layer["digest"].split(":", 1)[1]
        blob = os.path.join(root, "blobs", "sha256-" + digest)
        if os.path.isfile(blob):
            return blob, digest, os.path.getsize(blob)
    raise SystemExit(f"o modelo {tag} não está no seu Ollama. Baixe antes com: ollama pull {tag}")


def ollama_tags():
    with NO_PROXY.open(OLLAMA + "/api/tags", timeout=5) as r:
        return {m["name"].removesuffix(":latest") for m in json.load(r).get("models", [])}


def ollama_forget(model):
    """Se a demo parou no meio, tira do Ollama o sigma-<modelo> que a máquina da demo criou (só esse)."""
    tag = f"sigma-{model}"
    try:
        if tag not in ollama_tags():
            return
        req = urllib.request.Request(OLLAMA + "/api/delete", data=json.dumps({"model": tag}).encode(),
                                     method="DELETE", headers={"Content-Type": "application/json"})
        NO_PROXY.open(req, timeout=30).close()
        print(f"  (tirei do seu Ollama o {tag} que a demo tinha criado)")
    except (OSError, ValueError):
        pass


def write_config(folder, settings):
    """Configurações iniciais do app (o mesmo config.json que o painel grava)."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 2, "settings": settings}, f, indent=2)


# --------------------------------------------------------------- consultas
def online(names):
    ms = {m.get("name"): (a, m) for a, m in Client(B).machines().items() if m.get("online")}
    return ms if all(n in ms for n in names) else None


def computers_online(n):
    return sum(1 for m in Client(A).machines().values() if m.get("online") and m.get("compute")) >= n


def placed(model, seen):
    """Máquina que já hospeda o modelo (pinned); avisa quando a rede atribui."""
    m = Client(B).models().get(model)
    if not m:
        return None
    for a, w in (m.get("assigned") or {}).items():
        if a not in seen:
            seen.add(a)
            print(f"  janela {w}: a rede escolheu {NAME.get(a, a[:12])} → ela baixa, confere o SHA-256 e o formato "
                  "GGUF e carrega")
    pins = [a for a in m.get("pinners") or {} if model in (Client(B).machine(a).get("models") or [])]
    return pins[0] if pins else None


def model_file(machine, digest):
    return os.path.join(D, NAME[machine], "models", f"{digest}.gguf")


def fetch_site(node, name, site):
    """Abre o site pelo gateway de um nó. Se esse nó ainda não tem os arquivos, ele os busca nos outros
    nós (hash conferido) e guarda em cache."""
    url, warmed = f"{node}/site/{name}/", False
    try:
        r = NO_PROXY.open(url, timeout=10)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code != 404:
            raise
        for f in site["manifest"].values():
            Client(node).get_blob(f["hash"])
        r, warmed = NO_PROXY.open(url, timeout=10), True
    with r:
        return (r.read().decode("utf-8", "replace"), r.headers.get("X-Sigma-Served-By"),
                r.headers.get("Content-Security-Policy") or "", warmed)


def rent_fee(size, mi, model=True):
    """Aluguel de uma cópia por janela, como a chain cobra (chain.copy_fee): disco + memória reservada (modelo),
    cada um com o multiplicador da demanda de agora."""
    rent = mi.get("rent") or {}
    cost = size * mi["hosting_per_mb_window"] * rent.get("disk_bps", 10_000)
    if model:
        cost += (math.ceil(size / 1e6 * 1.2) + 512) * 1_000_000 * mi["ram_per_mb_window"] * rent.get("ram_bps", 10_000)
    return max(1, -(-cost // (1_000_000 * 10_000)))


STATUS = {"done": "respondido", "failed": "encerrado como falha", "canceled": "cancelado", "expired": "expirado",
          "disputed": "sem maioria", "open": "aberto", "running": "em andamento"}


def block_hash(node, h):
    return Client(node).get(f"/blocks?from={h}")["blocks"][0]["hash"]


def money(wallets):
    """(total impresso, saldos + budgets + reservas) lidos no mesmo bloco: os dois têm de bater."""
    c = Client(B)
    for _ in range(20):
        h = c.status()["height"]
        minted = c.mining()["minted"]
        held = sum(c.balance(w.address) for w in wallets)
        held += sum(s.get("budget", 0) for s in c.sites().values())
        held += sum(m.get("budget", 0) for m in c.models().values())
        held += sum(j.get("escrow", 0) for j in c.jobs() if j.get("status") in ("open", "running"))
        held += c.governance().get("locked", 0)          # votos e depósitos travados nas votações abertas
        if c.status()["height"] == h:
            break
    return minted, held


def settled(client, addr):
    """Saldo quando nenhum pedido dela segura valor reservado (o reembolso já entrou): espera e devolve."""
    def check():
        jobs = [j for j in client.jobs() if j.get("client") == addr and j.get("status") in ("open", "running")]
        return None if jobs else (client.balance(addr),)
    return wait(check, "o saldo da carla assentar")[0]


# ---------------------------------------------------------------- a demo
def machine_specs(ollama):
    """(nome, nó, porta do painel, configurações no config.json do app, opções extras)."""
    return [
        # a máquina de IA: é para cá que a rede manda os modelos
        ("alice-ia", A, 47911, {"engine": "ollama" if ollama else "mock", "ai_ram_gb": 2}, []),
        # com o Ollama, o bruno também roda IA (de teste) com 1,5 GB: só cabem modelos pequenos
        ("bruno-pc", B, 47912, {"engine": "mock", "ai_ram_gb": 1.5}, [] if ollama else ["--no-ai"]),
        # "cheat" é uma configuração escondida do host, só para a demo mostrar a punição
        ("trapaceiro", B, 47913, {"engine": "mock", "cheat": True}, ["--no-ai"]),
    ]


def private_chat(carla, wallets, model, text, job, question):
    """O que o nó guardou dessa conversa: o pedido e a resposta, os dois cifrados."""
    c = Client(A)
    spec = c.get_blob(job["spec_hash"])
    hidden = is_sealed(spec) and question.encode() not in spec and question[:20].encode() not in spec
    res = c.get_blob(job["result_hash"])
    mine = json.loads(wallets["carla"].unseal(res)).get("text") == text if is_sealed(res) else False
    try:
        wallets["bruno-pc"].unseal(res)
        others = True
    except ValueError:
        others = False
    print(f"  o pedido guardado no nó: {num(len(spec))} bytes cifrados para a chave da máquina "
          f"({'a pergunta não aparece neles' if hidden else 'ATENÇÃO: o texto da pergunta aparece no blob'})")
    print(f"  a resposta guardada no nó: {num(len(res))} bytes cifrados; "
          + ("abre com a chave da carla" if mine else "NÃO abriu com a chave da carla")
          + (" e não abre com a de mais ninguém (tentei a do bruno)" if not others else
             " e ABRIU com a chave do bruno (deveria ser só da carla)"))
    print("  continua público: que houve um pedido, de qual carteira, para qual modelo e máquina, quanto custou "
          "e quando; e a máquina que respondeu leu a pergunta (é o computador de outra pessoa)")
    return hidden and mine and not others


def cancel_and_fail(carla, wallets, model, host):
    """Cancelar uma pergunta que ninguém pegou e uma conversa longa demais: nenhuma das duas custa nada."""
    me = wallets["carla"].address
    c = Client(A)
    before = settled(c, me)
    jobs = []
    print(f"  o computador de {NAME[host]} dorme de repente (a demo congela o app dele); para a rede ele ainda "
          "está online")
    freeze(proc(NAME[host]), True)
    try:
        carla.ask(model, [{"role": "user", "content": "Ainda está aí?"}], max_tokens=64,
                  on_job=jobs.append, should_stop=lambda: bool(jobs))   # desiste assim que o pedido entra
        canceled = False
    except SigmaError as e:
        canceled = str(e) == sigma.client.CANCELED
        if not canceled:
            raise
    finally:
        freeze(proc(NAME[host]), False)
    job = c.job(jobs[-1]) if jobs else {}
    after = settled(c, me)
    print(f"  a carla perguntou, cansou de esperar e clicou em Cancelar: pedido "
          f"{STATUS.get(job.get('status'), job.get('status', '?'))} antes de qualquer máquina pegar; reservou "
          f"{sig(job.get('escrow', 0))} e recebeu tudo de volta (saldo {sig(before)} → {sig(after)})")
    print(f"  {NAME[host]} acordou e segue trabalhando (o pedido cancelado não conta como falta dela)")

    long_text = "palavra " * 3000   # ~8 mil tokens: mais que os 4.096 do contexto dos modelos da rede
    jobs.clear()
    before = after
    # o cliente oficial corta a conversa antes de mandar (fit_messages); aqui ele finge não saber o limite
    with mock.patch.object(sigma.client, "CONTEXT", 1_000_000):
        try:
            carla.ask(model, [{"role": "user", "content": long_text}], max_tokens=64, on_job=jobs.append)
            reason = None
        except SigmaError as e:
            reason = str(e)
    job = c.job(jobs[-1]) if jobs else {}
    after = settled(c, me)
    print(f"  uma conversa de {num(len(long_text))} caracteres: {NAME.get(job.get('machine'), 'a máquina')} "
          f"respondeu falha \"context\" ({reason or 'sem motivo'})")
    print(f"  pedido {STATUS.get(job.get('status'), job.get('status', '?'))}, custo {sig(job.get('cost') or 0)}: "
          "o valor reservado "
          f"({sig(job.get('escrow', 0))}) voltou inteiro na hora (saldo {sig(before)} → {sig(after)})")
    return job.get("status") == "failed" and canceled and after == before


def run(args):
    global D
    D = data_root()
    busy = [p for p in PORTS if not port_free(p)]
    if busy:
        raise SystemExit(f"porta(s) ocupada(s): {', '.join(map(str, busy))}. "
                         "Outra demo (ou o rede_local.py) ainda está rodando?")
    gguf = ollama_gguf(args.ollama) if args.ollama else None   # confere o Ollama antes de criar qualquer coisa
    shutil.rmtree(D, ignore_errors=True)
    if os.path.exists(D):
        raise SystemExit(f"não consegui apagar {D}: algum processo ainda usa a pasta?")
    os.makedirs(os.path.join(D, "logs"))
    specs = machine_specs(bool(args.ollama))
    names = [s[0] for s in specs]
    W = {n: Wallet.load_or_create(os.path.join(D, "carteiras", f"{n}.key")) for n in names + ["carla"]}

    title("1. Uma rede nova nasce sem nenhum SIG")
    if D != os.path.join(HERE, "demo-data"):
        print(f"  (dados em {D}: a pasta do projeto é funda demais para os caminhos do Windows)")
    r = subprocess.run([sys.executable, "-m", "sigma.node", "init", "--dir", os.path.join(D, "no-a"), *FAST],
                       cwd=HERE, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise SystemExit(f"o init falhou:\n{r.stdout}{r.stderr}")
    spawn("no-a", ["sigma.node", "run", "--dir", os.path.join(D, "no-a"), "--port", "8700", "--producer",
                   "--peer", B])
    spawn("no-b", ["sigma.node", "run", "--dir", os.path.join(D, "no-b"), "--port", "8701",
                   "--genesis", os.path.join(D, "no-a", "genesis.json"), "--peer", A])
    wait(lambda: Client(A).status()["height"] > 0 and Client(B).status()["height"] > 0, "os nós subirem")
    with open(os.path.join(D, "no-a", "genesis.json"), encoding="utf-8") as f:
        genesis = json.load(f)
    mi = Client(A).mining()
    print(f"  nó A (validador) em {A} · nó B (seguidor) em {B}")
    print(f"  genesis: alloc = {json.dumps(genesis['alloc'])} → ninguém começa com SIG, nem os criadores; "
          "não existe faucet")
    print(f"  emissão: 0,01% do que falta dos {num(mi['cap'] // UNIT)} SIG a cada período (aqui {sig(mi['mint_per_period'], 0)}),"
          " saindo um pouco em todo bloco, pelo tempo que o bloco durou")
    print(f"  aluguel: {mi['hosting_per_mb_window']} µSIG por MB de disco e {mi['ram_per_mb_window']} por MB de memória "
          "reservada, por janela, vezes a demanda da rede (de 1x vazia a 4x cheia)")
    print(f"  período de {mi['epoch_seconds']} s e janela de {mi['window_seconds']} s só na demo "
          "(na rede de verdade: 12 h e 5 min)")

    title("2. Três computadores rodam o app: ele minera e hospeda sozinho")
    for name, node, port, settings, extra in specs:
        folder = os.path.join(D, name)
        write_config(folder, settings)
        spawn(name, ["sigma.app", "--headless", "--node", node, "--data-dir", folder, "--port", str(port),
                     "--wallet", W[name].address, "--cpu", "25", "--disk-gb", "3", "--name", name, *extra], folder)
    reg = wait(lambda: online(names), "as 3 máquinas se registrarem na rede", timeout=120)
    for name in names:
        addr, m = reg[name]
        MACHINE[name], NAME[addr] = addr, name
        jobs = (["IA"] if m["ai"] else []) + (["computação"] if m["compute"] else []) + ["sites"]
        print(f"  {name:<11} máquina {addr[:12]}… recebe em {m['payout'][:12]}… · hospeda: {', '.join(jobs)}"
              f" · {m['hw']['cpu_cores']} núcleos" + (f", {num(m['hw']['ai_ram_mb'])} MB para IA" if m["ai"] else "")
              + (" · publicou a chave para pedidos cifrados" if m.get("ai") and m.get("pub") else ""))
    print("  cada app minera com 25% da CPU; ninguém escolhe modelo, site ou job: a rede decide e o app obedece")

    title("3. A emissão cai em todo bloco, dividida pelo trabalho")
    got = wait(lambda: (lambda b: b if all(b.values()) else None)(
        {n: Client(B).balance(W[n].address) for n in names}), "os três receberem a primeira fatia", timeout=180)
    mi = Client(B).mining()
    print(f"  a rede imprime agora {sig(mi['mint_per_hour'], 0)} por hora (na rede de verdade, ~833 SIG por hora no "
          f"começo), um pouco em cada bloco, para quem relatou trabalho na última janela fechada")
    total = sum(got.values())
    for name in names:
        share = f"{got[name] / total:.1%}".replace(".", ",")
        print(f"  {name:<11} já recebeu {sig(got[name], 2):>18}  ({share})")
    ids = []
    for name in names:   # cada PC tem a própria identidade: vários podem minerar para a mesma carteira
        st = wait(lambda n=name: panel(n), f"o painel de {name}")["mining"]
        ids.append(f"{name} = {W[name].address[:10]}….{st.get('worker')} ({st.get('reports_sent', 0)} relatórios)")
    print("  identidade de mineração (carteira.computador): " + " · ".join(ids))
    mi = Client(B).mining()
    print(f"  poder computacional da rede: {rate(mi['hashrate'])} · sem 'próxima distribuição': cai o tempo todo")

    title("4. Daqui em diante o SIG só circula entre quem usa e quem hospeda")
    bruno, carla = Client(B, W["bruno-pc"]), Client(A, W["carla"])
    wait(lambda: Client(B).balance(W["bruno-pc"].address) >= 2000 * UNIT, "o bruno ter SIG minerado", 120)
    bruno.transfer(W["carla"].address, 1000)
    wait(lambda: Client(A).balance(W["carla"].address) >= 1000 * UNIT, "a transferência chegar")
    print("  o bruno mandou 1.000 SIG para a carla. Ela não minera: só usa a rede (IA, sites, computação)")

    title("5. Um padrinho coloca um modelo de IA na rede; a rede escolhe onde ele fica")
    srv = file_server()
    model = None
    try:
        if args.ollama:
            path, digest, size = gguf
            model = "qwen-coder" if args.ollama == DEFAULT_OLLAMA else \
                "".join(c if c.isalnum() or c in ".-" else "-" for c in args.ollama.lower())[:48]
            license_ = "Apache-2.0" if args.ollama == DEFAULT_OLLAMA else "veja a licença no Ollama"
        else:
            model, license_ = "mock-patrocinado", "teste"
            path, digest, size = fake_gguf(model, 3_000_000)
        url = offer(path, f"{model}.gguf")
        bruno.register_model(model, url, digest, size, budget_sig=100, royalty_pct=10, reinvest_pct=50, replicas=1,
                             description=f"demo: {args.ollama or 'pesos falsos de teste'}", license=license_)
        print(f"  o bruno é o padrinho de '{model}' ({num(round(size / 1e6))} MB, publicado em {url})")
        print("  ele paga a hospedagem (orçamento de 100 SIG) e ganha 10% de cada token gasto; metade volta ao "
              "orçamento")
        host = wait(lambda s=set(): placed(model, s), "a máquina baixar e carregar o modelo",
                    timeout=900 if args.ollama else 120)
        engine = "no Ollama" if args.ollama and NAME[host] == "alice-ia" else "no motor"
        print(f"  {NAME[host]} baixou {rel(model_file(host, digest))}, conferiu o SHA-256 e o formato GGUF, carregou "
              f"{engine} e avisou a rede: já pode atender")

        title("6. A carla conversa com a IA: pedido cifrado, pago em SIG")
        t0 = time.time()
        question = "Explique em uma frase o que é uma blockchain."
        text, job = wait(lambda: carla.ask(model, [{"role": "user", "content": question}], max_tokens=200),
                         "a resposta da IA", timeout=900)
        print("  " + " ".join(text.split())[:400])
        royalty, who = job["royalty"], NAME.get(job.get("machine"), "a máquina")
        print(f"  {time.time() - t0:.0f} s · custo {sig(job['cost'])}: {sig(job['cost'] - royalty)} para {who} e "
              f"{sig(royalty)} de royalty (metade para o bruno, metade de volta ao orçamento do modelo)")
        print("  o que sobrou da reserva voltou para a carla")
        private_chat(carla, W, model, text, job, question)

        title("7. Cancelar ou não caber no modelo não custa nada")
        cancel_and_fail(carla, W, model, host)

        title("8. A carla publica um site; as máquinas guardam cópias e recebem aluguel")
        carla.deploy_site(os.path.join(HERE, "site-exemplo"), "ola-sigma", budget_sig=20, replicas=2)
        site = wait(lambda: (lambda s: s if s and len(s.get("pinners") or {}) >= 2 else None)(
            Client(B).sites().get("ola-sigma")), "2 máquinas hospedarem o site", timeout=150)
        html, served, csp, warmed = wait(lambda: fetch_site(B, "ola-sigma", site), "abrir o site pelo nó B",
                                         timeout=30)
        # as máquinas da demo não têm URL pública: o nó B entrega do cache dele (hash conferido)
        via = f"por {NAME[served]}" if served in NAME else \
            "pelo cache do próprio nó B" if served == "cache-do-gateway" else f"por {served}"
        print(f"  publicado pelo nó A, aberto pelo nó B: {B}/site/ola-sigma/")
        print(f"  cópias em: {', '.join(NAME.get(a, a[:12]) for a in site['pinners'])} · entregue {via} · "
              f"{len(html)} bytes, hash conferido contra a chain")
        if warmed:
            print("  (o nó B ainda não tinha os arquivos: buscou no nó A, conferiu os hashes e guardou em cache)")
        print("  o site roda isolado (" + ("sem cookies, localStorage nem acesso à carteira: "
                                          if "sandbox" in csp and "allow-same-origin" not in csp
                                          else "ATENÇÃO, sem o isolamento esperado: ") + csp + ")")

        title("9. Computação verificada: 3 máquinas fazem a mesma conta e uma trapaceia")
        cheat_pay = W["trapaceiro"].address
        before = Client(A).miner(cheat_pay)
        m0 = Client(A).machine(MACHINE["trapaceiro"])
        rep0, disp0 = m0["reputation"], m0.get("compute_disputes", 0)
        task = {"name": "primes", "params": {"n": 2_000_000}}
        result, job = wait(lambda: computers_online(3) and carla.compute(task, replicas=3),
                           "o job de computação", timeout=300)
        m1 = Client(A).machine(MACHINE["trapaceiro"])
        rep1, disp1 = m1["reputation"], m1.get("compute_disputes", 0)
        print(f"  quantos primos existem até 2.000.000? {result['count'] if result else '?'} "
              f"(job {STATUS.get(job['status'], job['status'])}, custo {sig(job['cost'])})")
        cheated = job["status"] == "done" and MACHINE["trapaceiro"] in job["results"] \
            and MACHINE["trapaceiro"] not in job["payments"]
        for addr in job["results"]:
            paid = job["payments"].get(addr)
            print(f"  {NAME.get(addr, addr[:12]):<11} " + (f"igual à maioria → pago {sig(paid)}" if paid else
                                                          "DIVERGIU da maioria → não recebe nada (fica uma "
                                                          "contestação registrada, sem perder reputação)"))
        if job["status"] != "done":
            print("  (sem maioria entre as réplicas: ninguém recebeu e a reserva voltou para a carla)")
        elif not cheated:
            print("  (o trapaceiro não trapaceou: este app não repassou a configuração 'cheat' do config.json ao host)")
        else:
            print(f"  reputação do trapaceiro: {rep0:+d} → {rep1:+d} · contestações de computação: "
                  f"{disp0} → {disp1} (a divergência vira contestação, não tira reputação)")
            after = wait(lambda: (lambda m: m if m["rewards_total"] > before["rewards_total"] else None)(
                Client(A).miner(cheat_pay)), "o trapaceiro receber mais uma fatia da emissão", timeout=60)
            print(f"  mineração do trapaceiro: {sig(before['rewards_total'], 2)} antes do job → "
                  f"{sig(after['rewards_total'], 2)} depois (trapacear num job custa só o pagamento daquele job — não "
                  "a reputação nem a mineração, então um bando de Sybils votando junto não derruba quem é honesto)")

        title("10. Um modelo sem orçamento é apagado da rede e do disco de quem o hospedava")
        tpath, tsha, tsize = fake_gguf("modelo-teste", 2_000_000)
        fee = rent_fee(tsize, Client(B).mining())          # o aluguel de uma janela, como a chain cobra
        budget = 5 * fee
        bruno.register_model("modelo-teste", offer(tpath, "modelo-teste.gguf"), tsha, tsize,
                             budget_sig=Decimal(budget) / UNIT, royalty_pct=20, replicas=1, license="teste")
        th = wait(lambda s=set(): placed("modelo-teste", s), "a máquina hospedar o modelo-teste", timeout=120)
        tfile = model_file(th, tsha)
        ws = Client(B).mining()["window_seconds"]
        print(f"  'modelo-teste' ({tsize / 1e6:.0f} MB) está em {NAME[th]}: {rel(tfile)}")
        print(f"  aluguel de {sig(fee)} por janela (disco + a memória que ele reserva, vezes a demanda) e orçamento "
              f"de {sig(budget)} → dura umas {budget // fee} janelas (~{budget // fee * ws} s)")
        wait(lambda: "modelo-teste" not in Client(B).models(), "o orçamento acabar", timeout=180)
        print(f"  janela {Client(B).mining()['window']}: o orçamento não cobre mais o aluguel → a rede apagou o modelo")
        wait(lambda: not os.path.exists(tfile), f"{NAME[th]} apagar o arquivo", timeout=90)
        print(f"  {NAME[th]} descarregou o modelo e apagou {rel(tfile)} sozinha")
        try:
            carla.ask("modelo-teste", [{"role": "user", "content": "oi"}])
        except SigmaError as e:
            print(f"  novo pedido recusado: {e}")

        title("11. A rede vota as próprias regras: 1 SIG = 1 voto, travado até o fim")
        pid = carla.propose("Aluguel de disco mais barato", "Para caber mais sites de coletivos na rede.",
                            "hosting_per_mb_window", 3)
        Client(B, W["bruno-pc"]).vote(pid, "sim", 300)
        Client(A, W["alice-ia"]).vote(pid, "nao", 100)
        prop = wait(lambda: (lambda p: p if p.get("voters") == 2 else None)(Client(B).proposal(pid)),
                    "os dois votos entrarem")
        gov = Client(B).governance()
        print(f"  a carla propôs: hosting_per_mb_window {prop['old']} → {prop['value']} µSIG por MB "
              f"(deixou {sig(prop['deposit'], 0)} de depósito, que volta no fim)")
        print(f"  sim {sig(prop['yes'], 0)} (bruno) · não {sig(prop['no'], 0)} (alice) · quórum hoje "
              f"{sig(prop['quorum_now'], 0)} · {sig(gov['locked'], 0)} travados nas votações")
        print("  a votação dura 10 dias; aprovada, a regra muda sozinha na rede e todo SIG travado volta inteiro")

        step = 12
        if args.ollama:
            title(f"{step}. O padrinho aposenta '{model}' (o Ollama volta a ser só seu)")
            bruno.retire_model(model)
            wait(lambda: f"sigma-{model}" not in ollama_tags(), "a máquina tirar o modelo do Ollama", timeout=120)
            print(f"  '{model}' saiu da rede, o orçamento que sobrou voltou para o bruno e sigma-{model} saiu do "
                  f"Ollama (o {args.ollama} original continua lá)")
            step += 1

        title(f"{step}. Quem ganhou o quê")
        c = Client(B)
        machines = c.machines()
        print(f"  {'':<11} {'minerou':>20} {'ganhou hospedando':>20} {'saldo':>20}  reputação  jobs  falhas")
        for name in names:
            m = machines[MACHINE[name]]
            print(f"  {name:<11} {sig(c.miner(W[name].address)['rewards_total']):>20} {sig(m['earned']):>20} "
                  f"{sig(c.balance(W[name].address)):>20}  {m['reputation']:>+9d}  {m['jobs_done']:>4}"
                  f"  {m.get('jobs_failed', 0):>6}")
        print(f"  {'carla':<11} {'-':>20} {'-':>20} {sig(c.balance(W['carla'].address)):>20}")
        print(f"  o bruno, como padrinho, recebeu {sig(royalty - royalty // 2)} de royalty da IA")
        minted, held = money(W.values())
        print(f"\n  impresso pela mineração até agora: {sig(minted, 2)} de {num(c.mining()['cap'] // UNIT)} SIG")
        print(f"  saldos + orçamentos + valores reservados: {sig(held, 2)} → "
              + ("todo SIG que existe veio da mineração" if held == minted else "NÃO BATE com o total impresso"))
        h = min(Client(A).status()["height"], Client(B).status()["height"])
        same = block_hash(A, h) == block_hash(B, h)
        print(f"  nós A e B têm o mesmo bloco #{h}? {'sim' if same else 'NÃO'}")
        print(f"\nregistros de cada processo: {rel(os.path.join(D, 'logs'))}")

        if args.manter:
            print("\nTudo continua rodando para você explorar (Ctrl+C desliga):")
            print(f"  site e carteira do nó: {A}/  ·  {A}/carteira")
            for name in names:
                try:
                    s = session(os.path.join(D, name))
                    print(f"  painel de {name}: http://127.0.0.1:{s['port']}/#t={s['token']}")
                except (OSError, ValueError, KeyError):
                    pass
            wait(lambda: False, "Ctrl+C", timeout=10 ** 9)
    finally:
        srv.shutdown()
        srv.server_close()
        args.model = model


def _interrupt(*_):
    raise KeyboardInterrupt


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    enable_ansi()
    # Ctrl+Break, fechar o console ou um kill: desliga os apps e os nós com calma, como no Ctrl+C
    for name in ("SIGBREAK", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _interrupt)
    ap = argparse.ArgumentParser(description="Demo ponta a ponta da rede Sigma, tudo nesta máquina.")
    ap.add_argument("--ollama", nargs="?", const=DEFAULT_OLLAMA, metavar="MODELO",
                    help=f"IA de verdade com um modelo que já está no seu Ollama (padrão {DEFAULT_OLLAMA})")
    ap.add_argument("--manter", action="store_true", help="no fim, deixa a rede rodando até o Ctrl+C")
    args = ap.parse_args()
    args.model = None
    try:
        run(args)
    except KeyboardInterrupt:
        print("\ndemo interrompida")
    except SigmaError as e:   # uma transação recusada: mostra o motivo, sem rastro de pilha
        raise SystemExit(f"\nerro: {e}" + (f" (veja os registros em {rel(os.path.join(D, 'logs'))})" if D else ""))
    finally:
        if PROCS:
            print("desligando os apps e os nós…", flush=True)
            stop_all()
        if args.ollama and args.model:
            ollama_forget(args.model)


if __name__ == "__main__":
    main()
