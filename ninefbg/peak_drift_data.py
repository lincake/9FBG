"""Validate compact nine-center recordings without reconstructing raw intensity.

The optical rows attest to the trusted acquisition used by the fixed peak
tracker. Their intensity digest is provenance, not a replacement spectrum and
not a claim that the discarded 512 intensity samples can be replayed.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re

from .drift_features import validate_drift_frame, validate_drift_reference, drift_pm
from .spectral_model import canonical_digest

RECORDING_SCHEMA = "nine_fbg_peak_drift_v1"
SOURCE_SCHEMA = "nine_fbg_optical_source_v1"
FILES = {"optical_sources.jsonl", "peak_frames.jsonl", "raw_force.jsonl", "events.jsonl"}


def is_compact(manifest):
    schema = manifest.get("recording_schema")
    mode = manifest.get("settings", {}).get("capture_mode")
    if schema is None and mode != "peak_drift":
        return False
    if (schema != RECORDING_SCHEMA or mode != "peak_drift"
            or manifest.get("settings", {}).get("input_kind") != "delta_lambda_9"):
        raise ValueError("寻峰漂移会话的记录格式与输入模式不一致")
    return True


def axis_for(manifest):
    axis = manifest.get("static_wavelength_nm")
    if (not isinstance(axis, list) or len(axis) != 512
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in axis)
            or not all(a < b for a, b in zip(axis, axis[1:]))):
        raise ValueError("寻峰漂移会话缺少实际设备的固定 512 点标定波长轴")
    return deepcopy(axis)


def trusted_optical(row):
    """Check an acquisition attestation; never fabricate intensity samples."""
    sha = lambda x: isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x) is not None
    return (isinstance(row, dict) and row.get("schema") == SOURCE_SCHEMA
            and "intensity" not in row and "wavelength_nm" not in row
            and row.get("source_kind") == "hardware"
            and row.get("time_quality") in {"host_rx_approx", "hardware_mapped"}
            and type(row.get("host_rx_monotonic_ns")) is int and row["host_rx_monotonic_ns"] > 0
            and type(row.get("frame_id")) in (str, int) and row.get("frame_id") != ""
            and isinstance(row.get("stream_epoch"), str) and bool(row["stream_epoch"])
            and row.get("stale") is not True and row.get("source_valid") is True
            and sha(row.get("axis_sha256")) and sha(row.get("spectrum_sha256"))
            and isinstance(row.get("preprocessing"), dict))


def features_from_rows(rows, sources, axis, checkpoint=None):
    digest = canonical_digest(axis)
    features, duplicates = {}, set()
    first_source_stamps = {}
    for source in sources.values():
        epoch, stamp = source.get("stream_epoch"), source.get("host_rx_monotonic_ns")
        if isinstance(epoch, str) and type(stamp) is int and stamp > 0:
            first_source_stamps[epoch] = min(stamp, first_source_stamps.get(epoch, stamp))
    for index, saved in enumerate(rows):
        if checkpoint and index % 128 == 0:
            checkpoint()
        result = saved.get("result")
        frame = result.get("drift_features") if isinstance(result, dict) else None
        validate_drift_frame(frame)
        identity = (frame["stream_epoch"], frame["frame_id"])
        source = sources.get(identity)
        if source is None:
            # 0.12.0 could publish an already-running display fit after the
            # capture writer opened. It is not part of any trial or reference.
            # Accept only that exact, source-time-bounded historical case.
            first_stamp = first_source_stamps.get(frame["stream_epoch"])
            if (saved.get("phase") == "pre_session" and saved.get("eligible") is False
                    and saved.get("trial_id") is None and saved.get("baseline_id") is None
                    and saved.get("point_id") is None and saved.get("valid") is False
                    and saved.get("frame_id") == frame["frame_id"]
                    and saved.get("host_rx_monotonic_ns") == frame["host_rx_monotonic_ns"]
                    and type(first_stamp) is int and frame["host_rx_monotonic_ns"] < first_stamp):
                continue
            raise ValueError("寻峰漂移数值缺少对应的光学源帧凭据")
        if (source.get("axis_sha256") != digest or frame["axis_sha256"] != digest
                or any(frame.get(key) != source.get(key) for key in
                       ("host_rx_monotonic_ns", "source_kind", "time_quality", "preprocessing"))
                or any(saved.get(key) != source.get(key) for key in ("trial_id", "baseline_id"))
                or frame.get("source_valid") is True and not trusted_optical(source)
                or frame.get("source_valid") is not True and frame.get("all_valid") is True):
            raise ValueError("寻峰漂移数值与源帧时间、标定轴或试次边界不一致")
        if identity in features:
            duplicates.add(identity)
        features[identity] = deepcopy(frame)
    for identity in duplicates:
        features[identity] = None
    return features


def baseline_reference(baseline, sources, axis, force_rows):
    from .spectral_data import _finite_vector, _trusted
    from .spectral_model import AXES, UNITS
    reference = deepcopy(baseline.get("spectral_reference") or {})
    if ("S0" in reference or "S0" in baseline or "wavelength_nm" in reference
            or not _finite_vector(baseline.get("force_zero"), 6)
            or baseline.get("unloaded_confirmed") is not True
            or not isinstance(reference.get("reference_id"), str) or not reference["reference_id"]
            or type(reference.get("effective_ns")) is not int):
        raise ValueError("寻峰漂移基线缺少固定零偏或无载源帧凭据")
    ids, stamps = reference.get("source_frame_ids"), reference.get("source_stamps_ns")
    if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
            or not isinstance(stamps, list) or len(stamps) != len(ids)
            or any(type(t) is not int or t <= 0 for t in stamps)
            or any(a >= b for a, b in zip(stamps, stamps[1:]))):
        raise ValueError("寻峰漂移基线源帧身份或时间无效")
    actual = []
    for identity, stamp in zip(ids, stamps):
        source = sources.get((reference.get("stream_epoch"), identity))
        if (source is None or not trusted_optical(source)
                or source["host_rx_monotonic_ns"] != stamp or stamp > reference["effective_ns"]
                or source.get("phase") not in {"unloaded", "recovery"}
                or source.get("axis_sha256") != canonical_digest(axis)):
            raise ValueError("寻峰漂移基线与光学源帧凭据不一致")
        actual.append(source)
    preprocessing = actual[0]["preprocessing"]
    if (any(row["preprocessing"] != preprocessing for row in actual)
            or reference.get("preprocessing", preprocessing) != preprocessing
            or reference.get("axis_sha256", canonical_digest(axis)) != canonical_digest(axis)):
        raise ValueError("寻峰漂移基线的标定轴或预处理配置不一致")
    force_ids, force_stamps = baseline.get("force_frame_ids"), baseline.get("force_source_stamps_ns")
    direct = baseline.get("zero_semantics") == "operator_unloaded_assertion_first_post_finish_source_frames"
    if (not isinstance(force_ids, list) or len(set(force_ids)) != len(force_ids)
            or (len(force_ids) != 1 if direct else len(force_ids) < 12)
            or not isinstance(force_stamps, list) or len(force_stamps) != len(force_ids)):
        raise ValueError("寻峰漂移六轴零偏缺少真实无载源帧")
    force_sources = []
    for identity, stamp in zip(force_ids, force_stamps):
        selected = [row for row in force_rows if row.get("frame_id") == identity
                    and row.get("stream_epoch") == baseline.get("force_epoch")]
        if (len(selected) != 1 or not _trusted(selected[0], 6, "force_values")
                or selected[0].get("duplicate_identity") or selected[0]["host_rx_monotonic_ns"] != stamp
                or selected[0].get("axes", AXES) != AXES or selected[0].get("units", UNITS) != UNITS
                or type(stamp) is not int or stamp > reference["effective_ns"]):
            raise ValueError("寻峰漂移六轴零偏与真实源帧身份或时间不一致")
        force_sources.append(selected[0]["force_values"])
    import statistics
    expected_zero = [statistics.median(row[i] for row in force_sources) for i in range(6)]
    if any(not math.isclose(x,y,rel_tol=0,abs_tol=1e-9) for x,y in zip(expected_zero,baseline["force_zero"])):
        raise ValueError("寻峰漂移六轴零偏不等于保存的无载源帧中位数")
    reference.update(wavelength_nm=deepcopy(axis), preprocessing=deepcopy(preprocessing),
                     axis_sha256=canonical_digest(axis))
    return reference


def recorded_reference(trial, replayed, peak_rows):
    """Replay the preload median and verify the saved immutable numeric reference."""
    reference = deepcopy(trial.get("drift_reference"))
    validate_drift_reference(reference)
    keys = ("lambda0_nm", "source_frame_ids", "source_timestamps_ns", "effective_ns", "stream_epoch",
            "algorithm_version", "config_version", "axis_sha256", "preprocessing", "fbg_ids", "units")
    if any(reference.get(key) != replayed.get(key) for key in keys):
        raise ValueError("寻峰漂移固定参考不等于本轮真实连续无载九峰中位数")
    for saved in peak_rows:
        if saved.get("trial_id") != trial["trial_id"]:
            continue
        delta = saved.get("delta_lambda_pm")
        if delta is None:
            continue
        if saved.get("drift_reference_id") != reference["reference_id"]:
            raise ValueError("寻峰漂移帧的固定参考身份不一致")
        frame = saved.get("result", {}).get("drift_features")
        expected = drift_pm(frame, reference)
        if (not isinstance(delta, list) or len(delta) != 9 or any(type(x) not in (int, float)
                or not math.isfinite(x) or not math.isclose(x, y, rel_tol=0, abs_tol=1e-8)
                for x, y in zip(delta, expected))):
            raise ValueError("保存的九路漂移与峰心和固定参考不一致")
    return reference
