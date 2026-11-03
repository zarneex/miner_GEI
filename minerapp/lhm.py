"""Мост датчиков CPU — поднимает само приложение, скрытым дочерним процессом.

Температуру, пакетную мощность и частоту процессора на этой плате отдаёт только
LibreHardwareMonitor со своим драйвером (`W:\\mining\\tools\\lhmd.ps1`), которому
нужны права администратора. Раньше мост стартовала задача планировщика при входе
в систему — на экране мигало окно PowerShell, и это раздражало.

Теперь окна нет: приложение запускает мост как дочерний процесс с
`CREATE_NO_WINDOW` и `-WindowStyle Hidden`, следит за ним и поднимает заново,
если тот упал. Если приложение запущено без прав администратора (тогда драйвер
LHM не загрузится), используется скрытая задача `MinerCPUSensor` без триггеров —
её можно запустить только по требованию, окно не появляется.
"""
from __future__ import annotations

import ctypes
import subprocess
import time
from pathlib import Path

from . import paths

POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
SCRIPT = paths.ROOT / "tools" / "lhmd.ps1"
TASK = "MinerCPUSensor"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
RETRY_S = 20.0          # не чаще одной попытки поднять мост в 20 с
GRACE_S = 25.0          # только запустился — даём время на инициализацию драйвера


def admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


class LhmBridge:
    """Жизненный цикл моста CPU: сам запуск, сторож, остановка при выходе."""

    def __init__(self, on_msg=None) -> None:
        self._proc: subprocess.Popen | None = None
        self._msg = on_msg or (lambda *_a: None)
        self._last_try = 0.0
        self._started_at = 0.0
        self._direct_fails = 0
        self._task_started = False

    # --- состояние -------------------------------------------------------
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def pid(self) -> int:
        return self._proc.pid if self.running() else 0

    # --- тик сторожа ------------------------------------------------------
    def tick(self, bridge_alive: bool) -> None:
        """Вызывается с каждым кадром датчиков: `bridge_alive` — cpu.json свежий."""
        now = time.time()
        if self.running():
            if now - self._started_at < GRACE_S:
                return
            if not bridge_alive and now - self._last_try > RETRY_S:
                self._last_try = now
                self._msg("мост CPU не пишет данные — запускаю заново")
                self._kill()
            return
        if bridge_alive:
            return
        if now - self._last_try < RETRY_S:
            return
        self._last_try = now
        if self._proc is not None:          # процесс был, но завершился
            code = self._proc.returncode
            self._proc = None
            self._direct_fails += 1
            self._msg(f"мост CPU завершился (код {code}) — пробую снова")
        self._spawn()

    # --- запуск -----------------------------------------------------------
    def _spawn(self) -> None:
        if not SCRIPT.exists():
            self._msg(f"мост CPU не найден: {SCRIPT}")
            return
        if admin() and self._direct_fails < 2:
            if self._launch_direct():
                return
        self._launch_task()

    def _launch_direct(self) -> bool:
        args = [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT)]
        try:
            self._proc = subprocess.Popen(
                args, cwd=str(SCRIPT.parent), creationflags=NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._msg(f"мост CPU не запустился: {e}")
            return False
        self._started_at = time.time()
        self._task_started = False
        self._msg(f"мост CPU запущен скрыто, pid={self._proc.pid} (датчики CPU живые)")
        return True

    def _launch_task(self) -> None:
        """Запасной путь: скрытая задача без триггеров (нужна, если нет прав)."""
        try:
            r = subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True,
                               text=True, timeout=20, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            self._msg(f"мост CPU: задача {TASK} не запустилась: {e}")
            return
        if r.returncode == 0:
            self._task_started = True
            self._started_at = time.time()
            self._msg(f"мост CPU запущен задачей {TASK} (окно не появляется)")
        else:
            self._msg(f"мост CPU недоступен: {(r.stdout + r.stderr).strip()[:80]}")

    # --- остановка --------------------------------------------------------
    def _kill(self) -> None:
        pid = self.pid()
        if not pid:
            return
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           text=True, timeout=15, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
        self._proc = None

    def stop(self) -> None:
        if self.running():
            self._kill()
            self._msg("мост CPU остановлен")
        if self._task_started:
            try:
                subprocess.run(["schtasks", "/End", "/TN", TASK], capture_output=True,
                               text=True, timeout=15, creationflags=NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                pass
            self._task_started = False
