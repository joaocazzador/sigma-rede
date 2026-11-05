"""Atualização automática: quando a rede roda uma versão mais nova, o app baixa o arquivo novo (Sigma.exe ou
sigma.pyz) do próprio nó, confere o SHA-256 com a lista publicada ao lado (SHA256SUMS.txt), troca o arquivo e abre
a versão nova, que fecha esta e continua de onde parou (a passagem de bastão de sempre: claim_instance).

- Só vale para quem roda o .exe ou o .pyz. Do código-fonte (git), nada muda: quem roda assim atualiza pelo git.
- Sob um supervisor (systemd, pm2), o app só troca o arquivo e sai: o supervisor reinicia já com o novo.
- Nunca no meio de um pedido de IA (a máquina espera ficar livre) e no máximo uma tentativa a cada CHECK_EVERY.
- O arquivo novo vem do mesmo nó e pelo mesmo HTTPS de onde a pessoa baixou o primeiro: a confiança é a mesma
  do download manual, com a conferência do SHA-256 feita sozinha.
"""
import hashlib
import os
import re
import subprocess
import sys
import time
import urllib.request

CHECK_EVERY = 1800          # segundos entre tentativas (e depois de uma falha)
MAX_SIZE = 200_000_000      # nenhum instalador do Sigma passa disso
SUM_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)$")


class UpdateError(Exception):
    pass


def current_file():
    """(tipo, caminho) do que está rodando: ("exe", Sigma.exe), ("pyz", sigma.pyz) ou (None, None) no código-fonte."""
    if getattr(sys, "frozen", False):
        return "exe", os.path.abspath(sys.executable)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isfile(root) and root.lower().endswith(".pyz"):
        return "pyz", root
    return None, None


def published_name(kind):
    return {"exe": "Sigma.exe", "pyz": "sigma.pyz"}[kind]


def supervised():
    """Rodando sob systemd ou pm2: basta sair, eles reiniciam o serviço já com o arquivo novo."""
    return bool(os.environ.get("INVOCATION_ID") or os.environ.get("pm_id") or os.environ.get("PM2_HOME"))


def _get(url, timeout=60):
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "sigma-updater")]
    return opener.open(url, timeout=timeout)


def expected_sha(node, name):
    with _get(f"{node}/downloads/SHA256SUMS.txt", timeout=30) as r:
        text = r.read(100_000).decode("utf-8", "replace")
    for line in text.splitlines():
        m = SUM_LINE.match(line.strip())
        if m and m.group(2) == name:
            return m.group(1)
    raise UpdateError(f"{name} não está na lista de SHA-256 do nó")


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(node, kind, path):
    """Baixa o instalador publicado para `path + '.new'`, conferindo o SHA-256. Devolve o caminho baixado, ou None
    se o publicado é o mesmo arquivo que já está rodando (nada a trocar)."""
    name = published_name(kind)
    want = expected_sha(node, name)
    try:
        if file_sha(path) == want:
            return None
    except OSError:
        pass
    tmp = path + ".new"
    h, size = hashlib.sha256(), 0
    try:
        with _get(f"{node}/downloads/{name}", timeout=120) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SIZE:
                    raise UpdateError("o arquivo novo é grande demais")
                h.update(chunk)
                f.write(chunk)
    except (OSError, UpdateError):
        _remove(tmp)
        raise
    if h.hexdigest() != want:
        _remove(tmp)
        raise UpdateError("o SHA-256 do arquivo baixado não confere com o publicado; nada foi trocado")
    return tmp


def swap(path, new):
    """Põe o arquivo novo no lugar. O que está rodando vira .old (o Windows deixa renomear um .exe aberto, não
    apagar); o .old some na próxima abertura (cleanup)."""
    old = path + ".old"
    _remove(old)
    os.replace(path, old)
    try:
        os.replace(new, path)
    except OSError:
        os.replace(old, path)   # volta o de antes: nunca deixa a pasta sem o Sigma
        raise
    if os.name != "nt":
        os.chmod(path, 0o755)


def relaunch(kind, path, argv):
    """Abre a versão nova com os mesmos argumentos (sem abrir janela): ela pede para esta fechar e assume."""
    args = [a for a in argv if a != "--background"] + ["--background"]
    cmd = [path, *args] if kind == "exe" else [sys.executable, path, *args]
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=flags, start_new_session=os.name != "nt")


def cleanup():
    kind, path = current_file()
    if path:
        _remove(path + ".old")
        _remove(path + ".new")


def _remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


class AutoUpdater:
    """Chamado pelo laço do app (tick). `busy()` diz se a máquina está no meio de um pedido."""

    def __init__(self, node, version_available, busy, log, on_restart, argv=None, enabled=True):
        self.node, self.version_available, self.busy, self.log = node, version_available, busy, log
        self.on_restart, self.argv = on_restart, list(sys.argv[1:] if argv is None else argv)
        self.kind, self.path = current_file()
        self.enabled = enabled and self.kind is not None
        self.next_try = 0.0
        self.applied = None

    def tick(self):
        if not self.enabled or self.applied or time.time() < self.next_try:
            return False
        version = self.version_available()
        if not version or self.busy():
            return False
        self.next_try = time.time() + CHECK_EVERY
        try:
            self.log(f"[atualização] baixando o Sigma {version}")
            new = download(self.node(), self.kind, self.path)
            if new is None:   # o site ainda publica este mesmo arquivo: espera o novo aparecer
                self.log(f"[atualização] o arquivo do {version} ainda não está no site; tento de novo depois")
                return False
            swap(self.path, new)
        except (OSError, UpdateError) as e:
            self.log(f"[atualização] não deu para atualizar agora ({e}); tento de novo em {CHECK_EVERY // 60} min")
            return False
        self.applied = version
        self.log(f"[atualização] Sigma {version} instalado; reiniciando")
        if supervised():
            self.on_restart(None)   # o systemd ou o pm2 abre o arquivo novo
        else:
            self.on_restart(lambda: relaunch(self.kind, self.path, self.argv))
        return True
