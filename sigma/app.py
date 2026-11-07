"""Sigma, o aplicativo: execute e pronto, o computador minera SIG e hospeda a rede.

    python -m sigma.app                   painel numa janela de app + janelinha de controle
    python -m sigma.app --headless        sem interface (servidores, VPS); Ctrl+C desliga
    python -m sigma.app --wallet sig…     minera e hospeda para esse endereço

Ninguém escolhe modelo, site ou job à mão: a rede decide e o app obedece.
Tudo se gerencia pelo painel local (http://127.0.0.1:47811), que só aceita pedidos
com o token desta execução e com Host 127.0.0.1/localhost. Uma pasta de dados tem um
app só (app.lock); uma versão mais nova fecha a antiga e assume o lugar dela.
"""
import argparse
import base64
import binascii
import collections
import glob
import hashlib
import hmac
import json
import math
import multiprocessing
import os
import pathlib
import queue
import re
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, catalog, chain, grupo, netsafe, securestore, updater
from .chain import UNIT
from .client import Client, SigmaError
from .crypto import N, Wallet, checksum_ok, norm_address, to_eth

VERSION = __version__
DEFAULT_NODE = "https://sigma.195-35-29-232.sslip.io"
DEFAULT_PORT = 47811
POLL_SECONDS = 3
IDLE_POLL_SECONDS = 30       # painel fechado: consulta o nó bem menos (milhares de apps falam com o mesmo nó)
PANEL_IDLE = 60              # sem pedido do painel há esse tempo = ninguém está olhando
EPOCHS_SHOWN = 24            # épocas recentes que o painel desenha na aba Rede
HISTORY_SHOWN = 100          # entradas do histórico da carteira (créditos já vêm somados por época)
RETRY_SECONDS = 20           # serviço que falhou ao iniciar tenta de novo depois disso
BATTERY_SECONDS = 30         # de quanto em quanto tempo confere se o notebook saiu da tomada
HIDDEN_SETTINGS = ("cheat",)  # só no config.json (testes e demo): fora do painel e da API
MAX_BODY = 100 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024  # limite de blob do nó
SITE_GRACE = 7 * 86400       # site expirado há mais que isso: o nó já apagou os arquivos dele
AI_CONTEXT = 4096            # contexto dos modelos da rede (o mesmo de client.CONTEXT e engines.CONTEXT)
MAX_AI_PRICE = 1_000_000     # teto da chain para o preço por mil tokens
PRICES_SECONDS = 30          # o teto de preço de cada modelo é refeito no máximo a cada 30 s
QUIT_WAIT = 60               # um Sigma antigo leva até ~50 s para desligar tudo
LOCK_WAIT = 30               # outra instância desta pasta ainda subindo: espera ela responder
OPENER = "abrir.html"        # página local que leva o token ao painel sem passar pela linha de comando
CODE_TEXT = 4000             # prévias e argumentos que o painel mostra (o resto é cortado)
CODE_OUTPUT = 8000           # saída de comando e respostas guardadas por passo do Sigma Code
CODE_STEPS = 300             # passos guardados na memória (a tarefa mais recente)
GIB = 2 ** 30                # o "GB" que o Windows mostra
DISK_TOP = 100_000           # GB de disco que dá para ceder à rede, no máximo
SITE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
TYPED_ADDRESS = re.compile(r"(0[xX]|sig)[0-9a-fA-F]{40}")
CHAT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")    # id de votação (= id da transação que a abriu)
ENGINES = ("auto", "ollama", "llamacpp", "mock")
HOST_KEYS = ("ai_enabled", "compute_enabled", "disk_gb", "ai_ram_gb", "public_url", "engine", "ai_threads", "name",
             "ai_image", "ai_video", "lend")
BOOL_SETTINGS = ("mining_enabled", "ai_enabled", "compute_enabled", "start_with_system", "mine_on_battery",
                 "ai_image", "ai_video", "lend")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LABELS = {"cpu_percent": "Uso do processador", "mining_enabled": "Minerar SIG", "ai_enabled": "IA para a rede",
          "compute_enabled": "Computação para a rede", "disk_gb": "Disco para a rede",
          "ai_ram_gb": "Memória para IA", "node": "Endereço do nó",
          "start_with_system": "Iniciar o Sigma junto com o computador",
          "mine_on_battery": "Minerar também na bateria", "name": "Nome na rede",
          "public_url": "Endereço público desta máquina", "engine": "Motor de IA", "ai_threads": "Núcleos para a IA",
          "ai_image": "Hospedar IA de imagem", "ai_video": "Hospedar IA de vídeo",
          "lend": "Emprestar memória para grupos"}
ADDR_TYPO = "Esse endereço tem um caractere errado. Copie e cole de novo direto da MetaMask, sem digitar."
ADDR_BAD = ("endereço inválido: ele começa com 0x (ou sig) e tem mais 40 letras e números. Copie e cole de novo "
            "direto da MetaMask.")
NO_CODE_MODEL = "nenhum modelo de IA está no ar na rede agora: tente de novo daqui a pouco"
CERT_FIX =("O Python deste Mac não tem os certificados de segurança. Abra Aplicativos › Python 3.x › "
            "Install Certificates.command e abra o Sigma de novo.")
SWAP_NEEDS_KEY = ("Antes de trocar de carteira, mostre e guarde a chave da carteira atual: ela é o único jeito de "
                  "voltar a acessar o saldo, os royalties e os orçamentos dela.")
CODE_FOLDER_UNC = ("digite o caminho de uma pasta em um disco deste computador (por exemplo C:\\projetos\\meu-site), "
                   "não um caminho de rede (\\\\…) nem de dispositivo")
CANCELED = "Pedido cancelado. O valor reservado voltou para a sua carteira."
CANCEL_MEMORY = 60           # s que um Cancelar adiantado (antes da pergunta chegar) fica valendo
CLAIMED = ("Um computador já está respondendo. Espere mais um pouco: se ele não entregar, o valor reservado "
           "volta para você sozinho.")
# o painel é um arquivo só, sem nada de fora: nem fontes, nem scripts, nem imagens de terceiros. As imagens e
# vídeos que a IA gera chegam por /api/media (com o token) e viram blob: na página
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
            "media-src blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
TASKS = ("text", "image", "video")
UNITS = {"text": "mil tokens", "image": "imagem", "video": "segundo de vídeo"}
MEDIA_LIMITS = {"image": {"sizes": ["512x512", "768x768", "1024x1024", "768x1024", "1024x768"], "max_count": 4},
                "video": {"sizes": ["480x272", "272x480", "832x480", "480x832"], "seconds": [2, 3, 5]}}
MAX_MEDIA_PRICE = 100_000_000   # teto da chain por imagem e por segundo de vídeo
MEDIA_EXT = {"image/png": ".png", "video/webm": ".webm"}
MEDIA_FILE = re.compile(r"^([0-9a-f]{64})\.(png|webm)\Z")
MEDIA_GRACE = 600             # s: mídia que nenhuma conversa cita, mas recém-chegada (o painel ainda vai guardar)
CHAT_FILE_ID = re.compile(r"^c[0-9a-f]{12}\Z")
MAX_CHATS = 500               # conversas guardadas (as mais antigas saem)
MAX_CHAT_MESSAGES = 1000
MAX_CHAT_CHARS = 2_000_000    # texto de uma conversa (as mídias ficam em arquivos à parte)
# + o bloco da v2, se o nó já marcou (App._no_media_yet)
NO_MEDIA_YET = "a rede ainda não aceita modelos de imagem e vídeo: eles entram na atualização das regras v2"
API_CSP = "default-src 'none'; frame-ancestors 'none'"


class AppError(Exception):
    """Erro explicado ao usuário (HTTP 400 no painel); `code` diz ao painel o que aconteceu (ex.: "canceled")."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- utilidades
def default_data_dir():
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"), "Sigma")
    return os.path.join(os.path.expanduser("~"), ".sigma")


def web_file(name):
    """Arquivo do painel: na pasta do código, no PyInstaller (sys._MEIPASS) ou dentro do .pyz."""
    base = getattr(sys, "_MEIPASS", None)
    paths = [os.path.join(base, "sigma", "web", name), os.path.join(base, "web", name)] if base else []
    paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", name))
    for p in paths:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
    try:  # zipapp: o arquivo está dentro do .pyz
        from importlib import resources
        return resources.files(__package__).joinpath("web", name).read_bytes()
    except Exception:
        return None


def icon_file(name):
    """Ícone Σ da janelinha: no PyInstaller (sys._MEIPASS/sigma), ao lado do código ou em packaging/."""
    base = getattr(sys, "_MEIPASS", None)
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(base, "sigma", name), os.path.join(base, name)] if base else []
    paths += [os.path.join(here, name), os.path.join(os.path.dirname(here), "packaging", name)]
    return next((p for p in paths if os.path.isfile(p)), None)


def icon_data(name):
    """O mesmo ícone dentro do .pyz (zipapp), onde não existe arquivo no disco para o Tk abrir."""
    try:
        from importlib import resources
        return resources.files(__package__).joinpath(name).read_bytes()
    except Exception:
        return None


FALLBACK_PAGE = ("<!doctype html><meta charset=utf-8><title>Sigma</title><body style='background:#000;color:#eee;"
                 "font-family:sans-serif;padding:40px'><h1 style='color:#ff3b1f'>&Sigma; Sigma</h1><p>O Sigma está "
                 "rodando, mas o painel (sigma/web/app.html) não foi encontrado nesta instalação.</p>").encode()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    """Gravação atômica com permissão 600: session.json guarda o token do painel."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(json.dumps(obj, indent=2, ensure_ascii=False).encode())
    os.replace(tmp, path)


def micro(v, name="valor", allow_zero=False):
    """SIG decimal (como a pessoa digita) -> µSIG inteiros, sem erro de arredondamento de float."""
    if v is None or isinstance(v, (bool, dict, list)):
        raise AppError(f"{name} inválido")
    try:
        d = Decimal(str(v).strip().replace(",", "."))
    except InvalidOperation:
        raise AppError(f"{name} inválido: use um número em SIG, como 1,5") from None
    if not d.is_finite():
        raise AppError(f"{name} inválido")
    m = d * UNIT
    if m != m.to_integral_value():
        raise AppError(f"{name}: no máximo 6 casas decimais (1 µSIG = 0,000001 SIG)")
    m = int(m)
    if m < 0 or (m == 0 and not allow_zero):
        raise AppError(f"{name} deve ser maior que zero")
    if m > 10 ** 15:
        raise AppError(f"{name} grande demais")
    return m


def _num(v, name):
    if v is None or isinstance(v, (bool, dict, list)):
        raise AppError(f"{name} inválido")
    try:
        n = float(v.replace(",", ".")) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        raise AppError(f"{name} inválido") from None
    if n != n or n in (float("inf"), float("-inf")):
        raise AppError(f"{name} inválido")
    return n


def _int(v, name, lo, hi):
    n = _num(v, name)
    if n != int(n) or not lo <= n <= hi:
        raise AppError(f"{name} deve ser um inteiro entre {lo} e {hi}")
    return int(n)


def _pct(v, name, top):
    """Porcentagem digitada (ex.: 10) -> basis points (1000)."""
    n = _num(v, name)
    if not 0 <= n <= top:
        raise AppError(f"{name} deve estar entre 0 e {top}%")
    return int(round(n * 100))


def _opt(d, key, default):
    """Campo opcional: ausente ou null usa o padrão."""
    v = d.get(key)
    return default if v is None or v == "" else v


def _sig(m):
    """µSIG -> SIG exato (Decimal) para os métodos do Client, que recebem SIG."""
    return Decimal(m) / UNIT


def _shift(t, offset):
    """Horário do nó -> relógio deste PC (o painel conta o tempo com o relógio local)."""
    return t + offset if isinstance(t, (int, float)) and not isinstance(t, bool) and t else (t or 0)


def _tidy(n):
    return int(n) if float(n).is_integer() else round(n, 2)


def _url(v, name):
    s = str(v or "").strip().rstrip("/")
    u = urlparse(s)
    if u.scheme not in ("http", "https") or not u.netloc or len(s) > 300:
        raise AppError(f"{name}: use uma URL http:// ou https://")
    return s


def gib(mb):
    """MB decimais (hardware e chain) -> o GB que o Windows mostra (2^30 bytes)."""
    return float(mb or 0) * 1e6 / GIB


def parse_private_key(v):
    s = str(v or "").strip().lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{64}", s):
        raise AppError("chave privada inválida: são 64 caracteres, de 0 a 9 e de a até f")
    k = int(s, 16)
    if not 0 < k < N:
        raise AppError("chave privada fora do intervalo válido")
    return k


def typed_address(v):
    """Endereço que a pessoa digitou ou colou: confere o checksum da MetaMask (EIP-55) antes de normalizar,
    porque depois de normalizado um caractere trocado não aparece mais."""
    s = str(v or "").strip() if isinstance(v, str) else ""
    if not TYPED_ADDRESS.fullmatch(s):   # só 0x/sig + 40 hex: nada de espaço, sinal ou "_" no meio
        raise AppError(ADDR_BAD)
    if not checksum_ok(s):
        raise AppError(ADDR_TYPO)
    return norm_address(s)


def friendly(err):
    """Erro de conexão em texto para a pessoa: no macOS, a falta de certificados vira o passo a passo."""
    return CERT_FIX if _is_mac() and netsafe.cert_problem(err) else str(err)


def autostart_supported():
    """'Iniciar o Sigma junto com o computador' por enquanto só existe no Windows (chave Run do usuário)."""
    return os.name == "nt"


def version_tuple(v):
    """'2.1.0' -> (2, 1, 0); texto ilegível conta como mais velho que qualquer versão."""
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except (TypeError, ValueError):
        return ()


def sig_txt(m, casas=None):
    """µSIG -> '1.234,5 SIG' (formato brasileiro)."""
    s = f"{m / UNIT:,.{casas if casas is not None else 6}f}"
    if casas is None:
        s = s.rstrip("0").rstrip(".")
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " SIG"


def rate_txt(h):
    """Contas por segundo no mesmo formato do painel: H/s abaixo de 1.000, depois kH/s ou MH/s com 1 casa."""
    h = float(h or 0)
    for unit, div in (("MH/s", 1e6), ("kH/s", 1e3)):
        if h >= div:
            return f"{h / div:.1f} {unit}".replace(".", ",")
    return f"{h:.0f} H/s"


