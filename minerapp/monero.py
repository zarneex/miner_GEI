"""Доход CPU-майнинга (Monero) для счётчика за всё время.

Считать надо честно, поэтому берём цену XMR (CoinGecko) и сложность сети (moneroblocks),
раз в час, с кэшем на диске. Если сети нет — работаем на последних известных числах.
Формула та же, что у свитчера: (H/сеть)·(86400/120)·награда·цена·(1−комиссия).
"""
import json
import threading
import time
import urllib.request

from .paths import DATA

CACHE = DATA / "monero.json"
REFRESH_S = 3600
_lock = threading.Lock()
_refreshing = False
BLOCK_TIME = 120.0
REWARD = 0.6001684
POOL_FEE = 0.01

_cache: dict = {"rev_per_hs_day": 0.0, "ts": 0.0, "price": 0.0, "nethash": 0.0}


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "miner-gui/1.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _from_disk() -> None:
    try:
        saved = json.loads(CACHE.read_text(encoding="utf-8"))
        _cache.update(rev_per_hs_day=float(saved.get("rev_per_hs_day", 0.0)),
                      price=float(saved.get("price", 0.0)),
                      nethash=float(saved.get("nethash", 0.0)))
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _refresh() -> None:
    """Сеть и запись кэша — в фоновом потоке: в GUI это давало паузы до 24 с."""
    now = time.time()
    price = nethash = 0.0
    try:
        price = float(_get("https://api.coingecko.com/api/v3/simple/price?ids=monero"
                           "&vs_currencies=usd")["monero"]["usd"])
        stats = _get("https://moneroblocks.info/api/get_stats")
        nethash = float(stats["difficulty"]) / BLOCK_TIME
    except (OSError, ValueError, KeyError, TypeError):
        price, nethash = _cache["price"], _cache["nethash"]
        if not (price > 0 and nethash > 0):
            _from_disk()
            return
    rev = 0.0
    if price > 0 and nethash > 0:
        coins_day = (1.0 / nethash) * (86400.0 / BLOCK_TIME) * REWARD
        rev = coins_day * price * (1 - POOL_FEE)
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps({"price": price, "nethash": nethash,
                                         "rev_per_hs_day": rev, "ts": now}),
                             encoding="utf-8")
        except OSError:
            pass
    _cache.update(rev_per_hs_day=rev, ts=now, price=price, nethash=nethash)


def _kick() -> None:
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True

    def job() -> None:
        global _refreshing
        try:
            _refresh()
        except Exception:
            pass
        finally:
            with _lock:
                _refreshing = False

    threading.Thread(target=job, daemon=True, name="monero").start()


def rev_per_hs_day() -> float:
    """Выручка в $ за сутки на 1 H/s (с учётом комиссии пула). 0.0 — данных нет.
    Отдаём последнее известное значение сразу, обновление уходит в фон."""
    now = time.time()
    if _cache["ts"] and now - _cache["ts"] < REFRESH_S:
        return _cache["rev_per_hs_day"]
    if not _cache["ts"]:
        _from_disk()
        _cache["ts"] = 0.0
    _kick()
    return _cache["rev_per_hs_day"]
