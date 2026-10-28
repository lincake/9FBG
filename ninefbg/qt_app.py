"""9FBG native PyQt6 entry point. Startup never connects an instrument."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from .qt_bootstrap import prepare_optional_torch

prepare_optional_torch()

from PyQt6.QtCore import QLocale, QTimer, QTranslator, QLibraryInfo, Qt
from PyQt6.QtGui import QCloseEvent, QFont, QFontDatabase, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLayout, QMainWindow, QPushButton, QSizePolicy, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget, QScrollArea)

from . import PRODUCT, VERSION
from .qt_context import AppContext
from .qt_theme import STYLE, apply_theme, ui_font
from .qt_input import install_input_wheel_guard
from .service import AcquisitionService

ROOT = Path(__file__).resolve().parents[1]

def instance_name(userdata):
    key = os.path.normcase(str(Path(userdata).resolve())).encode("utf-8")
    return "9FBG-desktop-" + hashlib.sha256(key).hexdigest()[:24]


def activate_existing(name):
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(350):
        return False
    socket.write(b"activate")
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


class MainWindow(QMainWindow):
    def __init__(self, context):
        super().__init__()
        from .qt_display import LivePage
        from .qt_experiment import ExperimentWorkspace
        from .qt_pulse import PulsePage
        self.ctx = context
        context.window = self
        self._closed = False
        self._busy = False
        self.setWindowTitle(f"{PRODUCT} {VERSION}")
        self.setWindowIcon(QIcon(str(ROOT / "assets/9FBG.ico")))
        self.resize(1440, 960)
        self.setMinimumSize(1040, 700)
        container = QWidget()
        outer = QHBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFixedWidth(252)
        sidebar_scroll.setWidget(sidebar)
        side = QVBoxLayout(sidebar)
        side.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        side.setContentsMargins(20, 24, 20, 18)
        side.setSpacing(9)
        brand = QLabel(PRODUCT)
        brand.setObjectName("brand")
        side.addWidget(brand)
        subtitle = QLabel(f"{VERSION} · 源码开发版\n九光栅采集与分析")
        subtitle.setObjectName("subtitle")
        side.addWidget(subtitle)
        side.addSpacing(18)
        connection_heading = QLabel("设备连接")
        connection_heading.setProperty("role", "sectionLabel")
        side.addWidget(connection_heading)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        prefs = context.preferences.get("connection", {})
        self.exposure = QSpinBox()
        self.exposure.setObjectName("integrationUs")
        self.exposure.setRange(20, 10_000_000)
        self.exposure.setValue(prefs.get("integration_us", 20))
        self.exposure.setMinimumWidth(80)
        self.exposure.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.rate = QSpinBox()
        self.rate.setObjectName("sampleRateHz")
        self.rate.setRange(1, 200)
        self.rate.setValue(prefs.get("sample_rate_hz", 25))
        self.rate.setMinimumWidth(80)
        self.rate.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        form.addRow("曝光 / μs", self.exposure)
        form.addRow("采样请求 / Hz", self.rate)
        side.addLayout(form)
        rate_note = QLabel("实际接收率以实时读数为准。\n请求须对应整数毫秒间隔。")
        rate_note.setWordWrap(True)
        rate_note.setObjectName("subtitle")
        side.addWidget(rate_note)
        self.connect_button = self._button(side, "连接光谱", self.connect_optical, "connectBtn")
        self.connect_button.setProperty("role", "primary")
        self.force_connect = self._button(side, "连接 PX6D", lambda: self.ctx.execute("force/connect"), "px6dConnectBtn")
        self.force_disconnect = self._button(side, "断开 PX6D", lambda: self.ctx.execute("force/disconnect"), "px6dDisconnectBtn")
        self.disconnect_button = self._button(side, "受控断开设备", lambda: self.ctx.execute("disconnect"), "disconnectBtn")
        side.addSpacing(14)
        self.unloaded = QCheckBox("已移开接触物，确认无载")
        self.unloaded.setObjectName("unloadedConfirmed")
        side.addWidget(self.unloaded)
        self.reference_button = self._button(side, "建立九峰参考", self.request_reference, "referenceBtn")
        self.unloaded.toggled.connect(self.update_controls)
        side.addStretch()
        self.device_status = QLabel("设备未连接\n等待手动连接")
        self.device_status.setWordWrap(True)
        side.addWidget(self.device_status)
        footer = QLabel("512 点实际标定轴\n固定 FBG 身份 · 本地处理\n显示小数位不代表测量精度")
        footer.setObjectName("subtitle")
        footer.setWordWrap(True)
        side.addWidget(footer)
        outer.addWidget(sidebar_scroll)
        body = QVBoxLayout()
        body.setContentsMargins(24, 20, 24, 14)
        body.setSpacing(14)
        heading_row = QHBoxLayout()
        heading_column = QVBoxLayout()
        heading_column.setSpacing(4)
        heading = QLabel("光纤触觉工作台")
        heading.setObjectName("pageHeading")
        heading_column.addWidget(heading)
        subheading = QLabel("实时采集与实验分析")
        subheading.setObjectName("pageSubheading")
        heading_column.addWidget(subheading)
        heading_row.addLayout(heading_column)
        heading_row.addStretch(1)
        version_badge = QLabel(f"9FBG {VERSION}")
        version_badge.setObjectName("versionBadge")
        heading_row.addWidget(version_badge, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(heading_row)
        self.notice = QLabel("设备等待手动连接；连接光谱后自动建立九峰参考。")
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(self.notice)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("workspaceTabs")
        self.live_page = LivePage(context)
        self.experiment_page = ExperimentWorkspace(context)
        self.pulse_page = PulsePage(context)
        self._style_actions()
        for page, title in ((self.live_page, "实时光谱与六轴力"),
                            (self.experiment_page, "采集 · 训练 · 推理"),
                            (self.pulse_page, "脉搏观察")):
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(page)
            self.tabs.addTab(area, title)
        self.tabs.currentChanged.connect(self.tab_changed)
        if hasattr(self.pulse_page, "freeze_others"):
            self.pulse_page.freeze_others.toggled.connect(self.tab_changed)
        body.addWidget(self.tabs, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(container)
        self.statusBar().showMessage(f"{PRODUCT} {VERSION} · 原生桌面界面 · 未连接设备")
        self.ctx.snapshot_ready.connect(self.update_state)
        self.ctx.error.connect(self.show_error)
        self.ctx.busy_changed.connect(self.set_busy)
        self.ctx.shutdown_finished.connect(self.finish_close)
        self.ctx.action_finished.connect(self.action_finished)
        self.update_controls()

    def _button(self, layout, text, callback, name):
        button = QPushButton(text)
        button.setObjectName(name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _style_actions(self):
        """Give native actions a consistent visual hierarchy without changing commands."""
        roles = ((self, "danger", ("disconnect_button", "force_disconnect")),
                 (self.experiment_page, "primary", ("start_button", "trial_start_button", "trial_finish_button", "training_start_button", "load_model_button", "freeze_button", "save_judgment_button")),
                 (self.experiment_page, "danger", ("trial_abort_button", "training_cancel_button", "delete_sessions_button", "delete_models_button")),
                 (self.experiment_page, "ghost", ("refresh_sessions_button", "clear_sessions_button", "refresh_models_button", "refresh_managed_models_button", "clear_models_button")),
                 (self.pulse_page, "primary", ("noise_button",)),
                 (self.pulse_page, "ghost", ("reset_button",)))
        for page, role, names in roles:
            for name in names:
                button = getattr(page, name, None)
                if button is not None:
                    button.setProperty("role", role)
        for page in (self.experiment_page, self.pulse_page, self.live_page):
            for button in page.findChildren(QPushButton):
                button.setCursor(Qt.CursorShape.PointingHandCursor)

    def connect_optical(self):
        values = {"source_mode": "hardware", "integration_us": self.exposure.value(), "sample_rate_hz": self.rate.value()}
        from .service import optical_request_settings
        try:
            optical_request_settings(values["integration_us"], values["sample_rate_hz"])
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.ctx.execute("settings", {"section": "connection", "values": values})
        self.ctx.execute("connect", {"mode": "hardware", "record": False, "connect_force": False, "auto_reference": True,
                                    "integration_us": values["integration_us"], "sample_rate_hz": values["sample_rate_hz"]})

    def request_reference(self):
        if not self.unloaded.isChecked():
            self.show_error("请先移开接触物，并明确确认当前无载")
            return
        self.unloaded.setChecked(False)
        self.ctx.execute("reference", {"unloaded_confirmed": True})

    def action_finished(self, action, result):
        if action == "connect" and isinstance(result, dict) and result.get("stopped"):
            self.show_error(str(result.get("error") or "光谱采集已停止；请先受控断开设备，再重新连接。"))
            return
        if action == "connect" and isinstance(result, dict) and result.get("running"):
            tracker = result.get("tracker") or {}
            self.notice.setText("光谱已连接，九峰参考已自动建立。" if tracker.get("reference_version") and len(tracker.get("reference_nm") or []) == 9 and not tracker.get("reference_pending") else "光谱已连接；九峰稳定后自动建立参考，进度见左侧状态。")
            self._notice_role("")
            return
        if action not in {"state", "pulse_samples", "pulse/samples", "settings", "history/sessions", "analysis/models"}:
            self.notice.setText("操作已完成；请按当前状态继续。")
            self._notice_role("")

    def _notice_role(self, role):
        self.notice.setProperty("role", role)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)
        self.notice.update()

    def show_error(self, message):
        self._notice_role("error")
        self.notice.setText(message)
        self.statusBar().showMessage(message)
        if not self.ctx.closing:
            self.setEnabled(True)

    def set_busy(self, busy):
        self._busy = busy
        self.update_controls()

    def update_controls(self, *_):
        state = self.ctx.state
        running = state.get("running", False)
        stopping = state.get("stopping", False)
        acquiring = running and not state.get("stopped", False)
        experiment = state.get("experiment", {}).get("running", False)
        blocked = stopping or self._busy or self.ctx.closing
        for widget in (self.exposure, self.rate, self.connect_button):
            widget.setEnabled(not running and not blocked)
        self.force_connect.setEnabled(acquiring and not state.get("force_connected") and not state.get("force_connecting") and not blocked)
        self.force_disconnect.setEnabled(running and (state.get("force_connected") or state.get("force_connecting")) and not blocked)
        self.disconnect_button.setEnabled((running or stopping) and not self._busy and not self.ctx.closing)
        self.reference_button.setEnabled(acquiring and not experiment and self.unloaded.isChecked() and not blocked)

    def update_state(self, state):
        priority = self.tabs.currentIndex() == 2 and getattr(self.pulse_page, "freeze_others", None) and self.pulse_page.freeze_others.isChecked()
        if not priority:
            self.live_page.update_state(state)
            self.experiment_page.update_state(state)
        if hasattr(self.pulse_page, "update_state"):
            self.pulse_page.update_state(state)
        self.update_controls()
        tracker = state.get("tracker") or {}
        unavailable = state.get("stopped") or tracker.get("display_stale")
        status = ("正在受控关闭" if state.get("stopping") else "设备未连接" if not state.get("running") else
                  "光谱采集已停止" if state.get("stopped") else
                  "光谱数据已过期" if tracker.get("display_stale") else "光谱已连接")
        reference_status = ("九峰参考暂不可用" if unavailable else
                            "正在建立九峰参考" if tracker.get("reference_pending") else
                            "九峰参考已就绪" if tracker.get("reference_version") and len(tracker.get("reference_nm") or []) == 9 else "等待有效九峰参考")
        self.device_status.setText(status + ("\n" + reference_status if state.get("running") else "") + ("\nPX6D 已连接" if state.get("force_connected") else "\nPX6D 未连接"))
        self.device_status.setToolTip(str(tracker.get("message") or ""))
        self.statusBar().showMessage(f"{PRODUCT} {VERSION} · {status}")
        if state.get("error"):
            self.show_error(state["error"])

    def tab_changed(self, *_):
        priority = self.tabs.currentIndex() == 2 and getattr(self.pulse_page, "freeze_others", None) and self.pulse_page.freeze_others.isChecked()
        self.ctx.timer.setInterval(1000 if priority else 160)
        if not priority and self.ctx.state:
            self.update_state(self.ctx.state)
            self.ctx.refresh()

    def closeEvent(self, event: QCloseEvent):
        if self._closed:
            event.accept()
            return
        event.ignore()
        self.experiment_page._save_preferences()
        self.notice.setText("正在受控断开设备、排空记录并关闭后台任务，请稍候…")
        self.setEnabled(False)
        self.ctx.shutdown()

    def finish_close(self):
        if hasattr(self.pulse_page, "shutdown"):
            self.pulse_page.shutdown()
        self._closed = True
        self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="9FBG 原生 PyQt6 桌面工作台；启动不连接硬件")
    parser.add_argument("--userdata", type=Path, default=ROOT / "userdata")
    parser.add_argument("--smoke-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--quit-after-ms", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setApplicationName(PRODUCT)
    app.setApplicationVersion(VERSION)
    app.setOrganizationName(PRODUCT)
    # Qt's offscreen Windows plugin can start without a system font database.
    # Explicitly load the installed Chinese font for meaningful rendered QA.
    if os.name == "nt" and not QFontDatabase.families():
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
    apply_theme(app)
    QLocale.setDefault(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
    translator = QTranslator(app)
    translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    app.installTranslator(translator)
    name = instance_name(args.userdata)
    if activate_existing(name):
        print(f"{PRODUCT} {VERSION} 已激活现有桌面窗口；设备状态保持。", flush=True)
        return 0
    instance = QLocalServer(app)
    if not instance.listen(name):
        if activate_existing(name):
            return 0
        print("9FBG 桌面实例正在启动或退出，请稍后重试。", file=sys.stderr, flush=True)
        return 1
    service = AcquisitionService(ROOT, args.userdata.resolve())
    ctx = AppContext(service)
    input_wheel_guard = install_input_wheel_guard(app)
    try:
        window = MainWindow(ctx)
        def activate():
            while instance.hasPendingConnections():
                socket = instance.nextPendingConnection()
                socket.readyRead.connect(socket.readAll)
                socket.disconnected.connect(socket.deleteLater)
                window.showNormal()
                window.raise_()
                window.activateWindow()
        instance.newConnection.connect(activate)
        window.show()
        print(f"{PRODUCT} {VERSION} 原生 PyQt6 已启动；设备等待手动连接。", flush=True)
        if args.smoke_test:
            def smoke():
                if not ctx.state:
                    QTimer.singleShot(100, smoke)
                    return
                destination = args.smoke_test.resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                snapshots = []
                for index in range(window.tabs.count()):
                    window.tabs.setCurrentIndex(index)
                    app.processEvents()
                    path = destination.with_name(destination.stem + f"-page-{index}.png")
                    window.grab().save(str(path))
                    snapshots.append(str(path))
                extra_snapshots = []
                window.tabs.setCurrentIndex(1)
                for index in range(1, window.experiment_page.tabs.count()):
                    window.experiment_page.tabs.setCurrentIndex(index)
                    app.processEvents()
                    path = destination.with_name(destination.stem + f"-experiment-{index}.png")
                    window.grab().save(str(path))
                    extra_snapshots.append(str(path))
                report = {"product": PRODUCT, "version": VERSION, "gui": "PyQt6", "state": ctx.state,
                          "screenshots": snapshots, "additional_screenshots": extra_snapshots,
                          "window_title": window.windowTitle(), "http_server": False}
                destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                window.close()
            QTimer.singleShot(650, smoke)
        elif args.quit_after_ms > 0:
            QTimer.singleShot(args.quit_after_ms, window.close)
        return app.exec()
    finally:
        # Stop Python event callbacks before Qt tears down the window and app.
        app.removeEventFilter(input_wheel_guard)
        window = getattr(ctx, "window", None)
        if window is not None and hasattr(window, "pulse_page"):
            window.pulse_page.shutdown()
        if not ctx.closing:
            ctx.shutdown()
        ctx.join()
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
