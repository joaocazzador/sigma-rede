"""Testes do cliente (sigma/client.py): pedido de IA privado (SPEC-v2.1 §12), corte da conversa e tetos de preço.

A rede é falsa e roda dentro do processo: FakeNet herda de Client e responde get/post/put_blob/get_blob como o
nó, com máquinas que reivindicam, deixam expirar, falham com cada código ou respondem. O único servidor HTTP de
verdade sobe em 127.0.0.1:8850 e é desligado no fim. Nada sai da máquina.
"""
import argparse
import base64
import collections
import copy
import hashlib
import http.server
import io
import json
import math
import os
import random
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from sigma import catalog, chain, cli, client, crypto, engines, netsafe
from sigma.chain import canon
from sigma.client import Client, SigmaError
from sigma.crypto import Wallet, address_of

MODEL = "modelo-teste"
QUESTION = "qual é a receita secreta do bolo da vovó?"
PROMPT = "um farol azul na tempestade"
FAIL_TEXT = {"context": "a conversa passou do limite do modelo (4.096 tokens)",
             "engine": "o motor de IA desta máquina falhou", "busy": "fila cheia", "unreadable": "pedido ilegível"}
V2_PRICES = {"ai_per_1k_tokens": 2000, "compute_per_unit": 500, **chain.MEDIA_PRICES}   # máquina das regras v2
PNG = b"\x89PNG\r\n\x1a\n" + b"imagem de teste"
WEBM = b"\x1aE\xdf\xa3" + b"video de teste"
MEDIA_TASKS = {"z-image": "image", "wan": "video"}
T0 = 1_000_000.0


