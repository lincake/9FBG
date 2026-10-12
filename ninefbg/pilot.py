"""Whole-trial cross-validation for a measured subset, never a 25-point model.

Callers must use analysis.load_rows to verify archived evidence and remove
incomplete, aborted or marked trials. Compatible A/B sessions may be pooled.
Their batch labels are preserved; trial folds are not independent sessions.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path

import numpy as np

from .inversion import (POINT_IDS, _balanced_weights, _digest, _errors,
                        _finite_number, _linear_candidate, _point_curves,
                        _quantile, _rows, _softmax, _statistics)

MODEL_SCHEMA = "9fbg_trial_pilot_model_v1"
REPORT_SCHEMA = "9fbg_pilot_cv_v1"
SCOPE = "within_session_whole_trial_cross_validation"
MULTI_SCOPE = "multi_session_whole_trial_cross_validation"
_LIMITATION = ("仅验证实际采集点在同一会话中的整轮重复性；不证明完整25点、"
               "跨会话或跨天泛化，不构成物理精度验收或正式训练资格。")
_MULTI_LIMITATION = ("仅验证所选会话中实际采集点的整轮留出重复性；训练折与验证折可来自相同会话，"
                     "不证明完整25点、跨会话或跨天独立泛化，不构成物理精度验收或正式训练资格。")
_DEFAULTS = {"max_force_N": 3.0, "force_bin_width_N": .25,
             "noise_floor_pm": .05, "contact_threshold": 3.0,
             "residual_threshold": 6.0, "reject_margin": .05,
             "method": "linear", "mlp_epochs": 100, "mlp_batch_size": 256,
             "mlp_learning_rate": .003, "seed": 1729}
MLP_SCHEMA = "9fbg_measured_point_mlp_v1"


def _options(options):
    if options is not None and not isinstance(options, dict):
        raise ValueError("少量点研究参数必须为对象")
    options = dict(options or {})
    unknown = set(options)-set(_DEFAULTS)-{"allow_synthetic_diagnostics"}
    if unknown:
        raise ValueError("少量点研究不支持该参数：" + ", ".join(sorted(unknown)))
    settings = {**_DEFAULTS, **options}
    if settings["method"] not in ("linear", "mlp"):
        raise ValueError("训练方法必须为 linear 或 mlp")
    for key, lower, upper in (("mlp_epochs", 1, 500), ("mlp_batch_size", 1, 4096), ("seed", 0, 2**32-1)):
        if type(settings[key]) is not int or not lower <= settings[key] <= upper:
            raise ValueError(f"{key} 必须为 {lower}～{upper} 的整数")
    for key in set(_DEFAULTS)-{"reject_margin", "method", "mlp_epochs", "mlp_batch_size", "seed"}:
        if not _finite_number(settings[key]) or settings[key] <= 0:
            raise ValueError(f"{key} 必须为正有限数")
        settings[key] = float(settings[key])
    if not _finite_number(settings["reject_margin"]) or not 0 <= settings["reject_margin"] < 1:
        raise ValueError("reject_margin 必须在 0～1 之间")
    settings["reject_margin"] = float(settings["reject_margin"])
    if "allow_synthetic_diagnostics" in options and type(options["allow_synthetic_diagnostics"]) is not bool:
        raise ValueError("合成诊断测试开关必须为布尔值")
    return settings


def _prepare(rows, options):
    # Archives already carry session prefixes. Direct callers can also supply
    # local T0001/B0001 identities: qualify copies only when pooling sessions.
    if isinstance(rows, list):
        session_ids = {row["session_id"] for row in rows if isinstance(row, dict) and isinstance(row.get("session_id"), str)}
        if len(session_ids) > 1:
            qualified = []
            for raw in rows:
                row = dict(raw) if isinstance(raw, dict) else raw
                if isinstance(row, dict) and isinstance(row.get("session_id"), str):
                    prefix = row["session_id"]+":"
                    for key in ("trial_id", "baseline_id"):
                        if isinstance(row.get(key), str) and row[key].strip() and not row[key].startswith(prefix):
                            row[key] = prefix+row[key]
                qualified.append(row)
            rows = qualified
    data, _, _ = _rows(rows)
    if any(row["split"] == "C" for row in data):
        raise ValueError("C 组保留作最终独立评估，不能参与少量点交叉验证拟合")
    if data[0]["source_kind"] != "hardware" and options.get("allow_synthetic_diagnostics") is not True:
        raise ValueError("没有真实硬件数据时不生成模型；合成数据仅供显式算法测试")
    valid = [row for row in data if row["valid"]]
    for key in ("trial_id", "baseline_id"):
        ownership = {}
        for row in valid:
            if ownership.setdefault(row[key], row["session_id"]) != row["session_id"]:
                raise ValueError("不同会话的试次或基线身份冲突，不能混合其无载参考")
    trials, points, baseline_owners = defaultdict(list), defaultdict(set), {}
    for row in valid:
        if row["point_id"] is not None:
            trials[row["trial_id"]].append(row)
            points[row["point_id"]].add(row["trial_id"])
    if not 2 <= len(points) <= 25:
        raise ValueError("定位研究至少需要两个实际采集点，每点至少三个合格完整试次")
    for point, ids in sorted(points.items()):
        if len(ids) < 3:
            raise ValueError(f"{point} 只有 {len(ids)} 个试次；每点至少需要三个合格完整试次")
    for trial, records in trials.items():
        if len({row["point_id"] for row in records}) != 1:
            raise ValueError("同一试次不能有多个点位标签")
        baselines = {row["baseline_id"] for row in records}
        if len(baselines) != 1:
            raise ValueError("一个试次必须绑定唯一基线，不能混合中途改变的参考")
        baseline = next(iter(baselines))
        if baseline in baseline_owners and baseline_owners[baseline] != trial:
            raise ValueError("不同试次不能共用同一基线，否则整轮拆分会泄漏测试无载参考")
        baseline_owners[baseline] = trial
    noise = defaultdict(list)
    for row in valid:
        if row["point_id"] is None and row["baseline_id"] in baseline_owners:
            # A baseline-only row may have its own synthetic group ID. The
            # baseline identity, rather than that ID, owns the observation.
            if row["trial_id"] in trials and baseline_owners[row["baseline_id"]] != row["trial_id"]:
                raise ValueError("无载行的试次与基线归属不一致")
            noise[row["baseline_id"]].append(row)
    for baseline, trial in baseline_owners.items():
        if len(noise[baseline]) < 2:
            raise ValueError(f"试次 {trial} 缺少至少两个同基线的真实无载样本，不能估计噪声")
    return data, valid, trials, points, baseline_owners, noise


def _select_rows(trial_ids, trials, baseline_owners, noise):
    ids = set(trial_ids)
    result = [row for trial in sorted(ids) for row in trials[trial]]
    for baseline in sorted(baseline_owners):
        if baseline_owners[baseline] in ids:
            # Normalize baseline-only group IDs to their owning press trial
            # for equal trial weighting; the archived input is never mutated.
            result.extend({**row, "trial_id": baseline_owners[baseline]} for row in noise[baseline])
    return result


def _fit_network(rows, point_ids, settings, progress=None):
    """Subset classifier and force regressor; only training rows define scaling."""
    rng = np.random.default_rng(settings["seed"])
    raw_x = np.asarray([row["x_pm"] for row in rows])
    weights = _balanced_weights(rows, settings["force_bin_width_N"])
    mean = np.sum(raw_x*weights[:, None], axis=0)
    scale = np.maximum(np.sqrt(np.sum((raw_x-mean)**2*weights[:, None], axis=0)), settings["noise_floor_pm"])
    x = (raw_x-mean)/scale
    force_scale = max(max(abs(row["force_N"]) for row in rows), .01)
    targets = np.asarray([row["force_N"]/force_scale for row in rows])
    classes = np.asarray([point_ids.index(row["point_id"]) if row["point_id"] else -1 for row in rows])
    count = len(point_ids)
    architecture = [9, 32, 16, count+1]
    parameters = []
    for left, right in zip(architecture[:-1], architecture[1:]):
        parameters.extend([rng.normal(0, math.sqrt(2/left), (left, right)), np.zeros(right)])
    first, second = [np.zeros_like(value) for value in parameters], [np.zeros_like(value) for value in parameters]
    step = 0
    for epoch in range(settings["mlp_epochs"]):
        order = rng.permutation(len(rows))
        for start in range(0, len(rows), settings["mlp_batch_size"]):
            indices = order[start:start+settings["mlp_batch_size"]]
            batch, local_weights = x[indices], weights[indices]
            local_weights = local_weights/local_weights.sum()
            w1, b1, w2, b2, w3, b3 = parameters
            a1 = np.maximum(0, batch@w1+b1)
            a2 = np.maximum(0, a1@w2+b2)
            output = a2@w3+b3
            gradient = np.zeros_like(output)
            loaded = classes[indices] >= 0
            if loaded.any():
                probabilities = _softmax(output[loaded, :count])
                probabilities[np.arange(loaded.sum()), classes[indices][loaded]] -= 1
                class_weights = local_weights[loaded]/local_weights[loaded].sum()
                gradient[loaded, :count] = probabilities*class_weights[:, None]
            gradient[:, count] = 2*(output[:, count]-targets[indices])*local_weights
            g3, gb3 = a2.T@gradient, gradient.sum(axis=0)
            d2 = (gradient@w3.T)*(a2 > 0)
            g2, gb2 = a1.T@d2, d2.sum(axis=0)
            d1 = (d2@w2.T)*(a1 > 0)
            gradients = [batch.T@d1, d1.sum(axis=0), g2, gb2, g3, gb3]
            norm = math.sqrt(sum(float(np.sum(grad*grad)) for grad in gradients))
            if not math.isfinite(norm):
                raise ValueError("MLP 训练出现非有限数值，未生成模型")
            step += 1
            for index, (parameter, grad) in enumerate(zip(parameters, gradients)):
                grad *= min(1.0, 10/max(norm, 1e-12))
                first[index] = .9*first[index]+.1*grad
                second[index] = .999*second[index]+.001*grad*grad
                parameter -= settings["mlp_learning_rate"]*(first[index]/(1-.9**step))/(np.sqrt(second[index]/(1-.999**step))+1e-8)
        if progress:
            progress({"epoch": epoch+1, "epochs": settings["mlp_epochs"]})
    return {"schema": MLP_SCHEMA, "kind": "mlp", "architecture": architecture,
            "classification_point_ids": list(point_ids), "classification_outputs": count,
            "force_outputs": 1, "feature_mean_pm": mean.tolist(), "feature_scale_pm": scale.tolist(),
            "force_scale_N": force_scale, "epochs": settings["mlp_epochs"], "seed": settings["seed"],
            "batch_size": settings["mlp_batch_size"], "learning_rate": settings["mlp_learning_rate"],
            "normalization_source": "training_trials_and_their_baselines_only",
            "layers": [{"weights": parameters[i].tolist(), "bias": parameters[i+1].tolist()} for i in (0, 2, 4)]}


def _network_output(network, raw_x):
    value = (raw_x-np.asarray(network["feature_mean_pm"]))/np.asarray(network["feature_scale_pm"])
    for index, layer in enumerate(network["layers"]):
        value = value@np.asarray(layer["weights"])+np.asarray(layer["bias"])
        if index < 2:
            value = np.maximum(0, value)
    count = network["classification_outputs"]
    return _softmax(value[:count]), float(value[count]*network["force_scale_N"])


def _fit(rows, point_ids, settings, progress=None):
    origin, variance = _statistics(rows, settings["force_bin_width_N"], settings["noise_floor_pm"])
    templates = []
    for point in point_ids:
        selected = [row for row in rows if row["point_id"] == point]
        weights = _balanced_weights(selected, settings["force_bin_width_N"])
        force = np.asarray([row["force_N"] for row in selected])
        values = np.asarray([row["x_pm"] for row in selected])-origin
        slope = np.sum(weights[:, None]*force[:, None]*values, axis=0)/np.sum(weights*force**2)
        if np.linalg.norm(slope) <= 1e-12:
            raise ValueError(f"{point} 缺少可辨识的有符号响应")
        templates.append({"point_id": point, "slope_pm_per_N": slope.tolist(),
                          "max_training_force_N": float(force.max())})
    loaded = [row for row in rows if row["point_id"] is not None]
    session_ids = sorted({row["session_id"] for row in rows})
    multi = len(session_ids) > 1
    model = {"schema": MODEL_SCHEMA, "point_ids": list(point_ids),
             "feature_ids": [f"FBG{i}" for i in range(1, 10)], "feature_unit": "pm", "force_unit": "N",
             "source_kind": rows[0]["source_kind"], "algorithm_version": rows[0]["algorithm_version"],
             "config_version": rows[0]["config_version"], "source_data_sha256": _digest(rows),
             "scope": "measured_points_multi_session_research_only" if multi else "measured_points_same_session_research_only", "formal_ready": False,
             "diagnostic_only": True, "limitation": _MULTI_LIMITATION if multi else _LIMITATION,
             "primary_method": settings["method"], "max_force_N": settings["max_force_N"],
             "max_force_provisional": True, "force_bin_width_N": settings["force_bin_width_N"],
             "baseline_context": {"origin_pm": origin.tolist(), "noise_variance_pm2": variance.tolist(),
                                  "variance_floor_pm2": settings["noise_floor_pm"]**2,
                                  "training_baseline_ids": sorted({row["baseline_id"] for row in rows}),
                                  "statistics_source": "training_trials_and_their_baselines_only"},
             "thresholds": {"contact_threshold": settings["contact_threshold"],
                            "residual_threshold": settings["residual_threshold"],
                            "margin_threshold": settings["reject_margin"],
                            "selection": "predeclared_not_selected_using_test_rows"},
             "templates": templates,
             "provenance": {"training_session_ids": session_ids,
                            "training_session_count": len(session_ids),
                            "training_session_splits": {row["session_id"]: row["split"] for row in rows},
                            "training_trial_ids": sorted({row["trial_id"] for row in loaded}),
                            "training_trial_sessions": {row["trial_id"]: row["session_id"] for row in loaded},
                            "training_baseline_sessions": {row["baseline_id"]: row["session_id"] for row in rows},
                            "training_row_count": len(rows), "test_rows_used_for_fitting": 0}}
    if settings["method"] == "mlp":
        model["mlp"] = _fit_network(rows, point_ids, settings, progress)
        # A predeclared training-only calibration rule, never a held-out-fold
        # search: neural force residuals have a different scale from WLS fits.
        residuals = []
        for row in loaded:
            _, force = _network_output(model["mlp"], np.asarray(row["x_pm"]))
            template = templates[point_ids.index(row["point_id"])]
            response = force*np.asarray(template["slope_pm_per_N"])
            residuals.append(float(np.sqrt(np.mean((np.asarray(row["x_pm"])-origin-response)**2/variance))))
        weights = _balanced_weights(loaded, settings["force_bin_width_N"])
        model["thresholds"].update(residual_threshold=max(settings["residual_threshold"], 1.2*_quantile(residuals, .99, weights)),
                                   selection="predeclared_training_residual_quantile_not_test_rows",
                                   residual_quantile=.99, residual_multiplier=1.2)
    model["model_sha256"] = _digest(model)
    return model


def validate_pilot_model(model):
    """Validate an independent research artifact, never an inversion bundle."""
    if not isinstance(model, dict) or model.get("schema") != MODEL_SCHEMA:
        raise ValueError("少量点研究模型版本不兼容")
    if model.get("formal_ready") is not False or model.get("diagnostic_only") is not True:
        raise ValueError("少量点模型不能绕过正式训练门")
    method = model.get("primary_method", "linear")
    if method not in ("linear", "mlp") or method == "linear" and "mlp" in model:
        raise ValueError("少量点模型方法定义不兼容")
    points = model.get("point_ids")
    if not isinstance(points, list) or not 2 <= len(points) <= 25 or any(not isinstance(point, str) or point not in POINT_IDS for point in points) or len(set(points)) != len(points):
        raise ValueError("少量点模型的实际点位身份无效")
    if model.get("feature_ids") != [f"FBG{i}" for i in range(1, 10)] or model.get("feature_unit") != "pm" or model.get("force_unit") != "N":
        raise ValueError("少量点模型的特征身份或单位无效")
    if model.get("source_kind") not in ("hardware", "synthetic") or any(not isinstance(model.get(key), str) or not model[key] for key in ("algorithm_version", "config_version")):
        raise ValueError("少量点模型缺少来源或算法配置定义")
    def vector(value, *, positive=False):
        if not isinstance(value, list) or len(value) != 9 or not all(_finite_number(x) and (not positive or x > 0) for x in value):
            raise ValueError("少量点模型需要九维有限参数与正噪声方差")
    baseline = model.get("baseline_context")
    if not isinstance(baseline, dict):
        raise ValueError("少量点模型缺少无载噪声定义")
    vector(baseline.get("origin_pm"))
    vector(baseline.get("noise_variance_pm2"), positive=True)
    if not _finite_number(baseline.get("variance_floor_pm2")) or baseline["variance_floor_pm2"] <= 0 or any(x < baseline["variance_floor_pm2"] for x in baseline["noise_variance_pm2"]):
        raise ValueError("少量点模型噪声下限无效")
    templates = model.get("templates")
    if not isinstance(templates, list) or any(not isinstance(item, dict) for item in templates) or [item.get("point_id") for item in templates] != points:
        raise ValueError("少量点模型的响应模板与点位身份不一致")
    for template in templates:
        vector(template.get("slope_pm_per_N"))
        if np.linalg.norm(template["slope_pm_per_N"]) <= 1e-12 or not _finite_number(template.get("max_training_force_N")) or template["max_training_force_N"] <= 0:
            raise ValueError("少量点响应模板或实测力范围无效")
    if any(not _finite_number(model.get(key)) or model[key] <= 0 for key in ("max_force_N", "force_bin_width_N")):
        raise ValueError("少量点模型力范围无效")
    thresholds = model.get("thresholds")
    if not isinstance(thresholds, dict) or any(not _finite_number(thresholds.get(key)) or thresholds[key] <= 0 for key in ("contact_threshold", "residual_threshold")) or not _finite_number(thresholds.get("margin_threshold")) or not 0 <= thresholds["margin_threshold"] < 1:
        raise ValueError("少量点模型拒识阈值无效")
    if method == "mlp":
        network = model.get("mlp")
        architecture = [9, 32, 16, len(points)+1]
        if not isinstance(network, dict) or network.get("schema") != MLP_SCHEMA or network.get("kind") != "mlp" or network.get("architecture") != architecture or network.get("classification_point_ids") != points or type(network.get("classification_outputs")) is not int or network["classification_outputs"] != len(points) or type(network.get("force_outputs")) is not int or network["force_outputs"] != 1:
            raise ValueError("MLP 版本、已测点身份或输出尺寸不兼容")
        vector(network.get("feature_mean_pm"))
        vector(network.get("feature_scale_pm"), positive=True)
        if not _finite_number(network.get("force_scale_N")) or network["force_scale_N"] <= 0:
            raise ValueError("MLP 力归一化参数无效")
        layers = network.get("layers")
        if not isinstance(layers, list) or len(layers) != 3:
            raise ValueError("MLP 层数不兼容")
        for layer, left, right in zip(layers, architecture[:-1], architecture[1:]):
            if not isinstance(layer, dict):
                raise ValueError("MLP 层参数必须为对象")
            weights, bias = layer.get("weights"), layer.get("bias")
            if not isinstance(weights, list) or len(weights) != left or any(not isinstance(row, list) or len(row) != right or not all(_finite_number(value) for value in row) for row in weights) or not isinstance(bias, list) or len(bias) != right or not all(_finite_number(value) for value in bias):
                raise ValueError("MLP 权重尺寸或有限数值无效")
    expected = _digest({key: value for key, value in model.items() if key != "model_sha256"})
    if model.get("model_sha256") != expected:
        raise ValueError("少量点模型校验失败")
    return True


def _predict(model, x_pm):
    origin = np.asarray(model["baseline_context"]["origin_pm"])
    variance = np.asarray(model["baseline_context"]["noise_variance_pm2"])
    x = np.asarray(x_pm)-origin
    linear = sorted((_linear_candidate(template, x, variance, model["max_force_N"])
                     for template in model["templates"]), key=lambda item: item["residual"])
    method = model.get("primary_method", "linear")
    if method == "mlp":
        probabilities, force = _network_output(model["mlp"], np.asarray(x_pm))
        candidates = []
        for probability, template in zip(probabilities, model["templates"]):
            maximum = min(template["max_training_force_N"], model["max_force_N"])
            fit_force = float(np.clip(force, 0, maximum))
            slope = np.asarray(template["slope_pm_per_N"])
            candidates.append({"point_id": template["point_id"], "raw_force_N": force, "fit_force_N": fit_force,
                               "residual": float(np.sqrt(np.mean((x-fit_force*slope)**2/variance))),
                               "unconstrained_residual": float(np.sqrt(np.mean((x-force*slope)**2/variance))),
                               "max_training_force_N": template["max_training_force_N"], "class_score": float(probability)})
        candidates.sort(key=lambda item: item["class_score"], reverse=True)
        margin = float(candidates[0]["class_score"]-candidates[1]["class_score"])
    else:
        candidates = linear
        margin = float((candidates[1]["residual"]-candidates[0]["residual"])/max(candidates[1]["residual"], 1e-12))
    best, second = candidates[:2]
    contact = float(np.sqrt(np.mean(x*x/variance)))
    thresholds = model["thresholds"]
    # Retain the measured-response range guard even if the neural output
    # saturates under extrapolation; no force-reference value is consulted.
    unrestricted = sorted(linear, key=lambda item: item["unconstrained_residual"])
    range_candidate, alternate = unrestricted[:2]
    raw_margin = ((alternate["unconstrained_residual"]-range_candidate["unconstrained_residual"])
                  / max(alternate["unconstrained_residual"], 1e-12))
    outside = not 0 <= range_candidate["raw_force_N"] <= min(range_candidate["max_training_force_N"], model["max_force_N"])
    credible_range = outside and range_candidate["unconstrained_residual"] <= thresholds["residual_threshold"] and raw_margin >= thresholds["margin_threshold"]
    if contact <= thresholds["contact_threshold"]:
        status, reason = "无接触", "特征未超过预先固定的无载接触阈值"
    elif credible_range:
        status, reason = "超范围", "可信未约束候选超出实测训练力范围；边界拟合力只用于评分"
    elif best["residual"] > thresholds["residual_threshold"] or margin < thresholds["margin_threshold"]:
        status, reason = "定位不确定", "候选残差过大或最优与次优区分不足"
    elif not 0 <= best["raw_force_N"] <= min(best["max_training_force_N"], model["max_force_N"]):
        status, reason = "超范围", "候选原始力超出实测训练范围；未裁切冒充实测力"
    else:
        status, reason = "有效估计", "通过预声明的研究估计检查；仅限实际采集点"
    reported = range_candidate if status == "超范围" and credible_range else best
    return {"status": status, "reason": reason, "point_id": best["point_id"] if status == "有效估计" else None,
            "force_N": best["fit_force_N"] if status == "有效估计" else 0.0 if status == "无接触" else None,
            "raw_force_N": reported["raw_force_N"], "fit_force_N": reported["fit_force_N"],
            "best": best, "second": second, "candidates": candidates,
            "residual": best["residual"], "second_residual": second["residual"],
            "confidence": margin, "margin": margin, "confidence_definition": "候选区分度，非校准概率",
            "method": method,
            "margin_definition": "softmax_top_two_gap_not_calibrated_probability" if method == "mlp" else "relative_best_second_residual_gap",
            "contact_score": contact, **thresholds, "formal_ready": False}


def predict_pilot(model, x_pm, quality_context=None):
    """Prediction accepts wavelengths only; reference force is never an input."""
    validate_pilot_model(model)
    invalid = {"status": "无效", "point_id": None, "force_N": None, "formal_ready": False}
    if not isinstance(x_pm, (list, tuple, np.ndarray)) or len(x_pm) != 9 or not all(_finite_number(x) for x in x_pm):
        return {**invalid, "reason": "需要九个有限、有符号的波长漂移特征"}
    context = quality_context or {}
    if not isinstance(context, dict) or context.get("valid") is False or context.get("stale") is True or context.get("display_stale") is True:
        return {**invalid, "reason": "波长特征质量无效或已过期"}
    if any(key in context and context[key] != model[key] for key in ("source_kind", "algorithm_version", "config_version")):
        return {**invalid, "reason": "来源、算法或配置版本不匹配"}
    return _predict(model, x_pm)


def _metrics(records, point_ids, width):
    loaded = [item for item in records if item["row"]["point_id"] is not None]
    rows = [item["row"] for item in loaded]
    weights = _balanced_weights(rows, width) if rows else np.asarray([])
    matrix = np.zeros((len(point_ids), len(point_ids)), dtype=int)
    correct, accepted_correct, accepted_mass = 0.0, 0.0, 0.0
    joint, known, distances, accepted_errors, accepted_weights = [], [], [], [], []
    bins, status_counts = defaultdict(list), defaultdict(int)
    for item, weight in zip(loaded, weights):
        row, prediction = item["row"], item["prediction"]
        chosen = prediction["best"]["point_id"]
        same = chosen == row["point_id"]
        matrix[point_ids.index(row["point_id"]), point_ids.index(chosen)] += 1
        correct += weight*same
        error = prediction["best"]["raw_force_N"]-row["force_N"]
        joint.append(error)
        actual = next(candidate for candidate in prediction["candidates"] if candidate["point_id"] == row["point_id"])
        known.append(actual["raw_force_N"]-row["force_N"])
        left, right = POINT_IDS.index(row["point_id"]), POINT_IDS.index(chosen)
        distances.append(math.hypot(left % 5-right % 5, left//5-right//5)/4)
        status_counts[prediction["status"]] += 1
        bins[math.floor(row["force_N"]/width)].append((error, weight))
        if prediction["status"] == "有效估计":
            accepted_mass += weight
            accepted_correct += weight*same
            accepted_errors.append(error)
            accepted_weights.append(weight)
    force_errors = _errors(joint, weights)
    unloaded = [item for item in records if item["row"]["point_id"] is None]
    unloaded_weights = _balanced_weights([item["row"] for item in unloaded], width) if unloaded else []
    false_contact = sum(weight*(item["prediction"]["status"] != "无接触") for item, weight in zip(unloaded, unloaded_weights)) if unloaded else None
    return {"n": len(rows), "test_trials": len({row["trial_id"] for row in rows}),
            "classification_accuracy": float(np.clip(correct, 0, 1)) if rows else None,
            "accepted_classification_accuracy": float(np.clip(accepted_correct/accepted_mass, 0, 1)) if accepted_mass else None,
            "force_mae_N": force_errors["mae_N"], "force_rmse_N": force_errors["rmse_N"],
            "force_p95_abs_error_N": force_errors["p95_abs_error_N"],
            "known_point_force": _errors(known, weights), "joint_force": force_errors,
            "accepted_joint_force": _errors(accepted_errors, accepted_weights),
            "position_error_norm_mean": float(np.sum(np.asarray(distances)*weights)) if rows else None,
            "localization": {"mean_normalized_grid_distance": float(np.sum(np.asarray(distances)*weights))*4 if rows else None,
                             "p95_normalized_grid_distance": _quantile(distances, .95, weights)*4 if rows else None,
                             "unit": "相邻格距=1；位置归一化坐标误差的相邻格距=0.25；无毫米精度声明"},
            "rejection_rate": float(np.clip(1-accepted_mass, 0, 1)) if rows else None,
            "confusion_matrix": matrix.tolist(), "confusion_point_ids": list(point_ids),
            "status_counts": dict(status_counts),
            "contact_detection": {"unloaded_n": len(unloaded), "false_contact_rate": float(false_contact) if false_contact is not None else None},
            "by_force_bin": [{"low_N": key*width, "high_N": (key+1)*width, **_errors(*zip(*values))} for key, values in sorted(bins.items())],
            "weighting": "实际点、整轮试次和有样本力箱等权；混淆矩阵为原始行计数",
            "classification_scope": "所有留出有效按压行的原始候选；拒识和已接受准确率另列"}


def fit_pilot(rows, options=None, progress=None):
    """Return a saveable research model and three-fold out-of-fold report.

    No frame randomization or threshold search is performed. Each entire trial
    and every noise observation sharing its baseline belong to exactly one
    held-out fold. With three trials per point, two train and one tests per fold.
    """
    settings = _options(options)
    if progress is not None and not callable(progress):
        raise ValueError("训练进度回调必须可调用")
    def notify(stage, percent, **details):
        if progress is not None:
            progress({"stage": stage, "progress_percent": float(percent),
                      "method": settings["method"], "fold_count": 3, **details})
    notify("prepare", 0)
    data, valid, trials, points, owners, noise = _prepare(rows, settings)
    point_ids = sorted(points)
    assignments = {trial: index % 3 for point in point_ids for index, trial in enumerate(sorted(points[point]))}
    records, folds = [], []
    for fold in range(3):
        test_ids = sorted(trial for trial, assignment in assignments.items() if assignment == fold)
        train_ids = sorted(set(trials)-set(test_ids))
        training = _select_rows(train_ids, trials, owners, noise)
        testing = _select_rows(test_ids, trials, owners, noise)
        notify("cv_fit", fold*25, fold=fold+1)
        model = _fit(training, point_ids, settings,
                     lambda update: notify("cv_fit", (fold+update["epoch"]/update["epochs"])*25,
                                           fold=fold+1, **update))
        notify("cv_evaluate", (fold+1)*25, fold=fold+1)
        fold_records = [{"row": row, "prediction": _predict(model, row["x_pm"]), "fold": fold+1} for row in testing]
        records.extend(fold_records)
        folds.append({"fold": fold+1, "train_trial_ids": train_ids, "test_trial_ids": test_ids,
                      "train_session_ids": sorted({row["session_id"] for row in training}),
                      "test_session_ids": sorted({row["session_id"] for row in testing}),
                      "train_baseline_ids": sorted(baseline for baseline, owner in owners.items() if owner in train_ids),
                      "test_baseline_ids": sorted(baseline for baseline, owner in owners.items() if owner in test_ids),
                      "training_row_count": len(training), "test_row_count": len(testing),
                      "summary": _metrics(fold_records, point_ids, settings["force_bin_width_N"]), "fold_model": model})
    overall = _metrics(records, point_ids, settings["force_bin_width_N"])
    by_point = {point: _metrics([item for item in records if item["row"]["point_id"] == point], point_ids, settings["force_bin_width_N"]) for point in point_ids}
    by_trial = {trial: {**_metrics([item for item in records if item["row"]["trial_id"] == trial], point_ids, settings["force_bin_width_N"]),
                        "point_id": trials[trial][0]["point_id"], "fold": assignments[trial]+1,
                        "session_id": trials[trial][0]["session_id"], "baseline_id": trials[trial][0]["baseline_id"]}
                for trial in sorted(trials)}
    used = _select_rows(sorted(trials), trials, owners, noise)
    notify("final_fit", 75)
    model = _fit(used, point_ids, settings,
                 lambda update: notify("final_fit", 75+update["epoch"]/update["epochs"]*25, **update))
    notify("validate", 100)
    model["refit_scope"] = "all_selected_trials_after_cross_validation_not_used_for_reported_scores"
    model["model_sha256"] = _digest({key: value for key, value in model.items() if key != "model_sha256"})
    session_ids = list(model["provenance"]["training_session_ids"])
    batches = sorted({row["split"] for row in used})
    multi = len(session_ids) > 1
    report = {"schema": REPORT_SCHEMA, "scope": MULTI_SCOPE if multi else SCOPE,
              "interpretation": _MULTI_LIMITATION if multi else _LIMITATION,
              "formal_ready": False, "model_generated": True, "method": settings["method"], "models": [settings["method"]],
              "point_ids": point_ids, "session_id": session_ids[0] if not multi else None,
              "session_ids": session_ids, "session_count": len(session_ids),
              "source_session_ids": sorted({row["session_id"] for row in data}),
              "batch": batches[0] if not multi else None, "batches": batches,
              "summary": overall, "by_point": by_point, "by_trial": by_trial, "folds": folds,
              "confusion_matrix": {"labels": point_ids, "counts": overall["confusion_matrix"]},
              "methods": {settings["method"]: {"overall": overall, "by_point": by_point, "by_trial": by_trial}},
              "position_error_definition": "5×5点阵索引归一化到[0,1]后的二维欧氏距离，相邻格距0.25；不换算毫米",
              "point_curves": _point_curves(valid), "source_data_sha256": _digest(data),
              "model_sha256": model["model_sha256"],
              "quality_summary": {"total_rows": len(data), "valid_rows": len(valid),
                                  "invalid_rows": len(data)-len(valid), "used_rows": len(used),
                                  "ignored_unassociated_rows": len(valid)-len(used),
                                  "press_rows": sum(row["point_id"] is not None for row in used),
                                  "unloaded_rows": sum(row["point_id"] is None for row in used),
                                  "points_present": point_ids, "session_count": len(session_ids), "trial_count": len(trials),
                                  "source_kind": valid[0]["source_kind"], "algorithm_version": valid[0]["algorithm_version"],
                                  "config_version": valid[0]["config_version"], "noise_verified": True},
              "validation": {"fold_count": 3, "grouping": "whole_trial_and_baseline",
                             "each_trial_tested_once": True, "test_rows_used_for_fitting": 0,
                             "threshold_selection": "predeclared_training_only_rule_not_test_data" if settings["method"] == "mlp" else "predeclared_not_tuned_on_test_data",
                             "independent_session_validation": False,
                             "reported_scores_source": "held_out_fold_models_only",
                             "saved_model_refit_after_validation": True},
              "out_of_fold_predictions": [{"fold": item["fold"], "session_id": item["row"]["session_id"],
                                           "trial_id": item["row"]["trial_id"], "baseline_id": item["row"]["baseline_id"],
                                           "true_point_id": item["row"]["point_id"], "true_force_N": item["row"]["force_N"],
                                           "candidate_point_id": item["prediction"]["best"]["point_id"],
                                           "candidate_force_N": item["prediction"]["best"]["raw_force_N"],
                                           "status": item["prediction"]["status"], "margin": item["prediction"]["margin"]}
                                          for item in records if item["row"]["point_id"] is not None]}
    validate_pilot_model(model)
    notify("complete", 100)
    return {"schema": "9fbg_trial_pilot_result_v1", "model": model, "report": report}


def save_pilot_model(model, path):
    validate_pilot_model(model)
    Path(path).write_text(json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def load_pilot_model(path):
    model = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_pilot_model(model)
    return model
