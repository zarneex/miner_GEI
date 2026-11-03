"""Окно приложения: консоль и графики, разделы GPU/CPU, ночной режим, метрики.

Метрики первого ряда — сеанс с момента запуска приложения; второй ряд — «полезная строка»:
доход за последний час, текущий темп в час, баланс пула, итог за всё время (с разбивкой
по устройствам и алгоритмам в подсказке).
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QPlainTextEdit,
    QPushButton, QSpinBox, QSystemTrayIcon, QTabWidget, QToolButton, QToolTip,
    QVBoxLayout, QWidget,
)

from . import auto as automod
from . import balance as balmod
from . import charts, config as cfgmod, engine, gpu, history, metrics, monero, paths, rates
from . import asyncjob
from . import cleanup as cleanmod
from . import display
from . import fan as fanmod
from . import governor as govmod
from . import gpu_guard
from . import lhm as lhmmod
from . import owner as ownermod
from .metrics import SessionMetrics

NIGHT_PL = 175          # минимальный лимит карты, Вт (замерено: кулер 59-60%)
DAY_PL = 250
MINUTE_S = 60           # шаг записи истории
BALANCE_S = 600
AUTO_S = 900
PERIOD_MAX = 30 * 86400   # столько поминутного дохода держим в памяти для счётчиков
# (фраза в подсказке, ключ, длина периода, часов в периоде, единица прогноза)
PERIODS = (("последний час", "час", 3600, 1.0, "/ч"),
           ("последние сутки", "сутки", 86400, 24.0, "/сут"),
           ("последнюю неделю", "неделя", 7 * 86400, 168.0, "/нед"),
           ("последний месяц", "месяц", 30 * 86400, 720.0, "/мес"))
IDLE_AFTER_S = 60         # столько бездействия — и интерфейс переходит в тихий режим
IDLE_INTERVAL = 20.0      # опрос датчиков в тихом режиме, с
ACTIVE_INTERVAL = 2.5
IDLE_BALANCE_S = 180      # простой: после 3 мин не опрашиваем баланс и доходность
MONITOR_OFF_S = 300       # простой: через 5 мин гасим мониторы (если включена галочка)
CLEANUP_TICK_S = 60       # уборка фона: раз в минуту, без тяжёлого опроса
# потолок оборотов держит `fan.FanKeeper`: сна нет, каждый такт сверяемся с картой
FAN_WATCHDOG_S = 15       # сторож: даже если датчики зависли, кулер возвращаем к потолку
PL_DEBOUNCE_MS = 500      # крутим спинбокс — не дёргаем nvidia-smi на каждый шаг
SHUTDOWN_NOTICE_MS = 1500  # столько ждём молча: дольше — уведомление «закроюсь, когда встанет»
THREADS_DEBOUNCE_MS = 600  # потоков касаемся реже: перезапуск XMRig не мгновенный
# Температуру CPU держит термогувернатор (`governor.py`) потолком частоты:
# потоки XMRig её не держат (12/14/16 → 90.5/90.6/90.8 °C, PBO упирается в лимит).
# без этого флага каждый дочерний консольный процесс (nvidia-smi, tasklist) открывает
# новое окно терминала: приложение запущено pythonw, своей консоли у него нет
NO_WINDOW = subprocess.CREATE_NO_WINDOW

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_VALUE = "MinerGUI"


def journal(text: str) -> None:
    """Строка в журнал ограничителей (`data/cpu-guard.log`).

    Консоль прокручивается, и «почему ограничитель снял потоки» потом не найти.
    Журнал маленький: пишем только решения ограничителя CPU.
    """
    try:
        path = paths.DATA / "cpu-guard.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


# --- автозапуск приложения (реестр, обратимо) ------------------------------
def _run_value() -> str:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if pyw.exists():
        exe = pyw
    return f'"{exe}" "{paths.PROJECT / "main.py"}"'


TASK_NAME = "MinerGUI"


def _task_command() -> str:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if pyw.exists():
        exe = pyw
    return f'"{exe}" "{paths.PROJECT / "main.py"}"'


def _task_exists() -> bool:
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True,
                           timeout=10, creationflags=NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _task_set(enabled: bool) -> tuple[bool, str]:
    if enabled:
        cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", _task_command(),
               "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"]
        ok_msg = "задача MinerGUI: вход в систему, с правами админа"
    else:
        cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
        ok_msg = "задача MinerGUI удалена"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stdout or r.stderr).strip()[:120]
    return True, ok_msg


def _run_delete() -> None:
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.DeleteValue(k, APP_VALUE)
    except (OSError, ImportError):
        pass


def autostart_enabled() -> bool:
    if _task_exists():
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, APP_VALUE)
            return bool(val)
    except (OSError, ImportError):
        return False


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """Автозапуск — задачей планировщика с правами администратора.

    Через `HKCU\\Run` приложение стартует без прав: XMRig не применит MSR
    (замерено: `FAILED TO APPLY MSR MOD, HASHRATE WILL BE LOW`), а `powercfg`
    не даст гувернатору менять потолок частоты. Задача `MinerGUI` с `/RL HIGHEST`
    запускает приложение при входе без запроса UAC.
    """
    if is_admin():
        ok, msg = _task_set(enabled)
        if ok:
            _run_delete()      # иначе при входе стартуют два экземпляра
            return True, msg
        return False, msg
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if enabled:
                winreg.SetValueEx(k, APP_VALUE, 0, winreg.REG_SZ, _run_value())
                return True, _run_value() + " (без прав: гувернатор и MSR не заработают)"
            winreg.DeleteValue(k, APP_VALUE)
            return True, "удалено из автозагрузки"
    except OSError as e:
        return False, str(e)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def idle_seconds() -> float:
    """Сколько секунд мышь и клавиатура не трогали систему (без опроса WMI)."""
    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LastInput(ctypes.sizeof(LastInput), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return max(0.0, ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0)


def fmt_hr(hr: float | None, coin: str = "pearl") -> str:
    if not hr:
        return "—"
    if coin == "quantus":
        return f"{hr / 1e6:.1f} MH/s"
    return f"{hr / 1e12:.1f} TH/s"


def fmt_hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_rub(value: float) -> str:
    if abs(value) < 0.0005:
        return "0 ₽"
    if abs(value) >= 1:
        return f"{value:.2f} ₽"
    if abs(value) >= 0.01:
        return f"{value:.3f} ₽"
    return f"{value:.4f} ₽"


ASSETS = Path(__file__).resolve().parent / "assets"

# Поля ввода «кошелёк» и «куки»: один тёмный стиль на оба, без рамок по умолчанию,
# с подсветкой фокуса — так строка настроек выглядит короче и опрятнее.
FIELD_QSS = (
    "QLineEdit{background:#141a20;color:#dfe7ef;border:1px solid #2d3843;"
    "border-radius:6px;padding:3px 8px;selection-background-color:#2b5f8a;}"
    "QLineEdit:hover{border-color:#3b4a59;}"
    "QLineEdit:focus{border-color:#4d9de0;background:#11171d;}"
)
ICON_BTN_QSS = (
    "QToolButton{background:transparent;border:1px solid transparent;border-radius:6px;}"
    "QToolButton:hover{background:#22303b;border-color:#34465a;}"
    "QToolButton:pressed{background:#1a242c;}"
)


def icon(name: str) -> QIcon:
    """Иконка из `assets/*.svg` (SVG рисует штатный плагин Qt)."""
    return QIcon(str(ASSETS / f"{name}.svg"))


class Console(QPlainTextEdit):
    """Консоль: моноширинный шрифт, ограниченный буфер, автопрокрутка."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(4000)
        self.setFont(QFont("Consolas", 10))
        self.setStyleSheet(
            "QPlainTextEdit{background:#101418;color:#c8d0d8;border:1px solid #2a323a;}"
            "QScrollBar:vertical{background:#101418;width:12px;}"
        )
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def put(self, msg: str) -> None:
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.appendPlainText(f"{time.strftime('%H:%M:%S')}  {msg}")
        if at_bottom:
            bar.setValue(bar.maximum())


class StatCard(QFrame):
    """Карточка метрики: заголовок, значение и вторая строка (скорость).

    Подсказка ставится на всю карточку, а не только на число, и обновляется лишь
    при изменении текста: `setToolTip` на каждом такте датчиков (раз в 2.5 с)
    сбрасывал уже показанную подсказку.
    """

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(140)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(2)
        self.cap = QLabel(title)
        self.cap.setStyleSheet("color:#7d8894;font-size:11px;")
        self.value = QLabel("—")
        self.value.setStyleSheet("font-size:16px;font-weight:600;")
        self.sub = QLabel("")
        self.sub.setStyleSheet("color:#8fa1b3;font-size:11px;")
        # подпись, число и вторая строка не перехватывают мышь: иначе при переходе
        # между строками подсказка пряталась и появлялась заново (дёрганье и разные шрифты)
        for w in (self.cap, self.value, self.sub):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.cap)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)
        self._tip = ""

    def set_sub(self, text: str) -> None:
        if text != self.sub.text():
            self.sub.setText(text)

    def set_tip(self, text: str) -> None:
        if text == self._tip:
            return
        self._tip = text
        self.setToolTip(text)


