"""Motores de IA da máquina: quem roda os modelos que a rede coloca neste computador.

- ollama:   se o Ollama (OLLAMA_MIN ou mais novo) já estiver instalado e respondendo em 127.0.0.1:11434,
            usa ele;
- llamacpp: senão, baixa sozinho o llama.cpp (versão fixada, SHA-256 conferido) e roda um llama-server
            por modelo, sem janela, com prioridade baixa, só em 127.0.0.1 e com uma chave secreta (uma página
            aberta no navegador não consegue usar o motor). A versão Vulkan (placa de vídeo) só é tentada
            quando há placa dedicada; se ela não subir, cai para a de CPU;
- mock:     respostas prontas e determinísticas, para testar a rede sem modelo de verdade.

O usuário não instala nem escolhe nada: a rede decide o modelo, o host baixa o GGUF e chama `load`.
O SHA-256 só garante que o arquivo é o que o padrinho registrou, não que ele seja inofensivo: por isso
`check_gguf` confere a estrutura do GGUF antes de qualquer motor abrir o arquivo.
"""
import hashlib
import http.client
import json
import os
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

from . import __version__, hardware, netsafe

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MIN = "0.6.0"            # Ollama mais antigo não hospeda modelos da rede (leitor de GGUF sem as correções)
CONTEXT = 4096
LLAMA_BUILD = "b11721"          # 18/10/2026
_RELEASES = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/"
LLAMA_ASSETS = {                # variante -> (arquivo, sha256)
    "win-cpu-x64": (f"llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip",
                    "19f5a91cfbd906fadfb36975a65a0c165c7db1a533d5db129dfa0a533dfdeaa1"),
    "win-vulkan-x64": (f"llama-{LLAMA_BUILD}-bin-win-vulkan-x64.zip",
                       "b2dab62833cc07aae4a1f28752874668b6a98d304a7f8ca6c1421a0d1e1a7075"),
    "macos-arm64": (f"llama-{LLAMA_BUILD}-bin-macos-arm64.tar.gz",
                    "2ace55512ee1ab4e2703f80c135c2a92d40d3367a5b3a98d21b103ffa7f0eb82"),
    "macos-x64": (f"llama-{LLAMA_BUILD}-bin-macos-x64.tar.gz",
                  "de3e409d44a9130f8e863975c13693f047b1c657180a94485464eb7cd5747579"),
    "linux-x64": (f"llama-{LLAMA_BUILD}-bin-ubuntu-x64.tar.gz",
                  "68c7a4dfb6a53e607b73c00c839bfc81c55141cd0a7697c0252e634190647c2f"),
    "linux-vulkan": (f"llama-{LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz",
                     "9b794ea6578bae49a1e871b7ec330a754ce1aa788846eb574f0a1227a75996d5"),
}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
BELOW_NORMAL = 0x00004000 if os.name == "nt" else 0   # BELOW_NORMAL_PRIORITY_CLASS: a IA dá a vez a quem usa o PC
LOAD_TIMEOUT = 600              # modelo grande em disco lento demora para subir
CHAT_TIMEOUT = 900
# modelos se revezam na memória (regras v5): um modelo parado há tanto tempo sai da RAM e volta no próximo pedido
IDLE_UNLOAD = 1800
# texto em fluxo: sem nenhum pedaço novo por tanto tempo, o motor travou; a máquina devolve falha e o cliente vai
# para a próxima na hora (em vez de esperar o prazo inteiro do pedido)
STALL_TIMEOUT = 120
FIRST_TOKEN_WAIT = 900   # s até a primeira palavra (carga do modelo e leitura da pergunta incluídas)
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1 nunca passa por proxy


class EngineError(Exception):
    pass


class RefusedFile(EngineError):
    """O arquivo não é o que o padrinho registrou (`reason` "sha": SHA-256 ou tamanho) ou não é um GGUF
    válido ("gguf"). O host anota e não baixa de novo por um tempo."""

    def __init__(self, msg, reason):
        super().__init__(msg)
        self.reason = reason


class ContextOverflow(EngineError):
    """O motor recusou o pedido porque a conversa passou do contexto do modelo (llama-server devolve HTTP 400
    "exceeds the available context size"; o Ollama pode cortar o texto calado e responder errado). O host
    mapeia isto para a falha "context", e não "engine": o cliente mostra "divida o texto" e para de tentar
    outras máquinas (todas rodam o mesmo modelo com o mesmo contexto)."""


CONTEXT_OVERFLOW_SIG = "exceeds the available context size"   # trecho do erro 400 do llama-server


def _pt(n):
    """Número no jeito brasileiro (1.234), para mensagens que o painel mostra."""
    return f"{n:,.0f}".replace(",", ".")