class FakeClock:
    """Relógio de mentira para o módulo client (mock.patch.object(client, "time", relógio)): sleep só avança."""

    def __init__(self, t=T0):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class FakeNet(Client):
    """Nó + máquinas de mentira. Cada consulta a /job/<id> faz a máquina dar o próximo passo do seu roteiro."""
    POLL_FAST = POLL_SLOW = 0.001

    def __init__(self, wallet, window=50, default_price=2000, balance=10 ** 9):
        super().__init__("http://rede-falsa.invalid", wallet)
        self.window, self.default_price = window, default_price
        self.balances = {wallet.address: balance}
        self.nonces, self.records, self.keys, self.behavior = {}, {}, {}, {}
        self.blobs, self.uploads, self.txs, self.jobs, self.polls = {}, [], [], {}, {}
        self.requests = []          # pedidos que as máquinas abriram com a chave delas (para conferir)
        self.create_mode, self.tx_mode = {}, {}   # máquina -> como o create_job termina; txid -> /tx/<id>
        self.blob_failures = 0      # quantas vezes o download de uma resposta falha antes de dar certo
        self.job_failures = 0       # quantas vezes /job/<id> cai (nó reiniciando) antes de voltar
        self.tasks = {}             # modelo -> classe ("image", "video") que o /models diz; o resto é texto
        self.models_down = False    # o /models falha (timeout) enquanto o /machines responde
        self.media = False          # o /mining diz que a rede já aceita imagem e vídeo (regras v2)
        self.clock = time.time      # relógio das máquinas de imagem/vídeo (os testes de prazo trocam)

    def add(self, name, behavior="ok", price=2000, seen=0, create="ok", **extra):
        """Máquina com o roteiro `behavior`; `seen` = janelas desde o último sinal (0 = nesta janela).
        `create`: "ok", "dropped" (o nó aceita o create_job e depois descarta), "unconfirmed" (o job entra,
        mas o cliente não consegue confirmar), "full" (a chain recusa na hora: fila cheia) ou "full_dropped"
        (entra no mempool e é descartado com "fila cheia" na produção do bloco)."""
        key = Wallet.generate()
        rec = {"name": name, "payout": key.address, "online": True, "ai": True, "pub": key.pub,
               "last_seen": self.window - seen, "models": [MODEL], "compute": False,
               "prices": {"ai_per_1k_tokens": price, "compute_per_unit": 500}}
        rec.update(extra)
        self.records[key.address] = rec
        self.keys[key.address] = key
        self.behavior[key.address] = behavior
        self.create_mode[key.address] = create
        return key.address

    def name_of(self, addr):
        return self.records[addr]["name"]

    def created(self):
        return [d for t, d in self.txs if t == "create_job"]

    # ------------------------------------------------------------ "HTTP"
    def get(self, path, timeout=10):
        if path == "/mining":
            out = {"window": self.window, "media": self.media}
            if self.default_price is not None:
                out["ai_default_price"] = self.default_price
            return out
        if path == "/machines":
            return copy.deepcopy(self.records)
        if path == "/models":
            if self.models_down:
                raise SigmaError("não consegui falar com o nó: tempo esgotado")
            names = {n for r in self.records.values() for n in (r.get("models") or []) + (r.get("media_models") or [])}
            return {n: dict({"name": n}, **({"task": self.tasks[n]} if n in self.tasks else {})) for n in names}
        if path.startswith("/account/"):
            a = path.split("/")[2]
            return {"address": a, "balance": self.balances.get(a, 0), "next_nonce": self.nonces.get(a, 0)}
        if path.startswith("/tx/"):
            mode = self.tx_mode.get(path.split("/")[2])
            if mode == "unreachable":
                raise SigmaError("não consegui falar com o nó: tempo esgotado")
            if mode == "dropped":
                return {"status": "dropped", "error": "a máquina escolhida não está online agora"}
            if mode == "full_dropped":
                return {"status": "dropped", "error": "a máquina escolhida está com a fila cheia agora; escolha outra"}
            return {"status": "included"}
        if path.startswith("/job/"):
            if self.job_failures > 0:        # nó reiniciando/Wi-Fi caiu: a consulta falha por um tempo
                self.job_failures -= 1
                raise SigmaError("não consegui falar com o nó: o nó respondeu HTTP 502")
            job = self.jobs.get(path.split("/")[2])
            if job is None:
                raise SigmaError("job não existe")
            self._tick(job)
            return copy.deepcopy(job)
        raise SigmaError(f"rota desconhecida no teste: {path}")

    def post(self, path, payload, timeout=10):
        assert path == "/tx", path
        tx = payload
        sender = address_of(tx["pub"])
        assert crypto.verify(tx["pub"], canon(chain.tx_body(tx)), tx["sig"]), "assinatura inválida"
        if tx["nonce"] != self.nonces.get(sender, 0):
            raise SigmaError("nonce inválido")
        cmode = self.create_mode.get(tx["data"].get("machine")) if tx["type"] == "create_job" else None
        if cmode == "full":                      # a chain recusa na hora: fila cheia (nem entra no mempool)
            raise SigmaError("a máquina escolhida está com a fila cheia agora; escolha outra")
        if cmode in ("dropped", "full_dropped"):
            self.tx_mode[tx["id"]] = cmode       # entrou no mempool e foi descartada antes do bloco
            return {"id": tx["id"]}
        getattr(self, "_tx_" + tx["type"])(sender, tx["data"], tx["id"])
        if cmode == "unconfirmed":
            self.tx_mode[tx["id"]] = "unreachable"
        self.nonces[sender] = tx["nonce"] + 1
        self.txs.append((tx["type"], copy.deepcopy(tx["data"])))
        return {"id": tx["id"]}

    def put_blob(self, data):
        data = bytes(data)
        self.uploads.append(data)
        h = hashlib.sha256(data).hexdigest()
        self.blobs[h] = data
        return h

    def get_blob(self, h):
        if self.blob_failures > 0:
            self.blob_failures -= 1
            raise SigmaError("não consegui falar com o nó: conexão caída")
        if h not in self.blobs:
            raise SigmaError("blob não encontrado")
        return self.blobs[h]

    # ------------------------------------------------------------ chain
    def _tx_create_job(self, sender, d, txid):
        if d.get("kind") == "compute":
            assert set(d) == {"kind", "task", "replicas", "escrow", "max_price"}, d
            self.balances[sender] -= d["escrow"]
            h = self.put_blob(canon({"count": 168}))
            self.jobs[txid] = {"id": txid, "client": sender, "kind": "compute", "status": "done", "claims": {},
                               "results": {}, "result_hash": h, "escrow": d["escrow"], "cost": 0}
            return
        task = self.tasks.get(d.get("model"), "text")   # imagem e vídeo (regras v2) levam units, não max_tokens
        assert set(d) == {"kind", "model", "spec_hash", "escrow", "max_price", "machine", "client_pub",
                          "max_tokens" if task == "text" else "units"}, d
        if task != "text":
            if not (isinstance(d["units"], int) and 1 <= d["units"] <= chain.MEDIA_UNITS[task]):
                raise SigmaError("units fora do limite")
            if d["escrow"] < d["units"] * d["max_price"]:
                raise SigmaError(f"o valor reservado precisa cobrir {d['units']} unidades")
        elif not (isinstance(d["max_tokens"], int) and not isinstance(d["max_tokens"], bool)
                  and 1 <= d["max_tokens"] <= client.CONTEXT):   # §2: max_tokens público, 1..CONTEXT
            raise SigmaError("max_tokens precisa estar entre 1 e 4096")
        elif d["escrow"] < math.ceil((d["max_tokens"] + 16) * d["max_price"] / 1000):
            raise SigmaError(f"o valor reservado precisa cobrir {d['max_tokens']} tokens")   # §2: cobre o preço
        if d["spec_hash"] not in self.blobs:
            raise SigmaError("envie o pedido (/blob) antes de criar o job")
        m = self.records.get(d["machine"])
        if not m or not m["online"] or m["last_seen"] < self.window - 1 or not m.get("pub") or not m["ai"] \
                or d["model"] not in (m.get("models") or []) + (m.get("media_models") or []) \
                or d["machine"] == sender:
            raise SigmaError("esta máquina não pode atender o pedido")
        if m["prices"][chain.TASK_PRICE[task]] > d["max_price"]:
            raise SigmaError("o preço da máquina está acima do máximo")
        if address_of(d["client_pub"]) != sender:
            raise SigmaError("client_pub não é de quem pede")
        if self.balances.get(sender, 0) < d["escrow"]:
            raise SigmaError("saldo insuficiente para o escrow")
        self.balances[sender] -= d["escrow"]
        size = {"max_tokens": d["max_tokens"]} if task == "text" else {"media": task, "units": d["units"]}
        self.jobs[txid] = {"id": txid, "client": sender, "kind": "ai", "model": d["model"], "status": "open",
                           "claims": {}, "results": {}, "escrow": d["escrow"], "max_price": d["max_price"],
                           "machine": d["machine"], "client_pub": d["client_pub"], "spec_hash": d["spec_hash"],
                           "result_hash": None, **size}
        self.polls[txid] = 0

    def _tx_register_model(self, sender, d, txid):
        pass                                     # o teste confere o que foi enviado (self.txs)

    def _tx_cancel_job(self, sender, d, txid):
        job = self.jobs[d["job_id"]]
        if job["client"] != sender:
            raise SigmaError("só quem pediu pode cancelar")
        if job["status"] != "open" or job["claims"]:
            raise SigmaError("um computador já está respondendo este pedido")
        self._close(job, "canceled", job["escrow"])

    def _tx_transfer(self, sender, d, txid):
        pass

    def _close(self, job, status, refund=0):
        job["status"] = status
        self.balances[job["client"]] += refund

    # ------------------------------------------------------------ máquinas
    def _tick(self, job):
        if job["kind"] != "ai" or job["status"] not in ("open", "running"):
            return
        n = self.polls[job["id"]] = self.polls[job["id"]] + 1
        m, how = job["machine"], self.behavior[job["machine"]]
        if job.get("media"):
            return self._tick_media(job, m, how)
        if how == "never":
            return
        if how == "expire":                     # dormindo: nunca reivindica, o prazo devolve o valor
            if n >= 2:
                job["missed"] = True
                self._close(job, "expired", job["escrow"])
            return
        if not job["claims"]:
            job["claims"][m] = {"time": 0, "price": self.records[m]["prices"]["ai_per_1k_tokens"]}
            job["status"] = "running"
            if how in ("slow_ok", "claim_expire"):
                return
        if how == "claim_expire":
            if n >= 3:
                self._close(job, "expired", job["escrow"])
            return
        if how == "slow_ok" and n < 3:
            return
        try:                                    # a máquina abre o pedido com a chave dela
            req = json.loads(self.keys[m].unseal(self.blobs[job["spec_hash"]]))
            assert set(req) == {"messages", "max_tokens", "json"}, req
            assert all(set(x) == {"role", "content"} for x in req["messages"])
            self.requests.append(dict(req, machine=m))
        except (ValueError, AssertionError):
            how = "fail:unreadable"
        if how.startswith("fail:"):
            code = how[5:]
            self._answer(job, {"text": "", "error": FAIL_TEXT.get(code, code), "code": code,
                               "model": job["model"], "machine": m}, failed=True)
        elif how == "fail_plain":               # falha sem cifra: não dá para saber se é dela mesmo
            self._answer(job, {"text": "", "error": "Sua carteira foi bloqueada", "code": "context",
                               "model": job["model"], "machine": m}, failed=True, sealed=False)
        elif how == "old_context":              # host antigo: falha como resposta vazia, de custo 0
            self._answer(job, {"text": "", "error": "a conversa\npassou do limite " + "x" * 300,
                               "code": "context", "model": job["model"], "machine": m})
        elif how == "garbage":                  # resposta sem cifra: não é para esta carteira
            h = hashlib.sha256(b'{"text": "oi"}').hexdigest()
            self.blobs[h] = b'{"text": "oi"}'
            job["results"][m] = {"hash": h}
            job.update(result_hash=h, cost=3)
            self._close(job, "done", job["escrow"] - 3)
        else:
            self._answer(job, {"text": f"resposta de {self.name_of(m)}", "model": job["model"], "machine": m},
                         cost=7)

    def _tick_media(self, job, m, how):
        """Pedido de imagem/vídeo: a máquina pega na hora e, conforme `how`, gera ("ok"), nunca entrega ("hang":
        a chain não encerra o job) ou, `s` segundos do relógio `clock` depois de pegar, entrega ("done:s"),
        falha ("fail:s") ou some ("expire:s": a chain devolve o valor)."""
        now, price = self.clock(), self.records[m]["prices"][chain.TASK_PRICE[job["media"]]]
        if not job["claims"]:
            job["claims"][m] = {"time": now, "price": price}
            job["status"] = "running"
        end, _, secs = how.partition(":")
        if how == "hang" or (secs and now < job["claims"][m]["time"] + float(secs)):
            return
        if end == "expire":
            job["missed"] = True
            return self._close(job, "expired", job["escrow"])
        if end == "fail":
            return self._answer(job, {"text": "", "error": "o motor de imagem e vídeo desta máquina falhou",
                                      "code": "engine", "model": job["model"], "machine": m}, failed=True)
        req = json.loads(self.keys[m].unseal(self.blobs[job["spec_hash"]]))   # a máquina abre com a chave dela
        assert req["task"] == job["media"] and req["prompt"] == PROMPT, req
        self.requests.append(dict(req, machine=m))
        if job["media"] == "image":
            items = [{"mime": "image/png", "data": base64.b64encode(PNG).decode(), "width": 64, "height": 64}]
            items *= job["units"]
        else:
            items = [{"mime": "video/webm", "data": base64.b64encode(WEBM).decode(), "seconds": job["units"]}]
        self._answer(job, {"media": items, "task": job["media"], "model": job["model"], "machine": m},
                     cost=job["units"] * price)

    def _answer(self, job, res, failed=False, cost=0, sealed=True):
        blob = crypto.seal(job["client_pub"], canon(res)) if sealed else canon(res)
        h = hashlib.sha256(blob).hexdigest()
        self.blobs[h] = blob
        m = job["machine"]
        job["results"][m] = {"hash": h, "time": 0, "usage": {"tokens_in": 0, "tokens_out": 0}}
        if failed:
            job["failed"] = True
            self._close(job, "failed", job["escrow"])
        else:
            job.update(result_hash=h, cost=cost)
            self._close(job, "done", job["escrow"] - cost)


