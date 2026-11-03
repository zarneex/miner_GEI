"""Термогувернатор CPU: держит заданную температуру потолком частоты.

Замерено на 9800X3D (2026-11-02): потоки XMRig температуру НЕ держат — 12/14/16
потоков дают 90.5/90.6/90.8 °C, потому что PBO поднимает частоту до теплового
лимита. Реальный рычаг — `PROCTHROTTLEMAX` в схеме питания Windows, но он
работает только если `PROCTHROTTLEMIN` опущен (в схеме Bitsum MIN = 100 % и
MAX игнорируется).

Поэтому гувернатор:
  * ставит паре MIN/MAX значение 5 / P, где P — потолок частоты в процентах;
  * ведёт P по температуре к цели из интерфейса (60–90 °C, шаг 2.5);
  * учит таблицу «(потоки, P) → установившаяся температура» (EWMA + МНК),
    чтобы после простоя сразу прыгать к нужному P, а не ползти шагами;
  * не обучается на переходных процессах и на упоре в минимум: если цель
    физически недостижима, честно пишет это и держит минимум, а не «учится»
    невозможному;
  * при остановке майнера/приложения возвращает исходные MIN/MAX (бэкап на
    диске — чтобы краш приложения не оставил процессор зарезанным).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

NO_WINDOW = 0x08000000
SUB_PROCESSOR = "SUB_PROCESSOR"
GUID_MIN = "893dee8e-2bef-41e0-89c6-b55d0929964c"   # PROCTHROTTLEMIN
GUID_MAX = "bc5038f7-23e0-4960-96da-33abaf5935ec"   # PROCTHROTTLEMAX

PCT_MIN = 25            # ниже — XMRig теряет смысл (замер: 2.4 ГГц уже мало)
PCT_MAX = 100
DOWN_STEP = 5           # шаг вниз при перегреве
DOWN_BIG = 10           # шаг вниз при перегреве > 5 °C
UP_STEP = 2             # шаг вверх (плавно)
DEADBAND = 1.5          # ±1.5 °C — не трогаем
TICK_S = 10.0           # период управления
SETTLE_S = 30.0         # после смены P ждём установления
UP_HOLD_S = 60.0        # столько держим запас, прежде чем поднять P
EMERGENCY_C = 6.0       # настолько выше цели — сразу в минимум
UNREACHABLE_S = 120.0   # столько упираемся в минимум — объявляем недостижимость
STATE_NAME = "cpu-governor.json"
BACKUP_NAME = "power-backup.json"


class _Result:
    """powercfg печатает в OEM-кодировке (cp866), а не в UTF-8: декодируем сами."""

    __slots__ = ("returncode", "stdout")

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _run(args: list[str]) -> _Result:
    p = subprocess.run(args, capture_output=True, creationflags=NO_WINDOW)
    raw = p.stdout or b""
    for enc in ("utf-8", "cp866"):
        try:
            return _Result(p.returncode, raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return _Result(p.returncode, raw.decode("cp866", errors="replace"))


def _parse_setting(block: str) -> int | None:
    """Текущее значение «от сети» из вывода powercfg /query (RU и EN).

    Подписи локализованы, а под SSH кириллица иногда теряется в `????` — поэтому
    кроме текстовых шаблонов есть разбор по порядку: после GUID идут «возможное /
    минимум / максимум / текущее от сети / текущее от батареи», текущее AC — 4-е
    hex-значение. Порядок одинаков во всех языках.
    """
    for pat in (r"от сети:\s*0x([0-9a-fA-F]+)",
                r"AC Power Setting Index:\s*0x([0-9a-fA-F]+)"):
        import re
        m = re.search(pat, block)
        if m:
            return int(m.group(1), 16)
    import re
    vals = re.findall(r"0x([0-9a-fA-F]{1,8})", block)
    if len(vals) >= 4:
        return int(vals[3], 16)
    return None


def read_scheme() -> dict:
    """{'guid': ..., 'min': int|None, 'max': int|None} активной схемы."""
    out = _run(["powercfg", "/getactivescheme"]).stdout
    guid = ""
    for token in out.replace(":", " ").split():
        if len(token) == 36 and token.count("-") == 4:
            guid = token
            break
    res = {"guid": guid, "min": None, "max": None}
    q = _run(["powercfg", "/query", "SCHEME_CURRENT", SUB_PROCESSOR]).stdout
    for key, g in (("min", GUID_MIN), ("max", GUID_MAX)):
        if g in q:
            res[key] = _parse_setting(q.split(g, 1)[1][:800])
    return res


def set_power(min_pct: int, max_pct: int) -> bool:
    """Записать пару MIN/MAX в активную схему и применить её."""
    ok = True
    for alias, val in (("PROCTHROTTLEMIN", min_pct), ("PROCTHROTTLEMAX", max_pct)):
        r = _run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR,
                  alias, str(int(val))])
        ok = ok and r.returncode == 0
    r = _run(["powercfg", "/setactive", "SCHEME_CURRENT"])
    return ok and r.returncode == 0


class ThermalGovernor:
    """Контроллер «цель °C → потолок частоты %» с обучением на замерах."""

    def __init__(self, data_dir: Path, on_msg=None):
        self.data_dir = Path(data_dir)
        self.on_msg = on_msg or (lambda _s: None)
        self.state_path = self.data_dir / STATE_NAME
        self.backup_path = self.data_dir / BACKUP_NAME
        self.pct = PCT_MAX
        self.ceiling = 99       # 100 % = PBO 5.2 ГГц и 90.5 °C: только по галочке
        self._step_logged = 0.0
        self.target = 85.0
        self.threads = 0
        self.active = False
        self.unreachable = False
        self._orig: dict | None = None
        self._orig_scheme = ""
        self._last_tick = 0.0
        self._started_at = 0.0
        self._last_change = 0.0
        self._last_learn = 0.0
        self._last_up = 0.0
        self._hot_at: tuple[int, float] | None = None
        self._coarse_logged = 0.0
        self._cell: dict[str, list] = {}      # "потоки:pct" -> [temp, вес]
        self._hist: list[float] = []          # последние температуры (устойчивость)
        self._unreach_since = 0.0
        self._check_scheme_at = 0.0
        self._load()

    # --- журнал -----------------------------------------------------------
    def _log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        self.on_msg(msg)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_dir / "cpu-governor.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # --- состояние/модель -------------------------------------------------
    def _load(self) -> None:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        cells = d.get("cells") or {}
        for k, v in cells.items():
            try:
                self._cell[k] = [float(v[0]), float(v[1])]
            except (TypeError, ValueError, IndexError):
                continue
        hot = d.get("hot")
        try:
            if hot and time.time() - float(hot[1]) < 10800:
                self._hot_at = (int(hot[0]), float(hot[1]))
        except (TypeError, ValueError, IndexError):
            pass

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            state = {"cells": self._cell, "updated": int(time.time())}
            if self._hot_at:      # граница «здесь уже пробили цель» — переживает рестарт,
                state["hot"] = [self._hot_at[0], self._hot_at[1]]   # чтобы не искать её заново
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    @staticmethod
    def _key(threads: int, pct: float) -> str:
        return f"{int(threads)}:{int(round(pct / 5.0) * 5)}"

    def _learn(self, threads: int, pct: float, temp: float) -> None:
        """EWMA по ячейке (потоки, P); вес растёт до 6, чтобы старая точка не жила вечно."""
        k = self._key(threads, pct)
        old = self._cell.get(k)
        if old:
            w = min(old[1] + 1.0, 6.0)
            a = 1.0 / w
            self._cell[k] = [old[0] * (1 - a) + temp * a, w]
        else:
            self._cell[k] = [temp, 1.0]
        # держим таблицу компактной: самые старые ячейки не нужны
        if len(self._cell) > 120:
            for k2 in list(self._cell)[:20]:
                self._cell.pop(k2, None)
        self._save()

    def _fit(self) -> tuple[float, float, float, float] | None:
        """T = a + b*P + c*(N − N0) по таблице.

        N центрируется: при одном и том же числе потоков столбец потоков
        коллинеарен единице, и МНК вырождается. Если потоков в таблице один —
        обходимся двумя параметрами (c = 0).
        Возвращает (a, b, c, N0) или None, если данных мало.
        """
        pts = [(int(k.split(":")[0]), int(k.split(":")[1]), v[0], v[1])
               for k, v in self._cell.items()]
        if len({p[1] for p in pts}) < 3 or len(pts) < 4:
            return None
        n0 = sum(p[0] for p in pts) / len(pts)
        use_n = len({p[0] for p in pts}) >= 2
        k = 3 if use_n else 2
        A = [[0.0] * k for _ in range(k)]
        y = [0.0] * k
        for n, pc, t, w in pts:
            row = [1.0, float(pc)] + ([float(n) - n0] if use_n else [])
            for i in range(k):
                for j in range(k):
                    A[i][j] += w * row[i] * row[j]
                y[i] += w * row[i] * t
        M = [A[i] + [y[i]] for i in range(k)]
        for col in range(k):
            piv = max(range(col, k), key=lambda r: abs(M[r][col]))
            if abs(M[piv][col]) < 1e-9:
                return None
            M[col], M[piv] = M[piv], M[col]
            for r in range(k):
                if r == col:
                    continue
                f = M[r][col] / M[col][col]
                for c2 in range(col, k + 1):
                    M[r][c2] -= f * M[col][c2]
        sol = [M[i][k] / M[i][i] for i in range(k)]
        a, b = sol[0], sol[1]
        c = sol[2] if k > 2 else 0.0
        if b <= 0.02:            # температура не растёт с частотой — модель бесполезна
            return None
        if not all(abs(v) < 1e4 for v in sol):
            return None
        return a, b, c, n0

    def predict(self, pct: float, threads: int) -> float | None:
        f = self._fit()
        if not f:
            return None
        a, b, c, n0 = f
        return a + b * pct + c * (threads - n0)

    def pct_for_target(self, target: float, threads: int) -> float | None:
        f = self._fit()
        if not f:
            return None
        a, b, c, n0 = f
        p = (target - a - c * (threads - n0)) / b
        if not (PCT_MIN - 10 <= p <= PCT_MAX + 10):
            return None
        return max(PCT_MIN, min(PCT_MAX, round(p / 5.0) * 5))

    # --- схема питания ----------------------------------------------------
    def _backup(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.backup_path.write_text(json.dumps(
                {"scheme": self._orig_scheme, "min": self._orig["min"],
                 "max": self._orig["max"], "ts": int(time.time())}), encoding="utf-8")
        except (OSError, TypeError):
            pass

    def restore_backup(self) -> str:
        """Вернуть схему, которую прошлый запуск не успел восстановить."""
        try:
            d = json.loads(self.backup_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        try:
            if d.get("min") is not None and d.get("max") is not None:
                set_power(int(d["min"]), int(d["max"]))
                self.backup_path.unlink(missing_ok=True)
                return f"схема восстановлена после прошлого запуска: {d['min']}/{d['max']} %"
        except (OSError, ValueError, TypeError):
            pass
        return ""

    def _apply(self, pct: int, why: str) -> None:
        pct = int(max(PCT_MIN, min(PCT_MAX, pct)))
        if pct == self.pct:
            return
        ok = set_power(5, pct)
        if ok:
            self.pct = pct
            self._last_change = time.time()
            self._log(f"{why}: потолок {pct} %")
        else:
            self._log(f"powercfg не принял {pct} % — остаюсь на {self.pct} %")

    # --- цикл -------------------------------------------------------------
    def tick(self, temp: float | None, threads: int, target: float,
             running: bool) -> None:
        now = time.time()
        self.target = float(target)
        if not running:
            if self.active:
                self.stop()
            return
        if temp is None:
            return
        if now - self._last_tick < TICK_S:
            return
        self._last_tick = now
        self.threads = int(threads or 0)

        if not self.active:
            self.start(initial=self._initial_pct())
            return          # даём температуре отреагировать, аварийный шаг — со след. такта

        # схему могли переключить/сбросить (Process Lasso, пользователь) — не воюем
        if now - self._check_scheme_at > 60:
            self._check_scheme_at = now
            cur = read_scheme()
            if cur["guid"] and self._orig_scheme and cur["guid"] != self._orig_scheme:
                self._log("схема питания сменилась — отпускаю управление")
                self.active = False
                return
            if cur["max"] is not None and cur["max"] not in (0,) and cur["max"] != self.pct:
                self._log(f"потолок сброшен извне ({cur['max']} %) — возвращаю {self.pct} %")
                set_power(5, self.pct)

        self._track(temp)

        if now - self._started_at < 25:
            return          # притирка: не гоняем потолок, пока спадает старая температура

        err = temp - self.target
        # упор в минимум: цель недостижима, не «учимся» дальше
        if self.pct <= PCT_MIN and err > 2.0:
            if not self._unreach_since:
                self._unreach_since = now
            elif now - self._unreach_since > UNREACHABLE_S and not self.unreachable:
                self.unreachable = True
                self._log(f"цель {self.target:.1f} °C недостижима: на {PCT_MIN} % "
                          f"{temp:.1f} °C — держу минимум (лечится BIOS: CO −25, "
                          "лимит 85 °C)")
            return
        self._unreach_since = 0.0
        if err <= 1.0:
            self.unreachable = False

        if err > EMERGENCY_C and now - self._started_at > 25:
            # аварийно: не ждём установления и не ползём по 5 % — сразу в минимум.
            # Первые 25 с после старта — это ещё прежняя температура, не перегрев.
            self._apply(PCT_MIN, f"аварийно: {temp:.1f} °C при цели "
                                 f"{self.target:.1f} °C")
            return
        if err > DEADBAND:
            # у верхней границы шаг мелкий: там кривая температуры крутая
            # (замер: 95 % — 75 °C, 100 % — 90 °C), пятёркой можно перескочить цель
            if self.pct >= 90:
                step = 2
            else:
                step = DOWN_BIG if err > 8 else DOWN_STEP
            if self.pct == self.ceiling:
                self._note_step(temp)      # упёрлись в потолок — это PBO, а не ошибка
            if now - self._last_change < SETTLE_S:
                return
            self._hot_at = (self.pct, now)     # выше этого потолка цель уже пробита
            self._save()                       # граница должна пережить рестарт
            self._apply(self.pct - step, f"CPU {temp:.1f} °C при цели "
                                         f"{self.target:.1f} °C")
            return
        if err < -DEADBAND:
            far = err < -10 and self.pct < 90
            small = self.pct >= 88
            step = 5 if far else (1 if small else 2)
            if self._hot_at and err < -10 and self.pct < self._hot_at[0] - 10:
                step = 10       # граница «где пробили цель» известна — идём к ней крупно
            if now - self._last_up < (30.0 if (far or small) else UP_HOLD_S):
                return
            nxt = self.pct + step
            nxt = min(nxt, self.ceiling)   # 100 % отдаёт полный PBO (+14 °C) — только по разрешению
            if self._hot_at and now - self._hot_at[1] < 10800:
                # не возвращаемся туда, где только что пробили цель: ступенька,
                # а не качели. Через 3 часа (условия могли остыть) — можно снова.
                nxt = min(nxt, self._hot_at[0] - 1)
            if nxt <= self.pct:
                self._note_coarse(temp)
                return
            pred = self.predict(nxt, self.threads)
            if pred is not None and pred > self.target - 0.5:
                self._note_coarse(temp)
                return
            self._last_up = now
            self._apply(nxt, f"CPU {temp:.1f} °C — запас, поднимаю потолок")

    def set_allow_100(self, allow: bool) -> None:
        """Разрешить/запретить 100 % (полный PBO). Запрет — сразу опускаем потолок."""
        self.ceiling = 100 if allow else 99
        if self.active and self.pct > self.ceiling:
            self._apply(self.ceiling, "100 % запрещён — опускаю потолок")

    def _note_step(self, temp: float) -> None:
        """Ступень 99↔100 — не ошибка алгоритма, а PBO. Пишем раз в 10 минут."""
        now = time.time()
        if now - self._step_logged < 600:
            return
        self._step_logged = now
        if self.ceiling >= 100:
            self._log(f"потолок 100 %: {temp:.1f} °C при цели {self.target:.1f} °C — "
                      "PBO отдал максимум, ниже только ступенькой 99 %")
        else:
            self._log(f"потолок 99 %: {temp:.1f} °C при цели {self.target:.1f} °C — "
                      "100 % стоит +40 Вт за +0–5 % хешрейта и даёт 90.5 °C, "
                      "100 % выключен (галочка «Разрешить 100 %» рядом)")

    def _note_coarse(self, temp: float) -> None:
        """Цель не берётся: между двумя ступенями потолка провал в несколько °C."""
        now = time.time()
        if now - self._coarse_logged < 600:
            return
        self._coarse_logged = now
        self._log(f"цель {self.target:.1f} °C не берётся точно: ступень потолка "
                  f"{self.pct} % даёт {temp:.1f} °C — держу её (это PBO, а не ошибка "
                  "алгоритма)")

    def _track(self, temp: float) -> None:
        """Обучаемся на установившемся режиме: точка берётся не раньше 30 с после
        смены потолка (тепловой переход) и не чаще раза в 15 с. Переходные точки
        не пишем — иначе модель выучит «температуру до остывания»."""
        now = time.time()
        prev = self._hist[-1] if self._hist else None
        self._hist.append(temp)
        self._hist = self._hist[-4:]
        held = now - self._last_change
        # на ступеньке температура прыгает на градусы за такт — такие точки не пишем,
        # иначе модель выучит «100 % = 79 °C» на переходе и будет лезть выше цели
        if prev is not None and abs(temp - prev) > 1.0:
            return
        if held < 30 or now - self._last_learn < 15:
            return
        self._last_learn = now
        self._learn(self.threads, self.pct, sum(self._hist) / len(self._hist))

    def _initial_pct(self) -> int:
        guess = self.pct_for_target(self.target, self.threads)
        if guess is not None:
            return int(guess)
        return 80        # без модели: середина, дальше по шагам

    def start(self, initial: int | None = None) -> None:
        self.restore_backup()
        cur = read_scheme()
        if cur["min"] is None or cur["max"] is None:
            # Не знаем исходных значений — не имеем права менять схему: иначе
            # после остановки вернуть будет нечего (так было на втором ПК).
            self.active = False
            self._log("схема питания не прочитана — гувернатор не включаю")
            return
        self._orig = {"min": cur["min"], "max": cur["max"]}
        self._orig_scheme = cur["guid"]
        self._backup()
        self.pct = PCT_MAX
        self.active = True
        self._started_at = time.time()
        self._last_up = self._started_at
        p0 = int(initial if initial is not None else self._initial_pct())
        p0 = min(p0, self.ceiling)
        self._apply(p0, f"старт: цель {self.target:.1f} °C, потоков {self.threads}")
        self._log(f"гувернатор включён (исходно {self._orig['min']}/{self._orig['max']} %)")

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        if self._orig and self._orig.get("min") is not None:
            ok = set_power(int(self._orig["min"]), int(self._orig["max"]))
            self._log(("схема возвращена: " if ok else "схему вернуть не удалось: ")
                      + f"{self._orig['min']}/{self._orig['max']} %")
        try:
            self.backup_path.unlink(missing_ok=True)
        except OSError:
            pass

    def on_target_changed(self, target: float, threads: int) -> None:
        """Пользователь покрутил спинбокс — сразу прыгаем к прогнозу модели."""
        self.target = float(target)
        if not self.active:
            return
        self._hot_at = None           # новая цель — старый потолок «пробито» не в счёт
        guess = self.pct_for_target(target, threads)
        if guess is not None and abs(guess - self.pct) >= 3:
            self._apply(min(int(guess), self.ceiling),
                        f"цель {target:.1f} °C — пересчёт по модели")
