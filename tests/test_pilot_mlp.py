"""Synthetic model contracts; these tests make no physical accuracy claim."""
from copy import deepcopy
import json

import numpy as np
import pytest

from ninefbg.inversion import _digest, validate_bundle
from ninefbg.pilot import (MLP_SCHEMA, fit_pilot, load_pilot_model, predict_pilot,
                          save_pilot_model, validate_pilot_model)
from test_pilot import sample_rows


def fit(rows, **options):
    return fit_pilot(rows, {"allow_synthetic_diagnostics": True, "method": "mlp", **options})


@pytest.fixture(scope="module")
def trained():
    rows, slopes, origin = sample_rows()
    return rows, slopes, origin, fit(rows)


def test_four_measured_points_train_a_subset_network_and_keep_whole_trial_cv(trained):
    rows, _, _, result = trained
    before = deepcopy(rows)
    model, report = result["model"], result["report"]
    assert model["primary_method"] == report["method"] == "mlp"
    assert model["mlp"]["schema"] == MLP_SCHEMA
    assert model["mlp"]["architecture"] == [9, 32, 16, 5]
    assert model["mlp"]["classification_point_ids"] == ["P01", "P05", "P21", "P25"]
    assert model["mlp"]["classification_outputs"] == 4
    assert model["mlp"]["force_outputs"] == 1
    assert model["formal_ready"] is report["formal_ready"] is False
    assert model["diagnostic_only"] and model["source_kind"] == "synthetic"
    assert report["summary"]["classification_accuracy"] > .9
    assert report["summary"]["test_trials"] == 12
    assert report["models"] == list(report["methods"]) == ["mlp"]
    held_out = []
    for fold in report["folds"]:
        train, test = set(fold["train_trial_ids"]), set(fold["test_trial_ids"])
        assert not train & test and len(train) == 8 and len(test) == 4
        assert not set(fold["train_baseline_ids"]) & set(fold["test_baseline_ids"])
        assert set(fold["fold_model"]["provenance"]["training_trial_ids"]) == train
        assert fold["fold_model"]["provenance"]["test_rows_used_for_fitting"] == 0
        held_out.extend(test)
    assert len(held_out) == len(set(held_out)) == 12
    assert report["validation"]["independent_session_validation"] is False
    assert "not_test" in report["validation"]["threshold_selection"]
    assert rows == before
    json.dumps(result, allow_nan=False)


def test_held_out_force_features_and_noise_cannot_change_network_or_thresholds(trained):
    rows, _, _, original = trained
    held_out = set(original["report"]["folds"][0]["test_trial_ids"])
    changed = deepcopy(rows)
    for row in changed:
        if row["trial_id"] in held_out:
            row["x_pm"] = [value*5+300 for value in row["x_pm"]]
            if row["point_id"]:
                row["force_N"] *= 2
    altered = fit(changed)
    assert altered["report"]["folds"][0]["fold_model"] == original["report"]["folds"][0]["fold_model"]
    assert altered["model"]["model_sha256"] != original["model"]["model_sha256"]


def test_inference_uses_nine_fbg_only_and_rejects_unloaded_stale_or_outside_range(trained):
    _, slopes, origin, result = trained
    model = result["model"]
    x_pm = (origin+slopes["P21"]*1.7).tolist()
    before = deepcopy(x_pm)
    prediction = predict_pilot(model, x_pm)
    assert prediction["status"] == "有效估计" and prediction["point_id"] == "P21"
    assert prediction["force_N"] == pytest.approx(1.7, abs=.25)
    assert prediction == predict_pilot(model, x_pm, {"force_N": 999, "truth_force_N": -99,
                                                     "point_id": "P01", "target_point_id": "P25"})
    assert x_pm == before
    assert predict_pilot(model, origin.tolist())["status"] == "无接触"
    outside = predict_pilot(model, (origin+slopes["P05"]*4.5).tolist())
    assert outside["status"] == "超范围"
    assert outside["point_id"] is outside["force_N"] is None
    assert outside["raw_force_N"] > 4.4
    assert outside["fit_force_N"] <= 3
    assert predict_pilot(model, x_pm, {"stale": True})["status"] == "无效"
    assert predict_pilot(model, x_pm, {"config_version": "other"})["status"] == "无效"
    assert predict_pilot(model, x_pm[:8])["status"] == "无效"
    assert predict_pilot(model, [float("nan")]*9)["status"] == "无效"