class NoWheelSpin(QSpinBox):
    """Спинбокс, который не крутится колесом без фокуса.

    Qt меняет значение колесом даже когда поле не в фокусе: случайный скролл над
    окном менял число потоков и лимит мощности. Колесо работает только в фокусе,
    а ввод с клавиатуры применяется по Enter/потере фокуса (keyboard tracking off),
    как в обычных «взрослых» настройках, а не на каждую цифру.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setKeyboardTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event) -> None:      # noqa: N802 — имя из Qt
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoWheelDoubleSpin(QDoubleSpinBox):
    """То же для дробных значений: шаг 2.5 °C и 2.5 % оборотов."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setKeyboardTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event) -> None:      # noqa: N802 — имя из Qt
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class MainWindow(QMainWindow):
    # строки из фоновых потоков (taskkill, сброс клоков) — только через сигнал
    console_line = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.cfg = cfgmod.load()
        self.app_cfg = cfgmod.app_settings(self.cfg)
        self._sys = dict(self.app_cfg.get("system") or {})
        self.metrics = SessionMetrics()
        self.krig = engine.KrigMiner(self)
        self.cpu = engine.CpuMiner(self)
        self.sensors = engine.SensorThread(self.krig, self.cpu, parent=self)
        self.auto_thread = automod.AutoWatcher(interval_s=AUTO_S, parent=self)
        self.balance_thread = balmod.BalanceWatcher(interval_s=BALANCE_S, parent=self)

        self.target_pl = DAY_PL
        self.eff_pl = DAY_PL
        self.power_guard = gpu_guard.PowerGuard(paths.PL_MIN, paths.PL_MAX)
        self._last_adj = 0.0
        self._fan_warned = False
        self.usd_rub = 80.0
        self._auto_coin: str | None = None
        self._auto_net: dict = {}
        self._minute = self._blank_minute()
        self._period_minutes: list[tuple[int, float, float, float]] = []
        self._sums: dict = {}       # суммы по периодам, пересчёт раз в минуту
        self._idle = False
        self._idle_s = 0.0
        self._monitors_dark = False
        self._startup_done = False     # уборка прошлого запуска завершена
        self._last_frame: dict = {}   # последний кадр датчиков — для сторожа кулера
        self._night_fan: float | None = None   # обороты ночного пресета (тише потолка)
        self._busy_flag = False     # идёт переключение: поля открыты, ввод копится в очереди
        self._shutdown_started = False   # выход уже запущен (повторный крестик игнорируем)
        self._shutdown_done = False
        self._shutdown_force = False
        self._tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
        self._gov_busy = False          # такт гувернатора уже идёт в фоне
        self._gov_lock = threading.Lock()   # tick и stop не должны звать powercfg разом
        self._pl_timer: QTimer | None = None
        # отложенные намерения: последнее значение побеждает
        self._want_gpu: bool | None = None
        self._want_cpu: bool | None = None
        self._want_threads: int | None = None
        self._last_balance = ("—", "", "")
        self.rate_gpu = metrics.RateWindow()
        self.rate_cpu = metrics.RateWindow()
        coin0 = self.app_cfg["gpu"].get("coin", "pearl")
        self._gpu_floor = self._hashrate_floor("pearl" if coin0 == "auto" else coin0)

        self.setWindowTitle(paths.APP_TITLE)
        self.resize(1160, 880)
        self.setMinimumSize(960, 700)
        self._build()
        self._wire()
        self._build_tray()
        # цель регулятора — значение спинбокса (конфиг мог задать не 250 Вт)
        self.target_pl = int(self.spin_pl.value())
        self.eff_pl = self.target_pl
        self.power_guard.reset(self.target_pl)
        self.fan = fanmod.FanKeeper(on_msg=self.console.put)
        self.lhm = lhmmod.LhmBridge(on_msg=self.console.put)
        self.cleaner = cleanmod.Cleaner(on_msg=self.console_line.emit)
        self.gov = govmod.ThermalGovernor(paths.DATA, on_msg=self.console.put)
        leftover = self.gov.restore_backup()   # прошлый запуск мог оставить схему зарезанной
        if leftover:
            self.console.put(leftover)
        self.gov.set_allow_100(self.chk_boost.isChecked())
        self._busy_widgets = ()
        self._threads_timer = QTimer(self)
        self._threads_timer.setSingleShot(True)
        self._threads_timer.timeout.connect(self._apply_threads_wish)
        self._fan_timer = QTimer(self)          # сторож кулера, см. _fan_watchdog
        self._fan_timer.setInterval(FAN_WATCHDOG_S * 1000)
        self._fan_timer.timeout.connect(self._fan_watchdog)
        self._fan_timer.start()
        self._cleanup_timer = QTimer(self)      # уборка фона, см. _cleanup_tick
        self._cleanup_timer.setInterval(CLEANUP_TICK_S * 1000)
        self._cleanup_timer.timeout.connect(self._cleanup_tick)
        self._cleanup_timer.start()
        self._cleanup_busy = False
        self.console_line.connect(self.console.put)
        # прошлый запуск мог умереть, оставив кулер на фиксированных оборотах
        if gpu.fan_policy() == gpu.FAN_POLICY_FIXED:
            _ok, msg = gpu.reset_fan_policy()
            self.fan.note(f"старт: снял фиксацию оборотов прошлого запуска — {msg}")
            self.console.put(f"кулер был зафиксирован прошлым запуском: {msg}")
        self._startup_cleanup()

        self.sensors.snapshot.connect(self.on_snapshot)
        self.krig.line.connect(self.console.put)
        self.cpu.line.connect(self.console.put)
        self.krig.state_changed.connect(lambda _: self._sync_gpu_ui())
        self.cpu.state_changed.connect(lambda _: self._sync_cpu_ui())
        self.auto_thread.result.connect(self.on_auto_result)
        self.balance_thread.result.connect(self.on_balance)
        self.sensors.start()
        self.auto_thread.start()
        self.balance_thread.start()

        self.console.put(f"приложение запущено; конфиг {paths.CFG}")
        if not is_admin():
            self.console.put(
                "ВНИМАНИЕ: приложение без прав администратора — XMRig не применит MSR "
                "(−10…20 % хешрейта), а термогувернатор не сможет менять потолок частоты. "
                "Включите «автозапуск приложения»: задача MinerGUI стартует с правами админа.")
        QToolTip.setFont(self.font())   # одна гарнитура для всех подсказок
        self._idle_timer = QTimer(self)
        self._idle_timer.timeout.connect(self._check_idle)
        self._idle_timer.start(4000)
        QTimer.singleShot(150, self._refresh_rates)
        QTimer.singleShot(900, self._apply_section_autostart)
        QTimer.singleShot(1200, self._seed_periods)
        QTimer.singleShot(2500, self._apply_system_startup)
        if os.environ.get("MINER_DEBUG_COORDS"):
            QTimer.singleShot(2500, self._print_coords)
        if os.environ.get("MINER_HOVER_DEMO"):
            QTimer.singleShot(2500, self._hover_demo)

    def _hover_demo(self) -> None:
        """Для снимка: показать синхронный курсор на середине видимого диапазона."""
        if self.charts._buckets:
            point = self.charts._buckets[int(len(self.charts._buckets) * 0.62)]["ts"]
            self.console.put(f"демо-курсор на {time.strftime('%H:%M', time.localtime(point))}")
            self.charts._on_hover(point * 1000)

    @staticmethod
    def _hashrate_floor(coin: str) -> float:
        """Порог «реальная работа»: половина хешрейта, под который посчитан доход."""
        ref = metrics.SessionMetrics.ref_hashrate(coin)
        return (ref or 0.0) * 0.5

    def _print_coords(self) -> None:
        """Диагностика для автотеста окна: где на экране переключатели."""
        for name, w in (("night", self.chk_night), ("gpu", self.chk_gpu),
                        ("cpu", self.chk_cpu)):
            p = w.mapToGlobal(w.rect().center())
            print(f"COORDS {name} {p.x()} {p.y()}", flush=True)
        bar = self.tabs.tabBar()
        for i in range(bar.count()):
            p = bar.mapToGlobal(bar.tabRect(i).center())
            print(f"TAB {i} {bar.tabText(i)} {p.x()} {p.y()}", flush=True)
        print(f"WINDOW {self.x()} {self.y()} {self.width()} {self.height()}", flush=True)

    # --- разметка ----------------------------------------------------------
    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("Майнинг")
        title.setStyleSheet("font-size:19px;font-weight:700;")
        head.addWidget(title)
        head.addSpacing(14)
        self.chk_autostart_app = QCheckBox("Автозапуск приложения")
        head.addWidget(self.chk_autostart_app)
        head.addSpacing(6)
        self.chk_tray_min = QCheckBox("Скрывать в трей при сворачивании")
        self.chk_tray_min.setToolTip(
            "Свернул окно — оно уходит в трей, майнинг продолжается.\n"
            "Вернуть: двойной клик по значку в трее или «Показать окно» в его меню.")
        head.addWidget(self.chk_tray_min)
        self.chk_tray_close = QCheckBox("Скрывать в трей при закрытии")
        self.chk_tray_close.setToolTip(
            "Крестик окна прячет приложение в трей, майнинг НЕ останавливается.\n"
            "Выйти с остановкой майнинга — кнопка «Остановить и закрыть» справа "
            "или тот же пункт меню трея.")
        head.addWidget(self.chk_tray_close)
        head.addStretch(1)
        self.lbl_shutdown = QLabel("")
        self.lbl_shutdown.setStyleSheet("color:#e0b341;font-size:11px;")
        head.addWidget(self.lbl_shutdown)
        self.btn_stop_close = QPushButton("Остановить и закрыть")
        self.btn_stop_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop_close.setStyleSheet(
            "QPushButton{background:#8a2f2f;color:#fff;border:1px solid #a03a3a;"
            "border-radius:5px;padding:4px 12px;font-weight:600;}"
            "QPushButton:hover{background:#a23a3a;}"
            "QPushButton:pressed{background:#6f2626;}"
            "QPushButton:disabled{background:#3a3f45;color:#8b939b;border-color:#4a5058;}")
        self.btn_stop_close.setToolTip(
            "Остановить майнинг и выйти. Стоп мягкий: сначала taskkill без /F, "
            "если процесс сигнал не принял — через пару секунд принудительный /F.\n"
            "Окно при этом не замирает: закрытие идёт в фоне, о готовности скажет "
            "уведомление в трее.")
        head.addWidget(self.btn_stop_close)
        outer.addLayout(head)

        sections = QHBoxLayout()
        sections.setSpacing(8)
        sections.addWidget(self._build_gpu())
        sections.addWidget(self._build_cpu())
        outer.addLayout(sections)

        self._build_system_row(outer)

        outer.addLayout(self._build_metrics())

        self.console = Console()
        self.charts = charts.ChartsPanel()
        self.charts.set_rate(self.usd_rub)
        self.charts.setStyleSheet("background:#151a1f;border:1px solid #2a323a;")
        self.tabs = QTabWidget()
        self.tabs.addTab(self.console, "Консоль")
        self.tabs.addTab(self.charts, "Графики")
        self.tabs.currentChanged.connect(self._on_tab)
        outer.addWidget(self.tabs, 1)
        if os.environ.get("MINER_START_TAB") == "charts":
            self.tabs.setCurrentIndex(1)

    def _build_system_row(self, outer: QVBoxLayout) -> None:
        """Ночные переключатели: уборка заблокирована, мониторы и кнопка работают.

        ВНИМАНИЕ (важно для всех, кто будет трогать этот код): функция
        «закрыть всё лишнее» (`cleanup.Cleaner`) — КРАЙНЕ НЕСТАБИЛЬНАЯ и
        НЕЗАКОНЧЕННАЯ. Она принудительно закрывает чужие процессы
        (`TerminateProcess`), останавливает службы Windows Update и на
        практике роняет даже само приложение-майнер — то есть ломает
        майнинг, ради которого всё запускается. Включать её смысла нет,
        пока не будет переписана и покрыта тестами. Поэтому галочка
        disabled и всегда снята: остаётся только как памятка о задумке.
        """
        row = QHBoxLayout()
        row.setSpacing(8)
        self.chk_close_extra = QCheckBox("Ночь: закрыть всё лишнее")
        self.chk_close_extra.setToolTip(
            "Заблокировано: функция крайне нестабильна — она принудительно закрывает "
            "чужие процессы и останавливает службы Windows Update, а на практике "
            "роняет даже само приложение-майнер. Пока не переработана, включать её "
            "смысла нет (см. minerapp/cleanup.py).")
        self.chk_close_extra.setChecked(False)
        self.chk_close_extra.setEnabled(False)
        self.chk_close_extra.setStyleSheet("QCheckBox:disabled{color:#6d7986;}")
        row.addWidget(self.chk_close_extra)

        self.chk_monitors = QCheckBox("Гасить мониторы при простое")
        self.chk_monitors.setToolTip(
            f"Через {MONITOR_OFF_S // 60} мин без мыши и клавиатуры мониторы гаснут "
            "(майнинг и таймеры продолжают работать). Любая клавиша или движение "
            "мыши включают их обратно.")
        row.addWidget(self.chk_monitors)

        self.btn_monitors_now = QPushButton("  Погасить сейчас")
        self.btn_monitors_now.setIcon(icon("monitor-off"))
        self.btn_monitors_now.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_monitors_now.setFixedHeight(24)
        self.btn_monitors_now.setStyleSheet(
            "QPushButton{background:#1d2833;color:#cdd8e3;border:1px solid #33424f;"
            "border-radius:6px;padding:2px 10px 2px 8px;font-size:11px;}"
            "QPushButton:hover{background:#243240;border-color:#41566a;}"
            "QPushButton:pressed{background:#172129;}")
        self.btn_monitors_now.setToolTip(
            "Погасить мониторы немедленно (система и майнинг продолжают работать). "
            "Включить обратно — любая клавиша или движение мыши.")
        row.addWidget(self.btn_monitors_now)
        row.addStretch(1)
        outer.addLayout(row)

    def _build_gpu(self) -> QGroupBox:
        g = QGroupBox("Видеокарта")
        lay = QGridLayout(g)
        lay.setHorizontalSpacing(10)
        lay.setVerticalSpacing(6)

        self.chk_gpu = QCheckBox("Вкл")
        self.chk_gpu_auto = QCheckBox("автозапуск")
        self.cmb_coin = QComboBox()
        self.cmb_coin.addItem("Авто (по доходности)", "auto")
        self.cmb_coin.addItem("Pearl (PRL)", "pearl")
        self.cmb_coin.addItem("Quantus (QTC)", "quantus")
        idx = self.cmb_coin.findData(self.app_cfg["gpu"].get("coin", "pearl"))
        self.cmb_coin.setCurrentIndex(max(0, idx))
        top = QHBoxLayout()
        top.addWidget(self.chk_gpu)
        top.addSpacing(8)
        top.addWidget(self.chk_gpu_auto)
        top.addSpacing(8)
        top.addWidget(self.cmb_coin)
        top.addStretch(1)
        lay.addLayout(top, 0, 0, 1, 2)

        self.spin_temp = NoWheelDoubleSpin()
        self.spin_temp.setRange(50, 100)
        self.spin_temp.setSingleStep(2.5)
        self.spin_temp.setDecimals(1)
        self.spin_temp.setValue(float(self.app_cfg["gpu"].get("max_temp", 80)))
        self.spin_temp.setSuffix(" °C")
        self.spin_temp.setToolTip(
            "Порог температуры, за которым приложение снижает лимит мощности (шаг 15 Вт "
            "до 175) и, если не помогло, останавливает GPU.\n"
            "Аппаратный порог троттлинга NVML на GeForce не ставит "
            "(nvmlDeviceSetTemperatureThreshold → NVML_ERROR_NOT_SUPPORTED), поэтому "
            "работает только программный ограничитель.")
        lay.addWidget(QLabel("Макс. температура"), 1, 0)
        lay.addWidget(self.spin_temp, 1, 1)

        self.spin_pl = NoWheelSpin()
        self.spin_pl.setRange(paths.PL_MIN, paths.PL_MAX)
        self.spin_pl.setSingleStep(paths.PL_STEP)
        self.spin_pl.setValue(int(self.app_cfg["gpu"].get("plimit", DAY_PL)))
        self.spin_pl.setSuffix(" Вт")
        self.spin_pl.setToolTip(
            "Лимит мощности карты: применяется на живом майнере через nvidia-smi -pl.\n"
            "175 Вт — минимум карты и тихий режим (~96 TH/s, кулер 59-60% авто),\n"
            "250 Вт — максимум (~107 TH/s, кулер 76-79%).")
        lay.addWidget(QLabel("Лимит мощности"), 2, 0)
        lay.addWidget(self.spin_pl, 2, 1)

        self.spin_fan = NoWheelDoubleSpin()
        self.spin_fan.setRange(0, 100)
        self.spin_fan.setSingleStep(2.5)
        self.spin_fan.setDecimals(1)
        self.spin_fan.setValue(float(self.app_cfg["gpu"].get("max_fan", 80)))
        self.spin_fan.setSuffix(" %")
        self.spin_fan.setToolTip(
            "Значение приложения — главное: кулер держится на нём, пока идёт GPU-майнинг "
            "(NVML не умеет «не выше», поэтому приложение фиксирует обороты и каждый такт "
            "сверяется с картой, возвращая их, если драйвер или майнер сбросили политику). "
            "Выше потолка — только при перегреве: макс. температура +3 °C. "
            "0 — не вмешиваться. Ночной режим берёт минимум из потолка и 55 %.")
        lay.addWidget(QLabel("Макс. вентиляторы"), 3, 0)
        lay.addWidget(self.spin_fan, 3, 1)

        # Ночной пресет заблокирован по просьбе владельца: он перезапускает майнер
        # и меняет лимит мощности/обороты на ходу, а после выхода карта может
        # остаться на чужих настройках. Галочка оставлена как памятка, но
        # неактивна и всегда снята (значение gpu.night в конфиге тоже гасим).
        self.chk_night = QCheckBox("Ночной режим: тихо (175 Вт, кулер 55%)")
        self.chk_night.setToolTip(
            "Заблокировано: режим не включается. Он перезапускает майнер и меняет "
            "лимит мощности и обороты карты на ходу — проще задать «Лимит мощности» "
            "и «Макс. вентиляторы» вручную слева.")
        self.chk_night.setChecked(False)
        self.chk_night.setEnabled(False)
        self.chk_night.setStyleSheet("QCheckBox:disabled{color:#6d7986;}")
        lay.addWidget(self.chk_night, 4, 0, 1, 2)

        self.lbl_gpu = QLabel("остановлен")
        self.lbl_gpu.setStyleSheet("color:#4d9de0;font-family:Consolas;")
        lay.addWidget(self.lbl_gpu, 5, 0, 1, 2)
        g.setLayout(lay)
        return g

    def _build_cpu(self) -> QGroupBox:
        g = QGroupBox("Процессор")
        lay = QGridLayout(g)
        lay.setHorizontalSpacing(10)
        lay.setVerticalSpacing(6)

        self.chk_cpu = QCheckBox("Вкл")
        self.chk_cpu_auto = QCheckBox("автозапуск")
        top = QHBoxLayout()
        top.addWidget(self.chk_cpu)
        top.addSpacing(8)
        top.addWidget(self.chk_cpu_auto)
        top.addStretch(1)
        lay.addLayout(top, 0, 0, 1, 2)

        self.spin_threads = NoWheelSpin()
        self.spin_threads.setRange(2, paths.CPU_TOTAL_THREADS)
        self.spin_threads.setSingleStep(1)
        self.spin_threads.setValue(int(self.app_cfg["cpu"].get("threads", 8)))
        lay.addWidget(QLabel("Потоков"), 1, 0)
        lay.addWidget(self.spin_threads, 1, 1)

        self.spin_load = NoWheelSpin()
        self.spin_load.setRange(20, 100)
        self.spin_load.setSingleStep(5)
        self.spin_load.setValue(int(self.app_cfg["cpu"].get("load", 100)))
        self.spin_load.setSuffix(" %")
        self.spin_load.setToolTip(
            "Потолок доли процессора для XMRig (Job Object Windows, HARD_CAP).\n"
            "Независим от «Потоков»: потоки — сколько ядер отдано, процент — доля\n"
            "их времени. 100 % — ограничение выключено.\n"
            "Ниже ~20 % лимитер вырождается в «пару потоков» и хешрейт дрожит.\n"
            "Ограничение снимается само при выходе приложения — процесс не залипнет.")
        lay.addWidget(QLabel("Нагрузка ЦП"), 2, 0)
        lay.addWidget(self.spin_load, 2, 1)

        self.spin_cpu_temp = NoWheelDoubleSpin()
        self.spin_cpu_temp.setRange(60.0, 90.0)
        self.spin_cpu_temp.setSingleStep(2.5)
        self.spin_cpu_temp.setDecimals(1)
        self.spin_cpu_temp.setValue(float(self.app_cfg["cpu"].get("max_temp", 85.0)))
        self.spin_cpu_temp.setSuffix(" °C")
        self.spin_cpu_temp.setToolTip(
            "Температура, которую держит термогувернатор, пока крутится XMRig.\n"
            "Рычаг — потолок частоты (PROCTHROTTLEMAX) до 99 %: 99 % = 4.6 ГГц / 76 °C,\n"
            "100 % = 5.2 ГГц PBO / 90.5 °C, промежуточных значений Windows не даёт.\n"
            "Поэтому цель 80–85 °C на этом CPU не берётся без BIOS — гувернатор\n"
            "честно держит 99 % и пишет об этом. Потоки температуру не держат.\n"
            "При остановке майнера схема питания возвращается как была.")
        lay.addWidget(QLabel("Держать температуру"), 3, 0)
        lay.addWidget(self.spin_cpu_temp, 3, 1)

        self.chk_boost = QCheckBox("Разрешить 100 % (полный PBO)")
        self.chk_boost.setToolTip(
            "99 % — это 4.6 ГГц и 76 °C, 100 % — 5.2 ГГц и 90.5 °C: между ними\n"
            "Windows не даёт промежуточных значений (ступенька PBO).\n"
            "Галочка ВКЛЮЧЕНА по умолчанию: со снятой приложение режет потолок\n"
            "частоты до 99 % и меняет схему питания — это ломает обычное поведение\n"
            "ПК. Снимите осознанно, если 90 °C на процессоре недопустимы:\n"
            "100 % стоит +40 Вт за +0–5 % хешрейта.")
        lay.addWidget(self.chk_boost, 4, 0, 1, 2)

        self.lbl_cpu = QLabel("остановлен")
        self.lbl_cpu.setStyleSheet("color:#4d9de0;font-family:Consolas;")
        hint = QLabel("загрузка — счётчики Windows · температура, частота, мощность — "
                      "LibreHardwareMonitor, запускается приложением в фоне")
        hint.setStyleSheet("color:#6c7681;font-size:10px;")
        lay.addWidget(self.lbl_cpu, 5, 0, 1, 2)
        lay.addWidget(hint, 6, 0, 1, 2)
        g.setLayout(lay)
        return g

    def _build_metrics(self) -> QVBoxLayout:
        """Две строки карточек; подробности живут в подсказках, чтобы не занимать экран."""
        box = QVBoxLayout()
        box.setSpacing(6)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.card_time = StatCard("Сеанс")
        self.card_usd = StatCard("Доход за сеанс")
        self.card_rub = StatCard("Свет за сеанс")
        self.card_rate = StatCard("Сейчас, ₽/час")
        for c in (self.card_time, self.card_usd, self.card_rub, self.card_rate):
            row1.addWidget(c)
        row1.addStretch(1)
        box.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.card_hour = StatCard("Доход за час")
        self.card_day = StatCard("Доход за сутки")
        self.card_week = StatCard("Доход за неделю")
        self.card_month = StatCard("Доход за месяц")
        self.card_total = StatCard("Доход за всё время")
        self.card_balance = StatCard("Баланс сайта")
        for c in (self.card_hour, self.card_day, self.card_week, self.card_month,
                  self.card_total, self.card_balance):
            row2.addWidget(c)
        row2.addStretch(1)
        box.addLayout(row2)

        self.card_usd.set_tip("доход сеанса: в долларах и рублях по курсу ЦБ")

        row3 = QHBoxLayout()
        row3.setSpacing(4)

        # --- кошелёк: куда майним (kryptex_user; от него же берётся баланс) ---
        self.lbl_login = QLabel("Кошелёк")
        self.lbl_login.setStyleSheet("color:#8b98a5;font-size:11px;")
        self.lbl_login.setToolTip(
            "Логин Kryptex, на который идёт майнинг: «логин/воркер», например krx…/rig1.\n"
            "Пишется в config.json (kryptex_user и xmrig.user), из него же берётся\n"
            "кошелёк для карточки «Баланс сайта». Применяется со следующего старта\n"
            "майнера — если майнинг идёт, перезапустите раздел вручную.")
        self.ed_login = QLineEdit()
        self.ed_login.setStyleSheet(FIELD_QSS)
        self.ed_login.setFixedWidth(212)
        self.ed_login.setMaxLength(96)
        self.ed_login.setToolTip("логин/воркер с kryptex.com")
        self.btn_login_ok = self._icon_button(
            "save", "Сохранить кошелёк (кнопка-галочка)")
        self.btn_login_cancel = self._icon_button(
            "revert", "Вернуть сохранённый кошелёк, ничего не меняя")
        row3.addWidget(self.lbl_login)
        row3.addWidget(self.ed_login)
        row3.addWidget(self.btn_login_cancel)
        row3.addWidget(self.btn_login_ok)
        row3.addSpacing(14)

        # --- куки кабинета: нужны только карточке «Баланс сайта» ---
        self.lbl_cookie = QLabel("Куки")
        self.lbl_cookie.setStyleSheet("color:#8b98a5;font-size:11px;")
        self.lbl_cookie.setToolTip(
            "Kryptex отдаёт баланс только авторизованному кабинету (без куки — 403).\n"
            "Вставьте куки с kryptex.com: они сохранятся в config.json в\n"
            "app.balance.headers.Cookie, и карточка «Баланс сайта» начнёт показывать сумму.\n"
            "Поле очищается после сохранения — секрет не остаётся на экране.")
        self.ed_cookie = QLineEdit()
        self.ed_cookie.setStyleSheet(FIELD_QSS)
        self.ed_cookie.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_cookie.setFixedWidth(212)
        self.ed_cookie.setToolTip("значение заголовка Cookie с kryptex.com")
        self.btn_cookie_ok = self._icon_button(
            "save", "Применить куки и обновить баланс")
        self.btn_cookie_cancel = self._icon_button(
            "revert", "Очистить поле, ничего не сохраняя")
        row3.addWidget(self.lbl_cookie)
        row3.addWidget(self.ed_cookie)
        row3.addWidget(self.btn_cookie_cancel)
        row3.addWidget(self.btn_cookie_ok)
        row3.addStretch(1)
        box.addLayout(row3)
        self._login_hint()
        self._cookie_hint()
        return box

    @staticmethod
    def _icon_button(name: str, tip: str) -> QToolButton:
        """Кнопка-иконка (SVG) в строке настроек: 24×24, без рамки, с подсветкой."""
        btn = QToolButton()
        btn.setIcon(icon(name))
        btn.setIconSize(QSize(14, 14))
        btn.setFixedSize(24, 24)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(ICON_BTN_QSS)
        btn.setToolTip(tip)
        return btn

    # --- связи -------------------------------------------------------------
    def _wire(self) -> None:
        self.chk_gpu.toggled.connect(self.on_gpu_toggle)
        self.chk_cpu.toggled.connect(self.on_cpu_toggle)
        self.chk_night.toggled.connect(self.on_night_toggle)
        self.spin_temp.valueChanged.connect(lambda v: self._persist("gpu", "max_temp", v))
        self.spin_fan.valueChanged.connect(lambda v: self._persist("gpu", "max_fan", v))
        self.spin_pl.valueChanged.connect(self.on_plimit_changed)
        self.spin_threads.valueChanged.connect(self.on_threads_changed)
        self.spin_load.valueChanged.connect(self.on_load_changed)
        self.spin_cpu_temp.valueChanged.connect(self.on_cpu_target_changed)
        self.cmb_coin.currentIndexChanged.connect(self.on_coin_changed)
        self.btn_cookie_ok.clicked.connect(self.on_cookie_apply)
        self.btn_cookie_cancel.clicked.connect(self._cookie_hint)
        self.btn_login_ok.clicked.connect(self.on_login_apply)
        self.btn_login_cancel.clicked.connect(self.on_login_revert)
        self.ed_login.textChanged.connect(self._sync_field_buttons)
        self.ed_cookie.textChanged.connect(self._sync_field_buttons)
        self.chk_gpu_auto.toggled.connect(lambda v: self._persist("gpu", "autostart", v))
        self.chk_cpu_auto.toggled.connect(lambda v: self._persist("cpu", "autostart", v))
        self.chk_autostart_app.setChecked(autostart_enabled()
                                          or bool(self.app_cfg.get("autostart_app")))
        self.chk_autostart_app.toggled.connect(self.on_autostart_app)
        self.chk_gpu_auto.setChecked(bool(self.app_cfg["gpu"].get("autostart", True)))
        self.chk_cpu_auto.setChecked(bool(self.app_cfg["cpu"].get("autostart", False)))
        # 100 % разрешён по умолчанию: иначе гувернатор держит срезанный потолок
        # 99 % и схема питания ПК перестаёт быть «как обычно» (см. _build_cpu).
        self.chk_boost.setChecked(bool(self.app_cfg["cpu"].get("allow_100", True)))
        self.chk_boost.toggled.connect(self.on_allow_100)
        # «Закрыть всё лишнее» заблокировано (см. _build_system_row): старое
        # «включено» из конфига гасим, чтобы уборка не поднялась сама
        self.chk_close_extra.blockSignals(True)
        self.chk_close_extra.setChecked(False)
        self.chk_close_extra.blockSignals(False)
        if self._sys.get("close_extra"):
            self._persist_sys("close_extra", False)
        # ночной пресет заблокирован: снимаем и значение в конфиге
        if self.app_cfg["gpu"].get("night"):
            self._persist("gpu", "night", False)
        # гашение мониторов по простою — рабочая галочка (как раньше)
        self.chk_monitors.setChecked(bool(self._sys.get("monitors_off", False)))
        self.chk_monitors.toggled.connect(self.on_monitors_toggle)
        self.btn_monitors_now.clicked.connect(self.on_monitors_now)
        self.chk_tray_min.setChecked(bool(self._sys.get("tray_on_min", False)))
        self.chk_tray_close.setChecked(bool(self._sys.get("tray_on_close", False)))
        self.chk_tray_min.toggled.connect(
            lambda on: self._persist_sys("tray_on_min", bool(on)))
        self.chk_tray_close.toggled.connect(
            lambda on: self._persist_sys("tray_on_close", bool(on)))
        if not self._tray_ok:      # нет трея (удалённый сеанс) — галочки бессмысленны
            for chk in (self.chk_tray_min, self.chk_tray_close):
                chk.setChecked(False)
                chk.setEnabled(False)
                chk.setToolTip("Системный трей недоступен — скрывать окно некуда.")
        self.btn_stop_close.clicked.connect(self.on_stop_and_close)

    # --- кошелёк для майнинга ---------------------------------------------
    @staticmethod
    def _login_value() -> str:
        try:
            return str(cfgmod.load().get("kryptex_user") or "")
        except (OSError, ValueError):
            return ""

    def _login_hint(self) -> None:
        """Вернуть в поле сохранённый логин: кошелёк — не секрет, его видно."""
        self._saved_login = self._login_value()     # до setText: см. _sync_field_buttons
        self.ed_login.setText(self._saved_login)
        self.ed_login.setPlaceholderText("логин/воркер, напр. krx…/rig1")
        self.lbl_login.setStyleSheet(
            "color:#8b98a5;font-size:11px;" if self.ed_login.text()
            else "color:#d08a4a;font-size:11px;")
        self._sync_field_buttons()

    def _sync_field_buttons(self) -> None:
        """Кнопки-иконки видны только когда поле реально отличается от конфига.

        Иначе строка настроек всегда тащит четыре значка и выглядит шумно:
        «сохранить» и «откатить» появляются при первом же изменении и снова
        прячутся после сохранения/отката.
        """
        saved = getattr(self, "_saved_login", "")
        changed = self.ed_login.text().strip() != saved.strip()
        for btn in (self.btn_login_ok, self.btn_login_cancel):
            btn.setVisible(changed)
        cookie_changed = bool(self.ed_cookie.text().strip())
        for btn in (self.btn_cookie_ok, self.btn_cookie_cancel):
            btn.setVisible(cookie_changed)

    def on_login_apply(self) -> None:
        """Сохранить кошелёк: один логин на GPU (krig) и CPU (XMRig)."""
        text = self.ed_login.text().strip()
        if not text:
            self.console.put("кошелёк не сохранён: поле пустое")
            self._login_hint()
            return
        if " " in text:
            self.console.put("кошелёк не сохранён: в логине не должно быть пробелов")
            self._login_hint()
            return
        if "/" not in text:            # Kryptex ждёт «логин/воркер»
            text += "/rig1"
        try:
            cfg = cfgmod.load()
            cfgmod.app_settings(cfg)
            cfg["kryptex_user"] = text
            cfg.setdefault("xmrig", {})["user"] = text
            cfgmod.save(cfg)
        except (OSError, KeyError, ValueError) as e:
            self.console.put(f"кошелёк не сохранился: {e}")
            return
        self.console.put(f"кошелёк сохранён: {text}")
        if self.krig.is_running() or self.cpu.is_running():
            self.console.put("майнинг уже идёт — новый кошелёк применится со "
                             "следующего запуска раздела")
        self._login_hint()
        self.balance_thread.refresh_now()    # баланс привязан к этому же логину

    def on_login_revert(self) -> None:
        """Откат: вернуть в поле то, что уже лежит в конфиге."""
        self._login_hint()
        self.console.put("кошелёк: поле возвращено к сохранённому значению")

    # --- куки для баланса --------------------------------------------------
    @staticmethod
    def _cookie_value() -> str:
        try:
            app = cfgmod.app_settings(cfgmod.load())
            headers = (app.get("balance") or {}).get("headers") or {}
            return str(headers.get("Cookie") or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def _cookie_hint(self) -> None:
        """Показать состояние: заданы ли куки (само значение на экране не держим)."""
        have = bool(self._cookie_value())
        self.ed_cookie.clear()
        self.ed_cookie.setPlaceholderText(
            "куки сохранены — вставьте новые"
            if have else "куки kryptex.com (необязательно)")
        self.lbl_cookie.setStyleSheet(
            "color:#7fc99a;font-size:11px;" if have else "color:#8b98a5;font-size:11px;")
        self._sync_field_buttons()

    def on_cookie_apply(self) -> None:
        text = balmod.normalize_cookie(self.ed_cookie.text())
        try:
            cfg = cfgmod.load()
            app = cfgmod.app_settings(cfg)
            bal = app.setdefault("balance", {})
            headers = {str(k): str(v) for k, v in (bal.get("headers") or {}).items()}
            if text:
                headers["Cookie"] = text
            else:
                headers.pop("Cookie", None)
            bal["headers"] = headers
            bal.setdefault("coin", "prl")
            cfgmod.save(cfg)
        except (OSError, KeyError, ValueError) as e:
            self.console.put(f"куки не сохранились: {e}")
            return
        self.console.put("куки для баланса " + ("сохранены" if text else "удалены")
                         + " — обновляю баланс")
        self._cookie_hint()
        self.balance_thread.refresh_now()

    def _persist(self, section: str, key: str, value) -> None:
        try:
            cfg = cfgmod.load()
            cfgmod.app_settings(cfg)
            cfg["app"][section][key] = value
            cfgmod.save(cfg)
        except (OSError, KeyError, ValueError) as e:
            self.console.put(f"настройки не сохранились ({section}.{key}): {e}")

    # --- запуск/остановка --------------------------------------------------
    def _active_coin(self) -> str:
        mode = self.cmb_coin.currentData()
        if mode != "auto":
            return mode
        if self.krig.is_running() and self.krig.coin:
            return self.krig.coin
        return self._auto_coin or "pearl"

    def on_coin_changed(self, _index: int) -> None:
        mode = self.cmb_coin.currentData()
        self._persist("gpu", "coin", mode)
        if mode == "auto":
            self.console.put("режим «Авто»: монета выбирается по доходности раз в 15 мин")
            return
        if self.krig.is_running() and self.krig.coin != mode:
            self.console.put(f"переключение на {mode}: перезапускаю майнер")
            self._restart_gpu(f"переключаю на {mode}")

    def _restart_gpu(self, what: str) -> None:
        """Перезапуск GPU: мягкий стоп в фоне, старт сразу по готовности — окно живо."""
        self._busy(True, what)

        def after() -> None:
            self._busy(False)
            self._start_gpu()
            self._drain_queue()

        self.krig.stop(quiet=True, on_done=after)

    def on_gpu_toggle(self, on: bool) -> None:
        if self._busy_flag:
            self._want_gpu = on     # последнее слово пользователя победит
            self.console.put(f"GPU: {'запуск' if on else 'остановка'} в очереди "
                             "— применю, как только закончится текущее переключение")
            return
        self._toggle_gpu(on)

    def _toggle_gpu(self, on: bool) -> None:
        if on:
            self._start_gpu()
            return
        if not self.krig.is_running():
            self._sync_gpu_ui()
            return
        self._busy(True, "останавливаю GPU")

        def after() -> None:
            self._busy(False)
            self._sync_gpu_ui()
            self._restore_pl_target("GPU остановлен")
            self._drain_queue()

        self.krig.stop(on_done=after)

    def _busy(self, on: bool, what: str = "") -> None:
        """Отметка «идёт переключение»: поля ввода остаются доступны, их последнее
        значение копится в очереди и применяется по готовности."""
        self._busy_flag = on
        if on:
            self.setCursor(Qt.CursorShape.BusyCursor)
            self.setWindowTitle(f"{paths.APP_TITLE} — {what}…")
        else:
            self.unsetCursor()
            self.setWindowTitle(paths.APP_TITLE)

    def _drain_queue(self) -> None:
        """Применить отложенные намерения: последнее значение побеждает."""
        if self._busy_flag:
            return
        if self._want_threads is not None and self.cpu.is_running():
            value, self._want_threads = self._want_threads, None
            self._restart_cpu(value)
            return
        if self._want_cpu is not None:
            on, self._want_cpu = self._want_cpu, None
            if on != self.cpu.is_running():
                self._toggle_cpu(on)
            return
        if self._want_gpu is not None:
            on, self._want_gpu = self._want_gpu, None
            if on != self.krig.is_running():
                self._toggle_gpu(on)

    def _stop_stray(self, log, image: str = "krig-miner.exe") -> None:
        """Майнер мог остаться от прошлого запуска (CLI/свитчер/убитое приложение):
        порт API и клоки заняты им. Гасим по имени образа — pid из state.json может
        быть уже затёрт. Вызывается из фонового потока: пишем только через `log`."""
        name = image.replace(".exe", "")
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=NO_WINDOW).stdout
            if name not in out.lower():
                return
            log(f"найден внешний {name} — останавливаю мягко")
            subprocess.run(["taskkill", "/IM", image, "/T"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW)
            for _ in range(10):
                time.sleep(0.5)
                check = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                                       capture_output=True, text=True, timeout=10,
                                       creationflags=NO_WINDOW).stdout
                if name not in check.lower():
                    return
            subprocess.run(["taskkill", "/IM", image, "/T", "/F"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return

    def _start_gpu(self) -> None:
        if self._busy_flag:
            self._want_gpu = True
            self.console.put("GPU: запуск в очереди — применю после текущего переключения")
            return
        self._busy(True, "запускаю GPU")
        asyncjob.run_bg(lambda: self._stop_stray(self.console_line.emit),
                        self._spawn_gpu, owner=self)

    def _spawn_gpu(self) -> None:
        try:
            coin = self._active_coin()
            preset = cfgmod.preset_for(coin, self.chk_night.isChecked())
            preset["plimit"] = self.spin_pl.value()   # спинбокс — единственный источник истины
            bits = [f"лимит {preset.get('plimit')} Вт",
                    f"память {preset.get('moffset') or 0:+d}"]
            if preset.get("fan"):
                bits.append(f"кулер {preset['fan']}% (жёстко)")
            self.console.put(f"GPU: старт {coin}, " + ", ".join(bits))
            self.krig.start(coin, preset)
            self._gpu_floor = self._hashrate_floor(coin)
            self.target_pl = int(preset.get("plimit") or DAY_PL)
            self.eff_pl = self.target_pl
            self.power_guard.reset(self.target_pl)
            self._night_fan = (self._preset_fan(True)
                               if self.chk_night.isChecked() else None)
            QTimer.singleShot(1500, lambda: self._apply_pl(self.target_pl, "старт"))
        finally:
            self._busy(False)
            self._drain_queue()

    def on_plimit_changed(self, watts: int) -> None:
        """Лимит мощности применяется на лету: майнер его не отбивает (проверено).
        Пока крутят спинбокс, nvidia-smi вызываем один раз — после остановки."""
        self._persist("gpu", "plimit", int(watts))
        self.target_pl = int(watts)
        self.power_guard.set_target(int(watts), time.time())
        if not self.krig.is_running():
            self.console.put(f"лимит мощности {watts} Вт применится при старте GPU")
            return
        if self._pl_timer is None:
            self._pl_timer = QTimer(self)
            self._pl_timer.setSingleShot(True)
            self._pl_timer.timeout.connect(
                lambda: self._apply_pl(self.target_pl, "вручную"))
        self._pl_timer.start(PL_DEBOUNCE_MS)

    def on_cpu_toggle(self, on: bool) -> None:
        if self._busy_flag:
            self._want_cpu = on
            self.console.put(f"CPU: {'запуск' if on else 'остановка'} в очереди "
                             "— применю после текущего переключения")
            return
        self._toggle_cpu(on)

    def _start_cpu(self, threads: int) -> None:
        """Запуск XMRig после уборки орфана.

        Если приложение в прошлый раз убили `taskkill /F`, XMRig остаётся жить
        (он не ребёнок GUI) и второй экземпляр молча удваивает нагрузку на CPU —
        поэтому сначала гасим по имени образа в фоне, потом стартуем свой.
        """
        if self._busy_flag:
            self._want_cpu = True
            self.console.put("CPU: запуск в очереди — применю после текущего переключения")
            return
        self._busy(True, "запускаю CPU")

        def spawn() -> None:
            self.cpu.load_pct = int(self.spin_load.value())
            self.cpu.start(threads)
            self._busy(False)
            self._sync_cpu_ui()
            self._drain_queue()

        asyncjob.run_bg(lambda: self._stop_stray(self.console_line.emit, "xmrig.exe"),
                        spawn, owner=self)

    def _toggle_cpu(self, on: bool) -> None:
        if on:
            self._start_cpu(self.spin_threads.value())
            return
        if not self.cpu.is_running():
            self._sync_cpu_ui()
            return
        self._busy(True, "останавливаю CPU")

        def after() -> None:
            self.gov.stop()
            self._busy(False)
            self._sync_cpu_ui()
            self._drain_queue()

        self.cpu.stop(on_done=after)

    def on_threads_changed(self, value: int) -> None:
        """Enter/уход фокуса или шаг — только тогда сюда попадаем (keyboard tracking off).

        Перезапуск не начинается мгновенно: сначала короткая пауза (чтобы прокрутка
        стрелками не гоняла XMRig), а если переключение уже идёт — значение просто
        запоминается и применится последним.
        """
        self._persist("cpu", "threads", value)
        if not self.cpu.is_running():
            return
        self._want_threads = value
        self._threads_timer.start(THREADS_DEBOUNCE_MS)

    def on_load_changed(self, value: int) -> None:
        """Спинбокс «Нагрузка ЦП»: доля процессора, независимая от потоков."""
        self._persist("cpu", "load", int(value))
        if self.cpu.is_running():
            self.cpu.set_load(int(value))

    def on_allow_100(self, on: bool) -> None:
        """Галочка «Разрешить 100 %»: снимает/ставит потолок 99 % у гувернатора."""
        self._persist("cpu", "allow_100", bool(on))
        self.gov.set_allow_100(bool(on))
        self.console.put(f"CPU: 100 % {'разрешён' if on else 'запрещён (потолок 99 %)'}")

    def on_cpu_target_changed(self, value: float) -> None:
        """Цель гувернатора поменяли: сохраняем и сразу пересчитываем потолок."""
        self._persist("cpu", "max_temp", float(value))
        self.gov.on_target_changed(float(value), self._cpu_threads_now())

    def _apply_threads_wish(self) -> None:
        value = self._want_threads
        if value is None or not self.cpu.is_running() or self._busy_flag:
            return                  # применится из _drain_queue по готовности
        self._want_threads = None
        if self.cpu.set_threads(value):
            return                  # потоки поменялись через API, процесс жив
        self._restart_cpu(value)

    def _restart_cpu(self, value: int) -> None:
        self._busy(True, f"перезапускаю CPU на {value} потоках")
        self.console.put(f"CPU: перезапуск на {value} потоках")

        def after() -> None:
            self.cpu.start(value)
            self._busy(False)
            self._drain_queue()

        self.cpu.stop(quiet=True, on_done=after)

    def _sync_gpu_ui(self) -> None:
        running = self.krig.is_running()
        if self.chk_gpu.isChecked() != running:
            self.chk_gpu.blockSignals(True)
            self.chk_gpu.setChecked(running)
            self.chk_gpu.blockSignals(False)

    def _sync_cpu_ui(self) -> None:
        running = self.cpu.is_running()
        if self.chk_cpu.isChecked() != running:
            self.chk_cpu.blockSignals(True)
            self.chk_cpu.setChecked(running)
            self.chk_cpu.blockSignals(False)

    # --- ночной режим ------------------------------------------------------
    def on_night_toggle(self, on: bool) -> None:
        """Обороты задаются только при старте майнера (--gpu-fan), поэтому режим
        переключается перезапуском: мягко гасим, поднимаем с нужным пресетом.

        Галочка заблокирована (см. `_build_gpu`), вызов отсюда недостижим —
        оставлен на случай, если ночной пресет когда-нибудь вернут.
        """
        if not self.chk_night.isEnabled():
            self.chk_night.blockSignals(True)
            self.chk_night.setChecked(False)
            self.chk_night.blockSignals(False)
            return
        self._persist("gpu", "night", on)
        watts = NIGHT_PL if on else DAY_PL
        self.spin_pl.blockSignals(True)
        self.spin_pl.setValue(watts)
        self.spin_pl.blockSignals(False)
        self._persist("gpu", "plimit", watts)
        self.target_pl = watts
        self.eff_pl = watts
        self.power_guard.reset(watts)
        self._fan_warned = False
        # ночью кулер тише потолка; потолок из интерфейса всё равно главный (берём минимум)
        self._night_fan = self._preset_fan(on)
        if not self.krig.is_running():
            self.console.put(f"ночной режим {'включён' if on else 'выключен'}: "
                             "применится при старте GPU-майнинга")
            return
        self.console.put(f"ночной режим {'включён' if on else 'выключен'}: "
                         "перезапускаю майнер (обороты задаются только при старте)")
        self._restart_gpu(f"ночной режим {'включён' if on else 'выключен'}")

    def _preset_fan(self, night: bool) -> float | None:
        """Обороты из пресета (ночной режим = 55 %) или None, если пресет их не задаёт."""
        try:
            preset = cfgmod.preset_for(self._active_coin(), night)
            return float(preset.get("fan") or 0) or None
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def _apply_pl(self, watts: int, why: str) -> None:
        ok, msg = gpu.set_power_limit(watts)
        self.console.put(f"лимит мощности {watts} Вт ({why}): {'ок' if ok else msg}")
        if ok:
            self.eff_pl = watts
        self._last_adj = time.time()

    def _restore_pl_target(self, why: str) -> None:
        """Вернуть лимит мощности к значению спинбокса (стоп майнера/выход).

        Нужно, чтобы после временного снижения карта не осталась на 190–200 Вт.
        """
        self.power_guard.reset(self.target_pl)
        if self.eff_pl != self.target_pl:
            self._apply_pl(self.target_pl, why)

    # --- ночные галочки: уборка фона и мониторы ----------------------------
    def on_close_extra_toggle(self, on: bool) -> None:
        """Уборка фона отключена (галочка disabled): осталось на будущую переработку.

        Функция `cleanup.Cleaner` роняет приложение-майнер, поэтому вызов
        `set_enabled(True)` отсюда недостижим — при любом состоянии галочки
        уборка остаётся выключенной.
        """
        self._persist_sys("close_extra", False)
        self.cleaner.set_enabled(False)
        if on:
            self.console.put("уборка фона отключена в коде (функция нестабильна)")

    def on_monitors_toggle(self, on: bool) -> None:
        """Галочка «Гасить мониторы при простое»: авто-гашение через N минут."""
        self._persist_sys("monitors_off", bool(on))
        if not on and self._monitors_dark:
            display.monitors_on()
            self._monitors_dark = False
        if on:
            self.console.put(f"мониторы гаснут через {MONITOR_OFF_S // 60} мин "
                             "без ввода — любая клавиша включит")

    def on_monitors_now(self) -> None:
        """Кнопка «Погасить сейчас»: выключить мониторы немедленно."""
        ok = display.monitors_off()
        self.console.put("мониторы погашены — любая клавиша или мышь включит"
                         if ok else "мониторы погасить не удалось")

    def _persist_sys(self, key: str, value) -> None:
        self._sys[key] = value
        self._persist("system", key, value)

    def _apply_system_startup(self) -> None:
        """Снять следы прошлого запуска; уборка фона заблокирована навсегда.

        В тестах (`MINER_NO_AUTOSTART`) ПК не трогаем. Галочка уборки disabled,
        поэтому `set_enabled(True)` не вызывается ни при каких настройках —
        только `restore()` возвращает службы обновлений, если прошлый запуск
        успел поставить паузу.
        """
        if os.environ.get("MINER_NO_AUTOSTART"):
            return
        self.cleaner.restore()

    def _cleanup_tick(self) -> None:
        """Уборка в фоне — только если галочка реально доступна (сейчас никогда)."""
        if not self.chk_close_extra.isEnabled() or not self.chk_close_extra.isChecked():
            return
        if self._cleanup_busy:
            return
        self._cleanup_busy = True

        def job():
            try:
                self.cleaner.tick()
            except Exception as e:      # noqa: BLE001 — уборка не должна ронять окно
                self.console_line.emit(f"уборка фона упала: {e!r}")

        asyncjob.run_bg(job, self._cleanup_done, owner=self)

    def _cleanup_done(self) -> None:
        self._cleanup_busy = False

    # --- тихий режим в АФК ------------------------------------------------
    def _check_idle(self) -> None:
        """ПК не трогают или окно свёрнуто — датчики реже, сеть и перерисовка стоп.

        История при этом пишется как обычно (раз в минуту), ограничители работают.
        Баланс и доходность не опрашиваем после `idle_balance_s`, мониторы гасим
        после `monitor_off_s`; первый ввод мгновенно возвращает всё обратно.
        """
        self._idle_s = idle_seconds()
        want = (self._idle_s >= IDLE_AFTER_S or self.isMinimized()
                or not self.isVisible())
        if want != self._idle:
            self._idle = want
            self.sensors.set_interval(IDLE_INTERVAL if want else ACTIVE_INTERVAL)
            if want:
                self.console.put(f"АФК: датчики раз в {IDLE_INTERVAL:.0f} с, "
                                 "графики не рисуются")
            else:
                self.console.put("интерфейс проснулся: обычный опрос")
        pause = self._idle_s >= float(self._sys.get("idle_balance_s", IDLE_BALANCE_S))
        self.balance_thread.set_paused(pause)
        self.auto_thread.set_paused(pause)
        if not self.chk_monitors.isChecked():
            return
        off_after = float(self._sys.get("monitor_off_s", MONITOR_OFF_S))
        if self._idle_s >= off_after and not self._monitors_dark:
            if display.monitors_off():
                self._monitors_dark = True
                self.console.put(f"простой {self._idle_s / 60:.0f} мин: мониторы "
                                 "погашены (любая клавиша включит)")
        elif self._idle_s < 2.0 and self._monitors_dark:
            self._monitors_dark = False        # ввод разбудил экран

    # --- обработка датчиков ------------------------------------------------
    def on_snapshot(self, d: dict) -> None:
        coin = self.krig.coin or self._active_coin()
        tick = self.metrics.tick(d.get("power") or 0.0, self._cpu_threads_now(), coin,
                                 d.get("gpu_running", False), d.get("cpu_running", False))
        self._accumulate(d, tick)
        if not self._idle:                 # в АФК не перерисовываем интерфейс зря
            self._update_labels(d, coin)
            self._update_metrics(coin)
        self._guard(d)                     # ограничители работают всегда
        self.lhm.tick(bool(d.get("cpu_bridge")))   # мост CPU живёт под присмотром
        if time.time() - self._minute["ts"] >= MINUTE_S:
            self._flush_minute()

    def _blank_minute(self) -> dict:
        return {"ts": time.time(), "usd": 0.0, "kwh": 0.0, "cpu_kwh": 0.0, "cpu_usd": 0.0,
                "pw": [], "tc": [], "fan": [], "hr": [], "cpu_pw": [],
                "coin": "", "seconds": 0.0, "cpu_seconds": 0.0}

    def _accumulate(self, d: dict, tick: dict) -> None:
        m = self._minute
        now = time.time()
        dt = tick["dt"]
        m["usd"] += tick["d_usd"]
        m["kwh"] += tick["d_kwh"]
        cpu_w = ((d.get("cpu_watts") or self._cpu_threads_now() * 8.75)
                 if d.get("cpu_running") else 0.0)
        m["cpu_kwh"] += cpu_w * dt / 3_600_000.0
        m["cpu_pw"].append(cpu_w)
        if d.get("power") is not None:
            m["pw"].append(float(d["power"]))
        if d.get("temp") is not None:
            m["tc"].append(float(d["temp"]))
        if d.get("fan") is not None:
            m["fan"].append(float(d["fan"]))
        if d.get("gpu_hashrate"):
            m["hr"].append(float(d["gpu_hashrate"]))
        if d.get("gpu_running"):
            m["coin"] = self.krig.coin or ""

        # «реальная работа»: карта грузится выше половины расчётного хешрейта,
        # поэтому разгон и простои не попадают ни в часы, ни в скорость
        gpu_hr = d.get("gpu_hashrate")
        gpu_work = bool(d.get("gpu_running")) and bool(gpu_hr) and (
            self._gpu_floor <= 0 or gpu_hr >= self._gpu_floor)
        if gpu_work:
            m["seconds"] += dt
        self.rate_gpu.feed(now, gpu_hr, gpu_work)

        cpu_hr = d.get("cpu_hashrate")
        cpu_floor = 300.0 * self.spin_threads.value()
        cpu_work = bool(d.get("cpu_running")) and bool(cpu_hr) and cpu_hr >= cpu_floor
        if cpu_work:
            m["cpu_seconds"] += dt
            m["cpu_usd"] += monero.rev_per_hs_day() * float(cpu_hr) / 86400.0 * dt
        self.rate_cpu.feed(now, cpu_hr, cpu_work)

    def _flush_minute(self) -> None:
        m = self._minute
        avg = lambda xs: sum(xs) / len(xs) if xs else None
        gpu_power = avg(m["pw"])
        cpu_w = avg(m["cpu_pw"]) or 0.0
        total_power = (gpu_power or 0.0) + cpu_w
        try:
            history.append(m["ts"], m["coin"], total_power, avg(m["tc"]), avg(m["fan"]),
                           avg(m["hr"]), m["usd"], m["kwh"] + m["cpu_kwh"], m["cpu_usd"])
            if self.krig.is_running() or m["kwh"] > 0:
                history.add_totals("gpu", m["coin"] or "pearl", m["usd"], m["kwh"],
                                   m["seconds"])
            if m["cpu_kwh"] > 0:
                history.add_totals("cpu", "monero", m["cpu_usd"], m["cpu_kwh"],
                                   m["cpu_seconds"])
        except OSError as e:
            self.console.put(f"история не записалась: {e}")
        self._period_minutes.append((int(m["ts"]), m["usd"], m["cpu_usd"],
                                     m["kwh"] + m["cpu_kwh"]))
        cutoff = time.time() - PERIOD_MAX - 3600
        while self._period_minutes and self._period_minutes[0][0] < cutoff:
            self._period_minutes.pop(0)
        self._minute = self._blank_minute()
        self._sums = self._window_sums()
        self._update_periods(self._active_coin())
        if self.tabs.currentIndex() == 1 and not self._idle:
            self.charts.refresh()
            if os.environ.get("MINER_HOVER_DEMO"):
                self._hover_demo()

    def _update_labels(self, d: dict, coin: str) -> None:
        if d.get("gpu_running"):
            vram = d.get("vram_mb")
            vram_total = d.get("vram_total_mb")
            vram_s = (f"VRAM {vram / 1024:.1f}/{vram_total / 1024:.1f} ГБ"
                      if vram and vram_total else "VRAM —")
            self.lbl_gpu.setText(
                f"{d.get('temp', '—')} °C · кулер {d.get('fan', '—')}% · "
                f"{d.get('power', 0):.0f} Вт · {vram_s} · "
                f"{d.get('sm_clock', 0):.0f}/{d.get('mem_clock', 0):.0f} МГц · "
                f"{fmt_hr(d.get('gpu_hashrate'), coin)}")
            self.lbl_gpu.setToolTip(
                f"температура, обороты, потребление, занятая видеопамять, "
                f"частоты ядра/памяти и хешрейт;\n"
                f"лимит мощности сейчас: {d.get('plimit', 0):.0f} Вт "
                f"(цель {self.spin_pl.value()} Вт), загрузка GPU: "
                f"{d.get('util', 0):.0f}%")
        else:
            self.lbl_gpu.setText("остановлен")
        # метрики процессора: температура/частота/мощность — от моста LHM, загрузка — Windows
        threads = self._cpu_threads_now()
        if d.get("cpu_running"):
            hr = d.get("cpu_hashrate")
            util = d.get("cpu_util")
            temp = d.get("cpu_temp")
            mhz = d.get("cpu_mhz")
            watts = d.get("cpu_watts")
            est = d.get("cpu_est_watts")
            bits = [f"{threads} потоков",
                    f"{(hr / 1000):.1f} kH/s" if hr else "разгон датасета…"]
            if temp is not None:
                bits.append(f"{temp:.0f} °C")
            if mhz:
                bits.append(f"{mhz:.0f} МГц")
            if watts:
                bits.append(f"{watts:.0f} Вт")
            elif est:
                bits.append(f"~{est:.0f} Вт (оценка)")
            if util is not None:
                bits.append(f"загрузка {util:.0f}%")
            self.lbl_cpu.setText(" · ".join(bits))
            src = ("температура, пакетная мощность и частота — драйвер "
                   "LibreHardwareMonitor (мост lhmd.ps1)") if d.get("cpu_bridge") else (
                   "мост датчиков не запущен: мощность — оценка 8.75 Вт на поток, "
                   "температуры нет")
            self.lbl_cpu.setToolTip(f"{src};\nзагрузка — счётчики Windows (GetSystemTimes); "
                                    f"потоков {threads}, приоритет ниже обычного")
        else:
            self.lbl_cpu.setText("остановлен")
            self.lbl_cpu.setToolTip("")

    def _now_rates(self, coin: str) -> tuple[float, float, float, float]:
        """(GPU ₽/ч, CPU ₽/ч, точность %, окно в секундах) по последним 10 минутам."""
        g = self.rate_gpu.stats()
        c = self.rate_cpu.stats()
        gpu = g["mean"] * self.metrics.usd_per_hour_mark(coin) * self.usd_rub
        cpu = c["mean"] * monero.rev_per_hs_day() * self.usd_rub / 24.0
        window = max(g["seconds"], c["seconds"])
        if g["mean"] and c["mean"]:
            rel = max(g["rel_pct"], c["rel_pct"])
        elif g["mean"]:
            rel = g["rel_pct"]
        else:
            rel = c["rel_pct"]
        return gpu, cpu, rel, window

    def _update_metrics(self, coin: str) -> None:
        el = self.metrics.elapsed()
        rate = self.usd_rub
        tariff = rates.electricity_rub_kwh()
        self.card_time.value.setText(fmt_hms(el))
        self.card_usd.value.setText(
            f"${self.metrics.usd:.3f} ({self.metrics.usd * rate:.2f} ₽)")
        self.card_rub.value.setText(fmt_rub(self.metrics.kwh * tariff))
        hours = el / 3600 or 1e-9
        self.card_rub.set_tip(
            f"{self.metrics.kwh:.3f} кВт·ч при тарифе {tariff:.2f} ₽/кВт·ч\n"
            f"средняя мощность за сеанс {self.metrics.kwh * 1000 / hours:.0f} Вт")

        gpu_rate, cpu_rate, rel, window = self._now_rates(coin)
        total = gpu_rate + cpu_rate
        if total <= 0:
            suffix = ""
        elif rel >= 50:
            suffix = " ±50%+"
        elif rel >= 10:
            suffix = f" ±{rel:.0f}%"
        else:
            suffix = f" ±{rel:.1f}%"
        self.card_rate.value.setText(f"{fmt_rub(total)}/ч{suffix}")
        self.card_rate.set_tip(
            f"Среднее за последние 10 минут реальной работы (окно {window / 60:.1f} мин)\n"
            f"GPU ({coin}): {fmt_rub(gpu_rate)}/ч\n"
            f"CPU (monero): {fmt_rub(cpu_rate)}/ч\n"
            f"точность ±{rel:.0f}% — растёт по мере накопления точек\n"
            "простои и разгон не учитываются")
        self._update_periods(coin)

    # --- счётчики за периоды ----------------------------------------------
    def _seed_periods(self) -> None:
        """Минутный кэш за 30 дней: доход за сутки/неделю/месяц без чтения диска.

        Дальше кэш растёт сам — каждая записанная минута добавляется в `_flush_minute`.
        """
        now = time.time()
        try:
            rows = history.load(now - PERIOD_MAX, now)
        except OSError:
            rows = []
        self._period_minutes = [(int(r["ts"]), r["usd"], r["cpu_usd"], r["kwh"])
                                for r in rows]
        self._sums = self._window_sums()
        self._update_periods(self._active_coin())

    def _window_sums(self) -> dict[str, dict]:
        """Один проход по кэшу сразу для всех периодов: в АФК это заметно дешевле."""
        now = time.time()
        cuts = {dat: now - secs for _acc, dat, secs, _h, _u in PERIODS}
        out = {dat: {"gpu": 0.0, "cpu": 0.0, "kwh": 0.0, "first": None, "work_min": 0}
               for dat in cuts}
        for ts, usd_g, usd_c, kwh_v in reversed(self._period_minutes):
            hit = False
            for dat, cut in cuts.items():
                if ts >= cut:
                    acc = out[dat]
                    acc["gpu"] += usd_g
                    acc["cpu"] += usd_c
                    acc["kwh"] += kwh_v
                    acc["first"] = ts
                    if usd_g + usd_c > 0:
                        acc["work_min"] += 1     # минута, в которую реально копали
                    hit = True
            if not hit:
                break
        for dat, acc in out.items():
            hours = (now - acc["first"]) / 3600 if acc["first"] else 0.0
            hours_work = acc["work_min"] / 60.0
            usd = acc["gpu"] + acc["cpu"]
            acc.update(usd=usd, hours=hours, hours_work=hours_work,
                       per_hour=usd / hours if hours > 0 else 0.0,
                       per_hour_work=usd / hours_work if hours_work > 0 else 0.0)
        return out

    def _update_periods(self, coin: str | None = None) -> None:
        """Счётчики периодов: крупное число — прогноз по текущему темпу, мелкое — факт.

        Прогноз берётся не из «среднего по истории» (там сидят простои, вчерашние
        пресеты и ночная тишина), а из текущего темпа за последние 10 минут реальной
        работы: `₽/ч × длину периода`. Поэтому карточки совпадают с «Сейчас, ₽/ч» и
        не отстают на часы. Факт за период — из поминутной истории, обновляется раз в минуту.
        """
        rate = self.usd_rub
        tariff = rates.electricity_rub_kwh()
        coin = coin or self._active_coin()
        sums = self._sums or self._window_sums()
        gpu_h, cpu_h, rel, window = self._now_rates(coin)
        now_h = gpu_h + cpu_h
        cards = {"час": self.card_hour, "сутки": self.card_day,
                 "неделя": self.card_week, "месяц": self.card_month}
        for acc, dat, _secs, period_h, _unit in PERIODS:
            card = cards[dat]
            s = sums.get(dat) or {}
            card.value.setText(fmt_rub(now_h * period_h))
            work_h = float(s.get("hours_work") or 0.0)
            usd_fact = float(s.get("usd") or 0.0)
            fact = fmt_rub(usd_fact * rate)
            card.set_sub(f"факт {fact} · {work_h:.1f} ч" if work_h > 0.02
                         else "факт ещё не накопился")
            tip = [f"прогноз: текущий темп {fmt_rub(now_h)}/ч × {period_h:.0f} ч",
                   f"прогноз на {dat}: {fmt_rub(now_h * period_h)} "
                   f"(GPU {fmt_rub(gpu_h * period_h)} · CPU {fmt_rub(cpu_h * period_h)})",
                   f"сейчас: GPU {fmt_rub(gpu_h)}/ч · CPU {fmt_rub(cpu_h)}/ч"
                   + (f" · ±{rel:.0f}%" if rel else ""),
                   f"окно текущего темпа: {window / 60:.0f} мин реальной работы"]
            if work_h > 0.02:
                tip += [f"факт за {acc}: {fact} за {work_h:.1f} ч майнинга",
                        f"  GPU {fmt_rub(float(s.get('gpu') or 0) * rate)} · "
                        f"CPU {fmt_rub(float(s.get('cpu') or 0) * rate)}",
                        f"  свет {fmt_rub(float(s.get('kwh') or 0) * tariff)} · "
                        f"{float(s.get('kwh') or 0):.3f} кВт·ч",
                        f"средний факт-темп: {fmt_rub(usd_fact / work_h * rate)}/ч"]
            card.set_tip("\n".join(tip))

        totals = history.load_totals()
        usd, kwh, sec = history.totals_sum(totals)
        hours = sec / 3600
        self.card_total.value.setText(fmt_rub(usd * rate))
        self.card_total.set_sub(f"{fmt_rub(usd * rate / hours)}/ч" if hours > 0.02 else "")
        lines = ["по устройствам:"]
        for device, algos in totals.items():
            for algo, node in algos.items():
                lines.append(f"{device.upper()} ({algo}): {fmt_rub(node.get('usd', 0) * rate)} · "
                             f"{node.get('seconds', 0) / 3600:.1f} ч работы")
        lines.append(f"итого: {fmt_rub(usd * rate)} · {hours:.1f} ч работы · "
                     f"{kwh:.2f} кВт·ч")
        if hours > 0.02:
            lines.append(f"средний темп по часам работы: {fmt_rub(usd * rate / hours)}/ч")
        self.card_total.set_tip("\n".join(lines))

    def _fan_guard(self, d: dict) -> None:
        """Кулер держит приложение, а не драйвер и не майнер.

        «Макс. вентиляторы» — это потолок, но NVML умеет только «ровно столько»,
        поэтому контроллер (`fan.FanKeeper`) каждый такт читает политику и обороты
        с самой карты и возвращает её к значению из интерфейса, если кто-то сбросил
        (старт/стоп майнера, `--gpu-fan`, драйвер, прошлый запуск). Выше потолка
        выходим только при перегреве: порог + 3 °C.
        """
        self._last_frame = d
        try:
            self.fan.apply(cap=self.spin_fan.value(),
                           temp=d.get("temp"),
                           max_temp=self.spin_temp.value(),
                           running=bool(d.get("gpu_running")),
                           quiet_fan=self._night_fan)
        except Exception as e:      # noqa: BLE001 — контроллер не должен умирать молча
            self.fan.log_error(f"ограничитель кулера упал: {e!r}")

    def _fan_watchdog(self) -> None:
        """Сторож: раз в 15 с возвращаем кулер к значению из приложения.

        Датчики опрашиваются раз в 2.5 с (в АФК — 20 с), но если поток датчиков
        завис, кадр не приходит и без сторожа карта осталась бы на автокривой.
        Признак «майнер работает» берём живьём, а не из старого кадра.
        """
        if not self._last_frame:
            return
        frame = dict(self._last_frame)
        frame["gpu_running"] = self.krig.is_running()
        self._fan_guard(frame)

    def _cpu_threads_now(self) -> int:
        """Сколько потоков реально отдано XMRig (ограничитель мог снять часть)."""
        try:
            return int(self.cpu.threads or self.spin_threads.value())
        except (TypeError, ValueError):
            return int(self.spin_threads.value())

    def _cpu_guard(self, d: dict) -> None:
        """Температуру процессора держит термогувернатор, а не число потоков.

        Замерено (2026-11-02): 12/14/16 потоков XMRig дают 90.5/90.6/90.8 °C —
        PBO поднимает частоту до теплового лимита, поэтому снятие потоков
        температуру не снижает. Реальный рычаг — потолок частоты процессора
        (`PROCTHROTTLEMAX` в схеме питания): его и ведёт `governor.ThermalGovernor`
        к цели из спинбокса (60–90 °C, шаг 2.5). Майнер при этом не перезапускается.

        Замерено: смена потолка — два вызова `powercfg`, из них `/setactive` ~250 мс,
        `/query` ~100 мс. В GUI-потоке это рывок на полсекунды, поэтому такт идёт
        в фоне (`asyncjob`), а новый не наслаивается на незакончившийся.
        """
        if self._shutdown_started or self._gov_busy:
            return
        self._gov_busy = True
        args = (d.get("cpu_temp"), self._cpu_threads_now(),
                float(self.spin_cpu_temp.value()), bool(d.get("cpu_running")))

        def job() -> None:
            try:
                with self._gov_lock:      # stop() из выхода не должен пересечься
                    self.gov.tick(*args)
            except Exception as e:        # noqa: BLE001 — контроллер не должен ронять окно
                journal(f"термогувернатор упал: {e!r}")
                self.console_line.emit(f"термогувернатор упал: {e!r}")

        def done(_result: object = None) -> None:
            self._gov_busy = False

        asyncjob.run_bg(job, done, owner=self)

    def _guard(self, d: dict) -> None:
        self._fan_guard(d)
        self._cpu_guard(d)
        if not d.get("gpu_running"):
            self.power_guard.reset(self.target_pl)
            return
        temp = d.get("temp")
        if temp is None:
            return
        limit_c = float(self.spin_temp.value())
        now = time.time()
        # Лимит — регулятор, а не «срезали и забыли»: снижаем на время перегрева,
        # возвращаем к значению спинбокса, когда температура ниже порога на 3 °C.
        new_limit = self.power_guard.observe(temp, limit_c, now)
        if new_limit is not None:
            self._apply_pl(new_limit, f"{temp:.1f} °C при пороге {limit_c:.0f} °C")
        if self.power_guard.need_stop(temp, limit_c, now):
            self.console.put(
                f"перегрев {temp:.0f} °C при {self.power_guard.applied} Вт дольше "
                f"{self.power_guard.stop_hold:.0f} с — останавливаю GPU")
            self.chk_gpu.setChecked(False)
            return
        if temp < limit_c:
            self._fan_warned = False
        elif (self.power_guard.applied <= self.power_guard.min_w
              and not self._fan_warned):
            self._fan_warned = True
            self.console.put(
                f"{temp:.0f} °C при кулере {self.spin_fan.value():.1f}% и "
                f"{self.power_guard.applied} Вт: ниже {paths.PL_MIN} Вт карта "
                "не опускается — поднимите потолок или включите ночной режим")

    # --- фон: курс, баланс, автовыбор монеты -------------------------------
    def _refresh_rates(self) -> None:
        try:
            self.usd_rub = rates.usd_rub()
        except (OSError, ValueError):
            self.usd_rub = 80.0
        self.charts.set_rate(self.usd_rub)
        self.console.put(f"курс USD→RUB {self.usd_rub:.2f}; "
                         f"свет {rates.electricity_rub_kwh():.2f} ₽/кВт·ч")
        QTimer.singleShot(3_600_000, self._refresh_rates)

    def on_balance(self, text: str, sub: str, hint: str) -> None:
        self._last_balance = (text, sub, hint)
        self.card_balance.value.setText(text)
        self.card_balance.set_sub(sub)
        self.card_balance.set_tip(hint)

    def on_auto_result(self, res: dict) -> None:
        if res.get("error"):
            self.console.put(f"авто: доходность не обновилась ({res['error']})")
            return
        net = res.get("net") or {}
        best = res.get("best")
        if not net or not best:
            return
        self._auto_net = net
        self._auto_coin = best
        pretty = ", ".join(f"{k} ${v:.2f}/сут" for k, v in sorted(net.items()))
        self.console.put(f"авто: {pretty} · лидер {best} (+{res.get('gain_pct', 0):.1f}%)")
        if self.cmb_coin.currentData() != "auto" or not self.krig.is_running():
            return
        current = self.krig.coin
        if best == current:
            return
        cur_net = net.get(current, 0.0)
        gain = (net[best] - cur_net) / cur_net * 100 if cur_net > 0 else 100.0
        min_gain = 20.0
        dwell_min = 120.0
        try:
            switch = cfgmod.load()["switch"]
            min_gain = float(switch.get("min_gain_pct", 20))
            dwell_min = float(switch.get("min_dwell_minutes", 120))
        except (OSError, KeyError, ValueError):
            pass
        last_switch = float(self.app_cfg.get("auto", {}).get("last_switch", 0) or 0)
        if gain < min_gain:
            self.console.put(f"авто: {best} выгоднее на {gain:.1f}% < порога {min_gain:.0f}% — не переключаю")
            return
        waited = (time.time() - last_switch) / 60 if last_switch else 1e9
        if waited < dwell_min:
            self.console.put(f"авто: {best} выгоднее на {gain:.1f}%, но прошло "
                             f"{waited:.0f} мин < {dwell_min:.0f} — держу {current}")
            return
        self.app_cfg = self._persist_auto_switch()
        self.console.put(f"авто: переключаю {current} → {best} (выгода {gain:.1f}%)")
        self._restart_gpu(f"авто: {current} → {best}")

    def _persist_auto_switch(self) -> dict:
        try:
            cfg = cfgmod.load()
            app = cfgmod.app_settings(cfg)
            node = app.setdefault("auto", {})
            node["last_switch"] = time.time()
            cfgmod.save(cfg)
            return app
        except (OSError, ValueError):
            return self.app_cfg

    def _on_tab(self, index: int) -> None:
        if index == 1:
            self.charts.set_rate(self.usd_rub)
            self.charts.refresh()

    # --- автозапуск --------------------------------------------------------
    def _apply_section_autostart(self) -> None:
        # окно могло быть закрыто раньше (или запущен автотест) — не поднимаем майнеры
        if os.environ.get("MINER_NO_AUTOSTART") or not self.isVisible():
            self.console.put("автозапуск разделов пропущен (окно не активно)")
            return
        if not self._startup_done:
            # уборка прошлого запуска ещё идёт: не начинаем майнинг раньше неё,
            # иначе её сброс клоков прилетит уже после старта майнера
            QTimer.singleShot(500, self._apply_section_autostart)
            return
        for name, chk, chk_auto in (("GPU", self.chk_gpu, self.chk_gpu_auto),
                                    ("CPU", self.chk_cpu, self.chk_cpu_auto)):
            if chk_auto.isChecked():
                self.console.put(f"{name}: автозапуск раздела сработал")
                chk.setChecked(True)
            else:
                self.console.put(f"{name}: автозапуск раздела выключен")

    def on_autostart_app(self, on: bool) -> None:
        ok, msg = set_autostart(on)
        if ok:
            self._persist_root("autostart_app", on)
            self.console.put(f"автозапуск приложения {'включён' if on else 'выключен'}: {msg}")
        else:
            self.console.put(f"автозапуск не изменился: {msg}")
            self.chk_autostart_app.blockSignals(True)
            self.chk_autostart_app.setChecked(not on)
            self.chk_autostart_app.blockSignals(False)

    def _persist_root(self, key: str, value) -> None:
        try:
            cfg = cfgmod.load()
            cfgmod.app_settings(cfg)
            cfg["app"][key] = value
            cfgmod.save(cfg)
        except (OSError, ValueError) as e:
            self.console.put(f"настройки не сохранились ({key}): {e}")

    # --- уборка за прошлым запуском и передача карты Afterburner -------------
    def _startup_cleanup(self) -> None:
        """Снять следы прошлого запуска (в фоне) и отметить карту своей.

        Прошлый процесс могли убить: тогда на карте остались фиксированные обороты,
        срезанный лимит мощности и чужие клоки. Чистильщик снимает их до старта
        майнинга, а маркер `data/gpu-owned.json` говорит фоновой задаче, что карта
        сейчас наша и трогать её нельзя.
        """
        if os.environ.get("MINER_NO_AUTOSTART"):     # автотест: железо не трогаем
            self._startup_done = True
            return

        def job() -> str:
            # force=False: если маркер принадлежит живому процессу, значит запущен
            # второй экземпляр — его майнеры трогать нельзя
            report = ownermod.cleanup(apply_ab=False, force=False, tag="старт GUI")
            ownermod.take("GUI запущено")
            return report

        def done(report: object = None) -> None:
            self._startup_done = True
            self.console.put(f"уборка прошлого запуска: {report}")

        asyncjob.run_bg(job, done, owner=self)

    def _hand_over_to_afterburner(self) -> None:
        """Вернуть карту пресету Afterburner — отдельным процессом, после выхода.

        Окно не должно ждать перезапуска Afterburner (5-6 с): чистильщик получает
        `--force`, снимаем маркер и уходим. Он же вернёт кулер в авто, клоки и
        лимит мощности к заводским и применит пресет Afterburner.
        """
        ownermod.drop()
        script = paths.PROJECT / "tools" / "gpu_cleanup.py"
        try:
            subprocess.Popen([sys.executable, str(script), "--force"],
                             creationflags=NO_WINDOW, close_fds=True)
        except OSError as e:
            self.console.put(f"чистильщик карты не запустился ({e}) — снимаю вручную")
            self.fan.release("окно закрыто")
            gpu.reset_clocks()
            gpu.set_power_limit(DAY_PL)

    # --- трей -------------------------------------------------------------
    @staticmethod
    def _tray_icon() -> QIcon:
        """Иконка трея: рисуем сами — файла с иконкой в проекте нет."""
        pm = QPixmap(32, 32)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1f7a4d"))
        p.drawRoundedRect(2, 2, 28, 28, 7, 7)
        p.setPen(QColor("#eaf3ee"))
        p.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "M")
        p.end()
        return QIcon(pm)

    def _build_tray(self) -> None:
        """Значок в трее: вернуть окно или остановить майнинг и выйти.

        Без трея (удалённый сеанс, урезанная оболочка) галочки скрытия выключены,
        а крестик работает как обычное закрытие.
        """
        self.tray = QSystemTrayIcon(self._tray_icon(), self)
        self.tray.setToolTip(f"{paths.APP_TITLE} — майнинг")
        menu = QMenu(self)
        menu.addAction("Показать окно", self._restore_from_tray)
        menu.addSeparator()
        menu.addAction("Остановить и закрыть", self.on_stop_and_close)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu
        self.tray.activated.connect(self._tray_activated)
        if self._tray_ok:
            self.tray.show()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        if self._shutdown_started:
            return
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _hide_to_tray(self, why: str = "") -> None:
        self.hide()
        self.setWindowState(Qt.WindowState.WindowNoState)
        self._tray_note(why or "Свёрнуто в трей — майнинг продолжается")

    def _tray_note(self, text: str, msec: int = 3500) -> None:
        """Тихое уведомление в трее; журнал — в консоль окна."""
        if self._tray_ok:
            self.tray.showMessage(paths.APP_TITLE, text,
                                  QSystemTrayIcon.MessageIcon.Information, msec)
        self.console.put(text)

    def changeEvent(self, event) -> None:
        """Сворачивание при галочке уводит окно в трей, майнинг не трогаем."""
        if (event.type() == QEvent.Type.WindowStateChange and self.isMinimized()
                and self._tray_ok and not self._shutdown_started
                and self.chk_tray_min.isChecked()):
            # singleShot: hide() прямо внутри смены состояния Qt не любит
            QTimer.singleShot(0, lambda: self._hide_to_tray(
                "Свернул окно в трей — майнинг продолжается"))
        super().changeEvent(event)

    # --- выход -------------------------------------------------------------
    def on_stop_and_close(self) -> None:
        """Кнопка в шапке и пункт меню трея: остановить майнинг и выйти."""
        self._begin_shutdown(force=True)

    def _begin_shutdown(self, force: bool = False) -> None:
        """Закрытие без подвисаний: майнеры гасятся в фоне, окно остаётся живым.

        `force=True` (кнопка) — мягкая попытка и добивание через ~1 с.
        `force=False` (крестик) — мягкая попытка, пауза 2.5 с и только потом `/F`:
        консольный майнер мягкий сигнал Windows не принимает (замерено), поэтому
        ждать «вслепую», как раньше, нечего — оконное время уходит в ноль.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._shutdown_force = force
        self._shutdown_had_mining = self.krig.is_running() or self.cpu.is_running()
        self.btn_stop_close.setEnabled(False)
        self.btn_stop_close.setText("Останавливаю…")
        self.lbl_shutdown.setText("останавливаю майнинг…")
        running = [m for m in (self.krig, self.cpu) if m.is_running()]
        self.console.put("выход: " + (", ".join(m.name for m in running) + " — мягкий стоп"
                                      if running else "майнинг не запущен"))
        cfgmod.log_event("выход: стоп майнинга" if running else "выход: майнинг не шёл")
        QTimer.singleShot(SHUTDOWN_NOTICE_MS, self._shutdown_notice)
        if not running:
            self._finish_shutdown()
            return
        pids = [m.pid() for m in running]
        grace = 1.0 if force else 2.5

        def job() -> None:
            for pid in pids:
                engine.soft_stop(pid, grace=grace, hard=True)

        def done(_result: object = None) -> None:
            self._finish_shutdown()

        asyncjob.run_bg(job, done, owner=self)

    def _shutdown_notice(self) -> None:
        """Стоп идёт дольше мгновения: сказать, что окно не зависло."""
        if not self._shutdown_started or self._shutdown_done:
            return
        self.lbl_shutdown.setText("закроюсь, как только остановится майнинг")
        self.setWindowTitle(f"{paths.APP_TITLE} — останавливаю майнинг…")
        self._tray_note("Закроюсь, как только остановится майнинг", 4000)

    def _finish_shutdown(self) -> None:
        """Довести выход: вернуть железо, службы и схему питания, закрыть окно."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self._cleanup_timer.stop()
            self._fan_timer.stop()
            self.cleaner.set_enabled(False)      # вернуть службы обновлений Windows
            if self._monitors_dark:
                display.monitors_on()
                self._monitors_dark = False
            self.auto_thread.stop()
            self.balance_thread.stop()
            with self._gov_lock:     # такт гувернатора в фоне не должен пересечься
                self.gov.stop()      # вернуть исходную схему питания, пока живы права
            try:
                self.sensors.stop()
                if not self.sensors.wait(2000):
                    self.console.put("опрос датчиков не завершился — прерываю")
                    self.sensors.terminate()
                    self.sensors.wait(800)
            except (RuntimeError, AttributeError):
                pass
            # процессы уже погашены фоном: здесь только состояние QProcess и хвосты
            self.krig.settle_stop(quiet=True)
            self.cpu.settle_stop(quiet=True)
            self.lhm.stop()
            # автотест не должен трогать железо живого майнинга: в тестах политика кулера,
            # клоки и лимит мощности остаются как есть (иначе прогон тестов роняет карту)
            if not os.environ.get("MINER_NO_AUTOSTART"):
                self.fan.release("окно закрыто")
                if self._shutdown_had_mining:  # ничего не запускали — чужие настройки не трогаем
                    gpu.reset_clocks()
                    gpu.set_power_limit(DAY_PL)
                self._hand_over_to_afterburner()
            try:
                self._flush_minute()
            except (OSError, ValueError):
                pass
            cfgmod.log_event("приложение закрыто" + (
                ", майнинг остановлен" if self._shutdown_had_mining else ""))
        except Exception as e:       # noqa: BLE001 — выйти обязаны в любом случае
            self.console.put(f"ошибка при выходе: {e}")
        finally:
            if self._tray_ok:
                self.tray.hide()
            QApplication.instance().quit()

    def closeEvent(self, event) -> None:
        if self._shutdown_started or self._shutdown_done:
            event.accept()
            return
        if self._tray_ok and self.chk_tray_close.isChecked():
            event.ignore()
            self._hide_to_tray("Скрыто в трей — майнинг продолжается. "
                               "Выйти: «Остановить и закрыть»")
            return
        event.ignore()       # окно закроем сами, когда майнеры встанут
        self._begin_shutdown(force=False)
