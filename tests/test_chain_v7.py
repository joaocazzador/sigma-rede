"""Regras v7 (Sigma 2.12): corrida e vigia.

Corrida: um pedido direcionado chama até 2 máquinas a mais com o mesmo modelo; a primeira que entrega fecha a
corrida; a falha ou o sumiço de uma só tira ela da corrida.
Vigia: a entrega não paga na hora. O pedido fica "review", a rede sorteia uma vigia (que hospeda o modelo e não
correu), e ela aprova (paga 95% à vencedora e 5% à vigia, reputação para as duas) ou reprova (o valor volta, a
vencedora perde reputação). O cliente contesta uma vez (outra vigia, mais prazo). Sem veredito, paga sozinho."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain  # noqa: E402
from sigma.chain import TxError, sha  # noqa: E402
from test_chain import H, WALLETS, supply  # noqa: E402
from test_chain_v6 import hosted  # noqa: E402
from test_chain_v21 import CLIENT, MA, MB, ai_job  # noqa: E402

MC, MD = WALLETS[12], WALLETS[13]
S2, S3 = sha(b"para B"), sha(b"para C")
USAGE = {"tokens_in": 10, "tokens_out": 10}


class Base(unittest.TestCase):
    def race(self, watchers=True):
        net = hosted(MA, MB, MC, MD, rules=7) if watchers else hosted(MA, MB, MC, rules=7)
        jid = ai_job(net, MA, racers={MB.address: S2, MC.address: S3})
        net.block()
        return net, jid

    def deliver(self, net, jid, m):
        net.send(m, "claim_job", {"job_id": jid})
        net.block()
        net.send(m, "submit_result", {"job_id": jid, "result_hash": H, "usage": USAGE})
        return net.block()


class TestRace(Base):
    def test_first_to_deliver_goes_to_review(self):
        net, jid = self.race()
        net.send(MA, "claim_job", {"job_id": jid})
        net.block()
        s = self.deliver(net, jid, MB)                    # MB corre com o pedido já "running" e entrega antes
        job = s.jobs[jid]
        self.assertEqual((job["status"], job["winner"], job["review"]["machine"]), ("review", MB.address, MB.address))
        self.assertEqual(job["review"]["watcher"], MD.address)   # a única que hospeda e não correu
        self.assertEqual(job["result_hash"], H)                  # o cliente já pode abrir a entrega
        with self.assertRaisesRegex(TxError, "encerrado"):
            net.send(MA, "submit_result", {"job_id": jid, "result_hash": H, "usage": USAGE})
        self.assertEqual(supply(s), s.mining["minted"])

    def test_one_failure_keeps_the_race(self):
        net, jid = self.race()
        net.send(MA, "claim_job", {"job_id": jid})
        net.send(MC, "claim_job", {"job_id": jid})
        net.block()
        net.send(MA, "submit_result", {"job_id": jid, "result_hash": H, "usage": {}, "failed": True})
        s = net.block()
        self.assertEqual((s.jobs[jid]["status"], s.jobs[jid]["out"]), ("running", [MA.address]))
        net.send(MC, "submit_result", {"job_id": jid, "result_hash": H, "usage": USAGE})
        self.assertEqual(net.block().jobs[jid]["review"]["machine"], MC.address)

    def test_all_fail_refunds(self):
        net, jid = self.race()
        for m in (MA, MB, MC):
            net.send(m, "claim_job", {"job_id": jid})
        net.block()
        before = net.state.bal(CLIENT.address)
        for m in (MA, MB, MC):
            net.send(m, "submit_result", {"job_id": jid, "result_hash": H, "usage": {}, "failed": True})
        s = net.block()
        self.assertEqual(s.jobs[jid]["status"], "failed")
        self.assertEqual(s.bal(CLIENT.address) - before, s.jobs[jid]["escrow"])

    def test_outsider_cannot_claim_and_bad_racers_refused(self):
        net, jid = self.race()
        with self.assertRaisesRegex(TxError, "outra máquina|não registrada"):
            net.send(WALLETS[14], "claim_job", {"job_id": jid})
        for racers, msg in (({MA.address: S2}, "repetida"), ({}, "racers"),
                            ({MB.address: S2, MC.address: S3, MD.address: S2}, "racers"),
                            ({MB.address: "x"}, "spec_hash")):
            with self.subTest(msg), self.assertRaisesRegex(TxError, msg):
                ai_job(net, MA, racers=racers)
        with self.assertRaisesRegex(TxError, "direcionado"):
            ai_job(net, None, racers={MB.address: S2})

    def test_racers_unknown_before_v7(self):
        net = hosted(MA, MB, rules=6)
        with self.assertRaisesRegex(TxError, "campos desconhecidos"):
            ai_job(net, MA, racers={MB.address: S2})

    def test_nobody_left_expires(self):
        net, jid = self.race()
        net.send(MA, "claim_job", {"job_id": jid})
        net.block()
        s = net.block(dt=chain.CLAIM_TIMEOUT + chain.TARGET_TIMEOUT + 5)
        self.assertEqual(s.jobs[jid]["status"], "expired")
        self.assertEqual(supply(s), s.mining["minted"])


class TestWatch(Base):
    def reviewed(self):
        net, jid = self.race()
        s = self.deliver(net, jid, MA)
        return net, jid, s

    def test_watcher_approves_and_takes_five_percent(self):
        net, jid, s = self.reviewed()
        payout_w = s.machines[MD.address]["payout"]
        before, rep_w = s.bal(payout_w), s.machines[MD.address]["reputation"]
        with self.assertRaisesRegex(TxError, "não é a vigia"):
            net.send(MB, "review_job", {"job_id": jid, "verdict": "ok"})
        net.send(MD, "review_job", {"job_id": jid, "verdict": "ok"})
        s = net.block()
        job = s.jobs[jid]
        self.assertEqual((job["status"], job["verdict"]), ("done", "ok"))
        self.assertEqual(job["watch_fee"], job["cost"] * chain.WATCH_BPS // 10000)
        self.assertEqual(s.bal(payout_w) - before, job["watch_fee"])
        self.assertEqual(s.machines[MD.address]["reputation"], rep_w + 1)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_watcher_rejects_refund_and_penalty(self):
        net, jid, s = self.reviewed()
        before, rep = s.bal(CLIENT.address), s.machines[MA.address]["reputation"]
        net.send(MD, "review_job", {"job_id": jid, "verdict": "bad"})
        s = net.block()
        job = s.jobs[jid]
        self.assertEqual((job["status"], job["verdict"]), ("rejected", "bad"))
        self.assertEqual(s.bal(CLIENT.address) - before, job["escrow"])
        self.assertEqual(s.machines[MA.address]["reputation"], rep - chain.BAD_REPUTATION)
        self.assertNotIn(MA.address, job["payments"])
        self.assertEqual(supply(s), s.mining["minted"])

    def test_no_verdict_pays_after_the_deadline(self):
        net, jid, s = self.reviewed()
        s = net.block(dt=chain.REVIEW_SECONDS + 5)
        job = s.jobs[jid]
        self.assertEqual((job["status"], job["verdict"]), ("done", "auto"))
        self.assertIn(MA.address, job["payments"])
        self.assertNotIn("watch_fee", job)
        self.assertEqual(supply(s), s.mining["minted"])

    def test_no_watcher_available_still_pays(self):
        net, jid = self.race(watchers=False)
        s = self.deliver(net, jid, MA)
        self.assertIsNone(s.jobs[jid]["review"]["watcher"])
        s = net.block(dt=chain.REVIEW_SECONDS + 5)
        self.assertEqual(s.jobs[jid]["status"], "done")

    def test_client_disputes_once(self):
        net, jid, s = self.reviewed()
        with self.assertRaisesRegex(TxError, "só quem pediu"):
            net.send(MB, "dispute_job", {"job_id": jid})
        net.send(CLIENT, "dispute_job", {"job_id": jid})
        s = net.block()
        rv = s.jobs[jid]["review"]
        self.assertTrue(rv["disputed"])
        with self.assertRaisesRegex(TxError, "já foi contestado"):
            net.send(CLIENT, "dispute_job", {"job_id": jid})
        # só MD podia vigiar (as outras correram): a contestação mantém a vigia e dá mais prazo
        self.assertEqual(rv["watcher"], MD.address)
        self.assertGreater(rv["until"], s.now + chain.REVIEW_SECONDS - 5)

    def test_review_unknown_before_v7(self):
        net = hosted(MA, MB, rules=6)
        with self.assertRaisesRegex(TxError, "desconhecido"):
            net.send(CLIENT, "dispute_job", {"job_id": "0" * 64})


if __name__ == "__main__":
    unittest.main()
