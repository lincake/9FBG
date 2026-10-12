"""Timer requests are scoped to reader lifetime, never startup or hardware."""
import json
import ctypes
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from ninefbg import timing
from ninefbg.service import AcquisitionService, DisplayWriter
from ninefbg.sync import ForceSynchronizer


@pytest.fixture
def windows_api(monkeypatch):
    calls = []
    state = SimpleNamespace(begin_result=0, end_result=0, begin_error=None, end_error=None,
                            timer_calls=[], create_result=True, create_error=None, load_error=None,
                            set_result=True, set_error=None, wait_result=0, wait_error=None,
                            wait_hook=None, close_result=True, close_error=None, next_handle=100,
                            real_load_wait_api=timing._load_wait_api)

    def begin(period):
        calls.append(("begin", period))
        if state.begin_error:
            raise state.begin_error
        return state.begin_result

    def end(period):
        calls.append(("end", period))
        if state.end_error:
            raise state.end_error
        return state.end_result

    api = SimpleNamespace(timeBeginPeriod=begin, timeEndPeriod=end)

    def load():
        calls.append(("load", None))
        return api

    monkeypatch.setattr(timing.sys, "platform", "win32")
    monkeypatch.setattr(timing, "_load_winmm", load)

    def create(attributes, name, flags, access):
        state.timer_calls.append(("create", attributes, name, flags, access))
        if state.create_error:
            raise state.create_error
        if not state.create_result:
            return 0
        state.next_handle += 1
        return state.next_handle

    def arm(handle, due, period, completion, argument, wake_context, tolerance):
        state.timer_calls.append(("arm", handle, due._obj.value, period, completion,
                                  argument, wake_context, tolerance))
        if state.set_error:
            raise state.set_error
        return state.set_result

    def wait(handle, timeout):
        state.timer_calls.append(("wait", handle, timeout))
        if state.wait_error:
            raise state.wait_error
        if state.wait_hook:
            state.wait_hook()
        return state.wait_result

    def close(handle):
        state.timer_calls.append(("close", handle))
        if state.close_error:
            raise state.close_error
        return state.close_result

    wait_api = SimpleNamespace(CreateWaitableTimerExW=create, SetWaitableTimerEx=arm,
                               WaitForSingleObject=wait, CloseHandle=close)

    def load_wait():
        state.timer_calls.append(("load",))
        if state.load_error:
            raise state.load_error
        return wait_api

    monkeypatch.setattr(timing, "_load_wait_api", load_wait)
    return calls, state


def test_constructor_has_no_timer_side_effect_and_diagnostics_are_independent(windows_api):
    calls, _ = windows_api
    lease = timing.AcquisitionTiming()
    assert calls == []
    diagnostic = lease.diagnostics()
    assert diagnostic["status"] == "not_started" and not diagnostic["active"]
    diagnostic["status"] = "changed"
    assert lease.diagnostics()["status"] == "not_started"
    json.dumps(lease.diagnostics(), allow_nan=False)


def test_windows_lease_pairs_the_same_period_and_reports_release(windows_api):
    calls, _ = windows_api
    lease = timing.AcquisitionTiming()
    with lease as entered:
        assert entered is lease
        assert calls == [("load", None), ("begin", 1)]
        diagnostic = lease.diagnostics()
        assert diagnostic["scope"] == "current_process" and diagnostic["requested_period_ms"] == 1
        assert diagnostic["status"] == "active" and diagnostic["active"]
        assert diagnostic["begin_attempted"] and diagnostic["begin_result"] == 0
        assert not diagnostic["end_attempted"]
    assert calls == [("load", None), ("begin", 1), ("end", 1)]
    diagnostic = lease.diagnostics()
    assert diagnostic["status"] == "released" and not diagnostic["active"]
    assert diagnostic["end_attempted"] and diagnostic["end_result"] == 0
    lease.__exit__(None, None, None)
    assert calls.count(("end", 1)) == 1
    with pytest.raises(RuntimeError, match="只能使用一次"):
        with lease:
            pass


