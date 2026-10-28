"""Explicit negative labels replay real-shaped immutable synthetic source evidence."""
from copy import deepcopy
import json

import numpy as np
import pytest

from ninefbg.force_target_wrench import CRITERIA
from ninefbg.no_contact import CONTACT_LABEL_VERSION, NO_CONTACT_POINT_ID, NO_CONTACT_QUALITY_VERSION
from ninefbg.no_contact import no_contact_criteria, no_contact_limits
from ninefbg.spectral_data import SPLIT_VERSION, SYNC_RULE, assign_splits, load_dataset, training_selection
from test_spectral_pipeline import _rewrite, spectral_archive, target_protocol_archive
from test_experiment import clock, context, result, sync
from test_no_contact_capture import begin_no_contact, finish_no_contact


def no_contact_archive(root, name="explicit_no_contact", *, trials_count=2):
    """No sensor is opened; every number is a visibly synthetic replay fixture."""
    path = spectral_archive(root, name, trials_count=trials_count)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    manifest["settings"].update(acquisition_protocol="force_target_wrench_v1",
                                force_target_wrench_criteria=deepcopy(CRITERIA),
                                no_contact_criteria=no_contact_criteria(CRITERIA))
    manifest["settings"]["force_target_wrench_criteria"]["sync_rule"] = deepcopy(SYNC_RULE)
    manifest["settings"]["no_contact_criteria"]["sync_rule"] = deepcopy(SYNC_RULE)
    manifest["settings"].pop("timed_wrench_criteria")
    spectra = [json.loads(line) for line in (path / "raw_spectra.jsonl").read_text().splitlines()]
    forces = [json.loads(line) for line in (path / "raw_force.jsonl").read_text().splitlines()]
    for trial, baseline in zip(manifest["final"]["trials"], manifest["final"]["baseline_history"]):
        trial_spectra = [row for row in spectra if row["trial_id"] == trial["trial_id"]]
        start = trial_spectra[51]["host_rx_monotonic_ns"] + 1
        finish = trial_spectra[92]["host_rx_monotonic_ns"] + 1
        end = trial_spectra[-1]["host_rx_monotonic_ns"]
        for index, row in enumerate(trial_spectra):
            row.update(point_id=NO_CONTACT_POINT_ID,
                       phase="preload" if index < 52 else "no_contact" if index <= 92 else "recovery")
        trial_forces = [row for row in forces if row["trial_id"] == trial["trial_id"]]
        for row in trial_forces:
            stamp = row["host_rx_monotonic_ns"]
            row.update(point_id=NO_CONTACT_POINT_ID,
                       phase="preload" if stamp < start else "no_contact" if stamp < finish else "recovery",
                       force_values=[zero + delta for zero, delta in zip(baseline["force_zero"],
                                      [.01, -.01, .005, .0001, -.0001, .0002])])
        first_spectrum = next(row for row in trial_spectra if row["host_rx_monotonic_ns"] > finish)
        first_force = next(row for row in trial_forces if row["host_rx_monotonic_ns"] > finish)
        evidence = {"schema": "manual_finish_unloaded_assertion_v1", "trigger": "manual_unloaded_confirmation",
                    "operator_confirmed": True, "automatic_unloaded_recovery_verified": False,
                    "selection": "first_trusted_source_frame_strictly_after_finish_boundary",
                    "previous_baseline_id": baseline["baseline_id"],
                    "finish_requested_monotonic_ns": finish, "zero_sampling_monotonic_ns": finish,
                    "force_frame_ids": [first_force["frame_id"]],
                    "force_source_stamps_ns": [first_force["host_rx_monotonic_ns"]],
                    "spectrum_frame_ids": [first_spectrum["frame_id"]],
                    "spectrum_source_stamps_ns": [first_spectrum["host_rx_monotonic_ns"]]}
        norm_limit, axis_limits = no_contact_limits(manifest["settings"]["no_contact_criteria"], baseline)
        metrics = {"operator_confirmed": True, "preload_duration_s": 2,
                   "preload_recorded_duration_s": 2.04, "preload_contact_detected": False,
                   "recording_started_monotonic_ns": start, "press_prompt_monotonic_ns": start,
                   "finish_requested_monotonic_ns": finish, "manual_finish_unloaded_confirmed": True,
                   "no_contact_recorded_duration_s": (finish - start) / 1e9, "no_contact_raw_spectra": 41,
                   "automatic_unloaded_recovery_required": False, "physical_accuracy_qualified": False,
                   "unloaded_force_norm_limit_N": norm_limit, "unloaded_axis_limits": axis_limits,
                   "unloaded_evidence": evidence}
        trial.update(point_id=NO_CONTACT_POINT_ID, sample_label="no_contact",
                     sample_label_version=CONTACT_LABEL_VERSION, operator_confirmed=True,
                     criteria_version=NO_CONTACT_QUALITY_VERSION, recording_started_monotonic_ns=start,
                     main_start_monotonic_ns=start, main_end_monotonic_ns=finish,
                     finish_requested_monotonic_ns=finish, zero_sampling_monotonic_ns=finish,
                     data_end_monotonic_ns=end,
                     quality={"accepted": True, "reasons": [], "criteria_version": NO_CONTACT_QUALITY_VERSION,
                              "metrics": metrics})
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _rewrite(path, "raw_spectra.jsonl", spectra)
    _rewrite(path, "raw_force.jsonl", forces)
    return path


