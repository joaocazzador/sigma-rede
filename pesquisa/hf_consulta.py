import json, re, sys, urllib.request
Q = [
 ("Qwen/Qwen3-8B-GGUF", r"Q4_K_M\.gguf$"), ("Qwen/Qwen3-14B-GGUF", r"Q4_K_M\.gguf$"),
 ("Qwen/Qwen3-30B-A3B-GGUF", r"Q4_K_M\.gguf$"), ("Qwen/Qwen3-32B-GGUF", r"Q4_K_M\.gguf$"),
 ("bartowski/phi-4-GGUF", r"phi-4-Q4_K_M\.gguf$"), ("bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF", r"Q4_K_M\.gguf$"),
 ("bartowski/mistralai_Mistral-Small-3.1-24B-Instruct-2503-GGUF", r"Instruct-2503-Q4_K_M\.gguf$"),
 ("ggml-org/gpt-oss-20b-GGUF", r"\.gguf$"), ("ggml-org/gpt-oss-120b-GGUF", r"\.gguf$"),
 ("city96/FLUX.1-schnell-gguf", r"Q4_K_S\.gguf$"), ("black-forest-labs/FLUX.1-schnell", r"^ae\.safetensors$"),
 ("comfyanonymous/flux_text_encoders", r"^clip_l\.safetensors$"), ("city96/t5-v1_1-xxl-encoder-gguf", r"Q4_K_M\.gguf$"),
 ("stabilityai/stable-diffusion-xl-base-1.0", r"^sd_xl_base_1\.0\.safetensors$"),
 ("city96/Wan2.1-T2V-14B-gguf", r"Q4_K_M\.gguf$"),
 ("Qwen/Qwen3-235B-A22B-GGUF", r"Q4_K_M"), ("unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF", r"Q4_K_M"),
 ("unsloth/DeepSeek-R1-0528-GGUF", r"Q4_K_M"), ("unsloth/GLM-4.5-GGUF", r"Q4_K_M"),
]
for repo, pat in Q:
    try:
        req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}?blobs=true", headers={"User-Agent": "sigma-app/2.6"})
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:
        print("==", repo, "ERRO", e); continue
    card = d.get("cardData") or {}
    fs = [(s["rfilename"], (s.get("lfs") or {}).get("size") or s.get("size"), (s.get("lfs") or {}).get("sha256")) for s in d.get("siblings", []) if re.search(pat, s["rfilename"])]
    print("==", repo, "| lic:", card.get("license"), "| gated:", d.get("gated"), "| n", len(fs), "| total", sum(f[1] or 0 for f in fs))
    for f in fs: print("   ", *f)
