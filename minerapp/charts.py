"""Вкладка графиков: тёмная тема и синхронный курсор по всем четырём графикам.

Наведение на любом графике ставит вертикальную метку на ту же временную точку во всех
четырёх и подсвечивает значение маркером; подсказка показывает все четыре величины сразу.
"""
import bisect
import math
import time

from PySide6.QtCharts import (QChart, QChartView, QDateTimeAxis, QLineSeries,
                              QScatterSeries, QValueAxis)
from PySide6.QtCore import QDateTime, QMargins, QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QVBoxLayout, QWidget)

from . import history, rates

BG = "#0f1418"
PLOT_BG = "#161e25"
GRID = "#2a323a"
AXIS = "#8a94a0"
TEXT = "#cfd8e3"

RANGES = [("1 ч", 3600), ("6 ч", 6 * 3600), ("24 ч", 86400), ("7 дней", 7 * 86400),
          ("30 дней", 30 * 86400), ("всё", 0)]
STEPS = [("1 мин", 60), ("5 мин", 300), ("15 мин", 900), ("1 час", 3600),
         ("6 часов", 6 * 3600), ("1 сутки", 86400)]
MAX_POINTS = 900  # больше точек на график не рисуем: склеиваем шаг


def _decimals(span: float, px: float) -> int:
    """Сколько знаков после запятой осмысленно при такой ширине графика."""
    if span <= 0 or px <= 0:
        return 2
    per_px = span / px
    return max(0, min(4, int(math.floor(-math.log10(per_px))) + 1))


class HoverView(QChartView):
    """График, который сообщает панели время точки под курсором."""

    hovered = Signal(object)  # мс эпохи или None, когда курсор ушёл

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRubberBand(QChartView.RubberBand.NoRubberBand)
        self.setBackgroundBrush(QColor(BG))

    def mouseMoveEvent(self, event) -> None:
        chart = self.chart()
        if chart is not None:
            try:
                x = chart.mapToValue(event.position().toPoint()).x()
                self.hovered.emit(float(x))
            except (AttributeError, ValueError, RuntimeError):
                self.hovered.emit(None)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.hovered.emit(None)
        super().leaveEvent(event)


