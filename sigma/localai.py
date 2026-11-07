"""Modo local: baixar modelos do catálogo para este PC e conversar/programar com eles sem passar pela rede.

Os arquivos ficam em <dados>/local-models (SHA-256 conferido). O motor é um llama.cpp próprio, separado do que serve a
rede. "Economia de memória" (lowmem): o modelo é lido do disco aos poucos (mmap), camada por camada conforme o motor
precisa, então cabe modelo maior que a RAM livre (fica mais lento); desligado, o modelo inteiro vai para a RAM antes
(--load-mode none), mais rápido.
"""
import os
import threading

from . import catalog, engines


class LocalAI:
    def __init__(self, data_dir, log=print, threads=None):
        self.dir = os.path.join(data_dir, "local-models")
        self.log = log
        self.engine = engines._LlamaCpp(os.path.join(data_dir, "local"), log, threads or max(1, os.cpu_count() // 2))
        self.lowmem = True
        self.jobs = {}                  # id -> {"progress", "error"}
        self.lock = threading.Lock()
        orig = self.engine._command

        def command(exe, variant, path, port, rpc=()):
            cmd = orig(exe, variant, path, port, rpc)
            return cmd + ["--load-mode", "mmap" if self.lowmem else "none"]
        self.engine._command = command

    def path(self, item):
        return os.path.join(self.dir, f"{item['sha256']}.gguf")

    def models(self):
        """[{id, name, size, ram_mb, status: baixado|baixando|erro|—, progress}] dos modelos de texto do catálogo."""
        out = []
        for it in catalog.CATALOG:
            if it.get("task", "text") != "text":
                continue
            j = self.jobs.get(it["id"])
            st = ("baixado" if os.path.isfile(self.path(it) + ".ok") else
                  "baixando" if j and j.get("error") is None and j["progress"] < 1 else "erro" if j and j.get("error") else "")
            out.append({"id": it["id"], "name": it["name"], "size": it["size"], "ram_mb": it["ram_mb"], "status": st,
                        "progress": j["progress"] if j else 0, "error": j.get("error") if j else None})
        return out

    def download(self, catalog_id):
        it = catalog.find(catalog_id)
        if not it or it.get("task", "text") != "text" or it.get("split"):
            raise ValueError("esse modelo não pode ser baixado para o modo local (só modelos de texto de um arquivo)")
        with self.lock:
            if catalog_id in self.jobs and self.jobs[catalog_id].get("error") is None and self.jobs[catalog_id]["progress"] < 1:
                return
            self.jobs[catalog_id] = {"progress": 0.0, "error": None}

        def run():
            job = self.jobs[catalog_id]
            try:
                os.makedirs(self.dir, exist_ok=True)
                engines.fetch(it["url"], self.path(it), it["sha256"], size=it["size"],
                              progress=lambda d, t: job.update(progress=d / t if t else 0))
                open(self.path(it) + ".ok", "w").close()
                job["progress"] = 1.0
                self.log(f"[local] {it['name']} baixado")
            except Exception as e:
                job["error"] = str(e)[:200]
        threading.Thread(target=run, name="sigma-local-baixar", daemon=True).start()

    def ready(self, catalog_id):
        it = catalog.find(catalog_id)
        return bool(it) and os.path.isfile(self.path(it) + ".ok")

    def ask(self, model, messages, max_tokens=1024, json_mode=False, **kw):
        """Igual ao client.ask, mas no motor deste PC: (texto, job) com custo zero."""
        it = catalog.find(model)
        if not it or not self.ready(model):
            raise ValueError(f"o modelo {model} não está baixado neste computador")
        if model not in self.engine.loaded():
            self.engine.load(model, self.path(it), it["sha256"])
        text, tin, tout = self.engine.chat(model, messages, max_tokens, json_mode=json_mode)
        return text, {"id": None, "cost": 0, "machine": "local", "attempts": 1, "status": "done",
                      "usage": {"tokens_in": tin, "tokens_out": tout}}

    def set_lowmem(self, on):
        self.lowmem = bool(on)
        for name in self.engine.resident():   # vale na próxima carga
            self.engine._stop_proc(name)
