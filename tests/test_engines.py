"""Testes do sigma.engines sem baixar nada da internet.

    python tests/test_engines.py

1. mock (e o GGUF que não passa no check_gguf é recusado antes de qualquer motor); 2. check_gguf: GGUFs
válidos (o de teste e um montado com tensores, listas e alinhamento) e cada regra de recusa; 3. downloads com o
opener seguro (porta 8830): rede interna recusada, redirect para a rede interna ou para file:// recusado;
4. extração .zip/.tar.gz + achar o llama-server (pacotes falsos feitos aqui); 5. instalação completa do
"llama.cpp" servida por um http.server local (porta 8732), com SHA-256 certo e errado, e o describe() com
installing/install_progress; 6. processos do llama.cpp com um llama-server falso (carregar, chave pelo ambiente,
prioridade baixa, conversar, cair e reiniciar, descarregar); 7. Vulkan que não sobe -> CPU; 8. "Ollama" falso
(porta 8733): envio do arquivo, versão mínima; 9. Ollama de verdade, só com SIGMA_TESTE_OLLAMA=1 (carrega o
GGUF do qwen2.5-coder:1.5b que já está no Ollama com um nome temporário, conversa e apaga).
"""
import functools
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from sigma import engines, netsafe  # noqa: E402
from sigma.engines import ContextOverflow, EngineError, EngineManager, RefusedFile  # noqa: E402

FAKE_SERVER = os.path.join(HERE, "fake_llama_server.py")
EXE = "llama-server.exe" if os.name == "nt" else "llama-server"
REDIRECT_PORT = 8830
_ports = iter(range(8734, 8740))


def check(cond, msg):
    print(("  ok   " if cond else "  FALHOU ") + msg, flush=True)
    if not cond:
        raise SystemExit(1)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def priority_of(pid):
    """Classe de prioridade (Windows) ou nice (outros) de um processo."""
    if os.name != "nt":
        return os.getpriority(os.PRIO_PROCESS, pid)
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    try:
        return k32.GetPriorityClass(h)
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------------------ GGUF montado à mão
def _s(text):
    data = text if isinstance(text, bytes) else text.encode()
    return struct.pack("<Q", len(data)) + data


def kv_str(key, value):
    return _s(key) + struct.pack("<I", 8) + _s(value)


def kv_u32(key, value):
    return _s(key) + struct.pack("<I", 4) + struct.pack("<I", value)


def kv_raw(key, vtype, payload):
    return _s(key) + struct.pack("<I", vtype) + payload


def arr(etype, count, items=b""):
    return struct.pack("<IQ", etype, count) + items


def tensor(name, dims, ggml_type=0, offset=0):
    return _s(name) + struct.pack("<I", len(dims)) + b"".join(struct.pack("<Q", d) for d in dims) + \
        struct.pack("<IQ", ggml_type, offset)


def gguf(kvs=(), tensors=(), version=3, n_tensors=None, n_kv=None, magic=b"GGUF", align=32, data=256):
    head = magic + struct.pack("<IQQ", version, len(tensors) if n_tensors is None else n_tensors,
                               len(kvs) if n_kv is None else n_kv) + b"".join(kvs) + b"".join(tensors)
    pad = (-len(head)) % align
    return head + b"\0" * pad + os.urandom(data)


def good_model():
    """Um GGUF com cara de modelo: arquitetura, alinhamento, vocabulário (lista de textos), lista de números,
    um bool e dois tensores alinhados."""
    vocab = arr(8, 3, _s("a") + _s("bb") + _s("ccc"))
    scores = arr(6, 3, struct.pack("<3f", 0.1, 0.2, 0.3))
    kvs = [kv_str("general.architecture", "llama"), kv_u32("general.alignment", 64),
           kv_raw("tokenizer.ggml.tokens", 9, vocab), kv_raw("tokenizer.ggml.scores", 9, scores),
           kv_raw("general.flag", 7, b"\x01"), kv_raw("llama.context_length", 10, struct.pack("<Q", 4096))]
    tensors = [tensor("token_embd.weight", [4, 4], 0, 0), tensor("output.weight", [4], 1, 64)]
    return gguf(kvs, tensors, align=64, data=256)


