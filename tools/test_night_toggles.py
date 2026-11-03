#!/usr/bin/env python
"""Проверка ночных галочек, метрики VRAM и простоя — без живого железа.

Окно строится offscreen; карта, мониторы, службы, сеть и LHM подменены
заглушками: тест не трогает боевой майнинг и не меняет настройки на диске.
"""
import os
import sys
import time
from pathlib import Path

os.environ["MINER_NO_AUTOSTART"] = "1"

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import ui  # noqa: E402
from minerapp.ui import MainWindow  # noqa: E402

ok = True


def check(name: str, good: bool) -> None:
    global ok
    print(f"{name}: {'PASS' if good else 'FAIL'}", flush=True)
    ok = ok and good


def patch_hardware() -> tuple[list, list]:
    """Заглушки до создания окна: ничего не пишем в железо и не звоним в сеть."""
    ui.gpu.fan_policy = lambda: None
    ui.gpu.reset_fan_policy = lambda: (True, "тест")
    ui.gpu.set_power_limit = lambda _w: (True, "тест")
    ui.fanmod.FanKeeper.apply = lambda self, **kw: None
    ui.govmod.ThermalGovernor.__init__ = lambda self, *a, **k: None
    ui.govmod.ThermalGovernor.tick = lambda self, *a, **k: None
    ui.govmod.ThermalGovernor.stop = lambda self: None
    ui.govmod.ThermalGovernor.restore_backup = lambda self: None
    ui.govmod.ThermalGovernor.set_allow_100 = lambda self, v: None
    ui.lhmmod.LhmBridge.__init__ = lambda self, *a, **k: None
    ui.lhmmod.LhmBridge.tick = lambda self, *a, **k: None
    ui.lhmmod.LhmBridge.stop = lambda self, *a, **k: None
    ui.balmod.BalanceWatcher.start = lambda self, *a, **k: None
    ui.automod.AutoWatcher.start = lambda self, *a, **k: None
    ui.rates.usd_rub = lambda: 80.0
    ui.rates.electricity_rub_kwh = lambda: 2.1
    offs: list[float] = []
    ons: list[float] = []
    ui.display.monitors_off = lambda: (offs.append(time.time()) or True)
    ui.display.monitors_on = lambda: (ons.append(time.time()) or True)
    return offs, ons


def main() -> int:
    offs, ons = patch_hardware()
    idle = [0.0]
    ui.idle_seconds = lambda: idle[0]

    app = QApplication(sys.argv)
    w = MainWindow()
    w._persist = lambda *a, **k: None

    check("галочка уборки есть", hasattr(w, "chk_close_extra"))
    check("галочка мониторов есть", hasattr(w, "chk_monitors"))
    check("галочка уборки заблокирована", not w.chk_close_extra.isEnabled())
    check("галочка мониторов активна", w.chk_monitors.isEnabled())
    check("ночной режим заблокирован", not w.chk_night.isEnabled())
    check("уборка всегда снята", w.chk_close_extra.isChecked() is False)
    check("ночной режим всегда снят", w.chk_night.isChecked() is False)

    # --- видеопамять вместо «N Вт лимит» ---
    d = {"gpu_running": True, "temp": 66, "fan": 52, "power": 175, "plimit": 240,
         "sm_clock": 2775, "mem_clock": 17001, "gpu_hashrate": 1.09e14,
         "util": 98, "vram_mb": 9268, "vram_total_mb": 12227}
    w._update_labels(d, "pearl")
    text = w.lbl_gpu.text()
    check("VRAM в строке GPU", "VRAM 9.1/11.9 ГБ" in text)
    check("«лимит» из строки убран", "лимит" not in text)
    check("лимит остался в подсказке", "240 Вт" in w.lbl_gpu.toolTip())

    # --- мониторы: авто-гашение по простою снова работает ---
    w.chk_monitors.blockSignals(True)
    w.chk_monitors.setChecked(True)
    w.chk_monitors.blockSignals(False)
    idle[0] = 400.0
    w._check_idle()
    check("мониторы гаснут на простое", len(offs) == 1)
    w._check_idle()
    check("повторно не гасим", len(offs) == 1)
    idle[0] = 0.0
    w._check_idle()
    check("ввод снимает признак темноты", w._monitors_dark is False)
    w._monitors_dark = True
    w.chk_monitors.setChecked(False)
    check("снятие галочки включает мониторы", len(ons) == 1)
    check("настройка мониторов сохранена в объекте", w._sys["monitors_off"] is False)

    # --- кнопка «Погасить сейчас» ---
    w.btn_monitors_now.click()
    check("кнопка «Погасить сейчас» гасит мониторы", len(offs) == 2)

    # --- уборка фона: таймер её не запускает ---
    enabled: list[bool] = []
    w.cleaner.set_enabled = lambda on: enabled.append(bool(on))
    w._cleanup_tick()
    app.processEvents()
    check("таймер уборки ничего не запускает", enabled == [])
    check("настройка уборки снята в объекте", w._sys["close_extra"] is False)

    # --- регулятор подключён к спинбоксу ---
    check("регулятор — цель спинбокса",
          w.power_guard.target == int(w.spin_pl.value()))

    # --- снимок окна для визуальной проверки (без показа на экране) ---
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    w.resize(1160, 880)
    w.show()
    app.processEvents()
    notes = PROJECT / "notes"
    notes.mkdir(exist_ok=True)
    shot = notes / "system-toggles.png"
    w.grab().save(str(shot))
    check("снимок окна сохранён", shot.exists() and shot.stat().st_size > 10000)

    w._flush_minute = lambda: None        # тест не пишет строку в боевую историю
    w.close()
    print(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
