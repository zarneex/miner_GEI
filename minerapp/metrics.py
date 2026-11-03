"""Метрики: интегралы сеанса и «скорость сейчас» по последним минутам реальной работы.

`RateWindow` считает средний хешрейт только за то время, когда майнер действительно работал
(простои и разгон отбрасываются), и оценивает относительную погрешность: чем больше точек
набралось за окно, тем точнее «сейчас, ₽/час».
"""
import csv
import time
from collections import deque

from . import config as cfgmod
from .paths import SAMPLES

CPU_WATTS_PER_THREAD = 8.75  # 105 Вт / 12 потоков, замер 2026-11-02
RATE_WINDOW_S = 600           # окно оценки «сейчас»


class SessionMetrics:
    def __init__(self) -> None:
        self.started = time.time()
        self.kwh = 0.0            # GPU + локальный CPU (оценка по потокам)
        self.usd = 0.0            # только GPU
        self.cpu_usd = 0.0        # локальный CPU, по фактическому хешрейту
        self.cpu_remote_usd = 0.0  # второй ПК, по фактическому хешрейту
        self.cpu_remote_kwh = 0.0
        self._last_ts = self.started
        self._samples: dict[str, dict] = {}
        self._samples_ts = 0.0

    @property
    def total_usd(self) -> float:
        """Доход сеанса по всем устройствам: GPU + CPU + второй ПК."""
        return self.usd + self.cpu_usd + self.cpu_remote_usd

    @property
    def total_kwh(self) -> float:
        return self.kwh + self.cpu_remote_kwh

    # --- доход -------------------------------------------------------------
    def _load_samples(self) -> None:
        if time.time() - self._samples_ts < 300:
            return
        self._samples_ts = time.time()
        if not SAMPLES.exists():
            return
        try:
            with open(SAMPLES, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    coin = row.get("coin")
                    if not coin:
                        continue
                    try:
                        self._samples[coin] = {
                            "ts": row["ts"],
                            "rev_day": float(row["rev_day"]),
                            "net_day": float(row["net_day"]),
                        }
                    except (KeyError, ValueError):
                        continue
        except OSError:
            pass

    def coin_rev_per_sec(self, coin: str) -> tuple[float, str | None]:
        """Оценка выручки в секунду и предупреждение, если данные стары."""
        self._load_samples()
        row = self._samples.get(coin)
        if not row:
            return 0.0, "нет замеров дохода в samples.csv — доход не считается"
        try:
            ts = time.mktime(time.strptime(row["ts"], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return 0.0, "битая метка времени в samples.csv"
        age_min = (time.time() - ts) / 60
        warn = None
        if age_min > 90:
            warn = f"оценка дохода устарела на {age_min:.0f} мин"
        return max(0.0, row["rev_day"] / 86400.0), warn

    @staticmethod
    def ref_hashrate(coin: str) -> float | None:
        """Хешрейт, под который посчитан доход в samples.csv (пресет normal)."""
        try:
            cfg = cfgmod.load()
            return float(cfg["coins"][coin]["measured"]["normal"]["hashrate"])
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def usd_per_hour_mark(self, coin: str) -> float:
        """$ в час на 1 H/s — чтобы считать «сейчас» по текущему хешрейту."""
        rev, _ = self.coin_rev_per_sec(coin)
        ref = self.ref_hashrate(coin)
        if not rev or not ref:
            return 0.0
        return rev * 3600.0 / ref

    # --- интеграл ----------------------------------------------------------
    def tick(self, gpu_watts: float, cpu_threads: int, gpu_coin: str | None,
             gpu_running: bool, cpu_running: bool) -> dict:
        """Один такт: обновляет итоги сеанса и возвращает приращения."""
        now = time.time()
        dt = max(0.0, min(now - self._last_ts, 60.0))
        self._last_ts = now
        watts = (gpu_watts if gpu_running else 0.0) + (
            cpu_threads * CPU_WATTS_PER_THREAD if cpu_running else 0.0)
        d_kwh = watts * dt / 3_600_000.0
        d_usd = 0.0
        if gpu_running and gpu_coin:
            rev, _ = self.coin_rev_per_sec(gpu_coin)
            d_usd = rev * dt
        self.kwh += d_kwh
        self.usd += d_usd
        return {"watts": watts, "d_kwh": d_kwh, "d_usd": d_usd, "dt": dt}

    def elapsed(self) -> float:
        return time.time() - self.started


class RateWindow:
    """Средний хешрейт за последние N секунд работы + относительная погрешность."""

    def __init__(self, window_s: int = RATE_WINDOW_S) -> None:
        self.window_s = window_s
        self._rows: deque[tuple[float, float, bool]] = deque()

    def feed(self, ts: float, hashrate: float | None, productive: bool) -> None:
        self._rows.append((ts, float(hashrate or 0.0), bool(productive)))
        cutoff = ts - self.window_s
        while self._rows and self._rows[0][0] < cutoff:
            self._rows.popleft()

    def clear(self) -> None:
        self._rows.clear()

    def stats(self) -> dict:
        """mean — средний рабочий хешрейт, rel_pct — оценка относительной погрешности, %."""
        rows = [(ts, hr) for ts, hr, productive in self._rows if productive and hr > 0]
        n = len(rows)
        if not n:
            return {"mean": 0.0, "rel_pct": 0.0, "points": 0, "seconds": 0.0}
        mean = sum(hr for _, hr in rows) / n
        if n > 2:
            var = sum((hr - mean) ** 2 for _, hr in rows) / (n - 1)
            stderr = (var ** 0.5) / (n ** 0.5)
            rel = stderr / mean * 100 if mean else 0.0
        else:
            rel = 100.0
        span = rows[-1][0] - rows[0][0] if n > 1 else 0.0
        return {"mean": mean, "rel_pct": min(rel, 100.0), "points": n, "seconds": span}
