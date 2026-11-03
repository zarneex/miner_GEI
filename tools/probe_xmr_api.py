"""Быстрый замер XMRig: реальный kH/s и вид ответа HTTP API (чтобы не гадать)."""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
NO_WINDOW = subprocess.CREATE_NO_WINDOW
XMRIG = Path(r"W:\mining\xmrig\xmrig-6.26.0\xmrig.exe")
CFG = json.loads(Path(r"W:\mining\config.json").read_text(encoding="utf-8"))["xmrig"]


def get(path: str) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:12002{path}", timeout=4) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"ERR {e}"


def main() -> int:
    threads = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    mask = ((1 << threads) - 1) << (16 - threads)
    args = [str(XMRIG), "--coin", "monero", "-o", CFG["url"], "-u", CFG["user"], "-p", "x",
            "--tls", "--threads", str(threads), "--cpu-affinity", f"0x{mask:X}",
            "--cpu-priority", "1", "-k", "--no-color", "--http-port", "12002",
            "--http-host", "127.0.0.1", "--log-file", r"W:\mining\logs\xmr-api-probe.log"]
    print(f"старт {threads} потоков")
    proc = subprocess.Popen(args, cwd=str(XMRIG.parent), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    time.sleep(95)
    for path in ("/json", "/1m", "/2min", "/summary"):
        body = get(path)
        print(f"--- {path} (первые 300 символов):", body[:300].replace("\n", " "))
    temps = json.loads(Path(r"W:\mining\data\cpu.json").read_text(encoding="utf-8"))
    print("cpu.json:", temps)
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   capture_output=True, creationflags=NO_WINDOW)
    print("остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
