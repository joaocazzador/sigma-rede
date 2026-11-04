# Sigma v2.1: complemento da especificação v2

Vale tudo o que está em `docs/SPEC-v2.md`, com as mudanças abaixo. Quando os dois discordarem, vale este.
Versão do pacote: `sigma.__version__ = "2.1.0"`. A rede pública será recriada do zero (`RESET=1`), então
mudanças de consenso não precisam de compatibilidade com a chain antiga.

Os achados da revisão adversarial que motivaram cada regra estão em `docs/revisao-v2-achados.md` (procure pelo
id entre colchetes, ex.: `[con-halt-unserializable-tx]`). Este documento é o contrato entre os módulos; o
arquivo de achados é o detalhe (cenário, evidência, texto sugerido).

---------------------------------------------------------------------------------------------------

## 1. Pedidos de IA privados (jobs direcionados e cifrados)

Hoje a pergunta e a resposta de cada job de IA ficam em blobs públicos no nó: qualquer pessoa lê. Na
v2.1 o cliente escolhe a máquina que vai responder, cifra o pedido para a chave pública dela, e a máquina
cifra a resposta para a chave pública do cliente. O nó e o resto da rede só veem bytes cifrados.

Continua público (e os textos devem dizer isso com honestidade): que houve um job, de qual carteira, para
qual modelo e máquina, quanto custou, quantos tokens, quando. A máquina que responde precisa ler a pergunta
para responder. É o computador de outra pessoa: o app oficial não guarda o que lê, mas quem controla a
máquina pode mudar o programa. Nunca escrever "ponta a ponta", "privado" ou "ninguém vê" sem essa ressalva.

### 1.1 Cifra (`sigma/crypto.py`, pronto e testado em `tests/test_crypto.py`)

- `seal(pub_hex, data: bytes) -> bytes`: ECIES (chave efêmera + ECDH secp256k1, fluxo SHAKE-256,
  HMAC-SHA256). Formato `b"SIGMA-SEAL-1"` + chave pública efêmera (64 bytes) + nonce (16) + cifrado + tag (32).
- `unseal(priv_int, blob) -> bytes` e `Wallet.unseal(blob)`: `ValueError` se não for para esta chave, se não
  estiver cifrado ou se foi alterado.
- `is_sealed(blob) -> bool`.

### 1.2 Conteúdo dos blobs de IA

- Pedido (texto claro): `canon({"messages": [{"role", "content"}...], "max_tokens": int, "json": bool})`.
  Num job direcionado o blob enviado ao nó é `seal(pub_da_maquina, esse_json)`.
- Resposta de sucesso: `canon({"text", "model", "machine"})`.
- Resposta de falha: `canon({"text": "", "error": "<frase em português>", "code": c, "model", "machine"})` com
  `c` em `"context"` (conversa maior que o contexto do modelo), `"engine"` (motor de IA falhou ou está fora),
  `"unreadable"` (pedido não abre com a chave desta máquina ou é JSON inválido), `"busy"` (fila cheia).
- Se o job tem `client_pub`, toda resposta (sucesso ou falha) vai `seal(client_pub, json)`.

### 1.3 Chain: ver §6 e §7 (campos `pub`, `machine`, `client_pub`, `failed`, `cancel_job`, prazos e falhas).

### 1.4 Nó

- `/machines` e `/machine/<addr>` mostram `"pub"` e `"jobs_failed"`. `/job/<id>` e `/jobs` mostram
  `"machine"`, `"client_pub"`, `"window"` e `"missed"` quando existirem.
- Admissão (só no nó, não é regra de consenso): `create_job` de IA com `machine` só entra no mempool se o blob
  `spec_hash` já estiver no armazenamento do nó: senão `TxError("envie o pedido (/blob) antes de criar o job")`.
  Assim a máquina escolhida nunca pode alegar que o pedido sumiu.

### 1.5 Máquina (`sigma/host.py`)

- `register_machine` sempre envia `"pub": machine_wallet.pub`.
- Jobs abertos: ignora os direcionados a outra máquina. Reivindica os direcionados a ela em segundos, mesmo
  enquanto gera outra resposta (fila: reivindica primeiro, responde em ordem). Com 3 ou mais pedidos de IA já
  na fila, responde os novos como falha `"busy"` em vez de deixar expirar.
- Pedido cifrado: abre com a chave da máquina. Se não abrir ou não for JSON válido: **reivindica e responde
  falha `"unreadable"`** (cifrada para `client_pub` quando houver). Assim lixo enviado de propósito não vira
  "falta" (miss) da máquina.
- Conversa maior que o contexto (`est > CONTEXT - 32`, mesma estimativa do cliente) → falha `"context"`.
  `max_tokens = min(max_tokens, CONTEXT - est)`. `EngineError` → falha `"engine"`.
- Falha = `submit_result` com `"failed": true`, `usage` zerado (ver §7). O cliente recebe o reembolso na hora.
- Nunca grava pedidos ou respostas em disco ou no log (só tamanhos, tokens e tempo).
- Jobs de IA sem `"machine"` (públicos, de clientes antigos) continuam sendo atendidos como na v2.

### 1.6 Cliente (`sigma/client.py`): ver §12. Sempre privado; nunca cai sozinho para um job público.

### 1.7 Demo

A demo mostra que o blob do pedido no nó está cifrado (não contém o texto da pergunta) e que a resposta
chega legível só para o cliente; mostra também um cancelamento e uma falha "context" com reembolso.

