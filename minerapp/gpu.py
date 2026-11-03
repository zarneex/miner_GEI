"""Обёртки над nvidia-smi и NVML: датчики, лимит мощности, политика кулера.

Про обороты: NVML на потребительских картах не даёт *потолка*, но даёт фиксированные
обороты (этим пользуется `krig --gpu-fan`) и — проверено на RTX 5070 — сброс политики
кулера обратно в авто (`nvmlDeviceSetFanControlPolicy`, policy=0). Поэтому ночной режим
ставит фикс 55%, а при выходе из него приложение возвращает автообдув; иначе карта
осталась бы с малым обдувом на 250 Вт (замерено: 79 °C вместо 75 °C).
"""
import ctypes
import re
import subprocess

from .paths import NVSMI

FIELDS = ("power.draw,power.limit,temperature.gpu,fan.speed,"
          "clocks.current.sm,clocks.current.memory,memory.used,memory.total,"
          "utilization.gpu")

FAN_POLICY_AUTO = 0
FAN_POLICY_FIXED = 1

_TIMEOUT = 8
_nvml: tuple | None | bool = None


def _run(args: list[str]) -> tuple[int, str]:
    try:
        # CREATE_NO_WINDOW обязателен: приложение живёт под pythonw без своей консоли,
        # и каждый nvidia-smi иначе открывает новое окно терминала (мигает пустой терминал)
        r = subprocess.run([str(NVSMI)] + args, capture_output=True, text=True,
                           timeout=_TIMEOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _num(text: str):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def read() -> dict:
    """Один вызов nvidia-smi на все датчики. Пустой dict, если карта не отвечает."""
    rc, out = _run([f"--query-gpu={FIELDS}", "--format=csv,noheader,nounits"])
    if rc != 0 or not out:
        return {}
    parts = [p.strip() for p in out.splitlines()[0].split(",")]
    keys = ["power", "plimit", "temp", "fan", "sm_clock", "mem_clock",
            "vram_mb", "vram_total_mb", "util"]
    return {k: _num(v) for k, v in zip(keys, parts)}


def set_power_limit(watts: int) -> tuple[bool, str]:
    rc, out = _run(["-pl", str(int(watts))])
    ok = rc == 0 and "All done" in out
    return ok, out.replace("\n", " ")[:200]


def limits() -> tuple[int | None, int | None]:
    """Заводские границы лимита мощности (мин/макс), Вт."""
    rc, out = _run(["-q", "-d", "POWER"])
    lo = hi = None
    if rc == 0:
        m = re.search(r"Min Power Limit\s*:\s*([\d.]+)\s*W", out)
        if m:
            lo = int(float(m.group(1)))
        m = re.search(r"Max Power Limit\s*:\s*([\d.]+)\s*W", out)
        if m:
            hi = int(float(m.group(1)))
    return lo, hi


def reset_clocks() -> None:
    _run(["--reset-gpu-clocks"])


# --- политика кулера через NVML -------------------------------------------
def _nvml_handle():
    global _nvml
    if _nvml is not None:
        return _nvml or None
    try:
        lib = ctypes.WinDLL("nvml.dll")
        if lib.nvmlInit_v2() != 0:
            _nvml = False
            return None
        h = ctypes.c_void_p()
        if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
            _nvml = False
            return None
        _nvml = (lib, h)
        return _nvml
    except (OSError, AttributeError):
        _nvml = False
        return None


def fan_policy() -> int | None:
    """0 — авто, 1 — фиксированные обороты, None — карта/функция недоступна."""
    got = _nvml_handle()
    if not got:
        return None
    lib, h = got
    try:
        pol = ctypes.c_int(-1)
        if lib.nvmlDeviceGetFanControlPolicy_v2(h, 0, ctypes.byref(pol)) != 0:
            return None
        return pol.value
    except AttributeError:
        return None


def reset_fan_policy() -> tuple[bool, str]:
    """Вернуть кулеру авторежим после фиксированных оборотов майнера."""
    got = _nvml_handle()
    if not got:
        return False, "NVML недоступен"
    lib, h = got
    try:
        rc = lib.nvmlDeviceSetFanControlPolicy(h, 0, FAN_POLICY_AUTO)
    except AttributeError:
        return False, "функция сброса политики кулера не найдена"
    return rc == 0, ("автообдув восстановлен" if rc == 0
                     else f"сброс политики кулера: rc={rc}")


def fan_speed() -> int | None:
    """Текущие обороты по данным NVML (0..100), None — недоступно.

    Нужно контроллеру кулера: он сверяет то, что требует приложение, с тем,
    что карта реально показывает, а не со своей памятью о прошлой записи.
    """
    got = _nvml_handle()
    if not got:
        return None
    lib, h = got
    try:
        val = ctypes.c_uint(0)
        if lib.nvmlDeviceGetFanSpeed_v2(h, 0, ctypes.byref(val)) != 0:
            return None
        return int(val.value)
    except AttributeError:
        return None


def set_fan_speed(percent: int) -> tuple[bool, str]:
    """Зафиксировать обороты кулера в процентах (проверено на RTX 5070: работает).

    NVML не даёт «потолка» оборотов, поэтому потолок делается так: включаем режим
    фиксированных оборотов и сами держим их на нужном уровне.
    """
    got = _nvml_handle()
    if not got:
        return False, "NVML недоступен"
    lib, h = got
    percent = max(0, min(100, int(percent)))
    try:
        rc_pol = lib.nvmlDeviceSetFanControlPolicy(h, 0, FAN_POLICY_FIXED)
        rc_sp = lib.nvmlDeviceSetFanSpeed_v2(h, 0, ctypes.c_uint(percent))
    except AttributeError:
        return False, "функции управления кулером не найдены"
    ok = rc_pol == 0 and rc_sp == 0
    if ok and fan_policy() != FAN_POLICY_FIXED:
        # запись «успешна», но карта политику не приняла — не делаем вид, что держим
        ok = False
    return ok, (f"обороты зафиксированы на {percent}%" if ok
                else f"фикс оборотов: policy rc={rc_pol}, speed rc={rc_sp}, "
                     f"текущая политика {fan_policy()}")
