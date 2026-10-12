"""Real PeakTracker replay on synthetic raw samples; never open devices."""
from copy import deepcopy
import json

import pytest

from ninefbg.experiment import AXES, IDS, UNITS
from ninefbg.reprocess import FEATURE_PROCESSING, reprocess_session
from ninefbg.tracking import PeakTracker, digest
from test_tracking import make_frame


def _write(path, name, rows):
    (path / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _archive(tmp_path):
    tracker = PeakTracker({"stable_frames": 3})
    tracker.request_reference(True)
    for sequence in range(3):
        frame = make_frame(sequence, source_kind="hardware")
        frame["time_quality"] = "host_rx_approx"
        result = tracker.process(frame)
    assert result["state"] == "tracking"
    # Use the actual service-style latest snapshot: reference is a list and
    # anchors live inside windows, rather than an invented serialized fixture.
    audit = deepcopy(result)
    assert "identity_anchor_nm" not in audit
    baseline = {"baseline_id": "B0001", "point_id": "P01", "status": "ready", "ready": True,
                "unloaded_confirmed": True, "used": True,
                "requested_monotonic_ns": 10_020_000_000, "effective_monotonic_ns": 10_030_000_000,
                "lambda0_nm": [value + .02 for value in result["reference_nm"]],
                "force_zero": [0, 0, -1, 0, 0, 0], "force_noise_N": .001,
                "reference_version": 2, "optical_epoch": "epoch-A", "force_epoch": "force-A",
                "algorithm_version": tracker.algorithm_version, "config_version": tracker.config_version}
    manifest = {"kind": "9fbg_calibration_experiment_v1", "session_id": "S-test",
                "settings": {"finger_id": "test-finger", "batch": "A", "force_sign": -1,
                             "force_direction_confirmed": True, "coordinates_confirmed": True, "max_force_N": 3},
                "acquisition_context": {"optical_epoch": "epoch-A", "force_epoch": "force-A",
                                        "reference_version": 1, "algorithm_version": tracker.algorithm_version,
                                        "config_version": tracker.config_version},
                "acquisition_audit": {"tracker": audit, "tracker_config": deepcopy(tracker.config),
                                      "optical_preprocessing": deepcopy(frame["preprocessing"])},
                "final": {"baseline_history": [baseline], "trials": [
                    {"trial_id": "T0001", "point_id": "P01", "baseline_id": "B0001", "status": "completed"}]}}
    raw, forces = [], []
    for sequence, phase in zip(range(3, 9), ("preload", "loading", "hold", "removed", "recovery", "unloaded")):
        annotation = {"session_id": "S-test", "trial_id": "T0001" if phase != "unloaded" else None,
                      "point_id": "P01", "baseline_id": "B0001", "phase": phase,
                      "eligible": True, "manual_mark": []}
        frame = make_frame(sequence, source_kind="hardware", shift={1: .08})
        frame.update(time_quality="host_rx_approx", **annotation, experiment=deepcopy(annotation))
        raw.append(frame)
        forces.append({"frame_id": "force-" + str(sequence), "stream_epoch": "force-A", "source_kind": "hardware",
                       "host_rx_monotonic_ns": frame["host_rx_monotonic_ns"], "time_quality": "host_rx_approx",
                       "force_values": [0, 0, -2, 0, 0, 0], "axes": list(AXES), "units": list(UNITS),
                       **annotation, "experiment": deepcopy(annotation)})
    _write(tmp_path, "raw_spectra.jsonl", raw)
    _write(tmp_path, "raw_force.jsonl", forces)
    _write(tmp_path, "results.jsonl", [{"frame_id": 5, "feature_processing": "online_display_only_fixture"}])
    return manifest, raw, forces


def test_replays_every_raw_frame_with_recorded_ids_baseline_and_immutable_sources(tmp_path):
    manifest, raw, forces = _archive(tmp_path)
    before = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    original = deepcopy((manifest, raw, forces))
    rows = reprocess_session(tmp_path, manifest)
    assert len(rows) == len(raw) == 6  # Five raw frames skipped by the online fixture are recovered.
    assert all(row["valid"] for row in rows), [(row["phase"], row["result"]["message"], row["sync"]) for row in rows]
    assert [row["frame_id"] for row in rows] == [frame["frame_id"] for frame in raw]
    assert [row["host_rx_monotonic_ns"] for row in rows] == [frame["host_rx_monotonic_ns"] for frame in raw]
    for row, frame in zip(rows, raw):
        assert row["feature_processing"] == FEATURE_PROCESSING
        assert [peak["id"] for peak in row["result"]["peaks"]] == list(IDS)
        assert row["result"]["fixed_windows_sha256"] == digest(manifest["acquisition_audit"]["tracker"]["windows"])
        assert "raw_spectrum" not in row["result"]
        assert all("fit_x" not in peak and "fit_y" not in peak for peak in row["result"]["peaks"])
        assert row["reference_version"] == 2
        assert row["x_pm"][1] == pytest.approx(60, abs=.3)
        assert row["force_N"] == 1 and row["force_corrected"] == [0, 0, -1, 0, 0, 0]
        assert row["point_id"] == ("P01" if frame["phase"] in ("loading", "hold") else None)
    assert original == (manifest, raw, forces)
    assert before == {file.name: file.read_bytes() for file in tmp_path.iterdir()}


def test_real_brackets_recover_sync_without_certifying_five_ms(tmp_path):
    manifest, raw, forces = _archive(tmp_path)
    left, right = deepcopy(forces[1]), deepcopy(forces[2])
    left.update(host_rx_monotonic_ns=10_034_000_000, force_values=[0, 0, -2, 0, 0, 0])
    right.update(host_rx_monotonic_ns=10_066_000_000, force_values=[0, 0, -4, 0, 0, 0])
    _write(tmp_path, "raw_force.jsonl", [forces[0], left, right, forces[-1]])
    row = reprocess_session(tmp_path, manifest)[2]  # spectrum at 50 ms
    assert row["valid"] and row["sync"]["method"] == "linear_receive_time"
    assert row["force_N"] == 2
    assert row["sync"]["force_frame_ids"] == [left["frame_id"], right["frame_id"]]
    assert row["sync"]["force_source_stamps_ns"] == [10_034_000_000, 10_066_000_000]
    assert row["sync"]["left_delta_ms"] == row["sync"]["right_delta_ms"] == 16
    assert row["sync"]["nearest_delta_ms"] == 16 and row["sync"]["bracket_ms"] == 32
    assert row["sync"]["target_exceeded"] is True


@pytest.mark.parametrize("change", ["optical_epoch", "force_epoch", "large_gap", "cross_trial", "cross_baseline", "simulation", "stale", "bad_time_quality", "bad_force_axes", "before_baseline", "unconfirmed_baseline", "missing_peak"])
def test_invalid_frames_are_retained_without_fabricating_usable_rows(tmp_path, change):
    manifest, raw, forces = _archive(tmp_path)
    index = 2
    if change == "optical_epoch":
        raw[index]["stream_epoch"] = "epoch-B"
    elif change == "force_epoch":
        forces[index]["stream_epoch"] = "force-B"
        forces = [forces[index]]
    elif change == "large_gap":
        forces = [forces[0], forces[-2]]
        forces[-1]["host_rx_monotonic_ns"] = 10_081_000_000  # 51 ms gap.
    elif change in {"cross_trial", "cross_baseline"}:
        key = "trial_id" if change == "cross_trial" else "baseline_id"
        forces[index]["experiment"][key] = "other"
    elif change == "simulation":
        raw[index]["source_kind"] = "simulation"
    elif change == "stale":
        raw[index]["stale"] = True
    elif change == "bad_time_quality":
        raw[index]["time_quality"] = {"unverified": True}
    elif change == "bad_force_axes":
        forces[index]["axes"] = [["Fx"]] * 6
        forces = [forces[index]]
    elif change == "before_baseline":
        manifest["final"]["baseline_history"][0]["effective_monotonic_ns"] = 10_051_000_000
    elif change == "unconfirmed_baseline":
        manifest["final"]["baseline_history"][0]["unloaded_confirmed"] = False
    elif change == "missing_peak":
        replacement = make_frame(5, missing=3, source_kind="hardware")
        raw[index]["intensity"] = replacement["intensity"]
    _write(tmp_path, "raw_spectra.jsonl", raw)
    _write(tmp_path, "raw_force.jsonl", forces)
    rows = reprocess_session(tmp_path, manifest)
    assert len(rows) == len(raw)
    assert not rows[index]["valid"]
    if change == "optical_epoch":
        assert rows[index + 1]["valid"]  # Bad epoch cannot clear or reinitialize fixed identities.
        assert "时段" in rows[index]["result"]["message"]
    if change == "missing_peak":
        assert rows[index]["result"]["peaks"][3]["stale"] is True
        assert rows[index]["x_pm"] is None


@pytest.mark.parametrize("change", ["audit", "algorithm", "config_hash", "partial_config", "identity", "fit_seed", "axis_hash", "anchors"])
def test_cannot_restore_unsafe_or_missing_algorithm_state(tmp_path, change):
    manifest, _, _ = _archive(tmp_path)
    audit = manifest["acquisition_audit"]
    if change == "audit":
        del manifest["acquisition_audit"]
    elif change == "algorithm":
        audit["tracker"]["algorithm_version"] = "different_algorithm"
    elif change == "config_hash":
        audit["tracker_config"]["maximum_jump_samples"] += 1
    elif change == "partial_config":
        del audit["tracker_config"]["minimum_prominence"]
    elif change == "identity":
        audit["tracker"]["windows"].reverse()
    elif change == "fit_seed":
        del audit["tracker"]["peaks"][2]["sigma_nm"]
    elif change == "axis_hash":
        del audit["tracker"]["axis_diagnostics"]["axis_sha256"]
    elif change == "anchors":
        del audit["tracker"]["windows"][2]["identity_anchor_nm"]
    with pytest.raises(ValueError):
        reprocess_session(tmp_path, manifest)


def test_epoch_time_or_pipeline_failures_do_not_reinitialize_identity(tmp_path):
    manifest, raw, _ = _archive(tmp_path)
    raw[1]["device_sequence"] = 1  # Decrease after replay frame3.
    raw[2]["wavelength_nm"][0] -= .01
    raw[3]["preprocessing"]["pipeline_id"] = "changed"
    raw[4]["host_rx_monotonic_ns"] = raw[0]["host_rx_monotonic_ns"]
    _write(tmp_path, "raw_spectra.jsonl", raw)
    rows = reprocess_session(tmp_path, manifest)
    assert [row["valid"] for row in rows] == [True, False, False, False, False, True]
    assert all(row["result"]["fixed_windows_sha256"] == digest(manifest["acquisition_audit"]["tracker"]["windows"]) for row in rows)


def test_spectra_streaming_bounded_force_window_compact_rows_and_progress(tmp_path, monkeypatch):
    from ninefbg import reprocess

    manifest, original_raw, original_force = _archive(tmp_path)
    raw = []
    for sequence in range(3, 106):
        frame = deepcopy(original_raw[2])
        frame.update(frame_id=sequence, device_sequence=sequence,
                     host_rx_monotonic_ns=10_000_000_000 + sequence * 10_000_000)
        raw.append(frame)
    forces = []
    for sequence in range(1200):
        frame = deepcopy(original_force[2])
        frame.update(frame_id="f" + str(sequence), host_rx_monotonic_ns=10_000_000_000 + sequence * 1_000_000)
        forces.append(frame)
    _write(tmp_path, "raw_spectra.jsonl", raw)
    _write(tmp_path, "raw_force.jsonl", forces)
    manifest["counts"] = {"raw_spectra": 103, "raw_force": 1200}
    yielded, processed, largest = 0, 0, 0
    real_iter, real_process, real_sync = reprocess._iter_rows, reprocess._FrozenTracker.process, reprocess.ForceSynchronizer

    def iter_spy(path, name):
        nonlocal yielded
        for frame in real_iter(path, name):
            if name == "raw_spectra.jsonl":
                # Reading ahead all 512-point raw arrays would fail this check.
                assert yielded == processed
                yielded += 1
            yield frame

    def process_spy(self, frame, baseline):
        nonlocal processed
        result = real_process(self, frame, baseline)
        processed += 1
        return result

    class SpySynchronizer(real_sync):
        def add(self, frame):
            nonlocal largest
            value = super().add(frame)
            largest = max(largest, len(self.frames))
            assert self.frames.maxlen == 256
            return value

    monkeypatch.setattr(reprocess, "_iter_rows", iter_spy)
    monkeypatch.setattr(reprocess._FrozenTracker, "process", process_spy)
    monkeypatch.setattr(reprocess, "ForceSynchronizer", SpySynchronizer)
    progress = []
    rows = reprocess_session(tmp_path, manifest, progress=progress.append)
    assert yielded == processed == len(rows) == 103
    assert all(row["valid"] for row in rows)
    assert largest == 256
    assert progress == [{"processed": 100, "total": 103}, {"processed": 103, "total": 103}]
    assert len(json.dumps(rows, ensure_ascii=False)) < 1_300_000
    assert all("raw_spectrum" not in row["result"] and "windows" not in row["result"] for row in rows)
