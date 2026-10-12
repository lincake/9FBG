from contextlib import contextmanager
from functools import partial
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from ninefbg.server import Handler, LocalServer
from ninefbg.service import AcquisitionService


@contextmanager
def history_server(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    opened = []
    service.history.opener = opened.append
    server = LocalServer(("127.0.0.1", 0), partial(Handler, service=service))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield service, opened, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def request(url, path, body=None, *, error=None):
    req = urllib.request.Request(url + path) if body is None else urllib.request.Request(
        url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    if error:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        assert caught.value.code == 400
        result = json.load(caught.value)
        assert error in result["error"]
        return result
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def archive(tmp_path, session_id):
    path = tmp_path / "calibration_sessions" / session_id
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(json.dumps({"kind": "9fbg_calibration_experiment_v1", "complete": True,
          "settings": {"finger_id": "6", "batch": "A"}, "final": {"trials": []}}), encoding="utf-8")
    (path / "raw_spectra.jsonl").write_bytes(b'{"original":true}\n')
    return path


def test_history_http_lists_all_opens_exact_path_and_round_trips_recoverable_batch(tmp_path):
    paths = [archive(tmp_path, value) for value in ("first", "second")]
    missing = tmp_path / "calibration_sessions" / "interrupted"
    missing.mkdir()
    before = {p.name: {f.name: f.read_bytes() for f in p.iterdir()} for p in paths}
    with history_server(tmp_path) as (service, opened, url):
        result = request(url, "/api/history/sessions")
        assert {row["session_id"] for row in result["sessions"]} == {"first", "second", "interrupted"}
        assert not result["deleted_sessions"]
        assert not (tmp_path / "history_operations").exists()
        result = request(url, "/api/history/open", {"session_id": "first", "deleted": False})
        assert result == {"opened": True, "path": str(paths[0])}
        assert opened == [str(paths[0])]
        request(url, "/api/history/open", {"session_id": "../outside"}, error="标识")
        request(url, "/api/history/open", {"session_id": "first", "deleted": 1}, error="布尔")
        assert opened == [str(paths[0])]
        deleted = request(url, "/api/history/delete", {"session_ids": ["first", "second"]})
        assert deleted["deleted_ids"] == ["first", "second"]
        assert {row["session_id"] for row in deleted["deleted_sessions"]} == {"first", "second"}
        assert not paths[0].exists() and not paths[1].exists()
        request(url, "/api/history/open", {"session_id": "first", "deleted": False}, error="不存在")
        result = request(url, "/api/history/open", {"session_id": "first", "deleted": True})
        assert result["opened"] and opened[-1] == str(tmp_path / "deleted_calibration_sessions" / "first")
        restored = request(url, "/api/history/restore", {"session_ids": ["first", "second"]})
        assert restored["restored_ids"] == ["first", "second"] and not restored["deleted_sessions"]
        assert {p.name: {f.name: f.read_bytes() for f in p.iterdir()} for p in paths} == before
        assert not service.running and not service.force_source and not service.sources and not service.workers


@pytest.mark.parametrize("busy", ["training", "acquisition", "recording", "stopping"])
@pytest.mark.parametrize("action", ["delete", "restore"])
def test_history_mutation_rejects_busy_service_without_touching_archives(tmp_path, busy, action):
    path = archive(tmp_path, "original")
    before = (path / "raw_spectra.jsonl").read_bytes()
    with history_server(tmp_path) as (service, _, url):
        if busy == "training":
            service.analysis.busy = True
        elif busy == "acquisition":
            service.experiment.running = True
        else:
            setattr(service, busy, True)
        request(url, "/api/history/" + action, {"session_ids": ["original"]}, error="进行")
        assert (path / "raw_spectra.jsonl").read_bytes() == before
        assert not (tmp_path / "deleted_calibration_sessions").exists()
        assert not (tmp_path / "history_operations").exists()


def test_case_aliases_cannot_delete_the_same_windows_archive_twice(tmp_path):
    path = archive(tmp_path, "original")
    before = (path / "manifest.json").read_bytes()
    with history_server(tmp_path) as (_, opened, url):
        request(url, "/api/history/delete", {"session_ids": ["original", "ORIGINAL"]}, error="不重复")
        request(url, "/api/history/open", {"session_id": "ORIGINAL"}, error="大小写")
        assert not opened and (path / "manifest.json").read_bytes() == before
        assert not (tmp_path / "history_operations").exists()
