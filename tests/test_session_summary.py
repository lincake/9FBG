import json
from copy import deepcopy

import pytest

from ninefbg.experiment import default_point_grid, list_sessions


def saved_manifest(tmp_path, session_id="20261010T073858Z_real", *, trials=None, guided=True, **overrides):
    manifest = {
        "complete": True,
        "started_utc": "2026-10-10T07:38:58+00:00",
        "ended_utc": "2026-10-10T07:48:58+00:00",
        "settings": {"finger_id": "6", "guided_acquisition": guided,
                     "repetitions_per_point": 3, "point_grid": default_point_grid()},
        "counts": {"trials_completed": 999, "raw_spectra": 100, "raw_force": 200, "results": 98},
        "final": {"trials": trials or []},
        **overrides,
    }
    path = tmp_path / "calibration_sessions" / session_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def trial(number, point="P01", **overrides):
    return {"trial_id": f"T{number:04d}", "point_id": point, "status": "completed",
            "bad": False, "manual_mark": [],
            "quality": {"accepted": True, "criteria_version": "guided_round_v1"}, **overrides}


def test_four_points_twelve_accepted_are_not_zero_or_full_grid(tmp_path):
    trials = [trial(number, point) for number, point in enumerate(
        ["P01"] * 3 + ["P05"] * 3 + ["P25"] * 3 + ["P21"] * 3, 1)]
    trials += [trial(13, bad=True), trial(14, quality={"accepted": False, "criteria_version": "guided_round_v1"}),
               trial(15, "P05", quality={"accepted": True, "criteria_version": "unknown_future_gate"})]
    path = saved_manifest(tmp_path, trials=trials)
    original = path.read_bytes()
    summary, = list_sessions(tmp_path)
    assert summary["trial_count"] == 15
    assert summary["accepted_trial_count"] == 12 and summary["rejected_trial_count"] == 3
    assert summary["has_training_data"] and summary["complete"]
    assert summary["point_count"] == summary["completed_point_count"] == 4
    assert summary["required_point_count"] == 25 and summary["total_required"] == 75
    assert not summary["acquisition_complete"] and not summary["formal_training_qualified"]
    assert summary["point_summary"] == [
        {"point_id": "P01", "row": 0, "col": 0, "attempts": 5, "accepted": 3},
        {"point_id": "P05", "row": 0, "col": 4, "attempts": 4, "accepted": 3},
        {"point_id": "P21", "row": 4, "col": 0, "attempts": 3, "accepted": 3},
        {"point_id": "P25", "row": 4, "col": 4, "attempts": 3, "accepted": 3},
    ]
    assert summary["started_utc"] == "2026-10-10T07:38:58+00:00"
    assert summary["ended_utc"] == "2026-10-10T07:48:58+00:00"
    assert list_sessions(tmp_path) == [summary] and path.read_bytes() == original


def test_guided_gate_manual_mark_and_unfinished_trials_are_not_accepted(tmp_path):
    rows = [trial(1), trial(2, quality=None), trial(3, manual_mark=["接触滑移"]),
            trial(4, status="aborted"), trial(5, status="preload"),
            trial(6, quality={"accepted": 1, "criteria_version": "guided_round_v1"})]
    saved_manifest(tmp_path, trials=rows)
    summary, = list_sessions(tmp_path)
    assert (summary["trial_count"], summary["accepted_trial_count"], summary["rejected_trial_count"], summary["pending_trial_count"]) == (6, 1, 4, 1)
    assert summary["point_summary"][0]["attempts"] == 6
    assert summary["point_summary"][0]["accepted"] == 1


def test_legacy_candidate_preserves_saved_one_based_coordinates(tmp_path):
    legacy = {"finger_id": "old", "point_grid": [{"id": "P01", "row": 1, "col": 1}]}
    rows = [trial(1, quality=None), trial(2, quality=None, bad=True),
            trial(3, "P02", quality=None, guided=True)]
    path = saved_manifest(tmp_path, trials=rows, guided=False, settings=legacy,
                          formal_training_qualified=True)
    before = path.read_bytes()
    summary, = list_sessions(tmp_path)
    assert summary["accepted_trial_count"] == summary["legacy_candidate_trial_count"] == 1
    assert summary["has_training_data"] and not summary["formal_training_qualified"]
    assert not summary["acquisition_complete"]
    assert summary["point_summary"] == [
        {"point_id": "P01", "row": 1, "col": 1, "attempts": 2, "accepted": 1},
        {"point_id": "P02", "row": None, "col": None, "attempts": 1, "accepted": 0},
    ]
    assert summary["settings"] == legacy and path.read_bytes() == before
    assert summary["data_id"] == "" and "data_id" not in summary["settings"]


