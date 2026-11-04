# Sigma 2.6: caução dos coordenadores (regras v3)

Segunda atualização de regras da rede (docs/ATUALIZACOES.md), sem recomeçar a chain. É a base econômica da divisão
de modelos gigantes aberta a todos: quem coordena trava SIG na rede e perde parte dele se trapacear. O sorteio dos
coordenadores, a conferência entre eles e as provas de fraude vêm na próxima atualização (docs/PENDENCIAS.md).

## 1. Caução

- Tipos novos `bond` e `unbond` (`{machine, amount}`), aceitos só a partir da v3 (`chain.TYPES_SINCE`). Antes disso
  são recusados como tipo desconhecido, como sempre foram, inclusive pela MetaMask.
- `bond`: assinada pela carteira que recebe os ganhos da máquina (o `payout`). A máquina precisa estar registrada,
  e uma caução de outra carteira não muda de dono.
- `unbond`: tira da caução ativa na hora e devolve ao dono depois de 14 dias (`UNBOND_SECONDS`), sozinha, no fim do
  bloco. Até lá ainda pode ser cortada. São no máximo 20 retiradas em espera por caução.
- O estado fica em `ext["bonds"]` e conta na oferta: `saldos + escrows + orçamentos + votações + caução == minted`.

## 2. Corte

- `State._slash(máquina, valor, motivo, reporter, victim, victim_cap)` tira primeiro da caução ativa e depois das
  retiradas em espera, das mais novas para as mais antigas.
- Do valor cortado:
  - 20% vão para quem provou a fraude;
  - até 30% vão para quem foi lesado, no máximo o prejuízo;
  - o resto volta para o que falta minerar (sai de `minted` e a emissão paga de novo; nada é queimado).
- Quem responde pela máquina (ela mesma, o dono da caução e o payout) não recebe parte do próprio corte.
- A máquina perde 10 de reputação. O corte fica registrado em `ext["slashes"]` (os últimos 200).
- Nesta versão nada chama `_slash` ainda: as provas de fraude entram na próxima.

## 3. Quem pode coordenar

- `State.is_coordinator(máquina)` exige as quatro condições:
  - regras v3;
  - caução ativa de pelo menos `min_bond`;
  - a caução da carteira que é o payout de agora;
  - máquina online e com a IA ligada.
- `min_bond` começa em 1.000 SIG. É criado na altura da v3 (`UPGRADE_PARAMS`), não no genesis, e é votável entre
  10 e 1.000.000 SIG (`GOVERNABLE_SINCE`).

## 4. Nó, cliente e linha de comando

- Nó:
  - `GET /bonds` e `GET /bonds/<máquina>`;
  - o `/mining` passa a ter `bonding`, `bonded`, `coordinators`, `min_bond` e `unbond_seconds`.
- `Client.bond`, `Client.unbond` e `Client.bonds`.
- Linha de comando: `bond` (ou `caucionar`) e `unbond` (ou `retirar-caucao`).
