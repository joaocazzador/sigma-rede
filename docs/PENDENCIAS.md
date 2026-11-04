# Pendências (anotado em 04/11/2026)

A 2.6 subiu só com a caução dos coordenadores (docs/SPEC-v2.6.md), para caber no limite de uso da semana. Tudo
abaixo ficou para a próxima, na ordem em que eu faria.

## 1. Testes deixados para depois

- Suíte inteira (`python -m unittest discover -s tests`). Na 2.6 rodaram: chain, chain_v21, chain_v23, chain_v25,
  chain_v3, upgrades, node, client e app.
- **Edge sem janela parou de responder nesta máquina** (Edge 156.0.4314.58: `--dump-dom` não devolve nada nem
  numa página trivial). Por isso falharam os 9 testes de `tests.test_site.TestNoEdge`; os outros 124 testes de app,
  crypto e site passaram. Provável atualização do Edge pendente. Reiniciar o Edge ou o PC e rodar de novo, junto
  com a conferência do painel (`python tests/mock_app_api.py --check`, ~10 min) e o demo de ponta a ponta. As
  mudanças de página da 2.6 são só os nomes do `min_bond` em `app.html` e `carteira.html`.
- Teste do `.exe` novo numa máquina limpa.
- Revisão adversária da caução v3 (dinheiro e consenso). Na 2.6 só eu revisei, rapidamente. A reexecução dos 2.628
  blocos reais da rede com o código novo deu o mesmo estado.

## 2. Medições que faltaram (a internet caiu no meio)

- **Determinismo entre CPUs.** Ver se o mesmo modelo dá o mesmo texto e os mesmos logits com variantes de CPU,
  threads e lote, e qual tolerância usar. O trabalho parcial está em `C:\sgtmp\shard2\determinismo\`.
- **Custo da conferência.** Comparar gerar com conferir a resposta pronta numa passada só (teacher forcing), com
  RTT de 0, 50 e 100 ms, e ver se a conferência pega um token trocado.
- Já medidos e guardados:
  - **Porteiro** (`C:\sgtmp\af-porteiro.md`; protótipo em `C:\sgtmp\shard2\porteiro\`): barrou 13 de 13 ataques
    com o texto idêntico, a −6% de velocidade.
  - **Coordenador leve** (`C:\sgtmp\af-leve.md`; protótipo em `C:\sgtmp\shard2\leve\`): GGUF esparso com o texto
    idêntico. O coordenador de um 70B fica só com embedding e output (~2 GB), e o manifesto por tensor tem SHA-256 e
    FNV.

## 3. Desenho antifraude

Já decidido com o dono: coordenadores com caução (feito na 2.6), conferência por mais de um coordenador, corte e
reputação. Falta escrever e revisar o desenho completo, com estas partes:

- quem gera e quem confere (1 gera e 2 conferem é o favorito, se a conferência for barata);
- sorteio pela chain;
- recibos assinados;
- disputa e quem julga;
- pedidos-teste disfarçados;
- quem chama `_slash`;
- rateio do pagamento entre o coordenador e os trabalhadores.

## 4. Regras v4 (a divisão em si)

- Modelo em partes, com manifesto por tensor.
- Papéis de coordenador e de trabalhador.
- Prazo maior por pedido.
- Correções de `_prune` e `pin_model` levantadas em `C:\sgtmp\shard-sintese.md`.

## 5. Máquina

- **Túnel cifrado.** O trabalhador disca para o coordenador (TLS 1.3 PSK, canais, retomada em até 120 s). Já tem
  1.516 linhas em `wip/tunnel.py`, mas não tem testes nem revisão; está fora do pacote e do GitHub.
- **Porteiro** de verdade em `sigma/porteiro.py`. Falta decidir de onde vem a política de grafos de cada modelo.
- Job Object no trabalhador e PC trabalhador sem a chave da carteira (opção no painel).
- Planejador de camadas e supervisor do grupo.

## 6. Painel e site

- Caução no painel (travar, retirar, ver quando volta) e nas páginas da carteira e do tutorial.
- Linha do `min_bond` na tabela de parâmetros do tutorial.
- README.
