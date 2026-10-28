"""Rebuild causal full-spectrum sequences from immutable original session evidence."""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import uuid

import numpy as np

from .spectral_model import AXES, UNITS, canonical_digest

SPLIT_VERSION = "spectral_group_split_v1"
SYNC_RULE = {"schema": "timed_wrench_sync_v1", "maximum_bracket_ms": 60., "maximum_endpoint_ms": 45.,
             "hardware_delay_calibration": "unknown"}
LEGACY_SYNC_RULE = {"schema": "legacy_receive_sync_v1", "maximum_bracket_ms": 40., "maximum_endpoint_ms": 25.,
                    "hardware_delay_calibration": "unknown"}
RAW_FILES = {"raw_spectra.jsonl", "raw_force.jsonl", "results.jsonl", "events.jsonl"}
SPECTRAL_PROTOCOLS = {"timed_wrench_v1", "force_target_wrench_v1"}
TARGET_PROTOCOL = "force_target_wrench_v1"
TARGET_QUALITY = "force_target_wrench_quality_v1"
TARGET_QUALITY_V2 = "force_target_wrench_quality_v2"
TARGET_QUALITIES = {TARGET_QUALITY, TARGET_QUALITY_V2}
CONTACT_LABEL_RULE = {"version": "contact_label_v1", "positive": "trusted_position_supervision",
                      "negative": "explicit_no_contact_or_low_force_unloaded_stage",
                      "normal_negative_contact_threshold_ratio": .5,
                      "ambiguous": "masked", "force_labels": "real_synchronized_six_axis"}


def _no_contact(trial):
    """A non-spatial label is explicit and versioned, never inferred from a point."""
    from .no_contact import is_no_contact_trial
    return is_no_contact_trial(trial)


def _contact_supervision(trial, phase, sync, criteria, position_valid):
    if not sync["valid"]:
        return 0., False
    if _no_contact(trial):
        return 0., phase in {"preload", "no_contact", "recovery"}
    if position_valid:
        return 1., True
    values = sync["corrected"]
    threshold = min(criteria.get("contact_force_norm_N", .1) * .5,
                    criteria.get("unloaded_force_norm_floor_N", .1))
    norm = math.sqrt(sum(value * value for value in values[:3]))
    trusted_unloaded = phase in {"unloaded", "preload", "waiting_contact", "recovery"}
    return 0., bool(trusted_unloaded and norm <= threshold)


def _drift_features(results, spectra_by_id, checkpoint=None):
    """Read immutable fitted centers and match their original spectral source.

    The chart's drift/reference values are never used. Older v2 results may
    carry the same fitted peaks without the new explicit feature envelope.
    """
    from .drift_features import make_drift_frame, validate_drift_frame
    features = {}
    duplicates = set()
    for index, saved in enumerate(results):
        if index % 128 == 0:
            _check(checkpoint)
        result = saved.get("result", saved)
        if not isinstance(result, dict):
            continue
        identity = (result.get("stream_epoch"), result.get("frame_id"))
        raw = spectra_by_id.get(identity)
        if raw is None:
            continue
        if any(saved.get(key) is not None and saved.get(key) != raw.get(key)
               for key in ("trial_id", "baseline_id")):
            raise ValueError("九路提峰结果与原始光谱试次边界不一致")
        fresh = make_drift_frame(result, raw)
        recorded = result.get("drift_features")
        if recorded is not None and recorded != fresh:
            raise ValueError("保存的九路数值与原始提峰结果或光谱来源不一致")
        feature = recorded if recorded is not None else fresh
        validate_drift_frame(feature, raw)
        if identity in features:
            duplicates.add(identity)
        features[identity] = feature
    for identity in duplicates:
        features[identity] = None
    return features


def _drift_reference(trial, rows, features, reference, criteria, *, optical_validator=None):
    from .drift_features import make_drift_reference
    metrics = trial.get("quality", {}).get("metrics", {})
    effective = metrics.get("press_prompt_monotonic_ns")
    from .no_contact import NO_CONTACT_QUALITY_VERSION
    if (trial.get("quality", {}).get("criteria_version") not in {TARGET_QUALITY_V2, NO_CONTACT_QUALITY_VERSION}
            or criteria.get("preload_duration_s") != 2
            or metrics.get("preload_duration_s") != 2
            or metrics.get("preload_contact_detected") is not False
            or type(metrics.get("preload_recorded_duration_s")) not in (int, float)
            or metrics["preload_recorded_duration_s"] < 2
            or type(effective) is not int):
        raise ValueError("九路漂移训练需要本轮明确无接触的 2 秒采前基线；请用新流程重新采集")
    selected = []
    binding = ("stream_epoch", "algorithm_version", "config_version", "axis_sha256", "preprocessing")
    for row in rows:
        if row.get("phase") != "preload" or row["host_rx_monotonic_ns"] >= effective:
            continue
        identity = (row.get("stream_epoch"), row.get("frame_id"))
        if identity not in features:
            continue  # Unprocessed raw records cannot fill the fitted-center stream.
        feature = features[identity]
        if (not isinstance(feature, dict) or feature.get("all_valid") is not True
                or feature.get("source_valid") is not True
                or not (optical_validator(row) if optical_validator else _trusted(row, 512, "intensity"))
                or row.get("duplicate_identity")):
            selected = []
            continue
        if selected and (not 0 < feature["host_rx_monotonic_ns"]-selected[-1]["host_rx_monotonic_ns"] <= 120_000_000
                         or any(feature[key] != selected[-1][key] for key in binding)):
            selected = []
        selected.append(feature)
    # Match online collection: use the last continuous valid preload segment,
    # never reintroduce earlier measurements across a peak failure or gap.
    return make_drift_reference(selected, effective_ns=effective,
                                reference_id="D0-" + trial["trial_id"] + "-" + canonical_digest(selected)[:16],
                                trigger="recorded_two_second_preload", wavelength_nm=reference["wavelength_nm"],
                                trial_id=trial["trial_id"], baseline_id=trial["baseline_id"])


def _finite_vector(value, count):
    return (isinstance(value, (list, tuple)) and len(value) == count and
            all(type(item) in (int, float) and math.isfinite(item) for item in value))


def _check(checkpoint):
    if checkpoint:
        checkpoint()


def _digest(path, checkpoint):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            _check(checkpoint)
            result.update(chunk)
    return result.hexdigest()


def _read(path, checkpoint):
    rows = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for index, line in enumerate(stream):
            if index % 128 == 0:
                _check(checkpoint)
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("原始记录必须为对象：" + path.name)
            rows.append(value)
    return rows


def _annotation(row, events):
    """Original raw annotation wins; older raw records may use the saved event timeline."""
    value = row.get("experiment") if isinstance(row.get("experiment"), dict) else row
    if "baseline_id" in value:
        return value
    stamp = row.get("host_rx_monotonic_ns", 0)
    index = bisect_left([event.get("host_rx_monotonic_ns", 0) for event in events], stamp + 1) - 1
    return events[index] if index >= 0 else {}


def _trusted(row, count, field):
    return (row.get("source_kind") == "hardware" and row.get("time_quality") in {"host_rx_approx", "hardware_mapped"}
            and type(row.get("host_rx_monotonic_ns")) is int and row["host_rx_monotonic_ns"] > 0
            and isinstance(row.get("stream_epoch"), str) and bool(row["stream_epoch"])
            and type(row.get("frame_id")) in (str, int) and row.get("frame_id") != ""
            and _finite_vector(row.get(field), count) and row.get("stale") is not True
            and (field == "intensity" or row.get("valid") is not False))


