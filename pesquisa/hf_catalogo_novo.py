"""Consulta o Hugging Face (só metadados: licença, tamanho, SHA-256) dos candidatos a entrar no catálogo.

    python pesquisa/hf_catalogo_novo.py > saida.json
"""
import json
import re
import sys
import urllib.request

# (repo, padrão do arquivo): o primeiro arquivo que casa
TEXT = [
    ("Qwen/Qwen3-0.6B-GGUF", r"Q8_0\.gguf$"),
    ("Qwen/Qwen3-1.7B-GGUF", r"Q8_0\.gguf$"),
    ("Qwen/Qwen3-4B-GGUF", r"Q4_K_M\.gguf$"),
    ("unsloth/Qwen3-4B-Instruct-2507-GGUF", r"Q4_K_M\.gguf$"),
    ("unsloth/Qwen3-4B-Thinking-2507-GGUF", r"Q4_K_M\.gguf$"),
    ("unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF", r"-Q4_K_M\.gguf$"),
    ("unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF", r"-Q4_K_M\.gguf$"),
    ("Qwen/Qwen2.5-7B-Instruct-GGUF", r"q4_k_m\.gguf$"),
    ("bartowski/Qwen2.5-14B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("bartowski/Qwen2.5-32B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF", r"q8_0\.gguf$"),
    ("bartowski/Qwen2.5-Coder-14B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("bartowski/Qwen2.5-Coder-32B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("bartowski/Qwen2.5-Math-7B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF", r"Qwen-7B-Q4_K_M\.gguf$"),
    ("bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF", r"Qwen-14B-Q4_K_M\.gguf$"),
    ("bartowski/DeepSeek-R1-Distill-Qwen-32B-GGUF", r"Qwen-32B-Q4_K_M\.gguf$"),
    ("unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF", r"-Q4_K_M\.gguf$"),
    ("unsloth/Phi-4-mini-instruct-GGUF", r"-Q4_K_M\.gguf$"),
    ("unsloth/Phi-4-reasoning-plus-GGUF", r"-Q4_K_M\.gguf$"),
    ("bartowski/Mistral-Nemo-Instruct-2407-GGUF", r"2407-Q4_K_M\.gguf$"),
    ("unsloth/Devstral-Small-2507-GGUF", r"-Q4_K_M\.gguf$"),
    ("unsloth/Magistral-Small-2507-GGUF", r"-Q4_K_M\.gguf$"),
    ("bartowski/allenai_OLMo-2-1124-7B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("bartowski/allenai_OLMo-2-1124-13B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("ibm-granite/granite-3.3-2b-instruct-GGUF", r"Q4_K_M\.gguf$"),
    ("ibm-granite/granite-3.3-8b-instruct-GGUF", r"Q4_K_M\.gguf$"),
    ("bartowski/SmolLM2-360M-Instruct-GGUF", r"Instruct-Q8_0\.gguf$"),
    ("ggml-org/SmolLM3-3B-GGUF", r"Q4_K_M\.gguf$"),
    ("unsloth/GLM-4-9B-0414-GGUF", r"-Q4_K_M\.gguf$"),
    ("unsloth/GLM-Z1-9B-0414-GGUF", r"-Q4_K_M\.gguf$"),
    ("ggml-org/gemma-3-1b-it-GGUF", r"Q4_K_M\.gguf$"),
    ("ggml-org/gemma-3-4b-it-GGUF", r"Q4_K_M\.gguf$"),
    ("ggml-org/gemma-3-12b-it-GGUF", r"Q4_K_M\.gguf$"),
    ("ggml-org/gemma-3-27b-it-GGUF", r"Q4_K_M\.gguf$"),
    ("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("bartowski/Llama-3.2-1B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("bartowski/Llama-3.2-3B-Instruct-GGUF", r"Instruct-Q4_K_M\.gguf$"),
    ("unsloth/Hermes-3-Llama-3.1-8B-GGUF", r"-Q4_K_M\.gguf$"),
    ("bartowski/aya-expanse-8b-GGUF", r"8b-Q4_K_M\.gguf$"),
]
MEDIA = [
    ("city96/Qwen-Image-gguf", r"qwen-image-Q4_K_M\.gguf$"),
    ("unsloth/Qwen2.5-VL-7B-Instruct-GGUF", r"-Q4_K_M\.gguf$"),
    ("Comfy-Org/Qwen-Image_ComfyUI", r"vae/qwen_image_vae\.safetensors$"),
    ("silveroxides/Chroma-GGUF", r"Q4_K_M\.gguf$"),
    ("QuantStack/Wan2.2-TI2V-5B-GGUF", r"Q4_K_M\.gguf$"),
    ("Comfy-Org/Wan_2.2_ComfyUI_Repackaged", r"vae/wan2\.2_vae\.safetensors$"),
    ("stabilityai/stable-diffusion-2-1", r"^v2-1_768-ema-pruned\.safetensors$"),
    ("segmind/SSD-1B", r"^SSD-1B\.safetensors$"),
    ("Lykon/dreamshaper-8", r"DreamShaper_8_pruned\.safetensors$"),
    ("stabilityai/sd-turbo", r"^sd_turbo\.safetensors$"),
]


def look(repo, pat):
    req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}?blobs=true",
                                 headers={"User-Agent": "sigma-catalogo"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:
        return {"repo": repo, "erro": str(e)[:120]}
    card = d.get("cardData") or {}
    files = [(s["rfilename"], (s.get("lfs") or {}).get("size") or s.get("size"), (s.get("lfs") or {}).get("sha256"))
             for s in d.get("siblings", []) if re.search(pat, s["rfilename"])]
    files.sort(key=lambda f: len(f[0]))
    return {"repo": repo, "license": card.get("license") or (d.get("tags") and [t for t in d["tags"] if t.startswith("license:")]),
            "gated": d.get("gated"), "files": files[:3]}


out = [look(r, p) for r, p in TEXT + MEDIA]
json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
