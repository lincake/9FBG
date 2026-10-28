"""Generated contact supervision and inference checks, not hardware qualification."""
from copy import deepcopy
import json

import numpy as np
import pytest

from ninefbg.spectral_data import CONTACT_LABEL_RULE
from ninefbg.spectral_model import (CONTACT_DEFINITION, CONTACT_SCHEMA, SCHEMA, build_network,
                                    load_bundle, masked_losses, metadata_digest, save_bundle,
                                    validate_metadata)
from ninefbg.spectral_training import _metrics
from ninefbg.spectral_finetuning import prepare_training
from ninefbg.spectral_training import checked_options
from test_spectral_finetuning import saved_parent, run_worker, fine_options
from test_spectral_runtime import setup_runtime, wait_result
from test_experiment import clock, context, result
from test_no_contact_capture import begin_no_contact, finish_no_contact
from test_no_contact_data import no_contact_archive as synthetic_no_contact_archive


@pytest.mark.parametrize("kind,size", [("delta_S", 512), ("delta_lambda_9", 9)])
def test_contact_head_negative_supervision_never_trains_a_spatial_class(kind, size):
    torch = pytest.importorskip("torch")
    torch.set_num_threads(2)
    network = build_network(kind, contact_head=True)
    p, y, c = network(torch.randn(3, 16, size))
    assert p.shape == (3, 25) and y.shape == (3, 6) and c.shape == (3,)
    # NO_CONTACT has no spatial class: -1 is safe only because its mask is false.
    lp, ly, lc = masked_losses(p, y, torch.full((3,), -1), torch.zeros(3, 6),
        torch.zeros(3, dtype=torch.bool), torch.ones(3, dtype=torch.bool),
        c, torch.zeros(3), torch.ones(3, dtype=torch.bool))
    assert lp.item() == 0 and lc.item() > 0
    (lp + ly + lc).backward()
    assert network.position_head.weight.grad.abs().sum().item() == 0
    assert network.contact_head.weight.grad.abs().sum().item() > 0
    assert all(parameter.grad is not None and torch.isfinite(parameter.grad).all()
               for parameter in network.parameters())


def test_probability_gate_suppresses_no_contact_despite_force_bias_and_has_hysteresis():
    probabilities = [.2, .7, .5, .3, .5, .6]
    force = [2., 0., 0., 0., 0., 0.]
    def predictor(*args):
        return [1.] + [0.] * 24, force, probabilities.pop(0)
    runtime, frames, _ = setup_runtime(predictor)
    runtime.metadata.update(schema=CONTACT_SCHEMA, contact_definition=deepcopy(CONTACT_DEFINITION))
    # Leave room for later generated timestamps without creating future frames.
    frames = [{**frame, "host_rx_monotonic_ns": frame["host_rx_monotonic_ns"] - 300_000_000} for frame in frames]
    try:
        for frame in frames:
            runtime.submit(frame)
        result = wait_result(runtime)["prediction"]
        assert result["valid"] and result["force_norm_N"] == 2
        assert result["contact"] is False and result["point_id"] is None
        assert result["status"] == "无接触" and result["contact_probability"] == .2
        assert result["contact_method"] == "binary_contact_head_v1"
        expected = [True, True, False, False, True]
        for index, contact in enumerate(expected, 1):
            # A contact head can accept light contact without a force-derived threshold.
            force[:] = [.01, 0., 0., 0., 0., 0.]
            runtime.submit({**frames[-1], "frame_id": 15 + index,
                "host_rx_monotonic_ns": frames[-1]["host_rx_monotonic_ns"] + index * 40_000_000})
            result = wait_result(runtime, index + 1)["prediction"]
            assert result["contact"] is contact
            assert result["point_id"] == ("P01" if contact else None)
    finally:
        runtime.close()


def test_legacy_runtime_keeps_force_gate_without_contact_probability():
    runtime, frames, _ = setup_runtime(lambda *args: ([1.] + [0.] * 24, [.01, 0., 0., 0., 0., 0.]))
    try:
        for frame in frames:
            runtime.submit(frame)
        result = wait_result(runtime)["prediction"]
        assert result["contact"] is False and result["point_id"] is None
        assert result["contact_probability"] is None
        assert result["contact_method"] == "legacy_predicted_force_hysteresis_v1"
    finally:
        runtime.close()


