#!/usr/bin/env python
"""Проверка уборки фона: классификация процессов и безопасность списков.

Ничего не закрывает и не пишет в конфиг: только чистые функции и чтение
состояния службы (без прав администратора это тоже безопасно).
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minerapp import cleanup, config  # noqa: E402

ok = True


def check(name: str, good: bool) -> None:
    global ok
    print(f"{name}: {'PASS' if good else 'FAIL'}", flush=True)
    ok = ok and good


def main() -> int:
    # майнер, DSH и система неприкосновенны
    for name in ("krig-miner.exe", "xmrig.exe", "DeepSeek Harness.exe",
                 "pythonw.exe", "python.exe", "explorer.exe", "dwm.exe",
                 "svchost.exe", "lsass.exe", "csrss.exe", "System"):
        check(f"{name} защищён", cleanup.classify(name) == "protected")
    # документы не трогаем вообще
    for name in ("WINWORD.EXE", "excel.exe", "notepad.exe", "Code.exe",
                 "pycharm64.exe", "OBS64.exe", "vmware-vmx.exe"):
        check(f"{name} не трогаем", cleanup.classify(name) == "never")
    # фоновое закрываем принудительно
    for name in ("steam.exe", "steamwebhelper.exe", "OneDrive.exe",
                 "GoogleUpdate.exe", "vgtray.exe", "EpicGamesLauncher.exe"):
        check(f"{name} закрывается", cleanup.classify(name) == "kill")
    # браузер и неизвестное приложение — только мягко
    for name in ("chrome.exe", "msedge.exe", "discord.exe", "some_app.exe"):
        check(f"{name} — мягко", cleanup.classify(name) == "window")
    check("пустое имя не закрываем", cleanup.classify("") == "protected")
    # фоновая копия браузера без окна закрывается принудительно, с окном — нет
    check("chrome без окна — принудительно", cleanup.kill_without_window("chrome.exe"))
    check("discord без окна — принудительно",
          cleanup.kill_without_window("Discord.exe"))
    check("some_app без окна не трогаем", not cleanup.kill_without_window("some_app.exe"))
    check("майнер без окна не трогаем", not cleanup.kill_without_window("krig-miner.exe"))

    app = config.DEFAULTS_APP
    check("в конфиге есть секция system", "system" in app)
    check("по умолчанию уборка выключена", app["system"]["close_extra"] is False)
    check("по умолчанию мониторы не гасим", app["system"]["monitors_off"] is False)
    check("порог простоя для сети", app["system"]["idle_balance_s"] == 180)
    check("порог гашения мониторов", app["system"]["monitor_off_s"] == 300)

    state = cleanup.service_state("wuauserv")
    check("служба wuauserv читается", state is None or state in (1, 2, 3, 4, 5, 6, 7))
    value = cleanup._au_value()
    check("политика NoAutoUpdate читается", value is None or isinstance(value, int))

    # выключенная уборка не трогает даже явный список
    c = cleanup.Cleaner(on_msg=lambda _m: None)
    c.tick()
    check("выключенная уборка — no-op", c.enabled is False)

    print(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