def _physical(settings):
    conditions = settings.get("conditions") or {}
    if not isinstance(conditions, dict):
        raise ValueError("装夹与温度条件格式无效")
    return {"finger_id": settings.get("finger_id"), "point_grid": deepcopy(settings.get("point_grid")),
            "coordinate_definition": deepcopy(settings.get("coordinate_definition")),
            "conditions": {key: conditions.get(key, settings.get(key)) for key in ("fixation", "temperature_C")},
            "axes": AXES, "units": UNITS, "wrench_reference": "PX6D_native_axes_and_origin"}


def _sample_rate(manifest):
    for device in manifest.get("acquisition_audit", {}).get("devices") or []:
        config = device.get("configuration") or {}
        if config.get("trigger_hz"):
            return config["trigger_hz"]
        if config.get("interval_ms"):
            return 1000 / config["interval_ms"]
    return manifest.get("settings", {}).get("sample_rate_hz")


def _reference(baseline, spectra_by_id, *, allow_legacy=False):
    reference = deepcopy(baseline.get("spectral_reference") or {})
    for key, flat in (("S0", "S0"), ("wavelength_nm", "wavelength_nm"), ("source_frame_ids", "source_frame_ids"),
                      ("source_stamps_ns", "source_stamps_ns"), ("reference_id", "spectral_reference_id"),
                      ("stream_epoch", "optical_epoch"), ("effective_ns", "effective_monotonic_ns")):
        reference.setdefault(key, baseline.get(flat))
    derived = False
    if reference.get("S0") is None and allow_legacy and baseline.get("unloaded_confirmed") is True:
        start = baseline.get("requested_monotonic_ns")
        end = baseline.get("effective_monotonic_ns")
        chosen = [row for row in spectra_by_id.values() if row.get("baseline_id") == baseline.get("baseline_id")
                  and row.get("phase") == "unloaded" and type(start) is int and type(end) is int
                  and start <= row["host_rx_monotonic_ns"] <= end and _trusted(row, 512, "intensity")]
        if len(chosen) >= 12:
            reference.update(S0=np.median([row["intensity"] for row in chosen], axis=0).tolist(),
                             wavelength_nm=chosen[0]["wavelength_nm"], preprocessing=chosen[0].get("preprocessing", {}),
                             source_frame_ids=[row["frame_id"] for row in chosen],
                             source_stamps_ns=[row["host_rx_monotonic_ns"] for row in chosen],
                             reference_id="derived_" + canonical_digest([row["frame_id"] for row in chosen])[:20])
            derived = True
    if (not _finite_vector(reference.get("S0"), 512) or not _finite_vector(reference.get("wavelength_nm"), 512)
            or not _finite_vector(baseline.get("force_zero"), 6) or not reference.get("reference_id")
            or not reference.get("stream_epoch") or baseline.get("unloaded_confirmed") is not True
            or type(reference.get("effective_ns")) is not int):
        raise ValueError("试次缺少真实 512 点无载参考、有效六轴零偏或明确无载证据")
    ids, stamps = reference.get("source_frame_ids"), reference.get("source_stamps_ns")
    if not isinstance(ids, list) or not ids or not isinstance(stamps, list) or len(stamps) != len(ids) or len(set(ids)) != len(ids):
        raise ValueError("全谱无载参考缺少唯一源帧和采集时间")
    sources = []
    for identity, stamp in zip(ids, stamps):
        source = spectra_by_id.get((reference["stream_epoch"], identity))
        if (source is None or not _trusted(source, 512, "intensity") or source["host_rx_monotonic_ns"] != stamp
                or stamp > reference["effective_ns"] or source.get("phase") not in {"unloaded", "recovery"}
                or source.get("wavelength_nm") != reference["wavelength_nm"]):
            raise ValueError("全谱无载参考与真实源帧不一致")
        sources.append(source)
    expected = np.median(np.asarray([row["intensity"] for row in sources], dtype=np.float64), axis=0)
    if not np.allclose(expected, reference["S0"], rtol=1e-9, atol=1e-9):
        raise ValueError("全谱 S0 不等于所记录无载源帧的逐点中位数")
    if not all(a < b for a, b in zip(reference["wavelength_nm"], reference["wavelength_nm"][1:])):
        raise ValueError("全谱波长轴必须严格递增")
    reference.setdefault("preprocessing", deepcopy(sources[0].get("preprocessing") or {}))
    if any((source.get("preprocessing") or {}) != reference["preprocessing"] for source in sources):
        raise ValueError("无载参考源帧预处理配置不同")
    reference["derived_from_legacy"] = derived
    return reference


def align_force(spectrum, force_rows, force_times, baseline, sync_rule=None):
    """Offline bracketing uses force receive times, never row numbers or future spectra."""
    sync_rule = sync_rule or SYNC_RULE
    stamp = spectrum["host_rx_monotonic_ns"]
    index = bisect_left(force_times, stamp)
    if index < len(force_rows) and force_times[index] == stamp:
        chosen = [force_rows[index]]
    elif 0 < index < len(force_rows):
        chosen = force_rows[index - 1:index + 1]
    else:
        return {"valid": False, "reason": "missing_force_bracket"}
    for row in chosen:
        if (not _trusted(row, 6, "force_values") or row.get("stream_epoch") != baseline.get("force_epoch")
                or any(row.get(key) != spectrum.get(key) for key in ("trial_id", "baseline_id"))):
            return {"valid": False, "reason": "force_boundary_or_source"}
        if row.get("axes", AXES) != AXES or row.get("units", UNITS) != UNITS:
            return {"valid": False, "reason": "force_axes_or_units"}
    if len(chosen) == 2:
        left, right = chosen
        gap = right["host_rx_monotonic_ns"] - left["host_rx_monotonic_ns"]
        if gap <= 0 or gap > sync_rule["maximum_bracket_ms"] * 1e6 or max(stamp - left["host_rx_monotonic_ns"], right["host_rx_monotonic_ns"] - stamp) > sync_rule["maximum_endpoint_ms"] * 1e6:
            return {"valid": False, "reason": "force_gap_or_endpoint"}
        fraction = (stamp - left["host_rx_monotonic_ns"]) / gap
        raw = [a + (b - a) * fraction for a, b in zip(left["force_values"], right["force_values"])]
    else:
        raw = list(chosen[0]["force_values"])
    return {"valid": True, "raw": raw, "zero": list(baseline["force_zero"]),
            "corrected": [value - zero for value, zero in zip(raw, baseline["force_zero"])],
            "force_frame_ids": [row["frame_id"] for row in chosen],
            "force_stream_epoch": baseline["force_epoch"],
            "source_stamps_ns": [row["host_rx_monotonic_ns"] for row in chosen],
            "method": "exact_receive_time" if len(chosen) == 1 else "linear_receive_time"}