def test_device_exception_is_preserved_and_timer_is_released(windows_api):
    calls, _ = windows_api
    lease = timing.AcquisitionTiming()
    expected = OSError("模拟设备打开失败")
    with pytest.raises(OSError) as caught:
        with lease:
            raise expected
    assert caught.value is expected
    assert calls[-1] == ("end", 1)
    assert lease.diagnostics()["status"] == "released"


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_non_windows_never_loads_or_calls_windows_api(windows_api, monkeypatch, platform):
    calls, _ = windows_api
    monkeypatch.setattr(timing.sys, "platform", platform)
    with timing.AcquisitionTiming() as lease:
        assert lease.diagnostics()["status"] == "not_applicable"
    assert not lease.diagnostics()["active"] and not lease.diagnostics()["end_attempted"]
    assert calls == []


def test_failed_begin_is_visible_fallback_without_unmatched_end(windows_api):
    calls, state = windows_api
    state.begin_result = 97
    with timing.AcquisitionTiming() as lease:
        diagnostic = lease.diagnostics()
        assert diagnostic["status"] == "fallback" and not diagnostic["active"]
        assert diagnostic["begin_attempted"] and diagnostic["begin_result"] == 97
        assert "97" in diagnostic["reason"]
    assert calls == [("load", None), ("begin", 1)]
    assert not lease.diagnostics()["end_attempted"]


@pytest.mark.parametrize("failure_stage", ["load", "begin"])
def test_unavailable_windows_api_preserves_context_body_and_reports_fallback(windows_api, monkeypatch, failure_stage):
    calls, state = windows_api
    if failure_stage == "load":
        def unavailable():
            raise OSError("模拟 WinMM 不可用")
        monkeypatch.setattr(timing, "_load_winmm", unavailable)
    else:
        state.begin_error = OSError("模拟计时调用失败")
    body = []
    with timing.AcquisitionTiming() as lease:
        body.append("reader continues")
        assert lease.diagnostics()["status"] == "fallback"
    assert body == ["reader continues"]
    assert not lease.diagnostics()["end_attempted"]
    assert not any(name == "end" for name, _ in calls)
    assert lease.diagnostics()["begin_attempted"] is (failure_stage == "begin")


@pytest.mark.parametrize("end_failure", ["return", "raise"])
def test_failed_end_is_visible_and_does_not_hide_device_failure(windows_api, end_failure):
    calls, state = windows_api
    if end_failure == "return":
        state.end_result = 97
    else:
        state.end_error = OSError("模拟释放失败")
    lease = timing.AcquisitionTiming()
    expected = RuntimeError("模拟采集失败")
    with pytest.raises(RuntimeError) as caught:
        with lease:
            raise expected
    assert caught.value is expected
    diagnostic = lease.diagnostics()
    assert diagnostic["status"] == "release_failed" and diagnostic["active"]
    assert diagnostic["end_attempted"]
    assert diagnostic["end_result"] == (97 if end_failure == "return" else None)
    lease.__exit__(None, None, None)
    assert calls.count(("end", 1)) == 1


def test_overlapping_reader_contexts_each_pair_own_begin_and_end(windows_api):
    calls, _ = windows_api
    with timing.AcquisitionTiming() as optical:
        with timing.AcquisitionTiming() as force:
            assert optical.diagnostics()["active"] and force.diagnostics()["active"]
            assert calls.count(("begin", 1)) == 2 and calls.count(("end", 1)) == 0
        assert force.diagnostics()["status"] == "released"
        assert optical.diagnostics()["active"]
        assert calls.count(("end", 1)) == 1
    assert calls.count(("begin", 1)) == calls.count(("end", 1)) == 2


