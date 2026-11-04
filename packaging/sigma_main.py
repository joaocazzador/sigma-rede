"""Ponto de entrada do Sigma.exe (PyInstaller): o mesmo que `python -m sigma.app`.

freeze_support() vem antes de tudo: no .exe, cada processo do minerador é o próprio Sigma.exe chamado de
novo, e é aqui que ele desvia para o trabalho em vez de abrir outro app (por isso o import vem depois).
"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from sigma.app import main
    sys.exit(main())