def test_no_contact_dataset_is_nonspatial_keeps_real_wrench_and_negative_optimization(tmp_path):
    path = no_contact_archive(tmp_path)
    data = load_dataset(tmp_path, [path.name])
    assert data["contact"].dtype == np.float32 and data["contact_mask"].dtype == bool
    assert not data["position_mask"].any() and np.all(data["position"] == -1)
    assert data["contact_mask"].any() and not data["contact"][data["contact_mask"]].any()
    np.testing.assert_allclose(data["wrench"][data["wrench_mask"]][:, :3], [[.01, -.01, .005]] *
                               int(data["wrench_mask"].sum()), atol=1e-7)
    assert set(data["split_labels"]) == {"train", "validation"}
    assert all(not points for points in data["split"]["group_points"].values())
    selected, policy = training_selection(data)
    assert len(selected) > 0 and np.all(data["contact_mask"][selected])
    assert policy["schema"] == "spectral_train_sampling_v2" and policy["explicit_negative_windows"] == len(selected)


def test_single_nonspatial_group_has_train_split_and_merges_with_spatial_sources(tmp_path):
    negative = no_contact_archive(tmp_path, trials_count=1)
    data = load_dataset(tmp_path, [negative.name])
    assert set(data["split_labels"]) == {"train"}
    press = target_protocol_archive(tmp_path)
    merged = load_dataset(tmp_path, [negative.name, press.name])
    assert merged["position_mask"].any()
    assert set(merged["contact"][merged["contact_mask"]]) == {0., 1.}
    negative_rows = np.asarray([row["sample_label"] == "no_contact" for row in merged["windows"]])
    assert not merged["position_mask"][negative_rows].any()
    assert all(value == "train" for value in merged["split_labels"][negative_rows])


@pytest.mark.parametrize("mutation", ["operator", "version", "force", "moment", "limits", "boundary", "source_count"])
def test_no_contact_label_rejects_false_or_incomplete_original_evidence(tmp_path, mutation):
    path = no_contact_archive(tmp_path)
    manifest = json.loads((path / "manifest.json").read_text())
    trial = manifest["final"]["trials"][0]
    if mutation in {"force", "moment"}:
        rows = [json.loads(line) for line in (path / "raw_force.jsonl").read_text().splitlines()]
        selected = next(row for row in rows if row["trial_id"] == trial["trial_id"] and row["phase"] == "no_contact")
        selected["force_values"][0 if mutation == "force" else 3] += 1.
        _rewrite(path, "raw_force.jsonl", rows)
    else:
        if mutation == "operator":
            trial["operator_confirmed"] = False
        elif mutation == "version":
            trial["sample_label_version"] = "unversioned"
        elif mutation == "limits":
            trial["quality"]["metrics"]["unloaded_force_norm_limit_N"] *= 2
        elif mutation == "boundary":
            trial["main_start_monotonic_ns"] += 1
        else:
            trial["quality"]["metrics"]["no_contact_raw_spectra"] = 15
        (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="无接触"):
        load_dataset(tmp_path, [path.name])


def test_normal_trial_low_force_stages_have_no_position_target_and_uncertain_labels_are_masked(tmp_path):
    path = spectral_archive(tmp_path)
    data = load_dataset(tmp_path, [path.name])
    assert np.all(data["contact"][data["position_mask"]] == 1)
    assert np.all(data["contact_mask"][data["position_mask"]])
    negatives = data["contact_mask"] & (data["contact"] == 0)
    assert negatives.any() and not data["position_mask"][negatives].any()
    assert np.all(np.linalg.norm(data["wrench"][negatives, :3], axis=1) <= .05)
    assert not data["contact_mask"][~data["wrench_mask"]].any()


@pytest.mark.parametrize("capture_mode", ["full_spectrum", "peak_drift"])
def test_recorder_no_contact_replays_fixed_reference_source_frames_and_no_spatial_identity(tmp_path, clock, capture_mode):
    manager, tick = begin_no_contact(tmp_path, clock, capture_mode=capture_mode)
    try:
        for _ in range(30):
            tick()
        if capture_mode == "full_spectrum":
            # A live full-spectrum pipeline publishes a result envelope too.
            manager.on_result(result(clock.now, 192), sync(192, 192, [0, 0, -1, 0, 0, 0]))
        finish_no_contact(manager, tick, clock)
        path = manager.writer.path
        manager.stop()
        data = load_dataset(tmp_path, [path.name], {"method": "drift_gru" if capture_mode == "peak_drift" else "spectral_gru"})
        assert data["x"].shape[1:] == (16, 9 if capture_mode == "peak_drift" else 512)
        assert np.all(data["position"] == -1) and not data["position_mask"].any()
        assert data["contact_mask"].any() and not data["contact"][data["contact_mask"]].any()
        selected, policy = training_selection(data)
        assert len(selected) == 16 and policy["explicit_no_contact_selected_count"] == 16
        assert policy["explicit_no_contact_available_train_count"] > 16
        assert all(row["sample_label"] == "no_contact" for row in data["windows"])
    finally:
        manager.stop()


