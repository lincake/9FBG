"""Full-spectrum protocol replay; these synthetic tests do not qualify hardware."""
from copy import deepcopy
import json

import pytest

from ninefbg.experiment import ExperimentManager, list_sessions
from ninefbg.timed_wrench import SYNC_RULE
from test_experiment import clock, context, force, settings, spectrum


def start(tmp_path, clock, **options):
    manager = ExperimentManager(tmp_path)
    state = context(clock.now)
    state["tracker"]["peaks"] = []
    state["tracker"]["display_stale"] = True
    manager.start(settings(acquisition_protocol="timed_wrench_v1", force_direction_confirmed=False, **options), state)
    manager.baseline_request(True, state)
    for number in range(12):
        timestamp = clock.advance(40)
        assert manager.on_force(force(timestamp, 100+number))
        assert manager.on_spectrum(spectrum(timestamp, 100+number))
    assert manager.snapshot()["baseline"]["ready"]
    manager.select_point("P01")
    return manager


def tick(manager, clock, number, wrench=None, ms=40, intensity_delta=0):
    timestamp = clock.advance(ms)
    raw = [0, 0, -1, 0, 0, 0]
    if wrench:
        raw = [v + z for v, z in zip(wrench, raw)]
    assert manager.on_force(force(timestamp, number, raw))
    frame = spectrum(timestamp, number)
    frame["intensity"] = [v+intensity_delta for v in frame["intensity"]]
    assert manager.on_spectrum(frame)


def enter_main(manager, clock):
    manager.trial("start", context(clock.now))
    assert manager.snapshot()["phase"] == "waiting_training"
    assert manager.snapshot()["guided"]["pause_reason"].startswith("acquisition:T")
    for _ in range(5):
        assert manager.snapshot()["phase"] == "waiting_training"
    assert manager.training_pause_acknowledged()
    number = 1000
    while manager.phase == "preload":
        tick(manager, clock, number)
        number += 1
        assert number < 1100
    assert manager.phase == "loading"
    return number


@pytest.mark.parametrize("wrench", [[.2, 0, 0, 0, 0, 0], [0, 0, -.6, .001, -.001, .002], [2, -3, 2, .1, -.1, .2]])
def test_fixed_three_seconds_accepts_light_and_signed_lateral_wrenches_without_peak_dependency(tmp_path, clock, wrench):
    manager = start(tmp_path, clock)
    original = deepcopy(manager.baseline)
    number = enter_main(manager, clock)
    start_ns = manager._timed["trial"]["main_start_ns"]
    for _ in range(74):
        tick(manager, clock, number, wrench)
        number += 1
    assert manager.phase == "loading"
    assert manager.snapshot()["guided"]["elapsed_s"] == pytest.approx(2.96)
    tick(manager, clock, number, wrench)
    number += 1
    assert manager.phase == "recovery"
    assert clock.now-start_ns == 3_000_000_000
    assert manager.snapshot()["guided"]["current_wrench"] == pytest.approx(wrench)
    assert manager.snapshot()["guided"]["observed_peak_force_norm_N"] == pytest.approx(sum(v*v for v in wrench[:3])**.5)
    assert manager.trial_id
    for _ in range(24):
        tick(manager, clock, number)
        number += 1
    assert manager.trial_id is None
    quality = manager.snapshot()["guided"]["last_quality"]
    assert quality["accepted"], quality
    assert quality["criteria_version"] == "timed_wrench_quality_v1"
    assert quality["metrics"]["main_duration_s"] == 3
    assert quality["metrics"]["peak_valid_frames"] == 0
    assert quality["metrics"]["valid_label_ratio"] == 1
    assert quality["metrics"]["sync_rule"] == SYNC_RULE
    assert manager.baseline["baseline_id"] != original["baseline_id"]
    assert manager._baselines[original["baseline_id"]]["S0"] == original["S0"]
    assert manager._baselines[original["baseline_id"]]["force_zero"] == original["force_zero"]
    assert manager.baseline["trigger"] == "automatic_unloaded_recovery"
    assert manager.baseline["ready"]
    manager.stop()
    saved = manager.get_session(manager.session_id)
    assert saved["final"]["trials"][0]["quality"]["accepted"]
    assert len(saved["final"]["baseline_history"]) == 2
    assert list_sessions(tmp_path)[0]["accepted_trial_count"] == 1


