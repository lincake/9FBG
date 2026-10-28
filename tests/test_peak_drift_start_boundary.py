"""In-flight display results cannot contaminate compact capture boundaries."""
from copy import deepcopy
import json

import pytest

from ninefbg.peak_drift_data import features_from_rows
from test_experiment import clock, context, spectrum, sync
from test_force_target_wrench import start_target
from test_drift_capture_integration import fitted
from test_peak_drift_pipeline import compact_archive, read
from test_drift_pipeline import _rewrite
from ninefbg.spectral_data import load_dataset


def orphan_fixture(root, clock):
    path = compact_archive(root, clock, invalid_initial=False)
    sources = read(path, "optical_sources.jsonl")
    rows = read(path, "peak_frames.jsonl")
    source = min(sources, key=lambda row: row["host_rx_monotonic_ns"])
    orphan = deepcopy(rows[0])
    stamp = source["host_rx_monotonic_ns"] - 40_000_000
    orphan.update(frame_id="display-before-session", host_rx_monotonic_ns=stamp,
                  phase="pre_session", eligible=False, trial_id=None, baseline_id=None,
                  point_id=None, selected_point_id=None, valid=False,
                  drift_reference_id=None, delta_lambda_pm=None)
    orphan["result"]["drift_features"].update(frame_id=orphan["frame_id"], host_rx_monotonic_ns=stamp)
    return path, sources, rows, orphan


def test_old_compact_orphan_before_session_is_ignored_without_changing_trial_windows(tmp_path, clock):
    path, sources, rows, orphan = orphan_fixture(tmp_path, clock)
    before = load_dataset(tmp_path, [path.name], {"method": "drift_gru"})
    _rewrite(path, "peak_frames.jsonl", [orphan] + rows)
    # This temporary fixture's original split ledger contains the old source hash.
    from test_drift_pipeline import _new_synthetic_split
    _new_synthetic_split(tmp_path)
    after = load_dataset(tmp_path, [path.name], {"method": "drift_gru"})
    import numpy as np
    np.testing.assert_array_equal(before["x"], after["x"])
    np.testing.assert_array_equal(before["wrench"], after["wrench"])
    assert before["windows"] == after["windows"]
    assert all(orphan["frame_id"] not in row["frame_ids"] for row in after["windows"])


@pytest.mark.parametrize("fault", ["inside_session", "trial", "baseline", "eligible", "valid", "phase", "epoch", "identity"])
def test_missing_sources_remain_rejected_outside_exact_historical_case(tmp_path, clock, fault):
    path, sources, rows, orphan = orphan_fixture(tmp_path, clock)
    feature = orphan["result"]["drift_features"]
    if fault == "inside_session":
        orphan["host_rx_monotonic_ns"] = feature["host_rx_monotonic_ns"] = sources[0]["host_rx_monotonic_ns"] + 1
    elif fault == "trial": orphan["trial_id"] = "T0001"
    elif fault == "baseline": orphan["baseline_id"] = "B0001"
    elif fault == "eligible": orphan["eligible"] = True
    elif fault == "valid": orphan["valid"] = True
    elif fault == "phase": orphan["phase"] = "preload"
    elif fault == "epoch": feature["stream_epoch"] = "unrecorded-epoch"
    elif fault == "identity": orphan["frame_id"] = "mismatched-top-level-id"
    by_id = {(row["stream_epoch"], row["frame_id"]): row for row in sources}
    axis = json.loads((path/"manifest.json").read_text(encoding="utf-8"))["static_wavelength_nm"]
    with pytest.raises(ValueError, match="缺少对应"):
        features_from_rows([orphan], by_id, axis)


def test_new_compact_capture_does_not_write_inflight_pre_session_fit(tmp_path, clock):
    raw = spectrum(clock.now - 40_000_000, 91)
    manager = start_target(tmp_path, clock, capture_mode="peak_drift")
    before = deepcopy(manager.counts)
    try:
        assert manager.source_annotation(raw["host_rx_monotonic_ns"])["phase"] == "pre_session"
        assert manager.on_result(fitted(raw), sync(91, 91, [0, 0, -1, 0, 0, 0])) is False
        assert manager.counts == before
    finally:
        manager.stop()
    assert not (manager.writer.path/"peak_frames.jsonl").exists()
