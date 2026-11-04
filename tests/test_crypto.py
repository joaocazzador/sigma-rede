"""Testes da cifra para chave pública (seal/unseal), usada nas perguntas e respostas da IA, e da conta da curva
em coordenadas jacobianas (comparada com a versão afim de antes)."""
import random
import unittest
from unittest import mock

from sigma import crypto
from sigma.crypto import Wallet


class SealTest(unittest.TestCase):
    def setUp(self):
        self.alice, self.bob = Wallet.generate(), Wallet.generate()

    def test_round_trip(self):
        for data in (b"", b"x", "olá, máquina".encode(), bytes(range(256)) * 300):
            blob = crypto.seal(self.alice.pub, data)
            self.assertTrue(crypto.is_sealed(blob))
            self.assertNotIn(b"m\xc3\xa1quina", blob)
            self.assertEqual(self.alice.unseal(blob), data)
            self.assertEqual(crypto.unseal(self.alice.priv, blob), data)

    def test_fresh_each_time(self):
        self.assertNotEqual(crypto.seal(self.alice.pub, b"mesma"), crypto.seal(self.alice.pub, b"mesma"))

    def test_wrong_key(self):
        with self.assertRaises(ValueError):
            self.bob.unseal(crypto.seal(self.alice.pub, b"segredo"))

    def test_tampering_is_detected(self):
        blob = crypto.seal(self.alice.pub, b"segredo de verdade")
        for i in (0, len(crypto.SEAL_MAGIC) + 3, len(blob) - 40, len(blob) - 1):
            bad = bytearray(blob)
            bad[i] ^= 1
            with self.assertRaises(ValueError):
                self.alice.unseal(bytes(bad))
        with self.assertRaises(ValueError):
            self.alice.unseal(blob[:-1])

    def test_not_sealed(self):
        self.assertFalse(crypto.is_sealed(b'{"messages": []}'))
        self.assertFalse(crypto.is_sealed("SIGMA-SEAL-1"))
        with self.assertRaises(ValueError):
            self.alice.unseal(b'{"messages": []}')

    def test_invalid_public_key(self):
        for pub in ("", "00" * 64, self.alice.pub[:-2] + "00", "zz" * 64, 123, None):
            with self.assertRaises(ValueError):
                crypto.seal(pub, b"x")

    def test_point_off_the_curve_in_blob(self):
        blob = bytearray(crypto.seal(self.alice.pub, b"x"))
        start = len(crypto.SEAL_MAGIC)
        blob[start:start + 64] = bytes(64)   # (0, 0) não está na secp256k1
        with self.assertRaises(ValueError):
            self.alice.unseal(bytes(blob))


# ------------------------------------------------- referência afim (a conta de antes das coordenadas jacobianas)
def _ref_mul(k, p):
    """k·p em coordenadas afins, dobra-e-soma da direita para a esquerda (como era antes)."""
    r = None
    while k:
        if k & 1:
            r = crypto._add(r, p)
        p = crypto._add(p, p)
        k >>= 1
    return r


def _ref_pub_of(priv):
    x, y = _ref_mul(priv, crypto.G)
    return x.to_bytes(32, "big").hex() + y.to_bytes(32, "big").hex()


