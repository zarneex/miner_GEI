"""Замер: сколько GPU теряет, когда CPU-майнинг занимает 14 потоков.

Читает хешрейт krig через его HTTP API, гасит CPU-майнинг кликом по чекбоксу,
ждёт восстановления и возвращает всё как было.
"""
import ctypes
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"W:\mining\scripts")
import minerctl  # noqa: E402

user32 = ctypes.windll.user32
WINDOW = (60, 60)          # окно приложения после переноса
CPU_CHECKBOX = (612, 112)  # координаты чекбокса «Вкл» в разделе «Процессор»


def hr() -> str:
    try:
        value = minerctl.api_hashrate(12000)
    except Exception as exc:  # noqa: BLE001
        return f"нет ответа ({exc})"
    return f"{value / 1e12:.1f} TH/s" if value else "—"


def click(pt: tuple[int, int]) -> None:
    x, y = WINDOW[0] + pt[0], WINDOW[1] + pt[1]
    user32.SetCursorPos(x, y)
    time.sleep(0.3)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.5)


print("1) CPU-майнинг включён:", hr(), flush=True)
time.sleep(10)
print("   ещё раз:", hr(), flush=True)

click(CPU_CHECKBOX)
print("2) CPU выключен, ждём восстановления GPU…", flush=True)
for i in range(6):
    time.sleep(15)
    print(f"   +{(i + 1) * 15:>3} с: {hr()}", flush=True)

click(CPU_CHECKBOX)
print("3) CPU включён обратно, ждём 45 с…", flush=True)
time.sleep(45)
print("   итог:", hr(), flush=True)
