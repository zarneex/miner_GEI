#!/usr/bin/env python
"""Проверка регулятора лимита GPU: снижение, возврат, мёртвая зона, стоп.

Проверяем чистую логику, без nvidia-smi: времена подаются явно.
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minerapp.gpu_guard import PowerGuard  # noqa: E402

ok = True


def check(name: str, good: bool) -> None:
    global ok
    print(f"{name}: {'PASS' if good else 'FAIL'}", flush=True)
    ok = ok and good


def main() -> int:
    g = PowerGuard(175, 250)
    g.reset(240)
    check("старт: применено ровно значение спинбокса", g.applied == 240)

    # пауза 30 с и два такта выше порога — только тогда режем
    check("один такт выше порога не режет", g.observe(82, 80, 0.0) is None)
    check("пауза 30 с соблюдается", g.observe(82, 80, 10.0) is None)
    check("после паузы — шаг 10 Вт", g.observe(82, 80, 31.0) == 230)
    check("после среза снова копим два такта", g.observe(82, 80, 61.0) is None)
    check("второй шаг", g.observe(82, 80, 63.0) == 220)
    g.observe(82, 80, 93.0)
    check("третий шаг", g.observe(82, 80, 95.0) == 210)
    for t in range(125, 2000, 5):
        g.observe(82, 80, float(t))
    check("ниже минимума не опускается", g.applied == 175)

    # мёртвая зона: между порогом − 3 и порогом ничего не меняем
    g2 = PowerGuard(175, 250)
    g2.reset(240)
    g2.applied = 220
    g2.last_change = 0.0
    changes = [g2.observe(78.0, 80, t) for t in range(10, 700, 10)]
    check("в мёртвой зоне лимит не дёргается", all(c is None for c in changes))

    # возврат к цели по 5 Вт, не быстрее раза в 30 с
    g3 = PowerGuard(175, 250)
    g3.reset(240)
    g3.applied = 220
    g3.last_change = 0.0
    check("возврат не раньше паузы", g3.observe(76, 80, 10) is None)
    check("холодно — возврат 5 Вт", g3.observe(76, 80, 31) == 225)
    check("возврат не чаще 30 с", g3.observe(76, 80, 45) is None)
    check("следующий возврат", g3.observe(76, 80, 61) == 230)
    for t in range(91, 3000, 30):
        g3.observe(76, 80, t)
    check("возвращается ровно к цели", g3.applied == 240)
    check("выше цели не уходит", g3.observe(70, 80, 3000) is None)

    # стоп: только на минимуме, выше порога + 6 и дольше минуты
    g4 = PowerGuard(175, 250)
    g4.reset(200)
    g4.applied = 175
    check("на минимуме не стопим сразу", g4.need_stop(86, 80, 0) is False)
    check("стоп после 60 с перегрева", g4.need_stop(86, 80, 61) is True)
    check("ниже порога + 6 — не стопим", g4.need_stop(84, 80, 120) is False)

    # ручная смена цели применяется сразу, регулятор потом снова снижает
    g5 = PowerGuard(175, 250)
    g5.reset(240)
    g5.applied = 200
    check("ручная цель применяется сразу", g5.set_target(250, 100) == 250)

    print(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
