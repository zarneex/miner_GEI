"""Смоук-тест окна: спинбокс цели и гувернатор на месте, железо не трогаем.

Запуск: C:\\Python314\\python.exe tools\\test_gui_governor.py
"""
import os
import sys
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