def part_gguf(tmp):
    print("2) check_gguf: o que passa e o que é recusado")
    path = os.path.join(tmp, "teste.gguf")
    digest = engines.write_test_gguf(path, 5000)
    with open(path, "rb") as f:
        raw = f.read()
    check(len(raw) == 5000 and digest == sha(raw) and raw[:4] == b"GGUF" and
          struct.unpack("<I", raw[4:8])[0] == 3, "write_test_gguf: tamanho exato, versão 3, devolve o SHA-256")
    engines.check_gguf(path)
    check(True, "o GGUF de teste passa no check_gguf")
    try:
        engines.write_test_gguf(os.path.join(tmp, "pequeno.gguf"), 10)
        check(False, "tamanho menor que o cabeçalho deveria falhar")
    except ValueError:
        check(True, "write_test_gguf recusa tamanho menor que o cabeçalho")

    def verdict(data):
        p = os.path.join(tmp, "caso.gguf")
        with open(p, "wb") as f:
            f.write(data)
        try:
            engines.check_gguf(p)
            return "ok"
        except RefusedFile as e:
            return f"recusado:{e.reason}:{e}"

    check(verdict(good_model()) == "ok", "GGUF com tensores, listas, bool e alinhamento 64 passa")
    check(verdict(gguf([kv_str("general.name", "v2")], version=2)) == "ok", "versão 2 passa")
    big_vocab = arr(8, 2000, b"".join(_s(f"tok{i}") for i in range(2000)))
    check(verdict(gguf([kv_raw("tokenizer.ggml.tokens", 9, big_vocab)])) == "ok", "vocabulário de 2.000 itens passa")

    bad = {
        "magic errado": gguf(magic=b"GGUX"),
        "versão 1": gguf(version=1),
        "versão 4": gguf(version=4),
        "arquivo vazio": b"",
        "cabeçalho cortado": b"GGUF" + struct.pack("<I", 3) + b"\0\0",
        "mais de 100.000 tensores": gguf(n_tensors=100_001),
        "mais de 100.000 metadados": gguf(n_kv=100_001),
        "metadado declarado e ausente": gguf(n_kv=1, data=0),
        "chave com mais de 256 bytes": gguf([kv_str("k" * 257, "x")]),
        "texto com mais de 1 MiB": gguf([kv_raw("general.name", 8, struct.pack("<Q", (1 << 20) + 1))]),
        "texto maior que o arquivo": gguf([kv_raw("general.name", 8, struct.pack("<Q", 5000) + b"abc")], data=10),
        "tipo de valor 13": gguf([kv_raw("general.x", 13, b"\0" * 8)]),
        "lista de listas": gguf([kv_raw("general.x", 9, arr(9, 1, arr(4, 1, b"\0" * 4)))]),
        "lista com mais de 10 milhões": gguf([kv_raw("general.x", 9, arr(0, 10_000_001))]),
        "lista maior que o arquivo": gguf([kv_raw("general.x", 9, arr(10, 100_000))], data=64),
        "textos da lista maiores que o arquivo": gguf([kv_raw("general.x", 9, arr(8, 50_000))], data=64),
        "texto da lista com mais de 64 KiB": gguf([kv_raw("general.x", 9, arr(8, 1, struct.pack("<Q", 70_000)))],
                                                  data=80_000),
        "alinhamento que não é potência de 2": gguf([kv_u32("general.alignment", 48)]),
        "alinhamento acima de 65536": gguf([kv_u32("general.alignment", 131072)]),
        "alinhamento zero": gguf([kv_u32("general.alignment", 0)]),
        "alinhamento com tipo errado": gguf([kv_raw("general.alignment", 10, struct.pack("<Q", 32))]),
        "nome de tensor com 64 bytes": gguf(tensors=[tensor("t" * 64, [4])]),
        "tensor sem dimensão": gguf(tensors=[tensor("t", [])]),
        "tensor com 5 dimensões": gguf(tensors=[tensor("t", [1, 1, 1, 1, 1])]),
        "dimensão zero": gguf(tensors=[tensor("t", [4, 0])]),
        "mais de 2^50 elementos": gguf(tensors=[tensor("t", [1 << 26, 1 << 25])]),
        "tipo ggml 64": gguf(tensors=[tensor("t", [4], ggml_type=64)]),
        "offset desalinhado": gguf(tensors=[tensor("t", [4], offset=16)]),
        "offset depois do fim do arquivo": gguf(tensors=[tensor("t", [4], offset=4096)], data=64),
    }
    for what, data in bad.items():
        v = verdict(data)
        check(v == f"recusado:gguf:{engines.GGUF_REFUSED}", f"recusado: {what}")
    try:
        engines.check_gguf(os.path.join(tmp, "nao-existe.gguf"))
        check(False, "arquivo inexistente deveria falhar")
    except RefusedFile:
        check(False, "arquivo inexistente não é 'GGUF inválido'")
    except EngineError as e:
        check("não encontrado" in str(e), f"arquivo inexistente: {e}")


