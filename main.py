"""Точка входа приложения управления майнингом.

Запуск:  C:\\Python314\\python.exe main.py   (pythonw.exe — без окна консоли)
"""
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle  # noqa: E402


class FastTips(QProxyStyle):
    """Подсказки без задержки: штатная пауза Qt (~700 мс) ощущается как «не появляется»."""

    def styleHint(self, hint, option=None, widget=None, returnData=None) -> int:
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return 120
        if hint == QStyle.StyleHint.SH_ToolTip_FallAsleepDelay:
            return 100
        return super().styleHint(hint, option, widget, returnData)


def _crash(text: str) -> None:
    """Записать исключение в журнал приложения.

    Под pythonw stderr — заглушка: без этого любое исключение исчезало молча,
    а вместе с ним мог тихо умереть ограничитель кулера.
    """
    try:
        from minerapp import fan as fanmod
        fanmod.FanKeeper().log_error(text)
    except Exception:            # noqa: BLE001 — журнал не должен ронять приложение
        pass


def _install_crash_log() -> None:
    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        _crash("необработанное исключение: " + text.strip())
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook
    threading.excepthook = lambda a: _crash("поток: " + repr(a.exc_value))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("miner")
    app.setStyle(FastTips(app.style()))
    _install_crash_log()
    from minerapp.ui import MainWindow
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
