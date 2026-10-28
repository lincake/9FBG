"""Native live acquisition display. Instrument and peak logic stays in service.

All conversions here are for presentation (nm to pm, or a local display zero).
No native widget changes the fixed FBG identities, fits or stored raw frames.
"""
from __future__ import annotations

from collections import deque
import statistics
import time

from PyQt6.QtCore import QSignalBlocker, QTimer, Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QHeaderView, QVBoxLayout, QWidget,
)

from .qt_widgets import COLORS, TrendPlot, checked_range, finite
from .qt_theme import ui_font


IDS = tuple(f"FBG{index}" for index in range(1, 10))
AXES = ("Fx", "Fy", "Fz", "Mx", "My", "Mz")
UNITS = ("N", "N", "N", "N·m", "N·m", "N·m")
LABELS = {
    "disconnected": "尚未连接", "idle": "等待无载初始化", "uninitialized": "等待无载初始化",
    "waiting": "等待稳定帧", "stabilizing": "正在检查无载稳定性", "initializing": "正在识别 9 个主峰",
    "tracking": "持续跟踪中", "ready": "持续跟踪中", "initialized": "持续跟踪中",
    "pressing": "按压采集中", "error": "需要处理异常", "failed": "检查未通过",
    "hardware": "实际设备", "replay": "原始数据回放", "unknown": "尚未核实", "unavailable": "不可用",
    "valid": "有效", "missing": "缺失", "pending": "等待核验", "none": "无", "raw": "原始数据",
    "stable": "稳定", "unstable": "光谱尚未稳定", "collecting": "正在收集稳定帧",
    "reference_waiting": "等待确认后的新帧", "reference_pending": "正在检查无载稳定性",
    "awaiting_confirmation": "等待无载确认", "input_invalid": "原始帧不可用",
    "initialization_failed": "初始化未通过", "tracking_degraded": "部分光栅质量异常",
    "low_prominence": "显著度不足", "fit_failed": "局部拟合失败", "saturated": "探测器饱和",
    "overlap": "峰重叠", "distorted": "峰形严重变形", "abnormal_jump": "波长异常跳变",
    "missing_peak": "跟踪窗口内漏峰", "invalid_frame": "光谱帧无效", "low_amplitude": "峰幅度不足",
    "width_out_of_bounds": "峰宽超出约束", "residual_too_large": "拟合残差过大",
    "window_boundary": "峰接近跟踪窗口边界", "out_of_window": "峰超出跟踪窗口",
    "insufficient_points": "局部原始采样点不足", "gaussian": "局部受约束高斯拟合",
    "template_shift": "局部参考光谱位移拟合", "raw_sdk_uint16_counts": "SDK 原始 uint16 计数",
    "host_rx_approx": "主机接收近似时间", "hardware_mapped": "硬件时钟已映射",
    "device_timestamp": "设备采集时间", "device": "设备采集时间", "host_receive": "主机接收时间",
    "host_rx_monotonic": "主机接收单调时钟", "host_receive_monotonic": "主机接收单调时钟",
    "recorded_host_receive": "原始主机接收时间", "recorded": "原始记录时间", "stale": "旧值",
    "current": "当前帧", "good": "可靠", "poor": "时间质量较低", "not_attested": "尚未核实",
    "exact_receive_time": "同一接收时刻匹配", "linear_receive_time": "接收时间插值匹配",
    "nearest_receive_time": "最近接收时刻匹配", "no_match": "未找到同步参考",
}


def fmt(value, digits=2):
    if not finite(value):
        return "—"
    text = f"{value:.{digits}f}"
    return text.removeprefix("-") if float(text) == 0 else text


def label(value, fallback="—"):
    if value is None or value == "":
        return fallback
    if isinstance(value, dict):
        return label(value.get("label") or value.get("status") or value.get("quality") or value.get("kind"), fallback)
    return LABELS.get(str(value), str(value))


def validate_spectrum(frame):
    """Validate all 512 actual device calibration coordinates, never invent an axis."""
    frame = frame if isinstance(frame, dict) else {}
    xs, ys = frame.get("wavelength_nm"), frame.get("intensity")
    if not isinstance(xs, list) or not isinstance(ys, list) or len(xs) != 512 or len(ys) != 512:
        return {"valid": False, "reason": "光谱需要完整的 512 点实际波长轴与原始强度"}
    if not all(finite(value) for value in xs + ys):
        return {"valid": False, "reason": "光谱波长与强度必须为有限数值"}
    if any(right <= left for left, right in zip(xs, xs[1:])):
        return {"valid": False, "reason": "实际波长轴必须严格递增，不能重复或倒序"}
    low, high = min(ys), max(ys)
    if low == high:
        pad = max(abs(low) * .05, 1)
        low, high = low - pad, high + pad
    try:
        xr, yr = checked_range((xs[0], xs[-1])), checked_range((low, high))
    except ValueError:
        return {"valid": False, "reason": "光谱坐标范围超出可显示范围"}
    return {"valid": True, "reason": None, "x_range": xr, "y_range": yr}


