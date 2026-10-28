"""Hardware-shaped synthetic archives verify isolation, never real accuracy.

These fixtures exercise real archive hashing, row selection, worker threads,
artifact saving and HTTP dispatch. Only raw optical reprocessing is replaced;
the generated features and raw placeholders are software test data.
"""
from contextlib import contextmanager
from copy import deepcopy
from functools import partial
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from ninefbg.analysis import AnalysisController
from ninefbg.experiment import coordinate_definition, default_point_grid
from ninefbg.pilot import load_pilot_model, validate_pilot_model
from ninefbg.server import Handler, LocalServer
from ninefbg.storage import SessionWriter
from ninefbg.tracking import ALGORITHM_VERSION, PeakTracker
from test_pilot import sample_rows


def archive_fixture(root, monkeypatch, *, batch="A", rejected_trial=None):
    rows, _, _ = sample_rows()
    settings = {"finger_id": "synthetic-controller-fixture", "batch": batch,
                "force_sign": -1, "force_direction_confirmed": True,
                "coordinates_confirmed": True, "point_grid": default_point_grid(),
                "coordinate_definition": coordinate_definition(),
                "guided_acquisition": True, "repetitions_per_point": 3}
    writer = SessionWriter(root / "calibration_sessions",
                           {"kind": "9fbg_calibration_experiment_v1", "settings": settings})
    for row in rows:
        row.update(session_id=writer.path.name, split=batch, source_kind="hardware",
                   algorithm_version=ALGORITHM_VERSION, config_version=PeakTracker().config_version)
        writer.submit("results", row)
    # Hardware-shaped metadata is intentionally not actual sensor evidence.
    for kind in ("raw_spectra", "raw_force", "events"):
        writer.submit(kind, {"frame_id": 1, "source_kind": "hardware", "fixture_only": True})
    trials = [{"trial_id": trial, "status": "completed", "bad": False, "manual_mark": [],
               "quality": {"accepted": trial != rejected_trial, "criteria_version": "guided_round_v1"}}
              for trial in sorted({row["trial_id"] for row in rows})]
    writer.close({"trials": trials})
    assert writer.error is None

    def synthetic_feature_extractor(path, manifest, **kwargs):
        return [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    monkeypatch.setattr("ninefbg.reprocess.reprocess_session", synthetic_feature_extractor)
    return writer.path


def archive_bytes(path):
    return {file.name: file.read_bytes() for file in path.iterdir() if file.is_file()}


def observe_worker(controller, monkeypatch):
    done = threading.Event()
    original = controller._work
    worker_ids = []
    def run(*args):
        worker_ids.append(threading.get_ident())
        try:
            original(*args)
        finally:
            done.set()
    monkeypatch.setattr(controller, "_work", run)
    return done, worker_ids


def finish(controller, done, *, success=True):
    assert done.wait(10), "Synthetic offline worker did not finish"
    assert controller.busy is False
    if success:
        assert controller.error is None, controller.error
    else:
        assert controller.error


def test_real_archive_checks_and_worker_save_separate_pilot_without_changing_live_model(tmp_path, monkeypatch):
    session = archive_fixture(tmp_path, monkeypatch)
    archived = archive_bytes(session)
    controller = AnalysisController(tmp_path)
    # An opaque existing live model is sufficient: pilot must never read,
    # replace, predict through or reset it. No real model training is invoked.
    live_bundle = {"opaque_existing_25_point_model": True}
    controller.bundle, controller.model_id = live_bundle, "Mexisting"
    controller.truth_zero = {"ready": True, "pending": False, "offsets": [1]*6, "epoch": "force-old"}
    controller.test_reference = {"ready": True, "pending": False, "reference_version": 7, "epoch": "optical-old"}
    controller._zero_samples = [(123, [1]*6)]
    controller._zero_seen = {("force-old", 1)}
    state = deepcopy((controller.truth_zero, controller.test_reference, controller._zero_samples, controller._zero_seen))
    models = tmp_path / "models"
    models.mkdir()
    previous = models / "Mexisting.json"
    previous.write_text(json.dumps({"algorithm_version": ALGORITHM_VERSION,
                                    "source_kind": "hardware", "fixture_only": True}), encoding="utf-8")
    old_model_files = archive_bytes(models)
    done, worker_ids = observe_worker(controller, monkeypatch)
    response = controller.start_job("pilot", [session.name])
    assert response["busy"] is True and response["job_id"].startswith("J")
    finish(controller, done)
    assert worker_ids and worker_ids[0] != threading.get_ident()
    assert controller.bundle is live_bundle and controller.model_id == "Mexisting"
    assert (controller.truth_zero, controller.test_reference, controller._zero_samples, controller._zero_seen) == state
    assert archive_bytes(models) == old_model_files
    assert archive_bytes(session) == archived, "Neither source manifest nor four evidence files may be rewritten"

    report = controller.full_report()
    assert report["schema"] == "9fbg_pilot_cv_v1" and report["kind"] == "pilot"
    assert report["formal_ready"] is False
    assert report["summary"]["test_trials"] == 12
    assert report["confusion_matrix"]["labels"] == ["P01", "P05", "P21", "P25"]
    assert len(report["confusion_matrix"]["counts"]) == 4
    test_ids = [trial for fold in report["folds"] for trial in fold["test_trial_ids"]]
    assert len(test_ids) == len(set(test_ids)) == 12
    assert all(trial.startswith(session.name + ":") for trial in test_ids)
    assert report["provenance"]["sources"][0]["files"] == json.loads(archived["manifest.json"])["files"]
    assert report["provenance"]["physical_definition"]["coordinate_definition"] == coordinate_definition()
    saved_model = Path(report["pilot_model_path"])
    assert saved_model.parent == tmp_path / "pilot_models"
    assert saved_model.name == report["pilot_model_id"] + ".json"
    assert report["pilot_model_id"].startswith("P")
    model = load_pilot_model(saved_model)
    assert validate_pilot_model(model)
    assert model["model_sha256"] == report["model_sha256"]
    assert model["acquisition_provenance"] == report["provenance"]
    assert model["source_kind"] == "hardware", "Hardware-shaped synthetic fixture only, never accuracy evidence"
    assert model["formal_ready"] is False
    aligned = Path(report["aligned_table"])
    assert aligned.parent.name == response["job_id"]
    assert len(aligned.read_text(encoding="utf-8").splitlines()) == 180
    assert json.loads((aligned.parent / "report.json").read_text(encoding="utf-8")) == report
    # The historical opaque fixture is deliberately not a valid model. The
    # validated catalog excludes it while the preservation checks above still
    # require its bytes and the active in-memory instance to remain untouched.
    assert {item["model_id"] for item in controller.models()} == {report["pilot_model_id"]}
    # Building still preserves M; only a later explicit load activates P and
    # clears the old reference authorization for new independent live testing.
    loaded = controller.load(report["pilot_model_id"])
    assert loaded["model_kind"] == "pilot"
    assert controller.bundle["schema"] == "9fbg_trial_pilot_model_v1"
    assert not controller.truth_zero["ready"] and not controller.test_reference["ready"]
    assert not controller._zero_samples and not controller._zero_seen


def test_pilot_with_no_live_model_does_not_create_models_directory_or_authorize_reference(tmp_path, monkeypatch):
    session = archive_fixture(tmp_path, monkeypatch, batch="B")
    controller = AnalysisController(tmp_path)
    zero, reference = deepcopy(controller.truth_zero), deepcopy(controller.test_reference)
    done, _ = observe_worker(controller, monkeypatch)
    controller.start_job("pilot", [session.name])
    finish(controller, done)
    assert controller.bundle is None and controller.model_id is None
    assert not (tmp_path / "models").exists()
    assert [row["model_kind"] for row in controller.models()] == ["pilot"]
    assert controller.truth_zero == zero and controller.test_reference == reference
    assert controller.snapshot()["prediction"]["status"] == "尚无模型"
    report = controller.full_report()
    assert report["batch"] == "B"
    assert report["validation"]["independent_session_validation"] is False
    assert report["validation"]["reported_scores_source"] == "held_out_fold_models_only"
    assert len(list((tmp_path / "pilot_models").glob("P*.json"))) == 1


@pytest.mark.parametrize("case,match", [("C", "C 批"), ("multiple", "一个"), ("duplicate", "一个")])
def test_c_or_multiple_sessions_rejected_before_worker_or_loader(tmp_path, monkeypatch, case, match):
    session = archive_fixture(tmp_path, monkeypatch, batch="C" if case == "C" else "A")
    controller = AnalysisController(tmp_path)
    source = archive_bytes(session)
    def forbidden(*args, **kwargs):
        pytest.fail("Early pilot preflight must not start a worker or reprocess raw data")
    monkeypatch.setattr(controller, "_work", forbidden)
    monkeypatch.setattr("ninefbg.analysis.load_rows", forbidden)
    selected = [session.name] if case == "C" else [session.name, session.name if case == "duplicate" else "second"]
    with pytest.raises(ValueError, match=match):
        controller.start_job("pilot", selected)
    assert controller.busy is False and controller.job_id is None
    assert not (tmp_path / "analyses").exists()
    assert not (tmp_path / "pilot_models").exists()
    assert not (tmp_path / "models").exists()
    assert archive_bytes(session) == source


def test_source_hash_failure_creates_no_pilot_artifact(tmp_path, monkeypatch):
    session = archive_fixture(tmp_path, monkeypatch)
    with (session / "raw_force.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"fixture_tampering":true}\n')
    before = archive_bytes(session)
    controller = AnalysisController(tmp_path)
    done, _ = observe_worker(controller, monkeypatch)
    controller.start_job("pilot", [session.name])
    finish(controller, done, success=False)
    assert "哈希" in controller.error
    assert controller.bundle is None and controller.report is None
    assert not (tmp_path / "analyses").exists() and not (tmp_path / "pilot_models").exists()
    assert archive_bytes(session) == before


def test_failed_guided_round_cannot_supply_the_third_trial(tmp_path, monkeypatch):
    session = archive_fixture(tmp_path, monkeypatch, rejected_trial="P01-T0")
    before = archive_bytes(session)
    controller = AnalysisController(tmp_path)
    done, _ = observe_worker(controller, monkeypatch)
    controller.start_job("pilot", [session.name])
    finish(controller, done, success=False)
    assert "P01" in controller.error and "三个" in controller.error
    assert controller.bundle is None and controller.report is None
    assert not (tmp_path / "pilot_models").exists() and not (tmp_path / "models").exists()
    assert archive_bytes(session) == before


@contextmanager
def pilot_http(controller):
    service = SimpleNamespace(root=Path(__file__).resolve().parents[1], analysis=controller, command_lock=threading.RLock(),
                              analysis_load=controller.load,
                              models_snapshot=lambda: {"models": controller.models()})
    server = LocalServer(("127.0.0.1", 0), partial(Handler, service=service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def test_pilot_http_route_reports_saved_result_without_exposing_it_as_live_model(tmp_path, monkeypatch):
    session = archive_fixture(tmp_path, monkeypatch)
    source = archive_bytes(session)
    controller = AnalysisController(tmp_path)
    done, _ = observe_worker(controller, monkeypatch)
    with pilot_http(controller) as url:
        request = urllib.request.Request(url + "/api/analysis/pilot",
                                         data=json.dumps({"session_ids": [session.name]}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            launched = json.load(response)
        assert launched["busy"] is True
        finish(controller, done)
        with urllib.request.urlopen(url + "/api/analysis/report", timeout=10) as response:
            report = json.load(response)
        assert report["schema"] == "9fbg_pilot_cv_v1" and report["summary"]["test_trials"] == 12
        assert report["pilot_model_id"].startswith("P")
        assert report["formal_ready"] is False
        with urllib.request.urlopen(url + "/api/analysis/models", timeout=10) as response:
            catalog = json.load(response)["models"]
        assert [row["model_id"] for row in catalog] == [report["pilot_model_id"]]
        assert catalog[0]["model_kind"] == "pilot"
        request = urllib.request.Request(url + "/api/analysis/load",
                                         data=json.dumps({"model_id": report["pilot_model_id"]}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.load(response)["model_kind"] == "pilot"
    assert controller.bundle["schema"] == "9fbg_trial_pilot_model_v1"
    assert controller.model_id == report["pilot_model_id"]
    assert not controller.test_reference["ready"] and not controller.truth_zero["ready"]
    assert archive_bytes(session) == source
