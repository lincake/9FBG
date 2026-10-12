from contextlib import contextmanager
from functools import partial
import json
from pathlib import Path
import socket
import threading
import urllib.error
import urllib.request

import pytest

from ninefbg.server import Handler, LocalServer


class IdleService:
    root = Path(__file__).resolve().parents[1]
    def __init__(self):
        self.connects = 0
        self.options = None
        self.force_connects = self.force_disconnects = 0
    def snapshot(self):
        return {"product":"9FBG","running":False,"devices":[]}
    def connect(self, *args, **kwargs):
        self.connects += 1
        self.options = kwargs
        return self.snapshot()
    def connect_force(self):
        self.force_connects += 1
        return self.snapshot()
    def disconnect_force(self):
        self.force_disconnects += 1
        return self.snapshot()


@contextmanager
def local_server():
    service=IdleService()
    server=LocalServer(("127.0.0.1",0),partial(Handler,service=service))
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield server,service,f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_read_only_ui_and_api_never_connect_hardware():
    with local_server() as (server, service, url):
        with urllib.request.urlopen(url+"/api/state") as response:
            assert json.load(response)["running"] is False
        with urllib.request.urlopen(url+"/") as response:
            assert "9FBG" in response.read().decode()
        assert service.connects==0


def test_remote_origin_cannot_open_instrument():
    with local_server() as (_, service, url):
        request=urllib.request.Request(url+"/api/connect",data=b'{}',headers={"Content-Type":"application/json","Origin":"https://external.invalid"})
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(request)
        assert info.value.code==403
        assert service.connects==0


def test_malformed_json_has_chinese_error_and_no_mutation():
    with local_server() as (_, service, url):
        request=urllib.request.Request(url+"/api/connect",data=b'{bad',headers={"Content-Type":"application/json"})
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(request)
        assert info.value.code==400
        assert "请求不是有效的 JSON" in json.load(info.value)["error"]
        assert service.connects==0


def test_second_server_cannot_share_same_port():
    with local_server() as (server, _, __):
        with pytest.raises(OSError):
            other=LocalServer(("127.0.0.1",server.server_port),partial(Handler,service=IdleService()))
            other.server_close()


def post_json(url, route, payload):
    request = urllib.request.Request(url+route, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(request)


def test_display_connect_passes_explicit_requests_and_force_routes():
    with local_server() as (_, service, url):
        options = {"record": False, "connect_force": False, "integration_us": 20, "sample_rate_hz": 100}
        with post_json(url, "/api/connect", {"mode": "hardware", **options}) as response:
            assert response.status == 200
        assert service.options == options
        with post_json(url, "/api/force/connect", {}) as response:
            assert response.status == 200
        with post_json(url, "/api/force/disconnect", {}) as response:
            assert response.status == 200
        assert service.force_connects == service.force_disconnects == 1


@pytest.mark.parametrize("options", [
    {"record": 0}, {"connect_force": "false"}, {"integration_us": True},
    {"integration_us": 19}, {"integration_us": 10_000_001},
    {"sample_rate_hz": 0}, {"sample_rate_hz": 201},
    {"sample_rate_hz": 100.0}, {"sample_rate_hz": 30},
])
def test_invalid_display_requests_rejected_before_connection(options):
    with local_server() as (_, service, url):
        with pytest.raises(urllib.error.HTTPError) as info:
            post_json(url, "/api/connect", options)
        assert info.value.code == 400
        assert json.load(info.value)["error"]
        assert service.connects == 0
