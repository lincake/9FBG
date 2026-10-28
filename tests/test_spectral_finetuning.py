"""Generated source fixtures exercise inheritance, never physical accuracy."""
from copy import deepcopy
import json
import queue

import pytest

from ninefbg.spectral_data import load_dataset, fit_normalization, training_selection
from ninefbg.spectral_finetuning import prepare_training, verify_plan
from ninefbg.spectral_model import build_network, load_bundle, save_bundle, validate_metadata, metadata_digest
from ninefbg.spectral_training import checked_options, _worker
from test_spectral_pipeline import spectral_archive, _rewrite


def saved_parent(root, *, trials_count=4, max_gap=120, adapt=False):
    torch = pytest.importorskip("torch")
    torch.set_num_threads(2)
    torch.manual_seed(187)
    old = spectral_archive(root, "old-source", trials_count=trials_count)
    options = checked_options({"epochs": 1, "batch_size": 64, "max_spectrum_gap_ms": max_gap,
                               "adapt_legacy_unloaded": adapt})
    data = load_dataset(root, [old.name], options)
    indices, sampling = training_selection(data)
    normal_x, normal_y = fit_normalization(data, indices)
    train = sorted({data["windows"][index]["trial_id"] for index in indices})
    validation = sorted({row["trial_id"] for row, split in zip(data["windows"], data["split_labels"]) if split == "validation"})
    metadata = {"wavelength_nm": data["wavelength_nm"], "preprocessing": data["preprocessing"],
                "sample_rate_hz": data["sample_rate_hz"], "acquisition_provenance": data["acquisition_provenance"],
                "input_normalization": normal_x, "output_normalization": normal_y,
                "trained_point_ids": sorted({f"P{value+1:02d}" for value in data["position"][indices][data["position_mask"][indices]]}),
                "training_trial_ids": train, "validation_trial_ids": validation, "split": data["split"],
                "training_options": options, "training_sampling": sampling, "references": data["references"],
                "window_rule": {"version": "spectral_causal_windows_v1", "length": 16, "max_gap_ms": max_gap,
                                "target": "last_frame", "hidden_init": "zero"}, "sync_rule": data["sync_rule"],
                "evaluation": {"independent_validation": bool(validation), "hardware_accuracy_qualified": False}}
    # A valid old-format parent deliberately has no 0.10.0 lineage fields. It
    # contains generated initial weights, not learned hardware qualification.
    parent = save_bundle(root, "Sparent", build_network(), metadata)
    new = spectral_archive(root, "new-source", sample_offset=9., trials_count=trials_count)
    return old, new, parent


class Events(queue.Queue):
    def close(self):
        pass
    def join_thread(self):
        pass


def run_worker(root, selected, raw_options, *, prepared=None):
    options = checked_options(raw_options)
    merged, plan = prepared or prepare_training(root, selected, options, explicit_options=raw_options)
    events = Events()
    _worker(str(root), "synthetic-direct-worker", merged, options, queue.Queue(), events, plan)
    values = []
    while not events.empty():
        values.append(events.get_nowait())
    assert values
    return values[-1], plan


def fine_options(**extra):
    return {"training_mode": "finetune", "base_model_id": "Sparent", "epochs": 1, "batch_size": 64, **extra}


def test_finetune_defaults_lr_alias_and_parent_window_settings_are_explicit(tmp_path):
    assert checked_options({})["learning_rate"] == .001
    assert checked_options(fine_options())["learning_rate"] == .0001
    assert checked_options(fine_options(lr=.0003))["learning_rate"] == .0003
    assert checked_options(fine_options(learning_rate=.0002))["learning_rate"] == .0002
    old, new, parent = saved_parent(tmp_path, max_gap=160, adapt=True)
    raw = fine_options()
    options = checked_options(raw)
    merged, plan = prepare_training(tmp_path, [old.name, new.name], options, explicit_options=raw)
    assert merged == [old.name, new.name] and plan["new_session_ids"] == [new.name]
    assert options["max_spectrum_gap_ms"] == 160 and options["adapt_legacy_unloaded"] is True
    for key, value in (("max_spectrum_gap_ms", 120), ("adapt_legacy_unloaded", False)):
        raw = fine_options(**{key: value})
        with pytest.raises(ValueError, match="样本规则"):
            prepare_training(tmp_path, [new.name], checked_options(raw), explicit_options=raw)


