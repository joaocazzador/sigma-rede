#!/usr/bin/env python3
"""netem.py - proxy TCP com atraso configuravel (asyncio, so biblioteca padrao).

Fica entre o llama-server e cada ggml-rpc-server. Para cada direcao aplica um atraso
de ida D (RTT = 2*D), opcionalmente uma banda maxima, preserva a ordem dos bytes e conta
bytes, leituras TCP, mensagens RPC por comando e respostas por comando.

Enquadramento (ggml/src/ggml-rpc/ggml-rpc.cpp, tag b11721, protocolo 8.0.0):
  cliente->servidor: | cmd (1 byte) | tamanho (uint64) | dados |
  servidor->cliente (so para comandos com resposta): | tamanho (uint64) | dados |

Uso:
  python netem.py --map nome:porta_escuta:host:porta_destino [--map ...] --ctl PORTA --log arquivo.jsonl
Controle (HTTP em 127.0.0.1:PORTA):
  /stats                         contadores acumulados (JSON)
  /set?delay_ms=25[&link=a][&bw_c2s=50][&bw_s2c=50]   atraso de ida em ms; banda em Mbit/s (0 = sem limite)
  /log?on=0|1                    liga/desliga o log de eventos por mensagem
  /mark?label=x                  escreve um marcador no log
  /quit                          encerra
"""
import argparse
import asyncio
import ctypes
import json
import os
import socket
import struct
import sys
import time
import urllib.parse

CMD_NAMES = [
    "ALLOC_BUFFER", "GET_ALIGNMENT", "GET_MAX_SIZE", "BUFFER_GET_BASE", "FREE_BUFFER", "BUFFER_CLEAR",
    "SET_TENSOR", "SET_TENSOR_HASH", "GET_TENSOR", "COPY_TENSOR", "GRAPH_COMPUTE", "GET_DEVICE_MEMORY",
    "INIT_TENSOR", "GET_ALLOC_SIZE", "HELLO", "DEVICE_COUNT", "GRAPH_RECOMPUTE", "MEMSET_TENSOR",
    "SET_TENSOR_2D", "GET_TENSOR_2D", "COMM_INIT", "COMM_ALLREDUCE", "COMM_FREE", "NONE",
]
# comandos para os quais o servidor manda resposta (rpc_serve_client em b11721)
HAS_RESPONSE = {
    "HELLO", "DEVICE_COUNT", "ALLOC_BUFFER", "GET_ALLOC_SIZE", "GET_ALIGNMENT", "GET_MAX_SIZE",
    "BUFFER_GET_BASE", "GET_TENSOR_2D", "SET_TENSOR_HASH", "GET_TENSOR", "COPY_TENSOR", "COMM_INIT",
    "GET_DEVICE_MEMORY",
}
RPC_TENSOR_SIZE = 296          # sizeof(rpc_tensor) com GGML_MAX_SRC=10, GGML_MAX_NAME=64
NAME_OFF = 228                 # deslocamento de rpc_tensor.name
HDR_CAP = 320                  # quantos bytes do inicio de cada mensagem guardamos para decodificar
READ_SIZE = 256 * 1024
Q_HIGH = 32 * 1024 * 1024      # pausa a leitura se houver mais que isso esperando o atraso
Q_LOW = 8 * 1024 * 1024
BW_PIECE = 64 * 1024


def win_timer_setup():
    """Pede resolucao de 1 ms ao Windows e prioridade um pouco acima do normal."""
    info = {}
    if os.name != "nt":
        return info
    try:
        info["timeBeginPeriod"] = ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception as e:  # noqa
        info["timeBeginPeriod_err"] = repr(e)
    try:
        # PROCESS_POWER_THROTTLING_STATE: Version=1, ControlMask, StateMask
        class PPTS(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]
        st = PPTS(1, 0x1 | 0x4, 0)  # EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION, estado 0 = sem estrangulamento
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        h = k32.GetCurrentProcess()
        info["SetProcessInformation"] = k32.SetProcessInformation(ctypes.c_void_p(h), 4, ctypes.byref(st), ctypes.sizeof(st))
        info["SetPriorityClass"] = k32.SetPriorityClass(ctypes.c_void_p(h), 0x8000)  # ABOVE_NORMAL
    except Exception as e:  # noqa
        info["throttle_err"] = repr(e)
    return info


