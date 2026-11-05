"""Regras v6 (Sigma 2.11): quem pediu libera (release_job) o pedido de uma máquina que sumiu da rede depois de
pegá-lo. O valor volta na hora, a máquina leva a falta, e o app passa o pedido para a próxima."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma.chain import TxError  # noqa: E402
from test_chain import machine, model, supply  # noqa: E402
from test_chain_v21 import CLIENT, MA, MB, PAYOUTS, SPONSOR, ai_job  # noqa: E402
from test_chain_v3 import V3Net  # noqa: E402


def hosted(*machines, rules=6):
    """Como o hosted da v2.1 (janela de 60 s, 'modelo-a' nas máquinas), numa rede que já nasce na v6."""
    net = V3Net(rules=rules, window_seconds=60, epoch_seconds=600)
    net.fund(CLIENT.address, SPONSOR.address)
    for m, p in zip(machines, PAYOUTS):
        net.send(m, "register_machine", machine(p, pub=m.pub))
    net.send(SPONSOR, "register_model", model("modelo-a", replicas=len(machines)))
    net.block()
    net.step(*machines)
    for m in machines:
        net.send(m, "pin_model", {"name": "modelo-a"})
    net.block()
    return net


class TestRelease(unittest.TestCase):
    def claimed(self):
        net = hosted(MA, MB)
        jid = ai_job(net, MA)
        net.block()
        net.send(MA, "claim_job", {"job_id": jid})
        net.block()
        return net, jid

    def test_machine_still_signaling_cannot_be_released(self):
        net, jid = self.claimed()
        with self.assertRaisesRegex(TxError, "ainda está sinalizando"):
            net.send(CLIENT, "release_job", {"job_id": jid})

    def test_gone_machine_is_released_and_refunded(self):
        net, jid = self.claimed()
        net.step(MB)            # MA some: não sinaliza nesta janela
        net.step(MB)            # nem na seguinte
        before = net.state.bal(CLIENT.address)
        rep = net.state.machines[MA.address]["reputation"]
        net.send(CLIENT, "release_job", {"job_id": jid})
        s = net.block()
        job = s.jobs[jid]
        self.assertEqual((job["status"], job.get("missed")), ("expired", True))
        self.assertEqual(s.bal(CLIENT.address) - before, job["escrow"])
        self.assertEqual(s.machines[MA.address]["reputation"], rep - 1)
        self.assertEqual(supply(s), s.mining["minted"])
        with self.assertRaisesRegex(TxError, "encerrado"):
            net.send(CLIENT, "release_job", {"job_id": jid})

    def test_only_the_client_and_only_claimed(self):
        net = hosted(MA, MB)
        jid = ai_job(net, MA)
        net.block()
        with self.assertRaisesRegex(TxError, "ainda não pegou"):
            net.send(CLIENT, "release_job", {"job_id": jid})
        net.send(MA, "claim_job", {"job_id": jid})
        net.block()
        with self.assertRaisesRegex(TxError, "só quem pediu"):
            net.send(MB, "release_job", {"job_id": jid})

    def test_unknown_before_v6(self):
        net = V3Net(rules=5)
        net.fund(CLIENT.address)
        with self.assertRaisesRegex(TxError, "desconhecido"):
            net.send(CLIENT, "release_job", {"job_id": "0" * 64})


if __name__ == "__main__":
    unittest.main()
