"""Диагностика подсказок: сколько живёт QToolTip, если просто держать курсор.

Аргументы: x y [секунды] — точка в координатах окна приложения.
Скрипт наводит курсор, ждёт и печатает таймлайн появления/исчезновения
всплывающих окон приложения (класс QTipLabel*), затем возвращает курсор назад.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

user32 = ctypes.windll.user32
EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
TITLE = "Майнинг — панель"


def app_window() -> int:
    found = []

    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if buf.value == TITLE:
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else 0


def pid_of(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def tips(window_pid: int) -> dict[int, str]:
    """Видимые всплывающие окна приложения: hwnd -> описание."""
    out = {}

    def cb(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != window_pid or not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "TipLabel" not in cls.value and "ToolTip" not in cls.value:
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        out[hwnd] = f"{cls.value} {rect.right - rect.left}x{rect.bottom - rect.top}"
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


def main(x: int, y: int, seconds: float = 10.0) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    hwnd = app_window()
    if not hwnd:
        print("окно не найдено")
        return
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    px, py = rect.left + x, rect.top + y
    old = wt.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    # окно может быть под браузером: SetForegroundWindow блокируется,
    # а без видимого окна подсказки не проверить — поднимаем поверх всех
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
    HWND_TOPMOST = -1
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    time.sleep(0.6)
    user32.SetCursorPos(px, py)
    print(f"курсор {px},{py} (окно {rect.left},{rect.top})", flush=True)

    start = time.time()
    alive: dict[int, str] = {}
    appeared = disappeared = 0
    while time.time() - start < seconds:
        now = tips(pid_of(hwnd))
        for h, d in now.items():
            if h not in alive:
                appeared += 1
                print(f"{time.time() - start:5.1f}s  появилась {d}", flush=True)
        for h, d in alive.items():
            if h not in now:
                disappeared += 1
                print(f"{time.time() - start:5.1f}s  исчезла   {d}", flush=True)
        alive = now
        time.sleep(0.1)
    user32.SetCursorPos(old.x, old.y)
    print(f"итого: появилось {appeared}, исчезло {disappeared}", flush=True)


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]) if len(sys.argv) > 3 else 10.0)
