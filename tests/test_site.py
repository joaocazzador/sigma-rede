"""Site público (sigma/web/*.html, sigma.css, og.png e README.md): textos, contratos da SPEC e o Edge sem internet.

    python -m unittest tests.test_site -v
    SIGMA_SEM_EDGE=1 python -m unittest tests.test_site -v      # só os textos e o HTML (sem abrir o Edge)

Cada achado da revisão (docs/revisao-v2-achados.md) vira uma conferência: a frase errada não pode voltar e a certa
tem que estar lá. As conferências no Edge abrem as páginas de verdade (perfil temporário, proxy morto e DNS
bloqueado: nada sai de 127.0.0.1), com uma API falsa do nó, em 375 e 1366 px, com a rede no ar e fora do ar, e
fazem o caminho da carteira do navegador (criar, guardar a chave, substituir, importar) com um ethers de mentira.
Portas: 8892-8898 (servidores do teste); 8899 é o proxy morto.
"""
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "sigma", "web")
PAGES = ("index.html", "baixar.html", "tutorial.html", "carteira.html", "forum.html")
OFFICIAL = "https://sigma.195-35-29-232.sslip.io"
SRI = "sha384-6Zl0Pc8zjSz8KvmNeXRvUQgY4ryFb+BwDvKCmLYcBME0joAaru491tQgi9B7zsMM"   # SPEC-v2.1 §4
ETHERS = "https://cdn.jsdelivr.net/npm/ethers@6.13.4/dist/ethers.umd.min.js"
SLOGAN = "Queremos que os donos dos robôs sejam"
ACCENTED = re.compile(r"[ÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]")


