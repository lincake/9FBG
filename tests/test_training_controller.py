"""Background training software regressions; no device or physical accuracy data."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import threading
import time

import pytest

from ninefbg.analysis import AnalysisController
from ninefbg.pilot import load_pilot_model, validate_pilot_model
from ninefbg.service import AcquisitionService
from test_live_pilot import load_pilot_archive
from test_live_validation_api import http_service, post
from test_pilot_controller import archive_fixture, archive_bytes, finish, observe_worker


def test_training_progress_tracks_file_work_and_does_not_run_concurrent_jobs(tmp_path, monkeypatch):
    session = archive_fixture(tmp_path, monkeypatch)
    source = archive_bytes(session)
    ready, release = threading.Event(), threading.Event()
    def blocked_extract(path, manifest, progress=None):
        rows = [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 180
        progress({"processed": 90, "total": len(rows)})
        ready.set()
        assert release.wait(10)
        progress({"processed": len(rows), "total": len(rows)})
        return rows
    monkeypatch.setattr("ninefbg.reprocess.reprocess_session", blocked_extract)
    controller = AnalysisController(tmp_path)
    assert controller.snapshot()["training"]["status"] == "idle"
    events = []
    original = controller._training_progress
    def observed(value, phase):
        original(value, phase)
        with controller.lock:
            events.append(deepcopy(controller.training))
    monkeypatch.setattr(controller, "_training_progress", observed)
    done, workers = observe_worker(controller, monkeypatch)
    try:
        response = controller.start_job("train", [session.name], {"method": "linear"})
        assert response["job_id"].startswith("J") and response["training"]["job_id"] == response["job_id"]
        assert response["training"]["status"] == "running" and response["training"]["progress_percent"] == 0
        assert ready.wait(10)
        running = controller.snapshot()["training"]
        assert running["progress_percent"] == 27.5
        assert running["processed"] == 90 and running["total"] == 180
        assert running["busy"] and running["model_id"] is None
        running["progress_percent"] = 99
        assert controller.training["progress_percent"] == 27.5
        for kind in ("train", "preview"):
            with pytest.raises(ValueError, match="已有"):
                controller.start_job(kind, [session.name])
        with pytest.raises(ValueError, match="正在运行"):
            controller.load("Punknown")
        assert not (tmp_path / "pilot_models").exists()
    finally:
        release.set()
    finish(controller, done)
    final = controller.snapshot()["training"]
    assert final["status"] == "completed" and not final["busy"]
    assert final["progress_percent"] == 100 and final["model_id"].startswith("P")
    assert final["point_ids"] == ["P01", "P05", "P21", "P25"]
    assert final["method"] == "linear" and final["method_label"] == "加权最小二乘"
    assert final["session_ids"] == [session.name]
    percentages = [event["progress_percent"] for event in events]
    assert percentages == sorted(percentages)
    assert set(event["verified_files"] for event in events if "file" in event and "processed" not in event) == {1, 2, 3, 4}
    assert all(percentage < 100 for percentage in percentages), "Only successful artifact publication can set 100%"
    assert any(event.get("stage_code") == "final_fit" for event in events)
    assert workers and workers[0] != threading.get_ident()
    assert controller.bundle is None and controller.model_id is None and controller.report_id is None
    assert controller.full_report() == {"reason": "尚无分析报告"}
    model_path = tmp_path / "pilot_models" / (final["model_id"] + ".json")
    assert validate_pilot_model(load_pilot_model(model_path))
    assert not list((tmp_path / "pilot_models").glob("*.pending"))
    assert (tmp_path / "analyses" / response["job_id"] / "report.json").is_file()
    assert archive_bytes(session) == source


@pytest.mark.parametrize("method,label", [("linear", "加权最小二乘"), ("mlp", "小型 MLP")])
def test_training_saves_a_new_model_without_replacing_live_model_report_or_references(load_pilot_archive, monkeypatch, method, label):
    archive = load_pilot_archive
    controller = AnalysisController(archive["root"])
    controller.load("Pfixture")
    live_bundle = controller.bundle
    controller.truth_zero = {"ready": True, "pending": False, "offsets": [1]*6, "epoch": "force-old"}
    controller.test_reference = {"ready": True, "pending": False, "reference_version": 7, "epoch": "optical-old"}
    controller._zero_samples, controller._zero_seen = [(1, [1]*6)], {("force-old", 7)}
    controller.report, controller.report_id = {"kind": "pilot", "summary": {"test_trials": 12}}, "previous-report"
    previous = deepcopy((controller.truth_zero, controller.test_reference, controller._zero_samples,
                         controller._zero_seen, controller.report, controller.report_id))
    source = archive_bytes(archive["session"])
    old_model = archive["file"].read_bytes()
    events = []
    original_progress = controller._training_progress
    def observed(value, phase):
        original_progress(value, phase)
        events.append(deepcopy(controller.training))
    monkeypatch.setattr(controller, "_training_progress", observed)
    done, _ = observe_worker(controller, monkeypatch)
    options = {"method": method, **({"mlp_epochs": 4} if method == "mlp" else {})}
    controller.start_job("train", [archive["session"].name], options)
    finish(controller, done)
    assert controller.bundle is live_bundle and controller.model_id == "Pfixture"
    assert (controller.truth_zero, controller.test_reference, controller._zero_samples,
            controller._zero_seen, controller.report, controller.report_id) == previous
    training = controller.training
    model_id = training["model_id"]
    assert training["status"] == "completed" and training["progress_percent"] == 100
    assert model_id != "Pfixture" and training["method_label"] == label
    model = load_pilot_model(archive["root"] / "pilot_models" / (model_id + ".json"))
    assert model["primary_method"] == method and model["formal_ready"] is False
    assert model["acquisition_provenance"]["sources"][0]["session_id"] == archive["session"].name
    catalog = {item["model_id"]: item for item in controller.models()}
    assert catalog[model_id]["method"] == method and catalog[model_id]["method_label"] == label
    local_minute = datetime.fromisoformat(model["created_utc"]).astimezone().strftime("%Y-%m-%d %H:%M")
    assert training["created_local_minute"] == catalog[model_id]["created_local_minute"] == local_minute
    if method == "mlp":
        assert any(event.get("epoch") for event in events), "MLP progress must come from completed optimizer epochs"
    loaded = controller.load(model_id)
    assert loaded["method"] == method and loaded["method_label"] == label
    assert controller.bundle["primary_method"] == method
    assert not controller.truth_zero["ready"] and not controller.test_reference["ready"]
    if method == "mlp":
        from ninefbg import pilot
        tracker = {"algorithm_version": model["algorithm_version"], "config_version": model["config_version"],
                   "stream_epoch": "software-live-mlp", "reference_version": 1, "reference_pending": False,
                   "display_stale": False, "display_age_ms": 0, "frame_id": 1,
                   "peaks": [{"id": f"FBG{i+1}", "valid": True, "stale": False,
                              "drift_nm": (origin+1.5*slope)/1000}
                             for i, (origin, slope) in enumerate(zip(model["baseline_context"]["origin_pm"], model["templates"][0]["slope_pm_per_N"]))]}
        assert controller.snapshot(tracker, source_kind="hardware")["prediction"]["status"] == "无效"
        controller.request_truth_zero({"tracker": tracker, "frame": {"stream_epoch": tracker["stream_epoch"]}, "force_connected": False})
        tracker.update(reference_version=2, host_rx_monotonic_ns=time.perf_counter_ns())
        original_predict = pilot.predict_pilot
        selected_methods = []
        def observe_predict(bundle, x_pm, quality_context=None):
            selected_methods.append(bundle["primary_method"])
            return original_predict(bundle, x_pm, quality_context)
        monkeypatch.setattr(pilot, "predict_pilot", observe_predict)
        live = controller.snapshot(tracker, source_kind="hardware")
        assert live["test_reference"]["ready"] and selected_methods == ["mlp"]
        assert live["prediction"]["status"] in {"有效估计", "定位不确定", "超范围", "无接触"}
        assert live["prediction"]["truth_force_N"] is live["prediction"]["error_N"] is None
        assert live["prediction"]["formal_ready"] is False
    assert archive["file"].read_bytes() == old_model
    assert archive_bytes(archive["session"]) == source
    assert not (archive["root"] / "models").exists()


def test_b_session_uses_linear_training_by_default_without_activating_the_model(tmp_path, monkeypatch):
    session = archive_fixture(tmp_path, monkeypatch, batch="B")
    controller = AnalysisController(tmp_path)
    done, _ = observe_worker(controller, monkeypatch)
    response = controller.start_job("train", [session.name])
    assert response["training"]["method"] == "linear"
    finish(controller, done)
    assert controller.training["status"] == "completed"
    assert controller.training["progress_percent"] == 100
    assert controller.bundle is None and controller.model_id is None
    model = load_pilot_model(tmp_path / "pilot_models" / (controller.training["model_id"] + ".json"))
    assert model["primary_method"] == "linear"
    assert model["provenance"]["training_session_ids"] == [session.name]


@pytest.mark.parametrize("method", ["linear", "mlp"])
def test_training_http_route_saves_the_selected_method_without_connecting_hardware(tmp_path, monkeypatch, method):
    session = archive_fixture(tmp_path, monkeypatch)
    source = archive_bytes(session)
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    done, _ = observe_worker(service.analysis, monkeypatch)
    with http_service(service) as url:
        response = post(url, "/api/training/start", {"session_ids": [session.name], "method": method})
        assert response["busy"] and response["job_id"].startswith("J")
        assert response["training"]["method"] == method and response["training"]["progress_percent"] == 0
        finish(service.analysis, done)
        training = service.snapshot()["analysis"]["training"]
        assert training["status"] == "completed" and training["progress_percent"] == 100
        assert training["method"] == method and training["model_id"].startswith("P")
    assert not service.running and service.sources == []
    assert service.analysis.bundle is None and service.analysis.model_id is None
    assert not (tmp_path / "sessions").exists() and not (tmp_path / "validation_sessions").exists()
    assert archive_bytes(session) == source


def test_training_http_route_rejects_active_acquisition_before_starting_a_job(tmp_path, monkeypatch):
    session = archive_fixture(tmp_path, monkeypatch)
    source = archive_bytes(session)
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    monkeypatch.setattr(service.experiment, "snapshot", lambda: {"running": True})
    with http_service(service) as url:
        post(url, "/api/training/start", {"session_ids": [session.name], "method": "linear"}, error="采集")
    assert not service.analysis.busy and service.analysis.job_id is None
    assert not (tmp_path / "analyses").exists() and not (tmp_path / "pilot_models").exists()
    assert archive_bytes(session) == source


def test_acquisition_start_waits_for_training_without_archive_or_hardware_side_effects(tmp_path, monkeypatch):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    starts = []
    snapshots = []
    settings = {"finger_id": "训练互斥回归", "batch": "A", "data_id": "legacy-fixture"}
    context = {"running": False, "test_context": "no devices"}

    def snapshot():
        snapshots.append(True)
        return deepcopy(context)

    def forbidden_hardware(*args, **kwargs):
        pytest.fail("Starting an acquisition session must not connect hardware")

    monkeypatch.setattr(service, "snapshot", snapshot)
    monkeypatch.setattr(service.experiment, "start", lambda value, state: starts.append((value, state)))
    monkeypatch.setattr(service, "connect", forbidden_hardware)
    monkeypatch.setattr(service, "connect_force", forbidden_hardware)
    service.experiment_error = "保留已有错误"
    service.analysis.busy = True

    with pytest.raises(ValueError, match="模型处理尚未结束"):
        service.experiment_start(settings)

    assert starts == [] and snapshots == []
    assert service.experiment_error == "保留已有错误"
    assert service.sources == [] and not service.running and not service.recording
    assert list(tmp_path.iterdir()) == []

    service.analysis.busy = False
    assert service.experiment_start(settings) == context
    assert len(starts) == 1 and starts[0][0] is settings
    assert starts[0][1] == {**context, "tracker_config": service.tracker.config}
    assert starts[0][1]["tracker_config"] is not service.tracker.config
    assert len(snapshots) == 2 and service.experiment_error is None
    assert service.sources == [] and not service.running and not service.recording
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("case,match", [("C", "C 批"), ("too_many", "100"), ("duplicate", "不重复"),
                                      ("incomplete", "完整"), ("method", "训练方法"), ("method_object", "训练方法"),
                                      ("options", "参数"), ("synthetic_option", "真实")])
def test_training_rejects_unsafe_or_invalid_inputs_before_a_worker(tmp_path, monkeypatch, case, match):
    session = archive_fixture(tmp_path, monkeypatch, batch="C" if case == "C" else "A")
    if case == "incomplete":
        path = session / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["complete"] = False
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    source = archive_bytes(session)
    controller = AnalysisController(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid training input must be refused before starting work")
    monkeypatch.setattr(controller, "_work", forbidden)
    selected = ([session.name, session.name] if case == "duplicate" else
                [session.name]+[f"other-{number}" for number in range(100)] if case == "too_many" else [session.name])
    options = {"method": "linear"}
    if case == "method": options["method"] = "unknown"
    elif case == "method_object": options["method"] = {}
    elif case == "options": options = []
    elif case == "synthetic_option": options["allow_synthetic_diagnostics"] = True
    with pytest.raises(ValueError, match=match):
        controller.start_job("train", selected, options)
    assert controller.job_id is None and not controller.busy
    assert controller.training["status"] == "idle"
    assert not (tmp_path / "pilot_models").exists() and not (tmp_path / "analyses").exists()
    assert archive_bytes(session) == source


@pytest.mark.parametrize("failure", ["hash", "source", "save", "report"])
def test_failed_training_never_reaches_100_or_publishes_a_partial_model(tmp_path, monkeypatch, failure):
    session = archive_fixture(tmp_path, monkeypatch)
    if failure == "hash":
        with (session / "raw_force.jsonl").open("a", encoding="utf-8") as stream:
            stream.write('{"fixture_tampered":true}\n')
    elif failure == "source":
        def synthetic_extract(path, manifest, **kwargs):
            return [{**json.loads(line), "source_kind": "synthetic"} for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
        monkeypatch.setattr("ninefbg.reprocess.reprocess_session", synthetic_extract)
    elif failure == "save":
        def interrupted_save(model, path):
            Path(path).write_text("partial-model", encoding="utf-8")
            raise OSError("software fixture write failure")
        monkeypatch.setattr("ninefbg.pilot.save_pilot_model", interrupted_save)
    else:
        original_write = Path.write_text
        def interrupted_report(path, *args, **kwargs):
            if path.name == "report.json":
                raise OSError("software fixture report failure")
            return original_write(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", interrupted_report)
    source = archive_bytes(session)
    controller = AnalysisController(tmp_path)
    done, _ = observe_worker(controller, monkeypatch)
    controller.start_job("train", [session.name], {"method": "linear"})
    finish(controller, done, success=False)
    training = controller.snapshot()["training"]
    assert training["status"] == "failed" and not training["busy"]
    assert training["progress_percent"] < 100 and training["model_id"] is None
    assert training["error"] == controller.error and training["error"].startswith("训练失败")
    assert controller.bundle is None and controller.model_id is None and controller.report is None
    assert controller.models() == []
    assert not list((tmp_path / "pilot_models").glob("P*.json"))
    assert not list((tmp_path / "pilot_models").glob("*.pending"))
    assert archive_bytes(session) == source