def _target_quality_evidence(trial, criteria, baseline, spectra, forces, *, optical_validator=None,
                              check_contact_label=True):
    """Keep v1 recovery semantics; v2 is an explicit operator assertion.

    The operator assertion does not certify unloaded recovery or physical
    accuracy. Its new zero must still name actual post-finish source frames.
    """
    if check_contact_label and _no_contact(trial):
        return _no_contact_quality_evidence(trial, criteria, baseline, spectra, forces,
                                            optical_validator=optical_validator)
    quality = trial.get("quality", {})
    version = quality.get("criteria_version")
    if version not in TARGET_QUALITIES:
        raise ValueError("变长合力试次质量规则版本不兼容")
    if version == TARGET_QUALITY:
        if criteria.get("criteria_version", TARGET_QUALITY) != TARGET_QUALITY:
            raise ValueError("变长合力试次质量规则版本不兼容")
        return
    if (criteria.get("criteria_version") != TARGET_QUALITY_V2
            or criteria.get("preload_duration_s") != 2
            or criteria.get("finish_zero_semantics") != "operator_unloaded_assertion_first_post_finish_source_frames"
            or criteria.get("automatic_unloaded_recovery_required") is not False
            or type(criteria.get("zero_source_frame_count")) is not int
            or criteria["zero_source_frame_count"] != 1):
        raise ValueError("直接回零试次的质量规则与版本不一致")
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    evidence = metrics.get("unloaded_evidence") if isinstance(metrics.get("unloaded_evidence"), dict) else {}
    boundary = evidence.get("finish_requested_monotonic_ns")
    sampling_boundary = evidence.get("zero_sampling_monotonic_ns")
    preload_recorded = metrics.get("preload_recorded_duration_s")
    if (evidence.get("schema") != "manual_finish_unloaded_assertion_v1"
            or evidence.get("trigger") != "manual_unloaded_confirmation"
            or evidence.get("operator_confirmed") is not True
            or evidence.get("automatic_unloaded_recovery_verified") is not False
            or evidence.get("selection") != "first_trusted_source_frame_strictly_after_finish_boundary"
            or evidence.get("previous_baseline_id") != trial.get("baseline_id")
            or type(boundary) is not int or boundary <= 0
            or type(sampling_boundary) is not int or sampling_boundary < boundary
            or metrics.get("finish_requested_monotonic_ns") != boundary
            or trial.get("finish_requested_monotonic_ns") != boundary
            or trial.get("zero_sampling_monotonic_ns") != sampling_boundary
            or metrics.get("preload_duration_s") != 2
            or type(preload_recorded) not in (int, float) or not math.isfinite(preload_recorded) or preload_recorded < 2
            or metrics.get("preload_contact_detected") is not False
            or type(metrics.get("press_prompt_monotonic_ns")) is not int
            or metrics.get("automatic_unloaded_recovery_required") is not False
            or metrics.get("manual_finish_unloaded_confirmed") is not True
            or metrics.get("physical_accuracy_qualified") is not False):
        raise ValueError("直接回零试次缺少明确的人工卸载声明；不能视为自动恢复或精度验收")
    for label, rows, id_key, stamp_key, epoch_key, field, count in (
            ("六轴", forces, "force_frame_ids", "force_source_stamps_ns", "force_epoch", "force_values", 6),
            ("光谱", spectra, "spectrum_frame_ids", "spectrum_source_stamps_ns", "optical_epoch", "intensity", 512)):
        ids, stamps = evidence.get(id_key), evidence.get(stamp_key)
        if (not isinstance(ids, list) or len(ids) != 1 or type(ids[0]) not in (str, int)
                or not isinstance(stamps, list) or len(stamps) != 1
                or type(stamps[0]) is not int or stamps[0] <= sampling_boundary):
            raise ValueError("直接回零" + label + "源帧必须来自结束边界之后")
        sources = [row for row in rows if row.get("frame_id") == ids[0]
                   and row.get("stream_epoch") == baseline.get(epoch_key)
                   and not row.get("duplicate_identity")]
        trusted = (len(sources) == 1 and (optical_validator(sources[0])
                   if field == "intensity" and optical_validator else _trusted(sources[0], count, field)))
        if (not trusted
                or sources[0]["host_rx_monotonic_ns"] != stamps[0]
                or sources[0].get("phase") != "recovery"
                or sources[0].get("trial_id") != trial.get("trial_id")
                or sources[0].get("baseline_id") != trial.get("baseline_id")
                or field == "force_values" and (sources[0].get("axes", AXES) != AXES
                                                 or sources[0].get("units", UNITS) != UNITS)):
            raise ValueError("直接回零" + label + "证据与真实源帧不一致")


def _no_contact_quality_evidence(trial, criteria, baseline, spectra, forces, *, optical_validator=None):
    """Recheck the negative label against immutable source force and finish frames."""
    from .no_contact import NO_CONTACT_POINT_ID, no_contact_limits, validate_no_contact_trial
    metrics = validate_no_contact_trial(trial, criteria)
    norm_limit, axis_limits = no_contact_limits(criteria, baseline)
    if (type(metrics.get("unloaded_force_norm_limit_N")) not in (int, float)
            or not math.isclose(metrics["unloaded_force_norm_limit_N"], norm_limit, rel_tol=0, abs_tol=1e-12)
            or not _finite_vector(metrics.get("unloaded_axis_limits"), 6)
            or not np.allclose(metrics["unloaded_axis_limits"], axis_limits, rtol=0, atol=1e-12)):
        raise ValueError("无接触质量阈值与固定六轴噪声参考不一致")
    end = trial.get("data_end_monotonic_ns")
    start = metrics["recording_started_monotonic_ns"]
    finish = metrics["finish_requested_monotonic_ns"]
    if type(end) is not int or end < finish:
        raise ValueError("无接触轮次缺少完整数据结束边界")
    selected_force = [row for row in forces if row.get("trial_id") == trial["trial_id"]
                      and type(row.get("host_rx_monotonic_ns")) is int and row["host_rx_monotonic_ns"] <= end
                      and row.get("phase") in {"preload", "no_contact", "recovery"}]
    if not selected_force:
        raise ValueError("无接触轮次缺少真实六轴源帧")
    for row in selected_force:
        if (not _trusted(row, 6, "force_values") or row.get("duplicate_identity")
                or row.get("baseline_id") != trial.get("baseline_id")
                or row.get("stream_epoch") != baseline.get("force_epoch")
                or row.get("point_id") != NO_CONTACT_POINT_ID
                or row.get("axes", AXES) != AXES or row.get("units", UNITS) != UNITS):
            raise ValueError("无接触标签缺少可信且边界一致的六轴源帧")
        corrected = [value - zero for value, zero in zip(row["force_values"], baseline["force_zero"])]
        norm = math.sqrt(sum(value * value for value in corrected[:3]))
        if norm > norm_limit or any(abs(value) > limit for value, limit in zip(corrected, axis_limits)):
            raise ValueError("无接触标签的真实六轴源帧超出无载阈值，拒绝作为负类")
    recorded = [row for row in spectra if row.get("trial_id") == trial["trial_id"]
                and row.get("phase") == "no_contact" and type(row.get("host_rx_monotonic_ns")) is int
                and start <= row["host_rx_monotonic_ns"] <= finish
                and row.get("baseline_id") == trial.get("baseline_id")
                and row.get("point_id") == NO_CONTACT_POINT_ID and not row.get("duplicate_identity")
                and row.get("stream_epoch") == baseline.get("optical_epoch")
                and (optical_validator(row) if optical_validator else _trusted(row, 512, "intensity"))]
    recorded.sort(key=lambda row: row["host_rx_monotonic_ns"])
    force_ordered = sorted(selected_force, key=lambda row: row["host_rx_monotonic_ns"])
    force_times = [row["host_rx_monotonic_ns"] for row in force_ordered]
    count, longest, previous = 0, 0, None
    for row in recorded:
        stamp = row["host_rx_monotonic_ns"]
        valid = align_force(row, force_ordered, force_times, baseline, criteria["sync_rule"])["valid"]
        if not valid or previous is not None and not 0 < stamp - previous <= criteria["maximum_spectrum_gap_ms"] * 1e6:
            count = 0
        if valid:
            count += 1
            longest = max(longest, count)
        previous = stamp
    if (longest < criteria.get("minimum_no_contact_spectra", 16) or not recorded
            or (recorded[-1]["host_rx_monotonic_ns"] - start) / 1e9 < criteria.get("minimum_recording_s", .64)):
        raise ValueError("无接触标签缺少足够真实连续光学源帧")
    for rows, gap in ((spectra, criteria["maximum_spectrum_gap_ms"]), (selected_force, criteria["maximum_force_gap_ms"])):
        preload = sorted(row["host_rx_monotonic_ns"] for row in rows
                         if row.get("trial_id") == trial["trial_id"] and row.get("phase") == "preload"
                         and row.get("baseline_id") == trial.get("baseline_id")
                         and type(row.get("host_rx_monotonic_ns")) is int and row["host_rx_monotonic_ns"] < start
                         and not row.get("duplicate_identity"))
        if (not preload or (preload[-1] - preload[0]) / 1e9 < 2
                or any(not 0 < b - a <= gap * 1e6 for a, b in zip(preload, preload[1:]))):
            raise ValueError("无接触标签的真实连续采前源帧不足 2 秒")
    # The common direct-zero validator retains its exact source identity rules.
    # This local view is never persisted and does not relabel the original trial.
    finish_trial = deepcopy(trial)
    finish_trial["quality"]["criteria_version"] = TARGET_QUALITY_V2
    finish_criteria = deepcopy(criteria)
    finish_criteria["criteria_version"] = TARGET_QUALITY_V2
    _target_quality_evidence(finish_trial, finish_criteria, baseline, spectra, forces,
                             optical_validator=optical_validator, check_contact_label=False)


