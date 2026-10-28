"""Windows cold model loading uses generated fixtures and never opens hardware."""
from __future__ import annotations

import gc
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import weakref

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bootstrap(tmp_path):
    # An isolated module namespace prevents tests from releasing or replacing the
    # process's real native handle after another Qt test has already loaded it.
    spec = importlib.util.spec_from_file_location(
        "ninefbg_test_bootstrap_copy", ROOT / "ninefbg" / "qt_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / "current-environment" / "torch"
    package.mkdir(parents=True)
    origin = package / "__init__.py"
    origin.touch()
    library = package / "lib" / "c10.dll"
    library.parent.mkdir()
    library.touch()
    finder = Mock(return_value=SimpleNamespace(origin=str(origin)))
    loader = Mock()
    module.sys = SimpleNamespace(platform="win32", modules={})
    module.importlib = SimpleNamespace(util=SimpleNamespace(find_spec=finder))
    module.ctypes = SimpleNamespace(CDLL=loader)
    return module, finder, loader, library


def test_non_windows_does_not_discover_or_preload_optional_runtime(bootstrap):
    module, finder, loader, _ = bootstrap
    module.sys.platform = "linux"
    assert module.prepare_optional_torch() is None
    finder.assert_not_called()
    loader.assert_not_called()


def test_already_imported_torch_needs_no_native_preload(bootstrap):
    module, finder, loader, _ = bootstrap
    module.sys.modules["torch"] = object()
    assert module.prepare_optional_torch() is None
    finder.assert_not_called()
    loader.assert_not_called()


@pytest.mark.parametrize("qt_module", ["PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets"])
def test_existing_qt_library_prevents_unsafe_late_native_preload(bootstrap, qt_module):
    module, finder, loader, _ = bootstrap
    module.sys.modules[qt_module] = object()
    assert module.prepare_optional_torch() is None
    finder.assert_not_called()
    loader.assert_not_called()


@pytest.mark.parametrize("missing", ["package", "origin", "library"])
def test_missing_optional_runtime_never_blocks_optical_startup(bootstrap, missing):
    module, finder, loader, library = bootstrap
    if missing == "package":
        finder.return_value = None
    elif missing == "origin":
        finder.return_value = SimpleNamespace(origin=None)
    else:
        library.unlink()
    assert module.prepare_optional_torch() is None
    loader.assert_not_called()


def test_preload_uses_selected_environment_and_keeps_handle_alive_once(bootstrap):
    module, finder, loader, library = bootstrap

    class NativeHandle:
        pass

    handle = NativeHandle()
    reference = weakref.ref(handle)
    loader.return_value = handle
    assert module.prepare_optional_torch() is None
    loader.assert_called_once_with(str(library.resolve()))
    finder.assert_called_once_with("torch")
    loader.return_value = None
    del handle
    gc.collect()
    assert reference() is not None, "The preloaded DLL must remain owned before the later model import"
    assert module.prepare_optional_torch() is None
    assert loader.call_count == finder.call_count == 1
    assert "torch" not in module.sys.modules


@pytest.mark.parametrize("error", [ImportError("optional package unavailable"),
                                  OSError("optional package discovery failed"),
                                  ValueError("optional module has no spec")])
def test_optional_package_discovery_errors_never_block_startup(bootstrap, error):
    module, finder, loader, _ = bootstrap
    finder.side_effect = error
    assert module.prepare_optional_torch() is None
    loader.assert_not_called()


def test_native_preload_error_leaves_optical_startup_available(bootstrap):
    module, _, loader, _ = bootstrap
    loader.side_effect = OSError(1114, "DLL initialization failed")
    assert module.prepare_optional_torch() is None
    loader.assert_called_once()
    assert "torch" not in module.sys.modules


@pytest.mark.parametrize("winerror,message", [(1114, "Windows 错误 1114"),
                                            (126, "模型运行库加载失败"),
                                            (None, "需要可选 CPU PyTorch")])
def test_actual_model_import_failure_has_chinese_reason_and_original_cause(monkeypatch, winerror, message):
    import builtins
    from ninefbg.spectral_model import torch_module

    original_import = builtins.__import__
    failure = (OSError("injected optional runtime DLL failure") if winerror is not None
               else ImportError("injected optional runtime missing"))
    if winerror is not None:
        failure.winerror = winerror

    def fail_torch(name, *args, **kwargs):
        if name == "torch":
            raise failure
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_torch)
    with pytest.raises(RuntimeError, match=message) as result:
        torch_module()
    assert result.value.__cause__ is failure


