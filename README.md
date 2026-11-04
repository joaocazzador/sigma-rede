# Σ Sigma

**Vão substituir você por um robô. Queremos que os donos dos robôs sejam as pessoas, não as Big Techs.**

O Sigma é uma rede de inteligência artificial feita para pertencer a quem participa. Cada computador com o app
ajuda a rodar a rede (modelos de IA, computação e sites) e recebe SIG por isso. Quem usa a IA paga em SIG a quem
hospeda. Hoje um servidor do projeto ainda valida os blocos e entrega os sites; o objetivo é dividir isso entre
muitos validadores, até ninguém, nem nós, conseguir mandar na rede.

## Baixar e rodar

1. Baixe o app em **https://sigma.195-35-29-232.sslip.io/baixar** (o endereço oficial de hoje: o Sigma ainda não
   tem domínio próprio, então desconfie de endereços parecidos).
   - **Windows:** `Sigma.exe`. Dois cliques e pronto.
   - **Linux e macOS:** `sigma.pyz`. Rode `python3 sigma.pyz` (Python 3.11 ou mais novo). No macOS, com o Python
     do python.org, abra antes **Aplicativos › Python 3.x › Install Certificates.command**: sem esse passo o
     Python não abre conexões seguras (HTTPS) e o Sigma fica sem rede.
2. Na primeira vez, escolha para onde vai o que você ganhar: **Criar uma carteira nova** (o mais simples: o app
   mostra a chave uma vez, anote), **Usar minha chave** (de uma carteira só para o Sigma) ou **Só colar um
   endereço** (por exemplo, o da sua MetaMask). Não cole no app a chave da conta onde você guarda outras
   criptomoedas: a mesma chave controla essa conta em qualquer rede compatível com o Ethereum.
3. Pronto: o computador já está minerando SIG e hospedando a rede.

Você não escolhe nem baixa modelo nenhum. A rede decide qual modelo fica em qual computador, e o app baixa,
confere o SHA-256 e o formato e carrega sozinho (no Ollama, se você tiver; senão no llama.cpp, que ele mesmo
instala). Tudo se gerencia pelo painel do app: carteira, quanto da CPU usar, IA, disco, sites, modelos e a rede.

### Conferir o download

O `Sigma.exe` não tem assinatura digital, então o Windows e o antivírus podem avisar. Antes de abrir:

- confira se baixou do endereço oficial acima: um número ou uma letra trocada já é outro site;
- compare o SHA-256 do arquivo com a lista da página de download (`/downloads/SHA256SUMS.txt`). O SHA-256 prova
  que o arquivo chegou inteiro e é igual ao publicado ali, mas **não protege de um site falso que publique a
  lista dele**: quem garante isso é o endereço. Ainda não existe uma segunda lista fora deste servidor;
- **páginas em `/site/` não são do Sigma**: endereços com `/site/` são sites publicados por qualquer pessoa da
  rede, não pelo projeto. O Sigma oficial só se baixa no endereço oficial acima; nenhuma página em `/site/` é do
  Sigma, e nenhuma delas vai pedir a sua chave;
- na dúvida, use o `sigma.pyz`: é o próprio código-fonte e dá para ler antes de rodar.

### O que o app faz no seu computador (e quanto custa)

- **Minera** com a parte do processador que você liberar (50% por padrão, prioridade baixa) e pausa quando a IA
  trabalha. No notebook, pausa a mineração na bateria e volta sozinho no carregador (Configurações → “Minerar
  também na bateria”).
- **Empresta memória e disco para a IA da rede**: por padrão, até 40% da memória e 20 GB do disco. Cada modelo
  tem de 0,5 a 5 GB e fica guardado enquanto existir na rede. Internet limitada (4G, por exemplo)? Desligue “IA
  para a rede” em Meu computador.
- **Gasta energia, e a conta de luz é sua.** A conta: watts a mais × horas ligado ÷ 1000 = kWh. Exemplo: 40 W a
  mais, 24 horas por dia, 30 dias = 28,8 kWh. O SIG não tem preço de mercado: participar é uma contribuição para a
  causa, não um investimento.
- **Vários computadores podem usar a mesma carteira**: cada um minera com a sua própria identidade, e os pontos
  somam. O limite é de 256 relatórios de mineração por janela de 5 minutos para cada conexão de internet; com
  CGNAT (comum no celular e em muitas fibras), vizinhos dividem esse limite.

## Novidades da 2.4

- **A rede muda de regras sem recomeçar do zero.** Cada bloco diz a versão das regras em que rodou. Uma regra
  nova entra num bloco marcado e o código guarda a antiga para os blocos de antes: a chain continua valendo e
  ninguém perde SIG. O painel avisa, em todas as abas, quando uma mudança de regras está marcada e falta baixar
  a versão nova. Um Sigma desatualizado para no bloco da mudança, sem perder nada, e continua de onde parou
  quando é atualizado. Como fazer uma atualização: [docs/ATUALIZACOES.md](docs/ATUALIZACOES.md).

