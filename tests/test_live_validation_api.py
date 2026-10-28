"""Synthetic live-shaped HTTP/service integration; never real validation data."""
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from functools import partial
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from ninefbg.inversion import _digest
from ninefbg.server import Handler, LocalServer
from ninefbg.service import AcquisitionService
from ninefbg.validation import ValidationController
from test_live_pilot import load_pilot_archive
from test_pilot_controller import archive_bytes


AXES = ["Fx", "Fy", "Fz", "Mx", "My", "Mz"]
UNITS = ["N"]*3 + ["N·m"]*3


class SyntheticTracker:
    def __init__(self, state):
        self.state, self.begins = state, 0
    def end_press(self):
        self.state["reference_frozen"] = self.state["press_active"] = False
    def request_reference(self, unloaded_confirmed):
        assert unloaded_confirmed is True
        self.state["reference_pending"] = True
    def begin_press(self):
        self.begins += 1
        self.state["reference_frozen"] = self.state["press_active"] = True
    def status(self):
        return deepcopy(self.state)


@pytest.fixture
def live_api(load_pilot_archive, monkeypatch):
    archive = load_pilot_archive
    clock = SimpleNamespace(now=10_000_000_000)
    monkeypatch.setattr("ninefbg.service.time.perf_counter_ns", lambda: clock.now)
    service = AcquisitionService(Path(__file__).resolve().parents[1], archive["root"])
    service.running, service.mode = True, "hardware"
    model = archive["model"]
    tracker = {"algorithm_version": model["algorithm_version"], "config_version": model["config_version"],
               "stream_epoch": "optical-live", "reference_version": 1, "reference_pending": False,
               "display_stale": False, "display_age_ms": 10, "reference_frozen": False,
               "frame_id": 1, "host_rx_monotonic_ns": clock.now-10_000_000,
               "peaks": [{"id": f"FBG{i}", "valid": True, "stale": False, "drift_nm": 0.0} for i in range(1, 10)]}
    force = {"source_kind": "hardware", "frame_id": 99, "stream_epoch": "force-live", "stale": False,
             "age_ms": 10, "force_values": [0]*6, "axes": AXES, "units": UNITS}
    state = {"tracker": tracker, "force": force, "sync": {}, "force_connected": True,
             "experiment": {"running": False, "point_id": "P25", "baseline": {"ready": False}}}
    service.tracker = SyntheticTracker(tracker)
    service.synchronizer = SimpleNamespace(frames=deque())
    def snapshot():
        analysis = service.analysis.snapshot(state["tracker"], state["force"], state["sync"], state["experiment"], source_kind=service.mode)
        return deepcopy({"running": service.running, "mode": service.mode,
                         **state, "frame": {"stream_epoch": tracker["stream_epoch"]}, "analysis": analysis,
                         "validation": service.validation.snapshot(analysis.get("model_id"), analysis.get("model_sha256"))})
    monkeypatch.setattr(service, "snapshot", snapshot)
    return SimpleNamespace(service=service, state=state, archive=archive, clock=clock)


