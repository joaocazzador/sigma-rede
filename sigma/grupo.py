"""Grupo: um coordenador roda modelos que não cabem nele usando a memória de outros computadores.

O coordenador roda o llama-server de sempre com `--rpc`: as camadas do modelo vão para o ggml-rpc-server de cada
trabalhador (llama.cpp fixado, o mesmo pacote do motor). Todo o tráfego passa pelo túnel cifrado (sigma/tunnel.py),
que o TRABALHADOR disca: ninguém abre porta em casa, só o coordenador escuta.

Para a chain o grupo é uma máquina só, o coordenador: ele declara para IA a própria memória somada à que os
trabalhadores conectados emprestam, recebe os pedidos e os pagamentos. Os trabalhadores não servem IA sozinhos
(a memória deles não conta duas vezes). Nesta versão o coordenador escolhe os trabalhadores (o dono do grupo);
o sorteio pela chain, a conferência entre coordenadores e o rateio do pagamento vêm depois (docs/PENDENCIAS.md).

Vídeo (2.9): com `--group-kind video` o grupo divide o modelo de difusão (Wan) em vez do de texto. O coordenador
roda o sd-cli com `--rpc-servers` e `--backend diffusion=RPC0&RPC1&…&CPU` (os blocos do transformador espalhados
pela memória livre de cada um), e cada trabalhador roda o rpc-server compatível com o stable-diffusion.cpp
(compilado com GGML_MAX_NAME=160, no commit do llama.cpp de ggml/scripts/sync-llama.last). Os dois rpc-servers não
se misturam: um grupo é de texto ou de vídeo.

Configuração (linha de comando do app):
    coordenador: --coordinate 0.0.0.0:7710 --group-worker <endereço da máquina>=<GB emprestados> (um por trabalhador)
    trabalhador: --worker-of <endereço do coordenador>@<host>:<porta>
    vídeo: as duas pontas com --group-kind video
"""
import os
import re
import subprocess
import sys
import threading
import time

from . import engines, tunnel
from .crypto import norm_address

RPC_NAME = "ggml-rpc-server.exe" if os.name == "nt" else "ggml-rpc-server"
SD_RPC_NAME = "rpc-server.exe" if os.name == "nt" else "rpc-server"
KINDS = ("text", "video")
OUTPUT_ON_COORDINATOR = r"^output\.weight$=CPU"   # medido: sem isto, 607 KB de logits por token cruzam a rede
ADDR = re.compile(r"^(sig|0x)[0-9a-fA-F]{40}$")


class GroupError(Exception):
    pass


def parse_worker(value):
    """'<endereço>=<GB>' -> (endereço sig…, MB emprestados)."""
    addr, _, gb = str(value).partition("=")
    if not ADDR.match(addr.strip()):
        raise GroupError(f"trabalhador inválido: {value!r} (use <endereço da máquina>=<GB emprestados>)")
    try:
        mb = round(float(gb) * 1024)
    except ValueError:
        raise GroupError(f"memória inválida para o trabalhador {addr}: {gb!r}") from None
    if not 1024 <= mb <= 1_048_576:
        raise GroupError(f"memória do trabalhador {addr}: de 1 a 1024 GB")
    return norm_address(addr.strip()), mb


def parse_kind(value):
    """O que o grupo divide: 'text' (padrão) ou 'video'."""
    kind = str(value or "text").strip().lower()
    if kind not in KINDS:
        raise GroupError(f"tipo de grupo inválido: {value!r} (use text ou video)")
    return kind


def sd_rpc_exe(data_dir):
    """rpc-server compatível com o stable-diffusion.cpp: SIGMA_SD_RPC ou engine/sd-rpc/ na pasta de dados."""
    exe = os.environ.get("SIGMA_SD_RPC") or os.path.join(data_dir, "engine", "sd-rpc", SD_RPC_NAME)
    if not os.path.isfile(exe):
        raise GroupError(f"o rpc-server do vídeo não está em {exe} (veja docs/GRUPO-VIDEO.md)")
    return exe


def parse_coordinator(value):
    """'<endereço>@<host>:<porta>' -> (endereço sig…, 'host:porta')."""
    addr, _, endpoint = str(value).partition("@")
    if not ADDR.match(addr.strip()) or ":" not in endpoint:
        raise GroupError(f"coordenador inválido: {value!r} (use <endereço do coordenador>@<host>:<porta>)")
    return norm_address(addr.strip()), endpoint.strip()


def _machine_pub(client, addr):
    """Chave pública da máquina, publicada na chain pelo registro dela."""
    m = client.machine(addr)
    pub = (m or {}).get("pub")
    if not pub:
        raise GroupError(f"a máquina {addr} não está registrada na rede (sem chave pública)")
    return pub