def ask_msgs(text=QUESTION):
    return [{"role": "user", "content": text}]


class AskTest(unittest.TestCase):
    def setUp(self):
        self.me = Wallet.generate()
        self.net = FakeNet(self.me)

    def tearDown(self):
        # nada que o nó recebeu do cliente estava legível: todo pedido foi cifrado para uma máquina
        for blob in self.net.uploads:
            self.assertTrue(crypto.is_sealed(blob), blob[:60])
            self.assertNotIn(QUESTION.encode(), blob)
            self.assertNotIn(b"messages", blob)

    def test_constants(self):
        self.assertEqual(client.CONTEXT, engines.CONTEXT)
        self.assertEqual(client.JOB_END, ("done", "failed", "disputed", "expired", "canceled"))
        self.assertIsInstance(client._RNG, random.SystemRandom)

    def test_private_success(self):
        a = self.net.add("a")
        jobs = []
        text, job = self.net.ask(MODEL, ask_msgs(), max_tokens=100, on_job=jobs.append, json_mode=True)
        self.assertEqual(text, "resposta de a")
        self.assertEqual((job["machine"], job["attempts"], job["trimmed"], job["status"]), (a, 1, 0, "done"))
        self.assertEqual(jobs, [job["id"]])
        (d,) = self.net.created()
        self.assertEqual((d["machine"], d["client_pub"], d["max_price"]), (a, self.me.pub, 2000))
        est = client.estimate(ask_msgs())
        self.assertEqual(d["escrow"], math.ceil((est + 100) * 2000 / 1000))
        (req,) = self.net.requests              # a máquina abriu com a chave dela e leu o pedido certo
        self.assertEqual(req["messages"], ask_msgs())
        self.assertEqual((req["max_tokens"], req["json"]), (100, True))
        self.assertEqual(self.net.balances[self.me.address], 10 ** 9 - 7)   # pagou só os tokens usados

    def test_never_falls_back_to_a_public_job(self):
        self.net.add("velha", pub=None)          # máquina antiga, sem chave pública
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs())
        self.assertEqual(str(cm.exception), client.NO_MACHINE)
        self.assertEqual((self.net.uploads, self.net.txs), ([], []))

    def test_candidate_filter(self):
        bad = [self.net.add("offline", online=False), self.net.add("dormiu", seen=2),
               self.net.add("sem-pub", pub=None), self.net.add("ia-desligada", ai=False),
               self.net.add("outro-modelo", models=["outro"]), self.net.add("cara", price=1_000_000),
               self.net.add("chave-curta", pub="ab" * 10)]
        mine = Wallet.generate()                 # a própria carteira do cliente não atende o próprio pedido
        self.net.records[self.me.address] = dict(self.net.records[bad[0]], name="eu", online=True)
        self.net.behavior[self.me.address] = "ok"
        del mine
        good = self.net.add("boa", price=500)
        for _ in range(5):
            text, job = self.net.ask(MODEL, ask_msgs())
            self.assertEqual(job["machine"], good)
        self.assertEqual({d["machine"] for d in self.net.created()}, {good})
        self.assertEqual(self.net.created()[0]["max_price"], 2000)   # teto = max(padrão, mais barata)

    def test_ceiling_ignores_the_priciest_machine(self):
        cheap = self.net.add("barata", price=500)
        self.net.add("caríssima", price=1_000_000)   # não define o quanto se reserva nem recebe a pergunta
        for _ in range(8):
            _, job = self.net.ask(MODEL, ask_msgs(), max_tokens=50)
            self.assertEqual(job["machine"], cheap)
        for d in self.net.created():
            self.assertEqual(d["max_price"], 2000)
            self.assertLessEqual(d["escrow"], math.ceil((client.estimate(ask_msgs()) + 50) * 2000 / 1000))

    def test_ceiling_when_all_are_above_the_default(self):
        a = self.net.add("a", price=5000)
        self.net.add("b", price=9000)
        _, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual(job["machine"], a)
        self.assertEqual(self.net.created()[0]["max_price"], 5000)

    def test_node_without_default_price(self):
        self.net.default_price = None
        self.net.add("a", price=100)
        self.net.ask(MODEL, ask_msgs())
        self.assertEqual(self.net.created()[0]["max_price"], chain.DEFAULT_PRICES["ai_per_1k_tokens"])

    def test_explicit_max_price(self):
        ok = {self.net.add("a", price=500), self.net.add("b", price=550)}
        self.net.add("c", price=700)
        for _ in range(6):
            _, job = self.net.ask(MODEL, ask_msgs(), max_price=600)
            self.assertIn(job["machine"], ok)
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs(), max_price=100)
        self.assertIn("a mais barata cobra 500", str(cm.exception))

    def test_insufficient_balance_before_uploading(self):
        self.net.balances[self.me.address] = 3
        self.net.add("a")
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs())
        self.assertIn("saldo insuficiente", str(cm.exception))
        self.assertEqual(self.net.uploads, [])

    def test_retries_on_other_machines(self):
        self.net.add("dorme", "expire")          # a única que sinalizou nesta janela: sempre a primeira
        b = self.net.add("b", seen=1)
        jobs = []
        text, job = self.net.ask(MODEL, ask_msgs(), on_job=jobs.append)
        self.assertEqual((text, job["machine"], job["attempts"]), ("resposta de b", b, 2))
        self.assertEqual(len(jobs), 2)
        self.assertEqual([self.net.name_of(d["machine"]) for d in self.net.created()], ["dorme", "b"])

    def test_three_attempts_each_on_a_different_machine(self):
        self.net.add("motor", "fail:engine")
        self.net.add("ilegivel", "fail:unreadable")
        c = self.net.add("c", seen=1)
        text, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual((job["machine"], job["attempts"]), (c, 3))
        machines = [d["machine"] for d in self.net.created()]
        self.assertEqual(len(set(machines)), 3)
        self.assertEqual(self.net.balances[self.me.address], 10 ** 9 - 7)   # as falhas devolveram tudo

    def test_busy_and_claim_expiry_move_on(self):
        self.net.add("cheia", "fail:busy")
        self.net.add("sumiu", "claim_expire")
        d = self.net.add("d", seen=1)
        _, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual(job["machine"], d)

    def test_attempts_exhausted(self):
        for i in range(4):
            self.net.add(f"m{i}", "expire")
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs())
        self.assertEqual(str(cm.exception), client.NO_ANSWER)
        machines = [d["machine"] for d in self.net.created()]
        self.assertEqual((len(machines), len(set(machines))), (3, 3))   # 3 tentativas, nunca a 4ª
        self.assertEqual(self.net.balances[self.me.address], 10 ** 9)  # não pagou nada

    def test_context_failure_stops_at_once(self):
        self.net.add("a", "fail:context")
        self.net.add("b", seen=1)
        self.net.add("c", seen=1)
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs())
        self.assertEqual(str(cm.exception), FAIL_TEXT["context"])
        self.assertEqual(len(self.net.created()), 1)
        self.assertEqual(self.net.balances[self.me.address], 10 ** 9)

    def test_context_is_read_at_call_time(self):
        # a demo mostra a falha "context" da máquina: com o limite do cliente aumentado, a conversa vai inteira
        self.net.add("a", "fail:context")
        with mock.patch.object(client, "CONTEXT", 1_000_000):
            with self.assertRaises(SigmaError) as cm:
                self.net.ask(MODEL, ask_msgs("x" * 20_000))
        self.assertEqual(str(cm.exception), FAIL_TEXT["context"])
        self.assertEqual(len(self.net.requests[0]["messages"][0]["content"]), 20_000)
        self.assertEqual(self.net.balances[self.me.address], 10 ** 9)   # o valor reservado voltou

    def test_context_failure_from_an_old_host(self):
        self.net.add("antiga", "old_context")
        self.net.add("b", seen=1)
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs())
        msg = str(cm.exception)                  # frase de outra máquina: curta e numa linha só
        self.assertTrue(msg.startswith("a conversa passou do limite"))
        self.assertLessEqual(len(msg), 200)
        self.assertNotIn("\n", msg)
        self.assertEqual(len(self.net.created()), 1)

    def test_answer_not_sealed_for_me_is_refused(self):
        self.net.add("a", "garbage")
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs())
        self.assertIn("ilegível", str(cm.exception))

    def test_unreadable_failure_does_not_stop_the_question(self):
        # falha sem cifra (com um texto qualquer): não é da máquina oficial, então nem a frase nem o "context"
        # valem; o valor já voltou e a pergunta vai para a próxima
        self.net.add("a", "fail_plain")
        b = self.net.add("b", seen=1)
        text, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual((text, job["machine"], job["attempts"]), ("resposta de b", b, 2))

    def test_dropped_create_job_moves_on(self):
        self.net.add("sumiu", create="dropped")   # o nó aceitou e descartou: nada foi reservado
        b = self.net.add("b", seen=1)
        jobs = []
        text, job = self.net.ask(MODEL, ask_msgs(), on_job=jobs.append)
        # on_job é chamado logo após cada txid (para o Ctrl+C), inclusive o do create descartado
        self.assertEqual((job["machine"], job["attempts"]), (b, 2))
        self.assertEqual((len(jobs), jobs[-1]), (2, job["id"]))

    def test_unconfirmed_create_job_never_creates_a_second_one(self):
        self.net.add("a", "never", create="unconfirmed")
        self.net.add("b", seen=1)
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs())
        self.assertIn("não confirmou o pedido", str(cm.exception))
        self.assertNotIn("não pagou nada", str(cm.exception))
        self.assertEqual(len(self.net.created()), 1)

    def test_answer_download_is_retried(self):
        self.net.add("a")
        self.net.blob_failures = 2               # a rede cai duas vezes ao baixar a resposta já paga
        text, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual(text, "resposta de a")

    def test_answer_download_keeps_failing(self):
        self.net.add("a")
        self.net.add("b", seen=1)
        self.net.blob_failures = 10 ** 6         # nunca baixa dentro do prazo
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs(), timeout=0.1)   # insiste até o prazo (não só 3 vezes) e aí sobe a causa
        self.assertIn("não consegui falar com o nó", str(cm.exception))   # a causa real, não "ilegível"
        self.assertEqual(len(self.net.created()), 1)   # já foi paga: não pergunta de novo para outra máquina

    def test_done_job_download_survives_more_than_three_failures(self):
        self.net.add("a")
        self.net.blob_failures = 10              # mais que o antigo limite de 3 tentativas: já foi pago, insiste
        text, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual(text, "resposta de a")

    def test_cancel_unclaimed_job(self):
        self.net.add("a", "never")
        self.net.add("b", seen=1)
        jobs = []
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs(), on_job=jobs.append, should_stop=lambda: bool(jobs))
        self.assertEqual(str(cm.exception), client.CANCELED)
        self.assertEqual([t for t, _ in self.net.txs], ["create_job", "cancel_job"])
        self.assertEqual(self.net.jobs[jobs[0]]["status"], "canceled")
        self.assertEqual(self.net.balances[self.me.address], 10 ** 9)   # o valor reservado voltou

    def test_stop_before_anything(self):
        self.net.add("a")
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs(), should_stop=lambda: True)
        self.assertEqual(str(cm.exception), client.CANCELED)
        self.assertEqual(self.net.txs, [])

    def test_claimed_job_is_not_canceled(self):
        a = self.net.add("a", "slow_ok")
        jobs = []
        text, job = self.net.ask(MODEL, ask_msgs(), on_job=jobs.append,
                                 should_stop=lambda: bool(self.net.jobs[jobs[0]]["claims"]) if jobs else False)
        self.assertEqual((text, job["machine"]), ("resposta de a", a))
        self.assertNotIn("cancel_job", [t for t, _ in self.net.txs])

    def test_stop_while_claimed_then_failed_does_not_retry(self):
        self.net.add("a", "claim_expire")
        self.net.add("b", seen=1)
        jobs = []
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs(), on_job=jobs.append,
                         should_stop=lambda: bool(jobs) and bool(self.net.jobs[jobs[0]]["claims"]))
        self.assertEqual(str(cm.exception), client.CANCELED)
        self.assertEqual(len(self.net.created()), 1)

    def test_timeout_cancels_unclaimed_and_refunds(self):
        self.net.add("a", "never")
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs(), timeout=0.05)
        self.assertEqual(str(cm.exception), client.NO_ANSWER)
        self.assertIn("cancel_job", [t for t, _ in self.net.txs])
        self.assertEqual(self.net.balances[self.me.address], 10 ** 9)

    def test_fresh_first_then_uniform_random(self):
        fresh = {self.net.add(f"nova{i}") for i in range(3)}
        self.net.add("velha0", seen=1)
        self.net.add("velha1", seen=1)
        count = collections.Counter()
        for _ in range(45):
            _, job = self.net.ask(MODEL, ask_msgs("oi"))
            count[job["machine"]] += 1
        self.assertEqual(set(count), fresh)     # quem sinalizou nesta janela vem antes
        for m in fresh:                          # ~15 cada; ninguém fica com todas as perguntas
            self.assertGreaterEqual(count[m], 3, count)

    def test_order_machines_distribution(self):
        recs = [(f"f{i}", {"last_seen": 9}) for i in range(4)] + [(f"s{i}", {"last_seen": 8}) for i in range(3)]
        first, second_group = collections.Counter(), collections.Counter()
        for _ in range(4000):
            order = client.order_machines(list(recs), 9)
            self.assertEqual({a for a, _ in order[:4]}, {"f0", "f1", "f2", "f3"})
            first[order[0][0]] += 1
            second_group[order[4][0]] += 1
        for name in ("f0", "f1", "f2", "f3"):
            self.assertTrue(800 < first[name] < 1200, first)
        for name in ("s0", "s1", "s2"):
            self.assertTrue(1000 < second_group[name] < 1700, second_group)

    def test_long_conversation_is_trimmed(self):
        self.net.add("a")
        msgs = [{"role": "system", "content": "regras"}, {"role": "user", "content": "primeira pergunta"}]
        for i in range(30):
            msgs.append({"role": "assistant" if i % 2 else "user", "content": f"{i} " + "palavra " * 200})
        msgs.append({"role": "user", "content": QUESTION})
        text, job = self.net.ask(MODEL, msgs, max_tokens=512)
        self.assertGreater(job["trimmed"], 0)
        sent = self.net.requests[0]["messages"]
        self.assertEqual(len(sent), len(msgs) - job["trimmed"])
        self.assertEqual(sent[0]["content"], "regras")
        self.assertEqual(sent[1]["content"], "primeira pergunta")
        self.assertEqual(sent[-1]["content"], QUESTION)
        self.assertLessEqual(client.estimate(sent) + 512, client.CONTEXT - 32)

    def test_huge_max_tokens_is_clamped(self):
        self.net.add("a")
        self.net.ask(MODEL, ask_msgs(), max_tokens=8192)
        self.assertEqual(self.net.requests[0]["max_tokens"], client.CONTEXT // 2)

    # ---------------------------------------------------- §2 escrow cobre o preço
    def test_create_job_sends_max_tokens_and_enough_escrow(self):
        # o cliente manda o max_tokens público e reserva o bastante para pagar o preço combinado
        self.net.add("a", price=2000)
        self.net.ask(MODEL, ask_msgs(), max_tokens=200)
        (d,) = self.net.created()
        self.assertEqual(d["max_tokens"], 200)
        self.assertGreaterEqual(d["escrow"], math.ceil((d["max_tokens"] + 16) * d["max_price"] / 1000))

    # ---------------------------------------------------- §1 fila cheia pula a candidata
    def test_full_queue_falls_through_to_the_next_machine(self):
        self.net.add("cheia", create="full")     # a chain recusa na hora: fila cheia
        b = self.net.add("b", seen=1)
        _, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual((job["machine"], job["attempts"]), (b, 1))   # não gastou tentativa na fila cheia
        self.assertEqual([self.net.name_of(d["machine"]) for d in self.net.created()], ["b"])

    def test_full_queue_does_not_use_up_the_three_attempts(self):
        # mais candidatas com fila cheia do que o limite de 3 tentativas: ainda chega numa com vaga
        for i in range(5):
            self.net.add(f"cheia{i}", create="full")
        b = self.net.add("boa", seen=1)          # na janela anterior: vem depois das "cheia" na ordem
        _, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual((job["machine"], job["attempts"]), (b, 1))

    def test_full_queue_dropped_in_the_block_also_falls_through(self):
        self.net.add("cheia", create="full_dropped")   # entra no mempool e é descartado com "fila cheia"
        b = self.net.add("b", seen=1)
        _, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual((job["machine"], job["attempts"]), (b, 1))

    def test_all_queues_full_reports_the_reason(self):
        for i in range(3):
            self.net.add(f"cheia{i}", create="full")
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs())
        self.assertIn("fila cheia", str(cm.exception))
        self.assertEqual(self.net.created(), [])   # nenhum pedido chegou a ser criado

    # ---------------------------------------------------- §8 espera do cliente
    def test_wait_keeps_polling_through_a_brief_outage(self):
        # o nó cai por mais polls do que os 10 do v2.1; a máquina ainda responde e o cliente não desiste
        a = self.net.add("a", "slow_ok")
        self.net.job_failures = 25
        text, job = self.net.ask(MODEL, ask_msgs())
        self.assertEqual((text, job["machine"]), ("resposta de a", a))
        self.assertEqual(self.net.balances[self.me.address], 10 ** 9 - 7)   # a resposta paga chegou

    def test_outage_past_the_deadline_is_charged_only_if_delivered(self):
        self.net.add("a", "slow_ok")             # reivindicou; está viva, só não entregou ainda
        self.net.job_failures = 10 ** 6          # o nó não volta antes do prazo
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs(), timeout=0.1)
        self.assertEqual(str(cm.exception), client.LATE_CLAIMED)
        self.assertEqual(len(self.net.created()), 1)   # não cria um segundo pedido por cima

    def test_on_job_is_called_right_after_send_tx(self):
        # Ctrl+C durante a confirmação do create_job: on_job já recebeu o id, então dá para cancelar o job
        self.net.add("a", "never")
        seen = []

        def boom(txid, timeout=30):
            raise KeyboardInterrupt
        self.net.wait_tx = boom
        with self.assertRaises(KeyboardInterrupt):
            self.net.ask(MODEL, ask_msgs(), on_job=seen.append)
        self.assertEqual(len(seen), 1)           # o id chegou ao on_job antes do wait_tx
        self.assertIn(seen[0], self.net.jobs)    # o job foi criado na chain: é cancelável

    def test_message_too_long(self):
        self.net.add("a")
        with self.assertRaises(SigmaError) as cm:
            self.net.ask(MODEL, ask_msgs("x" * 20_000))
        self.assertEqual(str(cm.exception), client.TOO_LONG)
        self.assertEqual(self.net.txs, [])

    def test_address_only_wallet_cannot_ask(self):
        net = FakeNet(self.me)
        net.wallet = self.me.address
        with self.assertRaises(SigmaError) as cm:
            net.ask(MODEL, ask_msgs())
        self.assertIn("só tem o endereço", str(cm.exception))

    def test_hosted_models_and_pick_model(self):
        self.net.add("coder", models=["qwen-coder", MODEL])
        self.net.add("dormiu", models=["outro"], seen=2)
        self.net.add("sem-pub", models=["velho"], pub=None)
        hosted = self.net.hosted_models()
        self.assertEqual(set(hosted), {"qwen-coder", MODEL})
        self.assertEqual(self.net.pick_model(prefer="coder"), "qwen-coder")

    def test_pick_model_does_not_guess_the_class(self):
        """Revisão 2.5 #23: sem o /models não dá para saber quais modelos são de texto. O pick_model não chuta
        "texto": se o /models falhar, o erro sobe, em vez de escolher o de imagem (que está em mais máquinas)."""
        self.net.add("texto", models=["qwen2.5-0.5b"])
        for i in range(2):                       # nó que não separa os de mídia: o de imagem vem em `models`
            self.net.add(f"img{i}", models=["z-image"], prices=dict(V2_PRICES))
        self.net.tasks = dict(MEDIA_TASKS)
        self.assertEqual(self.net.pick_model(prefer="coder"), "qwen2.5-0.5b")
        self.net.models_down = True              # o /models dá timeout enquanto o /machines responde
        with self.assertRaises(SigmaError) as cm:
            self.net.pick_model(prefer="coder")
        self.assertIn("não consegui falar com o nó", str(cm.exception))
        self.assertEqual(self.net.txs, [])

    def test_hosted_models_reads_the_media_models_too(self):
        """Revisão 2.5 #24: o nó põe os modelos de imagem e vídeo de cada máquina em `media_models` (um app 2.4
        só entende texto); o cliente 2.5 lê os dois campos, cada um com o preço da sua classe."""
        t = self.net.add("texto", models=["qwen2.5-0.5b"])
        i = self.net.add("img", models=[], media_models=["z-image", "wan"], prices=dict(V2_PRICES))
        self.net.tasks = dict(MEDIA_TASKS)
        self.assertEqual(self.net.hosted_models(), {"qwen2.5-0.5b": {t: 2000}, "z-image": {i: 20_000},
                                                    "wan": {i: 100_000}})
        self.net.models_down = True              # sem a classe, o que o nó pôs à parte não vira texto
        self.assertEqual(self.net.hosted_models(), {"qwen2.5-0.5b": {t: 2000}})

    def test_cancel_job_tx(self):
        self.net.add("a", "never")
        self.net.default_price = 2000
        jobs = []
        stop = threading.Event()
        with self.assertRaises(SigmaError):
            self.net.ask(MODEL, ask_msgs(), on_job=lambda j: (jobs.append(j), stop.set()), should_stop=stop.is_set)
        self.assertIn(("cancel_job", {"job_id": jobs[0]}), self.net.txs)


