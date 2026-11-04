"""Túnel do grupo de casa: o coordenador usa a memória dos PCs de casa pelo RPC do llama.cpp, sem ninguém abrir porta.

O RPC do llama.cpp (ggml-rpc-server) não tem autenticação nem cifra, e o llama-server abre de 14 a 18 conexões TCP
por servidor durante a carga. Aqui todas viajam por um túnel só, autenticado e cifrado, que o TRABALHADOR disca:

- Hub (coordenador, IP público): escuta TLS 1.3 com PSK. Cada trabalhador autorizado que disca ganha uma ponte em
  127.0.0.1:<porta efêmera> (o llama-server recebe `--rpc 127.0.0.1:<porta>`); cada conexão aceita na ponte vira um
  canal lógico dentro do túnel daquele trabalhador.
- Link (trabalhador, atrás de NAT): disca para o coordenador e, para cada canal que ele abre, conecta no alvo local
  (o porteiro ou o ggml-rpc-server em 127.0.0.1) e copia os bytes nos dois sentidos. Redisca sozinho, com espera
  crescente.

Autenticação: PSK de 32 bytes = HMAC-SHA256(x do ECDH secp256k1 entre as chaves de máquina da chain,
"sigma-tunel-v1|<coordenador>|<trabalhador>"); a identidade PSK é o endereço do trabalhador. O coordenador não tem
certificado e o trabalhador recusa servidor que mostre um. Só TLS 1.3 (módulo ssl do Python 3.13 ou mais novo).

Quadros dentro do TLS: tipo (1 byte), canal (4), número (8) e tamanho (4), depois os dados (no máximo FRAME_MAX).
- HELLO/WELCOME abrem o enlace: o trabalhador apresenta a sessão (ou nenhuma) e até onde recebeu; o coordenador diz
  se retomou ou se começou uma sessão nova, e até onde ele recebeu.
- OPEN (só o coordenador abre canais, em ordem), DATA, CLOSE (fim de um sentido, como o FIN do TCP), RESET (canal
  abortado) e WINDOW (crédito) são numerados por sentido e ficam no buffer de reenvio até o ACK do outro lado.
- ACK, PING/PONG (rtt, NAT aberto, enlace mudo) e BYE (fim da sessão) não são numerados.

Retomada: se o enlace de fora cai, as conexões locais continuam abertas e cada lado guarda o que o outro não
confirmou. Ao rediscar dentro da carência (GRACE), o trabalhador reapresenta a sessão; cada lado diz até onde recebeu
e reenvia o resto, na ordem: os bytes chegam idênticos. Passada a carência, os dois lados fecham as conexões locais.
Só o mesmo trabalhador autenticado retoma a própria sessão.

Contrapressão: o crédito (WINDOW) de cada canal acompanha o que a conexão local de fato aceitou (até CHAN_WINDOW por
canal e TUNNEL_WINDOW somado no túnel); o que foi lido e espera a vez tem teto por canal (PENDING_MAX) e o buffer de
reenvio tem teto por túnel (INFLIGHT_MAX). Um leitor lento só segura o próprio canal. O escritor é justo: cada canal
manda no máximo QUANTUM bytes e passa a vez, então uma carga de pesos não trava as outras conexões.

Tudo roda num laço asyncio numa thread própria (uma por Hub/Link); stop() fecha conexões, portas e a thread.
"""
import asyncio
import collections
import functools
import hashlib
import hmac
import ipaddress
import os
import random
import secrets
import socket
import ssl
import struct
import sys
import threading
import time

from . import crypto
from .crypto import address_of, norm_address, pub_of

# TLS-PSK chegou ao módulo ssl no Python 3.13; antes disso o módulo importa, mas Hub e Link recusam
AVAILABLE = (sys.version_info >= (3, 13) and bool(getattr(ssl, "HAS_PSK", False))
             and bool(getattr(ssl, "HAS_TLSv1_3", False)) and hasattr(ssl.SSLContext, "set_psk_server_callback"))
UNAVAILABLE = "túnel indisponível nesta versão do Python (precisa 3.13 ou mais novo)"

VERSION = 1                     # versão dos quadros (HELLO/WELCOME): lados diferentes não conversam
LABEL = b"sigma-tunel-v1"       # rótulo da PSK
PORT = 8790                     # porta sugerida do coordenador
FRAME_MAX = 256 << 10           # teto de dados por quadro: quem manda mais tem o túnel derrubado
QUANTUM = 64 << 10              # bytes que um canal manda por vez antes de passar a vez (escritor justo)
READ_CHUNK = 64 << 10           # leitura da conexão local
PENDING_MAX = 256 << 10         # lido da conexão local esperando vez e crédito, por canal (depois disso, para de ler)
CHAN_MIN = 64 << 10             # crédito garantido de cada canal: anda mesmo com o túnel cheio
CHAN_WINDOW = 1 << 20           # crédito máximo de um canal (recebido e ainda não entregue à conexão local)
TUNNEL_WINDOW = 16 << 20        # crédito somado dos canais de um túnel, acima do garantido
GROW_AFTER = 0.25               # s: canal que gasta meia janela mais rápido que isso ganha janela maior
INFLIGHT_MAX = 8 << 20          # buffer de reenvio (mandado e ainda sem ACK), por túnel
UNACKED_FRAMES_MAX = 50_000     # quadros sem ACK: passou disso, o outro lado não confirma nada e o túnel cai
ACK_BYTES = 256 << 10           # confirma ao receber isso...
ACK_DELAY = 0.01                # ...ou 10 ms depois do primeiro quadro sem confirmação
YIELD_EVERY = 1 << 20           # o escritor devolve a vez ao laço a cada 1 MB (os outros canais leem e escrevem)
MAX_CHANNELS = 64               # canais abertos ao mesmo tempo por trabalhador
GRACE = 120                     # s que a sessão espera o enlace voltar antes de fechar as conexões locais
PING_EVERY = 5                  # s entre pings
DEAD_AFTER = 20                 # s sem receber nada: o enlace está mudo (o TCP pode levar minutos para perceber)
HANDSHAKE_TIMEOUT = 10          # s para o TLS mais o HELLO/WELCOME
CONNECT_TIMEOUT = 10            # s para o trabalhador conectar no alvo local
MAX_HANDSHAKES = 64             # handshakes simultâneos no coordenador...
MAX_HANDSHAKES_PER_IP = 8       # ...e por IP (rede /64 no IPv6)
FAILS_PER_IP = 30               # recusas de um IP dentro de FAIL_WINDOW s...
FAIL_WINDOW = 60
BAN_FOR = 60                    # ...deixam esse IP ignorado por BAN_FOR s
AUTH_EVERY = 30                 # s entre conferências de quem ainda está autorizado (peers)
RETRY_MIN, RETRY_MAX = 0.5, 30  # s entre tentativas de discar: dobra a cada falha
STABLE_AFTER = 10               # s de enlace de pé que zeram a espera crescente
LINGER = 1.0                    # s para uma conexão local fechada esvaziar antes de ser cortada
_READ_LIMIT = FRAME_MAX + 64

HELLO, WELCOME, OPEN, DATA, CLOSE, RESET, WINDOW, ACK, PING, PONG, BYE = 1, 2, 16, 17, 18, 19, 20, 32, 33, 34, 35
KINDS = frozenset((HELLO, WELCOME, OPEN, DATA, CLOSE, RESET, WINDOW, ACK, PING, PONG, BYE))
NUMBERED = frozenset((OPEN, DATA, CLOSE, RESET, WINDOW))
_HDR = struct.Struct("!BIQI")         # tipo, canal, número (ou ack/ping), tamanho dos dados
_HELLO = struct.Struct("!H16s")       # versão, sessão (zeros = nova); o número do cabeçalho = até onde recebeu
_WELCOME = struct.Struct("!H16sB")    # versão, sessão, 1 = retomada
_U64 = struct.Struct("!Q")
NO_SESSION = bytes(16)
MAX_CHANNEL_ID = 2 ** 32 - 1


