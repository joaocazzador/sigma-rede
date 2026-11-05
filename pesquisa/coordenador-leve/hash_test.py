# -*- coding: utf-8 -*-
# Testa o caminho SET_TENSOR_HASH ponta a ponta contra o binario real.
# Forca output.weight (137 MiB, > 10 MiB) para um trabalhador: o coordenador
# le os ZEROS do esparso e manda SET_TENSOR_HASH(hash dos zeros); o porteiro
# troca pelo hash VERDADEIRO; o rpc-server acha os bytes reais no cache (que
# pre-preenchemos, simulando o download por HTTP Range) e da "cache hit".
import os, sys, json, time, shutil, hashlib, urllib.request
import gguf_util, make_sparse, bench

HERE = bench.HERE; LLAMA = bench.LLAMA; RPC = bench.RPC; SRV = bench.SRV
REAL = bench.REAL; LOGS = bench.LOGS
SPARSE2 = os.path.join(HERE, "coord-sparse2.gguf")

# 1) esparso que TAMBEM zera output.weight (mantem token_embd + output_norm reais)
make_sparse.KEEP = ["token_embd.weight", "output_norm.weight"]
g, real = make_sparse.build(REAL, SPARSE2)
print("----")
make_sparse.verify(REAL, SPARSE2)

# 2) hash FNV-1a verdadeiro de output.weight
t = g.by_name["output.weight"]
print("calculando fnv real de output.weight (%d bytes)..." % t.nbytes, flush=True)
th0 = time.time()
real_hash = gguf_util.fnv1a_64_file(REAL, t.abs_start, t.nbytes)
print("fnv real = 0x%016x  (%.1fs)" % (real_hash, time.time() - th0), flush=True)
hexname = "%016x" % real_hash
hashmap = os.path.join(HERE, "hashmap.json")
json.dump({"output.weight": hexname}, open(hashmap, "w"))

# 3) nomes "donos" do porteiro = tudo menos token_embd e output_norm (inclui output.weight)
owned = [x.name for x in g.tensors
         if x.name not in ("token_embd.weight", "output_norm.weight")]
owned_arg = ",".join(owned)

# 4) pre-preenche o cache dos 2 rpc-servers com os bytes REAIS de output.weight
cache_dirs = [os.path.join(HERE, "cache", "H-rpc0"), os.path.join(HERE, "cache", "H-rpc1")]
for cd in cache_dirs:
    os.makedirs(cd, exist_ok=True)
    dst = os.path.join(cd, hexname)
    with open(REAL, "rb") as fin, open(dst, "wb") as fout:
        fin.seek(t.abs_start)
        rem = t.nbytes
        while rem > 0:
            b = fin.read(min(1 << 20, rem))
            fout.write(b); rem -= len(b)
    print("cache preenchido:", dst, os.path.getsize(dst))

res = None
try:
    for i, rp in enumerate([50067, 50068]):
        bench.launch("H-rpc%d" % i, [RPC, "-H", "127.0.0.1", "-p", str(rp), "-t", "4", "-c"],
                     env={"LLAMA_CACHE": cache_dirs[i]})
    time.sleep(1.5)
    for i, (pp, rp) in enumerate(zip([50077, 50078], [50067, 50068])):
        bench.launch("H-port%d" % i, [sys.executable, os.path.join(HERE, "porteiro.py"),
                     "--listen", str(pp), "--upstream", "127.0.0.1:%d" % rp,
                     "--gguf", REAL, "--name", "hport%d" % i, "--owned", owned_arg,
                     "--hashmap", hashmap,
                     "--stats", os.path.join(LOGS, "H-port%d.json" % i)])
    time.sleep(1.5)
    # SEM -ot: output.weight vai para um trabalhador (ultima device)
    argv = [SRV, "-m", SPARSE2, "--host", "127.0.0.1", "--port", "8955",
            "-c", "2048", "-np", "1", "--no-webui",
            "--rpc", "127.0.0.1:50077,127.0.0.1:50078", "-ngl", "99", "-t", "4",
            "--log-timestamps"]
    bench.launch("H-srv", argv)
    load = bench.wait_health(8955, 180)
    print("load_s", load, flush=True)
    if load:
        res = bench.completion(8955)
        print("sha =", res["sha"][:16], " gen =", round(res["pred_per_s"] or 0, 2), "t/s")
    time.sleep(1.0)
    for i in (0, 1):
        fp = os.path.join(LOGS, "H-port%d.json" % i)
        if os.path.exists(fp):
            st = json.load(open(fp))
            print("porteiro%d: hash %d/%d trocados, set_tensor %d/%d, nomes=%d" % (
                i, st["n_hash_swapped"], st["n_hash"],
                st["n_set_tensor_swapped"], st["n_set_tensor"], st["n_swapped_names"]))
finally:
    time.sleep(0.5)
    bench.kill_all()

REF = "ca9e6ba7caeb46fc"
if res:
    print("TEXTO == referencia local:", res["sha"][:16] == REF, "(", res["sha"][:16], ")")
# limpa o cache grande deste teste
for cd in cache_dirs:
    shutil.rmtree(cd, ignore_errors=True)
try:
    os.remove(SPARSE2)
except OSError:
    pass
