# Sigma 2.5: imagem e vídeo (regras v2)

A primeira atualização de regras da rede (docs/ATUALIZACOES.md): entra num bloco marcado em `chain.SCHEDULE`, sem
recomeçar a chain. Antes da altura, tudo o que está abaixo é recusado como sempre foi (`chain.FIELDS_SINCE`).

## 1. Classes de IA

- `chain.TASKS = ("text", "image", "video")`. Modelo sem `task` (todos os de antes) é de texto.
- `register_model` aceita `task` e `files`: até 4 arquivos extras `{role, url, sha256, size}`, com `role` em
  `vae`, `clip_l`, `clip_g`, `t5xxl`, `llm`. Texto não leva `files`.
  - O modelo guarda `task`, `files`, `main_size` (só o arquivo principal), `size` (o total) e `units` (estatística).
  - `ram_mb = ceil(total × 1,2 / 1e6) + 1536` (imagem) ou `+ 4096` (vídeo). O texto continua com `+ 512`.
  - O dia de aluguel adiantado (acima de 50 MB) conta disco e memória para imagem e vídeo.
- `register_machine` aceita `tasks` (texto, imagem e/ou vídeo; com a IA ligada, pelo menos uma) e os preços
  `ai_per_image` (padrão 20.000 µSIG) e `ai_per_video_second` (padrão 100.000 µSIG). O teto de cada um é 100 SIG.
  - Deixar de aceitar uma classe tira da máquina só os modelos dela. Assim, quando o motor de texto cai (ex.: o
    Ollama fechado), a máquina declara só imagem/vídeo e continua com esses modelos.
- Leitura pelo nó: o `/machines` põe em `models` só os modelos de texto (o que um app 2.4 entende) e os de imagem e
  vídeo em `media_models`. É só a resposta HTTP: o estado da chain e o `/machine/<endereço>` continuam com tudo.
- A atribuição só põe um modelo numa máquina que aceita a classe dele.

## 2. Pedidos e pagamento

- `create_job` de IA para um modelo de imagem ou vídeo leva `units`, no lugar de `max_tokens`:
  - `units` é o número de imagens (1 a 4) ou de segundos (1 a 10);
  - o escrow precisa cobrir `units × max_price`;
  - o job guarda `media` e `units`.
- O claim usa o preço da classe. A entrega leva `usage = {"units": n}` (0 a `units`); uma falha leva 0.
- O pagamento é `min(escrow, n × preço)`:
  - 100% vai para a máquina, menos o royalty;
  - o troco volta para quem pediu.
- O prazo para entregar depois do claim é de 1.800 s (imagem) ou 7.200 s (vídeo); o do texto continua em 600 s.

## 3. Carteira dos fundadores e votações decididas antes

- `chain.UPGRADE_GRANTS`: saldos que uma atualização cria na altura dela, uma vez só, por rede (hash do genesis).
  Na rede principal (`MAINNET_GENESIS`), a v2 cria 1.000.000 SIG para `FOUNDERS`
  (`sigfd1980713ecf1a514ad3f4a15828bb2d0c00a09e` = `0xfd19…a09e`).
  - Entra no `begin_block` da altura da v2, antes da emissão; soma em `minted` (nunca passa do teto) e fica em
    `ext["grants"]` (`/status` → `grants`) e no histórico da carteira (crédito "founders").
  - Uma rede nova (genesis já na v2) ou recriada com `RESET=1` (genesis novo) não recebe nada: a reserva só existe
    como atualização da rede principal de hoje.
- A partir da v2, `_close_proposals` encerra uma votação antes dos 10 dias quando `max(sim, não) × 2 > minted`
  (um lado passou de metade de todos os votos possíveis); a proposta fica com `early: true`. Com mais da metade, o
  quórum (no máximo 50%) já está cumprido.
- Efeito declarado: enquanto `minted` for menor que 2 milhões de SIG (uns 50 dias de emissão depois da v2), a
  carteira dos fundadores sozinha decide qualquer votação na hora.

## 4. Máquina e motor

- `sigma/media.py`: stable-diffusion.cpp `master-1026-997478a`, com SHA-256 fixado por pacote (Windows CPU e
  Vulkan, Linux x64 e Vulkan, macOS arm64). Roda um `sd-cli` por pedido, sem janela e com prioridade baixa, numa
  pasta temporária que sai logo depois.
- Cada arquivo do modelo é conferido pelo SHA-256 e pela estrutura (`check_weights`: safetensors ou GGUF).
- O pedido vem cifrado:
  - `{prompt, size, count | seconds, negative?, steps?, cfg?, sampler?, seed?, flow_shift?}`;
  - `clean_spec` só deixa passar valores de listas fechadas e dentro de limites (números não finitos viram o
    padrão), tira caracteres de controle e os sinais `<` `>` do texto (o sd-cli leria `<lora:arquivo:peso>` do
    disco da máquina) e nunca pede mais unidades do que o job pagou;
  - pedido que não abre, por qualquer motivo, volta na hora como falha `unreadable` (escrow de volta, sem falta para
    a máquina): nenhum pedido reivindicado fica sem resposta.
- O resultado vai cifrado para `client_pub`: `{media: [{mime, data (base64), width, height, seconds?}], task,
  model, machine}`.
- Imagem e vídeo têm fila própria (2 pedidos), separada da do texto.
- Configurações `ai_image` (ligada) e `ai_video` (desligada). A máquina só declara `tasks` e os preços novos
  quando o `/mining` diz `media: true` (a rede já está na v2).

## 5. App e painel

- Rotas: `POST /api/ai/image`, `POST /api/ai/video` e `GET /api/media/<hash>`.
  - As mídias ficam em `<dados>/midia` e o painel as mostra como `blob:`; o CSP passa a permitir `img-src blob:`
    e `media-src blob:`.
- Conversas guardadas no computador (`<dados>/conversas`): `GET /api/chats`, `GET /api/chats/<id>` e
  `POST /api/chats/save|delete|rename`.
- Sigma Code em sessão: `POST /api/code/continue` (próxima instrução, mesma conversa) e `POST /api/code/reset`.
- Catálogo:
  - Z-Image Turbo (Apache-2.0; GGUF + Qwen3-4B + VAE);
  - Stable Diffusion 1.5 (OpenRAIL-M; um arquivo);
  - Wan 2.1 T2V 1.3B (Apache-2.0; + VAE + UMT5-XXL).
