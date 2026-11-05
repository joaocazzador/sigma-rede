# -*- coding: utf-8 -*-
# Orquestra o teste do coordenador leve:
#  A) referencia local (GGUF completo, sem --rpc)
#  B) RPC direto com GGUF completo (2 rpc-servers, SEM porteiro)
#  C) coordenador ESPARSO + 2 porteiros + 2 rpc-servers
# Compara o texto (SHA-256), mede tempo de carga, RAM do coordenador e velocidade.
# So biblioteca padrao.
import os
import sys
import json
import time
import socket
import signal
import ctypes
import hashlib
import subprocess
import urllib.request
from ctypes import wintypes

HERE = r"C:\sgtmp\shard2\leve"
LLAMA = os.path.join(HERE, "llama")
RPC = os.path.join(LLAMA, "ggml-rpc-server.exe")
SRV = os.path.join(LLAMA, "llama-server.exe")
REAL = r"C:\sgtmp\shard-exp\models\qwen2.5-0.5b.gguf"
SPARSE = os.path.join(HERE, "coord-sparse.gguf")
LOGS = os.path.join(HERE, "logs")
os.makedirs(LOGS, exist_ok=True)

PROMPT = ("Explique em 5 frases curtas como funciona uma rede de computadores "
          "que divide um modelo de linguagem entre varias maquinas.")
NPRED = 48

procs = []  # (nome, Popen)


def launch(name, argv, env=None, cwd=HERE):
    e = dict(os.environ)
    e["KMP_BLOCKTIME"] = "0"
    if env:
        e.update(env)
    log = open(os.path.join(LOGS, name + ".log"), "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env=e)
    procs.append((name, p))
    return p


def kill_from(mark):
    sl = procs[mark:]
    for name, p in sl:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    time.sleep(1.0)
    for name, p in sl:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    del procs[mark:]


def kill_all():
    kill_from(0)


# ---- memoria do processo (Win32) ----
class PMCEX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


def proc_mem(pid):
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        c = PMCEX()
        c.cb = ctypes.sizeof(PMCEX)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return None
        return dict(working_set=c.WorkingSetSize, peak_working_set=c.PeakWorkingSetSize,
                    private=c.PrivateUsage)
    finally:
        k32.CloseHandle(h)


def wait_health(port, timeout=120):
    t0 = time.time()
    url = "http://127.0.0.1:%d/health" % port
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return time.time() - t0
        except Exception:
            pass
        time.sleep(0.25)
    return None


def completion(port):
    body = json.dumps(dict(prompt=PROMPT, n_predict=NPRED, temperature=0.0,
                           seed=42, cache_prompt=False, ignore_eos=True)).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/completion" % port, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        j = json.loads(r.read().decode())
    dt = time.time() - t0
    text = j.get("content", "")
    tim = j.get("timings", {})
    return dict(text=text, sha=hashlib.sha256(text.encode()).hexdigest(),
                wall=dt, n=len(text),
                pred_per_s=tim.get("predicted_per_second"),
                pred_n=tim.get("predicted_n"))


def port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port)); s.close(); return True
    except OSError:
        return False


def run_local(port=8951):
    p = launch("A-local-srv", [SRV, "-m", REAL, "--host", "127.0.0.1", "--port", str(port),
                               "-c", "2048", "-np", "1", "--no-webui", "-ngl", "0",
                               "-t", "4", "--log-timestamps"])
    load = wait_health(port)
    mem = proc_mem(p.pid)
    res = completion(port) if load else None
    return dict(load_s=load, mem=mem, res=res)


