"""Пути и константы. По умолчанию всё живёт в W:\\mining\\config.json.

Корень данных можно переопределить переменной окружения `MINER_ROOT`
(например, `MINER_ROOT=D:\\mining`) — приложению не обязательно стоять на W:.
"""
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("MINER_ROOT") or r"W:\mining")
SCRIPTS = ROOT / "scripts"
CFG = ROOT / "config.json"
STATE = ROOT / "state.json"
LOGS = ROOT / "logs"
DATA = ROOT / "data"
SAMPLES = DATA / "samples.csv"
CPU_JSON = DATA / "cpu.json"      # срез моста lhmd.ps1: температура/мощность/частота CPU

KRIG = ROOT / "krig" / "krig-miner.exe"
XMRIG = ROOT / "xmrig" / "xmrig-6.26.0" / "xmrig.exe"
NVSMI = Path(r"C:\Windows\System32\nvidia-smi.exe")

PROJECT = Path(__file__).resolve().parent.parent
APP_NAME = "miner"
APP_TITLE = "Майнинг — панель"

# Лимит мощности карты (замерено: 175..250 Вт); шаг 10 Вт ≈ 1.3 °C в этом диапазоне
PL_MIN = 175
PL_MAX = 250
PL_STEP = 10

# логические процессоры этой машины: от них считается маска старших ядер и
# верхняя граница «Потоков» (на 9800X3D/7800X3D — 16)
CPU_TOTAL_THREADS = os.cpu_count() or 16


def ensure_engine_path() -> None:
    """Движок майнинга: рабочая копия в `MINER_ROOT\\scripts` или встроенная.

    Оригинал CLI-движка живёт в `W:\\mining\\scripts`; для публичного
    репозитория рядом лежит его копия (`engine/`), чтобы приложение
    запускалось на чужой машине без этой папки.
    """
    bundled = Path(__file__).resolve().parent.parent / "engine"
    where = SCRIPTS if (SCRIPTS / "minerctl.py").exists() else bundled
    p = str(where)
    if p not in sys.path:
        sys.path.insert(0, p)


def ensure_std_streams() -> None:
    """У pythonw.exe нет stdout/stderr (оба None), а движок на импорте делает
    `sys.stdout.reconfigure()` — приложение падало молча ещё до окна: запуск
    двойным кликом и автозапуск из реестра не работали, жил только запуск с
    перенаправленным выводом. Подставляем пустой поток, чтобы импорт не падал."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8",
                                        errors="replace"))
            except OSError:
                pass


ensure_std_streams()
