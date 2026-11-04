"""Tarefas de computação determinísticas.

Como o resultado é determinístico, a rede pode mandar a mesma tarefa para
várias máquinas e comparar os hashes: quem divergir da maioria é punido.
A própria chain calcula as "unidades de trabalho" de cada tarefa, então o
cliente não consegue declarar menos trabalho do que pede.
"""
import hashlib


def _primes(n):
    n = int(n)
    sieve = bytearray([1]) * (n + 1)
    sieve[0:2] = b"\x00\x00"
    for i in range(2, int(n ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i::i] = bytearray(len(range(i * i, n + 1, i)))
    return {"count": sum(sieve)}


def _hash_chain(seed, rounds):
    h = str(seed).encode()
    for _ in range(int(rounds)):
        h = hashlib.sha256(h).digest()
    return {"digest": h.hex()}


def _mandelbrot(width, height, iterations):
    inside = 0
    for py in range(int(height)):
        for px in range(int(width)):
            c = complex(-2.0 + 3.0 * px / width, -1.5 + 3.0 * py / height)
            z = 0j
            for _ in range(int(iterations)):
                z = z * z + c
                if abs(z) > 2:
                    break
            else:
                inside += 1
    return {"inside": inside}


TASKS = {
    "primes": {"fn": _primes, "params": ["n"], "max": {"n": 50_000_000},
               "units": lambda p: p["n"] // 100_000 + 1},
    "hash_chain": {"fn": _hash_chain, "params": ["seed", "rounds"], "max": {"rounds": 20_000_000},
                   "units": lambda p: p["rounds"] // 50_000 + 1},
    "mandelbrot": {"fn": _mandelbrot, "params": ["width", "height", "iterations"],
                   "max": {"width": 2000, "height": 2000, "iterations": 2000},
                   "units": lambda p: p["width"] * p["height"] * p["iterations"] // 2_000_000 + 1},
}


def validate(task):
    """Retorna as unidades de trabalho ou levanta ValueError."""
    if not isinstance(task, dict) or task.get("name") not in TASKS:
        raise ValueError("tarefa desconhecida; disponíveis: " + ", ".join(TASKS))
    spec = TASKS[task["name"]]
    params = task.get("params", {})
    if not isinstance(params, dict) or sorted(params) != sorted(spec["params"]):
        raise ValueError("parâmetros esperados: " + ", ".join(spec["params"]))
    if not all(isinstance(v, (int, str)) and len(str(v)) <= 64 for v in params.values()):
        raise ValueError("parâmetros devem ser inteiros ou textos curtos")
    for k, limit in spec["max"].items():
        if not isinstance(params[k], int) or not 0 < params[k] <= limit:
            raise ValueError(f"{k} deve ser inteiro entre 1 e {limit}")
    return spec["units"](params)


def run(task):
    return TASKS[task["name"]]["fn"](**task["params"])
