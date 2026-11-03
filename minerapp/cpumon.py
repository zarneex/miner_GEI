"""Датчики процессора: загрузка из Windows + температура/мощность/частота от моста LHM.

Загрузка — `GetSystemTimes` (дёшево, без WMI). А вот температуру, пакетную мощность и
реальную частоту Windows без драйвера не отдаёт: WMI-зона ACPI на этой плате даёт
290 К (16.9 °C, мусор), `NtPowerInformation` — только номинал 4.7 ГГц. Поэтому их
пишет отдельный привилегированный мост `W:\\mining\\tools\\lhmd.ps1`
(LibreHardwareMonitor + его драйвер) в `data\\cpu.json`. Моста нет или данные устарели —
температура/частота честно None, мощность остаётся оценкой 8.75 Вт на поток XMRig.
"""
import ctypes
import ctypes.wintypes as wt
import json
import time

from . import paths

W_PER_THREAD = 8.75
BRIDGE_TTL = 12.0        # данные моста старше этого — считаем, что он не работает


class CpuMonitor:
    """Загрузка CPU между вызовами `sample()`; первый вызов даёт None."""

    def __init__(self) -> None:
        self._prev: tuple[int, int, int] | None = None
        self._bridge_seen = False

    @staticmethod
    def _times() -> tuple[int, int, int] | None:
        idle, kernel, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None

        def val(t: wt.FILETIME) -> int:
            return (t.dwHighDateTime << 32) | t.dwLowDateTime

        return val(idle), val(kernel), val(user)

    @staticmethod
    def _bridge() -> dict:
        """Свежий срез от lhmd.ps1: temp_c, power_w, mhz, load_pct."""
        try:
            data = json.loads(paths.CPU_JSON.read_text(encoding="utf-8"))
            if time.time() - float(data.get("ts") or 0) > BRIDGE_TTL:
                return {}
            return data
        except (OSError, ValueError, TypeError):
            return {}

    def sample(self, threads: int) -> dict:
        cur = self._times()
        util = None
        if cur is not None and self._prev is not None:
            d_idle = cur[0] - self._prev[0]
            d_total = (cur[1] - self._prev[1]) + (cur[2] - self._prev[2])
            if d_total > 0:
                util = max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))
        self._prev = cur
        bridge = self._bridge()
        alive = bool(bridge)
        watts = bridge.get("power_w")
        return {"cpu_util": util,
                "cpu_watts": watts if watts else None,   # пакетная мощность от LHM
                "cpu_est_watts": threads * W_PER_THREAD if threads else None,
                "cpu_temp": bridge.get("temp_c"),
                "cpu_mhz": bridge.get("mhz"),
                "cpu_bridge": alive}
