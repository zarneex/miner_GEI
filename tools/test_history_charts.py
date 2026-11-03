#!/usr/bin/env python
"""Проверка слоя истории и графиков на синтетических данных.

Пишет строки с coin="test" за последние 6 часов, проверяет загрузку, склейку,
сводку и отрисовку панели графиков; в конце удаляет тестовые строки из файла месяца.
"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import charts, history  # noqa: E402

MARK = "test"
N = 360          # 6 часов по минуте
STEP = 60


def out(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def cleanup() -> int:
    """Убрать тестовые строки из файла текущего месяца."""
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
    return removed


def main() -> int:
    cleanup()
    now = time.time()
    total_usd = 0.0
    total_kwh = 0.0
    for i in range(N):
        ts = now - (N - i) * STEP
        # целыми корзинами по 5 минут выключаем доход: на графике видны простои
        usd = 0.0 if (i // 5) % 12 == 0 else 0.00002
        kwh = 175 * STEP / 3_600_000
        total_usd += usd
        total_kwh += kwh
        history.append(ts, MARK, 175.0, 66.0 + (i % 5), 55.0, 96e12, usd, kwh)
    out(f"записано {N} строк, ожидаемо usd={total_usd:.6f} kwh={total_kwh:.4f}")

    rows = history.load(now - 7 * 3600, now + 60)
    rows = [r for r in rows if r["coin"] == MARK]
    out(f"load: {len(rows)} строк -> {'PASS' if len(rows) == N else 'FAIL'}")

    s = history.summary(rows)
    ok_sum = abs(s["usd"] - total_usd) < 1e-6 and abs(s["kwh"] - total_kwh) < 1e-3
    out(f"summary: usd={s['usd']:.6f} kwh={s['kwh']:.4f} часы={s['hours']:.2f} "
        f"-> {'PASS' if ok_sum else 'FAIL'}")

    buckets = history.aggregate(rows, 300)
    ok_agg = len(buckets) in (N * STEP // 300, N * STEP // 300 + 1)
    out(f"aggregate(5 мин): {len(buckets)} корзин -> {'PASS' if ok_agg else 'FAIL'}")
    ok_zero = any(b["usd"] == 0 for b in buckets) and any(b["usd"] > 0 for b in buckets)
    out(f"простои видны как нули -> {'PASS' if ok_zero else 'FAIL'}")

    app = QApplication(sys.argv)
    panel = charts.ChartsPanel()
    panel.set_rate(80.5)
    panel.cmb_range.setCurrentIndex(1)   # 6 ч
    panel.cmb_step.setCurrentIndex(0)
    panel.refresh()
    points = len(panel.c_temp._points)
    text = panel.lbl_summary.text()
    out(f"график температуры: точек {points}; сводка: {text}")
    ok_view = points >= 300 and "доход" in text
    out(f"панель графиков -> {'PASS' if ok_view else 'FAIL'}")

    # синхронный курсор: одна временная точка подсвечивается на всех четырёх графиках
    if panel._buckets:
        mid = panel._buckets[len(panel._buckets) // 2]["ts"] * 1000
        panel._on_hover(mid)
        marked = {name: box.marker.count() for name, box in panel._boxes.items()}
        cursor_on = all(box.cursor.count() >= 2 for box in panel._boxes.values())
        out(f"курсор: маркеры {marked}, линии {cursor_on}")
        ok_sync = cursor_on and all(v == 1 for v in marked.values())
        out(f"синхронный курсор на всех графиках -> {'PASS' if ok_sync else 'FAIL'}")
        panel._on_hover(None)
        cleared = all(box.marker.count() == 0 and box.cursor.count() == 0
                      for box in panel._boxes.values())
        out(f"курсор убирается -> {'PASS' if cleared else 'FAIL'}")

    removed = cleanup()
    out(f"тестовые строки удалены: {removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
