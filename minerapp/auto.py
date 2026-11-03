"""Автовыбор конфига майнинга: какая монета выгоднее прямо сейчас.

Логика взята из свитчера (`W:\\mining\\scripts\\watch.py`), но решение принимает приложение:
раз в `interval` секунд обновляем цены/сложность (`watch.sample_once` пишет `data/samples.csv`),
считаем чистый доход за сутки по каждой монете и предлагаем переключиться, если лидер
выгоднее текущей больше чем на `min_gain_pct` и с прошлого переключения прошло
`min_dwell_minutes`.
"""
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, Signal

from .paths import ensure_engine_path

ensure_engine_path()
import watch  # noqa: E402  (движок свитчера, W:\mining\scripts)


class AutoWatcher(QObject):
    """Фоновый опрос доходности в демон-потоке.

    Не QThread специально: сетевой вызов может висеть десятки секунд (ретраи свитчера),
    а демон-поток не мешает закрытию приложения и не даёт Qt ругаться на живой поток.
    """

    result = Signal(dict)

    def __init__(self, interval_s: int = 900, parent: Any = None) -> None:
        super().__init__(parent)
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._paused = threading.Event()    # простой ПК: доходность не опрашиваем
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="auto-coin", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_paused(self, paused: bool) -> None:
        """Простой ПК: сетевой опрос доходности не нужен, проснёмся по вводу."""
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(5.0)
                continue
            try:
                rows = watch.sample_once(watch.load_cfg()) or []
                net = {r["coin"]: float(r["net_day"]) for r in rows
                       if r.get("coin") and r.get("net_day") is not None}
                if net:
                    best = max(net, key=net.get)
                    self.result.emit({
                        "at": time.time(), "net": net, "best": best,
                        "gain_pct": _gain(net, best),
                    })
            except Exception as e:  # сеть, парсинг, файлы — не роняем поток
                self.result.emit({"at": time.time(), "error": str(e)[:200]})
            self._stop.wait(self.interval_s)


def _gain(net: dict[str, float], best: str) -> float:
    """Насколько лидер выгоднее второй монеты, %."""
    others = [v for k, v in net.items() if k != best]
    if not others:
        return 0.0
    second = max(others)
    if second <= 0:
        return 100.0
    return (net[best] - second) / second * 100.0
