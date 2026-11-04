"""Imagem e vídeo (regras v2, Sigma 2.5).

    python -m unittest tests.test_media -v

A) sigma.media sem rede: formato dos arquivos (safetensors/GGUF), o pedido que o motor roda (listas fechadas e
   limites; números infinitos, caracteres de controle e tags <lora:…> não passam), a linha de comando do sd-cli e
   o gerador de teste; o motor stable-diffusion.cpp com um sd-cli de mentira (um script Python): download que
   cai, Vulkan que não roda -> CPU, SIGMA_SD_CLI que sumiu, falhas que viram EngineError e prioridade baixa;
B) de ponta a ponta com as peças de verdade: nó produtor (HTTP na porta 8861), máquina (Host com o gerador de
   teste), cliente e um servidor de arquivos (porta 8862). O padrinho registra um modelo de imagem com VAE e um de
   vídeo; a rede atribui, a máquina baixa, confere e anuncia; o cliente pede 2 imagens e 2 s de vídeo, cifrados,
   e paga por unidade entregue. Nada sai da máquina: tudo em 127.0.0.1 e em pastas temporárias.
"""
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain, engines, media  # noqa: E402
from sigma import host as hostmod  # noqa: E402
from sigma import node as nodemod  # noqa: E402
from sigma.chain import UNIT, make_tx  # noqa: E402
from sigma.client import Client  # noqa: E402
from sigma.crypto import Wallet, is_sealed  # noqa: E402
from sigma.engines import EngineError, RefusedFile  # noqa: E402
from test_node import find_nonces  # noqa: E402

NODE_PORT, FILES_PORT = 8861, 8862