# ------------------------------------------------------------------ downloads com o opener seguro
class Redirects(BaseHTTPRequestHandler):
    """/ok serve DATA; /para-meta, /para-file e /para-ok redirecionam (302)."""
    DATA = b""
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.seen.append(self.path)
        targets = {"/para-meta": "http://169.254.169.254/latest/meta-data/",
                   "/para-file": "file:///C:/Windows/win.ini",
                   "/para-ok": f"http://127.0.0.1:{REDIRECT_PORT}/ok"}
        if self.path in targets:
            self.send_response(302)
            self.send_header("Location", targets[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200 if self.path == "/ok" else 404)
        body = self.DATA if self.path == "/ok" else b""
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def part_downloads(tmp, servers):
    print(f"3) downloads com o opener seguro (porta {REDIRECT_PORT})")
    Redirects.DATA = os.urandom(200_000)
    good = sha(Redirects.DATA)
    srv = ThreadingHTTPServer(("127.0.0.1", REDIRECT_PORT), Redirects)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers.append(srv)
    base = f"http://127.0.0.1:{REDIRECT_PORT}"
    dest = os.path.join(tmp, "dl", "modelo.gguf")

    Redirects.seen.clear()
    try:
        engines.fetch(base + "/ok", dest, good, size=len(Redirects.DATA), opener=netsafe.safe_opener())
        check(False, "a rede interna deveria ser recusada")
    except EngineError as e:
        check(str(e) == "a URL do modelo aponta para a rede interna; recusada" and not Redirects.seen
              and not os.path.exists(dest), f"URL na rede interna: recusada sem nenhuma conexão ({e})")
    engines.fetch(base + "/ok", dest, good, size=len(Redirects.DATA), opener=netsafe.safe_opener(allow_internal=True))
    check(os.path.exists(dest), "rede de teste local (allow_internal=True): baixa normalmente")
    os.remove(dest)

    # finge que 127.0.0.1 é a internet pública: o "site do modelo" redireciona para dentro da casa
    real_blocked, real_connect = netsafe.blocked, netsafe._connect_public
    connects = []

    def spy(host, port, timeout):
        connects.append(host)
        return real_connect(host, port, timeout)

    netsafe.blocked = lambda ip: False if str(ip).split("%")[0] == "127.0.0.1" else real_blocked(ip)
    netsafe._connect_public = spy
    try:
        Redirects.seen.clear()
        try:
            engines.fetch(base + "/para-meta", dest, good, opener=netsafe.safe_opener())
            check(False, "redirect para 169.254.169.254 deveria ser recusado")
        except EngineError as e:
            check(str(e) == "a URL do modelo aponta para a rede interna; recusada",
                  f"redirect para a rede interna: {e}")
        check(Redirects.seen == ["/para-meta"] and connects == ["127.0.0.1", "169.254.169.254"]
              and not os.path.exists(dest + ".part"),
              "o IP do redirect é conferido antes de conectar (nenhum byte vai para 169.254.169.254)")
        try:
            engines.fetch(base + "/para-file", dest, good, opener=netsafe.safe_opener())
            check(False, "redirect para file:// deveria ser recusado")
        except EngineError as e:
            check("não permitido" in str(e), f"redirect para file://: {e}")
        engines.fetch(base + "/para-ok", dest, good, size=len(Redirects.DATA), opener=netsafe.safe_opener())
        check(os.path.exists(dest), "redirect para endereço público é seguido normalmente")
        os.remove(dest)
    finally:
        netsafe.blocked, netsafe._connect_public = real_blocked, real_connect
    try:
        engines.fetch(base + "/ok", dest, "0" * 64, size=len(Redirects.DATA), opener=netsafe.safe_opener(True))
        check(False, "SHA-256 errado deveria falhar")
    except RefusedFile as e:
        check(e.reason == "sha" and not os.path.exists(dest + ".part"), f"SHA-256 errado: RefusedFile ({e})")


# ------------------------------------------------------------------ llama.cpp e Ollama
def make_zip(path, inner):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{inner}/{EXE}", b"MZ fake")
        z.writestr(f"{inner}/ggml-cpu.dll", b"fake")
        z.writestr(f"{inner}/sub/{EXE}", b"MZ mais fundo")


def make_tar(path, evil=False):
    with tarfile.open(path, "w:gz") as t:
        for name, data in ((f"build/bin/{EXE}", b"#!fake"), ("build/bin/libllama.so", b"so")) + \
                (((("../fora.txt", b"x"),) if evil else ())):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            t.addfile(info, io.BytesIO(data))


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory, port):
    handler = functools.partial(_Quiet, directory=directory)
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class FakeOllama(BaseHTTPRequestHandler):
    blobs, created, deleted = {}, [], []
    version = "0.6.5"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self._send(200 if self.path.rsplit("/", 1)[-1] in self.blobs else 404)

    def do_GET(self):
        self._send(200, {"version": FakeOllama.version})

    def do_DELETE(self):
        self.deleted.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["model"])
        self._send(200)

    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.startswith("/api/blobs/"):
            digest = self.path.rsplit("/", 1)[-1]
            if "sha256:" + sha(data) != digest:
                return self._send(400, {"error": "digest mismatch"})
            self.blobs[digest] = len(data)
            return self._send(201)
        req = json.loads(data)
        if self.path == "/api/create":
            self.created.append(req)
            return self._send(200, {"status": "success"})
        if self.path == "/api/generate":
            return self._send(200, {"done": True, "done_reason": "load"})
        if self.path == "/api/chat":
            return self._send(200, {"message": {"content": "oi do ollama falso"}, "prompt_eval_count": 7,
                                    "eval_count": 4})
        self._send(404)