def test_stable_nonzero_load_and_unrecovered_spectrum_cannot_reset_zero(tmp_path, clock):
    manager = start(tmp_path, clock)
    old_baseline = deepcopy(manager.baseline)
    number = enter_main(manager, clock)
    for _ in range(75):
        tick(manager, clock, number, [.5, 0, 0, 0, 0, 0])
        number += 1
    assert manager.phase == "recovery"
    manager.trial("confirm_unloaded", context(clock.now))
    for _ in range(30):
        tick(manager, clock, number, [.5, 0, 0, 0, 0, 0])
        number += 1
    assert manager.trial_id
    assert manager.baseline["force_zero"] == old_baseline["force_zero"]
    assert "稳定非零" in manager.snapshot()["guided"]["message"]
    # Each component can lie under its axis floor while their resultant still
    # represents contact; the norm gate is independently enforced throughout.
    for _ in range(30):
        tick(manager, clock, number, [.08, .08, 0, 0, 0, 0])
        number += 1
    assert manager.trial_id
    assert manager.baseline["force_zero"] == old_baseline["force_zero"]
    for _ in range(30):
        tick(manager, clock, number, intensity_delta=10)
        number += 1
    assert manager.trial_id
    assert "光谱尚未恢复" in manager.snapshot()["guided"]["message"]
    for _ in range(30):
        tick(manager, clock, number)
        number += 1
    assert manager.trial_id is None
    assert manager.baseline["trigger"] == "manual_unloaded_confirmation"
    assert manager._baselines[old_baseline["baseline_id"]]["spectral_reference"] == old_baseline["spectral_reference"]
    manager.stop()


def test_overlimit_keeps_raw_six_axes_and_rejects_without_clipping(tmp_path, clock):
    manager = start(tmp_path, clock)
    number = enter_main(manager, clock)
    for _ in range(75):
        tick(manager, clock, number, [4, 4, 0, 100, -100, 50])
        number += 1
    for _ in range(25):
        tick(manager, clock, number)
        number += 1
    quality = manager.snapshot()["guided"]["last_quality"]
    assert not quality["accepted"]
    assert quality["metrics"]["peak_force_norm_N"] == pytest.approx(32**.5)
    assert any("合力超过" in reason for reason in quality["reasons"])
    manager.stop()
    rows = [json.loads(line) for line in (manager.writer.path / "raw_force.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["force_values"] == [4, 4, -1, 100, -100, 50] for row in rows)


def test_snapshot_never_advances_source_time_and_pause_timeout_preserves_failed_trial(tmp_path, clock):
    manager = start(tmp_path, clock)
    manager.trial("start", context(clock.now))
    clock.advance(21000)
    for _ in range(20):
        assert manager.snapshot()["phase"] == "waiting_training"
    assert manager.watchdog(clock.now)
    assert manager.trial_id is None
    assert manager.snapshot()["guided"]["last_quality"]["accepted"] is False
    assert any("训练暂停确认超时" in reason for reason in manager.snapshot()["guided"]["last_quality"]["reasons"])
    assert not manager.training_pause_acknowledged()
    manager.stop()


def test_trial_locks_point_and_rejects_duplicate_identity_and_incomplete_raw_frame(tmp_path, clock):
    manager = start(tmp_path, clock)
    number = enter_main(manager, clock)
    with pytest.raises(ValueError, match="不能切换"):
        manager.select_point("P02")
    tick(manager, clock, number, [.5, 0, 0, 0, 0, 0])
    manager.on_spectrum(spectrum(clock.now, number))
    number += 1
    timestamp = clock.advance(40)
    invalid = spectrum(timestamp, number)
    invalid["intensity"] = [1]*511
    manager.on_spectrum(invalid)
    manager.trial("abort", context(clock.now))
    reasons = manager.snapshot()["guided"]["last_quality"]["reasons"]
    assert any("身份重复" in reason for reason in reasons)
    assert any("512" in reason for reason in reasons)
    manager.stop()


def test_25_hz_spectra_and_30_hz_force_with_jitter_keep_trustworthy_bracket_coverage(tmp_path, clock):
    manager = start(tmp_path, clock)
    manager.trial("start", context(clock.now))
    assert manager.training_pause_acknowledged()
    base = clock.now
    events = []
    # Different real source IDs survive even when all intensity values match.
    for index in range(1, 181):
        jitter = [0, 700_000, -400_000, 200_000][index % 4]
        events.append((base+index*40_000_000+jitter, "spectrum", index+1000))
    for index in range(1, 217):
        jitter = [200_000, -300_000, 600_000][index % 3]
        events.append((base+round(index*1e9/30)+jitter, "force", index+1000))
    for timestamp, kind, number in sorted(events):
        clock.now = timestamp
        if kind == "force":
            values = [.7, -.2, -1.1, .002, -.003, .001] if manager.phase == "loading" else [0, 0, -1, 0, 0, 0]
            manager.on_force(force(timestamp, number, values))
        else:
            manager.on_spectrum(spectrum(timestamp, number))
        if manager.trial_id is None:
            break
    quality = manager.snapshot()["guided"]["last_quality"]
    assert quality["accepted"], quality
    assert quality["metrics"]["valid_label_ratio"] > .98
    assert quality["metrics"]["main_raw_spectra"] >= 74
    assert quality["metrics"]["sync_rule"]["hardware_delay_calibration"] == "unknown"
    manager.stop()
    saved = manager.get_session(manager.session_id)
    trial = saved["final"]["trials"][0]
    assert trial["main_end_monotonic_ns"] - trial["main_start_monotonic_ns"] == 3_000_000_000
    rows = [json.loads(line) for line in (manager.writer.path / "raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(trial["main_start_monotonic_ns"] <= row["host_rx_monotonic_ns"] < trial["main_end_monotonic_ns"]
               for row in rows if row.get("phase") == "loading")
