"""Versioned software setup policy; synthetic frames do not qualify hardware."""
from copy import deepcopy

import pytest

from ninefbg.capture_setup import SETUP_POLICY, setup_allows_trial
from ninefbg.experiment import ExperimentManager, coordinate_definition, default_point_grid
from test_experiment import clock, context, force, ready, settings, spectrum


@pytest.mark.parametrize("protocol", ["guided_round_v1", "timed_wrench_v1", "force_target_wrench_v1"])
@pytest.mark.parametrize("confirmation", ["omitted", False, True])
def test_explicit_policy_starts_with_real_baseline_without_inventing_attestations(tmp_path, clock, protocol, confirmation):
    supplied = settings(setup_policy=SETUP_POLICY, acquisition_protocol=protocol, guided_acquisition=True)
    for key in ("force_direction_confirmed", "coordinates_confirmed"):
        if confirmation == "omitted":
            supplied.pop(key)
        else:
            supplied[key] = confirmation
    original = deepcopy(supplied)
    manager = ExperimentManager(tmp_path)
    try:
        manager.start(supplied, context(clock.now))
        manager.select_point("P01")
        with pytest.raises(ValueError, match="无载"):
            manager.baseline_request(False, context(clock.now))
        with pytest.raises(ValueError, match="无载"):
            manager.trial("start", context(clock.now))
        if protocol == "guided_round_v1":
            ready(manager, clock)
            active = context(clock.now, 2)
        else:
            manager.baseline_request(True, context(clock.now))
            for number in range(12):
                timestamp = clock.advance(40)
                assert manager.on_force(force(timestamp, 100 + number))
                assert manager.on_spectrum(spectrum(timestamp, 100 + number))
            assert manager.baseline["ready"]
            active = context(clock.now)
        snapshot = manager.trial("start", active)
        assert snapshot["trial_id"] == "T0001"
        assert manager.baseline["unloaded_confirmed"] is True
    finally:
        manager.stop()
    manifest = manager.get_session(manager.session_id)
    stored = manifest["settings"]
    assert supplied == original
    assert stored["setup_policy"] == SETUP_POLICY
    assert stored["force_direction_confirmed"] is (confirmation is True)
    assert stored["coordinates_confirmed"] is (confirmation is True)
    assert stored["force_sign"] == -1
    assert stored["coordinate_definition"] == manifest["coordinate_definition"] == coordinate_definition()
    assert stored["point_grid"] == default_point_grid()
    assert manifest["formal_training_qualified"] is False


@pytest.mark.parametrize("policy", [None, "", "configured_geometry_v2", 1, True, {}])
def test_unknown_setup_policy_rejected_before_any_session_is_written(tmp_path, clock, policy):
    manager = ExperimentManager(tmp_path)
    with pytest.raises(ValueError, match="设置策略"):
        manager.start(settings(setup_policy=policy), context(clock.now))
    assert not (tmp_path / "calibration_sessions").exists()


def test_legacy_setup_requirements_preserve_coordinate_alias_and_force_direction():
    assert not setup_allows_trial({})
    assert not setup_allows_trial({"coordinates_confirmed": True})
    assert setup_allows_trial({"coordinates_confirmed": True}, requires_force_direction=False)
    assert setup_allows_trial({"point_grid_confirmed": True, "force_direction_confirmed": True})
    assert not setup_allows_trial({"coordinates_confirmed": False, "point_grid_confirmed": True,
                                   "force_direction_confirmed": True})
    assert setup_allows_trial({"setup_policy": SETUP_POLICY})