class GenerateTest(unittest.TestCase):
    """client.generate (regras v2): imagens e vídeo cifrados para uma máquina, pagos pelo que ela entregar."""

    def setUp(self):
        self.me = Wallet.generate()
        self.nets = []

    def tearDown(self):
        for net in self.nets:                    # todo pedido foi cifrado para uma máquina
            for blob in net.uploads:
                self.assertTrue(crypto.is_sealed(blob), blob[:60])
                self.assertNotIn(PROMPT.encode(), blob)

    def net(self, *machines, clock=None):
        """Rede com as máquinas [(nome, roteiro, janelas desde o último sinal)], que listam os modelos de imagem
        e vídeo em `media_models`, como o nó faz. Com `clock`, o tempo do cliente e das máquinas é o dele."""
        net = FakeNet(self.me)
        net.tasks = dict(MEDIA_TASKS)
        if clock:
            net.clock = clock.time
            net.POLL_FAST = net.POLL_SLOW = 20.0    # cada consulta avança 20 s no relógio falso
        self.nets.append(net)
        addrs = [net.add(name, how, seen=seen, models=[], media_models=list(MEDIA_TASKS), prices=dict(V2_PRICES))
                 for name, how, seen in machines]
        return net, addrs

    def test_image_from_a_machine_that_lists_it_apart(self):
        net, (a,) = self.net(("a", "ok", 0))
        items, job = net.generate("z-image", "image", {"prompt": PROMPT, "size": "512x512", "count": 2})
        self.assertEqual([(x["mime"], x["data"], x["width"]) for x in items], [("image/png", PNG, 64)] * 2)
        self.assertEqual((job["machine"], job["attempts"], job["status"]), (a, 1, "done"))
        (d,) = net.created()
        self.assertEqual((d["units"], d["max_price"], d["escrow"]), (2, 20_000, 40_000))
        self.assertEqual(net.balances[self.me.address], 10 ** 9 - 40_000)

    def test_each_attempt_gets_the_whole_time_the_chain_gives_it(self):
        """Revisão 2.5 #13: a 1ª máquina falha (ou some) perto do fim do prazo dela. A 2ª pega o pedido e gera com
        calma: o cliente espera por ela até a chain encerrar o job, em vez de desistir no prazo que sobrou da
        1ª (a entrega seria cobrada sem a mídia aparecer)."""
        for task, model, first, second, spec in (
                ("video", "wan", "fail:7080", "done:3000", {"prompt": PROMPT, "seconds": 2}),
                ("image", "z-image", "expire:1806", "done:900", {"prompt": PROMPT, "count": 1})):
            with self.subTest(task=task, first=first):
                clock = FakeClock()
                net, (a, b) = self.net(("a", first, 0), ("b", second, 1), clock=clock)
                with mock.patch.object(client, "time", clock):
                    items, job = net.generate(model, task, spec)
                self.assertEqual((job["machine"], job["attempts"], job["status"]), (b, 2, "done"))
                self.assertEqual([d["machine"] for d in net.created()], [a, b])
                units = spec.get("count") or spec.get("seconds")
                self.assertEqual(net.balances[self.me.address], 10 ** 9 - units * V2_PRICES[chain.TASK_PRICE[task]])
                self.assertGreater(clock.t - T0, chain.MEDIA_CLAIM_TIMEOUT[task] + 600)   # passou do prazo antigo

    def test_late_media_says_when_the_value_comes_back(self):
        """Revisão 2.5 #13: se a chain não encerrar o job a tempo, o aviso diz o prazo da máquina (até 2 h no
        vídeo), e não 'em alguns minutos'."""
        self.assertIn(f"{chain.MEDIA_CLAIM_TIMEOUT['image'] // 60} minutos", client.LATE_MEDIA["image"])
        self.assertIn(f"{chain.MEDIA_CLAIM_TIMEOUT['video'] // 3600} horas", client.LATE_MEDIA["video"])
        clock = FakeClock()
        net, _ = self.net(("a", "hang", 0), ("b", "ok", 1), clock=clock)
        with mock.patch.object(client, "time", clock), self.assertRaises(SigmaError) as cm:
            net.generate("wan", "video", {"prompt": PROMPT, "seconds": 3})
        self.assertEqual(str(cm.exception), client.LATE_MEDIA["video"])
        self.assertNotIn("alguns minutos", str(cm.exception))
        self.assertEqual(len(net.created()), 1)   # não cria outro pedido por cima do que a máquina pegou
        self.assertGreaterEqual(clock.t - T0, chain.TARGET_TIMEOUT + chain.MEDIA_CLAIM_TIMEOUT["video"])


