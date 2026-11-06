"""Nó do Sigma: mantém a chain, aceita transações (inclusive os relatórios de mineração), produz/sincroniza
blocos, guarda blobs (conteúdo endereçado por hash), serve o site, os downloads do app e os sites da rede.

    python -m sigma.node init --dir dados/no1 [--epoch-seconds 30 --window-seconds 5 --min-bits 4]
    python -m sigma.node run  --dir dados/no1 --port 8700 --producer [--downloads dist]
    python -m sigma.node run  --dir dados/no2 --port 8701 --genesis dados/no1/genesis.json --peer http://127.0.0.1:8700

O genesis tem `alloc` vazio. O SIG nasce da emissão contínua (0,01% do que falta dos 100 milhões a cada 12 h,
um pouco em cada bloco, dividido pelo trabalho de quem minerou) e, uma vez só, da carteira dos fundadores que a
atualização v2 da rede principal cria (chain.UPGRADE_GRANTS; aparece em /status como `grants`). Nas regras v3, as
cauções dos coordenadores ficam públicas em /bonds (e o total travado nelas, em /mining e no `supply` do /status).

Memória limitada: só os últimos MEM_BLOCKS blocos ficam na RAM; os mais antigos são lidos do chain.jsonl
quando alguém pede (`offsets` guarda a posição de cada altura no arquivo).
"""
import argparse
import array
import collections
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import re
import shutil
import stat
import sys
import threading
import time
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__, chain, netsafe, rpc
from .chain import (UNIT, State, TxError, UpgradeRequired, block_hash, canon, genesis_block, make_block, tx_id,
                    tx_summary)
from .crypto import Wallet, norm_address, progress_message, to_eth, verify
from .evm import DEFAULT_CHAIN_ID, SYSTEM_ADDRESS, system_call
from .forum import CATEGORIES, Forum, ForumError
from .pow import share_ok

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

BLOCK_TIME = 1.0
MAX_BLOB = 20 * 1024 * 1024
SMALL_BLOB = 256 * 1024         # com o armazenamento cheio, blobs pequenos (pedidos de IA) ainda entram...
BLOB_OVERFLOW = 1_000_000_000   # ... até o limite + 1 GB, para o chat não parar
BLOB_GRACE = 3600               # blob sem referência fica pelo menos 1 h (envio em andamento, job ainda na fila)
BLOB_GC_EVERY = 3600            # limpeza dos blobs a cada hora
SITE_GRACE = 7 * 86400          # arquivos de site expirado ficam 7 dias: dá para recarregar sem publicar de novo
MAX_TX = 256 * 1024             # corpo de /tx, /rpc e do fórum
# progresso de um pedido de IA (fora da chain): a máquina manda a última notícia (texto parcial, prévia da imagem,
# porcentagem, sinal de vida), assinada por ela e cifrada para o cliente; o nó guarda só a mais nova, na memória
MAX_PROGRESS = 1024 * 1024      # corpo de /progress (a prévia da imagem vai junto, em hexadecimal)
MAX_WATCH = 64 * 1024 * 1024    # corpo de /watch: a entrega inteira (imagens, vídeo) em hexadecimal
PROGRESS_KEEP = 2 * 3600        # segundos que a notícia fica depois da última atualização
PROGRESS_JOBS = 4000            # pedidos acompanhados ao mesmo tempo (os mais velhos saem)
MAX_SIGNED_DATA = 32 * 1024     # canon(data) de uma tx assinada: o resto é griefing de disco/cache (vai no /blob)
MAX_DRAIN = MAX_BLOB + 1024 * 1024   # corpo recusado até este tamanho é lido e descartado (resposta chega inteira)
MAX_JSON = 64 * 1024 * 1024     # resposta JSON de um peer (sincronização)
MAX_RPC_RESPONSE = 8 * 1024 * 1024   # teto de qualquer resposta de RPC: uma leitura não amplifica a memória do nó
MAX_BLOCK_TXS = 500
MAX_MEMPOOL = 20_000
MAX_SYNC_TXS = 2000             # por resposta de /blocks: blocos cheios de relatórios não estouram o seguidor
MAX_FUTURE = 30                 # segundos: bloco com horário além disso vem de um relógio errado
SNAPSHOT_EVERY = 500            # blocos entre snapshots do estado (reinício rápido)
MEM_BLOCKS = 2000               # blocos na RAM; os mais antigos vêm do disco
RECENT_PARSE = 10_000           # ao reiniciar, os últimos blocos são lidos por inteiro (cauda e tx_pos recente)
TX_POS_SECONDS = 7200           # ids das transações comuns ficam ~2 h no índice (as da MetaMask, para sempre)
MAX_RECENT_TXS = 200_000
HISTORY_MAX = 1000              # entradas por endereço no histórico
MAX_REFS = 10                   # nomes de site/modelo guardados numa entrada de crédito
EVM_UNIT = 2048                 # na cota por conexão, uma tx da MetaMask conta 1 a cada 1 KB (hash em Python puro)
MAX_EVM_RAW = 200_000           # caracteres hex do raw de uma tx da MetaMask (o mesmo limite da chain)
# limites por conexão de internet no modo --public (padrões das flags)
WORK_PER_IP = 256               # relatórios de mineração aceitos por janela
BAD_WORK_PER_IP = 8             # relatórios recusados por janela (o válido nunca é barrado por esta cota)
TX_PER_IP = 300                 # transações assinadas por janela (inclui eth_sendRawTransaction)
BLOB_MB_PER_IP_HOUR = 500       # MB de blobs novos por hora
QUIET_TXS = ("work", "ping")    # rotina automática das máquinas: não entra no histórico das carteiras
SIGNED_KEYS = frozenset({"type", "data", "nonce", "pub", "sig", "id"})
UNSIGNED_KEYS = frozenset({"type", "data", "id"})   # work e evm: sem assinatura do Sigma
WORK_FIELDS = ("miner", "window", "bits", "nonces", "worker")
NAME_KINDS = ("rent", "royalty", "budget")          # créditos cuja referência é um nome de site/modelo
MACHINE_DEFAULTS = {"pub": None, "jobs_failed": 0}  # máquinas antigas: os campos novos aparecem mesmo assim
# gravados no genesis: a rede fica definida pelo arquivo, não pelos padrões do código
GENESIS_PARAMS = dict(chain.DEFAULT_PARAMS)
SUPPLY_KEYS = ("minted", "cap", "epoch", "epoch_ends", "next_mint", "mint_per_hour", "miners_epoch", "hashrate",
               "bonded")
PAGES = {"": "index.html", "carteira": "carteira.html", "wallet": "carteira.html", "forum": "forum.html",
         "tutorial": "tutorial.html", "baixar": "baixar.html", "download": "baixar.html", "sigma.css": "sigma.css"}
HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")
WORKER_RE = re.compile(r"^[0-9a-f]{1,16}\Z")
DOWNLOAD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
PAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}\Z")
ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.(css|js|svg|png|jpe?g|gif|ico|webp|woff2?|txt)\Z")
HIDDEN_PAGES = {"app"}          # app.html é o painel local do app (servido por ele), não uma página do site
TEXT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml",
              ".txt": "text/plain"}
# sites publicados rodam numa origem opaca: sem cookies, localStorage nem as APIs do fórum e da carteira.
# Sem 'allow-downloads': um site de terceiros no domínio oficial não baixa arquivos (achado site-phishing)
SITE_HEADERS = {"Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups "
                                           "allow-popups-to-escape-sandbox allow-modals",
                "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
BLOB_HEADERS = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}
# /site/ nunca serve executável/instalável: um golpista não publica um "Sigma.exe" no domínio oficial
BLOCKED_SITE_EXT = frozenset((".exe", ".msi", ".msix", ".bat", ".cmd", ".com", ".scr", ".ps1", ".vbs", ".jar",
                              ".pyz", ".apk", ".dmg", ".pkg", ".appimage", ".deb", ".rpm", ".zip", ".7z", ".rar"))


class RateLimited(TxError):
    """Passou do limite desta conexão de internet (modo --public): responde HTTP 429."""
    status = 429

    def __init__(self, msg, retry_after=60):
        super().__init__(msg)
        self.retry_after = max(1, math.ceil(retry_after))


class MempoolFull(TxError):
    """Fila cheia: não é culpa de quem mandou (não conta como relatório recusado)."""


class _BadTx(Exception):
    """Uma transação deu erro inesperado ao ser aplicada no bloco (ela já saiu da fila)."""


def http_json(url, payload=None, timeout=5, limit=MAX_JSON):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(netsafe.read_capped(r, limit))   # um peer não estoura a memória do nó


def longpath(path):
    """No Windows, caminhos com mais de 260 caracteres precisam do prefixo de caminho longo."""
    prefix = "\\\\?\\"
    path = os.path.abspath(path)
    return prefix + path if os.name == "nt" and not path.startswith(prefix) else path


def content_type(name, default="application/octet-stream"):
    ext = os.path.splitext(name)[1].lower()
    ctype = TEXT_TYPES.get(ext) or mimetypes.guess_type(name)[0] or default
    return ctype + "; charset=utf-8" if ctype.startswith("text/") else ctype


def web_file(name):
    """Arquivo de sigma/web para a rota /<name>: páginas conhecidas, outras páginas .html e arquivos estáticos."""
    if name in PAGES:
        return PAGES[name]
    if PAGE_RE.match(name) and name not in HIDDEN_PAGES and os.path.isfile(os.path.join(WEB_DIR, name + ".html")):
        return name + ".html"
    if ASSET_RE.match(name) and os.path.isfile(os.path.join(WEB_DIR, name)):
        return name
    return None


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")


def ip_key(ip):
    """Chave dos limites por conexão: o IPv4 inteiro ou o bloco /64 do IPv6 (cada casa recebe um /64 inteiro)."""
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return str(ip)
    a = getattr(a, "ipv4_mapped", None) or a
    if a.version == 6:
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


def _wait_text(seconds):
    s = max(1, math.ceil(seconds))
    return f"daqui a ~{s} s" if s < 90 else f"daqui a ~{round(s / 60)} min"


