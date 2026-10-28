"""Actual CPU weights across training, inference, catalog and model recycling.

The source frames and fitted centers are explicitly synthetic temporary data;
these tests make no physical accuracy or unloaded acceptance claim.
"""
from copy import deepcopy
import hashlib
import time

import numpy as np
import pytest

from ninefbg.analysis import AnalysisController
from ninefbg.drift_features import FBG_IDS, drift_pm, make_drift_frame
from ninefbg.model_history import ModelHistory
from ninefbg.spectral_model import load_bundle
from test_drift_pipeline import drift_archive, worker
from test_experiment import clock, context, force, result, spectrum


def _tree_hashes(root):
    return {file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in root.rglob("*") if file.is_file()}


def _fitted_frame(clock, number, offset_nm=0.):
    raw = spectrum(clock.advance(40), number)
    fitted = result(raw["host_rx_monotonic_ns"], number)
    fitted["peaks"] = [{"id": fbg, "valid": True, "stale": False, "value_frame_id": number,
                        "center_nm": raw["wavelength_nm"][45+index*45]+offset_nm}
                       for index, fbg in enumerate(FBG_IDS)]
    return make_drift_frame(fitted, raw), raw


@pytest.mark.parametrize("compact_capture", [False, True])
def test_trained_nine_bundle_real_torch_inference_and_catalog_recycle_roundtrip(tmp_path, clock, compact_capture):
    torch = pytest.importorskip("torch")
    if compact_capture:
        from test_peak_drift_pipeline import compact_archive
        archive = compact_archive(tmp_path, clock)
    else:
        archive = drift_archive(tmp_path, clock)
    source_before = _tree_hashes(archive)
    trained = worker(tmp_path, [archive.name], {"method": "drift_gru", "epochs": 1, "batch_size": 32})
    assert trained["status"] == "completed", trained
    model_id = trained["model_id"]
    package = tmp_path/"spectral_models"/model_id
    bytes_before = {file.name: file.read_bytes() for file in package.iterdir() if file.is_file()}
    metadata, _ = load_bundle(package)
    assert metadata["input_kind"] == "delta_lambda_9" and metadata["method"] == "drift_gru"

    analysis = AnalysisController(tmp_path)
    hook = None
    try:
        catalog = analysis.models()
        listed = next(row for row in catalog if row["model_id"] == model_id)
        assert listed["input_kind"] == "delta_lambda_9"
        assert listed["method"] == "drift_gru" and "寻峰漂移" in listed["method_label"]
        loaded = analysis.load(model_id)
        assert loaded["method"] == "drift_gru" and loaded["input_kind"] == "delta_lambda_9"
        runtime = analysis.spectral_runtime
        assert runtime._predictor is None  # Exercise real _evaluate + trained torch weights.
        observed = []
        hook = runtime.model.register_forward_pre_hook(
            lambda module, arguments: observed.append(arguments[0].detach().cpu().clone()))
        analysis.request_truth_zero(context(clock.now))
        start = clock.now
        for number in range(50):
            feature, raw = _fitted_frame(clock, 100_000+number)
            analysis.on_force(force(raw["host_rx_monotonic_ns"], 200_000+number))
            analysis.on_spectrum(raw)
            analysis.on_result(feature)
        assert clock.now-start == 2_000_000_000
        assert runtime.snapshot()["reference"] is None  # First-to-last span is still 1.96 s.
        feature, raw = _fitted_frame(clock, 100_050)
        analysis.on_force(force(raw["host_rx_monotonic_ns"], 200_050))
        analysis.on_result(feature)
        reference = deepcopy(runtime.snapshot()["reference"])
        assert reference and len(reference["lambda0_nm"]) == 9
        assert reference["trigger"] == "operator_unloaded_confirmation"
        assert len(reference["source_frame_ids"]) == 51
        assert reference["source_timestamps_ns"][-1]-reference["source_timestamps_ns"][0] == 2_000_000_000

        for number in range(51, 67):
            feature, raw = _fitted_frame(clock, 100_000+number, offset_nm=.002)
            analysis.on_result(feature)
        desired = feature["frame_id"]
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            inference = runtime.snapshot()
            if inference["prediction"].get("frame_id") == desired:
                break
            time.sleep(.005)
        else:
            pytest.fail("真实九路 CPU 网络未完成："+repr(runtime.snapshot()))
        prediction = inference["prediction"]
        assert prediction["valid"] and prediction["input_kind"] == "delta_lambda_9"
        assert len(prediction["wrench_pred"]) == 6 and np.isfinite(prediction["wrench_pred"]).all()
        assert prediction["raw_point_id"] in {f"P{i:02d}" for i in range(1, 26)}
        assert prediction["window_frame_ids"] == list(range(100_051, 100_067))
        assert runtime.snapshot()["reference"] == reference
        assert observed and observed[-1].shape == (1, 16, 9)
        assert torch.isfinite(observed[-1]).all()
        np.testing.assert_allclose(drift_pm(feature, reference), [2.]*9, atol=1e-6)

        # Independently evaluate the exact completed evidence using the real
        # loaded heads and fixed train-only normalizers; no predictor is mocked.
        evidence = runtime.evidence_for(desired, feature["stream_epoch"], reference["reference_id"])
        delta = np.asarray([drift_pm(row, reference) for row in evidence["window"]], dtype=np.float32)
        normalizer = metadata["input_normalization"]
        expected_input = ((delta-np.asarray(normalizer["mean"], dtype=np.float32)) /
                          np.asarray(normalizer["scale"], dtype=np.float32))
        np.testing.assert_allclose(observed[-1].numpy()[0], expected_input, atol=1e-6)
        with torch.inference_mode():
            logits, wrench, contact_logits = runtime.model(torch.from_numpy(expected_input).unsqueeze(0))
        assert logits.shape == (1, 25) and wrench.shape == (1, 6) and contact_logits.shape == (1,)
        assert prediction["contact_probability"] == pytest.approx(float(torch.sigmoid(contact_logits[0])))
        assert prediction["contact_method"] == "binary_contact_head_v1"
        output = metadata["output_normalization"]
        expected_wrench = (wrench[0].numpy()*np.asarray(output["scale"], dtype=np.float32) +
                           np.asarray(output["mean"], dtype=np.float32))
        np.testing.assert_allclose(prediction["wrench_pred"], expected_wrench, atol=1e-6)
        assert prediction["raw_point_id"] == f"P{int(logits[0].argmax())+1:02d}"
        snapshot = analysis.snapshot(force=force(clock.now, 300_000), experiment={"running": False},
                                     source_kind="hardware")
        assert snapshot["input_kind"] == "delta_lambda_9" and snapshot["test_reference"]["ready"]
        assert snapshot["prediction"]["valid"]

        analysis.stop_inference()
        history = ModelHistory(tmp_path)
        deleted = history.delete([model_id])
        removed = next(row for row in deleted["deleted_models"] if row["model_id"] == model_id)
        assert removed["method"] == "drift_gru" and removed["input_kind"] == "delta_lambda_9"
        assert removed["restorable"] and not removed["loadable"]
        assert model_id not in {row["model_id"] for row in analysis.models()}
        history.restore([model_id])
        assert {file.name: file.read_bytes() for file in package.iterdir() if file.is_file()} == bytes_before
        restored, _ = load_bundle(package)
        assert restored == metadata
        assert analysis.load(model_id)["input_kind"] == "delta_lambda_9"
        assert _tree_hashes(archive) == source_before
    finally:
        if hook is not None:
            hook.remove()
        analysis.close()
