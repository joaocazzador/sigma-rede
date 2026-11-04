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


def _item(id_, name, repo, file, size, sha256, license, description, task="text", files=(), recipe=None):
    """Um modelo do catálogo. Imagem/vídeo (regras v2): `files` são os arquivos extras (VAE, codificador de
    texto) como (papel, repo, arquivo, tamanho, sha256) e `recipe` os passos e a escala que o modelo pede."""
    extra = [{"role": role, "url": f"{HF}/{r}/resolve/main/{f}", "sha256": h, "size": n}
             for role, r, f, n, h in files]
    total = size + sum(x["size"] for x in extra)
    item = {"id": id_, "name": name, "repo": repo, "file": file, "url": f"{HF}/{repo}/resolve/main/{file}",
            "size": size, "sha256": sha256, "license": license, "description": description,
            "ram_mb": ram_mb(total, task), "task": task}
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
          "Programador leve: escreve, completa e explica código em dezenas de linguagens."),
    _item("smollm2-1.7b", "SmolLM2 1.7B Instruct", "bartowski/SmolLM2-1.7B-Instruct-GGUF",
          "SmolLM2-1.7B-Instruct-Q4_K_M.gguf", 1055609824,
          "77665ea4815999596525c636fbeb56ba8b080b46ae85efef4f0d986a139834d7", "Apache-2.0",
          "Compacto e treinado com dados abertos; bom para conversas simples (melhor em inglês)."),
    _item("deepseek-r1-qwen-1.5b", "DeepSeek R1 Distill Qwen 1.5B", "bartowski/DeepSeek-R1-Distill-Qwen-1.5B-GGUF",
          "DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf", 1117320800,
          "1741e5b2d062b07acf048bf0d2c514dadf2a48f94e2b4aa0cfe069af3838ee2f", "MIT",
          "Pensa passo a passo antes de responder: bom para matemática e lógica."),
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
          "Um dos melhores modelos abertos de programação do seu tamanho; pede uns 6 GB de memória."),
    _item("qwen3-8b", "Qwen3 8B", "Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", 5027783488,
          "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785", "Apache-2.0",
          "Geração nova da Qwen: pensa antes de responder quando a pergunta pede; pede uns 7 GB de memória."),
    _item("phi-4", "Phi-4 14B", "bartowski/phi-4-GGUF", "phi-4-Q4_K_M.gguf", 9053114816,
          "009aba717c09d4a35890c7d35eb59d54e1dba884c7c526e7197d9c13ab5911d9", "MIT",
          "Da Microsoft: muito bom em matemática, lógica e código; pede uns 12 GB de memória."),
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
          recipe={"steps": 20, "cfg": 6.0, "sampler": "euler", "size": "832x480", "flow_shift": 3.0}),
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
          recipe={"steps": 20, "cfg": 6.0, "sampler": "euler", "size": "832x480", "flow_shift": 3.0}),
]

# Gigantes: não cabem em nenhum computador comum e só rodam divididos entre várias máquinas (a divisão aberta,
# docs/PENDENCIAS.md). Aparecem no catálogo para mostrar aonde a rede vai, mas ainda não se apadrinham: a chain só
# aceita um arquivo de até 60 GB por modelo. `size` é a soma das partes; `parts`, quantos arquivos o GGUF tem.
def _giant(id_, name, repo, size, parts, license, description):
    return {"id": id_, "name": name, "repo": repo, "url": f"{HF}/{repo}", "size": size, "parts": parts,
            "license": license, "description": description, "ram_mb": ram_mb(size), "task": "text", "split": True}


SPLIT_CATALOG = [
    _giant("gpt-oss-120b", "gpt-oss 120B", "ggml-org/gpt-oss-120b-GGUF", 63387346208, 1, "Apache-2.0",
           "O maior modelo aberto da OpenAI, perto dos modelos fechados em raciocínio."),
    _giant("qwen3-235b-a22b", "Qwen3 235B-A22B", "Qwen/Qwen3-235B-A22B-GGUF", 142154074176, 5, "Apache-2.0",
           "O topo da família Qwen3: conversa, raciocina e programa no nível dos melhores."),
    _giant("glm-4.5", "GLM-4.5 355B", "unsloth/GLM-4.5-GGUF", 216455572672, 5, "MIT",
           "Da Zhipu: forte em agentes, ferramentas e programação."),
    _giant("qwen3-coder-480b", "Qwen3 Coder 480B-A35B", "unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF",
           290058826208, 6, "Apache-2.0", "O maior modelo aberto de programação: projetos inteiros, não só trechos."),
    _giant("deepseek-r1-0528", "DeepSeek R1 671B", "unsloth/DeepSeek-R1-0528-GGUF", 404941900832, 9, "MIT",
           "O DeepSeek R1 completo, 671 bilhões de parâmetros: pensa longamente antes de responder."),
]


def find(catalog_id):
    """Item do catálogo pelo id (cópia, para ninguém alterar o catálogo sem querer)."""
    return next((dict(i) for i in CATALOG if i["id"] == catalog_id), None)


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
