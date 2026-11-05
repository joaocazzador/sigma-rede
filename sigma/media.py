"""Motor de imagem e vídeo da máquina (regras v2): o stable-diffusion.cpp gera o que a rede pede.

- sdcpp: baixa sozinho o stable-diffusion.cpp (versão fixada, SHA-256 conferido) e roda um `sd-cli` por pedido,
         sem janela, com prioridade baixa e numa pasta temporária que sai logo depois. A versão Vulkan (placa de
         vídeo) só é tentada quando há placa dedicada; se ela não instalar ou não rodar, cai para a de CPU;
- mock:  imagens (PNG de uma cor só) e "vídeos" de mentira, determinísticos, para testar a rede sem modelo.

Um modelo de imagem/vídeo é um arquivo principal (o checkpoint inteiro, `-m`, ou só o modelo de difusão,
`--diffusion-model`, quando há arquivos extras) mais até 4 extras: VAE e codificadores de texto (`files` na
chain). Cada arquivo é conferido pelo SHA-256 e pela estrutura (safetensors ou GGUF) antes de o motor abrir.

O pedido chega como um dicionário (o `spec` que o cliente cifra para a máquina): prompt, formato e, para imagem,
quantas; para vídeo, quantos segundos. Os números que o motor recebe saem sempre de listas fechadas e limites
(clean_spec), e o texto vai sem caracteres de controle e sem < > (o sd-cli abre o arquivo de uma tag
<lora:caminho:peso> no prompt): o texto de outra pessoa nunca vira opção de linha de comando.
"""
import base64
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
import zlib

from . import engines, hardware
from .engines import BELOW_NORMAL, NO_WINDOW, EngineError, RefusedFile, extract, fetch

SD_BUILD = "master-1026-997478a"          # 29/10/2026
_SD_RELEASES = f"https://github.com/leejet/stable-diffusion.cpp/releases/download/{SD_BUILD}/"
SD_ASSETS = {                             # variante -> (arquivo, sha256)
    "win-cpu-x64": ("sd-master-997478a-bin-win-cpu-x64.zip",
                    "5b1e1f7d1f347c61b2977d72c8a8d55d8542221b165770a944ac9d49c22fe4f1"),
    "win-vulkan-x64": ("sd-master-997478a-bin-win-vulkan-x64.zip",
                       "79822f46a19d6072308f418c5a55ce21cf7713310f17e3419da5287ef7f89f31"),
    "macos-arm64": ("sd-master-997478a-bin-Darwin-macOS-26.6.2-arm64.zip",
                    "14c95eb7c5d4bf876ca248fd710c1eaae653bdd8f843a3c7a9627e43aee04902"),
    "linux-x64": ("sd-master-997478a-bin-Linux-Ubuntu-24.04-x86_64.zip",
                  "5db7358a67c73cb91e7dc49f13e82e3f175b4054735c06adcbafa883a06115c8"),
    "linux-vulkan": ("sd-master-997478a-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip",
                     "81426e24443d6b231d2922fea42b4b473e5637a282cb9d59a4ea5cba53b0a7ca"),
}
WEIGHT_EXTS = (".safetensors", ".gguf")
ROLE_FLAGS = {"vae": "--vae", "clip_l": "--clip_l", "clip_g": "--clip_g", "t5xxl": "--t5xxl", "llm": "--llm"}
IMAGE_SIZES = ("512x512", "768x768", "1024x1024", "768x1024", "1024x768")
VIDEO_SIZES = ("832x480", "480x832")
VIDEO_SECONDS = (2, 3, 5)
VIDEO_FPS = 16                            # o Wan gera 16 quadros por segundo (4k+1 quadros por vídeo)
SAMPLERS = ("euler", "euler_a", "heun", "dpm++2m", "lcm")
MAX_PROMPT = 2000
DEFAULTS = {"image": {"steps": 20, "cfg": 7.0, "sampler": "euler_a", "size": "512x512"},
            "video": {"steps": 20, "cfg": 6.0, "sampler": "euler", "size": "832x480", "flow_shift": 3.0}}
