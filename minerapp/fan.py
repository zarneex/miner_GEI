"""Владелец кулера: значение из интерфейса — единственный источник истины.

NVML на потребительских картах умеет «ровно столько» и не умеет «не выше»,
поэтому потолок из приложения реализован как **фиксация** оборотов. Раньше
приложение помнило своё прошлое действие (`_fan_ctrl`) и писало в карту только
при смене цели. Любой сброс политики со стороны (драйвер, старт/стоп майнера,
`krig --gpu-fan`, прошлый запуск) оставлял карту на автокривой, а приложение
продолжало считать, что держит обороты — отсюда кулер «залипал» на 75–80 %
при значении 57.5 %.

Теперь памяти нет: каждый такт читаем состояние карты (политику и обороты) и,
если она ушла из-под контроля, возвращаем её к значению из интерфейса. Все
решения и ошибки пишутся в `W:\\mining\\data\\gui-fan.log` — молчаливых отказов
контроллера больше не бывает.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import gpu as gpu_mod
from . import paths

LOG = paths.DATA / "gui-fan.log"
LOG_MAX = 256 * 1024          # больше не растём: старый файл уезжает в .1
FIX_LOG_S = 20                # «карта ушла из-под контроля» — не чаще раза в 20 с
ERR_LOG_S = 60


class FanKeeper:
    """Держит обороты карты на значении из приложения, сверяясь с самой картой."""

    def __init__(self, log: Path | str = LOG, on_msg=None, tolerance: int = 2):
        self.log_path = Path(log)
        self.on_msg = on_msg
        self.tolerance = tolerance
        self.active = False       # карта сейчас под нашим контролем
        self.desired = 0          # обороты, которые требует приложение (0 — не вмешиваемся)
        self.emergency = False    # вышли выше потолка из-за перегрева
        self._last_fix_log = 0.0
        self._last_err_log = 0.0

    # --- публичное API -----------------------------------------------------
    def apply(self, cap: float, temp: float | None, max_temp: float | None,
              running: bool = True, quiet_fan: float | None = None) -> bool:
        """Один такт контроля. True — карта под управлением приложения.

        `cap` — «Макс. вентиляторы» (потолок). `quiet_fan` — тише потолка
        (ночной пресет), берётся минимум: обе величины заданы в приложении.
        Выше потолка выходим только при реальном перегреве: temp > max_temp + 3,
        возвращаемся при temp <= max_temp - 2 (гистерезис, без качелей).
        """
        if not running:
            self.release("GPU остановлен")
            return False
        cap = float(cap or 0)
        if cap <= 0:
            self.release("потолок 0 % — не вмешиваюсь")
            return False
        want = cap
        if quiet_fan:
            want = min(want, float(quiet_fan))
        if temp is not None and max_temp is not None:
            if temp > float(max_temp) + 3:
                self.emergency = True
            elif temp <= float(max_temp) - 2:
                self.emergency = False
            if self.emergency:
                want = 100.0
        self.desired = want
        return self._enforce("перегрев" if self.emergency else "потолок")

    def release(self, why: str = "") -> None:
        """Отдать кулер автокривой карты (GPU остановлен, окно закрыто, потолок 0)."""
        was_active = self.active
        self.active = False
        self.emergency = False
        self.desired = 0
        if gpu_mod.fan_policy() != gpu_mod.FAN_POLICY_FIXED:
            return
        ok, msg = gpu_mod.reset_fan_policy()
        if was_active or not ok:
            self._say(f"кулер: автообдув{(' — ' + why) if why else ''} ({msg})")

    # --- внутреннее --------------------------------------------------------
    def note(self, text: str) -> None:
        """Записать событие в журнал, не показывая его в консоли."""
        self._write(text)

    def _enforce(self, why: str) -> bool:
        pct = int(max(0, min(100, round(self.desired))))
        policy = gpu_mod.fan_policy()
        speed = gpu_mod.fan_speed()
        if policy == gpu_mod.FAN_POLICY_FIXED and (
                speed is None or abs(speed - pct) <= self.tolerance):
            if not self.active:
                self.active = True
                self._say(f"кулер {pct}% ({why}) — под контролем приложения")
            return True
        was = self.active
        ok, msg = gpu_mod.set_fan_speed(pct)
        self.active = bool(ok)
        if not ok:
            self.log_error(f"кулер {pct}% ({why}): {msg}")
            return False
        now = time.time()
        if not was or now - self._last_fix_log > FIX_LOG_S:
            what = ("взял под контроль" if not was
                    else "карту вернули к значению приложения")
            self._say(f"кулер {pct}% ({why}): {what} (было policy={policy}, {speed}%)")
            self._last_fix_log = now
        return True

    def _say(self, text: str) -> None:
        if self.on_msg:
            try:
                self.on_msg(text)
            except Exception:       # noqa: BLE001 — вывод не должен ронять контроль
                pass
        self._write(text)

    def log_error(self, text: str) -> None:
        now = time.time()
        if now - self._last_err_log < ERR_LOG_S:
            return
        self._last_err_log = now
        self._write("ОШИБКА " + text)
        if self.on_msg:
            try:
                self.on_msg("ограничитель кулера: " + text)
            except Exception:       # noqa: BLE001
                pass

    def _write(self, text: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n"
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX:
                self.log_path.replace(self.log_path.with_suffix(".1.log"))
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
