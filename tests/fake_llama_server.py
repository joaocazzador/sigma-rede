"""llama-server falso para testar o gerenciamento de processos do sigma.engines (aceita os mesmos argumentos).

/health responde 503 ("carregando") no primeiro segundo e depois 200, sem pedir chave (como o de verdade);
/v1/chat/completions exige `Authorization: Bearer <LLAMA_API_KEY>` quando a variável de ambiente existe (como o
llama-server de verdade com a chave) e devolve um eco com `usage`; GET /chave conta se a chave chegou pelo
ambiente. Com --fail sai na hora com erro (simula a versão Vulkan que não sobe).
"""
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("-c", type=int)
    ap.add_argument("--jinja", action="store_true")
    ap.add_argument("-t", type=int)
    ap.add_argument("-np", type=int)
    ap.add_argument("-ngl", type=int)
    ap.add_argument("--fail", action="store_true")
    a = ap.parse_args()
    if a.fail:
        print("ggml_vulkan: no devices found (falha simulada)", flush=True)
        sys.exit(3)
    started = time.time()
    key = os.environ.get("LLAMA_API_KEY")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *x):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                if time.time() - started < 1:
                    return self._send(503, {"error": {"message": "Loading model"}})
                return self._send(200, {"status": "ok"})
            if self.path == "/chave":
                return self._send(200, {"key_from_env": bool(key)})
            self._send(404, {})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if key and self.headers.get("Authorization") != f"Bearer {key}":
                return self._send(401, {"error": {"message": "Invalid API Key", "type": "authentication_error"}})
            last = req["messages"][-1]["content"]
            text = json.dumps({"eco": last}) if req.get("response_format") else f"eco: {last} (modelo {a.m})"
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                             "usage": {"prompt_tokens": len(last.split()), "completion_tokens": len(text.split()),
                                       "threads": a.t, "ctx": a.c, "max_tokens": req.get("max_tokens")}})

    print(f"servidor falso ouvindo em {a.host}:{a.port}", flush=True)
    HTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
