"""Synthetic algorithm regressions, never physical accuracy evidence."""
from copy import deepcopy
import json

import numpy as np
import pytest

from ninefbg.inversion import validate_bundle
from ninefbg.pilot import (fit_pilot, load_pilot_model, predict_pilot,
                           save_pilot_model, validate_pilot_model)


def sample_rows(repetitions=3):
    rng = np.random.default_rng(891)
    slopes = {point: rng.normal(size=9)*20 for point in ("P01", "P05", "P21", "P25")}
    origin = np.linspace(-.03, .03, 9)
    rows = []
    for point in slopes:
        for trial in range(repetitions):
            common = {"session_id": "s1", "trial_id": f"{point}-T{trial}",
                      "baseline_id": f"{point}-B{trial}", "valid": True,
                      "source_kind": "synthetic", "algorithm_version": "fixed-test",
                      "config_version": "fixed-config", "split": "A"}
            for _ in range(8):
                rows.append({**common, "phase": "unloaded", "point_id": None,
                             "force_N": 0.0, "x_pm": (origin+rng.normal(0, .005, 9)).tolist()})
            for force in (.25, .5, 1., 1.5, 2., 2.5, 3.):
                rows.append({**common, "phase": "loading", "point_id": point,
                             "force_N": force, "x_pm": (origin+slopes[point]*force+rng.normal(0, .005, 9)).tolist()})
    return rows, slopes, origin


def fit(rows, **options):
    return fit_pilot(rows, {"allow_synthetic_diagnostics": True, **options})


def test_four_points_twelve_trials_are_tested_once_as_whole_groups():
    rows, _, _ = sample_rows()
    before = deepcopy(rows)
    result = fit(rows)
    assert rows == before, "Never relabel or mutate archived batches/trials"
    report, model = result["report"], result["model"]
    assert report["schema"] == "9fbg_pilot_cv_v1"
    assert report["formal_ready"] is model["formal_ready"] is False
    assert report["summary"]["classification_accuracy"] == pytest.approx(1)
    assert report["summary"]["force_mae_N"] < .001
    assert report["summary"]["force_rmse_N"] < .001
    assert report["summary"]["position_error_norm_mean"] == 0
    assert report["summary"]["test_trials"] == 12
    assert report["summary"]["n"] == 84
    assert report["confusion_matrix"]["labels"] == ["P01", "P05", "P21", "P25"]
    assert np.asarray(report["confusion_matrix"]["counts"]).shape == (4, 4)
    assert np.asarray(report["confusion_matrix"]["counts"]).sum() == 84
    assert len(report["by_trial"]) == 12
    assert len(report["out_of_fold_predictions"]) == 84
    held_out = []
    for fold in report["folds"]:
        train, test = set(fold["train_trial_ids"]), set(fold["test_trial_ids"])
        assert len(train) == 8 and len(test) == 4 and not train & test
        assert not set(fold["train_baseline_ids"]) & set(fold["test_baseline_ids"])
        assert fold["fold_model"]["provenance"]["test_rows_used_for_fitting"] == 0
        assert set(fold["fold_model"]["provenance"]["training_trial_ids"]) == train
        assert set(fold["fold_model"]["baseline_context"]["training_baseline_ids"]) == set(fold["train_baseline_ids"])
        held_out.extend(test)
    assert len(held_out) == len(set(held_out)) == 12
    assert report["validation"]["independent_session_validation"] is False
    assert "跨天" in report["interpretation"] and "正式训练" in report["interpretation"]
    assert "毫米" in report["position_error_definition"]
    assert report["point_curves"]["P01"]["trials"][0]["force_N"] == [.25, .5, 1., 1.5, 2., 2.5, 3.]
    json.dumps(result, allow_nan=False)


