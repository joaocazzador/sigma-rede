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


class LayerFractionPlanner:
    """Planeja uma camada em frações sequenciais, com reatribuição simples em caso de falha.

    O padrão aqui é o que o usuário propôs para o desenho do grupo: cada camada deixa de ser um bloco inteiro
    e passa a ser uma fila de frações, cada uma com dependência da fração anterior. A máquina vence a fração
    que entrega primeiro; se ela trava, a fração volta à fila e outra máquina pode assumir.
    """

    def __init__(self, machines):
        self.machines = {str(k): int(v) for k, v in (machines or {}).items()}
        self.layers = {}

    def _frag(self, frag_id):
        layer, _, idx = str(frag_id).rpartition(":")
        layer_data = self._layer(layer)
        return layer, layer_data, layer_data["fragments"][int(idx)]

    def plan_layer(self, layer, total_units, fragment_size=1):
        total_units = int(total_units)
        fragment_size = int(fragment_size)
        if total_units <= 0:
            raise GroupError(f"camada {layer!r} sem unidades para dividir")
        if fragment_size <= 0:
            raise GroupError(f"fragment_size inválido para {layer!r}: {fragment_size!r}")
        count = max(1, (total_units + fragment_size - 1) // fragment_size)
        fragments = []
        for idx in range(count):
            start = idx * fragment_size
            end = min(total_units, start + fragment_size)
            fragments.append({
                "id": f"{layer}:{idx}",
                "layer": layer,
                "index": idx,
                "range": (start, end),
                "size": end - start,
                "state": "queued",
                "assigned_to": None,
                "attempts": 0,
            })
        self.layers[layer] = {"total_units": total_units, "fragment_size": fragment_size, "fragments": fragments}
        return fragments

    def _layer(self, layer):
        if layer not in self.layers:
            raise GroupError(f"camada {layer!r} não planejada")
        return self.layers[layer]

    def _prev_done(self, layer_data, idx):
        if idx <= 0:
            return True
        return layer_data["fragments"][idx - 1]["state"] == "done"

    def ready(self, layer):
        layer_data = self._layer(layer)
        out = []
        for frag in layer_data["fragments"]:
            if frag["state"] == "queued" and self._prev_done(layer_data, frag["index"]):
                out.append(frag["id"])
        return out

    def claim(self, frag_id, machine):
        _, _, frag = self._frag(frag_id)
        if frag["state"] == "done":
            return frag["id"]
        frag["assigned_to"] = str(machine)
        frag["state"] = "running"
        frag["attempts"] += 1
        frag["leased_at"] = time.time()
        return frag["id"]

    def leased(self, frag_id):
        _, _, frag = self._frag(frag_id)
        return frag.get("assigned_to")

    def release(self, frag_id):
        _, _, frag = self._frag(frag_id)
        assigned = frag.get("assigned_to")
        frag["assigned_to"] = None
        frag["state"] = "queued"
        frag.pop("leased_at", None)
        return frag["id"]

    def lease_expired(self, frag_id, timeout_seconds=30.0):
        _, _, frag = self._frag(frag_id)
        if frag.get("state") != "running":
            return False
        if "leased_at" not in frag:
            frag["state"] = "queued"
            frag["assigned_to"] = None
            return True
        expired = (time.time() - float(frag["leased_at"])) >= float(timeout_seconds)
        if expired:
            frag["state"] = "queued"
            frag["assigned_to"] = None
            frag.pop("leased_at", None)
        return expired

    def fail(self, frag_id):
        _, _, frag = self._frag(frag_id)
        frag["state"] = "queued"
        frag["assigned_to"] = None
        frag.pop("leased_at", None)
        return frag["id"]

    def reassign(self, frag_id, machine):
        _, _, frag = self._frag(frag_id)
        frag["assigned_to"] = str(machine)
        frag["state"] = "running"
        frag["leased_at"] = time.time()
        return frag["id"]

    def checkpoint(self, frag_id, done_units):
        _, _, frag = self._frag(frag_id)
        frag["size"] = max(frag["size"], int(done_units))
        if int(done_units) >= (frag["range"][1] - frag["range"][0]):
            frag["state"] = "done"
            frag["assigned_to"] = None
            frag.pop("leased_at", None)
        return frag["id"]

    def select_machine(self, frag_id):
        if not self.machines:
            raise GroupError("não há máquinas disponíveis para a fração")
        machine, _ = max(sorted(self.machines.items()), key=lambda kv: kv[1])
        return machine

    def next_ready(self, layer=None):
        if layer is None:
            for name in sorted(self.layers):
                ready = self.ready(name)
                if ready:
                    frag_id = ready[0]
                    return frag_id, self.select_machine(frag_id)
            return None
        ready = self.ready(layer)
        if not ready:
            return None
        return ready[0], self.select_machine(ready[0])

    def assign_next(self, layer=None):
        next_job = self.next_ready(layer)
        if next_job is None:
            return None
        frag_id, machine = next_job
        self.claim(frag_id, machine)
        return frag_id

    def status(self, layer):
        layer_data = self._layer(layer)
        done = sum(1 for frag in layer_data["fragments"] if frag["state"] == "done")
        return {"layer": layer, "done": done, "fragments": [
            {"id": frag["id"], "range": frag["range"], "state": frag["state"], "assigned_to": frag["assigned_to"],
             "attempts": frag["attempts"]}
            for frag in layer_data["fragments"]]}


def sd_rpc_exe(data_dir, log=print):
    """rpc-server compatível com o stable-diffusion.cpp: SIGMA_SD_RPC, ou o pacote do grupo de vídeo, que o app
    baixa sozinho na primeira vez (media.install_sd_rpc)."""
    exe = os.environ.get("SIGMA_SD_RPC")
    if not exe:
        from . import media
        try:
            exe = os.path.join(media.install_sd_rpc(data_dir, log), SD_RPC_NAME)
        except media.EngineError as e:
            raise GroupError(str(e)) from None
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
        self.planner = LayerFractionPlanner(self.workers)

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

    def plan_layer(self, layer, total_units, fragment_size=1):
        """Agenda uma camada como uma sequência de frações que podem ser redistribuídas em falha."""
        return self.planner.plan_layer(layer, total_units, fragment_size)

    def ready(self, layer):
        return self.planner.ready(layer)

    def claim(self, frag_id, machine):
        return self.planner.claim(frag_id, machine)

    def next_ready(self, layer=None):
        return self.planner.next_ready(layer)

    def assign_next(self, layer=None):
        return self.planner.assign_next(layer)

    def fail(self, frag_id):
        return self.planner.fail(frag_id)

    def leased(self, frag_id):
        return self.planner.leased(frag_id)

    def release(self, frag_id):
        return self.planner.release(frag_id)

    def lease_expired(self, frag_id, timeout_seconds=30.0):
        return self.planner.lease_expired(frag_id, timeout_seconds=timeout_seconds)

    def reassign(self, frag_id, machine):
        return self.planner.reassign(frag_id, machine)

    def checkpoint(self, frag_id, done_units):
        return self.planner.checkpoint(frag_id, done_units)

    def status(self):
        layers = {name: self.planner.status(name) for name in sorted(self.planner.layers)}
        return {"role": self.role, "kind": self.kind, "listen": self.listen, "workers": [
            {"address": a, "lent_mb": mb, "connected": bool(self.hub and self.hub.bridge_port(a))}
            for a, mb in sorted(self.workers.items())], "layers": layers}


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
            return sd_rpc_exe(self.data_dir, self.log)
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
