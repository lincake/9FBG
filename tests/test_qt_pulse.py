"""Native widget contracts with synthetic sources and no device connections."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QPoint, Qt, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
import pytest

from ninefbg.qt_pulse import PulsePage
from tests.test_pulse_signal_native import frames


class Context(QObject):
    pulse_ready = pyqtSignal(dict)
    snapshot_ready = pyqtSignal(dict)
    action_finished = pyqtSignal(str, object)
    error = pyqtSignal(str)
    closing = False

    def __init__(self):
        super().__init__()
        self.calls = []

    def execute(self, action, payload=None):
        self.calls.append((action, payload))
        return True


def events_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.002)
    assert predicate(), "native pulse worker did not finish"


def batch(samples=None, cursor=100, **changes):
    result = dict(schema="nine_fbg_pulse_samples_v1", cursor=cursor, samples=samples or [],
                  running=True, stopping=False, mode="hardware", blocked=False, reason="", age_ms=5,
                  latest_binding="test-reference", can_initialize=False, reset_required=False, truncated=False)
    result.update(changes)
    return result


@pytest.fixture
def pulse_page():
    app = QApplication.instance() or QApplication([])
    ctx = Context()
    page = PulsePage(ctx)
    assert not ctx.calls, "creating a page must not acquire or connect hardware"
    page.resize(1000, 900)
    page.show()
    page.timer.stop()
    QApplication.processEvents()
    yield page, ctx
    page.shutdown()
    page.close()
    QApplication.processEvents()
    assert not page._worker.isRunning()
    assert app is not None


def anchor(page, ctx, **changes):
    if not page._poll_busy:
        page._poll()
    ctx.pulse_ready.emit(batch(**changes))
    events_until(lambda: not page._engine_busy)


def start_noise(page, ctx):
    anchor(page, ctx)
    page.unloaded.setChecked(True)
    assert page.noise_button.isEnabled()
    page.noise_button.click()
    events_until(lambda: not page._engine_busy)
    assert page._pending == "noise"
    page._poll()
    assert ctx.calls[-1][1]["after"] == 2**53 - 1
    # Pre-action history on this anchor is intentionally ignored.
    ctx.pulse_ready.emit(batch(frames(200), cursor=200))
    events_until(lambda: not page._engine_busy)
    assert page.state["phase"] == "noise"
    assert page.state["processedCount"] == 0


def finish_noise(page, ctx):
    start_noise(page, ctx)
    page._poll()
    assert ctx.calls[-1][1]["after"] == 200
    ctx.pulse_ready.emit(batch(frames(510), cursor=710))
    events_until(lambda: not page._engine_busy)
    assert page.state["phase"] == "monitor"
    return page.state["weights"][:]


def test_page_requires_explicit_confirmation_and_anchors_after_action(pulse_page):
    page, ctx = pulse_page
    assert ctx.calls == [("pulse_samples", {"after": 2**53 - 1, "limit": 4096})]
    assert not page.noise_button.isEnabled()
    start_noise(page, ctx)
    assert not page.unloaded.isChecked()
    assert not page.unloaded.isEnabled()
    assert not page.apply_button.isEnabled()
    assert all(action not in {"connect", "reference"} for action, _ in ctx.calls)


def test_unloaded_mouse_click_survives_source_poll_between_press_and_release(pulse_page, monkeypatch):
    page, ctx = pulse_page
    anchor(page, ctx)
    jobs = []
    # Hold only processing completion; source arrival still traverses the real
    # page handler while the user's mouse is down. No hardware is acquired.
    monkeypatch.setattr(page._worker, "submit", lambda *args: jobs.append(args))
    position = QPoint(8, page.unloaded.height() // 2)
    QTest.mousePress(page.unloaded, Qt.MouseButton.LeftButton, pos=position)
    page._poll()
    ctx.pulse_ready.emit(batch(frames(1), cursor=101))
    assert jobs and jobs[-1][1] == "push" and page._engine_busy
    QTest.mouseRelease(page.unloaded, Qt.MouseButton.LeftButton, pos=position)
    assert page.unloaded.isChecked(), "正常源帧刷新不能取消用户正在进行的无载勾选"
    assert page.noise_button.isEnabled()


def test_noise_button_click_survives_source_poll_and_keeps_new_source_boundary(pulse_page, monkeypatch):
    page, ctx = pulse_page
    anchor(page, ctx)
    page.unloaded.setChecked(True)
    jobs = []
    monkeypatch.setattr(page._worker, "submit", lambda *args: jobs.append(args))
    position = page.noise_button.rect().center()
    QTest.mousePress(page.noise_button, Qt.MouseButton.LeftButton, pos=position)
    page._poll()
    ctx.pulse_ready.emit(batch(frames(1), cursor=101))
    old_generation = page._generation
    QTest.mouseRelease(page.noise_button, Qt.MouseButton.LeftButton, pos=position)
    assert page._pending == "noise"
    assert page._generation > old_generation
    assert jobs[-1][1] == "reset"
    assert not page.unloaded.isChecked() and not page.unloaded.isEnabled()
    assert not page.noise_button.isEnabled()
    # Completion of the pre-click push cannot reopen the pending operation.
    page._receive_state(old_generation, page.state.copy())
    assert page._pending == "noise" and page._engine_busy
    assert all(action not in {"connect", "pulse_reference"} for action, _ in ctx.calls)


def test_first_reference_confirmation_is_separate_from_noise_confirmation(pulse_page):
    page, ctx = pulse_page
    anchor(page, ctx, blocked=True, reason="等待明确无载确认与参考初始化完成", can_initialize=True)
    assert not page.reference_button.isHidden()
    assert not page.reference_button.isEnabled()
    page.unloaded.setChecked(True)
    page.reference_button.click()
    assert ctx.calls[-1] == ("pulse_reference", {"unloaded_confirmed": True})
    assert not page.unloaded.isChecked()
    assert page._reference_busy
    ctx.action_finished.emit("pulse_reference", batch(blocked=True))
    events_until(lambda: not page._engine_busy)
    assert not page._reference_busy
    assert not page.noise_button.isEnabled()
    assert page.state["phase"] == "idle"


def test_band_is_applied_explicitly_and_same_band_preserves_weights(pulse_page):
    page, ctx = pulse_page
    weights = finish_noise(page, ctx)
    before = page.state.copy()
    page.band_low.setValue(.5)
    assert page.state["filterBandHz"] == [.7, 3]
    assert page.state["weights"] == weights
    assert "尚未应用" in page.filter_status.text()
    page.band_low.setValue(.7)
    page.apply_button.click()
    assert page.state == before
    assert "未改变" in page.filter_status.text()
    page.band_low.setValue(.5)
    page.apply_button.click()
    events_until(lambda: not page._engine_busy)
    assert page.state["filterBandHz"] == [.5, 3]
    assert page.state["weights"] is None and page.state["pointCount"] == 0
    assert not page.unloaded.isChecked()


def test_band_limit_and_invalid_band_leave_signal_unchanged(pulse_page):
    page, ctx = pulse_page
    finish_noise(page, ctx)
    page.state["rateHz"] = 10
    before = page.state.copy()
    page.band_high.setValue(8)
    page.apply_button.click()
    assert "上限过高" in page.filter_status.text()
    assert page.state == before
    page.band_low.setValue(8)
    page.apply_button.click()
    assert "下限" in page.filter_status.text()
    assert page.state == before


def test_source_pause_retains_weights_and_binding_fault_invalidates(pulse_page):
    page, ctx = pulse_page
    weights = finish_noise(page, ctx)
    page._poll()
    ctx.pulse_ready.emit(batch(blocked=True, age_ms=1001, reason="光谱超过 1 秒未更新或缺少源帧接收时间"))
    events_until(lambda: not page._engine_busy)
    assert page.state["suspended"]
    assert page.state["weights"] == weights
    # Reanchor skips old samples, then a same-binding source resumes automatically.
    anchor(page, ctx)
    page._poll()
    ctx.pulse_ready.emit(batch(frames(4, start=111, seq=511), cursor=714))
    events_until(lambda: not page._engine_busy)
    assert not page.state["suspended"]
    assert page.state["weights"] == weights
    assert page.state["points"][-4]["breakBefore"]
    page._poll()
    ctx.pulse_ready.emit(batch(latest_binding="different-reference"))
    events_until(lambda: not page._engine_busy)
    assert page.state["phase"] == "failed"
    assert page.state["weights"] is None


def test_hiding_page_invalidates_unfinished_noise_and_stops_polling(pulse_page):
    page, ctx = pulse_page
    start_noise(page, ctx)
    page.hide()
    events_until(lambda: not page._engine_busy)
    assert page.state["phase"] == "failed"
    assert not page.timer.isActive()
    calls = ctx.calls[:]
    page._poll()
    assert ctx.calls == calls


def test_dynamic_labels_are_plain_text_and_priority_is_exposed(pulse_page):
    page, ctx = pulse_page
    assert page.freeze_others.isChecked()
    anchor(page, ctx, blocked=True, reason="<b>原始设备消息</b>")
    assert page.notice.text() == "<b>原始设备消息</b>"
    assert page.notice.textFormat() == Qt.TextFormat.PlainText


def test_rejected_controlled_shutdown_can_resume_polling(pulse_page):
    page, ctx = pulse_page
    weights = finish_noise(page, ctx)
    page._poll()
    assert page._poll_busy
    ctx.closing = True
    page.update_state({})
    events_until(lambda: not page._engine_busy)
    assert not page._poll_busy and not page.timer.isActive()
    assert page.state["weights"] == weights
    assert page.state["suspended"]
    ctx.closing = False
    page.update_state({})
    page.timer.stop()
    assert page._poll_busy
    assert ctx.calls[-1][1]["after"] == 2**53 - 1
    ctx.pulse_ready.emit(batch())
    assert page._initialized