class Coordinator:
    """Escuta os trabalhadores (Hub do túnel) e diz ao motor onde estão os rpc-servers deles."""
    role = "coordinator"

    def __init__(self, wallet, client, listen, workers, log=print, kind="text"):
        if not tunnel.AVAILABLE:
            raise GroupError(tunnel.UNAVAILABLE)
        self.wallet, self.client, self.listen, self.log = wallet, client, listen, log
        self.kind = parse_kind(kind)
        self.workers = dict(workers)          # endereço -> MB emprestados
        self.pubs = {}                        # endereço -> chave pública (da chain)
        self.hub = None

    def _peer(self, addr):
        try:
            return self.pubs.get(norm_address(addr))
        except ValueError:
            return None

    def start(self):
        for addr in self.workers:             # sem a chave da chain o trabalhador não entra
            try:
                self.pubs[addr] = _machine_pub(self.client, addr)
            except Exception as e:
                self.log(f"[grupo] trabalhador {addr}: {e}")
        self.hub = tunnel.Hub(self.wallet.priv, self.listen, self._peer, log=self.log)
        self.hub.start()
        what = "vídeo" if self.kind == "video" else "texto"
        self.log(f"[grupo] coordenador ({what}) escutando em {self.listen} para {len(self.workers)} trabalhador(es)")

    def stop(self):
        if self.hub:
            self.hub.stop()
            self.hub = None

    def connected(self):
        return [a for a in self.workers if self.hub and self.hub.bridge_port(a)]

    def rpc(self, kind=None):
        """Endereços --rpc dos trabalhadores conectados agora (as pontes locais do túnel). Com `kind`, só se o
        grupo for desse tipo (o motor de texto não usa os rpc-servers de um grupo de vídeo, nem o contrário)."""
        if kind and kind != self.kind:
            return []
        return [f"127.0.0.1:{self.hub.bridge_port(a)}" for a in self.connected()]

    def lent_mb(self):
        return sum(self.workers[a] for a in self.connected())

    def status(self):
        return {"role": self.role, "kind": self.kind, "listen": self.listen, "workers": [
            {"address": a, "lent_mb": mb, "connected": bool(self.hub and self.hub.bridge_port(a))}
            for a, mb in sorted(self.workers.items())]}


class Worker:
    """Empresta memória: roda um ggml-rpc-server em 127.0.0.1 e o liga ao coordenador pelo túnel."""
    role = "worker"

    def __init__(self, wallet, client, coordinator, endpoint, data_dir, threads=None, log=print, kind="text"):
        if not tunnel.AVAILABLE:
            raise GroupError(tunnel.UNAVAILABLE)
        self.wallet, self.client, self.log = wallet, client, log
        self.kind = parse_kind(kind)
        self.coordinator, self.endpoint, self.data_dir = coordinator, endpoint, data_dir
        self.threads = int(threads) if threads else max(1, engines.hardware.cpu_cores() // 2)
        self.proc = self.link = self.port = None
        self._stop = threading.Event()
        self._thread = None

    def _rpc_exe(self):
        if self.kind == "video":
            return sd_rpc_exe(self.data_dir)
        llama = engines._LlamaCpp(self.data_dir, self.log, self.threads)
        for variant in llama._candidates():
            exe = os.path.join(os.path.dirname(llama._install(variant)), RPC_NAME)
            if os.path.isfile(exe):
                return exe
        raise GroupError("o pacote do llama.cpp não trouxe o ggml-rpc-server")

    def _start_rpc(self):
        exe, self.port = self._rpc_exe(), engines.free_port()
        cache = os.path.join(self.data_dir, "engine", "rpc-cache")
        os.makedirs(cache, exist_ok=True)
        logf = open(os.path.join(self.data_dir, "engine", "rpc-server.log"), "ab")
        env = dict(os.environ, LLAMA_CACHE=cache)
        if sys.platform.startswith("linux"):
            env["LD_LIBRARY_PATH"] = os.path.dirname(exe) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        self.proc = subprocess.Popen([exe, "-H", "127.0.0.1", "-p", str(self.port), "-c", "-t", str(self.threads)],
                                     stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     cwd=os.path.dirname(exe), env=env,
                                     creationflags=engines.NO_WINDOW | engines.BELOW_NORMAL)
        logf.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.proc is None or self.proc.poll() is not None:
                    if self.link:                # rpc-server novo: o coordenador recarrega pelo túnel novo
                        self.link.stop()
                        self.link = None
                    self._start_rpc()
                if self.link is None:
                    pub = _machine_pub(self.client, self.coordinator)
                    self.link = tunnel.Link(self.wallet.priv, self.endpoint, pub, ("127.0.0.1", self.port),
                                            log=self.log)
                    self.link.start()
                    self.log(f"[grupo] emprestando memória para {self.coordinator} ({self.endpoint})")
            except Exception as e:
                self.log(f"[grupo] trabalhador: {e}; tentando de novo em 30 s")
                self._stop.wait(30)
                continue
            self._stop.wait(5)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sigma-grupo-trabalhador", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self.link:
            self.link.stop()
            self.link = None
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def lent_mb(self):
        return 0

    def rpc(self, kind=None):
        return []

    def status(self):
        link = self.link.status() if self.link else {}
        return {"role": self.role, "kind": self.kind, "coordinator": self.coordinator, "endpoint": self.endpoint,
                "rpc_running": bool(self.proc and self.proc.poll() is None), "connected": bool(link.get("connected"))}


def from_settings(cfg, wallet, client, data_dir, threads=None, log=print):
    """O grupo pedido nas configurações ({"listen", "workers"} ou {"coordinator", "endpoint"}), ou None."""
    if not cfg:
        return None
    if cfg.get("listen"):
        return Coordinator(wallet, client, cfg["listen"], cfg.get("workers") or {}, log, cfg.get("kind"))
    if cfg.get("coordinator"):
        return Worker(wallet, client, cfg["coordinator"], cfg["endpoint"], data_dir, threads, log, cfg.get("kind"))
    return None