---------------------------------------------------------------------------------------------------

## 2. Sigma Code no painel (aba "Programar") e na CLI

O Sigma Code (agente de programação) roda dentro do app, com a carteira do app, sem terminal. Quem responde
é o computador de outra pessoa da rede: ele vê tudo o que o agente lê e sugere tudo o que o agente faz. Por
isso **nada é gravado nem executado sem o usuário ver**, e arquivos de segredo nunca saem
`[seg-sigmacode-writes-sem-aprovacao] [ux-sigmacode-aba-sem-travas]`.

### 2.1 `sigma/sigmacode.py`

```python
Agent(client, model, max_tokens=2048, max_steps=15, auto_yes=False, root=".",
      confirm=None, on_event=None, should_stop=None, auto_files=False, protect=())
```

- `confirm(tool, args, info) -> bool`, com `info = {"reason": "write"|"edit"|"command", "preview": str,
  "changed": [caminhos relativos já alterados nesta tarefa]}`. `preview`: para gravar, caminho + tamanho +
  as primeiras 40 linhas do conteúdo novo; para editar, o trecho antigo e o novo; para comando, o comando.
  Cortado em 4.000 caracteres. Chamado antes de `write_file`/`create_file`/`edit_file` quando `auto_files` é
  falso, e **sempre** antes de `run_command` (a menos que `auto_yes`, só na CLI com `--yes`). `None` =
  pergunta no terminal (CLI). Recusa → a ferramenta devolve `"o usuário recusou"`.
- Arquivos protegidos (comparação sem diferenciar maiúsculas, em cada parte do caminho): `.env` e `.env.*`,
  `*.key`, `*.pem`, `*.p12`, `*.pfx`, `*.secret`, `id_rsa*`, `id_ed25519*`, `.npmrc`, `.pypirc`, `.netrc`,
  `.git-credentials`, as pastas `.ssh/`, `.gnupg/`, `.aws/`, `.git/` (ler `.gitignore` pode), e tudo em
  `protect` (caminhos absolutos; a CLI passa o arquivo da carteira). Para qualquer ferramenta a resposta é
  `"arquivo protegido: o Sigma Code não lê nem altera este arquivo"`; `list_dir` esconde esses itens.
- Caminhos sempre confinados à pasta (`realpath`; recusa caminho absoluto, de outra unidade, UNC e qualquer
  coisa que resolva para fora). `read_file` lê no máximo 200 KB; saída de comando cortada em `MAX_OUTPUT`;
  comando com timeout de 120 s.
- `on_event(evento: dict)`, na ordem em que acontecem: `{"kind": "step", "n", "cost", "machine"}`,
  `{"kind": "assistant", "text"}`, `{"kind": "tool", "tool", "args", "output", "refused": bool}`,
  `{"kind": "final", "text"}`, `{"kind": "error", "text"}`, `{"kind": "limit"}`. `None` = imprime no terminal.
- `should_stop() -> bool`: conferido antes de cada passo e passado ao `client.ask` (cancela pedido não
  reivindicado). `run(task)` devolve o texto final (ou `None` se parou/limite).
- CLI (`main()`): chama `netsafe.install_default_opener()`; mostra uma vez o aviso "Aviso: quem responde é o
  computador de outra pessoa da rede. Tudo o que o Sigma Code ler desta pasta vai para essa máquina. Não use
  em pastas com senhas, chaves ou documentos pessoais."; recusa começar se o arquivo `--wallet` estiver dentro
  de `--dir`; flag nova `--yes-files` (libera gravar/editar sem perguntar); `--yes` continua só para comandos.

### 2.2 API local do app (`sigma/app.py`, mesmas regras de token/Host/Origin das outras rotas)

- `GET /api/code/state` ->
  `{"running", "folder", "model", "task", "auto_files", "steps": [...], "pending": {...}|null,
    "spent", "final", "error"}`; cada passo
  `{"n", "kind": "assistant"|"tool"|"info"|"error"|"final", "text", "tool"?, "args"?, "output"?, "cost"?,
    "machine"?, "refused"?}`; `pending` = `{"id", "tool", "args", "reason", "preview", "changed"}`.
- `POST /api/code/start {"folder", "task", "model"?, "auto_files"?}`: `auto_files` padrão **falso**. 400 se já
  houver uma tarefa rodando, se a pasta não existir / não for pasta / for a raiz de uma unidade / for a pasta
  do usuário inteira / for a própria Área de Trabalho, Downloads ou Documentos (inclusive as do OneDrive;
  subpastas delas podem) / ficar dentro da pasta de dados do Sigma, se a tarefa estiver vazia, se a carteira
  for só endereço, ou se não houver modelo no ar. Sem `model`, usa `client.pick_model(prefer="coder")`.
  O agente recebe `protect=[arquivos de chave do app]`.
- `POST /api/code/answer {"id", "ok"}`, `POST /api/code/stop` (para depois do passo atual e recusa o que
  estiver pendente), `POST /api/code/pick-folder` (só com janela; seletor nativo do tkinter na thread do tk;
  devolve `{"folder": caminho ou ""}`; sem janela: 400 `"digite o caminho da pasta"`).
- Uma tarefa por vez. O histórico da última tarefa fica na memória até a próxima começar.

### 2.3 Painel (`sigma/web/app.html`)

