#!/usr/bin/env python
"""Кулер: значение из приложения всегда в приоритете + тихий режим в АФК.

Железо не трогаем: NVML подменяется состоянием в словаре, проверяется логика
контроллера `fan.FanKeeper` (он каждый такт читает карту, а не помнит своё).
"""
import os
import sys
import time
from pathlib import Path

os.environ["MINER_NO_AUTOSTART"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import gpu, ui  # noqa: E402

ok = True


def out(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def check(name: str, good: bool) -> None:
    global ok
    out(f"{name}: {'PASS' if good else 'FAIL'}")
    ok = ok and good


def main() -> int:
    app = QApplication(sys.argv)
    w = ui.MainWindow()
    w.show()
    w._persist = lambda *a, **k: None      # тест не пишет боевые настройки
    w.fan.log_path = Path(os.environ.get("TEMP", ".")) / "test-fan.log"

    # --- подмена NVML: карта живёт в state ---------------------------
    state = {"policy": 0, "speed": 42, "calls": [], "fail": False}
    orig = (gpu.fan_policy, gpu.fan_speed, gpu.set_fan_speed, gpu.reset_fan_policy)

    gpu.fan_policy = lambda: state["policy"]
    gpu.fan_speed = lambda: state["speed"]

    def set_fan(pct):
        state["calls"].append(pct)
        if state["fail"]:
            return False, "NVML error 999"
        state["policy"] = 1
        state["speed"] = pct
        return True, f"обороты зафиксированы на {pct}%"

    def reset_fan():
        state["policy"] = 0
        state["speed"] = 42
        return True, "автообдув восстановлен"

    gpu.set_fan_speed = set_fan
    gpu.reset_fan_policy = reset_fan

    w.spin_fan.setValue(57.5)
    w.spin_temp.setValue(75)
    w._night_fan = None
    w.fan.release()

    # --- 1. автокулер произвольно гуляет — приложение фиксирует своё значение
    w._fan_guard({"gpu_running": True, "temp": 70, "fan": 42})
    check("взял кулер под контроль на значении 57.5%", state["calls"] == [58])
    check("карта зафиксирована на 58%", state["policy"] == 1 and state["speed"] == 58)

    # --- 2. кто-то сбросил политику (драйвер/майнер/прошлый запуск)
    state["policy"], state["speed"] = 0, 80
    w._fan_guard({"gpu_running": True, "temp": 71, "fan": 80})
    check("внешний сброс политики замечен и исправлен",
          state["calls"] == [58, 58] and state["policy"] == 1)

    # --- 3. карта ушла по оборотам, политика при этом «наша»
    state["speed"] = 75
    w._fan_guard({"gpu_running": True, "temp": 72, "fan": 75})
    check("уход оборотов вверх исправлен", state["calls"] == [58, 58, 58])

    # --- 4. всё на месте — карту зря не дёргаем
    w._fan_guard({"gpu_running": True, "temp": 72, "fan": 58})
    check("на своём значении запись не повторяется", len(state["calls"]) == 3)

    # --- 5. перегрев: выходим выше потолка
    w._fan_guard({"gpu_running": True, "temp": 80, "fan": 58})
    check("перегрев 80 °C при пороге 75 — 100%", state["calls"][-1] == 100 and w.fan.emergency)
    w._fan_guard({"gpu_running": True, "temp": 74, "fan": 100})
    check("гистерезис держит аварию до 73 °C", state["calls"][-1] == 100)
    w._fan_guard({"gpu_running": True, "temp": 72, "fan": 100})
    check("остыло — вернул потолок 57.5%", state["calls"][-1] == 58 and not w.fan.emergency)

    # --- 6. ночной пресет тише потолка: берём минимум
    w._night_fan = 55.0
    w._fan_guard({"gpu_running": True, "temp": 70, "fan": 58})
    check("ночью 55% (тише потолка 57.5%)", state["calls"][-1] == 55)
    w._night_fan = 70.0
    w._fan_guard({"gpu_running": True, "temp": 70, "fan": 55})
    check("пресет громче потолка — потолок главнее", state["calls"][-1] == 58)
    w._night_fan = None

    # --- 7. потолок 0 — не вмешиваемся
    w.spin_fan.setValue(0)
    w._fan_guard({"gpu_running": True, "temp": 70, "fan": 70})
    check("потолок 0: карта отпущена", state["policy"] == 0 and not w.fan.active)

    # --- 8. GPU остановлен — автообдув
    w.spin_fan.setValue(57.5)
    w._fan_guard({"gpu_running": False, "temp": None, "fan": None})
    check("GPU остановлен — фиксация снята", state["policy"] == 0 and not w.fan.active)

    # --- 9. сторож возвращает карту без кадров датчиков
    w._last_frame = {"gpu_running": True, "temp": 74, "fan": 80}
    w.krig.is_running = lambda: True
    state["policy"], state["speed"] = 0, 80
    w._fan_watchdog()
    check("сторож вернул карту к 57.5%", state["policy"] == 1 and state["speed"] == 58)
    w.krig.is_running = lambda: False
    w._fan_watchdog()
    check("сторож видит остановку майнера", state["policy"] == 0)

    # --- 10. ошибка NVML не роняет контроллер, а попадает в консоль
    w.krig.is_running = lambda: True
    state["fail"] = True
    w.fan._last_err_log = 0.0
    w._last_frame = {"gpu_running": True, "temp": 70, "fan": 70}
    w._fan_guard({"gpu_running": True, "temp": 70, "fan": 70})
    state["fail"] = False
    check("сбой записи не убил контроллер", "ограничитель кулера" in w.console.toPlainText()
          or w.fan.log_path.read_text(encoding="utf-8").count("ОШИБКА") >= 1)
    check("после сбоя следующая попытка снова фиксирует",
          (w._fan_guard({"gpu_running": True, "temp": 70, "fan": 70}),
           state["policy"] == 1)[1])

    # --- тихий режим в АФК -------------------------------------------
    ui.idle_seconds = lambda: 120.0
    w._check_idle()
    check("АФК включает тихий режим", w._idle)
    check("датчики опрашиваются реже", w.sensors.interval == ui.IDLE_INTERVAL)
    ui.idle_seconds = lambda: 0.0
    w._check_idle()
    check("возврат к обычному опросу", not w._idle and w.sensors.interval == ui.ACTIVE_INTERVAL)

    (gpu.fan_policy, gpu.fan_speed, gpu.set_fan_speed, gpu.reset_fan_policy) = orig
    w.fan.log_path = Path(os.environ.get("TEMP", ".")) / "test-fan.log"
    w.krig.is_running = lambda: False
    # диалога «остановить и выйти?» больше нет — крестик гасит майнер сам

    # --- спинбокс лимита мощности -------------------------------------
    applied = []
    w.krig.is_running = lambda: True
    w._apply_pl = lambda watts, why: (applied.append(watts) or setattr(w, "target_pl", watts))
    w.spin_pl.setValue(175 if w.spin_pl.value() != 175 else 190)
    check("спинбокс не дёргает nvidia-smi на каждый шаг", not applied)
    w._pl_timer.timeout.emit()
    check("лимит мощности применяется из спинбокса", bool(applied) and applied[-1] == w.target_pl)

    QTimer.singleShot(300, w.close)
    QTimer.singleShot(600, app.quit)
    app.exec()
    out(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
