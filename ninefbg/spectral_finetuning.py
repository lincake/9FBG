"""Immutable parent/source plans for weights-only cumulative fine-tuning."""
from __future__ import annotations

from copy import deepcopy
import json


def _parent(userdata, value, *, checkpoint=None):
    from .model_history import model_id, model_path
    from .spectral_data import _digest
    from .spectral_model import validate_metadata
    value = model_id(value)
    if not value.startswith("S"):
        raise ValueError("继承微调只接受已保存的全谱 S 模型")
    path = model_path(userdata, value)
    metadata = validate_metadata(json.loads((path / "metadata.json").read_text(encoding="utf-8")))
    if metadata.get("model_id") != value:
        raise ValueError("父模型标识与保存目录不一致")
    weights_hash = _digest(path / "weights.pt", checkpoint)
    if weights_hash != metadata["weights_sha256"]:
        raise ValueError("父模型权重哈希不匹配")
    snapshot = {"model_id": value, "model_sha256": metadata["model_sha256"],
                "weights_sha256": weights_hash, "metadata_file_sha256": _digest(path / "metadata.json", checkpoint)}
    return path, metadata, snapshot


def _source(userdata, value, *, expected=None, checkpoint=None):
    from .history import session_path
    from .spectral_data import RAW_FILES, _digest
    from .peak_drift_data import is_compact, RECORDING_SCHEMA, FILES
    try:
        path = session_path(userdata, value, include_deleted=False)
        file = path / "manifest.json"
        if not file.is_file():
            raise ValueError("会话尚未结束保存，不能读取活动 JSONL 训练")
        manifest = json.loads(file.read_text(encoding="utf-8-sig"))
        if (manifest.get("kind") != "9fbg_calibration_experiment_v1" or manifest.get("complete") is not True
                or manifest.get("error") or manifest.get("overflow", 0)
                or manifest.get("settings", {}).get("batch") not in {"A", "B"}):
            raise ValueError("训练仅接受完整保存且无丢盘的 A/B 会话；C 批保留独立测试")
        files = manifest.get("files", {})
        compact = is_compact(manifest)
        required = FILES if compact else RAW_FILES
        if set(files) != required or any(not (path / name).is_file() for name in required):
            raise ValueError("会话缺少完整原始四文件与摘要")
        snapshot = {"session_id": value, "manifest_sha256": _digest(file, checkpoint),
                    "files": deepcopy(files)}
        if compact:
            snapshot["recording_schema"] = RECORDING_SCHEMA
        if expected is not None and any(snapshot[key] != expected.get(key) for key in snapshot):
            raise ValueError("来源清单或原始文件摘要已改变")
        return snapshot
    except (ValueError, OSError) as exc:
        raise ValueError("训练来源 " + str(value) + " 不可用：" + str(exc)) from exc


def prepare_training(userdata, selected_ids, options, *, explicit_options=None):
    """Validate before spawning; raw hashes are rechecked cooperatively by loader.

    options is the checked mutable worker-options dictionary. Omitted window
    settings inherit the parent; an explicit change cannot change old samples.
    """
    from .history import _ids
    selected_ids = _ids(selected_ids)
    explicit = options if explicit_options is None else explicit_options
    parent = None
    old_ids, expected_sources = [], {}
    if options["training_mode"] == "finetune":
        _, parent, parent_snapshot = _parent(userdata, options["base_model_id"])
        requested_kind = "delta_lambda_9" if options.get("method") == "drift_gru" else "delta_S"
        if parent["input_kind"] != requested_kind:
            raise ValueError("九路漂移与 512 点全谱模型不能互相继承权重，请选择相同输入类型的父模型")
        old_ids = _ids([source["session_id"] for source in parent["acquisition_provenance"]["sources"]])
        expected_sources = {source["session_id"]: source for source in parent["acquisition_provenance"]["sources"]}
        inherited = {"max_spectrum_gap_ms": parent["window_rule"]["max_gap_ms"],
                     "adapt_legacy_unloaded": parent.get("training_options", {}).get("adapt_legacy_unloaded", False)}
        if type(inherited["adapt_legacy_unloaded"]) is not bool:
            raise ValueError("父模型旧会话无载适配规则无效")
        for key, value in inherited.items():
            if key in explicit and options[key] != value:
                raise ValueError("继承微调不能改变父模型的 " + key + " 样本规则")
            options[key] = value
    else:
        parent_snapshot = None
    merged_ids = list(dict.fromkeys(old_ids + selected_ids))
    _ids(merged_ids)
    new_ids = [value for value in selected_ids if value not in old_ids]
    if parent is not None and not new_ids:
        raise ValueError("继承微调至少需要一个父模型尚未使用的新增会话")
    snapshots = [_source(userdata, value, expected=expected_sources.get(value)) for value in merged_ids]
    if parent is not None:
        from .spectral_model import has_contact_head
        if not has_contact_head(parent):
            from .history import session_path
            for value in merged_ids:
                manifest = json.loads((session_path(userdata, value, include_deleted=False) / "manifest.json").read_text(encoding="utf-8-sig"))
                if any(trial.get("point_id") == "NO_CONTACT" or trial.get("sample_label") == "no_contact"
                       for trial in manifest.get("final", {}).get("trials", [])):
                    raise ValueError("旧版模型没有独立接触头，不能用无接触标签继承微调；请选择从头训练生成新版接触模型")
    plan = {"schema": "spectral_training_plan_v1", "training_mode": options["training_mode"],
            "base_model_id": options["base_model_id"], "parent_snapshot": parent_snapshot,
            "parent_model_sha256": parent_snapshot["model_sha256"] if parent_snapshot else None,
            "selected_session_ids": selected_ids, "new_session_ids": new_ids,
            "merged_session_ids": merged_ids, "sources": snapshots}
    return merged_ids, plan


def verify_plan(userdata, plan, *, checkpoint=None, load_weights=False, verify_raw=False):
    """Reject replacement after API submission; never skip an unavailable source."""
    for source in plan["sources"]:
        if checkpoint:
            checkpoint()
        _source(userdata, source["session_id"], expected=source, checkpoint=checkpoint)
        if verify_raw:
            from .history import session_path
            from .spectral_data import _digest
            path = session_path(userdata, source["session_id"], include_deleted=False)
            if any(_digest(path / name, checkpoint) != digest for name, digest in source["files"].items()):
                raise ValueError("训练来源原始文件在任务期间已改变：" + source["session_id"])
    if plan["training_mode"] != "finetune":
        return None, None
    path, metadata, snapshot = _parent(userdata, plan["base_model_id"], checkpoint=checkpoint)
    if snapshot != plan["parent_snapshot"]:
        raise ValueError("父模型在训练提交后已改变，拒绝继承替换后的权重")
    if load_weights:
        from .spectral_model import load_bundle
        loaded, network = load_bundle(path)
        if loaded != metadata:
            raise ValueError("父模型在权重读取期间已改变")
        return metadata, network
    return metadata, None


def status_fields(plan):
    return {key: deepcopy(plan[key]) for key in ("training_mode", "base_model_id", "parent_model_sha256",
            "selected_session_ids", "new_session_ids", "merged_session_ids")}