Aba **Programar** (entre IA e Sites): pasta (campo + "Escolher pasta"), modelo (os que estão no ar, padrão um
de código), tarefa, caixa "aprovar alterações de arquivos sem perguntar" (**desmarcada em toda tarefa nova**;
comandos sempre pedem aprovação), Começar / Parar, a conversa ao vivo (passos, ferramentas e resultados, custo
de cada passo e total). Cartões de aprovação:
- comando: "Executar este comando no seu computador? Ele foi escrito pela IA de outra pessoa da rede. Só
  execute se você entende o que ele faz." + a lista "arquivos alterados nesta tarefa" + botões "Executar" e
  "Não executar";
- arquivo: "A IA quer alterar {arquivo}. Confira as mudanças antes de aplicar." + prévia + "Aplicar" e
  "Recusar".
Texto fixo: "Escolha a pasta do projeto. O Sigma Code só mexe dentro dela, e o que ele lê vai cifrado para o
computador de outra pessoa da rede, que responde. Arquivos de senha e chaves (.env, .key, .ssh) ficam de
fora. Cada passo é pago em SIG."

---------------------------------------------------------------------------------------------------

## 3. Sites publicados isolados da carteira e do fórum

Respostas de `/site/...` levam:
- `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads`
  (sem `allow-same-origin`: origem opaca, sem `localStorage`, cookies nem APIs do fórum);
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` e `Access-Control-Allow-Origin: *`.

Respostas de `/blob/...`: `Content-Security-Policy: sandbox`, `X-Content-Type-Options: nosniff`.
O tutorial e a aba Sites avisam: "Sites da rede rodam isolados: não podem usar cookies nem localStorage."

## 4. Biblioteca ethers com integridade conferida

`carteira.html` **e** `forum.html` carregam o ethers com
`integrity="sha384-6Zl0Pc8zjSz8KvmNeXRvUQgY4ryFb+BwDvKCmLYcBME0joAaru491tQgi9B7zsMM" crossorigin="anonymous"`
(conferido igual no jsdelivr e no unpkg para `ethers@6.13.4/dist/ethers.umd.min.js`) `[seg-forum-ethers-sem-sri]`.

---------------------------------------------------------------------------------------------------

## 5. Mineração: dificuldade dentro do hash, melhor relatório vale, vários PCs na mesma carteira

`[eco-posthoc-bits] [eco-preempt-work-report] [copy-limite-por-ip-cgnat]`

- `pow.POW_SALT = b"sigma-pow-v2"`. `pow.pow_hash(seed, ident, bits, nonce)` =
  scrypt(`f"{seed}:{ident}:{bits}:{nonce}"`, salt, N=1024, r=1, p=1, dklen=32) como inteiro big-endian.
  `pow.share_ok(seed, ident, nonce, bits)` (mesma ordem de argumentos de hoje) usa `pow_hash(seed, ident,
  bits, nonce)`. Um hash só vale no nível escolhido antes de calculá-lo.
- `ident` = `miner` (endereço `sig…`) ou `f"{miner}.{worker}"` quando o relatório traz `worker`
  (1 a 16 caracteres `[0-9a-f]`). O app usa `worker = endereço_da_máquina[3:11]`, então cada PC tem a sua
  identidade e vários PCs podem minerar para a mesma carteira.
- Relatório (tx `work`, sem assinatura): `{"type": "work", "data": {"miner", "window", "bits", "nonces",
  "worker"?}}`. Os pontos vão sempre para `miner`.
- Seed de cada janela: `seeds[str(w)] = sha(f"{prev_hash}:{w}".encode())` (a da janela 0 é
  `sha(f"{hash_do_genesis}:0".encode())`), para duas janelas nunca terem a mesma seed.
- `mining["last_reports"]`: `{ident: {str(janela): pontos}}`; na virada de janela ficam só janelas `>= w-1`.
- **Melhor relatório vale**: `novos = n_shares << bits`; `antes = last_reports[ident][str(janela)]` (0 se não
  houver). Exige `novos > antes` (`"esta janela já tem um relatório com tantos pontos ou mais deste
  minerador"`), conferido **antes** do scrypt (é barato). Se passar: credita só `novos - antes` em
  `points[miner]` e `miners[miner]["points_total"]` e guarda `novos`. Quem manda um relatório pelo endereço
  de outro só consegue aumentar os pontos dele, nunca apagar.
- `miners[miner]`: `{"points_total", "rewards_total", "last_window", "last_rate"}`. Ao creditar `d` na janela
  `j`: se `j > last_window` → `last_window = j`, `last_rate = d / W`; se `j == last_window` → `last_rate += d / W`
  (soma os PCs da mesma carteira); se `j < last_window` → só os pontos.
- Minerador (`sigma/miner.py`): `Miner(client, payout, workers, log=print, worker=None)`; mira ~14 shares por
  janela (`bits = round(log2(taxa * W / 14))`, limitado a `[min_bits, MAX_SHARE_BITS]`); calcula
  `pow_hash(seed, ident, bits, nonce)` com os bits do job; `_bench` usa bits fixos; envia `worker` quando há.

---------------------------------------------------------------------------------------------------

## 6. Transações: formato estrito, limites e campos novos

`[con-halt-unserializable-tx] [con-free-state-bloat] [eco-ai-max-price-from-priciest-host] [eco-site-size-self-declared]`

### 6.1 Formato

- Chaves de topo aceitas: assinada `{"type", "data", "nonce", "pub", "sig", "id"}`; `work` e `evm`
  `{"type", "data", "id"}`; o `data` do `evm` é exatamente `{"raw"}`. Qualquer outra chave →
  `TxError("campos desconhecidos na transação: a, b")`. O nó normaliza `work` e `evm` antes de guardar
  (só os campos do contrato chegam a um bloco).
