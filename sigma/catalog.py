"""Catálogo de modelos abertos que qualquer pessoa pode apadrinhar na rede.

Só entram modelos cuja licença permite redistribuir os pesos e usar comercialmente
(as máquinas da rede baixam e servem o arquivo). Modelo sem licença declarada também
fica de fora: sem ela não dá para saber se pode cobrar. Tamanhos e SHA-256 são os do
Hugging Face; a rede confere o hash de cada download.
"""
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request

HF = "https://huggingface.co"
MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{2,47}$")   # mesma regra da chain
MAX_SIZE = 60_000_000_000


MEDIA_RAM_MB = {"text": 512, "image": 1536, "video": 4096}   # mesma conta da chain (chain.MEDIA_RAM_MB)


def ram_mb(size, task="text"):
    """Memória que uma máquina precisa reservar para servir o modelo (mesma conta da chain)."""
    return math.ceil(size / 1e6 * 1.2) + MEDIA_RAM_MB.get(task, 512)


USES = ("chat", "code", "reason")   # para que serve um modelo de texto (as classes da tela de modelos)


def _item(id_, name, repo, file, size, sha256, license, description, task="text", files=(), recipe=None, use="chat"):
    """Um modelo do catálogo. Imagem/vídeo (regras v2): `files` são os arquivos extras (VAE, codificador de
    texto) como (papel, repo, arquivo, tamanho, sha256) e `recipe` os passos e a escala que o modelo pede."""
    extra = [{"role": role, "url": f"{HF}/{r}/resolve/main/{f}", "sha256": h, "size": n}
             for role, r, f, n, h in files]
    total = size + sum(x["size"] for x in extra)
    item = {"id": id_, "name": name, "repo": repo, "file": file, "url": f"{HF}/{repo}/resolve/main/{file}",
            "size": size, "sha256": sha256, "license": license, "description": description,
            "ram_mb": ram_mb(total, task), "task": task, "use": use if task == "text" else task}
    if task != "text":
        item.update(files=extra, total_size=total, recipe=dict(recipe or {}))
    return item


