"""Native display tests use raw replay-like fixtures and never open hardware."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy
import time

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from ninefbg.qt_display import DisplayZero, LivePage, normalize_force, normalize_peaks, validate_spectrum
from ninefbg.qt_widgets import TrendPlot


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class Context(QObject):
    error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.preferences = {}
        self.calls = []
        self.errors = []
        self.error.connect(self.errors.append)

    def execute(self, action, payload=None):
        self.calls.append((action, payload))


def sample(frame_id=1, stamp=1_000_000_000):
    # Deliberately nonuniform calibration values: a uniform/index axis is wrong.
    axis = [1510 + index * .031 + index * index * .000001 for index in range(512)]
    frame = {"wavelength_nm": axis, "intensity": [10 + index % 45 for index in range(512)],
             "frame_id": frame_id, "host_rx_monotonic_ns": stamp, "stream_epoch": "optical-epoch"}
    peaks = [{"id": f"FBG{index + 1}", "valid": True, "stale": False, "center_nm": axis[40 + index * 45],
              "drift_nm": (index + 1) * .001, "amplitude": 50, "fwhm_nm": .1,
              "prominence": 30, "residual": .01, "value_frame_id": frame_id,
              "fit_x": axis[40 + index * 45:43 + index * 45], "fit_y": [25, 50, 25]}
             for index in range(9)]
    force = {"force_values": [1., 2., 3., .1, .2, .3], "axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"],
             "units": ["N", "N", "N", "N·m", "N·m", "N·m"], "frame_id": frame_id,
             "stream_epoch": "force-epoch", "host_rx_monotonic_ns": stamp, "age_ms": 0}
    return {"running": True, "recording": False, "mode": "replay", "force_connected": True,
            "frame": frame, "force": force, "metrics": {}, "tracker": {
                "peaks": peaks, "reference_version": 1, "reference_nm": [p["center_nm"] - p["drift_nm"] for p in peaks],
                "stream_epoch": frame["stream_epoch"], "frame_id": frame_id, "host_rx_monotonic_ns": stamp,
                "algorithm_version": "test-definition", "config_version": "test-config", "display_age_ms": 0}}


@pytest.fixture
def page(qapp):
    widget = LivePage(Context())
    widget.timer.stop()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def test_actual_nonuniform_calibrated_axis_and_fixed_fit_ids(page):
    state = sample()
    original = deepcopy(state)
    page.update_state(state)
    assert page.spectrum.series[0]["x"] == state["frame"]["wavelength_nm"]
    assert page.spectrum.data_ranges()[0] == (state["frame"]["wavelength_nm"][0], state["frame"]["wavelength_nm"][-1])
    assert [series["name"] for series in page.spectrum.series[1:]] == [f"FBG{i}" for i in range(1, 10)]
    assert page.peak_table.item(0, 2).text() == "1.00"  # .001 nm = 1 pm
    assert "nm" in page.spectrum.x_label
    assert state == original  # Presentation never mutates acquisition data.


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reversed", "nonfinite", "bool"])
def test_spectrum_rejects_bad_actual_axis(mutation):
    frame = sample()["frame"]
    if mutation == "missing":
        frame["wavelength_nm"].pop()
    elif mutation == "duplicate":
        frame["wavelength_nm"][20] = frame["wavelength_nm"][19]
    elif mutation == "reversed":
        frame["wavelength_nm"].reverse()
    elif mutation == "nonfinite":
        frame["intensity"][10] = float("nan")
    else:
        frame["wavelength_nm"][10] = True
    assert validate_spectrum(frame)["valid"] is False


def test_partial_peaks_keep_identity_and_hide_invalid_cached_values(page):
    state = sample()
    invalid = {**state["tracker"]["peaks"][0], "valid": False, "stale": False}
    state["tracker"]["peaks"] = [state["tracker"]["peaks"][7], invalid]
    page.update_state(state)
    assert page.peak_table.item(0, 0).text() == "FBG1"
    assert page.peak_table.item(0, 1).text() == "—"
    assert page.peak_table.item(1, 0).text() == "FBG2"
    assert page.peak_table.item(1, 1).text() == "—"
    assert page.peak_table.item(7, 0).text() == "FBG8"
    assert page.peak_table.item(7, 1).text() != "—"


def test_duplicate_identity_is_not_promoted_to_another_fbg():
    peaks = normalize_peaks([{"id": "FBG8", "valid": True, "center_nm": 1551},
                             {"id": "FBG8", "valid": True, "center_nm": 1552}])
    assert peaks[7]["center_nm"] == 1551
    assert sum(row["valid"] for row in peaks) == 1
    assert [row["id"] for row in peaks] == [f"FBG{i}" for i in range(1, 10)]


def test_stopped_or_aged_snapshot_marks_peaks_old_and_clears_force(page):
    page.update_state(sample())
    assert page.force_value_labels[0].text() == "1.000"
    page.received_at = time.monotonic() - 1.1
    page._tick()
    assert "旧值" in page.peak_table.item(0, 7).text()
    assert page.peak_table.item(0, 1).text() != "—"
    assert page.force_value_labels[0].text() == "—"
    assert "旧帧" in page.spectrum.series[0]["name"]
    assert len(page.spectrum.series) == 1  # No valid-looking fit overlay on an old spectrum.


def test_force_freshness_and_units_are_independent_of_optical(page):
    state = sample()
    state["force"]["age_ms"] = 251
    page.update_state(state)
    assert page.force_value_labels[2].text() == "—"
    assert "250 ms" in page.force_status.text()
    assert "有效" in page.peak_table.item(2, 7).text()
    force = sample()["force"]
    force["axes"] = list(reversed(force["axes"]))
    force["units"] = list(reversed(force["units"]))
    force["force_values"] = list(reversed(force["force_values"]))
    assert normalize_force(force)["values"] == [1, 2, 3, .1, .2, .3]
    force["units"][0] = "N"
    assert normalize_force(force)["valid"] is False


def test_manual_display_range_cannot_replace_or_exceed_calibration(page):
    state = sample()
    page.update_state(state)
    page.range_mode.setCurrentIndex(1)
    page.range_inputs["spectrum_x_min"].setText("0")
    page._apply_range()
    assert "实际标定轴内" in page.ctx.errors[-1]
    assert page.spectrum.series[0]["x"] == state["frame"]["wavelength_nm"]
    page.range_inputs["spectrum_x_min"].setText("1511")
    page.range_inputs["spectrum_x_max"].setText("1520")
    page._apply_range()
    assert page.spectrum.data_ranges()[0] == (1511, 1520)
    assert page.ctx.calls[-1][0] == "settings"
    assert page.ctx.calls[-1][1]["values"]["spectrum_mode"] == "manual"


def test_restored_manual_range_is_checked_against_current_axis(page):
    page.set_preferences({"display": {"spectrum_mode": "manual", "spectrum_x_min": 1200,
                                     "spectrum_x_max": 1400, "spectrum_y_min": 0, "spectrum_y_max": 100}})
    state = sample()
    page.update_state(state)
    assert page.spectrum.data_ranges()[0] == validate_spectrum(state["frame"])["x_range"]
    assert "超出当前标定轴" in page.spectrum_status.text()


def test_history_deduplicates_and_breaks_after_invalid_source(page):
    first = sample()
    page.update_state(first)
    page.update_state(first)
    assert len(page._optical_history) == 1
    assert len(page._force_history) == 1
    old = sample(2, 1_100_000_000)
    old["tracker"]["display_stale"] = True
    old["force"]["stale"] = True
    page.update_state(old)
    page.update_state(sample(3, 1_200_000_000))
    assert len(page._optical_history) == 3
    assert page._optical_history[1][1] == [None] * 9
    assert page._force_history[1][1] == [None] * 6
    assert page.wavelength_plot.series[0]["y"] == [1.0, None, 1.0]


def test_history_does_not_label_mismatched_fit_as_current(page):
    state = sample()
    state["tracker"]["frame_id"] = 0
    page.update_state(state)
    assert not page._optical_history
    assert "旧值" in page.peak_table.item(0, 7).text()
    assert len(page.spectrum.series) == 1
    state = sample()
    state["tracker"]["peaks"][0]["value_frame_id"] = 0
    page.update_state(state)
    assert page._optical_history[-1][1][0] is None
    assert "旧值" in page.peak_table.item(0, 7).text()


def test_same_frame_quality_change_breaks_only_affected_channel(page):
    page.update_state(sample())
    changed = sample()
    changed["tracker"]["peaks"][0].update(valid=False, stale=True)
    page.update_state(changed)
    page.update_state(sample(2, 1_100_000_000))
    # A line break is metadata on the real next sample, not an invented value.
    assert len(page._optical_history) == 2
    assert page.wavelength_plot.series[0]["breaks"] == [True, True]
    assert page.wavelength_plot.series[1]["breaks"] == [True, False]


def test_display_zero_requires_distinct_frames_over_400ms_and_resets():
    zero = DisplayZero()
    normalized = normalize_force(sample()["force"])
    with pytest.raises(ValueError, match="确认"):
        zero.request(False, normalized)
    zero.request(True, normalized)
    for _ in range(15):
        zero.update(normalized)
    assert len(zero.samples) == 1
    for index in range(1, 12):
        frame = sample(index + 1, 1_000_000_000 + index * 20_000_000)["force"]
        zero.update(normalize_force(frame))
    assert zero.pending and zero.offsets is None  # 12 frames at 50 Hz span only 220 ms.
    for index in range(12, 21):
        frame = sample(index + 1, 1_000_000_000 + index * 20_000_000)["force"]
        zero.update(normalize_force(frame))
    assert zero.offsets == [1., 2., 3., .1, .2, .3]
    assert zero.correct([1., 2., 3., .1, .2, .3]) == [0.] * 6
    changed = normalize_force(sample(30, 1_500_000_000)["force"])
    changed["epoch"] = "another-connection"
    zero.update(changed)
    assert zero.offsets is None and not zero.pending


def test_plot_renders_native_paths_and_preserves_data_gaps(qapp):
    plot = TrendPlot()
    plot.resize(660, 270)
    source = [{"name": "FBG1", "x": [1, 1.01, 1.03, 1.1], "y": [1, 3, None, 2], "color": "#087f6b"}]
    plot.set_series(source)
    source[0]["y"][1] = 999
    assert plot.series[0]["y"] == [1, 3, None, 2]
    assert plot.data_ranges()[0] == (1, 1.1)
    plot.set_bands([{"min": 1.03, "max": 1.05, "label": "固定窗口"}])
    plot.set_markers([{"x": 1.01, "y": 3, "label": "FBG1"}])
    assert not plot.grab().isNull()
    plot.close()
