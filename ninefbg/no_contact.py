"""Versioned explicit no-contact labels, separate from the 25 spatial points."""
from __future__ import annotations

from copy import deepcopy
import math

NO_CONTACT_POINT_ID = "NO_CONTACT"
NO_CONTACT_QUALITY_VERSION = "no_contact_quality_v1"
CONTACT_LABEL_VERSION = "contact_label_v1"


def no_contact_criteria(base):
    value = deepcopy(base)
    value.update(criteria_version=NO_CONTACT_QUALITY_VERSION,
                 quality_scope="explicit_no_contact_spectra_and_six_axis_source_frames",
                 target_semantics="no_target_manual_finish", minimum_no_contact_spectra=16,
                 minimum_recording_s=.64)
    return value


def no_contact_limits(criteria, baseline):
    noise = baseline.get("force_noise_axes") or [0.] * 6
    if (not isinstance(noise, (list, tuple)) or len(noise) != 6
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in noise)):
        raise ValueError("无接触标签的六轴噪声参考无效")
    axes = [max(floor, criteria["noise_multiplier"] * value)
            for floor, value in zip(criteria["unloaded_axis_floor"], noise)]
    norm = max(criteria["unloaded_force_norm_floor_N"],
               criteria["noise_multiplier"] * math.sqrt(sum(v * v for v in noise[:3])))
    return norm, axes


def is_no_contact_trial(trial):
    return trial.get("point_id") == NO_CONTACT_POINT_ID or trial.get("sample_label") == "no_contact"


def validate_no_contact_trial(trial, criteria):
    quality = trial.get("quality") or {}
    metrics = quality.get("metrics") or {}
    start, end = metrics.get("recording_started_monotonic_ns"), metrics.get("finish_requested_monotonic_ns")
    if (trial.get("point_id") != NO_CONTACT_POINT_ID or trial.get("sample_label") != "no_contact"
            or trial.get("sample_label_version") != CONTACT_LABEL_VERSION
            or trial.get("operator_confirmed") is not True
            or trial.get("status") != "completed" or trial.get("bad") or trial.get("manual_mark")
            or trial.get("criteria_version") != NO_CONTACT_QUALITY_VERSION
            or criteria.get("criteria_version") != NO_CONTACT_QUALITY_VERSION
            or quality.get("criteria_version") != NO_CONTACT_QUALITY_VERSION
            or quality.get("accepted") is not True or quality.get("reasons")
            or metrics.get("operator_confirmed") is not True
            or metrics.get("preload_duration_s") != 2
            or metrics.get("preload_contact_detected") is not False
            or type(metrics.get("preload_recorded_duration_s")) not in (int, float)
            or metrics["preload_recorded_duration_s"] < 2
            or metrics.get("manual_finish_unloaded_confirmed") is not True
            or type(start) is not int or start <= 0 or type(end) is not int or end <= start
            or trial.get("recording_started_monotonic_ns") != start
            or trial.get("main_start_monotonic_ns") != start
            or trial.get("main_end_monotonic_ns") != end
            or trial.get("finish_requested_monotonic_ns") != end
            or type(metrics.get("no_contact_recorded_duration_s")) not in (int, float)
            or not math.isfinite(metrics["no_contact_recorded_duration_s"])
            or metrics["no_contact_recorded_duration_s"] < criteria.get("minimum_recording_s", .64)
            or type(metrics.get("no_contact_raw_spectra")) is not int
            or metrics["no_contact_raw_spectra"] < criteria.get("minimum_no_contact_spectra", 16)):
        raise ValueError("无接触轮次缺少明确标签、完整采前记录或手动结束凭据")
    return metrics
