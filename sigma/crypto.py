"""Criptografia do Sigma: ECDSA secp256k1 em Python puro (sem dependências).

Chaves privadas são inteiros; chaves públicas são 64 bytes (x||y) em hex;
endereços são "sig" + os últimos 40 hex do keccak256 da chave pública (o mesmo endereço do Ethereum).
seal/unseal cifram uma mensagem para o dono de uma chave pública (perguntas e respostas da IA).
"""
import hashlib
import hmac
import json
import os
import re

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(p, q):
    """Soma de dois pontos afins (None = ponto no infinito)."""
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % P == 0:
        return None
    if p == q:
        lam = 3 * p[0] * p[0] * pow(2 * p[1], -1, P) % P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, P) % P
    x = (lam * lam - p[0] - q[0]) % P
    return x, (lam * (p[0] - x) - p[1]) % P


# Multiplicação por escalar em coordenadas jacobianas (x = X/Z², y = Y/Z³): as contas intermediárias não
# precisam de inversão modular (a parte cara), só uma no fim. Mesmo resultado da versão afim, ~5x mais rápido:
# importa porque o nó confere assinaturas e o ecrecover da MetaMask o tempo todo. Z == 0 é o infinito.
def _jdouble(X, Y, Z):
    if not Z or not Y:
        return 0, 1, 0
    yy = Y * Y % P
    s = 4 * X * yy % P
    m = 3 * X * X % P                 # a = 0 na secp256k1
    x3 = (m * m - 2 * s) % P
    return x3, (m * (s - x3) - 8 * yy * yy) % P, 2 * Y * Z % P


def _jadd(X1, Y1, Z1, x2, y2):
    """(X1, Y1, Z1) jacobiano + (x2, y2) afim."""
    if not Z1:
        return x2, y2, 1
    zz = Z1 * Z1 % P
    h = (x2 * zz - X1) % P
    r = (y2 * zz * Z1 - Y1) % P
    if not h:                         # mesmo x: o mesmo ponto (dobra) ou o oposto (infinito)
        return _jdouble(X1, Y1, Z1) if not r else (0, 1, 0)
    hh = h * h % P
    hhh = h * hh % P
    v = X1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    return x3, (r * (v - x3) - Y1 * hhh) % P, Z1 * h % P


def _affine(X, Y, Z):
    if not Z:
        return None
    zi = pow(Z, -1, P)
    zi2 = zi * zi % P
    return X * zi2 % P, Y * zi2 * zi % P


_G_POWERS = None   # 2^i·G (afim), montado no primeiro uso: k·G vira só somas, sem dobras


def _g_powers():
    global _G_POWERS
    if _G_POWERS is None:
        pts, p = [], G
        for _ in range(256):
            pts.append(p)
            p = _add(p, p)
        _G_POWERS = tuple(pts)
    return _G_POWERS


def _mul(k, p):
    """k·p, com p afim (ou None); devolve afim ou None (infinito)."""
    if p is None or k <= 0:
        return None
    if p == G and k.bit_length() <= 256:
        X, Y, Z = 0, 1, 0
        for i, q in enumerate(_g_powers()):
            if k >> i & 1:
                X, Y, Z = _jadd(X, Y, Z, q[0], q[1])
        return _affine(X, Y, Z)
    x, y = p
    X, Y, Z = x, y, 1
    for bit in bin(k)[3:]:            # da segunda casa mais alta para a mais baixa: dobra e soma
        X, Y, Z = _jdouble(X, Y, Z)
        if bit == "1":
            X, Y, Z = _jadd(X, Y, Z, x, y)
    return _affine(X, Y, Z)


def _z(msg: bytes) -> int:
    return int.from_bytes(hashlib.sha256(msg).digest(), "big") % N


def pub_of(priv: int) -> str:
    x, y = _mul(priv, G)
    return x.to_bytes(32, "big").hex() + y.to_bytes(32, "big").hex()


_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
       0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
       0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M64 = (1 << 64) - 1


def _rol(v, n):
    return ((v << n) | (v >> (64 - n))) & _M64 if n else v


