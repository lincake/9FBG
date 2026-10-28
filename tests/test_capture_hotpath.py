"""Capture hotpath replay; synthetic evidence does not qualify hardware."""
from bisect import bisect_left
from copy import deepcopy
import math
import random

from ninefbg.experiment import ExperimentManager
from ninefbg.force_target_wrench import CRITERIA, PROTOCOL


def label_manager(tmp_path):
    manager = ExperimentManager(tmp_path)
    manager.session_id = "synthetic-session"
    manager.settings = {"acquisition_protocol": PROTOCOL, "force_target_wrench_criteria": deepcopy(CRITERIA)}
    manager.baseline = {"force_zero": [0, 0, -1, 0, 0, 0]}
    state = {"trial_id": "T0001", "force_samples": [], "main_spectra": []}
    return manager, state


def full_label_statistics(manager, state):
    """The previous full-history rule, retained as a regression oracle."""
    rows = sorted(state["force_samples"], key=lambda row: row["stamp"])
    times = [row["stamp"] for row in rows]
    valid, contact, rejected = 0, 0, {}
    for sample in state["main_spectra"]:
        timestamp = sample["stamp"]
        index = bisect_left(times, timestamp)
        if index < len(rows) and times[index] == timestamp:
            values = rows[index]["values"]
        elif (0 < index < len(rows) and times[index]-times[index-1] <= 60_000_000
              and max(timestamp-times[index-1], times[index]-timestamp) <= 45_000_000):
            fraction = (timestamp-times[index-1])/(times[index]-times[index-1])
            values = [a+(b-a)*fraction for a, b in zip(rows[index-1]["values"], rows[index]["values"])]
        else:
            rejected["missing_or_distant_bracket"] = rejected.get("missing_or_distant_bracket", 0)+1
            continue
        valid += 1
        corrected = [v-z for v, z in zip(values, manager.baseline["force_zero"])]
        contact += math.sqrt(sum(v*v for v in corrected[:3])) > manager._wrench_rules()["contact_force_norm_N"]
    return valid, contact, rejected


def test_incremental_labels_preserve_late_brackets_boundaries_duplicates_and_order(tmp_path):
    manager, state = label_manager(tmp_path)
    rng = random.Random(981)
    source_time = 1_000_000_000
    # Include source gaps and late spectra. Every prefix must agree, including
    # pending spectra currently rejected but recoverable by a later force row.
    for number in range(250):
        source_time += rng.choice([2, 10, 40, 60, 80])*1_000_000
        if number % 3 != 0:
            state["main_spectra"].append({"stamp": source_time+rng.choice([-50, -20, 0, 20, 45, 80])*1_000_000})
        if number % 4 != 0:
            state["force_samples"].append({"stamp": source_time, "values": [rng.choice([0, .1, .2, 4]), 0, -1, 0, 0, 0]})
        assert manager._target_label_statistics(state) == full_label_statistics(manager, state)
    # Equal times retain the first arrival under stable sort/bisect_left. A late
    # older frame must rebuild already finalized brackets using the old rule.
    for timestamp, value in [(source_time, 99), (source_time-27_000_000, 2), (source_time+40_000_000, 3)]:
        state["force_samples"].append({"stamp": timestamp, "values": [value, 0, -1, 0, 0, 0]})
        assert manager._target_label_statistics(state) == full_label_statistics(manager, state)


def test_incremental_labels_reset_for_reference_threshold_trial_and_source_replacement(tmp_path):
    manager, state = label_manager(tmp_path)
    state["force_samples"] = [{"stamp": 1_000_000_000, "values": [.2, 0, -1, 0, 0, 0]}]
    state["main_spectra"] = [{"stamp": 1_000_000_000}]
    assert manager._target_label_statistics(state) == (1, 1, {})
    manager.baseline["force_zero"] = [.2, 0, -1, 0, 0, 0]
    assert manager._target_label_statistics(state) == (1, 0, {})
    manager.baseline["force_zero"] = [0, 0, -1, 0, 0, 0]
    manager.settings["force_target_wrench_criteria"]["contact_force_norm_N"] = .5
    assert manager._target_label_statistics(state) == (1, 0, {})
    state.update(trial_id="T0002", main_spectra=[{"stamp": 2_000_000_000}])
    assert manager._target_label_statistics(state) == (0, 0, {"missing_or_distant_bracket": 1})
    # Same-length source replacement must invalidate the old source identity.
    state["force_samples"] = [{"stamp": 2_000_000_000, "values": [1, 0, -1, 0, 0, 0]}]
    assert manager._target_label_statistics(state) == (1, 1, {})
    answer = manager._target_label_statistics(state)
    answer[2]["caller mutation"] = 12
    assert manager._target_label_statistics(state) == (1, 1, {})


def test_long_capture_matches_each_spectrum_once_after_its_right_endpoint(tmp_path, monkeypatch):
    manager, state = label_manager(tmp_path)
    import ninefbg.force_target_wrench as target_module
    original = target_module._label_values
    calls = 0

    def counted_match(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(target_module, "_label_values", counted_match)
    for number in range(14000):
        timestamp = 1_000_000_000 + number*40_000_000
        state["force_samples"].append({"stamp": timestamp, "values": [4, 0, -1, 0, 0, 0]})
        manager._target_label_statistics(state)
        state["main_spectra"].append({"stamp": timestamp+20_000_000})
        manager._target_label_statistics(state)
    assert manager._target_label_statistics(state) == (13999, 13999, {"missing_or_distant_bracket": 1})
    # Work grows with new spectra, not historical spectra times polling count.
    assert calls == 13999
    before = calls
    for _ in range(1000):
        manager._target_label_statistics(state)
    assert calls == before
    state["force_samples"].append({"stamp": timestamp+40_000_000, "values": [4, 0, -1, 0, 0, 0]})
    assert manager._target_label_statistics(state) == (14000, 14000, {})
    assert calls == before+1
    assert manager._target_label_statistics(state) == full_label_statistics(manager, state)
