"""Continuous raw acquisition replay; no synthetic test qualifies hardware."""
from copy import deepcopy
import json
import time

import pytest

from ninefbg.experiment import ExperimentManager, list_sessions
from ninefbg.force_target_wrench import PROTOCOL, QUALITY_VERSION, RANGE_POLICY
from ninefbg.timed_wrench import SYNC_RULE
from test_experiment import clock, context, force, settings, spectrum
from test_timed_wrench import tick


def start_target(tmp_path, clock, **options):
    manager = ExperimentManager(tmp_path)
    active = context(clock.now)
    active["tracker"].update(peaks=[], display_stale=True)
    manager.start(settings(acquisition_protocol=PROTOCOL, force_direction_confirmed=False, **options), active)
    manager.baseline_request(True, active)
    for number in range(12):
        timestamp = clock.advance(40)
        assert manager.on_force(force(timestamp, 100+number))
        assert manager.on_spectrum(spectrum(timestamp, 100+number))
    assert manager.baseline["ready"]
    manager.select_point("P01")
    return manager


def begin(manager, clock):
    manager.trial("start", context(clock.now))
    assert manager.phase == "waiting_training"
    assert manager.training_pause_acknowledged()
    assert manager.phase == "preload"
    for number in range(500, 551):
        tick(manager, clock, number)
    assert manager.phase == "waiting_contact"


def recover_to_saving(manager, clock, number):
    for _ in range(35):
        tick(manager, clock, number)
        number += 1
        if manager.phase == "saving":
            break
    assert manager.phase == "saving", manager.snapshot()
    return number


def await_checkpoint(manager):
    token = manager._timed["trial"]["checkpoint_token"]
    deadline = time.monotonic()+5
    while not manager.writer.checkpoint_ready(token) and time.monotonic() < deadline:
        assert not manager.writer.error
        time.sleep(.005)
    assert manager.writer.checkpoint_ready(token), manager.writer.status()
    return token


