# -*- coding: utf-8 -*-
# Sobe so a config C (esparso + porteiros) com -v para capturar a tabela de
# buffers/atribuicao e confirmar onde output.weight ficou. Encerra tudo no fim.
import os, sys, time, subprocess, urllib.request, json
import bench  # reusa launch/kill/wait_health

HERE = bench.HERE; LLAMA = bench.LLAMA; RPC = bench.RPC; SRV = bench.SRV
REAL = bench.REAL; SPARSE = bench.SPARSE; LOGS = bench.LOGS

try:
    for i, rp in enumerate([50065, 50066]):
        cache = os.path.join(HERE, "cache", "V-rpc%d" % i)
        os.makedirs(cache, exist_ok=True)
        bench.launch("V-rpc%d" % i, [RPC, "-H", "127.0.0.1", "-p", str(rp), "-t", "4"],
                     env={"LLAMA_CACHE": cache})
    time.sleep(1.5)
    for i, (pp, rp) in enumerate(zip([50075, 50076], [50065, 50066])):
        bench.launch("V-port%d" % i, [sys.executable, os.path.join(HERE, "porteiro.py"),
                     "--listen", str(pp), "--upstream", "127.0.0.1:%d" % rp,
                     "--gguf", REAL, "--name", "vport%d" % i,
                     "--stats", os.path.join(LOGS, "V-port%d.json" % i)])
    time.sleep(1.5)
    argv = [SRV, "-m", SPARSE, "--host", "127.0.0.1", "--port", "8954",
            "-c", "2048", "-np", "1", "--no-webui", "-v",
            "--rpc", "127.0.0.1:50075,127.0.0.1:50076", "-ngl", "99", "-t", "4",
            "-ot", r"^output(_norm)?\.weight$=CPU"]
    bench.launch("V-srv", argv)
    load = bench.wait_health(8954, 120)
    print("load_s", load)
    time.sleep(1.0)
finally:
    time.sleep(0.5)
    bench.kill_all()
