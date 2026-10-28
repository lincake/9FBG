"""Automatic session IDs with local archives and simulated context, no devices."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ninefbg.data_number import AUTOMATIC_SOURCE, COUNTER_FILENAME, DataNumberAllocator, SCHEMA
from ninefbg.preferences import Preferences
from ninefbg.service import AcquisitionService
from test_experiment import context, settings, spectrum
from test_service import until


def archive(userdata, name, data_id, *, deleted=False, complete=True, automatic=False):
    root = userdata / ("deleted_calibration_sessions" if deleted else "calibration_sessions")
    path = root / name
    path.mkdir(parents=True)
    document = {"kind": "9fbg_calibration_experiment_v1", "session_id": name, "complete": complete,
                "settings": {"data_id": data_id}, "files": {}, "final": {}}
    if automatic:
        document["settings"]["data_id_source"] = AUTOMATIC_SOURCE
    (path / "manifest.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def manifest(path):
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def lease_note(userdata):
    with (userdata / ".data-number.lock").open("rb") as stream:
        stream.seek(1)  # The OS lease locks byte zero, not its diagnostic note.
        return json.loads(stream.read())


def local_service(userdata, monkeypatch):
    service = AcquisitionService(Path(__file__).resolve().parents[1], userdata)
    active = context(10_000_000_000)
    active["frame"] = spectrum(10_000_000_000)
    active["devices"][0]["configuration"] = {"interval_ms": 40, "trigger_hz": 25}

    def snapshot():
        experiment = service.experiment.snapshot()
        return {**deepcopy(active), "experiment": experiment,
                "data_number": service.data_numbers.snapshot(experiment)}

    monkeypatch.setattr(service, "snapshot", snapshot)
    monkeypatch.setattr(service, "_ensure_spectral_coordinator", lambda: None)
    return service


@pytest.mark.parametrize("preferred,expected", [("0018", "28"), ("0030", "31")])
def test_bootstrap_uses_numeric_history_deleted_and_preference_without_editing_them(tmp_path, preferred, expected):
    paths = [archive(tmp_path, "saved", "0018"), archive(tmp_path, "deleted", "0027", deleted=True),
             archive(tmp_path, "text", "样件 999"), archive(tmp_path, "old-incomplete", "23", complete=False)]
    preferences = Preferences(tmp_path)
    preferences.update("experiment", {"data_id": preferred})
    before = {(path / "manifest.json"): (path / "manifest.json").read_bytes() for path in paths}
    before[preferences.path] = preferences.path.read_bytes()
    allocator = DataNumberAllocator(tmp_path, preferences.values)
    assert allocator.current() == expected
    assert not (tmp_path / COUNTER_FILENAME).exists()
    assert all(path.read_bytes() == value for path, value in before.items())
    assert Preferences(tmp_path).values["experiment"]["data_id"] == preferred


def test_preview_is_memory_only_and_new_workspace_remains_read_only(tmp_path, monkeypatch):
    userdata = tmp_path / "not-created"
    service = AcquisitionService(Path(__file__).resolve().parents[1], userdata)

    def no_file_read(*args, **kwargs):
        pytest.fail("State preview must not scan or read numbering files")

    monkeypatch.setattr(service.data_numbers, "_reconcile_history", no_file_read)
    monkeypatch.setattr(service.data_numbers, "_load_counter", no_file_read)
    for _ in range(3):
        assert service.snapshot()["data_number"] == {"mode": "automatic", "next_data_id": "1",
                                                     "active_data_id": None, "active_source": None, "error": None}
    assert not userdata.exists() and not service.sources


@pytest.mark.parametrize("capture_mode", ["full_spectrum", "peak_drift"])
def test_successful_stop_advances_once_per_session_and_survives_restart(tmp_path, monkeypatch, capture_mode):
    archive(tmp_path, "previous", "18")
    service = local_service(tmp_path, monkeypatch)
    requested = settings(acquisition_protocol="force_target_wrench_v1", capture_mode=capture_mode)
    try:
        state = service.experiment_start(requested)
        assert "data_id" not in requested, "Caller settings are not rewritten"
        assert state["experiment"]["settings"]["data_id"] == "19"
        assert state["data_number"]["next_data_id"] == state["data_number"]["active_data_id"] == "19"
        assert state["data_number"]["active_source"] == "automatic"
        assert not service.data_numbers.path.exists(), "Starting a session cannot commit the next number"
        note = lease_note(tmp_path)
        assert note["session_id"] == service.experiment.session_id
        assert note["tentative_data_id"] == "19"
        path = service.experiment.writer.path
        # Round persistence and point selection remain inside this same number.
        service.experiment._transition("synthetic_round_saved", round_number=1)
        service.experiment._transition("synthetic_no_contact_round_saved", round_number=2)
        assert service.data_numbers.current() == "19" and not service.data_numbers.path.exists()
        state = service.experiment_stop()
        assert manifest(path)["complete"] is True and manifest(path)["settings"]["data_id"] == "19"
        assert state["data_number"]["next_data_id"] == "20"
        assert state["data_number"]["active_data_id"] is None
        original = service.data_numbers.path.read_bytes()
        service.experiment_stop()
        assert service.data_numbers.path.read_bytes() == original
        assert DataNumberAllocator(tmp_path).current() == "20"
        service.experiment_start(requested)
        assert service.experiment.settings["data_id"] == "20"
        service.experiment_stop()
        assert service.data_numbers.current() == "21"
    finally:
        if service.experiment.running:
            service.experiment_stop()
        service.data_numbers.release()
        service.analysis.close()


def test_failed_start_never_increments_and_releases_lease(tmp_path, monkeypatch):
    service = local_service(tmp_path, monkeypatch)
    original_start = service.experiment.start

    def fail(*args, **kwargs):
        raise OSError("模拟建立会话失败")

    monkeypatch.setattr(service.experiment, "start", fail)
    with pytest.raises(OSError, match="建立会话失败"):
        service.experiment_start(settings())
    assert service.data_numbers.current() == "1" and not service.data_numbers.path.exists()
    competitor = DataNumberAllocator(tmp_path)
    assert competitor.begin() == "1"
    competitor.release()
    monkeypatch.setattr(service.experiment, "start", original_start)
    service.experiment_start(settings())
    assert service.experiment.settings["data_id"] == "1"
    service.experiment_stop()
    assert service.data_numbers.current() == "2"


def test_invalid_start_does_not_create_numbering_or_session_files(tmp_path):
    userdata = tmp_path / "not-created"
    service = AcquisitionService(Path(__file__).resolve().parents[1], userdata)
    with pytest.raises(ValueError):
        service.experiment_start(settings())
    assert service.data_numbers.current() == "1" and not userdata.exists()
    assert not service.sources and not service.experiment.running


def test_incomplete_automatic_save_does_not_advance_even_after_restart(tmp_path, monkeypatch):
    archive(tmp_path, "previous", "18")
    service = local_service(tmp_path, monkeypatch)
    service.experiment_start(settings())
    path = service.experiment.writer.path
    service.experiment.error = "模拟处理队列未排空"
    service.experiment_stop()
    assert manifest(path)["complete"] is False
    assert service.data_numbers.current() == "19" and not service.data_numbers.path.exists()
    restarted = DataNumberAllocator(tmp_path)
    assert restarted.begin() == "19"
    restarted.release()


def test_manifest_saved_before_counter_failure_recovers_without_reusing_id(tmp_path, monkeypatch):
    archive(tmp_path, "previous", "18")
    counter = tmp_path / COUNTER_FILENAME
    counter.write_text(json.dumps({"schema": SCHEMA, "next_data_id": "19", "last_completed_session_id": "previous"}))
    original_counter = counter.read_bytes()
    service = local_service(tmp_path, monkeypatch)
    service.experiment_start(settings())
    path = service.experiment.writer.path
    import ninefbg.data_number as number_module

    original_replace = number_module.os.replace

    def fail_replace(*args):
        raise OSError("模拟原子替换失败")

    monkeypatch.setattr(number_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="会话已保存，编号状态保存失败"):
        service.experiment_stop()
    assert manifest(path)["complete"] is True
    assert service.data_numbers.current() == "20"
    assert service.data_numbers.snapshot()["error"]
    assert service.data_numbers.path.read_bytes() == original_counter
    assert not list(tmp_path.glob(".data-number-*.tmp"))
    restarted = DataNumberAllocator(tmp_path)
    assert restarted.current() == "20"
    assert restarted.begin() == "20", "Failed persistence also releases the OS lease"
    restarted.release()
    monkeypatch.setattr(number_module.os, "replace", original_replace)
    service.experiment_stop()
    assert service.data_numbers.snapshot()["error"] is None
    assert json.loads(service.data_numbers.path.read_text())["next_data_id"] == "20"


def test_active_reservation_blocks_other_allocators_then_refreshes_saved_counter(tmp_path):
    first, second = DataNumberAllocator(tmp_path), DataNumberAllocator(tmp_path)
    assert first.begin() == "1"
    path = archive(tmp_path, "first", "1", automatic=True)
    first.bind("first")
    with pytest.raises(ValueError, match="另一个采集会话"):
        second.begin()
    assert first.complete_session("first", path)
    first.release()
    assert second.begin() == "2", "A stale preview cannot allocate an already saved number"
    second.release()


def test_incomplete_crash_binding_can_reuse_tentative_id_with_distinct_session(tmp_path):
    first = DataNumberAllocator(tmp_path)
    assert first.begin() == "1"
    first.bind("crashed-session")
    archive(tmp_path, "crashed-session", "1", complete=False, automatic=True)
    first.release()  # Process termination releases the same OS-owned byte lock.
    second = DataNumberAllocator(tmp_path)
    assert second.begin() == "1"
    second.bind("new-session")
    note = lease_note(tmp_path)
    assert note["session_id"] == "new-session"
    second.release()


def test_os_lease_blocks_another_process_and_is_released_after_process_exit(tmp_path):
    path = archive(tmp_path, "child-session", "1", complete=False, automatic=True)
    original = (path / "manifest.json").read_bytes()
    ready = tmp_path / "child-ready"
    script = (
        "from pathlib import Path; import os, sys; "
        "from ninefbg.data_number import DataNumberAllocator; "
        "allocator=DataNumberAllocator(Path(sys.argv[1])); "
        "allocator.begin(); allocator.bind('child-session'); "
        "Path(sys.argv[2]).write_text('ready'); sys.stdin.readline(); os._exit(23)"
    )
    # Use the actual interpreter binary, avoiding the Windows venv launcher
    # wrapper whose exit does not itself prove its child has released handles.
    executable = getattr(sys, "_base_executable", sys.executable)
    process = subprocess.Popen([executable, "-c", script, str(tmp_path), str(ready)],
                               cwd=Path(__file__).resolve().parents[1],
                               stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    competitor = DataNumberAllocator(tmp_path)
    try:
        until(ready.exists)
        with pytest.raises(ValueError, match="另一个采集会话"):
            competitor.begin()
        process.stdin.write(b"exit\n")
        process.stdin.flush()
        assert process.wait(timeout=3) == 23  # os._exit bypasses release/cleanup.
        assert competitor.begin() == "1"
        competitor.bind("replacement-session")
        assert lease_note(tmp_path)["session_id"] == "replacement-session"
        assert (path / "manifest.json").read_bytes() == original
    finally:
        competitor.release()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)


@pytest.mark.parametrize("contents", ["not-json", '{"schema":"wrong","next_data_id":"1"}',
                                      '{"schema":"9fbg_data_number_v1","next_data_id":0}'])
def test_corrupt_counter_blocks_automatic_allocation_without_overwriting(tmp_path, contents):
    path = tmp_path / COUNTER_FILENAME
    path.write_text(contents, encoding="utf-8")
    archive(tmp_path, "saved", "18")
    allocator = DataNumberAllocator(tmp_path)
    assert allocator.snapshot()["error"]
    with pytest.raises(ValueError, match="自动数据编号读取失败"):
        allocator.begin()
    assert path.read_text(encoding="utf-8") == contents


def test_legacy_explicit_ids_and_existing_duplicate_history_remain_unchanged(tmp_path, monkeypatch):
    old = [archive(tmp_path, "old-a", "0018"), archive(tmp_path, "old-b", "0018", deleted=True)]
    before = [(path / "manifest.json").read_bytes() for path in old]
    service = local_service(tmp_path, monkeypatch)
    service.experiment_start(settings(data_id="人工-01"))
    path = service.experiment.writer.path
    assert service.experiment.settings["data_id"] == "人工-01"
    assert "data_id_source" not in service.experiment.settings
    assert service.data_numbers.snapshot(service.experiment.snapshot())["active_source"] == "legacy_manual"
    service.experiment_stop()
    assert manifest(path)["settings"]["data_id"] == "人工-01"
    assert service.data_numbers.current() == "19"
    service.experiment_start(settings(data_id="0025"))
    path = service.experiment.writer.path
    service.experiment_stop()
    assert manifest(path)["settings"]["data_id"] == "0025"
    assert service.data_numbers.current() == "26"
    assert [(path / "manifest.json").read_bytes() for path in old] == before


def test_counter_cannot_lower_existing_preference_floor(tmp_path):
    (tmp_path / COUNTER_FILENAME).write_text(json.dumps({"schema": SCHEMA, "next_data_id": "1", "last_completed_session_id": None}))
    allocator = DataNumberAllocator(tmp_path, {"experiment": {"data_id": "18"}})
    assert allocator.current() == allocator.begin() == "19"
    allocator.release()


def test_manifest_save_exception_releases_number_without_commit(tmp_path, monkeypatch):
    import ninefbg.storage as storage

    archive(tmp_path, "previous", "18")
    service = local_service(tmp_path, monkeypatch)
    service.experiment_start(settings())
    path = service.experiment.writer.path

    def fail_hash(*args):
        raise OSError("模拟清单哈希失败")

    monkeypatch.setattr(storage.hashlib, "file_digest", fail_hash)
    with pytest.raises(OSError, match="模拟清单哈希失败"):
        service.experiment_stop()
    assert not service.experiment.running and service.experiment.writer.closed
    assert not (path / "manifest.json").exists()
    assert not service.data_numbers.path.exists() and service.data_numbers.current() == "19"
    competitor = DataNumberAllocator(tmp_path)
    assert competitor.begin() == "19"
    competitor.release()


def test_bind_error_and_cleanup_save_exception_still_release_number(tmp_path, monkeypatch):
    import ninefbg.storage as storage

    archive(tmp_path, "previous", "18")
    service = local_service(tmp_path, monkeypatch)

    def fail_bind(*args):
        raise OSError("模拟编号关联失败")

    def fail_hash(*args):
        raise OSError("模拟清理清单失败")

    monkeypatch.setattr(service.data_numbers, "bind", fail_bind)
    monkeypatch.setattr(storage.hashlib, "file_digest", fail_hash)
    with pytest.raises(OSError, match="模拟编号关联失败") as failure:
        service.experiment_start(settings())
    assert "模拟清理清单失败" in str(failure.value.__cause__)
    assert not service.experiment.running and service.experiment.writer.closed
    assert service.data_numbers.current() == "19" and not service.data_numbers.path.exists()
    competitor = DataNumberAllocator(tmp_path)
    assert competitor.begin() == "19"
    competitor.release()
