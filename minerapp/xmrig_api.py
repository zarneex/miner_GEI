"""HTTP API XMRig 6.26: хешрейт, потоки на ходу, пауза/резюм.

Зачем API, если хешрейт уже парсится из лога:
  * потоки меняются БЕЗ перезапуска процесса: `PUT /2/config` → XMRig заново
    поднимает воркеры внутри себя (замерено: uptime 13→19→26 с, в логе новые
    строки `cpu READY threads N/N`, датасет RandomX не пересоздаётся);
  * пауза для игрового режима — мгновенная, без убийства процесса и без потери
    датасета (`POST /json_rpc {"method":"pause"}`).

Важно: по умолчанию API работает в restricted-режиме — `GET /2/config` и
`/json_rpc` отдают 403. Нужны `--http-no-restricted` и (желательно) токен:
XMRig проверяет `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_PORT = 12002
TIMEOUT = 4.0


class XmrigApi:
    """Тонкий клиент; все методы возвращают None/False вместо исключений."""

    def __init__(self, port: int = DEFAULT_PORT, token: str = "", host: str = "127.0.0.1") -> None:
        self.base = f"http://{host}:{int(port)}"
        self.token = token or ""
        self.last_error = ""

    # --- низкий уровень ---------------------------------------------------
    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, raw
        except urllib.error.HTTPError as e:
            self.last_error = f"{path}: HTTP {e.code}"
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = f"{path}: {e}"
            return 0, ""

    # --- чтение -----------------------------------------------------------
    def summary(self) -> dict | None:
        code, raw = self._req("GET", "/2/summary")
        if code != 200:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def hashrate(self) -> float | None:
        """Текущий хешрейт (10 s), H/s. None — API не ответил."""
        s = self.summary()
        if not s:
            return None
        try:
            v = s["hashrate"]["total"][0]
        except (KeyError, IndexError, TypeError):
            return None
        return float(v) if v is not None else None

    def uptime(self) -> int:
        s = self.summary() or {}
        try:
            return int(s.get("uptime") or 0)
        except (TypeError, ValueError):
            return 0

    def cpu_threads(self) -> int:
        """Сколько потоков реально работает (по backend'у)."""
        code, raw = self._req("GET", "/2/backends")
        if code != 200:
            return 0
        try:
            for b in json.loads(raw):
                if b.get("type") == "cpu":
                    return len(b.get("threads") or [])
        except (ValueError, TypeError, AttributeError):
            pass
        return 0

    def config(self) -> dict | None:
        code, raw = self._req("GET", "/2/config")
        if code != 200:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # --- запись -----------------------------------------------------------
    def set_threads(self, threads: int, affinity: int | None = None) -> bool:
        """Сменить число потоков на ходу. Процесс не перезапускается.

        `affinity` — битовая маска (как `--cpu-affinity`); None — оставить как есть.
        """
        cfg = self.config()
        if not cfg:
            self.last_error = self.last_error or "конфиг не прочитан"
            return False
        cpu = cfg.get("cpu") or {}
        prof = cpu.get("*")
        if not isinstance(prof, dict):
            self.last_error = "в конфиге нет профиля cpu['*'] — меняйте перезапуском"
            return False
        prof["threads"] = int(threads)
        if affinity is not None:
            prof["affinity"] = int(affinity)
        cfg["cpu"] = cpu
        code, raw = self._req("PUT", "/2/config", cfg)
        if code not in (200, 204):
            self.last_error = f"PUT /2/config: HTTP {code} {raw[:80]}"
            return False
        return True

    def pause(self) -> bool:
        return self._rpc("pause")

    def resume(self) -> bool:
        return self._rpc("resume")

    def _rpc(self, method: str) -> bool:
        code, _ = self._req("POST", "/json_rpc",
                            {"jsonrpc": "2.0", "id": 1, "method": method})
        return code == 200
