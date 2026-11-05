"""Máquina hospedeira: o que roda no computador de quem cede recursos para a rede.

Registra a máquina com o hardware detectado, sinaliza uma vez por janela que está no ar, baixa e carrega
sozinha os modelos que a REDE colocou aqui (ninguém escolhe à mão), hospeda sites que precisam de réplicas
e atende jobs de IA e de computação. Os pagamentos vão para a carteira `payout`; a chave da máquina só
assina essa rotina e o usuário nunca precisa vê-la.

Pedido de IA direcionado chega cifrado para a chave desta máquina, e a resposta vai cifrada para a chave de
quem perguntou: o nó só guarda bytes cifrados. A máquina precisa ler o pedido para responder, mas nunca grava
pedido nem resposta no disco ou no registro (só tamanhos, tokens e tempo). O que ela não consegue atender
(conversa maior que o modelo, motor fora do ar, fila cheia, pedido que não abre) volta na hora como falha:
quem perguntou recebe o dinheiro de volta e a máquina não leva falta.

O arquivo de um modelo baixado fica guardado enquanto o modelo existir na rede, mesmo sem atribuição: se a
rede colocar o modelo aqui de novo, ele volta em segundos, sem baixar tudo outra vez.
"""
import base64
import json
import multiprocessing
import os
import random
import re
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import grupo, hardware, media, netsafe, tasks
from .chain import FUNDED_ABOVE, MEDIA_CLAIM_TIMEOUT, MEDIA_PRICES, MIN_FUNDED_SECONDS, TASK_PRICE, canon
from .client import Client, SigmaError
from .crypto import is_sealed, norm_address, seal
from .engines import (CONTEXT, ContextOverflow, EngineError, EngineManager, RefusedFile, check_gguf, fetch,
                      sha256_file)

PRICES = {"ai_per_1k_tokens": 2000, "compute_per_unit": 500}   # a hospedagem tem preço da rede, não da máquina
MAX_PRICE = 1_000_000       # teto da chain para os dois preços
MAX_MEDIA_PRICE = 100_000_000   # teto da chain por imagem e por segundo de vídeo (v2)
DEFAULTS = {"ai_enabled": True, "compute_enabled": True, "disk_gb": 20, "ai_ram_gb": 4, "public_url": "",
            "engine": "auto", "ai_threads": None, "name": "", "port": None, "cheat": False,
            "ai_image": True, "ai_video": False}   # v2: aceitar modelos de imagem (padrão) e de vídeo (pesado)
MIN_AI_RAM_MB = 1500        # abaixo disso a rede não coloca modelo aqui
MAX_AI_RAM_MB = 1_048_576   # teto da chain para hw.ai_ram_mb
MAX_TOKENS = 10 ** 9        # teto da chain para tokens_in e tokens_out
COMPUTE_DEADLINE = 480      # s: o claim expira em 600 s, então uma tarefa que demora mais é abandonada antes
CLAIM_TIMEOUT = 600         # s, o mesmo da chain: pedido reivindicado e sem resposta depois disso vira falta
MAX_QUEUE = 3               # pedidos de IA na fila (contando o que está sendo respondido): os novos voltam "busy"
MAX_MEDIA_QUEUE = 2         # imagem/vídeo têm fila própria (minutos cada): o texto não espera atrás deles
MIN_ANSWER_TOKENS = 64      # se o escrow não paga nem isto de resposta, volta "busy" (não trabalha de graça)
CLAIM_TRIES = 5             # tentativas de reivindicar um pedido direcionado (o nó pode falhar por um instante)
MODEL_WAIT = 90             # s que um pedido espera o modelo terminar de carregar (logo depois de ligar o PC)
ENGINE_GRACE = 120          # s: motor fora do ar por pouco tempo (Ollama reiniciando) não tira os modelos da rede
REFUSED_FOR = 24 * 3600     # s sem baixar de novo um arquivo recusado
SITE_COOLDOWN = 600         # s até tentar de novo um site que não deu para hospedar
JOBS_FAST, JOBS_SLOW = 2, 20   # s entre consultas de jobs: hospedando modelo / sem modelo (só computação)
MINING_FRESH = 10           # s: o /mining que o minerador buscou serve para o ping se for mais novo que isso
MINING_EVERY = 30           # s: sem esse /mining, o host consulta o nó no máximo a cada 30 s
NETWORK_EVERY = 60          # s entre leituras do /models para decidir que arquivo guardado sai do disco
UNDERFUNDED_RETRY = 600     # s até olhar de novo o orçamento de um modelo em espera (chain.MIN_FUNDED_SECONDS)
HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")
MODEL_FILE = re.compile(r"^([0-9a-f]{64})\.(?:gguf|safetensors)(?:\.ok|\.part)?\Z")
LOADING = ("na fila", "conferindo", "baixando", "carregando", "anunciando")


def _pt(n):
    """Número no jeito brasileiro (3.000), para o registro que o painel mostra."""
    return f"{n:,.0f}".replace(",", ".")


def _mb(size):
    """Bytes -> "4.700 MB" ou "0,6 MB" (MB decimais), para o registro dos downloads."""
    mb = size / 1e6
    return (_pt(mb) if mb >= 10 else f"{mb:.1f}".replace(".", ",")) + " MB"


def _gb(mb):
    """MB decimais -> "4 GB" ou "1,5 GB", como o Windows mostra (1 GB = 2^30 bytes)."""
    v = round(hardware.mb_to_gb(mb), 1)
    return (f"{v:.0f}" if v == int(v) else f"{v:.1f}".replace(".", ",")) + " GB"


REFUSED_TEXT = {"sha": "Modelo recusado: o arquivo não confere com o que o padrinho registrou",
                "gguf": "Modelo recusado: o arquivo não é um GGUF válido",
                "weights": "Modelo recusado: um dos arquivos não é um safetensors nem um GGUF válido"}
UNDERFUNDED_TEXT = ("Modelo em espera: o orçamento dele não paga nem um dia de hospedagem. "
                    "Este computador baixa quando o padrinho recarregar.")
FAIL_TEXT = {   # o "error" da resposta de falha (o cliente mostra o de "context"; nos outros tenta outra máquina)
    "context": f"a conversa passou do limite do modelo ({_pt(CONTEXT)} tokens): comece uma conversa nova ou "
               "divida o texto em partes menores",
    "engine": "o motor de IA desta máquina não está respondendo agora",
    "unreadable": "o pedido não abre com a chave desta máquina (ou não é um JSON válido)",
    "busy": "esta máquina está com a fila de pedidos cheia agora",
}


def estimate_tokens(messages):
    """A mesma estimativa do cliente (client.fit_messages): len(conteúdo)//3 + 8 por mensagem, mais 16."""
    return sum(len(str(m.get("content") or "")) // 3 + 8 for m in messages) + 16


def _task_worker(conn, task):
    """Roda a tarefa de computação noutro processo: não trava o app nem passa do prazo do claim."""
    try:
        conn.send(("ok", tasks.run(task)))
    except Exception as e:
        conn.send(("erro", str(e)))


def run_task(task, deadline=COMPUTE_DEADLINE, should_stop=None):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_task_worker, args=(child, task), daemon=True, name="sigma-tarefa")
    p.start()
    child.close()
    try:
        end = time.time() + deadline
        while not parent.poll(min(1.0, max(0.0, end - time.time()))):
            if should_stop and should_stop():
                raise SigmaError("a tarefa foi interrompida: a máquina está desligando")
            if time.time() >= end:
                raise SigmaError(f"a tarefa passou de {deadline}s e foi abandonada")
        kind, value = parent.recv()
    except EOFError:
        raise SigmaError("o processo da tarefa fechou sem responder") from None
    finally:
        parent.close()
        p.join(1)
        if p.is_alive():
            p.terminate()
    if kind != "ok":
        raise SigmaError(f"a tarefa falhou: {value}")
    return value


_is_internal = netsafe.is_internal   # nome antigo (o nome aponta para a rede interna?)


class _Unreadable(Exception):
    """O pedido não abre com a chave desta máquina ou não é um JSON de pedido."""


# o que um pedido malformado levanta ao ser lido: além de JSON e campos inválidos, número infinito (Infinity e
# 1e999 passam no json do Python) virando inteiro (OverflowError) e JSON aninhado demais (RecursionError)
MALFORMED = (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError)


