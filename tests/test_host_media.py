"""A máquina com imagem e vídeo (regras v2, Sigma 2.5): regressões da revisão da 2.5.

    python -m unittest tests.test_host_media -v

Host de verdade (motores "mock") contra a chain falsa do test_host já na v2, com um servidor de arquivos local
(numa porta livre de 8863 a 8869), e (registro sem o motor de texto) contra a chain de verdade nas regras v2:

- pedido reivindicado que não abre (número infinito, JSON aninhado demais, erro que ninguém previu) ou cujo motor
  levanta um erro inesperado sempre recebe resposta: sem ela, viraria falta da máquina no prazo;
- o pedido de imagem aberto no claim não é baixado de novo para gerar;
- recusar um modelo não apaga o VAE (ou codificador) que outro modelo daqui usa, e um modelo de mídia cujo
  arquivo sumiu volta a baixá-lo em vez de seguir "hospedando";
- a recusa vale só para o arquivo que falhou e só para o que deu errado nele, uma recusa não apaga outra do mesmo
  arquivo (nenhuma das duas volta a baixar), e a atribuição recusada é recusada na rede uma vez;
- um erro inesperado num modelo não impede os outros de carregar;
- o motor de texto fora do ar não tira da máquina os modelos de imagem.
"""
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import host as hostmod  # noqa: E402
from sigma import node as nodemod  # noqa: E402
from sigma.client import SigmaError  # noqa: E402
from sigma.crypto import Wallet  # noqa: E402
from test_chain import model  # noqa: E402
from test_chain_v25 import A as SPONSOR, image_model, v2  # noqa: E402
from test_host import FakeChain, FakeClient, _copy, ai_job, gguf_bytes, opened, txs_of  # noqa: E402

