"""Grupo de vídeo (sigma/grupo.py + sigma/media.py): o tipo do grupo, as pontes só para o motor certo e o sd-cli
dividindo o modelo de difusão (--rpc-servers + --backend diffusion=RPC0&…&CPU). O teste com os programas de
verdade fica em pesquisa/grupo/e2e_video.py."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sigma import grupo, media  # noqa: E402

TMP = os.path.join(os.path.dirname(__file__), "_tmp_grupo_video")

VIDEO = {"task": "video", "prompt": "um gato", "negative": "", "width": 480, "height": 272, "steps": 4,
         "cfg": 6.0, "sampler": "euler", "seed": 1, "frames": 17, "flow_shift": 3.0, "seconds": 1}


class FakeGroup:
    def __init__(self, kind):
        self.kind = kind

    def rpc(self, kind=None):
        if kind and kind != self.kind:
            return []
        return ["127.0.0.1:5", "127.0.0.1:6"]


class TestKind(unittest.TestCase):
    def test_parse_kind(self):
        self.assertEqual(grupo.parse_kind(None), "text")
        self.assertEqual(grupo.parse_kind("VIDEO"), "video")
        with self.assertRaises(grupo.GroupError):
            grupo.parse_kind("imagem")

    def test_bridges_only_for_the_right_engine(self):
        g = FakeGroup("video")
        self.assertEqual(g.rpc("text"), [])
        self.assertEqual(len(g.rpc("video")), 2)

    def test_sd_rpc_missing(self):
        os.environ.pop("SIGMA_SD_RPC", None)
        with mock.patch.object(media, "install_sd_rpc", side_effect=media.EngineError("sem rede")),                 self.assertRaises(grupo.GroupError):
            grupo.sd_rpc_exe(TMP)

    def test_only_windows_has_the_package(self):
        with mock.patch.object(media.os, "name", "posix"), self.assertRaisesRegex(media.EngineError, "Windows"):
            media.install_sd_rpc(os.path.join(TMP, "nada"))


class TestSplitCommand(unittest.TestCase):
    def setUp(self):
        self.eng = media._SdCpp(TMP, lambda *a: None, 2)

    def test_group_args(self):
        self.eng.rpc_devices = lambda exe, rpc: ["RPC0", "RPC1", "RPC2"]   # o 2º servidor tem duas placas
        args = self.eng.group_args("sd-cli", ["127.0.0.1:5", "127.0.0.1:6"])
        self.assertEqual(args[args.index("--rpc-servers") + 1], "127.0.0.1:5,127.0.0.1:6")
        self.assertEqual(args[args.index("--backend") + 1], "diffusion=RPC0&RPC1&RPC2&CPU,te=CPU,vae=CPU")

    def test_split_replaces_offload(self):
        m = {"files": {"main": "wan.gguf", "t5xxl": "umt5.gguf", "vae": "vae.safetensors"}}
        group = ["--rpc-servers", "127.0.0.1:5", "--backend", "diffusion=RPC0&CPU,te=CPU,vae=CPU"]
        cmd = self.eng._command("sd-cli", m, VIDEO, "v.webm", group)
        self.assertNotIn("--offload-to-cpu", cmd)
        self.assertEqual(cmd[-2:], ["-o", "v.webm"])
        self.assertIn("--rpc-servers", cmd)
        self.assertIn("--offload-to-cpu", self.eng._command("sd-cli", m, VIDEO, "v.webm"))

    def test_parse_devices(self):
        import subprocess
        orig = subprocess.run

        class R:
            stdout = b"CPU     AMD Ryzen\nRPC0    127.0.0.1:5\nRPC1    127.0.0.1:6\n"
        subprocess.run = lambda *a, **k: R()
        try:
            self.assertEqual(media._SdCpp.rpc_devices("sd-cli", ["x"]), ["RPC0", "RPC1"])
            R.stdout = b"CPU only\n"
            with self.assertRaises(media.EngineError):
                media._SdCpp.rpc_devices("sd-cli", ["x"])
        finally:
            subprocess.run = orig

    def test_video_with_group_needs_the_rpc_cli(self):
        os.environ.pop("SIGMA_SD_CLI_RPC", None)
        self.eng.rpc_provider = lambda: ["127.0.0.1:5"]
        self.eng.models["wan"] = {"task": "video", "files": {"main": "wan.gguf"}}
        with mock.patch.object(media, "install_sd_rpc", return_value=os.path.join(TMP, "vazio")),                 self.assertRaises(media.EngineError) as e:
            self.eng.generate("wan", VIDEO, timeout=5)
        self.assertIn("sd-cli com RPC", str(e.exception))


if __name__ == "__main__":
    unittest.main()
