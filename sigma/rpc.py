"""JSON-RPC no estilo Ethereum, para a MetaMask enxergar a rede Sigma.

Adicione na MetaMask uma rede personalizada:
    RPC:       http://127.0.0.1:8700/rpc
    Chain ID:  51515
    Moeda:     SIG (18 casas decimais na MetaMask = µSIG × 10^12 no ledger)

Não há gás no Sigma: a rede cobra pelo serviço usado, não pela transação.
As operações próprias do Sigma (relatórios de mineração, registro de máquina, sites, modelos...)
aparecem como chamadas ao SYSTEM_ADDRESS.

Pedidos em lote têm no máximo MAX_BATCH entradas: cada eth_sendRawTransaction custa uma recuperação de
assinatura (secp256k1 em Python), e um único POST não pode segurar o nó por minutos.
"""
from . import __version__
from .chain import TxError, decode_evm, tx_id, tx_summary
from .crypto import norm_address, to_eth
from .evm import SYSTEM_ADDRESS, WEI_PER_USIG

ZERO32 = "0x" + "00" * 32
ZERO_ADDR = "0x" + "00" * 20
MAX_BATCH = 50
LIMIT_CODE = -32005   # "limit exceeded" (EIP-1474): o nó responde HTTP 429


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _hex(n):
    return hex(int(n))


def _addr(v):
    try:
        return norm_address(v)
    except ValueError:
        raise RpcError(-32602, f"endereço inválido: {v}")


def _eth(a):
    """Endereço Sigma -> 0x; o que não for endereço (ex.: relatório de mineração sem remetente) vira zero."""
    try:
        return to_eth(norm_address(a))
    except ValueError:
        return ZERO_ADDR


def _block_number(node, tag):
    if tag in (None, "latest", "pending", "safe", "finalized"):
        return node.height
    if tag == "earliest":
        return 0
    try:
        return int(tag, 16)
    except (TypeError, ValueError):
        raise RpcError(-32602, f"bloco inválido: {tag}")


def _block(node, block, full):
    txs = [_tx(node, t, block, i) if full else "0x" + (t.get("id") or tx_id(t)) for i, t in enumerate(block["txs"])]
    return {"number": _hex(block["height"]), "hash": "0x" + block["hash"], "parentHash": "0x" + block["prev"],
            "timestamp": _hex(block["time"]), "stateRoot": "0x" + block["state_root"],
            "miner": ZERO_ADDR, "gasLimit": _hex(30_000_000), "gasUsed": "0x0", "baseFeePerGas": "0x0",
            "difficulty": "0x0", "totalDifficulty": "0x0", "nonce": "0x0000000000000000", "extraData": "0x",
            "logsBloom": "0x" + "00" * 256, "sha3Uncles": ZERO32, "mixHash": ZERO32, "transactionsRoot": ZERO32,
            "receiptsRoot": ZERO32, "size": _hex(1000), "uncles": [], "transactions": txs}


def _tx(node, tx, block=None, index=0):
    try:
        s = tx_summary(tx)
    except Exception:  # tipo que o resumo não entende: mostra o mínimo em vez de quebrar o bloco inteiro
        s = {}
    to = s.get("to")
    out = {"hash": "0x" + (tx.get("id") or tx_id(tx)), "from": _eth(s.get("from")),
           # o relatório de mineração não tem nonce nem assinatura: aparece com nonce 0
           "to": _eth(to) if to else SYSTEM_ADDRESS, "value": _hex((s.get("amount") or 0) * WEI_PER_USIG),
           "nonce": _hex(s.get("nonce") or 0), "gas": _hex(21000),
           "gasPrice": "0x0", "input": "0x", "type": "0x0", "chainId": _hex(node.state.chain_id),
           "v": "0x0", "r": "0x0", "s": "0x0",
           "blockHash": "0x" + block["hash"] if block else None,
           "blockNumber": _hex(block["height"]) if block else None,
           "transactionIndex": _hex(index) if block else None}
    if tx.get("type") == "evm":
        e = decode_evm(tx["data"]["raw"])
        out.update({"to": e["to"], "value": _hex(e["value"]), "input": "0x" + e["data"].hex(),
                    "gas": _hex(e["gas"]), "type": _hex(e["kind"]), "v": _hex(e["v"]), "r": _hex(e["r"]),
                    "s": _hex(e["s"])})
    return out


def _txid_param(p):
    if not p or not isinstance(p[0], str) or len(p[0]) > 130:
        raise RpcError(-32602, "hash de transação inválido")
    return p[0].removeprefix("0x").lower()


def _find_tx(node, txid):
    """(tx, bloco, posição) de uma transação incluída, ou (tx, None, 0) se ainda está na fila. Blocos antigos
    vêm do disco (node.block), fora do lock da chain."""
    pos = node.tx_pos.get(txid)
    if pos:
        block = node.block(pos[0])
        if block is not None and pos[1] < len(block["txs"]):
            return block["txs"][pos[1]], block, pos[1]
        return None, None, 0
    ids = getattr(node, "mempool_ids", None)
    if ids is not None and txid not in ids:
        return None, None, 0
    tx = next((t for t in list(node.mempool) if t.get("id") == txid), None)
    return tx, None, 0