## Novidades da 2.3

- **Emissão contínua e 50 vezes menor.** A rede imprime 0,01% do que falta dos 100 milhões a cada 12 horas (era
  0,5%), um pouco em cada bloco. Não existe mais “próxima distribuição”: o SIG sai o tempo todo para quem está
  minerando. Nas primeiras 12 horas saem 10.000 SIG (uns 833 por hora); metade do total leva uns 9 anos e meio.
- **Aluguel pelo que se usa e pela procura.** Sites pagam pelo disco; modelos pagam pelo disco e pela memória que
  deixam reservada. Os dois sobem com a demanda da rede (de 1x vazia até 4x cheia).
- **Sem taxa nem queima.** Quem usa a IA paga por token, 100% para a máquina que respondeu, menos o royalty que o
  padrinho escolheu (0 a 50%).
- **Votações.** A rede vota as próprias regras: 1 SIG = 1 voto, travado até o fim e devolvido inteiro (veja
  [Votações](#votações)).

## Novidades da 2.1

- **Pedidos de IA cifrados.** O app sorteia uma das máquinas que têm o modelo, cifra a pergunta para a chave dela, e
  a resposta volta cifrada para a sua chave: o servidor e o resto da rede veem só bytes embaralhados. Cada nova
  pergunta é sorteada de novo e leva a conversa inteira, então numa conversa longa várias máquinas de pessoas
  diferentes leem tudo o que você escreveu. Mas a máquina que responde precisa ler o pedido, e ela é o computador de
  outra pessoa: o app oficial não guarda o que lê, só que quem controla a máquina pode mudar o programa. Continua
  público que houve um pedido, de qual carteira, para qual modelo e máquina, quantos tokens, quanto custou e quando.
  Não mande senha, documento nem segredo. Se ninguém responde a tempo, o app tenta outra máquina, e o valor
  reservado volta para a carteira; dá para cancelar um pedido que ninguém pegou.
- **Sigma Code no painel (aba Programar).** O agente de programação roda dentro do app, com a carteira do app, sem
  terminal. Só mexe na pasta escolhida; cada comando passa por você antes de rodar, e cada mudança em arquivo
  também, a não ser que você marque “Aprovar alterações de arquivos sem perguntar” (desmarcada em toda tarefa
  nova). Arquivos de senha e chaves (`.env`, `.key`, `.ssh` e parecidos) ficam de fora. Cada passo do Sigma Code vai
  para uma máquina sorteada e leva tudo o que o agente já leu: ao longo de uma tarefa, várias máquinas de outras
  pessoas veem os arquivos.
- **Vários PCs na mesma carteira**, como explicado acima, e o melhor relatório de cada janela é o que vale
  (ninguém consegue apagar os pontos de outro minerador).
- **Mais honestidade nas contas:** o histórico da carteira mostra aluguéis, pagamentos por pedidos, royalties,
  trocos e devoluções; o preço da hospedagem é da rede (igual para todas as máquinas); sites publicados rodam
  isolados (sem cookies nem localStorage), longe da carteira e do fórum.

## O que ainda falta

Dizemos o que funciona e o que ainda não funciona (a lista completa está em `/tutorial#limites`):

- **Um validador só.** Hoje um único servidor, mantido pelo projeto, assina os blocos. Na prática, ele pode atrasar
  ou recusar transações, e é ele quem decide se adota uma atualização de regras. Desde a 2.4 as mudanças de
  regra entram sem recomeçar a rede; reiniciar do zero (`RESET=1`, com todos os saldos) só para o que não cabe
  nesse modelo (veja [docs/ATUALIZACOES.md](docs/ATUALIZACOES.md)). O próximo passo é ter vários validadores.
- **Sites passam pelo servidor da rede.** As máquinas guardam cópias e recebem aluguel, mas quem visita um site
  recebe os arquivos pelo servidor do projeto. Se ele cair, os sites saem do ar até ele voltar.
- **Sem prova de armazenamento e sem auditoria dos tokens cobrados.** A rede ainda confia no que cada máquina diz
  que guarda e em quantos tokens diz que usou.
- **Sem domínio próprio e sem assinatura digital do `.exe`.**

## Emissão

- Teto: **100.000.000 SIG**.
- A regra: **0,01% do que ainda falta a cada 12 h** (`mint_bps = 1`), impressa **um pouco em cada bloco**, na
  proporção do tempo desde o bloco anterior. Saiam muitos blocos ou poucos, a soma em 12 h é a mesma.
- Cada pedaço é dividido entre quem minerou na **última janela de 5 minutos já fechada**, na proporção do trabalho
  (prova de trabalho scrypt, conferida pela rede). O pagamento anda uns 10 minutos atrás do trabalho. Tempo sem
  ninguém minerando não imprime nada: o SIG continua esperando.
- Os números, com a conta da chain (a cada 12 h, `minted += (cap - minted) * 1 // 10000`):

  | Quando | Sai em 12 h | Total impresso | Do teto |
  |---|---:|---:|---:|
  | 1º período (12 h) | 10.000 | 10.000 | 0,01% |
  | 30 dias | 9.941 | 598.233 | 0,6% |
  | 1 ano | 9.297 | 7.040.256 | 7,0% |
  | 5 anos | 6.943 | 30.581.602 | 30,6% |
  | 9,5 anos | 4.999 | 50.019.371 | 50,0% |
  | 20 anos | 2.322 | 76.778.068 | 76,8% |
  | 40 anos | 539 | 94.607.419 | 94,6% |

  Nas primeiras 12 horas são uns 833 SIG por hora para a rede inteira. A taxa pode mudar por votação (de 0 a 0,5%);
  o teto, não.
- **Não existe outro jeito de criar SIG**: sem SIG de boas-vindas, sem faucet, sem pré-mineração (nem para
  os criadores), sem taxa nem queima.
- Fora a emissão, o SIG só **circula**: quem usa IA, computação e sites paga quem hospeda; quem coloca um
  modelo na rede (o padrinho) paga a hospedagem dele e recebe royalty de cada uso.

### Quanto custa usar a rede

- **IA:** quem pergunta paga por token (preço padrão: 0,002 SIG a cada mil tokens), e o pagamento vai **100% para a
  máquina que respondeu**. A única parte que não fica com ela é o royalty que o padrinho do modelo escolheu (0 a
  50%). A rede não cobra taxa e não queima nada.
- **Aluguel de sites e modelos**, pago do orçamento a cada janela de 5 minutos para cada máquina que guarda uma
  cópia: disco a **5 µSIG por MB** e, nos modelos, memória reservada a **2 µSIG por MB**, cada um vezes a demanda da
  rede (`mult = 1 + 3 × u²`, com `u` = quanto do disco ou da memória das máquinas online já está pedido: 1x com a
  rede vazia, até 4x cheia). Exemplos: um site de 2 MB em 2 máquinas custa uns 0,006 SIG por dia com a rede vazia
  (até 0,023 cheia); um modelo de 1,1 GB (uns 1,8 GB de memória) em 2 máquinas, uns 5,3 SIG por dia (até 21 cheia).

## Votações

As regras da rede são votadas por quem participa.

- **Qualquer carteira propõe**, deixando um depósito de 10 SIG que volta no fim (aprovada ou não). No máximo 20
  votações abertas ao mesmo tempo.
- **Cada votação dura 10 dias** (pelo relógio dos blocos).
- **1 SIG = 1 voto.** O SIG do voto fica travado até o fim e volta inteiro: assim o mesmo SIG não vota duas vezes,
  nem passando por outra carteira. Uma carteira vota uma vez por votação.
- **Aprovada** com mais “sim” que “não” e votos de pelo menos **1% do SIG que existe** (o quórum).
- **De parâmetro:** se aprovada, a regra muda na hora. Dá para votar: a emissão (`mint_bps`, 0 a 0,5%), o aluguel
  de disco e de memória, quanto a demanda sobe o aluguel, a dificuldade mínima da mineração, o depósito e o
  quórum. **De texto:** registra a decisão da rede, sem mexer no código — é assim que a rede aprova uma mudança
  de regra antes de ela ser programada e marcada num bloco ([docs/ATUALIZACOES.md](docs/ATUALIZACOES.md)).
- **Não se vota:** o teto de 100 milhões e o tempo da rede (janelas de 5 minutos, períodos de 12 horas). Nenhuma
  votação mexe no saldo de ninguém.
- **Como votar:** no app, na aba **Votações**, ou pela linha de comando: `python -m sigma.cli governance` (lista),
  `python -m sigma.cli propose "título" --text "explicação" [--param mint_bps --value 2]` e
  `python -m sigma.cli vote ID sim 100`. A Carteira do site mostra as votações, só para acompanhar.
- **O risco, dito com todas as letras:** quem tem mais SIG tem mais votos. Sem cadastro, a rede não sabe quem é
  pessoa, só quanto SIG cada carteira trava. E, enquanto houver um validador só, o resultado depende desse
  servidor seguir o código.

O SIG não é investimento, não tem preço de mercado e ninguém promete valorização: é a forma de pagar e de ser
pago dentro da rede.

## Código-fonte e licença

O código-fonte está disponível: o `sigma.pyz` é um zip com o código em Python, e quem tem esta pasta roda direto
com `python -m sigma.app`. A licença ainda não foi escolhida pelo dono do projeto. Até lá, ler e conferir pode;
copiar, modificar e redistribuir dependem da licença. Por isso o site e este README dizem
“código-fonte disponível”: o nome “aberto” fica para quando houver licença.

## Para desenvolvedores

Python 3.11+ puro, só biblioteca padrão (nada de pip).

| Comando | O que faz |
|---|---|
| `python -m sigma.app` | o app: painel numa janela + janelinha de controle (dados em `%LOCALAPPDATA%\Sigma` ou `~/.sigma`) |
| `python -m sigma.app --headless --wallet sig…` | sem interface (servidores): minera e hospeda para esse endereço |
| `python -m sigma.node init --dir dados/no1` | genesis de uma rede nova (testes: `--epoch-seconds 30 --window-seconds 5 --min-bits 4`) |
| `python -m sigma.node run --dir dados/no1 --port 8700 --producer` | nó validador: site, carteira, fórum, `/mining`, `/downloads`, MetaMask em `/rpc` |
| `python rede_local.py` | rede local que fica rodando: nó rápido + app sem janela, com o motor de IA de teste e um modelo de teste que entra sozinho depois da primeira época (o endereço do painel aparece no terminal; `--janela` abre a janela; `--dados PASTA`, `--porta N`, `--sem-modelo`) |
| `python demo.py` | demo completa: 2 nós, 3 máquinas, emissão, IA cifrada (pedido e resposta), cancelamento, falha "context" com o valor devolvido, site isolado, computação com trapaceiro, modelo sem orçamento |
| `python demo.py --ollama` | a mesma demo com o `qwen2.5-coder:1.5b` que já está no seu Ollama |
| `python -m unittest discover -s tests -v` | testes rápidos (ledger, nó, app, site…) |
| `SIGMA_INTEGRATION=1 python -m unittest discover -s tests -v` | + integração com processos de verdade: minerador, motores, a máquina inteira e a demo (alguns minutos; cada um também roda direto, ex.: `python tests/test_miner.py`) |
| `python -m unittest tests.test_site -v` | confere os textos e as páginas do site; com o Edge instalado, também abre cada página sem internet, em 375 e 1366 px |
| `python tests/mock_app_api.py --check` | confere o painel (`app.html`) contra a API do app; com o Edge instalado, abre todas as abas e os cenários de falha sem internet |
| `python packaging/build_pyz.py` | gera `dist/sigma.pyz` e atualiza `dist/SHA256SUMS.txt` |
| `python packaging/build_windows.py` | gera `dist/Sigma.exe` com ícone Σ (precisa do PyInstaller, no Windows) |
| `bash deploy/deploy_vps.sh` | atualiza a VPS e publica o que houver em `dist/` (a chain e os saldos continuam; inclusive numa atualização de regras, ver [docs/ATUALIZACOES.md](docs/ATUALIZACOES.md)) |
| `RESET=1 bash deploy/deploy_vps.sh` | recria a chain da VPS do zero, sem nenhum SIG pré-minerado: todos os saldos recomeçam em zero (os dados antigos são guardados, o fórum continua). Só para o que uma atualização de regras não resolve |
| `PAYOUT=sig… bash deploy/deploy_vps.sh` | define a carteira que recebe o que a VPS ganha hospedando sites |

Onde fica cada coisa: `sigma/chain.py` (ledger: emissão, trabalho, máquinas, jobs, modelos, sites, aluguel, votações),
`sigma/pow.py` (prova de trabalho), `sigma/node.py` (nó HTTP), `sigma/app.py` + `sigma/web/app.html` (o app
e o painel), `sigma/miner.py` (minerador), `sigma/host.py` (a máquina que hospeda), `sigma/engines.py`
(Ollama, llama.cpp, mock), `sigma/client.py` e `sigma/cli.py` (cliente e linha de comando), `sigma/crypto.py`
(chaves, assinaturas e a cifra dos pedidos de IA), `sigma/sigmacode.py` (agente de programação que usa a IA da
rede) e `sigma/web/` (o site: manifesto, download, tutorial, carteira e fórum). A especificação está em
`docs/SPEC-v2.md`, com as mudanças da 2.1, 2.2 e 2.3 em `docs/SPEC-v2.1.md`, `docs/SPEC-v2.2.md` e
`docs/SPEC-v2.3.md` (emissão contínua, aluguel pela demanda e votações).

Rede pública: **https://sigma.195-35-29-232.sslip.io** (pm2 + nginx na VPS do GardenCounties). Essa VPS é, por
enquanto, o único validador: assina os blocos e entrega os sites, mas não minera nem roda IA, para não disputar
CPU com o jogo.
