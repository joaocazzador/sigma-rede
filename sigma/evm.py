"""Compatibilidade com carteiras Ethereum (MetaMask, Rabby, ethers.js...).

A MetaMask assina transações no formato do Ethereum. O Sigma aceita essas
transações como estão (tipo "evm"): recupera quem assinou pela assinatura,
confere o chain id e o nonce, e então:

- `to` = endereço comum  -> transferência de SIG
- `to` = SYSTEM_ADDRESS  -> `data` traz uma operação do Sigma em JSON, ex.
  {"type": "fund_model", "data": {"name": "qwen-coder", "amount": 5000000}}

Valores: a MetaMask usa 18 casas decimais; o ledger usa µSIG (6 casas).
"""
import json

from .crypto import N, address_of, keccak256, recover_pub

WEI_PER_USIG = 10 ** 12
SYSTEM_ADDRESS = "0x000000000000000000000000000000000000516d"   # "Sm"
DEFAULT_CHAIN_ID = 51515
MAX_RLP_DEPTH = 16   # uma transação de verdade tem no máximo 4 níveis de listas (a tx e a lista de acesso)


def rlp_decode(data: bytes):
    item, end = _rlp_item(data, 0)
    if end != len(data):
        raise ValueError("RLP com bytes sobrando")
    return item


def _rlp_item(b, i, depth=0):
    if i >= len(b):
        raise ValueError("RLP truncado")
    p = b[i]
    if p < 0x80:
        return b[i:i + 1], i + 1
    if p <= 0xB7:
        end = i + 1 + p - 0x80
        return b[i + 1:end], end
    if p <= 0xBF:
        ll = p - 0xB7
        start = i + 1 + ll
        end = start + int.from_bytes(b[i + 1:start], "big")
        return b[start:end], end
    if p <= 0xF7:
        start, end = i + 1, i + 1 + p - 0xC0
    else:
        ll = p - 0xF7
        start = i + 1 + ll
        end = start + int.from_bytes(b[i + 1:start], "big")
    if end > len(b):
        raise ValueError("RLP truncado")
    if depth >= MAX_RLP_DEPTH:   # milhares de listas uma dentro da outra: lixo, recusado sem recursão funda
        raise ValueError("listas aninhadas demais")
    items, j = [], start
    while j < end:
        it, j = _rlp_item(b, j, depth + 1)
        items.append(it)
    return items, end


def rlp_encode(item) -> bytes:
    if isinstance(item, int):
        item = item.to_bytes((item.bit_length() + 7) // 8, "big") if item else b""
    if isinstance(item, (bytes, bytearray)):
        if len(item) == 1 and item[0] < 0x80:
            return bytes(item)
        return _rlp_len(len(item), 0x80) + bytes(item)
    payload = b"".join(rlp_encode(x) for x in item)
    return _rlp_len(len(payload), 0xC0) + payload


def _rlp_len(n, offset):
    if n < 56:
        return bytes([offset + n])
    nb = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(nb)]) + nb


def _int(b):
    return int.from_bytes(b, "big")


def decode_tx(raw: bytes):
    """Decodifica uma transação Ethereum assinada (legacy/EIP-155, EIP-2930 ou EIP-1559)."""
    if raw and raw[0] in (1, 2):
        kind, fields = raw[0], rlp_decode(raw[1:])
        if not isinstance(fields, list):
            raise ValueError("transação malformada")
        if kind == 2 and len(fields) == 12:
            chain_id, nonce, _tip, gas_price, gas, to, value, data, _al, v, r, s = fields
        elif kind == 1 and len(fields) == 11:
            chain_id, nonce, gas_price, gas, to, value, data, _al, v, r, s = fields
        else:
            raise ValueError("transação malformada")
        z = keccak256(bytes([kind]) + rlp_encode(fields[:-3]))
        chain_id, recid = _int(chain_id), _int(v)
    else:
        fields = rlp_decode(raw)
        if not isinstance(fields, list) or len(fields) != 9:
            raise ValueError("transação malformada")
        nonce, gas_price, gas, to, value, data, v, r, s = fields
        v = _int(v)
        if v < 35:
            raise ValueError("transações sem chain id (pré EIP-155) não são aceitas")
        chain_id, recid = (v - 35) // 2, (v - 35) % 2
        z = keccak256(rlp_encode(fields[:6] + [chain_id, 0, 0]))
        kind = 0
    r, s = _int(r), _int(s)
    if s > N // 2:
        raise ValueError("assinatura com s alto não é aceita")
    if to and len(to) != 20:
        raise ValueError("destino inválido")
    pub = recover_pub(z, r, s, recid)
    return {"kind": kind, "chain_id": chain_id, "nonce": _int(nonce), "gas_price": _int(gas_price),
            "gas": _int(gas), "to": "0x" + to.hex() if to else None, "value": _int(value),
            "data": bytes(data), "pub": pub, "from": address_of(pub), "v": recid, "r": r, "s": s,
            "hash": keccak256(raw).hex()}


BAD_CALL = "data deve ser um JSON {type, data} em UTF-8 válido e com números dentro do limite"


def system_call(data: bytes):
    """Lê a operação do Sigma embutida no campo data de uma transação para SYSTEM_ADDRESS.

    O JSON vem de dentro da assinatura, então ninguém o "limpa" antes: um \\ud800 (pedaço de caractere) ou
    um número de milhares de dígitos passariam pelo json.loads e travariam o bloco que tentasse gravá-los.
    Por isso a operação inteira precisa passar pelo canon() aqui, antes de chegar ao estado."""
    try:
        call = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("data deve ser um JSON {type, data} em UTF-8")
    except (ValueError, RecursionError):   # inteiro grande demais para virar número, aninhamento sem fim
        raise ValueError(BAD_CALL) from None
    if not isinstance(call, dict) or not isinstance(call.get("type"), str) or not isinstance(call.get("data"), dict):
        raise ValueError("data deve ser um JSON {type, data}")
    from .chain import canon   # aqui dentro: a chain importa este módulo
    try:
        canon(call)
    except (ValueError, TypeError, RecursionError):   # UnicodeEncodeError é um ValueError
        raise ValueError(BAD_CALL) from None
    return call["type"], call["data"]
