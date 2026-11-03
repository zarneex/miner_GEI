#!/usr/bin/env python
"""Проба 2: ночной режим без перезапуска майнера — лимит мощности + фиксация частоты ядра.

Цель: кулер < 60% при сохранении хешрейта. Майнинг Pearl упирается в память, поэтому
снижение частоты ядра почти не стоит хешрейта, но снимает тепло и обороты.

По окончании: снять фиксацию клоков и вернуть лимит 250 Вт.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(r"W:\mining\scripts")))
import minerctl  # noqa: E402  (движок: запросы к API майнера и nvidia-smi)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from minerapp import gpu  # noqa: E402

LOG = Path(r"W:\mining\logs\night-probe2.log")


def say(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def stat(tag: str, seconds: int) -> None:
    """Ждём стабилизации и печатаем средние значения за окно."""
    rates, fans, temps, watts = [], [], [], []
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(10)
        d = gpu.read()
        hr = minerctl.api_hashrate(12000)
        if hr:
            rates.append(hr)
        if d.get("fan") is not None:
            fans.append(d["fan"])
        if d.get("temp") is not None:
            temps.append(d["temp"])
        if d.get("power") is not None:
            watts.append(d["power"])
        say(f"{tag} live: {d.get('power')}W {d.get('temp')}C {d.get('fan')}% "
            f"sm={d.get('sm_clock')} hr={hr}")
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    say(f"{tag} ИТОГ: {avg(watts)}W {avg(temps)}C кулер {avg(fans)}% "
        f"хешрейт {avg(rates)} ({len(rates)} замеров)")


def main() -> None:
    lo, hi = gpu.limits()
    say(f"границы лимита мощности: {lo}..{hi} Вт")
    say("set pl 175: " + gpu.set_power_limit(175)[1])
    stat("PL175", 60)

    for mhz in (1800, 1500, 1200):
        ok, msg = gpu.lock_core_clock(mhz)
        say(f"-lgc {mhz}: {ok} {msg}")
        stat(f"PL175+lgc{mhz}", 70)

    gpu.reset_clocks()
    say("клоки сброшены")
    say("set pl 250: " + gpu.set_power_limit(250)[1])
    say("готово (карта возвращена в исходный режим 250 Вт)")


if __name__ == "__main__":
    main()