def _target_baseline_evidence(baseline, reference, spectra, forces, trials, criteria, *, optical_validator=None):
    """Verify the applied v2 zero against its immutable original force sample."""
    evidence = baseline.get("unloaded_evidence")
    if criteria.get("criteria_version") != TARGET_QUALITY_V2:
        return
    direct = (baseline.get("prepared_from_baseline_id") is not None or baseline.get("zero_semantics") is not None
              or isinstance(evidence, dict) and evidence.get("schema") == "manual_finish_unloaded_assertion_v1")
    if not direct:
        return  # Initial 12-frame baselines and historical v1 keep their rules.
    if not isinstance(evidence, dict) or evidence.get("schema") != "manual_finish_unloaded_assertion_v1":
        raise ValueError("直接回零基线缺少人工卸载声明及自己的真实源帧")
    previous_id = evidence.get("previous_baseline_id")
    source_trials = [trial for trial in trials.values() if trial.get("baseline_id") == previous_id
                     and trial.get("quality", {}).get("metrics", {}).get("unloaded_evidence") == evidence]
    from .no_contact import NO_CONTACT_QUALITY_VERSION
    if (len(source_trials) != 1 or baseline.get("quality_version") not in {TARGET_QUALITY_V2, NO_CONTACT_QUALITY_VERSION}
            or baseline.get("prepared_from_baseline_id") != previous_id
            or baseline.get("automatic_unloaded_recovery_verified") is not False
            or baseline.get("zero_semantics") != "operator_unloaded_assertion_first_post_finish_source_frames"
            or baseline.get("force_frame_ids") != evidence.get("force_frame_ids")
            or baseline.get("force_source_stamps_ns") != evidence.get("force_source_stamps_ns")
            or reference.get("source_frame_ids") != evidence.get("spectrum_frame_ids")
            or reference.get("source_stamps_ns") != evidence.get("spectrum_source_stamps_ns")):
        raise ValueError("直接回零基线与上一轮人工声明及源帧身份不一致")
    previous_trial = source_trials[0]
    # Failed preceding rounds may still produce a valid new manual zero. Check
    # its declared source semantics without pretending that round was accepted.
    previous_criteria = {
        "criteria_version": TARGET_QUALITY_V2, "preload_duration_s": 2,
        "finish_zero_semantics": "operator_unloaded_assertion_first_post_finish_source_frames",
        "automatic_unloaded_recovery_required": False, "zero_source_frame_count": 1,
    }
    if _no_contact(previous_trial):
        # Its own fixed baseline was already validated when the accepted trial
        # was loaded. Here only the post-finish source envelope is needed.
        finish_trial = deepcopy(previous_trial)
        finish_trial["quality"]["criteria_version"] = TARGET_QUALITY_V2
        previous_trial = finish_trial
    _target_quality_evidence(previous_trial, previous_criteria,
       {"force_epoch": baseline.get("force_epoch"), "optical_epoch": reference.get("stream_epoch")}, spectra, forces,
       optical_validator=optical_validator, check_contact_label=False)
    source = next(row for row in forces if row.get("stream_epoch") == baseline.get("force_epoch")
                  and row.get("frame_id") == evidence["force_frame_ids"][0]
                  and row.get("host_rx_monotonic_ns") == evidence["force_source_stamps_ns"][0])
    if not np.allclose(source["force_values"], baseline["force_zero"], rtol=1e-9, atol=1e-9):
        raise ValueError("直接回零六轴零偏不等于所记录的真实源帧")


def _baseline_components(baselines, references, spectra_by_id=None):
    """References with shared original unloaded frames belong to one split group."""
    parent = {key: key for key in references}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    owners = {}
    for key, reference in references.items():
        reusable = baselines[key].get("reused_from_baseline_id")
        if reusable in parent:
            a, b = find(key), find(reusable)
            parent[max(a, b)] = min(a, b)
        for frame_id in reference["source_frame_ids"]:
            identity = (reference["stream_epoch"], frame_id)
            # An automatic next baseline may use the previous trial's recovery
            # spectra. Those raw frames were also inputs/targets of that trial,
            # even though its own S0 used different pre-contact source frames.
            source = (spectra_by_id or {}).get(identity, {})
            source_baseline = source.get("baseline_id") if source.get("trial_id") else None
            if source_baseline in parent:
                a, b = find(key), find(source_baseline)
                parent[max(a, b)] = min(a, b)
            if identity in owners:
                a, b = find(key), find(owners[identity])
                parent[max(a, b)] = min(a, b)
            owners[identity] = key
    return {key: find(key) for key in references}


