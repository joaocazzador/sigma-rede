"""Regras v3: a caução dos coordenadores, o núcleo econômico da divisão de modelos gigantes.

    python -m unittest tests.test_chain_v3 -v

A v3 ainda não tem altura marcada (chain.SCHEDULE): aqui as redes já nascem nela (genesis "rules": 3), menos
TestBeforeV3 (redes v1 e v2: tudo recusado como sempre foi), TestV2ToV3 (uma rede v2 que vira v3 num SCHEDULE de
teste) e TestOldChainReplays (uma chain v1 -> v2 gravada com o código de antes da v3 reexecuta com os mesmos state
roots). As redes daqui conferem, em todo bloco, que saldos + escrows + orçamentos + votos e depósitos + cauções ==
minted, inclusive nos blocos com corte (que devolve parte do SIG para o que falta minerar).
Porta usada: 8823 (só 127.0.0.1), no teste do nó, do cliente e da CLI por HTTP.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain, cli, evm  # noqa: E402
from sigma import node as nodemod  # noqa: E402
from sigma.chain import UNIT, State, TxError, sha  # noqa: E402
from sigma.client import Client, SigmaError  # noqa: E402
from sigma.crypto import Wallet, to_eth  # noqa: E402
import test_node as tn  # noqa: E402
from test_chain import H, WALLETS, Net, apply_block, evm_tx, machine, model, supply  # noqa: E402

O, B, C = WALLETS[0], WALLETS[1], WALLETS[2]     # O: dona da caução (o payout da máquina)
R, V = WALLETS[5].address, WALLETS[6].address    # quem prova a fraude e quem foi lesado
M, M2 = WALLETS[10], WALLETS[11]                 # máquinas
MIN_BOND = chain.UPGRADE_PARAMS[3]["min_bond"]
PORT = 8823


class V3Net(Net):
    """Rede de teste que já nasce na versão `rules` (3, se nada for dito) e confere a oferta em todo bloco."""

    def __init__(self, rules=3, **params):
        super().__init__(**params)
        self.genesis["rules"] = rules
        self.state = State(self.genesis)
        self.pending = self.state.clone()
        self.prev = chain.genesis_block(self.genesis)["hash"]

    def block(self, dt=0.25, at=None):
        s = super().block(dt, at)
        if supply(s) != s.mining["minted"]:
            raise AssertionError(f"oferta divergiu no bloco {self.height}: {supply(s)} != {s.mining['minted']}")
        return s

    def slash(self, *args, **kw):
        """Um corte no fim de um bloco, como uma prova de fraude fará (nada chama _slash nas regras ainda):
        (estado, cortado, minted logo antes do corte, minted logo depois)."""
        out, real = {}, State.end_block

        def end_block(st):
            real(st)
            out["before"] = st.mining["minted"]
            out["cut"] = st._slash(*args, **kw)
            out["after"] = st.mining["minted"]
        with mock.patch.object(State, "end_block", end_block):
            s = self.block()
        return s, out["cut"], out["before"], out["after"]

    def bonds(self):
        return self.state.ext.get("bonds", {})


def bonded(amount=1500 * UNIT, **params):
    """Rede v3 com a carteira O minerada (ninguém mais recebe emissão depois do fund) e a máquina M registrada com
    o payout de O e caucionada com `amount`."""
    net = V3Net(**params)
    net.fund(O.address)
    net.send(M, "register_machine", machine(O.address, pub=M.pub))
    net.block()
    if amount:
        net.send(O, "bond", {"machine": M.address, "amount": amount})
        net.block()
    return net


def system_tx(wallet, net, call):
    """Operação do Sigma assinada pela MetaMask (transação para o SYSTEM_ADDRESS)."""
    return evm_tx(wallet.priv, net.pending.nonces.get(wallet.address, 0), evm.SYSTEM_ADDRESS,
                  data=json.dumps(call).encode())


# ------------------------------------------------------------------ antes da v3
class TestBeforeV3(unittest.TestCase):
    """Nas redes v1 e v2 (a principal hoje), a caução não existe: tipos, campos e o parâmetro votável novos são
    recusados como sempre foram, e a leitura do nó não quebra."""

    def nets(self):
        for rules in (1, 2):
            net = V3Net(rules=rules)
            net.fund(O.address)
            net.send(M, "register_machine", machine(O.address, pub=M.pub))
            net.block()
            yield rules, net

    def test_new_types_are_unknown(self):
        for rules, net in self.nets():
            with self.subTest(rules=rules):
                nonce = net.pending.nonces.get(O.address, 0)
                for type_ in ("bond", "unbond"):
                    for data in ({"machine": M.address, "amount": UNIT}, {"machine": M.address, "amount": UNIT,
                                                                           "lixo": 1}):
                        with self.assertRaisesRegex(TxError, f"^tipo de transação desconhecido: {type_}$"):
                            net.send(O, type_, data)
                    with self.assertRaisesRegex(TxError, f"^operação desconhecida: {type_}$"):   # pela MetaMask
                        net.submit(system_tx(O, net, {"type": type_, "data": {"machine": M.address, "amount": 1}}))
                self.assertEqual(net.pending.nonces.get(O.address, 0), nonce)
                s = net.block()
                self.assertEqual((s.ext.get("bonds"), s.bonded(), s.is_coordinator(M.address)), (None, 0, False))

    def test_min_bond_is_not_votable_yet(self):
        for rules, net in self.nets():
            with self.subTest(rules=rules):
                self.assertNotIn("min_bond", net.state.params)
                old = ", ".join(sorted(set(chain.GOVERNABLE) - {"min_bond"}))   # a mensagem de antes da v3
                with self.assertRaises(TxError) as cm:
                    net.send(O, "propose", {"kind": "param", "title": "Caução maior", "text": "",
                                            "param": "min_bond", "value": 2_000 * UNIT})
                self.assertEqual(str(cm.exception), f"parâmetro não pode ser votado (os votáveis: {old})")

    def test_node_views_do_not_break(self):
        for rules, net in self.nets():
            with self.subTest(rules=rules):
                st = net.state
                g = nodemod.governance_info(st)
                self.assertEqual(set(g["governable"]), set(chain.GOVERNABLE) - {"min_bond"})
                self.assertNotIn("min_bond", g["params"])
                m = nodemod.mining_info(st)
                self.assertEqual({k: m[k] for k in ("bonding", "bonded", "coordinators", "min_bond")},
                                 {"bonding": False, "bonded": 0, "coordinators": 0, "min_bond": None})
                self.assertEqual(nodemod.bonds_info(st)["bonds"], [])
                self.assertIsNone(nodemod.bond_info(st, M.address))

    def test_genesis_params_stay_frozen(self):
        """O min_bond não entra em DEFAULT_PARAMS nem no genesis: nasce com a atualização (ou com a rede, se ela já
        nasce na v3), e os outros parâmetros são os mesmos."""
        self.assertNotIn("min_bond", chain.DEFAULT_PARAMS)
        with self.assertRaises(ValueError):
            Net(min_bond=MIN_BOND)
        v2, v3 = State(V3Net(rules=2).genesis).params, State(V3Net().genesis).params
        self.assertEqual(v3, dict(v2, min_bond=MIN_BOND))


# ------------------------------------------------------------------ v2 -> v3 por altura marcada
class TestV2ToV3(unittest.TestCase):
    def test_v3_enters_at_the_height(self):
        net = V3Net(rules=2)
        at = 8
        with mock.patch.object(chain, "SCHEDULE", {net.state.genesis_hash: {3: at}}):
            net.fund(O.address)
            net.send(M, "register_machine", machine(O.address, pub=M.pub))
            net.block()
            while net.height < at - 1:
                with self.assertRaisesRegex(TxError, "tipo de transação desconhecido: bond"):
                    net.send(O, "bond", {"machine": M.address, "amount": MIN_BOND})
                with self.assertRaisesRegex(TxError, "não pode ser votado"):
                    net.send(O, "propose", {"kind": "param", "title": "Caução maior", "text": "",
                                            "param": "min_bond", "value": 2_000 * UNIT})
                s = net.block()
            self.assertEqual((s.rules, s.params.get("min_bond"), nodemod.mining_info(s)["bonding"]), (2, None, False))
            ext = dict(s.ext)
            s = net.block()                                       # a altura da v3
            self.assertEqual((s.rules, s.params["min_bond"]), (3, MIN_BOND))
            self.assertEqual(s.upgrade["history"][-1], {"rules": 3, "height": at, "time": s.now})
            self.assertEqual(s.ext, ext)                         # a caução só entra no estado com a primeira
            self.assertIn("min_bond", nodemod.governance_info(s)["governable"])
            net.send(O, "bond", {"machine": M.address, "amount": MIN_BOND})
            s = net.block()
            self.assertTrue(s.is_coordinator(M.address))
            self.assertEqual(nodemod.mining_info(s)["coordinators"], 1)
            net.send(O, "unbond", {"machine": M.address, "amount": UNIT})
            s = net.block()
            self.assertFalse(s.is_coordinator(M.address))       # abaixo do mínimo
            replay = State(net.genesis)                           # outro nó, só com os blocos: as mesmas raízes
            for h, t, prev, txs, root in net.blocks:
                apply_block(replay, h, t, prev, txs)
                self.assertEqual(replay.root(), root, f"reexecução divergiu no bloco {h}")


# ------------------------------------------------------------------ caução
class TestBond(unittest.TestCase):
    def test_bond_locks_sig_and_counts_in_supply(self):
        net = bonded(amount=0)
        bal = net.state.bal(O.address)
        net.send(O, "bond", {"machine": M.address, "amount": 1500 * UNIT})
        s = net.block()
        self.assertEqual(s.ext["bonds"], {M.address: {"owner": O.address, "amount": 1500 * UNIT, "unbonding": [],
                                                      "slashed": 0, "since": s.now}})
        self.assertEqual((s.bal(O.address), s.bonded()), (bal - 1500 * UNIT, 1500 * UNIT))
        self.assertTrue(s.is_coordinator(M.address))
        since = s.now
        net.send(O, "bond", {"machine": M.address, "amount": 1})      # 1 µSIG a mais, pela mesma dona
        s = net.block()
        b = s.ext["bonds"][M.address]
        self.assertEqual((b["amount"], b["since"], s.bal(O.address)), (1500 * UNIT + 1, since, bal - 1500 * UNIT - 1))
        self.assertEqual(supply(s), s.mining["minted"])

    def test_bond_by_metamask(self):
        net = bonded(amount=0)
        net.submit(system_tx(O, net, {"type": "bond", "data": {"machine": to_eth(M.address), "amount": MIN_BOND}}))
        s = net.block()
        self.assertEqual(s.ext["bonds"][M.address]["amount"], MIN_BOND)
        net.submit(system_tx(O, net, {"type": "unbond", "data": {"machine": M.address, "amount": UNIT}}))
        s = net.block()
        self.assertEqual(s.ext["bonds"][M.address]["unbonding"][0]["amount"], UNIT)

    def test_bond_refusals(self):
        net = bonded(amount=0)
        net.fund(B.address)
        net.send(M2, "register_machine", machine(B.address, pub=M2.pub))
        net.block()
        cap = net.state.params["supply_cap"]
        for wallet, data, msg in (
                (O, {"machine": WALLETS[12].address, "amount": UNIT}, "máquina não registrada"),
                (O, {"machine": "sig123", "amount": UNIT}, "machine inválido"),
                (B, {"machine": M.address, "amount": UNIT}, "só a carteira que recebe os ganhos da máquina"),
                (M, {"machine": M.address, "amount": UNIT}, "só a carteira que recebe os ganhos da máquina"),
                (O, {"machine": M.address, "amount": 0}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address, "amount": -1}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address, "amount": True}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address, "amount": "10"}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address, "amount": 1.5}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address, "amount": cap + 1}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address, "amount": net.state.bal(O.address) + 1}, "saldo insuficiente"),
                (O, {"machine": M.address, "amount": 1, "lixo": 1}, "campos desconhecidos em bond: lixo")):
            with self.subTest(msg=msg, data=data), self.assertRaisesRegex(TxError, msg):
                net.send(wallet, "bond", data)
        self.assertEqual(net.block().ext.get("bonds"), None)

    def test_bond_of_another_owner_is_refused(self):
        """A máquina mudou o payout: a caução continua da carteira antiga (só ela retira), e a nova não pode
        caucionar por cima até a antiga retirar tudo (inclusive o que está em espera)."""
        net = bonded()
        net.fund(B.address)
        net.send(M, "register_machine", machine(B.address, pub=M.pub))
        net.block()
        with self.assertRaisesRegex(TxError, "a caução desta máquina é de outra carteira"):
            net.send(B, "bond", {"machine": M.address, "amount": MIN_BOND})
        with self.assertRaisesRegex(TxError, "só a carteira que recebe os ganhos"):
            net.send(O, "bond", {"machine": M.address, "amount": 1})   # a antiga não aumenta (não é mais o payout)
        with self.assertRaisesRegex(TxError, "só a carteira dona da caução pode retirá-la"):
            net.send(B, "unbond", {"machine": M.address, "amount": 1})
        net.send(O, "unbond", {"machine": M.address, "amount": 1500 * UNIT})
        s = net.block()
        with self.assertRaisesRegex(TxError, "a caução desta máquina é de outra carteira"):
            net.send(B, "bond", {"machine": M.address, "amount": MIN_BOND})   # ainda há retirada em espera
        s = net.block(at=s.ext["bonds"][M.address]["unbonding"][0]["release"])
        self.assertNotIn(M.address, s.ext["bonds"])
        net.send(M, "register_machine", machine(B.address, pub=M.pub))      # a máquina volta a sinalizar
        net.send(B, "bond", {"machine": M.address, "amount": MIN_BOND})
        s = net.block()
        self.assertEqual((s.ext["bonds"][M.address]["owner"], s.is_coordinator(M.address)), (B.address, True))


# ------------------------------------------------------------------ retirada
class TestUnbond(unittest.TestCase):
    def test_withdrawal_waits_14_days_then_goes_back_to_the_owner(self):
        net = bonded()
        bal = net.state.bal(O.address)
        net.send(O, "unbond", {"machine": M.address, "amount": 500 * UNIT})
        s = net.block()
        release = s.now + chain.UNBOND_SECONDS
        self.assertEqual(s.ext["bonds"][M.address]["unbonding"], [{"amount": 500 * UNIT, "release": release}])
        self.assertEqual((s.ext["bonds"][M.address]["amount"], s.bonded(), s.bal(O.address)),
                         (1000 * UNIT, 1500 * UNIT, bal))         # saiu do ativo, mas continua travado
        self.assertTrue(s.is_coordinator(M.address))             # 1.000 SIG: ainda no mínimo
        s = net.block(at=release - 0.001)                        # um instante antes: nada
        self.assertEqual((s.bal(O.address), len(s.ext["bonds"][M.address]["unbonding"])), (bal, 1))
        s = net.block(at=release)                                # release <= agora: volta sozinha
        self.assertEqual((s.bal(O.address), s.ext["bonds"][M.address]["unbonding"]), (bal + 500 * UNIT, []))
        self.assertIn([O.address, "bond", 500 * UNIT, M.address], s.credits)   # no histórico da carteira
        self.assertEqual(s.bonded(), 1000 * UNIT)
        # 14 dias sem sinal: a máquina sumiu do estado (limpeza), mas a caução continua da dona e volta inteira
        self.assertNotIn(M.address, s.machines)
        net.send(O, "unbond", {"machine": M.address, "amount": 1000 * UNIT})
        s = net.block()
        self.assertEqual(s.ext["bonds"][M.address]["amount"], 0)
        s = net.block(at=s.now + chain.UNBOND_SECONDS)
        self.assertEqual((s.ext["bonds"], s.bonded(), s.bal(O.address)), ({}, 0, bal + 1500 * UNIT))

    def test_two_withdrawals_come_back_each_at_its_time(self):
        net = bonded()
        bal = net.state.bal(O.address)
        net.send(O, "unbond", {"machine": M.address, "amount": 100 * UNIT})
        s = net.block()
        first = s.now + chain.UNBOND_SECONDS
        net.send(O, "unbond", {"machine": M.address, "amount": 200 * UNIT})
        s = net.block(dt=3600)
        second = s.now + chain.UNBOND_SECONDS
        self.assertEqual([u["release"] for u in s.ext["bonds"][M.address]["unbonding"]], [first, second])
        s = net.block(at=first + 1)
        self.assertEqual((s.bal(O.address), s.ext["bonds"][M.address]["unbonding"]),
                         (bal + 100 * UNIT, [{"amount": 200 * UNIT, "release": second}]))
        s = net.block(at=second)
        self.assertEqual((s.bal(O.address), s.bonded()), (bal + 300 * UNIT, 1200 * UNIT))

    def test_unbond_refusals(self):
        net = bonded()
        net.fund(B.address)
        cap = net.state.params["supply_cap"]
        for wallet, data, msg in (
                (O, {"machine": M2.address, "amount": UNIT}, "esta máquina não tem caução"),
                (B, {"machine": M.address, "amount": UNIT}, "só a carteira dona da caução pode retirá-la"),
                (M, {"machine": M.address, "amount": UNIT}, "só a carteira dona da caução pode retirá-la"),
                (O, {"machine": M.address, "amount": 1500 * UNIT + 1}, "não dá para retirar mais"),
                (O, {"machine": M.address, "amount": 0}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address, "amount": -5}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address, "amount": cap + 1}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": M.address, "amount": False}, "amount deve ser inteiro entre 1 e"),
                (O, {"machine": [M.address], "amount": 1}, "machine inválido"),
                (O, {"machine": M.address, "amount": 1, "to": B.address}, "campos desconhecidos em unbond: to")):
            with self.subTest(msg=msg, data=data), self.assertRaisesRegex(TxError, msg):
                net.send(wallet, "unbond", data)
        net.send(O, "unbond", {"machine": M.address, "amount": 1000 * UNIT})
        with self.assertRaisesRegex(TxError, r"a caução ativa desta máquina é de 500\.000000 SIG"):
            net.send(O, "unbond", {"machine": M.address, "amount": 500 * UNIT + 1})   # só do que está ativo
        s = net.block()
        self.assertEqual(s.ext["bonds"][M.address]["amount"], 500 * UNIT)

    def test_pending_withdrawals_are_limited(self):
        net = bonded()
        for _ in range(chain.MAX_UNBONDING - 1):
            net.send(O, "unbond", {"machine": M.address, "amount": 1})
        s = net.block()
        net.send(O, "unbond", {"machine": M.address, "amount": 1})
        with self.assertRaisesRegex(TxError, f"já há {chain.MAX_UNBONDING} retiradas em espera"):
            net.send(O, "unbond", {"machine": M.address, "amount": 1})
        s = net.block(at=s.now + chain.UNBOND_SECONDS)           # as 19 primeiras voltaram
        self.assertEqual(len(s.ext["bonds"][M.address]["unbonding"]), 1)
        net.send(O, "unbond", {"machine": M.address, "amount": 1})
        self.assertEqual(len(net.block().ext["bonds"][M.address]["unbonding"]), 2)


# ------------------------------------------------------------------ corte
class TestSlash(unittest.TestCase):
    def test_cuts_active_first_then_the_newest_withdrawals(self):
        net = bonded()
        net.send(O, "unbond", {"machine": M.address, "amount": 300 * UNIT})
        s = net.block()
        r1 = s.now + chain.UNBOND_SECONDS
        net.send(O, "unbond", {"machine": M.address, "amount": 200 * UNIT})
        s = net.block()
        r2 = s.now + chain.UNBOND_SECONDS
        s, cut, before, after = net.slash(M.address, 1100 * UNIT, "teste")
        b = s.ext["bonds"][M.address]
        self.assertEqual(cut, 1100 * UNIT)
        self.assertEqual((b["amount"], b["unbonding"], b["slashed"]),
                         (0, [{"amount": 300 * UNIT, "release": r1}, {"amount": 100 * UNIT, "release": r2}],
                          1100 * UNIT))
        self.assertEqual(before - after, 1100 * UNIT)            # sem quem provou nem vítima: tudo volta à emissão
        s, cut, before, after = net.slash(M.address, 1000 * UNIT, "de novo")
        self.assertEqual((cut, before - after), (400 * UNIT, 400 * UNIT))   # só havia 400 SIG
        self.assertNotIn(M.address, s.ext["bonds"])               # sem nada ativo nem em espera: saiu
        self.assertEqual([(x["amount"], x["reason"]) for x in s.ext["slashes"]],
                         [(1100 * UNIT, "teste"), (400 * UNIT, "de novo")])
        s = net.block(at=r2 + 1)
        self.assertNotIn([O.address, "bond"], [c[:2] for c in s.credits])   # nada para devolver

    def test_split_between_reporter_victim_and_the_unminted(self):
        net = bonded()
        rep, next_mint = net.state.machines[M.address]["reputation"], net.state.next_mint()
        s, cut, before, after = net.slash(M.address, 1000 * UNIT, "resultado falso no pedido x", reporter=R,
                                          victim=V, victim_cap=100 * UNIT)
        self.assertEqual(cut, 1000 * UNIT)
        self.assertEqual((s.bal(R), s.bal(V)), (200 * UNIT, 100 * UNIT))   # 20% e o prejuízo (abaixo dos 30%)
        self.assertEqual(before - after, 700 * UNIT)              # o resto sai de minted: a emissão o paga de novo
        self.assertIn([R, "slash_reporter", 200 * UNIT, M.address], s.credits)
        self.assertIn([V, "slash_victim", 100 * UNIT, M.address], s.credits)
        self.assertEqual(s.ext["slashes"], [{"machine": M.address, "amount": 1000 * UNIT,
                                             "reason": "resultado falso no pedido x", "height": net.height,
                                             "reporter": R, "to_reporter": 200 * UNIT, "victim": V,
                                             "to_victim": 100 * UNIT, "to_mining": 700 * UNIT}])
        self.assertEqual(s.machines[M.address]["reputation"], rep - chain.SLASH_REPUTATION)
        b = s.ext["bonds"][M.address]
        self.assertEqual((b["amount"], b["slashed"]), (500 * UNIT, 1000 * UNIT))
        self.assertFalse(s.is_coordinator(M.address))            # 500 SIG: abaixo do mínimo
        self.assertGreater(s.next_mint(), next_mint)              # falta mais para minerar: a emissão sobe

    def test_victim_gets_at_most_30_percent_and_at_most_the_loss(self):
        for cap, want in ((10_000 * UNIT, 300 * UNIT), (300 * UNIT, 300 * UNIT), (1, 1), (0, 0), (-5, 0),
                          (2.5, 0), (None, 0)):
            with self.subTest(victim_cap=cap):
                net = bonded()
                s, cut, before, after = net.slash(M.address, 1000 * UNIT, "x", victim=V, victim_cap=cap)
                self.assertEqual((s.bal(V), before - after), (want, 1000 * UNIT - want))

    def test_who_answers_for_the_machine_gets_no_share(self):
        """Quem responde pela máquina (ela mesma, a dona da caução, o payout de agora) não recebe parte do próprio
        corte: denunciar a si mesmo não devolve 20%, nem 'ser a vítima' de si mesmo devolve 30%."""
        net = bonded()
        net.send(M, "register_machine", machine(B.address, pub=M.pub))   # o payout mudou para B
        net.block()
        for reporter, victim in ((O.address, M.address), (M.address, O.address), (B.address, B.address)):
            with self.subTest(reporter=reporter, victim=victim):
                bal = {a: net.state.bal(a) for a in (O.address, M.address, B.address)}
                s, cut, before, after = net.slash(M.address, 100 * UNIT, "x", reporter=reporter, victim=victim,
                                                  victim_cap=100 * UNIT)
                self.assertEqual((cut, before - after), (100 * UNIT, 100 * UNIT))
                self.assertEqual({a: s.bal(a) for a in bal}, bal)
                self.assertEqual((s.ext["slashes"][-1]["to_reporter"], s.ext["slashes"][-1]["to_victim"]), (0, 0))

    def test_withdrawing_does_not_escape_the_cut(self):
        """A dona tira tudo da caução logo antes da fraude ser provada: a espera de 14 dias ainda é cortada."""
        net = bonded()
        bal = net.state.bal(O.address)
        net.send(O, "unbond", {"machine": M.address, "amount": 1500 * UNIT})
        s = net.block()
        release = s.now + chain.UNBOND_SECONDS
        self.assertFalse(s.is_coordinator(M.address))
        s = net.block(at=s.now + 7 * 86400)                       # uma semana depois, a prova chega
        s, cut, before, after = net.slash(M.address, 1000 * UNIT, "x", reporter=R)
        self.assertEqual((cut, s.ext["bonds"][M.address]["unbonding"]), (1000 * UNIT, [{"amount": 500 * UNIT,
                                                                                       "release": release}]))
        s = net.block(at=release)
        self.assertEqual((s.bal(O.address), s.bal(R), s.bonded()), (bal + 500 * UNIT, 200 * UNIT, 0))

    def test_without_bond_or_with_an_invalid_amount(self):
        net = bonded()
        net.send(M2, "register_machine", machine(B.address, pub=M2.pub))
        net.block()
        rep = net.state.machines[M2.address]["reputation"]
        s, cut, before, after = net.slash(M2.address, 100 * UNIT, "fraude de quem não tem caução", reporter=R)
        self.assertEqual((cut, before, s.bal(R)), (0, after, 0))  # nada para cortar: a fraude fica registrada
        self.assertEqual(s.machines[M2.address]["reputation"], rep - chain.SLASH_REPUTATION)
        self.assertEqual(s.ext["slashes"][-1]["amount"], 0)
        st = net.state.clone()
        root = st.root()
        for amount in (0, -1, 1.5, True, "10", None):            # pedido de corte inválido: nada muda
            self.assertEqual(st._slash(M.address, amount, "x"), 0)
        self.assertEqual(st.root(), root)

    def test_public_record_keeps_the_last_200(self):
        st = bonded().state.clone()
        for i in range(chain.SLASHES_KEEP + 5):
            self.assertEqual(st._slash(M.address, 1, f"corte {i}"), 1)
        self.assertEqual(len(st.ext["slashes"]), chain.SLASHES_KEEP)
        self.assertEqual(st.ext["slashes"][0]["reason"], "corte 5")
        self.assertEqual(supply(st), st.mining["minted"])

    def test_supply_matches_minted_in_every_block(self):
        """Um pouco de tudo, com duas cauções, cortes, retiradas que voltam e uma votação: V3Net confere saldos +
        escrows + orçamentos + votos/depósitos + cauções == minted em cada bloco."""
        net = bonded()                                            # M: 1.500 SIG de O
        net.fund(B.address, C.address)
        net.send(M2, "register_machine", machine(B.address, pub=M2.pub))
        net.send(C, "register_model", model("modelo-c", budget=2 * UNIT))
        net.block()
        net.send(B, "bond", {"machine": M2.address, "amount": 1200 * UNIT})
        pid = net.send(C, "propose", {"kind": "text", "title": "Uma decisão", "text": ""})["id"]
        net.block()
        net.send(C, "vote", {"proposal_id": pid, "choice": "sim", "amount": 200 * UNIT})
        net.send(O, "unbond", {"machine": M.address, "amount": 700 * UNIT})   # M: 800 ativos + 700 em espera
        net.mine(O.address, B.address)
        net.block()
        net.slash(M2.address, 900 * UNIT, "x", reporter=C.address, victim=O.address, victim_cap=50 * UNIT)
        net.slash(M.address, 1000 * UNIT, "y", reporter=B.address)            # M: 0 ativo + 500 em espera
        net.step(M, M2)
        net.next_window()                                         # a emissão volta (O e B mineraram)
        net.block(dt=3 * 86400)                                   # 3 dias: as máquinas saíram do estado
        net.send(B, "unbond", {"machine": M2.address, "amount": 300 * UNIT})   # M2: 0 ativo + 300 em espera
        net.send(M, "register_machine", machine(O.address, pub=M.pub))
        net.send(O, "bond", {"machine": M.address, "amount": 100 * UNIT})
        net.block()
        bal = net.state.bal(O.address)
        s, cut, before, after = net.slash(M2.address, 2000 * UNIT, "z", reporter=O.address)
        self.assertEqual((cut, s.bal(O.address) - bal), (300 * UNIT, 60 * UNIT))   # só havia a retirada em espera
        self.assertNotIn(M2.address, s.ext["bonds"])
        s = net.block(dt=chain.UNBOND_SECONDS)                    # a espera de M volta e a votação termina
        self.assertEqual((s.bonded(), s.proposals[pid]["status"]), (100 * UNIT, "approved"))
        self.assertEqual(supply(s), s.mining["minted"])
        self.assertGreater(len(net.blocks), 20)


# ------------------------------------------------------------------ coordenador
class TestCoordinator(unittest.TestCase):
    def test_eligibility(self):
        net = bonded(amount=MIN_BOND - 1)
        self.assertFalse(net.state.is_coordinator(M.address))   # abaixo da caução mínima
        net.send(O, "bond", {"machine": M.address, "amount": 1})
        s = net.block()
        self.assertTrue(s.is_coordinator(M.address))             # exatamente o mínimo
        for _ in range(chain.OFFLINE_WINDOWS + 1):                # sem sinal: offline
            s = net.next_window()
        self.assertFalse(s.machines[M.address]["online"])
        self.assertFalse(s.is_coordinator(M.address))
        s = net.step(M)
        self.assertTrue(s.is_coordinator(M.address))
        net.send(M, "register_machine", machine(O.address, pub=M.pub, ai=False))   # IA desligada
        s = net.block()
        self.assertFalse(s.is_coordinator(M.address))
        # ganhos indo para outra carteira: a caução de O não responde mais por quem recebe
        net.send(M, "register_machine", machine(WALLETS[12].address, pub=M.pub))
        s = net.block()
        self.assertFalse(s.is_coordinator(M.address))
        net.send(M, "register_machine", machine(O.address, pub=M.pub))           # o payout volta para O
        s = net.block()
        self.assertTrue(s.is_coordinator(M.address))
        for bad in (None, 5, ["x"], WALLETS[12].address):
            self.assertFalse(s.is_coordinator(bad))

    def test_min_bond_is_votable_on_v3(self):
        net = bonded()                                            # só O minerou: tem quase todo o SIG
        lo, hi = chain.GOVERNABLE["min_bond"]
        for value, msg in ((lo - 1, "value deve ser inteiro entre"), (hi + 1, "value deve ser inteiro entre"),
                           (MIN_BOND, "esse já é o valor de agora")):
            with self.subTest(value=value), self.assertRaisesRegex(TxError, msg):
                net.send(O, "propose", {"kind": "param", "title": "Caução", "text": "", "param": "min_bond",
                                        "value": value})
        pid = net.send(O, "propose", {"kind": "param", "title": "Caução maior para coordenar", "text": "",
                                      "param": "min_bond", "value": 2000 * UNIT})["id"]
        net.block()
        self.assertIn("min_bond", nodemod.governance_info(net.state)["governable"])
        # mais da metade de todo o SIG (o que está na caução conta em minted, mas não vota): decide na hora (v2)
        net.send(O, "vote", {"proposal_id": pid, "choice": "sim", "amount": net.state.mining["minted"] // 2 + 1})
        s = net.block()
        self.assertEqual((s.proposals[pid]["status"], s.params["min_bond"]), ("approved", 2000 * UNIT))
        self.assertFalse(s.is_coordinator(M.address))            # 1.500 SIG não bastam mais
        self.assertEqual(nodemod.governance_info(s)["governable"]["min_bond"],
                         {"min": lo, "max": hi, "now": 2000 * UNIT})


# ------------------------------------------------------------------ nó, cliente e CLI
class TestNodeClientCli(unittest.TestCase):
    """De ponta a ponta numa rede v3 de verdade: a CLI assina pelo cliente, o nó admite, o validador fecha o bloco
    e as rotas /bonds, /mining e /status mostram a caução."""

    def setUp(self):
        self.net = tn.Net(self, rules=3)
        self.owner, self.mw = Wallet.generate(), Wallet.generate()
        self.net.fund(self.owner)
        self.net.send(self.mw, "register_machine", tn.machine_data(self.owner.address, pub=self.mw.pub))
        self.assertTrue(self.net.block())
        tn.serve(self, nodemod.make_handler(self.net.node), PORT)
        self.c = Client(f"http://127.0.0.1:{PORT}", self.owner)

    def get(self, path):
        code, _, body = tn.req(PORT, "GET", path)
        return code, json.loads(body)

    def cli(self, *argv):
        real_wait, net = Client.wait_tx, self.net

        def wait(cl, txid, timeout=30):              # o validador fecha o bloco com a transação
            self.assertTrue(net.block())
            return real_wait(cl, txid, timeout)
        a = cli.build_parser().parse_args(list(argv))
        a.json = False
        out = io.StringIO()
        with mock.patch.object(Client, "wait_tx", wait), redirect_stdout(out):
            cli.run(a, self.c)
        return out.getvalue()

    def test_bond_and_unbond_from_the_cli(self):
        mw, owner = self.mw.address, self.owner.address
        out = self.cli("caucionar", to_eth(mw), "1500")
        self.assertIn(f"caução da máquina {cli.short(mw)}: 1.500,000000 SIG travados", out)
        self.assertIn("a máquina já pode coordenar a divisão de modelos gigantes", out)
        code, b = self.get(f"/bonds/{mw}")
        self.assertEqual((code, b["owner"], b["amount"], b["pending"], b["coordinator"]),
                         (200, owner, 1500 * UNIT, 0, True))
        code, allb = self.get("/bonds")
        self.assertEqual((allb["bonding"], allb["bonded"], allb["coordinators"], allb["min_bond"], allb["slashes"]),
                         (True, 1500 * UNIT, 1, MIN_BOND, []))
        self.assertEqual([x["machine"] for x in allb["bonds"]], [mw])
        m = self.get("/mining")[1]
        self.assertEqual((m["bonding"], m["bonded"], m["coordinators"], m["min_bond"]), (True, 1500 * UNIT, 1, MIN_BOND))
        self.assertEqual(self.get("/status")[1]["supply"]["bonded"], 1500 * UNIT)
        self.assertEqual([e["amount"] for e in self.net.node.history[owner] if e.get("type") == "bond"], [1500 * UNIT])

        out = self.cli("retirar-caucao", mw, "600")
        self.assertIn("retirada de 600 SIG começou: eles voltam sozinhos para a sua carteira em", out)
        self.assertIn("caução ativa agora: 900,000000 SIG", out)
        self.assertIn("para coordenar, a caução ativa precisa ser de pelo menos 1.000,000000 SIG (faltam "
                      "100,000000 SIG)", out)
        b = self.c.bonds(mw)
        self.assertEqual((b["amount"], b["pending"], b["total"], b["coordinator"]),
                         (900 * UNIT, 600 * UNIT, 1500 * UNIT, False))
        self.assertEqual(self.c.bonds()["coordinators"], 0)
        bal = self.c.balance()
        self.assertTrue(self.net.block(chain.UNBOND_SECONDS + 1))   # 14 dias depois: volta sozinha
        self.assertEqual(self.c.balance(), bal + 600 * UNIT)
        credit = [e for e in self.net.node.history[owner] if e.get("kind") == "bond"]
        self.assertEqual([(e["type"], e["amount"]) for e in credit], [("credit", 600 * UNIT)])
        self.assertEqual(self.c.bonds(mw)["unbonding"], [])

    def test_routes_refuse_what_is_not_there(self):
        self.assertEqual(self.get(f"/bonds/{Wallet.generate().address}"),
                         (404, {"error": "esta máquina não tem caução"}))
        self.assertEqual(self.get("/bonds/lixo")[0], 400)
        with self.assertRaises(SigmaError) as cm:
            self.c.bonds("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAee")   # checksum da MetaMask errado
        self.assertIn("caractere errado", str(cm.exception))
        with self.assertRaises(SigmaError) as cm:                  # a chain recusa e a CLI mostra o porquê
            self.cli("bond", self.mw.address, "999999")
        self.assertIn("saldo insuficiente para a caução", str(cm.exception))

    def test_cli_says_when_the_network_has_no_bonds_yet(self):
        class OldNode(Client):
            sent = []

            def get(self, path, timeout=10):
                return {"window": 3, "bonding": False} if path == "/mining" else {}

            def send_tx(self, *a, **kw):
                self.sent.append(a)
                return "x"
        node = OldNode("http://rede-falsa.invalid", self.owner)
        for argv in (("bond", self.mw.address, "1000"), ("retirar-caucao", self.mw.address, "1")):
            a = cli.build_parser().parse_args(list(argv))
            a.json = False
            with self.assertRaises(SigmaError) as cm:
                cli.run(a, node)
            self.assertEqual(str(cm.exception), cli.NO_BONDS_YET)
        self.assertEqual(node.sent, [])
        self.assertTrue({"bond", "caucionar", "unbond", "retirar-caucao"} <= cli.SPEND)


# ------------------------------------------------------------------ compatibilidade com a chain de antes
# State roots de cada bloco de v1_to_v2(), gravados rodando o mesmo cenário no código de antes da v3 (commit 4c8db89,
# "Rede principal: regras v2 marcadas para o bloco 1950", chain.RULES = 2). Se um ajudante do test_chain mudar (Net,
# machine, model), gere-os de novo com aquele código (git show 4c8db89:sigma/chain.py etc.), nunca com o de agora.
OLD_ROOTS = [
    "d75066b46147e9964d544d7e5c5aed38e24f15d5983642fbbc31301c41b7abf0",
    "f7ecdc5d1f0a29fab8b27e930f31d84435e96f71a32baae4663aa51879919fdc",
    "2e2ae1df85df4b6f3a5515489a3b89350166d268bb393f6bdbda7a6be70355f9",
    "f6d3f7b602e5ee16d0eb4f29935b67f18f68b14ffd61f65634a5136293aa5482",
    "03ffdd29e7d700a160c51b3a864946de12ac2ab82ea05d3d15ad1db0ca1540bd",
    "e560efc6707fccad1f62f75ec24892dc0e1a787d276dc82bd28cbb70438c9385",
    "39bc8f6ad25070f589d72ef4a9e5739bf6ac01b5a428bada30c12a7020b360de",
    "9c9de435f4652ace2c374a3b5203a33790a96809bb1d5e03856109a273a76128",
    "d9d3e0856a5d47f999783fc57f57d187f339746e6a78728e833cdd60579a95b6",
    "1bf8d566206e7c6c0e7af0f7196480d278bd49c7bf35ff4907456412139788a5",
    "26b59f9f36f350781c6d096297715cd32c15260dc3a916afd784f3b9d5085964",
    "583bd2139deb1431d0401c9be364ba13712e4b627862c1de5de97a4ae5781fa5",
    "2117f428efe95aebfd55ab90f274d7f8450d593ad7c1050ad92b7b7f597e15e7",
    "ca850bfda8e9e99863067996e4856624b1f91dd9486218775485a3dafec829a5",
    "7a792f6e36d5e1d16a4a60ef3fc666aeeb4203dfb7762748c80b7bb161d6ad0c",
    "738451c96a5b45736c81193bdaf730e66154d88e72b4fb55449017f0af304f4b",
    "bac04235cb633da3cfa9163df1f0d657955f03c4ebe13e769da5c9e6f7177939",
    "3d662fdccf950e915a46957a9c0f48157c8cd4479f40518ed1310d1329d43e81",
    "6d50c436f380d8a46179ed5d96a4ea245e9fd4ab5b9132d6d120d123afc8f742",
    "360d105ff4b5e124693b9a49c66dda5abc8f6001f7995129bc06e4607437ebc9",
    "6cfd52e1058949b70411315e75f8ae51e942eb97552c695253245ca9fa607f2d",
    "0ebf7372f02968dc6706f980c8f63edf760fec57fc566646c632ba319c41e9cb",
    "944a55613fce1e6565bf51f5005056a86fdfba057c45c12ad04a5f21c2db0f30",
    "387639aa79d9bb055ee2b00903defb92f1f2893da5ee99d5611e9cb8752d92a1",
    "efd4478e2034ce835f7a6ea913ded3507e90c766946e9eb16c3e76277dcd8f39",
    "5d805ae26129701e9a886d135f8ff6b77de561516d3006190b0656cae0f9e349",
    "9921b4638ce90532edf0a1b2f9b165ddcb6f79f3b33569feea353f8e53bf3c2a",
    "df67de208c2c7b6152f22354d5225eef80ede73bebcb4f40687394f1e54e9f9d",
    "632c2527607fc7d0f86bc1cea69224d91883e4f915a085e8d15a11515ae9856e",
    "2124dd1e234ea5a5b3a93088844820a70772528ec8d90cf15d1759bd2162904b",
    "3debe25e7e1b9f3f5801e0d5746f3e70700f8985de86c7cc33e8e908679ae495",
]
AT_V2 = 14
VAE = {"role": "vae", "url": "https://huggingface.co/org/img/resolve/main/ae.safetensors",
       "sha256": sha(b"vae"), "size": 3_000_000}


def v1_to_v2():
    """Rede v1 que vira v2 no bloco AT_V2, com um pouco de tudo das duas versões: mineração, máquinas, modelo de
    texto, site, pedido, MetaMask, votação aberta na v1 que fecha antes na v2, reserva dos fundadores, modelo e
    pedido de imagem e o fim de um período de 12 h."""
    net = Net(epoch_seconds=30, window_seconds=5, min_share_bits=2, max_shares=8, ram_per_mb_window=0,
              demand_max_bps=0)                       # os parâmetros de teste de hoje, fixados
    gh = net.state.genesis_hash
    A, PA, PB, F = WALLETS[0], WALLETS[3].address, WALLETS[4].address, WALLETS[7].address
    MA, MB = WALLETS[10], WALLETS[11]
    with mock.patch.object(chain, "SCHEDULE", {gh: {2: AT_V2}}), \
            mock.patch.object(chain, "UPGRADE_GRANTS", {gh: {2: [(F, 100 * UNIT)]}}):
        net.fund(A.address, B.address)
        net.send(MA, "register_machine", machine(PA, pub=MA.pub))
        net.send(MB, "register_machine", machine(PB, pub=MB.pub))
        net.send(A, "register_model", model("texto-a", replicas=2, budget=2000))
        net.send(B, "deploy_site", {"name": "site-b", "manifest": {"i.html": {"hash": H, "size": 5000}},
                                    "budget": chain.MIN_SITE_BUDGET + chain.SITE_BUDGET_PER_FILE})
        net.block()
        net.step(MA, MB)
        for w in (MA, MB):
            net.send(w, "pin_model", {"name": "texto-a"})
            net.send(w, "pin_site", {"name": "site-b", "version": 1})
        net.submit(evm_tx(A.priv, net.pending.nonces.get(A.address, 0), to_eth(C.address),
                          2 * UNIT * evm.WEI_PER_USIG))
        pid = net.send(A, "propose", {"kind": "param", "title": "Aluguel mais barato", "text": "",
                                      "param": "hosting_per_mb_window", "value": 3})["id"]
        net.block()
        job = net.send(B, "create_job", {"kind": "ai", "model": "texto-a", "spec_hash": H, "escrow": 3000,
                                         "max_price": 2000, "max_tokens": 1000})["id"]
        net.send(A, "vote", {"proposal_id": pid, "choice": "sim", "amount": 1000 * UNIT})
        net.block()
        net.send(MA, "claim_job", {"job_id": job})
        net.block()
        net.send(MA, "submit_result", {"job_id": job, "result_hash": H,
                                       "usage": {"tokens_in": 10, "tokens_out": 20}})
        net.send(A, "transfer", {"to": B.address, "amount": 1234})
        net.block()
        while net.height < AT_V2 - 1:
            net.mine(PA)
            net.next_window()
        net.block()                                          # a altura da v2: a reserva dos fundadores
        net.send(MB, "register_machine", machine(PB, pub=MB.pub, tasks=["text", "image"],
                                                 prices={"ai_per_image": 5000}))
        net.send(A, "register_model", model("z-img", size=20_000_000, budget=50 * UNIT, task="image",
                                            files=[VAE]))
        net.send(B, "vote", {"proposal_id": pid, "choice": "sim", "amount": 1000 * UNIT})
        net.block()                                          # mais da metade do SIG: a votação da v1 fecha já
        net.step(MA, MB)
        net.send(MB, "pin_model", {"name": "z-img"})
        net.block()
        img = net.send(C, "create_job", {"kind": "ai", "model": "z-img", "spec_hash": H, "escrow": 10_000,
                                         "max_price": 5000, "units": 2})["id"]
        net.block()
        net.send(MB, "claim_job", {"job_id": img})
        net.block()
        net.send(MB, "submit_result", {"job_id": img, "result_hash": H, "usage": {"units": 1}})
        net.block()
        for _ in range(4):
            net.step(MA, MB)
            net.mine(PA, PB)
        net.block()
        net.settle()
    return net, pid, img, F


class TestOldChainReplays(unittest.TestCase):
    """A rede principal está na v1 e entra na v2 no bloco 1950: com o código da v3 (sem altura marcada), uma chain
    v1 -> v2 gravada pelo código de antes reexecuta com os mesmos state roots em todos os blocos (no espírito do
    tests/test_upgrades.py)."""

    @classmethod
    def setUpClass(cls):
        cls.net, cls.pid, cls.img, cls.founders = v1_to_v2()

    def test_same_roots_as_the_code_before_v3(self):
        roots = [b[4] for b in self.net.blocks]
        self.assertEqual(len(roots), len(OLD_ROOTS))
        for h, (root, old) in enumerate(zip(roots, OLD_ROOTS), 1):
            self.assertEqual(root, old, f"o bloco {h} divergiu da chain gravada pelo código de antes")

    def test_replay_from_the_blocks(self):
        gh = self.net.state.genesis_hash
        s = State(self.net.genesis)
        with mock.patch.object(chain, "SCHEDULE", {gh: {2: AT_V2}}), \
                mock.patch.object(chain, "UPGRADE_GRANTS", {gh: {2: [(self.founders, 100 * UNIT)]}}):
            for (h, t, prev, txs, _), old in zip(self.net.blocks, OLD_ROOTS):
                apply_block(s, h, t, prev, txs)
                self.assertEqual(s.root(), old, f"reexecução divergiu no bloco {h}")

    def test_the_scenario_went_through_v1_and_v2(self):
        s = self.net.state
        self.assertEqual((s.rules, [x["rules"] for x in s.upgrade["history"]]), (2, [1, 2]))
        self.assertEqual(s.upgrade["history"][-1]["height"], AT_V2)
        self.assertEqual((s.proposals[self.pid]["status"], s.proposals[self.pid].get("early")), ("approved", True))
        self.assertEqual((s.jobs[self.img]["status"], s.jobs[self.img]["cost"]), ("done", 5000))
        self.assertEqual(s.bal(self.founders), 100 * UNIT)
        self.assertNotIn("min_bond", s.params)                 # nada da v3 numa rede que não chegou nela
        self.assertEqual(set(s.ext), {"grants"})
        self.assertEqual(supply(s), s.mining["minted"])


if __name__ == "__main__":
    unittest.main()
