"""Confined, recoverable management of calibration archives; never permanent deletion."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import uuid

from .experiment import _session_summary

ACTIVE_DIRECTORY = "calibration_sessions"
DELETED_DIRECTORY = "deleted_calibration_sessions"
MISSING = "采集数据不存在或已删除"


def _id(value):
    if (not isinstance(value, str) or not value or len(value) > 100
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value)):
        raise ValueError("采集数据标识格式无效")
    return value


def _linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _root(userdata, name):
    home = Path(userdata).resolve()
    path = home / name
    if _linked(path) or path.resolve().parent != home:
        raise ValueError("采集数据目录存在链接或超出允许位置")
    return path


def _path(userdata, session_id, deleted):
    base = _root(userdata, DELETED_DIRECTORY if deleted else ACTIVE_DIRECTORY)
    path = base / _id(session_id)
    if _linked(path) or path.resolve().parent != base.resolve():
        raise ValueError("采集数据路径存在链接或超出允许目录")
    if path.exists() and not path.is_dir():
        raise ValueError("采集数据位置不是文件夹")
    if path.exists() and path.resolve().name != session_id:
        raise ValueError("采集数据标识大小写与保存目录不一致，请刷新列表")
    return path


def session_path(userdata, session_id, *, include_deleted=False):
    """Resolve sources without allowing deleted archives back into training."""
    active, deleted = _path(userdata, session_id, False), _path(userdata, session_id, True)
    if active.exists() and deleted.exists():
        raise ValueError("采集数据同时存在于活动目录和回收区，无法确定来源")
    path = active if active.is_dir() else deleted if include_deleted and deleted.is_dir() else None
    if path is None:
        raise ValueError(MISSING)
    for name in ("manifest.json", "raw_spectra.jsonl", "raw_force.jsonl", "results.jsonl", "events.jsonl",
                 "optical_sources.jsonl", "peak_frames.jsonl"):
        file = path / name
        if _linked(file) or file.resolve().parent != path.resolve():
            raise ValueError("采集数据文件存在链接或超出允许目录")
    return path


def _ids(values):
    if (not isinstance(values, list) or not values or len(values) > 100
            or any(not isinstance(value, str) for value in values)
            or len({value.casefold() for value in values}) != len(values)):
        raise ValueError("请选择 1～100 条不重复的采集记录")
    return [_id(value) for value in values]


class SessionHistory:
    def __init__(self, userdata, *, opener=None):
        self.userdata = Path(userdata)
        self.lock = threading.RLock()
        self.opener = opener

    def _rows(self, deleted, active_session_id):
        root = _root(self.userdata, DELETED_DIRECTORY if deleted else ACTIVE_DIRECTORY)
        rows = []
        if not root.is_dir():
            return rows
        for path in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not path.is_dir() and not _linked(path):
                continue
            row = {"session_id": path.name, "path": str(path), "deleted": deleted,
                   "settings": {}, "complete": False, "counts": {}, "trainable": False,
                   "deletable": False, "restorable": False, "openable": False, "history_error": None}
            try:
                checked = session_path(self.userdata, path.name, include_deleted=deleted)
                if checked != path:
                    raise ValueError("记录位置与所选列表不一致")
                row.update(openable=True, deletable=not deleted and path.name != active_session_id,
                           restorable=deleted)
                manifest_path = checked / "manifest.json"
                if not manifest_path.is_file():
                    raise ValueError("记录缺少清单，不能训练；可打开位置检查或移入回收区")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                if not isinstance(manifest, dict) or not isinstance(manifest.get("settings"), dict):
                    raise ValueError("采集清单格式无效，不能训练")
                row.update(settings=manifest["settings"], complete=manifest.get("complete") is True,
                           counts=manifest.get("counts", {}), error=manifest.get("error"), **_session_summary(manifest))
                reason = ("记录位于回收区，恢复后才能训练" if deleted else
                          "当前会话仍在采集，不能训练或删除" if path.name == active_session_id else
                          "记录未完整结束，不能训练" if not row["complete"] else
                          "该记录不是点位采集会话" if manifest.get("kind") != "9fbg_calibration_experiment_v1" else
                          "诊断记录不参与训练" if row.get("diagnostic") else
                          "没有合格按压轮次，不能训练" if not row.get("has_training_data") else
                          "C 批是保留数据，不参与训练" if row["settings"].get("batch") not in {"A", "B"} else None)
                row.update(trainable=reason is None, history_error=reason)
            except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
                row["history_error"] = str(exc)
            rows.append(row)
        return rows

    def snapshot(self, *, active_session_id=None):
        with self.lock:
            return {"sessions": self._rows(False, active_session_id),
                    "deleted_sessions": self._rows(True, active_session_id)}

    def open(self, session_id, deleted=False):
        if type(deleted) is not bool:
            raise ValueError("回收区选择必须为布尔值")
        with self.lock:
            path = session_path(self.userdata, session_id, include_deleted=deleted)
            if path != _path(self.userdata, session_id, deleted):
                raise ValueError("所选采集记录已移动，请刷新列表")
            opener = self.opener or getattr(os, "startfile", None)
            if opener is None:
                raise ValueError("当前系统不支持直接打开文件夹")
            opener(str(path))
            return {"opened": True, "path": str(path)}

    def _write_operation(self, record):
        directory = _root(self.userdata, "history_operations")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (record["operation_id"] + ".json")
        temporary = directory / ("." + record["operation_id"] + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _transfer(self, session_ids, *, restore, active_session_id=None):
        values = _ids(session_ids)
        with self.lock:
            plans = []
            for session_id in values:
                if session_id == active_session_id:
                    raise ValueError("正在采集的会话不能删除或恢复")
                source = session_path(self.userdata, session_id, include_deleted=restore)
                if source != _path(self.userdata, session_id, restore):
                    raise ValueError("所选记录已移动，请刷新列表")
                target = _path(self.userdata, session_id, not restore)
                if target.exists():
                    raise ValueError("目标位置已有同名记录，不会覆盖原有数据")
                for child in source.rglob("*"):
                    if _linked(child) or not child.resolve().is_relative_to(source.resolve()):
                        raise ValueError("记录包含目录链接，不能批量移动")
                plans.append((source, target))
            record = {"schema": "9fbg_history_operation_v1", "operation_id": "H" + uuid.uuid4().hex,
                      "created_utc": datetime.now(timezone.utc).isoformat(), "action": "restore" if restore else "delete",
                      "session_ids": values, "moves": [{"from": str(a), "to": str(b)} for a, b in plans],
                      "status": "prepared", "completed_ids": []}
            self._write_operation(record)
            moved = []
            try:
                for source, target in plans:
                    # Both absolute paths were checked against their confined roots.
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        raise ValueError("目标位置已改变，不会覆盖现有数据")
                    source.rename(target)
                    moved.append((source, target))
                    record["completed_ids"].append(source.name)
                record["status"] = "committed"
                self._write_operation(record)
            except Exception as exc:
                failures = []
                for source, target in reversed(moved):
                    try:
                        if source.exists():
                            raise ValueError("原位置已占用")
                        target.rename(source)
                    except Exception as rollback:
                        failures.append(source.name + "：" + str(rollback))
                record.update(status="rollback_failed" if failures else "rolled_back", error=str(exc), rollback_errors=failures)
                try:
                    self._write_operation(record)
                except OSError:
                    pass
                raise ValueError("历史记录移动未完成：" + str(exc) + ("；回退未完成，请查看操作记录：" + "；".join(failures) if failures else "；原有数据已保留")) from exc
            result = self.snapshot(active_session_id=active_session_id)
            result["restored_ids" if restore else "deleted_ids"] = values
            return result

    def delete(self, session_ids, *, active_session_id=None):
        return self._transfer(session_ids, restore=False, active_session_id=active_session_id)

    def restore(self, session_ids, *, active_session_id=None):
        return self._transfer(session_ids, restore=True, active_session_id=active_session_id)