def test_stop_request_and_pending_join_do_not_release_before_reader_close(windows_api):
    calls, _ = windows_api
    opened, stop_requested, close_allowed = threading.Event(), threading.Event(), threading.Event()
    lease = timing.AcquisitionTiming()

    def reader():
        with lease:
            opened.set()
            stop_requested.wait(2)
            # Model a source still draining or closing after stop was requested.
            close_allowed.wait(2)

    worker = threading.Thread(target=reader)
    worker.start()
    try:
        assert opened.wait(1)
        stop_requested.set()
        worker.join(.02)
        assert worker.is_alive()
        assert lease.diagnostics()["active"] and calls.count(("end", 1)) == 0
    finally:
        stop_requested.set()
        close_allowed.set()
        worker.join(2)
    assert not worker.is_alive() and lease.diagnostics()["status"] == "released"
    assert calls.count(("begin", 1)) == calls.count(("end", 1)) == 1


@pytest.mark.parametrize("period", [True, 0, -1, 1001, 1.5, "1"])
def test_invalid_period_does_not_call_timer_api(windows_api, period):
    calls, _ = windows_api
    with pytest.raises(ValueError, match="整数"):
        timing.AcquisitionTiming(period)
    assert calls == []


@pytest.mark.parametrize("kind,failure", [("force", None), ("spectrum", None), ("force", "open"),
                                         ("spectrum", "open"), ("force", "read"),
                                         ("force", "close"), ("spectrum", "drain")])
def test_service_reader_keeps_timer_until_source_close_or_drain_even_on_failure(tmp_path, windows_api, kind, failure):
    calls, _ = windows_api
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "uncreated")
    service.writer = DisplayWriter()
    service.synchronizer = ForceSynchronizer()
    local_stop = threading.Event() if kind == "force" else None

    class Source:
        def stage(self, name):
            assert calls.count(("begin", 1)) == 1 and calls.count(("end", 1)) == 0
            calls.append(("source_" + name, None))
            if failure == name:
                raise OSError("模拟读取生命周期失败：" + name)

        def open(self):
            self.stage("open")

        def read(self):
            self.stage("read")
            (local_stop if local_stop is not None else service.stop_event).set()
            return None

        def close(self):
            self.stage("close")

        def drain_received(self):
            self.stage("drain")
            return []

        def diagnostics(self):
            return {"connected": True}

    service._reader(Source(), kind, local_stop)
    assert calls[:2] == [("load", None), ("begin", 1)]
    assert calls[-1] == ("end", 1)
    assert calls.count(("begin", 1)) == calls.count(("end", 1)) == 1
    assert ("source_close", None) in calls
    if kind == "spectrum":
        assert ("source_drain", None) in calls
    events = list(service.writer.events)
    assert events[0]["event"] == "acquisition_timing_start"
    assert events[0]["diagnostics"]["status"] == "active"
    assert events[-1]["event"] == "acquisition_timing_end"
    assert events[-1]["diagnostics"]["status"] == "released"
    assert events[-1]["diagnostics"]["end_result"] == 0
    assert bool(service.error) is (failure is not None)
    assert not service.userdata.exists()


def test_service_reader_pending_close_retains_timer_and_delays_end_event(tmp_path, windows_api):
    calls, _ = windows_api
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "uncreated")
    service.writer = DisplayWriter()
    local_stop = threading.Event()
    opened, closing, close_allowed = threading.Event(), threading.Event(), threading.Event()

    class Source:
        def open(self):
            opened.set()

        def read(self):
            return None

        def close(self):
            closing.set()
            close_allowed.wait(2)

        def diagnostics(self):
            return {"connected": True}

    worker = threading.Thread(target=service._reader, args=(Source(), "force", local_stop))
    worker.start()
    try:
        assert opened.wait(1)
        local_stop.set()
        assert closing.wait(1)
        worker.join(.02)
        assert worker.is_alive() and calls.count(("end", 1)) == 0
        assert not any(event["event"] == "acquisition_timing_end" for event in service.writer.events)
    finally:
        local_stop.set()
        close_allowed.set()
        worker.join(2)
    assert not worker.is_alive() and calls.count(("end", 1)) == 1
    assert service.writer.events[-1]["diagnostics"]["status"] == "released"
    assert not service.userdata.exists()


