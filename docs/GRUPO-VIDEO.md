# Grupo de vídeo: o modelo de vídeo dividido entre computadores (2.9, em teste)

O grupo de texto (docs/GRUPO.md) espalha as camadas do llama.cpp pelos trabalhadores. O de vídeo faz o mesmo com o
modelo de difusão do Wan, usando o RPC do stable-diffusion.cpp.

## Como funciona

- **Coordenador.** Roda o `sd-cli` com:
  - `--rpc-servers 127.0.0.1:<ponte>,…`: as pontes do túnel cifrado, uma por trabalhador;
  - `--backend "diffusion=RPC0&RPC1&…&CPU,te=CPU,vae=CPU"`: os blocos do transformador do Wan vão para os
    trabalhadores e para o coordenador, na proporção da memória livre de cada um; o codificador de texto (UMT5) e o
    VAE ficam no coordenador.
- **Trabalhador.** Roda o `rpc-server` compatível com o stable-diffusion.cpp e disca o coordenador pelo túnel, igual
  ao de texto. Não precisa ter o arquivo do modelo: o coordenador envia os pesos.
- **Na chain**, o grupo é uma máquina só: o coordenador declara a memória dele somada à emprestada e hospeda **só
  vídeo** (e imagem, se ligada). Ele não declara texto, porque a memória emprestada não serve para o llama.cpp.
- **Um grupo é de texto ou de vídeo.** Os dois `rpc-server` são de versões diferentes do ggml e não se misturam.

## Os dois programas que o pacote oficial não traz

O pacote pronto do stable-diffusion.cpp vem sem RPC. Os dois programas são compilados do código-fonte.

1. **`sd-cli` com RPC.** O stable-diffusion.cpp na versão fixada `master-1026-997478a`, com `-DSD_RPC=ON`.
2. **`rpc-server`.** O llama.cpp no commit de `ggml/scripts/sync-llama.last` (`47f5a64a…`), com `-DGGML_RPC=ON` e
   `GGML_MAX_NAME=160`. Sem isso, os nomes dos tensores do Wan não cabem e a transferência falha.

Onde o app procura:

| Programa | Variável | Lugar padrão |
|---|---|---|
| `sd-cli` com RPC (coordenador) | `SIGMA_SD_CLI_RPC` | `<dados>/engine/sd-rpc/sd-cli.exe` |
| `rpc-server` (trabalhador) | `SIGMA_SD_RPC` | `<dados>/engine/sd-rpc/rpc-server.exe` |

Script de compilação no Windows: `packaging/build_sd_rpc.ps1`. Ainda falta publicar os binários em `/baixar` e o
app baixá-los sozinho (com SHA-256 conferido), como faz com o llama.cpp.

## Como montar

```bash
# coordenador
python -m sigma.app --coordinate 0.0.0.0:7710 --group-kind video --group-worker <endereço>=10 --group-worker <endereço>=10
# cada trabalhador
python -m sigma.app --worker-of <endereço do coordenador>@<ip>:7710 --group-kind video
```

## Teste de ponta a ponta

`pesquisa/grupo/e2e_video.py` gera o mesmo vídeo duas vezes: num PC só e dividido entre rpc-servers locais. Depois
compara os resultados.

## Limites

- **Rede.** A cada passo da geração, as ativações do vídeo cruzam a rede entre os blocos. A documentação do sd.cpp
  recomenda mais de 1 Gbps. Pela internet de casa, o vídeo dividido sai bem mais lento que num PC só que caiba o
  modelo. O ganho é rodar o Wan 14B somando a memória de PCs pequenos.
- **Segurança.** O `rpc-server` não tem senha nem cifra. Por isso ele só escuta em 127.0.0.1 e o tráfego passa pelo
  túnel cifrado, igual ao de texto.
- **Pendente.** O rateio do pagamento com os trabalhadores e a antifraude, como no grupo de texto (PENDENCIAS.md).
