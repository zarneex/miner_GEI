"""Проба: смена потоков и pause/resume XMRig через HTTP API, без перезапуска процесса.

Отдельный экземпляр (порт 12003, свой лог), рабочий майнинг не трогает.
Ожидаемый итог:
  * PUT /2/config с cpu["*"].threads = N даёт в логе `cpu READY threads N/N`
    при непрерывном uptime процесса;
  * POST /json_rpc pause -> hashrate 10s падает, resume -> возвращается,
    и всё это без строк старта процесса.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(os.environ.get("MINER_ROOT") or r"W:\mining")
XMRIG = ROOT / "xmrig" / "xmrig-6.26.0" / "xmrig.exe"
LOG = ROOT / "logs" / "api-test.log"
PORT = 12003
TOKEN = "test-token-12345"
POOL = "xmr-ru.kryptex.network:8029"
# свой кошелёк в публичном коде не держим: логин задаётся переменной окружения
USER = os.environ.get("MINER_USER", "krx-your-login/rig1")


def req(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                               headers={"Authorization": f"Bearer {TOKEN}",
                                        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def mask(n: int, total: int = 16) -> int:
    return ((1 << n) - 1) << (total - n)


def state():
    _, raw = req("GET", "/2/backends")
    be = json.loads(raw)
    th = [t for b in be if b.get("type") == "cpu" for t in (b.get("threads") or [])]
    _, sraw = req("GET", "/2/summary")
    s = json.loads(sraw)
    hr = (s.get("hashrate", {}).get("total") or [None])[0]
    return len(th), s.get("uptime"), hr


def set_threads(n: int) -> int:
    _, raw = req("GET", "/2/config")
    cfg = json.loads(raw)
    cfg["cpu"]["*"] = {"intensity": 1, "threads": n, "affinity": mask(n)}
    code, _ = req("PUT", "/2/config", cfg)
    return code


def main() -> None:
    LOG.unlink(missing_ok=True)
    args = [str(XMRIG), "--coin", "monero", "-o", POOL, "-u", USER, "-p", "x", "--tls",
            "--threads", "4", "--cpu-affinity", f"0x{mask(4):X}",
            "--cpu-priority", "1", "--no-color",
            "--http-port", str(PORT), "--http-host", "127.0.0.1",
            "--http-access-token", TOKEN, "--http-no-restricted", "--log-file", str(LOG)]
    proc = subprocess.Popen(args, cwd=str(XMRIG.parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            time.sleep(0.5)
            if req("GET", "/2/summary")[0] == 200:
                break
        for _ in range(90):
            time.sleep(1.0)
            n, up, hr = state()
            if (hr or 0) > 0:
                break
        print(f"старт: потоков={n}, uptime={up} с, 10s={hr}")

        print(f"PUT threads=2 -> {set_threads(2)}")
        time.sleep(6)
        n2, up2, hr2 = state()
        print(f"после 2: потоков={n2}, uptime={up2} с, 10s={hr2}")

        print(f"PUT threads=4 -> {set_threads(4)}")
        time.sleep(7)
        n3, up3, hr3 = state()
        print(f"после 4: потоков={n3}, uptime={up3} с, 10s={hr3}")

        code, raw = req("POST", "/json_rpc", {"jsonrpc": "2.0", "id": 1, "method": "pause"})
        print(f"json_rpc pause -> {code} {raw[:80]}")
        time.sleep(15)
        n4, up4, hr4 = state()
        print(f"пауза: потоков={n4}, uptime={up4} с, 10s={hr4}")

        code, raw = req("POST", "/json_rpc", {"jsonrpc": "2.0", "id": 2, "method": "resume"})
        print(f"json_rpc resume -> {code} {raw[:80]}")
        time.sleep(15)
        n5, up5, hr5 = state()
        print(f"после resume: потоков={n5}, uptime={up5} с, 10s={hr5}")

        text = LOG.read_text(encoding="utf-8", errors="replace")
        for ln in text.splitlines():
            if any(k in ln for k in ("READY threads", "paused", "resumed", "about to start")):
                print("  LOG:", ln.strip()[:150])
    finally:
        proc.kill()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