def keccak256(data: bytes) -> bytes:
    """Keccak-256 do Ethereum (diferente do SHA3-256 padronizado do hashlib)."""
    rate = 136
    padded = bytearray(data + b"\x01" + b"\x00" * ((rate - (len(data) + 1) % rate) % rate))
    padded[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(padded[off + 8 * i:off + 8 * i + 8], "little")
        for rc in _RC:
            c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y] ^ d[x], _ROT[x][y])
            a = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            a[0][0] ^= rc
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def address_of(pub: str) -> str:
    """Mesmo endereço do Ethereum, com prefixo 'sig': sigABC... == 0xABC... na MetaMask."""
    return "sig" + keccak256(bytes.fromhex(pub))[12:].hex()


def to_eth(addr: str) -> str:
    return "0x" + addr[3:]


_HEX40 = re.compile(r"[0-9a-fA-F]{40}")


def _body(addr):
    """Os 40 hex depois de '0x'/'sig', ou None. Só dígitos hex: o int(x, 16) aceitaria "+", "_" e espaços."""
    if not isinstance(addr, str):
        return None
    body = addr[2:] if addr[:2].lower() == "0x" else addr[3:] if addr[:3] == "sig" else None
    return body if body is not None and _HEX40.fullmatch(body) else None


def norm_address(addr) -> str:
    """Aceita 'sig…' ou '0x…' e devolve 'sig…' em minúsculas; levanta ValueError se inválido."""
    body = _body(addr)
    if body is None:
        raise ValueError(f"endereço inválido: {str(addr)[:60]}")
    return "sig" + body.lower()


def checksum_ok(addr) -> bool:
    """Confere o checksum EIP-55 de um endereço digitado ('0x…' ou 'sig…', 40 hex).

    Tudo minúsculo ou tudo maiúsculo passa (não tem checksum); letras misturadas precisam bater com o
    EIP-55, que é como a MetaMask mostra o endereço. Um caractere trocado quase sempre é pego aqui."""
    body = _body(addr)
    if body is None:
        return False
    if body == body.lower() or body == body.upper():
        return True
    h = keccak256(body.lower().encode()).hex()
    return all(c == (c.upper() if int(h[i], 16) >= 8 else c.lower()) for i, c in enumerate(body))


def to_checksum(addr) -> str:
    """'0x' + o endereço com as maiúsculas do EIP-55 (o mesmo que a MetaMask mostra)."""
    body = norm_address(addr)[3:]
    h = keccak256(body.encode()).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(body))


