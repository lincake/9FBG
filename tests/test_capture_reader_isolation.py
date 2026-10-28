"""Deterministic reader isolation; every source is synthetic and local.

Blocking a state/report operation reproduces the former shared-lock failure.
Readers must still accept subsequent identities with their original acquisition
times while the report is blocked, rather than hiding or retiming a gap.
"""
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest

from ninefbg.service import AcquisitionService
from test_display_service import fake_hardware
from test_service import until


@pytest.mark.parametrize("component", ["experiment", "analysis", "validation", "diagnostics", "writer"])
def test_slow_state_component_cannot_block_either_raw_reader(tmp_path, fake_hardware, monkeypatch, component):
    optical, forces, _ = fake_hardware
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    entered, release = threading.Event(), threading.Event()
    responses, errors = [], []
    service.connect(record=True, connect_force=True)
    until(lambda: service.latest_force is not None and service.metrics["received"] >= 3)
    emitted = {"raw_spectra": {}, "raw_force": {}}
    for kind, source in (("raw_spectra", optical[0]), ("raw_force", forces[0])):
        read = source.read

        def observe(read=read, kind=kind):
            frame = read()
            emitted[kind][frame["frame_id"]] = deepcopy(frame)
            return frame

        monkeypatch.setattr(source, "read", observe)
    if component == "diagnostics":
        owner, name = optical[0], "diagnostics"
    elif component == "writer":
        owner, name = service.writer, "status"
    else:
        owner, name = getattr(service, component), "snapshot"
    original = getattr(owner, name)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(4), "Synthetic report must be released by the test"
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, blocked)

    def request_state():
        try:
            responses.append(service.snapshot())
        except BaseException as exc:
            errors.append(exc)

    request = threading.Thread(target=request_state)
    request.start()
    try:
        assert entered.wait(2)
        before_optical, before_force = optical[0].number, forces[0].number
        # The state request stays blocked while BOTH real reader loops progress.
        # On the old snapshot implementation the first subsequent source frame
        # waits for service.lock, so each count could increase by at most one.
        until(lambda: optical[0].number >= before_optical + 8 and
              forces[0].number >= before_force + 8, seconds=1.5)
        assert not release.is_set() and request.is_alive()
        assert service.metrics["received"] > before_optical + 2
        assert service.latest_force["frame_id"] > before_force + 2
    finally:
        release.set()
        request.join(3)
        monkeypatch.setattr(owner, name, original)
        service.disconnect()
        service.analysis.close()
    assert not errors and len(responses) == 1
    assert not request.is_alive()
    assert not service.writer.error
    assert service.writer.counts["raw_spectra"] == optical[0].number
    assert service.writer.counts["raw_force"] == forces[0].number
    for kind, count in (("raw_spectra", optical[0].number), ("raw_force", forces[0].number)):
        rows = [json.loads(row) for row in (service.writer.path / (kind + ".jsonl")).read_text(encoding="utf-8").splitlines()]
        assert [row["frame_id"] for row in rows] == list(range(1, count + 1))
        stamps = [row["host_rx_monotonic_ns"] for row in rows]
        assert all(right > left for left, right in zip(stamps, stamps[1:]))
        assert len({row["stream_epoch"] for row in rows}) == 1
        for row in rows:
            source = emitted[kind].get(row["frame_id"])
            if source is not None:
                assert row["host_rx_monotonic_ns"] == source["host_rx_monotonic_ns"]
                assert row["stream_epoch"] == source["stream_epoch"]
                for key in (("intensity", "wavelength_nm") if kind == "raw_spectra" else ("force_values",)):
                    assert row[key] == source[key]


def test_state_reference_and_published_frame_remain_bound_during_concurrent_reference_change(tmp_path, monkeypatch):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    stamp = time.perf_counter_ns()
    service.running, service.mode = True, "hardware"
    tracker = service.tracker
    tracker._reference_version = 1
    service.latest = {"frame": {"frame_id": 15, "host_rx_monotonic_ns": stamp,
                     "stream_epoch": "snapshot-epoch", "source_kind": "hardware"},
                     "tracker": {"reference_version": 1, "frame_id": 15, "stream_epoch": "snapshot-epoch",
                                 "peaks": [{"id": "FBG1", "valid": True, "stale": False}]}}
    original = service.experiment.snapshot

    def change_reference_after_source_copy():
        # Models/reports are built after releasing the source snapshot locks.
        # A new command/reference cannot mutate already copied frame provenance.
        with service.tracker_lock, service.lock:
            tracker._reference_version = 2
            service.latest["frame"]["frame_id"] = 16
        return original()

    monkeypatch.setattr(service.experiment, "snapshot", change_reference_after_source_copy)
    try:
        response = service.snapshot()
        assert response["frame"]["frame_id"] == 15
        assert response["tracker"]["frame_id"] == 15
        assert response["tracker"]["reference_version"] == 1
        response["frame"]["frame_id"] = "edited client copy"
        assert service.latest["frame"]["frame_id"] == 16
    finally:
        service.analysis.close()


