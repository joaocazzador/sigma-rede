"""Internet para as IAs da rede: o app de quem pergunta busca na web e lê as primeiras páginas, e o texto vai junto com a
pergunta para a máquina que responde (ela não navega: só recebe o que o app trouxe).

A busca é no DuckDuckGo (página HTML, sem chave). Tudo passa pelo netsafe: nada de endereços internos (o roteador, o
painel, outros PCs da casa), redirecionamento conferido e tamanho limitado.
"""
import html
import re
import urllib.parse
import urllib.request

from . import netsafe

SEARCH_URL = "https://lite.duckduckgo.com/lite/"   # a versão leve (a "html" barra robôs)
UA = "Mozilla/5.0 (Sigma; +https://github.com/joaocazzador/sigma-rede)"
MAX_PAGE = 600_000          # bytes lidos de cada página
PAGE_CHARS = 2500           # texto de cada página que vai para a IA
RESULTS = 5
PAGES = 3

_LINK = re.compile(r'<a[^>]+href="([^"]+)"[^>]*class=.result-link.[^>]*>(.*?)</a>', re.S)
_SNIP = re.compile(r'class=.result-snippet.[^>]*>(.*?)</td>', re.S)
_TAG = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def _get(url, timeout=12, data=None):
    headers = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers)
    with netsafe.safe_opener().open(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        data = netsafe.read_capped(r, MAX_PAGE)
    return data.decode("utf-8", "replace"), ctype


def _text(raw):
    t = html.unescape(_TAG.sub(" ", raw))
    return re.sub(r"\s+", " ", t).strip()


def _real_url(href):
    """O DuckDuckGo devolve /l/?uddg=<endereço>: tira o endereço de verdade."""
    q = urllib.parse.urlparse(href if href.startswith("http") else "https:" + href)
    target = urllib.parse.parse_qs(q.query).get("uddg", [None])[0]
    return target or (href if href.startswith("http") else None)


def search(query, results=RESULTS):
    """[{"title", "url", "snippet"}] dos primeiros resultados."""
    page, _ = _get(SEARCH_URL, data=urllib.parse.urlencode({"q": query[:300]}).encode())
    snippets = _SNIP.findall(page)
    out = []
    for (href, title), snippet in zip(_LINK.findall(page), snippets + [""] * 20):
        url = _real_url(html.unescape(href))
        if url and url.startswith(("http://", "https://")):
            out.append({"title": _text(title)[:200], "url": url, "snippet": _text(snippet)[:400]})
        if len(out) >= results:
            break
    return out


def context(query, pages=PAGES):
    """Texto para a IA: os resultados da busca e o começo das primeiras páginas, com o endereço de cada uma.
    (texto, fontes). Uma página que não abre fica só com o resumo da busca."""
    found = search(query)
    if not found:
        return "", []
    parts = []
    for i, r in enumerate(found, 1):
        body = ""
        if i <= pages:
            try:
                raw, ctype = _get(r["url"])
                if "html" in ctype or "text" in ctype:
                    body = _text(raw)[:PAGE_CHARS]
            except Exception:
                body = ""
        parts.append(f"[{i}] {r['title']}\n{r['url']}\n{body or r['snippet']}")
    return "\n\n".join(parts), found