def test_mlp_progress_reports_actual_epochs_folds_and_final_refit():
    rows, _, _ = sample_rows()
    progress = []
    result = fit_pilot(rows, {"allow_synthetic_diagnostics": True, "method": "mlp", "mlp_epochs": 7}, progress.append)
    assert progress[0]["stage"] == "prepare" and progress[0]["progress_percent"] == 0
    assert progress[-1]["stage"] == "complete" and progress[-1]["progress_percent"] == 100
    percentages = [item["progress_percent"] for item in progress]
    assert percentages == sorted(percentages)
    for fold in (1, 2, 3):
        steps = [item for item in progress if item["stage"] == "cv_fit" and item.get("fold") == fold and "epoch" in item]
        assert [item["epoch"] for item in steps] == list(range(1, 8))
        assert all(item["epochs"] == 7 and item["method"] == "mlp" for item in steps)
    final = [item for item in progress if item["stage"] == "final_fit" and "epoch" in item]
    assert [item["epoch"] for item in final] == list(range(1, 8))
    assert result["model"]["mlp"]["epochs"] == 7
    linear_progress = []
    linear = fit_pilot(rows, {"allow_synthetic_diagnostics": True}, linear_progress.append)
    assert linear["model"]["primary_method"] == "linear" and "mlp" not in linear["model"]
    assert linear_progress[-1]["progress_percent"] == 100
    assert not any("epoch" in item for item in linear_progress)


def test_mlp_save_load_checksum_and_old_linear_model_remain_compatible(tmp_path, trained):
    _, slopes, origin, result = trained
    model = result["model"]
    path = tmp_path/"mlp.json"
    save_pilot_model(model, path)
    loaded = load_pilot_model(path)
    assert loaded == model and validate_pilot_model(loaded)
    x = (origin+slopes["P01"]*1.4).tolist()
    assert predict_pilot(loaded, x) == predict_pilot(model, x)
    changed = deepcopy(model)
    changed["mlp"]["layers"][0]["weights"][0][0] += .01
    with pytest.raises(ValueError, match="校验失败"):
        validate_pilot_model(changed)
    with pytest.raises(ValueError, match="25 点"):
        validate_bundle(model)
    linear = fit_pilot(sample_rows()[0], {"allow_synthetic_diagnostics": True})["model"]
    linear.pop("primary_method")  # Older linear artifacts can omit the selector.
    linear["model_sha256"] = _digest({key: value for key, value in linear.items() if key != "model_sha256"})
    assert validate_pilot_model(linear)
    save_pilot_model(linear, tmp_path/"legacy.json")
    assert predict_pilot(load_pilot_model(tmp_path/"legacy.json"), x)["method"] == "linear"


@pytest.mark.parametrize("mutation", ["missing_network", "schema", "points", "weight_shape", "bias_nan", "scale_zero", "formal"])
def test_rehashed_mlp_with_incompatible_dimensions_or_identity_is_rejected(trained, mutation):
    model = deepcopy(trained[3]["model"])
    if mutation == "missing_network":
        model.pop("mlp")
    elif mutation == "schema":
        model["mlp"]["schema"] = "unknown"
    elif mutation == "points":
        model["mlp"]["classification_point_ids"].reverse()
    elif mutation == "weight_shape":
        model["mlp"]["layers"][0]["weights"][0].pop()
    elif mutation == "bias_nan":
        model["mlp"]["layers"][0]["bias"][0] = float("nan")
    elif mutation == "scale_zero":
        model["mlp"]["feature_scale_pm"][0] = 0
    else:
        model["formal_ready"] = True
    if mutation != "bias_nan":
        model["model_sha256"] = _digest({key: value for key, value in model.items() if key != "model_sha256"})
    with pytest.raises(ValueError):
        validate_pilot_model(model)


@pytest.mark.parametrize("options", [{"method": "piecewise"}, {"method": True}, {"mlp_epochs": 0},
                                      {"mlp_epochs": True}, {"mlp_batch_size": 0}, {"seed": -1},
                                      {"mlp_learning_rate": float("nan")}, {"mlp_learning_rate": 0}])
def test_invalid_method_or_training_parameters_fail_before_training(options):
    with pytest.raises(ValueError):
        fit(sample_rows()[0], **options)


def test_mlp_does_not_create_a_model_without_real_or_sufficient_data():
    rows, _, _ = sample_rows()
    with pytest.raises(ValueError, match="真实硬件"):
        fit_pilot(rows, {"method": "mlp"})
    with pytest.raises(ValueError, match="至少需要三个"):
        fit([row for row in rows if not row["trial_id"].endswith("T2")])
    with pytest.raises(ValueError, match="真实无载"):
        fit([row for row in rows if row["point_id"] is not None])
    for row in rows:
        row["split"] = "C"
    with pytest.raises(ValueError, match="C 组"):
        fit(rows)


def test_mlp_seed_and_balanced_normalization_are_repeatable(trained):
    rows, _, _, original = trained
    before = deepcopy(rows)
    repeated = fit(rows)
    assert repeated["model"] == original["model"]
    assert rows == before
    assert np.isfinite(original["model"]["mlp"]["feature_scale_pm"]).all()