FILES_PORTS = range(8869, 8862, -1)   # a primeira livre de 8869 a 8863 (outros testes podem estar rodando)
BASE = None
HW = {"cpu_cores": 4, "ram_mb": 16000, "vram_mb": 0, "gpu": "", "disk_free_mb": 50_000}
PRICE = 20_000   # µSIG por imagem (o padrão da rede)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safetensors(payload=b"\0" * 64):
    header = {"w": {"dtype": "F32", "shape": [len(payload) // 4], "data_offsets": [0, len(payload)]}}
    raw = json.dumps(header).encode()
    return struct.pack("<Q", len(raw)) + raw + payload


class Files(BaseHTTPRequestHandler):
    files, seen = {}, []

    def log_message(self, *a):
        pass

    def do_GET(self):
        name = self.path.strip("/")
        self.seen.append(name)
        data = self.files.get(name)
        self.send_response(200 if data is not None else 404)
        self.send_header("Content-Length", str(len(data or b"")))
        self.end_headers()
        self.wfile.write(data or b"")


def serve(name, data):
    """Publica `data` no servidor de arquivos: {url, sha256, size}, como o padrinho registraria."""
    Files.files[name] = data
    return {"url": f"{BASE}/{name}", "sha256": sha(data), "size": len(data)}


class FilesServer(ThreadingHTTPServer):
    allow_reuse_address = False   # no Windows, SO_REUSEADDR deixaria dois servidores abrirem a mesma porta


SERVER = None


def setUpModule():
    global SERVER, BASE
    for port in FILES_PORTS:
        try:
            SERVER = FilesServer(("127.0.0.1", port), Files)
            break
        except OSError:
            continue
    else:
        raise OSError(f"nenhuma porta livre de {FILES_PORTS[-1]} a {FILES_PORTS[0]}")
    BASE = f"http://127.0.0.1:{port}"
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def tearDownModule():
    SERVER.shutdown()
    SERVER.server_close()


class MediaClient(FakeClient):
    """O cliente falso do test_host com a rede já na v2: o /mining diz `media` e as atribuições levam a classe e
    os arquivos extras do modelo, como o nó manda."""

    def mining(self):
        return dict(super().mining(), media=True)

    def machine(self, addr):
        out = super().machine(addr)
        for a in out["assignments"]:
            m = self.c.models[a["name"]]
            a.update(task=m.get("task", "text"), files=_copy(m.get("files") or []),
                     main_size=m.get("main_size", m["size"]))
        return out


class HostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sghm")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        detect = mock.patch.object(hostmod.hardware, "detect", return_value=dict(HW))
        detect.start()
        self.addCleanup(detect.stop)
        Files.files.clear()
        Files.seen.clear()
        self.chain = FakeChain()
        self.wallet = Wallet.generate()
        self.me = self.wallet.address
        self.client = Wallet.generate()   # quem pede
        self.logs = []
        self.h = self.host()
        self.h._register_if_needed()

    def host(self):
        """Uma máquina (a mesma carteira e a mesma pasta de dados: chamar de novo é reiniciar o app)."""
        h = hostmod.Host("http://127.0.0.1:1", self.wallet, "0x" + "cd" * 20, os.path.join(self.tmp, "maquina"),
                         {"engine": "mock", "ai_image": True, "compute_enabled": False, "disk_gb": 5,
                          "ai_ram_gb": 4}, log=self.logs.append)
        h.client = MediaClient(self.chain, self.me)
        os.makedirs(h.models_dir, exist_ok=True)
        os.makedirs(h.blob_dir, exist_ok=True)
        self.addCleanup(h.engine.shutdown)
        return h

    def image_model(self, name, main, files=()):
        self.chain.models[name] = {"name": name, "url": main["url"], "sha256": main["sha256"],
                                   "size": main["size"] + sum(f["size"] for f in files), "main_size": main["size"],
                                   "task": "image", "files": [dict(f) for f in files],
                                   "assigned": {self.me: self.chain.window}, "pinners": {}}

    def text_model(self, name, f):
        self.chain.models[name] = {"name": name, "url": f["url"], "sha256": f["sha256"], "size": f["size"],
                                   "assigned": {self.me: self.chain.window}, "pinners": {}}

    def sync(self, h=None):
        h = h or self.h
        h._refresh()
        h._sync_models()

    def status(self, name, h=None):
        return {m["name"]: m for m in (h or self.h).stats()["models"]}.get(name)

    def path(self, f, ext=".safetensors"):
        return os.path.join(self.h.models_dir, f["sha256"] + ext)

    def refused_file(self):
        try:
            with open(self.h.refused_path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    # pedidos
    def media_job(self, jid, spec, model="img", units=1):
        """Pedido de imagem direcionado a esta máquina, cifrado para ela; `spec` em bytes (pode vir quebrado)."""
        job = ai_job(self.chain, jid, self.client, spec, me=self.me, model=model, sealed_to=self.wallet.pub,
                     escrow=PRICE * units)
        job.update(media="image", units=units)
        return job

    def text_job(self, jid, spec, model="texto"):
        return ai_job(self.chain, jid, self.client, spec, me=self.me, model=model, sealed_to=self.wallet.pub)

    def answer(self):
        self.h._work_jobs()        # uma exceção que escapa daqui deixaria o job reivindicado sem resposta
        self.h._drain_answers()
        self.h._drain_queue()

    def outcome(self, jid):
        j = self.chain.jobs[jid]
        if self.me not in j["results"]:
            return j["status"], None
        return j["status"], opened(self.chain, self.client, jid, self.me)[1].get("code")


class TestRequests(HostCase):
    """Todo pedido direcionado que esta máquina reivindica recebe resposta (sucesso ou falha): um pedido sem
    resposta vira falta no prazo (1.800 s para imagem), e três faltas tiram o modelo da máquina, de graça para
    quem fez (achados 1, 2 e 14 da revisão da 2.5)."""

    def setUp(self):
        super().setUp()
        self.image_model("img", serve("img.safetensors", safetensors(b"\1" * 64)))
        self.text_model("texto", serve("texto.gguf", gguf_bytes(self.tmp, 300_000)))
        self.sync()
        self.assertEqual({m["name"]: m["status"] for m in self.h.stats()["models"]},
                         {"img": "hospedando", "texto": "hospedando"})

    def test_poisoned_requests_are_answered(self):
        # números infinitos (o json do Python aceita Infinity e 1e999) e JSON aninhado demais (RecursionError)
        image = {"i-seed": b'{"prompt": "x", "seed": Infinity}', "i-steps": b'{"prompt": "x", "steps": 1e999}',
                 "i-count": b'{"prompt": "x", "count": -Infinity}', "i-fundo": b"[" * 100_000}
        text = {"t-max": b'{"messages": [{"role": "user", "content": "oi"}], "max_tokens": Infinity}',
                "t-fundo": b"[" * 100_000}
        for jid, spec in image.items():   # um de cada vez: a fila de imagem (2) e o limite por cliente ficam fora
            self.media_job(jid, spec)
            self.answer()
        for jid, spec in text.items():
            self.text_job(jid, spec)
            self.answer()
        for jid in [*image, *text]:
            status, code = self.outcome(jid)
            self.assertIn(status, ("done", "failed"), jid)   # nunca "running" sem resposta
            if status == "failed":
                self.assertEqual(code, "unreadable", jid)
        for jid in ("i-fundo", "t-max", "t-fundo"):   # o host é quem abre: viram falha "unreadable" na hora
            self.assertEqual(self.outcome(jid), ("failed", "unreadable"), jid)

    def test_unexpected_error_opening_a_claimed_request_still_answers(self):
        self.media_job("bug", b'{"prompt": "um gato"}')
        with mock.patch.object(hostmod.media, "clean_spec", side_effect=RuntimeError("bug")):
            self.answer()
        self.assertEqual(self.outcome("bug"), ("failed", "unreadable"))

    def test_request_opened_only_at_answer_time_is_answered_too(self):
        """O nó não entregou o pedido na hora do claim: ele é aberto na hora de gerar, e o que não abre ali (JSON
        aninhado demais, erro que ninguém previu) também volta como falha."""
        jobs = {"tarde-fundo": self.media_job("tarde-fundo", b"[" * 100_000),
                "tarde-bug": self.media_job("tarde-bug", b'{"prompt": "um gato"}')}
        hidden = {jid: self.chain.blobs.pop(j["spec_hash"]) for jid, j in jobs.items()}
        self.h._work_jobs()
        self.assertTrue(all(self.me in self.chain.jobs[jid]["claims"] for jid in jobs))
        for jid, blob in hidden.items():   # o nó voltou
            self.chain.blobs[jobs[jid]["spec_hash"]] = blob
        real = hostmod.media.clean_spec

        def clean(task, spec, units):
            if isinstance(spec, dict) and spec.get("prompt") == "um gato":
                raise RuntimeError("bug")
            return real(task, spec, units)
        with mock.patch.object(hostmod.media, "clean_spec", side_effect=clean):
            self.h._drain_answers()
            self.h._drain_queue()
        for jid in jobs:
            self.assertEqual(self.outcome(jid), ("failed", "unreadable"), jid)

    def test_unexpected_engine_error_fails_the_job(self):
        """Um NUL no prompt faz o Popen levantar ValueError, que não é EngineError: a geração falha 'engine'."""
        def boom(*a, **k):
            raise ValueError("embedded null character")
        self.h.media.generate = boom
        self.media_job("nul", b'{"prompt": "um gato"}')
        self.answer()
        self.assertEqual(self.outcome("nul"), ("failed", "engine"))
        self.assertEqual(self.h._pausing, 0)   # o minerador não fica pausado

    def test_image_request_opened_at_claim_is_not_downloaded_again(self):
        self.media_job("uma", b'{"prompt": "um gato"}')
        self.h._work_jobs()
        gets = self.h.client.calls["get_blob"]
        self.h._drain_queue()
        self.assertEqual((self.outcome("uma"), self.h.client.calls["get_blob"]), (("done", None), gets))

    def test_node_hiccup_at_answer_time_does_not_fail_an_opened_request(self):
        self.media_job("tropeco", b'{"prompt": "um gato"}')
        self.h._work_jobs()

        def down(h):
            raise SigmaError("não consegui falar com o nó")
        self.h.client.get_blob = down
        self.h._drain_queue()
        self.assertEqual(self.outcome("tropeco"), ("done", None))

    def test_price_raised_after_the_claim_keeps_the_claimed_job(self):
        """A chain cobra o preço gravado no claim: subir o preço depois não pode virar 'busy'."""
        self.media_job("preco", b'{"prompt": "um gato"}')
        self.h._work_jobs()
        self.h._record["prices"]["ai_per_image"] = 2 * PRICE
        self.h._drain_queue()
        self.assertEqual(self.outcome("preco"), ("done", None))


class TestSharedFiles(HostCase):
    """O mesmo VAE (mesmo SHA-256) em dois modelos de imagem: recusar um não apaga o arquivo do outro, que seguiria
    "hospedando" com todo pedido falhando (achado 3 da revisão da 2.5)."""

    def setUp(self):
        super().setUp()
        self.vae = serve("ae.safetensors", safetensors(b"\7" * 64))
        self.image_model("b-img", serve("b.safetensors", safetensors(b"\2" * 64)), [dict(self.vae, role="vae")])
        self.sync()
        self.assertEqual(self.status("b-img")["status"], "hospedando")
        self.vae_path = self.path(self.vae)

    def assert_vae_kept(self):
        self.assertTrue(self.h._verified(self.vae_path, self.vae["size"]))
        self.assertEqual(self.status("b-img")["status"], "hospedando")

    def test_refusing_a_model_keeps_the_vae_another_model_uses(self):
        main = serve("a.safetensors", safetensors(b"\3" * 64))
        self.image_model("a-img", dict(main, sha256=sha(b"outro arquivo")), [dict(self.vae, role="vae")])
        self.sync()
        self.assertEqual(self.status("a-img")["error"], hostmod.REFUSED_TEXT["sha"])
        self.assert_vae_kept()

    def test_wrong_size_for_a_shared_vae_refuses_only_the_wrong_model(self):
        main = serve("a.safetensors", safetensors(b"\3" * 64))
        self.image_model("a-img", main, [dict(self.vae, role="vae", size=self.vae["size"] + 1)])
        self.sync()
        self.assertEqual(self.status("a-img")["error"], hostmod.REFUSED_TEXT["sha"])
        self.assert_vae_kept()
        h2 = self.host()   # reiniciou o app: a recusa do tamanho errado não barra o modelo certo
        self.sync(h2)
        self.assertEqual((self.status("b-img", h2)["status"], self.status("a-img", h2)["status"]),
                         ("hospedando", "recusado"))

    def test_hosted_media_model_whose_file_vanished_downloads_it_again(self):
        os.remove(self.vae_path)
        os.remove(self.vae_path + ".ok")
        Files.seen.clear()
        self.sync()
        self.assertIn("ae.safetensors", Files.seen)
        self.assert_vae_kept()


class TestRefusals(HostCase):
    """A recusa fica gravada pelo arquivo que falhou e vale só para o que deu errado nele: um modelo de texto que
    aponta para um safetensors não bloqueia o modelo de imagem com o mesmo arquivo, e a isca com um VAE errado não
    bloqueia o modelo certo que usa o mesmo principal (achado 4 da revisão da 2.5). Duas recusas do mesmo arquivo
    ficam as duas gravadas: se uma apagasse a outra, cada volta do laço baixaria as duas de novo."""

    def test_text_refusal_does_not_block_the_image_model_with_the_same_file(self):
        sd = serve("sd15.safetensors", safetensors(b"\4" * 64))
        self.text_model("sd15-texto", sd)   # registrado como texto (por engano ou de propósito)
        self.sync()
        self.assertEqual(self.status("sd15-texto")["error"], hostmod.REFUSED_TEXT["gguf"])
        self.image_model("sd15", sd)
        self.sync()
        self.assertEqual(self.status("sd15")["status"], "hospedando")
        self.assertIn(("pin_model", {"name": "sd15"}), self.chain.txs)
        self.assertEqual(self.status("sd15-texto")["status"], "recusado")

    def test_bait_with_a_bad_vae_does_not_block_the_right_model(self):
        main = serve("z.safetensors", safetensors(b"\5" * 64))
        vae = serve("zvae.safetensors", safetensors(b"\6" * 64))
        junk = serve("isca-vae.safetensors", b"x" * 100)
        bad = dict(junk, role="vae", sha256=sha(b"y" * 100))   # o endereço não entrega esse SHA-256
        self.image_model("isca", main, [bad])
        self.image_model("z-legit", main, [dict(vae, role="vae")])
        self.sync()
        self.assertEqual(self.status("isca")["error"], hostmod.REFUSED_TEXT["sha"])
        self.assertEqual(self.status("z-legit")["status"], "hospedando")
        refused = self.refused_file()
        self.assertEqual(set(refused), {bad["sha256"]})   # o arquivo que falhou, não o principal
        self.assertEqual({k: refused[bad["sha256"]][k] for k in ("reason", "url", "size")},
                         {"reason": "sha", "url": junk["url"], "size": 100})

    def test_registration_error_records_and_deletes_nothing(self):
        gg = serve("zg.gguf", gguf_bytes(self.tmp, 300_000))
        self.image_model("z-gguf", gg)
        self.sync()
        self.assertEqual(self.status("z-gguf")["status"], "hospedando")
        self.image_model("isca", gg, [{"role": "vae", "url": f"{BASE}/ae.bin", "sha256": sha(b"q"), "size": 10}])
        self.sync()
        self.assertEqual(self.status("isca")["error"], hostmod.REFUSED_TEXT["weights"])
        self.assertTrue(self.h._verified(self.path(gg, ".gguf"), gg["size"]))   # o principal do z-gguf ficou
        self.assertEqual(self.refused_file(), {})
        self.assertIn(("decline_model", {"name": "isca"}), self.chain.txs)

    def assert_not_downloaded_again(self, names, refused):
        """Mais voltas do laço (a chain falsa não tira a atribuição no decline, como enquanto ele não entra num
        bloco) e um reinício do app: nenhum arquivo recusado é baixado de novo, e cada modelo segue recusado."""
        gets = len(Files.seen)
        for h in (self.h, self.h, self.host()):
            self.sync(h)
            self.assertEqual(len(Files.seen), gets)
            self.assertEqual({n: self.status(n, h)["error"] for n in names}, refused)

    def test_text_and_image_refusals_of_the_same_file_are_both_kept(self):
        """O mesmo arquivo, que não é GGUF nem safetensors, num modelo de texto e num de imagem: cada um baixa uma
        vez e recusa, e uma recusa não apaga a outra (senão cada volta baixaria os dois de novo)."""
        junk = serve("lixo.safetensors", b"\7" * 100)
        self.text_model("lixo-texto", junk)
        self.image_model("lixo-imagem", junk)
        self.sync()
        self.assertEqual(Files.seen.count("lixo.safetensors"), 2)
        refused = {"lixo-texto": hostmod.REFUSED_TEXT["gguf"], "lixo-imagem": hostmod.REFUSED_TEXT["weights"]}
        self.assert_not_downloaded_again(["lixo-texto", "lixo-imagem"], refused)
        entry = self.refused_file()[junk["sha256"]]   # a mais nova no registro (o formato de antes), a outra em others
        self.assertEqual({entry["reason"], *(e["reason"] for e in entry.get("others") or [])}, {"gguf", "weights"})
        self.h._load_refused()[junk["sha256"]]["until"] = time.time() - 1   # passaram as 24 h de uma delas
        gets = len(Files.seen)
        self.sync()
        self.assertEqual(len(Files.seen), gets + 1)   # só aquela tenta de novo (e recusa de novo); a outra segue
        self.assertEqual({n: self.status(n)["error"] for n in refused}, refused)

    def test_baits_declaring_the_same_sha_are_all_kept(self):
        """Iscas que declaram o mesmo SHA-256 com outro endereço ou outro tamanho: a recusa de cada declaração fica
        gravada, sem apagar a das outras."""
        bad = sha(b"y" * 100)   # o SHA-256 que as iscas declaram; os endereços entregam outra coisa
        u1, u2 = serve("isca1.safetensors", b"x" * 100), serve("isca2.safetensors", b"x" * 100)
        self.image_model("isca-1", dict(u1, sha256=bad))
        self.image_model("isca-2", dict(u2, sha256=bad))             # outro endereço
        self.image_model("isca-3", dict(u1, sha256=bad, size=90))    # outro tamanho (o arquivo passa dele)
        self.sync()
        self.assertEqual(len(Files.seen), 3)
        self.assert_not_downloaded_again(["isca-1", "isca-2", "isca-3"],
                                         dict.fromkeys(["isca-1", "isca-2", "isca-3"], hostmod.REFUSED_TEXT["sha"]))

    def test_refused_assignment_is_declined_once(self):
        """Arquivo já recusado (por exemplo, depois de reiniciar o app): a atribuição é recusada na rede uma vez,
        sem baixar de novo. Parada, ela venceria e, sem ninguém hospedando, contaria contra o modelo."""
        self.text_model("sd15-texto", serve("sd15.safetensors", safetensors(b"\4" * 64)))
        self.sync()
        h2 = self.host()
        before = len(txs_of(self.chain, "decline_model"))
        for _ in range(3):
            self.sync(h2)
        self.assertEqual(txs_of(self.chain, "decline_model")[before:], [{"name": "sd15-texto"}])
        self.assertEqual(self.status("sd15-texto", h2)["status"], "recusado")


class TestModelsIndependent(HostCase):
    """Um erro que ninguém previu num modelo (ex.: SIGMA_SD_CLI apontando para um arquivo que não existe) não
    impede os modelos seguintes, de texto inclusive, de carregar (achado 15 da revisão da 2.5)."""

    def test_unexpected_error_in_one_model_does_not_stop_the_others(self):
        self.image_model("a-imagem", serve("a.safetensors", safetensors(b"\6" * 64)))
        self.text_model("b-texto", serve("b.gguf", gguf_bytes(self.tmp, 300_000)))
        calls = []

        def boom(name, task, files):
            calls.append(name)
            raise KeyError("próprio")
        self.h.media.load = boom
        self.sync()
        self.assertEqual(self.status("b-texto")["status"], "hospedando")
        self.assertIn(("pin_model", {"name": "b-texto"}), self.chain.txs)
        st = self.status("a-imagem")
        self.assertEqual(st["status"], "erro")
        self.assertIn("KeyError", st["error"])
        self.sync()   # espera o prazo antes de tentar de novo, como num erro qualquer
        self.assertEqual(calls, ["a-imagem"])


class ChainClient:
    """O pedaço do Client que o registro da máquina usa, ligado à chain de verdade nas regras v2 (cada tx entra
    num bloco na hora)."""

    def __init__(self, net, wallet):
        self.net, self.wallet, self.node = net, wallet, "http://127.0.0.1:1"

    def mining(self):
        st = self.net.state
        return {"window": st.mining["window"], "window_seconds": st.params["window_seconds"], "media": st.rule(2)}

    def machine(self, addr):
        rec = nodemod.machine_info(self.net.state, addr)
        if rec is None:
            raise SigmaError("máquina não registrada")
        return _copy(rec)

    def machines(self):
        return _copy(self.net.state.machines)

    def send_tx(self, type_, data, wait=True, timeout=30):
        self.net.send(self.wallet, type_, data)
        self.net.block()


class TestTextEngineDown(unittest.TestCase):
    """O motor de texto fora do ar (o Ollama fechado) por mais que o ENGINE_GRACE: a máquina continua com IA para
    imagem e vídeo, que rodam no stable-diffusion.cpp, e a rede só tira dela os modelos de texto (achado 9 da
    revisão da 2.5)."""

    def test_image_models_stay_when_the_text_engine_goes_down(self):
        tmp = tempfile.mkdtemp(prefix="sghm")
        self.addCleanup(shutil.rmtree, tmp, True)
        net = v2()
        net.fund(SPONSOR.address)
        net.send(SPONSOR, "register_model", model("texto"))
        net.send(SPONSOR, "register_model", image_model())
        net.block()
        wallet = Wallet.generate()
        me = wallet.address
        with mock.patch.object(hostmod.hardware, "detect", return_value=dict(HW)):
            h = hostmod.Host("http://127.0.0.1:1", wallet, "0x" + "cd" * 20, tmp,
                             {"engine": "mock", "ai_image": True, "ai_ram_gb": 4, "disk_gb": 5}, log=lambda m: None)
            h.client = ChainClient(net, wallet)
            h._register_if_needed()
            self.assertEqual(net.state.machines[me]["tasks"], ["text", "image"])
            s = net.step(wallet)
            for name in ("texto", "z-image"):
                self.assertIn(me, s.models[name]["assigned"])
                net.send(wallet, "pin_model", {"name": name})
            s = net.block()
            self.assertEqual(sorted(s.machines[me]["models"]), ["texto", "z-image"])

            h.engine.available = lambda: False   # o Ollama fechou
            h._register_if_needed()               # dentro do prazo de tolerância: nada muda
            h._engine_down_since = time.time() - hostmod.ENGINE_GRACE - 1
            h._register_if_needed()
            mc = net.state.machines[me]
            self.assertEqual((mc["ai"], mc["tasks"], mc["models"]), (True, ["image"], ["z-image"]))
            self.assertIn(me, net.state.models["z-image"]["pinners"])
            self.assertNotIn(me, net.state.models["texto"]["pinners"])
            self.assertIsNone(h._why_not("z-image"))   # e continua gerando imagem

            del h.engine.available               # o Ollama voltou
            h._register_if_needed()
            self.assertEqual(net.state.machines[me]["tasks"], ["text", "image"])
            s = net.step(wallet)
            self.assertIn(me, s.models["texto"]["assigned"])   # o texto volta para ela
            self.assertIn(me, s.models["z-image"]["pinners"])
            h.engine.shutdown()

    def test_first_registration_with_the_text_engine_down_still_offers_images(self):
        tmp = tempfile.mkdtemp(prefix="sghm")
        self.addCleanup(shutil.rmtree, tmp, True)
        net = v2()
        wallet = Wallet.generate()
        with mock.patch.object(hostmod.hardware, "detect", return_value=dict(HW)):
            h = hostmod.Host("http://127.0.0.1:1", wallet, "0x" + "cd" * 20, tmp,
                             {"engine": "mock", "ai_image": True, "ai_ram_gb": 4, "disk_gb": 5}, log=lambda m: None)
            h.client = ChainClient(net, wallet)
            h.engine.available = lambda: False
            h._register_if_needed()
            mc = net.state.machines[wallet.address]
            self.assertEqual((mc["ai"], mc["tasks"]), (True, ["image"]))
            h.update_settings({"ai_image": False})   # sem imagem e sem texto: sem IA
            h._register_if_needed()
            mc = net.state.machines[wallet.address]
            self.assertEqual((mc["ai"], mc["tasks"]), (False, []))
            h.engine.shutdown()


if __name__ == "__main__":
    unittest.main()
