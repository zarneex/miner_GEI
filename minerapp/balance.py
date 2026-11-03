"""Баланс на Kryptex.

Проверено запросами 2026-11-02:

- `pool.kryptex.com/{coin}/api/v1/miner/balance/{wallet}` для кошельков `krx…` отдаёт **403**
  анонимно (любой `krx…`, даже несуществующий; чужой BTC-адрес при этом получает 200 с нулями).
  Кука кабинета пул **не принимает**: с `Cookie: sessionid=…` и с `Origin`/`Referer` пула —
  тот же 403, а `Vary` ответа не содержит `Cookie`, т.е. сессионной авторизации там нет.
- Кабинет `www.kryptex.com/site/balance` с той же кукой отдаёт 200 и «Суммарный баланс»
  (Kryptex зачисляет намайненное в общий баланс в BTC, отдельного PRL-числа наружу нет).

Поэтому основной источник — страница кабинета (HTML, разбор «Суммарный баланс»), а JSON-API
пула остаётся запасным: он публичен для воркеров/выплат/награды, но не для баланса.
"""
import json
import random
import re
import threading
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, Signal

from . import config as cfgmod

POOL = "https://pool.kryptex.com"
BALANCE_URL = POOL + "/{coin}/api/v1/miner/balance/{wallet}"
WORKERS_URL = POOL + "/{coin}/api/v3/miner/workers/{wallet}"
PAYOUTS_URL = POOL + "/{coin}/api/v1/miner/payouts/{wallet}/stats"
CABINET_BALANCE_URL = "https://www.kryptex.com/site/balance"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) miner-gui/1.0"


class PoolError(Exception):
    """Ответ пула с кодом (403 — баланс закрыт авторизацией)."""

    def __init__(self, code: int, url: str) -> None:
        super().__init__(f"код {code}")
        self.code = code
        self.url = url


def _get(url: str, headers: dict, timeout: int = 12):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise PoolError(e.code, url) from e


def normalize_cookie(text: str) -> str:
    """Привести вставленное к значению заголовка Cookie.

    Понимает: готовый `a=1; b=2`, строку с префиксом `Cookie:`, экспорт
    Cookie-Editor (JSON со списком `{name, value, ...}`), значения по строкам
    и просто голое значение сессии (`sessionid=...` подставится само).
    """
    t = (text or "").strip()
    if not t:
        return ""
    if t[0] in "[{":
        try:
            data = json.loads(t)
        except ValueError:
            pass
        else:
            items = data if isinstance(data, list) else [data]
            pairs = [f"{c['name']}={c.get('value', '')}" for c in items
                     if isinstance(c, dict) and c.get("name")]
            if pairs:
                return "; ".join(pairs)
    t = re.sub(r"(?i)^\s*cookie\s*:\s*", "", t)
    t = "; ".join(part.strip() for part in t.replace("\r", ";").replace("\n", ";").split(";")
                  if part.strip())
    if "=" not in t:
        t = "sessionid=" + t
    return t


