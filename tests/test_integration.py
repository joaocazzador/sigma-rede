"""Testes de integração com processos de verdade: só rodam com SIGMA_INTEGRATION=1.

    python -m unittest discover -s tests -v                          só os testes rápidos (sem esses scripts)
    SIGMA_INTEGRATION=1 python -m unittest tests.test_integration -v + minerador, motores, máquina, ponta a ponta e demo

Cada script roda num processo à parte (eles sobem nós, apps e servidores locais) e o teste falha se o script
terminar com código diferente de zero. Rodam um de cada vez, então as portas não se cruzam: 8730-8740 e 8830
(minerador, motores, máquina), 8832 (máquina contra o nó falso), 8746-8747 (ponta a ponta) e as da demo (8700,
8701, 47911-47913, 47919). O test_engines.py só usa o Ollama de verdade (127.0.0.1:11434) com
SIGMA_TESTE_OLLAMA=1; a demo daqui usa o motor de teste. Nada é baixado da internet.
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ON = os.environ.get("SIGMA_INTEGRATION") == "1"


@unittest.skipUnless(ON, "integração desligada: rode com SIGMA_INTEGRATION=1 (sobe nós e processos de verdade)")
class Scripts(unittest.TestCase):
    def run_script(self, path, timeout, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        try:
            r = subprocess.run([sys.executable, path, *args], cwd=ROOT, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            self.fail(f"{os.path.basename(path)} passou de {timeout}s:\n{out[-3000:]}")
        # o fim da saída mostra o que falhou (os scripts imprimem "FALHOU ..." e saem com 1)
        self.assertEqual(r.returncode, 0, f"{os.path.basename(path)} falhou (código {r.returncode}):\n"
                                          f"{r.stdout[-4000:]}\n{r.stderr[-3000:]}")
        return r.stdout

    def test_miner(self):
        self.run_script(os.path.join(HERE, "test_miner.py"), 300)

    def test_engines(self):
        self.run_script(os.path.join(HERE, "test_engines.py"), 900)

    def test_host(self):
        self.run_script(os.path.join(HERE, "test_host.py"), 900)

    def test_e2e_maquina(self):
        out = self.run_script(os.path.join(HERE, "test_e2e_maquina.py"), 900)
        self.assertIn("TUDO OK", out)

    def test_demo(self):
        out = self.run_script(os.path.join(ROOT, "demo.py"), 1800)
        for want in ("todo SIG que existe veio da mineração", "têm o mesmo bloco", "DIVERGIU da maioria",
                     "apagou o modelo", "sozinha",
                     # v2.1: pedido e resposta cifrados no nó, cancelamento e falha "context" sem custo
                     "a pergunta não aparece neles", "abre com a chave da carla e não abre com a de mais ninguém",
                     "pedido cancelado antes de qualquer máquina pegar", "respondeu falha \"context\"",
                     # v2.3: emissão contínua em todo bloco e votações
                     "por hora", "sem 'próxima distribuição'", "mineração do trapaceiro:",
                     "1 SIG = 1 voto", "travados nas votações",
                     "identidade de mineração (carteira.computador)"):
            self.assertIn(want, out)
        for bad in ("NÃO BATE", "? NÃO", "ATENÇÃO"):
            self.assertNotIn(bad, out)


if __name__ == "__main__":
    unittest.main()
