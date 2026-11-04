"""CLI da rede Sigma, para quem gosta de terminal. O jeito simples é o app (python -m sigma.app):
ele minera, hospeda a rede e mostra tudo num painel.

    python -m sigma.cli wallet                         cria/mostra a carteira (arquivo wallet.key)
    python -m sigma.cli balance [endereço]
    python -m sigma.cli send <endereço> <SIG>
    python -m sigma.cli mining                         emissão: impresso/teto, próxima distribuição, poder da rede
    python -m sigma.cli miner [endereço]               seus pontos na época e a recompensa estimada
    python -m sigma.cli epochs                         distribuições passadas
    python -m sigma.cli machines                       computadores que hospedam a rede
    python -m sigma.cli machine <endereço>             uma máquina e os modelos/sites que a rede pôs nela
    python -m sigma.cli models | catalog               modelos da rede | modelos abertos para apadrinhar
    python -m sigma.cli model-register --catalog qwen2.5-0.5b --budget 100
    python -m sigma.cli model-register meu-modelo --url https://huggingface.co/<repo>/resolve/main/<arquivo>.gguf --budget 100
    python -m sigma.cli model-register meu-modelo --url <URL> --sha256 <hash> --size <bytes> --budget 100
    python -m sigma.cli model-fund <nome> <SIG> | model-update <nome> --royalty 5 | model-retire <nome>
    python -m sigma.cli deploy-site <pasta> <nome> --budget 50 | site-fund <nome> <SIG>
    python -m sigma.cli ask "pergunta" [--model nome]
    python -m sigma.cli compute primes n=1000000 --replicas 3
    python -m sigma.cli status | jobs | sites

Rede: a pública por padrão (outra com --node URL ou SIGMA_NODE). Carteira: --wallet ARQUIVO ou SIGMA_WALLET.
"""
import argparse
import json
import os
import sys
import time

from . import netsafe
from .client import DEFAULT_NODE, JOB_END, PT, Client, SigmaError, TxDropped, fmt, machine_models
from .crypto import Wallet, to_eth
from .sigmacode import visible

SPEND = {"send", "deploy-site", "site-fund", "ask", "compute", "model-register", "model-fund",
         "model-update", "model-retire", "propose", "propor", "vote", "votar"}
NO_LICENSE = "esse modelo não declara a licença no Hugging Face: escolha um que declare"
NO_LICENSE_GIVEN = ("informe a licença do modelo com --license (ex.: --license apache-2.0): sem ela não dá para "
                    "saber se as máquinas da rede podem cobrar em SIG para servir o modelo")
NO_MEDIA_YET = "a rede ainda não aceita modelos de imagem e vídeo: eles entram na atualização das regras v2"
TASK_NAMES = {"text": "texto", "image": "imagem", "video": "vídeo"}


def dump(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def say(text):
    """Imprime texto que veio da rede (nome de máquina, descrição de modelo, resposta da IA) sem deixar passar
    caracteres de controle: um ESC escondido mexeria no terminal (título, área de transferência, cores)."""
    print(visible(text))


def short(addr):
    return f"{addr[:10]}…{addr[-4:]}" if addr else "?"


def num(n, casas=0):
    return f"{n:,.{casas}f}".translate(PT)


def plural(n, um, varios):
    """'1 minerador', '2 mineradores', '0 mineradores' (com o número no jeito brasileiro)."""
    n = n or 0
    return f"{num(n)} {um if n == 1 else varios}"


def fmt_rate(h):
    """Contas por segundo no formato do painel: H/s abaixo de 1.000, depois kH/s ou MH/s com uma casa."""
    h = float(h or 0)
    if round(h) < 1000:
        return f"{num(h)} H/s"
    return f"{num(h / 1e3, 1)} kH/s" if h < 999_950 else f"{num(h / 1e6, 1)} MH/s"


def pct(p, casas=2):
    return f"{p:.{casas}%}".translate(PT)


def fmt_size(n):
    return f"{n / 1e9:.1f} GB".translate(PT) if n >= 1e9 else f"{num(n / 1e6)} MB"


def fmt_left(ts, now=None):
    """Tempo até `ts` (horário absoluto ou segundos que faltam); `now` do nó evita erro do relógio local."""
    ts = ts or 0
    s = max(0, int(ts - (now or time.time()) if ts > 1e9 else ts))
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}min"
    return f"{s // 60}min{s % 60:02d}s" if s >= 60 else f"{s}s"