def _dig(data, path: str):
    node = data
    for part in (path.split(".") if path else []):
        if isinstance(node, list):
            try:
                node = node[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _workers_line(coin: str, wallet: str, headers: dict) -> str | None:
    """Что пул видит публично: воркеры, 24-часовой средний хешрейт, шары."""
    try:
        data = _get(WORKERS_URL.format(coin=coin, wallet=wallet), headers)
    except (OSError, ValueError, PoolError):
        return None
    results = (data or {}).get("results") or []
    if not results:
        return "пул не видит активных воркеров"
    bits = []
    for w in results[:3]:
        hr = float(w.get("avg_hashrate_24h") or 0) / 1e12
        shares = w.get("valid", 0)
        stale = w.get("stale", 0)
        bits.append(f"{w.get('worker', '?')}: {w.get('status', '?')}, "
                    f"24ч {hr:.1f} TH/s, шары {shares} (+{stale} stale)")
    return "пул видит: " + "; ".join(bits)


def _get_text(url: str, headers: dict, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise PoolError(e.code, url) from e


def _cabinet_balance(headers: dict) -> tuple[str, str, str] | None:
    """Баланс кабинета из HTML: (крупное число, вторая строка, подсказка).

    Крупным числом идёт рублёвый эквивалент — по нему видно, стоит ли выводить;
    BTC остаётся второй строкой и в подсказке. Если валюту на странице сменили
    (нет «₽»), крупным числом становится BTC.
    Возвращает None, если разобрать не удалось (тогда вызывающий пробует API пула).
    """
    h = {**headers, "Accept": "text/html,application/xhtml+xml",
         "Accept-Language": "ru,en;q=0.9"}
    try:
        html = _get_text(CABINET_BALANCE_URL, h)
    except PoolError as e:
        if e.code in (401, 403):
            return "—", "", f"кабинет kryptex.com: код {e.code} — куки не приняты"
        return None
    except (OSError, ValueError):
        return None
    label = next((m for m in ("Суммарный баланс", "Total balance") if m in html), None)
    if label is None:
        return None                      # не залогинились — отдали страницу входа
    tail = re.sub(r"(?s)<[^>]+>", " ", html.split(label, 1)[1][:2000])
    tail = re.sub(r"\s+", " ", tail)
    btc = re.search(r"([\d]+[.,]\d+)\s*BTC", tail)
    rub = re.search(r"≈\s*([\d\s]+[.,]\d+)\s*₽", tail)
    if not btc:
        return "—", "", "кабинет открылся, но число баланса не найдено (изменилась разметка)"
    value = btc.group(1).replace(",", ".")
    rub_val = rub.group(1).replace("\u00a0", " ").strip() if rub else ""
    if rub_val:
        text, sub = f"{rub_val} ₽", f"{value} BTC"
    else:
        text, sub = f"{value} BTC", ""
    tip = [f"кабинет kryptex.com: {value} BTC"
           + (f" ≈ {rub_val} ₽" if rub_val else "")]
    tip.append("Kryptex переводит намайненное (PRL/QTC/XMR) в общий баланс BTC")
    tip.append(f"источник: {CABINET_BALANCE_URL}")
    tip.append("кука сессии живёт ~2 недели, потом вставить свежую")
    return text, sub, "\n".join(tip)


def fetch_ex(cfg: dict | None = None) -> tuple[str, str, str, bool]:
    """(крупное число, вторая строка, подсказка, удалось ли получить число).

    Последний элемент нужен сторожу: по нему он решает, ждать обычный интервал
    или отступить (backoff), чтобы не долбить сайт при 403/429/сети.
    """
    try:
        cfg = cfg or cfgmod.load()
    except (OSError, ValueError):
        return "—", "", "Конфиг не прочитан", False
    app = cfgmod.app_settings(cfg)
    bal = app.get("balance") or {}
    coin = str(bal.get("coin") or "prl")
    wallet = str(cfg.get("kryptex_user", "")).split("/")[0].strip()
    if not wallet:
        return "—", "", "В конфиге не задан kryptex_user", False
    headers = {"User-Agent": UA, "Accept": "application/json"}
    for key, val in (bal.get("headers") or {}).items():
        headers[str(key)] = str(val)
    if bal.get("token"):
        headers["Authorization"] = str(bal["token"])

    url = (bal.get("url") or "").strip()
    if not url and headers.get("Cookie"):
        got = _cabinet_balance(headers)
        if got:
            return got[0], got[1], got[2], got[0] != "—"
    url = url or BALANCE_URL
    url = url.replace("{coin}", coin).replace("{wallet}", wallet)
    path = str(bal.get("json_path") or "total")

    workers = _workers_line(coin, wallet, headers)
    try:
        data = _get(url, headers)
    except PoolError as e:
        tip = [f"баланс: {url} → код {e.code} (нужна авторизация аккаунта kryptex.com)"]
        if e.code == 403:
            tip.append("пул баланс не отдаёт: для krx-кошельков он закрыт, а куку кабинета "
                       "пул не принимает (проверено). Баланс берём со страницы кабинета — "
                       "нужна кука sessionid в app.balance.headers.Cookie")
        if workers:
            tip.append(workers)
        return "—", "", "\n".join(tip), False
    except (OSError, ValueError) as e:
        return "—", "", f"Баланс не получен: {e}\n" + (workers or ""), False

    value = _dig(data, path)
    try:
        total = float(value)
    except (TypeError, ValueError):
        return "—", "", f"В ответе нет числа по пути '{path}'\n" + (workers or ""), False
    threshold = _dig(data, "threshold")
    confirmed = _dig(data, "confirmed")
    unconfirmed = _dig(data, "unconfirmed")
    tip = [f"баланс пула: {total:.6f} {coin.upper()}"]
    if threshold is not None:
        tip.append(f"порог выплаты: {threshold}")
    if confirmed is not None or unconfirmed is not None:
        tip.append(f"подтверждено {confirmed} · в пути {unconfirmed}")
    tip.append(f"источник: {url}")
    if workers:
        tip.append(workers)
    return f"{total:.4f} {coin.upper()}", "", "\n".join(tip), True


def fetch(cfg: dict | None = None) -> tuple[str, str]:
    """(крупное число, подсказка) — для проб и старых вызовов."""
    text, _sub, tip, _ok = fetch_ex(cfg)
    return text, tip


class BalanceWatcher(QObject):
    """Фоновое обновление баланса в демон-потоке (сеть не подвешивает окно).

    Опрос с разбросом ±10 %, чтобы запросы не шли по секундомеру, и с тормозом
    на сбоях: каждый провал удваивает паузу (10 → 20 → 40 мин), потолок — час.
    Так 403/429 или сеть не превращаются в долбёжку чужого сайта.
    """

    result = Signal(str, str, str)        # крупное число, вторая строка, подсказка

    def __init__(self, interval_s: int = 600, parent=None) -> None:
        super().__init__(parent)
        self.interval_s = interval_s
        self.failures = 0
        self._stop = threading.Event()
        self._paused = threading.Event()      # простой ПК: сеть не дёргаем
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="balance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_paused(self, paused: bool) -> None:
        """Пользователь не трогает мышь/клавиатуру — опрос баланса не нужен."""
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def wait_after(self, ok: bool) -> float:
        """Пауза до следующего опроса: обычный интервал или отступление."""
        if ok:
            self.failures = 0
            return self.interval_s * random.uniform(0.9, 1.1)
        self.failures = min(self.failures + 1, 6)
        return min(self.interval_s * (2 ** self.failures), 3600) * random.uniform(0.9, 1.1)

    def refresh_now(self) -> None:
        """Один внеочередной запрос (после смены куки) — не дожидаясь интервала."""
        def job() -> None:
            try:
                text, sub, hint, _ok = fetch_ex()
            except Exception as e:  # никакая ошибка источника не роняет поток
                text, sub, hint = "—", "", f"Баланс не получен: {e}"
            self.result.emit(text, sub, hint)

        self._thread_failures_reset()
        threading.Thread(target=job, name="balance-now", daemon=True).start()

    def _thread_failures_reset(self) -> None:
        self.failures = 0                 # после ручной смены куки считаем заново

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(5.0)      # проснулись/нет — проверяем флаг снова
                continue
            try:
                text, sub, hint, ok = fetch_ex()
            except Exception as e:  # никакая ошибка источника не роняет поток
                text, sub, hint, ok = "—", "", f"Баланс не получен: {e}", False
            self.result.emit(text, sub, hint)
            self._stop.wait(self.wait_after(ok))
