"""Source-clock full-spectrum recording, separate from the legacy 3 N gate.

This mixin only records/coordinates experiments. It never creates devices or a
training process. The service acknowledges its short training-pause handshake.
Recovery thresholds are configurable research starting values, not hardware
accuracy or safe-load qualifications.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import statistics
import time

from .no_contact import NO_CONTACT_POINT_ID
from .capture_setup import setup_allows_trial


PROTOCOL = "timed_wrench_v1"
QUALITY_VERSION = "timed_wrench_quality_v1"
SYNC_RULE = {"schema": "timed_wrench_sync_v1", "maximum_bracket_ms": 60,
             "maximum_endpoint_ms": 45, "hardware_delay_calibration": "unknown"}
CRITERIA = {"criteria_version": QUALITY_VERSION, "main_duration_s": 3, "pre_duration_s": 1,
            "recovery_stable_s": .8, "pre_timeout_s": 8, "recovery_timeout_s": 30,
            "training_pause_timeout_s": 20, "maximum_spectrum_gap_ms": 120,
            "maximum_force_gap_ms": 120, "minimum_main_spectra": 60,
            "minimum_raw_valid_ratio": .95, "minimum_label_ratio": .85,
            "contact_force_norm_N": .1, "unloaded_force_norm_floor_N": .1,
            "unloaded_axis_floor": [.1, .1, .1, .005, .005, .005],
            "noise_multiplier": 6, "spectral_noise_multiplier": 8,
            "spectral_recovery_floor_counts": 2, "thresholds_hardware_qualified": False,
            "quality_scope": "raw_full_spectrum_and_six_axis_source_frames",
            "physical_accuracy_qualified": False, "sync_rule": SYNC_RULE}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def stamp(frame):
    value = frame.get("host_rx_monotonic_ns")
    return value if type(value) is int and value > 0 else None


def identity(frame):
    epoch, value = frame.get("stream_epoch"), frame.get("frame_id")
    return (epoch, value) if isinstance(epoch, str) and epoch and type(value) in (str, int) and value != "" else None


def trusted(frame):
    return (frame.get("source_kind") == "hardware" and frame.get("time_quality") in {"host_rx_approx", "hardware_mapped"}
            and frame.get("valid") is not False and frame.get("stale") is not True and stamp(frame) is not None
            and identity(frame) is not None)


def spectrum(frame):
    values, axis = frame.get("intensity"), frame.get("wavelength_nm")
    if (not trusted(frame) or not isinstance(values, (list, tuple)) or len(values) != 512
            or not all(finite(value) for value in values) or not isinstance(axis, (list, tuple))
            or len(axis) != 512 or not all(finite(value) for value in axis)
            or any(right <= left for left, right in zip(axis, axis[1:]))):
        return None
    preprocessing = deepcopy(frame.get("preprocessing") or {})
    payload = {"wavelength_nm": list(axis), "preprocessing": preprocessing}
    axis_id = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    return {"identity": identity(frame), "stamp": stamp(frame), "values": list(values),
            "axis": list(axis), "axis_id": axis_id, "preprocessing": preprocessing}


def timed_settings(value):
    protocol = value.get("acquisition_protocol")
    if protocol not in {None, "manual_v1", "guided_round_v1", PROTOCOL, "force_target_wrench_v1"}:
        raise ValueError("未知实验采集协议")
    if protocol != PROTOCOL:
        return
    value["guided_acquisition"] = True
    for key, expected in (("pre_duration_s", 1), ("main_duration_s", 3)):
        if not finite(value.get(key, expected)) or value.get(key, expected) != expected:
            raise ValueError("全谱协议固定采前无载 1 秒、主按压 3 秒")
        value[key] = expected
    maximum = value.setdefault("max_force_norm_N", 5)
    if not finite(maximum) or not 0 < maximum <= 100:
        raise ValueError("三维合力记录上限必须为大于 0 且不超过 100 N 的有限数值")
    rules = deepcopy(CRITERIA)
    overrides = value.get("timed_wrench_criteria", {})
    if not isinstance(overrides, dict) or set(overrides) - set(CRITERIA):
        raise ValueError("全谱采集质量设置包含未知字段")
    configurable = {"unloaded_force_norm_floor_N", "contact_force_norm_N", "spectral_recovery_floor_counts", "unloaded_axis_floor"}
    if any(overrides[key] != CRITERIA[key] for key in overrides if key not in configurable):
        raise ValueError("固定采集/同步规则不能静默修改；更改需要新的规则版本")
    for key in ("unloaded_force_norm_floor_N", "contact_force_norm_N", "spectral_recovery_floor_counts"):
        if key in overrides:
            if not finite(overrides[key]) or overrides[key] <= 0:
                raise ValueError("无载检查阈值必须为有限正数")
            rules[key] = overrides[key]
    if "unloaded_axis_floor" in overrides:
        floors = overrides["unloaded_axis_floor"]
        if not isinstance(floors, list) or len(floors) != 6 or not all(finite(v) and v > 0 for v in floors):
            raise ValueError("六轴无载检查阈值需要 6 个有限正数")
        rules["unloaded_axis_floor"] = list(floors)
    value["timed_wrench_criteria"] = rules
    value["six_axis_definition"] = {"axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"],
                                    "units": ["N", "N", "N", "N·m", "N·m", "N·m"],
                                    "frame": "PX6D", "origin": "PX6D_defined", "rotation_applied": False,
                                    "force_sign_applied": False}


class TimedWrenchMixin:
    def _wrench_protocol(self):
        return self.settings.get("acquisition_protocol", PROTOCOL)

    def _wrench_rules(self):
        key = "force_target_wrench_criteria" if self._wrench_protocol() == "force_target_wrench_v1" else "timed_wrench_criteria"
        return self.settings[key]

    def _wrench_quality_version(self):
        return self._wrench_rules()["criteria_version"]

    def _timed_reset(self, enabled):
        self._timed = {"enabled": enabled, "trial": None, "spectra": [], "spectra_seen": set(),
                       "last_quality": None, "pause_reason": None, "current_wrench": None,
                       "force_norm_N": None, "last_force_stamp_ns": None,
                       "recovery_message": None}

    def _timed_select_point(self, point_id):
        if self.baseline.get("ready") and not self.baseline.get("used"):
            self.baseline["point_id"] = point_id
            self._guided["step"] = "ready"
        else:
            self.baseline = {"status": "required", "ready": False}
            self._guided["step"] = "needs_baseline"
        self.phase = "idle"

    def _timed_baseline_request(self, current):
        self._baseline_number += 1
        self.baseline = {"baseline_id": f"B{self._baseline_number:04d}", "point_id": self.point_id,
                         "status": "collecting", "ready": False, "used": False, "unloaded_confirmed": True,
                         "trigger": "manual_unloaded_confirmation", "reference_kind": "spectrum_unloaded_median_v1",
                         "quality_version": self._wrench_quality_version(), "force_zero": None, "force_noise_N": None,
                         "force_noise_axes": None, "lambda0_nm": None, "S0": None,
                         "reference_version": current["reference_version"],
                         "previous_reference_version": current["reference_version"], **self.context}
        self._baselines[self.baseline["baseline_id"]] = self.baseline
        self._zero_samples, self._zero_seen = [], set()
        self._timed["spectra"], self._timed["spectra_seen"] = [], set()
        self.phase = "unloaded"
        self._guided["step"] = "calibrating"
        self.baseline["requested_monotonic_ns"] = self._transition("unloaded_confirmation", unloaded_confirmed=True,
                                                                 acquisition_protocol=self._wrench_protocol())
        return self.snapshot()

    def _timed_reference(self, samples, baseline_id, effective):
        S0 = [statistics.median(row["values"][index] for row in samples) for index in range(512)]
        noise = [1.4826 * statistics.median(abs(row["values"][index] - S0[index]) for row in samples)
                 for index in range(512)]
        return {"schema": "spectrum_unloaded_median_v1", "reference_id": self.session_id + ":" + baseline_id,
                "S0": S0, "wavelength_nm": samples[0]["axis"], "preprocessing": samples[0]["preprocessing"],
                "source_frame_ids": [row["identity"][1] for row in samples],
                "source_stamps_ns": [row["stamp"] for row in samples], "stream_epoch": self.context["optical_epoch"],
                "effective_ns": effective, "axis_id": samples[0]["axis_id"], "noise_mad_scaled": noise,
                "noise_rms": math.sqrt(sum(value*value for value in noise)/512)}

    @staticmethod
    def _timed_assign_reference(baseline, reference):
        baseline.update(spectral_reference=reference, S0=list(reference["S0"]), wavelength_nm=list(reference["wavelength_nm"]),
                        spectral_reference_id=reference["reference_id"], spectral_axis_id=reference["axis_id"],
                        source_frame_ids=list(reference["source_frame_ids"]), source_stamps_ns=list(reference["source_stamps_ns"]))

    def _timed_baseline_ready(self):
        baseline = self.baseline
        samples = self._timed["spectra"]
        if (baseline.get("status") != "collecting" or baseline.get("force_zero") is None
                or len(samples) < 12 or samples[-1]["stamp"] - samples[0]["stamp"] < 400_000_000):
            return
        effective = max(time.perf_counter_ns(), samples[-1]["stamp"], (baseline.get("force_source_stamps_ns") or [0])[-1])
        reference = self._timed_reference(samples, baseline["baseline_id"], effective)
        self._timed_assign_reference(baseline, reference)
        baseline.update(status="ready", ready=True, effective_monotonic_ns=effective)
        self._guided["step"] = "ready" if self.point_id else "select_point"
        self._transition("baseline_ready", baseline=deepcopy(baseline), acquisition_protocol=self._wrench_protocol())

    def _timed_trial(self, action, current):
        if action == "start":
            if self.trial_id:
                raise ValueError("当前试次尚未结束")
            if self.point_id is None:
                raise ValueError("请选中点位并明确确认位置")
            independent_no_contact = (self._wrench_protocol() == "force_target_wrench_v1"
                                      and self.point_id == NO_CONTACT_POINT_ID)
            if not independent_no_contact and self._guided["accepted_by_point"][self.point_id] >= self.settings["repetitions_per_point"]:
                raise ValueError("当前点已完成合格重复次数，请确认下一点")
            self._same_context(current)
            if not setup_allows_trial(self.settings, requires_force_direction=False):
                raise ValueError("请先明确确认 25 点坐标")
            if not self.baseline.get("ready") or self.baseline.get("used") or self.baseline.get("point_id") != self.point_id:
                raise ValueError("请先明确确认无载并取得有效 512 点参考与六轴零偏")
            self._trial_number += 1
            self.trial_id = f"T{self._trial_number:04d}"
            self.baseline["used"] = True
            self.phase = "waiting_training"
            self._guided["attempts_by_point"][self.point_id] += 1
            self._trials[self.trial_id] = {"trial_id": self.trial_id, "point_id": self.point_id,
                                          "baseline_id": self.baseline["baseline_id"], "status": "active",
                                          "manual_mark": [], "guided": True, "acquisition_protocol": self._wrench_protocol(),
                                          "criteria_version": self._wrench_quality_version()}
            started = self._transition("trial_start", baseline=deepcopy(self.baseline), acquisition_protocol=self._wrench_protocol())
            self._timed["pause_reason"] = "acquisition:" + self.trial_id
            self._timed["trial"] = {"trial_id": self.trial_id, "point_id": self.point_id,
                                    "baseline_id": self.baseline["baseline_id"], "started_ns": started,
                                    "pre_start_ns": None, "pre_unloaded_first_ns": None, "pre_unloaded_span_s": 0,
                                    "main_start_ns": None, "main_end_ns": None,
                                    "last_spectrum_ns": None, "last_force_ns": None,
                                    "spectra_seen": set(), "force_seen": set(), "main_spectra": [],
                                    "force_samples": [], "recovery_spectra": [], "recovery_force": [],
                                    "raw_total": 0, "raw_valid": 0, "force_total": 0, "force_valid": 0,
                                    "peak_processed_frames": 0, "peak_valid_frames": 0,
                                    "peak_force_norm_N": 0, "elapsed_s": 0, "faults": set(),
                                    "manual_recovery_confirmed": False, "unloaded_evidence": None}
            self._guided["step"] = "waiting_training"
        elif action == "abort":
            if not self.trial_id:
                raise ValueError("当前没有活动试次")
            self._timed_finish(aborted=True, reason="操作员终止本轮")
        elif action in {"confirm_unloaded", "finish"}:
            if not self.trial_id or self.phase != "recovery":
                raise ValueError("请在 3 秒主采集结束并移开接触棒后确认无载")
            self._timed["trial"]["manual_recovery_confirmed"] = True
            self._transition("recovery_unloaded_confirmation", unloaded_confirmed=True)
            self._timed_check_recovery()
        else:
            raise ValueError("全谱试次由源帧推进；只允许开始、中止或卸载确认")
        return self.snapshot()

    def training_pause_acknowledged(self):
        """Service hook: invoke only after no training task or paused ACK.

        This method is deliberately separate from snapshot/GET state. The
        service retains and clears only the acquisition:<trial_id> pause reason.
        """
        with self.lock:
            if not self.running or not self._timed.get("enabled") or not self.trial_id or self.phase != "waiting_training":
                return False
            boundary = max(time.perf_counter_ns(), self._timeline[-1][0])
            self._timed["trial"]["pre_start_ns"] = boundary
            self.phase = "preload"
            self._guided["step"] = "preload"
            self._transition("training_pause_acknowledged", at_ns=boundary,
                             pause_reason=self._timed["pause_reason"], acquisition_protocol=PROTOCOL)
            return True

    def _timed_spectrum(self, frame, annotation):
        sample, timestamp, key = spectrum(frame), stamp(frame), identity(frame)
        if annotation.get("eligible") and frame.get("stream_epoch") != self.context["optical_epoch"]:
            return
        baseline = self.baseline
        if baseline.get("status") == "collecting" and sample and timestamp >= baseline["requested_monotonic_ns"]:
            if key not in self._timed["spectra_seen"]:
                self._timed["spectra_seen"].add(key)
                samples = self._timed["spectra"]
                if samples and (timestamp <= samples[-1]["stamp"] or sample["axis_id"] != samples[0]["axis_id"]):
                    baseline.update(status="invalid", ready=False, reason="无载原始光谱轴或时间不连续")
                else:
                    samples.append(sample)
                    self._timed_baseline_ready()
        state = self._timed.get("trial")
        if not state or annotation.get("trial_id") != state["trial_id"] or not self.trial_id:
            return
        if key in state["spectra_seen"]:
            state["faults"].add("原始光谱帧身份重复")
            return
        if key:
            state["spectra_seen"].add(key)
        if annotation["phase"] == "waiting_training":
            return
        state["raw_total"] += 1
        if sample is None or sample["axis_id"] != self.baseline["spectral_axis_id"]:
            state["faults"].add("原始光谱不是完整 512 点或轴/采样设置改变")
            return
        previous = state["last_spectrum_ns"]
        if previous is not None and (timestamp <= previous or timestamp - previous > self._wrench_rules()["maximum_spectrum_gap_ms"] * 1e6):
            state["faults"].add("原始光谱时间乱序或间断")
        state["last_spectrum_ns"] = timestamp
        state["raw_valid"] += 1
        if annotation["phase"] == "loading":
            state["main_spectra"].append(sample)
        if annotation["phase"] == "recovery":
            sample["recovery_rms"] = math.sqrt(sum((value-zero)**2 for value, zero in zip(sample["values"], self.baseline["S0"]))/512)
            state["recovery_spectra"].append(sample)
        if self.phase == "preload" and timestamp - state["pre_start_ns"] >= 1_000_000_000:
            force_stamp = state["last_force_ns"]
            if (self._timed_near_zero() and state["pre_unloaded_span_s"] >= 1
                    and force_stamp is not None and abs(timestamp-force_stamp) <= 120_000_000):
                state["main_start_ns"] = timestamp
                self.phase = "loading"
                self._guided["step"] = "loading"
                self._transition("timed_main_start", at_ns=self._guided_boundary(timestamp), trigger_source_stamp_ns=timestamp)
            else:
                self._timed["recovery_message"] = "采前合力尚未接近无载，请保持接触棒移开"
        elif self.phase == "loading":
            state["elapsed_s"] = min(3, (timestamp - state["main_start_ns"]) / 1e9)
        if self.phase == "recovery":
            self._timed_check_recovery()

    def _timed_before_source(self, frame):
        """Set the fixed source-time boundary before annotating the raw frame."""
        state, timestamp = self._timed.get("trial"), stamp(frame)
        if (not state or self.phase != "loading" or timestamp is None
                or frame.get("stream_epoch") not in {self.context["optical_epoch"], self.context["force_epoch"]}):
            return
        end = state["main_start_ns"] + 3_000_000_000
        if timestamp < end:
            return
        state["elapsed_s"], state["main_end_ns"] = 3, end
        self.phase = "recovery"
        self._guided["step"] = "unloading"
        boundary = max(end, self._timeline[-1][0])
        self._transition("timed_main_end", at_ns=boundary, trigger_source_stamp_ns=timestamp,
                         planned_main_end_monotonic_ns=end,
                         message="3 秒主采集已结束，请移开接触棒；继续记录卸载与恢复")

    def _timed_force(self, frame, annotation, values):
        if values is not None and trusted(frame) and self.baseline.get("force_zero"):
            corrected = [value - zero for value, zero in zip(values, self.baseline["force_zero"])]
            self._timed.update(current_wrench=corrected, force_norm_N=math.sqrt(sum(value * value for value in corrected[:3])),
                               last_force_stamp_ns=stamp(frame))
        state = self._timed.get("trial")
        if not state or not self.trial_id or annotation.get("trial_id") != state["trial_id"]:
            return
        if annotation["phase"] == "waiting_training":
            return
        timestamp, key = stamp(frame), identity(frame)
        if key in state["force_seen"]:
            state["faults"].add("六轴源帧身份重复")
            return
        if key:
            state["force_seen"].add(key)
        state["force_total"] += 1
        if values is None or not trusted(frame) or frame.get("stream_epoch") != self.context["force_epoch"]:
            state["faults"].add("六轴源帧无效或时段改变")
            return
        previous = state["last_force_ns"]
        if previous is not None and (timestamp <= previous or timestamp - previous > self._wrench_rules()["maximum_force_gap_ms"] * 1e6):
            state["faults"].add("六轴源帧时间乱序或间断")
        state["last_force_ns"] = timestamp
        state["force_valid"] += 1
        record = {"stamp": timestamp, "identity": key, "values": list(values)}
        state["force_samples"].append(record)
        norm = self._timed["force_norm_N"]
        state["peak_force_norm_N"] = max(state["peak_force_norm_N"], norm or 0)
        if norm is not None and norm > self.settings["max_force_norm_N"]:
            state["faults"].add("观测三维合力超过设定上限，真实六轴读数已保留")
        if annotation["phase"] == "preload":
            if self._timed_near_zero():
                if state["pre_unloaded_first_ns"] is None:
                    state["pre_unloaded_first_ns"] = timestamp
                state["pre_unloaded_span_s"] = (timestamp-state["pre_unloaded_first_ns"])/1e9
            else:
                state["pre_unloaded_first_ns"] = None
                state["pre_unloaded_span_s"] = 0
        if annotation["phase"] == "recovery":
            state["recovery_force"].append(record)
            # The optical source path performs the recovery check. Keep force
            # receive work independent of 512-column medians and finalization.

    def _timed_thresholds(self):
        rules = self._wrench_rules()
        noise = self.baseline.get("force_noise_axes") or [0] * 6
        return [max(floor, rules["noise_multiplier"] * value) for floor, value in zip(rules["unloaded_axis_floor"], noise)]

    def _timed_near_zero(self):
        values = self._timed.get("current_wrench")
        rules = self._wrench_rules()
        thresholds = self._timed_thresholds()
        noise = self.baseline.get("force_noise_axes") or [0]*6
        norm_limit = max(rules["unloaded_force_norm_floor_N"], rules["noise_multiplier"] * math.sqrt(sum(value * value for value in noise[:3])))
        return bool(values is not None and all(abs(v) <= threshold for v, threshold in zip(values, thresholds))
                    and self._timed["force_norm_N"] <= norm_limit)

    def _timed_check_recovery(self):
        state = self._timed.get("trial")
        if not state or self.phase != "recovery":
            return
        force_rows, spectral_rows = state["recovery_force"], state["recovery_spectra"]
        if not force_rows or not spectral_rows:
            return
        latest = min(force_rows[-1]["stamp"], spectral_rows[-1]["stamp"])
        first = latest - 800_000_000
        force_rows = [row for row in force_rows if first - 45_000_000 <= row["stamp"] <= latest]
        spectral_rows = [row for row in spectral_rows if first - 45_000_000 <= row["stamp"] <= latest]
        if (len(force_rows) < 12 or len(spectral_rows) < 12 or force_rows[-1]["stamp"] - force_rows[0]["stamp"] < 800_000_000
                or spectral_rows[-1]["stamp"] - spectral_rows[0]["stamp"] < 800_000_000):
            self._timed["recovery_message"] = "正在等待连续至少 0.8 秒的六轴与光谱恢复证据"
            return
        thresholds = self._timed_thresholds()
        zero = self.baseline["force_zero"]
        corrected = [[v - z for v, z in zip(row["values"], zero)] for row in force_rows]
        rules = self._wrench_rules()
        noise = self.baseline.get("force_noise_axes") or [0]*6
        norm_limit = max(rules["unloaded_force_norm_floor_N"], rules["noise_multiplier"] * math.sqrt(sum(value*value for value in noise[:3])))
        near = self._timed_near_zero() and all(math.sqrt(sum(value*value for value in row[:3])) <= norm_limit
                                               and all(abs(value) <= limit for value, limit in zip(row, thresholds))
                                               for row in corrected)
        variation = [max(row[axis] for row in corrected) - min(row[axis] for row in corrected) for axis in range(6)]
        rates = [max(abs(right["values"][axis] - left["values"][axis]) / ((right["stamp"] - left["stamp"]) / 1e9)
                     for left, right in zip(force_rows, force_rows[1:]) if right["stamp"] > left["stamp"]) for axis in range(6)]
        stable = all(value <= limit for value, limit in zip(variation, thresholds)) and all(value <= limit * 20 for value, limit in zip(rates, thresholds))
        if not near or not stable:
            self._timed["recovery_message"] = ("六轴尚未接近上次有效零点；稳定非零载荷不会回零" if not near
                                               else "六轴波动或变化率尚未稳定")
            return
        reference = self.baseline["spectral_reference"]
        # Each raw spectrum's RMS is computed once when it arrives. The entire
        # sustained recovery window must recover; a transient median cannot
        # hide a still-loaded spectrum. Column medians run once on completion.
        rms = max(row["recovery_rms"] for row in spectral_rows)
        noise_rms = reference["noise_rms"]
        rules = self._wrench_rules()
        spectrum_limit = max(rules["spectral_recovery_floor_counts"], noise_rms * rules["spectral_noise_multiplier"])
        if rms > spectrum_limit:
            self._timed["recovery_message"] = "光谱尚未恢复，请继续等待材料恢复并确认无载"
            return
        state["unloaded_evidence"] = {"schema": "timed_unloaded_evidence_v1", "stable_duration_s": .8,
                                      "previous_baseline_id": self.baseline["baseline_id"], "axis_thresholds": thresholds,
                                      "force_norm_threshold_N": norm_limit,
                                      "axis_variation": variation, "axis_rate_per_s": rates,
                                      "spectrum_recovery_rms": rms, "spectrum_recovery_limit": spectrum_limit,
                                      "force_frame_ids": [row["identity"][1] for row in force_rows],
                                      "spectrum_frame_ids": [row["identity"][1] for row in spectral_rows],
                                      "trigger": "manual_unloaded_confirmation" if state["manual_recovery_confirmed"] else "automatic_unloaded_recovery",
                                      "thresholds_hardware_qualified": False}
        self._timed_finish(recovery_force=force_rows, recovery_spectra=spectral_rows)

    def _timed_label_coverage(self, state):
        rows = sorted(state["force_samples"], key=lambda row: row["stamp"])
        valid = 0
        rejected = {}
        for sample in state["main_spectra"]:
            timestamp = sample["stamp"]
            left = next((row for row in reversed(rows) if row["stamp"] <= timestamp), None)
            right = next((row for row in rows if row["stamp"] >= timestamp), None)
            if left and right and (left["stamp"] == right["stamp"] or
                    (right["stamp"] - left["stamp"] <= 60_000_000 and max(timestamp-left["stamp"], right["stamp"]-timestamp) <= 45_000_000)):
                valid += 1
            else:
                rejected["missing_or_distant_bracket"] = rejected.get("missing_or_distant_bracket", 0) + 1
        return valid, rejected

    def _timed_finish(self, *, aborted=False, reason=None, recovery_force=None, recovery_spectra=None):
        state = self._timed.get("trial")
        if not state or not self.trial_id:
            return
        if reason:
            state["faults"].add(reason)
        valid_labels, rejected = self._timed_label_coverage(state)
        main_count = len(state["main_spectra"])
        ratio = state["raw_valid"] / max(1, state["raw_total"])
        label_ratio = valid_labels / max(1, main_count)
        reasons = sorted(state["faults"])
        if state["elapsed_s"] < 3:
            reasons.append("主采集源时间不足 3 秒")
        if main_count < 60:
            reasons.append("主采集完整原始光谱少于 60 帧")
        if ratio < .95:
            reasons.append("完整原始光谱比例低于 95%")
        if label_ratio < .85:
            reasons.append("可信六轴标签覆盖比例低于 85%")
        if not state["unloaded_evidence"]:
            reasons.append("未取得有效卸载与材料恢复证据")
        if self._trials[state["trial_id"]].get("manual_mark"):
            reasons.append("本轮有人工异常标记")
        metrics = {"main_duration_s": state["elapsed_s"], "main_raw_spectra": main_count,
                   "pre_unloaded_duration_s": state["pre_unloaded_span_s"],
                   "raw_spectra": state["raw_total"], "raw_valid_ratio": ratio,
                   "valid_label_count": valid_labels, "valid_label_ratio": label_ratio, "label_rejections": rejected,
                   "peak_force_norm_N": state["peak_force_norm_N"], "max_force_norm_N": self.settings["max_force_norm_N"],
                   "force_frames": state["force_total"], "force_valid_frames": state["force_valid"],
                   "peak_processed_frames": state["peak_processed_frames"], "peak_valid_frames": state["peak_valid_frames"],
                   "unloaded_evidence": deepcopy(state["unloaded_evidence"]), "sync_rule": deepcopy(SYNC_RULE),
                   "physical_accuracy_qualified": False}
        quality = {"accepted": not aborted and not reasons, "reasons": reasons, "metrics": metrics,
                   "criteria_version": QUALITY_VERSION, "acquisition_protocol": PROTOCOL}
        trial = self._trials[state["trial_id"]]
        trial.update(status="aborted" if aborted else "completed", bad=not quality["accepted"], quality=deepcopy(quality),
                     main_start_monotonic_ns=state["main_start_ns"], main_end_monotonic_ns=state["main_end_ns"])
        if reason:
            trial["stop_reason"] = reason
        self.counts["trials_aborted" if aborted else "trials_completed"] += 1
        if quality["accepted"]:
            self._guided["accepted_by_point"][state["point_id"]] += 1
        self._guided["last_quality"] = self._timed["last_quality"] = deepcopy(quality)
        self._transition("timed_round_quality", quality=deepcopy(quality), completed_trial_id=state["trial_id"])
        previous = self.baseline
        self.trial_id, self.phase = None, "idle"
        self._timed["trial"], self._timed["pause_reason"] = None, None
        self._timed["recovery_message"] = None
        self.baseline = {"status": "required", "ready": False}
        self._guided["step"] = "round_complete" if quality["accepted"] else "retry"
        if self._guided["accepted_by_point"][state["point_id"]] >= self.settings["repetitions_per_point"]:
            self._guided["step"] = "point_complete"
        if all(value >= self.settings["repetitions_per_point"] for value in self._guided["accepted_by_point"].values()):
            self._guided["step"] = "session_complete"
        if not aborted and recovery_force and recovery_spectra:
            self._baseline_number += 1
            baseline_id = f"B{self._baseline_number:04d}"
            offsets = [statistics.median(row["values"][axis] for row in recovery_force) for axis in range(6)]
            noise = [1.4826 * statistics.median(abs(row["values"][axis]-offsets[axis]) for row in recovery_force) for axis in range(6)]
            effective = max(time.perf_counter_ns(), recovery_force[-1]["stamp"], recovery_spectra[-1]["stamp"])
            self.baseline = {"baseline_id": baseline_id, "point_id": self.point_id, "status": "ready", "ready": True,
                             "used": False, "unloaded_confirmed": True, "trigger": state["unloaded_evidence"]["trigger"],
                             "unloaded_evidence": deepcopy(state["unloaded_evidence"]), "quality_version": QUALITY_VERSION,
                             "reference_kind": "spectrum_unloaded_median_v1", "force_zero": offsets,
                             "force_noise_axes": noise, "force_noise_N": noise[2], "lambda0_nm": None,
                             "force_frame_ids": [row["identity"][1] for row in recovery_force],
                             "force_source_stamps_ns": [row["stamp"] for row in recovery_force],
                             "prepared_from_baseline_id": previous["baseline_id"], "previous_reference_version": previous.get("reference_version"),
                             "reference_version": previous.get("reference_version"), "requested_monotonic_ns": recovery_spectra[0]["stamp"],
                             "effective_monotonic_ns": effective, **self.context}
            self._timed_assign_reference(self.baseline, self._timed_reference(recovery_spectra, baseline_id, effective))
            self._baselines[baseline_id] = self.baseline
            self._transition("baseline_ready", baseline=deepcopy(self.baseline), acquisition_protocol=PROTOCOL)
        self._transition("next_round_prepared" if self.baseline.get("ready") else "next_baseline_required")

    def _timed_watchdog(self, now):
        state = self._timed.get("trial")
        if not self.running or not state or not self.trial_id:
            return False
        elapsed = (now - state["started_ns"]) / 1e9
        reason = None
        if self.phase == "waiting_training" and elapsed > 20:
            reason = "训练暂停确认超时，本轮未开始正式采集"
        elif self.phase == "preload" and now - state["pre_start_ns"] > 8_000_000_000:
            reason = "采前无载记录超时，请重新确认无载"
        elif self.phase == "recovery" and now - state["main_end_ns"] > 30_000_000_000:
            reason = "卸载恢复超过 30 秒，证据不足；本轮数据保留但不计为合格"
        elif self.phase in {"preload", "loading", "recovery"}:
            last = min(state["last_force_ns"] or state["pre_start_ns"], state["last_spectrum_ns"] or state["pre_start_ns"])
            if now - last > 2_000_000_000:
                reason = "正式采集期间源帧断流超过 2 秒"
        if reason:
            self._timed_finish(aborted=True, reason=reason)
            return True
        return False

    def _timed_snapshot(self):
        guided, state = self._guided, self._timed.get("trial")
        repetitions = self.settings["repetitions_per_point"]
        accepted, attempts = guided["accepted_by_point"].get(self.point_id, 0), guided["attempts_by_point"].get(self.point_id, 0)
        messages = {"select_point": "请选中点位并明确确认位置", "needs_baseline": "请移开接触棒，确认无载并建立 512 点参考",
                    "calibrating": "正在采集真实 512 点无载参考与六轴软件零偏", "ready": "全谱参考已就绪，可以开始本轮采集",
                    "waiting_training": "正在等待后台训练暂停确认", "preload": "正在记录 1 秒采前无载，请保持接触棒移开",
                    "loading": "正在记录 3 秒主按压；实际接触点保持在所选位置", "unloading": "请移开接触棒，继续记录卸载与材料恢复",
                    "round_complete": "本轮合格，下一轮无载参考已准备", "retry": "本轮未通过质量检查；原始记录已保留"}
        step = guided["step"]
        total = sum(guided["accepted_by_point"].values())
        return {"enabled": True, "protocol": PROTOCOL, "criteria_version": QUALITY_VERSION, "step": step,
                "message": self._timed.get("recovery_message") or messages.get(step, "请确认下一点"),
                "accepted_by_point": deepcopy(guided["accepted_by_point"]), "attempts_by_point": deepcopy(guided["attempts_by_point"]),
                "repetitions_per_point": repetitions, "current_round": min(accepted+1, repetitions), "point_accepted": accepted,
                "point_attempts": attempts, "total_accepted": total, "total_required": 25*repetitions,
                "progress_percent": total/(25*repetitions)*100, "point_progress_percent": accepted/repetitions*100,
                "force_progress_percent": min(100, (self._timed.get("force_norm_N") or 0)/self.settings["max_force_norm_N"]*100),
                "force_norm_N": self._timed.get("force_norm_N"), "current_wrench": deepcopy(self._timed.get("current_wrench")),
                "force_source_stamp_ns": self._timed.get("last_force_stamp_ns"),
                "observed_peak_force_norm_N": state["peak_force_norm_N"] if state else (self._timed.get("last_quality") or {}).get("metrics", {}).get("peak_force_norm_N"),
                "elapsed_s": state["elapsed_s"] if state else 0, "duration_s": 3,
                "awaiting_training_pause": self.phase == "waiting_training", "pause_reason": self._timed.get("pause_reason"),
                "last_quality": deepcopy(self._timed.get("last_quality")), "complete": total >= 25*repetitions,
                "preload": None, "physical_accuracy_qualified": False}