class EventLog:
    def __init__(self, path):
        self.path = path
        self.on = bool(path)
        self.f = open(path, "a", buffering=1024 * 1024) if path else None
        self.t0 = time.monotonic()

    def write(self, rec):
        if self.f and self.on:
            self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def flush(self):
        if self.f:
            self.f.flush()


def new_dir_stats():
    return {"bytes": 0, "chunks": 0, "msgs": 0, "bursts": 0}


class Link:
    def __init__(self, name, listen_port, up_host, up_port):
        self.name = name
        self.listen_port = listen_port
        self.up_host = up_host
        self.up_port = up_port
        self.delay = 0.0          # atraso de ida (s)
        self.bw = {"c2s": 0.0, "s2c": 0.0}   # bit/s, 0 = sem limite
        self.next_free = {"c2s": 0.0, "s2c": 0.0}
        self.conns_total = 0
        self.conns_active = 0
        self.d = {"c2s": new_dir_stats(), "s2c": new_dir_stats()}
        self.cmd = {}
        self.rsp = {}
        self.late = {"n": 0, "sum_ms": 0.0, "max_ms": 0.0, "gt1ms": 0, "gt2ms": 0, "gt5ms": 0}
        self.errors = []

    def stats(self):
        return {
            "name": self.name, "listen": self.listen_port, "upstream": f"{self.up_host}:{self.up_port}",
            "delay_ms": round(self.delay * 1000, 3),
            "bw_c2s_mbit": self.bw["c2s"] / 1e6, "bw_s2c_mbit": self.bw["s2c"] / 1e6,
            "conns_total": self.conns_total, "conns_active": self.conns_active,
            "c2s": dict(self.d["c2s"]), "s2c": dict(self.d["s2c"]),
            "cmd": {k: dict(v) for k, v in self.cmd.items()},
            "rsp": {k: dict(v) for k, v in self.rsp.items()},
            "late": dict(self.late), "errors": self.errors[-10:],
        }


