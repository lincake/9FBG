"""Native Qt pulse observer using the unchanged source-time processing rules."""
from __future__ import annotations

import math
import queue
import time

from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                             QGridLayout, QGroupBox, QHBoxLayout, QLabel, QProgressBar,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .pulse_signal import PulseSession, finite, validate_filter_band_hz
from .qt_widgets import TrendPlot

PHASES = {"idle": "准备录制噪声", "noise": "正在记录 10 s 无负载",
          "calibrated_ready": "曲线已暂停 · 固定权重已保留",
          "monitor": "固定权重曲线持续显示 · 可随时上手",
          "failed": "噪声参考已失效 · 请重新录制无负载"}


def _format(value, digits=2):
    return f"{value:.{digits}f}" if finite(value) else "—"


def _band_text(band):
    return "～".join(f"{value:g}" for value in band) + " Hz"


class _PulseWorker(QThread):
    """All signal processing runs outside the GUI and acquisition threads."""
    ready = pyqtSignal(int, dict)
    failed = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.jobs = queue.Queue()
        self.session = PulseSession()

    def submit(self, generation, action, payload=None):
        self.jobs.put((generation, action, payload))

    def run(self):
        while True:
            generation, action, payload = self.jobs.get()
            if action == "_stop":
                return
            try:
                if action == "push":
                    result = self.session.push(payload)
                elif action == "noise":
                    result = self.session.begin_noise(confirmed=True)
                elif action == "monitor":
                    result = self.session.begin_monitor()
                elif action == "stop":
                    result = self.session.stop_monitor()
                elif action == "band":
                    result = self.session.set_filter_band(payload)
                elif action == "pause":
                    result = self.session.pause(payload)
                elif action == "interrupt":
                    result = self.session.interrupt(payload)
                else:
                    result = self.session.reset(payload) if payload else self.session.reset()
                self.ready.emit(generation, result)
            except Exception as exc:
                self.failed.emit(generation, str(exc))

    def shutdown(self):
        self.submit(-1, "_stop")
        self.wait(5000)


