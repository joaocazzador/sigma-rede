# Latência de rede no modelo dividido (llama.cpp b11721, ggml-rpc): agente "latencia"

Cada rpc-server custa **uma ida e volta (RTT) por token gerado**, e essas idas e voltas acontecem uma depois da outra. Com N computadores a um RTT R do coordenador, cada token leva cerca de **C + N·(R + 2 ms)**, onde C é o cômputo somado de todas as partes. No modelo de teste, a geração caiu de 15,4 t/s (1 servidor, RTT 0) para 1,95 t/s (3 servidores, RTT 150 ms). Num 70B rodando em PCs com CPU, a estimativa é de 0,5 a 0,7 t/s com 4 a 8 máquinas e RTT de 30 a 80 ms.

- **Ida e volta:** é medida e confirmada pelo protocolo. ms/token cresce com o RTT na proporção de 0,985, 1,983 e 2,970 com 1, 2 e 3 servidores. Em cada token, os únicos comandos que esperam resposta são os GET_TENSOR, um por servidor.
- **Bytes por token:** cada salto leva 4 × hidden bytes de ativação (3.584 B aqui), mais cerca de 2,5 KB de entradas pequenas.
  - O último servidor devolve os logits (607.752 B por token) quando a camada de saída fica nele.
  - Com 50 Mbit/s de subida isso soma 101 ms por token, medido. Com `-ot ^output\.weight$=CPU` esse custo desaparece.
- **Carga do modelo:** o coordenador manda todos os pesos (392,6 MB aqui).
  - Com o cache `-c` já cheio, só vão os tensores de até 10 MiB: 247,9 MB aqui, e estimo 0,85 GB dos 42 GB de um 70B.
  - Toda carga paga ainda cerca de 80 RTT por servidor de preparação.
- **Pedidos simultâneos:** vários pedidos no mesmo passo dividem o RTT. Com 4 simultâneos, a vazão total subiu 2,5× com RTT 50 e 3,1× com RTT 100.

## Ambiente e método
- **Máquina:** a mesma do agente "basico" (Windows 11, i7-3770 com 4 núcleos e 8 threads, 16 GB).
  - O minerador `Sigma (2).exe` do usuário manteve a CPU em 100% o tempo todo, em prioridade BelowNormal.
  - O Ollama ficou ocioso.
  - Tudo rodou em 127.0.0.1, nas portas 50061–50073, 50077–50079 e 8871–8872.
