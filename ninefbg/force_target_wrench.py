"""Continuous target-force recording with explicit finish and durable checkpoints.

The target is a prompt, not a truncation or an automatic end. Quality v2
records a 2-second no-contact lead-in and treats manual finish as the operator's
unloaded assertion. The next zero/reference uses the first trusted source frame
from each device strictly after that assertion, without an automatic recovery
gate. The historical timed protocol and quality-v1 records keep their meanings.
"""
from __future__ import annotations

from bisect import bisect_left
from copy import deepcopy
import math
import statistics
import time

from .timed_wrench import CRITERIA as TIMED_CRITERIA, SYNC_RULE, TimedWrenchMixin, finite, identity, spectrum, stamp, trusted
from .no_contact import (NO_CONTACT_POINT_ID, NO_CONTACT_QUALITY_VERSION, CONTACT_LABEL_VERSION,
                         no_contact_criteria, no_contact_limits)

PROTOCOL = "force_target_wrench_v1"
QUALITY_VERSION = "force_target_wrench_quality_v2"
RANGE_POLICY = "target_strictly_below_upper_v1"
CRITERIA = {key: deepcopy(value) for key, value in TIMED_CRITERIA.items()
            if key not in {"main_duration_s", "pre_duration_s", "pre_timeout_s", "minimum_main_spectra"}}
CRITERIA.update(criteria_version=QUALITY_VERSION, minimum_contact_spectra=16,
                checkpoint_timeout_s=30, quality_scope="continuous_raw_contact_and_six_axis_source_frames",
                target_semantics="observed_force_norm_prompt_only_manual_finish", preload_duration_s=2,
                finish_zero_semantics="operator_unloaded_assertion_first_post_finish_source_frames",
                automatic_unloaded_recovery_required=False, zero_source_frame_count=1)


def _label_values(rows, times, timestamp):
    """Match the unchanged exact/60 ms bracket/45 ms endpoint rule."""
    index = bisect_left(times, timestamp)
    if index < len(rows) and times[index] == timestamp:
        return rows[index]["values"]
    if (0 < index < len(rows) and times[index]-times[index-1] <= 60_000_000
            and max(timestamp-times[index-1], times[index]-timestamp) <= 45_000_000):
        fraction = (timestamp-times[index-1])/(times[index]-times[index-1])
        return [a+(b-a)*fraction for a, b in zip(rows[index-1]["values"], rows[index]["values"])]
    return None


def force_target_settings(value):
    if value.get("acquisition_protocol") != PROTOCOL:
        return
    value["guided_acquisition"] = True
    maximum = value.setdefault("max_force_norm_N", 5)
    target = value.setdefault("target_force_norm_N", 4)
    if not finite(maximum) or not 0 < maximum <= 100 or not finite(target) or not 0 < target <= maximum:
        raise ValueError("目标三维合力须为大于 0 且不超过独立合力上限的有限数值")
    if target == maximum:
        raise ValueError("目标三维合力必须低于合力上限；目标与上限相同会使达标后的轻微超调判为超限。请明确设置不同数值后再建立会话")
    if value.get("force_target_range_policy", RANGE_POLICY) != RANGE_POLICY:
        raise ValueError("新会话的合力目标与上限设置策略版本无效")
    value["force_target_range_policy"] = RANGE_POLICY
    if "main_duration_s" in value or "pre_duration_s" in value:
        raise ValueError("目标合力协议不使用固定采前时间或 3 秒主段，请使用独立协议设置")
    rules = deepcopy(CRITERIA)
    supplied = value.get("force_target_wrench_criteria", {})
    configurable = {"unloaded_force_norm_floor_N", "contact_force_norm_N", "spectral_recovery_floor_counts", "unloaded_axis_floor"}
    if not isinstance(supplied, dict) or set(supplied) - set(CRITERIA):
        raise ValueError("目标合力采集质量设置包含未知字段")
    if any(supplied[key] != CRITERIA[key] for key in supplied if key not in configurable):
        raise ValueError("目标合力采集/同步规则改变需要新的规则版本")
    for key in configurable - {"unloaded_axis_floor"}:
        if key in supplied:
            if not finite(supplied[key]) or supplied[key] <= 0:
                raise ValueError("无载与接触检查阈值必须为有限正数")
            rules[key] = supplied[key]
    if "unloaded_axis_floor" in supplied:
        floors = supplied["unloaded_axis_floor"]
        if not isinstance(floors, list) or len(floors) != 6 or not all(finite(v) and v > 0 for v in floors):
            raise ValueError("六轴无载检查阈值需要 6 个有限正数")
        rules["unloaded_axis_floor"] = list(floors)
    if target <= rules["contact_force_norm_N"]:
        raise ValueError("目标合力必须高于有效接触阈值")
    value["force_target_wrench_criteria"] = rules
    no_contact_rules = no_contact_criteria(rules)
    if "no_contact_criteria" in value and value["no_contact_criteria"] != no_contact_rules:
        raise ValueError("无接触采集规则改变需要新的规则版本")
    value["no_contact_criteria"] = no_contact_rules
    value["six_axis_definition"] = {"axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"],
                                    "units": ["N", "N", "N", "N·m", "N·m", "N·m"],
                                    "frame": "PX6D", "origin": "PX6D_defined", "rotation_applied": False,
                                    "force_sign_applied": False}


