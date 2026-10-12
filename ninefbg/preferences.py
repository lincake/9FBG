"""Durable operator settings, separate from live measurement authorization."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import threading
import uuid

SCHEMA = "9fbg_settings_v1"
FIELDS = {
    "connection": {"source_mode", "integration_us", "sample_rate_hz"},
    "display": {"spectrum_mode", "spectrum_x_min", "spectrum_x_max", "spectrum_y_min", "spectrum_y_max", "show_windows"},
    "experiment": {"finger_id", "data_id", "batch", "force_sign", "tip_diameter_mm", "temperature_C", "fixation", "repetitions_per_point", "selected_point_id"},
    "analysis": {"method", "include_mlp"},
}
ENUMS = {"source_mode": {"hardware", "replay"}, "spectrum_mode": {"auto", "manual"},
         "batch": {"A", "B", "C"}, "method": {"linear", "piecewise", "mlp"}}


def _validate(section, values):
    if section not in FIELDS or not isinstance(values, dict) or set(values) - FIELDS[section]:
        raise ValueError("设置包含不支持的分类或字段；测量状态与确认不能持久化")
    checked = deepcopy(values)
    for key, value in checked.items():
        if key in ENUMS:
            if not isinstance(value, str) or value not in ENUMS[key]:
                raise ValueError("设置选项无效：" + key)
        elif key in {"finger_id", "data_id", "fixation"}:
            limit = {"finger_id": 256, "data_id": 128, "fixation": 2000}[key]
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError("设置文本长度或格式无效：" + key)
        elif key == "selected_point_id":
            if value not in {f"P{i:02d}" for i in range(1, 26)}:
                raise ValueError("上次点位必须为 P01～P25")
        elif key in {"include_mlp", "show_windows"}:
            if type(value) is not bool:
                raise ValueError("显示或对照选项必须为布尔值")
        elif key in {"integration_us", "sample_rate_hz", "repetitions_per_point"}:
            limits = {"integration_us": (20, 10_000_000), "sample_rate_hz": (1, 200), "repetitions_per_point": (3, 999)}
            low, high = limits[key]
            if type(value) is not int or not low <= value <= high or key == "sample_rate_hz" and 1000 % value:
                raise ValueError("设置整数超出允许范围：" + key)
        elif key == "force_sign":
            if type(value) not in (int, float) or value not in {-1, 1}:
                raise ValueError("按压力方向必须为 -1 或 +1")
        else:
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                                      or key == "tip_diameter_mm" and value <= 0):
                raise ValueError("设置数值无效：" + key)
    return checked


class Preferences:
    def __init__(self, userdata):
        self.path = Path(userdata) / "settings.json"
        self.lock = threading.RLock()
        self.values, self.error = {}, None
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if data.get("schema") != SCHEMA or not isinstance(data.get("values"), dict):
                    raise ValueError("设置文件版本或格式无效")
                self.values = {section: _validate(section, values) for section, values in data["values"].items()}
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                self.error = "上次设置读取失败：" + str(exc)

    def snapshot(self):
        with self.lock:
            return {"schema": SCHEMA, "values": deepcopy(self.values), "error": self.error}

    def update(self, section, values):
        checked = _validate(section, values)
        with self.lock:
            if self.error:
                raise ValueError(self.error + "；请检查原设置文件，当前保留默认设置")
            merged = deepcopy(self.values)
            merged.setdefault(section, {}).update(checked)
            document = {"schema": SCHEMA, "values": merged}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(".settings-" + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self.values = merged
            return self.snapshot()