class FitMessagesTest(unittest.TestCase):
    def test_keeps_system_first_user_and_recent(self):
        msgs = [{"role": "system", "content": "s" * 30}, {"role": "user", "content": "começo"}]
        msgs += [{"role": "user" if i % 2 else "assistant", "content": "y" * 3000} for i in range(10)]
        out, dropped = client.fit_messages(msgs, 1024)
        self.assertEqual(out[:2], msgs[:2])
        self.assertEqual(out[-1], msgs[-1])
        self.assertEqual(dropped, len(msgs) - len(out))
        self.assertEqual(out[2:], msgs[len(msgs) - len(out) + 2:])   # as mais recentes, sem buraco
        self.assertLessEqual(client.estimate(out), 4096 - 1024 - 32)

    def test_nothing_to_drop(self):
        msgs = ask_msgs("oi")
        self.assertEqual(client.fit_messages(msgs, 1024), (msgs, 0))

    def test_exact_boundary(self):
        # estimativa = len//3 + 8 por mensagem + 16; cabe se estimativa <= context - max_tokens - 32
        room = 1000 - 100 - 32 - 16 - 8
        ok = [{"role": "user", "content": "a" * (room * 3)}]
        self.assertEqual(client.fit_messages(ok, 100, context=1000)[1], 0)
        with self.assertRaises(SigmaError):
            client.fit_messages([{"role": "user", "content": "a" * (room * 3 + 3)}], 100, context=1000)

    def test_first_user_dropped_only_if_it_cannot_fit(self):
        # espaço: 4096 - 1024 - 32 = 3040; "e agora?" ocupa 2 + 8, "ok" 0 + 8, mais 16 da conversa
        def conv(n):
            return [{"role": "user", "content": "z" * n}, {"role": "assistant", "content": "ok"},
                    {"role": "user", "content": "e agora?"}]
        # 9000 caracteres = 3008: com a última e os 16 dá 3034, cabe; a primeira do usuário fica e "ok" sai
        out, dropped = client.fit_messages(conv(9000), 1024)
        self.assertEqual([m["content"] for m in out], ["z" * 9000, "e agora?"])
        self.assertEqual(dropped, 1)
        self.assertLessEqual(client.estimate(out), 4096 - 1024 - 32)
        # 9100 caracteres = 3041: não cabe nem sozinha com a última, então sai e as recentes ficam
        out, dropped = client.fit_messages(conv(9100), 1024)
        self.assertEqual([m["content"] for m in out], ["ok", "e agora?"])
        self.assertEqual(dropped, 1)

    def test_last_message_too_long(self):
        with self.assertRaises(SigmaError) as cm:
            client.fit_messages([{"role": "system", "content": "s"}, {"role": "user", "content": "x" * 12_000}], 1024)
        self.assertEqual(str(cm.exception), client.TOO_LONG)

    def test_cleans_messages(self):
        out, _ = client.fit_messages([{"role": "user", "content": "oi", "extra": 1}], 10)
        self.assertEqual(out, [{"role": "user", "content": "oi"}])
        with self.assertRaises(SigmaError):
            client.fit_messages(["texto solto"], 10)