class ForceTargetWrenchMixin(TimedWrenchMixin):
    def _no_contact_selected(self):
        return self._force_target_protocol() and self.point_id == NO_CONTACT_POINT_ID

    def _wrench_rules(self):
        if self._no_contact_selected():
            return self.settings["no_contact_criteria"]
        return super()._wrench_rules()

    def _force_target_protocol(self):
        return self.settings.get("acquisition_protocol") == PROTOCOL

    def _timed_trial(self, action, current):
        if not self._force_target_protocol():
            return super()._timed_trial(action, current)
        if action == "start":
            super()._timed_trial(action, current)
            state = self._timed["trial"]
            state.update(target_reached=False, target_reached_ns=None, finish_requested=False,
                         finish_requested_ns=None, contact_detected=False, data_end_ns=None,
                         checkpoint_token=None, checkpoint_sequence=0, frozen_quality=None, next_baseline=None,
                         main_spectra_seen=set(), preload_elapsed_s=0, preload_contact_detected=False,
                         preload_force_first_ns=None, preload_spectrum_first_ns=None,
                         press_prompt_ns=None, zero_requested_ns=None)
            if self._no_contact_selected():
                state.update(no_contact=True, peak_abs_wrench=[0.] * 6, no_contact_peak_force_norm_N=0.)
                self._trials[self.trial_id].update(sample_label="no_contact", sample_label_version=CONTACT_LABEL_VERSION,
                                                  operator_confirmed=True)
            self._trials[self.trial_id].update(target_force_norm_N=self.settings["target_force_norm_N"],
                                              max_force_norm_N=self.settings["max_force_norm_N"], target_reached=False)
        elif action == "abort":
            if not self.trial_id:
                raise ValueError("当前没有活动试次")
            self._timed_finish(aborted=True, reason="操作员终止本轮")
        elif action in {"finish", "end"}:
            can_finish, reason = self._target_finish_permission()
            if not can_finish:
                raise ValueError(reason)
            state = self._timed["trial"]
            self.phase, self._guided["step"] = "recovery", "zeroing"
            self._timed["recovery_message"] = None
            boundary = self._transition("force_target_finish_requested", finish_requested=True,
                                       unloaded_confirmed=True, trigger="manual_unloaded_confirmation",
                                       automatic_unloaded_recovery_required=False,
                                       target_reached=state["target_reached"], target_force_norm_N=self.settings["target_force_norm_N"],
                                       message="已确认移开接触棒，直接建立下一轮软件零点与全谱参考；保存完成后可开始下一轮")
            state.update(finish_requested=True, finish_requested_ns=boundary, main_end_ns=boundary,
                         manual_recovery_confirmed=True, zero_requested_ns=boundary,
                         recovery_force=[], recovery_spectra=[])
        elif action == "confirm_unloaded":
            if not self.trial_id or self.phase != "recovery":
                raise ValueError("请先结束本轮并移开接触棒，再确认无载")
            self._timed["trial"]["manual_recovery_confirmed"] = True
            self._transition("recovery_unloaded_confirmation", unloaded_confirmed=True)
            self._timed_check_recovery()
        else:
            raise ValueError("目标合力试次只允许开始、结束本轮、无载确认或中止")
        return self.snapshot()

    def training_pause_acknowledged(self):
        if not self._force_target_protocol():
            return super().training_pause_acknowledged()
        with self.lock:
            if not self.running or not self.trial_id or self.phase != "waiting_training":
                return False
            boundary = max(time.perf_counter_ns(), self._timeline[-1][0])
            self._timed["trial"]["pre_start_ns"] = boundary
            self.phase, self._guided["step"] = "preload", "preload"
            self._transition("training_pause_acknowledged", at_ns=boundary, pause_reason=self._timed["pause_reason"],
                             acquisition_protocol=PROTOCOL, preload_duration_s=2,
                             message="已开始连续记录，请保持无接触 2 秒，提示开始按压后再加载")
            return True

    def _timed_before_source(self, frame):
        if not self._force_target_protocol():
            return super()._timed_before_source(frame)
        # The lead-in advances only from validated source callbacks below.
        # Neither frontend polling nor reaching the target can end a trial.

    def _target_preload_source(self, timestamp):
        state = self._timed.get("trial")
        if not state or state["pre_start_ns"] is None or timestamp is None:
            return
        state["elapsed_s"] = max(state["elapsed_s"], max(0, (timestamp-state["pre_start_ns"])/1e9))
        if self.phase != "preload":
            return
        spans = [(state[last]-state[first])/1e9 if state[first] is not None and state[last] is not None else 0
                 for first, last in (("preload_force_first_ns", "last_force_ns"),
                                     ("preload_spectrum_first_ns", "last_spectrum_ns"))]
        state["preload_elapsed_s"] = min(2, *spans)
        if state["faults"] or state["preload_elapsed_s"] < self._wrench_rules()["preload_duration_s"]:
            return
        # Both devices must actually have supplied the recorded lead-in. A lone
        # surviving stream or a GET request cannot announce that pressing is ready.
        latest = [state["last_force_ns"], state["last_spectrum_ns"]]
        if any(value is None or not 0 <= timestamp-value <= 120_000_000 for value in latest):
            return
        self.phase, self._guided["step"] = "waiting_contact", "waiting_contact"
        if self._no_contact_selected():
            self.phase, self._guided["step"] = "no_contact", "no_contact"
            boundary = self._transition("no_contact_recording_started", at_ns=self._guided_boundary(timestamp),
                                       sample_label="no_contact", operator_confirmed=True,
                                       message="正在采集无接触样本，请持续移开接触棒；记录足够后可手动结束本轮")
            state.update(press_prompt_ns=boundary, main_start_ns=boundary)
            self._trials[self.trial_id]["recording_started_monotonic_ns"] = boundary
            return
        state["press_prompt_ns"] = self._transition("force_target_press_prompt", at_ns=self._guided_boundary(timestamp),
                                                  trigger_source_stamp_ns=timestamp, preload_duration_s=2,
                                                  preload_contact_detected=state["preload_contact_detected"],
                                                  message="已记录 2 秒采前数据，现在可以在目标点缓慢按压")

    def _timed_spectrum(self, frame, annotation):
        if not self._force_target_protocol():
            return super()._timed_spectrum(frame, annotation)
        state = self._timed.get("trial")
        if not state or not self.trial_id or annotation.get("trial_id") != state["trial_id"]:
            return super()._timed_spectrum(frame, annotation)
        sample, timestamp, key = spectrum(frame), stamp(frame), identity(frame)
        if self.phase == "saving":
            # The operator's finish assertion replaces the recovery-range gate.
            # Keep source identity, epoch, axis and continuity protection while
            # the worker persists the frozen trial, without comparing to old S0.
            previous = state["last_spectrum_ns"]
            rules = self._wrench_rules()
            if (sample is None or frame.get("stream_epoch") != self.context["optical_epoch"]
                    or sample["axis_id"] != self.baseline["spectral_axis_id"]
                    or sample["identity"] in state["spectra_seen"]
                    or previous is not None and (sample["stamp"] <= previous
                                                 or sample["stamp"]-previous > rules["maximum_spectrum_gap_ms"]*1e6)):
                state["faults"].add("保存期间光谱源无效或不连续")
                self._target_invalidate_checkpoint("保存期间光谱源无效或不连续", timestamp)
                return
            state["spectra_seen"].add(sample["identity"])
            state["last_spectrum_ns"] = sample["stamp"]
            return  # Raw persisted, but saving frames do not extend frozen data.
        if key in state["spectra_seen"]:
            state["faults"].add("原始光谱帧身份重复")
            return
        if key:
            state["spectra_seen"].add(key)
        if annotation["phase"] == "waiting_training":
            return
        state["raw_total"] += 1
        if (sample is None or frame.get("stream_epoch") != self.context["optical_epoch"]
                or sample["axis_id"] != self.baseline["spectral_axis_id"]):
            state["faults"].add("原始光谱不是完整 512 点或轴/采样设置改变")
            return
        previous = state["last_spectrum_ns"]
        if previous is not None and (timestamp <= previous or timestamp-previous > self._wrench_rules()["maximum_spectrum_gap_ms"]*1e6):
            state["faults"].add("原始光谱时间乱序或间断")
        state["last_spectrum_ns"] = timestamp
        state["raw_valid"] += 1
        if annotation["phase"] == "preload" and state["preload_spectrum_first_ns"] is None:
            state["preload_spectrum_first_ns"] = timestamp
        if annotation["phase"] in {"loading", "release", "no_contact"}:
            state["main_spectra"].append(sample)
            state["main_spectra_seen"].add(key)
        if (annotation["phase"] == "recovery" and timestamp > state["zero_requested_ns"]
                and not state["recovery_spectra"]):
            state["recovery_spectra"].append(sample)
        if not state["faults"]:
            self._target_label_statistics(state)
        self._target_preload_source(timestamp)
        if self.phase == "recovery":
            self._timed_check_recovery()

    def _timed_force(self, frame, annotation, values):
        if not self._force_target_protocol():
            return super()._timed_force(frame, annotation, values)
        if self.phase == "saving":
            state, timestamp, key = self._timed["trial"], stamp(frame), identity(frame)
            previous = state["last_force_ns"]
            if (values is None or not trusted(frame) or frame.get("stream_epoch") != self.context["force_epoch"]
                    or key in state["force_seen"]
                    or previous is not None and (timestamp <= previous
                                                 or timestamp-previous > self._wrench_rules()["maximum_force_gap_ms"]*1e6)):
                state["faults"].add("保存期间六轴源无效或不连续")
                self._target_invalidate_checkpoint("保存期间六轴源无效或不连续", timestamp)
                return
            state["force_seen"].add(key)
            state["last_force_ns"] = timestamp
            corrected = [v-z for v, z in zip(values, self.baseline["force_zero"])]
            self._timed.update(current_wrench=corrected, force_norm_N=math.sqrt(sum(v*v for v in corrected[:3])),
                               last_force_stamp_ns=timestamp)
            norm = self._timed["force_norm_N"]
            state["peak_force_norm_N"] = max(state["peak_force_norm_N"], norm)
            if norm > self.settings["max_force_norm_N"]:
                state["faults"].add("观测三维合力超过设定上限，真实六轴读数已保留")
                self._target_invalidate_checkpoint("保存期间观测三维合力超过设定上限", timestamp)
            return
        super()._timed_force(frame, annotation, values)
        state, timestamp = self._timed.get("trial"), stamp(frame)
        if not state or not self.trial_id or annotation.get("trial_id") != state["trial_id"] or not trusted(frame) or values is None:
            return
        if not state["faults"]:
            self._target_label_statistics(state)
        norm = self._timed.get("force_norm_N")
        if state.get("no_contact") and annotation.get("phase") in {"preload", "no_contact"}:
            corrected = self._timed.get("current_wrench") or [0.] * 6
            state["peak_abs_wrench"] = [max(old, abs(value)) for old, value in zip(state["peak_abs_wrench"], corrected)]
            state["no_contact_peak_force_norm_N"] = max(state["no_contact_peak_force_norm_N"], norm or 0.)
            limit, axes = no_contact_limits(self._wrench_rules(), self.baseline)
            if norm is not None and (norm > limit or any(abs(v) > axis for v, axis in zip(corrected, axes))):
                state["faults"].add("无接触采集期间六轴读数超出无载范围，不能标为无接触样本")
                if annotation.get("phase") == "preload":
                    state["preload_contact_detected"] = True
        if annotation.get("phase") == "preload" and state["preload_force_first_ns"] is None:
            state["preload_force_first_ns"] = timestamp
        if not state.get("no_contact") and annotation.get("phase") == "preload" and norm is not None and norm > self._wrench_rules()["contact_force_norm_N"]:
            state["preload_contact_detected"] = True
            state["faults"].add("2 秒采前基线期间已接触加载，不能作为无载基线")
        self._target_preload_source(timestamp)
        if not state.get("no_contact") and self.phase == "waiting_contact" and norm is not None and norm > self._wrench_rules()["contact_force_norm_N"]:
            state.update(contact_detected=True, main_start_ns=timestamp)
            self.phase, self._guided["step"] = "loading", "loading"
            self._transition("force_target_contact_detected", at_ns=self._guided_boundary(timestamp),
                             trigger_source_stamp_ns=timestamp, contact_force_norm_N=norm)
        if self.phase in {"loading", "release"} and norm is not None and norm >= self.settings["target_force_norm_N"] and not state["target_reached"]:
            state.update(target_reached=True, target_reached_ns=timestamp)
            self._trials[self.trial_id]["target_reached"] = True
            self.phase, self._guided["step"] = "release", "release"
            self._transition("force_target_reached", at_ns=self._guided_boundary(timestamp), trigger_source_stamp_ns=timestamp,
                             force_norm_N=norm, target_force_norm_N=self.settings["target_force_norm_N"],
                             message="已达到目标合力，请卸载；记录继续，松开后点击结束本轮")
        if self.phase == "recovery":
            # Parent records actual force frames; the first post-assertion frame
            # is the zero sample. No stability or old-zero proximity is tested.
            state["recovery_force"] = [row for row in state["recovery_force"] if row["stamp"] > state["zero_requested_ns"]][:1]
            self._timed_check_recovery()

    def _timed_check_recovery(self):
        if not self._force_target_protocol():
            return super()._timed_check_recovery()
        state = self._timed.get("trial")
        if not state or self.phase != "recovery" or not state.get("finish_requested"):
            return
        force_rows, spectral_rows = state["recovery_force"], state["recovery_spectra"]
        if not force_rows or not spectral_rows:
            return
        if state.get("no_contact"):
            limit, axes = no_contact_limits(self._wrench_rules(), self.baseline)
            corrected = [value-zero for value, zero in zip(force_rows[0]["values"], self.baseline["force_zero"])]
            if math.sqrt(sum(v*v for v in corrected[:3])) > limit or any(abs(v) > axis for v, axis in zip(corrected, axes)):
                self._timed_finish(aborted=True, reason="无接触结束声明后的六轴源帧超出无载范围，未应用新参考")
                return
        now = time.perf_counter_ns()
        if any(not 0 <= now-row["stamp"] <= self._wrench_rules()[gap]*1e6
               for row, gap in ((force_rows[0], "maximum_force_gap_ms"), (spectral_rows[0], "maximum_spectrum_gap_ms"))):
            self._timed_finish(aborted=True, reason="直接回零取样源帧已过期或时间异常，未应用新参考")
            return
        state["unloaded_evidence"] = {
            "schema": "manual_finish_unloaded_assertion_v1", "trigger": "manual_unloaded_confirmation",
            "operator_confirmed": True, "automatic_unloaded_recovery_verified": False,
            "finish_requested_monotonic_ns": state["finish_requested_ns"],
            "zero_sampling_monotonic_ns": state["zero_requested_ns"],
            "previous_baseline_id": self.baseline["baseline_id"],
            "selection": "first_trusted_source_frame_strictly_after_finish_boundary",
            "force_frame_ids": [row["identity"][1] for row in force_rows],
            "spectrum_frame_ids": [row["identity"][1] for row in spectral_rows],
            "force_source_stamps_ns": [row["stamp"] for row in force_rows],
            "spectrum_source_stamps_ns": [row["stamp"] for row in spectral_rows],
            "thresholds_hardware_qualified": False,
        }
        self._timed_finish(recovery_force=force_rows, recovery_spectra=spectral_rows)

    def _target_label_statistics(self, state):
        # Source lists append only under the experiment lock. Once a spectrum
        # has its right force endpoint, later ordered force frames cannot change
        # its match. Keep only the not-yet-bracketed tail pending. This avoids
        # rescoring an entire long trial in UI reads or device receive callbacks.
        rows, spectra = state["force_samples"], state["main_spectra"]
        signature = (self.session_id, state["trial_id"], id(rows), id(spectra),
                     tuple(self.baseline["force_zero"]), self._wrench_rules()["contact_force_norm_N"])
        revision = (len(rows), len(spectra))
        cached = getattr(self, "_target_statistics_cache", None)
        if (cached is None or cached["signature"] != signature
                or len(rows) < cached["force_count"] or len(spectra) < cached["spectra_count"]):
            cached = {"signature": signature, "revision": None, "force_count": 0, "spectra_count": 0,
                      "times": [], "ordered": True, "pending": [], "valid": 0, "contact": 0,
                      "rejected": 0, "summary": (0, 0, {})}
            self._target_statistics_cache = cached
        if cached["revision"] == revision:
            valid, contact, rejected = cached["summary"]
            return valid, contact, dict(rejected)
        previous = cached["times"][-1] if cached["times"] else None
        for row in rows[cached["force_count"]:]:
            if previous is not None and row["stamp"] < previous:
                cached["ordered"] = False
            previous = row["stamp"]
            cached["times"].append(previous)
        if cached["ordered"]:
            candidates = cached["pending"] + spectra[cached["spectra_count"]:]
            pending = []
        else:
            # Out-of-order force frames already fault the trial, but final
            # evidence must retain the historical stable-sort matching result.
            rows = sorted(rows, key=lambda row: row["stamp"])
            cached["times"] = [row["stamp"] for row in rows]
            cached.update(valid=0, contact=0, rejected=0)
            candidates, pending = spectra, []
        times = cached["times"]
        for sample in candidates:
            timestamp = sample["stamp"]
            if cached["ordered"] and (not times or timestamp > times[-1]):
                pending.append(sample)
                continue
            values = _label_values(rows, times, timestamp)
            if values is None:
                cached["rejected"] += 1
                continue
            cached["valid"] += 1
            corrected = [v-z for v, z in zip(values, self.baseline["force_zero"])]
            cached["contact"] += math.sqrt(sum(v*v for v in corrected[:3])) > signature[-1]
        cached.update(force_count=revision[0], spectra_count=revision[1], pending=pending, revision=revision)
        rejected = cached["rejected"] + len(pending)
        cached["summary"] = (cached["valid"], cached["contact"],
                             {"missing_or_distant_bracket": rejected} if rejected else {})
        valid, contact, rejected = cached["summary"]
        return valid, contact, dict(rejected)

    def _target_finish_permission(self, now=None):
        """Read the same source-evidence gate for UI snapshots and finish API.

        Permission starts direct zero sampling. It is not a final quality verdict, and
        the force may already have returned below target when the user clicks.
        """
        state = self._timed.get("trial")
        if not self.running or not self.trial_id or not state or state["trial_id"] != self.trial_id:
            return False, "当前没有可结束的活动试次"
        if self.phase == "waiting_training":
            return False, "请等待采集准备完成，训练进程尚未确认保护暂停"
        trial = self._trials[self.trial_id]
        if state["faults"]:
            return False, "本轮已有质量故障，请中止本轮：" + "；".join(sorted(state["faults"]))
        if trial.get("manual_mark") or trial.get("bad"):
            return False, "本轮已标记无效，请中止本轮并保留原始记录"
        if self.phase == "preload":
            return False, "正在记录 2 秒采前基线，请保持无接触，提示开始按压后再加载"
        if self.phase not in {"waiting_contact", "loading", "release", "no_contact"}:
            return False, "当前阶段不能结束本轮，请等待软件回零和保存完成"
        no_contact = state.get("no_contact") is True
        if not no_contact and not state.get("target_reached"):
            target = self.settings["target_force_norm_N"]
            return False, f"本轮尚未达到目标合力 {target:g} N，请继续记录；需要退出可中止本轮"
        if not no_contact and self.phase != "release":
            return False, "当前尚未进入达到目标后的卸载提示阶段"
        now = time.perf_counter_ns() if now is None else now
        rules = self._wrench_rules()
        for key, gap, label in (("last_spectrum_ns", "maximum_spectrum_gap_ms", "光谱"),
                                ("last_force_ns", "maximum_force_gap_ms", "六轴")):
            timestamp = state[key]
            if type(timestamp) is not int or not 0 <= now - timestamp <= rules[gap] * 1e6:
                return False, label + "采集源尚未就绪或已过期，请等待新的有效源帧"
        valid, contact, _ = self._target_label_statistics(state)
        minimum = rules["minimum_no_contact_spectra"] if no_contact else rules["minimum_contact_spectra"]
        if no_contact:
            duration = (state["last_spectrum_ns"] - state["main_start_ns"])/1e9
            if valid < minimum or duration < rules["minimum_recording_s"]:
                return False, f"请继续记录无接触样本，至少 {minimum} 帧且不少于 {rules['minimum_recording_s']:g} 秒"
        elif contact < minimum:
            return False, f"可信接触光谱不足 {minimum} 帧（当前 {contact} 帧），可继续在目标点缓慢按压，或中止重采"
        if state["raw_valid"] / max(1, state["raw_total"]) < rules["minimum_raw_valid_ratio"]:
            return False, "完整原始光谱比例尚未达到采集要求，请继续记录或中止本轮"
        if valid / max(1, len(state["main_spectra"])) < rules["minimum_label_ratio"]:
            return False, "可信六轴标签覆盖尚未达到采集要求，请继续记录或中止本轮"
        return True, None

    def _target_quality(self, state, aborted=False):
        if state.get("no_contact"):
            return self._no_contact_quality(state, aborted=aborted)
        valid, contact, rejected = self._target_label_statistics(state)
        count = len(state["main_spectra"])
        ratio = state["raw_valid"]/max(1, state["raw_total"])
        label_ratio = valid/max(1, count)
        reasons = sorted(state["faults"])
        if not state["target_reached"]:
            reasons.append("本轮未达到设定目标合力；记录已保留")
        if not state["finish_requested"]:
            reasons.append("未明确结束本轮")
        if contact < self._wrench_rules()["minimum_contact_spectra"]:
            reasons.append("可信接触原始光谱少于 16 帧")
        if ratio < self._wrench_rules()["minimum_raw_valid_ratio"]:
            reasons.append("完整原始光谱比例低于 95%")
        if label_ratio < self._wrench_rules()["minimum_label_ratio"]:
            reasons.append("可信六轴标签覆盖比例低于 85%")
        if not state["unloaded_evidence"]:
            reasons.append("未取得结束本轮的无载声明及直接回零源帧")
        if state["preload_elapsed_s"] < self._wrench_rules()["preload_duration_s"]:
            reasons.append("两路真实源帧的采前基线记录不足 2 秒")
        if self._trials[state["trial_id"]].get("manual_mark"):
            reasons.append("本轮有人工异常标记")
        metrics = {"target_force_norm_N": self.settings["target_force_norm_N"], "target_reached": state["target_reached"],
                   "target_reached_monotonic_ns": state["target_reached_ns"], "contact_detected": state["contact_detected"],
                   "finish_requested": state["finish_requested"], "recording_duration_s": state["elapsed_s"],
                   "finish_requested_monotonic_ns": state["finish_requested_ns"],
                   "preload_duration_s": self._wrench_rules()["preload_duration_s"],
                   "preload_recorded_duration_s": state["preload_elapsed_s"],
                   "preload_contact_detected": state["preload_contact_detected"],
                   "press_prompt_monotonic_ns": state["press_prompt_ns"],
                   "manual_finish_unloaded_confirmed": state["finish_requested"] and state["manual_recovery_confirmed"],
                   "automatic_unloaded_recovery_required": False,
                   "contact_raw_spectra": contact, "main_raw_spectra": count, "raw_spectra": state["raw_total"],
                   "raw_valid_ratio": ratio, "valid_label_count": valid, "valid_label_ratio": label_ratio,
                   "label_rejections": rejected, "peak_force_norm_N": state["peak_force_norm_N"],
                   "max_force_norm_N": self.settings["max_force_norm_N"], "force_frames": state["force_total"],
                   "force_valid_frames": state["force_valid"], "peak_processed_frames": state["peak_processed_frames"],
                   "peak_valid_frames": state["peak_valid_frames"], "unloaded_evidence": deepcopy(state["unloaded_evidence"]),
                   "sync_rule": deepcopy(SYNC_RULE), "physical_accuracy_qualified": False}
        return {"accepted": not aborted and not reasons, "reasons": reasons, "metrics": metrics,
                "criteria_version": QUALITY_VERSION, "acquisition_protocol": PROTOCOL}

    def _no_contact_quality(self, state, aborted=False):
        valid, _, rejected = self._target_label_statistics(state)
        rules = self._wrench_rules()
        limit, axes = no_contact_limits(rules, self.baseline)
        count = len(state["main_spectra"])
        duration = max(0, (state["main_spectra"][-1]["stamp"]-state["main_start_ns"])/1e9) if count else 0
        reasons = sorted(state["faults"])
        ratio = state["raw_valid"]/max(1, state["raw_total"])
        label_ratio = valid/max(1, count)
        if not state["finish_requested"] or not state["unloaded_evidence"]:
            reasons.append("未取得手动结束的无接触声明及直接回零源帧")
        if state["preload_elapsed_s"] < rules["preload_duration_s"]:
            reasons.append("两路真实源帧的采前基线记录不足 2 秒")
        if valid < rules["minimum_no_contact_spectra"] or duration < rules["minimum_recording_s"]:
            reasons.append("无接触有效光谱帧数或记录时长不足")
        if ratio < rules["minimum_raw_valid_ratio"] or label_ratio < rules["minimum_label_ratio"]:
            reasons.append("无接触原始光谱或可信六轴标签覆盖不足")
        if self._trials[state["trial_id"]].get("manual_mark"):
            reasons.append("本轮有人工异常标记")
        metrics = {"sample_label": "no_contact", "sample_label_version": CONTACT_LABEL_VERSION,
                   "operator_confirmed": True, "recording_started_monotonic_ns": state["main_start_ns"],
                   "finish_requested": state["finish_requested"], "finish_requested_monotonic_ns": state["finish_requested_ns"],
                   "recording_duration_s": state["elapsed_s"], "no_contact_recorded_duration_s": duration,
                   "no_contact_raw_spectra": valid, "main_raw_spectra": count, "raw_spectra": state["raw_total"],
                   "raw_valid_ratio": ratio, "valid_label_count": valid, "valid_label_ratio": label_ratio,
                   "label_rejections": rejected, "preload_duration_s": 2,
                   "preload_recorded_duration_s": state["preload_elapsed_s"],
                   "preload_contact_detected": state["preload_contact_detected"],
                   "press_prompt_monotonic_ns": state["press_prompt_ns"],
                   "manual_finish_unloaded_confirmed": state["finish_requested"] and state["manual_recovery_confirmed"],
                   "automatic_unloaded_recovery_required": False, "unloaded_force_norm_limit_N": limit,
                   "unloaded_axis_limits": axes, "peak_abs_wrench": list(state["peak_abs_wrench"]),
                   "peak_force_norm_N": state["no_contact_peak_force_norm_N"], "force_frames": state["force_total"],
                   "force_valid_frames": state["force_valid"], "unloaded_evidence": deepcopy(state["unloaded_evidence"]),
                   "sync_rule": deepcopy(SYNC_RULE), "physical_accuracy_qualified": False}
        return {"accepted": not aborted and not reasons, "reasons": reasons, "metrics": metrics,
                "criteria_version": NO_CONTACT_QUALITY_VERSION, "acquisition_protocol": PROTOCOL}

    def _target_prepare_baseline(self, state, force_rows, spectral_rows):
        self._baseline_number += 1
        baseline_id = f"B{self._baseline_number:04d}"
        offsets = [statistics.median(row["values"][axis] for row in force_rows) for axis in range(6)]
        noise = [1.4826*statistics.median(abs(row["values"][axis]-offsets[axis]) for row in force_rows) for axis in range(6)]
        effective = max(time.perf_counter_ns(), force_rows[-1]["stamp"], spectral_rows[-1]["stamp"])
        baseline = {"baseline_id": baseline_id, "point_id": self.point_id, "status": "prepared", "ready": False,
                    "used": False, "unloaded_confirmed": True, "trigger": state["unloaded_evidence"]["trigger"],
                    "unloaded_evidence": deepcopy(state["unloaded_evidence"]), "quality_version": QUALITY_VERSION,
                    "zero_semantics": self._wrench_rules()["finish_zero_semantics"],
                    "automatic_unloaded_recovery_verified": False,
                    "noise_estimation": "single_source_frame_no_variation_estimate",
                    "reference_kind": "spectrum_unloaded_median_v1", "force_zero": offsets,
                    "force_noise_axes": noise, "force_noise_N": noise[2], "lambda0_nm": None,
                    "force_frame_ids": [row["identity"][1] for row in force_rows],
                    "force_source_stamps_ns": [row["stamp"] for row in force_rows],
                    "prepared_from_baseline_id": self.baseline["baseline_id"],
                    "previous_reference_version": self.baseline.get("reference_version"),
                    "reference_version": self.baseline.get("reference_version"),
                    "requested_monotonic_ns": spectral_rows[0]["stamp"], "prepared_monotonic_ns": effective,
                    "effective_monotonic_ns": effective, **self.context}
        self._timed_assign_reference(baseline, self._timed_reference(spectral_rows, baseline_id, effective))
        return baseline

    def _target_invalidate_checkpoint(self, reason, timestamp=None):
        state = self._timed.get("trial")
        if not state or self.phase != "saving":
            return
        token = state["checkpoint_token"]
        state.update(data_end_ns=None, frozen_quality=None, next_baseline=None, checkpoint_token=None,
                     unloaded_evidence=None, recovery_force=[], recovery_spectra=[])
        trial = self._trials[state["trial_id"]]
        trial.update(status="recording", bad=bool(state["faults"]))
        for key in ("quality", "checkpoint_token", "data_end_monotonic_ns"):
            trial.pop(key, None)
        self.phase, self._guided["step"] = "recovery", "zeroing"
        self._timed["recovery_message"] = reason+"；请保持接触棒移开，重新取得直接回零源帧并保存"
        state["zero_requested_ns"] = self._transition(
            "force_target_checkpoint_invalidated", at_ns=self._guided_boundary(timestamp or time.perf_counter_ns()),
            invalidated_checkpoint_token=token, reason=reason)

    def _timed_finish(self, *, aborted=False, reason=None, recovery_force=None, recovery_spectra=None):
        if not self._force_target_protocol():
            return super()._timed_finish(aborted=aborted, reason=reason, recovery_force=recovery_force, recovery_spectra=recovery_spectra)
        state = self._timed.get("trial")
        if not state or not self.trial_id:
            return
        if reason:
            state["faults"].add(reason)
        if aborted:
            return self._target_commit(state, self._target_quality(state, aborted=True), aborted=True)
        if self.phase == "saving":
            return
        state["data_end_ns"] = max(recovery_force[-1]["stamp"], recovery_spectra[-1]["stamp"])
        state["frozen_quality"] = self._target_quality(state)
        state["next_baseline"] = self._target_prepare_baseline(state, recovery_force, recovery_spectra)
        state["checkpoint_sequence"] += 1
        state["checkpoint_token"] = self.session_id+":"+state["trial_id"]+":checkpoint:"+str(state["checkpoint_sequence"])
        state["saving_started_ns"] = time.perf_counter_ns()
        trial = self._trials[state["trial_id"]]
        trial.update(status="saving", bad=not state["frozen_quality"]["accepted"], quality=deepcopy(state["frozen_quality"]),
                     finish_requested_monotonic_ns=state["finish_requested_ns"],
                     zero_sampling_monotonic_ns=state["zero_requested_ns"],
                     target_reached=state["target_reached"], target_reached_monotonic_ns=state["target_reached_ns"],
                     main_start_monotonic_ns=state["main_start_ns"], main_end_monotonic_ns=state["main_end_ns"],
                     data_end_monotonic_ns=state["data_end_ns"], checkpoint_token=state["checkpoint_token"])
        self.phase, self._guided["step"] = "saving", "saving"
        self._timed["recovery_message"] = None
        self._transition("force_target_checkpoint_requested", trial=deepcopy(trial), quality=deepcopy(state["frozen_quality"]),
                         next_baseline_candidate=deepcopy(state["next_baseline"]), checkpoint_token=state["checkpoint_token"])
        self.writer.request_checkpoint(state["checkpoint_token"])

    def recording_checkpoint_acknowledged(self):
        """Service coordinator hook; snapshots and source readers never wait/fsync."""
        with self.lock:
            state = self._timed.get("trial")
            if not self.running or not self._force_target_protocol() or self.phase != "saving" or not state:
                return False
            if self.writer.error:
                self._fail(RuntimeError(self.writer.error))
                return False
            now = time.perf_counter_ns()
            if any(state[key] is None or not 0 <= now-state[key] <= self._wrench_rules()[gap]*1e6
                           for key, gap in (("last_force_ns", "maximum_force_gap_ms"),
                                            ("last_spectrum_ns", "maximum_spectrum_gap_ms"))):
                state["faults"].add("保存确认时直接回零源帧不再新鲜")
                self._target_invalidate_checkpoint("保存确认时直接回零源帧不再新鲜")
                return False
            if not self.writer.checkpoint_ready(state["checkpoint_token"]):
                return False
            self._target_commit(state, deepcopy(state["frozen_quality"]), next_baseline=state["next_baseline"])
            return True

    def _target_commit(self, state, quality, *, aborted=False, next_baseline=None):
        trial = self._trials[state["trial_id"]]
        trial.update(status="aborted" if aborted else "completed", bad=not quality["accepted"], quality=deepcopy(quality),
                     target_reached=state["target_reached"], main_start_monotonic_ns=state["main_start_ns"],
                     finish_requested_monotonic_ns=state["finish_requested_ns"],
                     zero_sampling_monotonic_ns=state["zero_requested_ns"],
                     main_end_monotonic_ns=state["main_end_ns"], data_end_monotonic_ns=state.get("data_end_ns"))
        if not aborted:
            trial.update(checkpoint_saved=True, checkpoint_token=state["checkpoint_token"], saved_monotonic_ns=time.perf_counter_ns())
        self.counts["trials_aborted" if aborted else "trials_completed"] += 1
        if quality["accepted"]:
            self._guided["accepted_by_point"][state["point_id"]] += 1
        self._guided["last_quality"] = self._timed["last_quality"] = deepcopy(quality)
        self._transition("force_target_round_quality", completed_trial_id=state["trial_id"], quality=deepcopy(quality),
                         checkpoint_saved=not aborted)
        self.trial_id, self.phase = None, "idle"
        self._timed["trial"], self._timed["pause_reason"], self._timed["recovery_message"] = None, None, None
        self.baseline = {"status": "required", "ready": False}
        self._guided["step"] = "round_complete" if quality["accepted"] else "retry"
        if not state.get("no_contact") and self._guided["accepted_by_point"][state["point_id"]] >= self.settings["repetitions_per_point"]:
            self._guided["step"] = "point_complete"
        if not state.get("no_contact") and all(self._guided["accepted_by_point"][point["id"]] >= self.settings["repetitions_per_point"] for point in self.settings["point_grid"]):
            self._guided["step"] = "session_complete"
        if next_baseline is not None and not aborted:
            effective = time.perf_counter_ns()
            self.baseline = deepcopy(next_baseline)
            self.baseline.update(status="ready", ready=True, effective_monotonic_ns=effective, checkpoint_token=state["checkpoint_token"])
            self.baseline["spectral_reference"]["effective_ns"] = effective
            self._baselines[self.baseline["baseline_id"]] = self.baseline
            self._transition("baseline_ready", baseline=deepcopy(self.baseline), acquisition_protocol=PROTOCOL)
        self._transition("next_round_prepared" if self.baseline.get("ready") else "next_baseline_required")

    def _timed_watchdog(self, now):
        if not self._force_target_protocol():
            return super()._timed_watchdog(now)
        state = self._timed.get("trial")
        if not self.running or not self.trial_id or not state:
            return False
        reason = None
        if self.phase == "waiting_training" and now-state["started_ns"] > 20_000_000_000:
            reason = "训练暂停确认超时，本轮未宣布可按压"
        elif self.phase == "saving" and now-state["saving_started_ns"] > 30_000_000_000:
            reason = "本轮数据保存确认超过 30 秒；未应用新的零偏与全谱参考"
        elif self.phase == "recovery" and now-state["main_end_ns"] > 30_000_000_000:
            reason = "直接回零源帧等待超过 30 秒，未应用新参考；本轮记录已保留"
        elif self.phase in {"preload", "waiting_contact", "loading", "release", "no_contact", "recovery"}:
            last = min(state["last_force_ns"] or state["pre_start_ns"], state["last_spectrum_ns"] or state["pre_start_ns"])
            if now-last > 2_000_000_000:
                reason = "连续记录期间源帧断流超过 2 秒"
        if reason:
            self._timed_finish(aborted=True, reason=reason)
            return True
        return False

    def _timed_snapshot(self):
        if not self._force_target_protocol():
            return super()._timed_snapshot()
        value = super()._timed_snapshot()
        state = self._timed.get("trial")
        target = self.settings["target_force_norm_N"]
        messages = {"preload": "正在记录 2 秒采前基线，请保持无接触，提示开始按压后再加载",
                    "waiting_contact": "采前基线已记录，现在可以开始按压；保持所选接触位置并缓慢加载",
                    "loading": f"请缓慢加载，观测三维合力达到 {target:g} N 后按提示卸载",
                    "release": "已达到目标，请卸载；记录仍在继续，松开后点击结束本轮",
                    "zeroing": "正在直接建立下一轮软件零点与全谱参考，请保持接触棒移开",
                    "saving": "正在保存本轮固定数据与下一轮软件零点，请保持无接触；保存确认后准备下一轮",
                    "point_complete": "当前点位规定轮次已保存，请选择下一点位",
                    "round_complete": "本轮已保存，下一轮参考已准备，可以继续采集"}
        if self.baseline.get("ready"):
            messages["retry"] = "本轮失败记录保留，下一轮参考已准备，可重新采集"
        value.update(protocol=PROTOCOL, criteria_version=QUALITY_VERSION, target_force_norm_N=target,
                     target_reached=state.get("target_reached", False) if state else (self._timed.get("last_quality") or {}).get("metrics", {}).get("target_reached", False),
                     finish_requested=state.get("finish_requested", False) if state else False,
                     checkpoint_pending=self.phase == "saving", checkpoint_token=state.get("checkpoint_token") if state else None,
                     preload_duration_s=2,
                     preload_remaining_s=max(0, 2-state.get("preload_elapsed_s", 0)) if state else 0,
                     press_prompt_ready=bool(state and state.get("press_prompt_ns") is not None and not state["faults"]),
                     manual_finish_zeroing=self.phase == "recovery" and bool(state and state.get("finish_requested")),
                     duration_s=None, force_progress_percent=100 if state and state.get("target_reached") else min(100, max(0, (value.get("force_norm_N") or 0)/target*100)),
                     quality_faults=sorted(state["faults"]) if state else [])
        value["can_finish"], value["finish_block_reason"] = self._target_finish_permission()
        if self._no_contact_selected():
            value.update(sample_label="no_contact", criteria_version=NO_CONTACT_QUALITY_VERSION,
                         target_force_norm_N=None, force_progress_percent=0, press_prompt_ready=False,
                         current_round=value["point_attempts"]+(0 if state else 1), point_progress_percent=None)
            messages.update(preload="正在记录 2 秒无接触采前基线，请持续移开接触棒",
                            no_contact="正在采集无接触样本，可自行决定记录时长，数据足够后点击结束本轮",
                            round_complete="本轮无接触样本已保存，可选择其他点位或继续采集无接触")
        total = sum(value["accepted_by_point"].get(point["id"], 0) for point in self.settings["point_grid"])
        value.update(total_accepted=total, progress_percent=total/value["total_required"]*100,
                     complete=all(value["accepted_by_point"].get(point["id"], 0) >= self.settings["repetitions_per_point"] for point in self.settings["point_grid"]),
                     no_contact_accepted=value["accepted_by_point"].get(NO_CONTACT_POINT_ID, 0),
                     no_contact_attempts=value["attempts_by_point"].get(NO_CONTACT_POINT_ID, 0))
        if not self._timed.get("recovery_message") and value["step"] in messages:
            value["message"] = messages[value["step"]]
        if self.phase == "release" and state and not state["finish_requested"]:
            value["message"] = ("本轮采集数据检查已通过，请移开接触棒后点击“结束本轮”；保存完成后才计入合格轮次"
                                if value["can_finish"] else "已达到目标，当前不能结束本轮：" + value["finish_block_reason"])
        return value
