"""Bounded, read-only delivery of existing fixed-identity nine-peak results.

No acquisition, fitting, reference updates or persistence happens here. Each
sample uses one actual source frame; stale peak display values become nulls.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
import math
import threading
import time

FBG_IDS = [f"FBG{i}" for i in range(1, 10)]
MAX_SAMPLES = 4096
MAX_CURSOR = 2**53 - 1


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _same_value(left, right):
    return type(left) is type(right) and left == right


def validate_cursor(after, limit=MAX_SAMPLES):
    if type(after) is not int or not 0 <= after <= MAX_CURSOR:
        raise ValueError("脉搏数据游标必须为非负安全整数")
    if type(limit) is not int or not 1 <= limit <= MAX_SAMPLES:
        raise ValueError("脉搏数据条数必须为 1～4096 的整数")


def binding_fields(frame, result):
    reference = result.get("reference_nm")
    reference = (list(reference) if isinstance(reference, (list, tuple))
                 and len(reference) == 9 and all(_finite(value) for value in reference) else [])
    return {"stream_epoch": deepcopy(frame.get("stream_epoch")),
            "source_kind": frame.get("source_kind"),
            "reference_version": result.get("reference_version"),
            "config_version": result.get("config_version"),
            "algorithm_version": result.get("algorithm_version"),
            "reference_nm": reference}


def binding_key(fields):
    return json.dumps(fields, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":"))


def initialization_reason(*, running, stopping, stopped, mode, recording,
                          experiment_running, inference_active, busy,
                          press_active, pending, initialized):
    """One explicit first-initialization policy shared by UI and command."""
    if stopping or not running or stopped:
        return "请先手动连接真实光谱设备，并等待采集正常运行"
    if mode != "hardware":
        return "脉搏无载初始化只接受当前真实设备光谱"
    if recording or experiment_running or inference_active or busy:
        return "记录、实验采集、模型推理或分析任务正在运行，不能初始化脉搏参考"
    if press_active:
        return "当前正在按压，请先结束按压并移开手指"
    if pending:
        return "无载参考正在建立，请等待完成，不能重复申请"
    if initialized:
        return "九峰参考已建立；请直接采集本页无接触噪声，不要修改现有参考"
    return ""


class PulseBuffer:
    def __init__(self, capacity=MAX_SAMPLES):
        if type(capacity) is not int or not 1 <= capacity <= MAX_SAMPLES:
            raise ValueError("脉搏缓冲容量必须为 1～4096 的整数")
        self.lock = threading.Lock()
        self.samples = deque(maxlen=capacity)
        self.sequence = 0

    def append(self, frame, result):
        """Copy only small provenance and nine numeric channels, never spectra."""
        fields = binding_fields(frame, result)
        frame_id, stamp = frame.get("frame_id"), frame.get("host_rx_monotonic_ns")
        source_ok = (frame.get("source_kind") == "hardware" and frame.get("valid") is not False
                     and frame.get("stale") is not True and bool(frame.get("stream_epoch"))
                     and type(frame_id) in (str, int) and frame_id != ""
                     and type(stamp) is int and 0 < stamp <= time.perf_counter_ns()
                     and frame.get("time_quality") in {"host_rx_approx", "hardware_mapped"})
        matched = all(_same_value(result.get(key), frame.get(key)) for key in
                      ("frame_id", "stream_epoch", "host_rx_monotonic_ns"))
        rows = result.get("peaks") if isinstance(result.get("peaks"), list) else []
        ids = [row.get("id") for row in rows if isinstance(row, dict)]
        identities_ok = len(ids) == 9 and all(ids.count(fbg) == 1 for fbg in FBG_IDS)
        by_id = {row.get("id"): row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
        reference_ok = (len(fields["reference_nm"]) == 9
                        and type(fields["reference_version"]) is int and fields["reference_version"] > 0
                        and all(isinstance(fields[key], str) and bool(fields[key])
                                for key in ("config_version", "algorithm_version")))
        ready = (source_ok and matched and identities_ok and reference_ok
                 and result.get("valid") is not False and result.get("stale") is not True
                 and result.get("reference_pending") is not True and result.get("state") == "tracking")
        values, valid = [], []
        for index, fbg in enumerate(FBG_IDS):
            row = by_id.get(fbg, {})
            center = row.get("center_nm")
            usable = bool(ready and row.get("valid") is True and row.get("stale") is not True
                          and _same_value(row.get("value_frame_id"), frame_id) and _finite(center))
            value = (float(center) - fields["reference_nm"][index]) * 1000 if usable else None
            usable = usable and _finite(value)
            values.append(value if usable else None)
            valid.append(usable)
        if not source_ok:
            reason = "原始光谱源无效、陈旧或缺少真实设备身份与接收时间"
        elif not matched:
            reason = "九峰结果与原始光谱源帧不一致"
        elif not identities_ok:
            reason = "尚未建立 FBG1～FBG9 的固定光栅身份"
        elif result.get("valid") is False or result.get("stale") is True:
            reason = "九峰处理结果被标记为无效或陈旧"
        elif not reference_ok or result.get("reference_pending") is True or result.get("state") != "tracking":
            reason = "等待已确认无载的固定参考与有效九峰跟踪"
        elif not all(valid):
            reason = "当前源帧有光栅失峰或旧值；失效通道已置空"
        else:
            reason = ""
        sample = {"t_ns": stamp if type(stamp) is int else None, "frame_id": deepcopy(frame_id),
                  **fields, "binding": binding_key(fields), "fbg_ids": list(FBG_IDS),
                  "values_pm": values, "valid_channels": valid, "valid": all(valid), "reason": reason}
        with self.lock:
            self.sequence += 1
            sample["seq"] = self.sequence
            self.samples.append(sample)

    def read(self, after=0, limit=MAX_SAMPLES):
        """Read a bounded page; callers must discard history on cursor reset."""
        validate_cursor(after, limit)
        with self.lock:
            latest = self.sequence
            oldest = self.samples[0]["seq"] if self.samples else 0
            reset = after > latest
            rows = [] if reset else [sample for sample in self.samples if sample["seq"] > after][:limit]
            newest = self.samples[-1] if self.samples else None
        # Appended samples are never mutated. Copy a potentially full history
        # page after releasing the fitting thread's tiny append lock.
        return {"cursor": rows[-1]["seq"] if rows else latest,
                "oldest_seq": oldest, "truncated": reset or bool(after and oldest and after < oldest - 1),
                "reset_required": reset, "samples": deepcopy(rows), "latest": deepcopy(newest)}
