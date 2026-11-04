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


def ram_mb(size):
    """Memória que uma máquina precisa reservar para servir o modelo (mesma conta da chain)."""
    return math.ceil(size / 1e6 * 1.2) + 512


def _item(id_, name, repo, file, size, sha256, license, description):
    return {"id": id_, "name": name, "repo": repo, "file": file, "url": f"{HF}/{repo}/resolve/main/{file}",
            "size": size, "sha256": sha256, "license": license, "description": description,
            "ram_mb": ram_mb(size)}


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
