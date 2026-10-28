"""Native control guards; generated snapshots are not physical acceptance."""
from copy import deepcopy
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QApplication

from ninefbg.qt_experiment import (
    COORDINATE_SCHEMA, ExperimentWorkspace, POINT_IDS, capture_gates,
    point_layout, training_status,
)
from ninefbg.capture_setup import SETUP_POLICY
from test_experiment import clock, context as acquisition_context
from test_force_target_wrench import start_target, begin, recover_to_saving, await_checkpoint
from test_timed_wrench import tick


class FakeContext(QObject):
    action_finished = pyqtSignal(str, object)
    snapshot_ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, preferences=None):
        super().__init__()
        self.calls = []
        self.preferences = preferences or {}

    def execute(self, action, payload=None):
        self.calls.append((action, deepcopy(payload)))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def workspace(app):
    context = FakeContext()
    widget = ExperimentWorkspace(context)
    widget.update_state(state())
    yield widget, context
    widget._fresh_timer.stop()
    widget._preference_timer.stop()
    widget.close()
    widget.deleteLater()
    app.processEvents()


def state(**changes):
    value = {"product": "9FBG", "running": True, "mode": "hardware", "force_connected": True,
             "timing": {"sample_rate_hz": 25}, "experiment": {}, "analysis": {}, "validation": {}}
    value.update(changes)
    return value


def experiment(step="needs_baseline", **changes):
    value = {"running": True, "session_id": "E1", "point_id": "P01", "phase": "idle",
             "settings": {"acquisition_protocol": "force_target_wrench_v1", "coordinates_confirmed": True},
             "guided": {"enabled": True, "step": step}, "baseline": {"ready": False}}
    value.update(changes)
    return value


def session(sid="E1", **changes):
    value = {"session_id": sid, "complete": True, "has_training_data": True, "trainable": True,
             "settings": {"batch": "A"}, "accepted_trial_count": 3, "trial_count": 3}
    value.update(changes)
    return value


def model(mid="S1", method="spectral_gru", **changes):
    value = {"model_id": mid, "model_kind": "spectral_gru", "method": method,
             "loadable": True, "deletable": True, "restorable": True}
    value.update(changes)
    return value


def inference(**changes):
    value = {"model_id": "S1", "model_kind": "spectral_gru", "inference_active": True,
             "trained_point_ids": ["P01", "P02"], "test_reference": {"ready": True},
             "prediction": {"schema": "spectral_prediction_v1", "valid": True, "contact": True,
                            "status": "有效估计", "point_id": "P01", "comparison_ready": True,
                            "wrench_pred": [1., 2., 3., .1, .2, .3], "wrench_truth": [1., 2., 3., .1, .2, .3],
                            "wrench_error": [0.] * 6}}
    value.update(changes)
    return value


