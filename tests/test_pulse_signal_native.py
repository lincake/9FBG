"""Synthetic software parity only; these fixtures do not qualify physical pulse accuracy."""
from copy import deepcopy
import json
import math
from pathlib import Path
import shutil
import subprocess

import pytest

from ninefbg.pulse_signal import (PulseSession, BAND_HZ, CALIBRATION_METHOD,
                                 CENTER_CHANNEL, FILTER_CONFIG_SCHEMA,
                                 noise_weights, validate_filter_band_hz)

ROOT = Path(__file__).resolve().parents[1]


def frames(count, start=100, seq=1, rate=50, pulse=False, jitter=False):
    """Controlled artificial values and identities, never physical acceptance data."""
    rows = []
    stamp = start
    for index in range(count):
        number = seq + index
        values = [100 + 0.025 * math.sin(index * (0.13 + j / 100)) +
                  (2 * math.sin(2 * math.pi * 1.2 * stamp) if pulse and j == 4 else 0)
                  for j in range(9)]
        rows.append(dict(seq=number, t_ns=round(stamp * 1e9), frame_id=number,
                         stream_epoch="test-epoch", source_kind="hardware", binding="test-reference", values_pm=values))
        stamp += 1 / rate * (0.6 if number % 2 else 1.4) if jitter else 1 / rate
    return rows


def apply_python(operations, band=None):
    session = PulseSession(filter_band_hz=band or BAND_HZ)
    output = []
    for operation in operations:
        name = operation["action"]
        if name == "noise":
            session.begin_noise(confirmed=True)
        elif name == "push":
            session.push(operation["samples"])
        elif name == "band":
            session.set_filter_band(operation["band"])
        elif name == "pause":
            session.pause(operation["reason"])
        elif name == "stop":
            session.stop_monitor()
        elif name == "monitor":
            session.begin_monitor()
        elif name == "reset":
            session.reset()
        output.append(session.snapshot())
    return output


