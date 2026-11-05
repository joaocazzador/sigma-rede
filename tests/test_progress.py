"""Progresso dos pedidos de IA (fora da chain): a máquina manda ao nó a última notícia (texto até agora, prévia,
porcentagem, sinal de vida), assinada por ela e cifrada para quem pediu; o nó só aceita da máquina do pedido e
guarda a mais nova; só quem pediu consegue ler."""
import json
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import node  # noqa: E402
from sigma.chain import canon  # noqa: E402
from sigma.crypto import progress_message, seal  # noqa: E402
from test_chain_v6 import hosted  # noqa: E402
from test_chain_v21 import CLIENT, MA, MB, ai_job  # noqa: E402


class FakeNode:
    put_progress = node.Node.put_progress
    get_progress = node.Node.get_progress

    def __init__(self, state):
        self.state, self.lock, self.progress, self._progress_trim = state, threading.RLock(), {}, 0.0


def msg(wallet, jid, seq, info, pub=CLIENT.pub):
    data = seal(pub, canon(info)).hex()
    return {"job_id": jid, "machine": wallet.address, "seq": seq, "data": data,
            "sig": wallet.sign(progress_message(jid, seq, data))}


class TestProgress(unittest.TestCase):
    def setUp(self):
        net = hosted(MA, MB)
        self.jid = ai_job(net, MA)
        net.block()
        net.send(MA, "claim_job", {"job_id": self.jid})
        net.block()
        self.node = FakeNode(net.state)

    def test_machine_posts_client_reads(self):
        self.node.put_progress(msg(MA, self.jid, 1, {"stage": "escrevendo", "text": "Olá"}))
        p = self.node.get_progress(self.jid)
        info = json.loads(CLIENT.unseal(bytes.fromhex(p["data"])))
        self.assertEqual((info["text"], p["machine"]), ("Olá", MA.address))

    def test_only_newest_is_kept(self):
        self.node.put_progress(msg(MA, self.jid, 5, {"pct": 50}))
        self.node.put_progress(msg(MA, self.jid, 3, {"pct": 30}))      # atrasada: fica a 5
        info = json.loads(CLIENT.unseal(bytes.fromhex(self.node.get_progress(self.jid)["data"])))
        self.assertEqual(info["pct"], 50)

    def test_other_machine_or_bad_signature_refused(self):
        with self.assertRaisesRegex(ValueError, "não está neste pedido"):
            self.node.put_progress(msg(MB, self.jid, 1, {"pct": 1}))
        m = msg(MA, self.jid, 1, {"pct": 1})
        m["seq"] = 2                                                     # assinatura era da seq 1
        with self.assertRaisesRegex(ValueError, "assinatura"):
            self.node.put_progress(m)
        with self.assertRaisesRegex(ValueError, "não existe"):
            self.node.put_progress(msg(MA, "0" * 64, 1, {"pct": 1}))

    def test_nobody_else_can_read(self):
        self.node.put_progress(msg(MA, self.jid, 1, {"text": "segredo"}))
        with self.assertRaises(Exception):
            MB.unseal(bytes.fromhex(self.node.get_progress(self.jid)["data"]))


if __name__ == "__main__":
    unittest.main()