def run_isolated(code, *arguments):
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, "-c", code, *map(str, arguments)],
                            cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=40,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows Qt 与可选 CPU PyTorch DLL 顺序回归")
def test_cold_production_qt_loads_real_bundle_in_command_thread_without_hardware(tmp_path):
    if importlib.util.find_spec("torch") is None:
        pytest.skip("当前独立环境未安装可选 CPU PyTorch")
    userdata = tmp_path / "合成模型数据"
    screenshot = tmp_path / "model-loaded.png"
    # Generate the legal bundle in another process. Creating it here would warm
    # torch and hide the exact first-import ordering that failed in production.
    run_isolated("""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_spectral_finetuning import saved_parent
saved_parent(Path(sys.argv[1]))
print('generated synthetic bundle')
""", userdata)
    output = run_isolated("""
import json
import os
from pathlib import Path
import sys
import time

assert 'torch' not in sys.modules
from ninefbg import qt_app
assert 'torch' not in sys.modules, 'Native startup must keep full PyTorch import lazy'
from PyQt6.QtCore import QThread
app = qt_app.QApplication([])
if not qt_app.QFontDatabase.families():
    font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'msyh.ttc'
    if font.is_file():
        qt_app.QFontDatabase.addApplicationFont(str(font))
qt_app.apply_theme(app)
service = qt_app.AcquisitionService(Path.cwd(), Path(sys.argv[1]))
assert not service.running and service.sources == []

def forbidden_connection(*args, **kwargs):
    raise AssertionError('Model import cannot connect instruments')

service.connect = service.connect_force = forbidden_connection
original_load = service.analysis_load
forward_shapes = []

def load_and_forward(*args, **kwargs):
    assert QThread.currentThread() is context._commands
    assert 'torch' not in sys.modules, 'This must be the command thread first import'
    result = original_load(*args, **kwargs)
    from ninefbg.spectral_model import torch_module
    torch = torch_module()
    with torch.inference_mode():
        scores, wrench = service.analysis.spectral_runtime.model(torch.zeros(1, 16, 512))
    assert torch.isfinite(scores).all().item() and torch.isfinite(wrench).all().item()
    forward_shapes.extend([list(scores.shape), list(wrench.shape)])
    return result

service.analysis_load = load_and_forward
context = qt_app.AppContext(service)
errors, loaded, closed = [], [], []
context.error.connect(errors.append)
context.action_finished.connect(lambda action, result: loaded.append(result) if action == 'analysis/load' else None)
context.shutdown_finished.connect(lambda: closed.append(True))
window = qt_app.MainWindow(context)
window.setAttribute(qt_app.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
window.resize(1800, 1150)
window.tabs.setCurrentIndex(1)
window.experiment_page.tabs.setCurrentIndex(2)
window.show()

def wait_for(predicate):
    deadline = time.monotonic() + 15
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate(), errors

try:
    wait_for(lambda: window.experiment_page.model_select.findData('Sparent') >= 0)
    window.experiment_page.model_select.setCurrentIndex(
        window.experiment_page.model_select.findData('Sparent'))
    assert 'torch' not in sys.modules
    assert context.execute('analysis/load', {'model_id': 'Sparent', 'unloaded_confirmed': True})
    wait_for(lambda: (loaded or errors) and context._busy_count == 0)
    assert not errors, errors
    assert loaded[0]['model_id'] == service.analysis.model_id == 'Sparent'
    assert service.analysis.spectral_runtime.active
    assert forward_shapes == [[1, 25], [1, 6]]
    assert not service.running and service.sources == []
    assert context.state['devices'] == [] and context.state['frame'] == {}
    app.processEvents()
    assert window.grab().save(sys.argv[2])
finally:
    window.live_page.timer.stop()
    window.experiment_page._fresh_timer.stop()
    window.experiment_page._preference_timer.stop()
    window.pulse_page.shutdown()
    context.shutdown()
    wait_for(lambda: bool(closed))
    context.join()

assert not context._commands.isRunning() and not context._reads.isRunning()
print(json.dumps({'model_id': service.analysis.model_id, 'shapes': forward_shapes,
                  'devices': service.sources, 'workers_closed': True,
                  'screenshot': sys.argv[2]}))
""", userdata, screenshot)
    evidence = json.loads(output.splitlines()[-1])
    assert evidence == {"model_id": "Sparent", "shapes": [[1, 25], [1, 6]],
                        "devices": [], "workers_closed": True, "screenshot": str(screenshot)}
    print(f"model-loaded screenshot: {screenshot}")
