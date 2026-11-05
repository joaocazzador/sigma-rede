"""Mantém o computador acordado enquanto ele trabalha para a rede (hospedando modelos ou respondendo um pedido).

Um PC que dorme no meio de uma imagem ou de um vídeo larga o pedido: quem pediu espera o prazo inteiro e a
máquina perde reputação. A tela pode apagar normalmente; só o sono do sistema é adiado.

- Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED), segurado por uma thread própria (o pedido
  vale enquanto a thread que o fez estiver viva).
- macOS: `caffeinate -i -w <pid deste app>`, que solta sozinho se o app fechar.
- Linux: `systemd-inhibit --what=sleep`, quando existir (servidores e VPS quase nunca dormem).
"""
import os
import shutil
import subprocess
import sys
import threading

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


class KeepAwake:
    def __init__(self, log=None):
        self.log = log or (lambda *a: None)
        self.on = False
        self.lock = threading.Lock()
        self._ev = threading.Event()
        self._thread = None
        self._proc = None

    def set(self, want):
        """Liga (True) ou solta (False); chamar de novo com o mesmo valor não faz nada."""
        with self.lock:
            want = bool(want)
            if want == self.on:
                return
            self.on = want
            try:
                self._start() if want else self._stop()
            except Exception as e:   # sem como segurar o sono: o app segue igual
                self.log(f"[máquina] não consegui impedir o computador de dormir: {e}")

    def _start(self):
        if os.name == "nt":
            self._ev.clear()
            self._thread = threading.Thread(target=self._hold_windows, name="sigma-acordado", daemon=True)
            self._thread.start()
            return
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            cmd = ["caffeinate", "-i", "-w", str(os.getpid())]
        elif shutil.which("systemd-inhibit"):
            cmd = ["systemd-inhibit", "--what=sleep", "--who=Sigma", "--why=respondendo pedidos da rede", "--mode=block",
                   "sleep", "infinity"]
        else:
            return
        self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)

    def _hold_windows(self):
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        self._ev.wait()
        k32.SetThreadExecutionState(ES_CONTINUOUS)

    def _stop(self):
        if self._thread:
            self._ev.set()
            self._thread.join(timeout=2)
            self._thread = None
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
