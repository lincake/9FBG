"""Independent CPU process with batch-safe cooperative pause/cancel protocol."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import multiprocessing
from pathlib import Path
import queue
import threading
import time
import uuid

ACTIVE = {"running", "pause_requested", "paused", "cancelling"}
METHOD_LABEL = "全谱 MLP＋GRU（六维力）"
DRIFT_METHOD_LABEL = "寻峰漂移 MLP＋GRU（六维力）"
DEFAULTS = {"epochs": 100, "batch_size": 32, "learning_rate": .001, "patience": 10,
            "seed": 1701, "cpu_threads": 2, "position_loss_weight": 1., "wrench_loss_weight": 1.,
            "max_spectrum_gap_ms": 120, "adapt_legacy_unloaded": False,
            "training_mode": "scratch", "base_model_id": None, "method": "spectral_gru"}


def checked_options(options):
    value = dict(options or {})
    method = value.get("method", "spectral_gru")
    if not isinstance(method, str) or method not in {"spectral_gru", "drift_gru"}:
        raise ValueError("不支持的六维 MLP＋GRU 训练方法")
    if "lr" in value:
        value.setdefault("learning_rate", value.pop("lr"))
    if set(value) - set(DEFAULTS):
        raise ValueError("未知全谱训练参数：" + "、".join(sorted(set(value) - set(DEFAULTS))))
    mode = value.get("training_mode", "scratch")
    if not isinstance(mode, str) or mode not in {"scratch", "finetune"}:
        raise ValueError("训练模式只允许从头训练或继承权重微调")
    base = value.get("base_model_id")
    if mode == "scratch" and base is not None:
        raise ValueError("从头训练不能指定父模型")
    if mode == "finetune":
        from .model_history import model_id
        if model_id(base)[0] != "S":
            raise ValueError("继承微调只接受已保存的全谱 S 模型")
        value.setdefault("learning_rate", .0001)
    value = {**DEFAULTS, **value}
    for key, low, high in (("epochs", 1, 1000), ("batch_size", 1, 1024), ("patience", 1, 1000),
                           ("seed", 0, 2 ** 31 - 1), ("cpu_threads", 1, 8)):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(key + " 参数超出允许范围")
    for key, low, high in (("learning_rate", 1e-6, .1), ("position_loss_weight", 0., 100.),
                           ("wrench_loss_weight", 0., 100.), ("max_spectrum_gap_ms", 40., 1000.)):
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or not low <= value[key] <= high:
            raise ValueError(key + " 参数超出允许范围")
    if value["position_loss_weight"] == value["wrench_loss_weight"] == 0:
        raise ValueError("位置和六维损失权重不能同时为零")
    if type(value["adapt_legacy_unloaded"]) is not bool:
        raise ValueError("旧会话无载适配开关必须为布尔值")
    if method == "drift_gru" and value["max_spectrum_gap_ms"] > 120:
        raise ValueError("九路漂移的源帧连续性间隔不能超过 120 ms")
    return value


class _Cancelled(Exception):
    pass


class _Control:
    def __init__(self, commands, events):
        self.commands, self.events = commands, events
        self.reasons = set()
        self.cancelled = False
        self.was_paused = False

    def _drain(self):
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            if command.get("action") == "cancel":
                self.cancelled = True
            elif command.get("action") == "pause_reasons":
                self.reasons = set(command["reasons"])

    def checkpoint(self):
        self._drain()
        if self.cancelled:
            raise _Cancelled()
        while self.reasons:
            if not self.was_paused:
                self.events.put({"status": "paused", "pause_reasons": sorted(self.reasons), "stage": "paused"})
                self.was_paused = True
            time.sleep(.04)
            self._drain()
            if self.cancelled:
                raise _Cancelled()
        if self.was_paused:
            self.events.put({"status": "running", "pause_reasons": [], "stage": "resuming"})
            self.was_paused = False


def _metrics(data, indices, logits, predictions, contact_probabilities=None):
    import numpy as np
    from .spectral_model import AXES, UNITS, CONTACT_DEFINITION
    positions = data["position"][indices]
    pos_valid = data["position_mask"][indices]
    force_valid = data["wrench_mask"][indices]
    guesses = logits.argmax(1)
    confusion = np.zeros((25, 25), dtype=np.int64)
    for truth, guess in zip(positions[pos_valid], guesses[pos_valid]):
        confusion[truth, guess] += 1
    point_count = confusion.sum(1)
    recall = {f"P{index + 1:02d}": float(confusion[index, index] / count) if count else None
              for index, count in enumerate(point_count)}
    truth = data["wrench"][indices]
    error = predictions - truth

    def force_report(mask):
        if not mask.any():
            return {"count": 0, "mae": None, "rmse": None}
        selected = error[mask]
        return {"count": int(mask.sum()), "mae": np.abs(selected).mean(0).tolist(),
                "rmse": np.sqrt(np.square(selected).mean(0)).tolist()}

    phases = np.asarray([data["windows"][index]["phase"] for index in indices])
    force_norm = np.linalg.norm(truth[:, :3], axis=1)
    report = {"window_count": len(indices), "position_count": int(pos_valid.sum()),
              "position_accuracy": float((guesses[pos_valid] == positions[pos_valid]).mean()) if pos_valid.any() else None,
              "confusion_matrix": confusion.tolist(), "point_recall": recall,
              "axes": AXES, "units": UNITS, "wrench": force_report(force_valid),
              "by_phase": {phase: force_report(force_valid & (phases == phase)) for phase in sorted(set(phases))},
              "by_force_norm_N": {f"{low:g}–{high:g}": force_report(force_valid & (force_norm >= low) & (force_norm < high))
                                  for low, high in ((0, .1), (.1, 1), (1, 3), (3, 5), (5, float("inf")))}}
    report["position_by_force_norm_N"] = {}
    for low, high in ((0, .1), (.1, 1), (1, 3), (3, 5), (5, float("inf"))):
        mask = pos_valid & (force_norm >= low) & (force_norm < high)
        report["position_by_force_norm_N"][f"{low:g}–{high:g}"] = {
            "count": int(mask.sum()),
            "accuracy": float((guesses[mask] == positions[mask]).mean()) if mask.any() else None}
    if contact_probabilities is not None:
        valid = data["contact_mask"][indices]
        targets = data["contact"][indices] > .5
        positive, negative = valid & targets, valid & ~targets
        accepted = contact_probabilities >= CONTACT_DEFINITION["on_probability"]
        retained = contact_probabilities >= CONTACT_DEFINITION["off_probability"]
        report["contact"] = {
            "definition": dict(CONTACT_DEFINITION), "count": int(valid.sum()),
            "contact_count": int(positive.sum()), "no_contact_count": int(negative.sum()),
            "decision_rule": "fresh_state_on_probability; retention_reported_separately",
            "accuracy": float((accepted[valid] == targets[valid]).mean()) if valid.any() else None,
            "contact_recall": float(accepted[positive].mean()) if positive.any() else None,
            "no_contact_false_positive_rate": float(accepted[negative].mean()) if negative.any() else None,
            "no_contact_retention_rate": float(retained[negative].mean()) if negative.any() else None,
            "brier_score": float(np.square(contact_probabilities[valid] - targets[valid]).mean()) if valid.any() else None,
            "true_positive": int((accepted & positive).sum()), "false_positive": int((accepted & negative).sum()),
            "true_negative": int((~accepted & negative).sum()), "false_negative": int((~accepted & positive).sum())}
    return report


def _worker(userdata, job_id, session_ids, options, commands, events, plan):
    # multiprocessing imports only this module; no server/service/device construction.
    control = _Control(commands, events)
    try:
        control.checkpoint()
        from .spectral_data import load_dataset, fit_normalization, training_selection
        from .spectral_model import (torch_module, build_network, masked_losses, save_bundle, canonical_digest,
                                     has_contact_head, CONTACT_DEFINITION)
        from .spectral_finetuning import verify_plan, status_fields
        import numpy as np
        torch = torch_module()
        torch.set_num_threads(options["cpu_threads"])
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass
        torch.manual_seed(options["seed"])
        np.random.seed(options["seed"])
        events.put({"status": "running", "stage": "scanning", "progress_percent": 1,
                    "message": "正在核验完整原始会话与哈希"})
        parent, network = verify_plan(userdata, plan, checkpoint=control.checkpoint, load_weights=True)
        # load_bundle constrains inference threads; this worker owns its own CPU
        # budget and never shares the online model instance.
        torch.set_num_threads(options["cpu_threads"])
        data = load_dataset(userdata, session_ids, options, checkpoint=control.checkpoint,
                            progress=lambda record: events.put({"stage": "preparing", "progress_percent": 10,
                                                               "source_progress": record}),
                            inherited_split=parent["split"] if parent else None, expected_metadata=parent,
                            new_training_sessions=plan["new_session_ids"] if parent else None)
        control.checkpoint()
        train_indices, sampling = training_selection(data)
        contact_head = has_contact_head(parent) if parent else True
        validation_indices = np.flatnonzero(data["split_labels"] == "validation")
        if any(source["batch"] == "C" for source in data["acquisition_provenance"]["sources"]):
            raise ValueError("C 批保持独立测试用途，不能用于本次训练或调参")
        if not len(train_indices) or not data["position_mask"][train_indices].any():
            raise ValueError("训练集合缺少可信接触点位标签")
        if contact_head:
            if "contact" not in data or "contact_mask" not in data:
                raise ValueError("新版模型缺少接触监督，请重新建立完整数据集")
            supervised = data["contact"][train_indices][data["contact_mask"][train_indices]]
            if not (np.any(supervised == 0) and np.any(supervised == 1)):
                raise ValueError("独立接触头训练必须同时包含可信无接触负样本和真实按压正样本")
        elif any(row.get("sample_label") == "no_contact" for row in data["windows"]):
            raise ValueError("旧版模型没有接触头，不能继承无接触标签；请选从头训练生成新版接触模型")
        if parent:
            new_sources = set(plan["new_session_ids"])
            if not any(data["windows"][index]["trial_id"].split(":", 1)[0] in new_sources and
                       ((data["position_mask"][index] and options["position_loss_weight"] > 0) or
                        (data["wrench_mask"][index] and options["wrench_loss_weight"] > 0) or
                        (contact_head and data["contact_mask"][index])) for index in train_indices):
                raise ValueError("新增会话没有可用于优化的训练窗；请另采合格训练来源，固定验证数据不能转作训练")
            input_normalization = deepcopy(parent["input_normalization"])
            output_normalization = deepcopy(parent["output_normalization"])
        else:
            input_normalization, output_normalization = fit_normalization(data, train_indices)
        x_mean = np.asarray(input_normalization["mean"], dtype=np.float32)
        x_scale = np.asarray(input_normalization["scale"], dtype=np.float32)
        y_mean = np.asarray(output_normalization["mean"], dtype=np.float32)
        y_scale = np.asarray(output_normalization["scale"], dtype=np.float32)
        # Arrays are rebuilt from immutable raw records and never read active JSONL.
        x = torch.from_numpy((data["x"] - x_mean) / x_scale)
        target_y = torch.from_numpy((data["wrench"] - y_mean) / y_scale)
        target_p = torch.from_numpy(data["position"])
        p_mask = torch.from_numpy(data["position_mask"])
        y_mask = torch.from_numpy(data["wrench_mask"])
        target_c = torch.from_numpy(data["contact"]) if contact_head else None
        c_mask = torch.from_numpy(data["contact_mask"]) if contact_head else None
        train_set = torch.utils.data.TensorDataset(torch.from_numpy(train_indices))
        generator = torch.Generator().manual_seed(options["seed"])
        loader = torch.utils.data.DataLoader(train_set, batch_size=options["batch_size"], shuffle=True,
                                            num_workers=0, generator=generator)
        network = (network if parent else build_network(data.get("input_kind", "delta_S"), contact_head=True)).float().cpu()
        optimizer = torch.optim.Adam(network.parameters(), lr=options["learning_rate"])
        best_state, best_loss, best_epoch, wait = None, float("inf"), None, 0
        history = []

        def evaluate(indices):
            logits, predictions, probabilities = [], [], []
            total_p = total_y = total_c = 0.
            count_p = count_y = count_c = 0
            network.eval()
            with torch.inference_mode():
                for begin in range(0, len(indices), options["batch_size"]):
                    control.checkpoint()
                    selected = torch.from_numpy(indices[begin:begin + options["batch_size"]])
                    outputs = network(x[selected])
                    batch_logits, batch_y = outputs[:2]
                    lp, ly = masked_losses(batch_logits, batch_y, target_p[selected], target_y[selected],
                                           p_mask[selected], y_mask[selected])
                    np_, ny_ = int(p_mask[selected].sum()), int(y_mask[selected].sum())
                    total_p += float(lp) * np_
                    total_y += float(ly) * ny_
                    count_p += np_
                    count_y += ny_
                    if contact_head:
                        _, _, lc = masked_losses(batch_logits, batch_y, target_p[selected], target_y[selected],
                            p_mask[selected], y_mask[selected], outputs[2], target_c[selected], c_mask[selected])
                        nc_ = int(c_mask[selected].sum())
                        total_c += float(lc) * nc_
                        count_c += nc_
                        probabilities.append(torch.sigmoid(outputs[2]).numpy())
                    logits.append(batch_logits.numpy())
                    predictions.append(batch_y.numpy() * y_scale + y_mean)
            weighted_loss = (options["position_loss_weight"] * total_p / max(count_p, 1)
                             + options["wrench_loss_weight"] * total_y / max(count_y, 1)
                             + CONTACT_DEFINITION["loss_weight"] * total_c / max(count_c, 1))
            return (weighted_loss, (np.concatenate(logits) if logits else np.empty((0, 25))),
                    (np.concatenate(predictions) if predictions else np.empty((0, 6))),
                    np.concatenate(probabilities) if probabilities else None)

        comparison = None
        if parent:
            old_validation = set(parent["validation_trial_ids"])
            fixed_indices = np.asarray([index for index in validation_indices
                                        if data["windows"][index]["trial_id"] in old_validation], dtype=np.int64)
            if {data["windows"][index]["trial_id"] for index in fixed_indices} != old_validation:
                raise ValueError("父模型固定验证试次缺少原始窗口，不能建立可比较结果")
            identities = [{key: data["windows"][index][key] for key in
                           ("trial_id", "baseline_id", "reference_id", "stream_epoch", "frame_ids", "stamp")}
                          for index in fixed_indices]
            parent_eval = evaluate(fixed_indices) if len(fixed_indices) else None
            comparison = {"schema": "spectral_parent_child_validation_v1", "available": bool(len(fixed_indices)),
                          "reason": None if len(fixed_indices) else "父模型没有独立验证试次，无法比较父子验证结果",
                          "validation_trial_ids": sorted(old_validation), "window_count": len(fixed_indices),
                          "subset_count": len(fixed_indices),
                          "window_identity_sha256": canonical_digest(identities) if identities else None,
                          "parent": _metrics(data, fixed_indices, *parent_eval[1:]) if parent_eval else None,
                          "child": None, "normalization_inherited": True, "checkpoint_resume": False}

        last_update = 0.
        for epoch in range(1, options["epochs"] + 1):
            control.checkpoint()
            network.train()
            total = 0.
            for batch_index, (selected,) in enumerate(loader):
                control.checkpoint()  # Pause acknowledgment is issued outside each completed batch.
                optimizer.zero_grad(set_to_none=True)
                outputs = network(x[selected])
                logits, predicted = outputs[:2]
                lp, ly = masked_losses(logits, predicted, target_p[selected], target_y[selected], p_mask[selected], y_mask[selected])
                loss = options["position_loss_weight"] * lp + options["wrench_loss_weight"] * ly
                if contact_head:
                    _, _, lc = masked_losses(logits, predicted, target_p[selected], target_y[selected],
                        p_mask[selected], y_mask[selected], outputs[2], target_c[selected], c_mask[selected])
                    loss = loss + CONTACT_DEFINITION["loss_weight"] * lc
                if not torch.isfinite(loss).item():
                    raise ValueError("全谱训练损失出现非有限数值")
                loss.backward()
                torch.nn.utils.clip_grad_norm_(network.parameters(), 5.)
                optimizer.step()
                total += float(loss.detach()) * len(selected)
                if time.monotonic() - last_update >= .15:
                    last_update = time.monotonic()
                    events.put({"status": "running", "stage": "training", "epoch": epoch,
                                "batch": batch_index + 1, "batch_count": len(loader), "epochs": options["epochs"],
                                "progress_percent": min(94., 15. + 79. * ((epoch - 1) + (batch_index + 1) / len(loader)) / options["epochs"]),
                                "train_loss": total / max(1, (batch_index + 1) * options["batch_size"])})
            control.checkpoint()
            train_loss = total / len(train_indices)
            validation_loss = evaluate(validation_indices)[0] if len(validation_indices) else None
            history.append({"epoch": epoch, "train_loss": train_loss, "validation_loss": validation_loss})
            if validation_loss is not None:
                if validation_loss < best_loss - 1e-7:
                    best_loss, best_epoch, wait = validation_loss, epoch, 0
                    best_state = {key: value.detach().clone() for key, value in network.state_dict().items()}
                else:
                    wait += 1
                if wait >= options["patience"]:
                    break
        if best_state is not None:
            network.load_state_dict(best_state)
        control.checkpoint()
        train_eval = evaluate(train_indices)
        val_eval = evaluate(validation_indices) if len(validation_indices) else None
        if comparison and comparison["available"]:
            child_eval = evaluate(fixed_indices)
            comparison["child"] = _metrics(data, fixed_indices, *child_eval[1:])
        trained_points = sorted({f"P{position + 1:02d}" for position in data["position"][train_indices][data["position_mask"][train_indices]]})
        evaluation = {"schema": "spectral_evaluation_v1", "independent_validation": bool(len(validation_indices)),
                      "validation_note": "完整会话隔离" if len(validation_indices) and data["split"]["group_mode"] == "session" else
                                         "完整试次与共享无载来源隔离" if len(validation_indices) else "数据不足，无独立验证；训练指标不能代表独立准确率",
                       "train": _metrics(data, train_indices, *train_eval[1:]),
                       "validation": _metrics(data, validation_indices, *val_eval[1:]) if val_eval else None,
                      "trained_point_ids": trained_points, "best_epoch": best_epoch, "history": history,
                      "split_group_mode": data["split"]["group_mode"], "rejected": data["rejected"],
                      "training_sampling": sampling,
                      "train_distribution": "selected_for_optimization", "validation_distribution": "complete_natural_distribution",
                      "hardware_accuracy_qualified": False, "c_batch_used": False,
                      **status_fields(plan), "finetuning_comparison": comparison}
        lineage = {"schema": "spectral_weights_lineage_v1", **status_fields(plan), "weights_only": True,
                   "checkpoint_resume": False, "normalization_source": "parent_model" if parent else "train_split",
                   "normalization_parent_sha256": plan["parent_model_sha256"]}
        drift = data.get("input_kind") == "delta_lambda_9"
        metadata = {"wavelength_nm": data["wavelength_nm"], "preprocessing": data["preprocessing"],
                    "sample_rate_hz": data["sample_rate_hz"], "trained_point_ids": trained_points,
                    "input_normalization": input_normalization, "output_normalization": output_normalization,
                    "acquisition_provenance": data["acquisition_provenance"], "split": data["split"],
                    "evaluation": evaluation, "references": data["references"], "sync_rule": data["sync_rule"],
                    "training_options": options, "source_window_count": len(data["windows"]),
                    **status_fields(plan), "training_lineage": lineage,
                    "training_sampling": sampling,
                    "training_trial_ids": sorted({data["windows"][index]["trial_id"] for index in train_indices}),
                    "validation_trial_ids": sorted({data["windows"][index]["trial_id"] for index in validation_indices}),
                    "window_rule": {"version": "nine_fbg_causal_windows_v1" if drift else "spectral_causal_windows_v1", "length": 16,
                                    "max_gap_ms": options["max_spectrum_gap_ms"], "target": "last_frame", "hidden_init": "zero"}}
        if contact_head:
            metadata["contact_definition"] = dict(CONTACT_DEFINITION)
            metadata["contact_label_rule"] = deepcopy(data["contact_label_rule"])
        if drift:
            metadata.update(input_kind="delta_lambda_9", method="drift_gru", input_units="pm",
                            fbg_ids=data["fbg_ids"], peak_definition=data["peak_definition"])
        control.checkpoint()
        verify_plan(userdata, plan, checkpoint=control.checkpoint, verify_raw=True)
        model_id = "S" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        events.put({"stage": "saving", "progress_percent": 97, "message": "正在校验并原子保存候选模型"})
        saved = save_bundle(userdata, model_id, network, metadata)
        # A cancellation arriving during atomic save is honored; a complete candidate is retained as evidence.
        control.checkpoint()
        events.put({"status": "completed", "stage": "completed", "progress_percent": 100,
                    "model_id": model_id, "point_ids": trained_points, "report": evaluation,
                    "created_utc": saved["created_utc"], "message": "候选模型已完整保存，可在无接触间隙手动应用"})
    except _Cancelled:
        events.put({"status": "cancelled", "stage": "cancelled", "message": "训练已取消，进程已停止"})
    except Exception as exc:
        events.put({"status": "failed", "stage": "failed", "error": str(exc),
                    "message": ("九路漂移训练失败：" if options.get("method") == "drift_gru" else "全谱训练失败：") + str(exc)})
    finally:
        events.close()
        events.join_thread()


class SpectralTrainingManager:
    """One worker slot, with pause reasons owned independently by user/acquisition."""
    def __init__(self, userdata):
        self.userdata = Path(userdata)
        self.lock = threading.RLock()
        self.process = self.commands = self.events = self.monitor = None
        self.pause_reasons = set()
        self._closed = False
        self._status_revision = 0
        self._state = {"status": "idle", "stage": "idle", "method": "spectral_gru", "method_label": METHOD_LABEL,
                       "job_id": None, "model_id": None, "session_ids": [], "point_ids": [], "progress_percent": 0,
                       "training_mode": "scratch", "base_model_id": None, "parent_model_sha256": None,
                       "selected_session_ids": [], "new_session_ids": [], "merged_session_ids": [],
                       "error": None, "report": None, "pause_reasons": [], "busy": False, "active": False, "running": False}

    @property
    def active(self):
        with self.lock:
            return self._state["status"] in ACTIVE

    def status(self):
        with self.lock:
            value = deepcopy(self._state)
            # A terminal event can arrive before its process/queues have been
            # reaped. Keep the slot busy until the watcher releases it so an
            # immediate next generation cannot race process cleanup.
            value["busy"] = value["active"] = value["running"] = value["status"] in ACTIVE or self.process is not None
            value["progress_pct"] = value["progress_percent"]
            value["pause_reasons"] = sorted(self.pause_reasons) if value["active"] else []
            return value

    def status_if_changed(self, revision):
        """Avoid copying a completed evaluation on every acquisition tick."""
        with self.lock:
            if revision == self._status_revision:
                return self._status_revision, None
            return self._status_revision, self.status()

    def start(self, session_ids, options=None):
        from .spectral_finetuning import prepare_training, status_fields
        explicit_options = dict(options or {})
        options = checked_options(options)
        session_ids, plan = prepare_training(self.userdata, session_ids, options, explicit_options=explicit_options)
        with self.lock:
            if self._closed:
                raise ValueError("训练管理器已关闭")
            if self.active or self.process is not None:
                raise ValueError("已有训练任务，暂停中的任务仍占用训练名额")
            self.pause_reasons.clear()
            self._status_revision += 1
            job_id = "spectral_" + uuid.uuid4().hex[:16]
            self._state.update(status="running", stage="starting", job_id=job_id, model_id=None,
                               method=options["method"],
                               method_label=DRIFT_METHOD_LABEL if options["method"] == "drift_gru" else METHOD_LABEL,
                               session_ids=list(session_ids), point_ids=[], progress_percent=0, error=None, report=None,
                               options=deepcopy(options), created_utc=datetime.now(timezone.utc).isoformat(),
                               **status_fields(plan),
                               message="正在启动独立 CPU 训练进程")
            context = multiprocessing.get_context("spawn")
            self.commands, self.events = context.Queue(), context.Queue()
            self.process = context.Process(target=_worker, args=(str(self.userdata), job_id, session_ids, options,
                                                                self.commands, self.events, plan), name="9FBG-spectral-training", daemon=True)
            try:
                self.process.start()
            except Exception:
                self.commands.close()
                self.events.close()
                self.process = self.commands = self.events = None
                self._state.update(status="failed", stage="failed", error="无法启动训练子进程")
                self._status_revision += 1
                raise
            self._state["worker_pid"] = self.process.pid
            self.monitor = threading.Thread(target=self._watch, name="9FBG-spectral-training-monitor", daemon=True)
            self.monitor.start()
            return self.status()

    def _watch(self):
        with self.lock:
            process, events, commands = self.process, self.events, self.commands
        try:
            while True:
                try:
                    event = events.get(timeout=.1)
                except queue.Empty:
                    if process.is_alive():
                        continue
                    break
                with self.lock:
                    status = event.get("status")
                    # A stale running update cannot undo a more recent request.
                    if self._state["status"] == "cancelling" and status in {"running", "paused"}:
                        event.pop("status", None)
                    elif self.pause_reasons and status == "running":
                        event["status"] = "pause_requested"
                    elif not self.pause_reasons and status == "paused":
                        event.pop("status", None)
                    self._state.update(event)
                    self._status_revision += 1
                    if self._state["status"] != "completed":
                        self._state["progress_percent"] = min(99, self._state["progress_percent"])
            process.join(timeout=2)
            with self.lock:
                if self._state["status"] in ACTIVE:
                    self._state.update(status="cancelled" if self._state["status"] == "cancelling" else "failed",
                                       stage="stopped", error=None if self._state["status"] == "cancelling" else
                                       "训练进程提前退出（退出码 " + str(process.exitcode) + "）")
                    self._status_revision += 1
        except (EOFError, OSError, ValueError) as exc:
            with self.lock:
                if self._state["status"] in ACTIVE:
                    self._state.update(status="failed", stage="failed", error="训练进程通信结束：" + str(exc))
                    self._status_revision += 1
        finally:
            if not process.is_alive():
                process.close()
            for channel in (commands, events):
                try:
                    channel.close()
                    channel.cancel_join_thread()
                except (OSError, ValueError):
                    pass
            with self.lock:
                if self.process is process:
                    self.process = self.commands = self.events = None
                    self._status_revision += 1

    def pause(self, reason="user"):
        if reason not in {"user", "acquisition"}:
            raise ValueError("未知训练暂停原因")
        with self.lock:
            if self.active and self._state["status"] != "cancelling":
                self.pause_reasons.add(reason)
                self._status_revision += 1
                self.commands.put({"action": "pause_reasons", "reasons": sorted(self.pause_reasons)})
                if self._state["status"] != "paused":
                    self._state.update(status="pause_requested", message="等待当前批次结束后暂停")
            return self.status()

    def resume(self, reason="user"):
        if reason not in {"user", "acquisition"}:
            raise ValueError("未知训练暂停原因")
        with self.lock:
            self._status_revision += 1
            self.pause_reasons.discard(reason)
            if self.active and self._state["status"] != "cancelling":
                self.commands.put({"action": "pause_reasons", "reasons": sorted(self.pause_reasons)})
                if not self.pause_reasons:
                    self._state.update(status="running", message="正在继续训练")
            return self.status()

    def cancel(self):
        with self.lock:
            if self.active and self._state["status"] != "cancelling":
                self._state.update(status="cancelling", message="正在取消并回收训练进程")
                self._status_revision += 1
                self.commands.put({"action": "cancel"})
            return self.status()

    def close(self):
        with self.lock:
            self._closed = True
            self.cancel()
            process, monitor = self.process, self.monitor
        if monitor and monitor is not threading.current_thread():
            monitor.join(timeout=3)
        if process:
            try:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                if monitor and monitor.is_alive():
                    monitor.join(timeout=2)
            except ValueError:  # Monitor already reaped and closed it.
                pass
        return self.status()
