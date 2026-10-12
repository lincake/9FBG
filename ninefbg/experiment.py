"""Explicit, traceable calibration recording, independent of device/display IO."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import json
import math
from pathlib import Path
import statistics
import threading
import time

from .storage import SessionWriter, clean

IDS = tuple(f"FBG{i}" for i in range(1, 10))
AXES = ("Fx", "Fy", "Fz", "Mx", "My", "Mz")
UNITS = ("N", "N", "N", "N·m", "N·m", "N·m")
GUIDED_CRITERIA = {"criteria_version": "guided_round_v1", "preload_ms": 500, "preload_timeout_ms": 8000,
                   "target_min_N": 2.9, "target_max_N": 3.1, "target_hold_ms": 200,
                   "unloaded_abs_max_N": 0.1, "unloaded_hold_ms": 400,
                   "minimum_valid_spectra": 30, "minimum_valid_ratio": 0.9,
                   "force_bin_edges_N": [0.3, 0.78, 1.26, 1.74, 2.22, 2.7], "required_force_bins": 5,
                   "overload_N": 3.3, "maximum_force_gap_ms": 250,
                   "maximum_spectrum_gap_ms": 1000,
                   "quality_scope": "processed_loading_hold_source_frames", "physical_accuracy_qualified": False}
_GUIDED_MESSAGES = {"select_point": "请选中点位并明确确认位置", "needs_baseline": "请移开接触棒，确认无载并重新标定",
                    "calibrating": "正在采集新的九峰基线与六轴无载零点", "ready": "无载标定已就绪，可以开始本轮采集",
                    "preload": "正在记录无载预采样，请保持接触棒移开", "loading": "请缓慢加载至 3 N，覆盖加载范围",
                    "unloading": "加载已结束，请完全卸载并保持无载", "checking": "卸载已完成，正在检查本轮采集数据", "retry": "本轮未通过质量检查，请无载重标后重新采集",
                    "round_complete": "本轮合格；下一轮前请重新确认无载并标定", "point_complete": "当前点重复采集已完成，请确认下一点",
                    "session_complete": "25 点重复采集已完成"}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _trusted_time(frame):
    value = frame.get("time_quality")
    return isinstance(value, str) and value in {"host_rx_approx", "hardware_mapped"}


def coordinate_definition():
    """Versioned operator coordinate convention for newly created sessions.

    The confirmed pad normal supplies one direction only; it is not a complete
    PX6D-to-finger rotation or a replacement for the measured force_sign.
    """
    return {"schema": "bottom_left_xy_v1", "origin": "bottom_left",
            "row_index_base": 0, "col_index_base": 0,
            "x_positive": "right", "y_positive": "fingertip_up",
            "finger_pad_up_in_px6d": [0, 0, -1], "px6d_rotation_verified": False}


def default_point_grid():
    return [{"id": f"P{index + 1:02d}", "row": index // 5, "col": index % 5,
             "x_norm": (index % 5) / 4, "y_norm": (index // 5) / 4,
             "u": (index % 5) / 4, "v": (index // 5) / 4} for index in range(25)]


def _peaks(tracker):
    rows = tracker.get("peaks", [])
    if not isinstance(rows, list) or len(rows) != 9:
        return None
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    if set(by_id) != set(IDS) or any(row.get("valid") is not True or row.get("stale") is True or not _finite(row.get("center_nm")) for row in by_id.values()):
        return None
    return [by_id[key]["center_nm"] for key in IDS]


def _force_values(frame):
    values = frame.get("force_values")
    axes = frame.get("axes", AXES)
    if not isinstance(values, (list, tuple)) or len(values) != 6 or not all(_finite(x) for x in values):
        return None
    if not isinstance(axes, (list, tuple)) or len(axes) != 6 or any(not isinstance(axis, str) for axis in axes) or set(axes) != set(AXES):
        return None
    units = frame.get("units", [UNITS[AXES.index(axis)] for axis in axes])
    if not isinstance(units, (list, tuple)) or len(units) != 6 or any(unit != UNITS[AXES.index(axis)] for axis, unit in zip(axes, units)):
        return None
    return [values[axes.index(axis)] for axis in AXES]


def _stamp(frame):
    value = frame.get("host_rx_monotonic_ns")
    return value if type(value) is int and value > 0 else None


def _identity(frame):
    value = frame.get("frame_id")
    epoch = frame.get("stream_epoch")
    if not isinstance(epoch, str) or not epoch or type(value) not in (str, int) or value == "":
        return None
    return epoch, value


def _context(context, *, stable=False):
    tracker = context.get("tracker", context)
    frame, force = context.get("frame") or {}, context.get("force") or {}
    optical = next((item for item in context.get("devices", []) if item.get("wavelength_axis") or item.get("sdk_status_raw")), {})
    optical_connected = context.get("optical_connected", optical.get("connected", False))
    optical_epoch = context.get("optical_epoch", context.get("optical_stream_epoch", frame.get("stream_epoch")))
    force_epoch = context.get("force_epoch", context.get("force_stream_epoch", force.get("stream_epoch")))
    if context.get("mode") != "hardware" or context.get("running") is not True or optical_connected is not True or context.get("force_connected") is not True:
        raise ValueError("实验需要实际设备的光谱与 PX6D 两路实时连接")
    if not optical_epoch or not force_epoch or tracker.get("display_stale") is True or force.get("stale") is True or force.get("valid") is False:
        raise ValueError("实验需要两路新鲜有效的采集时段，不能使用旧值")
    if _finite(tracker.get("display_age_ms")) and tracker["display_age_ms"] > 1000 or _finite(force.get("age_ms")) and force["age_ms"] > 250:
        raise ValueError("实验采集帧已过期")
    if stable and (_peaks(tracker) is None or tracker.get("reference_pending") is True):
        raise ValueError("请先取得稳定有效的 9 个固定编号光栅")
    algorithm = context.get("algorithm_version", tracker.get("algorithm_version"))
    config = context.get("config_version", tracker.get("config_version"))
    if not algorithm or not config:
        raise ValueError("实验必须保留提峰算法与配置版本")
    return {"optical_epoch": optical_epoch, "force_epoch": force_epoch, "algorithm_version": algorithm,
            "config_version": config, "reference_version": tracker.get("reference_version", 0)}


def _settings(settings):
    value = deepcopy(settings)
    # Optional for older API clients; the current operator form requires it.
    # Keep this independent of the specimen ID and historical contact diameter.
    value.setdefault("data_id", "")
    if not isinstance(value["data_id"], str) or len(value["data_id"]) > 128:
        raise ValueError("数据编号必须为不超过 128 个字符的文本")
    if not isinstance(value.get("finger_id"), str) or not value["finger_id"].strip():
        raise ValueError("请填写手指或触觉件标识")
    if value.get("batch") not in {"A", "B", "C"}:
        raise ValueError("实验批次必须为 A、B 或 C")
    if type(value.get("force_sign", 1)) not in (int, float) or value.get("force_sign", 1) not in (-1, 1):
        raise ValueError("法向力方向必须为 +1 或 -1")
    value.setdefault("force_sign", 1)
    value["force_direction_confirmed"] = value.get("force_direction_confirmed") is True
    value["coordinates_confirmed"] = value.get("coordinates_confirmed", value.get("point_grid_confirmed")) is True
    value.setdefault("max_force_N", 3)
    if not _finite(value["max_force_N"]) or value["max_force_N"] <= 0:
        raise ValueError("法向力记录范围 max_force_N 必须为有限正数")
    value["max_force_N_confirmed"] = value.get("max_force_N_confirmed") is True
    if type(value.get("guided_acquisition", False)) is not bool:
        raise ValueError("引导式采集开关必须为布尔值")
    value["guided_acquisition"] = value.get("guided_acquisition", False)
    repetitions = value.get("repetitions_per_point", 3)
    if type(repetitions) is not int or not 3 <= repetitions <= 999:
        raise ValueError("每点合格重复次数必须为 3～999 的整数")
    value["repetitions_per_point"] = repetitions
    if value["guided_acquisition"]:
        if value["max_force_N"] != 3:
            raise ValueError("引导式采集当前使用固定 3 N 目标")
        value["guided_criteria"] = deepcopy(GUIDED_CRITERIA)
    value.update(coordinate_units="normalized", force_units="N", torque_units="N·m")
    definition = coordinate_definition()
    if "coordinate_definition" in value and value["coordinate_definition"] != definition:
        raise ValueError("新会话坐标定义必须为 bottom_left_xy_v1；不能将历史点阵静默解释为新定义")
    value["coordinate_definition"] = definition
    grid = value.get("point_grid")
    if grid is None:
        grid = default_point_grid()
    if not isinstance(grid, list) or len(grid) != 25:
        raise ValueError("实验点位必须完整包含 25 个点")
    normalized = []
    for item in grid:
        if not isinstance(item, dict):
            raise ValueError("实验点位格式无效")
        point = deepcopy(item)
        point["id"] = point.get("id", point.get("point_id"))
        if point["id"] not in {f"P{i:02d}" for i in range(1, 26)}:
            raise ValueError("25 点编号必须固定为 P01～P25")
        index = int(point["id"][1:]) - 1
        row, col = index // 5, index % 5
        if type(point.get("row")) is not int or type(point.get("col")) is not int or (point["row"], point["col"]) != (row, col):
            raise ValueError("新会话 25 点行列必须为 0～4，并与左下角 P01 起的固定编号对应；历史 1～5 点阵不能自动转换")
        u, v = point.get("u", point.get("x_norm")), point.get("v", point.get("y_norm"))
        if not isinstance(point["id"], str) or not point["id"] or not all(_finite(x) and 0 <= x <= 1 for x in (u, v)):
            raise ValueError("25 点需要独立编号及 0～1 的归一化坐标")
        expected_u, expected_v = col / 4, row / 4
        if any(key in point and (not _finite(point[key]) or point[key] != expected) for key, expected in (
                ("u", expected_u), ("x_norm", expected_u), ("v", expected_v), ("y_norm", expected_v))):
            raise ValueError("归一化坐标必须与固定编号、0 基行列一致：u=x_norm=col/4，v=y_norm=row/4")
        point.update(x_norm=expected_u, y_norm=expected_v, u=expected_u, v=expected_v)
        normalized.append(point)
    if len({point["id"] for point in normalized}) != 25 or len({(point["u"], point["v"]) for point in normalized}) != 25:
        raise ValueError("25 点的编号与坐标不能重复")
    value["point_grid"] = normalized
    value.setdefault("conditions", {})
    if not isinstance(value["conditions"], dict):
        raise ValueError("实验条件需要为对象")
    json.dumps(value, ensure_ascii=False, allow_nan=False)
    return value


class ExperimentManager:
    def __init__(self, userdata):
        self.userdata = Path(userdata)
        self.lock = threading.RLock()
        self.writer = None
        self.running = False
        self.status = "idle"
        self.error = None
        self.settings = {}
        self.context = {}
        self.session_id = self.point_id = self.trial_id = None
        self.phase = "idle"
        self.baseline = {"status": "required", "ready": False}
        self.counts = {}
        self._timeline = []
        self._baselines = {}
        self._trials = {}
        self._force_records = OrderedDict()
        self._zero_samples = []
        self._zero_seen = set()
        self._baseline_number = self._trial_number = 0
        self._guided = {"enabled": False}
        self._guided_rounds = {}

    def start(self, settings, context):
        with self.lock:
            if self.running or self.writer and not self.writer.closed:
                raise ValueError("请先结束当前实验会话")
            checked, active = _settings(settings), _context(context, stable=True)
            self.settings, self.context = checked, active
            audit = {key: deepcopy(context.get(key)) for key in ("devices", "tracker", "tracker_config")}
            audit["optical_preprocessing"] = deepcopy((context.get("frame") or {}).get("preprocessing"))
            self.writer = SessionWriter(self.userdata / "calibration_sessions", {"kind": "9fbg_calibration_experiment_v1", "settings": checked, "coordinate_definition": deepcopy(checked["coordinate_definition"]), "acquisition_context": active, "acquisition_audit": clean(audit), "formal_training_qualified": False})
            self.session_id = self.writer.metadata["session_id"]
            self.running, self.status, self.error = True, "recording", None
            enabled = checked["guided_acquisition"]
            self.point_id, self.trial_id, self.phase = None if enabled else checked["point_grid"][0]["id"], None, "idle"
            self.baseline = {"status": "required", "ready": False}
            self.counts = {key: 0 for key in ("raw_spectra", "raw_force", "results", "events", "sync_valid", "sync_invalid", "trials_completed", "trials_aborted")}
            self._timeline, self._baselines, self._trials = [], {}, {}
            self._force_records.clear()
            self._zero_samples, self._zero_seen = [], set()
            self._baseline_number = self._trial_number = 0
            self._guided = {"enabled": enabled, "step": "select_point", "last_quality": None, "force_N": 0, "preload": None,
                            "accepted_by_point": {point["id"]: 0 for point in checked["point_grid"]},
                            "attempts_by_point": {point["id"]: 0 for point in checked["point_grid"]}}
            self._guided_rounds = {}
            self._transition("session_start")
            return self.snapshot()

    def _require_running(self):
        if not self.running or self.error:
            raise ValueError(self.error or "请先开始实验会话")

    def _submit(self, kind, row):
        try:
            self.writer.submit(kind, row)
        except Exception as exc:
            self._fail(exc)
            raise
        self.counts[kind] += 1

    def _annotation(self, stamp):
        state = next((state for at, state in reversed(self._timeline) if stamp is not None and at <= stamp), None)
        if state is None:
            return {"session_id": self.session_id, "trial_id": None, "point_id": None, "baseline_id": None, "phase": "pre_session", "eligible": False, "manual_mark": []}
        return deepcopy(state)

    def _transition(self, event, *, at_ns=None, annotate=True, **extra):
        stamp = max(time.perf_counter_ns(), self._timeline[-1][0] if self._timeline else 0) if at_ns is None else at_ns
        marks = self._trials.get(self.trial_id, {}).get("manual_mark", [])
        state = {"session_id": self.session_id, "trial_id": self.trial_id, "point_id": self.point_id,
                 "baseline_id": self.baseline.get("baseline_id"), "phase": self.phase,
                 "eligible": True, "manual_mark": deepcopy(marks)}
        if annotate:
            # Delayed fits may refer to earlier samples, but must never append
            # an old phase boundary after a newer boundary in this timeline.
            if self._timeline and stamp < self._timeline[-1][0]:
                raise ValueError("实验阶段事件时间回退，拒绝污染采集时间线")
            self._timeline.append((stamp, state))
        self._submit("events", {"event": event, "host_rx_monotonic_ns": stamp, **state, **deepcopy(extra)})
        return stamp

    def _fail(self, exc):
        self.error = "实验记录失败：" + str(exc)
        self.running, self.status = False, "error"
        try:
            self.writer.close(self._final("recording_error"))
        except Exception:
            pass

    def _final(self, reason):
        return {**self.snapshot(), "reason": reason, "error": self.error,
                "baseline_history": deepcopy(list(self._baselines.values())), "trials": deepcopy(list(self._trials.values())),
                "formal_training_qualified": False}

    def stop(self, reason="operator_stop"):
        with self.lock:
            if self.writer and not self.writer.closed:
                if self.running:
                    if self.trial_id:
                        if self._guided.get("enabled"):
                            state = self._guided_rounds.get(self.trial_id)
                            if state:
                                state["faults"].add("采集停止或断流：" + str(reason))
                                self._guided_finish(state, aborted=True, event_reason=reason)
                        else:
                            self._trials[self.trial_id].update(status="aborted", stop_reason=reason)
                            self.counts["trials_aborted"] += 1
                    try:
                        self._transition("session_stop", reason=reason)
                    except Exception as exc:
                        self.error = str(exc)
                self.running = False
                self.status = "error" if self.error else "stopped"
                self.writer.close(self._final(reason))
                if self.writer.error:
                    self.error, self.status = self.writer.error, "error"
            return self.snapshot()

    def select_point(self, point_id):
        with self.lock:
            self._require_running()
            if self.trial_id:
                raise ValueError("活动试次内不能切换实验点位")
            if point_id not in {point["id"] for point in self.settings["point_grid"]}:
                raise ValueError("实验点位不在本会话的 25 点网格内")
            self.point_id = point_id
            self.baseline = {"status": "required", "ready": False}
            self.phase = "idle"
            if self._guided.get("enabled"):
                self._guided.update(step=self._guided_idle_step(point_id) if self._guided["accepted_by_point"][point_id] >= self.settings["repetitions_per_point"] else "needs_baseline",
                                    last_quality=None, force_N=0, preload=None)
            self._transition("point_selected")
            return self.snapshot()

    def baseline_request(self, unloaded_confirmed, context):
        with self.lock:
            self._require_running()
            if unloaded_confirmed is not True:
                raise ValueError("请明确确认接触棒已移开且当前无载")
            if self.trial_id:
                raise ValueError("活动试次内基线与六轴零点冻结，不能重新标定")
            if self._guided.get("enabled") and (self.point_id is None or self._guided["accepted_by_point"][self.point_id] >= self.settings["repetitions_per_point"]):
                raise ValueError("请先明确确认尚未完成重复次数的点位")
            current = _context(context, stable=True)
            self._same_context(current)
            self._baseline_number += 1
            self.baseline = {"baseline_id": f"B{self._baseline_number:04d}", "point_id": self.point_id,
                             "status": "collecting", "ready": False, "used": False, "unloaded_confirmed": True,
                             "previous_reference_version": current["reference_version"], "lambda0_nm": None,
                             "force_zero": None, "force_noise_N": None, "reference_version": None,
                             "optical_epoch": current["optical_epoch"], "force_epoch": current["force_epoch"],
                             "algorithm_version": current["algorithm_version"], "config_version": current["config_version"]}
            self._baselines[self.baseline["baseline_id"]] = self.baseline
            self._zero_samples, self._zero_seen = [], set()
            self.phase = "unloaded"
            if self._guided.get("enabled"):
                self._guided.update(step="calibrating", preload=None)
            self.baseline["requested_monotonic_ns"] = self._transition("unloaded_confirmation", unloaded_confirmed=True)
            return self.snapshot()

    def _same_context(self, current):
        if any(current[key] != self.context[key] for key in ("optical_epoch", "force_epoch", "algorithm_version", "config_version")):
            raise ValueError("设备采集时段、提峰算法或配置已改变，请结束后创建新实验会话")

    def _baseline_ready(self):
        if self.baseline.get("status") == "collecting" and self.baseline.get("lambda0_nm") is not None and self.baseline.get("force_zero") is not None:
            self.baseline.update(status="ready", ready=True, effective_monotonic_ns=time.perf_counter_ns())
            if self._guided.get("enabled"):
                self._guided["step"] = "ready"
            self._transition("baseline_ready", baseline=deepcopy(self.baseline))

    def trial(self, action, context):
        with self.lock:
            self._require_running()
            if self._guided.get("enabled") and action not in {"start", "abort"}:
                raise ValueError("引导式试次阶段由真实采集帧与质量门自动推进，不能手动跳过")
            if action == "start":
                if self.trial_id:
                    raise ValueError("当前试次尚未结束")
                if self._guided.get("enabled") and (self.point_id is None or self._guided["accepted_by_point"][self.point_id] >= self.settings["repetitions_per_point"]):
                    raise ValueError("请确认尚未完成重复次数的点位")
                current = _context(context, stable=True)
                self._same_context(current)
                if not self.settings["force_direction_confirmed"] or not self.settings["coordinates_confirmed"]:
                    raise ValueError("请先明确确认法向力方向与 25 点坐标")
                if not self.baseline.get("ready") or self.baseline.get("used") or self.baseline.get("point_id") != self.point_id:
                    raise ValueError("每次试次前都需要新的明确无载基线与六轴零点")
                if current["reference_version"] != self.baseline["reference_version"]:
                    raise ValueError("当前九峰参考版本与本次无载基线不一致")
                self._trial_number += 1
                self.trial_id = f"T{self._trial_number:04d}"
                self.baseline["used"] = True
                self.phase = "preload"
                self._trials[self.trial_id] = {"trial_id": self.trial_id, "point_id": self.point_id, "baseline_id": self.baseline["baseline_id"], "status": "active", "manual_mark": []}
                started = self._transition("trial_start", baseline=deepcopy(self.baseline))
                if self._guided.get("enabled"):
                    self._guided_start(started)
            elif action in {"finish", "abort"}:
                if not self.trial_id or action == "finish" and self.phase != "recovery":
                    raise ValueError("只有恢复阶段才能完成试次；异常可使用终止试次")
                if self._guided.get("enabled"):
                    self._guided_rounds[self.trial_id]["faults"].add("操作员终止本轮")
                    self._guided_finish(self._guided_rounds[self.trial_id], aborted=True)
                    return self.snapshot()
                old_id = self.trial_id
                self._trials[old_id]["status"] = "completed" if action == "finish" else "aborted"
                self.counts["trials_completed" if action == "finish" else "trials_aborted"] += 1
                self._transition("trial_" + action, completed_trial_id=old_id)
                self.trial_id, self.phase = None, "idle"
                self.baseline = {"status": "required", "ready": False}
                self._transition("next_baseline_required")
            else:
                expected = {"loading": "preload", "hold": "loading", "removed": "hold", "recovery": "removed"}
                if action not in expected or not self.trial_id or self.phase != expected[action]:
                    raise ValueError("试次阶段顺序必须为预载、加载、保持、移开、恢复、完成")
                self.phase = action
                self._transition("trial_" + action)
            return self.snapshot()

    def _guided_start(self, stamp):
        self._guided["attempts_by_point"][self.point_id] += 1
        diagnostics = {"schema": "preload_diagnostics_v1", "elapsed_ms": 0, "valid_continuous_ms": 0,
                       "reset_count": 0, "last_block_reason": "等待新的九峰与 PX6D 有效配对",
                       "last_reset_reason": None,
                       "timeout_ms": GUIDED_CRITERIA["preload_timeout_ms"],
                       "target_ms": GUIDED_CRITERIA["preload_ms"], "timed_out": False}
        self._guided.update(step="preload", force_N=0, last_quality=None, preload=diagnostics)
        state = {"trial_id": self.trial_id, "point_id": self.point_id, "baseline_id": self.baseline["baseline_id"],
                 "started_ns": stamp, "force_seen": set(), "result_seen": set(), "faults": set(),
                 "last_raw_ns": stamp,
                 "last_force_ns": None, "last_result_ns": None, "preload_first_ns": None, "preload_last_ns": None,
                 "preload_ms": 0, "preload_diagnostics": diagnostics,
                 "target_first_ns": None, "target_hold_ms": 0, "target_reached": False,
                 "unload_first_ns": None, "unload_hold_ms": 0, "recovered": False, "release_ns": None,
                 "processed_spectra": 0, "valid_spectra": 0, "peak_valid_spectra": 0, "sync_valid_spectra": 0,
                 "raw_spectra": 0, "force_frames": 0, "invalid_force_frames": 0, "duplicate_force_frames": 0,
                 "duplicate_results": 0, "max_force_N": 0, "force_bins": {}, "finalized": False, "accepted": False}
        self._guided_rounds[self.trial_id] = state
        self._trials[self.trial_id].update(guided=True, round_number=self._guided["accepted_by_point"][self.point_id] + 1,
                                          attempt_number=self._guided["attempts_by_point"][self.point_id],
                                          criteria_version="guided_round_v1")

    def _preload_block(self, state, reason):
        """Reset a source-driven run once; repeated watchdog reads add no resets."""
        if state["preload_first_ns"] is not None:
            state["preload_diagnostics"]["reset_count"] += 1
        state["preload_first_ns"] = state["preload_last_ns"] = None
        state["preload_ms"] = 0
        state["preload_diagnostics"].update(valid_continuous_ms=0, last_block_reason=reason, last_reset_reason=reason)

    def _preload_watchdog(self, now_ns, *, inspect_sources=False):
        if not self.running or not self._guided.get("enabled") or not self.trial_id or self.phase != "preload":
            return False
        state = self._guided_rounds[self.trial_id]
        diagnostics = state["preload_diagnostics"]
        elapsed = max(0, (now_ns - state["started_ns"]) / 1e6)
        diagnostics["elapsed_ms"] = max(diagnostics["elapsed_ms"], elapsed)
        if inspect_sources:
            force_ns, result_ns = state["last_force_ns"], state["last_result_ns"]
            if now_ns - (force_ns or state["started_ns"]) > 250_000_000:
                self._preload_block(state, "PX6D 源帧已过期：超过 250 ms 未更新" if force_ns else "等待新的 PX6D 源帧")
            elif now_ns - state["last_raw_ns"] > 1_000_000_000:
                self._preload_block(state, "原始光谱源帧已过期：超过 1000 ms 未更新")
            elif now_ns - (result_ns or state["started_ns"]) > 1_000_000_000:
                self._preload_block(state, "等待新的九峰提取与同步结果")
        if diagnostics["elapsed_ms"] < diagnostics["timeout_ms"]:
            return False
        diagnostics["timed_out"] = True
        if diagnostics["last_block_reason"] is None:
            recent = diagnostics["last_reset_reason"]
            diagnostics["last_block_reason"] = ("连续有效配对尚不足 500 ms；最近重置原因：" + recent
                                                 if recent else "连续有效配对尚不足 500 ms；等待连续有效同步配对")
        reason = "无载预采样超过 8 秒仍未取得连续 500 ms 有效配对；请重新确认无载并标定"
        state["faults"].add(reason)
        # This wall-clock safety boundary cannot advance loading or synthesize
        # samples. Source frames before it retain their original trial labels.
        boundary = max(now_ns, self._timeline[-1][0] if self._timeline else 0)
        self._transition("guided_preload_timeout", at_ns=boundary, reason=reason,
                         preload=deepcopy(diagnostics))
        self._guided_finish(state, aborted=True, event_reason="preload_timeout", at_ns=boundary)
        return True

    def watchdog(self, now_ns=None):
        """Expire stalled guided preload; call periodically even without frames.

        ``now_ns`` uses the same monotonic perf-counter clock as acquisition.
        Snapshots never call this method or advance source-derived progress.
        Return True only when this call aborted the stalled trial.
        """
        with self.lock:
            try:
                return self._preload_watchdog(time.perf_counter_ns() if now_ns is None else now_ns,
                                              inspect_sources=True)
            except Exception as exc:
                self._fail(exc)
                return False

    def _guided_force(self, frame, annotation, values):
        if not self._guided.get("enabled") or annotation.get("trial_id") != self.trial_id or not self.trial_id:
            return
        if self._preload_watchdog(time.perf_counter_ns()):
            return
        state = self._guided_rounds[self.trial_id]
        stamp, identity = _stamp(frame), _identity(frame)
        if identity in state["force_seen"]:
            state["duplicate_force_frames"] += 1
            return
        if identity:
            state["force_seen"].add(identity)
        usable = (stamp is not None and identity is not None and values is not None and frame.get("source_kind") == "hardware"
                  and _trusted_time(frame) and isinstance(frame.get("axes"), (list, tuple)) and isinstance(frame.get("units"), (list, tuple))
                  and frame.get("valid") is not False and frame.get("stale") is not True
                  and not (_finite(frame.get("age_ms")) and frame["age_ms"] > 250))
        if not usable or stamp < state["started_ns"]:
            state["invalid_force_frames"] += 1
            state["faults"].add("出现无效、过期或轴单位不一致的六轴源帧")
            state["target_first_ns"] = state["unload_first_ns"] = None
            if self.phase == "preload":
                self._preload_block(state, "PX6D 源帧无效、过期或轴单位不一致")
            return
        if state["last_force_ns"] is not None:
            gap = stamp - state["last_force_ns"]
            if gap <= 0:
                state["invalid_force_frames"] += 1
                state["faults"].add("六轴源帧时间乱序或回退")
                if self.phase == "preload":
                    self._preload_block(state, "PX6D 源帧时间乱序或回退")
                return
            if gap > 250_000_000:
                state["faults"].add("六轴采集间断超过 250 ms")
                state["target_first_ns"] = state["unload_first_ns"] = None
                if self.phase == "preload":
                    self._preload_block(state, "PX6D 相邻源帧间断超过 250 ms")
        state["last_force_ns"] = stamp
        if stamp - state["last_raw_ns"] > 1_000_000_000:
            state["faults"].add("原始光谱采集间断超过 1000 ms")
        state["force_frames"] += 1
        normal = self.settings["force_sign"] * (values[2] - self.baseline["force_zero"][2])
        self._guided["force_N"] = normal
        state["max_force_N"] = max(state["max_force_N"], normal)
        if normal > 3.3:
            state["faults"].add("法向力超过 3.3 N")
            if self.phase in {"preload", "loading"}:
                self.phase = "hold"
                self._guided["step"] = "unloading"
                self._transition("guided_overload_unload", at_ns=self._guided_boundary(stamp), trigger_source_stamp_ns=stamp)
        if self.phase == "loading":
            if 2.9 <= normal <= 3.1:
                if state["target_first_ns"] is None:
                    state["target_first_ns"] = stamp
                duration = (stamp - state["target_first_ns"]) / 1e6
                state["target_hold_ms"] = max(state["target_hold_ms"], duration)
                if duration >= 200:
                    state["target_reached"] = True
                    self.phase = "hold"
                    self._guided["step"] = "unloading"
                    self._transition("guided_target_reached", at_ns=self._guided_boundary(stamp), trigger_source_stamp_ns=stamp)
            else:
                state["target_first_ns"] = None
        if self.phase == "hold":
            if abs(normal) <= .1:
                if state["unload_first_ns"] is None:
                    state["unload_first_ns"] = stamp
                duration = (stamp - state["unload_first_ns"]) / 1e6
                state["unload_hold_ms"] = max(state["unload_hold_ms"], duration)
                if duration >= 400:
                    boundary = self._guided_boundary(stamp)
                    state.update(recovered=True, release_ns=boundary + 1)
                    self.phase = "removed"
                    self._transition("guided_removed", at_ns=boundary, trigger_source_stamp_ns=stamp)
                    self.phase = "recovery"
                    self._guided["step"] = "checking"
                    self._transition("guided_recovery", at_ns=boundary + 1, trigger_source_stamp_ns=stamp)
            else:
                state["unload_first_ns"] = None

    def _guided_boundary(self, source_stamp):
        # Preserve a later operator event (for example an anomaly mark) while
        # retaining the actual triggering acquisition time in the event data.
        return max(source_stamp + 1, self._timeline[-1][0] + 1 if self._timeline else 0)

    def _guided_quality(self, state):
        trial = self._trials[state["trial_id"]]
        ratio = state["valid_spectra"] / state["processed_spectra"] if state["processed_spectra"] else 0
        metrics = {key: deepcopy(state[key]) for key in (
            "preload_ms", "target_reached", "target_hold_ms", "unload_hold_ms", "recovered", "max_force_N",
            "processed_spectra", "valid_spectra", "peak_valid_spectra", "sync_valid_spectra", "raw_spectra",
            "force_frames", "invalid_force_frames", "duplicate_force_frames", "duplicate_results")}
        metrics.update(valid_ratio=ratio, force_bins_covered=len(state["force_bins"]),
                       preload_diagnostics=deepcopy(state["preload_diagnostics"]),
                       force_bin_counts={str(index): state["force_bins"].get(index, 0) for index in range(5)},
                       force_bin_edges_N=deepcopy(GUIDED_CRITERIA["force_bin_edges_N"]),
                       display_skipped_estimate=max(0, state["raw_spectra"] - state["processed_spectra"]),
                       result_watermark_ns=state["last_result_ns"], baseline_id=state["baseline_id"],
                       quality_scope=GUIDED_CRITERIA["quality_scope"], physical_accuracy_qualified=False)
        reasons = sorted(state["faults"])
        if state["preload_ms"] < 500:
            reasons.append("有效无载预采样不足 500 ms")
        if not state["target_reached"]:
            reasons.append("未在 2.9～3.1 N 持续至少 200 ms")
        if not state["recovered"] or state["unload_hold_ms"] < 400:
            reasons.append("未卸载至 ±0.1 N 并保持至少 400 ms")
        if state["valid_spectra"] < 30:
            reasons.append("加载/保持阶段有效九峰与同步光谱少于 30 帧")
        if ratio < .9:
            reasons.append("加载/保持阶段已处理光谱有效比例低于 90%")
        if len(state["force_bins"]) < 5:
            reasons.append("有效同步光谱未覆盖 0.3～2.7 N 的全部 5 个力箱")
        if trial.get("manual_mark"):
            reasons.append("本轮有人工异常标记")
        return {"accepted": not reasons, "reasons": reasons, "metrics": metrics, "criteria_version": "guided_round_v1"}

    def _guided_finish(self, state, *, aborted=False, event_reason=None, at_ns=None):
        quality = self._guided_quality(state)
        if aborted:
            quality["accepted"] = False
        trial = self._trials[state["trial_id"]]
        trial.update(status="aborted" if aborted else "completed", bad=not quality["accepted"], quality=deepcopy(quality))
        if event_reason:
            trial["stop_reason"] = event_reason
        self.counts["trials_aborted" if aborted else "trials_completed"] += 1
        state.update(finalized=True, accepted=quality["accepted"])
        if quality["accepted"]:
            self._guided["accepted_by_point"][state["point_id"]] += 1
        self._guided["last_quality"] = deepcopy(quality)
        self._transition("guided_round_quality", at_ns=at_ns, quality=deepcopy(quality), completed_trial_id=state["trial_id"])
        self.trial_id, self.phase = None, "idle"
        self.baseline = {"status": "required", "ready": False}
        self._guided["step"] = self._guided_idle_step(state["point_id"], quality["accepted"])
        self._transition("next_baseline_required", at_ns=at_ns)

    def _guided_idle_step(self, point_id, accepted=True):
        if all(value >= self.settings["repetitions_per_point"] for value in self._guided["accepted_by_point"].values()):
            return "session_complete"
        if self._guided["accepted_by_point"][point_id] >= self.settings["repetitions_per_point"]:
            return "point_complete"
        return "round_complete" if accepted else "retry"

    def _guided_result(self, row, peaks_valid, matching):
        if not self._guided.get("enabled"):
            return
        state = self._guided_rounds.get(row.get("trial_id"))
        if state is None:
            return
        self._preload_watchdog(time.perf_counter_ns())
        stamp, identity = row.get("host_rx_monotonic_ns"), (row["result"].get("stream_epoch"), row.get("frame_id"))
        if identity in state["result_seen"]:
            state["duplicate_results"] += 1
            return
        state["result_seen"].add(identity)
        state["last_result_ns"] = max(stamp or 0, state["last_result_ns"] or 0)
        if not matching or row["result"].get("reference_pending") is True:
            state["faults"].add("九峰参考、配置或采集时段与本轮锁定基线不一致")
        if row["phase"] == "preload" and not state["finalized"] and self.trial_id == state["trial_id"] and self.phase == "preload":
            if row["valid"] and row["force_N"] is not None and abs(row["force_N"]) <= .1:
                if state["preload_last_ns"] is None or stamp-state["preload_last_ns"] > 250_000_000:
                    if state["preload_last_ns"] is not None:
                        self._preload_block(state, "有效配对源帧间隔超过 250 ms")
                    state["preload_first_ns"] = stamp
                state["preload_last_ns"] = stamp
                state["preload_ms"] = (stamp-state["preload_first_ns"]) / 1e6
                state["preload_diagnostics"].update(valid_continuous_ms=state["preload_ms"], last_block_reason=None)
                if state["preload_ms"] >= 500:
                    self.phase = "loading"
                    self._guided["step"] = "loading"
                    self._transition("guided_loading_ready", at_ns=self._guided_boundary(stamp), trigger_source_stamp_ns=stamp)
            else:
                if not matching or row["result"].get("reference_pending") is True:
                    reason = "九峰参考尚未锁定，或参考/配置与本轮基线不一致"
                elif not peaks_valid or row["result"].get("valid") is False or row["result"].get("stale") is True:
                    reason = "九峰无效或已过期"
                elif not row["sync"].get("valid"):
                    reason = "同步配对无效：" + str(row["sync"].get("reason") or "等待可信 PX6D 配对")
                elif row["force_N"] is not None and abs(row["force_N"]) > .1:
                    reason = f"无载法向力超出 ±0.1 N（当前 {row['force_N']:.3f} N）"
                else:
                    reason = "采集来源或无载基线未满足有效条件"
                self._preload_block(state, reason)
        if row["phase"] in {"loading", "hold"}:
            state["processed_spectra"] += 1
            state["peak_valid_spectra"] += bool(peaks_valid)
            state["sync_valid_spectra"] += bool(row["sync"].get("valid"))
            if row["valid"]:
                state["valid_spectra"] += 1
                normal = row["force_N"]
                edges = GUIDED_CRITERIA["force_bin_edges_N"]
                if normal is not None:
                    for index, (left, right) in enumerate(zip(edges, edges[1:])):
                        if left <= normal < right or index == 4 and normal == right:
                            state["force_bins"][index] = state["force_bins"].get(index, 0) + 1
                            break
            if state["finalized"]:
                # A late fitted frame still changes its original entire trial.
                # Revoke a previously counted round if its final quality fails.
                quality = self._guided_quality(state)
                if state["accepted"] and not quality["accepted"]:
                    self._guided["accepted_by_point"][state["point_id"]] -= 1
                    state["accepted"] = False
                    self._trials[state["trial_id"]]["bad"] = True
                    if self.trial_id is None and self.point_id == state["point_id"]:
                        self._guided["step"] = "retry"
                if not state["accepted"]:
                    quality["accepted"] = False
                self._trials[state["trial_id"]]["quality"] = deepcopy(quality)
                self._guided["last_quality"] = deepcopy(quality)
                self._transition("guided_late_quality_update", annotate=False, at_ns=stamp,
                                 revised_trial_id=state["trial_id"], quality=deepcopy(quality))
        if not state["finalized"] and state["recovered"] and stamp is not None and stamp >= state["release_ns"]:
            self._guided_finish(state, at_ns=self._guided_boundary(stamp))

    def _guided_snapshot(self):
        if not self._guided.get("enabled"):
            return {"enabled": False}
        guided = self._guided
        repetitions = self.settings["repetitions_per_point"]
        accepted = guided["accepted_by_point"].get(self.point_id, 0)
        attempts = guided["attempts_by_point"].get(self.point_id, 0)
        total, required = sum(guided["accepted_by_point"].values()), 25 * repetitions
        step = guided["step"]
        return {"enabled": True, "step": step, "message": _GUIDED_MESSAGES[step], "repetitions_per_point": repetitions,
                "accepted_by_point": deepcopy(guided["accepted_by_point"]), "attempts_by_point": deepcopy(guided["attempts_by_point"]),
                "current_round": min(accepted + 1, repetitions), "point_accepted": accepted, "point_attempts": attempts,
                "total_accepted": total, "total_required": required, "progress_percent": total / required * 100,
                "point_progress_percent": accepted / repetitions * 100, "force_progress_percent": min(100, max(0, guided["force_N"] / 3 * 100)),
                "last_quality": deepcopy(guided["last_quality"]), "preload": deepcopy(guided.get("preload")),
                "complete": total >= required}

    def mark(self, reason):
        with self.lock:
            self._require_running()
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("异常标记需要说明原因")
            if self.trial_id:
                self._trials[self.trial_id]["manual_mark"].append(reason)
                self._trials[self.trial_id]["bad"] = True
                if self._guided.get("enabled"):
                    self._guided_rounds[self.trial_id]["faults"].add("人工异常：" + reason)
                    if self.phase in {"preload", "loading"}:
                        self.phase = "hold"
                    self._guided["step"] = "unloading"
            else:
                self.baseline.update(ready=False, status="invalid", bad=True)
                if self._guided.get("enabled"):
                    self._guided["step"] = "needs_baseline" if self.point_id is not None and self._guided["accepted_by_point"][self.point_id] < self.settings["repetitions_per_point"] else "select_point" if self.point_id is None else self._guided_idle_step(self.point_id)
            self._transition("manual_mark", reason=reason)
            return self.snapshot()

    def on_spectrum(self, frame):
        with self.lock:
            if not self.running:
                return False
            try:
                row = deepcopy(frame)
                annotation = self._annotation(_stamp(frame))
                row["source_phase"] = row.get("phase")
                row.update(annotation, experiment=annotation)
                self._submit("raw_spectra", row)
                state = self._guided_rounds.get(annotation.get("trial_id"))
                if state:
                    stamp = _stamp(frame)
                    if stamp is not None:
                        if stamp - state["last_raw_ns"] > 1_000_000_000:
                            state["faults"].add("原始光谱采集间断超过 1000 ms")
                        state["last_raw_ns"] = max(stamp, state["last_raw_ns"])
                    if annotation.get("phase") in {"loading", "hold"}:
                        state["raw_spectra"] += 1
                if annotation["eligible"] and frame.get("stream_epoch") != self.context["optical_epoch"]:
                    self.baseline.update(status="invalid", ready=False)
                    self.stop("optical_epoch_changed")
                return True
            except Exception as exc:
                self._fail(exc)
                return False

    def on_force(self, frame):
        with self.lock:
            if not self.running:
                return False
            try:
                stamp, identity = _stamp(frame), _identity(frame)
                annotation = self._annotation(stamp)
                self._submit("raw_force", {**deepcopy(frame), **annotation, "source_phase": frame.get("phase"), "experiment": annotation})
                if annotation["eligible"] and frame.get("stream_epoch") != self.context["force_epoch"]:
                    self.baseline.update(status="invalid", ready=False)
                    self.stop("force_epoch_changed")
                    return True
                if identity:
                    self._force_records.setdefault(identity, {"stamp": stamp, "annotation": annotation, "values": _force_values(frame), "source_kind": frame.get("source_kind"), "time_quality": frame.get("time_quality"), "usable": frame.get("valid") is not False and frame.get("stale") is not True and not (_finite(frame.get("age_ms")) and frame["age_ms"] > 250)})
                    if len(self._force_records) > 20000:
                        self._force_records.popitem(last=False)
                self._guided_force(frame, annotation, _force_values(frame))
                baseline = self.baseline
                if baseline.get("status") != "collecting" or baseline.get("force_zero") is not None or stamp is None or stamp < baseline["requested_monotonic_ns"]:
                    return True
                values = _force_values(frame)
                if frame.get("source_kind") != "hardware" or not _trusted_time(frame) or not identity or values is None or frame.get("valid") is False or frame.get("stale") is True or _finite(frame.get("age_ms")) and frame["age_ms"] > 250:
                    return True
                if identity[0] != baseline["force_epoch"]:
                    self.mark("PX6D 采集时段改变，无载基线失效")
                    return True
                if identity in self._zero_seen:
                    return True
                if self._zero_samples and stamp <= self._zero_samples[-1]["stamp"]:
                    self.mark("六轴无载帧乱序或时间回退，需重新确认无载")
                    return True
                self._zero_seen.add(identity)
                self._zero_samples.append({"stamp": stamp, "identity": identity, "values": values})
                while len(self._zero_samples) > 12 and self._zero_samples[1]["stamp"] <= stamp - 400_000_000:
                    self._zero_samples.pop(0)
                baseline["force_count"] = min(12, len(self._zero_samples))
                if len(self._zero_samples) >= 12 and stamp - self._zero_samples[0]["stamp"] >= 400_000_000:
                    chosen = [self._zero_samples[round(i * (len(self._zero_samples) - 1) / 11)] for i in range(12)]
                    offsets = [statistics.median(sample["values"][axis] for sample in chosen) for axis in range(6)]
                    baseline.update(force_zero=offsets, force_noise_N=1.4826 * statistics.median(abs(sample["values"][2] - offsets[2]) for sample in chosen),
                                    force_frame_ids=[sample["identity"][1] for sample in chosen], force_span_ms=(chosen[-1]["stamp"] - chosen[0]["stamp"]) / 1e6)
                    self._transition("force_zero_ready", baseline=deepcopy(baseline))
                    self._baseline_ready()
                return True
            except Exception as exc:
                self._fail(exc)
                return False

    def _checked_sync(self, result, sync, annotation):
        checked = deepcopy(sync or {})
        reason = None
        if checked.get("valid") is not True or checked.get("matched") is not True or checked.get("method") not in {"exact_receive_time", "linear_receive_time"}:
            reason = checked.get("reason") or "未取得可信左右帧或精确接收时间配对"
        elif checked.get("spectrum_frame_id") != result.get("frame_id") or checked.get("spectrum_stream_epoch") != result.get("stream_epoch") or result.get("stream_epoch") != self.context["optical_epoch"] or checked.get("force_stream_epoch") != self.context["force_epoch"]:
            reason = "同步结果帧身份或采集时段不一致"
        values = checked.get("force_values")
        if not isinstance(values, (list, tuple)) or len(values) != 6 or not all(_finite(x) for x in values):
            reason = reason or "同步六轴数值无效"
        frame_ids = checked.get("force_frame_ids") or []
        records = []
        if not frame_ids:
            reason = reason or "同步缺少六轴源帧身份"
        for frame_id in frame_ids:
            record = self._force_records.get((checked.get("force_stream_epoch"), frame_id))
            if record is None or record["annotation"].get("trial_id") != annotation.get("trial_id") or record["annotation"].get("baseline_id") != annotation.get("baseline_id") or not record["annotation"].get("eligible"):
                reason = reason or "同步引用的六轴帧未记录或跨越试次/基线"
            else:
                records.append(record)
        if result.get("source_kind") != "hardware" or not _trusted_time(result) or any(record["source_kind"] != "hardware" or not isinstance(record["time_quality"], str) or record["time_quality"] not in {"host_rx_approx", "hardware_mapped"} or record["values"] is None or not record["usable"] for record in records):
            reason = reason or "同步来源不是真实可信采集时间"
        if reason is None:
            timestamp = _stamp(result)
            if checked["method"] == "exact_receive_time" and len(records) == 1 and records[0]["stamp"] == timestamp:
                expected = records[0]["values"]
            elif checked["method"] == "linear_receive_time" and len(records) == 2 and timestamp is not None and records[0]["stamp"] < timestamp < records[1]["stamp"] and records[1]["stamp"] - records[0]["stamp"] <= 40_000_000 and max(timestamp - records[0]["stamp"], records[1]["stamp"] - timestamp) <= 25_000_000:
                fraction = (timestamp - records[0]["stamp"]) / (records[1]["stamp"] - records[0]["stamp"])
                expected = [left + (right - left) * fraction for left, right in zip(records[0]["values"], records[1]["values"])]
            else:
                reason, expected = "同步源帧不能满足 40 ms 间隔和两端 25 ms 时间容差", None
            if expected is not None and any(not math.isclose(actual, target, rel_tol=1e-9, abs_tol=1e-9) for actual, target in zip(values, expected)):
                reason = "同步值与保存的六轴源帧不一致"
        timestamp = _stamp(result)
        timestamps = sorted(record["stamp"] for record in records if record["stamp"] is not None)
        left = max((stamp for stamp in timestamps if timestamp is not None and stamp <= timestamp), default=None)
        right = min((stamp for stamp in timestamps if timestamp is not None and stamp >= timestamp), default=None)
        nearest = min((abs(stamp - timestamp) / 1e6 for stamp in timestamps), default=None) if timestamp is not None else None
        checked.update(force_source_stamps_ns=timestamps, left_force_stamp_ns=left, right_force_stamp_ns=right,
                       left_delta_ms=(timestamp-left)/1e6 if left is not None else None,
                       right_delta_ms=(right-timestamp)/1e6 if right is not None else None,
                       nearest_delta_ms=nearest, bracket_ms=(right-left)/1e6 if left is not None and right is not None else None,
                       target_ms=5.0, target_exceeded=nearest > 5 if nearest is not None else None,
                       time_quality="host_rx_approx", synchronization_semantics="主机接收时间近似配对；无共同硬件时钟；5 ms 仅为检查目标",
                       delta_ms_semantics="插值目标时刻偏差，不代表源帧接收误差为零或达到 5 ms 精度认证")
        checked["valid"] = reason is None
        if reason:
            checked["reason"] = reason
        return checked

    def on_result(self, result, sync):
        with self.lock:
            if not self.running:
                return False
            try:
                annotation = self._annotation(_stamp(result))
                baseline = self.baseline
                centers = _peaks(result)
                if baseline.get("status") == "collecting" and baseline.get("lambda0_nm") is None and annotation.get("baseline_id") == baseline["baseline_id"] and _stamp(result) is not None and _stamp(result) >= baseline["requested_monotonic_ns"]:
                    version = result.get("reference_version")
                    reference = result.get("reference_nm")
                    if isinstance(reference, dict) and set(reference) == set(IDS):
                        reference = [reference[key] for key in IDS]
                    if centers is not None and result.get("source_kind") == "hardware" and result.get("stream_epoch") == baseline["optical_epoch"] and type(version) is int and version > baseline["previous_reference_version"] and result.get("reference_pending") is not True and result.get("algorithm_version") == baseline["algorithm_version"] and result.get("config_version") == baseline["config_version"] and isinstance(reference, (list, tuple)) and len(reference) == 9 and all(_finite(value) for value in reference):
                        baseline.update(lambda0_nm=list(reference), reference_version=version, reference_frame_id=result.get("frame_id"))
                        self._baseline_ready()
                locked = self._baselines.get(annotation.get("baseline_id"), {})
                checked = self._checked_sync(result, sync, annotation)
                self.counts["sync_valid" if checked["valid"] else "sync_invalid"] += 1
                x_pm = [(value - zero) * 1000 for value, zero in zip(centers, locked["lambda0_nm"])] if centers is not None and locked.get("lambda0_nm") else None
                corrected = [value - zero for value, zero in zip(checked["force_values"], locked["force_zero"])] if checked["valid"] and locked.get("force_zero") else None
                matching = result.get("algorithm_version") == locked.get("algorithm_version") and result.get("config_version") == locked.get("config_version") and result.get("reference_version") == locked.get("reference_version") and result.get("stream_epoch") == locked.get("optical_epoch")
                bad = bool(annotation.get("manual_mark")) or locked.get("bad") is True or self._trials.get(annotation.get("trial_id"), {}).get("bad") is True
                valid = bool(annotation.get("eligible") and centers is not None and checked["valid"] and locked.get("ready") and matching and result.get("valid") is not False and result.get("stale") is not True and result.get("reference_pending") is not True and not bad)
                normal = self.settings["force_sign"] * corrected[2] if corrected else None
                noise_threshold = max(0.05, 3 * (locked.get("force_noise_N") or 0))
                lateral_ratio = math.hypot(corrected[0], corrected[1]) / abs(corrected[2]) if corrected and abs(corrected[2]) > noise_threshold else None
                contact = annotation.get("point_id") if annotation.get("trial_id") and annotation.get("phase") in {"loading", "hold"} else None
                row = {**annotation, "point_id": contact, "selected_point_id": annotation.get("point_id"), "result": deepcopy(result), "sync": checked,
                       "x_pm": x_pm, "force_N": normal, "force_corrected": corrected, "valid": valid,
                       "source_kind": result.get("source_kind", "hardware"), "algorithm_version": result.get("algorithm_version"), "config_version": result.get("config_version"),
                       "reference_version": result.get("reference_version"), "split": self.settings["batch"], "bad": bad,
                       "lateral_ratio": lateral_ratio, "lateral_warning": lateral_ratio is not None and lateral_ratio > 0.2,
                       "force_limit_exceeded": normal is not None and normal > self.settings["max_force_N"],
                       "force_noise_threshold_N": noise_threshold, "frame_id": result.get("frame_id"), "host_rx_monotonic_ns": _stamp(result)}
                self._submit("results", row)
                self._guided_result(row, centers is not None, matching)
                return True
            except Exception as exc:
                self._fail(exc)
                return False

    def snapshot(self):
        with self.lock:
            writer = self.writer.status() if self.writer else {}
            counts = dict(self.counts)
            for kind in ("raw_spectra", "raw_force", "results", "events"):
                if kind in counts:
                    counts[kind] = writer.get("counts", {}).get(kind, 0)
            return clean({"running": self.running, "session_id": self.session_id, "path": writer.get("path"),
                          "settings": deepcopy(self.settings), "point_id": self.point_id, "trial_id": self.trial_id,
                          "phase": self.phase, "baseline": deepcopy(self.baseline), "status": self.status,
                          "guided": self._guided_snapshot(),
                          "counts": counts, "accepted_counts": {kind: self.counts.get(kind, 0) for kind in ("raw_spectra", "raw_force", "results", "events")}, "writer_queue_depth": writer.get("queue_depth", 0),
                          "saved_counts": writer.get("counts", {}), "error": self.error or writer.get("error")})

    def list_sessions(self):
        return list_sessions(self.userdata)

    def get_session(self, session_id):
        return get_session(self.userdata, session_id)


def get_session(userdata, session_id):
    if not isinstance(session_id, str) or not session_id or Path(session_id).name != session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
        raise ValueError("实验会话标识无效")
    root = (Path(userdata) / "calibration_sessions").resolve()
    path = (root / session_id).resolve()
    if path.parent != root:
        raise ValueError("实验会话路径超出记录目录")
    manifest = path / "manifest.json"
    if not manifest.is_file():
        raise ValueError("实验会话尚未结束或不存在")
    return json.loads(manifest.read_text(encoding="utf-8"))


def _session_summary(manifest):
    """Read saved trial outcomes without changing legacy coordinates or gates.

    Accepted legacy trials are analysis candidates, not physical or formal
    qualification. File completeness and 25-point acquisition are independent.
    """
    settings = manifest.get("settings") if isinstance(manifest.get("settings"), dict) else {}
    final = manifest.get("final") if isinstance(manifest.get("final"), dict) else {}
    trials = final.get("trials") if isinstance(final.get("trials"), list) else []
    trials = [trial for trial in trials if isinstance(trial, dict)]
    guided = settings.get("guided_acquisition") is True
    repetitions = settings.get("repetitions_per_point", 3)
    if type(repetitions) is not int or not 3 <= repetitions <= 999:
        repetitions = 3
    grid = settings.get("point_grid") if isinstance(settings.get("point_grid"), list) else []
    coordinates = {}
    for point in grid:
        if isinstance(point, dict) and isinstance(point.get("id"), str):
            coordinates.setdefault(point["id"], {"row": point.get("row"), "col": point.get("col")})
    points = {}
    accepted_count = rejected_count = pending_count = legacy_count = 0
    for trial in trials:
        quality = trial.get("quality") if isinstance(trial.get("quality"), dict) else {}
        trial_guided = guided or trial.get("guided") is True
        accepted = (trial.get("status") == "completed" and not trial.get("bad") and not trial.get("manual_mark")
                    and (not trial_guided or (quality.get("accepted") is True
                                             and quality.get("criteria_version") == "guided_round_v1")))
        if accepted:
            accepted_count += 1
            legacy_count += int(not trial_guided)
        elif trial.get("status") in {"completed", "aborted"}:
            rejected_count += 1
        else:
            pending_count += 1
        point_id = trial.get("point_id")
        if isinstance(point_id, str) and point_id:
            point = points.setdefault(point_id, {"point_id": point_id, **coordinates.get(point_id, {"row": None, "col": None}), "attempts": 0, "accepted": 0})
            point["attempts"] += 1
            point["accepted"] += int(accepted)
    ordered_ids = [point_id for point_id in coordinates if point_id in points]
    ordered_ids.extend(sorted(set(points) - set(ordered_ids)))
    point_summary = [points[point_id] for point_id in ordered_ids]
    point_count = sum(point["accepted"] > 0 for point in point_summary)
    completed_points = sum(point["accepted"] >= repetitions for point in point_summary)
    required_ids = {"P%02d" % number for number in range(1, 26)}
    acquisition_complete = (guided and required_ids.issubset(coordinates)
                            and all(points.get(point_id, {}).get("accepted", 0) >= repetitions for point_id in required_ids))
    explicit_diagnostic = manifest.get("diagnostic") is True or settings.get("diagnostic") is True
    diagnostic = explicit_diagnostic or (accepted_count == 0 and isinstance(settings.get("finger_id"), str)
                                          and settings["finger_id"].startswith("诊断0.4.2_"))
    complete = manifest.get("complete") is True
    return {"summary_schema": "session_summary_v1", "data_id": settings.get("data_id") if isinstance(settings.get("data_id"), str) else "", "trial_count": len(trials),
            "accepted_trial_count": accepted_count, "rejected_trial_count": rejected_count,
            "pending_trial_count": pending_count, "legacy_candidate_trial_count": legacy_count,
            "has_training_data": complete and accepted_count > 0 and not diagnostic,
            "diagnostic": diagnostic, "started_utc": manifest.get("started_utc"),
            "ended_utc": manifest.get("ended_utc"), "point_summary": point_summary,
            "point_count": point_count, "attempted_point_count": len(point_summary),
            "completed_point_count": completed_points, "required_point_count": 25,
            "repetitions_per_point": repetitions, "total_required": 25 * repetitions,
            "acquisition_complete": acquisition_complete, "formal_training_qualified": False}


def list_sessions(userdata):
    root = Path(userdata) / "calibration_sessions"
    sessions = []
    if root.is_dir():
        for path in sorted(root.iterdir(), reverse=True):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                manifest = get_session(userdata, path.name)
                sessions.append({"session_id": path.name, "path": str(path), "settings": manifest.get("settings", {}), "complete": manifest.get("complete", False), "counts": manifest.get("counts", {}), "error": manifest.get("error"), **_session_summary(manifest)})
            except (ValueError, OSError):
                continue
    return sessions