def load_wallet(path):
    if not os.path.exists(path):
        raise SigmaError(f"a carteira '{path}' não existe; crie com: python -m sigma.cli wallet")
    try:
        return Wallet.load(path)
    except (ValueError, KeyError):
        raise SigmaError(f"arquivo de carteira inválido: {path}") from None


def load_catalog():
    try:
        from . import catalog
    except ImportError:
        raise SigmaError("o catálogo de modelos não está disponível nesta instalação") from None
    return catalog


# ------------------------------------------------------------ exibição amigável
def show_mining(m):
    cap, minted = m.get("cap") or 0, m.get("minted") or 0
    print(f"impresso até agora: {fmt(minted)} de {fmt(cap)}" + (f" ({pct(minted / cap, 4)})" if cap else "")
          + " · só a mineração cria SIG")
    now = m.get("now")
    print(f"emissão contínua: sai um pouco em todo bloco, {fmt(m.get('mint_per_hour') or 0)} por hora "
          f"({fmt(m.get('mint_per_period') or m.get('next_mint') or 0)} a cada 12 h), divididos entre "
          f"{plural(m.get('miners_epoch'), 'minerador', 'mineradores')} pelo trabalho da última janela fechada")
    print(f"poder da rede: ≈ {fmt_rate(m.get('hashrate'))} (contas por segundo) · pontos na base: "
          f"{num(m.get('points_total') or 0)}")
    print(f"janela {m.get('window')} termina em {fmt_left(m.get('window_ends'), now)} · provas de no mínimo "
          f"{m.get('min_bits')} bits, até {m.get('max_shares')} por relatório")


def show_miner(m):
    print(f"minerador {m.get('address')}")
    if not m.get("points") and m.get("last_window") is None:
        print("nenhum trabalho registrado ainda; para minerar, abra o app: python -m sigma.app")
    print(f"pontos na base (última janela fechada): {num(m.get('points') or 0)} ({pct(m.get('share') or 0)} da rede)"
          + (f" · {num(m['pending_points'])} em janelas que ainda fecham" if m.get("pending_points") else ""))
    print(f"recebendo agora: ≈ {fmt(m.get('est_per_hour') or m.get('est_reward') or 0)} por hora")
    print(f"total já recebido da mineração: {fmt(m.get('rewards_total') or 0)}")
    if m.get("last_window") is not None:
        print(f"último relatório: janela {m['last_window']} · ≈ {fmt_rate(m.get('last_rate'))}")


def show_epochs(eps):
    if not eps:
        print("nenhum período de 12 h fechado ainda (a emissão sai em todo bloco; aqui fica o resumo de cada 12 h)")
    for e in eps:
        top = e.get("top") or []
        best = f" · maior: {short(top[0][0])} com {fmt(top[0][1])}" if top else ""
        print(f"época {e.get('epoch')}: {fmt(e.get('mint') or 0)} para "
              f"{plural(e.get('miners'), 'minerador', 'mineradores')}{best}")


def show_governance(g):
    props = g.get("proposals") or []
    if not props:
        print("nenhuma votação ainda. Qualquer carteira pode propor: python -m sigma.cli propose \"título\"")
    for p in props:
        what = (f"{p.get('param')}: {num(p.get('old'))} → {num(p.get('value'))}" if p.get("kind") == "param"
                else "texto")
        state = {"open": "aberta", "approved": "aprovada", "rejected": "recusada"}.get(p.get("status"), p.get("status"))
        say(f"[{state}] {p.get('title')} ({what}) · sim {fmt(p.get('yes') or 0)} · não {fmt(p.get('no') or 0)} · "
            f"{plural(p.get('voters') or 0, 'carteira', 'carteiras')} · id {short(p.get('id'))}")
    gov = g.get("governable") or {}
    if gov:
        print("pode ser votado: " + ", ".join(f"{k} (agora {num(v['now'])}, de {num(v['min'])} a {num(v['max'])})"
                                              for k, v in sorted(gov.items())))
    print(f"cada votação dura 10 dias · 1 SIG = 1 voto (fica travado até o fim) · depósito para propor "
          f"{fmt(g.get('deposit') or 0)} (volta no fim) · quórum {num((g.get('quorum_bps') or 0) / 100, 2)}% do SIG "
          "que existe")


