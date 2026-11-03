"""Проверка ограничителя кулера в изоляции: что он делает с синтетическим кадром."""
import os
import sys
import traceback

os.environ.setdefault("MINER_NO_AUTOSTART", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtWidgets import QApplication          # noqa: E402

from minerapp import gpu as gpu_mod                  # noqa: E402

app = QApplication(sys.argv)
from minerapp.ui import MainWindow                   # noqa: E402

w = MainWindow()
w._persist = lambda *a, **k: None
w.show()

print("spin_fan =", w.spin_fan.value(), "| spin_temp =", w.spin_temp.value())
print("policy до  =", gpu_mod.fan_policy(), "| _fan_ctrl =", w._fan_ctrl)

frame = {"gpu_running": True, "temp": 74.0, "fan": 76.0}
try:
    w._guard(dict(frame))
    print("_guard ok | policy =", gpu_mod.fan_policy(), "| _fan_ctrl =", w._fan_ctrl)
except Exception:
    print("_guard упал:")
    traceback.print_exc()

for i in range(3):
    w._fan_guard(dict(frame))
    print(f"повтор {i}: policy =", gpu_mod.fan_policy(), "| _fan_ctrl =", w._fan_ctrl)

print("console:")
for line in w.console_text().splitlines()[-8:] if hasattr(w, "console_text") else []:
    print("  ", line)
