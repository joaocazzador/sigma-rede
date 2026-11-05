# Grupo: um modelo dividido entre vários computadores (2.7)

Um **coordenador** roda o modelo e manda as camadas para os **trabalhadores**, que emprestam a memória deles.
Funciona com o llama.cpp de sempre, pelo RPC dele.

- Todo o tráfego passa por um túnel cifrado, que o trabalhador disca. Ninguém abre porta em casa; só o coordenador
  escuta.
- Para a rede, o grupo é uma máquina só. O coordenador declara a própria memória somada à dos trabalhadores
  conectados, e por isso a rede passa a colocar nele modelos que não cabem em nenhum dos PCs sozinho.
- Os pedidos e os pagamentos são do coordenador. O rateio com os trabalhadores fica para depois
  (docs/PENDENCIAS.md).

## Como montar a demonstração

1. Em cada PC, abra o Sigma uma vez para a máquina se registrar na rede. Anote o endereço da máquina: aparece no
   registro como `[máquina] sig… ligada`.
2. **Coordenador.** Precisa de IP público ou estar na mesma rede local, e da porta 7710 liberada no firewall. Ele
   guarda o arquivo inteiro do modelo, então precisa de disco para isso:

   ```bash
   Sigma.exe --headless --coordinate 0.0.0.0:7710 --group-worker <máquina do PC 2>=10 --group-worker <máquina do PC 3>=10
   ```

3. **Cada trabalhador:**

   ```bash
   Sigma.exe --headless --worker-of <máquina do coordenador>@<IP do coordenador>:7710
   ```

4. Quando os trabalhadores conectam, o coordenador se registra de novo com a memória somada. Apadrinhe um modelo
   maior que qualquer PC do grupo: o Qwen3 32B pede ~24 GB e cabe em 3 PCs de 16 GB emprestando 10 GB cada.
   Converse com ele no painel de qualquer computador da rede.

## O que foi conferido

- Coordenador e 2 trabalhadores pelo túnel, com o llama.cpp b11721 de verdade e o Qwen 0.5B. O texto saiu
  **idêntico** ao de um PC só.
- O túnel não atrasa: o RPC direto, sem túnel, levou o mesmo tempo. Num PC só os três processos disputam o
  processador; em PCs separados, cada um usa o seu.
- Teste: `C:\sgtmp\grupo-e2e\e2e.py` e `tests/test_grupo.py`.

## Limites desta versão

- O modelo ainda é um arquivo só, de até 60 GB, que é o teto da chain. Os gigantes do catálogo esperam as regras
  v4.
- Se um trabalhador cai, a resposta em andamento falha, e o coordenador recarrega o modelo com quem sobrou.
- Os trabalhadores são escolhidos pelo dono do coordenador. Ainda não há sorteio pela chain, conferência entre
  coordenadores nem porteiro: use com PCs de confiança.
- O túnel precisa do Python 3.13 ou mais novo (o `.exe` já vem com o 3.14).

## Modelos gigantes em partes (2.8, regras v4)

- A partir do bloco 4032, um modelo de texto pode vir em várias partes: o GGUF dividido do Hugging Face.
  - Cada parte tem até 100 GB, e o modelo inteiro até 600 GB.
  - Os 5 gigantes do catálogo passam a ter o botão "Apadrinhar".
- O coordenador baixa todas as partes, confere o SHA-256 de cada uma, dá a elas os nomes que o llama.cpp procura
  (com links, sem copiar) e espalha as camadas pelos trabalhadores. Testado com o modelo de teste em 3 partes pelo
  túnel: texto idêntico.
- A rede só coloca um gigante num coordenador cuja memória somada (a dele mais a emprestada) cabe no modelo, e que
  tem disco para o modelo inteiro.

| Modelo | Memória somada | Disco no coordenador |
|---|---|---|
| gpt-oss 120B | ~77 GB, uns 8 trabalhadores emprestando 10 GB | 63 GB |
| Qwen3 235B | ~171 GB | 142 GB |

**Vídeo (2.9, em teste):** o stable-diffusion.cpp também tem RPC, compilado à parte. Com `--group-kind video`, o
modelo de difusão do Wan se divide entre os trabalhadores: veja [GRUPO-VIDEO.md](GRUPO-VIDEO.md). A imagem continua
rodando inteira num computador só.


## Grupos automáticos (2.14, regras v8)

A rede monta os grupos sozinha. Ninguém precisa configurar nada.

- **Quem entra.** Toda máquina com "Emprestar memória para grupos" ligado (o padrão) declara `lend` no registro e
  entra no sorteio. Uma máquina fica em um grupo de cada vez.
- **Como a rede monta.** Na virada da janela, um modelo de texto que não cabe em nenhuma máquina ganha um grupo por
  réplica. O coordenador é quem tem disco para o modelo inteiro e a maior reputação (a caução desempata). Os
  trabalhadores são os de maior reputação com pelo menos 2 GB livres, até a memória somada cobrir o modelo (até 64
  membros). O grupo fica em `ext["groups"]` e cada máquina vê o seu papel em `/machine/<endereço>` (`groups`).
- **Cópias que correm.** Cada réplica é um grupo completo, então cada parte do modelo tem a sua cópia no outro
  grupo. Os pedidos correm entre os grupos (regras v7): o mais rápido leva o pagamento e a reputação.
- **Pagamento e aluguel.** Repartidos pela memória de cada membro (o coordenador conta a parte dele). Todos os
  membros ganham reputação quando o grupo entrega.
- **Grupo que se desfaz.** Se algum membro some (sem sinalizar nesta janela nem na anterior) ou o coordenador larga o
  modelo, o grupo acaba, quem sumiu perde 1 de reputação e a rede monta outro na janela seguinte.
- **Vigia e punição.** A vigia sempre existe: se ninguém mais hospeda o gigante, vigia qualquer máquina confiável, fora
  do grupo. Trapaça custa caro: quem entregou perde 10 de reputação e metade da caução (parte vai para a vigia e para o
  cliente), e cada trabalhador do grupo perde 3.

### O relé (atrás de roteador)

PCs de casa não recebem conexões. Por isso o nó público roda um relé (`--relay-port`, na VPS a porta 7790,
`sigma/relay.py`). O coordenador e os trabalhadores discam o relé; ele confere pela assinatura de cada máquina que ela é
mesmo do grupo e só repassa bytes. O túnel do grupo (TLS 1.3 com a chave das máquinas) passa cifrado por dentro: o
relé não lê nada.

Nas pontas tudo continua igual: o coordenador escuta o túnel em 127.0.0.1 e o trabalhador disca 127.0.0.1; os agentes
`RelayHost` e `RelayJoin` fazem a ponte.

### O que esperar

- Pela internet, cada palavra passa por todos os computadores do grupo, um depois do outro. Num grupo de 5, a resposta
  sai em poucas palavras por segundo: bem mais lenta que num computador só, mas roda um modelo que não caberia em
  nenhum.
- A primeira carga manda as camadas do modelo do coordenador para cada trabalhador, pelo relé. Depois o `rpc-server` de
  cada trabalhador guarda as camadas em cache (`-c`) e a próxima carga é rápida.
- Ainda não testado com PCs de verdade pela internet: os testes cobrem a chain, a máquina e o túnel passando pelo relé
  (`tests/test_chain_v8.py`, `tests/test_host_groups.py`, `tests/test_relay.py`).