def test_test_trial_force_and_baseline_noise_cannot_change_its_fold_model():
    rows, _, _ = sample_rows()
    original = fit(rows)
    changed = deepcopy(rows)
    held_out = set(original["report"]["folds"][0]["test_trial_ids"])
    for row in changed:
        if row["trial_id"] in held_out:
            row["x_pm"] = [value*7+200 for value in row["x_pm"]]
            if row["point_id"] is not None:
                row["force_N"] *= 2
    altered = fit(changed)
    first = original["report"]["folds"][0]["fold_model"]
    assert altered["report"]["folds"][0]["fold_model"] == first
    assert altered["report"]["summary"] != original["report"]["summary"]
    assert altered["model"]["source_data_sha256"] != original["model"]["source_data_sha256"]


def test_baseline_only_group_ownership_excludes_test_baseline_statistics():
    rows, _, _ = sample_rows()
    for row in rows:
        if row["point_id"] is None:
            row["trial_id"] = "baseline-only-"+row["baseline_id"]
    original = fit(rows)
    changed = deepcopy(rows)
    held_out_baselines = set(original["report"]["folds"][0]["test_baseline_ids"])
    for row in changed:
        if row["point_id"] is None and row["baseline_id"] in held_out_baselines:
            row["x_pm"] = [1000.0]*9
    altered = fit(changed)
    assert altered["report"]["folds"][0]["fold_model"] == original["report"]["folds"][0]["fold_model"]
    assert original["report"]["quality_summary"]["trial_count"] == 12
    assert len(original["model"]["provenance"]["training_trial_ids"]) == 12


def test_force_truth_is_not_a_prediction_input():
    rows, slopes, origin = sample_rows()
    model = fit(rows)["model"]
    first = predict_pilot(model, (origin+slopes["P21"]*1.7).tolist())
    second = predict_pilot(model, (origin+slopes["P21"]*1.7).tolist(), {"force_N": 9999, "point_id": "P01"})
    assert first == second
    assert first["point_id"] == "P21"
    assert first["force_N"] == pytest.approx(1.7, abs=.002)
    assert any(value < 0 for value in model["templates"][0]["slope_pm_per_N"])
    assert predict_pilot(model, origin.tolist())["status"] == "无接触"


def test_raw_force_is_not_clipped_into_a_fake_valid_three_newton_result():
    rows, slopes, origin = sample_rows()
    model = fit(rows)["model"]
    result = predict_pilot(model, (origin+slopes["P05"]*4.5).tolist())
    assert result["status"] == "超范围"
    assert result["point_id"] is None and result["force_N"] is None
    assert result["raw_force_N"] > 4.4
    assert result["fit_force_N"] <= 3


def test_nearly_identical_templates_show_rejection_in_addition_to_raw_accuracy():
    rows, slopes, origin = sample_rows()
    for row in rows:
        if row["point_id"] == "P05":
            row["x_pm"] = (origin+slopes["P01"]*row["force_N"]).tolist()
    report = fit(rows)["report"]
    assert report["summary"]["classification_accuracy"] < 1
    assert report["summary"]["rejection_rate"] > .2
    assert report["by_point"]["P05"]["position_error_norm_mean"] > 0
    assert sum(report["summary"]["status_counts"].values()) == report["summary"]["n"]


def test_point_trial_force_bin_balance_resists_hold_frame_duplicates():
    rows, _, _ = sample_rows()
    original = fit(rows)
    changed = deepcopy(rows)
    repeated = next(row for row in rows if row["point_id"] == "P01" and row["trial_id"].endswith("T0") and row["force_N"] == 3)
    changed.extend(deepcopy(repeated) for _ in range(100))
    altered = fit(changed)
    assert altered["report"]["summary"]["force_mae_N"] == pytest.approx(original["report"]["summary"]["force_mae_N"], abs=1e-12)
    assert altered["report"]["summary"]["classification_accuracy"] == original["report"]["summary"]["classification_accuracy"]
    assert np.allclose(altered["model"]["templates"][0]["slope_pm_per_N"], original["model"]["templates"][0]["slope_pm_per_N"], atol=1e-12)


