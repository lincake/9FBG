"""Read-only replay of archived raw frames using the recorded fixed identities.

This adapter is deliberately tied to the current PeakTracker state schema. It
does not initialize peaks, estimate a new baseline, write files, or use online
display results as measurements. The caller verifies the archive file hashes.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from .experiment import AXES, IDS, UNITS, _force_values
from .capture_setup import setup_allows_trial
from .storage import clean
from .sync import ForceSynchronizer
from .tracking import ALGORITHM_VERSION, DEFAULT_CONFIG, PeakTracker, digest

FEATURE_PROCESSING = "frozen_identity_raw_replay_v1"
RESTORE_SCHEMA = "peaktracker_fixed_identity_state_v1"
_TIME_QUALITY = {"host_rx_approx", "hardware_mapped"}
_PHASES = {"unloaded", "preload", "loading", "hold", "removed", "recovery"}
_BINDING_KEYS = (
    "raw_available", "smoothing", "smoothing_enabled", "pipeline_id", "algorithm",
    "correction_algorithm", "calibration_sha256", "factory_calibration_sha256",
    "axis_source", "effective_integration_us", "exposure_us", "sensor_mode",
    "integration_us_requested", "sensor_mode_requested", "average_number", "state",
    "background_applied", "baseline_correction_applied", "factory_intensity_correction_applied",
)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _vector(value, count):
    return isinstance(value, (list, tuple)) and len(value) == count and all(_finite(x) for x in value)


def _stamp(frame):
    value = frame.get("host_rx_monotonic_ns")
    return value if type(value) is int and value > 0 else None


def _trusted_time(frame):
    value = frame.get("time_quality")
    return isinstance(value, str) and value in _TIME_QUALITY


def _identity(frame):
    value = frame.get("frame_id")
    return type(value) in (int, str) and value != "" and isinstance(frame.get("stream_epoch"), str) and bool(frame["stream_epoch"])


def _annotation(frame):
    source = frame.get("experiment")
    source = source if isinstance(source, dict) else frame
    return {key: deepcopy(source.get(key)) for key in (
        "session_id", "trial_id", "point_id", "baseline_id", "phase", "eligible", "manual_mark")}


def _iter_rows(path, name):
    file = path / name
    if not file.is_file():
        raise ValueError("原始实验文件不存在：" + name)
    with file.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"原始实验文件第 {number} 行无效：{name}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"原始实验文件第 {number} 行必须为对象：{name}")
            yield row


class _FrozenTracker:
    """Compatibility adapter for exactly ALGORITHM_VERSION's private state.

    Every restored window/anchor/seed is recorded acquisition evidence. Leaving
    binding unset lets the first matching raw frame establish its original
    transport binding. Later axis/pipeline changes are rejected before process
    can clear the fixed identities. Reference updates only copy saved baselines.
    """
    def __init__(self, manifest):
        audit, context = manifest.get("acquisition_audit"), manifest.get("acquisition_context")
        if not isinstance(audit, dict) or not isinstance(context, dict):
            raise ValueError("实验缺少固定身份恢复所需 acquisition_audit/acquisition_context")
        state, config = audit.get("tracker"), audit.get("tracker_config")
        if not isinstance(state, dict) or not isinstance(config, dict) or set(config) != set(DEFAULT_CONFIG):
            raise ValueError("实验缺少保存的完整九峰状态或完整 tracker_config；拒绝使用默认窗口或配置")
        if state.get("algorithm_version") != ALGORITHM_VERSION or context.get("algorithm_version") != ALGORITHM_VERSION:
            raise ValueError("实验提峰算法版本与固定身份恢复适配器不一致")
        tracker = PeakTracker(config)
        if tracker.config != config or tracker.config_version != state.get("config_version") or tracker.config_version != context.get("config_version"):
            raise ValueError("实验提峰配置哈希与保存的完整配置不一致")
        windows, peaks, reference = state.get("windows"), state.get("peaks"), state.get("reference_nm")
        if peaks is None and isinstance(state.get("latest"), dict):
            peaks = state["latest"].get("peaks")
        if isinstance(reference, dict) and set(reference) == set(IDS):
            reference = [reference[key] for key in IDS]
        if not isinstance(windows, list) or len(windows) != 9 or not isinstance(peaks, list) or len(peaks) != 9 or not _vector(reference, 9):
            raise ValueError("实验缺少固定九窗口、参考波长或完整逐峰拟合参数")
        # Ordering is evidence: never sort centers or reassign FBG identities.
        if any(not isinstance(row, dict) or row.get("id") != key for row, key in zip(windows, IDS)) or any(not isinstance(row, dict) or row.get("id") != key for row, key in zip(peaks, IDS)):
            raise ValueError("保存的窗口与逐峰身份必须按固定 FBG1～FBG9 排列")
        anchors = [window.get("identity_anchor_nm") for window in windows]
        if not _vector(anchors, 9) or any(a >= b for a, b in zip(anchors, anchors[1:])):
            raise ValueError("保存的固定身份锚点缺失或顺序无效")
        if state.get("identity_anchor_nm") is not None and state["identity_anchor_nm"] != anchors:
            raise ValueError("保存的身份锚点与窗口证据不一致")
        for index, (window, peak) in enumerate(zip(windows, peaks)):
            low, high = window.get("low_nm"), window.get("high_nm")
            if not _finite(low) or not _finite(high) or not low < anchors[index] < high or index and low < windows[index - 1]["high_nm"]:
                raise ValueError("保存的九峰窗口边界无效或相互重叠")
            if peak.get("valid") is not True or peak.get("stale") is True or not all(_finite(peak.get(key)) for key in ("center_nm", "sigma_nm", "amplitude", "background")) or peak["sigma_nm"] <= 0 or peak["amplitude"] <= 0 or not low <= peak["center_nm"] <= high:
                raise ValueError("保存的九峰拟合种子不完整、新鲜有效或不属于原窗口")
        version = state.get("reference_version")
        if type(version) is not int or version <= 0 or version != context.get("reference_version"):
            raise ValueError("保存的参考版本与采集上下文不一致")
        latest = state.get("latest") or state
        axis_hash = (latest.get("axis_diagnostics") or {}).get("axis_sha256")
        preprocessing = audit.get("optical_preprocessing")
        if preprocessing is None:
            preprocessing = (latest.get("raw_spectrum") or {}).get("preprocessing")
        if not isinstance(axis_hash, str) or len(axis_hash) != 64 or any(c not in "0123456789abcdef" for c in axis_hash) or not isinstance(preprocessing, dict):
            raise ValueError("实验缺少实际波长轴哈希或原始预处理链，不能安全恢复固定身份")
        optical_epoch, force_epoch = context.get("optical_epoch"), context.get("force_epoch")
        if not isinstance(optical_epoch, str) or not optical_epoch or not isinstance(force_epoch, str) or not force_epoch:
            raise ValueError("实验缺少光谱或 PX6D 采集时段")
        self.binding = (optical_epoch, axis_hash, digest({key: preprocessing.get(key) for key in _BINDING_KEYS}))
        tracker._anchors = deepcopy(anchors)
        tracker._windows = deepcopy(windows)
        tracker._reference = list(reference)
        tracker._reference_version = version
        tracker._last_valid = {index: {"row": deepcopy(peak), "timestamp": _stamp(latest) or 0,
                                      "frame_id": deepcopy(latest.get("frame_id"))} for index, peak in enumerate(peaks)}
        tracker._binding = None
        tracker._pending_reference = False
        self.tracker, self.windows, self.anchors = tracker, deepcopy(windows), list(anchors)
        self.reference, self.version = list(reference), version
        self.seen = set()

    def process(self, frame, baseline):
        tracker = self.tracker
        reason = None
        try:
            if not _identity(frame) or _stamp(frame) is None:
                raise ValueError("原始光谱缺少源帧身份或单调接收时间")
            if not _vector(frame.get("wavelength_nm"), 512):
                raise ValueError("原始实际波长轴必须为 512 个有限数值")
            metadata = frame.get("preprocessing")
            if not isinstance(metadata, dict):
                raise ValueError("原始光谱缺少预处理链")
            binding = (frame.get("stream_epoch"), digest(frame["wavelength_nm"]), digest({key: metadata.get(key) for key in _BINDING_KEYS}))
            if binding != self.binding:
                raise ValueError("原始光谱时段、实际波长轴或预处理链与保存身份不一致")
            key = (frame["stream_epoch"], frame["frame_id"])
            if key in self.seen:
                raise ValueError("原始光谱源帧身份重复")
            if tracker._last_time is not None and frame["host_rx_monotonic_ns"] <= tracker._last_time:
                raise ValueError("原始光谱时间回退或未递增")
            sequence = frame.get("device_sequence")
            if type(sequence) is int and tracker._last_device_sequence is not None and sequence < tracker._last_device_sequence:
                raise ValueError("原始光谱设备序号回退，拒绝重建身份")
            self.seen.add(key)
            use_baseline = _baseline_valid(baseline, frame, None, tracker)
            tracker._reference = list(baseline["lambda0_nm"]) if use_baseline else list(self.reference)
            tracker._reference_version = baseline["reference_version"] if use_baseline else self.version
            # This only freezes the existing reference. No begin/request action,
            # confirmation, initialization, or reference estimation is replayed.
            tracker._press = True
            output = tracker.process(deepcopy(frame))
            if tracker._windows != self.windows or tracker._anchors != self.anchors or tracker._pending_reference:
                raise ValueError("固定身份恢复适配器发现不可接受的自动状态变化")
            output["source_kind"] = frame.get("source_kind")
            return output
        except (ValueError, TypeError, KeyError) as exc:
            reason = str(exc)
        return {"frame_id": deepcopy(frame.get("frame_id")), "host_rx_monotonic_ns": _stamp(frame),
                "stream_epoch": frame.get("stream_epoch"), "source_kind": frame.get("source_kind"),
                "time_quality": deepcopy(frame.get("time_quality")), "state": "input_invalid", "message": reason,
                "peaks": [], "windows": deepcopy(self.windows), "algorithm_version": ALGORITHM_VERSION,
                "config_version": tracker.config_version, "reference_version": tracker._reference_version,
                "reference_pending": False}


def _baseline_valid(baseline, frame, context, tracker):
    if not isinstance(baseline, dict) or baseline.get("ready") is not True or baseline.get("unloaded_confirmed") is not True:
        return False
    effective, requested, stamp = baseline.get("effective_monotonic_ns"), baseline.get("requested_monotonic_ns"), _stamp(frame)
    if type(effective) is not int or type(requested) is not int or effective < requested or stamp is None or stamp < effective:
        return False
    if not _vector(baseline.get("lambda0_nm"), 9) or not _vector(baseline.get("force_zero"), 6) or type(baseline.get("reference_version")) is not int or baseline["reference_version"] <= 0:
        return False
    if baseline.get("algorithm_version") != ALGORITHM_VERSION or baseline.get("config_version") != tracker.config_version or baseline.get("optical_epoch") != frame.get("stream_epoch"):
        return False
    return context is None or all(baseline.get(key) == context.get(key) for key in ("optical_epoch", "force_epoch", "algorithm_version", "config_version"))


class _ForceReplay:
    """Sort compact PX6D evidence once; advance a bounded synchronization buffer.

    Optical raw arrays are streamed separately. The synchronizer never scans a
    complete session per spectrum, and source lookup retains only its 256-frame
    window. Different force epochs have independent iterators and buffers.
    """
    def __init__(self, frames):
        self.groups, self.positions, self.synchronizers = {}, {}, {}
        self.records = {}
        seen = set()
        for frame in frames:
            try:
                values = _force_values(frame)
            except (TypeError, ValueError, IndexError):
                values = None
            if not _identity(frame) or _stamp(frame) is None or values is None or not isinstance(frame.get("axes"), list) or not isinstance(frame.get("units"), list) or frame.get("source_kind") != "hardware" or not _trusted_time(frame) or frame.get("valid") is False or frame.get("stale") is True or _finite(frame.get("age_ms")) and frame["age_ms"] > 250:
                continue
            epoch, frame_id = frame["stream_epoch"], frame["frame_id"]
            if (epoch, frame_id) in seen:
                continue
            seen.add((epoch, frame_id))
            compact = {"frame_id": frame_id, "stream_epoch": epoch, "source_kind": "hardware",
                       "host_rx_monotonic_ns": _stamp(frame), "time_quality": frame["time_quality"],
                       "force_values": values, "axes": list(AXES), "units": list(UNITS)}
            self.groups.setdefault(epoch, []).append({"frame": compact, "annotation": _annotation(frame)})
        for epoch, group in self.groups.items():
            group.sort(key=lambda row: row["frame"]["host_rx_monotonic_ns"])
            self.positions[epoch] = 0
            self.synchronizers[epoch] = ForceSynchronizer(capacity=256)

    def advance(self, frame, force_epoch):
        synchronizer = self.synchronizers.get(force_epoch)
        stamp = _stamp(frame)
        if synchronizer is None or stamp is None:
            return synchronizer, self.records
        group, index = self.groups[force_epoch], self.positions[force_epoch]
        # Already buffered future data may serve this frame. Otherwise include
        # one nearest right source (even beyond the tolerance, to flag the gap).
        while index < len(group) and (not synchronizer.frames or synchronizer.frames[-1]["host_rx_monotonic_ns"] < stamp):
            record = group[index]
            source = record["frame"]
            if synchronizer.add(source):
                self.records[(force_epoch, source["frame_id"])] = record
            index += 1
        self.positions[force_epoch] = index
        retained = {(epoch, source["frame_id"]) for epoch, sync in self.synchronizers.items() for source in sync.frames}
        self.records = {key: record for key, record in self.records.items() if key in retained}
        return synchronizer, self.records


def _checked_sync(frame, annotation, baseline, synchronizer, records):
    sync = synchronizer.align(frame) if synchronizer and _trusted_time(frame) else {
        "valid": False, "matched": False, "force_values": None, "force_frame_ids": [],
        "spectrum_frame_id": frame.get("frame_id"), "spectrum_stream_epoch": frame.get("stream_epoch"),
        "force_stream_epoch": baseline.get("force_epoch"), "reason": "缺少可信光谱时间质量或同一采集时段的有效 PX6D 原始帧"}
    sources = [records.get((sync.get("force_stream_epoch"), key)) for key in sync.get("force_frame_ids", [])]
    reason = sync.get("reason")
    if sync.get("valid") is True:
        if not sources or any(source is None or source["annotation"].get("eligible") is not True or source["annotation"].get("session_id") != annotation.get("session_id") or any(source["annotation"].get(key) != annotation.get(key) for key in ("trial_id", "baseline_id")) for source in sources):
            reason = "PX6D 原始配对帧跨越会话、试次或无载基线"
        elif sync.get("force_stream_epoch") != baseline.get("force_epoch") or frame.get("stream_epoch") != baseline.get("optical_epoch"):
            reason = "原始同步采集时段与已确认基线不一致"
    stamp = _stamp(frame)
    times = sorted(_stamp(source["frame"]) for source in sources if source is not None)
    left = max((value for value in times if stamp is not None and value <= stamp), default=None)
    right = min((value for value in times if stamp is not None and value >= stamp), default=None)
    nearest = min((abs(value-stamp)/1e6 for value in times), default=None) if stamp is not None else None
    sync.update(force_source_stamps_ns=times, left_force_stamp_ns=left, right_force_stamp_ns=right,
                left_delta_ms=(stamp-left)/1e6 if left is not None else None,
                right_delta_ms=(right-stamp)/1e6 if right is not None else None,
                nearest_delta_ms=nearest, bracket_ms=(right-left)/1e6 if left is not None and right is not None else None,
                target_ms=5.0, target_exceeded=nearest > 5 if nearest is not None else None,
                synchronization_semantics="主机接收时间近似配对；无共同硬件时钟；5 ms 仅为检查目标",
                delta_ms_semantics="插值目标时刻偏差，不代表源帧接收误差为零或达到 5 ms 精度认证")
    if reason:
        sync.update(valid=False, reason=reason)
    return sync


def _compact_result(result, windows_hash):
    """Keep numerical evidence and quality, without duplicating raw/plot arrays."""
    keys = ("frame_id", "host_rx_monotonic_ns", "timestamp_source", "time_quality", "stream_epoch",
            "device_sequence", "device_timestamp_raw", "read_start_ns", "read_end_ns", "local_receive_id",
            "source_kind", "algorithm_version", "config_version", "state", "message", "reference_version",
            "reference_nm", "reference_pending", "reference_frozen", "drift_range_verified", "warnings")
    compact = {key: deepcopy(result[key]) for key in keys if key in result}
    compact["peaks"] = [{key: deepcopy(value) for key, value in peak.items() if key not in {"fit_x", "fit_y"}}
                        for peak in result.get("peaks", [])]
    compact["fixed_windows_sha256"] = windows_hash
    compact["axis_sha256"] = (result.get("axis_diagnostics") or {}).get("axis_sha256")
    compact["locator_noise_sigma"] = (result.get("locator_diagnostics") or {}).get("noise_sigma")
    return compact


def reprocess_session(path, manifest, progress=None):
    """Return one derived row per saved raw spectrum; never modify the archive.

    Raises ValueError when the recorded algorithm/config/fixed identity schema
    cannot be safely restored. Bad individual frames remain represented by an
    invalid row. File SHA256 verification belongs to the calling load_rows.
    """
    if not isinstance(manifest, dict) or manifest.get("kind") != "9fbg_calibration_experiment_v1":
        raise ValueError("只支持独立 9FBG 标定实验原始记录")
    adapter = _FrozenTracker(manifest)
    path = Path(path)
    force_replay = _ForceReplay(_iter_rows(path, "raw_force.jsonl"))
    context, settings = manifest["acquisition_context"], manifest.get("settings", {})
    if settings.get("force_sign") not in (-1, 1) or type(settings.get("force_sign")) not in (int, float):
        raise ValueError("实验原始法向力方向定义缺失或无效")
    final = manifest.get("final") or {}
    history = final.get("baseline_history", [])
    if not isinstance(history, list) or any(not isinstance(row, dict) or not isinstance(row.get("baseline_id"), str) for row in history) or len({row["baseline_id"] for row in history}) != len(history):
        raise ValueError("实验原始无载基线历史格式无效或编号重复")
    baselines = {row["baseline_id"]: row for row in history}
    trials = {row.get("trial_id"): row for row in final.get("trials", []) if isinstance(row, dict)}
    output = []
    windows_hash = digest(adapter.windows)
    total = (manifest.get("counts") or {}).get("raw_spectra")
    total = total if type(total) is int and total >= 0 else None
    for processed, frame in enumerate(_iter_rows(path, "raw_spectra.jsonl"), 1):
        annotation = _annotation(frame)
        baseline_key, trial_key = annotation.get("baseline_id"), annotation.get("trial_id")
        baseline = baselines.get(baseline_key, {}) if isinstance(baseline_key, str) else {}
        result = adapter.process(frame, baseline)
        peaks = result.get("peaks", [])
        valid_peaks = len(peaks) == 9 and all(row.get("id") == key and row.get("valid") is True and row.get("stale") is not True and _finite(row.get("center_nm")) for key, row in zip(IDS, peaks))
        ready = _baseline_valid(baseline, frame, context, adapter.tracker)
        synchronizer, records = force_replay.advance(frame, context.get("force_epoch"))
        sync = _checked_sync(frame, annotation, baseline, synchronizer, records)
        x_pm = [(peak["center_nm"]-zero)*1000 for peak, zero in zip(peaks, baseline["lambda0_nm"])] if valid_peaks and ready else None
        corrected = [value-zero for value, zero in zip(sync["force_values"], baseline["force_zero"])] if ready and sync.get("valid") is True and _vector(sync.get("force_values"), 6) else None
        normal = settings["force_sign"] * corrected[2] if corrected is not None else None
        trial = trials.get(trial_key, {}) if isinstance(trial_key, str) else {}
        bad = bool(annotation.get("manual_mark")) or baseline.get("bad") is True or trial.get("bad") is True
        phase = annotation.get("phase")
        matching_annotation = (annotation.get("eligible") is True and annotation.get("session_id") == manifest.get("session_id") and isinstance(phase, str) and phase in _PHASES and annotation.get("point_id") == baseline.get("point_id") and (phase == "unloaded" and trial_key is None or isinstance(trial_key, str) and bool(trial_key) and trial.get("baseline_id") == baseline_key and trial.get("point_id") == annotation.get("point_id")))
        valid = bool(valid_peaks and ready and sync.get("valid") is True and matching_annotation and frame.get("source_kind") == "hardware" and _trusted_time(frame) and frame.get("valid") is not False and frame.get("stale") is not True and setup_allows_trial(settings) and not bad)
        noise = baseline.get("force_noise_N")
        threshold = max(0.05, 3 * noise) if _finite(noise) and noise >= 0 else 0.05
        ratio = math.hypot(corrected[0], corrected[1])/abs(corrected[2]) if corrected is not None and abs(corrected[2]) > threshold else None
        limit = settings.get("max_force_N")
        row = {**annotation, "selected_point_id": annotation.get("point_id"),
               "point_id": annotation.get("point_id") if annotation.get("trial_id") and isinstance(phase, str) and phase in {"loading", "hold"} else None,
               "frame_id": deepcopy(frame.get("frame_id")), "host_rx_monotonic_ns": _stamp(frame),
               "result": _compact_result(result, windows_hash), "sync": sync, "x_pm": x_pm, "force_N": normal, "force_corrected": corrected,
               "valid": valid, "source_kind": frame.get("source_kind"), "algorithm_version": ALGORITHM_VERSION,
               "config_version": adapter.tracker.config_version, "reference_version": result.get("reference_version"),
               "split": settings.get("batch"), "bad": bad, "lateral_ratio": ratio,
               "lateral_warning": ratio is not None and ratio > 0.2, "force_noise_threshold_N": threshold,
               "force_limit_exceeded": normal is not None and _finite(limit) and normal > limit,
               "feature_processing": FEATURE_PROCESSING, "feature_restore_schema": RESTORE_SCHEMA,
               "feature_origin": "原始光谱离线固定身份重处理；原始在线 results.jsonl 保留不覆盖"}
        output.append(clean(row))
        if progress is not None and processed % 100 == 0:
            progress({"processed": processed, "total": total})
    if progress is not None:
        progress({"processed": len(output), "total": total if total is not None else len(output)})
    return output
