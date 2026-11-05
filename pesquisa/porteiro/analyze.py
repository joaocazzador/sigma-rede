#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze.py - le capturas (JSONL do proxy em modo capture), normaliza os grafos,
calcula os "modelos de grafo" e mede estabilidade. Opcionalmente emite policy.json (gate).

Uso:
  python analyze.py report  cap1.jsonl [cap2.jsonl ...]
  python analyze.py policy   --level 1 --out policy.json  cap_ref.jsonl
"""
import json
import sys
import hashlib

import rpcwire as w


def _hash_key(k):
    return hashlib.sha1(repr(k).encode("utf-8")).hexdigest()


def load_graphs(path):
    graphs = []
    cmds = set()
    types = set()
    ops = set()
    max_msg = 0
    max_alloc = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            ev = r.get("ev")
            if ev == "GRAPH":
                graphs.append(r["graph"])
                for t in r["graph"]["tensors"]:
                    types.add(t["type"])
                    ops.add(t["op"])
    return graphs


def graph_ntok(ng):
    """Proxy de n_tokens do grafo: maior ne[1] entre os tensores."""
    m = 0
    for t in ng["tensors"]:
        if t["ne"][1] > m:
            m = t["ne"][1]
    return m


def summarize(path):
    graphs = load_graphs(path)
    full = {}
    sk_names = {}
    sk_anon = {}
    for ng in graphs:
        nt = graph_ntok(ng)
        fk = _hash_key(w.full_key(ng))
        skn = _hash_key(w.struct_key(ng, with_names=True))
        ska = _hash_key(w.struct_key(ng, with_names=False))
        full.setdefault(fk, []).append(nt)
        sk_names.setdefault(skn, []).append((nt, ng["n_nodes"], ng["n_tensors"]))
        sk_anon.setdefault(ska, []).append((nt, ng["n_nodes"], ng["n_tensors"]))
    return {
        "path": path, "n_graphs": len(graphs),
        "distinct_full": len(full),
        "distinct_struct_with_names": len(sk_names),
        "distinct_struct_no_names": len(sk_anon),
        "full_keys": set(full.keys()),
        "sk_names": set(sk_names.keys()),
        "sk_anon": set(sk_anon.keys()),
        "sk_anon_detail": {k: v for k, v in sk_anon.items()},
        "ntok_values": sorted(set(graph_ntok(ng) for ng in graphs)),
        "nodes_values": sorted(set(ng["n_nodes"] for ng in graphs)),
    }


def report(paths):
    sums = [summarize(p) for p in paths]
    print("=== por captura ===")
    for s in sums:
        print("%s: grafos=%d | distintos: full=%d struct(c/nome)=%d struct(s/nome)=%d | n_nodes=%s | ntok(max ne[1])=%s"
              % (s["path"], s["n_graphs"], s["distinct_full"], s["distinct_struct_with_names"],
                 s["distinct_struct_no_names"], s["nodes_values"], s["ntok_values"]))
    if len(sums) >= 2:
        print("\n=== estabilidade entre capturas (intersecao / uniao) ===")
        for field, label in (("full_keys", "full"), ("sk_names", "struct c/ nome"), ("sk_anon", "struct s/ nome")):
            inter = set.intersection(*[s[field] for s in sums])
            uni = set.union(*[s[field] for s in sums])
            print("%-16s: comuns a todas=%d | uniao=%d | identicas=%s"
                  % (label, len(inter), len(uni), len(inter) == len(uni)))
        # detalhe: para struct s/ nome, mostrar quantos ne[1] distintos por chave
        print("\n=== struct s/ nome: dependencia de ne[1] (n_tokens) ===")
        base = sums[0]["sk_anon_detail"]
        for k, v in sorted(base.items(), key=lambda kv: -len(kv[1]))[:12]:
            nts = sorted(set(x[0] for x in v))
            nn = sorted(set(x[1] for x in v))
            print("  chave %s...: n_nodes=%s ne1=%s (%d amostras)" % (k[:10], nn, nts, len(v)))


def emit_policy(paths, level, out):
    cmds = set()
    types = set()
    ops = set()
    max_nodes = 0
    max_tensors = 0
    max_ne1 = 0
    sk = set()
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if r.get("ev") == "GRAPH":
                    ng = r["graph"]
                    max_nodes = max(max_nodes, ng["n_nodes"])
                    max_tensors = max(max_tensors, ng["n_tensors"])
                    for t in ng["tensors"]:
                        types.add(t["type"]); ops.add(t["op"])
                        max_ne1 = max(max_ne1, t["ne"][1])
                    sk.add(_hash_key(w.struct_key(ng, with_names=(level >= 2))))
    # comandos observados vem de um resumo separado (by_cmd). Aqui usamos o default seguro.
    pol = {
        "allowed_cmds": sorted([
            "HELLO", "DEVICE_COUNT", "GET_DEVICE_MEMORY", "GET_ALIGNMENT", "GET_MAX_SIZE",
            "ALLOC_BUFFER", "GET_ALLOC_SIZE", "BUFFER_GET_BASE", "FREE_BUFFER", "BUFFER_CLEAR",
            "INIT_TENSOR", "SET_TENSOR", "SET_TENSOR_HASH", "GET_TENSOR", "COPY_TENSOR",
            "MEMSET_TENSOR", "SET_TENSOR_2D", "GET_TENSOR_2D", "GRAPH_COMPUTE", "GRAPH_RECOMPUTE",
        ]),
        "allowed_types": sorted(types),
        "allowed_ops": sorted(ops),
        "graph_level": level,
        "struct_keys": sorted(sk),
        "max_ne1": max(512, max_ne1) * 2,       # folga: ate 2x o observado, >=1024
        "max_nodes": max(1024, max_nodes * 2),
        "max_tensors": max(2048, max_tensors * 2),
        "max_msg": 192 * 1024 * 1024,
        "max_alloc": 4 * 1024 * 1024 * 1024,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(pol, f, indent=2)
    print("policy escrita em %s: tipos=%s ops=%s struct_keys=%d max_ne1=%d max_nodes=%d"
          % (out, pol["allowed_types"], pol["allowed_ops"], len(sk), pol["max_ne1"], pol["max_nodes"]))


def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    mode = sys.argv[1]
    if mode == "report":
        report(sys.argv[2:])
    elif mode == "policy":
        args = sys.argv[2:]
        level = 1
        out = "policy.json"
        paths = []
        i = 0
        while i < len(args):
            if args[i] == "--level":
                level = int(args[i + 1]); i += 2
            elif args[i] == "--out":
                out = args[i + 1]; i += 2
            else:
                paths.append(args[i]); i += 1
        emit_policy(paths, level, out)
    else:
        print("modo desconhecido"); sys.exit(1)


if __name__ == "__main__":
    main()
