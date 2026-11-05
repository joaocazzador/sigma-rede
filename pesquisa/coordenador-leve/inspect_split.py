# -*- coding: utf-8 -*-
import sys, gguf_util
g = gguf_util.Gguf(sys.argv[1])
keep = ["token_embd.weight", "output.weight", "output_norm.weight"]
big = [t for t in g.tensors if t.nbytes > 10 * 1024 * 1024]
print("tensores > 10 MiB (passam por SET_TENSOR_HASH):")
for t in big:
    print("   ", t.name, gguf_util.GGML_TYPE_NAME[t.ggml_type], t.nbytes)
real = sum(g.by_name[k].nbytes for k in keep if k in g.by_name)
zeros = sum(t.nbytes for t in g.tensors if t.name not in keep)
print("header/data_start (bytes reais no inicio):", g.data_start)
print("manter real (keep):", round(real / 1e6, 1), "MB")
print("zerar (blk.*):", round(zeros / 1e6, 1), "MB")
print("total tensores:", round(sum(t.nbytes for t in g.tensors) / 1e6, 1), "MB")
# quantos tensores por bloco e total de blk.*
blk = [t for t in g.tensors if t.name.startswith("blk.")]
print("tensores blk.*:", len(blk), " nao-blk:", g.tensor_count - len(blk))
