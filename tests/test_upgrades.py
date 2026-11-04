"""Atualizações das regras sem recomeçar a rede (docs/ATUALIZACOES.md).

O "código novo" dos testes é o código de verdade com chain.RULES = 2, a atualização marcada em chain.SCHEDULE e
uma regra que só vale com rule(2): ela grava em ext quantos blocos já rodaram na v2 (o que muda o state root dali
em diante, como uma regra de verdade mudaria)."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain  # noqa: E402
from sigma import node as nodemod  # noqa: E402
from sigma.chain import UNIT, State, UpgradeRequired, canon, make_block, sha  # noqa: E402
from sigma.crypto import Wallet  # noqa: E402
from test_node import Net  # noqa: E402

real_end_block = State.end_block


def end_block_v2(self):
    real_end_block(self)
    if self.rule(2):
        self.ext["blocos_v2"] = self.ext.get("blocos_v2", 0) + 1


@contextlib.contextmanager
def new_code(genesis, at):
    """O Sigma v2: conhece as regras v2 e sabe que elas entram no bloco `at` desta rede."""
    with mock.patch.object(chain, "RULES", 2), \
            mock.patch.object(chain, "SCHEDULE", {sha(canon(genesis)): {2: at}}), \
            mock.patch.object(State, "end_block", end_block_v2):
        yield


def follower(test, net):
    d = tempfile.mkdtemp(prefix="sgf")
    test.addCleanup(shutil.rmtree, d, True)

    def open_node():
        n = nodemod.Node(d, net.genesis, None, peers=["http://validador"])
        n.clock = lambda: net.t
        test.addCleanup(n.forum.db.close)
        return n
    return open_node


def serve(net):
    """http_json falso: o seguidor sincroniza direto do nó produtor do teste."""
    def http_json(url, *a, **kw):
        start = int(url.split("from=")[1])
        return {"blocks": net.node.blocks_from(start)}
    return mock.patch.object(nodemod, "http_json", http_json)


class TestScheduledUpgrade(unittest.TestCase):
    def setUp(self):
        self.net = Net(self)
        self.alice = Wallet.generate()
        self.net.fund(self.alice)
        self.net.send(self.alice, "transfer", {"to": Wallet.generate().address, "amount": 5 * UNIT})
        self.assertTrue(self.net.block())
        self.h0 = self.net.node.height
        self.at = self.h0 + 3

    def test_new_rule_enters_at_height_without_reset(self):
        net = self.net
        root, bal = net.node.state.root(), net.node.state.bal(self.alice.address)
        with new_code(net.genesis, self.at):
            n = net.reopen()                     # o Sigma novo relê a chain inteira gravada pelo antigo
            self.assertEqual((n.height, n.state.root(), n.state.rules), (self.h0, root, 1))
            self.assertIsNone(n.upgrade_required)
            self.assertEqual(n.state.bal(self.alice.address), bal)
            for _ in range(4):
                self.assertTrue(net.block())
            n = net.node
            rules = {h: n.block(h)["rules"] for h in range(1, n.height + 1)}
            self.assertEqual({h for h, r in rules.items() if r == 2}, set(range(self.at, n.height + 1)))
            self.assertEqual(n.state.ext, {"blocos_v2": n.height - self.at + 1})
            self.assertEqual(n.state.upgrade["history"][-1],
                             {"rules": 2, "height": self.at, "time": n.block(self.at)["time"]})
            self.assertGreaterEqual(n.state.bal(self.alice.address), bal)   # ninguém perdeu saldo
            head, root2 = n.head["hash"], n.state.root()
            n = net.reopen()                     # reexecuta passando pela altura da atualização
            self.assertEqual((n.head["hash"], n.state.root(), n.state.rules), (head, root2, 2))

    def test_status_announces_upcoming_upgrade(self):
        with new_code(self.net.genesis, self.at):
            n = self.net.reopen()
            self.assertEqual(chain.upcoming_upgrades(n.state.genesis_hash, n.height), [{"rules": 2, "height": self.at}])
            for _ in range(3):
                self.assertTrue(self.net.block())
            self.assertEqual(chain.upcoming_upgrades(n.state.genesis_hash, self.net.node.height), [])

    def test_outdated_follower_stops_and_resumes_after_update(self):
        net, open_follower = self.net, follower(self, self.net)
        with new_code(net.genesis, self.at):
            net.reopen()
            for _ in range(4):
                self.assertTrue(net.block())
        head = net.node.head["hash"]
        b = open_follower()                      # Sigma antigo, sem saber da v2
        err = io.StringIO()
        with serve(net), mock.patch("sys.stderr", err):
            b.sync_from_peers()
            b.sync_from_peers()                  # parado: nem tenta de novo
        self.assertEqual(b.height, self.at - 1)
        self.assertEqual(b.upgrade_required["rules"], 2)
        self.assertEqual(b.upgrade_required["height"], self.at)
        self.assertIn("baixe a versão nova", b.upgrade_required["message"])
        self.assertEqual(err.getvalue().count("baixe a versão nova"), 1)
        self.assertFalse(b.produce_block())
        b.forum.db.close()
        with new_code(net.genesis, self.at), serve(net):
            b = open_follower()                  # o mesmo computador, com o Sigma atualizado
            self.assertIsNone(b.upgrade_required)
            self.assertEqual(b.height, self.at - 1)
            b.sync_from_peers()
            self.assertEqual((b.head["hash"], b.state.rules), (head, 2))
            self.assertEqual(b.state.root(), net.node.state.root())

    def test_old_code_reading_newer_chain_file_stops_without_losing_blocks(self):
        net = self.net
        # com um snapshot gravado já na v2 (reinício rápido): o Sigma antigo não pode confiar nele
        with new_code(net.genesis, self.at), mock.patch.object(nodemod, "SNAPSHOT_EVERY", self.at + 1):
            net.reopen()
            for _ in range(4):
                self.assertTrue(net.block())
            head, height = net.node.head["hash"], net.node.height
        self.assertTrue(os.path.exists(net.node.snapshot_file))
        with mock.patch("sys.stderr", io.StringIO()):
            n = net.reopen()                     # voltou para o Sigma antigo por engano
        self.assertEqual((n.height, n.upgrade_required["rules"]), (self.at - 1, 2))
        self.assertFalse(net.block())            # não grava nada por cima dos blocos da v2
        with new_code(net.genesis, self.at):
            n = net.reopen()
            self.assertEqual((n.height, n.head["hash"], n.upgrade_required), (height, head, None))

    def test_block_rules_must_match_the_state(self):
        net, open_follower = self.net, follower(self, self.net)
        b = open_follower()
        with serve(net):
            b.sync_from_peers()
        self.assertEqual(b.height, net.node.height)
        prev = b.head
        with mock.patch.object(chain, "RULES", 2):   # conhece a v2, mas nenhuma está marcada para esta rede
            forged = make_block(net.producer, prev["height"] + 1, prev["hash"], [], "0" * 64, prev["time"] + 1,
                                rules=2)
            with self.assertRaisesRegex(ValueError, "versão das regras divergente"):
                b._apply_block(forged, check_clock=False)
        with self.assertRaises(UpgradeRequired):  # o código de agora nem conhece a v2
            b._apply_block(forged, check_clock=False)
        bad = make_block(net.producer, prev["height"] + 1, prev["hash"], [], "0" * 64, prev["time"] + 1, rules=0)
        with self.assertRaisesRegex(ValueError, "versão das regras do bloco inválida"):
            b._apply_block(bad, check_clock=False)
        self.assertIsNone(b.upgrade_required)
        self.assertEqual(b.height, prev["height"])


class TestRulesBasics(unittest.TestCase):
    GENESIS = {"network": "sigma", "chain_id": 51515, "time": 1793700000.0, "validators": [], "alloc": {}}

    def test_genesis_rules(self):
        self.assertEqual(State(self.GENESIS).rules, 1)
        self.assertEqual(State(dict(self.GENESIS, rules=1)).rules, 1)
        with self.assertRaises(UpgradeRequired):
            State(dict(self.GENESIS, rules=chain.RULES + 1))
        for bad in (0, -1, "1", 1.5, True):
            with self.assertRaises(ValueError):
                State(dict(self.GENESIS, rules=bad))

    def test_schedule_never_goes_back_and_is_per_network(self):
        with mock.patch.object(chain, "RULES", 3), \
                mock.patch.object(chain, "SCHEDULE", {"rede-a": {2: 10, 3: 20}}):
            self.assertEqual([chain.scheduled_rules("rede-a", h, 1) for h in (9, 10, 19, 20, 99)], [1, 2, 2, 3, 3])
            self.assertEqual(chain.scheduled_rules("rede-a", 5, 3), 3)
            self.assertEqual(chain.scheduled_rules("rede-b", 99, 1), 1)

    def test_frozen_keys(self):
        """Mudar estas chaves muda o state root de todos os blocos antigos (a rede teria de recomeçar). Dado novo
        de uma atualização vai em State.ext; parâmetro novo entra no estado na altura dela (docs/ATUALIZACOES.md)."""
        self.assertEqual(set(chain.DEFAULT_PARAMS), {
            "epoch_seconds", "window_seconds", "min_share_bits", "max_shares", "supply_cap", "mint_bps",
            "hosting_per_mb_window", "ram_per_mb_window", "demand_max_bps", "proposal_deposit", "quorum_bps"})
        self.assertEqual(State.STATE_KEYS, (
            "balances", "nonces", "machines", "jobs", "sites", "models", "retired_models", "mining", "miners",
            "points", "epochs", "params", "proposals", "upgrade", "ext"))

    def test_schedule_only_marks_known_versions(self):
        for versions in chain.SCHEDULE.values():
            self.assertTrue(all(2 <= v <= chain.RULES for v in versions), "SCHEDULE marca uma versão sem código")


if __name__ == "__main__":
    unittest.main()
