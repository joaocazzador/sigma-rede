"""Regras v4: modelo de texto em várias partes (GGUF dividido), até 600 GB, para os gigantes que rodam num grupo.

Antes da v4 tudo continua recusado como sempre foi; os testes de compatibilidade da chain inteira são os de
tests/test_upgrades.py e tests/test_chain_v3.py (que agora reexecutam também com RULES = 4)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from sigma import chain  # noqa: E402
from sigma.chain import TxError, sha  # noqa: E402
from test_chain import WALLETS, model  # noqa: E402
from test_chain_v3 import V3Net  # noqa: E402

O = WALLETS[0]


def part(i, size=1_000_000):
    return {"role": "part", "url": f"https://huggingface.co/org/g/resolve/main/g-0000{i}-of-00003.gguf",
            "sha256": sha(f"parte{i}".encode()), "size": size}


def net(rules):
    n = V3Net(rules=rules)
    n.fund(O.address)
    return n


class TestParts(unittest.TestCase):
    def test_text_in_parts_on_v4(self):
        n = net(4)
        n.send(O, "register_model", model("gigante", files=[part(2), part(3)]))
        s = n.block()
        m = s.models["gigante"]
        self.assertEqual((m["size"], m["main_size"], len(m["files"])), (3_000_000, 1_000_000, 2))
        self.assertNotIn("task", m)                       # texto, como os modelos de antes
        self.assertEqual(m["ram_mb"], 4 + 512)            # a memória conta o modelo inteiro

    def test_before_v4_text_is_one_file(self):
        for rules in (2, 3):
            with self.subTest(rules=rules):
                n = net(rules)
                with self.assertRaisesRegex(TxError, "modelo de texto é um arquivo só"):
                    n.send(O, "register_model", model("gigante", files=[part(2)]))
                with self.assertRaisesRegex(TxError, "60 GB"):
                    n.send(O, "register_model", model("gigante", size=70_000_000_000))

    def test_limits_on_v4(self):
        n = net(4)
        with self.assertRaisesRegex(TxError, "role 'part'"):
            n.send(O, "register_model", model("gg1", files=[dict(part(2), role="vae")]))
        with self.assertRaisesRegex(TxError, "até 15 arquivos"):
            n.send(O, "register_model", model("gg2", files=[part(2)] * 16))
        with self.assertRaisesRegex(TxError, "100 GB"):
            n.send(O, "register_model", model("gg3", size=101_000_000_000))
        with self.assertRaisesRegex(TxError, "600 GB"):
            n.send(O, "register_model", model("gg4", size=90_000_000_000,
                                              files=[part(2, 90_000_000_000)] * 6))
        # um arquivo de 70 GB (o gpt-oss 120B tem 63 GB) já passa do teto de 60: o que barra é o orçamento
        with self.assertRaisesRegex(TxError, "orçamento precisa cobrir um dia"):
            n.send(O, "register_model", model("gg5", size=70_000_000_000))

    def test_media_rules_did_not_change(self):
        n = net(4)
        with self.assertRaisesRegex(TxError, "role deve ser um de"):
            n.send(O, "register_model", model("img", task="image", files=[part(2)]))

    def test_mainnet_schedule(self):
        ups = chain.SCHEDULE[chain.MAINNET_GENESIS]
        self.assertEqual(sorted(ups), [2, 3, 4])
        self.assertLess(ups[3], ups[4])


if __name__ == "__main__":
    unittest.main()
