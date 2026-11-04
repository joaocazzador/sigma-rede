"""O que este computador tem para oferecer à rede: núcleos, memória, placa de vídeo e disco.

Tudo em MB decimais (1 MB = 1.000.000 bytes), a mesma unidade que a chain usa para `ram_mb` e `disk_mb`.
O painel e as configurações falam em GB como o Windows mostra (1 GB = 2^30 bytes): `gb_to_mb` converte.
"""
import os
import platform
import re
import shutil
import subprocess
import sys

NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW: o app congelado não pisca console
MB_PER_GB = 2 ** 30 / 1_000_000   # 1.073,741824 MB decimais em 1 GB do Windows
_gpu_cache = None


def gb_to_mb(gb) -> int:
    """GB das configurações (2^30 bytes, como o Windows mostra) -> MB decimais da chain (4 GB -> 4.294 MB)."""
    return int(float(gb or 0) * MB_PER_GB)


def mb_to_gb(mb) -> float:
    """MB decimais -> GB como o Windows mostra (2^30 bytes)."""
    return float(mb or 0) / MB_PER_GB


def _run(cmd, timeout=5):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def _ram_bytes():
    if os.name == "nt":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullTotalPhys
        return 0
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "hw.memsize"]).strip()
        return int(out) if out.isdigit() else 0
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def _nvidia():
    """Placas NVIDIA pelo nvidia-smi (vem com o driver). Devolve (nome, MB) da maior."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "", 0
    best = ("", 0)
    for line in _run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]).splitlines():
        name, _, mib = line.rpartition(",")
        try:
            mb = int(float(mib) * 1.048576)   # o nvidia-smi fala em MiB
        except ValueError:
            continue
        if mb > best[1]:
            best = (name.strip(), mb)
    return best


_DISCRETE = re.compile(r"\bRX\b|Radeon Pro|FirePro|Radeon VII|\bR9\b|Arc\(TM\) [AB]\d{3}|\bArc [AB]\d{3}", re.I)


def _windows_registry_gpu():
    """Sem nvidia-smi: placas AMD/Intel dedicadas pelo registro do Windows (sem abrir programa nenhum)."""
    try:
        import winreg
    except ImportError:
        return "", 0
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    best = ("", 0)
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return best
    with root:
        for i in range(32):
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            try:
                with winreg.OpenKey(root, sub) as k:
                    name = str(winreg.QueryValueEx(k, "DriverDesc")[0])
                    size = 0
                    for v in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                        try:
                            raw = winreg.QueryValueEx(k, v)[0]
                        except OSError:
                            continue
                        size = int.from_bytes(raw[:8], "little") if isinstance(raw, bytes) else int(raw)
                        break
            except (OSError, ValueError):
                continue
            # placa integrada (Intel HD/UHD/Iris, Radeon Graphics de APU) usa a RAM: não conta como dedicada
            dedicated = "nvidia" in name.lower() or bool(_DISCRETE.search(name))
            if dedicated and size // 1_000_000 > best[1]:
                best = (name, size // 1_000_000)
    return best


def _linux_amd_gpu():
    """Sem nvidia-smi no Linux: placas AMD pelo driver amdgpu (/sys/class/drm)."""
    best = ("", 0)
    try:
        cards = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        return best
    for card in cards:
        dev = os.path.join("/sys/class/drm", card, "device")
        try:
            with open(os.path.join(dev, "vendor")) as f:
                if f.read().strip() != "0x1002":
                    continue
            with open(os.path.join(dev, "mem_info_vram_total")) as f:
                mb = int(f.read().strip()) // 1_000_000
        except (OSError, ValueError):
            continue
        if mb >= 3000 and mb > best[1]:   # menos que isso é a fatia de RAM de uma APU
            best = ("AMD Radeon", mb)
    return best


def gpu():
    """(nome, vram_mb) da placa de vídeo dedicada, ou ("", 0). Não muda durante a execução: fica em cache."""
    global _gpu_cache
    if _gpu_cache is None:
        found = _nvidia()
        if not found[1]:
            if os.name == "nt":
                found = _windows_registry_gpu()
            elif sys.platform.startswith("linux"):
                found = _linux_amd_gpu()
        _gpu_cache = found if found[1] else ("", 0)
    return _gpu_cache


def cpu_name():
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        if sys.platform == "darwin":
            return _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except (OSError, ImportError, IndexError):
        pass
    return platform.processor() or ""


def cpu_cores():
    return max(1, (getattr(os, "process_cpu_count", None) or os.cpu_count)() or 1)


def disk_free_mb(path):
    """Espaço livre no disco onde fica `path` (sobe até a primeira pasta que já existe)."""
    path = os.path.abspath(path)
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    try:
        return shutil.disk_usage(path).free // 1_000_000
    except OSError:
        return 0


def os_name():
    system = {"Darwin": "macOS", "Windows": "Windows"}.get(platform.system(), platform.system() or sys.platform)
    release = platform.mac_ver()[0] if system == "macOS" else platform.release()
    return f"{system} {release} {platform.machine()}".replace("  ", " ").strip()


def detect(data_dir):
    name, vram = gpu()
    return {"cpu_cores": cpu_cores(), "ram_mb": _ram_bytes() // 1_000_000, "vram_mb": vram, "gpu": name,
            "disk_free_mb": disk_free_mb(data_dir), "os": os_name(), "cpu": cpu_name()}