class ComputeAndMiscTest(unittest.TestCase):
    def test_compute_ceiling_is_the_nth_cheapest(self):
        me = Wallet.generate()
        net = FakeNet(me)
        for name, price in (("a", 100), ("b", 300), ("c", 5000)):
            net.add(name, compute=True, prices={"ai_per_1k_tokens": 2000, "compute_per_unit": price})
        task = {"name": "primes", "params": {"n": 1000}}
        net.compute(task, replicas=2)
        (d,) = net.created()
        self.assertEqual(d["max_price"], 300)                  # bastam as 2 mais baratas: nunca a de 5000
        self.assertEqual(d["escrow"], 1 * 300 * 2)
        with self.assertRaises(SigmaError) as cm:
            net.compute(task, replicas=4)
        self.assertIn("só há 3 máquinas", str(cm.exception))

    def test_transfer_refuses_a_mistyped_address(self):
        net = FakeNet(Wallet.generate())
        with self.assertRaises(SigmaError) as cm:
            net.transfer("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAee", 1)   # o certo termina em ...BeAed
        self.assertEqual(str(cm.exception), client.BAD_ADDRESS)
        net.transfer("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", 1)
        net.transfer("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaee", 1)        # tudo minúsculo: sem checksum
        self.assertEqual([d["to"] for t, d in net.txs if t == "transfer"],
                         ["sig5aaeb6053f3e94c9b9a09f33669435e7ef1beaed", "sig5aaeb6053f3e94c9b9a09f33669435e7ef1beaee"])
        with self.assertRaises(SigmaError) as cm:
            net.update_model("x", new_sponsor="0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAee")
        self.assertEqual(str(cm.exception), client.BAD_ADDRESS)