MAX_HEADER = 100 * 1024 * 1024            # cabeçalho de um safetensors (os de verdade têm alguns MB)
# Limites do GGUF de imagem/vídeo que mudam em relação ao texto, conferidos no pacote do sd-cli fixado
SD_MAX_NAME = 159                         # nome de tensor: o ggml do sd-cli vem com GGML_MAX_NAME=160
SD_DIMS = range(0, 6)                     # 0 (escalar) a 5: o sd.cpp junta na 4ª as dimensões a mais (o Wan tem 5)
# Sai do texto do pedido: caracteres de controle (o NUL faz o Popen recusar a linha de comando; quebra de linha e
# tab ficam), surrogates soltos (não viram UTF-8) e < > (o sd-cli abre o arquivo de uma tag <lora:caminho:peso>)
_UNSAFE_TEXT = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff<>]")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"


# --------------------------------------------------------------- arquivos
def weight_ext(url):
    """Extensão do arquivo pelo endereço (".safetensors" ou ".gguf"); None se não for um formato aceito."""
    path = str(url or "").split("?", 1)[0].split("#", 1)[0].lower()
    return next((e for e in WEIGHT_EXTS if path.endswith(e)), None)


def check_weights(path):
    """O arquivo é um safetensors ou um GGUF bem formado? RefusedFile("weights") se não for. Só lê o começo:
    o SHA-256 (que o host confere antes) garante que é o arquivo que o padrinho registrou; isto garante que o
    motor não vai abrir lixo. O GGUF passa pela mesma conferência do texto (check_gguf: contagens, tamanhos,
    tipos, dimensões e offsets), com os limites de nome e de dimensões do sd-cli (SD_MAX_NAME e SD_DIMS)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(8)
            if head[:4] == b"GGUF":
                f.seek(0)
                try:
                    engines._check_gguf(engines._GGUFReader(f, size), size, max_name=SD_MAX_NAME, dims=SD_DIMS)
                except engines._BadGGUF:
                    raise RefusedFile("GGUF malformado", "weights") from None
                return "gguf"
            if len(head) < 8:
                raise RefusedFile("arquivo pequeno demais para ser um modelo", "weights")
            n = struct.unpack("<Q", head)[0]
            if not 2 <= n <= min(MAX_HEADER, size - 8):
                raise RefusedFile("cabeçalho de safetensors inválido", "weights")
            header = json.loads(f.read(n))
    except RefusedFile:
        raise
    except (OSError, ValueError, UnicodeDecodeError, struct.error, RecursionError):   # [[[[… aninhado demais
        raise RefusedFile("o arquivo não é um safetensors nem um GGUF válido", "weights") from None
    if not isinstance(header, dict) or not header:
        raise RefusedFile("cabeçalho de safetensors vazio", "weights")
    end = 8 + n
    for name, t in header.items():
        if name == "__metadata__":
            continue
        offs = t.get("data_offsets") if isinstance(t, dict) else None
        if not (isinstance(offs, list) and len(offs) == 2 and all(isinstance(x, int) for x in offs)
                and 0 <= offs[0] <= offs[1] and end + offs[1] <= size):
            raise RefusedFile("tensor fora do arquivo no safetensors", "weights")
    return "safetensors"


# ----------------------------------------------------------------- pedido
def _num(v, lo, hi, default, kind=float):
    """`v` como número em [lo, hi]; o padrão se não for um número finito (texto, lista, NaN, ±Infinity, 1e999)."""
    try:
        x = kind(v)
    except (TypeError, ValueError, OverflowError):   # int(±inf) e float(10**400) dão OverflowError
        return default
    if x != x or x in (math.inf, -math.inf):   # NaN, ±Infinity
        return default
    return max(lo, min(hi, x))


def _text(v):
    """O texto do pedido como vai para o sd-cli: sem _UNSAFE_TEXT, sem espaço nas pontas e no máximo MAX_PROMPT."""
    return _UNSAFE_TEXT.sub("", v).strip()[:MAX_PROMPT] if isinstance(v, str) else ""


def clean_spec(task, spec, units):
    """O pedido como o motor vai rodar: tudo de listas fechadas ou dentro de limites. `units` é o que o job
    pagou (imagens ou segundos): o pedido nunca gera mais do que isso. ValueError se não der para entender."""
    if not isinstance(spec, dict):
        raise ValueError("pedido malformado")
    prompt = _text(spec.get("prompt"))
    if not prompt:   # vazio, não é texto ou só tinha caracteres que não vão para o motor
        raise ValueError("pedido sem descrição")
    d = DEFAULTS[task]
    sizes = IMAGE_SIZES if task == "image" else VIDEO_SIZES
    size = spec.get("size") if spec.get("size") in sizes else d["size"]
    w, h = (int(x) for x in size.split("x"))
    out = {"task": task, "prompt": prompt, "width": w, "height": h, "negative": _text(spec.get("negative")),
           "steps": _num(spec.get("steps"), 1, 50, d["steps"], int),
           "cfg": round(_num(spec.get("cfg"), 0.0, 20.0, d["cfg"]), 2),
           "sampler": spec.get("sampler") if spec.get("sampler") in SAMPLERS else d["sampler"],
           "seed": _num(spec.get("seed"), 0, 2 ** 31 - 1, int.from_bytes(os.urandom(4), "big") >> 1, int)}
    if task == "image":
        out["count"] = _num(spec.get("count"), 1, min(4, units), min(1, units), int)
    else:
        secs = _num(spec.get("seconds"), 1, units, units, int)
        out.update(seconds=secs, frames=VIDEO_FPS * secs + 1,
                   flow_shift=round(_num(spec.get("flow_shift"), 0.0, 20.0, d["flow_shift"]), 2))
    return out


def png_size(data):
    """(largura, altura) de um PNG, ou None."""
    if data[:8] != PNG_MAGIC or len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def solid_png(width, height, rgb):
    """PNG de uma cor só (o motor de teste), pequeno mesmo grande: a compressão some com as linhas iguais."""
    row = b"\0" + bytes(rgb) * width
    raw = zlib.compress(row * height, 9)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (PNG_MAGIC + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def media_variants(gpu_mb=None):
    """Variantes do stable-diffusion.cpp para este sistema, na ordem de preferência (vazio = sem versão pronta)."""
    import platform
    system, machine = platform.system(), platform.machine().lower()
    if gpu_mb is None:
        gpu_mb = hardware.gpu()[1]
    if system == "Windows" and machine in ("amd64", "x86_64"):
        return (["win-vulkan-x64"] if gpu_mb else []) + ["win-cpu-x64"]
    if system == "Linux" and machine in ("amd64", "x86_64"):
        return (["linux-vulkan"] if gpu_mb else []) + ["linux-x64"]
    if system == "Darwin":
        arm = machine == "arm64" or hardware._run(["sysctl", "-n", "hw.optional.arm64"]).strip() == "1"
        return ["macos-arm64"] if arm else []
    return []


def find_cli(folder):
    """Caminho do sd-cli dentro da pasta extraída (as versões antigas chamavam de sd)."""
    names = ("sd-cli.exe", "sd.exe") if os.name == "nt" else ("sd-cli", "sd")
    found = []
    for root, _, files in os.walk(folder):
        for i, n in enumerate(names):
            if n in files:
                found.append((i, root.count(os.sep), os.path.join(root, n)))
    if not found:
        raise EngineError("sd-cli não encontrado no pacote do stable-diffusion.cpp")
    exe = min(found)[2]
    if os.name != "nt":
        os.chmod(exe, os.stat(exe).st_mode | 0o755)
    return exe


# grupo de vídeo (docs/GRUPO-VIDEO.md): o sd-cli com RPC e o rpc-server compatível, compilados à parte
# (packaging/build_sd_rpc.ps1) e publicados no site; o app baixa sozinho e confere o SHA-256 fixado aqui
SD_RPC_URL = "https://sigma.195-35-29-232.sslip.io/downloads/sd-rpc-win-x64.zip"
SD_RPC_SHA = "889847699de5ff7d1b9082f5ae347f6d4480b2be8812a75c34c67e171fa448d7"
SD_RPC_SIZE = 16507415
_sd_rpc_lock = threading.Lock()


def sd_rpc_dir(data_dir):
    return os.path.join(data_dir, "engine", "sd-rpc")


def install_sd_rpc(data_dir, log=print):
    """Pasta com sd-cli.exe e rpc-server.exe do grupo de vídeo, baixando na primeira vez (só Windows x64)."""
    import platform
    folder = sd_rpc_dir(data_dir)
    ok = os.path.join(folder, ".ok")
    if os.path.isfile(ok):
        return folder
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise EngineError("o grupo de vídeo ainda só tem os programas para Windows de 64 bits")
    with _sd_rpc_lock:
        if os.path.isfile(ok):
            return folder
        archive = folder + ".zip"
        log("[grupo] baixando os programas do grupo de vídeo (16 MB)")
        try:
            os.makedirs(os.path.dirname(folder), exist_ok=True)
            fetch(SD_RPC_URL, archive, SD_RPC_SHA, size=SD_RPC_SIZE)
            tmp = folder + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            extract(archive, tmp)
            for name in ("sd-cli.exe", "rpc-server.exe"):
                if not os.path.isfile(os.path.join(tmp, name)):
                    raise EngineError(f"o pacote do grupo de vídeo veio sem {name}")
            shutil.rmtree(folder, ignore_errors=True)
            os.replace(tmp, folder)
            with open(ok, "w", encoding="utf-8") as f:
                f.write(SD_RPC_SHA)
            os.remove(archive)
        except (OSError, zipfile.BadZipFile) as e:
            raise EngineError(f"não consegui baixar os programas do grupo de vídeo: {e}") from None
        log("[grupo] programas do grupo de vídeo prontos")
        return folder


# progresso do sd-cli: a barra dos passos ("| 3/20 - 1.2s/it") e as fases que o registro anuncia
_STEP = re.compile(rb"\|\s*(\d+)/(\d+) - [\d.]+ ?(?:s/it|it/s)")
PREVIEW_MAX = 200_000        # bytes: prévia maior que isso não vai (só a porcentagem)


def read_progress(log_path, task):
    """{"stage", "pct", "step", "steps"} lido do registro do sd-cli até agora (melhor esforço)."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            log = f.read()
    except OSError:
        return {"stage": "carregando", "pct": 1}
    steps = _STEP.findall(log)
    if b"sampling completed" in log or b"generating latent video completed" in log:
        return {"stage": "finalizando", "pct": 95}
    if steps:
        a, b = int(steps[-1][0]), int(steps[-1][1])
        return {"stage": "desenhando" if task == "image" else "gerando o vídeo", "step": a, "steps": b,
                "pct": 10 + int(85 * a / max(1, b))}
    if b"get_learned_condition completed" in log or b"sampling using" in log:
        return {"stage": "começando a desenhar" if task == "image" else "começando o vídeo", "pct": 10}
    return {"stage": "carregando o modelo", "pct": 3}


