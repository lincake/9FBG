from pathlib import Path
import time
from unittest.mock import Mock

import pytest
from PyQt6.QtWidgets import QApplication

from ninefbg.qt_context import AppContext, dispatch
from ninefbg.service import AcquisitionService


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


def test_native_connection_preserves_explicit_opt_in_and_default_display_mode():
    service = Mock()
    dispatch(service, "connect", {"mode": "hardware", "integration_us": 20, "sample_rate_hz": 25})
    service.connect.assert_called_once_with("hardware", None, integration_us=20, sample_rate_hz=25,
                                             record=False, connect_force=False)


@pytest.mark.parametrize("auto_reference", [True, False])
def test_native_connection_forwards_explicit_automatic_reference(auto_reference):
    service = Mock()
    dispatch(service, "connect", {"auto_reference": auto_reference})
    service.connect.assert_called_once_with("hardware", None, auto_reference=auto_reference,
                                             record=False, connect_force=False)


@pytest.mark.parametrize("values", [{"record": 1}, {"connect_force": "true"}, {"sample_rate_hz": 30}, {"integration_us": 19},
                                    {"auto_reference": 1}, {"auto_reference": "true"}, {"auto_reference": None}])
def test_native_invalid_connection_never_reaches_device(values):
    service = Mock()
    with pytest.raises(ValueError):
        dispatch(service, "connect", values)
    service.connect.assert_not_called()


def test_native_reference_does_not_coerce_unloaded_confirmation():
    service = Mock()
    dispatch(service, "reference", {"unloaded_confirmed": "true"})
    service.reference.assert_called_once_with(False)
    service.connect.assert_not_called()


def test_context_startup_reads_only_and_shutdown_without_userdata_writes(app, tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "data")
    service.connect = Mock(side_effect=AssertionError("不能自动连接"))
    context = AppContext(service)
    done = []
    context.shutdown_finished.connect(lambda: done.append(True))
    try:
        wait_for(app, lambda: bool(context.state))
        assert context.state["running"] is False and context.state["devices"] == []
        service.connect.assert_not_called()
        assert not service.userdata.exists()
    finally:
        context.shutdown()
        wait_for(app, lambda: bool(done))
        context.join()


def test_slow_commands_leave_gui_and_small_read_feed_responsive(app, tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    released = __import__("threading").Event()
    entered = __import__("threading").Event()
    def slow():
        entered.set()
        released.wait(3)
        return service.snapshot()
    service.connect_force = slow
    context = AppContext(service)
    feed, done = [], []
    context.pulse_ready.connect(feed.append)
    context.shutdown_finished.connect(lambda: done.append(True))
    try:
        context.execute("force/connect")
        wait_for(app, entered.is_set)
        context.execute("pulse_samples", {"after": 0, "limit": 10})
        wait_for(app, lambda: bool(feed))
        assert not released.is_set()
        assert feed[0]["schema"] == "nine_fbg_pulse_samples_v1"
    finally:
        released.set()
        context.shutdown()
        wait_for(app, lambda: bool(done))
        context.join()


def test_close_discards_queued_device_connect_and_waits_for_controlled_completion(app, tmp_path):
    import threading
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    entered, release = threading.Event(), threading.Event()
    def slow():
        entered.set()
        release.wait(3)
        return service.snapshot()
    service.connect_force = slow
    service.connect = Mock(side_effect=AssertionError("退出后不能开始连接"))
    context = AppContext(service)
    done = []
    context.shutdown_finished.connect(lambda: done.append(True))
    try:
        context.execute("force/connect")
        wait_for(app, entered.is_set)
        context.execute("connect", {"mode": "hardware"})
        context.execute("settings", {"section": "experiment", "values": {"finger_id": "关闭前的明确编辑"}})
        context.shutdown()
        assert context.closing and not context.execute("connect", {"mode": "hardware"})
        assert not done
        release.set()
        wait_for(app, lambda: bool(done))
        service.connect.assert_not_called()
        assert service.preferences.snapshot()["values"]["experiment"]["finger_id"] == "关闭前的明确编辑"
    finally:
        release.set()
        context.join()


def test_validation_finish_preserves_report_result_shape():
    service = Mock()
    service.command_lock = __import__("threading").RLock()
    service.validation.finish.return_value = {"validation": {"judged": 2}, "report": {"path": "报告.json"}}
    assert dispatch(service, "validation/finish") == service.validation.finish.return_value


def test_rejected_close_recovers_busy_state_after_dropping_queued_clicks(app, tmp_path):
    import threading
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    entered, release = threading.Event(), threading.Event()
    def slow():
        entered.set()
        release.wait(3)
        return service.snapshot()
    service.connect_force = slow
    service.connect = Mock()
    service.disconnect = Mock(side_effect=[{"stopping": True, "error": "设备线程尚未退出"},
                                           {"stopping": False}, {"stopping": False}])
    context = AppContext(service)
    errors, done = [], []
    context.error.connect(errors.append)
    context.shutdown_finished.connect(lambda: done.append(True))
    try:
        context.execute("force/connect")
        wait_for(app, entered.is_set)
        for _ in range(3):
            context.execute("connect", {"mode": "hardware"})
        context.shutdown()
        release.set()
        wait_for(app, lambda: bool(errors))
        assert not context.closing and context._busy_count == 0
        service.connect.assert_not_called()
        context.shutdown()
        wait_for(app, lambda: bool(done))
    finally:
        release.set()
        context.join()
