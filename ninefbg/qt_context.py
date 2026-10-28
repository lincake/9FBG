"""Native Qt command boundary; the acquisition service owns all instrument logic."""
from __future__ import annotations

from copy import deepcopy
import queue
import threading

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot

from .service import optical_request_settings


def dispatch(service, action, payload=None):
    """Call the existing Python workflows directly, without HTTP or a browser."""
    data = {} if payload is None else payload
    if not isinstance(data, dict):
        raise ValueError("操作参数必须为对象")
    if action == "state":
        return service.snapshot()
    if action == "settings":
        return service.preferences.snapshot() if not data else service.preferences.update(data.get("section"), data.get("values"))
    if action == "connect":
        options = {key: data[key] for key in ("record", "connect_force", "auto_reference", "integration_us", "sample_rate_hz") if key in data}
        for key in ("record", "connect_force", "auto_reference"):
            if key in options and type(options[key]) is not bool:
                raise ValueError("记录、六轴连接与自动九峰参考选项必须为布尔值")
        optical_request_settings(options.get("integration_us"), options.get("sample_rate_hz"))
        # Native connections are display-only unless an explicit caller requests recording.
        options.setdefault("record", False)
        options.setdefault("connect_force", False)
        return service.connect(data.get("mode", "hardware"), data.get("replay_frames"), **options)
    simple = {"disconnect": service.disconnect, "force/connect": service.connect_force,
              "force/disconnect": service.disconnect_force, "experiment/stop": service.experiment_stop,
              "history/sessions": service.history_snapshot, "analysis/models": service.models_snapshot,
              "analysis/report": service.analysis.full_report, "analysis/stop": service.analysis_stop}
    if action in simple:
        return simple[action]()
    if action in {"reference", "pulse_reference", "pulse/reference", "experiment/baseline", "analysis/reference"}:
        method = {"reference": service.reference, "pulse_reference": service.pulse_reference,
                  "pulse/reference": service.pulse_reference, "experiment/baseline": service.experiment_baseline,
                  "analysis/reference": service.analysis_reference}[action]
        return method(data.get("unloaded_confirmed") is True)
    if action in {"pulse_samples", "pulse/samples"}:
        return service.pulse_samples(data.get("after", 0), data.get("limit", 4096))
    if action == "press":
        if type(data.get("active")) is not bool:
            raise ValueError("按压状态必须为布尔值")
        return service.press(data["active"])
    if action == "experiment/start":
        return service.experiment_start(data)
    if action == "experiment/point":
        service.experiment.select_point(data.get("point_id"))
        return service.snapshot()
    if action == "experiment/trial":
        return service.experiment_trial(data.get("action"))
    if action == "experiment/mark":
        service.experiment.mark(data.get("reason"))
        return service.snapshot()
    if action == "history/open":
        return service.history_open(data.get("session_id"), data.get("deleted", False))
    if action in {"history/delete", "history/restore"}:
        return service.history_change(data.get("session_ids"), restore=action.endswith("restore"))
    if action == "training/start":
        return service.training_start(data.get("session_ids"), data.get("method", "linear"),
                                      {key: value for key, value in data.items() if key not in {"session_ids", "method"}})
    if action in {"training/pause", "training/resume", "training/cancel"}:
        return service.training_control(action.rsplit("/", 1)[1])
    if action in {"analysis/models/delete", "analysis/models/restore"}:
        return service.models_change(data.get("model_ids"), restore=action.endswith("restore"))
    if action in {"analysis/preview", "analysis/build", "analysis/evaluate", "analysis/pilot"}:
        with service.command_lock:
            return service.analysis.start_job(action.rsplit("/", 1)[1], data.get("session_ids"),
                                              {key: value for key, value in data.items() if key != "session_ids"})
    if action == "analysis/load":
        return service.analysis_load(data.get("model_id"), data.get("unloaded_confirmed") is True)
    if action == "validation/freeze":
        return service.validation_freeze(data.get("actual_point_id"), data.get("wrench_tolerances"))
    if action in {"validation/judge", "validation/finish", "validation/discard"}:
        with service.command_lock:
            if action.endswith("judge"):
                value = service.validation.judge(data.get("sample_id"), data.get("position_correct"),
                                                  data.get("force_correct"), data.get("actual_point_id"))
            elif action.endswith("discard"):
                value = service.validation.discard(data.get("sample_id"))
            else:
                return service.validation.finish()
        return {"validation": value}
    raise ValueError("不支持的桌面操作：" + action)


