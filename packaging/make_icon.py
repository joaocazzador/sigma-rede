"""Desenha o ícone do Sigma (Σ preto sobre o vermelho do site) e grava sigma.ico e sigma.png.

    python packaging/make_icon.py [--ico packaging/sigma.ico] [--png packaging/sigma.png]

Python puro, sem Pillow: o Σ é um polígono rasterizado com antisserrilhado (cobertura exata na
horizontal, 8 subamostras na vertical). O .ico leva vários tamanhos: até 128 px em BMP (o formato que
qualquer Windows entende) e 256 px em PNG (o que o Windows espera nesse tamanho).
"""
import argparse
import os
import struct
import zlib
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))
RED, INK = (0xFF, 0x3B, 0x1F), (0x00, 0x00, 0x00)    # o vermelho sinal do site e o preto dos botões
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)        # o Windows escolhe conforme a escala da tela
SUB = 8                                               # subamostras verticais por pixel
# Σ numa grade de 256 (cantos retos, como o site): barra de cima, diagonal até o bico, diagonal de volta,
# barra de baixo. As diagonais são paralelas e têm a mesma espessura das barras.
SIGMA = [(66, 38), (190, 38), (190, 72), (108, 72), (150, 128), (108, 184), (190, 184), (190, 218),
         (66, 218), (66, 184), (108, 128), (66, 72)]


def _span(row, a, b, w):
    """Soma a cobertura do trecho [a, b) da linha, com as pontas fracionárias (antisserrilhado horizontal)."""
    if b <= a:
        return
    i, j = int(a), int(b)
    if i == j:
        row[i] += (b - a) * w
        return
    row[i] += (i + 1 - a) * w
    for x in range(i + 1, j):
        row[x] += w
    if j < len(row):
        row[j] += (b - j) * w


def coverage(size, poly=SIGMA, sub=SUB):
    """Fração de cada pixel coberta pelo polígono (0 a 1), linha por linha (regra par-ímpar)."""
    k = size / 256
    pts = [(x * k, y * k) for x, y in poly]
    edges = [(x0, y0, x1, y1) for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]) if y0 != y1]
    rows = []
    for py in range(size):
        row = [0.0] * size
        for s in range(sub):
            y = py + (s + 0.5) / sub
            xs = sorted(x0 + (y - y0) * (x1 - x0) / (y1 - y0) for x0, y0, x1, y1 in edges
                        if min(y0, y1) <= y < max(y0, y1))
            for a, b in zip(xs[::2], xs[1::2]):
                _span(row, max(0.0, a), min(float(size), b), 1 / sub)
        rows.append(row)
    return rows


@lru_cache(maxsize=None)
def pixels(size):
    """Linhas RGBA opacas: o fundo vermelho e o Σ preto misturados pela cobertura de cada pixel."""
    out = []
    for row in coverage(size):
        line = bytearray()
        for c in row:
            a = min(1.0, c)
            line += bytes(round(bg + (fg - bg) * a) for bg, fg in zip(RED, INK)) + b"\xff"
        out.append(bytes(line))
    return tuple(out)


def png(size):
    """PNG RGBA de 8 bits, sem filtro nas linhas (o zlib comprime bem a área lisa)."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + line for line in pixels(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def dib(size):
    """Imagem BMP do .ico: cabeçalho com a altura dobrada, BGRA de baixo para cima e a máscara AND (vazia)."""
    xor = b"".join(bytes(v for i in range(0, len(line), 4) for v in (line[i + 2], line[i + 1], line[i], line[i + 3]))
                   for line in reversed(pixels(size)))
    mask = bytes((size + 31) // 32 * 4 * size)        # cada linha da máscara ocupa múltiplos de 4 bytes
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
    return header + xor + mask


def ico(sizes=SIZES):
    images = [png(s) if s >= 256 else dib(s) for s in sizes]
    entries, offset = b"", 6 + 16 * len(images)
    for s, data in zip(sizes, images):
        # largura/altura 0 quer dizer 256; 1 plano, 32 bits por pixel
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(images)) + entries + b"".join(images)


def _write(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def write_icon(ico_path=None, png_path=None):
    """Grava o .ico (e, se pedido, o PNG de 256 px). Devolve o caminho do .ico."""
    ico_path = ico_path or os.path.join(HERE, "sigma.ico")
    _write(ico_path, ico())
    if png_path:
        _write(png_path, png(256))
    return ico_path


def main():
    ap = argparse.ArgumentParser(description="Gera o ícone Σ do Sigma (.ico e .png) sem nenhuma dependência.")
    ap.add_argument("--ico", default=os.path.join(HERE, "sigma.ico"), help="destino do .ico")
    ap.add_argument("--png", default=os.path.join(HERE, "sigma.png"), help="destino do PNG de 256 px")
    args = ap.parse_args()
    path = write_icon(args.ico, args.png)
    print(f"ícone: {path} ({os.path.getsize(path):,} bytes, {len(SIZES)} tamanhos: "
          f"{', '.join(map(str, SIZES))} px)")
    print(f"png:   {args.png} ({os.path.getsize(args.png):,} bytes, 256 px)")


if __name__ == "__main__":
    main()