def test_zero_optimizer_steps_preserve_every_parent_weight_normalizer_and_fixed_validation(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    old, new, parent = saved_parent(tmp_path)
    _, original = load_bundle(tmp_path/"spectral_models"/"Sparent")
    before = {key: value.clone() for key, value in original.state_dict().items()}
    def forbidden(*args, **kwargs):
        pytest.fail("微调不得重新拟合归一化")
    monkeypatch.setattr("ninefbg.spectral_data.fit_normalization", forbidden)
    # The real forward/backward runs, but zero optimization steps isolate the
    # initialization contract from any intended subsequent learning change.
    monkeypatch.setattr(torch.optim.Adam, "step", lambda self, *args, **kwargs: None)
    result, plan = run_worker(tmp_path, [new.name], fine_options())
    assert result["status"] == "completed", result
    child, network = load_bundle(tmp_path/"spectral_models"/result["model_id"])
    assert all(torch.equal(value, network.state_dict()[key]) for key, value in before.items())
    assert child["input_normalization"] == parent["input_normalization"]
    assert child["output_normalization"] == parent["output_normalization"]
    assert set(parent["training_trial_ids"]).issubset(child["training_trial_ids"])
    assert not set(parent["validation_trial_ids"]) & set(child["training_trial_ids"])
    compare = child["evaluation"]["finetuning_comparison"]
    assert compare["available"] and compare["parent"] == compare["child"]
    assert compare["validation_trial_ids"] == parent["validation_trial_ids"]
    assert compare["subset_count"] == compare["parent"]["window_count"]
    assert child["base_model_id"] == "Sparent" and child["parent_model_sha256"] == parent["model_sha256"]
    assert child["new_session_ids"] == [new.name] and plan["merged_session_ids"] == [old.name, new.name]


def test_missing_ledger_restores_parent_groups_without_training_on_old_holdout(tmp_path):
    old, new, parent = saved_parent(tmp_path)
    ledgers = list((tmp_path/"spectral_splits").glob("*.json"))
    assert len(ledgers) == 1
    ledgers[0].unlink()
    options = checked_options(fine_options())
    merged, _ = prepare_training(tmp_path, [new.name], options, explicit_options=fine_options())
    data = load_dataset(tmp_path, merged, options, inherited_split=parent["split"], expected_metadata=parent,
                        new_training_sessions=[new.name])
    for value in parent["training_trial_ids"]:
        assert data["split"]["trial_assignments"][value] == "train"
    for value in parent["validation_trial_ids"]:
        assert data["split"]["trial_assignments"][value] == "validation"
    assert ledgers[0].exists()
    assert any(row["trial_id"].startswith(new.name+":") and split == "train"
               for row, split in zip(data["windows"], data["split_labels"]))


@pytest.mark.parametrize("mapping", ["trial_assignments", "assignments", "source_manifests"])
def test_conflicting_local_ledger_is_rejected_without_rewriting_it(tmp_path, mapping):
    old, new, parent = saved_parent(tmp_path)
    ledger = next((tmp_path/"spectral_splits").glob("*.json"))
    value = json.loads(ledger.read_text(encoding="utf-8"))
    key = next(iter(value[mapping]))
    value[mapping][key] = "f"*64 if mapping == "source_manifests" else ("validation" if value[mapping][key] == "train" else "train")
    ledger.write_text(json.dumps(value), encoding="utf-8")
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="冲突"):
        load_dataset(tmp_path, [old.name, new.name], checked_options(fine_options()),
                     inherited_split=parent["split"], expected_metadata=parent, new_training_sessions=[new.name])
    assert ledger.read_bytes() == before


def test_single_old_and_new_baseline_groups_train_without_inventing_independent_validation(tmp_path):
    old, new, parent = saved_parent(tmp_path, trials_count=1)
    assert parent["validation_trial_ids"] == []
    result, _ = run_worker(tmp_path, [new.name], fine_options())
    assert result["status"] == "completed", result
    child, _ = load_bundle(tmp_path/"spectral_models"/result["model_id"])
    assert child["training_trial_ids"] == [new.name+":T0001", old.name+":T0001"]
    assert child["validation_trial_ids"] == [] and not child["evaluation"]["independent_validation"]
    compare = child["evaluation"]["finetuning_comparison"]
    assert not compare["available"] and compare["parent"] is compare["child"] is None
    assert compare["window_count"] == 0 and compare["reason"]


def test_already_heldout_new_group_cannot_be_promoted_to_training(tmp_path):
    old, new, parent = saved_parent(tmp_path, trials_count=1)
    # Establish the new group as held-out through an earlier explicit split.
    natural = load_dataset(tmp_path, [old.name, new.name])
    assert natural["split"]["trial_assignments"][new.name+":T0001"] == "validation"
    result, _ = run_worker(tmp_path, [new.name], fine_options())
    assert result["status"] == "failed" and "新增会话没有可用于优化" in result["error"]
    assert natural["split"]["trial_assignments"][new.name+":T0001"] == "validation"
    assert {path.name for path in (tmp_path/"spectral_models").iterdir()} == {"Sparent"}


