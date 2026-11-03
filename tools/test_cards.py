#!/usr/bin/env python
"""Проверка карточек метрик: рубли в скобках, скорость, счётчики за сутки/неделю/месяц,
подсказки на всей карточке и их отсутствие «дёрганья» на каждом такте датчиков."""
import os
import sys
import time
from pathlib import Path

os.environ["MINER_NO_AUTOSTART"] = "1"   # тест не должен поднимать боевые майнеры

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import ui  # noqa: E402
from minerapp.ui import MainWindow  # noqa: E402

ok = True


def out(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def check(name: str, good: bool) -> None:
    global ok
    out(f"{name}: {'PASS' if good else 'FAIL'}")
    ok = ok and good


def main() -> int:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    w.usd_rub = 80.56

    # имитируем 40 тактов реальной работы GPU и 20 тактов простоя
    now = time.time()
    for i in range(40):
        w.rate_gpu.feed(now - 40 + i, 108e12 * (1 + 0.02 * ((i % 3) - 1)), True)
    for i in range(20):
        w.rate_gpu.feed(now - 5 + i, 12e12, False)

    w._seed_periods()
    w._update_metrics("pearl")
    texts = {
        "сеанс": w.card_usd.value.text(),
        "свет": w.card_rub.value.text(),
        "сейчас": w.card_rate.value.text(),
        "час": w.card_hour.value.text(),
        "сутки": w.card_day.value.text(),
        "неделя": w.card_week.value.text(),
        "месяц": w.card_month.value.text(),
        "всего": w.card_total.value.text(),
    }
    for k, v in texts.items():
        out(f"{k}: {v}")
    subs = {
        "час": w.card_hour.sub.text(),
        "сутки": w.card_day.sub.text(),
        "неделя": w.card_week.sub.text(),
        "месяц": w.card_month.sub.text(),
    }
    for k, v in subs.items():
        out(f"прогноз {k}: {v!r}")
    for name, tip in (("свет", w.card_rub.toolTip()),
                      ("сейчас", w.card_rate.toolTip()),
                      ("сутки", w.card_day.toolTip()),
                      ("всего", w.card_total.toolTip())):
        out(f"подсказка {name}: " + tip.replace(chr(10), " | "))

    check("₽ в скобках у дохода за сеанс", "₽)" in texts["сеанс"])
    check("свет в рублях", texts["свет"].endswith("₽"))
    check("скорость сразу известна", "₽/ч" in texts["сейчас"])
    check("погрешность показана", "±" in texts["сейчас"])
    check("GPU в подсказке скорости", "GPU" in w.card_rate.toolTip())
    check("CPU в подсказке скорости", "CPU" in w.card_rate.toolTip())
    check("часы работы в подсказке итога", "ч работы" in w.card_total.toolTip())
    check("разбивка по устройству", "GPU (" in w.card_total.toolTip())
    check("подсказка на карточке", bool(w.card_rate.toolTip()))
    check("строки карточки не перехватывают мышь",
          not w.card_rate.cap.toolTip() and not w.card_rate.value.toolTip()
          and w.card_rate.cap.testAttribute(
              __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.WidgetAttribute.WA_TransparentForMouseEvents))
    check("подсказка у суток объясняет прогноз",
          "прогноз: текущий темп" in w.card_day.toolTip())
    # крупное число — прогноз от текущего темпа, мелкое — факт за период
    gpu_h, cpu_h, _rel, _win = w._now_rates("pearl")
    expect_hour = ui.fmt_rub(gpu_h + cpu_h)
    expect_day = ui.fmt_rub((gpu_h + cpu_h) * 24)
    out(f"темп сейчас {expect_hour}/ч → сутки {expect_day}")
    check("час = текущий темп", texts["час"] == expect_hour)
    check("сутки = 24 × текущий темп", texts["сутки"] == expect_day)
    check("мелкая строка показывает факт", subs["сутки"].startswith("факт"))
    # прогноз не должен тянуться за отстающей историей: обнуляем факт — число то же
    if "сутки" in w._sums:
        w._sums["сутки"]["usd"] = 0.0
    w._update_periods("pearl")
    check("прогноз не зависит от истории", w.card_day.value.text() == expect_day)

    # суммы по периодам считаются одним проходом; проверяем вложенность периодов
    sums = w._window_sums()
    out("суммы: " + ", ".join(f"{k}={v['usd']:.6f}" for k, v in sums.items()))
    check("периоды вложены (час <= сутки <= неделя <= месяц)",
          sums["час"]["usd"] <= sums["сутки"]["usd"] <= sums["неделя"]["usd"]
          <= sums["месяц"]["usd"])

    # GPU и CPU раздельно в прогнозе
    day_tip = w.card_day.toolTip()
    check("в прогнозе есть GPU и CPU отдельно", "GPU " in day_tip and "CPU " in day_tip)

    # «дёрганье» подсказок: повторный вызов не должен переставлять tooltip
    calls = []
    orig = w.card_rate.setToolTip

    def spy(text):
        calls.append(text)
        return orig(text)

    w.card_rate.setToolTip = spy
    for _ in range(5):
        w._update_metrics("pearl")
    check("подсказка не переставляется без изменений", len(calls) == 0)

    QTimer.singleShot(300, w.close)
    QTimer.singleShot(600, app.quit)
    app.exec()
    out(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
