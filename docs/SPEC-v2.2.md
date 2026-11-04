# Sigma v2.2: correções da revisão adversarial da v2.1

Vale tudo o que está em `docs/SPEC-v2.md` e `docs/SPEC-v2.1.md`; quando discordarem, vale este. A rede pública
ainda não foi lançada, então mudanças de consenso são livres (continua sem SIG pré-minerado). Versão do pacote:
`sigma.__version__ = "2.2.0"`.

Cada correção vem de um achado em `docs/revisao-v21-achados.md` (procure pelo id entre colchetes). Este
documento fixa só o que cruza mais de um módulo; o resto está no achado.

## 1. Pedidos direcionados: fila limitada `[targeted-flood-evicts-honest-host] [maq-targeted-flood-misses]`

- **Chain** (`create_job` de IA direcionado, `_target`): recusa quando a máquina escolhida já tem
  `MAX_TARGET_OPEN = 6` jobs vivos (`open`/`running`) com `job["machine"] == addr`
  (`"a máquina escolhida está com a fila cheia agora; escolha outra"`), e no máximo
  `MAX_TARGET_PER_CLIENT = 2` vivos por `(client, machine)` (`"você já tem pedidos demais nessa máquina; espere
  ela responder"`).
- **Chain** (`_miss`): não conta falta se a máquina entregou ou falhou (`submit_result`) algum job direcionado
  na janela atual (campo novo `machines[addr]["answered_window"]`, gravado em `_tx_submit_result` de job com
  `machine`). Ela está viva, só ocupada. Mantém a regra do `last_seen > miss_window`.
- **Node**: `claim_job` e `submit_result` de um job que tem `machine` **não** contam na cota `--tx-per-ip` (a
  máquina é forçada a mandá-los por causa de jobs de terceiros). As outras txs assinadas continuam contando.
- **Host**: não bloqueia o laço de jobs esperando confirmação de um direcionado. Reivindica com `wait=False`
  cada direcionado da passada (nonces em fila sob `_tx_lock`) e manda as falhas (`busy`/`unreadable`/
  `context`/`engine`) de uma thread de respostas, também `wait=False`. Se um cliente já tem mais de `MAX_QUEUE`
  direcionados abertos para esta máquina, falha os extras de uma vez.
- **Cliente** (`ask`): quando `create_job` é recusado por fila cheia, pula para a próxima candidata.

## 2. Escrow precisa cobrir o preço `[escrow-need-not-cover-price]`

- **Chain**: `create_job` de computação exige `escrow >= units * max_price * replicas`
  (`"o valor reservado precisa cobrir {units} × {max_price} µSIG × {replicas} cópias"`).
- **Chain**: `create_job` de IA ganha o campo obrigatório **público** `max_tokens` (1..`CONTEXT`=4096) e exige
  `escrow >= ceil((max_tokens + 16) * max_price / 1000)`. O job guarda `max_tokens`.
- **Host**: num pedido de IA, limita `max_tokens` a `min(job["max_tokens"], escrow*1000//price - est)`; se
  sobrar menos de 64 tokens, responde falha `"busy"`. Em computação, só reivindica se
  `escrow // replicas >= units * price`.
- **Cliente** (`ask`): manda `max_tokens` no `create_job` e dimensiona o escrow por ele (já é o teto que usa).

## 3. Reputação não é mais arma de graça `[compute-vote-sybil-rep-drain] [poison-model-rep-drain]`

- **Chain** (`_settle_compute`): a máquina que diverge da maioria **não** perde reputação (ela já não recebe
  pagamento). Em vez disso soma `machines[addr]["compute_disputes"] += 1` (campo novo, começa em 0), que o
  painel mostra mas que `_assign_models` **não** usa.
- **Chain** (`_expire_assignments`): atribuição vencida **não** tira reputação. Em vez disso, por
  `(máquina, modelo)` que venceu, grava `m["cooldown"][a] = w + COOLDOWN_WINDOWS` (não reatribui o mesmo
  arquivo à mesma máquina) e libera a vaga. O contador `failed`/`_delete_model` continua só para "ninguém deu
  pin". Com isso recusar um arquivo ruim deixa de custar reputação.