# ------------------------------------------------------------------ download
def fetch(url, dest, sha256, size=None, progress=None, should_stop=None, timeout=30, opener=None):
    """Baixa `url` em `dest` retomando de `dest + ".part"` (cabeçalho Range) e confere tamanho e SHA-256.

    Arquivo errado é apagado (nada corrompido fica no disco); queda de conexão deixa o .part para a próxima
    tentativa continuar de onde parou. `progress(feito, total)` é chamado a cada MB. `opener` (ex.:
    netsafe.safe_opener()) decide como conectar: o seguro confere o IP de cada conexão e de cada
    redirecionamento; sem ele vale o opener padrão do processo."""
    if not str(url).startswith(("https://", "http://")):
        raise EngineError("endereço de download inválido")
    sha256 = str(sha256).lower()
    part = dest + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if size is not None and have > size:
        os.remove(part)
        have = 0
    h = hashlib.sha256()
    if have:   # retomada: o hash começa pelo que já está no disco
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    if size is None or have < size:
        headers = {"User-Agent": f"sigma/{__version__}"}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            r = opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):   # redirect que não foi seguido: file://, ftp://, laço...
                raise EngineError("o download foi redirecionado para um endereço não permitido; recusado") from None
            if not (e.code == 416 and have):   # 416 com .part: o servidor diz que já está tudo aqui
                raise EngineError(f"o servidor recusou o download (HTTP {e.code})") from None
            r = None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            reason = getattr(e, "reason", e)
            if "rede interna" in str(reason):   # o opener seguro recusou o IP (de primeira ou num redirect)
                raise EngineError("a URL do modelo aponta para a rede interna; recusada") from None
            raise EngineError(f"não consegui baixar: {reason}") from None
        if r is not None:
            with r:
                resumed = have and r.status == 206 and \
                    r.headers.get("Content-Range", "").startswith(f"bytes {have}-")
                if have and not resumed:   # servidor ignorou o Range: recomeça do zero
                    have, h = 0, hashlib.sha256()
                length = r.headers.get("Content-Length")
                total = size or (have + int(length) if length and length.isdigit() else None)
                try:
                    with open(part, "ab" if have else "wb") as f:
                        while True:
                            if should_stop and should_stop():
                                raise EngineError("download interrompido")
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            have += len(chunk)
                            if size is not None and have > size:
                                raise ValueError("arquivo maior que o anunciado")
                            f.write(chunk)
                            h.update(chunk)
                            if progress:
                                progress(have, total)
                except ValueError as e:
                    os.remove(part)
                    raise RefusedFile(f"{e}: descartado", "sha") from None
                except (OSError, EOFError, http.client.HTTPException) as e:   # timeout, conexão caída, IncompleteRead
                    raise EngineError(f"a conexão caiu no meio do download ({e}); continua de onde parou "
                                      "na próxima tentativa") from None
                if size is None and total and have < total:   # o servidor fechou antes do fim
                    raise EngineError(f"download incompleto ({_pt(have)} de {_pt(total)} bytes); continua na "
                                      "próxima tentativa")
    if size is not None and have != size:
        raise EngineError(f"download incompleto ({_pt(have)} de {_pt(size)} bytes); continua na próxima tentativa")
    if h.hexdigest() != sha256:
        os.remove(part)
        raise RefusedFile("o SHA-256 do arquivo não confere: descartado", "sha")
    os.replace(part, dest)
    return dest


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _release_opener():
    """Download de endereço fixo no código (o llama.cpp no GitHub): HTTPS com os certificados que funcionam também
    no Python do python.org no macOS, e o proxy do sistema, se houver."""
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=netsafe.ssl_context()))


# --------------------------------------------------------------------- GGUF
# Limites do check_gguf: um GGUF de verdade fica muito abaixo deles; um arquivo feito para estourar o leitor do
# llama.cpp/Ollama (tamanhos e contagens absurdos no cabeçalho) não passa. Formato: magic, versão (u32),
# nº de tensores (u64), nº de metadados (u64), os metadados (chave, tipo, valor) e a tabela de tensores.
GGUF_MAX_TENSORS = 100_000
GGUF_MAX_KV = 100_000
GGUF_MAX_KEY = 256                 # bytes
GGUF_MAX_STRING = 1 << 20          # valor de texto: 1 MiB
GGUF_MAX_ARRAY_STRING = 64 << 10   # texto dentro de uma lista: 64 KiB
GGUF_MAX_ARRAY = 10_000_000
GGUF_MAX_ALIGN = 65536
GGUF_MAX_NAME = 63                 # nome de tensor: menos de 64 bytes (GGML_MAX_NAME)
GGUF_MAX_ELEMENTS = 2 ** 50
GGUF_MAX_TYPE = 64                 # os tipos do ggml (F32, F16, Q4_K...) ficam todos abaixo disso
GGUF_REFUSED = "arquivo recusado: não é um modelo GGUF válido"
_STRING, _ARRAY, _UINT32 = 8, 9, 4
_SCALARS = {t: struct.Struct(f) for t, f in {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f",
                                             7: "<B", 10: "<Q", 11: "<q", 12: "<d"}.items()}


class _BadGGUF(Exception):
    pass


class _GGUFReader:
    """Lê o GGUF em sequência sem nunca acreditar num tamanho maior do que o que sobra no arquivo."""

    def __init__(self, f, size):
        self.f, self.size, self.pos = f, size, 0

    def left(self):
        return self.size - self.pos

    def read(self, n):
        if n > self.left():
            raise _BadGGUF()
        data = self.f.read(n)
        if len(data) != n:
            raise _BadGGUF()
        self.pos += n
        return data

    def skip(self, n):
        if n > self.left():
            raise _BadGGUF()
        self.f.seek(n, 1)
        self.pos += n

    def scalar(self, vtype):
        s = _SCALARS[vtype]
        return s.unpack(self.read(s.size))[0]

    def u32(self):
        return self.scalar(4)

    def u64(self):
        return self.scalar(10)

    def string(self, limit):
        n = self.u64()
        if n > limit:
            raise _BadGGUF()
        return self.read(n)