def read(name):
    with open(os.path.join(ROOT, name) if name == "README.md" else os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def flat(text):
    return re.sub(r"\s+", " ", text)


def visible(page):
    """Texto que a pessoa lê (sem <script>, <style> e atributos), com os espaços normalizados."""
    page = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", page, flags=re.S)
    return flat(html.unescape(re.sub(r"<[^>]+>", " ", page)))


class Tags(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.tags = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def find(self, tag, **want):
        return [a for t, a in self.tags if t == tag and all(a.get(k) == v for k, v in want.items())]


def css_of(name):
    text = read(name)
    if not name.endswith(".css"):
        text = "\n".join(re.findall(r"<style>(.*?)</style>", text, re.S))
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def display_rules(name):
    """[(seletor, line-height)] das regras com a fonte de títulos (Anton): var(--display) ou h1/h2/h3."""
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css_of(name)):
        sel = " ".join(sel.split())
        if "var(--display)" not in body and not re.search(r"(^|[\s,>])h[123]\b", sel):
            continue
        m = re.search(r"line-height:\s*([\d.]+)(px)?", body) or re.search(r"font:[^;]*?/([\d.]+)(px)?\s", body)
        if m and not m.group(2):
            out.append((sel, float(m.group(1))))
    return out


class TestTextosDosAchados(unittest.TestCase):
    """As frases que os achados mandaram tirar não voltam; as que mandaram pôr estão lá."""

    @classmethod
    def setUpClass(cls):
        cls.raw = {p: read(p) for p in PAGES}
        cls.txt = {p: visible(cls.raw[p]) for p in PAGES}
        cls.readme = flat(read("README.md"))
        cls.all = " ".join(cls.txt.values())

    def nao_tem(self, frase, onde=None, ci=False):
        """A frase não aparece no texto visível (ci: sem diferenciar maiúsculas)."""
        for p in onde or PAGES:
            txt = self.txt[p].lower() if ci else self.txt[p]
            self.assertNotIn(frase.lower() if ci else frase, txt, f"{p} ainda diz {frase!r}")

    def test_codigo_fonte_disponivel_sem_licenca(self):   # [copy-codigo-aberto-sem-licenca]
        self.nao_tem("código aberto", ci=True)
        self.assertNotIn("código aberto", self.readme.lower())
        for p in PAGES:
            self.assertIn("código-fonte disponível", self.txt[p], f"{p}: rodapé sem 'código-fonte disponível'")
        self.assertIn("código-fonte disponível", self.readme)
        self.assertIn("A licença ainda não foi escolhida", self.readme)

    def test_slogan_numa_forma_so(self):   # [texto-rotulos-termos-gramatica] (16)
        for frase in ("têm que ser as pessoas", "devem ser as pessoas", "têm de ser as pessoas"):
            self.nao_tem(frase)
            self.assertNotIn(frase, self.readme)
        for p in PAGES:
            self.assertIn(SLOGAN, self.txt[p], f"{p}: rodapé sem o slogan")

    def test_descentralizacao_com_ressalva(self):   # [copy-descentralizacao-sem-ressalva]
        for frase in ("Sem dono, sem catraca", "Ninguém é dono da rede", "congela, confisca ou censura",
                      "rede sem dono", "Se um sair do ar, os outros continuam entregando", "e entrega para quem visita"):
            self.nao_tem(frase)
        self.assertIn("Hoje um único servidor do projeto ainda assina os blocos", self.txt["index.html"])
        tut = self.txt["tutorial.html"]
        self.assertIn("hoje um servidor do projeto assina os blocos e entrega os sites", tut)
        # v2.4: mudar as regras não zera nada, mas o validador único ainda poderia recriar a rede (dito com todas as letras)
        self.assertIn("mudar as regras não zera nada", tut)
        self.assertIn("poderia recriar a rede do zero, com todos os saldos", tut)
        self.assertIn("Sites passam pelo servidor da rede.", tut)
        self.assertIn("A rede pode zerar os saldos?", tut)
        self.nao_tem("não zerar os saldos")   # §14: sem essa promessa
        self.assertNotIn("Não há empresa no meio", self.readme)
        self.assertIn("Hoje um servidor do projeto ainda valida os blocos e entrega os sites", self.readme)
        self.assertIn("RESET=1", self.readme)

    def test_ia_cifrada_sem_prometer_demais(self):   # [priv-textos-ia-cifrada] [ux-ia-sem-aviso-privacidade]
        self.nao_tem("ainda não são criptografados")
        self.nao_tem("ponta a ponta", ci=True)
        self.nao_tem("uma máquina escolhida a cada pedido")
        tut, idx = self.txt["tutorial.html"], self.txt["index.html"]
        self.assertIn("Sua pergunta vai cifrada, mas alguém precisa ler.", tut)
        self.assertIn("quem controla a máquina pode mudar o programa e guardar", tut)
        self.assertIn("Continua público: que você fez um pedido, de qual carteira, para qual modelo e máquina", tut)
        self.assertIn("Privacidade da IA.", tut)
        self.assertIn("A máquina que responde lê o que eu escrevo?", idx)
        self.assertIn("Lê, porque precisa ler para responder.", idx)
        self.assertNotIn("criptografia nos pedidos de IA", idx)
        self.assertIn("Pedidos de IA cifrados.", self.readme)
        self.assertIn("a máquina que responde precisa ler o pedido", self.readme)

    def test_modelos_seguro_e_licenca(self):   # [seg-copy-modelos-seguro-licenca] [ux-sigmacode-aba-sem-travas]
        for frase in ("não roda programa de estranhos", "Não roda programa de estranhos", "licença livre",
                      "O app não roda código enviado por outras pessoas: só modelos de IA conferidos"):
            self.nao_tem(frase)
        sigma_code = "Fora do Sigma Code, o app não roda código de ninguém"
        self.assertIn(sigma_code, self.txt["index.html"])
        self.assertIn(sigma_code, self.txt["tutorial.html"])
        self.assertIn("No Sigma Code, cada comando passa por você antes de rodar", self.txt["baixar.html"])
        for p in ("index.html", "tutorial.html"):
            self.assertIn("Isso garante que o arquivo é exatamente o registrado, não que ele seja bom.", self.txt[p])
        self.assertIn("quem apadrinha responde pela licença do modelo que trouxe", self.txt["index.html"])
        self.assertIn("baixa do endereço que o padrinho registrou", self.txt["baixar.html"])

    def test_sigma_code_no_site(self):   # [ux-sigmacode-aba-sem-travas], SPEC §2.3
        tut = self.txt["tutorial.html"]
        self.assertIn("Programar", tut)
        self.assertIn("Aprovar alterações de arquivos sem perguntar", tut)
        self.assertIn("comandos sempre pedem aprovação", tut)
        self.assertIn("Arquivos de senha e chaves", tut)
        self.assertIn("O Sigma Code mexe nos meus arquivos?", self.txt["index.html"])
        self.assertIn("Sigma Code no painel (aba Programar)", self.readme)

    def test_custos_energia_memoria_bateria(self):   # [copy-custos-energia-memoria] [fluxo-minera-na-bateria] [ux-uso-oculto-ia]
        self.nao_tem("deixe na tomada")
        self.nao_tem("o Sigma sai da frente")
        for p in ("index.html", "tutorial.html"):
            self.assertIn("watts a mais × horas ligado ÷ 1000 = kWh", self.txt[p])
            self.assertIn("A IA é diferente.", self.txt[p])
            self.assertIn("pausa a mineração quando", self.txt[p])
        self.assertIn("Ao Sigma, não", self.txt["index.html"])
        self.assertIn("não custa nada além da energia do seu computador", self.txt["tutorial.html"])
        self.assertIn("Minerar também na bateria", self.txt["baixar.html"])
        self.assertIn("até 40% da memória e 20 GB do disco", self.txt["baixar.html"])
        self.assertIn("Desligue “IA para a rede”", self.txt["baixar.html"])
        self.assertIn("Gasta energia, e a conta de luz é sua.", self.readme)

    def test_limite_de_mineracao_e_varios_pcs(self):   # [copy-limite-por-ip-cgnat], SPEC §5 e §14
        self.nao_tem("4 relatórios")
        self.nao_tem("até 4 computadores")
        self.nao_tem("Os outros continuam hospedando")
        for p in ("index.html", "tutorial.html"):
            self.assertIn("256 relatórios", self.txt[p])
            self.assertIn("CGNAT", self.txt[p])
            self.assertIn("mesma carteira", self.txt[p])
        self.assertIn("cada um conta separado", self.txt["tutorial.html"])
        self.assertIn("256 relatórios", self.readme)
        self.assertIn("Vários computadores podem usar a mesma carteira", self.readme)

    def test_chave_metamask(self):   # [seg-copy-chave-metamask]
        for frase in ("use a da MetaMask", "importe a da MetaMask", "exportou da MetaMask", "exportada da MetaMask",
                      "Ninguém do Sigma vai pedir sua chave privada. Nunca.",
                      "Exporte a chave no painel e importe na Carteira ou na MetaMask"):
            self.nao_tem(frase)
            self.assertNotIn(frase, self.readme)
        self.assertIn("Não cole no app a chave da conta onde você guarda outras criptomoedas", self.txt["index.html"])
        self.assertIn("O app só pede a chave na tela dele, no seu computador", self.txt["baixar.html"])
        self.assertIn("O app só pede a chave na tela dele, no seu computador", self.txt["tutorial.html"])
        self.assertIn("Não cole aqui a chave da conta onde você guarda outras criptomoedas", self.txt["carteira.html"])
        forum = self.txt["forum.html"]
        self.assertIn("Ela é só do Sigma, então pode importá-la na MetaMask: no painel, Carteira → Mostrar minha chave.", forum)
        self.assertIn("Entre com a MetaMask ou crie uma carteira neste navegador", forum)

    def test_download_honesto(self):   # [seg-download-confianca-sslip-sha] [fluxo-macos-sem-certificados]
        bx = self.txt["baixar.html"]
        for frase in ("Baixe só deste site", "O certificado que tira esse aviso",
                      "Se o antivírus mandar o arquivo para a quarentena e o SHA-256 bater"):
            self.nao_tem(frase, ["baixar.html"])
        self.assertIn("Mas não protege você de um site falso que publique a lista dele", bx)
        self.assertIn("Baixe só pelo endereço oficial: sigma.195-35-29-232.sslip.io", bx)
        self.assertIn("com o endereço oficial conferido e o SHA-256 batendo", bx)
        self.assertIn("mesmo assinado, o Windows pode continuar avisando", bx)
        self.assertIn("Install Certificates.command", bx)
        self.assertIn("antigo demais", bx)
        self.assertIn("confira se você baixou do endereço oficial e se o SHA-256 bate", self.txt["index.html"])
        self.assertIn("não protege de um site falso que publique a lista dele", self.readme)
        self.assertIn("Install Certificates.command", self.readme)

    def test_hero_e_chamado(self):   # [design-hero-acentos-dobra] [copy-chamado-papeis-compartilhar] [copy-manifesto-data-center]
        idx, raw = self.txt["index.html"], self.raw["index.html"]
        self.assertNotIn("mais fortes que qualquer data center", idx)
        self.assertIn("rodam IA aberta sem depender do data center de ninguém", idx)
        self.assertNotIn("Ninguém recebe sem trabalhar", raw)
        self.assertEqual(raw.count("Nenhum SIG sai de graça."), 2)       # o texto inicial e o do script
        roles = re.findall(r'<div class="role[^"]*"><b>(.*?)</b>', raw)
        self.assertEqual(roles, ["Ligue seu computador", "Espalhe a palavra", "Publique um site", "Apadrinhe um modelo",
                                 "Programe com a gente", "Entre no fórum"])
        self.assertNotIn("pague a hospedagem dele em SIG", idx)
        self.assertIn("Com o SIG que você minerar", idx)
        self.assertIn('<a class="btn big" href="#passos">Como começar</a>', raw)      # (15): não é "Como funciona"
        self.assertEqual(len(re.findall(r"<div><b>[^<]+</b><span>", raw.split('class="principles')[1].split("</section>")[0])), 6)

    def test_rotulos_do_painel_e_gramatica(self):   # [texto-rotulos-termos-gramatica] [ux-termos-inconsistentes]
        self.nao_tem("rodada", ci=True)
        for frase in ("Iniciar com o computador", "Importar uma chave", "Criar uma nova", "Só o endereço",
                      "Budget", "budget", "Réplica", "réplica", "controle remoto da rede", "tem vírus", "provedor",
                      "Sai um por segundo", "um bloco novo a cada segundo", "preço garantido", "ninguém vende SIG",
                      "O dinheiro vai", "0xABC….", "direto da chain", "nó offline", "1 respostas"):
            self.nao_tem(frase)
        self.assertIn("Isso não quer dizer que o Sigma tenha vírus.", self.txt["baixar.html"])
        self.assertIn("“Iniciar o Sigma junto com o computador”", self.txt["baixar.html"])
        self.assertIn("clique em Salvar e depois em Desligar", self.txt["baixar.html"])
        for p in ("baixar.html", "tutorial.html"):
            for rotulo in ("Criar uma carteira nova", "Usar minha chave", "Só colar um endereço"):
                self.assertIn(rotulo, self.txt[p])
        self.assertIn("“Só colar um endereço”", self.raw["carteira.html"])
        self.assertIn("Orçamento", self.txt["tutorial.html"])
        self.assertIn("<dt>Cópia</dt>", self.raw["tutorial.html"])
        self.assertIn("(PC na rede)", self.txt["tutorial.html"])
        self.assertIn("letras minúsculas sem acento", self.txt["tutorial.html"])
        self.assertIn("O SIG não é investimento e não tem preço de mercado", self.txt["index.html"])
        self.assertIn("Sites da rede rodam isolados: não podem usar cookies nem localStorage.", self.txt["tutorial.html"])  # SPEC §3

    def test_promessa_dos_sites(self):   # [ux-site-promessa-exagerada] (o lado do site)
        self.nao_tem("ninguém derruba")
        self.assertIn("Por enquanto, quem visita recebe os arquivos pelo servidor da rede.", self.txt["tutorial.html"])
        self.assertIn("Por enquanto, quem visita recebe os arquivos pelo servidor", self.raw["carteira.html"])

    def test_aviso_site_de_terceiros(self):   # [site-phishing-dominio-oficial] (o lado do texto), SPEC §6
        # /site/ é conteúdo de terceiros: o aviso está em baixar.html (#conferir), tutorial.html#sites e no README
        for p in ("baixar.html", "tutorial.html"):
            self.assertIn("sites publicados por qualquer pessoa da rede, não pelo projeto", self.txt[p], p)
            self.assertIn("O Sigma oficial só se baixa no endereço oficial", self.txt[p], p)
            self.assertIn("nenhuma delas vai pedir a sua chave", self.txt[p], p)
        for frase in ("sites publicados por qualquer pessoa da rede, não pelo projeto",
                      "O Sigma oficial só se baixa no endereço oficial",
                      "nenhuma delas vai pedir a sua chave"):
            self.assertIn(frase, self.readme)
        # o passo a passo de segurança (#aviso, passo 1) também lembra que /site/ não é do projeto
        self.assertIn("sites de outras pessoas da rede, não do projeto, e nenhum deles é o Sigma", self.txt["baixar.html"])

    def test_privacidade_ia_varias_maquinas(self):   # [privacidade-ia-uma-maquina-so], SPEC §8
        # o singular que prometia uma máquina só (e "ninguém mais lê") não volta
        for frase in ("para aquela máquina", "ninguém mais consegue ler", "Só essa máquina consegue abrir",
                      "para essa máquina"):
            self.nao_tem(frase)
        self.assertNotIn("para essa máquina", self.readme)
        idx, tut = self.txt["index.html"], self.txt["tutorial.html"]
        # cada pergunta sorteia outra máquina e leva a conversa inteira; numa conversa longa, várias pessoas leem tudo
        self.assertIn("cada pergunta é sorteada para uma das máquinas que têm o modelo e leva a conversa inteira", idx)
        self.assertIn("várias pessoas diferentes da rede podem acabar lendo tudo o que você escreveu", idx)
        self.assertIn("Cada pergunta é sorteada para uma das máquinas que têm o modelo", tut)
        self.assertIn("numa conversa longa várias máquinas de pessoas diferentes leem tudo o que você já escreveu", tut)
        # cada passo do Sigma Code também sorteia e leva tudo o que já leu
        for p in (idx, tut):
            self.assertIn("Cada passo do Sigma Code", p)
            self.assertIn("leva tudo o que o agente já leu da pasta", p)
            self.assertIn("várias máquinas de outras pessoas acabam vendo os seus arquivos", p)
        self.assertIn("Cada nova pergunta é sorteada de novo e leva a conversa inteira", self.readme)
        self.assertIn("Cada passo do Sigma Code vai para uma máquina sorteada e leva tudo o que o agente já leu", self.readme)
        # o que os achados antigos mandaram manter continua de pé
        self.assertIn("Lê, porque precisa ler para responder.", idx)
        self.assertIn("quem controla a máquina pode mudar o programa e guardar", tut)
        self.assertIn("a máquina que responde precisa ler o pedido", self.readme)

    def test_site_expirado_perde_o_nome(self):   # [site-expirado-nome-tomado], SPEC §8
        tut = self.txt["tutorial.html"]
        self.nao_tem("É só recarregar para ele voltar")
        self.assertIn("Recarregue em até 7 dias para ele voltar com o mesmo nome", tut)
        self.assertIn("o nome fica livre para qualquer pessoa usar", tut)
        self.assertIn("em até 7 dias", tut)                                     # a FAQ repete o prazo
        # a ajuda de "Recarregar o orçamento do site" na Carteira (texto dentro do <script>)
        self.assertNotIn("Um site expirado volta ao ar ao ser recarregado.", self.raw["carteira.html"])
        self.assertIn("em até 7 dias", self.raw["carteira.html"])
        self.assertIn("o nome fica livre para qualquer pessoa", self.raw["carteira.html"])
        # a Carteira calcula e mostra o prazo de apagamento de um site expirado
        for trecho in ("function siteDelete", "PRUNE_WINDOWS", "s.expired_window", "apagado"):
            self.assertIn(trecho, self.raw["carteira.html"])


class TestContratosDoHTML(unittest.TestCase):
    """SRI, prévia de link, og.png, acentos e foco: o HTML e o CSS seguem a SPEC."""

    def test_ethers_com_integridade_na_carteira_e_no_forum(self):   # SPEC §4 [seg-forum-ethers-sem-sri]
        for p in ("carteira.html", "forum.html"):
            tags = Tags(read(p)).find("script", src=ETHERS)
            self.assertEqual(len(tags), 1, p)
            self.assertEqual(tags[0].get("integrity"), SRI, p)
            self.assertEqual(tags[0].get("crossorigin"), "anonymous", p)

    def test_nenhum_script_de_fora_sem_integridade(self):
        for p in PAGES:
            for t, a in Tags(read(p)).tags:
                if t == "script" and a.get("src", "").startswith(("http:", "https:", "//")):
                    self.assertTrue(a.get("integrity", "").startswith("sha384-"), f"{p}: {a['src']} sem integrity")

    def test_previa_do_link(self):   # [copy-chamado-papeis-compartilhar]
        for p in PAGES:
            metas = Tags(read(p)).find("meta")
            prop = {m.get("property") or m.get("name"): m.get("content") for m in metas}
            self.assertEqual(prop.get("og:image"), OFFICIAL + "/og.png", p)
            self.assertTrue(prop.get("og:url", "").startswith(OFFICIAL + "/"), p)
            self.assertEqual(prop.get("og:image:width"), "1200", p)
            self.assertEqual(prop.get("og:image:height"), "630", p)
            self.assertEqual(prop.get("twitter:card"), "summary_large_image", p)
            self.assertTrue(prop.get("og:title") and prop.get("og:description"), p)

    def test_og_png(self):
        with open(os.path.join(WEB, "og.png"), "rb") as f:
            head = f.read(32)
            size = os.fstat(f.fileno()).st_size
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(head[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", head[16:24]), (1200, 630))
        self.assertLess(size, 600_000)   # leve para o WhatsApp

    def test_no_serve_og_png(self):
        try:
            from sigma import node
        except Exception as e:   # outro módulo pode estar no meio de uma edição
            self.skipTest(f"sigma.node não importou: {e!r}")
        self.assertTrue(node.ASSET_RE.match("og.png"))
        if hasattr(node, "web_file"):
            self.assertEqual(node.web_file("og.png"), "og.png")      # o nó serve /og.png (a prévia do link)

    def test_titulos_com_acento_tem_entrelinha(self):   # [design-hero-acentos-dobra]
        # entrelinha abaixo de 1,12 só onde o texto nunca tem acento (números, setas, nomes sem acento)
        allowed = {".nav .brand", ".big-n", ".stamp", ".stat b", ".stats b", ".faq summary::after", ".cd-digits",
                   ".cd-side .amount", ".supply-head b", ".steps3 li + li::before", ".facts b", ".control b",
                   ".final h2", ".dl-hero h1", ".band-end h2", ".doc-head h1", "ol.steps > li::before", ".balance"}
        rules = {}
        for name in ("sigma.css",) + PAGES:
            for sel, lh in display_rules(name):
                rules[(name, sel)] = lh
        tight = {sel for (name, sel), lh in rules.items() if lh < 1.12}
        self.assertLessEqual(tight, allowed, f"títulos apertados demais para acento: {tight - allowed}")
        self.assertGreaterEqual(rules[("sigma.css", "h1, h2, h3")], 1.12)
        for name, sel in (("index.html", ".hero h1"), ("index.html", ".call h2"), ("index.html", ".punch"),
                          ("index.html", ".manifesto .quote"), ("index.html", ".principles b"), ("index.html", ".role b")):
            self.assertGreaterEqual(rules[(name, sel)], 1.12, sel)
        # os títulos apertados de propósito não têm acento
        for name, rx in (("index.html", r'<section class="sec final">.*?<h2>(.*?)</h2>'),
                         ("baixar.html", r'<h1>(Baixe.*?)</h1>'), ("baixar.html", r'band-end".*?<h2>(.*?)</h2>'),
                         ("tutorial.html", r'<h1>(Como.*?)</h1>')):
            m = re.search(rx, read(name), re.S)
            self.assertIsNotNone(m, rx)
            self.assertIsNone(ACCENTED.search(visible(m.group(1))), f"{name}: {m.group(1)!r} tem acento")

    def test_grade_dos_principios_e_do_chamado(self):   # [design-grade-principios-foco]
        css = css_of("index.html")
        self.assertIn(".principles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));", flat(css))
        self.assertIn("@media (max-width: 900px) { .principles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }", flat(css))
        self.assertIn(".roles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));", flat(css))
        self.assertNotIn("auto-fit, minmax(280px", css)

    def test_foco_visivel_nas_faixas_vermelhas(self):   # [design-grade-principios-foco]
        self.assertIn(".band-red :focus-visible { outline-color: var(--accent-ink); }", css_of("sigma.css"))
        self.assertIn(".role :focus-visible { outline-color: var(--accent); }", css_of("index.html"))

    def test_forum_cantos_retos_e_plural(self):   # [design-grade-principios-foco] [texto-rotulos-termos-gramatica] (13)
        page = read("forum.html")
        self.assertNotRegex(page, r"border-radius:\s*[1-9]")
        self.assertNotIn("${t.replies} respostas", page)
        self.assertIn('plural(t.replies, "resposta", "respostas")', page)

    def test_carteira_backup_obrigatorio_no_codigo(self):   # [ux-carteira-navegador-sem-backup]
        page = read("carteira.html")
        self.assertIn("const MIN_PASS = 10;", page)
        self.assertIn("Use uma senha com pelo menos 10 caracteres.", page)
        self.assertNotRegex(page, r"scrypt\s*:\s*\{")                       # sem N próprio: o padrão do ethers (2^17)
        self.assertRegex(page, r"encryptKeystoreJson\(\{address: w\.address, privateKey: w\.privateKey\}, pass\)")
        for frase in ("Guarde esta chave agora", "Copiar chave", "Guardei minha chave", "Criar ou importar outra carteira",
                      "Desbloquear a carteira deste navegador", "Criar ou importar uma carteira", "Substituir",
                      "Já existe uma carteira guardada neste navegador"):
            self.assertIn(frase, page)
        self.assertIn('<button id="ask-no" autofocus>Cancelar</button>', page)
        self.assertNotIn("Carteira criada! Use 'Mostrar chave'", page)
        self.assertNotIn("confirm(", page)
        self.assertNotIn("prompt(", page)

    def test_carteira_segue_o_protocolo_novo(self):   # SPEC §8 (preço da rede), §7 (estados), §9 (créditos)
        page = read("carteira.html")
        self.assertIn("hosting_per_mb_window", page)
        self.assertNotIn("p.price", page)                                    # os pins não guardam preço
        for estado in ("failed:", "canceled:", "expired:", "disputed:"):
            self.assertIn(estado, page)
        for kind in ("rent:", "job:", "royalty:", "change:", "refund:", "budget:"):
            self.assertIn(kind, page.split("const CREDIT")[1].split("};")[0])
        self.assertIn('h.type === "credit"', page)
        self.assertIn("cancel_job", page)

    def test_royalty_preenche_e_so_manda_o_que_mudou(self):   # [carteira-royalty-zerado], SPEC §8
        page = read("carteira.html")
        # o diálogo de Royalty abre com os valores atuais já preenchidos
        self.assertIn("prefill:", page)
        self.assertRegex(page, r"royalty:\s*m\.royalty_bps\s*/\s*100")
        self.assertRegex(page, r"reinvest:\s*m\.reinvest_bps\s*/\s*100")
        # campo vazio não é 0: só manda a chave que a pessoa preencheu
        self.assertIn('const filled = (i) => !!i && i.value.trim() !== "";', page)
        self.assertIn("filled(inputs.royalty)", page)
        self.assertIn("filled(inputs.reinvest)", page)
        # recusa fora de 0-50 (royalty) e 0-100 (reinvestimento)
        self.assertIn("v.royalty >= 0 && v.royalty <= 50", page)
        self.assertIn("v.reinvest >= 0 && v.reinvest <= 100", page)
        # quantidade de recarga vazia/zero (NaN/0) é recusada no fund-model e no fund-site
        self.assertEqual(page.count("if (!(v.amount > 0))"), 2)


# ------------------------------------------------------------------ Edge sem internet
DEAD_PROXY = 8899
PORTS = range(8892, 8899)
NOW = time.time()
ME = "sig" + "ab" * 20


def find_edge():
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.isfile(p):
            return p
    return next((shutil.which(n) for n in ("msedge", "microsoft-edge", "microsoft-edge-stable") if shutil.which(n)), None)


MEDIR = """<script>
setTimeout(() => {
  const d = document.documentElement, out = {sw: d.scrollWidth, cw: d.clientWidth, ih: innerHeight};
  const b = document.querySelector(".hero .btn.primary, #get-btn");
  if (b) { const r = b.getBoundingClientRect(); out.cta = [Math.round(r.top), Math.round(r.bottom)]; }
  const doc = parent !== window ? parent.document : document;
  const p = doc.createElement("pre"); p.id = "medida"; p.textContent = JSON.stringify(out); doc.body.appendChild(p);
}, 2500);
</script>"""

# ethers de mentira: o teste confere o caminho da página, não a criptografia (a integridade do arquivo de verdade
# é conferida no HTML, e o Edge do teste não sai para a internet)
ETHERS_STUB = """<script>
window.__calls = [];
window.ethers = (() => {
  const hex = (n) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, "0")).join("");
  class Wallet {
    constructor(key, provider) {
      if (!/^0x[0-9a-fA-F]{64}$/.test(key)) throw new Error("invalid private key");
      this.privateKey = key.toLowerCase(); this.address = "0x" + this.privateKey.slice(2, 42); this.provider = provider;
    }
    static createRandom() { return new Wallet("0x" + hex(32)); }
    async signMessage() { return "0x" + "11".repeat(65); }
    async sendTransaction() { return {hash: "0x" + "22".repeat(32)}; }
  }
  return {
    Wallet,
    async encryptKeystoreJson(account, password, ...rest) {
      __calls.push(rest.length);
      return JSON.stringify({address: account.address.slice(2).toLowerCase(), senha: password, chave: account.privateKey});
    },
    async decryptKeystoreJson(json, password) {
      const j = JSON.parse(json);
      if (j.senha !== password) throw new Error("incorrect password");
      return {address: "0x" + j.address, privateKey: j.chave};
    },
    getAddress: (a) => a, JsonRpcProvider: class {}, BrowserProvider: class {},
    hexlify: () => "0x", toUtf8Bytes: () => new Uint8Array(), parseUnits: (v) => BigInt(Math.round(Number(v) * 1e6)) * 10n ** 12n,
  };
})();
</script>"""

DRIVER = """<script>
const OLD = {address: "aa".repeat(20), senha: "senha-antiga-123", chave: "0x" + "aa".repeat(32)};
const CEN = new URLSearchParams(location.search).get("cenario");
try {   // o armazenamento é preparado antes da página ler
  if (CEN === "substituir") localStorage.setItem("sigma.keystore", JSON.stringify(OLD));
  else localStorage.removeItem("sigma.keystore");
} catch {}
const R = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const until = async (f, ms = 8000) => { for (let t = 0; t < ms; t += 50) { try { if (f()) return true; } catch {} await sleep(50); } return false; };
const g = (id) => document.getElementById(id);
const stored = () => { try { return JSON.parse(localStorage.getItem("sigma.keystore")); } catch { return null; } };
async function finishBackup() {
  g("key-check").click();
  R.continuar_ligado = !g("key-go").disabled;
  g("key-go").click();
  R.entrou = await until(() => !g("wallet").hidden);
  R.chave_apagada = g("key-value").textContent === "";
}
async function criar() {
  R.botao_vazio = g("btn-local").textContent;
  g("new-pass").value = "curta1234";
  g("btn-local").click();
  await sleep(300);
  R.toast_curta = g("toast").textContent;
  R.guardou_curta = stored() !== null;
  g("new-pass").value = "senha-bem-longa";
  g("btn-local").click();
  R.backup_aberto = await until(() => g("dlg-key").open);
  R.titulo = g("key-title").textContent;
  R.texto = g("key-text").textContent;
  R.chave = g("key-value").textContent;
  R.continuar_desligado = g("key-go").disabled;
  R.guardei_visivel = !g("key-check-row").hidden;
  R.copiar = g("key-copy").textContent;
  g("dlg-key").close();                                    // o navegador fecha à força (Esc duas vezes)...
  g("dlg-key").dispatchEvent(new Event("close"));          // ...e manda o "close" (o Edge sem tela não desenha quadros)
  await sleep(100);
  R.reabre = g("dlg-key").open;
  g("dlg-key").dispatchEvent(new Event("cancel", {cancelable: true}));   // Esc
  await sleep(100);
  R.esc_nao_fecha = g("dlg-key").open;
  R.carteira_escondida_antes = g("wallet").hidden;
  const s = stored();
  R.guardou = !!s && "0x" + s.address === "0x" + R.chave.slice(2, 42);
  R.kdf_padrao = __calls.length > 0 && __calls.every((n) => n === 0);
  await finishBackup();
  R.endereco = g("addr-sig").textContent;
}
async function substituir() {
  R.desbloquear = !g("unlock-box").hidden;
  R.novo_escondido = g("new-box").hidden;
  R.link_outro = !g("show-new-row").hidden;
  R.salvo = g("saved-addr").textContent;
  g("show-new").click();
  R.novo_aparece = !g("new-box").hidden;
  g("new-pass").value = "senha-bem-longa";
  g("btn-local").click();
  R.pergunta_aberta = await until(() => g("dlg-ask").open);
  R.pergunta = g("ask-text").textContent;
  R.sim = g("ask-yes").textContent;
  R.foco = document.activeElement && document.activeElement.id;
  g("ask-no").click();
  await sleep(300);
  R.cancelar_mantem = (stored() || {}).address === OLD.address && !g("dlg-key").open && g("wallet").hidden;
  g("btn-local").click();
  await until(() => g("dlg-ask").open);
  g("ask-yes").click();
  R.backup_depois = await until(() => g("dlg-key").open);
  R.trocou = (stored() || {}).address !== OLD.address;
  await finishBackup();
}
async function importar() {
  g("import-key").value = "0x" + "bb".repeat(32);
  g("import-key").dispatchEvent(new Event("input"));
  R.botao = g("btn-local").textContent;
  g("new-pass").value = "senha-bem-longa";
  g("btn-local").click();
  R.entrou = await until(() => !g("wallet").hidden);
  R.sem_backup = !g("dlg-key").open;
  R.endereco = g("addr-sig").textContent;
  await until(() => g("balance").textContent.includes("SIG"));
  await until(() => document.querySelector('[data-pane="activity"] table'));
  R.atividade = document.querySelector('[data-pane="activity"]').textContent.replace(/\\s+/g, " ");
  document.querySelector('[data-tab="models"]').click();
  await sleep(100);
  R.modelos = document.querySelector('[data-pane="models"]').textContent.replace(/\\s+/g, " ");
  document.querySelector('[data-tab="sites"]').click();
  await sleep(100);
  R.sites = document.querySelector('[data-pane="sites"]').textContent.replace(/\\s+/g, " ");
}
async function royalty() {
  // importa a carteira do padrinho (dona do modelo qwen) e abre o diálogo de Royalty
  g("import-key").value = "0x" + "bb".repeat(32);
  g("import-key").dispatchEvent(new Event("input"));
  g("new-pass").value = "senha-bem-longa";
  g("btn-local").click();
  await until(() => !g("wallet").hidden);
  await until(() => document.querySelector('[data-pane="activity"] table'));
  document.querySelector('[data-tab="models"]').click();
  await until(() => document.querySelector('[data-op="update-model"]'));
  const ops = [];
  ethers.toUtf8Bytes = (s) => { try { ops.push(JSON.parse(s)); } catch (e) {} return new Uint8Array(); };
  const field = (k) => g("op-fields").querySelector('[data-k="' + k + '"]');
  // 1) os campos abrem preenchidos com o royalty e o reinvestimento atuais (qwen: 10% / 50%)
  document.querySelector('[data-op="update-model"]').click();
  await until(() => g("dlg-op").open);
  R.royalty_preenchido = field("royalty").value;
  R.reinvest_preenchido = field("reinvest").value;
  // 2) muda só o reinvestimento: limpa o royalty e confirma; a tx não pode levar royalty_bps.
  // op-go trava enquanto tenta enviar (e destrava ao terminar): espero destravar antes do passo seguinte
  field("royalty").value = "";
  field("reinvest").value = "50";
  ops.length = 0;
  g("op-go").click();
  await until(() => !g("op-go").disabled);
  R.op_reinvest = ops.length ? ops[ops.length - 1] : null;
  // 3) valor fora da faixa é recusado antes de enviar (nada vai para a rede)
  if (!g("dlg-op").open) { document.querySelector('[data-op="update-model"]').click(); await until(() => g("dlg-op").open); }
  field("royalty").value = "60";
  field("reinvest").value = "50";
  ops.length = 0;
  g("op-go").click();
  await until(() => !g("op-go").disabled);
  R.op_fora = ops.length ? ops[ops.length - 1] : null;
  R.toast_fora = g("toast").textContent;
  // 4) recarregar o modelo com a quantidade vazia é recusado
  if (g("dlg-op").open) g("dlg-op").close();
  document.querySelector('[data-op="fund-model"]').click();
  await until(() => g("dlg-op").open);
  field("amount").value = "";
  ops.length = 0;
  g("op-go").click();
  await until(() => !g("op-go").disabled);
  R.op_recarga_vazia = ops.length ? ops[ops.length - 1] : null;
  R.toast_recarga = g("toast").textContent;
}
addEventListener("load", async () => {
  try {
    await until(() => g("net").textContent.includes("bloco"));
    await ({criar, substituir, importar, royalty})[CEN]();
  } catch (e) { R.erro = String(e && e.stack || e); }
  const p = document.createElement("pre"); p.id = "resultado"; p.textContent = JSON.stringify(R); document.body.appendChild(p);
});
</script>"""


def fake_api(path):
    """Respostas no formato do sigma/node.py (só o que as páginas leem)."""
    p = [x for x in path.strip("/").split("/") if x]
    u = 1_000_000
    minted = 2_500_000 * u
    mining = {"window": 4000, "window_seconds": 300, "window_ends": NOW + 120, "epoch": 12, "epoch_seconds": 43200,
              "epoch_ends": NOW + 5 * 3600, "minted": minted, "cap": 100_000_000 * u,
              "next_mint": (100_000_000 * u - minted) * 50 // 10000, "points_total": 10 ** 9, "miners_epoch": 42,
              "hashrate": 123456.7, "hosting_per_mb_window": 7, "ai_default_price": 2000, "now": time.time()}
    pc = "sig" + "11" * 20
    if p == ["status"]:
        return {"version": "2.1.0", "height": 12345, "machines": 37, "sites": 1, "models": 1, "chain_id": 51515,
                "block_interval": 3.2, "now": time.time(), "forum": {"users": 128},
                "supply": {k: mining[k] for k in ("minted", "cap", "epoch", "epoch_ends", "next_mint", "miners_epoch", "hashrate")}}
    if p == ["mining"]:
        return mining
    if p == ["mining", "epochs"]:
        return [{"epoch": 11, "end": NOW - 7 * 3600, "mint": 497_500 * u, "miners": 40, "top": [[ME, 1234 * u]]}]
    if p[:2] == ["mining", "miner"]:
        return {"points": 5 * 10 ** 7, "share": .05, "est_reward": 24_000 * u, "rewards_total": 1234 * u,
                "last_window": 3999, "last_rate": 4321.0}
    if p == ["machines"]:
        return {pc: {"name": "PC da Ana", "payout": ME, "online": True, "ai": True, "models": ["qwen2.5-0.5b"],
                     "earned": 3 * u, "reputation": 4, "hw": {"cpu_cores": 8, "ram_mb": 16000, "disk_mb": 200000, "gpu": ""}}}
    if p == ["models"]:
        return {"qwen2.5-0.5b": {"name": "qwen2.5-0.5b", "sponsor": "sig" + "bb" * 20, "size": 490_000_000, "license": "apache-2.0",
                                 "royalty_bps": 1000, "reinvest_bps": 5000, "replicas": 2, "budget": 80 * u, "rent_paid": 3 * u,
                                 "royalties": 120_000, "tokens": 45678, "jobs": 1, "assigned": {},
                                 "pinners": {pc: {"window": 3990, "misses": 0, "miss_window": -1}}}}
    if p == ["sites"]:
        # meu-site no ar; coletivo expirado na janela 3990 (window atual 4000): some em expired_window + PRUNE_WINDOWS
        return {"meu-site": {"name": "meu-site", "owner": "sig" + "bb" * 20, "manifest": {"index.html": {"hash": "dd" * 32, "size": 2_000_000}},
                             "budget": 2 * u, "replicas": 2, "pinners": {pc: {"window": 3990}}, "status": "active", "size": 2_000_000},
                "coletivo": {"name": "coletivo", "owner": "sig" + "bb" * 20, "manifest": {"index.html": {"hash": "ee" * 32, "size": 1_000_000}},
                             "budget": 3, "replicas": 2, "pinners": {}, "status": "expired", "expired_window": 3990, "size": 1_000_000}}
    if p[:1] == ["account"]:
        return {"address": p[1], "balance": 1234 * u, "nonce": 3}
    if p[:1] == ["history"]:
        return [{"id": None, "type": "credit", "kind": "rent", "epoch": 12, "amount": 123456, "count": 40, "to": p[1], "via": "rede",
                 "name": None, "refs": ["meu-site", "qwen2.5-0.5b"], "height": 12340, "time": NOW},
                {"id": None, "type": "credit", "kind": "refund", "epoch": 12, "amount": 4000, "count": 1, "to": p[1], "via": "rede",
                 "name": None, "refs": [], "height": 12330, "time": NOW},
                {"id": "aa" * 32, "type": "create_job", "from": p[1], "to": None, "amount": 4000, "via": "sigma", "height": 12320, "time": NOW},
                {"id": None, "type": "mint", "from": None, "to": p[1], "amount": 1234 * u, "epoch": 11, "via": "mineração",
                 "height": 12000, "time": NOW}]
    if p[:1] == ["job"]:
        return {"id": p[1], "kind": "ai", "model": "qwen2.5-0.5b", "status": "canceled"}
    if p == ["blocks"]:
        return {"blocks": []}
    if p[:2] == ["forum", "api"]:
        r = p[2:]
        if r == ["categories"]:
            return {"geral": "Geral", "ajuda": "Ajuda", "desenvolvimento": "Desenvolvimento"}
        if r == ["me"]:
            return {"user": None}
        if r == ["threads"]:
            return [{"id": 1, "title": "Um tópico", "category": "geral", "author": ME, "author_name": "ana", "replies": 1, "last_post": NOW},
                    {"id": 2, "title": "Outro tópico", "category": "ajuda", "author": ME, "author_name": None, "replies": 2, "last_post": NOW}]
    return None


def make_handler(down):
    page_names = {"": "index.html", "baixar": "baixar.html", "tutorial": "tutorial.html", "carteira": "carteira.html",
                  "forum": "forum.html", "sigma.css": "sigma.css", "og.png": "og.png"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            name = u.path.strip("/")
            if name == "frame":   # a página num quadro de largura exata (a janela sem tela tem largura mínima)
                body = (f'<!doctype html><body style="margin:0"><iframe src="{html.escape(q["src"][0])}" '
                        f'style="width:{int(q["w"][0])}px;height:{int(q["h"][0])}px;border:0;display:block"></iframe>').encode()
                return self.send(200, body, "text/html; charset=utf-8")
            if name in page_names or name == "carteira-teste":
                fn = page_names.get(name, "carteira.html")
                with open(os.path.join(WEB, fn), "rb") as f:
                    body = f.read()
                if fn.endswith(".css"):
                    return self.send(200, body, "text/css; charset=utf-8")
                if fn.endswith(".png"):
                    return self.send(200, body, "image/png")
                if name == "carteira-teste":
                    tag = re.search(rb'<script src="https://cdn\.jsdelivr\.net/npm/ethers[^>]*></script>', body).group(0)
                    body = body.replace(tag, (ETHERS_STUB + DRIVER).encode())
                if "medir" in q:
                    body = body.replace(b"</body>", MEDIR.encode() + b"</body>")
                return self.send(200, body, "text/html; charset=utf-8")
            if down:   # o nó fora do ar: o nginx responde uma página de erro
                return self.send(502, b"<html><body>502 Bad Gateway</body></html>", "text/html")
            if name == "downloads/SHA256SUMS.txt":
                return self.send(200, ("ab" * 32 + "  Sigma.exe\n" + "cd" * 32 + "  sigma.pyz\n").encode(), "text/plain")
            if name.startswith("downloads/"):
                return self.send(200, b"x" * 2048, "application/octet-stream")
            res = fake_api(u.path)
            if res is None:
                return self.send(404, b'{"error": "n\\u00e3o encontrado"}', "application/json")
            return self.send(200, json.dumps(res).encode(), "application/json")

    return H


class Server(ThreadingHTTPServer):
    request_queue_size = 128   # vários Edge ao mesmo tempo
    daemon_threads = True
    # no Windows, SO_REUSEADDR deixa dois servidores na mesma porta (o segundo "rouba" as conexões): porta ocupada
    # tem que falhar, para o teste passar para a próxima
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):   # o Edge fecha conexões ao sair: não é erro
            super().handle_error(request, client_address)


def start_server(down):
    for port in PORTS:
        try:
            srv = Server(("127.0.0.1", port), make_handler(down))
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
    return None


CONSOLE = re.compile(r':CONSOLE[^\]]*\] "(.*?)", source: (\S*)')


def edge_dom(url, w=1366, h=900, budget=9000, extra=()):
    """DOM depois de rodar a página no Edge sem janela, e os erros de JavaScript da página."""
    prof = tempfile.mkdtemp(prefix="site-edge-")
    args = [find_edge(), "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={w},{h}",
            f"--virtual-time-budget={budget}", f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--disable-default-apps", "--disable-domain-reliability", "--no-pings", "--metrics-recording-only",
            "--disable-features=Translate,OptimizationHints,MediaRouter,msSmartScreenProtection,AutofillServerCommunication",
            f"--proxy-server=127.0.0.1:{DEAD_PROXY}", "--force-prefers-reduced-motion", "--lang=pt-BR",
            "--enable-logging=stderr", "--v=0", *extra, "--dump-dom", url]
    if not any(a.startswith("--host-resolver-rules") for a in extra):
        args.insert(-2, "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1")
    try:
        r = subprocess.run(args, capture_output=True, timeout=180)
        dom = r.stdout.decode("utf-8", "replace")
        errors = [m.group(1) for m in CONSOLE.finditer(r.stderr.decode("utf-8", "replace"))
                  if m.group(2).startswith("http://127.0.0.1:") and "Uncaught" in m.group(1)]
        return dom, errors
    finally:
        for _ in range(20):
            shutil.rmtree(prof, ignore_errors=True)
            if not os.path.exists(prof):
                break
            time.sleep(0.25)


def pre(dom, el_id):
    m = re.search(rf'<pre id="{el_id}">(.*?)</pre>', dom, re.S)
    return json.loads(html.unescape(m.group(1))) if m else None


@unittest.skipIf(os.environ.get("SIGMA_SEM_EDGE") == "1", "SIGMA_SEM_EDGE=1")
@unittest.skipUnless(find_edge(), "Edge não encontrado")
class TestNoEdge(unittest.TestCase):
    """As páginas de verdade no Edge, sem internet: largura de celular e de notebook, rede fora do ar e a carteira."""

    @classmethod
    def setUpClass(cls):
        cls.on, cls.off = start_server(False), start_server(True)
        if not (cls.on and cls.off):
            for s in (cls.on, cls.off):
                if s:
                    s.shutdown(); s.server_close()
            raise unittest.SkipTest("sem porta livre entre 8892 e 8898")
        cls.base = f"http://127.0.0.1:{cls.on.server_address[1]}"
        cls.base_off = f"http://127.0.0.1:{cls.off.server_address[1]}"
        jobs = {}
        for page in ("/", "/baixar", "/tutorial", "/carteira", "/forum"):
            src = quote(page + "?medir=1", safe="")
            jobs[("375", page)] = (f"{cls.base}/frame?w=375&h=812&src={src}", 600, 900, 9000, ())
            jobs[("1366", page)] = (f"{cls.base}/frame?w=1366&h=657&src={src}", 1400, 800, 9000, ())
            jobs[("off", page)] = (cls.base_off + page, 1366, 900, 9000, ())
        jobs[("on", "/forum")] = (cls.base + "/forum", 1366, 900, 6000, ())
        for cen in ("criar", "substituir", "importar", "royalty"):
            jobs[("carteira", cen)] = (f"{cls.base}/carteira-teste?cenario={cen}", 1366, 900, 30000, ())
        jobs[("falso", "/baixar")] = (f"http://sigma.falso.test:{cls.on.server_address[1]}/baixar", 1366, 900, 6000,
                                      ("--host-resolver-rules=MAP sigma.falso.test 127.0.0.1 , MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
                                       "--proxy-bypass-list=sigma.falso.test"))
        with ThreadPoolExecutor(4) as ex:
            futs = {k: ex.submit(edge_dom, *v) for k, v in jobs.items()}
            cls.res = {k: f.result() for k, f in futs.items()}

    @classmethod
    def tearDownClass(cls):
        for s in (cls.on, cls.off):
            s.shutdown()
            s.server_close()

    def test_sem_estouro_horizontal_e_sem_erro(self):
        for width in ("375", "1366"):
            for page in ("/", "/baixar", "/tutorial", "/carteira", "/forum"):
                dom, errors = self.res[(width, page)]
                m = pre(dom, "medida")
                self.assertIsNotNone(m, f"{page} em {width} px não terminou de carregar")
                self.assertEqual(m["cw"], int(width), page)
                self.assertLessEqual(m["sw"], m["cw"], f"{page} em {width} px rola para o lado")
                self.assertEqual(errors, [], f"{page} em {width} px: erro de JavaScript")

    def test_botao_de_baixar_na_primeira_tela_do_notebook(self):   # [design-hero-acentos-dobra]: 1366×657
        for page in ("/", "/baixar"):
            m = pre(self.res[("1366", page)][0], "medida")
            self.assertIn("cta", m, page)
            top, bottom = m["cta"]
            self.assertGreaterEqual(top, 0, page)
            self.assertLessEqual(bottom, 657, f"{page}: o botão Baixar fica abaixo da dobra ({bottom} px)")

    def test_rede_fora_do_ar(self):
        texts = {p: visible(self.res[("off", p)][0]) for p in ("/", "/baixar", "/tutorial", "/carteira", "/forum")}
        self.assertIn("rede fora do ar", texts["/"])
        self.assertIn("Sem resposta da rede agora", texts["/"])
        self.assertIn("A lista ainda não está disponível neste servidor", texts["/baixar"])
        self.assertIn("51515", texts["/tutorial"])
        self.assertIn("rede fora do ar", texts["/carteira"])
        self.assertIn("O fórum não respondeu", texts["/forum"])
        self.assertIn("Tentar de novo", texts["/forum"])
        for p in texts:
            self.assertEqual(self.res[("off", p)][1], [], f"{p}: erro de JavaScript com a rede fora do ar")

    def test_forum_plural_e_login_sem_ethers(self):   # [texto-rotulos-termos-gramatica] (13) [seg-forum-ethers-sem-sri]
        dom, errors = self.res[("on", "/forum")]
        text = visible(dom)
        self.assertIn("· 1 resposta ·", text)
        self.assertIn("· 2 respostas ·", text)
        self.assertNotIn("1 respostas", text)
        self.assertEqual(errors, [])
        # o ethers do CDN não carrega aqui (sem internet): a página segue de pé e a MetaMask não depende dele
        self.assertIn("utf8Hex(m)", read("forum.html"))

    def test_carteira_nova_obriga_a_guardar_a_chave(self):   # [ux-carteira-navegador-sem-backup]
        r = pre(self.res[("carteira", "criar")][0], "resultado")
        self.assertIsNotNone(r)
        self.assertNotIn("erro", r, r.get("erro"))
        self.assertEqual(r["botao_vazio"], "Criar carteira")
        self.assertEqual(r["toast_curta"], "Erro: Use uma senha com pelo menos 10 caracteres.")
        self.assertFalse(r["guardou_curta"])
        self.assertTrue(r["backup_aberto"])
        self.assertEqual(r["titulo"], "Guarde esta chave agora")
        self.assertIn("ela some, e os SIG somem junto", r["texto"])
        self.assertRegex(r["chave"], r"^0x[0-9a-f]{64}$")
        self.assertTrue(r["continuar_desligado"])
        self.assertTrue(r["guardei_visivel"])
        self.assertEqual(r["copiar"], "Copiar chave")
        self.assertTrue(r["esc_nao_fecha"])
        self.assertTrue(r["reabre"])
        self.assertTrue(r["carteira_escondida_antes"])     # não entra na carteira antes de guardar a chave
        self.assertTrue(r["guardou"])
        self.assertTrue(r["kdf_padrao"])                   # encryptKeystoreJson sem opções: scrypt padrão do ethers
        self.assertTrue(r["continuar_ligado"])
        self.assertTrue(r["entrou"])
        self.assertTrue(r["chave_apagada"])
        self.assertEqual(r["endereco"], "sig" + r["chave"][2:42])

    def test_carteira_guardada_nao_some_num_clique(self):   # [ux-carteira-navegador-sem-backup]
        r = pre(self.res[("carteira", "substituir")][0], "resultado")
        self.assertIsNotNone(r)
        self.assertNotIn("erro", r, r.get("erro"))
        self.assertTrue(r["desbloquear"])
        self.assertTrue(r["novo_escondido"])
        self.assertTrue(r["link_outro"])
        self.assertEqual(r["salvo"], "sig" + "aa" * 20)
        self.assertTrue(r["novo_aparece"])
        self.assertTrue(r["pergunta_aberta"])
        self.assertIn("Já existe uma carteira guardada neste navegador (sig" + "aa" * 20 + ")", r["pergunta"])
        self.assertIn("Você tem essa chave anotada?", r["pergunta"])
        self.assertEqual(r["sim"], "Substituir")
        self.assertEqual(r["foco"], "ask-no")             # o foco começa em Cancelar
        self.assertTrue(r["cancelar_mantem"])
        self.assertTrue(r["backup_depois"])
        self.assertTrue(r["trocou"])
        self.assertTrue(r["entrou"])

    def test_carteira_importada_e_protocolo_novo(self):   # SPEC §7, §8, §9 na carteira
        r = pre(self.res[("carteira", "importar")][0], "resultado")
        self.assertIsNotNone(r)
        self.assertNotIn("erro", r, r.get("erro"))
        self.assertEqual(r["botao"], "Importar chave")
        self.assertTrue(r["entrou"])
        self.assertTrue(r["sem_backup"])                   # quem importa já tem a chave
        self.assertEqual(r["endereco"], "sig" + "bb" * 20)
        a = r["atividade"]
        self.assertIn("Aluguel por guardar sites e modelos · época 12", a)
        self.assertIn("meu-site, qwen2.5-0.5b · 40 lançamentos", a)
        self.assertIn("Devolução de pedidos · época 12", a)
        self.assertIn("cancelado: valor devolvido", a)
        self.assertIn("Recompensa da mineração · época 11", a)
        # aluguel com o preço da rede (/mining: 7 µSIG por MB): 490 MB → 3.430 µSIG por janela, 1 cópia online
        self.assertIn("0,00343 SIG", r["modelos"])
        self.assertIn("1/2 cópias", r["modelos"])
        # site de 2 MB: 14 µSIG por janela; 2 SIG duram 142.857 janelas de 5 min ≈ 496 dias
        self.assertIn("~496 dias", r["sites"])
        # site expirado "coletivo": a Carteira mostra o prazo até a rede apagá-lo e liberar o nome
        self.assertIn("expirado", r["sites"])            # [site-expirado-nome-tomado]
        self.assertIn("apagado em ~7 dias", r["sites"])
        self.assertIn("o nome fica livre", r["sites"])

    def test_carteira_royalty_nao_zera(self):   # [carteira-royalty-zerado], SPEC §8
        r = pre(self.res[("carteira", "royalty")][0], "resultado")
        self.assertIsNotNone(r)
        self.assertNotIn("erro", r, r.get("erro"))
        # 1) o diálogo abre com os valores atuais (qwen: royalty 10%, reinvest 50%)
        self.assertEqual(r["royalty_preenchido"], "10")
        self.assertEqual(r["reinvest_preenchido"], "50")
        # 2) mexer só no reinvestimento NÃO manda royalty_bps: o royalty do padrinho não vira 0
        op = r["op_reinvest"]
        self.assertIsNotNone(op, "nada foi enviado ao mudar o reinvestimento")
        self.assertEqual(op["type"], "update_model")
        self.assertEqual(op["data"].get("reinvest_bps"), 5000)
        self.assertNotIn("royalty_bps", op["data"])
        # 3) royalty fora de 0-50 é recusado no cliente, sem enviar nada
        self.assertIsNone(r["op_fora"])
        self.assertIn("0 a 50", r["toast_fora"])
        # 4) recarregar com a quantidade vazia é recusado, sem enviar nada
        self.assertIsNone(r["op_recarga_vazia"])
        self.assertIn("maior que zero", r["toast_recarga"])

    def test_aviso_de_endereco_que_nao_e_o_oficial(self):   # [seg-download-confianca-sslip-sha]
        on = visible(self.res[("off", "/baixar")][0])
        self.assertNotIn("não é o endereço oficial do Sigma", on)            # 127.0.0.1: rede local, sem aviso
        falso = visible(self.res[("falso", "/baixar")][0])
        self.assertIn("Atenção: sigma.falso.test não é o endereço oficial do Sigma. O oficial é sigma.195-35-29-232.sslip.io.", falso)


if __name__ == "__main__":
    unittest.main()
