# Experimento básico com o ggml-rpc-server (llama.cpp b11721): agente "basico"

A divisão de um modelo entre vários ggml-rpc-server funciona e gera texto idêntico ao modo local. Cada camada fica inteira em uma máquina, na ordem do `--rpc`. Três pontos pesam no projeto:

- **Falhas derrubam tudo:** se qualquer rpc-server cai, o llama-server morre e não se reconecta sozinho.
- **Um cliente por vez:** cada rpc-server atende um único llama-server; um segundo fica parado sem limite de tempo.
- **Logits por token:** se a camada de saída fica numa máquina remota, ela devolve cerca de 594 KiB por token.

**Ambiente.**
- Windows 11, i7-3770 (4 núcleos e 8 threads, só AVX; o llama.cpp carregou `ggml-cpu-ivybridge.dll`), 16 GB de RAM.
- Binários copiados para `C:\sgtmp\shard-exp\basico\llama\`, com SHA-256 igual ao da pasta original, que não foi alterada.
- Modelo `C:\sgtmp\shard-exp\models\qwen2.5-0.5b.gguf`. Tudo em 127.0.0.1, portas 50061–50073 e 8871–8876.

**Ruído na CPU.** Durante quase todas as medições havia carga que não era minha:
- o minerador `Sigma (2).exe` do usuário (prioridade BelowNormal, iniciado às 00:31:07);
- um `python` de outro agente (prioridade Normal);
- até cerca de 00:30, o llama-server do Ollama usando uns 4 núcleos.

A CPU total ficou em 100%. Por isso intercalei as rodadas e informo mediana e faixa. Como tudo roda na mesma máquina, os tokens/s não representam máquinas separadas.

**Como medi.**
- Scripts e logs estão em `C:\sgtmp\shard-exp\basico\`. Os logs ficam em `logs\` (`srv-*.err.txt`, `rpc-*.err.txt`, `results.jsonl`, `proxy-*.json`).
- Os bytes e as idas-e-voltas foram contados com um proxy TCP meu (`proxy.py`, só biblioteca padrão) colocado entre o llama-server e os rpc-servers.

## Fatos medidos

### 1. Opções (`--help`)

**ggml-rpc-server.exe** (saída completa):
```
  -t, --threads N                  number of threads for the CPU device (default: 4)
  -d, --device <dev1,dev2,...>     comma-separated list of devices
  -H, --host HOST                  host to bind to (default: 127.0.0.1)
  -p, --port PORT                  port to bind to (default: 50052)
  -c, --cache                      enable local file cache
