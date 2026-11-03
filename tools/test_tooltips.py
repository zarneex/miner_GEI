#!/usr/bin/env python
"""Проверка подсказки графиков: своя всплывашка не исчезает сама и переживает
обновление панели, а убирается только когда курсор ушёл с графика."""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import charts, history  # noqa: E402

MARK = "tiptest"
N = 120
STEP = 60
HOLD_S = 8.0     # держим курсор «на месте» дольше, чем живёт системный QToolTip

ok = True


def out(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def check(name: str, good: bool) -> None:
    global ok
    out(f"{name}: {'PASS' if good else 'FAIL'}")
    ok = ok and good


def cleanup() -> int:
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
    for i in range(N):
        ts = now - (N - i) * STEP
        history.append(ts, MARK, 180.0, 67.0, 55.0, 97e12, 0.00002, 175 * STEP / 3_600_000)

    app = QApplication(sys.argv)
    panel = charts.ChartsPanel()
    panel.resize(1000, 700)
    panel.show()
    panel.cmb_range.setCurrentIndex(1)
    panel.refresh()
    QTest.qWait(200)

    point = panel._buckets[len(panel._buckets) // 2]["ts"] * 1000
    panel._on_hover(point)
    check("подсказка появилась", panel.tip.isVisible())
    out(f"текст: {panel.tip.text().replace(chr(10), ' | ')}")

    QTest.qWait(int(HOLD_S * 1000))
    check(f"подсказка жива через {HOLD_S:.0f} с", panel.tip.isVisible())

    panel.refresh()          # обновление панели (раз в минуту) не должно её гасить
    check("подсказка жива после refresh", panel.tip.isVisible())
    marked = {name: box.marker.count() for name, box in panel._boxes.items()}
    check("курсор восстановлен после refresh", all(v == 1 for v in marked.values()))

    panel._on_hover(None)
    check("курсор ушёл — подсказка скрыта", not panel.tip.isVisible())
    cleared = all(box.marker.count() == 0 for box in panel._boxes.values())
    check("маркеры убраны", cleared)

    removed = cleanup()
    out(f"тестовые строки удалены: {removed}")
    out(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
