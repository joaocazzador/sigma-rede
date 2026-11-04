# Sigma v2 — especificação (fonte da verdade para a implementação)

Objetivo do dono do projeto, nas palavras dele:
> "Um programa simples que fosse somente executar e começar a minerar SIG. Não dê SIG de boas-vindas,
> não deve ter SIG sem mineração. Os primeiros 100.000.000 de SIG serão impressos assim: 0,5% do que
> falta ser impresso será impresso a cada 12h e dividido entre o poder computacional disponibilizado na
> rede; o resto só circula entre quem utiliza e quem hospeda. Eu não devo ter que baixar várias coisas e
> o modelo junto: a própria tecnologia gerencia os modelos e em que computador fica qual modelo. Só baixar
> e deixar meu computador hospedando a rede, direcionando para uma carteira. Uma interface executável
> onde posso gerenciar TODAS as coisas, disponível no site."
>
> A luta: "Vão substituir você por um robô. Queremos que os donos dos robôs não sejam as Big Techs, mas as pessoas."

Regras gerais de código (valem para todos os arquivos):
- Python 3.11+, **somente biblioteca padrão** (nada de pip). Código e mensagens em português, no estilo dos
  arquivos existentes (comentários curtos explicando o porquê, nomes claros, sem frameworks).
- Valores monetários no ledger sempre em **µSIG inteiros** (`UNIT = 1_000_000`). Nas APIs do app e da UI,
  entradas em SIG decimal; saídas em µSIG inteiros.
- Endereços: `norm_address()` de `sigma/crypto.py` (aceita `0x…`/`sig…`, devolve `sig` + 40 hex minúsculos).
- O que já existe e continua: `crypto.py`, `evm.py`, `rpc.py` (MetaMask), `forum.py`, `tasks.py`,
  `sigmacode.py`, páginas web, histórico, gateway de sites, blobs.
- **Removidos**: faucet / SIG de boas-vindas / `alloc` no genesis, stake, taxa de 1% e queima,
  `sigma/provider.py` e `sigma/backends.py` (substituídos por `host.py` e `engines.py`).

---------------------------------------------------------------------------------------------------

## 1. Genesis e parâmetros

```json
{"network": "sigma", "chain_id": 51515, "time": 1793700000.0, "validators": ["<pub hex>"], "alloc": {},
 "params": {"epoch_seconds": 43200, "window_seconds": 300, "min_share_bits": 12, "max_shares": 32,
            "supply_cap": 100000000000000, "mint_bps": 50}}
```
- `alloc` é sempre `{}` (nenhum SIG fora da mineração). `State` deve **recusar** genesis com alloc não vazio.
- `State.params` = defaults acima sobrescritos por `genesis["params"]`. Redes locais/demos usam parâmetros
  rápidos (ex.: `epoch_seconds=30, window_seconds=5, min_share_bits=4`).
- `sigma.node init --dir D [--epoch-seconds N --window-seconds N --min-bits N]` gera genesis + `producer.key`
  (não existe mais `faucet.key`).

## 2. Prova de trabalho (novo `sigma/pow.py`, usado por chain e minerador)

```python
POW_SALT = b"sigma-pow-v1"
def pow_hash(seed: str, miner: str, nonce: int) -> int:
    """scrypt(N=1024, r=1, p=1): pesado em memória, barato de verificar (~0,5 ms)."""
    data = f"{seed}:{miner}:{nonce}".encode()
    return int.from_bytes(hashlib.scrypt(data, salt=POW_SALT, n=1024, r=1, p=1, dklen=32), "big")

@lru_cache(maxsize=262144)          # mempool + bloco verificam o mesmo share: só calcula uma vez
def share_ok(seed: str, miner: str, nonce: int, bits: int) -> bool:
    return pow_hash(seed, miner, nonce) < (1 << (256 - bits))

def report_points(n_shares: int, bits: int) -> int:
    return n_shares << bits          # valor esperado = número de hashes feitos
```
`miner` é o endereço normalizado `sig…`. O endereço faz parte do hash: um share achado para A não serve para B,
por isso o relatório de trabalho **não precisa de assinatura**.