@pytest.mark.parametrize("case,match", [
    ("empty", "没有数据"), ("two_trials", "至少需要三个"),
    ("one_point", "至少需要两个"), ("missing_noise", "真实无载"),
    ("shared_baseline", "共用同一基线"), ("changed_baseline", "唯一基线"),
    ("C", "C 组"),
    ("mixed_config", "不能混用"), ("mixed_source", "不能混用"),
    ("multi_point_trial", "多个点位"), ("wrong_noise_owner", "归属不一致"),
])
def test_rejects_insufficient_or_leaky_data(case, match):
    rows, _, _ = sample_rows()
    if case == "empty":
        rows = []
    elif case == "two_trials":
        rows = [row for row in rows if not row["trial_id"].endswith("T2")]
    elif case == "one_point":
        rows = [row for row in rows if row["trial_id"].startswith("P01")]
    elif case == "missing_noise":
        rows = [row for row in rows if not (row["baseline_id"] == "P01-B0" and row["point_id"] is None)]
    elif case == "shared_baseline":
        for row in rows:
            if row["trial_id"] == "P01-T1":
                row["baseline_id"] = "P01-B0"
    elif case == "changed_baseline":
        next(row for row in rows if row["point_id"] == "P01")["baseline_id"] = "other"
    elif case == "C":
        for row in rows:
            row["split"] = "C"
    elif case == "mixed_config":
        rows[-1]["config_version"] = "other"
    elif case == "mixed_source":
        rows[-1]["source_kind"] = "hardware"
    elif case == "multi_point_trial":
        next(row for row in rows if row["point_id"] == "P01")["point_id"] = "P05"
    elif case == "wrong_noise_owner":
        rows[0]["baseline_id"] = "P05-B0"
    with pytest.raises(ValueError, match=match):
        fit(rows)


def test_hardware_only_default_and_no_formal_or_legacy_include_mlp_switch():
    rows, _, _ = sample_rows()
    with pytest.raises(ValueError, match="真实硬件"):
        fit_pilot(rows)
    for option in ({"formal_ready": True}, {"include_mlp": True}, {"noise_floor_pm": 0}, {"reject_margin": 1}):
        with pytest.raises(ValueError):
            fit(rows, **option)


def test_save_load_is_separate_from_formal_bundle_and_detects_tampering(tmp_path):
    rows, _, _ = sample_rows()
    model = fit(rows)["model"]
    path = tmp_path / "pilot.json"
    save_pilot_model(model, path)
    assert load_pilot_model(path) == model
    assert validate_pilot_model(model)
    with pytest.raises(ValueError, match="25 点"):
        validate_bundle(model)
    changed = deepcopy(model)
    changed["templates"][0]["slope_pm_per_N"][0] += 1
    with pytest.raises(ValueError, match="校验失败"):
        validate_pilot_model(changed)
    changed = deepcopy(model)
    changed["formal_ready"] = True
    with pytest.raises(ValueError, match="正式训练门"):
        validate_pilot_model(changed)


def test_invalid_rows_excluded_and_invalid_or_stale_input_rejected():
    rows, _, _ = sample_rows()
    rows.append({**deepcopy(rows[-1]), "valid": False, "x_pm": [999]*9})
    result = fit(rows)
    assert result["report"]["quality_summary"]["invalid_rows"] == 1
    model = result["model"]
    assert predict_pilot(model, [0]*8)["status"] == "无效"
    assert predict_pilot(model, [float("nan")]*9)["status"] == "无效"
    assert predict_pilot(model, [0]*9, {"stale": True})["status"] == "无效"
    assert predict_pilot(model, [0]*9, {"algorithm_version": "changed"})["status"] == "无效"


def test_more_than_three_trials_remain_whole_and_each_tests_once():
    rows, _, _ = sample_rows(4)
    report = fit(rows)["report"]
    held_out = [trial for fold in report["folds"] for trial in fold["test_trial_ids"]]
    assert len(set(held_out)) == len(held_out) == 16
    assert report["summary"]["test_trials"] == 16