def recover_pub(z: bytes, r: int, s: int, recid: int) -> str:
    """ecrecover: descobre a chave pública que assinou o hash z (assinaturas da MetaMask)."""
    if not (0 < r < N and 0 < s < N) or recid not in (0, 1):
        raise ValueError("assinatura inválida")
    alpha = (r * r * r + 7) % P
    beta = pow(alpha, (P + 1) // 4, P)
    if beta * beta % P != alpha:
        raise ValueError("assinatura inválida")
    y = beta if beta % 2 == recid else P - beta
    e = int.from_bytes(z, "big") % N
    rinv = pow(r, -1, N)   # Q = r⁻¹·(s·R − e·G) = (s·r⁻¹)·R + (−e·r⁻¹)·G: duas multiplicações em vez de três
    q = _add(_mul(s * rinv % N, (r, y)), _mul((-e) * rinv % N, G))
    if q is None:
        raise ValueError("assinatura inválida")
    return q[0].to_bytes(32, "big").hex() + q[1].to_bytes(32, "big").hex()


def sign(priv: int, msg: bytes) -> str:
    z = _z(msg)
    digest = hashlib.sha256(msg).digest()
    counter = 0
    while True:
        # nonce determinístico derivado da chave + mensagem (estilo RFC 6979)
        k = int.from_bytes(hmac.new(priv.to_bytes(32, "big"), digest + bytes([counter]),
                                    hashlib.sha256).digest(), "big") % N
        counter += 1
        if k == 0:
            continue
        r = _mul(k, G)[0] % N
        if r == 0:
            continue
        s = pow(k, -1, N) * (z + r * priv) % N
        if s == 0:
            continue
        if s > N // 2:
            s = N - s
        return r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex()


def verify(pub: str, msg: bytes, sig: str) -> bool:
    try:
        x, y = int(pub[:64], 16), int(pub[64:128], 16)
        r, s = int(sig[:64], 16), int(sig[64:128], 16)
    except (ValueError, TypeError):
        return False
    if len(pub) != 128 or len(sig) != 128:
        return False
    if (y * y - x * x * x - 7) % P != 0:
        return False
    if not (0 < r < N and 0 < s <= N // 2):
        return False
    w = pow(s, -1, N)
    pt = _add(_mul(_z(msg) * w % N, G), _mul(r * w % N, (x, y)))
    return pt is not None and pt[0] % N == r


# ---------------------------------------------------------------- cifra para uma chave pública
# ECIES: chave efêmera + ECDH secp256k1, fluxo SHAKE-256 e HMAC-SHA256 (cifra-depois-autentica).
# Formato: SEAL_MAGIC | chave pública efêmera (64 bytes) | nonce (16) | texto cifrado | tag (32).
SEAL_MAGIC = b"SIGMA-SEAL-1"
_SEAL_HEAD = len(SEAL_MAGIC) + 64 + 16


def _point(pub):
    """Chave pública em hex -> ponto da curva; recusa pontos fora da secp256k1 (ataque de curva inválida)."""
    if not isinstance(pub, str) or len(pub) != 128:
        raise ValueError("chave pública inválida")
    try:
        x, y = int(pub[:64], 16), int(pub[64:], 16)
    except ValueError:
        raise ValueError("chave pública inválida") from None
    if not (0 <= x < P and 0 <= y < P) or (y * y - x * x * x - 7) % P:
        raise ValueError("chave pública inválida")
    return x, y


def _seal_keys(priv, point, eph_pub, nonce):
    shared = _mul(priv, point)
    if shared is None:
        raise ValueError("chave pública inválida")
    k = hashlib.sha256(b"sigma-seal-v1" + shared[0].to_bytes(32, "big") + bytes.fromhex(eph_pub) + nonce).digest()
    return hashlib.sha256(k + b"enc").digest(), hashlib.sha256(k + b"mac").digest()


def _xor(data, key):
    n = len(data)
    stream = hashlib.shake_256(key).digest(n) if n else b""
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(n, "big")


def seal(pub: str, data: bytes) -> bytes:
    """Cifra `data` para o dono de `pub`: só a chave privada dele abre (unseal). Quem guarda ou repassa os
    bytes (o nó, as outras máquinas) não consegue ler nem alterar sem ser percebido."""
    point = _point(pub)
    eph = int.from_bytes(os.urandom(32), "big") % (N - 1) + 1
    eph_pub, nonce = pub_of(eph), os.urandom(16)
    enc, mac = _seal_keys(eph, point, eph_pub, nonce)
    body = SEAL_MAGIC + bytes.fromhex(eph_pub) + nonce + _xor(bytes(data), enc)
    return body + hmac.new(mac, body, hashlib.sha256).digest()


def unseal(priv: int, blob: bytes) -> bytes:
    """Abre o que `seal` cifrou para a chave pública de `priv`. ValueError se não for para esta chave,
    se não estiver cifrado ou se algum byte foi alterado."""
    blob = bytes(blob)
    if len(blob) < _SEAL_HEAD + 32 or not blob.startswith(SEAL_MAGIC):
        raise ValueError("conteúdo não está cifrado para uma chave Sigma")
    eph_pub = blob[len(SEAL_MAGIC):len(SEAL_MAGIC) + 64].hex()
    enc, mac = _seal_keys(priv, _point(eph_pub), eph_pub, blob[_SEAL_HEAD - 16:_SEAL_HEAD])
    if not hmac.compare_digest(blob[-32:], hmac.new(mac, blob[:-32], hashlib.sha256).digest()):
        raise ValueError("conteúdo cifrado para outra chave, ou alterado no caminho")
    return _xor(blob[_SEAL_HEAD:-32], enc)


def is_sealed(blob) -> bool:
    return isinstance(blob, (bytes, bytearray)) and bytes(blob[:len(SEAL_MAGIC)]) == SEAL_MAGIC


class Wallet:
    def __init__(self, priv: int):
        self.priv = priv
        self.pub = pub_of(priv)
        self.address = address_of(self.pub)

    @classmethod
    def generate(cls):
        return cls(int.from_bytes(os.urandom(32), "big") % (N - 1) + 1)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            return cls(int(json.load(f)["private_key"], 16))

    @classmethod
    def load_or_create(cls, path):
        if os.path.exists(path):
            return cls.load(path)
        w = cls.generate()
        w.save(path)
        return w

    def save(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"address": self.address, "public_key": self.pub,
                       "private_key": format(self.priv, "064x")}, f, indent=2)

    def sign(self, msg: bytes) -> str:
        return sign(self.priv, msg)

    def unseal(self, blob: bytes) -> bytes:
        return unseal(self.priv, blob)
