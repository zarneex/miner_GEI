"""Проверка: реально ли ставятся порог температуры и фиксированные обороты на этой карте.

Меняем только на текущие/безопасные значения и сразу возвращаем как было:
порог замедления 90 -> 85 -> 90; обороты авто -> фикс (текущие %) -> авто.
"""
import ctypes
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
THROTTLE = 0
lib = ctypes.WinDLL("nvml.dll")
assert lib.nvmlInit_v2() == 0
h = ctypes.c_void_p()
assert lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) == 0


def thr(kind: int) -> int:
    v = ctypes.c_uint(0)
    rc = lib.nvmlDeviceGetTemperatureThreshold(h, ctypes.c_int(kind), ctypes.byref(v))
    return v.value if rc == 0 else -rc


def set_thr(kind: int, value: int) -> int:
    return lib.nvmlDeviceSetTemperatureThreshold(h, ctypes.c_int(kind),
                                                ctypes.byref(ctypes.c_uint(value)))


def fan() -> int:
    v = ctypes.c_uint(0)
    lib.nvmlDeviceGetFanSpeed_v2(h, 0, ctypes.byref(v))
    return v.value


def policy() -> int:
    v = ctypes.c_int(-1)
    lib.nvmlDeviceGetFanControlPolicy_v2(h, 0, ctypes.byref(v))
    return v.value


print("порог замедления сейчас:", thr(THROTTLE))
print("set_threshold(85) rc =", set_thr(THROTTLE, 85), "-> прочитано:", thr(THROTTLE))
print("возврат 90 rc =", set_thr(THROTTLE, 90), "-> прочитано:", thr(THROTTLE))

cur = fan()
print(f"обороты авто: {cur}% (политика {policy()})")
rc1 = lib.nvmlDeviceSetFanSpeed_v2(h, 0, ctypes.c_uint(cur))
rc2 = lib.nvmlDeviceSetFanControlPolicy(h, 0, CT := 1)
print(f"фикс: set_speed rc={rc1}, policy rc={rc2}, политика={policy()}, обороты={fan()}%")
rc3 = lib.nvmlDeviceSetFanControlPolicy(h, 0, 0)
print(f"возврат авто: rc={rc3}, политика={policy()}, обороты={fan()}%")
