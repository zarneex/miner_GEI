"""Что реально умеет железо: датчики CPU и настройка кулера/температуры через NVML.

Проверяем, какие функции NVML вообще есть в этой версии драйвера (можно ли ставить
потолок температуры/фиксированные обороты), и какие датчики CPU доступны без сторонних
программ: температура (WMI ACPI), частота (NtPowerInformation), загрузка (GetSystemTimes).
"""
import ctypes
import ctypes.wintypes as wt
import platform
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def nvml() -> None:
    print("=== NVML ===")
    lib = ctypes.WinDLL("nvml.dll")
    print("nvmlInit:", lib.nvmlInit_v2())
    h = ctypes.c_void_p()
    print("handle:", lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)))
    for name in ("nvmlDeviceSetTemperatureThreshold", "nvmlDeviceGetTemperatureThreshold",
                 "nvmlDeviceSetFanSpeed_v2", "nvmlDeviceGetFanSpeed_v2",
                 "nvmlDeviceSetFanControlPolicy", "nvmlDeviceGetFanControlPolicy_v2",
                 "nvmlDeviceSetPowerManagementLimit", "nvmlDeviceGetPowerManagementLimit",
                 "nvmlDeviceGetClockInfo", "nvmlDeviceGetUtilizationRates",
                 "nvmlDeviceSetGpuLockedClocks", "nvmlDeviceSetMemoryLockedClocks"):
        print(f"  {name}: {hasattr(lib, name)}")
    if hasattr(lib, "nvmlDeviceGetFanControlPolicy_v2"):
        pol = ctypes.c_int(-1)
        lib.nvmlDeviceGetFanControlPolicy_v2(h, 0, ctypes.byref(pol))
        print("  текущая политика кулера:", pol.value, "(0 авто, 1 фикс)")
    if hasattr(lib, "nvmlDeviceGetFanSpeed_v2"):
        sp = ctypes.c_uint(0)
        lib.nvmlDeviceGetFanSpeed_v2(h, 0, ctypes.byref(sp))
        print("  обороты сейчас:", sp.value, "%")
    if hasattr(lib, "nvmlDeviceGetTemperatureThreshold"):
        for thr, label in ((0, "порог замедления"), (1, "критический"), (2, "аварийный"),
                           (3, "порог памяти")):
            v = ctypes.c_uint(0)
            rc = lib.nvmlDeviceGetTemperatureThreshold(h, ctypes.c_int(thr), ctypes.byref(v))
            print(f"  {label}: rc={rc} value={v.value}")
    # температура памяти GDDR7, если датчик есть
    if hasattr(lib, "nvmlDeviceGetMemoryInfo"):
        pass
    print("  nvmlDeviceGetMemtemp:", hasattr(lib, "nvmlDeviceGetMemtemp"))


def cpu() -> None:
    print("=== CPU ===")
    print(platform.processor())
    # частота через NtPowerInformation(ProcessorInformation=11)
    class ProcInfo(ctypes.Structure):
        _fields_ = [("CurrentMhz", wt.DWORD), ("MhzLimit", wt.DWORD),
                    ("MaxMhz", wt.DWORD), ("pad", wt.DWORD)]
    info = ProcInfo()
    rc = ctypes.windll.powrprof.CallNtPowerInformation(
        11, None, 0, ctypes.byref(info), ctypes.sizeof(info))
    print(f"  NtPowerInformation: rc={rc} current={info.CurrentMhz} MHz "
          f"max={info.MaxMhz} limit={info.MhzLimit}")
    # загрузка через GetSystemTimes
    idle, kernel, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                          ctypes.byref(user))
    print("  GetSystemTimes: ok")

    # температура: WMI ACPI thermal zone
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature "
             "| Select-Object -ExpandProperty CurrentTemperature"],
            capture_output=True, text=True, timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW)
        print("  WMI MSAcpi_ThermalZoneTemperature:", (ps.stdout or "").strip() or "нет данных",
              (ps.stderr or "").strip()[:120])
    except (OSError, subprocess.SubprocessError) as e:
        print("  WMI thermal zone: ошибка", e)
    # есть ли сторонний монитор
    for exe in ("HWiNFO64.exe", "LibreHardwareMonitor.exe", "OpenHardwareMonitor.exe"):
        found = subprocess.run(["where", exe], capture_output=True, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        print(f"  {exe}:", (found.stdout or "").strip() or "не найден")


if __name__ == "__main__":
    t0 = time.time()
    nvml()
    cpu()
    print(f"заняло {time.time() - t0:.2f} с")