- **Execução anterior:** havia na minha pasta o trabalho de uma execução anterior desta tarefa, interrompida às 01:41.
  - Revisei os scripts.
  - Baixei de novo o `ggml-rpc.cpp` da tag b11721 e o SHA-256 bate com a cópia em `ref\`.
  - **Refiz todas as medições entre 03:53 e 04:32.** Os números abaixo são só dessas rodadas.
- **Requisição:** `POST /completion` com prompt de 100 tokens (prompt_n = 100), `n_predict` 64, temperature 0, seed 42, `ignore_eos`, `cache_prompt:false` e streaming.
  - TTFT é o tempo do POST até o primeiro token, medido no cliente.
  - Geração é o `predicted_per_second` do llama-server, que bate com a medida no cliente.
- **llama-server:** `-c 2048 -np 1 -ngl 99 -t 4 --rpc …`. As 24 camadas e a saída ficam nos rpc-servers; só o `token_embd` fica no coordenador.
- **rpc-servers:** `-t 4 -c`.
- **`KMP_BLOCKTIME=0` em todos:** assim um servidor ocioso não deixa threads girando enquanto outro calcula, o que só acontece porque tudo divide a mesma CPU.
- **Texto gerado:** foi idêntico (SHA-256 `98ac662939b851ba…`) em todas as requisições, com qualquer atraso, banda e número de servidores.

## 1. O proxy (`C:\sgtmp\shard-exp\latencia\netem.py`)
É feito em asyncio, só com a biblioteca padrão. Atende vários enlaces (`--map nome:porta:host:porta_destino`) e tem um controle HTTP em 127.0.0.1:50073 (`/set?delay_ms=&bw_c2s=&bw_s2c=`, `/stats`, `/mark`). Assim o atraso muda entre requisições sem recarregar o modelo.

**Como funciona**
- **Atraso:** cada pedaço lido (até 256 KiB) é entregue na hora de chegada + D, nunca antes do pedaço anterior, o que preserva a ordem. O RTT é 2·D.
- **Temporização:**
  - TCP_NODELAY nos dois lados;
  - `timeBeginPeriod(1)`, sem estrangulamento de energia;
  - desconto de 1,1 ms por sentido para compensar o despertar atrasado do temporizador.
- **Aperto de mão TCP:** em conexão nova, o primeiro byte só chega ao servidor 1,5 RTT depois do connect.
- **Contrapressão:** acima de 32 MiB na fila.
- **Banda (opcional, por sentido):** em pedaços de 64 KiB.
- **Contagem:** bytes, leituras e mensagens por sentido.

**Decodificação** (lida no `ggml-rpc.cpp` b11721, protocolo 8.0.0)
- Cliente→servidor: `| cmd 1 B | tamanho uint64 | dados |`.
- Servidor→cliente: `| tamanho uint64 | dados |`, só para os 13 comandos que têm resposta, pareados em ordem com o pedido:
  - HELLO, DEVICE_COUNT, ALLOC_BUFFER, GET_ALLOC_SIZE, GET_ALIGNMENT, GET_MAX_SIZE, BUFFER_GET_BASE;
  - GET_TENSOR, GET_TENSOR_2D, SET_TENSOR_HASH, COPY_TENSOR, COMM_INIT, GET_DEVICE_MEMORY.
- O log guarda cada mensagem com o nome do tensor.
- O proxy corta a conexão se aparecer COMM_INIT, que faria o rpc-server escutar em 0.0.0.0:porta+1000. Isso nunca aconteceu, porque o comando só existe no modo `-sm tensor`.
- Não houve nenhum erro de decodificação em 364 conexões, e todas fecharam sem resposta pendente.

**Validação** (`selftest.py`, eco de 64 B, 40 medidas por ponto)

| RTT pedido (ms) | 0 | 20 | 50 | 100 | 150 |
|---|---|---|---|---|---|
| Medido: mediana (mín–máx) | 0,63 (0,61–0,87) | 20,04 (19,02–21,98) | 50,09 (49,14–51,99) | 100,08 (98,93–101,41) | 150,03 (148,95–152,21) |

- **Banda de 50 Mbit/s:** 10 MB levaram 1,673 s (esperado 1,65 s).
- **Pontualidade na matriz:** o atraso extra além do agendado foi de 0,53 a 0,60 ms em média, já compensado. Passou de 5 ms em só 5 de 57.210 entregas.
- **Custo do próprio proxy** (2 servidores, RTT 0, 6 rodadas):

| Configuração | Geração (t/s) |
|---|---|
| Local, sem `--rpc` | 16,71 |
| RPC direto | 15,88 |
| RPC pelo proxy | 15,40 |

O proxy soma cerca de 1 ms por ida e volta.

## 2. Velocidade × RTT × número de rpc-servers
Fiz 3 rodadas intercaladas, em ordem aleatória, de pedidos de 64 tokens. As 3 configurações ficaram carregadas ao mesmo tempo e só uma requisição rodava por vez.

| Servidores | RTT (ms) | Geração t/s: mediana (faixa) | ms/token | TTFT s: mediana (faixa) |
|---|---|---|---|---|
| 1 | 0 | 15,41 (15,30–15,61) | 64,9 | 1,916 (1,901–1,922) |
| 1 | 20 | 12,31 (12,17–12,37) | 81,2 | 1,928 (1,917–1,932) |
| 1 | 50 | 9,01 (8,99–9,11) | 111,0 | 1,946 (1,942–1,950) |
| 1 | 100 | 6,21 (6,18–6,26) | 160,9 | 1,993 (1,988–2,056) |
| 1 | 150 | 4,74 (4,70–4,75) | 211,2 | 2,057 (2,045–2,068) |
| 2 | 0 | 15,10 (15,01–15,20) | 66,2 | 1,940 (1,919–2,017) |
| 2 | 20 | 9,85 (9,67–9,90) | 101,5 | 1,950 (1,944–1,957) |
| 2 | 50 | 6,18 (6,13–6,18) | 161,8 | 2,009 (1,983–2,016) |
| 2 | 100 | 3,83 (3,81–3,83) | 261,2 | 2,111 (2,097–2,115) |
| 2 | 150 | 2,76 (2,75–2,76) | 362,2 | 2,207 (2,202–2,221) |
| 3 | 0 | 14,70 (13,95–14,80) | 68,0 | 1,979 (1,964–2,016) |
| 3 | 20 | 8,08 (8,02–8,08) | 123,8 | 1,981 (1,969–1,982) |
| 3 | 50 | 4,70 (4,69–4,71) | 212,7 | 2,068 (2,066–2,078) |
| 3 | 100 | 2,76 (2,74–2,77) | 362,7 | 2,233 (2,207–2,657) |
| 3 | 150 | 1,95 (1,95–1,95) | 513,0 | 2,366 (2,353–2,394) |

**Cômputo por token** (do pedido GET_TENSOR à resposta, mediana):

| Configuração | Cômputo por servidor (ms) | Soma (ms) |
|---|---|---|
| 1 servidor | 57,3 | 57,3 |
| 2 servidores | 27,2 + 30,8 | 58,0 |
| 3 servidores | 19,0 + 17,0 + 23,0 | 59,0 |

- O cômputo não cai com N, só se divide entre as máquinas.
- O coordenador gasta 0,8 a 1,9 ms entre a resposta do último servidor e o próximo token.
- O prompt de 100 tokens custa cerca de 1,9 s de cômputo somado nos servidores.

## 3. Por token: idas e voltas, bytes, comandos e a fórmula
**Idas e voltas:** há exatamente 1 por rpc-server por token, que é a resposta do GET_TENSOR. O padrão se repetiu em 915 de 915 passos de cada servidor. Os comandos SET_TENSOR e GRAPH_RECOMPUTE não esperam resposta. Os dados das ativações passam sempre pelo coordenador, numa topologia em estrela:
- `cpy_tensor` entre servidores diferentes cai para uma cópia via host;
- `cpy_tensor_async` é NULL.

**Bytes por token** (decodificação, medidos pelo proxy):

| Posição | Cliente→servidor | Servidor→cliente | Comandos por token |
|---|---|---|---|
| Único (1 servidor) | 14.464 B | 607.752 B (logits) | 9 SET_TENSOR, 1 GRAPH_RECOMPUTE, 1 GET_TENSOR |
| Primeiro | 14.146 B | 3.592 B | 8 SET_TENSOR, 1 GRAPH_RECOMPUTE, 1 GET_TENSOR |
| Meio | 6.028 B | 3.592 B | 5 SET_TENSOR, 1 GRAPH_RECOMPUTE, 1 GET_TENSOR |
| Último | 6.346 B | 607.752 B (logits) | 6 SET_TENSOR, 1 GRAPH_RECOMPUTE, 1 GET_TENSOR |

**O que vai em cada SET_TENSOR:**
- **Primeiro servidor:** `inp_mixed_embd`, `node_1` e `embd`, três tensores de 4·h B, ou seja, o embedding chega três vezes. Também vão `inp_mixed_slots` (8 B), `inp_pos` (4 B), `attn_inp_k_idxs` e `attn_inp_v_idxs` (8 B cada) e `attn_inp_kq_mask` (512 B = 256 posições × F16).
- **Demais servidores:** a saída da fatia anterior (`l_out-N`, 3.584 B) mais `inp_pos`, `k_idxs`, `v_idxs` e a máscara.
- **Último servidor:** também `out_ids` (4 B).

**Custos fixos de enquadramento:**
- cada SET_TENSOR tem 314 B de cabeçalho (9 + `rpc_tensor` de 296 + 1 + 8);
- GRAPH_RECOMPUTE ocupa 21 B;
- o pedido GET_TENSOR ocupa 321 B.

Em geral, por token:
- o primeiro servidor recebe 12h + 2·n_kv + 2.882 B;
- os demais recebem 4h + 2·n_kv + 1.932 B (o último, mais 318 B);
- cada servidor devolve 4h + 8 B, e o último devolve 4·vocab + 8 B se a saída estiver nele.

**Primeiro token e prompt:**
- No primeiro token depois do prompt, cada servidor recebe o grafo inteiro em GRAPH_COMPUTE no lugar do GRAPH_RECOMPUTE: de 105 a 353 KB, cerca de 430 B por nó. O envio é assíncrono, sem ida e volta extra.
- No prompt de 100 tokens, o primeiro servidor recebe 1.132.054 B (3 × 358.400 + máscara de 51.200). Cada servidor devolve 358.408 B, e o último só os logits do último token.

**Fórmula sugerida pelos dados:**

```
t_token ≈ C + Σ_i (RTT_i + o) + Σ_i (B↓_i / D_i + B↑_i / U_i)      tokens/s = 1000 / t_token(ms)
TTFT    ≈ P + Σ_i (RTT_i + o) + (bytes do prompt por salto) / banda   [prompt ≤ 512 tokens = 1 ubatch]
B pedidos no mesmo passo:  vazão ≈ B / (C(B) + Σ_i (RTT_i + o) + bytes(B)/banda)
```

Os termos:
- C é o cômputo somado de todas as fatias;
- o é o custo fixo por servidor (1 a 3 ms aqui, sendo cerca de 1 ms do proxy);
- B↓ e B↑ são os bytes da tabela acima;
- D e U são a banda de descida e de subida de cada casa;
- P é o cômputo do prompt.

**Qualidade dos ajustes:**
- **Ajuste global** (45 rodadas): ms/token = 60,5 + 1,91·N + 0,990·N·RTT, com R² = 0,9998 e resíduo máximo de 5,5 ms.
- **Ajuste só em RTT 0** (C = 63,3 ms, o = 1,56 ms): prevê os outros 12 pontos com erro de até 4,6%, e o medido sai sempre de 1 a 5% abaixo do previsto.
- **TTFT:** ≈ 1,87 s + 0,03·N s + 0,99·N·RTT, com R² de 0,81 porque o cômputo do prompt varia cerca de ±20 ms.

**Conferência do termo de banda** (2 servidores, RTT 50; a previsão usa 162 ms medidos sem limite mais os bytes):

| Saída (`output.weight`) | Descida / subida (Mbit/s) | Medido ms/token (t/s) | Previsto (ms) |
|---|---|---|---|
| Último rpc-server | sem limite | 172,7 (5,81)* | 162 |
| Último rpc-server | 300 / 300 | 179,1 (5,58) | 179 |
| Último rpc-server | 50 / 50 | 263,3 (3,80) | 263 |
| Último rpc-server | 50 / 10 | 654,0 (1,53) | 654 |
| Coordenador (`-ot`) | sem limite | 161,9 (6,18) | 162 |
| Coordenador (`-ot`) | 50 / 50 | 165,0 (6,06) | 166 |
| Coordenador (`-ot`) | 50 / 10 | 170,4 (5,87) | 171 |

\* Uma das duas rodadas saiu a 5,53 t/s; a matriz deu 161,8 ms nas mesmas condições. Com a banda limitada a 50/50, o TTFT subiu de 2,0 para 2,45 s, porque o prompt leva 1,5 MB de ida e 1,0 MB de volta.

**Pedidos simultâneos** (2 servidores, `-np 4`). Com 1 ou 4 pedidos foram 128 respostas, isto é, 64 passos × 2 servidores; o RTT é pago por passo, não por pedido.

| RTT (ms) | Simultâneos | t/s por pedido | ms por passo | t/s somado |
|---|---|---|---|---|
| 0 | 1 | 14,81 | 66,2 | 14,81 |
| 0 | 4 | 6,63 | 149,5 | 24,83 |
| 50 | 1 | 6,15 | 161,9 | 6,15 |
| 50 | 4 | 4,01 | 244,3 | 15,55 |
| 100 | 1 | 3,78 | 263,2 | 3,78 |
| 100 | 4 | 2,90 | 344,0 | 11,59 |

## 4. Carga do modelo
**Medido** (2 servidores, pelo proxy; os bytes vão do coordenador aos servidores):

| RTT (ms) | Banda | Cache | Até /health (s) | Bytes enviados |
|---|---|---|---|---|
| 0 | sem limite | vazio | 4,86 | 392.561.500 |
| 0 | sem limite | cheio | 3,90 | 247.918.114 |
| 50 | sem limite | vazio | 13,16 | 392.561.500 |
| 50 | sem limite | cheio | 12,03 | 247.918.114 |
| 100 | sem limite | vazio | 21,26 | 392.561.500 |
| 100 | sem limite | cheio | 25,76 (pausa de 6,6 s sem tráfego, não explicada) | 247.918.114 |
| 100 | sem limite, `-fit off` | vazio / cheio | 17,68 / 16,48 | ≈ igual |
| 50 | 300 Mbit/s | vazio / cheio | 19,55 / 16,15 | ≈ igual |
| 50 | 50 Mbit/s | vazio / cheio | 68,15 / 45,24 | ≈ igual |

**O que cada carga envia:**
- **Sem cache:** os bytes são os pesos que estão nos rpc-servers mais cerca de 0,1% de comandos.
- **Com cache cheio:** só o `output.weight` (144.643.072 B) passa de 10 MiB (`HASH_THRESHOLD`), e foi trocado por um SET_TENSOR_HASH.
- **Idas e voltas:** cada carga teve 123 respostas síncronas e 34 conexões (cerca de 78 RTT por servidor). Com `-fit off` foram 102 respostas e 29 conexões.
- **Vazão real:** com o limite de 50 Mbit/s por enlace, a vazão total ficou em cerca de 51 Mbit/s, porque os envios saíram praticamente em sequência.

**Modelo de tempo:** t ≈ 4,9 s + (cerca de 165 RTT para 2 servidores) + bytes/banda. A soma simples superestima de 10 a 20%: dá 76 s contra 68 s medidos, porque parte das esperas se sobrepõe à transferência.

**Extrapolação** (estimada, com o envio saindo de um único coordenador):

| Modelo | Sem cache, 50 Mbit/s | Sem cache, 300 Mbit/s | Cache cheio (est. ~2% dos bytes), 50 Mbit/s | Cache cheio, 300 Mbit/s |
|---|---|---|---|---|
| 40 GB | 1 h 47 min | 17,8 min | ~2 min | ~20 s |
| 200 GB | 8 h 53 min | 1 h 29 min | ~11 min | ~1,8 min |

A esses tempos é preciso somar:
- cerca de 80 RTT por servidor;
- 1 RTT por tensor acima de 10 MiB. São cerca de 401 num 70B, com ou sem cache, porque o cliente sempre tenta o hash antes.
- a leitura do arquivo inteiro e o hash FNV-1a no coordenador a cada carga (não medi).

O coordenador precisa do GGUF inteiro no disco, e um arquivo de 200 GB não cabe nos ~88 GB livres da VPS. O cache só fica cheio depois de uma primeira transferência completa.

## 5. Extrapolação para um 70B (estimativa; script `est70b_v2.py`)
**Bytes por token** (formato Llama-3-70B: 80 camadas, hidden 8.192, vocab 128.256, Q4_K_M de cerca de 42,5 GB):
- **Ativação por salto:** 8.192 × 4 B = **32.768 B por token em cada sentido** (servidor→coordenador e coordenador→próximo servidor).
- **Primeiro servidor:** recebe cerca de 103 KB por token, por causa das 3 cópias do embedding com n_kv = 1.024.
- **Logits:** 513 KB por token, se a saída ficar num servidor.
- **Prompt de 1.000 tokens:** 32,8 MB por salto, o que dá 5,2 s por salto a 50 Mbit/s de subida.

**Velocidade de geração estimada** (t/s):

| PCs com | Cômputo C por token | N | RTT 30 ms | RTT 50 ms | RTT 80 ms |
|---|---|---|---|---|---|
| CPU, 35 GB/s efetivos | 1,20 s | 4 | 0,70 | 0,66 | 0,61 |
| CPU, 35 GB/s efetivos | 1,20 s | 8 | 0,63 | 0,57 | 0,50 |
| GPU, 350 GB/s efetivos | 0,12 s | 4 | 2,85 | 2,32 | 1,81 |
| GPU, 350 GB/s efetivos | 0,12 s | 8 | 1,98 | 1,51 | 1,11 |
| Como esta máquina, 6,9 GB/s | 6,1 s | 4 ou 8 | 0,15–0,16 | 0,15–0,16 | 0,15–0,16 |

**Hipóteses da tabela:**
- C = bytes de pesos ÷ banda de memória efetiva. A decodificação é limitada pela memória, como medido aqui: 392 MB em ~57 ms.
- o = 2 ms por servidor.
- Casas com 300 Mbit/s de descida e 50 de subida.
- Saída no último PC, o que custa +98 ms. Com `-ot` num coordenador de 10 GB/s, o custo seria +86 ms e o resultado é praticamente o mesmo.
- 1 RTT por servidor por token, sem perda, sem jitter e sem slow start.

**Leitura da tabela:**
- Com CPUs, o gargalo é a memória: a rede representa de 9 a 33% do tempo.
- Com GPUs, a rede passa a dominar: com 8 GPUs a 80 ms, são 656 dos 904 ms por token.
- Cada PC precisa de 10,6 GB (N = 4) ou 5,3 GB (N = 8) só para os pesos.
- **Medido versus estimado:**
  - É medido o mecanismo: 1 RTT por servidor, 4·h bytes por salto, os logits e o termo de banda.
  - É estimado tudo o que se refere ao 70B: tamanhos, banda de memória, banda das casas e a ausência de efeitos de TCP.

## Não confirmado
- **Internet real:** o proxy só emula atraso fixo e banda. Não emula jitter, perda, nem o controle de congestionamento do TCP.
  - No Linux, o padrão `tcp_slow_start_after_idle=1` reinicia a janela depois de ociosidade maior que o RTO.
  - Mensagens acima de ~14 KB, como os 32 KiB de um 70B ou os logits, poderiam então precisar de 1 RTT extra ou mais.
  - Nada disso foi testado.
- **Máquinas separadas:** tudo rodou na mesma CPU disputada. O cômputo de 57 a 59 ms por token é desta máquina.
- **NAT:** o llama-server precisa abrir uma conexão TCP até cada rpc-server, o que atrás de NAT exige um relé. O proxy faz o papel de relé, mas a distância extra até um relé real não foi medida.
- **70B, 40 GB e 200 GB:** são só cálculo, porque não há modelo nem RAM aqui e não era permitido baixar.
  - A estrutura de entradas (3 cópias do embedding, máscara, índices) foi medida no Qwen2.5 neste build; outros modelos podem diferir.
  - A fração de ~2% de tensores pequenos depende da arquitetura.
- **Itens não medidos:**
  - prompt maior que 512 tokens (mais de um ubatch);
  - o paralelismo de pipeline (o build aloca as cópias `#0` a `#3` das entradas);
  - a velocidade do hash FNV-1a no coordenador;
  - o tempo de leitura de 40 ou 200 GB do disco.
