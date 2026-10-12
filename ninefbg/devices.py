"""Independent, opt-in BaySpec/PX6D readers. Importing this module opens nothing."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import numpy as np

APP_ROOT = Path(__file__).resolve().parents[1]
AXES = ("Fx", "Fy", "Fz", "Mx", "My", "Mz")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def load_device_config(path=None):
    return json.loads(Path(path or APP_ROOT / "config/device.json").read_text(encoding="utf-8"))


def _exact_int(value, lower, upper, name):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name}必须为 {lower}～{upper} 的整数。")
    return value


def _helper_kill_on_close_job(process_id):
    """Windows kills the helper if its parent exits, even if the DLL stalls."""
    if os.name != "nt":
        return None
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD), ("min_working", c.c_size_t), ("max_working", c.c_size_t), ("active_processes", w.DWORD), ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
    class Extended(c.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t), ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]
    k = c.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    k.SetInformationJobObject.restype = w.BOOL
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k.AssignProcessToJobObject.restype = w.BOOL
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise c.WinError(c.get_last_error())
    info = Extended()
    info.basic.flags = 0x00002000
    process = None
    try:
        if not k.SetInformationJobObject(job, 9, c.byref(info), c.sizeof(info)):
            raise c.WinError(c.get_last_error())
        process = k.OpenProcess(0x0001 | 0x0100, False, process_id)
        if not process or not k.AssignProcessToJobObject(job, process):
            raise c.WinError(c.get_last_error())
        return int(job)
    except Exception:
        k.CloseHandle(job)
        raise
    finally:
        if process:
            k.CloseHandle(process)


def _close_job(handle):
    if handle is not None and os.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w
        k = c.WinDLL("kernel32", use_last_error=True)
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.CloseHandle(handle)


def _create_helper_stop_event(epoch):
    if os.name != "nt":
        return None, None
    import ctypes as c
    from ctypes import wintypes as w
    k = c.WinDLL("kernel32", use_last_error=True)
    k.CreateEventW.argtypes = [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
    k.CreateEventW.restype = w.HANDLE
    name = "Local\\9FBG_Stop_" + epoch
    handle = k.CreateEventW(None, True, False, name)
    if not handle:
        raise c.WinError(c.get_last_error())
    return int(handle), name


def _signal_helper_stop_event(handle):
    if os.name == "nt" and handle is not None:
        import ctypes as c
        from ctypes import wintypes as w
        k = c.WinDLL("kernel32", use_last_error=True)
        k.SetEvent.argtypes = [w.HANDLE]
        k.SetEvent.restype = w.BOOL
        return bool(k.SetEvent(handle))
    return False


def _existing_sdk_helpers():
    """Read process inventory without opening any USB handle or old runtime."""
    if os.name != "nt":
        return []
    import ctypes as c
    from ctypes import wintypes as w
    class Entry(c.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD), ("heap", c.c_size_t), ("module", w.DWORD), ("threads", w.DWORD), ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD), ("exe", w.WCHAR * 260)]
    k = c.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    k.Process32FirstW.restype = w.BOOL
    k.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    k.Process32NextW.restype = w.BOOL
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot == c.c_void_p(-1).value:
        raise RuntimeError("无法核查光谱采集子进程占用状态。")
    entries = []
    try:
        item = Entry()
        item.size = c.sizeof(item)
        more = k.Process32FirstW(snapshot, c.byref(item))
        while more:
            entries.append({"pid": int(item.pid), "parent_pid": int(item.parent), "name": item.exe})
            more = k.Process32NextW(snapshot, c.byref(item))
    finally:
        k.CloseHandle(snapshot)
    pids = {item["pid"] for item in entries}
    return [{**item, "parent_alive": item["parent_pid"] in pids} for item in entries if item["name"].lower() == "bayspecsdkstream.exe"]


class DeviceLease:
    """The same OS byte lock used by TOUCH source, across ports and processes."""
    def __init__(self, kind, root=None, legacy_ports=(8660, 8765)):
        default = Path(os.environ.get("LOCALAPPDATA", str(APP_ROOT / "userdata"))) / "TOUCHmFBGShared/device-leases"
        self.path = Path(root or default) / f"{kind}.lock"
        self.kind, self.legacy_ports, self.stream = kind, tuple(legacy_ports), None

    def acquire(self):
        if self.stream is not None:
            return
        if self.kind == "bayspec":
            helpers = _existing_sdk_helpers()
            if helpers:
                owners = "、".join(str(item["pid"]) + ("（父进程已退出）" if not item["parent_alive"] else "") for item in helpers)
                raise RuntimeError("已有光谱采集子进程占用设备：PID " + owners + "；请先停止该采集，未打开新设备。")
        # Older accepted TOUCH versions do not participate in the file lease.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for port in self.legacy_ports:
            try:
                with opener.open(f"http://127.0.0.1:{int(port)}/api/health", timeout=5) as response:
                    status = json.loads(response.read(200000))
            except (urllib.error.URLError, OSError) as exc:
                cause = getattr(exc, "reason", exc)
                if getattr(cause, "errno", None) in {errno.ECONNREFUSED, 10061} or getattr(cause, "winerror", None) == 10061:
                    continue
                raise RuntimeError(f"无法确认端口 {port} 的旧程序设备占用状态：{exc}") from exc
            except (ValueError, TypeError) as exc:
                raise RuntimeError(f"端口 {port} 的设备占用响应无效。") from exc
            if status.get("edition_id") == "touch_mfbg_intensity_research_beta":
                source = status.get("sdk_live" if self.kind == "bayspec" else "px6d_reference") or {}
                if any(source.get(key) for key in ("active", "worker_alive", "running", "connected")):
                    raise RuntimeError(f"设备正由端口 {port} 的旧程序占用，请先受控停止采集。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b" ")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("设备已被另一个进程占用。") from exc
        self.stream = stream

    def release(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.close()


def audit_wavelength_axis(axis):
    values = np.asarray(axis, dtype=float)
    if values.shape != (512,) or not np.isfinite(values).all() or not np.all(np.diff(values) > 0):
        raise ValueError("设备波长轴必须包含 512 个有限、严格递增的实际标定点。")
    steps = np.diff(values)
    return {
        "point_count": 512,
        "strictly_increasing": True,
        "uniform": bool(np.allclose(steps, steps.mean(), rtol=1e-4, atol=1e-7)),
        "uniformity_relative_tolerance": 1e-4,
        "step_min_nm": float(steps.min()),
        "step_max_nm": float(steps.max()),
        "step_mean_nm": float(steps.mean()),
        "step_std_nm": float(steps.std()),
        "start_nm": float(values[0]), "end_nm": float(values[-1]),
        "axis_sha256": hashlib.sha256(values.astype(">f8").tobytes()).hexdigest(),
    }


class _Metrics:
    def __init__(self):
        self.received = 0
        self.errors = 0
        self.queue_drops = 0
        self.transport_duplicate_count = 0
        self.identical_value_count = 0
        self.last_error = None
        self._times = deque(maxlen=512)
        self._last_values = None

    def mark(self, values, received_ns):
        self.received += 1
        self._times.append(received_ns)
        if self._last_values == values:
            self.identical_value_count += 1
        self._last_values = list(values)

    def report(self):
        span = self._times[-1] - self._times[0] if len(self._times) > 1 else 0
        return {
            "received_frames": self.received, "invalid_frames": self.errors,
            "receive_rate_hz": (len(self._times)-1)*1e9/span if span > 0 else None,
            "actual_new_sensor_frame_rate_hz": None,
            "actual_new_sensor_frame_rate_status": "设备协议无硬件帧序号，无法证明物理新帧率",
            "duplicate_hardware_frames": None,
            "transport_duplicate_count": self.transport_duplicate_count,
            "identical_value_count": self.identical_value_count,
            "identical_value_semantics": "数值相同不等于重复帧",
            "queue_drops": self.queue_drops, "last_error": self.last_error,
        }


class BaySpecSource:
    _TIMING_FIELDS = (
        "cadence_wait_mechanism", "high_resolution_waitable_timer_active",
        "cadence_wait_fallback_reason", "cadence_wait_max_stop_check_ms",
    )

    def __init__(self, config=None):
        config = config or load_device_config()["bayspec"]
        self.config = dict(config.get("bayspec", config))
        self.helper = APP_ROOT / self.config.get("helper", "assets/sdk/BaySpecSdkStream.exe")
        self.axis_path = APP_ROOT / self.config.get("axis", "assets/sdk/wavelength_axis_F1520808_512.json")
        self.lease = DeviceLease("bayspec", self.config.get("lease_root"), self.config.get("legacy_ports", [8660, 8765]))
        self.metrics = _Metrics()
        self._queue = queue.Queue(maxsize=int(self.config.get("queue_limit", 128)))
        self.process = None
        self.worker = None
        self._status_event = threading.Event()
        self._stop = threading.Event()
        self._header = None
        self._axis = None
        self._axis_audit = None
        self._last_helper_index = None
        self._fatal_error = None
        self._overflow_frame = None
        self.overflow_stops = 0
        self._opened_ns = None
        self._last_rx_ns = None
        self._last_message_ns = None
        self._closing = False
        self.helper_exit_code = None
        self.last_stop_reason = None
        self.last_sdk_warning = None
        self.last_timing_warning = None
        self._cadence_wait_metadata = {}
        self._job_handle = None
        self._stop_event_handle = None
        self._stop_event_name = None
        self._sdk_fingerprints = None
        self.stream_epoch = None
        self.shutdown_forced = False

    def _load_axis(self):
        artifact = json.loads(self.axis_path.read_text(encoding="utf-8"))
        audit = audit_wavelength_axis(artifact["axis_nm"])
        if audit["axis_sha256"] != artifact.get("axis_sha256") or artifact.get("axis_unit") != "nm":
            raise ValueError("设备波长标定轴的哈希或单位无效。")
        if artifact.get("axis_artifact_device_id") != self.config.get("axis_artifact_device_id", "F1520808"):
            raise ValueError("波长标定轴与所选设备标识不一致。")
        source_csv = self.axis_path.parent / artifact["source_export_file"]
        if hashlib.sha256(source_csv.read_bytes()).hexdigest() != artifact.get("source_export_sha256"):
            raise ValueError("波长轴原始厂商导出文件校验失败。")
        self._axis = list(artifact["axis_nm"])
        self._axis_audit = {**audit, "source": "厂商实际设备导出标定轴，SDK不提供实时波长读回", "source_export_sha256": artifact["source_export_sha256"], "axis_artifact_device_id": artifact["axis_artifact_device_id"], "physical_device_identity_verified": False}

    def open(self):
        if self.process is not None:
            raise RuntimeError("光谱设备已经连接。")
        integration = _exact_int(self.config.get("integration_us", 70), 20, 10_000_000, "曝光时间")
        interval = _exact_int(self.config.get("interval_ms", 10), 5, 2000, "采集间隔")
        mode = _exact_int(self.config.get("sensor_mode", 1), 0, 1, "传感器模式")
        trigger = _exact_int(self.config.get("trigger_hz", 100), 1, 5000, "触发频率")
        self._load_axis()
        provenance = json.loads((self.helper.parent / "SDK_PROVENANCE.json").read_text(encoding="utf-8"))
        fingerprints = {entry["path"]: entry["sha256"] for entry in provenance["files"]}
        for asset in (self.helper, self.helper.parent / "_USB20BSDevOpDLL.dll"):
            if fingerprints.get(asset.name) != hashlib.sha256(asset.read_bytes()).hexdigest():
                raise ValueError("设备接口文件哈希与交付来源清单不一致：" + asset.name)
        self._sdk_fingerprints = {"helper_sha256": fingerprints[self.helper.name], "dll_sha256": fingerprints["_USB20BSDevOpDLL.dll"], "provenance_sha256": hashlib.sha256((self.helper.parent / "SDK_PROVENANCE.json").read_bytes()).hexdigest()}
        self.lease.acquire()
        try:
            self.metrics = _Metrics()
            self._queue = queue.Queue(maxsize=int(self.config.get("queue_limit", 128)))
            self._header = None
            self._last_helper_index = None
            self._fatal_error = None
            self._overflow_frame = None
            self.overflow_stops = 0
            self._opened_ns = time.perf_counter_ns()
            self._last_rx_ns = None
            self._last_message_ns = None
            self._closing = False
            self.helper_exit_code = None
            self.last_stop_reason = None
            self.last_sdk_warning = None
            self.last_timing_warning = None
            self._cadence_wait_metadata = {}
            self.stream_epoch = uuid.uuid4().hex
            self.shutdown_forced = False
            self._stop.clear()
            self._status_event.clear()
            self._stop_event_handle, self._stop_event_name = _create_helper_stop_event(self.stream_epoch)
            command = [str(self.helper), "--integration", str(integration), "--interval-ms", str(interval), "--sensor-mode", str(mode), "--trigger-hz", str(trigger)]
            if self._stop_event_name:
                command.extend(["--stop-event", self._stop_event_name])
            self.process = subprocess.Popen(command, cwd=self.helper.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8-sig", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._job_handle = _helper_kill_on_close_job(self.process.pid)
            self.worker = threading.Thread(target=self._read_worker, name="9FBG-BaySpec", daemon=True)
            self.worker.start()
            if not self._status_event.wait(8) or self._header is None:
                raise RuntimeError(self.metrics.last_error or "光谱设备启动超时，未收到有效设备状态。")
        except Exception:
            self.close()
            raise
        return self.diagnostics()

    def _read_worker(self):
        process = self.process
        try:
            while not self._stop.is_set():
                read_start = time.perf_counter_ns()
                line = process.stdout.readline()
                received_ns = time.perf_counter_ns()
                if not line:
                    break
                try:
                    self._consume_message(json.loads(line), received_ns, read_start)
                except (ValueError, TypeError, KeyError) as exc:
                    self.metrics.errors += 1
                    self.metrics.last_error = f"光谱帧校验失败：{exc}"
                    if self._header is None:
                        self._fatal_error = self.metrics.last_error
                        self._status_event.set()
                        break
        except (OSError, ValueError) as exc:
            self._fatal_error = "光谱采集管道异常：" + str(exc)
            self.metrics.last_error = self._fatal_error
        finally:
            self.helper_exit_code = process.poll()
            if not self._stop.is_set() and not self._closing and self._fatal_error is None:
                self.last_stop_reason = self.last_stop_reason or "unexpected_helper_eof"
                self._fatal_error = "光谱采集进程意外退出，当前数据流已停止。"
                if self.last_sdk_warning is not None:
                    self._fatal_error += " 最后一次 SDK 采集返回状态：" + str(self.last_sdk_warning.get("snapshot_result"))
                self.metrics.last_error = self._fatal_error
            self._status_event.set()

    def _update_cadence_timing(self, message):
        self._cadence_wait_metadata.update({key: message[key] for key in self._TIMING_FIELDS if key in message})
        if self._header is not None:
            # Diagnostics can outlive this call and expose the same header as
            # device_identity; replace it so later warnings cannot mutate an
            # already returned snapshot while it is being serialized.
            self._header = {**self._header, **self._cadence_wait_metadata}

    def _consume_message(self, message, received_ns=None, read_start_ns=None):
        received_ns = received_ns or time.perf_counter_ns()
        self._last_message_ns = received_ns
        kind = message.get("type")
        if kind == "error":
            self.metrics.last_error = "厂商采集接口错误：" + str(message.get("message", "未知错误"))
            self._fatal_error = self.metrics.last_error
            self.last_stop_reason = "helper_error"
            self._stop.set()
            self._status_event.set()
            return
        if kind == "stopped":
            self.last_stop_reason = message.get("reason", "helper_stopped")
            self._update_cadence_timing(message)
            return
        if kind == "timing_warning":
            self.last_timing_warning = dict(message)
            self._update_cadence_timing(message)
            return
        if kind == "status":
            required = {"device_count": 1, "device_index": 0, "spectrum_points": 512}
            if any(message.get(k) != v for k, v in required.items()) or message.get("is_open_ok", 0) <= 0:
                raise ValueError("需要且只能连接一个已打开的 512 点光谱设备。")
            if str(message.get("vid", "")).upper() != "0XBA8C" or str(message.get("pid", "")).upper() != "0X1688":
                raise ValueError("光谱设备 USB 类型与标定来源不匹配。")
            self._header = dict(message)
            self._update_cadence_timing(message)
            self._status_event.set()
            return
        if kind != "spectrum":
            if kind == "warning":
                self.last_sdk_warning = dict(message)
                self.metrics.errors += 1
                self.metrics.last_error = "光谱 SDK 本次采集未返回有效数据。"
            return
        if self._header is None or self._axis is None:
            raise ValueError("缺少有效设备状态或波长轴。")
        values = message.get("counts")
        if not isinstance(values, list) or len(values) != 512 or any(type(x) is not int or not 0 <= x <= 65535 for x in values):
            raise ValueError("原始强度必须包含 512 个 uint16 计数。")
        index = message.get("frame_index")
        if type(index) is not int or index < 0 or message.get("snapshot_result", 0) <= 0:
            raise ValueError("SDK 采集尝试标识无效。")
        if self._last_helper_index is not None and index <= self._last_helper_index:
            self.metrics.transport_duplicate_count += 1
            raise ValueError("SDK 传输尝试标识重复或倒退；硬件新帧状态未知。")
        self._last_helper_index = index
        self._update_cadence_timing(message)
        self.metrics.mark(values, received_ns)
        self._last_rx_ns = received_ns
        frame = {
            "wavelength_nm": list(self._axis), "intensity": list(values),
            "frame_id": self.metrics.received, "local_receive_id": self.metrics.received, "frame_id_source": "local_receive_sequence",
            "host_rx_monotonic_ns": received_ns, "utc_timestamp": utc_now(),
            "read_start_ns": read_start_ns or received_ns, "read_end_ns": received_ns,
            "read_interval_semantics": "主机管道读取区间，非曝光区间",
            "device_sequence": None, "device_timestamp_raw": None,
            "helper_attempt_index": index, "helper_timestamp_ms": message.get("timestamp_ms"),
            "helper_monotonic_ms": message.get("monotonic_ms"),
            "helper_snapshot_start_monotonic_ms": message.get("snapshot_start_monotonic_ms"),
            **self._cadence_wait_metadata,
            "time_quality": "host_rx_approx", "timestamp_source": "host_receive_monotonic",
            "stream_epoch": self.stream_epoch, "source_kind": "hardware",
            "preprocessing": {"state": "raw_sdk_uint16_counts", "raw_available": True, "smoothing": False, "background_applied": False, "baseline_correction_applied": False, "smoothing_applied": False, "factory_intensity_correction_applied": False, "average_number": 1, "saturation_level": 65535, "saturation_semantics": "uint16可表示上限，厂家物理线性范围未知", "sdk_internal_processing": "厂商DLL内部处理未公开", "integration_us_requested": self.config.get("integration_us", 70), "sensor_mode_requested": self.config.get("sensor_mode", 1), "settings_readback_verified": False},
            "wavelength_axis_audit": self._axis_audit,
        }
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # Retain this one extra accepted raw frame, stop the source, and
            # force the caller to record an incomplete acquisition. Never
            # overwrite already accepted raw spectra to make the UI keep up.
            self._overflow_frame = frame
            self.overflow_stops += 1
            self.last_stop_reason = "queue_overflow"
            self._fatal_error = "光谱采集队列已满，已停止接收；本次采集不完整，请降低采集频率或检查处理耗时。"
            self.metrics.last_error = self._fatal_error
            self._stop.set()
            _signal_helper_stop_event(self._stop_event_handle)
            if self.process is not None and self.process.stdin is not None:
                try:
                    self.process.stdin.write("stop\n")
                    self.process.stdin.flush()
                except (OSError, ValueError):
                    pass

    def read(self):
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            if self._overflow_frame is not None:
                frame, self._overflow_frame = self._overflow_frame, None
                return frame
            if self._fatal_error:
                raise RuntimeError(self._fatal_error)
            age = self._rx_age_ms()
            if self.process is not None and age is not None and age > float(self.config.get("frame_watchdog_sec", 2.0))*1000:
                self._fatal_error = "光谱设备超过 2 秒未返回新有效采集帧，已停止本次采集；请检查 USB 或厂商接口。"
                self.last_stop_reason = "frame_watchdog_timeout"
                self.metrics.last_error = self._fatal_error
                self.close()
                raise RuntimeError(self._fatal_error)
            return None

    def _rx_age_ms(self):
        anchor = self._last_rx_ns if self._last_rx_ns is not None else self._opened_ns
        return max(0.0, (time.perf_counter_ns()-anchor)/1e6) if anchor is not None else None

    def drain_received(self):
        """Pure memory drain; call after close to retain every accepted tail."""
        frames = []
        while True:
            try:
                frames.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if self._overflow_frame is not None:
            frame, self._overflow_frame = self._overflow_frame, None
            frames.append(frame)
        return frames

    def diagnostics(self):
        age = self._rx_age_ms()
        stale = not self._closing and self.process is not None and age is not None and age > float(self.config.get("frame_watchdog_sec", 2.0))*1000
        return {**self.metrics.report(), "connected": bool(self.process is not None and self.process.poll() is None and self._header and not self._fatal_error and not stale), "stream_epoch": self.stream_epoch, "queue_backlog": self._queue.qsize()+int(self._overflow_frame is not None), "overflow_stops": self.overflow_stops, "capture_incomplete": bool(self._fatal_error) or stale, "last_rx_age_ms": age, "frame_watchdog_sec": float(self.config.get("frame_watchdog_sec", 2.0)), "helper_reader_alive": bool(self.worker and self.worker.is_alive()), "helper_exit_code": self.process.poll() if self.process is not None else self.helper_exit_code, "last_stop_reason": self.last_stop_reason, "last_sdk_warning": self.last_sdk_warning, "last_timing_warning": self.last_timing_warning, "sdk_status_raw": self._header, "sdk_assets": self._sdk_fingerprints, "configuration": dict(self.config), "wavelength_axis": self._axis_audit, "hardware_timestamp_available": False, "physical_serial_readback_supported": False, "device_identity": self._header, "shutdown_forced": self.shutdown_forced}

    def close(self):
        self._closing = True
        self.last_stop_reason = self.last_stop_reason or "operator_close_requested"
        process, self.process = self.process, None
        try:
            if process is not None:
                if process.poll() is None:
                    _signal_helper_stop_event(self._stop_event_handle)
                    try:
                        process.stdin.write("stop\n")
                        process.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.shutdown_forced = True
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
        finally:
            if process is not None:
                self.helper_exit_code = process.poll()
            self._stop.set()
            if self.worker and self.worker is not threading.current_thread():
                self.worker.join(timeout=1)
            try:
                if process is not None:
                    for pipe in (process.stdin, process.stdout):
                        if pipe is not None:
                            try:
                                pipe.close()
                            except (OSError, ValueError):
                                pass
            finally:
                _close_job(self._job_handle)
                self._job_handle = None
                _close_job(self._stop_event_handle)
                self._stop_event_handle = None
                self._stop_event_name = None
                self.lease.release()


def crc8(data):
    value = 0
    for item in data:
        value ^= item
        for _ in range(8):
            value = ((value << 1) ^ 0x07) & 0xFF if value & 0x80 else (value << 1) & 0xFF
    return value


def command_packet(command, argument=1, device_id=127):
    body = bytes((0xAA, 0x55, device_id, command, argument))
    return body + bytes((crc8(body),))


def parse_force_frame(frame, device_id=127, verify_address=False):
    if len(frame) != 29 or frame[:2] != b"\xAA\x55" or frame[3] != 0x03:
        raise ValueError("PX6D 帧长度、帧头或响应类型错误。")
    if crc8(frame[:-1]) != frame[-1]:
        raise ValueError(f"PX6D CRC 校验失败：计算值 {crc8(frame[:-1]):02X}，接收值 {frame[-1]:02X}。")
    if verify_address and frame[2] & 0x7F != device_id:
        raise ValueError(f"PX6D 响应地址 {frame[2]:02X} 与请求地址 {device_id:02X} 不一致。")
    values = struct.unpack("<6f", frame[4:28])
    if not all(math.isfinite(x) for x in values):
        raise ValueError("PX6D 帧包含非有限数值。")
    return list(values)


class PX6DSource:
    def __init__(self, config=None):
        config = config or load_device_config()["px6d"]
        self.config = dict(config.get("px6d", config))
        self.lease = DeviceLease("px6d", self.config.get("lease_root"), self.config.get("legacy_ports", [8660, 8765]))
        self.port = None
        self.metrics = _Metrics()
        self.stream_epoch = None
        self.firmware = None
        self._buffer = bytearray()
        self._next_poll = 0
        self.port_identity = None
        self.last_failed_packet = None
        self.last_response_address = None

    def _find_port(self):
        from serial.tools import list_ports
        matches = [p for p in list_ports.comports() if p.vid == self.config.get("usb_vid", 0x28E9) and p.pid == self.config.get("usb_pid", 0x018A) and (not self.config.get("usb_serial_number") or p.serial_number == self.config["usb_serial_number"])]
        if len(matches) != 1:
            raise RuntimeError(f"PX6D 身份匹配到 {len(matches)} 个串口，需要唯一匹配；未打开其他串口。")
        selected = matches[0]
        self.port_identity = {"port": selected.device, "vid": selected.vid, "pid": selected.pid, "usb_serial_number": selected.serial_number, "description": selected.description}
        return selected.device

    def open(self):
        if self.port is not None:
            raise RuntimeError("PX6D 已经连接。")
        import serial
        self.lease.acquire()
        try:
            port = self._find_port()
            self.port = serial.Serial(port, int(self.config.get("baud_rate", 921600)), timeout=.01, write_timeout=.25)
            self.port.reset_input_buffer()
            self.port.reset_output_buffer()
            time.sleep(.2)
            device_id = _exact_int(self.config.get("device_id", 127), 0, 127, "PX6D 设备地址")
            self._buffer.clear()
            self.port.write(command_packet(0x07, device_id=device_id))
            self.port.flush()
            reply = self._packet(13, .4)
            if reply is None or reply[3] != 0x07 or (reply[2] & 0x7F) != device_id or crc8(reply[:-1]) != reply[-1]:
                raise RuntimeError("PX6D 固件握手失败。")
            self.firmware = reply[5:12].decode("ascii", errors="replace").rstrip("\x00")
            self.metrics = _Metrics()
            self.stream_epoch = uuid.uuid4().hex
            self._next_poll = 0
        except Exception:
            self.close()
            raise
        return self.diagnostics()

    def _packet(self, length, timeout):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            start = self._buffer.find(b"\xAA\x55")
            if start >= 0:
                del self._buffer[:start]
                if len(self._buffer) >= length:
                    packet = bytes(self._buffer[:length])
                    del self._buffer[:length]
                    return packet
            elif len(self._buffer) > 1:
                del self._buffer[:-1]
            waiting = int(self.port.in_waiting or 0)
            self._buffer.extend(self.port.read(min(waiting, 512) if waiting else 1))
        return None

    def read(self):
        if self.port is None:
            return None
        now = time.perf_counter()
        if now < self._next_poll:
            return None
        poll_hz = float(self.config.get("poll_hz", 50))
        if not math.isfinite(poll_hz) or not 1 <= poll_hz <= 100:
            raise ValueError("PX6D 轮询频率必须为 1～100 Hz。")
        self._next_poll = now + 1 / poll_hz
        start_ns = time.perf_counter_ns()
        try:
            device_id = self.config.get("device_id", 127)
            # A timeout invalidates the pending response; never reuse it as a
            # new request's current response on the next call.
            self.port.reset_input_buffer()
            self._buffer.clear()
            self.port.write(command_packet(0x05, device_id=device_id))
            self.port.flush()
            raw = self._packet(29, float(self.config.get("read_timeout_sec", .2)))
            received_ns = time.perf_counter_ns()
            if raw is None:
                raise TimeoutError("PX6D 当前帧读取超时。")
            try:
                # Preserve the prior working native USB reader's contract:
                # the response's address is exposed, not assumed to echo the
                # configured query address. CRC remains mandatory. Physical
                # endpoint selection uses the unique USB serial identity.
                values = parse_force_frame(raw, device_id, bool(self.config.get("verify_response_device_id", False)))
            except ValueError as exc:
                self.last_failed_packet = {"raw_packet_hex": raw.hex(), "length": len(raw), "request_device_id": device_id, "response_device_id": raw[2] if len(raw) > 2 else None, "calculated_crc": crc8(raw[:-1]) if raw else None, "received_crc": raw[-1] if raw else None, "read_start_ns": start_ns, "host_rx_monotonic_ns": received_ns, "utc_timestamp": utc_now(), "reason": str(exc)}
                raise
            self.last_response_address = raw[2]
            self.metrics.mark(values, received_ns)
            return {"force_values": values, "axes": list(AXES), "units": ["N", "N", "N", "N·m", "N·m", "N·m"], "frame_id": self.metrics.received, "local_receive_id": self.metrics.received, "frame_id_source": "local_receive_sequence", "device_sequence": None, "device_timestamp_raw": None, "host_rx_monotonic_ns": received_ns, "utc_timestamp": utc_now(), "read_start_ns": start_ns, "read_end_ns": received_ns, "roundtrip_ms": (received_ns-start_ns)/1e6, "time_quality": "host_rx_approx", "timestamp_source": "host_receive_monotonic", "stream_epoch": self.stream_epoch, "source_kind": "hardware", "raw_packet_hex": raw.hex(), "request_device_id": device_id, "response_device_id": raw[2], "response_address_verified": bool(self.config.get("verify_response_device_id", False)), "preprocessing": {"software_tare_applied": False, "filter_applied": False, "hardware_zero_command_sent": False}}
        except (ValueError, TimeoutError, OSError) as exc:
            self.metrics.errors += 1
            self.metrics.last_error = str(exc)
            raise

    def diagnostics(self):
        return {**self.metrics.report(), "connected": self.port is not None and self.port.is_open, "stream_epoch": self.stream_epoch, "port_identity": self.port_identity, "firmware": self.firmware, "hardware_timestamp_available": False, "queue_backlog": 0, "last_failed_packet": self.last_failed_packet, "last_failed_packet_hex": (self.last_failed_packet or {}).get("raw_packet_hex"), "last_response_address": self.last_response_address, "response_address_verification": "与既有工作USB协议一致，默认不假定响应地址回显请求；保留响应地址，CRC强制校验"}

    def close(self):
        port, self.port = self.port, None
        try:
            if port is not None:
                for method in ("cancel_read", "cancel_write"):
                    try:
                        getattr(port, method)()
                    except (AttributeError, OSError):
                        pass
                port.close()
        finally:
            self.lease.release()