def test_service_fallback_is_recorded_and_reader_still_opens_and_closes(tmp_path, windows_api):
    calls, state = windows_api
    state.begin_result = 97
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "uncreated")
    service.writer = DisplayWriter()
    local_stop = threading.Event()
    lifecycle = []

    class Source:
        def open(self):
            lifecycle.append("open")

        def read(self):
            local_stop.set()
            return None

        def close(self):
            lifecycle.append("close")

        def diagnostics(self):
            return {"connected": True}

    service._reader(Source(), "force", local_stop)
    assert lifecycle == ["open", "close"] and service.error is None
    assert calls == [("load", None), ("begin", 1)]
    events = [event for event in service.writer.events if event["event"].startswith("acquisition_timing_")]
    assert [event["diagnostics"]["status"] for event in events] == ["fallback", "fallback"]
    assert all(event["diagnostics"]["begin_result"] == 97 for event in events)
    assert not service.userdata.exists()


def test_service_construction_and_replay_never_request_windows_timer(tmp_path, windows_api):
    from test_service import spectra

    calls, _ = windows_api
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "uncreated")
    assert calls == []
    service.connect("replay", spectra(1), record=False, connect_force=False)
    try:
        assert calls == []
    finally:
        service.disconnect()
    assert calls == [] and not service.userdata.exists()


def test_native_loader_uses_windll_and_declares_pointer_safe_signatures(windows_api, monkeypatch):
    _, state = windows_api
    loads = []

    class Function:
        pass

    api = SimpleNamespace(**{name: Function() for name in
                             ("CreateWaitableTimerExW", "SetWaitableTimerEx",
                              "WaitForSingleObject", "CloseHandle")})

    def load(name, **kwargs):
        loads.append((name, kwargs))
        return api

    monkeypatch.setattr(timing.ctypes, "WinDLL", load)
    assert state.real_load_wait_api() is api
    assert loads == [("kernel32", {"use_last_error": True})]
    assert api.CreateWaitableTimerExW.restype is ctypes.c_void_p
    assert api.CreateWaitableTimerExW.argtypes == [ctypes.c_void_p, ctypes.c_wchar_p,
                                                  ctypes.c_ulong, ctypes.c_ulong]
    assert api.SetWaitableTimerEx.argtypes[1] is ctypes.POINTER(ctypes.c_longlong)
    assert api.WaitForSingleObject.argtypes == [ctypes.c_void_p, ctypes.c_ulong]
    assert api.CloseHandle.argtypes == [ctypes.c_void_p]


def test_high_resolution_wait_ready_relative_short_slices_and_paired_handle(windows_api):
    _, state = windows_api
    stop = threading.Event()
    with timing.AcquisitionTiming() as lease:
        assert lease.diagnostics()["wait_backend"] == "windows_high_resolution_timer"
        assert lease.diagnostics()["timer_active"]
        assert state.timer_calls[:2] == [("load",), ("create", None, None, 2, 0x100002)]
        assert lease.wait(stop, .003) is False
        arms = [call for call in state.timer_calls if call[0] == "arm"]
        assert [call[2] for call in arms] == [-20000, -10000]
        assert all(call[3:] == (0, None, None, None, 0) for call in arms)
        assert [call for call in state.timer_calls if call[0] == "wait"] == [
            ("wait", 101, 0xFFFFFFFF), ("wait", 101, 0xFFFFFFFF)]
        assert lease.diagnostics()["wait_calls"] == 1
        assert not lease.diagnostics()["timer_closed"]
    assert state.timer_calls[-1] == ("close", 101)
    assert lease.diagnostics()["timer_closed"] and not lease.diagnostics()["timer_active"]
    lease.__exit__(None, None, None)
    assert state.timer_calls.count(("close", 101)) == 1
    json.dumps(lease.diagnostics(), allow_nan=False)