def normalize_peaks(peaks, *, stale=False):
    """Retain IDs, reject duplicates and never renumber a partial peak list."""
    by_id = {}
    for peak in peaks if isinstance(peaks, list) else []:
        if not isinstance(peak, dict):
            continue
        identity = str(peak.get("id", "")).upper()
        if identity in tuple(str(i) for i in range(1, 10)):
            identity = "FBG" + identity
        if identity not in IDS or identity in by_id:
            continue
        row = {**peak, "id": identity, "valid": peak.get("valid") is True,
               "stale": peak.get("stale") is True}
        if stale and (row["valid"] or row["stale"]):
            row.update(valid=False, stale=True)
            row["failure_reason"] = row.get("failure_reason") or "采集已停止或光谱超过 1 秒未更新"
        if row["stale"]:
            row["valid"] = False
        by_id[identity] = row
    return [by_id.get(identity, {"id": identity, "valid": False, "stale": False,
                                "failure_reason": "尚未获得测量"}) for identity in IDS]


def normalize_force(force, *, running=True, connected=True, elapsed_ms=0):
    force = force if isinstance(force, dict) else {}
    base = {"valid": False, "stale": False, "values": None, "reason": "",
            "epoch": force.get("stream_epoch"), "frame_id": force.get("frame_id"),
            "stamp": force.get("host_rx_monotonic_ns")}

    def reject(reason, stale=False):
        return {**base, "reason": reason, "stale": stale}

    if not running or not connected:
        return reject("六轴参考已断开或采集已停止", True)
    if not force:
        return reject("尚未收到六轴参考帧")
    age = force.get("age_ms")
    if force.get("stale") is True or finite(age) and age + elapsed_ms > 250:
        return reject("六轴参考超过 250 ms 未更新", True)
    if force.get("valid") is False:
        return reject(force.get("reason") or "六轴参考帧无效")
    if force.get("error"):
        return reject("六轴参考帧包含错误：" + str(force["error"]))
    if age is not None and (not finite(age) or age < 0):
        return reject("六轴参考帧时效信息无效")
    values = force.get("force_values")
    if not isinstance(values, list) or len(values) != 6 or not all(finite(value) for value in values):
        return reject("六轴参考必须包含 6 个有限数值")
    axes = force.get("axes", list(AXES))
    if (not isinstance(axes, list) or len(axes) != 6 or not all(isinstance(axis, str) and axis in AXES for axis in axes)
            or len(set(axes)) != 6):
        return reject("六轴参考轴标识缺失、重复或未知")
    units = force.get("units", [UNITS[AXES.index(axis)] for axis in axes])
    if not isinstance(units, list) or len(units) != 6 or any(unit != UNITS[AXES.index(axis)] for unit, axis in zip(units, axes)):
        return reject("六轴参考单位错误：力应为 N，力矩应为 N·m")
    return {**base, "valid": True, "values": [values[axes.index(axis)] for axis in AXES]}


class DisplayZero:
    """Local-only display zero, using the unchanged source frame identity rule."""

    def __init__(self):
        self.version = 0
        self.offsets, self.epoch = None, None
        self.pending = False
        self.reason = "尚未进行显示置零"
        self.samples, self.seen, self.last = [], set(), None

    def clear(self, reason="显示零点已重置"):
        if self.offsets is not None:
            self.version += 1
        self.offsets, self.epoch = None, None
        self.pending = False
        self.reason = reason
        self.samples, self.seen, self.last = [], set(), None

    @staticmethod
    def identifiable(force):
        identity = force.get("frame_id")
        return (isinstance(force.get("epoch"), str) and bool(force["epoch"])
                and (isinstance(identity, str) and bool(identity.strip()) or type(identity) is int and identity >= 0)
                and type(force.get("stamp")) is int and force["stamp"] > 0)

    def request(self, confirmed, force):
        if confirmed is not True:
            raise ValueError("请先确认接触棒已移开，六轴参考当前无载")
        if force.get("valid") is not True:
            raise ValueError(force.get("reason") or "显示置零需要新鲜有效的六轴参考帧")
        if not self.identifiable(force):
            raise ValueError("显示置零需要原始采集时段、帧标识和接收时间")
        self.clear("正在收集无载六轴参考帧")
        self.pending, self.epoch = True, force["epoch"]
        self.update(force)

    def update(self, force):
        if force.get("valid") is not True or not self.identifiable(force):
            self.clear(force.get("reason") or "六轴参考缺少原始采集身份，显示零点已清除")
            return
        key, stamp = (force["epoch"], force["frame_id"]), force["stamp"]
        if self.epoch is not None and self.epoch != force["epoch"]:
            self.clear("六轴参考采集时段已改变，请重新确认无载并显示置零")
            return
        if self.last:
            old_key, old_stamp = self.last
            backwards_id = type(old_key[1]) is int and type(key[1]) is int and key[1] < old_key[1]
            if stamp < old_stamp or backwards_id or key != old_key and (stamp == old_stamp or key in self.seen):
                self.clear("六轴参考帧重复、乱序或时间回退，显示零点已清除")
                return
            if key == old_key:
                return
        self.epoch, self.last = force["epoch"], (key, stamp)
        self.seen.add(key)
        if len(self.seen) > 2048:
            self.seen = {key}
        if not self.pending:
            return
        self.samples.append((stamp, list(force["values"])))
        while len(self.samples) > 12 and self.samples[1][0] <= stamp - 400_000_000:
            self.samples.pop(0)
        if len(self.samples) < 12 or stamp - self.samples[0][0] < 400_000_000:
            self.reason = "已收集 12 帧，等待无载采样跨越 400 ms" if len(self.samples) >= 12 else "正在收集无载六轴参考帧"
            return
        selected = [self.samples[round(index * (len(self.samples) - 1) / 11)] for index in range(12)]
        self.offsets = [statistics.median(sample[1][axis] for sample in selected) for axis in range(6)]
        self.pending = False
        self.version += 1
        self.samples = selected
        self.reason = "显示已置零（12 个不同源帧的中位数，采样跨度至少 400 ms）"

    def correct(self, values):
        return [value - (self.offsets[index] if self.offsets is not None else 0) for index, value in enumerate(values)]


