#!/usr/bin/env python
"""Поле куки для баланса: сохранение, отмена, очистка и шаги спинбоксов.

Конфиг трогаем аккуратно: исходную секцию `app.balance` сохраняем и возвращаем
в конце, чтобы тест не оставлял за собой чужие куки.
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["MINER_NO_AUTOSTART"] = "1"

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import config as cfgmod, paths, ui  # noqa: E402
from minerapp.balance import normalize_cookie  # noqa: E402

ok = True


def out(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def check(name: str, good: bool) -> None:
    global ok
    out(f"{name}: {'PASS' if good else 'FAIL'}")
    ok = ok and good


def headers_of(cfg: dict) -> dict:
    return ((cfgmod.app_settings(cfg).get("balance") or {}).get("headers") or {})


def main() -> int:
    app = QApplication(sys.argv)
    original = json.dumps(cfgmod.load().get("app", {}).get("balance"), ensure_ascii=False)

    # тест не зависит от того, задана ли у пользователя живая кука: начинаем с пустой
    cfg0 = cfgmod.load()
    cfgmod.app_settings(cfg0).setdefault("balance", {})["headers"] = {}
    cfgmod.save(cfg0)

    w = ui.MainWindow()
    check("поле куки скрытое (не на экране)", w.ed_cookie.echoMode().name == "Password")
    check("пустое поле работает без ошибок", w._cookie_value() == "")

    check("голое значение получает имя sessionid",
          normalize_cookie("abc123") == "sessionid=abc123")
    check("экспорт Cookie-Editor (JSON) разбирается",
          normalize_cookie('[{"name":"sessionid","value":"a"},{"name":"csrftoken","value":"b"}]')
          == "sessionid=a; csrftoken=b")
    check("пустая строка остаётся пустой", normalize_cookie("   ") == "")

    w.ed_cookie.setText("krx_test_cookie=1")
    w.on_cookie_apply()
    check("куки сохранились в конфиг", headers_of(cfgmod.load()).get("Cookie") == "krx_test_cookie=1")
    check("поле очищено после сохранения", w.ed_cookie.text() == "")
    check("подпись помечена, что куки заданы",
          "7fc99a" in w.lbl_cookie.styleSheet())
    check("плейсхолдер говорит, что куки есть",
          "сохранены" in w.ed_cookie.placeholderText())

    w.btn_cookie_cancel.click()
    check("«Отмена» не стирает сохранённое",
          headers_of(cfgmod.load()).get("Cookie") == "krx_test_cookie=1")

    w.on_cookie_apply()          # пустое поле = удалить куки
    check("пустое поле удаляет куки", "Cookie" not in headers_of(cfgmod.load()))

    steps = (w.spin_temp.singleStep(), w.spin_fan.singleStep(),
             w.spin_threads.singleStep(), w.spin_pl.singleStep())
    out(f"шаги: температура {steps[0]}, кулер {steps[1]}, потоки {steps[2]}, "
        f"лимит мощности {steps[3]} Вт")
    check("температура и кулер шагают по 2.5", steps[0] == 2.5 and steps[1] == 2.5)
    check("потоки шагают по 1", steps[2] == 1)
    check("лимит мощности шагает по 10 Вт", steps[3] == 10)

    # подсказки периодов снова с разбивкой прогноза GPU/CPU
    now = time.time()
    for i in range(40):
        w.rate_gpu.feed(now - 40 + i, 108e12 * (1 + 0.02 * ((i % 3) - 1)), True)
    w._seed_periods()
    w._update_metrics("pearl")
    tip = w.card_day.toolTip()
    check("в подсказке есть прогноз с разбивкой",
          "прогноз на сутки" in tip and "GPU " in tip and "CPU " in tip)

    # вернуть исходную секцию баланса
    cfg = cfgmod.load()
    cfgmod.app_settings(cfg)
    if original and original != "null":
        cfg["app"]["balance"] = json.loads(original)
    else:
        cfg["app"].pop("balance", None)
    cfgmod.save(cfg)
    check("конфиг восстановлен", json.dumps(cfgmod.load()["app"].get("balance"),
                                            ensure_ascii=False) == original)

    w.krig.is_running = lambda: False       # диалога подтверждения больше нет
    w.close()
    out(f"ИТОГ: {'PASS' if ok else 'FAIL'} (конфиг {paths.CFG})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