- **Banda compartilhada:** a banda foi limitada por enlace, não num enlace comum. Na carga isso equivaleu a um enlace só, porque os envios foram sequenciais; com tráfego simultâneo seria otimista.
- **Pedidos simultâneos:** só testei com 2 servidores, 4 pedidos e RTT de 0, 50 e 100. Em GPU, o cômputo por passo C(B) deve crescer menos que aqui (de 66 para 149 ms).
- **Ruído:**
  - na carga com cache e RTT 100 houve uma pausa de 6,6 s sem tráfego, que não investiguei;
  - na execução interrompida das 01:30, o TTFT com 3 servidores foi 1 s maior, e isso não se repetiu;
  - às 04:19 um processo que não era meu (PID 13644) escutou em 127.0.0.1:8871. O bench se recusou a usar a porta e refiz essas fases.
- **Integridade do cache:** pelo código, o rpc-server não confere o conteúdo do arquivo de cache contra o hash, só o tamanho. Não testei.

## Encerramento
- **Processos:** o bench encerra todos os processos que inicia ao sair. Às 04:35:51:
  - `tasklist` mostra 0 `ggml-rpc-server.exe` e 0 `llama-server.exe`;
  - não resta nenhum Python meu;
  - nada escuta em 50061–50079 nem em 8871–8879.
- **Processos de terceiros:** não mexi no minerador, no Ollama, nem nos processos de outros agentes.
- **Projeto:** não alterei nenhum arquivo do projeto e não fiz commit.
- **Downloads:** só baixei código-fonte (`ggml-rpc.cpp` da tag b11721).
- **Disco:** ficaram cerca de 2,1 GB em `C:\sgtmp\shard-exp\latencia\`, dos quais 2,0 GB são 14 cópias do cache do `output.weight`. Podem ser apagadas.
- **Scripts:** em `C:\sgtmp\shard-exp\latencia\`: `netem.py`, `selftest.py`, `bench.py`, `analyze.py`, `analyze2.py`, `analyze3.py`, `est70b_v2.py`.
- **Dados:** em `C:\sgtmp\shard-exp\latencia\logs\`: `matrix-035352.jsonl`, `netem-events-matrix-035352.jsonl`, `load-040738.jsonl`, `load-041325.jsonl`, `load-041532.jsonl`, `direct-041622.jsonl`, `bw-042137.jsonl`, `batch-042807.jsonl`, `selftest.json`.