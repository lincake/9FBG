"""Source numeric features, not screenshots; synthetic frames only."""
from copy import deepcopy

import pytest

from ninefbg.drift_features import (FBG_IDS, drift_pm, make_drift_frame,
                                    make_drift_reference, validate_drift_frame,
                                    validate_drift_reference)


def source(number=1, shift=0):
    raw = {"source_kind": "hardware", "time_quality": "host_rx_approx", "frame_id": number,
           "stream_epoch": "optical-A", "host_rx_monotonic_ns": 1_000_000_000 + number*40_000_000,
           "wavelength_nm": [1500+i*.2 for i in range(512)], "intensity": [50]*512,
           "preprocessing": {"pipeline": "raw_device_values"}, "valid": False}
    result = {key: raw[key] for key in ("stream_epoch", "frame_id", "host_rx_monotonic_ns")}
    result.update(algorithm_version="gaussian_background_fixed_identity_1.0.0", config_version="fixed-config",
                  peaks=[{"id": name, "center_nm": 1510+i*2+shift, "valid": True,
                          "stale": False, "value_frame_id": number, "drift_nm": 12345}
                         for i, name in enumerate(FBG_IDS)])
    return raw, result


def samples():
    return [make_drift_frame(*reversed(source(i+1, i*.0001))) for i in range(12)]


def test_centers_are_real_fit_values_not_display_drift_and_raw_valid_flag_is_independent():
    raw, result = source()
    before = deepcopy((raw, result))
    frame = make_drift_frame(result, raw)
    assert frame["all_valid"] and frame["source_valid"]
    assert frame["centers_nm"] == [1510+i*2 for i in range(9)]
    assert "intensity" not in frame and "wavelength_nm" not in frame
    assert "drift_nm" not in frame
    assert validate_drift_frame(frame, raw) is frame
    assert (raw, result) == before


@pytest.mark.parametrize("change", ["old_peak", "invalid_peak", "duplicate_identity", "wrong_source", "bad_axis", "stale_source"])
def test_invalid_peaks_are_null_with_flags_never_filled_with_zero(change):
    raw, result = source()
    if change == "old_peak": result["peaks"][0]["value_frame_id"] = -1
    if change == "invalid_peak": result["peaks"][0]["valid"] = False
    if change == "duplicate_identity": result["peaks"][0]["id"] = "FBG2"
    if change == "wrong_source": result["frame_id"] = -1
    if change == "bad_axis": raw["wavelength_nm"][1] = raw["wavelength_nm"][0]
    if change == "stale_source": raw["stale"] = True
    frame = make_drift_frame(result, raw)
    assert not frame["all_valid"] and frame["centers_nm"][0] is None
    assert not frame["valid_channels"][0]


def test_reference_is_median_fixed_and_preserves_sources_and_units():
    rows = samples(); before = deepcopy(rows)
    reference = make_drift_reference(rows, effective_ns=rows[-1]["host_rx_monotonic_ns"], reference_id="trial-1")
    assert reference["lambda0_nm"][0] == pytest.approx(1510+.00055)
    assert reference["source_frame_ids"] == list(range(1, 13))
    assert reference["source_timestamps_ns"] == [row["host_rx_monotonic_ns"] for row in rows]
    raw, fitted = source(13, .00255); frame = make_drift_frame(fitted, raw)
    assert drift_pm(frame, reference) == pytest.approx([2.0]*9)
    assert rows == before and validate_drift_reference(reference) is reference


@pytest.mark.parametrize("change", ["too_few", "duplicate", "future", "new_config", "invalid_channel"])
def test_reference_rejects_wrong_sources_or_future_data(change):
    rows = samples(); effective = rows[-1]["host_rx_monotonic_ns"]
    if change == "too_few": rows = rows[:11]
    if change == "duplicate": rows[-1] = deepcopy(rows[0])
    if change == "future": effective -= 1
    if change == "new_config": rows[-1]["config_version"] = "new-config"
    if change == "invalid_channel": rows[-1].update(all_valid=False, valid_channels=[False]*9, centers_nm=[None]*9)
    with pytest.raises(ValueError): make_drift_reference(rows, effective_ns=effective, reference_id="trial-1")


def test_reference_changes_invalidate_cross_round_and_raw_provenance():
    rows = samples(); reference = make_drift_reference(rows, effective_ns=rows[-1]["host_rx_monotonic_ns"], reference_id="trial-1")
    raw, fitted = source(13, .00255); frame = make_drift_frame(fitted, raw)
    foreign = deepcopy(frame); foreign["stream_epoch"] = "optical-B"
    with pytest.raises(ValueError): drift_pm(foreign, reference)
    foreign = deepcopy(raw); foreign["host_rx_monotonic_ns"] += 1
    with pytest.raises(ValueError): validate_drift_frame(frame, foreign)
    invalid = deepcopy(frame); invalid["all_valid"] = False
    with pytest.raises(ValueError): validate_drift_frame(invalid)