## 3. Ledger (`sigma/chain.py`)

### 3.1 Ciclo de bloco (nova API do `State`)
```python
state.begin_block(height: int, time: float, prev_hash: str)   # avança janela/época, aluguel, atribuições
state.apply_tx(tx)                                            # usa self.height e self.now
state.end_block()                                             # timeouts de jobs, limpeza
```
`begin_block` define `self.height` e `self.now` antes de tudo. O nó chama os três, nessa ordem, para todo
bloco (produção e validação). O estado "pending" do mempool aplica txs sem begin_block (usa height/now do
último bloco).

Ordem dentro de `begin_block`:
1. **Janela** (`window_seconds`): se `time >= window_start + W`: `k = int((time - window_start) // W)`;
   `window += k`; `window_start += k*W`; `seeds = {str(window): prev_hash}` e, se `k == 1`, mantém também a
   seed da janela anterior (`seeds[str(window-1)]`). Poda `last_reports` (fica só janelas >= window-1).
   Em seguida `_on_window_change()`:
   a. máquinas com `last_seen < window - 2` ficam `online = False`; elas saem de `pinners`/`assigned` de
      todos os modelos e de `pinners` de todos os sites (e o nome sai de `machine["models"]`);
   b. **aluguel** (uma vez por mudança de janela): para cada site ativo e cada modelo, cada pinner online
      recebe `max(1, ceil(size * price / 1_000_000))` µSIG do budget (`price` = `hosting_per_mb_window`
      gravado no pin). Se o budget não cobre a soma: site → `status = "expired"`; modelo → `_delete_model`;
   c. atribuições pendentes (`model["assigned"]`) com mais de 6 janelas viram expiradas: saem, e a máquina
      perde 1 de reputação;
   d. `_assign_models()` (seção 3.5).
2. **Época** (`epoch_seconds`): se `time >= epoch_start + E`: `_settle_epoch()`;
   `k = int((time - epoch_start) // E)`; `epoch += k`; `epoch_start += k*E` (épocas sem bloco não emitem).

### 3.2 Emissão
```
remaining = supply_cap - minted
mint      = remaining * mint_bps // 10000          # 0,5% do que falta
total     = soma de points (pontos da época)
para cada (addr, pts) em sorted(points.items()):  r = mint * pts // total;  credita r
minted   += soma distribuída (o resto de arredondamento continua "por imprimir")
```
- Se `total == 0`, nada é emitido (a época passa vazia).
- Registra em `self.epochs` (máx. 60 últimas) `{"epoch", "start", "end", "mint", "total_points", "miners",
  "top": [[addr, reward], …até 10]}`; atualiza `miners[addr]["rewards_total"]`; zera `points`.
- Guarda as recompensas da liquidação num atributo **transitório** `self.last_settlement =
  {"epoch", "time", "rewards": {addr: µSIG}}` (fora de `to_dict()`, não entra no state root) para o nó
  indexar no histórico como entradas `type: "mint"`.
- Não existe outra forma de criar SIG. Não há taxa, queima nem stake.

### 3.3 Relatório de trabalho (tx sem assinatura)
```json
{"type": "work", "data": {"miner": "sig…", "window": 123, "bits": 14, "nonces": [5, 77, 912]}}
```
Validação, nesta ordem (barato primeiro): miner normalizável; `str(window)` em `seeds` (atual ou anterior);
janela ainda não reportada por esse miner (`last_reports[miner]`); `min_share_bits <= bits <= 200`;
`nonces` lista de ints únicos, `0 <= n < 2**62`, `1 <= len <= max_shares`; cada `share_ok(seed, miner, n, bits)`.
Efeito: `points[miner] += report_points(len, bits)`; `last_reports[miner].append(window)`;
`miners[miner] = {"points_total", "rewards_total", "last_window", "last_rate"}` (`last_rate` = pontos do
relatório / `window_seconds`, estimativa de hashes/s). `apply_tx` despacha `type == "work"` antes da checagem
de assinatura (sem `pub`, `sig` nem `nonce`). `tx_id` continua `sha(canon(body))`.

