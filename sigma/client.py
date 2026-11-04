"""Cliente da rede Sigma: usado pelo app, pelo minerador, pela máquina hospedeira, pela CLI e pelo Sigma Code.

Entradas de dinheiro em SIG decimal (`budget_sig`, `amount_sig`); tudo que volta do nó vem em µSIG inteiros.
Pedidos de IA vão sempre cifrados para uma máquina sorteada, e a resposta volta cifrada para esta carteira
(`ask`). Continua público que houve um pedido, de qual carteira, para qual modelo e máquina, quanto custou e
quando; e a máquina que responde lê a pergunta, porque precisa dela para responder.
"""
import hashlib
import http.client
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import chain, netsafe, tasks
from .chain import UNIT, canon, make_tx
from .crypto import checksum_ok, norm_address, seal

DEFAULT_NODE = "https://sigma.195-35-29-232.sslip.io"
JOB_END = ("done", "failed", "disputed", "expired", "canceled")
CONTEXT = 4096       # contexto dos modelos da rede (o mesmo engines.CONTEXT das máquinas; um teste confere)
ATTEMPTS = 3         # cada pergunta tenta no máximo 3 máquinas diferentes
_TX_LOCKS = {}   # endereço -> lock: o nonce é por chave, mesmo com vários Client usando a mesma carteira
_RNG = random.SystemRandom()   # sorteio imprevisível: ninguém se posiciona para receber todas as perguntas

TOO_LONG = "sua mensagem é longa demais para os modelos da rede: divida o texto em partes menores"
NO_MACHINE = "nenhuma máquina online com o modelo aceita pedidos cifrados agora"
NO_ANSWER = ("Nenhum computador respondeu a tempo. Você não pagou nada por esta pergunta: o valor reservado "
             "voltou para a sua carteira. Tente de novo.")
LATE_CLAIMED = ("Nenhum computador respondeu a tempo. Uma máquina pegou o pedido e ainda não entregou: se ela "
                "não entregar, o valor reservado volta sozinho para a sua carteira em alguns minutos.")
CANCELED = "cancelado"
CONTEXT_FAIL = "a conversa passou do limite do modelo (4.096 tokens): comece uma conversa nova"
UNREADABLE = "a máquina devolveu um resultado ilegível"
BAD_ADDRESS = "Esse endereço tem um caractere errado. Copie e cole de novo direto da MetaMask, sem digitar."
_PUB_RE = re.compile(r"[0-9a-fA-F]{128}")


class SigmaError(Exception):
    """Erro explicado em português. `status` é o código HTTP quando o nó respondeu com erro (400, 429, 503...),
    ou None (sem resposta): o minerador decide por ele se vale tentar de novo."""
    status = None


class TxDropped(SigmaError):
    """O nó descartou a transação: ela não entrou em nenhum bloco e nada mudou."""


PT = str.maketrans(",.", ".,")   # 1,234.5 -> 1.234,5: números como se escreve no Brasil


def fmt(amount):
    """µSIG -> texto em SIG no jeito brasileiro (1.234,500000 SIG), para a CLI e o Sigma Code."""
    return f"{amount / UNIT:,.6f}".translate(PT) + " SIG"


def _decimal(v, what):
    try:
        d = Decimal(str(v).strip().replace(",", "."))  # aceita "1,5" como se escreve no Brasil
    except InvalidOperation:
        raise SigmaError(f"{what} inválido: {v}") from None
    if not d.is_finite() or d < 0:
        raise SigmaError(f"{what} inválido: {v}")
    return d


def to_usig(amount_sig):
    """SIG decimal (número ou texto) -> µSIG inteiros, sem erro de ponto flutuante (0,57 SIG = 570000)."""
    return int((_decimal(amount_sig, "valor") * UNIT).to_integral_value(ROUND_HALF_UP))


def _bps(pct):
    """Porcentagem (ex.: 12,5) -> pontos-base (1250)."""
    return int((_decimal(pct, "porcentagem") * 100).to_integral_value(ROUND_HALF_UP))


def _q(part):
    return urllib.parse.quote(str(part), safe="")