def site_files(items):
    """[{path, data_b64}] -> {caminho: bytes}; se tudo veio dentro de uma pasta só, ela vira a raiz."""
    if not isinstance(items, list) or not items:
        raise AppError("escolha os arquivos do site")
    if len(items) > 500:
        raise AppError("no máximo 500 arquivos por site")
    files = {}
    for it in items:
        if not isinstance(it, dict):
            raise AppError("cada arquivo precisa de path e data_b64")
        path = str(it.get("path", "")).replace("\\", "/").lstrip("/")
        while path.startswith("./"):
            path = path[2:]
        if not path or any(p in ("", ".", "..") for p in path.split("/")):
            raise AppError(f"caminho inválido: {path or '(vazio)'}")
        try:
            data = base64.b64decode(it.get("data_b64") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise AppError(f"conteúdo inválido em {path}") from None
        if len(data) > MAX_FILE:
            raise AppError(f"{path} passa de 20 MB, o limite por arquivo")
        files[path] = data
    if all("/" in p for p in files) and len({p.split("/", 1)[0] for p in files}) == 1:
        files = {p.split("/", 1)[1]: v for p, v in files.items()}
    if sum(len(v) for v in files.values()) > 60 * 1024 * 1024:
        raise AppError("o site passa de 60 MB")
    return files


def _media_of(msgs):
    """Hashes das imagens e vídeos que as mensagens de uma conversa guardada citam (só hashes válidos: eles viram
    nome de arquivo em <dados>/midia)."""
    out = set()
    for m in msgs if isinstance(msgs, list) else []:
        for it in m.get("media") if isinstance(m, dict) and isinstance(m.get("media"), list) else []:
            if isinstance(it, dict) and HASH_RE.match(str(it.get("hash"))):
                out.add(it["hash"])
    return out


def ai_ceilings(machines, mining, me=None, tasks=None):
    """Teto de preço que o cliente vai usar em cada modelo, pela mesma regra do client.ask/generate: máquinas
    online, com IA ligada e `pub`, que sinalizaram nesta janela ou na anterior; vale o maior entre o preço padrão
    da rede e o da mais barata. Na unidade da classe do modelo (`tasks`: nome -> classe): µSIG por mil tokens,
    por imagem ou por segundo de vídeo. O painel mostra o custo máximo antes de enviar. Os modelos de imagem e
    vídeo de cada máquina vêm em `media_models` (o nó deixa em `models` só os de texto, que um app 2.4 entende)."""
    mining = mining if isinstance(mining, dict) else {}
    tasks = tasks or {}
    w = mining.get("window")
    defaults = {"text": mining.get("ai_default_price"), **{
        t: (mining.get("media_default_prices") or {}).get(chain.TASK_PRICE[t]) for t in ("image", "video")}}
    fallback = {"text": getattr(chain, "DEFAULT_PRICES", {}).get("ai_per_1k_tokens", 2000),
                "image": chain.MEDIA_PRICES["ai_per_image"], "video": chain.MEDIA_PRICES["ai_per_video_second"]}
    for t, v in defaults.items():
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            defaults[t] = fallback[t]
    best = {}
    for addr, m in machines.items() if isinstance(machines, dict) else ():
        pub = m.get("pub") if isinstance(m, dict) else None
        if not isinstance(pub, str) or len(pub) != 128 or not m.get("online") or not m.get("ai") or addr == me:
            continue
        seen = m.get("last_seen")
        if isinstance(w, int) and not (isinstance(seen, int) and seen >= w - 1):
            continue
        for name in [n for k in ("models", "media_models") if isinstance(m.get(k), list) for n in m[k]]:
            if not isinstance(name, str):
                continue
            task = tasks.get(name, "text")
            price = (m.get("prices") or {}).get(chain.TASK_PRICE.get(task, "ai_per_1k_tokens"))
            top = MAX_AI_PRICE if task == "text" else MAX_MEDIA_PRICE
            if isinstance(price, int) and not isinstance(price, bool) and 0 < price <= top:
                best[name] = min(best.get(name, price), price)
    return {name: max(defaults.get(tasks.get(name, "text"), 1), p) for name, p in best.items()}


def _ping_auth(token, nonce):
    """Prova (HMAC) de que quem responde /api/ping conhece o token desta pasta, sem revelá-lo: assim o novo
    Sigma só manda o token para desligar quem for mesmo a instância anterior, nunca para um impostor na porta."""
    return hmac.new(str(token).encode(), str(nonce).encode(), hashlib.sha256).hexdigest()


def ping(port, timeout=1.5, nonce=None):
    """Já existe um Sigma nesta porta? (sem proxy: é sempre a própria máquina). Com `nonce` (hex), pede também
    a prova HMAC do token, para autenticar quem responde antes de um /api/quit."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}/api/ping" + (f"?nonce={nonce}" if nonce else "")
    try:
        with opener.open(url, timeout=timeout) as r:
            d = json.loads(r.read())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ------------------------------------------------------- bateria (notebook)
def _linux_on_battery(root="/sys/class/power_supply"):
    """Na bateria quando existe fonte "Mains" e todas elas dizem online = 0 (desktop sem fonte: na tomada)."""
    states = []
    for d in glob.glob(os.path.join(root, "*")):
        try:
            with open(os.path.join(d, "type"), encoding="utf-8") as f:
                if f.read().strip() != "Mains":
                    continue
            with open(os.path.join(d, "online"), encoding="utf-8") as f:
                states.append(f.read().strip())
        except OSError:
            continue
    return bool(states) and all(s == "0" for s in states)


def _mac_on_battery(pmset_output):
    return "'Battery Power'" in str(pmset_output or "")


def _windows_on_battery():
    import ctypes

    class _Power(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32)]
    st = _Power()
    if not ctypes.WinDLL("kernel32").GetSystemPowerStatus(ctypes.byref(st)):
        return False
    return st.ACLineStatus == 0 and st.BatteryFlag != 128   # 128 = este computador não tem bateria


def on_battery():
    """O notebook está na bateria? Qualquer erro conta como na tomada (a mineração nunca para à toa)."""
    try:
        if os.name == "nt":
            return _windows_on_battery()
        if sys.platform == "darwin":
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
            return _mac_on_battery(out)
        return _linux_on_battery()
    except Exception:
        return False


def _is_mac():
    return sys.platform == "darwin"


# ------------------------------------------------- pastas do Sigma Code
def _norm(p):
    return os.path.normcase(os.path.realpath(p))


def _shell_folders():
    """Windows: onde o sistema diz que estão a Área de Trabalho, os Documentos e os Downloads (o OneDrive
    costuma levar as duas primeiras para dentro dele)."""
    if os.name != "nt":
        return []
    out = []
    try:
        import ctypes
        import uuid
        shell32, ole32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
        fn = shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        fn.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        for guid in ("B4BFCC3A-DB2C-424C-B029-7FE99A87C641",    # Área de Trabalho
                     "FDD39AD0-238F-46AF-ADB4-6C85480369C7",    # Documentos
                     "374DE290-123F-4565-9164-39C4925E467B"):   # Downloads
            ptr = ctypes.c_void_p()
            if fn(uuid.UUID(guid).bytes_le, 0, None, ctypes.byref(ptr)) == 0 and ptr.value:
                out.append(ctypes.wstring_at(ptr.value))
            if ptr.value:
                ole32.CoTaskMemFree(ptr)
    except Exception:
        pass
    return out


def _xdg_folders(home):
    """Linux: as pastas do usuário de ~/.config/user-dirs.dirs (podem estar traduzidas)."""
    out = []
    try:
        with open(os.path.join(home, ".config", "user-dirs.dirs"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*XDG_(DESKTOP|DOCUMENTS|DOWNLOAD)_DIR="(.+)"', line)
                if m:
                    out.append(m.group(2).replace("$HOME", home))
    except OSError:
        pass
    return out


def _known_folders(home=None):
    """Área de Trabalho, Downloads e Documentos (também as do OneDrive): o Sigma Code trabalha em subpastas
    delas, nunca nelas inteiras, onde costumam ficar documentos pessoais."""
    home = home or os.path.expanduser("~")
    names = ("Desktop", "Downloads", "Documents", "Área de Trabalho", "Documentos", "Transferências")
    roots = [home] + [os.environ[k] for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
                      if os.environ.get(k)]
    try:
        roots += [os.path.join(home, n) for n in os.listdir(home) if n.lower().startswith("onedrive")]
    except OSError:
        pass
    return [os.path.join(r, n) for r in roots for n in names] + _shell_folders() + _xdg_folders(home)


def code_folder(raw, data_dir, home=None):
    """Pasta onde o Sigma Code pode trabalhar; recusa, explicando, as que expõem coisa demais."""
    s = str(raw or "").strip().strip('"').strip("'").strip() if isinstance(raw, str) else ""
    if not s:
        raise AppError("escolha a pasta do projeto")
    home = home or os.path.expanduser("~")
    path = os.path.join(home, s[2:]) if s == "~" or s.startswith(("~/", "~\\")) else s
    if not os.path.isabs(path):   # relativo à pasta de onde o Sigma foi aberto: quase nunca é o que a pessoa quer
        example = r"C:\Users\voce\projetos\meu-site" if os.name == "nt" else "/home/voce/projetos/meu-site"
        raise AppError(f"digite o caminho completo da pasta (por exemplo {example})")
    path = os.path.abspath(path)
    if os.name == "nt":
        # Caminhos de rede e de dispositivo expõem demais e furam as comparações abaixo (o realpath os mantém).
        # "\\?\C:\..." é só o mesmo caminho com o prefixo de caminho longo: tira ele antes de comparar.
        low = path.lower()
        if low.startswith("\\\\?\\unc\\") or low.startswith("\\\\.\\"):
            raise AppError(CODE_FOLDER_UNC)
        if low.startswith("\\\\?\\"):
            path = path[4:]
        if path.startswith("\\\\"):   # \\servidor\compart, \\localhost\c$, \\127.0.0.1\c$: caminho de rede
            raise AppError(CODE_FOLDER_UNC)
    if not os.path.exists(path):
        raise AppError(f"a pasta {path} não existe")
    if not os.path.isdir(path):
        raise AppError(f"{path} é um arquivo, não uma pasta: escolha a pasta do projeto")
    real = _norm(path)
    if os.path.dirname(real) == real:
        raise AppError("escolha a pasta de um projeto, não a unidade inteira")
    if real == _norm(home):
        raise AppError("escolha a pasta de um projeto, não a sua pasta de usuário inteira")
    known = {_norm(p) for p in _known_folders(home)}
    if real in known:
        raise AppError("escolha uma pasta de projeto dentro dela: a Área de Trabalho, os Downloads e os Documentos "
                       "inteiros não podem")
    data = _norm(data_dir)
    if real == data or real.startswith(data.rstrip(os.sep) + os.sep):
        raise AppError("essa pasta fica dentro da pasta de dados do Sigma, onde estão as chaves: escolha a pasta "
                       "do projeto")
    if data.startswith(real.rstrip(os.sep) + os.sep):
        raise AppError("essa pasta contém a pasta de dados do Sigma, onde estão as chaves: escolha a pasta do "
                       "projeto")
    # Recusa também uma pasta que CONTÉM (é ancestral de) a pasta do usuário ou uma conhecida — senão a raiz do
    # OneDrive (que tem dentro a Área de Trabalho e os Documentos recusados) passaria.
    prefix = real.rstrip(os.sep) + os.sep
    if _norm(home).startswith(prefix) or any(k.startswith(prefix) for k in known):
        raise AppError("essa pasta contém a sua pasta de usuário ou uma pasta pessoal (Área de Trabalho, "
                       "Documentos, Downloads): escolha a pasta do projeto dentro dela")
    return path


def _clip(v, n=CODE_TEXT):
    """Argumentos vindos da IA, cortados para o painel (e só com tipos que o JSON aceita)."""
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + "…"
    if isinstance(v, dict):
        return {str(k): _clip(x, n) for k, x in list(v.items())[:50]}
    if isinstance(v, (list, tuple)):
        return [_clip(x, n) for x in list(v)[:50]]
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return str(v)[:n]


# ------------------------------------------------------- janela e sistema
def find_browser():
    """Edge primeiro (vem no Windows), depois Chrome/Chromium: abrem o painel como janela de app."""
    paths, names = [], ["msedge", "microsoft-edge", "microsoft-edge-stable", "chrome", "google-chrome",
                        "google-chrome-stable", "chromium", "chromium-browser"]
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for sub in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            paths += [os.path.join(r, sub) for r in roots if r]
    elif sys.platform == "darwin":
        paths = [f"/Applications/{a}.app/Contents/MacOS/{a}" for a in ("Microsoft Edge", "Google Chrome", "Chromium")]
    for p in paths:
        if os.path.isfile(p):
            return p
    return next((shutil.which(n) for n in names if shutil.which(n)), None)


def write_opener(path, url):
    """Grava <dados>/ui/abrir.html (permissão 600), que só leva o navegador até o painel com o token.
    Assim o token nunca aparece na linha de comando do navegador, que outros usuários do PC podem ler."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    page = (f'<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Sigma</title>'
            f'<script>location.replace({json.dumps(url)})</script>')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(page.encode())
    if os.name != "nt":
        os.chmod(path, 0o600)   # um arquivo que já existia mantém a permissão antiga sem isto
    return pathlib.Path(path).resolve().as_uri()


def open_window(url, ui_dir):
    """Abre o painel numa janela de app. O token vai num arquivo local regravado a cada abertura."""
    try:
        target = write_opener(os.path.join(ui_dir, OPENER), url)
    except OSError:
        target = url.split("#", 1)[0]   # sem o arquivo, abre sem o token (nunca na linha de comando)
    exe = find_browser()
    if exe:
        try:
            os.makedirs(ui_dir, exist_ok=True)
            extra = {} if os.name == "nt" else {"start_new_session": True}
            subprocess.Popen([exe, f"--app={target}", f"--user-data-dir={ui_dir}", "--no-first-run",
                              "--no-default-browser-check", "--window-size=1200,820"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **extra)
            return True
        except OSError:
            pass
    try:
        return webbrowser.open(target)
    except Exception:
        return False


def read_autostart():
    if not autostart_supported():
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, "Sigma")[0]
    except OSError:
        return None


def set_autostart(enabled, command):
    """Liga/desliga o 'iniciar com o computador' (Windows: chave Run do usuário, sem pedir administrador)."""
    if not autostart_supported():
        if enabled:
            raise AppError(f"{LABELS['start_with_system']}: por enquanto só funciona no Windows.")
        return
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, "Sigma", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(k, "Sigma")
            except FileNotFoundError:
                pass


class MinerLink:
    """O Host pausa o minerador durante a IA; como o minerador pode ser recriado, o Host fala com este elo."""

    def __init__(self):
        self.miner = None

    def pause(self):
        if self.miner:
            self.miner.pause()

    def resume(self):
        if self.miner:
            self.miner.resume()

    def stats(self):
        return self.miner.stats() if self.miner else {}

    def last_mining(self):
        """(último /mining do minerador, quando chegou): o Host sinaliza a janela sem consultar o nó de novo."""
        m = self.miner
        return m.last_mining() if m and hasattr(m, "last_mining") else (None, 0.0)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"   # no Windows o SO_REUSEADDR deixaria outro programa dividir a porta
    app = None

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)   # pula o getfqdn() do HTTPServer, que pode demorar
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if self.app and not isinstance(err, (ConnectionError, TimeoutError)):
            self.app.log(f"[painel] erro de conexão: {err!r}")


