"""Historical HTTP regression adapter. Application startup uses native PyQt6.

Handler/LocalServer remain for protected data/API regressions; no production
entry point starts this adapter or serves the retired browser interface.
"""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import threading
import urllib.request
from urllib.parse import parse_qs, urlsplit
import webbrowser

from . import VERSION
from .service import AcquisitionService, optical_request_settings
from .storage import clean


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, service, **kwargs):
        self.service = service
        super().__init__(*args, directory=str(service.root / "frontend"), **kwargs)

    def send_head(self):
        # Every UI reload must read the current source, including requests made
        # from an older cached page. File timestamps alone cannot identify a
        # delivered UI version, so do not let a conditional request reuse it.
        if "If-Modified-Since" in self.headers:
            del self.headers["If-Modified-Since"]
        self._static_ui_response = True
        try:
            return super().send_head()
        finally:
            self._static_ui_response = False

    def end_headers(self):
        if getattr(self, "_static_ui_response", False):
            self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def _json(self, data, code=200):
        body = json.dumps(clean(data), ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            # A browser timeout/navigation has already closed this socket.
            # Sending another JSON error cannot reach it and only adds a second
            # misleading traceback (notably WinError 10053 on Windows).
            self.close_connection = True

    def _local_request(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{host}" for host in hosts}

    def do_GET(self):
        if not self._local_request():
            return self._json({"error": "只接受本机界面请求"}, 403)
        if self.path == "/api/state":
            return self._json(self.service.snapshot())
        if urlsplit(self.path).path == "/api/pulse/samples":
            try:
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if any(key not in {"after", "limit"} for key in query) or any(len(values) != 1 for values in query.values()):
                    raise ValueError("脉搏数据请求参数无效")
                after, limit = query.get("after", ["0"])[0], query.get("limit", ["4096"])[0]
                if not after.isascii() or not after.isdecimal() or not limit.isascii() or not limit.isdecimal():
                    raise ValueError("脉搏数据游标与条数必须为非负整数")
                return self._json(self.service.pulse_samples(int(after), int(limit)))
            except (ValueError, OverflowError) as exc:
                return self._json({"error": str(exc)}, 400)
        if self.path == "/api/settings":
            return self._json(self.service.preferences.snapshot())
        if self.path == "/api/experiment/sessions":
            return self._json({"sessions": self.service.experiment.list_sessions()})
        if self.path == "/api/history/sessions":
            try:
                return self._json(self.service.history_snapshot())
            except (ValueError, OSError) as exc:
                return self._json({"error": str(exc)}, 400)
        if self.path == "/api/analysis/models":
            try:
                return self._json(self.service.models_snapshot())
            except (ValueError, OSError) as exc:
                return self._json({"error": str(exc)}, 400)
        if self.path == "/api/analysis/report":
            return self._json(self.service.analysis.full_report())
        if self.path.startswith("/api/"):
            return self._json({"error": "接口不存在"}, 404)
        return super().do_GET()

    def do_POST(self):
        if not self._local_request():
            return self._json({"error": "只接受本机界面请求"}, 403)
        try:
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ValueError("请求必须使用 JSON")
            size = int(self.headers.get("Content-Length", "0"))
            empty_stop = self.path == "/api/analysis/stop" and size == 0
            if not empty_stop and not 0 < size <= 64 * 1024 * 1024:
                raise ValueError("请求大小须在 1 字节到 64 MiB 之间")
            data = {} if empty_stop else json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("请求必须是 JSON 对象")
            if self.path == "/api/settings":
                result = self.service.preferences.update(data.get("section"), data.get("values"))
            elif self.path == "/api/connect":
                options = {key: data[key] for key in ("record", "connect_force", "integration_us", "sample_rate_hz") if key in data}
                for key in ("record", "connect_force"):
                    if key in options and type(options[key]) is not bool:
                        raise ValueError("记录与六轴连接选项必须为布尔值")
                for key in ("integration_us", "sample_rate_hz"):
                    if key in options and type(options[key]) is not int:
                        raise ValueError("曝光时间与采样请求频率必须为整数")
                optical_request_settings(options.get("integration_us"), options.get("sample_rate_hz"))
                result = self.service.connect(data.get("mode", "hardware"), data.get("replay_frames"), **options)
            elif self.path == "/api/force/connect":
                result = self.service.connect_force()
            elif self.path == "/api/force/disconnect":
                result = self.service.disconnect_force()
            elif self.path == "/api/disconnect":
                result = self.service.disconnect()
            elif self.path == "/api/shutdown":
                result = self.service.disconnect()
                if result.get("stopping"):
                    raise ValueError("设备或写盘尚未完成关闭，不能退出服务")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif self.path == "/api/reference":
                result = self.service.reference(data.get("unloaded_confirmed") is True)
            elif self.path == "/api/pulse/reference":
                if set(data) != {"unloaded_confirmed"}:
                    raise ValueError("脉搏初始化请求只接受 unloaded_confirmed 字段")
                result = self.service.pulse_reference(data.get("unloaded_confirmed") is True)
            elif self.path == "/api/press":
                if not isinstance(data.get("active"), bool):
                    raise ValueError("按压状态必须为布尔值")
                result = self.service.press(data["active"])
            elif self.path == "/api/experiment/start":
                result = self.service.experiment_start(data)
            elif self.path == "/api/experiment/stop":
                result = self.service.experiment_stop()
            elif self.path == "/api/experiment/point":
                self.service.experiment.select_point(data.get("point_id"))
                result = self.service.snapshot()
            elif self.path == "/api/experiment/baseline":
                result = self.service.experiment_baseline(data.get("unloaded_confirmed") is True)
            elif self.path == "/api/experiment/trial":
                result = self.service.experiment_trial(data.get("action"))
            elif self.path == "/api/experiment/mark":
                self.service.experiment.mark(data.get("reason"))
                result = self.service.snapshot()
            elif self.path == "/api/history/open":
                result = self.service.history_open(data.get("session_id"), data.get("deleted", False))
            elif self.path == "/api/history/delete":
                result = self.service.history_change(data.get("session_ids"))
            elif self.path == "/api/history/restore":
                result = self.service.history_change(data.get("session_ids"), restore=True)
            elif self.path == "/api/training/start":
                allowed = {"session_ids", "method", "epochs", "batch_size", "learning_rate", "lr", "patience", "seed",
                           "position_loss_weight", "wrench_loss_weight", "cpu_threads",
                           "max_spectrum_gap_ms", "adapt_legacy_unloaded", "training_mode", "base_model_id"}
                if set(data) - allowed:
                    raise ValueError("训练请求含未知字段")
                result = self.service.training_start(data.get("session_ids"), data.get("method", "linear"),
                    {key: value for key, value in data.items() if key not in {"session_ids", "method"}})
            elif self.path in {"/api/training/pause", "/api/training/resume", "/api/training/cancel"}:
                if data:
                    raise ValueError("训练控制请求不接受额外字段")
                result = self.service.training_control(self.path.rsplit("/", 1)[1])
            elif self.path == "/api/analysis/reference":
                result = self.service.analysis_reference(data.get("unloaded_confirmed") is True)
            elif self.path == "/api/analysis/stop":
                if data:
                    raise ValueError("结束推理请求不接受额外字段")
                result = self.service.analysis_stop()
            elif self.path in {"/api/analysis/models/delete", "/api/analysis/models/restore"}:
                if set(data) != {"model_ids"}:
                    raise ValueError("模型管理请求只接受 model_ids 字段")
                result = self.service.models_change(data["model_ids"], restore=self.path.endswith("/restore"))
            elif self.path in {"/api/analysis/preview", "/api/analysis/build", "/api/analysis/evaluate", "/api/analysis/pilot"}:
                keys = ("max_force_N",) if self.path == "/api/analysis/pilot" else ("method", "include_mlp", "max_force_N", "mlp_epochs", "drift_ranges_confirmed")
                options = {key: data[key] for key in keys if key in data}
                with self.service.command_lock:
                    result = self.service.analysis.start_job(self.path.rsplit("/", 1)[1], data.get("session_ids"), options)
            elif self.path == "/api/analysis/load":
                if "unloaded_confirmed" in data:
                    result = self.service.analysis_load(data.get("model_id"), data.get("unloaded_confirmed") is True)
                else:
                    result = self.service.analysis_load(data.get("model_id"))
            elif self.path == "/api/validation/freeze":
                result = self.service.validation_freeze(data.get("actual_point_id"), data.get("wrench_tolerances"))
            elif self.path == "/api/validation/judge":
                with self.service.command_lock:
                    result = {"validation": self.service.validation.judge(data.get("sample_id"), data.get("position_correct"), data.get("force_correct"), data.get("actual_point_id"))}
            elif self.path == "/api/validation/finish":
                with self.service.command_lock:
                    result = self.service.validation.finish()
            elif self.path == "/api/validation/discard":
                with self.service.command_lock:
                    result = {"validation": self.service.validation.discard(data.get("sample_id"))}
            elif self.path == "/api/compare":
                from .comparison import compare_algorithms
                frames = data.get("frames")
                if not isinstance(frames, list) or not frames:
                    raise ValueError("请提供带无载/按压标签和时间信息的原始光谱帧")
                result = compare_algorithms(frames, data.get("config"))
                from datetime import datetime, timezone
                import uuid
                output = self.service.userdata / "comparisons"
                output.mkdir(parents=True, exist_ok=True)
                file = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8] + ".json")
                result["report_path"] = str(file)
                file.write_text(json.dumps(clean(result), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            else:
                return self._json({"error": "接口不存在"}, 404)
            self._json(result)
        except json.JSONDecodeError:
            self._json({"error": "请求不是有效的 JSON，请检查文件格式"}, 400)
        except (ValueError, TypeError, KeyError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": "操作失败：" + str(exc)}, 500)

    def log_message(self, fmt, *args):
        if self.path != "/api/state":
            super().log_message(fmt, *args)


def main():
    from .qt_app import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
