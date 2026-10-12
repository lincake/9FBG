"""Append-only raw evidence writer; storage is independent of fitting/display."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import threading
import uuid

from . import PRODUCT, VERSION


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "tolist"):
        return clean(value.tolist())
    if isinstance(value, float):
        import math
        return value if math.isfinite(value) else None
    return value


class SessionWriter:
    def __init__(self, root: Path, metadata: dict, capacity=2048):
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:10]
        self.path = root / name
        self.path.mkdir(parents=True, exist_ok=False)
        self.metadata = {"product": PRODUCT, "version": VERSION, "session_id": name,
                         "started_utc": datetime.now(timezone.utc).isoformat(), **metadata}
        self.queue = queue.Queue(maxsize=capacity)
        self.error = None
        self.counts = {}
        self.last_saved_frame_ids = {}
        self.accepted = 0
        self.overflow = 0
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="9FBG-writer", daemon=True)
        self.thread.start()

    def submit(self, kind: str, value: dict):
        if self.error or self.closed:
            raise RuntimeError(self.error or "会话已关闭，不能写入")
        if kind not in {"raw_spectra", "raw_force", "results", "events"}:
            raise ValueError("未知记录类型")
        # Serialize before ownership transfers, so later UI/state mutation cannot alter evidence.
        line = json.dumps(clean(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        try:
            self.queue.put_nowait((kind, line, value.get("frame_id")))
            self.accepted += 1
        except queue.Full:
            self.overflow += 1
            self.error = "原始数据写盘队列已满；采集已要求停止，数据不完整"
            raise RuntimeError(self.error)

    def _run(self):
        handles = {}
        try:
            while True:
                item = self.queue.get()
                try:
                    if item is None:
                        break
                    kind, line, frame_id = item
                    if kind not in handles:
                        handles[kind] = (self.path / (kind + ".jsonl")).open("x", encoding="utf-8", newline="\n")
                    handles[kind].write(line)
                    handles[kind].flush()
                    self.counts[kind] = self.counts.get(kind, 0) + 1
                    if frame_id is not None:
                        self.last_saved_frame_ids[kind] = frame_id
                finally:
                    self.queue.task_done()
        except Exception as exc:
            self.error = "写盘失败：" + str(exc)
        finally:
            for handle in handles.values():
                try:
                    handle.flush()
                    import os
                    os.fsync(handle.fileno())
                    handle.close()
                except OSError as exc:
                    self.error = "数据落盘失败：" + str(exc)

    def close(self, final: dict):
        if self.closed:
            return
        self.closed = True
        while self.thread.is_alive():
            try:
                self.queue.put(None, timeout=.2)
                break
            except queue.Full:
                continue
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            self.error = "写盘线程尚未停止，不能宣布保存完成"
        manifest = {**self.metadata, "ended_utc": datetime.now(timezone.utc).isoformat(),
                    "counts": self.counts, "accepted_records": self.accepted, "overflow": self.overflow,
                    "complete": not self.error and not final.get("error") and not self.thread.is_alive() and self.queue.empty(),
                    "last_saved_frame_ids": self.last_saved_frame_ids,
                    "error": self.error, "final": clean(final), "files": {}}
        if not self.thread.is_alive():
            for file in self.path.glob("*.jsonl"):
                if not file.is_file():
                    continue
                with file.open("rb") as stream:
                    manifest["files"][file.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        (self.path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    def status(self):
        return {"path": str(self.path), "counts": dict(self.counts), "queue_depth": self.queue.qsize(),
                "error": self.error, "overflow": self.overflow, "closed": self.closed,
                "last_saved_frame_ids": dict(self.last_saved_frame_ids)}