# ------------------------------------------------------------- Sigma Code
class CodeRunner:
    """Sigma Code dentro do app: uma tarefa por vez, numa thread, com a carteira do app.

    Quem responde é o computador de outra pessoa: nada é gravado nem executado sem a pessoa aprovar no
    painel (arquivos só se ela mesma liberar), e as chaves do app ficam protegidas."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.thread = None
        self.waiting = None       # aprovação aberta: {"id", "event", "ok"}
        self.n = 0
        self.agent = None         # o agente da sessão: continua com /api/code/continue
        self.info = self._blank()

    @staticmethod
    def _blank(**kw):
        return dict({"running": False, "folder": "", "model": "", "task": "", "auto_files": False, "steps": [],
                     "pending": None, "spent": 0, "final": None, "error": None, "turns": []}, **kw)

    @staticmethod
    def agent_class():
        from .sigmacode import Agent
        return Agent

    def state(self):
        with self.lock:
            i = self.info
            return dict(i, steps=[dict(s) for s in i["steps"]], pending=dict(i["pending"]) if i["pending"] else None,
                        turns=[dict(t) for t in i["turns"]],
                        can_continue=bool(self.agent) and not i["running"])

    def summary(self):
        with self.lock:
            return {"running": self.info["running"], "pending": bool(self.info["pending"])}

    def _add(self, step):
        """Guarda um passo da conversa (com o lock já pego)."""
        step.setdefault("n", self.n)
        step.setdefault("turn", len(self.info["turns"]))
        self.info["steps"].append(step)
        del self.info["steps"][:-CODE_STEPS]

    def start(self, d):
        app = self.app
        busy = AppError("o Sigma Code já está trabalhando numa tarefa: espere terminar ou clique em Parar")
        with self.lock:
            if self.info["running"]:
                raise busy
        folder = code_folder(d.get("folder"), app.data_dir)
        task = d.get("task").strip() if isinstance(d.get("task"), str) else ""
        if not task:
            raise AppError("escreva o que o Sigma Code deve fazer")
        if len(task) > 20_000:
            raise AppError("a tarefa passa de 20.000 caracteres: resuma o pedido")
        auto = d.get("auto_files", False)   # padrão: toda gravação de arquivo pede aprovação
        if not isinstance(auto, bool):
            raise AppError("auto_files deve ser true ou false")
        model = d.get("model")
        if d.get("local") is True:   # modo local: o modelo baixado neste PC, sem rede nem custo
            if not isinstance(model, str) or not app.local().ready(model):
                raise AppError("no modo local, escolha um modelo baixado neste computador (tela Modelos → Baixar)")
            client = app.local()
        else:
            client = None
        try:   # "no ar" = alguma máquina que aceita pedido cifrado agora (a mesma regra do client.ask)
            if client is not None:
                raise _LocalOK()
            client = app._spender()
            # o Sigma Code conversa: só modelo de texto. A classe vem do /models (sem ela, não dá para saber)
            tasks = app._model_tasks()
            if model in (None, ""):
                model = client.pick_model(prefer="coder")
                if tasks.get(model, "text") != "text":   # o cliente errou a classe: a sessão só falharia
                    raise AppError(NO_CODE_MODEL)
            elif not isinstance(model, str) or not catalog.MODEL_NAME.match(model):
                raise AppError("modelo inválido")
            elif tasks.get(model, "text") != "text":
                raise AppError(f"o modelo {model} gera imagens ou vídeos: o Sigma Code precisa de um modelo de texto")
            elif model not in client.hosted_models(tasks):
                raise AppError(f"o modelo {model} não está no ar agora: escolha outro")
        except _LocalOK:
            pass
        except SigmaError as e:   # sem modelo no ar, ou o nó fora do ar (aí vale a frase do cliente)
            raise AppError(NO_CODE_MODEL if "nenhuma máquina" in str(e) else friendly(e)) from None
        agent = self.agent_class()(client, model, max_tokens=2048, max_steps=15, auto_yes=False, root=folder,
                                   confirm=self._confirm, on_event=self._event, should_stop=self.stop_ev.is_set,
                                   auto_files=auto, protect=app.key_files())
        with self.lock:
            if self.info["running"]:
                raise busy
            self.stop_ev.clear()
            self.waiting, self.n, self.agent = None, 0, agent
            self.info = self._blank(running=True, folder=folder, model=model, task=task, auto_files=auto,
                                    turns=[{"n": 1, "text": task[:CODE_TEXT]}])
            self.thread = threading.Thread(target=self._run, args=(agent, task), name="sigma-code", daemon=True)
            self.thread.start()
        app.log(f"[sigma code] tarefa nova em {folder} com o modelo {model}"
                + (" (gravar arquivos sem perguntar)" if auto else ""))
        return self.state()

    def follow_up(self, d):
        """Próxima instrução da mesma sessão (mesma pasta, mesmo modelo, a conversa continua)."""
        text = d.get("text").strip() if isinstance(d.get("text"), str) else ""
        if not text:
            raise AppError("escreva a próxima instrução para o Sigma Code")
        if len(text) > 20_000:
            raise AppError("a instrução passa de 20.000 caracteres: resuma o pedido")
        client = self.app._spender()
        with self.lock:
            if self.info["running"]:
                raise AppError("o Sigma Code ainda está trabalhando: espere terminar ou clique em Parar")
            agent = self.agent
            if agent is None:
                raise AppError("não há sessão para continuar: comece uma tarefa")
            # a carteira e o nó de agora (a pessoa pode ter trocado desde o começo da sessão): quem paga é a carteira
            # que o painel mostra. O agente está parado, então trocar o cliente aqui não corre com nada
            agent.client = client
            self.stop_ev.clear()
            self.waiting = None
            self.info["turns"].append({"n": len(self.info["turns"]) + 1, "text": text[:CODE_TEXT]})
            self.info.update(running=True, final=None, error=None, pending=None)
            self.thread = threading.Thread(target=self._run, args=(agent, text, True), name="sigma-code",
                                           daemon=True)
            self.thread.start()
        self.app.log(f"[sigma code] instrução nova na mesma sessão ({self.info['folder']})")
        return self.state()

    def reset(self):
        """Nova sessão: esquece a conversa terminada (a pasta e os arquivos ficam como estão)."""
        with self.lock:
            if self.info["running"]:
                raise AppError("o Sigma Code ainda está trabalhando: clique em Parar antes")
            self.agent, self.n = None, 0
            self.info = self._blank()
        return {"ok": True}

    def _run(self, agent, task, follow_up=False):
        try:
            final = agent.run(task, follow_up=follow_up)
            with self.lock:
                if isinstance(final, str) and final and not self.info["final"]:
                    self.info["final"] = final
                    self._add({"kind": "final", "text": final})
        except Exception as e:   # saldo, rede ou erro do agente: aparece na conversa, o app segue
            with self.lock:
                self.info["error"] = str(e)
                self._add({"kind": "error", "text": str(e)})
        finally:
            with self.lock:
                if self.stop_ev.is_set() and not self.info["final"] and not self.info["error"]:
                    self._add({"kind": "info", "text": "Tarefa parada."})
                w, self.waiting = self.waiting, None
                if w:
                    w["event"].set()
                self.info.update(running=False, pending=None)
                spent = self.info["spent"]
            self.app.log(f"[sigma code] tarefa terminada: {sig_txt(spent)} gastos")
            self.app.wake.set()

    def _event(self, ev):
        if not isinstance(ev, dict):
            return
        kind, text = ev.get("kind"), str(ev.get("text") or "")
        with self.lock:
            if kind == "step":
                n = ev.get("n")
                self.n = n if isinstance(n, int) and not isinstance(n, bool) else self.n + 1
                cost = ev.get("cost")
                cost = cost if isinstance(cost, int) and not isinstance(cost, bool) and cost > 0 else 0
                machine = str(ev.get("machine") or "")
                self.info["spent"] += cost
                self._add({"kind": "info", "text": f"Passo {self.n}: {sig_txt(cost)} pagos ao computador "
                                                   f"{machine[:12] or '?'}", "cost": cost, "machine": machine})
            elif kind == "assistant":
                self._add({"kind": "assistant", "text": text[:CODE_OUTPUT]})
            elif kind == "tool":
                tool, refused = str(ev.get("tool") or ""), bool(ev.get("refused"))
                # target = alvo real resolvido pelo sigmacode; o painel mostra ele, nunca uma chave livre dos args
                self._add({"kind": "tool", "text": tool + (" (recusado)" if refused else ""), "tool": tool,
                           "args": _clip(ev.get("args")), "output": str(ev.get("output") or "")[:CODE_OUTPUT],
                           "refused": refused, "target": str(ev.get("target") or "")})
            elif kind == "final":
                self.info["final"] = text
                self._add({"kind": "final", "text": text})
            elif kind == "error":
                self.info["error"] = text
                self._add({"kind": "error", "text": text})
            elif kind == "limit":
                self._add({"kind": "info", "text": "O Sigma Code chegou ao limite de passos desta tarefa. Confira o "
                                                   "que já foi feito e, se quiser, peça o resto numa tarefa nova."})

    def _confirm(self, tool, args, info):
        """Chamado pela thread do agente antes de gravar, editar ou executar: espera a resposta do painel."""
        info = info if isinstance(info, dict) else {}
        w = {"id": secrets.token_hex(8), "event": threading.Event(), "ok": False}
        with self.lock:
            if self.stop_ev.is_set():
                return False
            self.waiting = w
            # target = alvo real resolvido (visible(caminho)); o painel usa ele no título, não uma chave dos args
            self.info["pending"] = {"id": w["id"], "tool": str(tool), "args": _clip(args),
                                    "reason": str(info.get("reason") or ""),
                                    "target": str(info.get("target") or ""),
                                    "preview": str(info.get("preview") or "")[:CODE_TEXT],
                                    "changed": [str(c) for c in (info.get("changed") or [])][:200]}
        w["event"].wait()   # até /api/code/answer, /api/code/stop ou o app desligar
        with self.lock:
            if self.waiting is w:
                self.waiting, self.info["pending"] = None, None
        return bool(w["ok"]) and not self.stop_ev.is_set()

    def answer(self, d):
        ok = d.get("ok")
        if not isinstance(ok, bool):
            raise AppError("ok deve ser true ou false")
        with self.lock:
            w = self.waiting
            if not w or w["id"] != d.get("id"):
                raise AppError("esse pedido de aprovação não está mais aberto")
            w["ok"] = ok
            self.waiting, self.info["pending"] = None, None
            w["event"].set()
        return self.state()

    def stop(self):
        """Para depois do passo atual e recusa o que estiver esperando aprovação."""
        self.stop_ev.set()
        with self.lock:
            w, self.waiting = self.waiting, None
            self.info["pending"] = None
            if w:
                w["ok"] = False
                w["event"].set()
            if self.info["running"]:
                self._add({"kind": "info", "text": "Parando depois do passo atual…"})
        return self.state()


# ------------------------------------------------------------------- o app
class _LocalOK(Exception):
    """Sigma Code no modo local: pula a conferência dos modelos da rede."""


class App:
    def __init__(self, args):
        self.args = args
        self.data_dir = os.path.abspath(args.data_dir or default_data_dir())
        norm = os.path.normcase(self.data_dir).removeprefix("\\\\?\\")
        self.instance = hashlib.sha256(norm.encode()).hexdigest()[:16]   # duas pastas de dados = dois apps
        def p(name):
            return os.path.join(self.data_dir, name)
        self.config_path, self.secret_path, self.machine_path = p("config.json"), p("wallet.secret"), p("machine.key")
        self.session_path, self.log_path, self.ui_dir = p("session.json"), p("sigma.log"), p("ui")
        self.lock_path, self.opener_path = p("app.lock"), os.path.join(self.ui_dir, OPENER)
        self.lock = threading.RLock()       # mudanças de configuração e de serviços, uma por vez
        self.log_lock = threading.Lock()
        self.shut_lock = threading.Lock()
        self.logs = collections.deque(maxlen=400)
        self.stopping, self.done, self.wake = threading.Event(), threading.Event(), threading.Event()
        self._shut = False
        self._lock_fd = None
        self.token = secrets.token_urlsafe(32)
        self.port = self.httpd = self.http_thread = None
        self.allowed_hosts, self.allowed_origins, self.url = set(), set(), ""
        self.link, self.miner, self.host, self._workers = MinerLink(), None, None, None
        self.cache = self._empty_cache()
        self.node_state = {"ok": None, "error": None, "fix": None}   # None = ainda conectando
        self._node_fails = 0
        self.on_battery, self._battery_at = False, 0.0
        self._errors, self._last_start, self.viewed = {}, 0.0, 0.0
        self._prices = (0.0, {})
        self.wallet = self.address = self.payout_override = self.machine = None
        self.config, self.saved, self.overrides, self.settings, self.hw = {}, {}, {}, {}, {}
        self.hidden = {}
        self.chats, self.chat_lock = [], threading.Lock()
        self.canceled_ids = {}   # id -> quando: Cancelar que chegou antes da própria pergunta (corrida de cliques)
        self.code = CodeRunner(self)
        self.ui_calls, self.window_up = queue.Queue(), False   # pedidos do painel para a thread da janelinha

    @staticmethod
    def _empty_cache():
        # balance None = ainda não lido do /account do endereço atual (o painel mostra "—", não "0,00")
        return {"status": {}, "mining": {}, "me": {}, "balance": None, "history": [], "epochs": []}

    # ------------------------------------------------------------- registro
    def log(self, *parts, **_):
        """Também é o `log` do minerador e do Host (por isso aceita o jeito do print)."""
        line = f"{time.strftime('%H:%M:%S')} {' '.join(str(p) for p in parts)}"
        self.logs.append(line)
        self._echo(line)
        with self.log_lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d ") + line + "\n")
            except OSError:
                pass

    @staticmethod
    def _echo(line):
        if sys.stdout is not None:   # PyInstaller --windowed não tem console
            try:
                print(line, flush=True)
            except (OSError, ValueError):
                pass

    def _trace(self):
        with self.log_lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            except OSError:
                pass

    def _report(self, what, err):
        """Registra a falha de um serviço sem repetir a mesma mensagem a cada tentativa."""
        msg = f"{type(err).__name__}: {err}"
        if self._errors.get(what) != msg:
            self._errors[what] = msg
            self.log(f"[{what}] erro: {err}")
            self._trace()

    # --------------------------------------------- uma instância por pasta
    def take_lock(self):
        """Trava app.lock (exclusiva, sem esperar). O sistema solta sozinho se o app fechar ou travar."""
        if self._lock_fd is not None:
            return True
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return True   # sem como criar a trava: segue como antes dela (a pasta de dados vai reclamar sozinha)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def release_lock(self):
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _wait_lock(self, seconds=None):
        deadline = time.time() + (LOCK_WAIT if seconds is None else seconds)
        while not self.take_lock():
            if time.time() >= deadline:
                return False
            time.sleep(0.5)
        return True

    def already_running(self):
        """Mesmo app (mesma pasta de dados) já no ar? Devolve {"port", "token", "version", "url"} ou None."""
        sess = _read_json(self.session_path) or {}
        ports = [sess["port"]] if isinstance(sess.get("port"), int) else []
        want = self.args.port or DEFAULT_PORT
        ports += [want] if want not in ports else []
        for port in ports:
            info = ping(port)
            if info.get("app") == "sigma" and info.get("instance") == self.instance:
                token = sess.get("token") if sess.get("port") == port and isinstance(sess.get("token"), str) else None
                return {"port": port, "token": token, "version": str(info.get("version") or ""),
                        "url": f"http://127.0.0.1:{port}/" + (f"#t={token}" if token else "")}
        return None

    def quit_running(self, found):
        """Pede para o Sigma que está no ar desligar (o mesmo botão Desligar) e espera ele sair do ar.

        Antes de mandar o token, confirma por um desafio HMAC que quem responde na porta conhece o token desta
        pasta (só a instância anterior conhece): assim o token nunca vai para um impostor que só ocupou a porta."""
        token = found.get("token")
        if not token:
            return False
        port = found["port"]
        nonce = secrets.token_hex(16)
        if not hmac.compare_digest(str(ping(port, nonce=nonce).get("auth") or ""), _ping_auth(token, nonce)):
            self.log(f"quem responde em 127.0.0.1:{port} não provou conhecer o token desta pasta; não vou "
                     f"desligá-lo nem mandar o token")
            return False
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b"{}", method="POST",
                                     headers={"X-Sigma-Token": found["token"], "Content-Type": "application/json"})
        try:
            opener.open(req, timeout=5).close()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False   # token velho: ele não vai desligar por este pedido
        except Exception:
            pass   # pode fechar antes de responder; o que vale é ele sair do ar
        deadline = time.time() + QUIT_WAIT
        while time.time() < deadline:
            if ping(port, timeout=1).get("instance") != self.instance:
                return True
            time.sleep(1)
        return False

    def claim_instance(self, gui):
        """Uma instância por pasta de dados. Devolve None se esta deve subir, ou o código de saída do processo.

        Se conseguimos a app.lock, nenhum Sigma desta pasta está rodando (ele seguraria a trava): o que estiver
        respondendo na porta é outro programa, então ignoramos e subimos numa porta livre. Só confiamos em quem
        responde quando a trava está com outro processo — igual ou mais nova: abre a janela dela; mais antiga:
        autentica, pede para ela desligar e assume."""
        locked, found, said = self.take_lock(), self.already_running(), False
        deadline = time.time() + LOCK_WAIT
        while not locked and not found:   # outra instância pegou a pasta e ainda está subindo
            if time.time() >= deadline:
                _fatal("O Sigma já está aberto, mas não respondeu. Feche-o pelo Gerenciador de Tarefas e abra de "
                       "novo.", gui)
                return 2
            if not said:
                print("O Sigma já está abrindo; a janela aparece em instantes.", flush=True)
                said = True
            time.sleep(0.5)
            locked = self.take_lock()
            found = self.already_running()
        if locked:
            # Temos a trava: nenhum Sigma desta pasta está no ar. Um 'found' aqui é outro programa ocupando a
            # porta — não abrimos a janela dele nem mandamos o token; o _serve sobe numa porta livre.
            if found:
                self.log(f"outro programa responde em 127.0.0.1:{found['port']}; o Sigma vai subir em outra porta")
            return None
        # A trava está com outro processo desta pasta: um Sigma de verdade. Agora dá para confiar em 'found'.
        if version_tuple(found["version"]) < version_tuple(VERSION):
            print(f"um Sigma mais antigo ({found['version'] or '?'}) está aberto nesta pasta: fechando ele para "
                  f"abrir o {VERSION}", flush=True)
            if self.quit_running(found) and self._wait_lock():
                return None
            # Não pegamos a trava. Confere de novo quem responde: se já é um Sigma desta pasta igual ou mais novo
            # (ex.: dois cliques no executável novo — o outro já assumiu), é só abrir, não um erro.
            again = self.already_running()
            if again and version_tuple(again["version"]) >= version_tuple(VERSION):
                print(f"o Sigma já está rodando (painel em http://127.0.0.1:{again['port']})", flush=True)
                if gui and not self.args.background:
                    open_window(again["url"], self.ui_dir)
                return 0
            self.release_lock()
            _fatal("O Sigma antigo não fechou. Clique em Desligar na janelinha dele e abra este de novo.", gui)
            return 2
        self.release_lock()
        print(f"o Sigma já está rodando (painel em http://127.0.0.1:{found['port']})", flush=True)
        if gui and not self.args.background:
            open_window(found["url"], self.ui_dir)
        return 0

    # ----------------------------------------------------------- inicialização
    def boot(self):
        os.makedirs(self.data_dir, exist_ok=True)
        try:
            if os.path.getsize(self.log_path) > 2_000_000:
                os.replace(self.log_path, self.log_path + ".1")
        except OSError:
            pass
        self.log(f"Sigma {VERSION} · dados em {self.data_dir}")
        self.hw = self._detect_hw()
        self.machine = self._load_machine_key()   # antes da configuração: o nome padrão sai do endereço dela
        self._load_config()
        self._load_wallet()
        if self.args.wallet:
            self.payout_override = norm_address(self.args.wallet)
        if self.args.headless and not self._payout():
            w = Wallet.generate()
            self._store_wallet(w.address, w)
            self.log(f"carteira criada automaticamente: {w.address} · a chave privada está guardada, protegida, "
                     f"em {self.secret_path} (exporte pelo painel)")
        self._sync_autostart()
        self._check_battery(force=True)   # antes de ligar o minerador: um notebook na bateria nem começa
        self._serve()
        self.log(f"nó: {self.settings['node']} · painel local: http://127.0.0.1:{self.port} "
                 f"(token em {self.session_path})")
        updater.cleanup()   # o arquivo da versão anterior (.old) já pode sair
        self.updater = updater.AutoUpdater(
            node=lambda: self.settings["node"], version_available=self.update_available,
            busy=lambda: bool(self.host and self.host.busy()), log=self.log, on_restart=self._restart_for_update,
            enabled=not getattr(self.args, "no_auto_update", False) and not os.environ.get("SIGMA_NO_AUTO_UPDATE"))
        if self.updater.enabled:
            self.log("atualização automática ligada: quando sair versão nova, o Sigma se atualiza sozinho")
        if self._payout():
            self.log(f"tudo o que este computador ganhar vai para {self._payout()}")
        else:
            self.log("falta escolher a carteira: abra o painel para começar a minerar")
        threading.Thread(target=self._poll_loop, name="sigma-poll", daemon=True).start()
        threading.Thread(target=self.start_services, name="sigma-start", daemon=True).start()
        threading.Thread(target=self._media_gc, name="sigma-midia", daemon=True).start()

    def _detect_hw(self):
        try:
            from .hardware import detect
            return detect(self.data_dir)
        except Exception as e:
            self._report("hardware", e)
            try:
                free = shutil.disk_usage(self.data_dir).free // 10 ** 6
            except OSError:
                free = 0
            return {"cpu_cores": os.cpu_count() or 1, "ram_mb": 0, "vram_mb": 0, "gpu": "",
                    "disk_free_mb": free, "os": sys.platform}

    def _ram_top(self):
        """Memória que dá para ceder à IA, em GB do Windows (2^30 bytes): nunca mais do que o PC tem."""
        ram = int(self.hw.get("ram_mb") or 0)
        return max(1, int(gib(ram))) if ram else 1024

    def _defaults(self):
        ram = int(self.hw.get("ram_mb") or 0)
        # o nome fica público na chain: o padrão não revela o nome do PC (a pessoa pode trocar no painel)
        name = f"maquina-{self.machine.address[3:9]}" if self.machine else "maquina"
        # tudo no máximo por padrão, menos o disco: o computador dá à rede o que tem (a pessoa diminui no painel)
        return {"cpu_percent": 100, "mining_enabled": True, "ai_enabled": True, "compute_enabled": True,
                "disk_gb": 20, "ai_ram_gb": self._ram_top() if ram else 4,
                "node": DEFAULT_NODE, "start_with_system": autostart_supported(), "mine_on_battery": True, "name": name,
                "public_url": "", "engine": "auto", "ai_threads": 0, "ai_image": True, "ai_video": True,
                "lend": True}

    def _clean(self, k, v):
        """Valida uma configuração (vinda do painel, da linha de comando ou do config.json)."""
        label = LABELS.get(k, k)
        if k in BOOL_SETTINGS:
            if not isinstance(v, bool):
                raise AppError(f"{label}: o valor deve ser true ou false")
            return v
        if k == "cpu_percent":
            n = round(_num(v, label))
            if not 1 <= n <= 100:
                raise AppError(f"{label}: escolha de 1 a 100%.")
            return int(n)
        if k == "disk_gb":
            n = _num(v, label)
            if not 0 <= n <= DISK_TOP:
                raise AppError(f"{label}: escolha de 0 a {DISK_TOP} GB.")
            return _tidy(n)
        if k == "ai_ram_gb":
            n, top = _num(v, label), self._ram_top()
            if not 0 <= n <= top:
                raise AppError(f"{label}: escolha de 0 a {top} GB (a memória deste computador).")
            return _tidy(n)
        if k == "node":
            return _url(v, label)
        if k == "public_url":
            return "" if not str(v or "").strip() else _url(v, label)
        if k == "name":
            s = str(v or "").strip()
            if not 1 <= len(s) <= 40:
                raise AppError(f"{label}: use de 1 a 40 caracteres.")
            return s
        if k == "engine":
            if v not in ENGINES:
                raise AppError(f"{label}: escolha um de: {', '.join(ENGINES)}.")
            return v
        if k == "ai_threads":
            return _int(v, label, 0, 512)
        raise AppError(f"configuração desconhecida: {k}")

    def _load_config(self):
        self.config = _read_json(self.config_path) or {}
        defaults = self._defaults()
        stored = self.config.get("settings")
        stored = stored if isinstance(stored, dict) else {}
        self.saved = dict(defaults)
        for k, v in stored.items():
            if k in defaults:
                if k == "ai_ram_gb" and isinstance(v, (int, float)) and not isinstance(v, bool) and v > self._ram_top():
                    v = self._ram_top()   # a v2.0 contava em MB decimais e deixava passar um pouco além
                try:
                    self.saved[k] = self._clean(k, v)
                except AppError:
                    self.log(f"configuração '{k}' inválida no config.json; usando o padrão")
        # escondidas: só valem ligadas à mão no config.json (a demo usa "cheat" para mostrar a punição)
        self.hidden = {k: True for k in HIDDEN_SETTINGS if stored.get(k) is True}
        if self.hidden:
            self.log(f"configurações escondidas ligadas no config.json: {', '.join(self.hidden)}")
        a, o = self.args, {}
        # a linha de comando vale só para esta execução (scripts e servidores sempre passam as mesmas opções)
        for key, val in (("node", a.node), ("cpu_percent", a.cpu), ("disk_gb", a.disk_gb), ("name", a.name),
                         ("engine", a.engine), ("public_url", a.public_url)):
            if val is not None:
                o[key] = val
        if a.no_mine:
            o["mining_enabled"] = False
        if a.no_ai:
            o["ai_enabled"] = False
        self.group = {}   # só desta execução, como o resto da linha de comando
        try:
            if getattr(a, "coordinate", None):
                self.group = {"listen": a.coordinate,
                              "workers": dict(grupo.parse_worker(w) for w in a.group_worker),
                              "kind": grupo.parse_kind(getattr(a, "group_kind", None))}
            elif getattr(a, "worker_of", None):
                coord, endpoint = grupo.parse_coordinator(a.worker_of)
                self.group = {"coordinator": coord, "endpoint": endpoint,
                              "kind": grupo.parse_kind(getattr(a, "group_kind", None))}
        except grupo.GroupError as e:
            raise AppError(str(e)) from None
        self.overrides = {k: self._clean(k, v) for k, v in o.items()}
        self.settings = {**self.saved, **self.overrides}
        self._save_config()

    def _save_config(self):
        self.config["settings"] = {**self.saved, **self.hidden}
        self.config.setdefault("wallet", {"address": None})
        self.config["version"] = 2
        _write_json(self.config_path, self.config)

    def _load_key(self, path):
        return Wallet(parse_private_key(securestore.load_secret(path).decode("utf-8", "replace")))

    def _load_machine_key(self):
        """Chave da máquina: assina registro, pings e jobs; gerada aqui e nunca mostrada a ninguém."""
        if os.path.exists(self.machine_path):
            try:
                return self._load_key(self.machine_path)
            except (OSError, AppError) as e:
                bad = f"{self.machine_path}.ilegivel-{int(time.time())}"
                os.replace(self.machine_path, bad)
                self.log(f"a chave da máquina não abriu ({e}); criei outra (a antiga ficou em {bad})")
        w = Wallet.generate()
        securestore.save_secret(self.machine_path, format(w.priv, "064x").encode())
        return w

    def _load_wallet(self):
        addr = (self.config.get("wallet") or {}).get("address")
        try:
            addr = norm_address(addr) if addr else None
        except ValueError:
            addr = None
        self.address, self.wallet = addr, None
        if os.path.exists(self.secret_path):
            try:
                key = self._load_key(self.secret_path)
            except (OSError, AppError) as e:
                self.log(f"atenção: a chave da carteira não abriu ({e}); importe-a de novo pelo painel")
                return
            if addr is None or key.address == addr:
                self.wallet, self.address = key, key.address

    def _payout(self):
        return self.payout_override or self.address

    def _can_spend(self):
        w = self.wallet
        return bool(w and w.address == self._payout())

    def key_files(self):
        """O que o Sigma Code nunca lê nem altera: a pasta de dados e as chaves do app (também as antigas)."""
        old = os.path.join(self.data_dir, "carteiras-antigas")
        return [self.data_dir, self.secret_path, self.machine_path, self.session_path, self.config_path, old,
                *glob.glob(os.path.join(old, "*"))]

    def _store_wallet(self, address, wallet):
        """Grava a carteira; uma chave antiga nunca é apagada, só guardada em carteiras-antigas/."""
        with self.lock:
            same = wallet and self.wallet and self.wallet.address == wallet.address
            if os.path.exists(self.secret_path) and not same:
                old = self.wallet.address if self.wallet else "desconhecida"
                folder = os.path.join(self.data_dir, "carteiras-antigas")
                os.makedirs(folder, exist_ok=True)
                dest = os.path.join(folder, f"{old}-{time.strftime('%Y%m%d-%H%M%S')}.secret")
                os.replace(self.secret_path, dest)
                self.log(f"a chave da carteira anterior ({old}) foi guardada em {dest}")
            if wallet:
                securestore.save_secret(self.secret_path, format(wallet.priv, "064x").encode())
            self.config["wallet"] = {"address": address}
            self._save_config()
            if address != self._payout():   # outra carteira: saldo e histórico da antiga não valem mais
                self.cache = dict(self.cache, balance=None, history=[], me={})   # saldo desconhecido até ler /account
            self.wallet, self.address, self.payout_override = wallet, address, None

    def _set_wallet(self, address, wallet):
        before = self._payout()
        self._store_wallet(address, wallet)
        job = self.restart_services if before and before != address else self.start_services
        threading.Thread(target=job, name="sigma-wallet", daemon=True).start()
        self.wake.set()

    def autostart_command(self):
        if getattr(sys, "frozen", False):
            cmd = f'"{sys.executable}" --background'
        else:  # rodando do código-fonte: pythonw (sem console) e o caminho do pacote
            exe = sys.executable
            w = os.path.join(os.path.dirname(exe), "pythonw.exe")
            exe = w if os.path.exists(w) else exe
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if root.lower().endswith(".pyz") or os.path.isfile(root):
                # rodando de um .pyz: o zipapp já tem __main__; roda ele direto (um -c com o caminho quebraria
                # quando ele tem apóstrofo, ex. C:\Users\D'Avila)
                cmd = f'"{exe}" "{root}" --background'
            else:
                # pasta de código: o caminho vai como literal Python entre aspas simples, com \ e ' escapados —
                # assim um apóstrofo no caminho não quebra nem o Python nem as aspas do -c do Windows
                lit = "'" + root.replace("\\", "\\\\").replace("'", "\\'") + "'"
                cmd = (f'"{exe}" -c "import sys; sys.path.insert(0, {lit}); '
                       f'from sigma.app import main; main()" --background')
        if os.path.normcase(self.data_dir) != os.path.normcase(os.path.abspath(default_data_dir())):
            cmd += f' --data-dir "{self.data_dir}"'
        if self.args.port:
            cmd += f" --port {self.args.port}"
        return cmd

    def _sync_autostart(self):
        """Se a pessoa ligou 'iniciar com o computador', mantém o comando apontando para este executável."""
        if autostart_supported() and self.settings["start_with_system"] \
                and read_autostart() != self.autostart_command():
            try:
                set_autostart(True, self.autostart_command())
            except (OSError, AppError) as e:
                self._report("inicialização automática", e)

    # ----------------------------------------------------------- servidor local
    def _serve(self):
        ports = [self.args.port] if self.args.port else list(range(DEFAULT_PORT, DEFAULT_PORT + 10))
        err = None
        for port in ports:
            try:
                self.httpd = _Server(("127.0.0.1", port), make_handler(self))
                break
            except OSError as e:
                err = e
        else:
            raise AppError(f"não consegui abrir o painel local na porta {ports[0]} (ocupada? use --port): {err}")
        self.httpd.app = self
        self.port = self.httpd.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}
        self.url = f"http://127.0.0.1:{self.port}/#t={self.token}"
        self.http_thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5},
                                            name="sigma-http", daemon=True)
        self.http_thread.start()
        _write_json(self.session_path, {"port": self.port, "token": self.token, "pid": os.getpid(),
                                        "started": time.time(), "version": VERSION})

    def page(self):
        return web_file("app.html") or FALLBACK_PAGE

    def open_window(self):
        if self.url and not open_window(self.url, self.ui_dir):
            self.log(f"não consegui abrir uma janela; abra no navegador o arquivo {self.opener_path}")

    # --------------------------------------------- pedidos para a janelinha
    def ask_ui(self, fn, timeout=600):
        """Roda `fn(root)` na thread da janelinha (a única que pode mexer no tkinter) e espera o resultado."""
        if not self.window_up:
            raise AppError("digite o caminho da pasta")
        box, done = {}, threading.Event()
        self.ui_calls.put((fn, box, done))
        if not done.wait(timeout):
            raise AppError("a janela de escolher pasta não respondeu: digite o caminho da pasta")
        if "error" in box:
            raise AppError(f"não consegui abrir o seletor de pastas ({box['error']}): digite o caminho da pasta")
        return box.get("result")

    def run_ui_calls(self, root, cancel=False):
        while True:
            try:
                fn, box, done = self.ui_calls.get_nowait()
            except queue.Empty:
                return
            try:
                if cancel:
                    raise RuntimeError("a janelinha do Sigma fechou")
                box["result"] = fn(root)
            except Exception as e:
                box["error"] = e
            finally:
                done.set()

    # ------------------------------------------------------------- serviços
    def workers(self):
        cores = os.cpu_count() or 1
        return max(1, min(cores, round(cores * self.settings["cpu_percent"] / 100)))

    def host_settings(self):
        s = self.settings
        return {"ai_enabled": s["ai_enabled"], "compute_enabled": s["compute_enabled"], "disk_gb": s["disk_gb"],
                "ai_ram_gb": s["ai_ram_gb"], "public_url": s["public_url"], "engine": s["engine"],
                "ai_threads": s["ai_threads"] or None, "name": s["name"], "ai_image": s["ai_image"],
                "ai_video": s["ai_video"], "lend": s.get("lend", True), **self.hidden,
                "group": getattr(self, "group", None) or None}

    def start_services(self):
        """Liga o que falta (Host e minerador). Pode ser chamado de novo: só completa o que não subiu."""
        with self.lock:
            payout = self._payout()
            if self.stopping.is_set() or not payout:
                return
            self._last_start = time.time()
            if self.host is None:
                h = None
                try:
                    from .host import Host
                    h = Host(self.settings["node"], self.machine, payout, self.data_dir, self.host_settings(),
                             miner=self.link, log=self.log)
                    h.start()
                    self.host, h = h, None
                    self._errors.pop("host", None)
                except Exception as e:
                    self._report("host", e)
                finally:
                    if h is not None:   # criou mas não subiu: não deixa threads/motores pela metade
                        self._stop(h)
            self._sync_miner()

    def _battery_pause(self):
        return self.on_battery and not self.settings.get("mine_on_battery")

    def _mining_wanted(self):
        return (bool(self.settings.get("mining_enabled")) and bool(self._payout()) and not self.stopping.is_set()
                and not self._battery_pause())

    def _sync_miner(self):
        if not self._mining_wanted():
            if self.miner:
                self._stop_miner()
                self.log("mineração pausada: o notebook está na bateria" if self._battery_pause()
                         else "mineração desligada")
            return
        n = self.workers()
        if self.miner is None:
            try:
                from .miner import Miner
                # cada PC tem a sua identidade de mineração: vários podem minerar para a mesma carteira
                m = Miner(Client(self.settings["node"]), self._payout(), n, log=self.log,
                          worker=self.machine.address[3:11])
                m.start()
                self.miner, self.link.miner, self._workers = m, m, n
                self._errors.pop("minerador", None)
            except Exception as e:
                self._report("minerador", e)
        elif n != self._workers:
            try:
                self.miner.set_workers(n)
                self._workers = n
            except Exception as e:
                self._report("minerador", e)

    def _check_battery(self, force=False):
        """Notebook saiu da tomada (ou voltou): pausa ou retoma a mineração, a não ser que a pessoa libere."""
        if not force and time.time() - self._battery_at < BATTERY_SECONDS:
            return
        self._battery_at = time.time()
        now = bool(on_battery())
        if now == self.on_battery:
            return
        self.on_battery = now
        if self.settings.get("mining_enabled") and not self.settings.get("mine_on_battery"):
            self.log("o notebook está na bateria: a mineração pausa e volta sozinha no carregador" if now
                     else "carregador ligado: a mineração volta")
        with self.lock:
            if self.miner or self._payout():
                self._sync_miner()

    def _stop(self, obj):
        try:
            obj.stop()
        except Exception as e:
            self._report(type(obj).__name__.lower(), e)
        try:   # garante que nenhum llama-server fique órfão, mesmo se o Host esquecer
            from .engines import EngineManager
            for attr in ("engines", "engine", "engine_manager"):
                eng = getattr(obj, attr, None)
                if isinstance(eng, EngineManager):
                    eng.shutdown()
        except Exception:
            pass

    def _stop_miner(self):
        m, self.miner, self.link.miner, self._workers = self.miner, None, None, None
        if m:
            self._stop(m)

    def stop_services(self):
        with self.lock:
            self._stop_miner()
            h, self.host = self.host, None
            if h:
                self._stop(h)

    def restart_services(self):
        with self.lock:
            self.stop_services()
            self.start_services()
        self.wake.set()

    def _missing(self):
        return self.host is None or (self._mining_wanted() and self.miner is None)

    # ------------------------------------------------- consultas ao nó (cache)
    def _read(self, path):
        return Client(self.settings["node"]).get(path)

    def _poll_loop(self):
        n = 0
        while not self.stopping.is_set():
            try:
                self._poll(full=n % 3 == 0)
                if n and n % 60 == 0:
                    self.hw = self._detect_hw()   # disco livre muda quando a rede traz um modelo
            except Exception as e:
                self._report("painel", e)
            try:
                self._check_battery()
            except Exception as e:
                self._report("bateria", e)
            n += 1
            try:
                if getattr(self, "updater", None):
                    self.updater.tick()
            except Exception as e:
                self._report("atualização", e)
            if self._payout() and self._missing() and time.time() - self._last_start > RETRY_SECONDS:
                self.start_services()
            idle = time.time() - self.viewed > PANEL_IDLE   # ninguém olhando: o minerador e a máquina seguem iguais
            self.wake.wait(IDLE_POLL_SECONDS if idle else POLL_SECONDS)
            self.wake.clear()

    def _node_failed(self, err):
        """Uma consulta ao nó falhou. Só vira "sem conexão" depois de 2 falhas seguidas (ou se estava
        conectado); antes disso o painel continua em "conectando", sem assustar ninguém a cada abertura."""
        self._node_fails += 1
        text = friendly(err)
        fix = text if text == CERT_FIX else None   # a pessoa precisa agir: nem espera a segunda falha
        if self.node_state.get("ok") is True or self._node_fails >= 2 or fix:
            self.node_state = {"ok": False, "error": text, "fix": fix}

    def _restart_for_update(self, launch):
        """Arquivo novo já no lugar. Com `launch`, abre a versão nova, que pede para esta fechar (passagem de
        bastão); sem ele (systemd, pm2), esta desliga e o supervisor abre a nova."""
        def go():
            host = self.host
            if host:   # espera terminar o que tem (no máximo 3 h, o prazo de um vídeo); livre, para de pegar pedidos
                end = time.time() + 3 * 3600   # e a versão nova, que sobe em segundos, pega os que chegarem
                while time.time() < end and not self.stopping.is_set():
                    if not host.busy():
                        host.draining = True
                        if not host.busy():   # nenhum pedido entrou entre a conferência e a trava
                            break
                        host.draining = False
                    time.sleep(5)
            if launch:
                launch()
            else:
                self.shutdown()
        threading.Thread(target=go, name="sigma-atualizacao", daemon=True).start()

    def _poll(self, full):
        node = self.settings["node"]
        c = Client(node)

        def get(fn, default):
            try:
                return fn()
            except Exception:   # rota que o nó ainda não tem não derruba o resto do painel
                return default
        try:
            status = c.status()
        except (SigmaError, ValueError, OSError) as e:
            if self.settings["node"] == node:
                self._node_failed(e)
            return
        cache = dict(self.cache, status=status)
        cache["mining"] = get(c.mining, cache["mining"])
        if isinstance(cache["mining"].get("now"), (int, float)):
            cache["clock"] = time.time() - cache["mining"]["now"]   # diferença entre este PC e o nó
        full = full or cache["mining"].get("epoch") != self.cache["mining"].get("epoch")   # houve distribuição
        addr = self._payout()
        if addr:
            acc = get(lambda: c.account(addr), None)
            if isinstance(acc, dict):
                cache["balance"] = acc.get("balance", 0)
                full = full or cache["balance"] != self.cache["balance"]   # saldo mudou: o histórico também
            cache["me"] = get(lambda: c.miner(addr), cache["me"])
            hist = get(lambda: c.history(addr), None) if full else None
            if isinstance(hist, list):
                cache["history"] = hist[:HISTORY_SHOWN]
        else:
            cache.update(me={}, balance=None, history=[])
        eps = get(c.epochs, None) if full else None   # gráfico de épocas da aba Rede (muda a cada época)
        if isinstance(eps, list):
            cache["epochs"] = eps[:EPOCHS_SHOWN]
        if self.settings["node"] == node and self._payout() == addr:   # trocou no meio: descarta
            self.cache = cache
            self._node_fails = 0
            self.node_state = {"ok": True, "error": None, "fix": None}

    def _ai_prices(self, fresh=False):
        """Teto de preço de cada modelo no ar (cache de 30 s: o painel pergunta a cada mensagem digitada)."""
        t, prices = self._prices
        if fresh or time.time() - t > PRICES_SECONDS:
            try:
                prices = ai_ceilings(self._read("/machines"), self._read("/mining"), self._payout(),
                                     self._model_tasks())
            except (SigmaError, ValueError, OSError):
                pass   # fica com o que já sabia; tenta de novo daqui a pouco
            self._prices = (time.time(), prices)
        return prices

    # ------------------------------------------------------------- estado
    @staticmethod
    def _stats(obj):
        try:
            return (obj.stats() or {}) if obj else {}
        except Exception:
            return {}

    def update_available(self):
        """Versão mais nova que esta rodando na rede (o /status do nó), ou None."""
        v = (self.cache.get("status") or {}).get("version")
        return v if isinstance(v, str) and version_tuple(v) > version_tuple(VERSION) else None

    def rules_notice(self):
        """A rede já passou (late) ou vai passar, num bloco marcado, para regras que este Sigma não conhece
        (docs/ATUALIZACOES.md). Devolve {"rules", "height", "blocks_left", "seconds_left", "late"} ou None."""
        st = self.cache.get("status") or {}

        def num(v):
            return isinstance(v, int) and not isinstance(v, bool)
        now, net = st.get("height"), st.get("rules")
        if num(net) and net > chain.RULES:
            return {"rules": net, "height": None, "blocks_left": 0, "seconds_left": 0, "late": True}
        for u in st.get("upgrades") or []:
            if isinstance(u, dict) and num(u.get("rules")) and num(u.get("height")) and u["rules"] > chain.RULES:
                left = max(0, u["height"] - now) if num(now) else None
                every = st.get("block_interval")
                secs = (round(left * every) if left is not None and isinstance(every, (int, float))
                        and not isinstance(every, bool) and 0 < every < 86400 else None)
                return {"rules": u["rules"], "height": u["height"], "blocks_left": left, "seconds_left": secs,
                        "late": False}
        return None

    def download_url(self):
        return f"{self.settings['node']}/baixar"

    def state(self):
        """GET /api/state. Além do SPEC-v2 §7.3, o painel lê:
        node {"ok": true | false | null (null = ainda conectando: nunca "sem rede" na abertura), "error", "fix"};
        update {"version", "url"} | null (versão nova na rede); features {"autostart", "pick_folder"};
        limits {"ai_ram_gb", "disk_gb"} (máximos que /api/settings aceita, em GB de 2^30 bytes);
        mining {"fail_streak", "report_error"/"last_fail", "last_ok", "reports_failed", "worker", "on_battery",
        "battery_pause", "hosting_per_mb_window", "ai_default_price", …}; machine {"jobs_failed", "ai_queue",
        "engine_down", "engine": {…, "installing", "install_progress"}}; code {"running", "pending"};
        settings {…, "mine_on_battery"}."""
        s, c, addr = self.settings, self.cache, self._payout()
        bal = c["balance"]
        balance_known = isinstance(bal, (int, float)) and not isinstance(bal, bool)   # None = ainda não lido
        ms, hs = self._stats(self.miner), self._stats(self.host)
        mi, me, off = c["mining"] or {}, c["me"] or {}, c.get("clock", 0.0)
        engine = hs.get("engine") or {"kind": s["engine"], "ready": False, "installing": False, "install_progress": 0.0,
                                      "detail": "começa assim que a carteira estiver escolhida" if not addr
                                      else "IA desligada nas configurações" if not s["ai_enabled"] else "iniciando…"}
        last = ms.get("last_report") or {}
        newer = self.update_available()
        return {
            "version": VERSION, "setup_done": bool(addr),
            # balance null + balance_known false = ainda não lido: o painel mostra "—", nunca "0,00"
            "wallet": {"address": addr, "eth_address": to_eth(addr) if addr else None,
                       "can_spend": self._can_spend(), "balance": bal if balance_known else None,
                       "balance_known": balance_known, "history": c["history"]},
            "node": {"url": s["node"], "ok": self.node_state["ok"], "error": self.node_state["error"],
                     "fix": self.node_state.get("fix")},
            "network": c["status"],
            "update": {"version": newer, "url": self.download_url()} if newer else None,
            # versão das regras: a que este Sigma conhece e o aviso de atualização da rede (rules_notice)
            "rules": {"known": chain.RULES, "network": (c["status"] or {}).get("rules"),
                      "notice": self.rules_notice()},
            "features": {"autostart": autostart_supported(), "pick_folder": self.window_up},
            # máximos que as Configurações aceitam (GB do Windows): o painel usa os mesmos nos controles
            "limits": {"ai_ram_gb": self._ram_top(), "disk_gb": DISK_TOP},
            "mining": {"enabled": s["mining_enabled"], "running": bool(ms.get("running")),
                       "paused": bool(ms.get("paused")), "workers": ms.get("workers", 0) if self.miner else 0,
                       "max_workers": os.cpu_count() or 1, "cpu_percent": s["cpu_percent"],
                       "hashrate": float(ms.get("hashrate") or 0), "bits": ms.get("bits") or 0,
                       "shares_window": ms.get("shares_window", 0),
                       "window": mi["window"] if mi.get("window") is not None else ms.get("window") or 0,
                       "window_ends": _shift(mi.get("window_ends"), off), "epoch": mi.get("epoch", 0),
                       "epoch_ends": _shift(mi.get("epoch_ends"), off), "next_mint": mi.get("next_mint", 0),
                       # emissão contínua (v2.3): sai em todo bloco; a taxa de referência é por 12 h
                       "continuous": bool(mi.get("continuous")), "mint_bps": mi.get("mint_bps"),
                       "mint_per_period": mi.get("mint_per_period", mi.get("next_mint", 0)),
                       "mint_per_hour": mi.get("mint_per_hour", 0),
                       "my_points": me.get("points", 0), "my_pending_points": me.get("pending_points", 0),
                       "total_points": mi.get("points_total", 0),
                       "my_share": float(me.get("share") or 0), "est_reward": me.get("est_reward", 0),
                       "est_per_hour": me.get("est_per_hour", me.get("est_reward", 0)),
                       "rewards_total": me.get("rewards_total", 0), "last_error": ms.get("last_error"),
                       # relatórios recusados: o painel avisa "minerando sem contar" em vez de "minerando"
                       "reports_sent": ms.get("reports_sent", 0), "last_report": ms.get("last_report"),
                       "reports_failed": ms.get("reports_failed", 0), "fail_streak": int(ms.get("fail_streak") or 0),
                       "report_error": ms.get("last_fail"), "last_fail": ms.get("last_fail"),
                       "last_ok": ms.get("last_ok") or last.get("time"),
                       "worker": ms.get("worker") or (self.machine.address[3:11] if self.machine else None),
                       "on_battery": self.on_battery, "battery_pause": self._battery_pause(),
                       # extras para o painel (barras de progresso, emissão e preços), direto do /mining do nó
                       "window_seconds": mi.get("window_seconds", 0), "epoch_seconds": mi.get("epoch_seconds", 0),
                       "minted": mi.get("minted", 0), "cap": mi.get("cap", 0),
                       "miners_epoch": mi.get("miners_epoch", 0), "network_hashrate": float(mi.get("hashrate") or 0),
                       "hosting_per_mb_window": mi.get("hosting_per_mb_window"),
                       "ram_per_mb_window": mi.get("ram_per_mb_window"), "demand_max_bps": mi.get("demand_max_bps"),
                       "rent": mi.get("rent") or {}, "proposal_deposit": mi.get("proposal_deposit"),
                       "quorum_bps": mi.get("quorum_bps"),
                       "ai_default_price": mi.get("ai_default_price")},
            "machine": {"address": self.machine.address, "name": s["name"], "registered": bool(hs.get("registered")),
                        "online": bool(hs.get("online")), "hw": self.hw, "engine": engine,
                        "models": hs.get("models", []), "sites": hs.get("sites", []),
                        "jobs_done": hs.get("jobs_done", 0), "jobs_failed": hs.get("jobs_failed", 0),
                        "earned": hs.get("earned", 0), "reputation": hs.get("reputation", 0),
                        "ai_queue": hs.get("ai_queue", 0), "engine_down": bool(hs.get("engine_down")),
                        # v2: motor de imagem/vídeo (stable-diffusion.cpp), fila dele e as classes que a máquina aceita
                        "media_engine": hs.get("media_engine") or {"kind": "sdcpp", "ready": False, "detail": "",
                                                                   "installing": False, "install_progress": 0.0},
                        "media_queue": hs.get("media_queue", 0), "tasks": hs.get("tasks") or ["text"],
                        "last_error": hs.get("last_error") or self._errors.get("host")},
            "epochs": c.get("epochs") or [],   # /mining/epochs do nó, mais recente primeiro
            "code": self.code.summary(),
            "settings": dict(s),
            "logs": list(self.logs)[-150:]}

    def status_line(self):
        """Linha da janelinha de controle: 'Σ SIGMA · minerando · 6,8 kH/s · 12,34 SIG'."""
        if not self._payout():
            return "Σ SIGMA · falta escolher a carteira: clique em Abrir painel"
        ok = self.node_state["ok"]
        if ok is None:
            return "Σ SIGMA · conectando à rede…"
        ms = self._stats(self.miner)
        if not ok:
            mid = "sem conexão com a rede"
        elif not self.settings["mining_enabled"]:
            mid = "mineração pausada · hospedando a rede" if self.host else "mineração pausada"
        elif self._battery_pause():
            mid = "mineração pausada (na bateria)"
        elif ms.get("paused"):
            mid = "mineração pausada (IA trabalhando)"
        elif ms.get("running") and int(ms.get("fail_streak") or 0) >= 2:
            mid = "minerando sem contar (relatórios recusados)"
        elif ms.get("running"):
            mid = f"minerando · {rate_txt(ms.get('hashrate'))}"
        else:
            mid = "hospedando a rede" if self.host else "iniciando…"
        bal = self.cache["balance"]
        tail = sig_txt(bal, 2) if isinstance(bal, (int, float)) and not isinstance(bal, bool) else "—"
        return f"Σ SIGMA · {mid} · {tail}"

    def update_line(self):
        r = self.rules_notice()
        if r and r["late"]:
            return (f"A rede passou para as regras v{r['rules']} e este Sigma não as conhece: baixe a versão nova "
                    "para continuar minerando e hospedando direito.")
        if r:
            return (f"A rede muda de regras (v{r['rules']}) no bloco {r['height']}: baixe a versão nova do Sigma "
                    "antes disso. Ela fecha esta e continua de onde parou.")
        v = self.update_available()
        if v and getattr(self, "updater", None) and self.updater.enabled:
            return f"Saiu uma versão nova do Sigma ({v}): ele se atualiza sozinho assim que a máquina ficar livre."
        return f"Saiu uma versão nova do Sigma ({v}). Baixe e abra: ela fecha esta e continua de onde parou." \
            if v else ""

    # ------------------------------------------------------------- rotas da API
    def _spender(self):
        if not self._payout():
            raise AppError("escolha uma carteira primeiro")
        if not self._can_spend():
            raise AppError(f"esta carteira está no modo só endereço: para gastar SIG daqui, importe a chave em "
                           f"Carteira → Importar a chave desta carteira (ou use a MetaMask em "
                           f"{self.settings['node']}/carteira)")
        return Client(self.settings["node"], self.wallet)

    def _done(self, txid):
        self.wake.set()   # saldo e histórico novos sem esperar a próxima consulta
        return {"txid": txid}

    def api_state(self, q):
        if time.time() - self.viewed > PANEL_IDLE:
            self.wake.set()   # o painel acabou de abrir: busca dados novos na hora, sem esperar a consulta lenta
        self.viewed = time.time()
        return self.state()

    def _guard_switch(self, new_address, d):
        """Trocar para OUTRA carteira move a chave atual para carteiras-antigas/ (o painel não a mostra mais): essa
        chave é o único acesso ao saldo, aos royalties e aos orçamentos da carteira. Só deixa depois do portão do
        painel, que confirma que a pessoa viu/guardou a chave (manda "key_saved": true). Defesa além do painel —
        importante quando o saldo é desconhecido (nó fora do ar) e o portão do painel ainda não apareceu."""
        if (self.address and new_address != self.address and os.path.exists(self.secret_path)
                and d.get("key_saved") is not True):
            raise AppError(SWAP_NEEDS_KEY)

    def api_setup(self, d):
        mode = d.get("mode")
        if mode == "create":
            w = Wallet.generate()
            self._guard_switch(w.address, d)
            self._set_wallet(w.address, w)
            self.log(f"carteira nova criada: {w.address}")
            return {"address": w.address, "private_key": format(w.priv, "064x")}
        if mode == "import_key":
            w = Wallet(parse_private_key(d.get("private_key")))
            if d.get("expect_address"):   # "Importar a chave desta carteira": só a chave da carteira atual
                try:
                    want = norm_address(str(d["expect_address"]).strip())
                except ValueError:
                    raise AppError("expect_address inválido") from None
                if w.address != want:
                    raise AppError(f"Essa chave é de outra carteira ({w.address}). Para receber em outra carteira, "
                                   f"use Trocar carteira.")
            self._guard_switch(w.address, d)
            self._set_wallet(w.address, w)
            self.log(f"carteira importada: {w.address}")
            return {"address": w.address}
        if mode == "address":
            addr = typed_address(d.get("address"))
            self._guard_switch(addr, d)
            self._set_wallet(addr, None)
            self.log(f"minerando e hospedando para {addr} (para gastar, use a MetaMask no site)")
            return {"address": addr}
        raise AppError("mode deve ser 'create', 'import_key' ou 'address'")

    def api_settings(self, d):
        changes = {k: self._clean(k, v) for k, v in d.items()}
        if "node" in changes and changes["node"] != self.settings["node"]:
            try:
                Client(changes["node"]).get("/status")
            except (SigmaError, ValueError, OSError) as e:
                msg = friendly(e)   # o cliente já diz "não consegui falar com o nó X: …": não repete
                raise AppError(msg if "não consegui falar com o nó" in msg or msg == CERT_FIX
                               else f"não consegui falar com o nó {changes['node']}: {msg}") from None
        if "start_with_system" in changes and changes["start_with_system"] != self.settings["start_with_system"]:
            try:
                set_autostart(changes["start_with_system"], self.autostart_command())
            except OSError as e:
                raise AppError(f"o Windows não deixou mudar a inicialização automática: {e}") from None
        with self.lock:
            old = dict(self.settings)
            self.saved.update(changes)
            for k in changes:   # mudou no painel: vale mais que a opção da linha de comando
                self.overrides.pop(k, None)
            self._save_config()
            self.settings = {**self.saved, **self.overrides}
            new = self.settings
            changed = [k for k in changes if old.get(k) != new[k]]
            if changed:
                self.log("configurações: " + ", ".join(f"{k}={new[k]}" for k in changed))
            if new["node"] != old["node"]:   # outra rede: nada do que estava em cache vale mais
                self.cache = self._empty_cache()
                self.node_state, self._node_fails = {"ok": None, "error": None, "fix": None}, 0
                self._prices = (0.0, {})
                threading.Thread(target=self.restart_services, name="sigma-node", daemon=True).start()
            else:
                self._sync_miner()
                if self.host and any(new[k] != old[k] for k in HOST_KEYS):
                    try:
                        self.host.update_settings(self.host_settings())
                    except Exception as e:
                        self._report("host", e)
        self.wake.set()
        return {"settings": dict(self.settings)}

    def api_send(self, d):
        to, amount = typed_address(d.get("to")), micro(d.get("amount"), "valor")
        txid = self._spender().transfer(to, _sig(amount))
        self.log(f"enviado {sig_txt(amount)} para {to}")
        return self._done(txid)

    def api_export(self, q):
        if q.get("confirm", [""])[0] != "1":
            raise AppError("confirme a exportação (confirm=1): quem tiver a chave controla o dinheiro")
        if not self._can_spend():
            raise AppError("não há chave privada guardada para esta carteira")
        self.log("chave privada exportada pelo painel")
        return {"private_key": format(self.wallet.priv, "064x")}

    def _model_tasks(self, models=None):
        """nome -> classe (text/image/video) dos modelos da rede."""
        models = self._read("/models") if models is None else models
        return {n: chain.model_task(m) for n, m in (models or {}).items() if isinstance(m, dict)}

    def _net_parts(self):
        """v4: a rede aceita modelo de texto em partes (os gigantes, que rodam divididos num grupo)?"""
        mi = self.cache.get("mining") or self._read("/mining") or {}
        return bool(mi.get("parts"))

    def _net_media(self):
        mi = self.cache.get("mining") or self._read("/mining") or {}
        return bool(mi.get("media"))

    def _no_media_yet(self):
        """NO_MEDIA_YET com o bloco em que a v2 entra, quando o nó já marcou (as `upgrades` do /status)."""
        ups = (self.cache.get("status") or {}).get("upgrades")
        if not isinstance(ups, list):
            return NO_MEDIA_YET                  # /status ainda não lido: não dá para dizer se já tem bloco
        for u in ups:
            h = u.get("height") if isinstance(u, dict) and u.get("rules") == 2 else None
            if isinstance(h, int) and not isinstance(h, bool):
                return f"{NO_MEDIA_YET}, marcada para o bloco {h}"
        return f"{NO_MEDIA_YET}, que ainda não tem bloco marcado"

    def _group_reason(self, need, size, replicas, cands, used):
        """Por que um gigante ainda não ganhou grupo (regras v8), com as contas: quando a regra entra, quanta memória
        os computadores online emprestam somados (o que sobra depois dos modelos que já hospedam) e quem tem disco
        para coordenar."""
        st = self.cache.get("status") or {}
        rules = st.get("rules") if isinstance(st.get("rules"), int) else 0
        gb = lambda mb: f"{mb / 1000:.1f}".replace(".", ",")   # noqa: E731
        head = (f"Ele só roda dividido num grupo de computadores: pede {gb(need)} GB por grupo (uns "
                f"{math.ceil(need / 8000)} computadores cedendo 8 GB cada)"
                + (f", e cada uma das {replicas} cópias é um grupo." if replicas > 1 else "."))
        if rules < 8:
            h = next((u.get("height") for u in st.get("upgrades") or []
                      if isinstance(u, dict) and u.get("rules") == 8), None)
            when = f"no bloco {h} (a rede está no {st.get('height')})" if h else "quando a atualização das regras v8 entrar"
            return head + f" A rede passa a montar grupos sozinha {when}; até lá, nenhum grupo se forma."
        lenders = {a: x for a, x in cands.items() if x.get("lend")}
        free = {a: int((x.get("hw") or {}).get("ai_ram_mb") or 0) - used.get(a, [0])[0] for a, x in lenders.items()}
        free = {a: v for a, v in free.items() if v >= chain.MIN_LEND_MB}
        coords = [a for a in free if int((lenders[a].get("hw") or {}).get("disk_mb") or 0) * 1_000_000
                  - used.get(a, [0, 0])[1] >= size]
        total = sum(free.values())
        if not lenders:
            return head + (" Nenhum computador online está emprestando memória agora (Configurações → "
                           "\"Emprestar memória para grupos\"; os apps 2.14 já vêm com isso ligado).")
        msg = (head + f" Hoje {len(free)} computador(es) online emprestam memória, somando {gb(total)} GB livres "
               f"(o que sobra depois dos modelos que eles já hospedam)")
        if total < need:
            msg += f": faltam {gb(need - total)} GB para o primeiro grupo."
        else:
            msg += "."
        if not coords:
            msg += f" Nenhum deles tem {gb(size / 1e6)} GB de disco livre para coordenar (guardar o modelo inteiro)."
        return msg

    def _waiting(self, network, machines):
        """Modelo sem nenhuma máquina: por que a rede ainda não o colocou em ninguém (a mesma conta da chain em
        _assign_models) e o que mudaria neste computador. {"reasons": [...], "here": "..."} por nome."""
        from .hardware import gb_to_mb
        if not isinstance(network, dict) or not isinstance(machines, dict):
            return {}
        used = {}                                    # máquina -> [memória, disco, quantos] já comprometidos
        rules = (self.cache.get("status") or {}).get("rules")
        swap = isinstance(rules, int) and rules >= 5    # v5: a memória conta só o maior modelo (eles se revezam)
        top = chain.MAX_MODELS_PER_MACHINE_V5 if swap else chain.MAX_MODELS_PER_MACHINE
        for m in network.values():
            for a in {**(m.get("pinners") or {}), **(m.get("assigned") or {})}:
                u = used.setdefault(a, [0, 0, 0])
                r = int(m.get("ram_mb") or 0)
                u[0] = max(u[0], r) if swap else u[0] + r
                u[1] += int(m.get("size") or 0); u[2] += 1
        names = {"text": "texto", "image": "imagem", "video": "vídeo"}
        switch = {"image": "Gerar imagens", "video": "Gerar vídeos"}
        me, s, out = getattr(self.machine, "address", None), self.settings, {}
        for name, m in network.items():
            if m.get("pinners") or m.get("assigned"):
                continue
            task, need, size = m.get("task") or "text", int(m.get("ram_mb") or 0), int(m.get("size") or 0)
            online = {a: x for a, x in machines.items() if x.get("online") and x.get("ai")
                      and int(x.get("reputation") or 0) > chain.MIN_REPUTATION}
            def free(a, x):
                u, hw = used.get(a, [0, 0, 0]), x.get("hw") or {}
                ram = int(hw.get("ai_ram_mb") or 0) - (0 if swap else u[0])   # v5: cabe se o modelo cabe sozinho
                return ram, int(hw.get("disk_mb") or 0) * 1_000_000 - u[1], u[2]
            ok_task = {a: x for a, x in online.items() if task in (x.get("tasks") or ["text"])}
            ok_ram = {a: x for a, x in ok_task.items() if free(a, x)[0] >= need}
            ok_all = [a for a, x in ok_ram.items() if free(a, x)[1] >= size and free(a, x)[2] < top]
            reasons = []
            if not online:
                reasons.append("Nenhum computador com IA ligada está online agora.")
            elif not ok_task:
                reasons.append(f"Nenhum dos {len(online)} computadores online aceita modelos de {names[task]}"
                               + (f": quem tiver memória pode ligar \"{switch[task]}\" em Configurações." if task in switch else "."))
            elif not ok_ram:
                best = max(free(a, x)[0] for a, x in ok_task.items())
                reasons.append(f"O modelo pede {need / 1000:.1f} GB de memória para IA e o computador que mais tem "
                               f"livre cede {max(best, 0) / 1000:.1f} GB.")
                if need > 15000:   # não cabe num PC comum: só num grupo (docs/GRUPO.md)
                    reasons.append(self._group_reason(need, size, int(m.get("replicas") or 1), ok_task, used))
            elif not ok_all:
                reasons.append(f"Os computadores com memória suficiente não têm disco livre (ou já hospedam {top} modelos).")
            else:
                reasons.append(f"{len(ok_all)} computador(es) servem: a rede sorteia quem baixa na próxima virada de "
                               "janela (até 5 minutos).")
            here = None
            mine = machines.get(me) if me else None
            if task in switch and not s.get("ai_" + task):
                here = f"Este computador não aceita {names[task]}: ligue \"{switch[task]}\" em Configurações."
            elif not s.get("ai_enabled", True):
                here = "Este computador está com a IA desligada nas Configurações."
            elif need > gb_to_mb(s.get("ai_ram_gb") or 0):
                total = int(self.hw.get("ram_mb") or 0)
                here = (f"Este computador cede {s.get('ai_ram_gb')} GB para IA e o modelo pede {need / 1000:.1f} GB"
                        + (": aumente \"Memória para IA\" em Configurações." if total - 1000 >= need
                           else f"; este PC tem {total / 1000:.1f} GB no total, então não cabe aqui."))
            elif size > gb_to_mb(s.get("disk_gb") or 0) * 1_000_000:
                here = f"O modelo ocupa {size / 1e9:.1f} GB de disco: aumente \"Disco para a rede\" em Configurações."
            elif mine is None:
                here = "Este computador ainda não está registrado na rede."
            out[name] = {"reasons": reasons, "here": here}
        return out

    def api_models(self, q):
        network = self._read("/models")
        tasks = self._model_tasks(network)
        media = self._net_media()
        # media_note: o porquê de imagem e vídeo ainda não valerem (o painel não oferece Apadrinhar para eles)
        # os gigantes (split: só divididos entre máquinas) vêm no fim, sem botão de apadrinhar
        try:
            waiting = self._waiting(network, self._read("/machines"))
        except Exception:
            waiting = {}
        return {"network": network, "waiting": waiting,
                "catalog": [dict(i) for i in catalog.CATALOG + catalog.SPLIT_CATALOG],
                "ai": {"prices": self._ai_prices(), "context": AI_CONTEXT, "tasks": tasks, "units": dict(UNITS),
                       "limits": MEDIA_LIMITS, "media": media, "parts": self._net_parts(), "media_note": None if media else self._no_media_yet()}}

    @staticmethod
    def _model_name(v):
        if not isinstance(v, str) or not catalog.MODEL_NAME.match(v):
            raise AppError("nome do modelo: 3 a 48 caracteres, letras minúsculas, números, ponto e hífen")
        return v

    def api_sponsor(self, d):
        self._spender()   # sem chave não adianta nem consultar o Hugging Face
        if d.get("catalog_id"):
            info = catalog.find(d["catalog_id"])
            if not info:
                raise AppError("esse modelo não está no catálogo")
            name, desc = d.get("name") or info["id"], info["description"]
            if info.get("task", "text") != "text" and not self._net_media():
                raise AppError(self._no_media_yet())
            if info.get("split") and not self._net_parts():
                raise AppError("a rede ainda não aceita modelos em partes: eles entram na atualização das regras v4")
        elif d.get("url"):
            info = catalog.resolve_hf_url(d["url"])
            name, desc = d.get("name") or catalog.suggest_name(info["file"]), f"{info['repo']} · {info['file']}"
        else:
            raise AppError("escolha um modelo do catálogo (catalog_id) ou cole o link do Hugging Face (url)")
        problem = catalog.license_error(info["license"])
        if problem:
            raise AppError(problem)
        name = self._model_name(name)
        royalty = _pct(_opt(d, "royalty", 10), "royalty", 50)
        reinvest = _pct(_opt(d, "reinvest", 0), "reinvest", 100)
        replicas, budget = _int(_opt(d, "replicas", 2), "replicas", 1, 5), micro(d.get("budget"), "budget")
        task = info.get("task", "text")
        least = self._min_model_budget(int(info.get("total_size") or info["size"]), replicas, task)
        if least and budget < least:
            raise AppError(f"o orçamento precisa pagar pelo menos um dia de hospedagem das {replicas} cópias: "
                           f"{sig_txt(least)}. Com menos que isso, os computadores da rede não baixam o modelo.")
        txid = self._spender().register_model(name, info["url"], info["sha256"], info["size"], _sig(budget),
                                              Decimal(royalty) / 100, Decimal(reinvest) / 100, replicas,
                                              desc[:200], info["license"], task=task, files=info.get("files"))
        total = int(info.get("total_size") or info["size"])
        self.log(f"você é o padrinho de '{name}' ({total / GIB:.1f} GB): a rede escolhe as máquinas")
        return dict(self._done(txid), name=name)

    def _rent_per_window(self, size, replicas, mi=None, task="text"):
        """Aluguel por janela de todas as cópias de um modelo, como a chain cobra agora (chain.copy_fee): disco +
        memória que ele reserva, cada um com o multiplicador da demanda. None se o nó não informar os preços."""
        mi = mi or self.cache.get("mining") or self._read("/mining") or {}
        disk, ram, rent = mi.get("hosting_per_mb_window"), mi.get("ram_per_mb_window"), mi.get("rent") or {}
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (disk, ram)):
            return None
        db, rb = rent.get("disk_bps", 10_000), rent.get("ram_bps", 10_000)
        ram_mb = catalog.ram_mb(size, task)                   # pesos + contexto/geração, a mesma conta da chain
        cost = size * disk * db + ram_mb * 1_000_000 * ram * rb
        return replicas * max(1, -(-cost // (1_000_000 * 10_000)))

    def _min_model_budget(self, size, replicas, task="text"):
        """µSIG que pagam um dia de aluguel de todas as cópias, ao preço e à demanda de agora (o mesmo cálculo do
        nó e das máquinas, que não baixam modelo com orçamento menor). 0 se o nó não informar o preço."""
        if size <= chain.FUNDED_ABOVE:
            return 0                          # poucos MB: as máquinas baixam com qualquer orçamento
        mi = self.cache.get("mining") or self._read("/mining") or {}
        ws = mi.get("window_seconds")
        rent = self._rent_per_window(size, replicas, mi, task)
        if rent is None or not isinstance(ws, (int, float)) or ws <= 0:
            return 0
        return rent * math.ceil(86400 / ws)

    # ------------------------------------------------------------- votações
    def api_governance(self, q):
        """Votações da rede, as regras de votar e o meu saldo (para o painel mostrar quanto dá para votar)."""
        g = self._read("/governance") or {}
        g["me"] = self._payout()
        g["can_spend"] = self._can_spend()
        return g

    def api_propose(self, d):
        title, text = d.get("title"), d.get("text") or ""
        if not isinstance(title, str) or not 3 <= len(title.strip()) <= 120:
            raise AppError("o título da votação precisa ter de 3 a 120 caracteres")
        if not isinstance(text, str) or len(text) > 2000:
            raise AppError("a explicação pode ter no máximo 2.000 caracteres")
        param, value = d.get("param") or None, d.get("value")
        if param is not None:
            if param not in chain.GOVERNABLE:
                raise AppError("esse parâmetro não pode ser votado")
            lo, hi = chain.GOVERNABLE[param]
            value = _int(value, "valor novo", lo, hi)
        txid = self._spender().propose(title, text, param, value)
        self.log(f"votação aberta: {title.strip()[:60]}")
        return self._done(txid)

    def api_vote(self, d):
        pid = d.get("proposal_id")
        if not isinstance(pid, str) or not HASH_RE.match(pid):
            raise AppError("votação inválida")
        choice = d.get("choice")
        if choice not in chain.CHOICES:
            raise AppError("o voto é 'sim' ou 'nao'")
        amount = micro(d.get("amount"), "quantidade de SIG")
        txid = self._spender().vote(pid, choice, _sig(amount))
        self.log(f"voto '{choice}' com {sig_txt(amount)} (travados até o fim da votação)")
        return self._done(txid)

    def api_model_fund(self, d):
        name, amount = self._model_name(d.get("name")), micro(d.get("amount"), "valor")
        txid = self._spender().fund_model(name, _sig(amount))
        self.log(f"modelo '{name}' recarregado com {sig_txt(amount)}")
        return self._done(txid)

    def api_model_withdraw(self, d):
        name, amount = self._model_name(d.get("name")), micro(d.get("amount"), "valor")
        txid = self._spender().withdraw_model(name, _sig(amount))
        self.log(f"saquei {sig_txt(amount)} do orçamento do modelo '{name}'")
        return self._done(txid)

    # ------------------------------------------------- chat global, nomes e máquinas (v9)
    def api_net_chat(self, q):
        after = (q.get("after") or ["0"])[0]
        return Client(self.settings["node"]).chat_feed(int(after) if str(after).isdigit() else 0)

    def api_net_chat_post(self, d):
        text = d.get("text")
        if not isinstance(text, str) or not text.strip():
            raise AppError("escreva a mensagem")
        if len(text.encode("utf-8")) > chain.CHAT_MAX_BYTES:
            raise AppError(f"mensagem grande demais (até {chain.CHAT_MAX_BYTES // 4:,} caracteres)".replace(",", "."))
        return self._done(self._spender().chat_post(text.strip()))

    def api_set_name(self, d):
        name = d.get("name")
        if not isinstance(name, str):
            raise AppError("nome inválido")
        return self._done(self._spender().set_name(name))

    def api_net_machines(self, q):
        """Máquinas da rede para o pop-up "Máquinas online": nome, dono (nome público), o que servem e o hardware."""
        c = Client(self.settings["node"])
        machines, names, mining = c.machines(), c.get("/names"), c.mining()
        miners = (mining or {}).get("miners") or {}
        sites = self._read("/sites") if hasattr(self, "_read") else {}
        hosting = {}
        for name, s in (sites or {}).items():
            for a in (s.get("pinners") or {}) if isinstance(s, dict) else ():
                hosting.setdefault(a, []).append(name)
        out = []
        for a, m in (machines or {}).items():
            if not isinstance(m, dict):
                continue
            out.append({"address": a, "name": m.get("name"), "online": bool(m.get("online")),
                        "owner": m.get("payout"), "owner_name": (names or {}).get(m.get("payout")),
                        "models": list(m.get("models") or []) + list(m.get("media_models") or []),
                        "sites": hosting.get(a, []), "hw": m.get("hw") or {}, "reputation": m.get("reputation"),
                        "tasks": m.get("tasks"), "lend": m.get("lend"), "compute": m.get("compute"),
                        "jobs_done": m.get("jobs_done"), "mining": a in miners or m.get("payout") in miners})
        out.sort(key=lambda x: (not x["online"], -(x["reputation"] or 0), str(x["name"])))
        return {"machines": out}

    def api_model_update(self, d):
        name = self._model_name(d.get("name"))
        def pct(key, top):   # ausente = não muda
            return None if _opt(d, key, None) is None else Decimal(_pct(d[key], key, top)) / 100
        royalty, reinvest = pct("royalty", 50), pct("reinvest", 100)
        sponsor = typed_address(d["new_sponsor"]) if d.get("new_sponsor") else None
        if royalty is None and reinvest is None and sponsor is None:
            raise AppError("nada para mudar: informe royalty, reinvest ou new_sponsor")
        txid = self._spender().update_model(name, royalty, reinvest, sponsor)
        self.log(f"modelo '{name}' atualizado" + (f": agora o padrinho é {sponsor}" if sponsor else ""))
        return self._done(txid)

    def api_model_retire(self, d):
        name = self._model_name(d.get("name"))
        txid = self._spender().retire_model(name)
        self.log(f"modelo '{name}' apagado da rede; o budget que sobrou voltou para você")
        return self._done(txid)

    @staticmethod
    def _chat_input(d):
        """Pergunta vinda do painel -> (modelo, mensagens, max_tokens); recusa explicando o que está errado."""
        model = d.get("model")
        if not isinstance(model, str) or not model:
            raise AppError("escolha o modelo")
        msgs = d.get("messages")
        # uma conversa guardada (até MAX_CHAT_MESSAGES) mais a pergunta nova; o fit_messages corta pelo contexto
        if not isinstance(msgs, list) or not 1 <= len(msgs) <= MAX_CHAT_MESSAGES + 1:
            raise AppError(f"messages deve ser uma lista com 1 a {MAX_CHAT_MESSAGES + 1} mensagens")
        clean = []
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant") \
                    or not isinstance(m.get("content"), str):
                raise AppError("cada mensagem precisa de role (system, user ou assistant) e content (texto)")
            clean.append({"role": m["role"], "content": m["content"]})
        if sum(len(m["content"]) for m in clean) > 200_000:
            raise AppError("conversa longa demais: comece uma nova")
        # metade do contexto fica para a conversa (o client.ask corta no mesmo ponto)
        return model, clean, min(_int(_opt(d, "max_tokens", 512), "max_tokens", 1, 8192), AI_CONTEXT // 2)

    @staticmethod
    def _chat_id(d):
        cid = d.get("id")
        if cid is not None and not (isinstance(cid, str) and CHAT_ID.fullmatch(cid)):
            raise AppError("id do pedido inválido: use de 1 a 64 letras, números, - ou _")
        return cid

    def api_ai_quote(self, d):
        """Custo máximo da próxima pergunta, para o painel mostrar antes de enviar. Usa o mesmo teto e a mesma
        conta do client.ask: reserva = (tokens da conversa + resposta) × teto / 1000; o que sobrar volta.
        Imagem/vídeo: unidades (imagens ou segundos) × teto por unidade (client.generate)."""
        from .client import fit_messages
        task = self._model_tasks().get(d.get("model"), "text") if isinstance(d.get("model"), str) else "text"
        if task != "text":
            model, spec, units = self._media_input(d, task)
            price = self._ai_prices().get(model)
            return {"model": model, "task": task, "price": price, "unit": UNITS[task],
                    "max_cost": units * price if price else None}
        model, clean, max_tokens = self._chat_input(d)
        msgs, trimmed = fit_messages(clean, max_tokens)   # longa demais até para a última mensagem: 400 com o porquê
        price = self._ai_prices().get(model)
        tokens = sum(len(m["content"]) // 3 + 8 for m in msgs) + 16
        return {"model": model, "task": "text", "price": price, "unit": UNITS["text"], "max_tokens": max_tokens,
                "trimmed": trimmed, "max_cost": max(1, -(-(tokens + max_tokens) * price // 1000)) if price else None}

    def _media_input(self, d, task):
        """Pedido de imagem/vídeo vindo do painel -> (modelo, spec, unidades). A receita (passos, escala e o tamanho
        padrão) vem do catálogo quando o modelo é um dos dele (mesmo arquivo); o resto do pedido são valores de
        listas fechadas."""
        model = d.get("model")
        if not isinstance(model, str) or not catalog.MODEL_NAME.match(model):
            raise AppError("escolha o modelo")
        prompt = d.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise AppError("descreva o que você quer " + ("na imagem" if task == "image" else "no vídeo"))
        if len(prompt) > 2000:
            raise AppError("a descrição passa de 2.000 caracteres")
        lim = MEDIA_LIMITS[task]
        net = (self._read("/models") or {}).get(model) or {}
        recipe = next((i.get("recipe") or {} for i in catalog.CATALOG
                       if i.get("task") == task and i["sha256"] == net.get("sha256")), {})
        # sem tamanho no pedido vale o da receita (o SD 1.5 foi treinado em 512x512); o escolhido vale mais que ela
        size = _opt(d, "size", recipe["size"] if recipe.get("size") in lim["sizes"] else lim["sizes"][0])
        if size not in lim["sizes"]:
            raise AppError("formato inválido: escolha um de " + ", ".join(lim["sizes"]))
        spec = {"prompt": prompt.strip(), "size": size}
        spec.update({k: v for k, v in recipe.items() if k in ("steps", "cfg", "sampler", "flow_shift")})
        if task == "image":
            units = _int(_opt(d, "count", 1), "quantidade", 1, lim["max_count"])
            spec["count"] = units
        else:
            units = _opt(d, "seconds", lim["seconds"][0])
            if units not in lim["seconds"]:
                raise AppError("duração inválida: escolha " + ", ".join(f"{x} s" for x in lim["seconds"]))
            spec["seconds"] = units
        return model, spec, units

    def media_path(self, h, mime=None):
        """Arquivo de uma imagem/vídeo gerado (fica na pasta de dados, para as conversas guardadas)."""
        if mime:
            return os.path.join(self.data_dir, "midia", h + MEDIA_EXT[mime])
        for ext in MEDIA_EXT.values():
            p = os.path.join(self.data_dir, "midia", h + ext)
            if os.path.isfile(p):
                return p
        return None

    def _keep_media(self, items):
        out = []
        os.makedirs(os.path.join(self.data_dir, "midia"), exist_ok=True)
        for it in items:
            h = hashlib.sha256(it["data"]).hexdigest()
            path = self.media_path(h, it["mime"])
            if not os.path.isfile(path):
                with open(path + ".tmp", "wb") as f:
                    f.write(it["data"])
                os.replace(path + ".tmp", path)
            out.append(dict({k: it[k] for k in ("width", "height", "seconds") if k in it}, hash=h, mime=it["mime"]))
        return out

    def _api_media(self, d, task):
        if not self._net_media():
            raise AppError(self._no_media_yet())
        model, spec, units = self._media_input(d, task)
        if self._model_tasks().get(model) != task:
            raise AppError(f"o modelo {model} não gera " + ("imagens" if task == "image" else "vídeos"))
        cid = self._chat_id(d)
        max_price = _opt(d, "max_price", None)
        max_price = None if max_price is None else _int(max_price, "max_price", 1, MAX_MEDIA_PRICE)
        client = self._spender()
        ctx = {"stop": threading.Event(), "job": None, "id": cid}
        with self.chat_lock:
            if cid is not None and self.canceled_ids.pop(cid, None) is not None:
                raise AppError(CANCELED, code="canceled")
            self.chats.append(ctx)
        try:
            items, job = client.generate(model, task, spec, max_price=max_price, should_stop=ctx["stop"].is_set,
                                         on_job=lambda jid: ctx.update(job=jid),
                                         on_progress=lambda info: ctx.update(progress=info))
        except SigmaError as e:
            if ctx["stop"].is_set() and "cancelado" in str(e).lower():
                raise AppError(CANCELED, code="canceled") from None
            raise
        finally:
            with self.chat_lock:
                self.chats.remove(ctx)
        self.wake.set()
        media_out = self._keep_media(items)

        def num(k):
            v = job.get(k)
            return v if isinstance(v, int) and not isinstance(v, bool) else None
        return {"task": task, "media": media_out, "cost": num("cost") or 0, "machine": job.get("machine"),
                "review": job.get("status") == "review",
                "attempts": num("attempts"), "job": job.get("id"), "max_cost": num("escrow"),
                "price": num("max_price")}

    def api_image(self, d):
        return self._api_media(d, "image")

    def api_video(self, d):
        return self._api_media(d, "video")

    # ------------------------------------------------------------- conversas
    def _chats_dir(self):
        return os.path.join(self.data_dir, "conversas")

    def _chat_path(self, cid):
        if not isinstance(cid, str) or not CHAT_FILE_ID.match(cid):
            raise AppError("conversa inválida")
        return os.path.join(self._chats_dir(), cid + ".json")

    def _load_chat(self, cid):
        try:
            with open(self._chat_path(cid), encoding="utf-8") as f:
                c = json.load(f)
        except (OSError, ValueError):
            raise AppError("essa conversa não existe mais") from None
        return c if isinstance(c, dict) else {}

    def api_chats(self, q):
        out = []
        try:
            names = os.listdir(self._chats_dir())
        except OSError:
            names = []
        for n in names:
            if not n.endswith(".json") or not CHAT_FILE_ID.match(n[:-5]):
                continue
            try:
                c = self._load_chat(n[:-5])
            except AppError:
                continue
            out.append({"id": n[:-5], "title": str(c.get("title") or "")[:120], "task": c.get("task", "text"),
                        "model": str(c.get("model") or ""), "updated": c.get("updated") or 0,
                        "count": len(c.get("messages") or [])})
        out.sort(key=lambda c: -float(c["updated"] or 0))
        return {"chats": out}

    def api_chat_get(self, cid):
        c = self._load_chat(cid)
        c["id"] = cid
        return c

    @staticmethod
    def _clean_messages(msgs):
        if not isinstance(msgs, list) or len(msgs) > MAX_CHAT_MESSAGES:
            raise AppError(f"messages deve ser uma lista de até {MAX_CHAT_MESSAGES} mensagens")
        clean, chars = [], 0
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") \
                    or not isinstance(m.get("content", ""), str):
                raise AppError("cada mensagem precisa de role (user ou assistant) e content (texto)")
            c = {"role": m["role"], "content": m.get("content", "")}
            chars += len(c["content"])
            media_ = m.get("media")
            if media_ is not None:
                if not isinstance(media_, list) or len(media_) > 4:
                    raise AppError("media deve ser uma lista de até 4 itens")
                c["media"] = []
                for it in media_:
                    if not isinstance(it, dict) or not HASH_RE.match(str(it.get("hash"))) \
                            or it.get("mime") not in MEDIA_EXT:
                        raise AppError("mídia inválida na conversa")
                    c["media"].append({"hash": it["hash"], "mime": it["mime"], **{
                        k: it[k] for k in ("width", "height", "seconds")
                        if isinstance(it.get(k), int) and not isinstance(it.get(k), bool) and 0 < it[k] <= 100_000}})
            for k in ("cost", "time"):
                if isinstance(m.get(k), (int, float)) and not isinstance(m.get(k), bool) and m[k] >= 0:
                    c[k] = m[k]
            for k, top in (("machine", 64), ("error", 500), ("model", 48)):
                if isinstance(m.get(k), str):
                    c[k] = m[k][:top]
            clean.append(c)
        if chars > MAX_CHAT_CHARS:
            raise AppError("conversa longa demais para guardar: comece uma nova")
        return clean

    def api_chat_save(self, d):
        cid = d.get("id") or "c" + secrets.token_hex(6)
        path = self._chat_path(cid)
        task = d.get("task", "text")
        if task not in TASKS:
            raise AppError("task deve ser text, image ou video")
        model = d.get("model") or ""
        if model and (not isinstance(model, str) or not catalog.MODEL_NAME.match(model)):
            raise AppError("modelo inválido")
        msgs = self._clean_messages(d.get("messages"))
        if d.get("id") and not os.path.isfile(path):
            # apagada depois que o painel a abriu (a resposta chegou depois do Apagar, ou outra janela apagou): ela não
            # volta para a lista, citando imagens e vídeos que já saíram do disco
            raise AppError("essa conversa foi apagada: para continuar, clique em Nova conversa")
        old = self._load_chat(cid) if os.path.isfile(path) else {}
        title = d.get("title")
        if not isinstance(title, str) or not title.strip():
            title = old.get("title") or next((m["content"] for m in msgs if m["role"] == "user" and m["content"]),
                                             "Conversa nova")
        title = " ".join(str(title).split())[:60] or "Conversa nova"
        now = time.time()
        c = {"title": title, "task": task, "model": model, "created": old.get("created") or now, "updated": now,
             "messages": msgs}
        os.makedirs(self._chats_dir(), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
        # 'Tentar de novo' troca a resposta: a imagem ou o vídeo que saiu da conversa sai também do disco
        self._drop_media(_media_of(old.get("messages")) - _media_of(msgs))
        self._trim_chats()
        return {"id": cid, "title": title, "updated": now}

    def _trim_chats(self):
        chats = self.api_chats({})["chats"]
        for c in chats[MAX_CHATS:]:
            self._delete_chat(c["id"])

    def _delete_chat(self, cid):
        path = self._chat_path(cid)
        try:
            gone = _media_of(self._load_chat(cid).get("messages"))
        except AppError:
            gone = set()
        try:
            os.remove(path)
        except OSError:
            pass
        self._drop_media(gone)   # as imagens e vídeos que só essa conversa usava saem do disco

    def _media_in_use(self):
        """Hashes das imagens e vídeos que as conversas guardadas citam, ou None se alguma delas não deu para ler
        agora (na dúvida, nada sai do disco). Uma estragada não cita nada: ela nem aparece no painel."""
        folder = self._chats_dir()
        try:
            names = [n for n in os.listdir(folder) if n.endswith(".json") and CHAT_FILE_ID.match(n[:-5])]
        except FileNotFoundError:
            return set()
        except OSError:
            return None
        used = set()
        for n in names:
            try:
                with open(os.path.join(folder, n), encoding="utf-8") as f:
                    c = json.load(f)
            except FileNotFoundError:   # apagada agora há pouco
                continue
            except OSError:             # presa por outro programa (antivírus?)
                return None
            except ValueError:          # estragada
                continue
            used |= _media_of(c.get("messages") if isinstance(c, dict) else None)
        return used

    def _drop_media(self, gone):
        """Tira do disco as mídias de `gone` que nenhuma conversa guardada cita mais."""
        used = self._media_in_use() if gone else None
        if used is None:
            return
        for h in gone - used:
            p = self.media_path(h)
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _media_gc(self):
        """Na abertura: tira do disco as mídias que nenhuma conversa cita (o resultado chegou com o painel fechado
        e não foi guardado), menos as que chegaram há pouco (MEDIA_GRACE), que o painel ainda vai guardar."""
        folder = os.path.join(self.data_dir, "midia")
        try:
            names = os.listdir(folder)
        except OSError:
            return
        used = self._media_in_use() if names else None
        if used is None:
            return
        old = time.time() - MEDIA_GRACE
        for n in names:
            m, p = MEDIA_FILE.match(n), os.path.join(folder, n)
            try:
                if m and m.group(1) not in used and os.path.getmtime(p) < old:
                    os.remove(p)
            except OSError:
                pass

    def api_chat_delete(self, d):
        self._chat_path(d.get("id"))
        self._delete_chat(d["id"])
        return {"ok": True}

    def api_chat_rename(self, d):
        cid, title = d.get("id"), d.get("title")
        if not isinstance(title, str) or not title.strip():
            raise AppError("escreva o nome da conversa")
        c = self._load_chat(cid)
        c["title"] = " ".join(title.split())[:60]
        with open(self._chat_path(cid) + ".tmp", "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False)
        os.replace(self._chat_path(cid) + ".tmp", self._chat_path(cid))
        return {"ok": True}

    def local(self):
        if getattr(self, "_local", None) is None:
            from .localai import LocalAI
            self._local = LocalAI(self.data_dir, self.log)
        return self._local

    def api_local(self, q):
        loc = self.local()
        return {"models": loc.models(), "lowmem": loc.lowmem}

    def api_local_download(self, d):
        try:
            self.local().download(d.get("catalog_id"))
        except ValueError as e:
            raise AppError(str(e)) from None
        return {"ok": True}

    def api_local_settings(self, d):
        self.local().set_lowmem(d.get("lowmem") is not False)
        return {"lowmem": self.local().lowmem}

    def api_chat(self, d):
        if d.get("local") is True:   # modo local: responde o modelo baixado neste PC (sem rede, sem custo)
            model, clean, max_tokens = self._chat_input(d)
            try:
                text, job = self.local().ask(model, clean, max_tokens=max_tokens)
            except ValueError as e:
                raise AppError(str(e)) from None
            except Exception as e:
                raise AppError(f"o modelo local falhou: {e}") from None
            return {"text": text, "cost": 0, "machine": "este computador", "trimmed": 0, "attempts": 1, "job": None,
                    "max_cost": 0, "price": 0, "sources": []}
        model, clean, max_tokens = self._chat_input(d)
        cid = self._chat_id(d)
        sources = []
        if d.get("web") is True:   # internet: este app busca e lê as páginas; o texto vai junto para a IA da rede
            from . import websearch
            question = next((m["content"] for m in reversed(clean) if m.get("role") == "user"), "")
            try:
                found, sources = websearch.context(question)
            except Exception as e:
                raise AppError(f"não consegui pesquisar na internet agora ({type(e).__name__}); tente de novo ou "
                               "desligue a pesquisa") from None
            if found:
                note = {"role": "system", "content": (
                    "Resultados da internet buscados agora para a pergunta do usuário (podem estar incompletos). "
                    "Use-os quando ajudarem, diga quando não tiver certeza e cite as fontes pelo número [n].\n\n"
                    + found)}
                clean = clean[:-1] + [note] + clean[-1:]
        # o teto que o painel mostrou como "custo máximo": a pergunta nunca reserva mais do que isso
        max_price = _opt(d, "max_price", None)
        max_price = None if max_price is None else _int(max_price, "max_price", 1, MAX_AI_PRICE)
        client = self._spender()
        ctx = {"stop": threading.Event(), "job": None, "id": cid}
        with self.chat_lock:
            if cid is not None and self.canceled_ids.pop(cid, None) is not None:
                raise AppError(CANCELED, code="canceled")   # cancelada antes de sair: nada foi reservado
            self.chats.append(ctx)
        try:
            text, job = client.ask(model, clean, max_tokens=max_tokens, max_price=max_price,
                                   should_stop=ctx["stop"].is_set, on_job=lambda jid: ctx.update(job=jid),
                                   on_progress=lambda info: ctx.update(progress=info))
        except SigmaError as e:
            if ctx["stop"].is_set() and "cancelado" in str(e).lower():
                raise AppError(CANCELED, code="canceled") from None
            raise
        finally:
            with self.chat_lock:
                self.chats.remove(ctx)
        self.wake.set()

        def num(k):
            v = job.get(k)
            return v if isinstance(v, int) and not isinstance(v, bool) else None
        return {"text": text, "cost": num("cost") or 0, "machine": job.get("machine"), "trimmed": num("trimmed") or 0,
                "sources": [{"title": x["title"], "url": x["url"]} for x in sources],
                "review": job.get("status") == "review",
                "attempts": num("attempts"), "job": job.get("id"),
                "max_cost": num("escrow"), "price": num("max_price")}   # o que ficou reservado e o teto por mil tokens

    def api_ai_progress(self, q):
        """GET /api/ai/progress?id=<id do chat>: a última notícia do computador que está respondendo (texto até
        agora, prévia da imagem, porcentagem, fase) e há quantos segundos ela chegou. {"progress": null} se nada."""
        cid = (q.get("id") or [None])[0]
        with self.chat_lock:
            ctx = next((c for c in self.chats if c["id"] == cid), None) if cid else None
            p = dict(ctx.get("progress") or {}) if ctx else None
        if not p:
            return {"progress": None, "waiting": bool(ctx)}
        keep = ("stage", "pct", "step", "steps", "eta", "text", "chars", "preview", "age", "machine", "status",
                "claimed", "job")
        return {"progress": {k: p[k] for k in keep if k in p}, "waiting": True}

    def api_ai_dispute(self, d):
        """POST /api/ai/dispute {"job"}: contesta uma entrega em revisão (regras v7). Outra vigia confere."""
        jid = d.get("job")
        if not (isinstance(jid, str) and re.fullmatch(r"[0-9a-f]{64}", jid)):
            raise AppError("pedido inválido")
        job = self._spender().dispute(jid)
        return {"ok": True, "watcher": (job.get("review") or {}).get("watcher")}

    def api_ai_cancel(self, d):
        """Botão Cancelar do chat: se nenhum computador pegou o pedido, ele é cancelado e o valor volta.
        Com "id", só a pergunta com esse id; sem, todas as que estão esperando."""
        cid = self._chat_id(d)
        with self.chat_lock:
            chats = [c for c in self.chats if cid is None or c["id"] == cid]
            if cid is not None and not chats:   # a pergunta ainda não chegou aqui: ela já nasce cancelada
                now = time.time()
                self.canceled_ids = {k: t for k, t in self.canceled_ids.items() if now - t < CANCEL_MEMORY}
                if len(self.canceled_ids) < 1000:
                    self.canceled_ids[cid] = now
        claimed = False
        for ctx in chats:
            ctx["stop"].set()
            if ctx["job"]:
                try:
                    j = Client(self.settings["node"]).job(ctx["job"])
                    claimed = claimed or bool(j.get("claims")) or j.get("status") == "running"
                except (SigmaError, ValueError, OSError):
                    pass
        return {"ok": True, "running": bool(chats), "claimed": claimed, "message": CLAIMED if claimed else None}

    def api_sites(self, q):
        addr, node = self._payout(), self.settings["node"]
        sites = self._read("/sites") if addr else {}
        mine = [dict(s, url=f"{node}/site/{s.get('name')}/") for s in (sites or {}).values()
                if isinstance(s, dict) and s.get("owner") == addr]
        return {"mine": mine, "hosted": self._stats(self.host).get("sites", [])}

    @staticmethod
    def _site_name(v):
        if not isinstance(v, str) or not SITE_NAME.match(v):
            raise AppError("nome do site: 3 a 32 caracteres, letras minúsculas, números e hífen")
        return v

    def api_publish(self, d):
        name = self._site_name(d.get("name"))
        budget = micro(_opt(d, "budget", 0), "budget", allow_zero=True)
        replicas = _int(_opt(d, "replicas", 2), "replicas", 1, 5)
        files = site_files(d.get("files"))
        c = self._spender()
        manifest = c.deploy_site_files(files, name, _sig(budget), replicas)
        url = f"{self.settings['node']}/site/{name}/"
        self.log(f"site '{name}' publicado ({len(manifest)} arquivos): {url}")
        return dict(self._done(c.last_txid), url=url)

    def _gone(self, site):
        """Expirado há mais de 7 dias: o nó já apagou os arquivos, então recarregar ressuscitaria um site vazio."""
        ew = site.get("expired_window")
        if site.get("status") != "expired" or not isinstance(ew, int) or isinstance(ew, bool):
            return False

        def window(mi):
            w, ws = mi.get("window"), mi.get("window_seconds")
            ok = isinstance(w, int) and isinstance(ws, (int, float)) and not isinstance(w, bool) and ws > 0
            return (w, float(ws)) if ok else None
        now = window(self.cache.get("mining") or {}) or window(self._read("/mining") or {})
        return bool(now) and now[0] - ew >= math.ceil(SITE_GRACE / now[1])

    def api_site_fund(self, d):
        name, amount = self._site_name(d.get("name")), micro(d.get("amount"), "valor")
        site = (self._read("/sites") or {}).get(name)
        if not site:
            raise AppError(f"o site '{name}' não existe")
        if site.get("owner") != self._payout():
            raise AppError("este site pertence a outra carteira")
        if self._gone(site):
            raise AppError("O site ficou fora do ar por muito tempo e os arquivos saíram da rede: publique a pasta "
                           "de novo.")
        txid = self._spender().fund_site(name, _sig(amount))   # mesmo manifest + budget: a versão não muda
        self.log(f"site '{name}' recarregado com {sig_txt(amount)}")
        return self._done(txid)

    def api_code_state(self, q):
        return self.code.state()

    def api_code_start(self, d):
        return self.code.start(d)

    def api_code_answer(self, d):
        return self.code.answer(d)

    def api_code_stop(self, d):
        return self.code.stop()

    def api_code_continue(self, d):
        return self.code.follow_up(d)

    def api_code_reset(self, d):
        return self.code.reset()

    def api_code_pick_folder(self, d):
        folder = self.ask_ui(_pick_folder)
        return {"folder": os.path.normpath(folder) if folder else ""}

    def api_quit(self, d):
        self.log("desligamento pedido pelo painel")
        threading.Thread(target=lambda: (time.sleep(0.3), self.shutdown()), daemon=True).start()
        return {"ok": True}

    # ---------------------------------------------------------------- fim
    def shutdown(self):
        with self.shut_lock:
            first, self._shut = not self._shut, True
        if not first:
            self.done.wait(60)
            return
        self.log("desligando o Sigma…")
        self.stopping.set()
        self.wake.set()
        self.code.stop()   # libera a thread do agente se ela estiver esperando uma aprovação
        with self.chat_lock:
            for ctx in self.chats:
                ctx["stop"].set()
        self.stop_services()
        if self.httpd:
            if self.http_thread and self.http_thread.is_alive():
                self.httpd.shutdown()
            self.httpd.server_close()
        try:
            if (_read_json(self.session_path) or {}).get("token") == self.token:
                os.remove(self.session_path)
        except OSError:
            pass
        try:
            os.remove(self.opener_path)   # o abrir.html leva o token: não fica para trás
        except OSError:
            pass
        self.log("Sigma desligado. Até a próxima!")
        self.release_lock()
        self.done.set()

    def wait_console(self):
        """Modo console: segue até Ctrl+C, /api/quit ou SIGTERM; mostra o status a cada minuto."""
        last = time.time()
        try:
            while not self.done.wait(1):
                if time.time() - last >= 60:
                    last = time.time()
                    self._echo(f"{time.strftime('%H:%M:%S')} {self.status_line()}")
                    if self.update_line():
                        self._echo(self.update_line())
        except KeyboardInterrupt:
            pass
        self.shutdown()


# API local do painel (contrato com sigma/web/app.html). Toda rota exige X-Sigma-Token e Host
# 127.0.0.1/localhost:<porta> (menos /api/ping); Origin, se vier, tem de ser a do painel. Valores em µSIG
# inteiros na saída e SIG decimal na entrada. Erro: HTTP 400 {"error": frase em português, "code"?}
# ("code": "canceled" quando a pergunta foi cancelada).
#   GET  /api/ping                -> {"app": "sigma", "version", "instance"}                    (sem token)
#   GET  /api/state               -> App.state() (campos documentados lá)
#   POST /api/setup               {"mode": "create"} -> {"address", "private_key"} (mostrar uma vez)
#                                 {"mode": "import_key", "private_key", "expect_address"?} -> {"address"}
#                                 {"mode": "address", "address"} -> {"address"}   (endereço confere o EIP-55)
#   POST /api/settings            {cpu_percent?, mining_enabled?, ai_enabled?, compute_enabled?, disk_gb?,
#                                  ai_ram_gb?, node?, start_with_system?, mine_on_battery?, name?, public_url?,
#                                  engine?, ai_threads?} -> {"settings"}   (GB decimais, 2^30 bytes)
#   POST /api/send                {"to", "amount"} -> {"txid"}
#   GET  /api/wallet/export?confirm=1 -> {"private_key"}
#   GET  /api/models              -> {"network": /models do nó, "catalog": [...],
#                                     "ai": {"prices": {modelo: µSIG por mil tokens}, "context": 4096}}
#   POST /api/models/sponsor      {"catalog_id"|"url", "name"?, "budget", "royalty"?, "reinvest"?, "replicas"?}
#                                 -> {"txid", "name"}
#   POST /api/models/fund         {"name", "amount"} -> {"txid"}
#   POST /api/models/update       {"name", "royalty"?, "reinvest"?, "new_sponsor"?} -> {"txid"}
#   POST /api/models/retire       {"name"} -> {"txid"}
#   POST /api/ai/quote            {"model", "messages", "max_tokens"?} -> {"model", "price", "max_cost",
#                                  "max_tokens", "trimmed"}   (price/max_cost null = nenhuma máquina aceita)
#   POST /api/ai/chat             {"model", "messages", "max_tokens"?, "max_price"?, "id"?} -> {"text", "cost",
#                                  "machine", "trimmed", "attempts", "job", "max_cost", "price"}
#   POST /api/ai/cancel           {"id"?} -> {"ok", "running", "claimed", "message"}
#   POST /api/ai/image            {"model", "prompt", "size", "count", "max_price"?, "id"?} -> {"task", "media":
#                                  [{"hash", "mime", "width", "height"}], "cost", "machine", "attempts", "job",
#                                  "max_cost", "price"}   (regras v2; "price" é µSIG por imagem)
#   POST /api/ai/video            {"model", "prompt", "size", "seconds", "max_price"?, "id"?} -> o mesmo, com
#                                  "seconds" em cada mídia ("price" é µSIG por segundo de vídeo)
#   GET  /api/media/<hash>        -> os bytes da imagem (image/png) ou do vídeo (video/webm) gerado
#   GET  /api/chats               -> {"chats": [{"id", "title", "task", "model", "updated", "count"}]}
#   GET  /api/chats/<id>          -> {"id", "title", "task", "model", "created", "updated", "messages"}
#   POST /api/chats/save          {"id"?, "title"?, "task", "model", "messages"} -> {"id", "title", "updated"}
#   POST /api/chats/delete        {"id"} -> {"ok"};  POST /api/chats/rename {"id", "title"} -> {"ok"}
#   GET  /api/code/state          -> CodeRunner.state(): {"running", "folder", "model", "task", "auto_files",
#                                    "steps": [{"n", "kind", "text", "tool"?, "args"?, "output"?, "cost"?,
#                                    "machine"?, "refused"?}], "pending": {"id", "tool", "args", "reason",
#                                    "preview", "changed"} | null, "spent", "final", "error"}
#   POST /api/code/start          {"folder", "task", "model"?, "auto_files"?} -> /api/code/state
#   POST /api/code/answer         {"id", "ok"} -> /api/code/state
#   POST /api/code/stop           {} -> /api/code/state
#   POST /api/code/continue       {"text"} -> /api/code/state   (próxima instrução da mesma sessão)
#   POST /api/code/reset          {} -> {"ok"}                  (Nova sessão)
#   POST /api/code/pick-folder    {} -> {"folder": caminho ou ""}   (sem janelinha: 400 "digite o caminho da pasta")
#   GET  /api/sites               -> {"mine": [site + "url"], "hosted": [nomes]}
#   POST /api/sites/publish       {"name", "budget", "replicas"?, "files": [{"path", "data_b64"}]} -> {"txid", "url"}
#   POST /api/sites/fund          {"name", "amount"} -> {"txid"}
#   GET  /api/governance          -> /governance do nó + {"me", "can_spend"}
#   POST /api/governance/propose  {"title", "text"?, "param"?, "value"?} -> {"txid"}
#   POST /api/governance/vote     {"proposal_id", "choice": "sim"|"nao", "amount"} -> {"txid"}
#   POST /api/quit                {} -> {"ok": true}
ROUTES = {
    ("GET", "/api/state"): App.api_state,
    ("POST", "/api/setup"): App.api_setup,
    ("POST", "/api/settings"): App.api_settings,
    ("POST", "/api/send"): App.api_send,
    ("GET", "/api/wallet/export"): App.api_export,
    ("GET", "/api/models"): App.api_models,
    ("POST", "/api/models/sponsor"): App.api_sponsor,
    ("POST", "/api/models/fund"): App.api_model_fund,
    ("POST", "/api/models/update"): App.api_model_update,
    ("POST", "/api/models/retire"): App.api_model_retire,
    ("POST", "/api/models/withdraw"): App.api_model_withdraw,
    ("GET", "/api/net/chat"): App.api_net_chat,
    ("GET", "/api/local"): App.api_local,
    ("POST", "/api/local/download"): App.api_local_download,
    ("POST", "/api/local/settings"): App.api_local_settings,
    ("POST", "/api/net/chat"): App.api_net_chat_post,
    ("POST", "/api/name"): App.api_set_name,
    ("GET", "/api/net/machines"): App.api_net_machines,
    ("POST", "/api/ai/quote"): App.api_ai_quote,
    ("POST", "/api/ai/chat"): App.api_chat,
    ("POST", "/api/ai/cancel"): App.api_ai_cancel,
    ("GET", "/api/ai/progress"): App.api_ai_progress,
    ("POST", "/api/ai/dispute"): App.api_ai_dispute,
    ("POST", "/api/ai/image"): App.api_image,
    ("POST", "/api/ai/video"): App.api_video,
    ("GET", "/api/chats"): App.api_chats,
    ("POST", "/api/chats/save"): App.api_chat_save,
    ("POST", "/api/chats/delete"): App.api_chat_delete,
    ("POST", "/api/chats/rename"): App.api_chat_rename,
    ("GET", "/api/code/state"): App.api_code_state,
    ("POST", "/api/code/start"): App.api_code_start,
    ("POST", "/api/code/answer"): App.api_code_answer,
    ("POST", "/api/code/stop"): App.api_code_stop,
    ("POST", "/api/code/continue"): App.api_code_continue,
    ("POST", "/api/code/reset"): App.api_code_reset,
    ("POST", "/api/code/pick-folder"): App.api_code_pick_folder,
    ("GET", "/api/sites"): App.api_sites,
    ("POST", "/api/sites/publish"): App.api_publish,
    ("POST", "/api/sites/fund"): App.api_site_fund,
    ("GET", "/api/governance"): App.api_governance,
    ("POST", "/api/governance/propose"): App.api_propose,
    ("POST", "/api/governance/vote"): App.api_vote,
    ("POST", "/api/quit"): App.api_quit,
}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Sigma/" + VERSION
        sys_version = ""
        timeout = 120   # conexão parada não prende uma thread para sempre

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", csp=API_CSP):
            if not isinstance(body, bytes):
                # ensure_ascii=True: um argumento ruim da IA (ex.: surrogate solto) vira \uXXXX e nunca derruba
                # a resposta (sem isto, /api/code/state quebraria ao serializar a prévia/args do cartão)
                body = json.dumps(body, ensure_ascii=True).encode()
            self.send_response(code)
            for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                         ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
                         ("Referrer-Policy", "no-referrer"), ("Content-Security-Policy", csp)):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self, need_token):
            # Host errado = DNS rebinding; Origin estranha = outro site tentando falar com o painel
            if self.headers.get("Host", "") not in app.allowed_hosts:
                self._send(403, {"error": "acesso negado: o painel só atende em 127.0.0.1 ou localhost"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in app.allowed_origins:
                self._send(403, {"error": "acesso negado: origem não permitida"})
                return False
            given = self.headers.get("X-Sigma-Token", "")
            if need_token and not hmac.compare_digest(given.encode("utf-8", "replace"), app.token.encode()):
                self._send(401, {"error": "token ausente ou inválido: abra o painel pelo aplicativo Sigma"})
                return False
            return True

        def _json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise AppError("pedido grande demais")
            try:
                d = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                raise AppError("JSON inválido") from None
            if not isinstance(d, dict):
                raise AppError("o corpo deve ser um objeto JSON")
            return d

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method):
            u = urlparse(self.path)
            if not u.path.startswith("/api/"):
                if self._allowed(False):
                    if method == "GET" and u.path in ("/", "/index.html", "/app.html"):
                        return self._send(200, app.page(), "text/html; charset=utf-8", csp=PAGE_CSP)
                    self._send(404, {"error": "não encontrado"})
                return
            if u.path == "/api/ping":
                if self._allowed(False):
                    info = {"app": "sigma", "version": VERSION, "instance": app.instance}
                    nonce = (parse_qs(u.query).get("nonce") or [""])[0][:128]
                    if nonce:   # prova (HMAC) de que conhecemos o token desta pasta, para a troca de instância
                        info["auth"] = _ping_auth(app.token, nonce)
                    self._send(200, info)
                return
            if not self._allowed(True):
                return
            if method == "GET" and u.path.startswith("/api/media/"):   # bytes, não JSON
                h = u.path[len("/api/media/"):]
                path = app.media_path(h) if HASH_RE.match(h) else None
                if not path:
                    return self._send(404, {"error": "não encontrado"})
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:   # saiu agora há pouco (a conversa foi guardada sem ela)
                    return self._send(404, {"error": "não encontrado"})
                return self._send(200, data, "image/png" if path.endswith(".png") else "video/webm")
            m = re.fullmatch(r"/api/chats/(c[0-9a-f]{12})", u.path) if method == "GET" else None
            fn = (lambda a, q, cid=m.group(1) if m else None: a.api_chat_get(cid)) if m \
                else ROUTES.get((method, u.path))
            if fn is None:
                known = any(p == u.path for _, p in ROUTES)
                return self._send(405 if known else 404, {"error": "método não permitido" if known
                                                          else "rota desconhecida"})
            try:
                arg = parse_qs(u.query) if method == "GET" else self._json()
                return self._send(200, fn(app, arg))
            except (AppError, SigmaError, ValueError) as e:
                body = {"error": friendly(e)}
                if getattr(e, "code", None):
                    body["code"] = e.code
                return self._send(400, body)
            except (ConnectionError, TimeoutError):
                return None
            except Exception as e:
                app.log(f"[painel] erro interno em {u.path}: {e!r}")
                app._trace()
                return self._send(500, {"error": f"erro interno: {e}"})

    return Handler


# ------------------------------------------------------ janelinha de controle
def _pick_folder(root):
    """Seletor de pasta nativo, na frente do painel (roda na thread do tkinter)."""
    import tkinter as tk
    from tkinter import filedialog
    top = tk.Toplevel(root)
    try:
        top.geometry("1x1+200+200")
        try:
            top.attributes("-alpha", 0.0)
            top.attributes("-topmost", True)
        except tk.TclError:
            pass
        top.lift()
        top.focus_force()
        return filedialog.askdirectory(parent=top, mustexist=True, title="Escolha a pasta do projeto") or ""
    finally:
        top.destroy()


def _set_icon(root, tk):
    """Σ na janelinha e na barra de tarefas, em vez da pena do Tk."""
    try:
        ico = icon_file("sigma.ico") if os.name == "nt" else None
        if ico:
            root.iconbitmap(default=ico)
            return
        png, data = icon_file("sigma.png"), None
        if not png:
            data = icon_data("sigma.png")
            if not data:
                return
        # guarda a referência na janela: sem ela o Python apaga a imagem e o ícone some
        root._sigma_icon = tk.PhotoImage(file=png) if png else tk.PhotoImage(data=base64.b64encode(data))
        root.iconphoto(True, root._sigma_icon)
    except Exception:
        pass


def run_control_window(app, background=False):
    """Janelinha tkinter: status a cada 2 s, Abrir painel e Desligar. Sem tkinter (ou sem tela): console."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        if os.name == "nt":
            try:   # texto nítido em telas com escala
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        root = tk.Tk()
    except Exception as e:
        app.log(f"janela de controle indisponível ({e}); seguindo no modo console (Ctrl+C desliga)")
        return app.wait_console()
    red, bg, fg = "#ff3b1f", "#000000", "#f2f2f2"
    root.title("Sigma")
    root.configure(bg=bg)
    root.resizable(False, False)
    _set_icon(root, tk)
    status = tk.StringVar(value="Σ SIGMA · iniciando…")
    news = tk.StringVar(value="")
    box = tk.Frame(root, bg=bg, padx=18, pady=14)
    box.pack()
    status_label = tk.Label(box, textvariable=status, bg=bg, fg=fg, font=("Segoe UI", 11, "bold"), anchor="w",
                            justify="left", width=46, wraplength=440)
    status_label.pack(fill="x")
    news_label = tk.Label(box, textvariable=news, bg=bg, fg="#ffd400", font=("Segoe UI", 9, "bold"), anchor="w",
                          justify="left", wraplength=440)
    tk.Label(box, text="Vão substituir você por um robô. Queremos que os donos dos robôs sejam as pessoas, não as "
             "Big Techs.", bg=bg, fg="#8a8a8a", font=("Segoe UI", 8), anchor="w", justify="left",
             wraplength=440).pack(fill="x", pady=(2, 0))
    row = tk.Frame(box, bg=bg)
    row.pack(fill="x", pady=(12, 0))
    style = {"relief": "flat", "bd": 0, "padx": 16, "pady": 7, "cursor": "hand2", "font": ("Segoe UI", 10, "bold")}

    def ask_quit():
        if messagebox.askyesno("Desligar o Sigma?", "Seu computador vai parar de minerar SIG e de hospedar a rede."
                               "\n\nDesligar agora?", parent=root):
            status.set("Σ SIGMA · desligando…")
            threading.Thread(target=app.shutdown, daemon=True).start()
            return True
        return False

    def on_close():
        if not ask_quit():
            root.iconify()   # quis fechar mas não desligar: sai da frente e continua trabalhando

    open_btn = tk.Button(row, text="Abrir painel", bg=red, fg="#000", activebackground="#ff6a52",
                         activeforeground="#000", command=app.open_window, **style)
    open_btn.pack(side="left")
    news_btn = tk.Button(row, text="Baixar a versão nova", bg="#ffd400", fg="#000", activebackground="#ffe566",
                         activeforeground="#000", command=lambda: webbrowser.open(app.download_url()), **style)
    tk.Button(row, text="Desligar", bg="#1c1c1c", fg=fg, activebackground="#333333", activeforeground=fg,
              command=ask_quit, **style).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", on_close)
    last = [0.0]
    app.window_up = True

    def tick():
        if app.done.is_set():
            try:   # desligado pelo painel (pode haver uma pergunta aberta na janela)
                root.destroy()
            except tk.TclError:
                pass
            return
        app.run_ui_calls(root)
        if time.time() - last[0] >= 2:
            last[0] = time.time()
            try:
                status.set(app.status_line())
                line = app.update_line()
                if line != news.get():
                    news.set(line)
                    if line:
                        news_label.pack(fill="x", pady=(4, 0), after=status_label)
                        news_btn.pack(side="left", padx=(8, 0), after=open_btn)
                    else:
                        news_label.pack_forget()
                        news_btn.pack_forget()
            except Exception:
                pass
        root.after(300, tick)

    if background:
        root.iconify()
    tick()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        app.window_up = False
        app.run_ui_calls(root, cancel=True)   # ninguém fica esperando um seletor de pasta que não vai abrir
    app.shutdown()


