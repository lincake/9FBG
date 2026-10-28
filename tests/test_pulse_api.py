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
def pulse_server(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    server = LocalServer(("127.0.0.1", 0), partial(Handler, service=service))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield service, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_api_read_only_idle_and_stale_cursor_require_reset_without_devices(tmp_path, monkeypatch):
    with pulse_server(tmp_path) as (service, base):
        def forbidden(*args, **kwargs):
            raise AssertionError("只读接口不得连接设备或构造完整快照")
        monkeypatch.setattr(service, "connect", forbidden)
        monkeypatch.setattr(service, "snapshot", forbidden)
        with urllib.request.urlopen(base + "/api/pulse/samples?after=50&limit=10") as response:
            payload = json.load(response)
            assert response.headers["Cache-Control"] == "no-store"
        assert payload["schema"] == "nine_fbg_pulse_samples_v1"
        assert payload["cursor"] == 0 and payload["reset_required"]
        assert payload["blocked"] and not payload["running"] and payload["samples"] == []
        assert service.sources == [] and service.writer is None and service.workers == []


@pytest.mark.parametrize("query", ["after=-1", "after=true", "after=1.0", "after=", "after=+1",
                                   "after=9007199254740992", "after=0&after=1", "after=0&unexpected=1",
                                   "limit=0", "limit=4097", "limit=-1", "limit=", "after=%EF%BC%91"])
def test_api_invalid_parameters_return_chinese_400_without_mutation(tmp_path, query):
    with pulse_server(tmp_path) as (service, base):
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + "/api/pulse/samples?" + query)
        assert caught.value.code == 400 and json.load(caught.value)["error"]
        assert not service.running and not service.sources and service.writer is None


def test_api_rejects_external_origin_before_reading_buffer(tmp_path):
    with pulse_server(tmp_path) as (_, base):
        request = urllib.request.Request(base + "/api/pulse/samples", headers={"Origin": "https://external.invalid"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        assert caught.value.code == 403


def request_reference(base, payload, **headers):
    return urllib.request.urlopen(urllib.request.Request(
        base + "/api/pulse/reference", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers}))


def test_pulse_reference_api_explicit_first_initialization_without_px6d(tmp_path, monkeypatch):
    with pulse_server(tmp_path) as (service, base):
        service.running, service.mode = True, "hardware"
        def forbidden(*args, **kwargs):
            raise AssertionError("初始化不得隐式连接光谱或 PX6D")
        monkeypatch.setattr(service, "connect", forbidden)
        monkeypatch.setattr(service, "connect_force", forbidden)
        with urllib.request.urlopen(base + "/api/pulse/samples") as response:
            initial = json.load(response)
        assert initial["can_initialize"] and not service.tracker._pending_reference
        with request_reference(base, {"unloaded_confirmed": True}) as response:
            result = json.load(response)
        assert response.status == 200 and result["can_initialize"] is False
        assert service.tracker._pending_reference and service.reference_min_rx_ns > 0
        assert service.latest_force is None and service.sources == [] and service.writer is None
        with pytest.raises(urllib.error.HTTPError) as caught:
            request_reference(base, {"unloaded_confirmed": True})
        assert caught.value.code == 400 and "重复" in json.load(caught.value)["error"]


@pytest.mark.parametrize("payload", [{}, {"unloaded_confirmed": False}, {"unloaded_confirmed": 1},
                                       {"unloaded_confirmed": "true"}, {"unloaded_confirmed": True, "extra": 1}])
def test_pulse_reference_api_invalid_confirmation_is_400_without_reference_change(tmp_path, payload):
    with pulse_server(tmp_path) as (service, base):
        service.running, service.mode = True, "hardware"
        with pytest.raises(urllib.error.HTTPError) as caught:
            request_reference(base, payload)
        assert caught.value.code == 400 and json.load(caught.value)["error"]
        assert not service.tracker._pending_reference and service.reference_min_rx_ns == 0


def test_pulse_reference_api_preserves_initialized_reference_and_rejects_external_origin(tmp_path):
    with pulse_server(tmp_path) as (service, base):
        service.running, service.mode = True, "hardware"
        service.tracker._anchors = list(range(9))
        service.tracker._reference = [1500 + index for index in range(9)]
        service.tracker._reference_version = 7
        with pytest.raises(urllib.error.HTTPError) as caught:
            request_reference(base, {"unloaded_confirmed": True})
        assert caught.value.code == 400 and "现有参考" in json.load(caught.value)["error"]
        with pytest.raises(urllib.error.HTTPError) as caught:
            request_reference(base, {"unloaded_confirmed": True}, Origin="https://external.invalid")
        assert caught.value.code == 403
        assert service.tracker._reference_version == 7 and service.tracker._reference == [1500 + i for i in range(9)]
        assert not service.tracker._pending_reference
