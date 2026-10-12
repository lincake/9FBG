"""Offline response diagnostics and strictly separated 25-point inversion.

Features are signed FBG1..FBG9 wavelength shifts in pm. A alone fits every
statistic/model, B selects rejection thresholds, and C is evaluation only.
These diagnostic models never authorize formal training or deployment.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

import numpy as np

SCHEMA = "9fbg_response_inversion_v1"
POINT_IDS = tuple(f"P{i:02d}" for i in range(1, 26))
_EPS = 1e-12


def _finite_number(value):
    return isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, (bool, np.bool_)) and math.isfinite(float(value))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _quantile(values, quantile, weights=None):
    values = np.asarray(values, dtype=float)
    if weights is None:
        return float(np.quantile(values, quantile))
    order = np.argsort(values)
    weights = np.asarray(weights, dtype=float)[order]
    positions = (np.cumsum(weights)-.5*weights)/weights.sum()
    return float(np.interp(quantile, positions, values[order]))


def _rows(rows, *, isolation=True):
    if not isinstance(rows, list) or not rows:
        raise ValueError("请提供实际采集的有效数据；没有数据时不能生成模型")
    normalized, session_splits, trial_splits = [], {}, {}
    definitions, sources = set(), set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 行必须为对象")
        row = {key: raw.get(key) for key in ("x_pm", "force_N", "point_id", "session_id", "trial_id", "phase", "baseline_id", "valid", "source_kind", "algorithm_version", "config_version", "split")}
        values = row["x_pm"]
        if not isinstance(values, (list, tuple)) or len(values) != 9 or not all(_finite_number(x) for x in values):
            raise ValueError(f"第 {index + 1} 行需要 9 个有限、有符号的 x_pm 特征")
        if not _finite_number(row["force_N"]):
            raise ValueError(f"第 {index + 1} 行缺少实际有限力标签 force_N")
        row["x_pm"], row["force_N"] = [float(x) for x in values], float(row["force_N"])
        if row["point_id"] not in (*POINT_IDS, None):
            raise ValueError("点位必须为 P01～P25，空载点位必须为 null")
        for key in ("session_id", "trial_id", "phase", "baseline_id", "algorithm_version", "config_version"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"第 {index + 1} 行缺少 {key}")
        if type(row["valid"]) is not bool or row["split"] not in {"A", "B", "C"}:
            raise ValueError("valid 必须为布尔值，split 必须为 A、B 或 C")
        if row["source_kind"] not in {"hardware", "synthetic"}:
            raise ValueError("来源必须可核实为 hardware；synthetic 仅用于显式诊断测试")
        if row["point_id"] is None and row["phase"] not in {"unloaded", "no_load", "baseline", "recovery"}:
            raise ValueError("空载行必须有明确无载阶段，不能猜测按压真值")
        if row["point_id"] is not None and row["force_N"] <= 0:
            raise ValueError("点位按压行需要正的实际力标签，空载不能归入点位类别")
        if row["point_id"] is not None and row["phase"] in {"unloaded", "no_load", "baseline"}:
            raise ValueError("无载行不能归入 P01～P25 点位类别")
        for key, mapping in (("session_id", session_splits), ("trial_id", trial_splits)):
            previous = mapping.setdefault(row[key], row["split"])
            if isolation and previous != row["split"]:
                raise ValueError(f"同一 {key} 不能跨训练、验证或测试 split")
        definitions.add((row["algorithm_version"], row["config_version"]))
        sources.add(row["source_kind"])
        normalized.append(row)
    if len(definitions) != 1 or len(sources) != 1:
        raise ValueError("不能混用来源、提峰算法或配置版本")
    return normalized, session_splits, trial_splits


def _balanced_weights(rows, width):
    """Each point, trial and occupied actual-force bin has equal total weight."""
    groups = defaultdict(lambda: defaultdict(list))
    trials_per_point = defaultdict(set)
    for index, row in enumerate(rows):
        point, trial = row["point_id"], (row["session_id"], row["trial_id"])
        force_bin = math.floor(row["force_N"] / width) if point is not None else 0
        groups[(point, trial)][force_bin].append(index)
        trials_per_point[point].add(trial)
    result = np.zeros(len(rows), dtype=float)
    for (point, trial), bins in groups.items():
        for indices in bins.values():
            result[indices] = 1 / (len(trials_per_point) * len(trials_per_point[point]) * len(bins) * len(indices))
    return result / result.sum()


def _statistics(rows, width, floor):
    unloaded = [row for row in rows if row["point_id"] is None]
    if len(unloaded) < 2:
        raise ValueError("A 组至少需要 2 个真实无载样本估计噪声")
    weights = _balanced_weights(unloaded, width)
    values = np.asarray([row["x_pm"] for row in unloaded])
    origin = np.sum(values * weights[:, None], axis=0)
    variance = np.sum((values-origin)**2 * weights[:, None], axis=0)
    return origin, np.maximum(variance, floor**2)


def _linear_models(rows, origin, width):
    templates, curves = [], []
    for point in POINT_IDS:
        selected = [row for row in rows if row["point_id"] == point]
        if not selected:
            raise ValueError(f"A 组缺少 {point} 的真实按压数据，不能建立完整 25 点模型")
        weights = _balanced_weights(selected, width)
        force = np.asarray([row["force_N"] for row in selected])
        features = np.asarray([row["x_pm"] for row in selected])-origin
        slope = np.sum(weights[:, None]*force[:, None]*features, axis=0) / np.sum(weights*force**2)
        if np.linalg.norm(slope) <= _EPS:
            raise ValueError(f"{point} 无可辨识响应，不能建立响应模板")
        maximum = float(force.max())
        templates.append({"point_id": point, "slope_pm_per_N": slope.tolist(), "max_training_force_N": maximum})
        bins = defaultdict(list)
        for index, value in enumerate(force):
            bins[math.floor(value/width)].append(index)
        knots, responses = [0.0], [np.zeros(9).tolist()]
        for indices in bins.values():
            local = weights[indices] / weights[indices].sum()
            knots.append(float(np.sum(force[indices]*local)))
            responses.append(np.sum(features[indices]*local[:, None], axis=0).tolist())
        ordered = sorted(zip(knots, responses), key=lambda item: item[0])
        curves.append({"point_id": point, "force_knots_N": [item[0] for item in ordered],
                       "x_knots_pm": [item[1] for item in ordered], "max_training_force_N": maximum})
    return {"kind": "linear", "templates": templates}, {"kind": "piecewise", "templates": curves}


def _softmax(logits):
    values = np.exp(logits-np.max(logits, axis=-1, keepdims=True))
    return values / values.sum(axis=-1, keepdims=True)


def _fit_mlp(rows, options, width):
    epochs, seed = options["mlp_epochs"], options["seed"]
    rng = np.random.default_rng(seed)
    x = np.asarray([row["x_pm"] for row in rows])
    weights = _balanced_weights(rows, width)
    mean = np.sum(x*weights[:, None], axis=0)
    scale = np.sqrt(np.sum((x-mean)**2*weights[:, None], axis=0))
    scale = np.maximum(scale, options["noise_floor_pm"])
    x = (x-mean)/scale
    force_scale = max(max(abs(row["force_N"]) for row in rows), .01)
    targets = np.asarray([row["force_N"]/force_scale for row in rows])
    classes = np.asarray([POINT_IDS.index(row["point_id"]) if row["point_id"] else -1 for row in rows])
    parameters = []
    for left, right in ((9, 64), (64, 32), (32, 26)):
        parameters.extend([rng.normal(0, math.sqrt(2/left), (left, right)), np.zeros(right)])
    first, second = [np.zeros_like(p) for p in parameters], [np.zeros_like(p) for p in parameters]
    step = 0
    for _ in range(epochs):
        order = rng.permutation(len(rows))
        for start in range(0, len(rows), options["mlp_batch_size"]):
            indices = order[start:start+options["mlp_batch_size"]]
            batch, local_weights = x[indices], weights[indices]
            local_weights = local_weights/local_weights.sum()
            w1, b1, w2, b2, w3, b3 = parameters
            a1 = np.maximum(0, batch@w1+b1)
            a2 = np.maximum(0, a1@w2+b2)
            output = a2@w3+b3
            gradient = np.zeros_like(output)
            loaded = classes[indices] >= 0
            if loaded.any():
                probabilities = _softmax(output[loaded, :25])
                probabilities[np.arange(loaded.sum()), classes[indices][loaded]] -= 1
                classification_weights = local_weights[loaded]/local_weights[loaded].sum()
                gradient[loaded, :25] = probabilities*classification_weights[:, None]
            gradient[:, 25] = 2*(output[:, 25]-targets[indices])*local_weights
            g3, gb3 = a2.T@gradient, gradient.sum(axis=0)
            d2 = (gradient@w3.T)*(a2 > 0)
            g2, gb2 = a1.T@d2, d2.sum(axis=0)
            d1 = (d2@w2.T)*(a1 > 0)
            gradients = [batch.T@d1, d1.sum(axis=0), g2, gb2, g3, gb3]
            norm = math.sqrt(sum(float(np.sum(g*g)) for g in gradients))
            step += 1
            for index, (parameter, grad) in enumerate(zip(parameters, gradients)):
                grad *= min(1.0, 10/max(norm, _EPS))
                first[index] = .9*first[index]+.1*grad
                second[index] = .999*second[index]+.001*grad*grad
                parameter -= options["mlp_learning_rate"]*(first[index]/(1-.9**step))/(np.sqrt(second[index]/(1-.999**step))+1e-8)
    return {"kind": "mlp", "architecture": [9, 64, 32, 26], "classification_outputs": 25,
            "force_outputs": 1, "feature_mean_pm": mean.tolist(), "feature_scale_pm": scale.tolist(),
            "force_scale_N": force_scale, "epochs": epochs, "seed": seed,
            "layers": [{"weights": parameters[i].tolist(), "bias": parameters[i+1].tolist()} for i in (0, 2, 4)]}


def _linear_candidate(template, x, variance, maximum_force_N):
    slope = np.asarray(template["slope_pm_per_N"])
    raw_force = float(np.sum(slope*x/variance) / np.sum(slope*slope/variance))
    maximum = min(template["max_training_force_N"], maximum_force_N)
    force = float(np.clip(raw_force, 0, maximum))
    residual = float(np.sqrt(np.mean((x-force*slope)**2/variance)))
    unconstrained = float(np.sqrt(np.mean((x-raw_force*slope)**2/variance)))
    return {"point_id": template["point_id"], "raw_force_N": raw_force, "fit_force_N": force,
            "residual": residual, "unconstrained_residual": unconstrained,
            "max_training_force_N": template["max_training_force_N"]}


def _piecewise_candidate(template, x, variance, maximum_force_N):
    forces, responses = np.asarray(template["force_knots_N"]), np.asarray(template["x_knots_pm"])
    raw_best, fit_best = None, None
    maximum = min(template["max_training_force_N"], maximum_force_N)
    for index in range(len(forces)-1):
        direction = responses[index+1]-responses[index]
        denominator = float(np.sum(direction*direction/variance))
        if denominator <= _EPS:
            continue
        fraction = float(np.sum(direction*(x-responses[index])/variance)/denominator)
        if index > 0:
            fraction = max(0.0, fraction)
        if index < len(forces)-2:
            fraction = min(1.0, fraction)
        value = responses[index]+fraction*direction
        residual = float(np.sqrt(np.mean((x-value)**2/variance)))
        candidate = {"point_id": template["point_id"], "raw_force_N": float(forces[index]+fraction*(forces[index+1]-forces[index])),
                     "residual": residual, "max_training_force_N": template["max_training_force_N"]}
        if raw_best is None or residual < raw_best["residual"]:
            raw_best = candidate
        # Project independently onto this segment's feasible force interval.
        # First/last extrapolation is permitted only up to the measured/requested
        # bound; its unrestricted optimum remains available as a range warning.
        step = forces[index+1]-forces[index]
        lower = max((0-forces[index])/step, 0.0 if index > 0 else -math.inf)
        upper = min((maximum-forces[index])/step, 1.0 if index < len(forces)-2 else math.inf)
        if lower > upper:
            continue
        feasible = float(np.clip(fraction, lower, upper))
        fit_residual = float(np.sqrt(np.mean((x-responses[index]-feasible*direction)**2/variance)))
        if fit_best is None or fit_residual < fit_best["residual"]:
            fit_best = {"fit_force_N": float(np.clip(forces[index]+feasible*step, 0, maximum)), "residual": fit_residual}
    if raw_best is None or fit_best is None:
        raise ValueError("分段响应模板没有可辨识线段")
    return {**raw_best, "unconstrained_residual": raw_best["residual"], **fit_best}


def _mlp_output(model, raw_x):
    value = (raw_x-np.asarray(model["feature_mean_pm"]))/np.asarray(model["feature_scale_pm"])
    for index, layer in enumerate(model["layers"]):
        value = value@np.asarray(layer["weights"])+np.asarray(layer["bias"])
        if index < 2:
            value = np.maximum(0, value)
    return _softmax(value[:25]), float(value[25]*model["force_scale_N"])


def _candidates(bundle, method, raw_x):
    origin, variance = np.asarray(bundle["baseline_context"]["origin_pm"]), np.asarray(bundle["baseline_context"]["noise_variance_pm2"])
    x = raw_x-origin
    model = bundle["models"][method]
    if method == "mlp":
        probabilities, force = _mlp_output(model, raw_x)
        candidates = []
        for index, template in enumerate(bundle["models"]["linear"]["templates"]):
            response = force*np.asarray(template["slope_pm_per_N"])
            fit_force = float(np.clip(force, 0, min(template["max_training_force_N"], bundle["max_force_N"])))
            fit_response = fit_force*np.asarray(template["slope_pm_per_N"])
            candidates.append({"point_id": template["point_id"], "raw_force_N": force,
                               "fit_force_N": fit_force, "residual": float(np.sqrt(np.mean((x-fit_response)**2/variance))),
                               "unconstrained_residual": float(np.sqrt(np.mean((x-response)**2/variance))),
                               "class_score": float(probabilities[index]), "max_training_force_N": template["max_training_force_N"]})
        candidates.sort(key=lambda item: item["class_score"], reverse=True)
        margin = candidates[0]["class_score"]-candidates[1]["class_score"]
    else:
        function = _linear_candidate if method == "linear" else _piecewise_candidate
        candidates = sorted((function(template, x, variance, bundle["max_force_N"]) for template in model["templates"]), key=lambda item: item["residual"])
        margin = (candidates[1]["residual"]-candidates[0]["residual"])/max(candidates[1]["residual"], _EPS)
    return candidates, float(max(0, min(1, margin)))


def _select_thresholds(bundle, rows, options):
    unloaded, pressed = [r for r in rows if r["point_id"] is None], [r for r in rows if r["point_id"] is not None]
    if not unloaded or not pressed:
        raise ValueError("B 组必须同时含真实无载与按压样本，用于独立选择接触和拒识阈值")
    variance, origin = np.asarray(bundle["baseline_context"]["noise_variance_pm2"]), np.asarray(bundle["baseline_context"]["origin_pm"])
    scores = [float(np.sqrt(np.mean((np.asarray(r["x_pm"])-origin)**2/variance))) for r in unloaded]
    contact = options.get("contact_threshold")
    if contact is None:
        contact = max(.05, _quantile(scores, .995, _balanced_weights(unloaded, options["force_bin_width_N"]))*1.1)
    bundle["contact_threshold"] = float(contact)
    thresholds = {}
    for method in bundle["models"]:
        details, true_residuals = [], []
        limits = {template["point_id"]: min(template["max_training_force_N"], bundle["max_force_N"])
                  for template in bundle["models"]["linear"]["templates"]}
        in_range = [row for row in pressed if row["force_N"] <= limits[row["point_id"]]]
        if not in_range:
            raise ValueError("B 组缺少处于实际标定力范围内的按压样本，不能选择拒识阈值")
        validation_weights = _balanced_weights(in_range, options["force_bin_width_N"])
        for row in in_range:
            candidates, margin = _candidates(bundle, method, np.asarray(row["x_pm"]))
            actual = next(candidate for candidate in candidates if candidate["point_id"] == row["point_id"])
            true_residuals.append(actual["residual"])
            details.append((candidates[0]["point_id"] == row["point_id"], candidates[0]["residual"], margin))
        residual = max(.05, _quantile(true_residuals, .99, validation_weights)*1.2)
        choices = np.unique(np.r_[options["reject_margin"], np.linspace(options["reject_margin"], .99, 80)])
        def loss(margin):
            return sum(weight*((0 if correct else 5) if value <= residual and gap >= margin else (1 if correct else 0))
                       for (correct, value, gap), weight in zip(details, validation_weights))
        margin = float(min(choices, key=lambda choice: (loss(choice), choice)))
        thresholds[method] = {"residual_threshold": residual, "margin_threshold": margin,
                              "margin_definition": "softmax_top_two_gap_not_calibrated_probability" if method == "mlp" else "relative_best_second_residual_gap",
                              "selection_split": "B", "validation_press_count": len(pressed),
                              "validation_in_range_press_count": len(in_range)}
    bundle["thresholds"] = thresholds


def fit_models(rows, options=None):
    options = dict(options or {})
    if "epochs" in options:
        if "mlp_epochs" in options and options["mlp_epochs"] != options["epochs"]:
            raise ValueError("epochs 与 mlp_epochs 不一致")
        options["mlp_epochs"] = options["epochs"]
    data, sessions, trials = _rows(rows)
    valid = [row for row in data if row["valid"]]
    source = data[0]["source_kind"]
    if source != "hardware" and options.get("allow_synthetic_diagnostics") is not True:
        raise ValueError("没有真实硬件数据时不生成模型；合成数据仅允许显式算法诊断测试")
    defaults = {"max_force_N": 3.0, "force_bin_width_N": .25, "noise_floor_pm": .05,
                "reject_margin": .05, "include_mlp": True, "mlp_epochs": 100, "seed": 1729,
                "mlp_batch_size": 128, "mlp_learning_rate": .001, "method": "linear"}
    settings = {**defaults, **options}
    for key in ("max_force_N", "force_bin_width_N", "noise_floor_pm", "mlp_learning_rate"):
        if not _finite_number(settings[key]) or settings[key] <= 0:
            raise ValueError(f"{key} 必须为正有限数")
    if not _finite_number(settings["reject_margin"]) or not 0 <= settings["reject_margin"] < 1:
        raise ValueError("reject_margin 必须在 0～1 之间")
    if settings.get("contact_threshold") is not None and (not _finite_number(settings["contact_threshold"]) or settings["contact_threshold"] <= 0):
        raise ValueError("contact_threshold 必须为正有限数")
    if type(settings["include_mlp"]) is not bool:
        raise ValueError("include_mlp 必须为布尔值")
    for key, lower, upper in (("mlp_epochs", 1, 500), ("mlp_batch_size", 1, 4096), ("seed", 0, 2**32-1)):
        if type(settings[key]) is not int or not lower <= settings[key] <= upper:
            raise ValueError(f"{key} 必须为 {lower}～{upper} 的整数")
    if settings["method"] not in {"linear", "piecewise", "mlp"} or settings["method"] == "mlp" and not settings["include_mlp"]:
        raise ValueError("请选择已建立的 linear、piecewise 或 mlp 方法")
    training, validation = [r for r in valid if r["split"] == "A"], [r for r in valid if r["split"] == "B"]
    if not training:
        raise ValueError("A 组缺少有效训练数据")
    if not any(r["point_id"] is None for r in validation) or not any(r["point_id"] is not None for r in validation):
        raise ValueError("B 组必须同时含真实无载与按压样本，用于独立选择接触和拒识阈值")
    origin, variance = _statistics(training, settings["force_bin_width_N"], settings["noise_floor_pm"])
    linear, piecewise = _linear_models(training, origin, settings["force_bin_width_N"])
    bundle = {"schema": SCHEMA, "point_ids": list(POINT_IDS), "feature_ids": [f"FBG{i}" for i in range(1, 10)],
              "feature_unit": "pm", "force_unit": "N", "algorithm_version": data[0]["algorithm_version"],
              "config_version": data[0]["config_version"], "source_kind": source,
              "source_data_sha256": _digest(data), "primary_method": settings["method"],
              "max_force_N": float(settings["max_force_N"]), "max_force_provisional": True,
              "force_bin_width_N": float(settings["force_bin_width_N"]),
              "training_options": {key: settings[key] for key in defaults},
              "baseline_context": {"origin_pm": origin.tolist(), "noise_variance_pm2": variance.tolist(),
                                   "variance_floor_pm2": float(settings["noise_floor_pm"]**2),
                                   "training_baseline_ids": sorted({r["baseline_id"] for r in training}), "statistics_split": "A"},
              "provenance": {"session_splits": sessions, "trial_splits": trials,
                             "training_session_ids": sorted({r["session_id"] for r in training}),
                             "training_trial_ids": sorted({r["trial_id"] for r in training}),
                             "training_row_count": len(training), "validation_row_count": len(validation),
                             "test_rows_used_for_fitting": 0, "invalid_rows_excluded": len(data)-len(valid)},
              "grid": {"rows": 5, "columns": 5, "order": "P01_to_P25_row_major", "distance_unit": "adjacent_grid_spacing"},
              "models": {"linear": linear, "piecewise": piecewise},
              "formal_ready": False, "formal_blockers": ["诊断拟合不能替代现有正式训练门；仍需真实无载/按压同批算法比较、实测漂移范围和操作员审阅"],
              "diagnostic_only": True}
    if settings["include_mlp"]:
        bundle["models"]["mlp"] = _fit_mlp(training, settings, settings["force_bin_width_N"])
    _select_thresholds(bundle, validation, settings)
    bundle["bundle_sha256"] = _digest(bundle)
    validate_bundle(bundle)
    return bundle


def _vector(value, size, name, *, positive=False):
    try:
        result = np.asarray(value, dtype=float)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"模型 {name} 格式错误") from exc
    if result.shape != (size,) or not np.isfinite(result).all() or positive and not (result > 0).all():
        raise ValueError(f"模型 {name} 尺寸或有限值错误")
    return result


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA or bundle.get("point_ids") != list(POINT_IDS):
        raise ValueError("模型版本或 25 点身份定义不兼容")
    if bundle.get("feature_ids") != [f"FBG{i}" for i in range(1, 10)] or bundle.get("feature_unit") != "pm" or bundle.get("force_unit") != "N" or bundle.get("source_kind") not in {"hardware", "synthetic"}:
        raise ValueError("模型特征身份、单位或来源不兼容")
    if bundle.get("formal_ready") is not False or bundle.get("diagnostic_only") is not True:
        raise ValueError("本模块只允许诊断模型，不能绕过正式训练门")
    for key in ("algorithm_version", "config_version"):
        if not isinstance(bundle.get(key), str) or not bundle[key]:
            raise ValueError("模型缺少算法与配置版本锁")
    baseline = bundle.get("baseline_context", {})
    _vector(baseline.get("origin_pm"), 9, "origin_pm")
    _vector(baseline.get("noise_variance_pm2"), 9, "noise_variance_pm2", positive=True)
    if baseline.get("statistics_split") != "A" or not _finite_number(bundle.get("force_bin_width_N")) or bundle["force_bin_width_N"] <= 0:
        raise ValueError("模型噪声统计必须来自 A 组，实际力箱宽度必须为正")
    if not _finite_number(bundle.get("max_force_N")) or bundle["max_force_N"] <= 0 or not _finite_number(bundle.get("contact_threshold")) or bundle["contact_threshold"] <= 0:
        raise ValueError("模型缺少有效接触阈值或力范围")
    models = bundle.get("models", {})
    if not {"linear", "piecewise"} <= models.keys() or set(models)-{"linear", "piecewise", "mlp"} or bundle.get("primary_method") not in models:
        raise ValueError("模型方法不完整")
    for method in ("linear", "piecewise"):
        templates = models[method].get("templates", [])
        if [template.get("point_id") for template in templates] != list(POINT_IDS):
            raise ValueError("响应模板身份或顺序错误")
        for template in templates:
            if not _finite_number(template.get("max_training_force_N")) or template["max_training_force_N"] <= 0:
                raise ValueError("模板缺少实际训练力范围")
            if method == "linear":
                if np.linalg.norm(_vector(template.get("slope_pm_per_N"), 9, "slope_pm_per_N")) <= _EPS:
                    raise ValueError("线性模板响应不可辨识")
            else:
                knots = np.asarray(template.get("force_knots_N"), dtype=float)
                responses = np.asarray(template.get("x_knots_pm"), dtype=float)
                if knots.ndim != 1 or len(knots) < 2 or not np.isfinite(knots).all() or knots[0] != 0 or not (np.diff(knots) > 0).all() or responses.shape != (len(knots), 9) or not np.isfinite(responses).all() or not np.allclose(responses[0], 0, atol=0, rtol=0):
                    raise ValueError("分段模板必须共享零力原点且力节点严格递增")
    if "mlp" in models:
        mlp = models["mlp"]
        _vector(mlp.get("feature_mean_pm"), 9, "feature_mean_pm")
        _vector(mlp.get("feature_scale_pm"), 9, "feature_scale_pm", positive=True)
        if not _finite_number(mlp.get("force_scale_N")) or mlp["force_scale_N"] <= 0 or len(mlp.get("layers", [])) != 3:
            raise ValueError("MLP 输出或层数错误")
        for layer, shape in zip(mlp["layers"], ((9, 64), (64, 32), (32, 26))):
            weights = np.asarray(layer.get("weights"), dtype=float)
            if weights.shape != shape or not np.isfinite(weights).all():
                raise ValueError("MLP 权重尺寸错误")
            _vector(layer.get("bias"), shape[1], "MLP bias")
    for method in models:
        threshold = bundle.get("thresholds", {}).get(method, {})
        if not _finite_number(threshold.get("residual_threshold")) or threshold["residual_threshold"] <= 0 or not _finite_number(threshold.get("margin_threshold")) or not 0 <= threshold["margin_threshold"] <= 1 or threshold.get("selection_split") != "B":
            raise ValueError("模型缺少独立 B 组拒识阈值")
    source_digest, checksum = bundle.get("source_data_sha256"), bundle.get("bundle_sha256")
    if not isinstance(source_digest, str) or len(source_digest) != 64 or any(c not in "0123456789abcdef" for c in source_digest):
        raise ValueError("模型缺少源数据摘要")
    copy = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    if checksum != _digest(copy):
        raise ValueError("模型内容校验失败")
    return True


def save_bundle(bundle, path):
    validate_bundle(bundle)
    Path(path).write_text(json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def load_bundle(path):
    bundle = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    validate_bundle(bundle)
    return bundle


def _invalid(reason):
    return {"status": "无效", "reason": reason, "point_id": None, "force_N": None,
            "raw_force_N": None, "fit_force_N": None, "unconstrained_residual": None,
            "range_candidate": None, "confidence": None, "best": None, "second": None,
            "candidates": [], "methods": {}, "formal_ready": False}


def predict(bundle, x_pm, quality_context=None):
    validate_bundle(bundle)
    if not isinstance(x_pm, (list, tuple, np.ndarray)) or len(x_pm) != 9 or not all(_finite_number(x) for x in x_pm):
        return _invalid("需要 FBG1～FBG9 的 9 个有限、有符号特征")
    context = quality_context or {}
    if not isinstance(context, dict):
        return _invalid("质量上下文格式错误")
    if context.get("valid") is False or context.get("stale") is True or context.get("display_stale") is True:
        return _invalid("当前光谱无效或已过期")
    if any(_finite_number(context.get(key)) and context[key] > 1000 for key in ("age_ms", "display_age_ms")):
        return _invalid("当前特征超过 1 秒未更新")
    for key in ("algorithm_version", "config_version", "source_kind"):
        if key in context and context[key] != bundle[key]:
            return _invalid("输入来源、算法或配置版本与模型不一致")
    x = np.asarray(x_pm, dtype=float)
    baseline = bundle["baseline_context"]
    contact_score = float(np.sqrt(np.mean((x-np.asarray(baseline["origin_pm"]))**2/np.asarray(baseline["noise_variance_pm2"]))))
    predictions = {}
    for method in bundle["models"]:
        candidates, margin = _candidates(bundle, method, x)
        best, second = candidates[:2]
        thresholds = bundle["thresholds"][method]
        maximum = min(bundle["max_force_N"], best["max_training_force_N"])
        # A credible unrestricted continuation outside the feasible calibration
        # interval remains an explicit range warning. It never becomes an
        # accepted boundary-force estimate, even when constrained E increases.
        unrestricted = sorted(candidates, key=lambda item: item["unconstrained_residual"]) if method != "mlp" else candidates
        range_candidate = unrestricted[0]
        raw_margin = ((unrestricted[1]["unconstrained_residual"]-range_candidate["unconstrained_residual"])
                      / max(unrestricted[1]["unconstrained_residual"], _EPS)) if method != "mlp" else margin
        range_limit = min(bundle["max_force_N"], range_candidate["max_training_force_N"])
        outside = range_candidate["raw_force_N"] < 0 or range_candidate["raw_force_N"] > range_limit
        credible_range = outside and range_candidate["unconstrained_residual"] <= thresholds["residual_threshold"] and raw_margin >= thresholds["margin_threshold"]
        if contact_score <= bundle["contact_threshold"]:
            status, reason = "无接触", "特征未超过独立 B 组确定的无载接触阈值"
        elif credible_range:
            status, reason = "超范围", f"{range_candidate['point_id']} 的可信未约束候选超出标定力范围；边界拟合值仅用于残差评分，未作为实测力"
        elif best["residual"] > thresholds["residual_threshold"] or margin < thresholds["margin_threshold"]:
            status, reason = "定位不确定", "候选残差过大或最优、次优候选区分不足"
        elif best["raw_force_N"] < 0 or best["raw_force_N"] > maximum:
            status, reason = "超范围", "候选原始力超出已观测训练范围或暂定允许范围；未裁切"
        else:
            status, reason = "有效估计", "通过接触、候选区分、残差和训练力范围检查；仅诊断估计"
        reported = range_candidate if status == "超范围" and credible_range else best
        predictions[method] = {"method": method, "status": status, "reason": reason,
                               "point_id": best["point_id"] if status == "有效估计" else None,
                               "force_N": best["fit_force_N"] if status == "有效估计" else 0.0 if status == "无接触" else None,
                               "raw_force_N": reported["raw_force_N"], "fit_force_N": reported["fit_force_N"],
                               "unconstrained_residual": reported["unconstrained_residual"], "range_candidate": range_candidate if credible_range else None,
                               "confidence": margin,
                               "confidence_definition": "候选区分度，非校准概率", "best": best, "second": second,
                               "candidates": candidates, **thresholds, "contact_score": contact_score,
                               "contact_threshold": bundle["contact_threshold"], "accepted_max_force_N": min(bundle["max_force_N"], reported["max_training_force_N"]),
                               "formal_ready": False}
    return {**predictions[bundle["primary_method"]], "methods": predictions, "formal_ready": False}


def _errors(values, weights=None):
    values = np.asarray(values, dtype=float)
    if not len(values):
        return {"n": 0, "mae_N": None, "rmse_N": None, "p95_abs_error_N": None, "bias_N": None}
    weights = np.ones(len(values))/len(values) if weights is None else np.asarray(weights)/np.sum(weights)
    return {"n": len(values), "mae_N": float(np.sum(np.abs(values)*weights)), "rmse_N": float(np.sqrt(np.sum(values**2*weights))),
            "p95_abs_error_N": _quantile(np.abs(values), .95, weights), "bias_N": float(np.sum(values*weights))}


def _evaluate_group(bundle, rows, method, predictions):
    matrix = np.zeros((25, 25), dtype=int)
    known, joint, accepted_errors, accepted_weights, distances = [], [], [], [], []
    bins, sessions = defaultdict(list), defaultdict(list)
    statuses, accepted_correct, accepted_count, correct, accepted_mass = defaultdict(int), 0, 0, 0, 0
    loaded = [row for row in rows if row["point_id"] is not None]
    weights = _balanced_weights(loaded, bundle["force_bin_width_N"]) if loaded else np.asarray([])
    for row, weight in zip(loaded, weights):
        prediction = predictions[id(row)]["methods"][method]
        actual_index = POINT_IDS.index(row["point_id"])
        chosen = POINT_IDS.index(prediction["best"]["point_id"])
        matrix[actual_index, chosen] += 1
        same = actual_index == chosen
        correct += weight*int(same)
        statuses[prediction["status"]] += 1
        raw_force = prediction["best"]["raw_force_N"]
        joint_error = raw_force-row["force_N"]
        joint.append(joint_error)
        actual_candidate = next(candidate for candidate in prediction["candidates"] if candidate["point_id"] == row["point_id"])
        known.append(actual_candidate["raw_force_N"]-row["force_N"])
        distances.append(math.hypot(actual_index//5-chosen//5, actual_index % 5-chosen % 5))
        if prediction["status"] == "有效估计":
            accepted_count += 1
            accepted_correct += weight*int(same)
            accepted_mass += weight
            accepted_errors.append(joint_error)
            accepted_weights.append(weight)
        bins[math.floor(row["force_N"]/bundle["force_bin_width_N"])].append((joint_error, weight))
        sessions[row["session_id"]].append((joint_error, weight))
    unloaded = [row for row in rows if row["point_id"] is None]
    false_contacts = sum(weight*(predictions[id(row)]["methods"][method]["status"] != "无接触")
                         for row, weight in zip(unloaded, _balanced_weights(unloaded, bundle["force_bin_width_N"]))) if unloaded else None
    count = len(joint)
    return {"n": count, "known_point_force": _errors(known, weights), "joint_force": _errors(joint, weights),
            "accepted_joint_force": _errors(accepted_errors, accepted_weights), "classification_accuracy": float(np.clip(correct, 0, 1)) if count else None,
            "weighting": "各点、各试次与有样本实际力箱等权；混淆矩阵另保留原始行计数",
            "classification_scope": "全部有效按压行的原始最优候选，另报拒识及已接受准确率",
            "accepted_classification_accuracy": float(np.clip(accepted_correct/accepted_mass, 0, 1)) if accepted_mass else None,
            "confusion_matrix": matrix.tolist(), "confusion_point_ids": list(POINT_IDS),
            "localization": {"mean_normalized_grid_distance": float(np.sum(np.asarray(distances)*weights)) if distances else None,
                             "p95_normalized_grid_distance": _quantile(distances, .95, weights) if distances else None,
                             "unit": "相邻格距=1，P01～P25按5×5逐行排列；无毫米精度声明"},
            "rejection_rate": float(np.clip(1-accepted_mass, 0, 1)) if count else None, "rejected_row_fraction": (count-accepted_count)/count if count else None, "status_counts": dict(statuses),
            "contact_detection": {"unloaded_n": len(unloaded), "false_contact_rate": float(np.clip(false_contacts, 0, 1)) if false_contacts is not None else None},
            "by_force_bin": [{"low_N": key*bundle["force_bin_width_N"], "high_N": (key+1)*bundle["force_bin_width_N"], **_errors(*zip(*values))} for key, values in sorted(bins.items())],
            "by_session": {key: {**_errors(*zip(*values)), "independent_of_training": key not in bundle["provenance"]["training_session_ids"]} for key, values in sessions.items()}}


def _point_curves(rows, maximum=80):
    grouped = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row["valid"] and row["point_id"] is not None:
            grouped[row["point_id"]][(row["session_id"], row["trial_id"], row["split"])].append(row)
    result = {}
    for point, trials in grouped.items():
        result[point] = {"trials": []}
        for (session, trial, split), values in trials.items():
            indices = np.linspace(0, len(values)-1, min(maximum, len(values)), dtype=int)
            selected = [values[index] for index in indices]
            result[point]["trials"].append({"session_id": session, "trial_id": trial, "split": split,
                                             "force_N": [r["force_N"] for r in selected], "x_pm": [r["x_pm"] for r in selected],
                                             "phase": [r["phase"] for r in selected], "original_count": len(values), "display_count": len(selected)})
    return result


def evaluate_models(bundle, rows):
    validate_bundle(bundle)
    data, _, _ = _rows(rows)
    for row in data:
        if any(row[key] != bundle[key] for key in ("source_kind", "algorithm_version", "config_version")):
            raise ValueError("评估数据来源、算法或配置与模型不一致")
        for key, mapping in (("session_id", bundle["provenance"]["session_splits"]), ("trial_id", bundle["provenance"]["trial_splits"])):
            if row[key] in mapping and mapping[row[key]] != row["split"]:
                raise ValueError("独立评估不能把已有训练或验证会话/试次改标为其他 split")
    valid = [row for row in data if row["valid"]]
    predictions = {id(row): predict(bundle, row["x_pm"]) for row in valid}
    session_ids = sorted({row["session_id"] for row in valid})
    methods = {method: {"overall": _evaluate_group(bundle, valid, method, predictions),
                        "by_split": {split: _evaluate_group(bundle, [row for row in valid if row["split"] == split], method, predictions) for split in ("A", "B", "C")},
                        "by_session": {session: {**_evaluate_group(bundle, [row for row in valid if row["session_id"] == session], method, predictions),
                                                 "independent_of_training": session not in bundle["provenance"]["training_session_ids"]}
                                       for session in session_ids}}
               for method in bundle["models"]}
    return {"schema": "9fbg_response_evaluation_v1", "models": list(bundle["models"]), "methods": methods,
            "source_data_sha256": _digest(data), "model_bundle_sha256": bundle["bundle_sha256"],
            "point_curves": _point_curves(data), "provenance": deepcopy(bundle["provenance"]),
            "excluded_invalid_rows": len(data)-len(valid), "formal_ready": False,
            "interpretation": "A为拟合内诊断，B参与拒识阈值选择，只有独立C组可作最终留出评估；未接受估计也保留原始候选误差"}


def preview_rows(rows):
    """Pilot diagnostics for any measured subset; creates no trainable bundle."""
    data, _, _ = _rows(rows, isolation=False)
    valid = [row for row in data if row["valid"]]
    unloaded = [row for row in valid if row["point_id"] is None]
    origin = np.mean([row["x_pm"] for row in unloaded], axis=0) if unloaded else np.zeros(9)
    points = {}
    for point in POINT_IDS:
        selected = [row for row in valid if row["point_id"] == point]
        if not selected:
            continue
        weights = _balanced_weights(selected, .25)
        force = np.asarray([row["force_N"] for row in selected])
        values = np.asarray([row["x_pm"] for row in selected])-origin
        slope = np.sum(weights[:, None]*force[:, None]*values, axis=0)/np.sum(weights*force**2)
        residuals = values-force[:, None]*slope
        residual_variance = np.sum(weights[:, None]*residuals**2, axis=0)
        total_variance = np.sum(weights[:, None]*(values-np.sum(weights[:, None]*values, axis=0))**2, axis=0)
        r2 = [float(1-residual_variance[index]/variance) if variance > _EPS else None for index, variance in enumerate(total_variance)]
        trials = defaultdict(list)
        for row in selected:
            trials[(row["session_id"], row["trial_id"])].append(row)
        statistics = []
        for (session, trial), records in trials.items():
            baselines = {row["baseline_id"] for row in records}
            noise = [row["x_pm"] for row in unloaded if row["session_id"] == session and row["baseline_id"] in baselines]
            holds = [row for row in records if "hold" in row["phase"].lower() or "保持" in row["phase"]]
            hold_residuals = [np.asarray(row["x_pm"])-origin-row["force_N"]*slope for row in holds]
            bins = defaultdict(list)
            for row in records:
                bins[math.floor(row["force_N"]/.25)].append(row["x_pm"])
            within = [np.asarray(group)-np.mean(group, axis=0) for group in bins.values() if len(group) > 1]
            statistics.append({"trial_id": trial, "session_id": session, "n": len(records),
                               "noise_std_pm": np.std(noise, axis=0, ddof=1).tolist() if len(noise) >= 2 else None,
                               "noise_source": "同会话同baseline_id的无载样本" if len(noise) >= 2 else "缺少匹配无载噪声样本",
                               "within_force_bin_std_pm": np.sqrt(np.mean(np.concatenate(within)**2, axis=0)).tolist() if within else None,
                               "hold_n": len(holds), "hold_residual_rms_pm": float(np.sqrt(np.mean(np.asarray(hold_residuals)**2))) if holds else None})
        points[point] = {"slope_pm_per_N": slope.tolist(), "intercept_pm": origin.tolist(), "r2_per_fbg": r2,
                         "residual_rmse_pm": np.sqrt(residual_variance).tolist(), "residual_rms_pm": float(np.sqrt(np.mean(residual_variance))),
                         "force_range_N": [float(force.min()), float(force.max())],
                         "n": len(selected), "trial_statistics": statistics}
    return {"schema": "9fbg_response_pilot_preview_v1", "model_generated": False, "formal_ready": False,
            "point_curves": _point_curves(data), "points": points,
            "quality_summary": {"total_rows": len(data), "valid_rows": len(valid), "invalid_rows": len(data)-len(valid),
                                "unloaded_rows": len(unloaded), "press_rows": sum(r["point_id"] is not None for r in valid),
                                "points_present": list(points), "session_count": len({r["session_id"] for r in valid}),
                                "trial_count": len({r["trial_id"] for r in valid}), "source_kind": data[0]["source_kind"],
                                "algorithm_version": data[0]["algorithm_version"], "config_version": data[0]["config_version"],
                                "noise_verified": len(unloaded) >= 2}, "source_data_sha256": _digest(data),
            "interpretation": "试采诊断仅报告实际数据的斜率、噪声和保持残差；不生成模型，不构成正式训练或独立测试"}