- Campos de `data` por tipo (qualquer outro → `TxError("campos desconhecidos em <tipo>: a, b")`):

| tipo | campos |
|---|---|
| transfer | to, amount |
| register_machine | payout, name, hw, endpoint, prices, ai, compute, pub |
| ping | (nenhum) |
| create_job (ai) | kind, model, spec_hash, escrow, max_price, machine, client_pub |
| create_job (compute) | kind, task, replicas, escrow, max_price |
| claim_job | job_id |
| submit_result | job_id, result_hash, usage, failed |
| cancel_job | job_id |
| deploy_site | name, manifest, budget, replicas |
| pin_site | name, version |
| register_model | name, url, sha256, size, royalty_bps, reinvest_bps, replicas, license, description, budget |
| fund_model | name, amount |
| update_model | name, royalty_bps, reinvest_bps, new_sponsor |
| retire_model / pin_model | name |
| work | miner, window, bits, nonces, worker |

  Subcampos: `hw` = `{cpu_cores, ram_mb, ai_ram_mb, vram_mb, disk_mb, gpu}`; `prices` =
  `{ai_per_1k_tokens, compute_per_unit}` (o preço de hospedagem saiu da máquina: §8); `usage` =
  `{tokens_in, tokens_out}`; cada entrada do `manifest` é exatamente `{hash, size}`.
- Nada que `canon()` não consiga serializar entra no estado ou num bloco: `evm.system_call` roda
  `chain.canon(call)` dentro de try e transforma `UnicodeEncodeError`/`ValueError` em
  `ValueError("data deve ser um JSON {type, data} em UTF-8 válido e com números dentro do limite")`; o nó
  roda `canon(tx)` na admissão e responde `TxError("transação malformada: texto inválido ou número grande
  demais")`.

### 6.2 Limites de inteiros (`_int_in`)

- amount, escrow, budget, max_price: 1..`supply_cap` (budget de `deploy_site` pode ser 0 num site existente).
- `ai_per_1k_tokens`: 1..`MAX_AI_PRICE = 1_000_000`; `compute_per_unit`: 1..`MAX_COMPUTE_PRICE = 1_000_000`.
- `hw`: 0..`HW_MAX`, e `ai_ram_mb` 0..`1_048_576`.
- `tokens_in`, `tokens_out`: 0..`10**9`.
- manifest: 1..500 arquivos; caminho com 1..200 caracteres (`"caminho longo demais (máximo 200
  caracteres)"`), sem `/` no início e sem `..`; `size` 0..`MAX_FILE = 20 * 1024 * 1024` (o `MAX_BLOB` do nó).
- Site novo precisa de `budget >= MIN_SITE_BUDGET = 10_000` µSIG.

### 6.3 Campos novos

- `register_machine.pub` (opcional, 128 hex): `address_of(pub) == sender`, senão
  `TxError("pub não corresponde à chave da máquina")`; guardado em `machines[addr]["pub"]`.
- Máquina nova ganha `"jobs_failed": 0` e `"rep_window": -1`.
- Tx nova `cancel_job {job_id}` (§7).

---------------------------------------------------------------------------------------------------

## 7. Jobs: estados, prazos, falhas, cancelamento e reputação

`[eco-fake-pins-mitigation] [fluxo-v21-maquina-dormindo-perde-modelo] [fluxo-ia-claim-preso-contexto]
[ux-chat-sem-cancelar] [eco-compute-dispute-wipes-points] [eco-compute-escrow-stuck]`

- Todo job ganha `"window"` (janela de mineração em que foi criado).
- Estados: `open`, `running`, `done`, `failed` (só IA), `disputed` (só computação), `expired`, `canceled`.
- `create_job` de IA com `"machine"`: a máquina existe, está online, sinalizou na janela atual ou na anterior
  (`last_seen >= w - 1`), tem `ai` ligado, tem `pub`, hospeda o modelo, não é o próprio cliente, e o preço dela
  é `<= max_price`; `"client_pub"` obrigatório, com `address_of(client_pub) == sender`. Mensagens claras para
  cada caso. O job guarda `machine` e `client_pub`.
- `claim_job`: job com `machine` só pode ser reivindicado por ela (`"este job é para outra máquina"`).
- `submit_result` de IA com `"failed": true`: `usage` ausente ou zerado; o job fecha `"failed"`, o escrow
  volta inteiro ao cliente, `machines[m]["jobs_failed"] += 1`, sem pagamento e sem mudar reputação nem
  `misses`. `failed` num job de computação → `TxError`.
- `cancel_job {job_id}`: só o cliente (`"só quem pediu pode cancelar"`), só com status `open` e sem nenhuma
  reivindicação (`"um computador já está respondendo este pedido"`); devolve o escrow e fecha `"canceled"`.