def apply_javascript(operations, band=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is only needed for cross-language algorithm regression")
    script = """
const fs = require('fs');
const {PulseSession} = require('./frontend/pulse_signal.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const session = new PulseSession(input.band ? {filterBandHz: input.band} : {}), output = [];
for (const op of input.operations) {
  if (op.action === 'noise') session.beginNoise({confirmed:true});
  else if (op.action === 'push') session.push(op.samples);
  else if (op.action === 'band') session.setFilterBand(op.band);
  else if (op.action === 'pause') session.pause(op.reason);
  else if (op.action === 'stop') session.stopMonitor();
  else if (op.action === 'monitor') session.beginMonitor();
  else if (op.action === 'reset') session.reset();
  output.push(session.snapshot());
}
process.stdout.write(JSON.stringify(output));
"""
    result = subprocess.run([node, "-e", script], input=json.dumps(dict(operations=operations, band=band)),
                            cwd=ROOT, text=True, encoding="utf-8", capture_output=True, check=True, timeout=60)
    return json.loads(result.stdout)


def assert_same(actual, expected, path="snapshot"):
    if type(expected) in (int, float):
        assert actual == pytest.approx(expected, rel=1e-10, abs=1e-9), path
    elif isinstance(expected, dict):
        assert set(actual) == set(expected), path
        for key, value in expected.items():
            assert_same(actual[key], value, path + "." + key)
    elif isinstance(expected, list):
        assert len(actual) == len(expected), path
        for index, value in enumerate(expected):
            assert_same(actual[index], value, f"{path}[{index}]")
    else:
        assert actual == expected, path


def test_native_default_algorithm_matches_js_noise_pulse_gap_pause_and_binding():
    noise = frames(510, jitter=True)
    contact = frames(630, start=noise[-1]["t_ns"] / 1e9 + .02, seq=511, pulse=True, jitter=True)
    gap = frames(2, start=contact[-1]["t_ns"] / 1e9 + .22, seq=1141, pulse=True)
    resumed = frames(4, start=gap[-1]["t_ns"] / 1e9 + .02, seq=1143)
    bad = deepcopy(resumed[-1])
    bad.update(seq=1147, frame_id=1147, t_ns=bad["t_ns"] + 20000000, binding="different-reference")
    operations = [dict(action="noise"), dict(action="push", samples=noise),
                  dict(action="push", samples=contact), dict(action="push", samples=[contact[-1]]),
                  dict(action="push", samples=gap), dict(action="pause", reason="测试短暂中断"),
                  dict(action="push", samples=resumed), dict(action="stop"),
                  dict(action="monitor"), dict(action="push", samples=[bad])]
    native = apply_python(operations)
    assert_same(native, apply_javascript(operations))
    assert native[1]["phase"] == "monitor"
    assert native[1]["candidateBpm"] is None
    assert native[2]["candidateBpm"] == pytest.approx(72)
    assert native[2]["weights"] == native[1]["weights"]
    assert native[3]["rejectedCount"] == native[2]["rejectedCount"] + 1
    assert native[4]["points"][-2]["breakBefore"]
    assert native[4]["points"][-1]["warmup"]
    assert native[6]["weights"] == native[1]["weights"]
    assert native[-1]["phase"] == "failed" and native[-1]["weights"] is None


def test_effective_time_and_recorded_jitter_match_js():
    fixture = json.loads((ROOT / "tests/fixtures/pulse_noise_rate_jitter.json").read_text(encoding="utf-8"))
    relative = fixture["relative_ms"]
    rows = frames(len(relative))
    for row, ms in zip(rows, relative):
        row["t_ns"] = 100000000000 + round(ms * 1e6)
    continuation = frames(40, start=rows[-1]["t_ns"] / 1e9 + .04, seq=len(rows) + 1, rate=25)
    operations = [dict(action="noise"), dict(action="push", samples=rows), dict(action="push", samples=continuation)]
    native = apply_python(operations)
    assert_same(native, apply_javascript(operations))
    assert native[1]["phase"] == "noise"
    assert native[2]["phase"] == "monitor"
    assert native[2]["noiseRawSamples"] >= 181 and native[2]["noiseFilteredSamples"] >= 126


@pytest.mark.parametrize("band", [[.5, 5], [.1, 5], [3.5, 5], [.7, 8]])
def test_configured_band_and_inclusive_source_boundary_match_js(band):
    duration = max(10.1, (3 if band[0] >= .7 else 2.1 / band[0]) + 7.1)
    noise = frames(round(duration * 50) + 1)
    limit = min(.12, .45 / band[1])
    edge = frames(1, start=noise[-1]["t_ns"] / 1e9 + limit, seq=len(noise) + 1)
    over = frames(1, start=edge[-1]["t_ns"] / 1e9 + limit + 1e-7, seq=len(noise) + 2)
    operations = [dict(action="noise"), dict(action="push", samples=noise),
                  dict(action="push", samples=edge), dict(action="push", samples=over),
                  dict(action="band", band=band), dict(action="band", band=[1, 2])]
    native = apply_python(operations, band)
    assert_same(native, apply_javascript(operations, band))
    assert native[1]["phase"] == "monitor"
    assert native[-1]["weights"] is None and native[-1]["filterBandHz"] == [1, 2]
    assert native[4] == native[3]


def test_confirmed_unloaded_fixed_center_prior_and_snapshot_isolation():
    with pytest.raises(ValueError, match="确认"):
        PulseSession().begin_noise()
    with pytest.raises(ValueError, match="无载"):
        PulseSession().begin_monitor()
    assert noise_weights([0] * 9)[CENTER_CHANNEL] == pytest.approx(5 / 9)
    assert sum(noise_weights([100, 1, 1, 1, 100, 1, 1, 1, 1])) == pytest.approx(1)
    assert noise_weights([100, 1, 1, 1, 100, 1, 1, 1, 1])[CENTER_CHANNEL] >= .5
    session = PulseSession()
    session.begin_noise(True)
    session.push(frames(510))
    state = session.snapshot()
    assert state["calibration"]["method"] == CALIBRATION_METHOD
    assert state["filterConfigSchema"] == FILTER_CONFIG_SCHEMA
    frozen = deepcopy(state)
    state["weights"][0] = 999
    state["points"][0]["value"] = 999
    state["calibration"]["filterBandHz"][0] = 999
    assert session.snapshot() == frozen


@pytest.mark.parametrize("band", [[1, 1], [.01, 3], [.7, 9], [float("nan"), 3], [True, 3]])
def test_invalid_filter_cannot_mutate_session(band):
    session = PulseSession()
    before = session.snapshot()
    with pytest.raises(ValueError, match="带通"):
        session.set_filter_band(band)
    assert session.snapshot() == before


def test_long_noise_interruption_invalidates_incomplete_unloaded_capture():
    rows = frames(200)
    operations = [dict(action="noise"), dict(action="push", samples=rows),
                  dict(action="push", samples=frames(1, start=105, seq=201))]
    native = apply_python(operations)
    assert_same(native, apply_javascript(operations))
    assert native[-1]["phase"] == "failed"
    assert native[-1]["weights"] is None and native[-1]["candidateBpm"] is None


def test_integral_float_source_identity_matches_javascript_number_rules():
    samples = frames(5)
    for index, sample in enumerate(samples):
        sample["stream_epoch"] = 1.0 if index % 2 else 1
        if index % 2:
            for key in ("t_ns", "seq", "frame_id"):
                sample[key] = float(sample[key])
    duplicate = deepcopy(samples[-1])
    duplicate["frame_id"] = float(duplicate["frame_id"])
    operations = [dict(action="noise"), dict(action="push", samples=samples),
                  dict(action="push", samples=[duplicate])]
    native = apply_python(operations)
    assert_same(native, apply_javascript(operations))
    assert native[-1]["phase"] == "noise" and native[-1]["rejectedCount"] == 1
