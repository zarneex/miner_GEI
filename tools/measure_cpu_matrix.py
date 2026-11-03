"""Матрица замеров CPU: (потоки, потолок частоты, доля нагрузки, MSR) -> T, Вт, kH/s.

Зачем отдельный стенд: приложение занято своим XMRig, а для чистого замера нужен
один майнер, выключенный GPU и управляемый Job-лимит. Стенд поднимает мост датчиков
(LibreHardwareMonitor), гоняет по одному кейсу, пишет установившиеся значения и
возвращает схему питания как была.

Запускать ТОЛЬКО когда GUI-приложение закрыто и krig/xmrig не крутятся:
иначе два майнера искажают и температуру, и хешрейт.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

MINER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MINER))
sys.stdout.reconfigure(encoding="utf-8")

from minerapp import governor, paths  # noqa: E402
from minerapp.cpu_limit import CpuLimiter  # noqa: E402
from minerapp.xmrig_api import XmrigApi  # noqa: E402

POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
LHM = paths.ROOT / "tools" / "lhmd.ps1"
CPU_JSON = paths.CPU_JSON
PORT = 12004
TOKEN = "measure-token"
POOL = "xmr-ru.kryptex.network:8029"
# логин берём из общего конфига: в публичном коде своего кошелька не держим
try:
    USER = str(json.loads(paths.CFG.read_text(encoding="utf-8"))["kryptex_user"])
except (OSError, KeyError, ValueError):
    USER = ""


def mask(n: int, total: int = paths.CPU_TOTAL_THREADS) -> int:
    return ((1 << n) - 1) << (total - n)


def ensure_no_miners() -> None:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq xmrig.exe", "/NH"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    if "xmrig" in out.lower():
        raise SystemExit("есть запущенный xmrig — сначала закройте приложение и GPU-майнер")
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq krig-miner.exe", "/NH"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    if "krig" in out.lower():
        raise SystemExit("есть запущенный krig — сначала остановите GPU-майнинг")


def start_bridge() -> subprocess.Popen | None:
    age = 999
    if CPU_JSON.exists():
        age = (time.time() - CPU_JSON.stat().st_mtime)
    if age < 3:
        print("мост датчиков уже живой")
        return None
    p = subprocess.Popen([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-WindowStyle",
                          "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(LHM)],
                         cwd=str(LHM.parent), creationflags=0x08000000,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"мост датчиков запущен, pid={p.pid}")
    for _ in range(40):
        time.sleep(0.5)
        if CPU_JSON.exists() and time.time() - CPU_JSON.stat().st_mtime < 3:
            return p
    print("ВНИМАНИЕ: мост не пишет cpu.json — температура будет пустой")
    return p


def read_cpu() -> dict:
    try:
        return json.loads(CPU_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def start_xmrig(threads: int, no_msr: bool) -> subprocess.Popen:
    args = [str(paths.XMRIG), "--coin", "monero", "-o", POOL, "-u", USER, "-p", "x",
            "--tls", "--threads", str(threads), "--cpu-affinity", f"0x{mask(threads):X}",
            "--cpu-priority", "1", "-k", "--no-color",
            "--http-port", str(PORT), "--http-host", "127.0.0.1",
            "--http-access-token", TOKEN, "--http-no-restricted",
            "--log-file", str(paths.LOGS / "measure-xmr.log")]
    if no_msr:
        args.append("--no-msr")
    return subprocess.Popen(args, cwd=str(paths.XMRIG.parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def msr_state() -> str:
    try:
        text = (paths.LOGS / "measure-xmr.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "?"
    if "FAILED TO APPLY MSR MOD" in text:
        return "no"
    if "msr      detected" in text or "MSR" in text:
        return "yes"
    return "?"


def run_case(threads: int, pct: int, load: int, seconds: int, settle: int,
             no_msr: bool) -> dict:
    (paths.LOGS / "measure-xmr.log").unlink(missing_ok=True)
    proc = start_xmrig(threads, no_msr)
    api = XmrigApi(PORT, TOKEN)
    lim = CpuLimiter()
    try:
        hr = None
        for _ in range(90):
            time.sleep(1)
            hr = api.hashrate()
            if hr and hr > 0 and api.cpu_threads() == threads:
                break
        governor.set_power(5, pct)
        if load < 100:
            ok = lim.attach(proc.pid, load)
            if not ok:
                print(f"  лимит не применился: {lim.last_error}")
        print(f"  старт: потоков={api.cpu_threads()}, MSR={msr_state()}")
        time.sleep(settle)
        temps, watts, mhz, hrs = [], [], [], []
        t0 = time.time()
        while time.time() - t0 < seconds:
            time.sleep(5)
            c = read_cpu()
            if c.get("temp_c") is not None:
                temps.append(float(c["temp_c"]))
            if c.get("power_w") is not None:
                watts.append(float(c["power_w"]))
            if c.get("mhz") is not None:
                mhz.append(float(c["mhz"]))
            h = api.hashrate()
            if h:
                hrs.append(h)
        return {
            "threads": threads, "pct": pct, "load": load, "msr": msr_state(),
            "temp": statistics.median(temps) if temps else None,
            "watt": statistics.median(watts) if watts else None,
            "mhz": statistics.median(mhz) if mhz else None,
            "khs": round(statistics.median(hrs) / 1000, 2) if hrs else None,
            "n": len(temps),
        }
    finally:
        lim.close()
        proc.kill()
        proc.wait(timeout=15)
        time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=100)
    ap.add_argument("--settle", type=int, default=40)
    ap.add_argument("--cases", type=str, default="",
                    help='например "14:99:100,14:100:100,14:100:70"')
    args = ap.parse_args()
    cases = [(14, 99, 100), (14, 100, 100), (14, 100, 70),
             (12, 99, 100), (14, 99, 100, "nomsr")]
    if args.cases:
        cases = []
        for part in args.cases.split(","):
            bits = part.split(":")
            case = (int(bits[0]), int(bits[1]), int(bits[2]))
            if len(bits) > 3 and bits[3] == "nomsr":
                case = case + ("nomsr",)
            cases.append(case)

    ensure_no_miners()
    orig = governor.read_scheme()
    print(f"исходная схема: {orig['guid'][:8]} min={orig['min']} max={orig['max']}")
    bridge = start_bridge()
    (paths.LOGS / "measure-xmr.log").unlink(missing_ok=True)
    rows = []
    try:
        for case in cases:
            threads, pct, load = case[0], case[1], case[2]
            no_msr = len(case) > 3 and case[3] == "nomsr"
            print(f"\n=== потоки={threads} потолок={pct}% нагрузка={load}% "
                  f"{'без MSR' if no_msr else 'с MSR'} ===")
            row = run_case(threads, pct, load, args.seconds, args.settle, no_msr)
            rows.append(row)
            print(f"  -> T={row['temp']} °C, {row['watt']} Вт, {row['mhz']} МГц, "
                  f"{row['khs']} kH/s (точек {row['n']})")
    finally:
        lim = CpuLimiter()
        lim.close()
        subprocess.run(["taskkill", "/IM", "xmrig.exe", "/F"], capture_output=True,
                       creationflags=0x08000000)
        if orig.get("min") is not None and orig.get("max") is not None:
            governor.set_power(int(orig["min"]), int(orig["max"]))
            print(f"схема возвращена: {orig['min']}/{orig['max']} %")
        if bridge:
            subprocess.run(["taskkill", "/PID", str(bridge.pid), "/T", "/F"],
                           capture_output=True, creationflags=0x08000000)

    out = paths.ROOT / "data" / "cpu-matrix.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nитог: {out}")
    for r in rows:
        print(f"{r['threads']:>2} потоков | {r['pct']:>3} % | нагрузка {r['load']:>3} % | "
              f"MSR {r['msr']:>3} | {str(r['temp']):>5} °C | {str(r['watt']):>6} Вт | "
              f"{str(r['mhz']):>6} МГц | {str(r['khs']):>5} kH/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
