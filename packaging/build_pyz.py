"""Gera dist/sigma.pyz: o app inteiro num arquivo só, para Linux e macOS (roda em qualquer Python 3.11+).

    python packaging/build_pyz.py            depois:  python3 dist/sigma.pyz

Só biblioteca padrão (zipapp). O SHA-256 entra em dist/SHA256SUMS.txt, que o site mostra para quem quiser
conferir o download (formato do sha256sum: `sha256sum -c SHA256SUMS.txt` confere tudo de uma vez).
"""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import zipapp
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SUMS = "SHA256SUMS.txt"
# Entrada do .pyz. Confere a versão antes de importar o app: num Python velho a pessoa lê o motivo em
# português em vez de um erro de sintaxe. Por isso este arquivo só usa sintaxe antiga.
MAIN = '''"""Sigma: execute e pronto (python3 sigma.pyz). Opções: python3 sigma.pyz --help"""
import sys

if sys.version_info < (3, 11):
    sys.exit("o Sigma precisa do Python 3.11 ou mais novo (este é o %d.%d)" % sys.version_info[:2])

if __name__ == "__main__":
    from sigma.app import main
    sys.exit(main())
'''


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def update_sums(dist, path):
    """Põe (ou troca) a linha do arquivo em SHA256SUMS.txt; tira linhas de arquivos que não existem mais."""
    sums = os.path.join(dist, SUMS)
    lines = {}
    try:
        with open(sums, encoding="utf-8") as f:
            for line in f:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    lines[parts[1].strip().lstrip("*")] = parts[0]
    except FileNotFoundError:
        pass
    name = os.path.basename(path)
    lines[name] = sha256_file(path)
    lines = {n: h for n, h in lines.items() if os.path.isfile(os.path.join(dist, n))}
    with open(sums + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.writelines(f"{lines[n]}  {n}\n" for n in sorted(lines))
    os.replace(sums + ".tmp", sums)
    return lines[name]


# o que o app não pode deixar de ter: sem o netsafe nada conecta, sem o web/ não há painel nem páginas. A
# cópia é a pasta sigma/ inteira (copytree), mas esta lista é a garantia: todo módulo do pacote entra (o app
# importa node/rpc/forum/evm só na hora de usar; catalog/securestore já no começo) e nenhuma página falta.
ESSENTIAL = ("__main__.py", "sigma/__init__.py", "sigma/app.py", "sigma/netsafe.py", "sigma/host.py",
             "sigma/miner.py", "sigma/engines.py", "sigma/client.py", "sigma/chain.py", "sigma/crypto.py",
             "sigma/pow.py", "sigma/sigmacode.py", "sigma/node.py", "sigma/rpc.py", "sigma/evm.py",
             "sigma/forum.py", "sigma/tasks.py", "sigma/catalog.py", "sigma/securestore.py", "sigma/hardware.py",
             "sigma/cli.py", "sigma/sigma.png", "sigma/web/app.html", "sigma/web/index.html",
             "sigma/web/baixar.html", "sigma/web/tutorial.html", "sigma/web/carteira.html", "sigma/web/forum.html",
             "sigma/web/sigma.css", "sigma/web/og.png")


def stage(dest):
    """Cópia limpa do pacote (sem caches), o ícone da janelinha e o __main__.py: é isso que vai para o .pyz."""
    shutil.copytree(os.path.join(ROOT, "sigma"), os.path.join(dest, "sigma"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.tmp"))
    # o Tk lê o Σ de dentro do .pyz (app.icon_data("sigma.png")): sem ele a janelinha mostra a pena do Tk
    png = os.path.join(HERE, "sigma.png")
    if not os.path.isfile(png):
        sys.path.insert(0, HERE)
        import make_icon
        make_icon.write_icon(os.path.join(HERE, "sigma.ico"), png)
    shutil.copyfile(png, os.path.join(dest, "sigma", "sigma.png"))
    with open(os.path.join(dest, "__main__.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(MAIN)
    missing = [p for p in ESSENTIAL if not os.path.isfile(os.path.join(dest, *p.split("/")))]
    if missing:
        raise SystemExit("faltam arquivos no pacote: " + ", ".join(missing))


def build(dist):
    os.makedirs(dist, exist_ok=True)
    out = os.path.join(dist, "sigma.pyz")
    with tempfile.TemporaryDirectory(prefix="sigma-pyz-") as tmp:
        stage(tmp)
        # a linha #! deixa rodar direto no Linux/macOS (./sigma.pyz); o arquivo final só aparece pronto
        zipapp.create_archive(tmp, out + ".tmp", interpreter="/usr/bin/env python3", compressed=True)
    os.replace(out + ".tmp", out)
    with zipfile.ZipFile(out) as z:   # confere o que foi gravado de fato
        names = set(z.namelist())
    missing = [p for p in ESSENTIAL if p not in names]
    if missing:
        os.remove(out)
        raise SystemExit("o sigma.pyz saiu sem: " + ", ".join(missing))
    return out


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Gera dist/sigma.pyz (zipapp do app Sigma) e atualiza SHA256SUMS.txt.")
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"), help="pasta de saída (padrão: dist/)")
    args = ap.parse_args()
    if not os.path.isfile(os.path.join(ROOT, "sigma", "app.py")):
        sys.exit(f"não achei o pacote sigma/ em {ROOT}")
    out = build(os.path.abspath(args.dist))
    digest = update_sums(os.path.dirname(out), out)
    print(f"pronto: {out} ({os.path.getsize(out) / 1e6:.2f} MB)\n"
          f"  sha256 {digest}  (em {os.path.join(os.path.dirname(out), SUMS)})\n"
          f"  rodar: python3 sigma.pyz   (Python 3.11+; sem instalar nada)")


if __name__ == "__main__":
    main()
