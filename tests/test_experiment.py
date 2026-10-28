from copy import deepcopy
import json

import pytest

from ninefbg.experiment import ExperimentManager, coordinate_definition, default_point_grid, get_session, list_sessions


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 10_000_000_000

        def advance(self, ms=1):
            self.now += int(ms * 1e6)
            return self.now
    value = Clock()
    monkeypatch.setattr("ninefbg.experiment.time.perf_counter_ns", lambda: value.now)
    return value


def result(stamp, frame_id=1, version=2, delta_nm=0):
    reference = [1510.5 + index * 2 for index in range(9)]
    return {"frame_id": frame_id, "host_rx_monotonic_ns": stamp, "stream_epoch": "optical-a",
            "source_kind": "hardware", "time_quality": "host_rx_approx", "algorithm_version": "gaussian-raw-v1",
            "config_version": "config-hash", "reference_version": version, "reference_nm": reference,
            "state": "tracking", "reference_pending": False,
            "peaks": [{"id": f"FBG{index + 1}", "valid": True, "stale": False, "center_nm": value + delta_nm}
                      for index, value in reversed(list(enumerate(reference)))]}


def context(stamp, version=1):
    return {"mode": "hardware", "running": True, "force_connected": True,
            "frame": {"stream_epoch": "optical-a", "host_rx_monotonic_ns": stamp},
            "force": force(stamp), "devices": [{"connected": True, "wavelength_axis": {"point_count": 512}}],
            "tracker": {**result(stamp, version=version), "display_stale": False, "display_age_ms": 0}}


def force(stamp, frame_id=1, values=None):
    return {"frame_id": frame_id, "host_rx_monotonic_ns": stamp, "stream_epoch": "force-a", "source_kind": "hardware",
            "time_quality": "host_rx_approx", "force_values": values or [0, 0, -1, 0, 0, 0],
            "axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"], "units": ["N", "N", "N", "N·m", "N·m", "N·m"], "age_ms": 0}


def spectrum(stamp, frame_id=1):
    return {"frame_id": frame_id, "host_rx_monotonic_ns": stamp, "stream_epoch": "optical-a", "source_kind": "hardware",
            "time_quality": "host_rx_approx", "wavelength_nm": [1510 + index * .03 + index * index * 1e-6 for index in range(512)],
            "intensity": [index + 100 for index in range(512)], "phase": "unknown"}


def settings(**extra):
    return {"finger_id": "finger-01", "batch": "A", "force_sign": -1, "force_direction_confirmed": True,
            "coordinates_confirmed": True, "conditions": {"temperature_C": 23}, **extra}


def sync(spec_id, force_id, values, **extra):
    return {"valid": True, "matched": True, "method": "exact_receive_time", "spectrum_frame_id": spec_id,
            "spectrum_stream_epoch": "optical-a", "force_stream_epoch": "force-a", "force_frame_ids": [force_id],
            "force_values": values, "time_quality": "host_rx_approx", **extra}


def ready(manager, clock, version=2):
    manager.baseline_request(True, context(clock.now, version - 1))
    for index in range(12):
        stamp = clock.advance(40)
        manager.on_force(force(stamp, 100 * version + index))
    manager.on_result(result(clock.now, 50 * version, version), {"valid": False, "reason": "baseline no pair"})
    assert manager.snapshot()["baseline"]["ready"]