- Prazos em `end_block` (pelo relógio do bloco):
  - Direcionado, aberto, sem reivindicação há mais de `TARGET_TIMEOUT = 120` s → devolve o escrow, fecha
    `"expired"`, `job["missed"] = True` e chama `_miss(job)`.
  - Direcionado, reivindicado e sem resultado há mais de `CLAIM_TIMEOUT` → devolve o escrow, fecha
    `"expired"` (não reabre), `missed = True`, `_miss(job)`.
  - `_miss(job)`: se a máquina ainda hospeda o modelo e sinalizou depois da falta anterior
    (`machines[m]["last_seen"] > pin["miss_window"]`): `pin["misses"] += 1`, `pin["miss_window"] = w`,
    reputação −1. Isso dá no máximo uma falta por pin por janela, e o PC que dormiu leva uma falta só até
    sinalizar de novo; quem sinaliza toda janela sem responder perde o modelo em 3 janelas. Com
    `misses >= MAX_MISSES = 3`, a máquina deixa de hospedar o modelo (sai de `pinners` e de `mc["models"]`)
    e entra em `cooldown` daquele modelo por `COOLDOWN_WINDOWS = 288` janelas (§8).
  - Job direcionado que termina `"done"` zera o `misses` daquele pin.
  - Não direcionados: como hoje (reivindicação vencida reabre o job e custa 1 de reputação). Job aberto há mais
    de `OPEN_TIMEOUT`: se já tem resultados (computação parcial) → `_settle_compute(job)`; senão devolve e
    fecha `"expired"`.
- Reputação +1 em `_pay` só quando o valor pago é `> 0`, o cliente não é a máquina nem o payout dela, e no
  máximo uma vez por máquina por janela (`mc["rep_window"]`).
- Computação: `_settle_compute` conta voto só de máquinas com payout diferente do cliente e um voto por
  payout (em ordem de endereço da máquina); maioria = mais de metade de `replicas` entre os votos contados;
  paga só quem votou com a maioria; quem divergiu perde 1 de reputação. **Nunca** zera pontos de mineração.

---------------------------------------------------------------------------------------------------

## 8. Modelos e sites: preço da rede, atribuição, castigo e limpeza

`[eco-host-price-ignored-by-assignment] [eco-fake-pins-mitigation] [seg-model-arbitrary-gguf]
[fluxo-modelo-apagado-ao-perder-atribuicao] [con-free-state-bloat] [sec-machine-registration-state-bloat]`

- Preço de hospedagem é parâmetro da rede: `params["hosting_per_mb_window"]` no genesis (padrão 5, inteiro de
  1 a 100, validado em `_check_params`). Aluguel por janela e por pinner online =
  `max(1, ceil(size * hosting_per_mb_window / 1_000_000))`. Os pins não guardam preço: site `{"window"}`,
  modelo `{"window", "misses": 0, "miss_window": -1}`.
- Campos novos do modelo: `"cooldown": {addr: janela_final}`, `"former": {addr: janela}`, `"failed": 0`,
  `"pinned_window"` (janela de criação e, depois, a última janela com pinner online).
- `_assign_models`: pula máquina em cooldown (`cooldown[addr] > w`); no máximo `MAX_MODELS_PER_MACHINE = 4`
  modelos por máquina (hospedando + atribuídos); ordena as candidatas por `(addr not in former, sha(seed +
  nome + addr))`, então o modelo volta primeiro para quem já tem o arquivo.
- `former`: preenchido em `_unhost` (máquina ficou offline ou desligou a IA) com a janela atual; ficam só
  entradas com janela `>= w - 288`, no máximo `2 * replicas` (as mais novas).
- Prazo da atribuição: `assign_windows(m) = max(ASSIGN_WINDOWS, ceil(size / (500_000 * W)))` (pelo menos
  ~4 Mbit/s). Venceu: se o modelo tem algum pinner **e** é `funded(m)`, a máquina perde 1 de reputação; se não
  tem nenhum pinner, não pune ninguém e soma `failed += 1` (uma vez por janela). Com `failed >= 3` e nenhum
  pinner, o modelo é apagado (`_delete_model`, devolve o orçamento ao padrinho): arquivo errado ou inválido
  não fica rodando para sempre.
- `rent_window(item) = replicas * max(1, ceil(size * hosting_per_mb_window / 1_000_000))` (aluguel de todas
  as cópias por janela) e `funded(m)`: `size <= FUNDED_ABOVE = 50_000_000` ou o orçamento paga pelo menos
  `MIN_FUNDED_SECONDS = 86400` de `rent_window`. As máquinas não baixam modelo grande que não paga nem um dia
  (§11), por isso a atribuição vencida de um modelo assim não custa reputação.
- Sites: entrada expirada ganha `"expired_window"`; site ganha `"pinned_window"` (como no modelo).
- Limpeza na virada de janela, `PRUNE_WINDOWS = 2016` (1 semana com janelas de 5 min), só do que não tem
  nenhuma referência:
  - máquina com `w - last_seen > 2016` que não está em pinners/assigned de modelo ou site nem em job aberto
    (reivindicação ou alvo);
  - minerador com `last_window < w - 2016` e sem pontos na época;
  - site expirado há mais de 2016 janelas, ou ativo sem pinner desde `pinned_window < w - 2016`: devolve o que
    sobrou do orçamento ao dono e apaga;
  - modelo sem pinner desde `pinned_window < w - 2016`: `_delete_model`;
  - `retired_models` com mais de 7 dias.

## 9. Créditos no histórico `[ux-historico-incompleto]`

- `State.credits`: lista transitória (fora de `to_dict`, zerada em `begin_block`, como `last_settlement`) de
  `[endereço, tipo, µSIG, ref]`, com `tipo` em `"rent"` (aluguel; ref = nome do site/modelo), `"job"` (pagamento
  de pedido; ref = id do job), `"royalty"` (ref = modelo), `"change"` (troco do escrow; ref = job), `"refund"`
  (job expirado/cancelado/falho/disputado; ref = job) e `"budget"` (devolução de orçamento; ref = modelo/site).
