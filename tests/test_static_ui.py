"""Protected historical Web UI regressions; production uses native PyQt6."""
from contextlib import contextmanager
from functools import partial
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from ninefbg import VERSION
from ninefbg.server import Handler, LocalServer
from ninefbg.service import AcquisitionService


@contextmanager
def isolated_ui_server(tmp_path):
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
        service.close()


def userdata_contents(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def assert_static_no_store(headers):
    assert headers.get_all("Cache-Control") == ["no-store, max-age=0"]


def test_current_static_ui_with_version_queries_and_no_hardware_or_storage(tmp_path, monkeypatch):
    with isolated_ui_server(tmp_path) as (service, base):
        before = userdata_contents(tmp_path)

        def forbidden(*args, **kwargs):
            raise AssertionError("读取界面不得连接设备、初始化参考、开始记录或写入设置")

        for method in ("connect", "connect_force", "reference", "pulse_reference", "experiment_start"):
            monkeypatch.setattr(service, method, forbidden)
        monkeypatch.setattr(service.preferences, "update", forbidden)

        for route in ("/", f"/?version={VERSION}", "/pulse.html",
                      f"/pulse.html?version={VERSION}", "/pulse.html?version=0.14.0"):
            with urllib.request.urlopen(base + route) as response:
                page = response.read().decode("utf-8")
                assert response.status == 200
                assert_static_no_store(response.headers)
            assert "9FBG" in page and "0.18.0" in page
            if route.startswith("/pulse.html"):
                assert "记录 10 s 无负载" in page
                assert "自动固定噪声权重" in page
                assert "学习中心点权重" not in page
                assert "学习权重 · 15 s" not in page

        for route in (f"/pulse.js?version={VERSION}", f"/pulse_signal.js?version={VERSION}",
                      f"/pulse.css?version={VERSION}", f"/style.css?version={VERSION}"):
            with urllib.request.urlopen(base + route) as response:
                assert response.status == 200 and response.read()
                assert_static_no_store(response.headers)

        assert userdata_contents(tmp_path) == before
        assert not service.running and service.sources == [] and service.workers == []
        assert service.writer is None and not service.tracker._pending_reference


def test_static_conditional_reload_returns_current_content_instead_of_304(tmp_path):
    with isolated_ui_server(tmp_path) as (_, base):
        for route in ("/", "/pulse.html", f"/pulse.js?version={VERSION}", "/pulse.css"):
            request = urllib.request.Request(base + route, headers={
                "If-Modified-Since": "Fri, 31 Dec 9999 23:59:59 GMT",
            })
            with urllib.request.urlopen(request) as response:
                assert response.status == 200 and response.read()
                assert_static_no_store(response.headers)


def test_api_keeps_one_no_store_header_and_static_origin_validation(tmp_path):
    with isolated_ui_server(tmp_path) as (service, base):
        with urllib.request.urlopen(base + "/api/state") as response:
            assert response.headers.get_all("Cache-Control") == ["no-store"]
            assert json.load(response)["version"] == VERSION
        request = urllib.request.Request(base + "/pulse.html", headers={"Origin": "https://external.invalid"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        assert caught.value.code == 403
        assert caught.value.headers.get_all("Cache-Control") == ["no-store"]
        assert "只接受本机界面请求" in json.load(caught.value)["error"]
        assert not service.running and service.sources == [] and service.writer is None
