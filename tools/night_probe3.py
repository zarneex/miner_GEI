#!/usr/bin/env python
"""Проба 3: ночной режим с жёстким потолком оборотов через --gpu-fan.

NVML обороты не даёт, но Krig умеет --gpu-fan <процент> (фиксированные обороты).
Смотрим цену этого решения: температура при 175 Вт и кулере 50% / 55% и хешрейт.

Перезапускает майнер (мягко). По окончании возвращает обычный режим 250 Вт.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(r"W:\mining\scripts")))
import minerctl  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from minerapp import gpu  # noqa: E402

LOG = Path(r"W:\mining\logs\night-probe3.log")
COIN = "pearl"


def say(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def measure(tag: str, warmup: int, window: int) -> dict:
    print(f"[{tag}] разогрев {warmup} с…", flush=True)
    time.sleep(warmup)
    rates, fans, temps, watts = [], [], [], []
    end = time.time() + window
    while time.time() < end:
        d = gpu.read()
        hr = minerctl.api_hashrate(minerctl.load_cfg()["coins"][COIN]["api_port"])
        if hr:
            rates.append(hr)
        for key, bucket in (("fan", fans), ("temp", temps), ("power", watts)):
            if d.get(key) is not None:
                bucket.append(d[key])
        say(f"{tag} live: {d.get('power')}W {d.get('temp')}C {d.get('fan')}% "
            f"sm={d.get('sm_clock')} mem={d.get('mem_clock')} hr={hr}")
        time.sleep(10)
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    res = {"tag": tag, "watts": avg(watts), "temp": avg(temps), "fan": avg(fans),
           "hashrate": avg(rates), "n": len(rates)}
    say(f"{tag} ИТОГ: {res['watts']}W {res['temp']}C кулер {res['fan']}% "
        f"хешрейт {res['hashrate']} TH/s ({res['n']} замеров)")
    return res


def main() -> None:
    results = []
    for fan in (50, 55):
        preset = {"plimit": 175, "coffset": 0, "moffset": 1500, "fan": fan}
        say(f"=== перезапуск: {preset}")
        minerctl.start(COIN, preset)
        time.sleep(3)
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq krig-miner.exe", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout
        count = out.lower().count("krig-miner")
        say(f"процессов krig до замера: {count}")
        if count != 1:
            say("СТОП: майнеров не один, замер был бы мусором")
            return
        results.append(measure(f"PL175+fan{fan}", warmup=120, window=90))

    say("=== возврат в обычный режим 250 Вт")
    minerctl.stop()
    say("set pl 250: " + gpu.set_power_limit(250)[1])
    (LOG.parent / "night-probe3.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    say("готово")


if __name__ == "__main__":
    main()
