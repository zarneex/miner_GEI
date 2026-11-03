"""Мониторы: гасим по команде Windows, будит любая клавиша.

`SC_MONITORPOWER` переводит дисплеи в сон, но сама ОС продолжает работать —
майнинг и таймеры не останавливаются. `SetThreadExecutionState` монитор не
гасит (он лишь запрещает сон системы), поэтому используется WM_SYSCOMMAND.

Проверено на Win 11 26200: клавиатура и мышь стоят в `powercfg /devicequery
wake_armed`, пробуждение по любой клавише работает.
"""
import ctypes
from ctypes import wintypes

HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
_MONITOR_OFF = 2
_MONITOR_ON = -1


def _broadcast(lparam: int) -> bool:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM)
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, lparam)
        return True
    except (AttributeError, OSError):
        return False


def monitors_off() -> bool:
    """Погасить все мониторы (система продолжает работать)."""
    return _broadcast(_MONITOR_OFF)


def monitors_on() -> bool:
    """Включить мониторы обратно (на случай выключения галочки в темноте)."""
    return _broadcast(_MONITOR_ON)
