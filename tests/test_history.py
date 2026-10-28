"""History operations run only in temporary archives; Explorer is always a spy."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from ninefbg.experiment import default_point_grid
from ninefbg.history import (ACTIVE_DIRECTORY, DELETED_DIRECTORY, SessionHistory,
                            session_path)


def archive(tmp_path, session_id="session_01", *, accepted=1, **overrides):
    directory = tmp_path/ACTIVE_DIRECTORY/session_id
    directory.mkdir(parents=True)
    manifest = {"kind": "9fbg_calibration_experiment_v1", "complete": True,
                "started_utc": "2026-10-12T01:00:00+00:00", "ended_utc": "2026-10-12T01:02:00+00:00",
                "settings": {"finger_id": "finger-1", "data_id": "0001", "batch": "A",
                             "guided_acquisition": True, "repetitions_per_point": 3,
                             "point_grid": default_point_grid()},
                "counts": {"raw_spectra": 2, "raw_force": 2, "results": 2, "events": 1},
                "final": {"trials": [{"trial_id": f"T{index+1:04d}", "point_id": "P01",
                                      "status": "completed", "bad": False, "manual_mark": [],
                                      "quality": {"accepted": True, "criteria_version": "guided_round_v1"}}
                                     for index in range(accepted)]}, **overrides}
    (directory/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for name in ("raw_spectra.jsonl", "raw_force.jsonl", "results.jsonl", "events.jsonl"):
        (directory/name).write_bytes((json.dumps({"fixture": name, "session_id": session_id,
                                                "values": [1, -2, 3.125]}, ensure_ascii=False)+"\n").encode())
    extra = directory/"extra"/"original.bin"
    extra.parent.mkdir()
    extra.write_bytes(bytes(range(256)))
    return directory


def contents(directory):
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def operation_records(tmp_path):
    return [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path/"history_operations").glob("*.json")]


def rows_by_id(snapshot, field="sessions"):
    return {row["session_id"]: row for row in snapshot[field]}


def test_empty_history_is_read_only_and_does_not_create_directories(tmp_path):
    history = SessionHistory(tmp_path, opener=lambda path: pytest.fail("不应打开目录"))
    assert history.snapshot() == {"sessions": [], "deleted_sessions": []}
    assert list(tmp_path.iterdir()) == []


def test_default_history_lists_all_archives_and_candidates_need_not_have_three_trials(tmp_path):
    candidate = archive(tmp_path, "candidate", accepted=1)
    archive(tmp_path, "empty", accepted=0)
    archive(tmp_path, "unfinished", complete=False)
    archive(tmp_path, "diagnostic", diagnostic=True)
    archive(tmp_path, "retained_C", settings={"batch": "C"})
    missing = archive(tmp_path, "no_manifest")
    (missing/"manifest.json").unlink()
    broken = archive(tmp_path, "bad_manifest")
    (broken/"manifest.json").write_bytes(b"{not-json")
    malformed = archive(tmp_path, "wrong_shape")
    (malformed/"manifest.json").write_text("[]", encoding="utf-8")
    root = tmp_path/ACTIVE_DIRECTORY
    (root/"not-a-session.txt").write_text("普通文件", encoding="utf-8")
    before = contents(root)
    rows = rows_by_id(SessionHistory(tmp_path).snapshot())
    assert set(rows) == {"candidate", "empty", "unfinished", "diagnostic", "retained_C", "no_manifest", "bad_manifest", "wrong_shape"}
    assert rows["candidate"]["trainable"] and rows["candidate"]["accepted_trial_count"] == 1
    assert rows["candidate"]["completed_point_count"] == 0
    assert rows["candidate"]["formal_training_qualified"] is False
    assert all(not rows[key]["trainable"] and rows[key]["history_error"] for key in set(rows)-{"candidate"})
    assert all(row["openable"] and row["deletable"] for row in rows.values())
    assert contents(root) == before
    assert contents(candidate)["manifest.json"] == before[str(Path("candidate")/"manifest.json")]


def test_active_archive_cannot_be_deleted_or_used_as_a_training_candidate(tmp_path):
    directory = archive(tmp_path)
    before = contents(directory)
    history = SessionHistory(tmp_path)
    row = rows_by_id(history.snapshot(active_session_id=directory.name))[directory.name]
    assert row["openable"] and not row["deletable"] and not row["trainable"]
    with pytest.raises(ValueError, match="正在采集"):
        history.delete([directory.name], active_session_id=directory.name)
    assert contents(directory) == before and not operation_records(tmp_path)
    history.delete([directory.name])
    with pytest.raises(ValueError, match="正在采集"):
        history.restore([directory.name], active_session_id=directory.name)
    assert contents(tmp_path/DELETED_DIRECTORY/directory.name) == before


@pytest.mark.parametrize("restore", [False, True])
def test_batch_missing_id_is_preflighted_before_any_move_or_new_log(tmp_path, restore):
    directory = archive(tmp_path)
    history = SessionHistory(tmp_path)
    if restore:
        history.delete([directory.name])
        directory = tmp_path/DELETED_DIRECTORY/directory.name
    before = contents(directory)
    logs = deepcopy(operation_records(tmp_path))
    with pytest.raises(ValueError):
        (history.restore if restore else history.delete)([directory.name, "missing"])
    assert contents(directory) == before
    assert operation_records(tmp_path) == logs


@pytest.mark.parametrize("ids", [None, [], "session_01", ["session_01", "session_01"],
                                 [1], ["session_01", "../escape"], ["session_01"]*101])
def test_invalid_batch_and_duplicate_ids_never_move_data(tmp_path, ids):
    directory = archive(tmp_path)
    before = contents(directory)
    history = SessionHistory(tmp_path)
    with pytest.raises(ValueError):
        history.delete(ids)
    assert contents(directory) == before and not operation_records(tmp_path)


@pytest.mark.parametrize("bad_id", ["..", "../outside", "session/child", "session\\child", "/absolute", "C:\\outside",
                                    "\\\\server\\share", "a:b", "a;echo", "a&calc", "'session'", ""])
def test_resolver_and_open_accept_only_whitelisted_ids(tmp_path, bad_id):
    archive(tmp_path)
    opened = []
    history = SessionHistory(tmp_path, opener=opened.append)
    with pytest.raises(ValueError):
        session_path(tmp_path, bad_id, include_deleted=True)
    with pytest.raises(ValueError):
        history.open(bad_id)
    assert not opened and not operation_records(tmp_path)


def test_open_passes_one_checked_directory_to_spy_and_never_uses_shell(tmp_path, monkeypatch):
    import subprocess
    directory = archive(tmp_path)
    opened = []
    monkeypatch.setattr(os, "system", lambda *args: pytest.fail("不得调用 shell"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("不得调用子进程"))
    history = SessionHistory(tmp_path, opener=opened.append)
    assert history.open(directory.name) == {"opened": True, "path": str(directory)}
    assert opened == [str(directory)]
    with pytest.raises(ValueError):
        history.open(str(directory.resolve()))
    with pytest.raises(ValueError, match="布尔"):
        history.open(directory.name, deleted=1)
    history.delete([directory.name])
    with pytest.raises(ValueError):
        history.open(directory.name)
    deleted = tmp_path/DELETED_DIRECTORY/directory.name
    assert history.open(directory.name, deleted=True)["path"] == str(deleted)
    assert opened == [str(directory), str(deleted)]


def test_delete_and_restore_preserve_all_raw_bytes_manifest_and_recovery_provenance(tmp_path):
    first, second = archive(tmp_path, "first"), archive(tmp_path, "second", accepted=0)
    before = {path.name: contents(path) for path in (first, second)}
    history = SessionHistory(tmp_path)
    deleted = history.delete(["first", "second"])
    assert deleted["deleted_ids"] == ["first", "second"] and deleted["sessions"] == []
    assert set(rows_by_id(deleted, "deleted_sessions")) == {"first", "second"}
    for name in before:
        assert not (tmp_path/ACTIVE_DIRECTORY/name).exists()
        path = session_path(tmp_path, name, include_deleted=True)
        assert path.parent == tmp_path/DELETED_DIRECTORY and contents(path) == before[name]
        with pytest.raises(ValueError, match="已删除"):
            session_path(tmp_path, name)
        row = rows_by_id(deleted, "deleted_sessions")[name]
        assert row["restorable"] and row["openable"] and not row["trainable"] and not row["deletable"]
    delete_log, = operation_records(tmp_path)
    assert delete_log["action"] == "delete" and delete_log["status"] == "committed"
    assert delete_log["completed_ids"] == ["first", "second"]
    restored = history.restore(["first", "second"])
    assert restored["restored_ids"] == ["first", "second"] and not restored["deleted_sessions"]
    for name in before:
        assert contents(session_path(tmp_path, name)) == before[name]
    logs = operation_records(tmp_path)
    assert len(logs) == 2 and {row["action"] for row in logs} == {"delete", "restore"}
    assert all(row["status"] == "committed" for row in logs)


@pytest.mark.parametrize("restore", [False, True])
def test_destination_collision_refuses_to_overwrite_either_archive(tmp_path, restore):
    source = archive(tmp_path)
    history = SessionHistory(tmp_path)
    if restore:
        history.delete([source.name])
        source = tmp_path/DELETED_DIRECTORY/source.name
    target = tmp_path/(ACTIVE_DIRECTORY if restore else DELETED_DIRECTORY)/source.name
    target.mkdir(parents=True)
    (target/"sentinel.bin").write_bytes(b"existing destination")
    source_before, target_before = contents(source), contents(target)
    logs = deepcopy(operation_records(tmp_path))
    with pytest.raises(ValueError, match="同时存在|同名"):
        (history.restore if restore else history.delete)([source.name])
    assert contents(source) == source_before and contents(target) == target_before
    assert operation_records(tmp_path) == logs


@pytest.mark.parametrize("restore", [False, True])
def test_second_rename_failure_rolls_back_the_first_archive(tmp_path, monkeypatch, restore):
    paths = [archive(tmp_path, name) for name in ("first", "second")]
    history = SessionHistory(tmp_path)
    if restore:
        history.delete([path.name for path in paths])
        paths = [tmp_path/DELETED_DIRECTORY/path.name for path in paths]
    before = {path.name: contents(path) for path in paths}
    original = Path.rename
    fail_source = paths[1]
    def fail_second(path, target):
        if path == fail_source:
            raise OSError("第二条移动失败")
        return original(path, target)
    monkeypatch.setattr(Path, "rename", fail_second)
    with pytest.raises(ValueError, match="第二条移动失败.*原有数据已保留"):
        (history.restore if restore else history.delete)(["first", "second"])
    for path in paths:
        assert contents(path) == before[path.name]
        opposite = tmp_path/(ACTIVE_DIRECTORY if restore else DELETED_DIRECTORY)/path.name
        assert not opposite.exists()
    rollback = next(record for record in operation_records(tmp_path) if record["status"] == "rolled_back")
    assert rollback["completed_ids"] == ["first"] and rollback["rollback_errors"] == []


def test_commit_log_failure_rolls_back_all_archives_and_keeps_original_bytes(tmp_path, monkeypatch):
    paths = [archive(tmp_path, name) for name in ("first", "second")]
    before = {path.name: contents(path) for path in paths}
    history = SessionHistory(tmp_path)
    original = history._write_operation
    def fail_commit(record):
        if record["status"] == "committed":
            raise OSError("提交日志写入失败")
        return original(record)
    monkeypatch.setattr(history, "_write_operation", fail_commit)
    with pytest.raises(ValueError, match="提交日志写入失败.*原有数据已保留"):
        history.delete(["first", "second"])
    assert all(contents(path) == before[path.name] for path in paths)
    assert not list((tmp_path/DELETED_DIRECTORY).iterdir())
    record, = operation_records(tmp_path)
    assert record["status"] == "rolled_back" and record["completed_ids"] == ["first", "second"]
    assert record["rollback_errors"] == []


def create_directory_link(link, target):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        assert link.is_junction()
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("linked_root", [ACTIVE_DIRECTORY, DELETED_DIRECTORY, "history_operations"])
def test_junction_roots_cannot_escape_userdata(tmp_path, linked_root):
    userdata, external = tmp_path/"userdata", tmp_path/"outside"
    userdata.mkdir()
    external.mkdir()
    (external/"sentinel.bin").write_bytes(b"outside remains unchanged")
    if linked_root == "history_operations":
        directory = archive(userdata)
        before = contents(directory)
    link = userdata/linked_root
    create_directory_link(link, external)
    try:
        history = SessionHistory(userdata)
        if linked_root == "history_operations":
            with pytest.raises(ValueError, match="链接|超出"):
                history.delete([directory.name])
            assert contents(directory) == before
        else:
            with pytest.raises(ValueError, match="链接|超出"):
                session_path(userdata, "anything", include_deleted=True)
            with pytest.raises(ValueError, match="链接|超出"):
                history.snapshot()
        assert contents(external) == {"sentinel.bin": b"outside remains unchanged"}
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_linked_session_and_nested_junction_are_not_opened_or_moved(tmp_path):
    outside = tmp_path/"outside"
    outside.mkdir()
    (outside/"sentinel.bin").write_bytes(b"unrelated raw data")
    root = tmp_path/ACTIVE_DIRECTORY
    root.mkdir()
    linked = root/"linked_session"
    create_directory_link(linked, outside)
    normal = archive(tmp_path, "normal")
    nested = normal/"nested_link"
    create_directory_link(nested, outside)
    opened = []
    history = SessionHistory(tmp_path, opener=opened.append)
    try:
        row = rows_by_id(history.snapshot())[linked.name]
        assert not row["openable"] and not row["deletable"] and not row["trainable"]
        with pytest.raises(ValueError, match="链接|超出"):
            history.open(linked.name)
        with pytest.raises(ValueError, match="链接|超出"):
            history.delete([linked.name])
        with pytest.raises(ValueError, match="目录链接"):
            history.delete([normal.name])
        assert not opened and not operation_records(tmp_path)
        assert (outside/"sentinel.bin").read_bytes() == b"unrelated raw data"
    finally:
        for link in (nested, linked):
            link.rmdir() if os.name == "nt" else link.unlink()


def test_missing_or_file_shaped_session_is_not_a_resolvable_source(tmp_path):
    root = tmp_path/ACTIVE_DIRECTORY
    root.mkdir()
    (root/"file_session").write_bytes(b"not a directory")
    with pytest.raises(ValueError, match="不是文件夹"):
        session_path(tmp_path, "file_session")
    with pytest.raises(ValueError, match="不存在"):
        session_path(tmp_path, "missing", include_deleted=True)


@pytest.mark.parametrize("record_name", ["raw_force.jsonl", "optical_sources.jsonl", "peak_frames.jsonl"])
def test_linked_raw_file_cannot_be_resolved_opened_or_moved(tmp_path, monkeypatch, record_name):
    directory = archive(tmp_path)
    raw = directory/record_name
    outside = tmp_path/"outside_force.jsonl"
    outside.write_bytes(b"unrelated force evidence\n")
    original = Path.is_symlink
    # File-link permissions differ by Windows account; exercise the same
    # path-level rejection without requiring elevated symlink creation.
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == raw or original(path))
    opened = []
    history = SessionHistory(tmp_path, opener=opened.append)
    row = rows_by_id(history.snapshot())[directory.name]
    assert not row["openable"] and not row["trainable"] and not row["deletable"]
    for operation in (lambda: session_path(tmp_path, directory.name),
                      lambda: history.open(directory.name), lambda: history.delete([directory.name])):
        with pytest.raises(ValueError, match="文件存在链接"):
            operation()
    assert not opened and not operation_records(tmp_path)
    assert outside.read_bytes() == b"unrelated force evidence\n"


def test_over_limit_unique_batch_is_rejected_before_resolving_any_archive(tmp_path, monkeypatch):
    history = SessionHistory(tmp_path)
    monkeypatch.setattr("ninefbg.history.session_path", lambda *args, **kwargs: pytest.fail("无效批量不应解析来源"))
    with pytest.raises(ValueError, match="1～100"):
        history.delete([f"session_{index}" for index in range(101)])
    assert not list(tmp_path.iterdir())
