"""Prova de trabalho do Sigma: o que o minerador calcula e o que a chain confere.

Cada janela tem uma seed (derivada do bloco que a abriu). O minerador procura
nonces cujo hash fique abaixo do alvo de `bits` bits; cada um é um "share".
A identidade de quem minera (o endereço, ou "endereço.worker" quando vários
computadores mineram para a mesma carteira) faz parte do hash, então um share
achado para A não vale para B: por isso o relatório de trabalho não precisa de
assinatura. Os bits também entram no hash: um share só vale no nível que o
minerador escolheu antes de calcular, sem "subir de nível" depois.

scrypt (N=1024, r=1, p=1) pede memória a cada hash, o que aproxima o PC comum
de máquinas especializadas, e mesmo assim é barato de verificar.
"""
import hashlib
from functools import lru_cache

POW_SALT = b"sigma-pow-v2"


def pow_hash(seed: str, ident: str, bits: int, nonce: int) -> int:
    """scrypt(N=1024, r=1, p=1) sobre seed, identidade, bits e nonce: pesado em memória, barato de verificar."""
    data = f"{seed}:{ident}:{bits}:{nonce}".encode()
    return int.from_bytes(hashlib.scrypt(data, salt=POW_SALT, n=1024, r=1, p=1, dklen=32), "big")


def target(bits: int) -> int:
    """Um hash é share quando fica abaixo disto (o minerador compara direto, sem passar pelo cache)."""
    return 1 << (256 - bits)


@lru_cache(maxsize=262144)          # mempool + bloco verificam o mesmo share: só calcula uma vez
def share_ok(seed: str, ident: str, nonce: int, bits: int) -> bool:
    return pow_hash(seed, ident, bits, nonce) < target(bits)


def report_points(n_shares: int, bits: int) -> int:
    return n_shares << bits          # valor esperado = número de hashes feitos
