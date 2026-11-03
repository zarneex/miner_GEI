"""Курс USD→RUB и цена электричества в рублях.

Курс берём у ЦБ (cbr-xml-daily.ru, без ключа) раз в час; если сеть недоступна —
последнее известное значение из `data/fx.json`, иначе 80 ₽/$. Цена света — из конфига
(`app.electricity_rub_per_kwh`, по умолчанию 2.10 ₽ — тариф с октября 2026).
"""
import json
import threading
import time
import urllib.request

from . import config as cfgmod
from .paths import DATA

FX = DATA / "fx.json"
CBR = "https://www.cbr-xml-daily.ru/daily_json.js"
FALLBACK = 80.0
REFRESH_S = 3600

_cache: dict = {"rate": None, "ts": 0.0}
_lock = threading.Lock()
_refreshing = False


def _from_disk() -> float | None:
    try:
        return float(json.loads(FX.read_text(encoding="utf-8"))["usd_rub"])
    except (OSError, ValueError, KeyError):
        return None


def _refresh() -> None:
    """Сетевой запрос — только из фонового потока: в GUI это давало паузы до 10 с."""
    now = time.time()
    rate = None
    try:
        req = urllib.request.Request(CBR, headers={"User-Agent": "miner-gui/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        rate = float(data["Valute"]["USD"]["Value"])
        DATA.mkdir(parents=True, exist_ok=True)
        FX.write_text(json.dumps({"usd_rub": rate, "ts": now}), encoding="utf-8")
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        rate = _from_disk() or FALLBACK
    _cache.update(rate=rate, ts=now)


def _kick(fn) -> None:
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True

    def job() -> None:
        global _refreshing
        try:
            fn()
        except Exception:
            pass
        finally:
            with _lock:
                _refreshing = False

    threading.Thread(target=job, daemon=True, name="rates").start()


def usd_rub() -> float:
    """Курс USD→RUB: отдаём сразу (кэш/диск), обновление в фоне."""
    now = time.time()
    if _cache["rate"] and now - _cache["ts"] < REFRESH_S:
        return _cache["rate"]
    if not _cache["rate"]:
        _cache["rate"] = _from_disk()      # мгновенно, чтобы карточки не были нулём
        _cache["ts"] = 0.0
    _kick(_refresh)
    return _cache["rate"] or FALLBACK


def electricity_rub_kwh() -> float:
    try:
        app = cfgmod.app_settings(cfgmod.load())
        return float(app.get("electricity_rub_per_kwh", 2.10))
    except (OSError, ValueError, TypeError):
        return 2.10