CATALOG = [
    _item("qwen2.5-0.5b", "Qwen2.5 0.5B Instruct", "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
          "qwen2.5-0.5b-instruct-q4_k_m.gguf", 491400032,
          "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db", "Apache-2.0",
          "Minúsculo e rápido: roda em quase qualquer PC, bom para respostas curtas e tarefas simples."),
    _item("qwen2.5-1.5b", "Qwen2.5 1.5B Instruct", "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
          "qwen2.5-1.5b-instruct-q4_k_m.gguf", 1117320736,
          "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e", "Apache-2.0",
          "Pequeno e equilibrado: conversa bem em português e cabe em PCs com pouca memória."),
    _item("qwen2.5-coder-1.5b", "Qwen2.5 Coder 1.5B Instruct", "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
          "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf", 1117320768,
          "cc324af070c2ecbfd324a30884d2f951a7ff756aba85cb811a6ec436933bb046", "Apache-2.0",
          "Programador leve: escreve, completa e explica código em dezenas de linguagens.", use="code"),
    _item("smollm2-1.7b", "SmolLM2 1.7B Instruct", "bartowski/SmolLM2-1.7B-Instruct-GGUF",
          "SmolLM2-1.7B-Instruct-Q4_K_M.gguf", 1055609824,
          "77665ea4815999596525c636fbeb56ba8b080b46ae85efef4f0d986a139834d7", "Apache-2.0",
          "Compacto e treinado com dados abertos; bom para conversas simples (melhor em inglês)."),
    _item("deepseek-r1-qwen-1.5b", "DeepSeek R1 Distill Qwen 1.5B", "bartowski/DeepSeek-R1-Distill-Qwen-1.5B-GGUF",
          "DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf", 1117320800,
          "1741e5b2d062b07acf048bf0d2c514dadf2a48f94e2b4aa0cfe069af3838ee2f", "MIT",
          "Pensa passo a passo antes de responder: bom para matemática e lógica.", use="reason"),
    _item("phi-3.5-mini", "Phi-3.5 mini Instruct", "bartowski/Phi-3.5-mini-instruct-GGUF",
          "Phi-3.5-mini-instruct-Q4_K_M.gguf", 2393232672,
          "e4165e3a71af97f1b4820da61079826d8752a2088e313af0c7d346796c38eff5", "MIT",
          "Da Microsoft: raciocínio forte para o tamanho e contexto longo."),
    _item("mistral-7b-v0.3", "Mistral 7B Instruct v0.3", "bartowski/Mistral-7B-Instruct-v0.3-GGUF",
          "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf", 4372812000,
          "1270d22c0fbb3d092fb725d4d96c457b7b687a5f5a715abe1e818da303e562b6", "Apache-2.0",
          "Generalista robusto de 7B: escreve, resume e conversa bem; pede uns 6 GB de memória."),
    _item("qwen2.5-coder-7b", "Qwen2.5 Coder 7B Instruct", "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
          "qwen2.5-coder-7b-instruct-q4_k_m.gguf", 4683073536,
          "509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c", "Apache-2.0",
          "Um dos melhores modelos abertos de programação do seu tamanho; pede uns 6 GB de memória.", use="code"),
    _item("qwen3-8b", "Qwen3 8B", "Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", 5027783488,
          "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785", "Apache-2.0",
          "Geração nova da Qwen: pensa antes de responder quando a pergunta pede; pede uns 7 GB de memória."),
    _item("phi-4", "Phi-4 14B", "bartowski/phi-4-GGUF", "phi-4-Q4_K_M.gguf", 9053114816,
          "009aba717c09d4a35890c7d35eb59d54e1dba884c7c526e7197d9c13ab5911d9", "MIT",
          "Da Microsoft: muito bom em matemática, lógica e código; pede uns 12 GB de memória.", use="reason"),
    _item("qwen3-14b", "Qwen3 14B", "Qwen/Qwen3-14B-GGUF", "Qwen3-14B-Q4_K_M.gguf", 9001752960,
          "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0", "Apache-2.0",
          "Generalista forte, bom em português e com raciocínio passo a passo; pede uns 12 GB de memória."),
    _item("gpt-oss-20b", "gpt-oss 20B", "ggml-org/gpt-oss-20b-GGUF", "gpt-oss-20b-MXFP4.gguf", 12109566624,
          "27cd6c432c7672cb812a92f611cf3ba7bbc35928262bb1e1253ff4ee6ae35901", "Apache-2.0",
          "O modelo aberto da OpenAI: raciocina bem e é rápido para o tamanho (só parte dele trabalha em cada "
          "palavra); pede uns 15 GB de memória."),
    _item("mistral-small-3.1-24b", "Mistral Small 3.1 24B", "bartowski/mistralai_Mistral-Small-3.1-24B-Instruct-2503-GGUF",
          "mistralai_Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M.gguf", 14333910496,
          "c5743c1bf39db0ae8a5ade5df0374b8e9e492754a199cfdad7ef393c1590f7c0", "Apache-2.0",
          "Da Mistral: escreve muito bem, segue instruções com cuidado e tem contexto longo; pede uns 18 GB de "
          "memória."),
    _item("qwen3-30b-a3b", "Qwen3 30B-A3B", "Qwen/Qwen3-30B-A3B-GGUF", "Qwen3-30B-A3B-Q4_K_M.gguf", 18556685824,
          "0d003f6662faee786ed5da3e31b29c978de5ae5d275c8794c606a7f3c01aa8f5", "Apache-2.0",
          "Grande mas ágil: só 3B dos 30B trabalham em cada palavra, então responde rápido até no processador; "
          "pede uns 23 GB de memória."),
    _item("qwen3-32b", "Qwen3 32B", "Qwen/Qwen3-32B-GGUF", "Qwen3-32B-Q4_K_M.gguf", 19762149024,
          "efd971561896866f0e910cce52761ca77b1b138090c7f15fe284676d57d1f689", "Apache-2.0",
          "O maior Qwen3 que cabe num computador só: o mais caprichado da lista; pede uns 24 GB de memória."),
    # ---- mais modelos de texto (2.13): consultados no Hugging Face por pesquisa/hf_catalogo_novo.py
    _item("smollm2-135m", "SmolLM2 135M Instruct", "bartowski/SmolLM2-135M-Instruct-GGUF",
          "SmolLM2-135M-Instruct-Q8_0.gguf", 144811360,
          "5a1395716f7913741cc51d98581b9b1228d80987a9f7d3664106742eb06bba83", "Apache-2.0",
          "O menor do catálogo: respostas simples e instantâneas, roda até em PC muito antigo. Pede uns 0,7 GB de memória.", use="chat"),
    _item("smollm2-360m", "SmolLM2 360M Instruct", "bartowski/SmolLM2-360M-Instruct-GGUF",
          "SmolLM2-360M-Instruct-Q8_0.gguf", 386405280,
          "c004ac34cce45e03e4452bca472535bc371fcb0d07ae560bba2c57ce4a284b7b", "Apache-2.0",
          "Minúsculo e rápido, bom para tarefas curtas (resumir uma frase, classificar). Pede uns 1,0 GB de memória.", use="chat"),
    _item("qwen3-0.6b", "Qwen3 0.6B", "Qwen/Qwen3-0.6B-GGUF",
          "Qwen3-0.6B-Q8_0.gguf", 639446688,
          "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031", "Apache-2.0",
          "O Qwen3 mais leve: conversa e segue instruções simples, com modo de pensar. Pede uns 1,3 GB de memória.", use="chat"),
    _item("qwen2.5-coder-0.5b", "Qwen2.5 Coder 0.5B Instruct", "Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF",
          "qwen2.5-coder-0.5b-instruct-q8_0.gguf", 675710848,
          "e1a77721fa97d412f121878223eec81fb4ae6f271e18f922d746711f67b344d1", "Apache-2.0",
          "Programação em miniatura: completa trechos curtos de código num piscar de olhos. Pede uns 1,3 GB de memória.", use="code"),
    _item("tinyllama-1.1b", "TinyLlama 1.1B Chat", "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
          "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf", 668788096,
          "9fecc3b3cd76bba89d504f29b616eedf7da85b96540e490ca5824d3f7d2776a0", "Apache-2.0",
          "Pequeno e clássico: conversa leve em inglês, bom para testar a rede. Pede uns 1,3 GB de memória.", use="chat"),
    _item("gemma-3-1b", "Gemma 3 1B", "ggml-org/gemma-3-1b-it-GGUF",
          "gemma-3-1b-it-Q4_K_M.gguf", 806058240,
          "8ccc5cd1f1b3602548715ae25a66ed73fd5dc68a210412eea643eb20eb75a135", "Gemma",
          "O Gemma 3 pequeno do Google: conversa em vários idiomas, leve. Pede uns 1,5 GB de memória.", use="chat"),
    _item("llama-3.2-1b", "Llama 3.2 1B Instruct", "bartowski/Llama-3.2-1B-Instruct-GGUF",
          "Llama-3.2-1B-Instruct-Q4_K_M.gguf", 807694464,
          "6f85a640a97cf2bf5b8e764087b1e83da0fdb51d7c9fab7d0fece9385611df83", "Llama 3.2",
          "O Llama leve da Meta: resumos e respostas curtas, multilíngue. Pede uns 1,5 GB de memória.", use="chat"),
    _item("granite-3.3-2b", "Granite 3.3 2B Instruct", "ibm-granite/granite-3.3-2b-instruct-GGUF",
          "granite-3.3-2b-instruct-Q4_K_M.gguf", 1545303328,
          "ac71e9e32c0bea919b409c5918f69ca74339854b0319c5065e4e9fb6d95c4852", "Apache-2.0",
          "Da IBM: compacto e certinho para tarefas de escritório (resumo, extração, classificação). Pede uns 2,4 GB de memória.", use="chat"),
    _item("qwen3-1.7b", "Qwen3 1.7B", "Qwen/Qwen3-1.7B-GGUF",
          "Qwen3-1.7B-Q8_0.gguf", 1834426016,
          "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a", "Apache-2.0",
          "Pequeno e surpreendentemente esperto, com modo de pensar antes de responder. Pede uns 2,7 GB de memória.", use="chat"),
    _item("smollm3-3b", "SmolLM3 3B", "ggml-org/SmolLM3-3B-GGUF",
          "SmolLM3-Q4_K_M.gguf", 1915305312,
          "8334b850b7bd46238c16b0c550df2138f0889bf433809008cc17a8b05761863e", "Apache-2.0",
          "O melhor da sua faixa de tamanho: raciocina, conversa em 6 idiomas e lê textos longos. Pede uns 2,8 GB de memória.", use="chat"),
    _item("llama-3.2-3b", "Llama 3.2 3B Instruct", "bartowski/Llama-3.2-3B-Instruct-GGUF",
          "Llama-3.2-3B-Instruct-Q4_K_M.gguf", 2019377696,
          "6c1a2b41161032677be168d354123594c0e6e67d2b9227c84f296ad037c728ff", "Llama 3.2",
          "Llama compacto e equilibrado para conversa do dia a dia. Pede uns 2,9 GB de memória.", use="chat"),
    _item("granite-3.1-3b-a800m", "Granite 3.1 3B-A800M", "bartowski/granite-3.1-3b-a800m-instruct-GGUF",
          "granite-3.1-3b-a800m-instruct-Q4_K_M.gguf", 2016888384,
          "48e0edcd578fd4462f26127f04c651d0e650741110185297741089aea01a82b3", "Apache-2.0",
          "Mistura de especialistas da IBM: só 800M ativos por palavra, então responde rápido. Pede uns 2,9 GB de memória.", use="chat"),
    _item("gemma-3-4b", "Gemma 3 4B", "ggml-org/gemma-3-4b-it-GGUF",
          "gemma-3-4b-it-Q4_K_M.gguf", 2489757856,
          "882e8d2db44dc554fb0ea5077cb7e4bc49e7342a1f0da57901c0802ea21a0863", "Gemma",
          "Gemma 3 médio-pequeno: ótimo português para o tamanho. Pede uns 3,5 GB de memória.", use="chat"),
    _item("gemma-3n-e4b", "Gemma 3n E4B", "unsloth/gemma-3n-E4B-it-GGUF",
          "gemma-3n-E4B-it-Q4_K_M.gguf", 4539054208,
          "43b489bb77a81bda85180e7c490d40ad7f1d5c2ce654c9b05e15e104bd3c777e", "Gemma",
          "Feito para celulares e notebooks: rende como um modelo maior gastando pouca memória. Pede uns 6,0 GB de memória.", use="chat"),
    _item("qwen3-4b", "Qwen3 4B", "Qwen/Qwen3-4B-GGUF",
          "Qwen3-4B-Q4_K_M.gguf", 2497280256,
          "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5", "Apache-2.0",
          "Qwen3 de 4B: conversa, escreve e raciocina muito bem para o tamanho. Pede uns 3,5 GB de memória.", use="chat"),
    _item("qwen3-4b-instruct-2507", "Qwen3 4B Instruct 2507", "unsloth/Qwen3-4B-Instruct-2507-GGUF",
          "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2497281120,
          "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597", "Apache-2.0",
          "A versão de julho de 2025 do Qwen3 4B: respostas diretas, sem o modo de pensar. Pede uns 3,5 GB de memória.", use="chat"),
    _item("phi-4-mini", "Phi-4 mini Instruct", "unsloth/Phi-4-mini-instruct-GGUF",
          "Phi-4-mini-instruct-Q4_K_M.gguf", 2491874272,
          "88c00229914083cd112853aab84ed51b87bdf6b9ce42f532d8c85c7c63b1730a", "MIT",
          "O Phi pequeno da Microsoft: forte em lógica e matemática para 3,8B. Pede uns 3,5 GB de memória.", use="chat"),
    _item("qwen2.5-7b", "Qwen2.5 7B Instruct", "bartowski/Qwen2.5-7B-Instruct-GGUF",
          "Qwen2.5-7B-Instruct-Q4_K_M.gguf", 4683074240,
          "65b8fcd92af6b4fefa935c625d1ac27ea29dcb6ee14589c55a8f115ceaaa1423", "Apache-2.0",
          "Generalista confiável de 7B: escreve, resume e traduz. Pede uns 6,1 GB de memória.", use="chat"),
    _item("zephyr-7b-beta", "Zephyr 7B beta", "TheBloke/zephyr-7B-beta-GGUF",
          "zephyr-7b-beta.Q4_K_M.gguf", 4368438976,
          "503580dce392c6e64669ad21a77023ba2a17baa0c381250fb67c11ba6406a85e", "MIT",
          "Mistral ajustado para conversar de forma prestativa. Pede uns 5,8 GB de memória.", use="chat"),
    _item("openhermes-2.5-mistral-7b", "OpenHermes 2.5 Mistral 7B", "TheBloke/OpenHermes-2.5-Mistral-7B-GGUF",
          "openhermes-2.5-mistral-7b.Q4_K_M.gguf", 4368450304,
          "cd58120326971c71c0590f6b7084a0744e287ce9c67275d8b4bf34a5947d950b", "Apache-2.0",
          "Conversa e segue instruções bem; um dos ajustes mais usados do Mistral. Pede uns 5,8 GB de memória.", use="chat"),
    _item("openchat-3.5", "OpenChat 3.5 (0106)", "TheBloke/openchat-3.5-0106-GGUF",
          "openchat-3.5-0106.Q4_K_M.gguf", 4368450656,
          "49190d4d039e6dea463e567ebce707eb001648f4ba01e43eb7fa88d9975fc0ce", "Apache-2.0",
          "Conversa geral de 7B, muito bem avaliado na época em que saiu. Pede uns 5,8 GB de memória.", use="chat"),
    _item("starling-lm-7b", "Starling LM 7B beta", "bartowski/Starling-LM-7B-beta-GGUF",
          "Starling-LM-7B-beta-Q4_K_M.gguf", 4368450720,
          "85ad53b9cab2e73d929425f19db65f2da5817745026ed0fc696e5ce5f8ed0303", "Apache-2.0",
          "Treinado com preferências humanas: respostas úteis e bem educadas. Pede uns 5,8 GB de memória.", use="chat"),
    _item("llama-3.1-8b", "Llama 3.1 8B Instruct", "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
          "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", 4920739232,
          "7b064f5842bf9532c91456deda288a1b672397a54fa729aa665952863033557c", "Llama 3.1",
          "O Llama de 8B: generalista, multilíngue, lê textos longos. Pede uns 6,4 GB de memória.", use="chat"),
    _item("deephermes-3-8b", "DeepHermes 3 8B Preview", "bartowski/NousResearch_DeepHermes-3-Llama-3-8B-Preview-GGUF",
          "NousResearch_DeepHermes-3-Llama-3-8B-Preview-Q4_K_M.gguf", 4920740800,
          "de36671bcfc78636dc3c1be4b702198c9d9e0b8abe22dc644e4da332b31b325f", "Llama 3",
          "Llama da Nous Research que alterna entre resposta rápida e raciocínio longo. Pede uns 6,4 GB de memória.", use="chat"),
    _item("granite-3.3-8b", "Granite 3.3 8B Instruct", "ibm-granite/granite-3.3-8b-instruct-GGUF",
          "granite-3.3-8b-instruct-Q4_K_M.gguf", 4942873344,
          "77bcee066a76dcdd10d0d123c87e32c8ec2c74e31b6ffd87ebee49c9ac215dca", "Apache-2.0",
          "IBM de 8B: bom em instruções, resumos e documentos longos. Pede uns 6,4 GB de memória.", use="chat"),
    _item("yi-1.5-9b", "Yi 1.5 9B Chat", "bartowski/Yi-1.5-9B-Chat-GGUF",
          "Yi-1.5-9B-Chat-Q4_K_M.gguf", 5328957952,
          "9bfda34f9343785f9cfb040dd63a215d0ca8781cb38d5b8825c12e6e1d8230f9", "Apache-2.0",
          "Da 01.AI: conversa e raciocínio bons, forte em chinês e inglês. Pede uns 6,9 GB de memória.", use="chat"),
    _item("glm-4-9b", "GLM-4 9B (0414)", "unsloth/GLM-4-9B-0414-GGUF",
          "GLM-4-9B-0414-Q4_K_M.gguf", 6166574944,
          "8027e1089273e8817b2df0d91c9aa17c5ea467246dcdacac34989f8919fe6540", "MIT",
          "Da Zhipu: generalista de 9B, bom em escrita e em seguir instruções. Pede uns 7,9 GB de memória.", use="chat"),
    _item("mistral-nemo-12b", "Mistral Nemo 12B Instruct", "bartowski/Mistral-Nemo-Instruct-2407-GGUF",
          "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf", 7477208192,
          "7c1a10d202d8788dbe5628dc962254d10654c853cae6aaeca0618f05490d4a46", "Apache-2.0",
          "Mistral com NVIDIA: 12B, multilíngue, lê textos bem longos. Pede uns 9,5 GB de memória.", use="chat"),
    _item("gemma-3-12b", "Gemma 3 12B", "ggml-org/gemma-3-12b-it-GGUF",
          "gemma-3-12b-it-Q4_K_M.gguf", 7300574976,
          "7bb69bff3f48a7b642355d64a90e481182a7794707b3133890646b1efa778ff5", "Gemma",
          "Gemma 3 grande: conversa de alto nível, ótimo em português. Pede uns 9,3 GB de memória.", use="chat"),
    _item("qwen2.5-14b", "Qwen2.5 14B Instruct", "bartowski/Qwen2.5-14B-Instruct-GGUF",
          "Qwen2.5-14B-Instruct-Q4_K_M.gguf", 8988110976,
          "e47ad95dad6ff848b431053b375adb5d39321290ea2c638682577dafca87c008", "Apache-2.0",
          "Qwen2.5 de 14B: mais conhecimento e respostas mais completas. Pede uns 11 GB de memória.", use="chat"),
    _item("gemma-3-27b", "Gemma 3 27B", "ggml-org/gemma-3-27b-it-GGUF",
          "gemma-3-27b-it-Q4_K_M.gguf", 16546404736,
          "edc9aff4d811a285b9157618130b08688b0768d94ee5355b02dc0cb713012e15", "Gemma",
          "O maior Gemma 3: perto dos modelos fechados em conversa e escrita. Pede uns 20 GB de memória.", use="chat"),
    _item("qwen3-30b-a3b-instruct-2507", "Qwen3 30B-A3B Instruct 2507", "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF",
          "Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf", 18556686752,
          "6c997b8af17debdfb01d890214400ccbab00db6acc0ba8da5de1cc906c4774d0", "Apache-2.0",
          "30B com só 3B ativos por palavra: qualidade de modelo grande na velocidade de um pequeno. Pede uns 23 GB de memória.", use="chat"),
    _item("qwen2.5-32b", "Qwen2.5 32B Instruct", "bartowski/Qwen2.5-32B-Instruct-GGUF",
          "Qwen2.5-32B-Instruct-Q4_K_M.gguf", 19851336576,
          "2e5f6daea180dbc59f65a40641e94d3973b5dbaa32b3c0acf54647fa874e519e", "Apache-2.0",
          "Qwen2.5 de 32B: dos melhores generalistas abertos da sua geração. Pede uns 24 GB de memória.", use="chat"),
    _item("llama-3.3-70b", "Llama 3.3 70B Instruct", "bartowski/Llama-3.3-70B-Instruct-GGUF",
          "Llama-3.3-70B-Instruct-Q4_K_M.gguf", 42520398816,
          "32df3baccb556f9840059b2528b2dee4d3d516b24afdfb9d0c56ff5f63e3a664", "Llama 3.3",
          "O Llama grande: qualidade de topo; só roda num grupo de computadores. Pede uns 52 GB de memória.", use="chat"),
    _item("qwen2.5-coder-14b", "Qwen2.5 Coder 14B Instruct", "bartowski/Qwen2.5-Coder-14B-Instruct-GGUF",
          "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf", 8988111072,
          "2946d28c9e1bb2bcae6d42e8678863a31775df6f740315c7d7e6d6b6411f5937", "Apache-2.0",
          "Programa, revisa e explica código em dezenas de linguagens. Pede uns 11 GB de memória.", use="code"),
    _item("starcoder2-15b", "StarCoder2 15B Instruct", "bartowski/starcoder2-15b-instruct-v0.1-GGUF",
          "starcoder2-15b-instruct-v0.1-Q4_K_M.gguf", 9860188864,
          "4b8601ea66f93794d70ae54329508d4a7dc6ea83165ca54f99a159fb7a755d0b", "BigCode OpenRAIL-M",
          "Da BigCode, treinado só em código aberto com licença permissiva. Pede uns 12 GB de memória.", use="code"),
    _item("devstral-small", "Devstral Small (2507)", "unsloth/Devstral-Small-2507-GGUF",
          "Devstral-Small-2507-Q4_K_M.gguf", 14333918432,
          "5578b1cd0733b496cdb2d309d9a275de2ea31681793c8f05f70f3adfa65a26c4", "Apache-2.0",
          "Mistral para engenharia de software: lê projetos inteiros e propõe mudanças. Pede uns 18 GB de memória.", use="code"),
    _item("qwen3-coder-30b-a3b", "Qwen3 Coder 30B-A3B", "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF",
          "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", 18556689568,
          "fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad", "Apache-2.0",
          "O Qwen3 de programação: rápido (3B ativos) e muito bom com ferramentas e projetos. Pede uns 23 GB de memória.", use="code"),
    _item("qwen2.5-coder-32b", "Qwen2.5 Coder 32B Instruct", "bartowski/Qwen2.5-Coder-32B-Instruct-GGUF",
          "Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf", 19851336672,
          "8e2fd78ff55e7cdf577fda257bac2776feb7d73d922613caf35468073807e815", "Apache-2.0",
          "Um dos melhores modelos abertos de código, perto dos fechados. Pede uns 24 GB de memória.", use="code"),
    _item("qwen3-4b-thinking-2507", "Qwen3 4B Thinking 2507", "unsloth/Qwen3-4B-Thinking-2507-GGUF",
          "Qwen3-4B-Thinking-2507-Q4_K_M.gguf", 2497281152,
          "ddd52e18200baab281c5c46f70d544ce4d4fe4846eab1608f2fff48a64554212", "Apache-2.0",
          "Pensa passo a passo antes de responder: lógica e matemática acima do tamanho. Pede uns 3,5 GB de memória.", use="reason"),
    _item("qwen2.5-math-7b", "Qwen2.5 Math 7B Instruct", "bartowski/Qwen2.5-Math-7B-Instruct-GGUF",
          "Qwen2.5-Math-7B-Instruct-Q4_K_M.gguf", 4683074272,
          "7e03cee8c65b9ebf9ca14ddb010aca27b6b18e6c70f2779e94e7451d9529c091", "Apache-2.0",
          "Especialista em matemática: resolve e explica problemas passo a passo. Pede uns 6,1 GB de memória.", use="reason"),
    _item("deepseek-r1-qwen-7b", "DeepSeek R1 Distill Qwen 7B", "bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
          "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf", 4683073504,
          "731ece8d06dc7eda6f6572997feb9ee1258db0784827e642909d9b565641937b", "MIT",
          "Raciocínio do DeepSeek R1 num modelo de 7B. Pede uns 6,1 GB de memória.", use="reason"),
    _item("deepseek-r1-0528-qwen3-8b", "DeepSeek R1 0528 Qwen3 8B", "unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF",
          "DeepSeek-R1-0528-Qwen3-8B-Q4_K_M.gguf", 5027785216,
          "a86349a4180c4e6bb43f874c29c404fa2be3f90b15509bd6d86f697dba724ec1", "MIT",
          "O R1 de maio de 2025 destilado no Qwen3 8B: raciocínio forte em pouco espaço. Pede uns 6,5 GB de memória.", use="reason"),
    _item("glm-z1-9b", "GLM-Z1 9B (0414)", "unsloth/GLM-Z1-9B-0414-GGUF",
          "GLM-Z1-9B-0414-Q4_K_M.gguf", 6166575232,
          "1e8f66084c215b89738c87a7c53f9a9e116708ab9b02cf1c2e4225d767466b68", "MIT",
          "GLM que pensa antes de responder: matemática, lógica e código. Pede uns 7,9 GB de memória.", use="reason"),
    _item("deepseek-r1-qwen-14b", "DeepSeek R1 Distill Qwen 14B", "bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF",
          "DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf", 8988110240,
          "0b319bd0572f2730bfe11cc751defe82045fad5085b4e60591ac2cd2d9633181", "MIT",
          "Raciocínio do R1 em 14B: bom equilíbrio entre qualidade e memória. Pede uns 11 GB de memória.", use="reason"),
    _item("phi-4-reasoning-plus", "Phi-4 Reasoning Plus", "unsloth/Phi-4-reasoning-plus-GGUF",
          "Phi-4-reasoning-plus-Q4_K_M.gguf", 9053117120,
          "faf720745e20df40f52ee218be14c72b33070f7aacc508b3fbc61d47f32b4ffe", "MIT",
          "Phi-4 treinado para raciocinar longamente: forte em matemática e ciência. Pede uns 11 GB de memória.", use="reason"),
    _item("magistral-small", "Magistral Small (2507)", "unsloth/Magistral-Small-2507-GGUF",
          "Magistral-Small-2507-Q4_K_M.gguf", 14333913248,
          "bffb7d31073fbd9f8c00f210c8d3930a0464b1d5d562dcffebc76b6efecc8d32", "Apache-2.0",
          "O raciocinador da Mistral: pensa em vários idiomas, inclusive português. Pede uns 18 GB de memória.", use="reason"),
    _item("deepseek-r1-qwen-32b", "DeepSeek R1 Distill Qwen 32B", "bartowski/DeepSeek-R1-Distill-Qwen-32B-GGUF",
          "DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf", 19851335840,
          "bed9b0f551f5b95bf9da5888a48f0f87c37ad6b72519c4cbd775f54ac0b9fc62", "MIT",
          "O maior destilado do R1: raciocínio de alto nível. Pede uns 24 GB de memória.", use="reason"),

    # ---- imagem e vídeo (regras v2, stable-diffusion.cpp)
    _item("z-image-turbo", "Z-Image Turbo", "leejet/Z-Image-Turbo-GGUF", "z_image_turbo-Q4_K.gguf", 3864250304,
          "14b375ab4f226bc5378f68f37e899ef3c2242b8541e61e2bc1aff40976086fbd", "Apache-2.0",
          "Imagens bonitas em 8 passos, inclusive com texto escrito nelas; pede uns 10 GB de memória.",
          task="image",
          files=(("llm", "unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2497281120,
                  "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597"),
                 ("vae", "Comfy-Org/Z-Image-Turbo", "split_files/vae/ae.safetensors", 335304388,
                  "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38")),
          recipe={"steps": 8, "cfg": 1.0, "sampler": "euler", "size": "1024x1024"}),
    _item("stable-diffusion-1.5", "Stable Diffusion 1.5", "stable-diffusion-v1-5/stable-diffusion-v1-5",
          "v1-5-pruned-emaonly.safetensors", 4265146304,
          "6ce0161689b3853acaa03779ec93eafe75a02f4ced659bee03f50797806fa2fa", "OpenRAIL",
          "O clássico das imagens abertas: leve, roda em PCs modestos (512x512); pede uns 7 GB de memória.",
          task="image", recipe={"steps": 20, "cfg": 7.0, "sampler": "euler_a", "size": "512x512"}),
    _item("wan2.1-t2v-1.3b", "Wan 2.1 Texto para Vídeo 1.3B", "Comfy-Org/Wan-2.1",
          "split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors", 2838303560,
          "be531024cd9018cb5b48c40cfbb6a6191645b1c792eb8bf4f8c1c6e10f924dc5", "Apache-2.0",
          "Vídeos curtos (2 a 5 s) a partir de uma frase. Pesado: no processador leva vários minutos; pede uns "
          "12 GB de memória.",
          task="video",
          files=(("vae", "Comfy-Org/Wan-2.1", "split_files/vae/wan_2.1_vae.safetensors", 253815318,
                  "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b"),
                 ("t5xxl", "city96/umt5-xxl-encoder-gguf", "umt5-xxl-encoder-Q4_K_M.gguf", 3655145312,
                  "17cf97a5bbbc60a646d6105b832b6f657ce904a8a1ad970e4b59df0c67584a40")),
          recipe={"steps": 12, "cfg": 6.0, "sampler": "euler", "size": "480x272", "flow_shift": 3.0}),
    _item("flux.1-schnell", "FLUX.1 schnell", "city96/FLUX.1-schnell-gguf", "flux1-schnell-Q4_K_S.gguf", 6783943712,
          "4fd16477b3a5296d0cf722c4b92a9fd7f30d09ac7495826e4465d8de9c9fd973", "Apache-2.0",
          "Um dos melhores geradores de imagem abertos, em só 4 passos; pede uns 14 GB de memória.",
          task="image",
          files=(("vae", "Comfy-Org/Z-Image-Turbo", "split_files/vae/ae.safetensors", 335304388,
                  "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38"),
                 ("clip_l", "comfyanonymous/flux_text_encoders", "clip_l.safetensors", 246144152,
                  "660c6f5b1abae9dc498ac2d21e1347d2abdb0cf6c0c0c8576cd796491d9a6cdd"),
                 ("t5xxl", "city96/t5-v1_1-xxl-encoder-gguf", "t5-v1_1-xxl-encoder-Q4_K_M.gguf", 2896123072,
                  "6be2b0b7e2de7cf2919340c88cb802a103a997ce46c53131cec91958c1db1af4")),
          recipe={"steps": 4, "cfg": 1.0, "sampler": "euler", "size": "1024x1024"}),
    _item("sdxl-1.0", "Stable Diffusion XL 1.0", "stabilityai/stable-diffusion-xl-base-1.0",
          "sd_xl_base_1.0.safetensors", 6938078334,
          "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b", "OpenRAIL++",
          "Imagens em 1024x1024 com muitos estilos (foto, ilustração, pintura); pede uns 10 GB de memória.",
          task="image", recipe={"steps": 25, "cfg": 7.0, "sampler": "euler_a", "size": "1024x1024"}),
    _item("wan2.1-t2v-14b", "Wan 2.1 Texto para Vídeo 14B", "city96/Wan2.1-T2V-14B-gguf",
          "wan2.1-t2v-14b-Q4_K_M.gguf", 10124581504,
          "c9a64d612831debbac77af93744d4b5196b840bf65e9f9f5b958d03fc3baddf2", "Apache-2.0",
          "A versão grande do Wan: vídeos bem melhores, mas lenta sem placa de vídeo; pede uns 21 GB de memória.",
          task="video",
          files=(("vae", "Comfy-Org/Wan-2.1", "split_files/vae/wan_2.1_vae.safetensors", 253815318,
                  "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b"),
                 ("t5xxl", "city96/umt5-xxl-encoder-gguf", "umt5-xxl-encoder-Q4_K_M.gguf", 3655145312,
                  "17cf97a5bbbc60a646d6105b832b6f657ce904a8a1ad970e4b59df0c67584a40")),
          recipe={"steps": 12, "cfg": 6.0, "sampler": "euler", "size": "480x272", "flow_shift": 3.0}),
    _item("qwen-image", "Qwen-Image", "city96/Qwen-Image-gguf", "qwen-image-Q4_K_M.gguf", 13065746976,
          "c6f49103603b9a4927502549fb615c910386cfc675b1466325930ba10d2a11f4", "Apache-2.0",
          "O gerador de imagem da Qwen: o melhor aberto para escrever texto dentro da imagem (placas, cartazes, "
          "logos), inclusive em português; pede uns 25 GB de memória.",
          task="image",
          files=(("llm", "unsloth/Qwen2.5-VL-7B-Instruct-GGUF", "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf", 4683072384,
                  "d16776dcd9a28d42758c2958ed3a752aabf20a305252cd64ff2be72b4a78c503"),
                 ("vae", "Comfy-Org/Qwen-Image_ComfyUI", "split_files/vae/qwen_image_vae.safetensors", 253806246,
                  "a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f")),
          recipe={"steps": 20, "cfg": 2.5, "sampler": "euler", "size": "1024x1024"}),
    _item("ssd-1b", "Segmind SSD-1B", "segmind/SSD-1B", "SSD-1B.safetensors", 4465671506,
          "0bf1ce6b065a6b969ab02dc8e8fa21eb20ee189b10935c49ce68c77a7e432c1c", "Apache-2.0",
          "Um SDXL enxugado: imagens em 1024x1024 quase iguais, 60% mais rápido; pede uns 7 GB de memória.",
          task="image", recipe={"steps": 25, "cfg": 7.0, "sampler": "euler_a", "size": "1024x1024"}),
    _item("wan2.2-ti2v-5b", "Wan 2.2 Texto para Vídeo 5B", "QuantStack/Wan2.2-TI2V-5B-GGUF", "Wan2.2-TI2V-5B-Q4_K_M.gguf", 3433116000,
          "95b19697b7f98e65b0a543640e9ca7b4dfec32e2a6e3731e8e10708be52655e2", "Apache-2.0",
          "O Wan 2.2 de 5B: vídeos melhores que o 1.3B e bem mais leve que o 14B; pede uns 15 GB de memória.",
          task="video",
          files=(("vae", "Comfy-Org/Wan_2.2_ComfyUI_Repackaged", "split_files/vae/wan2.2_vae.safetensors", 1409400960,
                  "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156"),
                 ("t5xxl", "city96/umt5-xxl-encoder-gguf", "umt5-xxl-encoder-Q4_K_M.gguf", 3655145312,
                  "17cf97a5bbbc60a646d6105b832b6f657ce904a8a1ad970e4b59df0c67584a40")),
          recipe={"steps": 12, "cfg": 5.0, "sampler": "euler", "size": "480x272", "flow_shift": 5.0}),
    # ---- gigantes de um arquivo só (2.13): não cabem num PC de 16 GB; rodam sozinhos em PCs grandes ou num grupo
    _item("mixtral-8x7b", "Mixtral 8x7B Instruct (Q3)", "TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF",
          "mixtral-8x7b-instruct-v0.1.Q3_K_M.gguf", 20363356096,
          "bd2e1499e68195f1a6ff151e6fa5c6632acc150b80cca4a3772cbb7ca59d44cd", "Apache-2.0",
          "O clássico mistura de especialistas da Mistral, compactado: o gigante mais fácil de montar. Pede uns 25 GB de memória somada: uns 3 computadores de 16 GB."),
    _item("llama-3.3-70b-iq2", "Llama 3.3 70B Instruct (IQ2)", "bartowski/Llama-3.3-70B-Instruct-GGUF",
          "Llama-3.3-70B-Instruct-IQ2_XS.gguf", 21142113248,
          "0b4527182ff9b6bc84b7dd754c6161bdf2d9fec93489a482f10942d40c7bdf68", "Llama 3.3",
          "O Llama de 70B espremido ao máximo: perde um pouco de qualidade, mas cabe em poucos computadores. Pede uns 26 GB de memória somada: uns 3 computadores de 16 GB."),
    _item("gemma-3-27b-q8", "Gemma 3 27B (Q8)", "ggml-org/gemma-3-27b-it-GGUF",
          "gemma-3-27b-it-Q8_0.gguf", 28707604864,
          "794d14393b9a15de7f08da16dc35c503edb3eb48dd58fa5beb37b7cf2a188379", "Gemma",
          "O maior Gemma 3 em qualidade quase cheia: respostas mais precisas que a versão compacta. Pede uns 35 GB de memória somada: uns 4 computadores de 16 GB."),
    _item("mixtral-8x7b-q5", "Mixtral 8x7B Instruct (Q5)", "TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF",
          "mixtral-8x7b-instruct-v0.1.Q5_K_M.gguf", 32229279680,
          "af12961e014037ee8c5c9f3bf7cf9fd99cadc9dabd50f528a4248c4a8ee8fe77", "Apache-2.0",
          "Mixtral com mais qualidade: multilíngue e bom em código. Pede uns 40 GB de memória somada: uns 5 computadores de 16 GB."),
    _item("qwen3-30b-a3b-q8", "Qwen3 30B-A3B Instruct 2507 (Q8)", "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF",
          "Qwen3-30B-A3B-Instruct-2507-Q8_0.gguf", 32483932576,
          "47514861adaa7ae7f32818fe54595c7cbed2753417ba78df53054fcf7b9dcd88", "Apache-2.0",
          "O Qwen3 30B-A3B em qualidade quase cheia, e rápido (3B ativos). Pede uns 40 GB de memória somada: uns 5 computadores de 16 GB."),
    _item("llama-3.3-70b-q3", "Llama 3.3 70B Instruct (Q3)", "bartowski/Llama-3.3-70B-Instruct-GGUF",
          "Llama-3.3-70B-Instruct-Q3_K_M.gguf", 34267499488,
          "50221eecf9fd26481a55804e74581123f3a175cc589d044bee3306bc53f137c1", "Llama 3.3",
          "O Llama de 70B num meio-termo entre qualidade e memória. Pede uns 42 GB de memória somada: uns 5 computadores de 16 GB."),
    _item("deepseek-r1-llama-70b-q3", "DeepSeek R1 Distill Llama 70B (Q3)", "bartowski/DeepSeek-R1-Distill-Llama-70B-GGUF",
          "DeepSeek-R1-Distill-Llama-70B-Q3_K_M.gguf", 34267496608,
          "3778a253baf5188c056218430eaab3d8ad913ca72a33494c432c536f67349c95", "Llama 3.3",
          "O raciocínio do DeepSeek R1 no Llama de 70B, compactado. Pede uns 42 GB de memória somada: uns 5 computadores de 16 GB."),
    _item("deepseek-r1-llama-70b", "DeepSeek R1 Distill Llama 70B", "bartowski/DeepSeek-R1-Distill-Llama-70B-GGUF",
          "DeepSeek-R1-Distill-Llama-70B-Q4_K_M.gguf", 42520395936,
          "181a82a1d6d2fa24fe4db83a68eee030384986bdbdd4773ba76424e3a6eb9fd8", "Llama 3.3",
          "O maior destilado do R1: raciocínio de primeira em matemática e lógica. Pede uns 52 GB de memória somada: uns 6 computadores de 16 GB."),
    _item("mixtral-8x7b-q8", "Mixtral 8x7B Instruct (Q8)", "TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF",
          "mixtral-8x7b-instruct-v0.1.Q8_0.gguf", 49624262592,
          "cdca4a8c09dfd722702f781d479695cda0d45e1bd1cd602ba1b6085ad921fc5f", "Apache-2.0",
          "Mixtral em qualidade quase cheia. Pede uns 61 GB de memória somada: uns 7 computadores de 16 GB."),
]

