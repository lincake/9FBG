"""Confined model recycling; source sessions and judgments are never moved."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import re
import stat
import threading
import uuid

REGISTRY = {"M": ("models", "full"), "P": ("pilot_models", "pilot"), "S": ("spectral_models", "spectral_gru")}
RECYCLE = "deleted_models"
OPERATIONS = "model_history_operations"
SCHEMA = "9fbg_model_recycle_v1"


def model_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[MPS][A-Za-z0-9_-]{1,99}", value):
        raise ValueError("模型标识须为 M、P 或 S 开头的有效保存标识")
    return value


def _linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _root(userdata, name):
    source = Path(userdata).absolute()
    if any(_linked(part) for part in (source, *source.parents)):
        raise ValueError("模型用户数据目录不能经过链接")
    home = source.resolve()
    path = home / name
    if _linked(path) or path.resolve().parent != home or path.exists() and not path.is_dir():
        raise ValueError("模型目录存在链接或超出允许位置")
    return path


def _child(base, name):
    path = base / name
    if _linked(base) or _linked(path) or path.resolve().parent != base.resolve():
        raise ValueError("模型路径存在链接或超出允许目录")
    if path.exists() and path.resolve().name != name:
        raise ValueError("模型标识大小写与保存位置不一致，请刷新列表")
    return path


def _exists(path):
    return path.exists() or _linked(path)


def _active_paths(userdata, value):
    result = []
    for directory, _ in REGISTRY.values():
        root = _root(userdata, directory)
        result.append(_child(root, value if directory == "spectral_models" else value + ".json"))
    return result


def model_path(userdata, value):
    """Resolve only the active registry, with no aliases into the recycle area."""
    value = model_id(value)
    paths = _active_paths(userdata, value)
    expected = paths[list(REGISTRY).index(value[0])]
    deleted = _child(_root(userdata, RECYCLE), value)
    present = [path for path in paths if _exists(path)]
    if len(present) > 1 or present and _exists(deleted):
        raise ValueError("同名模型存在多个位置，无法确定来源")
    if not _exists(expected) or present != [expected]:
        raise ValueError("模型不存在、已移入回收区或保存位置不符")
    if value[0] == "S" and not expected.is_dir() or value[0] != "S" and not expected.is_file():
        raise ValueError("模型保存位置的文件类型无效")
    _inventory(expected, hashes=False)
    return expected


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _inventory(path, *, hashes=True):
    if _linked(path) or not path.exists():
        raise ValueError("模型资产不存在或是链接")
    files, directories = {}, []
    entries = [path] if path.is_file() else []
    pending = [path] if path.is_dir() else []
    while pending:
        for child in sorted(pending.pop().iterdir()):
            # Inspect a directory before descending into it, including Windows
            # junctions, so traversal itself cannot leave the checked package.
            if _linked(child) or not child.resolve().is_relative_to(path.resolve()):
                raise ValueError("模型包含目录链接，不能移动或载入")
            entries.append(child)
            if child.is_dir():
                pending.append(child)
    for child in entries:
        if (_linked(child) or not child.resolve().is_relative_to(path.resolve() if path.is_dir() else path.parent.resolve())
                or child.is_file() and child.stat().st_nlink > 1):
            raise ValueError("模型包含链接或歧义文件，不能移动或载入")
        name = child.relative_to(path).as_posix() if path.is_dir() else "$file"
        if child.is_dir():
            directories.append(name)
        elif child.is_file():
            files[name] = {"size": child.stat().st_size,
                           **({"sha256": _file_hash(child)} if hashes else {})}
        else:
            raise ValueError("模型包含不支持的资产类型")
    return {"files": files, "directories": sorted(directories)}


def _read_json(path):
    if _linked(path) or not path.is_file() or not 0 < path.stat().st_size <= 32 * 1024 * 1024:
        raise ValueError("模型清单不存在、是链接或大小无效")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("模型清单须为对象")
    return data


def _describe(userdata, value, path, *, verify_weights=True):
    from .analysis import _METHOD_LABELS, _model_time, _validate_live_pilot, _validate_spectral_sources
    from .inversion import validate_bundle
    from .tracking import ALGORITHM_VERSION
    _inventory(path, hashes=False)
    kind = REGISTRY[value[0]][1]
    if kind == "spectral_gru":
        from .spectral_model import validate_metadata
        data = validate_metadata(_read_json(path / "metadata.json"))
        if data.get("model_id") != value:
            raise ValueError("全谱模型标识与目录不一致")
        weights = path / "weights.pt"
        if not weights.is_file() or not 0 < weights.stat().st_size <= 512 * 1024 * 1024:
            raise ValueError("全谱权重不存在或大小无效")
        if verify_weights and _file_hash(weights) != data["weights_sha256"]:
            raise ValueError("全谱模型权重哈希不匹配")
        _validate_spectral_sources(userdata, data)
    else:
        data = _read_json(path)
        _validate_live_pilot(data, userdata) if kind == "pilot" else validate_bundle(data)
        if data.get("algorithm_version") != ALGORITHM_VERSION or data.get("source_kind") != "hardware":
            raise ValueError("模型固定算法或真实来源定义不符")
    method = data.get("method", "spectral_gru") if kind == "spectral_gru" else data.get("primary_method", "linear")
    created = _model_time(data.get("created_utc"))
    points = data.get("trained_point_ids", data.get("point_ids", []))
    return {"model_id": value, "model_kind": kind, "method": method,
            "input_kind": data.get("input_kind"),
            "method_label": _METHOD_LABELS.get(method, method), "created_utc": data.get("created_utc"),
            "created_local_minute": created.astimezone().strftime("%Y-%m-%d %H:%M") if created else None,
            "point_ids": deepcopy(points), "trained_point_ids": deepcopy(points),
            "model_sha256": data.get("model_sha256", data.get("bundle_sha256")),
            "research_only": True, "formal_ready": False}


def _write_json(path, value):
    temporary = path.parent / ("." + path.name + ".tmp_" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ids(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 100
            or any(not isinstance(value, str) for value in values)
            or len({value.casefold() for value in values}) != len(values)):
        raise ValueError("请选择 1～100 个不重复的模型")
    return [model_id(value) for value in values]


def _listing_signature(userdata):
    """Stat-only invalidation for the read-only catalog, never an IO attestation.

    Include every model asset and both source locations. Source raw file content
    is not read by the catalog, but replacing a raw file with a link must still
    invalidate it. Do not descend into links or Windows junctions.
    """
    signature = []

    def inspect(path, *, content=True):
        try:
            value = path.lstat()
        except FileNotFoundError:
            signature.append((str(path), None))
            return False
        attributes = getattr(value, "st_file_attributes", 0)
        linked = stat.S_ISLNK(value.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        identity = (str(path), value.st_mode, value.st_dev, value.st_ino, value.st_nlink, attributes)
        signature.append(identity + ((value.st_size, value.st_mtime_ns, value.st_ctime_ns) if content else ()))
        return stat.S_ISDIR(value.st_mode) and not linked

    for name in (*[entry[0] for entry in REGISTRY.values()], RECYCLE):
        root = _root(userdata, name)
        pending = [root] if inspect(root) else []
        while pending:
            for path in sorted(pending.pop().iterdir()):
                if inspect(path):
                    pending.append(path)
    for name in ("calibration_sessions", "deleted_calibration_sessions"):
        root = _root(userdata, name)
        if inspect(root):
            for directory in sorted(root.iterdir()):
                if inspect(directory):
                    for filename in ("manifest.json", "raw_spectra.jsonl", "raw_force.jsonl", "results.jsonl", "events.jsonl",
                                     "optical_sources.jsonl", "peak_frames.jsonl"):
                        inspect(directory / filename, content=filename == "manifest.json")
    return tuple(signature)


class ModelHistory:
    def __init__(self, userdata):
        self.userdata = Path(userdata)
        self.lock = threading.RLock()
        self._listing_cache = None

    def cached_snapshot(self, models_loader, *, in_use_model_id=None, blocked_reason=None):
        """Reuse unchanged catalog descriptions; execution paths revalidate assets."""
        with self.lock:
            before = _listing_signature(self.userdata)
            cached = self._listing_cache
            if cached is None or cached[0] != before:
                value = self.snapshot(models=models_loader())
                after = _listing_signature(self.userdata)
                # A concurrent external writer must not attach a mixed scan to
                # either generation's fingerprint. The next read rescans it.
                self._listing_cache = (after, deepcopy(value)) if before == after else None
            else:
                value = deepcopy(cached[1])
            for row in value["models"]:
                if row["loadable"]:
                    reason = blocked_reason or ("当前模型正在推理，请先结束推理" if row["model_id"] == in_use_model_id else None)
                    row.update(deletable=reason is None, history_error=reason)
            for row in value["deleted_models"]:
                if "deleted_utc" in row:
                    reason = blocked_reason or row["history_error"]
                    row.update(restorable=reason is None, history_error=reason)
            return value

    def _locations(self, value):
        directory = _root(self.userdata, REGISTRY[value[0]][0])
        active = _child(directory, value if value[0] == "S" else value+".json")
        container = _child(_root(self.userdata, RECYCLE), value)
        payload = _child(container, "payload" if value[0] == "S" else "payload.json")
        return active, container, payload

    def _archive(self, value):
        container = _child(_root(self.userdata, RECYCLE), model_id(value))
        if not container.is_dir():
            raise ValueError("回收模型不存在")
        manifest = _read_json(_child(container, "manifest.json"))
        body = {key: item for key, item in manifest.items() if key != "manifest_sha256"}
        relative = REGISTRY[value[0]][0] + "/" + (value if value[0] == "S" else value + ".json")
        payload_name = "payload" if value[0] == "S" else "payload.json"
        if (manifest.get("schema") != SCHEMA or manifest.get("model_id") != value
                or manifest.get("original_relative_path") != relative or manifest.get("payload_name") != payload_name
                or manifest.get("manifest_sha256") != _digest(body)):
            raise ValueError("回收模型清单身份、路径或哈希无效")
        if {p.name for p in container.iterdir()} != {"manifest.json", payload_name}:
            raise ValueError("回收模型包含未知资产或未完整结束")
        payload = _child(container, payload_name)
        if _inventory(payload) != manifest.get("inventory"):
            raise ValueError("回收模型资产哈希不匹配")
        actual = _describe(self.userdata, value, payload)
        summary = manifest.get("model_summary")
        if (not isinstance(summary, dict) or any(summary.get(key) != actual[key]
                for key in ("model_id", "model_kind", "method", "model_sha256", "point_ids"))):
            raise ValueError("回收模型摘要与原模型不一致")
        try:
            if datetime.fromisoformat(manifest["deleted_utc"]).utcoffset() is None:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ValueError("回收模型删除时间无效") from None
        return container, payload, manifest

    def snapshot(self, *, models=None, in_use_model_id=None, blocked_reason=None):
        with self.lock:
            rich = {row["model_id"]: row for row in models or []}
            result = {"models": [], "deleted_models": []}
            for prefix, (directory, kind) in REGISTRY.items():
                root = _root(self.userdata, directory)
                if not root.is_dir():
                    continue
                for path in root.iterdir():
                    value = path.name if prefix == "S" else path.stem
                    if path.name.startswith(".") or not value.startswith(prefix) or prefix != "S" and path.suffix != ".json":
                        continue
                    row = {**deepcopy(rich.get(value, {})), "model_id": value, "model_kind": kind,
                           "deleted": False, "loadable": False, "deletable": False, "restorable": False, "history_error": None}
                    try:
                        checked = model_path(self.userdata, value)
                        row.update(_describe(self.userdata, value, checked))
                        row["loadable"] = True
                        reason = blocked_reason or ("当前模型正在推理，请先结束推理" if value == in_use_model_id else None)
                        row.update(deletable=reason is None, history_error=reason)
                    except (ValueError, OSError, TypeError, KeyError) as exc:
                        row["history_error"] = str(exc)
                    result["models"].append(row)
            root = _root(self.userdata, RECYCLE)
            if root.is_dir():
                for path in root.iterdir():
                    if path.name.startswith("."):
                        continue
                    row = {"model_id": path.name, "deleted": True, "loadable": False, "deletable": False,
                           "restorable": False, "history_error": None}
                    try:
                        _, _, manifest = self._archive(path.name)
                        row.update(deepcopy(manifest["model_summary"]), deleted_utc=manifest["deleted_utc"])
                        created = datetime.fromisoformat(manifest["deleted_utc"])
                        row["deleted_local_minute"] = created.astimezone().strftime("%Y-%m-%d %H:%M")
                        conflict = any(_exists(p) for p in _active_paths(self.userdata, path.name))
                        reason = blocked_reason or ("活动位置已有同名模型，不能覆盖恢复" if conflict else None)
                        row.update(restorable=reason is None, history_error=reason)
                    except (ValueError, OSError, TypeError, KeyError) as exc:
                        row["history_error"] = str(exc)
                    result["deleted_models"].append(row)
            for rows in result.values():
                rows.sort(key=lambda row: (row.get("created_utc") or "", row["model_id"]), reverse=True)
            return result

    def _transfer(self, values, *, restore, in_use_model_id=None):
        values = _ids(values)
        with self.lock:
            # Even an interrupted or rolled-back operation invalidates the UI
            # cache; deletion/restoration always uses complete fresh validation.
            self._listing_cache = None
            plans = []
            for value in values:
                if value == in_use_model_id:
                    raise ValueError("当前模型正在推理，请先结束推理，再删除模型")
                if restore:
                    container, payload, manifest = self._archive(value)
                    if any(_exists(p) for p in _active_paths(self.userdata, value)):
                        raise ValueError("活动位置已有同名模型，不能覆盖恢复")
                    directory = _root(self.userdata, REGISTRY[value[0]][0])
                    active = _child(directory, value if value[0] == "S" else value + ".json")
                else:
                    active = model_path(self.userdata, value)
                    summary = _describe(self.userdata, value, active)
                    container = _child(_root(self.userdata, RECYCLE), value)
                    if _exists(container):
                        raise ValueError("回收区已有同名模型，不会覆盖")
                    payload = container / ("payload" if value[0] == "S" else "payload.json")
                    manifest = {"schema": SCHEMA, "model_id": value, "model_summary": summary,
                                "deleted_utc": datetime.now(timezone.utc).isoformat(),
                                "original_relative_path": active.relative_to(self.userdata.resolve()).as_posix(),
                                "payload_name": payload.name, "inventory": _inventory(active)}
                    manifest["manifest_sha256"] = _digest(manifest)
                plans.append((value, active, container, payload, manifest))
            logs = _root(self.userdata, OPERATIONS)
            record = {"schema": "9fbg_model_history_operation_v1", "operation_id": "MH"+uuid.uuid4().hex,
                      "created_utc": datetime.now(timezone.utc).isoformat(), "action": "restore" if restore else "delete",
                      "model_ids": values, "status": "prepared", "completed_ids": [],
                      "archives": {value: manifest for value, _, _, _, manifest in plans}}
            logs.mkdir(parents=True, exist_ok=True)
            def write_record():
                # The operation log is confined even if a directory was changed
                # while another asset was being hashed or moved.
                checked_logs = _root(self.userdata, OPERATIONS)
                _write_json(_child(checked_logs, record["operation_id"]+".json"), record)
            write_record()
            moved, created = [], []
            try:
                for plan in plans:
                    value, active, container, payload, manifest = plan
                    if self._locations(value) != (active, container, payload):
                        raise ValueError("模型位置在操作前改变")
                    source, target = (payload, active) if restore else (active, payload)
                    if _inventory(source) != manifest["inventory"]:
                        raise ValueError("模型资产在操作前改变，整批操作已取消")
                    if _exists(target):
                        raise ValueError("目标位置已改变，不会覆盖模型")
                    if restore:
                        target.parent.mkdir(parents=True, exist_ok=True)
                    else:
                        container.parent.mkdir(parents=True, exist_ok=True)
                        container.mkdir()
                        created.append(container)
                        _write_json(_child(container, "manifest.json"), manifest)
                    # Re-check all roots and nodes immediately before rename.
                    if self._locations(value) != (active, container, payload):
                        raise ValueError("模型位置在移动前改变")
                    source.rename(target)
                    moved.append(plan)
                    record["completed_ids"].append(value)
                if restore:
                    for _, _, container, _, _ in plans:
                        checked = _child(_root(self.userdata, RECYCLE), container.name)
                        _child(checked, "manifest.json").unlink()
                        container.rmdir()
                record["status"] = "committed"
                write_record()
            except Exception as exc:
                failures = []
                for value, active, container, payload, manifest in reversed(moved):
                    try:
                        self._locations(value)
                        source, target = (active, payload) if restore else (payload, active)
                        if _exists(target):
                            raise ValueError("原位置已占用")
                        if restore:
                            container.mkdir(parents=True, exist_ok=True)
                            _write_json(_child(container, "manifest.json"), manifest)
                        self._locations(value)
                        source.rename(target)
                    except Exception as rollback:
                        failures.append(value+"："+str(rollback))
                for container in created:
                    try:
                        checked = _child(_root(self.userdata, RECYCLE), container.name)
                        if checked.exists() and not any(p.name.startswith("payload") for p in checked.iterdir()):
                            _child(checked, "manifest.json").unlink(missing_ok=True)
                            container.rmdir()
                    except (OSError, ValueError) as cleanup:
                        failures.append(container.name+"："+str(cleanup))
                record.update(status="rollback_failed" if failures else "rolled_back", error=str(exc), rollback_errors=failures)
                try:
                    write_record()
                except (OSError, ValueError):
                    pass
                raise ValueError("模型移动未完成："+str(exc)+( "；请查看操作记录："+"；".join(failures) if failures else "；原模型已保留")) from exc
            result = self.snapshot()
            result["restored_ids" if restore else "deleted_ids"] = values
            return result

    def delete(self, model_ids, *, in_use_model_id=None):
        return self._transfer(model_ids, restore=False, in_use_model_id=in_use_model_id)

    def restore(self, model_ids):
        return self._transfer(model_ids, restore=True)
