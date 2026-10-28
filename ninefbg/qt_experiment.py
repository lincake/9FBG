"""Native experiment, dataset, training and inference workspaces.

All mutations go through the application's queued service context.  Widgets
never own a device, reinterpret a reference, or supply a target to inference.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView, QBoxLayout, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from .qt_widgets import TrendPlot
from .capture_setup import SETUP_POLICY, setup_allows_trial


POINT_IDS = tuple(f"P{i:02d}" for i in range(1, 26))
NO_CONTACT = "NO_CONTACT"
COORDINATE_SCHEMA = "bottom_left_xy_v1"
SEQUENCE_METHODS = {"spectral_gru", "drift_gru"}
WRENCH_PROTOCOLS = {"timed_wrench_v1", "force_target_wrench_v1"}
ACTIVE_PHASES = {"waiting_training", "waiting_contact", "preload", "no_contact",
                 "loading", "release", "hold", "removed", "recovery", "saving"}
ACTIVE_STEPS = ACTIVE_PHASES | {"unloading", "checking", "zeroing"}
METHOD_LABELS = {"spectral_gru": "全谱 MLP＋GRU（六维力）", "drift_gru": "寻峰漂移 MLP＋GRU（六维力）",
                 "linear": "加权最小二乘", "mlp": "小型 MLP"}
PHASE_LABELS = {"waiting_training": "等待训练暂停确认", "waiting_contact": "连续记录 · 等待接触",
                "preload": "连续记录 · 2 秒无载基线", "no_contact": "连续记录 · 无接触",
                "loading": "加载", "release": "已达目标 · 请卸载，仍在记录", "hold": "保持",
                "removed": "已移开", "recovery": "恢复观察", "saving": "等待保存确认",
                "idle": "等待试次", "unloaded": "无载基线", "finish": "已完成",
                "aborted": "已中止", "stopped": "会话已停止"}
AXES = ("Fx", "Fy", "Fz", "Mx", "My", "Mz")
UNITS = ("N", "N", "N", "N·m", "N·m", "N·m")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fmt(value, digits=3):
    return f"{value:.{digits}f}" if finite(value) else "—"


def local_time(value):
    if not value:
        return "未记录"
    try:
        from datetime import timezone, timedelta
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, AttributeError):
        return str(value)


def point_layout(settings=None, has_session=False):
    """Keep historical IDs and geometry; only known current geometry flips y."""
    settings = settings or {}
    current = (settings.get("coordinate_definition") or {}).get("schema") == COORDINATE_SCHEMA
    stored = settings.get("point_grid")
    if isinstance(stored, dict):
        stored = stored.get("points")
    valid = (isinstance(stored, list) and len(stored) == 25
             and all(isinstance(p, dict) for p in stored)
             and {p.get("id") for p in stored} == set(POINT_IDS)
             and all(type(p.get("row")) is int and type(p.get("col")) is int
                     and finite(p.get("u", p.get("x_norm")))
                     and finite(p.get("v", p.get("y_norm"))) for p in stored))
    if valid:
        points = deepcopy(stored)
        if current:
            points.sort(key=lambda p: (-p["row"], p["col"]))
        return points, current, True
    if has_session and (not current or stored is not None):
        return [{"id": p} for p in POINT_IDS], False, False
    points = [{"id": p, "row": i // 5, "col": i % 5, "u": i % 5 / 4, "v": i // 5 / 4}
              for i, p in enumerate(POINT_IDS)]
    return sorted(points, key=lambda p: (-p["row"], p["col"])), True, True


def training_status(session, method):
    """A visible explanation accompanies every unavailable dataset."""
    if not session:
        return False, "记录已变化，请刷新后重选"
    if session.get("deleted"):
        return False, "回收区记录须先恢复"
    settings = session.get("settings") or {}
    batch = settings.get("batch", session.get("batch"))
    if batch == "C":
        return False, "C 批保留数据不参与训练"
    if batch not in {"A", "B"}:
        return False, "批次未核实"
    if session.get("diagnostic"):
        return False, "诊断记录不参与训练"
    if session.get("trainable") is False:
        return False, session.get("history_error") or "记录未通过训练核验"
    if session.get("complete") is not True:
        return False, "会话尚未完整保存"
    accepted = session.get("accepted_trial_count", 0)
    no_contact = session.get("accepted_no_contact_trial_count", 0)
    if session.get("has_training_data") is not True or not (accepted or no_contact):
        return False, "没有合格采集数据"
    if method not in SEQUENCE_METHODS and no_contact and accepted <= no_contact:
        return False, "无接触标签须使用时序模型"
    if method == "spectral_gru" and (session.get("spectral_available") is False
            or session.get("capture_mode", settings.get("capture_mode")) == "peak_drift"):
        return False, session.get("spectral_unavailable_reason") or "九路漂移会话不含 512 点原谱"
    if method == "drift_gru" and session.get("drift_available") is False:
        return False, session.get("drift_unavailable_reason") or session.get("drift_error") or "缺少可信九路峰心或 2 秒基线"
    if method not in SEQUENCE_METHODS and settings.get("acquisition_protocol", session.get("acquisition_protocol")) in WRENCH_PROTOCOLS:
        return False, "六维协议须使用全谱或寻峰漂移时序模型"
    return True, "可训练"


def capture_gates(state, pending_point, busy=False):
    experiment = state.get("experiment") or {}
    analysis = state.get("analysis") or {}
    guided = experiment.get("guided") or {}
    settings = experiment.get("settings") or {}
    phase = experiment.get("phase", "idle")
    protocol = settings.get("acquisition_protocol", settings.get("protocol", guided.get("protocol")))
    running = experiment.get("running") is True
    active = running and bool(experiment.get("trial_id") or phase in ACTIVE_PHASES or guided.get("step") in ACTIVE_STEPS)
    baseline = experiment.get("baseline") or {}
    baseline_ready = baseline.get("ready") is True or baseline.get("status") == "ready"
    training = analysis.get("training") or {}
    train_busy = training.get("busy") is True or training.get("status") in {"running", "pause_requested", "paused", "cancelling"}
    same = pending_point == experiment.get("point_id")
    selected = bool(experiment.get("point_id")) and (experiment.get("point_id") != NO_CONTACT or protocol == "force_target_wrench_v1")
    setup_ready = setup_allows_trial(settings, requires_force_direction=protocol not in WRENCH_PROTOCOLS)
    return {
        "start": not busy and not running and state.get("running") is True and state.get("mode") == "hardware"
                 and state.get("force_connected") is True and (not analysis.get("busy") and not train_busy or train_busy and training.get("method") in SEQUENCE_METHODS),
        "stop": not busy and running,
        "point": not busy and not active,
        "point_confirm": not busy and running and guided.get("enabled") is True and not active and (pending_point != NO_CONTACT or protocol == "force_target_wrench_v1"),
        "baseline": not busy and running and guided.get("enabled") is True and not active and same and selected and guided.get("step") in {"needs_baseline", "retry", "round_complete"},
        "trial_start": not busy and running and guided.get("enabled") is True and not active and same and selected and baseline_ready and setup_ready and (guided.get("step") == "ready" or protocol == "force_target_wrench_v1" and guided.get("step") in {"round_complete", "retry"}),
        "trial_finish": not busy and running and guided.get("enabled") is True and bool(experiment.get("trial_id")) and protocol == "force_target_wrench_v1" and guided.get("can_finish") is True and (phase == "no_contact" and guided.get("step") == "no_contact" if experiment.get("point_id") == NO_CONTACT else phase == "release" and guided.get("step") == "release"),
        "trial_abort": not busy and active,
    }


def capture_point_states(state, unavailable=False):
    experiment = state.get("experiment") or {}
    guided = experiment.get("guided") or {}
    counts = guided.get("accepted_by_point") or {}
    states = {point: "success" for point in POINT_IDS if finite(counts.get(point)) and counts[point] > 0}
    confirmed = experiment.get("point_id")
    if experiment.get("running") is not True or confirmed not in POINT_IDS:
        return states
    phase, step = experiment.get("phase"), guided.get("step")
    if (experiment.get("trial_id") or confirmed not in states
            or step not in {"round_complete", "point_complete", "session_complete"}):
        states[confirmed] = "confirmed"
    if (not unavailable and experiment.get("trial_id")
            and phase in {"waiting_contact", "loading"} and step == phase
            and guided.get("protocol") == "force_target_wrench_v1"
            and guided.get("press_prompt_ready") is True
            and not guided.get("quality_faults") and not experiment.get("error")):
        states[confirmed] = "press_ready"
    return states


class PointGrid(QWidget):
    """25 native buttons; selection is a human label, never an inference input."""
    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self.callback = callback
        self.buttons = {}
        self.layout_key = None
        self.grid = QGridLayout(self)
        self.grid.setSpacing(5)

    def render(self, settings=None, has_session=False, selected=None, recognized=None, counts=None, enabled=True, trained=None, capture_states=None):
        points, current, known = point_layout(settings, has_session)
        key = tuple(p["id"] for p in points)
        if self.layout_key != key:
            while self.grid.count():
                item = self.grid.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.buttons = {}
            for index, point in enumerate(points):
                pid = point["id"]
                button = QPushButton(pid)
                button.setMinimumSize(55, 42)
                button.setCheckable(True)
                button.setProperty("role", "point")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda _checked=False, p=pid: self.callback(p))
                self.buttons[pid] = button
                self.grid.addWidget(button, index // 5, index % 5)
            self.layout_key = key
        for point in points:
            pid = point["id"]
            button = self.buttons[pid]
            count = (counts or {}).get(pid)
            button.setText(pid + (f"\n合格 {count}" if count is not None else ""))
            button.setChecked(pid == selected)
            button.setEnabled(enabled)
            reason = "历史方向未记录" if not known else f"行 {point['row']} / 列 {point['col']}；x/u={fmt(point.get('u', point.get('x_norm')), 2)}，y/v={fmt(point.get('v', point.get('y_norm')), 2)}"
            if trained is not None and pid not in trained:
                reason += "；未训练点"
            if capture_states is not None:
                capture_state = capture_states.get(pid, "")
                button.setProperty("captureState", capture_state)
                background, color, edge, description = {
                    "success": ("#e7f6ec", "#256340", "#b6ddc4", "已有采集成功并保存的合格轮次"),
                    "confirmed": ("#e5efff", "#2455be", "#a9c7f2", "已确认采集点位"),
                    "press_ready": ("#127c4a", "#ffffff", "#127c4a", "真实 2 秒无载基线已完成，可以开始按压"),
                }.get(capture_state, ("#ffffff", "#3167e3", "#d9e2ef", "尚未确认采集"))
                border = "2px solid #3167e3" if pid == selected else "1px solid " + edge
                style = ("QPushButton, QPushButton:disabled, QPushButton:checked {"
                         f"border: {border}; border-radius: 8px; background:{background}; color:{color};" + "}")
                reason += "；" + description
                button.setAccessibleDescription(description)
            else:
                background = "#127c4a" if pid == recognized else "#edf3ff" if pid == selected else ""
                color = "white" if pid == recognized else "#3167e3"
                border = "2px solid #3167e3" if pid == selected else "1px solid #d9e2ef"
                style = f"QPushButton {{border: {border}; border-radius: 8px;" + (f"background:{background}; color:{color};" if background else "") + "}"
            button.setToolTip(reason)
            if button.styleSheet() != style:
                button.setStyleSheet(style)
        return current, known


class ExperimentWorkspace(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.state = {}
        self._received_at = 0.0
        self._pending = set()
        self._rendering = False
        self.sessions, self.deleted_sessions = [], []
        self.models, self.deleted_models = [], []
        self.selected_sessions, self.selected_models = set(), set()
        self.pending_point = "P01"
        self.target_point = None
        self.frozen_sample = None
        self._list_loaded = False
        self._models_loaded = False
        self._last_training_model = None
        self._force_series = []
        self._last_force_frame = None
        self._display_stale = True
        self._preference_updates = {}
        self._build()
        self._load_preferences()
        self._bind_preferences()
        ctx.action_finished.connect(self._action_finished)
        ctx.error.connect(self._error)
        self._fresh_timer = QTimer(self)
        self._fresh_timer.setInterval(250)
        self._fresh_timer.timeout.connect(self._refresh_controls)
        self._fresh_timer.start()

    def _label(self, text=""):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _button(self, text, callback, row=None):
        button = QPushButton(text)
        button.clicked.connect(callback)
        if row is not None:
            row.addWidget(button)
        return button

    def _combo(self, options):
        widget = QComboBox()
        for text, value in options:
            widget.addItem(text, value)
        return widget

    def _spin(self, value, low, high, integer=False, decimals=4):
        widget = QSpinBox() if integer else QDoubleSpinBox()
        widget.setRange(low, high)
        if not integer:
            widget.setDecimals(decimals)
            widget.setSingleStep(.1)
        widget.setValue(value)
        return widget

    def _scroll_tab(self, title):
        content = QWidget()
        layout = QVBoxLayout(content)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(content)
        self.tabs.addTab(area, title)
        return layout

    def _build(self):
        outer = QVBoxLayout(self)
        self.message = self._label("先连接真实光谱设备与六轴设备，再开始采集。启动不会自动连接设备。")
        outer.addWidget(self.message)
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs)
        self._build_capture(self._scroll_tab("点位采集"))
        self._build_training(self._scroll_tab("数据与训练"))
        self._build_inference(self._scroll_tab("模型推理"))
        self._build_model_management(self._scroll_tab("模型管理"))
        self._refresh_controls()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_capture_layout()

    def _update_capture_layout(self):
        columns = getattr(self, "_capture_columns", None)
        if columns is None:
            return
        direction = (QBoxLayout.Direction.TopToBottom if self.width() < 1000
                     else QBoxLayout.Direction.LeftToRight)
        if columns.direction() != direction:
            columns.setDirection(direction)

    def _build_capture(self, layout):
        setup = QGroupBox()
        setup.setObjectName("captureSetupCard")
        setup_layout = QVBoxLayout(setup)
        self.capture_setup_toggle = self._button("", self._toggle_capture_setup)
        self.capture_setup_toggle.setObjectName("captureSetupToggle")
        self.capture_setup_toggle.setCheckable(True)
        self.capture_setup_toggle.setChecked(True)
        self.capture_setup_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        setup_layout.addWidget(self.capture_setup_toggle)
        self.capture_setup_content = QWidget()
        form = QFormLayout(self.capture_setup_content)
        form.setContentsMargins(0, 0, 0, 0)
        setup_layout.addWidget(self.capture_setup_content)
        self.finger_id = QLineEdit()
        self.finger_id.setMaxLength(256)
        self.finger_id.setPlaceholderText("输入实际样件标识")
        self.capture_mode = self._combo([("原始全谱（512 点）", "full_spectrum"), ("寻峰漂移（FBG1～FBG9）", "peak_drift")])
        self.protocol = self._combo([("合力目标＋手动结束", "force_target_wrench_v1"), ("原有法向引导采集", "guided_round_v1")])
        self.target_force = self._spin(4, .01, 100)
        self.max_force = self._spin(5, .01, 100)
        self.target_force.setToolTip("目标必须低于合力上限；达到目标后卸载，通过本轮数据检查后才能结束。")
        self.max_force.setToolTip("独立的质量上限，须高于目标；超过上限会使本轮不合格。请按实验允许范围明确设置。")
        self.repetitions = self._spin(3, 1, 999, True)
        self.force_sign = self._combo([("−Fz 为按压力", -1), ("+Fz 为按压力", 1)])
        for title, widget in [("手指 / 样件标识", self.finger_id), ("采集模式", self.capture_mode), ("采集协议", self.protocol), ("三维合力目标 / N", self.target_force), ("三维合力上限 / N", self.max_force), ("每点合格重复次数", self.repetitions), ("法向按压力方向", self.force_sign)]:
            form.addRow(title, widget)
        self.capture_note = self._label()
        form.addRow(self.capture_note)
        row = QHBoxLayout()
        self.start_button = self._button("建立采集会话", self._start_capture, row)
        self.stop_button = self._button("停止并保存会话", lambda: self._send("experiment/stop", {}), row)
        setup_layout.addLayout(row)
        self._toggle_capture_setup()
        layout.addWidget(setup)
        for widget in [self.capture_mode, self.protocol, self.force_sign]:
            widget.currentIndexChanged.connect(self._capture_setting_changed)
        columns = QHBoxLayout()
        self._capture_columns = columns
        self._update_capture_layout()
        points = QGroupBox("2 · 选择并确认采集点位")
        point_layout_box = QVBoxLayout(points)
        self.capture_axes = self._label("+x / +u → 右；+y / +v ↑ 指尖")
        point_layout_box.addWidget(self.capture_axes)
        self.capture_grid = PointGrid(self._select_capture_point)
        point_layout_box.addWidget(self.capture_grid)
        self.no_contact_button = self._button("无接触 · 已采集 0 轮", lambda: self._select_capture_point(NO_CONTACT))
        self.no_contact_button.setCheckable(True)
        point_layout_box.addWidget(self.no_contact_button)
        self.point_status = self._label("预选 P01；尚未确认")
        point_layout_box.addWidget(self.point_status)
        self.confirm_point_button = self._button("确认采集点位", self._confirm_point)
        point_layout_box.addWidget(self.confirm_point_button)
        self.capture_grid_hint = self._label("网格只预选，须点击确认；活动轮次不能切换。无接触次数独立累计，不计入 25 点完成进度。")
        point_layout_box.addWidget(self.capture_grid_hint)
        self.session_status = self._label()
        point_layout_box.addWidget(self.session_status)
        point_layout_box.addStretch()
        columns.addWidget(points, 2)
        round_box = QGroupBox("3 · 本轮标定与采集")
        round_layout = QVBoxLayout(round_box)
        self.guidance = self._label("待开始：先建立会话并确认实际采集点位。")
        self.guidance.setStyleSheet("font-size:11pt; font-weight:500; padding:6px;")
        round_layout.addWidget(self.guidance)
        self.preload_status = self._label()
        round_layout.addWidget(self.preload_status)
        self.unloaded_confirmed = QCheckBox("已移开接触棒，明确确认当前无接触；建立本轮光学参考与六轴软件零偏")
        self.unloaded_confirmed.toggled.connect(self._refresh_controls)
        round_layout.addWidget(self.unloaded_confirmed)
        row = QGridLayout()
        self.baseline_button = self._button("标定本轮", self._baseline)
        self.trial_start_button = self._button("开始本轮采集", lambda: self._trial_action("start"))
        self.trial_finish_button = self._button("结束本轮（确认已卸载）", self._finish_trial)
        self.trial_abort_button = self._button("中止本轮", lambda: self._trial_action("abort"))
        for index, button in enumerate((self.baseline_button, self.trial_start_button,
                                        self.trial_finish_button, self.trial_abort_button)):
            row.addWidget(button, index // 2, index % 2)
        round_layout.addLayout(row)
        self.baseline_status = self._label("本轮无载参考尚未就绪")
        round_layout.addWidget(self.baseline_status)
        self.progress_bars = {}
        for key, title in [("total", "25 点总采集进度"), ("point", "当前点合格轮次"), ("force", "本轮合力目标进度")]:
            label = self._label(title)
            bar = QProgressBar()
            bar.setRange(0, 100)
            round_layout.addWidget(label)
            round_layout.addWidget(bar)
            self.progress_bars[key] = (label, bar)
        self.live_force = self._label("PX6D 当前合力：— N")
        self.live_force.setStyleSheet("font-size:18pt; font-weight:600;")
        round_layout.addWidget(self.live_force)
        self.force_detail = self._label()
        round_layout.addWidget(self.force_detail)
        self.force_plot = TrendPlot()
        self.force_plot.setMinimumHeight(180)
        self.force_plot.set_labels("本页观察时间 / s", "PX6D 合力 / N")
        round_layout.addWidget(self.force_plot)
        self.force_plot.hide()
        self.quality_status = self._label("合格次数只由服务端真实质量检查计入。")
        self.record_status = self._label()
        round_layout.addWidget(self.quality_status)
        round_layout.addWidget(self.record_status)
        for label in (self.capture_axes, self.point_status, self.capture_grid_hint,
                      self.session_status, self.baseline_status, self.live_force,
                      self.force_detail, self.quality_status, self.record_status):
            label.hide()
        columns.addWidget(round_box, 3)
        layout.addLayout(columns)
        layout.addStretch()

    def _toggle_capture_setup(self):
        expanded = self.capture_setup_toggle.isChecked()
        self.capture_setup_content.setVisible(expanded)
        self.capture_setup_toggle.setText(("▾" if expanded else "▸") + " 1 · 会话设置")
        self.capture_setup_toggle.setToolTip("点击收起会话设置" if expanded else "点击展开会话设置")
        self.capture_setup_toggle.setAccessibleName("会话设置，已展开" if expanded else "会话设置，已收起")

    def _build_training(self, layout):
        dataset = QGroupBox("采集数据与历史记录")
        box = QVBoxLayout(dataset)
        row = QHBoxLayout()
        self.show_deleted_sessions = QCheckBox("查看已删除")
        self.show_deleted_sessions.toggled.connect(self._history_view_changed)
        row.addWidget(self.show_deleted_sessions)
        self.refresh_sessions_button = self._button("刷新会话", lambda: self._send("history/sessions"), row)
        self.select_trainable_button = self._button("全选可训练数据", self._select_trainable, row)
        self.clear_sessions_button = self._button("清空选择", self._clear_sessions, row)
        row.addStretch()
        box.addLayout(row)
        row = QHBoxLayout()
        self.open_session_button = self._button("打开选中保存位置", self._open_session, row)
        self.delete_sessions_button = self._button("移入回收区", lambda: self._change_history(False), row)
        self.restore_sessions_button = self._button("恢复所选", lambda: self._change_history(True), row)
        row.addStretch()
        box.addLayout(row)
        self.cumulative = QCheckBox("时序训练累计已保存 A / B 来源；手动勾选可改用子集")
        self.cumulative.setChecked(True)
        self.cumulative.toggled.connect(self._cumulative_changed)
        box.addWidget(self.cumulative)
        self.session_table = self._table(["选择", "数据编号 / 会话", "开始（北京时间）", "样件 / 批次", "采集模式", "合格 / 总轮次", "训练状态"])
        self.session_table.setMinimumHeight(230)
        self.session_table.itemChanged.connect(self._session_item_changed)
        box.addWidget(self.session_table)
        self.selection_status = self._label("已选择 0 条记录")
        box.addWidget(self.selection_status)
        layout.addWidget(dataset)
        training_box = QGroupBox("模型训练")
        box = QVBoxLayout(training_box)
        form = QFormLayout()
        self.training_method = self._combo([(label, key) for key, label in METHOD_LABELS.items()])
        self.training_mode = self._combo([("从头训练", "scratch"), ("继承模型继续训练", "finetune")])
        self.base_model = QComboBox()
        self.base_model.addItem("请选择同输入基础模型", "")
        self.training_method.currentIndexChanged.connect(self._training_method_changed)
        self.training_mode.currentIndexChanged.connect(self._training_mode_changed)
        self.base_model.currentIndexChanged.connect(self._refresh_controls)
        form.addRow("模型方法", self.training_method)
        form.addRow("训练方式", self.training_mode)
        form.addRow("同输入基础模型（仅作训练起点）", self.base_model)
        box.addLayout(form)
        self.method_note = self._label()
        box.addWidget(self.method_note)
        self.training_parameters = QWidget()
        grid = QGridLayout(self.training_parameters)
        self.parameter_widgets = {}
        specs = [("epochs", "迭代上限", 100, 1, 1000, True), ("batch_size", "批大小", 32, 1, 1024, True),
                 ("learning_rate", "学习率", .001, .000001, .1, False), ("patience", "早停耐心", 10, 1, 1000, True),
                 ("seed", "随机种子", 1701, 0, 2147483647, True), ("cpu_threads", "CPU 计算线程", 2, 1, 8, True),
                 ("position_loss_weight", "位置损失权重", 1, 0, 100, False), ("wrench_loss_weight", "六维力损失权重", 1, 0, 100, False)]
        for i, (key, text, default, low, high, integer) in enumerate(specs):
            widget = self._spin(default, low, high, integer, 6 if key == "learning_rate" else 4)
            self.parameter_widgets[key] = widget
            grid.addWidget(QLabel(text), i // 4 * 2, i % 4)
            grid.addWidget(widget, i // 4 * 2 + 1, i % 4)
        box.addWidget(self.training_parameters)
        self.adapt_legacy = QCheckBox("显式适配有可信原始无载谱的旧会话（记录派生 S0 来源）")
        box.addWidget(self.adapt_legacy)
        box.addWidget(self._label("仅使用完整结束的真实会话。A / B 按完整轮次固定划分训练与验证；C 不参与拟合或早停。继续训练只勾选新增会话，服务端带入基础模型历史来源并去重，候选模型另存。按压前须等待训练保护暂停确认。"))
        row = QHBoxLayout()
        self.training_start_button = self._button("批量训练所选数据", self._start_training, row)
        self.training_pause_button = self._button("暂停训练", lambda: self._send("training/pause", {}), row)
        self.training_resume_button = self._button("继续训练", lambda: self._send("training/resume", {}), row)
        self.training_cancel_button = self._button("取消训练", lambda: self._send("training/cancel", {}), row)
        self.go_inference_button = self._button("去模型推理", self._go_inference, row)
        box.addLayout(row)
        self.training_progress = QProgressBar()
        box.addWidget(self.training_progress)
        self.training_status_label = self._label("尚未开始训练")
        box.addWidget(self.training_status_label)
        self.training_result = QPlainTextEdit()
        self.training_result.setReadOnly(True)
        self.training_result.setMinimumHeight(140)
        self.training_result.setPlaceholderText("训练完成后显示已保存模型、整轮留出指标与继续训练对照。")
        box.addWidget(self.training_result)
        self.loss_plot = TrendPlot()
        self.loss_plot.setMinimumHeight(160)
        self.loss_plot.set_labels("真实迭代 / 次", "损失")
        box.addWidget(self.loss_plot)
        layout.addWidget(training_box)
        layout.addStretch()

    def _table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _build_inference(self, layout):
        preparation = QGroupBox("1 · 准备模型推理")
        box = QVBoxLayout(preparation)
        row = QHBoxLayout()
        self.model_select = QComboBox()
        self.model_select.addItem("请选择已保存模型", "")
        self.model_select.currentIndexChanged.connect(self._refresh_controls)
        row.addWidget(self.model_select, 1)
        self.refresh_models_button = self._button("刷新模型", lambda: self._send("analysis/models"), row)
        self.load_model_button = self._button("载入模型", self._load_model, row)
        self.inference_stop_button = self._button("结束推理", self._stop_inference, row)
        box.addLayout(row)
        self.test_unloaded = QCheckBox("已移开接触棒，明确确认当前无接触")
        self.test_unloaded.toggled.connect(self._refresh_controls)
        box.addWidget(self.test_unloaded)
        self.reference_button = self._button("建立本次无接触参考", self._inference_reference)
        box.addWidget(self.reference_button)
        self.inference_status = self._label("尚未载入模型；载入后重新确认无接触并建立本次参考。")
        box.addWidget(self.inference_status)
        self.model_definition = self._label()
        box.addWidget(self.model_definition)
        layout.addWidget(preparation)
        columns = QHBoxLayout()
        target = QGroupBox("2 · 选择实际按压目标并观察识别")
        box = QVBoxLayout(target)
        self.validation_axes = self._label("待载入模型核对点阵定义；编号排列不声明历史空间方向。")
        box.addWidget(self.validation_axes)
        self.validation_grid = PointGrid(self._select_target)
        box.addWidget(self.validation_grid)
        self.prediction_status = self._label("尚无有效推理；目标不输入模型。")
        box.addWidget(self.prediction_status)
        self.prediction_reason = self._label()
        box.addWidget(self.prediction_reason)
        self.wrench_table = self._table(["轴 / 单位", "模型", "PX6D 对照", "模型 − 对照"])
        self.wrench_table.setMinimumHeight(135)
        self.wrench_table.setMaximumHeight(165)
        box.addWidget(self.wrench_table)
        columns.addWidget(target, 1)
        judgment = QGroupBox("3 · 冻结读数后人工判断（可选保存统计）")
        box = QVBoxLayout(judgment)
        self.validation_mode = self._combo([("实时观察", "observe"), ("人工六轴验证", "judge")])
        self.validation_mode.currentIndexChanged.connect(self._refresh_controls)
        box.addWidget(self.validation_mode)
        tolerance_form = QGridLayout()
        self.tolerances = []
        for i, (axis, unit) in enumerate(zip(AXES, UNITS)):
            entry = QLineEdit()
            entry.setPlaceholderText("未配置")
            entry.textChanged.connect(self._refresh_controls)
            self.tolerances.append(entry)
            tolerance_form.addWidget(QLabel(f"{axis} 容差 / {unit}"), i // 3 * 2, i % 3)
            tolerance_form.addWidget(entry, i // 3 * 2 + 1, i % 3)
        box.addLayout(tolerance_form)
        self.freeze_button = self._button("冻结本次读数", self._freeze)
        box.addWidget(self.freeze_button)
        self.frozen_status = self._label("尚未冻结；先选择目标并实际按压。")
        box.addWidget(self.frozen_status)
        self.position_correct = QCheckBox("位置识别正确")
        self.force_correct = QCheckBox("力正确：绝对差值 ≤ 0.3 N")
        box.addWidget(self.position_correct)
        box.addWidget(self.force_correct)
        row = QHBoxLayout()
        self.save_judgment_button = self._button("保存本次判断", self._judge, row)
        self.discard_judgment_button = self._button("重新判断", self._discard, row)
        box.addLayout(row)
        box.addWidget(self._label("冻结后读数与目标固定。保存时未勾选即表示不正确，两项均不勾也可保存失败按压。重新判断放弃未保存判断，不计入分母。逐轴容差只用于人工核验。"))
        self.validation_stats = self._label("尚无已保存判断")
        box.addWidget(self.validation_stats)
        self.finish_validation_button = self._button("保存本轮判断报告", lambda: self._send("validation/finish", {}))
        box.addWidget(self.finish_validation_button)
        self.validation_report = self._label()
        box.addWidget(self.validation_report)
        box.addStretch()
        columns.addWidget(judgment, 1)
        layout.addLayout(columns)
        layout.addStretch()

    def _build_model_management(self, layout):
        management = QGroupBox("已保存模型管理（训练数据与人工报告保留）")
        box = QVBoxLayout(management)
        row = QHBoxLayout()
        self.show_deleted_models = QCheckBox("查看已删除")
        self.show_deleted_models.toggled.connect(self._model_view_changed)
        row.addWidget(self.show_deleted_models)
        self.refresh_managed_models_button = self._button("刷新模型", lambda: self._send("analysis/models"), row)
        self.select_models_button = self._button("全选可操作模型", self._select_models, row)
        self.clear_models_button = self._button("清空选择", self._clear_models, row)
        self.delete_models_button = self._button("移入回收区", lambda: self._change_models(False), row)
        self.restore_models_button = self._button("恢复所选", lambda: self._change_models(True), row)
        box.addLayout(row)
        self.model_table = self._table(["选择", "模型编号", "模型方法", "保存（北京时间）", "训练点位", "可用状态"])
        self.model_table.setMinimumHeight(180)
        self.model_table.itemChanged.connect(self._model_item_changed)
        box.addWidget(self.model_table)
        self.model_selection_status = self._label()
        box.addWidget(self.model_selection_status)
        layout.addWidget(management, 1)

    def _tab_changed(self, index):
        if index == 1 and not self._list_loaded:
            self._send("history/sessions")
        if index in {1, 2, 3} and not self._models_loaded:
            self._send("analysis/models")

    def _load_preferences(self):
        preferences = getattr(self.ctx, "preferences", {}) or {}
        experiment = preferences.get("experiment") or {}
        analysis = preferences.get("analysis") or {}
        self._rendering = True
        self.finger_id.setText(str(experiment.get("finger_id", "")))
        for widget, key in [(self.capture_mode, "capture_mode"), (self.protocol, "acquisition_protocol"), (self.force_sign, "force_sign")]:
            index = widget.findData(experiment.get(key))
            if index >= 0:
                widget.setCurrentIndex(index)
        for widget, key in [(self.target_force, "target_force_norm_N"), (self.max_force, "max_force_norm_N"), (self.repetitions, "repetitions_per_point")]:
            if finite(experiment.get(key)):
                widget.setValue(experiment[key])
        point = experiment.get("selected_point_id")
        if point in POINT_IDS or point == NO_CONTACT:
            self.pending_point = point
        index = self.training_method.findData(analysis.get("method"))
        if index >= 0:
            self.training_method.setCurrentIndex(index)
        for key, widget in self.parameter_widgets.items():
            if finite(analysis.get(key)):
                widget.setValue(analysis[key])
        self.adapt_legacy.setChecked(analysis.get("adapt_legacy_unloaded") is True)
        self._rendering = False
        self._refresh_controls()
        if experiment.get("acquisition_protocol") == "timed_wrench_v1":
            self._notice("固定 3 秒协议仅保留历史兼容。新会话采用合力目标＋手动结束，请核对目标与上限；历史记录保持原样。")

    def _bind_preferences(self):
        self._preference_timer = QTimer(self)
        self._preference_timer.setSingleShot(True)
        self._preference_timer.setInterval(400)
        self._preference_timer.timeout.connect(self._save_preferences)
        self.finger_id.editingFinished.connect(lambda: self._queue_preference("experiment", "finger_id", self.finger_id.text()))
        for widget, key in [(self.capture_mode, "capture_mode"), (self.protocol, "acquisition_protocol"), (self.force_sign, "force_sign")]:
            widget.currentIndexChanged.connect(lambda _index, w=widget, k=key: self._queue_preference("experiment", k, w.currentData()))
        for widget, key in [(self.target_force, "target_force_norm_N"), (self.max_force, "max_force_norm_N"), (self.repetitions, "repetitions_per_point")]:
            widget.valueChanged.connect(lambda value, k=key: self._queue_preference("experiment", k, value))
        self.training_method.currentIndexChanged.connect(lambda _index: self._queue_training_method_preference())
        for key, widget in self.parameter_widgets.items():
            widget.valueChanged.connect(lambda value, k=key: self._queue_preference("analysis", k, value))
        self.adapt_legacy.toggled.connect(lambda value: self._queue_preference("analysis", "adapt_legacy_unloaded", value))

    def _queue_training_method_preference(self):
        method = self.training_method.currentData()
        self._queue_preference("analysis", "method", method)
        if method in SEQUENCE_METHODS:
            self._queue_preference("analysis", "input_kind", "delta_lambda_9" if method == "drift_gru" else "delta_S")

    def _queue_preference(self, section, key, value):
        if self._rendering:
            return
        saved = (getattr(self.ctx, "preferences", {}) or {}).get(section) or {}
        if saved.get(key) == value and key not in self._preference_updates.get(section, {}):
            return
        self._preference_updates.setdefault(section, {})[key] = value
        self._preference_timer.start()

    def _save_preferences(self):
        updates, self._preference_updates = self._preference_updates, {}
        for section, values in updates.items():
            if section == "experiment" and ("max_force_norm_N" in values or "target_force_norm_N" in values):
                if self.protocol.currentData() == "force_target_wrench_v1" and self.target_force.value() >= self.max_force.value():
                    self._notice("合力目标必须低于上限，尚未保存这项设置；相同数值会使轻微超调锁定本轮，请核对后修改。")
                    values = {k: v for k, v in values.items() if k not in {"max_force_norm_N", "target_force_norm_N"}}
                else:
                    values.update(max_force_norm_N=self.max_force.value(), target_force_norm_N=self.target_force.value())
            if values:
                self.ctx.execute("settings", {"section": section, "values": values})

    def _send(self, action, payload=None):
        if action in self._pending:
            return
        self._pending.add(action)
        self._refresh_controls()
        try:
            self.ctx.execute(action, payload)
        except Exception as exc:
            self._pending.discard(action)
            self._error(str(exc))

    def _error(self, error):
        # The context reports an error without the action name. Its FIFO worker
        # has completed that command; avoid trapping the workspace in busy state.
        self._pending.clear()
        self.message.setText("操作未完成：" + str(error))
        self.message.setStyleSheet("color:#b83b32;")
        self._refresh_controls()

    def _notice(self, text):
        self.message.setText(text)
        self.message.setStyleSheet("")

    def _action_finished(self, action, result):
        if action in {"state", "pulse_samples", "pulse/samples"}:
            return
        owned = action in self._pending
        self._pending.discard(action)
        if not isinstance(result, dict):
            self._refresh_controls()
            return
        if "sessions" in result:
            self.sessions = result.get("sessions") or []
            self.deleted_sessions = result.get("deleted_sessions") or []
            self._list_loaded = True
            self._apply_cumulative()
            self._render_sessions()
        if "models" in result:
            self.models = result.get("models") or []
            self.deleted_models = result.get("deleted_models") or []
            self._models_loaded = True
            self._render_models()
        if "training" in result:
            self.update_state({**self.state, "analysis": {**(self.state.get("analysis") or {}), "training": result["training"]}})
        if result.get("product") != "9FBG" and any(key in result for key in ("experiment", "analysis", "validation", "running")):
            merged = {**self.state, **result}
            for key in ("experiment", "analysis", "validation"):
                if key in result:
                    merged[key] = {**(self.state.get(key) or {}), **(result[key] or {})}
            self.update_state(merged)
        if action == "validation/freeze":
            sample = result.get("sample") or (result.get("validation") or {}).get("pending_sample")
            if not sample or not sample.get("sample_id") or sample.get("valid") is not True:
                self._error("服务端未返回有效的冻结证据，未建立待判断读数。")
                return
            self.frozen_sample = deepcopy(sample)
            self.position_correct.setChecked(False)
            self.force_correct.setChecked(False)
        if action in {"validation/judge", "validation/discard"}:
            self.frozen_sample = None
            self.position_correct.setChecked(False)
            self.force_correct.setChecked(False)
        if action in {"experiment/start", "experiment/point", "experiment/baseline", "analysis/load", "analysis/reference"}:
            self.unloaded_confirmed.setChecked(False)
            self.test_unloaded.setChecked(False)
        if owned and action in {"analysis/load", "analysis/stop"}:
            self._send("analysis/models")
        if action in {"history/delete", "history/restore"}:
            self.selected_sessions.clear()
            self._render_sessions()
        if action in {"analysis/models/delete", "analysis/models/restore"}:
            self.selected_models.clear()
            self._render_models()
        if owned:
            messages = {"experiment/start": "采集会话已建立；请确认实际点位，再确认无接触并标定本轮。",
                        "experiment/baseline": "正在标定，请保持无接触，等待服务端参考就绪。",
                        "experiment/stop": "已停止并保存采集会话。",
                        "training/start": "已提交真实训练任务；只有模型校验保存完成后才显示 100%。",
                        "training/pause": "已请求暂停，等待当前批次与服务端确认。",
                        "training/resume": "已解除用户暂停；采集保护暂停仍由服务端保留。",
                        "training/cancel": "已请求取消，等待训练进程退出与资源回收。",
                        "analysis/load": "模型已载入；重新明确确认无接触并建立本次参考。",
                        "analysis/reference": "正在建立本次模型推理参考；保持无接触，等待参考就绪。",
                        "analysis/stop": "推理已结束；重新载入模型并建立无接触参考后可再次推理。",
                        "validation/freeze": "本次读数和目标已固定，请核对后保存判断或重新判断。",
                        "validation/judge": "本次人工判断已保存。", "validation/discard": "已放弃待判断读数；未保存判断不计入分母。",
                        "validation/finish": "本轮真实按压判断报告已保存；模型推理继续。",
                        "history/delete": "所选会话已移入可恢复回收区。", "history/restore": "所选会话已恢复。",
                        "analysis/models/delete": "所选模型已移入可恢复回收区，训练数据与人工报告保留。",
                        "analysis/models/restore": "所选模型已恢复；手动载入并重新建立参考后推理。"}
            if action in messages:
                self._notice(messages[action])
            if action == "history/open":
                self._notice(("已打开保存位置：" if result.get("opened") else "保存位置未能打开：") + str(result.get("path", "")))
        self._render_inference()
        self._refresh_controls()

    def update_state(self, state):
        if not isinstance(state, dict):
            return
        old_experiment = self.state.get("experiment") or {}
        self.state = state
        self._received_at = time.monotonic()
        self._display_stale = False
        experiment = state.get("experiment") or {}
        if experiment.get("session_id") != old_experiment.get("session_id"):
            self.unloaded_confirmed.setChecked(False)
            self._force_series = []
        sample = (state.get("validation") or {}).get("pending_sample")
        if sample and sample.get("sample_id"):
            if not self.frozen_sample or self.frozen_sample.get("sample_id") != sample.get("sample_id"):
                self.position_correct.setChecked(False)
                self.force_correct.setChecked(False)
            self.frozen_sample = deepcopy(sample)
            self.target_point = sample.get("target_point_id", sample.get("actual_point_id"))
        elif self.frozen_sample and self.frozen_sample.get("sample_id") == (state.get("validation") or {}).get("last_saved_sample_id"):
            self.frozen_sample = None
        if experiment.get("running") and experiment.get("settings"):
            settings = experiment["settings"]
            self._rendering = True
            for widget, key in [(self.capture_mode, "capture_mode"), (self.protocol, "acquisition_protocol"), (self.force_sign, "force_sign")]:
                index = widget.findData(settings.get(key))
                if index >= 0:
                    widget.setCurrentIndex(index)
            self._rendering = False
        self._render_capture()
        self._render_training()
        self._render_inference()
        self._refresh_controls()

    def _stale(self):
        return not self._received_at or time.monotonic() - self._received_at > 1.5

    def _busy(self):
        return bool(self._pending - {"history/sessions", "analysis/models", "history/open", "settings"})

    def _training_busy(self):
        analysis = self.state.get("analysis") or {}
        training = analysis.get("training") or {}
        return analysis.get("busy") is True or training.get("busy") is True or training.get("status") in {"running", "pause_requested", "paused", "cancelling"}

    def _mutation_blocked(self, models=False):
        return self._busy() or self._stale() or (self.state.get("experiment") or {}).get("running") is True or self._training_busy() or models and bool(self.frozen_sample or (self.state.get("validation") or {}).get("pending_sample"))

    def _capture_setting_changed(self):
        if self._rendering:
            return
        self.unloaded_confirmed.setChecked(False)
        if self.capture_mode.currentData() == "peak_drift" and self.protocol.currentData() != "force_target_wrench_v1":
            self.protocol.setCurrentIndex(self.protocol.findData("force_target_wrench_v1"))
        self._refresh_controls()

    def _actual_sample_rate(self):
        for device in self.state.get("devices") or []:
            if device.get("wavelength_axis") or device.get("sdk_status_raw") or (device.get("device_identity") or {}).get("spectrum_points"):
                interval = (device.get("sdk_status_raw") or {}).get("interval_ms", (device.get("device_identity") or {}).get("interval_ms"))
                if finite(interval) and interval > 0:
                    return 1000 / interval
        return (self.state.get("timing") or {}).get("sample_rate_hz", (self.state.get("config") or {}).get("sample_rate_hz", self.state.get("sample_rate_hz")))

    def _capture_force_age(self):
        age = ((self.state.get("experiment") or {}).get("live_force") or {}).get("age_ms")
        if not finite(age):
            return None
        return age + max(0, time.monotonic() - self._received_at) * 1000

    def _capture_force_valid(self):
        force = ((self.state.get("experiment") or {}).get("live_force") or {})
        age = self._capture_force_age()
        return force.get("valid") is True and finite(age) and age <= 250 and not self._stale()

    def _start_capture(self):
        finger = self.finger_id.text()
        if not finger.strip():
            return self._error("请填写实际样件标识。")
        mode, protocol = self.capture_mode.currentData(), self.protocol.currentData()
        wrench = protocol == "force_target_wrench_v1"
        if mode == "peak_drift" and not wrench:
            return self._error("寻峰漂移须使用合力目标＋手动结束协议。")
        if wrench and self._actual_sample_rate() != 25:
            return self._error("六维时序采集要求实际 25 Hz 光谱请求；请停止连接后明确修改采样设置并重新连接。")
        if wrench and self.target_force.value() >= self.max_force.value():
            return self._error("合力目标必须低于合力上限；目标与上限相同会使达标后的轻微超调判为超限。请明确设置不同数值。")
        if not capture_gates(self.state, self.pending_point, self._busy() or self._stale())["start"]:
            return self._error("当前不能建立会话；请连接真实光谱与六轴设备并等待新鲜状态。")
        payload = {"finger_id": finger, "batch": "A", "force_sign": self.force_sign.currentData(),
                   "setup_policy": SETUP_POLICY,
                   "coordinate_mode": "normalized", "conditions": {"fixation": ""},
                   "max_force_N": 3, "guided_acquisition": True, "repetitions_per_point": self.repetitions.value(),
                   "acquisition_protocol": protocol, "capture_mode": mode,
                   "input_kind": "delta_lambda_9" if mode == "peak_drift" else "delta_S"}
        if wrench:
            payload.update(max_force_norm_N=self.max_force.value(), target_force_norm_N=self.target_force.value())
        self._send("experiment/start", payload)

    def _select_capture_point(self, point):
        if not capture_gates(self.state, self.pending_point, self._busy() or self._stale())["point"]:
            return
        self.pending_point = point
        self._queue_preference("experiment", "selected_point_id", point)
        self.unloaded_confirmed.setChecked(False)
        self._render_capture()
        self._refresh_controls()

    def _confirm_point(self):
        if not capture_gates(self.state, self.pending_point, self._busy() or self._stale())["point_confirm"]:
            return self._error("当前不能切换已确认点位；请等待本轮结束并保存。")
        self.unloaded_confirmed.setChecked(False)
        self._send("experiment/point", {"point_id": self.pending_point})

    def _baseline(self):
        if not self.unloaded_confirmed.isChecked():
            return self._error("请先移开接触棒并明确确认当前无接触。")
        if not capture_gates(self.state, self.pending_point, self._busy() or self._stale())["baseline"]:
            return self._error("当前不能标定；请先确认实际点位并等待本轮结束。")
        self.unloaded_confirmed.setChecked(False)
        self._send("experiment/baseline", {"unloaded_confirmed": True})

    def _trial_action(self, action):
        if not capture_gates(self.state, self.pending_point, self._busy() or self._stale())["trial_" + action]:
            return self._error("当前阶段不允许该轮次操作，请核对服务端采集状态。")
        self._send("experiment/trial", {"action": action})

    def _finish_trial(self):
        if not capture_gates(self.state, self.pending_point, self._busy() or self._stale())["trial_finish"]:
            return self._error(self._finish_control_reason())
        self._send("experiment/trial", {"action": "finish"})

    def _finish_control_reason(self):
        if self._stale():
            return "服务状态已过期，正在等待新的状态；结束本轮暂不可用。"
        if self._busy():
            return "正在处理上一项操作，请等待完成后结束本轮。"
        experiment = self.state.get("experiment") or {}
        guided = experiment.get("guided") or {}
        if guided.get("finish_block_reason"):
            return str(guided["finish_block_reason"])
        if capture_gates(self.state, self.pending_point)["trial_finish"]:
            return "本轮采集数据检查已通过；请先移开接触棒，点击即确认已卸载并开始保存。"
        return "当前阶段不能结束本轮，请核对采集阶段并等待新的服务状态。"

    def _render_capture_guidance(self):
        experiment = self.state.get("experiment") or {}
        guided = experiment.get("guided") or {}
        text = experiment.get("error") or guided.get("message") or "待开始：建立会话、确认实际点位，再确认无接触并标定本轮。"
        press_ready = capture_point_states(self.state, self._busy() or self._stale()).get(experiment.get("point_id")) == "press_ready"
        if experiment.get("error") or guided.get("step") == "retry" or guided.get("quality_faults") or experiment.get("phase") == "preload":
            color = "#b83b32"
        else:
            color = "#127c4a" if press_ready else "#172b4d"
        style = f"font-size:11pt; font-weight:500; padding:6px; color:{color};"
        if self.guidance.text() != text:
            self.guidance.setText(text)
        if self.guidance.styleSheet() != style:
            self.guidance.setStyleSheet(style)

    def _render_capture(self, record_force=True):
        experiment = self.state.get("experiment") or {}
        guided = experiment.get("guided") or {}
        settings = experiment.get("settings") or {}
        gates = capture_gates(self.state, self.pending_point, self._busy() or self._stale())
        current, known = self.capture_grid.render(settings, bool(experiment.get("session_id")), self.pending_point,
                                                 counts=guided.get("accepted_by_point"), enabled=gates["point"],
                                                 capture_states=capture_point_states(self.state, self._busy() or self._stale()))
        self.capture_axes.setText("原点左下 P01；+x / +u → 右；+y / +v ↑ 指尖" if current else "历史点阵：保留原编号与方向" if known else "历史点阵方向未记录；编号不声明空间坐标")
        self.no_contact_button.setChecked(self.pending_point == NO_CONTACT)
        no_contact_count = (guided.get("accepted_by_point") or {}).get(NO_CONTACT, guided.get("no_contact_accepted", 0))
        if type(no_contact_count) is not int or no_contact_count < 0:
            no_contact_count = 0
        self.no_contact_button.setText(f"无接触 · 已采集 {no_contact_count} 轮")
        confirmed = experiment.get("point_id")
        display = lambda p: "无接触" if p == NO_CONTACT else p or "尚未确认"
        self.point_status.setText(f"预选：{display(self.pending_point)}；已确认：{display(confirmed)}")
        self.session_status.setText("会话：" + str(experiment.get("session_id") or "尚未建立") + "\n保存位置：" + str(experiment.get("path") or experiment.get("session_path") or "待建立") + "\n数据编号：" + str(experiment.get("data_id") or settings.get("data_id") or "服务端分配"))
        self._render_capture_guidance()
        preload = guided.get("preload") or {}
        self.preload_status.setText((f"连续有效无载 {fmt(preload.get('valid_continuous_ms'), 0)} / {fmt(preload.get('target_ms'), 0)} ms；等待 {fmt(preload.get('elapsed_ms'), 0)} ms；重置 {preload.get('reset_count', '—')} 次\n" + str(preload.get("last_block_reason") or "等待服务端诊断") + ("\n预采样超时，本轮失败不计数；须重新明确确认无载并标定。" if preload.get("timed_out") else "")) if preload else str(guided.get("finish_block_reason") or ""))
        if experiment.get("trial_id") and experiment.get("phase") == "release" and settings.get("acquisition_protocol") == "force_target_wrench_v1":
            self.preload_status.setText(self._finish_control_reason())
        baseline = experiment.get("baseline") or {}
        ready = baseline.get("ready") is True or baseline.get("status") == "ready"
        self.baseline_status.setText(("本轮无载参考已就绪" if ready else "正在采集并核验无载参考" if baseline.get("status") == "collecting" else "本轮无载参考尚未就绪") + (" · " + baseline["reason"] if baseline.get("reason") else ""))
        progress = [("total", "25 点总采集进度", "progress_percent"),
                    ("point", "无接触独立合格次数" if confirmed == NO_CONTACT else "当前点合格轮次", "point_progress_percent"),
                    ("force", "本轮加载进度（服务端）", "force_progress_percent")]
        for key, title, percent_key in progress:
            percent = guided.get(percent_key, 0)
            percent = max(0, min(100, percent)) if finite(percent) else 0
            label, bar = self.progress_bars[key]
            label.setText(title)
            bar.setValue(round(percent))
        force = experiment.get("live_force") or {}
        force_age = self._capture_force_age()
        valid = self._capture_force_valid()
        self._force_display_valid = valid
        wrench = settings.get("acquisition_protocol", self.protocol.currentData()) in WRENCH_PROTOCOLS
        value = force.get("force_norm_N" if wrench else "force_N") if valid else None
        limit = settings.get("max_force_norm_N", 5) if wrench else settings.get("max_force_N", 3)
        self.force_plot.set_labels("本页观察时间 / s", "PX6D 合力 / N" if wrench else "PX6D 按压力 / N")
        self.live_force.setText("PX6D 当前" + ("三维合力" if wrench else "按压力") + f"：{fmt(value)} N")
        self.live_force.setStyleSheet("font-size:18pt; font-weight:600;" + ("color:#b83b32;" if finite(value) and finite(limit) and value > limit else ""))
        self.force_detail.setText((f"最新六轴年龄 {fmt(force_age, 0)} ms；峰值 {fmt(guided.get('observed_peak_force_norm_N'))} N；上限 {fmt(settings.get('max_force_norm_N', self.max_force.value()))} N" if wrench else f"原始 Fz：{fmt(force.get('raw_Fz_N'))} N；本轮零点：{fmt(force.get('zero_Fz_N'))} N") + ("\n" + str(force.get("reason") or "等待有效六轴源帧") if not valid else ""))
        raw_force = self.state.get("force") or {}
        frame = (raw_force.get("stream_epoch"), raw_force.get("frame_id"), raw_force.get("host_rx_monotonic_ns"), value)
        if record_force and frame != self._last_force_frame:
            self._last_force_frame = frame
            self._force_series.append((time.monotonic(), value))
            self._force_series = self._force_series[-600:]
            origin = self._force_series[0][0]
            self.force_plot.set_series([{"name": "PX6D 合力" if wrench else "PX6D 按压力", "x": [t-origin for t, _v in self._force_series], "y": [v for _t, v in self._force_series], "color": "#157cba"}])
        quality = guided.get("last_quality") or {}
        quality_round = "上一轮" if experiment.get("trial_id") else "最近完成轮次"
        self.quality_status.setText((quality_round + ("检查通过" if quality.get("accepted") else "检查未通过")) + "；" + "；".join(quality.get("reasons") or []) if "accepted" in quality else "尚无完成轮次的判定；合格次数只由真实质量检查计入。")
        counts = experiment.get("counts") or {}
        self.record_status.setText("阶段：" + PHASE_LABELS.get(experiment.get("phase"), str(experiment.get("phase") or "等待会话")) + "；试次：" + str(experiment.get("trial_id") or "尚未开始") + f"\n光学帧 {counts.get('drift_frames' if settings.get('capture_mode') == 'peak_drift' else 'raw_spectra', 0)}；六轴 {counts.get('raw_force', 0)}；有效 / 无效同步 {counts.get('sync_valid', 0)} / {counts.get('sync_invalid', 0)}；完成 / 中止 {counts.get('trials_completed', 0)} / {counts.get('trials_aborted', 0)}\n" + ("九路峰心、漂移、六轴和事件连续保存；不保存 512 点原谱。" if settings.get("capture_mode") == "peak_drift" else "原始光谱、六轴、结果与事件连续保存；失败或中止记录保留。"))

    def _session_rows(self):
        return self.deleted_sessions if self.show_deleted_sessions.isChecked() else self.sessions

    def _apply_cumulative(self):
        if self.cumulative.isChecked() and self.training_method.currentData() in SEQUENCE_METHODS and self.training_mode.currentData() != "finetune" and not self.show_deleted_sessions.isChecked():
            self.selected_sessions = {row["session_id"] for row in self.sessions if training_status(row, self.training_method.currentData())[0]}

    def _render_sessions(self):
        self._rendering = True
        rows = self._session_rows()
        self.session_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            sid, settings = row.get("session_id", ""), row.get("settings") or {}
            allowed, reason = training_status(row, self.training_method.currentData())
            check = QTableWidgetItem()
            check.setData(Qt.ItemDataRole.UserRole, sid)
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if sid in self.selected_sessions else Qt.CheckState.Unchecked)
            self.session_table.setItem(i, 0, check)
            mode = row.get("capture_mode", settings.get("capture_mode"))
            values = [str(row.get("data_id") or settings.get("data_id") or "未编号") + " / " + sid,
                      local_time(row.get("started_utc")), str(settings.get("finger_id", row.get("finger_id", "未记录"))) + " / " + str(settings.get("batch", row.get("batch", "—"))),
                      "寻峰漂移 · 九路时序" if mode == "peak_drift" else "旧九路漂移 · 含原谱备份" if settings.get("input_kind") == "delta_lambda_9" else "原始全谱 · 512 点",
                      f"{row.get('accepted_trial_count', '—')} / {row.get('trial_count', '—')}（无接触 {row.get('accepted_no_contact_trial_count', 0)}）", reason]
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setToolTip(str(row.get("history_error") or value))
                self.session_table.setItem(i, col, item)
        self._rendering = False
        self._render_selection()
        self._refresh_controls()

    def _render_selection(self):
        method = self.training_method.currentData()
        selected = [next((r for r in self._session_rows() if r.get("session_id") == sid), None) for sid in sorted(self.selected_sessions)]
        invalid = [(sid, training_status(next((r for r in self._session_rows() if r.get("session_id") == sid), None), method)[1]) for sid in sorted(self.selected_sessions) if not training_status(next((r for r in self._session_rows() if r.get("session_id") == sid), None), method)[0]]
        accepted = sum(r.get("accepted_trial_count", 0) for r in selected if r)
        no_contact = sum(r.get("accepted_no_contact_trial_count", 0) for r in selected if r)
        attempts = sum(r.get("trial_count", 0) for r in selected if r)
        self.selection_status.setText(f"已选择 {len(self.selected_sessions)} 个会话 · 合格 {accepted} 轮（按压 {max(0, accepted-no_contact)} / 无接触 {no_contact}）· 总试次 {attempts} 轮" + ("\n训练已停用：" + "；".join(f"{sid}：{reason}" for sid, reason in invalid) if invalid else "") + ("\n继续训练仅勾选新增来源；基础模型历史来源由服务端合并去重。" if self.training_mode.currentData() == "finetune" else ""))

    def _session_item_changed(self, item):
        if self._rendering or item.column() != 0:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        self.cumulative.blockSignals(True)
        self.cumulative.setChecked(False)
        self.cumulative.blockSignals(False)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected_sessions.add(sid)
        else:
            self.selected_sessions.discard(sid)
        self._render_selection()
        self._refresh_controls()

    def _history_view_changed(self):
        self.selected_sessions.clear()
        self._render_sessions()

    def _select_trainable(self):
        self.cumulative.setChecked(False)
        self.selected_sessions = {r["session_id"] for r in self.sessions if training_status(r, self.training_method.currentData())[0]}
        self._render_sessions()

    def _clear_sessions(self):
        self.cumulative.setChecked(False)
        self.selected_sessions.clear()
        self._render_sessions()

    def _cumulative_changed(self):
        self._apply_cumulative()
        self._render_sessions()

    def _open_session(self):
        row = self.session_table.currentRow()
        if row < 0 or row >= len(self._session_rows()):
            return self._error("请先点选一行采集记录。")
        self._send("history/open", {"session_id": self._session_rows()[row]["session_id"], "deleted": self.show_deleted_sessions.isChecked()})

    def _confirm_mutation(self, title, text):
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setText(text)
        confirm = dialog.addButton("确认", QMessageBox.ButtonRole.AcceptRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        return dialog.clickedButton() == confirm

    def _change_history(self, restore):
        if self._mutation_blocked() or not self.selected_sessions or restore != self.show_deleted_sessions.isChecked():
            return self._error("当前不能操作记录；请先停止并保存采集、等待训练结束并选择对应记录。")
        ids = sorted(self.selected_sessions)
        if not self._confirm_mutation("恢复采集记录" if restore else "移入可恢复回收区", ("将恢复以下记录：" if restore else "将把以下记录移入可恢复回收区，原始数据不会永久删除：") + "\n" + "\n".join(ids)):
            return
        if self._mutation_blocked():
            return self._error("状态已变化，当前不能操作记录。")
        self._send("history/restore" if restore else "history/delete", {"session_ids": ids})

    def _training_method_changed(self):
        if self._rendering:
            return
        if self.training_method.currentData() not in SEQUENCE_METHODS:
            self.training_mode.setCurrentIndex(0)
        self._apply_cumulative()
        self._render_sessions()
        self._render_model_combos()
        self._refresh_controls()

    def _training_mode_changed(self):
        if self._rendering:
            return
        finetune = self.training_mode.currentData() == "finetune"
        self.parameter_widgets["learning_rate"].setValue(.0001 if finetune else .001)
        if finetune:
            self.cumulative.setChecked(False)
            self.selected_sessions.clear()
            self.adapt_legacy.setChecked(False)
        self._render_sessions()
        self._refresh_controls()

    def _start_training(self):
        method = self.training_method.currentData()
        if not self.selected_sessions or self.show_deleted_sessions.isChecked():
            return self._error("请勾选完整保存的可训练会话；回收区记录须先恢复。")
        invalid = []
        for sid in sorted(self.selected_sessions):
            allowed, reason = training_status(next((r for r in self.sessions if r.get("session_id") == sid), None), method)
            if not allowed:
                invalid.append(sid + "：" + reason)
        if invalid:
            return self._error("所选记录不能全部训练，程序不会静默跳过：" + "；".join(invalid))
        experiment = self.state.get("experiment") or {}
        if self._stale() or self._busy() or self._training_busy() or experiment.get("running") and method not in SEQUENCE_METHODS:
            return self._error("请等待已有训练结束，非时序训练须先停止并保存采集。")
        if experiment.get("running") and experiment.get("session_id") in self.selected_sessions:
            return self._error("当前采集会话尚未完整保存，须停止并保存后再训练。")
        if experiment.get("running") and ((experiment.get("settings") or {}).get("acquisition_protocol") not in WRENCH_PROTOCOLS or experiment.get("trial_id")):
            return self._error("时序训练只能在六维采集试次间隙启动；请等待本轮保存。")
        payload = {"session_ids": sorted(self.selected_sessions), "method": method}
        if method in SEQUENCE_METHODS:
            parameters = {key: widget.value() for key, widget in self.parameter_widgets.items()}
            if parameters["position_loss_weight"] + parameters["wrench_loss_weight"] <= 0:
                return self._error("位置与六维力损失权重至少有一项须大于零。")
            finetune = self.training_mode.currentData() == "finetune"
            parameters["training_mode"] = "finetune" if finetune else "scratch"
            if finetune:
                base = self._selected_base_model()
                if not base:
                    return self._error("请明确选择可用且输入相同的基础模型。")
                parameters["base_model_id"] = base["model_id"]
            else:
                parameters["adapt_legacy_unloaded"] = method != "drift_gru" and self.adapt_legacy.isChecked()
            payload.update(parameters)
        self._send("training/start", payload)

    def _render_training(self):
        training = (self.state.get("analysis") or {}).get("training") or {}
        progress = training.get("progress_percent")
        complete = training.get("status") == "completed" and progress == 100 and bool(training.get("model_id"))
        self.training_progress.setRange(0, 100 if finite(progress) else 0)
        if finite(progress):
            self.training_progress.setValue(round(min(100 if complete else 99, max(0, progress))))
        statuses = {"idle": "尚未开始训练", "running": "正在训练", "pause_requested": "已请求暂停 · 等待服务端确认", "paused": "训练已暂停", "cancelling": "正在取消训练", "cancelled": "训练已取消", "failed": "训练失败", "completed": "训练完成 · 模型已保存" if complete else "等待确认模型保存完成"}
        reasons = ["用户主动暂停" if p == "user" else "采集保护暂停" if p in {"acquisition", "trial"} else str(p) for p in training.get("pause_reasons") or []]
        self.training_status_label.setText(statuses.get(training.get("status", "idle"), "等待训练状态") + "\n" + str(training.get("message") or "") + (f"\n实际迭代 {training.get('current_iteration', training.get('epoch', '—'))} / {training.get('max_iterations', training.get('epochs', '—'))}" if training.get("status") != "idle" else "") + ("\n暂停原因：" + "；".join(reasons) if reasons else "") + ("\n错误：" + str(training["error"]) if training.get("error") else ""))
        report = training.get("report") or {}
        if report or complete:
            text = "模型编号：" + str(training.get("model_id") or "尚未保存") + "\n方法：" + METHOD_LABELS.get(training.get("method"), str(training.get("method") or "未记录"))
            text += "\n训练点位：" + "、".join(training.get("trained_point_ids", training.get("point_ids")) or [])
            text += "\n保存时间（北京时间）：" + str(training.get("created_local_minute") or local_time(training.get("created_utc")))
            text += "\n仅反映所选真实数据的研究验证，不构成完整 25 点或跨天精度验收。"
            if report.get("validation_note"):
                text += "\n验证范围：" + str(report["validation_note"])
            if training.get("training_mode") == "finetune":
                text += "\n继承基础模型：" + str(training.get("base_model_id")) + f"；新增 {len(training.get('new_session_ids') or [])} 个会话；合并 {len(training.get('merged_session_ids') or [])} 个来源"
            text += self._metrics_text(report)
            if self.training_result.toPlainText() != text:
                self.training_result.setPlainText(text)
            history = report.get("history") or []
            self.loss_plot.set_series([{"name": label, "x": [v.get("epoch") for v in history], "y": [v.get(key) for v in history], "color": color}
                                       for key, label, color in [("train_loss", "训练损失", "#157cba"), ("validation_loss", "整轮留出验证损失", "#d58618")]])
        if complete and self._last_training_model != training["model_id"]:
            self._last_training_model = training["model_id"]
            self._send("analysis/models")

    def _metrics_text(self, report):
        lines = []
        labels = {"samples": "样本数", "n": "样本数", "window_count": "窗口数", "position_count": "位置样本数", "classification_accuracy": "位置正确率", "position_accuracy": "位置正确率", "force_mae_N": "力平均绝对误差 / N", "force_rmse_N": "力均方根误差 / N", "wrench_mae": "六轴平均绝对误差", "wrench_rmse": "六轴均方根误差", "contact_accuracy": "接触分类正确率", "joint_accuracy": "位置与力联合正确率", "count": "样本数", "accuracy": "正确率", "contact_count": "接触样本数", "no_contact_count": "无接触样本数", "contact_recall": "接触召回率", "no_contact_false_positive_rate": "无接触误报率", "no_contact_retention_rate": "无接触保持率", "brier_score": "概率误差评分", "true_positive": "接触正确次数", "false_positive": "无接触误报次数", "true_negative": "无接触正确次数", "false_negative": "接触漏报次数"}
        def metrics_text(metrics):
            parts = [labels[k] + "=" + fmt(v, 4) for k, v in metrics.items() if k in labels and finite(v)]
            wrench = metrics.get("wrench") or {}
            for field, title in [("mae", "平均绝对误差"), ("rmse", "均方根误差")]:
                values = wrench.get(field)
                if isinstance(values, list) and len(values) == 6:
                    parts.append("六轴" + title + "：" + "，".join(f"{axis}={fmt(v, 4)} {unit}" for axis, unit, v in zip(AXES, UNITS, values)))
            contact = metrics.get("contact")
            if isinstance(contact, dict):
                parts.append("接触分类：" + "，".join(labels[k] + "=" + fmt(v, 4) for k, v in contact.items() if k in labels and finite(v)))
            return "；".join(parts)
        for key, label in [("train", "训练"), ("validation", "整轮留出验证"), ("summary", "汇总")]:
            metrics = report.get(key)
            if isinstance(metrics, dict):
                lines.append("\n" + label + "：" + metrics_text(metrics))
        comparison = report.get("finetuning_comparison") or report.get("finetune_comparison") or report.get("comparison")
        if isinstance(comparison, dict):
            if comparison.get("available"):
                lines.append("\n同一固定验证集继续训练对照（" + str(comparison.get("window_count", "—")) + " 个窗口）：")
                for field, title in [("parent", "基础模型"), ("child", "候选模型")]:
                    if isinstance(comparison.get(field), dict):
                        lines.append("\n" + title + "：" + metrics_text(comparison[field]))
            else:
                lines.append("\n继续训练对照不可用：" + str(comparison.get("reason") or "缺少固定独立验证集"))
        return "".join(lines)

    def _model_rows(self):
        return self.deleted_models if self.show_deleted_models.isChecked() else self.models

    def _model_method(self, model):
        if model.get("method") == "drift_gru" or model.get("input_kind") == "delta_lambda_9" or (model.get("metadata") or {}).get("input_kind") == "delta_lambda_9":
            return "drift_gru"
        return model.get("method") or model.get("model_kind")

    def _model_block_reason(self, model, restore=None):
        if not model:
            return "模型已变化，请刷新后重选"
        restore = self.show_deleted_models.isChecked() if restore is None else restore
        analysis = self.state.get("analysis") or {}
        if not restore and model.get("model_id") == analysis.get("model_id") and analysis.get("inference_active") is True:
            return "正在推理，请先结束推理"
        if model.get("restorable" if restore else "deletable") is not True:
            return model.get("model_error") or model.get("history_error") or model.get("reason") or ("当前不可恢复" if restore else "当前不可移入回收区")
        return ""

    def _render_model_combos(self):
        previous, base_id = self.model_select.currentData(), self.base_model.currentData()
        self.model_select.blockSignals(True)
        self.base_model.blockSignals(True)
        self.model_select.clear()
        self.model_select.addItem("请选择已保存模型", "")
        self.base_model.clear()
        self.base_model.addItem("请选择同输入基础模型", "")
        base_ids = set()
        for model in self.models:
            mid = model.get("model_id")
            label = str(mid) + " · " + METHOD_LABELS.get(self._model_method(model), str(model.get("method_label") or "研究模型")) + " · " + str(model.get("created_local_minute") or local_time(model.get("created_utc")))
            self.model_select.addItem(label + ("（不可载入）" if model.get("loadable") is False else ""), mid)
            if model.get("model_kind") == "spectral_gru" and self._model_method(model) == self.training_method.currentData() and model.get("loadable") is not False and model.get("deleted") is not True:
                self.base_model.addItem(label, mid)
                base_ids.add(mid)
        if previous and self.model_select.findData(previous) < 0:
            self.model_select.addItem("原模型已不可用：" + previous, previous)
        if base_id and base_id not in base_ids:
            self.base_model.addItem("原基础模型已不可用：" + base_id, base_id)
        self.model_select.setCurrentIndex(max(0, self.model_select.findData(previous)))
        self.base_model.setCurrentIndex(max(0, self.base_model.findData(base_id)))
        self.model_select.blockSignals(False)
        self.base_model.blockSignals(False)

    def _render_models(self):
        self._render_model_combos()
        self._rendering = True
        rows = self._model_rows()
        self.model_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            mid, reason = row.get("model_id", ""), self._model_block_reason(row)
            check = QTableWidgetItem()
            check.setData(Qt.ItemDataRole.UserRole, mid)
            flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
            if not reason:
                flags |= Qt.ItemFlag.ItemIsEnabled
            check.setFlags(flags)
            check.setCheckState(Qt.CheckState.Checked if mid in self.selected_models else Qt.CheckState.Unchecked)
            self.model_table.setItem(i, 0, check)
            values = [mid, METHOD_LABELS.get(self._model_method(row), str(row.get("method_label") or "研究模型")),
                      str(row.get("created_local_minute") or local_time(row.get("created_utc"))),
                      "、".join(row.get("trained_point_ids", row.get("point_ids")) or []) or "未记录", reason or ("可恢复" if self.show_deleted_models.isChecked() else "可移入回收区")]
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setToolTip(str(row.get("history_error") or value))
                self.model_table.setItem(i, col, item)
        self._rendering = False
        self.model_selection_status.setText(f"已勾选 {len(self.selected_models)} 个模型；当前推理模型须先结束推理。")
        self._refresh_controls()

    def _selected_base_model(self):
        mid = self.base_model.currentData()
        return next((m for m in self.models if m.get("model_id") == mid and m.get("model_kind") == "spectral_gru" and self._model_method(m) == self.training_method.currentData() and m.get("loadable") is not False and m.get("deleted") is not True), None)

    def _model_item_changed(self, item):
        if self._rendering or item.column() != 0:
            return
        mid = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected_models.add(mid)
        else:
            self.selected_models.discard(mid)
        self.model_selection_status.setText(f"已勾选 {len(self.selected_models)} 个模型")
        self._refresh_controls()

    def _model_view_changed(self):
        self.selected_models.clear()
        self._render_models()

    def _select_models(self):
        self.selected_models = {m["model_id"] for m in self._model_rows() if not self._model_block_reason(m)}
        self._render_models()

    def _clear_models(self):
        self.selected_models.clear()
        self._render_models()

    def _change_models(self, restore):
        rows = [next((m for m in self._model_rows() if m.get("model_id") == mid), None) for mid in self.selected_models]
        if self._mutation_blocked(True) or not rows or restore != self.show_deleted_models.isChecked() or any(self._model_block_reason(m, restore) for m in rows):
            return self._error("当前不能操作所选模型；请核对采集、训练、推理和待保存判断状态。")
        ids = sorted(self.selected_models)
        if not self._confirm_mutation("恢复模型" if restore else "移入可恢复回收区", ("将恢复以下模型：" if restore else "将把以下模型移入可恢复回收区；训练数据和人工判断报告保留：") + "\n" + "\n".join(ids)):
            return
        if self._mutation_blocked(True):
            return self._error("状态已变化，当前不能操作模型。")
        self._send("analysis/models/restore" if restore else "analysis/models/delete", {"model_ids": ids})

    def _go_inference(self):
        self._send("analysis/models")
        self.tabs.setCurrentIndex(2)
        mid = ((self.state.get("analysis") or {}).get("training") or {}).get("model_id")
        if self.model_select.findData(mid) >= 0:
            self.model_select.setCurrentIndex(self.model_select.findData(mid))
        self._notice("请选择刚保存的模型，手动载入后明确建立本次无接触参考。")

    def _load_model(self):
        mid = self.model_select.currentData()
        model = next((m for m in self.models if m.get("model_id") == mid), None)
        if not model or model.get("loadable") is False:
            return self._error("请明确选择当前可载入的真实保存模型。")
        spectral = model.get("model_kind") == "spectral_gru" or self._model_method(model) in SEQUENCE_METHODS
        if spectral and not self.test_unloaded.isChecked():
            return self._error("切换六维时序模型前，请先移开接触棒并明确确认无接触。")
        payload = {"model_id": mid}
        if spectral:
            payload["unloaded_confirmed"] = True
        self.test_unloaded.setChecked(False)
        self._send("analysis/load", payload)

    def _inference_reference(self):
        if not self.test_unloaded.isChecked():
            return self._error("请先明确确认当前无接触。")
        self.test_unloaded.setChecked(False)
        self._send("analysis/reference", {"unloaded_confirmed": True})

    def _stop_inference(self):
        if self.frozen_sample or (self.state.get("validation") or {}).get("pending_sample"):
            return self._error("请先保存本次判断或重新判断，再结束推理。")
        self.test_unloaded.setChecked(False)
        self._send("analysis/stop", {})

    def _spectral(self):
        analysis = self.state.get("analysis") or {}
        return analysis.get("model_kind") == "spectral_gru" or (analysis.get("prediction") or {}).get("schema") == "spectral_prediction_v1"

    def _prediction_ready(self):
        analysis = self.state.get("analysis") or {}
        reference = analysis.get("test_reference") or {}
        prediction = analysis.get("prediction") or {}
        return not self._stale() and self.state.get("running") is True and not (self.state.get("experiment") or {}).get("running") and analysis.get("inference_active") is True and bool(analysis.get("model_id")) and reference.get("ready") is True and reference.get("pending") is not True and prediction.get("stale") is not True

    def _live_comparison_ready(self):
        return self._prediction_ready() and (self.state.get("analysis", {}).get("prediction") or {}).get("comparison_ready") is True

    def _select_target(self, point):
        if self.frozen_sample or self._busy():
            return
        self.target_point = point
        self.position_correct.setChecked(False)
        self.force_correct.setChecked(False)
        self._render_inference()
        self._refresh_controls()
        self._notice(f"目标已选为 {point}；请实际按压对应位置。目标不输入模型。")

    def _read_tolerances(self, strict=True):
        result = []
        for axis, entry in zip(AXES, self.tolerances):
            if not entry.text().strip():
                result.append(None)
                continue
            try:
                value = float(entry.text())
                if not math.isfinite(value) or value <= 0:
                    raise ValueError
            except ValueError:
                if strict:
                    raise ValueError(axis + " 容差须为正的有限数值，或留空。")
                value = None
            result.append(value)
        return result

    def _freeze(self):
        if not self.target_point or self.target_point not in POINT_IDS:
            return self._error("请先选择实际按压目标。")
        if self.frozen_sample:
            return self._error("请先保存当前判断或重新判断。")
        if not self._live_comparison_ready():
            return self._error("没有可核验的真实同步按压对照；请完成参考并实际按压。")
        payload = {"actual_point_id": self.target_point}
        if self._spectral():
            if self.validation_mode.currentData() != "judge":
                return self._error("请明确选择人工六轴验证模式。")
            try:
                payload["wrench_tolerances"] = self._read_tolerances()
            except ValueError as exc:
                return self._error(str(exc))
        self._send("validation/freeze", payload)

    def _force_judgment_allowed(self, sample):
        if not sample or sample.get("inference_valid") is False:
            return False
        if sample.get("schema") == "spectral_manual_sample_v1" or "wrench_tolerances" in sample:
            errors = sample.get("wrench_error", (sample.get("prediction") or {}).get("wrench_error"))
            tolerances = sample.get("wrench_tolerances")
            return isinstance(errors, list) and len(errors) == 6 and isinstance(tolerances, list) and len(tolerances) == 6 and all(finite(e) and finite(t) and t > 0 and abs(e) <= t + 1e-12 for e, t in zip(errors, tolerances))
        error = sample.get("absolute_error_N")
        return finite(error) and abs(error) <= .3 + 1e-12

    def _judge(self):
        sample = self.frozen_sample
        if not sample or sample.get("valid") is not True or sample.get("model_id") != (self.state.get("analysis") or {}).get("model_id"):
            return self._error("请先冻结当前模型的真实按压对照。")
        target = sample.get("target_point_id", sample.get("actual_point_id"))
        if not target or target != self.target_point:
            return self._error("目标与冻结证据不一致，请重新判断。")
        predicted = (sample.get("prediction") or {}).get("point_id")
        if self.position_correct.isChecked() and (sample.get("inference_valid") is False or predicted != target):
            return self._error("冻结识别位置与目标不同，不能标记位置正确。")
        if self.force_correct.isChecked() and not self._force_judgment_allowed(sample):
            return self._error("冻结力误差未通过容差，不能标记力正确。")
        self._send("validation/judge", {"sample_id": sample["sample_id"], "position_correct": self.position_correct.isChecked(), "force_correct": self.force_correct.isChecked(), "actual_point_id": target})

    def _discard(self):
        if self.frozen_sample:
            self._send("validation/discard", {"sample_id": self.frozen_sample["sample_id"]})

    def _render_inference(self):
        analysis = self.state.get("analysis") or {}
        validation = self.state.get("validation") or {}
        prediction = analysis.get("prediction") or {}
        reference = analysis.get("test_reference") or {}
        active = analysis.get("inference_active") is True
        self.inference_status.setText("当前模型：" + str(analysis.get("model_id") or "尚未载入") + ("；推理已结束，须重新载入并建立参考" if analysis.get("model_id") and not active else "；推理已启用" if active else "") + "\n本次无接触参考：" + ("已就绪" if reference.get("ready") else "正在建立" if reference.get("pending") else "未就绪") + (" · " + str(reference["reason"]) if reference.get("reason") else ""))
        definition = analysis.get("model_definition") or {}
        trained = analysis.get("trained_point_ids") or []
        self.model_definition.setText("已训练点位：" + ("、".join(trained) if trained else "等待模型定义") + "\n输入：" + ("9 路固定 FBG 波长漂移 / pm" if analysis.get("input_kind") == "delta_lambda_9" or definition.get("input_kind") == "delta_lambda_9" else "512 点真实全谱" if self._spectral() else "固定九峰漂移") + "；模型定义、峰算法、参考与校准沿用现有服务核验。")
        geometry = definition.get("coordinate_definition") or analysis.get("model_coordinate_definition") or analysis.get("coordinate_definition")
        grid = definition.get("point_grid") or analysis.get("model_point_grid") or analysis.get("point_grid")
        if not grid and isinstance(analysis.get("grid"), dict):
            grid = analysis["grid"].get("point_grid")
            geometry = geometry or analysis["grid"].get("coordinate_definition")
        settings = {"point_grid": grid, "coordinate_definition": geometry} if grid or geometry else {}
        ready = self._prediction_ready()
        prediction_valid = prediction.get("valid") is True if self._spectral() else prediction.get("status") == "有效估计" and prediction.get("valid") is not False
        recognized = prediction.get("point_id") if ready and prediction_valid and prediction.get("point_id") in trained and (not self._spectral() or prediction.get("contact") is True) else None
        current, known = self.validation_grid.render(settings, bool(analysis.get("model_id")), self.target_point, recognized, enabled=not self._busy() and not bool(self.frozen_sample), trained=trained)
        self.validation_axes.setText("原点左下 P01；+x 向右，+y 向上至指尖" if current else "历史模型按记录点阵排列" if known else "模型方向未记录；编号排列不声明空间方向")
        matching = bool(self.target_point and recognized == self.target_point)
        source = self.frozen_sample.get("prediction") if self.frozen_sample else prediction
        source = source or {}
        self.prediction_status.setText("目标：" + str(self.target_point or "尚未选择") + "；模型识别：" + str(recognized or "—") + "；" + ("位置一致" if matching else "位置不同" if recognized and self.target_point else str(prediction.get("status") or "等待推理")) + ("\n预测力与 PX6D 对照仅在识别位置与目标一致时显示。" if not matching and not self.frozen_sample else ""))
        self.prediction_reason.setText(("状态已过期，等待新鲜设备数据。" if self._stale() else "") + str(prediction.get("reason") or ""))
        frozen_valid = bool(self.frozen_sample and self.frozen_sample.get("valid") is True)
        predicted_show = matching and ready or frozen_valid
        comparison_show = frozen_valid or predicted_show and prediction.get("comparison_ready") is True
        spectral = self._spectral() or bool(self.frozen_sample and self.frozen_sample.get("schema") == "spectral_manual_sample_v1")
        if spectral:
            predicted, truth = source.get("wrench_pred"), source.get("wrench_truth")
            errors = source.get("wrench_error")
            if isinstance(predicted, list) and isinstance(truth, list) and len(predicted) == len(truth) == 6:
                errors = [p-t if finite(p) and finite(t) else None for p, t in zip(predicted, truth)]
            rows = [[f"{axis} / {unit}", fmt(predicted[i] if predicted_show and isinstance(predicted, list) and len(predicted) == 6 else None, 3), fmt(truth[i] if comparison_show and isinstance(truth, list) and len(truth) == 6 else None, 3), fmt(errors[i] if comparison_show and isinstance(errors, list) and len(errors) == 6 else None, 3)] for i, (axis, unit) in enumerate(zip(AXES[:3], UNITS[:3]))]
        else:
            force, truth = source.get("force_N"), source.get("truth_force_N")
            rows = [["法向按压力 / N", fmt(force if predicted_show else None), fmt(truth if comparison_show else None), fmt(force-truth if comparison_show and finite(force) and finite(truth) else None)]]
        self.wrench_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                self.wrench_table.setItem(r, c, QTableWidgetItem(value))
        if self.frozen_sample:
            sample = self.frozen_sample
            self.frozen_status.setText("冻结编号：" + str(sample.get("sample_id")) + "\n目标：" + str(sample.get("target_point_id") or sample.get("actual_point_id")) + "；识别：" + str((sample.get("prediction") or {}).get("point_id") or "拒识") + "\n读数与容差已固定；人工勾选后保存，或重新判断。")
        else:
            self.frozen_status.setText("尚未冻结；先选目标、实际按压，再冻结当前服务端真实证据。")
        stats = validation.get("session_stats") or validation.get("stats") or {}
        total = stats.get("judged_total", 0)
        self.validation_stats.setText(f"本轮已保存判断 {total} 次；位置正确 {stats.get('position_correct', 0)} / {total}；力正确 {stats.get('force_correct', 0)} / {total}；无效 {stats.get('invalid_count', 0)} 次" + ("\n正确率仅来自实际按压后保存的人工判断。" if total else ""))
        report = validation.get("latest_report") or {}
        self.validation_report.setText("已保存报告：" + str(report.get("path") or report.get("report_path") or report.get("report_id") or "") if report else "")

    def _refresh_controls(self, *_args):
        if not hasattr(self, "finish_validation_button"):
            return
        busy, stale = self._busy(), self._stale()
        gates = capture_gates(self.state, self.pending_point, busy or stale)
        experiment = self.state.get("experiment") or {}
        analysis = self.state.get("analysis") or {}
        training = analysis.get("training") or {}
        running = experiment.get("running") is True
        for button, key in [(self.start_button, "start"), (self.stop_button, "stop"), (self.confirm_point_button, "point_confirm"), (self.trial_start_button, "trial_start"), (self.trial_finish_button, "trial_finish"), (self.trial_abort_button, "trial_abort")]:
            button.setEnabled(gates[key])
        self.baseline_button.setEnabled(gates["baseline"] and self.unloaded_confirmed.isChecked())
        self.unloaded_confirmed.setEnabled(gates["baseline"])
        self.capture_grid.render(experiment.get("settings"), bool(experiment.get("session_id")), self.pending_point,
                                 counts=(experiment.get("guided") or {}).get("accepted_by_point"), enabled=gates["point"],
                                 capture_states=capture_point_states(self.state, busy or stale))
        self._render_capture_guidance()
        target_protocol = self.protocol.currentData() == "force_target_wrench_v1"
        self.no_contact_button.setEnabled(gates["point"] and target_protocol)
        self.trial_finish_button.setVisible(target_protocol)
        finish_reason = self._finish_control_reason()
        self.trial_finish_button.setToolTip(finish_reason)
        if target_protocol and experiment.get("trial_id") and experiment.get("phase") == "release":
            self.preload_status.setText(finish_reason)
        for widget in [self.finger_id, self.capture_mode, self.protocol, self.target_force, self.max_force, self.repetitions]:
            widget.setEnabled(not busy and not running)
        self.force_sign.setEnabled(not busy and not running and not target_protocol)
        rate = self._actual_sample_rate()
        self.capture_note.setText(("仅保存九路峰心、漂移和标签；原谱用于实时寻峰，不保存。" if self.capture_mode.currentData() == "peak_drift" else "连续保存真实 512 点原谱与六轴参考。") + (f" 当前光谱请求：{fmt(rate, 1)} Hz；六维协议要求 25 Hz。先记录 2 秒无载基线，服务端提示后再按压。" if target_protocol else " 按所选法向方向记录，每轮重新确认无载并标定参考。") + ("\n状态已过期，操作停用；等待新鲜服务状态。" if stale else ""))
        method = self.training_method.currentData()
        sequence, finetune = method in SEQUENCE_METHODS, self.training_mode.currentData() == "finetune"
        analysis_busy = busy or self._training_busy()
        selected_valid = bool(self.selected_sessions) and all(training_status(next((r for r in self.sessions if r.get("session_id") == sid), None), method)[0] for sid in self.selected_sessions)
        self.training_start_button.setEnabled(not analysis_busy and not stale and (not running or sequence) and not self.show_deleted_sessions.isChecked() and selected_valid and (not finetune or bool(self._selected_base_model())))
        self.training_method.setEnabled(not analysis_busy)
        self.training_mode.setEnabled(sequence and not analysis_busy and not stale)
        self.base_model.setEnabled(sequence and finetune and not analysis_busy and not stale)
        self.training_parameters.setVisible(sequence)
        self.training_parameters.setEnabled(not analysis_busy)
        self.cumulative.setVisible(sequence)
        self.cumulative.setEnabled(not analysis_busy and not finetune and not self.show_deleted_sessions.isChecked())
        self.adapt_legacy.setVisible(sequence)
        self.adapt_legacy.setEnabled(not analysis_busy and not finetune and method != "drift_gru")
        if method == "drift_gru":
            self.method_note.setText("9 路波长漂移 / pm → 16 帧因果窗口 → 位置与有符号六维力。固定 FBG 身份与真实 2 秒无载九峰基线；缺峰或时间间断不训练。")
        elif sequence:
            self.method_note.setText("512 点真实原始全谱 → 16 帧因果窗口 → 位置与有符号六维力。须有可信无载 S0，来源不兼容时明确拒绝。")
        else:
            self.method_note.setText("合并兼容 A / B 的合格整轮，每个训练点至少 3 轮；C 批保留。六维协议不使用旧法向模型。")
        pause_reasons = training.get("pause_reasons") or []
        status = training.get("status")
        sequence_training = training.get("method") in SEQUENCE_METHODS
        self.training_pause_button.setEnabled(not busy and not stale and sequence_training and status in {"running", "pause_requested", "paused"} and "user" not in pause_reasons)
        self.training_resume_button.setEnabled(not busy and not stale and sequence_training and status in {"pause_requested", "paused"} and "user" in pause_reasons)
        self.training_cancel_button.setEnabled(not busy and not stale and sequence_training and self._training_busy() and status != "cancelling")
        self.go_inference_button.setEnabled(not busy and status == "completed" and training.get("progress_percent") == 100 and bool(training.get("model_id")))
        history_blocked = self._mutation_blocked()
        self.delete_sessions_button.setEnabled(not history_blocked and bool(self.selected_sessions) and not self.show_deleted_sessions.isChecked())
        self.restore_sessions_button.setEnabled(not history_blocked and bool(self.selected_sessions) and self.show_deleted_sessions.isChecked())
        self.select_trainable_button.setEnabled(not busy and not self.show_deleted_sessions.isChecked())
        self.clear_sessions_button.setEnabled(not busy)
        self.show_deleted_sessions.setEnabled(not busy)
        self.session_table.setEnabled(not busy)
        self.refresh_sessions_button.setEnabled("history/sessions" not in self._pending)
        self.open_session_button.setEnabled(not busy and self.session_table.rowCount() > 0)
        selected_model = next((m for m in self.models if m.get("model_id") == self.model_select.currentData()), None)
        selected_spectral = bool(selected_model and (selected_model.get("model_kind") == "spectral_gru" or self._model_method(selected_model) in SEQUENCE_METHODS))
        gap = running and (experiment.get("settings") or {}).get("acquisition_protocol") in WRENCH_PROTOCOLS and not experiment.get("trial_id") and (experiment.get("guided") or {}).get("step") not in ACTIVE_STEPS
        pending = bool(self.frozen_sample or (self.state.get("validation") or {}).get("pending_sample"))
        self.load_model_button.setEnabled(not analysis_busy and not stale and (not running or gap and selected_spectral) and not pending and bool(selected_model) and selected_model.get("loadable") is not False and (not selected_spectral or self.test_unloaded.isChecked()))
        self.test_unloaded.setEnabled(not busy and not stale and self.state.get("running") is True and (not running or gap and selected_spectral))
        self.reference_button.setEnabled(not busy and not stale and self.state.get("running") is True and not running and analysis.get("inference_active") is True and bool(analysis.get("model_id")) and self.test_unloaded.isChecked())
        self.inference_stop_button.setEnabled(not busy and not stale and not running and not pending and analysis.get("inference_active") is True and bool(analysis.get("model_id")))
        self.refresh_models_button.setEnabled("analysis/models" not in self._pending)
        self.refresh_managed_models_button.setEnabled("analysis/models" not in self._pending)
        self.model_select.setEnabled(not busy and not pending)
        models_blocked = self._mutation_blocked(True)
        selected_manage = [next((m for m in self._model_rows() if m.get("model_id") == mid), None) for mid in self.selected_models]
        can_manage = bool(selected_manage) and all(not self._model_block_reason(m) for m in selected_manage)
        self.delete_models_button.setEnabled(not models_blocked and can_manage and not self.show_deleted_models.isChecked())
        self.restore_models_button.setEnabled(not models_blocked and can_manage and self.show_deleted_models.isChecked())
        self.select_models_button.setEnabled(not models_blocked)
        self.clear_models_button.setEnabled(not busy)
        self.show_deleted_models.setEnabled(not busy)
        self.model_table.setEnabled(not models_blocked)
        spectral = self._spectral()
        self.validation_mode.setVisible(spectral)
        self.validation_mode.setEnabled(not busy and not pending)
        for entry in self.tolerances:
            entry.setEnabled(spectral and not busy and not pending)
        self.force_correct.setText("六维力正确：六轴均在各自容差内" if spectral else "力正确：绝对差值 ≤ 0.3 N")
        judge_mode = not spectral or self.validation_mode.currentData() == "judge"
        self.freeze_button.setEnabled(not busy and not stale and not running and not pending and bool(self.target_point) and judge_mode and self._live_comparison_ready())
        sample = self.frozen_sample or {}
        can_judge = not busy and not running and bool(sample) and sample.get("valid") is True and sample.get("model_id") == analysis.get("model_id") and bool(sample.get("target_point_id") or sample.get("actual_point_id"))
        can_position = can_judge and sample.get("inference_valid") is not False and (sample.get("prediction") or {}).get("point_id") == self.target_point
        can_force = can_judge and self._force_judgment_allowed(sample)
        self.position_correct.setEnabled(can_position)
        self.force_correct.setEnabled(can_force)
        if not can_position:
            self.position_correct.setChecked(False)
        if not can_force:
            self.force_correct.setChecked(False)
        self.save_judgment_button.setEnabled(can_judge)
        self.discard_judgment_button.setEnabled(not busy and bool(sample))
        stats = (self.state.get("validation") or {}).get("session_stats") or (self.state.get("validation") or {}).get("stats") or {}
        self.finish_validation_button.setEnabled(not busy and not running and not pending and bool(analysis.get("model_id")) and stats.get("judged_total", 0) > 0)
        for button in self.validation_grid.buttons.values():
            button.setEnabled(not busy and not pending)
        if self._capture_force_valid() != getattr(self, "_force_display_valid", False):
            # A held snapshot may remain usable for controls while its much
            # faster six-axis display freshness limit has already elapsed.
            # Expiring that display never fabricates another plot sample.
            self._render_capture(record_force=False)
        if stale:
            self.unloaded_confirmed.setChecked(False)
            self.test_unloaded.setChecked(False)
            if not self._display_stale:
                self._display_stale = True
                self._render_capture(record_force=False)
                self._render_inference()