### 3.4 Máquinas (substituem "providers")
Todas assinadas pela **chave da máquina** (gerada pelo app, o usuário nunca vê). Pagamentos vão para `payout`.
- `register_machine` `{payout, name, hw: {cpu_cores, ram_mb, ai_ram_mb, vram_mb, disk_mb, gpu}, ai: bool,
  compute: bool, endpoint: str (pode ser ""), prices: {ai_per_1k_tokens, compute_per_unit, hosting_per_mb_window}}`
  — cria/atualiza; `last_seen = window`, `online = True`. Defaults de preço: 2000 / 500 / 5.
- `ping` `{}` — `last_seen = window`, `online = True`; recusa se `last_seen == window` ("já sinalizou nesta janela").
- Registro em `state.machines[addr]`:
  `{"payout", "name", "hw", "ai", "compute", "endpoint", "prices", "since", "last_seen", "online",
    "models": [nomes hospedados], "jobs_done": 0, "earned": 0, "reputation": 0}`.
- `pin_site {name, version}` — máquina online, cabe em `hw.disk_mb` (somando sites e modelos já hospedados).
- `pin_model {name}` — só se a máquina estiver em `model["assigned"]`; move para `model["pinners"]`
  (`{"window", "price"}`) e adiciona o nome em `machine["models"]`.
- Jobs: `claim_job` exige máquina online (AI: modelo em `machine["models"]`; compute: `machine["compute"]`);
  `claims[machine] = {"time": now, "price"}`; pagamentos creditam o `payout` (100% do preço; menos o royalty
  do padrinho nos jobs de IA). Divergência em computação: a máquina da minoria perde 5 de reputação e o
  `payout` dela **perde os pontos da época atual** (`points[payout] = 0`).
- Timeouts por tempo (`end_block`): claim sem resultado há > 600 s sai (reputação -1, job reabre);
  job aberto sem resultados há > 3600 s expira e devolve o escrow; jobs encerrados há > 2 dias saem do estado.

### 3.5 Modelos patrocinados e atribuição automática
- `register_model` (assinada pelo padrinho):
  `{name, url, sha256, size, license, description, royalty_bps (≤5000), reinvest_bps, replicas (1-5, padrão 2), budget}`.
  `url` começa com `https://` ou `http://`; `sha256` 64 hex; `0 < size <= 60e9`;
  `ram_mb = ceil(size / 1e6 * 1.2) + 512` (gravado no registro).
- Registro: `{"name","sponsor","url","sha256","size","ram_mb","license","description","royalty_bps",
  "reinvest_bps","replicas","budget","rent_paid","royalties","tokens","jobs","assigned": {machine: window},
  "pinners": {machine: {"window","price"}},"created"}`.
- `fund_model`, `update_model` (royalty/reinvest/new_sponsor), `retire_model`, royalty em cada job,
  `_delete_model` (devolve budget, cancela jobs, tira de `machine["models"]`, entra em `retired_models`) — como na v1.
- `_assign_models()` a cada janela: para cada modelo (ordem por nome) com
  `need = replicas - len(pinners) - len(assigned) > 0`: candidatas = máquinas online, `ai` verdadeiro,
  reputação > -10, que ainda não têm o modelo, com `hw.ai_ram_mb >= ram já comprometida + model.ram_mb` e
  disco livre suficiente; ordena por `sha256(seed_da_janela + nome + endereço)`; atribui as `need` primeiras
  (`assigned[addr] = window`). **Ninguém escolhe modelo à mão: a rede decide.**

### 3.6 Sites
Iguais à v1 (manifest de blobs, budget, réplicas, `pin_site` automático pelas máquinas), com aluguel por
janela (3.1.b) e `pinners[machine] = {"window", "price"}`.

### 3.7 Estado completo
`to_dict()` = `balances, nonces, machines, jobs, sites, models, retired_models, mining, miners, points,
epochs`, onde `mining = {"window", "window_start", "seeds", "epoch", "epoch_start", "minted", "last_reports"}`.
`params` e `chain_id` ficam fora (constantes do genesis). `tx_summary()` deve entender `work`, `ping`,
`register_machine` e as demais.