def _atomic_json(path, value):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def assign_splits(userdata, groups, compatibility, *, seed=1701, inherited_split=None, new_training_sessions=None):
    """Append-only split decisions. Training never reassigns a previous held-out group."""
    home = Path(userdata).resolve()
    root = home / "spectral_splits"
    if root.is_symlink() or hasattr(root, "is_junction") and root.is_junction() or root.resolve().parent != home:
        raise ValueError("固定划分目录超出允许位置")
    root.mkdir(parents=True, exist_ok=True)
    path = root / (canonical_digest(compatibility) + ".json")
    if path.is_symlink():
        raise ValueError("固定划分文件不能为链接")
    existing = path.exists()
    state = json.loads(path.read_text(encoding="utf-8")) if existing else {
        "version": SPLIT_VERSION, "seed": seed, "compatibility": deepcopy(compatibility),
        "assignments": {}, "source_manifests": {}, "group_points": {}, "trial_assignments": {}}
    if state.get("version") != SPLIT_VERSION or state.get("compatibility") != compatibility:
        raise ValueError("已保存的全谱分组划分格式不兼容")
    if inherited_split is not None:
        inherited = inherited_split
        if (inherited.get("version") != SPLIT_VERSION
                or inherited.get("compatibility", compatibility) != compatibility
                or inherited.get("group_mode") not in {"session", "baseline_component"}):
            raise ValueError("父模型固定划分与本次物理或光谱定义不兼容")
        if existing and state.get("group_mode") != inherited["group_mode"]:
            raise ValueError("本地固定划分与父模型的分组方式冲突")
        state["group_mode"] = inherited["group_mode"]
        if not existing:
            state["seed"] = inherited.get("seed", seed)
        for name in ("assignments", "trial_assignments", "source_manifests", "group_points"):
            imported = inherited.get(name, {})
            if not isinstance(imported, dict):
                raise ValueError("父模型固定划分格式无效")
            target = state.setdefault(name, {})
            for key, value in imported.items():
                if key in target and target[key] != value:
                    raise ValueError("本地固定划分与父模型的历史试次或来源冲突")
                target[key] = deepcopy(value)
    assignments = state["assignments"]
    candidates = sorted(groups, key=lambda group: canonical_digest([state["seed"], group]))
    # Choose complete sessions when they have not already acquired per-baseline assignments.
    sessions = {item["session_id"] for item in groups.values() if item["batch"] != "C"}
    state.setdefault("group_mode", "session" if len(sessions) >= 2 else "baseline_component")
    logical = defaultdict(list)
    for key in candidates:
        group = groups[key]
        logical[group["session_id"] if state["group_mode"] == "session" else key].append(key)
    logical_points = {key: set().union(*(set(groups[item]["point_ids"]) for item in values)) for key, values in logical.items()}
    logical_contact = {key: set().union(*(set(groups[item].get("contact_labels", [])) for item in values))
                       for key, values in logical.items()}
    decisions = {}
    for logical_id, keys in logical.items():
        known = {assignments[key] for key in keys if key in assignments}
        # Shared original baseline/trial identities retain their previous assignment.
        known.update(state["trial_assignments"][trial] for key in keys for trial in groups[key]["trial_ids"]
                     if trial in state["trial_assignments"])
        if len(known) > 1:
            raise ValueError("共享无载来源的试次曾分入不同集合，拒绝产生泄漏")
        if known:
            decisions[logical_id] = known.pop()
        elif all(groups[key]["batch"] == "C" for key in keys):
            decisions[logical_id] = "test"
    explicit_logical = {logical_id for logical_id, keys in logical.items()
                        if any(groups[key].get("explicit_no_contact") for key in keys)}
    # Explicit operator-labelled negatives are a distinct acquisition source.
    # Ordinary low-force preload labels cannot replace their training coverage.
    # Historical held-out assignments remain immutable, including when every
    # explicit negative is already held out.
    if not any(decisions.get(key) == "train" for key in explicit_logical):
        new_explicit = sorted(explicit_logical - decisions.keys(),
                              key=lambda value: canonical_digest([state["seed"], value]))
        if new_explicit:
            decisions[new_explicit[0]] = "train"
    if new_training_sessions:
        new_sources = set(new_training_sessions)
        eligible = {logical_id for logical_id, keys in logical.items() if any(
            groups[key]["session_id"] in new_sources and groups[key].get("optimization_eligible") for key in keys)}
        # Only previously unassigned groups may be chosen. A single new group
        # can be replayed even when neither generation has independent holdout;
        # established validation groups never change to make room for it.
        if not any(decisions.get(key) == "train" for key in eligible):
            candidates_new = sorted(eligible - decisions.keys(), key=lambda value: canonical_digest([state["seed"], value]))
            if candidates_new:
                decisions[candidates_new[0]] = "train"
    # Ensure training has actual point coverage before considering held-out groups.
    training_points = set().union(*(logical_points[key] for key, value in decisions.items() if value == "train")) if decisions else set()
    training_contact = set().union(*(logical_contact[key] for key, value in decisions.items() if value == "train")) if decisions else set()
    for key in sorted(logical, key=lambda value: canonical_digest([state["seed"], value])):
        if key not in decisions and (not logical_points[key].issubset(training_points)
                                     or not logical_contact[key].issubset(training_contact)):
            decisions[key] = "train"
            training_points.update(logical_points[key])
            training_contact.update(logical_contact[key])
    if not any(value == "train" for value in decisions.values()):
        candidates_train = [key for key in sorted(logical, key=lambda value: canonical_digest([state["seed"], value]))
                            if key not in decisions and any(groups[item].get("optimization_eligible") for item in logical[key])]
        if candidates_train:
            decisions[candidates_train[0]] = "train"
    validation_count = sum(value == "validation" for value in decisions.values())
    desired = max(1, round(.2 * sum(not all(groups[item]["batch"] == "C" for item in values) for values in logical.values())))
    for key in sorted(logical, key=lambda value: canonical_digest([state["seed"], value])):
        if key not in decisions:
            decisions[key] = "validation" if validation_count < desired else "train"
            validation_count += decisions[key] == "validation"
    for logical_id, keys in logical.items():
        for key in keys:
            group = groups[key]
            if key in assignments and assignments[key] != decisions[logical_id]:
                raise ValueError("固定划分不能重新分配历史试次")
            assignments[key] = decisions[logical_id]
            state["group_points"][key] = group["point_ids"]
            for trial in group["trial_ids"]:
                state["trial_assignments"][trial] = decisions[logical_id]
            previous = state["source_manifests"].get(group["session_id"])
            if previous is not None and previous != group["manifest_sha256"]:
                raise ValueError("已划分原始会话发生变化，拒绝重新训练")
            state["source_manifests"][group["session_id"]] = group["manifest_sha256"]
    _atomic_json(path, state)
    return state