# Gigantes (regras v4): não cabem em nenhum computador comum e rodam divididos num grupo (sigma/grupo.py). O
# coordenador baixa todas as partes do GGUF (precisa de disco para o modelo inteiro) e espalha as camadas pela memória
# dos trabalhadores; a rede só coloca o modelo num coordenador cuja memória somada (a dele + a emprestada) cabe.
def _giant(id_, name, repo, license, description, parts):
    (f0, s0, h0), rest = parts[0], parts[1:]
    item = _item(id_, name, repo, f0, s0, h0, license, description)
    extra = [{"role": "part", "url": f"{HF}/{repo}/resolve/main/{f}", "sha256": h, "size": n} for f, n, h in rest]
    total = s0 + sum(x["size"] for x in extra)
    item.update(files=extra, total_size=total, ram_mb=ram_mb(total), split=True, parts=1 + len(extra))
    return item


SPLIT_CATALOG = [
    _giant("gpt-oss-120b", "gpt-oss 120B", "ggml-org/gpt-oss-120b-GGUF", "Apache-2.0",
           "O maior modelo aberto da OpenAI, perto dos modelos fechados em raciocínio.",
           [
            ("gpt-oss-120b-MXFP4.gguf", 63387346208,
             "582bd40f6886200101f4c4ed9f25f3fe80cc14c86e9e2b37746cd8904a0c622d")]),
    _giant("qwen3-235b-a22b", "Qwen3 235B-A22B", "Qwen/Qwen3-235B-A22B-GGUF", "Apache-2.0",
           "O topo da família Qwen3: conversa, raciocina e programa no nível dos melhores.",
           [
            ("Q4_K_M/Qwen3-235B-A22B-Q4_K_M-00001-of-00005.gguf", 29742283872,
             "ce666f562eb8eefda48eec0aa93680ba40ca5727d0ba9918f3fb1771141375e6"),
            ("Q4_K_M/Qwen3-235B-A22B-Q4_K_M-00002-of-00005.gguf", 29974106496,
             "ac73c457aac5993d02444063c976062c70be609c03ca6584d0c3be0632e79ce6"),
            ("Q4_K_M/Qwen3-235B-A22B-Q4_K_M-00003-of-00005.gguf", 29933980640,
             "6357074e14a748446c2b0dcc64a1a7c64b1c124fbfb9cb48114147645d3310fe"),
            ("Q4_K_M/Qwen3-235B-A22B-Q4_K_M-00004-of-00005.gguf", 29936094304,
             "2eb47718eaeeebee3726cd2ca4ac11b5768faded5d7adddec5b754aeb7c387dd"),
            ("Q4_K_M/Qwen3-235B-A22B-Q4_K_M-00005-of-00005.gguf", 22567608864,
             "dbe2c7547c0d397d2ef16e08218a586383771dc12e9678cfd6fa8cc23e94e8f8")]),
    _giant("glm-4.5", "GLM-4.5 355B", "unsloth/GLM-4.5-GGUF", "MIT",
           "Da Zhipu: forte em agentes, ferramentas e programação.",
           [
            ("Q4_K_M/GLM-4.5-Q4_K_M-00001-of-00005.gguf", 50006655328,
             "af56d111e3e639af15e1e0740f994cf1179c8836d5b840da8d935206cfa33b1f"),
            ("Q4_K_M/GLM-4.5-Q4_K_M-00002-of-00005.gguf", 49705685664,
             "5a9ad08799ef59ef1e3e32857299f39f22b90c56c489462353e842915c872146"),
            ("Q4_K_M/GLM-4.5-Q4_K_M-00003-of-00005.gguf", 49631164960,
             "8892999a5fd118084e5353ea9889506c4f5da35ee05524d8dd576957726d9272"),
            ("Q4_K_M/GLM-4.5-Q4_K_M-00004-of-00005.gguf", 49501239424,
             "aee51d4a55f8edfb4bfe0b8316500b92c49a4c2d678930fa4ee70b98ac3b9aaa"),
            ("Q4_K_M/GLM-4.5-Q4_K_M-00005-of-00005.gguf", 17610827296,
             "94bc61f761c84e681acf11c431abd0cf09fa43bc86ffb8a0fa063d8630b4815a")]),
    _giant("qwen3-coder-480b", "Qwen3 Coder 480B-A35B", "unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF", "Apache-2.0",
           "O maior modelo aberto de programação: projetos inteiros, não só trechos.",
           [
            ("Q4_K_M/Qwen3-Coder-480B-A35B-Instruct-Q4_K_M-00001-of-00006.gguf", 49973553312,
             "47c7a389014e2b355d8a7d22cb9a1e7a3e8d320d2c41478f9f22b57e385c24c2"),
            ("Q4_K_M/Qwen3-Coder-480B-A35B-Instruct-Q4_K_M-00002-of-00006.gguf", 49768927168,
             "2ed1501a15c36be304e7599f9e02e6bd16ecd67a1e59c35144a63d5f77cf924a"),
            ("Q4_K_M/Qwen3-Coder-480B-A35B-Instruct-Q4_K_M-00003-of-00006.gguf", 48862488352,
             "3cae399c40bdc4873e326a0c4f2e6d48f6fccb11eedddaf894436526984cebfe"),
            ("Q4_K_M/Qwen3-Coder-480B-A35B-Instruct-Q4_K_M-00004-of-00006.gguf", 48952192064,
             "966ff0058ea3f5e849cb2161b5ce90d9cbee478ccb8f142ac54f14c1fd58bb12"),
            ("Q4_K_M/Qwen3-Coder-480B-A35B-Instruct-Q4_K_M-00005-of-00006.gguf", 49721298176,
             "e896375ba530a21fd526ad0b8ede091501ea3361cd02d134aea32428c75614bf"),
            ("Q4_K_M/Qwen3-Coder-480B-A35B-Instruct-Q4_K_M-00006-of-00006.gguf", 42780367136,
             "6722d06717918499719e125206b3b5d4ff886dd532acf5a1a0749d59803d3ddb")]),
    _giant("deepseek-r1-0528", "DeepSeek R1 671B", "unsloth/DeepSeek-R1-0528-GGUF", "MIT",
           "O DeepSeek R1 completo, 671 bilhões de parâmetros: pensa longamente antes de responder.",
           [
            ("Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00001-of-00009.gguf", 48423670176,
             "81034e575733f3be6051805afa475fafa36074ea9c52030232c5d70d2fd7b1d0"),
            ("Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00002-of-00009.gguf", 49488117120,
             "9900cbe6e956fe83ca4e47b27493d3ee434e536d4287ff7122808ccb6bdcc790"),
            ("Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00003-of-00009.gguf", 49594422144,
             "46462bef75ad3c7326b74dfbd22328575b28a964af193f1ebd53a6b7d7d9b703"),
            ("Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00004-of-00009.gguf", 48331230336,
             "e8a6b4964375c39bfb54f5f29d106497013f00f0bddf49925e6091277b6f2937"),
            ("Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00005-of-00009.gguf", 49480748288,
             "708d5ac916934f6f38aa53eb0d60e02716641f724987b83659546fa54f0c2e96"),
            ("Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00006-of-00009.gguf", 48331230336,
             "8d7b85f938349dc6108c868ec5616fd60fc2be23916157390cfa44fe376cb4c7"),
            ("Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00007-of-00009.gguf", 49488117120,
             "368ee7cea044c3e7a771a7e406d032fd69757a44b1bb2a3f8001f0f4e0b2e5bc"),
            ("Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00008-of-00009.gguf", 46997494496,
             "d32fc34c50e06fadb545c4168c7c8b6ccac28f4b75a768b35734a09a1bca1841"),
            ("Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00009-of-00009.gguf", 14806870816,
             "dc6efd592eb003895582c9ed7e197d49af511d6f11f7ee11320eae3785f22f84")]),
    # ---- mais gigantes (2.13): de 3 a 55 computadores de 16 GB (pesquisa/hf_gigantes.py)
    _giant("llama-3.3-70b-q5", "Llama 3.3 70B Instruct (Q5)", "bartowski/Llama-3.3-70B-Instruct-GGUF", "Llama 3.3",
           "O Llama de 70B com mais qualidade que o Q4. Pede uns 61 GB de memória somada: uns 7 computadores de 16 GB.",
           [
            ("Llama-3.3-70B-Instruct-Q5_K_M/Llama-3.3-70B-Instruct-Q5_K_M-00001-of-00002.gguf", 39987554880,
             "1e0a25b0c10a7590947fce663985c0d5e7401c41ad4cf994dd36d9337c532d3d"),
            ("Llama-3.3-70B-Instruct-Q5_K_M/Llama-3.3-70B-Instruct-Q5_K_M-00002-of-00002.gguf", 9962267232,
             "7d1098ce9c1cea0cee718467d0914ad1cdcef501651b0917300284181838cd39")]),
    _giant("llama-4-scout", "Llama 4 Scout 17B-16E", "unsloth/Llama-4-Scout-17B-16E-Instruct-GGUF", "Llama 4",
           "O Llama 4 com 16 especialistas: só 17B ativos por palavra e contexto gigantesco. Pede uns 79 GB de memória somada: uns 9 computadores de 16 GB.",
           [
            ("Q4_K_M/Llama-4-Scout-17B-16E-Instruct-Q4_K_M-00001-of-00002.gguf", 49848379744,
             "fbe956902467171ed7c0c326e5d868771a84d46468d407abecd0f289297313f9"),
            ("Q4_K_M/Llama-4-Scout-17B-16E-Instruct-Q4_K_M-00002-of-00002.gguf", 15511520608,
             "e7330ae14527f08e8a44a6a573b45084052b8822c4b8a5179c5cad9cd6f6f795")]),
    _giant("glm-4.5-air", "GLM-4.5 Air", "unsloth/GLM-4.5-Air-GGUF", "MIT",
           "O GLM-4.5 enxuto (106B, 12B ativos): excelente em código, ferramentas e raciocínio. Pede uns 89 GB de memória somada: uns 10 computadores de 16 GB.",
           [
            ("Q4_K_M/GLM-4.5-Air-Q4_K_M-00001-of-00002.gguf", 50000746752,
             "3e7e5d25a6db33b7c90f8c5203d6f490d6c0a4f04a46f228af0e33727db5df5e"),
            ("Q4_K_M/GLM-4.5-Air-Q4_K_M-00002-of-00002.gguf", 22975001632,
             "ac6e50523d45c53faf3bf72d8fe109f3dd54bc6abe98a7773a3636112ead82a6")]),
    _giant("llama-3.3-70b-q8", "Llama 3.3 70B Instruct (Q8)", "bartowski/Llama-3.3-70B-Instruct-GGUF", "Llama 3.3",
           "O Llama de 70B em qualidade quase cheia. Pede uns 91 GB de memória somada: uns 11 computadores de 16 GB.",
           [
            ("Llama-3.3-70B-Instruct-Q8_0/Llama-3.3-70B-Instruct-Q8_0-00001-of-00002.gguf", 39927004064,
             "0c4ec5083aec2efa104269e8b63e2d9f3322e2fa50511184314093a7b4697878"),
            ("Llama-3.3-70B-Instruct-Q8_0/Llama-3.3-70B-Instruct-Q8_0-00002-of-00002.gguf", 35048050944,
             "e5c8858f5feafdc56200bbcbb3c4fadb07cb323c8ed13548dfb798dd1fb24255")]),
    _giant("qwen3-235b-a22b-2507-q2", "Qwen3 235B-A22B Instruct 2507 (Q2)", "unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF", "Apache-2.0",
           "O topo do Qwen3 compactado ao máximo: um dos melhores modelos abertos em bem menos computadores. Pede uns 104 GB de memória somada: uns 12 computadores de 16 GB.",
           [
            ("Q2_K/Qwen3-235B-A22B-Instruct-2507-Q2_K-00001-of-00002.gguf", 49906751904,
             "7cb5dfd9d11f6297477f359439ad906eb92d62fc96b1783871159e7204d427ff"),
            ("Q2_K/Qwen3-235B-A22B-Instruct-2507-Q2_K-00002-of-00002.gguf", 35784249568,
             "73801de2867057497a7fb6d0f0350ef7832b98f25c264f3a9909313af785672d")]),
    _giant("glm-4.5-air-q8", "GLM-4.5 Air (Q8)", "unsloth/GLM-4.5-Air-GGUF", "MIT",
           "GLM-4.5 Air em qualidade quase cheia. Pede uns 142 GB de memória somada: uns 16 computadores de 16 GB.",
           [
            ("Q8_0/GLM-4.5-Air-Q8_0-00001-of-00003.gguf", 49793116576,
             "8a58096ec777113d5ee396af4198b6b0aeb91d9d02d933f44b310c1645a4a973"),
            ("Q8_0/GLM-4.5-Air-Q8_0-00002-of-00003.gguf", 49788877664,
             "dd9da13844bd2d9148515618228e654727d7438db062163ffb55d0ac4a2ba8d8"),
            ("Q8_0/GLM-4.5-Air-Q8_0-00003-of-00003.gguf", 17874512000,
             "f9674c7541e7641f6de6c41fcc62f5bcf2e312aff1c4e6d8d2e8927a8cef77c6")]),
    _giant("minimax-m2", "MiniMax M2", "unsloth/MiniMax-M2-GGUF", "MIT",
           "Feito para agentes e programação: rápido (10B ativos) e muito bom com ferramentas. Pede uns 167 GB de memória somada: uns 19 computadores de 16 GB.",
           [
            ("Q4_K_M/MiniMax-M2-Q4_K_M-00001-of-00003.gguf", 49386415584,
             "ed85df4bed999b9c30ecaef9edc3217206b2e6b0e304e52bbf0c737e4c1fc919"),
            ("Q4_K_M/MiniMax-M2-Q4_K_M-00002-of-00003.gguf", 49712985440,
             "4a25cc6787ace8cec20f1799151bbd3b43d7e9c2bf20a3e73b91a2c8693bf355"),
            ("Q4_K_M/MiniMax-M2-Q4_K_M-00003-of-00003.gguf", 39242984096,
             "58369de8424f885ae73de33343260574623d40e21741dd27e31510e4b184867d")]),
    _giant("qwen3-235b-a22b-2507", "Qwen3 235B-A22B Instruct 2507", "unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF", "Apache-2.0",
           "A versão de julho de 2025 do maior Qwen3: respostas diretas de nível de topo. Pede uns 172 GB de memória somada: uns 20 computadores de 16 GB.",
           [
            ("Q4_K_M/Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00001-of-00003.gguf", 49944699040,
             "5dc744396d0b116215e0b97fbfb74b3e0945f24857ba976c258e4edd15eed558"),
            ("Q4_K_M/Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00002-of-00003.gguf", 49930349824,
             "a9a9213792b6f56663cab014b958909c986cafd8ba59c1fef9257858dc879683"),
            ("Q4_K_M/Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00003-of-00003.gguf", 42279026016,
             "b12aa8ceb940f0ccfd3aa00dbbe6d2faf3792185758a1cad08f8ffba2124d8bc")]),
    _giant("qwen3-235b-a22b-thinking-2507", "Qwen3 235B-A22B Thinking 2507", "unsloth/Qwen3-235B-A22B-Thinking-2507-GGUF", "Apache-2.0",
           "O maior Qwen3 que pensa longamente: matemática e lógica de ponta. Pede uns 172 GB de memória somada: uns 20 computadores de 16 GB.",
           [
            ("Q4_K_M/Qwen3-235B-A22B-Thinking-2507-Q4_K_M-00001-of-00003.gguf", 49944699040,
             "e943eaf1d0c59e220915bfad7a5236303ec051de63aa1327f7a3378b44d1bae0"),
            ("Q4_K_M/Qwen3-235B-A22B-Thinking-2507-Q4_K_M-00002-of-00003.gguf", 49930349824,
             "cb6d0629fac10258ada1a4358d48c4b5473f564d8288ce11c2df9f8b6eb69ab6"),
            ("Q4_K_M/Qwen3-235B-A22B-Thinking-2507-Q4_K_M-00003-of-00003.gguf", 42279026016,
             "049d971ef0732eb5168a6f421b63ad2b742d82511f32f39735862e60edbb5f4b")]),
    _giant("glm-4.6", "GLM-4.6", "unsloth/GLM-4.6-GGUF", "MIT",
           "O GLM mais novo: programação e agentes no nível dos melhores modelos fechados. Pede uns 260 GB de memória somada: uns 29 computadores de 16 GB.",
           [
            ("Q4_K_M/GLM-4.6-Q4_K_M-00001-of-00005.gguf", 50006655360,
             "be232f022f8b6b8735dcb6bd77254b87354f87a0601a029b365a1db051f886a8"),
            ("Q4_K_M/GLM-4.6-Q4_K_M-00002-of-00005.gguf", 49705685664,
             "fa74ec39ef7a0b163a84bdf714675f0d930880564a9680f85d279796624d72e7"),
            ("Q4_K_M/GLM-4.6-Q4_K_M-00003-of-00005.gguf", 49631164960,
             "a1e4a10484209d73322c0de016081741c708014e41700a3397e4d8a48044ec8b"),
            ("Q4_K_M/GLM-4.6-Q4_K_M-00004-of-00005.gguf", 49501239424,
             "c5cb19c79c745d02fe5db22d831460548a49647d906addfa23aad6b25351507c"),
            ("Q4_K_M/GLM-4.6-Q4_K_M-00005-of-00005.gguf", 16737887648,
             "a153145b2ab8b7a94242acbff507fe025760f565d361c52ac95667bbe5da561a")]),
    _giant("llama-4-maverick", "Llama 4 Maverick 17B-128E", "unsloth/Llama-4-Maverick-17B-128E-Instruct-GGUF", "Llama 4",
           "O Llama 4 grande, com 128 especialistas: conversa e visão de mundo de alto nível. Pede uns 292 GB de memória somada: uns 33 computadores de 16 GB.",
           [
            ("Q4_K_M/Llama-4-Maverick-17B-128E-Instruct-Q4_K_M-00001-of-00005.gguf", 49408442368,
             "f2f8206b0c81e28644a8a4632e5745381b4e3009b92e2754b9b975a7d9fc0d56"),
            ("Q4_K_M/Llama-4-Maverick-17B-128E-Instruct-Q4_K_M-00002-of-00005.gguf", 49543134080,
             "1f1b167907c322954c6e971271aa7426028149a979f2d23d56da409ab21a67e2"),
            ("Q4_K_M/Llama-4-Maverick-17B-128E-Instruct-Q4_K_M-00003-of-00005.gguf", 49566112640,
             "8138c895fc95c32c04d6fdcb98f223467b68deb597f0f5c7e2a2661313d85771"),
            ("Q4_K_M/Llama-4-Maverick-17B-128E-Instruct-Q4_K_M-00004-of-00005.gguf", 48169827200,
             "ade0af564d22825d7f273a4431d7d8d51aefeb18cbff93144f5469a9d5322afa"),
            ("Q4_K_M/Llama-4-Maverick-17B-128E-Instruct-Q4_K_M-00005-of-00005.gguf", 46079637152,
             "c4e1a1f23f910ea9f704fe537b2e9764ddca663e04d191ba520104890432acfb")]),
    _giant("kimi-k2", "Kimi K2 Instruct", "unsloth/Kimi-K2-Instruct-GGUF", "MIT modificada",
           "1 trilhão de parâmetros (32B ativos), da Moonshot: excelente em agentes e código. Pede uns 459 GB de memória somada: uns 51 computadores de 16 GB.",
           [
            ("UD-Q2_K_XL/Kimi-K2-Instruct-UD-Q2_K_XL-00001-of-00008.gguf", 49981610528,
             "cee60a71507624b42ef6eff87ce2aaf187632c214ab6fcfe97c02f5a5376f883"),
            ("UD-Q2_K_XL/Kimi-K2-Instruct-UD-Q2_K_XL-00002-of-00008.gguf", 48635816800,
             "274fdfaef6ab52d3a3c55387ee038fb12d2aecff9635b0d966609c2e94a66b16"),
            ("UD-Q2_K_XL/Kimi-K2-Instruct-UD-Q2_K_XL-00003-of-00008.gguf", 49755444480,
             "14ee63a8b7ff1b21610de3a00eeea785cb1d4dd442052858f4d2a4da95e1b230"),
            ("UD-Q2_K_XL/Kimi-K2-Instruct-UD-Q2_K_XL-00004-of-00008.gguf", 49752068352,
             "3c80d279309723c95e5299b64e994df8aea53133d9dd54ff01775d978a90a050"),
            ("UD-Q2_K_XL/Kimi-K2-Instruct-UD-Q2_K_XL-00005-of-00008.gguf", 49748104448,
             "d12918cff1ad3f4e267a1ec9d124c0066f29981a983584f9823bde6219ca5d2f"),
            ("UD-Q2_K_XL/Kimi-K2-Instruct-UD-Q2_K_XL-00006-of-00008.gguf", 49750233344,
             "4b5a59f999648696e3c03f174c95038a5f910595db9c1eee6bb658cf3388f053"),
            ("UD-Q2_K_XL/Kimi-K2-Instruct-UD-Q2_K_XL-00007-of-00008.gguf", 49754018048,
             "366855f43768806ced21f83c344aab45d9c292761e4a87153a6eda118c94021c"),
            ("UD-Q2_K_XL/Kimi-K2-Instruct-UD-Q2_K_XL-00008-of-00008.gguf", 34484027872,
             "28a93946b17f0ee9fe6191e26fe805a36e0c0c329f73cc28b00d32cdfd8142eb")]),
    _giant("deepseek-v3.1", "DeepSeek V3.1", "unsloth/DeepSeek-V3.1-GGUF", "MIT",
           "O DeepSeek de 671B com modo de pensar ligável: um dos maiores modelos abertos. Pede uns 487 GB de memória somada: uns 55 computadores de 16 GB.",
           [
            ("Q4_K_M/DeepSeek-V3.1-Q4_K_M-00001-of-00009.gguf", 48473901440,
             "02c50f1b9abeb956ee130c65c765535bbfc4651ec9fd9ef266d31a66eedce287"),
            ("Q4_K_M/DeepSeek-V3.1-Q4_K_M-00002-of-00009.gguf", 49549016448,
             "977a3a05e11bf840cc39253befc8b0ad00cd0b9e49dddd86527e35c1fcd0459e"),
            ("Q4_K_M/DeepSeek-V3.1-Q4_K_M-00003-of-00009.gguf", 49657385856,
             "7017d14676e19d7fd3db9bca63fda804dcff1785ab5baf9e3726ef374d657030"),
            ("Q4_K_M/DeepSeek-V3.1-Q4_K_M-00004-of-00009.gguf", 48386509952,
             "cea9da55904e023cf7f109223902c94615722985bd349e1f4c45f4033c6fc9e2"),
            ("Q4_K_M/DeepSeek-V3.1-Q4_K_M-00005-of-00009.gguf", 49543597312,
             "655a9431679749c3712cfcc1a774665923e0dd68c4d1c8c7cf54ec6a28734937"),
            ("Q4_K_M/DeepSeek-V3.1-Q4_K_M-00006-of-00009.gguf", 48386509952,
             "4f21a846e26b1e0bb18352012184a0be6e09130276299cfde74eea01983897fd"),
            ("Q4_K_M/DeepSeek-V3.1-Q4_K_M-00007-of-00009.gguf", 49549016448,
             "0a4e6d891c84d07d0551d89a0b1c8bfec4b6e89ced24241ab84d132252ce5294"),
            ("Q4_K_M/DeepSeek-V3.1-Q4_K_M-00008-of-00009.gguf", 47035800288,
             "34fa6d5c853b3c0945e48bd61aeab0417540a839cf8f5dee751be468adbeb092"),
            ("Q4_K_M/DeepSeek-V3.1-Q4_K_M-00009-of-00009.gguf", 14816275232,
             "947c42d5b5c085edaf93771c6a90ae9fdbe2b78a176e571381520473b474e5a2")]),
]


