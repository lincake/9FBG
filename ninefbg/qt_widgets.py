"""Small native Qt plotting widgets shared by the desktop workspaces.

The plot receives display copies only. It never resamples a calibration axis,
interpolates missing measurements or calls an instrument.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QToolTip, QWidget


COLORS = ("#a86408", "#087f6b", "#246bc1", "#8a49b3", "#c04448",
          "#6b7a0d", "#087d98", "#b23d7d", "#5a59ac")


def finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def checked_range(value):
    if value is None:
        return None
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or not all(finite(item) for item in value) or value[1] <= value[0]
            or not finite(value[1] - value[0])):
        raise ValueError("坐标范围需要有限数值，且上限必须大于下限")
    return float(value[0]), float(value[1])


def axis_ticks(bounds, count=5):
    """Return evenly labelled 1/2/2.5/5 ticks without rounding source values."""
    low, high = checked_range(bounds)
    target = (high - low) / max(1, count - 1)
    if target <= 0 or not finite(target):
        return [low, high], lambda value: f"{value:.4g}"
    power = 10.0 ** math.floor(math.log10(target))
    if not power:
        return [low, high], lambda value: f"{value:.4g}"
    factor = next((item for item in (1, 2, 2.5, 5, 10) if item * power >= target), 10)
    step = factor * power
    digits = max(0, -math.floor(math.log10(step)) + (1 if factor == 2.5 else 0))
    first, last = math.ceil(low / step - 1e-10), math.floor(high / step + 1e-10)
    values = [index * step for index in range(first, min(last + 1, first + 40))]
    scientific = digits > 8 or max(abs(low), abs(high)) >= 1e9

    def format_value(value):
        if abs(value) < step * 1e-8:
            value = 0
        return f"{value:.2e}" if scientific else f"{value:.{digits}f}"

    return values or [low, high], format_value


class TrendPlot(QWidget):
    """QPainter plot with real x coordinates, optional points and explicit gaps.

    ``set_series`` accepts dictionaries containing ``name``, ``x``, ``y`` and
    ``color``. ``points=True`` renders individual markers; ``dashed=True`` draws
    a dashed path. ``breaks`` optionally flags source samples that start a new
    segment. A missing/non-finite sample breaks a line. Explicit axis
    ranges can be reset to automatic with ``None``. ``show_legend=False`` hides
    the legend while retaining series names for hover values.
    """

    def __init__(self, parent=None, *, show_legend=True):
        super().__init__(parent)
        self.series = []
        self.show_legend = show_legend
        self.x_label, self.y_label, self.title = "时间 / s", "数值", ""
        self.empty_text = "等待有效数据"
        self._x_range = self._y_range = None
        self._bands, self._markers = [], []
        self._area = QRectF()
        self._paint_ranges = ((0.0, 1.0), (-1.0, 1.0))
        self.setMinimumSize(220, 180)
        self.setMouseTracking(True)
        self.setObjectName("nativeTrendPlot")

    def sizeHint(self):
        return QSize(600, 260)

    def set_series(self, series):
        # Copies prevent a producer from changing a path during paintEvent.
        self.series = [{**item, "x": list(item.get("x", [])), "y": list(item.get("y", [])),
                        "breaks": list(item.get("breaks", []))}
                       for item in (series or []) if isinstance(item, dict)]
        self.update()

    def set_labels(self, x_label, y_label):
        self.x_label, self.y_label = str(x_label), str(y_label)
        self.update()

    def set_title(self, title):
        self.title = str(title)
        self.update()

    def set_empty_text(self, text):
        self.empty_text = str(text)
        self.update()

    def set_x_range(self, bounds=None):
        self._x_range = checked_range(bounds)
        self.update()

    def set_y_range(self, bounds=None):
        self._y_range = checked_range(bounds)
        self.update()

    def set_bands(self, bands):
        """Display window bands: [{min, max, color, label}]."""
        self._bands = list(bands or [])
        self.update()

    def set_markers(self, markers):
        """Display source-coordinate markers: [{x, y, color, label}]."""
        self._markers = list(markers or [])
        self.update()

    @staticmethod
    def _auto_range(values, default, padding=False):
        values = [value for value in values if finite(value)]
        if not values:
            return default
        low, high = min(values), max(values)
        if high == low:
            pad = max(abs(low) * .05, 1.0 if low == 0 else .001)
            low, high = low - pad, high + pad
        elif padding:
            pad = (high - low) * .08
            low, high = low - pad, high + pad
        try:
            return checked_range((low, high))
        except ValueError:
            return default

    def data_ranges(self):
        points = [(x, y) for item in self.series for x, y in zip(item["x"], item["y"])
                  if finite(x) and finite(y)]
        xs = [x for x, _ in points]
        x_bounds = self._x_range or self._auto_range(xs, (0.0, 1.0))
        ys = [y for x, y in points if x_bounds[0] <= x <= x_bounds[1]]
        y_bounds = self._y_range or self._auto_range(ys, (-1.0, 1.0), True)
        return x_bounds, y_bounds

    def _point(self, x, y):
        xr, yr = self._paint_ranges
        return QPointF(self._area.left() + (x - xr[0]) / (xr[1] - xr[0]) * self._area.width(),
                       self._area.bottom() - (y - yr[0]) / (yr[1] - yr[0]) * self._area.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        label_font = QFont(self.font())
        label_font.setPointSizeF(9.0)
        label_font.setWeight(QFont.Weight.Normal)
        painter.setFont(label_font)
        metrics = painter.fontMetrics()
        self._paint_ranges = self.data_ranges()
        xr, yr = self._paint_ranges
        xt, xf = axis_ticks(xr, max(2, min(7, self.width() // 110)))
        yt, yf = axis_ticks(yr)
        left_margin = max(68, max((metrics.horizontalAdvance(yf(y)) for y in yt), default=0) + 20)
        plot_width = max(1, self.width() - left_margin - 22)
        names = [(item.get("name", ""), item.get("color", COLORS[index % len(COLORS)]))
                 for index, item in enumerate(self.series) if self.show_legend and item.get("name")]
        legend_rows, row, row_width = [], [], 0
        for name, color in names:
            width = min(plot_width, metrics.horizontalAdvance(str(name)) + 40)
            if row and row_width + width > plot_width:
                legend_rows.append((row, row_width))
                row, row_width = [], 0
            row.append((str(name), color, width))
            row_width += width
        if row:
            legend_rows.append((row, row_width))
        legend_top = 42 if self.title else 8
        top = legend_top + len(legend_rows) * 21 + 25
        self._area = QRectF(left_margin, top, plot_width, max(1, self.height() - top - 52))
        text_color, grid_color = QColor("#64748b"), QColor("#e9eff7")
        if self.title:
            title_font = QFont(self.font())
            title_font.setPointSizeF(11.0)
            title_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(title_font)
            painter.setPen(QColor("#23364d"))
            painter.drawText(QRectF(self._area.left(), 9, self._area.width(), 26),
                             Qt.AlignmentFlag.AlignCenter, self.title)
            painter.setFont(label_font)
        for row_index, (entries, width) in enumerate(legend_rows):
            left = self._area.left() + (plot_width - width) / 2
            y = legend_top + row_index * 21
            for name, color, entry_width in entries:
                painter.setPen(QPen(QColor(color), 2.2, Qt.PenStyle.SolidLine,
                                    Qt.PenCapStyle.RoundCap))
                painter.drawLine(QPointF(left + 2, y + 7), QPointF(left + 16, y + 7))
                painter.setPen(text_color)
                name = metrics.elidedText(name, Qt.TextElideMode.ElideRight, max(1, int(entry_width - 30)))
                painter.drawText(QRectF(left + 23, y - 3, entry_width - 25, 20),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
                left += entry_width
        for x in xt:
            pos = self._point(x, yr[0]).x()
            painter.setPen(grid_color)
            painter.drawLine(QPointF(pos, self._area.top()), QPointF(pos, self._area.bottom()))
            painter.setPen(text_color)
            painter.drawText(QRectF(pos - 55, self._area.bottom() + 6, 110, 20),
                             Qt.AlignmentFlag.AlignHCenter, xf(x))
        for y in yt:
            pos = self._point(xr[0], y).y()
            painter.setPen(grid_color)
            painter.drawLine(QPointF(self._area.left(), pos), QPointF(self._area.right(), pos))
            painter.setPen(text_color)
            painter.drawText(QRectF(0, pos - 9, self._area.left() - 11, 20),
                             Qt.AlignmentFlag.AlignRight, yf(y))
        painter.setPen(QPen(QColor("#ccd7e6"), .9))
        painter.drawRect(self._area)
        painter.setPen(text_color)
        painter.drawText(QRectF(self._area.left(), self.height() - 24, self._area.width(), 20),
                         Qt.AlignmentFlag.AlignCenter, self.x_label)
        painter.drawText(QRectF(self._area.left(), top - 25, self._area.width(), 20),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.y_label)
        painter.save()
        painter.setClipRect(self._area.adjusted(0, 0, 1, 1))
        for band in self._bands:
            low, high = band.get("min"), band.get("max")
            if not finite(low) or not finite(high) or high <= low:
                continue
            color = QColor(band.get("color", "#8295aa"))
            fill = QColor(color)
            fill.setAlpha(16)
            left, right = self._point(low, yr[0]).x(), self._point(high, yr[0]).x()
            painter.fillRect(QRectF(left, self._area.top(), right - left, self._area.height()), fill)
            color.setAlpha(90)
            painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(left, self._area.top()), QPointF(left, self._area.bottom()))
            painter.drawLine(QPointF(right, self._area.top()), QPointF(right, self._area.bottom()))
            painter.drawText(QPointF(left + 4, self._area.top() + 14), str(band.get("label", "")))
        has_data = False
        for index, item in enumerate(self.series):
            color = QColor(item.get("color", COLORS[index % len(COLORS)]))
            pen = QPen(color, float(item.get("width", 1.7)))
            if item.get("dashed"):
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            path, open_segment = QPainterPath(), False
            for sample_index, (x, y) in enumerate(zip(item["x"], item["y"])):
                if sample_index < len(item["breaks"]) and item["breaks"][sample_index]:
                    open_segment = False
                if not finite(x) or not finite(y):
                    open_segment = False
                    continue
                point = self._point(x, y)
                if not finite(point.x()) or not finite(point.y()):
                    open_segment = False
                    continue
                has_data = True
                if item.get("points"):
                    painter.drawEllipse(point, 2.8, 2.8)
                elif open_segment:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                open_segment = True
            if not item.get("points"):
                painter.drawPath(path)
        for marker in self._markers:
            if not finite(marker.get("x")) or not finite(marker.get("y")):
                continue
            point = self._point(marker["x"], marker["y"])
            painter.setPen(QPen(QColor(marker.get("color", "#246bc1")), 1.5))
            painter.drawEllipse(point, 4, 4)
            painter.drawText(point + QPointF(5, -7), str(marker.get("label", "")))
        painter.restore()
        if not has_data:
            painter.setPen(text_color)
            painter.drawText(self._area, Qt.AlignmentFlag.AlignCenter, self.empty_text)

    def mouseMoveEvent(self, event):
        if not self._area.contains(event.position()):
            QToolTip.hideText()
            return
        xr, _ = self._paint_ranges
        wanted = xr[0] + (event.position().x() - self._area.left()) / self._area.width() * (xr[1] - xr[0])
        lines = []
        for item in self.series[:12]:
            candidates = [(x, y) for x, y in zip(item["x"], item["y"]) if finite(x) and finite(y)]
            if candidates:
                x, y = min(candidates, key=lambda pair: abs(pair[0] - wanted))
                if abs(x - wanted) <= (xr[1] - xr[0]) * .035:
                    lines.append(f"{item.get('name', '数据')}：{x:.7g} / {y:.7g}")
        if lines:
            QToolTip.showText(event.globalPosition().toPoint(), "\n".join(lines), self)
        else:
            QToolTip.hideText()
