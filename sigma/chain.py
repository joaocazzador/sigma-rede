"""Ledger do Sigma: transações, blocos e a máquina de estados.

Tudo que envolve dinheiro (saldo, escrow, pagamento, aluguel, emissão) é
decidido aqui, de forma determinística, para que qualquer nó consiga
reexecutar a chain e chegar exatamente no mesmo estado.

O genesis nasce sem saldos. O SIG nasce de duas formas: a emissão contínua
(0,01% do que falta dos 100 milhões a cada 12 h, saindo um pouco em todo
bloco), dividida pelos pontos de prova de trabalho de cada carteira, e, uma vez
só, a carteira dos fundadores (1 milhão de SIG, 1% do teto), criada na altura
da atualização v2 da rede principal (UPGRADE_GRANTS). Depois disso o SIG só
circula: quem usa IA, computação e hospedagem paga as máquinas que hospedam.
Não há taxa nem queima. Nas regras v3, quem coordena a divisão de modelos
gigantes trava SIG como caução (bond): se trapacear, parte dela é cortada, uma
fatia vai para quem provou a fraude, outra para quem foi lesado e o resto volta
para o que falta minerar (sai de `minted` e a emissão o paga de novo).

Ciclo de cada bloco: begin_block (atualização das regras, janela, época,
aluguel, atribuições, limpeza) → apply_tx de cada transação → end_block
(prazos dos jobs, votações e retiradas da caução).

Atualizações sem recomeçar a rede (docs/ATUALIZACOES.md): cada bloco diz a
versão das regras que valia nele. Uma regra nova entra numa altura marcada em
SCHEDULE e o código mantém a antiga para os blocos de antes, então a chain
inteira continua reexecutando igual e ninguém perde saldo.

Formato estrito: cada tipo de transação aceita só os campos do contrato e todo
inteiro tem limite. Cada handler `_tx_*` primeiro valida tudo e só depois
altera o estado, então uma transação inválida nunca deixa o estado pela metade,
e nada que canon() não consiga serializar chega ao estado ou a um bloco.
"""
import copy
import hashlib
import json
import math
import re

from collections import OrderedDict
from functools import lru_cache

from . import evm, tasks
from .crypto import address_of, norm_address, verify
from .pow import report_points, share_ok

UNIT = 1_000_000            # 1 SIG = 1.000.000 µSIG
# ---- atualizações das regras (docs/ATUALIZACOES.md)
RULES = 7                   # a versão mais nova das regras que este código sabe executar
# v1: a rede da v2.4. v2 (v2.5): modelos de imagem e vídeo, preço por imagem e por segundo de vídeo.
# v3 (v2.6): caução dos coordenadores (bond/unbond, corte e reputação).
# v4 (v2.8): modelo de texto em várias partes (GGUF dividido), até 600 GB, para rodar dividido entre computadores
# v5 (v2.10): a máquina que devolve falha num pedido de imagem ou vídeo perde reputação (como a que some); e a
# memória de IA conta só o MAIOR modelo da máquina, não a soma: ela carrega um por vez e troca quando pedem outro
# (o disco continua contando todos)
# v6 (v2.11): o cliente libera (release_job) um pedido direcionado cuja máquina sumiu da rede (não sinalizou nesta
# janela nem na anterior): o valor volta na hora, a máquina leva a falta e o app manda o pedido para a próxima
# v7 (v2.12): corrida. Um pedido direcionado pode chamar até MAX_RACERS máquinas ociosas a mais com o mesmo modelo
# (`racers`: endereço -> pedido cifrado para ela). Todas podem pegar; a primeira que entrega leva o pagamento e a
# reputação, e o pedido fecha. A falha ou o sumiço de uma só tira ela da corrida.
# v7 também traz a VIGIA: a entrega de IA (com ou sem corrida) não paga na hora; o pedido fica "review" e a rede
# sorteia, no bloco da entrega, uma máquina com o mesmo modelo para conferir (review_job). Aprovou: a vencedora
# recebe 95%, a vigia 5% (WATCH_BPS) e ambas ganham reputação. Reprovou: o valor volta e a vencedora perde
# reputação. O cliente pode contestar uma vez (dispute_job): sorteia outra vigia e dá mais REVIEW_SECONDS; havendo
# discordância, vale a vigia. Sem veredito no prazo, paga sozinho (ninguém fica sem receber por falta de vigia).
# atualizações marcadas: hash do genesis da rede -> {versão: altura do primeiro bloco com ela}. Uma rede nova
# já nasce na versão RULES; as entradas daqui só valem para a rede cujo genesis tem aquele hash.
# rede principal (genesis a8eabe25…1029, recriada na 2.4): a v2 (imagem e vídeo, reserva dos fundadores, votações
# decididas antes) entra no bloco 1950, marcado na publicação da 2.5 em 04/11/2026 (~1 h de aviso); a v3 (caução dos
# coordenadores) no bloco 2780, marcado no mesmo dia no bloco 2679 (~40 min de aviso, 24 s por bloco; a primeira
# marcação da 2.6 era o bloco 8000 e foi antecipada antes de qualquer bloco nas regras v3)
# a v4 (modelos de texto em partes, os gigantes) no bloco 4032, marcado no bloco 3882 (~1 h de aviso, 2.8)
# a v5 (reputação na falha de imagem/vídeo, modelos que se revezam na memória) no bloco 4300, marcado no bloco 4236
# (~18 min de aviso, 2.10: o dono pediu o mais rápido possível)
# a v6 (liberar o pedido de uma máquina que sumiu) no bloco 5800, marcado no bloco 5736 (~20 min de aviso, 2.11)
SCHEDULE = {"a8eabe25b19487fdc77c5bf86a812e55c2837e053c20d1aba2bc0794a7bb1029": {2: 1950, 3: 2780, 4: 4032, 5: 4300, 6: 5800}}
UPGRADE_HISTORY_KEEP = 50
# saldos que uma atualização cria na altura dela, uma vez só: hash do genesis -> {versão: [(endereço, µSIG)]}.
# A v2 da rede principal cria a carteira dos fundadores: 1 milhão de SIG (1% do teto) para as primeiras despesas do
# projeto (marketing, administração, infraestrutura). Fica pública no estado (ext["grants"]) e no histórico.
MAINNET_GENESIS = "a8eabe25b19487fdc77c5bf86a812e55c2837e053c20d1aba2bc0794a7bb1029"
FOUNDERS = "sigfd1980713ecf1a514ad3f4a15828bb2d0c00a09e"
UPGRADE_GRANTS = {MAINNET_GENESIS: {2: [(FOUNDERS, 1_000_000 * 1_000_000)]}}
# As chaves do genesis ficam congeladas: um parâmetro novo entra por uma atualização (no estado, na altura dela),
# nunca em DEFAULT_PARAMS — senão o estado do genesis muda e a chain antiga deixa de bater.
# parâmetros que uma versão cria: versão -> {nome: valor inicial}. Entram em `params` na altura dela (ou desde o
# genesis, numa rede que já nasce nela); depois disso se votam como os outros (GOVERNABLE).
UPGRADE_PARAMS = {3: {"min_bond": 1_000 * UNIT}}   # v3: caução mínima para coordenar (1.000 SIG)
DEFAULT_PARAMS = {"epoch_seconds": 43200, "window_seconds": 300, "min_share_bits": 12, "max_shares": 32,
                  "supply_cap": 100_000_000 * UNIT,
                  "mint_bps": 1,                  # 0,01% do que falta a cada 12 h, saindo aos poucos em todo bloco
                  "hosting_per_mb_window": 5,     # aluguel de disco: µSIG por MB por janela, por cópia
                  "ram_per_mb_window": 2,         # aluguel de memória (modelos carregados): µSIG por MB por janela
                  "demand_max_bps": 30000,        # com a rede cheia, o aluguel sobe até 1 + 3 = 4x
                  "proposal_deposit": 10 * UNIT,  # depósito para abrir uma votação (volta no fim)
                  "quorum_bps": 100}              # votação só vale com votos de pelo menos 1% do SIG que existe
DEFAULT_PRICES = {"ai_per_1k_tokens": 2000, "compute_per_unit": 500}
MAX_HOSTING_PRICE = 100     # teto do aluguel da rede (µSIG por MB por janela): sai do orçamento dos outros
VOTE_SECONDS = 10 * 86400   # cada votação dura 10 dias (pelo relógio dos blocos)
MAX_OPEN_PROPOSALS = 20
CLOSED_PROPOSALS_KEEP = 100
CHOICES = ("sim", "nao")
# o que uma votação pode mudar, com limites (o teto de 100 milhões e o tempo das janelas não se votam)
GOVERNABLE = {"mint_bps": (0, 50), "hosting_per_mb_window": (1, MAX_HOSTING_PRICE), "ram_per_mb_window": (0, 100),
              "demand_max_bps": (0, 100_000), "min_share_bits": (8, 32),
              "proposal_deposit": (UNIT, 10_000 * UNIT), "quorum_bps": (0, 5000),
              "min_bond": (10 * UNIT, 1_000_000 * UNIT)}
GOVERNABLE_SINCE = {"min_bond": 3}   # parâmetro que só existe a partir de uma versão: antes dela, não se vota
MAX_AI_PRICE = 1_000_000    # µSIG por mil tokens: nenhuma máquina engole o escrow com um preço absurdo
MAX_COMPUTE_PRICE = 1_000_000
# ---- classes de IA (regras v2): além de texto, modelos que geram imagem e vídeo (stable-diffusion.cpp)
TASKS = ("text", "image", "video")
TASK_PRICE = {"text": "ai_per_1k_tokens", "image": "ai_per_image", "video": "ai_per_video_second"}
MEDIA_PRICES = {"ai_per_image": 20_000, "ai_per_video_second": 100_000}   # padrão: 0,02 SIG/imagem, 0,1 SIG/s
MAX_MEDIA_PRICE = 100_000_000   # µSIG por imagem ou por segundo de vídeo (100 SIG)
MEDIA_UNITS = {"image": 4, "video": 10}            # no máximo 4 imagens ou 10 s de vídeo por pedido
MEDIA_CLAIM_TIMEOUT = {"image": 1800, "video": 7200}   # gerar leva mais que responder texto (no CPU, muito mais)
MEDIA_RAM_MB = {"image": 1536, "video": 4096}      # memória de trabalho além dos pesos (a do texto é 512)
MODEL_ROLES = ("vae", "clip_l", "clip_g", "t5xxl", "llm")   # arquivos extras de um modelo de imagem/vídeo
MAX_MODEL_FILES = 4
# ---- modelos gigantes (regras v4): o GGUF dividido em partes (llama-gguf-split), baixado inteiro pelo coordenador
# do grupo (sigma/grupo.py), que espalha as camadas pela memória dos trabalhadores
PART_ROLE = "part"
MAX_PARTS = 15               # partes além da primeira (o DeepSeek R1 671B tem 9 ao todo)
MAX_PART_SIZE = 100_000_000_000
MAX_SPLIT_SIZE = 600_000_000_000
# ---- caução dos coordenadores (regras v3): quem coordena a divisão de modelos gigantes trava SIG na rede
UNBOND_SECONDS = 14 * 86400  # a retirada espera 14 dias e até lá ainda pode ser cortada (o trapaceiro não saca antes)
MAX_UNBONDING = 20           # retiradas em espera por caução: o estado e a conta de cada bloco não crescem sem limite
SLASH_REPORTER_BPS = 2000    # do que for cortado, 20% vão para quem provou a fraude
SLASH_VICTIM_BPS = 3000      # e até 30% para quem foi lesado (no máximo o prejuízo); o resto volta para a emissão
SLASH_REPUTATION = 10        # reputação que a máquina perde em cada corte
SLASHES_KEEP = 200           # registro público dos últimos cortes (ext["slashes"])
MAX_SHARE_BITS = 200
MAX_NONCE = 2 ** 62
CLAIM_TIMEOUT = 600         # segundos para entregar um job reivindicado
TARGET_TIMEOUT = 120        # segundos para a máquina escolhida reivindicar um pedido direcionado
OPEN_TIMEOUT = 3600         # segundos até um job aberto expirar (ou liquidar o que já entregaram)
CONTEXT = 4096              # teto de tokens de um pedido de IA (o mesmo contexto dos motores)
MAX_TARGET_OPEN = 6         # pedidos direcionados vivos por máquina: acima disso a fila está cheia
MAX_TARGET_PER_CLIENT = 2   # pedidos direcionados vivos de um mesmo cliente para a mesma máquina
JOB_RETENTION = 2 * 86400   # jobs encerrados saem do estado depois disso (ficam no histórico dos blocos)
OFFLINE_WINDOWS = 2         # sem sinal por mais janelas que isso, a máquina fica offline
ASSIGN_WINDOWS = 6          # janelas (no mínimo) para baixar e carregar um modelo atribuído
ASSIGN_BYTES_PER_SECOND = 500_000   # ~4 Mbit/s: modelo grande ganha mais janelas para baixar
MIN_REPUTATION = -10        # daqui para baixo a rede não confia mais modelos à máquina
MAX_MISSES = 3              # faltas em pedidos direcionados (em janelas diferentes) até perder o modelo
COOLDOWN_WINDOWS = 288      # ~1 dia sem receber de novo o modelo que perdeu por faltas
FORMER_WINDOWS = 288        # por ~1 dia o modelo volta primeiro para quem já tem o arquivo
MAX_MODELS_PER_MACHINE = 4  # hospedando + atribuídos
MAX_MODELS_PER_MACHINE_V5 = 8   # v5: os modelos se revezam na memória, o limite é o disco
PRUNE_WINDOWS = 2016        # ~1 semana (janelas de 5 min): o que ficou sem uso sai do estado
RETIRED_KEEP = 7 * 86400    # segundos que um nome apagado fica em retired_models
EPOCH_HISTORY = 60
MAX_MODEL_SIZE = 60_000_000_000
MAX_ROYALTY_BPS = 5000      # o padrinho leva no máximo 50% do que se gasta no modelo
HW_FIELDS = ("cpu_cores", "ram_mb", "ai_ram_mb", "vram_mb", "disk_mb")
HW_MAX = 10 ** 9
MAX_AI_RAM_MB = 1_048_576   # 1 TB: memória "infinita" declarada não faz uma máquina caber todos os modelos
MAX_TOKENS = 10 ** 9
MAX_FILE = 20 * 1024 * 1024  # o MAX_BLOB do nó: tamanho declarado de cada arquivo de site
MAX_SITE_FILES = 200
MAX_PATH = 120
MAX_SITE_PATHS = 16 * 1024  # soma dos caminhos de um site: no máximo 16 KB no estado
MIN_SITE_BUDGET = 10_000    # 0,01 SIG para criar um site
SITE_BUDGET_PER_FILE = 2_000  # cada arquivo do manifest custa mais isso no orçamento mínimo: estado não é de graça
UNPINNED_SITE_WINDOWS = 288  # ~1 dia: site/modelo sem nenhum pinner sai do estado (não espera a semana do _prune)
MIN_FUNDED_SECONDS = 86400  # as máquinas só baixam modelo cujo orçamento paga um dia de aluguel de todas as cópias
FUNDED_ABOVE = 50_000_000   # bytes: até aqui o download é barato e o modelo não precisa pagar esse dia adiantado
# nomes reservados: um site de terceiros em /site/<nome>/ não pode se passar pela página oficial (phishing)
RESERVED_SITE_NAMES = frozenset({"baixar", "download", "downloads", "carteira", "wallet", "forum", "tutorial",
                                 "sigma", "oficial", "suporte", "ajuda", "atualizar", "update", "app", "admin",
                                 "metamask"})