# ------------------------------------------------------------------ motores
class _SdCpp:
    kind = "sdcpp"

    def __init__(self, data_dir, log, threads):
        self.root = os.path.join(data_dir, "engine", "sd-" + SD_BUILD)
        self.work = os.path.join(data_dir, "media-tmp")
        self.log, self.threads = log, threads
        self.override = os.environ.get("SIGMA_SD_CLI")   # sd-cli próprio (ex.: build com CUDA)
        self.variants = media_variants()
        self.failed = set()
        self.models = {}             # nome -> {"task", "files": {papel: caminho}}
        self.lock = threading.RLock()
        self.install_lock = threading.Lock()
        self.installing = None
        self.running = None          # nome do modelo gerando agora
        self.killer = engines._KillOnClose()
        self.rpc_provider = None     # grupo de vídeo: pontes do túnel para os rpc-servers dos trabalhadores
        self.data_dir = data_dir

    def _rpc(self):
        try:
            return list(self.rpc_provider()) if self.rpc_provider else []
        except Exception:
            return []

    def _group_cli(self):
        """sd-cli com RPC (SD_RPC=ON), que o pacote oficial não traz: SIGMA_SD_CLI_RPC ou engine/sd-rpc/."""
        exe = os.environ.get("SIGMA_SD_CLI_RPC")
        if not exe:
            exe = os.path.join(install_sd_rpc(self.data_dir, self.log), "sd-cli.exe")
        if not os.path.isfile(exe):
            raise EngineError(f"o sd-cli com RPC não está em {exe} (veja docs/GRUPO-VIDEO.md)")
        return exe

    @staticmethod
    def rpc_devices(exe, rpc, timeout=60):
        """Nomes dos dispositivos RPC que o sd-cli enxerga (RPC0, RPC1, …): um rpc-server pode dar mais de um."""
        try:
            out = subprocess.run([exe, "--rpc-servers", ",".join(rpc), "--list-devices"], capture_output=True,
                                 timeout=timeout, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            raise EngineError(f"o sd-cli não listou os trabalhadores do grupo: {e}") from None
        names = []
        for line in out.decode(errors="replace").splitlines():
            first = line.strip().split(maxsplit=1)
            if first and re.fullmatch(r"RPC\d+", first[0]):
                names.append(first[0])
        if not names:
            raise EngineError("nenhum trabalhador do grupo respondeu ao sd-cli")
        return names

    def group_args(self, exe, rpc):
        """Divide o modelo de difusão: blocos nos trabalhadores e no coordenador (CPU, por último); o
        codificador de texto e o VAE ficam aqui. Sem --offload-to-cpu, que guardaria todos os pesos na CPU."""
        devices = self.rpc_devices(exe, rpc)
        return ["--rpc-servers", ",".join(rpc), "--backend", "diffusion=" + "&".join(devices + ["CPU"]) +
                ",te=CPU,vae=CPU"]

    def _candidates(self):
        return ["próprio"] if self.override else [v for v in self.variants if v not in self.failed]

    def available(self):
        return bool(self._candidates())

    def _installed(self, variant):
        if variant == "próprio":
            return self.override if os.path.isfile(self.override) else None
        folder = os.path.join(self.root, variant)
        if os.path.isfile(os.path.join(folder, ".ok")):
            try:
                return find_cli(folder)
            except EngineError:
                return None
        return None

    def describe(self):
        if self.installing:
            v, frac = self.installing
            return {"kind": self.kind, "ready": False, "installing": True, "install_progress": round(frac, 4),
                    "detail": f"baixando o motor de imagem e vídeo (stable-diffusion.cpp, {v}): {frac:.0%}"}
        if not self.available():
            return {"kind": self.kind, "ready": False,
                    "detail": "não há versão do stable-diffusion.cpp pronta para este sistema"}
        ready = any(self._installed(v) for v in self._candidates())
        with self.lock:
            names, running = sorted(self.models), self.running
        if running:
            detail = f"stable-diffusion.cpp gerando com {running}"
        elif names:
            detail = f"stable-diffusion.cpp pronto: {', '.join(names)}"
        elif ready:
            detail = "stable-diffusion.cpp instalado; esperando a rede colocar um modelo de imagem ou vídeo aqui"
        else:
            detail = "o stable-diffusion.cpp é baixado sozinho quando a rede colocar o primeiro modelo de imagem aqui"
        return {"kind": self.kind, "ready": ready or bool(names), "detail": detail}

    def _install(self, variant):
        exe = self._installed(variant)
        if exe:
            return exe
        if variant not in SD_ASSETS:   # "próprio": o SIGMA_SD_CLI não é um arquivo (não há o que baixar)
            raise EngineError(f"SIGMA_SD_CLI não aponta para um arquivo: {self.override}")
        with self.install_lock:
            exe = self._installed(variant)
            if exe:
                return exe
            fname, sha = SD_ASSETS[variant]
            folder = os.path.join(self.root, variant)
            archive = os.path.join(self.root, fname)
            self.log(f"[motor] baixando o stable-diffusion.cpp {SD_BUILD} ({variant})")
            self.installing = (variant, 0.0)
            try:
                os.makedirs(self.root, exist_ok=True)
                fetch(_SD_RELEASES + fname, archive, sha, opener=engines._release_opener(),
                      progress=lambda done, total: setattr(self, "installing", (variant, done / total if total else 0)))
                tmp = folder + ".tmp"
                shutil.rmtree(tmp, ignore_errors=True)
                extract(archive, tmp)
                find_cli(tmp)
                shutil.rmtree(folder, ignore_errors=True)
                os.replace(tmp, folder)
                with open(os.path.join(folder, ".ok"), "w", encoding="utf-8") as f:
                    f.write(sha)
                os.remove(archive)
            except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
                raise EngineError(f"não consegui instalar o stable-diffusion.cpp: {e}") from None
            finally:
                self.installing = None
            self.log(f"[motor] stable-diffusion.cpp {SD_BUILD} ({variant}) pronto")
            return find_cli(folder)

    def load(self, name, task, files):
        """Nada fica carregado entre pedidos (cada pedido roda um sd-cli): aqui só instala o motor e guarda os
        caminhos já conferidos. Só a versão Vulkan sai da lista se não instalar; a de CPU (e o SIGMA_SD_CLI)
        fica: o host tenta de novo, cada vez mais devagar, e o download continua de onde parou."""
        for variant in self._candidates():
            try:
                self._install(variant)
                break
            except EngineError as e:
                self.log(f"[motor] {e}")
                if "vulkan" not in variant:   # uma queda de rede não desliga imagem e vídeo até reiniciar o app
                    raise
                self.failed.add(variant)      # a Vulkan não instalou: tenta a de CPU
        else:
            raise EngineError("não consegui instalar o stable-diffusion.cpp neste computador")
        with self.lock:
            self.models[name] = {"task": task, "files": dict(files)}

    def unload(self, name):
        with self.lock:
            self.models.pop(name, None)

    def loaded(self):
        with self.lock:
            return sorted(self.models)

    def _command(self, exe, m, spec, out, group=None):
        files = m["files"]
        main = ["--diffusion-model", files["main"]] if len(files) > 1 else ["-m", files["main"]]
        cmd = [exe, *main]
        for role, flag in ROLE_FLAGS.items():
            if role in files:
                cmd += [flag, files[role]]
        if spec["task"] == "video":
            cmd += ["-M", "vid_gen", "--video-frames", str(spec["frames"]), "--fps", str(VIDEO_FPS),
                    "--flow-shift", str(spec["flow_shift"]), "--vae-tiling"]
        cmd += ["-p", spec["prompt"], "-W", str(spec["width"]), "-H", str(spec["height"]),
                "--steps", str(spec["steps"]), "--cfg-scale", str(spec["cfg"]),
                "--sampling-method", spec["sampler"], "-s", str(spec["seed"]), "-t", str(self.threads)]
        cmd += group if group else ["--offload-to-cpu"]
        cmd += ["-o", out]
        if spec["negative"]:
            cmd += ["-n", spec["negative"]]
        if spec["task"] == "image" and spec["count"] > 1:
            cmd += ["-b", str(spec["count"])]
        return cmd

    def generate(self, name, spec, timeout=None, should_stop=None, on_progress=None):
        """-> [(bytes, mime, meta)]: as imagens (PNG) ou o vídeo (WebM) que o sd-cli gravou; toda falha sai como
        EngineError (o host responde o pedido). Se a versão Vulkan não rodar, o mesmo pedido vai para a de CPU no
        tempo que sobrou; se a de CPU gerar, o problema era a Vulkan, que sai da lista nesta execução."""
        with self.lock:
            m = self.models.get(name)
        if m is None:
            raise EngineError(f"o modelo '{name}' não está pronto nesta máquina")
        deadline = time.time() + (timeout or 3600)   # um prazo só: a tentativa na CPU usa o que sobrou
        rpc = self._rpc() if spec["task"] == "video" else []
        if rpc:   # grupo de vídeo: o modelo dividido entre os trabalhadores conectados (sem tentativa local)
            exe = self._group_cli()
            return self._run(name, exe, m, spec, deadline, should_stop, self.group_args(exe, rpc), on_progress)
        gpu, error = None, EngineError("o stable-diffusion.cpp não está instalado")
        for variant in self._candidates():
            try:
                results = self._run(name, self._install(variant), m, spec, deadline, should_stop,
                                    on_progress=on_progress)
            except EngineError as e:
                if "vulkan" not in variant or (should_stop and should_stop()) or deadline - time.time() < 60:
                    raise
                self.log(f"[motor] a versão Vulkan do sd-cli não rodou ({e}); usando a de CPU")
                gpu, error = variant, e
                continue
            if gpu:
                self.failed.add(gpu)
            return results
        raise error

    def _run(self, name, exe, m, spec, deadline, should_stop, group=None, on_progress=None):
        """Um sd-cli com prioridade baixa numa pasta temporária, que sai logo depois: nada do pedido fica no
        disco."""
        job_dir = os.path.join(self.work, hashlib.sha256(os.urandom(16)).hexdigest()[:16])
        out = os.path.join(job_dir, "video.webm" if spec["task"] == "video" else "imagem.png")
        cmd = self._command(exe, m, spec, out, group) if group else self._command(exe, m, spec, out)
        preview = os.path.join(job_dir, "previa.png")
        if on_progress and spec["task"] == "image":   # o desenho até agora, a cada passo (projeção barata do latente)
            cmd += ["--preview", "proj", "--preview-path", preview, "--preview-interval", "1"]
        nice = shutil.which("nice") if os.name != "nt" else None
        if nice:   # prioridade baixa desde o primeiro instante (no Windows vem pelo creationflags)
            cmd = [nice, "-n", "10", *cmd]
        env = dict(os.environ)
        if sys.platform.startswith("linux"):
            env["LD_LIBRARY_PATH"] = os.path.dirname(exe) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        logf = None
        try:
            os.makedirs(job_dir, exist_ok=True)
            logf = open(os.path.join(job_dir, "sd.log"), "wb")
            try:
                proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        cwd=job_dir, env=env, creationflags=NO_WINDOW | BELOW_NORMAL)
            except (OSError, ValueError) as e:   # ValueError: NUL num argumento (o clean_spec já tira)
                raise EngineError(f"não consegui iniciar o sd-cli: {e}") from None
            if os.name != "nt" and not nice:
                try:
                    os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
                except (OSError, AttributeError):
                    pass
            self.killer.add(proc)
            with self.lock:
                self.running = name
            seen, last_report = None, 0.0
            while proc.poll() is None:
                if on_progress and time.time() - last_report >= 1.0:
                    last_report = time.time()
                    info = read_progress(os.path.join(job_dir, "sd.log"), spec["task"])
                    try:
                        st = os.stat(preview)
                        if (st.st_mtime, st.st_size) != seen and 0 < st.st_size <= PREVIEW_MAX:
                            seen = (st.st_mtime, st.st_size)
                            with open(preview, "rb") as f:
                                data = f.read()
                            if data[:8] == b"\x89PNG\r\n\x1a\n":
                                info["preview"] = base64.b64encode(data).decode()
                    except OSError:
                        pass
                    try:
                        on_progress(info)
                    except Exception:
                        pass   # progresso é extra: nunca derruba a geração
                if (should_stop and should_stop()) or time.time() > deadline:
                    proc.kill()
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        pass   # o kill já foi pedido; o pedido volta como falha do mesmo jeito
                    raise EngineError("a geração passou do prazo e foi interrompida" if time.time() > deadline
                                      else "a geração foi interrompida: a máquina está desligando")
                time.sleep(0.5)
            logf.close()
            results = self._collect(job_dir, spec)
            if not results:
                raise EngineError(f"o sd-cli não gerou nada (código {proc.returncode}): {self._tail(job_dir)}")
            return results
        except OSError as e:   # a pasta do pedido, o registro ou o resultado que não abre
            raise EngineError(f"o sd-cli falhou: {e}") from None
        finally:
            with self.lock:
                self.running = None
            if logf:
                logf.close()
            shutil.rmtree(job_dir, ignore_errors=True)   # nada do pedido fica no disco

    @staticmethod
    def _tail(job_dir):
        try:
            with open(os.path.join(job_dir, "sd.log"), "rb") as f:
                lines = f.read()[-600:].decode(errors="replace").strip().splitlines()
            return " | ".join(lines[-3:])[:300] or "sem mensagem"
        except OSError:
            return "sem mensagem"

    @staticmethod
    def _collect(job_dir, spec):
        out = []
        names = sorted(os.listdir(job_dir))
        if spec["task"] == "image":
            for n in names:
                if n.lower().endswith(".png") and len(out) < spec["count"]:
                    with open(os.path.join(job_dir, n), "rb") as f:
                        data = f.read()
                    wh = png_size(data)
                    if wh:
                        out.append((data, "image/png", {"width": wh[0], "height": wh[1]}))
            return out
        for n in names:
            if n.lower().endswith(".webm"):
                with open(os.path.join(job_dir, n), "rb") as f:
                    data = f.read()
                if data[:4] == WEBM_MAGIC:
                    return [(data, "video/webm", {"width": spec["width"], "height": spec["height"],
                                                   "seconds": spec["seconds"]})]
        return []

    def shutdown(self):
        with self.lock:
            self.models.clear()


