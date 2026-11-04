# Sigma v2.3: emissão a cada bloco, aluguel pela demanda e votações

Vale tudo o que está nas SPECs v2, v2.1 e v2.2; quando discordarem, vale este. `sigma.__version__ = "2.3.0"`.
A rede pública foi lançada sem nenhum SIG impresso ainda, então ela é recriada do zero (`RESET=1`) sem perda.

## 1. Emissão contínua, 50x menor

- `params["mint_bps"]` passa de 50 para **1**: 0,01% do que falta dos 100 milhões a cada 12 h (antes 0,5%).
  `epoch_seconds` (43.200) continua sendo o período de referência da taxa ("% a cada 12 h").
- A emissão sai **em todo bloco**, fracionada pelo tempo: o bloco imprime
  `(cap - minted) * mint_bps * dt_ms // (10000 * epoch_ms)`, com `dt_ms = round((agora - emit_time) * 1000)` e
  `epoch_ms = round(epoch_seconds * 1000)`. Assim a soma em 12 h é a mesma taxa, não importa quantos blocos saem
  (é a média exata: cada bloco leva a fração de 12 h que ele durou). `mining["emit_time"]` começa no genesis.
- Quem recebe: os pontos da última janela já fechada para relatórios (`State.points`, a "base"). Relatórios de uma
  janela `w` são aceitos durante `w` e `w+1`; na virada para `w`, a janela `w-2` fecha e vira a base. Os pontos das
  janelas abertas ficam em `mining["open_points"] = {str(janela): {endereço: pontos}}`.
  Cada bloco divide a sua emissão por `pontos / total` da base (arredondado para baixo; o resto fica por imprimir).
  Sem pontos na base, nada é impresso naquele intervalo (o tempo passa).
- Cada crédito de mineração entra em `State.credits` com tipo `"mining"` (o nó junta por endereço e época de 12 h,
  e mostra como entrada de mineração).
- `epochs` continua como estatística a cada 12 h (quanto saiu, quantos mineradores, os 10 maiores), sem
  liquidação: `mining["period_paid"]` e `mining["period_rewards"]` acumulam e fecham na virada do período.
- `/mining` ganha `mint_per_period` (12 h), `mint_per_hour`, `continuous: true`; `miner_info` ganha
  `est_per_hour` (fatia na base × emissão por hora).

## 2. Aluguel pelo que o modelo/site usa e pela demanda da rede

- Parâmetros (votáveis): `hosting_per_mb_window` (disco, µSIG por MB por janela, padrão 5),
  `ram_per_mb_window` (memória, padrão 2) e `demand_max_bps` (quanto a demanda pode subir o preço, padrão 30000 =
  até 4x).
- Na virada de cada janela a rede calcula a demanda:
  - memória: `pedido = Σ ram_mb × replicas` de todos os modelos; `capacidade = Σ hw.ai_ram_mb` das máquinas online
    com IA ligada;
  - disco: `pedido = Σ size × replicas` de modelos e sites ativos; `capacidade = Σ hw.disk_mb × 1e6` das máquinas
    online.
  - `u = min(1, pedido / capacidade)` (sem capacidade: `u = 1`), em bps; multiplicador
    `mult_bps = 10000 + demand_max_bps × u² / 10000²`. Guardado em `mining["rent"] = {"disk_bps", "ram_bps"}`.
- Aluguel por janela de cada cópia online:
  - modelo: `max(1, ceil((size × disco × disk_bps + ram_mb × 1e6 × memória × ram_bps) / (1e6 × 10000)))`;
  - site: `max(1, ceil(size × disco × disk_bps / (1e6 × 10000)))`.
  `rent_window(item)` = cópias × isso (vale para `funded()`, para o mínimo de orçamento e para o painel).
- O uso em si (cada pergunta) continua pago por quem usa, por token, direto para a máquina que respondeu.

## 3. Sem taxa nem queima

Confirmado no código: nada é queimado. Quem usa paga 100% para quem hospeda; a única parte que não vai para a
máquina é o royalty que o próprio padrinho escolheu (0 a 50%).

## 4. Votações

- Qualquer carteira propõe (`propose`): `{"kind": "param"|"text", "title", "text", "param"?, "value"?}`. Deixa um
  depósito de `params["proposal_deposit"]` (padrão 10 SIG) que volta no fim. No máximo `MAX_OPEN_PROPOSALS = 20`
  abertas. Duração: `VOTE_SECONDS = 10 dias` (pelo relógio dos blocos).
- Votar (`vote`): `{"proposal_id", "choice": "sim"|"nao", "amount"}`. **1 SIG = 1 voto.** O valor fica travado
  até o fim da votação (assim o mesmo SIG não vota duas vezes por carteiras diferentes) e volta inteiro. Um voto
  por carteira por votação.
- Fim: aprovada se `sim > não` e `sim + não >= minted × quorum_bps / 10000` (`quorum_bps` padrão 100 = 1% do SIG
  que existe). Proposta de parâmetro aprovada muda o parâmetro na hora; de texto só registra a decisão.
- Parâmetros votáveis e limites: `mint_bps` 0..50, `hosting_per_mb_window` 1..100, `ram_per_mb_window` 0..100,
  `demand_max_bps` 0..100000, `min_share_bits` 8..32, `proposal_deposit` 1..10000 SIG, `quorum_bps` 0..5000.
  O teto de 100 milhões e o tempo das janelas não são votáveis.
- Os parâmetros passam a fazer parte do estado (`params` entra no `state_root`).
- O estado guarda as votações em `proposals`; as encerradas ficam (sem a lista de votos) e só as 100 mais
  recentes.
- Oferta: saldos + orçamentos + reservas + **votos travados + depósitos** = impresso.