SITE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}\Z")
MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{2,47}\Z")
HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
HEX128 = re.compile(r"^[0-9a-fA-F]{128}\Z")
ADDRESS = re.compile(r"^sig[0-9a-f]{40}\Z")
WORKER = re.compile(r"^[0-9a-f]{1,16}\Z")
URL_RE = re.compile(r"^https?://\S+\Z")

# formato estrito: só estes campos existem (qualquer outro é recusado antes de mexer no estado)
SIGNED_KEYS = frozenset({"type", "data", "nonce", "pub", "sig", "id"})
UNSIGNED_KEYS = frozenset({"type", "data", "id"})          # work e evm
TX_FIELDS = {
    "transfer": {"to", "amount"},
    "register_machine": {"payout", "name", "hw", "endpoint", "prices", "ai", "compute", "pub", "tasks"},
    "ping": set(),
    "claim_job": {"job_id"},
    "submit_result": {"job_id", "result_hash", "usage", "failed"},
    "cancel_job": {"job_id"},
    "release_job": {"job_id"},           # regras v6 (TYPES_SINCE): pedido de uma máquina que sumiu
    "review_job": {"job_id", "verdict"},  # regras v7: o veredito da vigia ("ok" ou "bad")
    "dispute_job": {"job_id"},           # regras v7: o cliente contesta a entrega (sorteia outra vigia)
    "deploy_site": {"name", "manifest", "budget", "replicas"},
    "pin_site": {"name", "version"},
    "register_model": {"name", "url", "sha256", "size", "royalty_bps", "reinvest_bps", "replicas", "license",
                       "description", "budget", "task", "files"},
    "fund_model": {"name", "amount"},
    "update_model": {"name", "royalty_bps", "reinvest_bps", "new_sponsor"},
    "retire_model": {"name"},
    "pin_model": {"name"},
    "decline_model": {"name"},
    "propose": {"kind", "title", "text", "param", "value"},
    "vote": {"proposal_id", "choice", "amount"},
    "bond": {"machine", "amount"},       # regras v3 (TYPES_SINCE): caução de uma máquina
    "unbond": {"machine", "amount"},
    "work": {"miner", "window", "bits", "nonces", "worker"},
    "evm": {"raw"},
}
JOB_FIELDS = {"ai": {"kind", "model", "spec_hash", "escrow", "max_price", "max_tokens", "machine", "client_pub",
                     "units", "racers"},
              "compute": {"kind", "task", "replicas", "escrow", "max_price"}}
SIGNED_TYPES = frozenset(TX_FIELDS) - {"work", "evm"} | {"create_job"}
# tipos que só existem a partir de uma versão das regras: antes dela, são recusados como tipo desconhecido, como
# sempre foram (SIGNED_TYPES sai de TX_FIELDS; sem isto, um tipo novo valeria em qualquer versão)
TYPES_SINCE = {"bond": 3, "unbond": 3, "release_job": 6, "review_job": 7, "dispute_job": 7}
HW_KEYS = frozenset(HW_FIELDS + ("gpu",))
USAGE_KEYS = ("tokens_in", "tokens_out")
LIVE = ("open", "running", "review")  # jobs que ainda seguram escrow ("review" só existe a partir da v7)
WORKING = ("open", "running")          # jobs que ainda aceitam claim e entrega
REVIEW_SECONDS = 1800       # v7: prazo da vigia; sem veredito, a entrega é paga sozinha
WATCH_BPS = 500             # v7: 5% do valor vai para a vigia que aprovou
REDO_ONE_IN = 10            # v7: 1 em cada 10 entregas a vigia refaz o trabalho para comparar
BAD_REPUTATION = 2          # v7: reputação que a vencedora perde quando a vigia reprova
MAX_RACERS = 2              # v7: máquinas a mais que podem correr um pedido direcionado (além da escolhida)


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tx_body(tx):
    return {k: v for k, v in tx.items() if k not in ("sig", "id")}


def tx_id(tx):
    if tx.get("type") == "evm":  # mesmo hash que a MetaMask mostra
        return evm.keccak256(bytes.fromhex(str(tx.get("data", {}).get("raw", "")).removeprefix("0x"))).hex()
    return sha(canon(tx_body(tx)))


@lru_cache(maxsize=4096)
def decode_evm(raw_hex):
    """Decodifica (e recupera o remetente de) uma transação da MetaMask; em cache porque é lento."""
    try:
        return evm.decode_tx(bytes.fromhex(raw_hex.removeprefix("0x")))
    except (ValueError, TypeError, AttributeError, IndexError) as e:   # RLP com listas onde vão bytes etc.
        raise TxError(f"transação Ethereum inválida: {e}")
    except RecursionError:              # RLP com milhares de listas uma dentro da outra
        raise TxError("transação Ethereum inválida: listas aninhadas demais")


def _system_call(data):
    """Operação do Sigma dentro de uma tx da MetaMask; JSON inválido (ou aninhado demais) vira TxError."""
    try:
        return evm.system_call(data)
    except ValueError as e:
        raise TxError(str(e))
    except RecursionError:
        raise TxError("data deve ser um JSON {type, data} sem tantos níveis aninhados")


_SIG_CACHE = OrderedDict()   # (pub, sha(msg), sig) -> bool
_SIG_CACHE_MAX = 8192


def _sig_ok(pub, msg, sig):
    """Mempool e bloco conferem a mesma assinatura: em cache porque ECDSA em Python puro é lento. A chave é
    `(pub, sha(msg), sig)` — o resumo da mensagem, não a mensagem inteira (que vai até 32 KB) — para uma
    rajada de transações grandes recusadas não inchar o cache (achado sig-cache-guarda-lixo, SPEC-v2.2 §4)."""
    key = (pub, sha(msg), sig)
    try:
        ok = _SIG_CACHE[key]
        _SIG_CACHE.move_to_end(key)
        return ok
    except KeyError:
        pass
    ok = _SIG_CACHE[key] = verify(pub, msg, sig)
    if len(_SIG_CACHE) > _SIG_CACHE_MAX:
        _SIG_CACHE.popitem(last=False)
    return ok


def tx_sender(tx):
    """Quem responde pela transação (num relatório de trabalho, o minerador que está no hash)."""
    if tx.get("type") == "evm":
        return decode_evm(tx["data"]["raw"])["from"]
    if tx.get("type") == "work":
        data = tx.get("data")
        return _addr(data.get("miner") if isinstance(data, dict) else None, "minerador")
    return address_of(tx["pub"])


def _try_addr(v):
    try:
        return _addr(v)
    except TxError:
        return None


def _summary_amount(data):
    v = next((data[k] for k in ("amount", "budget", "escrow") if _is_int(data.get(k))), 0)
    return v if 0 <= v <= 10 ** 30 else 0     # tx malformada não leva um número gigante para o histórico


