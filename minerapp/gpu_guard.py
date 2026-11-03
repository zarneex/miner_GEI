"""Динамический лимит мощности GPU: снижаем только на время перегрева.

Почему так, а не «срезали и забыли»: `nvidia-smi -pl` меняет максимум, а не
потребление «здесь и сейчас». Если один раз снизить лимит при перегреве и не
вернуть его, карта навсегда остаётся на 190–200 Вт, и в интерфейсе «текущий
лимит» перестаёт совпадать со спинбоксом. Поэтому цель (значение пользователя)
хранится отдельно, а применённый лимит — это регулятор: он возвращается к цели,
как только температура уходит ниже порога.

Гистерезис (замерено: 10 Вт ≈ 1.3 °C, поэтому шаг 2.5 °C спинбокса осмыслен):
  * снижаем на 10 Вт, когда температура держится выше порога;
  * возвращаем по 5 Вт, только когда температура ниже порога на 3 °C;
  * между этими уровнями — мёртвая зона, поэтому 190↔200 у порога не скачут.
"""
from __future__ import annotations


class PowerGuard:
    """Регулятор лимита мощности: цель, применённое значение и гистерезис."""

    def __init__(self, min_w: int = 175, max_w: int = 250, step_down: int = 10,
                 step_up: int = 5, down_every: float = 30.0, up_every: float = 30.0,
                 band: float = 3.0, hot_n: int = 2, stop_margin: float = 6.0,
                 stop_hold: float = 60.0) -> None:
        self.min_w = int(min_w)
        self.max_w = int(max_w)
        self.step_down = int(step_down)
        self.step_up = int(step_up)
        self.down_every = float(down_every)
        self.up_every = float(up_every)
        self.band = float(band)
        self.hot_n = int(hot_n)
        self.stop_margin = float(stop_margin)
        self.stop_hold = float(stop_hold)
        self.target = self.max_w
        self.applied = self.max_w
        self.last_change = 0.0
        self._hot = 0
        self._stop_since: float | None = None

    # --- цель -------------------------------------------------------------
    def reset(self, target: int) -> None:
        """Новый майнер/старт: цель и применённое значение совпадают."""
        self.target = self._clamp(target)
        self.applied = self.target
        self.last_change = 0.0
        self._hot = 0
        self._stop_since = None

    def set_target(self, target: int, now: float) -> int:
        """Пользователь крутил спинбокс: применяем ровно это значение.

        Если карте станет жарко, регулятор сам снизит лимит и потом вернёт.
        """
        self.target = self._clamp(target)
        self.applied = self.target
        self.last_change = float(now)
        self._hot = 0
        self._stop_since = None
        return self.applied

    def _clamp(self, watts: int) -> int:
        return max(self.min_w, min(self.max_w, int(watts)))

    # --- регулятор --------------------------------------------------------
    def observe(self, temp: float | None, limit_c: float, now: float) -> int | None:
        """Один такт датчиков. Возвращает новый лимит, если его надо применить."""
        if temp is None:
            return None
        if temp >= limit_c:
            self._hot += 1
            if (self._hot >= self.hot_n and self.applied > self.min_w
                    and now - self.last_change >= self.down_every):
                self.applied = max(self.min_w, self.applied - self.step_down)
                self.last_change = float(now)
                self._hot = 0
                return self.applied
            return None
        self._hot = 0
        if temp <= limit_c - self.band and self.applied < self.target:
            if now - self.last_change >= self.up_every:
                self.applied = min(self.target, self.applied + self.step_up)
                self.last_change = float(now)
                return self.applied
        return None

    def need_stop(self, temp: float | None, limit_c: float, now: float) -> bool:
        """Карта уже на минимуме, а температура выше порога + 6 °C — пора гасить."""
        if temp is None:
            self._stop_since = None
            return False
        if self.applied > self.min_w or temp < limit_c + self.stop_margin:
            self._stop_since = None
            return False
        if self._stop_since is None:
            self._stop_since = float(now)
            return False
        return now - self._stop_since >= self.stop_hold
