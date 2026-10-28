from copy import deepcopy
import hashlib
import json
import math
from types import SimpleNamespace

import pytest

from ninefbg.validation import ValidationController

SHA_A, SHA_B = "a" * 64, "b" * 64


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=10_000_000_000)
    monkeypatch.setattr("ninefbg.validation.time.perf_counter_ns", lambda: value.now)
    return value


def evidence(clock, frame_id=1, *, checksum=SHA_A, error=0.1):
    identity = {"frame_id": frame_id, "optical_epoch": "optical-1", "force_epoch": "force-1",
                "reference_version": 3, "algorithm_version": "fixed-peaks-1", "config_version": "config-1",
                "model_checksum": checksum}
    stamp = clock.now - 10_000_000
    source = {"frame_id": frame_id + 100, "stream_epoch": "force-1", "source_kind": "hardware",
              "host_rx_monotonic_ns": stamp, "force_values": [0.1, 0.2, -1, 0.01, 0.02, 0.03],
              "axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"], "units": ["N"] * 3 + ["N·m"] * 3}
    return {**identity, "model_id": "M-a" if checksum == SHA_A else "M-b", "valid": True,
            "source_kind": "hardware", "stale": False, "age_ms": 10, "force_age_ms": 10,
            "test_reference_ready": True, "truth_zero_ready": True, "reference_frozen": True,
            "experiment_running": False, "host_rx_monotonic_ns": stamp,
            "prediction_context": deepcopy(identity), "truth_context": deepcopy(identity),
            "x_pm": list(range(9)), "force_zero": [0] * 6, "force_sign": -1,
            "prediction": {"status": "有效估计", "point_id": "P01", "force_N": 1 + error,
                           "truth_force_N": 1, "error_N": error},
            "sync": {"valid": True, "matched": True, "method": "exact_receive_time",
                     "spectrum_frame_id": frame_id, "spectrum_stream_epoch": "optical-1",
                     "force_stream_epoch": "force-1", "force_frame_ids": [source["frame_id"]],
                     "force_values": list(source["force_values"])},
            "force_source_frames": [source]}


def configured(tmp_path):
    store = ValidationController(tmp_path)
    store.configure(SHA_A, "M-a")
    return store


