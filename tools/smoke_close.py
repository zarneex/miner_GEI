#!/usr/bin/env python
"""Живой смоук закрытия: поднять окно, послать WM_CLOSE, замерить время выхода.

Запускается без майнинга (`MINER_NO_AUTOSTART=1`): проверяем, что крестик
не подвешивает окно и процесс уходит. Печатает секунды и код выхода.
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minerapp import paths  # noqa: E402

user32 = ctypes.windll.user32
EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
PYTHONW = Path(sys.executable).with_name("pythonw.exe")


def find_window(pid: int) -> int:
    hits: list[int] = []

    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if buf.value.startswith(paths.APP_TITLE):
            hits.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return hits[0] if hits else 0


def main() -> int:
    env = dict(os.environ, MINER_NO_AUTOSTART="1")
    env.pop("QT_QPA_PLATFORM", None)
    proc = subprocess.Popen([str(PYTHONW), str(PROJECT / "main.py")], cwd=str(PROJECT),
                            env=env, close_fds=True)
    hwnd = 0
    for _ in range(40):
        time.sleep(0.25)
        if proc.poll() is not None:
            print(f"FAIL: приложение упало до окна, код {proc.returncode}")
            return 1
        hwnd = find_window(proc.pid)
        if hwnd:
            break
    if not hwnd:
        print("FAIL: окно не появилось")
        proc.kill()
        return 1
    print(f"окно найдено (hwnd={hwnd}, pid={proc.pid})")

    t0 = time.time()
    user32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE
    while time.time() - t0 < 20 and proc.poll() is None:
        time.sleep(0.05)
    dt = time.time() - t0
    if proc.poll() is None:
        print(f"FAIL: окно живо через {dt:.1f} с — закрытие подвисло")
        proc.kill()
        return 1
    print(f"OK: закрылось за {dt:.2f} с, код {proc.returncode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
