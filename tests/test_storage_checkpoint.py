"""A round is durable before its next zero may become effective."""
import hashlib
import json
import os
import threading

from ninefbg.storage import SessionWriter


def test_checkpoint_ack_follows_durable_raw_and_keeps_session_open(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = os.fsync

    def blocked_fsync(descriptor):
        entered.set()
        assert release.wait(3)
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", blocked_fsync)
    writer = SessionWriter(tmp_path, {})
    try:
        writer.submit("raw_spectra", {"frame_id": 1, "intensity": [1, 2, 3]})
        writer.submit("events", {"event": "round_recovery_verified"})
        writer.request_checkpoint("session:T1")
        assert entered.wait(1)
        assert not writer.checkpoint_ready("session:T1")
        # A pending fsync must not stop a device reader from enqueueing frames.
        writer.submit("raw_spectra", {"frame_id": 2, "intensity": [4, 5, 6]})
        assert writer.accepted == 3 and not writer.closed
        release.set()
        writer.queue.join()
        assert writer.checkpoint_ready("session:T1")
        assert writer.status()["last_checkpoint"] == "session:T1"
        assert not (writer.path / "manifest.json").exists()
    finally:
        release.set()
        writer.close({})
    manifest = json.loads((writer.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] and manifest["accepted_records"] == 3
    assert manifest["counts"] == {"raw_spectra": 2, "events": 1}
    raw = writer.path / "raw_spectra.jsonl"
    assert manifest["files"][raw.name] == hashlib.sha256(raw.read_bytes()).hexdigest()


def test_fsync_failure_never_acknowledges_round_checkpoint(tmp_path, monkeypatch):
    def failed_fsync(descriptor):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(os, "fsync", failed_fsync)
    writer = SessionWriter(tmp_path, {})
    writer.submit("raw_force", {"frame_id": 1, "force_values": [0] * 6})
    writer.request_checkpoint("session:T1")
    writer.thread.join(2)
    assert not writer.thread.is_alive() and writer.error
    assert not writer.checkpoint_ready("session:T1")
    writer.close({})
    manifest = json.loads((writer.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False
