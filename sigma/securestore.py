"""Guarda segredos (chaves privadas) no disco sem deixá-los em texto puro.

No Windows usa a DPAPI da conta do usuário (CryptProtectData): o arquivo só abre
nesta mesma conta do Windows, então copiar a pasta para outro PC não leva a chave.
Nos outros sistemas, o arquivo fica com permissão 600 (só o dono lê).

    save_secret(caminho, b"...")    load_secret(caminho) -> bytes
"""
import os

_DPAPI = b"SIGMA-DPAPI-1\n"     # cabeçalho: diz como o resto do arquivo foi guardado
_PLAIN = b"SIGMA-PLAIN-1\n"
_ENTROPY = b"sigma-securestore-v1"   # outros programas da conta precisam saber disto para abrir
METHOD = "dpapi" if os.name == "nt" else "file"

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ARGS = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
             wintypes.DWORD, ctypes.POINTER(_Blob)]
    for _fn in (_crypt32.CryptProtectData, _crypt32.CryptUnprotectData):
        _fn.argtypes, _fn.restype = _ARGS, wintypes.BOOL
    _kernel32.LocalFree.argtypes, _kernel32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    _UI_FORBIDDEN = 0x01   # nunca abrir diálogo (o app pode estar rodando sem janela)

    def _blob(data):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _dpapi(data, protect):
        inp, _keep1 = _blob(data)
        ent, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        fn = _crypt32.CryptProtectData if protect else _crypt32.CryptUnprotectData
        desc = ctypes.c_wchar_p("Sigma") if protect else None
        if not fn(ctypes.byref(inp), desc, ctypes.byref(ent), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
            err = ctypes.get_last_error()
            raise OSError(f"o Windows não {'protegeu' if protect else 'abriu'} o segredo (erro {err})"
                          + ("" if protect else ": o arquivo está corrompido ou foi guardado por outra "
                                                "conta do Windows ou outro computador"))
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)


def _write_private(path, payload):
    """Escrita atômica, já criando o arquivo com permissão 600 (nunca existe aberto para todos)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if os.name != "nt":
        os.chmod(path, 0o600)


def save_secret(path, data: bytes):
    data = bytes(data)
    _write_private(path, _DPAPI + _dpapi(data, True) if os.name == "nt" else _PLAIN + data)


def load_secret(path) -> bytes:
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(_DPAPI):
        if os.name != "nt":
            raise OSError("este segredo foi protegido pelo Windows e só abre na mesma conta do Windows")
        return _dpapi(raw[len(_DPAPI):], False)
    if raw.startswith(_PLAIN):
        return raw[len(_PLAIN):]
    return raw   # arquivo sem cabeçalho (guardado à mão): devolve como está