@pytest.mark.parametrize("kind,size", [("delta_S", 512), ("delta_lambda_9", 9)])
def test_real_torch_evaluation_returns_sigmoid_contact_independent_of_wrench(kind, size):
    torch = pytest.importorskip("torch")
    network = build_network(kind, contact_head=True)
    with torch.no_grad():
        network.contact_head.weight.zero_(); network.contact_head.bias.fill_(-8.)
        network.wrench_head.weight.zero_(); network.wrench_head.bias.copy_(torch.tensor([2., 0., 0., 0., 0., 0.]))
    runtime, frames, reference = setup_runtime()
    runtime._predictor = None
    metadata = {**runtime.metadata, "schema": CONTACT_SCHEMA, "input_kind": kind,
                "input_normalization": {"mean": [0.] * size, "scale": [1.] * size},
                "output_normalization": {"mean": [0.] * 6, "scale": [1.] * 6}}
    if kind == "delta_lambda_9":
        reference = {**reference, "lambda0_nm": [1500.] * 9}
        frames = [{**frame, "centers_nm": [1500.001] * 9} for frame in frames]
    try:
        scores, wrench, probability = runtime._evaluate(frames, reference, network, metadata)
        assert len(scores) == 25 and wrench == [2., 0., 0., 0., 0., 0.]
        assert probability == pytest.approx(1 / (1 + np.exp(8)))
    finally:
        runtime.close()


def test_contact_metrics_separate_no_contact_false_positives_and_low_force_position():
    data = {"position": np.asarray([-1, -1, 0, 1]),
            "position_mask": np.asarray([False, False, True, True]),
            "wrench_mask": np.ones(4, dtype=bool),
            "wrench": np.asarray([[0.] * 6, [0.] * 6, [.2] + [0.] * 5, [2.] + [0.] * 5]),
            "contact": np.asarray([0., 0., 1., 1.], dtype=np.float32),
            "contact_mask": np.ones(4, dtype=bool),
            "windows": [{"phase": phase} for phase in ("no_contact", "preload", "loading", "loading")]}
    logits = np.zeros((4, 25)); logits[:, 0] = 1
    report = _metrics(data, np.arange(4), logits, data["wrench"], np.asarray([.1, .8, .2, .9]))
    assert report["contact"]["no_contact_false_positive_rate"] == .5
    assert report["contact"]["contact_recall"] == .5
    assert report["contact"]["true_negative"] == 1
    assert report["position_count"] == 2
    assert report["position_by_force_norm_N"]["0.1–1"] == {"count": 1, "accuracy": 1.}
    assert report["position_by_force_norm_N"]["1–3"] == {"count": 1, "accuracy": 0.}


def test_v2_bundle_loads_explicit_three_heads_and_rejects_definition_changes(tmp_path):
    old, _, parent = saved_parent(tmp_path)
    legacy, legacy_network = load_bundle(tmp_path / "spectral_models" / "Sparent")
    assert legacy["schema"] == SCHEMA and not hasattr(legacy_network, "contact_head")
    metadata = {key: value for key, value in parent.items() if key != "schema"}
    metadata.update(contact_definition=deepcopy(CONTACT_DEFINITION), contact_label_rule=deepcopy(CONTACT_LABEL_RULE))
    saved = save_bundle(tmp_path, "Scontact", build_network(contact_head=True), metadata)
    assert saved["schema"] == CONTACT_SCHEMA
    loaded, network = load_bundle(tmp_path / "spectral_models" / "Scontact")
    assert hasattr(network, "contact_head") and loaded == saved
    for key in ("on_probability", "off_probability"):
        changed = deepcopy(saved)
        changed["contact_definition"][key] += .01
        changed["metadata_sha256"] = changed["model_sha256"] = metadata_digest(changed)
        with pytest.raises(ValueError, match="接触头定义"):
            validate_metadata(changed)
    changed = deepcopy(saved); changed["schema"] = SCHEMA
    changed["metadata_sha256"] = changed["model_sha256"] = metadata_digest(changed)
    with pytest.raises(ValueError, match="网络"):
        validate_metadata(changed)


