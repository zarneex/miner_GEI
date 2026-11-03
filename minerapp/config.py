"""Работа с общим конфигом W:\\mining\\config.json.

Второго конфига приложение не заводит: настройки GUI живут в секции "app" того же файла.
Запись атомарная (tmp + replace), чтобы свитчер не прочитал половину файла.
"""
import json
import os
import time
from pathlib import Path

from .paths import CFG, LOGS

DEFAULTS_APP = {
    "gpu": {"max_temp": 80, "max_fan": 80, "night": False, "autostart": True,
            "coin": "pearl"},
    "cpu": {"max_temp": 85.0, "threads": 8, "autostart": False, "allow_100": True},
    "autostart_app": False,
    "electricity_rub_per_kwh": 2.10,
    # ВНИМАНИЕ: close_extra — крайне нестабильная функция (cleanup.Cleaner):
    # она закрывает чужие процессы и роняет само приложение-майнер. Галочка
    # в интерфейсе заблокирована, значение всегда False; включать не нужно.
    "system": {"close_extra": False, "monitors_off": False,
               "tray_on_min": False, "tray_on_close": False,
               "idle_balance_s": 180, "idle_gui_s": 300, "monitor_off_s": 300},
    "balance": {"url": "", "token": "", "json_path": ""},
    "auto": {"last_switch": 0.0},
}


def load() -> dict:
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def save(cfg: dict) -> None:
    tmp = CFG.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    for _ in range(5):
        try:
            os.replace(tmp, CFG)
            return
        except PermissionError:
            time.sleep(0.2)
    os.replace(tmp, CFG)


def app_settings(cfg: dict) -> dict:
    """Секция "app" с补齐 умолчаний. Недостающие ключи дописываются в конфиг."""
    app = cfg.setdefault("app", {})
    changed = False
    for section, defaults in DEFAULTS_APP.items():
        if isinstance(defaults, dict):
            cur = app.setdefault(section, {})
            for k, v in defaults.items():
                if k not in cur:
                    cur[k] = v
                    changed = True
        elif section not in app:
            app[section] = defaults
            changed = True
    if changed:
        try:
            save(cfg)
        except OSError as e:
            print(f"конфиг не сохранился: {e}")
    return app


def save_app(key: str, value) -> None:
    """Точечно сохранить один ключ секции app."""
    try:
        cfg = load()
        app_settings(cfg)
        cfg["app"][key] = value
        save(cfg)
    except (OSError, ValueError) as e:
        print(f"конфиг не сохранился ({key}): {e}")


def preset_for(coin: str, night: bool) -> dict:
    """Пресет запуска: ночной — 175 Вт с памятью +1500, обычный — как в конфиге."""
    cfg = load()
    if night:
        return dict(cfg["presets"]["quiet"])
    base = dict(cfg["coins"][coin].get("preset") or cfg["presets"]["normal"])
    base.setdefault("plimit", 250)
    return base


def log_event(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    LOGS.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOGS / "gui.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