@contextmanager
def http_service(service):
    server = LocalServer(("127.0.0.1", 0), partial(Handler, service=service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def post(url, route, body=None, *, error=None):
    request = urllib.request.Request(url+route, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"})
    if error:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 400
        detail = json.load(caught.value)["error"]
        assert error in detail
        return detail
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def saved_events(fixture):
    return [json.loads(line) for path in (fixture.archive["root"] / "validation_sessions").glob("*/records.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


def authorize_and_press(fixture, url, *, with_force=True):
    """Twelve distinct unloaded PX frames then the thirteenth comparison frame."""
    service, state, clock = fixture.service, fixture.state, fixture.clock
    previous_begins = service.tracker.begins
    state["force_connected"] = with_force
    if not with_force:
        state["force"] = None
    post(url, "/api/analysis/reference", {"unloaded_confirmed": True})
    assert service.analysis.test_reference["pending"]
    requested = service.analysis.test_reference["requested_ns"]
    clock.now = requested+10_000_000
    tracker = state["tracker"]
    tracker.update(reference_version=tracker["reference_version"]+1, reference_pending=False, host_rx_monotonic_ns=clock.now)
    with service.tracker_lock:
        service._freeze_independent_reference(tracker, {"stream_epoch": tracker["stream_epoch"], "host_rx_monotonic_ns": clock.now})
    assert service.tracker.begins == previous_begins+1 and tracker["reference_frozen"]
    service.snapshot()
    assert service.analysis.test_reference["ready"]
    if with_force:
        for index in range(12):
            clock.now += 40_000_000
            frame = {"frame_id": 100+index, "stream_epoch": "force-live", "source_kind": "hardware",
                     "time_quality": "host_rx_approx", "host_rx_monotonic_ns": clock.now,
                     "force_values": [0]*6, "axes": AXES, "units": UNITS, "stale": False, "age_ms": 0}
            state["force"] = frame
            service.analysis.on_force(frame)
        assert service.analysis.truth_zero["ready"]
    clock.now += 30_000_000
    stamp = clock.now-20_000_000
    model = fixture.archive["model"]
    slope, origin = model["templates"][0]["slope_pm_per_N"], model["baseline_context"]["origin_pm"]
    tracker.update(frame_id=13, host_rx_monotonic_ns=stamp, display_age_ms=20)
    for index, peak in enumerate(tracker["peaks"]):
        peak["drift_nm"] = (origin[index]+slope[index]*1.5)/1000
    if with_force:
        frames = [{"frame_id": 201+index, "stream_epoch": "force-live", "source_kind": "hardware",
                   "time_quality": "host_rx_approx", "host_rx_monotonic_ns": stamp+offset,
                   "force_values": [0, 0, value, 0, 0, 0], "axes": AXES, "units": UNITS}
                  for index, (offset, value) in enumerate(((-10_000_000, -1.2), (10_000_000, -1.6)))]
        service.synchronizer.frames = deque(frames)
        state["force"] = {**deepcopy(frames[1]), "stale": False, "age_ms": 10}
        state["sync"] = {"valid": True, "matched": True, "method": "linear_receive_time", "bracket_ms": 20,
                         "spectrum_frame_id": 13, "spectrum_stream_epoch": "optical-live",
                         "force_stream_epoch": "force-live", "force_frame_ids": [201, 202],
                         "force_values": [0, 0, -1.4, 0, 0, 0]}
    return service.snapshot()["analysis"]["prediction"]


def test_http_load_and_thirteen_frame_live_prediction_do_not_use_operator_point_or_force(live_api):
    fixture = live_api
    with http_service(fixture.service) as url:
        loaded = post(url, "/api/analysis/load", {"model_id": "Pfixture", "force_N": 999, "actual_point_id": "P25"})
        assert loaded["model_kind"] == "pilot"
        assert loaded["analysis"]["inference_active"] is True
        before = fixture.service.snapshot()["analysis"]["prediction"]
        assert before["status"] == "无效" and before["force_N"] is None
        result = authorize_and_press(fixture, url)
        assert result["status"] == "有效估计" and result["point_id"] == "P01"
        assert result["force_N"] == pytest.approx(1.5, abs=1e-9)
        assert result["raw_force_N"] == pytest.approx(1.5, abs=1e-9)
        assert result["truth_force_N"] == pytest.approx(1.4)
        assert result["error_N"] == pytest.approx(.1)
        fixture.state["experiment"]["point_id"] = "P21"
        fixture.state["force"]["force_values"] = [999]*6
        assert fixture.service.snapshot()["analysis"]["prediction"] == result
    assert not (fixture.archive["root"] / "validation_sessions").exists()


@pytest.mark.parametrize("case", ["stale", "epoch", "reference", "pending", "config", "invalid_peak", "replay"])
def test_live_pilot_invalidates_changed_reference_or_stale_inputs(live_api, case):
    fixture = live_api
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        tracker = fixture.state["tracker"]
        if case == "stale": tracker["display_stale"] = True
        elif case == "epoch": tracker["stream_epoch"] = "new-optical-epoch"
        elif case == "reference": tracker["reference_version"] += 1
        elif case == "pending": tracker["reference_pending"] = True
        elif case == "config": tracker["config_version"] = "changed"
        elif case == "invalid_peak": tracker["peaks"][0]["valid"] = False
        elif case == "replay": fixture.service.mode = "replay"
        prediction = fixture.service.snapshot()["analysis"]["prediction"]
        assert prediction["status"] == "无效" and prediction["force_N"] is None
        post(url, "/api/validation/freeze", error="同步" if case != "replay" else "真实设备")
    assert not (fixture.archive["root"] / "validation_sessions").exists()


def test_live_fbg_only_predicts_but_cannot_create_px_comparison(live_api):
    fixture = live_api
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        prediction = authorize_and_press(fixture, url, with_force=False)
        assert prediction["status"] == "有效估计" and prediction["force_N"] == pytest.approx(1.5)
        assert prediction["truth_force_N"] is None and prediction["error_N"] is None
        post(url, "/api/validation/freeze", error="同步")
    assert not (fixture.archive["root"] / "validation_sessions").exists()


@pytest.mark.parametrize("case", ["disconnected", "stale", "epoch", "unmatched", "unloaded", "opposite_force"])
def test_fresh_optical_prediction_cannot_freeze_without_a_real_press_pair(live_api, case):
    fixture = live_api
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        if case == "disconnected":
            fixture.state["force"] = None
        elif case == "stale":
            fixture.state["force"]["stale"] = True
        elif case == "epoch":
            fixture.state["force"]["stream_epoch"] = "replacement-force-epoch"
        elif case == "unmatched":
            fixture.state["sync"]["matched"] = False
        else:
            value = 0.0 if case == "unloaded" else 1.4
            fixture.state["sync"]["force_values"][2] = value
            for frame in fixture.service.synchronizer.frames:
                frame["force_values"][2] = value
        prediction = fixture.service.snapshot()["analysis"]["prediction"]
        assert prediction["status"] == "有效估计" and prediction["force_N"] == pytest.approx(1.5)
        assert prediction["comparison_ready"] is False
        post(url, "/api/validation/freeze", error="同步")
    assert fixture.service.validation.snapshot()["stats"]["judged_total"] == 0
    assert not (fixture.archive["root"] / "validation_sessions").exists()


def rejected_optical_features(fixture, status):
    model = fixture.archive["model"]
    origin = model["baseline_context"]["origin_pm"]
    first, second = [item["slope_pm_per_N"] for item in model["templates"][:2]]
    if status == "超范围":
        values = [zero+4*slope for zero, slope in zip(origin, first)]
    elif status == "定位不确定":
        values = [zero+.75*(left+right) for zero, left, right in zip(origin, first, second)]
    else:
        assert status == "无接触"
        values = origin
    for peak, value in zip(fixture.state["tracker"]["peaks"], values):
        peak["drift_nm"] = value/1000


@pytest.mark.parametrize("status", ["定位不确定", "超范围", "无接触"])
def test_a_rejected_estimate_counts_as_failure_without_fabricating_a_force_error(live_api, status):
    fixture = live_api
    archived = archive_bytes(fixture.archive["session"])
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        rejected_optical_features(fixture, status)
        prediction = fixture.service.snapshot()["analysis"]["prediction"]
        assert prediction["status"] == status and prediction["point_id"] is None
        assert prediction["comparison_ready"] is True
        assert prediction["truth_force_N"] == pytest.approx(1.4)
        assert prediction["error_N"] is None
        sample = post(url, "/api/validation/freeze", {"point_id": "P01", "force_N": 1.4, "error_N": 0})["sample"]
        assert sample["valid"] is sample["comparison_ready"] is True
        assert sample["inference_valid"] is False
        assert sample["prediction"]["point_id"] is sample["prediction"]["force_N"] is sample["prediction"]["error_N"] is None
        assert sample["absolute_error_N"] is None
        assert sample["evidence"]["prediction"] == prediction, "Keep every original rejection/candidate value in immutable evidence"
        for position, force in ((True, False), (False, True), (True, True)):
            post(url, "/api/validation/judge", {"sample_id": sample["sample_id"], "position_correct": position,
                                                "force_correct": force, "actual_point_id": "P01"}, error="拒识")
        judgment = {"sample_id": sample["sample_id"], "position_correct": False, "force_correct": False, "actual_point_id": "P01"}
        stats = post(url, "/api/validation/judge", judgment)["validation"]["stats"]
        assert stats["judged_total"] == stats["inference_rejected"] == 1
        assert stats["position_rate"] == stats["force_rate"] == stats["joint_rate"] == 0
        assert stats["force_error_count"] == 0 and stats["mae_N"] is stats["rmse_N"] is None
        assert post(url, "/api/validation/judge", judgment)["validation"]["stats"]["judged_total"] == 1
        assert len(saved_events(fixture)) == 2

        # Next genuinely distinct frame has a valid 0.1 N error. The earlier
        # rejected estimate remains in classification rates, not error means.
        fixture.clock.now += 40_000_000
        stamp = fixture.clock.now-20_000_000
        tracker, model = fixture.state["tracker"], fixture.archive["model"]
        tracker.update(frame_id=14, host_rx_monotonic_ns=stamp)
        for peak, origin, slope in zip(tracker["peaks"], model["baseline_context"]["origin_pm"], model["templates"][0]["slope_pm_per_N"]):
            peak["drift_nm"] = (origin+1.5*slope)/1000
        frames = list(fixture.service.synchronizer.frames)
        for index, frame in enumerate(frames):
            frame.update(frame_id=203+index, host_rx_monotonic_ns=stamp+(-10_000_000 if index == 0 else 10_000_000))
        fixture.state["force"] = {**deepcopy(frames[1]), "stale": False, "age_ms": 10}
        fixture.state["sync"].update(spectrum_frame_id=14, force_frame_ids=[203, 204])
        valid_sample = post(url, "/api/validation/freeze")["sample"]
        assert valid_sample["inference_valid"] is True
        assert valid_sample["absolute_error_N"] == pytest.approx(.1)
        stats = post(url, "/api/validation/judge", {"sample_id": valid_sample["sample_id"], "position_correct": True,
                                                   "force_correct": True, "actual_point_id": "P01"})["validation"]["stats"]
        assert stats["judged_total"] == 2 and stats["inference_rejected"] == 1
        assert stats["position_rate"] == stats["force_rate"] == stats["joint_rate"] == .5
        assert stats["force_error_count"] == 1
        assert stats["mae_N"] == stats["rmse_N"] == pytest.approx(.1)
    recovered = ValidationController(fixture.archive["root"])
    assert recovered.error is None
    recovered.configure(fixture.archive["model"]["model_sha256"], "Pfixture")
    assert recovered.snapshot()["stats"] == stats
    assert recovered.samples[sample["sample_id"]]["evidence"] == sample["evidence"]
    assert len(saved_events(fixture)) == 4
    assert archive_bytes(fixture.archive["session"]) == archived


def test_freeze_keeps_same_frame_raw_pair_immutable_and_negative_judgment_counts_once(live_api):
    fixture = live_api
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        frozen = post(url, "/api/validation/freeze", {"force_N": 999, "truth_force_N": 999})["sample"]
        assert frozen["prediction"]["point_id"] == "P01" and frozen["prediction"]["force_N"] == pytest.approx(1.5)
        evidence = frozen["evidence"]
        assert evidence["frame_id"] == 13 and evidence["sync"]["force_frame_ids"] == [201, 202]
        assert [row["force_values"][2] for row in evidence["force_source_frames"]] == [-1.2, -1.6]
        assert evidence["sync"]["force_values"][2] == -1.4
        assert "actual_point_id" not in evidence
        assert frozen["target_point_id"] is frozen["actual_point_id"] is None
        fixture.state["tracker"]["peaks"][0]["drift_nm"] = 888
        fixture.service.synchronizer.frames[0]["force_values"][2] = 888
        assert fixture.service.validation.snapshot()["pending_sample"]["evidence"] == evidence
        judgment = {"sample_id": frozen["sample_id"], "position_correct": False, "force_correct": False,
                    "actual_point_id": "P25", "force_N": 999, "error_N": 0}
        saved = post(url, "/api/validation/judge", judgment)["validation"]
        assert saved["stats"]["judged_total"] == 1
        assert saved["stats"]["position_rate"] == saved["stats"]["force_rate"] == saved["stats"]["joint_rate"] == 0
        assert saved["stats"]["mae_N"] == pytest.approx(.1)
        assert post(url, "/api/validation/judge", judgment)["validation"]["stats"]["judged_total"] == 1
        assert len(saved_events(fixture)) == 2
        post(url, "/api/validation/judge", {**judgment, "force_correct": True}, error="改判")
        post(url, "/api/validation/discard", {"sample_id": frozen["sample_id"]}, error="分母")
    events = saved_events(fixture)
    assert events[0]["sample"]["evidence"] == evidence
    assert events[1]["judgment"]["actual_point_id"] == "P25"


def test_frozen_target_survives_restart_and_cannot_be_changed_at_judgment(live_api):
    fixture = live_api
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        prediction = authorize_and_press(fixture, url)
        sample = post(url, "/api/validation/freeze", {"actual_point_id": "P05"})["sample"]
        assert sample["target_point_id"] == sample["actual_point_id"] == "P05"
        assert sample["evidence"]["target_point_id"] == "P05"
        assert sample["prediction"] == prediction
        assert sample["prediction"]["point_id"] == "P01", "The operator's target must not become the prediction"
        recovered = ValidationController(fixture.archive["root"])
        assert recovered.error is None
        recovered.configure(fixture.archive["model"]["model_sha256"], "Pfixture")
        assert recovered.snapshot()["pending_sample"] == sample
        post(url, "/api/validation/judge", {"sample_id": sample["sample_id"], "position_correct": False,
                                            "force_correct": False, "actual_point_id": "P01"}, error="目标")
        post(url, "/api/validation/judge", {"sample_id": sample["sample_id"], "position_correct": True,
                                            "force_correct": False}, error="实际点位")
        assert fixture.service.validation.snapshot()["stats"]["judged_total"] == 0
        saved = post(url, "/api/validation/judge", {"sample_id": sample["sample_id"], "position_correct": False,
                                                   "force_correct": False})["validation"]
        assert saved["stats"]["judged_total"] == 1 and saved["stats"]["joint_rate"] == 0
    recovered = ValidationController(fixture.archive["root"])
    assert recovered.error is None
    recovered.configure(fixture.archive["model"]["model_sha256"], "Pfixture")
    assert recovered.snapshot()["stats"] == saved["stats"]
    assert recovered.samples[sample["sample_id"]]["target_point_id"] == "P05"
    assert recovered.judgments[sample["sample_id"]]["actual_point_id"] == "P05"
    assert saved_events(fixture)[1]["judgment"]["actual_point_id"] == "P05"


def test_target_selection_never_enters_prediction_and_same_frame_cannot_be_relabeled(live_api, monkeypatch):
    from ninefbg import pilot
    fixture = live_api
    calls = []
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        expected_prediction = authorize_and_press(fixture, url)
        original_predict = pilot.predict_pilot
        def observe_predict(model, x_pm, quality_context=None):
            assert len(x_pm) == 9
            assert set(quality_context) == {"source_kind", "algorithm_version", "config_version"}
            calls.append(deepcopy((x_pm, quality_context, model["model_sha256"])))
            return original_predict(model, x_pm, quality_context)
        monkeypatch.setattr(pilot, "predict_pilot", observe_predict)

        first = post(url, "/api/validation/freeze", {"actual_point_id": "P05"})["sample"]
        assert first["target_point_id"] == "P05" and first["prediction"] == expected_prediction
        post(url, "/api/validation/discard", {"sample_id": first["sample_id"]})
        post(url, "/api/validation/freeze", {"actual_point_id": "P01"}, error="目标点位")
        assert len(saved_events(fixture)) == 2, "Discard cannot permit a second evaluation of the same source frame"
        assert fixture.service.validation.snapshot()["stats"]["judged_total"] == 0

        # A new optical/raw-force pair keeps exactly the same measured inputs.
        # Only source identities and the human target change.
        fixture.clock.now += 40_000_000
        stamp = fixture.clock.now-20_000_000
        fixture.state["tracker"].update(frame_id=14, host_rx_monotonic_ns=stamp)
        frames = deepcopy(list(fixture.service.synchronizer.frames))
        for index, frame in enumerate(frames):
            frame.update(frame_id=203+index, host_rx_monotonic_ns=stamp+(-10_000_000 if index == 0 else 10_000_000))
        fixture.service.synchronizer.frames = deque(frames)
        fixture.state["force"] = {**deepcopy(frames[1]), "stale": False, "age_ms": 10}
        fixture.state["sync"].update(spectrum_frame_id=14, force_frame_ids=[203, 204])
        second = post(url, "/api/validation/freeze", {"actual_point_id": "P01"})["sample"]
        assert second["sample_id"] != first["sample_id"]
        assert second["target_point_id"] == second["actual_point_id"] == "P01"
        for key in ("point_id", "force_N", "status", "best", "second", "candidates", "truth_force_N", "error_N", "x_pm"):
            assert first["prediction"][key] == second["prediction"][key]
        assert first["prediction"]["prediction_context"]["frame_id"] == 13
        assert second["prediction"]["prediction_context"]["frame_id"] == 14
        assert len(calls) >= 3 and all(call == calls[0] for call in calls)
        assert len(saved_events(fixture)) == 3
        assert fixture.service.validation.snapshot()["stats"]["judged_total"] == 0


@pytest.mark.parametrize("target", ["P00", "P26", "p01", "P1", "", 12, True, {}, []])
def test_http_freeze_rejects_invalid_target_without_creating_validation_files(live_api, target):
    fixture = live_api
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        before = authorize_and_press(fixture, url)
        post(url, "/api/validation/freeze", {"actual_point_id": target}, error="点位")
        assert fixture.service.snapshot()["analysis"]["prediction"] == before
    assert not (fixture.archive["root"] / "validation_sessions").exists()


def test_discard_resolves_pending_without_counting_or_rewriting_frozen_values(live_api):
    fixture = live_api
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        sample = post(url, "/api/validation/freeze")["sample"]
        assert post(url, "/api/validation/freeze")["sample"]["sample_id"] == sample["sample_id"]
        discarded = post(url, "/api/validation/discard", {"sample_id": sample["sample_id"]})["validation"]
        assert discarded["pending_sample"] is None and discarded["stats"]["judged_total"] == 0
        post(url, "/api/validation/discard", {"sample_id": sample["sample_id"]})
        assert len(saved_events(fixture)) == 2
        post(url, "/api/validation/judge", {"sample_id": sample["sample_id"], "position_correct": False, "force_correct": False}, error="放弃")


def test_http_fake_force_cannot_turn_a_large_frozen_error_into_a_correct_judgment(live_api):
    fixture = live_api
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        fixture.service.synchronizer.frames[0]["force_values"][2] = -.6
        fixture.service.synchronizer.frames[1]["force_values"][2] = -1.0
        fixture.state["sync"]["force_values"][2] = -.8
        frozen = post(url, "/api/validation/freeze", {"truth_force_N": 1.5, "error_N": 0})["sample"]
        assert frozen["prediction"]["truth_force_N"] == pytest.approx(.8)
        assert frozen["absolute_error_N"] == pytest.approx(.7)
        forged = {"sample_id": frozen["sample_id"], "position_correct": True, "force_correct": True,
                  "actual_point_id": "P01", "force_N": 1.5, "truth_force_N": 1.5, "error_N": 0}
        post(url, "/api/validation/judge", forged, error="0.3 N")
        post(url, "/api/validation/judge", {**forged, "force_correct": False, "actual_point_id": "P25"}, error="实际点位")
        assert fixture.service.validation.snapshot()["stats"]["judged_total"] == 0
        saved = post(url, "/api/validation/judge", {**forged, "force_correct": False})["validation"]
        assert saved["stats"]["judged_total"] == 1 and saved["stats"]["force_rate"] == 0
        assert saved["stats"]["mae_N"] == pytest.approx(.7)


def test_no_hardware_reference_and_freeze_refused_without_validation_directory(live_api):
    fixture = live_api
    fixture.service.running = False
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        post(url, "/api/analysis/reference", {"unloaded_confirmed": True}, error="真实光谱")
        post(url, "/api/validation/freeze", error="真实设备")
    stats = fixture.service.validation.snapshot()["stats"]
    assert stats["judged_total"] == 0 and stats["joint_rate"] is None
    assert not (fixture.archive["root"] / "validation_sessions").exists()


@pytest.mark.parametrize("case", ["missing_raw", "raw_disagrees", "wrong_spectrum", "old_clock"])
def test_service_freeze_rejects_untraceable_or_cross_frame_pairing(live_api, case):
    fixture = live_api
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        if case == "missing_raw": fixture.service.synchronizer.frames.clear()
        elif case == "raw_disagrees": fixture.service.synchronizer.frames[0]["force_values"][2] = -3
        elif case == "wrong_spectrum": fixture.state["sync"]["spectrum_frame_id"] = 12
        elif case == "old_clock": fixture.clock.now += 2_000_000_000
        post(url, "/api/validation/freeze", error={"missing_raw": "数量", "raw_disagrees": "重算", "wrong_spectrum": "同步", "old_clock": "采集时间"}[case])
    assert fixture.service.validation.snapshot()["stats"]["judged_total"] == 0
    assert not (fixture.archive["root"] / "validation_sessions").exists()


def test_model_switch_isolates_saved_counts_and_reloading_requires_new_reference(live_api):
    fixture = live_api
    model = deepcopy(fixture.archive["model"])
    model["created_utc"] = "2026-10-10T08:02:00+00:00"
    model["model_sha256"] = _digest({key: value for key, value in model.items() if key != "model_sha256"})
    (fixture.archive["root"] / "pilot_models" / "Psecond.json").write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    with http_service(fixture.service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        sample = post(url, "/api/validation/freeze")["sample"]
        judgment = {"sample_id": sample["sample_id"], "position_correct": False, "force_correct": False}
        post(url, "/api/validation/judge", judgment)
        post(url, "/api/analysis/load", {"model_id": "Psecond"})
        assert fixture.service.validation.snapshot()["stats"]["judged_total"] == 0
        assert not fixture.service.analysis.truth_zero["ready"] and not fixture.service.analysis.test_reference["ready"]
        assert fixture.state["tracker"]["reference_frozen"] is False
        post(url, "/api/validation/judge", judgment, error="对应的模型")
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        assert fixture.service.validation.snapshot()["stats"]["judged_total"] == 1
        assert fixture.service.snapshot()["analysis"]["prediction"]["status"] == "无效"
        post(url, "/api/validation/freeze", error="同步")


def test_http_stop_protects_pending_judgment_preserves_report_and_training_and_requires_reload(live_api, monkeypatch):
    fixture, service = live_api, live_api.service
    with http_service(service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(fixture, url)
        sample = post(url, "/api/validation/freeze", {"actual_point_id": "P01"})["sample"]
        before = deepcopy(service.analysis.test_reference)
        post(url, "/api/analysis/stop", error="先保存本次判断")
        assert service.analysis.inference_active and service.analysis.test_reference == before
        assert service.validation.snapshot()["pending_sample"]["sample_id"] == sample["sample_id"]
        post(url, "/api/validation/judge", {"sample_id": sample["sample_id"],
                                            "position_correct": True, "force_correct": True})
        report = post(url, "/api/validation/finish")["report"]
        assert service.analysis.inference_active, "Saving judgment statistics does not end inference"
        report_bytes = Path(report["path"]).read_bytes()
        records_before = saved_events(fixture)
        service.analysis.busy = True
        service.analysis.training.update(busy=True, status="running", method="linear")
        training_before = deepcopy(service.analysis.training)
        force_stop_before = service.force_stop_event.is_set()
        from ninefbg.pilot import predict_pilot
        monkeypatch.setattr("ninefbg.pilot.predict_pilot", lambda *args: pytest.fail("已结束推理仍运行了网络"))
        stopped = post(url, "/api/analysis/stop")
        analysis = stopped["analysis"]
        assert not analysis["inference_active"] and analysis["model_id"] == "Pfixture"
        assert analysis["prediction"]["status"] == "已结束"
        assert analysis["prediction"]["point_id"] is analysis["prediction"]["force_N"] is None
        assert analysis["prediction"]["wrench_pred"] is analysis["prediction"]["truth_force_N"] is None
        assert not analysis["test_reference"]["ready"] and not analysis["test_reference"]["pending"]
        assert not analysis["truth_zero"]["ready"] and not analysis["truth_zero"]["pending"]
        assert analysis["busy"] and analysis["training"] == training_before
        assert service.running and not service.stop_event.is_set()
        assert service.force_stop_event.is_set() == force_stop_before and not service.test_reference_freeze
        tracker_before = deepcopy(fixture.state["tracker"])
        post(url, "/api/analysis/reference", {"unloaded_confirmed": True}, error="重新载入")
        assert fixture.state["tracker"] == tracker_before
        for _ in range(3):
            fixture.state["tracker"]["frame_id"] += 1
            with urllib.request.urlopen(url+"/api/state") as response:
                assert not json.load(response)["analysis"]["inference_active"]
        assert saved_events(fixture) == records_before
        assert Path(report["path"]).read_bytes() == report_bytes
        service.analysis.busy = False
        service.analysis.training.update(busy=False, status="completed")
        loaded = post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        assert loaded["analysis"]["inference_active"] and not loaded["analysis"]["test_reference"]["ready"]
        monkeypatch.setattr("ninefbg.pilot.predict_pilot", predict_pilot)
        assert authorize_and_press(fixture, url)["status"] == "有效估计"


@pytest.mark.parametrize("condition", ["loaded", "reference_pending", "disconnected"])
def test_http_stop_without_judgments_does_not_need_devices_or_ready_reference(live_api, condition):
    fixture, service = live_api, live_api.service
    with http_service(service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        if condition == "reference_pending":
            post(url, "/api/analysis/reference", {"unloaded_confirmed": True})
            assert service.analysis.test_reference["pending"]
        elif condition == "disconnected":
            service.running = False
        post(url, "/api/analysis/stop", {"unexpected": True}, error="不接受额外字段")
        assert service.analysis.inference_active
        if condition == "loaded":
            empty = urllib.request.Request(url+"/api/analysis/stop", data=b"",
                                           headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(empty, timeout=10) as response:
                result = json.load(response)
        else:
            result = post(url, "/api/analysis/stop")
        assert not result["analysis"]["inference_active"]
        assert result["validation"]["stats"]["judged_total"] == 0
        assert result["analysis"]["model_id"] == "Pfixture"
        assert not (fixture.archive["root"] / "validation_sessions").exists()


def test_http_stop_refuses_active_acquisition_and_save_without_changing_reference(live_api, monkeypatch):
    service = live_api.service
    with http_service(service) as url:
        post(url, "/api/analysis/load", {"model_id": "Pfixture"})
        authorize_and_press(live_api, url)
        baseline = {"ready": True, "force_zero": [1]*6, "reference_version": 2}
        monkeypatch.setattr(service.experiment, "snapshot", lambda: {"running": True, "baseline": deepcopy(baseline)})
        before = deepcopy(service.analysis.test_reference)
        tracker_before = deepcopy(live_api.state["tracker"])
        post(url, "/api/analysis/stop", error="先停止并保存采集会话")
        assert service.analysis.inference_active and service.analysis.test_reference == before
        assert live_api.state["tracker"] == tracker_before
        assert service.experiment.snapshot()["baseline"] == baseline
        monkeypatch.setattr(service.experiment, "snapshot", lambda: {"running": False})
        service.stopping = True
        post(url, "/api/analysis/stop", error="正在停止保存")
        assert service.analysis.inference_active
        service.stopping = False
