"""Temporary-file HTTP regressions; no user assets or hardware are opened."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninefbg.service import AcquisitionService
from test_history_api import request
from test_live_validation_api import live_api, http_service, authorize_and_press
from test_live_pilot import load_pilot_archive, load_full_model
from test_model_history import assets, spectral_assets, tree_bytes


@pytest.fixture
def model_service(spectral_assets):
    service = AcquisitionService(Path(__file__).resolve().parents[1], spectral_assets)
    try:
        yield service
    finally:
        service.analysis.close()


@pytest.mark.parametrize("value", ["Mfixture", "Pfixture", "Sfixture"])
def test_http_inactive_current_model_can_be_recycled_restored_and_explicitly_reloaded(model_service, value):
    service, root = model_service, model_service.userdata
    preserved = {name: tree_bytes(root/name) for name in ("calibration_sessions", "validation_sessions", "analyses")}
    with http_service(service) as url:
        listing = request(url, "/api/analysis/models")
        rows = {row["model_id"]: row for row in listing["models"]}
        assert set(rows) == {"Mfixture", "Pfixture", "Sfixture"}
        assert all(row["loadable"] and row["deletable"] for row in rows.values())
        loaded = request(url, "/api/analysis/load", {"model_id": value, "unloaded_confirmed": True})
        assert loaded["analysis"]["inference_active"] and loaded["analysis"]["model_id"] == value
        request(url, "/api/analysis/models/delete", {"model_ids": [value]}, error="先结束推理")
        assert service.analysis.model_id == value and service.analysis.inference_active
        request(url, "/api/analysis/stop", {})
        deleted = request(url, "/api/analysis/models/delete", {"model_ids": [value]})
        assert deleted["deleted_ids"] == [value]
        assert deleted["analysis"]["model_id"] is deleted["analysis"]["model_kind"] is None
        assert not deleted["analysis"]["inference_active"]
        assert deleted["analysis"]["prediction"]["point_id"] is None
        assert service.analysis.bundle is service.analysis.spectral_runtime.metadata is service.analysis.spectral_runtime.model is None
        assert service.validation.model_checksum is service.validation.model_id is service.validation.validation_session_id is None
        assert not deleted["validation"]["pending_sample"]
        row = next(row for row in deleted["deleted_models"] if row["model_id"] == value)
        assert row["restorable"] and row["deleted_utc"] and row["trained_point_ids"]
        request(url, "/api/analysis/load", {"model_id": value, "unloaded_confirmed": True}, error="不存在")
        restored = request(url, "/api/analysis/models/restore", {"model_ids": [value]})
        assert restored["restored_ids"] == [value] and not restored["deleted_models"]
        assert restored["analysis"]["model_id"] is None and not restored["analysis"]["inference_active"]
        loaded = request(url, "/api/analysis/load", {"model_id": value, "unloaded_confirmed": True})
        assert loaded["analysis"]["inference_active"] and loaded["analysis"]["model_id"] == value
        assert not loaded["analysis"]["test_reference"]["ready"] and not loaded["analysis"]["truth_zero"]["ready"]
    assert {name: tree_bytes(root/name) for name in preserved} == preserved
    assert not service.running and not service.force_source and not service.workers


@pytest.mark.parametrize("busy", ["training", "paused", "acquisition", "recording", "stopping"])
@pytest.mark.parametrize("action", ["delete", "restore"])
def test_http_model_changes_reject_busy_and_save_periods_before_moving_any_asset(load_pilot_archive, busy, action):
    archive = load_pilot_archive
    service = AcquisitionService(Path(__file__).resolve().parents[1], archive["root"])
    if action == "restore":
        service.model_history.delete(["Pfixture"])
    before = tree_bytes(archive["root"])
    if busy == "training":
        service.analysis.busy = True
    elif busy == "paused":
        service.analysis.training = {"method": "spectral_gru"}
        service.analysis.spectral_training = SimpleNamespace(status=lambda: {
            "method": "spectral_gru", "status": "paused", "busy": True, "active": True})
    elif busy == "acquisition":
        service.experiment.running = True
    else:
        setattr(service, busy, True)
    with http_service(service) as url:
        request(url, "/api/analysis/models/"+action, {"model_ids": ["Pfixture"]}, error="进行")
        listing = request(url, "/api/analysis/models")
        rows = listing["deleted_models" if action == "restore" else "models"]
        assert len(rows) == 1 and not rows[0]["deletable"] and not rows[0]["restorable"]
    assert tree_bytes(archive["root"]) == before


def test_http_model_management_rejects_unknown_fields_bad_ids_and_mixed_batch_without_partial_move(load_pilot_archive):
    archive = load_pilot_archive
    service = AcquisitionService(Path(__file__).resolve().parents[1], archive["root"])
    before = tree_bytes(archive["root"])
    with http_service(service) as url:
        for body in ({}, {"model_ids": ["Pfixture"], "path": "outside"}):
            request(url, "/api/analysis/models/delete", body, error="只接受")
            request(url, "/api/analysis/models/restore", body, error="只接受")
        for values, error in (([], "不重复"), (["Pfixture", "Pfixture"], "不重复"),
                              (["Pfixture", "../Moutside"], "标识"), (["Pfixture", "Mmissing"], "不存在"),
                              (["C:/outside/Pfixture"], "标识"), ([True], "不重复")):
            request(url, "/api/analysis/models/delete", {"model_ids": values}, error=error)
        invalid = archive["root"]/"pilot_models"/"Pbroken.json"
        invalid.write_bytes(b'{"invalid":true}')
        row = next(row for row in request(url, "/api/analysis/models")["models"] if row["model_id"] == "Pbroken")
        assert not row["loadable"] and not row["deletable"] and row["history_error"]
        request(url, "/api/analysis/models/delete", {"model_ids": ["Pfixture", "Pbroken"]}, error="模型")
        invalid.unlink()
    assert tree_bytes(archive["root"]) == before


def test_http_pending_judgment_for_another_loaded_context_blocks_model_management(live_api):
    fixture, service = live_api, live_api.service
    with http_service(service) as url:
        request(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        sample = request(url, "/api/validation/freeze", {"actual_point_id": "P01"})["sample"]
        # Switching the selected recorder checksum can hide a previous pending
        # sample from snapshot(); model management must inspect every checksum.
        service.validation.configure("f"*64, "Mother")
        assert service.validation.snapshot()["pending_sample"] is None
        before = tree_bytes(fixture.archive["root"])
        for action in ("delete", "restore"):
            request(url, "/api/analysis/models/"+action, {"model_ids": ["Pfixture"]}, error="先保存本次判断")
        assert service.validation.samples[sample["sample_id"]] == sample
        assert service.analysis.inference_active and service.analysis.model_id == "Pfixture"
        assert tree_bytes(fixture.archive["root"]) == before


def test_http_delete_detaches_current_context_without_rewriting_saved_judgments_or_reports(live_api):
    fixture, service = live_api, live_api.service
    with http_service(service) as url:
        request(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        sample = request(url, "/api/validation/freeze", {"actual_point_id": "P01"})["sample"]
        request(url, "/api/validation/judge", {"sample_id": sample["sample_id"], "position_correct": True, "force_correct": True})
        report = request(url, "/api/validation/finish", {})["report"]
        checksum = service.validation.model_checksum
        history = deepcopy((service.validation.samples, service.validation.judgments, service.validation.reports))
        records = tree_bytes(fixture.archive["root"]/"validation_sessions")
        report_bytes = Path(report["path"]).read_bytes()
        request(url, "/api/analysis/stop", {})
        result = request(url, "/api/analysis/models/delete", {"model_ids": ["Pfixture"]})
        assert result["validation"]["model_id"] is result["validation"]["model_checksum"] is None
        assert result["analysis"]["model_id"] is None
        assert (service.validation.samples, service.validation.judgments, service.validation.reports) == history
        assert service.validation.snapshot(model_checksum=checksum)["stats"]["judged_total"] == 1
        assert tree_bytes(fixture.archive["root"]/"validation_sessions") == records
        assert Path(report["path"]).read_bytes() == report_bytes
        request(url, "/api/analysis/models/restore", {"model_ids": ["Pfixture"]})
        loaded = request(url, "/api/analysis/load", {"model_id": "Pfixture"})
        assert loaded["validation"]["stats"]["judged_total"] == 1 and loaded["analysis"]["inference_active"]


def test_model_disk_operations_do_not_hold_the_realtime_analysis_lock(live_api, monkeypatch):
    service = live_api.service
    for name in ("snapshot", "delete", "restore"):
        original = getattr(service.model_history, name)
        def checked(*args, _original=original, **kwargs):
            assert not service.analysis.lock._is_owned()
            return _original(*args, **kwargs)
        monkeypatch.setattr(service.model_history, name, checked)
    service.models_snapshot()
    service.models_change(["Pfixture"])
    service.models_change(["Pfixture"], restore=True)
