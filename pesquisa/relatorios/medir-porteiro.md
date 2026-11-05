Tudo medido e encerrado. Segue o relatório.

---

# PORTEIRO POR MODELOS DE GRAFO — relatório (agente "porteiro")

**Veredito:** funciona e é a defesa certa para a ameaça **T3** (proteger o PC de quem empresta memória). Um proxy em Python stdlib entre a rede e o `ggml-rpc-server` local: (a) deixa o coordenador legítimo passar com **texto idêntico**; (b) **recusou os 13 grafos/comandos forjados ANTES de chegarem ao rpc-server**, que sobreviveu a todos. Custo ~6% de t/s no loopback (desprezível na internet). **Não cobre T1/T2/T4/T5/T6** — isso exige a redundância/consenso (os "3 coordenadores"), que é outra peça.

## Protótipo (em `C:\sgtmp\shard2\porteiro\`, nada do projeto foi alterado)
- `rpcwire.py` — decodifica o protocolo ggml-rpc 8.0.0, `ggml_nbytes` idêntico ao `ggml.c:1309`, normalização de grafo.
- `proxy.py` — **o porteiro** (modos `capture` e `gate`): HELLO-primeiro, allowlist de comandos, tetos de tamanho, modelo-sombra de buffers e re-checagem de limites por tensor, allowlist de tipos/ops e validação do grafo contra os modelos aprendidos.
- `analyze.py` — mede estabilidade dos modelos e emite `policy.json`.
- `forge.py` — cliente malicioso de teste (13 ataques).
- `driver.py` — orquestra rpc-servers/llama/porteiro e mede.
- `policy.json` — política aprendida (nível 1, recomendada).
- Fonte conferida: `ggml-rpc.cpp` tag b11721, SHA-256 `583cf1ee…eacd` (baixado de raw.githubusercontent.com).

## 1. Formatos (lidos no código, não adivinhados)
`rpc_tensor` = 296 B (packed); grafo = `device u32 | uid u64 | n_nodes u32 | nodes[] u64 | n_tensors u32 | tensors[]`; SET_TENSOR = `tensor | cache_flag u8 | offset u64 | dados`. Constantes: `GGML_TYPE_COUNT=43`, `GGML_OP_COUNT=101`, `RPC_CONN_CAPS_SIZE=24`. Achado que simplifica tudo: `rpc_serve_client` cria **um `rpc_server` por conexão** e libera os buffers no fim — carga+inferência cabem em **uma conexão persistente**, então o modelo-sombra de buffers do porteiro é por-conexão, igual ao servidor.

## 2. Os modelos de grafo são ESTÁVEIS — sim, fortemente
Capturei 2 execuções (reinício + processo llama-server diferente), prompts de 2, 4, 34 e 259 tokens. Por execução: 16 GRAPH_COMPUTE (prefill + decode, 2 shards).

| Chave do grafo | distintas | comuns entre as 2 execuções |
|---|---|---|
| estrutural **sem nomes** (op/type/buffer/topologia de src) | **2** (uma por shard) | **2 = idênticas** |
| estrutural com nomes de nó | 8 | 8 = idênticas |
| concreta (inclui `ne`, `nb`, offsets) | 14 | **0 — todas diferentes** |

- **2 modelos estruturais** cobrem n_tokens de 1 (decode) a 259 (prefill): a topologia **não depende** de n_tokens/lote. O que varia por pedido é `ne`/`nb`/`op_params`/offsets — e as chaves concretas mudam a cada execução (posição do alocador), por isso **não dá para allowlistar o grafo concreto**; o que prende é o par "modelo estrutural + checagem de que cada tensor cabe no buffer vivo". Os nomes de nó **não são invariantes** (ver nível 2 abaixo).

## 3. Porteiro no caminho: texto idêntico
`local (sem rpc) == rpc direto == pelo porteiro` em SHA-256 para os 4 prompts (ex.: p_med `b540789b9f69d699`). Em modo `gate`:
- **nível 0** (só checagem por tensor) e **nível 1** (modelo estrutural sem nomes): **texto idêntico, 0 recusas** de tráfego legítimo, inclusive prefill de 259 tokens.
- **nível 2** (nomes na chave): recusou um grafo legítimo de 444 nós → confirma que nomes são frágeis. **Recomendo o nível 1.**

## 4. Grafos/comandos forjados: 13/13 recusados, rpc-server vivo
Cliente malicioso contra um rpc-server dedicado atrás do porteiro-gate. Cada ataque recusado com o motivo certo, **antes de repassar** (a validação ocorre antes do `sendall` ao upstream), e `rpc_vivo_apos_ataques = true`:

