#!/usr/bin/env python
"""Перезапуск процесса сразу после остановки: Qt не должен отвергать start().

Регрессия, которую ловим: ограничитель CPU снял поток и перезапустил XMRig, но
`QProcess.start()` вызывался раньше, чем Qt заметил выход старого процесса —
новый не стартовал, раздел «Процессор» молча оставался выключенным.
Здесь вместо XMRig — ping: он живёт долго и не трогает железо.
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import engine  # noqa: E402

PING = Path(r"C:\Windows\System32\ping.exe")
ARGS = ["-n", "120", "127.0.0.1"]
ok = True


def out(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def check(name: str, good: bool) -> None:
    global ok
    out(f"{name}: {'PASS' if good else 'FAIL'}")
    ok = ok and good


def main() -> int:
    app = QApplication(sys.argv)
    m = engine._Miner("test")
    started: list[int] = []
    m.state_changed.connect(lambda on: started.append(1) if on else None)

    m._spawn(PING, ARGS, Path("C:/"))
    restarted = {"done": False}

    def step1() -> None:
        check("первый запуск работает", m.is_running())

        def after_stop() -> None:
            # то же место, где падал перезапуск: сразу start() после остановки
            m._spawn(PING, ARGS, Path("C:/"))
            restarted["done"] = True
            QTimer.singleShot(1500, step2)

        m.stop(quiet=True, on_done=after_stop)

    def step2() -> None:
        check("перезапуск сразу после остановки удался", restarted["done"] and m.is_running())
        check("pid новый, процесс живой", m.pid() > 0)

        def finish() -> None:
            m.stop(quiet=True)
            QTimer.singleShot(600, app.quit)

        finish()

    QTimer.singleShot(700, step1)
    QTimer.singleShot(16000, app.quit)    # страховка: тест не висит
    app.exec()
    out(f"ИТОГ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
