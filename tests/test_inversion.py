"""Synthetic algorithm checks only; none certify real force/position accuracy."""
from copy import deepcopy
import hashlib
import json

import numpy as np
import pytest

from ninefbg.inversion import (POINT_IDS, evaluate_models, fit_models, load_bundle,
                              predict, preview_rows, save_bundle, validate_bundle)


def sample_data():
    rng = np.random.default_rng(123)
    slopes = rng.normal(size=(25, 9))
    slopes = slopes / np.linalg.norm(slopes, axis=1)[:, None] * 20
    origin = np.linspace(-.2, .2, 9)
    rows = []
    for split in "ABC":
        common = {"session_id": f"session-{split}", "baseline_id": f"baseline-{split}",
                  "valid": True, "source_kind": "synthetic", "algorithm_version": "test-gaussian-1",
                  "config_version": "test-config-1", "split": split}
        for index in range(10):
            rows.append({**common, "x_pm": (origin+rng.normal(0, .01, 9)).tolist(), "force_N": 0.0,
                         "point_id": None, "trial_id": f"{split}-unloaded", "phase": "unloaded"})
        for point_index, point in enumerate(POINT_IDS):
            for trial in range(2):
                for force in (.4, 1.0, 2.0, 3.0):
                    rows.append({**common, "x_pm": (origin+force*slopes[point_index]+rng.normal(0, .01, 9)).tolist(),
                                 "force_N": force, "point_id": point, "trial_id": f"{split}-{point}-trial-{trial}",
                                 "phase": "hold"})
    return rows, slopes, origin


@pytest.fixture(scope="module")
def dataset():
    return sample_data()


@pytest.fixture(scope="module")
def bundle(dataset):
    return fit_models(dataset[0], {"include_mlp": False, "allow_synthetic_diagnostics": True})


def test_signed_templates_force_estimation_and_no_formal_claim(bundle, dataset):
    _, slopes, origin = dataset
    assert validate_bundle(bundle)
    assert bundle["formal_ready"] is False
    assert any(value < 0 for value in bundle["models"]["linear"]["templates"][0]["slope_pm_per_N"])
    for method in ("linear", "piecewise"):
        result = predict(bundle, (origin+slopes[7]*1.5).tolist())["methods"][method]
        assert result["status"] == "有效估计"
        assert result["point_id"] == "P08"
        assert result["raw_force_N"] == pytest.approx(1.5, abs=.01)
        assert result["best"]["residual"] < result["second"]["residual"]
        assert len(result["candidates"]) == 25
    assert predict(bundle, origin.tolist())["status"] == "无接触"
    assert predict(bundle, origin.tolist())["point_id"] is None


def test_uncertain_out_of_range_invalid_and_version_rejection(bundle, dataset):
    _, slopes, origin = dataset
    result = predict(bundle, (origin+slopes[0]*4.2).tolist())
    assert result["status"] == "超范围"
    assert result["raw_force_N"] > 4
    assert result["force_N"] is None, "Out-of-range force must not be clipped to an apparently valid 3 N"
    assert predict(bundle, (origin+(slopes[0]+slopes[1])).tolist())["status"] == "定位不确定"
    assert predict(bundle, [float("nan")]*9)["status"] == "无效"
    assert predict(bundle, [0]*8)["status"] == "无效"
    assert predict(bundle, origin.tolist(), {"stale": True})["status"] == "无效"
    assert predict(bundle, origin.tolist(), {"config_version": "changed"})["status"] == "无效"


def test_fit_rejects_missing_real_data_missing_point_or_validation(dataset):
    rows, _, _ = dataset
    with pytest.raises(ValueError, match="没有数据"):
        fit_models([])
    with pytest.raises(ValueError, match="真实硬件"):
        fit_models(rows, {"include_mlp": False})
    with pytest.raises(ValueError, match="P25"):
        fit_models([r for r in rows if not (r["split"] == "A" and r["point_id"] == "P25")],
                   {"include_mlp": False, "allow_synthetic_diagnostics": True})
    with pytest.raises(ValueError, match="B 组"):
        fit_models([r for r in rows if r["split"] != "B"],
                   {"include_mlp": False, "allow_synthetic_diagnostics": True})
    with pytest.raises(ValueError, match="无载"):
        fit_models([r for r in rows if not (r["split"] == "A" and r["point_id"] is None)],
                   {"include_mlp": False, "allow_synthetic_diagnostics": True})


@pytest.mark.parametrize("key", ["session_id", "trial_id"])
def test_same_session_or_trial_cannot_cross_splits(dataset, key):
    rows = deepcopy(dataset[0])
    b = next(row for row in rows if row["split"] == "B")
    b[key] = rows[0][key]
    with pytest.raises(ValueError, match="不能跨"):
        fit_models(rows, {"include_mlp": False, "allow_synthetic_diagnostics": True})


