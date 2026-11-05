# -*- coding: utf-8 -*-
# Monta um GGUF "esparso" do coordenador: copia de verdade o cabecalho/metadados
# e os tensores que ficam com o coordenador (token_embd, output, output_norm) e
# deixa BURACOS (sparse NTFS) no lugar dos tensores de camada (blk.*). Em NTFS os
# buracos nao ocupam disco e sao lidos como zeros.
import os
import sys
import ctypes
from ctypes import wintypes
import gguf_util

KEEP = ["token_embd.weight", "output.weight", "output_norm.weight"]
FSCTL_SET_SPARSE = 0x000900C4


def _handle(f):
    import msvcrt
    return msvcrt.get_osfhandle(f.fileno())


def set_sparse(f):
    k32 = ctypes.windll.kernel32
    br = wintypes.DWORD(0)
    ok = k32.DeviceIoControl(_handle(f), FSCTL_SET_SPARSE, None, 0, None, 0,
                             ctypes.byref(br), None)
    return bool(ok)


def allocated_size(path):
    k32 = ctypes.windll.kernel32
    high = wintypes.DWORD(0)
    low = k32.GetCompressedFileSizeW(ctypes.c_wchar_p(path), ctypes.byref(high))
    if low == 0xFFFFFFFF:
        err = ctypes.get_last_error()
        if err:
            return None
    return (high.value << 32) | low


def merge_ranges(ranges):
    ranges = sorted(ranges)
    out = []
    for s, e in ranges:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def build(src, dst):
    g = gguf_util.Gguf(src)
    # regioes REAIS a copiar: cabecalho + tensores KEEP
    real = [(0, g.data_start)]
    for name in KEEP:
        t = g.by_name.get(name)
        if t:
            real.append((t.abs_start, t.abs_end))
    real = merge_ranges(real)

    if os.path.exists(dst):
        os.remove(dst)
    with open(dst, "wb") as out:
        sparse_ok = set_sparse(out)
        with open(src, "rb") as fin:
            for s, e in real:
                fin.seek(s)
                out.seek(s)
                remaining = e - s
                while remaining > 0:
                    chunk = fin.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
        # garante o tamanho logico total (buracos ate o fim ficam como zeros)
        out.truncate(g.file_size)

    logical = os.path.getsize(dst)
    alloc = allocated_size(dst)
    real_bytes = sum(e - s for s, e in real)
    print("sparse_flag_ok:", sparse_ok)
    print("regioes reais:", real)
    print("bytes reais copiados:", real_bytes, "=", round(real_bytes / 1e6, 1), "MB")
    print("tamanho logico:", logical, "=", round(logical / 1e6, 1), "MB")
    if alloc is not None:
        print("alocado em disco:", alloc, "=", round(alloc / 1e6, 1), "MB",
              "(", round(100.0 * alloc / logical, 1), "% do logico )")
    return g, real


def verify(src, dst):
    """Confere que as regioes reais batem byte a byte e que os buracos sao zeros."""
    import hashlib
    g = gguf_util.Gguf(src)
    keep_set = set(KEEP)
    ok = True
    with open(src, "rb") as a, open(dst, "rb") as b:
        # cabecalho
        a.seek(0); b.seek(0)
        ha = hashlib.sha256(a.read(g.data_start)).hexdigest()
        b.seek(0)
        hb = hashlib.sha256(b.read(g.data_start)).hexdigest()
        print("cabecalho identico:", ha == hb)
        ok = ok and (ha == hb)
        # keep tensors identicos
        for name in KEEP:
            t = g.by_name[name]
            a.seek(t.abs_start); b.seek(t.abs_start)
            da = a.read(t.nbytes); b.seek(t.abs_start); db = b.read(t.nbytes)
            same = da == db
            print("keep %-20s identico: %s" % (name, same))
            ok = ok and same
        # amostra de blk.* deve ser zeros no esparso
        import random
        random.seed(1)
        sample = random.sample([t for t in g.tensors if t.name not in keep_set], 8)
        for t in sample:
            b.seek(t.abs_start)
            chunk = b.read(min(t.nbytes, 1 << 16))
            allzero = (chunk.count(0) == len(chunk))
            print("blk  %-26s zeros no esparso: %s" % (t.name, allzero))
            ok = ok and allzero
    print("VERIFICACAO:", "OK" if ok else "FALHOU")
    return ok


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    build(src, dst)
    print("----")
    verify(src, dst)
