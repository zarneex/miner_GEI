#!/usr/bin/env python
"""Синтетическая история для проверки отрисовки графиков: add | remove.

Пишет поминутные строки с coin="test" за N часов (по умолчанию 12) с правдоподобными
значениями: днём 250 Вт / 76 °C, «ночные» часы — 175 Вт / 66 °C, есть получасовой простой.
`remove` вычищает тестовые строки из файлов истории.
"""
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minerapp import history  # noqa: E402

MARK = "test"


def remove() -> int:
    removed = 0
    for path in history.HIST.glob("*.csv"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        keep = [lines[0]] if lines else []
        for ln in lines[1:]:
            if f",{MARK}," in ln:
                removed += 1
            else:
                keep.append(ln)
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    print(f"удалено строк: {removed}")
    return removed


def add(hours: int) -> int:
    now = time.time()
    n = hours * 60
    for i in range(n):
        ts = now - (n - i) * 60
        hour = time.localtime(ts).tm_hour
        night = hour >= 23 or hour < 8
        idle = (i // 30) % 7 == 3          # каждые 3.5 часа — получасовой простой
        power = 0.0 if idle else (175.0 if night else 250.0)
        temp = None if idle else (66.0 if night else 76.0)
        fan = None if idle else (55.0 if night else 79.0)
        hr = 0.0 if idle else (96e12 if night else 108e12)
        usd = 0.0 if idle else (1.9e-5 if night else 2.4e-5)
        kwh = power * 60 / 3_600_000
        history.append(ts, MARK, power, temp, fan, hr, usd, kwh)
    print(f"добавлено строк: {n} ({hours} ч)")
    return n


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "add"
    if cmd == "add":
        add(int(sys.argv[2]) if len(sys.argv) > 2 else 12)
    else:
        remove()
