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
def idle_server(userdata):
    service = AcquisitionService(Path(__file__).resolve().parents[1], userdata)
    server = LocalServer(("127.0.0.1", 0), partial(Handler, service=service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield service, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def post(url, route, payload, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(url+route, data=json.dumps(payload).encode(), headers=headers)
    return urllib.request.urlopen(request)


def test_new_workspace_read_only_routes_do_not_make_data_or_connect(tmp_path):
    userdata = tmp_path / "not-created"
    with idle_server(userdata) as (service, url):
        for route in ("/api/state", "/api/experiment/sessions", "/api/analysis/models", "/api/analysis/report"):
            with urllib.request.urlopen(url+route) as response:
                assert response.status == 200
                assert isinstance(json.load(response), dict)
        assert not service.running and not service.sources
        assert not userdata.exists()


def test_new_mutations_require_devices_and_data_and_local_origin(tmp_path):
    userdata = tmp_path / "not-created"
    with idle_server(userdata) as (service, url):
        for route, payload in (("/api/experiment/start", {"finger_id": "test", "batch": "A"}),
                               ("/api/experiment/baseline", {"unloaded_confirmed": True}),
                               ("/api/experiment/trial", {"action": "start"}),
                               ("/api/analysis/build", {"session_ids": []}),
                               ("/api/analysis/reference", {"unloaded_confirmed": False})):
            with pytest.raises(urllib.error.HTTPError) as error:
                post(url, route, payload)
            assert error.value.code == 400
            assert json.load(error.value)["error"]
        with pytest.raises(urllib.error.HTTPError) as error:
            post(url, "/api/experiment/start", {}, "https://external.invalid")
        assert error.value.code == 403
        assert not service.sources and not userdata.exists()
