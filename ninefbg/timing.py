"""A scoped Windows timer request for one acquisition reader's lifetime.

Construction and import have no operating-system side effects. Each reader
owns its own paired begin/end calls; Windows maintains the process request
counts. This changes timed-wait granularity, never device sampling settings
or the monotonic clock used to label acquired frames.
"""
from __future__ import annotations

import ctypes
import math
import sys
import threading
import time


def _load_winmm():
    """Load lazily, after a Windows reader enters its acquisition context."""
    library = ctypes.WinDLL("winmm", use_last_error=True)
    for name in ("timeBeginPeriod", "timeEndPeriod"):
        function = getattr(library, name)
        function.argtypes = [ctypes.c_uint]
        function.restype = ctypes.c_uint
    return library


def _load_wait_api():
    """WinDLL releases the GIL while the kernel waits for this reader's timer."""
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                              ctypes.c_ulong, ctypes.c_ulong]
    library.CreateWaitableTimerExW.restype = ctypes.c_void_p
    library.SetWaitableTimerEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong),
                                         ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_ulong]
    library.SetWaitableTimerEx.restype = ctypes.c_int
    library.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    library.WaitForSingleObject.restype = ctypes.c_ulong
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.CloseHandle.restype = ctypes.c_int
    return library


def _api_error(operation):
    return OSError(f"{operation} 失败（Windows 错误代码 {ctypes.get_last_error()}）")