class _Worker(QThread):
    completed = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    def __init__(self, service, *, reader=False):
        super().__init__()
        self.service, self.reader = service, reader
        self.jobs = queue.Queue(maxsize=64)
        self.pending = set()
        self.guard = threading.Lock()

    def enqueue(self, action, payload=None):
        with self.guard:
            if self.reader and action in self.pending:
                return False
            try:
                self.jobs.put_nowait((action, deepcopy(payload)))
            except queue.Full:
                return False
            self.pending.add(action)
        return True

    def run(self):
        while True:
            action, payload = self.jobs.get()
            if action == "_stop":
                return
            try:
                if action == "_shutdown":
                    result = self.service.disconnect()
                    if result.get("stopping"):
                        raise ValueError(result.get("error") or "设备或写盘尚未完成关闭，请稍后重试退出")
                    self.service.close()
                    self.completed.emit(action, result)
                    return
                result = dispatch(self.service, action, payload)
                self.completed.emit(action, result)
            except Exception as exc:
                self.failed.emit(action, "操作失败：" + str(exc))
            finally:
                with self.guard:
                    self.pending.discard(action)

    def discard_pending(self, keep=()):
        with self.guard:
            retained = []
            while True:
                try:
                    action, payload = self.jobs.get_nowait()
                    if action in keep:
                        retained.append((action, payload))
                    else:
                        self.pending.discard(action)
                except queue.Empty:
                    break
            for job in retained:
                self.jobs.put_nowait(job)


class AppContext(QObject):
    snapshot_ready = pyqtSignal(dict)
    pulse_ready = pyqtSignal(dict)
    action_finished = pyqtSignal(str, object)
    error = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)
    shutdown_finished = pyqtSignal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.preferences = service.preferences.snapshot().get("values", {})
        self.state = {}
        self.closing = False
        self._busy_count = 0
        self._commands = _Worker(service)
        self._reads = _Worker(service, reader=True)
        for worker in (self._commands, self._reads):
            worker.completed.connect(self._completed)
            worker.failed.connect(self._failed)
            worker.start()
        self.timer = QTimer(self)
        self.timer.setInterval(160)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    @pyqtSlot()
    def refresh(self):
        if not self.closing:
            self._reads.enqueue("state")

    def execute(self, action, payload=None):
        if self.closing:
            return False
        action = action.removeprefix("/api/")
        read = action in {"state", "pulse_samples", "pulse/samples"}
        worker = self._reads if read else self._commands
        accepted = worker.enqueue(action, payload)
        if accepted and not read:
            self._busy_count += 1
            self.busy_changed.emit(True)
        elif not accepted and not read:
            self.error.emit("待处理操作过多，请等待当前操作完成")
        return accepted

    @pyqtSlot(str, object)
    def _completed(self, action, result):
        if action == "_shutdown":
            self.shutdown_finished.emit()
            return
        if action == "settings" and isinstance(result, dict):
            self.preferences = result.get("values", self.preferences)
        if action in {"pulse_samples", "pulse/samples"}:
            self.pulse_ready.emit(result)
        if action == "state" or isinstance(result, dict) and result.get("product") == "9FBG":
            self.state = result
            self.snapshot_ready.emit(result)
        self.action_finished.emit(action, result)
        if action not in {"state", "pulse_samples", "pulse/samples"}:
            self._busy_count = max(0, self._busy_count - 1)
            self.busy_changed.emit(bool(self._busy_count))
            self.refresh()

    @pyqtSlot(str, str)
    def _failed(self, action, message):
        if action == "_shutdown":
            self.closing = False
            self.timer.start()
        if action not in {"state", "pulse_samples", "pulse/samples"}:
            self._busy_count = max(0, self._busy_count - 1)
            self.busy_changed.emit(bool(self._busy_count))
        self.error.emit(message)

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.timer.stop()
        # Pending clicks must never open new devices after the window closes.
        self._commands.discard_pending(keep={"settings"})
        self._reads.discard_pending()
        self._busy_count = 0
        self.busy_changed.emit(False)
        self._commands.enqueue("_shutdown")

    def join(self, timeout_ms=5000):
        self.timer.stop()
        self._reads.discard_pending()
        self._reads.enqueue("_stop")
        self._reads.wait(timeout_ms)
        self._commands.wait(timeout_ms)

