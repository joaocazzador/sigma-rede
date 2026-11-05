# Síntese final: modelos gigantes divididos entre computadores na rede Sigma

Nesta etapa não alterei nenhum arquivo do projeto, não iniciei nenhum processo e não baixei nada. Li só código e logs (`sigma/chain.py`, `host.py`, `engines.py`, `node.py`, `app.py`, `securestore.py`, `docs/ATUALIZACOES.md` e os logs em `C:\sgtmp\shard-exp\casa\logs\` e `C:\sgtmp\shard-exp\revisor-casa\logs\`). Não há processo para encerrar.

---

## Recomendação

**Fazer em duas etapas. A primeira é o "grupo de casa", entregue como regras v3. A segunda, com computadores de pessoas diferentes, só entra depois de três condições que hoje não existem.**

1. **Etapa 1, grupo de casa (v3):** uma pessoa junta os próprios computadores (mesmo payout, de preferência na mesma rede local). Para a chain, o grupo é **uma máquina só**: o *cabeça*. O cabeça guarda o GGUF inteiro, roda o `llama-server` e distribui camadas para os outros PCs (*trabalhadores*), que rodam o `ggml-rpc-server` fixado (b11721). Eles se falam por um túnel TLS autenticado com as chaves de máquina que já estão na chain.
   - **Por que primeiro:** não cria ninguém novo lendo pedidos, nem caminho de dinheiro novo, nem relay, nem carga na VPS. O RPC inseguro do llama.cpp fica restrito às máquinas do mesmo dono.
   - **Por que direto na v3 e não "só app" (a 1A do agente casa):** sem prazo maior por pedido, um modelo de 70B em CPU não termina uma resposta útil dentro dos ~540 s que o `host.py` tem hoje (`CLAIM_TIMEOUT − 60`). Os outros pontos derrubados na revisão também precisam de mudança de regra: `_prune` em 24 h, faltas zeradas pelo `pin_model` e mínimo do registro sem RAM.
2. **Etapa 2, desconhecidos:** fica condicionada a três coisas.
   - (a) Isolamento comprovado do processo do trabalhador e PC trabalhador **sem a chave da carteira**.
   - (b) Contabilidade por fatia e prova de serviço na chain.
   - (c) Um modelo de confiança para coordenadores escolhido pelo dono (ver "Decisões").

   Não recomendo o modo "qualquer um coordena", nem a VPS como coordenadora.
3. **O que se combina dos três desenhos:**
   - do "casa": a estrutura, o túnel TLS-PSK, o teto de memória por Job Object e as partes do GGUF;
   - do "coordenador": o rateio por membro e a VPS fora do papel de cabeça;
   - do "aberto": o porteiro validador de protocolo e o aviso de privacidade;
   - das revisões: o túnel com retomada, a discagem reversa de verdade, as correções no consenso e os números honestos.

---

## Como funciona

### Regras v3 da chain (`chain.py`, seguindo `docs/ATUALIZACOES.md`)

**Ativação**
- `RULES = 3` e altura marcada em `SCHEDULE[a8eabe25…1029]`.
- A rede principal ainda está na v1 (`SCHEDULE = {}`), então v2 e v3 podem entrar na mesma altura: `{2: h, 3: h}`.
- Nada novo em `DEFAULT_PARAMS` nem em `STATE_KEYS`. Os campos novos só nascem em registros gravados com `rule(3)`.

**Tipo novo precisa de trava própria**
- `SIGNED_TYPES` é derivado de `TX_FIELDS`. Por isso um tipo novo seria aceito em qualquer versão.
- Solução: `TYPES_SINCE = {"unpin_model": 3}`, conferido em `apply_tx` **e** em `_apply_evm`, com a mesma mensagem de recusa de hoje.
- Campos novos via `FIELDS_SINCE`: `register_machine.cluster`, `register_machine.caps`, `register_model.layers`, `create_job.deadline` e `pin_model.speed`.

**Grupo (`register_machine.cluster`)**
- O cabeça declara `{"members": [...]}`, com 1 a 7 membros. O trabalhador declara `{"head": addr}` e é **obrigado a ter `ai: false`**, para a memória dele não contar duas vezes.
- Um membro só é válido se: os dois lados se declaram, o payout é o mesmo, o membro está online e cede pelo menos 1.500 MB.
- Uma máquina nunca é cabeça e trabalhadora ao mesmo tempo.
- `_ai_ram(cabeça)` = memória do cabeça + Σ(memória cedida pelo membro − 512 MB), com teto `MAX_AI_RAM_MB`.
- Com `rule(3)`, `_assign_models` e `_update_demand` usam `_ai_ram`. `MAX_MODELS_PER_MACHINE` vale para o grupo inteiro.

**Modelo em partes**
- `register_model` de texto aceita, só com `rule(3)`, `files` com papel `"part"` (as partes 2..n do `llama-gguf-split`).
- A validação é própria da v3. `MODEL_ROLES` não muda, para os blocos da era v2 reexecutarem iguais.
- Limites: até 16 partes, até 60 GB cada e teto total a decidir (ver "Decisões"; recomendo 100 GB).
- `layers` é opcional e o host confere contra o `block_count` da parte 1.
- **Só recebe modelo em partes** quem declarou `caps: ["parts"]`. Isso evita que um app 2.5 baixe só a parte 1 e falhe.

**Correções de consenso que as revisões derrubaram** (todas atrás de `rule(3)`)
- `_prune` espera `max(UNPINNED_SITE_WINDOWS, assign_windows(m) + folga)`. Hoje, um modelo acima de ~43 GB é apagado em 24 h antes de alguém conseguir fixá-lo. Esse problema já existe com o teto atual de 60 GB.
- O mínimo do registro de texto passa a incluir `ram_mb`, como `funded()` já faz. Hoje um 70B com 2 réplicas registra com 122,4 SIG, mas os hosts só baixam com 181,7 SIG.
- Reenviar `pin_model` (para atualizar `speed`) **preserva** `misses` e `miss_window`.
- `unpin_model` (sair sem punição) só fica livre de cooldown se `misses == 0`.

**Prazo por pedido**
- `create_job.deadline` aceita de 600 a 14.400 s, só em pedido de texto direcionado.
- `claim_timeout(job)` passa a ser `job.get("deadline") or <regra atual>`. Jobs antigos continuam iguais.

**Prontidão**
- O cabeça só manda `pin_model` depois de um autoteste: carregar o modelo e gerar 32 tokens com temperatura 0.
- Com `rule(3)`, o `pin_model` exige `_ai_ram ≥ memória comprometida`. A velocidade medida vai em `speed`, só como informação.

**Falta de um membro**
- O host falha na hora os pedidos da fila como "engine", com troco e sem falta.
- Depois de 120 s, o cabeça manda `unpin_model` para os modelos que não cabem mais.
- Se o membro passar de `OFFLINE_WINDOWS` sem sinal, a virada da janela roda `_degrade_clusters`. Ele tira do cabeça os modelos que não cabem mais (pinners **e** atribuições), dos maiores para os menores, em ordem determinística. O cabeça vai para `former`, e o modelo volta para ele primeiro.

**Dinheiro**
- Nenhum caminho novo: aluguel e pagamento vão para o payout do cabeça, que é o do grupo. `_settle_ai` não muda.
- Invariantes testadas em todo bloco:
  - `supply == minted`;
  - custo + troco = escrow;
  - royalty ≤ custo;
  - aluguel creditado = aluguel debitado.

### Papéis na máquina (`host.py` / `engines.py`)

**Cabeça**
- Baixa as partes com o `fetch` atual, confere SHA-256 e `check_gguf` de cada uma e monta hard links `m-0000N-of-0000M.gguf`.
- Roda o `llama-server --rpc 127.0.0.1:<ponte1>,…`, sempre no llama.cpp gerenciado (o Ollama não tem RPC).
- Mantém a saída local com `-ot "^output\.weight$=CPU"`. A alternativa "rpc-server do próprio cabeça por último" também mantém a saída no cabeça e liga o paralelismo de pipeline; a escolha entre as duas depende de medição em PCs reais (ver "Números").
- O supervisor olha o túnel e o progresso dos tokens, **não o `/health`**: medido, o `/health` continua "ok" com um trabalhador morto.
- Responde em streaming e corta no prazo, cobrando só os tokens gerados.
- `LOAD_TIMEOUT` próprio para grupos. O `EngineManager.chat` não pode reiniciar sozinho, no meio do pedido, um grupo que leva minutos para recarregar.

**Trabalhador**
- Um `ggml-rpc-server -H 127.0.0.1 -c` por modelo, porque cada um atende um cliente por vez (medido).
- `LLAMA_CACHE` isolado por modelo.
- Prioridade `BELOW_NORMAL`, Job Object com `KILL_ON_JOB_CLOSE` e teto de memória igual ao cedido + 15%. Medido: acima do teto a carga falha de forma limpa e o trabalhador sobrevive.
- Impede a suspensão enquanto estiver no grupo, pausa o minerador quando há cálculo e sai do grupo se estiver na bateria.
- Memória cedida padrão de 50 a 60% da RAM, para o Windows não paginar os pesos.

**PC trabalhador sem carteira**
- O app instalado como trabalhador guarda só a chave da máquina. Os ganhos vão para um endereço de payout cuja chave privada não fica naquele PC.
- O app já tem esse mecanismo no modo headless (`--wallet` define `payout_override`). Falta expor isso no painel.
- Assim, um problema no processo do RPC não alcança a carteira da pessoa.

### Túnel e NAT

**Etapa 1**
- O **trabalhador disca para o cabeça**. Só o cabeça abre uma porta na rede local.
- Descoberta: máquinas com o mesmo payout na chain, mais broadcast na LAN ou o IP digitado. O dono confirma no painel.
- Autenticação e cifra: TLS 1.3 com PSK, derivada do ECDH secp256k1 das chaves de máquina publicadas na chain. O servidor não tem certificado, e o cliente recusa servidor que mostre certificado.
  - Medido: handshake de 3,6 a 5,6 ms, 3 de 3 conexões indevidas recusadas, +0,7 ms por ida e volta, ~140 MB/s.
  - Exige Python 3.13 ou mais novo (o `.exe` já usa o 3.14).
- **Ainda não está implementado:**
  - O protótipo medido tem o sentido invertido (o trabalhador escuta). A discagem reversa tem de ser escrita e medida.
  - Falta a multiplexação. O `llama-server` abre de 14 a 18 conexões por servidor na carga; elas viram canais lógicos sobre o canal de controle.
  - Falta a **retomada**: numerar bytes, guardar um buffer de reenvio limitado e reconectar o enlace de fora sem fechar as conexões locais. Medido: pausas de 30 s e 90 s com as conexões abertas não derrubaram nada e o texto saiu idêntico. A desistência fica para depois de ~120 s.
- **Guarda de protocolo no porteiro do trabalhador:**
  - HELLO primeiro, versão 8.0.0;
  - teto de tamanho por mensagem;
  - bloqueio de `COMM_*`, que abriria um socket em todas as interfaces.

  A ponte no cabeça só aceita a conexão do `llama-server` que ela mesma iniciou.

**Etapa 2:** os dois lados discam para um relay que só emenda bytes cifrados. O relay precisa de operador e de remuneração; a VPS do GardenCounties não serve como relay principal.

### Como os pesos chegam

- **Etapa 1:** o cabeça baixa do Hugging Face e manda as fatias pela rede local, no comportamento de fábrica do llama.cpp. O cache `-c` faz as recargas mandarem quase só os tensores pequenos.
- **Etapa 2:** um manifesto publicado no registro, com offset, tamanho, SHA-256 e FNV-1a de cada tensor. Cada trabalhador baixa só a sua fatia por HTTP Range, confere o SHA-256 e preenche o próprio cache. O cabeça ainda precisa do arquivo inteiro, porque o cliente de fábrica calcula o hash sobre os bytes.

### Segurança: quem vê o quê

| Parte | Etapa 1 (mesmo dono) | Etapa 2 (desconhecidos) |
|---|---|---|
| Cabeça | lê o texto, como a máquina que responde hoje | lê o texto; é um terceiro |
| Trabalhadores | veem ativações, das quais dá para deduzir boa parte do texto; são do mesmo dono | idem, mas são terceiros: **não há sigilo** |
| Rede local / relay | só tráfego TLS | só tráfego TLS; vê quem fala com quem e quanto |
| Nó/VPS | blobs selados, como hoje | idem |
| Integridade da resposta | confiança no dono, como hoje | não é verificável. Comparar hashes entre grupos **não funciona**: hardware e divisão diferentes mudam os tokens |

O risco que impede a Etapa 2 hoje: o rpc-server não tem autenticação e é descrito pelos próprios autores como frágil e inseguro. Quem coordena controla o que o processo do trabalhador executa. O túnel, o porteiro e o Job Object reduzem esse risco, mas não o zeram. O isolamento real do processo no Windows sem admin não foi testado.

---

## Números

### Medidos (i7-3770, loopback, CPU disputada com o minerador; não representam máquinas separadas)

| Fato | Valor |
|---|---|
| Texto dividido igual ao local | SHA-256 idêntico em todas as rodadas, com 2 e 3 servidores, túnel e partes |
| Custo por token | `ms/token = 60,5 + 1,91·N + 0,990·N·RTT` (R² = 0,9998); 1 ida e volta por trabalhador, em sequência |
| Exemplo | 3 servidores: 14,70 t/s com RTT 0 e 1,95 t/s com RTT 150 ms |
| Topologia de casa pelo túnel (0,5B) | 15,40 t/s (faixa 15,09–15,59) contra 16,99 local (−9%) |
| Logits com a saída remota | 607.752 B por token; com 50/10 Mbit/s isso deu 654 ms/token, contra 170 ms com `-ot` |
| Bytes por salto | 4 × hidden (3.584 B aqui) |
| Carga com cache cheio | 247,9 MB enviados em vez de 392,6 MB |
| 4 pedidos simultâneos | vazão total 2,5× maior com RTT 50 e 3,1× com RTT 100 |
| Pausa de enlace com TCP aberto | 30 s e 90 s sem queda e texto idêntico |
| Queda real de um rpc-server | o `llama-server` morre e não reconecta |
| Teto de memória (Job Object) | carga além do teto falha limpa, e o trabalhador sobrevive |
| Prompt de 3.011 tokens, numa CPU só | casa: 85,8–90,8 s; "rpc do cabeça por último" (pipeline ligado): 126,6–133,7 s. Numa CPU só o pipeline não ajuda; em PCs separados, não foi medido |

### Estimados para um 70B Q4_K_M (~42,5 GB; conta, não medição)

| Memória de cada PC | 4–8 PCs na rede local | Etapa 2, via relay |
|---|---|---|
| Como a máquina de teste (6,9 GB/s) | ~0,16 t/s | ~0,15 t/s |
| DDR4 comum (20 GB/s) | ~0,45–0,47 t/s | ~0,39–0,43 t/s |
| DDR4 boa (35 GB/s) | ~0,77–0,82 t/s | ~0,60–0,71 t/s |
| DDR5 (55 GB/s) | ~1,2–1,3 t/s | ~0,8–1,0 t/s |

- **Leitura:** em CPU, o gargalo é a memória, não a rede. O grupo **não fica mais rápido** que um PC com memória suficiente; ele só torna o modelo possível.
- **Primeira resposta:** ~2,6 s por token de prompt numa máquina como a de teste (um prompt de 1.000 tokens leva ~43 min). Num PC moderno, estimo 3 a 8 min para 1.000 tokens.
- **O que cabe pela regra `1,2× + 512`** (um 70B pede 51.512 MB):

  | Grupo | Resultado |
  |---|---|
  | 5 PCs de 16 GB | cabe |
  | 3 PCs de 32 GB | cabe |
  | 4 PCs de 16 GB | não cabe |
  | 8 PCs de 16 GB | até ~80 GB |

- **Carga pela rede local** (~32 GB nos trabalhadores):

  | Rede | Primeira vez | Recarga com cache |
  |---|---|---|
  | Gigabit | ~5 min | ~8 s + leitura do disco e hash no cabeça (não medidos) |
  | Wi-Fi 5 | ~18 min | — |
  | Wi-Fi 4 | ~67 min | — |

- **Aluguel de uma cópia de 70B:** 315.524 µSIG por janela, ou 90,87 SIG por dia com demanda 1×. Pode chegar a 4× com a rede cheia.

### O que o usuário **não** vai ter
- Velocidade de ChatGPT. Num 400B pela internet, espere bem menos que 1 token/s em CPU, e o teto proposto nem comporta 400 GB.
- Respostas imediatas a prompts longos.
- Sigilo diante de quem coordena, ou diante de trabalhadores desconhecidos.
- Grupo que sobreviva à queda de um membro no meio de uma resposta.
- Ganho para quem tem um PC só: ele não contribui para modelos gigantes na Etapa 1.

---

## Decisões do dono

1. **Quem pode coordenar um grupo (modelo de confiança)**
   - **(A) Só máquinas do mesmo dono.** Nenhuma confiança nova; quem lê os pedidos é o mesmo de hoje; nenhum custo para a VPS. Une apenas os PCs de uma pessoa, então fica concentrado em quem tem vários.
   - **(B) Coordenadores aprovados (por votação, ou servidores dedicados do projeto, nunca a VPS do GardenCounties).** Une desconhecidos. O coordenador lê os pedidos, e quem empresta o PC passa a confiar nele. Exige isolamento do trabalhador, relay, contabilidade por fatia e prova de serviço. Um servidor dedicado é custo novo; a VPS não comporta o papel (2 núcleos, 7 GB de RAM, ~88 GB de disco, outro negócio rodando ao lado).
   - **(C) Aberto: qualquer máquina com reputação ou caução.** É o mais descentralizado, mas expõe quem empresta o PC a qualquer desconhecido e é aberto a Sybil.
   - **Recomendação:** (A) agora; (B) só depois de cumpridas as condições da Etapa 2; (C) não.
2. **Teto de tamanho do modelo**
   - **60 GB:** arquivo único, mudança mínima.
   - **100 GB:** o que um grupo de casa de 8 × 16 GB comporta.
   - **250 GB:** exige ~38 GB cedidos por PC, e downloads de dias.
   - **Recomendação:** 100 GB.
3. **Papel da VPS**
   - **Só validador, como hoje.**
   - **Relay leve:** só ativações, com cota.
   - **Coordenadora:** inviável para modelo gigante.
   - **Recomendação:** só validador. Na Etapa 2, um relay separado.
4. **Prazo máximo de um pedido**
   - **10 min (o de hoje):** um 70B em CPU dá ~200 tokens.
   - **1 h.**
   - **4 h:** o escrow fica preso por mais tempo.
   - **Recomendação:** até 4 h, com o tempo estimado mostrado antes de enviar.
5. **Ligado por padrão?**
   - **Desligado (opt-in)** ou **sugerido no painel para quem tem vários PCs**.
   - **Recomendação:** opt-in, com aviso de privacidade e de lentidão para quem pergunta.
6. **PC trabalhador sem carteira:** obrigatório ou opcional? **Recomendação:** obrigatório para entrar em grupo, com ganhos indo para um endereço de payout.
7. **Investir na Etapa 2?**
   - **Sim:** 2 a 4 meses, com riscos que não fecham por completo.
   - **Não, por ora:** observar quantos donos usam grupos de casa antes.
   - **Recomendação:** decidir depois de medir a Etapa 1 em PCs reais.

---

## Fases e esforço

| Fase | Conteúdo | Esforço (uma pessoa) |
|---|---|---|
| 0. Medições que faltam | 2–3 PCs reais por cabo e Wi-Fi; as duas topologias (`-ot` contra rpc do cabeça por último); GGUF em partes + RPC; um modelo médio (~14B); prompt acima de 512 tokens; firewall do Windows | 1–2 semanas |
| 1. Regras v3 + testes | tudo da seção de regras; testes de reexecução (`test_upgrades`), recusa antes da altura, grupo, degradação, propriedades com sequências aleatórias (oferta = mintado) | 2–3 semanas |
| 2. Máquina | túnel com discagem reversa, multiplexação e retomada; porteiro; Job Object; planejador de camadas (conferido pelo log); supervisor; hard links das partes; modo sem carteira; impedir suspensão | 3–5 semanas |
| 3. Cliente e painel | prazo pela velocidade, `max_tokens` limitado pelo tempo, selo "grupo de N PCs", avisos, painel do dono | 1–2 semanas |
| 4. Publicação | ponta a ponta com 3 instâncias e nó local; 24 h de teste contínuo; validador primeiro, instaladores depois, altura com ≥ 1 dia de aviso | 1 semana + janela de aviso |
| 5. Etapa 2 (condicional) | coordenadores conforme a decisão 1, manifesto + Range, relay remunerado, aluguel e pagamento por fatia **só por serviço pago**, caução, sondas, isolamento testado | 2–4 meses |
| 6. Grupo aberto | — | indefinido; pode nunca valer o risco |

Testes de aceitação da Etapa 1:
- 100% de texto igual ao local;
- geração ≥ 90% da local no loopback (medido: 91%);
- queda de um membro termina em falha com troco, degradação e volta do modelo;
- nenhum processo sobra no fim.

---

## Riscos e incertezas

**Medido:** o mecanismo de divisão e a igualdade do texto; 1 ida e volta por trabalhador por token; bytes por salto e logits; efeito da banda; cache; falhas (não reconecta, um cliente por vez); o túnel TLS-PSK com a biblioteca padrão; o teto do Job Object; partes do GGUF carregando localmente; tolerância a pausas de 30 e 90 s. Tudo numa CPU só, por loopback.

**Estimado:** tudo sobre 70B, 100 GB e 250 GB (velocidade, memória, carga, prompt), redes locais reais, Wi-Fi, internet e relay.

**Incerto ou não testado:**
- GGUF em partes junto com RPC;
- RPC com GPU (se o pacote Vulkan traz o rpc-server);
- jitter, perda e slow start na internet;
- tempo de leitura e de hash do arquivo no cabeça;
- buffers de cálculo reais de um 70B;
- paralelismo de pipeline em máquinas separadas;
- isolamento do processo no Windows sem admin;
- aviso do firewall e antivírus;
- notebooks e Wi-Fi instável;
- modelos MoE, que podem mudar bastante a conta de velocidade.

**Riscos que continuam:**
1. Qualquer membro que cai derruba a resposta em andamento, e a recarga leva minutos.
2. A capacidade e a velocidade são autodeclaradas. "Mesmo payout" prova para onde vai o dinheiro, não que a pessoa é a mesma.
3. O RPC do llama.cpp é prova de conceito: toda atualização do motor precisa ser coordenada no grupo inteiro, e o protocolo pode mudar.
4. Na Etapa 2, não há sigilo contra o coordenador nem contra os trabalhadores, e a integridade da resposta só se verifica por estatística.
5. Pode haver poucos donos com 4 a 5 PCs de memória sobrando. A oferta de modelos gigantes ficaria concentrada.