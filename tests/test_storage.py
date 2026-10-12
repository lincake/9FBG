import json
import hashlib
import time

from ninefbg.storage import SessionWriter


def test_immutable_raw_and_append_only_close(tmp_path):
    writer = SessionWriter(tmp_path, {"mode": "test"})
    frame = {"frame_id": "same", "intensity": [1, 2, 3]}
    writer.submit("raw_spectra", frame)
    frame["intensity"][0] = 999
    writer.submit("events", {"event": "reference_commit", "reference_version": 1})
    writer.submit("events", {"event": "reference_commit", "reference_version": 2})
    writer.close({})
    path = writer.path / "raw_spectra.jsonl"
    assert json.loads(path.read_text(encoding="utf-8"))["intensity"] == [1, 2, 3]
    manifest = json.loads((writer.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is True
    assert manifest["counts"] == {"raw_spectra": 1, "events": 2}
    assert manifest["files"][path.name] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_write_failure_is_visible_and_session_incomplete(tmp_path):
    writer = SessionWriter(tmp_path, {})
    (writer.path / "raw_spectra.jsonl").mkdir()
    writer.submit("raw_spectra", {"frame_id": 7})
    writer.thread.join(timeout=2)
    assert writer.error
    writer.close({})
    manifest = json.loads((writer.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False