def _ref_recover_pub(z, r, s, recid):
    """ecrecover de antes: Q = r⁻¹·(s·R − e·G), com três multiplicações."""
    P, N = crypto.P, crypto.N
    alpha = (r * r * r + 7) % P
    beta = pow(alpha, (P + 1) // 4, P)
    if beta * beta % P != alpha:
        raise ValueError("assinatura inválida")
    y = beta if beta % 2 == recid else P - beta
    e = int.from_bytes(z, "big") % N
    q = _ref_mul(pow(r, -1, N), crypto._add(_ref_mul(s, (r, y)), _ref_mul((-e) % N, crypto.G)))
    if q is None:
        raise ValueError("assinatura inválida")
    return q[0].to_bytes(32, "big").hex() + q[1].to_bytes(32, "big").hex()


class JacobianMatchesAffineTest(unittest.TestCase):
    """A conta jacobiana (mais rápida) dá exatamente o mesmo que a afim: endereços, assinaturas, ecrecover e
    seal não podem mudar."""

    def setUp(self):
        self.rng = random.Random(20261103)

    def keys(self, n):
        edge = [1, 2, 3, 7, crypto.N - 1, crypto.N - 2, 2 ** 255, 2 ** 128 + 1, (crypto.N - 1) // 2]
        return edge + [self.rng.randrange(1, crypto.N) for _ in range(n)]

    def test_scalar_multiplication(self):
        points = [crypto.G] + [_ref_mul(self.rng.randrange(1, crypto.N), crypto.G) for _ in range(4)]
        # N+2 e 3N+2 passam pelo caso raro "somar o mesmo ponto" (vira dobra); N e 2N+1 passam pelo infinito
        scalars = [1, 2, 3, crypto.N - 1, crypto.N, crypto.N + 1, crypto.N + 2, 2 * crypto.N + 1, 3 * crypto.N + 2,
                   2 ** 256 - 1, 2 ** 300 + 5]
        scalars += [self.rng.randrange(1, 2 ** 256) for _ in range(8)]
        for p in points:
            for k in scalars:
                self.assertEqual(crypto._mul(k, p), _ref_mul(k, p), (k, p))
        self.assertIsNone(crypto._mul(0, crypto.G))
        self.assertIsNone(crypto._mul(5, None))

    def test_keys_signatures_and_recovery(self):
        for i, priv in enumerate(self.keys(40)):
            pub = crypto.pub_of(priv)
            self.assertEqual(pub, _ref_pub_of(priv), priv)
            msg = self.rng.randbytes(self.rng.randrange(0, 200)) + str(i).encode()
            sig = crypto.sign(priv, msg)
            with mock.patch.object(crypto, "_mul", _ref_mul):   # a mesma assinatura com a conta afim
                self.assertEqual(crypto.sign(priv, msg), sig)
                self.assertTrue(crypto.verify(pub, msg, sig))
            self.assertTrue(crypto.verify(pub, msg, sig))
            self.assertFalse(crypto.verify(pub, msg + b"!", sig))
            z = self.rng.randbytes(32)
            r, s = self.rng.randrange(1, crypto.N), self.rng.randrange(1, crypto.N)
            for recid in (0, 1):
                try:
                    want = _ref_recover_pub(z, r, s, recid)
                except ValueError:
                    with self.assertRaises(ValueError):
                        crypto.recover_pub(z, r, s, recid)
                    continue
                self.assertEqual(crypto.recover_pub(z, r, s, recid), want)

    def test_recover_matches_a_real_signature(self):
        # assinatura de verdade (estilo MetaMask): recover_pub devolve a chave de quem assinou
        for priv in self.keys(10):
            z = self.rng.randbytes(32)
            e = int.from_bytes(z, "big") % crypto.N
            k = self.rng.randrange(1, crypto.N)
            R = _ref_mul(k, crypto.G)
            r = R[0] % crypto.N
            s = pow(k, -1, crypto.N) * (e + r * priv) % crypto.N
            recid = R[1] % 2
            self.assertEqual(crypto.recover_pub(z, r, s, recid), _ref_pub_of(priv))

    def test_seal_is_byte_for_byte_the_same(self):
        for i, priv in enumerate(self.keys(12)):
            pub = _ref_pub_of(priv)
            data = self.rng.randbytes(self.rng.randrange(0, 600))
            seed = self.rng.randbytes(16)
            blobs = []
            for mul in (crypto._mul, _ref_mul):
                stream = random.Random(seed)       # o mesmo "acaso" (chave efêmera e nonce) nas duas contas
                with mock.patch.object(crypto.os, "urandom", lambda n: stream.randbytes(n)), \
                        mock.patch.object(crypto, "_mul", mul):
                    blobs.append(crypto.seal(pub, data))
            self.assertEqual(blobs[0], blobs[1], i)
            self.assertEqual(crypto.unseal(priv, blobs[0]), data)
            with mock.patch.object(crypto, "_mul", _ref_mul):
                self.assertEqual(crypto.unseal(priv, blobs[0]), data)


if __name__ == "__main__":
    unittest.main()