def load_dataset(userdata, session_ids, options=None, *, checkpoint=None, progress=None,
                 inherited_split=None, expected_metadata=None, new_training_sessions=None):
    """Load closed hashed sources; nine-input features use source-matched peaks.

    Pilot x_pm and demonstration-chart references are never training inputs.
    """
    from .history import session_path, _ids
    from . import peak_drift_data as compact_data
    options = options or {}
    drift = options.get("method") == "drift_gru"
    if drift and not 40 <= options.get("max_spectrum_gap_ms", 120) <= 120:
        raise ValueError("九路漂移的源帧连续性间隔必须为 40～120 ms")
    session_ids = _ids(session_ids)
    paths = [session_path(userdata, value, include_deleted=False).resolve() for value in sorted(session_ids)]
    if len({os.path.normcase(str(path)) for path in paths}) != len(paths):
        raise ValueError("所选记录重复指向同一原始会话")
    sources, references_info, groups, sequences = [], [], {}, []
    physical = axis = preprocessing = sample_rate = chosen_sync_rule = peak_definition = None
    rejected = Counter()
    for session_index, path in enumerate(paths):
        _check(checkpoint)
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8-sig"))
        if manifest.get("kind") != "9fbg_calibration_experiment_v1" or manifest.get("complete") is not True or manifest.get("error") or manifest.get("overflow", 0):
            raise ValueError("全谱训练只读取完整结束且无丢盘的会话：" + path.name)
        settings, files = manifest.get("settings", {}), manifest.get("files", {})
        compact = compact_data.is_compact(manifest)
        if compact and not drift:
            raise ValueError("寻峰漂移会话仅含九路峰心和漂移，不能训练 512 点全谱模型")
        required_files = compact_data.FILES if compact else RAW_FILES
        optical_validator = compact_data.trusted_optical if compact else None
        static_axis = compact_data.axis_for(manifest) if compact else None
        protocol = settings.get("acquisition_protocol", "guided_round_v1")
        if protocol not in SPECTRAL_PROTOCOLS and options.get("adapt_legacy_unloaded") is not True:
            raise ValueError("旧协议须明确启用真实采前无载参考适配，不能自动当作六维采集")
        if protocol in SPECTRAL_PROTOCOLS and _sample_rate(manifest) != 25:
            raise ValueError(protocol + " 需要真实记录的 25 Hz 光谱请求设置")
        criteria_key = "force_target_wrench_criteria" if protocol == TARGET_PROTOCOL else "timed_wrench_criteria"
        criteria = settings.get(criteria_key, {})
        sync_rule = criteria.get("sync_rule", SYNC_RULE) if protocol in SPECTRAL_PROTOCOLS else LEGACY_SYNC_RULE
        if sync_rule != (SYNC_RULE if protocol in SPECTRAL_PROTOCOLS else LEGACY_SYNC_RULE):
            raise ValueError("六维同步规则版本或阈值不兼容")
        if chosen_sync_rule is not None and sync_rule != chosen_sync_rule:
            raise ValueError("不能在同一模型中混合不同六维同步规则")
        chosen_sync_rule = deepcopy(sync_rule)
        if not required_files.issubset(files) or compact and set(files) != required_files:
            raise ValueError("全谱会话缺少原始四文件及哈希")
        for name in sorted(required_files):
            if not isinstance(files[name], str) or _digest(path / name, checkpoint) != files[name]:
                raise ValueError("全谱原始来源哈希不匹配：" + path.name + "/" + name)
        signature = _physical(settings)
        if physical is not None and signature != physical:
            raise ValueError("不能混合不同样件、点阵、装夹或温度条件的六维会话")
        physical = signature
        events = sorted(_read(path / "events.jsonl", checkpoint), key=lambda row: row.get("host_rx_monotonic_ns", 0))
        spectra = _read(path / ("optical_sources.jsonl" if compact else "raw_spectra.jsonl"), checkpoint)
        forces = _read(path / "raw_force.jsonl", checkpoint)
        for rows in (spectra, forces):
            seen = set()
            for row in rows:
                annotation = _annotation(row, events)
                row.update({key: annotation.get(key) for key in ("trial_id", "baseline_id", "point_id", "phase")})
                identity = (row.get("stream_epoch"), row.get("frame_id"))
                if identity in seen:
                    row["duplicate_identity"] = True
                seen.add(identity)
        spectra_by_id = {(row.get("stream_epoch"), row.get("frame_id")): row for row in spectra if not row.get("duplicate_identity")}
        peak_rows = _read(path / ("peak_frames.jsonl" if compact else "results.jsonl"), checkpoint) if drift else []
        features = (compact_data.features_from_rows(peak_rows, spectra_by_id, static_axis, checkpoint)
                    if compact else _drift_features(peak_rows, spectra_by_id, checkpoint)) if drift else {}
        if drift and not features:
            raise ValueError("所选会话没有与原始光谱匹配的九路提峰数值，请使用九路漂移模式重新采集")
        baselines = {row["baseline_id"]: row for row in manifest.get("final", {}).get("baseline_history", [])}
        trials = {row["trial_id"]: row for row in manifest.get("final", {}).get("trials", [])}
        accepted_trials = {key: trial for key, trial in trials.items() if trial.get("status") == "completed"
                           and not trial.get("bad") and not trial.get("manual_mark")
                           and (trial.get("quality", {}).get("accepted") is True if protocol in SPECTRAL_PROTOCOLS else True)}
        if protocol == TARGET_PROTOCOL:
            for trial in accepted_trials.values():
                trial_criteria = settings.get("no_contact_criteria", {}) if _no_contact(trial) else criteria
                _target_quality_evidence(trial, trial_criteria, baselines.get(trial.get("baseline_id"), {}), spectra, forces,
                                         optical_validator=optical_validator)
        refs = {}
        for baseline_id in {trial.get("baseline_id") for trial in accepted_trials.values()}:
            if baseline_id not in baselines:
                raise ValueError("完整试次缺少对应基线历史")
            refs[baseline_id] = (compact_data.baseline_reference(baselines[baseline_id], spectra_by_id, static_axis, forces)
                if compact else _reference(baselines[baseline_id], spectra_by_id, allow_legacy=options.get("adapt_legacy_unloaded") is True))
            if protocol == TARGET_PROTOCOL:
                _target_baseline_evidence(baselines[baseline_id], refs[baseline_id], spectra, forces, trials, criteria,
                                          optical_validator=optical_validator)
        components = _baseline_components(baselines, refs, spectra_by_id)
        source = {"session_id": path.name, "manifest_sha256": _digest(path / "manifest.json", checkpoint),
                  "files": {key: files[key] for key in sorted(required_files)}, "protocol": protocol,
                  "batch": settings.get("batch"), "raw_spectra": len(spectra), "raw_force": len(forces)}
        if compact:
            source.update(recording_schema=compact_data.RECORDING_SCHEMA, raw_spectra=0,
                          optical_source_attestations=len(spectra), peak_frames=len(peak_rows),
                          source_verification="saved_peak_centers_and_optical_acquisition_attestations")
        sources.append(source)
        for baseline_id, reference in refs.items():
            if not drift:
                references_info.append({"session_id": path.name, "baseline_id": baseline_id, **deepcopy(reference),
                                        "force_zero": deepcopy(baselines[baseline_id]["force_zero"]),
                                        "force_noise_axes": deepcopy(baselines[baseline_id].get("force_noise_axes"))})
        force_groups = defaultdict(list)
        for row in forces:
            if protocol == TARGET_PROTOCOL:
                trial = trials.get(row.get("trial_id"), {})
                end = trial.get("data_end_monotonic_ns")
                force_stamp = row.get("host_rx_monotonic_ns")
                if row.get("phase") == "saving" or type(end) is int and type(force_stamp) is int and force_stamp > end:
                    continue
            if not row.get("duplicate_identity") and type(row.get("host_rx_monotonic_ns")) is int:
                force_groups[(row.get("trial_id"), row.get("baseline_id"), row.get("stream_epoch"))].append(row)
        for rows in force_groups.values():
            rows.sort(key=lambda row: row["host_rx_monotonic_ns"])
        trial_rows = defaultdict(list)
        for row in spectra:
            if row.get("trial_id") in accepted_trials:
                trial_rows[row["trial_id"]].append(row)
        for trial_id, rows in trial_rows.items():
            _check(checkpoint)
            trial = accepted_trials[trial_id]
            no_contact_trial = _no_contact(trial)
            trial_criteria = settings.get("no_contact_criteria", {}) if no_contact_trial else criteria
            baseline_id = trial["baseline_id"]
            baseline, reference = baselines[baseline_id], refs[baseline_id]
            drift_reference = _drift_reference(trial, rows, features, reference, trial_criteria,
                                              optical_validator=optical_validator) if drift else None
            if compact:
                drift_reference = compact_data.recorded_reference(trial, drift_reference, peak_rows)
            if drift:
                definition = {key: deepcopy(drift_reference[key]) for key in
                              ("algorithm_version", "config_version", "axis_sha256", "preprocessing")}
                definition["feature_schema"] = "nine_fbg_centers_v1"
                if peak_definition is not None and definition != peak_definition:
                    raise ValueError("九路会话的固定提峰算法或配置定义不一致")
                peak_definition = definition
                references_info.append({"session_id": path.name, **deepcopy(drift_reference),
                                        "force_zero": deepcopy(baseline["force_zero"])})
            group_id = path.name + ":" + components[baseline_id]
            group = groups.setdefault(group_id, {"session_id": path.name, "batch": settings.get("batch"),
                                                "manifest_sha256": source["manifest_sha256"], "point_ids": [], "trial_ids": []})
            if no_contact_trial:
                group["explicit_no_contact"] = True
            if not no_contact_trial and trial.get("point_id") not in group["point_ids"]:
                group["point_ids"].append(trial.get("point_id"))
            group["trial_ids"].append(path.name + ":" + trial_id)
            force_rows = force_groups[(trial_id, baseline_id, baseline.get("force_epoch"))]
            force_times = [row["host_rx_monotonic_ns"] for row in force_rows]
            run = []
            last_stamp = None
            for row_index, row in enumerate(rows):
                if row_index % 128 == 0:
                    _check(checkpoint)
                stamp = row.get("host_rx_monotonic_ns")
                identity = (row.get("stream_epoch"), row.get("frame_id"))
                # Feature acquisition is the actual backend fitted-center stream;
                # unprocessed raw spectra are provenance, not synthetic feature frames.
                if drift and identity not in features:
                    continue
                if protocol == TARGET_PROTOCOL and (row.get("phase") == "saving" or
                        type(trial.get("data_end_monotonic_ns")) is int and type(stamp) is int and
                        stamp > trial["data_end_monotonic_ns"]):
                    # Flush/commit latency is recorded as raw evidence, never
                    # converted into additional trial training observations.
                    rejected["after_trial_data_end"] += 1
                    run, last_stamp = [], None
                    continue
                row_axis = reference["wavelength_nm"] if compact else row.get("wavelength_nm")
                trusted_source = optical_validator(row) if compact else _trusted(row, 512, "intensity")
                usable = (trusted_source and not row.get("duplicate_identity")
                          and row.get("baseline_id") == baseline_id and row.get("stream_epoch") == reference["stream_epoch"]
                          and stamp >= reference["effective_ns"] and row_axis == reference["wavelength_nm"]
                          and (not compact or row.get("axis_sha256") == canonical_digest(row_axis))
                          and (row.get("preprocessing") or {}) == reference["preprocessing"])
                if not usable:
                    rejected["spectrum_source_reference_or_config"] += 1
                    run, last_stamp = [], None
                    continue
                if drift:
                    from .drift_features import drift_pm
                    try:
                        delta = np.asarray(drift_pm(features[identity], drift_reference), dtype=np.float32)
                    except ValueError:
                        rejected["nine_peak_invalid_or_config"] += 1
                        run, last_stamp = [], None
                        continue
                else:
                    delta = np.asarray(row["intensity"], dtype=np.float32) - np.asarray(reference["S0"], dtype=np.float32)
                if axis is None:
                    axis, preprocessing = deepcopy(reference["wavelength_nm"]), deepcopy(reference["preprocessing"])
                    sample_rate = _sample_rate(manifest)
                if axis != row_axis or preprocessing != (row.get("preprocessing") or {}) or _sample_rate(manifest) != sample_rate:
                    raise ValueError("全谱会话的波长轴、曝光预处理或采样设置不一致")
                if last_stamp is not None and not 0 < stamp - last_stamp <= int(options.get("max_spectrum_gap_ms", 120) * 1e6):
                    rejected["spectrum_gap_or_order"] += 1
                    run = []
                last_stamp = stamp
                sync = align_force(row, force_rows, force_times, baseline, chosen_sync_rule)
                if not sync["valid"]:
                    rejected[sync["reason"]] += 1
                values = sync.get("corrected", [0.] * 6)
                norm = math.sqrt(sum(value * value for value in values[:3])) if sync["valid"] else None
                threshold = trial_criteria.get("contact_force_norm_N", .1)
                contact_phases = {"loading", "release"} if protocol == TARGET_PROTOCOL else {"loading", "hold"}
                position_valid = not no_contact_trial and sync["valid"] and row.get("phase") in contact_phases and norm > threshold
                point = trial.get("point_id")
                if not no_contact_trial and (not isinstance(point, str) or len(point) != 3 or point[0] != "P"
                                             or not point[1:].isdigit() or not 1 <= int(point[1:]) <= 25):
                    raise ValueError("全谱试次的固定点位标识无效")
                contact, contact_valid = _contact_supervision(trial, row.get("phase"), sync, trial_criteria, position_valid)
                run.append({"delta": delta,
                            "stamp": stamp, "frame_id": row["frame_id"], "wrench": values, "wrench_valid": sync["valid"],
                            "position": -1 if no_contact_trial else int(point[1:]) - 1,
                            "position_valid": bool(position_valid), "contact": contact, "contact_valid": contact_valid,
                            "phase": row.get("phase"), "sync": sync})
                if len(run) > 16:
                    run.pop(0)
                if len(run) == 16 and (not drift or stamp >= drift_reference["effective_ns"]):
                    last = run[-1]
                    if ((last["position_valid"] and options.get("position_loss_weight", 1) > 0) or
                            (last["contact_valid"] and options.get("contact_loss_weight", 1) > 0) or
                            (last["wrench_valid"] and options.get("wrench_loss_weight", 1) > 0 and
                             (protocol != TARGET_PROTOCOL or last["position_valid"]))):
                        group["optimization_eligible"] = True
                    if last["contact_valid"]:
                        labels = group.setdefault("contact_labels", [])
                        if last["contact"] not in labels:
                            labels.append(last["contact"])
                    sequences.append({"x": np.stack([item["delta"] for item in run]), "wrench": last["wrench"],
                                      "wrench_mask": last["wrench_valid"], "position": last["position"],
                                      "position_mask": last["position_valid"], "contact": last["contact"],
                                      "contact_mask": last["contact_valid"], "group_id": group_id,
                                      "protocol": protocol,
                                      "sample_label": "no_contact" if no_contact_trial else "spatial_press",
                                      "sample_label_version": CONTACT_LABEL_RULE["version"],
                                      "trial_id": path.name + ":" + trial_id, "baseline_id": path.name + ":" + baseline_id,
                                      "reference_id": (drift_reference if drift else reference)["reference_id"], "frame_ids": [item["frame_id"] for item in run],
                                      "stream_epoch": row["stream_epoch"], "stamp": last["stamp"], "phase": last["phase"], "sync": last["sync"]})
        if progress:
            progress({"stage": "sources", "session_index": session_index + 1, "session_count": len(paths),
                      "windows": len(sequences), "rejected": dict(rejected)})
    if not sequences or axis is None:
        raise ValueError("没有可构成 16 个真实连续光谱帧的合格试次")
    if sample_rate is None:
        raise ValueError("会话缺少实际请求的光谱采样设置；不能默认为 25 Hz")
    compatibility = {"physical_definition": physical, "wavelength_nm": axis, "preprocessing": preprocessing,
                     "sample_rate_hz": sample_rate,
                     "reference_rule": "nine_fbg_preload_median_v1" if drift else "spectrum_unloaded_median_v1", "sync_rule": chosen_sync_rule}
    if drift:
        compatibility.update(input_kind="delta_lambda_9", peak_definition=peak_definition)
    if expected_metadata is not None:
        expected = {"physical_definition": expected_metadata["acquisition_provenance"]["physical_definition"],
                    **{key: expected_metadata[key] for key in ("wavelength_nm", "preprocessing", "sample_rate_hz", "reference_rule", "sync_rule")}}
        if drift:
            expected.update(input_kind=expected_metadata["input_kind"], peak_definition=expected_metadata["peak_definition"])
        if compatibility != expected or options.get("max_spectrum_gap_ms", 120) != expected_metadata["window_rule"]["max_gap_ms"]:
            raise ValueError("累计数据与父模型的样件、点阵、波长轴、预处理、采样或窗口同步规则不兼容")
    split = assign_splits(userdata, groups, compatibility, seed=options.get("seed", 1701), inherited_split=inherited_split,
                          new_training_sessions=new_training_sessions)
    assignments = [split["assignments"][row["group_id"]] for row in sequences]
    data = {"x": np.stack([row.pop("x") for row in sequences]).astype(np.float32),
            "wrench": np.asarray([row["wrench"] for row in sequences], dtype=np.float32),
            "position": np.asarray([row["position"] for row in sequences], dtype=np.int64),
            "wrench_mask": np.asarray([row["wrench_mask"] for row in sequences], dtype=bool),
            "position_mask": np.asarray([row["position_mask"] for row in sequences], dtype=bool),
            "contact": np.asarray([row["contact"] for row in sequences], dtype=np.float32),
            "contact_mask": np.asarray([row["contact_mask"] for row in sequences], dtype=bool),
            "contact_label_rule": deepcopy(CONTACT_LABEL_RULE),
            "split_labels": np.asarray(assignments), "windows": sequences, "split": split,
            "wavelength_nm": axis, "preprocessing": preprocessing, "sample_rate_hz": sample_rate,
            "references": references_info, "rejected": dict(rejected), "sync_rule": chosen_sync_rule,
            "acquisition_provenance": {"sources": sources, "physical_definition": physical,
                                       "training_conditions": physical["conditions"], "hardware_delay": "uncalibrated"}}
    if drift:
        from .drift_features import FBG_IDS
        data.update(input_kind="delta_lambda_9", fbg_ids=list(FBG_IDS), peak_definition=peak_definition)
    return data