def records(tmp_path):
    return [json.loads(line) for path in (tmp_path / "validation_sessions").glob("*/records.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


def test_no_directory_or_success_claim_before_explicit_valid_capture(tmp_path):
    store = configured(tmp_path)
    assert not (tmp_path / "validation_sessions").exists()
    snapshot = store.snapshot()
    assert snapshot["pending_sample"] is None and snapshot["stats"]["judged_total"] == 0
    for field in ("position_rate", "force_rate", "joint_rate", "mae_N", "rmse_N", "MAE_N", "RMSE_N"):
        assert snapshot["stats"][field] is None
    assert snapshot["stats"]["status"] == "尚无模型推理判断" and not snapshot["formal_ready"]
    assert snapshot["model_checksum"] == SHA_A and snapshot["force_tolerance_N"] == 0.3
    assert store.configure(SHA_A, "M-a")["validation_session_id"] == snapshot["validation_session_id"]


def test_freeze_is_immutable_idempotent_and_requires_explicit_pending_resolution(tmp_path, clock):
    store = configured(tmp_path)
    source = evidence(clock)
    frozen = store.capture(source)
    assert store.capture(deepcopy(source))["sample_id"] == frozen["sample_id"]
    assert len(records(tmp_path)) == 1
    source["prediction"]["force_N"] = 999
    frozen["evidence"]["x_pm"][0] = 999
    frozen["prediction"]["point_id"] = "P25"
    pending = store.snapshot()["pending_sample"]
    assert pending["evidence"]["x_pm"] == list(range(9))
    assert pending["prediction"]["point_id"] == "P01" and pending["prediction"]["force_N"] == 1.1
    assert pending["raw_force_available"] and pending["evidence"]["force_source_frames"][0]["force_values"][2] == -1
    assert pending["created_utc"].endswith("+00:00") and pending["reference_version"] == 3
    with pytest.raises(ValueError, match="保存或明确放弃"):
        store.capture(evidence(clock, 2))
    assert store.snapshot()["stats"]["judged_total"] == 0


def test_negative_judgment_counts_once_and_cannot_be_changed_or_removed(tmp_path, clock):
    store = configured(tmp_path)
    sample = store.capture(evidence(clock))
    result = store.judge(sample["sample_id"], False, False, "P05")
    stats = result["stats"]
    assert (stats["judged_total"], stats["position_correct"], stats["force_correct"], stats["joint_correct"]) == (1, 0, 0, 0)
    assert stats["position_rate"] == stats["force_rate"] == stats["joint_rate"] == 0
    assert stats["mae_N"] == pytest.approx(0.1) and stats["rmse_N"] == pytest.approx(0.1)
    assert result["pending_sample"] is None and result["last_saved_sample_id"] == sample["sample_id"]
    assert store.judge(sample["sample_id"], False, False, "P05")["stats"]["judged_total"] == 1
    assert len(records(tmp_path)) == 2
    with pytest.raises(ValueError, match="改判"):
        store.judge(sample["sample_id"], True, False)
    with pytest.raises(ValueError, match="分母"):
        store.discard(sample["sample_id"])
    judged = records(tmp_path)[1]
    assert judged["judgment"]["actual_point_id"] == "P05" and judged["judgment"]["source"] == "operator"
    assert "actual_point_id" not in records(tmp_path)[0]["sample"]["evidence"]


@pytest.mark.parametrize("error", [0.3, -0.3])
def test_exact_force_tolerance_boundary_is_accepted_by_manual_choice(tmp_path, clock, error):
    store = configured(tmp_path)
    sample = store.capture(evidence(clock, error=error))
    result = store.judge(sample["sample_id"], True, True, "P01")
    assert result["stats"]["force_rate"] == result["stats"]["joint_rate"] == 1


def test_force_tolerance_and_actual_position_disagreement_are_enforced_server_side(tmp_path, clock):
    store = configured(tmp_path)
    sample = store.capture(evidence(clock, error=0.300001))
    with pytest.raises(ValueError, match="0.3 N"):
        store.judge(sample["sample_id"], True, True)
    with pytest.raises(ValueError, match="实际点位"):
        store.judge(sample["sample_id"], True, False, "P25")
    for args in ((1, False, None), (False, 0, None), (False, False, "P26"), (False, False, [])):
        with pytest.raises(ValueError):
            store.judge(sample["sample_id"], *args)
    assert store.snapshot()["stats"]["judged_total"] == 0
    assert len(records(tmp_path)) == 1
    assert store.judge(sample["sample_id"], False, False, "P25")["stats"]["judged_total"] == 1


@pytest.mark.parametrize("field,value", [
    ("valid", False), ("stale", True), ("source_kind", "replay"), ("age_ms", 1000.01),
    ("force_age_ms", 250.01), ("test_reference_ready", False), ("truth_zero_ready", False),
    ("experiment_running", True), ("reference_frozen", False), ("reference_version", 0),
    ("x_pm", [0] * 8), ("x_pm", [0] * 8 + [float("nan")]), ("force_zero", [0] * 5),
])
def test_invalid_comparisons_never_create_record_or_denominator(tmp_path, clock, field, value):
    store = configured(tmp_path)
    snapshot = evidence(clock)
    snapshot[field] = value
    with pytest.raises(ValueError):
        store.capture(snapshot)
    assert not (tmp_path / "validation_sessions").exists()
    assert store.snapshot()["stats"]["judged_total"] == 0
    assert store.snapshot()["stats"]["joint_rate"] is None


def test_source_clock_checks_old_snapshot_even_when_claimed_age_is_fresh(tmp_path, clock):
    store = configured(tmp_path)
    old = evidence(clock)
    clock.now += 2_000_000_000
    with pytest.raises(ValueError, match="采集时间"):
        store.capture(old)
    assert not (tmp_path / "validation_sessions").exists()


@pytest.mark.parametrize("context,key", [(name, key) for name in ("prediction_context", "truth_context")
                                         for key in ("frame_id", "optical_epoch", "force_epoch", "reference_version", "algorithm_version", "config_version", "model_checksum")])
def test_cross_frame_epoch_reference_algorithm_or_model_is_rejected(tmp_path, clock, context, key):
    store = configured(tmp_path)
    snapshot = evidence(clock)
    snapshot[context][key] = "wrong"
    with pytest.raises(ValueError, match="同一帧"):
        store.capture(snapshot)
    assert not (tmp_path / "validation_sessions").exists()


@pytest.mark.parametrize("key,value", [("valid", False), ("matched", False), ("spectrum_frame_id", 999),
                                       ("spectrum_stream_epoch", "different"), ("force_stream_epoch", "different"),
                                       ("method", "nearest_diagnostic_only"), ("force_frame_ids", []),
                                       ("force_values", [0] * 5)])
def test_invalid_or_untraceable_sync_cannot_become_a_saved_comparison(tmp_path, clock, key, value):
    store = configured(tmp_path)
    source = evidence(clock)
    source["sync"][key] = value
    with pytest.raises(ValueError):
        store.capture(source)
    assert store.snapshot()["stats"]["judged_total"] == 0


def test_forged_truth_or_error_is_recomputed_from_frozen_sync_and_zero(tmp_path, clock):
    store = configured(tmp_path)
    for field in ("truth_force_N", "error_N"):
        source = evidence(clock)
        source["prediction"][field] = 88
        with pytest.raises(ValueError, match="配对六轴值不一致"):
            store.capture(source)


def test_original_force_pair_is_preserved_and_reproduces_interpolation(tmp_path, clock):
    store = configured(tmp_path)
    source = evidence(clock)
    stamp = source["host_rx_monotonic_ns"]
    left, right = deepcopy(source["force_source_frames"][0]), deepcopy(source["force_source_frames"][0])
    left.update(frame_id=100, host_rx_monotonic_ns=stamp-10_000_000)
    right.update(frame_id=101, host_rx_monotonic_ns=stamp+10_000_000)
    left["force_values"][2], right["force_values"][2] = -1, -3
    source["force_source_frames"] = [left, right]
    source["sync"].update(method="linear_receive_time", force_frame_ids=[100, 101], bracket_ms=20)
    source["sync"]["force_values"][2] = -2
    source["prediction"].update(force_N=2.1, truth_force_N=2)
    frozen = store.capture(source)
    assert frozen["evidence"]["force_source_frames"] == [left, right]
    assert frozen["evidence"]["sync"]["force_values"][2] == -2
    store.discard(frozen["sample_id"])
    source["frame_id"] = source["prediction_context"]["frame_id"] = source["truth_context"]["frame_id"] = source["sync"]["spectrum_frame_id"] = 2
    source["force_source_frames"][1]["force_values"][2] = -4
    with pytest.raises(ValueError, match="重算结果"):
        store.capture(source)


def test_discard_and_saved_freeze_survive_restart_without_entering_denominator(tmp_path, clock):
    store = configured(tmp_path)
    frozen = store.capture(evidence(clock))
    sid = frozen["validation_session_id"]
    clock.now += 100_000_000_000  # Frozen evidence remains a historical observation.
    restored = ValidationController(tmp_path)
    assert restored.error is None
    assert restored.configure(SHA_A, "M-a")["validation_session_id"] == sid
    assert restored.snapshot()["pending_sample"]["sample_id"] == frozen["sample_id"]
    assert restored.discard(frozen["sample_id"])["stats"]["judged_total"] == 0
    assert restored.discard(frozen["sample_id"])["pending_sample"] is None
    with pytest.raises(ValueError, match="放弃"):
        restored.judge(frozen["sample_id"], False, False)
    again = ValidationController(tmp_path)
    again.configure(SHA_A, "M-a")
    assert again.snapshot()["stats"]["judged_total"] == again.snapshot()["stats"]["pending_count"] == 0
    next_sample = again.capture(evidence(clock, 2))
    assert again.judge(next_sample["sample_id"], False, False)["stats"]["judged_total"] == 1


def test_pending_can_be_judged_after_restart_using_frozen_values_not_new_time(tmp_path, clock):
    store = configured(tmp_path)
    sample = store.capture(evidence(clock, error=0.2))
    clock.now += 100_000_000_000
    restored = ValidationController(tmp_path)
    restored.configure(SHA_A, "M-a")
    result = restored.judge(sample["sample_id"], True, True)
    assert result["stats"]["judged_total"] == 1 and result["stats"]["mae_N"] == pytest.approx(0.2)
    resumed = ValidationController(tmp_path)
    resumed.configure(SHA_A, "M-a")
    assert resumed.snapshot()["stats"] == result["stats"]


def test_models_and_validation_sessions_are_partitioned_and_rmse_counts_all_labels(tmp_path, clock):
    store = configured(tmp_path)
    sample = store.capture(evidence(clock, error=0.1))
    first_sid = sample["validation_session_id"]
    store.judge(sample["sample_id"], True, True)
    store.configure(SHA_A, "M-a", new_session=True)
    sample = store.capture(evidence(clock, 2, error=0.5))
    second_sid = sample["validation_session_id"]
    store.judge(sample["sample_id"], False, False)
    stats = store.snapshot()["stats"]
    assert stats["judged_total"] == 2 and stats["joint_rate"] == 0.5
    assert stats["mae_N"] == pytest.approx(0.3)
    assert stats["rmse_N"] == pytest.approx(math.sqrt((0.1**2 + 0.5**2)/2))
    assert store.snapshot(session_id=first_sid)["stats"]["joint_rate"] == 1
    assert store.snapshot(session_id=second_sid)["stats"]["joint_rate"] == 0
    store.configure(SHA_B, "M-b")
    other = store.capture(evidence(clock, 3, checksum=SHA_B, error=0))
    assert store.judge(other["sample_id"], True, False)["stats"]["judged_total"] == 1
    assert store.snapshot(model_sha256=SHA_A)["stats"]["judged_total"] == 2
    assert store.snapshot(model_id="M-a")["stats"]["judged_total"] == 2
    assert store.snapshot(model_id="unknown")["stats"]["judged_total"] == 0
    with pytest.raises(ValueError, match="切回"):
        store.judge(sample["sample_id"], False, False)
    replay = ValidationController(tmp_path)
    replay.configure(SHA_A, "M-a")
    assert replay.snapshot()["stats"]["judged_total"] == 2
    replay.configure(SHA_B, "M-b")
    assert replay.snapshot()["stats"]["judged_total"] == 1


def test_file_hash_chain_is_append_only_and_corruption_fails_closed(tmp_path, clock):
    store = configured(tmp_path)
    sample = store.capture(evidence(clock))
    file, = (tmp_path / "validation_sessions").glob("*/records.jsonl")
    original = file.read_bytes()
    store.judge(sample["sample_id"], False, False)
    saved = file.read_bytes()
    assert saved.startswith(original) and len(saved.splitlines()) == 2
    first, second = [json.loads(line) for line in saved.splitlines()]
    assert second["previous_sha256"] == first["record_sha256"]
    assert first["sample"]["evidence_sha256"] == hashlib.sha256(json.dumps(first["sample"]["evidence"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    file.write_bytes(saved.replace(b'"point_id":"P01"', b'"point_id":"P25"', 1))
    damaged = file.read_bytes()
    restored = ValidationController(tmp_path)
    assert "校验失败" in restored.error
    restored.configure(SHA_A, "M-a")
    with pytest.raises(ValueError, match="停止写入"):
        restored.capture(evidence(clock, 2))
    assert file.read_bytes() == damaged


def test_fsync_failure_never_publishes_an_unsaved_judgment_count(tmp_path, clock, monkeypatch):
    store = configured(tmp_path)
    sample = store.capture(evidence(clock))
    monkeypatch.setattr("ninefbg.validation.os.fsync", lambda fd: (_ for _ in ()).throw(OSError("测试同步写入失败")))
    with pytest.raises(ValueError, match="保存失败"):
        store.judge(sample["sample_id"], True, True)
    assert store.snapshot()["stats"]["judged_total"] == 0 and store.snapshot()["error"]
    file, = (tmp_path / "validation_sessions").glob("*/records.jsonl")
    preserved = file.read_bytes()
    with pytest.raises(ValueError, match="停止写入"):
        store.discard(sample["sample_id"])
    assert file.read_bytes() == preserved


def test_missing_raw_force_values_are_explicitly_distinct_from_interpolated_values(tmp_path, clock):
    store = configured(tmp_path)
    source = evidence(clock)
    del source["force_source_frames"]
    sample = store.capture(source)
    assert sample["raw_force_available"] is False
    assert sample["evidence"]["sync"]["force_values"] == source["sync"]["force_values"]


def test_configure_same_model_preserves_current_explicit_session(tmp_path, clock):
    store = configured(tmp_path)
    first = store.validation_session_id
    newest = store.configure(SHA_A, "M-a", new_session=True)["validation_session_id"]
    assert newest != first
    assert store.configure(SHA_A, "M-a")["validation_session_id"] == newest
    sample = store.capture(evidence(clock))
    assert sample["validation_session_id"] == newest


def test_pair_source_age_and_fixed_peak_identity_are_independently_checked(tmp_path, clock):
    store = configured(tmp_path)
    snapshot = evidence(clock)
    snapshot["force_source_frames"][0]["host_rx_monotonic_ns"] -= 300_000_000
    with pytest.raises(ValueError, match="原始六轴帧已过期"):
        store.capture(snapshot)
    snapshot = evidence(clock)
    snapshot["feature_ids"] = [f"FBG{i}" for i in range(9, 0, -1)]
    with pytest.raises(ValueError, match="FBG1"):
        store.capture(snapshot)
    assert not (tmp_path / "validation_sessions").exists()


def test_boolean_is_not_an_integer_source_identity(tmp_path, clock):
    store = configured(tmp_path)
    for section in ("prediction_context", "truth_context", "sync"):
        source = evidence(clock, frame_id=1)
        source[section]["spectrum_frame_id" if section == "sync" else "frame_id"] = True
        with pytest.raises(ValueError):
            store.capture(source)
    assert not (tmp_path / "validation_sessions").exists()


@pytest.mark.parametrize("status,raw_force", [("定位不确定", None), ("超范围", None), ("无接触", 0.0)])
def test_fresh_pressed_rejection_counts_as_failure_without_fabricated_force_error(tmp_path, clock, status, raw_force):
    store = configured(tmp_path)
    source = evidence(clock)
    source["prediction"].update(status=status, point_id=None, force_N=raw_force, error_N=None,
                                comparison_ready=True, reason="模型拒识原因")
    sample = store.capture(source)
    assert sample["valid"] and sample["comparison_ready"] and sample["inference_valid"] is False
    assert sample["prediction"]["status"] == status
    assert sample["prediction"]["truth_force_N"] == 1
    assert all(sample["prediction"][key] is None for key in ("point_id", "force_N", "error_N"))
    assert sample["absolute_error_N"] is None
    assert sample["evidence"]["prediction"]["force_N"] == raw_force
    for position, force in ((True, False), (False, True), (True, True)):
        with pytest.raises(ValueError, match="拒识"):
            store.judge(sample["sample_id"], position, force)
    result = store.judge(sample["sample_id"], False, False, "P05")
    stats = result["stats"]
    assert stats["judged_total"] == stats["inference_rejected"] == 1
    assert stats["position_rate"] == stats["force_rate"] == stats["joint_rate"] == 0
    assert stats["force_error_count"] == 0
    assert stats["mae_N"] is stats["rmse_N"] is stats["MAE_N"] is stats["RMSE_N"] is None
    assert store.judge(sample["sample_id"], False, False, "P05")["stats"]["judged_total"] == 1
    restored = ValidationController(tmp_path)
    restored.configure(SHA_A, "M-a")
    assert restored.error is None and restored.snapshot()["stats"] == stats
    second = restored.capture(evidence(clock, 2, error=0.2))
    combined = restored.judge(second["sample_id"], True, True)["stats"]
    assert combined["judged_total"] == 2 and combined["inference_rejected"] == 1
    assert combined["force_error_count"] == 1
    assert combined["position_rate"] == combined["force_rate"] == combined["joint_rate"] == 0.5
    assert combined["mae_N"] == combined["rmse_N"] == pytest.approx(0.2)
    replay = ValidationController(tmp_path)
    replay.configure(SHA_A, "M-a")
    assert replay.snapshot()["stats"] == combined


@pytest.mark.parametrize("truth", [0.05, 0, -0.1])
@pytest.mark.parametrize("status", ["有效估计", "无接触"])
def test_unloaded_truth_cannot_be_frozen_as_a_press_even_for_rejection(tmp_path, clock, truth, status):
    store = configured(tmp_path)
    source = evidence(clock)
    source["sync"]["force_values"][2] = source["force_source_frames"][0]["force_values"][2] = -truth
    source["prediction"].update(status=status, truth_force_N=truth, comparison_ready=True,
                                force_N=truth+0.1 if status == "有效估计" else 0,
                                error_N=0.1 if status == "有效估计" else None,
                                point_id="P01" if status == "有效估计" else None)
    with pytest.raises(ValueError, match="0.05 N"):
        store.capture(source)
    assert not (tmp_path / "validation_sessions").exists()
    assert store.snapshot()["stats"]["judged_total"] == 0


@pytest.mark.parametrize("field,value", [("stale", True), ("force_age_ms", 251),
                                         ("x_pm", [0] * 8 + [float("nan")]), ("truth_zero_ready", False)])
def test_rejection_does_not_relax_acquisition_validity_guards(tmp_path, clock, field, value):
    store = configured(tmp_path)
    source = evidence(clock)
    source["prediction"].update(status="定位不确定", point_id=None, force_N=None, error_N=None, comparison_ready=True)
    source[field] = value
    with pytest.raises(ValueError):
        store.capture(source)
    assert store.snapshot()["stats"]["judged_total"] == 0
    assert not (tmp_path / "validation_sessions").exists()


def test_rejection_requires_comparison_ready_and_cannot_accept_unknown_model_status(tmp_path, clock):
    store = configured(tmp_path)
    for status, ready in (("定位不确定", False), ("无效", True), ("尚无模型", True)):
        source = evidence(clock)
        source["prediction"].update(status=status, point_id=None, force_N=None, error_N=None, comparison_ready=ready)
        with pytest.raises(ValueError):
            store.capture(source)
    assert not (tmp_path / "validation_sessions").exists()


@pytest.mark.parametrize("target", ["P00", "P26", "p01", " P01", "", 1, True, []])
def test_invalid_target_never_creates_a_frozen_record(tmp_path, clock, target):
    store = configured(tmp_path)
    source = evidence(clock)
    source["target_point_id"] = target
    with pytest.raises(ValueError, match="目标点位"):
        store.capture(source)
    assert not (tmp_path / "validation_sessions").exists()
    assert store.snapshot()["stats"]["judged_total"] == 0


def test_target_is_frozen_separately_from_prediction_and_judgment_cannot_change_it(tmp_path, clock):
    store = configured(tmp_path)
    source = evidence(clock)
    source["target_point_id"] = "P05"
    original = deepcopy(source)
    sample = store.capture(source)
    assert source == original
    assert sample["target_point_id"] == sample["actual_point_id"] == "P05"
    assert sample["evidence"]["target_point_id"] == "P05"
    assert sample["prediction"] == original["prediction"]
    assert sample["evidence"]["x_pm"] == original["x_pm"]
    sample["target_point_id"] = "P01"
    source["target_point_id"] = "P01"
    pending = store.snapshot()["pending_sample"]
    assert pending["target_point_id"] == pending["actual_point_id"] == "P05"
    with pytest.raises(ValueError, match="目标点位已"):
        store.judge(pending["sample_id"], False, False, "P01")
    with pytest.raises(ValueError, match="实际点位与冻结预测不同"):
        store.judge(pending["sample_id"], True, False)
    stats = store.judge(pending["sample_id"], False, True)["stats"]
    assert stats["position_correct"] == 0 and stats["force_correct"] == 1
    assert stats["mae_N"] == pytest.approx(0.1)
    saved = records(tmp_path)[1]["judgment"]
    assert saved["actual_point_id"] == "P05"
    assert store.judge(pending["sample_id"], False, True, "P05")["stats"] == stats


def test_target_and_actual_restore_after_restart_and_default_judgment_is_idempotent(tmp_path, clock):
    store = configured(tmp_path)
    source = evidence(clock)
    source["target_point_id"] = "P01"
    sample = store.capture(source)
    restored = ValidationController(tmp_path)
    restored.configure(SHA_A, "M-a")
    pending = restored.snapshot()["pending_sample"]
    assert pending["target_point_id"] == pending["actual_point_id"] == "P01"
    assert pending["prediction"] == source["prediction"]
    result = restored.judge(sample["sample_id"], True, True)
    assert result["stats"]["joint_correct"] == 1
    assert restored.judge(sample["sample_id"], True, True, "P01")["stats"]["judged_total"] == 1
    again = ValidationController(tmp_path)
    again.configure(SHA_A, "M-a")
    assert again.error is None and again.snapshot()["stats"] == result["stats"]


def test_same_source_frame_cannot_be_relabeled_even_after_discard(tmp_path, clock):
    store = configured(tmp_path)
    source = evidence(clock)
    source["target_point_id"] = "P01"
    frozen = store.capture(source)
    store.discard(frozen["sample_id"])
    source["target_point_id"] = "P05"
    with pytest.raises(ValueError, match="此源帧的目标点位已"):
        store.capture(source)
    assert store.snapshot()["stats"]["judged_total"] == 0
    assert len(records(tmp_path)) == 2
    fresh = evidence(clock, 2)
    fresh["target_point_id"] = "P05"
    next_sample = store.capture(fresh)
    assert next_sample["sample_id"] != frozen["sample_id"]
    assert next_sample["target_point_id"] == "P05"
    assert next_sample["prediction"] == frozen["prediction"]


@pytest.mark.parametrize("status,raw_force", [("定位不确定", None), ("超范围", None), ("无接触", 0.0)])
def test_target_does_not_turn_a_rejection_into_a_correct_prediction(tmp_path, clock, status, raw_force):
    store = configured(tmp_path)
    source = evidence(clock)
    source["target_point_id"] = "P25"
    source["prediction"].update(status=status, point_id=None, force_N=raw_force, error_N=None, comparison_ready=True)
    sample = store.capture(source)
    assert sample["target_point_id"] == sample["actual_point_id"] == "P25"
    assert sample["prediction"]["point_id"] is None
    with pytest.raises(ValueError, match="拒识"):
        store.judge(sample["sample_id"], True, False)
    result = store.judge(sample["sample_id"], False, False)
    assert result["stats"]["inference_rejected"] == result["stats"]["judged_total"] == 1
    assert result["stats"]["force_error_count"] == 0 and result["stats"]["mae_N"] is None
    assert records(tmp_path)[1]["judgment"]["actual_point_id"] == "P25"


def test_legacy_records_without_target_remain_readable_without_backfill_or_rewrite(tmp_path, clock):
    store = configured(tmp_path)
    sample = store.capture(evidence(clock))
    expected = store.judge(sample["sample_id"], False, False, "P05")["stats"]
    events = records(tmp_path)
    events[0]["sample"].pop("target_point_id")
    events[0]["sample"].pop("actual_point_id")
    previous = None
    lines = []
    for event in events:
        event["previous_sha256"] = previous
        event.pop("record_sha256")
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        event["record_sha256"] = hashlib.sha256(encoded).hexdigest()
        previous = event["record_sha256"]
        lines.append(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n")
    path, = (tmp_path / "validation_sessions").glob("*/records.jsonl")
    path.write_bytes(b"".join(lines))
    original = path.read_bytes()
    restored = ValidationController(tmp_path)
    restored.configure(SHA_A, "M-a")
    assert restored.error is None and restored.snapshot()["stats"] == expected
    assert restored.samples[sample["sample_id"]]["target_point_id"] is None
    assert restored.samples[sample["sample_id"]]["actual_point_id"] is None
    assert restored.judgments[sample["sample_id"]]["actual_point_id"] == "P05"
    assert path.read_bytes() == original
