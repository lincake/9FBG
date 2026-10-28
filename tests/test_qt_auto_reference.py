"""Native connection intent uses generated fixtures and never opens hardware."""
from copy import deepcopy
import os
from pathlib import Path
import time
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QPushButton

from ninefbg.qt_app import MainWindow
from ninefbg.qt_context import AppContext
from ninefbg.service import AcquisitionService


class FakeContext(QObject):
    snapshot_ready = pyqtSignal(dict)
    pulse_ready = pyqtSignal(dict)
    action_finished = pyqtSignal(str, object)
    error = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)
    shutdown_finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.calls = []
        self.preferences = {}
        self.state = {"product": "9FBG", "running": False,
                      "force_connected": False, "force_connecting": False}
        self.closing = False
        self.timer = QTimer(self)

    def execute(self, action, payload=None):
        self.calls.append((action, deepcopy(payload)))
        return True

    def refresh(self):
        self.window.update_state(deepcopy(self.state))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def dispose_window(window, app):
    window.live_page.timer.stop()
    window.experiment_page._fresh_timer.stop()
    window.experiment_page._preference_timer.stop()
    window.pulse_page.shutdown()
    window._closed = True
    window.close()
    window.deleteLater()
    app.processEvents()


@pytest.fixture
def window(app):
    context = FakeContext()
    widget = MainWindow(context)
    yield widget, context
    dispose_window(widget, app)


