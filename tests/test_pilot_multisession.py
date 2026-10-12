"""Pooled synthetic regressions protect identities, not physical acceptance."""
from copy import deepcopy
import json

import pytest

from ninefbg.inversion import _digest
from ninefbg.pilot import (MODEL_SCHEMA, MULTI_SCOPE, SCOPE, fit_pilot,
                          load_pilot_model, predict_pilot, save_pilot_model,
                          validate_pilot_model)
from test_pilot import sample_rows


def pooled_rows(*, shared_points=False, repetitions=3):
    rows, slopes, origin = sample_rows(repetitions)
    local_ids = {"session-A": {}, "session-B": {}}
    for row in rows:
        if shared_points:
            session = "session-B" if row["trial_id"].endswith("T2") else "session-A"
        else:
            session = "session-A" if row["trial_id"].startswith(("P01", "P05")) else "session-B"
        ids = local_ids[session]
        ordinal = ids.setdefault(row["trial_id"], len(ids)+1)
        row.update(session_id=session, trial_id=f"T{ordinal:04d}", baseline_id=f"B{ordinal:04d}",
                   split="A" if session == "session-A" else "B")
    return rows, slopes, origin


def fit(rows, method="linear"):
    return fit_pilot(rows, {"method": method, "allow_synthetic_diagnostics": True})


@pytest.mark.parametrize("method", ["linear", "mlp"])
def test_sessions_with_disjoint_points_and_same_local_ids_fit_and_predict_without_mixing(tmp_path, method):
    rows, slopes, origin = pooled_rows()
    before = deepcopy(rows)
    result = fit(rows, method)
    model, report = result["model"], result["report"]
    assert rows == before
    assert model["schema"] == MODEL_SCHEMA
    assert model["primary_method"] == method
    assert model["scope"] == "measured_points_multi_session_research_only"
    assert report["scope"] == MULTI_SCOPE
    assert report["session_id"] is report["batch"] is None
    assert report["session_ids"] == ["session-A", "session-B"]
    assert report["session_count"] == report["quality_summary"]["session_count"] == 2
    assert report["batches"] == ["A", "B"]
    assert report["point_ids"] == ["P01", "P05", "P21", "P25"]
    assert report["summary"]["test_trials"] == 12
    assert report["validation"]["independent_session_validation"] is False
    assert "不证明" in report["interpretation"] and "跨会话" in report["interpretation"]
    provenance = model["provenance"]
    assert provenance["training_session_ids"] == report["session_ids"]
    assert provenance["training_session_count"] == 2
    assert provenance["training_session_splits"] == {"session-A": "A", "session-B": "B"}
    assert len(provenance["training_trial_ids"]) == 12
    assert len(provenance["training_trial_sessions"]) == len(provenance["training_baseline_sessions"]) == 12
    assert provenance["training_trial_sessions"]["session-A:T0001"] == "session-A"
    assert provenance["training_trial_sessions"]["session-B:T0001"] == "session-B"
    assert provenance["training_baseline_sessions"]["session-A:B0001"] == "session-A"
    assert provenance["training_baseline_sessions"]["session-B:B0001"] == "session-B"
    assert len(model["baseline_context"]["training_baseline_ids"]) == 12
    seen = []
    for fold in report["folds"]:
        train, test = set(fold["train_trial_ids"]), set(fold["test_trial_ids"])
        assert not train & test and len(train) == 8 and len(test) == 4
        assert not set(fold["train_baseline_ids"]) & set(fold["test_baseline_ids"])
        assert fold["train_session_ids"] == fold["test_session_ids"] == ["session-A", "session-B"]
        assert set(fold["fold_model"]["provenance"]["training_trial_ids"]) == train
        assert set(fold["fold_model"]["baseline_context"]["training_baseline_ids"]) == set(fold["train_baseline_ids"])
        assert fold["fold_model"]["provenance"]["test_rows_used_for_fitting"] == 0
        seen.extend(test)
    assert len(seen) == len(set(seen)) == 12
    assert len(report["out_of_fold_predictions"]) == 84
    assert all(row["trial_id"].startswith(row["session_id"]+":") and row["baseline_id"].startswith(row["session_id"]+":") for row in report["out_of_fold_predictions"])
    path = tmp_path/f"{method}.json"
    save_pilot_model(model, path)
    loaded = load_pilot_model(path)
    assert loaded == model and validate_pilot_model(loaded)
    x_pm = (origin+slopes["P21"]*1.7).tolist()
    prediction = predict_pilot(loaded, x_pm)
    assert prediction["status"] == "有效估计" and prediction["point_id"] == "P21"
    assert prediction == predict_pilot(model, x_pm, {"point_id": "P05", "target_point_id": "P01", "truth_force_N": 999})
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("method", ["linear", "mlp"])
def test_three_repetitions_per_point_can_be_contributed_by_two_sessions(method):
    rows, _, _ = pooled_rows(shared_points=True)
    # Each A point has two trials; each B point has one. The union has three.
    for session in ("session-A", "session-B"):
        with pytest.raises(ValueError, match="至少需要三个"):
            fit([row for row in rows if row["session_id"] == session], method)
    report = fit(rows, method)["report"]
    assert report["summary"]["test_trials"] == 12 and report["session_count"] == 2
    assert all(item["test_trials"] == 3 for item in report["by_point"].values())
    assert all(fold["test_row_count"] == 60 for fold in report["folds"])