def run_rpc(sparse, use_porteiro, tag, srv_port, rpc_ports, proxy_ports=None):
    # rpc-servers
    for i, rp in enumerate(rpc_ports):
        cache = os.path.join(HERE, "cache", "%s-rpc%d" % (tag, i))
        os.makedirs(cache, exist_ok=True)
        launch("%s-rpc%d" % (tag, i), [RPC, "-H", "127.0.0.1", "-p", str(rp), "-t", "4"],
               env={"LLAMA_CACHE": cache})
    time.sleep(1.5)
    endpoints = []
    if use_porteiro:
        for i, (pp, rp) in enumerate(zip(proxy_ports, rpc_ports)):
            launch("%s-porteiro%d" % (tag, i),
                   [sys.executable, os.path.join(HERE, "porteiro.py"),
                    "--listen", str(pp), "--upstream", "127.0.0.1:%d" % rp,
                    "--gguf", REAL, "--name", "port%d" % i,
                    "--stats", os.path.join(LOGS, "%s-port%d.json" % (tag, i))])
            endpoints.append("127.0.0.1:%d" % pp)
        time.sleep(1.5)
    else:
        endpoints = ["127.0.0.1:%d" % rp for rp in rpc_ports]

    model = sparse if sparse else REAL
    argv = [SRV, "-m", model, "--host", "127.0.0.1", "--port", str(srv_port),
            "-c", "2048", "-np", "1", "--no-webui",
            "--rpc", ",".join(endpoints), "-ngl", "99", "-t", "4", "--log-timestamps",
            "-ot", r"^output(_norm)?\.weight$=CPU"]
    p = launch("%s-srv" % tag, argv)
    load = wait_health(srv_port)
    mem = proc_mem(p.pid)
    res = completion(srv_port) if load else None
    return dict(load_s=load, mem=mem, res=res, srv_pid=p.pid)


def main():
    out = {}
    try:
        print(">> A: referencia local (GGUF completo, sem RPC)")
        m = len(procs)
        out["A_local"] = run_local(8951)
        print("   load=%.2fs sha=%s" % (out["A_local"]["load_s"] or -1,
              (out["A_local"]["res"] or {}).get("sha", "")[:16]))
        kill_from(m)

        print(">> B: RPC direto, GGUF completo, 2 rpc-servers, SEM porteiro")
        m = len(procs)
        out["B_rpc_full"] = run_rpc(None, False, "B", 8952, [50061, 50062])
        print("   load=%.2fs sha=%s" % (out["B_rpc_full"]["load_s"] or -1,
              (out["B_rpc_full"]["res"] or {}).get("sha", "")[:16]))
        kill_from(m)

        print(">> C: coordenador ESPARSO + 2 porteiros + 2 rpc-servers")
        m = len(procs)
        out["C_sparse_porteiro"] = run_rpc(SPARSE, True, "C", 8953, [50063, 50064],
                                           [50073, 50074])
        print("   load=%.2fs sha=%s" % (out["C_sparse_porteiro"]["load_s"] or -1,
              (out["C_sparse_porteiro"]["res"] or {}).get("sha", "")[:16]))
        res = completion(8953) if out["C_sparse_porteiro"]["load_s"] else None
        if res:
            out["C_sparse_porteiro"]["res2"] = res  # 2a geracao (determinismo)
        time.sleep(1.0)
        for i in (0, 1):
            fp = os.path.join(LOGS, "C-port%d.json" % i)
            if os.path.exists(fp):
                out["porteiro%d" % i] = json.load(open(fp))
        kill_from(m)
    finally:
        kill_all()

    with open(os.path.join(LOGS, "bench-result.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)

    # resumo
    def sha(c): return (out.get(c, {}).get("res") or {}).get("sha", "")
    print("\n==== RESUMO ====")
    for c in ("A_local", "B_rpc_full", "C_sparse_porteiro"):
        d = out.get(c, {})
        r = d.get("res") or {}
        m = d.get("mem") or {}
        print("%-20s load=%6s s  ws=%7s MB  priv=%7s MB  gen=%5s t/s  sha=%s" % (
            c, round(d.get("load_s") or 0, 2),
            round((m.get("working_set") or 0) / 1e6, 1),
            round((m.get("private") or 0) / 1e6, 1),
            round(r.get("pred_per_s") or 0, 2), r.get("sha", "")[:16]))
    print("A==C texto identico:", sha("A_local") == sha("C_sparse_porteiro") != "")
    print("B==C texto identico:", sha("B_rpc_full") == sha("C_sparse_porteiro") != "")
    for i in (0, 1):
        pst = out.get("porteiro%d" % i)
        if pst:
            print("porteiro%d: set_tensor=%d trocados=%d nomes=%d hash=%d/%d" % (
                i, pst["n_set_tensor"], pst["n_set_tensor_swapped"],
                pst["n_swapped_names"], pst["n_hash_swapped"], pst["n_hash"]))


if __name__ == "__main__":
    try:
        main()
    finally:
        kill_all()
