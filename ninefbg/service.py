"""Two independent readers, bounded processing, append-only storage and snapshots."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
import math
from pathlib import Path
import queue
import threading
import time

from . import PRODUCT, VERSION
from .storage import SessionWriter, clean
from .tracking import AUTO_REFERENCE_SOURCE, PeakTracker
from .experiment import ExperimentManager, _settings, _context
from .analysis import AnalysisController
from .validation import ValidationController
from .preferences import Preferences
from .history import SessionHistory
from .timing import AcquisitionTiming
from .data_number import DataNumberAllocator, AUTOMATIC_SOURCE
from .pulse import PulseBuffer, binding_fields, binding_key, initialization_reason


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
        self.pulse_buffer = PulseBuffer()
        self.latest = {}
        self.latest_force = None
        self.latest_sync = None
        self.synchronizer = None
        from .sync import ForceSynchronizer
        self.spectral_synchronizer = ForceSynchronizer(max_gap_ms=60, max_age_ms=45)
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
        from .model_history import ModelHistory
        self.model_history = ModelHistory(userdata)
        self.test_reference_freeze = False
        self.preferences = Preferences(userdata)
        self.history = SessionHistory(userdata)
        self.data_numbers = DataNumberAllocator(userdata, self.preferences.values)
        self.guided_release_after_ns = None
        self.reference_history = []
        self.reference_history_version = 0
        self.acquisition_timing = {}
        self._spectral_coord_stop = threading.Event()
        self._spectral_coordinator = None
        self._spectral_pause_reason = None
        self._spectral_experiment_reference_id = None

    def _wrench_protocol(self):
        return self.experiment.settings.get("acquisition_protocol") in {"timed_wrench_v1", "force_target_wrench_v1"}

    def _ensure_spectral_coordinator(self):
        if self._spectral_coordinator is None:
            self._spectral_coordinator = threading.Thread(target=self._coordinate_spectral,
                name="9FBG-spectral-coordination", daemon=True)
            self._spectral_coordinator.start()

    def _coordinate_spectral(self):
        """Wait for a batch-boundary pause without blocking commands or readers."""
        while not self._spectral_coord_stop.wait(.02):
            try:
                self.analysis._refresh_spectral_training()
                if self.experiment.settings.get("acquisition_protocol") == "force_target_wrench_v1":
                    if (self.experiment.drift_results_checkpoint_ready()
                            and self.experiment.recording_checkpoint_acknowledged()):
                        self.analysis.spectral_runtime.invalidate("本轮已保存并建立下一轮参考，重新预热")
                        self._spectral_experiment_reference_id = None
                # The coordinator needs no chart, fitted curve or UI history.
                # Building those while holding the experimental reader lock at
                # 50 Hz can delay both independently acquired source streams.
                experiment = self.experiment.coordination_snapshot()
                wrench = experiment.get("acquisition_protocol") in {"timed_wrench_v1", "force_target_wrench_v1"}
                trial_id = experiment.get("trial_id") if wrench and experiment.get("running") else None
                reason = "acquisition:" + trial_id if trial_id else None
                if self._spectral_pause_reason and self._spectral_pause_reason != reason:
                    self.analysis.training_control("resume", self._spectral_pause_reason)
                    self._spectral_pause_reason = None
                if reason:
                    if self._spectral_pause_reason != reason:
                        self.analysis.training_control("pause", reason)
                        self._spectral_pause_reason = reason
                    training = self.analysis.training
                    if not self.analysis.busy or training.get("status") == "paused":
                        self.experiment.training_pause_acknowledged()
            except Exception as exc:
                self.experiment_error = "采集保护协调失败：" + str(exc)

    def close(self):
        self.disconnect()
        self._spectral_coord_stop.set()
        if self._spectral_coordinator is not None:
            self._spectral_coordinator.join(3)
        self.analysis.close()

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
            self.analysis._refresh_spectral_training()
            if self.analysis.busy and self.analysis.training.get("method") not in {"spectral_gru", "drift_gru"}:
                raise ValueError("模型处理尚未结束，请等待完成后再建立采集会话")
            if self.recording:
                raise ValueError("请先停止旧式连续记录，再使用独立点位采集会话")
            if not self.experiment.running and self.experiment.writer is not None and self.experiment.writer.closed:
                self._complete_data_number()
            self.experiment_error = None
            context = {**self.snapshot(), "tracker_config": deepcopy(self.tracker.config)}
            if settings.get("acquisition_protocol") in {"timed_wrench_v1", "force_target_wrench_v1"}:
                from .spectral_data import _sample_rate
                rate = _sample_rate({"acquisition_audit": {"devices": context.get("devices", [])}})
                if rate != 25:
                    raise ValueError("MLP＋GRU 六维采集请先明确设置光谱请求为 25 Hz 并重新连接；不能自动热改设备")
            if "data_id" not in settings:
                # Fail pure settings/device validation before creating a lease
                # file; a failed start never consumes a number or connects IO.
                checked = _settings(settings)
                timed = checked.get("acquisition_protocol") in {"timed_wrench_v1", "force_target_wrench_v1"}
                _context(context, stable=not timed, spectrum_only=timed)
                settings = {**settings, "data_id": self.data_numbers.begin(), "data_id_source": AUTOMATIC_SOURCE}
            elif "data_id_source" in settings:
                settings = {key: value for key, value in settings.items() if key != "data_id_source"}
            try:
                self.experiment.start(settings, context)
                if settings.get("data_id_source") == AUTOMATIC_SOURCE:
                    self.data_numbers.bind(self.experiment.session_id)
            except BaseException as start_error:
                if settings.get("data_id_source") == AUTOMATIC_SOURCE:
                    try:
                        if self.experiment.running and self.experiment.settings.get("data_id_source") == AUTOMATIC_SOURCE:
                            self.experiment.error = "自动数据编号会话建立失败，保留已写入数据但不计为完整保存"
                            self.experiment.stop("自动数据编号会话建立失败")
                    except BaseException as cleanup_error:
                        raise start_error from cleanup_error
                    finally:
                        self.data_numbers.release()
                raise
            if self.experiment.settings.get("input_kind") == "delta_lambda_9":
                # Keep the same Queue object: the processor owns its task_done.
                # A bounded capture queue preserves fitted rows while readers
                # remain non-blocking; overflow makes the round explicitly bad.
                with self.frames.mutex:
                    self.frames.maxsize = 256
            if self._wrench_protocol():
                self.analysis.spectral_runtime.invalidate("已建立新采集会话，等待本轮无载参考")
                self._spectral_experiment_reference_id = None
                self._ensure_spectral_coordinator()
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
            self._spectral_experiment_reference_id = None
            self.reference(confirmed, experiment_command=True)
        return self.snapshot()

    def experiment_trial(self, action):
        with self.command_lock:
            self.experiment.trial(action, self.snapshot())
            if self._wrench_protocol():
                if action in {"start", "abort", "finish", "end"}:
                    self.analysis.spectral_runtime.invalidate("试次边界已改变，等待 16 个连续源帧", reference=False)
                self._ensure_spectral_coordinator()
                return self.snapshot()
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
            if self.experiment.running and self.experiment.settings.get("input_kind") == "delta_lambda_9":
                target = self.experiment.begin_result_drain()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    with self.experiment.lock:
                        drained = self.experiment._drift_result_watermark_ns >= target
                    if drained:
                        break
                    processor = getattr(self, "processor", None)
                    if processor is None or not processor.is_alive():
                        break
                    time.sleep(.005)
                with self.experiment.lock:
                    if self.experiment._drift_result_watermark_ns < target:
                        self.experiment.error = "九路峰心处理队列未排空；保留数据但不能宣布会话完整"
            try:
                self.experiment.stop(reason)
            except BaseException:
                # close() may have stopped the session before hashing or the
                # manifest write failed. Release its tentative number without
                # committing it, and preserve the original save exception.
                if not self.experiment.running:
                    self.data_numbers.release()
                raise
            if not self.recording:
                with self.frames.mutex:
                    self.frames.maxsize = 2
            if self._wrench_protocol():
                self.analysis.spectral_runtime.invalidate("采集会话已结束，请明确确认无接触并建立独立测试参考")
                self._spectral_experiment_reference_id = None
            self._complete_data_number()
        return self.snapshot()

    def _complete_data_number(self):
        writer = self.experiment.writer
        try:
            if writer is not None and writer.closed and not self.experiment.running:
                self.data_numbers.complete_session(self.experiment.session_id, writer.path)
        finally:
            if not self.experiment.running:
                self.data_numbers.release()

    def analysis_reference(self, confirmed):
        with self.command_lock:
            if confirmed is not True:
                raise ValueError("请明确确认接触棒已移开且当前无载")
            if self.experiment.snapshot().get("running"):
                raise ValueError("实验会话内请使用试次无载基线操作")
            self.analysis.require_reference_enabled()
            if not self.running or self.stop_event.is_set() or self.mode != "hardware":
                raise ValueError("请先连接真实光谱设备，再建立独立测试参考")
            if self.analysis.is_spectral():
                if self.analysis.spectral_runtime.is_drift():
                    # Fixed FBG identities need the existing explicitly
                    # confirmed tracker initialization after a fresh connect.
                    # Request it before the independent 2 s lambda0 request so
                    # reference() cannot invalidate that new runtime request.
                    with self.tracker_lock:
                        self.tracker.end_press()
                        self.test_reference_freeze = True
                        try:
                            self.reference(True)
                            self.analysis.request_truth_zero(self.snapshot())
                        except Exception:
                            self.test_reference_freeze = False
                            raise
                    return self.snapshot()
                self.analysis.request_truth_zero(self.snapshot())
                self.test_reference_freeze = False
                return self.snapshot()
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

    def analysis_stop(self):
        with self.command_lock:
            if self.experiment.snapshot().get("running"):
                raise ValueError("采集会话正在运行，请先停止并保存采集会话，再结束推理")
            if self.stopping:
                raise ValueError("设备或采集正在停止保存，请等待完成后再结束推理")
            if self.validation.snapshot().get("pending_sample"):
                raise ValueError("请先保存本次判断或选择重新判断，再结束推理")
            self.analysis.stop_inference()
            with self.tracker_lock:
                self.tracker.end_press()
                self.test_reference_freeze = False
            self._spectral_experiment_reference_id = None
            return self.snapshot()

    def training_start(self, session_ids, method="linear", options=None):
        with self.command_lock:
            experiment = self.experiment.snapshot()
            if experiment.get("running") and method not in {"spectral_gru", "drift_gru"}:
                raise ValueError("请先保存结束采集会话，再开始训练")
            if experiment.get("running") and (not self._wrench_protocol() or experiment.get("trial_id")):
                raise ValueError("MLP＋GRU 训练请在六维采集的试次间隙启动；活动轮次保持采集保护")
            result = self.analysis.start_job("train", session_ids, {**(options or {}), "method": method})
            if method in {"spectral_gru", "drift_gru"}:
                self._ensure_spectral_coordinator()
            return result

    def training_control(self, action):
        with self.command_lock:
            return {"training": self.analysis.training_control(action, "user")}

    def history_snapshot(self):
        with self.command_lock:
            experiment = self.experiment.snapshot()
            return self.history.snapshot(active_session_id=experiment.get("session_id") if experiment.get("running") else None)

    def history_open(self, session_id, deleted=False):
        with self.command_lock:
            return self.history.open(session_id, deleted)

    def history_change(self, session_ids, *, restore=False):
        with self.command_lock:
            self.analysis._refresh_spectral_training()
            if self.analysis.busy:
                raise ValueError("模型训练或处理正在进行，请完成后再删除或恢复历史数据")
            experiment = self.experiment.snapshot()
            if experiment.get("running") or self.recording or self.stopping:
                raise ValueError("采集或保存正在进行，请先停止并保存会话，再管理历史数据")
            action = self.history.restore if restore else self.history.delete
            return action(session_ids)

    def analysis_load(self, model_id, unloaded_confirmed=False):
        with self.command_lock:
            experiment = self.experiment.snapshot()
            spectral_candidate = isinstance(model_id, str) and (model_id.startswith("S") or (self.userdata / "spectral_models" / model_id).is_dir())
            if experiment.get("running") and (not spectral_candidate or not self._wrench_protocol() or experiment.get("trial_id")):
                raise ValueError("请先保存结束采集会话，再载入模型进行模型推理")
            if spectral_candidate:
                zero = (experiment.get("baseline") or {}) if experiment.get("running") else self.analysis.truth_zero
                offsets = zero.get("force_zero", zero.get("offsets"))
                epoch = zero.get("force_epoch", zero.get("epoch"))
                force = self.latest_force
                fresh = bool(force and force.get("stream_epoch") == epoch and
                    0 <= (time.perf_counter_ns()-force.get("host_rx_monotonic_ns", 0))/1e6 <= 250 and
                    not force.get("stale") and not self.force_stop_event.is_set())
                if zero.get("ready") and offsets and fresh:
                    norm = math.sqrt(sum((a-b)**2 for a,b in zip(force["force_values"][:3], offsets[:3])))
                    if norm > .1:
                        raise ValueError("当前合力尚未接近有效无载零点，请完全卸载后切换模型")
                elif unloaded_confirmed is not True:
                    raise ValueError("切换全谱模型前，请明确确认当前无接触；新模型应用后重新建立 S0")
            result = self.analysis.load(model_id)
            self._spectral_experiment_reference_id = None
            with self.tracker_lock:
                self.tracker.end_press()
                self.test_reference_freeze = False
            analysis = self.analysis.snapshot()
            self.validation.configure(analysis["model_sha256"], model_id)
            return {**result, **self.snapshot()}

    def _model_management_block(self):
        self.analysis._refresh_spectral_training()
        if self.analysis.busy:
            return "模型训练或处理正在进行，请完成后再删除或恢复模型"
        if self.experiment.snapshot().get("running") or self.recording or self.stopping:
            return "采集或保存正在进行，请先停止并保存会话，再管理模型"
        if self.validation.has_pending():
            return "请先保存本次判断或选择重新判断，再删除或恢复模型"
        return None

    def models_snapshot(self):
        # Read-only catalogs use their own asset cache/transfer lock. Waiting
        # behind a device command can otherwise turn a healthy listing into a
        # timeout. All model mutations still recheck the live command guards.
        blocked = self._model_management_block()
        with self.analysis.lock:
            active = self.analysis.model_id if self.analysis.inference_active else None
        return self.model_history.cached_snapshot(self.analysis.models,
            in_use_model_id=active, blocked_reason=blocked)

    def models_change(self, model_ids, *, restore=False):
        with self.command_lock:
            blocked = self._model_management_block()
            if blocked:
                raise ValueError(blocked)
            with self.analysis.lock:
                active = self.analysis.model_id if self.analysis.inference_active else None
            result = (self.model_history.restore(model_ids) if restore else
                      self.model_history.delete(model_ids, in_use_model_id=active))
            if not restore and self.analysis.forget_models(result["deleted_ids"]):
                self.validation.detach_model()
                self._spectral_experiment_reference_id = None
            key = "restored_ids" if restore else "deleted_ids"
            return {**self.snapshot(), **self.models_snapshot(), key: result[key]}

    def validation_freeze(self, actual_point_id=None, wrench_tolerances=None):
        with self.command_lock:
            if self.analysis.is_spectral():
                return self._spectral_validation_freeze(actual_point_id, wrench_tolerances)
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
                    raise ValueError("请载入模型，连接真实设备，并在采集会话结束后进行模型推理")
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

    def _spectral_comparison(self, analysis, force, experiment):
        prediction = analysis["prediction"]
        prediction.update(comparison_ready=False, wrench_truth=None, wrench_error=None)
        if not prediction.get("valid"):
            return
        baseline = experiment.get("baseline") or {}
        if experiment.get("running"):
            reference_id = None if analysis.get("input_kind") == "delta_lambda_9" else (baseline.get("spectral_reference") or {}).get("reference_id")
            if reference_id and prediction.get("reference_id") != reference_id:
                prediction.update(valid=False, status="预热", point_id=None, wrench_pred=None,
                                  reason="本轮参考已更新，等待新参考的连续全谱窗口")
                return
            from .analysis import _physical_definition
            expected = analysis.get("model_definition")
            actual = _physical_definition(experiment.get("settings", {}))
            if expected and any(expected.get(key) != actual.get(key) for key in ("finger_id", "point_grid", "coordinate_definition")):
                prediction.update(valid=False, status="无效", point_id=None, reason="当前触觉件或点阵与全谱模型不一致")
                return
            zero = {"ready": baseline.get("ready"), "offsets": baseline.get("force_zero"),
                    "epoch": baseline.get("force_epoch"), "zero_id": baseline.get("baseline_id")}
        else:
            zero = analysis["truth_zero"]
        offsets = zero.get("offsets")
        if not zero.get("ready") or not offsets or not force or force.get("stale"):
            return
        # Snapshot construction does not own the acquisition lock. Only the
        # synchronizer and its exact matched source records need this lock;
        # prediction/report formatting must not hold up the PX6D reader.
        with self.lock:
            sync = self.spectral_synchronizer.align({"frame_id": prediction["frame_id"], "stream_epoch": prediction["stream_epoch"],
                "host_rx_monotonic_ns": prediction["host_rx_monotonic_ns"], "time_quality": "host_rx_approx"})
            source_ids = sync.get("force_frame_ids", [])
            sources = [deepcopy(f) for f in self.spectral_synchronizer.frames if f.get("frame_id") in source_ids and f.get("stream_epoch") == zero.get("epoch")]
        if not sync.get("valid") or sync.get("matched") is not True or sync.get("method") not in {"exact_receive_time", "linear_receive_time"} or sync.get("force_stream_epoch") != zero.get("epoch"):
            return
        stamp = prediction["host_rx_monotonic_ns"]
        times = sorted(f["host_rx_monotonic_ns"] for f in sources)
        if sync.get("method") == "linear_receive_time" and (len(times) != 2 or
                not times[0] < stamp < times[1] or times[1] - times[0] > 60_000_000 or
                max(stamp - times[0], times[1] - stamp) > 45_000_000):
            return
        truth = [value - offset for value, offset in zip(sync["force_values"], offsets)]
        context = {**prediction["prediction_context"], "force_epoch": zero.get("epoch"), "zero_id": zero.get("zero_id")}
        prediction.update(wrench_truth=truth, wrench_error=[a-b for a, b in zip(prediction["wrench_pred"], truth)],
            truth_force_norm_N=math.sqrt(sum(x*x for x in truth[:3])), sync=deepcopy(sync),
            force_zero=deepcopy(offsets), zero_id=zero.get("zero_id"), force_source_frames=sources,
            prediction_context=context, truth_context=deepcopy(context),
            comparison_ready=bool(not experiment.get("running") and analysis["test_reference"].get("ready") and
                                  all(0 <= (time.perf_counter_ns() - t) / 1e6 <= 250 for t in times)))

    def _spectral_validation_freeze(self, actual_point_id, tolerances):
        with self.tracker_lock, self.lock:
            state = self.snapshot()
            analysis = state["analysis"]
            prediction = analysis["prediction"]
            if state["experiment"].get("running") or state["mode"] != "hardware" or not state["running"]:
                raise ValueError("请结束并保存采集会话，在独立模型推理流程中冻结")
            if not prediction.get("valid") or not prediction.get("comparison_ready"):
                raise ValueError("当前缺少新鲜全谱窗口与独立六轴对照，请建立无载参考后实际按压")
            if prediction.get("truth_force_norm_N", 0) <= .05:
                raise ValueError("无载不能作为一次实际按压验证")
            raw = self.analysis.spectral_runtime.evidence_for(prediction["frame_id"], prediction["stream_epoch"], prediction["reference_id"])
            evidence = {"schema": "spectral_validation_evidence_v1", "valid": True, "source_kind": "hardware",
                "stale": False, "age_ms": prediction["age_ms"], "force_age_ms": (state.get("force") or {}).get("age_ms"),
                "target_point_id": actual_point_id, "model_id": analysis["model_id"],
                "model_checksum": analysis["model_sha256"], "frame_id": prediction["frame_id"],
                "host_rx_monotonic_ns": prediction["host_rx_monotonic_ns"], "optical_epoch": prediction["stream_epoch"],
                "force_epoch": prediction["sync"]["force_stream_epoch"], "reference_id": prediction["reference_id"],
                "reference_version": None, "zero_id": prediction["zero_id"], "prediction": deepcopy(prediction),
                "prediction_context": prediction["prediction_context"], "truth_context": prediction["truth_context"],
                "sync": prediction["sync"], "force_source_frames": prediction["force_source_frames"],
                "force_zero": prediction["force_zero"], "zero_reference": deepcopy(analysis["truth_zero"]),
                "wrench_tolerances": tolerances or [None]*6,
                "axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"], "units": ["N", "N", "N", "N·m", "N·m", "N·m"],
                "test_reference_ready": True, "truth_zero_ready": True, "experiment_running": False,
                "reference_frozen": True, **raw}
        sample = self.validation.capture(evidence)
        return {"sample": sample, "validation": self.validation.snapshot(analysis["model_id"], analysis["model_sha256"])}

    def _event(self, event, **data):
        if self.writer and not self.writer.closed:
            self.writer.submit("events", {"event": event, "host_monotonic_ns": time.perf_counter_ns(), **data})

    def connect(self, mode="hardware", replay_frames=None, config=None, *, record=True,
                connect_force=True, integration_us=None, sample_rate_hz=None, auto_reference=False):
        with self.command_lock:
            if self.running or self.stopping:
                raise ValueError("当前采集尚未停止")
            if mode not in {"hardware", "replay"}:
                raise ValueError("请选择真实设备或原始数据回放")
            if mode == "replay" and not replay_frames:
                raise ValueError("请先载入原始光谱 JSONL，回放不能生成测量数据")
            if type(record) is not bool or type(connect_force) is not bool:
                raise ValueError("记录与六轴连接选项必须为布尔值")
            if type(auto_reference) is not bool:
                raise ValueError("连接后自动九峰参考选项必须为布尔值")
            if auto_reference and mode != "hardware":
                raise ValueError("连接自动九峰参考仅用于本次真实光谱连接，回放请使用明确确认的参考操作")
            optical_settings = optical_request_settings(integration_us, sample_rate_hz)
            self.tracker = PeakTracker(config)
            self.analysis.spectral_runtime.invalidate("光谱连接或配置已改变，请重建全谱参考")
            self._spectral_experiment_reference_id = None
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
            self.spectral_synchronizer.clear()
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
                    if auto_reference:
                        # Arm before the optical reader can publish its first
                        # frame. Adapter backlog older than this connection's
                        # request must not supply any of its stable frames.
                        with self.tracker_lock, self.lock:
                            self.tracker.request_connection_reference()
                            self.reference_min_rx_ns = time.perf_counter_ns()
                            self._event("optical_connect_auto_reference_requested",
                                        reference_source=AUTO_REFERENCE_SOURCE, unloaded_confirmed=False,
                                        minimum_source_rx_ns=self.reference_min_rx_ns)
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
                        self.spectral_synchronizer.add(frame)
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
        if (self.analysis.is_spectral() and not self.analysis.spectral_runtime.is_drift()
                and self._wrench_protocol() and self.experiment.running):
            with self.experiment.lock:
                baseline = self.experiment.baseline
                reference = baseline.get("spectral_reference") if self.experiment.running and baseline.get("ready") else None
                reference_id = reference.get("reference_id") if reference else None
                if reference_id and reference_id != self._spectral_experiment_reference_id:
                    self.analysis.spectral_runtime.set_reference(reference)
                    self._spectral_experiment_reference_id = reference_id
        self.analysis.on_spectrum(frame)
        now = time.perf_counter_ns()
        drift_overflow = False
        drift_draining = (getattr(self.experiment, "settings", {}).get("input_kind") == "delta_lambda_9"
                          and self.experiment._drift_beyond_stop(frame))
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
            if drift_draining:
                # During controlled save only previously accepted source fits
                # enter the queue. Display-only new arrivals cannot keep the
                # capture backlog growing or manufacture an overflow fault.
                self.metrics["display_skipped"] += 1
                return
            try:
                self.frames.put_nowait((deepcopy(frame), now))
            except queue.Full:
                if not self.recording and not (self.experiment.running and self.experiment.settings.get("input_kind") == "delta_lambda_9"):
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
                    if self.experiment.running and self.experiment.settings.get("input_kind") == "delta_lambda_9":
                        drift_overflow = True
            self.metrics["max_queue_depth"] = max(self.metrics["max_queue_depth"], self.frames.qsize())
        if drift_overflow:
            self._experiment_hook("drift_processing_overflow", frame)

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
                            automatic = self.tracker.status().get("pending_reference_source") == AUTO_REFERENCE_SOURCE
                            reason=("该光谱早于本次连接自动参考申请；仅保存原始帧" if automatic
                                    else "该光谱早于本次无载确认；仅保存原始帧")
                            result = {**self.tracker.status(), **origin,
                                      "peaks":[{"id":f"FBG{i}","valid":False,"stale":False,"failure_reason":reason,
                                                "center_nm":None,"drift_nm":None,"amplitude":None,"fwhm_nm":None,
                                                "prominence":None,"residual":None,"fit_x":[],"fit_y":[]} for i in range(1,10)],
                                      "state": "reference_waiting", "message": reason,"processing_ms":0.0}
                        else:
                            result = self.tracker.process(frame)
                        self._freeze_independent_reference(result, frame)
                        self.pulse_buffer.append(frame, result)
                        reference_state = self.tracker.export_state() if result.get("reference_version") != previous_version else None
                        if reference_state:
                            self.reference_history = deepcopy(reference_state.get("reference_history", []))
                            self.reference_history_version = result.get("reference_version")
                    from .drift_features import make_drift_frame
                    result["drift_features"] = make_drift_frame(result, frame)
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
                    annotation = self.experiment.source_annotation(frame.get("host_rx_monotonic_ns")) if self.experiment.running else None
                    self.analysis.on_result(result["drift_features"], annotation)
                except Exception as exc:
                    with self.lock:
                        self.error = "处理失败：" + str(exc)
                    if self.experiment.running and self.experiment.settings.get("input_kind") == "delta_lambda_9":
                        self._experiment_hook("drift_processing_failure", frame, "九路峰心处理失败；本轮派生数据不完整："+str(exc))
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
            self.analysis.spectral_runtime.invalidate("光学参考已改变，请建立独立全谱参考")
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
        self.analysis.spectral_runtime.invalidate("光谱已断开，请重连后重建全谱参考")
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
        self._complete_data_number()
        return self.snapshot()

    def pulse_reference(self, confirmed):
        """Explicit first initialization through the existing unloaded workflow."""
        with self.command_lock:
            if confirmed is not True:
                raise ValueError("请先移开手指，并明确确认当前无载后再初始化九峰参考")
            with self.tracker_lock, self.lock:
                state = {"running": self.running, "stopping": self.stopping,
                         "stopped": self.stop_event.is_set(), "mode": self.mode,
                         "recording": self.recording, "press_active": self.tracker._press,
                         "pending": self.tracker._pending_reference, "initialized": bool(self.tracker._anchors)}
            with self.experiment.lock:
                state["experiment_running"] = self.experiment.running
            with self.analysis.lock:
                state.update(inference_active=self.analysis.inference_active, busy=self.analysis.busy)
            reason = initialization_reason(**state)
            if reason:
                raise ValueError(reason)
            # Reuse the existing reference request, source-time cutoff, stable
            # checks and versioning. This never connects either instrument.
            self.reference(True)
            return self.pulse_samples()

    def pulse_samples(self, after=0, limit=4096):
        """A small read-only feed, independent of UI/analysis/history snapshots."""
        page = self.pulse_buffer.read(after, limit)
        latest = page.pop("latest")
        # Read just the reference binding, not status metrics, spectra, model
        # reports or recorder history. Use the existing tracker/service lock order.
        with self.tracker_lock, self.lock:
            tracker = self.tracker
            current = {"reference_version": tracker._reference_version,
                       "reference_nm": list(tracker._reference),
                       "config_version": tracker.config_version,
                       "algorithm_version": tracker.algorithm_version}
            pending, initialized, press_active = tracker._pending_reference, bool(tracker._anchors), tracker._press
            frame = self.latest.get("frame", {})
            current_binding = binding_key(binding_fields(frame, current))
            running, stopping, mode = self.running, self.stopping, self.mode
            recording, stopped = self.recording, self.stop_event.is_set()
        with self.experiment.lock:
            experiment_running = self.experiment.running
        with self.analysis.lock:
            inference_active, busy = self.analysis.inference_active, self.analysis.busy
        can_initialize = not initialization_reason(
            running=running, stopping=stopping, stopped=stopped, mode=mode,
            recording=recording, experiment_running=experiment_running, inference_active=inference_active,
            busy=busy, press_active=press_active, pending=pending, initialized=initialized)
        stamp = latest.get("t_ns") if latest else None
        age = max(0, (time.perf_counter_ns() - stamp) / 1e6) if type(stamp) is int and stamp > 0 else None
        if (recording and running) or experiment_running or inference_active:
            reason = "记录、实验采集或模型推理正在运行；请先结束该流程再测量脉搏"
        elif stopping or not running or stopped:
            reason = "光谱采集已停止或正在关闭"
        elif mode != "hardware":
            reason = "脉搏测量只接受当前真实设备光谱"
        elif pending or not initialized:
            reason = "等待明确无载确认与参考初始化完成"
        elif latest is None or age is None or age > 1000:
            reason = "光谱超过 1 秒未更新或缺少源帧接收时间"
        elif latest["binding"] != current_binding:
            reason = "采集段或参考已变化；等待当前绑定的新源帧"
        elif not latest["valid"]:
            reason = latest["reason"] or "当前源帧九路光栅尚未全部有效"
        else:
            reason = ""
        return {"schema": "nine_fbg_pulse_samples_v1", "version": VERSION,
                **page, "running": running, "stopping": stopping, "mode": mode,
                "blocked": bool(reason), "reason": reason, "age_ms": age,
                "latest_binding": current_binding, "can_initialize": can_initialize}

    def snapshot(self):
        # Capture the tracker/reference and published source frame together.
        # No UI report, experiment history, model work or adapter diagnostics may
        # run under this reader-shared lock. A slow GET /state must not delay the
        # next actual PX6D or optical source read.
        with self.tracker_lock, self.lock:
            current = self.tracker.status()
            if self.reference_history_version != current.get("reference_version"):
                self.reference_history = self.tracker.export_state().get("reference_history", [])
                self.reference_history_version = current.get("reference_version")
            reference_history = deepcopy(self.reference_history)
            tracker = deepcopy(self.latest.get("tracker", current))
            reference_changed = tracker.get("reference_version", 0) != current.get("reference_version", 0)
            for field in ("reference_pending", "pending_reference_source", "reference_source", "reference_unloaded_confirmed",
                          "press_active", "reference_frozen", "reference_version", "reference_nm"):
                tracker[field] = deepcopy(current.get(field))
            frame = deepcopy(self.latest.get("frame", {}))
            received_ns = self.latest.get("received_ns")
            force = deepcopy(self.latest_force)
            sync = deepcopy(self.latest_sync)
            metrics = {**self.metrics, "queue_depth": self.frames.qsize()}
            rx_times = list(self.rx_times)
            sources = tuple(self.sources)
            force_source, force_worker = self.force_source, self.force_worker
            force_history = deepcopy(list(self.force_history))
            acquisition_timing = tuple(self.acquisition_timing.items())
            writer = self.writer
            running, stopping, mode = self.running, self.stopping, self.mode
            stopped, force_stopped = self.stop_event.is_set(), self.force_stop_event.is_set()
            recording, error, force_error = self.recording, self.error, self.force_error
            experiment_error = getattr(self, "experiment_error", None)
        if frame:
            stamp = frame.get("host_rx_monotonic_ns") if mode == "hardware" else received_ns
            age = max(0, (time.perf_counter_ns() - stamp) / 1e6) if type(stamp) is int and stamp > 0 else None
            # Reception staleness is distinct from acquisition quality, including replay.
            tracker["display_age_ms"] = age
            if reference_changed or age is None or age > 1000 or not running or stopped:
                tracker["display_stale"] = True
                for peak in tracker.get("peaks", []):
                    peak.update(valid=False, stale=True, age_ms=max(age or 0, peak.get("age_ms") or 0))
                    peak["failure_reason"] = ("该光谱拟合使用旧参考；等待本次参考的处理结果" if reference_changed else
                                              peak.get("failure_reason") or ("原始光谱缺少采集接收时间" if age is None else "采集已停止或光谱超过 1 秒未更新"))
        device_diagnostics = [source.diagnostics() for source in sources]
        force_diagnostics = next((diagnostic for source, diagnostic in zip(sources, device_diagnostics)
                                  if source is force_source), {})
        metrics["input_queue_depth"] = sum(d.get("queue_backlog", 0) for d in device_diagnostics)
        metrics["writer_queue_depth"] = writer.queue.qsize() if writer else 0
        if len(rx_times) > 1:
            intervals = sorted((right-left)/1e6 for left, right in zip(rx_times, rx_times[1:]))
            metrics["receive_intervals_ms"] = {name: intervals[int((len(intervals)-1)*q)] for name, q in [("p50", .5), ("p95", .95), ("p99", .99)]}
        tracker["reference_history"] = reference_history
        if force:
            force["age_ms"] = max(0, (time.perf_counter_ns()-force["host_rx_monotonic_ns"])/1e6)
            force["stale"] = force.get("stale") is True or force["age_ms"] > 250 or not running or stopped or force_stopped
        experiment = self.experiment.snapshot()
        baseline = experiment.get("baseline") or {}
        experiment["live_force"] = {"valid": False, "force_N": None, "reason": "等待本次无载基线及新鲜 PX6D 数据"}
        if baseline.get("ready") and baseline.get("force_zero") and force and not force.get("stale") and force.get("stream_epoch") == baseline.get("force_epoch"):
            normal = experiment.get("settings", {}).get("force_sign", -1) * (force["force_values"][2] - baseline["force_zero"][2])
            experiment["live_force"] = {"valid": True, "force_N": normal, "raw_Fz_N": force["force_values"][2],
                                        "zero_Fz_N": baseline["force_zero"][2], "age_ms": force["age_ms"],
                                        "over_provisional_range": normal > experiment.get("settings", {}).get("max_force_N", 3),
                                        "reason": "最新六轴接收值减本次零点；不是光谱同步标签或模型预测"}
            if experiment.get("settings", {}).get("acquisition_protocol") in {"timed_wrench_v1", "force_target_wrench_v1"}:
                wrench = [a-b for a, b in zip(force["force_values"], baseline["force_zero"])]
                norm = math.sqrt(sum(x*x for x in wrench[:3]))
                experiment["live_force"] = {"schema": "live_wrench_v1", "valid": True,
                    "wrench": wrench, "force_norm_N": norm, "age_ms": force["age_ms"],
                    "axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"], "units": ["N", "N", "N", "N·m", "N·m", "N·m"],
                    "over_provisional_range": norm > experiment["settings"]["max_force_norm_N"],
                    "reason": "实际 PX6D 六轴减固定软件零偏；合力仅含 Fx/Fy/Fz，不含力矩"}
        if experiment_error:
            experiment["error"] = experiment_error
        analysis = self.analysis.snapshot(tracker, force, sync, experiment, source_kind=frame.get("source_kind"))
        if analysis.get("model_kind") == "spectral_gru":
            self._spectral_comparison(analysis, force, experiment)
        validation = self.validation.snapshot(analysis.get("model_id"), analysis.get("model_sha256"))
        response = {"product": PRODUCT, "version": VERSION, "running": running,
                      "stopping": stopping, "stopped": stopped, "mode": mode, "error": error,
                      "tracker": tracker, "frame": frame, "metrics": metrics,
                      "force": force, "sync": sync,
                      "recording": recording and running,
                      "force_error": force_error,
                      "force_connected": bool(running and not stopped and not force_stopped and force_diagnostics.get("connected")),
                      "force_connecting": bool(force_worker and force_worker.is_alive() and force_source and not force_diagnostics.get("connected") and not force_stopped),
                      "force_diagnostic_history": force_history,
                      "acquisition_timing": {key: value.diagnostics() for key, value in acquisition_timing},
                      "devices": device_diagnostics,
                      "experiment": experiment, "analysis": analysis, "validation": validation,
                      "data_number": self.data_numbers.snapshot(experiment),
                      "session": writer.status() if writer and recording else None}
        response = clean(response)
        # The source copies can age while a report is being formatted. Recheck
        # the response after all expensive copying, without changing acquisition
        # timestamps or borrowing a newer frame's freshness for an older value.
        now = time.perf_counter_ns()
        if frame:
            stamp = frame.get("host_rx_monotonic_ns") if mode == "hardware" else received_ns
            age = max(0, (now-stamp)/1e6) if type(stamp) is int and stamp > 0 else None
            shown_tracker = response["tracker"]
            shown_tracker["display_age_ms"] = age
            if reference_changed or age is None or age > 1000 or not running or stopped:
                shown_tracker["display_stale"] = True
                for peak in shown_tracker.get("peaks", []):
                    peak.update(valid=False, stale=True, age_ms=max(age or 0, peak.get("age_ms") or 0))
                    peak["failure_reason"] = ("该光谱拟合使用旧参考；等待本次参考的处理结果" if reference_changed else
                        peak.get("failure_reason") or "采集已停止或光谱超过 1 秒未更新")
        shown_force = response.get("force")
        prediction = response["analysis"].get("prediction", {})
        if shown_force:
            shown_force["age_ms"] = max(0, (now-shown_force["host_rx_monotonic_ns"])/1e6)
            shown_force["stale"] = (shown_force.get("stale") is True or shown_force["age_ms"] > 250
                                     or not running or stopped or force_stopped)
            if shown_force["stale"]:
                response["experiment"]["live_force"].update(valid=False, force_N=None,
                    force_norm_N=None, wrench=None, reason="PX6D 源帧已过期或连接已停止，等待新鲜六轴数据")
                prediction.update(comparison_ready=False, wrench_truth=None, wrench_error=None,
                    truth_force_N=None, truth_force_norm_N=None, error_N=None)
                prediction.pop("truth_context", None)
                zero = response["analysis"].get("truth_zero", {})
                if zero.get("ready") or zero.get("pending"):
                    zero.update(ready=False, pending=False, offsets=None,
                        reason="PX6D 已断开或过期；测试真值零点需重新确认")
        if response["analysis"].get("model_kind") == "spectral_gru":
            # Nine-peak fitting can lag the raw full-spectrum inference stream.
            # Each prediction is checked against its own last source timestamp.
            stamp = prediction.get("host_rx_monotonic_ns")
            if type(stamp) is int:
                age = (now-stamp)/1e6
                prediction["age_ms"] = age
                if age < 0 or age > 1000:
                    prediction.update(valid=False, status="无效", point_id=None,
                        comparison_ready=False, reason="推理末帧已过期，等待新的完整窗口")
        elif response["tracker"].get("display_stale") and response["analysis"].get("inference_active"):
            prediction.update(valid=False, status="无效", point_id=None, force_N=None,
                raw_force_N=None, contact=False, comparison_ready=False, truth_force_N=None,
                error_N=None, reason="当前九峰源帧已过期，等待新鲜光谱处理结果")
            prediction.pop("truth_context", None)
        return response
