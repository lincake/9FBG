"""Two independent readers, bounded processing, append-only storage and snapshots."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import queue
import threading
import time

from . import PRODUCT, VERSION
from .storage import SessionWriter, clean
from .tracking import PeakTracker
from .experiment import ExperimentManager
from .analysis import AnalysisController
from .validation import ValidationController
from .preferences import Preferences
from .history import SessionHistory
from .timing import AcquisitionTiming


class DisplayWriter:
    """Keep only bounded operational events; display mode never creates files."""
    def __init__(self):
        self.closed = False
        self.error = None
        self.queue = queue.Queue()
        self.events = deque(maxlen=256)

    def submit(self, kind, value):
        if self.closed:
            raise RuntimeError("显示连接已关闭")
        if kind == "events":
            self.events.append(deepcopy(value))

    def close(self, final):
        self.closed = True


def optical_request_settings(integration_us=None, sample_rate_hz=None):
    """Validate explicit requests before any device or recording is opened."""
    settings = {}
    if integration_us is not None:
        if type(integration_us) is not int or not 20 <= integration_us <= 10_000_000:
            raise ValueError("曝光时间必须为 20～10000000 μs 的整数")
        settings["integration_us"] = integration_us
    if sample_rate_hz is not None:
        if type(sample_rate_hz) is not int or not 1 <= sample_rate_hz <= 200:
            raise ValueError("采样请求频率必须为 1～200 Hz 的整数")
        if 1000 % sample_rate_hz:
            raise ValueError("采样请求频率须对应整数毫秒间隔，例如 25、50 或 100 Hz")
        settings.update(interval_ms=1000 // sample_rate_hz, trigger_hz=sample_rate_hz)
    return settings


class AcquisitionService:
    def __init__(self, root: Path, userdata: Path):
        self.root, self.userdata = root, userdata
        self.lock = threading.RLock()
        self.command_lock = threading.RLock()
        self.tracker_lock = threading.RLock()
        self.running = False
        self.stopping = False
        self.mode = None
        self.error = None
        self.tracker = PeakTracker()
        self.latest = {}
        self.latest_force = None
        self.latest_sync = None
        self.synchronizer = None
        self.writer = None
        self.sources = []
        self.workers = []
        self.stop_event = threading.Event()
        self.process_done = threading.Event()
        self.frames = queue.Queue(maxsize=256)
        self.metrics = {}
        self.rx_times = deque(maxlen=512)
        self.identities = set()
        self.identity_order = deque(maxlen=4096)
        self.previous_payload = None
        self.reference_min_rx_ns = 0
        self.new_times = deque(maxlen=512)
        self.capture_phase = "unknown"
        self.pending_confirmation = False
        self.phase_events = [(0, "unknown")]
        self.recording = False
        self.force_stop_event = threading.Event()
        self.force_stop_event.set()
        self.force_source = None
        self.force_worker = None
        self.force_error = None
        self.force_history = deque(maxlen=8)
        self.device_config = {}
        self.experiment = ExperimentManager(userdata)
        self.analysis = AnalysisController(userdata)
        self.validation = ValidationController(userdata)
        self.test_reference_freeze = False
        self.preferences = Preferences(userdata)
        self.history = SessionHistory(userdata)
        self.guided_release_after_ns = None
        self.reference_history = []
        self.reference_history_version = 0
        self.acquisition_timing = {}

    def _experiment_hook(self, method, *args):
        """Recording failures stop the experimental recorder, not live display."""
        try:
            result = getattr(self.experiment, method)(*args)
            # The force reader must never wait for optical fitting. Release
            # finished rounds on the processing thread (or explicit commands),
            # after its source-frame watermark has passed the recovery boundary.
            if method == "on_result":
                self._release_completed_guided_press()
            return result
        except Exception as exc:
            try:
                self.experiment.stop("实验记录失败：" + str(exc))
            except Exception:
                pass
            self.experiment_error = "实验记录失败：" + str(exc)

    def _release_completed_guided_press(self):
        """Release the optical press lock only after the server finishes a round.

        Readers must not take command_lock: controlled shutdown holds it while
        joining those readers. Recheck the round under the same lock order as
        snapshots so a concurrent explicit start cannot release an active press.
        """
        if not self.experiment.settings.get("guided_acquisition"):
            return
        with self.tracker_lock, self.lock, self.experiment.lock:
            if not self.experiment.running or self.experiment.trial_id is None:
                if self.capture_phase == "press" and self.tracker.status().get("press_active"):
                    self.guided_release_after_ns = time.perf_counter_ns()
                    self.capture_phase = "recovery"
                    self.phase_events.append((self.guided_release_after_ns, self.capture_phase))
                processed_ns = (self.latest.get("frame") or {}).get("host_rx_monotonic_ns", 0)
                if self.guided_release_after_ns is None or processed_ns < self.guided_release_after_ns:
                    return
                # Already acquired press frames keep their legitimate frozen
                # reference until a new recovery source frame has been fitted.
                self.tracker.end_press()
                self.guided_release_after_ns = None
                self.capture_phase = "recovery"
                self._event("press_end", reason="guided_round_finished")

    def experiment_start(self, settings):
        with self.command_lock:
            if self.analysis.busy:
                raise ValueError("模型处理尚未结束，请等待完成后再建立采集会话")
            if self.recording:
                raise ValueError("请先停止旧式连续记录，再使用独立点位采集会话")
            self.experiment_error = None
            self.experiment.start(settings, {**self.snapshot(), "tracker_config": deepcopy(self.tracker.config)})
        return self.snapshot()

    def experiment_baseline(self, confirmed):
        with self.command_lock:
            self._release_completed_guided_press()
            deadline = time.perf_counter() + .3
            while self.guided_release_after_ns is not None and time.perf_counter() < deadline:
                self.stop_event.wait(.005)
                self._release_completed_guided_press()
            if self.guided_release_after_ns is not None:
                raise ValueError("上轮光谱尾帧尚未处理完成，请稍候再标定；参考仍保持冻结")
            self.experiment.baseline_request(confirmed, self.snapshot())
            self.reference(confirmed, experiment_command=True)
        return self.snapshot()

    def experiment_trial(self, action):
        with self.command_lock:
            self.experiment.trial(action, self.snapshot())
            if action == "start":
                try:
                    self.press(True, experiment_command=True)
                except Exception:
                    self.experiment.trial("abort", self.snapshot())
                    raise
            elif action in {"removed", "abort", "finish"} and self.tracker.status().get("press_active"):
                self.press(False, experiment_command=True)
        return self.snapshot()

    def experiment_stop(self, reason="操作员停止实验会话"):
        with self.command_lock:
            if self.tracker.status().get("press_active"):
                self.press(False, experiment_command=True)
            self.experiment.stop(reason)
        return self.snapshot()

    def analysis_reference(self, confirmed):
        with self.command_lock:
            if confirmed is not True:
                raise ValueError("请明确确认接触棒已移开且当前无载")
            if self.experiment.snapshot().get("running"):
                raise ValueError("实验会话内请使用试次无载基线操作")
            if not self.running or self.stop_event.is_set() or self.mode != "hardware":
                raise ValueError("请先连接真实光谱设备，再建立独立测试参考")
            # Register the old version before a fast processor can commit the
            # newly requested reference; only that later version may authorize testing.
            with self.tracker_lock:
                self.tracker.end_press()
                self.analysis.request_truth_zero(self.snapshot())
                self.test_reference_freeze = True
                try:
                    self.reference(True)
                except Exception:
                    self.test_reference_freeze = False
                    raise
        return self.snapshot()

    def training_start(self, session_ids, method="linear"):
        with self.command_lock:
            if self.experiment.snapshot().get("running"):
                raise ValueError("请先保存结束采集会话，再开始训练")
            return self.analysis.start_job("train", session_ids, {"method": method})

    def history_snapshot(self):
        with self.command_lock:
            experiment = self.experiment.snapshot()
            return self.history.snapshot(active_session_id=experiment.get("session_id") if experiment.get("running") else None)

    def history_open(self, session_id, deleted=False):
        with self.command_lock:
            return self.history.open(session_id, deleted)

    def history_change(self, session_ids, *, restore=False):
        with self.command_lock:
            if self.analysis.busy:
                raise ValueError("模型训练或处理正在进行，请完成后再删除或恢复历史数据")
            experiment = self.experiment.snapshot()
            if experiment.get("running") or self.recording or self.stopping:
                raise ValueError("采集或保存正在进行，请先停止并保存会话，再管理历史数据")
            action = self.history.restore if restore else self.history.delete
            return action(session_ids)

    def analysis_load(self, model_id):
        with self.command_lock:
            if self.experiment.snapshot().get("running"):
                raise ValueError("请先保存结束采集会话，再载入实机验证模型")
            result = self.analysis.load(model_id)
            with self.tracker_lock:
                self.tracker.end_press()
                self.test_reference_freeze = False
            analysis = self.analysis.snapshot()
            self.validation.configure(analysis["model_sha256"], model_id)
            return result

    def validation_freeze(self, actual_point_id=None):
        with self.command_lock:
            if actual_point_id is not None and (not isinstance(actual_point_id, str)
                    or actual_point_id not in {f"P{i:02d}" for i in range(1, 26)}):
                raise ValueError("验证目标点位必须为 P01～P25，或留空")
            with self.tracker_lock, self.lock:
                state = self.snapshot()
                analysis, tracker = state["analysis"], state["tracker"]
                prediction = analysis["prediction"]
                force, sync = state.get("force") or {}, state.get("sync") or {}
                zero = analysis["truth_zero"]
                if (not state["running"] or state["mode"] != "hardware" or state["experiment"].get("running")
                        or not analysis.get("model_id") or not analysis.get("model_sha256")):
                    raise ValueError("请载入模型，连接真实设备，并在采集会话结束后进行实机验证")
                if prediction.get("comparison_ready") is not True:
                    raise ValueError("当前没有新鲜且同步的实际按压对照；请完成无载参考后按压，保持稳定再冻结")
                if sync.get("spectrum_stream_epoch") != tracker.get("stream_epoch"):
                    raise ValueError("六轴与光谱不属于同一采集时段，不能冻结")
                source_ids = sync.get("force_frame_ids") or []
                source_frames = [deepcopy(frame) for frame in (self.synchronizer.frames if self.synchronizer else [])
                                 if frame.get("stream_epoch") == sync.get("force_stream_epoch") and frame.get("frame_id") in source_ids]
                # The selected target is a human comparison label only. It is
                # added after prediction and never enters either predictor.
                evidence = {"valid": True, "target_point_id": actual_point_id,
                            "source_kind": "hardware", "stale": tracker.get("display_stale", False),
                            "age_ms": tracker.get("display_age_ms"), "force_age_ms": force.get("age_ms"),
                            "model_checksum": analysis["model_sha256"], "model_id": analysis["model_id"],
                            "frame_id": tracker.get("frame_id"), "host_rx_monotonic_ns": tracker.get("host_rx_monotonic_ns"),
                            "optical_epoch": tracker.get("stream_epoch"), "force_epoch": sync.get("force_stream_epoch"),
                            "algorithm_version": tracker.get("algorithm_version"), "config_version": tracker.get("config_version"),
                            "reference_version": tracker.get("reference_version"), "x_pm": prediction.get("x_pm"),
                            "prediction": deepcopy(prediction), "prediction_context": prediction.get("prediction_context"),
                            "truth_context": prediction.get("truth_context"), "sync": sync,
                            "force_zero": zero.get("offsets"), "force_sign": zero.get("force_sign"),
                            "force_source_frames": source_frames,
                            "test_reference_ready": analysis["test_reference"].get("ready") is True,
                            "truth_zero_ready": zero.get("ready") is True,
                            "experiment_running": state["experiment"].get("running") is True,
                            "reference_frozen": tracker.get("reference_frozen") is True}
            # Fsync runs after releasing acquisition locks. The captured values
            # and matching raw force frames are immutable independent copies.
            sample = self.validation.capture(evidence)
            return {"sample": sample, "validation": self.validation.snapshot(analysis["model_id"], analysis["model_sha256"])}

    def _freeze_independent_reference(self, result, frame):
        """Called under tracker_lock after an explicitly requested reference."""
        if not self.test_reference_freeze:
            return
        with self.analysis.lock:
            requested = deepcopy(self.analysis.test_reference)
        if (requested.get("pending") and not result.get("reference_pending")
                and result.get("reference_version", 0) > requested.get("previous_reference_version", 0)
                and frame.get("stream_epoch") == requested.get("epoch")
                and frame.get("host_rx_monotonic_ns", 0) >= requested.get("requested_ns", 0)
                and len(result.get("peaks", [])) == 9 and all(peak.get("valid") for peak in result["peaks"])):
            self.tracker.begin_press()
            result.update(reference_frozen=True, press_active=True)
            self.test_reference_freeze = False

    def _event(self, event, **data):
        if self.writer and not self.writer.closed:
            self.writer.submit("events", {"event": event, "host_monotonic_ns": time.perf_counter_ns(), **data})

    def connect(self, mode="hardware", replay_frames=None, config=None, *, record=True,
                connect_force=True, integration_us=None, sample_rate_hz=None):
        with self.command_lock:
            if self.running or self.stopping:
                raise ValueError("当前采集尚未停止")
            if mode not in {"hardware", "replay"}:
                raise ValueError("请选择真实设备或原始数据回放")
            if mode == "replay" and not replay_frames:
                raise ValueError("请先载入原始光谱 JSONL，回放不能生成测量数据")
            if type(record) is not bool or type(connect_force) is not bool:
                raise ValueError("记录与六轴连接选项必须为布尔值")
            optical_settings = optical_request_settings(integration_us, sample_rate_hz)
            self.tracker = PeakTracker(config)
            self.mode, self.error, self.latest = mode, None, {}
            self.latest_force = self.latest_sync = None
            self.frames = queue.Queue(maxsize=256 if record else 2)
            self.stop_event.clear()
            self.process_done.clear()
            self.rx_times.clear()
            self.identities.clear()
            self.identity_order.clear()
            self.previous_payload = None
            self.reference_min_rx_ns = 0
            self.new_times.clear()
            self.capture_phase = "unknown"
            self.guided_release_after_ns = None
            self.reference_history = []
            self.reference_history_version = 0
            self.acquisition_timing = {}
            self.pending_confirmation = False
            self.phase_events = [(0, "unknown")]
            self.metrics = {"received": 0, "processed": 0, "dropped": 0, "duplicates": 0,
                            "display_skipped": 0,
                            "identical_payloads": 0, "processing_ms": 0, "processing_p95_ms": 0,
                            "receive_hz": 0, "new_frame_hz": None, "queue_depth": 0,
                            "new_frame_rate_reason": "设备未提供可验证的新帧身份", "max_queue_depth": 0}
            self.sources = []
            self.workers = []
            self.synchronizer = None
            self.recording = record
            self.force_source = self.force_worker = None
            self.force_stop_event = threading.Event()
            self.force_stop_event.set()
            self.force_error = None
            self.force_history.clear()
            self.device_config = {}
            self.writer = SessionWriter(self.userdata / "sessions", {"mode": mode, "algorithm": self.tracker.export_state()}) if record else DisplayWriter()
            self.running = True
            self._event("session_start", mode=mode)
            processor = threading.Thread(target=self._process, name="9FBG-fit", daemon=True)
            processor.start()
            self.processor = processor
            if mode == "replay":
                worker = threading.Thread(target=self._replay, args=(deepcopy(replay_frames),), name="9FBG-replay", daemon=True)
                self.workers.append(worker)
                worker.start()
            else:
                try:
                    from .devices import BaySpecSource
                    from .sync import ForceSynchronizer
                    device_config = json.loads((self.root / "config" / "device.json").read_text(encoding="utf-8-sig"))
                    self.device_config = deepcopy(device_config)
                    self.synchronizer = ForceSynchronizer()
                    spectrum = BaySpecSource({**device_config.get("bayspec", {}), **optical_settings})
                    self.sources.append(spectrum)
                    optical = threading.Thread(target=self._reader, args=(spectrum, "spectrum"), name="9FBG-optical", daemon=True)
                    self.workers.append(optical)
                    optical.start()
                    if connect_force and device_config.get("px6d", {}).get("enabled", True):
                        self._start_force()
                except Exception:
                    self.disconnect()
                    raise
        return self.snapshot()

    def _start_force(self):
        from .devices import PX6DSource
        from .sync import ForceSynchronizer
        force = PX6DSource(self.device_config.get("px6d", {}))
        with self.lock:
            if self.force_worker is not None and self.force_worker in self.workers:
                self.workers.remove(self.force_worker)
            if self.force_source is not None:
                self.force_history.append(deepcopy(self.force_source.diagnostics()))
                self.sources.remove(self.force_source)
            self.synchronizer = ForceSynchronizer()
            self.latest_sync = {"valid": False, "matched": False, "reason": "等待本次 PX6D 连接的新帧"}
            if self.latest_force:
                self.latest_force["stale"] = True
            self.force_error = None
            if self.error and self.error.startswith("PX6D 参考不可用："):
                self.error = None
            self.force_stop_event = threading.Event()
            self.force_source = force
            self.sources.append(force)
            worker = threading.Thread(target=self._reader, args=(force, "force", self.force_stop_event), name="9FBG-force", daemon=True)
            self.force_worker = worker
            self.workers.append(worker)
        worker.start()

    def connect_force(self):
        with self.command_lock:
            if not self.running or self.stopping or self.mode != "hardware" or self.stop_event.is_set():
                raise ValueError("请先连接光谱设备，再单独连接 PX6D")
            if self.force_worker and self.force_worker.is_alive():
                raise ValueError("PX6D 已连接或正在连接，请先断开")
            self._start_force()
        return self.snapshot()

    def disconnect_force(self):
        with self.command_lock:
            if self.experiment.snapshot().get("running"):
                self.experiment_stop("PX6D 断开，结束实验会话；保留已记录原始数据")
            self.force_stop_event.set()
            worker = self.force_worker
            if worker is not None:
                worker.join(timeout=8)
                if worker.is_alive():
                    raise RuntimeError("PX6D 线程尚未退出；光谱继续运行，六轴暂不能重连")
            with self.lock:
                if self.synchronizer:
                    self.synchronizer.clear()
                self.latest_sync = {"valid": False, "matched": False, "reason": "PX6D 已单独断开；光谱独立显示"}
                self._event("device_disconnect", stream="force")
        return self.snapshot()

    def _reader(self, source, kind, local_stop=None):
        # Windows Event.wait uses the process's timer resolution. The optical
        # helper's request does not give this Python process the same resolution.
        # Keep a balanced request only while an actual reader is alive.
        timing = AcquisitionTiming()
        try:
            with timing:
                with self.lock:
                    self.acquisition_timing[kind] = timing
                self._read_source(source, kind, local_stop, timing)
        finally:
            self._event("acquisition_timing_end", stream=kind, diagnostics=timing.diagnostics())

    def _read_source(self, source, kind, local_stop, timing):
        try:
            self._event("acquisition_timing_start", stream=kind, diagnostics=timing.diagnostics())
            source.open()
            self._event("device_open", stream=kind, diagnostics=source.diagnostics())
            while not self.stop_event.is_set() and not (local_stop and local_stop.is_set()):
                frame = source.read()
                if frame is None:
                    timing.wait(local_stop or self.stop_event, .002)
                    continue
                if kind == "spectrum":
                    self._receive(frame)
                else:
                    self.writer.submit("raw_force", frame)
                    self._experiment_hook("on_force", frame)
                    self.analysis.on_force(frame)
                    with self.lock:
                        self.latest_force = deepcopy(frame)
                        self.synchronizer.add(frame)
        except Exception as exc:
            with self.lock:
                self.error = ("光谱采集失败：" if kind == "spectrum" else "PX6D 参考不可用：") + str(exc)
                if kind == "force":
                    self.force_error = str(exc)
                    if local_stop:
                        local_stop.set()
                    if self.synchronizer:
                        self.synchronizer.clear()
                    self.latest_sync = {"valid": False, "matched": False, "reason": self.error}
            try:
                self._event("device_error", stream=kind, reason=str(exc))
            except Exception:
                pass
            if kind == "spectrum" or self.writer.error:
                self.stop_event.set()
            self._experiment_hook("stop", "设备异常：" + str(exc))
        finally:
            try:
                source.close()
                if kind == "spectrum" and hasattr(source, "drain_received"):
                    for accepted_frame in source.drain_received():
                        self._receive(accepted_frame)
            except Exception as exc:
                with self.lock:
                    self.error = "关闭设备失败：" + str(exc)

    def _replay(self, frames):
        try:
            for frame in frames:
                if self.stop_event.is_set():
                    break
                frame["source_kind"] = "replay"
                # Preserve acquisition time and identity; replay wall time is only pacing.
                self._receive(frame)
                self.stop_event.wait(.02)
            self._event("replay_end", frames=len(frames))
        except Exception as exc:
            self.error = "回放失败：" + str(exc)
            self.stop_event.set()

    def _receive(self, frame):
        frame = deepcopy(frame)
        with self.lock:
            # Annotations follow acquisition receive time, so adapter backlog
            # cannot acquire a later confirmation/press label.
            acquired_ns = frame.get("host_rx_monotonic_ns", 0)
            if self.mode == "hardware":
                phase = next((phase for stamp,phase in reversed(self.phase_events) if stamp <= acquired_ns), "unknown")
                eligible = acquired_ns >= self.reference_min_rx_ns
                frame["phase"] = phase
                frame["unloaded_confirmed"] = self.pending_confirmation and eligible
                if frame["unloaded_confirmed"]:
                    frame["confirmation_event_monotonic_ns"] = self.reference_min_rx_ns
                    self.pending_confirmation = False
            else:
                # Preserve original replay provenance/annotations. This run's
                # manual reference action is a separate annotation namespace.
                frame["replay_operation_phase"] = self.capture_phase
        self.writer.submit("raw_spectra", frame)
        self._experiment_hook("on_spectrum", frame)
        now = time.perf_counter_ns()
        with self.lock:
            self.metrics["received"] += 1
            self.rx_times.append(now)
            if len(self.rx_times) > 1:
                self.metrics["receive_hz"] = (len(self.rx_times) - 1) * 1e9 / (now - self.rx_times[0])
            sequence = frame.get("device_sequence")
            if sequence is not None:
                key = (frame.get("stream_epoch"), sequence)
                if key in self.identities:
                    self.metrics["duplicates"] += 1
                    self._event("duplicate_device_identity", frame_id=frame.get("frame_id"), device_sequence=sequence)
                    return
                if len(self.identity_order) == self.identity_order.maxlen:
                    self.identities.discard(self.identity_order.popleft())
                self.identity_order.append(key)
                self.identities.add(key)
                self.new_times.append(now)
                self.metrics["new_frame_rate_reason"] = "依据真实设备帧身份统计"
                self.metrics["new_frame_hz"] = ((len(self.new_times)-1)*1e9/(self.new_times[-1]-self.new_times[0])) if len(self.new_times)>1 else None
            if frame.get("intensity") == self.previous_payload:
                self.metrics["identical_payloads"] += 1
            self.previous_payload = deepcopy(frame.get("intensity"))
            try:
                self.frames.put_nowait((deepcopy(frame), now))
            except queue.Full:
                if not self.recording:
                    # Display follows the newest received spectrum. Each evicted
                    # queued item completes its queue task without becoming a
                    # hardware loss or a recorded-data processing overflow.
                    try:
                        self.frames.get_nowait()
                    except queue.Empty:
                        pass  # The processor consumed it after put_nowait.
                    else:
                        self.frames.task_done()
                        self.metrics["display_skipped"] += 1
                    self.frames.put_nowait((deepcopy(frame), now))
                else:
                    self.metrics["dropped"] += 1
                    self.error = "提峰队列已满；原始光谱已保存，该帧未参与拟合"
                    self._event("processing_overflow", frame_id=frame.get("frame_id"))
            self.metrics["max_queue_depth"] = max(self.metrics["max_queue_depth"], self.frames.qsize())

    def _process(self):
        if self.mode == "hardware":
            timing = AcquisitionTiming()
            try:
                with timing:
                    with self.lock:
                        self.acquisition_timing["processing"] = timing
                    self._process_frames(timing)
            finally:
                self._event("acquisition_timing_end", stream="processing", diagnostics=timing.diagnostics())
        else:
            self._process_frames(None)

    def _process_frames(self, timing):
        durations = deque(maxlen=256)
        try:
            while not self.process_done.is_set() or not self.frames.empty():
                self._experiment_hook("watchdog", time.perf_counter_ns())
                try:
                    frame, received_ns = self.frames.get(timeout=.1)
                except queue.Empty:
                    self._release_completed_guided_press()
                    continue
                try:
                    start = time.perf_counter_ns()
                    # Allow a future force bracket to arrive; do not match on fit completion.
                    if self.synchronizer and self.latest_force and start-frame.get("host_rx_monotonic_ns",0)<40_000_000:
                        deadline = time.perf_counter() + .035
                        while not self.stop_event.is_set() and time.perf_counter() < deadline:
                            with self.lock:
                                bracket = self.synchronizer.align(frame)
                            if bracket.get("valid"):
                                break
                            if timing:
                                timing.wait(self.stop_event, .003)
                            else:
                                self.stop_event.wait(.003)
                    with self.tracker_lock:
                        previous_version = self.tracker.status().get("reference_version")
                        eligibility_ns = frame.get("host_rx_monotonic_ns", 0) if self.mode=="hardware" else received_ns
                        if self.tracker.status().get("reference_pending") and eligibility_ns < self.reference_min_rx_ns:
                            origin={key:deepcopy(frame.get(key)) for key in ("frame_id","host_rx_monotonic_ns","time_quality","timestamp_source","stream_epoch","device_sequence","device_timestamp_raw","read_start_ns","read_end_ns")}
                            reason="该光谱早于本次无载确认；仅保存原始帧"
                            result = {**self.tracker.status(), **origin,
                                      "peaks":[{"id":f"FBG{i}","valid":False,"stale":False,"failure_reason":reason,
                                                "center_nm":None,"drift_nm":None,"amplitude":None,"fwhm_nm":None,
                                                "prominence":None,"residual":None,"fit_x":[],"fit_y":[]} for i in range(1,10)],
                                      "state": "reference_waiting", "message": reason,"processing_ms":0.0}
                        else:
                            result = self.tracker.process(frame)
                        self._freeze_independent_reference(result, frame)
                        reference_state = self.tracker.export_state() if result.get("reference_version") != previous_version else None
                        if reference_state:
                            self.reference_history = deepcopy(reference_state.get("reference_history", []))
                            self.reference_history_version = result.get("reference_version")
                    with self.lock:
                        sync = self.synchronizer.align(frame) if self.synchronizer else {"valid": False, "reason": "未接入力参考；光谱提峰独立运行"}
                        self.latest, self.latest_sync = {"frame": frame, "tracker": result}, sync
                        self.latest["received_ns"] = received_ns
                        duration = (time.perf_counter_ns() - start) / 1e6
                        durations.append(duration)
                        self.metrics.update(processed=self.metrics["processed"] + 1, processing_ms=duration,
                                            processing_p95_ms=sorted(durations)[int((len(durations)-1)*.95)])
                        self.metrics["processing_queue_age_ms"] = (start-received_ns)/1e6
                        self.metrics["fit_ms"] = result.get("processing_ms")
                        if reference_state:
                            self._event("reference_commit", frame_id=frame.get("frame_id"), state=reference_state)
                    self.writer.submit("results", {"result": result, "sync": sync, "processing_ms": duration})
                    self._experiment_hook("on_result", {**result, "source_kind": frame.get("source_kind")}, sync)
                except Exception as exc:
                    with self.lock:
                        self.error = "处理失败：" + str(exc)
                    try:
                        self._event("processing_error", frame_id=frame.get("frame_id"), reason=str(exc))
                    except Exception:
                        self.stop_event.set()
                finally:
                    self.frames.task_done()
        finally:
            pass

    def reference(self, confirmed, *, experiment_command=False):
        if self.experiment.snapshot().get("running") and not experiment_command:
            raise ValueError("实验会话中请使用点位采集区的无载基线操作")
        with self.tracker_lock, self.lock:
            if not self.running or self.stop_event.is_set():
                raise ValueError("请先启动光谱采集或回放")
            self.tracker.request_reference(unloaded_confirmed=confirmed)
            self.reference_min_rx_ns = time.perf_counter_ns()
            self.capture_phase = "unloaded"
            self.pending_confirmation = True
            self.phase_events.append((self.reference_min_rx_ns, "unloaded"))
            self._event("unloaded_confirmation", unloaded_confirmed=confirmed)
        return self.snapshot()

    def press(self, active, *, experiment_command=False):
        if self.experiment.snapshot().get("running") and not experiment_command:
            raise ValueError("实验会话中请使用试次阶段按钮")
        with self.tracker_lock, self.lock:
            if not self.running or self.stop_event.is_set():
                raise ValueError("请先启动光谱采集或回放")
            if active:
                self.tracker.begin_press()
                self.capture_phase = "press"
            else:
                self.tracker.end_press()
                self.capture_phase = "recovery"
            self.phase_events.append((time.perf_counter_ns(), self.capture_phase))
            self._event("press_start" if active else "press_end")
        return self.snapshot()

    def disconnect(self):
        with self.command_lock:
            return self._disconnect()

    def _disconnect(self):
        if not self.running:
            return self.snapshot()
        self.stopping = True
        self.stop_event.set()
        self.force_stop_event.set()
        for worker in self.workers:
            worker.join(timeout=8)
        alive = [worker.name for worker in self.workers if worker.is_alive()]
        if alive:
            self.error = "设备线程尚未退出：" + "、".join(alive) + "；不能重连"
            self.stopping = True
            return self.snapshot()
        self.process_done.set()
        self.processor.join(timeout=15)
        if self.processor.is_alive():
            self.error = "提峰线程尚未排空；不能重连"
            return self.snapshot()
        self._experiment_hook("stop", "设备已受控断开；已排空原始数据与处理队列")
        with self.lock:
            self._event("session_stop", metrics=self.metrics, error=self.error)
            self.writer.close({"metrics": self.metrics, "algorithm": self.tracker.export_state(), "error": self.error,
                               "devices": [source.diagnostics() for source in self.sources]})
            self.running = self.stopping = False
        return self.snapshot()

    def snapshot(self):
        # Keep the current reference and published result capture in the same
        # tracker -> service lock order used by reference/press commands.
        with self.tracker_lock, self.lock:
            current = self.tracker.status()
            if self.reference_history_version != current.get("reference_version"):
                self.reference_history = self.tracker.export_state().get("reference_history", [])
                self.reference_history_version = current.get("reference_version")
            reference_history = deepcopy(self.reference_history)
            tracker = deepcopy(self.latest.get("tracker", current))
            reference_changed = tracker.get("reference_version", 0) != current.get("reference_version", 0)
            for field in ("reference_pending", "press_active", "reference_frozen", "reference_version", "reference_nm"):
                tracker[field] = deepcopy(current.get(field))
            frame = deepcopy(self.latest.get("frame", {}))
            if frame:
                stamp = frame.get("host_rx_monotonic_ns") if self.mode == "hardware" else self.latest.get("received_ns")
                age = max(0, (time.perf_counter_ns() - stamp) / 1e6) if type(stamp) is int and stamp > 0 else None
                # Reception staleness is distinct from acquisition quality, including replay.
                tracker["display_age_ms"] = age
                if reference_changed or age is None or age > 1000 or not self.running or self.stop_event.is_set():
                    tracker["display_stale"] = True
                    for peak in tracker.get("peaks", []):
                        peak.update(valid=False, stale=True, age_ms=max(age or 0, peak.get("age_ms") or 0))
                        peak["failure_reason"] = ("该光谱拟合使用旧参考；等待本次参考的处理结果" if reference_changed else
                                                  peak.get("failure_reason") or ("原始光谱缺少采集接收时间" if age is None else "采集已停止或光谱超过 1 秒未更新"))
            metrics = {**self.metrics, "queue_depth": self.frames.qsize()}
            device_diagnostics = [source.diagnostics() for source in self.sources]
            metrics["input_queue_depth"] = sum(d.get("queue_backlog",0) for d in device_diagnostics)
            metrics["writer_queue_depth"] = self.writer.queue.qsize() if self.writer else 0
            if len(self.rx_times)>1:
                intervals = sorted((right-left)/1e6 for left,right in zip(self.rx_times, list(self.rx_times)[1:]))
                metrics["receive_intervals_ms"] = {name:intervals[int((len(intervals)-1)*q)] for name,q in [("p50",.5),("p95",.95),("p99",.99)]}
            tracker["reference_history"] = reference_history
            force = deepcopy(self.latest_force)
            if force:
                force["age_ms"] = max(0,(time.perf_counter_ns()-force["host_rx_monotonic_ns"])/1e6)
                force["stale"] = force.get("stale") is True or force["age_ms"]>250 or not self.running or self.stop_event.is_set() or self.force_stop_event.is_set()
            experiment = self.experiment.snapshot()
            baseline = experiment.get("baseline") or {}
            experiment["live_force"] = {"valid": False, "force_N": None, "reason": "等待本次无载基线及新鲜 PX6D 数据"}
            if baseline.get("ready") and baseline.get("force_zero") and force and not force.get("stale") and force.get("stream_epoch") == baseline.get("force_epoch"):
                normal = experiment.get("settings", {}).get("force_sign", -1) * (force["force_values"][2] - baseline["force_zero"][2])
                experiment["live_force"] = {"valid": True, "force_N": normal, "raw_Fz_N": force["force_values"][2],
                                            "zero_Fz_N": baseline["force_zero"][2], "age_ms": force["age_ms"],
                                            "over_provisional_range": normal > experiment.get("settings", {}).get("max_force_N", 3),
                                            "reason": "最新六轴接收值减本次零点；不是光谱同步标签或模型预测"}
            if getattr(self, "experiment_error", None):
                experiment["error"] = self.experiment_error
            analysis = self.analysis.snapshot(tracker, force, deepcopy(self.latest_sync), experiment, source_kind=frame.get("source_kind"))
            validation = self.validation.snapshot(analysis.get("model_id"), analysis.get("model_sha256"))
            response = {"product": PRODUCT, "version": VERSION, "running": self.running,
                          "stopping": self.stopping, "mode": self.mode, "error": self.error,
                          "tracker": tracker, "frame": frame, "metrics": metrics,
                          "force": force, "sync": deepcopy(self.latest_sync),
                          "recording": self.recording and self.running,
                          "force_error": self.force_error,
                          "force_connected": bool(self.running and not self.stop_event.is_set() and not self.force_stop_event.is_set() and self.force_source and self.force_source.diagnostics().get("connected")),
                          "force_connecting": bool(self.force_worker and self.force_worker.is_alive() and self.force_source and not self.force_source.diagnostics().get("connected") and not self.force_stop_event.is_set()),
                          "force_diagnostic_history": list(self.force_history),
                          "acquisition_timing": {key: value.diagnostics() for key, value in self.acquisition_timing.items()},
                          "devices": device_diagnostics,
                          "experiment": experiment, "analysis": analysis, "validation": validation,
                          "session": self.writer.status() if self.writer and self.recording else None}
        # JSON-safe copying must not extend the service lock held by readers.
        return clean(response)