def safetensors(payload=b"\0" * 64, extra=None):
    header = {"w": {"dtype": "F32", "shape": [len(payload) // 4], "data_offsets": [0, len(payload)]}}
    header.update(extra or {})
    raw = json.dumps(header).encode()
    return struct.pack("<Q", len(raw)) + raw + payload


def gguf(name=None, offset=0, data=64, shape=(4,)):
    """GGUF pequeno e válido: um metadado e, com `name`, um tensor F32 com a forma `shape` (dados alinhados em
    32)."""
    def s(b):
        return struct.pack("<Q", len(b)) + b
    head = (b"GGUF" + struct.pack("<IQQ", 3, 1 if name else 0, 1) + s(b"general.name") + struct.pack("<I", 8)
            + s(b"teste"))
    if name:   # quantas dimensões, a forma, tipo F32 e offset
        head += s(name.encode()) + struct.pack(f"<I{len(shape)}QIQ", len(shape), *shape, 0, offset)
    return head + b"\0" * (-len(head) % 32 + data)


# sd-cli de mentira (um script Python que o teste põe na frente da linha de comando, com o executável da versão como
# 1º argumento): anota a versão, a pasta, o prompt e a própria prioridade em SIGMA_TESTE_SD e grava o PNG em -o,
# como o de verdade. A "versão Vulkan" sai com erro sem gravar nada (placa sem memória); o prompt "quebrado" falha
# em todas (modelo que não roda).
FAKE_SD = r'''
import json, os, struct, sys, time, zlib
exe, args = sys.argv[1], sys.argv[2:]
prompt = args[args.index("-p") + 1]
time.sleep(0.3)   # sem o nice, a prioridade baixa chega pelo os.setpriority logo depois do Popen
if os.name == "nt":
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    prio = k32.GetPriorityClass(k32.GetCurrentProcess())
else:
    prio = os.getpriority(os.PRIO_PROCESS, 0)
with open(os.environ["SIGMA_TESTE_SD"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"exe": exe, "prio": prio, "cwd": os.getcwd(), "prompt": prompt}) + "\n")
if "vulkan" in exe or "quebrado" in prompt:
    print("ggml_vulkan: ErrorOutOfDeviceMemory")
    sys.exit(1)
w, h = int(args[args.index("-W") + 1]), int(args[args.index("-H") + 1])
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
with open(args[args.index("-o") + 1], "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\x80\x40\x20" * w) * h)) + chunk(b"IEND", b""))
'''


class TestWeights(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sgm")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, data, name="m.safetensors"):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_good_files(self):
        self.assertEqual(media.check_weights(self.write(safetensors())), "safetensors")
        self.assertEqual(media.check_weights(self.write(b"GGUF" + struct.pack("<I", 3) + b"\0" * 32, "m.gguf")),
                         "gguf")

    def test_bad_files_refused(self):
        bad = [b"", b"oi", b"GGUF" + struct.pack("<I", 99), struct.pack("<Q", 10 ** 12) + b"{}",
               struct.pack("<Q", 5) + b"[1,2]", safetensors(extra={"x": {"data_offsets": [0, 10 ** 9]}}),
               struct.pack("<Q", 4) + b"\xff\xfe{}", b"<html>nao e modelo</html>"]
        for data in bad:
            with self.assertRaises(RefusedFile, msg=repr(data[:20])) as cm:
                media.check_weights(self.write(data))
            self.assertEqual(cm.exception.reason, "weights")

    def test_gguf_structure_is_checked(self):
        """O GGUF de imagem/vídeo passa pela mesma conferência do texto (check_gguf), com os limites do sd-cli
        fixado (nome de tensor de até 159 bytes e de 0 a 5 dimensões): um cabeçalho feito para estourar o leitor do
        ggml não chega ao sd-cli. Antes, magic + versão bastavam."""
        longo = self.write(gguf("t" * 100), "longo.gguf")
        self.assertEqual(media.check_weights(longo), "gguf")
        with self.assertRaises(RefusedFile):   # o texto continua com o limite do llama.cpp (63 bytes)
            engines.check_gguf(longo)
        bad = {"2^63 tensores e metadados": b"GGUF" + struct.pack("<IQQ", 3, 2 ** 63, 2 ** 63),
               "cabeçalho cortado (5 bytes)": b"GGUF\x03",
               "metadado declarado e ausente": b"GGUF" + struct.pack("<IQQ", 3, 0, 1),
               "nome de tensor com 160 bytes": gguf("t" * 160),
               "tensor com 6 dimensões": gguf("t", shape=(1,) * 6),
               "dimensão de tamanho zero": gguf("t", shape=(4, 0)),
               "tensor depois do fim do arquivo": gguf("t", offset=4096)}
        for what, data in bad.items():
            with self.assertRaises(RefusedFile, msg=what) as cm:
                media.check_weights(self.write(data, "ruim.gguf"))
            self.assertEqual(cm.exception.reason, "weights")

    def test_gguf_that_the_sd_cli_reads_is_accepted(self):
        """O que o sd-cli fixado lê não é recusado: nome de tensor de 128 a 159 bytes (o ggml dele vem com
        GGML_MAX_NAME=160, e não 128), tensor de 5 dimensões (o patch_embedding.weight dos GGUF do Wan; o leitor
        próprio do sd.cpp junta na 4ª as dimensões a mais) e tensor sem dimensão (um escalar para o ggml). Antes,
        os três viravam "GGUF malformado" e toda máquina recusava o modelo. O texto continua com os limites do
        llama.cpp."""
        ok = {"nome de tensor com 128 bytes": gguf("t" * 128),
              "nome de tensor com 159 bytes": gguf("t" * 159),
              "tensor de 5 dimensões (Wan)": gguf("patch_embedding.weight", shape=(2, 2, 1, 16, 8), data=2048),
              "tensor sem dimensão": gguf("t", shape=())}
        for what, data in ok.items():
            path = self.write(data, "bom.gguf")
            self.assertEqual(media.check_weights(path), "gguf", what)
            with self.assertRaises(RefusedFile, msg=what):
                engines.check_gguf(path)

    def test_nested_safetensors_header_refused(self):
        """Cabeçalho JSON aninhado demais (RecursionError no json.loads) é um arquivo recusado, e não um erro que
        escapa do host e trava o laço dos modelos."""
        raw = b"[" * 100_000
        with self.assertRaises(RefusedFile) as cm:
            media.check_weights(self.write(struct.pack("<Q", len(raw)) + raw + b"\0" * 8))
        self.assertEqual(cm.exception.reason, "weights")

    def test_weight_ext(self):
        self.assertEqual(media.weight_ext("https://hf.co/a/b/resolve/main/x.SafeTensors?download=1"), ".safetensors")
        self.assertEqual(media.weight_ext("https://hf.co/a/b/resolve/main/x.gguf"), ".gguf")
        for url in ("https://hf.co/x.ckpt", "https://hf.co/x.bin", "", None):
            self.assertIsNone(media.weight_ext(url))


class TestSpec(unittest.TestCase):
    def test_image_spec_is_closed(self):
        s = media.clean_spec("image", {"prompt": "  um gato  ", "size": "4096x4096", "count": 9, "steps": 999,
                                       "cfg": "x", "sampler": "rm -rf", "seed": -5, "negative": 7}, units=2)
        self.assertEqual((s["prompt"], s["width"], s["height"], s["count"]), ("um gato", 512, 512, 2))
        self.assertEqual((s["steps"], s["cfg"], s["sampler"], s["seed"], s["negative"]),
                         (50, 7.0, "euler_a", 0, ""))
        self.assertEqual(media.clean_spec("image", {"prompt": "x" * 5000}, 1)["prompt"], "x" * media.MAX_PROMPT)
        for bad in ({}, {"prompt": ""}, {"prompt": 3}, "texto", None):
            with self.assertRaises(ValueError):
                media.clean_spec("image", bad, 1)

    def test_video_spec(self):
        s = media.clean_spec("video", {"prompt": "mar", "size": "480x832", "seconds": 9}, units=3)
        self.assertEqual((s["width"], s["height"], s["seconds"], s["frames"], s["flow_shift"]), (480, 832, 3, 49, 3.0))
        self.assertEqual(media.clean_spec("video", {"prompt": "mar"}, units=2)["seconds"], 2)

    def test_non_finite_numbers_use_the_defaults(self):
        """Infinity, -Infinity, NaN e 1e999 (o json.loads aceita) e inteiros enormes caem no padrão: int(inf) e
        float(10**400) davam OverflowError, que derrubava o _prepare do host depois do claim (o job ficava sem
        resposta e a máquina levava falta)."""
        img = json.loads('{"prompt": "x", "seed": Infinity, "steps": 1e999, "count": -Infinity, "cfg": NaN}')
        s = media.clean_spec("image", img, units=2)
        self.assertEqual((s["steps"], s["count"], s["cfg"]), (20, 1, 7.0))
        self.assertTrue(0 <= s["seed"] <= 2 ** 31 - 1)
        s = media.clean_spec("image", {"prompt": "x", "cfg": 10 ** 400, "steps": -10 ** 400, "seed": 10 ** 400}, 1)
        self.assertEqual((s["cfg"], s["steps"], s["seed"]), (7.0, 1, 2 ** 31 - 1))
        self.assertEqual(media.clean_spec("image", {"prompt": "x", "cfg": float("-inf")}, 1)["cfg"], 7.0)
        vid = json.loads('{"prompt": "x", "seconds": Infinity, "flow_shift": -Infinity, "steps": -1e999}')
        v = media.clean_spec("video", vid, units=3)
        self.assertEqual((v["seconds"], v["frames"], v["flow_shift"], v["steps"]), (3, 49, 3.0, 20))

    def test_control_characters_never_reach_the_engine(self):
        """O NUL no argv faz o Popen levantar ValueError (o job ficava sem resposta): ele, os outros caracteres
        de controle e os surrogates soltos saem do prompt e do negative; quebra de linha e tab ficam. Texto que só
        tinha isso é um pedido sem descrição (falha "unreadable" na hora, sem falta para a máquina)."""
        s = media.clean_spec("image", {"prompt": " um gato\x00 laranja\x07\r\nno sofá\t",
                                       "negative": "feio\x00\x1b[0m\ud800"}, 1)
        self.assertEqual((s["prompt"], s["negative"]), ("um gato laranja\nno sofá", "feio[0m"))
        for bad in ("\x00", "\x00\x01\x1f\x7f\x9f", "\ud800 \x00"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                media.clean_spec("image", {"prompt": bad}, 1)

    def test_lora_tags_never_reach_the_engine(self):
        """O sd-cli lê <lora:caminho:peso> no prompt e abre o arquivo (relativo à pasta do pedido ou absoluto):
        quem pede escolheria arquivos do disco da máquina para o motor abrir. < e > saem do texto, então nenhuma
        tag sobra, nem aninhada."""
        tag = re.compile(r"<lora:([^:>]+):([^>]+)>")   # o que o sd-cli procura no texto
        with mock.patch.object(media, "media_variants", return_value=[]):
            eng = media._SdCpp(tempfile.gettempdir(), print, 4)
        for text in ("um gato <lora:../../models/" + "ab" * 32 + ":1>", "<lora:C:\\dados\\models\\x:0.8> um gato",
                     "um gato <lo<lora:a:1>ra:b:1>", "um gato <lora:x:abc>", "um gato <<lora:x:1>>"):
            s = media.clean_spec("image", {"prompt": text, "negative": text}, 1)
            cmd = eng._command("sd-cli", {"files": {"main": "m.gguf"}}, s, "out.png")
            for arg in (cmd[cmd.index("-p") + 1], cmd[cmd.index("-n") + 1]):
                self.assertIsNone(tag.search(arg), arg)
                self.assertFalse({"<", ">"} & set(arg), arg)
        self.assertEqual(media.clean_spec("image", {"prompt": "um gato <lora:x:1> no sofá"}, 1)["prompt"],
                         "um gato lora:x:1 no sofá")

    def test_command_uses_only_known_flags(self):
        eng = media._SdCpp(tempfile.gettempdir(), print, 4)
        img = {"main": "m.gguf", "vae": "ae.safetensors", "llm": "qwen.gguf"}
        spec = media.clean_spec("image", {"prompt": "-o /etc/passwd", "count": 2, "seed": 7}, units=2)
        cmd = eng._command("sd-cli", {"files": img}, spec, "out.png")
        self.assertEqual(cmd[:7], ["sd-cli", "--diffusion-model", "m.gguf", "--vae", "ae.safetensors", "--llm",
                                   "qwen.gguf"])
        self.assertEqual(cmd[cmd.index("-p") + 1], "-o /etc/passwd")      # o prompt é um argumento só
        self.assertEqual(cmd[cmd.index("-b") + 1], "2")
        self.assertNotIn("vid_gen", cmd)
        single = eng._command("sd-cli", {"files": {"main": "sd15.safetensors"}},
                              media.clean_spec("image", {"prompt": "x"}, 1), "out.png")
        self.assertEqual(single[1:3], ["-m", "sd15.safetensors"])
        self.assertNotIn("-b", single)
        vid = eng._command("sd-cli", {"files": {"main": "wan.safetensors", "t5xxl": "umt5.gguf"}},
                           media.clean_spec("video", {"prompt": "x", "seconds": 2}, 2), "out.webm")
        self.assertEqual(vid[vid.index("-M") + 1], "vid_gen")
        self.assertEqual(vid[vid.index("--video-frames") + 1], "33")

    def test_mock_generates(self):
        m = media.MediaManager(tempfile.gettempdir(), "mock")
        p = os.path.join(tempfile.mkdtemp(prefix="sgm"), "m.safetensors")
        with open(p, "wb") as f:
            f.write(safetensors())
        m.load("img", "image", {"main": p})
        out = m.generate("img", media.clean_spec("image", {"prompt": "x", "count": 3, "size": "768x768"}, 3))
        self.assertEqual([(mime, meta) for _, mime, meta in out], [("image/png", {"width": 96, "height": 96})] * 3)
        self.assertTrue(all(media.png_size(d) == (96, 96) for d, _, _ in out))
        m.load("vid", "video", {"main": p})
        (data, mime, meta), = m.generate("vid", media.clean_spec("video", {"prompt": "x", "seconds": 2}, 2))
        self.assertEqual((mime, meta["seconds"], data[:4]), ("video/webm", 2, media.WEBM_MAGIC))

    def test_sd_variants(self):
        with mock.patch("platform.system", return_value="Windows"), mock.patch("platform.machine",
                                                                               return_value="AMD64"):
            self.assertEqual(media.media_variants(gpu_mb=0), ["win-cpu-x64"])
            self.assertEqual(media.media_variants(gpu_mb=4000), ["win-vulkan-x64", "win-cpu-x64"])
        self.assertTrue(all(len(sha) == 64 for _, sha in media.SD_ASSETS.values()))


class TestSdCpp(unittest.TestCase):
    """O motor stable-diffusion.cpp sem rede e sem o sd-cli de verdade (FAKE_SD roda no lugar dele)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sgsd")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.logs = []
        self.report = os.path.join(self.dir, "sd-cli.jsonl")
        self.fake = os.path.join(self.dir, "fake_sd.py")
        with open(self.fake, "w", encoding="utf-8") as f:
            f.write(FAKE_SD)

    def engine(self, variants=("teste-cpu",), override=None):
        with mock.patch.dict(os.environ), mock.patch.object(media, "media_variants", return_value=list(variants)):
            os.environ.pop("SIGMA_SD_CLI", None)
            if override:
                os.environ["SIGMA_SD_CLI"] = override
            return media._SdCpp(os.path.join(self.dir, "dados"), self.logs.append, 2)

    @staticmethod
    def install_fake(eng, variant):
        """Uma versão já instalada (a pasta com o .ok e o sd-cli), sem baixar nada."""
        folder = os.path.join(eng.root, variant)
        os.makedirs(folder)
        open(os.path.join(folder, "sd-cli.exe" if os.name == "nt" else "sd-cli"), "wb").close()
        with open(os.path.join(folder, ".ok"), "w", encoding="utf-8") as f:
            f.write("teste")

    def use_fake(self, eng):
        orig = eng._command
        eng._command = lambda exe, m, spec, out: [sys.executable, self.fake, exe] + orig(exe, m, spec, out)[1:]

    def runs(self):
        if not os.path.exists(self.report):
            return []
        with open(self.report, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_download_that_fails_does_not_turn_media_off(self):
        """A conexão que cai ao baixar o stable-diffusion.cpp não tira a versão de CPU da lista: available()
        continua True (a máquina segue anunciando imagem e vídeo e a chain não tira os modelos dela) e a próxima
        tentativa do host baixa de novo. Antes, ela ia para `failed` e a mídia só voltava reiniciando o app."""
        pkg = os.path.join(self.dir, "sd-teste.zip")
        with zipfile.ZipFile(pkg, "w") as z:
            z.writestr("sd-teste/" + ("sd-cli.exe" if os.name == "nt" else "sd-cli"), b"sd-cli de mentira")
        with open(pkg, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        urls = []

        def fetch(url, dest, sha256, **kw):   # o GitHub: a 1ª conexão cai (e a Vulkan nunca baixa)
            urls.append(url)
            if len(urls) == 1 or "vulkan" in url:
                raise EngineError("a conexão caiu no meio do download (timed out); continua de onde parou na "
                                  "próxima tentativa")
            shutil.copy(pkg, dest)
            return dest

        assets = {"teste-cpu": ("sd-teste-cpu.zip", digest), "teste-vulkan": ("sd-teste-vulkan.zip", digest)}
        with mock.patch.dict(media.SD_ASSETS, assets), mock.patch.object(media, "fetch", fetch):
            eng = self.engine(["teste-cpu"])
            with self.assertRaises(EngineError):
                eng.load("img", "image", {"main": "m.safetensors"})
            self.assertEqual((eng.failed, eng.available()), (set(), True))
            self.assertNotIn("não há versão", eng.describe()["detail"])
            eng.load("img", "image", {"main": "m.safetensors"})   # o host tenta de novo (retry_at)
            self.assertEqual((len(urls), eng.loaded()), (2, ["img"]))
            self.assertTrue(eng._installed("teste-cpu"))
            gpu = self.engine(["teste-vulkan", "teste-cpu"])       # com placa: a Vulkan que não instala sai
            gpu.load("img", "image", {"main": "m.safetensors"})
            self.assertEqual((gpu.failed, gpu.loaded()), ({"teste-vulkan"}, ["img"]))

    def test_vulkan_that_does_not_run_falls_back_to_cpu(self):
        """O sd-cli Vulkan que sai com erro sem gravar nada (ex.: placa com pouca memória): o mesmo pedido roda na
        de CPU e, como ela gerou, a Vulkan sai da lista; o próximo pedido já vai direto para a CPU. Antes, todo
        pedido usava a Vulkan e falhava. O sd-cli roda com prioridade baixa e numa pasta que sai depois."""
        eng = self.engine(["teste-vulkan", "teste-cpu"])
        for v in eng.variants:
            self.install_fake(eng, v)
        self.use_fake(eng)
        eng.load("img", "image", {"main": "m.safetensors"})
        spec = media.clean_spec("image", {"prompt": "um gato laranja"}, 1)
        with mock.patch.dict(os.environ, {"SIGMA_TESTE_SD": self.report}):
            with self.assertRaises(EngineError):   # menos de 60 s sobrando: falha sem tentar a CPU
                eng.generate("img", spec, timeout=30)
            self.assertEqual((len(self.runs()), eng.failed), (1, set()))
            out = eng.generate("img", spec, timeout=120)
            self.assertEqual([(mime, meta) for _, mime, meta in out], [("image/png", {"width": 512, "height": 512})])
            self.assertEqual(eng.failed, {"teste-vulkan"})
            self.assertTrue(any("Vulkan" in x and "usando a de CPU" in x for x in self.logs), self.logs)
            eng.generate("img", spec, timeout=120)
        runs = self.runs()
        self.assertEqual([os.path.basename(os.path.dirname(r["exe"])) for r in runs],
                         ["teste-vulkan", "teste-vulkan", "teste-cpu", "teste-cpu"])
        for r in runs:   # BELOW_NORMAL no Windows; nice 10 nos outros
            self.assertTrue(r["prio"] == 0x4000 if os.name == "nt" else r["prio"] >= 10, r["prio"])
            self.assertEqual(os.path.realpath(os.path.dirname(r["cwd"])), os.path.realpath(eng.work))
        self.assertEqual(os.listdir(eng.work), [])   # nada do pedido fica no disco

    def test_model_that_fails_everywhere_keeps_vulkan(self):
        """Se a de CPU também falha, o problema é o modelo ou o pedido, não a placa: a Vulkan continua."""
        eng = self.engine(["teste-vulkan", "teste-cpu"])
        for v in eng.variants:
            self.install_fake(eng, v)
        self.use_fake(eng)
        eng.load("img", "image", {"main": "m.safetensors"})
        with mock.patch.dict(os.environ, {"SIGMA_TESTE_SD": self.report}), self.assertRaises(EngineError) as cm:
            eng.generate("img", media.clean_spec("image", {"prompt": "modelo quebrado"}, 1), timeout=120)
        self.assertIn("não gerou nada", str(cm.exception))
        self.assertEqual((len(self.runs()), eng.failed), (2, set()))

    def test_sd_cli_override_that_is_not_a_file(self):
        """SIGMA_SD_CLI apontando para um arquivo que sumiu: EngineError (o host mostra o erro e tenta de novo,
        e os outros modelos continuam carregando). Antes, KeyError 'próprio' parava o laço dos modelos."""
        exe = os.path.join(self.dir, "meu-sd", "sd-cli.exe")
        eng = self.engine(override=exe)
        for _ in range(2):
            with self.assertRaises(EngineError) as cm:
                eng.load("img", "image", {"main": "m.safetensors"})
            self.assertIn("SIGMA_SD_CLI", str(cm.exception))
        self.assertTrue(eng.available())
        os.makedirs(os.path.dirname(exe))
        open(exe, "wb").close()   # o arquivo voltou: a próxima tentativa carrega
        eng.load("img", "image", {"main": "m.safetensors"})
        self.assertEqual(eng.loaded(), ["img"])

    def test_nul_in_the_prompt_is_an_engine_error(self):
        """O host só trata EngineError: o ValueError do Popen (NUL no argv) deixava o job reivindicado sem
        resposta (falta para a máquina). O NUL de um pedido nem chega ao Popen (o clean_spec tira), e um que
        passasse vira EngineError."""
        eng = self.engine(override=sys.executable)   # um executável que existe
        eng.load("img", "image", {"main": "m.safetensors"})
        spec = media.clean_spec("image", {"prompt": "um gato\x00 laranja"}, 1)
        self.use_fake(eng)
        with mock.patch.dict(os.environ, {"SIGMA_TESTE_SD": self.report}):
            (data, mime, meta), = eng.generate("img", spec, timeout=60)
        self.assertEqual((mime, [r["prompt"] for r in self.runs()]), ("image/png", ["um gato laranja"]))
        with self.assertRaises(EngineError) as cm:
            eng.generate("img", dict(spec, prompt="um gato\x00"), timeout=30)
        self.assertIn("sd-cli", str(cm.exception))
        self.assertEqual(os.listdir(eng.work), [])

    def test_kill_that_does_not_finish_is_an_engine_error(self):
        """O wait(10) depois do kill que estoura o tempo (TimeoutExpired) também sai como EngineError."""
        eng = self.engine(override=sys.executable)
        eng.load("img", "image", {"main": "m.safetensors"})

        class Stuck:   # sd-cli que nem o kill derruba a tempo (pid inexistente: o setpriority não mexe em nada)
            pid, returncode = 99_999_999, None

            def __init__(self, cmd, **kw):
                pass

            def poll(self):
                return None

            def kill(self):
                pass

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("sd-cli", timeout)

        with mock.patch.object(media.subprocess, "Popen", Stuck), self.assertRaises(EngineError) as cm:
            eng.generate("img", media.clean_spec("image", {"prompt": "um gato"}, 1), timeout=30,
                         should_stop=lambda: True)
        self.assertIn("interrompida", str(cm.exception))
        self.assertEqual(os.listdir(eng.work), [])

    def test_low_priority_outside_windows(self):
        """Linux e macOS: `nice -n 10` na frente do sd-cli ou, sem o nice, os.setpriority logo depois do Popen,
        como o llama-server (no Windows a prioridade baixa vem pelo creationflags). Antes, prioridade normal."""
        eng = self.engine(override=sys.executable)
        eng.load("img", "image", {"main": "m.safetensors"})
        spec = media.clean_spec("image", {"prompt": "um gato"}, 1)
        calls, prio = [], []

        class Proc:   # Popen de mentira: anota o comando e grava o PNG em -o, como o sd-cli
            pid, returncode = 4242, 0

            def __init__(self, cmd, **kw):
                calls.append((cmd, kw))
                with open(cmd[cmd.index("-o") + 1], "wb") as f:
                    f.write(media.solid_png(8, 8, b"\1\2\3"))

            def poll(self):
                return 0

        class PosixOS:   # o `os` do sigma.media como no Linux
            name, PRIO_PROCESS = "posix", 0

            def setpriority(self, which, who, value):
                prio.append((which, who, value))

            def __getattr__(self, attr):
                return getattr(os, attr)

        for nice in ("/usr/bin/nice", None):
            with mock.patch.object(media, "os", PosixOS()), mock.patch.object(media.subprocess, "Popen", Proc), \
                    mock.patch.object(media.shutil, "which", lambda name, nice=nice: nice if name == "nice" else None):
                eng.generate("img", spec, timeout=30)
        (with_nice, _), (without, _) = calls
        self.assertEqual(with_nice[:4], ["/usr/bin/nice", "-n", "10", sys.executable])
        self.assertEqual((without[0], prio), (sys.executable, [(0, 4242, 10)]))   # setpriority só sem o nice
        if os.name == "nt":   # no Windows de verdade: BELOW_NORMAL_PRIORITY_CLASS pelo creationflags, sem nice
            with mock.patch.object(media.subprocess, "Popen", Proc):
                eng.generate("img", spec, timeout=30)
            self.assertEqual(calls[-1][0][0], sys.executable)
            self.assertEqual(calls[-1][1]["creationflags"] & 0x4000, 0x4000)


class Files(BaseHTTPRequestHandler):
    files = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = self.files.get(self.path.strip("/"))
        self.send_response(200 if data is not None else 404)
        self.send_header("Content-Length", str(len(data or b"")))
        self.end_headers()
        self.wfile.write(data or b"")


def wait(cond, what, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"esperei demais: {what}")


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sgme")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.producer = Wallet.generate()
        params = {"epoch_seconds": 30, "window_seconds": 3, "min_share_bits": 1, "max_shares": 32,
                  "ram_per_mb_window": 0, "demand_max_bps": 0}
        self.genesis = {"network": "sigma", "chain_id": 51515, "time": time.time(), "validators": [self.producer.pub],
                        "alloc": {}, "params": dict(chain.DEFAULT_PARAMS, **params), "rules": 2}
        self.node = nodemod.Node(os.path.join(self.tmp, "no"), self.genesis, self.producer, heartbeat=0.3)
        self.addCleanup(self.node.forum.db.close)
        self.srv = ThreadingHTTPServer(("127.0.0.1", NODE_PORT), nodemod.make_handler(self.node))
        self.files = ThreadingHTTPServer(("127.0.0.1", FILES_PORT), Files)
        for s in (self.srv, self.files):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
        threading.Thread(target=self.node.loop, daemon=True).start()
        self.url = f"http://127.0.0.1:{NODE_PORT}"

    def mine(self, address):
        """O único jeito de ter SIG: minerar (shares de 1 bit), esperar a janela virar base e a emissão pagar."""
        st = self.node.pending
        w = st.mining["window"]
        nonces = find_nonces(st.mining["seeds"][str(w)], address, 1, 4)
        self.node.submit_tx({"type": "work", "data": {"miner": address, "window": w, "bits": 1, "nonces": nonces}})
        wait(lambda: self.node.state.bal(address) > 100 * UNIT, "a mineração pagar", timeout=60)

    def file(self, name, data):
        Files.files[name] = data
        return {"url": f"http://127.0.0.1:{FILES_PORT}/{name}", "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data)}

    def test_image_and_video_through_the_network(self):
        sponsor, client = Wallet.generate(), Wallet.generate()
        self.mine(sponsor.address)
        self.mine(client.address)
        c_sponsor, c_client = Client(self.url, sponsor), Client(self.url, client)
        main, vae = self.file("img.safetensors", safetensors(b"\1" * 64)), self.file("ae.safetensors", safetensors())
        c_sponsor.register_model("img-teste", main["url"], main["sha256"], main["size"], 20, replicas=1,
                                 license="Apache-2.0", task="image", files=[dict(vae, role="vae")])
        wan = self.file("wan.safetensors", safetensors(b"\2" * 64))
        c_sponsor.register_model("vid-teste", wan["url"], wan["sha256"], wan["size"], 20, replicas=1,
                                 license="Apache-2.0", task="video")
        payout = Wallet.generate()
        machine = Wallet.generate()
        logs = []
        with mock.patch.object(hostmod.hardware, "detect", return_value={
                "cpu_cores": 4, "ram_mb": 16000, "vram_mb": 0, "gpu": "", "disk_free_mb": 50_000}):
            h = hostmod.Host(self.url, machine, payout.address, os.path.join(self.tmp, "maquina"),
                             {"engine": "mock", "ai_image": True, "ai_video": True, "ai_ram_gb": 8, "disk_gb": 5},
                             log=logs.append)
            h.start()
            self.addCleanup(h.stop)
            wait(lambda: machine.address in self.node.state.machines
                 and self.node.state.machines[machine.address].get("tasks") == ["text", "image", "video"],
                 "a máquina se registrar com imagem e vídeo")
            for name in ("img-teste", "vid-teste"):
                wait(lambda: machine.address in (self.node.state.models[name]["pinners"]),
                     f"a máquina hospedar {name}", timeout=120)
            price = self.node.state.machines[machine.address]["prices"]["ai_per_image"]
            before = self.node.state.bal(client.address)
            items, job = c_client.generate("img-teste", "image", {"prompt": "um gato laranja", "count": 2,
                                                                  "size": "512x512"}, timeout=120)
            self.assertEqual([(i["mime"], media.png_size(i["data"])) for i in items], [("image/png", (64, 64))] * 2)
            self.assertEqual((job["status"], job["cost"], job["machine"]), ("done", 2 * price, machine.address))
            self.assertTrue(is_sealed(self.node.get_blob(job["result_hash"])))     # o nó só guardou bytes cifrados
            wait(lambda: self.node.state.bal(client.address) == before - 2 * price, "o troco voltar")
            vprice = self.node.state.machines[machine.address]["prices"]["ai_per_video_second"]
            items, job = c_client.generate("vid-teste", "video", {"prompt": "ondas", "seconds": 2}, timeout=120)
            self.assertEqual((items[0]["mime"], items[0]["seconds"], items[0]["data"][:4]),
                             ("video/webm", 2, media.WEBM_MAGIC))
            self.assertEqual(job["cost"], 2 * vprice)
            self.assertFalse(any("um gato laranja" in str(x) for x in logs))   # o pedido não vai para o registro


if __name__ == "__main__":
    unittest.main()