def test_summary_data_id_comes_only_from_saved_text_and_preserves_old_diameter(tmp_path):
    settings = {"finger_id": "specimen-99", "data_id": "0007-中文", "conditions": {"tip_diameter_mm": 2}}
    current = saved_manifest(tmp_path, "20261010T000002Z_numbered", settings=settings)
    legacy = saved_manifest(tmp_path, "20261010T000001Z_legacy", settings={"finger_id": "specimen-98", "conditions": {"tip_diameter_mm": 3}})
    original = {path: path.read_bytes() for path in (current, legacy)}
    numbered, old = list_sessions(tmp_path)
    assert numbered["data_id"] == "0007-中文" and numbered["settings"] == settings
    assert old["data_id"] == ""
    assert old["settings"]["conditions"]["tip_diameter_mm"] == 3
    assert "data_id" not in old["settings"]
    assert all(path.read_bytes() == content for path, content in original.items())


def test_explicit_diagnostic_marker_is_narrow_and_zero_trials_are_not_inferred(tmp_path):
    base = {"guided_acquisition": True, "point_grid": default_point_grid()}
    saved_manifest(tmp_path, "20261010T000001Z_empty", settings={**base, "finger_id": "6"})
    saved_manifest(tmp_path, "20261010T000002Z_diag", settings={**base, "finger_id": "诊断0.4.2_25Hz_无载定时验证_不作按压训练"})
    saved_manifest(tmp_path, "20261010T000003Z_unrecognized", settings={**base, "finger_id": "诊断其他样件"})
    saved_manifest(tmp_path, "20261010T000004Z_accepted", trials=[trial(1)], settings={**base, "finger_id": "诊断0.4.2_操作员真实新采集"})
    saved_manifest(tmp_path, "20261010T000005Z_explicit", diagnostic=True)
    summaries = list_sessions(tmp_path)
    assert [row["session_id"] for row in summaries] == sorted([row["session_id"] for row in summaries], reverse=True)
    assert {row["session_id"].split("_")[-1]: row["diagnostic"] for row in summaries} == {
        "empty": False, "diag": True, "unrecognized": False, "accepted": False, "explicit": True}
    assert [row["session_id"] for row in summaries if row["has_training_data"]] == ["20261010T000004Z_accepted"]


def test_saved_completeness_and_full_grid_progress_are_independent(tmp_path):
    trials = [trial(number, point["id"]) for number, point in enumerate(default_point_grid() * 3, 1)]
    saved_manifest(tmp_path, "20261010T000001Z_full", trials=trials)
    saved_manifest(tmp_path, "20261010T000002Z_incomplete", trials=deepcopy(trials), complete=False)
    incomplete, full = list_sessions(tmp_path)
    for row in (incomplete, full):
        assert row["trial_count"] == row["accepted_trial_count"] == 75
        assert row["point_count"] == row["completed_point_count"] == 25
        assert row["acquisition_complete"] and not row["formal_training_qualified"]
    assert full["has_training_data"] and full["complete"]
    assert not incomplete["has_training_data"] and not incomplete["complete"]


@pytest.mark.parametrize("repetitions", [1, 2, 999])
def test_saved_repetition_range_is_respected_without_rewriting_history(tmp_path, repetitions):
    settings = {"finger_id": "6", "guided_acquisition": True,
                "repetitions_per_point": repetitions, "point_grid": default_point_grid()}
    trials = [trial(number, point["id"]) for number, point in enumerate(default_point_grid(), 1)]
    path = saved_manifest(tmp_path, settings=settings, trials=trials)
    original = path.read_bytes()
    summary, = list_sessions(tmp_path)
    assert summary["repetitions_per_point"] == repetitions
    assert summary["total_required"] == 25 * repetitions
    assert summary["accepted_trial_count"] == summary["point_count"] == 25
    assert summary["completed_point_count"] == (25 if repetitions == 1 else 0)
    assert summary["acquisition_complete"] is (repetitions == 1)
    assert summary["has_training_data"] and not summary["formal_training_qualified"]
    assert summary["settings"] == settings and path.read_bytes() == original


@pytest.mark.parametrize("repetitions", [None, 0, -1, 1000, True, 1.0, "1"])
def test_invalid_saved_repetitions_keep_default_fallback(tmp_path, repetitions):
    settings = {"guided_acquisition": True, "point_grid": default_point_grid()}
    if repetitions is not None:
        settings["repetitions_per_point"] = repetitions
    path = saved_manifest(tmp_path, settings=settings, trials=[trial(1)])
    original = path.read_bytes()
    summary, = list_sessions(tmp_path)
    assert summary["repetitions_per_point"] == 3 and summary["total_required"] == 75
    assert summary["completed_point_count"] == 0 and not summary["acquisition_complete"]
    assert path.read_bytes() == original


def test_listing_skips_unfinished_and_invalid_manifest_without_creating_storage(tmp_path):
    assert list_sessions(tmp_path) == [] and not (tmp_path / "calibration_sessions").exists()
    unfinished = tmp_path / "calibration_sessions/unfinished"
    unfinished.mkdir(parents=True)
    invalid = tmp_path / "calibration_sessions/invalid"
    invalid.mkdir()
    (invalid / "manifest.json").write_text("{", encoding="utf-8")
    assert list_sessions(tmp_path) == []