---------------------------------------------------------------------------------------------------

## 4. Nó (`sigma/node.py`)

- Produção/validação chamam `begin_block(h, time, prev_hash)` → txs → `end_block()`. Blocos válidos têm
  `time > tempo do anterior` e `time <= agora + 30 s` (seguidores). Snapshots continuam.
- Removidos: `--faucet`, `/faucet`, `faucet_send`, `welcomed`, `WELCOME_*`.
- Histórico: não indexa `work` nem `ping`; indexa as recompensas de época (`last_settlement`) como
  `{"type": "mint", "from": None, "to": addr, "amount": µSIG, "height", "time", "epoch"}`.
- Novas rotas GET:
  - `/mining` → `{"window", "seed", "prev_window", "prev_seed", "window_seconds", "window_ends",
    "min_bits", "max_shares", "epoch", "epoch_seconds", "epoch_ends", "minted", "cap", "next_mint",
    "points_total", "miners_epoch", "hashrate"}` (`hashrate` = soma de `last_rate` dos mineradores vistos nas
    duas últimas janelas).
  - `/mining/epochs` → lista `epochs` (mais recente primeiro).
  - `/mining/miner/<addr>` → `{"address", "points", "share", "est_reward", "rewards_total", "last_window",
    "last_rate", "points_total"}` (`share` = points/points_total; `est_reward` = next_mint × share).
  - `/machines` (e alias `/providers`) → dict de máquinas.
  - `/machine/<addr>` → registro + `"assignments": [{"name","url","sha256","size","ram_mb","status": "assigned"|"pinned"}]`
    + `"sites": [nomes hospedados]`.
  - `/models`, `/models/<nome>` como na v1.
  - `/downloads/<arquivo>` → serve arquivos de `--downloads DIR` (sem `..`; `Content-Disposition: attachment`;
    envio em blocos), e a página `/baixar` (`sigma/web/baixar.html`) entra em `PAGES`.
- `/status` ganha `"supply": {"minted", "cap", "epoch", "epoch_ends", "next_mint", "miners_epoch", "hashrate"}`
  e `"machines"` (quantas online). Some `faucet`, `welcome`, `burned`.
- `POST /tx` aceita `work` (sem assinatura). Limite simples anti-abuso: no máximo 4 relatórios `work` por IP
  por janela no modo `--public`.

## 5. Cliente (`sigma/client.py`)
Mantém `get/post/status/account/balance/job/send_tx/put_blob/get_blob/deploy_site/ask/compute`. Muda/adiciona:
```python
submit_work(report: dict) -> str                         # POST /tx {"type":"work","data":report}
tx_status(txid) -> dict
mining() -> dict;  miner(addr) -> dict;  epochs() -> list
machines() -> dict;  machine(addr) -> dict
models() -> dict;  model(name) -> dict
deploy_site_files(files: dict[str, bytes], name, budget_sig, replicas=2) -> manifest
register_model(name, url, sha256, size, budget_sig, royalty_pct=10, reinvest_pct=0, replicas=2,
               description="", license="") -> txid
```
`ask()` escolhe entre máquinas online com o modelo em `machine["models"]`. Sem `faucet()`, sem `providers()`.

## 6. Minerador, máquina e motores de IA

### 6.1 `sigma/hardware.py`
`detect(data_dir) -> {"cpu_cores", "ram_mb", "vram_mb", "gpu", "disk_free_mb", "os"}` (Windows: ctypes
`GlobalMemoryStatusEx`; GPU via `nvidia-smi` se existir, senão `""`/0; Linux `/proc/meminfo`;
macOS `sysctl hw.memsize`).

### 6.2 `sigma/miner.py`
```python
class Miner:
    def __init__(self, client, payout: str, workers: int, log=print)
    def start(self); def stop(self); def set_workers(self, n); def pause(self); def resume(self)
    def stats(self) -> dict  # running, paused, workers, hashrate, window, bits, shares_window,
                             # reports_sent, reports_failed, last_report, last_error
```
- `multiprocessing` (contexto `spawn`; funciona congelado com PyInstaller), cada processo com prioridade
  baixa (Windows: `SetPriorityClass(BELOW_NORMAL)` via ctypes; Unix `os.nice(10)`).
