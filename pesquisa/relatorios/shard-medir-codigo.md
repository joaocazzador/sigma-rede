I have everything I need from the source. No binaries were run and I started no processes, so there is nothing to clean up. Here is the report.

---

# Relatório — leitura do código-fonte do backend RPC do llama.cpp

**Fonte:** repositório `ggml-org/llama.cpp`, tag **`b11721`** (abriu normalmente; commit `ff5a233`, 2026-10-18), lida via `raw.githubusercontent.com`. Cópias locais só-leitura em `C:\sgtmp\shard-exp\codigo\fonte\`. Nenhum binário executado; nenhum processo iniciado.

Aviso do próprio projeto (`tools/rpc/README.md:3-5`): *"the functionality is fragile and insecure. Never run the RPC server on an open network or in a sensitive environment!"*

---

## Fatos (com citação)

### 1. Protocolo: comandos, enquadramento, HELLO/versão

**Versão.** `ggml/include/ggml-rpc.h:9-15`:
```c
#define RPC_PROTO_MAJOR_VERSION    8
#define RPC_PROTO_MINOR_VERSION    0
#define RPC_PROTO_PATCH_VERSION    0
static_assert(GGML_OP_COUNT == 101, "GGML_OP_COUNT has changed - update RPC_PROTO_PATCH_VERSION");
```
A versão real nesta tag é **8.0.0** (o exemplo `v3.0.0` no README é saída de amostra desatualizada). O `static_assert` amarra o protocolo ao conjunto de 101 operações do ggml compilado.

**Lista de comandos** — `enum rpc_cmd` (`ggml-rpc.cpp:57-83`), 23 comandos ativos:
```c
RPC_CMD_ALLOC_BUFFER=0, GET_ALIGNMENT, GET_MAX_SIZE, BUFFER_GET_BASE, FREE_BUFFER,
BUFFER_CLEAR, SET_TENSOR, SET_TENSOR_HASH, GET_TENSOR, COPY_TENSOR, GRAPH_COMPUTE,
GET_DEVICE_MEMORY, INIT_TENSOR, GET_ALLOC_SIZE, HELLO, DEVICE_COUNT, GRAPH_RECOMPUTE,
MEMSET_TENSOR, SET_TENSOR_2D, GET_TENSOR_2D, COMM_INIT, COMM_ALLREDUCE, COMM_FREE, NONE
```
Função de alocação/leitura/escrita de buffers, mais `GRAPH_COMPUTE` (envia o grafo inteiro e manda computar), `GRAPH_RECOMPUTE` (recomputa um grafo já em cache por `uid`), e o trio `COMM_*` (tensor-parallel entre 2 servidores — ver item 4).

**Enquadramento.** Requisição = `| cmd (1 byte) | request_size (8 bytes) | request_data |` (`send_rpc_cmd`, `ggml-rpc.cpp:372-386`). Resposta = `| response_size (8 bytes) | response_data |`. Tanto cliente quanto servidor **exigem que o tamanho recebido bata exatamente com o `sizeof` esperado** (`recv_msg`, linhas 333-342); mensagens de tamanho variável (set/get tensor, graph) usam o overload que faz `input.resize(size)` dentro de `try/catch(bad_alloc)` (344-356).

**HELLO/handshake.** Cliente: `negotiate_hello` (423-442) envia `conn_caps` e **recusa se `response.major != 8 || response.minor > 0`**. Servidor: `rpc_serve_client` (2441-2473) exige que o **primeiro** comando seja `HELLO` e que `hello_input_size == sizeof(rpc_msg_hello_req)`, senão fecha. Os `conn_caps` (24 bytes, `transport.h:11`) servem **só** para negociar upgrade de transporte RDMA (QPN/PSN/GID em `transport.cpp:588-649`); **não** são autenticação.

### 2. Validação no servidor (por comando)

Toda desserialização passa por `deserialize_tensor` (`ggml-rpc.cpp:1641-1695`), que confere:
- `type < GGML_TYPE_COUNT` e `blck_size != 0` (evita tipo inválido / divisão por zero);
- o `buffer` recebido tem que estar no conjunto `buffers` conhecido do servidor, senão vira `nullptr` (1666-1669);
- se há buffer e `nelements>0`, exige que `[data, data+ggml_nbytes)` caiba dentro de `[buffer_start, buffer_start+buffer_size)`, incluindo checagem de overflow `data+size < data` (1671-1684).

Por comando (o que é conferido):
- **alloc_buffer / get_alignment / get_max_size / get_device_memory / get_alloc_size:** só validam `device < backends.size()` (ex. 1463, 1503).
- **buffer_get_base / free_buffer / buffer_clear:** validam que `remote_ptr` está em `buffers` (1551, 1564, 1585). `free_buffer` descarta todos os grafos em cache para evitar use-after-free (1571-1575).
- **set_tensor** (1698-1754): valida header mínimo, desserializa, e **re-confere** `data+offset` dentro do buffer e `size <= p1-data-offset` (1730-1739).
- **set_tensor_2d** (1756-1816): valida `n_copies!=0`, `size!=0`, `size*n_copies==data_size`, overflow de `(n_copies-1)*stride+size`, e o span dentro do buffer **e** dentro de `ggml_nbytes` (1771, 1794-1811).
- **get_tensor / get_tensor_2d** (1922-2007): mesma checagem de limites do buffer e (no 2D) do `ggml_nbytes` (1946-1953, 1980-2002).
- **memset_tensor** (1593-1639): confere região contra `ggml_nbytes(tensor)` e contra os limites do buffer.
- **copy_tensor** (2009-2048): confere que `dst_data + ggml_nbytes(src) <= dst_base + dst_buf_sz` (2032). Repare: valida o **destino**, mas a extensão lida do `src` só é limitada pelo `ggml_nbytes(src)` calculado dos `ne/nb` que o próprio cliente mandou.
- **graph_compute** (2106-2196): confere que o buffer de entrada contém de fato `n_nodes` e `n_tensors` declarados (2109, 2125, 2133); reconstrói o grafo por `create_node` (2050-2104, recursivo sobre `src[]`/`view_src`, com memoização em `tensor_map` que impede recursão infinita em ciclos).

**Onde um cliente malicioso ainda pode ler/escrever fora ou travar (concreto):**

1. **`nb[]` (strides) são copiados verbatim** do cliente (`deserialize_tensor:1663-1665`) e **toda** a checagem de limites depende de `ggml_nbytes`, que é *calculado a partir desses mesmos `ne`/`nb`* (`ggml.c:1309-1329`: `nbytes += (ne[i]-1)*nb[i]`). A verificação só garante que *a extensão linear agregada daquele tensor* cabe no buffer — ela **não** garante que os `nb` sejam consistentes com o `ne`, nem com o que o kernel realmente endereça.
2. **Ops do tipo gather/scatter quebram a hipótese da checagem agregada.** Em `GET_ROWS` (`ggml-cpu/ops.cpp:5441-5451`) o endereço de `src0` é `i01*nb01 + i11*nb02 + i12*nb03`, onde os índices do laço vêm das dimensões de `dst`/`src1` e `i01` é lido do *conteúdo* de `src1`. Como cada tensor é validado isoladamente contra o seu próprio `ggml_nbytes`, um grafo com formas/strides **mutuamente inconsistentes** entre `src0`, `src1` e `dst` pode endereçar fora da extensão validada de `src0`.
3. **Mitigação parcial virou DoS.** Vários kernels têm `GGML_ASSERT` (ex. `ops.cpp:5447`: `GGML_ASSERT(i01>=0 && i01<ne01)`), mas `GGML_ASSERT` **não** é removido em release (`ggml.h:288` = `if(!(x)) GGML_ABORT(...)`). Logo um grafo forjado que viole esses asserts **aborta o processo do worker** (DoS), em vez de vazar memória. E qualquer grafo cujo compute não retorne `SUCCESS` cai no `GGML_ASSERT(status==GGML_STATUS_SUCCESS ...)` de `graph_compute:2192` → abort.
4. **`op_params` (64 bytes) são 100% controlados pelo cliente** (`deserialize_tensor:1688-1690`) e não há validação semântica por-op — parâmetros extremos (rope, pooling, etc.) podem causar laços gigantes ou aborts.
5. **`nb` é `uint32_t` no `rpc_tensor`** (`ggml-rpc.cpp:40-41`); o próprio código comenta (1049-1051) que strides acima de 4 GiB truncam no fio — fonte de inconsistência adicional.
6. **Esgotamento de memória:** `n_nodes`/`n_tensors` são `uint32_t`; `graph_compute` aloca `ggml_tensor_overhead()*(n_nodes+n_tensors)+ggml_graph_overhead_custom(n_nodes)` (2148). `recv_msg` faz `resize(size)` de um `uint64` (350) — protegido por `catch(bad_alloc)`, mas um tamanho grande **porém alocável** (poucos GB) esgota a RAM de uma VPS de 7 GB.

### 3. Cache de tensores

- **Hash:** `fnv_hash` = **FNV-1a de 64 bits** (`ggml-rpc.cpp:313-321`, base `0xcbf29ce484222325`, primo `0x100000001b3`). **Não é criptográfico.**
- **Quando o cliente manda hash em vez de dados:** `rpc_use_hash_cache` (844-846) → **somente** se `size > HASH_THRESHOLD` (**10 MiB**, linha 118) **e** `tensor->buffer->usage == GGML_BACKEND_BUFFER_USAGE_WEIGHTS`. Ou seja, só pesos grandes; ativações nunca entram no cache.
- **Nome do arquivo:** `snprintf(hash_str, 17, "%016" PRIx64, hash)` — 16 hex minúsculos, em `cache_dir/<hash>` (`set_tensor:1744-1747`, `get_cached_file:1822-1824`). `cache_dir` = `$LLAMA_CACHE` ou (Windows) `%LOCALAPPDATA%\llama.cpp\rpc`, só com a flag `-c`/`--cache` (`rpc-server.cpp:64-83, 242-252`).
- **O cliente PRECISA ler os dados para calcular o hash:** `fnv_hash((const uint8_t*)data, size)` (858) roda sobre os bytes que o cliente já tem em memória. **Portanto, no protocolo de fábrica, o "cabeça" (main host) precisa do arquivo inteiro** para sequer computar os hashes que enviará. O cache economiza *transferência pela rede*, não a posse do modelo pelo cabeça.
- **Na carga, o servidor NÃO re-verifica o hash do arquivo em cache:** `get_cached_file` (1818-1836) só lê o arquivo de nome `<hash>`; `set_tensor_hash` (1838-1883) depois faz apenas a checagem de limites do buffer e `ggml_backend_tensor_set`. Não há `assert fnv(conteúdo)==hash_pedido`.

**"Um trabalhador poderia preencher o próprio cache baixando só os seus tensores do HF por HTTP Range?"** — **Sim, com uma ressalva de arquitetura.** Nada no servidor impede: o worker pode, de antemão, baixar por `Range` os offsets GGUF dos tensores que lhe cabem, computar `fnv_hash` de cada um e gravar em `cache_dir/<hash>`. Quando o cabeça mandar `SET_TENSOR_HASH` (hash do *mesmo* conteúdo), dá **cache hit** e nenhum peso trafega. **A ressalva:** no protocolo atual quem decide o hash é o cabeça, computando-o sobre os bytes reais — ou seja, o cabeça ainda precisa possuir os pesos. Para o cabeça **não** ter o modelo inteiro, o app Sigma teria que mudar o esquema: publicar/pré-computar os hashes FNV-1a por tensor junto do GGUF e fazer o cabeça enviar `SET_TENSOR_HASH` a partir desses hashes conhecidos (sem ler os bytes), deixando cada worker buscar só a sua fatia do HF. O código do servidor aceitaria isso sem alteração; o que falta está no lado cliente.

### 4. Distribuição de camadas

- **Decisão de quem fica com o quê:** `llama-model.cpp:1634-1692`. Por padrão (`all_zero`, 1635) os pontos de corte são **proporcionais à memória livre de cada dispositivo** (`splits[i] = free`, 1651); `--tensor-split` sobrescreve com proporções do usuário (1654). Cada camada `il` é atribuída a **um** dispositivo por `std::upper_bound` sobre os cortes cumulativos (`get_layer_buft_list`, 1669-1679).
- **É pipeline (sequencial por camadas), não divisão de tensores**, no caminho RPC padrão: cada dispositivo recebe um **bloco contíguo** de camadas, na ordem dos dispositivos; dispositivos RPC são apenas anexados à lista. README confirma (83-84): *"distributes model weights and the KV cache across all available devices ... in proportion to each device's available memory"*. A camada de entrada fica sempre na CPU (1682-1683).
- **Divisão por linhas (tensor split real)** existe via `LLAMA_SPLIT_MODE_ROW`/`make_gpu_buft_list` (1199-1227), mas é para GPUs locais (split buffer type), **não** é o padrão do RPC.
- **Caminho tensor-parallel dedicado para RPC:** `COMM_INIT`/`COMM_ALLREDUCE`/`COMM_FREE` (2224-2411) fazem um **allreduce par-a-par direto entre dois servidores** (`world==2`, 2226), com os dados trocados **servidor↔servidor sem passar pelo cliente** (comentário 2967-2969). É paralelismo de tensores, mas restrito a exatamente 2 nós.
- **O que viaja a cada token (pipeline):** no corte entre dispositivos, o `ggml_backend_sched` insere cópias e transfere a **ativação/estado oculto** da fronteira via `SET_TENSOR`/`GET_TENSOR` assíncronos (grandeza ≈ `n_embd × n_tokens × sizeof(dtype)`). O **KV cache fica residente** em cada dispositivo (não retransmitido por token). O grafo em si é enviado uma vez e depois reusado por `uid` via `GRAPH_RECOMPUTE` (2198-2213). *Não instrumentei os bytes exatos por token.*

### 5. Autenticação / criptografia

**Nenhuma.** Busca por `auth|token|password|tls|ssl|encrypt|hmac|credential` em `ggml-rpc.cpp`, `transport.cpp`, `transport-apple.cpp` retornou vazio. O handshake HELLO só casa versão e negocia RDMA. O transporte é TCP em claro (`transport.cpp:666-688`); os `conn_caps` são parâmetros RDMA, não segredo.

**Amarrar a 127.0.0.1: sim, e é o padrão.** `rpc-server.cpp:95` default `host="127.0.0.1"`; `create_server` (`transport.cpp:724-744`) faz `inet_addr(host)` + `bind` naquele endereço exato, então liga **só** no loopback. Se `host != 127.0.0.1`, o `main` imprime o aviso em caixa (`rpc-server.cpp:224-232`). **Exceção importante:** o listener do caminho `COMM_INIT` (rank 0) faz `socket_t::create_server("0.0.0.0", request.port)` (`ggml-rpc.cpp:2237`) — **fixo em `0.0.0.0`**, na porta `port0+1000` (2993-3023), independentemente de o servidor principal estar em 127.0.0.1. Se o modo COMM for acionado, abre-se um socket em todas as interfaces.

### 6. Proteções sem recompilar o llama.cpp (só Python stdlib no app Sigma)

**(a) Proxy validador do protocolo (127.0.0.1:porta_pública → 127.0.0.1:porta_do_rpc_server).** Viável e é a defesa de maior alcance, porque o enquadramento é simples e determinístico (`cmd|size|data`). Um proxy em Python stdlib (`socket`/`socketserver`/`struct`) pode, **sem o ggml**:
- Exigir `HELLO` primeiro, com `major==8, minor==0` e `size==sizeof(rpc_msg_hello_req)`; rejeitar `cmd >= RPC_CMD_COUNT`.
- Impor **limites de tamanho** por mensagem e por sessão (mata o DoS de `resize` gigante) e **timeouts/limite de conexões** (mata o DoS de cliente único serial — ver Riscos).
- Manter um **modelo-sombra dos buffers**: observar as respostas de `ALLOC_BUFFER` (`remote_ptr`, `remote_size`) e `BUFFER_GET_BASE` (`base_ptr`) e, para cada `rpc_tensor` que chega, **refazer as mesmas checagens de limite** que o servidor faz — mas de forma mais estrita, recomputando `ggml_nbytes` a partir de `ne`/`nb` e recusando tensores cujo `buffer` não seja um `remote_ptr` conhecido ou cujo `[data,data+nbytes)` saia do `[base,base+size)`.
- **Bloquear comandos que o Sigma não usa** (ex.: `COMM_*`, e assim fechar o socket `0.0.0.0`), reduzindo superfície.

**O que fica de fora do proxy** (limites reais): o proxy **não** entende semântica de ops. Ele não consegue, com confiança, validar consistência **entre** tensores de um grafo (o vetor OOB de `GET_ROWS`/gather do item 2), porque isso exigiria replicar a lógica de cada um dos ~101 kernels e o endereçamento por `nb[]`. Também não vê o conteúdo de tensores-índice (que decidem acessos em runtime). Ou seja: o proxy fecha bem **tamanho, versão, comandos, limites por-tensor e DoS de enquadramento**, mas **não** fecha "grafo sintaticamente válido, semanticamente malicioso". Para esse resto, a defesa tem que ser o sandbox (b).

**(b) Sandbox do processo sem admin.**
- **Windows (o nosso caso):** dá para reduzir risco com `ctypes` + API Win32, **sem admin**:
  - **Job Object** (`CreateJobObject`/`AssignProcessToJobObject` + `JOBOBJECT_EXTENDED_LIMIT_INFORMATION`): teto de memória (`JOB_OBJECT_LIMIT_PROCESS_MEMORY`) e `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` — contém o DoS de RAM e garante que o worker morra junto com o supervisor.
  - **Token de integridade baixa:** criar o processo com um token ajustado para *Low integrity* (`SetTokenInformation`/`TOKEN_MANDATORY_LABEL`) limita escrita a recursos de integridade média/alta (mitiga o abort/corrupção escalando para o resto da conta).
  - **Restricted/AppContainer:** `CreateRestrictedToken` remove SIDs/privilégios; AppContainer (perfil via `CreateAppContainerProfile` + `STARTUPINFOEX`/`PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`) isola FS/rede, mas é bem mais complexo de montar por `ctypes` e pode quebrar o acesso do worker ao GGUF/porta — precisa teste.
  - Combinar com: rodar o worker em **conta de usuário dedicada e descartável**, `cache_dir` próprio e isolado, e **sem** a flag `-c` se o cache não for usado.
- **Linux:** `user namespaces` + `seccomp` (lista branca de syscalls) + `setrlimit` contenciona bem; dá para orquestrar com `unshare`/`prctl` via `ctypes`, ou `bwrap`/`nsjail` se permitido (mas isso é binário externo, fora do "só stdlib").
- **macOS:** `sandbox-exec` com perfil restritivo (depreciado, mas funciona sem admin).

**Quanto reduz o risco:** sandbox **não** impede a leitura/escrita OOB *dentro* do espaço de endereço do próprio worker (um grafo malicioso ainda pode vazar/corromper a memória daquele processo, inclusive os pesos e as ativações que passam por ele), mas **confina o estrago ao processo worker**: limita RAM (DoS), impede escalonamento para o resto da máquina e torna o abort inofensivo para o host. Com proxy (a) + sandbox (b) + conta descartável, o risco cai de "qualquer peer na rede pode travar/ler-escrever memória e potencialmente pivotar na máquina" para "um peer pode, no pior caso, corromper/derrubar o seu próprio processo de inferência e ver os dados que já passariam por ele de qualquer modo".

---

## Riscos concretos

1. **Sem autenticação nem criptografia (item 5).** Qualquer um que alcance a porta TCP fala o protocolo. A privacidade "pedido cifrado só para a máquina" do Sigma **não existe** no nível do RPC: o backend RPC vê e processa tudo em claro. As máquinas das pessoas estão atrás de NAT (bom), mas basta o nó/cabeça ser hostil, ou um túnel mal configurado, para expor o worker.
2. **DoS trivial por cliente único (serial).** `start_server` faz `accept()`+`rpc_serve_client()` em laço **single-thread** (`ggml-rpc.cpp:2817-2826`) com `listen(...,1)` (`transport.cpp:745`). Um cliente que conecta e não fala monopoliza o worker indefinidamente.
3. **DoS por abort.** `GGML_ASSERT`/`GGML_ABORT` estão ativos em release (`ggml.h:288`); grafos forjados que violem asserts de kernel ou o `status==SUCCESS` (`2192`) **derrubam o processo**. Índices fora de faixa em `GET_ROWS` (`ops.cpp:5447`) são um gatilho pronto.
4. **DoS por memória.** `graph_compute`/`recv_msg` alocam conforme campos `uint32/uint64` do cliente; tamanho grande-mas-alocável esgota os 7 GB da VPS.
5. **Leitura/escrita OOB na memória do worker (item 2).** `nb[]`/`op_params` verbatim + checagem só por-tensor permitem grafos semanticamente inconsistentes que endereçam fora da extensão validada; parte vira abort (DoS), parte pode ser OOB real em ops gather/scatter. Não comprovei exploração ativa (só leitura de código).
6. **Cache sem verificação de integridade (item 3).** `get_cached_file` não re-checa `fnv(conteúdo)==hash`. FNV-1a 64 bits não é resistente a colisão/segunda-preimagem. Quem escreve em `cache_dir` controla o mapa hash→bytes; combinado com a ausência de auth, é um vetor de **envenenamento de pesos** (resultados errados silenciosos).
7. **Listener `0.0.0.0` no COMM (item 5).** Se o modo tensor-parallel (`COMM_INIT`) for usado, abre socket em todas as interfaces na porta `base+1000`, furando o bind em 127.0.0.1.

---

## Mitigações possíveis e o que sobra

- **Fecha bem (proxy stdlib):** versão/HELLO, comandos permitidos, limites de tamanho por-mensagem e por-sessão, timeouts, nº de conexões, re-validação de limites por-tensor contra buffers conhecidos, bloqueio de `COMM_*`. Isso elimina riscos 1(parcial, no sentido de restringir quem entra), 2, 4 e 7, e reduz 5/6.
- **Fecha bem (sandbox + conta descartável + Job Object):** confina riscos 3, 4 e 5 ao processo worker; impede pivotagem; limita RAM.
- **Fecha bem (app Sigma):** manter o túnel cliente→worker **cifrado e autenticado por fora** (o Sigma já cifra os pedidos para a chave da máquina — o RPC deve rodar **apenas** dentro desse túnel, nunca em porta aberta); usar `cache_dir` isolado e, se quiser integridade, o Sigma computar/conferir SHA-256 dos tensores por fora (o `check_gguf` já citado na tarefa).
- **O que sobra (não dá para fechar só com proxy/sandbox):** grafo sintaticamente válido mas semanticamente malicioso contra a memória do **próprio** worker — inclusive ler as ativações/pesos que já transitam por ele. Num modelo onde pedaços do modelo rodam em máquinas de terceiros, **cada worker enxerga as ativações da sua fatia**; isso é inerente ao sharding por RPC e nenhum wrapper em Python resolve. A confidencialidade do *conteúdo do prompt* contra um worker hostil **não é alcançável** com esta pilha sem criptografia homomórfica/hardware (fora de escopo). Integridade de resultado idem: um worker malicioso pode devolver números errados; só detectável por redundância (recomputar a mesma fatia em 2+ máquinas e comparar) — mecanismo que o Sigma teria que construir.

---

## Não confirmado

- **Não executei binários** nem instrumentei: os bytes exatos trafegados por token, o custo real do abort, e a explorabilidade prática dos vetores OOB do item 2 são inferências da leitura de código, não medições.
- **Não li todos os ~101 kernels**; afirmo OOB concreto só onde vi o endereçamento (`GET_ROWS`, `ggml-cpu/ops.cpp:5441-5451`) e a cópia verbatim de `nb`/`op_params`. Outros kernels podem ter mais ou menos guardas.
- **AppContainer/token de baixa integridade via `ctypes`**: descrevi o mecanismo pela API Win32, mas **não testei** que o worker (abrir GGUF, abrir socket loopback, carregar as DLLs do llama) continua funcional sob essas restrições — precisa validação empírica na fase de implementação.
- **RDMA**: o pacote `win-cpu-x64` não traz RDMA (`GGML_RPC_RDMA` não definido), então `get_caps` devolve tudo-zero e o transporte é sempre TCP aqui; a análise de RDMA (`transport-apple.cpp`) não se aplica ao nosso binário.
- O comportamento de `comm_init` ligando em `0.0.0.0` foi lido no código (`ggml-rpc.cpp:2237`); **não** confirmei em execução se, na build Windows/CPU, o caminho COMM chega a ser acionado pelo `llama-server` padrão (ele exige exatamente 2 backends RPC e ausência de `GGML_RPC_NO_COMM`).