def _size_text(n):
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{n // 1024} KB"


def _count(n, one, many):
    return f"{n} {one if n == 1 else many}"


# ----------------------------------------------------------- formato das transações (SPEC §6.1)
def _no_extra(d, allowed, where):
    extra = sorted(k for k in d if k not in allowed)
    if extra:
        names = ", ".join(str(k)[:40] for k in extra[:10]) + (", …" if len(extra) > 10 else "")
        raise TxError(f"campos desconhecidos {where}: {names}")


def normalize_tx(tx):
    """(1) Formato: só as chaves do contrato e nada que o canon() não consiga gravar (texto com pedaço de
    caractere, número grande demais, NaN). (2) work e evm são refeitos só com os campos do contrato: enfeites
    não chegam a um bloco nem geram ids novos. O id é calculado depois (ver submit_tx)."""
    if not isinstance(tx, dict) or not isinstance(tx.get("type"), str) or not isinstance(tx.get("data"), dict):
        raise TxError("transação malformada")
    try:
        canon(tx)    # o mesmo canon() do bloco: o que passa aqui sempre dá para gravar
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise TxError("transação malformada: texto inválido ou número grande demais") from None
    kind, data = tx["type"], tx["data"]
    _no_extra(tx, UNSIGNED_KEYS if kind in ("work", "evm") else SIGNED_KEYS, "na transação")
    if kind == "evm":
        _no_extra(data, ("raw",), "em evm")
        if not isinstance(data.get("raw"), str):
            raise TxError("transação malformada")
        if len(data["raw"]) >= MAX_EVM_RAW:   # a regra da chain, antes do hash (caro em Python puro)
            raise TxError(f"transação da MetaMask grande demais (máximo {MAX_EVM_RAW // 2000} KB)")
        return {"type": "evm", "data": {"raw": data["raw"]}}
    if kind == "work":
        _no_extra(data, WORK_FIELDS, "em work")
        return {"type": "work", "data": {k: data[k] for k in WORK_FIELDS if k in data}}
    # tx assinada: guarda no bloco só as chaves do contrato, já limpas. O corpo canônico nunca passa de 32 KB —
    # transfer/register_machine/create_job são minúsculos; conteúdo grande vai no /blob (achados disk-bloat e
    # sig-cache: um corpo gigante não incha a chain.jsonl nem o cache de assinaturas)
    signed = {k: tx[k] for k in ("type", "data", "nonce", "pub", "sig") if k in tx}
    if len(canon(signed["data"])) > MAX_SIGNED_DATA:
        raise TxError("transação grande demais")
    return signed


def _tx_id(tx):
    try:
        return tx_id(tx)
    except (TypeError, ValueError, AttributeError):
        raise TxError("transação malformada") from None


# ------------------------------------------------------------------ leitura do estado
RELAY_PORT = 0   # porta pública do relé dos grupos neste nó (cmd_run --relay-port)


def mining_info(st):
    """Painel da mineração: janela e seed atuais, época, emissão e poder computacional da rede."""
    m, p = st.mining, st.params
    w, ws, es = m["window"], p["window_seconds"], p["epoch_seconds"]
    seeds = m.get("seeds") or {}
    recent = [x for x in st.miners.values() if isinstance(x.get("last_window"), int) and x["last_window"] >= w - 2]
    return {"window": w, "seed": seeds.get(str(w)), "prev_window": w - 1, "prev_seed": seeds.get(str(w - 1)),
            "window_seconds": ws, "window_ends": m["window_start"] + ws,
            "min_bits": p["min_share_bits"], "max_shares": p["max_shares"],
            "epoch": m["epoch"], "epoch_seconds": es, "epoch_ends": m["epoch_start"] + es,
            "minted": m["minted"], "cap": p["supply_cap"], "mint_bps": p["mint_bps"],
            # emissão contínua: sai em todo bloco; a taxa de referência é por período de 12 h
            "continuous": True, "next_mint": st.next_mint(), "mint_per_period": st.next_mint(),
            "mint_per_hour": st.next_mint() * 3600 // max(1, round(es)),
            "points_total": sum(st.points.values()),            # a base que recebe agora (última janela fechada)
            "miners_epoch": sum(1 for v in st.points.values() if v > 0),
            # quem relatou uma das duas últimas janelas encerradas ainda está minerando
            "hashrate": round(sum(x.get("last_rate") or 0 for x in recent), 1),
            # preços da rede: aluguel por MB por janela (genesis) e o preço de IA de referência
            "hosting_per_mb_window": p["hosting_per_mb_window"], "ram_per_mb_window": p["ram_per_mb_window"],
            "demand_max_bps": p["demand_max_bps"], "rent": dict(m.get("rent") or {}),
            "proposal_deposit": p["proposal_deposit"], "quorum_bps": p["quorum_bps"],
            "ai_default_price": chain.DEFAULT_PRICES["ai_per_1k_tokens"],
            # v2: preço de referência por imagem e por segundo de vídeo, e se a rede já aceita essas classes
            "media_default_prices": dict(chain.MEDIA_PRICES), "media": st.rule(2),
            # v3: se a rede já aceita caução, o SIG travado nelas, quantas máquinas podem coordenar agora, a caução
            # mínima para isso (None antes da v3) e a espera de uma retirada
            "parts": st.rule(4),   # v4: modelo de texto em partes (gigantes, rodam divididos num grupo)
            # v8: a rede monta os grupos sozinha; o relé (porta pública deste nó) liga quem está atrás de roteador
            "groups": st.rule(8), "relay_port": RELAY_PORT or None,
            "bonding": st.rule(3), "bonded": st.bonded(), "coordinators": coordinators(st),
            "min_bond": st.params.get("min_bond"), "unbond_seconds": chain.UNBOND_SECONDS,
            "now": time.time()}


def coordinators(st):
    """Quantas máquinas podem coordenar a divisão de um modelo gigante agora (State.is_coordinator)."""
    return sum(1 for a in (st.ext.get("bonds") or {}) if st.is_coordinator(a))


def bond_info(st, machine):
    """Uma caução como o nó mostra: o ativo, as retiradas em espera (quando cada uma volta), o total, quanto já
    foi cortado e se a máquina pode coordenar agora. None se a máquina não tem caução."""
    b = (st.ext.get("bonds") or {}).get(machine) if isinstance(machine, str) else None
    if b is None:
        return None
    pending = sum(u["amount"] for u in b["unbonding"])
    return dict(b, machine=machine, unbonding=[dict(u) for u in b["unbonding"]], pending=pending,
                total=b["amount"] + pending, coordinator=st.is_coordinator(machine))


def bonds_info(st):
    """/bonds: as cauções (as maiores primeiro), o total travado, quantas máquinas podem coordenar, as regras (caução
    mínima e espera da retirada) e os últimos cortes (o mais novo primeiro)."""
    bonds = [bond_info(st, a) for a in sorted(st.ext.get("bonds") or {})]
    bonds.sort(key=lambda b: -b["amount"])
    return {"bonding": st.rule(3), "bonds": bonds, "bonded": st.bonded(), "coordinators": coordinators(st),
            "min_bond": st.params.get("min_bond"), "unbond_seconds": chain.UNBOND_SECONDS,
            "slashes": list(reversed(st.ext.get("slashes") or []))}


def proposal_info(st, pid, full=True):
    """Uma votação como o site e o painel mostram: totais, prazo, quórum de agora e (aberta) os votos."""
    prop = st.proposals.get(pid) if isinstance(pid, str) else None
    if prop is None:
        return None
    out = {k: v for k, v in prop.items() if k != "votes"}
    out["quorum_now"] = st.mining["minted"] * st.params["quorum_bps"] // 10_000
    out["voters"] = prop.get("voters", len(prop.get("votes") or {}))
    if full:
        out["votes"] = [{"address": a, **v} for a, v in sorted((prop.get("votes") or {}).items(),
                                                                  key=lambda kv: -kv[1]["amount"])][:200]
    return out


def governance_info(st):
    """Lista das votações (abertas primeiro, depois as encerradas mais novas) e as regras de votar. Só os
    parâmetros que se votam na versão em vigor (o min_bond só existe a partir da v3)."""
    props = sorted(st.proposals.values(), key=lambda p: (p["status"] != "open", -p["created"]))
    return {"proposals": [proposal_info(st, p["id"], full=False) for p in props],
            "governable": {k: {"min": lo, "max": hi, "now": st.params[k]}
                           for k, (lo, hi) in chain.governable(st.rules).items() if k in st.params},
            "params": dict(st.params), "vote_seconds": chain.VOTE_SECONDS,
            "max_open": chain.MAX_OPEN_PROPOSALS, "deposit": st.params["proposal_deposit"],
            "quorum_bps": st.params["quorum_bps"], "locked": st.locked()}


def miner_info(st, addr, info=None):
    """Fatia de um minerador na base que recebe agora; a estimativa por hora usa a mesma conta da chain."""
    info = info or mining_info(st)
    pts, total = st.points.get(addr, 0), info["points_total"]
    rec = st.miners.get(addr) or {}
    pending = sum(w.get(addr, 0) for w in (st.mining.get("open_points") or {}).values())
    per_hour = info["mint_per_hour"] * pts // total if total else 0
    return {"address": addr, "points": pts, "share": pts / total if total else 0.0, "pending_points": pending,
            "est_per_hour": per_hour, "est_reward": per_hour,
            "rewards_total": rec.get("rewards_total", 0), "last_window": rec.get("last_window"),
            "last_rate": rec.get("last_rate", 0), "points_total": total,
            "lifetime_points": rec.get("points_total", 0), "epoch": info["epoch"]}


def hosting_index(st):
    """máquina -> (modelos atribuídos/hospedados, sites hospedados), numa passada só pelo estado: cada máquina
    consulta /machine/<ela> a cada poucos segundos, e o nó calcula isto uma vez por bloco."""
    assign, sites = {}, {}
    for name, m in sorted(st.models.items()):
        pinners, assigned = m.get("pinners") or {}, m.get("assigned") or {}
        rent = st.rent_window(m)   # a máquina não baixa GB de um modelo que não paga nem um dia de aluguel
        for addr in sorted(set(pinners) | set(assigned)):
            assign.setdefault(addr, []).append(
                {"name": name, "url": m.get("url"), "sha256": m.get("sha256"), "size": m.get("size"),
                 "ram_mb": m.get("ram_mb"), "status": "pinned" if addr in pinners else "assigned",
                 "budget": m.get("budget"), "rent_window": rent,
                 # v2: classe do modelo e, para imagem/vídeo, os arquivos extras (VAE, codificador de texto)
                 "task": chain.model_task(m), "files": list(m.get("files") or []),
                 "main_size": m.get("main_size", m.get("size"))})
    for name, s in sorted(st.sites.items()):
        if s.get("status") == "active":
            for addr in s.get("pinners") or {}:
                sites.setdefault(addr, []).append(name)
    return assign, sites


def machines_info(st):
    """/machines: o registro de cada máquina, com os modelos de imagem e vídeo à parte, em `media_models`; em
    `models` ficam só os de texto. Um app 2.4 não conhece a classe dos modelos: com um de imagem em `models`, ele
    o listaria e escolheria como se fosse de texto, e todo pedido seria recusado. Só a resposta HTTP muda: o
    estado da chain (e o /machine/<endereço>, que a própria máquina lê) continua com todos em `models`."""
    out = {}
    for a, m in st.machines.items():
        rec = dict(MACHINE_DEFAULTS, **m)
        names = rec.get("models") or []
        rec["models"] = [n for n in names if chain.model_task(st.models.get(n) or {}) == "text"]
        rec["media_models"] = [n for n in names if chain.model_task(st.models.get(n) or {}) != "text"]
        out[a] = rec
    return out


def machine_info(st, addr, index=None):
    """Registro da máquina + o que a rede colocou nela: modelos atribuídos/hospedados e sites."""
    rec = st.machines.get(addr)
    if rec is None:
        return None
    assign, sites = index or hosting_index(st)
    out = dict(MACHINE_DEFAULTS, **rec, address=addr)
    out["assignments"] = assign.get(addr, [])
    out["sites"] = sites.get(addr, [])
    out["groups"] = []   # v8: os grupos em que a máquina está, com o papel dela e as chaves dos outros membros
    for gid, g in sorted((st.ext.get("groups") or {}).items()):
        if addr != g["coordinator"] and addr not in g["workers"]:
            continue
        pubs = {a: (st.machines.get(a) or {}).get("pub") for a in [g["coordinator"], *g["workers"]]}
        out["groups"].append({"id": gid, "model": g["model"], "coordinator": g["coordinator"],
                              "role": "coordinator" if addr == g["coordinator"] else "worker",
                              "workers": dict(g["workers"]), "own_mb": g["own_mb"], "pubs": pubs})
    return out


def blob_refs(st):
    """Hashes que o estado ainda usa: arquivos de sites ativos ou expirados há menos de 7 dias, e pedidos,
    respostas e resultados dos jobs que ainda estão no estado."""
    w = st.mining["window"]
    grace = math.ceil(SITE_GRACE / st.params["window_seconds"])
    refs = set()
    for s in st.sites.values():
        if s.get("status") != "active":
            ew = s.get("expired_window")
            if isinstance(ew, int) and w - ew >= grace:
                continue
        for f in (s.get("manifest") or {}).values():
            if isinstance(f, dict) and isinstance(f.get("hash"), str):
                refs.add(f["hash"])
    for m in st.ext.get("chat") or []:   # v9: os textos do chat global que ainda estão na lista
        refs.add(m["blob"])
    for j in st.jobs.values():
        for k in ("spec_hash", "result_hash"):
            if isinstance(j.get(k), str):
                refs.add(j[k])
        for r in (j.get("results") or {}).values():
            if isinstance(r, dict) and isinstance(r.get("hash"), str):
                refs.add(r["hash"])
    return refs


class Quotas:
    """Contadores por conexão que zeram a cada período (a janela de mineração, ou a hora para os blobs)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.period, self.counts = {}, {}

    def _bucket(self, name, period):
        cur = self.period.get(name)
        if cur is None or period > cur:
            self.period[name], self.counts[name] = period, {}
        elif period < cur:
            return None              # período que já passou: nada a contar
        return self.counts[name]

    def get(self, name, period, key):
        with self.lock:
            c = self._bucket(name, period)
            return 0 if c is None else c.get(key, 0)

    def add(self, name, period, key, n=1, limit=None):
        """Soma n e devolve o novo total; com `limit`, só soma se couber (senão devolve None)."""
        with self.lock:
            c = self._bucket(name, period)
            if c is None:
                return 0
            v = c.get(key, 0) + n
            if limit is not None and v > limit:
                return None
            if v > 0:
                c[key] = v
            else:
                c.pop(key, None)
            return v


class Node:
    def __init__(self, datadir, genesis, producer=None, peers=(), heartbeat=1.0, blob_cap=0, downloads=None,
                 public=False, work_per_ip=WORK_PER_IP, bad_work_per_ip=BAD_WORK_PER_IP, tx_per_ip=TX_PER_IP,
                 blob_mb_per_ip_hour=BLOB_MB_PER_IP_HOUR):
        self.datadir = datadir
        self.genesis = genesis
        self.producer = producer
        self.peers = list(peers)
        self.heartbeat = heartbeat
        self.blob_cap = blob_cap
        self.downloads = os.path.abspath(downloads) if downloads else None
        self.public = public
        self.work_per_ip, self.bad_work_per_ip, self.tx_per_ip = work_per_ip, bad_work_per_ip, tx_per_ip
        self.blob_mb = blob_mb_per_ip_hour
        self.blob_quota = int(blob_mb_per_ip_hour * 1024 * 1024)
        self.clock = time.time             # relógio dos blocos e dos limites (os testes trocam)
        self.lock = threading.RLock()
        self.blob_lock = threading.Lock()  # gravação e limpeza de blobs, sem segurar o lock da chain
        self.blob_dir = longpath(os.path.join(datadir, "blobs"))
        os.makedirs(self.blob_dir, exist_ok=True)
        self.chain_file = os.path.join(datadir, "chain.jsonl")
        self.snapshot_file = os.path.join(datadir, "snapshot.json")
        self.genesis_block = genesis_block(genesis)
        self.blocks = [self.genesis_block]           # só a cauda: os últimos MEM_BLOCKS
        self.offsets = array.array("q", [-1])        # altura -> posição no chain.jsonl (0 = genesis, fora dele)
        self.state = State(genesis)
        self.progress, self._progress_trim = {}, 0.0   # job_id -> última notícia da máquina (put_progress)
        self.watch = {}                                # (job_id, papel, vigia) -> cópia cifrada para a vigia (v7)
        self.tx_pos = {}       # id -> (altura, posição no bloco): MetaMask para sempre, as demais por ~2 h
        self._recent_txs = collections.deque()       # (horário, altura, [ids comuns]) para podar o tx_pos
        self._recent_count = 0
        self.history = {}      # endereço -> [transações, recompensas e créditos], no máximo HISTORY_MAX
        self._credit_idx = {}  # (endereço, tipo, época) -> entrada de crédito do histórico
        self.dropped = {}
        self.mempool, self.mempool_ids = [], set()
        self.quotas = Quotas()
        self._cache = (None, {})                     # respostas prontas do estado atual (/jobs, /machines...)
        self._gc_busy, self._last_gc = False, time.time() - BLOB_GC_EVERY + 600   # 1ª limpeza ~10 min depois
        self.upgrade_required = None   # {rules, height, message}: a rede passou para regras que este código não tem
        self._load_chain()
        with os.scandir(self.blob_dir) as it:
            self.blob_bytes = sum(e.stat().st_size for e in it if e.is_file())
        self.pending = self.state.clone()
        self.forum = Forum(os.path.join(datadir, "forum.db"), lambda: self.state)

    # ------------------------------------------------------- disco e snapshot
    def _scan_chain_file(self):
        """1ª leitura, sem decodificar: a posição de cada bloco no arquivo. Uma última linha pela metade (queda
        de energia no meio da gravação) é cortada; nada do arquivo fica inteiro na memória."""
        offsets = array.array("q", [-1])
        if not os.path.exists(self.chain_file):
            return offsets
        last = None
        with open(self.chain_file, "rb") as f:
            pos = 0
            for line in f:
                if line.strip():
                    offsets.append(pos)
                    last = (pos, line)
                pos += len(line)
        if last is None:
            return offsets
        try:
            json.loads(last[1])
        except ValueError:
            print(f"[sigma] última linha de {self.chain_file} incompleta; descartada", file=sys.stderr)
            with open(self.chain_file, "r+b") as f:
                f.truncate(last[0])
            offsets.pop()
            return offsets
        if not last[1].endswith(b"\n"):   # o próximo bloco precisa começar numa linha nova
            with open(self.chain_file, "ab") as f:
                f.write(b"\n")
        return offsets

    def _read_block_at(self, offset, f=None):
        if f is not None:
            f.seek(offset)
            return json.loads(f.readline())
        with open(self.chain_file, "rb") as fh:
            fh.seek(offset)
            return json.loads(fh.readline())

    def _read_snapshot(self, offsets):
        """(altura, estado, histórico) se o snapshot bater com a chain; senão None e tudo é reexecutado."""
        try:
            with open(self.snapshot_file, encoding="utf-8") as f:
                snap = json.load(f)
            h, history = snap["height"], snap["history"]
            if not (isinstance(h, int) and 0 < h < len(offsets) and isinstance(history, dict)):
                return None
            b = self._read_block_at(offsets[h])
            if b.get("height") != h or b.get("hash") != snap["hash"]:
                return None
            st = State.from_dict(self.genesis, snap["state"])
            st.height, st.now = h, b["time"]   # fora do to_dict: o pendente precisa deles
            if st.rules > chain.RULES:         # gravado por um Sigma mais novo: reexecuta e para onde precisar
                return None
            if st.root() != b["state_root"]:
                return None
            return h, st, history, b
        except Exception:  # ausente, de outra versão ou corrompido: reexecutar é sempre seguro
            return None

    def _load_chain(self):
        """Lê a chain do disco linha a linha. Com snapshot válido, os blocos até ele só entram nos índices (as
        transações da MetaMask e as recentes; o histórico vem do snapshot) e só os seguintes são reexecutados."""
        offsets = self._scan_chain_file()
        total = len(offsets) - 1
        self.offsets = offsets
        if not total:
            return
        snap = self._read_snapshot(offsets)
        snap_h = snap[0] if snap else 0
        with open(self.chain_file, "rb") as f:
            h = 0
            for line in f:
                if not line.strip():
                    continue
                h += 1
                if h > snap_h:
                    try:
                        b = json.loads(line)
                    except ValueError:
                        raise ValueError(f"{self.chain_file}: bloco {h} corrompido") from None
                    # o arquivo é deste nó: o hash do bloco e o state_root ainda são conferidos
                    try:
                        self._apply_block(b, persist=False, check_clock=False, verify_sig=False)
                    except UpgradeRequired as e:   # gravado por um Sigma mais novo: para aqui, sem apagar nada
                        self._need_upgrade(e)
                        del self.offsets[h:]
                        return
                    continue
                full = h > total - RECENT_PARSE or h == snap_h
                if not (full or b'"evm"' in line):
                    continue
                b = snap[3] if h == snap_h else json.loads(line)
                if b.get("height") != h:
                    raise ValueError(f"{self.chain_file}: bloco {h} fora de ordem")
                self._index_txs(b)
                if h > total - MEM_BLOCKS or h == snap_h:
                    self._push_tail(b)
                if h == snap_h:
                    _, self.state, self.history, _ = snap
                    self._rebuild_credit_idx()

    def _save_snapshot(self):
        tmp = self.snapshot_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "height": self.height, "hash": self.head["hash"],
                       "state": self.state.to_dict(), "history": self.history}, f)
        os.replace(tmp, self.snapshot_file)

    def _append_line(self, line):
        """Grava o bloco no fim do chain.jsonl. Se a gravação falhar no meio (disco cheio), corta o pedaço:
        o arquivo nunca fica com uma linha quebrada antes do próximo bloco."""
        with open(self.chain_file, "ab") as f:
            start = f.seek(0, os.SEEK_END)
            try:
                f.write(line)
                f.flush()
            except OSError:
                try:
                    f.truncate(start)
                except OSError:
                    pass
                raise
        self.offsets.append(start)

    @property
    def head(self):
        return self.blocks[-1]

    @property
    def height(self):
        return self.blocks[-1]["height"]

    def _push_tail(self, block):
        tail = self.blocks
        if tail and tail[-1]["height"] != block["height"] - 1:
            tail = []            # a cauda é sempre contínua (block() conta a posição pela altura)
        tail.append(block)
        # lista nova ao cortar: quem está lendo a antiga continua vendo blocos coerentes
        self.blocks = tail[-MEM_BLOCKS:] if len(tail) > MEM_BLOCKS else tail

    def _tail_get(self, h):
        tail = self.blocks
        try:
            i = h - tail[0]["height"]
            if 0 <= i < len(tail):
                b = tail[i]
                if b["height"] == h:
                    return b
        except IndexError:
            pass
        return None

    def block(self, h):
        """Bloco da altura h: da cauda na memória ou, se for antigo, lido do chain.jsonl (None se não existe)."""
        if not isinstance(h, int) or isinstance(h, bool) or h < 0:
            return None
        b = self._tail_get(h)
        if b is not None:
            return b
        if h == 0:
            return self.genesis_block
        if h >= len(self.offsets):
            return None
        return self._read_block_at(self.offsets[h])

    def blocks_from(self, start, max_blocks=200, max_txs=MAX_SYNC_TXS):
        """Blocos a partir de `start` para quem sincroniza (os antigos lidos do disco, em sequência)."""
        out, n, h, f = [], 0, max(1, start), None
        head = self.height
        try:
            while h <= head and len(out) < max_blocks:
                b = self._tail_get(h)
                if b is None:
                    if f is None:
                        f = open(self.chain_file, "rb")
                    b = self._read_block_at(self.offsets[h], f)
                if out and n + len(b["txs"]) > max_txs:
                    break
                out.append(b)
                n += len(b["txs"])
                h += 1
        finally:
            if f is not None:
                f.close()
        return out

    # -------------------------------------------------------------- blocos
    def _run_block(self, s, height, t, prev_hash, txs, strict):
        """begin_block -> txs -> end_block sobre `s`. Validando (strict), uma tx inválida derruba o bloco;
        produzindo, ela só fica de fora. Devolve (liquidação da época feita neste bloco, txs incluídas,
        créditos que a chain fez direto nos saldos)."""
        s.last_settlement = None      # transitório: só vale o que este bloco liquidar
        s.begin_block(height, t, prev_hash)
        settled = s.last_settlement
        included = []
        for tx in txs:
            if strict:
                s.apply_tx(tx)
                continue
            try:
                s.apply_tx(tx)
                included.append(tx)
            except TxError as e:
                self._drop(tx["id"], str(e))
            except Exception as e:
                # bug ao aplicar: a tx sai da fila para não travar a produção de blocos para sempre
                self._drop(tx["id"], f"erro interno: {e}")
                self._remove_txs({tx["id"]})
                raise _BadTx(tx["id"]) from e
        s.end_block()
        credits = list(getattr(s, "credits", None) or ())
        return settled or s.last_settlement, included, credits

    def _apply_block(self, block, persist=True, check_clock=True, verify_sig=True):
        """Valida e aplica um bloco recebido (ou relido do disco, sem conferir o relógio nem a assinatura)."""
        prev = self.head
        if not isinstance(block, dict) or not isinstance(block.get("txs"), list):
            raise ValueError("bloco malformado")
        if block.get("height") != prev["height"] + 1 or block.get("prev") != prev["hash"]:
            raise ValueError("bloco fora de sequência")
        t = block.get("time")
        if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) or t <= prev["time"]:
            raise ValueError("o horário do bloco precisa ser maior que o do bloco anterior")
        if check_clock and t > self.clock() + MAX_FUTURE:
            raise ValueError("bloco com horário no futuro (confira o relógio deste computador)")
        if block.get("producer") not in self.genesis["validators"]:
            raise ValueError("produtor não é validador")
        if block_hash(block) != block.get("hash") or (verify_sig and not verify(
                block["producer"], block["hash"].encode(), block.get("sig", ""))):
            raise ValueError("assinatura do bloco inválida")
        rules = block.get("rules", 1)
        if not chain._is_int(rules) or rules < 1:
            raise ValueError("versão das regras do bloco inválida")
        if rules > chain.RULES:      # bloco assinado pelo validador numa versão que este código não tem
            raise UpgradeRequired(rules, block["height"])
        s = self.state.clone()
        settled, _, credits = self._run_block(s, block["height"], t, block["prev"], block["txs"], strict=True)
        if s.rules != rules:
            raise ValueError(f"versão das regras divergente (o bloco diz v{rules}, este nó chegou na v{s.rules})")
        if s.root() != block["state_root"]:
            raise ValueError("state_root divergente")
        self._commit(block, s, settled, credits, persist)

    def _need_upgrade(self, e):
        """Para de produzir e de sincronizar na altura da atualização (nada é descartado) e avisa no /status."""
        if self.upgrade_required is None:
            print(f"[sigma] {e}", file=sys.stderr)
        self.upgrade_required = {"rules": e.rules, "height": e.height, "message": str(e)}

    def _commit(self, block, s, settled, credits=(), persist=True, line=None):
        if persist:  # primeiro no disco: bloco na memória sem estar no arquivo sumiria ao reiniciar
            self._append_line(line or (json.dumps(block) + "\n").encode())
        self.state = s
        self._push_tail(block)
        self._index_block(block, settled, credits, s.mining.get("epoch"))
        if persist and block["height"] % SNAPSHOT_EVERY == 0:
            try:
                self._save_snapshot()
            except (OSError, ValueError, TypeError) as e:
                print(f"[sigma] não consegui gravar o snapshot: {e}", file=sys.stderr)

    # ------------------------------------------------------------- índices
    def _index_txs(self, block):
        """tx_pos: as da MetaMask ficam para sempre (recibos); as demais, só das últimas ~2 h. Ids antigos
        respondem "unknown", o que é seguro: repetição já é barrada por nonce e por relatório de janela."""
        h, t = block["height"], block["time"]
        common = []
        for i, tx in enumerate(block["txs"]):
            txid = tx.get("id") or tx_id(tx)
            self.tx_pos[txid] = (h, i)
            if tx.get("type") != "evm":
                common.append(txid)
        if common:
            self._recent_txs.append((t, h, common))
            self._recent_count += len(common)
        cutoff = t - TX_POS_SECONDS
        while self._recent_txs and (self._recent_txs[0][0] < cutoff or self._recent_count > MAX_RECENT_TXS):
            _, oh, old = self._recent_txs.popleft()
            self._recent_count -= len(old)
            for x in old:
                if self.tx_pos.get(x, (None,))[0] == oh:
                    del self.tx_pos[x]

    def _hist_add(self, addr, entry):
        lst = self.history.setdefault(addr, [])
        lst.append(entry)
        if len(lst) > HISTORY_MAX:   # memória limitada: as mais antigas saem
            old = lst.pop(0)
            if old.get("type") in ("credit", "mint") and old.get("kind"):
                key = (addr, old.get("kind"), old.get("epoch"))
                if self._credit_idx.get(key) is old:
                    del self._credit_idx[key]

    def _add_credit(self, credit, epoch, h, t):
        """Crédito que a chain fez direto no saldo (aluguel, pedido, royalty, troco, devolução): uma entrada
        por (endereço, tipo, época), somando, para o histórico fechar com o saldo sem virar uma linha por bloco.
        A entrada atualizada volta para o fim da lista (fica em ordem da última atualização)."""
        try:
            addr, kind, amount, ref = credit
        except (TypeError, ValueError):
            return
        if not (isinstance(addr, str) and isinstance(kind, str) and isinstance(amount, int)
                and not isinstance(amount, bool) and amount > 0):
            return
        key = (addr, kind, epoch)
        e = self._credit_idx.get(key)
        if e is not None:
            lst = self.history.get(addr, [])
            for i in range(len(lst) - 1, -1, -1):
                if lst[i] is e:
                    del lst[i]
                    break
            else:
                e = None             # já tinha saído pelo limite do histórico
        if e is None:
            e = {"id": None, "type": "credit", "kind": kind, "epoch": epoch, "amount": 0, "count": 0,
                 "from": None, "to": addr, "via": "rede", "name": None, "nonce": None, "refs": []}
            if kind == "mining":             # emissão contínua: uma linha de mineração por período de 12 h
                e.update(type="mint", via="mineração")
            self._credit_idx[key] = e
        e["amount"] += amount
        e["count"] += 1
        e["height"], e["time"] = h, t
        if kind in NAME_KINDS and isinstance(ref, str) and ref not in e["refs"]:
            e["refs"].append(ref[:64])
            del e["refs"][:-MAX_REFS]
            e["name"] = e["refs"][0] if len(e["refs"]) == 1 else None
        self._hist_add(addr, e)

    def _rebuild_credit_idx(self):
        self._credit_idx = {(a, e.get("kind"), e.get("epoch")): e for a, lst in self.history.items()
                            for e in lst if isinstance(e, dict) and e.get("type") in ("credit", "mint")
                            and e.get("kind")}

    def _index_block(self, block, settled=None, credits=(), epoch=None):
        h, t = block["height"], block["time"]
        rewards = (settled or {}).get("rewards") or {}
        if rewards:  # a emissão acontece no começo do bloco, antes das transações
            ep = settled.get("epoch")
            for addr, amount in sorted(rewards.items()):
                if amount > 0:
                    self._hist_add(addr, {
                        "id": None, "type": "mint", "from": None, "to": addr, "amount": amount, "height": h,
                        "time": t, "epoch": ep, "via": "mineração", "name": None, "nonce": None})
        self._index_txs(block)
        for tx in block["txs"]:
            if tx.get("type") in QUIET_TXS:
                continue
            try:
                s = dict(tx_summary(tx), height=h, time=t)
            except Exception:  # um resumo que falha não pode impedir o bloco de entrar
                continue
            if s.get("type") in QUIET_TXS:
                continue
            for a in sorted({s.get("from"), s.get("to")} - {None}):
                self._hist_add(a, s)
        for c in credits or ():
            self._add_credit(c, epoch, h, t)

    # --------------------------------------------------------------- fila
    def _drop(self, txid, error):
        self.dropped[txid] = error
        while len(self.dropped) > 10_000:  # memória limitada: os mais antigos saem primeiro
            self.dropped.pop(next(iter(self.dropped)))

    def _set_mempool(self, txs):
        self.mempool = txs
        self.mempool_ids = {tx["id"] for tx in txs}

    def _remove_txs(self, ids):
        self._set_mempool([m for m in self.mempool if m["id"] not in ids])

    def _rebuild_pending(self):
        """Estado pendente = último bloco + mempool, sem begin_block (usa altura/horário do último bloco)."""
        txs = [tx for tx in self.mempool if tx["id"] not in self.tx_pos]
        while True:
            self.pending = self.state.clone()
            keep, bad = [], None
            for tx in txs:
                try:
                    self.pending.apply_tx(tx)
                    keep.append(tx)
                except TxError as e:
                    self._drop(tx["id"], str(e))
                except Exception as e:
                    self._drop(tx["id"], f"erro interno: {e}")
                    bad = tx
                    break
            if bad is None:
                break
            txs = [t for t in txs if t is not bad]   # o erro pode ter deixado o pendente pela metade: refaz sem ela
        self._set_mempool(keep)

    def produce_block(self):
        """Fecha um bloco com a fila. Nunca para de produzir: se aplicar, calcular o state_root ou montar o
        bloco der erro, as transações desse lote saem da fila (com o motivo) e o próximo bloco segue sem elas."""
        with self.lock:
            if self.upgrade_required:   # o resto da chain (no disco ou no validador) é de regras que não temos
                return False
            for _ in range(3):   # erro inesperado numa tx: ela sai e o bloco é refeito sem ela
                prev = self.head
                h = prev["height"] + 1
                t = max(self.clock(), prev["time"] + 0.001)   # horário dos blocos sempre crescente
                batch = self.mempool[:MAX_BLOCK_TXS]
                s = self.state.clone()
                try:
                    settled, included, credits = self._run_block(s, h, t, prev["hash"], batch, strict=False)
                    block = make_block(self.producer, h, prev["hash"], included, s.root(), t, rules=s.rules)
                    line = (json.dumps(block) + "\n").encode()
                except _BadTx:
                    continue
                except UpgradeRequired as e:   # a fila fica como está: volta a andar com o código novo
                    self._need_upgrade(e)
                    return False
                except Exception as e:
                    reason = f"erro interno ao fechar o bloco: {e}"
                    ids = {tx["id"] for tx in batch}
                    for txid in ids:
                        self._drop(txid, reason)
                    self._remove_txs(ids)
                    self._rebuild_pending()
                    print(f"[produtor] bloco {h} descartado ({len(ids)} transações fora da fila): {e}",
                          file=sys.stderr)
                    return False
                self._commit(block, s, settled, credits, line=line)   # o próprio produtor já executou
                self._rebuild_pending()
                return True
            self._rebuild_pending()
            return False

    def sync_from_peers(self):
        if self.upgrade_required:
            return
        for peer in self.peers:
            try:
                blocks = http_json(f"{peer}/blocks?from={self.height + 1}", timeout=10)["blocks"]
            except Exception:
                continue
            if not blocks or not isinstance(blocks, list):
                continue
            with self.lock:
                for b in blocks:
                    try:
                        self._apply_block(b)
                    except UpgradeRequired as e:
                        self._need_upgrade(e)
                        break
                    except Exception as e:
                        h = b.get("height") if isinstance(b, dict) else None
                        print(f"[sync] bloco {h} de {peer} rejeitado: {e}", file=sys.stderr)
                        break
                self._rebuild_pending()

    def loop(self):
        while True:
            start = time.time()
            try:
                # bloco quando há transações; sem elas, só a cada `heartbeat` segundos
                if self.upgrade_required:   # parado na altura da atualização até rodar o Sigma novo
                    pass
                elif self.producer and (self.mempool or self.clock() - self.head["time"] >= self.heartbeat):
                    self.produce_block()
                elif not self.producer:
                    self.sync_from_peers()
                self._maybe_gc()
            except Exception as e:
                print(f"[loop] erro: {e}", file=sys.stderr)
            time.sleep(max(0.05, BLOCK_TIME - (time.time() - start)))

    # --------------------------------------------------------- transações
    def _check_chat_blob(self, d):
        h, size = d.get("blob"), d.get("size")
        path = self.blob_path(h) if isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) else None
        if not path or not os.path.isfile(path):
            raise TxError("o texto da mensagem não está no nó: mande o blob antes")
        if os.path.getsize(path) != size or size > chain.CHAT_MAX_BYTES:
            raise TxError("o tamanho da mensagem não confere")
        try:
            with open(path, "rb") as f:
                text = f.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise TxError("a mensagem precisa ser texto UTF-8") from None
        if not text.strip():
            raise TxError("mensagem vazia")

    def chat_feed(self, after=0, limit=200):
        """GET /chat: as mensagens do chat global depois de `after` (as `limit` mais novas), com o nome público."""
        st = self.state
        names = st.ext.get("names") or {}
        out = []
        for m in [m for m in (st.ext.get("chat") or []) if m["n"] > after][-limit:]:
            try:
                with open(self.blob_path(m["blob"]), "rb") as f:
                    text = f.read(chain.CHAT_MAX_BYTES).decode("utf-8", "replace")
            except OSError:
                text = None
            out.append({"n": m["n"], "from": m["from"], "name": names.get(m["from"]), "time": m["time"], "text": text})
        return {"messages": out, "price": st.params.get("chat_price"), "keep": chain.CHAT_KEEP,
                "max_bytes": chain.CHAT_MAX_BYTES, "fund": st.ext.get("public_fund", 0),
                "fund_bps": st.params.get("public_fund_bps"), "name_price": st.params.get("name_price")}

    def submit_tx(self, tx, gossip=True, ip=None):
        """Admissão (SPEC §10): (1) formato, chaves e canon; (2) work/evm refeitos só com os campos do contrato;
        (3) fora do lock, o que é caro de conferir (assinatura, MetaMask, scrypt) fica no cache, e o apply_tx lá
        dentro sai de graça; (4) no --public, a cota da conexão; (5) no lock, aplica no estado pendente.
        A cota de transações assinadas é cobrada antes da parte cara: tx recusada também conta, senão quem manda
        lixo nunca chegaria ao limite. A de relatórios de mineração só gasta a parte baixa com os recusados."""
        tx = normalize_tx(tx)
        if tx.get("type") == "chat_post":   # v9: o texto tem de estar aqui, do tamanho declarado e em UTF-8
            self._check_chat_blob(tx.get("data") or {})
        key = ip_key(ip) if self.public and ip is not None else None
        if key is not None and tx["type"] != "work" and not self._targeted_job_tx(tx):
            # claim_job/submit_result de um job direcionado são forçados pela máquina por causa de jobs de
            # terceiros: não contam na cota --tx-per-ip (SPEC §1). As outras txs assinadas continuam contando.
            self._charge_tx(key, tx)
        if tx["type"] == "evm":   # o id é o hash que a MetaMask mostra: sai da decodificação (que fica em cache)
            tx["id"] = chain.decode_evm(tx["data"]["raw"])["hash"]
        else:
            tx["id"] = _tx_id(tx)
        if self._known(tx["id"]):
            return tx["id"]
        window = None
        if key is not None and tx["type"] == "work":
            window = self.state.mining["window"]
            if self.quotas.add("work_ok", window, key, limit=self.work_per_ip) is None:
                raise RateLimited(f"limite de {_count(self.work_per_ip, 'relatório', 'relatórios')} de mineração "
                                  "por conexão de internet nesta janela; o próximo vai na janela seguinte "
                                  f"({self._wait_window()})",
                                  self._window_left())
        try:
            self._warm(tx)
            spec = self._targeted_spec(tx)
            if spec and not os.path.isfile(self.blob_path(spec)):
                raise TxError("envie o pedido (/blob) antes de criar o job")
            self._admit(tx)
        except TxError as e:
            if window is not None:
                self.quotas.add("work_ok", window, key, -1)   # não entrou: a vaga volta
                # só o recusado gasta a cota baixa: relatório válido nunca é barrado por ela
                if not isinstance(e, MempoolFull) and \
                        self.quotas.add("work_bad", window, key) > self.bad_work_per_ip:
                    raise RateLimited(
                        f"relatório recusado ({e}); esta conexão de internet já passou de "
                        f"{_count(self.bad_work_per_ip, 'relatório', 'relatórios')} de mineração "
                        f"{'recusado' if self.bad_work_per_ip == 1 else 'recusados'} nesta janela: confira se o "
                        "relógio do computador está certo e tente de novo na próxima janela "
                        f"({self._wait_window()})",
                        self._window_left()) from None
            raise
        if gossip:
            for peer in self.peers:
                threading.Thread(target=self._gossip, args=(peer, tx, spec), daemon=True).start()
        return tx["id"]

    def _known(self, txid):
        """Já incluída (nas últimas ~2 h, ou da MetaMask) ou já na fila: reenviar não faz nada."""
        return txid in self.tx_pos or txid in self.mempool_ids

    def _admit(self, tx):
        with self.lock:
            if self._known(tx["id"]):
                return
            if len(self.mempool) >= MAX_MEMPOOL:
                raise MempoolFull("fila de transações cheia; tente de novo em alguns segundos")
            try:
                self.pending.apply_tx(tx)
            except TxError:
                raise
            except Exception as e:  # erro inesperado pode ter deixado o estado pendente pela metade
                self._rebuild_pending()
                raise TxError(f"transação malformada ({e})")
            self.mempool.append(tx)
            self.mempool_ids.add(tx["id"])
            self.dropped.pop(tx["id"], None)

    def _window_left(self):
        """Segundos até a janela de mineração atual acabar (pelo relógio deste nó)."""
        m, p = self.state.mining, self.state.params
        return m["window_start"] + p["window_seconds"] - self.clock()

    def _wait_window(self):
        return _wait_text(self._window_left())

    def _charge_tx(self, key, tx):
        """Cota de transações assinadas da conexão nesta janela. Uma tx da MetaMask grande conta 1 a cada 1 KB
        (o keccak em Python puro custa caro mesmo quando ela é recusada); uma tx assinada do Sigma, 1 a cada 1 KB
        de canon(data) — assim um corpo grande não sai barato antes de ser recusado (achados disk-bloat/sig-cache)."""
        units = 1 + len(tx["data"]["raw"]) // EVM_UNIT if tx["type"] == "evm" else 1 + len(canon(tx["data"])) // 1024
        if self.quotas.add("tx", self.state.mining["window"], key, units, limit=self.tx_per_ip) is None:
            raise RateLimited(f"limite de {_count(self.tx_per_ip, 'transação', 'transações')} por conexão de "
                              f"internet nesta janela; tente de novo {self._wait_window()}", self._window_left())

    def _warm(self, tx):
        """Fora do lock: o que é caro fica no cache que o apply_tx consulta de novo lá dentro (aí sai de graça).
        Assim uma rajada de assinaturas ou de relatórios não segura o lock da produção de blocos."""
        kind = tx["type"]
        if kind == "evm":
            chain.decode_evm(tx["data"]["raw"])   # inválida: TxError, a mesma mensagem que o apply_tx daria
            return
        if kind == "work":
            return self._warm_work(tx["data"])
        # só confere a assinatura (e enche o cache do _sig_ok) de um tipo assinado conhecido com pub/sig no
        # formato HEX128: senão uma rajada de lixo recusado encheria o cache sem nunca virar bloco
        # (achado sig-cache-guarda-lixo). O corpo já está limitado a 32 KB pelo normalize_tx. "Conhecido" é na
        # versão das regras em vigor: um bond antes da v3 é recusado como tipo desconhecido.
        pub, sig = tx.get("pub"), tx.get("sig")
        if kind in chain.signed_types(self.state.rules) and isinstance(pub, str) and chain.HEX128.match(pub) \
                and isinstance(sig, str) and chain.HEX128.match(sig):
            try:
                chain._sig_ok(pub, canon(chain.tx_body(tx)), sig)
            except Exception:
                pass   # o apply_tx dá o erro certo

    def _warm_work(self, d):
        try:
            st, p = self.state, self.state.params
            w, bits, nonces, worker = d.get("window"), d.get("bits"), d.get("nonces"), d.get("worker")
            seed = st.mining["seeds"].get(str(w))
            if not (seed and isinstance(bits, int) and p["min_share_bits"] <= bits <= 200
                    and isinstance(nonces, list) and 0 < len(nonces) <= p["max_shares"]):
                return
            if worker is not None and not (isinstance(worker, str) and WORKER_RE.match(worker)):
                return
            miner = norm_address(d.get("miner"))
            ident = f"{miner}.{worker}" if worker is not None else miner
            # melhor relatório vale: se não supera o que já está guardado, a chain recusa antes do scrypt
            prev = (self.pending.mining.get("last_reports") or {}).get(ident)
            if isinstance(prev, dict) and len(nonces) << bits <= prev.get(str(w), 0):
                return
            for n in nonces:
                if not (isinstance(n, int) and not isinstance(n, bool) and 0 <= n < 2 ** 62) \
                        or not share_ok(seed, ident, n, bits):
                    return
        except Exception:
            return

    def _targeted_job_tx(self, tx):
        """True se a tx é um claim_job/submit_result (nativo ou pela MetaMask) de um job que tem `machine`: a
        máquina é obrigada a mandá-los por causa de jobs direcionados de terceiros, então não contam na cota
        --tx-per-ip (achados targeted-flood-evicts-honest-host / maq-targeted-flood-misses)."""
        kind, data = tx["type"], tx["data"]
        if kind == "evm":
            try:
                e = chain.decode_evm(data["raw"])   # fica no cache para o _warm/apply_tx
                if e["to"] != SYSTEM_ADDRESS:
                    return False
                kind, data = system_call(e["data"])
            except Exception:
                return False
        if kind not in ("claim_job", "submit_result") or not isinstance(data, dict):
            return False
        jid = data.get("job_id")
        if not isinstance(jid, str):
            return False
        job = self.state.jobs.get(jid) or self.pending.jobs.get(jid)
        return bool(isinstance(job, dict) and job.get("machine"))

    def _targeted_spec(self, tx):
        """spec_hash de um create_job de IA direcionado a uma máquina (nativo ou pela MetaMask), senão None."""
        kind, data = tx["type"], tx["data"]
        if kind == "evm":
            try:
                e = chain.decode_evm(data["raw"])   # já está no cache
                if e["to"] != SYSTEM_ADDRESS:
                    return None
                kind, data = system_call(e["data"])
            except Exception:
                return None
        if kind != "create_job" or not isinstance(data, dict):
            return None
        spec = data.get("spec_hash")
        if data.get("kind") == "ai" and data.get("machine") and isinstance(spec, str) and HASH_RE.match(spec):
            return spec
        return None

    def _gossip(self, peer, tx, spec=None):
        try:
            if spec:   # o pedido vai junto: o outro nó também exige o blob antes do job direcionado
                data = self.get_blob(spec)
                if data is not None:
                    req = urllib.request.Request(f"{peer}/blob", data=data,
                                                 headers={"Content-Type": "application/octet-stream"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        netsafe.read_capped(r, 64 * 1024)
            http_json(f"{peer}/tx?nogossip=1", tx)
        except Exception:
            pass

    def tx_status(self, txid):
        with self.lock:
            if txid in self.tx_pos:
                return {"status": "included", "height": self.tx_pos[txid][0]}
            if txid in self.mempool_ids:
                return {"status": "pending"}
            if txid in self.dropped:
                return {"status": "dropped", "error": self.dropped[txid]}
        return {"status": "unknown"}

    # ------------------------------------------------- respostas prontas
    def cached(self, st, key, make):
        """Resposta calculada uma vez por bloco: /jobs?status=open, /machines, /mining... A marca é o estado
        daquele bloco (objeto e altura), sem guardar uma referência que seguraria o estado velho na memória."""
        mark = (id(st), st.height)
        ref, items = self._cache
        if ref != mark:
            items = {}
            self._cache = (mark, items)
        if key not in items:
            items[key] = make()
        return items[key]

    def mining(self, st):
        info = self.cached(st, "mining", lambda: mining_info(st))
        return dict(info, now=time.time())

    # --------------------------------------------------------------- blobs
    def blob_path(self, h):
        return os.path.join(self.blob_dir, h)

    # ------------------------------------------------------------ progresso
    def put_progress(self, msg):
        """POST /progress: {"job_id", "machine", "seq", "data" (hex, cifrado para o cliente), "sig"}. Só a máquina
        que pegou o pedido (ou a escolhida, num direcionado) manda, e só enquanto ele está vivo. Guarda a mais nova
        (seq maior); nada disso entra na chain."""
        if not isinstance(msg, dict):
            raise ValueError("progresso malformado")
        jid, mach, seq, data, sig = (msg.get(k) for k in ("job_id", "machine", "seq", "data", "sig"))
        if not (isinstance(jid, str) and re.fullmatch(r"[0-9a-f]{64}", jid)):
            raise ValueError("job_id inválido")
        if not (isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq < 2 ** 53):
            raise ValueError("seq inválido")
        if not (isinstance(data, str) and re.fullmatch(r"(?:[0-9a-f]{2})*", data)):
            raise ValueError("data deve ser hexadecimal")
        if not isinstance(sig, str):
            raise ValueError("sig inválida")
        try:
            mach = norm_address(mach)
        except (ValueError, TypeError):
            raise ValueError("machine inválida") from None
        st = self.state
        job = st.jobs.get(jid)
        if job is None or job.get("status") not in ("open", "running"):
            raise ValueError("pedido não existe ou já terminou")
        if mach not in (job.get("claims") or {}) and job.get("machine") != mach:
            raise ValueError("esta máquina não está neste pedido")
        pub = (st.machines.get(mach) or {}).get("pub")
        if not pub or not verify(pub, progress_message(jid, seq, data), sig):
            raise ValueError("assinatura do progresso não confere")
        now = time.time()
        with self.lock:
            box = self.progress
            old = box.get(jid)
            if old and old["seq"] >= seq:
                return {"ok": True, "seq": old["seq"]}
            box[jid] = {"machine": mach, "seq": seq, "data": data, "time": now}
            if len(box) > PROGRESS_JOBS or now - self._progress_trim > 60:
                self._progress_trim = now
                for k in [k for k, v in box.items() if now - v["time"] > PROGRESS_KEEP]:
                    del box[k]
                while len(box) > PROGRESS_JOBS:
                    del box[min(box, key=lambda k: box[k]["time"])]
        return {"ok": True, "seq": seq}

    def put_watch(self, msg):
        """POST /watch (regras v7): as cópias para a vigia. {"job_id", "role": "request" (assinada pelo cliente) ou
        "result" (pela máquina que entregou), "sender", "data" (hex, cifrado para a vigia), "sig"}. Só enquanto o
        pedido está em revisão; guarda uma por papel e vigia (a contestação troca a vigia)."""
        if not isinstance(msg, dict):
            raise ValueError("cópia malformada")
        jid, role, sender, data, sig = (msg.get(k) for k in ("job_id", "role", "sender", "data", "sig"))
        if not (isinstance(jid, str) and re.fullmatch(r"[0-9a-f]{64}", jid)) or role not in ("request", "result"):
            raise ValueError("job_id ou role inválido")
        if not (isinstance(data, str) and re.fullmatch(r"(?:[0-9a-f]{2})*", data)) or not isinstance(sig, str):
            raise ValueError("data ou sig inválido")
        try:
            sender = norm_address(sender)
        except (ValueError, TypeError):
            raise ValueError("sender inválido") from None
        job = self.state.jobs.get(jid)
        if job is None or job.get("status") != "review":
            raise ValueError("pedido não está em revisão")
        rv = job.get("review") or {}
        owner = job.get("client") if role == "request" else rv.get("machine")
        if sender != owner:
            raise ValueError("só quem pediu manda o pedido, e só quem entregou manda a entrega")
        watcher = rv.get("watcher")
        if not watcher:
            raise ValueError("este pedido não tem vigia")
        pub = job.get("client_pub") if role == "request" else (self.state.machines.get(sender) or {}).get("pub")
        if not pub or not verify(pub, progress_message(jid + ":" + role + ":" + watcher, 0, data), sig):
            raise ValueError("assinatura não confere")
        with self.lock:
            self.watch[(jid, role, watcher)] = {"data": data, "time": time.time()}
            now = time.time()
            for k in [k for k, v in self.watch.items() if now - v["time"] > PROGRESS_KEEP]:
                del self.watch[k]
        return {"ok": True}

    def get_watch(self, jid, role, watcher):
        with self.lock:
            v = self.watch.get((jid, role, watcher))
            return dict(v) if v else None

    def group_members(self, gid):
        """Relé (v8): {"coordinator": (endereço, pub), "workers": {endereço: pub}} do grupo, ou None."""
        st = self.state
        g = (st.ext.get("groups") or {}).get(gid)
        if g is None:
            return None
        pub = lambda a: (st.machines.get(a) or {}).get("pub")   # noqa: E731
        return {"coordinator": (g["coordinator"], pub(g["coordinator"])),
                "workers": {a: pub(a) for a in g["workers"]}}

    def get_progress(self, jid):
        with self.lock:
            v = self.progress.get(jid)
            return dict(v, age=round(time.time() - v["time"], 1)) if v else None

    def put_blob(self, data, ip=None):
        h = hashlib.sha256(data).hexdigest()
        p = self.blob_path(h)
        n = len(data)
        with self.blob_lock:
            if os.path.exists(p):
                try:
                    os.utime(p)   # enviado de novo: a limpeza não apaga o que alguém acabou de usar
                except OSError:
                    pass
                return h
            if self.blob_cap and self.blob_bytes + n > self.blob_cap:
                # cheio: os pequenos (pedidos e respostas de IA) ainda entram numa folga, para o chat não parar
                if n > SMALL_BLOB or self.blob_bytes + n > self.blob_cap + BLOB_OVERFLOW:
                    raise TxError("o armazenamento deste nó está cheio; a limpeza automática libera espaço a "
                                  "cada hora")
            if self.public and ip is not None:
                now = self.clock()
                if self.quotas.add("blob", int(now // 3600), ip_key(ip), n, limit=self.blob_quota) is None:
                    raise self._blob_limited(now)
            with open(p + ".tmp", "wb") as f:
                f.write(data)
            os.replace(p + ".tmp", p)
            self.blob_bytes += n
        return h

    def _blob_limited(self, now):
        return RateLimited(f"limite de envio de arquivos desta conexão de internet: {self.blob_mb:g} MB por hora; "
                           f"tente de novo {_wait_text(3600 - now % 3600)}", 3600 - now % 3600)

    def check_blob_quota(self, ip, n):
        """Antes de ler o corpo: a conexão que já passou da cota desta hora nem chega a ocupar 20 MB de memória
        (o blob que já existe não gasta cota, mas aí não dá para saber antes de ler)."""
        if self.public and ip is not None:
            now = self.clock()
            if self.quotas.get("blob", int(now // 3600), ip_key(ip)) + n > self.blob_quota:
                raise self._blob_limited(now)

    def get_blob(self, h):
        p = self.blob_path(h)
        if HASH_RE.match(h) and os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    return f.read()
            except OSError:   # a limpeza apagou entre a conferência e a leitura
                return None
        return None

    def gc_blobs(self, now=None):
        """Limpeza dos blobs: fica o que algum site ou job ainda usa (no estado e na fila); o resto sai depois
        de 1 h, tempo para um envio em andamento virar transação. Devolve (quantos saíram, bytes liberados)."""
        now = time.time() if now is None else now
        with self.lock:
            keep = blob_refs(self.state) | blob_refs(self.pending)
        removed = freed = 0
        try:
            with os.scandir(self.blob_dir) as it:
                entries = list(it)
        except OSError:
            return 0, 0
        for e in entries:
            if e.name in keep:
                continue
            with self.blob_lock:   # o mesmo lock do put_blob: quem reenviou agora não perde o arquivo
                try:
                    info = os.stat(e.path)
                    if not stat.S_ISREG(info.st_mode) or now - info.st_mtime < BLOB_GRACE:
                        continue
                    os.remove(e.path)
                except OSError:
                    continue
                self.blob_bytes -= info.st_size
            removed += 1
            freed += info.st_size
        return removed, freed

    def _maybe_gc(self):
        if self._gc_busy or time.time() - self._last_gc < BLOB_GC_EVERY:
            return
        self._gc_busy, self._last_gc = True, time.time()

        def run():
            try:
                removed, freed = self.gc_blobs()
                if removed:
                    print(f"[blobs] limpeza: {removed} arquivo(s) sem uso, {freed / 1e6:,.1f} MB liberados",
                          file=sys.stderr)
            except Exception as e:
                print(f"[blobs] limpeza falhou: {e}", file=sys.stderr)
            finally:
                self._gc_busy = False

        threading.Thread(target=run, name="sigma-blob-gc", daemon=True).start()

    # ---------------------------------------------- leituras de fora (com teto)
    @staticmethod
    def _endpoints(st, addrs):
        """Endereços públicos das máquinas (quem está atrás de NAT registra endpoint vazio). Só lê o estado:
        a conferência de rede interna (DNS) fica para _reachable."""
        out = []
        for a in addrs:
            ep = (st.machines.get(a) or {}).get("endpoint")
            if isinstance(ep, str) and ep.startswith(("http://", "https://")):
                out.append((a, ep.rstrip("/")))
        return out

    def _reachable(self, endpoints):
        """No --public, máquina cujo endpoint aponta para a rede interna do servidor é ignorada."""
        if not self.public:
            return endpoints
        out = []
        for a, ep in endpoints:
            try:
                host = urlparse(ep).hostname
            except ValueError:
                continue
            if host and not netsafe.is_internal(host):
                out.append((a, ep))
        return out

    def _open_machine(self, url, timeout):
        """Conexão com a máquina de outra pessoa. No --public, o IP é conferido de novo na hora de conectar
        (e a cada redirecionamento): DNS que muda de ideia não leva o nó para a rede interna."""
        if self.public:
            return netsafe.safe_opener().open(url, timeout=timeout)
        return urllib.request.urlopen(url, timeout=timeout)

    def _site_blobs(self, st):
        """hash -> (máquinas que hospedam um site com ele, maior tamanho declarado), uma conta por bloco: pedir
        um blob que falta não percorre todos os manifests (nem segura o lock da chain)."""
        def make():
            idx = {}
            for site in st.sites.values():
                if site.get("status") != "active":
                    continue
                pinners = set(site.get("pinners") or {})
                for f in (site.get("manifest") or {}).values():
                    hosts, size = idx.get(f["hash"], (set(), 0))
                    idx[f["hash"]] = (hosts | pinners, max(size, f["size"]))
            return idx
        return self.cached(st, "site_blobs", make)

    def get_blob_any(self, h, ask_peers=True):
        """Blob local ou, se faltar, de um peer ou de uma máquina que hospeda um site com ele (hash conferido).
        Toda leitura de fora tem teto: um peer ou uma máquina não estoura a memória do nó."""
        data = self.get_blob(h)
        if data is not None or not ask_peers or not HASH_RE.match(h):
            return data
        for peer in self.peers:
            try:
                with urllib.request.urlopen(f"{peer}/blob/{h}?local=1", timeout=3) as r:
                    data = netsafe.read_capped(r, MAX_BLOB)
            except Exception:
                continue
            if hashlib.sha256(data).hexdigest() == h:
                try:
                    self.put_blob(data)
                except (TxError, OSError):
                    pass   # sem espaço para guardar: entrega mesmo assim
                return data
        st = self.state      # o estado de um bloco nunca muda depois de gravado: dá para ler sem o lock
        hosts, size = self._site_blobs(st).get(h, ((), 0))
        limit = min(size, MAX_BLOB)
        for _, ep in self._reachable(self._endpoints(st, sorted(hosts))):
            try:
                with self._open_machine(f"{ep}/blob/{h}", timeout=10) as r:
                    data = netsafe.read_capped(r, limit)   # nunca mais que o tamanho declarado
            except Exception:
                continue
            if hashlib.sha256(data).hexdigest() == h:
                return data
        return None

    def fetch_site_file(self, name, path):
        """Busca o arquivo nas máquinas que hospedam o site (hash conferido); por último, no cache do nó."""
        st = self.state
        site = st.sites.get(name)
        if not site or site["status"] != "active":
            return None, None, "site não encontrado ou expirado"
        if path == "" or path.endswith("/"):
            path += "index.html"
        entry = site["manifest"].get(path)
        if not entry:
            return None, None, "arquivo não encontrado"
        limit = min(entry["size"], MAX_BLOB)
        for addr, ep in self._reachable(self._endpoints(st, sorted(site["pinners"]))):
            try:
                with self._open_machine(f"{ep}/blob/{entry['hash']}", timeout=3) as r:
                    data = netsafe.read_capped(r, limit)   # nunca mais que o tamanho declarado
            except Exception:
                continue
            if hashlib.sha256(data).hexdigest() == entry["hash"]:
                return data, path, addr
        data = self.get_blob(entry["hash"])  # cache local do gateway
        return (data, path, "cache-do-gateway") if data is not None else (None, None, "nenhuma máquina respondeu")

    def download_path(self, name):
        """Arquivo de --downloads, só pelo nome (sem pastas nem '..'): o resto do disco fica fora de alcance."""
        if not self.downloads or not DOWNLOAD_RE.match(name) or ".." in name:
            return None
        path = os.path.join(self.downloads, name)
        return path if os.path.isfile(path) else None


def make_handler(node):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", headers=None):
            if not isinstance(body, bytes):
                body = json_bytes(body)
            extra = headers or {}
            # CR ou LF num valor de cabeçalho injetaria outra linha na resposta (ex.: Set-Cookie no domínio
            # oficial): recusa com 500 em vez de repassar (achado site-redirect-crlf-header-injection)
            if any(isinstance(v, str) and ("\r" in v or "\n" in v) for v in (ctype, *extra.values())):
                code, body, ctype, extra = 500, json_bytes({"error": "cabeçalho inválido"}), \
                    "application/json", {}
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_page(self, name):
            try:
                with open(os.path.join(WEB_DIR, name), "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, {"error": "página não encontrada"})
            return self._send(200, data, content_type(name))

        def _send_file(self, path, name):
            """Download em blocos: o instalador nunca é carregado inteiro na memória."""
            try:
                f = open(path, "rb")
            except OSError:
                return self._send(404, {"error": "arquivo não encontrado"})
            with f:
                self.send_response(200)
                # tipo fixo: o mimetypes muda conforme o sistema (no Windows, .pyz vira zip)
                self.send_header("Content-Type", content_type(name) if name.lower().endswith(".txt")
                                 else "application/octet-stream")
                self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if self.command == "HEAD":
                    return
                try:
                    while chunk := f.read(256 * 1024):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # quem baixava desistiu no meio

        def _ip(self):
            """IP de quem chamou. Só confia no X-Forwarded-For vindo do proxy local (nginx), e usa o último
            valor, que é o que o próprio nginx acrescenta (os anteriores o cliente pode inventar)."""
            ip = self.client_address[0]
            fwd = self.headers.get("X-Forwarded-For")
            if fwd and ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                ip = fwd.split(",")[-1].strip() or ip
            return ip

        def _token(self):
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c["sigma_session"].value if "sigma_session" in c else None

        def _session_cookie(self, token, max_age):
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return {"Set-Cookie": f"sigma_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"}

        def _forum_get(self, parts, q):
            f = node.forum
            if parts == ["me"]:
                addr = f.who(self._token())
                return {"user": f.profile(addr) if addr else None}
            if parts == ["categories"]:
                return CATEGORIES
            if parts == ["stats"]:
                return f.stats()
            if parts == ["challenge"]:
                return {"message": f.challenge(q.get("address", [""])[0])}
            if parts == ["threads"]:
                return f.threads(q.get("category", [None])[0])
            if len(parts) == 2 and parts[0] == "thread" and parts[1].isdigit():
                return f.thread(int(parts[1]))
            if len(parts) == 2 and parts[0] == "profile":
                return f.profile(norm_address(parts[1]))
            raise ForumError("rota desconhecida", 404)

        def _forum_post(self, parts, d):
            f = node.forum
            if not isinstance(d, dict):
                raise ForumError("pedido inválido")
            if parts == ["login"]:
                token, user = f.login(d.get("address"), d.get("signature", ""))
                return 200, {"user": user}, self._session_cookie(token, 30 * 86400)
            if parts == ["logout"]:
                f.logout(self._token())
                return 200, {"ok": True}, self._session_cookie("", 0)
            me = f.require(self._token())
            if parts == ["name"]:
                return 200, {"user": f.set_name(me, d.get("name"))}, None
            if parts == ["threads"]:
                return 200, {"id": f.new_thread(me, d.get("category"), d.get("title"), d.get("body"))}, None
            if len(parts) == 3 and parts[0] == "thread" and parts[1].isdigit() and parts[2] == "reply":
                f.reply(me, int(parts[1]), d.get("body"))
                return 200, {"ok": True}, None
            raise ForumError("rota desconhecida", 404)

        def _length(self, limit):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise TxError("Content-Length inválido")
            if n < 0:
                raise TxError("Content-Length inválido")
            if n > limit:
                self._drain(n)
                raise TxError(f"corpo grande demais (máximo {_size_text(limit)})")
            return n

        def _drain(self, n):
            """Lê e descarta o corpo recusado (em pedaços, sem guardar), para quem enviou receber a resposta em
            vez de uma conexão cortada no meio do envio. Corpo absurdo (acima de MAX_DRAIN) não é lido."""
            if n > MAX_DRAIN:
                self.close_connection = True
                return
            while n > 0:
                chunk = self.rfile.read(min(n, 64 * 1024))
                if not chunk:
                    break
                n -= len(chunk)

        def _body(self, limit):
            return self.rfile.read(self._length(limit))

        @staticmethod
        def _json(body):
            try:
                return json.loads(body)
            except (ValueError, RecursionError):
                raise TxError("o corpo do pedido não é um JSON válido") from None

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()  # mesmas respostas, sem corpo (o navegador consulta o tamanho do download)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            parts = [unquote(p) for p in u.path.strip("/").split("/")] if u.path != "/" else []
            st = node.state
            try:
                if not parts or (len(parts) == 1 and parts[0] in PAGES):
                    return self._send_page(PAGES[parts[0] if parts else ""])
                if parts == ["status"]:
                    head, mempool = node.head, len(node.mempool)
                    h = head["height"]
                    past = node.block(max(1, h - 100)) if h > 1 else None
                    body = {"network": "sigma", "version": __version__, "height": h, "head": head["hash"],
                            "producer": bool(node.producer), "mempool": mempool, "peers": node.peers,
                            "machines": node.cached(st, "online", lambda: sum(
                                1 for m in st.machines.values() if m.get("online"))),
                            "jobs": len(st.jobs), "sites": len(st.sites), "models": len(st.models),
                            "chain_id": st.chain_id, "time": head["time"],
                            "block_interval": round((head["time"] - past["time"]) / max(1, min(100, h - 1)), 2)
                            if past else 1,
                            # versão das regras: a desta chain, a mais nova que este programa conhece, as marcadas
                            "genesis": st.genesis_hash, "rules": st.rules, "rules_known": chain.RULES,
                            "upgrades": chain.upcoming_upgrades(st.genesis_hash, h),
                            "upgrade_required": node.upgrade_required,
                            # saldos criados por atualizações (a carteira dos fundadores), públicos
                            "grants": list((st.ext or {}).get("grants") or [])}
                    info = node.mining(st)
                    body.update(supply={k: info[k] for k in SUPPLY_KEYS}, now=info["now"],
                                forum=node.cached(st, "forum", node.forum.stats))
                    return self._send(200, body)
                if parts[:2] == ["forum", "api"]:
                    try:
                        return self._send(200, self._forum_get(parts[2:], q))
                    except ForumError as e:
                        return self._send(e.code, {"error": str(e)})
                if parts[0] == "mining":
                    if len(parts) == 1:
                        return self._send(200, node.mining(st))
                    if parts[1:] == ["epochs"]:
                        return self._send(200, st.epochs[::-1])
                    if len(parts) == 3 and parts[1] == "miner":
                        return self._send(200, miner_info(st, norm_address(parts[2]), node.mining(st)))
                if parts[0] == "governance":
                    if len(parts) == 1:
                        return self._send(200, governance_info(st))
                    if len(parts) == 2:
                        g = proposal_info(st, parts[1])
                        return self._send(200 if g else 404, g or {"error": "votação não existe"})
                if parts[0] == "bonds":   # v3: cauções dos coordenadores (antes da v3, a lista vem vazia)
                    if len(parts) == 1:
                        return self._send(200, node.cached(st, "bonds", lambda: json_bytes(bonds_info(st))))
                    if len(parts) == 2:
                        b = bond_info(st, norm_address(parts[1]))
                        return self._send(200 if b else 404, b or {"error": "esta máquina não tem caução"})
                if parts[0] in ("machines", "providers") and len(parts) == 1:
                    return self._send(200, node.cached(st, "machines", lambda: json_bytes(machines_info(st))))
                if parts[0] == "machine" and len(parts) == 2:
                    m = machine_info(st, norm_address(parts[1]),
                                     node.cached(st, "hosting", lambda: hosting_index(st)))
                    return self._send(200 if m else 404, m or {"error": "máquina não registrada"})
                if parts[0] == "history" and len(parts) == 2:
                    a = norm_address(parts[1])
                    limit = min(max(int(q.get("limit", ["100"])[0]), 1), HISTORY_MAX)
                    with node.lock:
                        items = node.history.get(a, [])[-limit:][::-1]
                    return self._send(200, items)
                if parts[0] == "blocks":
                    return self._send(200, {"blocks": node.blocks_from(int(q.get("from", ["1"])[0]))})
                if parts[0] == "account" and len(parts) == 2:
                    a = norm_address(parts[1])
                    with node.lock:
                        st = node.state
                        body = {"address": a, "eth_address": to_eth(a), "balance": st.balances.get(a, 0),
                                "nonce": st.nonces.get(a, 0), "next_nonce": node.pending.nonces.get(a, 0),
                                "machine": st.machines.get(a), "miner": st.miners.get(a),
                                "points": st.points.get(a, 0)}
                    return self._send(200, body)
                if parts[0] == "jobs":
                    status = q.get("status", [None])[0]

                    def jobs():
                        return json_bytes([j for j in st.jobs.values()
                                           if not status or j["status"] == status][-200:])
                    # as máquinas consultam os abertos a cada poucos segundos: uma conta por bloco
                    return self._send(200, node.cached(st, ("jobs", status), jobs)
                                      if status in (None, "open") else jobs())
                if parts[0] == "watch" and len(parts) == 4:
                    w = node.get_watch(parts[1], parts[2], parts[3])
                    return self._send(200 if w else 404, w or {"error": "sem cópia"})
                if parts[0] == "chat" and len(parts) == 1:
                    after = int(q.get("after", ["0"])[0] or 0)
                    return self._send(200, node.chat_feed(after))
                if parts[0] == "names" and len(parts) == 1:
                    return self._send(200, node.state.ext.get("names") or {})
                if parts[0] == "progress" and len(parts) == 2:
                    p = node.get_progress(parts[1])
                    return self._send(200 if p else 404, p or {"error": "sem notícia deste pedido"})
                if parts[0] == "job" and len(parts) == 2:
                    j = st.jobs.get(parts[1])
                    return self._send(200 if j else 404, j or {"error": "job não existe"})
                if parts[0] == "sites" and len(parts) == 1:
                    return self._send(200, node.cached(st, "sites", lambda: json_bytes(st.sites)))
                if parts[0] == "models":
                    if len(parts) == 2:
                        m = st.models.get(parts[1])
                        return self._send(200 if m else 404, m or {
                            "error": "modelo não existe", "retired_at": st.retired_models.get(parts[1])})
                    return self._send(200, node.cached(st, "models", lambda: json_bytes(st.models)))
                if parts[0] == "tx" and len(parts) == 2:
                    return self._send(200, node.tx_status(parts[1]))
                if parts[0] == "blob" and len(parts) == 2:
                    data = node.get_blob_any(parts[1], ask_peers="local" not in q)
                    return self._send(200, data, "application/octet-stream", BLOB_HEADERS) if data is not None \
                        else self._send(404, {"error": "blob não encontrado"}, headers=BLOB_HEADERS)
                if parts[0] == "downloads" and len(parts) == 2:
                    path = node.download_path(parts[1])
                    return self._send_file(path, parts[1]) if path \
                        else self._send(404, {"error": "arquivo não encontrado"})
                if parts[0] == "site" and len(parts) >= 2:
                    rel = "/".join(parts[2:]) + ("/" if u.path.endswith("/") and len(parts) > 2 else "")
                    if len(parts) == 2 and not u.path.endswith("/"):
                        # só redireciona um nome de site válido, e com o Location escapado (nada de CR/LF no nome)
                        if not chain.SITE_NAME.match(parts[1]):
                            return self._send(404, {"error": "site não encontrado"}, headers=SITE_HEADERS)
                        loc = f"/site/{quote(parts[1], safe='')}/"
                        return self._send(301, b"", headers=dict(SITE_HEADERS, Location=loc))
                    if os.path.splitext(parts[-1])[1].lower() in BLOCKED_SITE_EXT:
                        # um site de terceiros no domínio oficial não entrega executável/instalável
                        return self._send(403, {"error": "este tipo de arquivo não pode ser baixado de um site "
                                                          "publicado na rede"}, headers=SITE_HEADERS)
                    data, path, src = node.fetch_site_file(parts[1], rel)
                    if data is None:
                        return self._send(404, {"error": src}, headers=SITE_HEADERS)
                    return self._send(200, data, content_type(path), dict(SITE_HEADERS, **{"X-Sigma-Served-By": src}))
                if len(parts) == 1 and (name := web_file(parts[0])):
                    return self._send_page(name)
                return self._send(404, {"error": "rota desconhecida"})
            except (TxError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            except ConnectionError:
                pass  # o cliente fechou a conexão: não há a quem responder
            except Exception as e:
                return self._send(500, {"error": str(e)})

        def do_POST(self):
            u = urlparse(self.path)
            try:
                if u.path == "/blob":
                    n = self._length(MAX_BLOB)
                    try:
                        node.check_blob_quota(self._ip(), n)
                    except RateLimited:
                        self._drain(n)
                        raise
                    return self._send(200, {"hash": node.put_blob(self.rfile.read(n), ip=self._ip())})
                if u.path == "/progress":
                    return self._send(200, node.put_progress(self._json(self._body(MAX_PROGRESS))))
                if u.path == "/watch":
                    return self._send(200, node.put_watch(self._json(self._body(MAX_WATCH))))
                body = self._body(MAX_TX)
                if u.path.startswith("/forum/api/"):
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        return self._send(415, {"error": "use Content-Type: application/json"})
                    try:
                        code, payload, headers = self._forum_post(u.path.strip("/").split("/")[2:],
                                                                  self._json(body or b"{}"))
                        return self._send(code, payload, headers=headers)
                    except ForumError as e:
                        return self._send(e.code, {"error": str(e)})
                if u.path == "/rpc":
                    parsed = self._json(body)
                    out = rpc.dispatch(node, parsed, ip=self._ip())
                    if isinstance(out, dict) and (out.get("error") or {}).get("code") == rpc.LIMIT_CODE:
                        wait = max(1, math.ceil(node._window_left()))
                        return self._send(429, out, headers={"Retry-After": str(wait)})
                    out_bytes = json_bytes(out)
                    if len(out_bytes) > MAX_RPC_RESPONSE:   # resposta que amplificaria a memória do produtor
                        rid = parsed.get("id") if isinstance(parsed, dict) else None
                        return self._send(200, {"jsonrpc": "2.0", "id": rid,
                                                "error": {"code": -32603, "message": "resposta grande demais"}})
                    return self._send(200, out_bytes)
                if u.path == "/tx":
                    tx = self._json(body)
                    return self._send(200, {"id": node.submit_tx(tx, gossip="nogossip" not in u.query,
                                                                 ip=self._ip())})
                return self._send(404, {"error": "rota desconhecida"})
            except RateLimited as e:
                return self._send(429, {"error": str(e)}, headers={"Retry-After": str(e.retry_after)})
            except (TxError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            except ConnectionError:
                pass
            except Exception as e:
                return self._send(500, {"error": str(e)})

    return Handler


def _pt(n):
    return f"{n:,}".replace(",", ".")


def _utf8_stdio():
    """Saída em UTF-8 em qualquer console (no Windows e no Git Bash os acentos saíam trocados)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def cmd_init(args):
    os.makedirs(args.dir, exist_ok=True)
    if os.path.exists(os.path.join(args.dir, "chain.jsonl")):
        raise SystemExit(f"já existe uma chain em {args.dir}: para criar uma rede nova use outra pasta "
                         "(ou apague chain.jsonl e snapshot.json)")
    params = dict(GENESIS_PARAMS)
    for key, value in (("epoch_seconds", args.epoch_seconds), ("window_seconds", args.window_seconds),
                       ("min_share_bits", args.min_bits), ("hosting_per_mb_window", args.hosting_per_mb_window)):
        if value is not None:
            params[key] = value
    if params["window_seconds"] < 1 or params["epoch_seconds"] < params["window_seconds"]:
        raise SystemExit("--window-seconds precisa ser >= 1 e --epoch-seconds >= --window-seconds")
    if not 1 <= params["min_share_bits"] <= 200:
        raise SystemExit("--min-bits precisa estar entre 1 e 200")
    if not 1 <= params["hosting_per_mb_window"] <= 100:
        raise SystemExit("--hosting-per-mb-window precisa estar entre 1 e 100 µSIG")
    producer = Wallet.load_or_create(os.path.join(args.dir, "producer.key"))
    genesis = {"network": "sigma", "chain_id": DEFAULT_CHAIN_ID, "time": time.time(),
               "validators": [producer.pub], "alloc": {}, "params": params, "rules": chain.RULES}
    with open(os.path.join(args.dir, "genesis.json"), "w", encoding="utf-8") as f:
        json.dump(genesis, f, indent=2)
    pct = f"{params['mint_bps'] / 100:g}".replace(".", ",")
    print(f"genesis criado em {args.dir}\n  validador: {producer.address}\n"
          f"  regras:    v{chain.RULES} (atualizações marcam-se em chain.SCHEDULE com o hash do genesis: "
          f"{chain.sha(canon(genesis))})\n"
          f"  emissão:   só pela mineração, {pct}% do que falta dos {_pt(params['supply_cap'] // UNIT)} SIG "
          f"a cada {params['epoch_seconds']} s, saindo aos poucos em todo bloco\n"
          f"  janelas:   {params['window_seconds']} s, shares de pelo menos {params['min_share_bits']} bits\n"
          f"  aluguel:   {params['hosting_per_mb_window']} µSIG por MB de disco e {params['ram_per_mb_window']} "
          f"por MB de memória por janela, por cópia, até {1 + params['demand_max_bps'] / 10000:g}x com a rede cheia\n"
          f"  votações:  10 dias, 1 SIG = 1 voto, depósito de {params['proposal_deposit'] // UNIT} SIG")


def cmd_run(args):
    gpath = args.genesis or os.path.join(args.dir, "genesis.json")
    with open(gpath, encoding="utf-8") as f:
        genesis = json.load(f)
    if genesis.get("alloc"):
        raise SystemExit("este genesis é da versão antiga (tem SIG que não veio da mineração). "
                         "Crie uma rede nova: python -m sigma.node init --dir PASTA_NOVA")
    os.makedirs(args.dir, exist_ok=True)
    if args.genesis and not os.path.exists(os.path.join(args.dir, "genesis.json")):
        shutil.copy(gpath, os.path.join(args.dir, "genesis.json"))
    producer = None
    if args.producer:
        kpath = os.path.join(args.dir, "producer.key")
        if not os.path.exists(kpath):
            raise SystemExit(f"{kpath} não existe: o validador é criado pelo init")
        producer = Wallet.load(kpath)
        if producer.pub not in genesis["validators"]:
            raise SystemExit(f"{kpath} não é validadora deste genesis")
    if args.downloads and not os.path.isdir(args.downloads):
        print(f"[sigma] aviso: a pasta de downloads {args.downloads} não existe (por enquanto /downloads dá 404)",
              file=sys.stderr)
    try:
        node = Node(args.dir, genesis, producer, args.peer, heartbeat=args.heartbeat,
                    blob_cap=int(args.blob_cap_gb * 1e9), downloads=args.downloads, public=args.public,
                    work_per_ip=args.work_per_ip, bad_work_per_ip=args.bad_work_per_ip, tx_per_ip=args.tx_per_ip,
                    blob_mb_per_ip_hour=args.blob_mb_per_ip_hour)
    except (ValueError, TxError, KeyError, TypeError) as e:
        # chain de uma versão com outras regras (ex.: v2.0 rodando no nó v2.1): reexecutar dá outro resultado
        raise SystemExit(f"não consegui carregar a chain de {args.dir}: {e}\n"
                         "Se ela foi criada por uma versão anterior do Sigma (as regras mudaram), crie uma rede nova "
                         "em outra pasta (python -m sigma.node init --dir PASTA_NOVA) ou, na VPS, rode o deploy com "
                         "RESET=1.") from None
    threading.Thread(target=node.loop, daemon=True).start()
    if args.relay_port:   # v8: relé dos grupos (coordenador e trabalhadores atrás de roteador se encontram aqui)
        global RELAY_PORT
        RELAY_PORT = args.relay_port
        from .relay import RelayServer
        RelayServer("0.0.0.0", args.relay_port, node.group_members,
                    log=lambda m: print(f"[sigma] {m}", flush=True)).start()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(node))
    role = "produtor" if producer else "seguidor"
    print(f"[sigma] nó {role} em http://{args.host}:{args.port}  altura={node.height}  peers={args.peer}\n"
          f"        site: http://127.0.0.1:{args.port}/  ·  mineração: /mining  ·  MetaMask RPC: "
          f"http://127.0.0.1:{args.port}/rpc (chain id {node.state.chain_id})", flush=True)
    if args.public:
        print(f"        público: por conexão, {args.work_per_ip} relatórios aceitos e {args.bad_work_per_ip} "
              f"recusados por janela, {args.tx_per_ip} transações por janela, {args.blob_mb_per_ip_hour:g} MB "
              "de arquivos por hora", flush=True)
    srv.serve_forever()