class TunnelError(Exception):
    """Túnel indisponível (Python antigo), chave inválida ou porta que não abriu."""


class _Violation(Exception):
    """O outro lado mandou algo fora do protocolo: o túnel dele cai (só o dele)."""


class _Refused(Exception):
    """Conexão que não chegou a virar túnel (identidade, chave, certificado, versão, HELLO)."""


def _pt(n):
    """Número no jeito brasileiro (262.144), para o registro."""
    return f"{n:,.0f}".replace(",", ".")


def _short(addr):
    return f"{addr[:9]}…{addr[-4:]}" if isinstance(addr, str) and len(addr) > 16 else str(addr)


def _priv(key):
    """Chave privada da máquina: o inteiro ou a carteira (crypto.Wallet)."""
    priv = getattr(key, "priv", key)
    if not isinstance(priv, int) or isinstance(priv, bool) or not 0 < priv < crypto.N:
        raise TunnelError("chave de máquina inválida")
    return priv


def _hostport(value, what):
    """("host", porta) ou "host:porta" -> (host, porta)."""
    if isinstance(value, str):
        host, sep, port = value.rpartition(":")
        if not sep:
            raise TunnelError(f"{what} inválido: {value!r} (use host:porta)")
        value = (host.strip("[]"), port)
    try:
        host, port = value
        port = int(port)
    except (TypeError, ValueError):
        raise TunnelError(f"{what} inválido: {value!r}") from None
    if not 0 <= port <= 65535:
        raise TunnelError(f"{what} inválido: porta {port}")
    return str(host), port


def _ip_key(ip):
    """Chave dos limites por IP: o IPv4 (também o mapeado em IPv6) ou a rede /64 do IPv6."""
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return str(ip)[:64]
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


def tunnel_psk(my_priv, peer_pub, hub, worker):
    """PSK de 32 bytes do túnel entre o coordenador `hub` e o trabalhador `worker` (endereços). Os dois lados chegam
    à mesma chave: o x do ECDH é o mesmo com (priv do coordenador, pub do trabalhador) e (priv do trabalhador, pub do
    coordenador). ValueError se a chave pública não for um ponto da secp256k1 (ataque de curva inválida)."""
    shared = crypto._mul(_priv(my_priv), crypto._point(peer_pub))
    if shared is None:
        raise ValueError("chave pública inválida")
    msg = b"|".join((LABEL, norm_address(hub).encode(), norm_address(worker).encode()))
    return hmac.new(shared[0].to_bytes(32, "big"), msg, hashlib.sha256).digest()


def _server_context(psk_for):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.num_tickets = 0                  # a retomada é da sessão do túnel, não do TLS
    ctx.set_psk_server_callback(psk_for)
    return ctx


def _client_context(identity, psk):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED  # nenhuma CA carregada: servidor que mostra certificado (sem a PSK) é recusado
    ctx.set_psk_client_callback(lambda hint: (identity, psk))
    return ctx


def _nodelay(transport_or_writer):
    """Sem o algoritmo de Nagle: o RPC do llama.cpp troca mensagens pequenas de pedido e resposta, e o Windows as
    seguraria até ~200 ms esperando juntar mais bytes."""
    sock = transport_or_writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


def _frame(kind, cid=0, seq=0, body=b""):
    return _HDR.pack(kind, cid, seq, len(body)) + body


def _abort(transport):
    try:
        if transport is not None:
            transport.abort()
    except Exception:
        pass


# ------------------------------------------------------------------ dono de uma conexão em 127.0.0.1
def local_peer_pid(server_port, client_port):
    """PID do processo que abriu a conexão 127.0.0.1:`client_port` -> 127.0.0.1:`server_port`. None quando não dá
    para saber (sistema sem suporte, conexão que já fechou): quem filtra por PID recusa nesse caso."""
    try:
        if os.name == "nt":
            return _pid_windows(server_port, client_port)
        if sys.platform.startswith("linux"):
            return _pid_linux(server_port, client_port)
    except Exception:
        return None
    return None


def _pid_windows(server_port, client_port):
    import ctypes
    from ctypes import wintypes
    get = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    get.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int,
                    wintypes.ULONG]
    get.restype = wintypes.DWORD
    size = wintypes.DWORD(0)
    get(None, ctypes.byref(size), False, 2, 4, 0)   # AF_INET, TCP_TABLE_OWNER_PID_CONNECTIONS
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value + 8192)
        size = wintypes.DWORD(len(buf))
        ret = get(buf, ctypes.byref(size), False, 2, 4, 0)
        if ret == 0:
            break
        if ret != 122:   # ERROR_INSUFFICIENT_BUFFER: a tabela cresceu, tenta de novo
            return None
    else:
        return None
    raw = buf.raw
    loopback = 0x0100007F   # 127.0.0.1 como o Windows guarda (ordem de rede lida como inteiro little-endian)
    for i in range(struct.unpack_from("<I", raw, 0)[0]):   # MIB_TCPROW_OWNER_PID: 6 DWORDs
        _, laddr, lport, raddr, rport, pid = struct.unpack_from("<6I", raw, 4 + 24 * i)
        if (laddr == loopback and raddr == loopback and int.from_bytes(struct.pack("<H", lport & 0xFFFF), "big")
                == client_port and int.from_bytes(struct.pack("<H", rport & 0xFFFF), "big") == server_port):
            return pid
    return None


def _pid_linux(server_port, client_port):
    inode = None
    with open("/proc/net/tcp", encoding="ascii") as f:
        for line in f.read().splitlines()[1:]:
            parts = line.split()
            if len(parts) > 9 and parts[1] == f"0100007F:{client_port:04X}" and parts[2] == f"0100007F:{server_port:04X}":
                inode = parts[9]
                break
    if not inode or inode == "0":
        return None
    want = f"socket:[{inode}]"
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fds = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fds):
                if os.readlink(os.path.join(fds, fd)) == want:
                    return int(pid)
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------- canal, enlace e sessão
class _Channel:
    """Uma conexão local (do llama-server na ponte, ou do trabalhador até o alvo) dentro do túnel."""

    def __init__(self, cid):
        self.id = cid
        self.reader = self.writer = None
        self.pending = bytearray()           # lido da conexão local, esperando a vez e o crédito
        self.sent = 0                        # bytes já postos em quadros
        self.send_limit = CHAN_MIN           # até onde o outro lado deixa mandar (crédito acumulado)
        self.inbox = collections.deque()     # recebido, esperando a conexão local aceitar
        self.inbox_bytes = 0
        self.received = 0
        self.recv_limit = CHAN_MIN           # até onde deixamos o outro lado mandar
        self.window = CHAN_MIN               # crédito que damos de cada vez (cresce com o uso, até CHAN_WINDOW)
        self.granted_at = time.monotonic()
        self.consumed = 0                    # já entregue à conexão local
        self.local_eof = False               # a conexão local fechou o lado dela: CLOSE depois dos dados
        self.close_sent = False
        self.peer_eof = False                # o outro lado mandou CLOSE...
        self.eof_written = False             # ...e o fim já foi passado à conexão local
        self.done = False
        self.room = asyncio.Event()          # o pendente baixou do teto: a leitura local continua
        self.room.set()
        self.arrived = asyncio.Event()       # chegou dado ou CLOSE para entregar
        self.tasks = []


