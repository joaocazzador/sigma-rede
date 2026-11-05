#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""forge.py - cliente MALICIOSO de teste. Conecta no porteiro (modo gate) e envia
mensagens/grafos forjados, verificando que cada um e RECUSADO antes de chegar ao
ggml-rpc-server. Nao e um ataque real: e a prova de que o porteiro barra T3.

Uso: python forge.py <porta_porteiro>
Cada ataque usa uma conexao nova. Deteccao de recusa: apos enviar o forjado, mando
um GET_DEVICE_MEMORY (tem resposta); se a conexao foi fechada pelo porteiro, falha -> recusado.
No fim, confere que o rpc-server continua vivo (HELLO + GET_DEVICE_MEMORY numa conexao limpa).
"""
import json
import socket
import struct
import sys

import rpcwire as w

HOST = "127.0.0.1"


def conn(port):
    s = socket.create_connection((HOST, port), timeout=5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def send_cmd(s, cmd, data):
    s.sendall(struct.pack("<B", cmd) + struct.pack("<Q", len(data)) + data)


def recv_rsp(s):
    hdr = _recvn(s, 8)
    if hdr is None:
        return None
    (sz,) = struct.unpack("<Q", hdr)
    return _recvn(s, sz)


def _recvn(s, n):
    buf = b""
    try:
        while len(buf) < n:
            r = s.recv(n - len(buf))
            if not r:
                return None
            buf += r
    except OSError:
        return None
    return buf


def hello(s):
    send_cmd(s, w.CMD["HELLO"], b"\x00" * 24)
    return recv_rsp(s) is not None


def get_dev_mem(s):
    send_cmd(s, w.CMD["GET_DEVICE_MEMORY"], struct.pack("<I", 0))
    return recv_rsp(s)


def alloc(s, size):
    send_cmd(s, w.CMD["ALLOC_BUFFER"], struct.pack("<IQ", 0, size))
    r = recv_rsp(s)
    if not r or len(r) < 16:
        return None, None
    ptr, rsize = struct.unpack("<QQ", r)
    return ptr, rsize


def getbase(s, ptr):
    send_cmd(s, w.CMD["BUFFER_GET_BASE"], struct.pack("<Q", ptr))
    r = recv_rsp(s)
    if not r or len(r) < 8:
        return None
    (base,) = struct.unpack("<Q", r)
    return base


def mk_tensor(id=1, ttype=0, buffer=0, ne=(1, 1, 1, 1), nb=None, op=0, op_params=None,
              flags=0, src=(0,) * 10, view_src=0, view_offs=0, data=0, name=b"x", use_count=0):
    if nb is None:
        ts = w.TYPE_SIZE_BLCK1.get(ttype, 4)
        nb0 = ts
        nb = (nb0, nb0 * ne[0], nb0 * ne[0] * ne[1], nb0 * ne[0] * ne[1] * ne[2])
    if op_params is None:
        op_params = [0] * 16
    return struct.pack(
        w._TENSOR_FMT, id, ttype, buffer, ne[0], ne[1], ne[2], ne[3],
        nb[0], nb[1], nb[2], nb[3], op, *op_params, flags, *src, view_src, view_offs,
        data, (name if isinstance(name, bytes) else name.encode())[:63], use_count)


def mk_graph(device, uid, tensors_bytes_list, node_ids):
    out = struct.pack("<IQI", device, uid, len(node_ids))
    for nid in node_ids:
        out += struct.pack("<Q", nid)
    out += struct.pack("<I", len(tensors_bytes_list))
    for tb in tensors_bytes_list:
        out += tb
    return out


def attempt(port, label, fn, need_buffer=False):
    """Abre conexao, (opcional) prepara buffer, executa fn(s, buf), depois tenta usar a conexao."""
    s = conn(port)
    buf = None
    try:
        if need_buffer:
            if not hello(s):
                return {"attack": label, "setup": "hello_falhou"}
            ptr, rsize = alloc(s, 1 << 20)
            if ptr is None:
                return {"attack": label, "setup": "alloc_falhou"}
            base = getbase(s, ptr)
            if base is None:
                return {"attack": label, "setup": "getbase_falhou"}
            buf = {"ptr": ptr, "size": rsize, "base": base}
        fn(s, buf)
        # follow-up: se o porteiro recusou (fechou), isto falha
        alive = get_dev_mem(s) is not None
        return {"attack": label, "rejeitado": (not alive)}
    except (ConnectionResetError, ConnectionAbortedError, OSError) as ex:
        return {"attack": label, "rejeitado": True, "erro_socket": type(ex).__name__}
    finally:
        try:
            s.close()
        except Exception:
            pass


def run(port):
    results = []

    # 1. comando desconhecido (byte 200) - sem HELLO antes
    def a1(s, b):
        send_cmd(s, 200, b"\x00" * 8)
    results.append(attempt(port, "cmd_desconhecido_200", a1))

    # 2. primeiro comando nao e HELLO (GET_DEVICE_MEMORY)
    def a2(s, b):
        send_cmd(s, w.CMD["GET_DEVICE_MEMORY"], struct.pack("<I", 0))
    results.append(attempt(port, "sem_hello", a2))

    # 3. HELLO com tamanho errado
    def a3(s, b):
        send_cmd(s, w.CMD["HELLO"], b"\x00" * 8)
    results.append(attempt(port, "hello_tamanho_errado", a3))

    # 4. COMM_INIT (abriria socket 0.0.0.0) - precisa HELLO antes p/ passar do sem_hello
    def a4(s, b):
        hello(s)
        body = struct.pack("<IIII", 0, 0, 2, 51000) + b"\x00" * 64
        send_cmd(s, w.CMD["COMM_INIT"], body)
    results.append(attempt(port, "comm_init_bloqueado", a4))

    # 5. mensagem gigante (DoS de resize) - GRAPH_COMPUTE com size enorme declarado
    def a5(s, b):
        hello(s)
        s.sendall(struct.pack("<B", w.CMD["GRAPH_COMPUTE"]) + struct.pack("<Q", 1 << 40))
    results.append(attempt(port, "mensagem_gigante_1TiB", a5))

    # 6. ALLOC absurdo (10 GiB)
    def a6(s, b):
        hello(s)
        send_cmd(s, w.CMD["ALLOC_BUFFER"], struct.pack("<IQ", 0, 10 * (1 << 30)))
    results.append(attempt(port, "alloc_10GiB", a6))

    # 7. SET_TENSOR com data fora do buffer conhecido
    def a7(s, b):
        t = mk_tensor(id=1, ttype=0, buffer=b["ptr"], ne=(16, 1, 1, 1),
                      data=b["base"] + b["size"] + 0x1000, name=b"evil")
        payload = b"\x00" * 64
        body = t + struct.pack("<B", 0) + struct.pack("<Q", 0) + payload
        send_cmd(s, w.CMD["SET_TENSOR"], body)
    results.append(attempt(port, "set_tensor_data_fora_do_buffer", a7, need_buffer=True))

    # 8. GET_TENSOR lendo fora do buffer
    def a8(s, b):
        t = mk_tensor(id=1, ttype=0, buffer=b["ptr"], ne=(16, 1, 1, 1), data=b["base"], name=b"leak")
        body = t + struct.pack("<QQ", 0, b["size"] + (1 << 20))  # size > buffer
        send_cmd(s, w.CMD["GET_TENSOR"], body)
    results.append(attempt(port, "get_tensor_le_fora_do_buffer", a8, need_buffer=True))

    # 9. grafo: node com op PERMITIDA (ADD) mas data fora do buffer (defesa de limites T3)
    def a9(s, b):
        t = mk_tensor(id=0x1000, ttype=0, buffer=b["ptr"], ne=(64, 64, 1, 1),
                      op=2, data=b["base"] + b["size"] - 16, name=b"oob")  # 64*64*4 nao cabe
        g = mk_graph(0, 12345, [t], [0x1000])
        send_cmd(s, w.CMD["GRAPH_COMPUTE"], g)
    results.append(attempt(port, "grafo_add_data_fora_do_buffer", a9, need_buffer=True))

    # 10. grafo com op invalida (>= GGML_OP_COUNT)
    def a10(s, b):
        t = mk_tensor(id=0x2000, ttype=0, buffer=b["ptr"], ne=(1, 1, 1, 1),
                      op=120, data=b["base"], name=b"badop")
        g = mk_graph(0, 23456, [t], [0x2000])
        send_cmd(s, w.CMD["GRAPH_COMPUTE"], g)
    results.append(attempt(port, "grafo_op_invalida_120", a10, need_buffer=True))

    # 11. grafo com op fora do allowlist (valida, 0..100, mas nao usada pelo modelo)
    def a11(s, b):
        t = mk_tensor(id=0x3000, ttype=0, buffer=b["ptr"], ne=(1, 1, 1, 1),
                      op=50, data=b["base"], name=b"notallow")
        g = mk_graph(0, 34567, [t], [0x3000])
        send_cmd(s, w.CMD["GRAPH_COMPUTE"], g)
    results.append(attempt(port, "grafo_op_fora_do_allowlist_50", a11, need_buffer=True))

    # 12. grafo com tipo invalido/removido (type 5)
    def a12(s, b):
        t = mk_tensor(id=0x4000, ttype=5, buffer=b["ptr"], ne=(1, 1, 1, 1),
                      op=0, data=b["base"], name=b"badtype")
        g = mk_graph(0, 45678, [t], [0x4000])
        send_cmd(s, w.CMD["GRAPH_COMPUTE"], g)
    results.append(attempt(port, "grafo_tipo_removido_5", a12, need_buffer=True))

    # 13. grafo EM LIMITES mas com struct_key desconhecida (passa por-tensor, falha no modelo)
    def a13(s, b):
        t = mk_tensor(id=0x5000, ttype=0, buffer=b["ptr"], ne=(1, 1, 1, 1),
                      op=2, data=b["base"], name=b"node_0")  # ADD, 4 bytes, cabe
        g = mk_graph(0, 56789, [t], [0x5000])
        send_cmd(s, w.CMD["GRAPH_COMPUTE"], g)
    results.append(attempt(port, "grafo_em_limites_struct_desconhecida", a13, need_buffer=True))

    # liveness final: o rpc-server sobreviveu?
    try:
        s = conn(port)
        ok = hello(s) and (get_dev_mem(s) is not None)
        s.close()
    except OSError:
        ok = False
    return {"results": results, "rpc_vivo_apos_ataques": ok}


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 50073
    print(json.dumps(run(port), indent=2, ensure_ascii=False))