def test_state_marks_old_reference_results_stale_without_relabeling_peak_values(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    service.running, service.mode = True, "hardware"
    service.tracker._reference_version = 2
    peak = {"id": "FBG1", "valid": True, "stale": False, "drift_nm": .001,
            "value_reference_version": 1, "value_frame_id": 11}
    service.latest = {"frame": {"frame_id": 11, "host_rx_monotonic_ns": time.perf_counter_ns(),
                     "stream_epoch": "test", "source_kind": "hardware"},
                     "tracker": {"reference_version": 1, "peaks": [deepcopy(peak)]}}
    try:
        response = service.snapshot()
        actual = response["tracker"]["peaks"][0]
        assert response["tracker"]["reference_version"] == 2
        assert response["tracker"]["display_stale"]
        assert not actual["valid"] and actual["stale"]
        assert actual["drift_nm"] == peak["drift_nm"]
        assert actual["value_reference_version"] == 1 and actual["value_frame_id"] == 11
        assert service.latest["tracker"]["peaks"][0] == peak
    finally:
        service.analysis.close()


@pytest.mark.parametrize("delay_component", ["experiment", "analysis"])
def test_slow_state_response_rechecks_force_age_without_retiming_source(tmp_path, monkeypatch, delay_component):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    start = time.perf_counter_ns()
    clock = [start]
    service.running, service.mode = True, "hardware"
    service.force_stop_event.clear()
    service.latest_force = {"frame_id": 31, "stream_epoch": "force-age", "host_rx_monotonic_ns": start,
                            "force_values": [0, 0, 0, 0, 0, 0], "time_quality": "host_rx_approx"}
    service.latest = {"frame": {"frame_id": 41, "stream_epoch": "optical-age", "host_rx_monotonic_ns": start,
                               "source_kind": "hardware"}, "tracker": {"reference_version": 0, "peaks": []}}
    experiment = {"running": True, "baseline": {"ready": True, "force_zero": [0] * 6,
                  "force_epoch": "force-age"}, "settings": {"force_sign": -1, "max_force_N": 4}}
    analysis = {"model_kind": "full", "inference_active": True,
                "truth_zero": {"ready": True, "offsets": [0] * 6},
                "prediction": {"status": "有效估计", "comparison_ready": True, "truth_force_N": 1,
                               "error_N": 0, "truth_context": {"frame_id": 41}}}

    def report(value, name):
        def snapshot(*args, **kwargs):
            if name == delay_component:
                clock[0] += 350_000_000
            return deepcopy(value)
        return snapshot

    monkeypatch.setattr("ninefbg.service.time.perf_counter_ns", lambda: clock[0])
    monkeypatch.setattr(service.experiment, "snapshot", report(experiment, "experiment"))
    monkeypatch.setattr(service.analysis, "snapshot", report(analysis, "analysis"))
    try:
        response = service.snapshot()
        force = response["force"]
        assert force["host_rx_monotonic_ns"] == start and force["frame_id"] == 31
        assert force["age_ms"] == 350 and force["stale"]
        assert not response["experiment"]["live_force"]["valid"]
        prediction = response["analysis"]["prediction"]
        assert prediction["truth_force_N"] is None and prediction["error_N"] is None
        assert not prediction["comparison_ready"] and "truth_context" not in prediction
        assert not response["analysis"]["truth_zero"]["ready"]
        assert not service.latest_force.get("stale"), "Only the copied response is invalidated"
    finally:
        service.analysis.close()


@pytest.mark.parametrize("spectral_age_ms, valid", [(1100, False), (10, True)])
def test_delayed_response_ages_spectral_prediction_by_its_own_source_not_nine_peak_lag(tmp_path, monkeypatch, spectral_age_ms, valid):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    start = time.perf_counter_ns()
    clock = [start]
    service.running, service.mode = True, "hardware"
    source_stamp = start - spectral_age_ms * 1_000_000
    # The nine-peak processing result is already old, while a raw-spectrum model
    # can legitimately have a newer source frame. Their clocks must stay separate.
    service.latest = {"frame": {"frame_id": 1, "host_rx_monotonic_ns": start - 2_000_000_000,
                     "stream_epoch": "optical-age", "source_kind": "hardware"},
                     "tracker": {"reference_version": 0, "peaks": [{"id": "FBG1", "valid": True}]}}

    def snapshot(*args, **kwargs):
        clock[0] += 200_000_000
        return {"model_kind": "spectral_gru", "inference_active": True, "truth_zero": {},
                "prediction": {"valid": True, "point_id": "P01", "status": "有效估计",
                               "host_rx_monotonic_ns": source_stamp, "frame_id": 2,
                               "comparison_ready": True}}

    monkeypatch.setattr("ninefbg.service.time.perf_counter_ns", lambda: clock[0])
    monkeypatch.setattr(service.analysis, "snapshot", snapshot)
    # The comparison implementation is tested independently with exact force
    # source evidence; this fixture isolates expiry during response formatting.
    monkeypatch.setattr(service, "_spectral_comparison", lambda *args: None)
    try:
        response = service.snapshot()
        assert response["tracker"]["display_stale"]
        assert response["tracker"]["peaks"][0]["stale"]
        prediction = response["analysis"]["prediction"]
        assert prediction["host_rx_monotonic_ns"] == source_stamp and prediction["frame_id"] == 2
        assert prediction["age_ms"] == spectral_age_ms + 200
        assert prediction["valid"] is valid
        if not valid:
            assert prediction["point_id"] is None and not prediction["comparison_ready"]
        else:
            assert prediction["point_id"] == "P01", "Nine-peak lag cannot invalidate a newer raw-spectrum prediction"
    finally:
        service.analysis.close()
