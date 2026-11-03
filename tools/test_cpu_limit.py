"""Тест Job-лимитера: реально ли ядро режет долю процессора в процентах.

Метод: отдельный python-процесс в одну нить жжёт ровно одно ядро (потоки Python
бесполезны — GIL). Меряем его CPU-время по GetProcessTimes за окно 4 с:
норма ~1.0 CPU-с/с; при 25 % ждём ~0.25, при 50 % ~0.5, при 100 % снова ~1.0.
После detach процесс обязан остаться жив.
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from minerapp.cpu_limit import CpuLimiter  # noqa: E402

BURN = r"""
import time
x = 0
end = time.time() + 120
while time.time() < end:
    for _ in range(50000):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
"""

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def cpu_seconds(pid: int) -> float:
    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        raise OSError(ctypes.get_last_error())
    try:
        c, e, k, u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                   ctypes.byref(k), ctypes.byref(u)):
            raise OSError(ctypes.get_last_error())
        return ((k.dwHighDateTime << 32) | k.dwLowDateTime) / 1e7 + \
               ((u.dwHighDateTime << 32) | u.dwLowDateTime) / 1e7
    finally:
        k32.CloseHandle(h)


def measure(pid: int, window: float = 4.0) -> float:
    a = cpu_seconds(pid)
    time.sleep(window)
    return (cpu_seconds(pid) - a) / window


def main() -> int:
    proc = subprocess.Popen([sys.executable, "-c", BURN],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fails = []
    try:
        time.sleep(1.0)
        lim = CpuLimiter()
        base = measure(proc.pid)
        print(f"без лимита: {base:.2f} CPU-с/с (ждали 0.9..1.05)")
        if not (0.85 <= base <= 1.1):
            fails.append("база")

        if not lim.attach(proc.pid, 2):
            print("attach не удался:", lim.last_error)
            return 1
        q2 = measure(proc.pid)
        print(f"лимит 2 % (0.32 ядра): {q2:.2f} CPU-с/с (ждали 0.18..0.45)")
        if not (0.15 <= q2 <= 0.50):
            fails.append("2%")

        if not lim.set(4):
            print("set(4) не удался:", lim.last_error)
            return 1
        q4 = measure(proc.pid)
        print(f"лимит 4 % (0.64 ядра): {q4:.2f} CPU-с/с (ждали 0.45..0.80)")
        if not (0.40 <= q4 <= 0.85):
            fails.append("4%")

        if not lim.set(100):
            print("set(100) не удался:", lim.last_error)
            return 1
        q100 = measure(proc.pid)
        print(f"лимит 100 %: {q100:.2f} CPU-с/с (ждали 0.9..1.05)")
        if not (0.85 <= q100 <= 1.1):
            fails.append("100%")

        if not lim.detach():
            fails.append("detach")
        alive = proc.poll() is None
        print(f"после detach процесс жив: {alive}, лимитер active={lim.active}")
        if not alive:
            fails.append("жив после detach")
        q_after = measure(proc.pid, 3.0)
        print(f"после detach: {q_after:.2f} CPU-с/с (ждали 0.9..1.05)")
        if not (0.85 <= q_after <= 1.1):
            fails.append("после detach")
    finally:
        proc.kill()
        proc.wait(timeout=10)

    print("PASS" if not fails else f"FAIL: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
