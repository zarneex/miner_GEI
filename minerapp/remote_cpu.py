"""Второй CPU-майнинг: карточка «Процессор» для другого ПК (по SSH).

Модуль персональный и по умолчанию выключен (`app.cpu_remote.enabled=false`):
у остальных пользователей репозитория секция «Процессор» выглядит как раньше —
одна вкладка нет, сеть не трогается. Включается только в личном config.json.

Как работает: локальная панель запускает на втором ПК агент
(`tools/remote_cpu_agent.py`) по уже настроенному SSH-ключу и говорит с ним
JSON-строками через stdin/stdout. Отдельного порта, службы и правки firewall нет.
Агент сам держит XMRig, Job Object (доля CPU), термогувернатор и мост LHM, поэтому
набор функций повторяет локальную карточку: старт/стоп, автозапуск, потоки на
ходу, «Нагрузка ЦП %», «Держать температуру» + цель + «Разрешить 100 %».
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
                               QLabel, QSpinBox, QVBoxLayout, QWidget)

from . import asyncjob

NO_WINDOW = 0x08000000
POLL_MS = 2500
OFFLINE_POLL_MS = 6000


class RemoteError(RuntimeError):
    pass


class SshAgent:
    """Один долгоживущий `ssh host python agent.py --stdio` + JSON-обмен."""

    def __init__(self, host: str, python: str, script: str,
                 connect_timeout: int = 6) -> None:
        self.host = host
        self.python = python
        self.script = script
        self.connect_timeout = connect_timeout
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._seq = 0

    # --- процесс -----------------------------------------------------------
    def _argv(self) -> list[str]:
        remote = f'"{self.python}" "{self.script}" --stdio'
        return ["ssh", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={self.connect_timeout}",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                self.host, remote]

    def _ensure(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._close(quiet=True)
        self._q = queue.Queue()
        self._proc = subprocess.Popen(
            self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            bufsize=1, creationflags=NO_WINDOW)
        q = self._q
        proc = self._proc

        def reader() -> None:
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        q.put(line)
            except (OSError, ValueError):
                pass
            finally:
                q.put(None)

        threading.Thread(target=reader, name="remote-cpu-reader", daemon=True).start()

    def _close(self, quiet: bool = False) -> None:
        p = self._proc
        self._proc = None
        if p is None:
            return
        try:
            p.terminate()
            p.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                p.kill()
            except OSError:
                pass

    def close(self) -> None:
        """Попросить агент выйти (он вернёт схему питания) и закрыть канал."""
        if self._proc is not None and self._proc.poll() is None:
            try:
                self.call("quit", timeout=2)
            except RemoteError:
                pass
        self._close()

    # --- обмен -------------------------------------------------------------
    def call(self, cmd: str, timeout: float = 20.0, **kw) -> dict:
        with self._lock:
            self._ensure()
            assert self._proc is not None
            self._seq += 1
            rid = self._seq
            req = {"id": rid, "cmd": cmd}
            req.update(kw)
            try:
                self._proc.stdin.write(json.dumps(req, ensure_ascii=True) + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError) as e:
                self._close()
                raise RemoteError(f"канал закрыт: {e}") from e
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    self._close()
                    raise RemoteError(f"нет ответа за {timeout:.0f} с")
                try:
                    line = self._q.get(timeout=left)
                except queue.Empty:
                    continue
                if line is None:
                    self._close()
                    raise RemoteError("SSH-канал закрылся (ключ, хост или агент)")
                try:
                    resp = json.loads(line)
                except ValueError:
                    continue
                if resp.get("id") != rid:
                    continue        # поздний ответ прошлого запроса
                if not resp.get("ok", True):
                    raise RemoteError(resp.get("msg") or "ошибка агента")
                return resp


class _NoWheelSpin(QSpinBox):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setKeyboardTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event) -> None:      # noqa: N802 — имя из Qt
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _NoWheelDoubleSpin(QDoubleSpinBox):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setKeyboardTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event) -> None:      # noqa: N802 — имя из Qt
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class RemoteCpuPanel(QWidget):
    """Вкладка «PC2» внутри секции «Процессор».

    Все настройки независимы от локальной карточки и лежат в
    `app.cpu_remote` того же config.json. Сеть — только по действиям/опросу.
    """

    log_line = Signal(str)      # в консоль окна, из любого потока

    def __init__(self, cfg: dict, save, log=None, parent=None) -> None:
        super().__init__(parent)
        self.cfg = dict(cfg or {})
        self._save = save
        self._log = self.log_line.emit
        self.client = SshAgent(str(self.cfg.get("host") or "pc2"),
                               str(self.cfg.get("python") or "python"),
                               str(self.cfg.get("script") or ""))
        self.online = False
        self.running = False
        self._busy = False
        self._polling = False
        self._autostart_pending = False
        self.last: dict = {}
        self._cpus_shown = ""
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        QTimer.singleShot(400, self._poll)

    # --- построение --------------------------------------------------------
    def _build(self) -> None:
        lay = QGridLayout(self)
        lay.setContentsMargins(8, 2, 8, 6)
        lay.setHorizontalSpacing(8)
        lay.setVerticalSpacing(4)
        lay.setColumnStretch(1, 1)

        self.chk_on = QCheckBox("Вкл")
        self.chk_auto = QCheckBox("автозапуск")
        self.chk_auto.setToolTip("Запускать CPU-майнинг на этом ПК при старте приложения.")
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.chk_on)
        top.addWidget(self.chk_auto)
        top.addStretch(1)
        self.lbl_state = QLabel("нет связи")
        self.lbl_state.setStyleSheet("color:#6c7681;font-size:11px;")
        top.addWidget(self.lbl_state)
        lay.addLayout(top, 0, 0, 1, 2)

        self.spin_threads = _NoWheelSpin()
        self.spin_threads.setRange(2, int(self.cfg.get("threads_max") or 32))
        self.spin_threads.setSingleStep(1)
        self.spin_threads.setValue(int(self.cfg.get("threads") or 8))
        self.spin_threads.setToolTip(self._threads_tip())
        lay.addWidget(QLabel("Потоков"), 1, 0)
        lay.addWidget(self.spin_threads, 1, 1)

        self.spin_load = _NoWheelSpin()
        self.spin_load.setRange(20, 100)
        self.spin_load.setSingleStep(5)
        self.spin_load.setValue(int(self.cfg.get("load") or 100))
        self.spin_load.setSuffix(" %")
        self.spin_load.setToolTip(
            "Потолок доли процессора для XMRig (Job Object HARD_CAP).\n"
            "Независим от «Потоков»: потоки — сколько ядер отдано, процент — доля\n"
            "их времени. 100 % — ограничение выключено.")
        lay.addWidget(QLabel("Нагрузка ЦП"), 2, 0)
        lay.addWidget(self.spin_load, 2, 1)

        self.chk_hold = QCheckBox("Держать температуру")
        self.chk_hold.setChecked(bool(self.cfg.get("hold_temp")))
        self.chk_hold.setToolTip(
            "Термогувернатор на втором ПК держит цель справа потолком частоты\n"
            "(PROCTHROTTLEMAX), как на этом ПК. Выключено — схема питания не трогается.")
        lay.addWidget(self.chk_hold, 3, 0)

        self.spin_temp = _NoWheelDoubleSpin()
        self.spin_temp.setRange(60.0, 90.0)
        self.spin_temp.setSingleStep(2.5)
        self.spin_temp.setDecimals(1)
        self.spin_temp.setValue(float(self.cfg.get("max_temp") or 80.0))
        self.spin_temp.setSuffix(" °C")
        self.spin_temp.setToolTip("Цель термогувернатора второго ПК (60–90 °C, шаг 2.5).")
        lay.addWidget(self.spin_temp, 3, 1)

        self.chk_boost = QCheckBox("Разрешить 100 % (полный PBO)")
        self.chk_boost.setChecked(bool(self.cfg.get("allow_100", True)))
        self.chk_boost.setToolTip(
            "Разрешить потолок 100 % на втором ПК. Снята — гувернатор не поднимает\n"
            "частоту выше 99 %.")
        lay.addWidget(self.chk_boost, 4, 0, 1, 2)

        self.lbl_live = QLabel("остановлен")
        self.lbl_live.setStyleSheet("color:#4d9de0;font-family:Consolas;font-size:11px;")
        self.lbl_detail = QLabel("")
        self.lbl_detail.setStyleSheet("color:#8fa3b8;font-size:10px;")
        lay.addWidget(self.lbl_live, 5, 0, 1, 2)
        lay.addWidget(self.lbl_detail, 6, 0, 1, 2)
        self.setToolTip(self._hint_text())
        self._sync_hold_ui()

        self.chk_on.toggled.connect(self.on_toggle)
        self.chk_auto.toggled.connect(lambda on: self._save("autostart", bool(on)))
        self.chk_hold.toggled.connect(self.on_hold)
        self.chk_boost.toggled.connect(self.on_allow)
        self.spin_temp.valueChanged.connect(self.on_target)
        self.spin_threads.valueChanged.connect(self._on_threads_edit)
        self.spin_load.valueChanged.connect(self._on_load_edit)
        self._threads_timer = QTimer(self)
        self._threads_timer.setSingleShot(True)
        self._threads_timer.timeout.connect(self.on_threads)
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.timeout.connect(self.on_load)

    def _hint_text(self) -> str:
        host = str(self.cfg.get("host") or "pc2")
        return (f"{host}: управление по SSH, отдельного порта и службы нет · "
                "XMRig на втором ПК · температура/частота — LHM · "
                "агент сам держит Job Object и термогувернатор")

    def _threads_tip(self, cpus: str = "") -> str:
        tip = ("Сколько потоков отдать XMRig на втором ПК. Меняются на ходу через\n"
               "HTTP API, без перезапуска (если API не ответил — карточка перезапустит).\n"
               "Порядок: сначала P-ядра (0–11), E-ядра (12–19) — только после них.\n"
               "Кеш L3 24 МБ тянет ~12 потоков: больше — хешрейт падает, а не растёт.")
        if cpus:
            tip += f"\nСейчас заняты ядра {cpus}."
        return tip

    def _sync_hold_ui(self) -> None:
        on = self.chk_hold.isChecked()
        self.spin_temp.setEnabled(on)
        self.chk_boost.setEnabled(on)

    # --- статус ------------------------------------------------------------
    def _poll(self) -> None:
        if self._polling or self._busy:
            return
        self._polling = True
        box: dict = {}

        def job() -> None:
            try:
                box["st"] = self.client.call("status", timeout=8)
            except Exception as e:      # noqa: BLE001 — панель не должна падать
                box["e"] = e

        def done(_r=None) -> None:
            self._polling = False
            if "st" in box:
                self._apply_status(box["st"])
            else:
                self._offline(box.get("e"))

        asyncjob.run_bg(job, done, owner=self)
        self._timer.setInterval(POLL_MS if self.online else OFFLINE_POLL_MS)

    def _apply_status(self, st: dict) -> None:
        first = not self.online
        self.online = True
        self.last = st
        self.running = bool(st.get("running"))
        self._timer.setInterval(POLL_MS)
        self._sync_on()
        temp, power, mhz, hr = (st.get("temp_c"), st.get("power_w"),
                                st.get("mhz"), st.get("hashrate"))
        if self.running:
            parts = [f"{temp:.1f} °C" if temp is not None else "— °C",
                     f"{power:.0f} Вт" if power is not None else "— Вт",
                     f"{hr / 1000:.2f} kH/s" if hr is not None else "— kH/s"]
            self.lbl_live.setText(" · ".join(parts))
            det = []
            if mhz:
                det.append(f"{mhz:.0f} МГц")
            threads_run = st.get("threads_run") or int(self.spin_threads.value())
            det.append(f"{threads_run} потоков")
            if st.get("load_pct"):
                det.append(f"доля {st['load_pct']} %")
            self.lbl_detail.setText(" · ".join(det))
        else:
            self.lbl_live.setText("остановлен")
            self.lbl_detail.setText("на связи · XMRig не запущен")
        cpus = str(st.get("cpus") or "")
        if cpus and cpus != self._cpus_shown:
            self._cpus_shown = cpus
            self.spin_threads.setToolTip(self._threads_tip(cpus))
        gov = st.get("governor") or {}
        if gov.get("active"):
            self.chk_hold.setToolTip("Гувернатор держит потолок "
                                     f"{gov.get('pct')} % (цель {gov.get('target')} °C). "
                                     + str(gov.get("msg") or ""))
        self.lbl_state.setText("на связи" if not self.running else "работает")
        self.lbl_state.setStyleSheet("color:#3f9e63;font-size:11px;" if self.running
                                     else "color:#6c7681;font-size:11px;")
        if first:
            self._log("PC2: связь есть")
            self._sync_governor()
            if self._autostart_pending and not self.running:
                self._autostart_pending = False
                self.chk_on.setChecked(True)

    def _offline(self, err=None) -> None:
        was = self.online
        self.online = False
        self.lbl_state.setText("нет связи")
        self.lbl_state.setStyleSheet("color:#c06060;font-size:11px;")
        if was:
            self._log(f"PC2: нет связи — {err}")
        self.lbl_live.setToolTip(str(err or ""))
        self.lbl_detail.setText("")

    def _sync_on(self) -> None:
        if self.chk_on.isChecked() != self.running:
            self.chk_on.blockSignals(True)
            self.chk_on.setChecked(self.running)
            self.chk_on.blockSignals(False)

    # --- действия ----------------------------------------------------------
    def _run(self, what: str, fn, after=None) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_controls(False)
        self._log(f"PC2: {what}…")
        box: dict = {}

        def job() -> None:
            try:
                box["r"] = fn()
            except Exception as e:      # noqa: BLE001
                box["e"] = e

        def done(_r=None) -> None:
            self._busy = False
            self._set_controls(True)
            if "e" in box:
                self.online = False
                self._log(f"PC2: {what} не вышло — {box['e']}")
                self._sync_on()
                return
            msg = (box.get("r") or {}).get("msg")
            if msg:
                self._log(f"PC2: {msg}")
            if after is not None:
                after()
            self._poll()

        asyncjob.run_bg(job, done, owner=self)

    def _set_controls(self, on: bool) -> None:
        for w in (self.chk_on, self.spin_threads, self.spin_load,
                  self.chk_hold, self.chk_boost, self.spin_temp):
            w.setEnabled(on)
        if on:
            self._sync_hold_ui()

    def on_toggle(self, on: bool) -> None:
        if self._busy:
            return
        if on:
            self._run("запуск", lambda: self.client.call(
                "start", threads=int(self.spin_threads.value()), timeout=40),
                after=self._started)
        else:
            self.running = False
            self._run("остановка", lambda: self.client.call("stop", timeout=40),
                      after=self._stopped)

    def _started(self) -> None:
        self.running = True
        self._sync_on()
        self._sync_governor()

    def _stopped(self) -> None:
        self.running = False
        self._sync_on()

    def _on_threads_edit(self, value: int) -> None:
        self._save("threads", int(value))
        if self.running:
            self._threads_timer.start(600)

    def on_threads(self) -> None:
        value = int(self.spin_threads.value())
        if not self.running or self._busy:
            return

        def job() -> dict:
            try:
                return self.client.call("threads", n=value, timeout=15)
            except RemoteError:
                # API не смог — как локальная карточка, перезапускаем
                self.client.call("stop", timeout=30)
                return self.client.call("start", threads=value, timeout=40)

        self._run(f"потоки -> {value}", job)

    def _on_load_edit(self, value: int) -> None:
        self._save("load", int(value))
        if self.running:
            self._load_timer.start(350)

    def on_load(self) -> None:
        value = int(self.spin_load.value())
        if not self.running or self._busy:
            return
        self._run(f"нагрузка {value} %",
                  lambda: self.client.call("load", pct=value, timeout=15))

    def on_hold(self, on: bool) -> None:
        self._save("hold_temp", bool(on))
        self._sync_hold_ui()
        self._log(f"PC2: температуру {'держу' if on else 'не держу'}")
        if self.running or not on:
            self._sync_governor()

    def on_target(self, value: float) -> None:
        self._save("max_temp", float(value))
        if self.running and self.chk_hold.isChecked():
            self._sync_governor()

    def on_allow(self, on: bool) -> None:
        self._save("allow_100", bool(on))
        if self.running and self.chk_hold.isChecked():
            self._sync_governor()

    def _sync_governor(self) -> None:
        if not self.online:
            return
        active = bool(self.running and self.chk_hold.isChecked())
        target = float(self.spin_temp.value())
        allow = bool(self.chk_boost.isChecked())

        def job() -> None:
            try:
                self.client.call("governor", active=active, target=target,
                                 allow100=allow, timeout=10)
            except RemoteError as e:
                self._log(f"PC2: гувернатор — {e}")

        threading.Thread(target=job, name="remote-gov", daemon=True).start()

    def autostart_if_needed(self) -> None:
        if self.chk_auto.isChecked():
            self._autostart_pending = True
            self._log("PC2: автозапуск раздела сработал")
            if self.online:
                self._autostart_pending = False
                self.chk_on.setChecked(True)
        else:
            self._log("PC2: автозапуск раздела выключен")

    def shutdown(self) -> None:
        self._timer.stop()
        try:
            self.client.close()
        except Exception:               # noqa: BLE001 — выход не должен падать
            pass