def show_machines(ms):
    for addr, m in sorted(ms.items(), key=lambda kv: (not kv[1].get("online"), kv[1].get("name") or "", kv[0])):
        hw = m.get("hw") or {}
        bits = [f"{hw.get('cpu_cores', '?')} núcleos", f"{(hw.get('ram_mb') or 0) / 1000:.0f} GB RAM"]
        if hw.get("gpu"):
            bits.append(str(hw["gpu"]))
        models = machine_models(m)   # texto e, à parte no /machines, imagem e vídeo
        if models:
            bits.append("modelos: " + ", ".join(models))
        bits.append(f"{plural(m.get('jobs_done'), 'pedido atendido', 'pedidos atendidos')} · "
                    f"ganhou {fmt(m.get('earned') or 0)}")
        say(f"{'●' if m.get('online') else '○'} {m.get('name') or '(sem nome)'} {short(addr)} · " + " · ".join(bits))
    print(f"{sum(bool(m.get('online')) for m in ms.values())} de {plural(len(ms), 'máquina', 'máquinas')} online")


def show_models(ms):
    if not ms:
        print("nenhum modelo na rede ainda; apadrinhe um do catálogo: python -m sigma.cli catalog")
    for name, m in sorted(ms.items()):
        ready, loading = len(m.get("pinners") or {}), len(m.get("assigned") or {})
        say(f"{name} · {fmt_size(m.get('size') or 0)} · {ready}/{m.get('replicas', '?')} máquinas prontas"
            + (f" (+{loading} baixando)" if loading else "")
            + f" · budget {fmt(m.get('budget') or 0)} · royalty {num((m.get('royalty_bps') or 0) / 100, 2).rstrip('0').rstrip(',')}%"
            + f" · padrinho {short(m.get('sponsor'))}")
        extra = " · ".join(str(x) for x in (m.get("license"), m.get("description")) if x)
        if extra:
            say(f"    {extra}")


def show_catalog(items):
    for x in items:
        size = x.get("total_size") or x.get("size") or 0   # imagem e vídeo: com o VAE e o codificador de texto
        print(f"{x['id']:<24} {TASK_NAMES.get(x.get('task'), 'texto'):<6} {fmt_size(size):>8}  "
              f"{x.get('license', ''):<11} {x.get('description', '')}")
    print("\napadrinhe com: python -m sigma.cli model-register --catalog <id> --budget <SIG>")


# ---------------------------------------------------------------- ajudantes
def model_source(a):
    """Descobre url, sha256 e tamanho do GGUF: do catálogo, do Hugging Face ou do que veio na linha de comando.
    Os de imagem e vídeo (só do catálogo) levam também a classe (`task`) e os arquivos extras (`files`: VAE e
    codificador de texto); `size` é só o do arquivo principal, como a chain pede."""
    info = {"name": a.name, "url": a.url, "sha256": a.sha256, "size": a.size,
            "license": a.license, "description": a.description, "task": "text", "files": None}
    if a.catalog:
        items = load_catalog().CATALOG
        item = next((x for x in items if x["id"] == a.catalog), None)
        if not item:
            raise SigmaError(f"'{a.catalog}' não está no catálogo; opções: " + ", ".join(x["id"] for x in items))
        info.update(name=a.name or item["id"], url=item["url"], sha256=item["sha256"], size=item["size"],
                    license=a.license or item.get("license", ""),
                    description=a.description or item.get("description", ""),
                    task=item.get("task", "text"), files=item.get("files"), total_size=item.get("total_size"))
    elif not (a.sha256 and a.size):
        try:  # só a URL: pergunta ao Hugging Face o tamanho e o sha256 do arquivo
            cat = load_catalog()
            found = cat.resolve_hf_url(a.url)
        except Exception as e:
            raise SigmaError(f"não consegui descobrir tamanho e sha256 de {a.url} ({e}); "
                             "informe --sha256 e --size") from None
        found = found if isinstance(found, dict) else {}
        suggest = getattr(cat, "suggest_name", None)  # nome válido a partir do arquivo, se ninguém deu um
        info.update(name=a.name or (suggest(found["file"]) if suggest and found.get("file") else None),
                    url=found.get("url") or a.url, sha256=a.sha256 or found.get("sha256"),
                    size=a.size or found.get("size"), license=a.license or found.get("license") or "")
    if not info["name"]:
        raise SigmaError("informe o nome do modelo na rede (ex.: meu-modelo)")
    if not (info["sha256"] and info["size"]):
        raise SigmaError("não sei o sha256/tamanho do arquivo: informe --sha256 e --size")
    problem = license_problem(info["license"])   # sempre, mesmo quando ninguém informou a licença
    if problem == NO_LICENSE and not a.catalog and a.sha256 and a.size and not a.license:
        problem = NO_LICENSE_GIVEN               # arquivo de fora do catálogo e do Hugging Face: diga qual é
    if problem:
        raise SigmaError(problem)
    return info


