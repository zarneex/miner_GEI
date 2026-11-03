"""Тест вкладки второго ПК: окно не шире, контролы те же, команды уходят агенту.

Офскрин, без реального SSH и без майнинга: `SshAgent` подменяется заглушкой.
Конфиг копируется во временный MINER_ROOT, чтобы не трогать боевой.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["MINER_NO_AUTOSTART"] = "1"
ROOT = Path(tempfile.mkdtemp(prefix="miner-remote-test-"))
os.environ["MINER_ROOT"] = str(ROOT)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENGINE = Path(r"W:\mining\scripts\minerctl.py")
if not ENGINE.exists():
    shutil.rmtree(ROOT, ignore_errors=True)
    print("SKIP: нет W:\\mining\\scripts — тест персонального модуля второго ПК")
    sys.exit(0)
SRC = Path(r"W:\mining\config.json")
DST = ROOT / "config.json"
cfg = json.loads(SRC.read_text(encoding="utf-8"))
# в тестовую копию не тащим кошелёк и куку кабинета
cfg["kryptex_user"] = "test/rig"
cfg.setdefault("xmrig", {})["user"] = "test/rig"
cfg.setdefault("app", {}).setdefault("balance", {}).setdefault("headers", {})["Cookie"] = ""
cfg.setdefault("app", {})["cpu_remote"] = {
    "enabled": True, "host": "pc2-test",
    "python": r"C:\Python314\pythonw.exe",
    "script": r"C:\mining\agent\remote_cpu_agent.py",
    "label": "PC2 · i5-13500",
    "threads": 12, "threads_max": 20, "load": 100,
    "autostart": False, "hold_temp": False, "max_temp": 80.0, "allow_100": True,
}
DST.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
# приложению нужен движок: во временном MINER_ROOT кладём копию W:\mining\scripts
shutil.copytree(r"W:\mining\scripts", ROOT / "scripts",
                ignore=shutil.ignore_patterns("__pycache__"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from minerapp import remote_cpu as rc  # noqa: E402
from minerapp import ui as uimod  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)


class FakeAgent:
    """Заглушка SSH-агента: помнит команды, ведёт себя как живой XMRig."""

    last: "FakeAgent | None" = None

    def __init__(self, *a, **kw) -> None:
        FakeAgent.last = self
        self.calls: list[tuple[str, dict]] = []
        self.running = False
        self.temp = 66.0
        self.power = 49.0
        self.mhz = 3681
        self.hr = 2550.0
        self.threads = 12
        self.load = 100
        self.gov = {"active": False, "pct": 100, "target": 80.0, "msg": ""}

    def call(self, cmd: str, timeout: float = 20.0, **kw) -> dict:
        self.calls.append((cmd, dict(kw)))
        if cmd == "status":
            return {
                "ok": True, "running": self.running, "pid": 4242 if self.running else 0,
                "threads": self.threads, "threads_run": self.threads if self.running else 0,
                "load_pct": self.load, "hashrate": self.hr if self.running else None,
                "uptime": 77 if self.running else 0, "temp_c": self.temp,
                "power_w": self.power, "mhz": self.mhz, "load_sensor": 90,
                "bridge": True, "cpu_total": 20,
                "governor": dict(self.gov), "error": "",
            }
        if cmd == "start":
            self.running = True
            self.threads = int(kw.get("threads") or self.threads)
            return {"ok": True, "msg": f"{self.threads} потоков"}
        if cmd == "stop":
            self.running = False
            return {"ok": True, "msg": "остановлен"}
        if cmd == "threads":
            self.threads = int(kw.get("n") or self.threads)
            return {"ok": True, "msg": f"потоки -> {self.threads}"}
        if cmd == "load":
            self.load = int(kw.get("pct") or self.load)
            return {"ok": True, "msg": f"лимит {self.load} %"}
        if cmd == "governor":
            self.gov["active"] = bool(kw.get("active"))
            self.gov["target"] = float(kw.get("target") or 80)
            return {"ok": True, "msg": "гувернатор"}
        if cmd == "quit":
            return {"ok": True, "msg": "выхожу"}
        return {"ok": False, "msg": "?"}

    def close(self) -> None:
        pass


rc.SshAgent = FakeAgent
app = QApplication(sys.argv)
w = uimod.MainWindow()
w.show()


def pump(ms: int) -> None:
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


try:
    pump(1200)
    check("вкладки секции «Процессор»: две", w.cpu_tabs is not None and w.cpu_tabs.count() == 2,
          f"count={w.cpu_tabs.count() if w.cpu_tabs else 0}")
    check("подписи вкладок", [w.cpu_tabs.tabText(i) for i in range(2)] ==
          ["Этот ПК", "PC2 · i5-13500"], str([w.cpu_tabs.tabText(i) for i in range(2)]))
    check("окно не стало шире", w.width() == 1160 and w.minimumWidth() == 960,
          f"{w.width()}x{w.height()}, min {w.minimumWidth()}")
    check("локальная карточка на месте",
          all(hasattr(w, n) for n in ("chk_cpu", "spin_threads", "spin_load", "chk_hold",
                                      "spin_cpu_temp", "chk_boost", "lbl_cpu")))
    r = w.remote_cpu
    check("вкладка PC2 создана", r is not None)
    check("значения из конфига", (r.spin_threads.value(), r.spin_load.value(),
                                  r.chk_hold.isChecked()) == (12, 100, False),
          f"{r.spin_threads.value()}/{r.spin_load.value()}/{r.chk_hold.isChecked()}")
    check("живая строка после опроса", r.lbl_live.text() == "остановлен",
          r.lbl_live.text())

    r.chk_on.setChecked(True)
    pump(900)
    check("старт ушёл агенту", any(c == "start" for c, _ in FakeAgent.last.calls),
          str(FakeAgent.last.calls[-2:]))
    check("галочка «Вкл» осталась включённой", r.chk_on.isChecked() and r.running)
    check("live показывает работу", "kH/s" in r.lbl_live.text(), r.lbl_live.text())

    r.spin_threads.setValue(10)
    pump(1100)
    check("потоки на ходу", any(c == "threads" and kw.get("n") == 10
                                for c, kw in FakeAgent.last.calls),
          str([c for c, _ in FakeAgent.last.calls]))

    r.spin_load.setValue(80)
    pump(900)
    check("лимит % на ходу", any(c == "load" and kw.get("pct") == 80
                                for c, kw in FakeAgent.last.calls))

    r.chk_hold.setChecked(True)
    pump(700)
    check("гувернатор включён с целью", any(c == "governor" and kw.get("active")
                                           and kw.get("target") == 80.0
                                           for c, kw in FakeAgent.last.calls),
          str([(c, kw) for c, kw in FakeAgent.last.calls if c == "governor"][-2:]))
    check("цель и 100 % доступны при галочке",
          r.spin_temp.isEnabled() and r.chk_boost.isEnabled())

    r.chk_boost.setChecked(False)
    pump(500)
    check("снятый 100 % уходит гувернатору", any(
        c == "governor" and kw.get("allow100") is False
        for c, kw in FakeAgent.last.calls))

    r.chk_on.setChecked(False)
    pump(900)
    check("стоп ушёл агенту", any(c == "stop" for c, _ in FakeAgent.last.calls))
    check("после стопа не работает", not r.running and not r.chk_on.isChecked())

    shot = Path(r"C:\Users\Admin\DSH\майнинг\miner\notes\remote-cpu-tab.png")
    w.grab().save(str(shot))
    check("снимок сохранён", shot.exists() and shot.stat().st_size > 10000,
          f"{shot.stat().st_size if shot.exists() else 0} B")
finally:
    w._shutdown_started = True
    w._finish_shutdown()
    pump(300)
    shutil.rmtree(ROOT, ignore_errors=True)

print("\nВСЁ ПРОШЛО" if not FAILED else f"\nПРОВАЛЕНО: {len(FAILED)}")
sys.exit(1 if FAILED else 0)
