#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rpcwire.py - decodificacao do protocolo ggml-rpc (llama.cpp tag b11721, protocolo 8.0.0).

So biblioteca padrao. Usado pelo porteiro (proxy.py) e pela analise (analyze.py).
Formatos conferidos no codigo-fonte baixado de raw.githubusercontent.com (tag b11721),
SHA-256 583cf1ee...eacd, em ggml/src/ggml-rpc/ggml-rpc.cpp e ggml/include/ggml-rpc.h.

Enquadramento:
  cliente->servidor: | cmd (1 byte) | request_size (uint64 LE) | request_data |
  servidor->cliente (so comandos com resposta): | response_size (uint64 LE) | response_data |
"""
import struct

RPC_PROTO = (8, 0, 0)
RPC_CONN_CAPS_SIZE = 24
GGML_MAX_DIMS = 4
GGML_MAX_SRC = 10
GGML_MAX_NAME = 64
GGML_MAX_OP_PARAMS = 64            # bytes (16 x int32)
GGML_TYPE_COUNT = 43
GGML_OP_COUNT = 101
HASH_THRESHOLD = 10 * 1024 * 1024
GRAPH_CACHE_MAX = 1024

# enum rpc_cmd (ggml-rpc.cpp:57-83)
CMD = {
    "ALLOC_BUFFER": 0, "GET_ALIGNMENT": 1, "GET_MAX_SIZE": 2, "BUFFER_GET_BASE": 3,
    "FREE_BUFFER": 4, "BUFFER_CLEAR": 5, "SET_TENSOR": 6, "SET_TENSOR_HASH": 7,
    "GET_TENSOR": 8, "COPY_TENSOR": 9, "GRAPH_COMPUTE": 10, "GET_DEVICE_MEMORY": 11,
    "INIT_TENSOR": 12, "GET_ALLOC_SIZE": 13, "HELLO": 14, "DEVICE_COUNT": 15,
    "GRAPH_RECOMPUTE": 16, "MEMSET_TENSOR": 17, "SET_TENSOR_2D": 18, "GET_TENSOR_2D": 19,
    "COMM_INIT": 20, "COMM_ALLREDUCE": 21, "COMM_FREE": 22, "NONE": 23,
}
RPC_CMD_COUNT = 24
CMD_NAME = {v: k for k, v in CMD.items()}

# comandos para os quais o servidor devolve uma resposta (rpc_serve_client)
HAS_RESPONSE = {
    "HELLO", "DEVICE_COUNT", "ALLOC_BUFFER", "GET_ALLOC_SIZE", "GET_ALIGNMENT",
    "GET_MAX_SIZE", "BUFFER_GET_BASE", "GET_TENSOR_2D", "SET_TENSOR_HASH",
    "GET_TENSOR", "COPY_TENSOR", "COMM_INIT", "GET_DEVICE_MEMORY",
}

RPC_TENSOR_SIZE = 296
# struct rpc_tensor (packed). Confere sizeof == 296.
_TENSOR_FMT = "<Q I Q 4I 4I I 16i i 10Q Q Q Q 64s i".replace(" ", "")
assert struct.calcsize(_TENSOR_FMT) == RPC_TENSOR_SIZE, struct.calcsize(_TENSOR_FMT)

# tamanho fixo de request por comando (None = tamanho variavel)
REQ_SIZE = {
    "HELLO": RPC_CONN_CAPS_SIZE,              # 24
    "DEVICE_COUNT": 0,
    "ALLOC_BUFFER": 12,                       # device u32 + size u64
    "GET_ALLOC_SIZE": 4 + RPC_TENSOR_SIZE + GGML_MAX_SRC * RPC_TENSOR_SIZE,  # 3260
    "GET_ALIGNMENT": 4,
    "GET_MAX_SIZE": 4,
    "BUFFER_GET_BASE": 8,
    "FREE_BUFFER": 8,
    "BUFFER_CLEAR": 9,                        # remote_ptr u64 + value u8
    "MEMSET_TENSOR": RPC_TENSOR_SIZE + 17,    # tensor + offset u64 + size u64 + value u8 = 313
    "SET_TENSOR_HASH": RPC_TENSOR_SIZE + 16,  # tensor + offset u64 + hash u64 = 312
    "GET_TENSOR": RPC_TENSOR_SIZE + 16,       # tensor + offset u64 + size u64 = 312
    "GET_TENSOR_2D": RPC_TENSOR_SIZE + 32,    # tensor + 4 x u64 = 328
    "COPY_TENSOR": 2 * RPC_TENSOR_SIZE,       # 592
    "GET_DEVICE_MEMORY": 4,
    "INIT_TENSOR": RPC_TENSOR_SIZE,           # 296
    "GRAPH_RECOMPUTE": 12,                    # device u32 + uid u64
    "COMM_INIT": 4 + 4 + 4 + 4 + 64,          # 80
    "COMM_ALLREDUCE": 4 + RPC_TENSOR_SIZE,    # 300
    "COMM_FREE": 4,
    # variaveis:
    "SET_TENSOR": None,                       # >= tensor + 1 + 8 = 305
    "SET_TENSOR_2D": None,                    # >= tensor + 32 = 328
    "GRAPH_COMPUTE": None,                    # >= 16
}
# tamanho minimo das mensagens variaveis
REQ_MIN = {
    "SET_TENSOR": RPC_TENSOR_SIZE + 1 + 8,
    "SET_TENSOR_2D": RPC_TENSOR_SIZE + 32,
    "GRAPH_COMPUTE": 2 * 4 + 8,
}
# tamanho fixo de response por comando (None = variavel)
RSP_SIZE = {
    "HELLO": 4 + RPC_CONN_CAPS_SIZE,          # 28
    "DEVICE_COUNT": 4,
    "ALLOC_BUFFER": 16,
    "GET_ALLOC_SIZE": 8,
    "GET_ALIGNMENT": 8,
    "GET_MAX_SIZE": 8,
    "BUFFER_GET_BASE": 8,
    "SET_TENSOR_HASH": 1,
    "COPY_TENSOR": 1,
    "COMM_INIT": 1,
    "GET_DEVICE_MEMORY": 16,
    "GET_TENSOR": None,                       # = request.size
    "GET_TENSOR_2D": None,                    # = size * n_copies
}

# ---- ggml_nbytes (ggml.c:1309) ----
# blck_size por tipo (type_traits em ggml.c). 0 = tipo removido/invalido.
BLCK = {
    0: 1, 1: 1, 2: 32, 3: 32, 6: 32, 7: 32, 8: 32, 9: 32,
    10: 256, 11: 256, 12: 256, 13: 256, 14: 256, 15: 256,
    16: 256, 17: 256, 18: 256, 19: 256, 20: 32, 21: 256, 22: 256, 23: 256,
    24: 1, 25: 1, 26: 1, 27: 1, 28: 1, 29: 256, 30: 1,
    34: 256, 35: 256, 39: 32,
    # 40 NVFP4, 41 Q1_0, 42 Q2_0: nao aparecem no Qwen2.5; deixados fora do allowlist.
}
# type_size so e necessario para os tipos com blck_size == 1 (ggml_nbytes usa nb[] nos demais)
TYPE_SIZE_BLCK1 = {0: 4, 1: 2, 24: 1, 25: 2, 26: 4, 27: 8, 28: 8, 30: 2}
TYPE_NAME = {
    0: "f32", 1: "f16", 2: "q4_0", 3: "q4_1", 6: "q5_0", 7: "q5_1", 8: "q8_0", 9: "q8_1",
    10: "q2_K", 11: "q3_K", 12: "q4_K", 13: "q5_K", 14: "q6_K", 15: "q8_K",
    16: "iq2_xxs", 17: "iq2_xs", 18: "iq3_xxs", 19: "iq1_s", 20: "iq4_nl", 21: "iq3_s",
    22: "iq2_s", 23: "iq4_xs", 24: "i8", 25: "i16", 26: "i32", 27: "i64", 28: "f64",
    29: "iq1_m", 30: "bf16", 34: "tq1_0", 35: "tq2_0", 39: "mxfp4", 40: "nvfp4",
    41: "q1_0", 42: "q2_0",
}


def ggml_nbytes(ttype, ne, nb):
    """Replica ggml.c:1309 exatamente. ne/nb sao os valores uint32 do fio."""
    for i in range(GGML_MAX_DIMS):
        if ne[i] <= 0:
            return 0
    blck = BLCK.get(ttype, 0)
    if blck == 0:
        return None  # tipo invalido/removido
    if blck == 1:
        ts = TYPE_SIZE_BLCK1.get(ttype)
        if ts is None:
            return None
        nbytes = ts
        for i in range(GGML_MAX_DIMS):
            nbytes += (ne[i] - 1) * nb[i]
    else:
        nbytes = ne[0] * nb[0] // blck
        for i in range(1, GGML_MAX_DIMS):
            nbytes += (ne[i] - 1) * nb[i]
    return nbytes


def parse_tensor(buf, off=0):
    """Desempacota um rpc_tensor. Retorna dict com os campos."""
    t = struct.unpack_from(_TENSOR_FMT, buf, off)
    # layout: id,type,buffer, ne0..3, nb0..3, op, op_params0..15, flags, src0..9, view_src, view_offs, data, name, use_count
    d = {
        "id": t[0], "type": t[1], "buffer": t[2],
        "ne": list(t[3:7]), "nb": list(t[7:11]),
        "op": t[11], "op_params": list(t[12:28]), "flags": t[28],
        "src": list(t[29:39]), "view_src": t[39], "view_offs": t[40],
        "data": t[41], "name": t[42].split(b"\x00", 1)[0].decode("latin-1"),
        "use_count": t[43],
    }
    return d


def parse_graph(data):
    """Desempacota o corpo de GRAPH_COMPUTE.
    | device u32 | uid u64 | n_nodes u32 | nodes[n_nodes] u64 | n_tensors u32 | tensors[n_tensors] |
    Retorna (meta, node_ids, tensors) ou levanta ValueError se malformado.
    """
    if len(data) < 16:
        raise ValueError("graph too short")
    device, uid, n_nodes = struct.unpack_from("<IQI", data, 0)
    off = 16
    need = off + n_nodes * 8 + 4
    if len(data) < need:
        raise ValueError("graph nodes truncated")
    node_ids = list(struct.unpack_from("<%dQ" % n_nodes, data, off)) if n_nodes else []
    off += n_nodes * 8
    (n_tensors,) = struct.unpack_from("<I", data, off)
    off += 4
    need = off + n_tensors * RPC_TENSOR_SIZE
    if len(data) < need:
        raise ValueError("graph tensors truncated")
    tensors = [parse_tensor(data, off + i * RPC_TENSOR_SIZE) for i in range(n_tensors)]
    return ({"device": device, "uid": uid, "n_nodes": n_nodes, "n_tensors": n_tensors}, node_ids, tensors)


def normalize_graph(meta, node_ids, tensors, buffers):
    """Produz um grafo normalizado, independente de execucao.
    - ids de tensor -> indice pela ordem no array tensors (ordem estavel por topologia)
    - buffer remoto -> indice canonico pela 1a aparicao
    - data -> (bufidx, offset relativo a base do buffer)  [base vem do modelo-sombra]
    buffers: dict remote_ptr -> {"size":..., "base":...}
    """
    id2idx = {}
    for i, t in enumerate(tensors):
        if t["id"] != 0:
            id2idx.setdefault(t["id"], i)
    bufmap = {}  # remote_ptr -> bufidx

    def bufidx(ptr):
        if ptr == 0:
            return -1
        if ptr not in bufmap:
            bufmap[ptr] = len(bufmap)
        return bufmap[ptr]

    norm = []
    for t in tensors:
        bi = bufidx(t["buffer"])
        data_off = None
        if t["buffer"] != 0:
            b = buffers.get(t["buffer"])
            if b and b.get("base") is not None:
                data_off = t["data"] - b["base"]
            else:
                data_off = ("raw", t["data"])
        srcs = [id2idx.get(s, -1) if s != 0 else -1 for s in t["src"]]
        vs = id2idx.get(t["view_src"], -1) if t["view_src"] != 0 else -1
        norm.append({
            "op": t["op"], "type": t["type"], "ne": t["ne"], "nb": t["nb"],
            "op_params": t["op_params"], "flags": t["flags"],
            "bufidx": bi, "data_off": data_off, "src": srcs,
            "view_src": vs, "view_offs": t["view_offs"],
            "name": t["name"], "use_count": t["use_count"],
        })
    nodes = [id2idx.get(nid, -1) for nid in node_ids]
    return {"device": meta["device"], "uid": meta["uid"],
            "n_nodes": meta["n_nodes"], "n_tensors": meta["n_tensors"],
            "n_buffers": len(bufmap), "nodes": nodes, "tensors": norm}


def struct_key(ng, with_names=True):
    """Chave estrutural do grafo: ignora ne/nb/op_params/data_off/view_offs (params por pedido).
    Mantem op, type, bufidx, topologia de src/view_src, (nome opcional) e ordem dos nodes."""
    parts = []
    for t in ng["tensors"]:
        item = (t["op"], t["type"], t["bufidx"], tuple(t["src"]), t["view_src"])
        if with_names:
            item = item + (t["name"],)
        parts.append(item)
    return (tuple(ng["nodes"]), tuple(parts))


def full_key(ng):
    """Chave concreta: inclui ne/nb/op_params/data_off/view_offs (sem bases absolutas)."""
    parts = []
    for t in ng["tensors"]:
        parts.append((t["op"], t["type"], t["bufidx"], tuple(t["ne"]), tuple(t["nb"]),
                      tuple(t["op_params"]), t["flags"], tuple(t["src"]), t["view_src"],
                      t["view_offs"], t["data_off"], t["name"]))
    return (tuple(ng["nodes"]), tuple(parts))