- O nó junta isso no histórico: uma entrada por (endereço, tipo, época) `{"type": "credit", "kind", "epoch",
  "amount", "count", "height", "time"}`, atualizada a cada bloco, e guarda no snapshot o que precisar para
  reconstruir igual depois de reiniciar.

---------------------------------------------------------------------------------------------------

## 10. Nó (`sigma/node.py`, `sigma/rpc.py`, `sigma/evm.py`, `sigma/forum.py`)

`[con-halt-unserializable-tx] [sec-rpc-batch-recover-dos] [sec-work-rate-limit-cgnat]
[fluxo-deploy-no-memoria-reinicio-em-laco] [fluxo-deploy-blobs-sem-limpeza] [sec-unbounded-outbound-read-oom]
[sec-forum-challenge-unbounded] [fluxo-polling-escala-no]`

- Admissão de tx: (1) formato/chaves e `canon(tx)`; (2) normaliza `work`/`evm`; (3) **fora do lock**, aquece
  os caches caros (`_sig_ok`, `decode_evm`); (4) limites por IP no `--public`; (5) dentro do lock, aplica no
  estado pendente. Corpo de `/tx` e `/rpc` no máximo 256 KB.
- `/rpc` em lote: no máximo 50 entradas (`-32600`, `"lote grande demais (máximo 50)"`).
- Limites por IP por janela no `--public` (flags com estes padrões):
  `--work-per-ip 256` relatórios aceitos e `--bad-work-per-ip 8` recusados (só os recusados gastam a cota
  baixa; relatório válido nunca é barrado pela cota de inválidos); `--tx-per-ip 300` transações assinadas
  (inclui `eth_sendRawTransaction`); `--blob-mb-per-ip-hour 500` MB de blobs novos por hora. Respostas 429 em
  português, dizendo o limite e quando tentar de novo.
- `produce_block`: `s.root()` e `make_block` dentro do try; qualquer exceção descarta as txs do lote
  (`_drop` com motivo), tira do mempool, refaz o pendente e volta sem gravar. Nunca para de produzir.
- Memória: só os últimos 2.000 blocos na RAM + `offsets` (posição de cada altura no `chain.jsonl`);
  `node.block(h)` lê do disco quando precisa; `/blocks`, `/status` e o RPC usam isso. `tx_pos` guarda as EVM
  para sempre e as demais das últimas ~2 h. Histórico no máximo 1.000 entradas por endereço.
- Blobs: limpeza a cada hora (ficam os manifests de sites ativos ou expirados há menos de 7 dias e os
  `spec_hash`/`result_hash`/resultados de jobs ainda no estado; o resto com mais de 1 h sai); acima do limite
  ainda aceita blobs de até 256 KB até limite + 1 GB (para o chat não parar).
- Leituras de saída sempre com teto (`netsafe.read_capped`): blob até `min(tamanho declarado, MAX_BLOB)`,
  JSON de peers/máquinas até 64 MB. No `--public`, endpoints de máquinas que resolvem para a rede interna são
  ignorados (`netsafe.is_internal`).
- Fórum: desafios de login expiram de verdade e ficam no máximo 10.000 (o mais antigo sai).
- `/jobs?status=open` em cache por altura. `/mining` ganha `"hosting_per_mb_window"` e
  `"ai_default_price"` (= `chain.DEFAULT_PRICES["ai_per_1k_tokens"]`). `/status` mantém `"version"`.
- `main()` reconfigura stdout/stderr para UTF-8.

## 11. Máquina (`sigma/host.py`, `sigma/engines.py`, `sigma/miner.py`)

`[fluxo-modelo-apagado-ao-perder-atribuicao] [fluxo-ollama-oscila-desliga-ia] [seg-model-ssrf]
[seg-model-arbitrary-gguf] [seg-llama-sem-apikey] [eco-site-size-self-declared] [fluxo-polling-escala-no]
[ux-minerando-sem-contar] [fluxo-limite-relatorios-por-ip] [copy-custos-energia-memoria]`

- §1.5 inteiro. `register_machine` manda só os campos do §6.1 (`prices` sem hospedagem).
- O minerador recebe `worker = endereço_da_máquina[3:11]`.
- Arquivo de modelo baixado fica no disco enquanto o modelo existir na rede (`.gguf`, `.ok` e `.part`), mesmo
  sem atribuição; sai o mais antigo quando o disco passar de `disk_gb` ou para caber um download novo; sai na
  hora se o modelo deixar a rede. Se `/models` não responde, não apaga nada. Reatribuição = carregar + pin.
- Modelo que não é `funded` (§8; o nó manda `budget` e `rent_window` em cada atribuição do `/machine`): a
  máquina não baixa (arquivo que já está no disco carrega normalmente), mostra "Modelo em espera: o orçamento
  dele não paga nem um dia de hospedagem. Este computador baixa quando o padrinho recarregar." e olha de novo
  em 10 min. O painel recusa apadrinhar abaixo desse mínimo, dizendo o valor.
- Downloads de modelo com `netsafe.safe_opener()` (ou `allow_internal=True` quando o próprio nó é interno,
  igual à regra de hoje); erro de conexão interna → `EngineError("a URL do modelo aponta para a rede
  interna; recusada")`.