def test_start_records_before_pause_ack_and_long_waiting_contact_never_times_out(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        baseline = deepcopy(manager.baseline)
        manager.trial("start", context(clock.now))
        baseline["used"] = True
        trial_id = manager.trial_id
        for number in range(1000, 1005):
            tick(manager, clock, number)
            assert manager.snapshot()["phase"] == "waiting_training"
        with pytest.raises(ValueError, match="等待采集准备"):
            manager.trial("finish", context(clock.now))
        assert not manager.snapshot()["guided"]["can_finish"]
        assert manager.phase == "waiting_training" and manager.trial_id == trial_id
        assert manager.training_pause_acknowledged()
        for number in range(1005, 1405):
            tick(manager, clock, number)
            assert not manager.watchdog(clock.now)
        snapshot = manager.snapshot()
        assert snapshot["phase"] == "waiting_contact"
        assert snapshot["guided"]["elapsed_s"] > 15
        assert snapshot["guided"]["duration_s"] is None
        assert not snapshot["guided"]["target_reached"]
        assert not snapshot["guided"]["can_finish"]
        assert "未达到" in snapshot["guided"]["finish_block_reason"]
        assert manager.baseline == baseline
        with pytest.raises(ValueError, match="不能切换"):
            manager.select_point("P02")
        manager.trial("abort", context(clock.now))
        assert manager._trials[trial_id]["status"] == "aborted"
    finally:
        manager.stop()
    rows = [json.loads(line) for line in (manager.writer.path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(row.get("phase") == "waiting_training" for row in rows) == 5
    assert sum(row.get("phase") == "preload" for row in rows) == 51
    assert sum(row.get("phase") == "waiting_contact" for row in rows) == 349


def test_two_seconds_requires_both_recorded_source_spans_and_get_never_advances(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        manager.trial("start", context(clock.now))
        assert manager.training_pause_acknowledged()
        clock.advance(2100)  # No source data: elapsed wall time is not baseline evidence.
        for _ in range(5):
            guided = manager.snapshot()["guided"]
            assert manager.phase == "preload"
            assert guided["preload_remaining_s"] == 2
            assert not guided["press_prompt_ready"]
        # Optical data alone also cannot announce pressing readiness.
        for number in range(500, 552):
            assert manager.on_spectrum(spectrum(clock.advance(40), number))
        assert manager.phase == "preload"
        assert manager.snapshot()["guided"]["preload_remaining_s"] == 2
        assert manager.on_force(force(clock.now, 600))
        for number in range(601, 650):
            tick(manager, clock, number)
            assert manager.phase == "preload"
        tick(manager, clock, 650)
        assert manager.phase == "waiting_contact"
        assert manager.snapshot()["guided"]["preload_remaining_s"] == 0
        assert manager.snapshot()["guided"]["press_prompt_ready"]
        state = manager._timed["trial"]
        assert state["last_force_ns"]-state["preload_force_first_ns"] >= 2_000_000_000
        assert state["last_spectrum_ns"]-state["preload_spectrum_first_ns"] >= 2_000_000_000
        assert state["main_spectra"] == []
        manager.trial("abort", context(clock.now))
    finally:
        manager.stop()
    rows = [json.loads(line) for line in (manager.writer.path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()]
    trial_rows = [row for row in rows if row.get("trial_id")]
    assert trial_rows and all(row["phase"] == "preload" for row in trial_rows)
    assert all(row["intensity"] == spectrum(1)["intensity"] for row in trial_rows)


def test_early_preload_contact_is_preserved_and_never_claimed_as_unloaded_baseline(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        manager.trial("start", context(clock.now))
        manager.training_pause_acknowledged()
        tick(manager, clock, 500, [.4, 0, 0, 0, 0, 0])
        for number in range(501, 560):
            tick(manager, clock, number)
        assert manager.phase == "preload"
        assert not manager.snapshot()["guided"]["press_prompt_ready"]
        finish_block = manager.snapshot()["guided"]["finish_block_reason"]
        assert "质量故障" in finish_block and "采前基线期间已接触" in finish_block and "请中止本轮" in finish_block
        assert manager._timed["trial"]["preload_contact_detected"]
        assert any("采前基线期间已接触" in reason for reason in manager._timed["trial"]["faults"])
        manager.trial("abort", context(clock.now))
        quality = manager.snapshot()["guided"]["last_quality"]
        assert not quality["accepted"]
        assert quality["metrics"]["preload_contact_detected"]
        assert quality["metrics"]["press_prompt_monotonic_ns"] is None
    finally:
        manager.stop()
    rows = [json.loads(line) for line in (manager.writer.path/"raw_force.jsonl").read_text(encoding="utf-8").splitlines()]
    loaded = next(row for row in rows if row["frame_id"] == 500)
    assert loaded["phase"] == "preload" and loaded["force_values"] == [.4, 0, -1, 0, 0, 0]


def test_direct_zero_uses_first_post_finish_source_frames_and_never_pre_finish_tail(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        begin(manager, clock)
        for number in range(1000, 1020):
            tick(manager, clock, number, [4, 0, 0, 0, 0, 0], intensity_delta=200)
        original = deepcopy(manager.baseline)
        manager.trial("finish", context(clock.now))
        boundary = manager._timed["trial"]["finish_requested_ns"]
        assert manager.phase == "recovery"
        assert manager.baseline == original
        first_force = force(clock.advance(20), 2000, [.7, -.2, -1.3, .02, -.01, .03])
        assert manager.on_force(first_force)
        assert manager.phase == "recovery"  # A real spectrum is still required.
        assert manager.on_force(force(clock.advance(10), 2001, [1, 1, -2, .1, .1, .1]))
        first_spectrum = spectrum(clock.advance(10), 2002)
        first_spectrum["intensity"] = [value+17 for value in first_spectrum["intensity"]]
        assert manager.on_spectrum(first_spectrum)
        assert manager.phase == "saving"
        candidate = manager._timed["trial"]["next_baseline"]
        assert candidate["force_zero"] == first_force["force_values"]
        assert candidate["S0"] == first_spectrum["intensity"]
        assert candidate["force_frame_ids"] == [2000]
        assert candidate["source_frame_ids"] == [2002]
        evidence = candidate["unloaded_evidence"]
        assert evidence["previous_baseline_id"] == original["baseline_id"]
        assert evidence["finish_requested_monotonic_ns"] == boundary
        assert all(value > boundary for value in evidence["force_source_stamps_ns"]+evidence["spectrum_source_stamps_ns"])
        await_checkpoint(manager)
        assert manager.recording_checkpoint_acknowledged()
        assert manager.baseline["force_zero"] == first_force["force_values"]
        assert manager.baseline["S0"] == first_spectrum["intensity"]
        assert manager._baselines[original["baseline_id"]]["S0"] == original["S0"]
        assert manager.snapshot()["guided"]["last_quality"]["accepted"]
    finally:
        manager.stop()


@pytest.mark.parametrize("invalid_kind", ["duplicate", "axis", "gap"])
def test_saving_source_fault_cannot_be_hidden_by_direct_zero(tmp_path, clock, invalid_kind):
    manager = start_target(tmp_path, clock)
    try:
        begin(manager, clock)
        for number in range(1000, 1020):
            tick(manager, clock, number, [4, 0, 0, 0, 0, 0])
        manager.trial("finish", context(clock.now))
        number = recover_to_saving(manager, clock, 1020)
        first_token = await_checkpoint(manager)
        old = deepcopy(manager.baseline)
        if invalid_kind == "duplicate":
            assert manager.on_spectrum(spectrum(clock.now, 1020))
        elif invalid_kind == "axis":
            bad = spectrum(clock.advance(40), number)
            bad["wavelength_nm"][1] += .001
            assert manager.on_spectrum(bad)
            number += 1
        else:
            tick(manager, clock, number, ms=121)
            number += 1
        assert manager.phase == "recovery"
        assert not manager.recording_checkpoint_acknowledged()
        assert manager.baseline == old
        recover_to_saving(manager, clock, number)
        assert await_checkpoint(manager) != first_token
        assert manager.recording_checkpoint_acknowledged()
        quality = manager.snapshot()["guided"]["last_quality"]
        assert not quality["accepted"]
        assert any("无效或不连续" in reason for reason in quality["reasons"])
        assert manager.snapshot()["guided"]["accepted_by_point"]["P01"] == 0
    finally:
        manager.stop()


def test_target_is_latched_prompt_until_manual_finish_then_real_checkpoint_commits_reference(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        old = deepcopy(manager.baseline)
        begin(manager, clock)
        old["used"] = True
        for number in range(1000, 1014):
            tick(manager, clock, number, [1, -1, -.5, .01, -.02, .03])
        assert manager.phase == "loading"
        tick(manager, clock, 1014, [4, 0, 0, .01, -.02, .03])
        assert manager.phase == "release"
        assert not manager.snapshot()["guided"]["can_finish"]
        assert "不足 16 帧" in manager.snapshot()["guided"]["finish_block_reason"]
        timeline = deepcopy(manager._timeline)
        with pytest.raises(ValueError, match="不足 16 帧"):
            manager.trial("finish", context(clock.now))
        assert manager.phase == "release" and manager._timeline == timeline
        assert not manager._timed["trial"]["finish_requested"]
        tick(manager, clock, 1015, [.3, -.2, 0, .001, -.001, .002])
        assert not manager.snapshot()["guided"]["can_finish"]
        tick(manager, clock, 1016, [.3, -.2, 0, .001, -.001, .002])
        assert manager.snapshot()["guided"]["can_finish"]
        assert manager.snapshot()["guided"]["finish_block_reason"] is None
        for number in range(1017, 1117):
            tick(manager, clock, number, [.3, -.2, 0, .001, -.001, .002])
        assert manager.phase == "release" and manager.trial_id
        assert manager.snapshot()["guided"]["target_reached"]
        assert manager.snapshot()["guided"]["elapsed_s"] > 4
        assert manager.baseline == old
        # Reaching the configured target is latched. Unloading does not revoke
        # permission once valid contact evidence is sufficient.
        for number in range(1117, 1120):
            tick(manager, clock, number)
        assert manager.snapshot()["guided"]["force_norm_N"] == 0
        assert manager.snapshot()["guided"]["can_finish"]
        manager.trial("finish", context(clock.now))
        assert manager.phase == "recovery"
        assert not manager.snapshot()["guided"]["can_finish"]
        with pytest.raises(ValueError, match="当前阶段不能结束"):
            manager.trial("end", context(clock.now))
        assert manager.snapshot()["guided"]["step"] == "zeroing"
        assert manager.snapshot()["guided"]["manual_finish_zeroing"]
        # Finish is the operator's unloaded assertion. A new actual source pair
        # establishes the next reference immediately even when neither source
        # has returned near the old baseline, without a 0.8 s recovery window.
        tick(manager, clock, 1120, [.5, .08, 0, .01, 0, 0], intensity_delta=10)
        assert manager.phase == "saving" and manager.baseline == old
        number = 1121
        trial_id = manager.trial_id
        cutoff = manager._trials[trial_id]["data_end_monotonic_ns"]
        frozen = deepcopy(manager._trials[trial_id]["quality"])
        token = await_checkpoint(manager)
        for _ in range(5):
            assert manager.snapshot()["phase"] == "saving"
            assert manager.trial_id == trial_id
            assert manager.baseline == old
            assert not manager.snapshot()["guided"]["can_finish"]
        with pytest.raises(ValueError, match="当前阶段不能结束"):
            manager.trial("finish", context(clock.now))
        assert manager.phase == "saving"
        with pytest.raises(ValueError, match="不能切换"):
            manager.select_point("P02")
        # Saving source frames still go to disk, but cannot change the frozen
        # quality or become training data under this trial's data boundary.
        tick(manager, clock, number)
        assert manager._trials[trial_id]["quality"] == frozen
        assert manager.recording_checkpoint_acknowledged()
        assert not manager.recording_checkpoint_acknowledged()
        assert manager.trial_id is None
        assert not manager.snapshot()["guided"]["can_finish"]
        assert manager.baseline["ready"] and manager.baseline["baseline_id"] != old["baseline_id"]
        assert manager.baseline["checkpoint_token"] == token
        assert manager.baseline["force_zero"] == pytest.approx([.5, .08, -1, .01, 0, 0])
        assert manager.baseline["S0"] == [v+10 for v in old["S0"]]
        assert manager.baseline["force_frame_ids"] == [1120]
        assert manager.baseline["source_frame_ids"] == [1120]
        assert manager.baseline["effective_monotonic_ns"] > cutoff
        assert manager.baseline["spectral_reference"]["effective_ns"] == manager.baseline["effective_monotonic_ns"]
        assert manager._baselines[old["baseline_id"]]["spectral_reference"] == old["spectral_reference"]
        quality = manager.snapshot()["guided"]["last_quality"]
        assert quality["accepted"], quality
        assert quality["criteria_version"] == QUALITY_VERSION
        assert quality["metrics"]["peak_force_norm_N"] == pytest.approx(4)
        assert quality["metrics"]["peak_valid_frames"] == 0
        assert quality["metrics"]["sync_rule"] == SYNC_RULE
        assert quality["metrics"]["unloaded_evidence"]["trigger"] == "manual_unloaded_confirmation"
        assert quality["metrics"]["unloaded_evidence"]["automatic_unloaded_recovery_verified"] is False
        assert quality["metrics"]["unloaded_evidence"]["operator_confirmed"] is True
        assert quality["metrics"]["preload_recorded_duration_s"] == 2
        assert "main_duration_s" not in quality["metrics"]
    finally:
        manager.stop()
    saved = manager.get_session(manager.session_id)
    assert saved["complete"]
    assert saved["final"]["trials"][0]["checkpoint_saved"]
    assert saved["final"]["trials"][0]["quality"]["accepted"]
    assert list_sessions(tmp_path)[0]["accepted_trial_count"] == 1


def test_early_finish_is_rejected_recording_continues_and_abort_preserves_failed_round(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        old = deepcopy(manager.baseline)
        begin(manager, clock)
        old["used"] = True
        number = 1000
        for _ in range(20):
            tick(manager, clock, number, [.5, 0, 0, 0, 0, 0])
            number += 1
        timeline = deepcopy(manager._timeline)
        for action in ("end", "finish"):
            with pytest.raises(ValueError, match="未达到"):
                manager.trial(action, context(clock.now))
            assert manager.phase == "loading"
            assert not manager._timed["trial"]["finish_requested"]
            assert manager._timeline == timeline
            assert manager.baseline == old
        for _ in range(5):
            tick(manager, clock, number, [.5, 0, 0, 0, 0, 0])
            number += 1
        assert manager.running and manager.trial_id
        assert manager._timed["trial"]["raw_total"] == 76
        manager.trial("abort", context(clock.now))
        quality = manager.snapshot()["guided"]["last_quality"]
        assert not quality["accepted"]
        assert any("未达到" in reason for reason in quality["reasons"])
        assert not quality["metrics"]["target_reached"]
        assert quality["metrics"]["unloaded_evidence"] is None
        assert not manager.baseline["ready"]
        assert manager._baselines[old["baseline_id"]]["force_zero"] == old["force_zero"]
        assert manager._baselines[old["baseline_id"]]["spectral_reference"] == old["spectral_reference"]
        assert manager.snapshot()["guided"]["accepted_by_point"]["P01"] == 0
    finally:
        manager.stop()
    assert list_sessions(tmp_path)[0]["accepted_trial_count"] == 0
    assert manager.get_session(manager.session_id)["final"]["trials"][0]["status"] == "aborted"
    rows = [json.loads(line) for line in (manager.writer.path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len([row for row in rows if row.get("trial_id")]) == 76


def test_overlimit_and_aborted_slip_preserve_raw_without_advancing_reference(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        old = deepcopy(manager.baseline)
        begin(manager, clock)
        for number in range(1000, 1020):
            tick(manager, clock, number, [4, 4, 0, 100, -100, 50])
        assert manager.phase == "release"
        assert not manager.snapshot()["guided"]["can_finish"]
        with pytest.raises(ValueError, match="质量故障"):
            manager.trial("finish", context(clock.now))
        assert manager.phase == "release" and not manager._timed["trial"]["finish_requested"]
        manager.mark("接触棒滑移")
        assert manager.phase == "release"
        manager.trial("abort", context(clock.now))
        quality = manager.snapshot()["guided"]["last_quality"]
        assert not quality["accepted"]
        assert quality["metrics"]["peak_force_norm_N"] == pytest.approx(32**.5)
        assert any("合力超过" in reason for reason in quality["reasons"])
        assert any("人工异常" in reason for reason in quality["reasons"])
        assert not manager.baseline["ready"]
        assert len(manager._baselines) == 1
        assert manager._baselines[old["baseline_id"]]["force_zero"] == old["force_zero"]
    finally:
        manager.stop()
    rows = [json.loads(line) for line in (manager.writer.path/"raw_force.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["force_values"] == [4, 4, -1, 100, -100, 50] for row in rows)


@pytest.mark.parametrize("reload_kind", ["force", "spectrum"])
def test_completed_checkpoint_does_not_reapply_old_zero_or_spectrum_recovery_gates(tmp_path, clock, reload_kind):
    manager = start_target(tmp_path, clock)
    try:
        begin(manager, clock)
        old = deepcopy(manager.baseline)
        for number in range(1000, 1020):
            tick(manager, clock, number, [4, 0, 0, 0, 0, 0])
        manager.trial("finish", context(clock.now))
        number = recover_to_saving(manager, clock, 1020)
        first_token = await_checkpoint(manager)
        candidate = deepcopy(manager._timed["trial"]["next_baseline"])
        if reload_kind == "force":
            tick(manager, clock, number, [2, 0, 0, 0, 0, 0])
            number += 1
        elif reload_kind == "spectrum":
            tick(manager, clock, number, intensity_delta=10)
            number += 1
        assert manager.recording_checkpoint_acknowledged()
        assert manager.baseline["checkpoint_token"] == first_token
        assert manager.baseline["force_zero"] == candidate["force_zero"]
        assert manager.baseline["S0"] == candidate["S0"]
        assert manager.baseline["ready"] and manager.trial_id is None
        assert manager.snapshot()["guided"]["last_quality"]["accepted"]
        assert manager._baselines[old["baseline_id"]]["S0"] == old["S0"]
    finally:
        manager.stop()


def test_stale_checkpoint_cannot_publish_reference_or_hide_quality_fault(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        begin(manager, clock)
        for number in range(1000, 1020):
            tick(manager, clock, number, [4, 0, 0, 0, 0, 0])
        manager.trial("finish", context(clock.now))
        number = recover_to_saving(manager, clock, 1020)
        first_token = await_checkpoint(manager)
        old = deepcopy(manager.baseline)
        clock.advance(121)
        assert not manager.recording_checkpoint_acknowledged()
        assert manager.phase == "recovery" and manager.trial_id
        assert manager.baseline == old
        assert manager._timed["trial"]["next_baseline"] is None
        recover_to_saving(manager, clock, number)
        assert await_checkpoint(manager) != first_token
        assert manager.recording_checkpoint_acknowledged()
        quality = manager.snapshot()["guided"]["last_quality"]
        assert not quality["accepted"]
        assert any("不再新鲜" in reason for reason in quality["reasons"])
        assert manager.snapshot()["guided"]["accepted_by_point"]["P01"] == 0
    finally:
        manager.stop()


def test_manual_mark_during_saving_refreezes_rejected_quality_without_accepted_count(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        begin(manager, clock)
        old = deepcopy(manager.baseline)
        for number in range(1000, 1020):
            tick(manager, clock, number, [4, 0, 0, 0, 0, 0])
        manager.trial("finish", context(clock.now))
        number = recover_to_saving(manager, clock, 1020)
        previous_token = await_checkpoint(manager)
        manager.mark("本轮发生滑移")
        assert manager.phase == "recovery" and manager.baseline == old
        assert not manager.recording_checkpoint_acknowledged()
        recover_to_saving(manager, clock, number)
        assert await_checkpoint(manager) != previous_token
        assert manager.recording_checkpoint_acknowledged()
        quality = manager.snapshot()["guided"]["last_quality"]
        assert not quality["accepted"]
        assert any("人工异常" in reason for reason in quality["reasons"])
        assert manager.snapshot()["guided"]["accepted_by_point"]["P01"] == 0
        assert "下一轮参考已准备" in manager.snapshot()["guided"]["message"]
    finally:
        manager.stop()


def test_overlimit_while_saving_survives_recovery_and_new_durable_checkpoint(tmp_path, clock):
    manager = start_target(tmp_path, clock)
    try:
        begin(manager, clock)
        for number in range(1000, 1020):
            tick(manager, clock, number, [4.1, 0, 0, 0, 0, 0])
        manager.trial("finish", context(clock.now))
        number = recover_to_saving(manager, clock, 1020)
        first_token = await_checkpoint(manager)
        old = deepcopy(manager.baseline)
        tick(manager, clock, number, [6, 0, 0, 0, 0, 0])
        number += 1
        assert manager.phase == "recovery" and manager.baseline == old
        assert not manager.recording_checkpoint_acknowledged()
        assert manager.snapshot()["guided"]["observed_peak_force_norm_N"] == pytest.approx(6)
        recover_to_saving(manager, clock, number)
        assert await_checkpoint(manager) != first_token
        assert manager.recording_checkpoint_acknowledged()
        quality = manager.snapshot()["guided"]["last_quality"]
        assert not quality["accepted"]
        assert quality["metrics"]["peak_force_norm_N"] == pytest.approx(6)
        assert any("合力超过" in reason for reason in quality["reasons"])
        assert manager.snapshot()["guided"]["accepted_by_point"]["P01"] == 0
    finally:
        manager.stop()
    rows = [json.loads(line) for line in (manager.writer.path/"raw_force.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row.get("phase") == "saving" and row["force_values"] == [6, 0, -1, 0, 0, 0] for row in rows)


@pytest.mark.parametrize("options", [{"target_force_norm_N": 6}, {"target_force_norm_N": float("nan")},
                                    {"target_force_norm_N": 0}, {"max_force_norm_N": 0},
                                    {"target_force_norm_N": 3, "max_force_norm_N": 3},
                                    {"force_target_range_policy": "unknown"},
                                    {"main_duration_s": 3}, {"pre_duration_s": 1},
                                    {"force_target_wrench_criteria": {"minimum_contact_spectra": 60}}])
def test_target_protocol_rejects_invalid_or_silently_redefined_rules(tmp_path, clock, options):
    manager = ExperimentManager(tmp_path)
    with pytest.raises(ValueError):
        manager.start(settings(acquisition_protocol=PROTOCOL, **options), context(clock.now))
    assert not manager.running


@pytest.mark.parametrize("stale_source", ["光谱", "六轴"])
def test_target_configuration_keeps_independent_upper_bound_and_native_signed_six_axes(tmp_path, clock, stale_source):
    manager = start_target(tmp_path, clock, target_force_norm_N=4.5, max_force_norm_N=5)
    try:
        assert manager.settings["target_force_norm_N"] == 4.5
        assert manager.settings["max_force_norm_N"] == 5
        assert manager.settings["force_target_range_policy"] == RANGE_POLICY
        assert manager.settings["force_sign"] == -1
        assert not manager.settings["six_axis_definition"]["force_sign_applied"]
        begin(manager, clock)
        for number in range(1000, 1020):
            tick(manager, clock, number, [0, 0, -5, -.001, .002, -.003])
        assert manager.phase == "release"
        assert manager.snapshot()["guided"]["current_wrench"] == pytest.approx([0, 0, -5, -.001, .002, -.003])
        assert manager.snapshot()["guided"]["can_finish"]
        # Inclusive 120 ms source age; a future monotonic source stamp cannot
        # satisfy the same gate used by snapshots and direct commands.
        assert manager._target_finish_permission(now=clock.now+120_000_000)[0]
        assert not manager._target_finish_permission(now=clock.now-1)[0]
        clock.advance(100)
        if stale_source == "光谱":
            manager.on_force(force(clock.now, 1020, [0, 0, -6, -.001, .002, -.003]))
        else:
            manager.on_spectrum(spectrum(clock.now, 1020))
        clock.advance(21)
        guided = manager.snapshot()["guided"]
        assert not guided["can_finish"] and stale_source+"采集源" in guided["finish_block_reason"]
        timeline = deepcopy(manager._timeline)
        with pytest.raises(ValueError, match=stale_source+"采集源"):
            manager.trial("finish", context(clock.now))
        assert manager.phase == "release" and manager._timeline == timeline
        assert not manager._timed["trial"]["finish_requested"]
        manager.mark("操作员确认接触位置错误")
        assert not manager.snapshot()["guided"]["can_finish"]
        assert "人工异常" in manager.snapshot()["guided"]["finish_block_reason"]
        manager.trial("abort", context(clock.now))
    finally:
        manager.stop()


def test_source_callbacks_keep_labels_incremental_and_duplicate_release_is_still_faulted(tmp_path, clock, monkeypatch):
    manager = start_target(tmp_path, clock)
    begin(manager, clock)
    import ninefbg.force_target_wrench as target_module
    original = target_module._label_values
    matched = 0

    def counted_match(*args):
        nonlocal matched
        matched += 1
        return original(*args)

    monkeypatch.setattr(target_module, "_label_values", counted_match)
    try:
        for number in range(1000, 1200):
            tick(manager, clock, number, [1 if number < 1100 else 4, 0, 0, 0, 0, 0])
        state = manager._timed["trial"]
        assert manager.phase == "release"
        # The first simultaneous optical frame precedes the latched contact
        # timeline boundary and remains in waiting_contact, as before.
        assert matched == len(state["main_spectra"]) == 199
        for _ in range(25):
            assert manager.snapshot()["guided"]["can_finish"]
        assert matched == 199
        assert manager.on_spectrum(spectrum(clock.now, 1199))
        assert len(state["main_spectra"]) == 199
        assert "原始光谱帧身份重复" in state["faults"]
        assert not manager.snapshot()["guided"]["can_finish"]
        manager.trial("abort", context(clock.now))
        assert not manager.snapshot()["guided"]["last_quality"]["accepted"]
    finally:
        manager.stop()
