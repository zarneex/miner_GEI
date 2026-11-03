"""История метрик для графиков и счётчиков.

Файлы: `W:\\mining\\data\\history\\ГГГГ-ММ.csv`, поминутные срезы (шаг 60 с):
    ts, coin, power_w, temp_c, fan, hashrate, usd, kwh
`usd` и `kwh` — **приращения** за эту минуту, поэтому сумма по диапазону и есть доход/расход.

Размер: ~60 байт на строку, 1440 строк в сутки → ~2.6 МБ в месяц, ~31 МБ в год.
Держим 12 месяцев детальных данных, всё, что старше, — в `totals.json` (там итог не теряется).
Итог за всё время: `W:\\mining\\data\\totals.json` — по устройству и алгоритму.
"""
import csv
import json
import time
from pathlib import Path

from .paths import DATA

HIST = DATA / "history"
TOTALS = DATA / "totals.json"
FIELDS = ["ts", "coin", "power_w", "temp_c", "fan", "hashrate", "usd", "kwh", "cpu_usd"]
KEEP_MONTHS = 12


def _month_path(ts: float) -> Path:
    return HIST / time.strftime("%Y-%m.csv", time.localtime(ts))


def append(ts: float, coin: str, power_w: float, temp_c: float | None, fan: float | None,
           hashrate: float | None, usd: float, kwh: float, cpu_usd: float = 0.0) -> None:
    HIST.mkdir(parents=True, exist_ok=True)
    path = _month_path(ts)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({
            "ts": int(ts), "coin": coin,
            "power_w": f"{power_w:.1f}", "temp_c": "" if temp_c is None else f"{temp_c:.0f}",
            "fan": "" if fan is None else f"{fan:.0f}",
            "hashrate": f"{hashrate:.0f}" if hashrate else "",
            "usd": f"{usd:.9f}", "kwh": f"{kwh:.6f}",
            "cpu_usd": f"{cpu_usd:.9f}",
        })


def prune(keep_months: int = KEEP_MONTHS) -> None:
    """Удаляем детальные файлы старше N месяцев: итог за всё время не страдает."""
    if not HIST.exists():
        return
    cutoff = time.localtime(time.time() - keep_months * 30 * 86400)[:2]
    for path in HIST.glob("*.csv"):
        try:
            y, m = (int(x) for x in path.stem.split("-"))
        except ValueError:
            continue
        if (y, m) < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def load(start: float | None = None, end: float | None = None) -> list[dict]:
    """Срезы за диапазон (epoch). Пустые строки пропускаются."""
    rows: list[dict] = []
    if not HIST.exists():
        return rows
    for path in sorted(HIST.glob("*.csv")):
        try:
            with open(path, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        ts = int(r["ts"])
                    except (KeyError, ValueError):
                        continue
                    if start is not None and ts < start:
                        continue
                    if end is not None and ts > end:
                        continue
                    rows.append({
                        "ts": ts, "coin": r.get("coin", ""),
                        "power_w": _f(r.get("power_w")), "temp_c": _f(r.get("temp_c")),
                        "fan": _f(r.get("fan")), "hashrate": _f(r.get("hashrate")),
                        "usd": _f(r.get("usd")) or 0.0, "kwh": _f(r.get("kwh")) or 0.0,
                        "cpu_usd": _f(r.get("cpu_usd")) or 0.0,
                    })
        except OSError:
            continue
    rows.sort(key=lambda x: x["ts"])
    return rows


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def aggregate(rows: list[dict], step_s: int) -> list[dict]:
    """Склеиваем в корзины по step_s: средние для мощности/температуры, суммы для денег."""
    if not rows:
        return []
    out: list[dict] = []
    cur_ts = rows[0]["ts"] // step_s * step_s
    bucket = {"ts": cur_ts, "pw": [], "tc": [], "usd": 0.0, "kwh": 0.0, "hr": []}
    for r in rows:
        b_ts = r["ts"] // step_s * step_s
        if b_ts != bucket["ts"]:
            out.append(_finish(bucket))
            bucket = {"ts": b_ts, "pw": [], "tc": [], "usd": 0.0, "kwh": 0.0, "hr": []}
        if r["power_w"] is not None:
            bucket["pw"].append(r["power_w"])
        if r["temp_c"] is not None:
            bucket["tc"].append(r["temp_c"])
        if r["hashrate"]:
            bucket["hr"].append(r["hashrate"])
        bucket["usd"] += r["usd"]
        bucket["kwh"] += r["kwh"]
    out.append(_finish(bucket))
    return out


def _finish(b: dict) -> dict:
    return {"ts": b["ts"],
            "power_w": sum(b["pw"]) / len(b["pw"]) if b["pw"] else None,
            "temp_c": sum(b["tc"]) / len(b["tc"]) if b["tc"] else None,
            "hashrate": sum(b["hr"]) / len(b["hr"]) if b["hr"] else None,
            "usd": b["usd"], "kwh": b["kwh"]}


def summary(rows: list[dict]) -> dict:
    """Итоги по диапазону: суммы и средние."""
    usd = sum(r["usd"] for r in rows)
    kwh = sum(r["kwh"] for r in rows)
    pw = [r["power_w"] for r in rows if r["power_w"] is not None]
    tc = [r["temp_c"] for r in rows if r["temp_c"] is not None]
    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600 if len(rows) > 1 else 0.0
    return {
        "usd": usd, "kwh": kwh,
        "power_avg": sum(pw) / len(pw) if pw else None,
        "temp_avg": sum(tc) / len(tc) if tc else None,
        "hours": span_h,
        "usd_per_hour": usd / span_h if span_h > 0 else 0.0,
        "rows": len(rows),
    }


# --- итог за всё время ----------------------------------------------------
def load_totals() -> dict:
    try:
        with open(TOTALS, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def add_totals(device: str, algo: str, usd: float, kwh: float, seconds: float) -> dict:
    data = load_totals()
    node = data.setdefault(device, {}).setdefault(algo, {"usd": 0.0, "kwh": 0.0, "seconds": 0.0})
    node["usd"] = round(node.get("usd", 0.0) + usd, 9)
    node["kwh"] = round(node.get("kwh", 0.0) + kwh, 6)
    node["seconds"] = round(node.get("seconds", 0.0) + seconds, 1)
    tmp = TOTALS.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(TOTALS)
    except OSError:
        pass
    return data


def totals_sum(data: dict) -> tuple[float, float, float]:
    """(доход, кВт·ч, секунды майнинга) по всем устройствам/алгоритмам."""
    usd = kwh = sec = 0.0
    for algos in data.values():
        for node in algos.values():
            usd += node.get("usd", 0.0)
            kwh += node.get("kwh", 0.0)
            sec += node.get("seconds", 0.0)
    return usd, kwh, sec
