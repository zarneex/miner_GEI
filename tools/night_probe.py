#!/usr/bin/env python
"""Проба для ночного режима: снижаем лимит мощности при работающем майнере и смотрим,
падают ли обороты кулера, и не возвращает ли майнер лимит обратно.

Пишет строки в W:\\mining\\logs\\night-probe.log и в stdout.
По окончании возвращает лимит 250 Вт.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

NVSMI = r"C:\Windows\System32\nvidia-smi.exe"
STATE = Path(r"W:\mining\state.json")
LOG = Path(r"W:\mining\logs\night-probe.log")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def nv(query: str):
    try:
        out = subprocess.run([NVSMI, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        return out.splitlines()[0].strip() if out else None
    except Exception:
        return None


def say(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def set_pl(w: int) -> str:
    r = subprocess.run([NVSMI, "-pl", str(w)], capture_output=True, text=True, timeout=30)
    return (r.stdout + r.stderr).strip().replace("\n", " ")[:160]


def sample(tag: str) -> None:
    say(f"{tag}: plimit={nv('power.limit')} draw={nv('power.draw')} temp={nv('temperature.gpu')} "
        f"fan={nv('fan.speed')} sm={nv('clocks.current.sm')} mem={nv('clocks.current.memory')}")


def main() -> None:
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
            say(f"state: coin={st.get('coin')} pid={st.get('pid')} preset={st.get('preset')}")
        except Exception:
            pass
    say("=== baseline (как есть) ===")
    for _ in range(3):
        sample("base")
        time.sleep(15)

    say("=== шаг 1: nvidia-smi -pl 210 ===")
    say("set: " + set_pl(210))
    for i in range(5):
        time.sleep(15)
        sample(f"210+{(i + 1) * 15}s")

    say("=== шаг 2: nvidia-smi -pl 175 ===")
    say("set: " + set_pl(175))
    for i in range(6):
        time.sleep(15)
        sample(f"175+{(i + 1) * 15}s")

    say("=== шаг 3: сколько держит майнер, если ничего не трогать (60 с) ===")
    for i in range(4):
        time.sleep(15)
        sample(f"hold+{(i + 1) * 15}s")

    say("=== возврат -pl 250 ===")
    say("set: " + set_pl(250))
    time.sleep(10)
    sample("restored")
    say("готово")


if __name__ == "__main__":
    main()