class AcquisitionTiming:
    """Single-use, exception-safe timer lease; unsupported/failure is visible.

    Keep the context around source.open/read/close, including any draining.
    A stop request or an incomplete join must not close this context on the
    reader's behalf. Diagnostics remain available after the context exits.
    """

    def __init__(self, period_ms=1):
        if type(period_ms) is not int or not 1 <= period_ms <= 1000:
            raise ValueError("采集计时周期必须为 1～1000 ms 的整数")
        self._period_ms = period_ms
        self._platform = sys.platform
        self._library = None
        self._entered = False
        self._exited = False
        self._leased = False
        self._wait_api = None
        self._timer = None
        self._timer_usable = False
        # A timer handle must not be closed while a kernel wait is pending.
        self._wait_lock = threading.Lock()
        self._state = {
            "schema": "acquisition_timing_v1", "platform": self._platform,
            "scope": "current_process", "requested_period_ms": period_ms,
            "status": "not_started", "active": False,
            "begin_attempted": False, "begin_result": None,
            "end_attempted": False, "end_result": None,
            "reason": "尚未请求采集计时精度",
            "wait_backend": "not_started", "wait_calls": 0,
            "wait_fallback_count": 0, "wait_reason": "尚未创建采集等待计时器",
            "timer_create_attempted": False, "timer_created": False,
            "timer_active": False, "timer_close_attempted": False,
            "timer_closed": False, "timer_error": None, "timer_close_error": None,
        }

    def diagnostics(self):
        """A JSON-safe independent snapshot, suitable for acquisition events."""
        return dict(self._state)

    def __enter__(self):
        if self._entered:
            raise RuntimeError("采集计时上下文只能使用一次；每个读取线程需独立创建")
        self._entered = True
        if self._platform != "win32":
            self._state.update(status="not_applicable", reason="当前平台无需 Windows 采集计时请求")
            self._state.update(wait_backend="event_wait", wait_reason="当前平台使用可停止事件等待")
            return self
        try:
            self._library = _load_winmm()
            self._state["begin_attempted"] = True
            result = int(self._library.timeBeginPeriod(self._period_ms))
            self._state["begin_result"] = result
            if result == 0:
                self._leased = True
                self._state.update(status="active", active=True,
                                   reason=f"当前进程已请求 {self._period_ms} ms 采集计时精度")
            else:
                self._state.update(status="fallback",
                                   reason=f"Windows 未接受采集计时请求（返回代码 {result}）；沿用系统计时")
        except Exception as exc:
            self._state.update(status="fallback", reason="采集计时请求不可用；沿用系统计时：" + str(exc))
        # A high-resolution waitable timer is explicit: timeBeginPeriod alone
        # may be ignored for an invisible/occluded Windows 11 process.
        try:
            self._state["timer_create_attempted"] = True
            self._wait_api = _load_wait_api()
            self._timer = self._wait_api.CreateWaitableTimerExW(None, None, 0x00000002, 0x00100002)
            if not self._timer:
                raise _api_error("CreateWaitableTimerExW")
            self._timer_usable = True
            self._state.update(wait_backend="windows_high_resolution_timer", timer_created=True,
                               timer_active=True, wait_reason="使用独立 Windows 高分辨率等待计时器")
        except Exception as exc:
            self._fallback(exc)
        return self

    def _fallback(self, error):
        self._timer_usable = False
        self._state.update(wait_backend="windows_sleep_fallback", timer_error=str(error),
                           wait_reason="高分辨率等待不可用；使用短片睡眠并检查停止：" + str(error))

    def wait(self, stop_event, seconds):
        """Wait without spinning; return True if a stop was observed.

        Windows checks stop before and after each at-most-2-ms native wait.
        This bounds the extra stop latency without introducing native stop
        handles into the existing event lifecycle. Scheduling/GIL delays can
        still extend a wait. Failures use modern Python's short time.sleep,
        never the Windows Event.wait path whose tick can be much coarser.
        """
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("采集等待时长必须为非负有限秒数")
        with self._wait_lock:
            if not self._entered or self._exited:
                raise RuntimeError("采集等待必须在有效计时上下文内执行")
            if stop_event.is_set():
                return True
            self._state["wait_calls"] += 1
            if self._platform != "win32":
                return bool(stop_event.wait(seconds))
            remaining = float(seconds)
            while remaining > 0:
                if stop_event.is_set():
                    return True
                duration = min(remaining, .002)
                if self._timer_usable:
                    try:
                        # Negative due times are relative, in 100-ns units;
                        # no wall-clock value can make this wait run backward.
                        due = ctypes.c_longlong(-max(1, math.ceil(duration * 10_000_000)))
                        if not self._wait_api.SetWaitableTimerEx(self._timer, ctypes.byref(due), 0,
                                                                None, None, None, 0):
                            raise _api_error("SetWaitableTimerEx")
                        result = int(self._wait_api.WaitForSingleObject(self._timer, 0xFFFFFFFF))
                        if result != 0:  # WAIT_OBJECT_0 is the only successful timer result.
                            raise _api_error(f"WaitForSingleObject（返回代码 {result}）")
                    except Exception as exc:
                        self._fallback(exc)
                        self._state["wait_fallback_count"] += 1
                        time.sleep(duration)
                else:
                    self._state["wait_fallback_count"] += 1
                    time.sleep(duration)
                remaining -= duration
            return bool(stop_event.is_set())

    def __exit__(self, exc_type, exc_value, traceback):
        with self._wait_lock:
            if self._exited or not self._entered:
                return False
            self._exited = True
            self._timer_usable = False
            if self._timer:
                self._state["timer_close_attempted"] = True
                try:
                    if not self._wait_api.CloseHandle(self._timer):
                        raise _api_error("CloseHandle")
                    self._timer = None
                    self._state.update(timer_closed=True, timer_active=False)
                except Exception as exc:
                    self._state["timer_close_error"] = str(exc)
            if self._leased:
                self._state["end_attempted"] = True
                try:
                    result = int(self._library.timeEndPeriod(self._period_ms))
                    self._state["end_result"] = result
                    if result == 0:
                        self._leased = False
                        self._state.update(status="released", active=False, reason="采集读取线程已释放计时请求")
                    else:
                        # A failed release cannot be reported as a successful end.
                        self._state.update(status="release_failed",
                                           reason=f"Windows 采集计时释放失败（返回代码 {result}）；请求可能仍有效")
                except Exception as exc:
                    self._state.update(status="release_failed", reason="采集计时释放失败；请求可能仍有效：" + str(exc))
        # Preserve any instrument or recording exception from the context body.
        return False