def license_problem(license):
    """Por que essa licença não serve para a rede (texto para o usuário), ou None se serve.

    Licença vazia, 'other' ou 'unknown' não vale: quem apadrinha responde pela licença do modelo que traz, e
    as máquinas da rede cobram em SIG para servir o modelo. O resto segue a regra do catálogo."""
    lic = str(license or "").strip()
    if lic.lower() in ("", "other", "unknown"):
        return NO_LICENSE
    try:
        cat = load_catalog()
    except SigmaError:
        return None              # sem o catálogo instalado, a decisão fica com quem registra
    explain = getattr(cat, "license_error", None)   # o catálogo sabe explicar o motivo
    if callable(explain):
        return explain(lic) or None
    if not cat.commercial_ok(lic):
        return (f"a licença '{lic}' não permite uso comercial, e as máquinas da rede cobram em SIG para servir "
                "o modelo")
    return None


def commercial_ok(license):
    """Mesma regra do catálogo (licença não declarada também não serve)."""
    return license_problem(license) is None


def parse_params(items):
    """['n=1000', 'seed=abc'] -> {'n': 1000, 'seed': 'abc'}"""
    params = {}
    for item in items:
        k, sep, v = item.partition("=")
        if not sep or not k:
            raise SigmaError(f"parâmetro '{item}' deve ser chave=valor (ex.: n=1000000)")
        params[k] = int(v) if v.lstrip("-").isdigit() else v
    return params


def build_parser():
    ap = argparse.ArgumentParser(prog="sigma.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node", default=os.environ.get("SIGMA_NODE", DEFAULT_NODE), help="URL do nó (padrão: rede pública)")
    ap.add_argument("--wallet", default=os.environ.get("SIGMA_WALLET", "wallet.key"), help="arquivo da carteira")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="comando")

    def cmd(name, help_, raw=False, **kw):
        p = sub.add_parser(name, help=help_, description=help_, **kw)
        if raw:
            p.add_argument("--json", action="store_true", help="mostra a resposta crua em JSON")
        return p

    cmd("wallet", "cria/mostra a carteira")
    cmd("balance", "saldo de uma carteira").add_argument("address", nargs="?")
    p = cmd("send", "envia SIG"); p.add_argument("to"); p.add_argument("amount", help="quantidade em SIG")
    cmd("mining", "emissão de SIG e próxima distribuição", True)
    cmd("miner", "seus pontos na época e a recompensa estimada", True).add_argument("address", nargs="?")
    cmd("epochs", "distribuições passadas", True)
    cmd("machines", "computadores que hospedam a rede", True, aliases=["providers"])
    cmd("machine", "uma máquina e o que a rede colocou nela").add_argument("address")
    cmd("models", "modelos de IA da rede", True)
    cmd("catalog", "modelos abertos que você pode apadrinhar", True)
    p = cmd("model-register", "vira padrinho de um modelo (a rede escolhe sozinha quem hospeda)")
    p.add_argument("name", nargs="?", help="nome na rede (com --catalog, o padrão é o id do catálogo)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--catalog", metavar="ID", help="modelo do catálogo (veja: python -m sigma.cli catalog)")
    src.add_argument("--url", help="URL do arquivo .gguf (do Hugging Face, tamanho e sha256 vêm sozinhos)")
    p.add_argument("--sha256", help="sha256 do arquivo .gguf")
    p.add_argument("--size", type=int, help="tamanho do arquivo em bytes")
    p.add_argument("--budget", required=True, help="SIG para pagar o aluguel de quem hospeda")
    p.add_argument("--royalty", type=float, default=10, help="%% de cada token gasto que vai para você")
    p.add_argument("--reinvest", type=float, default=0, help="%% do royalty que volta para o budget")
    p.add_argument("--replicas", type=int, default=2, help="em quantas máquinas o modelo fica (1-5)")
    p.add_argument("--license", default="")
    p.add_argument("--description", default="")
    p = cmd("model-fund", "recarrega o budget de um modelo seu"); p.add_argument("name")
    p.add_argument("amount", help="quantidade em SIG")
    p = cmd("model-update", "muda royalty/reinvestimento ou passa o modelo adiante"); p.add_argument("name")
    p.add_argument("--royalty", type=float); p.add_argument("--reinvest", type=float); p.add_argument("--new-sponsor")
    cmd("model-retire", "apaga o modelo da rede e devolve o budget restante").add_argument("name")
    p = cmd("deploy-site", "publica uma pasta como site"); p.add_argument("directory"); p.add_argument("name")
    p.add_argument("--budget", default="50", help="SIG para pagar a hospedagem")
    p.add_argument("--replicas", type=int, default=2)
    p = cmd("site-fund", "recarrega o budget de um site seu"); p.add_argument("name")
    p.add_argument("amount", help="quantidade em SIG")
    p = cmd("ask", "pergunta a um modelo da rede (paga em SIG; vai cifrada para uma só máquina, que lê a "
                   "pergunta para responder)"); p.add_argument("prompt")
    p.add_argument("--model", help="nome do modelo (padrão: um que esteja no ar agora)")
    p.add_argument("--max-tokens", type=int, default=1024)
    p = cmd("compute", "computação conferida por várias máquinas"); p.add_argument("task")
    p.add_argument("params", nargs="*", help="chave=valor"); p.add_argument("--replicas", type=int, default=2)
    cmd("governance", "votações da rede (o que está aberto e o que pode ser votado)", True, aliases=["votacoes"])
    p = cmd("propose", "abre uma votação de 10 dias (deixa um depósito que volta no fim)", aliases=["propor"])
    p.add_argument("title", help="título da votação")
    p.add_argument("--text", default="", help="explicação")
    p.add_argument("--param", help="regra que muda se for aprovada (sem isso, é uma votação de texto)")
    p.add_argument("--value", type=int, help="valor novo da regra (inteiro)")
    p = cmd("vote", "vota: 1 SIG = 1 voto, travado até o fim da votação", aliases=["votar"])
    p.add_argument("proposal_id"); p.add_argument("choice", help="sim ou nao"); p.add_argument("amount", help="SIG")
    cmd("status", "estado do nó e da rede")
    cmd("jobs", "jobs recentes")
    cmd("sites", "sites publicados")
    return ap


