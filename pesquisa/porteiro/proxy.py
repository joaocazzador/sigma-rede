#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""proxy.py - PORTEIRO: proxy validador entre a rede e o ggml-rpc-server local.

So biblioteca padrao (socket, struct, threading, json). Protocolo ggml-rpc 8.0.0 (b11721).

Modos:
  capture  -> encaminha tudo, decodifica e grava os grafos (para aprender os modelos)
  gate     -> valida cada mensagem; recusa (fecha a conexao) ANTES de repassar ao rpc-server

Topologia (sempre loopback): llama-server --rpc 127.0.0.1:<porta_porteiro>
    porteiro escuta em <porta_porteiro>  -> encaminha para 127.0.0.1:<porta_rpc_real>

Defesas no modo gate:
  - HELLO obrigatorio em primeiro lugar, tamanho == 24, versao negociada 8.0.0
  - allowlist de comandos (bloqueia COMM_* e tudo que o Sigma nao usa)
  - teto de tamanho por mensagem e por ALLOC_BUFFER
  - modelo-sombra dos buffers (ALLOC_BUFFER/BUFFER_GET_BASE/FREE_BUFFER) e re-checagem
    de limites por tensor, recomputando ggml_nbytes a partir de ne/nb (igual ao servidor)
  - allowlist de tipos e de ops de tensor
  - validacao do grafo contra os "modelos de grafo" aprendidos (nivel configuravel)
