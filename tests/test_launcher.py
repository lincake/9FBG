"""Native Windows launcher acceptance, isolated from operator data and devices."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from ninefbg import PRODUCT, VERSION


def test_all_production_entry_points_use_native_pyqt():
    root = Path(__file__).resolve().parents[1]
    launcher = (root / "9FBG-Dev.cmd").read_text(encoding="utf-8")
    assert "-m ninefbg.qt_app" in launcher
    assert "pip install" not in launcher and "pytest" not in launcher
    assert "ninefbg.server" not in launcher
    assert "qt_app import main" in (root / "ninefbg/__main__.py").read_text(encoding="utf-8")
    from ninefbg import server
    import inspect
    source = inspect.getsource(server.main)
    assert "desktop_main" in source and "webbrowser" not in source and "LocalServer" not in source


def test_native_instance_identity_preserves_operator_data_separation(tmp_path):
    from ninefbg.qt_app import instance_name
    assert instance_name(tmp_path) == instance_name(tmp_path / ".." / tmp_path.name)
    assert instance_name(tmp_path) != instance_name(tmp_path / "other")


def test_window_construction_failure_closes_started_background_workers(tmp_path):
    code = """
import sys
from ninefbg import qt_app
contexts = []
cleaned = []
OriginalContext = qt_app.AppContext
class CheckedContext(OriginalContext):
    def join(self, *args, **kwargs):
        super().join(*args, **kwargs)
        assert not self._commands.isRunning() and not self._reads.isRunning()
        cleaned.append(True)
qt_app.AppContext = CheckedContext
def broken(context):
    contexts.append(context)
    raise RuntimeError('injected native widget constructor failure')
qt_app.MainWindow = broken
try:
    qt_app.main(['--userdata', sys.argv[1]])
except RuntimeError as error:
    assert 'injected' in str(error)
else:
    raise AssertionError('constructor failure was not propagated')
assert contexts and cleaned
assert not contexts[0].service.running
print('workers closed')
"""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "constructor-failure")],
                            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"workers closed" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="真实 Windows cmd 启动回归")
def test_production_cmd_native_cold_start_without_hardware_or_http(tmp_path):
    root = Path(__file__).resolve().parents[1]
    launcher = root / "9FBG-Dev.cmd"
    comspec = Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))
    userdata = tmp_path / "原生启动验证 data"
    report_path = tmp_path / "native-smoke.json"
    command = f'"{comspec}" /d /s /c ""{launcher}" --userdata "{userdata}" --smoke-test "{report_path}""'
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1"}
    result = subprocess.run(command, executable=str(comspec), cwd=tmp_path, env=env,
                            capture_output=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["gui"] == "PyQt6" and not report["http_server"]
    assert report["product"] == PRODUCT == "9FBG" and report["version"] == VERSION
    assert report["window_title"] == f"9FBG {VERSION}"
    state = report["state"]
    assert not state["running"] and not state["recording"]
    assert state["devices"] == [] and state["session"] is None
    assert not userdata.exists(), "普通启动/查看页面不能建立测量数据或改写设置"
    assert len(report["screenshots"]) == 3
    assert all(Path(path).stat().st_size > 10000 for path in report["screenshots"])


@pytest.mark.skipif(os.name != "nt", reason="Windows 本地桌面实例回归")
def test_second_native_launch_activates_existing_window_without_new_service(tmp_path):
    root = Path(__file__).resolve().parents[1]
    userdata = tmp_path / "单实例数据"
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1"}
    command = [sys.executable, "-m", "ninefbg.qt_app", "--userdata", str(userdata), "--quit-after-ms", "6500"]
    with (tmp_path / "first.log").open("wb") as log:
        first = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if "原生 PyQt6 已启动" in (tmp_path / "first.log").read_text(encoding="utf-8", errors="replace"):
                    break
                assert first.poll() is None
                time.sleep(.05)
            second = subprocess.run(command[:-2], cwd=root, env=env, capture_output=True,
                                     timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            assert second.returncode == 0
            assert "已激活现有桌面窗口" in second.stdout.decode("utf-8", "replace")
            assert first.poll() is None
            assert first.wait(timeout=15) == 0
            assert not userdata.exists()
        finally:
            if first.poll() is None:
                first.terminate()
                first.wait(timeout=10)