def _why(e):
    return "tempo esgotado" if isinstance(e, TimeoutError) else str(e)


def _typed_address(addr):
    """Endereço que alguém digitou -> 'sig…'. Com maiúsculas e minúsculas misturadas, confere o checksum da
    MetaMask (EIP-55): um caractere trocado mandaria o dinheiro para uma carteira que ninguém controla."""
    raw = addr.strip() if isinstance(addr, str) else addr
    try:
        norm = norm_address(raw)
    except ValueError as e:
        raise SigmaError(str(e)) from None
    if not checksum_ok(raw):
        raise SigmaError(BAD_ADDRESS)
    return norm


def _message(m):
    if not isinstance(m, dict):
        raise SigmaError("cada mensagem precisa de role e content")
    return {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}


def estimate(messages):
    """Tokens que a conversa ocupa (~3 caracteres por token): a mesma conta do cliente e da máquina."""
    return sum(len(str(m.get("content") or "")) // 3 + 8 for m in messages) + 16


def fit_messages(messages, max_tokens, context=CONTEXT):
    """Corta a conversa para caber no contexto do modelo junto com a resposta.

    Ficam as mensagens `system`, a primeira do usuário e as mais recentes que couberem em
    `context - max_tokens - 32` tokens. Devolve (mensagens, quantas ficaram de fora). Se nem a última couber,
    SigmaError: a máquina recusaria do mesmo jeito, depois de o usuário esperar."""
    msgs = [_message(m) for m in messages]
    if not msgs:
        raise SigmaError("a conversa está vazia")
    room = context - max_tokens - 32 - 16          # 16: o que `estimate` soma por conversa
    cost = [len(m["content"]) // 3 + 8 for m in msgs]
    keep = {i for i, m in enumerate(msgs) if m["role"] == "system"} | {len(msgs) - 1}
    used = sum(cost[i] for i in keep)
    if used > room:
        raise SigmaError(TOO_LONG)
    first = next((i for i, m in enumerate(msgs) if m["role"] == "user"), None)
    if first is not None and first not in keep and used + cost[first] <= room:
        keep.add(first)
        used += cost[first]
    for i in range(len(msgs) - 2, -1, -1):         # das mais novas para as mais antigas, sem buracos no meio
        if i in keep:
            continue
        if used + cost[i] > room:
            break
        keep.add(i)
        used += cost[i]
    out = [m for i, m in enumerate(msgs) if i in keep]
    return out, len(msgs) - len(out)


def order_machines(machines, window, rng=None):
    """Ordem das tentativas para [(endereço, registro)]: primeiro quem sinalizou nesta janela, depois quem
    sinalizou na anterior (pode ter acabado de dormir); dentro de cada grupo, sorteio uniforme. Assim nenhum
    operador recebe todas as perguntas de um modelo por ser o mais barato ou o mais antigo."""
    rng = rng or _RNG

    def now(rec):
        seen = rec.get("last_seen")
        return isinstance(seen, int) and seen >= window
    fresh = [x for x in machines if now(x[1])]
    rest = [x for x in machines if not now(x[1])]
    rng.shuffle(fresh)
    rng.shuffle(rest)
    return fresh + rest


_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")


def _sentence(text, default):
    """Frase escrita por outra máquina: só texto curto, numa linha só, sem caracteres de controle."""
    s = " ".join(_CONTROL.sub(" ", text).split())[:200] if isinstance(text, str) else ""
    return s or default


def _queue_full(err):
    """A chain recusou o `create_job` porque a fila da máquina está cheia (ela está viva, só ocupada): o cliente
    pula para a próxima candidata sem gastar uma das tentativas (SPEC-v2.2 §1)."""
    s = str(err)
    return "fila cheia" in s or "pedidos demais" in s


class Client:
    POLL_FAST, POLL_SLOW = 0.25, 1.0   # intervalo das consultas de espera (rápido nos primeiros 10 s)

    def __init__(self, node_url=None, wallet=None):
        self.node = (node_url or DEFAULT_NODE).rstrip("/")
        self.wallet = wallet      # Wallet (assina: do usuário ou da máquina) ou só um endereço (só consulta)
        self.last_txid = None

    @property
    def address(self):
        if self.wallet is None:
            raise SigmaError("nenhuma carteira configurada")
        if isinstance(self.wallet, str):
            try:
                return norm_address(self.wallet)
            except ValueError as e:
                raise SigmaError(str(e)) from None
        return self.wallet.address

    def _signer(self):
        if self.wallet is None:
            raise SigmaError("nenhuma carteira configurada")
        if isinstance(self.wallet, str):
            raise SigmaError("esta carteira só tem o endereço: para gastar, importe a chave privada "
                             "(ou use a MetaMask no site)")
        return self.wallet

    # ---------------------------------------------------------------- http
    def _req(self, path, payload=None, raw=None, timeout=10):
        data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
        try:
            req = urllib.request.Request(self.node + path, data=data,
                                         headers={"Content-Type": "application/json"})
        except ValueError:
            raise SigmaError(f"endereço do nó inválido: {self.node}") from None
        # HTTPS com os certificados do sistema (no macOS, o Python do python.org vem sem nenhum)
        ctx = netsafe.ssl_context() if req.type == "https" else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except Exception:  # resposta sem JSON (ex.: proxy fora do ar)
                msg = f"o nó respondeu HTTP {e.code}"
            finally:
                e.close()      # devolve a conexão já, sem esperar o coletor de lixo
            err = SigmaError(msg)
            err.status = e.code   # 400 = recusa; 429/5xx = passageiro (o minerador tenta de novo)
            raise err from None
        except urllib.error.URLError as e:
            raise SigmaError(f"não consegui falar com o nó {self.node}: {_why(e.reason)}") from None
        except (OSError, http.client.HTTPException) as e:  # timeout ou conexão caída no meio da resposta
            raise SigmaError(f"não consegui falar com o nó {self.node}: {_why(e)}") from None

    def _json(self, body):
        try:
            return json.loads(body)
        except ValueError:
            raise SigmaError(f"resposta inválida do nó {self.node}") from None

    def get(self, path, timeout=10):
        return self._json(self._req(path, timeout=timeout))

    def post(self, path, payload, timeout=10):
        return self._json(self._req(path, payload, timeout=timeout))

    def _poll(self, fetch, done, timeout, what):
        """Consulta até `done(resposta)`; uma falha de rede passageira não derruba uma espera longa."""
        start, fails = time.time(), 0
        while True:
            try:
                x = fetch()
                fails = 0
                if done(x):
                    return x
            except SigmaError:
                fails += 1
                if fails >= 10 or time.time() - start >= timeout:  # mostra a causa real, não só "tempo esgotado"
                    raise
            if time.time() - start >= timeout:
                raise SigmaError(f"tempo esgotado esperando {what}")
            time.sleep(self.POLL_SLOW if fails or time.time() - start > 10 else self.POLL_FAST)

    # --------------------------------------------------------- consultas
    def status(self):
        return self.get("/status")

    def account(self, address=None):
        return self.get(f"/account/{_q(address or self.address)}")

    def balance(self, address=None):
        return self.account(address)["balance"]

    def history(self, address=None):
        return self.get(f"/history/{_q(address or self.address)}")

    def job(self, job_id):
        return self.get(f"/job/{_q(job_id)}")

    def jobs(self, status=None):
        return self.get("/jobs" + (f"?status={_q(status)}" if status else ""))

    def sites(self):
        return self.get("/sites")

    def tx_status(self, txid):
        return self.get(f"/tx/{_q(txid)}")

    # --------------------------------------------------------- mineração
    def mining(self):
        return self.get("/mining")

    def epochs(self):
        return self.get("/mining/epochs")

    def miner(self, addr=None):
        return self.get(f"/mining/miner/{_q(addr or self.address)}")

    def submit_work(self, report):
        """Relatório de trabalho do minerador: vai sem assinatura, porque o endereço entra em cada hash."""
        return self.post("/tx", {"type": "work", "data": report})["id"]

    # ------------------------------------------------ máquinas e modelos
    def machines(self):
        return self.get("/machines")

    def machine(self, addr=None):
        return self.get(f"/machine/{_q(addr or self.address)}")

    def models(self):
        return self.get("/models")

    def model(self, name):
        return self.get(f"/models/{_q(name)}")

    # -------------------------------------------------------- transações
    def send_tx(self, type_, data, wait=True, timeout=30):
        """Assina com a chave deste cliente (carteira do usuário ou chave da máquina) e envia ao nó."""
        w = self._signer()
        with _TX_LOCKS.setdefault(w.address, threading.Lock()):  # nonce lido e usado sem ninguém no meio
            nonce = self.account(w.address)["next_nonce"]
            txid = self.post("/tx", make_tx(w, type_, data, nonce))["id"]
        self.last_txid = txid
        if wait:
            self.wait_tx(txid, timeout)
        return txid

    def wait_tx(self, txid, timeout=30):
        st = self._poll(lambda: self.tx_status(txid), lambda s: s["status"] in ("included", "dropped"),
                        timeout, "a transação entrar em um bloco")
        if st["status"] == "dropped":
            raise TxDropped(f"transação descartada: {st.get('error')}")
        return st

    def transfer(self, to, amount_sig):
        return self.send_tx("transfer", {"to": _typed_address(to), "amount": to_usig(amount_sig)})

    # ------------------------------------------------------------- blobs
    def put_blob(self, data: bytes) -> str:
        timeout = 60 + len(data) // 100_000  # conexão lenta: pelo menos 100 KB/s
        return self._json(self._req("/blob", raw=data, timeout=timeout))["hash"]

    def get_blob(self, h: str) -> bytes:
        data = self._req(f"/blob/{_q(h)}", timeout=60)
        if hashlib.sha256(data).hexdigest() != h:
            raise SigmaError("blob corrompido (hash não confere)")
        return data

    # --------------------------------------------------------------- sites
    def deploy_site_files(self, files: dict[str, bytes], name, budget_sig, replicas=2):
        """Publica (ou atualiza) um site a partir de {caminho: bytes}; devolve o manifest."""
        self._signer()
        budget = to_usig(budget_sig)  # valida antes de subir qualquer arquivo
        manifest = {}
        for path, data in files.items():
            rel = str(path).replace("\\", "/").lstrip("/")
            if not rel or ".." in rel:
                raise SigmaError(f"caminho inválido: {path}")
            blob = data.encode() if isinstance(data, str) else bytes(data)
            try:
                manifest[rel] = {"hash": self.put_blob(blob), "size": len(blob)}
            except SigmaError as e:
                raise SigmaError(f"{rel}: {e}") from None
        if not manifest:
            raise SigmaError("nenhum arquivo para publicar")
        self.send_tx("deploy_site", {"name": name, "manifest": manifest, "budget": budget, "replicas": replicas})
        return manifest

    def deploy_site(self, directory, name, budget_sig, replicas=2):
        if not os.path.isdir(directory):
            raise SigmaError(f"pasta não encontrada: {directory}")
        files = {}
        for root, _, names in os.walk(directory):
            for fn in names:
                full = os.path.join(root, fn)
                with open(full, "rb") as f:
                    files[os.path.relpath(full, directory).replace(os.sep, "/")] = f.read()
        if not files:
            raise SigmaError(f"a pasta {directory} está vazia")
        return self.deploy_site_files(files, name, budget_sig, replicas)

    def fund_site(self, name, amount_sig):
        """Recarrega o budget de um site seu: reenvia o mesmo manifest, então a versão e as réplicas ficam."""
        site = self.sites().get(name)
        if not site:
            raise SigmaError(f"o site '{name}' não existe")
        return self.send_tx("deploy_site", {"name": name, "manifest": site["manifest"],
                                            "budget": to_usig(amount_sig), "replicas": site["replicas"]})

    # ------------------------------------------------ modelos patrocinados
    def register_model(self, name, url, sha256, size, budget_sig, royalty_pct=10, reinvest_pct=0, replicas=2,
                       description="", license=""):
        """Você vira o padrinho: paga o aluguel de quem hospeda e recebe royalty de cada token gasto.
        Ninguém sobe arquivo: a rede escolhe as máquinas e elas baixam o GGUF direto da `url`."""
        try:
            size, replicas = int(size), int(replicas)
        except (TypeError, ValueError):
            raise SigmaError("tamanho e réplicas devem ser números inteiros") from None
        return self.send_tx("register_model", {
            "name": name, "url": str(url).strip(), "sha256": str(sha256).strip().lower(), "size": size,
            "budget": to_usig(budget_sig), "royalty_bps": _bps(royalty_pct), "reinvest_bps": _bps(reinvest_pct),
            "replicas": replicas, "description": description, "license": license}, timeout=60)

    def fund_model(self, name, amount_sig):
        return self.send_tx("fund_model", {"name": name, "amount": to_usig(amount_sig)})

    def update_model(self, name, royalty_pct=None, reinvest_pct=None, new_sponsor=None):
        d = {"name": name}
        if royalty_pct is not None:
            d["royalty_bps"] = _bps(royalty_pct)
        if reinvest_pct is not None:
            d["reinvest_bps"] = _bps(reinvest_pct)
        if new_sponsor:
            d["new_sponsor"] = _typed_address(new_sponsor)
        return self.send_tx("update_model", d)

    def retire_model(self, name):
        return self.send_tx("retire_model", {"name": name})

    # ------------------------------------------------------------- votações
    def governance(self):
        """Votações da rede (abertas primeiro) e as regras: o que pode ser votado, depósito, quórum."""
        return self.get("/governance")

    def proposal(self, pid):
        return self.get(f"/governance/{_q(pid)}")

    def propose(self, title, text="", param=None, value=None):
        """Abre uma votação de 10 dias. Com `param`, aprovada muda essa regra da rede para `value`; sem, é uma
        votação de texto (registra a decisão). Deixa o depósito da rede travado até o fim (volta inteiro)."""
        data = {"kind": "param" if param else "text", "title": str(title).strip(), "text": str(text or "")}
        if param:
            data.update(param=param, value=value)
        return self.send_tx("propose", data)

    def vote(self, pid, choice, amount_sig):
        """1 SIG = 1 voto. O valor fica travado até o fim da votação e volta inteiro."""
        choice = {"s": "sim", "sim": "sim", "yes": "sim", "n": "nao", "nao": "nao", "não": "nao", "no": "nao"}.get(
            str(choice).strip().lower())
        if choice is None:
            raise SigmaError("o voto é 'sim' ou 'nao'")
        return self.send_tx("vote", {"proposal_id": pid, "choice": choice, "amount": to_usig(amount_sig)})

    def decline_model(self, name):
        """A máquina recusa um modelo que a rede atribuiu a ela (sha/GGUF errado, URL interna, sem orçamento):
        a chain tira a atribuição na hora e põe o cooldown, sem punir a reputação (SPEC-v2.2 §3)."""
        return self.send_tx("decline_model", {"name": name})

    # ---------------------------------------------------------------- jobs
    def _wait_job(self, job_id, timeout):
        return self._poll(lambda: self.job(job_id), lambda j: j["status"] in JOB_END, timeout,
                          f"o job {job_id[:12]} terminar (se ninguém entregar, o valor reservado volta "
                          "quando ele expirar)")

    def cancel_job(self, job_id):
        """Cancela um pedido seu que nenhuma máquina pegou ainda: o valor reservado volta na hora."""
        return self.send_tx("cancel_job", {"job_id": job_id})

    def _window(self, mining=None):
        mining = self.mining() if mining is None else mining
        w = mining.get("window") if isinstance(mining, dict) else None
        if not isinstance(w, int) or isinstance(w, bool):
            raise SigmaError(f"resposta inválida do nó {self.node} (janela de mineração)")
        return w

    def _ai_hosts(self, model=None, window=None):
        """Máquinas que aceitam pedido cifrado agora: [(endereço, registro, preço)]. Online, com IA ligada, com
        chave pública e (com `window`) que sinalizaram nesta janela ou na anterior: quem acabou de dormir ainda
        aparece online na chain por até 2 janelas e só faria a pergunta esperar."""
        try:
            me = self.address
        except SigmaError:
            me = None
        machines = self.machines()
        if not isinstance(machines, dict):
            raise SigmaError(f"resposta inválida do nó {self.node} (lista de máquinas)")
        out = []
        for addr, m in machines.items():
            if not isinstance(m, dict) or not m.get("online") or not m.get("ai") or addr == me:
                continue
            pub, seen = m.get("pub"), m.get("last_seen")
            if not (isinstance(pub, str) and _PUB_RE.fullmatch(pub)):
                continue
            if window is not None and not (isinstance(seen, int) and seen >= window - 1):
                continue
            if model is not None and model not in (m.get("models") or []):
                continue
            price = (m.get("prices") or {}).get("ai_per_1k_tokens")
            if isinstance(price, int) and not isinstance(price, bool) and price >= 1:
                out.append((addr, m, price))
        return out

    def hosted_models(self):
        """Modelos que alguma máquina responde agora: nome -> {máquina: µSIG por 1k tokens}."""
        try:
            w = self._window()
        except SigmaError:
            w = None    # sem /mining: lista pelo que a chain diz estar online
        out = {}
        for addr, m, price in self._ai_hosts(window=w):
            for name in m.get("models") or []:
                out.setdefault(name, {})[addr] = price
        return out

    def pick_model(self, prefer=""):
        """Um modelo que esteja no ar agora (de preferência com `prefer` no nome e em mais máquinas)."""
        hosted = self.hosted_models()
        if not hosted:
            raise SigmaError("nenhuma máquina online está com um modelo de IA carregado agora")
        return min(hosted, key=lambda n: (not (prefer and prefer in n), -len(hosted[n]), n))

    def _route(self, model, max_price):
        """Máquinas a tentar, na ordem, e o preço máximo (teto) por 1k tokens."""
        mining = self.mining()
        w = self._window(mining)
        hosts = self._ai_hosts(model, w)
        if not hosts:
            raise SigmaError(NO_MACHINE)
        cheapest = min(p for _, _, p in hosts)
        if not max_price:
            # nunca o preço da mais cara: uma máquina que se anuncia caríssima não define o quanto se reserva
            default = mining.get("ai_default_price")   # `_window` já conferiu que é um dict
            if not isinstance(default, int) or isinstance(default, bool) or default < 1:
                default = getattr(chain, "DEFAULT_PRICES", {}).get("ai_per_1k_tokens", 2000)   # nó antigo
            ceiling = max(default, cheapest)
        else:
            if not isinstance(max_price, int) or isinstance(max_price, bool) or max_price < 1:
                raise SigmaError("preço máximo inválido: use µSIG inteiros por 1k tokens")
            ceiling = max_price
            if ceiling < cheapest:
                raise SigmaError(f"preço máximo abaixo do de todas as máquinas (a mais barata cobra "
                                 f"{cheapest} µSIG por 1k tokens)")
        return order_machines([(a, m) for a, m, p in hosts if p <= ceiling], w), ceiling

    def ask(self, model, messages, max_tokens=1024, max_price=None, timeout=900, json_mode=False,
            should_stop=None, on_job=None):
        """Pergunta a um modelo da rede e devolve (texto, job). Paga só os tokens usados; o resto volta.

        Sempre privado (nunca cai sozinho para um pedido público): sorteia uma máquina que tem o modelo, cifra
        o pedido para a chave dela e ela cifra a resposta para esta carteira. Se ela não responder, tenta outra
        (até 3). `should_stop()` cancela um pedido que nenhuma máquina pegou ainda; `on_job(id)` é chamado a
        cada pedido criado. `job` ganha "machine" (quem respondeu), "trimmed" (mensagens antigas que ficaram de
        fora para caber no contexto) e "attempts" (máquinas tentadas)."""
        w = self._signer()
        try:
            max_tokens = max(1, min(int(max_tokens), CONTEXT // 2))   # metade do contexto fica para a conversa
        except (TypeError, ValueError):
            raise SigmaError("max_tokens deve ser um número inteiro") from None
        msgs, trimmed = fit_messages(messages, max_tokens, CONTEXT)   # CONTEXT lido agora (a demo pode trocar)
        deadline = time.time() + timeout
        order, ceiling = self._route(model, max_price)
        escrow = max(1, math.ceil((estimate(msgs) + max_tokens) * ceiling / 1000))
        balance = self.balance(w.address)
        if balance < escrow:
            raise SigmaError(f"saldo insuficiente: esta pergunta reserva até {fmt(escrow)} e a carteira tem "
                             f"{fmt(balance)}")
        request = canon({"messages": msgs, "max_tokens": max_tokens, "json": bool(json_mode)})
        created, attempts, last_error = 0, 0, None
        for addr, rec in order:                       # percorre todas as candidatas; a fila cheia não gasta tentativa
            if attempts >= ATTEMPTS:
                break
            if should_stop and should_stop():
                raise SigmaError(CANCELED)
            if time.time() >= deadline:
                break
            try:
                spec_hash = self.put_blob(seal(rec["pub"], request))   # o nó só guarda bytes cifrados
                job_id = self.send_tx("create_job", {"kind": "ai", "model": model, "spec_hash": spec_hash,
                                                     "escrow": escrow, "max_price": ceiling, "machine": addr,
                                                     "client_pub": w.pub, "max_tokens": max_tokens}, wait=False)
            except (SigmaError, ValueError) as e:     # recusado (ex.: a máquina saiu do ar no meio do caminho) ou
                last_error = e                        # chave pública inválida: nada foi reservado, vai a próxima
                if not _queue_full(e):                # fila cheia: a máquina está viva, só ocupada; não gasta tentativa
                    attempts += 1
                continue
            if on_job:
                on_job(job_id)                        # logo após o txid: Ctrl+C antes da inclusão ainda cancela o job
            try:
                self.wait_tx(job_id)
            except TxDropped as e:
                last_error = e
                if not _queue_full(e):                # outra fila encheu entre admitir e produzir o bloco
                    attempts += 1
                continue
            except SigmaError as e:
                # o nó aceitou e não confirmou: o pedido ainda pode entrar num bloco. Criar outro por cima
                # reservaria o valor duas vezes (e "você não pagou nada" ficaria falso)
                raise SigmaError(f"a rede não confirmou o pedido a tempo ({e}). Se ele entrar e nenhuma máquina "
                                 "responder, o valor reservado volta sozinho para a sua carteira.") from None
            attempts += 1
            created += 1
            job = self._wait_private(job_id, deadline, should_stop)
            status, res = job.get("status"), None
            if status == "canceled":
                raise SigmaError(CANCELED)
            if status == "done":                      # já pago: a resposta tem de abrir com a chave desta carteira
                res = self._open_result(job, addr, deadline=deadline)   # baixa até o prazo: o valor já foi cobrado
                if not res or not isinstance(res.get("text"), str):
                    raise SigmaError(UNREADABLE)
                if res["text"] or not res.get("error"):
                    job.update(machine=addr, trimmed=trimmed, attempts=attempts)
                    return res["text"], job
                # máquina antiga: a falha vinha como resposta vazia, de custo 0
            elif status == "failed":
                try:
                    res = self._open_result(job, addr)
                except SigmaError:
                    res = None                        # sem o motivo: o valor já voltou, tenta a próxima
            if res and res.get("code") == "context":   # nenhuma outra máquina teria um contexto maior
                raise SigmaError(_sentence(res.get("error"), CONTEXT_FAIL))
            if should_stop and should_stop():         # falhou ou expirou: o valor já voltou
                raise SigmaError(CANCELED)
            # "expired" ou falha da máquina ("engine", "busy", "unreadable"): tenta a próxima
        if not created and last_error is not None:
            raise SigmaError(str(last_error))
        raise SigmaError(NO_ANSWER)

    def _wait_private(self, job_id, deadline, should_stop):
        """Espera um pedido terminar. Uma falha de rede passageira não desiste: recua e continua consultando até
        o prazo do job (ele sempre termina na chain). Cancela se `should_stop()` (ou o prazo acabou) e ninguém
        reivindicou; se o prazo passar com o job vivo, avisa que só cobra se a máquina entregar."""
        start, fails = time.time(), 0
        while True:
            late = time.time() >= deadline
            try:
                job = self.job(job_id)
                fails = 0
            except SigmaError:
                # o nó pode ter caído por alguns segundos (reinício do pm2, Wi-Fi): não largar um job que está
                # aberto ou rodando na chain, senão a máquina responde e o usuário paga sem ver a resposta
                fails += 1
                if late:
                    raise SigmaError(LATE_CLAIMED)
                time.sleep(min(self.POLL_SLOW * fails, 5.0))   # recuo: não martela um nó que está voltando
                continue
            if job.get("status") in JOB_END:
                return job
            stop = bool(should_stop and should_stop())
            if (stop or late) and job.get("status") == "open" and not job.get("claims") \
                    and self._try_cancel(job_id):
                raise SigmaError(CANCELED if stop else NO_ANSWER)
            if late:
                raise SigmaError(LATE_CLAIMED)
            time.sleep(self.POLL_FAST if time.time() - start < 10 else self.POLL_SLOW)

    def _try_cancel(self, job_id):
        try:
            self.cancel_job(job_id)
            return True
        except SigmaError:
            return False   # uma máquina pegou o pedido no meio do caminho: segue esperando por ela

    def _open_result(self, job, machine, tries=3, deadline=None):
        """Resposta do pedido, cifrada para esta carteira -> dict, ou None se não abrir (não é para esta chave,
        foi alterada ou não é JSON). Falha de rede ao baixar tenta de novo e, persistindo, sobe (SigmaError).
        Com `deadline`, insiste no download até o prazo: o job já está pago, então a resposta tem de chegar."""
        res = (job.get("results") or {}).get(machine)
        h = job.get("result_hash") or (res.get("hash") if isinstance(res, dict) else None)
        if not isinstance(h, str):
            return None
        i = 0
        while True:
            try:
                blob = self.get_blob(h)
                break
            except SigmaError:
                i += 1
                if (time.time() >= deadline) if deadline is not None else (i >= tries):
                    raise
                time.sleep(self.POLL_SLOW)
        try:
            out = json.loads(self._signer().unseal(blob))
        except ValueError:
            return None
        return out if isinstance(out, dict) else None

    def _result(self, h):
        try:
            return json.loads(self.get_blob(h))
        except ValueError:
            raise SigmaError("a máquina devolveu um resultado ilegível") from None

    def compute(self, task, replicas=2, max_price=None, timeout=300):
        """Computação determinística repetida em `replicas` máquinas; quem divergir da maioria é punido."""
        try:
            units = tasks.validate(task)
        except ValueError as e:
            raise SigmaError(str(e)) from None
        if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 1:
            raise SigmaError("replicas deve ser um número inteiro a partir de 1")
        offers = sorted(p for m in self.machines().values() if isinstance(m, dict) and m.get("online")
                        and m.get("compute") for p in [(m.get("prices") or {}).get("compute_per_unit")]
                        if isinstance(p, int) and not isinstance(p, bool) and p >= 1)
        if not max_price:
            if len(offers) < replicas:
                raise SigmaError(f"só há {len(offers)} {'máquina' if len(offers) == 1 else 'máquinas'} de "
                                 f"computação online para {replicas} réplicas")
            max_price = offers[replicas - 1]   # bastam as `replicas` mais baratas: nunca o preço da mais cara
        able = sum(p <= max_price for p in offers)
        if able < replicas:
            raise SigmaError(f"só há {able} {'máquina' if able == 1 else 'máquinas'} de computação online "
                             f"(no seu preço) para {replicas} réplicas")
        job_id = self.send_tx("create_job", {"kind": "compute", "task": task, "replicas": replicas,
                                             "escrow": units * max_price * replicas, "max_price": max_price})
        job = self._wait_job(job_id, timeout)
        return (self._result(job["result_hash"]) if job.get("result_hash") else None), job
