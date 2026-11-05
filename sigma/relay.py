"""Relé dos grupos (regras v8): liga coordenador e trabalhadores que estão atrás de roteador, sem ninguém abrir porta.

Os dois lados discam o relé (no nó público), que só repassa bytes: o túnel do grupo (sigma/tunnel.py, TLS 1.3 com
chave das máquinas) passa por dentro, cifrado de ponta a ponta, então o relé não lê nada nem consegue se passar por
ninguém. O relé só aceita membros do grupo, cada um provando com a assinatura da sua máquina:

    coordenador:  {"op": "host", "group", "addr", "ts", "sig"}  -> fica conectado e recebe "conn <token>" por pedido
    trabalhador:  {"op": "join", "group", "addr", "ts", "sig"}  -> o relé avisa o coordenador e espera ele abrir
    coordenador:  {"op": "data", "token"}                       -> o relé emenda as duas conexões ("ok") e repassa

A assinatura é sobre "sigma-relay:<op>:<grupo>:<ts>", com ts a até RELAY_SKEW segundos do relógio do relé.

Nas pontas (RelayHost e RelayJoin) o relé fica invisível: o coordenador continua escutando em 127.0.0.1 (o Hub do
túnel) e o trabalhador disca 127.0.0.1:<porta local> (o Link do túnel); os agentes daqui fazem a ponte.
"""
import json
import secrets
import socket
import threading
import time

from .crypto import verify

RELAY_SKEW = 300
JOIN_WAIT = 20.0
MAX_PIPES = 512
LINE_MAX = 4096


def relay_message(op, group, ts):
    return f"sigma-relay:{op}:{group}:{int(ts)}".encode()


def _readline(sock, limit=LINE_MAX):
    buf = b""
    while not buf.endswith(b"\n"):
        ch = sock.recv(1)
        if not ch:
            raise ConnectionError("conexão fechada")
        buf += ch
        if len(buf) > limit:
            raise ConnectionError("linha grande demais")
    return buf[:-1].decode("utf-8", "replace")


def _pipe(a, b):
    """Repassa bytes nos dois sentidos até um lado fechar."""
    def one(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
    t = threading.Thread(target=one, args=(a, b), daemon=True)
    t.start()
    one(b, a)
    t.join(timeout=5)
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


# ------------------------------------------------------------------ servidor (no nó)
class RelayServer:
    """`members(group)` -> {"coordinator": (addr, pub), "workers": {addr: pub}} ou None (grupo não existe)."""

    def __init__(self, host, port, members, log=print, now=time.time):
        self.host, self.port, self.members, self.log, self.now = host, port, members, log, now
        self.hosts = {}            # grupo -> socket de controle do coordenador
        self.pending = {}          # token -> (socket do trabalhador, evento, caixa)
        self.pipes = 0
        self.lock = threading.Lock()
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, name="sigma-rele", daemon=True).start()
        self.log(f"[relé] escutando em {self.host}:{self.port}")
        return self

    def stop(self):
        if self.sock:
            self.sock.close()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _check(self, msg, op):
        group, addr, ts, sig = msg.get("group"), msg.get("addr"), msg.get("ts"), msg.get("sig")
        if not isinstance(group, str) or not isinstance(addr, str) or not isinstance(ts, int) or not isinstance(sig, str):
            raise ValueError("pedido malformado")
        if abs(self.now() - ts) > RELAY_SKEW:
            raise ValueError("relógio fora do prazo")
        mem = self.members(group)
        if mem is None:
            raise ValueError("grupo não existe")
        if op == "host":
            caddr, pub = mem["coordinator"]
            if addr != caddr:
                raise ValueError("só o coordenador do grupo")
        else:
            pub = (mem.get("workers") or {}).get(addr)
            if pub is None:
                raise ValueError("esta máquina não é do grupo")
        if not pub or not verify(pub, relay_message(op, group, ts), sig):
            raise ValueError("assinatura não confere")
        return group

    def _serve(self, conn):
        try:
            conn.settimeout(30)
            msg = json.loads(_readline(conn))
            op = msg.get("op")
            if op == "host":
                group = self._check(msg, "host")
                with self.lock:
                    old = self.hosts.get(group)
                    self.hosts[group] = conn
                if old is not None:
                    try:
                        old.close()
                    except OSError:
                        pass
                conn.sendall(b"ok\n")
                conn.settimeout(None)
                return           # fica aberto: o relé escreve "conn <token>" nele
            if op == "join":
                group = self._check(msg, "join")
                with self.lock:
                    ctl = self.hosts.get(group)
                    if ctl is None:
                        raise ValueError("o coordenador do grupo não está conectado ao relé")
                    if self.pipes >= MAX_PIPES:
                        raise ValueError("relé cheio")
                    token = secrets.token_hex(16)
                    box = {"sock": None}
                    ev = threading.Event()
                    self.pending[token] = (conn, ev, box)
                try:
                    ctl.sendall(f"conn {token}\n".encode())
                except OSError:
                    with self.lock:
                        self.pending.pop(token, None)
                    raise ValueError("o coordenador caiu")
                if not ev.wait(JOIN_WAIT):
                    with self.lock:
                        self.pending.pop(token, None)
                    raise ValueError("o coordenador não respondeu")
                return           # quem emenda é o "data"
            if op == "data":
                token = msg.get("token")
                with self.lock:
                    item = self.pending.pop(token, None) if isinstance(token, str) else None
                if item is None:
                    raise ValueError("token inválido")
                worker, ev, box = item
                with self.lock:
                    self.pipes += 1
                try:
                    worker.settimeout(None)
                    conn.settimeout(None)
                    worker.sendall(b"ok\n")
                    conn.sendall(b"ok\n")
                    ev.set()
                    _pipe(worker, conn)
                finally:
                    with self.lock:
                        self.pipes -= 1
                return
            raise ValueError("op desconhecida")
        except (ValueError, ConnectionError, OSError, json.JSONDecodeError) as e:
            try:
                conn.sendall(f"erro {str(e)[:200]}\n".encode())
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass


