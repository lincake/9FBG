"""MODELLOAD software regressions; no physical trial or device access."""
from copy import deepcopy
import json

import pytest

from ninefbg.analysis import AnalysisController, load_rows
from ninefbg.inversion import _digest, fit_models
from ninefbg.pilot import fit_pilot, save_pilot_model
from ninefbg.tracking import ALGORITHM_VERSION, PeakTracker
from test_inversion import sample_data
from test_pilot_controller import archive_fixture, archive_bytes


@pytest.fixture
def load_pilot_archive(tmp_path, monkeypatch):
    # Only generated software data is fitted. archive_fixture replaces raw
    # optical reprocessing and labels its source files fixture_only.
    session = archive_fixture(tmp_path, monkeypatch)
    path = session / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["settings"]["data_id"] = "四点 · 操作者编号 007"
    manifest["started_utc"] = "2026-10-10T06:05:01+00:00"
    manifest["ended_utc"] = "2026-10-10T06:20:31+00:00"
    path.write_text(json.dumps(manifest, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    rows, provenance = load_rows(tmp_path, [session.name])
    model = fit_pilot(rows)["model"]
    model["acquisition_provenance"] = provenance
    model["created_utc"] = "2026-10-10T08:01:59+00:00"
    model["model_sha256"] = _digest({key: value for key, value in model.items() if key != "model_sha256"})
    directory = tmp_path / "pilot_models"
    directory.mkdir()
    file = directory / "Pfixture.json"
    save_pilot_model(model, file)
    return {"root": tmp_path, "session": session, "file": file, "model": model, "manifest": manifest}


@pytest.fixture(scope="module")
def load_full_model():
    rows, _, _ = sample_data()
    for row in rows:
        row.update(source_kind="hardware", algorithm_version=ALGORITHM_VERSION,
                   config_version=PeakTracker().config_version)
    model = fit_models(rows, {"include_mlp": False})
    model["created_utc"] = "2026-10-10T09:00:01+00:00"
    model["bundle_sha256"] = _digest({key: value for key, value in model.items() if key != "bundle_sha256"})
    return model


def write_resealed_pilot(archive, mutation):
    model = deepcopy(archive["model"])
    mutation(model)
    model["model_sha256"] = _digest({key: value for key, value in model.items() if key != "model_sha256"})
    archive["file"].write_text(json.dumps(model, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return model


def test_model_load_pilot_preserves_archives_and_clears_every_zero_reference_authorization(load_pilot_archive, monkeypatch):
    archive = load_pilot_archive
    controller = AnalysisController(archive["root"])
    before = archive_bytes(archive["session"])
    model_bytes = archive["file"].read_bytes()
    controller.truth_zero = {"ready": True, "pending": True, "offsets": [1]*6, "epoch": "old-force"}
    controller.test_reference = {"ready": True, "pending": True, "reference_version": 4, "epoch": "old-optical"}
    controller._zero_seen = {("old-force", 4)}
    controller._zero_samples = [(123, [1]*6)]
    controller.report = {"schema": "9fbg_pilot_cv_v1", "scope": "offline_repeatability_only", "summary": {"test_trials": 12}}
    old_report = deepcopy(controller.report)
    def forbidden(*args, **kwargs):
        pytest.fail("Loading an existing model must never reprocess, refit or create a recording")
    monkeypatch.setattr("ninefbg.pilot.fit_pilot", forbidden)
    monkeypatch.setattr("ninefbg.reprocess.reprocess_session", forbidden)
    monkeypatch.setattr("ninefbg.storage.SessionWriter", forbidden)
    result = controller.load("Pfixture")
    assert result["model_kind"] == "pilot" and result["research_only"] is True
    assert result["formal_ready"] is False
    assert result["point_ids"] == ["P01", "P05", "P21", "P25"]
    assert controller.model_id == "Pfixture"
    assert controller.bundle == archive["model"]
    assert controller.bundle["schema"] == "9fbg_trial_pilot_model_v1"
    assert not controller.truth_zero["ready"] and not controller.truth_zero["pending"]
    assert controller.truth_zero.get("offsets") is None
    assert not controller.test_reference["ready"] and not controller.test_reference["pending"]
    assert not controller._zero_seen and not controller._zero_samples
    assert controller.report == old_report, "Offline CV is retained as its own report, never relabeled live accuracy"
    assert archive_bytes(archive["session"]) == before
    assert archive["file"].read_bytes() == model_bytes
    assert not (archive["root"] / "models").exists()


def test_model_load_lists_m_and_p_by_creation_time_and_explicit_source_metadata(load_pilot_archive, load_full_model):
    archive = load_pilot_archive
    full_dir = archive["root"] / "models"
    full_dir.mkdir()
    (full_dir / "Maaa.json").write_text(json.dumps(load_full_model, ensure_ascii=False), encoding="utf-8")
    source_before = archive_bytes(archive["session"])
    controller = AnalysisController(archive["root"])
    listed = controller.models()
    assert [row["model_id"] for row in listed] == ["Maaa", "Pfixture"], "Use created_utc rather than filename order"
    assert listed[0]["model_kind"] == "full"
    row = listed[1]
    assert row["model_kind"] == "pilot" and row["research_only"] is True and row["formal_ready"] is False
    assert row["point_ids"] == ["P01", "P05", "P21", "P25"]
    assert row["created_minute"] == "2026-10-10T08:01+00:00"
    assert row["data_id"] == "四点 · 操作者编号 007"
    assert row["data_ids"] == ["四点 · 操作者编号 007"]
    assert row["source_session_ids"] == [archive["session"].name]
    assert row["data_started_minute"] == "2026-10-10T06:05+00:00"
    assert row["data_ended_minute"] == "2026-10-10T06:20+00:00"
    assert row["source_manifest_verified"] is True
    assert row["source_records"][0]["manifest_verified"] is True
    assert row["scope"] == "measured_points_same_session_research_only"
    assert archive_bytes(archive["session"]) == source_before
    controller.load("Maaa")
    assert controller.bundle == load_full_model and controller.model_id == "Maaa"
    controller.load("Pfixture")
    assert controller.bundle == archive["model"] and controller.model_id == "Pfixture"


@pytest.mark.parametrize("case,match", [
    ("checksum", "校验"), ("synthetic", "真实硬件"), ("algorithm", "提峰算法"),
    ("physical", "物理定义"), ("finger", "物理定义"), ("force_sign", "物理定义"),
    ("grid", "坐标定义"), ("grid_finite", "有限数值"), ("coordinates", "坐标约定"),
    ("source", "原始数据来源"), ("source_hash", "来源校验"), ("source_files", "来源校验"),
    ("training_session", "训练会话"), ("scope", "适用范围"), ("formal", "正式训练门"),
])
def test_model_load_rejects_invalid_pilot_without_changing_loaded_state(load_pilot_archive, case, match):
    archive = load_pilot_archive
    def mutation(model):
        if case == "synthetic":
            model["source_kind"] = "synthetic"
        elif case == "algorithm":
            model["algorithm_version"] = "different-algorithm"
        elif case == "physical":
            model["acquisition_provenance"].pop("physical_definition")
        elif case == "finger":
            model["acquisition_provenance"]["physical_definition"]["finger_id"] = ""
        elif case == "force_sign":
            model["acquisition_provenance"]["physical_definition"]["force_sign"] = True
        elif case == "grid":
            model["acquisition_provenance"]["physical_definition"]["point_grid"].pop()
        elif case == "grid_finite":
            model["acquisition_provenance"]["physical_definition"]["point_grid"][0]["x_norm"] = "invalid"
        elif case == "coordinates":
            model["acquisition_provenance"]["physical_definition"]["coordinate_definition"] = None
        elif case == "source":
            model["acquisition_provenance"]["sources"] = []
        elif case == "source_hash":
            model["acquisition_provenance"]["sources"][0]["manifest_sha256"] = "missing"
        elif case == "source_files":
            model["acquisition_provenance"]["sources"][0]["files"].pop("raw_force.jsonl")
        elif case == "training_session":
            model["provenance"]["training_session_ids"] = ["another-session"]
        elif case == "scope":
            model["scope"] = "formal_25_point"
        elif case == "formal":
            model["formal_ready"] = True
    changed = write_resealed_pilot(archive, mutation)
    if case == "checksum":
        changed["templates"][0]["slope_pm_per_N"][0] += 1
        archive["file"].write_text(json.dumps(changed), encoding="utf-8")
    controller = AnalysisController(archive["root"])
    previous = {"opaque_existing_model": True}
    controller.bundle, controller.model_id = previous, "Mprevious"
    controller.truth_zero = {"ready": True, "offsets": [0]*6}
    controller.test_reference = {"ready": True, "reference_version": 8}
    before = deepcopy((controller.truth_zero, controller.test_reference))
    with pytest.raises(ValueError, match=match):
        controller.load("Pfixture")
    assert controller.bundle is previous and controller.model_id == "Mprevious"
    assert (controller.truth_zero, controller.test_reference) == before
    assert controller.models() == [], "Invalid live models must not appear selectable"


def test_model_load_checks_existing_manifest_hash_without_rehashing_or_modifying_raw(load_pilot_archive):
    archive = load_pilot_archive
    manifest = deepcopy(archive["manifest"])
    manifest["settings"]["data_id"] = "modified-after-training"
    (archive["session"] / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = archive_bytes(archive["session"])
    controller = AnalysisController(archive["root"])
    with pytest.raises(ValueError, match="清单哈希"):
        controller.load("Pfixture")
    assert controller.models() == []
    assert archive_bytes(archive["session"]) == before


def test_model_load_preserves_legacy_absent_coordinate_definition(load_pilot_archive):
    archive = load_pilot_archive
    manifest = deepcopy(archive["manifest"])
    manifest["settings"].pop("coordinate_definition")
    manifest_path = archive["session"] / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    import hashlib
    def mutation(model):
        model["acquisition_provenance"]["physical_definition"].pop("coordinate_definition")
        model["acquisition_provenance"]["sources"][0]["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    write_resealed_pilot(archive, mutation)
    before = archive_bytes(archive["session"])
    controller = AnalysisController(archive["root"])
    controller.load("Pfixture")
    assert "coordinate_definition" not in controller.bundle["acquisition_provenance"]["physical_definition"]
    assert archive_bytes(archive["session"]) == before


def test_model_load_current_config_check_remains_a_live_tracker_gate(load_pilot_archive):
    archive = load_pilot_archive
    write_resealed_pilot(archive, lambda model: model.update(config_version="stored-nondefault-config"))
    controller = AnalysisController(archive["root"])
    controller.load("Pfixture")
    assert controller.bundle["config_version"] == "stored-nondefault-config", "Load must not guess the connected tracker config from defaults"
    assert not controller.test_reference["ready"]


def test_model_load_rechecks_busy_after_file_validation(load_pilot_archive, monkeypatch):
    archive = load_pilot_archive
    controller = AnalysisController(archive["root"])
    controller.bundle, controller.model_id = {"previous": True}, "Mprevious"
    before = controller.bundle
    import ninefbg.analysis as analysis_module
    original = analysis_module._validate_live_pilot
    def concurrent_job(*args):
        original(*args)
        controller.busy = True
    monkeypatch.setattr(analysis_module, "_validate_live_pilot", concurrent_job)
    with pytest.raises(ValueError, match="正在运行"):
        controller.load("Pfixture")
    assert controller.bundle is before and controller.model_id == "Mprevious"