class Host:
    def __init__(self, node_url, machine_wallet, payout, data_dir, settings, miner=None, log=print):
        self.wallet = machine_wallet
        self.address = machine_wallet.address
        self.payout = norm_address(payout)
        self.client = Client(node_url, machine_wallet)
        self.node_url = self.client.node
        self.data_dir = data_dir
        self.settings = dict(DEFAULTS, **(settings or {}))
        self.miner = miner
        self.log = log
        # grupo (sigma/grupo.py): coordenador usa a memória de outros PCs; trabalhador empresta a sua e não serve IA
        self.group = grupo.from_settings(self.settings.get("group"), machine_wallet, self.client, data_dir,
                                         self.settings["ai_threads"], log)
        if self.group and self.group.role == "coordinator":
            self.settings["engine"] = "llamacpp"      # o Ollama não divide camadas entre máquinas
        if self.group and self.group.role == "worker":
            self.settings["ai_enabled"] = False
        self.engine = EngineManager(data_dir, self.settings["engine"], log, self.settings["ai_threads"])
        self.engine.rpc_provider = self.group.rpc if self.group else None
        # v2: imagem e vídeo (stable-diffusion.cpp); com o motor "mock" dos testes, o gerador de teste
        self.media = media.MediaManager(data_dir, self.settings["engine"], log, self.settings["ai_threads"])
        self.models_dir = os.path.join(data_dir, "models")
        self.blob_dir = os.path.join(data_dir, "blobs")
        self.refused_path = os.path.join(self.models_dir, "recusados.json")
        self._lock = threading.RLock()
        self._queue_cv = threading.Condition(self._lock)
        self._tx_lock = threading.Lock()      # nonce sequencial: uma tx da máquina por vez
        self._stop = threading.Event()
        self._wake = threading.Event()        # acorda o laço dos modelos quando o registro muda
        self._jobs_wake = threading.Event()   # acorda o laço dos jobs (modelo recém-anunciado, desligar)
        self._threads = []
        self._server = None
        self._record = None                   # último /machine/<eu>
        self._registered = None               # assinatura do último register_machine (o que importa nele)
        self._reg_hw = None
        self._hw = None
        self._seen_window = None
        self._last_text = False               # o último register_machine desta execução foi com o motor de texto?
        self._engine_down_since = None        # desde quando o motor de IA não responde (None = respondendo)
        self._mining = (0.0, None)            # (quando, /mining) consultado pelo próprio host
        self._network = (0.0, None)           # (quando, {sha256: nome}) dos modelos que existem na rede
        self._names = {}                      # sha256 -> nome do modelo, para o registro dizer qual arquivo saiu
        self._refused = None                  # sha256 -> recusas do arquivo (recusados.json, lido sob demanda)
        self._models = {}                     # nome -> estado local do modelo
        self._site_cooldown = {}
        self._swap = False
        self._pausing = 0
        self._we_paused = False
        self._errors = {}                     # origem -> (hora, mensagem)
        self._engine_desc = (0.0, None)
        self._queue = deque()                 # pedidos de IA já reivindicados, na ordem em que chegaram
        self._fails = deque()                 # falhas a mandar (job, código, msg): a thread de respostas entrega
        self._current = None                  # o pedido sendo respondido agora
        self._mqueue = deque()                # pedidos de imagem/vídeo já reivindicados (fila própria, v2)
        self._mcurrent = None                 # o pedido de imagem/vídeo sendo gerado agora
        self._media_desc = (0.0, None)
        self._handled = {}                    # id do job -> hora em que foi reivindicado, respondido ou ignorado
        self._claim_fails = {}                # id do job direcionado -> tentativas de claim que não deram certo
        self._compute = None                  # thread da tarefa de computação em andamento

    # ------------------------------------------------------------ controle
    def start(self):
        if self._threads:
            return
        self._stop.clear()
        os.makedirs(self.models_dir, exist_ok=True)
        os.makedirs(self.blob_dir, exist_ok=True)
        self._start_server()
        if self.group:
            self.group.start()
        for target, name in ((self._main_loop, "registro"), (self._models_loop, "modelos"),
                             (self._jobs_loop, "jobs"), (self._ai_loop, "ia"), (self._media_loop, "midia"),
                             (self._answers_loop, "respostas")):
            t = threading.Thread(target=target, name=f"sigma-maquina-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        self.log(f"[máquina] {self.address} ligada; pagamentos para {self.payout}")

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._jobs_wake.set()
        with self._queue_cv:
            self._queue_cv.notify_all()
        for t in self._threads:
            t.join(timeout=10)
        self._threads = []
        if self._compute:
            self._compute.join(timeout=5)
        # pedido que ficou na fila volta como falha: o cliente recebe o dinheiro na hora e esta máquina não leva falta
        self._fail_queue("engine", "esta máquina foi desligada antes de responder")
        self._stop_server()
        self.engine.shutdown()
        self.media.shutdown()
        if self.group:
            self.group.stop()
        with self._lock:
            if self._we_paused and self.miner:
                self.miner.resume()
            self._pausing, self._we_paused = 0, False
        self.log("[máquina] desligada")

    def update_settings(self, settings):
        with self._lock:
            old = self.settings
            self.settings = dict(old, **(settings or {}))
            new = self.settings
        if (new["engine"], new["ai_threads"]) != (old["engine"], old["ai_threads"]):
            self._swap = True
        if (new["public_url"], new["port"]) != (old["public_url"], old["port"]) and self._threads:
            self._stop_server()
            self._start_server()
        self._wake.set()

    def stats(self) -> dict:
        with self._lock:
            rec = self._record or {}
            models = [{"name": m["name"], "status": m["status"], "progress": round(m["progress"], 4),
                       "size": m["size"], "error": m["error"]} for m in sorted(self._models.values(),
                                                                                key=lambda m: m["name"])]
            recent = [e for e in self._errors.values() if time.time() - e[0] < 600]
            queue = len(self._queue) + (1 if self._current else 0)
            mqueue = len(self._mqueue) + (1 if self._mcurrent else 0)
        return {"registered": bool(self._registered) and bool(rec), "machine": self.address, "payout": self.payout,
                "online": bool(rec.get("online")), "engine": self._describe_engine(), "models": models,
                "sites": list(rec.get("sites") or []), "jobs_done": rec.get("jobs_done", 0),
                "jobs_failed": rec.get("jobs_failed", 0), "earned": rec.get("earned", 0),
                "reputation": rec.get("reputation", 0), "ai": bool(rec.get("ai")),
                "compute": bool(rec.get("compute")), "hw": self._hw, "ai_queue": queue, "media_queue": mqueue,
                "media_engine": self._describe_media(), "tasks": list(rec.get("tasks") or ["text"]),
                "engine_down": self._engine_down_since is not None,
                "last_error": max(recent)[1] if recent else None}

    def _describe_engine(self):
        t, desc = self._engine_desc   # o painel pergunta toda hora: guarda por alguns segundos
        if desc is None or time.time() - t > 5:
            try:
                desc = self.engine.describe()
            except Exception as e:
                desc = {"kind": "?", "ready": False, "detail": str(e), "installing": False, "install_progress": 0.0}
            self._engine_desc = (time.time(), desc)
        return desc

    def _describe_media(self):
        t, desc = self._media_desc
        if desc is None or time.time() - t > 5:
            try:
                desc = self.media.describe()
            except Exception as e:
                desc = {"kind": "?", "ready": False, "detail": str(e), "installing": False, "install_progress": 0.0}
            self._media_desc = (time.time(), desc)
        return desc

    def _error(self, origin, msg):
        with self._lock:
            repeated = self._errors.get(origin, (0, None))[1] == msg
            self._errors[origin] = (time.time(), msg)
        if not repeated:
            self.log(f"[máquina] {msg}")

    def _ok(self, origin):
        with self._lock:
            self._errors.pop(origin, None)

    def _tx(self, type_, data, wait=True):
        with self._tx_lock:
            return self.client.send_tx(type_, data, wait=wait)

    # ------------------------------------------------- registro e presença
    def _prices(self, rec=None, media=False):
        """Preços desta máquina (só IA e computação), dentro do teto da chain. Com `rec`: os que a chain guardou.
        `media` (rede na v2): também por imagem e por segundo de vídeo."""
        base = (rec or {}).get("prices") or self.settings.get("prices") or {}
        out = {}
        media = media or (isinstance(base, dict) and any(k in base for k in MEDIA_PRICES))
        for k, v in {**PRICES, **(MEDIA_PRICES if media else {})}.items():
            try:
                p = int(base.get(k, v)) if isinstance(base, dict) else v
            except (TypeError, ValueError):
                p = v
            out[k] = max(1, min(MAX_PRICE if k in PRICES else MAX_MEDIA_PRICE, p))
        return out

    def _mining_info(self):
        """O /mining mais novo que o host ou o minerador tem (consulta o nó se não tiver nenhum)."""
        info = self._mining[1]
        get = getattr(self.miner, "last_mining", None)
        if callable(get):
            try:
                info = (get() or (None, 0.0))[0] or info
            except Exception:
                pass
        if info is None:
            try:
                info = self.client.mining()
                self._mining = (time.time(), info)
            except SigmaError:
                return {}
        return info if isinstance(info, dict) else {}

    def _net_media(self):
        """A rede já está nas regras v2 (aceita modelos de imagem e vídeo)? Antes disso, registrar "tasks" ou
        preços de imagem seria recusado."""
        return bool(self._mining_info().get("media"))

    def _media_tasks(self):
        """Imagem e vídeo que esta máquina aceita: os ligados nas configurações, se o motor de imagem existe. Não
        dependem do motor de texto: o stable-diffusion.cpp roda sozinho, com o Ollama fechado ou não."""
        if not self.media.available():
            return []
        return [t for t, key in (("image", "ai_image"), ("video", "ai_video")) if self.settings.get(key)]

    def _engine_ok(self):
        """O motor de texto responde? Fora do ar por pouco tempo (até ENGINE_GRACE) ainda conta como ligado: um
        Ollama que reinicia não tira os modelos da rede. No primeiro registro desta execução, motor fora = sem
        texto (imagem e vídeo seguem, com o motor deles)."""
        if self.engine.available():
            if self._engine_down_since is not None:
                self._engine_down_since = None
                self.log("[máquina] o motor de IA voltou a responder")
            return True
        if not self._last_text:
            return False
        now = time.time()
        if self._engine_down_since is None:
            self._engine_down_since = now
            who = {"ollama": "o Ollama", "llamacpp": "o llama.cpp"}.get(self.engine.kind, "o motor de IA")
            self.log(f"[máquina] {who} não respondeu; mantendo os modelos e tentando de novo")
        return now - self._engine_down_since < ENGINE_GRACE

    def _registration(self):
        s = self.settings
        hw = hardware.detect(self.data_dir)
        self._hw = hw
        ai_ram = hardware.gb_to_mb(s["ai_ram_gb"])   # GB como o Windows mostra (2^30 bytes) -> MB da chain
        # nunca mais do que existe (nem que o teto da chain)
        ai_ram = max(0, min(ai_ram, max(hw["ram_mb"] - 1000, hw["vram_mb"]), MAX_AI_RAM_MB))
        if self.group:   # coordenador: soma a memória que os trabalhadores conectados emprestam
            ai_ram = min(ai_ram + self.group.lent_mb(), MAX_AI_RAM_MB)
        used = self._disk_used_mb()
        disk = max(0, min(hardware.gb_to_mb(s["disk_gb"]), used + max(0, hw["disk_free_mb"] - 2000)))
        want_ai = bool(s["ai_enabled"]) and ai_ram >= MIN_AI_RAM_MB
        if not want_ai:
            self._engine_down_since = None
        text = want_ai and self._engine_ok()
        name = str(s["name"] or f"maquina-{self.address[3:9]}")[:40]   # sem o nome do PC: ele fica público
        net_media = self._net_media()
        # v2: imagem e vídeo têm motor próprio; com o de texto fora do ar a máquina segue com eles, e a rede só tira
        # daqui os modelos de texto (não os de imagem, que teriam de ser baixados de novo em outra máquina)
        extra = self._media_tasks() if want_ai and net_media else []
        data = {"payout": self.payout, "name": name,
                "hw": {"cpu_cores": hw["cpu_cores"], "ram_mb": hw["ram_mb"], "ai_ram_mb": ai_ram,
                       "vram_mb": hw["vram_mb"], "disk_mb": disk, "gpu": hw["gpu"]},
                "ai": text or bool(extra), "compute": bool(s["compute_enabled"]),
                "endpoint": str(s["public_url"] or "").rstrip("/"), "prices": self._prices(media=net_media),
                "pub": self.wallet.pub}
        if net_media:   # v2: quais classes de IA esta máquina hospeda
            data["tasks"] = (["text"] if text else []) + extra
        return data

    def _fingerprint(self, data):
        s = self.settings
        return (data["payout"], data["name"], data["ai"], data["compute"], data["endpoint"],
                json.dumps(data["prices"], sort_keys=True), s["disk_gb"], s["ai_ram_gb"],
                tuple(data.get("tasks") or ()), data["hw"]["ai_ram_mb"])

    def _register_if_needed(self):
        data = self._registration()
        fp = self._fingerprint(data)
        if fp == self._registered:
            return
        self._tx("register_machine", data)
        tasks_ = data.get("tasks", ["text"])
        self._registered, self._reg_hw = fp, data["hw"]
        self._last_text = data["ai"] and "text" in tasks_
        try:
            self._seen_window = self._window()   # o registro já conta como sinal desta janela
        except (SigmaError, KeyError, TypeError, AttributeError):
            pass
        ai = f"sim ({_gb(data['hw']['ai_ram_mb'])} de memória para IA)" if data["ai"] else "não"
        extra = " e ".join({"image": "imagem", "video": "vídeo"}[t] for t in tasks_ if t != "text")
        if extra and data["ai"]:
            ai += f" · também {extra}" if "text" in tasks_ else f" · só {extra} (o motor de texto não responde)"
        self.log(f"[máquina] registrada como '{data['name']}': IA {ai}, computação "
                 f"{'sim' if data['compute'] else 'não'}, disco {_gb(data['hw']['disk_mb'])}")
        self._refresh()

    def _refresh(self):
        try:
            rec = self.client.machine(self.address)
        except SigmaError as e:
            try:
                known = self.address in self.client.machines()
            except SigmaError:
                raise e from None
            if not known:   # rede nova ou reiniciada: registra de novo
                self._registered = None
                return
            raise
        with self._lock:
            self._record = rec
        self._wake.set()

    def _window(self):
        """Janela atual da rede: a do /mining que o minerador acabou de buscar (menos de 10 s); senão consulta o
        nó, no máximo a cada 30 s (mil apps ligados não podem martelar o nó). None se ainda não sabe."""
        get = getattr(self.miner, "last_mining", None)
        if callable(get):
            try:
                info, t = get() or (None, 0.0)
            except Exception:
                info, t = None, 0.0
            if isinstance(info, dict) and isinstance(info.get("window"), int) and time.time() - t < MINING_FRESH:
                return info["window"]
        t, info = self._mining
        if info is None or time.time() - t >= self._mining_every(info):
            info = self.client.mining()
            self._mining = (time.time(), info)
        return info.get("window")

    @staticmethod
    def _mining_every(info):
        """30 s na rede de verdade (janelas de 5 min); numa rede de teste com janelas curtas, um terço da janela,
        senão a máquina passaria janelas inteiras sem sinalizar e cairia para offline."""
        try:
            ws = float(info.get("window_seconds") or 300)
        except (TypeError, ValueError, AttributeError):
            ws = 300.0
        return max(1.0, min(MINING_EVERY, ws / 3))

    def _ping_if_needed(self):
        rec = self._record
        if not rec:
            return
        w = self._window()
        if not isinstance(w, int):
            return
        seen = self._seen_window
        if (isinstance(seen, int) and seen >= w) or (isinstance(rec.get("last_seen"), int) and rec["last_seen"] >= w):
            self._seen_window = max(w, seen) if isinstance(seen, int) else w
            return
        try:
            self._tx("ping", {})
        except SigmaError as e:
            if "sinalizou" not in str(e):
                raise
        self._seen_window = w

    def _main_loop(self):
        next_refresh = next_sites = 0.0
        while not self._stop.is_set():
            try:
                self._register_if_needed()
                if time.time() >= next_refresh:
                    self._refresh()
                    next_refresh = time.time() + 10
                self._ping_if_needed()
                self._ok("rede")
                if time.time() >= next_sites:
                    next_sites = time.time() + 30
                    self._pin_sites()
                    self._ok("sites")
            except SigmaError as e:
                self._error("rede", f"erro falando com o nó: {e}")
            except Exception as e:   # o laço nunca morre: tenta de novo no próximo ciclo
                self._error("rede", f"erro inesperado: {type(e).__name__}: {e}")
            self._stop.wait(self._loop_wait())

    def _loop_wait(self):
        """5 s entre as voltas do registro e do ping na rede de verdade (janelas de 5 min). Numa rede de teste com
        janelas de poucos segundos, um terço da janela: com 5 s a máquina pularia janelas sem sinalizar e, nesse
        meio-tempo, a chain recusaria os pedidos direcionados a ela ("não está online agora")."""
        return max(0.5, min(5.0, self._window_seconds() / 3))

    def _window_seconds(self):
        """Duração da janela da rede (do /mining mais novo que o host ou o minerador tem); 300 s se não souber."""
        info = self._mining[1]
        get = getattr(self.miner, "last_mining", None)
        if callable(get):
            try:
                info = (get() or (None, 0.0))[0] or info
            except Exception:
                pass
        try:
            return float((info or {}).get("window_seconds") or 300)
        except (TypeError, ValueError, AttributeError):
            return 300.0

    def _underfunded(self, a):
        """O orçamento do modelo não paga nem MIN_FUNDED_SECONDS de aluguel de todas as cópias? Então baixar GB
        para ele é desperdício da internet de quem hospeda: o modelo seria apagado logo depois."""
        budget, rent, size = a.get("budget"), a.get("rent_window"), a.get("size")
        if not isinstance(budget, int) or not isinstance(rent, int) or rent <= 0:
            return False                      # nó que não manda esses campos: segue como antes
        if isinstance(size, int) and size <= FUNDED_ABOVE:
            return False                      # poucos MB: baixar não pesa (a mesma regra da chain.funded)
        return budget // rent * self._window_seconds() < MIN_FUNDED_SECONDS

    # -------------------------------------------------------------- modelos
    def _model_path(self, sha, ext=".gguf"):
        return os.path.join(self.models_dir, f"{sha}{ext}")

    def _parts(self, m):
        """Arquivos do modelo: o GGUF (texto) ou, para imagem/vídeo (v2), o principal mais os extras (VAE,
        codificadores de texto), cada um com o papel, o endereço, o SHA-256, o tamanho e onde fica no disco."""
        if m["task"] == "text":   # v4: o GGUF pode vir em partes (role "part"), na ordem do llama-gguf-split
            parts = [{"role": "main", "url": m["url"], "sha256": m["sha256"],
                      "size": m["main_size"] if m.get("files") else m["size"], "path": self._model_path(m["sha256"])}]
            for f in m.get("files") or []:
                sha = str(f.get("sha256")).lower()
                if f.get("role") != "part" or not HASH_RE.match(sha) or not isinstance(f.get("size"), int):
                    raise RefusedFile("parte de modelo de texto inválida", "gguf")
                parts.append({"role": "part", "url": f.get("url"), "sha256": sha, "size": f["size"],
                              "path": self._model_path(sha)})
            return parts
        parts = [{"role": "main", "url": m["url"], "sha256": m["sha256"], "size": m["main_size"]}]
        parts += [{"role": f.get("role"), "url": f.get("url"), "sha256": str(f.get("sha256")).lower(),
                   "size": f.get("size")} for f in m["files"]]
        for x in parts:
            ext = media.weight_ext(x["url"])
            if (ext is None or not HASH_RE.match(x["sha256"]) or not isinstance(x["size"], int)
                    or (x["role"] != "main" and x["role"] not in media.ROLE_FLAGS)):
                raise RefusedFile("arquivo de modelo de imagem/vídeo que não é .safetensors nem .gguf", "weights")
            x["path"] = self._model_path(x["sha256"], ext)
        return parts

    def _split_names(self, sha, paths):
        """Partes de um GGUF dividido com os nomes que o llama.cpp procura (links, sem copiar os GB): devolve a
        primeira, que é o que se passa ao llama-server."""
        folder = os.path.join(self.models_dir, "partes-" + sha[:16])
        os.makedirs(folder, exist_ok=True)
        names = [os.path.join(folder, f"m-{i:05d}-of-{len(paths):05d}.gguf") for i in range(1, len(paths) + 1)]
        for src, dst in zip(paths, names):
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                continue
            if os.path.exists(dst):
                os.remove(dst)
            try:
                os.link(src, dst)
            except OSError:
                os.symlink(src, dst)
        return names[0]

    def _all_paths(self, m):
        try:
            paths = [x["path"] for x in self._parts(m)]
        except RefusedFile:
            paths = [self._model_path(m["sha256"])]
        return [q for p in paths for q in (p, p + ".ok", p + ".part")]

    def _engine_of(self, m):
        return self.engine if m.get("task", "text") == "text" else self.media

    def _disk_used_bytes(self):
        total = 0
        for folder in (self.models_dir, self.blob_dir):
            try:
                with os.scandir(folder) as it:
                    total += sum(e.stat().st_size for e in it if e.is_file())
            except OSError:
                pass
        return total

    def _disk_used_mb(self):
        return self._disk_used_bytes() // 1_000_000

    def _verified(self, path, size):
        """O arquivo existe, tem o tamanho certo e já teve o SHA-256 conferido (marcador .ok)?"""
        try:
            st = os.stat(path)
            with open(path + ".ok", encoding="utf-8") as f:
                return st.st_size == size and f.read().strip() == f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            return False

    def _mark_verified(self, path):
        st = os.stat(path)
        with open(path + ".ok", "w", encoding="utf-8") as f:
            f.write(f"{st.st_size}:{st.st_mtime_ns}")

    @staticmethod
    def _touch(path):
        """Marca o modelo como usado agora: para liberar disco, sai primeiro o que está parado há mais tempo."""
        try:
            os.utime(path + ".ok")
        except OSError:
            pass

    @staticmethod
    def _remove_files(paths):
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass

    def _node_internal(self):
        try:
            return netsafe.is_internal(urllib.parse.urlparse(self.node_url).hostname or "")
        except ValueError:
            return False

    def _url_allowed(self, url):
        try:
            u = urllib.parse.urlparse(url)
            host = u.hostname
        except ValueError:
            return False
        if u.scheme not in ("http", "https") or not host:
            return False
        # URL apontando para a rede interna só vale quando o próprio nó também é local (rede de testes)
        return not netsafe.is_internal(host) or self._node_internal()

    def _download_opener(self):
        """Confere o IP de cada conexão e de cada redirecionamento: nem redirect nem DNS levam o download para dentro
        da casa de quem hospeda. Só numa rede de testes local (o próprio nó é interno) a rede interna vale."""
        return netsafe.safe_opener(allow_internal=self._node_internal())

    def _models_loop(self):
        while not self._stop.is_set():
            try:
                self._sync_models()
                self._ok("modelos")
            except SigmaError as e:
                self._error("modelos", f"modelos: {e}")
            except Exception as e:
                self._error("modelos", f"modelos: erro inesperado: {type(e).__name__}: {e}")
            self._wake.wait(10)
            self._wake.clear()

    def _sync_models(self):
        if self._swap:
            self._swap_engine()
        rec = self._record
        if rec is None:
            return
        assigned = {a["name"]: a for a in rec.get("assignments") or [] if HASH_RE.match(str(a.get("sha256")))}
        for name in [n for n, m in self._models.items()
                     if n not in assigned or assigned[n]["sha256"] != m["sha256"]]:
            self._drop(name)
        self._gc_models()
        for name, a in sorted(assigned.items()):
            if self._stop.is_set() or self._swap:
                break
            try:
                if self.settings["ai_enabled"]:
                    self._ensure(name, a)
                else:
                    self._pause_model(name, a)
            except Exception as e:   # um erro que ninguém previu num modelo não impede os outros de carregar
                with self._lock:
                    m = self._models.get(name)
                err = f"erro inesperado: {type(e).__name__}: {e}"
                if m is None:
                    self._error(f"modelo {name}", f"modelo '{name}': {err}")
                else:
                    self._retry_later(m, name, err)

    @staticmethod
    def _model_info(name, a):
        """O modelo atribuído, como o host guarda: classe (texto, se a rede não disser) e arquivos extras (v2)."""
        return {"name": name, "sha256": a["sha256"], "size": int(a["size"]), "url": a.get("url"),
                "task": a.get("task") if a.get("task") in ("image", "video") else "text",
                "files": list(a.get("files") or []), "main_size": int(a.get("main_size") or a["size"])}

    def _state(self, name, a):
        with self._lock:
            m = self._models.get(name)
            if m is None:
                m = self._models[name] = dict(self._model_info(name, a), status="na fila", progress=0.0, error=None,
                                              loaded=False, retry_at=0.0, fails=0,
                                              declined=False)   # já recusou esta atribuição na rede?
                self._names[a["sha256"]] = name
            return m

    def _set(self, m, status, progress=None, error=None):
        with self._lock:
            m["status"], m["error"] = status, error
            if progress is not None:
                m["progress"] = progress

    def _retry_later(self, m, name, err):
        """Não deu para hospedar agora (rede, disco, motor): mostra o erro e tenta de novo, cada vez mais devagar."""
        m["fails"] += 1
        m["retry_at"] = time.time() + min(600, 15 * 2 ** m["fails"])
        self._set(m, "erro", error=str(err))
        self._error(f"modelo {name}", f"modelo '{name}': {err}")

    def _ensure(self, name, a):
        """Arquivo baixado e conferido -> carregado no motor -> pin_model (só então a rede manda jobs)."""
        m = self._state(name, a)
        if m["task"] != "text":
            return self._ensure_media(m, name, a)
        loaded = m["loaded"] and name in self.engine.loaded()
        if loaded and a["status"] == "pinned":
            self._set(m, "hospedando", 1.0)
            return
        if time.time() < m["retry_at"]:
            return
        try:
            parts = self._parts(m)
        except RefusedFile as e:
            return self._hold_refused(m, a, e.reason)
        main = parts[0]
        refused = next((r for r in (self._refused_reason(x, "gguf") for x in parts) if r), None)
        if refused:   # arquivo recusado há menos de 24 h: nem baixa de novo
            return self._hold_refused(m, a, refused)
        path = main["path"]
        try:
            if not loaded:
                missing = [x for x in parts if not self._verified(x["path"], x["size"])]
                if missing:
                    if self._underfunded(a):   # arquivo já no disco carrega; baixar GB para quem não paga, não
                        m["retry_at"] = time.time() + UNDERFUNDED_RETRY
                        self._set(m, "recusado", 0.0, UNDERFUNDED_TEXT)
                        self._decline(name)   # avisa a rede: tira a atribuição e põe cooldown, sem punição
                        return
                    if len(parts) == 1:
                        self._download(m, a["url"], path)
                    else:
                        for x in missing:
                            self._download(m, x["url"], x["path"], sha=x["sha256"], size=x["size"], kind="gguf",
                                           label=x["role"])
                if len(parts) > 1:   # o llama.cpp acha as partes pelo nome: <prefixo>-0000N-of-0000M.gguf
                    path = self._split_names(main["sha256"], [x["path"] for x in parts])
                self._set(m, "carregando", 1.0)
                self.engine.load(name, path, m["sha256"])
                m["loaded"] = True
                self._touch(path)
            if a["status"] == "assigned":
                self._set(m, "anunciando", 1.0)
                self._tx("pin_model", {"name": name})
                self.log(f"[máquina] hospedando o modelo '{name}': a rede já pode mandar pedidos para cá")
                self._refresh()
                self._jobs_wake.set()
            self._set(m, "hospedando", 1.0)
            m["fails"] = 0
        except RefusedFile as e:
            self._refuse(m, a, e.reason, main)
        except (EngineError, SigmaError, OSError) as e:
            self._retry_later(m, name, e)

    def _ensure_media(self, m, name, a):
        """Imagem/vídeo (v2): baixa e confere cada arquivo (principal + extras), entrega ao stable-diffusion.cpp e
        anuncia (pin_model), como no texto."""
        engine = self.media
        try:
            parts = self._parts(m)
        except RefusedFile as e:   # cadastro errado (extensão ou papel): nada foi baixado, nada a anotar nem apagar
            return self._hold_refused(m, a, e.reason)
        # o sd-cli abre os arquivos a cada pedido: carregado só vale com todos eles no disco, conferidos (um arquivo
        # que sumiu é baixado de novo, em vez de o modelo seguir "hospedando" com todo pedido falhando)
        loaded = m["loaded"] and name in engine.loaded() and all(self._verified(x["path"], x["size"]) for x in parts)
        if loaded and a["status"] == "pinned":
            self._set(m, "hospedando", 1.0)
            return
        if time.time() < m["retry_at"]:
            return
        refused = next((r for r in (self._refused_reason(x, "weights") for x in parts) if r), None)
        if refused:
            return self._hold_refused(m, a, refused)
        current = None   # o arquivo sendo baixado agora: se ele for recusado, a recusa é só dele
        try:
            if not loaded:
                missing = [x for x in parts if not self._verified(x["path"], x["size"])]
                if missing and self._underfunded(a):
                    m["retry_at"] = time.time() + UNDERFUNDED_RETRY
                    self._set(m, "recusado", 0.0, UNDERFUNDED_TEXT)
                    self._decline(name)
                    return
                for x in missing:
                    current = x
                    self._download(m, x["url"], x["path"], sha=x["sha256"], size=x["size"], kind="weights",
                                   label=x["role"])
                current = None
                self._set(m, "carregando", 1.0)
                engine.load(name, m["task"], {x["role"]: x["path"] for x in parts})
                m["loaded"] = True
                for x in parts:
                    self._touch(x["path"])
            if a["status"] == "assigned":
                self._set(m, "anunciando", 1.0)
                self._tx("pin_model", {"name": name})
                what = "de imagem" if m["task"] == "image" else "de vídeo"
                self.log(f"[máquina] hospedando o modelo {what} '{name}': a rede já pode mandar pedidos para cá")
                self._refresh()
                self._jobs_wake.set()
            self._set(m, "hospedando", 1.0)
            m["fails"] = 0
        except RefusedFile as e:   # sem `current`, quem recusou foi o check_weights do motor, num arquivo já conferido
            self._refuse(m, a, e.reason, current or self._culprit(parts))
        except (EngineError, SigmaError, OSError) as e:
            self._retry_later(m, name, e)

    @staticmethod
    def _culprit(parts):
        """Qual arquivo do modelo não passa no check_weights (o do load do motor não diz qual); None se nenhum."""
        for x in parts:
            if os.path.isfile(x["path"]):
                try:
                    media.check_weights(x["path"])
                except RefusedFile:
                    return x
        return None

    def _download(self, m, url, path, sha=None, size=None, kind="gguf", label=None):
        """Baixa um arquivo do modelo e confere SHA-256 e formato (GGUF do texto, ou safetensors/GGUF da
        imagem e do vídeo). `sha`/`size`: do arquivo (sem eles, os do modelo de texto)."""
        sha, size = sha or m["sha256"], m["size"] if size is None else size
        check = check_gguf if kind == "gguf" else media.check_weights
        what = f"'{m['name']}'" + (f" ({label})" if label and label != "main" else "")
        if not self._url_allowed(url):
            raise EngineError("a URL do modelo aponta para a rede interna; recusada")
        if os.path.exists(path):   # tamanho errado ou não conferido: confere agora
            self._set(m, "conferindo", 1.0)
            real = os.path.getsize(path)
            if real == size and sha256_file(path) == sha:
                check(path)
                self._mark_verified(path)
                return
            if real != size and (self._verified(path, real) or sha256_file(path) == sha):
                # o arquivo com este SHA-256 já está aqui, com outro tamanho: quem errou foi o cadastro deste
                # modelo. O arquivo certo fica (pode ser o VAE que outro modelo daqui usa)
                raise RefusedFile("o tamanho registrado não confere com o do arquivo de mesmo SHA-256", "sha")
            os.remove(path)
        part = path + ".part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        need = max(0, size - have)
        self._gc_models(need=need, keep=(m["sha256"], sha))   # abre espaço tirando os modelos guardados mais antigos
        if hardware.disk_free_mb(self.models_dir) * 1_000_000 < need + 1_000_000_000:
            raise EngineError("sem espaço em disco para baixar o modelo")
        self.log(f"[máquina] baixando o modelo {what} ({_mb(size)})"
                 + (f", continuando de {_mb(have)}" if have else ""))
        marks = {"next": 0.1}

        def progress(done, total):
            frac = done / total if total else 0.0
            self._set(m, "baixando", frac)
            if frac >= marks["next"]:
                marks["next"] = int(frac * 10 + 1) / 10
                self.log(f"[máquina] {what}: {frac:.0%}")

        self._set(m, "baixando", have / size if size else 0.0)
        fetch(url, path, sha, size=size, progress=progress, should_stop=self._stop.is_set,
              opener=self._download_opener())
        check(path)   # depois do SHA-256 e antes de qualquer motor abrir o arquivo
        self._mark_verified(path)
        self.log(f"[máquina] {what} baixado e conferido (SHA-256 e formato "
                 + ("GGUF" if kind == "gguf" else "do arquivo") + " ok)")

    # -- arquivos recusados (recusados.json): o mesmo arquivo ruim não é baixado de novo por 24 h. Cada sha256 guarda
    # a recusa mais nova do arquivo ({until, reason, url?, size?}, o formato de antes da 2.5) e, em "others", as outras
    # que ainda valem para ele (outra classe, outro endereço ou tamanho): uma recusa nunca apaga a outra
    @staticmethod
    def _refusals(v):
        """As recusas de um arquivo no recusados.json (a do registro e as de "others"), cada uma sem "others"."""
        if not isinstance(v, dict):
            return []
        others = v.get("others") if isinstance(v.get("others"), list) else []
        return [{k: x for k, x in e.items() if k != "others"} for e in [v, *others] if isinstance(e, dict)]

    @staticmethod
    def _nest(refusals):
        """O contrário de _refusals: a primeira recusa é o registro, e as outras vão em "others"."""
        top = dict(refusals[0])
        if refusals[1:]:
            top["others"] = refusals[1:]
        return top

    def _load_refused(self):
        if self._refused is None:
            try:
                with open(self.refused_path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            self._refused = {k: v for k, v in data.items() if HASH_RE.match(str(k)) and isinstance(v, dict)} \
                if isinstance(data, dict) else {}
        now = time.time()

        def alive(v):
            try:
                return float(v.get("until") or 0) > now
            except (TypeError, ValueError):
                return False
        live = ((k, [e for e in self._refusals(v) if alive(e)]) for k, v in self._refused.items())
        self._refused = {k: self._nest(es) for k, es in live if es}
        return self._refused

    def _refused_reason(self, x, kind):
        """Por que o arquivo `x` ({sha256, url, size}) foi recusado nas últimas 24 h, para um modelo que confere
        com `kind` ("gguf": texto; "weights": imagem/vídeo); None se não foi. Cada recusa só vale para o que deu
        errado: o arquivo que não é GGUF pode ser o safetensors certo de um modelo de imagem, e o endereço que não
        entregou aquele SHA-256 (ou aquele tamanho) não barra outro modelo que declara o arquivo certo."""
        with self._lock:
            refusals = self._refusals(self._load_refused().get(x["sha256"]))
        for entry in refusals:
            reason = entry.get("reason") if entry.get("reason") in REFUSED_TEXT else "sha"
            # na falha de SHA-256, o endereço e o tamanho que falharam (sem eles, de antes da 2.5: qualquer um)
            if reason == kind or (reason == "sha" and all(entry.get(k) in (None, x[k]) for k in ("url", "size"))):
                return reason
        return None

    def _refuse(self, m, a, reason, x):
        """O arquivo `x` do modelo (None: não se sabe qual) não confere ("sha") ou não é do formato certo ("gguf",
        "weights"): anota por 24 h só esse arquivo, apaga os bytes ruins (nunca um arquivo que outro modelo daqui
        usa, como o mesmo VAE) e mostra o porquê no painel."""
        reason = reason if reason in REFUSED_TEXT else "sha"
        if x is not None:
            # na falha de SHA-256 o fetch já descartou o que baixou, e o que estiver no disco é o arquivo certo
            if reason != "sha":
                keep = self._paths_in_use(m["name"])
                self._remove_files([p for p in (x["path"], x["path"] + ".ok", x["path"] + ".part") if p not in keep])
            entry = {"until": time.time() + REFUSED_FOR, "reason": reason}
            if reason == "sha":   # vale só para esta declaração (endereço e tamanho), não para o arquivo certo
                entry.update(url=x["url"], size=x["size"])
            with self._lock:
                refused = self._load_refused()
                # a recusa nova vai na frente, e as outras do mesmo arquivo ficam: cada declaração recusada segue
                # sem baixar de novo pelas 24 h dela (senão duas recusas se apagariam e cada volta baixaria as duas)
                refused[x["sha256"]] = self._nest([entry, *self._refusals(refused.get(x["sha256"]))])
                data = dict(refused)
            try:
                os.makedirs(self.models_dir, exist_ok=True)
                with open(self.refused_path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(self.refused_path + ".tmp", self.refused_path)
            except OSError as e:
                self.log(f"[máquina] não consegui anotar o arquivo recusado em {self.refused_path}: {e}")
        self._hold_refused(m, a, reason)
        self._error(f"modelo {m['name']}", f"modelo '{m['name']}': {REFUSED_TEXT[reason]}"
                    + ("; este computador não baixa esse arquivo de novo nas próximas 24 h" if x is not None else ""))

    def _hold_refused(self, m, a, reason):
        """O modelo não vai ser hospedado aqui (arquivo recusado, cadastro errado): mostra o porquê e recusa a
        atribuição na rede uma vez. Parada, ela venceria e, sem ninguém hospedando, contaria contra o modelo."""
        self._set(m, "recusado", 0.0, REFUSED_TEXT[reason])
        if a.get("status") == "assigned" and not m["declined"]:
            m["declined"] = self._decline(m["name"])

    def _paths_in_use(self, name):
        """Arquivos que outro modelo daqui usa (hospedado, baixando ou só atribuído): o mesmo VAE ou codificador
        pode servir a dois modelos, e recusar um nunca apaga o arquivo do outro."""
        with self._lock:
            others = [m for n, m in self._models.items() if n != name]
            known = set(self._models)
        for a in (self._record or {}).get("assignments") or []:
            if a.get("name") not in known and a.get("name") != name:
                try:
                    others.append(self._model_info(a["name"], a))
                except (KeyError, TypeError, ValueError):
                    pass
        return {p for m in others for p in self._all_paths(m)}

    def _decline(self, name):
        """Recusa o modelo na rede (`decline_model`): a atribuição sai na hora e entra em cooldown, sem punição
        de reputação. Recusar um arquivo ruim (sha/GGUF errado, URL interna, sem orçamento) deixa de custar
        reputação (SPEC-v2.2 §3). O aviso vai sem esperar o bloco (wait=False) e uma falha dele não para o laço:
        se não entrar, a atribuição expira sozinha depois, sem punir esta máquina. True se a tx foi aceita."""
        try:
            self._tx("decline_model", {"name": name}, wait=False)
            return True
        except SigmaError as e:
            self.log(f"[máquina] não consegui recusar o modelo '{name}' na rede ({e}); a atribuição expira sozinha")
            return False

    # -- arquivos guardados: ficam enquanto o modelo existir na rede, e saem os mais antigos se faltar disco
    def _model_files(self):
        """sha256 -> {"paths", "bytes", "mtime"} dos arquivos de modelo nesta máquina (.gguf, .ok e .part)."""
        out = {}
        try:
            with os.scandir(self.models_dir) as it:
                for e in it:
                    mt = MODEL_FILE.match(e.name)
                    if not mt or not e.is_file():
                        continue
                    st = e.stat()
                    f = out.setdefault(mt.group(1), {"paths": [], "bytes": 0, "mtime": 0.0})
                    f["paths"].append(e.path)
                    f["bytes"] += st.st_size
                    f["mtime"] = max(f["mtime"], st.st_mtime)
        except OSError:
            pass
        return out

    def _network_models(self, fresh=False):
        """{sha256: nome} dos modelos que existem na rede, ou None se o /models não respondeu (aí nada sai)."""
        t, net = self._network
        if net is not None and not fresh and time.time() - t < NETWORK_EVERY:
            return net
        try:
            models = self.client.models()
        except SigmaError:
            return None
        if not isinstance(models, dict):
            return None
        net = {str(x.get("sha256")).lower(): n for n, x in models.items() if isinstance(x, dict)}
        for n, x in models.items():   # v2: os arquivos extras (VAE, codificadores) também são da rede
            if isinstance(x, dict) and isinstance(x.get("files"), list):
                for f in x["files"]:
                    if isinstance(f, dict):
                        net.setdefault(str(f.get("sha256")).lower(), n)
        self._network = (time.time(), net)
        self._names.update({s: n for s, n in net.items()})
        return net

    def _protected(self):
        """Arquivos que não saem: os que a rede atribuiu a esta máquina (ou que ela hospeda) e os que estão em uso."""
        rec = self._record or {}
        keep = {str(a.get("sha256")) for a in rec.get("assignments") or []}
        keep |= {str(f.get("sha256")).lower() for a in rec.get("assignments") or []
                 for f in a.get("files") or [] if isinstance(f, dict)}
        with self._lock:
            keep |= {m["sha256"] for m in self._models.values()}
            keep |= {str(f.get("sha256")).lower() for m in self._models.values() for f in m.get("files") or []
                     if isinstance(f, dict)}
        return keep

    def _label(self, sha):
        return f"'{self._names[sha]}'" if sha in self._names else f"{sha[:12]}…"

    def _gc_models(self, need=0, keep=()):
        """Arquivos guardados (sem atribuição aqui): saem os de modelos que deixaram a rede e, se o disco passar do
        combinado (disk_gb) ou faltar espaço para baixar `need` bytes, os parados há mais tempo primeiro. Se o
        /models não responde, nada sai do disco."""
        if self._record is None:
            return
        protected = self._protected() | set(keep)
        cached = {s: f for s, f in self._model_files().items() if s not in protected}
        if not cached:
            return
        network = self._network_models()
        if network is None:
            return
        for sha in [s for s in cached if s not in network]:
            self._remove_files(cached.pop(sha)["paths"])
            self.log(f"[máquina] apagado o arquivo do modelo {self._label(sha)}: o modelo saiu da rede")
        budget = hardware.gb_to_mb(self.settings["disk_gb"]) * 1_000_000
        used = self._disk_used_bytes()
        free = hardware.disk_free_mb(self.models_dir) * 1_000_000
        for sha, f in sorted(cached.items(), key=lambda kv: kv[1]["mtime"]):
            if used + need <= budget and (not need or free >= need + 1_000_000_000):
                break
            self._remove_files(f["paths"])
            used -= f["bytes"]
            free += f["bytes"]
            self.log(f"[máquina] apagado o arquivo guardado do modelo {self._label(sha)} (o parado há mais tempo) "
                     "para liberar espaço em disco")

    def _pause_model(self, name, a):
        """IA desligada nas configurações: tira o modelo da memória, mas guarda o arquivo."""
        m = self._state(name, a)
        if m["loaded"]:
            try:
                self._engine_of(m).unload(name)
            except EngineError as e:
                self.log(f"[máquina] aviso ao descarregar '{name}': {e}")
            m["loaded"] = False
        self._set(m, "IA desligada")

    def _drop(self, name):
        """Modelo saiu da lista desta máquina (IA desligada, PC desligado por um tempo, reatribuído, apagado):
        descarrega do motor. O arquivo fica guardado enquanto o modelo existir na rede; sai na hora se o modelo
        deixou a rede (e nada sai se o nó não responde)."""
        with self._lock:
            m = self._models.pop(name)
            still_used = any(x["sha256"] == m["sha256"] for x in self._models.values())
        try:
            self._engine_of(m).unload(name)
        except EngineError as e:
            self.log(f"[máquina] aviso ao descarregar '{name}': {e}")
        network = None if still_used else self._network_models(fresh=True)
        if network is not None and m["sha256"] not in network:
            # os extras (VAE, codificadores) que outro modelo da rede ou desta máquina usa ficam
            keep = self._protected()
            gone = []
            for p in self._all_paths(m):
                sha = os.path.basename(p).split(".", 1)[0]
                if sha not in keep and (sha == m["sha256"] or sha not in network):
                    gone.append(p)
            self._remove_files(gone)
            self.log(f"[máquina] o modelo '{name}' saiu da rede: arquivo apagado")
        else:
            self.log(f"[máquina] o modelo '{name}' saiu deste computador; o arquivo fica guardado caso a rede o "
                     "coloque aqui de novo")

    def _swap_engine(self):
        """Mudou o motor nas configurações: descarrega tudo e recarrega no novo."""
        self._swap = False
        old, old_media = self.engine, self.media
        self.engine = EngineManager(self.data_dir, self.settings["engine"], self.log, self.settings["ai_threads"])
        self.engine.rpc_provider = self.group.rpc if self.group else None
        self.media = media.MediaManager(self.data_dir, self.settings["engine"], self.log, self.settings["ai_threads"])
        self._engine_desc = self._media_desc = (0.0, None)
        old_media.shutdown()
        with self._lock:
            for m in self._models.values():
                m.update(loaded=False, status="na fila", retry_at=0.0)
        old.shutdown()
        self._registered = None   # o "ai" do registro pode mudar com o motor novo

    # ---------------------------------------------------------------- sites
    def _pin_sites(self):
        rec = self._record
        if not rec or not rec.get("online"):
            return
        sites = self.client.sites()
        mine = {n for n, s in sites.items() if self.address in (s.get("pinners") or {})}
        disk = (self._reg_hw or rec.get("hw") or {}).get("disk_mb", 0) * 1_000_000
        used = sum(int(a.get("size") or 0) for a in rec.get("assignments") or []) + \
            sum(int(sites[n].get("size") or 0) for n in mine)
        candidates = [s for s in sites.values() if s.get("status") == "active" and s["name"] not in mine
                      and len(s.get("pinners") or {}) < s.get("replicas", 1)
                      and time.time() > self._site_cooldown.get(s["name"], 0)]
        random.shuffle(candidates)   # máquinas diferentes tentam sites diferentes primeiro
        for site in candidates:
            if self._stop.is_set():
                break
            if used + int(site.get("size") or 0) > disk:
                continue
            try:
                self._fetch_site(site)
                self._tx("pin_site", {"name": site["name"], "version": site["version"]})
                mine.add(site["name"])
                used += int(site.get("size") or 0)
                self.log(f"[máquina] hospedando o site '{site['name']}' ({_pt(site.get('size', 0))} bytes)")
            except (SigmaError, OSError, ValueError) as e:
                self._site_cooldown[site["name"]] = time.time() + SITE_COOLDOWN
                self.log(f"[máquina] não hospedei o site '{site['name']}': {e}")
        self._cleanup_blobs(sites, mine)

    def _fetch_site(self, site):
        """Baixa do nó os arquivos que faltam: confere antes o espaço livre pelos tamanhos declarados e, arquivo por
        arquivo, o tamanho de verdade. Tamanho declarado que não confere recusa o site (o disco e o aluguel usam
        esse número, e quem declara é o dono do site)."""
        files = []
        for path, f in sorted((site.get("manifest") or {}).items()):
            h, declared = str(f.get("hash")), f.get("size")
            if not HASH_RE.match(h) or not isinstance(declared, int) or isinstance(declared, bool) or declared < 0:
                raise ValueError(f"o arquivo {path} tem hash ou tamanho inválido")
            files.append((path, h, declared, os.path.join(self.blob_dir, h)))
        missing = sum(declared for _, _, declared, p in files if not os.path.exists(p))
        if hardware.disk_free_mb(self.blob_dir) * 1_000_000 < missing + 500_000_000:   # folga para o sistema
            raise ValueError(f"sem espaço em disco para os arquivos do site ({_pt(missing)} bytes)")
        for path, h, declared, p in files:
            if os.path.exists(p):
                real = os.path.getsize(p)
            else:
                data = self.client.get_blob(h)   # o cliente confere o hash
                real = len(data)
                if real == declared:
                    with open(p + ".tmp", "wb") as out:
                        out.write(data)
                    os.replace(p + ".tmp", p)
            if real != declared:
                raise ValueError(f"o tamanho declarado de {path} não confere ({_pt(declared)} ≠ {_pt(real)} bytes)")

    def _cleanup_blobs(self, sites, mine):
        keep = {f["hash"] for n in mine if n in sites for f in sites[n]["manifest"].values()}
        try:
            names = os.listdir(self.blob_dir)
        except OSError:
            return
        for fn in names:
            if fn not in keep:
                try:
                    os.remove(os.path.join(self.blob_dir, fn))
                except OSError:
                    pass

    def _start_server(self):
        """Com URL pública, serve os arquivos dos sites hospedados em /blob/<hash> (o gateway busca aqui)."""
        url = str(self.settings.get("public_url") or "")
        if not url:
            return
        blob_dir = self.blob_dir
        port = int(self.settings.get("port") or urllib.parse.urlparse(url).port or 8801)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                h = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
                p = os.path.join(blob_dir, h)
                if not (self.path.startswith("/blob/") and HASH_RE.match(h) and os.path.isfile(p)):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with open(p, "rb") as f:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(os.path.getsize(p)))
                    self.end_headers()
                    for block in iter(lambda: f.read(1 << 16), b""):
                        self.wfile.write(block)

        try:
            self._server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        except OSError as e:
            self._error("servidor", f"não consegui abrir a porta {port} para servir os sites: {e}")
            return
        threading.Thread(target=self._server.serve_forever, name="sigma-maquina-blobs", daemon=True).start()
        self.log(f"[máquina] servindo arquivos de sites em {url.rstrip('/')}/blob/<hash> (porta {port})")

    def _stop_server(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ----------------------------------------------------------------- jobs
    def _hosting(self):
        """Hospeda algum modelo (ou a rede acha que sim)? Então pedidos direcionados podem chegar a qualquer hora."""
        if (self._record or {}).get("models"):
            return True
        with self._lock:
            return any(m["status"] == "hospedando" for m in self._models.values())

    def _jobs_interval(self):
        """Pedidos direcionados precisam ser reivindicados em segundos; sem modelo, só há computação (prazo de
        minutos), então consulta bem menos: mil apps ligados não podem martelar o nó."""
        return JOBS_FAST if self._hosting() else JOBS_SLOW

    def _jobs_loop(self):
        while not self._stop.is_set():
            try:
                self._work_jobs()
                self._ok("jobs")
            except SigmaError as e:
                self._error("jobs", f"jobs: {e}")
            except Exception as e:
                self._error("jobs", f"jobs: erro inesperado: {type(e).__name__}: {e}")
            self._jobs_wake.wait(self._jobs_interval())
            self._jobs_wake.clear()

    def _work_jobs(self):
        """Uma volta pelos jobs abertos: reivindica os direcionados a esta máquina (sempre, mesmo enquanto responde
        outro), pega um público quando está livre e uma tarefa de computação de cada vez."""
        rec = self._record
        if not rec or not rec.get("online"):
            return
        with self._lock:
            ready = {n for n, m in self._models.items() if m["status"] == "hospedando" and m["loaded"]}
        compute = bool(self.settings["compute_enabled"] and rec.get("compute"))
        if not ready and not compute and not rec.get("models"):
            return
        prices = self._prices(rec)
        self._forget_handled()
        seen_by_client = {}   # cliente -> direcionados abertos a esta máquina vistos nesta passada (limite por fila)
        for job in self.client.jobs("open"):
            if self._stop.is_set():
                return
            jid = job.get("id")
            if not isinstance(jid, str) or jid in self._handled or job.get("client") == self.address:
                continue
            claims = job.get("claims") or {}
            if self.address in claims:
                continue
            if job.get("kind") == "ai":
                target = job.get("machine")
                if target:
                    if target == self.address:
                        # um cliente com mais de MAX_QUEUE direcionados abertos aqui: os extras voltam "busy" na
                        # hora, para um flood não encher a fila nem empurrar os outros para o prazo
                        n = seen_by_client[job.get("client")] = seen_by_client.get(job.get("client"), 0) + 1
                        self._take_targeted(job, force_busy=n > MAX_QUEUE)
                    continue   # direcionado a outra máquina: não é para esta
                kind = job.get("media") or "text"
                free = not self._queue_size(kind) and (kind != "text" or self._engine_down_since is None)
                if job.get("model") in ready and free \
                        and prices.get(TASK_PRICE.get(kind, "?"), 10 ** 18) <= job.get("max_price", 0):
                    self._take_public(job)
            elif job.get("kind") == "compute" and compute and len(claims) < job.get("replicas", 1) \
                    and prices["compute_per_unit"] <= job.get("max_price", 0) and not self._computing():
                self._take_compute(job)

    def _forget_handled(self):
        old = time.time() - 2 * 3600   # um job aberto expira bem antes disso
        self._handled = {j: t for j, t in self._handled.items() if t > old}
        if len(self._handled) > 5000:
            self._handled = dict(sorted(self._handled.items(), key=lambda kv: kv[1])[-5000:])
        if len(self._claim_fails) > 1000:
            self._claim_fails.clear()

    def _claim(self, job):
        """Reivindica o job; True se ele ficou com esta máquina."""
        try:
            self._tx("claim_job", {"job_id": job["id"]})
        except SigmaError:
            return False   # outra máquina chegou antes, quem pediu cancelou, ou o nó não respondeu
        try:
            return self.address in (self.client.job(job["id"]).get("claims") or {})
        except SigmaError:
            return True    # a tx entrou num bloco: o job é desta máquina, mesmo sem conseguir conferir agora

    def _claim_targeted(self, job):
        """Reivindica um direcionado sem esperar o bloco (wait=False): o laço de jobs não pode travar esperando
        confirmação quando chega um monte de pedidos de uma vez. Só esta máquina pode reivindicar um pedido
        direcionado a ela, então basta a tx ter sido aceita; se o nó falhar por um instante, tenta na próxima
        passada. Os nonces vão em fila sob _tx_lock."""
        try:
            self._tx("claim_job", {"job_id": job["id"]}, wait=False)
            return True
        except SigmaError:
            return False

    def _queue_size(self, kind="text"):
        """Pedidos na fila (contando o que está sendo respondido): a de texto ou a de imagem/vídeo."""
        with self._lock:
            if kind == "text":
                return len(self._queue) + (1 if self._current else 0)
            return len(self._mqueue) + (1 if self._mcurrent else 0)

    def _take_targeted(self, job, force_busy=False):
        """Pedido direcionado a esta máquina: reivindica na hora (sem travar, wait=False) e responde em ordem. O
        que não dá para atender volta logo como falha, de uma thread de respostas: quem pediu recebe o dinheiro de
        volta e esta máquina não leva falta. `force_busy`: cliente com fila cheia aqui, o extra volta "busy"."""
        jid = job["id"]
        self._handled[jid] = time.time()
        if not self._claim_targeted(job):
            tries = self._claim_fails.get(jid, 0) + 1
            self._claim_fails[jid] = tries
            if tries < CLAIM_TRIES:   # o nó pode ter falhado por um instante: tenta de novo na próxima volta
                self._handled.pop(jid, None)
            return
        self._claim_fails.pop(jid, None)
        kind = job.get("media") or "text"
        if force_busy or self._queue_size(kind) >= (MAX_QUEUE if kind == "text" else MAX_MEDIA_QUEUE):
            return self._fail_async(job, "busy")
        why = self._why_not(job.get("model"))
        if why:
            return self._fail_async(job, "engine", why)
        item = {"job": job, "since": time.time()}
        try:
            self._prepare(item)
        except _Unreadable:
            return self._fail_async(job, "unreadable")
        except SigmaError:
            pass   # o nó não entregou o pedido agora: a fila tenta de novo na hora de responder
        except Exception as e:   # o job já é desta máquina: sem resposta, viraria falta no prazo
            self._error("ia", f"IA: o pedido do job {jid[:10]} não abriu: {type(e).__name__}")
            return self._fail_async(job, "unreadable")
        else:
            code, msg = self._over_limit(item)
            if code:
                return self._fail_async(job, code, msg)
        self._enqueue(item)

    def _take_public(self, job):
        """Pedido sem máquina escolhida (de clientes antigos): como na v2, só reivindica o que consegue ler."""
        jid = job["id"]
        self._handled[jid] = time.time()
        item = {"job": job}
        try:
            self._prepare(item)
        except _Unreadable:
            self.log(f"[máquina] job {jid[:10]} ignorado: pedido ilegível")
            return
        except SigmaError:
            self._handled.pop(jid, None)   # o nó não entregou agora: tenta na próxima volta
            return
        if not self._claim(job):
            return
        item["since"] = time.time()
        code, msg = self._over_limit(item)
        if code:
            return self._fail(job, code, msg)
        self._enqueue(item)

    def _over_limit(self, item):
        """Por que o pedido não pode ser respondido: "context" (a conversa não cabe no contexto do modelo) ou
        "busy" (o escrow não paga nem MIN_ANSWER_TOKENS de resposta). (código, mensagem) ou (None, None)."""
        if item.get("media"):   # imagem/vídeo: o escrow precisa pagar o que foi pedido no preço desta máquina
            if item.get("pays", 1) < 1:
                return "busy", "o valor reservado não paga este pedido no preço desta máquina"
            return None, None
        if item["est"] > CONTEXT - 32:
            return "context", None
        if item.get("pays", CONTEXT) < MIN_ANSWER_TOKENS:
            return "busy", "o valor reservado não paga esta resposta"
        return None, None

    def _why_not(self, model):
        """Por que esta máquina não consegue responder pedidos deste modelo agora (None = consegue ou já vai)."""
        with self._lock:
            m = self._models.get(model)
            task = m["task"] if m else next((a.get("task") for a in (self._record or {}).get("assignments") or []
                                             if a.get("name") == model), "text")
        if task in ("image", "video"):
            if not self.media.available():
                return "esta máquina não tem o motor de imagem e vídeo"
        elif self._engine_down_since is not None:
            return FAIL_TEXT["engine"]
        if not self.settings["ai_enabled"]:
            return "a IA desta máquina foi desligada"
        if not self._model_pending(model) and not self._model_ready(model):
            return f"o modelo '{model}' não está carregado nesta máquina agora"
        return None

    def _model_ready(self, model):
        with self._lock:
            m = self._models.get(model)
            return bool(m and m["status"] == "hospedando" and m["loaded"])

    def _model_pending(self, model):
        """O modelo ainda vai ficar pronto: baixando/carregando, ou atribuído aqui e o laço dos modelos ainda não
        passou por ele (logo depois de ligar o PC, a rede já manda pedidos para o que esta máquina hospeda)."""
        with self._lock:
            m = self._models.get(model)
            if m is not None:
                return m["status"] in LOADING
        return any(a.get("name") == model for a in (self._record or {}).get("assignments") or [])

    def _prepare(self, item):
        """Baixa e abre o pedido (cifrado para a chave desta máquina). Nada dele vai para o disco nem para o log."""
        job = item["job"]
        h = job.get("spec_hash")
        if not isinstance(h, str) or not HASH_RE.match(h):
            raise _Unreadable()
        blob = self.client.get_blob(h)
        if is_sealed(blob):
            try:
                blob = self.wallet.unseal(blob)
            except ValueError:
                raise _Unreadable() from None
        if job.get("media") in ("image", "video"):
            return self._prepare_media(item, blob)
        try:
            spec = json.loads(blob)
            raw = spec["messages"]
            if not isinstance(raw, list) or not raw:
                raise ValueError("sem mensagens")
            messages = [{"role": str(x["role"]), "content": str(x["content"])} for x in raw]
            want = int(spec.get("max_tokens") or 512)
            json_mode = bool(spec.get("json"))
        except MALFORMED:
            raise _Unreadable() from None
        jmax = job.get("max_tokens")   # teto público do job (a chain confere que o escrow o cobre)
        if isinstance(jmax, int) and not isinstance(jmax, bool) and jmax > 0:
            want = min(want, jmax)
        est = estimate_tokens(messages)
        # quanto o escrow paga de resposta: escrow*1000/preço são os tokens pagos ao todo, menos a entrada (est).
        # Com escrow que não cobre o preço, isto fica pequeno (ou negativo) e o pedido volta "busy" (_over_limit).
        price = self._prices(self._record)["ai_per_1k_tokens"]
        escrow = job.get("escrow")
        pays = (escrow * 1000 // price - est) if isinstance(escrow, int) and not isinstance(escrow, bool) else CONTEXT
        item.update(messages=messages, est=est, json=json_mode, pays=pays,
                    max_tokens=min(want, pays, CONTEXT - est))

    def _prepare_media(self, item, blob):
        """Pedido de imagem/vídeo (v2): o spec vira o que o motor roda (media.clean_spec: listas fechadas e
        limites) e nunca pede mais do que o job pagou (`units`)."""
        job = item["job"]
        units = job.get("units")
        if not isinstance(units, int) or isinstance(units, bool) or units < 1:
            raise _Unreadable()
        try:
            spec = media.clean_spec(job["media"], json.loads(blob), units)
        except MALFORMED:
            raise _Unreadable() from None
        want = spec["count"] if spec["task"] == "image" else spec["seconds"]
        price = self._prices(self._record, media=True)[TASK_PRICE[job["media"]]]
        escrow = job.get("escrow")
        pays = (escrow // price - want + 1) if isinstance(escrow, int) and not isinstance(escrow, bool) else 1
        item.update(media=job["media"], spec=spec, pays=pays, units=want)

    def _enqueue(self, item):
        with self._queue_cv:
            if item["job"].get("media"):
                self._mqueue.append(item)
            else:
                self._queue.append(item)
            self._queue_cv.notify_all()   # acorda o laço "ia"/"midia" (e o de respostas, que vê a fila dele vazia)

    def _fail_async(self, job, code, msg=None):
        """Enfileira uma falha para a thread de respostas mandar (wait=False), sem travar o laço de jobs: num
        flood de direcionados, cada claim e cada falha saem sem esperar bloco."""
        with self._queue_cv:
            self._fails.append((job, code, msg))
            self._queue_cv.notify_all()

    def _next_fail(self, wait=0.0):
        with self._queue_cv:
            if not self._fails and wait:
                self._queue_cv.wait(wait)
            if not self._fails or (wait and self._stop.is_set()):
                return None
            return self._fails.popleft()

    def _run_fail(self, task):
        job, code, msg = task
        try:
            self._fail(job, code, msg, wait=False)
        except SigmaError as e:
            self._error("ia", f"IA: não consegui entregar a falha do job {job['id'][:10]}: {e}")
        except Exception as e:
            self._error("ia", f"IA: erro inesperado ao recusar o job {job['id'][:10]}: {type(e).__name__}")

    def _answers_loop(self):
        """Manda as falhas dos pedidos (busy/unreadable/context/engine) sem travar o laço de jobs: put_blob +
        submit_result com wait=False, uma de cada vez. Separado do laço "ia" para que uma inferência longa em
        andamento não atrase a recusa de um pedido novo."""
        while not self._stop.is_set():
            task = self._next_fail(wait=1.0)
            if task is not None:
                self._run_fail(task)

    def _drain_answers(self):
        """Manda agora todas as falhas pendentes (o laço "respostas" faz isso sozinho; os testes chamam direto)."""
        while (task := self._next_fail()) is not None:
            self._run_fail(task)

    @staticmethod
    def _seal(job, data):
        """Com `client_pub`, a resposta (sucesso ou falha) vai cifrada: só quem pediu consegue ler."""
        pub = job.get("client_pub")
        return seal(pub, data) if pub else data

    def _fail(self, job, code, msg=None, wait=False):
        """Responde o pedido como falha: o job fecha "failed", o valor reservado volta inteiro para quem pediu e
        nada conta contra esta máquina. Por padrão sem esperar o bloco (wait=False): a falha não precisa de
        confirmação e o laço que a manda não pode travar."""
        text = msg or FAIL_TEXT[code]
        data = canon({"text": "", "error": text, "code": code, "model": job.get("model"), "machine": self.address})
        try:
            blob = self._seal(job, data)
        except ValueError:
            blob = data   # a falha não leva nada do pedido: pode ir sem cifra
        usage = {"units": 0} if job.get("media") else {"tokens_in": 0, "tokens_out": 0}
        self._deliver(job, blob, usage, failed=True, wait=wait)
        self.log(f"[máquina] IA {job.get('model')} job {job['id'][:10]}: não respondido ({code}: {text})")

    def _deliver(self, job, blob, usage, failed=False, wait=True):
        """put_blob + submit_result. Uma queda do nó no meio não pode perder a resposta (o claim seguraria o job
        até o prazo e viraria falta): tenta até 3 vezes, conferindo antes se a anterior não tinha entrado."""
        data = {"job_id": job["id"], "usage": usage}
        if failed:
            data["failed"] = True
        for attempt in range(3):
            try:
                data["result_hash"] = self.client.put_blob(blob)
                self._tx("submit_result", data, wait=wait)
                return
            except SigmaError:
                if attempt == 2 or self._stop.wait(3):
                    raise
                try:
                    cur = self.client.job(job["id"])
                except SigmaError:
                    continue
                if self.address in (cur.get("results") or {}) or cur.get("status") not in ("open", "running"):
                    return   # a tentativa anterior entrou (ou o job já fechou): nada a reenviar

    def _wait_model(self, model):
        """Espera o modelo ficar pronto (ex.: carregando logo depois de ligar), no máximo MODEL_WAIT segundos."""
        deadline = time.time() + MODEL_WAIT
        while True:
            if self._model_ready(model):
                return True
            if not self._model_pending(model) or time.time() > deadline or self._stop.is_set():
                return False
            self._stop.wait(1)

    def _answer(self, item):
        """Responde um pedido da fila. A resposta vai cifrada para quem perguntou; só tokens e tempo vão ao log."""
        job = item["job"]
        if "messages" not in item and "spec" not in item:   # o pedido (texto ou mídia) não tinha chegado no claim
            for attempt in range(3):
                try:
                    self._prepare(item)
                    break
                except _Unreadable:
                    return self._fail(job, "unreadable")
                except SigmaError as e:
                    if attempt == 2 or self._stop.wait(5):
                        return self._fail(job, "unreadable", f"não consegui baixar o pedido do nó: {e}")
                except Exception as e:   # nenhum pedido reivindicado fica sem resposta (viraria falta no prazo)
                    self._error("ia", f"IA: o pedido do job {job['id'][:10]} não abriu: {type(e).__name__}")
                    return self._fail(job, "unreadable")
            code, msg = self._over_limit(item)
            if code:
                return self._fail(job, code, msg)
        model = job.get("model")
        if not self._wait_model(model):
            return self._fail(job, "engine", self._why_not(model) or f"o modelo '{model}' não ficou pronto a tempo")
        if item.get("media"):
            return self._answer_media(item)
        left = CLAIM_TIMEOUT - (time.time() - item["since"]) - 60   # folga para a resposta entrar num bloco
        if left < 30:
            return self._fail(job, "busy", "esta máquina demorou demais para chegar a este pedido na fila")
        t0 = time.time()
        self._busy(True)
        try:
            text, tin, tout = self.engine.chat(model, item["messages"], max(1, item["max_tokens"]),
                                               json_mode=item["json"], timeout=left)
        except ContextOverflow:   # o motor recusou por contexto (ou cortou calado): falha "context", não "engine"
            return self._fail(job, "context")
        except EngineError as e:
            self._error("ia", f"IA falhou no job {job['id'][:10]}: {e}")
            return self._fail(job, "engine", f"o motor de IA desta máquina falhou: {str(e)[:200]}")
        finally:
            self._busy(False)
        data = canon({"text": text, "model": model, "machine": self.address})
        try:
            blob = self._seal(job, data)
        except ValueError:
            return self._fail(job, "unreadable", "a chave pública de quem pediu é inválida")
        tin, tout = (min(MAX_TOKENS, max(0, int(x))) for x in (tin, tout))
        self._deliver(job, blob, {"tokens_in": tin, "tokens_out": tout})
        self._ok("ia")
        secs = f"{time.time() - t0:.1f}".replace(".", ",")
        self.log(f"[máquina] IA {model} job {job['id'][:10]}: {tin}+{tout} tokens em {secs} s")

    def _answer_media(self, item):
        """Gera as imagens ou o vídeo e entrega cifrado para quem pediu; paga-se só o que saiu (units)."""
        job, spec = item["job"], item["spec"]
        model = job.get("model")
        left = MEDIA_CLAIM_TIMEOUT[item["media"]] - (time.time() - item["since"]) - 120
        if left < 60:
            return self._fail(job, "busy", "esta máquina demorou demais para chegar a este pedido na fila")
        t0 = time.time()
        self._busy(True)
        try:
            results = self.media.generate(model, spec, timeout=left, should_stop=self._stop.is_set)
        except EngineError as e:
            self._error("ia", f"geração falhou no job {job['id'][:10]}: {e}")
            return self._fail(job, "engine", f"o motor de imagem e vídeo desta máquina falhou: {str(e)[:200]}")
        except Exception as e:   # erro que o motor não previu (ex.: o sd-cli que nem abre): falha, nunca falta
            self._error("ia", f"geração falhou no job {job['id'][:10]}: erro inesperado: {type(e).__name__}")
            return self._fail(job, "engine", "o motor de imagem e vídeo desta máquina falhou")
        finally:
            self._busy(False)
        out = [dict(meta, mime=mime, data=base64.b64encode(data).decode()) for data, mime, meta in results]
        units = len(out) if item["media"] == "image" else min(item["units"], int(out[0].get("seconds") or 0))
        data = canon({"media": out, "task": item["media"], "model": model, "machine": self.address})
        try:
            blob = self._seal(job, data)
        except ValueError:
            return self._fail(job, "unreadable", "a chave pública de quem pediu é inválida")
        self._deliver(job, blob, {"units": max(0, min(units, item["units"]))})
        self._ok("ia")
        secs = f"{time.time() - t0:.1f}".replace(".", ",")
        what = f"{units} imagem(ns)" if item["media"] == "image" else f"{units} s de vídeo"
        self.log(f"[máquina] {item['media']} {model} job {job['id'][:10]}: {what} em {secs} s")

    def _next_mitem(self, wait=0.0):
        with self._queue_cv:
            if not self._mqueue and wait:
                self._queue_cv.wait(wait)
            if not self._mqueue or (wait and self._stop.is_set()):
                return None
            self._mcurrent = self._mqueue.popleft()
            return self._mcurrent

    def _run_mitem(self, item):
        try:
            self._answer(item)
        except SigmaError as e:
            self._error("ia", f"IA: não consegui entregar o resultado do job {item['job']['id'][:10]}: {e}")
        except Exception as e:
            self._error("ia", f"IA: erro inesperado no job {item['job']['id'][:10]}: {type(e).__name__}")
        finally:
            with self._lock:
                self._mcurrent = None

    def _media_loop(self):
        """Gera imagem/vídeo, um por vez, numa fila separada da de texto: um vídeo de vários minutos não segura
        as respostas de texto que chegam enquanto isso."""
        while not self._stop.is_set():
            item = self._next_mitem(wait=1.0)
            if item is not None:
                self._run_mitem(item)

    def _next_item(self, wait=0.0):
        with self._queue_cv:
            if not self._queue and wait:
                self._queue_cv.wait(wait)
            if not self._queue or (wait and self._stop.is_set()):
                return None
            self._current = self._queue.popleft()
            return self._current

    def _run_item(self, item):
        try:
            self._answer(item)
        except SigmaError as e:
            self._error("ia", f"IA: não consegui entregar a resposta do job {item['job']['id'][:10]}: {e}")
        except Exception as e:
            self._error("ia", f"IA: erro inesperado no job {item['job']['id'][:10]}: {type(e).__name__}")
        finally:
            with self._lock:
                self._current = None

    def _ai_loop(self):
        """Responde a fila de pedidos de IA, um por vez e na ordem; enquanto isso o laço dos jobs segue
        reivindicando."""
        while not self._stop.is_set():
            item = self._next_item(wait=1.0)
            if item is not None:
                self._run_item(item)

    def _drain_queue(self):
        """Responde agora, na ordem, tudo o que está na fila (o laço 'ia' faz isso sozinho; os testes chamam direto)."""
        while (item := self._next_item()) is not None:
            self._run_item(item)
        while (item := self._next_mitem()) is not None:
            self._run_mitem(item)

    def _fail_queue(self, code, msg, budget=10.0):
        """Ao desligar: o que ficou na fila (respostas pendentes e falhas já decididas) volta como falha, sem
        esperar os blocos (no máximo `budget` segundos)."""
        end = time.time() + budget
        while time.time() < end:
            with self._lock:
                if self._fails:
                    job, fcode, fmsg = self._fails.popleft()
                elif self._queue:
                    job, fcode, fmsg = self._queue.popleft()["job"], code, msg
                elif self._mqueue:
                    job, fcode, fmsg = self._mqueue.popleft()["job"], code, msg
                else:
                    return
            try:
                self._fail(job, fcode, fmsg, wait=False)
            except SigmaError:
                return

    def _busy(self, on):
        """Inferência na CPU: pausa o minerador enquanto durar (e só retoma se foi o host que pausou)."""
        with self._lock:
            if on:
                self._pausing += 1
                if self._pausing == 1 and self.miner and not self.miner.stats().get("paused"):
                    self.miner.pause()
                    self._we_paused = True
            else:
                self._pausing = max(0, self._pausing - 1)
                if self._pausing == 0 and self._we_paused:
                    if self.miner:
                        self.miner.resume()
                    self._we_paused = False

    def _computing(self):
        return bool(self._compute and self._compute.is_alive())

    def _take_compute(self, job):
        self._handled[job["id"]] = time.time()
        try:
            units = tasks.validate(job.get("task"))
        except (ValueError, TypeError):
            return
        # só reivindica se o valor reservado cobre o preço desta máquina por cópia: senão computaria de graça
        price = self._prices(self._record)["compute_per_unit"]
        escrow, replicas = job.get("escrow"), job.get("replicas", 1) or 1
        if not (isinstance(escrow, int) and not isinstance(escrow, bool) and escrow // replicas >= units * price):
            return
        if not self._claim(job):
            return
        self._compute = threading.Thread(target=self._do_compute, args=(job,), name="sigma-maquina-computacao",
                                         daemon=True)
        self._compute.start()

    def _do_compute(self, job):
        """Roda numa thread à parte: a tarefa demora minutos e os pedidos de IA não podem esperar por ela."""
        try:
            result = run_task(job["task"], should_stop=self._stop.is_set)
        except SigmaError as e:
            self._error("computação", f"computação do job {job['id'][:10]}: {e}")
            return
        cheat = bool(self.settings.get("cheat"))
        if cheat:   # máquina desonesta: só existe para a demo mostrar a punição
            result = {k: (v + 1 if isinstance(v, int) else "0" * 64) for k, v in result.items()}
        try:
            h = self.client.put_blob(canon(result))
            self._tx("submit_result", {"job_id": job["id"], "result_hash": h})
        except SigmaError as e:
            self._error("computação", f"computação do job {job['id'][:10]}: não consegui entregar: {e}")
            return
        self.log(f"[máquina] computação {job['task']['name']} job {job['id'][:10]} -> {result}"
                 + ("  (TRAPACEANDO)" if cheat else ""))
