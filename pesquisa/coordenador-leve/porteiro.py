# -*- coding: utf-8 -*-
# "Porteiro" do trabalhador: proxy TCP entre o llama-server (coordenador) e o
# ggml-rpc-server local. Entende o enquadramento do protocolo RPC do llama.cpp
# (b11721, v8.0.0). Para cada quadro SET_TENSOR / SET_TENSOR_HASH cujo NOME de
# tensor pertenca a este trabalhador, troca os bytes ZERADOS (ou o hash dos
# zeros) pelos bytes/hash VERDADEIROS daquele tensor, lidos do GGUF real,
# respeitando offset e tamanho. Os demais quadros passam intactos.
#
# Direcao cliente->servidor: parseada quadro a quadro (cmd|size|data).
# Direcao servidor->cliente: bombeada sem parse.
#
# So biblioteca padrao.
import argparse
import socket
import struct
import threading
import json
import time
import gguf_util

# ---- protocolo ----
RPC_CMD_SET_TENSOR = 6
RPC_CMD_SET_TENSOR_HASH = 7
SIZEOF_RPC_TENSOR = 296
# offsets dentro de rpc_tensor (packed)
OFF_NAME = 228          # char name[64]
LEN_NAME = 64
# SET_TENSOR data: | rpc_tensor(296) | cache_flag(1) | offset(8) | payload |
ST_OFF_CACHEFLAG = SIZEOF_RPC_TENSOR            # 296
ST_OFF_WRITEOFF = SIZEOF_RPC_TENSOR + 1         # 297
ST_OFF_PAYLOAD = SIZEOF_RPC_TENSOR + 1 + 8      # 305
# SET_TENSOR_HASH req: | rpc_tensor(296) | offset(8) | hash(8) |
STH_OFF_WRITEOFF = SIZEOF_RPC_TENSOR            # 296
STH_OFF_HASH = SIZEOF_RPC_TENSOR + 8            # 304
STH_REQ_SIZE = SIZEOF_RPC_TENSOR + 16           # 312

MAX_FRAME = 512 * 1024 * 1024


def recvall(sock, n):
    parts = []
    got = 0
    while got < n:
        b = sock.recv(min(65536, n - got))
        if not b:
            return None
        parts.append(b)
        got += len(b)
    return b"".join(parts)


def tensor_name(data):
    raw = data[OFF_NAME:OFF_NAME + LEN_NAME]
    nul = raw.find(b"\x00")
    if nul >= 0:
        raw = raw[:nul]
    return raw.decode("utf-8", "replace")


class RealModel:
    """Fornece os bytes verdadeiros de um tensor por nome, lendo o GGUF real."""
    def __init__(self, gguf_path, owned_names):
        self.path = gguf_path
        self.g = gguf_util.Gguf(gguf_path)
        self.owned = set(owned_names)
        self.lock = threading.Lock()
        self.fh = open(gguf_path, "rb")
        self._hash_cache = {}

    def owns(self, name):
        return name in self.owned and name in self.g.by_name

    def read(self, name, offset, length):
        t = self.g.by_name[name]
        start = t.abs_start + offset
        with self.lock:
            self.fh.seek(start)
            return self.fh.read(length)

    def load_hashmap(self, path):
        if path:
            import json as _j
            with open(path) as fp:
                for k, v in _j.load(fp).items():
                    self._hash_cache[k] = int(v, 16) if isinstance(v, str) else v

    def fnv_full(self, name):
        if name in self._hash_cache:
            return self._hash_cache[name]
        t = self.g.by_name[name]
        h = gguf_util.fnv1a_64_file(self.path, t.abs_start, t.nbytes)
        self._hash_cache[name] = h
        return h


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.swapped_names = set()
        self.n_set_tensor = 0
        self.n_set_tensor_swapped = 0
        self.n_hash = 0
        self.n_hash_swapped = 0
        self.n_other = 0
        self.bytes_c2s = 0
        self.bytes_s2c = 0

    def as_dict(self):
        with self.lock:
            return dict(
                n_set_tensor=self.n_set_tensor,
                n_set_tensor_swapped=self.n_set_tensor_swapped,
                n_hash=self.n_hash,
                n_hash_swapped=self.n_hash_swapped,
                n_other=self.n_other,
                bytes_c2s=self.bytes_c2s,
                bytes_s2c=self.bytes_s2c,
                swapped_names=sorted(self.swapped_names),
                n_swapped_names=len(self.swapped_names),
            )