"""
import argparse
import json
import os
import socket
import struct
import sys
import threading
import time

import rpcwire as w

STREAM_THRESHOLD = 1 << 20  # mensagens acima disso sao repassadas em pedacos (sem bufferizar tudo)
CHUNK = 256 * 1024


def now():
    return time.monotonic()


def win_timer():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


class Reject(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class Policy:
    """Regras do modo gate. Carregada de um JSON aprendido (analyze.py) + limites."""
    def __init__(self, d=None):
        d = d or {}
        self.allowed_cmds = set(d.get("allowed_cmds", [
            "HELLO", "DEVICE_COUNT", "GET_DEVICE_MEMORY", "GET_ALIGNMENT", "GET_MAX_SIZE",
            "ALLOC_BUFFER", "GET_ALLOC_SIZE", "BUFFER_GET_BASE", "FREE_BUFFER", "BUFFER_CLEAR",
            "INIT_TENSOR", "SET_TENSOR", "SET_TENSOR_HASH", "GET_TENSOR", "COPY_TENSOR",
            "MEMSET_TENSOR", "SET_TENSOR_2D", "GET_TENSOR_2D", "GRAPH_COMPUTE", "GRAPH_RECOMPUTE",
        ]))
        self.max_msg = int(d.get("max_msg", 192 * 1024 * 1024))
        self.max_alloc = int(d.get("max_alloc", 4 * 1024 * 1024 * 1024))
        self.max_nodes = int(d.get("max_nodes", 8192))
        self.max_tensors = int(d.get("max_tensors", 16384))
        self.allowed_types = set(d.get("allowed_types", [])) or None  # None = aceita qualquer tipo valido
        self.allowed_ops = set(d.get("allowed_ops", [])) or None
        self.graph_level = int(d.get("graph_level", 1))  # 0 so-tensor, 1 struct sem nome, 2 struct com nome
        self.struct_keys = set(tuple(k) if isinstance(k, list) else k for k in d.get("struct_keys", []))
        self.max_ne1 = int(d.get("max_ne1", 4096))  # teto de n_tokens observavel em ne[1]
        # bloqueio explicito sempre:
        self.blocked = {"COMM_INIT", "COMM_ALLREDUCE", "COMM_FREE", "NONE"}


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.msgs = 0
        self.rejects = []
        self.by_cmd = {}
        self.graphs_seen = 0

    def bump(self, cmd):
        with self.lock:
            self.msgs += 1
            self.by_cmd[cmd] = self.by_cmd.get(cmd, 0) + 1

    def reject(self, rec):
        with self.lock:
            self.rejects.append(rec)


class Link:
    def __init__(self, name, listen_port, up_host, up_port):
        self.name = name
        self.listen_port = listen_port
        self.up_host = up_host
        self.up_port = up_port


def recv_exact(sock, n):
    buf = bytearray(n)
    mv = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(mv[got:], n - got)
        if r == 0:
            return None
        got += r
    return bytes(buf)


def stream_copy(src, dst, n, sink=None):
    """Copia n bytes de src para dst em pedacos. sink(bytes) opcional (p/ contagem)."""
    left = n
    while left > 0:
        chunk = src.recv(min(CHUNK, left))
        if not chunk:
            return False
        dst.sendall(chunk)
        if sink:
            sink(chunk)
        left -= len(chunk)
    return True


class Connection:
    def __init__(self, engine, link, csock, upsock, cid):
        self.e = engine
        self.link = link
        self.c = csock          # cliente (llama-server ou atacante)
        self.u = upsock         # upstream (rpc-server real)
        self.cid = cid
        self.buffers = {}       # remote_ptr -> {"size":int,"base":int|None}
        self.hello_done = False
        self.pending_alloc = None  # (device,size) aguardando resposta
        self.pending_getbase = None
        self.msg_i = 0
        self.cmd_count = {}

    def log(self, rec):
        rec["t"] = round(now() - self.e.t0, 6)
        rec["cid"] = self.cid
        rec["link"] = self.link.name
        self.e.log(rec)

    # ---- validacao por tensor (gate) ----
    def check_tensor(self, t, where):
        pol = self.e.policy
        if t["type"] >= w.GGML_TYPE_COUNT or w.BLCK.get(t["type"], 0) == 0:
            raise Reject("tipo_invalido", "%s type=%d" % (where, t["type"]))
        if pol.allowed_types is not None and t["type"] not in pol.allowed_types:
            raise Reject("tipo_fora_do_allowlist", "%s type=%d" % (where, t["type"]))
        if t["op"] >= w.GGML_OP_COUNT:
            raise Reject("op_invalida", "%s op=%d" % (where, t["op"]))
        if pol.allowed_ops is not None and t["op"] not in pol.allowed_ops:
            raise Reject("op_fora_do_allowlist", "%s op=%d" % (where, t["op"]))
        # buffer tem que ser conhecido (ou 0). Igual ao deserialize_tensor do servidor.
        buf = t["buffer"]
        if buf != 0:
            b = self.buffers.get(buf)
            if b is None:
                raise Reject("buffer_desconhecido", "%s buffer=0x%x" % (where, buf))
            if b.get("base") is None:
                # buffer alocado mas sem base ainda: so pode se data==0
                if t["data"] != 0:
                    raise Reject("buffer_sem_base", where)
                return
            nb = w.ggml_nbytes(t["type"], t["ne"], t["nb"])
            if nb is None:
                raise Reject("nbytes_indefinido", where)
            data = t["data"]
            p0 = b["base"]; p1 = b["base"] + b["size"]
            # overflow + dentro do buffer (identico a deserialize_tensor:1676)
            if data + nb < data or data < p0 or data + nb > p1:
                raise Reject("tensor_fora_do_buffer",
                             "%s data=0x%x nb=%d buf=[0x%x,0x%x)" % (where, data, nb, p0, p1))

    def region_check(self, t, offset, size, where):
        """Re-faz a checagem de regiao do servidor (set/get_tensor)."""
        buf = t["buffer"]
        if buf == 0:
            raise Reject("tensor_sem_buffer", where)
        b = self.buffers.get(buf)
        if b is None or b.get("base") is None:
            raise Reject("buffer_desconhecido", where)
        p0 = b["base"]; p1 = b["base"] + b["size"]
        d = t["data"]
        if d + offset < p0 or d + offset >= p1 or size > (p1 - d - offset):
            raise Reject("regiao_fora_do_buffer",
                         "%s data=0x%x off=%d size=%d buf=[0x%x,0x%x)" % (where, d, offset, size, p0, p1))

    def validate_graph(self, data):
        pol = self.e.policy
        try:
            meta, node_ids, tensors = w.parse_graph(data)
        except ValueError as ex:
            raise Reject("grafo_malformado", str(ex))
        if meta["n_nodes"] > pol.max_nodes or meta["n_tensors"] > pol.max_tensors:
            raise Reject("grafo_grande_demais", "n_nodes=%d n_tensors=%d" % (meta["n_nodes"], meta["n_tensors"]))
        # cada tensor: limites por buffer + tipo/op + ne[1] (n_tokens) sob teto
        for t in tensors:
            self.check_tensor(t, "graph")
            if t["ne"][1] > pol.max_ne1:
                raise Reject("ne1_acima_do_teto", "ne[1]=%d > %d" % (t["ne"][1], pol.max_ne1))
        ng = w.normalize_graph(meta, node_ids, tensors, self.buffers)
        # topologia: todo node tem que resolver; todo src/view_src != 0 tem que resolver
        idset = set(i for i, t in enumerate(tensors))
        for nidx in ng["nodes"]:
            if nidx == -1:
                raise Reject("node_sem_tensor", "grafo refere id de node inexistente")
        for t in ng["tensors"]:
            for s in t["src"]:
                if s == -2:
                    raise Reject("src_sem_tensor", "")
        if pol.graph_level >= 1 and pol.struct_keys:
            sk = w.struct_key(ng, with_names=(pol.graph_level >= 2))
            if _hash_key(sk) not in pol.struct_keys:
                raise Reject("grafo_fora_do_modelo", "struct_key desconhecida uid=%d n_nodes=%d" % (meta["uid"], meta["n_nodes"]))
        return ng, meta

    # ---- loop principal da conexao ----
    def run(self):
        c, u = self.c, self.u
        pol = self.e.policy
        gate = self.e.mode == "gate"
        try:
            while True:
                hdr = recv_exact(c, 1)
                if hdr is None:
                    break
                cmd_byte = hdr[0]
                szb = recv_exact(c, 8)
                if szb is None:
                    break
                (size,) = struct.unpack("<Q", szb)
                name = w.CMD_NAME.get(cmd_byte, "UNKNOWN(%d)" % cmd_byte)
                self.msg_i += 1

                # --- validacoes que nao exigem o corpo ---
                if gate:
                    if cmd_byte >= w.RPC_CMD_COUNT:
                        raise Reject("cmd_invalido", "cmd=%d" % cmd_byte)
                    if not self.hello_done and name != "HELLO":
                        raise Reject("sem_hello", "primeiro cmd=%s" % name)
                    if name in pol.blocked or name not in pol.allowed_cmds:
                        raise Reject("cmd_bloqueado", name)
                    if size > pol.max_msg:
                        raise Reject("mensagem_grande_demais", "%s size=%d" % (name, size))
                    exp = w.REQ_SIZE.get(name)
                    if exp is not None and size != exp:
                        raise Reject("tamanho_de_request_errado", "%s size=%d exp=%d" % (name, size, exp))
                    mn = w.REQ_MIN.get(name)
                    if mn is not None and size < mn:
                        raise Reject("request_curto", "%s size=%d min=%d" % (name, size, mn))

                self.e.stats.bump(name)
                self.cmd_count[name] = self.cmd_count.get(name, 0) + 1

                # --- streaming para corpos grandes que nao precisamos inspecionar inteiros ---
                if size > STREAM_THRESHOLD and name in ("SET_TENSOR", "SET_TENSOR_2D"):
                    self.handle_big_set(name, size, gate)
                    continue

                data = recv_exact(c, size) if size else b""
                if size and data is None:
                    break

                ng = None
                if gate:
                    ng = self.validate_request(name, data)

                # repassa a request intacta
                u.sendall(hdr + szb + data)

                # efeitos de buffer no lado da request (FREE/CLEAR nao tem resposta)
                if name == "FREE_BUFFER":
                    (ptr,) = struct.unpack_from("<Q", data, 0)
                    self.buffers.pop(ptr, None)

                if name == "GRAPH_COMPUTE" and ng is not None and self.e.mode == "capture":
                    self.record_graph(ng)
                elif name == "GRAPH_COMPUTE" and self.e.mode == "capture":
                    # capture sem gate: normaliza para registro
                    try:
                        meta, nids, tn = w.parse_graph(data)
                        self.record_graph(w.normalize_graph(meta, nids, tn, self.buffers))
                    except ValueError:
                        pass

                # --- resposta (se houver) ---
                if name in w.HAS_RESPONSE:
                    rszb = recv_exact(u, 8)
                    if rszb is None:
                        break
                    (rsize,) = struct.unpack("<Q", rszb)
                    if rsize > STREAM_THRESHOLD and name in ("GET_TENSOR", "GET_TENSOR_2D"):
                        c.sendall(rszb)
                        if not stream_copy(u, c, rsize):
                            break
                        rdata = None
                    else:
                        rdata = recv_exact(u, rsize)
                        if rdata is None and rsize:
                            break
                        c.sendall(rszb + (rdata or b""))
                    self.on_response(name, data, rdata)
                    if name == "HELLO":
                        self.hello_done = True
        except Reject as rj:
            rec = {"ev": "REJECT", "reason": rj.reason, "detail": rj.detail,
                   "cmd": name if 'name' in dir() else "?", "msg_i": self.msg_i}
            self.log(rec)
            self.e.stats.reject(rec)
        except (ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            if self.cmd_count:
                self.log({"ev": "CONN_CLOSE", "cmds": self.cmd_count})
            for s in (self.c, self.u):
                try:
                    s.close()
                except Exception:
                    pass

    def handle_big_set(self, name, size, gate):
        """SET_TENSOR/SET_TENSOR_2D grandes: valida o cabecalho e repassa o resto em pedacos."""
        c, u = self.c, self.u
        if name == "SET_TENSOR":
            hdr_len = w.RPC_TENSOR_SIZE + 1 + 8
        else:
            hdr_len = w.RPC_TENSOR_SIZE + 32
        head = recv_exact(c, hdr_len)
        if head is None:
            raise Reject("set_truncado", name)
        if gate:
            t = w.parse_tensor(head, 0)
            if name == "SET_TENSOR":
                (offset,) = struct.unpack_from("<Q", head, w.RPC_TENSOR_SIZE + 1)
                payload = size - hdr_len
                self.check_tensor(t, "set_tensor")
                self.region_check(t, offset, payload, "set_tensor")
            else:
                offset, sz, ncopies, stride = struct.unpack_from("<4Q", head, w.RPC_TENSOR_SIZE)
                self.check_tensor(t, "set_tensor_2d")
                if ncopies == 0 or sz == 0:
                    raise Reject("set2d_params", name)
                span = (ncopies - 1) * stride + sz
                self.region_check(t, offset, span, "set_tensor_2d")
        # repassa cabecalho + resto
        u.sendall(struct.pack("<B", w.CMD[name]) + struct.pack("<Q", size) + head)
        if not stream_copy(c, u, size - hdr_len):
            raise Reject("set_stream_interrompido", name)

    def validate_request(self, name, data):
        """Valida requests que dependem do corpo. Retorna grafo normalizado (GRAPH_COMPUTE) ou None."""
        if name == "ALLOC_BUFFER":
            device, bsize = struct.unpack_from("<IQ", data, 0)
            if bsize > self.e.policy.max_alloc:
                raise Reject("alloc_grande_demais", "size=%d" % bsize)
            self.pending_alloc = (device, bsize)
        elif name == "BUFFER_GET_BASE":
            (ptr,) = struct.unpack_from("<Q", data, 0)
            if ptr not in self.buffers:
                raise Reject("get_base_buffer_desconhecido", "0x%x" % ptr)
            self.pending_getbase = ptr
        elif name in ("FREE_BUFFER", "BUFFER_CLEAR"):
            (ptr,) = struct.unpack_from("<Q", data, 0)
            if ptr not in self.buffers:
                raise Reject("buffer_desconhecido", name)
        elif name in ("SET_TENSOR",):
            t = w.parse_tensor(data, 0)
            (offset,) = struct.unpack_from("<Q", data, w.RPC_TENSOR_SIZE + 1)
            payload = len(data) - (w.RPC_TENSOR_SIZE + 1 + 8)
            self.check_tensor(t, "set_tensor")
            self.region_check(t, offset, payload, "set_tensor")
        elif name == "SET_TENSOR_2D":
            t = w.parse_tensor(data, 0)
            offset, sz, ncopies, stride = struct.unpack_from("<4Q", data, w.RPC_TENSOR_SIZE)
            self.check_tensor(t, "set_tensor_2d")
            if ncopies == 0 or sz == 0:
                raise Reject("set2d_params", name)
            span = (ncopies - 1) * stride + sz
            self.region_check(t, offset, span, "set_tensor_2d")
        elif name in ("SET_TENSOR_HASH", "GET_TENSOR", "MEMSET_TENSOR"):
            t = w.parse_tensor(data, 0)
            self.check_tensor(t, name)
            if name == "GET_TENSOR":
                offset, sz = struct.unpack_from("<QQ", data, w.RPC_TENSOR_SIZE)
                self.region_check(t, offset, sz, "get_tensor")
            elif name == "MEMSET_TENSOR":
                offset, sz = struct.unpack_from("<QQ", data, w.RPC_TENSOR_SIZE)
                self.region_check(t, offset, sz, "memset")
        elif name == "GET_TENSOR_2D":
            t = w.parse_tensor(data, 0)
            self.check_tensor(t, "get_tensor_2d")
        elif name == "COPY_TENSOR":
            src = w.parse_tensor(data, 0)
            dst = w.parse_tensor(data, w.RPC_TENSOR_SIZE)
            self.check_tensor(src, "copy_src")
            self.check_tensor(dst, "copy_dst")
        elif name == "GRAPH_COMPUTE":
            ng, meta = self.validate_graph(data)
            return ng
        elif name == "GRAPH_RECOMPUTE":
            device, uid = struct.unpack_from("<IQ", data, 0)
            # uid 0 nunca e cacheado; o servidor recusaria. Pre-empta.
            if uid == 0:
                raise Reject("recompute_uid0", "")
        return None

    def on_response(self, name, req, rsp):
        if rsp is None:
            return
        if name == "ALLOC_BUFFER" and self.pending_alloc is not None:
            remote_ptr, remote_size = struct.unpack_from("<QQ", rsp, 0)
            if remote_ptr != 0:
                self.buffers[remote_ptr] = {"size": remote_size, "base": None}
            self.pending_alloc = None
        elif name == "BUFFER_GET_BASE" and self.pending_getbase is not None:
            (base,) = struct.unpack_from("<Q", rsp, 0)
            b = self.buffers.get(self.pending_getbase)
            if b is not None:
                b["base"] = base
            self.pending_getbase = None

    def record_graph(self, ng):
        self.e.stats.graphs_seen += 1
        rec = {"ev": "GRAPH", "uid": ng["uid"], "device": ng["device"],
               "n_nodes": ng["n_nodes"], "n_tensors": ng["n_tensors"],
               "n_buffers": ng["n_buffers"], "graph": ng}
        self.log(rec)


def _hash_key(k):
    import hashlib
    return hashlib.sha1(repr(k).encode("utf-8")).hexdigest()


class Engine:
    def __init__(self, mode, links, policy, logpath):
        self.mode = mode
        self.links = links
        self.policy = policy
        self.stats = Stats()
        self.t0 = now()
        self.logf = open(logpath, "a", buffering=1, encoding="utf-8") if logpath else None
        self.loglock = threading.Lock()
        self.cid = 0
        self.cidlock = threading.Lock()

    def log(self, rec):
        if self.logf:
            with self.loglock:
                self.logf.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def next_cid(self):
        with self.cidlock:
            self.cid += 1
            return self.cid

    def serve_link(self, link):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", link.listen_port))
        srv.listen(8)
        while True:
            try:
                csock, _ = srv.accept()
            except OSError:
                break
            cid = self.next_cid()
            threading.Thread(target=self.handle, args=(link, csock, cid), daemon=True).start()

    def handle(self, link, csock, cid):
        csock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            upsock = socket.create_connection((link.up_host, link.up_port))
        except OSError:
            csock.close()
            return
        upsock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        Connection(self, link, csock, upsock, cid).run()

    def run(self):
        win_timer()
        threads = []
        for link in self.links:
            t = threading.Thread(target=self.serve_link, args=(link,), daemon=True)
            t.start()
            threads.append(t)
        print("porteiro mode=%s links=%s" % (self.mode, ",".join(
            "%s:%d->%d" % (l.name, l.listen_port, l.up_port) for l in self.links)), flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


def parse_map(s):
    # nome:porta_escuta:host:porta_destino
    name, lp, host, up = s.split(":")
    return Link(name, int(lp), host, int(up))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["capture", "gate"], required=True)
    ap.add_argument("--map", action="append", required=True, help="nome:porta_escuta:host:porta_destino")
    ap.add_argument("--log", default=None)
    ap.add_argument("--policy", default=None, help="json de politica (gate)")
    ap.add_argument("--summary", default=None, help="grava resumo de stats ao receber SIGINT/linha 'quit'")
    args = ap.parse_args()

    pol = Policy()
    if args.policy and os.path.exists(args.policy):
        with open(args.policy, encoding="utf-8") as f:
            pol = Policy(json.load(f))
    links = [parse_map(m) for m in args.map]
    eng = Engine(args.mode, links, pol, args.log)

    # thread que escuta 'quit' no stdin para encerrar e gravar resumo
    def watch_stdin():
        for line in sys.stdin:
            if line.strip() == "quit":
                if args.summary:
                    with open(args.summary, "w", encoding="utf-8") as f:
                        json.dump({"msgs": eng.stats.msgs, "by_cmd": eng.stats.by_cmd,
                                   "rejects": eng.stats.rejects, "graphs": eng.stats.graphs_seen}, f, indent=2)
                if eng.logf:
                    eng.logf.flush()
                os._exit(0)
    threading.Thread(target=watch_stdin, daemon=True).start()
    eng.run()


if __name__ == "__main__":
    main()