def main():
    _utf8_stdio()
    ap = argparse.ArgumentParser(prog="sigma.node")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init", help="cria o genesis de uma rede nova (sem SIG: tudo nasce da mineração)")
    a.add_argument("--dir", required=True)
    a.add_argument("--epoch-seconds", type=int, help="duração de cada época/emissão (padrão 43200 = 12 h)")
    a.add_argument("--window-seconds", type=int, help="duração de cada janela de mineração (padrão 300)")
    a.add_argument("--min-bits", type=int, help="dificuldade mínima de um share (padrão 12)")
    a.add_argument("--hosting-per-mb-window", type=int,
                   help="aluguel em µSIG por MB por janela para cada máquina que guarda (padrão 5)")
    r = sub.add_parser("run")
    r.add_argument("--dir", required=True)
    r.add_argument("--genesis")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8700)
    r.add_argument("--producer", action="store_true", help="este nó assina blocos (validador)")
    r.add_argument("--peer", action="append", default=[])
    r.add_argument("--heartbeat", type=float, default=1.0, help="segundos entre blocos vazios")
    r.add_argument("--relay-port", type=int, default=0,
                   help="porta pública do relé dos grupos (regras v8; 0 = sem relé)")
    r.add_argument("--blob-cap-gb", type=float, default=0, help="limite de armazenamento de blobs (0 = sem limite)")
    r.add_argument("--downloads", help="pasta com os instaladores do app, servidos em /downloads/<arquivo>")
    r.add_argument("--public", action="store_true",
                   help="servidor público: limites por conexão de internet (IP; no IPv6, o bloco /64)")
    r.add_argument("--work-per-ip", type=int, default=WORK_PER_IP,
                   help=f"--public: relatórios de mineração aceitos por IP por janela (padrão {WORK_PER_IP})")
    r.add_argument("--bad-work-per-ip", type=int, default=BAD_WORK_PER_IP,
                   help="--public: relatórios recusados por IP por janela; só os recusados gastam esta cota "
                        f"(padrão {BAD_WORK_PER_IP})")
    r.add_argument("--tx-per-ip", type=int, default=TX_PER_IP,
                   help="--public: transações assinadas por IP por janela, inclusive as da MetaMask; uma da "
                        f"MetaMask grande conta 1 a cada 1 KB (padrão {TX_PER_IP})")
    r.add_argument("--blob-mb-per-ip-hour", type=float, default=BLOB_MB_PER_IP_HOUR,
                   help=f"--public: MB de arquivos novos (blobs) por IP por hora (padrão {BLOB_MB_PER_IP_HOUR})")
    args = ap.parse_args()
    {"init": cmd_init, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