class Conn:
    """Uma conexao cliente <-> servidor passando pelo proxy."""
    _ids = 0

    def __init__(self, link, log):
        Conn._ids += 1
        self.id = Conn._ids
        self.link = link
        self.log = log
        self.loop = asyncio.get_running_loop()
        self.t_conn = self.loop.time()
        self.d_at_conn = link.delay
        self.q = {"c2s": asyncio.Queue(), "s2c": asyncio.Queue()}
        self.queued = {"c2s": 0, "s2c": 0}
        self.resume = {"c2s": asyncio.Event(), "s2c": asyncio.Event()}
        self.last_deliver = {"c2s": 0.0, "s2c": 0.0}
        self.last_dir = None
        # estado do decodificador cliente->servidor
        self.c_state = "cmd"
        self.c_need = 1
        self.c_buf = bytearray()
        self.c_cmd = None
        self.c_size = 0
        self.c_left = 0
        self.c_hdr = bytearray()
        self.c_t_first = 0.0
        # estado do decodificador servidor->cliente
        self.s_state = "size"
        self.s_buf = bytearray()
        self.s_left = 0
        self.s_cur = None
        self.pending = []      # fila FIFO de comandos que esperam resposta
        self.proto_ok = True
        self.blocked = False   # COMM_INIT bloqueado

    # ---------- agendamento ----------
    def schedule(self, direction, data, t_arr):
        """Calcula quando cada pedaco deve ser entregue e coloca na fila."""
        link = self.link
        D = link.delay
        bw = link.bw[direction]
        items = []
        if bw > 0:
            for i in range(0, len(data), BW_PIECE):
                piece = data[i:i + BW_PIECE]
                start = max(t_arr, link.next_free[direction])
                end = start + len(piece) * 8.0 / bw
                link.next_free[direction] = end
                items.append((end + D, piece))
        else:
            items.append((t_arr + D, data))
        out = []
        for at, piece in items:
            if direction == "c2s":
                # emula o aperto de mao TCP: o cliente so poderia mandar algo 1 RTT depois do connect
                at = max(at, self.t_conn + 3 * self.d_at_conn)
            at = max(at, self.last_deliver[direction])
            self.last_deliver[direction] = at
            out.append((at, piece))
        return out

    # ---------- decodificacao ----------
    def decode_c2s(self, data, t_arr, t_fwd):
        mv = memoryview(data)
        i = 0
        n = len(mv)
        while i < n and self.proto_ok:
            if self.c_state == "cmd":
                self.c_cmd = mv[i]
                i += 1
                self.c_state = "size"
                self.c_buf = bytearray()
                self.c_t_first = t_arr
            elif self.c_state == "size":
                take = min(8 - len(self.c_buf), n - i)
                self.c_buf += mv[i:i + take]
                i += take
                if len(self.c_buf) == 8:
                    self.c_size = struct.unpack("<Q", self.c_buf)[0]
                    self.c_left = self.c_size
                    self.c_hdr = bytearray()
                    if self.c_cmd >= len(CMD_NAMES):
                        self.proto_ok = False
                        self.link.errors.append(f"conn {self.id}: comando desconhecido {self.c_cmd}")
                        return
                    self.c_state = "payload"
                    if self.c_left == 0:
                        self.finish_c2s(t_arr, t_fwd)
            else:
                take = min(self.c_left, n - i)
                if len(self.c_hdr) < HDR_CAP:
                    h = min(HDR_CAP - len(self.c_hdr), take)
                    self.c_hdr += mv[i:i + h]
                i += take
                self.c_left -= take
                if self.c_left == 0:
                    self.finish_c2s(t_arr, t_fwd)

    def finish_c2s(self, t_arr, t_fwd):
        name = CMD_NAMES[self.c_cmd]
        frame = 9 + self.c_size
        st = self.link.cmd.setdefault(name, {"n": 0, "bytes": 0})
        st["n"] += 1
        st["bytes"] += frame
        self.link.d["c2s"]["msgs"] += 1
        h = bytes(self.c_hdr)
        rec = {"t": round(t_arr - self.log.t0, 6), "ln": self.link.name, "c": self.id, "d": "c2s",
               "cmd": name, "len": frame}
        try:
            if name in ("SET_TENSOR", "GET_TENSOR", "SET_TENSOR_HASH", "INIT_TENSOR", "MEMSET_TENSOR",
                        "SET_TENSOR_2D", "GET_TENSOR_2D") and len(h) >= RPC_TENSOR_SIZE:
                rec["name"] = h[NAME_OFF:NAME_OFF + 64].split(b"\0", 1)[0].decode("utf-8", "replace")
                rec["type"] = struct.unpack_from("<I", h, 8)[0]
                if name == "SET_TENSOR":
                    rec["data"] = self.c_size - RPC_TENSOR_SIZE - 9
                    rec["cache_flag"] = h[RPC_TENSOR_SIZE]
                elif name == "GET_TENSOR" and len(h) >= RPC_TENSOR_SIZE + 16:
                    rec["want"] = struct.unpack_from("<Q", h, RPC_TENSOR_SIZE + 8)[0]
            elif name == "GET_ALLOC_SIZE" and len(h) >= 4 + RPC_TENSOR_SIZE:
                rec["name"] = h[4 + NAME_OFF:4 + NAME_OFF + 64].split(b"\0", 1)[0].decode("utf-8", "replace")
            elif name == "GRAPH_COMPUTE" and len(h) >= 16:
                dev, uid, nn = struct.unpack_from("<IQI", h, 0)
                rec["uid"] = uid
                rec["n_nodes"] = nn
                rec["dev"] = dev
            elif name == "GRAPH_RECOMPUTE" and len(h) >= 12:
                dev, uid = struct.unpack_from("<IQ", h, 0)
                rec["uid"] = uid
                rec["dev"] = dev
            elif name == "ALLOC_BUFFER" and len(h) >= 12:
                dev, size = struct.unpack_from("<IQ", h, 0)
                rec["size"] = size
        except Exception as e:  # noqa
            rec["decode_err"] = repr(e)
        self.log.write(rec)
        if name in HAS_RESPONSE:
            self.pending.append((name, t_fwd, rec.get("name")))
        self.c_state = "cmd"

    def decode_s2c(self, data, t_arr):
        mv = memoryview(data)
        i = 0
        n = len(mv)
        while i < n and self.proto_ok:
            if self.s_state == "size":
                take = min(8 - len(self.s_buf), n - i)
                if not self.s_buf:
                    self.s_t_first = t_arr
                self.s_buf += mv[i:i + take]
                i += take
                if len(self.s_buf) == 8:
                    self.s_left = struct.unpack("<Q", self.s_buf)[0]
                    self.s_buf = bytearray()
                    if not self.pending:
                        self.proto_ok = False
                        self.link.errors.append(f"conn {self.id}: resposta sem pedido")
                        return
                    self.s_cur = self.pending.pop(0) + (self.s_left, self.s_t_first)
                    self.s_state = "payload"
                    if self.s_left == 0:
                        self.finish_s2c(t_arr)
            else:
                take = min(self.s_left, n - i)
                i += take
                self.s_left -= take
                if self.s_left == 0:
                    self.finish_s2c(t_arr)

    def finish_s2c(self, t_arr):
        cmd, t_fwd, tname, size, t_first = self.s_cur
        st = self.link.rsp.setdefault(cmd, {"n": 0, "bytes": 0, "svc_ms_sum": 0.0})
        st["n"] += 1
        st["bytes"] += 8 + size
        svc = (t_first - t_fwd) * 1000.0
        st["svc_ms_sum"] += svc
        self.link.d["s2c"]["msgs"] += 1
        rec = {"t": round(t_arr - self.log.t0, 6), "ln": self.link.name, "c": self.id, "d": "s2c",
               "cmd": cmd, "len": 8 + size, "svc_ms": round(svc, 3)}
        if tname:
            rec["name"] = tname
        self.log.write(rec)
        self.s_state = "size"

    # ---------- bombas ----------
    async def pump_in(self, reader, direction, peer_writer_closed):
        link = self.link
        other = "s2c" if direction == "c2s" else "c2s"
        try:
            while True:
                if self.queued[direction] > Q_HIGH:
                    self.resume[direction].clear()
                    await self.resume[direction].wait()
                data = await reader.read(READ_SIZE)
                t_arr = self.loop.time()
                if not data:
                    break
                ds = link.d[direction]
                ds["bytes"] += len(data)
                ds["chunks"] += 1
                if self.last_dir != direction:
                    ds["bursts"] += 1
                    self.last_dir = direction
                items = self.schedule(direction, data, t_arr)
                if direction == "c2s":
                    if self.blocked:
                        continue
                    self.decode_c2s(data, t_arr, items[-1][0])
                    if self.proto_ok and "COMM_INIT" in link.cmd and link.cmd["COMM_INIT"]["n"] > 0:
                        # trava de seguranca: COMM_INIT faria o servidor escutar em 0.0.0.0:porta+1000
                        self.blocked = True
                        link.errors.append(f"conn {self.id}: COMM_INIT bloqueado")
                        self.q[direction].put_nowait(None)
                        break
                else:
                    self.decode_s2c(data, t_arr)
                for at, piece in items:
                    self.queued[direction] += len(piece)
                    self.q[direction].put_nowait((at, piece))
        except (ConnectionError, OSError):
            pass
        finally:
            self.q[direction].put_nowait(None)

    async def pump_out(self, writer, direction):
        link = self.link
        late = link.late
        q = self.q[direction]
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                at, piece = item
                now = self.loop.time()
                if at > now:
                    await asyncio.sleep(at - now)
                lt = (self.loop.time() - at) * 1000.0
                late["n"] += 1
                late["sum_ms"] += lt
                if lt > late["max_ms"]:
                    late["max_ms"] = lt
                if lt > 1:
                    late["gt1ms"] += 1
                if lt > 2:
                    late["gt2ms"] += 1
                if lt > 5:
                    late["gt5ms"] += 1
                writer.write(piece)
                self.queued[direction] -= len(piece)
                if self.queued[direction] < Q_LOW:
                    self.resume[direction].set()
                if writer.transport.get_write_buffer_size() > 4 * 1024 * 1024:
                    await writer.drain()
            try:
                await writer.drain()
                if writer.can_write_eof():
                    writer.write_eof()
            except (ConnectionError, OSError):
                pass
        except (ConnectionError, OSError):
            pass


