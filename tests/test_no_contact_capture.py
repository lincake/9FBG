"""Synthetic source replays exercise explicit no-contact capture, never hardware."""
from copy import deepcopy
import json
import time

import pytest

from ninefbg.experiment import ExperimentManager, list_sessions
from ninefbg.no_contact import NO_CONTACT_POINT_ID, validate_no_contact_trial
from test_experiment import clock, context, force, settings, spectrum, sync
from test_drift_capture_integration import fitted


def no_contact_archive(root, clock, *, capture_mode="full_spectrum", repetitions=3):
    manager, tick = begin_no_contact(root, clock, capture_mode=capture_mode, repetitions=repetitions)
    try:
        for _ in range(30):
            tick()
        finish_no_contact(manager, tick, clock)
        path = manager.writer.path
        manager.stop()
        return path
    finally:
        manager.stop()


def begin_no_contact(root, clock, *, capture_mode="full_spectrum", repetitions=3):
    manager = ExperimentManager(root)
    active = context(clock.now)
    active["frame"] = spectrum(clock.now)
    manager.start(settings(acquisition_protocol="force_target_wrench_v1", capture_mode=capture_mode,
                           repetitions_per_point=repetitions, sample_rate_hz=25,
                           target_force_norm_N=3, max_force_norm_N=5), active)
    manager.select_point(NO_CONTACT_POINT_ID)
    manager.baseline_request(True, active)
    number = 100

    def tick(wrench=None, ms=40, stale=False):
        nonlocal number
        timestamp = clock.advance(ms)
        values = [0., 0., -1., 0., 0., 0.]
        if wrench:
            values = [a+b for a, b in zip(values, wrench)]
        raw = spectrum(timestamp, number)
        source = force(timestamp, number, values)
        source["stale"] = stale
        assert manager.on_force(source)
        assert manager.on_spectrum(raw)
        assert manager.on_result(fitted(raw), sync(number, number, values))
        number += 1

    for _ in range(12):
        tick()
    assert manager.baseline["ready"]
    start_no_contact_round(manager, tick, clock)
    return manager, tick


def start_no_contact_round(manager, tick, clock):
    manager.trial("start", context(clock.now))
    assert manager.phase == "waiting_training"
    assert manager.training_pause_acknowledged()
    for _ in range(51):
        tick()
    assert manager.phase == "no_contact", manager.snapshot()


def finish_no_contact(manager, tick, clock):
    assert manager.snapshot()["guided"]["can_finish"], manager.snapshot()
    clock.advance(1)
    manager.trial("finish", context(clock.now))
    assert manager.phase == "recovery"
    tick()
    assert manager.phase == "saving", manager.snapshot()
    acknowledge_no_contact_save(manager)


def acknowledge_no_contact_save(manager):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        if manager.drift_results_checkpoint_ready() and manager.recording_checkpoint_acknowledged():
            break
        time.sleep(.005)
    assert manager.trial_id is None, manager.snapshot()