def pump_s2c(src, dst, stats):
    try:
        while True:
            b = src.recv(65536)
            if not b:
                break
            dst.sendall(b)
            with stats.lock:
                stats.bytes_s2c += len(b)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle_c2s(client, upstream, model, stats, verbose):
    """Le quadros cmd|size|data do cliente, reescreve os donos, envia ao upstream."""
    try:
        while True:
            cmd_b = recvall(client, 1)
            if not cmd_b:
                break
            size_b = recvall(client, 8)
            if not size_b:
                break
            size = struct.unpack("<Q", size_b)[0]
            if size > MAX_FRAME:
                break
            data = recvall(client, size) if size else b""
            if size and data is None:
                break
            cmd = cmd_b[0]
            out_data = data

            if cmd == RPC_CMD_SET_TENSOR and size >= ST_OFF_PAYLOAD:
                with stats.lock:
                    stats.n_set_tensor += 1
                name = tensor_name(data)
                if model.owns(name):
                    woff = struct.unpack_from("<Q", data, ST_OFF_WRITEOFF)[0]
                    payload_len = size - ST_OFF_PAYLOAD
                    real = model.read(name, woff, payload_len)
                    if len(real) == payload_len:
                        out_data = (data[:ST_OFF_PAYLOAD] + real)
                        with stats.lock:
                            stats.n_set_tensor_swapped += 1
                            stats.swapped_names.add(name)
                        if verbose:
                            print("[%s] SET_TENSOR troca %s off=%d len=%d"
                                  % (stats_tag, name, woff, payload_len), flush=True)
                    else:
                        print("[porteiro] AVISO leitura curta %s: %d!=%d"
                              % (name, len(real), payload_len), flush=True)
            elif cmd == RPC_CMD_SET_TENSOR_HASH and size == STH_REQ_SIZE:
                with stats.lock:
                    stats.n_hash += 1
                name = tensor_name(data)
                if model.owns(name):
                    real_hash = model.fnv_full(name)
                    out_data = (data[:STH_OFF_HASH]
                                + struct.pack("<Q", real_hash))
                    with stats.lock:
                        stats.n_hash_swapped += 1
                        stats.swapped_names.add(name)
                    if verbose:
                        print("[porteiro] SET_TENSOR_HASH troca %s -> 0x%016x"
                              % (name, real_hash), flush=True)
            else:
                if cmd not in (RPC_CMD_SET_TENSOR, RPC_CMD_SET_TENSOR_HASH):
                    with stats.lock:
                        stats.n_other += 1

            upstream.sendall(cmd_b + size_b + out_data)
            with stats.lock:
                stats.bytes_c2s += 1 + 8 + len(out_data)
    except OSError:
        pass
    finally:
        try:
            upstream.shutdown(socket.SHUT_WR)
        except OSError:
            pass


stats_tag = "porteiro"


def serve(args):
    global stats_tag
    stats_tag = args.name
    owned = None
    if args.owned:
        owned = [s for s in args.owned.split(",") if s]
    else:
        g = gguf_util.Gguf(args.gguf)
        owned = [t.name for t in g.tensors
                 if t.name not in ("token_embd.weight", "output.weight",
                                   "output_norm.weight")]
    model = RealModel(args.gguf, owned)
    model.load_hashmap(args.hashmap)
    stats = Stats()
    uhost, uport = args.upstream.rsplit(":", 1)
    uport = int(uport)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.listen))
    srv.listen(64)
    print("[%s] escutando 127.0.0.1:%d -> %s:%d  (dono de %d tensores)"
          % (args.name, args.listen, uhost, uport, len(owned)), flush=True)

    conns = []

    def on_conn(client):
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        up = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        up.connect((uhost, uport))
        t1 = threading.Thread(target=handle_c2s,
                              args=(client, up, model, stats, args.verbose),
                              daemon=True)
        t2 = threading.Thread(target=pump_s2c,
                              args=(up, client, stats), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
        try: client.close()
        except OSError: pass
        try: up.close()
        except OSError: pass

    def control():
        # grava stats periodicamente e no fim
        while True:
            time.sleep(0.5)
            if args.stats:
                tmp = args.stats + ".tmp"
                with open(tmp, "w") as fp:
                    json.dump(stats.as_dict(), fp, indent=2)
                import os as _os
                _os.replace(tmp, args.stats)

    if args.stats:
        threading.Thread(target=control, daemon=True).start()

    try:
        while True:
            client, _ = srv.accept()
            th = threading.Thread(target=on_conn, args=(client,), daemon=True)
            th.start()
            conns.append(th)
    except KeyboardInterrupt:
        pass
    finally:
        if args.stats:
            with open(args.stats, "w") as fp:
                json.dump(stats.as_dict(), fp, indent=2)
        print("[%s] stats finais: %s" % (args.name,
              json.dumps(stats.as_dict())), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--upstream", required=True, help="HOST:PORT do rpc-server")
    ap.add_argument("--gguf", required=True, help="GGUF REAL (bytes verdadeiros)")
    ap.add_argument("--name", default="porteiro")
    ap.add_argument("--owned", default="", help="lista de nomes; vazio=todos blk.*")
    ap.add_argument("--stats", default="", help="arquivo json de estatisticas")
    ap.add_argument("--hashmap", default="", help="json {nome: hash_hex} precomputado")
    ap.add_argument("--verbose", action="store_true")
    serve(ap.parse_args())