def find(catalog_id):
    """Item do catálogo pelo id (cópia, para ninguém alterar o catálogo sem querer); os gigantes também."""
    return next((dict(i) for i in CATALOG + SPLIT_CATALOG if i["id"] == catalog_id), None)


def suggest_name(filename):
    """Nome válido na rede a partir do arquivo: 'Phi-3.5-mini-Q4_K_M.gguf' -> 'phi-3.5-mini-q4-k-m'."""
    base = filename.rsplit("/", 1)[-1]
    base = re.sub(r"\.gguf$", "", base, flags=re.I).lower()
    name = re.sub(r"[^a-z0-9.-]+", "-", base).strip(".-")[:48].rstrip(".-")
    return name if MODEL_NAME.match(name) else ("modelo-" + name)[:48].rstrip(".-")


_LICENSES = {"apache-2.0": "Apache-2.0", "mit": "MIT", "bsd-2-clause": "BSD-2-Clause",
             "bsd-3-clause": "BSD-3-Clause", "cc-by-4.0": "CC-BY-4.0", "cc-by-sa-4.0": "CC-BY-SA-4.0",
             "gpl-3.0": "GPL-3.0", "lgpl-3.0": "LGPL-3.0", "openrail": "OpenRAIL", "llama3.1": "Llama-3.1",
             "llama3.2": "Llama-3.2", "llama3.3": "Llama-3.3", "gemma": "Gemma"}


