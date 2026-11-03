#!/usr/bin/env python
"""Управление GPU-майнером Krig (Pearl PRL + Quantus QTC через пулы Kryptex).

Подкоманды:
  start <coin>            запустить майнер на монете
  stop                    остановить майнер и сбросить разгон
  status                  жив ли процесс, что майнит
  api                     сырой ответ HTTP-API майнера (хешрейт)
  probe <coin> [--minutes N] [--plimit W] [--coffset M] [--moffset M]
                          замер: запустить, дождаться стабилизации, усреднить
                          хешрейт и потребление, погасить, вернуть результат
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Встроенная копия движка (оригинал — W:\mining\scripts\minerctl.py). Корень
# данных один на всё приложение и переопределяется через MINER_ROOT.
ROOT = Path(os.environ.get("MINER_ROOT") or r"W:\mining")
BIN = ROOT / "krig" / "krig-miner.exe"
CFG = ROOT / "config.json"
LOGS = ROOT / "logs"
STATE = ROOT / "state.json"
NVSMI = Path(r"C:\Windows\System32\nvidia-smi.exe")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_cfg() -> dict:
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def save_cfg(cfg: dict) -> None:
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def build_cmd(coin: str, cfg: dict, preset: dict | None = None) -> list[str]:
    c = cfg["coins"][coin]
    p = preset or c["preset"]
    cmd = [
        str(BIN),
        "--coin", coin,
        "--url", c["url"],
        "--user", cfg["kryptex_user"],
        "--no-tui",
        "--api-port", str(c["api_port"]),
        "--api-host", "127.0.0.1",
    ]
    if p.get("plimit"):
        cmd += ["--gpu-plimit", str(p["plimit"])]
    if p.get("coffset"):
        cmd += ["--gpu-coffset", str(p["coffset"])]
    if p.get("moffset"):
        cmd += ["--gpu-moffset", str(p["moffset"])]
    if p.get("fan"):
        cmd += ["--gpu-fan", str(p["fan"])]
    return cmd


def read_state() -> dict:
    if STATE.exists():
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def write_state(st: dict) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return str(pid) in out
    except Exception:
        return False


def stop() -> bool:
    st = read_state()
    pid = st.get("pid")
    if pid and pid_alive(pid):
        # сначала мягко: майнер сам сбрасывает разгон при выходе
        subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                       capture_output=True, text=True, timeout=30)
        for _ in range(20):
            if not pid_alive(pid):
                break
            time.sleep(0.5)
        if pid_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=30)
            for _ in range(20):
                if not pid_alive(pid):
                    break
                time.sleep(0.5)
    # на всякий случай снимаем оставшиеся блокировки клоков и лимит мощности
    subprocess.run([str(NVSMI), "--reset-gpu-clocks"], capture_output=True, text=True, timeout=30)
    subprocess.run([str(NVSMI), "-pl", "250"], capture_output=True, text=True, timeout=30)
    write_state({})
    return True


def start(coin: str, preset: dict | None = None) -> int:
    cfg = load_cfg()
    if coin not in cfg["coins"]:
        raise SystemExit(f"неизвестная монета: {coin}")
    stop()
    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    logfile = LOGS / f"{coin}-{stamp}.log"
    cmd = build_cmd(coin, cfg, preset) + ["--log-file", str(logfile)]
    print("run:", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, cwd=str(BIN.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    write_state({"pid": proc.pid, "coin": coin, "started": time.time(),
                 "log": str(logfile), "preset": preset or cfg["coins"][coin]["preset"]})
    return proc.pid


def nv(query: str) -> str:
    try:
        out = subprocess.run(
            [str(NVSMI), f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return out.splitlines()[0].strip() if out else ""
    except Exception:
        return ""


def api_raw(port: int) -> str:
    for path in ("/", "/api", "/summary"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            continue
    return ""


def api_hashrate(port: int) -> float | None:
    """Достаёт суммарный хешрейт из ответа API, не зная точной схемы."""
    raw = api_raw(port)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r'"(?:total_hashrate|hashrate|totalHashrate)"\s*:\s*([0-9.]+)', raw)
        return float(m.group(1)) if m else None
    found: list[float] = []

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, (int, float)):
            if re.search(r"hashrate|speed|hs$|rate", key, re.I):
                found.append(float(node))

    walk(data)
    return max(found) if found else None


def probe(coin: str, minutes: float, overrides: dict) -> dict:
    cfg = load_cfg()
    preset = dict(cfg["coins"][coin]["preset"])
    preset.update({k: v for k, v in overrides.items() if v is not None})
    port = cfg["coins"][coin]["api_port"]

    print(f"[probe] {coin}: {preset}")
    start(coin, preset)
    print("[probe] ждём стабилизации 150 с...")
    time.sleep(150)

    rates, powers, temps = [], [], []
    deadline = time.time() + minutes * 60
    raw_saved = False
    while time.time() < deadline:
        if not raw_saved:
            raw = api_raw(port)
            if raw:
                out = LOGS / f"api-{coin}.json"
                out.write_text(raw, encoding="utf-8")
                print(f"[probe] ответ API сохранён: {out} ({len(raw)} байт)")
                raw_saved = True
        hr = api_hashrate(port)
        pw = nv("power.draw")
        tp = nv("temperature.gpu")
        gclk = nv("clocks.current.graphics")
        mclk = nv("clocks.current.memory")
        if hr:
            rates.append(hr)
        if pw:
            try:
                powers.append(float(pw))
            except ValueError:
                pass
        if tp:
            temps.append(float(tp))
        print(f"  hr={hr}  W={pw}  T={tp}  gclk={gclk}  mclk={mclk}")
        time.sleep(20)

    stop()
    avg_hr = sum(rates) / len(rates) if rates else None
    avg_pw = sum(powers) / len(powers) if powers else None
    result = {
        "coin": coin,
        "preset": preset,
        "hashrate": avg_hr,
        "hashrate_samples": len(rates),
        "power_w": round(avg_pw, 1) if avg_pw else None,
        "temp_c_max": max(temps) if temps else None,
        "minutes": minutes,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (LOGS / "probe-results.jsonl").open("a", encoding="utf-8").write(
        json.dumps(result, ensure_ascii=False) + "\n")
    print("[probe] итог:", json.dumps(result, ensure_ascii=False))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("coin", choices=["pearl", "quantus"])

    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("api")

    p = sub.add_parser("probe")
    p.add_argument("coin", choices=["pearl", "quantus"])
    p.add_argument("--minutes", type=float, default=3)
    p.add_argument("--plimit", type=int)
    p.add_argument("--coffset", type=int)
    p.add_argument("--moffset", type=int)
    p.add_argument("--fan", type=int)

    a = ap.parse_args()

    if a.cmd == "start":
        pid = start(a.coin)
        print("pid:", pid)
    elif a.cmd == "stop":
        stop()
        print("остановлено, разгон сброшен")
    elif a.cmd == "status":
        st = read_state()
        pid = st.get("pid")
        if pid and pid_alive(pid):
            print(f"работает: {st.get('coin')} pid={pid} с {time.strftime('%H:%M:%S', time.localtime(st.get('started', 0)))}")
            print("power:", nv("power.draw"), "W  temp:", nv("temperature.gpu"), "C")
        else:
            print("не запущен")
    elif a.cmd == "api":
        cfg = load_cfg()
        port = cfg["coins"]["pearl"]["api_port"]
        st = read_state()
        if st.get("coin") in cfg["coins"]:
            port = cfg["coins"][st["coin"]]["api_port"]
        print(api_raw(port) or "API не отвечает")
    elif a.cmd == "probe":
        probe(a.coin, a.minutes, {"plimit": a.plimit, "coffset": a.coffset,
                                  "moffset": a.moffset, "fan": a.fan})


if __name__ == "__main__":
    main()
