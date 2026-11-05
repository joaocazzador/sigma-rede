import json, os, subprocess, sys, time, urllib.request
ROOT = sys.argv[1]; sys.path.insert(0, ROOT)
from sigma import grupo, engines
from sigma.crypto import Wallet
LL = "C:/sgtmp/shard-exp/llama"; MODEL = open("C:/sgtmp/grupo-e2e/first.txt").read().strip()
coord, w1, w2 = Wallet.generate(), Wallet.generate(), Wallet.generate()
pubs = {w.address: w.pub for w in (coord, w1, w2)}
class FakeClient:
    def machine(self, a): return {"pub": pubs.get(a)}
data = r"C:\sgtmp\grupo-e2e\data"; os.makedirs(os.path.join(data, "engine"), exist_ok=True)
log = lambda m: print(m, flush=True)
hub = grupo.Coordinator(coord, FakeClient(), "127.0.0.1:50120", {w1.address: 2048, w2.address: 2048}, log)
hub.start()
workers = []
for w in (w1, w2):
    k = grupo.Worker(w, FakeClient(), coord.address, "127.0.0.1:50120", data, 2, log)
    k._rpc_exe = lambda: os.path.join(LL, grupo.RPC_NAME)
    k.start(); workers.append(k)
t = time.time()
while len(hub.rpc()) < 2 and time.time() - t < 60: time.sleep(0.5)
rpc = hub.rpc(); print("rpc:", rpc, "lent", hub.lent_mb(), flush=True)
def run(rpc, port):
    llama = engines._LlamaCpp(data, log, 2)
    cmd = llama._command(os.path.join(LL, "llama-server.exe"), "cpu", MODEL, port, rpc)
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(rf"C:\sgtmp\grupo-e2e\srv{port}.log", "wb"))
    try:
        for _ in range(240):
            try:
                if json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3).read()).get("status") == "ok": break
            except Exception: time.sleep(0.5)
        body = json.dumps({"prompt": "Explique em uma frase o que é uma rede de computadores:", "n_predict": 48,
                           "temperature": 0, "seed": 1, "cache_prompt": False}).encode()
        t0 = time.time()
        r = json.loads(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/completion", body,
                       {"Content-Type": "application/json"}), timeout=300).read())
        return r["content"], round(time.time() - t0, 2)
    finally:
        p.kill(); p.wait()
local = run([], 8971); split = run(rpc, 8972); direct = run([f"127.0.0.1:{k.port}" for k in workers], 8973); print("DIRETO:", direct[1])
print("LOCAL:", local); print("GRUPO:", split); print("IDENTICO:", local[0] == split[0])
st = [k.status() for k in workers]; print("trabalhadores:", st)
for k in workers: k.stop()
hub.stop()
