"""Тест термогувернатора без железа: подменяем powercfg и датчик температуры.

Проверяем: выход на цель, обучение модели и прыжок по ней при смене цели,
объявление недостижимой цели, возврат исходной схемы при остановке, бэкап
на диске, отсутствие качелей.

Запуск: C:\\Python314\\python.exe tools\\test_governor.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
os.environ["MINER_NO_AUTOSTART"] = "1"

from minerapp import governor as G  # noqa: E402

FAILS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


class Rig:
    """Стенд: фейковая схема питания + инерционная температура."""

    def __init__(self, truth):
        self.calls = []              # [(время, pct)]
        self.orig = {"guid": "test-guid", "min": 100, "max": 100}
        self.cur = dict(self.orig)
        self.pct = 100
        self.truth = truth          # T(pct) -> °C
        self.clock = 1000.0

    def read_scheme(self):
        return dict(self.cur)

    def set_power(self, mn, mx):
        self.calls.append((self.clock, mx))
        self.cur["min"], self.cur["max"] = mn, mx
        self.pct = mx
        return True


def run(truth, target, seconds, threads=14, start_pct=100):
    rig = Rig(truth)
    rig.pct = start_pct
    tmp = Path(tempfile.mkdtemp(prefix="govtest-"))
    msgs = []
    gov = G.ThermalGovernor(tmp, on_msg=msgs.append)
    patches = [mock.patch.object(G, "read_scheme", rig.read_scheme),
               mock.patch.object(G, "set_power", rig.set_power),
               mock.patch.object(G.time, "time", lambda: rig.clock)]
    for p in patches:
        p.start()
    temp = truth(start_pct)
    for _ in range(int(seconds / 2.5)):
        rig.clock += 2.5
        temp += (truth(rig.pct) - temp) * 0.25     # инерция датчика/радиатора
        gov.tick(temp, threads, target, True)
    return tmp, rig, gov, msgs, patches, temp


def finish(tmp, rig, gov, patches):
    gov.stop()
    for p in patches:
        p.stop()


def test_reaches_target():
    tmp, rig, gov, msgs, patches, temp = run(lambda p: 40 + 0.5 * p, 75.0, 300)
    pct, pct_tail = rig.pct, rig.pct
    finish(tmp, rig, gov, patches)
    check("цель 75 °C: вышел на 70 %", pct == 70, f"pct={pct}, история {rig.calls}")
    check("цель 75 °C: температура у цели", abs(temp - 75) <= 2.0, f"T={temp:.1f}")
    check("остановка вернула схему", rig.cur == rig.orig, str(rig.cur))
    check("бэкап удалён после остановки", not (tmp / G.BACKUP_NAME).exists())


def test_model_and_jump():
    # цель 70 °C требует 60 %; по дороге 80→60 набирается 5 ячеек для модели
    tmp, rig, gov, msgs, patches, temp = run(lambda p: 40 + 0.5 * p, 70.0, 700)
    pred = gov.pct_for_target(75.0, 14)
    pred_t = gov.predict(60, 14)
    pct_before = rig.pct
    gov.on_target_changed(75.0, 14)
    pct_after = rig.pct
    cur_after = dict(rig.cur)
    finish(tmp, rig, gov, patches)
    check("цель 70 °C: вышел на 60 %", abs(pct_before - 60) <= 3, f"pct={pct_before}")
    check("модель: P для 75 °C ≈ 70 %", pred is not None and abs(pred - 70) <= 10,
          f"pct_for_target(75)={pred}")
    check("модель: прогноз T(60 %) ≈ 70",
          pred_t is not None and abs(pred_t - 70) <= 4, f"predict={pred_t}")
    check("смена цели: прыжок по модели, без ползучей подстройки",
          abs(pct_after - 70) <= 5, f"pct после смены цели {pct_after}")
    check("после смены цели 75 °C схема в норме",
          cur_after["max"] == pct_after, str(cur_after))


def test_unreachable():
    tmp, rig, gov, msgs, patches, temp = run(lambda p: 90.0, 60.0, 300)
    pct = rig.pct
    msg = "; ".join(m for m in msgs if "недостижима" in m)
    finish(tmp, rig, gov, patches)
    check("недостижимая цель: стоит на минимуме", pct == G.PCT_MIN, f"pct={pct}")
    check("недостижимая цель: сказал об этом", gov.unreachable, msg)
    check("недостижимая цель: не крутит потолок", len(rig.calls) <= 3,
          f"переключений {len(rig.calls)}: {[c[1] for c in rig.calls]}")


def test_no_oscillation():
    tmp, rig, gov, msgs, patches, temp = run(lambda p: 40 + 0.5 * p, 85.0, 900)
    t_end = rig.clock
    tail = [c for c in rig.calls if c[0] >= t_end - 240]
    changes = sum(1 for a, b in zip(tail, tail[1:]) if a[1] != b[1])
    pct = rig.pct
    finish(tmp, rig, gov, patches)
    check("гистерезис: последние 4 минуты без дёрганья", changes <= 1,
          f"смен {changes}, значения {[c[1] for c in tail]}")
    check("гистерезис: 85 °C у цели", abs(temp - 85) <= 2.0, f"T={temp:.1f}, pct={pct}")


def test_hot_ceiling_persists():
    """Ступенька 99/100: граница «здесь пробили» запоминается и переживает рестарт."""
    def cliff(p):
        # 80 % → 88 °C: цель 85 пробита уже на старте, граница запоминается
        return 40 + 0.6 * p if p <= 95 else 88.0
    tmp, rig, gov, msgs, patches, temp = run(cliff, 85.0, 900)
    hot_before = gov._hot_at[0] if gov._hot_at else None
    pct_before = rig.pct
    gov.stop()
    gov2 = G.ThermalGovernor(tmp)   # читаем состояние, пока часы стенда ещё подменены
    for p in patches:
        p.stop()
    hot_after = gov2._hot_at[0] if gov2._hot_at else None
    check("ступенька: граница найдена", hot_before is not None, f"hot={hot_before}")
    check("ступенька: граница пережила рестарт", hot_after == hot_before,
          f"hot после перезапуска {hot_after}")
    check("ступенька: выше границы не лезем", pct_before <= (hot_before or 100),
          f"pct={pct_before}, hot={hot_before}")


def test_hold_off_releases_scheme():
    """Галочка «Держать температуру» снята: tick(running=False) отпускает схему
    питания и больше не трогает потолок частоты.

    ВАЖНО: все такты идут, пока `powercfg` подменён (patch живёт до finish) —
    иначе tick(running=True) дёрнул бы настоящую схему питания Windows.
    """
    tmp, rig, gov, msgs, patches, temp = run(lambda p: 40 + 0.5 * p, 70.0, 300)
    active_before = gov.active
    rig.clock += 15
    gov.tick(temp, 14, 70.0, False)          # галочку сняли
    restored = dict(rig.cur)
    active_after = gov.active
    n_calls = len(rig.calls)
    for _ in range(24):                      # ещё 2 минуты без галочки
        rig.clock += 5
        gov.tick(temp, 14, 70.0, False)
    n_calls_later = len(rig.calls)

    # галочку вернули — под патчами, до finish()
    rig.clock += 15
    gov.tick(temp, 14, 70.0, True)
    calls_back = len(rig.calls)
    active_back = gov.active
    applied = rig.cur["max"]
    pct_after = gov.pct
    finish(tmp, rig, gov, patches)
    check("галочка снята: до этого гувернатор был активен", active_before is True,
          f"active={active_before}")
    check("галочка снята: схема питания возвращена сразу", restored == rig.orig,
          f"{restored}")
    check("галочка снята: гувернатор выключился", not active_after)
    check("галочка снята: потолок больше не меняется", n_calls_later == n_calls,
          f"вызовов {n_calls} → {n_calls_later}")
    check("галочка возвращена: гувернатор снова активен", active_back is True)
    check("галочка возвращена: потолок снова применён", calls_back > n_calls_later,
          f"{n_calls_later} → {calls_back}: {[c[1] for c in rig.calls]}")
    check("галочка возвращена: применён именно потолок гувернатора",
          applied == pct_after, f"схема {applied} %, gov.pct {pct_after} %")


if __name__ == "__main__":
    test_reaches_target()
    test_model_and_jump()
    test_unreachable()
    test_no_oscillation()
    test_hot_ceiling_persists()
    test_hold_off_releases_scheme()
    print("\n" + ("ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЫ: {FAILS}"))
    sys.exit(1 if FAILS else 0)