# ------------------------------------------------------------------ pontas (nas máquinas)
def _hello(relay, op, group, wallet):
    host, _, port = relay.rpartition(":")
    s = socket.create_connection((host, int(port)), timeout=20)
    ts = int(time.time())
    msg = {"op": op, "group": group, "addr": wallet.address, "ts": ts,
           "sig": wallet.sign(relay_message(op, group, ts))}
    s.sendall(json.dumps(msg).encode() + b"\n")
    return s


class RelayHost:
    """Coordenador: fica conectado ao relé e, a cada trabalhador que chega, emenda uma conexão nova do relé com o
    Hub do túnel em `target` (127.0.0.1:<porta>)."""

    def __init__(self, relay, group, wallet, target, log=print):
        self.relay, self.group, self.wallet, self.target, self.log = relay, group, wallet, target, log
        self.stop_ev = threading.Event()
        self.thread = None
        self.ctl = None
        self.connected = False

    def start(self):
        self.thread = threading.Thread(target=self._run, name="sigma-rele-coord", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_ev.set()
        if self.ctl:
            try:
                self.ctl.close()
            except OSError:
                pass

    def _run(self):
        while not self.stop_ev.is_set():
            try:
                self.ctl = _hello(self.relay, "host", self.group, self.wallet)
                answer = _readline(self.ctl)
                if answer != "ok":
                    raise ConnectionError(answer)
                self.ctl.settimeout(None)
                self.connected = True
                while not self.stop_ev.is_set():
                    line = _readline(self.ctl)
                    if line.startswith("conn "):
                        threading.Thread(target=self._splice, args=(line[5:].strip(),), daemon=True).start()
            except (OSError, ConnectionError, ValueError) as e:
                self.connected = False
                if not self.stop_ev.is_set():
                    self.log(f"[relé] coordenador do grupo {self.group[:8]}: {e}; tentando de novo em 10 s")
                    self.stop_ev.wait(10)

    def _splice(self, token):
        try:
            host, _, port = self.relay.rpartition(":")
            up = socket.create_connection((host, int(port)), timeout=20)
            up.sendall(json.dumps({"op": "data", "token": token}).encode() + b"\n")
            if _readline(up) != "ok":
                up.close()
                return
            up.settimeout(None)
            th, _, tp = self.target.rpartition(":")
            local = socket.create_connection((th, int(tp)), timeout=10)
            local.settimeout(None)
            _pipe(up, local)
        except (OSError, ConnectionError):
            pass


class RelayJoin:
    """Trabalhador: escuta em 127.0.0.1:<porta> (o Link do túnel disca aqui) e leva cada conexão ao coordenador
    pelo relé."""

    def __init__(self, relay, group, wallet, log=print):
        self.relay, self.group, self.wallet, self.log = relay, group, wallet, log
        self.sock = None
        self.port = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, name="sigma-rele-trab", daemon=True).start()
        return self

    @property
    def endpoint(self):
        return f"127.0.0.1:{self.port}"

    def stop(self):
        if self.sock:
            self.sock.close()

    def _accept(self):
        while True:
            try:
                local, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._splice, args=(local,), daemon=True).start()

    def _splice(self, local):
        try:
            up = _hello(self.relay, "join", self.group, self.wallet)
            answer = _readline(up)
            if answer != "ok":
                self.log(f"[relé] grupo {self.group[:8]}: {answer}")
                up.close()
                local.close()
                return
            up.settimeout(None)
            _pipe(local, up)
        except (OSError, ConnectionError) as e:
            self.log(f"[relé] grupo {self.group[:8]}: {e}")
            try:
                local.close()
            except OSError:
                pass