- **Chain**: tx nova `decline_model {name}` (campos `{name}`), que a máquina manda quando recusa o arquivo
  (sha/GGUF errado, URL interna, modelo sem orçamento): remove a atribuição na hora e grava o `cooldown`, sem
  punição. Só o endereço com a atribuição pode mandar.
- **Host**: manda `decline_model` em `_refuse` (sha/GGUF) e no caso `underfunded`, em vez de só parar de baixar.

## 4. Estado e disco não incham de graça `[site-manifest-state-bloat-cheap] [window-turnover-models-x-machines] [disk-bloat-nome-grande] [sig-cache-guarda-lixo]`

- **Chain**: orçamento mínimo de site proporcional ao manifest: `MIN_SITE_BUDGET + 2_000 µSIG por arquivo`.
  Limites menores: `MAX_SITE_FILES = 200`, `MAX_PATH = 120`, soma dos caminhos `<= 16 KB`. Site sem nenhum
  pinner expira (sai do estado) em `UNPINNED_SITE_WINDOWS = 288` janelas (~1 dia), não 2016.
- **Chain**: orçamento mínimo de modelo `budget >= rent_window(m) * 288` (um dia de todas as cópias), já
  previsto pela `funded()` da v2.1 — agora **recusado na hora** em `register_model` quando `size > FUNDED_ABOVE`
  (`"o orçamento precisa cobrir um dia de hospedagem: {mínimo} µSIG"`). Modelo nunca atribuível (nenhuma máquina
  cabe) soma `failed` e sai em ~288 janelas sem pinner.
- **Chain** (`_assign_models`): monta a lista de máquinas elegíveis uma vez por janela e então itera os
  modelos; `_unhost` usa `mc["models"]` + varre só `assigned`, sem percorrer todos os modelos por máquina.
- **Chain** (`_tx_register_machine`): recusa `name` com mais de 40 e `gpu` com mais de 80 caracteres
  (`TxError`), em vez de truncar calado — assim o bloco gravado no disco não carrega o texto gigante.
- **Node**: `normalize_tx` guarda no bloco a tx já limpa; a cota `--tx-per-ip` de uma tx assinada custa
  `1 + len(canon(data))//1024` unidades (como já é com EVM). `_warm`/`_sig_ok` em cache por
  `(pub, sha(msg), sig)` e só são chamados quando `pub`/`sig` casam `HEX128` e o tipo está em `SIGNED_TYPES`.
  `canon(data)` de uma tx assinada no máximo 32 KB (`"transação grande demais"`).

## 5. RPC e cabeçalhos `[rpc-feehistory-amplificacao] [site-redirect-crlf-header-injection]`

- **Node** (`rpc.eth_feeHistory`): `rewardPercentiles` só pode ser uma lista de no máximo 100 números
  (`RpcError -32602`); `n` continua limitado a 1..1024. Teto geral de alguns MB em qualquer resposta de RPC.
- **Node** (`_send`): recusa (500) qualquer valor de cabeçalho com CR ou LF. No 301 de `/site/<nome>` sem barra:
  se `parts[1]` não casa `SITE_NAME`, responde 404; o `Location` usa `urllib.parse.quote(parts[1], safe="")`.

## 6. Sites de terceiros não se passam pelo oficial `[site-phishing-dominio-oficial]`

A entrega em outra origem fica para a implantação (precisa de nginx/certificado na VPS; ver §9). Agora:
- **Node**: `/site/...` responde 403 para extensões executáveis/instaláveis
  (`.exe .msi .msix .bat .cmd .com .scr .ps1 .vbs .jar .pyz .apk .dmg .pkg .appimage .deb .rpm .zip .7z .rar`)
  e o `Content-Security-Policy` de `/site/` perde o `allow-downloads`.
- **Chain** (`SITE_NAME`): nomes reservados recusados em `deploy_site` de um site novo (`baixar`, `download`,
  `downloads`, `carteira`, `wallet`, `forum`, `tutorial`, `sigma`, `oficial`, `suporte`, `ajuda`, `atualizar`,
  `update`, `app`, `admin`, `metamask`).
- **Site/README**: aviso em `baixar.html`, `tutorial.html#sites` e `README.md`: endereços com `/site/` são
  sites publicados por qualquer pessoa da rede, não pelo projeto; o Sigma oficial só se baixa na página inicial
  oficial; nenhuma página em `/site/` pede a sua chave.