def rows(manager, name):
    return [json.loads(line) for line in (manager.writer.path / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]


def test_capture_coordination_reads_only_handshake_state(tmp_path, monkeypatch):
    manager = ExperimentManager(tmp_path)
    manager.running = True
    manager.settings = {"acquisition_protocol": "force_target_wrench_v1"}
    manager.trial_id = "T0007"

    def expensive_read():
        raise AssertionError("The frequent coordinator must not build a UI/writer snapshot")

    monkeypatch.setattr(manager, "snapshot", expensive_read)
    monkeypatch.setattr(manager, "_guided_snapshot", expensive_read)
    manager.writer = type("Writer", (), {"status": staticmethod(expensive_read)})()
    assert manager.coordination_snapshot() == {
        "running": True, "acquisition_protocol": "force_target_wrench_v1", "trial_id": "T0007"}
    manager.running, manager.trial_id = False, None
    assert manager.coordination_snapshot() == {
        "running": False, "acquisition_protocol": "force_target_wrench_v1", "trial_id": None}


def test_new_grid_corners_and_versioned_operator_coordinate_definition(tmp_path, clock):
    grid = default_point_grid()
    corners = {point["id"]: (point["row"], point["col"], point["u"], point["v"], point["x_norm"], point["y_norm"])
               for point in grid if point["id"] in {"P01", "P05", "P21", "P25"}}
    assert corners == {"P01": (0, 0, 0, 0, 0, 0), "P05": (0, 4, 1, 0, 1, 0),
                       "P21": (4, 0, 0, 1, 0, 1), "P25": (4, 4, 1, 1, 1, 1)}
    definition = coordinate_definition()
    assert definition == {"schema": "bottom_left_xy_v1", "origin": "bottom_left",
                          "row_index_base": 0, "col_index_base": 0, "x_positive": "right",
                          "y_positive": "fingertip_up", "finger_pad_up_in_px6d": [0, 0, -1],
                          "px6d_rotation_verified": False}
    definition["finger_pad_up_in_px6d"][2] = 1
    assert coordinate_definition()["finger_pad_up_in_px6d"] == [0, 0, -1]
    manager = ExperimentManager(tmp_path)
    manager.start(settings(point_grid=grid), context(clock.now))
    snapshot = manager.snapshot()
    assert snapshot["settings"]["coordinate_definition"] == coordinate_definition()
    assert snapshot["settings"]["force_sign"] == -1
    assert snapshot["settings"]["point_grid"] == grid
    manager.stop()
    manifest = manager.get_session(manager.session_id)
    assert manifest["coordinate_definition"] == manifest["settings"]["coordinate_definition"] == coordinate_definition()
    assert manifest["settings"]["point_grid"] == grid


@pytest.mark.parametrize("change", ["old_index_base", "wrong_row", "wrong_col", "wrong_u", "wrong_v", "conflicting_x_alias", "conflicting_y_alias", "missing_row", "empty_grid", "old_definition"])
def test_new_sessions_reject_ambiguous_old_or_inconsistent_coordinates(tmp_path, clock, change):
    grid = default_point_grid()
    supplied = settings(point_grid=grid)
    if change == "old_index_base":
        for point in grid:
            point["row"] += 1
            point["col"] += 1
    elif change == "wrong_row":
        grid[0]["row"] = 4
    elif change == "wrong_col":
        grid[0]["col"] = 4
    elif change == "wrong_u":
        grid[0]["u"] = 1
    elif change == "wrong_v":
        grid[0]["v"] = 1
    elif change == "conflicting_x_alias":
        grid[0]["x_norm"] = .25
    elif change == "conflicting_y_alias":
        grid[0]["y_norm"] = .25
    elif change == "missing_row":
        del grid[0]["row"]
    elif change == "empty_grid":
        supplied["point_grid"] = []
    elif change == "old_definition":
        supplied["coordinate_definition"] = {"schema": "legacy_grid", "row_index_base": 1}
    manager = ExperimentManager(tmp_path)
    with pytest.raises(ValueError):
        manager.start(supplied, context(clock.now))
    assert not (tmp_path / "calibration_sessions").exists()


def test_loading_historical_manifest_does_not_reinterpret_its_grid(tmp_path):
    session_id = "legacy-session"
    path = tmp_path / "calibration_sessions" / session_id
    path.mkdir(parents=True)
    grid = default_point_grid()
    for point in grid:
        point["row"] += 1
        point["col"] += 1
    historical = {"session_id": session_id, "kind": "9fbg_calibration_experiment_v1",
                  "settings": {"point_grid": grid, "force_sign": -1}, "complete": True,
                  "counts": {}, "final": {}}
    file = path / "manifest.json"
    file.write_text(json.dumps(historical), encoding="utf-8")
    before = file.read_bytes()
    loaded = get_session(tmp_path, session_id)
    assert loaded == historical
    assert loaded["settings"]["point_grid"][0]["row"] == 1
    assert "coordinate_definition" not in loaded and "coordinate_definition" not in loaded["settings"]
    assert file.read_bytes() == before


def test_explicit_start_grid_guards_and_no_auto_restore(tmp_path, clock):
    manager = ExperimentManager(tmp_path)
    assert not (tmp_path / "calibration_sessions").exists()
    for limit in (0, -1, float("inf"), "3"):
        with pytest.raises(ValueError, match="max_force_N"):
            manager.start(settings(max_force_N=limit), context(clock.now))
    grid = default_point_grid()
    grid[0]["id"] = "custom"
    with pytest.raises(ValueError, match="P01"):
        manager.start(settings(point_grid=grid), context(clock.now))
    invalid = context(clock.now)
    invalid["mode"] = "replay"
    with pytest.raises(ValueError, match="实际设备"):
        manager.start(settings(), invalid)
    invalid = context(clock.now)
    invalid["tracker"]["peaks"][0]["valid"] = False
    with pytest.raises(ValueError, match="9 个"):
        manager.start(settings(), invalid)
    assert not (tmp_path / "calibration_sessions").exists()
    manager.start(settings(force_direction_confirmed=False, coordinates_confirmed=False), context(clock.now))
    grid = manager.snapshot()["settings"]["point_grid"]
    assert len(grid) == 25 and grid[0]["id"] == "P01" and grid[-1]["id"] == "P25"
    assert (grid[0]["u"], grid[0]["v"], grid[-1]["u"], grid[-1]["v"]) == (0, 0, 1, 1)
    ready(manager, clock)
    with pytest.raises(ValueError, match="方向与 25 点"):
        manager.trial("start", context(clock.now, 2))
    manager.stop()
    assert get_session(tmp_path, manager.session_id)["formal_training_qualified"] is False
    assert get_session(tmp_path, manager.session_id)["acquisition_audit"]["tracker"]["reference_nm"] == result(clock.now)["reference_nm"]
    assert len(list_sessions(tmp_path)) == 1
    restored = ExperimentManager(tmp_path)
    assert restored.snapshot()["baseline"]["ready"] is False
    assert restored.snapshot()["running"] is False
    with pytest.raises(ValueError, match="标识"):
        get_session(tmp_path, "../outside")


def test_data_id_is_independent_metadata_and_diameter_is_not_required(tmp_path, clock):
    manager = ExperimentManager(tmp_path)
    entered = "  数据-0007  "
    original_settings = settings(data_id=entered)
    snapshot = manager.start(original_settings, context(clock.now))
    assert snapshot["settings"]["data_id"] == entered
    assert snapshot["settings"]["finger_id"] == original_settings["finger_id"]
    assert "tip_diameter_mm" not in snapshot["settings"]["conditions"]
    manager.stop()
    stored = get_session(tmp_path, manager.session_id)
    assert stored["settings"]["data_id"] == entered
    assert list_sessions(tmp_path)[0]["data_id"] == entered
    assert "data_id" not in stored["acquisition_context"]


def test_missing_data_id_preserves_legacy_contact_diameter_without_conversion(tmp_path, clock):
    manager = ExperimentManager(tmp_path)
    snapshot = manager.start(settings(conditions={"tip_diameter_mm": 2.5}), context(clock.now))
    assert snapshot["settings"]["data_id"] == ""
    assert snapshot["settings"]["conditions"]["tip_diameter_mm"] == 2.5
    manager.stop()
    stored = get_session(tmp_path, manager.session_id)
    assert stored["settings"]["data_id"] == ""
    assert stored["settings"]["conditions"]["tip_diameter_mm"] == 2.5


@pytest.mark.parametrize("data_id", [None, 7, True, [], "x" * 129])
def test_invalid_data_id_rejected_before_session_files_are_created(tmp_path, clock, data_id):
    manager = ExperimentManager(tmp_path)
    with pytest.raises(ValueError, match="数据编号"):
        manager.start(settings(data_id=data_id), context(clock.now))
    assert not (tmp_path / "calibration_sessions").exists()


def test_after_confirmation_distinct_frames_span_and_real_reference_list(tmp_path, clock):
    manager = ExperimentManager(tmp_path)
    manager.start(settings(), context(clock.now))
    clock.advance(10)
    manager.baseline_request(True, context(clock.now))
    original = force(clock.now - 1, 99, [9] * 6)
    manager.on_force(original)
    manager.on_result(result(clock.now - 1), {"valid": False})
    assert manager.snapshot()["baseline"]["lambda0_nm"] is None
    assert manager.snapshot()["baseline"].get("force_count", 0) == 0
    for index in range(12):
        frame = force(clock.advance(20), index + 1)
        manager.on_force(frame)
        manager.on_force(deepcopy(frame))
    assert manager.snapshot()["baseline"]["force_zero"] is None
    assert manager.snapshot()["baseline"]["force_count"] == 12
    manager.on_result(result(clock.now, 20, version=1), {"valid": False})
    assert manager.snapshot()["baseline"]["lambda0_nm"] is None, "An old reference version cannot qualify a fresh baseline"
    manager.on_result(result(clock.now, 21, version=2), {"valid": False})
    assert manager.snapshot()["baseline"]["ready"] is False
    for index in range(12, 21):
        manager.on_force(force(clock.advance(20), index + 1))
    baseline = manager.snapshot()["baseline"]
    assert baseline["ready"] and baseline["force_zero"] == [0, 0, -1, 0, 0, 0]
    assert baseline["force_span_ms"] >= 400
    assert len(set(baseline["force_frame_ids"])) == 12
    assert baseline["lambda0_nm"] == result(clock.now)["reference_nm"]
    assert original["force_values"] == [9] * 6
    manager.stop()
    history = manager.get_session(manager.session_id)["final"]["baseline_history"]
    assert history[0]["effective_monotonic_ns"] > history[0]["requested_monotonic_ns"]
    assert history[0]["force_epoch"] == "force-a"


def test_trial_timeline_contact_labels_frozen_zero_and_new_baseline(tmp_path, clock):
    manager = ExperimentManager(tmp_path)
    manager.start(settings(), context(clock.now))
    ready(manager, clock)
    baseline = manager.snapshot()["baseline"]
    clock.advance(1)
    manager.trial("start", context(clock.now, 2))
    old_stamp = clock.now
    with pytest.raises(ValueError, match="切换"):
        manager.select_point("P02")
    with pytest.raises(ValueError, match="冻结"):
        manager.baseline_request(True, context(clock.now, 2))
    with pytest.raises(ValueError, match="阶段顺序"):
        manager.trial("removed", context(clock.now, 2))
    clock.advance(1)
    manager.trial("loading", context(clock.now, 2))
    # A queued preload frame is saved as preload, never relabelled loading.
    raw = spectrum(old_stamp, 77)
    before = deepcopy(raw)
    manager.on_spectrum(raw)
    assert raw == before
    for action in (None, "hold", "removed", "recovery"):
        if action:
            clock.advance(1)
            manager.trial(action, context(clock.now, 2))
        stamp = clock.advance(2)
        frame_id = int(stamp)
        values = [0.6, 0, -3, 0, 0, 0]
        manager.on_force(force(stamp, frame_id, values))
        manager.on_result(result(stamp, frame_id, 2, .002), sync(frame_id, frame_id, values))
    assert manager.snapshot()["baseline"]["force_zero"] == baseline["force_zero"]
    assert manager.snapshot()["baseline"]["lambda0_nm"] == baseline["lambda0_nm"]
    clock.advance(1)
    manager.trial("finish", context(clock.now, 2))
    with pytest.raises(ValueError, match="新的明确无载"):
        manager.trial("start", context(clock.now, 2))
    manager.select_point("P02")
    manager.stop()
    saved_raw = rows(manager, "raw_spectra")[0]
    assert saved_raw["phase"] == "preload" and saved_raw["trial_id"] == "T0001"
    assert saved_raw["intensity"] == before["intensity"]
    saved = rows(manager, "results")[-4:]
    assert [row["point_id"] for row in saved] == ["P01", "P01", None, None]
    assert [row["phase"] for row in saved] == ["loading", "hold", "removed", "recovery"]
    assert all(row["valid"] and row["force_N"] == 2 and row["force_corrected"][2] == -2 for row in saved)
    assert saved[0]["x_pm"] == pytest.approx([2] * 9)
    assert saved[0]["lateral_warning"] is True and saved[0]["split"] == "A"
    manifest = manager.get_session(manager.session_id)
    assert manifest["complete"] and manifest["final"]["counts"]["trials_completed"] == 1
    assert set(manifest["files"]) == {"events.jsonl", "raw_force.jsonl", "raw_spectra.jsonl", "results.jsonl"}


def test_sync_source_proof_old_frames_and_manual_bad(tmp_path, clock):
    manager = ExperimentManager(tmp_path)
    manager.start(settings(), context(clock.now))
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    clock.advance(1)
    manager.trial("loading", context(clock.now, 2))
    stamp = clock.advance(10)
    values = [0, 0, -2, 0, 0, 0]
    manager.on_force(force(stamp, 900, values))
    manager.on_result(result(stamp, 800), sync(800, 900, [0, 0, -8, 0, 0, 0]))
    manager.on_result(result(stamp, 801), sync(801, 999, values))
    manager.on_result(result(stamp, 802), sync(802, 900, values, method="nearest_diagnostic_only"))
    manager.on_result(result(stamp + 50_000_000, 803), sync(803, 900, values))
    clock.advance(1)
    manager.mark("接触棒侧滑")
    stamp = clock.advance(10)
    manager.on_force(force(stamp, 901, values))
    manager.on_result(result(stamp, 804), sync(804, 901, values))
    manager.trial("abort", {})
    manager.stop()
    saved = rows(manager, "results")[-5:]
    assert all(row["valid"] is False for row in saved)
    assert "不一致" in saved[0]["sync"]["reason"]
    assert "未记录" in saved[1]["sync"]["reason"]
    assert saved[-1]["sync"]["valid"] is True and saved[-1]["bad"] is True
    assert saved[-1]["manual_mark"] == ["接触棒侧滑"]
    assert manager.snapshot()["counts"]["trials_aborted"] == 1


def test_epoch_change_and_writer_failure_stop_only_experiment(tmp_path, clock, monkeypatch):
    manager = ExperimentManager(tmp_path)
    manager.start(settings(), context(clock.now))
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    changed = force(clock.advance(1), 800)
    changed["stream_epoch"] = "force-reconnected"
    manager.on_force(changed)
    assert not manager.snapshot()["running"]
    assert manager.snapshot()["counts"]["trials_aborted"] == 1
    assert manager.get_session(manager.session_id)["final"]["reason"] == "force_epoch_changed"
    failed = ExperimentManager(tmp_path)
    failed.start(settings(), context(clock.now))
    monkeypatch.setattr(failed.writer, "submit", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    assert failed.on_spectrum(spectrum(clock.advance(1))) is False
    assert failed.snapshot()["running"] is False
    assert "disk full" in failed.snapshot()["error"]
    assert failed.get_session(failed.session_id)["complete"] is False


def test_valid_interpolation_preserves_real_source_deltas_not_zero_error(tmp_path, clock):
    manager = ExperimentManager(tmp_path)
    manager.start(settings(), context(clock.now))
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    clock.advance(1)
    manager.trial("loading", context(clock.now, 2))
    left = clock.advance(1)
    manager.on_force(force(left, 900, [0, 0, -2, 0, 0, 0]))
    right = clock.advance(32)
    manager.on_force(force(right, 901, [0, 0, -4, 0, 0, 0]))
    center = left + 16_000_000
    pair = sync(800, 900, [0, 0, -3, 0, 0, 0], method="linear_receive_time", force_frame_ids=[900, 901], delta_ms=0)
    manager.on_result(result(center, 800), pair)
    manager.stop()
    saved = rows(manager, "results")[-1]
    assert saved["valid"] is True, "A real valid 32 ms interpolation remains usable even though its nearest source is over the 5 ms check target"
    quality = saved["sync"]
    assert quality["left_delta_ms"] == quality["right_delta_ms"] == quality["nearest_delta_ms"] == 16
    assert quality["bracket_ms"] == 32
    assert quality["force_source_stamps_ns"] == [left, right]
    assert quality["target_exceeded"] is True
    assert "不代表" in quality["delta_ms_semantics"]


def guided_manager(tmp_path, clock, repetitions=3):
    manager = ExperimentManager(tmp_path)
    manager.start(settings(guided_acquisition=True, repetitions_per_point=repetitions), context(clock.now))
    return manager


def guided_sample(manager, clock, normal=0, *, ms=20, version=2, invalid=False, delayed=False, force_extra=None):
    stamp = clock.advance(ms)
    frame_id = stamp
    values = [0, 0, -1-normal, 0, 0, 0]
    force_frame = force(stamp, frame_id, values)
    force_frame.update(force_extra or {})
    manager.on_force(force_frame)
    manager.on_spectrum(spectrum(stamp, frame_id))
    fitted = result(stamp, frame_id, version=version, delta_nm=normal * .001)
    if invalid:
        fitted["peaks"][0].update(valid=False, stale=True)
    pair = sync(frame_id, frame_id, values)
    if not delayed:
        manager.on_result(fitted, pair)
    return force_frame, fitted, pair


def guided_preload(manager, clock, version=2):
    manager.trial("start", context(clock.now, version))
    for _ in range(15):
        guided_sample(manager, clock, ms=50, version=version)
        if manager.snapshot()["guided"]["step"] == "loading":
            return
    raise AssertionError("Expected real valid unloaded samples to advance preload")


def guided_loading(manager, clock, version=2, *, invalid_count=0):
    for index in range(30):
        guided_sample(manager, clock, normal=.3 + 2.4 * index / 29, version=version, invalid=index < invalid_count)
    for _ in range(11):
        guided_sample(manager, clock, normal=3, version=version)
    assert manager.snapshot()["guided"]["step"] == "unloading"
    assert manager.phase == "hold" and manager.trial_id


def guided_unload(manager, clock, version=2, *, defer_watermark=False):
    for _ in range(21):
        guided_sample(manager, clock, normal=0, version=version)
    assert manager.phase == "recovery" and manager.trial_id, "Keep trial/reference frozen until processed source watermark catches up"
    if not defer_watermark:
        guided_sample(manager, clock, normal=0, version=version)
        assert manager.trial_id is None


def test_guided_full_round_confirm_baseline_loading_unload_and_quality(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    initial = manager.snapshot()
    assert initial["point_id"] is None and initial["guided"]["step"] == "select_point"
    assert initial["guided"]["total_required"] == 75 and initial["guided"]["total_accepted"] == 0
    with pytest.raises(ValueError, match="确认"):
        manager.baseline_request(True, context(clock.now))
    manager.select_point("P01")
    assert manager.snapshot()["guided"]["step"] == "needs_baseline"
    ready(manager, clock)
    assert manager.snapshot()["guided"]["step"] == "ready"
    locked = manager.snapshot()["baseline"]
    guided_preload(manager, clock)
    guided_loading(manager, clock)
    assert manager.snapshot()["baseline"]["lambda0_nm"] == locked["lambda0_nm"]
    assert manager.snapshot()["guided"]["force_progress_percent"] == 100
    guided_unload(manager, clock)
    guided = manager.snapshot()["guided"]
    assert guided["step"] == "round_complete" and guided["point_accepted"] == guided["point_attempts"] == 1
    assert guided["current_round"] == 2 and guided["progress_percent"] == pytest.approx(100 / 75)
    assert guided["point_progress_percent"] == pytest.approx(100 / 3)
    quality = guided["last_quality"]
    assert quality["accepted"] and quality["reasons"] == [] and quality["criteria_version"] == "guided_round_v1"
    assert quality["metrics"]["target_hold_ms"] >= 200 and quality["metrics"]["unload_hold_ms"] >= 400
    assert quality["metrics"]["valid_spectra"] >= 30 and quality["metrics"]["valid_ratio"] == 1
    assert quality["metrics"]["force_bins_covered"] == 5
    assert quality["metrics"]["physical_accuracy_qualified"] is False
    with pytest.raises(ValueError, match="新的明确无载"):
        manager.trial("start", context(clock.now, 2))
    manager.stop()
    manifest = manager.get_session(manager.session_id)
    assert manifest["final"]["trials"][0]["quality"] == quality
    assert manifest["final"]["trials"][0]["bad"] is False
    events = rows(manager, "events")
    names = [row["event"] for row in events]
    assert all(name in names for name in ("guided_loading_ready", "guided_target_reached", "guided_removed", "guided_recovery", "guided_round_quality"))
    assert next(row for row in events if row["event"] == "guided_round_quality")["quality"]["accepted"] is True


def test_guided_retry_requires_new_baseline_and_does_not_count_bad_attempt(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    guided_preload(manager, clock)
    guided_loading(manager, clock, invalid_count=10)
    guided_unload(manager, clock)
    guided = manager.snapshot()["guided"]
    assert guided["step"] == "retry" and guided["point_accepted"] == 0 and guided["point_attempts"] == 1
    assert not guided["last_quality"]["accepted"]
    assert any("90%" in reason for reason in guided["last_quality"]["reasons"])
    with pytest.raises(ValueError, match="新的明确无载"):
        manager.trial("start", context(clock.now, 2))
    ready(manager, clock, version=3)
    guided_preload(manager, clock, version=3)
    guided_loading(manager, clock, version=3)
    guided_unload(manager, clock, version=3)
    assert manager.snapshot()["guided"]["point_accepted"] == 1
    assert manager.snapshot()["guided"]["point_attempts"] == 2
    manager.stop()
    trials = manager.get_session(manager.session_id)["final"]["trials"]
    assert [trial["quality"]["accepted"] for trial in trials] == [False, True]
    assert [trial["bad"] for trial in trials] == [True, False]
    assert len(rows(manager, "raw_spectra")) > 100


def test_guided_duplicate_or_stale_frames_and_snapshot_cannot_advance(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    force_frame, fitted, pair = guided_sample(manager, clock)
    clock.advance(600)
    for _ in range(15):
        manager.on_force(deepcopy(force_frame))
        manager.on_result(deepcopy(fitted), deepcopy(pair))
        assert manager.snapshot()["guided"]["step"] == "preload"
    state = manager._guided_rounds[manager.trial_id]
    assert state["preload_ms"] == 0 and state["force_frames"] == 1
    stale = force(clock.advance(20), 123456, [0, 0, -4, 0, 0, 0])
    stale["age_ms"] = 251
    manager.on_force(stale)
    assert state["target_reached"] is False and state["invalid_force_frames"] == 1
    assert manager.snapshot()["guided"]["step"] == "preload"
    manager.trial("abort", {})
    assert manager.snapshot()["guided"]["step"] == "retry"
    assert manager.snapshot()["guided"]["point_accepted"] == 0
    manager.stop()


def test_guided_no_manual_bypass_no_point_switch_while_quality_pending(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    guided_preload(manager, clock)
    for action in ("loading", "hold", "removed", "recovery", "finish"):
        with pytest.raises(ValueError, match="不能手动"):
            manager.trial(action, context(clock.now, 2))
    guided_loading(manager, clock)
    guided_unload(manager, clock, defer_watermark=True)
    with pytest.raises(ValueError, match="切换"):
        manager.select_point("P02")
    with pytest.raises(ValueError, match="尚未结束"):
        manager.trial("start", context(clock.now, 2))
    guided_sample(manager, clock)
    manager.select_point("P02")
    assert manager.snapshot()["guided"]["step"] == "needs_baseline" and not manager.baseline["ready"]
    manager.stop()


def test_guided_delayed_invalid_result_revises_original_round_and_progress(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    guided_preload(manager, clock)
    delayed = [guided_sample(manager, clock, normal=1, invalid=True, delayed=True) for _ in range(10)]
    guided_loading(manager, clock)
    guided_unload(manager, clock)
    assert manager.snapshot()["guided"]["point_accepted"] == 1
    timeline = deepcopy(manager._timeline)
    for _, fitted, pair in delayed:
        manager.on_result(fitted, pair)
    guided = manager.snapshot()["guided"]
    assert guided["point_accepted"] == guided["total_accepted"] == 0
    assert guided["step"] == "retry" and not guided["last_quality"]["accepted"]
    assert guided["last_quality"]["metrics"]["processed_spectra"] >= 70
    assert manager._timeline == timeline, "Late quality evidence cannot insert an old phase boundary"
    manager.stop()
    trial = manager.get_session(manager.session_id)["final"]["trials"][0]
    assert trial["bad"] and not trial["quality"]["accepted"]
    assert any(row["event"] == "guided_late_quality_update" for row in rows(manager, "events"))


@pytest.mark.parametrize("repetitions", [1, 2, 3])
def test_guided_point_repetitions_progress_and_settings_bounds(tmp_path, clock, repetitions):
    for invalid_repetitions in (0, -1, 1000, 1.0, 3.5, True, "1"):
        manager = ExperimentManager(tmp_path)
        with pytest.raises(ValueError, match="1～999"):
            manager.start(settings(guided_acquisition=True, repetitions_per_point=invalid_repetitions), context(clock.now))
    manager = guided_manager(tmp_path, clock, repetitions=repetitions)
    manager.select_point("P01")
    for version in range(2, 2 + repetitions):
        ready(manager, clock, version=version)
        guided_preload(manager, clock, version=version)
        guided_loading(manager, clock, version=version)
        guided_unload(manager, clock, version=version)
    guided = manager.snapshot()["guided"]
    assert guided["point_accepted"] == guided["point_attempts"] == repetitions
    assert guided["point_progress_percent"] == 100 and guided["progress_percent"] == 4
    assert guided["step"] == "point_complete" and not guided["complete"]
    with pytest.raises(ValueError, match="尚未完成"):
        manager.baseline_request(True, context(clock.now, 1 + repetitions))
    manager.select_point("P02")
    guided = manager.snapshot()["guided"]
    assert guided["point_accepted"] == guided["point_attempts"] == 0 and guided["current_round"] == 1
    assert guided["accepted_by_point"]["P01"] == repetitions and guided["total_accepted"] == repetitions
    manager.stop()


def test_repetition_upper_bound_and_default_are_preserved(tmp_path, clock):
    manager = guided_manager(tmp_path, clock, repetitions=999)
    assert manager.snapshot()["settings"]["repetitions_per_point"] == 999
    assert manager.snapshot()["guided"]["total_required"] == 24975
    manager.stop()
    manager = ExperimentManager(tmp_path)
    manager.start(settings(guided_acquisition=True), context(clock.now))
    assert manager.snapshot()["settings"]["repetitions_per_point"] == 3
    assert manager.snapshot()["guided"]["total_required"] == 75
    manager.stop()


@pytest.mark.parametrize("failure", ["overload", "axes", "manual", "stream_gap", "optical_gap"])
def test_guided_force_faults_never_count_a_round_but_raw_is_retained(tmp_path, clock, failure):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    guided_preload(manager, clock)
    if failure == "overload":
        guided_sample(manager, clock, normal=3.31)
        assert manager.snapshot()["guided"]["step"] == "unloading"
    else:
        guided_loading(manager, clock)
        if failure == "axes":
            guided_sample(manager, clock, normal=3, force_extra={"axes": ["Fx"] * 6})
        elif failure == "manual":
            manager.mark("接触棒滑动")
        elif failure == "stream_gap":
            guided_sample(manager, clock, normal=3, ms=251)
        elif failure == "optical_gap":
            for _ in range(21):
                stamp = clock.advance(50)
                manager.on_force(force(stamp, stamp, [0, 0, -4, 0, 0, 0]))
    guided_unload(manager, clock)
    guided = manager.snapshot()["guided"]
    assert guided["step"] == "retry" and guided["point_accepted"] == 0
    assert not guided["last_quality"]["accepted"] and guided["last_quality"]["reasons"]
    manager.stop()
    assert len(rows(manager, "raw_force")) >= 30
    trial = manager.get_session(manager.session_id)["final"]["trials"][0]
    assert trial["status"] == "completed" and trial["bad"]


def test_guided_distinct_target_frames_and_unload_duration_are_required(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    guided_preload(manager, clock)
    force_frame, fitted, pair = guided_sample(manager, clock, normal=3)
    for _ in range(15):
        clock.advance(20)
        manager.on_force(deepcopy(force_frame))
        manager.on_result(deepcopy(fitted), deepcopy(pair))
        manager.snapshot()
    assert manager.phase == "loading" and manager.snapshot()["guided"]["step"] == "loading"
    # A new stale target frame also cannot satisfy the hold interval.
    guided_sample(manager, clock, normal=3, force_extra={"stale": True})
    assert manager.phase == "loading"
    for _ in range(11):
        guided_sample(manager, clock, normal=3)
    assert manager.phase == "hold"
    for _ in range(10):
        guided_sample(manager, clock, normal=0)
    assert manager.trial_id and manager.phase == "hold", "Less than 400 ms of recovery cannot finish"
    manager.trial("abort", {})
    quality = manager.snapshot()["guided"]["last_quality"]
    assert not quality["accepted"] and not quality["metrics"]["recovered"]
    manager.stop()


def test_guided_pending_tail_result_is_checked_before_quality_finalization(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    guided_preload(manager, clock)
    guided_loading(manager, clock)
    deferred = []
    for index in range(21):
        deferred.append(guided_sample(manager, clock, normal=0, delayed=index >= 12, invalid=index >= 12))
    assert manager.trial_id and manager.phase == "recovery"
    assert manager.snapshot()["guided"]["point_accepted"] == 0
    for _, fitted, pair in deferred[12:]:
        manager.on_result(fitted, pair)
    assert manager.trial_id, "The recovery source watermark has not arrived yet"
    guided_sample(manager, clock)
    quality = manager.snapshot()["guided"]["last_quality"]
    assert not quality["accepted"] and quality["metrics"]["valid_ratio"] < .9
    assert manager.snapshot()["guided"]["point_accepted"] == 0
    manager.stop()


def test_guided_ready_baseline_mark_returns_to_manual_calibration_gate(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    assert manager.snapshot()["guided"]["step"] == "ready"
    manager.mark("接触棒尚未完全移开")
    assert manager.snapshot()["guided"]["step"] == "needs_baseline"
    assert not manager.snapshot()["baseline"]["ready"]
    with pytest.raises(ValueError, match="新的明确无载"):
        manager.trial("start", context(clock.now, 2))
    ready(manager, clock, version=3)
    assert manager.snapshot()["guided"]["step"] == "ready" and manager.baseline["ready"]
    manager.stop()


def test_guided_mark_during_preload_can_unload_and_finish_as_failed(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    manager.mark("点位确认有误")
    assert manager.phase == "hold" and manager.snapshot()["guided"]["step"] == "unloading"
    guided_unload(manager, clock)
    assert manager.snapshot()["guided"]["step"] == "retry"
    assert not manager.snapshot()["guided"]["last_quality"]["accepted"]
    manager.stop()


def test_preload_diagnostics_reset_on_real_invalid_pairs_and_keep_original_gate(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    criteria = manager.snapshot()["settings"]["guided_criteria"]
    assert criteria["criteria_version"] == "guided_round_v1"
    assert criteria["preload_timeout_ms"] == 8000 and criteria["preload_ms"] == 500
    assert criteria["unloaded_abs_max_N"] == .1 and criteria["minimum_valid_ratio"] == .9
    for _ in range(3):
        guided_sample(manager, clock, ms=100)
        guided_sample(manager, clock, ms=100)
        force_frame, fitted, pair = guided_sample(manager, clock, ms=100, delayed=True)
        pair.update(valid=False, reason="测试：左右六轴源帧不满足配对条件")
        manager.on_result(fitted, pair)
    diagnostic = manager.snapshot()["guided"]["preload"]
    assert diagnostic["schema"] == "preload_diagnostics_v1"
    assert diagnostic["elapsed_ms"] == 900 and diagnostic["valid_continuous_ms"] == 0
    assert diagnostic["reset_count"] == 3 and "同步配对无效" in diagnostic["last_block_reason"]
    assert manager.phase == "preload"
    for _ in range(5):
        guided_sample(manager, clock, ms=100)
    assert manager.snapshot()["guided"]["preload"]["valid_continuous_ms"] == 400
    assert manager.phase == "preload", "Elapsed wall time must not replace 500 ms continuous valid source time"
    guided_sample(manager, clock, ms=100)
    diagnostic = manager.snapshot()["guided"]["preload"]
    assert manager.phase == "loading" and diagnostic["valid_continuous_ms"] == 500
    assert diagnostic["last_block_reason"] is None and diagnostic["reset_count"] == 3
    manager.stop()


@pytest.mark.parametrize("block", ["peaks", "reference", "force_range", "force_stale"])
def test_preload_diagnostics_distinguish_blocking_sources(tmp_path, clock, block):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    guided_sample(manager, clock, ms=100)
    guided_sample(manager, clock, ms=100)
    if block == "force_stale":
        frame = force(clock.advance(50), clock.now)
        frame["age_ms"] = 251
        manager.on_force(frame)
        expected = "PX6D 源帧无效、过期"
    else:
        normal = .101 if block == "force_range" else 0
        _, fitted, pair = guided_sample(manager, clock, normal=normal, ms=50, delayed=True)
        if block == "peaks":
            fitted["peaks"][0].update(valid=False, stale=True)
            expected = "九峰无效"
        elif block == "reference":
            fitted["reference_pending"] = True
            expected = "九峰参考尚未锁定"
        else:
            expected = "无载法向力超出 ±0.1 N"
        manager.on_result(fitted, pair)
    diagnostic = manager.snapshot()["guided"]["preload"]
    assert expected in diagnostic["last_block_reason"]
    assert diagnostic["valid_continuous_ms"] == 0 and diagnostic["reset_count"] == 1
    assert manager.phase == "preload" and not diagnostic["timed_out"]
    manager.stop()


def test_preload_watchdog_no_source_frames_aborts_only_trial_without_fake_progress(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    before_counts = deepcopy(manager.counts)
    frozen = manager.snapshot()["guided"]["preload"]
    clock.advance(7999)
    for _ in range(10):
        assert manager.snapshot()["guided"]["preload"] == frozen
    assert manager.trial_id == "T0001" and manager.phase == "preload"
    assert manager.watchdog() is False
    diagnostic = manager.snapshot()["guided"]["preload"]
    assert diagnostic["elapsed_ms"] == 7999 and diagnostic["valid_continuous_ms"] == 0
    assert "等待新的 PX6D 源帧" in diagnostic["last_block_reason"]
    assert diagnostic["reset_count"] == 0
    clock.advance(1)
    assert manager.watchdog(clock.now) is True
    snapshot = manager.snapshot()
    assert snapshot["running"] and snapshot["status"] == "recording" and snapshot["error"] is None
    assert snapshot["trial_id"] is None and snapshot["phase"] == "idle"
    assert snapshot["guided"]["step"] == "retry" and snapshot["guided"]["point_accepted"] == 0
    assert snapshot["guided"]["point_attempts"] == 1
    assert snapshot["guided"]["preload"]["timed_out"] is True
    assert snapshot["baseline"] == {"status": "required", "ready": False}
    quality = snapshot["guided"]["last_quality"]
    assert not quality["accepted"] and any("8 秒" in reason for reason in quality["reasons"])
    assert quality["metrics"]["preload_diagnostics"] == snapshot["guided"]["preload"]
    assert manager.counts["trials_aborted"] == 1
    for kind in ("raw_spectra", "raw_force", "results"):
        assert manager.counts[kind] == before_counts[kind]
    assert manager.watchdog(clock.advance(1000)) is False
    assert manager.counts["trials_aborted"] == 1
    with pytest.raises(ValueError, match="新的明确无载"):
        manager.trial("start", context(clock.now, 2))
    ready(manager, clock, version=3)
    assert manager.snapshot()["guided"]["preload"] is None
    manager.trial("start", context(clock.now, 3))
    assert manager.snapshot()["guided"]["preload"]["elapsed_ms"] == 0
    manager.stop()
    manifest = manager.get_session(manager.session_id)
    first = manifest["final"]["trials"][0]
    assert first["status"] == "aborted" and first["bad"] and first["stop_reason"] == "preload_timeout"
    timeout_events = [r for r in rows(manager, "events") if r["event"] == "guided_preload_timeout"]
    assert len(timeout_events) == 1 and timeout_events[0]["preload"]["timed_out"]


def test_preload_force_expiry_watchdog_resets_run_and_timeout_preserves_raw(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    guided_sample(manager, clock, ms=100)
    force_frame, fitted, pair = guided_sample(manager, clock, ms=100)
    assert manager.snapshot()["guided"]["preload"]["valid_continuous_ms"] == 100
    clock.advance(251)
    assert not manager.watchdog()
    diagnostic = manager.snapshot()["guided"]["preload"]
    assert "PX6D 源帧已过期" in diagnostic["last_block_reason"]
    assert diagnostic["valid_continuous_ms"] == 0 and diagnostic["reset_count"] == 1
    for _ in range(10):
        manager.on_force(deepcopy(force_frame))
        manager.on_result(deepcopy(fitted), deepcopy(pair))
        manager.watchdog()
    assert manager.snapshot()["guided"]["preload"]["reset_count"] == 1
    assert manager.snapshot()["guided"]["preload"]["valid_continuous_ms"] == 0
    clock.advance(8000)
    late = result(clock.now, frame_id=5555)
    manager.on_result(late, {"valid": False, "reason": "仍无可信六轴配对"})
    assert manager.trial_id is None and manager.snapshot()["guided"]["step"] == "retry"
    manager.stop()
    assert len(rows(manager, "raw_spectra")) == 2
    assert any(r["frame_id"] == 5555 for r in rows(manager, "results"))
    assert not any(r["event"] == "guided_loading_ready" for r in rows(manager, "events"))


def test_watchdog_does_not_timeout_legacy_or_an_already_loading_round(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    guided_preload(manager, clock)
    diagnostic = manager.snapshot()["guided"]["preload"]
    clock.advance(9000)
    assert manager.watchdog() is False
    assert manager.phase == "loading" and manager.snapshot()["guided"]["preload"] == diagnostic
    manager.stop()
    legacy = ExperimentManager(tmp_path)
    legacy.start(settings(), context(clock.now))
    ready(legacy, clock)
    legacy.trial("start", context(clock.now, 2))
    clock.advance(9000)
    assert legacy.watchdog() is False and legacy.phase == "preload"
    assert legacy.snapshot()["guided"] == {"enabled": False}
    legacy.stop()


def test_preload_timeout_after_reset_then_one_valid_frame_preserves_last_reset_reason(tmp_path, clock):
    manager = guided_manager(tmp_path, clock)
    manager.select_point("P01")
    ready(manager, clock)
    manager.trial("start", context(clock.now, 2))
    assert manager.snapshot()["guided"]["preload"]["last_reset_reason"] is None
    guided_sample(manager, clock, ms=100)
    _, fitted, pair = guided_sample(manager, clock, ms=100, delayed=True)
    pair.update(valid=False, reason="测试：插值两端过远")
    manager.on_result(fitted, pair)
    reason = manager.snapshot()["guided"]["preload"]["last_reset_reason"]
    assert "同步配对无效" in reason and "插值两端过远" in reason
    # Keep receiving force while no new valid spectrum can complete a run.
    for _ in range(38):
        stamp = clock.advance(200)
        manager.on_force(force(stamp, stamp))
    guided_sample(manager, clock, ms=100)
    diagnostic = manager.snapshot()["guided"]["preload"]
    assert diagnostic["elapsed_ms"] == 7900 and diagnostic["valid_continuous_ms"] == 0
    assert diagnostic["last_block_reason"] is None and diagnostic["last_reset_reason"] == reason
    clock.advance(100)
    for _ in range(5):
        assert manager.snapshot()["guided"]["preload"] == diagnostic
    assert manager.watchdog() is True
    final = manager.snapshot()["guided"]["preload"]
    assert final["timed_out"] and final["valid_continuous_ms"] == 0
    assert final["last_block_reason"] == "连续有效配对尚不足 500 ms；最近重置原因：" + reason
    assert final["last_reset_reason"] == reason
    assert manager.snapshot()["guided"]["step"] == "retry"
    manager.stop()