def run(a, c):
    def view(data, show):
        if a.json:
            dump(data)
        else:
            show(data)

    if a.cmd == "wallet":
        new = not os.path.exists(a.wallet)
        w = Wallet.load_or_create(a.wallet)
        print(("carteira nova criada\n" if new else "") + f"endereço: {w.address}\nMetaMask: {to_eth(w.address)}\n"
              f"arquivo:  {os.path.abspath(a.wallet)} (quem tiver este arquivo gasta seus SIG: guarde bem)")
        print(f"saldo:    {fmt(c.balance(w.address))}")
    elif a.cmd == "balance":
        print(fmt(c.balance(a.address or load_wallet(a.wallet).address)))
    elif a.cmd == "send":
        c.transfer(a.to, a.amount)
        print(f"enviado · saldo {fmt(c.balance())}")
    elif a.cmd == "mining":
        view(c.mining(), show_mining)
    elif a.cmd == "miner":
        view(c.miner(a.address or load_wallet(a.wallet).address), show_miner)
    elif a.cmd == "epochs":
        view(c.epochs(), show_epochs)
    elif a.cmd in ("machines", "providers"):
        view(c.machines(), show_machines)
    elif a.cmd == "machine":
        dump(c.machine(a.address))
    elif a.cmd == "models":
        view(c.models(), show_models)
    elif a.cmd == "catalog":
        view(load_catalog().CATALOG, show_catalog)
    elif a.cmd == "model-register":
        info = model_source(a)
        if info["task"] != "text":   # na v1 a chain recusaria task/files com uma mensagem que ninguém entende
            mining = c.mining()
            if not (isinstance(mining, dict) and mining.get("media")):
                raise SigmaError(NO_MEDIA_YET)
        c.register_model(info["name"], info["url"], info["sha256"], info["size"], a.budget, a.royalty,
                         a.reinvest, a.replicas, info["description"], info["license"], task=info["task"],
                         files=info["files"])
        total = int(info.get("total_size") or info["size"])
        print(f"você é o padrinho de '{info['name']}' ({fmt_size(total)}). A rede escolhe sozinha as "
              f"máquinas que vão baixar e servir o modelo. Acompanhe: python -m sigma.cli models")
    elif a.cmd == "model-fund":
        c.fund_model(a.name, a.amount)
        print(f"budget de '{a.name}': {fmt(c.model(a.name)['budget'])}")
    elif a.cmd == "model-update":
        c.update_model(a.name, a.royalty, a.reinvest, a.new_sponsor)
        print("modelo atualizado")
    elif a.cmd == "model-retire":
        c.retire_model(a.name)
        print(f"'{a.name}' apagado da rede; o budget restante voltou para você · saldo {fmt(c.balance())}")
    elif a.cmd == "deploy-site":
        m = c.deploy_site(a.directory, a.name, a.budget, a.replicas)
        print(f"site '{a.name}' publicado ({plural(len(m), 'arquivo', 'arquivos')}). Acesse: {c.node}/site/{a.name}/")
    elif a.cmd == "site-fund":
        c.fund_site(a.name, a.amount)
        print(f"budget de '{a.name}': {fmt(c.sites()[a.name]['budget'])}")
    elif a.cmd == "ask":
        model = a.model or c.pick_model()
        jobs = []
        try:
            text, job = c.ask(model, [{"role": "user", "content": a.prompt}], a.max_tokens, on_job=jobs.append)
        except KeyboardInterrupt:     # Ctrl+C: se nenhuma máquina pegou o pedido ainda, o valor volta na hora
            if jobs:
                cancel_on_interrupt(c, jobs[-1])
            raise SystemExit(130) from None
        say(text)                     # escrito pela máquina de outra pessoa: nada de sequência de terminal
        print(f"\n— modelo {model} · custo {fmt(job.get('cost') or 0)} · máquina {short(job.get('machine'))}")
    elif a.cmd in ("governance", "votacoes"):
        view(c.governance(), show_governance)
    elif a.cmd in ("propose", "propor"):
        if a.param and a.value is None:
            raise SigmaError("com --param, informe também --value")
        pid = c.propose(a.title, a.text, a.param, a.value)
        print(f"votação aberta por 10 dias: {pid}\nvote com: python -m sigma.cli vote {pid} sim <SIG>")
    elif a.cmd in ("vote", "votar"):
        c.vote(a.proposal_id, a.choice, a.amount)
        print(f"voto registrado: {a.amount} SIG travados até o fim da votação · saldo {fmt(c.balance())}")
    elif a.cmd == "compute":
        result, job = c.compute({"name": a.task, "params": parse_params(a.params)}, a.replicas)
        print(f"status: {job['status']} · resultado: {result} · custo {fmt(job.get('cost') or 0)}")
    else:  # status, jobs, sites
        dump(c.get("/" + a.cmd))