def test_explicit_negative_sampling_caps_long_trial_and_balances_trials_without_touching_holdout():
    windows = [{"trial_id": "press", "protocol": "force_target_wrench_v1", "phase": "loading",
                "sample_label": "spatial_press"} for _ in range(40)]
    windows += [{"trial_id": tid, "protocol": "force_target_wrench_v1", "phase": "no_contact", "sample_label": "no_contact"}
                for tid, count in (("short", 100), ("long", 5000), ("heldout", 500)) for _ in range(count)]
    count = len(windows)
    data = {"windows": windows, "split_labels": np.asarray(["train"] * (count-500) + ["validation"] * 500),
            "position_mask": np.asarray([True] * 40 + [False] * (count-40)),
            "wrench_mask": np.ones(count, dtype=bool), "contact_mask": np.ones(count, dtype=bool),
            "contact": np.asarray([1.] * 40 + [0.] * (count-40), dtype=np.float32)}
    selected, policy = training_selection(data)
    assert len(selected) == 80 and policy["explicit_no_contact_selected_count"] == 40
    assert policy["explicit_no_contact_heldout_count"] == 500
    explicit = [data["windows"][index]["trial_id"] for index in selected if index >= 40]
    assert explicit.count("short") == explicit.count("long") == 20
    assert "heldout" not in explicit
    assert data["windows"][selected[-1]]["trial_id"] == "long"  # Uniform sampling reaches the long trial's end.


def test_new_explicit_no_contact_source_enters_train_after_press_train_validation_are_fixed(tmp_path):
    press = target_protocol_archive(tmp_path)
    original = load_dataset(tmp_path, [press.name])
    assert set(original["split_labels"]) == {"train", "validation"}
    train = original["split_labels"] == "train"
    assert set(original["contact"][train & original["contact_mask"]]) == {0., 1.}
    negative = no_contact_archive(tmp_path, "new_explicit_no_contact", trials_count=1)
    merged = load_dataset(tmp_path, [press.name, negative.name])
    assert all(merged["split"]["assignments"][key] == value
               for key, value in original["split"]["assignments"].items())
    selected, policy = training_selection(merged)
    assert policy["explicit_no_contact_selected_count"] > 0
    assert any(merged["windows"][index]["sample_label"] == "no_contact" for index in selected)


def test_explicit_no_contact_fixed_validation_source_is_never_reassigned_to_train(tmp_path):
    groups = {
        "press:B1": {"session_id": "press", "batch": "A", "manifest_sha256": "press-source",
                     "point_ids": ["P01"], "trial_ids": ["press:T1"], "contact_labels": [0., 1.],
                     "optimization_eligible": True},
        "negative:B1": {"session_id": "negative", "batch": "A", "manifest_sha256": "negative-source",
                        "point_ids": [], "trial_ids": ["negative:T1"], "contact_labels": [0.],
                        "optimization_eligible": True, "explicit_no_contact": True},
    }
    inherited = {"version": SPLIT_VERSION, "seed": 1701, "compatibility": {}, "group_mode": "baseline_component",
                 "assignments": {"press:B1": "train", "negative:B1": "validation"},
                 "trial_assignments": {"press:T1": "train", "negative:T1": "validation"},
                 "source_manifests": {"press": "press-source", "negative": "negative-source"},
                 "group_points": {"press:B1": ["P01"], "negative:B1": []}}
    split = assign_splits(tmp_path, groups, {}, inherited_split=inherited)
    assert split["assignments"] == inherited["assignments"]
    windows = [{"trial_id": "press:T1", "protocol": "force_target_wrench_v1", "phase": "loading",
                "sample_label": "spatial_press"},
               {"trial_id": "negative:T1", "protocol": "force_target_wrench_v1", "phase": "no_contact",
                "sample_label": "no_contact"}]
    selected, policy = training_selection({"windows": windows, "split_labels": np.asarray(["train", "validation"]),
                                          "position_mask": np.asarray([True, False]), "wrench_mask": np.ones(2, bool),
                                          "contact_mask": np.ones(2, bool), "contact": np.asarray([1., 0.], np.float32)})
    assert list(selected) == [0]
    assert policy["explicit_no_contact_selected_count"] == 0
    assert policy["explicit_no_contact_heldout_count"] == 1
    assert "未参与本次优化" in policy["explicit_no_contact_note"]
