"""Владелец настроек карты: маркер владения и уборка за собой (в т.ч. после краша).

Приложение меняет состояние видеокарты: обороты кулера (NVML, политика fixed),
лимит мощности (`nvidia-smi -pl`) и, через майнер, смещения клоков. Штатный выход
всё возвращает сам, но если процесс убили (`taskkill /F`, закрытый терминал,
пропало питание) — на карте остаются чужие настройки: фиксированные обороты,
урезанный лимит, чужие клоки. Это уже случалось: кулер «залипал» на 80 %.

Что делаем вместо «надеемся на аккуратный выход»:

* пока приложение живо, оно держит маркер `data/gpu-owned.json` со своим pid;
* чистильщик (`tools/gpu_cleanup.py`, задача `MinerGPUReset` — при входе в систему
  и раз в 5 минут) видит маркер, проверяет, что pid уже мёртв, снимает наши
  настройки (кулер в авто, клоки и лимит мощности к заводским) и перезапускает
  MSI Afterburner, чтобы тот применил свой пресет;
* приложение при штатном закрытии убирает за собой и удаляет маркер.

Так «после резкого завершения» на карте не остаётся ничего от приложения, а если
ОС перезагрузилась — задача при входе делает то же самое, ещё до старта GUI.
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import time
from pathlib import Path

from . import gpu, paths

MARKER = paths.DATA / "gpu-owned.json"
LOG = paths.DATA / "gpu-cleanup.log"
AB = Path(r"C:\Program Files (x86)\MSI Afterburner\MSIAfterburner.exe")
AB_NAME = "MSIAfterburner.exe"
DEFAULT_PL = 250            # заводской лимит RTX 5070 (175..250)
NO_WINDOW = subprocess.CREATE_NO_WINDOW
_PROCESS_QUERY_LIMITED = 0x1000


def log(text: str, path: Path = LOG) -> None:
    """Строка в журнал уборки: без него «чистильщик сработал» не проверить."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)) == 0:
            return False
        return code.value == 259      # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


# --- маркер владения --------------------------------------------------------
def take(note: str = "") -> None:
    """Отметить, что состояние карты сейчас наше (пишет GUI при старте майнинга)."""
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    data = {"pid": ctypes.windll.kernel32.GetCurrentProcessId(),
            "ts": time.time(), "note": note}
    try:
        MARKER.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def drop() -> None:
    try:
        MARKER.unlink(missing_ok=True)
    except OSError:
        pass


def marker_pid() -> int | None:
    try:
        data = json.loads(MARKER.read_text(encoding="utf-8"))
        return int(data.get("pid") or 0) or None
    except (OSError, ValueError, TypeError):
        return None


# --- сброс и пресет Afterburner --------------------------------------------
def _run(args: list[str], timeout: int = 25) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


STRAY_MINERS = ("krig-miner.exe", "xmrig.exe")


def stop_stray_miners() -> list[str]:
    """Погасить майнеры, оставшиеся от убитого приложения.

    Вызывается только когда маркер есть, а процесс GUI мёртв: значит, майнеры
    поднимало именно приложение, и без него они жарят карту с автокулером.
    """
    notes: list[str] = []
    for image in STRAY_MINERS:
        rc, out = _run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], timeout=15)
        if image.replace(".exe", "").lower() not in out.lower():
            continue
        _run(["taskkill", "/IM", image, "/T", "/F"], timeout=25)
        notes.append(f"остановлен {image.replace('.exe', '')}")
    return notes


def reset_card() -> list[str]:
    """Снять всё, что ставит приложение: кулер в авто, клоки и лимит к заводским."""
    notes: list[str] = []
    if gpu.fan_policy() == gpu.FAN_POLICY_FIXED:
        ok, msg = gpu.reset_fan_policy()
        notes.append(f"кулер: {msg}")
    else:
        notes.append("кулер: уже авто")
    # по одному аргументу: `nvidia-smi -rgc -rmc` Windows не принимает
    # («Invalid combination of input arguments»)
    rc_g, out_g = _run(["nvidia-smi", "-rgc"])
    rc_m, out_m = _run(["nvidia-smi", "-rmc"])
    notes.append("клоки: сброшены" if rc_g == 0 and rc_m == 0
                 else f"клоки: {(out_g or out_m)[:60]}")
    rc, out = _run(["nvidia-smi", "-pl", str(DEFAULT_PL)])
    notes.append(f"лимит мощности {DEFAULT_PL} Вт" if rc == 0 else f"лимит: {out[:60]}")
    return notes


def afterburner_running() -> bool:
    rc, out = _run(["tasklist", "/FI", f"IMAGENAME eq {AB_NAME}", "/NH"], timeout=15)
    return AB_NAME.lower() in out.lower()


def apply_afterburner() -> str:
    """Перезапустить Afterburner тихо (`/s`): он применяет свой пресет при старте."""
    if not AB.exists():
        return "Afterburner не найден — пресет не применён"
    _run(["taskkill", "/IM", AB_NAME, "/F"], timeout=20)
    time.sleep(1.2)
    try:
        subprocess.Popen([str(AB), "/s"], cwd=str(AB.parent),
                         creationflags=NO_WINDOW, close_fds=True)
    except OSError as e:
        return f"Afterburner не запустился: {e}"
    time.sleep(3.0)
    return ("пресет Afterburner применён (перезапуск /s)"
            if afterburner_running() else "Afterburner не поднялся")


def cleanup(apply_ab: bool = True, force: bool = False, tag: str = "") -> str:
    """Убрать следы приложения, если оно уже не работает. Возвращает отчёт."""
    pid = marker_pid()
    if pid and pid_alive(pid) and not force:
        return f"приложение живо (pid {pid}) — не трогаю"
    notes: list[str] = []
    if pid or MARKER.exists():
        notes.extend(stop_stray_miners())
    notes.extend(reset_card())
    if apply_ab:
        notes.append(apply_afterburner())
    drop()
    report = "; ".join(notes)
    log(f"{tag or ('принудительно' if force else 'после краха')}: {report}")
    return report


def why_stale() -> str:
    """Короткое объяснение для журнала, почему чистильщик вмешался."""
    pid = marker_pid()
    if MARKER.exists() and not pid:
        return "маркер без pid"
    if pid and not pid_alive(pid):
        return f"процесс {pid} мёртв"
    if pid:
        return f"процесс {pid} жив"
    return "маркера нет"
