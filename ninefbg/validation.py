"""Independent, append-only records of explicit live manual validation.

No device, prediction, training or experiment operation is performed here.
The server supplies one atomic prediction/truth comparison and its identities.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

SCHEMA = "manual_validation_v1"
FORCE_TOLERANCE_N = 0.3
_CONTEXT_KEYS = ("frame_id", "optical_epoch", "force_epoch", "reference_version",
                 "algorithm_version", "config_version", "model_checksum")
_POINTS = {f"P{i:02d}" for i in range(1, 26)}
_AXES = ["Fx", "Fy", "Fz", "Mx", "My", "Mz"]
_UNITS = ["N", "N", "N", "N·m", "N·m", "N·m"]
_REJECTION_STATES = {"定位不确定", "超范围", "无接触"}


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _checksum(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("人工验收需要有效的模型 SHA256 校验值")
    return value.lower()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _vector(value, size, label):
    if not isinstance(value, list) or len(value) != size or not all(_finite(item) for item in value):
        raise ValueError(label + f"必须为 {size} 个有限数值")
    return value


def _frame_id(value):
    return type(value) in (int, str) and value != "" and (type(value) is not int or value >= 0)


def _same(actual, expected):
    return type(actual) is type(expected) and actual == expected


def _target_point(value):
    if value is not None and (not isinstance(value, str) or value not in _POINTS):
        raise ValueError("目标点位必须为 P01～P25 或留空")
    return value


def _ready(evidence, name):
    if name + "_ready" in evidence:
        return evidence[name + "_ready"] is True
    nested = evidence.get(name)
    return isinstance(nested, dict) and nested.get("ready") is True


def _inference_valid(prediction):
    return (isinstance(prediction, dict) and prediction.get("status") == "有效估计"
            and isinstance(prediction.get("point_id"), str) and prediction["point_id"] in _POINTS
            and all(_finite(prediction.get(key)) for key in ("force_N", "error_N")))


def _display_prediction(prediction):
    displayed = deepcopy(prediction)
    if not _inference_valid(prediction):
        # Retain all raw model output in evidence, but never present a rejected
        # candidate (including the no-contact sentinel 0 N) as an estimate.
        displayed.update(point_id=None, force_N=None, error_N=None,
                         comparison_ready=True, inference_valid=False)
    return displayed


def _validated(evidence, *, check_clock=True):
    if not isinstance(evidence, dict):
        raise ValueError("人工验收缺少服务端冻结证据")
    value = deepcopy(evidence)
    _target_point(value.get("target_point_id"))
    if value.get("valid") is not True or value.get("stale") is not False or value.get("source_kind") != "hardware":
        raise ValueError("只能冻结当前有效的实机预测与 PX6D 对照")
    if not _ready(value, "test_reference") or not _ready(value, "truth_zero") or value.get("experiment_running") is not False or value.get("reference_frozen") is not True:
        raise ValueError("独立测试光学参考与六轴零点必须就绪并冻结，且实验采集不能运行")
    for name, maximum in (("age_ms", 1000), ("force_age_ms", 250)):
        if not _finite(value.get(name)) or not 0 <= value[name] <= maximum:
            raise ValueError("当前光谱或 PX6D 数据已过期，不能冻结验收")
    stamp = value.get("host_rx_monotonic_ns")
    if type(stamp) is not int or stamp <= 0 or not _frame_id(value.get("frame_id")):
        raise ValueError("人工验收缺少实际源帧标识或采集时间")
    if check_clock and not 0 <= (time.perf_counter_ns() - stamp) / 1e6 <= 1000:
        raise ValueError("当前光谱采集时间已过期或来自不同时间基准")
    value["model_checksum"] = _checksum(value.get("model_checksum", value.get("model_sha256")))
    for key in ("optical_epoch", "force_epoch", "algorithm_version", "config_version"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError("人工验收缺少采集时段、算法或配置身份")
    if type(value.get("reference_version")) is not int or value["reference_version"] <= 0:
        raise ValueError("人工验收缺少有效的光学参考版本")
    identity = {key: value[key] for key in _CONTEXT_KEYS}
    for name in ("prediction_context", "truth_context"):
        context = value.get(name)
        if not isinstance(context, dict) or any(not _same(context.get(key), expected) for key, expected in identity.items()):
            raise ValueError("预测与 PX6D 对照不是同一帧、参考、模型或采集时段")
    _vector(value.get("x_pm"), 9, "九峰输入")
    feature_ids = [f"FBG{i}" for i in range(1, 10)]
    if value.get("feature_ids", feature_ids) != feature_ids:
        raise ValueError("人工验收九峰输入身份必须固定为 FBG1～FBG9")
    value.setdefault("feature_ids", feature_ids)
    prediction = value.get("prediction")
    if not isinstance(prediction, dict) or not _finite(prediction.get("truth_force_N")):
        raise ValueError("当前点位或力预测没有有效的 PX6D 对照")
    inference_valid = _inference_valid(prediction)
    if prediction.get("comparison_ready", inference_valid) is not True:
        raise ValueError("当前帧没有有效的实机比较证据")
    if not inference_valid:
        if prediction.get("status") not in _REJECTION_STATES:
            raise ValueError("当前预测或模型状态不能作为人工验收样本")
        if any(prediction.get(key) is not None and not _finite(prediction[key]) for key in ("force_N", "error_N")):
            raise ValueError("模型拒识值必须留空或为有限的原始候选值")
        if prediction.get("point_id") is not None and (not isinstance(prediction["point_id"], str) or prediction["point_id"] not in _POINTS):
            raise ValueError("模型拒识候选点位无效")
    sync = value.get("sync")
    if not isinstance(sync, dict) or sync.get("valid") is not True or sync.get("matched") is not True or not _same(sync.get("spectrum_frame_id"), value["frame_id"]) or sync.get("spectrum_stream_epoch") != value["optical_epoch"] or sync.get("force_stream_epoch") != value["force_epoch"]:
        raise ValueError("人工验收需要同一光谱帧与六轴采集时段的有效同步配对")
    aligned = _vector(sync.get("force_values"), 6, "配对六轴值")
    ids = sync.get("force_frame_ids")
    method = sync.get("method")
    required = 1 if method == "exact_receive_time" else 2 if method == "linear_receive_time" else 0
    if not isinstance(ids, list) or len(ids) != required or not required or not all(_frame_id(item) for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("同步配对缺少可追溯的原始六轴帧，最近值不能作验收对照")
    if method == "linear_receive_time" and (not _finite(sync.get("bracket_ms")) or not 0 < sync["bracket_ms"] <= 40):
        raise ValueError("六轴插值帧间隔超过有效同步范围")
    zero = _vector(value.get("force_zero"), 6, "六轴无载零点")
    sign = value.get("force_sign")
    if type(sign) not in (int, float) or sign not in (-1, 1):
        raise ValueError("人工验收缺少已确认的法向力方向")
    truth = sign * (aligned[2] - zero[2])
    if not _finite(truth) or not math.isclose(prediction["truth_force_N"], truth, rel_tol=0, abs_tol=1e-9):
        raise ValueError("PX6D 对照或力误差与冻结的配对六轴值不一致")
    if truth <= 0.05:
        raise ValueError("PX6D 对照尚未超过 0.05 N 按压门槛，无载不能作为一次按压验收")
    if inference_valid and not math.isclose(prediction["error_N"], prediction["force_N"] - truth, rel_tol=0, abs_tol=1e-9):
        raise ValueError("PX6D 对照或力误差与冻结的配对六轴值不一致")
    sources = value.get("force_source_frames")
    if sources is not None:
        if not isinstance(sources, list) or len(sources) != len(ids):
            raise ValueError("原始六轴帧数量与配对身份不一致")
        by_id = {}
        for frame in sources:
            if not isinstance(frame, dict) or not _frame_id(frame.get("frame_id")) or frame.get("stream_epoch") != value["force_epoch"] or frame.get("source_kind") != "hardware":
                raise ValueError("原始六轴帧跨越采集时段或来源无效")
            if frame.get("valid") is False or frame.get("stale") is True or frame.get("time_quality", "host_rx_approx") not in {"host_rx_approx", "hardware_mapped"}:
                raise ValueError("原始六轴帧已过期或缺少可信采集时间")
            if frame.get("axes", _AXES) != _AXES or frame.get("units", _UNITS) != _UNITS:
                raise ValueError("原始六轴帧轴身份或单位不一致")
            _vector(frame.get("force_values"), 6, "原始六轴值")
            if type(frame.get("host_rx_monotonic_ns")) is not int or frame["host_rx_monotonic_ns"] <= 0:
                raise ValueError("原始六轴帧缺少采集时间")
            if check_clock and not 0 <= (time.perf_counter_ns()-frame["host_rx_monotonic_ns"])/1e6 <= 250:
                raise ValueError("配对引用的原始六轴帧已过期，不能冻结验收")
            by_id[frame["frame_id"]] = frame
        if set(by_id) != set(ids):
            raise ValueError("原始六轴帧与同步配对引用不一致")
        frames = [by_id[item] for item in ids]
        if required == 1:
            if frames[0]["host_rx_monotonic_ns"] != stamp:
                raise ValueError("精确同步引用的六轴帧采集时间不一致")
            expected = frames[0]["force_values"]
        else:
            left, right = [frame["host_rx_monotonic_ns"] for frame in frames]
            if not left < stamp < right or right - left > 40_000_000 or max(stamp-left, right-stamp) > 25_000_000:
                raise ValueError("原始六轴帧不满足当前同步端点与间隔范围")
            fraction = (stamp-left)/(right-left)
            expected = [a+(b-a)*fraction for a, b in zip(frames[0]["force_values"], frames[1]["force_values"])]
        if any(not math.isclose(a, b, rel_tol=0, abs_tol=1e-9) for a, b in zip(aligned, expected)):
            raise ValueError("配对六轴值与原始帧重算结果不一致")
    _canonical(value)  # Reject non-finite or non-JSON extras rather than cleaning them.
    return value


def _judgment_values(sample, position_correct, force_correct, actual_point_id):
    if type(position_correct) is not bool or type(force_correct) is not bool:
        raise ValueError("位置与力判断必须由操作者明确选择真或假")
    if actual_point_id is not None and (not isinstance(actual_point_id, str) or actual_point_id not in _POINTS):
        raise ValueError("实际点位必须为 P01～P25 或留空")
    target = sample.get("target_point_id")
    if target is not None:
        if actual_point_id is not None and actual_point_id != target:
            raise ValueError("目标点位已随证据固定，不能更改为另一实际点位")
        actual_point_id = target
    if not sample.get("inference_valid", _inference_valid(sample["prediction"])) and (position_correct or force_correct):
        raise ValueError("模型拒识没有有效位置或力预测，只能保存位置错误且力错误")
    if position_correct and actual_point_id is not None and actual_point_id != sample["prediction"]["point_id"]:
        raise ValueError("实际点位与冻结预测不同，不能标记位置正确")
    if force_correct and sample["absolute_error_N"] > FORCE_TOLERANCE_N and not math.isclose(sample["absolute_error_N"], FORCE_TOLERANCE_N, rel_tol=0, abs_tol=1e-12):
        raise ValueError("冻结力误差超过 0.3 N，不能标记力正确")
    return {"position_correct": position_correct, "force_correct": force_correct,
            "joint_correct": position_correct and force_correct, "actual_point_id": actual_point_id}


class ValidationController:
    """One pending frozen comparison per model; counts use saved judgments only."""

    def __init__(self, userdata):
        self.root = Path(userdata) / "validation_sessions"
        self.lock = threading.RLock()
        self.sessions, self.samples, self.judgments = {}, {}, {}
        self.discarded, self.identities = set(), {}
        self.reports = {}
        self.model_checksum = self.model_id = self.validation_session_id = None
        self.error = None
        self._invalid = {}
        self._load()

    def _load(self):
        if not self.root.is_dir():
            return
        try:
            for path in sorted(self.root.glob("*/records.jsonl")):
                if path.parent.is_symlink() or path.is_symlink():
                    raise ValueError("人工验收记录不能指向符号链接")
                previous = None
                with path.open("rb") as stream:
                    for line in stream:
                        if not line.endswith(b"\n"):
                            raise ValueError("人工验收日志存在未完整写入的尾行")
                        event = json.loads(line)
                        actual = event.get("record_sha256")
                        body = {key: value for key, value in event.items() if key != "record_sha256"}
                        if event.get("schema") != SCHEMA or event.get("validation_session_id") != path.parent.name or event.get("previous_sha256") != previous or actual != _digest(body):
                            raise ValueError("人工验收日志哈希或会话身份校验失败")
                        self._apply(event, replay=True)
                        previous = actual
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.error = "人工验收存储读取失败，已停止写入：" + str(exc)

    def configure(self, model_checksum=None, model_id=None, *, model_sha256=None, new_session=False):
        checksum = _checksum(model_checksum if model_checksum is not None else model_sha256)
        if model_id is not None and (not isinstance(model_id, str) or not model_id):
            raise ValueError("模型标识必须为非空文本")
        with self.lock:
            previous_checksum, previous_sid = self.model_checksum, self.validation_session_id
            self.model_checksum, self.model_id = checksum, model_id
            known = [sid for sid, session in self.sessions.items() if session["model_checksum"] == checksum]
            if new_session or not known:
                sid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:12]
                self.sessions[sid] = {"model_checksum": checksum, "model_id": model_id, "created_utc": _utc(), "head": None}
            elif previous_checksum == checksum and previous_sid in known:
                sid = previous_sid
            else:
                sid = max(known, key=lambda item: (self.sessions[item]["created_utc"], item))
            self.validation_session_id = sid
            return self.snapshot()

    def _require_writable(self):
        if self.error:
            raise ValueError(self.error)
        if self.model_checksum is None or self.validation_session_id is None:
            raise ValueError("请先加载模型，再进行人工实机验收")

    def _identity(self, evidence):
        return _canonical({key: evidence[key] for key in _CONTEXT_KEYS}).decode("utf-8")

    def _pending(self, checksum, session_id=None):
        return [sample for sample_id, sample in self.samples.items()
                if sample["model_checksum"] == checksum and (session_id is None or sample["validation_session_id"] == session_id)
                and sample_id not in self.judgments and sample_id not in self.discarded]

    def capture(self, evidence):
        with self.lock:
            self._require_writable()
            try:
                value = _validated(evidence)
                if value["model_checksum"] != self.model_checksum:
                    raise ValueError("冻结证据与当前加载模型不一致")
                if value.get("model_id") is not None and self.model_id is not None and value["model_id"] != self.model_id:
                    raise ValueError("冻结证据与当前模型标识不一致")
                identity = self._identity(value)
                if identity in self.identities:
                    sample = deepcopy(self.samples[self.identities[identity]])
                    if value.get("target_point_id") != sample.get("target_point_id"):
                        raise ValueError("此源帧的目标点位已随证据固定，不能更改")
                    sample.update(judged=sample["sample_id"] in self.judgments, discarded=sample["sample_id"] in self.discarded)
                    return sample
                if self._pending(self.model_checksum):
                    raise ValueError("请先保存或明确放弃当前冻结判定，再冻结下一次")
                if self.validation_session_id in self.reports:
                    self.configure(self.model_checksum, self.model_id, new_session=True)
                created = _utc()
                inference_valid = _inference_valid(value["prediction"])
                prediction = _display_prediction(value["prediction"])
                sample = {"sample_id": "V" + uuid.uuid4().hex, "valid": True, "reason": None,
                          "comparison_ready": True, "inference_valid": inference_valid,
                          "model_id": self.model_id, "model_checksum": self.model_checksum, "model_sha256": self.model_checksum,
                          "validation_session_id": self.validation_session_id, "session_id": self.validation_session_id,
                          "created_utc": created, "reference_version": value["reference_version"],
                          "target_point_id": value.get("target_point_id"), "actual_point_id": value.get("target_point_id"),
                          "prediction": prediction, "absolute_error_N": abs(prediction["error_N"]) if inference_valid else None,
                          "force_tolerance_N": FORCE_TOLERANCE_N, "evidence_sha256": _digest(value),
                          "raw_force_available": bool(value.get("force_source_frames")),
                          "evidence": value}
                self._append("freeze", {"sample": sample})
                return deepcopy(sample)
            except (ValueError, TypeError) as exc:
                self._invalid[self.model_checksum] = self._invalid.get(self.model_checksum, 0) + 1
                raise ValueError(str(exc)) from exc

    def _sample_for_current_model(self, sample_id):
        if not isinstance(sample_id, str) or sample_id not in self.samples:
            raise ValueError("冻结样本不存在，请先冻结当前有效比较")
        sample = self.samples[sample_id]
        if sample["model_checksum"] != self.model_checksum:
            raise ValueError("请切回冻结样本所对应的模型后再操作")
        return sample

    def judge(self, sample_id, position_correct, force_correct, actual_point_id=None):
        with self.lock:
            self._require_writable()
            sample = self._sample_for_current_model(sample_id)
            judgment = _judgment_values(sample, position_correct, force_correct, actual_point_id)
            if sample_id in self.discarded:
                raise ValueError("此冻结样本已明确放弃，不能保存判断")
            if sample_id in self.judgments:
                old = self.judgments[sample_id]
                if any(old.get(key) != value for key, value in judgment.items()):
                    raise ValueError("此样本已保存判断，不能改判或重复计数")
                return self.snapshot()
            self._append("judge", {"sample_id": sample_id, "judgment": {**judgment, "judged_utc": _utc(), "source": "operator"},
                                   "evidence_sha256": sample["evidence_sha256"]}, session_id=sample["validation_session_id"])
            return self.snapshot()

    record = judge

    def discard(self, sample_id):
        with self.lock:
            self._require_writable()
            sample = self._sample_for_current_model(sample_id)
            if sample_id in self.judgments:
                raise ValueError("已保存的人工判断不能放弃或从分母移除")
            if sample_id not in self.discarded:
                self._append("discard", {"sample_id": sample_id}, session_id=sample["validation_session_id"])
            return self.snapshot()

    def _report_records(self, checksum, sid):
        records = []
        for sample_id, judgment in self.judgments.items():
            sample = self.samples[sample_id]
            if sample["model_checksum"] != checksum or sample["validation_session_id"] != sid:
                continue
            prediction = sample["prediction"]
            records.append({"sample_id": sample_id, "created_utc": sample["created_utc"],
                            "actual_point_id": judgment["actual_point_id"],
                            "point_id": prediction.get("point_id"), "force_N": prediction.get("force_N"),
                            "truth_force_N": prediction.get("truth_force_N"), "error_N": prediction.get("error_N"),
                            "inference_valid": sample["inference_valid"],
                            "position_correct": judgment["position_correct"],
                            "force_correct": judgment["force_correct"], "source": "operator",
                            "evidence_sha256": sample["evidence_sha256"]})
        return records

    def _report_view(self, report):
        if report is None:
            return None
        return {**deepcopy(report), "path": str(self.root / report["session_id"] / "report.json")}

    def _write_report(self, report):
        path = self.root / report["session_id"] / "report.json"
        content = _canonical(report) + b"\n"
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError("已保存验证报告与结束记录不一致；保留原文件，请检查")
            return
        temporary = path.with_name(".report-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def finish(self):
        """Seal only explicitly saved judgments from this validation round."""
        with self.lock:
            self._require_writable()
            sid = self.validation_session_id
            if sid in self.reports:
                report = self.reports[sid]
                self._write_report(report)
                return {"validation": self.snapshot(), "report": self._report_view(report)}
            if self._pending(self.model_checksum):
                raise ValueError("请先保存本次判断，或明确重新判断，再结束验证")
            stats = self._result_stats(self.model_checksum, sid)
            if not stats["judged_total"]:
                raise ValueError("尚无已保存的实机判断，请实际按压并保存后再结束验证")
            report = {"schema": "9fbg_manual_validation_report_v1", "report_id": "R" + uuid.uuid4().hex,
                      "created_utc": _utc(), "session_id": sid, "model_id": self.sessions[sid]["model_id"],
                      "model_sha256": self.model_checksum, "force_tolerance_N": FORCE_TOLERANCE_N,
                      "stats": stats, "records": self._report_records(self.model_checksum, sid),
                      "source_head_sha256": self.sessions[sid]["head"],
                      "scope": "operator_saved_live_press_judgments_only", "formal_ready": False}
            report["report_sha256"] = _digest(report)
            self._append("finish", {"report": report})
            self._write_report(report)
            return {"validation": self.snapshot(), "report": self._report_view(report)}

    def _append(self, kind, payload, *, session_id=None):
        sid = session_id or self.validation_session_id
        session = self.sessions[sid]
        event = {"schema": SCHEMA, "event": kind, "event_id": uuid.uuid4().hex,
                 "validation_session_id": sid, "model_checksum": session["model_checksum"],
                 "model_id": session["model_id"], "created_utc": _utc(),
                 "previous_sha256": session["head"], **deepcopy(payload)}
        event["record_sha256"] = _digest(event)
        line = _canonical(event) + b"\n"
        directory = self.root / sid
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "records.jsonl").open("ab") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            self.error = "人工验收记录保存失败，已停止写入：" + str(exc)
            raise ValueError(self.error) from exc
        self._apply(event)

    def _apply(self, event, *, replay=False):
        sid, checksum = event["validation_session_id"], _checksum(event["model_checksum"])
        session = self.sessions.setdefault(sid, {"model_checksum": checksum, "model_id": event.get("model_id"), "created_utc": event["created_utc"], "head": None})
        if session["model_checksum"] != checksum:
            raise ValueError("同一人工验收会话不能混入不同模型")
        kind = event["event"]
        if sid in self.reports:
            raise ValueError("已结束的实机验证会话不能追加或更改判断")
        if kind == "freeze":
            sample = deepcopy(event["sample"])
            if replay:
                _validated(sample["evidence"], check_clock=False)
            if sample["model_checksum"] != checksum or sample["validation_session_id"] != sid or sample["evidence_sha256"] != _digest(sample["evidence"]):
                raise ValueError("冻结样本与保存证据身份不一致")
            inference_valid = _inference_valid(sample["evidence"]["prediction"])
            absolute_error = abs(sample["evidence"]["prediction"]["error_N"]) if inference_valid else None
            target = sample["evidence"].get("target_point_id")
            if sample.get("target_point_id") != target or sample.get("actual_point_id") != target:
                raise ValueError("冻结目标点位与保存证据不一致")
            if sample.get("valid") is not True or sample["prediction"] != _display_prediction(sample["evidence"]["prediction"]) or sample["reference_version"] != sample["evidence"]["reference_version"] or sample["absolute_error_N"] != absolute_error or sample.get("force_tolerance_N") != FORCE_TOLERANCE_N or sample.get("inference_valid", inference_valid) is not inference_valid or sample.get("comparison_ready", True) is not True:
                raise ValueError("冻结显示值与保存证据不一致")
            # Enrich only in memory when reading earlier valid freeze records.
            sample.setdefault("inference_valid", inference_valid)
            sample.setdefault("comparison_ready", True)
            sample.setdefault("target_point_id", None)
            sample.setdefault("actual_point_id", None)
            identity = self._identity(sample["evidence"])
            if sample["sample_id"] in self.samples or identity in self.identities or self._pending(checksum):
                raise ValueError("人工验收日志包含重复冻结或未处理的样本")
            self.samples[sample["sample_id"]] = sample
            self.identities[identity] = sample["sample_id"]
        elif kind in {"judge", "discard"}:
            sample_id = event["sample_id"]
            sample = self.samples[sample_id]
            if sample["model_checksum"] != checksum or sample["validation_session_id"] != sid or sample_id in self.judgments or sample_id in self.discarded:
                raise ValueError("人工验收日志的判断重复或跨会话")
            if kind == "judge":
                if event.get("evidence_sha256") != sample["evidence_sha256"]:
                    raise ValueError("人工判断引用的冻结证据哈希不一致")
                judgment = event["judgment"]
                expected = _judgment_values(sample, judgment["position_correct"], judgment["force_correct"], judgment["actual_point_id"])
                if any(judgment.get(key) != value for key, value in expected.items()) or judgment.get("source") != "operator":
                    raise ValueError("保存记录没有一致的人工判断来源")
                self.judgments[sample_id] = deepcopy(event["judgment"])
            else:
                self.discarded.add(sample_id)
        elif kind == "finish":
            report = deepcopy(event["report"])
            expected_digest = _digest({key: value for key, value in report.items() if key != "report_sha256"})
            if (report.get("schema") != "9fbg_manual_validation_report_v1"
                    or report.get("report_sha256") != expected_digest
                    or report.get("session_id") != sid or report.get("model_sha256") != checksum
                    or report.get("model_id") != session["model_id"]
                    or report.get("source_head_sha256") != session["head"]
                    or report.get("stats") != self._result_stats(checksum, sid)
                    or not report["stats"]["judged_total"] or self._pending(checksum)
                    or report.get("force_tolerance_N") != FORCE_TOLERANCE_N
                    or report.get("scope") != "operator_saved_live_press_judgments_only"
                    or report.get("formal_ready") is not False
                    or report.get("records") != self._report_records(checksum, sid)):
                raise ValueError("实机验证结果与已保存的人工判断不一致")
            self.reports[sid] = report
        else:
            raise ValueError("人工验收日志包含未知事件")
        session["head"] = event["record_sha256"]

    def _result_stats(self, checksum, session_id):
        stats = self._stats(checksum, session_id)
        # Failed requests are transient diagnostics, not saved operator judgments.
        stats.pop("invalid_count", None)
        return stats

    def _stats(self, checksum, session_id=None):
        ids = [sample_id for sample_id in self.judgments if self.samples[sample_id]["model_checksum"] == checksum
               and (session_id is None or self.samples[sample_id]["validation_session_id"] == session_id)]
        total = len(ids)
        position = sum(self.judgments[item]["position_correct"] for item in ids)
        force = sum(self.judgments[item]["force_correct"] for item in ids)
        joint = sum(self.judgments[item]["joint_correct"] for item in ids)
        errors = [self.samples[item]["prediction"]["error_N"] for item in ids
                  if self.samples[item]["inference_valid"] and _finite(self.samples[item]["prediction"].get("force_N"))
                  and _finite(self.samples[item]["prediction"].get("error_N"))]
        error_count = len(errors)
        rejected = sum(not self.samples[item]["inference_valid"] for item in ids)
        scale = max((abs(error) for error in errors), default=0)
        mae = scale * (math.fsum(abs(error)/scale for error in errors)/error_count) if scale else 0.0 if error_count else None
        rmse = scale * math.sqrt(math.fsum((error/scale)**2 for error in errors)/error_count) if scale else 0.0 if error_count else None
        return {"judged_total": total, "position_correct": position, "force_correct": force, "joint_correct": joint,
                "inference_rejected": rejected, "force_error_count": error_count,
                "position_rate": position / total if total else None, "force_rate": force / total if total else None,
                "joint_rate": joint / total if total else None, "mae_N": mae, "rmse_N": rmse, "MAE_N": mae, "RMSE_N": rmse,
                "pending_count": len(self._pending(checksum, session_id)), "invalid_count": self._invalid.get(checksum, 0),
                "status": "已记录人工实机验收" if total else "尚未实机验证"}

    def snapshot(self, model_id=None, model_sha256=None, session_id=None, *, model_checksum=None):
        with self.lock:
            checksum = model_checksum if model_checksum is not None else model_sha256
            if model_id is not None and checksum is None:
                candidates = {item["model_checksum"] for item in self.sessions.values() if item["model_id"] == model_id}
                if len(candidates) == 1:
                    checksum = candidates.pop()
                elif len(candidates) > 1:
                    raise ValueError("同名模型存在不同校验值，请明确选择模型 SHA256")
            elif checksum is None:
                checksum = self.model_checksum
            if checksum is not None:
                checksum = _checksum(checksum)
            if session_id is not None and (session_id not in self.sessions or self.sessions[session_id]["model_checksum"] != checksum):
                raise ValueError("人工验收会话与所选模型不一致")
            sid = session_id or (self.validation_session_id if checksum == self.model_checksum else None)
            pending = self._pending(checksum, session_id)
            sample = deepcopy(pending[-1]) if pending else None
            saved = [sample_id for sample_id in self.judgments if self.samples[sample_id]["model_checksum"] == checksum
                     and (session_id is None or self.samples[sample_id]["validation_session_id"] == session_id)]
            reports = [report for report in self.reports.values() if report["model_sha256"] == checksum]
            latest_report = max(reports, key=lambda row: (row["created_utc"], row["session_id"])) if reports else None
            return {"schema": SCHEMA, "model_id": model_id if model_id is not None else self.model_id if checksum == self.model_checksum else sample.get("model_id") if sample else None,
                    "model_checksum": checksum, "model_sha256": checksum, "validation_session_id": sid, "session_id": sid,
                    "pending_sample": sample, "stats": self._stats(checksum, session_id),
                    "session_stats": self._stats(checksum, sid) if sid else None,
                    "finished": sid in self.reports, "latest_report": self._report_view(latest_report),
                    "last_saved_sample_id": saved[-1] if saved else None, "error": self.error,
                    "force_tolerance_N": FORCE_TOLERANCE_N, "formal_ready": False,
                    "stats_scope": "validation_session" if session_id is not None else "model_checksum_all_sessions"}
