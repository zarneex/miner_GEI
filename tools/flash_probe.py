"""Диагностика «пустой терминал мигает»: 20 с ловим появление окон консолей.

Печатает каждое новое консольное окно (класс/TITLE/pid) с точностью до 40 мс,
чтобы увидеть, с какой частотой и из какого процесса оно появляется.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

user32 = ctypes.windll.user32

CONSOLE_CLASSES = {
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",
    "PseudoConsoleWindow",
    "VirtualConsoleClass",
}

EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def windows() -> list[tuple[int, str, str, int, tuple]]:
    found = []

    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append((hwnd, cls.value, title.value, pid.value,
                      (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def main(seconds: float = 20.0) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    end = time.time() + seconds
    seen: set[int] = set()
    hits = 0
    t0 = time.time()
    while time.time() < end:
        for hwnd, cls, title, pid, rect in windows():
            if cls in CONSOLE_CLASSES and hwnd not in seen:
                seen.add(hwnd)
                hits += 1
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                print(f"{time.time() - t0:6.2f}s  {cls}  pid={pid}  "
                      f"«{title}»  {w}x{h} @ {rect[0]},{rect[1]}", flush=True)
        time.sleep(0.04)
    print(f"итого консольных окон за {seconds:.0f} с: {hits}", flush=True)


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 20.0)
