"""Уборка фона на ночь и пауза автообновлений Windows.

!!! ГЛАВНОЕ: ФУНКЦИЯ ЭКСПЕРИМЕНТАЛЬНАЯ, КРАЙНЕ НЕСТАБИЛЬНАЯ И ВЫКЛЮЧЕНА. !!!

Галочка «Ночь: закрыть всё лишнее» в интерфейсе заблокирована, `Cleaner`
работает только как «снять следы прошлой паузы» (`restore`). Причина — на
практике этот код роняет даже само приложение-майнер: `TerminateProcess` по
чужим процессам, остановка служб `wuauserv`/`UsoSvc`/`DoSvc` и гонки с треем
приводят к падению GUI, а вместе с ним и майнинга. Прежде чем включать,
функцию нужно переписать (без убийства процессов, с dry-run и тестами) —
сейчас смысла активировать её нет.

Что задумывалось закрывать:
  * приложения с видимыми окнами — **мягко** (`WM_CLOSE`): приложение само решает,
    спросить про сохранение или отказаться; принудительно такие не убиваются;
  * только явный список фоновых (апдейтеры, синхронизации, лаунчеры, треи) —
    `TerminateProcess`: там нечему теряться.

Чего не касаемся никогда: системные процессы (explorer/dwm/svchost/...), майнеры,
DSH, python-процессы приложения, редакторы документов (Word/Excel/Notepad/IDE),
OBS и виртуальные машины — в них могут быть несохранённые данные.

Автообновления Windows: политика `NoAutoUpdate=1` в
`HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU` плюс остановка
`wuauserv`/`UsoSvc`/`DoSvc`. Всё обратимо без перезагрузки: значение политики
удаляется, службы поднимаются. Состояние паузы лежит в `data/wu-pause.json`,
чтобы после падения приложения обновления не остались выключенными.
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

from . import paths

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_TERMINATE = 0x0001
WM_CLOSE = 0x0010
MAX_PATH = 260

SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_CONTROL_STOP = 0x00000001
SERVICE_STOPPED = 1
SERVICE_RUNNING = 4

ASK_EVERY_S = 1800.0      # одному и тому же окну просим закрыться не чаще раза в 30 мин
KILL_REMEMBER_S = 1800.0  # помним закрытый pid, чтобы не писать о нём каждую минуту


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
    ]


# --- списки ---------------------------------------------------------------
# Система, майнеры, DSH и то, без чего Windows не живёт. Не закрываем никогда.
PROTECTED = {
    "system", "registry", "memory compression", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "dwm.exe", "fontdrvhost.exe", "ctfmon.exe", "sihost.exe", "taskhostw.exe",
    "runtimebroker.exe", "dllhost.exe", "conhost.exe", "audiodg.exe",
    "spoolsv.exe", "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe", "searchindexer.exe", "searchprotocolhost.exe",
    "searchfilterhost.exe", "wmiprvse.exe", "werfault.exe", "wudfhost.exe",
    "tiworker.exe", "trustedinstaller.exe", "msiexec.exe",
    "explorer.exe", "shellhost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "textinputhost.exe",
    "crossdeviceresume.exe", "applicationframehost.exe", "lockapp.exe",
    "logonui.exe", "consent.exe",
    "krig-miner.exe", "xmrig.exe",
    "powershell.exe", "pwsh.exe", "cmd.exe", "wscript.exe", "cscript.exe",
    "tasklist.exe", "taskkill.exe", "schtasks.exe", "sc.exe", "net.exe",
    "net1.exe", "reg.exe", "nvidia-smi.exe", "nvcontainer.exe",
    "nvidia app.exe", "nvsphelper64.exe",
}
# Семейства, которые нельзя гасить даже по имени: это или наше окружение
# (python/deepseek), или майнер с другим именем образа.
PROTECTED_PREFIX = ("python", "deepseek", "dsh")

# Приложения с окнами, в которых могут быть несохранённые данные: не трогаем
# вообще (ни мягко, ни принудительно).
NEVER = {
    "taskmgr.exe", "notepad.exe", "notepad++.exe", "wordpad.exe",
    "winword.exe", "excel.exe", "powerpnt.exe", "msaccess.exe", "outlook.exe",
    "acrord32.exe", "acrobat.exe", "photoshop.exe", "illustrator.exe",
    "audacity.exe", "reaper.exe",
    "code.exe", "pycharm64.exe", "pycharm.exe", "idea64.exe", "devenv.exe",
    "sublime_text.exe",
    "obs64.exe", "obs32.exe",
    "bluestacks.exe", "bluestacksplayer.exe", "hd-player.exe",
    "vmware.exe", "vmware-vmx.exe", "virtualbox.exe", "virtualboxvm.exe",
    "vmplayer.exe",
}

# Фоновые, которые можно гасить принудительно: документов они не держат.
KILL = {
    # апдейтеры и телеметрия
    "googleupdate.exe", "googleupdateondemand.exe", "edgeupdate.exe",
    "microsoftedgeupdate.exe", "microsoftedgeupdatem.exe", "jusched.exe",
    "jucheck.exe", "adobearm.exe", "armsvc.exe", "adobeipcbroker.exe",
    "ccxprocess.exe", "officeclicktorun.exe", "msoia.exe",
    # синхронизация облаков
    "onedrive.exe", "dropbox.exe", "googledrivefs.exe", "yandexdisk.exe",
    "icloud.exe", "icloudservices.exe", "mega.exe",
    # треи, оверлеи, подсветка
    "vgtray.exe", "rtss.exe", "lghub.exe", "lghub_agent.exe", "icue.exe",
    "signalrgb.exe", "openrgb.exe", "wallpaperengine.exe",
    "nvidia broadcast.exe",
    # лаунчеры и их фоновые части
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe",
    "epicwebhelper.exe", "battle.net.exe", "riotclientservices.exe",
    "riotclientux.exe", "riotclientcrashhandler.exe", "eadesktop.exe",
    "origin.exe", "eawebhelper.exe", "ubisoftconnect.exe", "upc.exe",
    "galaxyclient.exe", "galaxyclienthelper.exe",
}

# Приложения, которые в фоне (без окна, в трее) можно гасить принудительно:
# если окно есть — закрываем мягко, чтобы не потерять введённое в формы.
KILL_IF_WINDOWLESS = {
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
    "vivaldi.exe", "yandex.exe", "chromium.exe", "telegram.exe", "discord.exe",
    "whatsapp.exe", "slack.exe", "teams.exe", "ms-teams.exe", "spotify.exe",
    "skype.exe",
}

# Окна, которые не считаем «приложением пользователя» (панель задач, обои).
SKIP_WINDOW_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "DV2ControlHost", "SysShadow", "TaskListThumbnailWnd",
    "tooltips_class32", "#32768",
}

AU_KEY = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"


def classify(name: str) -> str:
    """`protected` | `never` | `kill` | `window` — что делать с процессом."""
    n = (name or "").strip().lower()
    if not n:
        return "protected"
    if n in PROTECTED:
        return "protected"
    if any(n.startswith(p) for p in PROTECTED_PREFIX):
        return "protected"
    if n in NEVER:
        return "never"
    if n in KILL:
        return "kill"
    return "window"


def kill_without_window(name: str) -> bool:
    """Фоновая копия браузера/чата (без окна) — можно гасить принудительно."""
    return (name or "").strip().lower() in KILL_IF_WINDOWLESS


# --- процессы -------------------------------------------------------------
def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    k.Process32NextW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    k.CloseHandle.restype = wintypes.BOOL
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.OpenProcess.restype = wintypes.HANDLE
    k.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k.TerminateProcess.restype = wintypes.BOOL
    return k


def list_processes() -> list[tuple[int, str]]:
    """(pid, имя образа) без `tasklist` и без окна консоли."""
    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    out: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return out


def terminate(pid: int) -> bool:
    """Жёстко завершить процесс; False — не вышло (права/уже вышел)."""
    if pid <= 4:
        return False
    k = _kernel32()
    h = k.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(k.TerminateProcess(h, 1))
    finally:
        k.CloseHandle(h)


def windows_by_pid() -> dict[int, list[int]]:
    """pid → видимые top-level окна (панель задач и обои не считаются)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (enum_proc, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    result: dict[int, list[int]] = {}

    def cb(hwnd, _lparam) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        if cls.value in SKIP_WINDOW_CLASSES:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            result.setdefault(int(pid.value), []).append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc(cb), 0)
    return result


