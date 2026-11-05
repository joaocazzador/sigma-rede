"""Consulta no Hugging Face (só metadados) os candidatos a gigantes: modelos que só rodam divididos num grupo.

Para cada (repo, padrão) junta todas as partes do GGUF que casam (os arquivos -0000N-of-0000M.gguf) e mostra o
total, a licença e quantos PCs de 16 GB (~9 GB emprestados cada) o modelo pede.

    python pesquisa/hf_gigantes.py > saida.json
"""
import json
import math
import re
import sys
import urllib.request

C = [
    # ~3 PCs
    ("TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF", r"Q3_K_M\.gguf$"),
    ("bartowski/Llama-3.3-70B-Instruct-GGUF", r"IQ2_XS\.gguf$"),
    ("ggml-org/gemma-3-27b-it-GGUF", r"Q8_0\.gguf$"),
    # ~4-5 PCs
    ("TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF", r"Q5_K_M\.gguf$"),
    ("bartowski/Llama-3.3-70B-Instruct-GGUF", r"Q3_K_M\.gguf$"),
    ("bartowski/DeepSeek-R1-Distill-Llama-70B-GGUF", r"Q3_K_M\.gguf$"),
    ("unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF", r"Q8_0\.gguf$"),
    # ~6-7 PCs
    ("bartowski/DeepSeek-R1-Distill-Llama-70B-GGUF", r"Q4_K_M\.gguf$"),
    ("TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF", r"Q8_0\.gguf$"),
    ("bartowski/Llama-3.3-70B-Instruct-GGUF", r"Q5_K_M"),
    # ~10 PCs
    ("unsloth/GLM-4.5-Air-GGUF", r"Q4_K_M"),
    ("unsloth/Llama-4-Scout-17B-16E-Instruct-GGUF", r"Q4_K_M"),
    ("unsloth/gpt-oss-120b-GGUF", r"Q8_0"),
    ("bartowski/Llama-3.3-70B-Instruct-GGUF", r"Q8_0"),
    # ~12-15 PCs
    ("bartowski/Mixtral-8x22B-Instruct-v0.1-GGUF", r"Q4_K_M"),
    ("unsloth/GLM-4.5-Air-GGUF", r"Q8_0"),
    ("unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF", r"Q2_K\b|Q2_K/"),
    # maiores
    ("unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF", r"Q4_K_M"),
    ("unsloth/Qwen3-235B-A22B-Thinking-2507-GGUF", r"Q4_K_M"),
    ("unsloth/Llama-4-Maverick-17B-128E-Instruct-GGUF", r"Q4_K_M"),
    ("unsloth/DeepSeek-V3.1-GGUF", r"Q4_K_M"),
    ("unsloth/Kimi-K2-Instruct-GGUF", r"Q2_K_XL|UD-Q2_K_XL"),
    ("unsloth/GLM-4.6-GGUF", r"Q4_K_M"),
    ("unsloth/MiniMax-M2-GGUF", r"Q4_K_M"),
]


def look(repo, pat):
    req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}?blobs=true",
                                 headers={"User-Agent": "sigma-catalogo"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=90).read())
    except Exception as e:
        return {"repo": repo, "pat": pat, "erro": str(e)[:120]}
    card = d.get("cardData") or {}
    files = sorted([(s["rfilename"], (s.get("lfs") or {}).get("size") or s.get("size"),
                     (s.get("lfs") or {}).get("sha256"))
                    for s in d.get("siblings", []) if re.search(pat, s["rfilename"]) and s["rfilename"].endswith(".gguf")
                    and "mmproj" not in s["rfilename"]])
    total = sum(f[1] or 0 for f in files)
    ram = math.ceil(total / 1e6 * 1.2) + 512
    return {"repo": repo, "pat": pat, "license": card.get("license"), "gated": d.get("gated"), "files": files,
            "total": total, "ram_mb": ram, "pcs16": math.ceil(ram / 9000)}


json.dump([look(r, p) for r, p in C], sys.stdout, indent=1, ensure_ascii=False)