def handle(node, method, params, ip=None):
    p = list(params or [])
    st = node.state
    if method in ("eth_chainId",):
        return _hex(st.chain_id)
    if method == "net_version":
        return str(st.chain_id)
    if method == "web3_clientVersion":
        return f"sigma/{__version__}"
    if method in ("net_listening",):
        return True
    if method == "eth_syncing":
        return False
    if method == "eth_blockNumber":
        return _hex(node.height)
    if method == "eth_getBalance":
        return _hex(st.balances.get(_addr(p[0]), 0) * WEI_PER_USIG)
    if method == "eth_getTransactionCount":
        a = _addr(p[0])
        with node.lock:
            src = node.pending if len(p) > 1 and p[1] == "pending" else st
            return _hex(src.nonces.get(a, 0))
    if method in ("eth_gasPrice", "eth_maxPriorityFeePerGas"):
        return "0x0"
    if method == "eth_feeHistory":
        try:
            n = int(p[0], 16) if isinstance(p[0], str) else int(p[0])
        except (TypeError, ValueError, IndexError):
            raise RpcError(-32602, "número de blocos inválido")
        n = max(1, min(n, 1024))
        # rewardPercentiles: no máximo 100 números. Sem isto, uma string de 250 KB em p[2] faz o nó montar
        # uma resposta de centenas de MB (len(p[2]) × n) e trava a produção (achado rpc-feehistory-amplificacao)
        pct = p[2] if len(p) > 2 and p[2] is not None else []
        if not isinstance(pct, list) or len(pct) > 100 or not all(isinstance(x, (int, float)) for x in pct):
            raise RpcError(-32602, "rewardPercentiles deve ser uma lista de no máximo 100 números")
        return {"oldestBlock": _hex(max(0, node.height - n + 1)), "baseFeePerGas": ["0x0"] * (n + 1),
                "gasUsedRatio": [0.0] * n, "reward": [["0x0"] * len(pct)] * n}
    if method == "eth_estimateGas":
        data = (p[0] or {}).get("data") or (p[0] or {}).get("input") or "0x"
        return _hex(21000 + 16 * (len(data) - 2) // 2)
    if method in ("eth_getCode", "eth_call"):
        return "0x"
    if method == "eth_accounts":
        return []
    if method in ("eth_getBlockByNumber", "eth_getBlockByHash"):
        if not p:
            raise RpcError(-32602, "informe o bloco")
        if method == "eth_getBlockByHash":
            # só a cauda que está na memória (os últimos blocos); hash mais antigo responde null
            h = str(p[0]).removeprefix("0x").lower()
            block = next((b for b in reversed(list(node.blocks)) if b["hash"] == h), None)
        else:
            n = _block_number(node, p[0])
            block = node.block(n) if 0 <= n <= node.height else None   # os antigos vêm do disco
        return _block(node, block, bool(len(p) > 1 and p[1])) if block else None
    if method == "eth_sendRawTransaction":
        if not p or not isinstance(p[0], str):
            raise RpcError(-32602, "informe a transação assinada (hex)")
        raw = p[0].removeprefix("0x")
        try:
            txid = node.submit_tx({"type": "evm", "data": {"raw": raw}}, ip=ip)
        except TxError as e:   # passou do limite da conexão (--public): o nó responde 429
            raise RpcError(LIMIT_CODE if getattr(e, "status", None) == 429 else -32000, str(e))
        return "0x" + txid
    if method in ("eth_getTransactionByHash", "eth_getTransactionReceipt"):
        tx, block, index = _find_tx(node, _txid_param(p))
        if tx is None:
            return None
        if method == "eth_getTransactionByHash":
            return _tx(node, tx, block, index)
        if not block:
            return None
        t = _tx(node, tx, block, index)
        return {"transactionHash": t["hash"], "transactionIndex": t["transactionIndex"], "blockHash": t["blockHash"],
                "blockNumber": t["blockNumber"], "from": t["from"], "to": t["to"], "cumulativeGasUsed": "0x5208",
                "gasUsed": "0x5208", "effectiveGasPrice": "0x0", "contractAddress": None, "logs": [],
                "logsBloom": "0x" + "00" * 256, "status": "0x1", "type": t["type"]}
    if method == "eth_getLogs":
        return []
    raise RpcError(-32601, f"método não suportado: {method}")


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _one(node, req, ip):
    rid = req.get("id") if isinstance(req, dict) else None
    try:
        if not isinstance(req, dict):   # inclui lote dentro de lote: o limite não se multiplica
            raise RpcError(-32600, "pedido inválido")
        return {"jsonrpc": "2.0", "id": rid, "result": handle(node, req.get("method"), req.get("params"), ip)}
    except RpcError as e:
        return _error(rid, e.code, str(e))
    except Exception as e:
        return _error(rid, -32603, str(e))


def dispatch(node, req, ip=None):
    """Recebe um pedido JSON-RPC (ou um lote de até MAX_BATCH) e devolve a resposta. `ip` é a conexão de quem
    pediu: no nó --public, as transações enviadas contam no limite dela."""
    if isinstance(req, list):
        if not req:
            return _error(None, -32600, "lote vazio")
        if len(req) > MAX_BATCH:
            return _error(None, -32600, f"lote grande demais (máximo {MAX_BATCH})")
        return [_one(node, r, ip) for r in req]
    return _one(node, req, ip)