class PulsePage(QWidget):
    """10 s unloaded preparation, fixed weights and continuous native chart."""
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.state = PulseSession().snapshot()
        self._generation = 0
        self._cursor, self._initialized, self._pending = 0, False, None
        self._poll_busy = self._engine_busy = self._reference_busy = False
        self._engine_action = None
        self._request_generation, self._request_anchor = 0, True
        self._available = self._can_initialize = False
        self._last_fresh = self._last_source_stamp = None
        self._service_message = "请先在采集工作台手动连接光谱；首次使用可在本页确认无载并初始化九峰参考。无需连接 PX6D。"
        self._filter_message = ""
        self._closed = False
        self._quiesced = False
        self._build_ui()
        self._worker = _PulseWorker()
        self._worker.ready.connect(self._receive_state)
        self._worker.failed.connect(self._processing_error)
        self._worker.start()
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        ctx.pulse_ready.connect(self._receive_batch)
        ctx.action_finished.connect(self._action_finished)
        ctx.error.connect(self._context_error)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        self._render()

    def _label(self, text=""):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    def _build_ui(self):
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body, layout = QWidget(), QVBoxLayout()
        body.setLayout(layout)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        title = self._label("脉搏测量 · FBG5 中心附近")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        layout.addWidget(self._label("记录 10 s 无负载噪声后自动持续显示合成曲线，可随时放上或移开手。FBG5 至少占 50% 权重；无载噪声不标定机械响应方向或灵敏度。"))
        self.notice = self._label(self._service_message)
        self.notice.setObjectName("pulseServiceNotice")
        layout.addWidget(self.notice)
        flow = QGroupBox("无负载准备与持续观察")
        flow_layout = QGridLayout(flow)
        self._flow_layout = flow_layout
        self._flow_reference_visible = True
        self.unloaded = QCheckBox("已移开手指和其他接触物，明确确认当前无载荷")
        self.unloaded.setObjectName("pulseUnloaded")
        self.unloaded.toggled.connect(self._render)
        flow_layout.addWidget(self.unloaded, 0, 0, 1, 2)
        self.reference_button = QPushButton("首次使用 · 初始化九峰参考")
        self.reference_button.setObjectName("pulseReferenceBtn")
        self.reference_button.clicked.connect(self._initialize_reference)
        self.noise_button = QPushButton("录制无负载 · 有效 10 s")
        self.noise_button.setObjectName("pulseNoiseBtn")
        self.noise_button.clicked.connect(self._begin_noise)
        self.monitor_button = QPushButton("暂停曲线 · 保留权重")
        self.monitor_button.setObjectName("pulseMonitorBtn")
        self.monitor_button.clicked.connect(self._toggle_monitor)
        self.reset_button = QPushButton("重新采集噪声")
        self.reset_button.setObjectName("pulseResetBtn")
        self.reset_button.clicked.connect(self._reset)
        for index, button in enumerate((self.reference_button, self.noise_button, self.monitor_button, self.reset_button)):
            flow_layout.addWidget(button, 1 + index // 2, index % 2)
        flow_layout.addWidget(self._label("短时缺帧不计入有效 10 s，每个新段重新预热；噪声完成后权重只计算一次。离开本页会暂停显示，设备采集继续。"), 3, 0, 1, 2)
        self.freeze_others = QCheckBox("曲线优先刷新，冻结其他界面显示；设备采集继续")
        self.freeze_others.setObjectName("pulseFreezeOthers")
        self.freeze_others.setChecked(True)
        flow_layout.addWidget(self.freeze_others, 4, 0, 1, 2)
        layout.addWidget(flow)
        self.phase_label, self.reason_label, self.timing_label = self._label(), self._label(), self._label()
        self.phase_label.setObjectName("pulsePhase")
        self.reason_label.setObjectName("pulseReason")
        self.timing_label.setObjectName("pulseTiming")
        for label in (self.phase_label, self.reason_label, self.timing_label):
            layout.addWidget(label)
        self.progress = QProgressBar()
        self.progress.setObjectName("pulseProgress")
        self.progress.setRange(0, 1000)
        self.progress.setFormat("%p%")
        layout.addWidget(self.progress)
        metrics = QHBoxLayout()
        self.bpm_label, self.rate_label, self.value_label, self.quality_label = self._label(), self._label(), self._label(), self._label()
        for name, label in (("pulseBpm", self.bpm_label), ("pulseRate", self.rate_label), ("pulseValue", self.value_label), ("pulseQuality", self.quality_label)):
            label.setObjectName(name)
            metrics.addWidget(label)
        layout.addLayout(metrics)
        chart_heading = QHBoxLayout()
        chart_heading.addWidget(self._label("FBG5 中心附近 · 实时合成趋势"), 1)
        chart_heading.addWidget(QLabel("显示窗口"))
        self.window = QComboBox()
        self.window.setObjectName("pulseWindow")
        for seconds in (15, 30, 60):
            self.window.addItem(f"最近 {seconds} s", seconds)
        self.window.setCurrentIndex(1)
        self.window.currentIndexChanged.connect(self._render_chart)
        chart_heading.addWidget(self.window)
        layout.addLayout(chart_heading)
        self.chart = TrendPlot(show_legend=False)
        self.chart.setObjectName("pulseChart")
        self.chart.setMinimumHeight(300)
        self.chart.set_labels("相对源接收时间 / s", "合成等效漂移 / pm")
        layout.addWidget(self.chart)
        band_group = QGroupBox("带通滤波 · 输入后点击应用")
        band_layout = QGridLayout(band_group)
        self.band_low, self.band_high = QDoubleSpinBox(), QDoubleSpinBox()
        for name, spin, value in (("pulseBandLow", self.band_low, 0.7), ("pulseBandHigh", self.band_high, 3.0)):
            spin.setObjectName(name)
            spin.setRange(0.1, 8)
            spin.setDecimals(4)
            spin.setSingleStep(0.1)
            spin.setValue(value)
            spin.valueChanged.connect(self._band_edited)
        band_layout.addWidget(QLabel("下限 / Hz"), 0, 0)
        band_layout.addWidget(self.band_low, 0, 1)
        band_layout.addWidget(QLabel("上限 / Hz"), 0, 2)
        band_layout.addWidget(self.band_high, 0, 3)
        self.apply_button = QPushButton("应用频带 · 需重录噪声")
        self.apply_button.setObjectName("pulseFilterApply")
        self.apply_button.clicked.connect(self._apply_band)
        band_layout.addWidget(self.apply_button, 0, 4)
        self.filter_status, self.filter_help = self._label(), self._label()
        self.filter_status.setObjectName("pulseFilterStatus")
        self.filter_help.setObjectName("pulseFilterHelp")
        band_layout.addWidget(self.filter_status, 1, 0, 1, 5)
        band_layout.addWidget(self.filter_help, 2, 0, 1, 5)
        layout.addWidget(band_group)
        layout.addStretch()

    def _layout_flow_actions(self, reference_visible):
        if self._flow_reference_visible == reference_visible:
            return
        self._flow_reference_visible = reference_visible
        actions = (self.reference_button, self.noise_button, self.monitor_button, self.reset_button)
        for button in actions:
            self._flow_layout.removeWidget(button)
        visible_actions = actions if reference_visible else actions[1:]
        for index, button in enumerate(visible_actions):
            self._flow_layout.addWidget(button, 1 + index // 2, index % 2)

    def _send_session(self, action, payload=None):
        if not self._closed:
            self._engine_busy = True
            self._engine_action = action
            self._worker.submit(self._generation, action, payload)

    def _reanchor(self):
        self._initialized = False
        self._last_fresh = self._last_source_stamp = None

    def _invalidate(self):
        self._generation += 1
        self._engine_busy = False
        self._engine_action = None
        self._reanchor()

    def _suspend(self, reason, transient=False):
        self._invalidate()
        self._pending, self._available, self._can_initialize = None, False, False
        if self.state["phase"] not in ("idle", "failed"):
            if not transient or self.state["phase"] != "calibrated_ready":
                self._send_session("pause" if transient else "interrupt", reason)

    def _tick(self):
        if self._closed:
            self.timer.stop()
            return
        if getattr(self.ctx, "closing", False):
            self._quiesce_for_close()
            return
        if self._last_fresh is not None and time.monotonic() - self._last_fresh > 1:
            self._service_message = "光谱源帧超过 1 秒未更新，保留曲线和权重，等待同一源恢复"
            self._suspend(self._service_message, transient=True)
            self._render()
        self._poll()

    def _poll(self):
        if self._closed or not self.isVisible() or self._poll_busy or self._reference_busy or self._engine_busy or getattr(self.ctx, "closing", False):
            return
        self._request_generation, self._request_anchor = self._generation, not self._initialized
        self._poll_busy = True
        accepted = self.ctx.execute("pulse_samples", {"after": 2**53 - 1 if self._request_anchor else self._cursor, "limit": 4096})
        if accepted is False:
            self._poll_busy = False

    def _receive_batch(self, batch):
        if not self._poll_busy:
            return
        self._poll_busy = False
        if self._reference_busy or self._request_generation != self._generation or not self.isVisible() or self._closed:
            return
        if not isinstance(batch, dict) or batch.get("schema") != "nine_fbg_pulse_samples_v1" or type(batch.get("cursor")) is not int or not 0 <= batch["cursor"] <= 2**53 - 1 or not isinstance(batch.get("samples"), list):
            self._service_message = "脉搏数据格式不匹配，需要连续九峰源帧"
            self._suspend(self._service_message)
            self._render()
            return
        anchor = self._request_anchor
        self._can_initialize = batch.get("can_initialize") is True
        hardware_running = batch.get("running") is True and batch.get("stopping") is not True and batch.get("mode") == "hardware"
        age = batch.get("age_ms")
        timeout = hardware_running and finite(age) and age > 1000 and (batch.get("blocked") is not True or batch.get("reason") == "光谱超过 1 秒未更新或缺少源帧接收时间")
        self._available = batch.get("blocked") is not True and hardware_running and not timeout
        self._service_message = "光谱已连接 · 10 s 无负载后自动持续显示合成曲线，可随时上手观察。FBG5 为中心主路，权重只计算一次。" if self._available else "请先返回采集工作台手动连接光谱；本页可独立初始化九峰参考，无需 PX6D。" if batch.get("running") is False and batch.get("stopping") is not True else batch.get("reason") or "等待有效光谱和九峰参考"
        samples = batch["samples"]
        source_stamp = samples[-1].get("t_ns") if samples and isinstance(samples[-1], dict) else None
        if finite(source_stamp) and source_stamp != self._last_source_stamp and finite(age) and age < 1000:
            self._last_source_stamp, self._last_fresh = source_stamp, time.monotonic() - max(0, age) / 1000
        self._cursor = batch["cursor"]
        binding = self.state.get("binding")
        allowed_reference = self._can_initialize
        if binding is not None and isinstance(batch.get("latest_binding"), str) and batch["latest_binding"] != binding:
            self._service_message = "参考、配置、算法或采集时段已变化，请重新录制无负载噪声"
            self._suspend(self._service_message)
        elif not anchor and (batch.get("truncated") or batch.get("reset_required")):
            transient = not batch.get("reset_required") and hardware_running and (batch.get("blocked") is not True or timeout)
            self._service_message = "脉搏读取缓冲缺帧，曲线和权重保留，自动等待新源帧" if transient else "源数据缓冲已重置，请重新录制无负载"
            self._suspend(self._service_message, transient=transient)
        elif not self._available:
            if timeout:
                self._service_message = "光谱源帧超过 1 秒未更新，保留曲线和权重，等待同一源恢复"
            self._suspend(self._service_message, transient=timeout)
            self._can_initialize = allowed_reference
        elif anchor:
            self._initialized = True
            action, self._pending = self._pending, None
            if action:
                self._send_session(action)
        else:
            self._send_session("push", samples)
        self._render()

    def _receive_state(self, generation, state):
        if generation != self._generation or self._closed:
            return
        self._engine_busy = False
        self._engine_action = None
        self.state = state
        self._render()

    def _processing_error(self, generation, message):
        if generation == self._generation:
            self._engine_busy = False
            self._engine_action = None
            self._service_message = message
            self._render()

    def _context_error(self, message):
        if self._closed or not (self._poll_busy or self._reference_busy):
            return
        self._poll_busy = self._reference_busy = False
        self._service_message = message
        self._suspend(message, transient=True)
        self._render()

    def _action_finished(self, action, result):
        if action not in ("pulse_reference", "pulse/reference") or not self._reference_busy:
            return
        self._reference_busy, self._can_initialize = False, False
        self._service_message = "已明确确认无载，等待九峰稳定初始化；完成后请再次确认无载并采集噪声。"
        self._reanchor()
        self._render()
        self._poll()

    def _begin_noise(self):
        if not self.noise_button.isEnabled() or not self.unloaded.isChecked():
            return
        self._invalidate()
        self._pending = "noise"
        self.unloaded.setChecked(False)
        self._send_session("reset")
        self._render()

    def _initialize_reference(self):
        if not self.reference_button.isEnabled() or not self.unloaded.isChecked():
            return
        self._invalidate()
        self._pending, self._reference_busy = None, True
        self.unloaded.setChecked(False)
        self._send_session("reset", "正在初始化九峰参考，请保持无载")
        self.ctx.execute("pulse_reference", {"unloaded_confirmed": True})
        self._render()

    def _toggle_monitor(self):
        if not self.monitor_button.isEnabled():
            return
        self._invalidate()
        if self.state["phase"] == "calibrated_ready":
            self._pending = "monitor"
        else:
            self._pending = None
            self._send_session("stop")
        self._render()

    def _reset(self):
        if self._reference_busy:
            return
        self._invalidate()
        self._pending = None
        self.unloaded.setChecked(False)
        self._send_session("reset", "固定权重已清除，请重新确认无负载并记录 10 s")
        self._render()

    def _band_edited(self):
        self._filter_message = ""
        self._render()

    def _apply_band(self):
        if not self.apply_button.isEnabled():
            return
        try:
            band = validate_filter_band_hz([self.band_low.value(), self.band_high.value()])
            if band == self.state["filterBandHz"]:
                self._filter_message = "频带未改变，现有曲线和权重继续保留。"
                self._render()
                return
            rate = self.state.get("rateHz")
            if finite(rate) and rate > 0 and band[1] > rate * 0.45 + 1e-9:
                limit = math.floor(min(8, rate * 0.45) * 100 + 1e-9) / 100
                raise ValueError(f"上限过高：当前有效提峰率约 {rate:.1f} Hz，请将上限设为 ≤ {limit:.2f} Hz。")
        except ValueError as exc:
            self._filter_message = str(exc)
            self._render()
            return
        self._invalidate()
        self._pending = None
        self.unloaded.setChecked(False)
        self._filter_message = "已应用 " + _band_text(band) + "，请移开接触物，重新确认无负载并录制噪声。"
        self._send_session("band", band)
        self._render()

    def _render(self, *_):
        state = self.state
        learning, weights = state["phase"] == "noise", state.get("weights")
        # Normal source processing may span a mouse press/release. Disabling
        # widgets for each push cancels real Qt clicks. Only explicit session
        # transitions lock controls; generation + worker ordering still protect
        # reference/noise boundaries when a command arrives during a push.
        command_busy = self._engine_busy and self._engine_action != "push"
        busy = self._reference_busy or self._pending is not None or command_busy
        self.notice.setText(state["reason"] if state["phase"] == "failed" else self._service_message)
        self.phase_label.setText("等待操作后的新源帧" if self._pending else "等待源帧恢复 · 曲线与权重保留" if state["suspended"] else PHASES.get(state["phase"], state["phase"]))
        self.reason_label.setText(state["reason"])
        self.progress.setValue(round(state["progress"] * 1000))
        self.bpm_label.setText("候选周期频率\n" + _format(state["candidateBpm"], 0) + " 次/分")
        self.rate_label.setText("实际有效提峰率\n" + _format(state["rateHz"], 1) + " Hz")
        self.value_label.setText("合成等效漂移\n" + _format(state["valuePm"], 3) + " pm")
        quality = "已暂停" if state["phase"] == "calibrated_ready" else "等待源帧" if state["suspended"] else "周期稳定" if state["phase"] == "monitor" and state["quality"]["valid"] else "需重采噪声" if state["phase"] == "failed" else "观察中" if state["phase"] == "monitor" else "采集中" if learning else "等待"
        self.quality_label.setText("信号状态\n" + quality)
        self.quality_label.setToolTip(state["quality"]["label"])
        reference_visible = self._can_initialize or self._reference_busy
        self.reference_button.setVisible(reference_visible)
        self._layout_flow_actions(reference_visible)
        self.reference_button.setEnabled(self._can_initialize and not busy and self.unloaded.isChecked())
        self.noise_button.setEnabled(self._available and not busy and not learning and weights is None and self.unloaded.isChecked())
        self.unloaded.setEnabled(not learning and not busy and weights is None)
        self.monitor_button.setEnabled(not busy and weights is not None and (state["phase"] != "calibrated_ready" or self._available))
        self.monitor_button.setText("继续显示曲线" if state["phase"] == "calibrated_ready" else "暂停曲线 · 保留权重")
        self.reset_button.setEnabled(not self._reference_busy)
        timing = []
        if learning:
            timing.append(f"有效无载 {state['effectiveNoiseSeconds']:.1f}/10 s · 预热后 {state['noiseFilteredSeconds']:.1f}/7 s · 原始有效样本 {state['noiseRawSamples']}/181 · 带内样本 {state['noiseFilteredSamples']}/126")
        if state["gapCount"]:
            timing.append(f"已分段 {state['gapCount']} 次 · 最大间隔 {state['maxGapMs']:.0f} ms")
        self.timing_label.setText(" · ".join(timing))
        band = state["filterBandHz"]
        dirty = [self.band_low.value(), self.band_high.value()] != band
        for control in (self.band_low, self.band_high, self.apply_button):
            control.setEnabled(not learning and not busy)
        self.filter_status.setText(self._filter_message or ("编辑尚未应用，曲线仍使用 " + _band_text(band) + "；应用更改会清除噪声权重，需重新录制无负载。" if dirty else "当前频带 " + _band_text(band) + "。输入后点击应用；更改频带需重新录制无负载噪声。"))
        rate = state["rateHz"]
        rate_help = f"当前有效提峰率下，上限须 ≤ {math.floor(min(8, rate * .45) * 100 + 1e-9) / 100:.2f} Hz。" if finite(rate) and rate > 0 else "上限还受实际有效提峰率限制。"
        candidate = state["candidateBandHz"]
        self.filter_help.setText(f"范围 0.1～8 Hz，下限须小于上限。{rate_help}当前预热约 {state['warmupSeconds']:.1f} s，仍需补足预热后 7 s 噪声。" + ("周期判断范围 " + _band_text(candidate) + "。" if candidate else "当前频带未覆盖 0.7～3 Hz，仅显示曲线。"))
        self._render_chart()

    def _render_chart(self, *_):
        points = self.state.get("points", [])
        if not points:
            self.chart.set_series([])
            return
        end, window = points[-1]["t"], self.window.currentData()
        start = end - window
        selected = [point for point in points if point["t"] >= start]
        series, segment_x, segment_y, warmup = [], [], [], None
        def flush():
            if segment_x:
                series.append(dict(name="预热段" if warmup else "合成曲线", x=segment_x[:], y=segment_y[:], color="#a5c8c2" if warmup else "#087f72"))
                segment_x.clear()
                segment_y.clear()
        for point in selected:
            next_warmup = point.get("warmup", False)
            if point.get("breakBefore") or (warmup is not None and next_warmup != warmup):
                flush()
            warmup = next_warmup
            segment_x.append(point["t"] - end)
            segment_y.append(point["value"])
        flush()
        self.chart.set_series(series)
        self.chart.set_x_range([-window, 0])

    def update_state(self, state):
        # Service snapshots do not supply or manufacture pulse source samples.
        if getattr(self.ctx, "closing", False):
            self._quiesce_for_close()
        elif self._quiesced and not self._closed:
            self._quiesced = False
            self._reanchor()
            if self.isVisible():
                self.timer.start()
                self._poll()

    def _quiesce_for_close(self):
        if self._closed or self._quiesced:
            return
        self._quiesced = True
        self.timer.stop()
        # The context may discard an enqueued read/reference during shutdown;
        # it then has no completion signal. Do not retain a permanent busy flag
        # if controlled shutdown is rejected and the operator resumes the UI.
        self._poll_busy = self._reference_busy = False
        self._invalidate()
        self._pending, self._available, self._can_initialize = None, False, False
        if self.state["phase"] not in ("idle", "failed", "calibrated_ready"):
            self._send_session("pause", "正在受控关闭设备，暂时暂停观察")
        self._render()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._closed:
            self._reanchor()
            self.timer.start()
            self._poll()

    def hideEvent(self, event):
        self.timer.stop()
        if not self._closed:
            self._invalidate()
            self._pending, self._available = None, False
            if self.state.get("weights") and self.state["phase"] == "monitor":
                self._send_session("pause", "页面暂时隐藏，返回后自动继续观察")
            elif self.state.get("weights") is None:
                self._send_session("interrupt", "页面暂时隐藏，本次无载采集未完成，请重新录制")
        super().hideEvent(event)

    def shutdown(self):
        if not self._closed:
            self._closed = True
            self.timer.stop()
            self._worker.shutdown()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
