"""Conexões de saída com cuidado: quem decide o endereço pode ser um estranho da rede.

- `blocked(ip)` / `is_internal(host)`: endereço da rede interna (roteador, localhost, nuvem, CGNAT...).
- `safe_opener()`: baixa de URLs escolhidas por outras pessoas (modelos de IA) conferindo, a cada conexão e
  a cada redirecionamento, o IP em que de fato conecta. Assim nem redirect nem DNS rebinding levam o download
  para dentro da casa de quem hospeda. Sem FTP, sem file:// e sem proxy.
- `read_capped(resp, limite)`: lê uma resposta HTTP sem estourar a memória.
- `ssl_context()` / `install_default_opener()`: HTTPS que funciona também no Python do python.org no macOS.
"""
import functools
import http.client
import ipaddress
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

CHUNK = 1 << 16


def blocked(ip) -> bool:
    """IP fora da internet pública (privado, loopback, link-local, CGNAT, reservado, multicast...)."""
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return True
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return not ip.is_global or ip.is_multicast


def is_internal(host) -> bool:
    """O nome aponta para a rede interna? Nome que não resolve não é interno (a conexão falha sozinha)."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    return any(blocked(info[4][0]) for info in infos)


def _connect_public(host, port, timeout):
    """Resolve uma vez, confere todos os IPs e conecta no IP conferido (fecha a porta para o DNS rebinding)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        raise OSError(f"não consegui resolver {host}: {e}") from None
    if not infos:
        raise OSError(f"não consegui resolver {host}")
    if any(blocked(info[4][0]) for info in infos):
        raise OSError("a URL aponta para a rede interna; recusada")
    family, socktype, proto, _, addr = infos[0]
    sock = socket.socket(family, socktype, proto)
    try:
        if timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(timeout)
        sock.connect(addr)
    except BaseException:
        sock.close()
        raise
    return sock


class SafeHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _connect_public(self.host, self.port, self.timeout)


class SafeHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = _connect_public(self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(SafeHTTPConnection, req)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(SafeHTTPSConnection, req, context=self._context)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme not in ("http", "https"):
            raise urllib.error.HTTPError(newurl, code, "redirecionamento recusado (só http/https)", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_opener(allow_internal=False):
    """Opener para URLs de estranhos. `allow_internal=True` só para redes de teste locais (o próprio nó é local):
    mesmo assim, só http/https, sem proxy e sem file://."""
    od = urllib.request.OpenerDirector()
    if allow_internal:
        handlers = [urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(context=ssl_context())]
    else:
        handlers = [_SafeHTTPHandler(), _SafeHTTPSHandler(context=ssl_context())]
    for h in handlers + [_SafeRedirect(), urllib.request.HTTPDefaultErrorHandler(),
                         urllib.request.HTTPErrorProcessor()]:
        od.add_handler(h)
    return od


def read_capped(resp, limit: int) -> bytes:
    """Lê no máximo `limit` bytes; ValueError se a resposta passar disso (sem guardar o excesso na memória)."""
    parts, n = [], 0
    while True:
        chunk = resp.read(min(CHUNK, limit + 1 - n))
        if not chunk:
            return b"".join(parts)
        n += len(chunk)
        if n > limit:
            raise ValueError("resposta grande demais")
        parts.append(chunk)


@functools.lru_cache(maxsize=1)
def ssl_context():
    """Contexto HTTPS padrão. No macOS, o Python do python.org vem sem certificados: usa os do sistema."""
    ctx = ssl.create_default_context()
    if sys.platform == "darwin" and not os.environ.get("SSL_CERT_FILE"):
        paths = ssl.get_default_verify_paths()
        if not (paths.cafile and os.path.exists(paths.cafile)):
            try:
                out = subprocess.run(["security", "find-certificate", "-a", "-p",
                                      "/System/Library/Keychains/SystemRootCertificates.keychain"],
                                     capture_output=True, text=True, timeout=20).stdout
                if "BEGIN CERTIFICATE" in out:
                    ctx.load_verify_locations(cadata=out)
            except (OSError, subprocess.SubprocessError, ssl.SSLError, ValueError):
                pass
    return ctx


def install_default_opener():
    """Chamado no começo de app/cli/sigmacode: todo urlopen do processo usa o ssl_context() acima."""
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_context())))


def cert_problem(err) -> bool:
    """O erro é falta de certificados (caso clássico do Python do python.org no macOS)?"""
    return "CERTIFICATE_VERIFY_FAILED" in str(err)
