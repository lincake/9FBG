"""Catalog invalidation and disconnected clients, using temporary assets only."""
from io import BytesIO
import json
import os
from pathlib import Path
import shutil

import pytest

from ninefbg.server import Handler
from ninefbg.service import AcquisitionService
from test_model_history import assets, spectral_assets
from test_live_pilot import load_pilot_archive, load_full_model
from test_history import create_directory_link


@pytest.fixture
def catalog(assets, monkeypatch):
    service = AcquisitionService(Path(__file__).resolve().parents[1], assets)
    original = service.analysis.models
    calls = []

    def scanned():
        calls.append(True)
        return original()

    monkeypatch.setattr(service.analysis, "models", scanned)
    try:
        yield service, calls
    finally:
        service.analysis.close()


def by_id(value):
    return {row["model_id"]: row for row in value["models"]}


def test_unchanged_catalog_reuses_descriptions_but_not_mutable_rows_or_live_guards(catalog):
    service, calls = catalog
    first = by_id(service.models_snapshot())
    assert set(first) == {"Mfixture", "Pfixture"}
    first["Pfixture"]["point_ids"].clear()
    first["Pfixture"]["loadable"] = False
    assert by_id(service.models_snapshot())["Pfixture"]["point_ids"]
    service.analysis.busy = True
    assert all(not row["deletable"] and "正在进行" in row["history_error"]
               for row in service.models_snapshot()["models"])
    service.analysis.busy = False
    service.analysis.inference_active, service.analysis.model_id = True, "Pfixture"
    rows = by_id(service.models_snapshot())
    assert not rows["Pfixture"]["deletable"] and "先结束推理" in rows["Pfixture"]["history_error"]
    assert rows["Mfixture"]["deletable"]
    assert len(calls) == 1


def test_catalog_invalidates_model_edits_additions_and_removal(catalog):
    service, calls = catalog
    service.models_snapshot()
    source = service.userdata / "models" / "Mfixture.json"
    with source.open("ab") as stream:
        stream.write(b"\n")
    assert by_id(service.models_snapshot())["Mfixture"]["loadable"]
    shutil.copyfile(source, source.with_name("Mnew.json"))
    assert "Mnew" in by_id(service.models_snapshot())
    source.write_bytes(b"{broken")
    assert not by_id(service.models_snapshot())["Mfixture"]["loadable"]
    source.unlink()
    assert "Mfixture" not in by_id(service.models_snapshot())
    assert len(calls) == 5


def test_catalog_invalidates_source_manifest_edits(catalog, load_pilot_archive):
    service, calls = catalog
    assert by_id(service.models_snapshot())["Pfixture"]["loadable"]
    manifest = load_pilot_archive["session"] / "manifest.json"
    with manifest.open("ab") as stream:
        stream.write(b"\n")
    row = by_id(service.models_snapshot())["Pfixture"]
    assert not row["loadable"] and "清单哈希" in row["history_error"]
    assert len(calls) == 2


def test_catalog_invalidates_source_movement_and_same_name_conflicts(catalog, load_pilot_archive):
    service, calls = catalog
    service.models_snapshot()
    source = load_pilot_archive["session"]
    target = service.userdata / "deleted_calibration_sessions" / source.name
    target.parent.mkdir()
    source.rename(target)
    assert by_id(service.models_snapshot())["Pfixture"]["loadable"]
    source.mkdir()
    row = by_id(service.models_snapshot())["Pfixture"]
    assert not row["loadable"] and "同时存在" in row["history_error"]
    assert len(calls) == 3


def test_live_source_raw_append_keeps_catalog_cache_but_file_replacement_invalidates(catalog, load_pilot_archive):
    service, calls = catalog
    service.models_snapshot()
    source = load_pilot_archive["session"] / "raw_spectra.jsonl"
    with source.open("ab") as stream:
        stream.write(b"\n")
    assert by_id(service.models_snapshot())["Pfixture"]["loadable"]
    assert len(calls) == 1
    replacement = source.with_name("replacement.jsonl")
    shutil.copyfile(source, replacement)
    os.replace(replacement, source)
    service.models_snapshot()
    assert len(calls) == 2