def test_scratch_trains_contact_head_reports_real_masks_and_v2_finetunes(tmp_path):
    old, new, _ = saved_parent(tmp_path)
    state, _ = run_worker(tmp_path, [old.name], {"epochs": 1, "batch_size": 64})
    assert state["status"] == "completed", state
    parent, _ = load_bundle(tmp_path / "spectral_models" / state["model_id"])
    assert parent["schema"] == CONTACT_SCHEMA
    report = parent["evaluation"]["train"]["contact"]
    assert report["contact_count"] > 0 and report["no_contact_count"] > 0
    assert parent["contact_definition"]["hardware_calibrated"] is False
    state2, _ = run_worker(tmp_path, [new.name], fine_options(base_model_id=state["model_id"]))
    assert state2["status"] == "completed", state2
    child, network = load_bundle(tmp_path / "spectral_models" / state2["model_id"])
    assert child["schema"] == CONTACT_SCHEMA and hasattr(network, "contact_head")
    assert child["evaluation"]["finetuning_comparison"]["parent"]["contact"]["count"] > 0


def test_legacy_finetune_rejects_explicit_no_contact_before_starting_worker(tmp_path):
    _, new, _ = saved_parent(tmp_path)
    file = new / "manifest.json"
    manifest = json.loads(file.read_text(encoding="utf-8"))
    manifest["final"]["trials"][0].update(point_id="NO_CONTACT", sample_label="no_contact",
                                         label_version="contact_label_v1")
    file.write_text(json.dumps(manifest), encoding="utf-8")
    options = checked_options(fine_options())
    with pytest.raises(ValueError, match="从头训练"):
        prepare_training(tmp_path, [new.name], options)


def test_v2_parent_can_finetune_only_new_no_contact_supervision_without_spatial_labels(tmp_path):
    old, _, _ = saved_parent(tmp_path)
    state, _ = run_worker(tmp_path, [old.name], {"epochs": 1, "batch_size": 64})
    assert state["status"] == "completed", state
    negative = synthetic_no_contact_archive(tmp_path, trials_count=1)
    # The new source contributes to the contact loss even when six-axis loss is disabled.
    child_state, _ = run_worker(tmp_path, [negative.name], fine_options(
        base_model_id=state["model_id"], wrench_loss_weight=0.))
    assert child_state["status"] == "completed", child_state
    metadata, _ = load_bundle(tmp_path / "spectral_models" / child_state["model_id"])
    assert metadata["schema"] == CONTACT_SCHEMA
    assert metadata["trained_point_ids"] == ["P01", "P02"]
    assert metadata["training_sampling"]["explicit_no_contact_selected_count"] > 0
    assert negative.name + ":T0001" in metadata["training_trial_ids"]


def test_only_no_contact_cannot_claim_a_trained_position_model(tmp_path):
    negative = synthetic_no_contact_archive(tmp_path, trials_count=1)
    state, _ = run_worker(tmp_path, [negative.name], {"epochs": 1})
    assert state["status"] == "failed" and "接触点位标签" in state["error"]
    assert not (tmp_path / "spectral_models").exists()


@pytest.mark.parametrize("capture_mode,method,size", [("full_spectrum", "spectral_gru", 512),
                                                       ("peak_drift", "drift_gru", 9)])
def test_saved_recorder_no_contact_and_real_press_labels_train_three_heads(tmp_path, clock, capture_mode, method, size):
    manager, tick = begin_no_contact(tmp_path, clock, capture_mode=capture_mode)
    try:
        for _ in range(30):
            tick()
        finish_no_contact(manager, tick, clock)
        manager.select_point("P01")
        manager.trial("start", context(clock.now))
        assert manager.training_pause_acknowledged()
        for _ in range(51):
            tick()
        for _ in range(24):
            tick([3.1, 0., 0., 0., 0., 0.])
        assert manager.phase == "release"
        for _ in range(5):
            tick()
        finish_no_contact(manager, tick, clock)
        path = manager.writer.path
        if capture_mode == "full_spectrum":
            invalid = result(clock.now, 990000)
            invalid.update(peaks=[], valid=False)
            manager.on_result(invalid, {"valid": False, "reason": "synthetic: no fitted peak source"})
        manager.stop()
    finally:
        manager.stop()
    state, _ = run_worker(tmp_path, [path.name], {"method": method, "epochs": 1, "batch_size": 32})
    assert state["status"] == "completed", state
    metadata, network = load_bundle(tmp_path / "spectral_models" / state["model_id"])
    assert metadata["schema"] == CONTACT_SCHEMA
    assert metadata["architecture"]["input_size"] == size and hasattr(network, "contact_head")
    assert metadata["trained_point_ids"] == ["P01"]
    assert metadata["training_sampling"]["schema"] == "spectral_train_sampling_v2"
    assert metadata["evaluation"]["train"]["contact"]["no_contact_count"] > 0
    assert metadata["evaluation"]["train"]["contact"]["contact_count"] > 0