def _fatal(msg, gui):
    print(f"erro: {msg}", file=sys.stderr)
    if gui:   # no .exe sem console, a única forma de a pessoa ver o problema
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("Sigma", msg)
            r.destroy()
        except Exception:
            pass


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="sigma.app", description="Sigma: execute e pronto. Seu computador minera SIG "
                                 "e hospeda a rede (IA, computação e sites) para a sua carteira.")
    ap.add_argument("--headless", action="store_true", help="sem interface (servidores); Ctrl+C desliga")
    ap.add_argument("--background", action="store_true", help="inicia sem abrir o painel (ao ligar o computador)")
    ap.add_argument("--wallet", metavar="ENDEREÇO", help="só minerar e hospedar para este endereço (sig… ou 0x…)")
    ap.add_argument("--node", metavar="URL", help=f"nó da rede (padrão {DEFAULT_NODE})")
    ap.add_argument("--data-dir", help="pasta de dados (padrão %%LOCALAPPDATA%%\\Sigma ou ~/.sigma)")
    ap.add_argument("--no-mine", action="store_true", help="não minerar nesta execução")
    ap.add_argument("--no-ai", action="store_true", help="não servir IA nesta execução")
    ap.add_argument("--cpu", type=float, metavar="PCT", help="porcentagem da CPU para minerar (1-100)")
    ap.add_argument("--disk-gb", type=float, metavar="N", help="espaço em disco cedido para a rede")
    ap.add_argument("--name", help="nome desta máquina na rede")
    ap.add_argument("--port", type=int, help=f"porta do painel local (padrão {DEFAULT_PORT})")
    ap.add_argument("--engine", choices=ENGINES, help="motor de IA (padrão auto: Ollama se houver, senão llama.cpp)")
    ap.add_argument("--public-url", help="URL pública desta máquina, para servir arquivos de sites (opcional)")
    # grupo (sigma/grupo.py): um coordenador roda modelos que não cabem nele com a memória de outros PCs
    ap.add_argument("--coordinate", metavar="HOST:PORTA", help="coordenar um grupo: escuta os trabalhadores aqui")
    ap.add_argument("--group-worker", action="append", default=[], metavar="ENDEREÇO=GB",
                    help="trabalhador do grupo (máquina da rede) e quantos GB ele empresta; um por trabalhador")
    ap.add_argument("--worker-of", metavar="ENDEREÇO@HOST:PORTA",
                    help="emprestar memória para o coordenador (endereço da máquina dele e onde ele escuta)")
    ap.add_argument("--no-auto-update", action="store_true",
                    help="não se atualizar sozinho quando sair versão nova (só vale para o .exe e o .pyz)")
    ap.add_argument("--group-kind", choices=("text", "video"),
                    help="o que o grupo divide: text (llama.cpp, padrão) ou video (stable-diffusion.cpp)")
    args = ap.parse_args(argv)
    if args.port is not None and not 0 < args.port < 65536:
        ap.error("--port deve estar entre 1 e 65535")
    return args


