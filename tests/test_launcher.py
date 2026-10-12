"""Exercise the delivered Windows launcher without connecting any instruments."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from ninefbg import PRODUCT, VERSION


pytestmark = pytest.mark.skipif(os.name != "nt", reason="真实 Windows cmd 启动回归")


def _read_state(url):
    with urllib.request.urlopen(url + "/api/state", timeout=0.5) as response:
        return json.load(response)


def _shutdown(url):
    request = urllib.request.Request(
        url + "/api/shutdown", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def test_production_cmd_cold_start_reuse_and_controlled_shutdown(tmp_path):
    """A shortcut's actual .cmd must survive Chinese paths and launch arguments.

    No /api/connect request is made. An independent port/userdata prevents this
    test from reusing, stopping or writing into the operator's normal service.
    """
    root = Path(__file__).resolve().parents[1]
    launcher = root / "9FBG-Dev.cmd"
    comspec = Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))
    assert launcher.is_file()
    assert comspec.is_file()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    userdata = tmp_path / "启动验证 data"
    url = f"http://127.0.0.1:{port}"
    # Pass the raw Unicode command line to CreateProcess. /s /c uses the normal
    # outer quote pair, retaining quotes around the production batch and data.
    # list2cmdline backslash escaping is inappropriate inside cmd's /c string.
    command = (
        f'"{comspec}" /d /s /c ""{launcher}" '
        f'--no-browser --port {port} --userdata "{userdata}""'
    )
    log_path = tmp_path / "cold-launch.log"
    reuse_log_path = tmp_path / "reuse-launch.log"
    started = None
    ready = False
    with log_path.open("wb") as output:
        try:
            # Intentionally run from outside the application directory: the
            # real shortcut can have another cwd and must rely on %~dp0.
            started = subprocess.Popen(
                command, executable=str(comspec), cwd=tmp_path,
                stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            deadline = time.perf_counter() + 25
            state = None
            while time.perf_counter() < deadline:
                if started.poll() is not None:
                    pytest.fail(
                        f"生产批处理冷启动提前退出 {started.returncode}：\n"
                        + log_path.read_text(encoding="utf-8", errors="replace")
                    )
                try:
                    state = _read_state(url)
                    ready = True
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    time.sleep(0.05)
            assert ready, "生产批处理冷启动未在独立端口提供状态接口"
            assert state["product"] == PRODUCT == "9FBG"
            assert state["version"] == VERSION
            assert state["running"] is False
            assert state["session"] is None
            assert state["devices"] == []
            assert not (userdata / "sessions").exists(), "源码启动不应建立硬件采集会话"

            begin_reuse = time.perf_counter()
            with reuse_log_path.open("wb") as reuse_output:
                reused = subprocess.run(
                    command, executable=str(comspec), cwd=tmp_path,
                    stdout=reuse_output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=12,
                )
            assert reused.returncode == 0, reuse_log_path.read_text(encoding="utf-8", errors="replace")
            assert time.perf_counter() - begin_reuse < 12
            assert "已复用现有工作台" in reuse_log_path.read_text(encoding="utf-8", errors="replace")
            assert started.poll() is None, "重复启动应复用原服务，而不终止冷启动进程"
            reused_state = _read_state(url)
            assert reused_state["version"] == VERSION
            assert reused_state["running"] is False
            assert reused_state["devices"] == []
            assert reused_state["session"] is None

            closed = _shutdown(url)
            assert closed["running"] is False
            assert closed.get("stopping") is False
            assert started.wait(timeout=12) == 0, "受控退出应让生产 cmd 正常返回"
            assert not (userdata / "sessions").exists()
        finally:
            if started is not None and started.poll() is None:
                if ready:
                    try:
                        _shutdown(url)
                        started.wait(timeout=8)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
                if started.poll() is None:
                    # Failure cleanup applies only to this test's own process
                    # tree, which has never received a hardware connect call.
                    subprocess.run(
                        ["taskkill", "/PID", str(started.pid), "/T", "/F"],
                        capture_output=True, timeout=8,
                    )
                    started.wait(timeout=8)