@pytest.mark.parametrize("key,value", [("source_kind", "hardware"), ("algorithm_version", "other"), ("config_version", "other")])
def test_mixed_provenance_rejected(dataset, key, value):
    rows = deepcopy(dataset[0])
    rows[-1][key] = value
    with pytest.raises(ValueError, match="不能混用"):
        fit_models(rows, {"include_mlp": False, "allow_synthetic_diagnostics": True})


def test_c_data_cannot_change_statistics_templates_or_thresholds(bundle, dataset):
    rows = deepcopy(dataset[0])
    for row in rows:
        if row["split"] == "C":
            row["x_pm"] = [value*100+500 for value in row["x_pm"]]
            if row["point_id"] is not None:
                row["force_N"] *= 20
    changed = fit_models(rows, {"include_mlp": False, "allow_synthetic_diagnostics": True})
    assert changed["baseline_context"] == bundle["baseline_context"]
    assert changed["models"] == bundle["models"]
    assert changed["thresholds"] == bundle["thresholds"]
    assert changed["source_data_sha256"] != bundle["source_data_sha256"]


def test_force_bin_trial_balance_resists_duplicated_samples(bundle, dataset):
    rows = deepcopy(dataset[0])
    repeated = [row for row in rows if row["split"] == "A" and row["point_id"] == "P01" and row["force_N"] == .4 and row["trial_id"].endswith("0")][0]
    rows.extend(deepcopy(repeated) for _ in range(200))
    changed = fit_models(rows, {"include_mlp": False, "allow_synthetic_diagnostics": True})
    assert np.allclose(changed["models"]["linear"]["templates"][0]["slope_pm_per_N"],
                       bundle["models"]["linear"]["templates"][0]["slope_pm_per_N"], atol=1e-12)


def test_independent_session_evaluation_counts_rejections_and_grid_units(bundle, dataset):
    rows = [row for row in dataset[0] if row["split"] == "C"]
    report = evaluate_models(bundle, rows)
    result = report["methods"]["linear"]["by_split"]["C"]
    assert result["known_point_force"]["mae_N"] < .01
    assert result["joint_force"]["rmse_N"] < .01
    assert result["classification_accuracy"] == 1
    assert np.asarray(result["confusion_matrix"]).shape == (25, 25)
    assert sum(map(sum, result["confusion_matrix"])) == 200
    assert result["localization"]["mean_normalized_grid_distance"] == 0
    assert "无毫米" in result["localization"]["unit"]
    assert result["by_session"]["session-C"]["independent_of_training"]
    assert report["point_curves"]["P01"]["trials"][0]["force_N"] == [.4, 1, 2, 3]
    with pytest.raises(ValueError, match="改标"):
        evaluate_models(bundle, [{**dataset[0][0], "split": "C"}])


def test_json_roundtrip_checksum_shape_and_formal_gate(bundle, tmp_path):
    path = tmp_path / "model.json"
    save_bundle(bundle, path)
    assert load_bundle(path) == bundle
    changed = deepcopy(bundle)
    changed["models"]["linear"]["templates"][0]["slope_pm_per_N"][0] += 1
    with pytest.raises(ValueError, match="校验失败"):
        validate_bundle(changed)
    changed = deepcopy(bundle)
    changed["formal_ready"] = True
    with pytest.raises(ValueError, match="正式训练门"):
        validate_bundle(changed)


def test_noise_floor_shared_origin_and_actual_force_labels(dataset):
    rows = deepcopy(dataset[0])
    for row in rows:
        if row["point_id"] is None:
            row["x_pm"] = [0.0]*9
    result = fit_models(rows, {"include_mlp": False, "allow_synthetic_diagnostics": True, "noise_floor_pm": .1})
    assert np.allclose(result["baseline_context"]["noise_variance_pm2"], .01)
    for template in result["models"]["piecewise"]["templates"]:
        assert template["force_knots_N"][0] == 0
        assert template["x_knots_pm"][0] == [0]*9
    assert max(row["force_N"] for row in rows) == 3


def test_force_labels_over_provisional_limit_remain_actual(dataset):
    rows = deepcopy(dataset[0])
    for row in rows:
        if row["force_N"] == 3:
            row["force_N"] = 4.5
    result = fit_models(rows, {"include_mlp": False, "allow_synthetic_diagnostics": True})
    assert result["max_force_N"] == 3 and result["max_force_provisional"]
    assert result["models"]["linear"]["templates"][0]["max_training_force_N"] == 4.5
    assert result["models"]["piecewise"]["templates"][0]["force_knots_N"][-1] == 4.5
    assert 4.5 in preview_rows(rows)["point_curves"]["P01"]["trials"][0]["force_N"]


