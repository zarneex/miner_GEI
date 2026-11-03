#!/usr/bin/env python
"""Проверка полей «Кошелёк» / «Куки», иконок и галочки «Разрешить 100 %».

Окно строится offscreen: железо, сеть, датчики и запись конфига подменены
заглушками — тест не трогает боевой майнинг и не меняет настройки на диске.
"""
import os
import sys
from pathlib import Path

os.environ["MINER_NO_AUTOSTART"] = "1"

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QToolButton  # noqa: E402

from minerapp import config as cfgmod  # noqa: E402
from minerapp import ui  # noqa: E402
from minerapp.ui import MainWindow  # noqa: E402

ok = True


def check(name: str, good: bool) -> None:
    global ok
    print(f"{name}: {'PASS' if good else 'FAIL'}", flush=True)
    ok = ok and good


def patch_hardware() -> None:
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


def main() -> int:
    patch_hardware()
    app = QApplication(sys.argv)

    # конфиг в памяти: боевой W:\mining\config.json тест не читает и не пишет
    fake: dict = {"kryptex_user": "krxTEST/rig1",
                  "xmrig": {"url": "pool:8029", "user": "krxTEST/rig1"},
                  "app": {"balance": {"headers": {}}}}
    orig_app = cfgmod.app_settings          # умолчания дописывает настоящая функция
    ui.cfgmod.load = lambda: fake
    ui.cfgmod.app_settings = orig_app
    ui.cfgmod.save = lambda cfg: fake.update(cfg)

    w = MainWindow()
    w._persist = lambda *a, **k: None
    w.balance_thread.refresh_now = lambda: None

    # --- поля и иконки ---
    check("поле кошелька есть", isinstance(w.ed_login, ui.QLineEdit))
    check("кошелёк подставлен из конфига", w.ed_login.text() == "krxTEST/rig1")
    for btn in (w.btn_login_ok, w.btn_login_cancel,
                w.btn_cookie_ok, w.btn_cookie_cancel):
        check(f"{btn.toolTip()[:18]} — QToolButton", isinstance(btn, QToolButton))
        check(f"{btn.toolTip()[:18]} — иконка", not btn.icon().isNull())
    check("иконка сохранения не пустая", not ui.icon("save").isNull())
    check("иконка отката не пустая", not ui.icon("revert").isNull())
    check("без изменений кнопки кошелька скрыты",
          w.btn_login_ok.isHidden() and w.btn_login_cancel.isHidden())
    check("без изменений кнопки куки скрыты",
          w.btn_cookie_ok.isHidden() and w.btn_cookie_cancel.isHidden())
    w.ed_login.setText("mylogin")
    check("изменение показало кнопки кошелька",
          not w.btn_login_ok.isHidden() and not w.btn_login_cancel.isHidden())
    w.ed_login.setText("krxTEST/rig1")
    check("возврат к сохранённому спрятал кнопки", w.btn_login_ok.isHidden())

    # --- сохранение кошелька: пишем и в kryptex_user, и в xmrig.user ---
    w.ed_login.setText("  mylogin  ")
    w.on_login_apply()
    check("кошелёк записан в kryptex_user", fake["kryptex_user"] == "mylogin/rig1")
    check("кошелёк записан в xmrig.user", fake["xmrig"]["user"] == "mylogin/rig1")
    check("владелец воркера по умолчанию rig1", fake["kryptex_user"].endswith("/rig1"))
    check("после сохранения кнопки скрыты", w.btn_login_ok.isHidden())

    # --- пустое поле не затирает сохранённый кошелёк ---
    w.ed_login.setText("   ")
    before = fake["kryptex_user"]
    w.on_login_apply()
    check("пустой логин не сохраняется", fake["kryptex_user"] == before)
    check("поле вернулось к сохранённому", w.ed_login.text() == before)

    # --- откат ---
    w.ed_login.setText("что-то-другое")
    w.on_login_revert()
    check("откат вернул значение из конфига", w.ed_login.text() == before)

    # --- куки: кнопка-иконка сохраняет, поле очищается ---
    w.ed_cookie.setText("abc123")
    check("ввод показал кнопки куки", not w.btn_cookie_ok.isHidden())
    w.on_cookie_apply()
    check("куки нормализованы", fake["app"]["balance"]["headers"]["Cookie"]
          == "sessionid=abc123")
    check("поле куки очищено", w.ed_cookie.text() == "")
    check("кнопки куки снова скрыты", w.btn_cookie_ok.isHidden())
    check("подпись «Куки» помечена зелёным",
          "7fc99a" in w.lbl_cookie.styleSheet())

    # --- 100 % включён по умолчанию ---
    check("в умолчаниях конфига allow_100=True",
          cfgmod.DEFAULTS_APP["cpu"]["allow_100"] is True)
    fake["app"].setdefault("cpu", {})["allow_100"] = True
    w._flush_minute = lambda: None       # тест не пишет строку в боевую историю
    w.close()

    w2 = MainWindow()
    w2._persist = lambda *a, **k: None
    w2.balance_thread.refresh_now = lambda: None
    check("галочка «Разрешить 100 %» стоит по умолчанию", w2.chk_boost.isChecked())

    # --- снимок строки настроек для визуальной проверки ---
    w2.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    w2.resize(1160, 880)
    w2.show()
    app.processEvents()
    notes = PROJECT / "notes"
    notes.mkdir(exist_ok=True)
    shot = notes / "credentials-row.png"
    w2.grab().save(str(shot))
    check("снимок строки сохранён", shot.exists() and shot.stat().st_size > 10000)

    w2._flush_minute = lambda: None
    w2.close()
    print(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
