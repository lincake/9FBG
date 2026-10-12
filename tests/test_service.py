import json
from pathlib import Path
import time
import numpy as np
import pytest

from ninefbg.service import AcquisitionService
from ninefbg.storage import SessionWriter


def spectra(count=40):
    axis = np.linspace(1500, 1580, 512)
    signal = np.ones(512)*120
    for center in np.linspace(1506, 1574, 9):
        signal += 3000*np.exp(-.5*((axis-center)/.45)**2)
    stamp = time.perf_counter_ns()
    return [{"frame_id": i, "wavelength_nm": axis.tolist(), "intensity": signal.tolist(),
             "host_rx_monotonic_ns": stamp+i*20_000_000, "time_quality": "host_rx_approx",
             "timestamp_source": "host_receive_monotonic", "stream_epoch": "test",
             "device_sequence": None, "preprocessing": {"raw_available": True, "smoothing": False}}
            for i in range(count)]


def until(condition, seconds=5):
    end = time.perf_counter()+seconds
    while time.perf_counter()<end:
        if condition():
            return
        time.sleep(.02)
    raise AssertionError("超时等待服务状态")


def test_replay_reference_press_and_stop_preserve_raw(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    frames = spectra()
    service.connect("replay", frames, {"stable_frames": 3})
    try:
        assert not service.tracker.status()["initialized"]
        with pytest.raises(ValueError):
            service.reference(False)
        service.reference(True)
        until(lambda: service.tracker.status()["initialized"])
        service.press(True)
        with pytest.raises(ValueError):
            service.reference(True)
        service.press(False)
        until(lambda: service.metrics["received"] == len(frames))
    finally:
        service.disconnect()
    assert not service.running
    assert service.frames.empty()
    assert all(not peak["valid"] and peak["stale"] for peak in service.snapshot()["tracker"]["peaks"])
    records = [json.loads(line) for line in (service.writer.path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records)==40
    assert records[0]["host_rx_monotonic_ns"]==frames[0]["host_rx_monotonic_ns"]
    manifest = json.loads((service.writer.path/"manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"]
    assert manifest["last_saved_frame_ids"]["raw_spectra"] == 39
    assert manifest["final"]["algorithm"]["reference_history"][0]["version"] == 1


def test_duplicate_real_identity_still_saved_and_equal_values_kept(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    frames=spectra(10)
    for i,frame in enumerate(frames):
        frame["device_sequence"] = i if i<9 else 8
    service.connect("replay", frames)
    try:
        until(lambda:service.metrics["received"]==10)
    finally:
        service.disconnect()
    assert service.metrics["duplicates"]==1
    assert service.metrics["processed"]==9
    assert service.writer.counts["raw_spectra"]==10
    assert service.metrics["identical_payloads"]==8


def test_new_reference_never_uses_preconfirmation_backlog(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    service.connect("replay", spectra(20), {"stable_frames":3})
    try:
        service.reference(True)
        until(lambda:service.tracker.status()["initialized"])
        history=service.tracker.export_state()["reference_history"][0]
        assert len(history["frame_ids"])==3
        assert service.reference_min_rx_ns>0
    finally:
        service.disconnect()


def test_adapter_backlog_cannot_consume_later_unloaded_confirmation(tmp_path):
    service=AcquisitionService(Path(__file__).resolve().parents[1],tmp_path)
    service.running=True
    service.mode="hardware"
    service.writer=SessionWriter(tmp_path,{})
    service.metrics={"received":0,"duplicates":0,"identical_payloads":0,"max_queue_depth":0}
    service.reference(True)
    cutoff=service.reference_min_rx_ns
    old,new=spectra(2)
    old["host_rx_monotonic_ns"]=cutoff-1
    new["host_rx_monotonic_ns"]=cutoff+1
    service._receive(old)
    assert service.pending_confirmation is True
    service._receive(new)
    assert service.pending_confirmation is False
    service.writer.close({})
    saved=[json.loads(row) for row in (service.writer.path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()]
    assert saved[0]["phase"]=="unknown" and saved[0]["unloaded_confirmed"] is False
    assert saved[1]["phase"]=="unloaded" and saved[1]["unloaded_confirmed"] is True


def test_skipped_preconfirmation_frame_keeps_time_quality_and_ids(tmp_path):
    service=AcquisitionService(Path(__file__).resolve().parents[1],tmp_path)
    service.running=True
    service.mode="hardware"
    service.writer=SessionWriter(tmp_path,{})
    service.metrics={"processed":0}
    service.reference(True)
    frame=spectra(1)[0]
    frame["host_rx_monotonic_ns"]=service.reference_min_rx_ns-1
    service.frames.put((frame,time.perf_counter_ns()))
    service.process_done.set()
    service._process()
    result=service.latest["tracker"]
    assert result["state"]=="reference_waiting"
    for key in ("frame_id","host_rx_monotonic_ns","time_quality","timestamp_source","stream_epoch"):
        assert result[key]==frame[key]
    assert len(result["peaks"])==9 and all(not p["valid"] for p in result["peaks"])
    assert not service.tracker.status()["initialized"]
    service.writer.close({})