def _license_of(info):
    card = info.get("cardData") or {}
    lic = card.get("license")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    if lic == "other" and card.get("license_name"):
        lic = card["license_name"]
    if not lic:
        lic = next((t.split(":", 1)[1] for t in info.get("tags", []) if t.startswith("license:")), "")
    return _LICENSES.get(str(lic).lower(), str(lic))[:60]


UNDECLARED = ("", "other", "unknown")   # o Hugging Face não diz qual é: ninguém sabe se pode cobrar
LICENSE_UNDECLARED = "esse modelo não declara a licença no Hugging Face: escolha um que declare"


def license_error(license):
    """Por que essa licença impede servir o modelo cobrando em SIG (texto para a pessoa), ou None se pode."""
    lic = str(license or "").strip().lower()
    if lic in UNDECLARED:
        return LICENSE_UNDECLARED
    if (re.search(r"(^|[-_ ])nc($|[-_ ])", lic) or "non-commercial" in lic or "noncommercial" in lic
            or "research" in lic):
        return (f"a licença {license} não permite uso comercial: as máquinas cobram em SIG para servir o modelo, "
                f"então escolha outro")
    return None


def commercial_ok(license):
    """Licença declarada e sem "NC" (não comercial) nem "só pesquisa": quem cobra em SIG pode servir."""
    return license_error(license) is None