- `engines.check_gguf(path)` (Python puro, `struct`) depois do SHA-256 e antes de carregar; regras em
  `[seg-model-arbitrary-gguf]`. Recusado → `EngineError("arquivo recusado: não é um modelo GGUF válido")`,
  sha anotado em `<dados>/models/recusados.json` por 24 h e status do modelo `"recusado"` com a frase
  "Modelo recusado: o arquivo não confere com o que o padrinho registrou" ou "... não é um GGUF válido".
- `engines.write_test_gguf(path, size)`: grava um GGUF mínimo válido (magic, versão 3, 0 tensores, 1 metadado)
  completado até `size` bytes. Testes, demo e rede local usam isso nos modelos falsos.
- llama-server com chave: `env["LLAMA_API_KEY"] = secrets.token_urlsafe(24)` e `Authorization: Bearer` no
  chat; roda com prioridade baixa (Windows `BELOW_NORMAL_PRIORITY_CLASS`, outros `nice 10`). Ollama: recusa
  versão abaixo de `OLLAMA_MIN = "0.6.0"` com "atualize o Ollama para hospedar modelos da rede".
- `engines.describe()`/status do motor ganha `"installing": bool` e `"install_progress": 0..1`.
- Motor fora do ar por pouco tempo: continua se registrando com `ai=True` por até 120 s; nesse tempo não pega
  job público e responde os direcionados como falha `"engine"`. Loga uma vez.
- Sites: confere espaço livre pelos tamanhos declarados **antes** de baixar e recusa (com espera de 600 s) o
  site cujo arquivo baixado não tem o tamanho declarado.
- Consulta ao nó: jobs a cada 2 s só se hospeda algum modelo, senão a cada 20 s; ping usa o `/mining` que o
  minerador já buscou (se tiver menos de 10 s), senão busca no máximo a cada 30 s.
- `Miner.stats()` ganha `"fail_streak"` (zera num relatório aceito, +1 num recusado), `"last_fail"` (texto da
  recusa), `"last_ok"` (hora do último aceito) e `"worker"`. A janela só entra em `_reported` depois de aceita
  ou de erro 400; em erro de rede, 5xx ou 429 tenta de novo uma vez ~30 s depois, enquanto `/mining` ainda
  mostrar `prev_window == w`. Log: `"[minerador] janela 3: 9 provas enviadas (~36.864 contas)"`.

## 12. Cliente e CLI (`sigma/client.py`, `sigma/cli.py`, `sigma/crypto.py`)

`[eco-ai-max-price-from-priciest-host] [priv-textos-ia-cifrada] [fluxo-v21-maquina-dormindo-perde-modelo]
[fluxo-ia-claim-preso-contexto] [ux-chat-sem-cancelar] [seg-copy-modelos-seguro-licenca] [ux-termos-inconsistentes]`

- `JOB_END = ("done", "failed", "disputed", "expired", "canceled")`. `CONTEXT = 4096` (igual a `engines.CONTEXT`;
  um teste confere).
- `fit_messages(messages, max_tokens, context=CONTEXT) -> (mensagens, quantas_ficaram_de_fora)`: estimativa
  `len(content)//3 + 8` por mensagem `+ 16`; mantém as `system`, a primeira do usuário e as mais recentes que
  couberem em `context - max_tokens - 32`; se nem a última couber →
  `SigmaError("sua mensagem é longa demais para os modelos da rede: divida o texto em partes menores")`.
- `ask(model, messages, max_tokens=1024, max_price=None, timeout=900, json_mode=False, should_stop=None,
  on_job=None) -> (texto, job)`; `job` ganha `"machine"`, `"trimmed"` e `"attempts"`:
  1. `w` = janela atual (`/mining`). Candidatas: máquinas online com `last_seen >= w - 1`, que hospedam o
     modelo, com `pub`. Teto = `max_price` ou `max(ai_default_price, menor preço entre as candidatas)`;
     ficam as de preço `<= teto`.
  2. Ordem: primeiro as que sinalizaram na janela atual, depois as outras; dentro de cada grupo, **sorteio
     uniforme** (`random.SystemRandom`), para nenhum operador receber todas as perguntas de um modelo.
  3. Até 3 tentativas, cada uma numa máquina diferente: `seal` para a `pub` dela → `put_blob` →
     `create_job {kind: "ai", model, spec_hash, escrow, max_price: teto, machine, client_pub: wallet.pub}` →
     `on_job(job_id)` → espera. Escrow = `ceil((estimativa_entrada + max_tokens) * teto / 1000)`.
     Entre consultas: se `should_stop()` e ninguém reivindicou → `cancel_job` e `SigmaError("cancelado")`.
     `done` → baixa, `wallet.unseal`, devolve. `failed` → abre o erro: `code == "context"` →
     `SigmaError` com a frase da máquina; outro código → próxima máquina. `expired` → próxima máquina.
  4. Sem candidata: `SigmaError("nenhuma máquina online com o modelo aceita pedidos cifrados agora")`.
     Tentativas esgotadas: `SigmaError("Nenhum computador respondeu a tempo. Você não pagou nada por esta
     pergunta: o valor reservado voltou para a sua carteira. Tente de novo.")`.
- `cancel_job(job_id)`. `compute()`: teto padrão = o preço da `replicas`-ésima máquina mais barata (bastam
  essas), nunca o maior.
- `crypto.checksum_ok(addr)` e `crypto.to_checksum(addr)` (EIP-55) prontos, com testes em
  `tests/test_netsafe.py`. Opcional: acelerar `_mul/_add` com coordenadas jacobianas, com teste que compara com
  a versão afim para chaves aleatórias (assinaturas, endereços e `seal` não podem mudar).
