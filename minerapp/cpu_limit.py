"""Лимит нагрузки CPU в процентах — через Job Object Windows.

Зачем: число потоков XMRig — грубый рычаг (поток либо есть, либо нет), а нужен
непрерывный. `PROCTHROTTLEMAX` меняет частоту, а не долю загрузки. Единственный
честный «процент» в Windows — Job Object с HARD_CAP: ядро не даёт процессу
(и его детям) израсходовать больше CpuRate сотых процента от ВСЕГО процессора.

Как работает: процесс на время интервала планирования получает квоту времени;
выбрал — стоит до следующего интервала. Поэтому 12 % на 16 потоках ведут себя
как «два потока»: хешрейт падает пропорционально, но дрожание возможно.
Тепловая масса его сглаживает, а хешрейт-метрика должна это учитывать.

Что важно для нас:
  * границы 1..100 % (CpuRate = процент × 100), у нас в UI 20..100 шаг 5;
  * лимит снимается сам, когда закрывается ручка job'а — то есть при выходе
    приложения (даже падении) процесс не останется зарезанным;
  * дети процесса входят в тот же job: XMRig не запускает детей, но на будущее.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

# JOBOBJECTINFOCLASS
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
CPU_RATE_CONTROL_ENABLE = 0x1
CPU_RATE_CONTROL_HARD_CAP = 0x4
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100

HARD_MIN_PCT = 1     # физический минимум API (CpuRate = 100)
UI_MIN_PCT = 20      # ниже — лимитер вырождается в «пару потоков», дрожание съедает смысл
MAX_PCT = 100


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    """winnt.h: DWORD ControlFlags; union { DWORD CpuRate; DWORD Weight; };"""

    class _Value(ctypes.Union):
        _fields_ = [("CpuRate", wintypes.DWORD), ("Weight", wintypes.DWORD)]

    _anonymous_ = ("_value",)
    _fields_ = [("ControlFlags", wintypes.DWORD), ("_value", _Value)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.SetInformationJobObject.restype = wintypes.BOOL
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                         wintypes.LPVOID, wintypes.DWORD]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _err() -> str:
    return f"WinError {ctypes.get_last_error()}"


class CpuLimiter:
    """Job Object с жёстким потолком доли процессора.

    Жизненный цикл: `attach(pid)` — создать job и посадить процесс; `set(pct)` —
    поменять потолок на ходу; `detach()`/`close()` — отпустить (лимит исчезает).
    """

    def __init__(self, kill_on_close: bool = False) -> None:
        self._job = None          # HANDLE
        self._pid = 0
        self.pct = MAX_PCT
        self.kill_on_close = bool(kill_on_close)
        self.last_error = ""

    # --- состояние --------------------------------------------------------
    @property
    def active(self) -> bool:
        return bool(self._job) and self._pid > 0

    def attached_pid(self) -> int:
        return self._pid if self._job else 0

    # --- управление -------------------------------------------------------
    def attach(self, pid: int, pct: int = MAX_PCT) -> bool:
        """Создать job, посадить процесс и сразу поставить потолок."""
        self.close()
        if not pid:
            self.last_error = "нет pid"
            return False
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            self.last_error = f"CreateJobObject: {_err()}"
            return False
        if self.kill_on_close:
            # Процесс умрёт вместе с ручкой job'а: агент на другом ПК не оставит
            # XMRig сиротой, если SSH-канал оборвётся и агента убьют.
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(
                    job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info), ctypes.sizeof(info)):
                self.last_error = f"KILL_ON_JOB_CLOSE: {_err()}"
                _k32.CloseHandle(job)
                return False
        h = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not h:
            self.last_error = f"OpenProcess({pid}): {_err()}"
            _k32.CloseHandle(job)
            return False
        try:
            if not _k32.AssignProcessToJobObject(job, h):
                self.last_error = f"AssignProcessToJobObject: {_err()}"
                _k32.CloseHandle(job)
                return False
        finally:
            _k32.CloseHandle(h)
        self._job, self._pid = job, int(pid)
        self.pct = MAX_PCT
        if int(pct) < MAX_PCT:
            self.set(int(pct))
        return True

    def set(self, pct: int) -> bool:
        """Поменять потолок на ходу. 100 % = ограничение выключено."""
        if not self._job:
            self.last_error = "job не создан"
            return False
        pct = max(HARD_MIN_PCT, min(MAX_PCT, int(pct)))
        info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        if pct >= MAX_PCT:
            info.ControlFlags = 0            # выключить rate control, не снимая job
            info.CpuRate = 0
        else:
            info.ControlFlags = CPU_RATE_CONTROL_ENABLE | CPU_RATE_CONTROL_HARD_CAP
            info.CpuRate = pct * 100
        if not _k32.SetInformationJobObject(self._job, JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            self.last_error = f"SetInformationJobObject({pct}%): {_err()}"
            return False
        self.pct = pct
        return True

    def close(self) -> None:
        """Отпустить процесс: закрыть ручку job'а (лимит снимается ядром)."""
        if self._job:
            _k32.CloseHandle(self._job)
        self._job = None
        self._pid = 0
        self.pct = MAX_PCT

    def detach(self) -> bool:
        """Снять лимит, оставив процесс жить (он продолжит работать без потолка)."""
        ok = True
        if self._job and self.pct < MAX_PCT:
            ok = self.set(MAX_PCT)
        self.close()
        return ok