`cmd_invalido`(200) · `sem_hello` · `tamanho_de_request_errado`(HELLO) · `cmd_bloqueado`(COMM_INIT — fecha o furo do socket 0.0.0.0) · `mensagem_grande_demais`(1 TiB — mata o DoS de `resize`) · `alloc_grande_demais`(10 GiB) · `tensor_fora_do_buffer`(SET_TENSOR OOB-escrita) · `regiao_fora_do_buffer`(GET_TENSOR OOB-leitura) · `tensor_fora_do_buffer`(nó ADD de grafo com data fora — a defesa central de T3) · `op_invalida`(120) · `op_fora_do_allowlist`(50) · `tipo_invalido`(tipo removido 5) · `grafo_fora_do_modelo`(em limites, mas struct_key desconhecida).

A política aprendida permite exatamente os tipos do Qwen2.5 (f32,f16,q5_0,q8_0,q4_K,q6_K,i32,i64), 14 ops e 2 modelos estruturais; o cliente legítimo usa 15 dos 23 comandos (COMM_* nunca aparecem).

## 5. Custo (p_med, 48 tokens, 5 rodadas intercaladas, CPU disputada pelo minerador, loopback)
mediana t/s: **direto 12,89 · capture 12,60 (−2,2%) · gate 12,04 (−6,6%)** ≈ **+5,5 ms/token** sobre ~77 ms de cômputo. Na internet, onde cada token já custa centenas de ms (RTT), isso é ruído.

## 6. Risco que SOBRA (mesmo com o porteiro)
- **Dentro dos limites o conteúdo é livre:** valores de **índices** em tensores (ex.: GET_ROWS) e o **conteúdo das ativações** não são verificáveis pelo proxy sem replicar os ~101 kernels. O porteiro garante que todo acesso fica dentro de um buffer conhecido do próprio processo, mas um grafo legítimo em forma ainda lê/escreve as ativações que já passariam por ele.
- **Confidencialidade (T6):** o trabalhador vê as ativações da sua fatia; o porteiro não dá sigilo contra o próprio trabalhador/coordenador.
- **T1/T2/T4/T5 (fraude de trabalhador/coordenador, Sybil, cliente que não paga):** **fora do escopo do porteiro.** Integridade de resultado só por **redundância** (recomputar a fatia em ≥2 máquinas e comparar) — é aqui que entra a ideia do usuário dos "≥3 coordenadores que respondem igualmente"; o porteiro é a camada que impede que a máquina de quem empresta memória seja derrubada/invadida enquanto isso.

## Não confirmado
- Tudo em **loopback numa CPU só** (minerador a 100%); números de t/s não representam máquinas separadas nem internet (jitter/perda/slow-start não testados).
- Modelo único testado: **Qwen2.5-0.5B** (2 shards). Modelos maiores/MoE podem ter mais modelos estruturais — a abordagem (poucas chaves estruturais estáveis + limites por buffer) deve valer, mas **não medi** em 70B.
- `blck_size` de tipos exóticos (NVFP4/Q1_0/Q2_0) ficou **fora do allowlist** de propósito (não aparecem; seriam recusados).
- O teto `ne[1]` é guarda fraca (a dimensão do vocabulário, 151936, domina `ne[1]`); **a restrição que de fato prende é a checagem de caber-no-buffer**, não o teto de n_tokens.
- Não testei sessões longas com FREE_BUFFER/COPY_TENSOR/SET_TENSOR_2D (suportados no código, não usados nesta captura; estão no allowlist por precaução).
- Prova de que o byte forjado não chegou ao servidor: por construção do código (recusa antes do forward) **e** pela sobrevivência do rpc-server; não instrumentei o servidor por dentro.

## Encerramento
Encerrei todos os processos que iniciei. `tasklist`: **0 `llama-server.exe`** e **0 `ggml-rpc-server.exe` meus**; nada meu escuta em 50061-50066/50071-50073/50081-50082 ou 8950-8954. **Não toquei** nos 2 `ggml-rpc-server.exe` em 50098/50099 nem nos pythons — são de **outro agente** (`C:\sgtmp\shard2\determinismo\`). Não falei com a VPS, não fiz deploy/commit, baixei só código-fonte do llama.cpp (tag b11721, raw.githubusercontent.com). Em disco ficaram ~473 MB em `C:\sgtmp\shard2\porteiro\` (49 MB da cópia do llama; ~414 MB de caches `-c`; 11 MB de logs) — podem ser apagados.