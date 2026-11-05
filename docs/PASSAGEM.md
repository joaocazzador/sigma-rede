# Passagem de bastão (04/11/2026, Sigma 2.8.0)

Tudo o que uma nova sessão precisa para continuar daqui. O que falta fazer está em [PENDENCIAS.md](PENDENCIAS.md).

## Onde as coisas estão

- **Código:** <https://github.com/joaocazzador/sigma-rede> (público, branch `main`). Não confundir com o repositório
  antigo e privado "SIGMA", que não é usado.
- **Rede no ar:** <https://sigma.195-35-29-232.sslip.io>.
  - O validador único e o "servidor-sigma" (que hospeda e minera) rodam na VPS do GardenCounties.
  - Os instaladores ficam em `/baixar`.
- **Pesquisa da divisão de modelos:** relatórios e protótipos em [pesquisa/](../pesquisa/).
  - Medições: RPC do llama.cpp, latência, porteiro e coordenador leve.
  - O teste de ponta a ponta do grupo: `pesquisa/grupo/e2e_grupo.py`.
  - Os caminhos `C:\sgtmp\…` citados nos relatórios são da máquina antiga; os arquivos que importam foram copiados
    para cá.

## Versões e regras da rede principal

A rede usa regras versionadas, que entram numa altura marcada sem recomeçar a chain (docs/ATUALIZACOES.md;
`chain.SCHEDULE`).

| Regras | Altura | Versão do app | O que trouxe |
|---|---|---|---|
| v1 | genesis | 2.4 | a rede recriada |
| v2 | 1950 | 2.5 | imagem e vídeo; 1.000.000 SIG da carteira dos fundadores (`0xfd19…a09e`); votação decidida antes com mais da metade |
| v3 | 2780 | 2.6 | caução dos coordenadores: `bond` e `unbond`, 14 dias para retirar, `_slash` pronto mas ainda sem quem o chame |
| v4 | 4032 | 2.8 | modelo de texto em partes, até 600 GB (os gigantes do catálogo) |
| v5 | 4300 | 2.10 | falha em imagem ou vídeo tira 1 de reputação; a memória de IA conta só o maior modelo da máquina (eles se revezam na RAM), até 8 modelos por máquina |
| v6 | 5800 | 2.11 | o cliente libera o pedido de uma máquina que sumiu da rede (release_job): valor de volta na hora, falta para a máquina |
| v7 | 6870 | 2.12 | corrida (até 2 máquinas ociosas a mais; a primeira que entrega vence) e vigia (a entrega fica "review"; uma máquina sorteada aprova ou reprova; 5% para ela; o cliente contesta uma vez; sem veredito em 30 min paga sozinho) |

- A v4 foi marcada para ~23h20 de 04/11. Confira em `/mining`, que deve mostrar `"parts": true`.
- Versões que não mudaram regras:
  - 2.6.1: catálogo com 26 modelos;
  - 2.7: grupo, que divide um modelo entre PCs pelo túnel cifrado (docs/GRUPO.md);
  - 2.7.1: o painel explica por que um modelo espera computadores.

## Mapa do código

| Arquivo | Conteúdo |
|---|---|
| `sigma/chain.py` | consenso determinístico: regras, `SCHEDULE`, `UPGRADE_GRANTS`, caução, partes |
| `sigma/node.py` | nó HTTP (validador ou seguidor) |
| `sigma/app.py` | app do usuário: painel local em `sigma/web/app.html`, linha de comando, `--headless` |
| `sigma/host.py` | a máquina: registro, modelos, pedidos de IA |
| `sigma/engines.py` | motor de texto: llama.cpp b11721 ou Ollama |
| `sigma/media.py` | motor de imagem e vídeo: stable-diffusion.cpp; o vídeo se divide num grupo de vídeo (docs/GRUPO-VIDEO.md) |
| `sigma/grupo.py` | coordenador e trabalhador do grupo |
| `sigma/tunnel.py` | túnel TLS 1.3 PSK com retomada; ainda sem revisão de segurança nem testes de estresse |
| `sigma/updater.py` | atualização automática do .exe e do .pyz (baixa do nó, confere o SHA-256, troca e reabre) |
| `sigma/catalog.py` | catálogo de modelos, com tamanhos e SHA-256 do Hugging Face (`pesquisa/hf_consulta.py`) |
| `sigma/client.py`, `sigma/cli.py` | cliente HTTP e linha de comando |
| `tests/` | `python -m unittest discover -s tests` |