def set_nodelay(writer):
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


async def handle(link, log, c_reader, c_writer):
    link.conns_total += 1
    link.conns_active += 1
    conn = Conn(link, log)
    set_nodelay(c_writer)
    log.write({"t": round(conn.t_conn - log.t0, 6), "ln": link.name, "c": conn.id, "ev": "open",
               "delay_ms": link.delay * 1000})
    try:
        s_reader, s_writer = await asyncio.open_connection(link.up_host, link.up_port, limit=4 * 1024 * 1024)
    except OSError as e:
        link.errors.append(f"conn {conn.id}: upstream {e!r}")
        link.conns_active -= 1
        c_writer.close()
        return
    set_nodelay(s_writer)
    tasks = [
        asyncio.create_task(conn.pump_in(c_reader, "c2s", None)),
        asyncio.create_task(conn.pump_out(s_writer, "c2s")),
        asyncio.create_task(conn.pump_in(s_reader, "s2c", None)),
        asyncio.create_task(conn.pump_out(c_writer, "s2c")),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    for w in (c_writer, s_writer):
        try:
            w.close()
        except Exception:  # noqa
            pass
    link.conns_active -= 1
    log.write({"t": round(asyncio.get_running_loop().time() - log.t0, 6), "ln": link.name, "c": conn.id,
               "ev": "close", "pending": len(conn.pending), "proto_ok": conn.proto_ok})


async def control(links, log, info, stop, reader, writer):
    try:
        line = (await reader.readline()).decode("latin-1").strip()
        while True:
            h = await reader.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
        parts = line.split(" ")
        url = urllib.parse.urlparse(parts[1] if len(parts) > 1 else "/")
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        body = {}
        if url.path == "/stats":
            body = {"t": time.time(), "mono": asyncio.get_running_loop().time(), "info": info,
                    "links": {n: l.stats() for n, l in links.items()}}
        elif url.path == "/set":
            sel = [links[qs["link"]]] if "link" in qs else list(links.values())
            for l in sel:
                if "delay_ms" in qs:
                    l.delay = float(qs["delay_ms"]) / 1000.0
                if "bw_c2s" in qs:
                    l.bw["c2s"] = float(qs["bw_c2s"]) * 1e6
                    l.next_free["c2s"] = 0.0
                if "bw_s2c" in qs:
                    l.bw["s2c"] = float(qs["bw_s2c"]) * 1e6
                    l.next_free["s2c"] = 0.0
            log.write({"t": round(time.monotonic() - log.t0, 6), "ev": "set", "q": qs})
            body = {n: {"delay_ms": l.delay * 1000, "bw_c2s": l.bw["c2s"] / 1e6, "bw_s2c": l.bw["s2c"] / 1e6}
                    for n, l in links.items()}
        elif url.path == "/log":
            log.on = qs.get("on", "1") == "1"
            body = {"log": log.on}
        elif url.path == "/mark":
            log.write({"t": round(time.monotonic() - log.t0, 6), "ev": "mark", "label": qs.get("label", "")})
            log.flush()
            body = {"ok": True}
        elif url.path == "/quit":
            body = {"ok": True}
            stop.set()
        else:
            body = {"error": "desconhecido"}
        data = json.dumps(body).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                     + str(len(data)).encode() + b"\r\n\r\n" + data)
        await writer.drain()
    except Exception as e:  # noqa
        try:
            writer.write(b"HTTP/1.1 500 ERR\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        except Exception:  # noqa
            pass
        print("erro no controle:", repr(e), file=sys.stderr)
    finally:
        writer.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", action="append", required=True, help="nome:porta_escuta:host:porta_destino")
    ap.add_argument("--ctl", type=int, required=True)
    ap.add_argument("--log", default="")
    ap.add_argument("--delay-ms", type=float, default=0.0)
    a = ap.parse_args()
    info = win_timer_setup()
    log = EventLog(a.log)
    links = {}
    servers = []
    for m in a.map:
        name, lp, host, up = m.split(":")
        link = Link(name, int(lp), host, int(up))
        link.delay = a.delay_ms / 1000.0
        links[name] = link
        srv = await asyncio.start_server(lambda r, w, l=link: handle(l, log, r, w), "127.0.0.1", int(lp),
                                         limit=4 * 1024 * 1024)
        servers.append(srv)
    stop = asyncio.Event()
    ctl = await asyncio.start_server(lambda r, w: control(links, log, info, stop, r, w), "127.0.0.1", a.ctl)
    print(json.dumps({"ready": True, "pid": os.getpid(), "info": info,
                      "links": {n: [l.listen_port, l.up_port] for n, l in links.items()}}), flush=True)

    async def flusher():
        while True:
            await asyncio.sleep(1.0)
            log.flush()
    ft = asyncio.create_task(flusher())
    await stop.wait()
    ft.cancel()
    log.flush()
    for s in servers + [ctl]:
        s.close()


if __name__ == "__main__":
    asyncio.run(main())