def _check_gguf(r, size, max_name=GGUF_MAX_NAME, dims=range(1, 5)):
    """`max_name` (o maior nome de tensor) e `dims` (quantas dimensões um tensor pode ter): os limites do leitor
    que vai abrir o arquivo. O padrão é o do llama.cpp; o sd-cli aceita nomes mais longos e outras dimensões."""
    if r.read(4) != b"GGUF" or r.u32() not in (2, 3):
        raise _BadGGUF()
    n_tensors, n_kv = r.u64(), r.u64()
    if n_tensors > GGUF_MAX_TENSORS or n_kv > GGUF_MAX_KV:
        raise _BadGGUF()
    alignment = 32
    for _ in range(n_kv):
        key, vtype, value = r.string(GGUF_MAX_KEY), r.u32(), None
        if vtype == _ARRAY:   # lista: tipo dos itens, quantidade e os itens (lista de lista não vale)
            etype, count = r.u32(), r.u64()
            if count > GGUF_MAX_ARRAY or not (etype == _STRING or etype in _SCALARS):
                raise _BadGGUF()
            if etype == _STRING:
                if count * 8 > r.left():
                    raise _BadGGUF()
                for _ in range(count):
                    r.string(GGUF_MAX_ARRAY_STRING)
            else:
                r.skip(count * _SCALARS[etype].size)
        elif vtype == _STRING:
            value = r.string(GGUF_MAX_STRING)
        elif vtype in _SCALARS:
            value = r.scalar(vtype)
        else:
            raise _BadGGUF()
        if key == b"general.alignment":
            if vtype != _UINT32 or not 0 < value <= GGUF_MAX_ALIGN or value & (value - 1):
                raise _BadGGUF()
            alignment = value
    offsets = []
    for _ in range(n_tensors):
        r.string(max_name)
        n_dims = r.u32()
        if n_dims not in dims:
            raise _BadGGUF()
        elements = 1
        for _ in range(n_dims):
            dim = r.u64()
            if dim < 1:
                raise _BadGGUF()
            elements *= dim
        if elements > GGUF_MAX_ELEMENTS or r.u32() >= GGUF_MAX_TYPE:
            raise _BadGGUF()
        offset = r.u64()
        if offset % alignment:
            raise _BadGGUF()
        offsets.append(offset)
    data_start = -(-r.pos // alignment) * alignment   # os dados começam alinhados logo depois da tabela
    if any(data_start + off >= size for off in offsets):
        raise _BadGGUF()


def check_gguf(path):
    """Confere a estrutura do GGUF sem carregar o modelo (Python puro: só lê os metadados e a tabela de tensores).
    Fora das regras -> RefusedFile(reason="gguf"); arquivo que não abre -> EngineError."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            _check_gguf(_GGUFReader(f, size), size)
    except _BadGGUF:
        raise RefusedFile(GGUF_REFUSED, "gguf") from None
    except FileNotFoundError:
        raise EngineError(f"arquivo do modelo não encontrado: {path}") from None
    except OSError as e:
        raise EngineError(f"não consegui ler o arquivo do modelo: {e}") from None


def _gguf_text(text):
    data = text.encode()
    return struct.pack("<Q", len(data)) + data


def write_test_gguf(path, size):
    """Grava um GGUF mínimo e válido (magic, versão 3, 0 tensores, 1 metadado) completado com bytes aleatórios até
    `size` bytes: os modelos falsos dos testes, da demo e da rede local. Devolve o SHA-256 do arquivo."""
    head = (b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + _gguf_text("general.name") + struct.pack("<I", _STRING)
            + _gguf_text("sigma-teste"))
    size = int(size)
    if size < len(head):
        raise ValueError(f"o GGUF de teste precisa de pelo menos {len(head)} bytes")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    h = hashlib.sha256(head)
    with open(path, "wb") as f:
        f.write(head)
        left = size - len(head)
        while left:
            chunk = os.urandom(min(left, 1 << 20))
            f.write(chunk)
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- llama.cpp
def extract(archive, dest):
    """Extrai .zip ou .tar.gz em `dest` sem deixar nada escapar da pasta (caminhos absolutos ou '..')."""
    os.makedirs(dest, exist_ok=True)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)   # o zipfile já remove '..' e caminhos absolutos
        return
    with tarfile.open(archive, "r:*") as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(dest, filter="data")
            return
        root = os.path.realpath(dest)
        for m in t.getmembers():   # Python sem filtro: confere à mão
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (target == root or target.startswith(root + os.sep)) or m.isdev():
                raise EngineError(f"arquivo suspeito no pacote: {m.name}")
            if (m.issym() or m.islnk()) and not os.path.realpath(
                    os.path.join(os.path.dirname(target), m.linkname)).startswith(root + os.sep):
                raise EngineError(f"link suspeito no pacote: {m.name}")
        t.extractall(dest)


def find_server(folder):
    """Caminho do llama-server dentro da pasta extraída (o mais raso, se houver mais de um)."""
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    found = []
    for root, _, files in os.walk(folder):
        if name in files:
            found.append(os.path.join(root, name))
    if not found:
        raise EngineError("llama-server não encontrado no pacote do llama.cpp")
    exe = min(found, key=lambda p: (p.count(os.sep), p))
    if os.name != "nt":
        os.chmod(exe, os.stat(exe).st_mode | 0o755)
    return exe


def llama_variants(gpu_mb=None):
    """Variantes do llama.cpp para este sistema, na ordem de preferência (vazio = sem versão pronta)."""
    system, machine = platform.system(), platform.machine().lower()
    if gpu_mb is None:
        gpu_mb = hardware.gpu()[1]
    if system == "Windows" and machine in ("amd64", "x86_64"):
        return (["win-vulkan-x64"] if gpu_mb else []) + ["win-cpu-x64"]
    if system == "Linux" and machine in ("amd64", "x86_64"):
        return (["linux-vulkan"] if gpu_mb else []) + ["linux-x64"]
    if system == "Darwin":   # Python x64 rodando no Rosetta ainda ganha a versão nativa (Metal)
        arm = machine == "arm64" or hardware._run(["sysctl", "-n", "hw.optional.arm64"]).strip() == "1"
        return ["macos-arm64"] if arm else ["macos-x64"]
    return []


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method, url, body=None, timeout=CHAT_TIMEOUT, raw=None, headers=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {"Content-Type": "application/json"})
    with _LOCAL.open(req, timeout=timeout) as r:
        return r.read()


def _stream_lines(method, url, body, timeout, headers, stall=STALL_TIMEOUT):
    """POST com resposta em fluxo: devolve as linhas (bytes) conforme chegam. `stall` é quanto esperar cada
    pedaço; `timeout`, o prazo total. Passou de qualquer um: EngineError (o motor travou)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {"Content-Type": "application/json"})
    deadline = time.time() + timeout
    # a primeira palavra pode demorar (carregar o modelo, ler a pergunta num PC lento): espera até FIRST_TOKEN_WAIT;
    # depois disso, `stall` entre um pedaço e outro
    r = _LOCAL.open(req, timeout=min(FIRST_TOKEN_WAIT, timeout))
    first = True
    try:
        while True:
            if time.time() > deadline:
                raise EngineError("a resposta passou do prazo")
            try:
                line = r.readline()
            except (TimeoutError, OSError) as e:
                if "timed out" in str(e) or isinstance(e, TimeoutError):
                    raise EngineError(f"o motor parou de responder (nada novo em {stall} s)") from None
                raise
            if not line:
                return
            if first:
                first = False
                try:
                    r.fp.raw._sock.settimeout(min(stall, timeout))
                except AttributeError:
                    pass
            yield line
    finally:
        r.close()


