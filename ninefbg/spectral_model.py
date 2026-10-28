"""Versioned CPU spectral model bundles. PyTorch is optional and imported on use."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import uuid

SCHEMA = "spectral_model_v1"
CONTACT_SCHEMA = "spectral_model_v2"
MODEL_KIND = "spectral_gru"
AXES = ["Fx", "Fy", "Fz", "Mx", "My", "Mz"]
UNITS = ["N", "N", "N", "N·m", "N·m", "N·m"]
ARCHITECTURE = {"input_size": 512, "frame_mlp": [128, 64], "activation": "ReLU",
                "gru_hidden_size": 64, "gru_layers": 1, "bidirectional": False,
                "position_classes": 25, "wrench_outputs": 6, "window_hidden_init": "zero"}
DRIFT_ARCHITECTURE = {**ARCHITECTURE, "input_size": 9, "frame_mlp": [32, 32], "gru_hidden_size": 32}
INPUT_KINDS = {"delta_S", "delta_lambda_9"}
CONTACT_DEFINITION = {"version": "binary_contact_head_v1", "no_contact_label": "NO_CONTACT",
                      "supervision_version": "contact_label_v1",
                      "outputs": 1, "loss": "binary_cross_entropy_with_logits", "loss_weight": 1.,
                      "on_probability": .6, "off_probability": .4,
                      "threshold_source": "declared_research_default", "hardware_calibrated": False}


def architecture_for(input_kind="delta_S", *, contact_head=False):
    if input_kind not in INPUT_KINDS:
        raise ValueError("模型输入类型不兼容")
    value = deepcopy(DRIFT_ARCHITECTURE if input_kind == "delta_lambda_9" else ARCHITECTURE)
    if contact_head:
        value["contact_outputs"] = 1
    return value


def has_contact_head(metadata):
    return metadata.get("schema") == CONTACT_SCHEMA


def torch_module():
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("全谱 MLP＋GRU 需要可选 CPU PyTorch；请先运行 9FBG-Setup-ML.cmd") from exc
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1114:
            raise RuntimeError("模型运行库初始化失败（Windows 错误 1114）。请关闭当前 9FBG 窗口，从固定快捷方式重新打开后再载入模型；若仍失败，请检查可选 CPU PyTorch 运行环境。") from exc
        raise RuntimeError("CPU PyTorch 模型运行库加载失败，请检查本程序的可选机器学习运行环境。") from exc
    return torch


def build_network(input_kind="delta_S", *, contact_head=False):
    torch = torch_module()
    architecture = architecture_for(input_kind, contact_head=contact_head)
    input_size = architecture["input_size"]
    mlp1, mlp2 = architecture["frame_mlp"]
    hidden_size = architecture["gru_hidden_size"]

    class SpectralGRU(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.frame_mlp = torch.nn.Sequential(torch.nn.Linear(input_size, mlp1), torch.nn.ReLU(),
                                                 torch.nn.Linear(mlp1, mlp2), torch.nn.ReLU())
            self.gru = torch.nn.GRU(mlp2, hidden_size, num_layers=1, batch_first=True, bidirectional=False)
            self.position_head = torch.nn.Linear(hidden_size, 25)
            self.wrench_head = torch.nn.Linear(hidden_size, 6)
            if contact_head:
                self.contact_head = torch.nn.Linear(hidden_size, 1)

        def forward(self, spectra):
            if spectra.ndim != 3 or spectra.shape[1:] != (16, input_size):
                raise ValueError("模型输入必须为 [批次,16," + str(input_size) + "]")
            features = self.frame_mlp(spectra)
            # Each overlapping window starts with a fresh zero hidden state.
            _, hidden = self.gru(features)
            last = hidden[-1]
            result = self.position_head(last), self.wrench_head(last)
            return (*result, self.contact_head(last).squeeze(-1)) if contact_head else result

    return SpectralGRU()


def masked_losses(logits, wrench, target_position, target_wrench, position_mask, wrench_mask,
                  contact_logits=None, target_contact=None, contact_mask=None):
    torch = torch_module()
    if position_mask.any():
        position_loss = torch.nn.functional.cross_entropy(logits[position_mask], target_position[position_mask])
    else:
        position_loss = logits.sum() * 0
    if wrench_mask.any():
        wrench_loss = torch.nn.functional.smooth_l1_loss(wrench[wrench_mask], target_wrench[wrench_mask])
    else:
        wrench_loss = wrench.sum() * 0
    if contact_logits is None:
        return position_loss, wrench_loss
    if target_contact is None or contact_mask is None:
        raise ValueError("接触头缺少独立二元监督与有效掩码")
    contact_loss = (torch.nn.functional.binary_cross_entropy_with_logits(
        contact_logits[contact_mask], target_contact[contact_mask]) if contact_mask.any()
        else contact_logits.sum() * 0)
    return position_loss, wrench_loss, contact_loss


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def metadata_digest(value):
    return canonical_digest({key: item for key, item in value.items()
                             if key not in {"metadata_sha256", "model_sha256"}})


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _vector(value, count, label, positive=False):
    if (not isinstance(value, list) or len(value) != count or
            any(type(item) not in (int, float) or not math.isfinite(item) or positive and item <= 0 for item in value)):
        raise ValueError(label + "格式无效")


def validate_metadata(metadata):
    if not isinstance(metadata, dict) or metadata.get("schema") not in {SCHEMA, CONTACT_SCHEMA} or metadata.get("model_kind") != MODEL_KIND:
        raise ValueError("全谱模型格式或类型不兼容")
    input_kind = metadata.get("input_kind")
    contact = has_contact_head(metadata)
    if input_kind not in INPUT_KINDS or metadata.get("architecture") != architecture_for(input_kind, contact_head=contact) or metadata.get("window_size") != 16:
        raise ValueError("全谱网络或输入规则不兼容")
    if contact and metadata.get("contact_definition") != CONTACT_DEFINITION:
        raise ValueError("接触头定义或概率迟滞门槛不兼容")
    if contact:
        from .spectral_data import CONTACT_LABEL_RULE
        if metadata.get("contact_label_rule") != CONTACT_LABEL_RULE:
            raise ValueError("接触正负样本监督规则不兼容")
    if not contact and "contact_definition" in metadata:
        raise ValueError("旧模型不能声明未训练的独立接触头")
    drift = input_kind == "delta_lambda_9"
    if drift and (metadata.get("method") != "drift_gru" or metadata.get("input_units") != "pm"
                  or metadata.get("fbg_ids") != [f"FBG{index}" for index in range(1, 10)]
                  or not isinstance(metadata.get("peak_definition"), dict)):
        raise ValueError("九路漂移模型缺少固定光栅、提峰定义或 pm 单位")
    if drift:
        definition = metadata["peak_definition"]
        if (set(definition) != {"algorithm_version", "config_version", "axis_sha256", "preprocessing", "feature_schema"}
                or definition.get("feature_schema") != "nine_fbg_centers_v1"
                or any(not isinstance(definition.get(key), str) or not definition[key]
                       for key in ("algorithm_version", "config_version"))
                or definition.get("axis_sha256") != canonical_digest(metadata.get("wavelength_nm"))
                or definition.get("preprocessing") != metadata.get("preprocessing")):
            raise ValueError("九路模型的提峰定义与真实波长轴或预处理不一致")
    if metadata.get("axes") != AXES or metadata.get("units") != UNITS or metadata.get("wrench_reference") != "PX6D_native_axes_and_origin":
        raise ValueError("六维坐标、轴序或单位不兼容")
    if metadata.get("reference_rule") != ("nine_fbg_preload_median_v1" if drift else "spectrum_unloaded_median_v1") or metadata.get("formal_ready") is not False:
        raise ValueError("全谱参考规则或研究模型资格不兼容")
    _vector(metadata.get("wavelength_nm"), 512, "真实 512 点波长轴")
    axis = metadata["wavelength_nm"]
    if not all(a < b for a, b in zip(axis, axis[1:])):
        raise ValueError("全谱波长轴必须严格递增")
    if not isinstance(metadata.get("preprocessing"), dict):
        raise ValueError("模型缺少光谱预处理定义")
    if type(metadata.get("sample_rate_hz")) not in (int, float) or not 0 < metadata["sample_rate_hz"] <= 200:
        raise ValueError("模型采样设置无效")
    for key, count in (("input_normalization", 9 if drift else 512), ("output_normalization", 6)):
        normalizer = metadata.get(key)
        if not isinstance(normalizer, dict):
            raise ValueError("模型缺少训练集合归一化")
        _vector(normalizer.get("mean"), count, key)
        _vector(normalizer.get("scale"), count, key, positive=True)
        if normalizer.get("fitted_split") != "train":
            raise ValueError("模型归一化必须仅由固定训练集合拟合")
        floors = ([.1] * 9 if drift else [1.] * 512) if key == "input_normalization" else [.05, .05, .05, .001, .001, .001]
        if any(actual < floor for actual, floor in zip(normalizer["scale"], floors)):
            raise ValueError("模型归一化尺度小于所声明规则下限")
    points = metadata.get("trained_point_ids")
    if (not isinstance(points, list) or not points or len(set(points)) != len(points) or
            any(not isinstance(point, str) or not re.fullmatch(r"P(?:0[1-9]|1[0-9]|2[0-5])", point) for point in points)):
        raise ValueError("已训练点位范围无效")
    provenance = metadata.get("acquisition_provenance")
    if (not isinstance(provenance, dict) or not isinstance(provenance.get("physical_definition"), dict) or
            not isinstance(provenance.get("sources"), list) or not provenance["sources"]):
        raise ValueError("模型缺少原始来源定义")
    physical = provenance["physical_definition"]
    if (not isinstance(physical.get("finger_id"), str) or not physical["finger_id"].strip()
            or physical.get("axes") != AXES or physical.get("units") != UNITS
            or physical.get("wrench_reference") != "PX6D_native_axes_and_origin"
            or not isinstance(physical.get("conditions"), dict)):
        raise ValueError("模型样件或六维物理定义无效")
    grid = physical.get("point_grid")
    if (not isinstance(grid, list) or len(grid) != 25
            or [point.get("id") if isinstance(point, dict) else None for point in grid] != [f"P{index:02d}" for index in range(1, 26)]):
        raise ValueError("模型必须保存完整固定 P01～P25 点阵")
    coordinate = physical.get("coordinate_definition")
    if coordinate is not None:
        from .experiment import coordinate_definition, default_point_grid
        if coordinate != coordinate_definition() or grid != default_point_grid():
            raise ValueError("模型点阵坐标定义不兼容")
    window = metadata.get("window_rule")
    if (not isinstance(window, dict) or window.get("version") != ("nine_fbg_causal_windows_v1" if drift else "spectral_causal_windows_v1")
            or window.get("length") != 16 or window.get("target") != "last_frame" or window.get("hidden_init") != "zero"
            or type(window.get("max_gap_ms")) not in (int, float) or not math.isfinite(window["max_gap_ms"])
            or not 40 <= window["max_gap_ms"] <= (120 if drift else 1000)):
        raise ValueError("模型因果窗口规则不兼容")
    from .spectral_data import SYNC_RULE, LEGACY_SYNC_RULE
    if metadata.get("sync_rule") not in (SYNC_RULE, LEGACY_SYNC_RULE):
        raise ValueError("模型六维同步规则不兼容")
    required = {"raw_spectra.jsonl", "raw_force.jsonl", "results.jsonl", "events.jsonl"}
    from .peak_drift_data import RECORDING_SCHEMA as COMPACT_SCHEMA, FILES as COMPACT_FILES
    sha = lambda value: isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    for source in provenance["sources"]:
        compact = isinstance(source, dict) and source.get("recording_schema") == COMPACT_SCHEMA
        source_files = COMPACT_FILES if compact else required
        if (not isinstance(source, dict) or not isinstance(source.get("session_id"), str) or
                not sha(source.get("manifest_sha256")) or not isinstance(source.get("files"), dict) or
                set(source["files"]) != source_files or not all(sha(item) for item in source["files"].values())
                or source.get("recording_schema") not in (None, COMPACT_SCHEMA)
                or compact and (not drift or source.get("raw_spectra") != 0
                    or source.get("source_verification") != "saved_peak_centers_and_optical_acquisition_attestations")):
            raise ValueError("模型原始来源哈希格式无效")
        if source.get("protocol") in {"timed_wrench_v1", "force_target_wrench_v1"} and metadata["sample_rate_hz"] != 25:
            raise ValueError("全谱六维模型必须保存 25 Hz 光谱请求设置")
    sampling = metadata.get("training_sampling")
    if sampling is not None and (not isinstance(sampling, dict) or sampling.get("schema") not in {"spectral_train_sampling_v1", "spectral_train_sampling_v2"}
            or sampling.get("applies_to") != "force_target_wrench_v1" or sampling.get("fitted_split") != "train"
            or sampling.get("force_only_to_contact_max_ratio") != 1.
            or sampling.get("method") != ("per_trial_stage_balanced_uniform_time_with_explicit_no_contact"
                if sampling.get("schema") == "spectral_train_sampling_v2" else "per_trial_stage_balanced_uniform_time")
            or sampling.get("schema") == "spectral_train_sampling_v2" and (
                not contact or sampling.get("explicit_no_contact_policy") != "global_contact_budget_trial_balanced_uniform_time")
            or sampling.get("validation_sampling") != "complete_natural_distribution"):
        raise ValueError("模型训练窗限比规则不兼容")
    split = metadata.get("split")
    if (not isinstance(split, dict) or split.get("version") != "spectral_group_split_v1" or
            not isinstance(split.get("assignments"), dict) or
            not all(value in {"train", "validation", "test"} for value in split["assignments"].values())):
        raise ValueError("模型缺少固定分组划分")
    if (split.get("group_mode") not in {"session", "baseline_component"}
            or not isinstance(split.get("trial_assignments"), dict)
            or not split["trial_assignments"] or not isinstance(split.get("source_manifests"), dict)
            or any(value not in {"train", "validation", "test"} for value in split["trial_assignments"].values())
            or not all(sha(value) for value in split["source_manifests"].values())):
        raise ValueError("模型试次分组来源格式无效")
    train_ids, val_ids = metadata.get("training_trial_ids"), metadata.get("validation_trial_ids")
    if (not isinstance(train_ids, list) or not train_ids or not isinstance(val_ids, list)
            or not all(isinstance(value, str) for value in train_ids + val_ids)
            or len(set(train_ids)) != len(train_ids) or len(set(val_ids)) != len(val_ids)
            or set(train_ids) & set(val_ids)
            or any(split["trial_assignments"].get(value) != "train" for value in train_ids)
            or any(split["trial_assignments"].get(value) != "validation" for value in val_ids)):
        raise ValueError("模型训练与验证试次划分冲突")
    if not isinstance(metadata.get("evaluation"), dict):
        raise ValueError("模型缺少独立验证摘要")
    if (metadata["evaluation"].get("independent_validation") is not bool(val_ids)
            or metadata["evaluation"].get("hardware_accuracy_qualified") is not False):
        raise ValueError("模型验证声明与固定划分或研究资格不一致")
    # Older spectral_model_v1 bundles remain loadable. New training deliveries
    # explicitly distinguish a new fit from weights-only parent inheritance.
    if "training_mode" in metadata:
        mode, base = metadata["training_mode"], metadata.get("base_model_id")
        parent_hash = metadata.get("parent_model_sha256")
        if (not isinstance(mode, str) or mode not in {"scratch", "finetune"} or mode == "scratch" and (base is not None or parent_hash is not None)
                or mode == "finetune" and (not isinstance(base, str) or not re.fullmatch(r"S[A-Za-z0-9_-]{1,99}", base)
                                           or not sha(parent_hash))):
            raise ValueError("模型训练模式或父模型身份无效")
        options, lineage = metadata.get("training_options"), metadata.get("training_lineage")
        if (not isinstance(options, dict) or options.get("training_mode") != mode or options.get("base_model_id") != base
                or not isinstance(lineage, dict) or lineage.get("schema") != "spectral_weights_lineage_v1"
                or lineage.get("weights_only") is not True or lineage.get("checkpoint_resume") is not False
                or lineage.get("normalization_source") != ("parent_model" if mode == "finetune" else "train_split")
                or lineage.get("normalization_parent_sha256") != parent_hash):
            raise ValueError("模型继承与归一化来源声明无效")
        from .history import _ids
        fields = ("training_mode", "base_model_id", "parent_model_sha256", "selected_session_ids", "new_session_ids", "merged_session_ids")
        if any(lineage.get(key) != metadata.get(key) for key in fields):
            raise ValueError("模型累计来源与继承记录不一致")
        selected, new, merged = (metadata.get(key) for key in ("selected_session_ids", "new_session_ids", "merged_session_ids"))
        for values in (selected, new, merged):
            _ids(values)
        if (not set(new).issubset(selected) or not set(selected).issubset(merged)
                or set(merged) != {source["session_id"] for source in provenance["sources"]}
                or len(merged) != len(provenance["sources"]) or mode == "scratch" and selected != merged):
            raise ValueError("模型累计会话来源格式无效")
        comparison = metadata["evaluation"].get("finetuning_comparison")
        if mode == "finetune":
            if (not isinstance(comparison, dict) or comparison.get("schema") != "spectral_parent_child_validation_v1"
                    or type(comparison.get("available")) is not bool
                    or comparison.get("normalization_inherited") is not True or comparison.get("checkpoint_resume") is not False
                    or not isinstance(comparison.get("validation_trial_ids"), list)
                    or not set(comparison["validation_trial_ids"]).issubset(val_ids)
                    or type(comparison.get("window_count")) is not int or comparison["window_count"] < 0
                    or comparison.get("subset_count") != comparison["window_count"]):
                raise ValueError("父子固定验证比较格式无效")
            if comparison["available"]:
                if (not comparison["window_count"] or not comparison["validation_trial_ids"]
                        or not sha(comparison.get("window_identity_sha256"))
                        or any(not isinstance(comparison.get(key), dict) or
                               comparison[key].get("window_count") != comparison["window_count"] for key in ("parent", "child"))):
                    raise ValueError("父子验证必须使用同一固定窗口集合")
            elif (comparison["window_count"] or comparison["validation_trial_ids"] or
                    comparison.get("parent") is not None or comparison.get("child") is not None
                    or comparison.get("window_identity_sha256") is not None):
                raise ValueError("父子验证缺失时不能声明比较结果")
    if not sha(metadata.get("weights_sha256")):
        raise ValueError("模型缺少权重哈希")
    digest = metadata_digest(metadata)
    if metadata.get("metadata_sha256") != digest or metadata.get("model_sha256") != digest:
        raise ValueError("模型元数据哈希不匹配")
    return deepcopy(metadata)


def _safe_directory(path):
    path = Path(path)
    if path.is_symlink() or hasattr(path, "is_junction") and path.is_junction():
        raise ValueError("模型目录不能为链接")
    for name in ("metadata.json", "weights.pt"):
        file = path / name
        if file.is_symlink() or file.resolve().parent != path.resolve():
            raise ValueError("模型文件超出允许目录")
    return path


def load_bundle(path):
    path = _safe_directory(path)
    metadata = validate_metadata(json.loads((path / "metadata.json").read_text(encoding="utf-8")))
    if file_digest(path / "weights.pt") != metadata["weights_sha256"]:
        raise ValueError("全谱模型权重哈希不匹配")
    torch = torch_module()
    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    state = torch.load(path / "weights.pt", map_location="cpu", weights_only=True)
    network = build_network(metadata["input_kind"], contact_head=has_contact_head(metadata))
    expected = network.state_dict()
    if (not isinstance(state, dict) or set(state) != set(expected) or
            any(not isinstance(state[key], torch.Tensor) or state[key].shape != expected[key].shape or
                state[key].dtype != torch.float32 or not torch.isfinite(state[key]).all().item() for key in expected)):
        raise ValueError("全谱权重结构、类型或数值无效")
    network.load_state_dict(state, strict=True)
    network.eval()
    return metadata, network


def save_bundle(userdata, model_id, model, metadata):
    if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", model_id):
        raise ValueError("模型标识无效")
    home = Path(userdata).resolve()
    root = home / "spectral_models"
    if root.is_symlink() or hasattr(root, "is_junction") and root.is_junction() or root.resolve().parent != home:
        raise ValueError("全谱模型目录超出允许位置")
    root.mkdir(parents=True, exist_ok=True)
    final = root / model_id
    if final.exists():
        raise ValueError("模型标识已存在，不能覆盖历史模型")
    candidate = root / (".candidate_" + uuid.uuid4().hex)
    candidate.mkdir()
    try:
        torch = torch_module()
        torch.save({key: value.detach().cpu().to(torch.float32) for key, value in model.state_dict().items()}, candidate / "weights.pt")
        with (candidate / "weights.pt").open("r+b") as stream:
            os.fsync(stream.fileno())
        input_kind = metadata.get("input_kind", "delta_S")
        drift = input_kind == "delta_lambda_9"
        contact = hasattr(model, "contact_head")
        if metadata.get("schema") is not None and metadata["schema"] != (CONTACT_SCHEMA if contact else SCHEMA):
            raise ValueError("保存模型结构与声明版本不一致")
        value = {**deepcopy(metadata), "schema": CONTACT_SCHEMA if contact else SCHEMA, "model_kind": MODEL_KIND, "model_id": model_id,
                 "created_utc": datetime.now(timezone.utc).isoformat(), "architecture": architecture_for(input_kind, contact_head=contact),
                 "window_size": 16, "input_kind": input_kind,
                 "reference_rule": "nine_fbg_preload_median_v1" if drift else "spectrum_unloaded_median_v1",
                 "axes": list(AXES), "units": list(UNITS), "wrench_reference": "PX6D_native_axes_and_origin",
                 "formal_ready": False, "weights_sha256": file_digest(candidate / "weights.pt")}
        value["metadata_sha256"] = value["model_sha256"] = metadata_digest(value)
        target = candidate / "metadata.json"
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        load_bundle(candidate)  # Candidate is complete and fully loadable before publication.
        os.replace(candidate, final)
        return value
    finally:
        if candidate.exists() and candidate.parent.resolve() == root.resolve():
            shutil.rmtree(candidate)