def main(argv=None):
    multiprocessing.freeze_support()   # PyInstaller: processos do minerador entram por aqui
    for s in (sys.stdout, sys.stderr):
        if s is not None and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    netsafe.install_default_opener()   # HTTPS que funciona também no Python do python.org no macOS
    args = parse_args(argv)
    gui = not args.headless
    if args.wallet:
        try:
            args.wallet = typed_address(args.wallet)
        except AppError as e:
            _fatal(str(e), gui)
            return 2
    app = App(args)
    try:
        os.makedirs(app.data_dir, exist_ok=True)
    except OSError as e:
        _fatal(f"não consegui criar a pasta de dados {app.data_dir}: {e}", gui)
        return 2
    code = app.claim_instance(gui)
    if code is not None:
        return code
    try:
        app.boot()
    except (AppError, OSError) as e:
        _fatal(str(e), gui)
        app.shutdown()
        return 2
    for name in ("SIGTERM", "SIGBREAK"):   # systemd/pm2 e o fechar do console: desliga limpo
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name),
                              lambda *_: threading.Thread(target=app.shutdown, daemon=True).start())
            except (ValueError, OSError):
                pass
    if args.headless:
        app.wait_console()
    else:
        if not args.background:
            app.open_window()
        run_control_window(app, background=args.background)
    return 0


if __name__ == "__main__":
    sys.exit(main())
