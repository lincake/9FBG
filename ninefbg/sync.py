"""Align acquisition receive times; fitting completion is never a time source."""
from bisect import bisect_left
from collections import deque
import math


class ForceSynchronizer:
    def __init__(self, max_gap_ms=40.0, max_age_ms=25.0, capacity=2000):
        self.max_gap_ns = int(max_gap_ms * 1e6)
        self.max_age_ns = int(max_age_ms * 1e6)
        self.frames = deque(maxlen=capacity)
        self.epoch = None
        self.resets = 0
        self.rejected = 0
        self.duplicates = 0
        self._seen = set()
        self._last_spec_epoch = None

    def clear(self):
        self.frames.clear()
        self._seen.clear()

    def add(self, frame):
        timestamp = frame.get("host_rx_monotonic_ns")
        values = frame.get("force_values")
        epoch = frame.get("stream_epoch")
        if type(timestamp) is not int or timestamp <= 0 or not epoch or not isinstance(values, (list, tuple)) or len(values) != 6 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in values):
            self.rejected += 1
            return False
        if frame.get("time_quality") not in {"host_rx_approx", "hardware_mapped"}:
            self.rejected += 1
            return False
        if self.epoch != epoch:
            if self.epoch is not None:
                self.resets += 1
            self.clear()
            self.epoch = epoch
        key = (epoch, frame.get("frame_id"))
        if key in self._seen or self.frames and timestamp <= self.frames[-1]["host_rx_monotonic_ns"]:
            self.duplicates += 1
            return False
        if len(self.frames) == self.frames.maxlen:
            evicted = self.frames[0]
            self._seen.discard((epoch, evicted.get("frame_id")))
        self._seen.add(key)
        self.frames.append(dict(frame))
        return True

    def align(self, spectrum):
        timestamp = spectrum.get("host_rx_monotonic_ns")
        spec_epoch = spectrum.get("stream_epoch")
        base = {"spectrum_frame_id": spectrum.get("frame_id"), "spectrum_stream_epoch": spec_epoch, "force_stream_epoch": self.epoch, "timestamp_source": "host_receive_monotonic", "time_quality": "host_rx_approx", "matched": False, "valid": False, "force_values": None, "force_frame_ids": [], "reason": None}
        if type(timestamp) is not int or timestamp <= 0 or not spec_epoch or spectrum.get("time_quality") not in {"host_rx_approx", "hardware_mapped"}:
            return {**base, "reason": "光谱缺少可信采集接收时间或流标识"}
        if not self.frames:
            return {**base, "reason": "没有 PX6D 采集帧"}
        # A new optical stream cannot match force samples retained before its
        # first received frame, even though both clocks share the host origin.
        if self._last_spec_epoch != spec_epoch:
            self._last_spec_epoch = spec_epoch
            self._spec_epoch_start_ns = timestamp
        eligible = [f for f in self.frames if f["host_rx_monotonic_ns"] >= self._spec_epoch_start_ns]
        if not eligible:
            return {**base, "reason": "光谱重连后等待同一采集时段的 PX6D 帧"}
        times = [f["host_rx_monotonic_ns"] for f in eligible]
        position = bisect_left(times, timestamp)
        if position < len(times) and times[position] == timestamp:
            selected = eligible[position]
            return {**base, "matched": True, "valid": True, "method": "exact_receive_time", "force_values": list(selected["force_values"]), "force_frame_ids": [selected["frame_id"]], "delta_ms": 0.0}
        if 0 < position < len(times):
            left, right = eligible[position-1], eligible[position]
            gap = times[position]-times[position-1]
            if left["stream_epoch"] != right["stream_epoch"] or gap > self.max_gap_ns:
                return {**base, "reason": "PX6D 相邻帧间隔过大或跨重连时段"}
            if max(timestamp-times[position-1], times[position]-timestamp) > self.max_age_ns:
                return {**base, "reason": "PX6D 插值两端距离光谱采集时间过远"}
            fraction = (timestamp-times[position-1])/gap
            values = [a+(b-a)*fraction for a, b in zip(left["force_values"], right["force_values"])]
            return {**base, "matched": True, "valid": True, "method": "linear_receive_time", "force_values": values, "force_frame_ids": [left["frame_id"], right["frame_id"]], "bracket_ms": gap/1e6, "delta_ms": 0.0}
        nearest = eligible[min(position, len(eligible)-1)]
        delta = nearest["host_rx_monotonic_ns"]-timestamp
        if abs(delta) > self.max_age_ns:
            return {**base, "reason": "PX6D 最近帧超过时间容差", "delta_ms": delta/1e6}
        return {**base, "method": "nearest_diagnostic_only", "nearest_force_values": list(nearest["force_values"]), "nearest_force_frame_id": nearest["frame_id"], "delta_ms": delta/1e6, "reason": "等待 PX6D 左右采集帧，最近值仅供诊断"}

    def diagnostics(self):
        return {"buffered_force_frames": len(self.frames), "force_stream_epoch": self.epoch, "stream_resets": self.resets, "invalid_force_frames": self.rejected, "duplicate_transport_frames": self.duplicates, "max_gap_ms": self.max_gap_ns/1e6, "max_age_ms": self.max_age_ns/1e6, "time_quality": "host_rx_approx", "note": "主机接收时间近似同步；设备无公共硬件时钟"}
