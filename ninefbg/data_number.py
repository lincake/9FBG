"""Session-level automatic numbers; acquisition callbacks never perform this IO."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import uuid


SCHEMA = "9fbg_data_number_v1"
AUTOMATIC_SOURCE = "automatic_session_number_v1"
COUNTER_FILENAME = "data_number.json"


def _number(value):
    if isinstance(value, str) and len(value) <= 128 and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


class DataNumberAllocator:
    def __init__(self, userdata, preferences=None):
        self.userdata = Path(userdata)
        self.path = self.userdata / COUNTER_FILENAME
        self.lock = threading.RLock()
        self.next_value = 1
        self.last_completed_session_id = None
        self.error = None
        self._blocked = False
        self._persisted = None
        self._lease = None
        try:
            self._load_counter()
            self._reconcile_history()
            preferred = _number((preferences or {}).get("experiment", {}).get("data_id"))
            if preferred is not None:
                self.next_value = max(self.next_value, preferred + 1)
            self._validate_next()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self._blocked = True
            self.error = "自动数据编号读取失败：" + str(exc)

    def _validate_next(self):
        if len(str(self.next_value)) > 128:
            raise ValueError("自动数据编号已超过 128 位，请检查已有编号")

    def _load_counter(self):
        if _linked(self.path):
            raise ValueError("自动数据编号文件不能是链接")
        if not self.path.exists():
            return
        if not self.path.is_file() or self.path.stat().st_size > 64 * 1024:
            raise ValueError("自动数据编号文件格式或大小无效")
        state = json.loads(self.path.read_text(encoding="utf-8-sig"))
        value = _number(state.get("next_data_id"))
        last = state.get("last_completed_session_id")
        if (state.get("schema") != SCHEMA or value is None or value < 1
                or last is not None and (not isinstance(last, str) or len(last) > 100)):
            raise ValueError("自动数据编号文件版本或内容无效")
        self.next_value = max(self.next_value, value)
        self.last_completed_session_id = last
        self._persisted = (value, last)

    def _reconcile_history(self):
        # One startup scan also repairs a crash between the durable manifest and
        # counter replacement. Old identifiers and preferences are never edited.
        home = self.userdata.resolve()
        for name in ("calibration_sessions", "deleted_calibration_sessions"):
            root = self.userdata / name
            if _linked(root) or root.resolve().parent != home:
                raise ValueError("采集历史目录存在链接或超出允许位置")
            if not root.exists():
                continue
            if not root.is_dir():
                raise ValueError("采集历史位置不是文件夹")
            for directory in root.iterdir():
                if _linked(directory):
                    raise ValueError("采集历史目录包含链接，请先核对")
                if not directory.is_dir():
                    continue
                manifest = directory / "manifest.json"
                if _linked(manifest):
                    raise ValueError("采集历史清单不能是链接")
                if not manifest.exists():
                    continue  # Unfinished recordings do not have a saved ID.
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
                settings = data.get("settings", {})
                if data.get("kind") != "9fbg_calibration_experiment_v1" or not isinstance(settings, dict):
                    continue
                # Failed new automatic sessions do not consume their number.
                # Reserve all legacy numeric IDs, including incomplete archives.
                if settings.get("data_id_source") == AUTOMATIC_SOURCE and data.get("complete") is not True:
                    continue
                value = _number(settings.get("data_id"))
                if value is not None and value >= self.next_value:
                    self.next_value = value + 1
                    if data.get("complete") is True:
                        self.last_completed_session_id = data.get("session_id", directory.name)

    def current(self):
        with self.lock:
            if self._blocked:
                raise ValueError(self.error)
            self._validate_next()
            return str(self.next_value)

    def _refresh_saved(self):
        try:
            self._load_counter()
            self._reconcile_history()
            self._validate_next()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self._blocked = True
            self.error = "自动数据编号读取失败：" + str(exc)
            raise ValueError(self.error) from exc

    def _acquire_lease(self):
        path = self.userdata / ".data-number.lock"
        if _linked(path):
            raise ValueError("自动数据编号租约文件不能是链接")
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
        try:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise ValueError("另一个采集会话正在使用自动数据编号，请先结束并保存该会话") from exc
        self._lease = stream

    def begin(self):
        """Hold an OS lease for a tentative ID; no counter is incremented."""
        with self.lock:
            if self._blocked:
                raise ValueError(self.error)
            if self._lease is not None:
                raise ValueError("当前自动编号已有活动会话，请先停止并保存")
            self._acquire_lease()
            try:
                # Another process may have saved since our startup preview.
                self._refresh_saved()
                value = self.current()
                self._bind(value, None)
                return value
            except BaseException:
                self.release()
                raise

    def bind(self, session_id):
        with self.lock:
            if self._lease is None:
                raise ValueError("自动数据编号尚未取得会话租约")
            self._bind(str(self.next_value), session_id)

    def _bind(self, data_id, session_id):
        # The lease's small durable note is separate from the committed counter.
        # After a crash the OS releases the lease; an incomplete session may
        # reuse this tentative data_id while retaining its distinct session_id.
        value = json.dumps({"schema": SCHEMA, "tentative_data_id": data_id,
                            "session_id": session_id}, ensure_ascii=False).encode("utf-8")
        self._lease.seek(1)
        self._lease.truncate()
        self._lease.write(value)
        self._lease.flush()
        os.fsync(self._lease.fileno())

    def release(self):
        with self.lock:
            stream, self._lease = self._lease, None
            if stream is not None:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()

    def snapshot(self, experiment=None):
        experiment = experiment or {}
        settings = experiment.get("settings") or {}
        active = experiment.get("running") is True
        with self.lock:
            return {"mode": "automatic", "next_data_id": str(self.next_value),
                    "active_data_id": settings.get("data_id") if active else None,
                    "active_source": ("automatic" if settings.get("data_id_source") == AUTOMATIC_SOURCE else "legacy_manual") if active else None,
                    "error": self.error}

    def complete_session(self, session_id, directory):
        """Advance only from the stopped writer's complete, immutable manifest."""
        directory = Path(directory)
        root = self.userdata.resolve() / "calibration_sessions"
        manifest_path = directory / "manifest.json"
        if (_linked(directory) or directory.resolve().parent != root
                or directory.name != session_id or _linked(manifest_path)):
            raise ValueError("采集会话编号保存位置不一致")
        if not manifest_path.is_file():
            return False
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if (data.get("kind") != "9fbg_calibration_experiment_v1"
                or data.get("session_id") != session_id or data.get("complete") is not True):
            return False
        value = _number((data.get("settings") or {}).get("data_id"))
        if value is None:
            return False  # Explicit legacy text IDs remain untouched.
        with self.lock:
            if self._blocked:
                raise ValueError(self.error)
            if (self._persisted == (self.next_value, session_id)
                    and self.last_completed_session_id == session_id):
                return False
            temporary_lease = self._lease is None
            if temporary_lease:
                self._acquire_lease()
            # Publish the safe high-water mark even if persistence fails. The
            # saved manifest recovers the same value on the next process start.
            try:
                if temporary_lease:
                    self._refresh_saved()
                self.next_value = max(self.next_value, value + 1)
                self.last_completed_session_id = session_id
                self._validate_next()
                state = (self.next_value, session_id)
                if state == self._persisted:
                    return False
                try:
                    self._persist()
                except OSError as exc:
                    self.error = "会话已保存，编号状态保存失败：" + str(exc)
                    raise OSError(self.error) from exc
                self._persisted, self.error = state, None
                return True
            finally:
                if temporary_lease:
                    self.release()

    def _persist(self):
        if _linked(self.path):
            raise OSError("自动数据编号文件不能是链接")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(".data-number-" + uuid.uuid4().hex + ".tmp")
        document = {"schema": SCHEMA, "next_data_id": str(self.next_value),
                    "last_completed_session_id": self.last_completed_session_id}
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
