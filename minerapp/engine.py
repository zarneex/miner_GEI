"""Движок: запуск майнеров через QProcess с потоковым выводом в консоль.

Использует тот же код сборки команды, что и CLI (`W:\\mining\\scripts\\minerctl.py`),
чтобы пулы, кошелёк и пресеты не разъезжались. Мягкая остановка обязательна: после
`taskkill /F` драйвер остаётся с чужими смещениями клоков.
"""
import json
import re
import secrets
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QThread, Signal

from . import asyncjob, cpumon, gpu, paths
from .cpu_limit import CpuLimiter
from .paths import CPU_TOTAL_THREADS, KRIG, LOGS, STATE, XMRIG, ensure_engine_path
from .xmrig_api import XmrigApi

ensure_engine_path()
import minerctl  # noqa: E402  (движок майнинга, W:\mining\scripts)

XMRIG_SPEED = re.compile(r"speed\s+10s/60s/15m\s+([\d.]+)")
XMRIG_HTTP_PORT = 12002


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return str(pid) in out
    except (OSError, subprocess.SubprocessError):
        return False


def soft_stop(pid: int, grace: float = 3.0, hard: bool = True) -> bool:
    """Мягкая остановка одного процесса по pid — без слепого ожидания.

    Сначала `taskkill` без `/F`: так гасятся процессы с окном (они успевают
    сохранить состояние и снять разгон). Консольный процесс Windows мягкий сигнал
    не принимает — taskkill сразу отвечает «only forcefully»; ждать тогда нечего,
    и через короткую паузу идёт `/F` (см. замер: python.exe, `taskkill /T` → rc=1,
    процесс жив).

    Возвращает True, если процесс ушёл без `/F`.
    """
    if not pid or not _pid_alive(pid):
        return True
    flags = subprocess.CREATE_NO_WINDOW
    res = subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True,
                         text=True, timeout=20, creationflags=flags)
    accepted = res.returncode == 0          # мягкий сигнал доставлен
    deadline = time.time() + (grace if accepted else min(grace, 0.6))
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    if not _pid_alive(pid):
        return True
    if not hard:
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   text=True, timeout=20, creationflags=flags)
    deadline = time.time() + 5.0
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    return False


def _kill_soft(pid: int, grace: float = 8.0, hard_first: bool = False) -> None:
    """Мягкий стоп: сначала taskkill без /F (майнер сам снимает разгон), потом /F.

    `hard_first` нужен XMRig: ему снимать нечего, а мягкий стоп на консольном
    процессе тянется всю паузу grace — перезапуск на других потоках ждал 8 с.
    """
    if not pid or not _pid_alive(pid):
        return
    run = subprocess.run
    flags = subprocess.CREATE_NO_WINDOW
    if hard_first:
        run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True,
            timeout=20, creationflags=flags)
        return
    run(["taskkill", "/PID", str(pid), "/T"],
        capture_output=True, text=True, timeout=20, creationflags=flags)
    deadline = time.time() + grace
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.4)
    if _pid_alive(pid):
        run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True,
            timeout=20, creationflags=flags)


