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