def test_current_grid_bottom_left_and_historical_geometry_are_not_reassigned():
    points, current, known = point_layout()
    assert current and known
    assert points[0]["id"] == "P21"
    assert points[20]["id"] == "P01"
    unknown, current, known = point_layout({}, True)
    assert [p["id"] for p in unknown] == list(POINT_IDS)
    assert not current and not known
    recorded = [{"id": pid, "row": i // 5, "col": i % 5, "u": i % 5 / 4, "v": i // 5 / 4} for i, pid in enumerate(POINT_IDS)]
    legacy, current, known = point_layout({"point_grid": recorded}, True)
    assert legacy == recorded and known and not current
    corrected, current, _known = point_layout({"point_grid": recorded, "coordinate_definition": {"schema": COORDINATE_SCHEMA}}, True)
    assert current and corrected[0]["id"] == "P21"


@pytest.mark.parametrize("changes,method", [
    ({"complete": False}, "spectral_gru"),
    ({"settings": {"batch": "C"}}, "spectral_gru"),
    ({"deleted": True}, "spectral_gru"),
    ({"diagnostic": True}, "spectral_gru"),
    ({"capture_mode": "peak_drift"}, "spectral_gru"),
    ({"drift_available": False}, "drift_gru"),
    ({"settings": {"batch": "A", "acquisition_protocol": "force_target_wrench_v1"}}, "linear"),
])
def test_incompatible_training_records_report_a_reason(changes, method):
    allowed, reason = training_status(session(**changes), method)
    assert allowed is False and reason


def test_capture_start_uses_configured_geometry_and_requires_real_25_hz(workspace):
    widget, context = workspace
    widget.finger_id.setText("样件 <原样> 01")
    widget.update_state(state(timing={"sample_rate_hz": 50}))
    widget._start_capture()
    assert not context.calls
    widget.update_state(state(devices=[{"sdk_status_raw": {"interval_ms": 40}}], timing={"sample_rate_hz": 50}))
    widget._start_capture()
    action, payload = context.calls[-1]
    assert action == "experiment/start"
    assert payload["finger_id"] == "样件 <原样> 01"
    assert payload["setup_policy"] == SETUP_POLICY
    assert "coordinates_confirmed" not in payload
    assert "force_direction_confirmed" not in payload
    assert "temperature_C" not in payload["conditions"]
    assert payload["target_force_norm_N"] == 4
    assert payload["max_force_norm_N"] == 5
    assert "unloaded_confirmed" not in payload


@pytest.mark.parametrize("protocol", ["force_target_wrench_v1", "guided_round_v1"])
def test_configured_geometry_trials_still_require_ready_unloaded_baseline(workspace, protocol):
    widget, context = workspace
    exp = experiment("ready", settings={"acquisition_protocol": protocol, "setup_policy": SETUP_POLICY,
                                       "coordinates_confirmed": False, "force_direction_confirmed": False})
    widget.update_state(state(experiment=exp))
    assert not widget.trial_start_button.isEnabled()
    exp["baseline"] = {"ready": True}
    widget.update_state(state(experiment=exp))
    assert widget.trial_start_button.isEnabled()
    widget.trial_start_button.click()
    assert context.calls[-1] == ("experiment/trial", {"action": "start"})


def test_grid_selection_is_only_preselection_and_requires_confirmation(workspace):
    widget, context = workspace
    widget.update_state(state(experiment=experiment()))
    widget.capture_grid.buttons["P02"].click()
    assert widget.pending_point == "P02"
    assert not context.calls
    assert not widget.baseline_button.isEnabled()
    widget.confirm_point_button.click()
    assert context.calls[-1] == ("experiment/point", {"point_id": "P02"})


def test_baseline_requires_fresh_explicit_unloaded_confirmation(workspace):
    widget, context = workspace
    widget.update_state(state(experiment=experiment()))
    widget._baseline()
    assert not context.calls
    widget.unloaded_confirmed.setChecked(True)
    widget.baseline_button.click()
    assert context.calls[-1] == ("experiment/baseline", {"unloaded_confirmed": True})
    assert not widget.unloaded_confirmed.isChecked()


def test_no_contact_and_manual_finish_wait_for_backend_data_gate(workspace):
    widget, context = workspace
    exp = experiment("no_contact", point_id="NO_CONTACT", trial_id="T1", phase="no_contact")
    widget.update_state(state(experiment=exp))
    widget.pending_point = "NO_CONTACT"
    widget._finish_trial()
    assert not context.calls
    exp["guided"]["can_finish"] = True
    widget.update_state(state(experiment=exp))
    widget._finish_trial()
    assert context.calls[-1] == ("experiment/trial", {"action": "finish"})
    assert capture_gates(state(experiment=exp), "NO_CONTACT")["point"] is False


def test_real_manager_ready_round_enables_native_finish_and_saves_accepted_trial(workspace, tmp_path, clock):
    widget, context = workspace
    manager = start_target(tmp_path, clock, target_force_norm_N=3, max_force_norm_N=4)
    try:
        widget.update_state(state(experiment=manager.snapshot()))
        assert widget.capture_grid.buttons["P01"].property("captureState") == "confirmed"
        begin(manager, clock)
        widget.update_state(state(experiment=manager.snapshot()))
        assert widget.capture_grid.buttons["P01"].property("captureState") == "press_ready"
        for number in range(1000, 1020):
            tick(manager, clock, number, [3.05, 0, 0, 0, 0, 0])
        tick(manager, clock, 1020)  # Unloading cannot erase already reached target progress.
        snapshot = manager.snapshot()
        assert snapshot["guided"]["can_finish"] is True
        assert snapshot["guided"]["force_progress_percent"] == 100
        assert "采集数据检查已通过" in snapshot["guided"]["message"]
        widget.update_state(state(experiment=snapshot))
        assert widget.capture_grid.buttons["P01"].property("captureState") == "confirmed"
        assert widget.trial_finish_button.isEnabled()
        assert "检查已通过" in widget.preload_status.text()
        widget.trial_finish_button.click()
        assert context.calls[-1] == ("experiment/trial", {"action": "finish"})
        widget._action_finished("experiment/trial", {"experiment": manager.trial("finish", acquisition_context(clock.now))})
        recover_to_saving(manager, clock, 1021)
        await_checkpoint(manager)
        assert manager.recording_checkpoint_acknowledged()
        widget.update_state(state(experiment=manager.snapshot()))
        assert manager.snapshot()["guided"]["last_quality"]["accepted"] is True
        assert manager.snapshot()["guided"]["accepted_by_point"]["P01"] == 1
        assert not manager.trial_id
        assert not widget.trial_finish_button.isEnabled()
        assert widget.trial_start_button.isEnabled()
        assert widget.capture_grid.buttons["P01"].property("captureState") == "success"
    finally:
        manager.stop()


def test_capture_point_colors_distinguish_saved_confirmed_and_fresh_press_permission(workspace):
    widget, _context = workspace
    widget.pending_point = "P24"
    exp = experiment("needs_baseline", point_id="P25")
    exp["guided"].update(protocol="force_target_wrench_v1", accepted_by_point={"P21": 1, "P05": 1})
    widget.update_state(state(experiment=exp))
    buttons = widget.capture_grid.buttons
    assert buttons["P21"].property("captureState") == "success"
    assert buttons["P05"].property("captureState") == "success"
    assert buttons["P25"].property("captureState") == "confirmed"
    assert buttons["P24"].isChecked() and buttons["P24"].property("captureState") == ""
    exp.update(trial_id="T1", phase="preload")
    exp["guided"].update(step="preload", preload_remaining_s=0, press_prompt_ready=False)
    widget.update_state(state(experiment=exp))
    assert buttons["P25"].property("captureState") == "confirmed", "Elapsed UI time cannot allow pressing"
    exp["phase"] = exp["guided"]["step"] = "waiting_contact"
    exp["guided"]["press_prompt_ready"] = True
    widget.update_state(state(experiment=exp))
    assert buttons["P25"].property("captureState") == "press_ready"
    assert not buttons["P25"].isEnabled(), "The point remains fixed during the trial while its readiness color stays visible"
    widget._received_at -= 2
    widget._refresh_controls()
    assert buttons["P25"].property("captureState") == "confirmed", "Expired state must not invite pressing"
    widget.update_state(state(experiment=exp))
    assert buttons["P25"].property("captureState") == "press_ready"
    exp["guided"]["quality_faults"] = ["采集源帧间断"]
    widget.update_state(state(experiment=exp))
    assert buttons["P25"].property("captureState") == "confirmed"
    exp["guided"]["quality_faults"] = []
    exp["phase"] = exp["guided"]["step"] = "release"
    widget.update_state(state(experiment=exp))
    assert buttons["P25"].property("captureState") == "confirmed", "An unloading prompt must not invite more pressing"
    exp.update(trial_id=None, phase="idle")
    exp["guided"].update(step="round_complete", accepted_by_point={"P21": 1, "P05": 1, "P25": 1})
    widget.update_state(state(experiment=exp))
    assert buttons["P25"].property("captureState") == "success"
    assert "采集成功" in buttons["P25"].accessibleDescription()


def test_capture_point_turns_green_only_after_both_real_source_baselines_complete(workspace, tmp_path, clock):
    widget, _context = workspace
    manager = start_target(tmp_path, clock)
    try:
        manager.trial("start", acquisition_context(clock.now))
        assert manager.training_pause_acknowledged()
        for number in range(500, 550):
            tick(manager, clock, number)
        widget.update_state(state(experiment=manager.snapshot()))
        assert manager.phase == "preload"
        assert widget.capture_grid.buttons["P01"].property("captureState") == "confirmed"
        widget.guidance.ensurePolished()
        assert "正在记录 2 秒采前基线" in widget.guidance.text()
        assert widget.guidance.palette().color(QPalette.ColorRole.WindowText).name() == "#b83b32"
        tick(manager, clock, 550)
        widget.update_state(state(experiment=manager.snapshot()))
        assert manager.phase == "waiting_contact"
        assert manager.snapshot()["guided"]["preload_remaining_s"] == 0
        assert widget.capture_grid.buttons["P01"].property("captureState") == "press_ready"
        assert "现在可以开始按压" in widget.guidance.text()
        assert widget.guidance.palette().color(QPalette.ColorRole.WindowText).name() == "#127c4a"
        widget._received_at -= 2
        widget._refresh_controls()
        assert widget.guidance.palette().color(QPalette.ColorRole.WindowText).name() != "#127c4a"
        widget.update_state(state(experiment=manager.snapshot()))
        assert widget.guidance.palette().color(QPalette.ColorRole.WindowText).name() == "#127c4a"
        manager.mark("用于核对故障时取消绿色提示")
        widget.update_state(state(experiment=manager.snapshot()))
        assert widget.guidance.palette().color(QPalette.ColorRole.WindowText).name() == "#b83b32"
    finally:
        manager.stop()


def test_native_start_rejects_equal_target_and_upper_limit_without_changing_operator_values(workspace):
    widget, context = workspace
    widget.finger_id.setText("实际样件")
    widget.target_force.setValue(3)
    widget.max_force.setValue(3)
    widget._start_capture()
    assert not context.calls
    assert "目标必须低于合力上限" in widget.message.text()
    assert widget.target_force.value() == widget.max_force.value() == 3


def test_previous_accepted_round_and_local_finish_blocks_are_labeled_explicitly(workspace):
    widget, context = workspace
    exp = experiment("release", trial_id="T2", phase="release")
    exp["guided"].update(last_quality={"accepted": True, "reasons": []}, can_finish=False,
                         finish_block_reason="可信接触光谱不足 16 帧（当前 10 帧）")
    widget.update_state(state(experiment=exp))
    assert "上一轮检查通过" in widget.quality_status.text()
    assert not widget.trial_finish_button.isEnabled()
    assert "不足 16 帧" in widget.preload_status.text()
    exp["guided"].update(can_finish=True, finish_block_reason=None)
    widget._pending.add("experiment/trial")
    widget.update_state(state(experiment=exp))
    assert not widget.trial_finish_button.isEnabled()
    assert "正在处理上一项操作" in widget.preload_status.text()
    widget._pending.clear()
    widget._received_at -= 2
    widget._refresh_controls()
    assert not widget.trial_finish_button.isEnabled()
    assert "服务状态已过期" in widget.preload_status.text()
    widget.update_state(state(experiment=exp))
    assert widget.trial_finish_button.isEnabled()
    widget.trial_finish_button.click()
    assert context.calls[-1] == ("experiment/trial", {"action": "finish"})


def test_training_uses_all_explicit_selected_sources_and_never_skips_missing(workspace):
    widget, context = workspace
    widget.sessions = [session("E1"), session("E2")]
    widget.cumulative.setChecked(False)
    widget.selected_sessions = {"E1", "missing"}
    widget._start_training()
    assert not context.calls
    assert "missing" in widget.message.text()
    widget.selected_sessions = {"E1", "E2"}
    widget._start_training()
    action, payload = context.calls[-1]
    assert action == "training/start"
    assert payload["session_ids"] == ["E1", "E2"]
    assert payload["training_mode"] == "scratch"


def test_finetune_requires_explicit_same_input_base_model(workspace):
    widget, context = workspace
    widget.sessions = [session("Enew")]
    widget.models = [model("Sfull"), model("Sdrift", "drift_gru")]
    widget._render_models()
    widget.training_mode.setCurrentIndex(widget.training_mode.findData("finetune"))
    widget.selected_sessions = {"Enew"}
    widget._start_training()
    assert not context.calls
    assert widget.base_model.findData("Sdrift") == -1
    widget.base_model.setCurrentIndex(widget.base_model.findData("Sfull"))
    widget._start_training()
    action, payload = context.calls[-1]
    assert action == "training/start"
    assert payload["base_model_id"] == "Sfull"
    assert payload["learning_rate"] == .0001
    assert "adapt_legacy_unloaded" not in payload


def test_model_switch_consumes_unloaded_confirmation_and_reference_requires_another(workspace):
    widget, context = workspace
    widget.models = [model()]
    widget._render_models()
    widget.model_select.setCurrentIndex(widget.model_select.findData("S1"))
    widget._load_model()
    assert not context.calls
    widget.test_unloaded.setChecked(True)
    widget._load_model()
    assert context.calls[-1] == ("analysis/load", {"model_id": "S1", "unloaded_confirmed": True})
    assert not widget.test_unloaded.isChecked()
    widget._action_finished("analysis/load", {"analysis": inference(test_reference={"ready": False})})
    context.calls.clear()
    widget._inference_reference()
    assert not context.calls
    widget.test_unloaded.setChecked(True)
    widget._inference_reference()
    assert context.calls[-1] == ("analysis/reference", {"unloaded_confirmed": True})


def test_validation_target_never_goes_to_inference_and_freeze_requires_manual_mode(workspace):
    widget, context = workspace
    widget.update_state(state(analysis=inference()))
    widget._select_target("P01")
    assert not context.calls
    widget._freeze()
    assert not context.calls
    widget.validation_mode.setCurrentIndex(widget.validation_mode.findData("judge"))
    widget._freeze()
    assert context.calls[-1] == ("validation/freeze", {"actual_point_id": "P01", "wrench_tolerances": [None] * 6})


@pytest.mark.parametrize("spectral", [True, False])
def test_unready_live_comparison_hides_retained_truth_and_error_but_keeps_prediction(workspace, spectral):
    widget, _context = workspace
    analysis = inference()
    if spectral:
        analysis["prediction"]["comparison_ready"] = False
    else:
        analysis["model_kind"] = "pilot"
        analysis["prediction"] = {"status": "有效估计", "valid": True, "point_id": "P01",
                                  "force_N": 1.5, "truth_force_N": 1.4, "error_N": .1,
                                  "comparison_ready": False}
    widget.target_point = "P01"
    widget.update_state(state(analysis=analysis))
    for row in range(widget.wrench_table.rowCount()):
        assert widget.wrench_table.item(row, 1).text() != "—"
        assert widget.wrench_table.item(row, 2).text() == "—"
        assert widget.wrench_table.item(row, 3).text() == "—"
    assert not widget.freeze_button.isEnabled()


def test_valid_frozen_comparison_remains_displayable_when_live_comparison_is_unready(workspace):
    widget, _context = workspace
    analysis = inference()
    sample = {"sample_id": "Vfixed", "model_id": "S1", "valid": True, "inference_valid": True,
              "schema": "spectral_manual_sample_v1", "target_point_id": "P01",
              "prediction": deepcopy(analysis["prediction"]), "wrench_tolerances": [.1] * 6}
    analysis["prediction"]["comparison_ready"] = False
    widget.update_state(state(analysis=analysis, validation={"pending_sample": sample}))
    for row in range(3):
        assert widget.wrench_table.item(row, 1).text() != "—"
        assert widget.wrench_table.item(row, 2).text() != "—"
        assert widget.wrench_table.item(row, 3).text() != "—"


def test_pending_frozen_evidence_locks_target_and_blocks_stop(workspace):
    widget, context = workspace
    sample = {"sample_id": "V1", "model_id": "S1", "valid": True, "inference_valid": True,
              "schema": "spectral_manual_sample_v1", "target_point_id": "P01",
              "prediction": inference()["prediction"], "wrench_tolerances": [.1] * 6}
    widget.update_state(state(analysis=inference(), validation={"pending_sample": sample}))
    widget._select_target("P02")
    assert widget.target_point == "P01"
    widget._stop_inference()
    assert not context.calls
    assert widget.save_judgment_button.isEnabled()
    widget.position_correct.setChecked(True)
    widget.force_correct.setChecked(True)
    widget._judge()
    assert context.calls[-1] == ("validation/judge", {"sample_id": "V1", "actual_point_id": "P01", "position_correct": True, "force_correct": True})


def test_force_judgment_cannot_pass_missing_or_exceeded_tolerances(workspace):
    widget, _context = workspace
    sample = {"schema": "spectral_manual_sample_v1", "inference_valid": True,
              "prediction": {"wrench_error": [0.] * 6}, "wrench_tolerances": [.1] * 5 + [None]}
    assert not widget._force_judgment_allowed(sample)
    sample["wrench_tolerances"] = [.1] * 6
    assert widget._force_judgment_allowed(sample)
    sample["prediction"]["wrench_error"][2] = .1001
    assert not widget._force_judgment_allowed(sample)
    assert not widget._force_judgment_allowed({"inference_valid": True, "absolute_error_N": .301})


def test_rejected_but_valid_press_can_be_saved_as_failure(workspace):
    widget, context = workspace
    sample = {"sample_id": "Vbad", "model_id": "S1", "valid": True, "inference_valid": False,
              "target_point_id": "P01", "prediction": {"point_id": None}, "absolute_error_N": None}
    widget.update_state(state(analysis=inference(), validation={"pending_sample": sample}))
    assert not widget.position_correct.isEnabled()
    assert not widget.force_correct.isEnabled()
    widget._judge()
    assert context.calls[-1][1]["position_correct"] is False
    assert context.calls[-1][1]["force_correct"] is False


def test_recycle_needs_selection_confirmation_and_preserves_exact_ids(workspace, monkeypatch):
    widget, context = workspace
    widget.sessions = [session("E1"), session("E2")]
    widget.cumulative.setChecked(False)
    widget.selected_sessions = {"E2"}
    widget._render_sessions()
    monkeypatch.setattr(widget, "_confirm_mutation", lambda *_args: False)
    widget._change_history(False)
    assert not context.calls
    monkeypatch.setattr(widget, "_confirm_mutation", lambda *_args: True)
    widget._change_history(False)
    assert context.calls[-1] == ("history/delete", {"session_ids": ["E2"]})


def test_current_inference_model_is_protected_from_recycle(workspace, monkeypatch):
    widget, context = workspace
    widget.models = [model()]
    widget.update_state(state(analysis=inference()))
    widget.selected_models = {"S1"}
    monkeypatch.setattr(widget, "_confirm_mutation", lambda *_args: True)
    widget._change_models(False)
    assert not context.calls
    assert "推理" in widget._model_block_reason(widget.models[0])


def test_stale_snapshot_clears_live_force_and_recognition_and_confirmations(workspace):
    widget, context = workspace
    exp = experiment(live_force={"valid": True, "age_ms": 10, "force_norm_N": 2.345})
    widget.update_state(state(experiment=exp))
    widget.unloaded_confirmed.setChecked(True)
    assert "2.345" in widget.live_force.text()
    widget._received_at = time.monotonic() - 2
    widget._refresh_controls()
    assert "—" in widget.live_force.text()
    assert not widget.unloaded_confirmed.isChecked()
    assert not widget.baseline_button.isEnabled()
    widget.update_state(state(analysis=inference()))
    assert "#127c4a" in widget.validation_grid.buttons["P01"].styleSheet()
    widget._received_at = time.monotonic() - 2
    widget._refresh_controls()
    assert "#127c4a" not in widget.validation_grid.buttons["P01"].styleSheet()


def test_held_force_expires_at_250_ms_before_global_snapshot_timeout(workspace):
    widget, _context = workspace
    exp = experiment(live_force={"valid": True, "age_ms": 10, "force_norm_N": 2.345})
    widget.update_state(state(experiment=exp, force={"frame_id": 17, "host_rx_monotonic_ns": 123456}))
    assert "2.345" in widget.live_force.text()
    recorded = deepcopy(widget._force_series)
    widget._received_at = time.monotonic() - .4
    widget._refresh_controls()
    assert not widget._stale()
    assert "—" in widget.live_force.text()
    assert widget._capture_force_age() >= 410
    assert widget._force_series == recorded
    widget._refresh_controls()
    assert widget._force_series == recorded


@pytest.mark.parametrize("protocol,force,expected_label,overloaded", [
    ("guided_round_v1", {"force_N": 3.5}, "PX6D 按压力 / N", True),
    ("guided_round_v1", {"force_N": -3.5}, "PX6D 按压力 / N", False),
    ("force_target_wrench_v1", {"force_norm_N": 3.5}, "PX6D 合力 / N", False),
    ("force_target_wrench_v1", {"force_norm_N": 5.5}, "PX6D 合力 / N", True),
])
def test_force_chart_label_and_overload_limit_follow_actual_protocol(workspace, protocol, force, expected_label, overloaded):
    widget, _context = workspace
    exp = experiment(settings={"acquisition_protocol": protocol, "coordinates_confirmed": True},
                     live_force={"valid": True, "age_ms": 10, **force})
    widget.update_state(state(experiment=exp))
    assert widget.force_plot.y_label == expected_label
    assert ("color:#b83b32" in widget.live_force.styleSheet()) is overloaded


def test_preferences_restore_without_initial_writes_or_confirmation_replay(app):
    identifier = "样件 <保留原样> " + "A" * 130
    context = FakeContext({"experiment": {"finger_id": identifier, "selected_point_id": "P07", "repetitions_per_point": 7,
                                           "capture_mode": "peak_drift", "acquisition_protocol": "force_target_wrench_v1", "temperature_C": 23},
                           "analysis": {"method": "drift_gru", "epochs": 42, "learning_rate": .002}})
    widget = ExperimentWorkspace(context)
    assert not context.calls
    assert widget.finger_id.text() == identifier
    assert widget.pending_point == "P07"
    assert widget.repetitions.value() == 7
    assert widget.training_method.currentData() == "drift_gru"
    assert widget.parameter_widgets["epochs"].value() == 42
    assert not widget.unloaded_confirmed.isChecked()
    assert not widget.test_unloaded.isChecked()
    assert all(not hasattr(widget, name) for name in ("temperature", "coordinates_confirmed", "direction_confirmed"))
    assert context.preferences["experiment"]["temperature_C"] == 23
    widget.update_state(state())
    widget.repetitions.setValue(8)
    widget._save_preferences()
    assert context.calls[-1] == ("settings", {"section": "experiment", "values": {"repetitions_per_point": 8}})
    assert all("confirmed" not in key for _action, payload in context.calls for key in payload.get("values", {}))
    widget._fresh_timer.stop()
    widget._preference_timer.stop()
    widget.close()

