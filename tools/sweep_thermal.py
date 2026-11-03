"""Замеры: потоки CPU и пресеты GPU при кулере 58 % — что даёт максимум до 75 °C.

Запускать при выключенном GUI: скрипт сам гасит приложение, снимает задачу-чистильщик
на время замеров, поднимает мост датчиков CPU, гоняет точки и в конце возвращает всё
как было. Результат — `notes/thermal-<дата>.md` и JSONL рядом.

    python.exe tools\\sweep_thermal.py            # все точки
    python.exe tools\\sweep_thermal.py --cpu-only
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, r"W:\mining\scripts")

from minerapp import owner, paths  # noqa: E402
import minerctl                            # noqa: E402

NO_WINDOW = subprocess.CREATE_NO_WINDOW
XMRIG = paths.XMRIG
CPU_JSON = paths.CPU_JSON
XMR_PORT = 12002
TASK_CPU = "MinerCPUSensor"
TASK_CLEAN = "MinerGPUReset"
RESULT = PROJECT / "notes" / f"thermal-{time.strftime('%Y-%m-%d')}.md"
JSONL = PROJECT / "notes" / f"thermal-{time.strftime('%Y-%m-%d')}.jsonl"


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def run(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": str(e)})()


def stop_gui() -> None:
    pid = owner.marker_pid()
    if pid and owner.pid_alive(pid):
        log(f"останавливаю GUI pid={pid}")
        run(["taskkill", "/PID", str(pid), "/T", "/F"])
        time.sleep(2)
    for image in owner.STRAY_MINERS:
        run(["taskkill", "/IM", image, "/T", "/F"])
    time.sleep(2)
    owner.drop()


def gpu_state() -> dict:
    out = run(["nvidia-smi", "--query-gpu=power.draw,temperature.gpu,fan.speed,"
                                "clocks.sm,clocks.mem,power.limit",
               "--format=csv,noheader,nounits"]).stdout.strip()
    try:
        pw, temp, fan, sm, mem, pl = [float(x) for x in out.split(",")]
    except ValueError:
        return {}
    return {"power": pw, "temp": temp, "fan": fan, "sm": sm, "mem": mem, "plimit": pl}


def cpu_state() -> dict:
    try:
        return json.loads(CPU_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cpu_hashrate() -> float | None:
    """Хешрейт XMRig: у него нет `/2min`, зато есть `/json` и `/1m`/`/5m`."""
    for path in ("/json", "/1m", "/2min"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{XMR_PORT}{path}", timeout=4) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
        total = (data.get("hashrate") or {}).get("total")
        if isinstance(total, list) and total:
            return float(total[0])
        if isinstance(total, (int, float)):
            return float(total)
        if isinstance(data.get("hashrate"), (int, float)):
            return float(data["hashrate"])
    return None


def affinity(n: int, total: int = 16) -> str:
    return f"0x{((1 << n) - 1) << (total - n):X}"


def cpu_point(threads: int, minutes: float = 1.2) -> dict:
    cfg = minerctl.load_cfg()
    x = cfg["xmrig"]
    args = [str(XMRIG), "--coin", "monero", "-o", x["url"], "-u", x["user"], "-p", "x",
            "--tls", "--threads", str(threads), "--cpu-affinity", affinity(threads),
            "--cpu-priority", "1", "-k", "--no-color", "--http-port", str(XMR_PORT),
            "--http-host", "127.0.0.1", "--log-file", r"W:\mining\logs\xmr-sweep.log"]
    log(f"CPU {threads} потоков: старт")
    proc = subprocess.Popen(args, cwd=str(XMRIG.parent), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    time.sleep(75)
    hrs, temps, watts, mhz = [], [], [], []
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        hr = cpu_hashrate()
        st = cpu_state()
        if hr:
            hrs.append(hr)
        if st.get("temp_c"):
            temps.append(float(st["temp_c"]))
        if st.get("power_w"):
            watts.append(float(st["power_w"]))
        if st.get("mhz"):
            mhz.append(float(st["mhz"]))
        time.sleep(12)
    run(["taskkill", "/PID", str(proc.pid), "/T", "/F"])
    time.sleep(8)
    row = {"kind": "cpu", "threads": threads,
           "khs": round(sum(hrs) / len(hrs) / 1000, 2) if hrs else None,
           "temp_c": round(max(temps), 1) if temps else None,
           "power_w": round(sum(watts) / len(watts), 1) if watts else None,
           "mhz": round(sum(mhz) / len(mhz)) if mhz else None,
           "samples": len(hrs)}
    log(f"CPU {threads}: {row}")
    return row


def gpu_point(plimit: int, coffset: int, moffset: int, minutes: float = 2.0) -> dict:
    log(f"GPU {plimit} Вт, ядро {coffset:+d}, память {moffset:+d}, кулер 58%: старт")
    res = minerctl.probe("pearl", minutes,
                         {"plimit": plimit, "coffset": coffset, "moffset": moffset, "fan": 58})
    time.sleep(15)
    row = {"kind": "gpu", "plimit": plimit, "coffset": coffset, "moffset": moffset,
           "ths": round(res["hashrate"] / 1e12, 2) if res.get("hashrate") else None,
           "temp_c": res.get("temp_c_max"), "power_w": res.get("power_w")}
    log(f"GPU {plimit}/{coffset:+d}/{moffset:+d}: {row}")
    return row


def main() -> int:
    cpu_only = "--cpu-only" in sys.argv
    rows: list[dict] = []
    run(["schtasks", "/Change", "/TN", TASK_CLEAN, "/DISABLE"])
    stop_gui()
    run(["schtasks", "/Run", "/TN", TASK_CPU])     # мост датчиков CPU без окна
    time.sleep(14)
    try:
        for n in (12, 14, 16):
            rows.append(cpu_point(n))
        if not cpu_only:
            run(["schtasks", "/End", "/TN", TASK_CPU])
            time.sleep(2)
            for pl, co, mo in ((220, 0, 0), (220, 0, 1500), (220, 100, 1500), (240, 0, 1500)):
                rows.append(gpu_point(pl, co, mo))
    finally:
        run(["taskkill", "/IM", "xmrig.exe", "/T", "/F"])
        run(["taskkill", "/IM", "krig-miner.exe", "/T", "/F"])
        run(["schtasks", "/End", "/TN", TASK_CPU])
        run(["schtasks", "/Change", "/TN", TASK_CLEAN, "/ENABLE"])
        with JSONL.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        write_report(rows)
    return 0


def write_report(rows: list[dict]) -> None:
    cpu = [r for r in rows if r["kind"] == "cpu"]
    gpu = [r for r in rows if r["kind"] == "gpu"]
    lines = [f"# Замеры {time.strftime('%d.%m.%Y %H:%M')}", "",
             "Кулер GPU: 58 % (потолок приложения). CPU — один XMRig, без GPU-нагрузки.", ""]
    if cpu:
        lines += ["## Процессор (RandomX)", "",
                  "| Потоки | kH/s | °C | Вт | МГц |", "|---|---|---|---|---|"]
        lines += [f"| {r['threads']} | {r['khs']} | {r['temp_c']} | {r['power_w']} "
                  f"| {r['mhz']} |" for r in cpu]
        lines.append("")
    if gpu:
        lines += ["## Видеокарта (Pearl, кулер 58 %)", "",
                  "| Лимит, Вт | Ядро | Память | TH/s | °C | Вт |", "|---|---|---|---|---|---|"]
        lines += [f"| {r['plimit']} | {r['coffset']:+d} | {r['moffset']:+d} | {r['ths']} "
                  f"| {r['temp_c']} | {r['power_w']} |" for r in gpu]
        lines.append("")
    RESULT.write_text("\n".join(lines), encoding="utf-8")
    log(f"отчёт: {RESULT}")


if __name__ == "__main__":
    sys.exit(main())