- CLI: `netsafe.install_default_opener()` no início; a conferência de licença comercial roda sempre
  (`catalog.commercial_ok` recusa licença vazia, `other` ou `unknown`: "esse modelo não declara a licença no
  Hugging Face: escolha um que declare"); plural certo ("1 minerador", "2 mineradores").

## 13. App e painel (`sigma/app.py`, `sigma/web/app.html`, `sigma/securestore.py`, `sigma/catalog.py`)

Tudo o que os achados de painel pedem (ids `ux-*`, `fluxo-*` e `seg-*` com `app.py`/`app.html`). Contratos que
outras partes usam:
- Nomes que o site cita: abas Início, Carteira, Meu computador, IA, Programar, Sites, Modelos, Rede,
  Configurações; opções do assistente "Criar uma carteira nova", "Usar minha chave", "Só colar um endereço";
  cartão "Carteira que recebe" com "Importar a chave desta carteira" e "Trocar carteira"; em Configurações
  "Iniciar o Sigma junto com o computador" e "Minerar também na bateria"; "época" em todo lugar (nunca
  "rodada"); "contas por segundo".
- `main()` chama `netsafe.install_default_opener()`; no macOS, erro com `netsafe.cert_problem` vira "O Python
  deste Mac não tem os certificados de segurança. Abra Aplicativos › Python 3.x › Install Certificates.command
  e abra o Sigma de novo."
- `/api/setup` aceita `expect_address` (§ `[ux-sem-trocar-carteira]`) e usa `crypto.checksum_ok` antes de
  `norm_address` em endereço digitado (setup, enviar, passar adiante): "Esse endereço tem um caractere errado.
  Copie e cole de novo direto da MetaMask, sem digitar."
- O minerador do app recebe `worker = endereço_da_máquina[3:11]`.
- Uma instância por pasta de dados (`app.lock`); versão mais nova fecha a antiga (`/api/quit`) e assume;
  aviso de versão nova quando `/status.version` da rede for maior.
- Painel sem fontes de terceiros: usa fontes do sistema (títulos `Anton, Impact, "Haettenschweiler",
  "Arial Narrow Bold", sans-serif`; texto `Inter, "Segoe UI", system-ui, -apple-system, Roboto, Arial,
  sans-serif`; código `"IBM Plex Mono", Consolas, "Cascadia Mono", Menlo, monospace`). Nada de Google Fonts.
- Mineração pausa na bateria por padrão (`mine_on_battery = False`).

## 14. Site (`sigma/web/*.html`, `sigma/web/sigma.css`, `README.md`)

Todos os achados `copy-*`, `seg-copy-*`, `priv-*`, `design-*`, `texto-*`, `ux-carteira-navegador-sem-backup`,
`seg-forum-ethers-sem-sri`, `seg-download-confianca-sslip-sha`, `fluxo-macos-sem-certificados` (parte do site).
Decisões do projeto que valem para os textos:
- Esta versão já sai com a IA cifrada: usar os textos "no mesmo release da cifra" de `[priv-textos-ia-cifrada]`.
- Sem licença escolhida ainda: trocar "código aberto" por "código-fonte disponível" (o dono decide a licença).
- Sem domínio próprio nem segundo canal de SHA-256 ainda: o passo a passo de download fala só do endereço
  oficial atual e da lista desta página, com a ressalva de que o SHA-256 não protege de site falso.
- Limites de mineração: até 256 relatórios por janela por conexão de internet; vários PCs podem usar a mesma
  carteira (cada um conta separado); com CGNAT, vizinhos dividem o limite.
- Não prometer "não zerar saldos"; o tutorial diz que hoje um servidor do projeto assina os blocos e, numa
  troca de versão, pode reiniciar a rede.
- Notebook: a mineração pausa na bateria e volta na tomada.
- O site continua usando Google Fonts por enquanto (hospedar as fontes aqui depende de baixar os arquivos).

## 15. Ferramentas e implantação

- `demo.py`, `rede_local.py`, `tests/test_e2e_maquina.py`, `tests/test_integration.py`: genesis rápido com
  `hosting_per_mb_window`, modelos falsos com `engines.write_test_gguf`, mineração com `worker`, IA privada
  (§1.7), cancelamento, falha "context".
- `packaging/build_windows.py`: ícone também dentro do pacote (`--add-data` do `.ico` e do `.png`) para a
  janelinha do Tk usar `iconbitmap`/`iconphoto`.
- `deploy/ecosystem.config.cjs`: `sigma-node` com `--heartbeat 30` e `max_memory_restart: "1500M"`.

## 16. Já pronto antes desta rodada

- `sigma/netsafe.py` (+ `tests/test_netsafe.py`): `blocked(ip)`, `is_internal(host)`, `safe_opener(
  allow_internal=False)`, `read_capped(resp, limite)`, `ssl_context()`, `install_default_opener()`,
  `cert_problem(erro)`.
- `crypto.checksum_ok`, `crypto.to_checksum`, `crypto.seal/unseal/is_sealed`, `Wallet.unseal`.
- `sigma/pow.py` já no formato do §5 (`POW_SALT = b"sigma-pow-v2"`, `pow_hash(seed, ident, bits, nonce)`,
  `share_ok(seed, ident, nonce, bits)`): chain e minerador só precisam passar a usar.
- `sigma.__version__ = "2.1.0"`.
