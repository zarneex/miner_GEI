"""Разведка кулера: кто владеет, работает ли запись, держится ли значение.

python probe_fan.py            # прочитать состояние
python probe_fan.py 58         # попробовать зафиксировать 58 % и посмотреть, что выйдет
"""
import ctypes
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
NO_WINDOW = 0x08000000

lib = ctypes.WinDLL("nvml.dll")
assert lib.nvmlInit_v2() == 0
h = ctypes.c_void_p()
assert lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) == 0


def policy():
    p = ctypes.c_int(-1)
    rc = lib.nvmlDeviceGetFanControlPolicy_v2(h, 0, ctypes.byref(p))
    return f"rc={rc} policy={p.value}"


def fan():
    s = ctypes.c_uint(0)
    rc = lib.nvmlDeviceGetFanSpeed_v2(h, 0, ctypes.byref(s))
    rpm = ctypes.c_uint(0)
    rc2 = lib.nvmlDeviceGetFanSpeedRPM(h, 0, ctypes.byref(rpm))
    return f"rc={rc} fan={s.value}%  (rpm rc={rc2} {rpm.value})"


def smi():
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=fan.speed,temperature.gpu,power.draw",
         "--format=csv,noheader"],
        capture_output=True, text=True, creationflags=NO_WINDOW)
    return out.stdout.strip()


print("до :", policy(), "|", fan(), "| smi:", smi())

if len(sys.argv) > 1:
    pct = int(sys.argv[1])
    rc_pol = lib.nvmlDeviceSetFanControlPolicy(h, 0, 1)
    rc_sp = lib.nvmlDeviceSetFanSpeed_v2(h, 0, ctypes.c_uint(pct))
    print(f"запись {pct}%: policy rc={rc_pol} speed rc={rc_sp}")
    for i in range(1, 7):
        time.sleep(1.5)
        print(f"  +{i * 1.5:.1f}с:", policy(), "|", fan(), "| smi:", smi())
    lib.nvmlShutdown()
