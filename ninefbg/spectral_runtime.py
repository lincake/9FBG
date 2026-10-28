"""Causal source-frame inference. HTTP snapshots never advance this time axis."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import hashlib
import json
import math
import statistics
import threading
import time
import uuid

AXES = ["Fx", "Fy", "Fz", "Mx", "My", "Mz"]
UNITS = ["N", "N", "N", "N·m", "N·m", "N·m"]


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _vector(value, size):
    return isinstance(value, (list, tuple)) and len(value) == size and all(
        type(x) in (int, float) and math.isfinite(x) for x in value)


class SpectralRuntime:
    def __init__(self, *, predictor=None):
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.worker = None
        self.model = self.metadata = None
        self.active = False
        self.reference = None
        self.reference_request = None
        self.reference_samples = []
        self.trial_binding = None
        self.trial_preload = []
        self.frames = deque(maxlen=16)
        self.seen = set()
        self.seen_order = deque(maxlen=4096)
        self.pending = None
        self.generation = 0
        self.contact = False
        self.completed_window = None
        self._predictor = predictor
        self.metrics = {"submitted": 0, "completed": 0, "skipped_requests": 0,
                        "duplicate_identities": 0, "resets": 0, "latency_ms": None}
        self.result = self._invalid("尚无全谱模型")

    def _invalid(self, reason):
        return {"schema": "spectral_prediction_v1", "model_kind": "spectral_gru",
                "input_kind": (self.metadata or {}).get("input_kind", "delta_S"),
                "valid": False, "status": "无效", "point_id": None, "contact": False,
                "contact_probability": None, "contact_method": None,
                "wrench_pred": None, "wrench_truth": None, "wrench_error": None,
                "force_norm_N": None, "axes": AXES, "units": UNITS,
                "comparison_ready": False, "formal_ready": False, "reason": reason}

    def _reset(self, reason, *, reference=False):
        self.generation += 1
        self.frames.clear()
        self.pending = None
        self.completed_window = None
        self.contact = False
        self.metrics["resets"] += 1
        self.result = self._invalid(reason)
        if reference:
            self.reference = None
            self.reference_request = None
            self.reference_samples = []
            self.trial_binding = None
            self.trial_preload = []

    def is_drift(self):
        return (self.metadata or {}).get("input_kind") == "delta_lambda_9"

    def invalidate(self, reason, *, reference=True):
        with self.lock:
            if not self.active and self.model is not None:
                return
            self._reset(reason, reference=reference)

    def apply(self, metadata, model):
        # Caller fully validates a candidate before changing the live instance.
        with self.lock:
            self.metadata, self.model = deepcopy(metadata), model
            self.active = True
            self.seen.clear()
            self.seen_order.clear()
            self._reset("模型已改变，请明确确认无接触并建立" + ("九路漂移参考" if self.is_drift() else "全谱参考"), reference=True)
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name="9FBG-spectral-inference", daemon=True)
                self.worker.start()

    def unload(self):
        with self.lock:
            self.active = False
            self.model = self.metadata = None
            self._reset("当前模型使用旧九峰路径", reference=True)

    def stop_inference(self):
        with self.lock:
            self.active = False
            self._reset("推理已结束，请重新载入模型并明确确认无接触后建立新参考", reference=True)
            self.result["status"] = "已结束"
            self.seen.clear()
            self.seen_order.clear()
            self.wake.clear()

    def request_reference(self, confirmed, epoch, *, requested_ns=None):
        if confirmed is not True or not epoch:
            raise ValueError("请明确确认当前无接触，并连接真实光谱设备")
        with self.lock:
            if not self.active:
                raise ValueError("推理已结束，请重新载入模型，再确认无接触并建立新参考")
            self._reset("正在收集 2 秒真实无载九峰参考" if self.is_drift() else "正在收集真实无载 512 点全谱参考", reference=True)
            self.reference_request = {"epoch": epoch, "requested_ns": requested_ns or time.perf_counter_ns(),
                                      "trigger": "operator_unloaded_confirmation"}

    def set_reference(self, reference):
        """Apply a versioned acquisition reference once; never alter old windows."""
        if self.is_drift():
            from .drift_features import validate_drift_reference
            validate_drift_reference(reference)
            with self.lock:
                if not self.active or self.reference and self.reference.get("reference_id") == reference["reference_id"]:
                    return
                self._reset("九路漂移参考已改变，等待 16 个连续源帧")
                self.reference, self.reference_request = deepcopy(reference), None
                self.reference_samples = []
            return
        if not isinstance(reference, dict) or not _vector(reference.get("S0"), 512) or not _vector(reference.get("wavelength_nm"), 512):
            raise ValueError("全谱无载参考必须含真实 512 点 S0 与波长轴")
        ref = deepcopy(reference)
        if not ref.get("reference_id") or not ref.get("stream_epoch") or type(ref.get("effective_ns")) is not int:
            raise ValueError("全谱参考缺少版本、采集时段或生效时间")
        with self.lock:
            if not self.active:
                return
            if self.reference and self.reference.get("reference_id") == ref["reference_id"]:
                return
            self._reset("全谱参考已改变，等待 16 个连续源帧")
            self.reference, self.reference_request = ref, None
            self.reference_samples = []

    def _collect_reference(self, frame):
        request = self.reference_request
        if not request:
            return
        stamp = frame["host_rx_monotonic_ns"]
        if frame["stream_epoch"] != request["epoch"]:
            self._reset("光谱已重连，请重新明确确认无接触", reference=True)
            return
        if stamp < request["requested_ns"]:
            return
        samples = self.reference_samples
        if samples and (frame["wavelength_nm"] != samples[0]["wavelength_nm"] or
                        frame.get("preprocessing", {}) != samples[0].get("preprocessing", {})):
            self._reset("无载收集期间光谱轴或采样配置改变", reference=True)
            return
        samples.append(deepcopy(frame))
        if len(samples) >= 12 and stamp - samples[0]["host_rx_monotonic_ns"] >= 400_000_000:
            self.reference = {"schema": "spectral_reference_v1", "reference_id": "S0_" + uuid.uuid4().hex,
                "S0": [statistics.median(f["intensity"][i] for f in samples) for i in range(512)],
                "wavelength_nm": deepcopy(frame["wavelength_nm"]),
                "axis_sha256": signature(frame["wavelength_nm"]),
                "preprocessing": deepcopy(frame.get("preprocessing", {})),
                "stream_epoch": frame["stream_epoch"], "effective_ns": stamp,
                "requested_ns": request["requested_ns"],
                "source_frame_ids": [f["frame_id"] for f in samples],
                "source_timestamps_ns": [f["host_rx_monotonic_ns"] for f in samples],
                "trigger": request["trigger"], "unloaded_confirmed": True}
            self.reference_request = None
            self.reference_samples = []
            self._reset("全谱参考已就绪，等待 16 个连续源帧")

    def submit(self, frame):
        """Only copies raw values/identities; network evaluation occurs outside locks."""
        with self.lock:
            if self.model is None or not self.active:
                return
            # Drift uses the existing fitted source result, never a second fit
            # or the frozen demonstration chart's cached values.
            if self.is_drift():
                return
            epoch, identity, stamp = frame.get("stream_epoch"), frame.get("frame_id"), frame.get("host_rx_monotonic_ns")
            if (not epoch or type(identity) not in (str, int) or type(stamp) is not int or
                    frame.get("source_kind") != "hardware" or frame.get("time_quality") not in {"host_rx_approx", "hardware_mapped"} or
                    frame.get("valid") is False or not _vector(frame.get("intensity"), 512) or not _vector(frame.get("wavelength_nm"), 512)):
                self._reset("原始光谱身份、时间或 512 点内容无效")
                return
            key = (epoch, identity)
            if key in self.seen:
                self.metrics["duplicate_identities"] += 1
                return
            if len(self.seen_order) == self.seen_order.maxlen:
                self.seen.discard(self.seen_order.popleft())
            self.seen.add(key)
            self.seen_order.append(key)
            self._collect_reference(frame)
            ref, meta = self.reference, self.metadata
            if ref is None:
                return
            if epoch != ref["stream_epoch"] or frame["wavelength_nm"] != ref["wavelength_nm"] or frame.get("preprocessing", {}) != ref.get("preprocessing", {}):
                self._reset("光谱时段、波长轴或曝光/预处理改变，请重建全谱参考", reference=True)
                return
            if frame["wavelength_nm"] != meta["wavelength_nm"] or frame.get("preprocessing", {}) != meta.get("preprocessing", {}):
                self._reset("当前波长轴或曝光/预处理与模型不兼容")
                return
            if stamp < ref["effective_ns"]:
                return
            if self.frames and (stamp <= self.frames[-1]["host_rx_monotonic_ns"] or
                    stamp - self.frames[-1]["host_rx_monotonic_ns"] > int(meta.get("window_rule", {}).get("max_gap_ms", 120) * 1e6)):
                self._reset("源帧时间倒退或断流，重新预热")
            self.frames.append({key: deepcopy(frame[key]) for key in
                                ("intensity", "frame_id", "host_rx_monotonic_ns", "stream_epoch")})
            self.metrics["submitted"] += 1
            if len(self.frames) < 16:
                self.result = self._invalid(f"预热中：{len(self.frames)}/16 个连续源帧")
                return
            if self.pending is not None:
                self.metrics["skipped_requests"] += 1
            self.pending = (self.generation, list(self.frames), ref, self.model, meta)
            self.wake.set()

    def submit_result(self, frame, annotation=None):
        """Append a real fitted nine-center source frame to its causal window."""
        from .drift_features import validate_drift_frame, make_drift_reference, drift_pm
        with self.lock:
            if not self.active or self.model is None or not self.is_drift():
                return
            try:
                validate_drift_frame(frame)
                if not frame["all_valid"] or frame.get("source_valid") is not True:
                    raise ValueError("九路峰心失效或含旧值，重新预热")
            except (ValueError, TypeError, KeyError) as exc:
                self._reset(str(exc))
                if self.reference_request:
                    self.reference_samples = []
                self.trial_preload = []
                return
            stamp, epoch, identity = frame["host_rx_monotonic_ns"], frame["stream_epoch"], frame["frame_id"]
            key = (epoch, identity)
            if key in self.seen:
                self.metrics["duplicate_identities"] += 1
                return
            if len(self.seen_order) == self.seen_order.maxlen:
                self.seen.discard(self.seen_order.popleft())
            self.seen.add(key)
            self.seen_order.append(key)
            meta = self.metadata
            definition = meta.get("peak_definition", {})
            if (frame["axis_sha256"] != signature(meta["wavelength_nm"]) or
                    frame["preprocessing"] != meta.get("preprocessing", {}) or
                    any(definition.get(name) is not None and definition[name] != frame.get(name)
                        for name in ("algorithm_version", "config_version"))):
                self._reset("九路峰心算法、真实波长轴或采样配置与模型不兼容", reference=True)
                return
            request = self.reference_request
            if request:
                if epoch != request["epoch"]:
                    self._reset("光谱已重连，请重新明确确认无接触", reference=True)
                    return
                if stamp < request["requested_ns"]:
                    return
                samples = self.reference_samples
                if samples and (not self._drift_binding_equal(frame, samples[0]) or
                                not 0 < stamp-samples[-1]["host_rx_monotonic_ns"] <= 120_000_000):
                    samples.clear()
                    self._reset("无载九峰时间间断或配置改变，重新收集 2 秒参考")
                samples.append(deepcopy(frame))
                if len(samples) >= 12 and stamp-samples[0]["host_rx_monotonic_ns"] >= 2_000_000_000:
                    self.reference = make_drift_reference(samples, effective_ns=stamp,
                        reference_id="L0_"+uuid.uuid4().hex, requested_ns=request["requested_ns"],
                        wavelength_nm=meta["wavelength_nm"])
                    seed = self._drift_window_tail(samples, meta)
                    self.reference_request, self.reference_samples = None, []
                    self._reset("九路漂移参考已就绪，等待连续源帧")
                    self.frames.extend(seed[:-1])
                else:
                    return
            elif annotation and annotation.get("trial_id"):
                binding = (annotation.get("session_id"), annotation["trial_id"])
                if binding != self.trial_binding:
                    self._reset("新轮次九路漂移参考正在收集", reference=True)
                    self.trial_binding = binding
                if annotation.get("phase") == "preload":
                    samples = self.trial_preload
                    if samples and (not self._drift_binding_equal(frame, samples[0]) or
                                    not 0 < stamp-samples[-1]["host_rx_monotonic_ns"] <= 120_000_000):
                        samples.clear()
                    samples.append(deepcopy(frame))
                    return
                if self.reference is None:
                    samples = self.trial_preload
                    if len(samples) < 12:
                        self.result = self._invalid("本轮 2 秒无载段有效九峰不足，不能建立漂移参考")
                        return
                    boundary = annotation.get("press_prompt_monotonic_ns") or samples[-1]["host_rx_monotonic_ns"]
                    self.reference = make_drift_reference(samples, effective_ns=boundary,
                        reference_id="L0_"+str(binding[0])+"_"+str(binding[1]),
                        trigger="trial_preload_unloaded", wavelength_nm=meta["wavelength_nm"],
                        session_id=binding[0], trial_id=binding[1])
                    self._reset("本轮九路漂移参考已固定")
                    self.frames.extend(self._drift_window_tail(samples, meta))
                    self.trial_preload = []
            ref = self.reference
            if ref is None:
                return
            if not self._drift_binding_equal(frame, ref):
                self._reset("九路峰心时段或配置改变，请重建无载参考", reference=True)
                return
            max_gap_ns = int(meta.get("window_rule", {}).get("max_gap_ms", 120)*1e6)
            if self.frames and not 0 < stamp-self.frames[-1]["host_rx_monotonic_ns"] <= max_gap_ns:
                self._reset("九路源帧时间倒退或断流，重新预热")
            # Keep evidence as actual centers, with fixed-reference drift values.
            value = deepcopy(frame)
            value["delta_lambda_pm"] = drift_pm(frame, ref)
            self.frames.append(value)
            self.metrics["submitted"] += 1
            if len(self.frames) < 16:
                self.result = self._invalid(f"预热中：{len(self.frames)}/16 个连续九路源帧")
                return
            if self.pending is not None:
                self.metrics["skipped_requests"] += 1
            self.pending = (self.generation, list(self.frames), ref, self.model, meta)
            self.wake.set()

    @staticmethod
    def _drift_binding_equal(left, right):
        return all(left.get(key) == right.get(key) for key in
                   ("stream_epoch", "algorithm_version", "config_version", "axis_sha256", "preprocessing"))

    @staticmethod
    def _drift_window_tail(samples, metadata):
        tail = []
        maximum = int(metadata.get("window_rule", {}).get("max_gap_ms", 120)*1e6)
        for frame in samples[-16:]:
            if tail and not 0 < frame["host_rx_monotonic_ns"]-tail[-1]["host_rx_monotonic_ns"] <= maximum:
                tail.clear()
            tail.append(frame)
        return tail

    def _evaluate(self, frames, reference, model, metadata):
        if self._predictor:
            return self._predictor(frames, reference, model, metadata)
        import numpy as np
        import torch
        if metadata.get("input_kind") == "delta_lambda_9":
            delta = (np.asarray([f["centers_nm"] for f in frames], dtype=np.float64)
                     - np.asarray(reference["lambda0_nm"], dtype=np.float64)) * 1000
            delta = delta.astype(np.float32)
        else:
            delta = np.asarray([f["intensity"] for f in frames], dtype=np.float32) - np.asarray(reference["S0"], dtype=np.float32)
        norm = metadata["input_normalization"]
        delta = (delta - np.asarray(norm["mean"], dtype=np.float32)) / np.asarray(norm["scale"], dtype=np.float32)
        model.eval()
        with torch.inference_mode():
            outputs = model(torch.from_numpy(delta).unsqueeze(0))
            logits, values = outputs[:2]
            scores = logits[0].cpu().numpy()
            out = metadata["output_normalization"]
            wrench = values[0].cpu().numpy() * np.asarray(out["scale"], dtype=np.float32) + np.asarray(out["mean"], dtype=np.float32)
            from .spectral_model import has_contact_head
            probability = float(torch.sigmoid(outputs[2][0])) if has_contact_head(metadata) else None
        return (scores.tolist(), wrench.tolist(), probability) if probability is not None else (scores.tolist(), wrench.tolist())

    def _run(self):
        while not self.stop.is_set():
            self.wake.wait(.1)
            with self.lock:
                request, self.pending = self.pending, None
                self.wake.clear()
                if not self.active or request is not None and request[0] != self.generation:
                    request = None
            if request is None:
                continue
            generation, frames, reference, model, metadata = request
            started = time.perf_counter_ns()
            try:
                from .spectral_model import has_contact_head
                learned_contact = has_contact_head(metadata)
                output = self._evaluate(frames, reference, model, metadata)
                if len(output) != (3 if learned_contact else 2):
                    raise ValueError("模型输出与声明的接触头版本不一致")
                scores, wrench = output[:2]
                probability = output[2] if learned_contact else None
                if learned_contact and (type(probability) not in (int, float) or
                                        not math.isfinite(probability) or not 0 <= probability <= 1):
                    raise ValueError("接触概率不是 0～1 之间的有限值")
                if not _vector(scores, 25) or not _vector(wrench, 6):
                    raise ValueError("模型输出不是有限的 25 类与六维值")
                norm = math.sqrt(sum(x*x for x in wrench[:3]))
                trained = metadata["trained_point_ids"]
                raw_point = f"P{max(range(25), key=lambda i: scores[i]) + 1:02d}"
                last = frames[-1]
                with self.lock:
                    if generation != self.generation:
                        continue
                    if learned_contact:
                        definition = metadata["contact_definition"]
                        self.contact = probability >= definition["off_probability" if self.contact else "on_probability"]
                    else:
                        thresholds = metadata.get("contact_hysteresis", {"on_N": .1, "off_N": .05})
                        self.contact = norm >= (thresholds.get("off_N", .05) if self.contact else thresholds.get("on_N", .1))
                    point = raw_point if self.contact and raw_point in trained else None
                    context = {"frame_id": last["frame_id"], "optical_epoch": last["stream_epoch"],
                               "reference_id": reference["reference_id"], "model_checksum": metadata["model_sha256"]}
                    self.result = {**self._invalid(""), "valid": True,
                        "status": "有效估计" if point else "无接触" if not self.contact else "定位不确定",
                        "point_id": point, "raw_point_id": raw_point, "contact": self.contact,
                        "contact_probability": probability,
                        "contact_method": "binary_contact_head_v1" if learned_contact else "legacy_predicted_force_hysteresis_v1",
                        "wrench_pred": wrench, "force_norm_N": norm,
                        "trained_point_ids": deepcopy(trained), "model_id": metadata.get("model_id"),
                        "frame_id": last["frame_id"], "stream_epoch": last["stream_epoch"],
                        "host_rx_monotonic_ns": last["host_rx_monotonic_ns"],
                        "reference_id": reference["reference_id"], "model_sha256": metadata["model_sha256"],
                        "window_frame_ids": [f["frame_id"] for f in frames],
                        "window_timestamps_ns": [f["host_rx_monotonic_ns"] for f in frames],
                        "window_rule": deepcopy(metadata.get("window_rule", {"max_gap_ms":120})),
                        "prediction_context": context, "completed_ns": time.perf_counter_ns(),
                        "reason": "" if point else ("独立接触头判定无接触" if learned_contact else "预测合力低于接触门槛") if not self.contact else "候选点位未训练"}
                    self.metrics["completed"] += 1
                    self.completed_window = deepcopy(frames)
                    self.metrics["latency_ms"] = (time.perf_counter_ns() - started) / 1e6
            except Exception as exc:
                with self.lock:
                    if generation == self.generation:
                        self.result = self._invalid("全谱推理失败：" + str(exc))

    def snapshot(self):
        with self.lock:
            value = deepcopy(self.result)
            stamp = value.get("host_rx_monotonic_ns")
            age = (time.perf_counter_ns() - stamp) / 1e6 if type(stamp) is int else None
            if age is not None and (age < 0 or age > 1000):
                value.update(valid=False, status="无效", point_id=None, comparison_ready=False,
                             reason="推理末帧已过期，等待新的完整窗口")
            value["age_ms"] = age
            return {"inference_active": self.active, "generation": self.generation,
                    "prediction": value, "reference": deepcopy(self.reference),
                    "reference_pending": self.reference_request is not None,
                    "reference_count": len(self.reference_samples), "metrics": deepcopy(self.metrics)}

    def evidence_for(self, frame_id, epoch, reference_id):
        with self.lock:
            if (self.result.get("frame_id") != frame_id or self.result.get("stream_epoch") != epoch or
                    self.result.get("reference_id") != reference_id or self.completed_window is None):
                raise ValueError("全谱推理窗口已更新，请重新冻结当前帧")
            return {"window": deepcopy(self.completed_window), "spectral_reference": deepcopy(self.reference)}

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.worker is not None:
            self.worker.join(5)
        with self.lock:
            self.active = False
            self._reset("全谱推理已关闭", reference=True)
