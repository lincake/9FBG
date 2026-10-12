"""Generated archive regressions; no device data or physical accuracy claim."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from ninefbg.analysis import AnalysisController, load_rows
from ninefbg.history import SessionHistory, session_path
from ninefbg.inversion import _digest
from ninefbg.pilot import load_pilot_model
from test_pilot_controller import archive_fixture, archive_bytes, finish, observe_worker


def rewrite_manifest(session, manifest):
    (session / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def pooled_archives(root, monkeypatch):
    """Each generated A/B archive is insufficient alone: two/one trials per point."""
    sessions = [archive_fixture(root, monkeypatch, batch=batch) for batch in ("A", "B")]
    for index, session in enumerate(sessions):
        manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
        original = [json.loads(line) for line in (session / "results.jsonl").read_text(encoding="utf-8").splitlines()]
        rows = [row for row in original if row["trial_id"].endswith(("T0", "T1") if index == 0 else "T2")]
        names = {name: f"T{number:04d}" for number, name in enumerate(sorted({row["trial_id"] for row in rows}), 1)}
        for row in rows:
            row.update(trial_id=names[row["trial_id"]], baseline_id="B"+names[row["trial_id"]][1:])
        file = session / "results.jsonl"
        file.write_text("".join(json.dumps(row, allow_nan=False)+"\n" for row in rows), encoding="utf-8")
        manifest["files"][file.name] = hashlib.sha256(file.read_bytes()).hexdigest()
        manifest["counts"]["results"] = len(rows)
        manifest["settings"].update(data_id=f"generated-only-{index}", conditions={"fixation": "测试夹具", "temperature_C": 23})
        manifest["final"]["trials"] = [{**trial, "trial_id": names[trial["trial_id"]]}
                                       for trial in manifest["final"]["trials"] if trial["trial_id"] in names]
        rewrite_manifest(session, manifest)
    return sessions


def train(controller, monkeypatch, sessions, method="linear"):
    done, _ = observe_worker(controller, monkeypatch)
    controller.start_job("train", [session.name for session in sessions], {"method": method, **({"mlp_epochs": 4} if method == "mlp" else {})})
    finish(controller, done)
    file = controller.userdata / "pilot_models" / (controller.training["model_id"]+".json")
    return file, load_pilot_model(file)


@pytest.mark.parametrize("method", ["linear", "mlp"])
def test_multiple_archives_jointly_satisfy_trials_save_and_load_without_changing_live_state(tmp_path, monkeypatch, method):
    sessions = pooled_archives(tmp_path, monkeypatch)
    original = {session.name: archive_bytes(session) for session in sessions}
    controller = AnalysisController(tmp_path)
    previous_bundle = {"previous_live_model": True}
    controller.bundle, controller.model_id = previous_bundle, "Mprevious"
    controller.truth_zero, controller.test_reference = {"ready": True}, {"ready": True, "reference_version": 7}
    controller.report = {"previous_offline_report": True}
    previous = deepcopy((controller.truth_zero, controller.test_reference, controller.report))

    file, model = train(controller, monkeypatch, list(reversed(sessions)), method)

    assert controller.bundle is previous_bundle and controller.model_id == "Mprevious"
    assert (controller.truth_zero, controller.test_reference, controller.report) == previous
    ids = sorted(session.name for session in sessions)
    assert model["primary_method"] == method and model["formal_ready"] is False
    assert model["scope"] == "measured_points_multi_session_research_only"
    assert model["provenance"]["training_session_ids"] == ids
    assert model["provenance"]["training_session_count"] == 2
    assert set(model["provenance"]["training_trial_sessions"].values()) == set(ids)
    assert len(model["provenance"]["training_trial_ids"]) == 12
    for session in sessions:
        assert session.name+":T0001" in model["provenance"]["training_trial_ids"]
    assert len(model["baseline_context"]["training_baseline_ids"]) == 12
    provenance = model["acquisition_provenance"]
    assert {source["session_id"] for source in provenance["sources"]} == set(ids)
    assert provenance["training_conditions"] == {"fixation": "测试夹具", "temperature_C": 23.0}
    assert set(provenance["physical_definition"]) == {"finger_id", "force_sign", "point_grid", "coordinate_definition"}
    assert all(source["training_conditions"] == provenance["training_conditions"] for source in provenance["sources"])
    report = json.loads((tmp_path / "analyses" / controller.job_id / "report.json").read_text(encoding="utf-8"))
    assert report["session_ids"] == ids and report["session_count"] == 2
    assert report["session_id"] is report["batch"] is None
    assert report["validation"]["independent_session_validation"] is False
    held_out = []
    for fold in report["folds"]:
        assert not set(fold["train_trial_ids"]) & set(fold["test_trial_ids"])
        assert not set(fold["train_baseline_ids"]) & set(fold["test_baseline_ids"])
        held_out.extend(fold["test_trial_ids"])
    assert len(held_out) == len(set(held_out)) == 12
    catalog = controller.models()
    assert len(catalog) == 1 and catalog[0]["source_manifest_verified"] is True
    assert set(catalog[0]["source_session_ids"]) == set(ids)
    assert set(catalog[0]["data_ids"]) == {"generated-only-0", "generated-only-1"}
    loaded = controller.load(file.stem)
    assert loaded["method"] == method and controller.bundle == model
    assert not controller.truth_zero["ready"] and not controller.test_reference["ready"]
    assert {session.name: archive_bytes(session) for session in sessions} == original
    assert not (tmp_path / "models").exists() and not (tmp_path / "validation_sessions").exists()


def test_raw_reprocessing_progress_advances_across_every_selected_session(tmp_path, monkeypatch):
    sessions = pooled_archives(tmp_path, monkeypatch)
    second_ready, release = threading.Event(), threading.Event()
    controller = AnalysisController(tmp_path)
    progress = []
    observed = controller._training_progress

    def collect(value, phase):
        observed(value, phase)
        progress.append(deepcopy(controller.training))

    def extract(path, manifest, progress=None):
        rows = [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
        progress({"processed": len(rows)//2, "total": len(rows)})
        if path == sessions[1]:
            second_ready.set()
            assert release.wait(10)
        progress({"processed": len(rows), "total": len(rows)})
        return rows

    monkeypatch.setattr(controller, "_training_progress", collect)
    monkeypatch.setattr("ninefbg.reprocess.reprocess_session", extract)
    done, _ = observe_worker(controller, monkeypatch)
    try:
        initial = controller.start_job("train", [session.name for session in sessions])
        assert initial["training"]["progress_percent"] == 0
        assert second_ready.wait(10)
        current = controller.snapshot()["training"]
        assert current["session_id"] == sessions[1].name and current["session_index"] == current["session_count"] == 2
        assert current["processed"] == 30 and current["total"] == 60
        assert current["progress_percent"] == 36.25 and current["model_id"] is None
        assert any(event["session_id"] == sessions[0].name and event["progress_percent"] == 13.75 for event in progress)
        assert any(event["session_id"] == sessions[0].name and event["progress_percent"] == 22.5 for event in progress)
        assert all(event["progress_percent"] <= 22.5 for event in progress if event.get("session_id") == sessions[0].name)
    finally:
        release.set()
    finish(controller, done)
    values = [event["progress_percent"] for event in progress]
    assert values == sorted(values) and all(value < 100 for value in values)
    assert controller.training["status"] == "completed" and controller.training["progress_percent"] == 100


@pytest.mark.parametrize("change,match", [
    ({"conditions": {"fixation": "另一夹具", "temperature_C": 23}}, "装夹"),
    ({"conditions": {"fixation": "测试夹具", "temperature_C": 24}}, "温度"),
    ({"conditions": {"fixation": "", "temperature_C": 23}}, "装夹"),
    ({"conditions": {"fixation": "测试夹具", "temperature_C": None}}, "温度"),
    ({"temperature_C": 24}, "冲突"),
    ({"finger_id": "另一触觉件"}, "触觉件"),
    ({"batch": "C"}, "C 批"),
])
def test_incompatible_selected_archives_fail_before_a_training_worker(tmp_path, monkeypatch, change, match):
    sessions = pooled_archives(tmp_path, monkeypatch)
    manifest = json.loads((sessions[1] / "manifest.json").read_text(encoding="utf-8"))
    manifest["settings"].update(change)
    rewrite_manifest(sessions[1], manifest)
    controller = AnalysisController(tmp_path)
    monkeypatch.setattr(controller, "_work", lambda *args: pytest.fail("Incompatible archives must be rejected before work"))
    before = {session.name: archive_bytes(session) for session in sessions}
    with pytest.raises(ValueError, match=match) as caught:
        controller.start_job("train", [session.name for session in sessions])
    if "conditions" in change or "finger_id" in change:
        assert all(session.name in str(caught.value) for session in sessions)
        assert "generated-only-0" in str(caught.value) and "generated-only-1" in str(caught.value)
    assert not controller.busy and controller.job_id is None
    assert not (tmp_path / "analyses").exists() and not (tmp_path / "pilot_models").exists()
    if "batch" not in change:
        with pytest.raises(ValueError, match=match):
            load_rows(tmp_path, [session.name for session in sessions])
    assert {session.name: archive_bytes(session) for session in sessions} == before


def test_blank_historical_conditions_compare_without_migrating_physical_definition(tmp_path, monkeypatch):
    sessions = pooled_archives(tmp_path, monkeypatch)
    for index, session in enumerate(sessions):
        manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
        manifest["settings"].pop("coordinate_definition")
        manifest["settings"].pop("conditions")
        if index:
            manifest["settings"]["conditions"] = {"fixation": "   ", "temperature_C": None}
        rewrite_manifest(session, manifest)
    rows, provenance = load_rows(tmp_path, [session.name for session in sessions])
    assert len(rows) == 180 and provenance["training_conditions"] == {"fixation": None, "temperature_C": None}
    assert set(provenance["physical_definition"]) == {"finger_id", "point_grid", "force_sign"}
    controller = AnalysisController(tmp_path)
    _, model = train(controller, monkeypatch, sessions)
    assert "coordinate_definition" not in model["acquisition_provenance"]["physical_definition"]


@pytest.mark.parametrize("failure", ["raw_hash", "config_version", "source_kind"])
def test_a_bad_later_source_cannot_publish_a_pooled_model(tmp_path, monkeypatch, failure):
    sessions = pooled_archives(tmp_path, monkeypatch)
    if failure == "raw_hash":
        with (sessions[1] / "raw_force.jsonl").open("a", encoding="utf-8") as stream:
            stream.write('{}\n')
    else:
        def extract(path, manifest, **kwargs):
            rows = [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
            return [{**row, failure: "other-config" if failure == "config_version" else "synthetic"} for row in rows] if path == sessions[1] else rows
        monkeypatch.setattr("ninefbg.reprocess.reprocess_session", extract)
    before = {session.name: archive_bytes(session) for session in sessions}
    controller = AnalysisController(tmp_path)
    previous = {"previous_live_model": True}
    controller.bundle, controller.model_id = previous, "Mprevious"
    controller.truth_zero = {"ready": True, "offsets": [1]*6}
    done, _ = observe_worker(controller, monkeypatch)
    controller.start_job("train", [session.name for session in sessions])
    finish(controller, done, success=False)
    assert controller.training["progress_percent"] < 100 and controller.training["model_id"] is None
    assert controller.bundle is previous and controller.model_id == "Mprevious" and controller.truth_zero["ready"] is True
    assert not list((tmp_path / "pilot_models").glob("P*.json")) and controller.models() == []
    assert {session.name: archive_bytes(session) for session in sessions} == before


@pytest.mark.parametrize("case,match", [("last_hash", "来源校验"), ("last_files", "来源校验"),
                                       ("duplicate", "来源重复"), ("missing_id", "训练会话"),
                                       ("conditions", "装夹"), ("scope", "适用范围")])
def test_pooled_model_load_checks_every_source_and_exact_training_membership(tmp_path, monkeypatch, case, match):
    sessions = pooled_archives(tmp_path, monkeypatch)
    controller = AnalysisController(tmp_path)
    file, model = train(controller, monkeypatch, sessions)
    if case == "last_hash":
        model["acquisition_provenance"]["sources"][-1]["manifest_sha256"] = "bad"
    elif case == "last_files":
        model["acquisition_provenance"]["sources"][-1]["files"].pop("raw_force.jsonl")
    elif case == "duplicate":
        model["acquisition_provenance"]["sources"][-1] = deepcopy(model["acquisition_provenance"]["sources"][0])
    elif case == "missing_id":
        model["provenance"]["training_session_ids"].pop()
    elif case == "conditions":
        model["acquisition_provenance"]["sources"][-1]["training_conditions"]["fixation"] = "另一夹具"
    else:
        model["scope"] = "measured_points_same_session_research_only"
    model["model_sha256"] = _digest({key: value for key, value in model.items() if key != "model_sha256"})
    file.write_text(json.dumps(model, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    previous = {"previous_live_model": True}
    controller.bundle, controller.model_id = previous, "Mprevious"
    controller.test_reference = {"ready": True, "reference_version": 7}
    with pytest.raises(ValueError, match=match):
        controller.load(file.stem)
    assert controller.bundle is previous and controller.model_id == "Mprevious" and controller.test_reference["ready"] is True
    assert controller.models() == []


def test_later_source_manifest_is_checked_even_when_source_order_differs_from_training_order(tmp_path, monkeypatch):
    sessions = pooled_archives(tmp_path, monkeypatch)
    controller = AnalysisController(tmp_path)
    file, model = train(controller, monkeypatch, list(reversed(sessions)))
    assert controller.load(file.stem)["model_kind"] == "pilot"
    manifest = json.loads((sessions[0] / "manifest.json").read_text(encoding="utf-8"))
    manifest["settings"]["data_id"] = "modified-after-training"
    rewrite_manifest(sessions[0], manifest)
    with pytest.raises(ValueError, match="清单哈希"):
        controller.load(file.stem)
    assert controller.models() == []


def test_legacy_pilot_endpoint_still_requires_one_session(tmp_path, monkeypatch):
    sessions = pooled_archives(tmp_path, monkeypatch)
    controller = AnalysisController(tmp_path)
    with pytest.raises(ValueError, match="一个"):
        controller.start_job("pilot", [session.name for session in sessions])
    assert controller.job_id is None and not controller.busy


def test_recycled_sources_remain_verifiable_for_existing_models_but_cannot_train(tmp_path, monkeypatch):
    sessions = pooled_archives(tmp_path, monkeypatch)
    controller = AnalysisController(tmp_path)
    file, model = train(controller, monkeypatch, sessions)
    original = {session.name: archive_bytes(session) for session in sessions}
    model_bytes = file.read_bytes()
    history = SessionHistory(tmp_path)
    moved = history.delete([sessions[1].name])
    assert moved["deleted_ids"] == [sessions[1].name] and not sessions[1].exists()
    recycled = session_path(tmp_path, sessions[1].name, include_deleted=True)
    assert recycled.parent.name == "deleted_calibration_sessions"
    assert archive_bytes(recycled) == original[sessions[1].name]
    catalog = controller.models()
    assert len(catalog) == 1 and catalog[0]["source_manifest_verified"] is True
    assert {source["session_id"] for source in catalog[0]["source_records"] if source["manifest_verified"]} == {session.name for session in sessions}
    assert controller.load(file.stem)["model_kind"] == "pilot" and controller.bundle == model
    previous_job = controller.job_id
    with pytest.raises(ValueError, match="已删除"):
        controller.start_job("train", [session.name for session in sessions])
    with pytest.raises(ValueError, match="已删除"):
        load_rows(tmp_path, [sessions[1].name])
    assert not controller.busy and controller.job_id == previous_job
    history.restore([sessions[1].name])
    rows, provenance = load_rows(tmp_path, [session.name for session in sessions])
    assert len(rows) == 180 and len(provenance["sources"]) == 2
    assert {session.name: archive_bytes(session) for session in sessions} == original
    assert file.read_bytes() == model_bytes


@pytest.mark.parametrize("field,match", [("finger_id", "物理定义"), ("conditions", "装夹")])
def test_every_available_source_manifest_must_match_the_models_declared_conditions(tmp_path, monkeypatch, field, match):
    sessions = pooled_archives(tmp_path, monkeypatch)
    controller = AnalysisController(tmp_path)
    file, model = train(controller, monkeypatch, sessions)
    manifest = json.loads((sessions[1] / "manifest.json").read_text(encoding="utf-8"))
    manifest["settings"][field] = "另一触觉件" if field == "finger_id" else {"fixation": "另一夹具", "temperature_C": 23}
    rewrite_manifest(sessions[1], manifest)
    source = next(source for source in model["acquisition_provenance"]["sources"] if source["session_id"] == sessions[1].name)
    source["manifest_sha256"] = hashlib.sha256((sessions[1] / "manifest.json").read_bytes()).hexdigest()
    model["model_sha256"] = _digest({key: value for key, value in model.items() if key != "model_sha256"})
    file.write_text(json.dumps(model, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        controller.load(file.stem)
    assert controller.models() == []


@pytest.mark.parametrize("message,allowed", [("采集数据不存在或已删除", True),
                                            ("采集数据同时存在于活动目录和回收区，无法确定来源", False),
                                            ("采集数据路径存在链接或超出允许目录", False)])
def test_portable_model_missing_source_compatibility_cannot_swallow_unsafe_paths(tmp_path, monkeypatch, message, allowed):
    sessions = pooled_archives(tmp_path, monkeypatch)
    controller = AnalysisController(tmp_path)
    file, _ = train(controller, monkeypatch, sessions)
    original_path = session_path

    def unavailable(userdata, session_id, *, include_deleted=False):
        if session_id == sessions[1].name:
            raise ValueError(message)
        return original_path(userdata, session_id, include_deleted=include_deleted)

    monkeypatch.setattr("ninefbg.history.session_path", unavailable)
    if allowed:
        assert controller.load(file.stem)["model_kind"] == "pilot"
        assert controller.models()[0]["source_manifest_verified"] is False
    else:
        with pytest.raises(ValueError, match="无法确定来源|超出允许目录"):
            controller.load(file.stem)
        assert controller.models() == []


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows case-insensitive filesystem lookup")
@pytest.mark.parametrize("loose_lookup", [False, True])
def test_windows_case_variants_cannot_inflate_one_archives_trials_or_change_live_state(tmp_path, monkeypatch, loose_lookup):
    sessions = pooled_archives(tmp_path, monkeypatch)
    # This archive contains only one real generated trial at each of four points;
    # repeating its path three times must not satisfy three independent trials.
    session = sessions[1]
    aliases = [session.name, session.name.replace("T", "t"), session.name.replace("Z", "z")]
    assert len(set(aliases)) == 3
    assert all((session.parent / alias).samefile(session) for alias in aliases)
    before = {path.name: archive_bytes(path) for path in sessions}
    controller = AnalysisController(tmp_path)
    live_bundle = {"previous_live_model": True}
    controller.bundle, controller.model_id = live_bundle, "Mprevious"
    controller.truth_zero = {"ready": True, "offsets": [1]*6}
    controller.test_reference = {"ready": True, "reference_version": 7}
    controller.report = {"saved_live_counts": {"judged_total": 5, "position_correct": 3}}
    state = deepcopy((controller.truth_zero, controller.test_reference, controller.report, controller.training))
    monkeypatch.setattr(controller, "_work", lambda *args: pytest.fail("Aliases must be refused before a worker"))
    monkeypatch.setattr("ninefbg.reprocess.reprocess_session", lambda *args, **kwargs: pytest.fail("Aliases must be refused before processing features"))
    if loose_lookup:
        # Independently protect analysis if a future/history lookup stops
        # enforcing canonical spelling and returns the requested case as-is.
        monkeypatch.setattr("ninefbg.history.session_path", lambda userdata, value, **kwargs: session.parent / value)
    with pytest.raises(ValueError, match="同一原始会话|大小写"):
        controller.start_job("train", aliases)
    with pytest.raises(ValueError, match="同一原始会话|大小写"):
        load_rows(tmp_path, aliases)
    with pytest.raises(ValueError, match="大小写"):
        controller.start_job("pilot", [aliases[1]])
    assert controller.bundle is live_bundle and controller.model_id == "Mprevious"
    assert (controller.truth_zero, controller.test_reference, controller.report, controller.training) == state
    assert not controller.busy and controller.job_id is None
    assert not (tmp_path / "analyses").exists() and not (tmp_path / "pilot_models").exists()
    assert {path.name: archive_bytes(path) for path in sessions} == before


def test_analysis_rejects_identical_resolved_sources_even_if_a_lookup_returns_different_ids(tmp_path, monkeypatch):
    session = archive_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr("ninefbg.history.session_path", lambda *args, **kwargs: session)
    monkeypatch.setattr("ninefbg.experiment.get_session", lambda *args, **kwargs: pytest.fail("Duplicate paths must fail before reading metadata"))
    controller = AnalysisController(tmp_path)
    before = archive_bytes(session)
    with pytest.raises(ValueError, match="同一原始会话"):
        controller.start_job("train", ["alias-one", "alias-two"])
    with pytest.raises(ValueError, match="同一原始会话"):
        load_rows(tmp_path, ["alias-one", "alias-two"])
    assert not controller.busy and controller.job_id is None
    assert not (tmp_path / "analyses").exists() and not (tmp_path / "pilot_models").exists()
    assert archive_bytes(session) == before


def test_distinct_sources_without_manifest_session_id_remain_trainable_and_loadable(tmp_path, monkeypatch):
    sessions = pooled_archives(tmp_path, monkeypatch)
    for session in sessions:
        manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
        manifest.pop("session_id", None)
        rewrite_manifest(session, manifest)
    before = {session.name: archive_bytes(session) for session in sessions}
    controller = AnalysisController(tmp_path)
    file, model = train(controller, monkeypatch, sessions)
    assert model["provenance"]["training_session_ids"] == sorted(session.name for session in sessions)
    assert controller.load(file.stem)["model_kind"] == "pilot"
    assert {session.name: archive_bytes(session) for session in sessions} == before