- Processo `k` de `n` testa nonces `k, k+n, k+2n…` com `pow_hash` sobre `(seed, payout, nonce)` da janela
  atual; shares que batem o `bits` da janela voltam por fila; contador compartilhado mede hashes/s.
- Thread principal: consulta `/mining` a cada ~3 s; ao virar a janela, envia **um** relatório da janela que
  terminou (até `max_shares` shares) e recomeça com a nova seed. `bits` da janela =
  `clamp(round(log2(hashrate * W / 20)), min_bits, 200)`, para render ~20 shares por janela.
- `pause()` para os processos sem perder a janela (usado enquanto a máquina roda um job de IA na CPU).

### 6.3 `sigma/engines.py`
```python
class EngineManager:
    def __init__(self, data_dir, prefer="auto", log=print, threads=None)   # prefer: auto|ollama|llamacpp|mock
    def available(self) -> bool; def describe(self) -> dict  # {"kind","ready","detail"}
    def load(self, name, gguf_path, sha256)                    # bloqueia até pronto
    def unload(self, name)
    def chat(self, name, messages, max_tokens, json_mode=False) -> (text, tokens_in, tokens_out)
    def shutdown(self)
```
- `auto`: usa o Ollama se responder em `127.0.0.1:11434`; senão o **llama.cpp gerenciado**.
- Ollama: `HEAD/POST /api/blobs/sha256:<sha>` com o arquivo, `POST /api/create {"model": "sigma-<nome>",
  "files": {"model.gguf": "sha256:<sha>"}, "stream": false}`, `/api/chat`, `DELETE /api/delete`.
- llama.cpp: baixa **automaticamente** (sem perguntar nada ao usuário) a versão fixada abaixo, confere o
  SHA-256, extrai em `<data_dir>/engine/b11721/` e roda um `llama-server` por modelo:
  `llama-server -m <gguf> --host 127.0.0.1 --port <livre> -c 4096 --jinja -t <threads>` (sem janela no
  Windows: `CREATE_NO_WINDOW`), espera `/health`, conversa por `/v1/chat/completions` (usa `usage`).
  Se a versão Vulkan falhar ao subir, cai para a de CPU.
  ```
  b11721 (18/10/2026)
  win-cpu-x64     https://github.com/ggml-org/llama.cpp/releases/download/b11721/llama-b11721-bin-win-cpu-x64.zip       19f5a91cfbd906fadfb36975a65a0c165c7db1a533d5db129dfa0a533dfdeaa1
  win-vulkan-x64  https://github.com/ggml-org/llama.cpp/releases/download/b11721/llama-b11721-bin-win-vulkan-x64.zip    b2dab62833cc07aae4a1f28752874668b6a98d304a7f8ca6c1421a0d1e1a7075
  macos-arm64     https://github.com/ggml-org/llama.cpp/releases/download/b11721/llama-b11721-bin-macos-arm64.tar.gz    2ace55512ee1ab4e2703f80c135c2a92d40d3367a5b3a98d21b103ffa7f0eb82
  macos-x64       https://github.com/ggml-org/llama.cpp/releases/download/b11721/llama-b11721-bin-macos-x64.tar.gz      de3e409d44a9130f8e863975c13693f047b1c657180a94485464eb7cd5747579
  linux-x64       https://github.com/ggml-org/llama.cpp/releases/download/b11721/llama-b11721-bin-ubuntu-x64.tar.gz     68c7a4dfb6a53e607b73c00c839bfc81c55141cd0a7697c0252e634190647c2f
  linux-vulkan    https://github.com/ggml-org/llama.cpp/releases/download/b11721/llama-b11721-bin-ubuntu-vulkan-x64.tar.gz 9b794ea6578bae49a1e871b7ec330a754ce1aa788846eb574f0a1227a75996d5
  ```
- `mock`: respostas determinísticas para testes (pode reaproveitar a lógica do antigo `MockBackend`).