class LivePage(QWidget):
    """Live Qt page. The main window calls update_state exactly once per snapshot."""

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.state = {}
        self.received_at = time.monotonic()
        self._restoring = False
        self._manual_range = None
        self._fresh_signature = None
        self._optical_history, self._force_history = deque(maxlen=2400), deque(maxlen=2400)
        self._optical_binding, self._optical_identity, self._force_identity = None, None, None
        self._optical_break, self._force_break = True, True
        self._channel_breaks = [True] * 9
        self._optical_starts = {}
        self._optical_seen, self._force_seen = set(), set()
        self.zero = DisplayZero()
        self._build()
        self.set_preferences(getattr(ctx, "preferences", {}))
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.update_state({})

    @staticmethod
    def _text(value="", wrap=True):
        widget = QLabel(value)
        widget.setWordWrap(wrap)
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return widget

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("liveBody")
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 10, 4, 10)
        layout.setSpacing(14)
        status_row = QHBoxLayout()
        status_row.addStretch(1)
        status_column = QVBoxLayout()
        status_column.setSpacing(3)
        self.frame_count_label = self._text("帧数：0", wrap=False)
        self.frame_count_label.setObjectName("liveFrameCount")
        self.frame_count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.frame_count_label.setToolTip("当前连接累计接收的光谱帧数")
        status_column.addWidget(self.frame_count_label)
        self.summary = self._text("有效光栅：0 / 9", wrap=False)
        self.summary.setObjectName("liveSummary")
        self.summary.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_column.addWidget(self.summary)
        status_row.addLayout(status_column)
        layout.addLayout(status_row)

        group = QGroupBox()
        group.setObjectName("spectrumCard")
        section = QVBoxLayout(group)
        section.setContentsMargins(18, 12, 18, 18)
        section.setSpacing(16)
        self.spectrum = TrendPlot()
        self.spectrum.setMinimumHeight(320)
        self.spectrum.set_title("实际标定光谱 · 512 点原始计数")
        self.spectrum.set_labels("实际波长 / nm", "原始强度 / 计数")
        section.addWidget(self.spectrum)
        controls = QGridLayout()
        self.range_mode = QComboBox()
        self.range_mode.addItem("完整标定轴 · 自动强度", "auto")
        self.range_mode.addItem("手动显示范围", "manual")
        controls.addWidget(self.range_mode, 0, 0, 1, 2)
        self.range_inputs = {}
        for column, (key, text) in enumerate((("spectrum_x_min", "波长下限 / nm"),
                                              ("spectrum_x_max", "波长上限 / nm"),
                                              ("spectrum_y_min", "强度下限 / 计数"),
                                              ("spectrum_y_max", "强度上限 / 计数"))):
            field_label = QLabel(text)
            field_label.setProperty("role", "muted")
            controls.addWidget(field_label, 1, column)
            entry = QLineEdit()
            entry.setPlaceholderText("自动")
            self.range_inputs[key] = entry
            controls.addWidget(entry, 2, column)
            controls.setColumnStretch(column, 1)
        controls.setHorizontalSpacing(14)
        controls.setVerticalSpacing(8)
        self.show_windows = QCheckBox("显示固定 FBG 跟踪窗口")
        self.show_windows.setChecked(True)
        controls.addWidget(self.show_windows, 0, 2, 1, 2)
        section.addLayout(controls)
        actions = QHBoxLayout()
        self.apply_range = QPushButton("应用显示范围")
        self.apply_range.setProperty("role", "primary")
        self.reset_range = QPushButton("恢复完整光谱")
        self.reset_range.setProperty("role", "ghost")
        actions.addWidget(self.apply_range)
        actions.addWidget(self.reset_range)
        actions.addStretch()
        section.addLayout(actions)
        self.spectrum_status = self._text()
        self.spectrum_status.setProperty("role", "muted")
        section.addWidget(self.spectrum_status)
        layout.addWidget(group)
        self.range_mode.currentIndexChanged.connect(self._range_mode_changed)
        self.apply_range.clicked.connect(self._apply_range)
        self.reset_range.clicked.connect(self._reset_range)
        self.show_windows.toggled.connect(self._windows_changed)

        group = QGroupBox("九路波长趋势")
        section = QVBoxLayout(group)
        self.reference_label = self._text()
        section.addWidget(self.reference_label)
        self.reference_label.hide()
        self.peak_table = QTableWidget(9, 8)
        self.peak_table.setHorizontalHeaderLabels(("光栅", "峰心 / nm", "漂移 / pm", "幅度 / 计数",
                                                  "峰宽 / nm", "显著度 / 计数", "拟合残差", "质量与原因"))
        self.peak_table.verticalHeader().setVisible(False)
        self.peak_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.peak_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.peak_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.peak_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.peak_table.setMinimumHeight(295)
        for index in range(9):
            self.peak_table.setRowHeight(index, 28)
        section.addWidget(self.peak_table)
        self.peak_table.hide()
        self.warning_label = self._text()
        section.addWidget(self.warning_label)
        row = QHBoxLayout()
        self.wavelength_mode = QComboBox()
        self.wavelength_mode.addItem("波长变化 Δλ / pm", "pm")
        self.wavelength_mode.addItem("峰心波长 / nm", "nm")
        self.history_window = QComboBox()
        for seconds in (10, 30, 60, 120):
            self.history_window.addItem(f"最近 {seconds} s", seconds)
        self.history_window.setCurrentIndex(1)
        row.addWidget(self.wavelength_mode)
        row.addWidget(self.history_window)
        row.addStretch()
        section.addLayout(row)
        self.wavelength_plot = TrendPlot()
        self.wavelength_plot.setMinimumHeight(280)
        section.addWidget(self.wavelength_plot)
        self.history_status = self._text()
        section.addWidget(self.history_status)
        self.wavelength_mode.currentIndexChanged.connect(self._render_histories)
        self.history_window.currentIndexChanged.connect(self._render_histories)
        layout.addWidget(group)

        group = QGroupBox("PX6D 六轴参考 · 独立实时显示")
        section = QVBoxLayout(group)
        self.force_status = self._text()
        section.addWidget(self.force_status)
        self.force_value_labels, self.force_raw_labels = [], []
        grid = QGridLayout()
        for index, axis in enumerate(AXES):
            card = QGroupBox(f"{axis} / {UNITS[index]}")
            card_layout = QVBoxLayout(card)
            value = QLabel("—")
            value.setProperty("role", "metricValue")
            value.setFont(ui_font(20, QFont.Weight.DemiBold))
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            raw = self._text("原始 —\n显示零点 —")
            card_layout.addWidget(value)
            card_layout.addWidget(raw)
            self.force_value_labels.append(value)
            self.force_raw_labels.append(raw)
            grid.addWidget(card, 0, index)
        section.addLayout(grid)
        row = QHBoxLayout()
        self.unloaded_confirm = QCheckBox("已移开接触棒，确认六轴当前无载荷")
        self.zero_button = QPushButton("六轴显示置零")
        self.zero_reset_button = QPushButton("恢复原始显示")
        row.addWidget(self.unloaded_confirm)
        row.addWidget(self.zero_button)
        row.addWidget(self.zero_reset_button)
        row.addStretch()
        section.addLayout(row)
        self.unloaded_confirm.toggled.connect(self._refresh_zero_controls)
        self.zero_button.clicked.connect(self.request_display_zero)
        self.zero_reset_button.clicked.connect(self.reset_display_zero)
        layout.addWidget(group)
        layout.addStretch()

    def set_preferences(self, preferences):
        values = preferences.get("values", preferences) if isinstance(preferences, dict) else {}
        values = values.get("display", values)
        self._restoring = True
        blockers = [QSignalBlocker(self.range_mode), QSignalBlocker(self.show_windows)]
        self.range_mode.setCurrentIndex(1 if values.get("spectrum_mode") == "manual" else 0)
        self.show_windows.setChecked(values.get("show_windows", True) is True)
        for key, entry in self.range_inputs.items():
            if key in values:
                entry.setText(str(values[key]) if finite(values[key]) else "")
        self._manual_range = None
        if self.range_mode.currentData() == "manual":
            try:
                self._manual_range = self._read_range()
            except ValueError:
                pass
        del blockers
        self._restoring = False
        self._enable_range_inputs()
        if hasattr(self, "spectrum"):
            self._render_spectrum()

    def _save_preferences(self):
        if self._restoring:
            return
        patch = {"spectrum_mode": self.range_mode.currentData(), "show_windows": self.show_windows.isChecked()}
        for key, entry in self.range_inputs.items():
            try:
                value = float(entry.text())
                patch[key] = value if finite(value) else None
            except ValueError:
                patch[key] = None
        self.ctx.execute("settings", {"section": "display", "values": patch})

    def _enable_range_inputs(self):
        manual = self.range_mode.currentData() == "manual"
        for entry in self.range_inputs.values():
            entry.setEnabled(manual)

    def _read_range(self):
        values = {}
        for key, entry in self.range_inputs.items():
            try:
                values[key] = float(entry.text())
            except ValueError as exc:
                raise ValueError("手动显示范围需要填写 4 个有限数值") from exc
        return (checked_range((values["spectrum_x_min"], values["spectrum_x_max"])),
                checked_range((values["spectrum_y_min"], values["spectrum_y_max"])))

    def _range_mode_changed(self):
        self._enable_range_inputs()
        if self.range_mode.currentData() == "auto":
            self._manual_range = None
            self._save_preferences()
        self._render_spectrum()

    def _apply_range(self):
        try:
            if self.range_mode.currentData() == "manual":
                ranges = self._read_range()
                checked = validate_spectrum(self.state.get("frame"))
                if not checked["valid"]:
                    raise ValueError("请先接收完整的实际标定光谱，再应用手动范围")
                if ranges[0][0] < checked["x_range"][0] - .00001 or ranges[0][1] > checked["x_range"][1] + .00001:
                    raise ValueError("波长显示范围必须位于当前实际标定轴内")
                self._manual_range = ranges
            else:
                self._manual_range = None
            self._save_preferences()
            self._render_spectrum()
        except ValueError as exc:
            self.ctx.error.emit(str(exc))

    def _reset_range(self):
        self._manual_range = None
        self.range_mode.setCurrentIndex(0)
        self._render_spectrum()
        self._save_preferences()

    def _windows_changed(self):
        self._save_preferences()
        self._render_spectrum()

    def _elapsed_ms(self):
        return max(0, (time.monotonic() - self.received_at) * 1000)

    def _optical_stale(self):
        tracker = self.state.get("tracker") or {}
        age = tracker.get("display_age_ms")
        return (self.state.get("running") is not True or tracker.get("display_stale") is True
                or finite(age) and age + self._elapsed_ms() > 1000 or self._elapsed_ms() > 1000)

    def _same_optical_source(self):
        tracker, frame = self.state.get("tracker") or {}, self.state.get("frame") or {}
        stamp = tracker.get("host_rx_monotonic_ns")
        return (tracker.get("frame_id") is not None and tracker.get("frame_id") == frame.get("frame_id")
                and type(stamp) is int and stamp > 0 and stamp == frame.get("host_rx_monotonic_ns")
                and isinstance(tracker.get("stream_epoch"), str) and bool(tracker["stream_epoch"])
                and tracker.get("stream_epoch") == frame.get("stream_epoch"))

    def _display_peaks(self):
        tracker = self.state.get("tracker") or {}
        source_matches = self._same_optical_source()
        peaks = normalize_peaks(tracker.get("peaks"), stale=self._optical_stale() or not source_matches)
        for peak in peaks:
            if peak["valid"] and peak.get("value_frame_id") != tracker.get("frame_id"):
                peak.update(valid=False, stale=True, failure_reason="拟合值属于其他原始帧；等待当前帧结果")
            elif peak["stale"] and not source_matches:
                peak["failure_reason"] = "等待同一源帧的有效九峰结果；当前为旧值"
        return peaks

    def _current_force(self):
        devices = self.state.get("devices") or []
        device = next((item for item in devices if item.get("port_identity")), {})
        connected = self.state.get("force_connected", device.get("connected", bool(self.state.get("force"))))
        elapsed = self._elapsed_ms()
        # A missing source age cannot make a frozen application snapshot timeless.
        if elapsed > 250:
            return normalize_force(self.state.get("force"), running=False)
        return normalize_force(self.state.get("force"), running=self.state.get("running") is True,
                               connected=connected is True, elapsed_ms=elapsed)

    def update_state(self, state):
        self.state = state if isinstance(state, dict) else {}
        self.received_at = time.monotonic()
        self._ingest_histories()
        self._render()

    def _tick(self):
        signature = self._optical_stale(), self._current_force()["valid"]
        if signature != self._fresh_signature:
            self._optical_break = self._optical_break or signature[0]
            self._force_break = self._force_break or not signature[1]
            self._render()

    def _render(self):
        tracker = self.state.get("tracker") or {}
        peaks = self._display_peaks()
        count = sum(peak["valid"] and not peak["stale"] for peak in peaks)
        self.summary.setText(f"有效光栅：{count} / 9")
        metrics = self.state.get("metrics") or {}
        self.frame_count_label.setText(f"帧数：{fmt(metrics.get('received', 0), 0)}")
        reference = tracker.get("reference_version")
        frozen = tracker.get("press_active") is True or tracker.get("reference_frozen") is True
        self.reference_label.setText(
            f"参考：{'第 ' + str(reference) + ' 版' if reference else '未建立'}　·　{'按压期间冻结' if frozen else '等待无载确认' if not reference else '无载确认后可更新'}\n"
            f"算法 {tracker.get('algorithm_version') or '—'}　｜　配置 {tracker.get('config_version') or '—'}")
        for row_index, peak in enumerate(peaks):
            available = peak["valid"] or peak["stale"]
            quality = "旧值" if peak["stale"] else "有效" if peak["valid"] else "无效"
            reason = label(peak.get("failure_reason") if not peak["valid"] else peak.get("quality_warning") or peak.get("fit_availability"), "")
            values = [peak["id"], fmt(peak.get("center_nm"), 5) if available else "—",
                      fmt(peak.get("drift_nm") * 1000, 2) if available and finite(peak.get("drift_nm")) else "—",
                      *[fmt(peak.get(key), digits) if available else "—" for key, digits in
                        (("amplitude", 2), ("fwhm_nm", 4), ("prominence", 2), ("residual", 3))],
                      quality + (" · " + reason if reason else "")]
            for column, value in enumerate(values):
                item = self.peak_table.item(row_index, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.peak_table.setItem(row_index, column, item)
                item.setText(value)
                item.setToolTip(f"{peak['id']}　{quality}\n{reason}\n值所属原始帧：{peak.get('value_frame_id', '—')}\n"
                                f"距有效帧：{fmt(peak.get('age_ms'), 0)} ms")
                item.setBackground(QColor("#fff3d6" if peak["stale"] else "#fff0ef" if not peak["valid"] else "#ffffff"))
                if column == 0:
                    item.setForeground(QColor(COLORS[row_index]))
        warnings = list(tracker.get("warnings") or [])
        if tracker.get("windows") and tracker.get("drift_range_verified") is not True:
            warnings.append("跟踪窗口已固定；最大按压漂移范围尚待真实数据确认。")
        self.warning_label.setText("　·　".join(dict.fromkeys(str(item) for item in warnings)))
        self._render_spectrum()
        self._render_force()
        self._render_histories()
        self._fresh_signature = self._optical_stale(), self._current_force()["valid"]

    def _render_spectrum(self):
        frame, tracker = self.state.get("frame") or {}, self.state.get("tracker") or {}
        checked = validate_spectrum(frame)
        self.spectrum.set_markers([])
        self.spectrum.set_bands([])
        if not checked["valid"]:
            self.spectrum.set_series([])
            self.spectrum.set_empty_text("等待光谱数据" if not frame else "当前光谱不可用")
            self.spectrum_status.setText("连接光谱设备后显示实际标定光谱。" if not frame else checked["reason"])
            self.apply_range.setEnabled(False)
            return
        self.apply_range.setEnabled(True)
        xr, yr = checked["x_range"], checked["y_range"]
        yr = (min(0, yr[0]), max(1, yr[1] * 1.08))
        manual = self._manual_range
        invalid_saved = manual and (manual[0][0] < xr[0] - .00001 or manual[0][1] > xr[1] + .00001)
        if invalid_saved:
            manual = None
        if manual:
            xr, yr = manual
        self.spectrum.set_x_range(xr)
        self.spectrum.set_y_range(yr)
        stale = self._optical_stale()
        self.spectrum.set_labels("实际波长 / nm", "原始强度 / 计数" + (" · 旧帧" if stale else ""))
        series = [{"name": "原始光谱" + ("（旧帧）" if stale else ""), "x": frame["wavelength_nm"],
                   "y": frame["intensity"], "color": "#96a2af" if stale else "#203b58"}]
        markers = []
        peaks = self._display_peaks()
        for index, peak in enumerate(peaks):
            if not peak["valid"] or peak["stale"]:
                continue
            fx, fy = peak.get("fit_x"), peak.get("fit_y")
            if isinstance(fx, list) and isinstance(fy, list):
                series.append({"name": peak["id"], "x": fx, "y": fy, "color": COLORS[index], "width": 2})
            top = max((value for value in (fy or []) if finite(value)), default=None)
            if top is None and finite(peak.get("amplitude")):
                top = peak["amplitude"] + (peak.get("background") if finite(peak.get("background")) else 0)
            if finite(top) and finite(peak.get("center_nm")):
                markers.append({"x": peak["center_nm"], "y": top, "label": peak["id"], "color": COLORS[index]})
        if not tracker.get("reference_version") and not stale:
            for index, candidate in enumerate(tracker.get("candidates") or []):
                if not isinstance(candidate, dict) or not finite(candidate.get("center_nm")):
                    continue
                nearest = min(range(512), key=lambda pos: abs(frame["wavelength_nm"][pos] - candidate["center_nm"]))
                markers.append({"x": candidate["center_nm"], "y": frame["intensity"][nearest],
                                "label": f"候选 {index + 1}", "color": "#8295aa"})
        bands = []
        windows = tracker.get("windows") or []
        if isinstance(windows, dict):
            windows = [{"id": identity, "range_nm": bounds} for identity, bounds in windows.items()]
        if self.show_windows.isChecked():
            for index, window in enumerate(windows):
                if not isinstance(window, dict):
                    continue
                bounds = window.get("range_nm") or window.get("bounds_nm") or window.get("window_nm")
                if not isinstance(bounds, (list, tuple)):
                    bounds = [window.get("low_nm", window.get("left_nm", window.get("min_nm"))),
                              window.get("high_nm", window.get("right_nm", window.get("max_nm")))]
                try:
                    low, high = checked_range(bounds)
                except ValueError:
                    continue
                identity = window.get("id")
                color = COLORS[IDS.index(identity) if identity in IDS else index % 9]
                bands.append({"min": low, "max": high, "label": identity or f"窗口 {index + 1}", "color": color})
        self.spectrum.set_bands(bands)
        self.spectrum.set_markers(markers)
        self.spectrum.set_series(series)
        if self.range_mode.currentData() == "auto":
            for key, value in zip(self.range_inputs, (*xr, *yr)):
                self.range_inputs[key].setText(fmt(value, 5 if "_x_" in key else 1))
        hidden = sum(x < xr[0] or x > xr[1] or y < yr[0] or y > yr[1]
                     for x, y in zip(frame["wavelength_nm"], frame["intensity"]))
        self.spectrum_status.setText(
            ("旧帧 · " if stale else "") + f"实际标定轴 {fmt(checked['x_range'][0], 5)}～{fmt(checked['x_range'][1], 5)} nm · "
            + ("手动显示范围" if manual else "完整标定轴 / 自动强度")
            + (f" · {hidden} 点在显示范围外" if hidden else "")
            + (" · 已保存范围超出当前标定轴，暂用完整光谱" if invalid_saved else "")
            + "\n范围设置仅调整显示，不修改原始采样或提峰配置。")

    def request_display_zero(self):
        try:
            if self._zero_locked():
                raise ValueError("按压或采集会话进行中，请结束后重新确认无载并显示置零")
            self.zero.request(self.unloaded_confirm.isChecked(), self._current_force())
            self._force_history.clear()
            self._force_seen.clear()
            self._force_identity = None
            self._render_force()
        except ValueError as exc:
            self.ctx.error.emit(str(exc))

    def reset_display_zero(self):
        if self._zero_locked():
            return
        self.zero.clear()
        self._force_history.clear()
        self._force_seen.clear()
        self._force_identity = None
        self._render_force()
        self._render_histories()

    def _zero_locked(self):
        tracker = self.state.get("tracker") or {}
        return (tracker.get("press_active") is True or tracker.get("reference_frozen") is True
                or self.state.get("pressing") is True or (self.state.get("experiment") or {}).get("running") is True
                or self.state.get("recording") is True)

    def _refresh_zero_controls(self):
        force = self._current_force()
        locked = self._zero_locked()
        self.zero_button.setEnabled(force["valid"] and self.unloaded_confirm.isChecked() and not locked and not self.zero.pending)
        self.zero_reset_button.setEnabled(not locked and (self.zero.pending or self.zero.offsets is not None))
        self.unloaded_confirm.setEnabled(not locked and not self.zero.pending and force["valid"])
        if locked or not force["valid"]:
            self.unloaded_confirm.setChecked(False)

    def _render_force(self):
        force = self._current_force()
        old_version = self.zero.version
        self.zero.update(force)
        if self.zero.version != old_version:
            self._force_history.clear()
            self._force_seen.clear()
            self._force_identity = None
        values = self.zero.correct(force["values"]) if force["valid"] else None
        for index in range(6):
            digits = 3 if index < 3 else 5
            self.force_value_labels[index].setText(fmt(values[index], digits) if values else "—")
            self.force_value_labels[index].setStyleSheet("color: #087f6b" if values else "color: #8794a3")
            raw = fmt(force["values"][index], digits) if force["valid"] else "—"
            offset = self.zero.offsets[index] if self.zero.offsets is not None else 0
            self.force_raw_labels[index].setText(f"原始 {raw}\n显示零点 {fmt(offset, digits)}")
        sync = self.state.get("sync") or {}
        paired = force["valid"] and not self._optical_stale() and sync.get("valid") is True
        self.force_status.setText(("六轴实时 · " + ("光谱按主机接收时间近似配对" if paired else "当前未配对光谱；六轴独立更新"))
                                  if force["valid"] else force["reason"])
        self._refresh_zero_controls()

    def _ingest_histories(self):
        tracker, frame = self.state.get("tracker") or {}, self.state.get("frame") or {}
        binding = (tracker.get("stream_epoch"), tracker.get("reference_version"),
                   tuple(tracker.get("reference_nm") or []), tracker.get("algorithm_version"), tracker.get("config_version"))
        if binding != self._optical_binding:
            self._optical_history.clear()
            self._optical_binding, self._optical_identity, self._optical_break = binding, None, True
            self._channel_breaks, self._optical_seen = [True] * 9, set()
            self._optical_starts.clear()
        stamp, identity = tracker.get("host_rx_monotonic_ns"), (tracker.get("stream_epoch"), tracker.get("frame_id"))
        same_source = self._same_optical_source()
        usable = (not self._optical_stale() and same_source and type(stamp) is int and stamp > 0
                  and tracker.get("reference_version") and not tracker.get("reference_pending"))
        if usable and identity != self._optical_identity:
            if identity in self._optical_seen or self._optical_history and stamp <= self._optical_history[-1][0]:
                self._optical_break = True
            else:
                peaks = normalize_peaks(tracker.get("peaks"))
                values = [(peak.get("center_nm"), peak.get("drift_nm") * 1000)
                          if peak["valid"] and not peak["stale"] and peak.get("value_frame_id") == tracker.get("frame_id")
                          and finite(peak.get("center_nm")) and finite(peak.get("drift_nm")) else None for peak in peaks]
                if self._optical_history:
                    gap = self._optical_break or stamp - self._optical_history[-1][0] > 1_000_000_000
                    if gap:
                        self._optical_history.append((stamp - 1, [None] * 9))
                self._optical_starts[stamp] = list(self._channel_breaks)
                self._optical_history.append((stamp, values))
                if len(self._optical_starts) > 2400:
                    oldest = self._optical_history[0][0]
                    self._optical_starts = {source_stamp: starts for source_stamp, starts in self._optical_starts.items()
                                            if source_stamp >= oldest}
                self._optical_identity, self._optical_break = identity, False
                self._channel_breaks = [value is None for value in values]
                self._optical_seen.add(identity)
                if len(self._optical_seen) > 4800:
                    self._optical_seen = {identity}
        elif usable:
            for index, peak in enumerate(self._display_peaks()):
                if not peak["valid"]:
                    self._channel_breaks[index] = True
            if self._optical_history and stamp != self._optical_history[-1][0]:
                self._optical_break = True
        elif not usable:
            self._optical_break = True
        force = self._current_force()
        identity = (force["epoch"], force["frame_id"])
        if force["valid"] and DisplayZero.identifiable(force):
            if self._force_identity and force["epoch"] != self._force_identity[0]:
                self._force_history.clear()
                self._force_identity, self._force_break = None, True
                self._force_seen.clear()
            if identity != self._force_identity:
                if identity in self._force_seen or self._force_history and force["stamp"] <= self._force_history[-1][0]:
                    self._force_break = True
                else:
                    if self._force_history and (self._force_break or force["stamp"] - self._force_history[-1][0] > 250_000_000):
                        self._force_history.append((force["stamp"] - 1, [None] * 6))
                    self._force_history.append((force["stamp"], self.zero.correct(force["values"])))
                    self._force_identity, self._force_break = identity, False
                    self._force_seen.add(identity)
                    if len(self._force_seen) > 4800:
                        self._force_seen = {identity}
        else:
            self._force_break = True

    def _render_histories(self):
        seconds = self.history_window.currentData() or 30
        mode = self.wavelength_mode.currentData()
        history = list(self._optical_history)
        end = history[-1][0] if history else 0
        history = [(stamp, values) for stamp, values in history if stamp >= end - seconds * 1e9]
        self.wavelength_plot.set_labels("相对最新源帧时间 / s", "波长变化 Δλ / pm" if mode == "pm" else "峰心波长 / nm")
        self.wavelength_plot.set_x_range((-seconds, 0))
        self.wavelength_plot.set_series([{"name": identity, "color": COLORS[index],
                                          "x": [(stamp - end) / 1e9 for stamp, _ in history],
                                          "y": [values[index][1 if mode == "pm" else 0] if values[index] else None for _, values in history],
                                          "breaks": [self._optical_starts.get(stamp, [True] * 9)[index] for stamp, _ in history]}
                                         for index, identity in enumerate(IDS)])
        self.wavelength_plot.set_empty_text("等待同一源帧的有效九峰结果；无效与过期数据不会接入曲线")
        self.history_status.setText(("当前光谱已过期；曲线保留历史有效帧。" if self._optical_stale() else
                                     "九路趋势只接入同一原始源帧的有效拟合。")
                                    + " 参考或算法配置改变后清空趋势；无效与过期处断线，不补帧。")