def test_source_junction_replacement_is_not_hidden_by_warm_catalog(catalog, load_pilot_archive):
    service, calls = catalog
    service.models_snapshot()
    source = load_pilot_archive["session"]
    preserved = service.userdata / "preserved-source"
    source.rename(preserved)
    create_directory_link(source, preserved)
    try:
        row = by_id(service.models_snapshot())["Pfixture"]
        assert not row["loadable"] and "链接" in row["history_error"]
        assert len(calls) == 2
        assert (preserved / "manifest.json").is_file()
    finally:
        source.rmdir() if os.name == "nt" else source.unlink()


def test_model_hard_link_is_not_hidden_by_warm_catalog(catalog):
    service, calls = catalog
    service.models_snapshot()
    model = service.userdata / "models" / "Mfixture.json"
    os.link(model, service.userdata / "linked-model.json")
    row = by_id(service.models_snapshot())["Mfixture"]
    assert not row["loadable"] and "链接" in row["history_error"]
    assert len(calls) == 2


def test_catalog_does_not_cache_a_scan_changed_by_an_external_writer(catalog, monkeypatch):
    service, calls = catalog
    original = service.analysis.models

    def changing_scan():
        value = original()
        if len(calls) == 1:
            model = service.userdata / "models" / "Mfixture.json"
            shutil.copyfile(model, model.with_name("Mnew.json"))
        return value

    monkeypatch.setattr(service.analysis, "models", changing_scan)
    service.models_snapshot()
    assert service.model_history._listing_cache is None
    assert "Mnew" in by_id(service.models_snapshot())
    service.models_snapshot()
    assert len(calls) == 2


@pytest.mark.parametrize("action", ["delete", "load"])
def test_catalog_cache_never_substitutes_for_weight_validation_during_operations(spectral_assets, action):
    service = AcquisitionService(Path(__file__).resolve().parents[1], spectral_assets)
    try:
        assert by_id(service.models_snapshot())["Sfixture"]["loadable"]
        weights = spectral_assets / "spectral_models" / "Sfixture" / "weights.pt"
        with weights.open("ab") as stream:
            stream.write(b"tampered after listing")
        # No intervening listing: the command must independently validate bytes.
        with pytest.raises(ValueError, match="权重哈希"):
            if action == "delete":
                service.models_change(["Sfixture"])
            else:
                service.analysis_load("Sfixture", unloaded_confirmed=True)
        assert weights.exists() and not (spectral_assets / "deleted_models").exists()
        assert not by_id(service.models_snapshot())["Sfixture"]["loadable"]
    finally:
        service.analysis.close()


def test_catalog_invalidates_delete_restore_and_recycled_payload_tampering(catalog):
    service, calls = catalog
    service.models_snapshot()
    removed = service.models_change(["Pfixture"])
    assert "Pfixture" not in by_id(removed) and removed["deleted_models"][0]["restorable"]
    service.analysis.busy = True
    assert not service.models_snapshot()["deleted_models"][0]["restorable"]
    service.analysis.busy = False
    service.models_change(["Pfixture"], restore=True)
    assert by_id(service.models_snapshot())["Pfixture"]["loadable"]
    service.models_change(["Pfixture"])
    payload = service.userdata / "deleted_models" / "Pfixture" / "payload.json"
    with payload.open("ab") as stream:
        stream.write(b"\n")
    row = service.models_snapshot()["deleted_models"][0]
    assert not row["restorable"] and "资产哈希" in row["history_error"]
    assert len(calls) == 5


@pytest.mark.parametrize("exception", [ConnectionAbortedError(10053, "client aborted"),
                                        ConnectionResetError(10054, "client reset"),
                                        BrokenPipeError(32, "closed pipe"), TimeoutError("closed")])
def test_json_client_disconnect_does_not_attempt_a_second_response(exception):
    handler = object.__new__(Handler)
    responses = []
    handler.send_response = lambda status: responses.append(status)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None

    class ClosedWriter:
        def write(self, value):
            raise exception

    handler.wfile = ClosedWriter()
    handler._json({"models": []})
    assert responses == [200] and handler.close_connection is True


def test_json_successful_response_retains_unicode_and_content():
    handler = object.__new__(Handler)
    handler.send_response = lambda status: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()
    handler._json({"message": "模型列表"})
    assert json.loads(handler.wfile.getvalue()) == {"message": "模型列表"}