class CliTest(unittest.TestCase):
    """sigma/cli.py: plural, termos iguais aos do painel, licença conferida sempre, Ctrl+C e HTTPS seguro."""

    def out(self, fn, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*a)
        return buf.getvalue()

    def test_plural(self):
        self.assertEqual(cli.plural(1, "minerador", "mineradores"), "1 minerador")
        self.assertEqual(cli.plural(2, "minerador", "mineradores"), "2 mineradores")
        self.assertEqual(cli.plural(0, "minerador", "mineradores"), "0 mineradores")
        self.assertEqual(cli.plural(None, "minerador", "mineradores"), "0 mineradores")
        self.assertEqual(cli.plural(1234, "máquina", "máquinas"), "1.234 máquinas")

    def test_rate_format(self):
        for h, want in ((0, "0 H/s"), (None, "0 H/s"), (999, "999 H/s"), (999.6, "1,0 kH/s"), (6835, "6,8 kH/s"),
                        (1_500_000, "1,5 MH/s"), (999_960, "1,0 MH/s")):
            self.assertEqual(cli.fmt_rate(h), want, h)

    def test_views_use_the_plural_and_the_panel_words(self):
        text = self.out(cli.show_epochs, [{"epoch": 4, "mint": 490_074_750_312, "miners": 1, "top": []},
                                          {"epoch": 5, "mint": 1, "miners": 3, "top": []}])
        text += self.out(cli.show_mining, {"cap": 10 ** 14, "minted": 10 ** 12, "epoch": 5, "miners_epoch": 1,
                                           "hashrate": 6835, "window": 3, "min_bits": 12, "max_shares": 64,
                                           "next_mint": 10, "epoch_ends": 100, "window_ends": 50, "now": 10,
                                           "points_total": 5})
        text += self.out(cli.show_machines, {"sigA": {"online": True, "name": "pc", "hw": {}, "jobs_done": 1,
                                                      "earned": 0}})
        self.assertIn("época 4: 490.074,750312 SIG para 1 minerador\n", text)
        self.assertIn("para 3 mineradores", text)
        self.assertIn("divididos entre 1 minerador pelo", text)
        self.assertIn("6,8 kH/s (contas por segundo)", text)
        self.assertIn("1 pedido atendido", text)
        self.assertIn("1 de 1 máquina online", text)
        for word in ("1 mineradores", "shares", "rodada", "jobs"):
            self.assertNotIn(word, text)

    def source(self, **kw):
        base = dict(name="meu-modelo", url="https://exemplo.invalid/m.gguf", sha256="ab" * 32, size=1000,
                    license="", description="", catalog=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_license_check_always_runs(self):
        with self.assertRaises(SigmaError) as cm:              # arquivo de fora: precisa dizer a licença
            cli.model_source(self.source())
        self.assertEqual(str(cm.exception), cli.NO_LICENSE_GIVEN)
        for lic in ("other", "unknown", " Other "):
            with self.assertRaises(SigmaError) as cm:
                cli.model_source(self.source(license=lic))
            self.assertEqual(str(cm.exception), cli.NO_LICENSE)
        with self.assertRaises(SigmaError) as cm:
            cli.model_source(self.source(license="cc-by-nc-4.0"))
        self.assertIn("não permite uso comercial", str(cm.exception))
        self.assertEqual(cli.model_source(self.source(license="apache-2.0"))["license"], "apache-2.0")
        self.assertFalse(cli.commercial_ok(""))
        self.assertTrue(cli.commercial_ok("MIT"))

    def test_hugging_face_model_without_a_declared_license(self):
        found = {"url": "https://huggingface.co/a/b/resolve/main/m.gguf", "sha256": "cd" * 32, "size": 5,
                 "license": "", "file": "m.gguf"}
        with mock.patch("sigma.catalog.resolve_hf_url", return_value=dict(found)):
            with self.assertRaises(SigmaError) as cm:
                cli.model_source(self.source(url=found["url"], sha256=None, size=None))
        self.assertEqual(str(cm.exception), "esse modelo não declara a licença no Hugging Face: escolha um que declare")
        with mock.patch("sigma.catalog.resolve_hf_url", return_value=dict(found, license="mit")):
            info = cli.model_source(self.source(url=found["url"], sha256=None, size=None))
        self.assertEqual((info["license"], info["sha256"]), ("mit", "cd" * 32))
        info = cli.model_source(self.source(catalog="qwen2.5-0.5b", url=None, sha256=None, size=None, name=None))
        self.assertEqual(info["name"], "qwen2.5-0.5b")

    def test_catalog_image_and_video_keep_their_class_and_files(self):
        """Revisão 2.5 #12: um item de imagem ou vídeo do catálogo vai para a rede como imagem ou vídeo, com o VAE
        e o codificador de texto (antes saía como modelo de texto, só com o arquivo principal)."""
        media = [x for x in catalog.CATALOG if x["task"] != "text"]
        self.assertEqual({x["task"] for x in media}, {"image", "video"})
        for item in media:
            info = cli.model_source(self.source(catalog=item["id"], url=None, sha256=None, size=None, name=None))
            self.assertEqual((info["task"], info["files"], info["size"]), (item["task"], item["files"], item["size"]))
        info = cli.model_source(self.source(license="apache-2.0"))      # --url: GGUF de texto, um arquivo só
        self.assertEqual((info["task"], info["files"]), ("text", None))

    def test_model_register_from_the_catalog_sends_task_and_files(self):
        net = FakeNet(Wallet.generate())
        a = cli.build_parser().parse_args(["model-register", "--catalog", "z-image-turbo", "--budget", "100"])
        a.json = False
        with self.assertRaises(SigmaError) as cm:              # a rede ainda está na v1: nada é enviado
            cli.run(a, net)
        self.assertEqual(str(cm.exception), cli.NO_MEDIA_YET)
        self.assertEqual(net.txs, [])
        net.media = True                                       # a v2 chegou
        text = self.out(cli.run, a, net)
        item = catalog.find("z-image-turbo")
        ((kind, d),) = net.txs
        self.assertEqual(kind, "register_model")
        # só o tamanho do arquivo principal: a chain soma os extras (como o app faz)
        self.assertEqual((d["name"], d["task"], d["size"], d["files"]),
                         ("z-image-turbo", "image", item["size"], item["files"]))
        self.assertIn(f"({cli.fmt_size(item['total_size'])})", text)
        a = cli.build_parser().parse_args(["model-register", "--catalog", "qwen2.5-0.5b", "--budget", "100"])
        a.json = False
        net.media = False                                      # texto não depende da v2 e vai sem task/files
        self.out(cli.run, a, net)
        self.assertNotIn("task", net.txs[-1][1])
        self.assertNotIn("files", net.txs[-1][1])

    def test_catalog_listing_shows_the_class_and_the_whole_size(self):
        lines = self.out(cli.show_catalog, catalog.CATALOG).splitlines()
        z = next(x for x in lines if x.startswith("z-image-turbo"))
        self.assertIn("imagem", z)
        self.assertIn(cli.fmt_size(catalog.find("z-image-turbo")["total_size"]), z)   # com o Qwen3 e o VAE
        self.assertIn("vídeo", next(x for x in lines if x.startswith("wan2.1-t2v-1.3b")))
        self.assertIn("texto", next(x for x in lines if x.startswith("qwen2.5-0.5b")))

    def test_machines_view_lists_the_media_models_too(self):
        """Revisão 2.5 #24: o nó põe os modelos de imagem e vídeo em `media_models`; a CLI mostra os dois."""
        text = self.out(cli.show_machines, {"sigA": {"online": True, "name": "pc", "hw": {}, "models": ["qwen"],
                                                     "media_models": ["z-image"], "jobs_done": 0, "earned": 0}})
        self.assertIn("modelos: qwen, z-image", text)

    def test_main_uses_the_safe_https_opener_first(self):
        calls = []
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8851), _Node)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            out = io.StringIO()
            with mock.patch.object(cli.netsafe, "install_default_opener", lambda: calls.append("opener")), \
                    redirect_stdout(out):
                cli.main(["--node", "http://127.0.0.1:8851", "mining", "--json"])
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(calls, ["opener"])
        self.assertEqual(json.loads(out.getvalue())["window"], 7)

    def test_send_refuses_a_mistyped_address_before_anything(self):
        tmp = tempfile.mkdtemp(prefix="cli-")
        try:
            wfile = os.path.join(tmp, "w.key")
            Wallet.generate().save(wfile)
            err = io.StringIO()
            with mock.patch.object(cli.netsafe, "install_default_opener"), redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:   # nó inexistente: nada chega a sair da máquina
                    cli.main(["--node", "http://127.0.0.1:8852", "--wallet", wfile, "send",
                              "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAee", "1"])
            self.assertEqual(cm.exception.code, 1)
            self.assertIn(client.BAD_ADDRESS, err.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def ctrl_c(self, job):
        fake = mock.Mock()
        fake.pick_model.return_value = "m"
        fake.job.return_value = job

        def ask(model, msgs, max_tokens, on_job=None, **kw):
            on_job("job-1")
            raise KeyboardInterrupt
        fake.ask.side_effect = ask
        err = io.StringIO()
        with mock.patch.object(cli, "Client", return_value=fake), mock.patch.object(cli, "load_wallet"), \
                mock.patch.object(cli.netsafe, "install_default_opener"), redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["--node", "http://127.0.0.1:8852", "ask", "oi"])
        self.assertEqual(cm.exception.code, 130)
        return fake, err.getvalue()

    def test_mac_without_certificates_gets_the_fix(self):
        err = SigmaError("não consegui falar com o nó https://x: [SSL: CERTIFICATE_VERIFY_FAILED] certificate "
                         "verify failed: unable to get local issuer certificate")
        with mock.patch.object(cli.sys, "platform", "darwin"):
            self.assertIn("Install Certificates.command", cli.friendly(err))
        with mock.patch.object(cli.sys, "platform", "win32"):
            self.assertEqual(cli.friendly(err), str(err))

    def test_text_from_the_network_cannot_drive_the_terminal(self):
        esc, osc = chr(27), chr(27) + "]52;c;cm0gLXJmIH4=" + chr(7)   # OSC 52: escreveria na área de transferência
        text = self.out(cli.show_machines, {"sigA": {"online": True, "name": "pc" + osc, "hw": {"gpu": esc + "[2J"},
                                                     "jobs_done": 0, "earned": 0}})
        text += self.out(cli.show_models, {"m": {"size": 1, "license": "mit" + esc + "[8m", "description": "oi" + osc,
                                                 "sponsor": "sigB"}})
        fake = mock.Mock()
        fake.pick_model.return_value = "m"
        fake.ask.return_value = ("resposta" + osc + "\nlinha 2\tcom tab", {"cost": 1, "machine": "sigM"})
        buf = io.StringIO()
        with mock.patch.object(cli, "Client", return_value=fake), mock.patch.object(cli, "load_wallet"), \
                mock.patch.object(cli.netsafe, "install_default_opener"), redirect_stdout(buf):
            cli.main(["--node", "http://127.0.0.1:8852", "ask", "oi"])
        text += buf.getvalue()
        self.assertNotIn(esc, text)
        self.assertNotIn(chr(7), text)
        self.assertIn("\\x1b]52", text)
        self.assertIn("\nlinha 2\tcom tab", text)          # quebra de linha e tab continuam

    def test_ctrl_c_cancels_an_unclaimed_question(self):
        fake, err = self.ctrl_c({"status": "open", "claims": {}})
        fake.cancel_job.assert_called_once_with("job-1")
        self.assertIn("Pedido cancelado. O valor reservado voltou para a sua carteira.", err)

    def test_ctrl_c_with_a_machine_already_answering(self):
        fake, err = self.ctrl_c({"status": "running", "claims": {"sigM": {}}})
        fake.cancel_job.assert_not_called()
        self.assertIn("Um computador já está respondendo", err)
        fake, err = self.ctrl_c({"status": "expired", "claims": {}})
        fake.cancel_job.assert_not_called()

    def test_ctrl_c_waits_for_inclusion_before_canceling(self):
        # o Ctrl+C chegou antes de o pedido entrar num bloco: cancel_on_interrupt espera a inclusão (wait_tx)
        fake, err = self.ctrl_c({"status": "open", "claims": {}})
        fake.wait_tx.assert_called_once_with("job-1")   # esperou a inclusão antes de olhar/cancelar
        fake.cancel_job.assert_called_once_with("job-1")

    def test_ctrl_c_when_the_request_never_entered_a_block(self):
        # a tx foi descartada: nada foi reservado, então não há o que cancelar e nem se consulta o job
        fake = mock.Mock()
        fake.pick_model.return_value = "m"
        fake.wait_tx.side_effect = client.TxDropped("transação descartada")

        def ask(model, msgs, max_tokens, on_job=None, **kw):
            on_job("job-1")
            raise KeyboardInterrupt
        fake.ask.side_effect = ask
        with mock.patch.object(cli, "Client", return_value=fake), mock.patch.object(cli, "load_wallet"), \
                mock.patch.object(cli.netsafe, "install_default_opener"), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["--node", "http://127.0.0.1:8852", "ask", "oi"])
        self.assertEqual(cm.exception.code, 130)
        fake.cancel_job.assert_not_called()
        fake.job.assert_not_called()


