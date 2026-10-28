"""Source-frame scheduling checks. Generated inputs are not hardware evidence."""
from copy import deepcopy
import threading
import time

import pytest

from ninefbg.spectral_runtime import SpectralRuntime


def setup_runtime(predictor=None):
    runtime = SpectralRuntime(predictor=predictor or (lambda *args: ([1.] + [0.]*24, [.2, -.1, 0., .01, -.02, 0.])))
    axis = [1500.+i*.1 for i in range(512)]
    now = time.perf_counter_ns()
    meta = {"wavelength_nm": axis,"preprocessing": {},"trained_point_ids": ["P01"],
            "model_sha256": "a"*64,"model_id":"Sfixture", "window_rule":{"max_gap_ms":120}}
    reference = {"reference_id":"R1","stream_epoch":"optical", "S0":[10.]*512,
                 "wavelength_nm":axis,"preprocessing":{},"effective_ns":now-2_000_000_000}
    runtime.apply(meta, object())
    runtime.set_reference(reference)
    frames = [{"intensity":[11.]*512,"wavelength_nm":axis,"preprocessing":{},"frame_id":i,
               "stream_epoch":"optical","host_rx_monotonic_ns":now-(15-i)*40_000_000,
               "source_kind":"hardware","time_quality":"host_rx_approx"} for i in range(16)]
    return runtime, frames, reference


def wait_result(runtime, count=1):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        result=runtime.snapshot()
        if result["metrics"]["completed"]>=count:
            return result
        time.sleep(.005)
    raise AssertionError(runtime.snapshot())


def test_true_frame_identity_identical_payload_and_snapshot_never_advance():
    runtime, frames, _ = setup_runtime()
    try:
        for frame in frames[:15]: runtime.submit(frame)
        before = deepcopy(runtime.frames)
        for _ in range(20):
            assert not runtime.snapshot()["prediction"]["valid"]
        assert runtime.frames == before
        runtime.submit(frames[-1])
        result=wait_result(runtime)
        assert result["prediction"]["window_frame_ids"] == list(range(16))
        assert result["prediction"]["point_id"]=="P01"
        assert result["prediction"]["wrench_pred"][1]<0
        count=result["metrics"]["completed"]
        for _ in range(20): runtime.snapshot()
        runtime.submit(frames[-1])
        assert runtime.snapshot()["metrics"]["completed"]==count
        assert runtime.snapshot()["metrics"]["duplicate_identities"]==1
    finally: runtime.close()


def test_reference_change_gap_and_reconnect_invalidate_pending_generation():
    entered, release = threading.Event(), threading.Event()
    def predictor(*args):
        entered.set()
        assert release.wait(3)
        return [1.]+[0.]*24, [.2]*6
    runtime, frames, reference=setup_runtime(predictor)
    try:
        for frame in frames: runtime.submit(frame)
        assert entered.wait(2)
        runtime.set_reference({**reference,"reference_id":"R2"})
        release.set()
        time.sleep(.03)
        assert not runtime.snapshot()["prediction"]["valid"]
        assert len(runtime.frames)==0
        later={**frames[-1],"frame_id":16,"host_rx_monotonic_ns":frames[-1]["host_rx_monotonic_ns"]+200_000_000}
        runtime.submit(later)
        assert len(runtime.frames)==1
        runtime.submit({**later,"frame_id":17,"host_rx_monotonic_ns":later["host_rx_monotonic_ns"]+200_000_000})
        assert len(runtime.frames)==1
        runtime.submit({**later,"frame_id":18,"stream_epoch":"new_epoch"})
        assert runtime.snapshot()["reference"] is None
    finally:
        release.set()
        runtime.close()


def test_untrained_prediction_and_config_change_never_show_valid_position():
    runtime, frames, _=setup_runtime(lambda *args: ([0.,1.]+[0.]*23,[.2,0.,0.,0.,0.,0.]))
    try:
        for frame in frames: runtime.submit(frame)
        result=wait_result(runtime)["prediction"]
        assert result["valid"] and result["point_id"] is None and result["reason"]=="候选点位未训练"
        runtime.submit({**frames[-1],"frame_id":16,"preprocessing":{"integration_us_requested":80}})
        assert runtime.snapshot()["reference"] is None
    finally: runtime.close()


def test_stop_cancels_pending_and_inflight_results_until_explicit_reapply_and_new_reference():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def predictor(*args):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return [1.]+[0.]*24, [.2]*6
    runtime, frames, reference = setup_runtime(predictor)
    metadata, model = deepcopy(runtime.metadata), runtime.model
    try:
        for frame in frames:
            runtime.submit(frame)
        assert entered.wait(2)
        runtime.submit({**frames[-1], "frame_id": 16,
                        "host_rx_monotonic_ns": frames[-1]["host_rx_monotonic_ns"]+40_000_000})
        assert runtime.pending is not None
        runtime.stop_inference()
        stopped = runtime.snapshot()
        assert not stopped["inference_active"] and stopped["prediction"]["status"] == "已结束"
        assert stopped["reference"] is None and not stopped["reference_pending"]
        assert runtime.pending is None and not runtime.frames and runtime.completed_window is None
        release.set()
        runtime.set_reference(reference)
        runtime.invalidate("光谱已重连")
        with pytest.raises(ValueError, match="重新载入"):
            runtime.request_reference(True, "optical")
        for frame in frames:
            runtime.submit(frame)
        time.sleep(.03)
        assert calls == [1] and runtime.snapshot()["metrics"]["completed"] == 0
        assert runtime.snapshot()["prediction"]["wrench_pred"] is None
        runtime.apply(metadata, model)
        for frame in frames:
            runtime.submit(frame)
        assert runtime.pending is None and runtime.snapshot()["reference"] is None
        runtime.set_reference({**reference, "reference_id": "R2"})
        for frame in frames:
            runtime.submit({**frame, "frame_id": frame["frame_id"]+100})
        assert wait_result(runtime)["prediction"]["reference_id"] == "R2"
        assert len(calls) == 2
    finally:
        release.set()
        runtime.close()