def training_selection(data):
    """Cap long target-protocol no-contact recordings after immutable grouping.

    Keep every trusted contact target. Per trial, take at most the same number
    of valid no-contact force targets, evenly across stages and source time.
    Evaluation retains the complete natural distribution. Legacy/timed source
    windows retain their existing training behavior.
    """
    train_indices = np.flatnonzero(data["split_labels"] == "train")
    by_trial = defaultdict(list)
    kept = []
    for index in train_indices:
        row = data["windows"][index]
        if row.get("protocol") == TARGET_PROTOCOL:
            by_trial[row["trial_id"]].append(int(index))
        else:
            kept.append(int(index))
    details, explicit_trials = [], {}
    explicit_all = [index for index, row in enumerate(data["windows"]) if row.get("sample_label") == "no_contact"]
    for trial_id, indices in sorted(by_trial.items()):
        if all(data["windows"][index].get("sample_label") == "no_contact" for index in indices):
            negative = [index for index in indices if data["contact_mask"][index] and data["contact"][index] == 0]
            explicit_trials[trial_id] = negative
            details.append({"trial_id": trial_id, "source_windows": len(indices), "contact_windows": 0,
                            "force_only_available": sum(bool(data["wrench_mask"][index]) for index in indices),
                            "force_only_selected": 0, "force_only_by_phase": {},
                            "sample_label": "no_contact", "explicit_negative_available": len(negative),
                            "explicit_negative_windows": 0})
            continue
        contact = [index for index in indices if data["position_mask"][index]]
        stages = defaultdict(list)
        for index in indices:
            if data["wrench_mask"][index] and not data["position_mask"][index]:
                stages[data["windows"][index]["phase"]].append(index)
        allocations = {phase: 0 for phase in stages}
        budget = min(len(contact), sum(len(values) for values in stages.values()))
        while sum(allocations.values()) < budget:
            for phase in sorted(stages):
                if allocations[phase] < len(stages[phase]) and sum(allocations.values()) < budget:
                    allocations[phase] += 1
        selected_force_only = []
        for phase, count in allocations.items():
            if count:
                positions = np.linspace(0, len(stages[phase]) - 1, count).round().astype(int)
                selected_force_only.extend(stages[phase][position] for position in positions)
        kept.extend(contact)
        kept.extend(selected_force_only)
        details.append({"trial_id": trial_id, "source_windows": len(indices), "contact_windows": len(contact),
                        "force_only_available": sum(len(values) for values in stages.values()),
                        "force_only_selected": len(selected_force_only), "force_only_by_phase": allocations})
    positive_budget = int(data["position_mask"][train_indices].sum())
    explicit_available = sum(len(indices) for indices in explicit_trials.values())
    explicit_budget = min(explicit_available, max(positive_budget, 16))
    allocations = {trial_id: 0 for trial_id in explicit_trials}
    while sum(allocations.values()) < explicit_budget:
        for trial_id in sorted(explicit_trials):
            if allocations[trial_id] < len(explicit_trials[trial_id]) and sum(allocations.values()) < explicit_budget:
                allocations[trial_id] += 1
    explicit_selected = []
    for trial_id, count in allocations.items():
        available = explicit_trials[trial_id]
        if count:
            positions = np.linspace(0, len(available) - 1, count).round().astype(int)
            chosen = [available[position] for position in positions]
            kept.extend(chosen)
            explicit_selected.extend(chosen)
            detail = next(row for row in details if row["trial_id"] == trial_id)
            detail.update(force_only_selected=len(chosen), explicit_negative_windows=len(chosen),
                          force_only_by_phase=dict(Counter(data["windows"][index]["phase"] for index in chosen)))
    selected = np.asarray(sorted(kept), dtype=np.int64)
    policy = {"schema": "spectral_train_sampling_v1", "applies_to": TARGET_PROTOCOL,
                      "force_only_to_contact_max_ratio": 1., "method": "per_trial_stage_balanced_uniform_time",
                      "fitted_split": "train", "validation_sampling": "complete_natural_distribution",
                      "source_train_windows": len(train_indices), "selected_train_windows": len(selected),
                      "trials": details}
    if explicit_all:
        policy.update(schema="spectral_train_sampling_v2",
                      method="per_trial_stage_balanced_uniform_time_with_explicit_no_contact",
                      explicit_no_contact_policy="global_contact_budget_trial_balanced_uniform_time",
                      force_only_ratio_scope="spatial_press_trials", explicit_negative_windows=len(explicit_selected),
                      explicit_no_contact_budget=explicit_budget,
                      explicit_no_contact_available_train_count=explicit_available,
                      explicit_no_contact_selected_count=len(explicit_selected),
                      explicit_no_contact_heldout_count=int(sum(data["split_labels"][index] != "train" for index in explicit_all)),
                      explicit_no_contact_note=("显式无接触标签已按训练正类总量限额并按轮次与时间均匀抽取"
                                                if explicit_selected else "固定划分中没有显式无接触训练窗；这些标签未参与本次优化"))
    return selected, policy


