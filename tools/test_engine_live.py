"""Живой тест движка: потоки через API и лимит нагрузки через Job Object.

Поднимает настоящий XMRig через `engine.CpuMiner` (как приложение), проверяет:
  * старт и появление хешрейта;
  * лимит доли CPU применён (Job Object активен);
  * `set_threads(12)` меняет потоки БЕЗ перезапуска: uptime процесса растёт,
    в логе появляется новая строка `READY threads 12/12`;
  * стоп снимает лимит и гасит процесс.

Запускать при выключенном GUI (иначе два XMRig на одном порту API).
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from minerapp import engine  # noqa: E402
from minerapp.xmrig_api import XmrigApi  # noqa: E402

FAILS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


def pump(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.05)


def main() -> int:
    app = QCoreApplication([])
    cpu = engine.CpuMiner()
    cpu.load_pct = 70
    api = XmrigApi(engine.XMRIG_HTTP_PORT, "")
    try:
        cpu.start(14)
        for _ in range(400):
            pump(0.1)
            if cpu.is_running() and cpu.pid():
                break
        check("XMRig запустился", cpu.is_running() and cpu.pid() > 0, f"pid={cpu.pid()}")

        api.token = cpu._token
        hr = 0.0
        for _ in range(120):
            pump(1.0)
            hr = api.hashrate() or 0.0
            if hr > 0:
                break
        check("хешрейт пошёл", hr > 0, f"{hr/1000:.2f} kH/s")

        check("Job-лимит 70 % применён", cpu.limiter.active and cpu.limiter.pct == 70,
              f"active={cpu.limiter.active}, pct={cpu.limiter.pct}, err={cpu.limiter.last_error}")

        up1 = api.uptime()
        n1 = api.cpu_threads()
        check("стартовал на 14 потоках", n1 == 14, f"{n1}")

        ok = cpu.set_threads(12)
        check("set_threads(12) вернул True", ok)
        pump(4.0)
        n2 = api.cpu_threads()
        up2 = api.uptime()
        check("потоков стало 12", n2 == 12, f"{n2}")
        check("процесс не перезапускался (uptime растёт)", up2 >= up1, f"{up1} -> {up2}")

        text = (Path(r"W:\mining\logs") / "xmr-gui.log").read_text(encoding="utf-8", errors="replace")
        ready12 = [ln for ln in text.splitlines() if "READY threads 12/12" in ln]
        check("в логе есть READY threads 12/12", bool(ready12), ready12[-1][:80] if ready12 else "")

        cpu.set_load(100)
        pump(0.5)
        check("set_load(100) снял лимит", not cpu.limiter.active)

        cpu.set_load(50)
        pump(0.5)
        check("set_load(50) снова поставил лимит", cpu.limiter.active and cpu.limiter.pct == 50,
              f"pct={cpu.limiter.pct}")
    finally:
        cpu.stop(wait=True)
        pump(0.5)

    check("после стопа лимит снят", not cpu.limiter.active)
    check("после стопа процесс не жив", not cpu.is_running())
    print("\n" + ("ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЫ: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