### 6.4 `sigma/host.py`
```python
class Host:
    def __init__(self, node_url, machine_wallet, payout, data_dir, settings, miner=None, log=print)
        # settings: ai_enabled, compute_enabled, disk_gb, ai_ram_gb, public_url, engine, ai_threads, name
    def start(self); def stop(self); def update_settings(self, settings)
    def stats(self) -> dict  # registered, machine, online, engine, models[{name,status,progress,size}],
                             # sites, jobs_done, earned, reputation, last_error
```
- Ao iniciar: detecta hardware, `register_machine` (ai = ai_enabled e motor disponível e ai_ram ≥ 1,5 GB),
  e daí em diante um `ping` por janela.
- **Modelos sem intervenção**: lê `/machine/<eu>`; para cada atribuição baixa o GGUF de `url` para
  `<data_dir>/models/<sha256>.gguf` (com retomada via `Range`, progresso, SHA-256 conferido no fim), carrega
  no motor e só então envia `pin_model`. Modelo que saiu da lista (apagado, reatribuído) → descarrega do
  motor e apaga o arquivo. Na reinicialização, recarrega os modelos já hospedados.
- Sites: `pin_site` automático enquanto couber em `disk_gb`, como na v1 (baixa blobs do nó, serve em
  `/blob/<hash>` se houver `public_url`).
- Jobs: IA para modelos hospedados (pausa o minerador durante a inferência), computação se `compute_enabled`.
- Todas as tx da máquina passam por um lock (nonce sequencial).

## 7. App (`sigma/app.py` + `sigma/securestore.py` + `sigma/catalog.py`)

### 7.1 Execução
`python -m sigma.app` (ou `Sigma.exe`). Pasta de dados: Windows `%LOCALAPPDATA%\Sigma`, outros `~/.sigma`.
Argumentos: `--headless`, `--wallet <endereço>` (só minerar para esse endereço), `--node URL`
(padrão `https://sigma.195-35-29-232.sslip.io`), `--data-dir`, `--no-mine`, `--no-ai`, `--cpu PCT`,
`--disk-gb N`, `--name`, `--port` (UI local, padrão 47811), `--background` (inicia sem abrir janela).
- Instância única: se já houver um app respondendo em `127.0.0.1:<porta>/api/ping`, só abre a janela dele e sai.
- Sem `--headless`: abre o painel numa janela de app (Windows: `msedge --app=<url> --user-data-dir=<dados>/ui
  --no-first-run --window-size=1200,820`; senão Chrome `--app`; senão o navegador padrão) e mostra uma
  janelinha tkinter de controle ("Σ SIGMA · minerando · X H/s · saldo" + botões **Abrir painel** e
  **Desligar**). Fechar a janelinha pergunta se quer desligar. Sem tkinter: modo console.
- `multiprocessing.freeze_support()` no início (PyInstaller).
- "Iniciar com o computador" (Windows): `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, valor
  `Sigma` = `"<exe>" --background` (via `winreg`).

### 7.2 Chaves
- `machine.key`: gerada automaticamente, nunca exibida.
- Carteira do usuário: (a) criar nova, (b) importar chave privada (ex.: exportada da MetaMask) ou (c) só o
  endereço (minera e hospeda; para gastar, usa a MetaMask no site). Chave guardada por `securestore`
  (Windows: DPAPI via ctypes `CryptProtectData`; outros: arquivo com permissão 600).

### 7.3 Servidor local do painel (somente `127.0.0.1`)
- Serve `sigma/web/app.html` em `/` (sem segredos dentro). A URL aberta é `http://127.0.0.1:<porta>/#t=<token>`.
- Toda rota `/api/*` exige o cabeçalho `X-Sigma-Token: <token>` e `Host` igual a `127.0.0.1:<porta>` ou
  `localhost:<porta>` (contra CSRF e DNS rebinding). Token aleatório por execução, salvo em
  `<dados>/session.json` para a segunda instância.
- Erros: HTTP 400 `{"error": "mensagem em português"}`.