def test_new_train_windows_with_only_zero_weight_labels_cannot_count_as_new_optimization(tmp_path):
    old, new, parent = saved_parent(tmp_path, trials_count=1)
    rows = [json.loads(line) for line in (new/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row.get("trial_id"):
            row["phase"] = "preload"  # Force labels remain valid; position has no trusted contact label.
    _rewrite(new, "raw_spectra.jsonl", rows)
    assigned = load_dataset(tmp_path, [old.name, new.name], checked_options(fine_options()),
                            inherited_split=parent["split"], expected_metadata=parent, new_training_sessions=[new.name])
    assert assigned["split"]["trial_assignments"][new.name+":T0001"] == "train"
    result, _ = run_worker(tmp_path, [new.name], fine_options(wrench_loss_weight=0.))
    assert result["status"] == "failed" and "新增会话没有可用于优化" in result["error"]


@pytest.mark.parametrize("asset", ["weights", "metadata", "source_manifest", "source_raw"])
def test_parent_or_sources_replaced_after_submission_fail_before_candidate_publish(tmp_path, asset):
    old, new, parent = saved_parent(tmp_path)
    raw = fine_options()
    options = checked_options(raw)
    prepared = prepare_training(tmp_path, [new.name], options, explicit_options=raw)
    if asset == "weights":
        path = tmp_path/"spectral_models"/"Sparent"/"weights.pt"
        with path.open("ab") as stream:
            stream.write(b"changed")
    elif asset == "metadata":
        path = tmp_path/"spectral_models"/"Sparent"/"metadata.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["created_utc"] = "2026-10-13T00:00:00+00:00"
        value["metadata_sha256"] = value["model_sha256"] = metadata_digest(value)
        path.write_text(json.dumps(value), encoding="utf-8")
    elif asset == "source_manifest":
        path = old/"manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["operator_fixture_note"] = "replaced"
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        with (old/"raw_force.jsonl").open("ab") as stream:
            stream.write(b"{}\n")
    result, _ = run_worker(tmp_path, [new.name], raw, prepared=prepared)
    assert result["status"] == "failed" and any(word in result["error"] for word in ("哈希", "改变", "摘要"))
    assert {path.name for path in (tmp_path/"spectral_models").iterdir()} == {"Sparent"}


def test_resealed_lineage_or_comparison_tampering_is_rejected(tmp_path):
    _, new, _ = saved_parent(tmp_path)
    result, _ = run_worker(tmp_path, [new.name], fine_options())
    assert result["status"] == "completed", result
    child, _ = load_bundle(tmp_path/"spectral_models"/result["model_id"])
    for mutation in (lambda value: value["training_lineage"].update(checkpoint_resume=True),
                     lambda value: value["training_lineage"].update(normalization_source="train_split"),
                     lambda value: value["evaluation"]["finetuning_comparison"].update(subset_count=1)):
        tampered = deepcopy(child)
        mutation(tampered)
        tampered["metadata_sha256"] = tampered["model_sha256"] = metadata_digest(tampered)
        with pytest.raises(ValueError, match="来源|继承|比较"):
            validate_metadata(tampered)


def test_parent_preflight_does_not_hold_realtime_analysis_lock_and_reserves_job_slot(tmp_path):
    from ninefbg.analysis import AnalysisController
    controller = AnalysisController(tmp_path)
    class Trainer:
        def start(self, sessions, options):
            assert not controller.lock._is_owned()
            assert controller.busy
            controller._refresh_spectral_training()
            assert controller.busy
            return self.status()
        def status(self):
            return {"method": "spectral_gru", "busy": True, "status": "running", "job_id": "test-reserved-slot"}
    controller.spectral_training = Trainer()
    reply = controller.start_job("train", ["synthetic-only"], {"method": "spectral_gru"})
    assert reply["busy"] and reply["job_id"] == "test-reserved-slot"
    assert not controller._spectral_starting


@pytest.mark.parametrize("options", [{"training_mode": "scratch"}, {"base_model_id": None}])
def test_legacy_methods_reject_spectral_mode_fields_before_launching_async_work(tmp_path, options):
    from ninefbg.analysis import AnalysisController
    controller = AnalysisController(tmp_path)
    with pytest.raises(ValueError, match="仅支持 MLP"):
        controller.start_job("train", ["synthetic-only"], {"method": "linear", **options})
    assert not controller.busy