def _parse_url(url):
    u = urllib.parse.urlparse(str(url or "").strip())
    if u.scheme not in ("http", "https") or u.netloc.lower() not in ("huggingface.co", "www.huggingface.co", "hf.co"):
        raise ValueError("use um link do Hugging Face (https://huggingface.co/<autor>/<repositório>/…)")
    parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("link incompleto: falta o repositório (https://huggingface.co/<autor>/<repositório>)")
    repo, rest = f"{parts[0]}/{parts[1]}", parts[2:]
    if not rest:
        return repo, "main", None
    if rest[0] in ("resolve", "blob") and len(rest) >= 3:
        return repo, rest[1], "/".join(rest[2:])
    if rest[0] == "tree" and len(rest) >= 2:
        return repo, rest[1], None
    raise ValueError("link não reconhecido: copie o link do arquivo .gguf (botão de download) no Hugging Face")


def _pick_gguf(files):
    """Link só do repositório: escolhe o Q4_K_M (bom equilíbrio entre qualidade e memória)."""
    single = [f for f in files if not re.search(r"-\d{5}-of-\d{5}\.gguf$", f, re.I)]
    best = sorted((f for f in single if "q4_k_m" in f.lower()), key=len)
    if best:
        return best[0]
    if len(single) == 1:
        return single[0]
    if not single:
        raise ValueError("este repositório não tem arquivos .gguf de uma parte só")
    raise ValueError("escolha um arquivo .gguf do repositório, por exemplo: " + ", ".join(single[:5]))


