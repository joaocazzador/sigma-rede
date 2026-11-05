"""Vídeo dividido entre rpc-servers, de ponta a ponta, com os programas de verdade.

Gera o mesmo vídeo duas vezes com o Wan 2.1 1.3B: uma só neste PC (como o Sigma faz hoje) e outra com o modelo de
difusão dividido entre N rpc-servers locais + a CPU do coordenador (como no grupo de vídeo). Compara o SHA-256 e o
tempo. Os rpc-servers aqui ficam em 127.0.0.1 (sem túnel): o teste é do motor; o túnel é o mesmo do grupo de texto.

    python pesquisa/grupo/e2e_video.py --sd C:\\sgtmp\\sdrpc\\bin\\sd-cli.exe --rpc C:\\sgtmp\\sdrpc\\bin\\rpc-server.exe \\
        --models C:\\sgtmp\\sdrpc\\models [--servers 2] [--frames 9] [--steps 4]
"""
import argparse
import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from sigma import media  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"rpc-server não abriu a porta {port}")


def run(cmd, out, log):
    t = time.time()
    with open(log, "wb") as f:
        code = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode
    took = time.time() - t
    if code or not os.path.isfile(out):
        tail = open(log, "rb").read()[-1500:].decode(errors="replace")
        raise SystemExit(f"sd-cli falhou ({code}):\n{tail}")
    return hashlib.sha256(open(out, "rb").read()).hexdigest(), took


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sd", required=True)
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--models", required=True)
    ap.add_argument("--servers", type=int, default=2)
    ap.add_argument("--frames", type=int, default=9)
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--size", default="320x192")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--skip-local", action="store_true")
    a = ap.parse_args()
    w, h = a.size.split("x")
    spec = {"task": "video", "prompt": "a red fox running in the snow", "negative": "", "width": int(w),
            "height": int(h), "steps": a.steps, "cfg": 6.0, "sampler": "euler", "seed": 42, "frames": a.frames,
            "flow_shift": 3.0, "seconds": 1}
    m = {"files": {"main": os.path.join(a.models, "wan13.safetensors"),
                   "vae": os.path.join(a.models, "vae.safetensors"),
                   "t5xxl": os.path.join(a.models, "umt5.gguf")}}
    eng = media._SdCpp(tempfile.mkdtemp(), print, a.threads)
    work = tempfile.mkdtemp(prefix="e2e-video-")

    results = {}
    if not a.skip_local:
        out = os.path.join(work, "local.webm")
        results["local"] = run(eng._command(a.sd, m, spec, out), out, out + ".log")
        print(f"local:    {results['local'][0][:16]}…  {results['local'][1]:.0f} s")

    procs, rpc = [], []
    try:
        for _ in range(a.servers):
            port = free_port()
            procs.append(subprocess.Popen([a.rpc, "-H", "127.0.0.1", "-p", str(port), "-t", str(a.threads)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            wait_port(port)
            rpc.append(f"127.0.0.1:{port}")
        group = eng.group_args(a.sd, rpc)
        print("dividido:", " ".join(group))
        out = os.path.join(work, "dividido.webm")
        results["split"] = run(eng._command(a.sd, m, spec, out, group), out, out + ".log")
        print(f"dividido: {results['split'][0][:16]}…  {results['split'][1]:.0f} s")
        log = open(out + ".log", "rb").read().decode(errors="replace")
        for line in log.splitlines():
            if "RPC" in line and ("MB" in line or "layer" in line.lower() or "block" in line.lower()):
                print("   ", line.strip()[:160])
    finally:
        for p in procs:
            p.kill()
    if "local" in results:
        same = results["local"][0] == results["split"][0]
        print("vídeo idêntico ao de um PC só" if same else "vídeo DIFERENTE do de um PC só")
    print("arquivos em", work)


if __name__ == "__main__":
    main()
