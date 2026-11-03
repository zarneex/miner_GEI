#!/usr/bin/env python
"""Проверка трея и закрытия: галочки в шапке, кнопка «Остановить и закрыть»,
отсутствие подвисания при выходе.

Окно строится offscreen; железо, сеть, службы и LHM подменены заглушками,
майнеры — фальшивые: тест не трогает боевой майнинг и файлы состояния.
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


def patch_hardware() -> None:
    """Заглушки до создания окна: ничего не пишем в железо и не звоним в сеть."""
    ui.gpu.fan_policy = lambda: None
    ui.gpu.reset_fan_policy = lambda: (True, "тест")
    ui.gpu.reset_clocks = lambda: (True, "тест")
    ui.gpu.set_power_limit = lambda _w: (True, "тест")
    ui.fanmod.FanKeeper.apply = lambda self, **kw: None
    ui.fanmod.FanKeeper.release = lambda self, *a, **k: None
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
    ui.display.monitors_off = lambda: True
    ui.display.monitors_on = lambda: True


def pump(app: QApplication, cond=None, timeout: float = 8.0) -> bool:
    """Прокрутить очередь событий (фоновый стоп приходит queued-сигналом)."""
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond is not None and cond():
            return True
        time.sleep(0.02)
    return bool(cond() if cond is not None else True)


def fake_miner(w: MainWindow, pid: int) -> None:
    """Майнер «жив»: stop уходит в фоновую заглушку, состояние не пишем."""
    w.krig.is_running = lambda: True
    w.krig.pid = lambda: pid
    w.krig.on_stop_bg = lambda: None
    w.cpu.is_running = lambda: False


def main() -> int:
    patch_hardware()
    app = QApplication(sys.argv)
    w = MainWindow()
    w._persist = lambda *a, **k: None
    w._flush_minute = lambda: None
    stops: list[tuple] = []
    ui.engine.soft_stop = lambda pid, grace=0.0, hard=True: stops.append((pid, grace, hard))
    ui.cfgmod.log_event = lambda *_a, **_k: None
    for chk in (w.chk_close_extra, w.chk_monitors):
        chk.blockSignals(True)
        chk.setChecked(False)
        chk.blockSignals(False)

    # --- шапка: две галочки и кнопка справа ---------------------------------
    check("галочка «в трей при сворачивании»", hasattr(w, "chk_tray_min"))
    check("галочка «в трей при закрытии»", hasattr(w, "chk_tray_close"))
    check("по умолчанию обе выключены",
          not w.chk_tray_min.isChecked() and not w.chk_tray_close.isChecked())
    check("кнопка на месте подписи про лимиты",
          w.btn_stop_close.text() == "Остановить и закрыть")
    check("трей доступен в этой сессии", w._tray_ok is True)

    w.chk_tray_min.setChecked(True)
    check("настройка сворачивания сохранена", w._sys["tray_on_min"] is True)
    w.chk_tray_min.setChecked(False)
    w.chk_tray_close.setChecked(True)
    check("настройка закрытия сохранена", w._sys["tray_on_close"] is True)

    # --- значок и меню трея -------------------------------------------------
    menu_texts = [a.text() for a in w.tray.contextMenu().actions() if a.text()]
    check("значок трея создан", isinstance(w.tray, ui.QSystemTrayIcon))
    check("меню трея: показать и остановить",
          menu_texts == ["Показать окно", "Остановить и закрыть"])
    check("подсказка трея задана", "майнинг" in w.tray.toolTip().lower())

    # --- крестик при галочке: в трей, майнинг жив ---------------------------
    fake_miner(w, 4321)
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    w.resize(1160, 880)
    w.show()
    app.processEvents()
    notes = PROJECT / "notes"
    notes.mkdir(exist_ok=True)
    shot = notes / "tray-close.png"
    w.grab().save(str(shot))
    w.grab(w.rect().adjusted(0, 0, 0, -w.height() + 74)).save(
        str(notes / "tray-close-header.png"))
    check("снимок окна сохранён", shot.exists() and shot.stat().st_size > 10000)

    w.close()
    pump(app, lambda: not w.isVisible(), 1.0)
    check("крестик прячет окно в трей", not w.isVisible())
    check("майнинг при скрытии не останавливается", not w._shutdown_started and not stops)

    w._restore_from_tray()
    pump(app, lambda: w.isVisible(), 1.0)
    check("из трея окно возвращается", w.isVisible())

    # --- сворачивание при галочке: тоже в трей ------------------------------
    w.chk_tray_close.setChecked(False)
    w.chk_tray_min.setChecked(True)
    w.showMinimized()
    pump(app, lambda: not w.isVisible(), 1.5)
    check("сворачивание уводит в трей", not w.isVisible())
    check("сворачивание майнинг не трогает", not w._shutdown_started)
    w._restore_from_tray()
    pump(app, lambda: w.isVisible(), 1.0)
    w.chk_tray_min.setChecked(False)

    # --- кнопка: стоп с добиванием, окно не замирает ------------------------
    t0 = time.time()
    w.on_stop_and_close()
    dt = time.time() - t0
    check("кнопка отвечает мгновенно", dt < 0.2)
    pump(app, lambda: w._shutdown_done, 8.0)
    check("кнопка гасит майнер", bool(stops) and stops[0][0] == 4321)
    check("стоп мягкий с добиванием /F", stops[0][2] is True)
    check("у кнопки короткая пауза перед /F", stops[0][1] == 1.0)
    check("выход дошёл до конца", w._shutdown_done and w._shutdown_started)

    # --- крестик без галочки: тот же стоп, но окно не висит -----------------
    w2 = MainWindow()
    w2._persist = lambda *a, **k: None
    w2._flush_minute = lambda: None
    fake_miner(w2, 777)
    for chk in (w2.chk_close_extra, w2.chk_monitors,
                w2.chk_tray_min, w2.chk_tray_close):
        chk.blockSignals(True)
        chk.setChecked(False)
        chk.blockSignals(False)
    stops.clear()
    # уведомление о долгом стопе (отдельно: стоп-заглушка мгновенная)
    notes_seen: list[str] = []
    w2._tray_note = lambda text, msec=0: notes_seen.append(text)
    w2._shutdown_started, w2._shutdown_done = True, False
    w2._shutdown_notice()
    check("уведомление «закроюсь, как только встанет»",
          bool(notes_seen) and "остановится майнинг" in notes_seen[0])
    check("строка статуса в шапке",
          "остановится майнинг" in w2.lbl_shutdown.text())
    w2._shutdown_started, w2._shutdown_done = False, False
    t0 = time.time()
    w2.close()
    dt = time.time() - t0
    check("крестик не блокирует интерфейс", dt < 0.2)
    pump(app, lambda: w2._shutdown_done, 8.0)
    check("крестик тоже гасит майнер", bool(stops) and stops[0][0] == 777)
    check("крестик ждёт мягкий стоп дольше кнопки", stops[0][1] == 2.5)
    check("выход завершён без зависания", w2._shutdown_done)

    print(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
