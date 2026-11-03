#!/usr/bin/env python
"""Самопроверка GUI без ручных кликов: гоняет реальные переключатели и печатает факты.

Сценарий: ночной режим -> старт GPU (должны быть обороты 55% и <60%), затем стоп,
затем CPU на 4 потоках и его хешрейт, затем выход.
"""
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import config as cfgmod  # noqa: E402
from minerapp import gpu  # noqa: E402
from minerapp.ui import MainWindow  # noqa: E402


def out(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def main() -> None:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()

    # до срабатывания автозапуска раздела включаем ночной режим
    w.chk_night.setChecked(True)
    w.spin_threads.setValue(4)

    def at_finish_gpu() -> None:
        d = gpu.read()
        out(f"GPU label: {w.lbl_gpu.text()}")
        out(f"nvidia-smi: {d}")
        pl = d.get("plimit")
        fan = d.get("fan")
        temp = d.get("temp")
        ok = (pl == 175) and (fan is not None and fan < 60) and (temp is not None and temp < 78)
        out(f"НОЧНОЙ РЕЖИМ ПРОВЕРКА: plimit={pl} fan={fan} temp={temp} -> "
            f"{'PASS' if ok else 'FAIL'}")
        out(f"метрики: {w.card_usd.value.text()} {w.card_kwh.value.text()} "
            f"{w.card_avg.value.text()}")
        w.chk_gpu.setChecked(False)  # мягкий стоп

    def start_cpu() -> None:
        w.chk_cpu.setChecked(True)
        out("CPU включён на 4 потока")

    def finish_cpu() -> None:
        hr = w.cpu.hashrate
        out(f"CPU label: {w.lbl_cpu.text()} (hashrate={hr})")
        out(f"CPU ПРОВЕРКА: {'PASS' if hr else 'FAIL'} h/s")
        w.chk_cpu.setChecked(False)
        w.chk_night.setChecked(False)   # вернуть дневные настройки в конфиг
        w.spin_threads.setValue(8)
        QTimer.singleShot(2500, finish)

    def finish() -> None:
        out("консоль приложения:")
        for line in w.console.toPlainText().splitlines()[-18:]:
            out("  | " + line)
        w.close()
        app.quit()

    QTimer.singleShot(150_000, at_finish_gpu)   # прогрев + стабилизация кулера
    QTimer.singleShot(185_000, start_cpu)
    QTimer.singleShot(300_000, finish_cpu)
    app.exec()


if __name__ == "__main__":
    main()
