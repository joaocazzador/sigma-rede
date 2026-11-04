"""Gera dist/Sigma.exe: o app para Windows num executável só (PyInstaller --onefile --windowed).

    python packaging/build_windows.py

Precisa do PyInstaller, que este script nunca instala sozinho. O ícone Σ é desenhado na hora por
make_icon.py: vira o ícone do .exe e vai também dentro dele (sigma/sigma.ico e sigma/sigma.png), para a
janelinha de controle do Tk mostrar o Σ em vez da pena. O SHA-256 do .exe entra em dist/SHA256SUMS.txt (o site
mostra para conferir o download).
"""
import argparse
import glob
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import make_icon  # noqa: E402
from build_pyz import SUMS, update_sums  # noqa: E402


def hidden_imports():
    """Todos os módulos do pacote: o app importa alguns só na hora de usar (host, miner, engines...)."""
    pkg = os.path.join(ROOT, "sigma")
    return sorted("sigma." + f[:-3] for f in os.listdir(pkg) if f.endswith(".py") and f != "__init__.py")


def tcl_tk_data(dest):
    """O Python 3.14+ do Windows traz o Tcl/Tk 9 com as bibliotecas dentro de zips (tcl/libtcl9.*.zip e
    libtk9.*.zip), que o PyInstaller não coleta. Sem elas o .exe nem abre: o gancho do tkinter exige as pastas
    _tcl_data e _tk_data. Extrai as duas em `dest` e devolve as opções --add-data ([] no Tcl 8: o PyInstaller
    já cuida)."""
    root, opts = os.path.join(sys.base_prefix, "tcl"), []
    for pattern, top, name in (("libtcl*.zip", "tcl_library/", "_tcl_data"), ("libtk*.zip", "tk_library/", "_tk_data")):
        found = sorted(glob.glob(os.path.join(root, pattern)))
        if not found:
            return []
        target = os.path.join(dest, name)
        with zipfile.ZipFile(found[-1]) as z:
            for info in z.infolist():
                parts = info.filename[len(top):].split("/")
                if not info.filename.startswith(top) or info.is_dir() or ".." in parts:
                    continue
                path = os.path.join(target, *parts)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with z.open(info) as src, open(path, "wb") as out:
                    shutil.copyfileobj(src, out)
        opts += ["--add-data", f"{target}{os.pathsep}{name}"]
    return opts


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Gera dist/Sigma.exe com o PyInstaller e atualiza SHA256SUMS.txt.")
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"), help="pasta do .exe (padrão: dist/)")
    ap.add_argument("--build", default=os.path.join(ROOT, "build"), help="pasta de trabalho (padrão: build/)")
    args = ap.parse_args()
    if os.name != "nt":
        print("o Sigma.exe só pode ser gerado no Windows (o PyInstaller faz executáveis do sistema em que roda).\n"
              "No Linux e no macOS, gere o sigma.pyz: python3 packaging/build_pyz.py")
        return 1
    if importlib.util.find_spec("PyInstaller") is None:
        print("o PyInstaller não está instalado neste Python. Instale uma vez e rode de novo:\n"
              f"    \"{sys.executable}\" -m pip install pyinstaller\n"
              "(este script não instala nada sozinho)")
        return 1
    dist, build = os.path.abspath(args.dist), os.path.abspath(args.build)
    png = os.path.join(HERE, "sigma.png")
    icon = make_icon.write_icon(os.path.join(HERE, "sigma.ico"), png)
    web = os.path.join(ROOT, "sigma", "web")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
           "--name", "Sigma", "--icon", icon,
           "--noupx",   # executável comprimido com UPX dispara mais alarmes falsos de antivírus
           "--distpath", dist, "--workpath", os.path.join(build, "pyinstaller"), "--specpath", build,
           "--paths", ROOT,
           "--add-data", f"{web}{os.pathsep}sigma/web",   # o painel (app.html) vai junto, em sigma/web
           # a janelinha do Tk acha o Σ em _MEIPASS/sigma (app.icon_file): iconbitmap no Windows, iconphoto nos outros
           "--add-data", f"{icon}{os.pathsep}sigma",
           "--add-data", f"{png}{os.pathsep}sigma"]
    for mod in hidden_imports():
        cmd += ["--hidden-import", mod]
    cmd.append(os.path.join(HERE, "sigma_main.py"))
    print("gerando o Sigma.exe (leva alguns minutos)...", flush=True)
    with tempfile.TemporaryDirectory(prefix="sigma-tcl-") as tmp:   # pasta curta: o tzdata do Tcl é fundo
        tcl = tcl_tk_data(tmp)
        if tcl:
            print("Tcl/Tk 9 em zip: as bibliotecas da janelinha de controle vão junto no .exe", flush=True)
        r = subprocess.run(cmd[:-1] + tcl + cmd[-1:], cwd=ROOT)
    exe = os.path.join(dist, "Sigma.exe")
    if r.returncode or not os.path.isfile(exe):
        print(f"o PyInstaller falhou (código {r.returncode}); veja as mensagens acima")
        return r.returncode or 1
    digest = update_sums(dist, exe)
    print(f"\npronto: {exe} ({os.path.getsize(exe) / 1e6:.1f} MB)\n"
          f"  sha256 {digest}  (em {os.path.join(dist, SUMS)})\n"
          "  o executável não tem assinatura digital: o SmartScreen e alguns antivírus podem avisar na\n"
          "  primeira vez (o site explica isso e mostra o SHA-256 para conferir)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
