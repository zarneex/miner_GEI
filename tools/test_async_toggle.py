#!/usr/bin/env python
"""GUI не замирает при переинициализации, а ввод копится в очередь.

Проверяем замером:
- таймер 50 мс тикает всё время «медленной» остановки (3 с) — окно живо;
- поля ввода не блокируются;
- число потоков меняется не на каждый тик, а после паузы, и применяется последним;
- колесо без фокуса значение не крутит.
Железо и настоящие майнеры не трогаем: stop/start и запись настроек подменены.
"""
import os
import sys
import time
from pathlib import Path

os.environ["MINER_NO_AUTOSTART"] = "1"

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import engine, ui  # noqa: E402

ok = True
gaps: list[float] = []
last = [time.perf_counter()]
cpu_started: list[int] = []


def out(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def check(name: str, good: bool) -> None:
    global ok
    out(f"{name}: {'PASS' if good else 'FAIL'}")
    ok = ok and good


def tick() -> None:
    now = time.perf_counter()
    gaps.append(now - last[0])
    last[0] = now


def main() -> int:
    app = QApplication(sys.argv)
    engine._kill_soft = lambda pid, grace=8.0, hard_first=False: time.sleep(3.0)

    w = ui.MainWindow()
    w.show()
    w._persist = lambda *a, **k: None      # тест не пишет боевые настройки
    w.krig.is_running = lambda: True
    w.krig._pid = 424242
    w.cpu.is_running = lambda: True
    w.cpu._pid = 424243
    w.cpu.start = lambda threads: cpu_started.append(threads)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(50)

    def phase_first() -> None:
        check("спинбокс не крутится колесом без фокуса",
              isinstance(w.spin_threads, ui.NoWheelSpin))
        check("ввод применяется по Enter/фокусу, а не на каждую цифру",
              not w.spin_threads.keyboardTracking())
        w.spin_threads.setValue(12)

    def phase_debounce() -> None:
        check("значение потоков не применилось мгновенно",
              not w._busy_flag and not cpu_started)

    def phase_busy() -> None:
        check("через паузу перезапуск начался", w._busy_flag)
        check("поля ввода остаются доступны во время перезапуска",
              w.spin_threads.isEnabled() and w.chk_cpu.isEnabled())
        w.spin_threads.setValue(10)
        w.spin_threads.setValue(8)        # последнее слово — 8 потоков

    def finish() -> None:
        worst = max(gaps[1:]) * 1000 if len(gaps) > 1 else 0.0
        out(f"худший разрыв между тиками GUI за все перезапуски: {worst:.0f} мс")
        check("окно не замерло", worst < 250)
        check("применены оба значения, последнее — 8", cpu_started == [12, 8])
        check("поля снова доступны, очередь пуста",
              w.spin_threads.isEnabled() and not w._busy_flag
              and w._want_threads is None)
        w.krig.is_running = lambda: False
        w.cpu.is_running = lambda: False
        # диалога подтверждения больше нет: крестик сам гасит майнер и выходит
        w.close()
        QTimer.singleShot(1200, app.quit)

    QTimer.singleShot(100, phase_first)
    QTimer.singleShot(400, phase_debounce)
    QTimer.singleShot(1000, phase_busy)
    QTimer.singleShot(9500, finish)
    app.exec()
    out(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