class _MockMedia:
    """Gerador de brinquedo: PNG de uma cor tirada do prompt e um "vídeo" que só tem o cabeçalho WebM. NÃO é
    uma IA de verdade: serve para exercitar pagamento por imagem/segundo, cifra e o painel."""
    kind = "mock"

    def __init__(self):
        self.models = {}

    def available(self):
        return True

    def describe(self):
        return {"kind": self.kind, "ready": True, "detail": "gerador de teste (não é um modelo de verdade)"}

    def load(self, name, task, files):
        for p in files.values():
            if not os.path.isfile(p):
                raise EngineError(f"arquivo do modelo não encontrado: {p}")
        self.models[name] = task

    def unload(self, name):
        self.models.pop(name, None)

    def loaded(self):
        return sorted(self.models)

    def generate(self, name, spec, timeout=None, should_stop=None, on_progress=None):
        if name not in self.models:
            raise EngineError(f"o modelo '{name}' não está pronto nesta máquina")
        seed = hashlib.sha256(f"{spec['prompt']}:{spec['seed']}".encode()).digest()
        if on_progress:
            for a in range(1, 3):
                on_progress({"stage": "desenhando", "step": a, "steps": 2, "pct": 10 + 85 * a // 2})
        if spec["task"] == "image":
            return [(solid_png(spec["width"] // 8, spec["height"] // 8, seed[3 * i:3 * i + 3]), "image/png",
                     {"width": spec["width"] // 8, "height": spec["height"] // 8}) for i in range(spec["count"])]
        data = WEBM_MAGIC + b"sigma-mock-video:" + base64.b16encode(seed[:8]) + b"\0" * 64
        return [(data, "video/webm", {"width": spec["width"], "height": spec["height"], "seconds": spec["seconds"]})]

    def shutdown(self):
        self.models.clear()


class MediaManager:
    """O motor de imagem e vídeo desta máquina. `prefer` "mock" nos testes; qualquer outro valor usa o
    stable-diffusion.cpp."""

    def __init__(self, data_dir, prefer="auto", log=print, threads=None):
        self.data_dir = data_dir
        self.prefer = "mock" if prefer == "mock" else "sdcpp"
        self.log = log
        self.threads = int(threads) if threads else max(1, hardware.cpu_cores() // 2)
        self._impl = None
        self._lock = threading.Lock()
        self.rpc_provider = None     # grupo de vídeo (sigma/grupo.py): função -> ["127.0.0.1:<ponte>", ...]

    def set_rpc_provider(self, provider):
        self.rpc_provider = provider
        with self._lock:
            if self._impl is not None:
                self._impl.rpc_provider = provider

    def _engine(self):
        with self._lock:
            if self._impl is None:
                self._impl = _MockMedia() if self.prefer == "mock" else _SdCpp(self.data_dir, self.log, self.threads)
                self._impl.rpc_provider = self.rpc_provider
            return self._impl

    @property
    def kind(self):
        return self._engine().kind

    def available(self):
        return self._engine().available()

    def describe(self):
        d = dict(self._engine().describe())
        d.setdefault("installing", False)
        d.setdefault("install_progress", 0.0)
        return d

    def load(self, name, task, files):
        """`files`: {"main": caminho, "vae": …, "llm": …}. Confere a estrutura de cada arquivo (check_weights:
        RefusedFile se não passar) antes de entregar ao motor."""
        for path in files.values():
            check_weights(path)
        self._engine().load(name, task, files)

    def unload(self, name):
        self._engine().unload(name)

    def loaded(self):
        return self._engine().loaded()

    def generate(self, name, spec, timeout=None, should_stop=None, on_progress=None):
        if on_progress is None:
            return self._engine().generate(name, spec, timeout, should_stop)
        return self._engine().generate(name, spec, timeout, should_stop, on_progress=on_progress)

    def shutdown(self):
        with self._lock:
            impl = self._impl
        if impl:
            impl.shutdown()