class _Miner(QObject):
    line = Signal(str)
    state_changed = Signal(bool)

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self.name = name
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        self.proc.started.connect(self._started)
        self._buf = ""
        self._pid = 0
        self._quiet_stop = False
        self.expect_stop = False
        self.soft_stop = True     # сначала мягкий taskkill (майнер сам снимает разгон)

    # --- QProcess ---------------------------------------------------------
    def _read(self) -> None:
        data = self.proc.readAllStandardOutput().data().decode("utf-8", "replace")
        self._buf += data
        *lines, self._buf = self._buf.split("\n")
        for ln in lines:
            ln = ln.rstrip()
            if ln:
                self.on_line(ln)

    def on_line(self, ln: str) -> None:
        self.line.emit(f"[{self.name}] {ln}")

    def _finished(self, code: int, _status) -> None:
        self._pid = 0
        self.state_changed.emit(False)
        if not self.expect_stop:
            self.line.emit(f"[{self.name}] процесс завершился, код {code}")

    def _error(self, err) -> None:
        self.line.emit(f"[{self.name}] ошибка запуска: {err}")

    def is_running(self) -> bool:
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def pid(self) -> int:
        return self._pid

    def _spawn(self, program: Path, args: list[str], workdir: Path) -> None:
        self.expect_stop = False
        LOGS.mkdir(parents=True, exist_ok=True)
        if self.proc.state() != QProcess.ProcessState.NotRunning:
            # Qt считает процесс ещё живым (сигнал finished не дошёл) — start() тогда
            # молча отвергается, и перезапуск на других потоках «не происходит».
            self.proc.kill()
            self.proc.waitForFinished(3000)
        self.proc.setProgram(str(program))
        self.proc.setArguments(args)
        self.proc.setWorkingDirectory(str(workdir))
        self.proc.start()      # без waitForStarted: ждём сигнал started, окно живо

    def _started(self) -> None:
        self._pid = int(self.proc.processId())
        self.state_changed.emit(True)
        self.line.emit(f"[{self.name}] запущен, pid={self._pid}")
        self.on_started()

    def on_started(self) -> None:
        """Хук: что сделать после реального старта (в GUI-потоке)."""

    def on_stop_bg(self) -> None:
        """Хук: что сделать в фоне после выхода процесса (сброс клоков, политики)."""

    def stop(self, quiet: bool = False, wait: bool = False, on_done=None) -> None:
        """Остановка не держит окно: taskkill и ожидание выхода — в фоновом потоке.

        `wait=True` — синхронный путь, нужен только при закрытии приложения.
        """
        self.expect_stop = True
        self._quiet_stop = quiet
        pid = self._pid
        if wait:
            if pid:
                try:
                    _kill_soft(pid, hard_first=not self.soft_stop)
                except (OSError, subprocess.SubprocessError):
                    pass
            self._finish_stop(quiet, wait=True)
            self.on_stop_bg()
            if on_done:
                on_done()
            return

        def job() -> None:
            if pid:
                try:
                    _kill_soft(pid, hard_first=not self.soft_stop)
                except (OSError, subprocess.SubprocessError):
                    pass
            self.on_stop_bg()

        def after(_result: object = None) -> None:
            self._finish_stop(quiet)
            if on_done:
                on_done()

        asyncjob.run_bg(job, after, owner=self)

    def _finish_stop(self, quiet: bool, wait: bool = False) -> None:
        self._pid = 0
        try:
            if wait:
                self.proc.kill()
                self.proc.waitForFinished(3000)
            elif self.proc.state() != QProcess.ProcessState.NotRunning:
                self.proc.kill()
                # ждём, пока Qt действительно увидит выход: иначе следующий start()
                # отвергнется и раздел молча останется выключенным
                self.proc.waitForFinished(3000)
        except RuntimeError:
            pass
        self.state_changed.emit(False)
        if not quiet:
            self.line.emit(f"[{self.name}] остановлен")

    def settle_stop(self, quiet: bool = True) -> None:
        """Процесс уже погашен извне (`soft_stop` в фоне) — привести объект в порядок.

        `kill()` на мёртвом процессе — no-op, поэтому ожидание `waitForFinished`
        длится миллисекунды; зато следующий `start()` не будет отвергнут, а хвосты
        (state.json, политика кулера, лимитер CPU) снимаются тем же хуком.
        """
        self.expect_stop = True
        self._quiet_stop = quiet
        try:
            if self.proc.state() != QProcess.ProcessState.NotRunning:
                self.proc.kill()
                self.proc.waitForFinished(2000)
        except RuntimeError:
            pass
        self._pid = 0
        self.on_stop_bg()
        self.state_changed.emit(False)



