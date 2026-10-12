"""Sealed manual-validation reports use only operator-saved live comparisons."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

import pytest

from ninefbg.validation import ValidationController
from test_validation import SHA_A, SHA_B, clock, configured, evidence


def read_events(store, session_id=None):
    path = store.root/(session_id or store.validation_session_id)/"records.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def save(store, clock, frame_id=1, *, error=.1, position=True, force=True, target="P01", checksum=SHA_A):
    source = evidence(clock, frame_id, error=error, checksum=checksum)
    source["target_point_id"] = target
    sample = store.capture(source)
    store.judge(sample["sample_id"], position, force)
    return sample


def test_finish_requires_at_least_one_saved_judgment_and_no_pending_sample(tmp_path, clock):
    store = configured(tmp_path)
    with pytest.raises(ValueError, match="尚无已保存"):
        store.finish()
    assert not store.root.exists()
    sample = store.capture(evidence(clock))
    with pytest.raises(ValueError, match="先保存本次判断"):
        store.finish()
    assert len(read_events(store)) == 1
    assert not (store.root/store.validation_session_id/"report.json").exists()
    store.discard(sample["sample_id"])
    with pytest.raises(ValueError, match="尚无已保存"):
        store.finish()
    save(store, clock, 2)
    pending = store.capture(evidence(clock, 3))
    with pytest.raises(ValueError, match="先保存本次判断"):
        store.finish()
    assert not any(event["event"] == "finish" for event in read_events(store))
    store.discard(pending["sample_id"])
    report = store.finish()["report"]
    assert report["stats"]["judged_total"] == 1
    assert len(report["records"]) == 1


def test_report_counts_manual_negatives_and_rejections_but_only_valid_force_errors(tmp_path, clock):
    store = configured(tmp_path)
    first = save(store, clock, 1, error=.1, position=True, force=False)
    second = save(store, clock, 2, error=-.2, position=False, force=True, target="P05")
    rejected = evidence(clock, 3)
    rejected["target_point_id"] = "P25"
    rejected["prediction"].update(status="无接触", point_id=None, force_N=0., error_N=None, comparison_ready=True)
    third = store.capture(rejected)
    store.judge(third["sample_id"], False, False)
    before = read_events(store)
    result = store.finish()
    report, stats = result["report"], result["report"]["stats"]
    assert report["schema"] == "9fbg_manual_validation_report_v1"
    assert report["model_sha256"] == SHA_A and report["model_id"] == "M-a"
    assert report["scope"] == "operator_saved_live_press_judgments_only"
    assert report["formal_ready"] is False and report["force_tolerance_N"] == .3
    assert stats["judged_total"] == 3 and stats["inference_rejected"] == 1
    assert stats["position_correct"] == stats["force_correct"] == 1 and stats["joint_correct"] == 0
    assert stats["position_rate"] == stats["force_rate"] == pytest.approx(1/3)
    assert stats["joint_rate"] == 0
    assert stats["force_error_count"] == 2
    assert stats["mae_N"] == pytest.approx(.15)
    assert stats["rmse_N"] == pytest.approx(math.sqrt((.1**2+.2**2)/2))
    assert "invalid_count" not in stats
    assert {row["sample_id"] for row in report["records"]} == {first["sample_id"], second["sample_id"], third["sample_id"]}
    assert [row["actual_point_id"] for row in report["records"]] == ["P01", "P05", "P25"]
    assert all(row["source"] == "operator" for row in report["records"])
    assert report["records"][2]["inference_valid"] is False
    assert report["records"][2]["point_id"] is report["records"][2]["force_N"] is report["records"][2]["error_N"] is None
    assert report["source_head_sha256"] == before[-1]["record_sha256"]
    stored = json.loads(Path(report["path"]).read_bytes())
    assert stored == {key: value for key, value in report.items() if key != "path"}
    assert stored["report_sha256"] == hashlib.sha256(canonical({key: value for key, value in stored.items() if key != "report_sha256"})).hexdigest()
    events = read_events(store)
    assert events[:-1] == before and events[-1]["event"] == "finish"
    assert events[-1]["previous_sha256"] == report["source_head_sha256"]
    assert result["validation"]["finished"]


def test_finish_is_idempotent_restores_after_restart_and_next_press_starts_a_separate_round(tmp_path, clock):
    store = configured(tmp_path)
    save(store, clock)
    first = store.finish()["report"]
    first_sid = first["session_id"]
    log = store.root/first_sid/"records.jsonl"
    report_path = Path(first["path"])
    original_log, original_report = log.read_bytes(), report_path.read_bytes()
    assert store.finish()["report"] == first
    assert log.read_bytes() == original_log and report_path.read_bytes() == original_report
    restored = ValidationController(tmp_path)
    restored.configure(SHA_A, "M-a")
    assert restored.error is None and restored.snapshot()["finished"]
    assert restored.finish()["report"] == first
    assert log.read_bytes() == original_log and report_path.read_bytes() == original_report
    next_sample = restored.capture(evidence(clock, 2, error=.5))
    assert next_sample["validation_session_id"] != first_sid
    assert restored.snapshot()["finished"] is False
    assert restored.snapshot()["session_stats"]["judged_total"] == 0
    assert restored.snapshot()["stats"]["judged_total"] == 1
    assert restored.snapshot()["latest_report"] == first
    restored.judge(next_sample["sample_id"], False, False)
    second = restored.finish()["report"]
    assert second["report_id"] != first["report_id"] and second["session_id"] != first_sid
    assert second["stats"]["judged_total"] == 1 and second["stats"]["joint_correct"] == 0
    assert second["stats"]["mae_N"] == .5
    assert log.read_bytes() == original_log and report_path.read_bytes() == original_report
    again = ValidationController(tmp_path)
    again.configure(SHA_A, "M-a")
    assert again.error is None
    assert again.snapshot()["stats"]["judged_total"] == 2
    assert again.snapshot()["session_stats"]["judged_total"] == 1
    assert again.snapshot()["latest_report"] == second


def test_finished_reports_and_statistics_are_partitioned_by_model_checksum(tmp_path, clock):
    store = configured(tmp_path)
    save(store, clock)
    report_a = store.finish()["report"]
    store.configure(SHA_B, "M-b")
    save(store, clock, 2, checksum=SHA_B, error=.2, position=False, force=False)
    report_b = store.finish()["report"]
    assert report_a["model_sha256"] != report_b["model_sha256"]
    assert report_b["stats"]["judged_total"] == 1 and report_b["stats"]["joint_rate"] == 0
    assert report_b["stats"]["mae_N"] == .2
    assert store.snapshot(model_checksum=SHA_A)["stats"]["joint_rate"] == 1
    assert store.snapshot(model_checksum=SHA_A)["latest_report"] == report_a
    restored = ValidationController(tmp_path)
    assert restored.error is None
    restored.configure(SHA_A, "M-a")
    assert restored.finish()["report"] == report_a
    restored.configure(SHA_B, "M-b")
    assert restored.finish()["report"] == report_b


def test_same_checksum_alias_change_preserves_the_saved_session_identity(tmp_path, clock):
    store = configured(tmp_path)
    save(store, clock)
    sid = store.validation_session_id
    store.configure(SHA_A, "renamed-same-checksum")
    assert store.validation_session_id == sid
    report = store.finish()["report"]
    assert report["model_id"] == "M-a" and report["model_sha256"] == SHA_A
    assert report["stats"]["judged_total"] == 1
    assert read_events(store)[-1]["model_id"] == report["model_id"]
    restored = ValidationController(tmp_path)
    assert restored.error is None
    restored.configure(SHA_A, "another-model-alias")
    assert restored.finish()["report"] == report
    assert len(read_events(restored)) == 3
    assert restored.snapshot()["stats"]["judged_total"] == 1


def test_failed_capture_diagnostic_does_not_break_persistent_finish_report_replay(tmp_path, clock):
    store = configured(tmp_path)
    invalid = evidence(clock)
    invalid["stale"] = True
    with pytest.raises(ValueError):
        store.capture(invalid)
    assert store.snapshot()["stats"]["invalid_count"] == 1
    save(store, clock, 2)
    report = store.finish()["report"]
    assert "invalid_count" not in report["stats"]
    restored = ValidationController(tmp_path)
    restored.configure(SHA_A, "M-a")
    assert restored.error is None
    assert restored.snapshot()["stats"]["invalid_count"] == 0
    assert restored.finish()["report"] == report


@pytest.mark.parametrize("restart_before_retry", [False, True])
def test_report_file_write_failure_retries_same_sealed_report_without_duplicate_finish(tmp_path, clock, monkeypatch, restart_before_retry):
    store = configured(tmp_path)
    save(store, clock)
    original_replace = __import__("os").replace
    with monkeypatch.context() as failed:
        failed.setattr("ninefbg.validation.os.replace", lambda *args: (_ for _ in ()).throw(OSError("测试报告替换失败")))
        with pytest.raises((OSError, ValueError), match="测试报告替换失败"):
            store.finish()
    sealed = deepcopy(store.reports[store.validation_session_id])
    directory = store.root/store.validation_session_id
    before = (directory/"records.jsonl").read_bytes()
    assert not (directory/"report.json").exists()
    assert not list(directory.glob(".report-*.tmp"))
    assert len(read_events(store)) == 3 and read_events(store)[-1]["event"] == "finish"
    assert store.error is None  # A report export failure remains recoverable.
    if restart_before_retry:
        store = ValidationController(tmp_path)
        store.configure(SHA_A, "M-a")
        assert store.error is None
    result = store.finish()["report"]
    assert result["report_id"] == sealed["report_id"]
    assert (directory/"records.jsonl").read_bytes() == before
    assert json.loads((directory/"report.json").read_bytes()) == sealed
    assert __import__("os").replace is original_replace


def test_external_report_tampering_is_detected_and_never_overwritten(tmp_path, clock):
    store = configured(tmp_path)
    save(store, clock)
    report = store.finish()["report"]
    path = Path(report["path"])
    log = path.with_name("records.jsonl")
    original_log = log.read_bytes()
    tampered = path.read_bytes().replace(b'"judged_total":1', b'"judged_total":9')
    assert tampered != path.read_bytes()
    path.write_bytes(tampered)
    with pytest.raises(ValueError, match="报告与结束记录不一致"):
        store.finish()
    assert path.read_bytes() == tampered and log.read_bytes() == original_log
    restored = ValidationController(tmp_path)
    restored.configure(SHA_A, "M-a")
    with pytest.raises(ValueError, match="报告与结束记录不一致"):
        restored.finish()
    assert path.read_bytes() == tampered and log.read_bytes() == original_log


def test_rehashed_finish_claim_cannot_change_original_judgments_or_reopen_session(tmp_path, clock):
    store = configured(tmp_path)
    save(store, clock)
    report = store.finish()["report"]
    log = Path(report["path"]).with_name("records.jsonl")
    events = read_events(store)
    finish = events[-1]
    finish["report"]["stats"]["judged_total"] = 9
    finish["report"]["report_sha256"] = hashlib.sha256(canonical({key: value for key, value in finish["report"].items() if key != "report_sha256"})).hexdigest()
    finish["record_sha256"] = hashlib.sha256(canonical({key: value for key, value in finish.items() if key != "record_sha256"})).hexdigest()
    log.write_bytes(b"".join(canonical(event)+b"\n" for event in events))
    changed = log.read_bytes()
    restored = ValidationController(tmp_path)
    restored.configure(SHA_A, "M-a")
    assert "人工判断不一致" in restored.error
    with pytest.raises(ValueError, match="停止写入"):
        restored.finish()
    assert log.read_bytes() == changed


def test_http_finish_route_rejects_empty_round_and_seals_saved_comparison_once(tmp_path, clock):
    from functools import partial
    import threading
    import urllib.error
    import urllib.request
    from ninefbg.server import Handler, LocalServer
    from ninefbg.service import AcquisitionService

    userdata = tmp_path/"isolated_userdata"
    service = AcquisitionService(Path(__file__).resolve().parents[1], userdata)
    service.validation.configure(SHA_A, "M-a")
    assert not service.running and not service.sources and not service.workers
    server = LocalServer(("127.0.0.1", 0), partial(Handler, service=service))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/api/validation/finish"

    def request():
        return urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})

    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request(), timeout=5)
        assert caught.value.code == 400
        assert "尚无已保存" in json.load(caught.value)["error"]
        assert not service.validation.root.exists()
        sample = save(service.validation, clock)
        with urllib.request.urlopen(request(), timeout=5) as response:
            assert response.status == 200
            first = json.load(response)
        report = first["report"]
        assert first["validation"]["finished"]
        assert first["validation"]["latest_report"] == report
        assert report["stats"]["judged_total"] == 1 and report["stats"]["joint_correct"] == 1
        assert report["records"][0]["sample_id"] == sample["sample_id"]
        path = Path(report["path"])
        assert path.is_relative_to(userdata)
        assert json.loads(path.read_bytes()) == {key: value for key, value in report.items() if key != "path"}
        log = path.with_name("records.jsonl")
        before = log.read_bytes()
        with urllib.request.urlopen(request(), timeout=5) as response:
            assert response.status == 200
            repeated = json.load(response)
        assert repeated["report"] == report and repeated["validation"]["latest_report"] == report
        assert log.read_bytes() == before
        assert sum(event["event"] == "finish" for event in read_events(service.validation)) == 1
        assert not service.running and not service.sources and not service.workers
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive()