def part_context_overflow():
    """Estouro de contexto do motor vira ContextOverflow (não EngineError): o host mapeia para a falha "context"
    (o cliente mostra "divida o texto" e para), não "engine" (o cliente tentaria outra máquina com o mesmo
    modelo para sempre). Sem subir motor nenhum: troca só a camada HTTP (_http).
    [maq-context-overflow-mislabeled-engine, SPEC-v2.2 §8]"""
    print("3b) estouro de contexto do motor -> ContextOverflow (não 'engine')")
    msgs = [{"role": "user", "content": "texto denso demais para o contexto"}]

    class FakeProc:
        def poll(self):
            return None   # o llama-server continua no ar; quem recusou foi o pedido, não o processo

    def fake_http(err=None, out=None):
        def f(method, url, body=None, timeout=None, raw=None, headers=None):
            if err is not None:
                raise err
            return json.dumps(out).encode()
        return f

    def http_err(code, body):
        return urllib.error.HTTPError("http://127.0.0.1:1/v1/chat/completions", code, "erro", {},
                                      io.BytesIO(body.encode()))

    orig = engines._http
    tmp = tempfile.mkdtemp(prefix="sg-ctx-")
    try:
        # --- llama.cpp: HTTP 400 cujo corpo tem "exceeds the available context size" ---
        lc = engines._LlamaCpp(os.path.join(tmp, "data"), log=lambda m: None, threads=1)
        lc.procs["m"] = {"proc": FakeProc(), "port": 1, "variant": "x", "path": "p", "logf": None}
        engines._http = fake_http(err=http_err(400, "the request (13305 tokens) exceeds the available "
                                                    "context size (4096 tokens)"))
        try:
            lc.chat("m", msgs, 16)
            check(False, "llama.cpp: deveria ter levantado ContextOverflow")
        except ContextOverflow:
            check(True, "llama.cpp: HTTP 400 'exceeds the available context size' -> ContextOverflow")
        except EngineError as e:
            check(False, f"llama.cpp: virou EngineError, não ContextOverflow: {e}")

        engines._http = fake_http(err=http_err(400, "invalid grammar: unexpected token"))
        try:
            lc.chat("m", msgs, 16)
            check(False, "llama.cpp: 400 comum deveria levantar EngineError")
        except ContextOverflow:
            check(False, "llama.cpp: um 400 qualquer NÃO pode virar ContextOverflow")
        except EngineError:
            check(True, "llama.cpp: outro 400 continua 'engine' (EngineError)")

        engines._http = fake_http(err=http_err(500, "internal server error"))
        try:
            lc.chat("m", msgs, 16)
            check(False, "llama.cpp: 500 deveria levantar EngineError")
        except ContextOverflow:
            check(False, "llama.cpp: um 500 não é estouro de contexto")
        except EngineError:
            check(True, "llama.cpp: 500 continua EngineError (motor fora, tente outra)")

        # --- Ollama: não recusa; corta calado. prompt_eval_count >= CONTEXT = a entrada já encheu o contexto ---
        om = engines._Ollama(log=lambda m: None)
        engines._http = fake_http(out={"message": {"content": "resposta sobre um texto cortado"},
                                       "prompt_eval_count": engines.CONTEXT, "eval_count": 9})
        try:
            om.chat("m", msgs, 16)
            check(False, "Ollama: entrada que enche o contexto deveria levantar ContextOverflow")
        except ContextOverflow:
            check(True, f"Ollama: prompt_eval_count >= {engines.CONTEXT} (corte calado) -> ContextOverflow")
        engines._http = fake_http(out={"message": {"content": "ok"}, "prompt_eval_count": engines.CONTEXT - 1,
                                       "eval_count": 4})
        check(om.chat("m", msgs, 16) == ("ok", engines.CONTEXT - 1, 4),
              "Ollama: entrada que cabe no contexto responde normal (sem falso positivo)")
    finally:
        engines._http = orig
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tmp = tempfile.mkdtemp(prefix="sg-eng-")
    engines.free_port = lambda: next(_ports)   # só as portas reservadas para este teste (8734-8739)
    servers = []
    try:
        gguf_path = os.path.join(tmp, "modelo.gguf")
        gsha = engines.write_test_gguf(gguf_path, 2048 + 64)
        check(gsha == engines.sha256_file(gguf_path), "modelo falso gravado com write_test_gguf")
        junk = os.path.join(tmp, "nao-e-gguf.gguf")
        with open(junk, "wb") as f:
            f.write(b"GGUF" + os.urandom(2048))   # começa com o magic, mas o resto é lixo

        print("1) mock")
        em = EngineManager(os.path.join(tmp, "data"), prefer="mock", log=lambda m: None)
        check(em.available() and em.describe()["kind"] == "mock", "mock disponível")
        d = em.describe()
        check(d["installing"] is False and d["install_progress"] == 0.0,
              f"describe tem installing/install_progress: {d}")
        em.load("modelo-teste", gguf_path, gsha)
        text, tin, tout = em.chat("modelo-teste", [{"role": "user", "content": "olá rede sigma"}], 64)
        check("sigma-mock" in text and tin == 3 and tout > 0, f"chat: {text[:60]}… ({tin}+{tout} tokens)")
        code = em.chat("modelo-teste", [{"role": "system", "content": "SIGMA_CODE_PROTOCOL"},
                                        {"role": "user", "content": "crie fibonacci.py"}], 64)[0]
        check(json.loads(code)["tool"] == "write_file", "protocolo do Sigma Code (write_file)")
        check(em.loaded() == ["modelo-teste"], "loaded() lista o modelo")
        em.unload("modelo-teste")
        check(em.loaded() == [], "descarregado")
        try:
            em.load("x", os.path.join(tmp, "nao-existe.gguf"), gsha)
            check(False, "deveria recusar arquivo inexistente")
        except EngineError:
            check(True, "recusa arquivo inexistente")
        try:
            em.load("lixo", junk, engines.sha256_file(junk))
            check(False, "arquivo que não é GGUF deveria ser recusado")
        except RefusedFile as e:
            check(e.reason == "gguf" and str(e) == engines.GGUF_REFUSED and em.loaded() == [],
                  f"load confere o GGUF antes do motor: {e}")
        check(engines.CONTEXT == 4096, "CONTEXT de 4.096 tokens (o mesmo do cliente)")

        part_gguf(tmp)
        part_downloads(tmp, servers)
        part_context_overflow()

        print("4) extração e busca do llama-server")
        z = os.path.join(tmp, "pacote.zip")
        make_zip(z, "llama-b11721-bin-win-cpu-x64")
        engines.extract(z, os.path.join(tmp, "x-zip"))
        exe = engines.find_server(os.path.join(tmp, "x-zip"))
        check(exe.endswith(os.path.join("llama-b11721-bin-win-cpu-x64", EXE)), f"zip: {os.path.relpath(exe, tmp)}")
        tgz = os.path.join(tmp, "pacote.tar.gz")
        make_tar(tgz)
        engines.extract(tgz, os.path.join(tmp, "x-tar"))
        exe = engines.find_server(os.path.join(tmp, "x-tar"))
        check(exe.endswith(os.path.join("build", "bin", EXE)), f"tar.gz: {os.path.relpath(exe, tmp)}")
        evil = os.path.join(tmp, "malicioso.tar.gz")
        make_tar(evil, evil=True)
        try:
            engines.extract(evil, os.path.join(tmp, "x-evil"))
            escaped = os.path.exists(os.path.join(tmp, "fora.txt"))
            check(not escaped, "tar com '../' não escreve fora da pasta")
        except (EngineError, tarfile.TarError) as e:
            check(not os.path.exists(os.path.join(tmp, "fora.txt")), f"tar com '../' recusado ({type(e).__name__})")
        try:
            engines.find_server(os.path.join(tmp, "data"))
            check(False, "deveria faltar o llama-server")
        except EngineError:
            check(True, "pasta sem llama-server -> EngineError")

        print("5) instalação do motor por http local (porta 8732)")
        rel = os.path.join(tmp, "release")
        os.makedirs(rel)
        make_zip(os.path.join(rel, "llama-teste-cpu.zip"), "llama-teste")
        with open(os.path.join(rel, "llama-teste-cpu.zip"), "rb") as f:
            zsha = sha(f.read())
        servers.append(serve(rel, 8732))
        engines._RELEASES = "http://127.0.0.1:8732/"
        engines.LLAMA_ASSETS["teste-cpu"] = ("llama-teste-cpu.zip", zsha)
        engines.LLAMA_ASSETS["teste-errado"] = ("llama-teste-cpu.zip", "0" * 64)
        msgs = []
        em = EngineManager(os.path.join(tmp, "data"), prefer="llamacpp", log=msgs.append, threads=3)
        impl = em._engine()
        check(em.kind == "llamacpp", "motor llama.cpp escolhido")
        impl.installing = ("teste-cpu", 0.25)   # o que o painel vê enquanto o motor baixa
        d = em.describe()
        check(d["installing"] is True and d["install_progress"] == 0.25 and not d["ready"],
              f"baixando o motor: installing e install_progress ({d['detail']})")
        impl.installing = None
        exe = impl._install("teste-cpu")
        root = os.path.join(tmp, "data", "engine", "b11721")
        check(exe.startswith(os.path.join(root, "teste-cpu")) and os.path.isfile(exe), "instalado em engine/b11721/")
        check(os.path.isfile(os.path.join(root, "teste-cpu", ".ok")), "marcador .ok gravado")
        check(not os.path.exists(os.path.join(root, "llama-teste-cpu.zip")), "pacote baixado foi apagado")
        check(impl._install("teste-cpu") == exe, "segunda chamada reaproveita a instalação")
        d = em.describe()
        check(d["installing"] is False and d["install_progress"] == 0.0, "depois de instalar: installing falso")
        try:
            impl._install("teste-errado")
            check(False, "SHA errado deveria falhar")
        except EngineError as e:
            check("SHA-256" in str(e) and not os.path.exists(os.path.join(root, "teste-errado")),
                  f"SHA-256 errado recusado: {e}")

        print("6) processos do llama.cpp com um llama-server falso")
        impl.override = sys.executable
        orig = impl._command
        impl._command = lambda exe, v, p, port: [sys.executable, FAKE_SERVER] + orig(exe, v, p, port)[1:] + \
            (["--fail"] if "vulkan" in v else [])
        t = time.time()
        em.load("modelo-a", gguf_path, gsha)
        check(time.time() - t >= 0.9, f"load esperou o /health ficar ok ({time.time() - t:.1f}s)")
        proc = impl.procs["modelo-a"]["proc"]
        port = impl.procs["modelo-a"]["port"]
        cmdline = " ".join(map(str, proc.args))
        check(impl.api_key and impl.api_key not in cmdline, "a chave do llama-server não aparece na linha de comando")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/chave", timeout=5) as r:
            check(json.load(r)["key_from_env"], "a chave chega ao llama-server pela variável LLAMA_API_KEY")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", method="POST",
                                     data=json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            check(False, "sem a chave o motor não deveria responder")
        except urllib.error.HTTPError as e:
            check(e.code == 401, "uma página (sem a chave) não consegue usar o motor: 401")
        prio = priority_of(proc.pid)
        check(prio == 0x4000 if os.name == "nt" else prio >= 10,
              f"llama-server com prioridade baixa ({'BELOW_NORMAL' if prio == 0x4000 else prio})")
        text, tin, tout = em.chat("modelo-a", [{"role": "user", "content": "diga oi"}], 32)
        check(text.startswith("eco: diga oi") and tin == 2 and tout > 0,
              f"chat com a chave: {text[:50]} ({tin}+{tout})")
        js = em.chat("modelo-a", [{"role": "user", "content": "json"}], 32, json_mode=True)[0]
        check(json.loads(js) == {"eco": "json"}, "json_mode envia response_format")
        d = em.describe()
        check(d["ready"] and "modelo-a" in d["detail"] and d["installing"] is False, f"describe: {d['detail']}")
        proc.kill()
        proc.wait()
        text = em.chat("modelo-a", [{"role": "user", "content": "de novo"}], 32)[0]
        check(text.startswith("eco: de novo") and impl.procs["modelo-a"]["proc"].poll() is None,
              "processo que caiu é reiniciado na próxima conversa (com a mesma chave)")
        newproc = impl.procs["modelo-a"]["proc"]
        em.unload("modelo-a")
        check(newproc.poll() is not None and em.loaded() == [], "unload encerra o llama-server")
        try:
            em.load("lixo", junk, engines.sha256_file(junk))
            check(False, "o llama.cpp não deveria receber um arquivo que não é GGUF")
        except RefusedFile:
            check("lixo" not in impl.procs, "arquivo que não é GGUF nem chega ao llama-server")

        print("7) Vulkan que não sobe -> CPU")
        impl.override = None
        impl.variants = ["teste-vulkan", "teste-cpu"]
        impl._install = lambda v: sys.executable
        em.load("modelo-b", gguf_path, gsha)
        check(impl.procs["modelo-b"]["variant"] == "teste-cpu" and "teste-vulkan" in impl.failed,
              "caiu para a versão de CPU")
        check(any("Vulkan" in m for m in msgs), "avisou no log")
        em.shutdown()
        check(em.loaded() == [], "shutdown descarrega tudo")

        print("8) Ollama falso (porta 8733)")
        for v, ok in (("0.6.0", True), ("0.6.5", True), ("0.10.1", True), ("1.0", True), ("v0.7.2", True),
                      ("0.5.13", False), ("0.1.32", False), ("falso", False), ("", False)):
            check(engines.ollama_version_ok(v) is ok, f"versão {v or '(vazia)'} {'aceita' if ok else 'recusada'}")
        real_ollama = engines.OLLAMA_URL
        srv = ThreadingHTTPServer(("127.0.0.1", 8733), FakeOllama)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        engines.OLLAMA_URL = "http://127.0.0.1:8733"
        try:
            olog = []
            em = EngineManager(os.path.join(tmp, "data"), prefer="ollama", log=olog.append)
            check(em.available() and em.describe()["ready"], "Ollama 0.6.5: disponível")
            em.load("pequeno", gguf_path, gsha)
            check(FakeOllama.blobs.get("sha256:" + gsha) == os.path.getsize(gguf_path),
                  "arquivo enviado inteiro e conferido")
            check(FakeOllama.created[-1] == {"model": "sigma-pequeno", "files": {"model.gguf": "sha256:" + gsha},
                                             "stream": False}, "create com o nome sigma-<modelo>")
            check(em.chat("pequeno", [{"role": "user", "content": "oi"}], 8) == ("oi do ollama falso", 7, 4), "chat")
            em.unload("pequeno")
            check(FakeOllama.deleted == ["sigma-pequeno"], "delete no Ollama")
            sent = dict(FakeOllama.blobs)
            try:
                em.load("lixo", junk, engines.sha256_file(junk))
                check(False, "o Ollama não deveria receber um arquivo que não é GGUF")
            except RefusedFile:
                check(FakeOllama.blobs == sent, "arquivo que não é GGUF nem é enviado ao Ollama")
            FakeOllama.version = "0.5.7"
            em = EngineManager(os.path.join(tmp, "data"), prefer="ollama", log=olog.append)
            olog.clear()
            ok = [em.available() for _ in range(3)]
            d = em.describe()
            check(ok == [False] * 3 and not d["ready"]
                  and "atualize o Ollama para hospedar modelos da rede" in d["detail"],
                  f"Ollama 0.5.7 recusado: {d['detail']}")
            warned = [m for m in olog if "atualize o Ollama" in m]
            check(len(warned) == 1 and "atualize o Ollama para hospedar modelos da rede" in warned[0],
                  f"avisa uma vez só no log: {warned}")
        finally:
            FakeOllama.version = "0.6.5"
            engines.OLLAMA_URL = real_ollama

        print("9) Ollama de verdade")
        if os.environ.get("SIGMA_TESTE_OLLAMA") != "1":
            print("   pulado (rode com SIGMA_TESTE_OLLAMA=1 para usar o Ollama desta máquina em 127.0.0.1:11434)")
        else:
            real_ollama_test(tmp)
        print("TUDO OK")
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def real_ollama_test(tmp):
    em = EngineManager(os.path.join(tmp, "data"), prefer="auto", log=print)
    if em.kind != "ollama":
        print("   Ollama não está no ar em 127.0.0.1:11434 (ou é antigo demais): pulado")
        return
    root = os.environ.get("OLLAMA_MODELS") or os.path.join(os.path.expanduser("~"), ".ollama", "models")
    manifest = os.path.join(root, "manifests", "registry.ollama.ai", "library", "qwen2.5-coder", "1.5b")
    if not os.path.exists(manifest):
        print("   qwen2.5-coder:1.5b não está no Ollama: pulado")
        return
    with open(manifest, encoding="utf-8") as f:
        layer = next(x for x in json.load(f)["layers"] if x["mediaType"].endswith(".model"))
    digest = layer["digest"].split(":", 1)[1]
    blob = os.path.join(root, "blobs", "sha256-" + digest)
    name = f"teste-{os.urandom(3).hex()}"
    t = time.time()
    em.load(name, blob, digest)   # passa pelo check_gguf: um GGUF de verdade tem de passar
    print(f"   carregado como sigma-{name} em {time.time() - t:.1f}s")
    t = time.time()
    text, tin, tout = em.chat(name, [{"role": "user", "content": "Responda só com a palavra: pronto"}], 16)
    print(f"   resposta: {text!r} ({tin}+{tout} tokens, {time.time() - t:.1f}s)")
    check(bool(text.strip()) and tout > 0, "o modelo respondeu")
    d = em.describe()
    check(d["ready"] and name in d["detail"], f"describe: {d['detail']}")
    em.unload(name)
    with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
        tags = [m["name"] for m in json.load(r)["models"]]
    check(f"sigma-{name}:latest" not in tags, "modelo temporário apagado do Ollama")
    check("qwen2.5-coder:1.5b" in tags and os.path.exists(blob), "o modelo original continua lá")


if __name__ == "__main__":
    main()
