"""Nine fitted peak centers and explicit fixed unloaded references.

These helpers do no fitting, device access, plotting or implicit zero updates.
The values must come from the existing fixed-identity peak algorithm.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import statistics

METHOD = "drift_gru"
INPUT_KIND = "delta_lambda_9"
FRAME_SCHEMA = "nine_fbg_centers_v1"
REFERENCE_RULE = "nine_fbg_preload_median_v1"
WINDOW_RULE = "nine_fbg_causal_windows_v1"
FBG_IDS = [f"FBG{i}" for i in range(1, 10)]
MINIMUM_REFERENCE_FRAMES = 12


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _vector(values, size):
    return isinstance(values, (list, tuple)) and len(values) == size and all(_finite(v) for v in values)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def make_drift_frame(result, raw_frame):
    """Record fitted centers, never the display's cross-round drift/reference.

    Unavailable peaks retain nulls and validity flags, rather than zero values.
    The raw source identity must match the fitted result's identity exactly.
    """
    raw = raw_frame if isinstance(raw_frame, dict) else {}
    result = result if isinstance(result, dict) else {}
    axis = raw.get("wavelength_nm")
    axis_ok = _vector(axis, 512) and all(a < b for a, b in zip(axis, axis[1:]))
    source_ok = (raw.get("source_kind") == "hardware"
                 and raw.get("stale") is not True and bool(raw.get("stream_epoch"))
                 and type(raw.get("frame_id")) in (str, int) and raw.get("frame_id") != ""
                 and type(raw.get("host_rx_monotonic_ns")) is int and raw["host_rx_monotonic_ns"] > 0
                 and raw.get("time_quality") in {"host_rx_approx", "hardware_mapped"}
                 and _vector(raw.get("intensity"), 512) and axis_ok)
    matched = all(result.get(key) == raw.get(key) for key in
                  ("frame_id", "stream_epoch", "host_rx_monotonic_ns"))
    rows = result.get("peaks") if isinstance(result.get("peaks"), list) else []
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    identities_ok = len(ids) == 9 and len(set(ids)) == 9 and set(ids) == set(FBG_IDS)
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    centers, valid, reasons = [], [], []
    for fbg in FBG_IDS:
        peak = by_id.get(fbg, {})
        center = peak.get("center_nm")
        usable = bool(source_ok and matched and identities_ok and peak.get("valid") is True
                      and peak.get("stale") is not True and peak.get("value_frame_id") == raw.get("frame_id")
                      and _finite(center) and axis[0] <= center <= axis[-1])
        centers.append(float(center) if usable else None)
        valid.append(usable)
        reasons.append(None if usable else str(peak.get("failure_reason") or "本源帧九峰无有效测量"))
    return {"schema": FRAME_SCHEMA, "fbg_ids": list(FBG_IDS), "centers_nm": centers,
            "valid_channels": valid, "all_valid": all(valid), "invalid_reasons": reasons,
            "source_kind": raw.get("source_kind"), "time_quality": raw.get("time_quality"),
            "stream_epoch": raw.get("stream_epoch"), "frame_id": raw.get("frame_id"),
            "host_rx_monotonic_ns": raw.get("host_rx_monotonic_ns"),
            "algorithm_version": result.get("algorithm_version"), "config_version": result.get("config_version"),
            "axis_sha256": _digest(list(axis)) if axis_ok else None,
            "preprocessing": deepcopy(raw.get("preprocessing") or {}),
            "units": "nm", "derived_from": "fixed_identity_fitted_peak_centers",
            "source_valid": bool(source_ok and matched and identities_ok)}


def validate_drift_frame(frame, raw_frame=None):
    """Validate the numeric record and optional immutable original source."""
    if (not isinstance(frame, dict) or frame.get("schema") != FRAME_SCHEMA
            or frame.get("fbg_ids") != FBG_IDS or frame.get("units") != "nm"
            or frame.get("derived_from") != "fixed_identity_fitted_peak_centers"
            or not isinstance(frame.get("centers_nm"), list) or len(frame["centers_nm"]) != 9
            or not isinstance(frame.get("valid_channels"), list) or len(frame["valid_channels"]) != 9
            or any(type(value) is not bool for value in frame["valid_channels"])
            or type(frame.get("all_valid")) is not bool
            or frame["all_valid"] != all(frame["valid_channels"])
            or any(not _finite(value) if valid else value is not None
                   for value, valid in zip(frame["centers_nm"], frame["valid_channels"]))):
        raise ValueError("九路峰心数值、固定编号或质量标记无效")
    if (not frame.get("stream_epoch") or type(frame.get("frame_id")) not in (str, int) or frame.get("frame_id") == ""
            or type(frame.get("host_rx_monotonic_ns")) is not int or frame["host_rx_monotonic_ns"] <= 0
            or frame.get("source_kind") != "hardware"
            or frame.get("time_quality") not in {"host_rx_approx", "hardware_mapped"}
            or not isinstance(frame.get("algorithm_version"), str) or not frame["algorithm_version"]
            or not isinstance(frame.get("config_version"), str) or not frame["config_version"]
            or not isinstance(frame.get("axis_sha256"), str) or len(frame["axis_sha256"]) != 64
            or not isinstance(frame.get("preprocessing"), dict)):
        raise ValueError("九路峰心缺少真实源帧、算法或采样配置来源")
    if raw_frame is not None:
        axis = raw_frame.get("wavelength_nm")
        if (any(frame.get(key) != raw_frame.get(key) for key in
                ("stream_epoch", "frame_id", "host_rx_monotonic_ns", "source_kind", "time_quality"))
                or not _vector(axis, 512) or not all(a < b for a, b in zip(axis, axis[1:]))
                or not _vector(raw_frame.get("intensity"), 512) or raw_frame.get("stale") is True
                or frame["axis_sha256"] != _digest(list(axis))
                or frame["preprocessing"] != (raw_frame.get("preprocessing") or {})):
            raise ValueError("九路峰心与原始光谱身份、时间、真实波长轴或配置不一致")
    return frame


def make_drift_reference(samples, *, effective_ns, reference_id, trigger="operator_unloaded_confirmation",
                         wavelength_nm=None, **annotations):
    """Median of explicitly selected valid unloaded source frames; no future data."""
    samples = list(samples)
    if len(samples) < MINIMUM_REFERENCE_FRAMES:
        raise ValueError("九路无载参考至少需要 12 个有效九峰源帧")
    for sample in samples:
        validate_drift_frame(sample)
        if not sample["all_valid"] or sample.get("source_valid") is not True:
            raise ValueError("九路无载参考不能使用失峰、旧值或不可信源帧")
    first = samples[0]
    binding = ("stream_epoch", "algorithm_version", "config_version", "axis_sha256", "preprocessing")
    identities = [(sample["stream_epoch"], sample["frame_id"]) for sample in samples]
    stamps = [sample["host_rx_monotonic_ns"] for sample in samples]
    if (len(set(identities)) != len(identities) or any(a >= b for a, b in zip(stamps, stamps[1:]))
            or any(any(sample[key] != first[key] for key in binding) for sample in samples)
            or type(effective_ns) is not int or effective_ns < stamps[-1]
            or not isinstance(reference_id, str) or not reference_id):
        raise ValueError("九路无载参考源帧身份、时间、编号或配置不一致")
    reference = {"schema": REFERENCE_RULE, "reference_rule": REFERENCE_RULE,
                 "reference_id": reference_id, "lambda0_nm": [statistics.median(row["centers_nm"][i] for row in samples) for i in range(9)],
                 "fbg_ids": list(FBG_IDS), "units": "nm", "effective_ns": effective_ns,
                 "source_frame_ids": [sample["frame_id"] for sample in samples],
                 "source_timestamps_ns": stamps, "source_stamps_ns": list(stamps),
                 "trigger": trigger, "unloaded_confirmed": True,
                 **{key: deepcopy(first[key]) for key in binding}}
    if wavelength_nm is not None:
        if not _vector(wavelength_nm, 512) or _digest(list(wavelength_nm)) != first["axis_sha256"]:
            raise ValueError("九路参考的设备实际波长轴与源帧不一致")
        reference["wavelength_nm"] = list(wavelength_nm)
    reserved = set(reference).intersection(annotations)
    if reserved:
        raise ValueError("九路参考的保留来源字段不能覆盖")
    reference.update(deepcopy(annotations))
    return reference


def validate_drift_reference(reference):
    if (not isinstance(reference, dict) or reference.get("schema") != REFERENCE_RULE
            or reference.get("reference_rule") != REFERENCE_RULE or reference.get("fbg_ids") != FBG_IDS
            or not _vector(reference.get("lambda0_nm"), 9) or reference.get("units") != "nm"
            or reference.get("unloaded_confirmed") is not True or not reference.get("reference_id")
            or type(reference.get("effective_ns")) is not int or not reference.get("stream_epoch")
            or not isinstance(reference.get("algorithm_version"), str) or not reference.get("algorithm_version")
            or not isinstance(reference.get("config_version"), str) or not reference.get("config_version")
            or not isinstance(reference.get("axis_sha256"), str) or len(reference["axis_sha256"]) != 64
            or not isinstance(reference.get("preprocessing"), dict)):
        raise ValueError("九路固定无载参考缺少数值或可信来源")
    ids, stamps = reference.get("source_frame_ids"), reference.get("source_timestamps_ns")
    if (not isinstance(ids, list) or len(ids) < MINIMUM_REFERENCE_FRAMES
            or any(type(value) not in (str, int) or value == "" for value in ids) or len(set(ids)) != len(ids)
            or not isinstance(stamps, list) or len(stamps) != len(ids)
            or any(type(stamp) is not int or stamp <= 0 for stamp in stamps)
            or any(a >= b for a, b in zip(stamps, stamps[1:])) or stamps[-1] > reference["effective_ns"]):
        raise ValueError("九路固定无载参考缺少唯一真实源帧及时间")
    return reference


def drift_pm(frame, reference):
    validate_drift_frame(frame)
    validate_drift_reference(reference)
    if (not frame["all_valid"] or frame.get("source_valid") is not True
            or any(frame[key] != reference[key] for key in
                   ("stream_epoch", "algorithm_version", "config_version", "axis_sha256", "preprocessing"))):
        raise ValueError("九路漂移源帧失效，或与本轮固定参考不兼容")
    return [(center-zero)*1000 for center, zero in zip(frame["centers_nm"], reference["lambda0_nm"])]
