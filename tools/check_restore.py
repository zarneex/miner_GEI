#!/usr/bin/env python
"""Обратимость настроек: снимок состояния ПК до и после работы приложения.

Приложение меняет три вещи в системе: схему питания Windows (потолок частоты
процессора — термогувернатор), лимит мощности и политику кулера видеокарты.
Все они должны возвращаться к исходным при выходе. Скрипт делает снимок и
сравнивает его:

    python tools/check_restore.py snapshot notes/state-before.json
    # ... запустить приложение, дать майнингу поработать, закрыть ...
    python tools/check_restore.py snapshot notes/state-after.json
    python tools/check_restore.py compare notes/state-before.json notes/state-after.json

Код возврата 0 — состояние совпало. Обороты кулера в сравнение не входят: они
зависят от температуры, а не от настроек.
"""
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minerapp import governor, gpu  # noqa: E402

# поля снимка, которые обязаны совпасть после выхода приложения
STABLE = ("scheme_guid", "cpu_min_pct", "cpu_max_pct",
          "gpu_power_limit_w", "gpu_fan_policy")


def snapshot() -> dict:
    sch = governor.read_scheme()
    cards = gpu.read()
    return {
        "scheme_guid": sch.get("guid"),
        "cpu_min_pct": sch.get("min"),
        "cpu_max_pct": sch.get("max"),
        "gpu_power_limit_w": cards.get("plimit"),
        "gpu_fan_policy": gpu.fan_policy(),      # 0 = авто, 1 = фиксированные
        "gpu_fan_speed": gpu.fan_speed(),
        "gpu_sm_clock": cards.get("sm_clock"),
        "gpu_mem_clock": cards.get("mem_clock"),
    }


def compare(before: dict, after: dict) -> bool:
    ok = True
    for key in STABLE:
        a, b = before.get(key), after.get(key)
        good = a == b
        ok = ok and good
        print(f"{key}: {a} -> {b}  {'PASS' if good else 'FAIL'}")
    return ok


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "snapshot":
        data = snapshot()
        Path(argv[1]).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(json.dumps(data, ensure_ascii=False))
        return 0
    if len(argv) >= 3 and argv[0] == "compare":
        before = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        after = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        ok = compare(before, after)
        print(f"ИТОГ: {'PASS' if ok else 'FAIL'} — после выхода всё как прежде")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