class ChartBox(QWidget):
    """Один график: линия, вертикальный курсор и маркер точки."""

    def __init__(self, title: str, unit: str, color: str, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        cap = QLabel(title)
        cap.setStyleSheet(f"color:{TEXT};font-weight:600;font-size:12px;")
        lay.addWidget(cap)

        self.view = HoverView()
        chart = QChart()
        chart.setBackgroundBrush(QColor(BG))
        chart.setBackgroundRoundness(0)
        chart.setPlotAreaBackgroundVisible(True)
        chart.setPlotAreaBackgroundBrush(QColor(PLOT_BG))
        chart.legend().hide()
        chart.setMargins(QMargins(4, 2, 4, 2))

        self.series = QLineSeries()
        self.series.setPen(QPen(QColor(color), 1.6))
        self.cursor = QLineSeries()
        pen = QPen(QColor("#7c8b99"), 1.0, Qt.PenStyle.DashLine)
        self.cursor.setPen(pen)
        self.marker = QScatterSeries()
        self.marker.setMarkerSize(10.0)
        self.marker.setColor(QColor(color))
        self.marker.setBorderColor(QColor("#e8eef4"))

        for s in (self.series, self.cursor, self.marker):
            chart.addSeries(s)

        self.axis_x = QDateTimeAxis()
        self.axis_x.setTickCount(6)
        self.axis_x.setLabelsBrush(QBrush(QColor(AXIS)))
        self.axis_x.setGridLineColor(QColor(GRID))
        self.axis_x.setLinePenColor(QColor(GRID))
        self.axis_y = QValueAxis()
        self.axis_y.setLabelsBrush(QBrush(QColor(AXIS)))
        self.axis_y.setGridLineColor(QColor(GRID))
        self.axis_y.setLinePenColor(QColor(GRID))
        self.axis_y.setTitleText(unit)
        self.axis_y.setTitleBrush(QBrush(QColor(AXIS)))
        chart.addAxis(self.axis_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(self.axis_y, Qt.AlignmentFlag.AlignLeft)
        for s in (self.series, self.cursor, self.marker):
            s.attachAxis(self.axis_x)
            s.attachAxis(self.axis_y)

        self.chart = chart
        self.view.setChart(chart)
        lay.addWidget(self.view, 1)
        self.view.setMinimumHeight(150)
        self._points: list[tuple[float, float]] = []
        self.hide_cursor()

    def plot(self, buckets: list[dict], key: str, fmt: str, scale: float = 1.0) -> None:
        self.series.clear()
        self._points = []
        for b in buckets:
            v = b.get(key)
            if v is None:
                continue
            self._points.append((float(b["ts"]), v * scale))
        if not self._points:
            self.hide_cursor()
            return
        for ts, val in self._points:
            self.series.append(QPointF(ts * 1000.0, val))
        values = [p[1] for p in self._points]
        lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.12 or max(abs(hi) * 0.1, 1.0)
        self.axis_y.setRange(0.0 if lo >= 0 else lo - pad, hi + pad)
        self.axis_x.setFormat(fmt)
        start = int(self._points[0][0] * 1000)
        end = int(self._points[-1][0] * 1000)
        if end - start < 60_000:
            end = start + 60_000
        self.axis_x.setRange(QDateTime.fromMSecsSinceEpoch(start),
                             QDateTime.fromMSecsSinceEpoch(end))
        self.hide_cursor()

    def show_at(self, ts: float) -> float | None:
        """Поставить курсор и маркер на ближайшую точку к ts (секунды эпохи)."""
        if not self._points:
            return None
        xs = [p[0] for p in self._points]
        i = bisect.bisect_left(xs, ts)
        i = min(max(i, 0), len(self._points) - 1)
        if i > 0 and abs(xs[i - 1] - ts) < abs(xs[i] - ts):
            i -= 1
        point_ts, value = self._points[i]
        self.cursor.clear()
        self.cursor.append(QPointF(point_ts * 1000.0, self.axis_y.min()))
        self.cursor.append(QPointF(point_ts * 1000.0, self.axis_y.max()))
        self.marker.clear()
        self.marker.append(QPointF(point_ts * 1000.0, value))
        return value

    def hide_cursor(self) -> None:
        self.cursor.clear()
        self.marker.clear()


class ChartsPanel(QWidget):
    """Выбор диапазона/шага, сводка за диапазон и четыре синхронных графика."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        ctl = QHBoxLayout()
        lbl = QLabel("Диапазон")
        lbl.setStyleSheet(f"color:{AXIS};")
        ctl.addWidget(lbl)
        self.cmb_range = QComboBox()
        for label, sec in RANGES:
            self.cmb_range.addItem(label, sec)
        self.cmb_range.setCurrentIndex(2)
        ctl.addWidget(self.cmb_range)
        ctl.addSpacing(10)
        lbl_step = QLabel("Шаг")
        lbl_step.setStyleSheet(f"color:{AXIS};")
        ctl.addWidget(lbl_step)
        self.cmb_step = QComboBox()
        for label, sec in STEPS:
            self.cmb_step.addItem(label, sec)
        self.cmb_step.setCurrentIndex(0)
        ctl.addWidget(self.cmb_step)
        ctl.addSpacing(16)
        self.lbl_summary = QLabel("—")
        self.lbl_summary.setStyleSheet(f"color:{TEXT};")
        ctl.addWidget(self.lbl_summary, 1)
        root.addLayout(ctl)

        grid = QGridLayout()
        grid.setSpacing(6)
        self.c_temp = ChartBox("Температура ядра", "°C", "#e0603a")
        self.c_usd = ChartBox("Доход", "₽ за шаг", "#4caf50")
        self.c_pow = ChartBox("Мощность", "Вт", "#4d9de0")
        self.c_kwh = ChartBox("Затраты", "кВт·ч за шаг", "#c9a227")
        self._boxes = {"temp": self.c_temp, "usd": self.c_usd,
                       "power": self.c_pow, "kwh": self.c_kwh}
        grid.addWidget(self.c_temp, 0, 0)
        grid.addWidget(self.c_usd, 0, 1)
        grid.addWidget(self.c_pow, 1, 0)
        grid.addWidget(self.c_kwh, 1, 1)
        root.addLayout(grid, 1)

        for box in self._boxes.values():
            box.view.hovered.connect(self._on_hover)

        # своя подсказка вместо QToolTip: системная всплывашка исчезает через несколько
        # секунд, даже если курсор стоит на месте, и не переживает обновление графиков
        self.tip = QLabel(self)
        self.tip.setStyleSheet(
            "QLabel{background:#1b232b;color:#e8eef4;border:1px solid #3a444e;"
            "padding:5px 8px;font-size:12px;}")
        self.tip.setFont(QFont("Consolas", 9))
        self.tip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.tip.hide()

        self.cmb_range.currentIndexChanged.connect(self.refresh)
        self.cmb_step.currentIndexChanged.connect(self.refresh)
        self._usd_rub = 80.0
        self._buckets: list[dict] = []
        self._hover_ts: float | None = None
        self.refresh()

    def set_rate(self, usd_rub: float) -> None:
        self._usd_rub = usd_rub

    # --- данные ------------------------------------------------------------
    def refresh(self) -> None:
        span = int(self.cmb_range.currentData() or 0)
        step = int(self.cmb_step.currentData() or 60)
        now = time.time()
        start = now - span if span else None
        rows = history.load(start, now)
        if not rows:
            self.lbl_summary.setText("история копится: первые точки появятся через минуту работы")
            for box, key, _fmt, scale in self._plots("hh:mm"):
                box.plot([], key, _fmt, scale)
            self._buckets = []
            self._hover_ts = None
            self.tip.hide()
            return
        eff_step, fmt = self._effective_step(step, rows, span)
        self._buckets = history.aggregate(rows, eff_step)
        s = history.summary(rows)
        rate = self._usd_rub
        usd_rub = s["usd"] * rate
        light_rub = s["kwh"] * rates.electricity_rub_kwh()
        self.lbl_summary.setText(
            f"{self._range_label()} · шаг {self._step_label(eff_step)} · "
            f"доход {usd_rub:.2f} ₽ · свет {light_rub:.2f} ₽ · "
            f"{s['kwh']:.3f} кВт·ч · ср. {s['power_avg'] or 0:.0f} Вт · "
            f"ср. {s['temp_avg'] or 0:.1f} °C · {s['hours']:.1f} ч")
        for box, key, axis_fmt, scale in self._plots(fmt):
            box.plot(self._buckets, key, axis_fmt, scale)
        if self._hover_ts is not None:      # курсор не должен гаснуть при обновлении
            self._on_hover(self._hover_ts * 1000.0)

    def _plots(self, fmt: str):
        rate = self._usd_rub
        return [
            (self.c_temp, "temp_c", fmt, 1.0),
            (self.c_usd, "usd", fmt, rate),
            (self.c_pow, "power_w", fmt, 1.0),
            (self.c_kwh, "kwh", fmt, 1.0),
        ]

    def _range_label(self) -> str:
        return self.cmb_range.currentText()

    @staticmethod
    def _step_label(step: int) -> str:
        for label, sec in STEPS:
            if sec == step:
                return label
        return f"{step // 60} мин"

    def _effective_step(self, step: int, rows: list[dict], span: int) -> tuple[int, str]:
        total_span = span or (rows[-1]["ts"] - rows[0]["ts"])
        for cand in (s for _, s in STEPS):
            if cand >= step and total_span / cand <= MAX_POINTS:
                step = cand
                break
        fmt = "dd.MM hh:mm" if total_span > 2 * 86400 else "hh:mm"
        return step, fmt

    # --- синхронный курсор -------------------------------------------------
    def _on_hover(self, ms) -> None:
        if ms is None:
            for box in self._boxes.values():
                box.hide_cursor()
            self._hover_ts = None
            self.tip.hide()
            return
        if not self._buckets:
            return
        ts = ms / 1000.0
        stamps = [b["ts"] for b in self._buckets]
        i = bisect.bisect_left(stamps, ts)
        i = min(max(i, 0), len(stamps) - 1)
        if i > 0 and abs(stamps[i - 1] - ts) < abs(stamps[i] - ts):
            i -= 1
        bucket = self._buckets[i]
        self._hover_ts = bucket["ts"]
        values = {name: box.show_at(bucket["ts"]) for name, box in self._boxes.items()}

        def dec(name: str, minimum: int = 0) -> int:
            box = self._boxes[name]
            span = box.axis_y.max() - box.axis_y.min()
            return max(minimum, _decimals(span, max(box.view.width(), 1)))

        stamp = time.strftime("%d.%m %H:%M", time.localtime(bucket["ts"]))
        lines = [stamp]
        if values["temp"] is not None:
            lines.append(f"температура  {values['temp']:.{dec('temp', 1)}f} °C")
        if values["usd"] is not None:
            lines.append(f"доход        {values['usd']:.{max(2, dec('usd'))}f} ₽")
        if values["power"] is not None:
            lines.append(f"мощность     {values['power']:.0f} Вт")
        if values["kwh"] is not None:
            lines.append(f"затраты      {values['kwh']:.{dec('kwh', 2)}f} кВт·ч")
        self._show_tip("\n".join(lines))

    def _show_tip(self, text: str) -> None:
        """Своя всплывашка рядом с курсором: держится, пока курсор на графике."""
        self.tip.setText(text)
        self.tip.adjustSize()
        pos = self.mapFromGlobal(QCursor.pos())
        x, y = pos.x() + 18, pos.y() + 16
        if x + self.tip.width() > self.width() - 4:
            x = max(4, pos.x() - self.tip.width() - 18)
        if y + self.tip.height() > self.height() - 4:
            y = max(4, pos.y() - self.tip.height() - 16)
        self.tip.move(x, y)
        self.tip.show()
        self.tip.raise_()
