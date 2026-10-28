"""Compact, source-attested peak-drift recording, independent of plotting."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

RECORDING_SCHEMA = "nine_fbg_peak_drift_v1"
OPTICAL_SCHEMA = "nine_fbg_optical_source_v1"
CAPTURE_MODES = {"full_spectrum", "peak_drift"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def compact(settings):
    return isinstance(settings, dict) and settings.get("capture_mode") == "peak_drift"


def _vector(value, count):
    return (isinstance(value, (list, tuple)) and len(value) == count
            and all(type(item) in (int, float) and math.isfinite(item) for item in value))


def static_axis(context):
    axis = (context.get("frame") or {}).get("wavelength_nm")
    if not _vector(axis, 512) or any(a >= b for a, b in zip(axis, axis[1:])):
        return None
    return list(axis)


def project(value):
    """Keep numeric peaks/force provenance while dropping optical arrays.

    This is applied at the writer boundary as well as the capture boundary,
    including transition events and the final baseline history. The immutable
    calibration axis is stored once in manifest.static_wavelength_nm instead.
    """
    removed = {"intensity", "S0", "wavelength_nm", "static_wavelength_nm", "raw_spectrum",
               "fit_x", "fit_y", "fit_x_nm", "fit_y_counts", "fitted_x",
               "fitted_y", "fitted_intensity", "noise_mad_scaled", "original_spectrum"}
    if isinstance(value, dict):
        result = {}
        axis = value.get("wavelength_nm")
        if _vector(axis, 512):
            result["axis_sha256"] = digest(list(axis))
        for key, item in value.items():
            if key in removed:
                continue
            # Future renderer fields cannot accidentally reintroduce a full
            # optical vector to this compact recording format.
            identity_list = (key.endswith("frame_ids") or key.endswith("stamps_ns")
                             or key.endswith("timestamps_ns") or key in {"frame_ids", "trial_ids"})
            if (isinstance(item, (list, tuple)) and len(item) == 512 and not identity_list
                    and all(type(element) in (int, float) for element in item)):
                continue
            result[key] = project(item)
        return result
    if isinstance(value, (list, tuple)):
        return [project(item) for item in value]
    if hasattr(value, "tolist"):
        return project(value.tolist())
    return deepcopy(value)


def optical_source(frame, annotation):
    axis = frame.get("wavelength_nm")
    intensity = frame.get("intensity")
    axis_ok = _vector(axis, 512) and all(a < b for a, b in zip(axis, axis[1:]))
    intensity_ok = _vector(intensity, 512)
    trusted = (axis_ok and intensity_ok and frame.get("source_kind") == "hardware"
               and frame.get("stale") is not True and bool(frame.get("stream_epoch"))
               and type(frame.get("frame_id")) in (int, str) and frame.get("frame_id") != ""
               and type(frame.get("host_rx_monotonic_ns")) is int and frame["host_rx_monotonic_ns"] > 0
               and frame.get("time_quality") in {"host_rx_approx", "hardware_mapped"})
    return {"schema": OPTICAL_SCHEMA,
            **{key: deepcopy(frame.get(key)) for key in ("frame_id", "stream_epoch",
               "host_rx_monotonic_ns", "source_kind", "time_quality", "valid")},
            "stale": frame.get("stale") is True, "source_valid": bool(trusted),
            "axis_sha256": digest(list(axis)) if axis_ok else None,
            "spectrum_sha256": digest(list(intensity)) if intensity_ok else None,
            "preprocessing": deepcopy(frame.get("preprocessing") or {}),
            "source_phase": frame.get("phase"), **deepcopy(annotation), "experiment": deepcopy(annotation)}


def peak_row(row, reference):
    from .drift_features import drift_pm
    value = {key: deepcopy(item) for key, item in row.items() if key not in {"result", "x_pm"}}
    feature = deepcopy((row.get("result") or {}).get("drift_features") or {})
    value["result"] = {"drift_features": feature}
    value["drift_reference_id"] = reference.get("reference_id") if reference else None
    value["delta_lambda_pm"] = None
    value["input_kind"] = "delta_lambda_9"
    if reference and feature.get("all_valid") is True and feature.get("source_valid") is True:
        try:
            value["delta_lambda_pm"] = drift_pm(feature, reference)
        except ValueError:
            pass  # Invalid channels remain explicitly unavailable, never zero.
    return project(value)
