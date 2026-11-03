"""Агент CPU-майнинга для второго ПК: управление XMRig по stdio JSON.

Запускается с этого ПК по SSH (отдельного порта и службы не нужно):

    ssh pc2 "C:\\...\\python.exe C:\\mining\\agent\\remote_cpu_agent.py --stdio"

На каждый запрос-строку печатает одну строку-ответ. Умеет ровно то же, что
локальная карточка «Процессор»: старт/стоп XMRig, потоки на ходу через HTTP API,
доля CPU через Job Object, термогувернатор (`ThermalGovernor` — тот же код,
что в приложении) и датчики LibreHardwareMonitor.

Файлы-соседи (копируются рядом): `governor.py`, `xmrig_api.py`, `cpu_limit.py`.
Всё остальное — XMRig и LHM — лежит в C:\\mining (см. tools/deploy_remote_cpu.py).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
XMRIG = ROOT / "xmrig" / "xmrig.exe"
CFG = ROOT / "config.json"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
CPU_JSON = DATA / "cpu.json"
BRIDGE = ROOT / "tools" / "lhmd.ps1"
HTTP_PORT = 12002
NO_WINDOW = 0x08000000
BRIDGE_TTL = 15.0

sys.path.insert(0, str(HERE))
import xmrig_api  # noqa: E402
xmrig_api.TIMEOUT = 1.2          # статус не должен ждать 4 с, если API молчит

from xmrig_api import XmrigApi  # noqa: E402
from cpu_limit import CpuLimiter  # noqa: E402
from governor import ThermalGovernor  # noqa: E402


def _mask(threads: int) -> int:
    """Младшие логические CPU: на Intel 12+ это P-ядра, E-ядра идут последними."""
    n = max(1, min(int(threads), os.cpu_count() or 1))
    return (1 << n) - 1


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


class Agent:
    def __init__(self) -> None:
        LOGS.mkdir(parents=True, exist_ok=True)
        DATA.mkdir(parents=True, exist_ok=True)
        self.cfg = _read_json(CFG)
        self.proc: subprocess.Popen | None = None
        self.api: XmrigApi | None = None
        self.limiter = CpuLimiter(kill_on_close=True)
        self.threads = 0
        self.load_pct = 100
        self.hold = False
        self.target = 80.0
        self.allow100 = True
        self._lock = threading.RLock()          # процесс/потоки/доля
        self._gov_lock = threading.Lock()       # гувернатор и powercfg
        self._stop = threading.Event()
        self._last_error = ""
        self._bridge: subprocess.Popen | None = None
        self._bridge_at = 0.0
        self._gov_msg = ""
        self._gov_last_msg = ""
        self.gov = ThermalGovernor(DATA, on_msg=self._note_gov)
        self._startup_msg = self.gov.restore_backup()
        self._start_bridge()
        threading.Thread(target=self._gov_loop, name="gov", daemon=True).start()

    # --- служебное ---------------------------------------------------------
    def _log(self, msg: str) -> None:
        try:
            with open(LOGS / "agent.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except OSError:
            pass

    def _note_gov(self, msg: str) -> None:
        self._gov_last_msg = str(msg)
        self._log(f"gubernator: {msg}")

    def _running(self) -> bool:
        return bool(self.proc is not None and self.proc.poll() is None)

    # --- мост датчиков -----------------------------------------------------
    def _start_bridge(self) -> None:
        if not BRIDGE.exists():
            self._log(f"нет моста датчиков: {BRIDGE}")
            return
        try:
            self._bridge = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-WindowStyle", "Hidden", "-File", str(BRIDGE), "-Out", str(CPU_JSON)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            self._bridge_at = time.time()
            self._log("мост LHM запущен")
        except OSError as e:
            self._log(f"мост LHM не стартовал: {e}")

    def _bridge_watchdog(self) -> None:
        """Мост мог упасть (драйвер LHM, обновление) — поднимаем заново."""
        try:
            mt = CPU_JSON.stat().st_mtime
        except OSError:
            mt = 0.0
        alive = bool(self._bridge and self._bridge.poll() is None)
        fresh = time.time() - mt < BRIDGE_TTL
        if alive and fresh:
            return
        if time.time() - max(mt, self._bridge_at) < 20.0:
            return
        if self._bridge and self._bridge.poll() is None:
            self._bridge.kill()
        self._start_bridge()

    def _sensors(self) -> dict:
        d = _read_json(CPU_JSON)
        try:
            fresh = time.time() - float(d.get("ts") or 0) < BRIDGE_TTL
        except (TypeError, ValueError):
            fresh = False
        return d if fresh else {}

    # --- гувернатор --------------------------------------------------------
    def _gov_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._gov_lock:
                    running = self._running()
                    temp = self._sensors().get("temp_c")
                    if self.hold and running:
                        self.gov.tick(temp, self.threads or 1, self.target, True)
                    elif self.gov.active:
                        self.gov.tick(temp, self.threads or 1, self.target, False)
            except Exception as e:      # noqa: BLE001 — цикл не должен умирать
                self._log(f"гувернатор: {e!r}")
            self._stop.wait(2.0)

    def set_governor(self, active: bool, target: float, allow100: bool) -> str:
        with self._gov_lock:
            if bool(allow100) != self.allow100:
                self.allow100 = bool(allow100)
                self.gov.set_allow_100(self.allow100)
            if float(target) != self.target:
                self.target = float(target)
                self.gov.on_target_changed(self.target, self.threads or 1)
            self.hold = bool(active)
            if not self.hold:
                self.gov.tick(None, self.threads or 1, self.target, False)
        return ("гувернатор включён" if self.hold else "гувернатор выключен")

    # --- XMRig -------------------------------------------------------------
    def start(self, threads: int) -> str:
        with self._lock:
            if self._running():
                return "уже работает"
            x = (self.cfg.get("xmrig") or {})
            if not x.get("url") or not x.get("user"):
                raise RuntimeError(f"в {CFG} нет xmrig.url/xmrig.user")
            n = max(2, min(int(threads), os.cpu_count() or 2))
            mask = _mask(n)
            token = secrets.token_hex(8)
            args = [str(XMRIG), "--coin", "monero", "-o", str(x["url"]),
                    "-u", str(x["user"]), "-p", "x", "--tls",
                    "--threads", str(n), "--cpu-affinity", f"0x{mask:X}",
                    "--cpu-priority", "1", "-k", "--no-color",
                    "--http-port", str(HTTP_PORT), "--http-host", "127.0.0.1",
                    "--http-access-token", token, "--http-no-restricted",
                    "--log-file", str(LOGS / "xmr-agent.log")]
            self.proc = subprocess.Popen(
                args, cwd=str(XMRIG.parent), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW)
            self.api = XmrigApi(HTTP_PORT, token)
            self.threads = n
            if not self.limiter.attach(self.proc.pid, self.load_pct):
                self._log(f"Job Object не сел: {self.limiter.last_error}")
            self._log(f"XMRig стартовал: pid={self.proc.pid}, {n} потоков, маска 0x{mask:X}")
            return f"{n} потоков, маска 0x{mask:X}"

    def stop(self) -> str:
        with self._lock:
            p = self.proc
            if p is None:
                self.limiter.close()
                return "уже остановлен"
            try:
                p.kill()
                p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.proc = None
            self.api = None
            self.limiter.close()
            self.threads = 0
            with self._gov_lock:
                self.gov.tick(None, 1, self.target, False)
            self._log("XMRig остановлен")
            return "остановлен"

    def set_threads(self, n: int) -> str:
        with self._lock:
            n = max(2, min(int(n), os.cpu_count() or 2))
            if not self._running() or self.api is None:
                self.threads = n
                return f"запомнено {n} — применю при старте"
            if not self.api.set_threads(n, _mask(n)):
                raise RuntimeError(self.api.last_error or "API не поменял потоки")
            self.threads = n
            return f"потоки -> {n} без перезапуска"

    def set_load(self, pct: int) -> str:
        with self._lock:
            self.load_pct = max(20, min(100, int(pct)))
            if self._running() and self.limiter.active:
                if not self.limiter.set(self.load_pct):
                    raise RuntimeError(self.limiter.last_error)
                return f"лимит {self.load_pct} %"
            return f"запомнено {self.load_pct} %"

    # --- статус ------------------------------------------------------------
    def status(self) -> dict:
        self._bridge_watchdog()
        running = self._running()
        hashrate = uptime = None
        run_threads = 0
        if running and self.api is not None:
            hashrate = self.api.hashrate()
            uptime = self.api.uptime()
            run_threads = self.api.cpu_threads()
        s = self._sensors()
        return {
            "ok": True,
            "running": running,
            "pid": self.proc.pid if running and self.proc else 0,
            "threads": self.threads,
            "threads_run": run_threads or (self.threads if running else 0),
            "load_pct": self.load_pct,
            "hashrate": hashrate,
            "uptime": uptime or 0,
            "temp_c": s.get("temp_c"),
            "power_w": s.get("power_w"),
            "mhz": s.get("mhz"),
            "load_sensor": s.get("load_pct"),
            "bridge": bool(s),
            "cpu_total": os.cpu_count() or 0,
            "cpu_name": os.environ.get("PROCESSOR_IDENTIFIER", ""),
            "governor": {"active": bool(self.gov.active), "pct": int(self.gov.pct),
                         "target": float(self.target), "msg": self._gov_last_msg},
            "startup": self._startup_msg,
            "error": self._last_error,
        }

    # --- остановка ---------------------------------------------------------
    def shutdown(self) -> None:
        self._stop.set()
        try:
            self.stop()
        except Exception:              # noqa: BLE001
            pass
        try:
            with self._gov_lock:
                self.gov.stop()
        except Exception:              # noqa: BLE001
            pass
        if self._bridge and self._bridge.poll() is None:
            try:
                self._bridge.kill()
            except OSError:
                pass
        self._log("агент остановлен")

    # --- протокол ----------------------------------------------------------
    def handle(self, req: dict) -> dict:
        cmd = str(req.get("cmd") or "")
        self._last_error = ""
        try:
            if cmd == "status":
                return self.status()
            if cmd == "start":
                return {"ok": True, "msg": self.start(int(req.get("threads") or 8))}
            if cmd == "stop":
                return {"ok": True, "msg": self.stop()}
            if cmd == "threads":
                return {"ok": True, "msg": self.set_threads(int(req.get("n") or 8))}
            if cmd == "load":
                return {"ok": True, "msg": self.set_load(int(req.get("pct") or 100))}
            if cmd == "governor":
                return {"ok": True, "msg": self.set_governor(
                    bool(req.get("active")), float(req.get("target") or 80.0),
                    bool(req.get("allow100", True)))}
            if cmd == "restore_power":
                return {"ok": True, "msg": self.gov.restore_backup() or "нечего возвращать"}
            if cmd == "quit":
                return {"ok": True, "msg": "выхожу"}
            return {"ok": False, "msg": f"неизвестная команда: {cmd}"}
        except Exception as e:          # noqa: BLE001 — ответ должен быть всегда
            self._last_error = f"{cmd}: {e}"
            self._log("ошибка: " + traceback.format_exc(limit=3))
            return {"ok": False, "msg": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdio", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--wait-sensors", type=float, default=8.0)
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, OSError):
        pass
    agent = Agent()
    if a.selftest:
        deadline = time.time() + a.wait_sensors
        while time.time() < deadline and not agent._sensors():
            time.sleep(0.5)
        print(json.dumps(agent.status(), ensure_ascii=True), flush=True)
        agent.shutdown()
        return 0
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                print(json.dumps({"ok": False, "msg": "плохой JSON"}, ensure_ascii=True),
                      flush=True)
                continue
            resp = agent.handle(req)
            resp["id"] = req.get("id")
            print(json.dumps(resp, ensure_ascii=True), flush=True)
            if req.get("cmd") == "quit":
                break
    finally:
        agent.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
