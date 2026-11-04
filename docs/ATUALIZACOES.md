# Como mudar a rede sem recomeçar do zero

A partir da v2.4, a rede Sigma pode mudar de regras sem apagar a chain e sem ninguém perder SIG.
Este guia diz o que dá para mudar, como e o que nunca fazer.

## Por que existia o problema

Ao ligar, o nó reexecuta todos os blocos gravados e confere, em cada um, a "impressão digital" do estado
(`state_root`). Se o código muda uma regra, os blocos antigos dão outro resultado e o nó recusa a própria chain.
Antes da v2.4 o único jeito de mudar uma regra era recriar a rede (`RESET=1`).

## Como funciona agora

- Cada bloco diz a versão das regras em que rodou (`"rules": 1`, `2`, …).
- O código guarda as duas regras, a antiga e a nova, e escolhe pela versão: `if self.rule(2): …nova… else: …antiga…`.
  Os blocos antigos continuam rodando na regra antiga e batendo; os novos, na nova.
- A versão nova entra numa **altura marcada** (`chain.SCHEDULE`). Do bloco marcado em diante, tudo roda nela.
- Um nó desatualizado **para com segurança** quando vê um bloco de uma versão que não conhece: não descarta
  nada, não grava nada por cima e avisa no `/status` (`upgrade_required`) e no painel para baixar a versão nova.
  Quando a pessoa atualiza o Sigma, ele continua do bloco onde parou.
- O `/status` mostra `rules` (a versão em vigor), `rules_known` (a mais nova que aquele programa conhece),
  `upgrades` (as marcadas que ainda vão chegar) e `genesis` (o hash do genesis, que identifica a rede).
- O app compara isso com a própria versão e mostra um aviso vermelho em todas as abas do painel antes da
  atualização chegar ("a rede muda de regras no bloco N: baixe a versão nova antes disso").

## O que muda sem atualização nenhuma

- **Os parâmetros votáveis** (taxa de emissão, preços de disco e memória, teto da demanda, dificuldade, depósito,
  quórum): uma votação aprovada muda na hora, dentro dos limites de `GOVERNABLE`.
- **Tudo que não é regra da chain**: site, painel, carteira, app, minerador, motor das IAs, fórum, textos.
  Basta publicar: `bash deploy/deploy_vps.sh` (sem `RESET=1`).

## Passo a passo de uma atualização de regra

Exemplo: mudar a fórmula do aluguel.

1. **Escreva a regra nova ao lado da antiga**, nunca no lugar dela:

   ```python
   def copy_fee(self, item):
       if self.rule(2):
           ...  # fórmula nova
       ...      # fórmula de antes, intacta
   ```

2. **Suba a versão**: `RULES = 2` em `sigma/chain.py`.
3. **Marque a altura** em que ela entra, com o hash do genesis da rede (está no `/status`, campo `genesis`):

   ```python
   SCHEDULE = {"<hash do genesis>": {2: 120000}}
   ```

   O genesis da rede principal (sigma.195-35-29-232.sslip.io, recriada na v2.4 em 04/11/2026) é
   `a8eabe25b19487fdc77c5bf86a812e55c2837e053c20d1aba2bc0794a7bb1029`.

   Escolha uma altura com folga para todo mundo baixar a versão nova (o painel avisa assim que ela for
   publicada). Com blocos de ~25 s, 3.500 blocos dão ~1 dia.
4. **Teste** que a chain antiga continua batendo (veja `tests/test_upgrades.py`, que faz exatamente isso com uma
   regra de exemplo) e rode a bateria toda.
5. **Publique o validador primeiro**: `bash deploy/deploy_vps.sh` (sem `RESET=1`). Depois gere e publique os
   instaladores (`packaging/build_pyz.py` e `packaging/build_windows.py` vão junto no mesmo deploy).
6. Na altura marcada, a rede passa para a v2 sozinha.

Se quiser que a comunidade decida, abra antes uma **votação de texto** ("Mudar a fórmula do aluguel na v2?")
e só marque a altura se ela for aprovada.

## Regras de ouro (o que quebra a chain antiga)

- **Nunca mude a regra antiga.** Ela precisa continuar existindo para os blocos de antes.
- **Nunca acrescente chaves em `DEFAULT_PARAMS` nem em `State.STATE_KEYS`** — o estado do genesis e de todos os
  blocos mudaria. Dado novo vai em `self.ext` (criado na altura da atualização); parâmetro novo entra em
  `self.params` também só na altura dela. `tests/test_upgrades.py` falha se essas chaves mudarem.
- **Tipo novo de transação** só é aceito com `rule(N)`: antes da altura, ele tem de ser recusado como sempre foi.
- **`SCHEDULE` só marca versões que o código tem** (`2 <= versão <= RULES`); o teste também confere isso.
- **Não marque uma altura que já passou**: os blocos já gravados rodaram na regra antiga.
- O **validador** (a VPS) precisa estar atualizado antes da altura. Se não estiver, ele para de produzir blocos
  na altura marcada até ser atualizado (nada se perde, mas a rede fica parada).

## Quando ainda é preciso recomeçar

Só se a mudança não couber nesse modelo (por exemplo, trocar o formato dos blocos ou o algoritmo de
assinatura). Nesse caso o `RESET=1` continua existindo, e todo SIG da rede antiga deixa de valer, então só faça
isso com a rede avisada.