@pytest.mark.parametrize("capture_mode", ["full_spectrum", "peak_drift"])
def test_explicit_no_contact_manual_finish_without_press_target_and_durable_labels(tmp_path, clock, capture_mode):
    path = no_contact_archive(tmp_path, clock, capture_mode=capture_mode)
    manifest = json.loads((path/"manifest.json").read_text(encoding="utf-8"))
    trial = manifest["final"]["trials"][0]
    assert trial["point_id"] == NO_CONTACT_POINT_ID
    assert trial["sample_label"] == "no_contact"
    assert trial["quality"]["accepted"]
    assert trial["checkpoint_saved"]
    assert trial["target_reached"] is False
    metrics = validate_no_contact_trial(trial, manifest["settings"]["no_contact_criteria"])
    assert metrics["peak_force_norm_N"] == 0
    if capture_mode == "peak_drift":
        assert metrics["nine_fbg_no_contact_windows"] > 0
        assert not (path/"raw_spectra.jsonl").exists()
    else:
        rows = [json.loads(line) for line in (path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()]
        captured = [row for row in rows if row.get("phase") == "no_contact"]
        assert len(captured) >= 30 and all(len(row["intensity"]) == 512 for row in captured)
    summary = next(row for row in list_sessions(tmp_path) if row["session_id"] == path.name)
    assert summary["no_contact_trial_count"] == summary["accepted_no_contact_trial_count"] == 1
    assert summary["point_count"] == summary["completed_point_count"] == 0
    assert summary["has_training_data"]
    assert summary["spectral_available"] is (capture_mode == "full_spectrum")
    assert not summary["acquisition_complete"]
    if capture_mode == "peak_drift":
        assert summary["drift_available"]


def test_no_contact_minimum_window_then_operator_chooses_duration_and_reference_stays_fixed(tmp_path, clock):
    manager, tick = begin_no_contact(tmp_path, clock)
    try:
        original = deepcopy(manager.baseline)
        assert not manager.snapshot()["guided"]["can_finish"]
        with pytest.raises(ValueError, match="继续记录无接触"):
            manager.trial("finish", context(clock.now))
        for _ in range(100):
            tick()
            assert manager.phase == "no_contact"
        assert manager.baseline == original
        assert manager.snapshot()["guided"]["target_force_norm_N"] is None
        assert manager.snapshot()["guided"]["total_accepted"] == 0
        with pytest.raises(ValueError, match="不能切换"):
            manager.select_point("P01")
        finish_no_contact(manager, tick, clock)
        assert manager.snapshot()["guided"]["no_contact_accepted"] == 1
        assert manager.snapshot()["guided"]["total_accepted"] == 0
        assert manager.snapshot()["guided"]["progress_percent"] == 0
        assert manager.baseline["ready"]
        manager.select_point("P01")
        manager.trial("start", context(clock.now))
        assert not manager._timed["trial"].get("no_contact")
        manager.trial("abort", context(clock.now))
    finally:
        manager.stop()


@pytest.mark.parametrize("capture_mode", ["full_spectrum", "peak_drift"])
def test_no_contact_count_is_independent_and_can_repeat_or_switch_after_each_saved_round(tmp_path, clock, capture_mode):
    manager, tick = begin_no_contact(tmp_path, clock, capture_mode=capture_mode, repetitions=1)
    path = manager.writer.path
    try:
        for completed in range(1, 4):
            active = manager.snapshot()["guided"]
            assert active["no_contact_attempts"] == completed
            assert active["no_contact_accepted"] == completed-1
            assert active["current_round"] == completed
            assert active["point_progress_percent"] is None
            for _ in range(30):
                tick()
            finish_no_contact(manager, tick, clock)
            saved = manager.snapshot()["guided"]
            assert saved["step"] == "round_complete"
            assert saved["no_contact_accepted"] == saved["no_contact_attempts"] == completed
            assert saved["current_round"] == completed+1
            assert saved["total_accepted"] == saved["progress_percent"] == 0
            assert not saved["complete"]
            assert manager.baseline["ready"]
            manager.select_point("P01")
            spatial = manager.snapshot()["guided"]
            assert spatial["point_accepted"] == spatial["point_attempts"] == 0
            assert spatial["no_contact_accepted"] == completed
            assert spatial["no_contact_attempts"] == completed
            assert spatial["current_round"] == 1
            assert spatial["point_progress_percent"] == 0
            manager.select_point(NO_CONTACT_POINT_ID)
            if completed < 3:
                start_no_contact_round(manager, tick, clock)
    finally:
        manager.stop()
    manifest = json.loads((path/"manifest.json").read_text(encoding="utf-8"))
    trials = manifest["final"]["trials"]
    assert len(trials) == 3
    assert all(trial["point_id"] == NO_CONTACT_POINT_ID and trial["checkpoint_saved"] for trial in trials)
    assert all(validate_no_contact_trial(trial, manifest["settings"]["no_contact_criteria"]) for trial in trials)
    summary = next(row for row in list_sessions(tmp_path) if row["session_id"] == path.name)
    assert summary["no_contact_trial_count"] == summary["accepted_no_contact_trial_count"] == 3
    assert summary["point_count"] == summary["completed_point_count"] == 0
    assert not summary["acquisition_complete"]


@pytest.mark.parametrize("capture_mode", ["full_spectrum", "peak_drift"])
def test_aborted_no_contact_attempt_keeps_a_distinct_next_round_number(tmp_path, clock, capture_mode):
    manager, tick = begin_no_contact(tmp_path, clock, capture_mode=capture_mode, repetitions=1)
    try:
        manager.trial("abort", context(clock.now))
        aborted = manager.snapshot()["guided"]
        assert aborted["no_contact_accepted"] == 0
        assert aborted["no_contact_attempts"] == 1
        assert aborted["current_round"] == 2
        manager.baseline_request(True, context(clock.now))
        for _ in range(12):
            tick()
        assert manager.baseline["ready"]
        start_no_contact_round(manager, tick, clock)
        active = manager.snapshot()["guided"]
        assert active["no_contact_attempts"] == active["current_round"] == 2
        assert active["no_contact_accepted"] == 0
        for _ in range(30):
            tick()
        finish_no_contact(manager, tick, clock)
        saved = manager.snapshot()["guided"]
        assert saved["no_contact_accepted"] == 1
        assert saved["no_contact_attempts"] == 2
        assert saved["current_round"] == 3
    finally:
        manager.stop()


@pytest.mark.parametrize("capture_mode", ["full_spectrum", "peak_drift"])
def test_completed_spatial_quota_does_not_stop_independent_no_contact_capture(tmp_path, clock, capture_mode):
    manager, tick = begin_no_contact(tmp_path, clock, capture_mode=capture_mode, repetitions=1)
    try:
        # Gate replay only: represent a completed 25-point workflow without
        # claiming the synthetic source data qualify physical presses.
        for point in manager.settings["point_grid"]:
            manager._guided["accepted_by_point"][point["id"]] = 1
        for completed in range(1, 3):
            for _ in range(30):
                tick()
            finish_no_contact(manager, tick, clock)
            saved = manager.snapshot()["guided"]
            assert saved["complete"]
            assert saved["total_accepted"] == saved["total_required"] == 25
            assert saved["progress_percent"] == 100
            assert saved["no_contact_accepted"] == completed
            assert saved["step"] == "round_complete"
            manager.select_point("P01")
            with pytest.raises(ValueError, match="已完成合格重复次数"):
                manager.trial("start", context(clock.now))
            manager.select_point(NO_CONTACT_POINT_ID)
            if completed < 2:
                start_no_contact_round(manager, tick, clock)
        manager.mark("接触棒位置发生变化，请重新确认无载")
        marked = manager.snapshot()["guided"]
        assert marked["step"] == "needs_baseline"
        assert marked["no_contact_accepted"] == marked["no_contact_attempts"] == 2
        assert marked["total_accepted"] == 25
        assert marked["current_round"] == 3
        assert not manager.baseline["ready"]
        manager.baseline_request(True, context(clock.now))
        for _ in range(12):
            tick()
        assert manager.baseline["ready"]
        start_no_contact_round(manager, tick, clock)
        assert manager.snapshot()["guided"]["current_round"] == 3
        manager.trial("abort", context(clock.now))
    finally:
        manager.stop()


@pytest.mark.parametrize("capture_mode", ["full_spectrum", "peak_drift"])
def test_no_contact_selection_and_count_wait_for_durable_save(tmp_path, clock, monkeypatch, capture_mode):
    manager, tick = begin_no_contact(tmp_path, clock, capture_mode=capture_mode, repetitions=1)
    try:
        for point_id in ("P01", NO_CONTACT_POINT_ID):
            with pytest.raises(ValueError, match="不能切换"):
                manager.select_point(point_id)
        for _ in range(30):
            tick()
        clock.advance(1)
        manager.trial("finish", context(clock.now))
        tick()
        assert manager.phase == "saving"
        with monkeypatch.context() as pending:
            pending.setattr(manager.writer, "checkpoint_ready", lambda token: False)
            assert not manager.recording_checkpoint_acknowledged()
            snapshot = manager.snapshot()["guided"]
            assert snapshot["checkpoint_pending"]
            assert snapshot["no_contact_accepted"] == 0
            assert snapshot["no_contact_attempts"] == 1
            for point_id in ("P01", NO_CONTACT_POINT_ID):
                with pytest.raises(ValueError, match="不能切换"):
                    manager.select_point(point_id)
        acknowledge_no_contact_save(manager)
        assert manager.snapshot()["guided"]["no_contact_accepted"] == 1
        manager.select_point("P01")
        manager.select_point(NO_CONTACT_POINT_ID)
        assert manager.baseline["ready"]
    finally:
        manager.stop()


@pytest.mark.parametrize("wrench,ms,stale", [([.2, 0, 0, 0, 0, 0], 40, False),
                                           ([0, 0, 0, .01, 0, 0], 40, False),
                                           (None, 121, False), (None, 40, True)])
def test_press_torque_gap_or_untrusted_source_cannot_be_saved_as_no_contact(tmp_path, clock, wrench, ms, stale):
    manager, tick = begin_no_contact(tmp_path, clock)
    try:
        tick(wrench, ms=ms, stale=stale)
        for _ in range(25):
            tick()
        assert not manager.snapshot()["guided"]["can_finish"]
        with pytest.raises(ValueError, match="质量故障"):
            manager.trial("finish", context(clock.now))
        manager.trial("abort", context(clock.now))
        assert not manager.snapshot()["guided"]["last_quality"]["accepted"]
        assert manager.snapshot()["guided"]["no_contact_accepted"] == 0
        assert manager.snapshot()["guided"]["no_contact_attempts"] == 1
    finally:
        manager.stop()


def test_legacy_guided_cannot_select_non_spatial_label(tmp_path, clock):
    manager = ExperimentManager(tmp_path)
    try:
        manager.start(settings(guided_acquisition=True), context(clock.now))
        with pytest.raises(ValueError, match="手动结束"):
            manager.select_point(NO_CONTACT_POINT_ID)
    finally:
        manager.stop()


def test_contact_after_finish_assertion_cannot_be_hidden_in_next_software_zero(tmp_path, clock):
    manager, tick = begin_no_contact(tmp_path, clock)
    try:
        for _ in range(30):
            tick()
        original = deepcopy(manager.baseline)
        trial_id = manager.trial_id
        clock.advance(1)
        manager.trial("finish", context(clock.now))
        tick([.2, 0, 0, 0, 0, 0])
        assert manager.trial_id is None
        assert manager._trials[trial_id]["status"] == "aborted"
        assert not manager._trials[trial_id]["quality"]["accepted"]
        assert manager._baselines[original["baseline_id"]] == original
        assert not manager.baseline["ready"]
        assert manager.snapshot()["guided"]["no_contact_accepted"] == 0
    finally:
        manager.stop()