def fit_normalization(data, train_indices=None):
    train = data["split_labels"] == "train"
    if train_indices is not None:
        selected = np.zeros(len(train), dtype=bool)
        selected[train_indices] = True
        if np.any(selected & ~train):
            raise ValueError("归一化选择包含非训练集合，拒绝产生泄漏")
        train &= selected
    valid_force = train & data["wrench_mask"]
    if not train.any() or not valid_force.any():
        raise ValueError("固定训练集合缺少有效全谱或六维标签")
    # Each unique training input frame contributes once, even in overlapping windows.
    frames = {}
    for index in np.flatnonzero(train):
        row = data["windows"][index]
        for offset, frame_id in enumerate(row["frame_ids"]):
            frames[(row["trial_id"], row["stream_epoch"], frame_id)] = data["x"][index, offset]
    x = np.asarray(list(frames.values()), dtype=np.float64)
    y = data["wrench"][valid_force].astype(np.float64)
    input_floor = .1 if data.get("input_kind") == "delta_lambda_9" else 1.
    input_mean, input_scale = x.mean(0), np.maximum(x.std(0), input_floor)
    output_mean, output_std = y.mean(0), y.std(0)
    floors = np.asarray([.05, .05, .05, .001, .001, .001])
    output_scale = np.maximum(output_std, floors)
    return {"mean": input_mean.tolist(), "scale": input_scale.tolist(), "fitted_split": "train", "unique_frames": len(frames)}, {
        "mean": output_mean.tolist(), "scale": output_scale.tolist(), "fitted_split": "train",
        "range_min": y.min(0).tolist(), "range_max": y.max(0).tolist(),
        "insufficiently_excited_axes": [axis for axis, std, floor in zip(AXES, output_std, floors) if std < floor]}
