"""Быстрый вывод последних строк истории: хешрейт, мощность, доход CPU по минутам."""
import csv
import glob
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
LAST = int(sys.argv[2]) if len(sys.argv) > 2 else 40

rows = []
cutoff = time.time() - HOURS * 3600
for path in sorted(glob.glob(r"W:\mining\data\history\*.csv")):
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts = int(r["ts"])
            except (KeyError, ValueError):
                continue
            if ts > cutoff:
                rows.append(r)
rows.sort(key=lambda r: int(r["ts"]))
print(f"{len(rows)} строк за {HOURS:.0f} ч")

for r in rows[-LAST:]:
    hr = float(r["hashrate"]) if r["hashrate"] else 0.0
    cpu = float(r.get("cpu_usd") or 0)
    stamp = time.strftime("%H:%M", time.localtime(int(r["ts"])))
    print(f"{stamp}  hr={hr / 1e12:5.1f} TH/s  W={r['power_w']:>6}  "
          f"cpu_usd={cpu:.5f}  temp={r['temp_c']:>3}")