class KrigMiner(_Miner):
    """GPU-майнер Krig 1.5.13 (Pearl PRL / Quantus QTC)."""

    def __init__(self, parent=None) -> None:
        super().__init__("GPU", parent)
        self.coin = ""
        self.port = 12000
        self.preset: dict = {}
        self.hashrate: float | None = None
        self.fan_fixed = False

    def start(self, coin: str, preset: dict) -> None:
        cfg = minerctl.load_cfg()
        cmd = minerctl.build_cmd(coin, cfg, preset)
        # страховка от прошлого жёсткого убийства: майнер мог оставить фикс. обороты
        if not preset.get("fan") and gpu.fan_policy() == gpu.FAN_POLICY_FIXED:
            _, msg = gpu.reset_fan_policy()
            self.line.emit(f"[GPU] остались фиксированные обороты с прошлого запуска: {msg}")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        logfile = LOGS / f"{coin}-gui-{stamp}.log"
        args = cmd[1:] + ["--log-file", str(logfile)]
        self.coin = coin
        self.port = int(cfg["coins"][coin]["api_port"])
        self.preset = dict(preset)
        self.hashrate = None
        self.fan_fixed = bool(preset.get("fan"))
        self._logfile = logfile
        self._spawn(KRIG, args, KRIG.parent)

    def on_started(self) -> None:
        self.line.emit(f"[GPU] монета {self.coin}, лимит {self.preset.get('plimit')} Вт, "
                       f"память {self.preset.get('moffset') or 0:+d}")
        try:
            with open(STATE, "w", encoding="utf-8") as f:
                json.dump({"pid": self._pid, "coin": self.coin, "started": time.time(),
                           "log": str(self._logfile), "preset": self.preset}, f,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass

    def set_hashrate(self, hr: float | None) -> None:
        self.hashrate = hr

    def on_stop_bg(self) -> None:
        """После выхода krig: забыть pid, вернуть авто-обороты и заводские клоки."""
        self.hashrate = None
        try:
            with open(STATE, "w", encoding="utf-8") as f:
                json.dump({}, f)
        except OSError:
            pass
        if self.fan_fixed:
            _ok, msg = gpu.reset_fan_policy()
            self.fan_fixed = False
            self.line.emit(f"[GPU] {msg}")
        if not self._quiet_stop:
            gpu.reset_clocks()


class CpuMiner(_Miner):
    """CPU-майнер XMRig 6.26.0 (Monero RandomX).

    Две независимые точки контроля нагрузки:
      * число потоков — `set_threads()` через HTTP API: XMRig перезапускает воркеры
        внутри процесса (uptime не сбрасывается), датасет RandomX остаётся;
      * доля процессора в процентах — `set_load()` через Job Object: ядро не даёт
        процессу израсходовать больше N % времени всех логических CPU.
    """

    def __init__(self, parent=None) -> None:
        super().__init__("CPU", parent)
        self.threads = 0
        self.hashrate: float | None = None
        self.load_pct = 100
        self.limiter = CpuLimiter()
        self.api: XmrigApi | None = None
        self._token = ""

    @staticmethod
    def affinity_mask(threads: int) -> int:
        """Младшие ядра остаются системе: занимаем старшие N потоков."""
        n = max(1, min(threads, CPU_TOTAL_THREADS))
        return ((1 << n) - 1) << (CPU_TOTAL_THREADS - n)

    def start(self, threads: int) -> None:
        cfg = minerctl.load_cfg()
        x = cfg["xmrig"]
        mask = self.affinity_mask(threads)
        token = secrets.token_hex(8)
        args = ["--coin", "monero", "-o", x["url"], "-u", x["user"], "-p", "x",
                "--tls", "--threads", str(threads),
                "--cpu-affinity", f"0x{mask:X}",
                "--cpu-priority", "1", "-k", "--no-color",
                "--http-port", str(XMRIG_HTTP_PORT), "--http-host", "127.0.0.1",
                "--http-access-token", token, "--http-no-restricted",
                "--log-file", str(LOGS / "xmr-gui.log")]
        self.soft_stop = False    # XMRig разгон карты не держит: мягкий стоп только тянет время
        self.threads = threads
        self.hashrate = None
        self._token = token
        self.api = XmrigApi(XMRIG_HTTP_PORT, token)
        self._spawn(XMRIG, args, XMRIG.parent)
        self.line.emit(f"[CPU] {threads} потоков, маска 0x{mask:X} (старшие ядра), приоритет 1")

    def on_started(self) -> None:
        """Сразу после старта: посадить процесс в Job, если лимит доли CPU включён."""
        if self.load_pct >= 100:
            return
        ok = self.limiter.attach(self.pid(), self.load_pct)
        self.line.emit(f"[CPU] лимит {self.load_pct} % " + (
            "применён (Job Object)" if ok else f"не применён: {self.limiter.last_error}"))

    def set_load(self, pct: int) -> bool:
        """Потолок доли процессора в процентах (20–100), на ходу.

        Меняется повторным SetInformationJobObject: процесс не останавливается,
        хешрейт проседает примерно пропорционально доле.
        """
        pct = int(max(20, min(100, pct)))
        self.load_pct = pct
        if pct >= 100:
            if self.limiter.active:
                self.limiter.detach()
                self.line.emit("[CPU] лимит доли CPU снят (100 %)")
            return True
        if not self.is_running():
            self.line.emit(f"[CPU] лимит {pct} % запомнен — применю при старте")
            return True
        if not self.limiter.active:
            ok = self.limiter.attach(self.pid(), pct)
        else:
            ok = self.limiter.set(pct)
        if ok:
            self.line.emit(f"[CPU] лимит доли CPU: {pct} %")
        else:
            self.line.emit(f"[CPU] лимит {pct} % не применён: {self.limiter.last_error}")
        return ok

    def set_threads(self, value: int) -> bool:
        """Сменить потоки на ходу. False — API не смог (нужен перезапуск)."""
        if not self.is_running() or self.api is None:
            return False
        if not self.api.set_threads(value, self.affinity_mask(value)):
            self.line.emit(f"[CPU] API не поменял потоки: {self.api.last_error}")
            return False
        self.threads = int(value)
        self.line.emit(f"[CPU] потоки -> {value} без перезапуска (HTTP API)")
        return True

    def on_line(self, ln: str) -> None:
        m = XMRIG_SPEED.search(ln)
        if m:
            try:
                self.hashrate = float(m.group(1))
            except ValueError:
                pass
        super().on_line(ln)

    def _finished(self, code: int, status) -> None:
        self.limiter.close()      # job закрывается вместе с процессом — лимит не залипнет
        self.api = None
        super()._finished(code, status)

    def on_stop_bg(self) -> None:
        self.limiter.close()
        self.hashrate = None


class SensorThread(QThread):
    """Опрос датчиков в фоне: nvidia-smi занимает ~150 мс, в GUI-потоке это рывки.

    Интервал меняется на ходу (`set_interval`): в АФК и когда окно свёрнуто опрос
    реже — история всё равно пишется раз в минуту, а ПК не дёргается зря.
    """

    snapshot = Signal(dict)

    def __init__(self, krig: KrigMiner, cpu: CpuMiner, interval: float = 2.5, parent=None):
        super().__init__(parent)
        self.krig = krig
        self.cpu = cpu
        self.interval = interval
        self._stop = False
        self._wake = False
        self.cpumon = cpumon.CpuMonitor()

    def set_interval(self, seconds: float) -> None:
        seconds = max(1.0, float(seconds))
        if seconds < self.interval:
            self._wake = True     # проснулись — опросим сразу, не ждём 20 с
        self.interval = seconds

    def run(self) -> None:
        while not self._stop:
            data = gpu.read()
            hr = None
            if self.krig.is_running():
                try:
                    hr = minerctl.api_hashrate(self.krig.port)
                except Exception:
                    hr = None
                self.krig.set_hashrate(hr)
            data["gpu_hashrate"] = hr
            data["cpu_hashrate"] = self.cpu.hashrate if self.cpu.is_running() else None
            data["gpu_running"] = self.krig.is_running()
            data["cpu_running"] = self.cpu.is_running()
            data.update(self.cpumon.sample(self.cpu.threads))
            self.snapshot.emit(data)
            self._wake = False
            waited = 0.0
            while not self._stop and not self._wake and waited < self.interval:
                self.msleep(100)
                waited += 0.1

    def stop(self) -> None:
        self._stop = True