def test_mlp_fixed_seed_dimensions_and_epoch_budget(dataset):
    options = {"include_mlp": True, "allow_synthetic_diagnostics": True, "mlp_epochs": 3, "seed": 99}
    first = fit_models(dataset[0], options)
    second = fit_models(dataset[0], options)
    assert first["models"]["mlp"] == second["models"]["mlp"]
    assert np.asarray(first["models"]["mlp"]["layers"][-1]["weights"]).shape == (32, 26)
    assert first["models"]["mlp"]["classification_outputs"] == 25
    assert "mlp" in predict(first, dataset[0][-1]["x_pm"])["methods"]
    json.dumps(first, allow_nan=False)
    with pytest.raises(ValueError, match="500"):
        fit_models(dataset[0], {**options, "mlp_epochs": 501})


def test_pilot_preview_accepts_five_points_without_training_or_other_splits(dataset):
    chosen = {"P01", "P05", "P13", "P21", "P25", None}
    rows = [row for row in dataset[0] if row["split"] == "A" and row["point_id"] in chosen]
    preview = preview_rows(rows)
    assert preview["model_generated"] is False and preview["formal_ready"] is False
    assert len(preview["points"]) == 5
    assert len(preview["points"]["P13"]["slope_pm_per_N"]) == 9
    trial = preview["points"]["P13"]["trial_statistics"][0]
    assert len(trial["noise_std_pm"]) == 9
    assert trial["hold_n"] == 4 and trial["hold_residual_rms_pm"] is not None
    assert preview["quality_summary"]["noise_verified"]
    assert len(preview["points"]["P13"]["r2_per_fbg"]) == 9
    assert len(preview["points"]["P13"]["residual_rmse_pm"]) == 9
    json.dumps(preview, allow_nan=False)


def reseal_diagnostic_bundle(bundle):
    content = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    bundle["bundle_sha256"] = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                                      separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return bundle


def test_negative_force_projection_cannot_beat_true_positive_response(bundle):
    changed = deepcopy(bundle)
    positive = np.asarray(changed["models"]["linear"]["templates"][1]["slope_pm_per_N"])
    changed["models"]["linear"]["templates"][0]["slope_pm_per_N"] = (-positive).tolist()
    positive_curve = deepcopy(changed["models"]["piecewise"]["templates"][1])
    changed["models"]["piecewise"]["templates"][0] = {
        **positive_curve, "point_id": "P01", "x_knots_pm": (-np.asarray(positive_curve["x_knots_pm"])).tolist()}
    reseal_diagnostic_bundle(changed)
    origin = np.asarray(changed["baseline_context"]["origin_pm"])
    # P01 can match this exactly only with a forbidden negative force. P02 is
    # the real positive response, even though P01 precedes it in model order.
    for method in ("linear", "piecewise"):
        vector = positive if method == "linear" else np.asarray(positive_curve["x_knots_pm"][2])/positive_curve["force_knots_N"][2]
        result = predict(changed, (origin+vector).tolist())["methods"][method]
        assert result["status"] == "有效估计" and result["point_id"] == "P02"
        reverse = next(candidate for candidate in result["candidates"] if candidate["point_id"] == "P01")
        assert reverse["raw_force_N"] < 0
        assert reverse["fit_force_N"] == 0
        assert reverse["unconstrained_residual"] < reverse["residual"]
        assert reverse["residual"] > result["best"]["residual"]


def test_out_of_range_keeps_unclipped_force_but_scores_feasible_boundary(bundle, dataset):
    _, slopes, origin = dataset
    for method in ("linear", "piecewise"):
        result = predict(bundle, (origin+slopes[7]*4.2).tolist())["methods"][method]
        assert result["status"] == "超范围"
        assert result["raw_force_N"] > 4
        assert result["fit_force_N"] <= 3
        assert result["force_N"] is None
        candidate = result["range_candidate"]
        assert candidate["point_id"] == "P08"
        assert candidate["residual"] > candidate["unconstrained_residual"]
        assert candidate["residual"] > result["residual_threshold"], "Range warning survives increased constrained residual"
        assert all(0 <= c["fit_force_N"] <= min(3, c["max_training_force_N"]) for c in result["candidates"])
        assert result["formal_ready"] is False


def test_measured_point_limit_is_used_even_below_provisional_limit(dataset):
    rows, slopes, origin = deepcopy(dataset)
    rows = [row for row in rows if not (row["split"] == "A" and row["point_id"] == "P02" and row["force_N"] > 1)]
    for row in rows:
        if row["point_id"] == "P02":
            row["x_pm"] = (origin+slopes[1]*row["force_N"]).tolist()
    changed = fit_models(rows, {"include_mlp": False, "allow_synthetic_diagnostics": True})
    for method in ("linear", "piecewise"):
        result = predict(changed, (origin+slopes[1]*2).tolist())["methods"][method]
        assert result["status"] == "超范围"
        assert result["raw_force_N"] == pytest.approx(2, abs=.01)
        assert result["fit_force_N"] <= 1
        assert result["accepted_max_force_N"] == 1
        assert result["force_N"] is None