def test_stopped_event_returns_without_arming_and_stop_between_slices_is_observed(windows_api):
    _, state = windows_api
    stop = threading.Event()
    with timing.AcquisitionTiming() as lease:
        stop.set()
        before = list(state.timer_calls)
        assert lease.wait(stop, .003) is True
        assert state.timer_calls == before
        stop.clear()
        state.wait_hook = stop.set
        assert lease.wait(stop, .003) is True
        assert len([call for call in state.timer_calls if call[0] == "arm"]) == 1


def test_each_context_owns_an_independent_waitable_timer(windows_api):
    _, state = windows_api
    with timing.AcquisitionTiming() as first:
        with timing.AcquisitionTiming() as second:
            assert first.wait(threading.Event(), .002) is False
            assert second.wait(threading.Event(), .002) is False
        assert state.timer_calls[-1] == ("close", 102)
        assert first.diagnostics()["timer_active"]
    assert state.timer_calls[-1] == ("close", 101)
    assert [call for call in state.timer_calls if call[0] == "close"] == [("close", 102), ("close", 101)]


@pytest.mark.parametrize("stage", ["load", "create_return", "create_raise"])
def test_timer_creation_failure_has_visible_sleep_fallback_and_stop_checks(windows_api, monkeypatch, stage):
    _, state = windows_api
    if stage == "load":
        state.load_error = OSError("模拟 kernel32 不可用")
    elif stage == "create_return":
        state.create_result = False
    else:
        state.create_error = OSError("模拟计时器创建失败")
    sleeps = []
    stop = threading.Event()

    def sleep(seconds):
        sleeps.append(seconds)
        stop.set()

    monkeypatch.setattr(timing.time, "sleep", sleep)
    with timing.AcquisitionTiming() as lease:
        diagnostic = lease.diagnostics()
        assert diagnostic["wait_backend"] == "windows_sleep_fallback"
        assert diagnostic["timer_create_attempted"] and not diagnostic["timer_created"]
        assert diagnostic["timer_error"] and "短片睡眠" in diagnostic["wait_reason"]
        assert lease.wait(stop, .003) is True
        assert sleeps == [.002]
        assert lease.diagnostics()["wait_fallback_count"] == 1
    assert not lease.diagnostics()["timer_close_attempted"]
    assert not any(call[0] in ("arm", "wait", "close") for call in state.timer_calls)


def test_high_resolution_timer_is_available_even_if_winmm_request_failed(windows_api):
    _, state = windows_api
    state.begin_result = 97
    with timing.AcquisitionTiming() as lease:
        assert lease.diagnostics()["status"] == "fallback"
        assert lease.diagnostics()["wait_backend"] == "windows_high_resolution_timer"
        assert lease.wait(threading.Event(), .002) is False
    assert lease.diagnostics()["timer_closed"] and not lease.diagnostics()["end_attempted"]


@pytest.mark.parametrize("stage", ["set_return", "set_raise", "wait_return", "wait_raise"])
def test_native_wait_error_falls_back_once_and_still_closes_handle(windows_api, monkeypatch, stage):
    _, state = windows_api
    if stage == "set_return":
        state.set_result = False
    elif stage == "set_raise":
        state.set_error = OSError("模拟设置计时器失败")
    elif stage == "wait_return":
        state.wait_result = 0xFFFFFFFF
    else:
        state.wait_error = OSError("模拟计时器等待失败")
    sleeps = []
    monkeypatch.setattr(timing.time, "sleep", sleeps.append)
    with timing.AcquisitionTiming() as lease:
        assert lease.wait(threading.Event(), .003) is False
        diagnostic = lease.diagnostics()
        assert diagnostic["wait_backend"] == "windows_sleep_fallback"
        assert diagnostic["timer_error"] and diagnostic["wait_fallback_count"] == 2
        arm_count = len([call for call in state.timer_calls if call[0] == "arm"])
        assert lease.wait(threading.Event(), .002) is False
        assert len([call for call in state.timer_calls if call[0] == "arm"]) == arm_count == 1
    assert sleeps == [.002, .001, .002]
    assert lease.diagnostics()["timer_closed"]