def close_windows(hwnds: list[int]) -> int:
    """Мягкая просьба закрыться (WM_CLOSE). Приложение само решает."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)
    user32.PostMessageW.restype = wintypes.BOOL
    sent = 0
    for hwnd in hwnds:
        if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            sent += 1
    return sent


# --- службы Windows -------------------------------------------------------
def _advapi32():
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    a.OpenSCManagerW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    a.OpenSCManagerW.restype = wintypes.HANDLE
    a.OpenServiceW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD)
    a.OpenServiceW.restype = wintypes.HANDLE
    a.QueryServiceStatus.argtypes = (wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS))
    a.QueryServiceStatus.restype = wintypes.BOOL
    a.ControlService.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                 ctypes.POINTER(SERVICE_STATUS))
    a.ControlService.restype = wintypes.BOOL
    a.StartServiceW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                ctypes.POINTER(wintypes.LPCWSTR))
    a.StartServiceW.restype = wintypes.BOOL
    a.CloseServiceHandle.argtypes = (wintypes.HANDLE,)
    a.CloseServiceHandle.restype = wintypes.BOOL
    return a


def _open_service(name: str, access: int):
    a = _advapi32()
    scm = a.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        return None
    svc = a.OpenServiceW(scm, name, access)
    a.CloseServiceHandle(scm)
    return svc or None


def service_state(name: str) -> int | None:
    """Состояние службы (1 — остановлена, 4 — работает), None — не открылась."""
    a = _advapi32()
    svc = _open_service(name, SERVICE_QUERY_STATUS)
    if not svc:
        return None
    try:
        st = SERVICE_STATUS()
        if not a.QueryServiceStatus(svc, ctypes.byref(st)):
            return None
        return int(st.dwCurrentState)
    finally:
        a.CloseServiceHandle(svc)


def stop_service(name: str) -> tuple[bool, str]:
    a = _advapi32()
    svc = _open_service(name, SERVICE_QUERY_STATUS | SERVICE_STOP)
    if not svc:
        return False, "служба не открылась (нужны права администратора)"
    try:
        st = SERVICE_STATUS()
        if not a.QueryServiceStatus(svc, ctypes.byref(st)):
            return False, "состояние не прочиталось"
        if st.dwCurrentState == SERVICE_STOPPED:
            return True, "уже остановлена"
        if not a.ControlService(svc, SERVICE_CONTROL_STOP, ctypes.byref(st)):
            return False, f"stop не принят (код {ctypes.get_last_error()})"
        for _ in range(8):
            time.sleep(0.5)
            if a.QueryServiceStatus(svc, ctypes.byref(st)) and \
                    st.dwCurrentState == SERVICE_STOPPED:
                return True, "остановлена"
        return False, "не остановилась за 4 с"
    finally:
        a.CloseServiceHandle(svc)


def start_service(name: str) -> tuple[bool, str]:
    a = _advapi32()
    svc = _open_service(name, SERVICE_QUERY_STATUS | SERVICE_START)
    if not svc:
        return False, "служба не открылась (нужны права администратора)"
    try:
        st = SERVICE_STATUS()
        if a.QueryServiceStatus(svc, ctypes.byref(st)) and \
                st.dwCurrentState == SERVICE_RUNNING:
            return True, "уже работает"
        if a.StartServiceW(svc, 0, None):
            return True, "запущена"
        err = ctypes.get_last_error()
        if err == 1056:                    # ERROR_SERVICE_ALREADY_RUNNING
            return True, "уже работает"
        return False, f"запуск не принят (код {err})"
    finally:
        a.CloseServiceHandle(svc)


# --- политика автообновлений ----------------------------------------------
def _au_value() -> int | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, AU_KEY) as k:
            return int(winreg.QueryValueEx(k, "NoAutoUpdate")[0])
    except (OSError, ValueError):
        return None


def _au_set(value: int) -> bool:
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, AU_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "NoAutoUpdate", 0, winreg.REG_DWORD, int(value))
        return True
    except OSError:
        return False


def _au_clear() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, AU_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "NoAutoUpdate")
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


class UpdatePauser:
    """Обратимая пауза автообновлений: политика + три службы, без перезагрузки."""

    SERVICES = ("wuauserv", "UsoSvc", "DoSvc")

    def __init__(self, data_dir: Path | None = None, on_msg=print) -> None:
        self.state_path = Path(data_dir or paths.DATA) / "wu-pause.json"
        self.on_msg = on_msg

    def _load_state(self) -> dict | None:
        try:
            with self.state_path.open(encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save_state(self, state: dict) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.state_path.open("w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except OSError as e:
            self.on_msg(f"состояние паузы обновлений не записалось: {e}")

    def pause(self, alive=None) -> None:
        """Включить паузу. `alive()` проверяется между службами: галочку могли снять."""
        if alive is not None and not alive():
            return
        state = self._load_state()
        if state is None:
            old = _au_value()
            running = {s: service_state(s) == SERVICE_RUNNING for s in self.SERVICES}
            state = {"old": old, "created": old is None, "running": running}
            self._save_state(state)
        if _au_set(1):
            self.on_msg("обновления Windows: NoAutoUpdate=1 (вернётся при снятии галочки)")
        else:
            self.on_msg("обновления Windows: политику не записать (нужны права админа)")
        for name in self.SERVICES:
            if alive is not None and not alive():
                return
            if service_state(name) == SERVICE_RUNNING:
                ok, msg = stop_service(name)
                self.on_msg(f"обновления Windows: {name} — {msg}")
                if not ok:
                    break

    def resume(self) -> None:
        state = self._load_state()
        if state is None:
            return
        if state.get("created"):
            _au_clear()
        elif state.get("old") is not None:
            _au_set(int(state["old"]))
        self.on_msg("обновления Windows: пауза снята, политика восстановлена")
        for name, was_running in (state.get("running") or {}).items():
            if was_running and service_state(name) != SERVICE_RUNNING:
                ok, msg = start_service(name)
                self.on_msg(f"обновления Windows: {name} — {msg}")
        try:
            self.state_path.unlink()
        except OSError:
            pass

    def restore_if_stale(self) -> None:
        """Прошлый запуск мог умереть с включённой уборкой — вернуть обновления."""
        if self._load_state() is not None:
            self.on_msg("обновления были на паузе с прошлого запуска — возвращаю")
            self.resume()

    def ensure(self) -> None:
        """Раз в такт: политика на месте, службы остановлены."""
        if self._load_state() is None:
            return
        if _au_value() != 1:
            _au_set(1)
        for name in self.SERVICES:
            if service_state(name) == SERVICE_RUNNING:
                stop_service(name)


class Cleaner:
    """Периодическая уборка: закрыть лишнее, не тронув майнер, DSH и документы."""

    def __init__(self, on_msg=print, data_dir: Path | None = None) -> None:
        self.on_msg = on_msg
        self.updates = UpdatePauser(data_dir, on_msg)
        self.enabled = False
        self._asked: dict[int, float] = {}
        self._killed: dict[int, float] = {}
        self._lock = threading.RLock()

    def set_enabled(self, on: bool) -> None:
        if bool(on) == self.enabled:
            return
        self.enabled = bool(on)
        if self.enabled:
            # пауза служб занимает до нескольких секунд — окно не должно ждать
            threading.Thread(target=self._enable, name="cleanup-on", daemon=True).start()
        else:
            self._asked.clear()
            self.updates.resume()      # старт служб неблокирующий, ждать нечего

    def _enable(self) -> None:
        with self._lock:
            self.updates.pause(alive=lambda: self.enabled)
            if not self.enabled:          # галочку сняли, пока шла пауза служб
                self.updates.resume()
                return
            self.tick()

    def restore(self) -> None:
        """Вернуть обновления, если прошлый запуск оставил их на паузе."""
        self.updates.restore_if_stale()

    def tick(self) -> None:
        with self._lock:
            self._tick_locked()

    def _tick_locked(self) -> None:
        if not self.enabled:
            return
        self.updates.ensure()
        now = time.time()
        procs = list_processes()
        windows = windows_by_pid()
        killed: list[str] = []
        asked = 0
        for pid, name in procs:
            if not self.enabled:            # галочку сняли, пока шла уборка
                return
            if pid <= 4 or pid == os.getpid():
                continue
            kind = classify(name)
            if kind in ("protected", "never"):
                continue
            hwnds = windows.get(pid)
            if kind == "kill" or (not hwnds and kill_without_window(name)):
                if pid in self._killed:
                    continue
                if terminate(pid):
                    self._killed[pid] = now
                    killed.append(name.lower())
                continue
            if not hwnds:
                continue
            if now - self._asked.get(pid, 0.0) < ASK_EVERY_S:
                continue
            if close_windows(hwnds):
                self._asked[pid] = now
                asked += 1
        cutoff = now - KILL_REMEMBER_S
        self._killed = {p: t for p, t in self._killed.items() if t >= cutoff}
        self._asked = {p: t for p, t in self._asked.items() if t >= cutoff}
        if killed:
            self.on_msg("уборка: закрыл " + ", ".join(sorted(set(killed))))
        if asked:
            self.on_msg(f"уборка: попросил мягко закрыться ({asked})")