def tx_summary(tx):
    """Resumo legível de qualquer transação: quem, para quem, o quê e quanto (nunca falha, nem com lixo)."""
    tx = tx if isinstance(tx, dict) else {}
    type_ = tx.get("type")
    data = tx.get("data") if isinstance(tx.get("data"), dict) else {}
    sender, nonce, via = None, None, "sigma"
    if type_ == "evm":
        via = "metamask"
        try:
            e = decode_evm(data.get("raw")) if isinstance(data.get("raw"), str) else None
        except TxError:
            e = None
        if e is None:
            type_, data = "inválida", {}
        else:
            sender, nonce = e["from"], e["nonce"] if e["nonce"] < 2 ** 63 else None
            if e["to"] == evm.SYSTEM_ADDRESS:
                try:
                    type_, data = _system_call(e["data"])
                except TxError:
                    type_, data = "inválida", {}
            else:
                type_, data = "transfer", {"to": e["to"], "amount": e["value"] // evm.WEI_PER_USIG}
    elif type_ == "work":  # sem assinatura nem nonce: "quem manda" é o minerador do hash
        sender, nonce = _try_addr(data.get("miner")), 0
    else:
        pub = tx.get("pub")
        if isinstance(pub, str) and HEX128.match(pub):
            sender = address_of(pub)
        nonce = tx.get("nonce") if _is_int(tx.get("nonce")) and 0 <= tx["nonce"] < 2 ** 63 else None
    to = _try_addr(data.get("to")) if type_ == "transfer" else None
    name = data.get("name")
    name = name[:64] if isinstance(name, str) and _utf8_ok(name) else None
    txid = tx.get("id") if isinstance(tx.get("id"), str) and len(tx["id"]) <= 128 else None
    if txid is None:
        try:
            txid = tx_id(tx)
        except (TypeError, ValueError, AttributeError, RecursionError):
            txid = None
    type_ = type_[:40] if isinstance(type_, str) and _utf8_ok(type_) else None
    return {"id": txid, "type": type_, "from": sender, "to": to, "amount": _summary_amount(data), "nonce": nonce,
            "via": via, "name": name}


def _utf8_ok(text):
    try:
        text.encode()
        return True
    except UnicodeEncodeError:
        return False


def block_hash(block):
    return sha(canon({k: v for k, v in block.items() if k not in ("sig", "hash")}))


def fmt(amount):
    return f"{amount / UNIT:,.6f} SIG"


def _pt(n):
    return f"{n:,}".replace(",", ".")      # 1.000.000, como se escreve em português


class TxError(Exception):
    pass


class UpgradeRequired(Exception):
    """A rede passou para uma versão das regras que este programa não conhece: é preciso baixar o Sigma novo.
    Não é erro do bloco: o nó para naquela altura (sem descartar nada) até ser atualizado."""

    def __init__(self, rules, height):
        super().__init__(f"a rede passou para as regras v{rules} no bloco {height}; este programa só conhece até "
                         f"a v{RULES}: baixe a versão nova do Sigma")
        self.rules, self.height = rules, height


def scheduled_rules(genesis_hash, height, current):
    """Versão das regras no bloco `height` pelo que está marcado no código (nunca volta para trás)."""
    rules = current
    for version, at in (SCHEDULE.get(genesis_hash) or {}).items():
        if version > rules and height >= at:
            rules = version
    return rules


def upcoming_upgrades(genesis_hash, height):
    """Atualizações marcadas no código que ainda não chegaram: [{rules, height}] em ordem."""
    return [{"rules": v, "height": at} for v, at in sorted((SCHEDULE.get(genesis_hash) or {}).items())
            if at > height]


def _need(cond, msg):
    if not cond:
        raise TxError(msg)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _pos_int(v, name, allow_zero=False):
    _need(_is_int(v) and (v >= 0 if allow_zero else v > 0),
          f"{name} deve ser inteiro {'>= 0' if allow_zero else '> 0'}")
    return v


def _int_in(v, name, lo, hi):
    _need(_is_int(v) and lo <= v <= hi, f"{name} deve ser inteiro entre {lo} e {hi}")
    return v


def _hex64(v):
    return isinstance(v, str) and HEX64.match(v) is not None


def _text(d, key, limit):
    v = d.get(key, "")
    _need(isinstance(v, str), f"{key} deve ser texto")
    return v[:limit]


def _flag(d, key):
    v = d.get(key, False)
    _need(isinstance(v, bool), f"{key} deve ser verdadeiro ou falso")
    return v


def _addr(v, name="endereço"):
    try:
        a = norm_address(v)
    except (ValueError, TypeError):
        raise TxError(f"{name} inválido")
    _need(ADDRESS.match(a) is not None, f"{name} inválido")   # nada de "_", espaço ou dígito de outro alfabeto
    return a


def _get(table, key):
    """Busca por nome/id vindo da transação (que pode nem ser texto)."""
    return table.get(key) if isinstance(key, str) else None


def _ceil_div(a, b):
    return -(-a // b)        # inteiro: sem arredondamento de float no ledger


def _canon_ok(obj):
    """Texto com surrogate solto, inteiro de milhares de dígitos ou NaN travariam o bloco: recusa na entrada."""
    try:
        canon(obj)
    except (TypeError, ValueError, RecursionError):
        raise TxError("transação malformada: texto inválido ou número grande demais")


def _only(d, allowed, where):
    """Recusa qualquer campo fora do contrato (antes de qualquer mudança no estado)."""
    extra = sorted(str(k) for k in d if k not in allowed)
    _need(not extra, f"campos desconhecidos {where}: {', '.join(k[:40] for k in extra[:10])}")


# campos que só existem a partir de uma versão das regras: antes dela, são recusados como sempre foram
FIELDS_SINCE = {"register_machine": {"tasks": 2}, "register_model": {"task": 2, "files": 2},
                "create_job": {"units": 2, "racers": 7}}


def _fields(type_, d, rules):
    if type_ == "create_job":
        allowed = JOB_FIELDS.get(d.get("kind")) if isinstance(d.get("kind"), str) else None
        _need(allowed is not None, "kind deve ser 'ai' ou 'compute'")
    else:
        allowed = TX_FIELDS[type_]
    since = FIELDS_SINCE.get(type_, {})
    _only(d, {k for k in allowed if since.get(k, 1) <= rules}, f"em {type_}")


def signed_types(rules):
    """Tipos de transação assinada que a versão `rules` das regras aceita (um tipo novo só a partir da dele)."""
    return frozenset(t for t in SIGNED_TYPES if TYPES_SINCE.get(t, 1) <= rules)


def governable(rules):
    """O que uma votação pode mudar na versão `rules` das regras, com os limites (GOVERNABLE_SINCE)."""
    return {k: v for k, v in GOVERNABLE.items() if GOVERNABLE_SINCE.get(k, 1) <= rules}


def model_task(m):
    """Classe do modelo: texto (o padrão, e todo modelo de antes da v2), imagem ou vídeo."""
    return m.get("task", "text")


def _runners(job):
    """Quem pode responder um pedido direcionado: a máquina escolhida e, numa corrida (v7), as extras."""
    m = job.get("machine")
    return ([m] if m else []) + sorted(job.get("racers") or {})


def claim_timeout(job):
    """Prazo para entregar um job reivindicado: imagem e vídeo (v2) demoram mais que texto."""
    return MEDIA_CLAIM_TIMEOUT.get(job.get("media"), CLAIM_TIMEOUT)


def machine_tasks(mc):
    """Classes que a máquina aceita hospedar (antes da v2, só texto)."""
    return mc.get("tasks", ["text"])


def _same_id(tx, txid):
    _need("id" not in tx or tx["id"] == txid, "id da transação não confere com o conteúdo")


def _pub(v, name):
    _need(isinstance(v, str) and HEX128.match(v) is not None, f"{name} deve ter 128 caracteres hexadecimais")
    return v.lower()


def _check_params(genesis):
    over = genesis.get("params") or {}
    if not isinstance(over, dict):
        raise ValueError("genesis inválido: params deve ser um objeto")
    p = dict(DEFAULT_PARAMS, **over)
    ok = (set(p) == set(DEFAULT_PARAMS)
          and all(isinstance(p[k], (int, float)) and not isinstance(p[k], bool) and math.isfinite(p[k]) and p[k] > 0
                  for k in ("epoch_seconds", "window_seconds"))
          and all(_is_int(p[k]) for k in DEFAULT_PARAMS if k not in ("epoch_seconds", "window_seconds"))
          and 1 <= p["min_share_bits"] <= MAX_SHARE_BITS and p["max_shares"] >= 1
          and p["supply_cap"] >= 0 and 0 <= p["mint_bps"] <= 10000
          # só os do genesis: um votável criado por atualização (min_bond, v3) não está em DEFAULT_PARAMS
          and all(lo <= p[k] <= hi for k, (lo, hi) in GOVERNABLE.items()
                  if k in DEFAULT_PARAMS and k not in ("min_share_bits", "mint_bps")))
    if not ok:
        raise ValueError("genesis inválido: params fora do formato esperado")
    return p


def window_seed(prev_hash, window):
    """Seed da janela: muda a cada janela mesmo que nenhum bloco tenha entrado entre duas viradas."""
    return sha(f"{prev_hash}:{window}".encode())


class State:
    # Congeladas: uma chave nova aqui mudaria o state root de todos os blocos antigos. Dado novo de uma atualização
    # vai em `ext` (criado na altura dela) — veja docs/ATUALIZACOES.md.
    STATE_KEYS = ("balances", "nonces", "machines", "jobs", "sites", "models", "retired_models",
                  "mining", "miners", "points", "epochs", "params", "proposals", "upgrade", "ext")

    def __init__(self, genesis):
        if genesis.get("alloc"):
            raise ValueError("genesis inválido: 'alloc' deve ser vazio (nenhum SIG nasce no genesis)")
        self.params = _check_params(genesis)
        rules = genesis.get("rules", 1)
        if not (_is_int(rules) and 1 <= rules):
            raise ValueError("genesis inválido: rules deve ser um inteiro a partir de 1")
        if rules > RULES:
            raise UpgradeRequired(rules, 0)
        for version in range(2, rules + 1):        # rede que já nasce numa versão nova: os parâmetros dela desde já
            self._upgrade_params(version)
        self.genesis_hash = sha(canon(genesis))   # transitório: escolhe as atualizações de SCHEDULE desta rede
        # versão das regras em vigor e quando cada uma entrou (a do genesis no bloco 0)
        self.upgrade = {"rules": rules, "history": [{"rules": rules, "height": 0, "time": genesis["time"]}]}
        self.ext = {}              # dados criados por atualizações futuras (vazio até lá)
        self.chain_id = genesis.get("chain_id", evm.DEFAULT_CHAIN_ID)
        self.balances = {}
        self.nonces = {}
        self.machines = {}
        self.jobs = {}
        self.sites = {}
        self.models = {}
        self.retired_models = {}   # nome -> quando foi apagado
        start = genesis["time"]
        # a seed da janela 0 sai do hash do genesis (o "prev" do bloco 1)
        self.mining = {"window": 0, "window_start": start, "seeds": {"0": window_seed(sha(canon(genesis)), 0)},
                       "epoch": 0, "epoch_start": start, "minted": 0, "last_reports": {},
                       "emit_time": start,          # até onde a emissão contínua já foi paga
                       "open_points": {},           # janela ainda aceitando relatórios -> {endereço: pontos}
                       "rent": {"disk_bps": 10000, "ram_bps": 10000},   # multiplicador da demanda (1x = 10000)
                       "period_paid": 0, "period_rewards": {}}          # estatística do período de 12 h
        self.miners = {}           # endereço -> totais de mineração
        self.points = {}           # base que recebe a emissão agora: pontos da última janela fechada
        self.epochs = []           # resumo de cada período de 12 h (mais antigo primeiro)
        self.proposals = {}        # votações: id -> proposta (as encerradas ficam sem a lista de votos)
        # transitórios: fora de to_dict(), não entram no state root
        self.height, self.now = 0, start
        self.last_settlement = None
        self.credits = []          # [endereço, tipo, µSIG, ref] creditados neste bloco (o nó põe no histórico)

    def to_dict(self):
        return {k: getattr(self, k) for k in self.STATE_KEYS}

    def root(self):
        return sha(canon(self.to_dict()))

    def clone(self):
        return copy.deepcopy(self)

    @classmethod
    def from_dict(cls, genesis, d):
        s = cls(genesis)
        for k in cls.STATE_KEYS:
            if k in d:
                setattr(s, k, d[k])
        return s

    def bal(self, addr):
        return self.balances.get(addr, 0)

    def _credit(self, addr, amount):
        if amount:
            self.balances[addr] = self.bal(addr) + amount

    def _debit(self, addr, amount):
        if amount:
            self.balances[addr] = self.bal(addr) - amount

    def _give(self, addr, amount, kind, ref):
        """Crédito que não vem de transação nem de mineração: anotado para o histórico da carteira."""
        if amount > 0:
            self._credit(addr, amount)
            self.credits.append([addr, kind, amount, ref])

    def next_mint(self):
        """Quanto sai num período de 12 h no ritmo de agora: mint_bps (0,01%) do que falta. Na prática sai aos
        poucos, em todo bloco (_emit)."""
        return (self.params["supply_cap"] - self.mining["minted"]) * self.params["mint_bps"] // 10000

    def copy_fee(self, item):
        """Aluguel de uma cópia por janela, pelo que ela ocupa e pela demanda da rede: disco (sites e modelos) e
        memória (só modelos, que ficam carregados), cada um com o multiplicador da demanda de agora."""
        p, r = self.params, self.mining["rent"]
        cost = item["size"] * p["hosting_per_mb_window"] * r["disk_bps"]
        if "ram_mb" in item:                     # modelo: a máquina reserva a memória enquanto hospeda
            cost += item["ram_mb"] * 1_000_000 * p["ram_per_mb_window"] * r["ram_bps"]
        return max(1, _ceil_div(cost, 1_000_000 * 10_000))

    def rent_window(self, item):
        """Aluguel de todas as cópias de um site/modelo por janela."""
        return item["replicas"] * self.copy_fee(item)

    def _update_demand(self):
        """Na virada da janela: quanto da memória e do disco das máquinas online os modelos e sites pedem.
        u = pedido / capacidade (até 1); o aluguel vale 1 + demand_max * u² (de 1x com a rede vazia a 4x cheia)."""
        ram_need = sum(m["ram_mb"] * m["replicas"] for m in self.models.values())
        disk_need = (sum(m["size"] * m["replicas"] for m in self.models.values())
                     + sum(s["size"] * s["replicas"] for s in self.sites.values() if s["status"] == "active"))
        ram_cap = sum(mc["hw"]["ai_ram_mb"] for mc in self.machines.values() if mc["online"] and mc["ai"])
        disk_cap = sum(mc["hw"]["disk_mb"] * 1_000_000 for mc in self.machines.values() if mc["online"])
        k = self.params["demand_max_bps"]

        def mult(need, cap):
            u = 10_000 if cap <= 0 else min(10_000, need * 10_000 // cap)
            return 10_000 + k * u * u // 100_000_000
        self.mining["rent"] = {"disk_bps": mult(disk_need, disk_cap), "ram_bps": mult(ram_need, ram_cap)}

    def locked(self):
        """SIG travado em votações abertas (votos + depósitos): conta na oferta."""
        return sum(p["deposit"] + p["yes"] + p["no"] for p in self.proposals.values() if p["status"] == "open")

    def bonded(self):
        """SIG travado nas cauções dos coordenadores (regras v3), ativo + retiradas em espera: conta na oferta."""
        return sum(b["amount"] + sum(u["amount"] for u in b["unbonding"])
                   for b in (self.ext.get("bonds") or {}).values())

    def funded(self, m):
        """O orçamento do modelo paga pelo menos MIN_FUNDED_SECONDS de aluguel de todas as cópias? (Modelo até
        FUNDED_ABOVE bytes sempre passa: baixar poucos MB não pesa para ninguém.)"""
        return (m["size"] <= FUNDED_ABOVE
                or m["budget"] // self.rent_window(m) * self.params["window_seconds"] >= MIN_FUNDED_SECONDS)

    def assign_windows(self, m):
        """Prazo para baixar e carregar o modelo: ASSIGN_WINDOWS, ou mais se o arquivo for grande (~4 Mbit/s)."""
        per_window = ASSIGN_BYTES_PER_SECOND * self.params["window_seconds"]
        need = _ceil_div(m["size"], per_window) if _is_int(per_window) else math.ceil(m["size"] / per_window)
        return max(ASSIGN_WINDOWS, need)

    # ------------------------------------------------- versão das regras
    @property
    def rules(self):
        return self.upgrade["rules"]

    def rule(self, version):
        """A regra da versão `version` já vale? É assim que o código escolhe entre a regra nova e a antiga:
        `if self.rule(2): <regra nova> else: <regra de antes>` (os blocos antigos continuam batendo)."""
        return self.upgrade["rules"] >= version

    def _activate_rules(self, height):
        """Chegou a altura de uma atualização marcada: o bloco já roda na versão nova (e fica no histórico)."""
        u = self.upgrade
        target = scheduled_rules(self.genesis_hash, height, u["rules"])
        if target == u["rules"]:
            return
        if target > RULES:                       # só por segurança: SCHEDULE só marca versões que o código tem
            raise UpgradeRequired(target, height)
        old, u["rules"] = u["rules"], target
        u["history"].append({"rules": target, "height": height, "time": self.now})
        del u["history"][1:-UPGRADE_HISTORY_KEEP]
        for version in range(old + 1, target + 1):   # parâmetros (UPGRADE_PARAMS) e saldos (UPGRADE_GRANTS) novos
            self._upgrade_params(version)
            for addr, amount in (UPGRADE_GRANTS.get(self.genesis_hash) or {}).get(version, ()):
                amount = min(amount, self.params["supply_cap"] - self.mining["minted"])   # nunca passa do teto
                if amount > 0:
                    self.mining["minted"] += amount
                    self._give(addr, amount, "founders", f"v{version}")
                    self.ext.setdefault("grants", []).append(
                        {"address": addr, "amount": amount, "rules": version, "height": height})

    def _upgrade_params(self, version):
        """Parâmetros que a versão `version` cria (UPGRADE_PARAMS): entram em `params` na altura dela, nunca em
        DEFAULT_PARAMS. Um valor que já esteja lá (votado) fica."""
        for key, value in UPGRADE_PARAMS.get(version, {}).items():
            self.params.setdefault(key, value)

    # ---------------------------------------------------------- ciclo do bloco
    def begin_block(self, height, time, prev_hash):
        """Abre o bloco: o relógio dele avança janela e época; na virada, a rede se reorganiza."""
        self.height, self.now = height, time
        self.last_settlement = None
        self.credits = []
        self._activate_rules(height)             # antes de tudo: o bloco inteiro já roda nas regras dele
        m, p = self.mining, self.params
        W, E = p["window_seconds"], p["epoch_seconds"]
        self._emit()                             # a fatia de emissão do tempo desde o bloco anterior
        if time >= m["epoch_start"] + E:         # fecha a estatística do período de 12 h (não há liquidação)
            self._close_period()
            k = max(1, int((time - m["epoch_start"]) // E))
            m["epoch"] += k
            m["epoch_start"] += k * E
        if time >= m["window_start"] + W:
            k = max(1, int((time - m["window_start"]) // W))
            seed = m["seeds"].get(str(m["window"]))
            m["window"] += k
            m["window_start"] += k * W
            w = m["window"]
            m["seeds"] = {str(w): window_seed(prev_hash, w)}
            if k == 1 and seed is not None:   # relatórios da janela que acabou de fechar ainda entram
                m["seeds"][str(w - 1)] = seed
            kept = {}
            for ident, reports in m["last_reports"].items():
                recent = {wk: pts for wk, pts in reports.items() if int(wk) >= w - 1}
                if recent:
                    kept[ident] = recent
            m["last_reports"] = kept
            # a janela w-2 não recebe mais relatórios: os pontos dela viram a base que recebe a emissão
            opened = m["open_points"]
            self.points = opened.get(str(w - 2), {})
            m["open_points"] = {wk: pts for wk, pts in opened.items() if int(wk) >= w - 1}
            self._on_window_change()

    def _emit(self):
        """Emissão contínua: cada bloco imprime a fração da taxa de 12 h correspondente ao tempo que passou desde
        o bloco anterior (a média exata, não importa quantos blocos saem), dividida pelos pontos da base."""
        m, p = self.mining, self.params
        # produtor parado: o tempo sem blocos além de 2 janelas não emite (ninguém conseguiu relatar trabalho)
        dt_ms = min(round((self.now - m["emit_time"]) * 1000), round(2 * p["window_seconds"] * 1000))
        if dt_ms <= 0:
            return
        m["emit_time"] = self.now
        total = sum(self.points.values())
        if total <= 0:                           # sem trabalho, nada é impresso (o tempo passa)
            return
        mint = ((p["supply_cap"] - m["minted"]) * p["mint_bps"] * dt_ms
                // (10_000 * max(1, round(p["epoch_seconds"] * 1000))))
        paid = 0
        for addr, pts in sorted(self.points.items()):
            r = mint * pts // total              # o resto do arredondamento continua "por imprimir"
            if r > 0:
                paid += r
                self._give(addr, r, "mining", m["epoch"])
                rec = self.miners.get(addr)
                if rec is not None:
                    rec["rewards_total"] += r
                m["period_rewards"][addr] = m["period_rewards"].get(addr, 0) + r
        m["minted"] += paid
        m["period_paid"] += paid

    def _close_period(self):
        """Fim de um período de 12 h: só estatística (quanto saiu, para quantos e os 10 maiores)."""
        m, p = self.mining, self.params
        rewards = m["period_rewards"]
        top = sorted(rewards.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        self.epochs.append({"epoch": m["epoch"], "start": m["epoch_start"], "end": m["epoch_start"] + p["epoch_seconds"],
                            "mint": m["period_paid"], "miners": len(rewards), "top": [[a, r] for a, r in top]})
        del self.epochs[:-EPOCH_HISTORY]
        m["period_paid"], m["period_rewards"] = 0, {}

    def _on_window_change(self):
        w = self.mining["window"]
        for item in [*self.models.values(), *self.sites.values()]:   # última janela com alguém hospedando
            if item["pinners"]:
                item["pinned_window"] = w
        # a limpeza vem antes de tirar quem sumiu: se o produtor ficar parado por mais de uma semana, o primeiro
        # bloco não apaga de uma vez os modelos e sites que estavam no ar, nem as máquinas que os guardavam (eles
        # ganham uma janela para a máquina sinalizar e voltar a hospedar)
        self._prune()
        offline = [a for a in sorted(self.machines)
                   if self.machines[a]["online"] and self.machines[a]["last_seen"] < w - OFFLINE_WINDOWS]
        if offline:                              # índice máquina -> modelos atribuídos, montado uma vez por janela
            assigned_index = {}                  # (em vez de percorrer todos os modelos para cada máquina)
            for name in self.models:
                for a in self.models[name]["assigned"]:
                    assigned_index.setdefault(a, []).append(name)
            for a in offline:                    # quem parou de sinalizar sai de tudo que hospedava
                self.machines[a]["online"] = False
                self._unhost(a, assigned_index=assigned_index)
        self._update_demand()                    # o preço do aluguel desta janela segue a demanda de agora
        self._charge_rent()                      # uma vez por virada de janela
        self._expire_assignments()
        self._assign_models()

    def _expire_assignments(self):
        """Atribuição não cumprida a tempo volta para a rede. Ninguém perde reputação (recusar um arquivo ruim
        não pode custar caro, senão vira arma de Sybil): a máquina que venceu entra em cooldown daquele modelo,
        para a rede não insistir no mesmo arquivo com a mesma máquina, e a vaga é liberada. Se ninguém hospeda,
        o modelo soma `failed` e, na terceira vez, sai da rede."""
        w = self.mining["window"]
        for name in sorted(self.models):
            m = self.models[name]
            limit = self.assign_windows(m)
            late = [a for a in sorted(m["assigned"]) if w - m["assigned"][a] > limit]
            if not late:
                continue
            for a in late:
                del m["assigned"][a]
                m["cooldown"][a] = w + COOLDOWN_WINDOWS   # não reatribui o mesmo arquivo à mesma máquina
            if not m["pinners"]:
                m["failed"] += 1
                if m["failed"] >= 3:
                    self._delete_model(name)

    def _prune(self):
        """Limpeza do estado: sai o que ficou sem uso. Modelo ou site ativo sem nenhum pinner sai em ~1 dia
        (UNPINNED_SITE_WINDOWS): estado não é de graça. Site expirado (ainda com orçamento) espera a semana do
        PRUNE_WINDOWS para o dono recarregar com o mesmo nome; máquinas e mineradores parados, idem."""
        w = self.mining["window"]
        cut = w - PRUNE_WINDOWS
        unpinned_cut = w - UNPINNED_SITE_WINDOWS
        for name in sorted(self.sites):
            s = self.sites[name]
            if ((s["status"] == "expired" and s.get("expired_window", w) < cut)
                    or (s["status"] == "active" and not s["pinners"] and s["pinned_window"] < unpinned_cut)):
                self._give(s["owner"], s["budget"], "budget", name)   # o que sobrou volta para o dono
                del self.sites[name]
        for name in sorted(self.models):
            m = self.models[name]
            if not m["pinners"] and m["pinned_window"] < unpinned_cut:
                self._delete_model(name)
        for m in self.models.values():
            m["cooldown"] = {a: until for a, until in m["cooldown"].items() if until > w}
            self._trim_former(m)
        busy = set()
        for m in self.models.values():
            busy.update(m["pinners"], m["assigned"])
        for s in self.sites.values():
            busy.update(s["pinners"])
        for j in self.jobs.values():
            if j["status"] in LIVE:
                busy.update(j["claims"])
                if j.get("machine"):
                    busy.add(j["machine"])
        for a in sorted(self.machines):
            if w - self.machines[a]["last_seen"] > PRUNE_WINDOWS and a not in busy:
                del self.machines[a]
        scoring = set(self.points).union(*self.mining["open_points"].values())
        for a in sorted(self.miners):
            if self.miners[a]["last_window"] < cut and a not in scoring:
                del self.miners[a]
        for name in sorted(self.retired_models):
            if self.now - self.retired_models[name] > RETIRED_KEEP:
                del self.retired_models[name]

    def end_block(self):
        """Fecha o bloco: prazos dos jobs (pelo relógio do bloco), votações, retiradas da caução e limpeza."""
        now = self.now
        for jid in [i for i, j in self.jobs.items()
                    if j["status"] not in LIVE and now - j.get("closed", j["created"]) > JOB_RETENTION]:
            del self.jobs[jid]
        for jid in sorted(self.jobs):
            job = self.jobs[jid]
            if job["status"] not in LIVE:
                continue
            if job["status"] == "review":
                if now > job["review"]["until"]:
                    self._settle_review(job, None)    # sem veredito: paga sozinho
                continue
            if job.get("racers"):
                self._race_deadline(job)
                continue
            if job.get("machine"):
                self._target_deadline(job)
                continue
            for mach, claim in sorted(job["claims"].items()):
                if mach not in job["results"] and now - claim["time"] > claim_timeout(job):
                    del job["claims"][mach]       # sumiu sem entregar: perde reputação e o job reabre
                    self.machines[mach]["reputation"] -= 1
                    job["status"] = "open"
            if job["status"] == "open" and now - job["created"] > OPEN_TIMEOUT:
                if job["results"]:                # computação parcial: paga a maioria, se houver, e devolve o resto
                    self._settle_compute(job)
                else:
                    self._refund(job, "expired")
        self._close_proposals()
        if self.rule(3):
            self._release_bonds()

    def _target_deadline(self, job):
        """Pedido direcionado: a máquina escolhida tem TARGET_TIMEOUT para reivindicar e CLAIM_TIMEOUT para
        entregar. Passou do prazo: o cliente recebe tudo de volta (o job não reabre) e a máquina leva falta."""
        claim = job["claims"].get(job["machine"])
        start, limit = (job["created"], TARGET_TIMEOUT) if claim is None else (claim["time"], claim_timeout(job))
        if self.now - start > limit:
            job["missed"] = True
            self._refund(job, "expired")
            self._miss(job)

    def _miss(self, job, addr=None):
        """Falta da máquina num pedido direcionado. Uma nova falta só conta se a máquina sinalizou depois da
        anterior (`last_seen > miss_window`): o PC que dormiu leva uma falta só até acordar, e quem sinaliza
        toda janela sem responder perde o modelo na MAX_MISSES-ésima, ficando COOLDOWN_WINDOWS sem recebê-lo.
        Também não conta se a máquina entregou ou falhou algum pedido direcionado nesta janela (`answered_window`):
        ela está viva, só ocupada — um cliente não evita isso inundando a fila (ver MAX_TARGET_OPEN)."""
        m = self.models.get(job["model"])
        addr, w = addr or job["machine"], self.mining["window"]
        pin = m["pinners"].get(addr) if m else None
        mc = self.machines.get(addr)
        if pin is None or mc is None or mc["last_seen"] <= pin["miss_window"]:
            return
        if mc.get("answered_window", -1) >= w:
            return
        pin["misses"] += 1
        pin["miss_window"] = w
        mc["reputation"] -= 1
        if pin["misses"] >= MAX_MISSES:
            del m["pinners"][addr]
            if m["name"] in mc["models"]:
                mc["models"].remove(m["name"])
            m["cooldown"][addr] = w + COOLDOWN_WINDOWS

    # ------------------------------------------------------------ transações
    def apply_tx(self, tx):
        """Aplica uma transação no bloco aberto (usa self.height e self.now)."""
        _need(isinstance(tx, dict) and isinstance(tx.get("data"), dict), "transação malformada")
        _canon_ok(tx)
        type_ = tx.get("type")
        _only(tx, UNSIGNED_KEYS if type_ in ("work", "evm") else SIGNED_KEYS, "na transação")
        if type_ == "work":                      # sem assinatura: o endereço está dentro de cada hash
            _same_id(tx, tx_id(tx))
            return self._apply_work(tx["data"])
        if type_ == "evm":
            return self._apply_evm(tx)
        _need(isinstance(type_, str) and type_ in signed_types(self.rules),   # bond/unbond só a partir da v3
              f"tipo de transação desconhecido: {str(type_)[:40]}")
        _fields(type_, tx["data"], self.rules)
        pub, sig = tx.get("pub"), tx.get("sig")
        _need(isinstance(pub, str) and HEX128.match(pub) and isinstance(sig, str) and HEX128.match(sig)
              and _sig_ok(pub, canon(tx_body(tx)), sig), "assinatura inválida")
        txid = tx_id(tx)
        _same_id(tx, txid)
        sender = address_of(pub)
        expected = self.nonces.get(sender, 0)
        _need(_is_int(tx.get("nonce")) and tx["nonce"] == expected, f"nonce inválido (esperado {expected})")
        getattr(self, "_tx_" + type_)(sender, tx["data"], txid)
        self.nonces[sender] = expected + 1

    def _apply_evm(self, tx):
        """Transação assinada pela MetaMask: transferência comum ou operação do Sigma (via SYSTEM_ADDRESS)."""
        _only(tx["data"], TX_FIELDS["evm"], "em evm")
        raw = tx["data"].get("raw")
        _need(isinstance(raw, str) and len(raw) < 200_000, "raw inválido")
        e = decode_evm(raw)
        txid = e["hash"]                         # = tx_id(tx), já calculado no decode em cache (keccak é caro)
        _same_id(tx, txid)
        _need(e["chain_id"] == self.chain_id, f"chain id errado (a rede Sigma usa {self.chain_id})")
        sender = e["from"]
        expected = self.nonces.get(sender, 0)
        _need(e["nonce"] == expected, f"nonce inválido (esperado {expected})")
        if e["to"] == evm.SYSTEM_ADDRESS:
            _need(e["value"] == 0, "operações do Sigma não levam valor; use os campos da própria operação")
            type_, data = _system_call(e["data"])
            _canon_ok([type_, data])             # o JSON veio de dentro do raw: confere de novo aqui
            _need(type_ in signed_types(self.rules), f"operação desconhecida: {type_[:40]}")
            _fields(type_, data, self.rules)
            getattr(self, "_tx_" + type_)(sender, data, txid)
        else:
            _need(e["to"] is not None, "criação de contratos não é suportada")
            _need(e["value"] % evm.WEI_PER_USIG == 0, "o valor deve ter no máximo 6 casas decimais")
            self._tx_transfer(sender, {"to": e["to"], "amount": e["value"] // evm.WEI_PER_USIG}, txid)
        self.nonces[sender] = expected + 1

    def _apply_work(self, d):
        """Relatório de trabalho de uma janela: vale o melhor relatório de cada identidade, e só a diferença
        entra nos pontos. Quem manda um relatório pelo endereço de outro só consegue aumentar os pontos dele."""
        m, p = self.mining, self.params
        _only(d, TX_FIELDS["work"], "em work")
        miner = _addr(d.get("miner"), "minerador")
        worker = d.get("worker")
        _need(worker is None or (isinstance(worker, str) and WORKER.match(worker) is not None),
              "worker deve ter de 1 a 16 caracteres [0-9a-f]")
        ident = miner if worker is None else f"{miner}.{worker}"   # cada PC da mesma carteira conta separado
        window = d.get("window")
        _need(_is_int(window) and str(window) in m["seeds"], "janela de mineração inválida ou já encerrada")
        bits = d.get("bits")
        _need(_is_int(bits) and p["min_share_bits"] <= bits <= MAX_SHARE_BITS,
              f"bits deve estar entre {p['min_share_bits']} e {MAX_SHARE_BITS}")
        nonces = d.get("nonces")
        _need(isinstance(nonces, list) and 1 <= len(nonces) <= p["max_shares"],
              f"o relatório deve ter de 1 a {p['max_shares']} shares")
        _need(all(_is_int(n) and 0 <= n < MAX_NONCE for n in nonces), "nonce de share inválido")
        _need(len(set(nonces)) == len(nonces), "nonces repetidos no relatório")
        new = report_points(len(nonces), bits)
        old = m["last_reports"].get(ident, {}).get(str(window), 0)
        _need(new > old, "esta janela já tem um relatório com tantos pontos ou mais deste minerador")   # antes do scrypt
        seed = m["seeds"][str(window)]
        _need(all(share_ok(seed, ident, n, bits) for n in nonces), "share inválido (não atinge a dificuldade)")
        delta = new - old
        win = m["open_points"].setdefault(str(window), {})   # vira base quando a janela fechar para relatórios
        win[miner] = win.get(miner, 0) + delta
        m["last_reports"].setdefault(ident, {})[str(window)] = new
        rec = self.miners.setdefault(miner, {"points_total": 0, "rewards_total": 0, "last_window": window,
                                             "last_rate": 0})
        rec["points_total"] += delta
        rate = delta / p["window_seconds"]       # estimativa de hashes/s
        if window > rec["last_window"]:
            rec["last_window"], rec["last_rate"] = window, rate
        elif window == rec["last_window"]:       # outros PCs (ou um relatório melhor) da mesma janela somam
            rec["last_rate"] += rate

    def _tx_transfer(self, sender, d, txid):
        amount = _int_in(d.get("amount"), "amount", 1, self.params["supply_cap"])
        to = _addr(d.get("to"), "endereço de destino")
        _need(self.bal(sender) >= amount, "saldo insuficiente")
        self._debit(sender, amount)
        self._credit(to, amount)

    # --------------------------------------------------------------- máquinas
    def _tx_register_machine(self, sender, d, txid):
        """Assinada pela chave da máquina (o app gera, o usuário nunca vê); o dinheiro vai para `payout`."""
        payout = _addr(d.get("payout"), "payout")
        name = d.get("name", "")
        # recusa texto grande em vez de truncar calado: o bloco gravado no disco não carrega o nome gigante
        # (achado disk-bloat-nome-grande). register_machine é grátis, então o estado e o disco não saem de graça.
        _need(isinstance(name, str), "name deve ser texto")
        _need(len(name) <= 40, "name deve ter no máximo 40 caracteres")
        hw = d.get("hw", {})
        _need(isinstance(hw, dict), "hw inválido")
        _only(hw, HW_KEYS, "em hw")
        clean_hw = {k: _int_in(hw.get(k, 0), f"hw.{k}", 0, MAX_AI_RAM_MB if k == "ai_ram_mb" else HW_MAX)
                    for k in HW_FIELDS}
        gpu = hw.get("gpu", "")
        _need(isinstance(gpu, str), "hw.gpu deve ser texto")
        _need(len(gpu) <= 80, "hw.gpu deve ter no máximo 80 caracteres")
        clean_hw["gpu"] = gpu
        endpoint = d.get("endpoint", "")
        _need(isinstance(endpoint, str) and len(endpoint) <= 200 and (endpoint == "" or URL_RE.match(endpoint)),
              "endpoint deve ser vazio ou uma URL http(s)")
        prices = d.get("prices", {})
        _need(isinstance(prices, dict), "prices inválido")
        media = self.rule(2)                       # v2: preço por imagem e por segundo de vídeo
        _only(prices, {**DEFAULT_PRICES, **(MEDIA_PRICES if media else {})}, "em prices")   # o aluguel é da rede
        ai_price = prices.get("ai_per_1k_tokens", DEFAULT_PRICES["ai_per_1k_tokens"])
        _need(_is_int(ai_price) and 1 <= ai_price <= MAX_AI_PRICE,
              f"ai_per_1k_tokens deve ser no máximo {_pt(MAX_AI_PRICE)} µSIG por mil tokens (e pelo menos 1)")
        compute_price = prices.get("compute_per_unit", DEFAULT_PRICES["compute_per_unit"])
        _need(_is_int(compute_price) and 1 <= compute_price <= MAX_COMPUTE_PRICE,
              f"compute_per_unit deve ser no máximo {_pt(MAX_COMPUTE_PRICE)} µSIG por unidade (e pelo menos 1)")
        clean_prices = {"ai_per_1k_tokens": ai_price, "compute_per_unit": compute_price}
        if media:
            for key, default in MEDIA_PRICES.items():
                clean_prices[key] = prices.get(key, default)
                _need(_is_int(clean_prices[key]) and 1 <= clean_prices[key] <= MAX_MEDIA_PRICE,
                      f"{key} deve ser no máximo {_pt(MAX_MEDIA_PRICE)} µSIG (e pelo menos 1)")
        ai, compute = _flag(d, "ai"), _flag(d, "compute")
        pub = d.get("pub")
        if pub is not None:   # a chave que recebe os pedidos cifrados
            pub = _pub(pub, "pub")
            _need(address_of(pub) == sender, "pub não corresponde à chave da máquina")
        offer = {"payout": payout, "name": name, "hw": clean_hw, "ai": ai, "compute": compute,
                 "endpoint": endpoint, "prices": clean_prices}
        if media:   # classes de IA que a máquina aceita hospedar: texto, imagem e/ou vídeo. Sem o motor de texto
            # (o Ollama fechado), ela fica só com imagem e vídeo, que rodam no stable-diffusion.cpp
            tasks_ = d.get("tasks", ["text"])
            _need(isinstance(tasks_, list) and len(tasks_) <= len(TASKS) and all(t in TASKS for t in tasks_)
                  and len(set(tasks_)) == len(tasks_), "tasks deve ser uma lista com 'text', 'image' e/ou 'video'")
            _need(tasks_ or not ai, "com a IA ligada, tasks precisa ter pelo menos uma classe")
            offer["tasks"] = [t for t in TASKS if t in tasks_]
        seen = {"last_seen": self.mining["window"], "online": True}
        mc = self.machines.get(sender)
        if mc is None:
            self.machines[sender] = {**offer, "pub": pub, "since": self.now, **seen, "models": [], "jobs_done": 0,
                                     "jobs_failed": 0, "earned": 0, "reputation": 0, "rep_window": -1,
                                     "compute_disputes": 0, "answered_window": -1}
            return
        if not ai:   # desligou a IA: a rede tira os modelos daqui e realoca
            self._unhost(sender, sites=False)
        elif media:  # deixou de aceitar uma classe (texto, imagem ou vídeo): só os modelos dela saem daqui
            self._unhost_tasks(sender, offer["tasks"])
        mc.update(offer)
        if pub is not None:
            mc["pub"] = pub
        mc.update(seen)

    def _tx_ping(self, sender, d, txid):
        mc = self.machines.get(sender)
        _need(mc is not None, "máquina não registrada")
        w = self.mining["window"]
        _need(mc["last_seen"] != w, "já sinalizou nesta janela")
        mc.update({"last_seen": w, "online": True})

    def _unhost(self, addr, sites=True, assigned_index=None):
        """Tira a máquina de tudo que ela guardava; a próxima atribuição realoca (e prefere quem já teve o arquivo).
        Usa `mc["models"]` (o que ela hospeda) para os pinners e, para as atribuições, um índice montado uma vez
        por janela — ou, sem índice, uma varredura (um único _unhost avulso, não o laço da virada)."""
        w = self.mining["window"]
        for name in list(self.machines[addr]["models"]):   # pinners: só os modelos que ela de fato hospeda
            m = self.models.get(name)
            if m is not None and m["pinners"].pop(addr, None) is not None:
                m["former"][addr] = w
                self._trim_former(m)
        assigned = assigned_index.get(addr, ()) if assigned_index is not None \
            else [n for n in sorted(self.models) if addr in self.models[n]["assigned"]]
        for name in assigned:
            m = self.models.get(name)
            if m is not None:
                m["assigned"].pop(addr, None)
        if sites:
            for s in self.sites.values():
                s["pinners"].pop(addr, None)
        self.machines[addr]["models"] = []

    def _unhost_tasks(self, addr, tasks_):
        """Tira da máquina os modelos (hospedados e atribuídos) de classes que ela não aceita mais."""
        mc, w = self.machines[addr], self.mining["window"]
        for name in sorted(self.models):
            m = self.models[name]
            if model_task(m) in tasks_:
                continue
            m["assigned"].pop(addr, None)
            if m["pinners"].pop(addr, None) is not None:
                m["former"][addr] = w
                self._trim_former(m)
            if name in mc["models"]:
                mc["models"].remove(name)

    def _trim_former(self, m):
        """Ficam só as entradas do último dia, no máximo 2 × réplicas (as mais novas)."""
        w = self.mining["window"]
        recent = sorted(((win, a) for a, win in m["former"].items() if win >= w - FORMER_WINDOWS),
                        key=lambda x: (-x[0], x[1]))
        m["former"] = {a: win for win, a in recent[:2 * m["replicas"]]}

    def _pay_machine(self, addr, amount, ref):
        mc = self.machines[addr]
        self._give(mc["payout"], amount, "rent", ref)
        mc["earned"] += amount

    def _usage(self):
        """RAM de IA, disco e número de modelos já comprometidos por máquina (o que hospeda + o que está baixando).
        A partir da v5 a RAM é a do maior modelo (a máquina troca de modelo conforme os pedidos); antes, a soma."""
        ram, disk, count = {}, {}, {}
        swap = self.rule(5)
        for m in self.models.values():
            for a in [*m["pinners"], *m["assigned"]]:
                ram[a] = max(ram.get(a, 0), m["ram_mb"]) if swap else ram.get(a, 0) + m["ram_mb"]
                disk[a] = disk.get(a, 0) + m["size"]
                count[a] = count.get(a, 0) + 1
        for s in self.sites.values():
            for a in s["pinners"]:
                disk[a] = disk.get(a, 0) + s["size"]
        return ram, disk, count

    # ------------------------------------------------------------------ jobs
    def _tx_create_job(self, sender, d, txid):
        kind = d.get("kind")
        _need(kind in ("ai", "compute"), "kind deve ser 'ai' ou 'compute'")
        cap = self.params["supply_cap"]
        escrow = _int_in(d.get("escrow"), "escrow", 1, cap)
        max_price = _int_in(d.get("max_price"), "max_price", 1, cap)
        job = {"id": txid, "client": sender, "kind": kind, "status": "open", "created": self.now,
               "window": self.mining["window"], "escrow": escrow, "max_price": max_price, "claims": {},
               "results": {}, "payments": {}, "result_hash": None}
        if kind == "ai":
            model = _get(self.models, d.get("model"))
            _need(model is not None, "modelo não existe na rede (ou foi apagado)")
            _need(_hex64(d.get("spec_hash")), "spec_hash inválido")
            media = model_task(model)
            if media == "text":
                _need("units" not in d, "pedido de texto usa max_tokens, não units")
                # max_tokens é público e o escrow tem que cobri-lo: a máquina honesta não trabalha de graça sem
                # saber (achado escrow-need-not-cover-price). +16 é o que a conversa soma por pedido.
                max_tokens = _int_in(d.get("max_tokens"), "max_tokens", 1, CONTEXT)
                need = _ceil_div((max_tokens + 16) * max_price, 1000)
                _need(escrow >= need,
                      f"o valor reservado precisa cobrir {max_tokens} tokens a {max_price} µSIG por mil "
                      f"({_pt(need)} µSIG)")
                job.update({"model": d["model"], "spec_hash": d["spec_hash"], "replicas": 1,
                            "max_tokens": max_tokens})
            else:   # v2: imagem (units = quantas imagens) ou vídeo (units = segundos), preço por unidade
                _need("max_tokens" not in d, f"pedido de {media} usa units, não max_tokens")
                what = "imagens" if media == "image" else "segundos de vídeo"
                units = _int_in(d.get("units"), "units", 1, MEDIA_UNITS[media])
                _need(escrow >= units * max_price,
                      f"o valor reservado precisa cobrir {units} {what} a {max_price} µSIG cada "
                      f"({_pt(units * max_price)} µSIG)")
                job.update({"model": d["model"], "spec_hash": d["spec_hash"], "replicas": 1, "media": media,
                            "units": units})
            client_pub = d.get("client_pub")
            if client_pub is not None:   # a resposta vai cifrada para esta chave
                client_pub = _pub(client_pub, "client_pub")
                _need(address_of(client_pub) == sender, "client_pub não corresponde à carteira que paga o pedido")
                job["client_pub"] = client_pub
            if d.get("machine") is not None:
                job["machine"] = self._target(d, sender, max_price)
                _need(client_pub is not None, "um pedido direcionado precisa de client_pub (a chave que recebe a "
                                              "resposta cifrada)")
            if d.get("racers") is not None:   # v7: corrida
                _need(job.get("machine") is not None, "racers só num pedido direcionado (com machine)")
                racers = d["racers"]
                _need(isinstance(racers, dict) and 1 <= len(racers) <= MAX_RACERS,
                      f"racers: de 1 a {MAX_RACERS} máquinas a mais")
                out = {}
                for addr, h in sorted(racers.items()):
                    a = self._target({"machine": addr, "model": d["model"]}, sender, max_price)
                    _need(a != job["machine"] and a not in out, "máquina repetida na corrida")
                    _need(_hex64(h), "spec_hash de racers inválido")
                    out[a] = h
                job["racers"] = out
        else:
            task = d.get("task")
            _need(isinstance(task, dict), "tarefa malformada")
            _only(task, {"name", "params"}, "na tarefa")
            try:
                units = tasks.validate(task)
            except ValueError as e:
                raise TxError(str(e))
            except TypeError:
                raise TxError("tarefa malformada")
            replicas = d.get("replicas", 2)
            _need(_is_int(replicas) and 1 <= replicas <= 7, "replicas deve estar entre 1 e 7")
            _need(escrow >= units * max_price * replicas,
                  f"o valor reservado precisa cobrir {units} × {max_price} µSIG × {replicas} cópias")
            job.update({"task": {"name": task["name"], "params": dict(task["params"])}, "units": units,
                        "replicas": replicas})
        _need(txid not in self.jobs, "job já existe")
        _need(self.bal(sender) >= escrow, "saldo insuficiente para o escrow")
        self._debit(sender, escrow)
        self.jobs[txid] = job

    def _target(self, d, sender, max_price):
        """Pedido direcionado: só a máquina escolhida responde, então ela precisa estar pronta agora."""
        addr = _addr(d.get("machine"), "machine")
        mc = self.machines.get(addr)
        w = self.mining["window"]
        _need(mc is not None, "a máquina escolhida não está registrada na rede")
        _need(mc["online"] and mc["last_seen"] >= w - 1,
              "a máquina escolhida não está online agora (não sinalizou nesta janela nem na anterior)")
        _need(mc["ai"], "a máquina escolhida está com a IA desligada")
        _need(mc["pub"] is not None, "a máquina escolhida não recebe pedidos cifrados (não publicou a chave pública)")
        _need(d["model"] in mc["models"], "a máquina escolhida não hospeda este modelo")
        _need(addr != sender, "você não pode mandar o pedido para a sua própria máquina")
        _need(self._ai_price(mc, d["model"]) <= max_price,
              "o preço da máquina escolhida está acima do máximo do pedido")
        # fila limitada: assim a máquina sempre consegue reivindicar e responder dentro do prazo, e um cliente não
        # a derruba por faltas inundando-a de pedidos (achado targeted-flood-evicts-honest-host). O job novo ainda
        # não entrou em self.jobs, então conta só os que já estão vivos.
        live = [j for j in self.jobs.values() if j["status"] in WORKING and addr in _runners(j)]
        _need(len(live) < MAX_TARGET_OPEN, "a máquina escolhida está com a fila cheia agora; escolha outra")
        _need(sum(1 for j in live if j["client"] == sender) < MAX_TARGET_PER_CLIENT,
              "você já tem pedidos demais nessa máquina; espere ela responder")
        return addr

    def _ai_price(self, mc, name):
        """Preço da máquina para o modelo: por mil tokens (texto), por imagem ou por segundo de vídeo."""
        price = mc["prices"].get(TASK_PRICE[model_task(self.models[name])])
        _need(price is not None, "esta máquina não tem preço para este tipo de IA")
        return price

    def _tx_claim_job(self, sender, d, txid):
        job = _get(self.jobs, d.get("job_id"))
        mc = self.machines.get(sender)
        _need(job is not None, "job não existe")
        _need(mc is not None, "máquina não registrada")
        _need(job.get("machine") is None or sender in _runners(job), "este job é para outra máquina")
        _need(mc["online"], "máquina offline: ela precisa sinalizar (ping) antes")
        _need(job["status"] == "open" or (job.get("racers") and job["status"] == "running"),
              f"job não está aberto ({job['status']})")
        _need(sender not in job["claims"], "você já reivindicou este job")
        _need(sender != job["client"], "o cliente não pode executar o próprio job")
        if job["kind"] == "ai":
            _need(job["model"] in mc["models"], "esta máquina não hospeda este modelo")
            price = self._ai_price(mc, job["model"])
        else:
            _need(mc["compute"], "esta máquina não oferece computação")
            price = mc["prices"]["compute_per_unit"]
        _need(price <= job["max_price"], "seu preço está acima do máximo do cliente")
        job["claims"][sender] = {"time": self.now, "price": price}
        if len(job["claims"]) >= job["replicas"]:
            job["status"] = "running"

    def _tx_submit_result(self, sender, d, txid):
        job = _get(self.jobs, d.get("job_id"))
        _need(job is not None, "job não existe")
        _need(job["status"] in WORKING, "job já encerrado")
        _need(sender in job["claims"], "você não reivindicou este job")
        _need(sender not in job["results"], "resultado já enviado")
        _need(_hex64(d.get("result_hash")), "result_hash inválido")
        failed = _flag(d, "failed")
        usage = d.get("usage", {})             # mesmo formato em qualquer job (na computação não conta)
        _need(isinstance(usage, dict), "usage inválido")
        if job.get("media"):                   # v2: imagens ou segundos de vídeo entregues
            _only(usage, ("units",), "em usage")
            usage = {"units": _int_in(usage.get("units", 0), "units", 0, job["units"])}
        else:
            _only(usage, USAGE_KEYS, "em usage")
            usage = {k: _int_in(usage.get(k, 0), k, 0, MAX_TOKENS) for k in USAGE_KEYS}
        res = {"hash": d["result_hash"], "time": self.now}
        if job["kind"] == "ai":
            _need(not failed or not any(usage.values()), "uma falha não cobra tokens: usage deve ser zero")
            res["usage"] = usage
        else:
            _need(not failed, "só pedidos de IA podem terminar em falha")
        if failed:
            res["failed"] = True
        job["results"][sender] = res
        if job.get("machine"):   # respondeu (entrega ou falha) um direcionado: está viva nesta janela, só ocupada
            self.machines[sender]["answered_window"] = self.mining["window"]
        if job["kind"] == "ai":
            if failed and job.get("racers") and self._race_alive(job):
                self._race_drop(job, sender)          # v7: só ela sai da corrida; as outras seguem
            elif failed:
                self._fail_ai(job, sender)
            elif self.rule(7):
                self._start_review(job, sender)
            else:
                self._settle_ai(job, sender)
        elif len(job["results"]) >= job["replicas"]:
            self._settle_compute(job)

    def _tx_cancel_job(self, sender, d, txid):
        """O cliente desistiu antes de qualquer máquina pegar o pedido: o escrow volta inteiro."""
        job = _get(self.jobs, d.get("job_id"))
        _need(job is not None, "job não existe")
        _need(job["client"] == sender, "só quem pediu pode cancelar")
        _need(job["status"] in LIVE, f"este pedido já foi encerrado ({job['status']})")
        _need(not job["claims"], "um computador já está respondendo este pedido")
        self._refund(job, "canceled")

    def _tx_release_job(self, sender, d, txid):
        """v6: a máquina escolhida pegou o pedido e sumiu da rede (não sinalizou nesta janela nem na anterior). Quem
        pediu recebe tudo de volta na hora, em vez de esperar o prazo inteiro (2 h num vídeo), e a máquina leva a
        falta, como no fim do prazo. Uma máquina que segue sinalizando está viva, só demorando: não dá para liberar
        (assim ninguém recebe a resposta pela metade e foge da conta)."""
        job = _get(self.jobs, d.get("job_id"))
        _need(job is not None, "job não existe")
        _need(job["client"] == sender, "só quem pediu pode liberar")
        _need(job["status"] in LIVE, f"este pedido já foi encerrado ({job['status']})")
        _need(job["status"] in WORKING, "a máquina já entregou: o pedido está com a vigia")
        mach = job.get("machine")
        _need(mach is not None, "só pedidos direcionados a uma máquina podem ser liberados")
        w = self.mining["window"]
        working = [r for r in _runners(job) if r in job["claims"] and r not in job["results"]
                   and r not in (job.get("out") or [])]
        _need(working, "a máquina ainda não pegou o pedido: cancele em vez de liberar")
        for r in working:
            mc = self.machines.get(r)
            _need(mc is None or mc["last_seen"] < w - 1,
                  "o computador ainda está sinalizando na rede: espere ele entregar ou o prazo acabar")
        job["missed"] = True
        self._refund(job, "expired")
        for r in working:
            self._miss(job, r)

    def _pay(self, job, mach, amount, cut=0):
        """Paga 100% do preço ao payout da máquina (menos o royalty do padrinho): sem taxa, sem queima.
        Reputação só sobe com pagamento de verdade, de outra pessoa, uma vez por janela: não dá para fabricar."""
        mc = self.machines[mach]
        self._give(mc["payout"], amount - cut, "job", job["id"])
        job["payments"][mach] = amount
        mc["earned"] += amount - cut
        mc["jobs_done"] += 1
        w = self.mining["window"]
        if amount > 0 and job["client"] not in (mach, mc["payout"]) and mc["rep_window"] != w:
            mc["reputation"] += 1
            mc["rep_window"] = w

    def _refund(self, job, status):
        """Devolve o escrow inteiro ao cliente (expirado, cancelado, falho, disputado) e encerra o job."""
        self._give(job["client"], job["escrow"], "refund", job["id"])
        self._close(job, status)

    def _race_alive(self, job):
        """Corrida (v7): ainda há quem possa entregar? Quem pegou e está no prazo, ou quem nem pegou ainda e
        está dentro de TARGET_TIMEOUT (sem resultado e sem ter saído)."""
        out = job.get("out") or []
        for r in _runners(job):
            if r in job["results"] or r in out:
                continue
            claim = job["claims"].get(r)
            if claim is not None and self.now - claim["time"] <= claim_timeout(job):
                return True
            if claim is None and self.now - job["created"] <= TARGET_TIMEOUT:
                return True
        return False

    def _race_drop(self, job, mach):
        """Uma corredora falhou: conta a falha (e, em imagem e vídeo, 1 de reputação, v5) e sai da corrida."""
        self.machines[mach]["jobs_failed"] += 1
        if self.rule(5) and job.get("media"):
            self.machines[mach]["reputation"] -= 1
        job["out"] = sorted(set(job.get("out") or []) | {mach})

    def _race_deadline(self, job):
        """Prazos de uma corrida (v7): quem pegou e passou do prazo leva falta e sai; a escolhida que nem pegou em
        TARGET_TIMEOUT também (as extras, não: elas só correm se estiverem livres). Sem ninguém que ainda possa
        entregar, o pedido expira e o valor volta."""
        out = set(job.get("out") or [])
        for r in _runners(job):
            if r in job["results"] or r in out:
                continue
            claim = job["claims"].get(r)
            if claim is not None and self.now - claim["time"] > claim_timeout(job):
                out.add(r)
                self._miss(job, r)
            elif claim is None and self.now - job["created"] > TARGET_TIMEOUT:
                out.add(r)
                if r == job["machine"]:
                    self._miss(job, r)
        job["out"] = sorted(out)
        if not self._race_alive(job):
            job["missed"] = True
            self._refund(job, "expired")

    # ------------------------------------------------------------ vigia (v7)
    def _pick_watcher(self, job, exclude=()):
        """Sorteio da vigia no bloco da entrega: uma máquina online que hospeda o modelo, fora de quem correu, do
        cliente e de quem já vigiou. Ninguém sabe antes da entrega quem vai ser. None se não houver."""
        m = self.models.get(job["model"])
        if not m:
            return None
        seed = self.mining["seeds"].get(str(self.mining["window"]), "")
        skip = set(_runners(job)) | set(job["claims"]) | {job["client"]} | set(exclude)
        # o mesmo dono não vigia a si mesmo: nem o payout do cliente nem o de quem correu
        owners = {job["client"]} | {self.machines[r]["payout"] for r in job["claims"] if r in self.machines}
        cands = [a for a in sorted(m["pinners"]) if a not in skip and a in self.machines
                 and self.machines[a]["online"] and self.machines[a]["pub"]
                 and self.machines[a]["payout"] not in owners]
        if not cands:
            return None
        return min(cands, key=lambda a: sha((seed + job["id"] + a + str(len(exclude))).encode()))

    def _start_review(self, job, winner):
        """A entrega chegou: o pedido fica com a vigia (o cliente já pode abrir o resultado; o valor segue
        reservado). 1 em REDO_ONE_IN entregas, sorteadas, a vigia refaz o trabalho para comparar."""
        seed = self.mining["seeds"].get(str(self.mining["window"]), "")
        watcher = self._pick_watcher(job)
        job["status"] = "review"
        job["result_hash"] = job["results"][winner]["hash"]
        job["review"] = {"machine": winner, "until": self.now + REVIEW_SECONDS, "watcher": watcher,
                         "watchers": [watcher] if watcher else [],
                         "redo": int(sha((seed + job["id"]).encode()), 16) % REDO_ONE_IN == 0}
        if job.get("racers"):
            job["winner"] = winner

    def _settle_review(self, job, verdict):
        """Fecha a revisão: "ok" (ou nenhum veredito no prazo) paga; "bad" devolve e pune a vencedora."""
        rv = job["review"]
        mach, watcher = rv["machine"], rv.get("watcher")
        if verdict == "bad":
            mc = self.machines.get(mach)
            if mc:
                mc["reputation"] -= BAD_REPUTATION
                mc["jobs_failed"] += 1
            self._rep_up(watcher)
            job.update({"cost": 0, "royalty": 0, "verdict": "bad"})
            self._refund(job, "rejected")
            return
        job["verdict"] = verdict or "auto"
        self._settle_ai(job, mach, watcher=watcher if verdict == "ok" else None)

    def _rep_up(self, addr):
        """+1 de reputação, no máximo uma vez por janela (como o pagamento de um pedido)."""
        mc = self.machines.get(addr) if addr else None
        w = self.mining["window"]
        if mc and mc["rep_window"] != w:
            mc["reputation"] += 1
            mc["rep_window"] = w

    def _tx_review_job(self, sender, d, txid):
        job = _get(self.jobs, d.get("job_id"))
        _need(job is not None, "job não existe")
        _need(job["status"] == "review", f"este pedido não está em revisão ({job['status']})")
        _need(sender == job["review"].get("watcher"), "você não é a vigia deste pedido")
        verdict = d.get("verdict")
        _need(verdict in ("ok", "bad"), "verdict deve ser 'ok' ou 'bad'")
        self._settle_review(job, verdict)

    def _tx_dispute_job(self, sender, d, txid):
        """O cliente contesta a entrega, uma vez: sorteia outra vigia e dá mais REVIEW_SECONDS. Quem decide é a
        vigia; se ninguém decidir no prazo, paga (a contestação sozinha não devolve o valor)."""
        job = _get(self.jobs, d.get("job_id"))
        _need(job is not None, "job não existe")
        _need(job["client"] == sender, "só quem pediu pode contestar")
        _need(job["status"] == "review", f"este pedido não está em revisão ({job['status']})")
        rv = job["review"]
        _need(not rv.get("disputed"), "este pedido já foi contestado")
        rv["disputed"] = True
        rv["watcher"] = self._pick_watcher(job, exclude=rv["watchers"]) or rv.get("watcher")
        if rv["watcher"] and rv["watcher"] not in rv["watchers"]:
            rv["watchers"].append(rv["watcher"])
        rv["until"] = self.now + REVIEW_SECONDS

    def _settle_ai(self, job, winner=None, watcher=None):
        mach = winner if winner is not None else next(iter(job["results"]))
        res = job["results"][mach]
        if job.get("racers"):
            job["winner"] = mach
        price = job["claims"][mach]["price"]
        if job.get("media"):   # imagem/vídeo: o preço da máquina por unidade entregue
            units = res["usage"]["units"]
            cost = min(job["escrow"], units * price)
        else:
            tokens = res["usage"]["tokens_in"] + res["usage"]["tokens_out"]
            cost = min(job["escrow"], _ceil_div(tokens * price, 1000))
        model = self.models.get(job["model"])
        royalty = cost * model["royalty_bps"] // 10000 if model else 0
        fee = cost * WATCH_BPS // 10000 if watcher and watcher in self.machines else 0
        self._pay(job, mach, cost, cut=royalty + fee)
        if fee:   # v7: a vigia que aprovou recebe 5% (sai do que a vencedora receberia) e ganha reputação
            self._give(self.machines[watcher]["payout"], fee, "watch", job["id"])
            self.machines[watcher]["earned"] += fee
            self._rep_up(watcher)
            job["watch_fee"] = fee
        if model:  # parte de cada token gasto vai para o padrinho (e, se ele quiser, volta para a hospedagem)
            reinvest = royalty * model["reinvest_bps"] // 10000
            model["budget"] += reinvest
            self._give(model["sponsor"], royalty - reinvest, "royalty", model["name"])
            model["royalties"] += royalty
            if job.get("media"):
                model["units"] = model.get("units", 0) + units
            else:
                model["tokens"] += tokens
            model["jobs"] += 1
            pin = model["pinners"].get(mach)
            if mach in _runners(job) and pin:   # pedido direcionado (ou corrida) entregue: as faltas zeram
                pin["misses"] = 0
        self._give(job["client"], job["escrow"] - cost, "change", job["id"])
        job.update({"result_hash": res["hash"], "cost": cost, "royalty": royalty})
        self._close(job, "done")

    def _fail_ai(self, job, mach):
        """A máquina não conseguiu responder (conversa longa demais, motor fora, pedido ilegível, fila cheia):
        o cliente recebe tudo de volta na hora, ninguém é pago. A partir da v5, falha em imagem ou vídeo custa 1
        de reputação (como sumir): no texto a falha costuma ser do pedido (conversa longa demais), lá não conta."""
        self.machines[mach]["jobs_failed"] += 1
        if self.rule(5) and job.get("media"):
            self.machines[mach]["reputation"] -= 1
        job.update({"result_hash": job["results"][mach]["hash"], "cost": 0, "royalty": 0})
        self._refund(job, "failed")

    def _settle_compute(self, job):
        """Voto por maioria do hash do resultado. Conta um voto por payout (o primeiro, por endereço da máquina)
        e nunca o do próprio cliente, para Sybils do mesmo dono não formarem maioria contra quem é honesto."""
        votes, voted = {}, set()
        for mach in sorted(job["results"]):
            payout = self.machines[mach]["payout"]
            if payout == job["client"] or payout in voted:
                continue
            voted.add(payout)
            votes.setdefault(job["results"][mach]["hash"], []).append(mach)
        winner = next((h for h, ms in sorted(votes.items()) if len(ms) * 2 > job["replicas"]), None)
        spent = 0
        if winner is not None:
            share = job["escrow"] // job["replicas"]
            for mach in votes[winner]:
                cost = min(share, job["units"] * job["claims"][mach]["price"])
                self._pay(job, mach, cost)
                spent += cost
            for h, ms in sorted(votes.items()):
                if h != winner:
                    for mach in ms:              # divergiu da maioria: não recebe. Só conta uma disputa (o painel
                        mc = self.machines[mach]  # mostra), que _assign_models NÃO usa: dois Sybils de payouts
                        mc["compute_disputes"] = mc.get("compute_disputes", 0) + 1   # diferentes não drenam a honesta
            job["result_hash"] = winner
            job["cost"] = spent
            self._give(job["client"], job["escrow"] - spent, "change", job["id"])
            self._close(job, "done")
        else:
            job["cost"] = 0
            self._refund(job, "disputed")

    def _close(self, job, status):
        job.update({"status": status, "closed": self.now})

    # ----------------------------------------------------------------- sites
    def _tx_deploy_site(self, sender, d, txid):
        name = d.get("name")
        _need(isinstance(name, str) and SITE_NAME.match(name), "nome: 3-32 caracteres [a-z0-9-]")
        manifest = d.get("manifest")
        _need(isinstance(manifest, dict) and 0 < len(manifest) <= MAX_SITE_FILES,
              f"manifest deve ter de 1 a {MAX_SITE_FILES} arquivos")
        clean = {}
        for path, f in manifest.items():
            _need(isinstance(path, str) and path, "caminho inválido")
            _need(len(path) <= MAX_PATH, f"caminho longo demais (máximo {MAX_PATH} caracteres)")
            _need(not path.startswith("/") and ".." not in path, f"caminho inválido: {path}")
            _need(isinstance(f, dict) and set(f) == {"hash", "size"},
                  f"cada arquivo do manifest tem exatamente hash e size ({path})")
            _need(_hex64(f["hash"]), "hash inválido")
            _int_in(f["size"], "size", 0, MAX_FILE)
            clean[path] = {"hash": f["hash"], "size": f["size"]}
        _need(sum(len(p) for p in clean) <= MAX_SITE_PATHS,
              f"a soma dos caminhos passa de {MAX_SITE_PATHS // 1024} KB")
        budget = d.get("budget", 0)
        site = self.sites.get(name)
        _need(site is None or site["owner"] == sender, "nome já pertence a outra conta")
        # orçamento mínimo proporcional ao manifest: cada arquivo pesa no estado, então custa (achado
        # site-manifest-state-bloat-cheap). Nome novo não pode se passar pela página oficial (site-phishing).
        min_new = MIN_SITE_BUDGET + SITE_BUDGET_PER_FILE * len(clean)
        if site is None:
            _need(name not in RESERVED_SITE_NAMES, "esse nome é reservado pelo Sigma oficial; escolha outro")
            _need(not _is_int(budget) or budget >= min_new,
                  f"um site novo precisa de um orçamento de pelo menos {_pt(min_new)} µSIG "
                  f"({_pt(MIN_SITE_BUDGET)} + {_pt(SITE_BUDGET_PER_FILE)} por arquivo)")
        _int_in(budget, "budget", min_new if site is None else 0, self.params["supply_cap"])
        replicas = d.get("replicas", 2)
        _need(_is_int(replicas) and 1 <= replicas <= 5, "replicas deve estar entre 1 e 5")
        _need(self.bal(sender) >= budget, "saldo insuficiente para o budget")
        self._debit(sender, budget)
        size = sum(f["size"] for f in clean.values())
        if site is None:
            site = self.sites[name] = {"name": name, "owner": sender, "budget": 0, "version": 0,
                                       "created": self.now, "pinners": {}, "paid": 0,
                                       "pinned_window": self.mining["window"]}
        if site.get("manifest") != clean:
            site["version"] += 1
            site["pinners"] = {}
        if site.pop("expired_window", None) is not None:   # voltou do expirado: a limpeza conta a partir de agora
            site["pinned_window"] = self.mining["window"]
        site.update({"manifest": clean, "size": size, "replicas": replicas,
                     "budget": site["budget"] + budget, "status": "active", "updated": self.now})

    def _tx_pin_site(self, sender, d, txid):
        site = _get(self.sites, d.get("name"))
        mc = self.machines.get(sender)
        _need(site is not None and site["status"] == "active", "site não existe ou expirou")
        _need(mc is not None, "máquina não registrada")
        _need(mc["online"], "máquina offline: ela precisa sinalizar (ping) antes")
        _need(d.get("version") == site["version"], "versão desatualizada do site")
        _need(sender not in site["pinners"], "você já hospeda este site")
        _need(len(site["pinners"]) < site["replicas"], "site já tem réplicas suficientes")
        used = self._usage()[1].get(sender, 0)
        _need(used + site["size"] <= mc["hw"]["disk_mb"] * 1_000_000, "sem espaço em disco nesta máquina")
        site["pinners"][sender] = {"window": self.mining["window"]}

    # ------------------------------------------- modelos de IA patrocinados
    def _model_of(self, sender, d, sponsor_only=True):
        m = _get(self.models, d.get("name"))
        _need(m is not None, "modelo não existe (ou já foi apagado da rede)")
        _need(not sponsor_only or m["sponsor"] == sender, "só o padrinho controla este modelo")
        return m

    @staticmethod
    def _bps(d, key, limit, default):
        v = d.get(key, default)
        _need(_is_int(v) and 0 <= v <= limit, f"{key} deve estar entre 0 e {limit}")
        return v

    def _tx_register_model(self, sender, d, txid):
        """O padrinho aponta um GGUF (url + sha256 + tamanho) e paga a hospedagem; a rede escolhe as máquinas."""
        name = d.get("name")
        _need(isinstance(name, str) and MODEL_NAME.match(name), "nome: 3-48 caracteres [a-z0-9.-]")
        _need(name not in self.models, "já existe um modelo com esse nome")
        url = d.get("url")
        _need(isinstance(url, str) and len(url) <= 500 and URL_RE.match(url), "url deve começar com https:// ou http://")
        digest = d.get("sha256")
        _need(isinstance(digest, str) and _hex64(digest.lower()), "sha256 deve ter 64 caracteres hexadecimais")
        size = d.get("size")
        task = d.get("task", "text")              # v2: só aparece a partir da v2 (_fields recusa antes)
        split = task == "text" and self.rule(4)   # v4: texto em partes, cada uma até 100 GB
        if split:
            _need(_is_int(size) and 0 < size <= MAX_PART_SIZE, "size deve estar entre 1 byte e 100 GB")
        else:
            _need(_is_int(size) and 0 < size <= MAX_MODEL_SIZE, "size deve estar entre 1 byte e 60 GB")
        _need(task in TASKS, "task deve ser 'text', 'image' ou 'video'")
        files = d.get("files", [])
        most = MAX_PARTS if split else MAX_MODEL_FILES
        _need(isinstance(files, list) and len(files) <= most, f"files deve ser uma lista de até {most} arquivos")
        _need(task != "text" or not files or split, "modelo de texto é um arquivo só (GGUF): sem files")
        clean_files, roles = [], set()
        for f in files:   # VAE, codificadores de texto: baixados e conferidos como o arquivo principal
            _need(isinstance(f, dict), "cada arquivo de files deve ser um objeto")
            _only(f, ("role", "url", "sha256", "size"), "em files")
            role, furl, fsha, fsize = f.get("role"), f.get("url"), f.get("sha256"), f.get("size")
            if split:   # v4: as partes 2..n do GGUF, na ordem
                _need(role == PART_ROLE, "modelo de texto só leva files com role 'part' (as partes do GGUF)")
            else:
                _need(role in MODEL_ROLES and role not in roles,
                      f"role deve ser um de {', '.join(MODEL_ROLES)}, sem repetir")
            _need(isinstance(furl, str) and len(furl) <= 500 and URL_RE.match(furl), "url de files inválida")
            _need(isinstance(fsha, str) and _hex64(fsha.lower()), "sha256 de files inválido")
            _need(_is_int(fsize) and 0 < fsize <= (MAX_PART_SIZE if split else MAX_MODEL_SIZE), "size de files inválido")
            roles.add(role)
            clean_files.append({"role": role, "url": furl, "sha256": fsha.lower(), "size": fsize})
        size_all = size + sum(f["size"] for f in clean_files)
        if split:
            _need(size_all <= MAX_SPLIT_SIZE, "o modelo inteiro (com as partes) passa de 600 GB")
        else:
            _need(size_all <= MAX_MODEL_SIZE, "o modelo inteiro (com files) passa de 60 GB")
        royalty = self._bps(d, "royalty_bps", MAX_ROYALTY_BPS, 1000)
        reinvest = self._bps(d, "reinvest_bps", 10000, 0)
        replicas = d.get("replicas", 2)
        _need(_is_int(replicas) and 1 <= replicas <= 5, "replicas deve estar entre 1 e 5")
        license_, description = _text(d, "license", 60), _text(d, "description", 200)
        budget = _int_in(d.get("budget"), "budget", 1, self.params["supply_cap"])
        # modelo grande precisa cobrir um dia de aluguel de todas as cópias já no registro, senão vira lixo de
        # estado que ninguém consegue hospedar (achado window-turnover-models-x-machines). É a mesma conta da
        # funded(): até FUNDED_ABOVE o download é barato e não exige esse adiantamento.
        # pesos + contexto (texto) ou + memória de trabalho da geração (imagem/vídeo), em MB
        ram_mb = math.ceil(size_all / 1e6 * 1.2) + (512 if task == "text" else MEDIA_RAM_MB[task])
        if size_all > FUNDED_ABOVE:
            probe = {"replicas": replicas, "size": size_all}
            if task != "text":
                probe["ram_mb"] = ram_mb
            minimo = self.rent_window(probe) * _ceil_div(MIN_FUNDED_SECONDS, self.params["window_seconds"])
            _need(budget >= minimo, f"o orçamento precisa cobrir um dia de hospedagem: {_pt(minimo)} µSIG")
        _need(self.bal(sender) >= budget, "saldo insuficiente para o budget")
        self._debit(sender, budget)
        self.retired_models.pop(name, None)
        self.models[name] = {"name": name, "sponsor": sender, "url": url, "sha256": digest.lower(), "size": size_all,
                             "ram_mb": ram_mb,
                             "license": license_, "description": description,
                             "royalty_bps": royalty, "reinvest_bps": reinvest, "replicas": replicas,
                             "budget": budget, "rent_paid": 0, "royalties": 0, "tokens": 0, "jobs": 0,
                             "assigned": {}, "pinners": {}, "cooldown": {}, "former": {}, "failed": 0,
                             "pinned_window": self.mining["window"], "created": self.now}
        if task != "text":   # v2: classe, arquivos extras e o tamanho só do arquivo principal
            self.models[name].update({"task": task, "files": clean_files, "main_size": size, "units": 0})
        elif clean_files:    # v4: as partes do GGUF e o tamanho só da primeira
            self.models[name].update({"files": clean_files, "main_size": size})

    def _tx_fund_model(self, sender, d, txid):
        m = self._model_of(sender, d)
        amount = _int_in(d.get("amount"), "amount", 1, self.params["supply_cap"])
        _need(self.bal(sender) >= amount, "saldo insuficiente")
        self._debit(sender, amount)
        m["budget"] += amount

    def _tx_update_model(self, sender, d, txid):
        m = self._model_of(sender, d)
        royalty = self._bps(d, "royalty_bps", MAX_ROYALTY_BPS, m["royalty_bps"])
        reinvest = self._bps(d, "reinvest_bps", 10000, m["reinvest_bps"])
        new_sponsor = _addr(d.get("new_sponsor", m["sponsor"]), "new_sponsor")
        m.update({"royalty_bps": royalty, "reinvest_bps": reinvest, "sponsor": new_sponsor})

    def _tx_retire_model(self, sender, d, txid):
        self._delete_model(self._model_of(sender, d)["name"])

    def _tx_pin_model(self, sender, d, txid):
        """A máquina baixou, conferiu o sha256 e carregou o modelo que a rede atribuiu a ela."""
        m = self._model_of(sender, d, sponsor_only=False)
        mc = self.machines.get(sender)
        _need(mc is not None, "máquina não registrada")
        _need(sender in m["assigned"], "a rede não atribuiu este modelo a esta máquina")
        del m["assigned"][sender]
        m["pinners"][sender] = {"window": self.mining["window"], "misses": 0, "miss_window": -1}
        m["failed"] = 0                          # alguém conseguiu: o arquivo é bom, as falhas antigas não contam
        if m["name"] not in mc["models"]:
            mc["models"].append(m["name"])

    def _tx_decline_model(self, sender, d, txid):
        """A máquina recusa o arquivo que a rede lhe atribuiu (sha/GGUF errado, URL interna, modelo sem orçamento):
        libera a vaga na hora e entra em cooldown, sem punição — recusar um arquivo ruim não pode custar reputação,
        senão vira arma de Sybil (achado poison-model-rep-drain). Só o endereço com a atribuição pode mandar."""
        m = self._model_of(sender, d, sponsor_only=False)
        _need(sender in m["assigned"], "a rede não atribuiu este modelo a esta máquina")
        del m["assigned"][sender]
        m["cooldown"][sender] = self.mining["window"] + COOLDOWN_WINDOWS

    def _delete_model(self, name):
        """Apaga o modelo da rede: devolve o troco ao padrinho e cancela os jobs pendentes dele."""
        m = self.models.pop(name)
        self.retired_models[name] = self.now
        self._give(m["sponsor"], m["budget"], "budget", name)
        for jid in sorted(self.jobs):
            job = self.jobs[jid]
            if job["kind"] == "ai" and job["model"] == name and job["status"] in LIVE:
                self._refund(job, "expired")
        for mc in self.machines.values():
            if name in mc["models"]:
                mc["models"].remove(name)

    def _assign_models(self):
        """Ninguém escolhe modelo à mão: a cada janela a rede sorteia (pela seed) quem baixa o quê, começando por
        quem já teve o arquivo e pulando quem perdeu o modelo por faltas. A lista de máquinas elegíveis (online,
        com IA, confiáveis) é montada uma vez por janela: a virada não custa O(modelos × máquinas)."""
        w = self.mining["window"]
        seed = self.mining["seeds"][str(w)]
        ram, disk, count = self._usage()
        eligible = [a for a in sorted(self.machines)
                    if self.machines[a]["online"] and self.machines[a]["ai"]
                    and self.machines[a]["reputation"] > MIN_REPUTATION]
        for name in sorted(self.models):
            m = self.models[name]
            need = m["replicas"] - len(m["pinners"]) - len(m["assigned"])
            if need <= 0:
                continue
            task = model_task(m)
            fits = [a for a in eligible
                    if task in machine_tasks(self.machines[a])
                    and a not in m["pinners"] and a not in m["assigned"]
                    and m["cooldown"].get(a, w) <= w
                    and count.get(a, 0) < (MAX_MODELS_PER_MACHINE_V5 if self.rule(5) else MAX_MODELS_PER_MACHINE)
                    and self.machines[a]["hw"]["ai_ram_mb"] >= (max(ram.get(a, 0), m["ram_mb"]) if self.rule(5)
                                                                else ram.get(a, 0) + m["ram_mb"])
                    and self.machines[a]["hw"]["disk_mb"] * 1_000_000 - disk.get(a, 0) >= m["size"]]
            for a in sorted(fits, key=lambda a: (a not in m["former"], sha((seed + name + a).encode())))[:need]:
                m["assigned"][a] = w
                ram[a] = max(ram.get(a, 0), m["ram_mb"]) if self.rule(5) else ram.get(a, 0) + m["ram_mb"]
                disk[a] = disk.get(a, 0) + m["size"]
                count[a] = count.get(a, 0) + 1

    # --------------------------------------------------------------- aluguel
    def _charge_rent(self):
        """Aluguel da janela, a preço da rede: cada máquina online que guarda o site/modelo recebe do budget dele.
        Se o budget não cobre todas as réplicas, o site expira e o modelo é apagado (budget zerado
        também conta como "não cobre": não faz sentido a rede baixar algo que não pode pagar)."""
        w = self.mining["window"]
        for name in sorted(self.sites):
            site = self.sites[name]
            if site["status"] != "active":
                continue
            fees = self._rent_fees(site)
            if site["budget"] < max(1, sum(fees.values())):
                site.update({"status": "expired", "pinners": {}, "expired_window": w})
                continue
            for a, fee in fees.items():
                site["budget"] -= fee
                site["paid"] += fee
                self._pay_machine(a, fee, name)
        for name in sorted(self.models):
            m = self.models[name]
            fees = self._rent_fees(m)
            if m["budget"] < max(1, sum(fees.values())):
                self._delete_model(name)
                continue
            for a, fee in fees.items():
                m["budget"] -= fee
                m["rent_paid"] += fee
                self._pay_machine(a, fee, name)

    def _rent_fees(self, item):
        fee = self.copy_fee(item)
        return {a: fee for a in sorted(item["pinners"]) if self.machines[a]["online"]}

    # ------------------------------------------------------------- votações
    def _tx_propose(self, sender, d, txid):
        """Qualquer carteira abre uma votação de 10 dias deixando um depósito (que volta no fim). De parâmetro:
        se aprovada, muda a regra na hora. De texto: registra a decisão da rede."""
        kind = d.get("kind")
        _need(kind in ("param", "text"), "kind deve ser 'param' ou 'text'")
        title, text = d.get("title"), d.get("text", "")
        _need(isinstance(title, str) and 3 <= len(title.strip()) <= 120, "título: de 3 a 120 caracteres")
        _need(isinstance(text, str) and len(text) <= 2000, "texto: no máximo 2.000 caracteres")
        prop = {"id": txid, "proposer": sender, "kind": kind, "title": title.strip(), "text": text,
                "created": self.now, "ends": self.now + VOTE_SECONDS, "status": "open",
                "yes": 0, "no": 0, "votes": {}}
        if kind == "param":
            param, value = d.get("param"), d.get("value")
            votable = governable(self.rules)       # min_bond só a partir da v3
            _need(isinstance(param, str) and param in votable,
                  f"parâmetro não pode ser votado (os votáveis: {', '.join(sorted(votable))})")
            lo, hi = votable[param]
            _int_in(value, "value", lo, hi)
            _need(value != self.params[param], "esse já é o valor de agora")
            prop.update(param=param, value=value, old=self.params[param])
        else:
            _need("param" not in d and "value" not in d, "votação de texto não leva param nem value")
        _need(sum(1 for p in self.proposals.values() if p["status"] == "open") < MAX_OPEN_PROPOSALS,
              f"já há {MAX_OPEN_PROPOSALS} votações abertas: espere uma terminar")
        _need(txid not in self.proposals, "votação já existe")
        deposit = self.params["proposal_deposit"]
        _need(self.bal(sender) >= deposit, f"o depósito para abrir uma votação é de {fmt(deposit)} (volta no fim)")
        self._debit(sender, deposit)
        prop["deposit"] = deposit
        self.proposals[txid] = prop

    def _tx_vote(self, sender, d, txid):
        """1 SIG = 1 voto. O SIG fica travado até o fim da votação (assim não vota duas vezes por outra
        carteira) e volta inteiro."""
        prop = _get(self.proposals, d.get("proposal_id"))
        _need(prop is not None, "votação não existe")
        _need(prop["status"] == "open" and self.now < prop["ends"], "esta votação já terminou")
        choice = d.get("choice")
        _need(choice in CHOICES, "choice deve ser 'sim' ou 'nao'")
        amount = _int_in(d.get("amount"), "amount", 1, self.params["supply_cap"])
        _need(sender not in prop["votes"], "esta carteira já votou nesta votação")
        _need(self.bal(sender) >= amount, "saldo insuficiente para esses votos")
        self._debit(sender, amount)
        prop["votes"][sender] = {"choice": choice, "amount": amount}
        prop["yes" if choice == "sim" else "no"] += amount

    def _close_proposals(self):
        """Votações que chegaram ao fim: aprovada com mais sim que não e quórum; o SIG travado volta a todos.
        A partir da v2, uma votação também termina antes dos 10 dias quando um lado passa de metade de todos os votos
        possíveis (todo o SIG que existe): o outro lado não teria como virar."""
        for pid in sorted(self.proposals):
            prop = self.proposals[pid]
            if prop["status"] != "open":
                continue
            early = self.rule(2) and max(prop["yes"], prop["no"]) * 2 > self.mining["minted"]
            if self.now < prop["ends"] and not early:
                continue
            if early and self.now < prop["ends"]:
                prop["early"] = True
            quorum = self.mining["minted"] * self.params["quorum_bps"] // 10_000
            passed = prop["yes"] > prop["no"] and prop["yes"] + prop["no"] >= quorum
            prop.update(status="approved" if passed else "rejected", closed=self.now, quorum=quorum,
                        voters=len(prop["votes"]))
            if passed and prop["kind"] == "param":
                self.params[prop["param"]] = prop["value"]
                prop["applied"] = True
            for addr, v in sorted(prop["votes"].items()):
                self._give(addr, v["amount"], "vote", pid)
            self._give(prop["proposer"], prop["deposit"], "deposit", pid)
            prop["votes"] = {}                   # encerrada: ficam só os totais
        closed = sorted((p for p in self.proposals.values() if p["status"] != "open"),
                        key=lambda p: (p["closed"], p["id"]))
        for p in closed[:-CLOSED_PROPOSALS_KEEP]:
            del self.proposals[p["id"]]

    # ------------------------------------------- caução dos coordenadores (regras v3)
    # ext["bonds"]: máquina -> {owner, amount (ativo), unbonding: [{amount, release}], slashed, since}, criado na
    # primeira caução. ext["slashes"]: os últimos SLASHES_KEEP cortes, públicos. Quem escolhe os coordenadores de um
    # pedido, a conferência entre eles e as provas de fraude (que chamam _slash) são a etapa seguinte.
    def is_coordinator(self, machine):
        """A máquina pode coordenar a divisão de um modelo gigante: regras v3, caução ativa de pelo menos
        min_bond, online e com a IA ligada. A caução tem de ser da carteira que recebe os ganhos dela: se o payout
        mudar, quem passa a receber não responde com o SIG de outro."""
        if not self.rule(3) or not isinstance(machine, str):
            return False
        b, mc = (self.ext.get("bonds") or {}).get(machine), self.machines.get(machine)
        return bool(b and mc and b["owner"] == mc["payout"] and b["amount"] >= self.params["min_bond"]
                    and mc["online"] and mc["ai"])

    def _tx_bond(self, sender, d, txid):
        """Trava SIG como caução de uma máquina. Assinada pela carteira que recebe os ganhos dela (o payout):
        quem responde pela máquina é quem trava, e uma caução de outra carteira não muda de dono."""
        machine = _addr(d.get("machine"), "machine")
        amount = _int_in(d.get("amount"), "amount", 1, self.params["supply_cap"])
        mc = self.machines.get(machine)
        _need(mc is not None, "máquina não registrada")
        _need(mc["payout"] == sender, "só a carteira que recebe os ganhos da máquina (o payout dela) pode caucioná-la")
        b = (self.ext.get("bonds") or {}).get(machine)
        _need(b is None or b["owner"] == sender,
              "a caução desta máquina é de outra carteira: ela precisa retirar tudo antes")
        _need(self.bal(sender) >= amount, "saldo insuficiente para a caução")
        self._debit(sender, amount)
        if b is None:
            b = self.ext.setdefault("bonds", {})[machine] = {"owner": sender, "amount": 0, "unbonding": [],
                                                             "slashed": 0, "since": self.now}
        b["amount"] += amount

    def _tx_unbond(self, sender, d, txid):
        """Começa a retirar parte da caução: sai do ativo na hora (pode deixar a máquina abaixo do mínimo para
        coordenar) e volta ao dono UNBOND_SECONDS depois, sozinha, no fim do bloco. Na espera ainda pode ser
        cortada: quem trapaceou não saca antes de a disputa terminar."""
        machine = _addr(d.get("machine"), "machine")
        b = (self.ext.get("bonds") or {}).get(machine)
        _need(b is not None, "esta máquina não tem caução")
        _need(b["owner"] == sender, "só a carteira dona da caução pode retirá-la")
        amount = _int_in(d.get("amount"), "amount", 1, self.params["supply_cap"])
        _need(amount <= b["amount"], f"a caução ativa desta máquina é de {fmt(b['amount'])}: não dá para retirar mais")
        _need(len(b["unbonding"]) < MAX_UNBONDING,
              f"já há {MAX_UNBONDING} retiradas em espera nesta caução: espere a primeira ser liberada")
        b["amount"] -= amount
        b["unbonding"].append({"amount": amount, "release": self.now + UNBOND_SECONDS})

    def _release_bonds(self):
        """Fim do bloco: as retiradas que cumpriram a espera voltam ao dono (crédito "bond", ref = a máquina)."""
        bonds = self.ext.get("bonds") or {}
        for machine in sorted(bonds):
            b = bonds[machine]
            due = [u for u in b["unbonding"] if u["release"] <= self.now]
            if due:
                b["unbonding"] = [u for u in b["unbonding"] if u["release"] > self.now]
                self._give(b["owner"], sum(u["amount"] for u in due), "bond", machine)
            self._drop_empty_bond(machine)

    def _drop_empty_bond(self, machine):
        """Caução sem nada ativo nem em espera sai do estado."""
        b = self.ext["bonds"][machine]
        if not b["amount"] and not b["unbonding"]:
            del self.ext["bonds"][machine]

    def _slash(self, machine, amount, reason, reporter=None, victim=None, victim_cap=0):
        """Corta até `amount` µSIG da caução da máquina e devolve quanto cortou de fato: primeiro da caução ativa,
        depois das retiradas em espera (as mais novas primeiro). Do cortado, SLASH_REPORTER_BPS vai para quem
        provou a fraude, até SLASH_VICTIM_BPS (e no máximo `victim_cap`, o prejuízo) para quem foi lesado, e o
        resto volta para o que falta minerar: sai de `minted` e a emissão o paga de novo aos mineradores (o Sigma
        não queima SIG). Quem responde pela máquina (a própria máquina, o dono da caução e o payout) não recebe
        parte do próprio corte. A máquina perde SLASH_REPUTATION de reputação e o corte entra em ext["slashes"]."""
        if not _is_int(amount) or amount < 1:
            return 0
        b, mc = (self.ext.get("bonds") or {}).get(machine), self.machines.get(machine)
        cut = 0
        if b is not None:
            cut = min(amount, b["amount"])
            b["amount"] -= cut
            for u in reversed(b["unbonding"]):       # a lista está na ordem em que as retiradas começaram
                take = min(amount - cut, u["amount"])
                u["amount"] -= take
                cut += take
            b["unbonding"] = [u for u in b["unbonding"] if u["amount"] > 0]
            b["slashed"] += cut
        own = {machine, (b or {}).get("owner"), (mc or {}).get("payout")}   # quem responde pela máquina
        to_reporter = cut * SLASH_REPORTER_BPS // 10_000 if reporter is not None and reporter not in own else 0
        to_victim = (min(victim_cap, cut * SLASH_VICTIM_BPS // 10_000)
                     if victim is not None and victim not in own and _is_int(victim_cap) and victim_cap > 0 else 0)
        to_mining = cut - to_reporter - to_victim
        self._give(reporter, to_reporter, "slash_reporter", machine)
        self._give(victim, to_victim, "slash_victim", machine)
        self.mining["minted"] -= to_mining
        if mc is not None:
            mc["reputation"] -= SLASH_REPUTATION
        self.ext.setdefault("slashes", []).append(
            {"machine": machine, "amount": cut, "reason": str(reason)[:200], "height": self.height,
             "reporter": reporter, "to_reporter": to_reporter, "victim": victim, "to_victim": to_victim,
             "to_mining": to_mining})
        del self.ext["slashes"][:-SLASHES_KEEP]
        if b is not None:
            self._drop_empty_bond(machine)
        return cut


def make_tx(wallet, type_, data, nonce):
    tx = {"type": type_, "data": data, "nonce": nonce, "pub": wallet.pub}
    tx["sig"] = wallet.sign(canon(tx_body(tx)))
    tx["id"] = tx_id(tx)
    return tx


def make_block(wallet, height, prev, txs, state_root, timestamp, rules=1):
    """`rules`: a versão das regras em que o bloco rodou. Um nó que não conhece essa versão para e avisa que
    precisa ser atualizado, em vez de recusar o bloco como inválido."""
    block = {"height": height, "prev": prev, "time": timestamp, "txs": txs,
             "state_root": state_root, "producer": wallet.pub, "rules": rules}
    block["hash"] = block_hash(block)
    block["sig"] = wallet.sign(block["hash"].encode())
    return block


def genesis_block(genesis):
    return {"height": 0, "prev": "0" * 64, "time": genesis["time"], "txs": [],
            "state_root": State(genesis).root(), "producer": "", "hash": sha(canon(genesis)), "sig": ""}