## Como testar

Bateria principal, ~3 min, 488 testes passando na 2.8:

```bash
python -m unittest tests.test_chain tests.test_chain_v21 tests.test_chain_v23 tests.test_chain_v25 tests.test_chain_v3 tests.test_chain_v4 tests.test_upgrades tests.test_node tests.test_client tests.test_app tests.test_grupo tests.test_host_media tests.test_media
```

- **Problema conhecido:** o Edge sem janela parou de responder nesta máquina. Por isso falham os
  `tests.test_site.TestNoEdge` e a conferência do painel (`python tests/mock_app_api.py --check`).
- **Antes de marcar regra nova, reexecute a chain real.** Copie `chain.jsonl` e `genesis.json` de
  `/opt/sigma/data/no/` na VPS para uma pasta vazia e abra com `sigma.node.Node(pasta, genesis)`. O nó reexecuta
  bloco a bloco e confere cada `state_root`.

## Como publicar

1. Suba a versão em `sigma/__init__.py`.
2. Gere os instaladores com `python packaging/build_pyz.py` e `python packaging/build_windows.py`.
3. Faça o commit.
4. Rode `bash deploy/deploy_vps.sh`, **sem `RESET=1`**. Ele envia o código e os instaladores e reinicia só
   `sigma-node` e `sigma-hosting`.
5. Faça o `git push`.

Acesso à VPS: `ssh -i ~/.ssh/garden_vps root@195.35.29.232`. O Python de lá é o 3.14.

## Regras do dono do projeto (valem sempre)

- Textos para pessoas em português do Brasil, no estilo poster do site.
- **Pedir confirmação antes de publicar** (deploy e push), a menos que o pedido já diga para publicar.
- **Não afetar o GardenCounties:** não mexer nos outros apps do pm2 (garden-api, garden-web, nicher-api, nicher-web,
  cookintime) e rodar `nginx -t` antes de qualquer mudança no nginx.
- **Nunca recomeçar a chain sem pedir.** Se for preciso, os dados antigos vão para outra pasta e o `forum.db` fica.
- **Fórum público:** não postar nada de teste no fórum sem perguntar.
- **Downloads:** baixar só o que já foi aprovado (llama.cpp, stable-diffusion.cpp e modelos do catálogo); o resto
  precisa de permissão.
- **Regra nova de consenso:** sempre por altura marcada (`SCHEDULE`), com aviso, sem mudar a regra antiga, e com a
  chain real reexecutada antes.
- **A VPS minera** para `0xfd1980713ecf1a514ad3f4a15828bb2d0c00a09e` com `chrt --idle` e 100% da CPU
  (`deploy/ecosystem.config.cjs`).
- **Economia:** o dono pediu para economizar uso, ser objetivo e deixar os testes de estresse para depois.

## Próximo passo sugerido

1. Confirmar a v4 no ar.
2. Ajudar a montar a demonstração do grupo com PCs reais (docs/GRUPO.md).
3. Depois disso, seguir [PENDENCIAS.md](PENDENCIAS.md), em ordem de prioridade:
   - o grupo no painel, porque hoje só funciona pela linha de comando;
   - o rateio do pagamento com os trabalhadores;
   - os testes de estresse do túnel;
   - o porteiro;
   - a antifraude, com quem chama `_slash`.