## 7. Sigma Code e cartões de aprovação `[app-approval-card-spoof] [app-command-card-newline-hide] [sigmacode-write-truncates-before-encoding] [sigmacode-line-endings-rewritten] [command-card-multiline-posix]`

- **sigmacode**: `write_file`/`edit_file` gravam de forma atômica — `content.encode("utf-8")` (ou o texto já
  editado) num arquivo temporário na mesma pasta + `os.replace`; recusam argumentos com surrogates
  (`[\ud800-\udfff]`) com mensagem em pt-BR antes de perguntar; leem e gravam com `newline=""` (preservam as
  quebras de linha do arquivo; `old` casa o texto cru). O `info` do `confirm` e o evento `tool` levam
  `target = visible(caminho resolvido)`; `run_command` leva só o comando. A prévia de comando mostra as
  quebras de linha (`⏎`) e o número de linhas; comando com mais de uma linha é recusado
  (`"um comando por vez"`).
- **app** (`CodeRunner`): guarda `pending.target`/`step.target` e repassa ao painel; `_send` usa
  `ensure_ascii=True` (ou saneia a prévia) para um argumento ruim nunca quebrar `/api/code/state`.
- **app.html**: o título do cartão e a conversa usam `p.target` (não `argLine` de chaves livres dos args); o
  cartão de comando mostra todas as linhas (sem `max-height`) e o número de linhas.

## 8. Painel e carteira `[app-swap-gate-unknown-balance] [app-instance-impostor] [app-code-folder-bypass] [carteira-royalty-zerado] [privacidade-ia-uma-maquina-so] [site-expirado-nome-tomado] [maq-context-overflow-mislabeled-engine] [client-wait-gives-up-on-brief-outage] [cli-ctrl-c-before-on-job] [app-wizard-battery-text] [app-autostart-apostrophe] [app-takeover-double-click-false-error]`

Cada um na correção do próprio achado. Pontos que valem lembrar:
- **Saldo desconhecido** (`app` + `app.html`): o cache do saldo começa `None` até o primeiro `/account` do
  endereço atual; `state()` manda `balance: null`/`balance_known: false`; o painel mostra `—`, não `0,00`; o
  portão de "Trocar carteira" exige sempre mostrar/guardar a chave quando o saldo não é conhecido, e
  `api_setup` recusa trocar uma carteira com chave guardada sem `"key_saved": true`.
- **Impostor na porta** (`app`): se `take_lock()` conseguiu a trava, ignora qualquer `found` na porta (um Sigma
  desta pasta seguraria a trava); segue para o boot pulando a porta ocupada. Só confia em `found` quando a
  trava está com outro processo, e aí autentica (HMAC do token sobre um nonce) antes de mandar o token.
- **Contexto estourado** (`engines`/`host`/`client`): o `_LlamaCpp.chat`/`_Ollama.chat` detecta a recusa de
  contexto do motor (HTTP 400 "exceeds the available context size"; o Ollama pode truncar calado — tratar os
  dois) e o `host._answer` mapeia para falha `"context"`, não `"engine"`, então o cliente mostra "divida o
  texto" e para de tentar outras máquinas.
- **Espera do cliente** (`client`): em poll que falha, espera com `POLL_SLOW`/recuo e continua até o prazo do
  job; se o prazo passar com o job vivo, mensagem dizendo que só cobra se a máquina entregar e que volta
  sozinho; job `done` é pago, então baixa o resultado até o prazo.

## 9. Já pronto nesta rodada (antes dos agentes)

- A regra do orçamento mínimo de modelo e da falta de PC dormindo (v2.1 §7/§8) já está no código; os agentes de
  consenso só precisam alinhar `register_model` à recusa na hora (§4) e `_expire_assignments`/`_settle_compute`
  à §3.

## 10. Implantação (depois das correções)

- Servir sites publicados em outra origem (`sites.195-35-29-232.sslip.io/<nome>/`) com `server_name` e
  certificado próprios no nginx, e no host oficial `/site/<nome>/` virar 301 para lá. Enquanto isso não sai,
  valem o 403 por extensão e os nomes reservados da §6.
