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

**Vídeo e imagem não se dividem.** O motor deles (stable-diffusion.cpp) não tem conexão entre máquinas, como o RPC do
llama.cpp. Eles rodam inteiros num computador com memória suficiente: o Wan 2.1 14B pede ~21 GB, então cabe num PC
de 32 GB.
