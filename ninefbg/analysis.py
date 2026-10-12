"""Offline experiment tables/models and FBG-only live research predictions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import threading
import time
import uuid

from .storage import clean
from .tracking import ALGORITHM_VERSION


_METHOD_LABELS = {"linear": "加权最小二乘", "mlp": "小型 MLP"}


def _id(value):
    if not isinstance(value, str) or not value or len(value) > 100 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value):
        raise ValueError("记录标识格式无效")
    return value


def _digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _training_source_paths(userdata, session_ids):
    """A physical archive is one source, even if a lookup returns aliases."""
    from .history import session_path
    paths, seen = [], set()
    for session_id in session_ids:
        _id(session_id)
        path = Path(session_path(userdata, session_id, include_deleted=False)).resolve()
        identity = os.path.normcase(str(path))
        if identity in seen:
            raise ValueError("所选记录指向同一原始会话，不能重复计入训练或分析")
        seen.add(identity)
        paths.append(path)
    for session_id, path in zip(session_ids, paths):
        if path.name != session_id:
            raise ValueError("采集数据标识大小写与保存目录不一致，请刷新列表")
    return paths


def _physical_definition(settings):
    """Keep absent historical coordinate semantics absent, rather than migrate them."""
    definition = {key: deepcopy(settings.get(key)) for key in ("finger_id", "point_grid", "force_sign")}
    if "coordinate_definition" in settings:
        definition["coordinate_definition"] = deepcopy(settings["coordinate_definition"])
    return definition


def _training_conditions(settings):
    """Compare declared collection conditions without migrating old definitions."""
    conditions = settings.get("conditions") or {}
    if not isinstance(conditions, dict):
        raise ValueError("会话装夹与环境条件格式无效")

    def normalize(key, value):
        if value is None or isinstance(value, str) and not value.strip():
            return None
        if key == "fixation":
            if not isinstance(value, str):
                raise ValueError("会话装夹方式必须为文本")
            return value.strip()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("会话温度必须为有限数值或留空")
        return float(value)

    result = {}
    for key in ("fixation", "temperature_C"):
        nested = normalize(key, conditions.get(key))
        historical = normalize(key, settings.get(key))
        if key in conditions and key in settings and nested != historical:
            raise ValueError("会话装夹或温度元数据存在冲突")
        result[key] = nested if key in conditions else historical
    return result


def _incompatible_sources(reason, session_id, settings, first_id, first_settings):
    def label(value, metadata):
        data_id = metadata.get("data_id")
        return value + ("（数据编号 " + data_id + "）" if isinstance(data_id, str) and data_id.strip() else "")
    return ValueError(reason + "；不兼容记录：" + label(session_id, settings) +
                      "；与首条记录 " + label(first_id, first_settings) + " 不同，请分开训练")


def _archived_source_path(userdata, session_id):
    from .history import session_path
    try:
        return session_path(userdata, session_id, include_deleted=True)
    except ValueError as exc:
        # Preserve the existing portable-model behavior only for absent data;
        # ambiguous directories or unsafe paths must still reject the model.
        if str(exc) != "采集数据不存在或已删除":
            raise
        return None


def _model_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _model_sources(userdata, bundle):
    """Read explicit source metadata; never derive data IDs/dates from filenames."""
    sources = (bundle.get("acquisition_provenance") or {}).get("sources", [])
    result = []
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, dict) or not isinstance(source.get("session_id"), str):
            continue
        record = {"session_id": source["session_id"], "data_id": source.get("data_id"),
                  "started_utc": source.get("started_utc"), "ended_utc": source.get("ended_utc"),
                  "manifest_sha256": source.get("manifest_sha256"), "manifest_verified": False}
        try:
            directory = _archived_source_path(userdata, _id(source["session_id"]))
            path = directory / "manifest.json" if directory is not None else None
            if path is not None and path.is_file() and _digest_file(path) == source.get("manifest_sha256"):
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(manifest, dict) and isinstance(manifest.get("settings"), dict):
                    record.update(data_id=manifest["settings"].get("data_id"),
                                  started_utc=manifest.get("started_utc"), ended_utc=manifest.get("ended_utc"),
                                  manifest_verified=True)
        except (ValueError, OSError, TypeError):
            pass
        for key in ("data_id", "started_utc", "ended_utc"):
            if not isinstance(record[key], str):
                record[key] = None
        for key in ("started", "ended"):
            stamp = _model_time(record[key + "_utc"])
            record[key + "_minute"] = stamp.isoformat(timespec="minutes") if stamp else None
        result.append(record)
    return result


def _validate_live_pilot(bundle, userdata):
    """Check an archived research model without refitting or changing references."""
    from .pilot import validate_pilot_model
    validate_pilot_model(bundle)
    if bundle.get("algorithm_version") != ALGORITHM_VERSION or bundle.get("source_kind") != "hardware":
        raise ValueError("研究模型必须来自真实硬件，且提峰算法须与当前固定算法一致")
    if bundle.get("scope") not in {"measured_points_same_session_research_only", "measured_points_multi_session_research_only"}:
        raise ValueError("研究模型适用范围定义不兼容")
    provenance = bundle.get("acquisition_provenance")
    physical = provenance.get("physical_definition") if isinstance(provenance, dict) else None
    if not isinstance(physical, dict) or not isinstance(physical.get("finger_id"), str) or not physical["finger_id"].strip() or type(physical.get("force_sign")) not in (int, float) or physical["force_sign"] not in (-1, 1):
        raise ValueError("研究模型缺少有效的触觉件或法向力物理定义")
    grid = physical.get("point_grid")
    point_ids = {f"P{i:02d}" for i in range(1, 26)}
    if not isinstance(grid, list) or len(grid) != 25 or any(not isinstance(point, dict) or not isinstance(point.get("id"), str) for point in grid) or {point["id"] for point in grid} != point_ids:
        raise ValueError("研究模型缺少完整的点位坐标定义")
    for point in grid:
        if any(type(point.get(key)) not in (int, float) or not math.isfinite(point[key]) for key in ("row", "col", "x_norm", "y_norm")):
            raise ValueError("研究模型点位坐标不是有限数值")
    if "coordinate_definition" in physical and (not isinstance(physical["coordinate_definition"], dict) or not isinstance(physical["coordinate_definition"].get("schema"), str) or not physical["coordinate_definition"]["schema"]):
        raise ValueError("研究模型的坐标约定版本无效")
    sources = provenance.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 100 or any(not isinstance(source, dict) for source in sources):
        raise ValueError("研究模型缺少完整的原始数据来源")
    source_ids = [_id(source.get("session_id")) for source in sources]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("研究模型原始数据来源重复")
    expected_scope = "measured_points_multi_session_research_only" if len(sources) > 1 else "measured_points_same_session_research_only"
    if bundle["scope"] != expected_scope:
        raise ValueError("研究模型适用范围与原始会话数量不一致")
    def sha256(value):
        return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)
    required = {"raw_spectra.jsonl", "raw_force.jsonl", "results.jsonl", "events.jsonl"}
    training_provenance = bundle.get("provenance")
    training_ids = training_provenance.get("training_session_ids") if isinstance(training_provenance, dict) else None
    if not isinstance(training_ids, list) or any(not isinstance(value, str) for value in training_ids) or sorted(training_ids) != sorted(source_ids):
        raise ValueError("研究模型训练会话与原始来源不一致")
    conditions = provenance.get("training_conditions")
    if conditions is not None and (not isinstance(conditions, dict) or conditions != _training_conditions(conditions)):
        raise ValueError("研究模型装夹与环境条件定义无效")
    if len(sources) > 1 and conditions is None:
        raise ValueError("多会话研究模型缺少装夹与环境相容性记录")
    for source, session_id in zip(sources, source_ids):
        files = source.get("files")
        if not sha256(source.get("manifest_sha256")) or not isinstance(files, dict) or set(files) != required or not all(sha256(value) for value in files.values()):
            raise ValueError("研究模型缺少原始四文件与清单的来源校验摘要")
        if conditions is not None and source.get("training_conditions") != conditions:
            raise ValueError("研究模型各来源的装夹或环境条件不一致")
        directory = _archived_source_path(userdata, session_id)
        path = directory / "manifest.json" if directory is not None else None
        if path is not None:
            if not path.is_file() or _digest_file(path) != source["manifest_sha256"]:
                raise ValueError("研究模型原始会话清单哈希不匹配")
            manifest = json.loads(path.read_text(encoding="utf-8"))
            settings = manifest.get("settings") if isinstance(manifest, dict) else None
            if not isinstance(settings, dict) or manifest.get("kind") != "9fbg_calibration_experiment_v1" or manifest.get("complete") is not True or _physical_definition(settings) != physical or manifest.get("files") != files:
                raise ValueError("研究模型与完整原始会话的物理定义或来源不一致")
            if conditions is not None and _training_conditions(settings) != conditions:
                raise ValueError("研究模型与原始会话的装夹或环境条件不一致")
    return True


def load_rows(userdata, session_ids, progress=None):
    """Verify immutable raw evidence and exclude aborted/marked entire trials."""
    from .experiment import get_session
    from .reprocess import reprocess_session
    if not isinstance(session_ids, list) or not session_ids or len(session_ids) > 100 or any(not isinstance(value, str) for value in session_ids) or len(set(session_ids)) != len(session_ids):
        raise ValueError("请选择不重复的已结束实验会话，最多 100 个")
    paths = _training_source_paths(userdata, session_ids)
    rows, sources, omitted, physical, training_conditions = [], [], {}, None, None
    first_settings = None
    for session_index, (session_id, path) in enumerate(zip(session_ids, paths), 1):
        manifest = get_session(userdata, session_id)
        if manifest.get("kind") != "9fbg_calibration_experiment_v1" or manifest.get("complete") is not True:
            raise ValueError("实验记录未完整结束，不能用于拟合：" + session_id)
        settings = manifest["settings"]
        signature = _physical_definition(settings)
        if physical is None:
            physical = signature
            first_settings = settings
        elif signature != physical:
            raise _incompatible_sources("不能混合不同触觉件、点位坐标或法向力定义的会话", session_id, settings, session_ids[0], first_settings)
        conditions = _training_conditions(settings)
        if training_conditions is None:
            training_conditions = conditions
        elif conditions != training_conditions:
            raise _incompatible_sources("不能混合不同装夹方式或温度环境条件的会话；留空须与留空一致", session_id, settings, session_ids[0], first_settings)
        if not settings.get("force_direction_confirmed") or not settings.get("coordinates_confirmed"):
            raise ValueError("会话缺少法向力方向或点位确认")
        def notify(value):
            if progress:
                progress({"session_id": session_id, "session_index": session_index,
                          "session_count": len(session_ids), **value})
        notify({"stage": "校验会话原始来源", "source_progress_percent": 0})
        files = manifest.get("files", {})
        if not {"raw_spectra.jsonl", "raw_force.jsonl", "results.jsonl", "events.jsonl"}.issubset(files):
            raise ValueError("实验缺少原始两路、逐峰结果或操作事件")
        for file_index, (name, expected) in enumerate(files.items(), 1):
            if name not in {"raw_spectra.jsonl", "raw_force.jsonl", "results.jsonl", "events.jsonl"} or _digest_file(path / name) != expected:
                raise ValueError("实验来源文件哈希不匹配：" + session_id + "/" + name)
            notify({"stage": "校验原始文件", "file": name,
                    "verified_files": file_index, "total_files": len(files)})
        trials = {trial["trial_id"]: trial for trial in manifest.get("final", {}).get("trials", [])}
        accepted = 0
        extracted = reprocess_session(path, manifest, progress=notify)
        notify({"stage": "本会话原始光谱补算完成", "source_progress_percent": 100})
        for row in extracted:
            trial = trials.get(row.get("trial_id"))
            reason = None
            if not row.get("valid") or row.get("bad") or row.get("manual_mark"):
                reason = "质量无效或人工异常"
            elif trial and (trial.get("status") != "completed" or trial.get("bad") or trial.get("manual_mark")):
                reason = "整次按压未完成或已标记异常"
            elif trial and settings.get("guided_acquisition") is True and (
                    not isinstance(trial.get("quality"), dict) or trial["quality"].get("accepted") is not True
                    or trial["quality"].get("criteria_version") != "guided_round_v1"):
                reason = "引导采集轮次缺少通过的完整质量检查"
            elif row.get("phase") not in {"unloaded", "preload", "loading", "hold"}:
                reason = "释放或恢复阶段仅保留原始记录"
            elif row.get("lateral_warning"):
                reason = "侧向分量超过初始检查比例"
            elif row.get("phase") in {"preload", "loading", "hold"} and not trial:
                reason = "缺少完整独立按压身份"
            elif row.get("phase") == "preload":
                force, threshold = row.get("force_N"), row.get("force_noise_threshold_N")
                threshold = max(0.05, threshold) if type(threshold) in (int, float) and math.isfinite(threshold) else 0.05
                if type(force) not in (int, float) or not math.isfinite(force) or abs(force) > threshold:
                    reason = "预采样力超过无载噪声范围，不作为无接触样本"
            elif row.get("phase") in {"loading", "hold"} and (type(row.get("force_N")) not in (int, float) or row["force_N"] <= row.get("force_noise_threshold_N", 0.05)):
                reason = "力接近空载噪声，不赋点位类别"
            if reason:
                omitted[reason] = omitted.get(reason, 0) + 1
                continue
            if row.get("phase") == "preload":
                row.update(recorded_phase="preload", phase="unloaded", point_id=None)
            # Baseline-only observations form a distinct group; no point label.
            if row.get("phase") == "unloaded":
                row["point_id"] = None
                row["trial_id"] = row.get("trial_id") or "baseline_" + str(row.get("baseline_id"))
            row["source_trial_id"] = row["trial_id"]
            row["trial_id"] = session_id + ":" + row["trial_id"]
            row["baseline_id"] = session_id + ":" + str(row["baseline_id"])
            row.update(session_id=session_id, split=settings["batch"], finger_id=settings["finger_id"])
            rows.append(row)
            accepted += 1
        sources.append({"session_id": session_id, "manifest_sha256": _digest_file(path / "manifest.json"),
                        "files": files, "accepted_rows": accepted, "raw_spectra": manifest.get("counts", {}).get("raw_spectra", 0),
                        "online_processed_rows": manifest.get("counts", {}).get("results", 0),
                        "offline_processed_rows": len(extracted), "feature_processing": "frozen_identity_raw_replay_v1",
                        "training_conditions": deepcopy(conditions)})
    if not rows:
        raise ValueError("所选会话没有通过基线、同步、九峰及完整试次检查的样本")
    return rows, {"sources": sources, "omitted": omitted, "physical_definition": physical,
                  "training_conditions": training_conditions,
                  "feature_coverage_note": "离线逐个补算原始光谱，沿用保存的固定身份窗口、算法配置和本次基线；原始与在线结果均不覆盖。仅质量有效且完整试次帧进入训练表。"}


class AnalysisController:
    def __init__(self, userdata):
        self.userdata = Path(userdata)
        self.lock = threading.RLock()
        self.busy, self.error, self.report = False, None, None
        self.job_id = self.report_id = self.model_id = None
        self.bundle = None
        self.job_progress = {}
        self.training = {"job_id": None, "busy": False, "status": "idle", "progress_percent": 0,
                         "stage": "等待训练", "method": None, "method_label": None, "error": None,
                         "model_id": None, "point_ids": [], "session_ids": [], "created_utc": None,
                         "created_local_minute": None}
        self.truth_zero = {"ready": False, "count": 0, "reason": "尚未建立独立测试六轴零点"}
        self.test_reference = {"ready": False, "pending": False, "reason": "请确认当前无接触并建立独立测试光学参考"}
        self._zero_samples, self._zero_seen = [], set()

    def request_truth_zero(self, context):
        with self.lock:
            force = context.get("force") or {}
            tracker, frame = context.get("tracker") or {}, context.get("frame") or {}
            requested_ns = time.perf_counter_ns()
            self.test_reference = {"ready": False, "pending": True, "requested_ns": requested_ns,
                                   "previous_reference_version": tracker.get("reference_version", 0),
                                   "reference_version": None,
                                   "epoch": frame.get("stream_epoch", tracker.get("stream_epoch")),
                                   "algorithm_version": tracker.get("algorithm_version"),
                                   "config_version": tracker.get("config_version"),
                                   "reason": "等待本次明确无载后的新光学参考"}
            self.truth_zero = {"ready": False, "count": 0, "pending": bool(context.get("force_connected")),
                               "requested_ns": requested_ns, "epoch": force.get("stream_epoch"),
                               "force_sign": (self.bundle or {}).get("acquisition_provenance", {}).get("physical_definition", {}).get("force_sign", -1), "reason": "收集无载六轴零点" if context.get("force_connected") else "未连接 PX6D；仅建立 FBG 测试参考"}
            self._zero_samples, self._zero_seen = [], set()

    def on_force(self, frame):
        with self.lock:
            zero = self.truth_zero
            if not zero.get("pending"):
                return
            stamp, epoch = frame.get("host_rx_monotonic_ns"), frame.get("stream_epoch")
            if epoch != zero.get("epoch"):
                zero.update(pending=False, ready=False, reason="PX6D 时段改变，请重新确认无载")
                return
            key = (epoch, frame.get("frame_id"))
            values = frame.get("force_values")
            if frame.get("source_kind") != "hardware" or frame.get("time_quality") not in {"host_rx_approx", "hardware_mapped"} or frame.get("valid") is False or frame.get("stale") is True or type(stamp) is not int or stamp <= zero["requested_ns"] or key in self._zero_seen or type(frame.get("frame_id")) not in (int, str) or not isinstance(values, list) or len(values) != 6 or not all(type(x) in (int, float) and math.isfinite(x) for x in values):
                return
            if self._zero_samples and stamp <= self._zero_samples[-1][0]:
                zero.update(pending=False, ready=False, reason="六轴时间回退，需重新确认无载")
                return
            if frame.get("axes") != ["Fx", "Fy", "Fz", "Mx", "My", "Mz"] or frame.get("units") != ["N", "N", "N", "N·m", "N·m", "N·m"]:
                return
            self._zero_seen.add(key)
            self._zero_samples.append((stamp, list(values)))
            zero["count"] = min(12, len(self._zero_samples))
            if len(self._zero_samples) >= 12 and stamp - self._zero_samples[0][0] >= 400_000_000:
                sample = [self._zero_samples[round(i * (len(self._zero_samples)-1) / 11)] for i in range(12)]
                zero.update(ready=True, pending=False, offsets=[statistics.median(row[1][axis] for row in sample) for axis in range(6)], reason="六轴无载测试零点已建立")

    def models(self):
        result = []
        for directory, prefix, kind in (("models", "M", "full"), ("pilot_models", "P", "pilot")):
            path = self.userdata / directory
            if not path.is_dir():
                continue
            for file in path.glob(prefix + "*.json"):
                try:
                    if file.stat().st_size > 32 * 1024 * 1024:
                        continue
                    data = json.loads(file.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        continue
                    if kind == "pilot":
                        _validate_live_pilot(data, self.userdata)
                    sources = _model_sources(self.userdata, data)
                    ids = list(dict.fromkeys(record["data_id"] for record in sources if record["data_id"]))
                    created = _model_time(data.get("created_utc"))
                    result.append({"model_id": file.stem, "model_kind": kind,
                                   "created_utc": data.get("created_utc"),
                                   "created_minute": created.isoformat(timespec="minutes") if created else None,
                                   "created_local_minute": created.astimezone().strftime("%Y-%m-%d %H:%M") if created else None,
                                   "method": data.get("primary_method", "linear"),
                                   "method_label": _METHOD_LABELS.get(data.get("primary_method", "linear"), data.get("primary_method")),
                                   "formal_ready": False, "research_only": True,
                                   "point_ids": deepcopy(data.get("point_ids", [])),
                                   "scope": data.get("scope", "full_25_point_research_only" if kind == "full" else None),
                                   "source_session_ids": [record["session_id"] for record in sources],
                                   "data_id": ids[0] if len(ids) == 1 else "", "data_ids": ids,
                                   "source_records": sources,
                                   "data_started_utc": sources[0]["started_utc"] if len(sources) == 1 else None,
                                   "data_ended_utc": sources[0]["ended_utc"] if len(sources) == 1 else None,
                                   "data_started_minute": sources[0]["started_minute"] if len(sources) == 1 else None,
                                   "data_ended_minute": sources[0]["ended_minute"] if len(sources) == 1 else None,
                                   "source_manifest_verified": bool(sources) and all(record["manifest_verified"] for record in sources),
                                   "algorithm_version": data.get("algorithm_version"), "config_version": data.get("config_version")})
                except (ValueError, OSError, TypeError, KeyError, AttributeError):
                    continue
        result.sort(key=lambda row: (_model_time(row["created_utc"]) or datetime.min.replace(tzinfo=timezone.utc), row["model_id"]), reverse=True)
        return result[:100]

    def load(self, model_id):
        from .inversion import validate_bundle
        with self.lock:
            if self.busy:
                raise ValueError("模型处理正在运行，请等待完成")
        model_id = _id(model_id)
        kind = "pilot" if model_id.startswith("P") else "full"
        file = self.userdata / ("pilot_models" if kind == "pilot" else "models") / (model_id + ".json")
        if not file.is_file() or file.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("模型不存在或文件大小不合要求")
        bundle = json.loads(file.read_text(encoding="utf-8"))
        if kind == "pilot":
            _validate_live_pilot(bundle, self.userdata)
        else:
            validate_bundle(bundle)
        if bundle.get("algorithm_version") != ALGORITHM_VERSION or bundle.get("source_kind") != "hardware":
            raise ValueError("模型提峰算法与当前固定算法不一致")
        with self.lock:
            if self.busy:
                raise ValueError("模型处理正在运行，请等待完成")
            self.model_id, self.bundle, self.error = model_id, bundle, None
            self._reset_truth_zero()
        return {"model_id": model_id, "model_kind": kind, "point_ids": deepcopy(bundle.get("point_ids", [])),
                "method": bundle.get("primary_method", "linear"),
                "method_label": _METHOD_LABELS.get(bundle.get("primary_method", "linear"), bundle.get("primary_method")),
                "research_only": True, "formal_ready": False}

    def _reset_truth_zero(self):
        self.truth_zero = {"ready": False, "pending": False, "count": 0,
                           "reason": "模型已改变，请确认当前无接触并建立独立测试参考"}
        self.test_reference = {"ready": False, "pending": False,
                               "reason": "模型已改变，请确认当前无接触并建立独立测试光学参考"}
        self._zero_samples, self._zero_seen = [], set()

    def _update_test_reference(self, tracker, source_kind, fresh_peaks):
        reference = self.test_reference
        if not (reference.get("ready") or reference.get("pending")):
            return
        age = tracker.get("display_age_ms")
        stale = tracker.get("display_stale") is True or type(age) in (int, float) and (not math.isfinite(age) or age > 1000)
        binding_changed = (source_kind != "hardware" or not reference.get("epoch") or
                           tracker.get("stream_epoch") != reference.get("epoch") or
                           any(tracker.get(key) != reference.get(key) for key in ("algorithm_version", "config_version")))
        version = tracker.get("reference_version")
        changed_ready = reference.get("ready") and (tracker.get("reference_pending") or version != reference.get("reference_version"))
        if stale or binding_changed or changed_ready:
            reference.update(ready=False, pending=False, reason="光谱已过期、采集时段或参考已改变，请重新确认无接触并建立测试参考")
            return
        stamp = tracker.get("host_rx_monotonic_ns")
        previous = reference.get("previous_reference_version")
        if reference.get("pending") and fresh_peaks and not tracker.get("reference_pending") and type(version) is int and type(previous) is int and version > previous and type(stamp) is int and stamp >= reference["requested_ns"]:
            reference.update(ready=True, pending=False, reference_version=version,
                             frame_id=tracker.get("frame_id"), committed_rx_ns=stamp,
                             reason="本次明确无载后的独立测试光学参考已就绪")

    def start_job(self, kind, session_ids, options=None):
        if kind not in {"preview", "build", "evaluate", "pilot", "train"}:
            raise ValueError("未知离线分析操作")
        if not isinstance(session_ids, list) or not session_ids:
            raise ValueError("请先选择已结束的实验会话")
        for value in session_ids:
            _id(value)
        if options is not None and not isinstance(options, dict):
            raise ValueError("训练与分析参数必须为对象")
        options = deepcopy(options or {})
        if options.get("allow_synthetic") or options.get("allow_synthetic_diagnostics") or options.get("formal_ready") or options.get("drift_ranges_confirmed"):
            raise ValueError("不能通过界面参数替代真实算法比较、漂移证据或正式训练门")
        if kind in {"pilot", "train"}:
            from .experiment import get_session
            if kind == "pilot" and len(session_ids) != 1:
                raise ValueError("少量点分轮验证请选择一个 A 或 B 批已保存会话")
            if kind == "train" and (len(session_ids) > 100 or len(set(session_ids)) != len(session_ids)):
                raise ValueError("训练请选择 1～100 个不重复的 A 或 B 批已保存会话")
            _training_source_paths(self.userdata, session_ids)
            physical, conditions, first_settings = None, None, None
            for session_id in session_ids:
                manifest = get_session(self.userdata, session_id)
                if manifest.get("settings", {}).get("batch") not in {"A", "B"}:
                    raise ValueError("训练请选择 A 或 B 批；C 批是保留数据，不参与训练")
                if kind == "train":
                    if manifest.get("kind") != "9fbg_calibration_experiment_v1" or manifest.get("complete") is not True:
                        raise ValueError("请选择已完整保存的实验会话进行训练")
                    signature = _physical_definition(manifest["settings"])
                    declared_conditions = _training_conditions(manifest["settings"])
                    if physical is not None and physical != signature:
                        raise _incompatible_sources("不能混合不同触觉件、点位坐标或法向力定义的会话", session_id, manifest["settings"], session_ids[0], first_settings)
                    if conditions is not None and conditions != declared_conditions:
                        raise _incompatible_sources("不能混合不同装夹方式或温度环境条件的会话；留空须与留空一致", session_id, manifest["settings"], session_ids[0], first_settings)
                    if first_settings is None:
                        first_settings = manifest["settings"]
                    physical, conditions = signature, declared_conditions
            if kind == "train":
                if not isinstance(options.get("method", "linear"), str) or options.get("method", "linear") not in _METHOD_LABELS:
                    raise ValueError("训练方法必须为加权最小二乘或小型 MLP")
                options.setdefault("method", "linear")
        with self.lock:
            if self.busy:
                raise ValueError("已有模型处理任务，请等待完成")
            if kind == "evaluate" and self.bundle is None:
                raise ValueError("请先建立或载入模型")
            self.busy, self.error = True, None
            self.job_progress = {"stage": "验证原始来源并补算全部光谱"}
            self.job_id = "J" + uuid.uuid4().hex
            if kind == "train":
                method = options["method"]
                self.training = {"job_id": self.job_id, "busy": True, "status": "running", "progress_percent": 0,
                                 "stage": "开始校验已保存的原始数据", "method": method, "method_label": _METHOD_LABELS[method],
                                 "error": None, "model_id": None, "point_ids": [], "session_ids": deepcopy(session_ids),
                                 "created_utc": None, "created_local_minute": None}
                self.job_progress = deepcopy(self.training)
            response = {"busy": True, "job_id": self.job_id}
            if kind == "train":
                response["training"] = deepcopy(self.training)
            thread = threading.Thread(target=self._work, args=(kind, deepcopy(session_ids), options), name="9FBG-analysis", daemon=True)
            thread.start()
        return response

    def _work(self, kind, session_ids, options):
        pending_model = None
        try:
            from .inversion import fit_models, evaluate_models, preview_rows, validate_bundle
            rows, provenance = load_rows(self.userdata, session_ids,
                                         (lambda value: self._training_progress(value, "source")) if kind == "train" else self._progress)
            if kind == "build" and any(row["split"] == "C" for row in rows):
                raise ValueError("建立模型仅选择 A 训练与 B 验证会话；C 批保留到明确执行独立评估")
            if kind == "evaluate":
                expected = self.bundle.get("acquisition_provenance", {}).get("physical_definition")
                if not expected or provenance["physical_definition"] != expected:
                    raise ValueError("独立评估的触觉件、25 点坐标或法向力定义与当前模型不一致")
            if kind == "train":
                self._training_progress({"stage": "原始光谱补算完成，生成训练特征表", "progress_percent": 45}, "overall")
            else:
                self._progress({"stage": "生成特征表与分析研究模型"})
            output = self.userdata / "analyses" / self.job_id
            output.mkdir(parents=True, exist_ok=False)
            with (output / "aligned_features.jsonl").open("x", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(json.dumps(clean(row), ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
            if kind == "preview":
                report = preview_rows(rows)
            elif kind == "build":
                bundle = fit_models(rows, options)
                validate_bundle(bundle)
                bundle["acquisition_provenance"] = provenance
                physical = provenance["physical_definition"]
                bundle["grid"]["point_grid"] = deepcopy(physical["point_grid"])
                if "coordinate_definition" in physical:
                    coordinates = deepcopy(physical["coordinate_definition"])
                    bundle["grid"]["coordinate_definition"] = coordinates
                    if isinstance(coordinates, dict) and coordinates.get("schema") == "bottom_left_xy_v1":
                        bundle["grid"]["order"] = "P01_bottom_left_row_major_x_right_y_up"
                bundle["created_utc"] = datetime.now(timezone.utc).isoformat()
                bundle.pop("bundle_sha256")
                bundle["bundle_sha256"] = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
                validate_bundle(bundle)
                model_id = "M" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
                models = self.userdata / "models"
                models.mkdir(parents=True, exist_ok=True)
                with (models / (model_id + ".json")).open("x", encoding="utf-8") as stream:
                    json.dump(clean(bundle), stream, ensure_ascii=False, allow_nan=False)
                report = evaluate_models(bundle, rows)
                with self.lock:
                    self.bundle, self.model_id = bundle, model_id
                    self._reset_truth_zero()
            elif kind in {"pilot", "train"}:
                from .pilot import fit_pilot, save_pilot_model
                result = fit_pilot(rows, options, progress=lambda value: self._training_progress(value, "fit")) if kind == "train" else fit_pilot(rows, options)
                report = result["report"]
                model = result["model"]
                model["acquisition_provenance"] = provenance
                model["created_utc"] = datetime.now(timezone.utc).isoformat()
                model.pop("model_sha256")
                model["model_sha256"] = hashlib.sha256(json.dumps(model, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
                pilot_id = "P" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
                models = self.userdata / "pilot_models"
                models.mkdir(parents=True, exist_ok=True)
                model_path = models / (pilot_id + ".json")
                if kind == "train":
                    self._training_progress({"stage": "保存训练模型与来源记录", "progress_percent": 95}, "overall")
                if kind == "train":
                    _validate_live_pilot(model, self.userdata)
                    pending_model = models / ("." + pilot_id + ".json.pending")
                    save_pilot_model(model, pending_model)
                else:
                    save_pilot_model(model, model_path)
                # Pilot results never replace the separate 25-point live model.
                report.update(pilot_model_id=pilot_id, pilot_model_path=str(model_path),
                              model_sha256=model["model_sha256"])
            else:
                report = evaluate_models(self.bundle, rows)
            report.update(kind=kind, provenance=provenance, formal_ready=False,
                          created_utc=datetime.now(timezone.utc).isoformat(),
                          aligned_table=str(output / "aligned_features.jsonl"), model_id=None if kind in {"pilot", "train"} else self.model_id)
            (output / "report.json").write_text(json.dumps(clean(report), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
            if kind == "train":
                pending_model.replace(model_path)
                pending_model = None
            with self.lock:
                if kind == "train":
                    created = _model_time(model["created_utc"])
                    self.training.update(busy=False, status="completed", progress_percent=100, stage="训练模型已保存，请手动载入后进行实机验证",
                                         model_id=pilot_id, point_ids=deepcopy(model["point_ids"]), created_utc=model["created_utc"],
                                         created_local_minute=created.astimezone().strftime("%Y-%m-%d %H:%M"))
                    self.job_progress = deepcopy(self.training)
                else:
                    self.report, self.report_id = report, self.job_id
        except Exception as exc:
            with self.lock:
                self.error = "离线分析失败：" + str(exc)
                if kind == "train":
                    self.error = "训练失败：" + str(exc)
                    self.training.update(busy=False, status="failed", error=self.error, stage="训练失败；没有完成新模型")
                    self.job_progress = deepcopy(self.training)
        finally:
            if pending_model is not None:
                try:
                    pending_model.unlink(missing_ok=True)
                except OSError:
                    pass
            with self.lock:
                self.busy = False

    def _progress(self, value):
        with self.lock:
            self.job_progress = deepcopy(value)

    def _training_progress(self, value, phase):
        details = deepcopy(value)
        with self.lock:
            if self.training.get("status") != "running":
                return
            percent = self.training["progress_percent"]
            if phase == "source":
                if details.get("session_id") != self.training.get("session_id"):
                    for field in ("file", "verified_files", "total_files", "processed", "total", "source_progress_percent"):
                        self.training.pop(field, None)
                if "file" in details:
                    for field in ("processed", "total", "source_progress_percent"):
                        self.training.pop(field, None)
                elif "processed" in details:
                    for field in ("file", "verified_files", "total_files", "source_progress_percent"):
                        self.training.pop(field, None)
                local_percent = details.get("source_progress_percent", 0) * .45
                completed, total = details.get("verified_files"), details.get("total_files")
                if type(completed) is int and type(total) is int and total > 0:
                    local_percent = 10 * max(0, min(1, completed / total))
                else:
                    completed, total = details.get("processed"), details.get("total")
                    if type(completed) is int and type(total) is int and total > 0:
                        local_percent = 10 + 35 * max(0, min(1, completed / total))
                    details.setdefault("stage", "按保存的参考逐帧补算原始光谱")
                index, count = details.get("session_index", 1), details.get("session_count", 1)
                if type(index) is int and type(count) is int and 1 <= index <= count:
                    percent = (45 * (index - 1) + max(0, min(45, local_percent))) / count
            elif phase == "fit":
                completed = details.get("progress_percent")
                if type(completed) in (int, float) and math.isfinite(completed):
                    percent = 45 + .5 * max(0, min(100, completed))
                for field in ("fold", "epoch", "epochs"):
                    if field not in details:
                        self.training.pop(field, None)
                stage = details.get("stage")
                details["stage_code"] = stage
                details["stage"] = {"prepare": "检查训练点与完整轮次", "cv_fit": "训练分轮检查模型",
                                    "cv_evaluate": "检查留出的完整轮次", "final_fit": "训练全部合格轮次模型",
                                    "validate": "校验训练模型参数", "complete": "训练计算完成，等待保存"}.get(stage, "执行模型训练")
            else:
                percent = details.get("progress_percent", percent)
            details.pop("progress_percent", None)
            self.training.update(details)
            self.training["progress_percent"] = max(self.training["progress_percent"], min(99, percent))
            self.job_progress = deepcopy(self.training)

    def full_report(self):
        with self.lock:
            return deepcopy(self.report or {"reason": "尚无分析报告"})

    def snapshot(self, tracker=None, force=None, sync=None, experiment=None, *, source_kind=None):
        from .inversion import predict
        from .pilot import MODEL_SCHEMA as PILOT_SCHEMA, predict_pilot
        tracker, experiment = tracker or {}, experiment or {}
        peaks = tracker.get("peaks", [])
        by_id = {row.get("id"): row for row in peaks}
        fresh_peaks = (source_kind == "hardware" and len(peaks) == 9 and
                       set(by_id) == {f"FBG{i}" for i in range(1, 10)} and
                       not tracker.get("display_stale") and not tracker.get("reference_pending") and
                       all(row.get("valid") and not row.get("stale") and type(row.get("drift_nm")) in (int, float) and math.isfinite(row["drift_nm"]) for row in by_id.values()))
        with self.lock:
            bundle, zero = self.bundle, deepcopy(self.truth_zero)
            if (zero.get("ready") or zero.get("pending")) and (not force or force.get("stale") or force.get("stream_epoch") != zero.get("epoch")):
                zero.update(ready=False, pending=False, offsets=None, reason="PX6D 已断开、过期或时段改变；测试真值零点需重新确认")
                self.truth_zero = deepcopy(zero)
                self._zero_samples, self._zero_seen = [], set()
            self._update_test_reference(tracker, source_kind, fresh_peaks)
            test_reference = deepcopy(self.test_reference)
            base = {"busy": self.busy, "error": self.error, "job_id": self.job_id,
                    "report_id": self.report_id, "model_id": self.model_id, "formal_ready": False,
                    "model_kind": ("pilot" if bundle.get("schema") == PILOT_SCHEMA else "full") if bundle else None,
                    "trained_point_ids": deepcopy(bundle.get("point_ids", [point["id"] for point in bundle.get("grid", {}).get("point_grid", [])])) if bundle else [],
                    "model_sha256": bundle.get("model_sha256", bundle.get("bundle_sha256")) if bundle else None,
                    "job_progress": deepcopy(self.job_progress),
                    "training": deepcopy(self.training),
                    "model_definition": deepcopy((bundle or {}).get("acquisition_provenance", {}).get("physical_definition")),
                    "report": {key: deepcopy(self.report[key]) for key in ("kind", "formal_ready", "quality_summary", "model_id", "pilot_model_id", "summary", "scope") if self.report and key in self.report},
                    "truth_zero": zero, "test_reference": test_reference}
        prediction = {"status": "尚无模型", "point_id": None, "force_N": None, "truth_force_N": None,
                      "error_N": None, "comparison_ready": False,
                      "reason": "请先在训练页训练模型，再选择并载入模型进行实机验证"}
        if bundle:
            baseline = experiment.get("baseline") or {}
            experiment_reference = bool(experiment.get("running") and baseline.get("ready") and
                                        baseline.get("reference_version") == tracker.get("reference_version") and
                                        baseline.get("optical_epoch") and baseline.get("optical_epoch") == tracker.get("stream_epoch") and
                                        all(baseline.get(key) == tracker.get(key) for key in ("algorithm_version", "config_version")))
            reference_ready = experiment_reference if experiment.get("running") else test_reference.get("ready") is True
            valid = fresh_peaks and reference_ready
            if tracker.get("algorithm_version") != bundle.get("algorithm_version") or tracker.get("config_version") != bundle.get("config_version"):
                valid = False
            definition = bundle.get("acquisition_provenance", {}).get("physical_definition")
            context_match = True
            if experiment.get("running") and definition:
                active_definition = _physical_definition(experiment.get("settings", {}))
                if active_definition != definition:
                    valid = False
                    context_match = False
            if valid:
                x_pm = [by_id[f"FBG{i}"]["drift_nm"]*1000 for i in range(1,10)]
                # Neither force truth nor operator-selected point enters inversion.
                predictor = predict_pilot if bundle.get("schema") == PILOT_SCHEMA else predict
                prediction = predictor(bundle, x_pm, {"source_kind": source_kind, "algorithm_version": tracker.get("algorithm_version"), "config_version": tracker.get("config_version")})
                # These are the exact nine features used by this prediction,
                # never a later frame copied during manual judgement.
                prediction["x_pm"] = x_pm
            else:
                prediction = {"status": "无效", "point_id": None, "force_N": None,
                              "reason": "当前会话的触觉件、点位坐标或力定义与模型不一致" if not context_match else
                                        "当前试次基线或独立测试光学参考未就绪，请明确确认无接触后建立参考" if not reference_ready else
                                        "九峰、基线、算法或配置不满足当前模型输入要求"}
            prediction.update(truth_force_N=None, error_N=None, formal_ready=False)
            offsets, epoch, sign = zero.get("offsets"), zero.get("epoch"), zero.get("force_sign", -1)
            if experiment_reference and baseline.get("force_zero"):
                offsets, epoch = baseline["force_zero"], baseline.get("force_epoch")
                sign = experiment.get("settings", {}).get("force_sign", -1)
            if reference_ready and context_match and force and not force.get("stale") and sync and sync.get("valid") and sync.get("spectrum_frame_id") == tracker.get("frame_id") and sync.get("spectrum_stream_epoch", tracker.get("stream_epoch")) == tracker.get("stream_epoch") and sync.get("force_stream_epoch") == epoch and offsets and len(sync.get("force_values") or []) == 6:
                truth = sign * (sync["force_values"][2] - offsets[2])
                prediction["truth_force_N"] = truth
                if prediction.get("status") == "有效估计" and type(prediction.get("force_N")) in (int, float):
                    prediction["error_N"] = prediction["force_N"] - truth
            identity = {"model_checksum": base["model_sha256"], "frame_id": tracker.get("frame_id"),
                        "optical_epoch": tracker.get("stream_epoch"), "force_epoch": epoch,
                        "reference_version": tracker.get("reference_version"),
                        "algorithm_version": tracker.get("algorithm_version"), "config_version": tracker.get("config_version")}
            prediction["prediction_context"] = identity
            if prediction.get("truth_force_N") is not None:
                prediction["truth_context"] = deepcopy(identity)
            prediction["comparison_ready"] = bool(valid and not experiment.get("running")
                                                   and test_reference.get("ready") and zero.get("ready")
                                                   and tracker.get("reference_frozen") is True
                                                   and sync and sync.get("matched") is True
                                                   and sync.get("spectrum_stream_epoch") == tracker.get("stream_epoch")
                                                   and type(prediction.get("truth_force_N")) in (int, float)
                                                   and math.isfinite(prediction["truth_force_N"])
                                                   and prediction["truth_force_N"] > 0.05
                                                   and prediction.get("status") in {"有效估计", "定位不确定", "超范围", "无接触"})
        return clean({**base, "prediction": prediction})