def resolve_hf_url(url, timeout=20):
    """Descobre tamanho, SHA-256 e licença de um GGUF do Hugging Face (sem baixar o modelo).

    Devolve {"repo", "file", "url", "size", "sha256", "license"}; levanta ValueError com a explicação.
    """
    repo, rev, file = _parse_url(url)
    api = f"{HF}/api/models/{urllib.parse.quote(repo)}"
    if rev != "main":
        api += f"/revision/{urllib.parse.quote(rev, safe='')}"
    req = urllib.request.Request(api + "?blobs=true", headers={"User-Agent": "sigma-app/2.0",
                                                                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            info = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise ValueError(f"repositório {repo} não encontrado (ou é privado)") from None
        raise ValueError(f"o Hugging Face recusou a consulta (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ValueError(f"não consegui consultar o Hugging Face: {getattr(e, 'reason', e)}") from None
    if info.get("gated") or info.get("private"):
        raise ValueError("este modelo exige login ou aceite de termos no Hugging Face: as máquinas da rede "
                         "não conseguiriam baixá-lo")
    siblings = {s.get("rfilename"): s for s in info.get("siblings", []) if isinstance(s, dict)}
    if file is None:
        file = _pick_gguf([f for f in siblings if str(f).lower().endswith(".gguf")])
    if not file.lower().endswith(".gguf"):
        raise ValueError("o arquivo precisa ser um .gguf (formato que o motor de IA da rede usa)")
    if re.search(r"-\d{5}-of-\d{5}\.gguf$", file, re.I):
        raise ValueError("modelos divididos em várias partes ainda não são suportados; escolha um .gguf único")
    s = siblings.get(file)
    if not s:
        raise ValueError(f"o arquivo {file} não existe em {repo}")
    lfs = s.get("lfs") or {}
    sha256, size = str(lfs.get("sha256", "")).lower(), lfs.get("size") or s.get("size")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or not isinstance(size, int):
        raise ValueError("o Hugging Face não informou o SHA-256 desse arquivo")
    if not 0 < size <= MAX_SIZE:
        raise ValueError("o modelo precisa ter no máximo 60 GB")
    return {"repo": repo, "file": file,
            "url": f"{HF}/{repo}/resolve/{urllib.parse.quote(rev, safe='')}/{urllib.parse.quote(file)}",
            "size": size, "sha256": sha256, "license": _license_of(info)}