@pytest.mark.parametrize("method", ["linear", "mlp"])
def test_mutating_a_held_out_multisession_trial_cannot_change_its_fold_model(method):
    rows, _, _ = pooled_rows()
    original = fit(rows, method)
    held_out = set(original["report"]["folds"][0]["test_trial_ids"])
    changed = deepcopy(rows)
    for row in changed:
        if row["session_id"]+":"+row["trial_id"] in held_out:
            row["x_pm"] = [value*4+200 for value in row["x_pm"]]
            if row["point_id"]:
                row["force_N"] *= 2
    altered = fit(changed, method)
    assert original["report"]["folds"][0]["fold_model"] == altered["report"]["folds"][0]["fold_model"]
    assert original["model"]["source_data_sha256"] != altered["model"]["source_data_sha256"]


def test_archive_qualified_identities_are_not_prefixed_twice():
    rows, _, _ = pooled_rows()
    original = fit(rows)
    already_qualified = deepcopy(rows)
    for row in already_qualified:
        row["trial_id"] = row["session_id"]+":"+row["trial_id"]
        row["baseline_id"] = row["session_id"]+":"+row["baseline_id"]
    assert fit(already_qualified) == original


@pytest.mark.parametrize("kind", ["algorithm_version", "config_version", "source_kind", "C"])
def test_pooling_keeps_algorithm_configuration_source_and_c_group_guards(kind):
    rows, _, _ = pooled_rows()
    for row in rows:
        if row["session_id"] == "session-B":
            if kind == "C":
                row["split"] = "C"
            else:
                row[kind] = "hardware" if kind == "source_kind" else "other"
    with pytest.raises(ValueError, match="C 组" if kind == "C" else "不能混用"):
        fit(rows)


def test_pooling_does_not_relax_three_trials_per_union_point_or_hardware_default():
    rows, _, _ = pooled_rows(repetitions=2)
    with pytest.raises(ValueError, match="至少需要三个"):
        fit(rows)
    with pytest.raises(ValueError, match="真实硬件"):
        fit_pilot(pooled_rows()[0])


@pytest.mark.parametrize("key", ["trial_id", "baseline_id"])
def test_session_prefix_does_not_turn_an_empty_identity_into_a_valid_one(key):
    rows, _, _ = pooled_rows()
    rows[0][key] = " "
    with pytest.raises(ValueError, match=key):
        fit(rows)


def test_single_session_scope_and_legacy_v1_models_remain_supported(tmp_path):
    rows, slopes, origin = sample_rows()
    result = fit(rows)
    model, report = result["model"], result["report"]
    assert report["scope"] == SCOPE and report["session_id"] == "s1" and report["batch"] == "A"
    assert report["session_ids"] == ["s1"] and report["session_count"] == 1
    assert model["scope"] == "measured_points_same_session_research_only"
    assert all(not trial.startswith("s1:") for trial in model["provenance"]["training_trial_ids"])
    old = deepcopy(model)
    for key in ("training_session_count", "training_session_splits", "training_trial_sessions", "training_baseline_sessions"):
        old["provenance"].pop(key)
    old["model_sha256"] = _digest({key: value for key, value in old.items() if key != "model_sha256"})
    path = tmp_path/"legacy-P-v1.json"
    save_pilot_model(old, path)
    loaded = load_pilot_model(path)
    x_pm = (origin+slopes["P05"]*1.5).tolist()
    assert predict_pilot(loaded, x_pm) == predict_pilot(model, x_pm)


def test_union_can_use_one_distinct_point_from_each_session():
    rows, _, _ = pooled_rows()
    rows = [row for row in rows if row["point_id"] in ("P01", "P21") or
            row["point_id"] is None and row["trial_id"] in ("T0001", "T0002", "T0003")]
    report = fit(rows)["report"]
    assert report["point_ids"] == ["P01", "P21"] and report["session_count"] == 2
    assert report["summary"]["test_trials"] == 6
