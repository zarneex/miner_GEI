#!/usr/bin/env python
"""Проверка: можно ли вернуть кулеру автоматический режим через NVML (ctypes).

`krig --gpu-fan N` ставит НВМЛ политику «фиксированные обороты», и она остаётся после
выхода майнера: карта потом греется с малым обдувом. Смотрим, доступен ли сброс.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

for cand in (r"C:\Windows\System32\nvml.dll",
             r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"):
    try:
        nvml = ctypes.WinDLL(cand)
        print("загружен", cand)
        break
    except OSError as e:
        print("не загрузился", cand, e)
else:
    raise SystemExit("nvml.dll не найден")


def call(name: str, *args) -> tuple[int, str]:
    fn = getattr(nvml, name, None)
    if fn is None:
        return -1, "нет функции"
    rc = fn(*args)
    return rc, {0: "OK", 999: "NOT_SUPPORTED", 3: "NOT_SUPPORTED/other",
                1: "UNINITIALIZED"}.get(rc, str(rc))


nvml.nvmlInit_v2()
handle = ctypes.c_void_p()
print("get handle:", nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)))

for fn in ("nvmlDeviceSetFanControlPolicy", "nvmlDeviceGetFanControlPolicy_v2",
           "nvmlDeviceSetDefaultFanSpeed_v2", "nvmlDeviceSetFanSpeed_v2",
           "nvmlDeviceGetFanSpeed", "nvmlDeviceGetNumFans"):
    print(f"  {fn}: {hasattr(nvml, fn)}")

for fan in (0,):
    pol = ctypes.c_int(-1)
    rc, msg = call("nvmlDeviceGetFanControlPolicy_v2", handle, fan, ctypes.byref(pol))
    print(f"policy(fan={fan}) rc={rc} {msg} value={pol.value}")

for fan in (0, 1):
    rc, msg = call("nvmlDeviceSetFanControlPolicy", handle, fan, 0)
    print(f"set policy default fan={fan}: rc={rc} {msg}")

spd = ctypes.c_uint()
for i in range(6):
    rc, _ = call("nvmlDeviceGetFanSpeed", handle, ctypes.byref(spd))
    pol = ctypes.c_int(-1)
    rc2, _ = call("nvmlDeviceGetFanControlPolicy_v2", handle, 0, ctypes.byref(pol))
    print(f"  t+{i*5}s: fan={spd.value}% policy={pol.value} (rc {rc},{rc2})")
    time.sleep(5)
