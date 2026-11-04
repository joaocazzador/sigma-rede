"""Empacotamento (SPEC-v2.1 §15): o sigma.pyz leva o netsafe, as páginas, o painel e o ícone da janelinha; o
Sigma.exe leva o .ico e o .png para o Tk usar iconbitmap/iconphoto. O PyInstaller nunca roda aqui: o comando
dele é conferido, não executado.

    python -m unittest tests.test_packaging -v
"""
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "packaging"))

import build_pyz  # noqa: E402
import build_windows  # noqa: E402


class PyzTest(unittest.TestCase):
    def test_pyz_tem_o_essencial(self):
        tmp = tempfile.mkdtemp(prefix="sigma-pyz-t-")
        try:
            out = build_pyz.build(tmp)
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                png = z.read("sigma/sigma.png")
                main = z.read("__main__.py").decode()
            for p in build_pyz.ESSENTIAL + ("sigma/web/og.png", "sigma/rpc.py", "sigma/forum.py"):
                self.assertIn(p, names)
            self.assertIn("sigma/netsafe.py", build_pyz.ESSENTIAL)
            self.assertEqual([n for n in names if "__pycache__" in n or n.endswith(".pyc")], [])
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")              # o Σ que o Tk mostra no Linux e no macOS
            self.assertIn("sys.version_info < (3, 11)", main)
            digest = build_pyz.update_sums(tmp, out)
            with open(os.path.join(tmp, build_pyz.SUMS), encoding="utf-8") as f:
                self.assertEqual(f.read(), f"{digest}  sigma.pyz\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(os.name == "nt", "o Sigma.exe só é gerado no Windows")
class WindowsTest(unittest.TestCase):
    def test_icone_vai_dentro_do_exe(self):
        tmp = tempfile.mkdtemp(prefix="sigma-exe-t-")
        seen = {}

        def fake_run(cmd, cwd=None):   # no lugar do PyInstaller: só guarda o comando
            seen["cmd"] = cmd
            return mock.Mock(returncode=1)
        try:
            argv = ["build_windows.py", "--dist", os.path.join(tmp, "dist"), "--build", os.path.join(tmp, "build")]
            with mock.patch.object(build_windows.importlib.util, "find_spec", return_value=object()), \
                    mock.patch.object(build_windows.subprocess, "run", fake_run), \
                    mock.patch.object(build_windows.make_icon, "write_icon", lambda ico, png=None: ico), \
                    mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
                self.assertEqual(build_windows.main(), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        cmd = seen["cmd"]
        datas = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-data"]
        here = os.path.join(ROOT, "packaging")
        self.assertIn(f"{os.path.join(here, 'sigma.ico')}{os.pathsep}sigma", datas)
        self.assertIn(f"{os.path.join(here, 'sigma.png')}{os.pathsep}sigma", datas)
        self.assertIn(f"{os.path.join(ROOT, 'sigma', 'web')}{os.pathsep}sigma/web", datas)
        self.assertIn("sigma.netsafe", cmd)                                     # hidden-import de todo o pacote
        self.assertEqual(cmd[cmd.index("--icon") + 1], os.path.join(here, "sigma.ico"))


if __name__ == "__main__":
    unittest.main()
