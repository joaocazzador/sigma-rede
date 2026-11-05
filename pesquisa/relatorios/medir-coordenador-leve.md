Everything is shut down and ports are clear. I have all the measurements. Here is the report.

---

# Relatório — COORDENADOR LEVE (agente "leve")

**Ideia testada:** o próprio app de quem pergunta (o cliente) é o coordenador (roda o `llama-server`), para nenhum estranho ler o pedido, **sem** precisar do arquivo inteiro do modelo gigante. Truque: GGUF "esparso" (cabeçalho + `token_embd`/`output`/`output_norm` reais, zeros no lugar das camadas) + um "porteiro" local em cada trabalhador que troca os zeros pelos bytes/hash verdadeiros ao ver o NOME do tensor.

**Resultado em uma frase:** funciona e o texto sai **idêntico** ao normal. Provei os dois caminhos de transferência (bytes e hash) contra o binário real `b11721`. O ganho real é de **disco e privacidade**; não é ganho de rede nem de RAM comprometida.

Tudo rodou em `C:\sgtmp\shard2\leve\`, só em 127.0.0.1, portas 50061-50078 e 8951-8955. CPU disputada pelo minerador do usuário (100%), então os t/s são de loopback e **não** representam máquinas separadas.

## Scripts (caminhos absolutos)
- `C:\sgtmp\shard2\leve\gguf_util.py` — leitor de GGUF v3 (stdlib) + FNV-1a de 64 bits.
- `C:\sgtmp\shard2\leve\make_sparse.py` — monta o GGUF esparso (sparse NTFS via `FSCTL_SET_SPARSE`/ctypes) e confere byte a byte.
- `C:\sgtmp\shard2\leve\porteiro.py` — o porteiro: proxy TCP que entende o enquadramento do RPC e troca `SET_TENSOR`/`SET_TENSOR_HASH` dos tensores que o trabalhador possui.
- `C:\sgtmp\shard2\leve\bench.py` — compara 3 configs (local; RPC completo; esparso+porteiro) e mede.
- `C:\sgtmp\shard2\leve\hash_test.py` — teste ponta a ponta do caminho `SET_TENSOR_HASH`.
- `C:\sgtmp\shard2\leve\manifest.py` — gera manifesto SHA-256 por tensor e mede o custo.
- `C:\sgtmp\shard2\leve\run_verbose.py`, `inspect_split.py` — apoio (tabela de buffers, split de tamanhos).
- Logs e dados: `C:\sgtmp\shard2\leve\logs\` (`bench-result.json`, `*-srv.log`, `*-port*.json`). Artefato: `C:\sgtmp\shard2\leve\coord-sparse.gguf`.

## 1. Montagem (modelo de teste Qwen2.5-0.5B Q4_K_M, 291 tensores, 24 camadas)
O GGUF esparso do coordenador mantém reais só `output.weight` (137 MiB), `token_embd.weight` (89 MiB) e `output_norm.weight` (tiny) + cabeçalho/metadados (5,9 MB); zera os 288 tensores `blk.*` (247 MB). Verificação: cabeçalho e os 3 tensores "keep" **byte a byte idênticos**; amostras de `blk.*` lidas como zeros. OK.

## 2. Texto idêntico, disco, RAM, carga
Três configs, mesma requisição determinística (temp 0, seed 42, 48 tokens, `cache_prompt:false`):

| Config | Texto SHA-256 | Carga | Working set | Private | Geração |
|---|---|---|---|---|---|
| A local (GGUF completo, sem RPC) | `ca9e6ba7caeb46fc` | 12,11 s | 492,3 MB | 179,2 MB | 13,94 t/s |
| B RPC direto (GGUF completo, 2 rpc-servers) | `ca9e6ba7caeb46fc` | 3,02 s | 469,6 MB | 121,0 MB | 13,59 t/s |
| **C esparso + 2 porteiros + 2 rpc-servers** | `ca9e6ba7caeb46fc` | 5,29 s | 469,8 MB | 121,6 MB | 13,24 t/s |

- **A == C e B == C: texto idêntico** (confirmado também numa 2ª geração). O truque não altera a saída.
- **Disco do coordenador:** esparso = **244,3 MB alocados** contra **491,4 MB** lógicos (49,7%). Economia de ~247 MB (os `blk.*` viraram buracos). Se o `output` também sair do coordenador (variante do teste hash), caiu para **99,7 MB alocados (20,3%)**.
- **RAM que o coordenador realmente precisa:** do log, `CPU_Mapped model buffer size = 227,20 MiB` (= `token_embd` 89 + `output` 137 + `output_norm`), mais `CPU compute 9,02 MiB` e `output 0,58 MiB`. **O KV cache fica nos trabalhadores** (13+11 MiB), não no coordenador. Buffers dos trabalhadores: 129,37 e 106,39 MiB (os pesos reais, trocados pelo porteiro).
- **Atenção (medido):** o *working set* de C (~470 MB) é igual ao de B. Carregar com `-ot` e mmap faz o `llama-server` **ler e enviar todas as camadas zeradas**, então ele toca o arquivo lógico inteiro durante a carga. O que fica *comprometido* (private) é ~121 MB; o resto é página de arquivo/zero recuperável. Ou seja: **o esparso economiza disco, não o pico de working set da carga**.
- `output.weight` e `output_norm.weight` confirmados **na CPU do coordenador** (`buffer type overridden to CPU`); camadas 0-12 no trabalhador 0, 13-24 no trabalhador 1. Cada porteiro trocou só a sua fatia: **156 + 132 = 288 tensores** (todos os `blk.*`), split limpo 13/11.

## 3. O `llama-server` confere os bytes? Não.
Empírico: texto idêntico em B e C, logo nada quebrou o truque. No código (`ggml-rpc.cpp` b11721): `set_tensor`/`set_tensor_hash` só validam **limites de buffer**, nunca o conteúdo; não há checksum dos pesos. O único hash é o FNV-1a opcional (`HASH_THRESHOLD` = 10 MiB, só buffers `WEIGHTS`), que serve ao *cache*, não a validação — e é exatamente o que o porteiro intercepta. Único aviso visto: `tensor overrides to CPU are used with mmap enabled` (dica de desempenho, não bloqueia). Com `-ot "^output(_norm)?\.weight$=CPU"` a saída fica no coordenador e os logits (607 KB/token) **não** trafegam.

## 4. Caminho do hash provado ponta a ponta (o que um 70B usa)
No 0.5B as camadas são < 10 MiB, então vão por `SET_TENSOR` (bytes). Num 70B, os tensores de camada passam de 10 MiB e vão por `SET_TENSOR_HASH`. Forcei `output.weight` (137 MiB) para um trabalhador (sem `-ot`), zerado no esparso:
- Coordenador leu zeros → mandou `SET_TENSOR_HASH(hash dos zeros)`.
- Porteiro trocou pelo hash real **`0xc5d93d2d0af14837`** (bate com o valor já conhecido do projeto).
- O rpc-server achou os bytes reais no **cache** (que pré-preenchi, simulando o download por HTTP Range) → **cache hit**, nenhum byte de peso trafegou.
- **Texto idêntico** (`ca9e6ba7caeb46fc`). Porteiro trocou 1/1 hash.
- Confirmação da importância do `-ot`: com `output` remoto, voltaram **30 MB** de logits (s2c); com `output` local (config C), só 0,78 MB.

**Bytes que cruzam** (medidos pelo porteiro): config C (byte-swap) coord→trabalhadores ~252 MB — ou seja, **os zeros das camadas pequenas ainda trafegam**. No caminho hash, os 137 MiB do `output` **não** trafegaram (16 bytes de hash). Portanto: num 70B, os pesos grandes (quase tudo) usam o hash e **não** vão pela rede depois que o trabalhador enche o cache pelo HF; só tensores < 10 MiB (normas/vieses, fração ínfima) trafegam como zeros.

## 5. Manifesto de integridade (SHA-256 por tensor)
Para o trabalhador conferir o que baixou. Medido no 0.5B (`manifest.py`, `hashlib`):
- SHA-256 sobre 485,5 MB: **3,24 s a 150 MB/s**. Manifesto JSON: **46,5 KiB** para 291 tensores (~164 B/tensor). `sha256(output.weight)=4962347a…` bate com o valor conhecido.
- **Estimativa 70B** (~42,5 GB, ~720 tensores): SHA-256 **~284 s (~4,7 min)**, uma vez; manifesto **~116 KiB**.
- Proposta: o padrinho publica no registro `{nome, offset, nbytes, sha256, fnv}`. O trabalhador baixa sua fatia por HTTP Range, **confere o SHA-256** e grava cada tensor em `cache/<fnv>` usando o FNV **do manifesto** — assim ninguém recomputa FNV em tempo de execução. O porteiro lê esse FNV para reescrever o hash (testei com `--hashmap`).
- **Custo do FNV:** em Python puro medi **~1,9 MB/s** (137 MiB em 74,8 s). Para 42 GB isso seria ~6 h — **impraticável em stdlib**. Por isso o FNV deve vir pronto no manifesto (gerado pelo padrinho com a rotina em C do próprio llama.cpp), nunca recalculado em Python no runtime.

## 6. Limitações
- **Coordenador ainda precisa de:** RAM para `token_embd`+`output` (227 MiB aqui; estimo ~2 GB num 70B, pois são matrizes vocab×hidden) e CPU para fazer o embedding de entrada, a projeção final sobre o vocabulário e a amostragem **a cada token**. Ele **não** computa as camadas nem guarda o KV (isso fica nos trabalhadores).
- **Rede não melhora no caminho de bytes:** tensores < 10 MiB viajam como zeros (o porteiro troca no destino). Só o caminho hash evita o tráfego.
- **Working set da carga não cai** com o esparso (o `llama-server` varre o arquivo lógico inteiro via mmap). O ganho é disco + privacidade, não pico de RAM.
- **Arquivo esparso:** NTFS ok (medido). **FAT/exFAT não têm sparse** — o arquivo ocuparia o tamanho lógico inteiro (o truque ainda funciona, mas sem economia de disco). Linux (ext4/xfs/btrfs) e macOS (APFS) têm sparse; só muda a ferramenta. Em qualquer FS o mmap lê buraco como zero, então o truque funciona; só a economia de disco depende do sparse.
- **O porteiro** roda na máquina do trabalhador (lê os bytes reais dele), adiciona ~1-2 ms por ida-e-volta e uma cópia por quadro (proxy em Python). Em produção, a guarda de protocolo do porteiro (exigir HELLO/v8.0.0, limitar tamanho, bloquear `COMM_*`) também é a defesa contra o RPC inseguro — ver `shard-medir-codigo.md`.

## 7. Conexão com o anti-fraude (pedido do usuário)
Este desenho ataca o item **T6 (privacidade)**: como **o próprio cliente é o coordenador**, nenhum estranho lê o pedido em claro, e some a figura do "coordenador terceiro que lê o prompt" (parte de T2). Ele **não** resolve sozinho: T1 (trabalhador devolve ativações erradas), T3 (ataques RPC ao PC do trabalhador — precisa do porteiro-guarda + sandbox), T4 (Sybil) e T5 (cliente malicioso). Sobre a ideia dos "3 coordenadores que respondem igualmente": para inferência dividida isso tem um problema de determinismo — eu só confirmei que o texto é idêntico **na mesma máquina** variando o split; **não** testei igualdade entre hardwares diferentes, e a proposta anterior (`docs\DIVISAO-DE-MODELOS.md`) afirma que hardware/divisão diferentes mudam os tokens. Então "comparar hashes de saída entre coordenadores" não é confiável sem fixar o caminho numérico; a redundância teria que ser no nível das ativações por fatia, não da saída final.

## Não confirmado
- **Máquinas separadas / internet:** tudo foi loopback numa CPU disputada; os t/s e tempos de carga não valem para rede real (latência, banda, jitter, NAT/relay não testados).
- **Modelos grandes (14B/70B):** disco, RAM, carga e velocidade são **extrapolação**; não há modelo grande nem foi permitido baixar.
- **Determinismo entre hardwares diferentes** (base da ideia dos 3 coordenadores): não testado.
- **Caminho hash com muitos tensores grandes simultâneos** e download real por HTTP Range do HF: simulei o cache pré-preenchido num tensor; não baixei do HF nem testei dezenas de tensores grandes.
- **FNV rápido:** medi só o Python puro (lento); não integrei a rotina C.
- **GGUF em partes (`llama-gguf-split`) + esparso + RPC** juntos: não testei.
- **Robustez do porteiro** sob quedas/reconexão e sob o multiplexing de 14-18 conexões da carga: funcionou nos testes, mas não estressei falhas.
- **`--no-mmap`** (poderia mudar o working set da carga) e **`--load-mode none`** (sugerido pelo aviso): não testei.

## Encerramento
Encerrei todos os processos que iniciei. `tasklist` não mostra `llama-server.exe` nem `ggml-rpc-server.exe`; nenhum Python meu (porteiro/bench) rodando; nada escutando em 50061-50099 ou 8950-8969. Não toquei no minerador do usuário, no Ollama nem em outros agentes. Não alterei arquivos do projeto, não falei com a VPS, não fiz deploy nem commit, e só li código-fonte do llama.cpp que já estava em disco. Ficaram em `C:\sgtmp\shard2\leve\` ~270 MB reais (binários do llama 48 MB + `coord-sparse.gguf` 244 MB alocados, reproduzível por `make_sparse.py` e removível).