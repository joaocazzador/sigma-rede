# -*- coding: utf-8 -*-
# Leitor de GGUF (v2/v3) so com biblioteca padrao. Devolve cabecalho, KVs e a
# tabela de tensores (nome, tipo, dims, offset relativo ao inicio dos dados,
# nbytes e o intervalo absoluto no arquivo). Tambem calcula FNV-1a de 64 bits.
import struct

GGUF_MAGIC = 0x46554747  # "GGUF" little-endian

# (block_size, type_size_bytes) por ggml_type
GGML_TYPE = {
    0:  (1, 4),    # F32
    1:  (1, 2),    # F16
    2:  (32, 18),  # Q4_0
    3:  (32, 20),  # Q4_1
    6:  (32, 22),  # Q5_0
    7:  (32, 24),  # Q5_1
    8:  (32, 34),  # Q8_0
    9:  (32, 36),  # Q8_1
    10: (256, 84),   # Q2_K
    11: (256, 110),  # Q3_K
    12: (256, 144),  # Q4_K
    13: (256, 176),  # Q5_K
    14: (256, 210),  # Q6_K
    15: (256, 292),  # Q8_K
    30: (1, 2),    # BF16
}

GGML_TYPE_NAME = {0:"F32",1:"F16",2:"Q4_0",3:"Q4_1",6:"Q5_0",7:"Q5_1",
                  8:"Q8_0",9:"Q8_1",10:"Q2_K",11:"Q3_K",12:"Q4_K",
                  13:"Q5_K",14:"Q6_K",15:"Q8_K",30:"BF16"}

# tipos de valor de metadado
(GUINT8, GINT8, GUINT16, GINT16, GUINT32, GINT32, GFLOAT32, GBOOL,
 GSTRING, GARRAY, GUINT64, GINT64, GFLOAT64) = range(13)


class Reader:
    """Le direto de um objeto de arquivo aberto em binario (sem carregar tudo)."""
    def __init__(self, f, pos=0):
        self.f = f
        self.f.seek(pos)

    @property
    def pos(self):
        return self.f.tell()

    def raw(self, n):
        b = self.f.read(n)
        if len(b) != n:
            raise EOFError("fim inesperado do GGUF")
        return b

    def u32(self): return struct.unpack_from("<I", self.raw(4))[0]
    def u64(self): return struct.unpack_from("<Q", self.raw(8))[0]
    def i32(self): return struct.unpack_from("<i", self.raw(4))[0]
    def i64(self): return struct.unpack_from("<q", self.raw(8))[0]

    def string(self):
        n = self.u64()
        return self.raw(n).decode("utf-8", "replace")

    def value(self, vtype):
        if vtype == GUINT8:   return struct.unpack_from("<B", self.raw(1))[0]
        if vtype == GINT8:    return struct.unpack_from("<b", self.raw(1))[0]
        if vtype == GUINT16:  return struct.unpack_from("<H", self.raw(2))[0]
        if vtype == GINT16:   return struct.unpack_from("<h", self.raw(2))[0]
        if vtype == GUINT32:  return self.u32()
        if vtype == GINT32:   return self.i32()
        if vtype == GFLOAT32: return struct.unpack_from("<f", self.raw(4))[0]
        if vtype == GBOOL:    return self.raw(1)[0] != 0
        if vtype == GSTRING:  return self.string()
        if vtype == GUINT64:  return self.u64()
        if vtype == GINT64:   return self.i64()
        if vtype == GFLOAT64: return struct.unpack_from("<d", self.raw(8))[0]
        if vtype == GARRAY:
            et = self.u32()
            n = self.u64()
            return [self.value(et) for _ in range(n)]
        raise ValueError("tipo de metadado desconhecido: %d" % vtype)


def nbytes_of(ggml_type, dims):
    ne = 1
    for d in dims:
        ne *= d
    blk, tsz = GGML_TYPE[ggml_type]
    if ne % blk != 0:
        # tolerante: arredonda para cima em blocos
        nblocks = (ne + blk - 1) // blk
    else:
        nblocks = ne // blk
    return nblocks * tsz


class Tensor:
    __slots__ = ("name", "ggml_type", "dims", "rel_offset", "nbytes",
                 "abs_start", "abs_end")

    def __repr__(self):
        return "<Tensor %s %s dims=%s nbytes=%d abs=[%d,%d)>" % (
            self.name, GGML_TYPE_NAME.get(self.ggml_type, self.ggml_type),
            self.dims, self.nbytes, self.abs_start, self.abs_end)


class Gguf:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            r = Reader(f)
            magic = r.u32()
            if magic != GGUF_MAGIC:
                raise ValueError("nao e GGUF (magic=0x%08x)" % magic)
            self.version = r.u32()
            self.tensor_count = r.u64()
            self.kv_count = r.u64()
            self.kv = {}
            for _ in range(self.kv_count):
                key = r.string()
                vtype = r.u32()
                self.kv[key] = r.value(vtype)
            self.alignment = self.kv.get("general.alignment", 32)
            # tabela de tensores
            tensors = []
            for _ in range(self.tensor_count):
                t = Tensor()
                t.name = r.string()
                ndim = r.u32()
                t.dims = [r.u64() for _ in range(ndim)]
                t.ggml_type = r.u32()
                t.rel_offset = r.u64()
                t.nbytes = nbytes_of(t.ggml_type, t.dims)
                tensors.append(t)
            # inicio da secao de dados: pos atual alinhado para cima
            pos = r.pos
            pad = (self.alignment - (pos % self.alignment)) % self.alignment
            self.data_start = pos + pad
            for t in tensors:
                t.abs_start = self.data_start + t.rel_offset
                t.abs_end = t.abs_start + t.nbytes
            self.tensors = tensors
            self.by_name = {t.name: t for t in tensors}
        import os
        self.file_size = os.path.getsize(path)


def fnv1a_64_file(path, start, length, chunk=1 << 20):
    """FNV-1a 64 bits sobre um intervalo do arquivo (igual ao do ggml-rpc)."""
    h = 0xcbf29ce484222325
    prime = 0x100000001b3
    mask = (1 << 64) - 1
    remaining = length
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            b = f.read(min(chunk, remaining))
            if not b:
                break
            for byte in b:
                h ^= byte
                h = (h * prime) & mask
            remaining -= len(b)
    return h


if __name__ == "__main__":
    import sys
    g = Gguf(sys.argv[1])
    print("versao", g.version, "tensores", g.tensor_count,
          "alinhamento", g.alignment, "data_start", g.data_start,
          "file_size", g.file_size)
    print("arch", g.kv.get("general.architecture"),
          "block_count", g.kv.get(str(g.kv.get("general.architecture")) + ".block_count"))
    tot = 0
    for t in g.tensors:
        tot += t.nbytes
        if len(sys.argv) > 2:
            print(t)
    print("soma nbytes dos tensores:", tot)
