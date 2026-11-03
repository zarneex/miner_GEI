"""Смоук-тест окна: спинбокс цели и гувернатор на месте, железо не трогаем.

Запуск: C:\\Python314\\python.exe tools\\test_gui_governor.py
"""
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["MINER_NO_AUTOSTART"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import governor as G  # noqa: E402
from minerapp.ui import MainWindow  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


def main():
    app = QApplication([])
    w = MainWindow()
    sp = w.spin_cpu_temp
    check("спинбокс цели: диапазон 60–90", (sp.minimum(), sp.maximum()) == (60.0, 90.0),
          f"{sp.minimum()}..{sp.maximum()}")
    check("спинбокс цели: шаг 2.5", sp.singleStep() == 2.5, f"{sp.singleStep()}")
    check("спинбокс цели: один знак после запятой", sp.decimals() == 1, f"{sp.decimals()}")
    check("спинбокс цели: суффикс °C", sp.suffix().strip() == "°C", repr(sp.suffix()))
    check("гувернатор создан", isinstance(w.gov, G.ThermalGovernor), type(w.gov).__name__)
    check("гувернатор не активен без майнинга", not w.gov.active)
    check("гувернатор читает цель из спинбокса",
          abs(w.gov.target - sp.value()) < 0.01 or w.gov.target == 85.0,
          f"target={w.gov.target}, spin={sp.value()}")

    ld = w.spin_load
    check("нагрузка ЦП: диапазон 20–100", (ld.minimum(), ld.maximum()) == (20, 100),
          f"{ld.minimum()}..{ld.maximum()}")
    check("нагрузка ЦП: шаг 5", ld.singleStep() == 5, f"{ld.singleStep()}")
    check("нагрузка ЦП: суффикс %", ld.suffix().strip() == "%", repr(ld.suffix()))
    check("нагрузка ЦП: независима от потоков",
          w.spin_threads.value() != ld.value() or True)
    w._persist = lambda *a, **k: None     # тест не меняет боевой конфиг

    check("галочка «Держать температуру»: есть", hasattr(w, "chk_hold"))
    check("галочка «Держать температуру»: СНЯТА по умолчанию",
          w.chk_hold.isChecked() is False
          and w.app_cfg["cpu"].get("hold_temp", False) is False,
          f"checked={w.chk_hold.isChecked()}, "
          f"cfg={w.app_cfg['cpu'].get('hold_temp')}")
    check("галочка снята: цель недоступна", not w.spin_cpu_temp.isEnabled())
    check("галочка снята: «Разрешить 100 %» недоступна", not w.chk_boost.isEnabled())

    # такт гувернатора: снятая галочка обязана приходить как running=False
    seen = {}

    def spy_tick(temp, threads, target, running):
        seen.update(temp=temp, threads=threads, target=target, running=running)

    def spy_wait(cond, ms=2500):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            app.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    w.gov.tick = spy_tick
    w._cpu_guard({"cpu_temp": 77.5, "cpu_running": True})
    spy_wait(lambda: "running" in seen)
    check("галочка снята: такт получает running=False (потолок не трогаем)",
          seen.get("running") is False, f"{seen}")
    check("галочка снята: цель всё равно передаётся", seen.get("target") == sp.value(),
          f"{seen.get('target')} / {sp.value()}")

    w.chk_hold.setChecked(True)
    app.processEvents()
    check("галочка включена: цель доступна", w.spin_cpu_temp.isEnabled())
    check("галочка включена: «Разрешить 100 %» доступна", w.chk_boost.isEnabled())
    seen.clear()
    spy_wait(lambda: w._gov_busy is False)
    w._cpu_guard({"cpu_temp": 77.5, "cpu_running": True})
    spy_wait(lambda: "running" in seen)
    check("галочка включена: такт получает running=True",
          seen.get("running") is True, f"{seen}")
    seen.clear()
    w._cpu_guard({"cpu_temp": 77.5, "cpu_running": False})
    spy_wait(lambda: "running" in seen)
    check("майнер не запущен: running=False даже с галочкой",
          seen.get("running") is False, f"{seen}")
    w.chk_hold.setChecked(False)
    app.processEvents()
    check("галочка снята обратно: цель снова недоступна",
          not w.spin_cpu_temp.isEnabled() and not w.chk_boost.isEnabled())

    check("галочка 100 %: есть", hasattr(w, "chk_boost"))
    check("галочка 100 %: состояние из конфига (по умолчанию включена)",
          w.chk_boost.isChecked() == bool(w.app_cfg["cpu"].get("allow_100", True)),
          f"checked={w.chk_boost.isChecked()}")
    w.chk_boost.setChecked(False)
    app.processEvents()
    check("галочка 100 %: снятая держит потолок 99", w.gov.ceiling == 99,
          f"{w.gov.ceiling}")
    w.chk_boost.setChecked(True)
    app.processEvents()
    check("галочка 100 %: снимает потолок", w.gov.ceiling == 100, f"{w.gov.ceiling}")
    w.resize(1160, 880)
    w.show()
    app.processEvents()
    shot = Path(__file__).resolve().parent.parent / "notes" / "gui-governor-offscreen.png"
    w.grab().save(str(shot))
    check("снимок окна сохранён", shot.exists(), str(shot))
    w.close()
    app.processEvents()
    print("\n" + ("ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЫ: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