def wait_for(app, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


def test_startup_never_submits_connection_and_press_markers_are_removed(window, app):
    widget, context = window
    app.processEvents()
    assert not any(action in {"connect", "reference", "press"} for action, _ in context.calls)
    assert widget.findChild(QPushButton, "pressStartBtn") is None
    assert widget.findChild(QPushButton, "pressEndBtn") is None
    assert not ({"开始按压标记", "结束按压标记"} &
                {button.text() for button in widget.findChildren(QPushButton)})


def test_manual_connect_requests_automatic_reference_without_unloaded_attestation(window):
    widget, context = window
    widget.exposure.setValue(60)
    widget.rate.setValue(50)
    widget.connect_button.click()
    assert context.calls == [
        ("settings", {"section": "connection", "values": {
            "source_mode": "hardware", "integration_us": 60, "sample_rate_hz": 50}}),
        ("connect", {"mode": "hardware", "record": False, "connect_force": False,
                     "auto_reference": True, "integration_us": 60, "sample_rate_hz": 50}),
    ]
    assert not widget.unloaded.isChecked()
    assert all("unloaded_confirmed" not in payload for _action, payload in context.calls)


def test_invalid_optical_request_never_saves_or_connects(window):
    widget, context = window
    widget.rate.setValue(30)  # 1000 / 30 is not an integer device interval.
    widget.connect_button.click()
    assert not context.calls
    assert widget.notice.text()


@pytest.mark.parametrize("reference", [
    {}, {"reference_pending": True},
    {"reference_pending": False, "reference_version": 1},
])
def test_repeated_state_refreshes_never_queue_connection_or_reference(window, reference):
    widget, context = window
    for running in (False, True, True, False):
        context.state = {"product": "9FBG", "running": running, "tracker": reference,
                         "force_connected": False, "force_connecting": False}
        for _ in range(4):
            context.refresh()
            widget.update_controls()
    assert not any(action in {"connect", "reference", "press"} for action, _ in context.calls)


def test_processed_reference_readiness_uses_current_nine_peak_values(window):
    widget, context = window
    tracker = {"reference_version": 1, "reference_pending": False,
               "reference_nm": [1520. + index * 2 for index in range(9)]}
    assert "initialized" not in tracker  # Processed service snapshots omit this internal flag.
    context.state.update(running=True, tracker=tracker)
    context.refresh()
    widget.action_finished("connect", context.state)
    assert "九峰参考已就绪" in widget.device_status.text()
    assert "九峰参考已自动建立" in widget.notice.text()

    tracker["reference_nm"] = []  # Version provenance remains after current reference invalidates.
    context.refresh()
    widget.action_finished("connect", context.state)
    assert "等待有效九峰参考" in widget.device_status.text()
    assert "已就绪" not in widget.device_status.text()
    assert "已自动建立" not in widget.notice.text()
    assert "九峰稳定后自动建立参考" in widget.notice.text()


def test_manual_rereference_retains_fresh_explicit_unloaded_confirmation(window):
    widget, context = window
    context.state.update(running=True)
    context.refresh()
    assert not widget.reference_button.isEnabled()
    widget.request_reference()
    assert not context.calls
    assert "确认当前无载" in widget.notice.text()
    widget.unloaded.setChecked(True)
    assert widget.reference_button.isEnabled()
    widget.reference_button.click()
    assert context.calls == [("reference", {"unloaded_confirmed": True})]
    assert not widget.unloaded.isChecked()
    assert not widget.reference_button.isEnabled()


@pytest.mark.parametrize("extra_state,optical_status,reference_status,can_reference", [
    ({}, "光谱已连接", "九峰参考已就绪", True),
    ({"stopped": True, "error": "光谱采集失败：测试设备超时"},
     "光谱采集已停止", "九峰参考暂不可用", False),
    ({"tracker_stale": True}, "光谱数据已过期", "九峰参考暂不可用", True),
    ({"force_error": "测试六轴超时", "error": "PX6D 参考不可用：测试六轴超时"},
     "光谱已连接", "九峰参考已就绪", True),
])
def test_sidebar_distinguishes_current_optical_data_from_retained_reference(
        window, extra_state, optical_status, reference_status, can_reference):
    widget, context = window
    extra_state = dict(extra_state)
    tracker = {"reference_version": 1, "reference_pending": False,
               "reference_nm": [1520. + index * 2 for index in range(9)]}
    if extra_state.pop("tracker_stale", False):
        tracker["display_stale"] = True
    context.state.update({"running": True, "stopped": False, "tracker": tracker, **extra_state})
    widget.unloaded.setChecked(True)
    context.refresh()

    assert widget.device_status.text().splitlines()[:2] == [optical_status, reference_status]
    assert widget.reference_button.isEnabled() is can_reference
    assert widget.force_connect.isEnabled() is can_reference
    # A fatal reader can retain a device lease until controlled disconnect finishes.
    assert widget.disconnect_button.isEnabled()
    assert not widget.connect_button.isEnabled()
    assert not widget.exposure.isEnabled()
    assert not widget.rate.isEnabled()
    assert not context.calls


def test_failed_reader_during_connect_never_replaces_error_with_reference_success(window):
    widget, context = window
    error = "光谱采集失败：光谱设备超过 2 秒未返回新有效采集帧"
    context.state.update(running=True, stopped=True, error=error,
                         tracker={"reference_version": 1, "reference_pending": False,
                                  "reference_nm": [1520. + index * 2 for index in range(9)]})
    context.refresh()
    widget.action_finished("connect", context.state)
    assert widget.notice.text() == error
    assert "已自动建立" not in widget.notice.text()
    assert "九峰参考已就绪" not in widget.device_status.text()


def test_failed_connection_cannot_trigger_a_later_reference_command(app, tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "userdata")
    service.connect = Mock(side_effect=ValueError("测试连接失败，未打开设备"))
    service.reference = Mock(side_effect=AssertionError("连接失败后不能建立参考"))
    context = AppContext(service)
    widget = MainWindow(context)
    errors, done = [], []
    context.error.connect(errors.append)
    context.shutdown_finished.connect(lambda: done.append(True))
    try:
        wait_for(app, lambda: bool(context.state))
        service.connect.assert_not_called()
        widget.connect_button.click()
        wait_for(app, lambda: bool(errors) and context._busy_count == 0)
        service.connect.assert_called_once_with("hardware", None, record=False,
            connect_force=False, auto_reference=True, integration_us=20, sample_rate_hz=25)
        for _ in range(4):
            context.refresh()
            app.processEvents()
        service.reference.assert_not_called()
        assert not context.state.get("running")
        assert "测试连接失败" in widget.notice.text()
    finally:
        dispose_window(widget, app)
        context.shutdown()
        wait_for(app, lambda: bool(done))
        context.join()
