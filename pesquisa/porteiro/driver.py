#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""driver.py - orquestra rpc-servers, porteiro e llama-server; mede texto e t/s.

So biblioteca padrao. Encerra tudo que inicia no finally.
Fases:
  capture   -> 2 rpc + porteiro(capture) + llama#1; prompts; depois llama#2 (restart). Gera capA/capB.
  textcmp   -> local / rpc-direto / porteiro-capture: compara SHA-256 do texto.
  gate      -> porteiro(gate, policy) em varios niveis; texto identico + t/s.
  cost      -> t/s: direto vs porteiro(capture) vs porteiro(gate) intercalado.
"""
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = r"C:\sgtmp\shard2\porteiro"
LLAMA = os.path.join(ROOT, "llama")
RPC = os.path.join(LLAMA, "ggml-rpc-server.exe")
SRV = os.path.join(LLAMA, "llama-server.exe")
MODEL = r"C:\sgtmp\shard-exp\models\qwen2.5-0.5b.gguf"
LOGS = os.path.join(ROOT, "logs")
os.makedirs(LOGS, exist_ok=True)

PROCS = []  # (name, Popen, logfile)


def spawn(name, args, env=None):
    e = dict(os.environ)
    e["KMP_BLOCKTIME"] = "0"
    if env:
        e.update(env)
    lf = open(os.path.join(LOGS, name + ".out.txt"), "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.PIPE, env=e)
    PROCS.append((name, p, lf))
    return p


def kill_all():
    for name, p, lf in PROCS:
        try:
            if p.poll() is None:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    time.sleep(1.0)
    for name, p, lf in PROCS:
        try:
            lf.close()
        except Exception:
            pass


def wait_port(port, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def wait_health(port, timeout=90):
    t0 = time.time()
    url = "http://127.0.0.1:%d/health" % port
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def start_rpc(port, cachedir):
    os.makedirs(cachedir, exist_ok=True)
    p = spawn("rpc-%d" % port, [RPC, "-H", "127.0.0.1", "-p", str(port), "-t", "4", "-c"],
              env={"LLAMA_CACHE": cachedir})
    wait_port(port, 20)
    return p


def start_porteiro(name, mode, maps, log=None, policy=None):
    args = [sys.executable, os.path.join(ROOT, "proxy.py"), "--mode", mode]
    for m in maps:
        args += ["--map", m]
    if log:
        args += ["--log", log]
    if policy:
        args += ["--policy", policy]
    p = spawn(name, args)
    for m in maps:
        lp = int(m.split(":")[1])
        wait_port(lp, 15)
    time.sleep(0.5)
    return p


def start_llama(port, rpc_endpoints=None, ngl=99):
    args = [SRV, "-m", MODEL, "--host", "127.0.0.1", "--port", str(port),
            "-c", "2048", "-np", "1", "--no-webui", "-ngl", str(ngl), "-t", "4"]
    if rpc_endpoints:
        args += ["--rpc", ",".join(rpc_endpoints)]
    p = spawn("llama-%d" % port, args)
    ok = wait_health(port, 120)
    return p, ok


def completion(port, prompt, n_predict=48, seed=42):
    body = json.dumps({
        "prompt": prompt, "n_predict": n_predict, "temperature": 0, "seed": seed,
        "cache_prompt": False, "stream": False, "ignore_eos": True,
    }).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d/completion" % port, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        j = json.loads(r.read().decode("utf-8"))
    wall = time.time() - t0
    content = j.get("content", "")
    tim = j.get("timings", {})
    return {
        "sha": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
        "n": len(content),
        "pred_n": tim.get("predicted_n"), "pred_tps": tim.get("predicted_per_second"),
        "prompt_n": tim.get("prompt_n"), "prompt_tps": tim.get("prompt_per_second"),
        "predicted_ms": tim.get("predicted_ms"), "prompt_ms": tim.get("prompt_ms"),
        "wall": round(wall, 3),
    }


PROMPTS = {
    "p_short": "Oi.",
    "p_min": "Explique rede.",
    "p_med": ("Explique em cinco frases curtas como uma rede de computadores distribui "
              "um modelo de linguagem entre varias maquinas diferentes e por que isso ajuda."),
    "p_long": (("A rede Sigma une milhares de computadores caseiros para formar uma grande "
                "inteligencia artificial aberta a todos. ") * 10 + "Resuma o paragrafo acima."),
}


def ptoks(port):
    """conta tokens de cada prompt via /tokenize"""
    out = {}
    for k, v in PROMPTS.items():
        try:
            body = json.dumps({"content": v}).encode("utf-8")
            req = urllib.request.Request("http://127.0.0.1:%d/tokenize" % port, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.loads(r.read().decode("utf-8"))
            out[k] = len(j.get("tokens", []))
        except Exception as ex:
            out[k] = "err:%s" % ex
    return out


def phase_capture():
    capA = os.path.join(LOGS, "capA.jsonl")
    capB = os.path.join(LOGS, "capB.jsonl")
    for f in (capA, capB):
        if os.path.exists(f):
            os.remove(f)
    start_rpc(50061, os.path.join(ROOT, "cacheA"))
    start_rpc(50062, os.path.join(ROOT, "cacheB"))
    start_porteiro("porteiro-capA", "capture",
                   ["A:50071:127.0.0.1:50061", "B:50072:127.0.0.1:50062"], log=capA)
    p1, ok = start_llama(8950, ["127.0.0.1:50071", "127.0.0.1:50072"])
    print("llama#1 health:", ok)
    print("prompt tokens:", json.dumps(ptoks(8950)))
    res = {}
    for k in ("p_short", "p_min", "p_med", "p_long"):
        res[k] = completion(8950, PROMPTS[k])
        print("run1", k, res[k]["sha"], "pred_tps=%.2f" % (res[k]["pred_tps"] or 0),
              "prompt_n=", res[k]["prompt_n"])
    # restart: mata llama#1 e porteiro-capA; novo porteiro-capB + llama#2
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p1.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for n, p, lf in PROCS:
        if n == "porteiro-capA" and p.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    start_porteiro("porteiro-capB", "capture",
                   ["A:50071:127.0.0.1:50061", "B:50072:127.0.0.1:50062"], log=capB)
    p2, ok2 = start_llama(8953, ["127.0.0.1:50071", "127.0.0.1:50072"])
    print("llama#2 health:", ok2)
    res2 = {}
    for k in ("p_short", "p_min", "p_med", "p_long"):
        res2[k] = completion(8953, PROMPTS[k])
        print("run2", k, res2[k]["sha"], "pred_tps=%.2f" % (res2[k]["pred_tps"] or 0))
    # texto identico entre run1 e run2?
    same = all(res[k]["sha"] == res2[k]["sha"] for k in res)
    print("TEXTO run1==run2 (via porteiro, restart):", same)
    with open(os.path.join(LOGS, "capture_res.json"), "w", encoding="utf-8") as f:
        json.dump({"run1": res, "run2": res2, "same": same}, f, indent=2)


def phase_textcmp():
    start_rpc(50061, os.path.join(ROOT, "cacheA"))
    start_rpc(50062, os.path.join(ROOT, "cacheB"))
    # local (sem rpc)
    pl, okl = start_llama(8952, None, ngl=0)
    print("local health", okl)
    loc = {k: completion(8952, PROMPTS[k]) for k in PROMPTS}
    # rpc direto
    pd, okd = start_llama(8951, ["127.0.0.1:50061", "127.0.0.1:50062"])
    print("direct health", okd)
    direct = {k: completion(8951, PROMPTS[k]) for k in PROMPTS}
    # o rpc-server atende 1 cliente por vez: encerra o direto antes de subir o porteiro
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pd.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    # porteiro capture
    start_porteiro("porteiro-tc", "capture",
                   ["A:50071:127.0.0.1:50061", "B:50072:127.0.0.1:50062"],
                   log=os.path.join(LOGS, "tc.jsonl"))
    pp, okp = start_llama(8950, ["127.0.0.1:50071", "127.0.0.1:50072"])
    print("porteiro health", okp)
    port = {k: completion(8950, PROMPTS[k]) for k in PROMPTS}
    print("\n%-8s %-18s %-18s %-18s" % ("prompt", "local", "direto", "porteiro"))
    allok = True
    for k in PROMPTS:
        a, b, c = loc[k]["sha"], direct[k]["sha"], port[k]["sha"]
        ok = (a == b == c)
        allok = allok and ok
        print("%-8s %-18s %-18s %-18s %s" % (k, a, b, c, "OK" if ok else "DIFERE"))
    print("TODOS IDENTICOS:", allok)
    with open(os.path.join(LOGS, "textcmp.json"), "w", encoding="utf-8") as f:
        json.dump({"local": loc, "direct": direct, "porteiro": port, "allok": allok}, f, indent=2)


def phase_gate(level):
    policy = {0: "policy_l0.json", 1: "policy.json", 2: "policy_l2.json"}[level]
    policy = os.path.join(ROOT, policy)
    start_rpc(50061, os.path.join(ROOT, "cacheA"))
    start_rpc(50062, os.path.join(ROOT, "cacheB"))
    start_porteiro("porteiro-gate%d" % level, "gate",
                   ["A:50081:127.0.0.1:50061", "B:50082:127.0.0.1:50062"],
                   log=os.path.join(LOGS, "gate%d.jsonl" % level), policy=policy)
    pp, okp = start_llama(8950, ["127.0.0.1:50081", "127.0.0.1:50082"])
    print("gate level", level, "health", okp)
    res = {}
    if okp:
        for k in PROMPTS:
            try:
                res[k] = completion(8950, PROMPTS[k])
                print("gate%d" % level, k, res[k]["sha"], "pred_tps=%.2f" % (res[k]["pred_tps"] or 0))
            except Exception as ex:
                res[k] = {"err": str(ex)}
                print("gate%d" % level, k, "ERRO", ex)
    with open(os.path.join(LOGS, "gate%d_res.json" % level), "w", encoding="utf-8") as f:
        json.dump({"health": okp, "res": res}, f, indent=2)


def phase_cost():
    policy = os.path.join(ROOT, "policy.json")
    # cada config tem seu PROPRIO par de rpc-servers (o rpc atende 1 cliente por vez),
    # todos carregados ao mesmo tempo; so 1 pedido roda por vez, intercalado.
    start_rpc(50061, os.path.join(ROOT, "cacheA"))
    start_rpc(50062, os.path.join(ROOT, "cacheB"))
    start_rpc(50063, os.path.join(ROOT, "cacheC"))
    start_rpc(50064, os.path.join(ROOT, "cacheD"))
    start_rpc(50065, os.path.join(ROOT, "cacheE"))
    start_rpc(50066, os.path.join(ROOT, "cacheF"))
    pd, okd = start_llama(8951, ["127.0.0.1:50061", "127.0.0.1:50062"])
    start_porteiro("porteiro-costcap", "capture",
                   ["A:50071:127.0.0.1:50063", "B:50072:127.0.0.1:50064"],
                   log=os.path.join(LOGS, "costcap.jsonl"))
    pp, okp = start_llama(8950, ["127.0.0.1:50071", "127.0.0.1:50072"])
    start_porteiro("porteiro-costgate", "gate",
                   ["A:50081:127.0.0.1:50065", "B:50082:127.0.0.1:50066"],
                   log=os.path.join(LOGS, "costgate.jsonl"), policy=policy)
    pg, okg = start_llama(8954, ["127.0.0.1:50081", "127.0.0.1:50082"])
    print("cost health direto=%s capture=%s gate=%s" % (okd, okp, okg))
    import statistics
    rounds = 5
    data = {"direto": [], "capture": [], "gate": []}
    targets = {"direto": 8951, "capture": 8950, "gate": 8954}
    for i in range(rounds):
        for cfg, pt in targets.items():
            r = completion(pt, PROMPTS["p_med"], n_predict=48)
            data[cfg].append(r["pred_tps"] or 0)
    out = {}
    for cfg, xs in data.items():
        xs = [x for x in xs if x]
        out[cfg] = {"median_tps": round(statistics.median(xs), 3) if xs else None,
                    "min": round(min(xs), 3) if xs else None, "max": round(max(xs), 3) if xs else None,
                    "samples": [round(x, 2) for x in xs]}
        print(cfg, out[cfg])
    with open(os.path.join(LOGS, "cost.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)


def phase_forge():
    policy = os.path.join(ROOT, "policy.json")
    flog = os.path.join(LOGS, "forge_gate.jsonl")
    if os.path.exists(flog):
        os.remove(flog)
    start_rpc(50063, os.path.join(ROOT, "cacheC"))
    start_porteiro("porteiro-forge", "gate", ["F:50073:127.0.0.1:50063"], log=flog, policy=policy)
    time.sleep(0.5)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "forge.py"), "50073"],
                         capture_output=True, text=True, timeout=120)
    print(out.stdout)
    if out.stderr:
        print("STDERR:", out.stderr[-800:])
    time.sleep(0.5)
    # rejects vistos pelo porteiro
    print("=== REJECTs registrados pelo porteiro ===")
    try:
        for line in open(flog, encoding="utf-8"):
            r = json.loads(line)
            if r.get("ev") == "REJECT":
                print("  %-34s %s" % (r.get("reason"), r.get("detail", "")))
    except FileNotFoundError:
        print("  (sem log)")


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "capture"
    try:
        if phase == "capture":
            phase_capture()
        elif phase == "textcmp":
            phase_textcmp()
        elif phase == "gate":
            phase_gate(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
        elif phase == "cost":
            phase_cost()
        elif phase == "forge":
            phase_forge()
        else:
            print("fase desconhecida")
    finally:
        print("encerrando processos...")
        kill_all()


if __name__ == "__main__":
    main()