def _http_error(e):
    try:
        return e.read().decode(errors="replace")[:200]
    except Exception:
        return str(e)


class _KillOnClose:
    """Windows: os llama-server morrem junto com o app, mesmo se ele for fechado à força (Job Object)."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("Io", ctypes.c_ulonglong * 6),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = Extended()
            info.Basic.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            h = k32.CreateJobObjectW(None, None)
            if h and k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle, self.k32 = h, k32
        except Exception:
            self.handle = None

    def add(self, proc):
        if self.handle:
            try:
                self.k32.AssignProcessToJobObject(self.handle, int(proc._handle))
            except Exception:
                pass


class _LlamaCpp:
    kind = "llamacpp"

    def __init__(self, data_dir, log, threads):
        self.root = os.path.join(data_dir, "engine", LLAMA_BUILD)
        self.log, self.threads = log, threads
        self.override = os.environ.get("SIGMA_LLAMA_SERVER")   # llama-server próprio (ex.: build com CUDA)
        self.variants = llama_variants()
        self.failed = set()          # variantes que não subiram nesta execução
        self.rpc_provider = None     # grupo (sigma/grupo.py): função -> ["127.0.0.1:<ponte>", ...]
        self.procs = {}              # nome -> {"proc", "port", "variant", "path", "logf"}: os que estão na RAM
        self.known = {}              # nome -> caminho: todos os que esta máquina hospeda (na RAM ou não)
        self.busy = {}               # nome -> pedidos em andamento (um modelo ocupado nunca sai da RAM)
        self.used = {}               # nome -> último uso (time.time())
        self.lock = threading.RLock()
        self._reaper = None
        self.install_lock = threading.Lock()
        self.installing = None       # (variante, fração) durante o download do motor
        self.error = None
        self.killer = _KillOnClose()
        # só quem tem a chave (este app) conversa com os llama-server: uma página aberta no navegador não usa o motor
        self.api_key = secrets.token_urlsafe(24)

    def _headers(self):
        return {"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key}

    def _candidates(self):
        if self.override:
            return ["próprio"]
        return [v for v in self.variants if v not in self.failed]

    def available(self):
        return bool(self._candidates())

    def _installed(self, variant):
        folder = os.path.join(self.root, variant)
        if os.path.isfile(os.path.join(folder, ".ok")):
            try:
                return find_server(folder)
            except EngineError:
                return None
        return None

    def describe(self):
        installing = self.installing
        if installing:
            v, frac = installing
            return {"kind": self.kind, "ready": False, "installing": True, "install_progress": round(frac, 4),
                    "detail": f"baixando o motor de IA (llama.cpp {LLAMA_BUILD}, {v}): {frac:.0%}"}
        if not self.available():
            detail = self.error or "não há versão do llama.cpp pronta para este sistema; instale o Ollama"
            return {"kind": self.kind, "ready": False, "detail": detail}
        with self.lock:
            running = sorted(n for n, p in self.procs.items() if p["proc"].poll() is None)
            variants = sorted({p["variant"] for p in self.procs.values()})
        ready = bool(self.override) or any(self._installed(v) for v in self._candidates())
        if running:
            detail = f"llama.cpp {LLAMA_BUILD} ({', '.join(variants)}) rodando: {', '.join(running)}"
        elif ready:
            detail = f"llama.cpp {LLAMA_BUILD} instalado; esperando a rede colocar um modelo aqui"
        else:
            detail = "o llama.cpp é baixado sozinho quando a rede colocar o primeiro modelo neste computador"
        return {"kind": self.kind, "ready": ready, "detail": detail}

    def _install(self, variant):
        exe = self._installed(variant)
        if exe:
            return exe
        with self.install_lock:   # dois modelos ao mesmo tempo não baixam o motor duas vezes
            exe = self._installed(variant)
            if exe:
                return exe
            fname, sha = LLAMA_ASSETS[variant]
            folder = os.path.join(self.root, variant)
            archive = os.path.join(self.root, fname)
            self.log(f"[motor] baixando o llama.cpp {LLAMA_BUILD} ({variant})")
            self.installing = (variant, 0.0)
            try:
                fetch(_RELEASES + fname, archive, sha, opener=_release_opener(),
                      progress=lambda done, total: setattr(self, "installing", (variant, done / total if total else 0)))
                tmp = folder + ".tmp"
                shutil.rmtree(tmp, ignore_errors=True)
                extract(archive, tmp)
                find_server(tmp)
                shutil.rmtree(folder, ignore_errors=True)
                os.replace(tmp, folder)
                with open(os.path.join(folder, ".ok"), "w", encoding="utf-8") as f:
                    f.write(sha)
                os.remove(archive)
            except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
                raise EngineError(f"não consegui instalar o llama.cpp: {e}") from None
            finally:
                self.installing = None
            self.log(f"[motor] llama.cpp {LLAMA_BUILD} ({variant}) pronto")
            return find_server(folder)

    def _rpc(self):
        """Grupo (sigma/grupo.py): os rpc-servers dos trabalhadores conectados, pelas pontes do túnel."""
        try:
            return list(self.rpc_provider()) if self.rpc_provider else []
        except Exception:
            return []

    def _command(self, exe, variant, path, port, rpc=()):
        cmd = [exe, "-m", path, "--host", "127.0.0.1", "--port", str(port), "-c", str(CONTEXT), "--jinja",
               "-t", str(self.threads), "-np", "1"]
        if rpc:   # as camadas vão para os trabalhadores; a saída (logits) fica aqui
            cmd += ["--rpc", ",".join(rpc), "-ot", r"^output\.weight$=CPU"]
        if "vulkan" in variant:   # cabe inteiro na placa: todas as camadas na GPU
            _, vram = hardware.gpu()
            if os.path.getsize(path) * 1.2 / 1e6 + 500 <= vram:
                cmd += ["-ngl", "999"]
        return cmd

    def _start(self, name, exe, variant, path):
        port, rpc = free_port(), self._rpc()
        cmd = self._command(exe, variant, path, port, rpc)
        nice = shutil.which("nice") if os.name != "nt" else None
        if nice:   # prioridade baixa desde o primeiro instante (no Windows vem pelo creationflags)
            cmd = [nice, "-n", "10", *cmd]
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
        logf = open(self._log_path(name), "ab")
        env = dict(os.environ, LLAMA_API_KEY=self.api_key)   # a chave vai pelo ambiente, nunca na linha de comando
        if sys.platform.startswith("linux"):   # as .so vêm na mesma pasta do executável
            env["LD_LIBRARY_PATH"] = os.path.dirname(exe) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        try:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=os.path.dirname(exe) or None, env=env,
                                    creationflags=NO_WINDOW | BELOW_NORMAL)
        except OSError as e:
            logf.close()
            raise EngineError(f"não consegui iniciar o llama-server: {e}") from None
        if os.name != "nt" and not nice:
            try:
                os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
            except (OSError, AttributeError):
                pass
        self.killer.add(proc)
        deadline = time.time() + LOAD_TIMEOUT
        while True:
            if proc.poll() is not None:
                logf.close()
                raise EngineError(f"o llama-server fechou ao carregar o modelo: {self._tail(name)}")
            try:
                health = _http("GET", f"http://127.0.0.1:{port}/health", timeout=3, headers=self._headers())
                if json.loads(health).get("status") == "ok":
                    break
            except (urllib.error.URLError, OSError, ValueError):
                pass   # ainda carregando (503) ou subindo
            if time.time() > deadline:
                self._kill(proc)
                logf.close()
                raise EngineError("o llama-server não ficou pronto a tempo")
            time.sleep(0.5)
        return {"proc": proc, "port": port, "variant": variant, "path": path, "logf": logf, "rpc": rpc}

    def _log_path(self, name):
        return os.path.join(self.root, "logs", re.sub(r"[^\w.-]", "_", name) + ".log")

    def _tail(self, name):
        try:
            with open(self._log_path(name), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 600))
                lines = f.read().decode(errors="replace").strip().splitlines()
            return " | ".join(lines[-3:])[:400] or "sem mensagem"
        except OSError:
            return "sem mensagem"

    def load(self, name, path, sha256):
        """Confere que o modelo sobe (carrega uma vez) e o registra. Depois ele sai da RAM quando outro modelo
        precisar do espaço ou ficar IDLE_UNLOAD parado, e volta sozinho no próximo pedido (chat)."""
        with self.lock:
            p = self.procs.get(name)
            if p and p["proc"].poll() is None and p["path"] == path and p.get("rpc", []) == self._rpc():
                self.known[name] = path
                return
        if not os.path.isfile(path):
            raise EngineError(f"arquivo do modelo não encontrado: {path}")
        self._stop_proc(name)
        self._make_room(name)
        self._spawn(name, path)
        with self.lock:
            self.known[name] = path
            self.used[name] = time.time()
        self._start_reaper()

    def _make_room(self, keep):
        """Tira da RAM os outros modelos parados: a rede conta só o maior modelo da máquina (v5), então cabe um
        de cada vez. Os que estão respondendo agora ficam."""
        with self.lock:
            idle = [n for n in self.procs if n != keep and not self.busy.get(n)]
        for n in idle:
            self._stop_proc(n)
            self.log(f"[motor] '{n}' saiu da memória para dar lugar a " + (f"'{keep}'" if keep else "imagem/vídeo"))

    def free_memory(self):
        """Antes de gerar imagem ou vídeo: tira da RAM os modelos de texto parados."""
        self._make_room(None)

    def _start_reaper(self):
        if self._reaper and self._reaper.is_alive():
            return
        self._reaper = threading.Thread(target=self._reap, name="sigma-llama-ocioso", daemon=True)
        self._reaper.start()

    def _reap(self):
        while True:
            time.sleep(30)
            now = time.time()
            with self.lock:
                if not self.known and not self.procs:
                    return
                idle = [n for n in self.procs if not self.busy.get(n) and now - self.used.get(n, now) > IDLE_UNLOAD]
            for n in idle:
                self._stop_proc(n)
                self.log(f"[motor] '{n}' parado há mais de {IDLE_UNLOAD // 60} min: saiu da memória até o "
                         "próximo pedido")

    def _spawn(self, name, path):
        errors = []
        for variant in self._candidates():
            try:
                exe = self.override or self._install(variant)
                started = self._start(name, exe, variant, path)
            except EngineError as e:
                errors.append(f"{variant}: {e}")
                if "vulkan" in variant:   # placa de vídeo não deu: tenta a versão de CPU
                    self.failed.add(variant)
                    self.log(f"[motor] a versão Vulkan não subiu ({e}); usando a de CPU")
                    continue
                break
            with self.lock:
                self.procs[name] = started
            self.error = None
            self.log(f"[motor] modelo '{name}' carregado no llama.cpp ({variant}, porta {started['port']})")
            return
        self.error = "; ".join(errors) or "nenhuma versão do llama.cpp disponível"
        raise EngineError(self.error)

    @staticmethod
    def _kill(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)

    def _stop_proc(self, name):
        with self.lock:
            p = self.procs.pop(name, None)
        if p:
            self._kill(p["proc"])
            p["logf"].close()

    def unload(self, name):
        with self.lock:
            self.known.pop(name, None)
            self.used.pop(name, None)
        self._stop_proc(name)

    def loaded(self):
        """Os modelos que esta máquina hospeda (na RAM agora ou esperando o próximo pedido)."""
        with self.lock:
            return sorted(self.known)

    def resident(self):
        with self.lock:
            return sorted(self.procs)

    def _ready(self, name):
        """O llama-server do modelo no ar: sobe de novo se saiu da RAM (ou caiu), abrindo espaço antes."""
        with self.lock:
            path = self.known.get(name)
            p = self.procs.get(name)
        if path is None:
            raise EngineError(f"o modelo '{name}' não está carregado")
        if p is not None and p["proc"].poll() is None:
            return p
        if p is not None:
            self.log(f"[motor] o llama-server de '{name}' tinha parado; reiniciando")
            self._stop_proc(name)
        self._make_room(name)
        self._spawn(name, path)
        with self.lock:
            return self.procs[name]

    def chat(self, name, messages, max_tokens, json_mode=False, timeout=None, on_text=None):
        with self.lock:
            self.busy[name] = self.busy.get(name, 0) + 1
            self.used[name] = time.time()
        try:
            if on_text:
                return self._chat_stream(name, messages, max_tokens, json_mode, timeout, on_text)
            return self._chat(name, messages, max_tokens, json_mode, timeout)
        finally:
            with self.lock:
                self.busy[name] -= 1
                self.used[name] = time.time()

    def _chat_stream(self, name, messages, max_tokens, json_mode, timeout, on_text):
        """Como _chat, mas chama on_text(texto até agora) a cada pedaço que o llama-server solta."""
        p = self._ready(name)
        body = {"messages": messages, "max_tokens": max_tokens, "stream": True,
                "stream_options": {"include_usage": True}}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        parts, tin, tout, chunks = [], 0, 0, 0
        try:
            for line in _stream_lines("POST", f"http://127.0.0.1:{p['port']}/v1/chat/completions", body,
                                      timeout or CHAT_TIMEOUT, self._headers()):
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except ValueError:
                    continue
                usage = ev.get("usage") or {}
                if usage:
                    tin = int(usage.get("prompt_tokens") or tin)
                    tout = int(usage.get("completion_tokens") or tout)
                timings = ev.get("timings") or {}
                if timings and not usage:
                    tin = int(timings.get("prompt_n") or tin)
                    tout = int(timings.get("predicted_n") or tout)
                for ch in ev.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
                        chunks += 1
                        on_text("".join(parts))
        except urllib.error.HTTPError as e:
            detail = _http_error(e)
            if e.code == 400 and CONTEXT_OVERFLOW_SIG in detail.lower():
                raise ContextOverflow(f"o llama-server recusou ({e.code}): {detail}") from None
            raise EngineError(f"o llama-server recusou ({e.code}): {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise EngineError(f"o llama-server não respondeu: {e}") from None
        return "".join(parts), tin, tout or chunks

    def _chat(self, name, messages, max_tokens, json_mode=False, timeout=None):
        p = self._ready(name)
        body = {"messages": messages, "max_tokens": max_tokens, "stream": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            out = json.loads(_http("POST", f"http://127.0.0.1:{p['port']}/v1/chat/completions", body,
                                   timeout=timeout or CHAT_TIMEOUT, headers=self._headers()))
        except urllib.error.HTTPError as e:
            detail = _http_error(e)
            if e.code == 400 and CONTEXT_OVERFLOW_SIG in detail.lower():   # a conversa não cabe no contexto
                raise ContextOverflow(f"o llama-server recusou ({e.code}): {detail}") from None
            raise EngineError(f"o llama-server recusou ({e.code}): {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise EngineError(f"o llama-server não respondeu: {e}") from None
        usage = out.get("usage") or {}
        text = (out.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return text, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)

    def shutdown(self):
        for name in self.loaded():
            self.unload(name)
        for name in self.resident():
            self._stop_proc(name)


def _version(v):
    m = re.match(r"\D*(\d+)\.(\d+)(?:\.(\d+))?", str(v))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def ollama_version_ok(v):
    """Ollama `v` (ex.: "0.6.2") é pelo menos OLLAMA_MIN? Versão que não dá para ler não passa."""
    got = _version(v)
    return got is not None and got >= _version(OLLAMA_MIN)


class _Ollama:
    kind = "ollama"

    def __init__(self, log):
        self.log = log
        self.models = {}   # nome na rede -> sha256
        self._old = None   # versão antiga já avisada no registro

    @staticmethod
    def tag(name):
        return f"sigma-{name}"

    def version(self):
        return json.loads(_http("GET", f"{OLLAMA_URL}/api/version", timeout=1.5)).get("version", "?")

    def available(self):
        try:
            v = self.version()
        except (urllib.error.URLError, OSError, ValueError):
            return False
        if ollama_version_ok(v):
            return True
        if self._old != v:   # avisa uma vez por versão
            self._old = v
            self.log(f"[motor] o Ollama {v} é antigo demais (precisa do {OLLAMA_MIN} ou mais novo): "
                     "atualize o Ollama para hospedar modelos da rede")
        return False

    def describe(self):
        try:
            v = self.version()
        except (urllib.error.URLError, OSError, ValueError):
            return {"kind": self.kind, "ready": False, "detail": f"o Ollama não responde em {OLLAMA_URL}"}
        if not ollama_version_ok(v):
            return {"kind": self.kind, "ready": False,
                    "detail": f"Ollama {v}: atualize o Ollama para hospedar modelos da rede (mínimo {OLLAMA_MIN})"}
        names = ", ".join(sorted(self.models)) or "nenhum modelo da rede ainda"
        return {"kind": self.kind, "ready": True, "detail": f"Ollama {v} em 127.0.0.1:11434 · {names}"}

    def _call(self, method, path, body=None, timeout=900):
        try:
            return _http(method, OLLAMA_URL + path, body, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise EngineError(f"o Ollama recusou ({e.code}): {_http_error(e)}") from None
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"o Ollama não respondeu: {getattr(e, 'reason', e)}") from None

    def _has_blob(self, digest):
        try:
            _http("HEAD", f"{OLLAMA_URL}/api/blobs/{digest}", timeout=10)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise EngineError(f"o Ollama recusou ({e.code})") from None
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"o Ollama não respondeu: {getattr(e, 'reason', e)}") from None

    def _push_blob(self, digest, path):
        size = os.path.getsize(path)
        try:
            with open(path, "rb") as f:   # o Ollama confere o sha256 do que recebe
                _http("POST", f"{OLLAMA_URL}/api/blobs/{digest}", raw=f, timeout=3600,
                      headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"})
        except urllib.error.HTTPError as e:
            raise EngineError(f"o Ollama recusou o arquivo ({e.code}): {_http_error(e)}") from None
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"o Ollama não recebeu o arquivo: {getattr(e, 'reason', e)}") from None

    def load(self, name, path, sha256):
        digest = "sha256:" + sha256.lower()
        if not self._has_blob(digest):
            if not os.path.isfile(path):
                raise EngineError(f"arquivo do modelo não encontrado: {path}")
            self.log(f"[motor] entregando '{name}' ao Ollama ({_pt(os.path.getsize(path) / 1e6)} MB)")
            self._push_blob(digest, path)
        self._call("POST", "/api/create", {"model": self.tag(name), "files": {"model.gguf": digest},
                                           "stream": False}, timeout=1800)
        # já carrega na memória: confere que o Ollama consegue mesmo rodar esse GGUF antes de anunciar
        self._call("POST", "/api/generate", {"model": self.tag(name), "prompt": "", "stream": False,
                                             "options": {"num_ctx": CONTEXT}}, timeout=LOAD_TIMEOUT)
        self.models[name] = sha256
        self.log(f"[motor] modelo '{name}' pronto no Ollama como {self.tag(name)}")

    def unload(self, name):
        self.models.pop(name, None)
        try:   # tira da memória antes: depois de apagado o Ollama não aceita mais o nome e a RAM fica presa
            self._call("POST", "/api/generate", {"model": self.tag(name), "keep_alive": 0}, timeout=60)
        except EngineError:
            pass   # não estava carregado (ou nem existe): segue para apagar
        try:
            self._call("DELETE", "/api/delete", {"model": self.tag(name)}, timeout=60)
        except EngineError as e:
            if "404" not in str(e):
                raise

    def loaded(self):
        return sorted(self.models)

    def free_memory(self):
        """O Ollama já tira da RAM o modelo parado depois de uns minutos e carrega no pedido seguinte; aqui só
        pede para tirar agora (antes de imagem ou vídeo). Um modelo respondendo termina antes de sair."""
        for name in list(self.models):
            try:
                self._call("POST", "/api/generate", {"model": self.tag(name), "keep_alive": 0}, timeout=30)
            except EngineError:
                pass

    def chat(self, name, messages, max_tokens, json_mode=False, timeout=None, on_text=None):
        body = {"model": self.tag(name), "messages": messages, "stream": bool(on_text), "keep_alive": "30m",
                "options": {"num_predict": max_tokens, "num_ctx": CONTEXT}}
        if json_mode:
            body["format"] = "json"
        if on_text:
            out = self._chat_stream(body, timeout, on_text)
        else:
            try:
                out = json.loads(self._call("POST", "/api/chat", body, timeout=timeout or CHAT_TIMEOUT))
            except ValueError:
                raise EngineError("resposta ilegível do Ollama") from None
        prompt = int(out.get("prompt_eval_count") or 0)
        # o Ollama não recusa um pedido grande: ele corta calado o que não cabe em num_ctx e responde sobre um
        # texto incompleto. Se a entrada já encheu o contexto, a resposta seria errada: trata como estouro.
        if prompt >= CONTEXT:
            raise ContextOverflow(f"o Ollama cortou o pedido: {prompt} tokens não cabem no contexto de {CONTEXT}")
        return out.get("message", {}).get("content") or "", prompt, int(out.get("eval_count") or 0)

    def _chat_stream(self, body, timeout, on_text):
        """O /api/chat em fluxo (uma linha JSON por pedaço): junta o texto e devolve como a resposta inteira."""
        parts, last = [], {}
        try:
            for line in _stream_lines("POST", OLLAMA_URL + "/api/chat", body, timeout or CHAT_TIMEOUT, None):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    raise EngineError(f"o Ollama recusou: {str(ev['error'])[:200]}")
                piece = (ev.get("message") or {}).get("content")
                if piece:
                    parts.append(piece)
                    on_text("".join(parts))
                if ev.get("done"):
                    last = ev
                    break
        except urllib.error.HTTPError as e:
            raise EngineError(f"o Ollama recusou ({e.code}): {_http_error(e)}") from None
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"o Ollama não respondeu: {getattr(e, 'reason', e)}") from None
        return dict(last, message={"content": "".join(parts)})

    def shutdown(self):
        pass   # o Ollama é do usuário: os modelos ficam nele até a rede tirar


_TEMPLATES = {
    "fibonacci": 'def fibonacci(n):\n    a, b = 0, 1\n    for _ in range(n):\n        yield a\n'
                 '        a, b = b, a + b\n\n\nif __name__ == "__main__":\n'
                 '    print("Fibonacci:", list(fibonacci(15)))\n',
    ".py": 'def main():\n    print("Olá da rede Sigma!")\n\n\nif __name__ == "__main__":\n    main()\n',
    ".html": '<!doctype html>\n<html lang="pt-br">\n<head><meta charset="utf-8"><title>Sigma</title></head>\n'
             '<body><h1>Olá da rede Sigma</h1></body>\n</html>\n',
}


class _Mock:
    """Modelo de brinquedo determinístico. NÃO é uma IA de verdade: serve para exercitar
    pagamento, escrow, atribuição de modelos e o loop de ferramentas do Sigma Code."""
    kind = "mock"

    def __init__(self):
        self.models = {}

    def available(self):
        return True

    def describe(self):
        return {"kind": self.kind, "ready": True,
                "detail": "IA de teste (respostas prontas, não é um modelo de verdade)"}

    def load(self, name, path, sha256):
        if not os.path.isfile(path):
            raise EngineError(f"arquivo do modelo não encontrado: {path}")
        self.models[name] = sha256

    def unload(self, name):
        self.models.pop(name, None)

    def loaded(self):
        return sorted(self.models)

    def chat(self, name, messages, max_tokens, json_mode=False, timeout=None, on_text=None):
        text = self._reply(name, messages)
        if on_text:
            words = text.split(" ")
            for i in range(1, len(words) + 1):
                on_text(" ".join(words[:i]))
        tin = sum(len(str(m.get("content", "")).split()) for m in messages)
        return text, tin, len(text.split())

    @staticmethod
    def _reply(name, messages):
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        users = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
        if "SIGMA_CODE_PROTOCOL" not in system:
            last = users[-1] if users else ""
            return (f"[sigma-mock:{name}] Recebi sua mensagem ({len(last.split())} palavras). Eu sou um modelo "
                    f"de teste da rede Sigma, não uma IA de verdade. Você disse: \"{last[:200]}\"")
        task = users[0] if users else ""
        steps = [u for u in users[1:] if u.startswith("TOOL_RESULT")]
        m = re.search(r"([\w\-/]+\.(?:py|html|js|md|txt|css))", task)
        if not m:
            return json.dumps({"final": "[sigma-mock] Sou um modelo de teste: só sei criar um arquivo "
                                        "citado pelo nome (ex.: 'crie hello.py')."})
        path = m.group(1)
        if not steps:
            ext = os.path.splitext(path)[1]
            content = _TEMPLATES["fibonacci"] if "fibonacci" in task.lower() and ext == ".py" \
                else _TEMPLATES.get(ext, f"Arquivo criado pelo sigma-mock para: {task}\n")
            return json.dumps({"tool": "write_file", "args": {"path": path, "content": content}})
        if len(steps) == 1 and path.endswith(".py"):
            return json.dumps({"tool": "run_command", "args": {"command": f"python {path}"}})
        return json.dumps({"final": f"Criei {path}" + (" e executei com sucesso." if len(steps) > 1 else ".")})

    def shutdown(self):
        self.models.clear()


class EngineManager:
    def __init__(self, data_dir, prefer="auto", log=print, threads=None):
        self.data_dir = data_dir
        self.prefer = prefer if prefer in ("auto", "ollama", "llamacpp", "mock") else "auto"
        self.log = log
        self.threads = int(threads) if threads else max(1, hardware.cpu_cores() // 2)
        self._impl = None
        self._lock = threading.Lock()
        self.rpc_provider = None     # grupo: só o llama.cpp gerenciado divide camadas (o Ollama não tem RPC)

    def _engine(self):
        with self._lock:   # escolhido uma vez: o Ollama, se estiver no ar; senão o llama.cpp gerenciado
            if self._impl is None:
                if self.prefer == "mock":
                    self._impl = _Mock()
                elif self.prefer == "ollama":
                    self._impl = _Ollama(self.log)
                elif self.prefer == "llamacpp":
                    self._impl = _LlamaCpp(self.data_dir, self.log, self.threads)
                else:
                    ollama = _Ollama(self.log)
                    self._impl = ollama if ollama.available() else _LlamaCpp(self.data_dir, self.log, self.threads)
                self.log(f"[motor] motor de IA: {self._impl.kind}")
                if hasattr(self._impl, "rpc_provider"):
                    self._impl.rpc_provider = self.rpc_provider
            return self._impl

    @property
    def kind(self):
        return self._engine().kind

    def available(self) -> bool:
        return self._engine().available()

    def describe(self) -> dict:
        """{"kind", "ready", "detail", "installing", "install_progress" (0..1, enquanto baixa o motor)}"""
        d = dict(self._engine().describe())
        d.setdefault("installing", False)
        d.setdefault("install_progress", 0.0)
        return d

    def load(self, name, gguf_path, sha256):
        """Confere o GGUF (check_gguf: RefusedFile se não passar) e bloqueia até o modelo estar pronto para
        conversar (levanta EngineError se não der)."""
        check_gguf(gguf_path)
        self._engine().load(name, gguf_path, sha256)

    def unload(self, name):
        self._engine().unload(name)

    def free_memory(self):
        """Libera a RAM dos modelos de texto parados (antes de imagem ou vídeo, que precisam dela)."""
        impl = self._engine()
        if hasattr(impl, "free_memory"):
            try:
                impl.free_memory()
            except Exception as e:
                self.log(f"[motor] não consegui liberar a memória: {e}")

    def loaded(self) -> list:
        return self._engine().loaded()

    def chat(self, name, messages, max_tokens, json_mode=False, timeout=None, on_text=None):
        """-> (texto, tokens_in, tokens_out). `timeout`: segundos que o motor tem para responder. `on_text`, se
        dada, recebe o texto até agora a cada pedaço (o cliente vê a resposta sendo escrita)."""
        if on_text:
            return self._engine().chat(name, messages, max_tokens, json_mode, timeout, on_text=on_text)
        return self._engine().chat(name, messages, max_tokens, json_mode, timeout)

    def shutdown(self):
        with self._lock:
            impl = self._impl
        if impl:
            impl.shutdown()