Rotas (JSON):
```
GET  /api/ping                     -> {"app": "sigma", "version"}                 (sem token)
GET  /api/state                    -> estado completo (abaixo)
POST /api/setup                    {"mode":"create"} -> {"address","private_key"} (mostrada uma vez para backup)
                                   {"mode":"import_key","private_key"} | {"mode":"address","address"} -> {"address"}
POST /api/settings                 {cpu_percent?, mining_enabled?, ai_enabled?, compute_enabled?, disk_gb?,
                                    ai_ram_gb?, node?, start_with_system?, name?} -> {"settings"}
POST /api/send                     {"to","amount"} -> {"txid"}
GET  /api/wallet/export?confirm=1  -> {"private_key"}
GET  /api/models                   -> {"network": {nome: modelo}, "catalog": [item…]}
POST /api/models/sponsor           {"catalog_id"|"url", "name"?, "budget", "royalty", "reinvest", "replicas"} -> {"txid","name"}
POST /api/models/fund              {"name","amount"}            -> {"txid"}
POST /api/models/update            {"name","royalty"?,"reinvest"?,"new_sponsor"?} -> {"txid"}
POST /api/models/retire            {"name"}                     -> {"txid"}
POST /api/ai/chat                  {"model","messages":[{"role","content"}],"max_tokens"?} -> {"text","cost","machine"}
GET  /api/sites                    -> {"mine": [site…], "hosted": [nomes]}
POST /api/sites/publish            {"name","budget","replicas","files":[{"path","data_b64"}]} -> {"txid","url"}
POST /api/sites/fund               {"name","amount"}            -> {"txid"}
POST /api/quit                     -> {"ok": true}
```
`/api/state`:
```json
{"version": "2.0.0", "setup_done": true,
 "wallet": {"address": "sig…", "eth_address": "0x…", "can_spend": true, "balance": 0, "history": []},
 "node": {"url": "…", "ok": true, "error": null},
 "network": {"…": "o /status do nó"},
 "mining": {"enabled": true, "running": true, "paused": false, "workers": 4, "max_workers": 8,
            "cpu_percent": 50, "hashrate": 6800.0, "bits": 14, "shares_window": 12, "window": 123,
            "window_ends": 0, "epoch": 3, "epoch_ends": 0, "next_mint": 0, "my_points": 0,
            "total_points": 0, "my_share": 0.0, "est_reward": 0, "rewards_total": 0, "last_error": null},
 "machine": {"address": "sig…", "registered": true, "online": true, "hw": {}, "engine": {"kind","ready","detail"},
             "models": [{"name","status","progress","size"}], "sites": [], "jobs_done": 0, "earned": 0, "reputation": 0},
 "settings": {"cpu_percent": 50, "mining_enabled": true, "ai_enabled": true, "compute_enabled": true,
              "disk_gb": 20, "ai_ram_gb": 4, "node": "…", "start_with_system": false, "name": "…"},
 "logs": ["…"]}
```

### 7.4 Catálogo (`sigma/catalog.py`)
Modelos abertos com licença que permite uso comercial (dados reais do Hugging Face):
```
id                       repo / arquivo                                                          bytes        sha256                                                            licença
qwen2.5-0.5b             Qwen/Qwen2.5-0.5B-Instruct-GGUF qwen2.5-0.5b-instruct-q4_k_m.gguf        491400032    74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db  Apache-2.0
qwen2.5-1.5b             Qwen/Qwen2.5-1.5B-Instruct-GGUF qwen2.5-1.5b-instruct-q4_k_m.gguf        1117320736   6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e  Apache-2.0
qwen2.5-coder-1.5b       Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF qwen2.5-coder-1.5b-instruct-q4_k_m.gguf 1117320768 cc324af070c2ecbfd324a30884d2f951a7ff756aba85cb811a6ec436933bb046 Apache-2.0
smollm2-1.7b             bartowski/SmolLM2-1.7B-Instruct-GGUF SmolLM2-1.7B-Instruct-Q4_K_M.gguf   1055609824   77665ea4815999596525c636fbeb56ba8b080b46ae85efef4f0d986a139834d7  Apache-2.0
deepseek-r1-qwen-1.5b    bartowski/DeepSeek-R1-Distill-Qwen-1.5B-GGUF DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf 1117320800 1741e5b2d062b07acf048bf0d2c514dadf2a48f94e2b4aa0cfe069af3838ee2f MIT
phi-3.5-mini             bartowski/Phi-3.5-mini-instruct-GGUF Phi-3.5-mini-instruct-Q4_K_M.gguf   2393232672   e4165e3a71af97f1b4820da61079826d8752a2088e313af0c7d346796c38eff5  MIT
mistral-7b-v0.3          bartowski/Mistral-7B-Instruct-v0.3-GGUF Mistral-7B-Instruct-v0.3-Q4_K_M.gguf 4372812000 1270d22c0fbb3d092fb725d4d96c457b7b687a5f5a715abe1e818da303e562b6 Apache-2.0
qwen2.5-coder-7b         Qwen/Qwen2.5-Coder-7B-Instruct-GGUF qwen2.5-coder-7b-instruct-q4_k_m.gguf 4683073536  509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c  Apache-2.0
```
URL = `https://huggingface.co/<repo>/resolve/main/<arquivo>`. `resolve_hf_url(url)` obtém tamanho e sha256 de
qualquer GGUF do Hugging Face via `https://huggingface.co/api/models/<repo>?blobs=true` (campo `lfs`).