def test_non_windows_wait_uses_event_and_never_creates_timer(windows_api, monkeypatch):
    _, state = windows_api
    monkeypatch.setattr(timing.sys, "platform", "linux")
    calls = []
    stop = SimpleNamespace(is_set=lambda: False, wait=lambda seconds: calls.append(seconds) or True)
    with timing.AcquisitionTiming() as lease:
        assert lease.wait(stop, .003) is True
        assert lease.diagnostics()["wait_backend"] == "event_wait"
    assert calls == [.003] and state.timer_calls == []


def test_windows_fallback_never_uses_coarse_event_wait(windows_api, monkeypatch):
    _, state = windows_api
    state.create_result = False
    sleeps = []
    monkeypatch.setattr(timing.time, "sleep", sleeps.append)

    def forbidden_wait(seconds):
        raise AssertionError("Windows 短等待不应回退到 Event.wait")

    stop = SimpleNamespace(is_set=lambda: False, wait=forbidden_wait)
    with timing.AcquisitionTiming() as lease:
        assert lease.wait(stop, .003) is False
    assert sleeps == [.002, .001]


@pytest.mark.parametrize("failure", ["return", "raise"])
def test_close_failure_is_visible_without_hiding_device_exception(windows_api, failure):
    calls, state = windows_api
    if failure == "return":
        state.close_result = False
    else:
        state.close_error = OSError("模拟句柄关闭失败")
    expected = OSError("模拟设备失败")
    with pytest.raises(OSError) as caught:
        with timing.AcquisitionTiming() as lease:
            raise expected
    assert caught.value is expected
    assert lease.diagnostics()["timer_close_attempted"]
    assert lease.diagnostics()["timer_close_error"] and not lease.diagnostics()["timer_closed"]
    assert lease.diagnostics()["timer_active"]
    assert calls[-1] == ("end", 1)


def test_pending_native_wait_cannot_have_its_timer_closed(windows_api):
    calls, state = windows_api
    waiting, allow_return, exit_started = threading.Event(), threading.Event(), threading.Event()
    stop = threading.Event()
    lease = timing.AcquisitionTiming()
    lease.__enter__()

    def blocked_native_wait():
        waiting.set()
        assert allow_return.wait(2)

    state.wait_hook = blocked_native_wait
    result = []
    worker = threading.Thread(target=lambda: result.append(lease.wait(stop, .002)))

    def close_context():
        exit_started.set()
        lease.__exit__(None, None, None)

    closer = threading.Thread(target=close_context)
    worker.start()
    try:
        assert waiting.wait(1)
        stop.set()
        closer.start()
        assert exit_started.wait(1)
        closer.join(.02)
        assert closer.is_alive()
        assert not any(call[0] == "close" for call in state.timer_calls)
        assert ("end", 1) not in calls
    finally:
        allow_return.set()
        worker.join(2)
        closer.join(2)
    assert not worker.is_alive() and not closer.is_alive() and result == [True]
    assert lease.diagnostics()["timer_closed"]
    assert calls.count(("end", 1)) == 1


@pytest.mark.parametrize("seconds", [True, -1, float("nan"), float("inf"), "0.002", None])
def test_invalid_wait_duration_never_arms_timer(windows_api, seconds):
    _, state = windows_api
    with timing.AcquisitionTiming() as lease:
        with pytest.raises(ValueError, match="有限秒数"):
            lease.wait(threading.Event(), seconds)
    assert not any(call[0] in ("arm", "wait") for call in state.timer_calls)


def test_wait_requires_a_live_context_and_zero_duration_does_not_arm(windows_api):
    _, state = windows_api
    stop = threading.Event()
    lease = timing.AcquisitionTiming()
    with pytest.raises(RuntimeError, match="有效计时上下文"):
        lease.wait(stop, .002)
    with lease:
        assert lease.wait(stop, 0) is False
    with pytest.raises(RuntimeError, match="有效计时上下文"):
        lease.wait(stop, .002)
    assert not any(call[0] in ("arm", "wait") for call in state.timer_calls)