def cancel_on_interrupt(c, job_id):
    """Ctrl+C no meio de uma pergunta: cancela o pedido se nenhuma máquina pegou (o valor volta na hora).
    O Ctrl+C pode ter chegado antes de o pedido entrar num bloco; espera a inclusão antes de decidir."""
    try:
        try:
            c.wait_tx(job_id)         # o pedido pode ainda não ter entrado num bloco: espera para poder cancelar
        except TxDropped:
            return                    # o pedido não entrou em nenhum bloco: nada foi reservado
        job = c.job(job_id)
        if job.get("status") in JOB_END:
            return                    # já terminou: o valor voltou (ou a resposta foi paga)
        if job.get("claims"):
            print("\nUm computador já está respondendo. Se ele não entregar, o valor reservado volta sozinho para a "
                  "sua carteira.", file=sys.stderr)
            return
        c.cancel_job(job_id)
        print("\nPedido cancelado. O valor reservado voltou para a sua carteira.", file=sys.stderr)
    except SigmaError as e:
        print(f"\nnão deu para cancelar ({e}). Se nenhuma máquina responder, o valor reservado volta sozinho para "
              "a sua carteira.", file=sys.stderr)
    except KeyboardInterrupt:
        pass


def friendly(e):
    """Mensagem de erro para quem está no terminal."""
    if netsafe.cert_problem(e) and sys.platform == "darwin":
        return ("O Python deste Mac não tem os certificados de segurança. Abra Aplicativos › Python 3.x › "
                "Install Certificates.command e rode o comando de novo.")
    return str(e)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    netsafe.install_default_opener()   # HTTPS com os certificados do sistema (Python do python.org no macOS)
    a = build_parser().parse_args(argv)
    a.json = getattr(a, "json", False)
    c = Client(a.node)
    try:
        if a.cmd in SPEND:
            c.wallet = load_wallet(a.wallet)
        run(a, c)
    except SigmaError as e:
        print(f"erro: {visible(friendly(e))}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
