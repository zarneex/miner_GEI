"""Фоновые задания для блокирующих вызовов (taskkill, ожидание выхода процесса,
nvidia-smi): GUI-поток не должен ждать их 5-10 секунд.

`run_bg(fn, on_done)` выполняет `fn()` в отдельном демон-потоке и вызывает
`on_done()` уже в GUI-потоке (сигнал → queued connection). Так окно продолжает
рисоваться, а поля ввода на время переключения блокирует само окно.

Ссылка на мост держится в модульном наборе до доставки сигнала: иначе Python
собирал объект раньше, чем GUI обрабатывал queued-событие, и колбэк пропадал
(старт майнера «залипал» в состоянии «переключаю», воспроизводилось через раз).
"""
import threading

from PySide6.QtCore import QObject, QTimer, Signal


class _Bridge(QObject):
    """Мост поток → GUI-поток. `_call` живёт в потоке создания моста (GUI)."""

    done = Signal(object)

    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback
        self.done.connect(self._call)

    def _call(self, result) -> None:
        try:
            self._callback(result)
        finally:
            # отпускаем ссылку следующим тактом цикла: удалять себя внутри своего
            # слота нельзя
            QTimer.singleShot(0, lambda: _KEEP.discard(self))


_KEEP: set = set()


def run_bg(fn, on_done=None, owner=None):
    """`fn()` в фоне, `on_done()` — в потоке окна. Возвращает поток."""
    bridge = _Bridge(lambda result: on_done() if on_done else None)
    _KEEP.add(bridge)

    def runner() -> None:
        try:
            fn()
        except Exception:          # фон не должен падать молча в никуда
            pass
        try:
            bridge.done.emit(None)
        except RuntimeError:       # окно уже закрыто
            _KEEP.discard(bridge)

    thread = threading.Thread(target=runner, daemon=True, name="miner-bg")
    thread.start()
    return thread
