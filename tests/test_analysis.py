"""Hardware-shaped storage fixtures test software; no physical qualification."""
from copy import deepcopy
import json
import threading
import time

import numpy as np
import pytest

from ninefbg.analysis import AnalysisController, load_rows
from ninefbg.capture_setup import SETUP_POLICY
from ninefbg.experiment import coordinate_definition, default_point_grid
from ninefbg.storage import SessionWriter
from ninefbg.tracking import PeakTracker, ALGORITHM_VERSION


@pytest.fixture(autouse=True)
def fixture_features(monkeypatch):
    # Reprocessing has its own numerical integration tests; these fixtures
    # isolate storage, controller jobs and dataset selection from optical fits.
    def extract(path, manifest, **kwargs):
        return [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    monkeypatch.setattr("ninefbg.reprocess.reprocess_session", extract)


def legacy_point_grid():
    return [{"id": f"P{i+1:02d}", "row": i // 5 + 1, "col": i % 5 + 1,
             "x_norm": (i % 5) / 4, "y_norm": (i // 5) / 4,
             "u": (i % 5) / 4, "v": (i // 5) / 4} for i in range(25)]


def session_fixture(root, batch, *, aborted=False, marked=False, new_coordinates=False):
    settings = {"finger_id": "software-fixture", "batch": batch, "force_sign": -1,
                "force_direction_confirmed": True, "coordinates_confirmed": True,
                "point_grid": legacy_point_grid()}
    if new_coordinates:
        settings.update(point_grid=default_point_grid(), coordinate_definition=coordinate_definition())
    writer = SessionWriter(root / "calibration_sessions", {"kind": "9fbg_calibration_experiment_v1", "settings": settings})
    config = PeakTracker().config_version
    common = {"baseline_id": "B0001", "valid": True, "source_kind": "hardware",
              "algorithm_version": ALGORITHM_VERSION, "config_version": config,
              "split": batch, "session_id": writer.metadata["session_id"]}
    for i in range(4):
        writer.submit("results", {**common, "trial_id": None, "point_id": None, "phase": "unloaded",
                                  "force_N": 0.0, "x_pm": [(.01 if i % 2 else -.01)] * 9})
    rng = np.random.default_rng(12)
    trials = []
    for i in range(25):
        trial = f"T{i+1:04d}"
        trials.append({"trial_id": trial, "status": "aborted" if aborted and i == 0 else "completed",
                       "manual_mark": ["移开"] if marked and i == 1 else []})
        slope = rng.normal(size=9) * 30
        for force in (.5, 1, 2):
            writer.submit("results", {**common, "trial_id": trial, "point_id": f"P{i+1:02d}",
                                      "phase": "loading", "force_N": force, "x_pm": (slope * force).tolist()})
    # These labels must never become classes merely because P01 was selected.
    writer.submit("results", {**common, "trial_id": "T0001", "point_id": "P01", "phase": "loading", "force_N": .001, "x_pm": [.001]*9})
    writer.submit("results", {**common, "trial_id": "T0001", "point_id": None, "phase": "preload", "force_N": 0, "x_pm": [0]*9})
    writer.submit("results", {**common, "trial_id": "T0002", "point_id": "P02", "phase": "preload", "force_N": -.03, "force_noise_threshold_N": .02, "x_pm": [.01]*9})
    writer.submit("results", {**common, "trial_id": "T0001", "point_id": "P01", "phase": "preload", "force_N": .5, "x_pm": [10]*9})
    writer.submit("results", {**common, "trial_id": "T0001", "point_id": "P01", "phase": "preload", "force_N": -.5, "x_pm": [10]*9})
    for kind in ("raw_spectra", "raw_force", "events"):
        writer.submit(kind, {"fixture": "software-only", "frame_id": 1})
    writer.close({"trials": trials})
    return writer


def wait_job(controller):
    limit = time.perf_counter() + 10
    while controller.busy and time.perf_counter() < limit:
        time.sleep(.01)
    assert not controller.busy
    assert not controller.error, controller.error


def authorize_test_reference(controller, tracker):
    previous = deepcopy(tracker)
    previous["reference_version"] -= 1
    controller.request_truth_zero({"tracker": previous, "frame": {"stream_epoch": tracker["stream_epoch"]},
                                   "force_connected": False})
    tracker["host_rx_monotonic_ns"] = time.perf_counter_ns()
    snapshot = controller.snapshot(tracker, source_kind="hardware")
    assert snapshot["test_reference"]["ready"]
    return snapshot


def test_rows_are_scoped_by_session_and_whole_bad_trials_are_removed(tmp_path):
    a = session_fixture(tmp_path, "A", aborted=True, marked=True)
    b = session_fixture(tmp_path, "B")
    rows, provenance = load_rows(tmp_path, [a.path.name, b.path.name])
    assert not any(r["point_id"] in {"P01", "P02"} and r["session_id"] == a.path.name for r in rows)
    assert all(r["trial_id"].startswith(r["session_id"] + ":") for r in rows)
    assert not any(r["phase"] == "preload" or r["point_id"] and r["force_N"] <= .05 for r in rows)
    assert provenance["omitted"]
    assert not any(r.get("recorded_phase") == "preload" and r["session_id"] == a.path.name for r in rows)
    preloads = [r for r in rows if r.get("recorded_phase") == "preload"]
    assert len(preloads) == 2 and all(row["session_id"] == b.path.name for row in preloads)
    assert all(row["phase"] == "unloaded" and row["point_id"] is None for row in preloads)
    assert {row["source_trial_id"] for row in preloads} == {"T0001", "T0002"}
    assert all(row["trial_id"] == b.path.name + ":" + row["source_trial_id"] for row in preloads)
    assert {row["force_N"] for row in preloads} == {0, -.03}
    assert provenance["omitted"]["预采样力超过无载噪声范围，不作为无接触样本"] == 2
    with (b.path / "raw_force.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{}\n')
    with pytest.raises(ValueError, match="哈希"):
        load_rows(tmp_path, [b.path.name])


@pytest.mark.parametrize("policy", [None, SETUP_POLICY])
def test_legacy_training_load_uses_recorded_setup_policy_and_keeps_sources_immutable(tmp_path, policy):
    writer = session_fixture(tmp_path, "A", new_coordinates=True)
    manifest_path = writer.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["settings"].update(force_direction_confirmed=False, coordinates_confirmed=False)
    if policy is not None:
        manifest["settings"]["setup_policy"] = policy
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    before = {file.name: file.read_bytes() for file in writer.path.iterdir()}
    if policy is None:
        with pytest.raises(ValueError, match="方向或点位确认"):
            load_rows(tmp_path, [writer.path.name])
    else:
        rows, _ = load_rows(tmp_path, [writer.path.name])
        assert len(rows) > 25
        assert {row["point_id"] for row in rows if row["point_id"]} == {f"P{i:02d}" for i in range(1, 26)}
    assert before == {file.name: file.read_bytes() for file in writer.path.iterdir()}


def test_guided_rounds_need_explicit_passed_quality_even_if_marked_completed(tmp_path):
    writer = session_fixture(tmp_path, "A", new_coordinates=True)
    manifest_path = writer.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["settings"].update(guided_acquisition=True, repetitions_per_point=3)
    for trial in manifest["final"]["trials"]:
        trial["quality"] = {"accepted": True, "criteria_version": "guided_round_v1"}
    manifest["final"]["trials"][0].pop("quality")
    manifest["final"]["trials"][1]["quality"]["accepted"] = False
    manifest["final"]["trials"][2]["quality"]["criteria_version"] = "unknown"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    immutable = {name: (writer.path / name).read_bytes() for name in manifest["files"]}
    rows, provenance = load_rows(tmp_path, [writer.path.name])
    assert not any(row.get("point_id") in {"P01", "P02", "P03"} for row in rows)
    assert any(row.get("point_id") == "P04" for row in rows)
    assert any(row["phase"] == "unloaded" and row["point_id"] is None for row in rows)
    assert provenance["omitted"]["引导采集轮次缺少通过的完整质量检查"] > 0
    assert immutable == {name: (writer.path / name).read_bytes() for name in manifest["files"]}


def test_job_saves_reloadable_model_and_live_prediction_does_not_use_force(tmp_path):
    a, b = session_fixture(tmp_path, "A"), session_fixture(tmp_path, "B")
    controller = AnalysisController(tmp_path)
    controller.start_job("build", [a.path.name, b.path.name], {"include_mlp": False})
    wait_job(controller)
    assert controller.model_id and controller.report_id
    assert controller.full_report()["formal_ready"] is False
    assert controller.bundle["grid"]["point_grid"] == legacy_point_grid()
    assert controller.bundle["grid"]["order"] == "P01_to_P25_row_major"
    assert "coordinate_definition" not in controller.bundle["grid"]
    loaded = AnalysisController(tmp_path)
    loaded.truth_zero = {"ready": True, "offsets": [0]*6, "epoch": "old"}
    loaded.load(controller.model_id)
    assert loaded.bundle["bundle_sha256"] == controller.bundle["bundle_sha256"]
    assert loaded.truth_zero["ready"] is False and loaded.truth_zero.get("offsets") is None
    template = loaded.bundle["models"]["linear"]["templates"][0]["slope_pm_per_N"]
    origin = loaded.bundle["baseline_context"]["origin_pm"]
    tracker = {"algorithm_version": ALGORITHM_VERSION, "config_version": PeakTracker().config_version,
               "stream_epoch": "optical-test", "reference_version": 2, "host_rx_monotonic_ns": time.perf_counter_ns(),
               "frame_id": 4, "peaks": [{"id": f"FBG{i+1}", "valid": True, "drift_nm": (value+origin[i])/1000} for i,value in enumerate(template)]}
    assert loaded.snapshot(tracker, source_kind="hardware")["prediction"]["status"] == "无效", "Loading a model cannot authorize an old display reference"
    authorize_test_reference(loaded, tracker)
    without_force = loaded.snapshot(tracker, source_kind="hardware")["prediction"]
    assert without_force["status"] == "有效估计"
    with_unrelated_force = loaded.snapshot(tracker, {"force_values": [999]*6, "stale": False}, source_kind="hardware")["prediction"]
    for key in ("status", "point_id", "force_N", "raw_force_N"):
        assert without_force[key] == with_unrelated_force[key]
    assert without_force["truth_force_N"] is None
    loaded.truth_zero = {"ready": True, "offsets": [0,0,1,0,0,0], "epoch": "force-test", "force_sign": -1}
    force = {"force_values": [0,0,-1,0,0,0], "stale": False, "stream_epoch": "force-test"}
    sync = {"valid": True, "spectrum_frame_id": 4, "force_stream_epoch": "force-test", "force_values": force["force_values"]}
    experiment = {"running": False, "baseline": {"ready": True, "force_zero": [0,0,-.2,0,0,0], "force_epoch": "force-test"}}
    stopped = loaded.snapshot(tracker, force, sync, experiment, source_kind="hardware")["prediction"]
    assert stopped["truth_force_N"] == 2, "A stopped session must not override the newly confirmed independent zero"
    experiment.update(running=True, settings={"finger_id": "another-piece", "point_grid": default_point_grid(), "force_sign": 1})
    mismatched = loaded.snapshot(tracker, force, sync, experiment, source_kind="hardware")["prediction"]
    assert mismatched["status"] == "无效" and mismatched["truth_force_N"] is None and mismatched["error_N"] is None
    loaded.load(controller.model_id)
    assert not loaded.test_reference["ready"] and not loaded.test_reference["pending"]
    assert loaded.snapshot(tracker, source_kind="hardware")["prediction"]["status"] == "无效"
    assert loaded.snapshot(tracker, source_kind="replay")["prediction"]["status"] == "无效"
    stale = deepcopy(tracker)
    stale["display_stale"] = True
    assert loaded.snapshot(stale)["prediction"]["status"] == "无效"


def test_empty_no_data_and_formal_bypass_never_generate_model(tmp_path):
    controller = AnalysisController(tmp_path)
    assert not tmp_path.joinpath("models").exists()
    assert controller.snapshot()["prediction"]["point_id"] is None
    with pytest.raises(ValueError):
        controller.start_job("build", [])
    with pytest.raises(ValueError, match="正式训练门"):
        controller.start_job("build", ["fake"], {"allow_synthetic_diagnostics": True})
    controller.start_job("preview", ["missing"])
    limit = time.perf_counter()+3
    while controller.busy and time.perf_counter() < limit:
        time.sleep(.01)
    assert controller.error and not tmp_path.joinpath("models").exists()


def test_truth_zero_is_held_separate_and_invalidated_by_epoch(tmp_path, monkeypatch):
    monkeypatch.setattr("ninefbg.analysis.time.perf_counter_ns", lambda: 100)
    controller = AnalysisController(tmp_path)
    controller.request_truth_zero({"force_connected": True, "force": {"stream_epoch": "force-A"}})
    frame = {"source_kind": "hardware", "time_quality": "host_rx_approx", "stream_epoch": "force-A", "axes": ["Fx","Fy","Fz","Mx","My","Mz"],
             "units": ["N","N","N","N·m","N·m","N·m"], "force_values": [0,0,-.2,0,0,0]}
    for i in range(12):
        controller.on_force({**frame, "frame_id": i, "host_rx_monotonic_ns": 101+i*40_000_000})
    assert controller.truth_zero["ready"]
    assert controller.truth_zero["offsets"][2] == -.2
    controller.snapshot(force={"stale": False, "stream_epoch": "force-B"})
    assert not controller.truth_zero["ready"]


@pytest.mark.parametrize("force", [None, {"stale": True, "stream_epoch": "force-A"},
                                   {"stale": False, "stream_epoch": "force-B"}])
def test_pending_truth_zero_is_cancelled_by_disconnect_staleness_or_epoch(tmp_path, monkeypatch, force):
    monkeypatch.setattr("ninefbg.analysis.time.perf_counter_ns", lambda: 100)
    controller = AnalysisController(tmp_path)
    controller.request_truth_zero({"force_connected": True, "force": {"stream_epoch": "force-A"}})
    frame = {"source_kind": "hardware", "time_quality": "host_rx_approx", "stream_epoch": "force-A",
             "axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"], "units": ["N", "N", "N", "N·m", "N·m", "N·m"],
             "force_values": [0, 0, -.2, 0, 0, 0]}
    for i in range(5):
        controller.on_force({**frame, "frame_id": i, "host_rx_monotonic_ns": 101+i*40_000_000})
    assert controller.truth_zero["pending"] and controller.truth_zero["count"] == 5
    zero = controller.snapshot(force=force)["truth_zero"]
    assert not zero["pending"] and not zero["ready"] and zero.get("offsets") is None
    assert "重新确认" in zero["reason"] and not controller._zero_samples


def runtime_fixture(tmp_path, monkeypatch):
    controller = AnalysisController(tmp_path)
    controller.bundle = {"algorithm_version": "test-algo", "config_version": "test-config"}
    controller.inference_active = True
    tracker = {"algorithm_version": "test-algo", "config_version": "test-config", "stream_epoch": "optical-A",
               "reference_version": 2, "frame_id": 4, "host_rx_monotonic_ns": time.perf_counter_ns(),
               "peaks": [{"id": f"FBG{i}", "valid": True, "drift_nm": .01} for i in range(1, 10)]}
    monkeypatch.setattr("ninefbg.inversion.predict", lambda *args: {"status": "有效估计", "point_id": "P01", "force_N": 1})
    return controller, tracker


def test_stop_discards_inflight_legacy_prediction_and_later_reads_never_predict(tmp_path, monkeypatch):
    controller, tracker = runtime_fixture(tmp_path, monkeypatch)
    authorize_test_reference(controller, tracker)
    entered, release = threading.Event(), threading.Event()
    calls, replies = [], []
    def blocked_predict(*args):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {"status": "有效估计", "point_id": "P01", "force_N": 1}
    monkeypatch.setattr("ninefbg.inversion.predict", blocked_predict)
    reader = threading.Thread(target=lambda: replies.append(controller.snapshot(tracker, source_kind="hardware")))
    reader.start()
    try:
        assert entered.wait(2)
        controller.stop_inference()
    finally:
        release.set()
        reader.join(3)
    assert not reader.is_alive()
    assert not replies[0]["inference_active"]
    assert replies[0]["prediction"]["status"] == "已结束"
    assert replies[0]["prediction"]["point_id"] is replies[0]["prediction"]["force_N"] is None
    for _ in range(5):
        assert controller.snapshot(tracker, source_kind="hardware")["prediction"]["status"] == "已结束"
    assert len(calls) == 1
    with pytest.raises(ValueError, match="重新载入"):
        controller.request_truth_zero({})


def test_stop_during_legacy_build_preserves_loaded_model_and_candidate_stays_offline(tmp_path, monkeypatch):
    a, b = session_fixture(tmp_path, "A"), session_fixture(tmp_path, "B")
    controller = AnalysisController(tmp_path)
    controller.bundle = {"algorithm_version": "test-algo", "config_version": "test-config"}
    controller.inference_active = True
    controller.model_id = "Mprevious"
    previous = deepcopy(controller.bundle)
    entered, release = threading.Event(), threading.Event()
    from ninefbg import inversion
    actual_fit = inversion.fit_models
    def delayed_fit(*args):
        entered.set()
        assert release.wait(3)
        return actual_fit(*args)
    monkeypatch.setattr(inversion, "fit_models", delayed_fit)
    controller.start_job("build", [a.path.name, b.path.name], {"include_mlp": False})
    try:
        assert entered.wait(2)
        controller.stop_inference()
        assert controller.busy
    finally:
        release.set()
    wait_job(controller)
    assert not controller.inference_active and controller.model_id == "Mprevious"
    assert controller.bundle == previous
    candidate = controller.full_report()["model_id"]
    assert candidate != "Mprevious" and (tmp_path/"models"/(candidate+".json")).is_file()
    controller.load(candidate)
    assert controller.inference_active and not controller.test_reference["ready"]


def test_test_reference_requires_new_version_and_post_confirmation_valid_frame_without_px6d(tmp_path, monkeypatch):
    controller, tracker = runtime_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr("ninefbg.analysis.time.perf_counter_ns", lambda: 100)
    tracker.update(reference_version=1, host_rx_monotonic_ns=90)
    controller.request_truth_zero({"tracker": tracker, "frame": {"stream_epoch": "optical-A"}, "force_connected": False})
    assert controller.snapshot(tracker, source_kind="hardware")["test_reference"]["pending"]
    tracker.update(reference_version=2, host_rx_monotonic_ns=99)
    assert not controller.snapshot(tracker, source_kind="hardware")["test_reference"]["ready"]
    tracker["host_rx_monotonic_ns"] = 101
    tracker["peaks"][0]["valid"] = False
    assert not controller.snapshot(tracker, source_kind="hardware")["test_reference"]["ready"]
    tracker["peaks"][0]["valid"] = True
    ready = controller.snapshot(tracker, source_kind="hardware")
    assert ready["test_reference"]["ready"] and not ready["truth_zero"]["ready"]
    assert ready["prediction"]["status"] == "有效估计" and ready["prediction"]["truth_force_N"] is None


@pytest.mark.parametrize("field,value", [("stream_epoch", "optical-B"), ("reference_version", 3),
                                         ("reference_pending", True), ("display_stale", True),
                                         ("algorithm_version", "changed"), ("config_version", "changed")])
def test_authorized_test_reference_is_invalidated_by_stream_reference_or_quality_change(tmp_path, monkeypatch, field, value):
    controller, tracker = runtime_fixture(tmp_path, monkeypatch)
    authorize_test_reference(controller, tracker)
    tracker[field] = value
    snapshot = controller.snapshot(tracker, source_kind="hardware")
    assert not snapshot["test_reference"]["ready"] and not snapshot["test_reference"]["pending"]
    assert snapshot["prediction"]["status"] == "无效"


def test_running_experiment_requires_its_matching_reference_version_and_epoch(tmp_path, monkeypatch):
    controller, tracker = runtime_fixture(tmp_path, monkeypatch)
    baseline = {"ready": True, "reference_version": 2, "optical_epoch": "optical-A",
                "algorithm_version": "test-algo", "config_version": "test-config"}
    experiment = {"running": True, "baseline": baseline}
    assert controller.snapshot(tracker, experiment=experiment, source_kind="hardware")["prediction"]["status"] == "有效估计"
    baseline["reference_version"] = 1
    assert controller.snapshot(tracker, experiment=experiment, source_kind="hardware")["prediction"]["status"] == "无效"
    baseline.update(reference_version=2, optical_epoch="optical-B")
    assert controller.snapshot(tracker, experiment=experiment, source_kind="hardware")["prediction"]["status"] == "无效"


def test_service_registers_reference_before_fast_new_reference_commit(tmp_path, monkeypatch):
    from ninefbg.service import AcquisitionService
    controller, tracker = runtime_fixture(tmp_path, monkeypatch)
    tracker["reference_version"] = 1
    service = AcquisitionService(tmp_path, tmp_path)
    service.analysis = controller
    service.running, service.mode = True, "hardware"
    service.snapshot = lambda: {"tracker": deepcopy(tracker), "frame": {"stream_epoch": "optical-A"}, "force_connected": False}
    def fast_reference(confirmed):
        assert confirmed and controller.test_reference["pending"]
        assert controller.test_reference["previous_reference_version"] == 1
        tracker.update(reference_version=2, host_rx_monotonic_ns=time.perf_counter_ns())
        return controller.snapshot(tracker, source_kind="hardware")
    service.reference = fast_reference
    service.analysis_reference(True)
    assert controller.test_reference["ready"] and controller.test_reference["reference_version"] == 2


@pytest.mark.parametrize("field,value", [("finger_id", "another-piece"), ("force_sign", 1),
                                         ("point_grid", list(reversed(default_point_grid()))),
                                         ("coordinate_definition", coordinate_definition())])
def test_independent_evaluation_rejects_different_physical_definition(tmp_path, field, value):
    a, b, c = [session_fixture(tmp_path, batch) for batch in "ABC"]
    controller = AnalysisController(tmp_path)
    controller.start_job("build", [a.path.name, b.path.name], {"include_mlp": False})
    wait_job(controller)
    manifest = json.loads((c.path / "manifest.json").read_text(encoding="utf-8"))
    manifest["settings"][field] = value
    (c.path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    controller.start_job("evaluate", [c.path.name])
    limit = time.perf_counter() + 10
    while controller.busy and time.perf_counter() < limit:
        time.sleep(.01)
    assert not controller.busy and "与当前模型不一致" in controller.error
    assert controller.full_report()["kind"] == "build", "A mismatched test cannot replace the valid model report"


def test_build_keeps_c_for_explicit_independent_evaluation(tmp_path):
    sessions = [session_fixture(tmp_path, batch) for batch in "ABC"]
    controller = AnalysisController(tmp_path)
    controller.start_job("build", [writer.path.name for writer in sessions], {"include_mlp": False})
    limit = time.perf_counter() + 10
    while controller.busy and time.perf_counter() < limit:
        time.sleep(.01)
    assert not controller.busy and "C 批保留" in controller.error
    assert not (tmp_path / "models").exists()


def test_legacy_preview_preserves_absent_coordinate_definition_and_original_archive(tmp_path):
    session = session_fixture(tmp_path, "A")
    manifest_path = session.path / "manifest.json"
    original = manifest_path.read_bytes()
    controller = AnalysisController(tmp_path)
    controller.start_job("preview", [session.path.name])
    wait_job(controller)
    physical = controller.full_report()["provenance"]["physical_definition"]
    assert set(physical) == {"finger_id", "point_grid", "force_sign"}
    assert physical["point_grid"] == legacy_point_grid() and physical["point_grid"][0]["row"] == 1
    assert manifest_path.read_bytes() == original
    assert not (tmp_path / "models").exists()


def test_load_rows_does_not_mix_coordinate_definition_presence_even_with_identical_grids(tmp_path):
    a, b = session_fixture(tmp_path, "A"), session_fixture(tmp_path, "B")
    manifest_path = b.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["settings"]["coordinate_definition"] = coordinate_definition()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="不能混合"):
        load_rows(tmp_path, [a.path.name, b.path.name])


def test_new_model_saves_actual_coordinate_grid_and_rejects_legacy_evaluation(tmp_path):
    a, b, c = [session_fixture(tmp_path, batch, new_coordinates=True) for batch in "ABC"]
    controller = AnalysisController(tmp_path)
    controller.start_job("build", [a.path.name, b.path.name], {"include_mlp": False})
    wait_job(controller)
    grid = controller.bundle["grid"]
    assert grid["point_grid"] == default_point_grid()
    assert grid["point_grid"][0]["row"] == grid["point_grid"][0]["col"] == 0
    assert grid["coordinate_definition"] == coordinate_definition()
    assert grid["order"] == "P01_bottom_left_row_major_x_right_y_up"
    assert controller.full_report()["provenance"]["physical_definition"]["coordinate_definition"] == coordinate_definition()
    loaded = AnalysisController(tmp_path)
    loaded.load(controller.model_id)
    assert loaded.bundle["bundle_sha256"] == controller.bundle["bundle_sha256"]
    assert loaded.bundle["grid"] == grid
    loaded.start_job("evaluate", [c.path.name])
    wait_job(loaded)
    manifest_path = c.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["settings"].pop("coordinate_definition")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    loaded.start_job("evaluate", [c.path.name])
    limit = time.perf_counter() + 10
    while loaded.busy and time.perf_counter() < limit:
        time.sleep(.01)
    assert not loaded.busy and "与当前模型不一致" in loaded.error


@pytest.mark.parametrize("new_model", [False, True])
def test_live_prediction_compares_coordinate_definition_presence(tmp_path, monkeypatch, new_model):
    controller, tracker = runtime_fixture(tmp_path, monkeypatch)
    settings = {"finger_id": "same-piece", "point_grid": default_point_grid(), "force_sign": -1}
    definition = deepcopy(settings)
    if new_model:
        definition["coordinate_definition"] = coordinate_definition()
    controller.bundle["acquisition_provenance"] = {"physical_definition": definition}
    baseline = {"ready": True, "reference_version": 2, "optical_epoch": "optical-A",
                "algorithm_version": "test-algo", "config_version": "test-config"}
    experiment = {"running": True, "settings": deepcopy(definition), "baseline": baseline}
    assert controller.snapshot(tracker, experiment=experiment, source_kind="hardware")["prediction"]["status"] == "有效估计"
    if new_model:
        experiment["settings"].pop("coordinate_definition")
    else:
        experiment["settings"]["coordinate_definition"] = coordinate_definition()
    mismatch = controller.snapshot(tracker, experiment=experiment, source_kind="hardware")["prediction"]
    assert mismatch["status"] == "无效" and "定义与模型不一致" in mismatch["reason"]