class _Wire:
    """Uma conexão TLS de fora (o enlace). A sessão sobrevive a ela: quando cai, outra toma o lugar."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.ctrl = collections.deque()      # ACK/PING/PONG/BYE: não numerados, vão na frente
        self.written = 0                     # maior número já escrito neste enlace
        self.last_rx = time.monotonic()
        self.since = time.time()
        self.ping_id = 0
        self.ping_at = None
        self.tasks = []
        self.ended = asyncio.Event()

    def abort(self):
        _abort(self.writer.transport)


class _Session:
    """O que sobrevive às quedas do enlace: canais, numeração, buffer de reenvio e crédito. Só a thread do laço mexe."""

    def __init__(self, owner, sid, peer):
        self.owner, self.loop = owner, owner._loop
        self.id, self.peer = sid, peer
        self.psk = None
        self.channels = {}
        self.max_id = 0                      # maior canal já aberto: os números de canal só crescem
        self.out_seq = 0                     # último número dado a um quadro nosso
        self.written_max = 0                 # maior número já escrito em algum enlace
        self.acked = 0                       # o outro lado confirmou até aqui
        self.queue = collections.deque()     # numerados a escrever neste enlace: (número, quadro)
        self.unacked = collections.deque()   # escritos, esperando ACK
        self.unacked_bytes = 0               # bytes em queue + unacked: o buffer de reenvio
        self.recv_seq = 0                    # último quadro numerado recebido
        self.ack_sent = 0
        self.ack_bytes = 0
        self.ack_due = False
        self.ack_timer = None
        self.ready = collections.OrderedDict()   # canais com algo a mandar, em rodízio
        self.wake = asyncio.Event()
        self.wire = None
        self.expiry = None
        self.started = self.since = time.time()
        self.bytes_in = self.bytes_out = 0
        self.rtt = None
        self.resumes = 0
        self.opened = 0
        self.port = None
        self.server = None
        self.closed = False

    # ------------------------------------------------------------ enlace
    def check_resume(self, peer_recv):
        if not self.acked <= peer_recv <= self.written_max:
            raise _Violation(f"o outro lado diz ter recebido até o quadro {peer_recv}, mas o certo é entre "
                             f"{self.acked} e {self.written_max}")

    def attach(self, wire, peer_recv):
        """Prende um enlace (novo ou de retomada): descarta o que o outro lado diz já ter recebido e põe o resto na
        fila, na ordem. Quem chama já mandou o HELLO/WELCOME, que vai antes de tudo."""
        self.check_resume(peer_recv)
        if self.wire is not None:
            self._drop_wire(self.wire)       # enlace velho, meio morto: o novo toma o lugar
        frames = self.unacked
        frames.extend(self.queue)
        while frames and frames[0][0] <= peer_recv:
            self.unacked_bytes -= len(frames.popleft()[1])
        self.queue, self.unacked = frames, collections.deque()
        self.acked = wire.written = peer_recv
        if self.expiry is not None:
            self.expiry.cancel()
            self.expiry = None
        self.wire = wire
        self.ack_sent, self.ack_bytes, self.ack_due = self.recv_seq, 0, False   # o HELLO/WELCOME já confirmou
        self.since = time.time()
        wire.tasks = [self.loop.create_task(self._read_loop(wire)), self.loop.create_task(self._write_loop(wire))]
        self.wake.set()

    def _drop_wire(self, wire):
        if self.wire is wire:
            self.wire = None
        wire.abort()
        current = asyncio.current_task()
        for t in wire.tasks:
            if t is not current:
                t.cancel()
        wire.ended.set()

    def wire_lost(self, wire, why):
        if wire is not self.wire or self.closed:
            self._drop_wire(wire)
            return
        self._drop_wire(wire)
        self.since = time.time()
        self.owner._wire_down(self, why)
        if self.expiry is None:
            self.expiry = self.loop.call_later(self.owner.grace, self._expired)

    def _expired(self):
        self.expiry = None
        self.end(f"o enlace não voltou em {_pt(self.owner.grace)} s")

    def end(self, why, bye=False):
        """Fim da sessão: fecha as conexões locais (de forma limpa), o enlace e a ponte. `bye` avisa o outro lado,
        que então fecha as dele na hora em vez de esperar a carência."""
        if self.closed:
            return
        self.closed = True
        for h in (self.expiry, self.ack_timer):
            if h is not None:
                h.cancel()
        self.expiry = self.ack_timer = None
        for chan in list(self.channels.values()):
            self._release(chan)
        self.channels.clear()
        self.ready.clear()
        self.queue.clear()
        self.unacked.clear()
        self.unacked_bytes = 0
        wire = self.wire
        if wire is not None:
            self.wire = None
            if bye:
                try:
                    wire.writer.write(_frame(BYE))
                except Exception:
                    pass
            current = asyncio.current_task()
            for t in wire.tasks:
                if t is not current:
                    t.cancel()
            self.owner._close_later(wire.writer.transport)
            wire.ended.set()
        self.wake.set()
        self.owner._session_ended(self, why)

    def tick(self, now):
        """Ping e enlace mudo (chamado a cada PING_EVERY)."""
        wire = self.wire
        if wire is None or self.closed:
            return
        if now - wire.last_rx > self.owner.dead_after:
            self.wire_lost(wire, f"nada recebido em {_pt(self.owner.dead_after)} s")
            return
        if len(self.queue) + len(self.unacked) > UNACKED_FRAMES_MAX:
            self.owner._violation(self, "o outro lado não confirma o que recebe")
            return
        wire.ping_id += 1
        wire.ping_at = time.perf_counter()
        wire.ctrl.append(_frame(PING, 0, wire.ping_id))
        self.wake.set()

    # ------------------------------------------------------------ envio
    def number(self, kind, cid, body=b""):
        """Quadro numerado: vai para a fila e fica guardado até o outro lado confirmar (ACK)."""
        if self.closed:
            return
        self.out_seq += 1
        frame = _HDR.pack(kind, cid, self.out_seq, len(body)) + body
        self.queue.append((self.out_seq, frame))
        self.unacked_bytes += len(frame)
        self.wake.set()

    def ready_chan(self, chan):
        if not chan.done and not chan.close_sent:
            if chan.id not in self.ready:
                self.ready[chan.id] = chan
            self.wake.set()

    def next_frame(self):
        """Escritor justo: cada canal pronto manda no máximo QUANTUM bytes e passa a vez. Põe na fila um DATA, ou o
        CLOSE de um canal que já mandou tudo; False se ninguém pode mandar agora (sem dados, sem crédito ou com o
        buffer de reenvio cheio)."""
        if self.unacked_bytes >= self.owner.inflight_max:
            return False
        ready = self.ready
        while ready:
            cid, chan = ready.popitem(last=False)
            if chan.done or chan.close_sent:
                continue
            credit = chan.send_limit - chan.sent
            if chan.pending and credit > 0:
                n = min(len(chan.pending), credit, QUANTUM)
                data = bytes(chan.pending[:n])
                del chan.pending[:n]
                chan.sent += n
                if len(chan.pending) < PENDING_MAX:
                    chan.room.set()
                if (chan.pending and chan.send_limit > chan.sent) or (not chan.pending and chan.local_eof):
                    ready[cid] = chan        # ainda tem o que mandar: volta para o fim da fila
                self.number(DATA, cid, data)
                return True
            if not chan.pending and chan.local_eof:
                chan.close_sent = True
                self.number(CLOSE, cid)
                self._finish_if_done(chan)
                return True
            # sem crédito: sai do rodízio e volta quando chegar WINDOW
        return False

    def grant(self, chan):
        """Mais crédito para o outro lado conforme a conexão local aceita os bytes. A janela dobra (até CHAN_WINDOW,
        dentro do TUNNEL_WINDOW somado) quando o canal gasta meia janela depressa: transferência grande anda rápido,
        conversa miúda não prende memória."""
        if chan.done or chan.peer_eof or chan.recv_limit - chan.consumed > chan.window // 2:
            return
        now = time.monotonic()
        if chan.window < CHAN_WINDOW and now - chan.granted_at < GROW_AFTER:
            extra = sum(c.window - CHAN_MIN for c in self.channels.values())
            chan.window += max(0, min(chan.window, CHAN_WINDOW - chan.window, TUNNEL_WINDOW - extra))
        chan.granted_at = now
        chan.recv_limit = chan.consumed + chan.window
        self.number(WINDOW, chan.id, _U64.pack(chan.recv_limit))

    def _schedule_ack(self):
        if self.ack_bytes >= ACK_BYTES:
            self.ack_due = True
            self.wake.set()
        elif self.ack_timer is None:
            self.ack_timer = self.loop.call_later(ACK_DELAY, self._ack_timer_fired)

    def _ack_timer_fired(self):
        self.ack_timer = None
        self.ack_due = True
        self.wake.set()

    async def _write_loop(self, wire):
        out = wire.writer
        burst = 0
        try:
            while wire is self.wire:
                if self.ack_due:
                    self.ack_due = False
                    if self.ack_timer is not None:
                        self.ack_timer.cancel()
                        self.ack_timer = None
                    if self.recv_seq > self.ack_sent:
                        wire.ctrl.append(_frame(ACK, 0, self.recv_seq))
                        self.ack_sent, self.ack_bytes = self.recv_seq, 0
                parts = list(wire.ctrl)
                wire.ctrl.clear()
                if self.queue or self.next_frame():
                    item = self.queue.popleft()
                    self.unacked.append(item)
                    wire.written = item[0]
                    if item[0] > self.written_max:
                        self.written_max = item[0]
                    parts.append(item[1])
                if parts:
                    data = parts[0] if len(parts) == 1 else b"".join(parts)
                    out.write(data)
                    self.bytes_out += len(data)
                    burst += len(data)
                    await out.drain()
                    if burst >= YIELD_EVERY:
                        burst = 0
                        await asyncio.sleep(0)
                    continue
                self.wake.clear()
                if wire.ctrl or self.queue or self.ack_due or self.next_frame():
                    continue
                burst = 0
                await self.wake.wait()
        except (ConnectionError, OSError) as e:
            self.wire_lost(wire, f"o enlace caiu ao escrever ({e.__class__.__name__})")

    # ------------------------------------------------------------ recepção
    async def _read_loop(self, wire):
        reader = wire.reader
        try:
            while wire is self.wire:
                head = await reader.readexactly(_HDR.size)
                kind, cid, seq, size = _HDR.unpack(head)
                if kind not in KINDS:
                    raise _Violation(f"tipo de quadro desconhecido ({kind})")
                if size > FRAME_MAX:
                    raise _Violation(f"quadro grande demais ({_pt(size)} bytes; o teto é {_pt(FRAME_MAX)})")
                body = await reader.readexactly(size) if size else b""
                if wire is not self.wire:
                    return
                wire.last_rx = time.monotonic()
                self.bytes_in += _HDR.size + size
                if not self.on_frame(wire, kind, cid, seq, body):
                    return
        except _Violation as e:
            if wire is self.wire:
                self.owner._violation(self, str(e))
        except (EOFError, ConnectionError, OSError) as e:
            self.wire_lost(wire, "o outro lado fechou o enlace" if isinstance(e, EOFError)
                           else f"o enlace caiu ({e.__class__.__name__})")

    def on_frame(self, wire, kind, cid, seq, body):
        """Um quadro recebido. False quando a sessão acabou (BYE)."""
        if kind in NUMBERED:
            if cid == 0:
                raise _Violation("quadro de canal sem canal")
            if seq != self.recv_seq + 1:
                raise _Violation(f"quadro número {seq} fora de ordem (esperava {self.recv_seq + 1})")
            self.recv_seq = seq
            self.ack_bytes += _HDR.size + len(body)
            self._schedule_ack()
            if kind == DATA:
                self._on_data(cid, body)
            elif kind == WINDOW:
                self._on_window(cid, body)
            elif kind == OPEN:
                self._on_open(cid, body)
            elif kind == CLOSE:
                self._on_close(cid, body)
            else:
                self._on_reset(cid, body)
            return True
        if cid or body:
            raise _Violation("quadro de controle com canal ou dados")
        if kind == ACK:
            self._on_ack(wire, seq)
        elif kind == PING:
            wire.ctrl.append(_frame(PONG, 0, seq))
            self.wake.set()
        elif kind == PONG:
            if wire.ping_at is not None and seq == wire.ping_id:
                self.rtt = time.perf_counter() - wire.ping_at
                wire.ping_at = None
        elif kind == BYE:
            self.end("o outro lado encerrou a sessão")
            return False
        else:
            raise _Violation("HELLO/WELCOME no meio da sessão")
        return True

    def _channel(self, cid):
        """O canal `cid`, ou None se ele já fechou deste lado (quadro atrasado: ignorado). Canal que nunca foi aberto
        é violação."""
        if cid > self.max_id:
            raise _Violation(f"canal {cid} fora de ordem (nunca foi aberto)")
        return self.channels.get(cid)

    def _on_open(self, cid, body):
        if not self.owner.accepts_open:
            raise _Violation("o trabalhador tentou abrir um canal")
        if body:
            raise _Violation("OPEN com dados")
        if cid != self.max_id + 1:
            raise _Violation(f"canal {cid} fora de ordem (esperava {self.max_id + 1})")
        self.max_id = cid
        if len(self.channels) >= self.owner.max_channels:
            self.owner._note("canais", f"[túnel] canal recusado: teto de {self.owner.max_channels} canais abertos")
            self.number(RESET, cid)
            return
        chan = self.new_channel(cid)
        chan.tasks.append(self.loop.create_task(self.owner._open_target(self, chan)))

    def _on_data(self, cid, body):
        if not body:
            raise _Violation("quadro de dados vazio")
        chan = self._channel(cid)
        if chan is None:
            return
        if chan.peer_eof:
            raise _Violation(f"dados no canal {cid} depois do fim")
        if chan.received + len(body) > chan.recv_limit:
            raise _Violation(f"o canal {cid} passou do crédito ({_pt(chan.received + len(body))} de "
                             f"{_pt(chan.recv_limit)} bytes)")
        chan.received += len(body)
        chan.inbox.append(body)
        chan.inbox_bytes += len(body)
        chan.arrived.set()

    def _on_window(self, cid, body):
        if len(body) != _U64.size:
            raise _Violation("WINDOW com tamanho errado")
        chan = self._channel(cid)
        if chan is None:
            return
        limit = _U64.unpack(body)[0]
        if limit > chan.send_limit:
            chan.send_limit = limit
            if chan.pending or chan.local_eof:
                self.ready_chan(chan)

    def _on_close(self, cid, body):
        if body:
            raise _Violation("CLOSE com dados")
        chan = self._channel(cid)
        if chan is None:
            return
        if chan.peer_eof:
            raise _Violation(f"CLOSE repetido no canal {cid}")
        chan.peer_eof = True
        chan.arrived.set()

    def _on_reset(self, cid, body):
        if body:
            raise _Violation("RESET com dados")
        chan = self._channel(cid)
        if chan is not None:
            self.drop_channel(chan, notify=False)

    def _on_ack(self, wire, upto):
        if upto > wire.written:
            raise _Violation(f"confirmação do quadro {upto}, que ainda não foi mandado")
        if upto <= self.acked:
            return
        self.acked = upto
        while self.unacked and self.unacked[0][0] <= upto:
            self.unacked_bytes -= len(self.unacked.popleft()[1])
        self.wake.set()

    # ------------------------------------------------------------ canais
    def new_channel(self, cid):
        chan = _Channel(cid)
        self.channels[cid] = chan
        self.opened += 1
        return chan

    def start_pumps(self, chan, reader, writer):
        chan.reader, chan.writer = reader, writer
        chan.tasks.append(self.loop.create_task(self._pump_up(chan)))
        chan.tasks.append(self.loop.create_task(self._pump_down(chan)))

    async def _pump_up(self, chan):
        """Conexão local -> túnel. Para de ler quando o canal tem PENDING_MAX esperando (contrapressão)."""
        reader = chan.reader
        try:
            while not chan.done:
                if len(chan.pending) >= PENDING_MAX:
                    chan.room.clear()
                    await chan.room.wait()
                    continue
                data = await reader.read(READ_CHUNK)
                if chan.done:
                    return
                if not data:
                    chan.local_eof = True
                    self.ready_chan(chan)
                    return
                chan.pending += data
                self.ready_chan(chan)
        except (ConnectionError, OSError):
            self.drop_channel(chan, notify=True)

    async def _pump_down(self, chan):
        """Túnel -> conexão local. Conexão local lenta só segura este canal: o crédito acompanha o que ela aceitou."""
        writer = chan.writer
        try:
            while not chan.done:
                if chan.inbox:
                    items, n = list(chan.inbox), chan.inbox_bytes
                    chan.inbox.clear()
                    chan.inbox_bytes = 0
                    writer.writelines(items)
                    await writer.drain()
                    if chan.done:
                        return
                    chan.consumed += n
                    self.grant(chan)
                elif chan.peer_eof:
                    if writer.can_write_eof():
                        writer.write_eof()
                    chan.eof_written = True
                    self._finish_if_done(chan)
                    return
                else:
                    chan.arrived.clear()
                    await chan.arrived.wait()
        except (ConnectionError, OSError):
            self.drop_channel(chan, notify=True)

    def _finish_if_done(self, chan):
        """Os dois sentidos terminaram (CLOSE mandado e recebido): o canal sai e a conexão local fecha."""
        if chan.done or not (chan.close_sent and chan.eof_written):
            return
        self.channels.pop(chan.id, None)
        self.ready.pop(chan.id, None)
        self._release(chan)

    def _release(self, chan):
        chan.done = True
        chan.room.set()
        chan.arrived.set()
        current = asyncio.current_task()
        for t in chan.tasks:
            if t is not current:
                t.cancel()
        if chan.writer is not None:
            self.owner._close_later(chan.writer.transport)

    def drop_channel(self, chan, notify):
        """Canal abortado (erro na conexão local, alvo fora do ar, RESET do outro lado): corta a conexão local e, com
        `notify`, avisa o outro lado (RESET)."""
        if chan.done:
            return
        self.channels.pop(chan.id, None)
        self.ready.pop(chan.id, None)
        chan.done = True
        chan.room.set()
        chan.arrived.set()
        current = asyncio.current_task()
        for t in chan.tasks:
            if t is not current:
                t.cancel()
        if chan.writer is not None:
            _abort(chan.writer.transport)
        if notify:
            self.number(RESET, chan.id)

    def describe(self):
        chans = list(self.channels.values())
        wire = self.wire
        return {"connected": wire is not None and not self.closed, "since": self.since, "session_since": self.started,
                "bytes_in": self.bytes_in, "bytes_out": self.bytes_out,
                "rtt_ms": None if self.rtt is None else round(self.rtt * 1000, 3),
                "channels": len(chans), "opened": self.opened, "resumes": self.resumes, "port": self.port,
                "pending": sum(len(c.pending) for c in chans), "buffered": sum(c.inbox_bytes for c in chans),
                "unacked": self.unacked_bytes}


# ------------------------------------------------------------------------------ base: laço numa thread
class _Base:
    thread_name = "sigma-tunel"
    accepts_open = False

    def __init__(self, log, grace, ping_every, dead_after, handshake_timeout, max_channels, inflight_max):
        if not AVAILABLE:
            raise TunnelError(UNAVAILABLE)
        self.log = log or (lambda msg: None)
        self.grace, self.ping_every, self.dead_after = float(grace), float(ping_every), float(dead_after)
        self.handshake_timeout = float(handshake_timeout)
        self.max_channels, self.inflight_max = int(max_channels), int(inflight_max)
        self._loop = None
        self._thread = None
        self._ready = None
        self._stop_ev = None
        self._stopping = False
        self._start_error = None
        self._lingering = set()
        self._noted = {}

    # ------------------------------------------------------------ controle
    def start(self):
        if self._thread is not None:
            return self
        self._start_error, self._stopping = None, False
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, name=self.thread_name, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._start_error is not None:
            self._thread.join(10)
            self._thread = None
            raise TunnelError(f"não consegui abrir o túnel: {self._start_error}")
        return self

    def stop(self):
        """Fecha tudo (conexões locais, enlace, pontes, porta) e espera a thread do laço terminar."""
        thread = self._thread
        if thread is None:
            return
        if threading.current_thread() is thread:   # chamado de dentro do laço: só pede
            self._loop.call_soon(self._stop_ev.set)
            return
        try:
            self._loop.call_soon_threadsafe(self._stop_ev.set)
        except RuntimeError:   # o laço já fechou
            pass
        thread.join(30)
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        loop.set_exception_handler(self._loop_error)
        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            if self._start_error is None:
                self._start_error = e
        finally:
            try:
                tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in tasks:
                    t.cancel()
                if tasks:
                    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())   # threads do getaddrinfo
            finally:
                loop.close()
                self._ready.set()

    async def _main(self):
        self._stop_ev = asyncio.Event()
        try:
            await self._open()
        except (OSError, TunnelError) as e:
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()
        tasks = [asyncio.create_task(c) for c in self._background()]
        await self._stop_ev.wait()
        self._stopping = True
        try:
            await self._shutdown()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for t in list(self._lingering):
                _abort(t)
            self._lingering.clear()
            for _ in range(3):   # deixa os transportes cortados soltarem os sockets
                await asyncio.sleep(0)

    async def _open(self):
        pass

    def _background(self):
        return []

    async def _shutdown(self):
        pass

    def _loop_error(self, loop, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionError, ssl.SSLError, asyncio.CancelledError)):
            return
        self._note(f"interno:{type(exc).__name__}", f"[túnel] erro interno: {context.get('message')} {exc!r}", 10)

    # ------------------------------------------------------------ utilidades da sessão
    def _note(self, key, msg, every=60):
        """Registro sem enxurrada: a mesma chave no máximo uma vez a cada `every` s."""
        now = time.monotonic()
        if now - self._noted.get(key, -1e9) < every:
            return
        if len(self._noted) > 2000:
            self._noted.clear()
        self._noted[key] = now
        self.log(msg)

    def _close_later(self, transport):
        """Fecha de forma limpa (esvazia e manda FIN); o que não fechar em LINGER s é cortado."""
        if transport is None:
            return
        if self._stopping:
            _abort(transport)
            return
        try:
            transport.close()
        except Exception:
            _abort(transport)
            return
        self._lingering.add(transport)
        self._loop.call_later(LINGER, self._linger_done, transport)

    def _linger_done(self, transport):
        self._lingering.discard(transport)
        _abort(transport)

    def _violation(self, sess, msg):
        self.log(f"[túnel] {_short(sess.peer)}: {msg}; túnel derrubado")
        sess.end(msg, bye=True)

    def _wire_down(self, sess, why):
        if not self._stopping:
            self.log(f"[túnel] enlace com {_short(sess.peer)} caiu ({why}); as conexões locais esperam até "
                     f"{_pt(self.grace)} s pela volta")

    def _session_ended(self, sess, why):
        pass

    async def _open_target(self, sess, chan):
        sess.drop_channel(chan, notify=True)


# ------------------------------------------------------------------------------------------- coordenador
class _Peer:
    """Quem está do outro lado de um handshake: o callback da PSK só conhece a identidade, então cada conexão tem um
    contexto TLS próprio que anota aqui quem se apresentou."""

    def __init__(self):
        self.claimed = None      # identidade apresentada no ClientHello
        self.identity = None     # endereço para o qual havia PSK
        self.psk = None
        self.bad = False


class _Gate(asyncio.Protocol):
    """Conexão de fora recém-aceita: segura a leitura até o coordenador decidir se faz o handshake."""

    def __init__(self, hub):
        self.hub = hub

    def connection_made(self, transport):
        _nodelay(transport)
        transport.pause_reading()
        self.hub._accepted(transport)

    def data_received(self, data):
        pass

    def connection_lost(self, exc):
        pass


class Hub(_Base):
    """Lado do coordenador: escuta TLS 1.3 com PSK e dá a cada trabalhador autorizado uma ponte em 127.0.0.1.

    `peers(endereço)` devolve a chave pública (hex) do trabalhador autorizado, ou None (mais tarde vem da chain).
    É chamada no laço do túnel a cada handshake e a cada AUTH_EVERY s: precisa ser rápida (tabela em memória).
    `bridge_pids()`, se dada, devolve os PIDs que podem usar as pontes (o llama-server que o coordenador iniciou);
    conexão de outro processo, ou de dono desconhecido, é recusada."""
    thread_name = "sigma-tunel-hub"

    def __init__(self, machine_priv, listen, peers, log=print, *, bridge_pids=None, grace=GRACE,
                 ping_every=PING_EVERY, dead_after=DEAD_AFTER, handshake_timeout=HANDSHAKE_TIMEOUT,
                 max_channels=MAX_CHANNELS, max_handshakes=MAX_HANDSHAKES,
                 max_handshakes_per_ip=MAX_HANDSHAKES_PER_IP, fails_per_ip=FAILS_PER_IP, ban_for=BAN_FOR,
                 auth_every=AUTH_EVERY, inflight_max=INFLIGHT_MAX):
        super().__init__(log, grace, ping_every, dead_after, handshake_timeout, max_channels, inflight_max)
        self.priv = _priv(machine_priv)
        self.address = address_of(pub_of(self.priv))
        self.listen = _hostport(listen, "endereço de escuta")
        self.peers = peers
        self.bridge_pids = bridge_pids
        self.max_handshakes, self.max_per_ip = int(max_handshakes), int(max_handshakes_per_ip)
        self.fails_per_ip, self.ban_for, self.auth_every = int(fails_per_ip), float(ban_for), float(auth_every)
        self.port = None
        self.refused = 0
        self._server = None
        self._sessions = {}          # endereço do trabalhador -> _Session (uma por trabalhador)
        self._by_id = {}             # id da sessão -> _Session
        self._psk_cache = {}         # endereço -> (chave pública, PSK): o ECDH em Python puro custa ~5 ms
        self._last_port = {}         # endereço -> porta da última ponte (tenta a mesma de novo)
        self._hs_total = 0
        self._hs_ip = collections.Counter()
        self._hs_tasks = set()
        self._fails = {}             # IP -> horários das recusas recentes
        self._banned = {}            # IP -> até quando fica ignorado

    # ------------------------------------------------------------ consulta (de qualquer thread)
    def bridge_port(self, worker):
        """Porta da ponte do trabalhador em 127.0.0.1 (o llama-server usa --rpc 127.0.0.1:<porta>), ou None se ele
        não tem sessão. Durante a carência (enlace caído) a ponte continua aberta."""
        try:
            addr = norm_address(worker)
        except ValueError:
            return None
        sess = self._sessions.get(addr)
        return sess.port if sess is not None and not sess.closed else None

    def wait_bridge(self, worker, timeout=10.0):
        """Espera o trabalhador conectar; devolve a porta da ponte (ou None no fim do prazo)."""
        end = time.monotonic() + timeout
        while True:
            port = self.bridge_port(worker)
            if port and self.status_of(worker).get("connected"):
                return port
            if time.monotonic() >= end:
                return None
            time.sleep(0.02)

    def status_of(self, worker):
        try:
            sess = self._sessions.get(norm_address(worker))
        except ValueError:
            sess = None
        return sess.describe() if sess is not None else {"connected": False}

    def status(self):
        """{"address", "listen", "port", "running", "handshakes", "refused", "workers": {endereço: {"connected",
        "since" (desde quando conectado, ou caído), "session_since", "bytes_in", "bytes_out", "rtt_ms",
        "channels", "opened", "resumes", "port", "pending", "buffered", "unacked"}}}"""
        sessions = list(self._sessions.items())
        return {"address": self.address, "listen": f"{self.listen[0]}:{self.port or self.listen[1]}",
                "port": self.port, "running": self.running, "handshakes": self._hs_total, "refused": self.refused,
                "workers": {addr: sess.describe() for addr, sess in sessions}}

    # ------------------------------------------------------------ laço
    async def _open(self):
        self._server = await self._loop.create_server(lambda: _Gate(self), self.listen[0], self.listen[1],
                                                      backlog=128)
        self.port = self._server.sockets[0].getsockname()[1]
        self.log(f"[túnel] coordenador {_short(self.address)} esperando trabalhadores em "
                 f"{self.listen[0]}:{self.port}")

    def _background(self):
        return [self._tick_loop()]

    async def _tick_loop(self):
        last_auth = time.monotonic()
        while True:
            await asyncio.sleep(self.ping_every)
            now = time.monotonic()
            for sess in list(self._sessions.values()):
                sess.tick(now)
            if now - last_auth >= self.auth_every:
                last_auth = now
                for addr, sess in list(self._sessions.items()):
                    if not sess.closed and self._psk_for(addr) != sess.psk:
                        self.log(f"[túnel] trabalhador {_short(addr)} não está mais autorizado; túnel fechado")
                        sess.end("o trabalhador não está mais autorizado", bye=True)
            for key in [k for k, q in self._fails.items() if not q or now - q[-1] > FAIL_WINDOW]:
                del self._fails[key]
            for key in [k for k, until in self._banned.items() if until <= now]:
                del self._banned[key]

    async def _shutdown(self):
        if self._server is not None:
            self._server.close()
        for task in list(self._hs_tasks):
            task.cancel()
        for sess in list(self._sessions.values()):
            sess.end("o coordenador foi desligado", bye=True)
        await asyncio.sleep(0.05)   # deixa o BYE sair antes de cortar
        self.log(f"[túnel] coordenador {_short(self.address)} desligado")

    # ------------------------------------------------------------ handshake
    def _psk_for(self, addr):
        """PSK do trabalhador `addr`, se `peers` o autoriza; None se não (ou se `peers` falha)."""
        try:
            pub = self.peers(addr)
        except Exception as e:
            self._note("peers", f"[túnel] a consulta de trabalhadores autorizados falhou: {e}")
            return None
        if not pub or not isinstance(pub, str):
            return None
        cached = self._psk_cache.get(addr)
        if cached is not None and cached[0] == pub:
            return cached[1]
        try:
            if address_of(pub) != addr:
                self._note(f"pub:{addr}", f"[túnel] a chave pública de {_short(addr)} não é desse endereço: recusado")
                return None
            psk = tunnel_psk(self.priv, pub, self.address, addr)
        except (ValueError, TypeError):
            return None
        self._psk_cache[addr] = (pub, psk)
        return psk

    def _context_for(self, peer):
        def psk_for(identity):
            try:
                if peer.claimed is not None and identity != peer.claimed:   # trocou de identidade no meio: recusa
                    peer.bad = True
                peer.claimed = identity
                if peer.bad or not isinstance(identity, str) or len(identity) > 64:
                    return b""
                addr = norm_address(identity)
                if addr != identity:   # só a forma canônica ("sig" + 40 hex minúsculos)
                    return b""
                psk = self._psk_for(addr)
                if not psk:
                    return b""
                peer.identity, peer.psk = addr, psk
                return psk
            except Exception:
                return b""
        return _server_context(psk_for)

    def _accepted(self, transport):
        peer = transport.get_extra_info("peername")
        key = _ip_key(peer[0] if peer else "?")
        now = time.monotonic()
        why = None
        if self._stopping:
            why = "o coordenador está desligando"
        elif self._banned.get(key, 0) > now:
            why = "recusas demais deste IP"
        elif self._hs_total >= self.max_handshakes:
            why = f"{self.max_handshakes} handshakes em andamento"
        elif self._hs_ip[key] >= self.max_per_ip:
            why = f"{self.max_per_ip} handshakes em andamento deste IP"
        if why:
            self.refused += 1
            self._note(f"limite:{key}", f"[túnel] conexão de {key} recusada: {why}")
            _abort(transport)
            return
        self._hs_total += 1
        self._hs_ip[key] += 1
        task = self._loop.create_task(self._handshake(transport, key))
        self._hs_tasks.add(task)
        task.add_done_callback(self._hs_tasks.discard)

    async def _handshake(self, transport, key):
        loop = self._loop
        peer = _Peer()
        reader = asyncio.StreamReader(limit=_READ_LIMIT, loop=loop)
        proto = asyncio.StreamReaderProtocol(reader, loop=loop)
        writer = None
        try:
            try:
                async with asyncio.timeout(self.handshake_timeout):
                    tls = await loop.start_tls(transport, proto, self._context_for(peer), server_side=True,
                                               ssl_handshake_timeout=self.handshake_timeout)
                    proto.connection_made(tls)
                    writer = asyncio.StreamWriter(tls, proto, reader, loop)
                    if peer.identity is None or peer.bad:
                        raise _Refused("identidade sem chave")
                    kind, cid, seq, size = _HDR.unpack(await reader.readexactly(_HDR.size))
                    if kind != HELLO or cid or size != _HELLO.size:
                        raise _Refused("o primeiro quadro não é HELLO")
                    version, sid = _HELLO.unpack(await reader.readexactly(size))
                    if version != VERSION:
                        raise _Refused(f"túnel versão {version} (este coordenador fala a {VERSION})")
            finally:
                self._hs_total -= 1
                self._hs_ip[key] -= 1
                if self._hs_ip[key] <= 0:
                    del self._hs_ip[key]
        except (TimeoutError, EOFError, OSError, _Refused) as e:
            self._failed(key, peer, e)
            _abort(writer.transport if writer is not None else transport)
            return
        await self._welcome(peer, reader, writer, sid, seq)

    def _failed(self, key, peer, exc):
        self.refused += 1
        now = time.monotonic()
        q = self._fails.setdefault(key, collections.deque())
        q.append(now)
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()
        if len(q) >= self.fails_per_ip and self._banned.get(key, 0) <= now:
            self._banned[key] = now + self.ban_for
            self.log(f"[túnel] {key}: {len(q)} conexões recusadas em {FAIL_WINDOW} s; IP ignorado por "
                     f"{_pt(self.ban_for)} s")
        if len(self._fails) > 10_000:   # inundação de muitos IPs: esquece os mais antigos
            for k in list(self._fails)[:5000]:
                del self._fails[k]
        self._note(f"recusa:{key}", f"[túnel] conexão de {key} recusada: {self._reason(peer, exc)}")

    @staticmethod
    def _reason(peer, exc):
        if isinstance(exc, _Refused) and str(exc) != "identidade sem chave":
            return str(exc)
        if isinstance(exc, TimeoutError):
            return "o handshake passou do prazo"
        if getattr(exc, "reason", None) == "BINDER_DOES_NOT_VERIFY":
            return f"chave errada para {_short(peer.identity or peer.claimed)}"
        if peer.claimed is not None and peer.identity is None:
            return f"identidade desconhecida ({str(peer.claimed)[:48]!r})"
        return "não é o TLS do túnel"

    async def _welcome(self, peer, reader, writer, sid, peer_recv):
        identity = peer.identity
        wire = _Wire(reader, writer)
        sess = self._by_id.get(sid) if sid != NO_SESSION else None
        if self._stopping:
            wire.abort()
            return
        if sess is not None and not sess.closed:
            if sess.peer != identity:   # sessão de outro trabalhador: nada de sequestro
                self.refused += 1
                self.log(f"[túnel] {_short(identity)} tentou retomar a sessão de {_short(sess.peer)}: recusado")
                wire.abort()
                return
            try:
                sess.check_resume(peer_recv)
            except _Violation as e:
                wire.abort()
                self._violation(sess, str(e))
                return
            writer.write(_frame(WELCOME, 0, sess.recv_seq, _WELCOME.pack(VERSION, sess.id, 1)))
            sess.psk = peer.psk
            sess.attach(wire, peer_recv)
            sess.resumes += 1
            self.log(f"[túnel] trabalhador {_short(identity)}: enlace retomado (sessão continua, "
                     f"{len(sess.channels)} canais intactos)")
            return
        new = _Session(self, secrets.token_bytes(16), identity)
        new.psk = peer.psk
        try:
            await self._open_bridge(new)
        except OSError as e:
            self.log(f"[túnel] não consegui abrir a ponte de {_short(identity)}: {e}")
            wire.abort()
            return
        if self._stopping:
            new.server.close()
            wire.abort()
            return
        old = self._sessions.get(identity)
        if old is not None:
            old.end("o trabalhador começou uma sessão nova")
        self._sessions[identity] = new
        self._by_id[new.id] = new
        writer.write(_frame(WELCOME, 0, 0, _WELCOME.pack(VERSION, new.id, 0)))
        new.attach(wire, 0)
        self.log(f"[túnel] trabalhador {_short(identity)} conectado; ponte em 127.0.0.1:{new.port}")

    async def _open_bridge(self, sess):
        cb = functools.partial(self._on_bridge, sess)
        port = self._last_port.get(sess.peer, 0)
        try:
            server = await asyncio.start_server(cb, "127.0.0.1", port)
        except OSError:
            if not port:
                raise
            server = await asyncio.start_server(cb, "127.0.0.1", 0)
        sess.server = server
        sess.port = server.sockets[0].getsockname()[1]

    async def _on_bridge(self, sess, reader, writer):
        """Conexão na ponte (o llama-server): vira um canal do túnel do trabalhador."""
        _nodelay(writer)
        peer = writer.get_extra_info("peername") or ("?", 0)
        why = None
        if sess.closed or self._stopping:
            why = "sessão encerrada"
        elif peer[0] != "127.0.0.1":
            why = f"conexão de {peer[0]}"
        elif len(sess.channels) >= self.max_channels:
            why = f"teto de {self.max_channels} canais"
        elif sess.max_id >= MAX_CHANNEL_ID:
            why = "números de canal esgotados"
        elif self.bridge_pids is not None:
            try:
                allowed = set(self.bridge_pids() or ())
            except Exception:
                allowed = set()
            pid = local_peer_pid(sess.port, peer[1])
            if pid is None or pid not in allowed:
                why = f"processo {pid if pid is not None else 'desconhecido'} não é o llama-server do coordenador"
        if why:
            self._note(f"ponte:{why}", f"[túnel] ponte de {_short(sess.peer)} recusou uma conexão: {why}")
            _abort(writer.transport)
            return
        sess.max_id += 1
        chan = sess.new_channel(sess.max_id)
        sess.number(OPEN, chan.id)
        sess.start_pumps(chan, reader, writer)

    def _session_ended(self, sess, why):
        if self._sessions.get(sess.peer) is sess:
            del self._sessions[sess.peer]
        if self._by_id.get(sess.id) is sess:
            del self._by_id[sess.id]
        if sess.server is not None:
            sess.server.close()
            self._last_port[sess.peer] = sess.port
        if not self._stopping:
            self.log(f"[túnel] trabalhador {_short(sess.peer)}: sessão encerrada ({why}); ponte fechada")


# ------------------------------------------------------------------------------------------- trabalhador
class Link(_Base):
    """Lado do trabalhador: disca para o coordenador (`endpoint`, chave pública `hub_pub` vinda da chain) e liga cada
    canal que ele abre ao `target` local (o porteiro ou o ggml-rpc-server em 127.0.0.1). Redisca sozinho."""
    thread_name = "sigma-tunel-link"
    accepts_open = True

    def __init__(self, machine_priv, endpoint, hub_pub, target, log=print, *, grace=GRACE, ping_every=PING_EVERY,
                 dead_after=DEAD_AFTER, handshake_timeout=HANDSHAKE_TIMEOUT, max_channels=MAX_CHANNELS,
                 retry_min=RETRY_MIN, retry_max=RETRY_MAX, inflight_max=INFLIGHT_MAX):
        super().__init__(log, grace, ping_every, dead_after, handshake_timeout, max_channels, inflight_max)
        self.priv = _priv(machine_priv)
        self.address = address_of(pub_of(self.priv))
        try:
            crypto._point(hub_pub)
            self.hub = address_of(hub_pub)
            self._psk = tunnel_psk(self.priv, hub_pub, self.hub, self.address)
        except (ValueError, TypeError):
            raise TunnelError("chave pública do coordenador inválida") from None
        self.endpoint = _hostport(endpoint, "endereço do coordenador")
        self.target = _hostport(target, "alvo local")
        self.retry_min, self.retry_max = float(retry_min), float(retry_max)
        self.redials = 0
        self.last_error = None
        self._session = None
        self._down_since = time.time()

    def status(self):
        """{"address", "hub", "endpoint", "target", "running", "redials", "last_error", "connected", "since",
        "session_since", "bytes_in", "bytes_out", "rtt_ms", "channels", "opened", "resumes", "pending",
        "buffered", "unacked"}"""
        sess = self._session
        if sess is not None and not sess.closed:
            d = sess.describe()
        else:
            d = {"connected": False, "since": self._down_since, "session_since": None, "bytes_in": 0,
                 "bytes_out": 0, "rtt_ms": None, "channels": 0, "opened": 0, "resumes": 0, "pending": 0,
                 "buffered": 0, "unacked": 0}
        d.pop("port", None)
        d.update(address=self.address, hub=self.hub, endpoint=f"{self.endpoint[0]}:{self.endpoint[1]}",
                 target=f"{self.target[0]}:{self.target[1]}", running=self.running, redials=self.redials,
                 last_error=self.last_error)
        return d

    def wait_connected(self, timeout=10.0):
        end = time.monotonic() + timeout
        while not self.status()["connected"]:
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)
        return True

    def _background(self):
        return [self._dial_loop(), self._tick_loop()]

    async def _tick_loop(self):
        while True:
            await asyncio.sleep(self.ping_every)
            sess = self._session
            if sess is not None:
                sess.tick(time.monotonic())

    async def _dial_loop(self):
        delay = self.retry_min
        while not self._stopping:
            began = time.monotonic()
            try:
                await self._dial()
            except _Refused as e:
                self._failed(str(e))
            except ssl.SSLCertVerificationError:
                self._failed("o coordenador mostrou um certificado em vez da chave do túnel: recusado")
            except ConnectionRefusedError:
                self._failed(f"o coordenador não atende em {self.endpoint[0]}:{self.endpoint[1]}")
            except TimeoutError:
                self._failed("o coordenador não respondeu a tempo")
            except _Violation as e:
                self._failed(f"o coordenador respondeu fora do protocolo ({e})")
            except (EOFError, ConnectionError, ssl.SSLError):
                self._failed("o coordenador recusou o túnel (chave ou identidade não conferem)")
            except OSError as e:
                self._failed(f"não consegui discar para o coordenador: {e}")
            if self._stopping:
                return
            if time.monotonic() - began >= STABLE_AFTER:
                delay = self.retry_min
            try:
                await asyncio.wait_for(self._stop_ev.wait(), delay * random.uniform(0.8, 1.2))
                return
            except TimeoutError:
                pass
            delay = min(delay * 2, self.retry_max)
            self.redials += 1

    def _failed(self, msg):
        if not self._stopping:
            if msg != self.last_error:
                self.log(f"[túnel] {msg}")
            self.last_error = msg

    async def _dial(self):
        host, port = self.endpoint
        ctx = _client_context(self.address, self._psk)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname="", limit=_READ_LIMIT,
                                    ssl_handshake_timeout=self.handshake_timeout), self.handshake_timeout + 1)
        _nodelay(writer)
        try:
            tls = writer.get_extra_info("ssl_object")
            if tls is None or tls.version() != "TLSv1.3" or tls.getpeercert(binary_form=True):
                raise _Refused("o coordenador mostrou um certificado em vez da chave do túnel: recusado")
            sess = self._session if self._session is not None and not self._session.closed else None
            writer.write(_frame(HELLO, 0, sess.recv_seq if sess else 0,
                                _HELLO.pack(VERSION, sess.id if sess else NO_SESSION)))
            await writer.drain()
            async with asyncio.timeout(self.handshake_timeout):
                kind, cid, seq, size = _HDR.unpack(await reader.readexactly(_HDR.size))
                if kind != WELCOME or cid or size != _WELCOME.size:
                    raise _Violation("a resposta ao HELLO não é WELCOME")
                version, sid, flags = _WELCOME.unpack(await reader.readexactly(size))
            if version != VERSION:
                raise _Refused(f"o coordenador fala o túnel versão {version} (este trabalhador fala a {VERSION})")
            if self._stopping:
                raise _Refused("desligando")
            wire = _Wire(reader, writer)
            if flags & 1:
                if sess is None or sid != sess.id or self._session is not sess:
                    raise _Violation("o coordenador retomou uma sessão que não é a deste trabalhador")
                try:
                    sess.attach(wire, seq)
                except _Violation as e:
                    self._violation(sess, str(e))
                    raise
                sess.resumes += 1
                self.log(f"[túnel] enlace com o coordenador retomado ({len(sess.channels)} canais intactos)")
            else:
                if sess is not None:
                    sess.end("o coordenador começou uma sessão nova")
                sess = self._session = _Session(self, sid, self.hub)
                sess.attach(wire, 0)
                self.log(f"[túnel] conectado ao coordenador {_short(self.hub)} em {host}:{port}")
        except BaseException:
            _abort(writer.transport)
            raise
        self.last_error = None
        await wire.ended.wait()
        self._down_since = time.time()

    async def _open_target(self, sess, chan):
        host, port = self.target
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
            _nodelay(writer)
        except (OSError, TimeoutError) as e:
            self._note("alvo", f"[túnel] não consegui conectar no alvo {host}:{port}: {e}")
            sess.drop_channel(chan, notify=True)
            return
        if chan.done or sess.closed:
            _abort(writer.transport)
            return
        sess.start_pumps(chan, reader, writer)

    async def _shutdown(self):
        sess = self._session
        if sess is not None:
            sess.end("o trabalhador foi desligado", bye=True)
        await asyncio.sleep(0.05)   # deixa o BYE sair antes de cortar
        self.log("[túnel] trabalhador desligado")

    def _session_ended(self, sess, why):
        if self._session is sess:
            self._session = None
            self._down_since = time.time()
        if not self._stopping:
            self.log(f"[túnel] sessão com o coordenador encerrada ({why}); conexões locais fechadas")
