"""Развернуть агента CPU-майнинга на втором ПК (персональный модуль).

Запуск с этого ПК, где уже настроен SSH-ключ:

    python tools\\deploy_remote_cpu.py --host pc2

Скрипт копирует XMRig, библиотеку LibreHardwareMonitor, мост датчиков и агента,
пишет на втором ПК config.json с тем же кошельком (rig по умолчанию `pc2`) и
проверяет агента через `--selftest`. Ничего не публикует и не включает модуль
в приложении: для этого нужен `app.cpu_remote.enabled=true` в личном config.json.

Драйвер PawnIO (через него LHM читает температуру) ставится отдельно, если его
ещё нет: `--pawnio C:\\path\\PawnIO_setup.exe`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
FILES = ("remote_cpu_agent.py", "governor.py", "xmrig_api.py", "cpu_limit.py")
NO_WINDOW = 0x08000000


def run(args: list[str]) -> subprocess.CompletedProcess:
    print("+", " ".join(args))
    return subprocess.run(args, check=True, creationflags=NO_WINDOW)


def ssh(host: str, command: str) -> subprocess.CompletedProcess:
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", host, command])


def scp(host: str, src: Path, dst: str) -> None:
    run(["scp", "-o", "BatchMode=yes", str(src), f"{host}:{dst}"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="pc2", help="алиас из ~/.ssh/config")
    ap.add_argument("--local-root", default=r"W:\mining", help="откуда брать XMRig и LHM")
    ap.add_argument("--remote-root", default=r"C:\mining", help="куда ставить на втором ПК")
    ap.add_argument("--python", default=r"C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe")
    ap.add_argument("--rig", default="pc2", help="имя рига в кошельке (wallet/rig)")
    ap.add_argument("--pawnio", default="", help="PawnIO_setup.exe, если драйвера ещё нет")
    a = ap.parse_args()

    local = Path(a.local_root)
    remote = a.remote_root.rstrip("\\").replace("\\", "/")
    cfg = json.loads((local / "config.json").read_text(encoding="utf-8"))
    wallet = str((cfg.get("xmrig") or {}).get("user") or "")
    if "/" not in wallet:
        print("в локальном config.json нет xmrig.user вида wallet/rig", file=sys.stderr)
        return 2
    user = wallet.split("/")[0] + "/" + a.rig
    url = str((cfg.get("xmrig") or {}).get("url") or "")

    for sub in ("xmrig", "tools", "agent", "logs", "data"):
        ssh(a.host, f'if not exist "{remote}/{sub}" mkdir "{remote}/{sub}"')

    xm = local / "xmrig" / "xmrig-6.26.0"
    scp(a.host, xm / "xmrig.exe", f"{remote}/xmrig/xmrig.exe")
    scp(a.host, xm / "WinRing0x64.sys", f"{remote}/xmrig/WinRing0x64.sys")
    run(["scp", "-o", "BatchMode=yes", "-r",
         str(local / "tools" / "LibreHardwareMonitor"), f"{a.host}:{remote}/tools/"])
    for name in FILES:
        scp(a.host, HERE / name if (HERE / name).exists() else PROJECT / "minerapp" / name,
            f"{remote}/agent/{name}")
    scp(a.host, HERE / "lhmd_remote.ps1", f"{remote}/tools/lhmd.ps1")

    tmp = Path(tempfile.mkstemp(suffix=".json")[1])
    tmp.write_text(json.dumps({"xmrig": {"url": url, "user": user}},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        scp(a.host, tmp, f"{remote}/config.json")
    finally:
        tmp.unlink(missing_ok=True)

    if a.pawnio:
        scp(a.host, Path(a.pawnio), f"{remote}/tools/PawnIO_setup.exe")
        ssh(a.host, f'"{remote}/tools/PawnIO_setup.exe" -install -silent')

    ssh(a.host, f'"{a.python}" "{remote}/agent/remote_cpu_agent.py" --selftest')
    print("\nГотово. Включить карточку: app.cpu_remote.enabled=true, "
          f'python="{a.python}", script="{a.remote_root}\\agent\\remote_cpu_agent.py".')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
