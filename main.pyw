"""Запуск панели без окна консоли.

Двойной клик по этому файлу Windows открывает через pythonw.exe (ассоциация .pyw),
поэтому фонового терминала нет. Это тот же main.py, только удобнее для ярлыка.
"""
import os
import runpy
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
sys.path.insert(0, str(BASE))

runpy.run_path(str(BASE / "main.py"), run_name="__main__")