```
- O endereço padrão é **127.0.0.1:50052**. Muda-se com `-H` e `-p`.
- **Não há nenhuma opção de autenticação nem de TLS.** O banner mostra `transport : TCP` e `Starting RPC server v8.0.0`.
- Há dois textos dentro do binário: `WARNING: Host ('%s') is != '127.0.0.1'` e `Never expose the RPC server to an open network!`. Não os executei, porque as regras proíbem usar outro host.
- **`-c` é só uma chave liga/desliga, sem argumento.** O diretório vem de `LLAMA_CACHE` e fica em `%LLAMA_CACHE%\rpc`. O banner confirma: `local cache : C:\sgtmp\shard-exp\basico\cache\rpcA\rpc`.

**llama-server.exe** (opções relevantes):
- `--rpc SERVERS` (env `LLAMA_ARG_RPC`).
- `-ngl/--n-gpu-layers N` (padrão `auto`).
- `-sm/--split-mode {none,layer,row,tensor}`; `layer` é o padrão e `tensor` está marcado como EXPERIMENTAL.
- `-ts/--tensor-split N0,N1,...`, `-mg`, `-dev/--device`, `--list-devices`.
- `-ot/--override-tensor <padrão>=<buffer type>`, `-cmoe`, `-ncmoe N`, `-ncffn N`.
- `-fit [on|off]` (padrão on, margem de 1024 MiB por dispositivo).
- `-t N` (padrão -1), `-tb`.
- `--host` (padrão 127.0.0.1) e `--port` (o help diz padrão 9931).
- `--api-key`, que protege só o HTTP, não o RPC.
- Sem servidores RPC, `--list-devices` responde `(none)`.

### 2. Divisão básica com 2 rpc-servers: funciona e o texto é idêntico

Comandos:
```
set LLAMA_CACHE=C:\sgtmp\shard-exp\basico\cache\rpcA & ggml-rpc-server.exe -H 127.0.0.1 -p 50061 -t 4 -c
set LLAMA_CACHE=C:\sgtmp\shard-exp\basico\cache\rpcB & ggml-rpc-server.exe -H 127.0.0.1 -p 50062 -t 4 -c
llama-server.exe -m C:\sgtmp\shard-exp\models\qwen2.5-0.5b.gguf --host 127.0.0.1 --port 8871 -c 2048 -np 1 --no-webui --rpc 127.0.0.1:50061,127.0.0.1:50062 -ngl 99 -t 4 --log-timestamps
llama-server.exe (mesmo modelo) --port 8872 -c 2048 -np 1 --no-webui -ngl 0 -t 4 --log-timestamps     (referência sem --rpc)
POST /v1/chat/completions {"messages":[{"role":"user","content":"Explique em 5 frases curtas como funciona uma rede de computadores que divide um modelo de linguagem entre varias maquinas."}],"temperature":0,"seed":42,"max_tokens":128,"cache_prompt":false}
```

**Texto idêntico** (comparei o SHA-256 da resposta):

| Caso | SHA-256 (16 primeiros caracteres) | Execuções |
|---|---|---|
| 128 tokens | `4738dfb44a60e1a1` | 14 com RPC, 14 locais, 3 RPC com `-ot` |
| 256 tokens, outro prompt (seed 7) | `bae66d5972c9c38f` | 2 com RPC, 2 locais |
| 32 tokens, outras configurações | `20e40ea60aff6d12` | todas as de 2 e 3 servidores (seções 5 e 7) |

**Velocidade com 128 tokens** (prompt de 58 tokens, 14 rodadas intercaladas):

| Configuração | Geração: faixa (t/s) | Geração: mediana | Prompt: mediana |
|---|---|---|---|
| RPC, 2 servidores | 6,98–14,67 | **9,3** | 39,6 |
| Local, sem `--rpc` | 12,00–16,33 | **13,7** | 43,1 |
| RPC + `output.weight` na CPU local (`-ot`, 3 rodadas) | 12,13 / 16,79 / 20,22 | 16,8 | 43,4 |

- Com 256 tokens: RPC 8,49 e 7,97 t/s; local 14,95 e 13,65 t/s.
- No padrão, o RPC gerou cerca de 68% da velocidade local, nesta máquina e via loopback.

### 3. Onde ficou cada parte, memória e GGUF

Dados do log com `-v`:

| Dispositivo | Camadas | Pesos | KV cache | Buffer de cálculo |
|---|---|---|---|---|
| RPC0 = 127.0.0.1:50061 | 0–12 (13 camadas) | 129,37 MiB | 13 MiB | 64,55 MiB |
| RPC1 = 127.0.0.1:50062 | 13–23 + `output.weight` + `output_norm` ("layer 24") | 244,33 MiB | 11 MiB | 48,79 MiB |
| CPU do llama-server | `token_embd.weight` (q5_0), CPU_Mapped | 89,26 MiB | — | 25,57 MiB |

- O llama-server enxerga cada rpc-server pela RAM da máquina dele, por exemplo `RPC0 : 127.0.0.1:50061 (16269 MiB, 2753 MiB free)`.
- A divisão padrão segue a RAM livre de cada servidor. Aqui era igual nos dois, por isso ficou 13 para 12.
- Conferi os bytes com o meu leitor de GGUF: as camadas 0–12 somam 135.656.960 B, que dá exatamente 129,37 MiB.
- Dois detalhes estranhos no log:
  - as linhas de buffer chamam os dois servidores de `RPC0[endpoint]`, então é preciso identificá-los pelo endereço;
  - o log diz "offloading 23 repeating layers" e "offloaded 25/25", mas as linhas `assigned to device` mostram as 24 camadas no RPC.

**Memória de cada rpc-server** (working set / memória privada):

| Momento | RPC0 (50061) | RPC1 (50062) |
|---|---|---|
| Após a carga | 150,3 / 211,6 MiB | 264,5 / 309,5 MiB (pico 282,5 MiB) |
| Após gerar | 162,5 / 214,3 MiB | 275,5 / 311,9 MiB |

- O resumo de memória do llama-server bate com isso: RPC0 usa 206 MiB (129 de pesos + 13 de KV + 64 de cálculo) e RPC1 usa 311 MiB (244 + 11 + 55).
- Um rpc-server ocioso usa 6,4 / 1,6 MiB. Quando o cliente se desconecta, ele libera tudo e volta a cerca de 10 / 5 MiB.
- O llama-server que coordena o RPC usa 445–452 / 132–137 MiB; o working set é quase todo o mmap do GGUF. O llama-server local usa 478 / 175 MiB.

**O rpc-server não precisou do arquivo GGUF.**
- Ele não tem opção para receber um modelo e rodou numa pasta vazia (`rpcwd`).
- O contador de leitura de disco do RPC0 ficou em 0,01 MiB durante todas as cargas, enquanto ele guardava 129 MiB de pesos.
- O proxy mediu 136.042.176 B chegando a ele por TCP.
- O único arquivo que um rpc-server lê é o próprio cache: o RPC1 leu +137,94 MiB na carga com cache.
- Quem precisa do GGUF inteiro é o llama-server coordenador.

### 4. Cache

**O que aparece no diretório.** Depois da primeira carga:
- rpcA ficou com 0 arquivos;
- rpcB ficou com 1 arquivo, `c5d93d2d0af14837`, de 144.643.072 B. É o `output.weight` (Q8_0, 151936×896). O log diz `[set_tensor] saved to '...\cache\rpcB\rpc\c5d93d2d0af14837'`.

**Nome dos arquivos.**
- O nome é o hash FNV-1a de 64 bits dos bytes do tensor, em 16 caracteres hexadecimais, sem extensão.
- Calculei esse hash sobre a fatia do GGUF e deu `c5d93d2d0af14837`. O SHA-256 do arquivo de cache é igual ao da fatia (`4962347a…`): o conteúdo são os bytes brutos do tensor.
- O mesmo nome apareceu nos 9 diretórios que receberam o arquivo, ou seja, o nome depende só do conteúdo.
- Num modelo de teste confirmei o mesmo para `blk.0.ffn_up.weight`, que virou `1d2c75695057acf5`.

**Só tensores grandes vão para o cache.** Gerei localmente um modelo de teste com precisões misturadas (`models\mix.gguf`, feito com `llama-quantize` e `--tensor-type`) para descobrir o limite:

| Tensor | Tamanho | Foi para o cache? |
|---|---|---|
| ffn_down Q6_K | 3,41 MiB | não |
| ffn_gate F16 | 8,31 MiB | não |
| ffn_up F32 | 16,62 MiB | sim (13 + 11 arquivos) |
| output | 137,94 MiB | sim |

O limite fica entre 8,31 e 16,62 MiB.

- Sem `-c` nenhum arquivo é gravado, mesmo com `LLAMA_CACHE` definido.
- Os diretórios padrão `%LOCALAPPDATA%\llama.cpp` e `%USERPROFILE%\.cache\llama.cpp` não foram criados.

**Bytes transmitidos** (contados pelo proxy):

| Carga | Para RPC0 | Para RPC1 |
|---|---|---|
| Sem cache ainda | 136.042.176 B | 256.519.416 B |
| Com cache | 136.042.176 B | 111.876.030 B |

A carga com cache economiza 144,6 MB, cerca de 37% dos bytes deste modelo. Cada inicialização do llama-server abre de 14 a 18 conexões TCP por servidor (a maioria do passo `--fit`) e mantém 1 conexão persistente.

**Tempo da segunda carga** (3 rodadas, com `-lv 4`, sem proxy):

| Caso | Envio dos tensores (s) | Até /health (s) |
|---|---|---|
| Sem cache ainda | 1,43 / 1,48 / 1,45 | 3,44 / 3,52 / 3,63 |
| Com cache | 1,12 / 1,18 / 1,21 | 3,13 / 3,36 / 3,55 |
| Sem `-c` | 1,07 / 1,06 | 3,23 / 3,53 |

- No modelo de teste (941 MiB via RPC): 4,31 s sem cache contra 3,71 e 3,69 s com cache.
- Ou seja: a segunda carga é um pouco mais rápida, mas no loopback não ganha de rodar sem cache. A vantagem real é transmitir menos bytes.

### 5. `--tensor-split`, `-ngl` e `-ot`

- **`--tensor-split 1,3` funciona:** camadas 0–6 no RPC0 (69,32 MiB) e 7–24 no RPC1 (304,39 MiB), ou seja, 7/25 = 28%. A divisão é feita por camada inteira.
- **`-ngl 12` deixa camadas na CPU local:** camadas 0–12 na CPU (KV 13 MiB na CPU), 13–18 no RPC0 e 19–23 mais a saída no RPC1. O log diz "offloading output layer… 11 repeating layers… 12/25", ou seja, a camada de saída conta dentro do `-ngl` e é a primeira a sair da CPU.
- **Para manter só a saída local, use `-ot ^output\.weight$=CPU`.** Sem as âncoras `^` e `$`, o padrão `output\.weight=CPU` também pega os 24 `blk.N.attn_output.weight`, que vão para a CPU. Com as âncoras fica correto: CPU com 227,20 MiB e RPC1 com 106,39 MiB.

### 6. Falhas

| Cenário | O que aconteceu |
|---|---|
| Matei o RPC1 ou o RPC0 no meio de uma geração em streaming | Chegaram mais 2 ou 3 pedaços da resposta. Em cerca de 1 s o log mostra `E send failed (bytes_sent=0, size_to_send=1)` (ou `recv failed (bytes_recv=0, size_to_recv=8)`) seguido de `ggml-rpc.cpp:685: Remote RPC server crashed or returned malformed response`. O **processo llama-server morre**: o cliente HTTP recebe `ConnectionResetError` cerca de 4,5 a 5 s depois, sem JSON de erro. O outro rpc-server sobrevive e libera a memória. |
| Matei um rpc-server com o llama-server ocioso e depois reiniciei o rpc-server | O llama-server continua vivo e o `/health` segue respondendo 200. No pedido seguinte ele morre com o mesmo erro, **mesmo com o rpc-server já reiniciado**. Ele não se reconecta: é preciso reiniciá-lo e recarregar tudo. |
| Iniciei o llama-server com um endereço RPC fora do ar | Ele aborta com `ggml-rpc.cpp:650: Failed to connect to 127.0.0.1:50069`. |
| Um segundo llama-server tentou usar um rpc-server já ocupado | **O rpc-server atende um cliente por vez.** O segundo ficou 6 min 41 s com a conexão TCP estabelecida e o log vazio, sem limite de tempo. Ele só carregou (em 2,5 s) depois que matei o primeiro. |

### 7. Três rpc-servers

- **Funciona.** Com `--rpc 50061,50062,50063`: camadas 0–8 no 50061 (90,08 MiB), 9–16 no 50062 (78,59 MiB) e 17–24 no 50063 (205,04 MiB).
- **A ordem do `--rpc` define a ordem das camadas.** Com `--rpc 50063,50061,50062`, as camadas 0–8 foram para o 50063, as 9–16 para o 50061 e as 17–24 para o 50062.
  - O resumo de memória do log confirma isso.
  - A memória privada dos processos também: o 50063 caiu de 266,9 para 169,1 MiB e o 50062 subiu de 140,7 para 266,9 MiB.
  - O texto gerado foi o mesmo nas duas ordens.
- Cuidado: as linhas "model buffer size" vêm ordenadas pelo endereço, não pela ordem do `--rpc`.

### Extra: tráfego e idas-e-voltas por token

Medido pelo proxy, como média de 64 tokens (a diferença entre um pedido de 65 tokens e um de 1 token):

| Fluxo por token | Padrão | Com `-ot ^output\.weight$=CPU` |
|---|---|---|
| llama-server → RPC0 | 13,9 KB | 13,9 KB |
| RPC0 → llama-server | 3,6 KB | 3,6 KB |
| llama-server → RPC1 | 6,1 KB | 6,1 KB |
| RPC1 → llama-server | **608,0 KB** (logits: 151.936 × 4 B) | **3,6 KB** |
| Idas-e-voltas síncronas, por servidor | cerca de 1 | cerca de 1 |

- O prompt de 58 tokens soma mais 5 idas-e-voltas por servidor. As ativações do prompt somam 208 KB (58 × 896 × 4 B).
- Passando pelo proxy em Python, a geração caiu para 6,04 t/s no padrão, contra 16,51 t/s com `-ot`.

### Encerramento

Encerrei todos os processos que iniciei: rpc-servers, llama-servers e os proxies em Python. O `tasklist` mostra 0 `ggml-rpc-server.exe` e 0 `llama-server.exe`, e nada escuta em 50061–50079 ou 8871–8879. Não mexi nos processos do Ollama nem do Sigma; o llama-server do Ollama já tinha saído sozinho. Ficaram em disco, em `C:\sgtmp\shard-exp\basico\`, cerca de 1,86 GB de caches e o `models\mix.gguf` (1.137.616.736 B). Não apaguei nada.

## Não confirmado

- **Desempenho em máquinas separadas:** latência real, banda real e velocidade na internet não foram testadas, só loopback numa CPU disputada. Por exemplo, 2 servidores com 50 ms de RTT cada somariam pelo menos 100 ms por token só de latência; isso é estimativa minha, não medição.
- **Por que o RPC padrão gera mais devagar que o local**, e por que com a saída local ele às vezes superou o local: os logits de 608 KB por token são só uma hipótese. Tudo roda na mesma CPU, então não serve de conclusão.
- **Limite exato do cache:** medi que fica entre 8,31 e 16,62 MiB; os 10 MiB são palpite.
- **Tráfego do modelo de teste com cache:** não medi; pelo cálculo, cerca de 424 MB em vez de 987 MB.
- **Integridade do cache:** não testei se o servidor confere um arquivo de cache corrompido ou adulterado.
- **Diretório padrão do cache no Windows sem `LLAMA_CACHE`:** não testei.
- **Versões diferentes entre cliente e servidor:** não testei. O texto `RPC server version mismatch` existe na DLL, e o handshake mostrou `version: 8.0.0`.
- **Escutar fora de 127.0.0.1:** não executei, por regra.
- **Idas-e-voltas com 3 servidores** (a expectativa é 1 por servidor) e matar um servidor durante a carga do modelo: não medi.
- **Recursos não testados:** `-sm row` e `-sm tensor` com RPC, e os textos de comunicação direta entre servidores na DLL (`GGML_RPC_NO_COMM`, `failed to connect to peer`, `allreduce`).
- **Comportamento anômalo da porta, não investigado:** por um erro meu, subi um segundo llama-server na porta 8871 já ocupada. O log dele disse `listening on http://127.0.0.1:8871`, mas o sistema só mostrava o processo antigo escutando. O teste de falha contaminado foi refeito do zero.