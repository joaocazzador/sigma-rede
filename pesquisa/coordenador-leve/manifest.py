# -*- coding: utf-8 -*-
# Gera um manifesto de integridade por tensor (SHA-256 + FNV-1a + offset/tamanho)
# para o modelo. Mede o custo de gerar e estima para um 70B.
import sys, os, json, time, hashlib
import gguf_util


def build_manifest(path, do_fnv=False):
    g = gguf_util.Gguf(path)
    manifest = {"model": os.path.basename(path), "alignment": g.alignment,
                "data_start": g.data_start, "tensors": []}
    t0 = time.time()
    sha_bytes = 0
    with open(path, "rb") as f:
        for t in g.tensors:
            f.seek(t.abs_start)
            h = hashlib.sha256()
            rem = t.nbytes
            while rem > 0:
                b = f.read(min(1 << 20, rem))
                if not b:
                    break
                h.update(b)
                rem -= len(b)
            sha_bytes += t.nbytes
            entry = {"name": t.name, "type": t.ggml_type,
                     "offset": t.rel_offset, "nbytes": t.nbytes,
                     "sha256": h.hexdigest()}
            if do_fnv:
                entry["fnv"] = "%016x" % gguf_util.fnv1a_64_file(path, t.abs_start, t.nbytes)
            manifest["tensors"].append(entry)
    sha_dt = time.time() - t0
    return manifest, sha_dt, sha_bytes


if __name__ == "__main__":
    path = sys.argv[1]
    man, dt, nbytes = build_manifest(path, do_fnv=False)
    js = json.dumps(man, indent=0)
    outp = os.path.join(os.path.dirname(os.path.abspath(path)), "..") if False else "manifest.json"
    with open(os.path.join(os.path.dirname(__file__), "manifest.json"), "w") as f:
        f.write(js)
    n = len(man["tensors"])
    print("tensores:", n)
    print("bytes com sha:", nbytes, "=", round(nbytes / 1e6, 1), "MB")
    print("tempo sha-256:", round(dt, 2), "s  (%.0f MB/s)" % (nbytes / 1e6 / dt))
    print("tamanho do manifesto json:", len(js), "bytes =", round(len(js) / 1024, 1), "KiB")
    print("bytes/tensor no manifesto:", round(len(js) / n, 1))
    # estimativa 70B
    GB70 = 42.5e9
    tensors70 = 80 * 9 + 3  # ~80 camadas x ~9 tensores + embd/out/norm
    print("\n--- estimativa 70B (~42.5 GB, ~%d tensores) ---" % tensors70)
    print("sha-256 (mesmo MB/s):", round(GB70 / 1e6 / (nbytes / 1e6 / dt), 1), "s")
    print("tamanho manifesto (escala por tensor):", round(len(js) / n * tensors70 / 1024, 1), "KiB")