class _Node(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        routes = {"/status": {"height": 3}, "/mining": {"window": 7, "epoch": 1}}
        errors = {"/limite": (429, {"error": "limite de 256 relatórios"}),
                  "/recusa": (400, {"error": "share inválido"}), "/quebrado": (500, {"error": "erro interno"})}
        code, body = (200, routes[self.path]) if self.path in routes else \
            errors.get(self.path, (404, {"error": "rota desconhecida"}))
        data = json.dumps(body).encode()
        if self.path == "/proxy":   # nginx fora do ar: página HTML, sem JSON
            code, data = 502, b"<html>502 Bad Gateway</html>"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class HttpTest(unittest.TestCase):
    def test_real_http(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8850), _Node)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = Client("http://127.0.0.1:8850")
            self.assertEqual(c.status()["height"], 3)
            with self.assertRaises(SigmaError) as cm:
                c.get("/nada")
            self.assertEqual((str(cm.exception), cm.exception.status), ("rota desconhecida", 404))
            # o código HTTP vai junto: o minerador tenta de novo no 429/5xx e não no 400, pelo código e não pelo texto
            from sigma import miner
            for path, status, msg, retry in (("/limite", 429, "limite de 256 relatórios", True),
                                             ("/recusa", 400, "share inválido", False),
                                             ("/quebrado", 500, "erro interno", True),
                                             ("/proxy", 502, "o nó respondeu HTTP 502", True)):
                with self.assertRaises(SigmaError) as cm:
                    c.get(path)
                self.assertEqual((str(cm.exception), cm.exception.status), (msg, status))
                self.assertIs(miner._retryable(cm.exception), retry, path)
            with self.assertRaises(SigmaError) as cm:   # sem resposta nenhuma: sem código
                Client("http://127.0.0.1:8849").status()
            self.assertIsNone(cm.exception.status)
            self.assertTrue(miner._retryable(cm.exception))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_https_uses_the_system_certificates(self):
        seen = {}

        def fake_urlopen(req, timeout=None, context=None):
            seen["context"] = context
            raise urllib.error.URLError("sem rede no teste")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(SigmaError):
                Client("https://no-de-teste.invalid").status()
        self.assertIs(seen["context"], netsafe.ssl_context())


if __name__ == "__main__":
    unittest.main()