### 7.5 Painel (`sigma/web/app.html`, arquivo único)
Mesmo estilo visual do site (fundo preto, vermelho sinal `#ff3b1f`, Anton + IBM Plex Mono + Inter, cantos
retos). Primeira execução: assistente em 3 passos (o porquê em uma frase → carteira: criar / importar chave /
colar endereço → "Pronto, seu computador já está minerando"). Depois, abas:
**Início** (minerando sim/não, H/s, pontos e fatia da época, recompensa estimada, contagem regressiva até a
próxima distribuição, total ganho, saldo), **Carteira** (endereço com copiar, enviar, histórico incluindo
recompensas de mineração, exportar chave), **Meu computador** (hardware, % de CPU, IA liga/desliga, memória
para IA, disco, modelos que a rede colocou aqui com progresso de download, sites hospedados, ganhos, motor
de IA), **IA** (conversar com os modelos da rede pagando em SIG), **Sites** (publicar pasta, meus sites,
recarregar), **Modelos** (modelos da rede; apadrinhar do catálogo ou por URL do Hugging Face; meus modelos:
recarregar / royalty / passar adiante / apagar), **Rede** (emissão: minerado/teto, próxima distribuição,
histórico de épocas, máquinas online), **Configurações** e links para fórum/site/manifesto.

## 8. Site público
Mensagem central: **"Vão substituir você por um robô. Queremos que os donos dos robôs sejam as pessoas, não
as Big Techs."** O site convida a **baixar o app e minerar**. Explica a emissão (100 milhões de SIG, 0,5% do
que falta a cada 12 h, dividido pelo poder computacional; ninguém recebeu SIG de graça, nem os criadores),
mostra contagem regressiva e números ao vivo de `/status`/`/mining`, e não promete valorização.
- `index.html` reescrito, mais atraente; `baixar.html` novo (detecta o sistema; Windows: `/downloads/Sigma.exe`;
  Linux/macOS: `/downloads/sigma.pyz` com `python3 sigma.pyz`; SHA-256 em `/downloads/SHA256SUMS.txt`; aviso
  honesto sobre SmartScreen/antivírus; o que o app faz no seu PC); `tutorial.html` simplificado;
  `carteira.html` sem faucet/boas-vindas; `forum.html` com o mesmo menu.

## 9. Ferramentas e implantação
- `demo.py` e `rede_local.py` usam genesis rápido e o app em modo `--headless`.
- `packaging/build_windows.py` (PyInstaller, `--onefile --windowed`, ícone Σ gerado), `packaging/build_pyz.py`
  (zipapp), `packaging/sigma_main.py` (entrada).
- `deploy/deploy_vps.sh` com `RESET=1` para recriar a chain sem SIG pré-minerado (preserva `forum.db`),
  publica `/opt/sigma/downloads`, e o processo de hospedagem da VPS vira `python3 -m sigma.app --headless
  --no-mine --no-ai`.
