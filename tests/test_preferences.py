import json

import pytest

from ninefbg.preferences import Preferences
from test_server import local_server, post_json
import urllib.error
import urllib.request


def test_settings_survive_new_process_without_restoring_measurement_state(tmp_path):
    userdata = tmp_path / "not-created"
    store = Preferences(userdata)
    assert store.snapshot()["values"] == {} and not userdata.exists()
    store.update("connection", {"source_mode": "hardware", "integration_us": 40, "sample_rate_hz": 50})
    store.update("experiment", {"finger_id": "实际样件 01", "batch": "B", "force_sign": -1,
                               "repetitions_per_point": 7, "selected_point_id": "P21", "fixation": "原装夹条件"})
    store.update("display", {"spectrum_mode": "manual", "spectrum_x_min": 1510.1, "spectrum_x_max": 1580.8,
                             "show_windows": False})
    restored = Preferences(userdata)
    assert restored.snapshot() == store.snapshot()
    assert restored.values["experiment"]["repetitions_per_point"] == 7
    assert restored.values["experiment"]["finger_id"] == "实际样件 01"
    assert restored.values["display"]["show_windows"] is False
    for section, patch in (("experiment", {"unloaded_confirmed": True}),
                           ("connection", {"auto_connect": True}),
                           ("display", {"force_zero": [0] * 6}),
                           ("experiment", {"reference_version": 3})):
        with pytest.raises(ValueError, match="确认不能持久化"):
            restored.update(section, patch)
    assert restored.snapshot() == store.snapshot()


@pytest.mark.parametrize("section,patch", [
    ("connection", {"integration_us": True}), ("connection", {"sample_rate_hz": 30}),
    ("experiment", {"repetitions_per_point": 2}), ("experiment", {"selected_point_id": "P26"}),
    ("experiment", {"force_sign": 0}), ("display", {"spectrum_x_min": float("nan")}),
])
def test_invalid_preferences_do_not_create_userdata(tmp_path, section, patch):
    store = Preferences(tmp_path / "settings-only")
    with pytest.raises(ValueError):
        store.update(section, patch)
    assert not store.path.parent.exists()


def test_atomic_save_failure_preserves_previous_file_and_memory(tmp_path, monkeypatch):
    store = Preferences(tmp_path)
    store.update("experiment", {"repetitions_per_point": 4})
    original = store.path.read_bytes()
    monkeypatch.setattr("ninefbg.preferences.os.replace", lambda *args: (_ for _ in ()).throw(OSError("测试写盘失败")))
    with pytest.raises(OSError):
        store.update("experiment", {"repetitions_per_point": 5})
    assert store.path.read_bytes() == original
    assert store.values["experiment"]["repetitions_per_point"] == 4
    assert not list(tmp_path.glob(".settings-*.tmp"))


def test_data_id_text_survives_reload_without_converting_historical_tip_diameter(tmp_path):
    store = Preferences(tmp_path)
    store.update("experiment", {"finger_id": "样件-6", "tip_diameter_mm": 2})
    restored = Preferences(tmp_path)
    assert "data_id" not in restored.values["experiment"]
    entered = "  数据-0007  "
    restored.update("experiment", {"data_id": entered})
    values = Preferences(tmp_path).values["experiment"]
    assert values["data_id"] == entered
    assert values["finger_id"] == "样件-6" and values["tip_diameter_mm"] == 2
    before = restored.path.read_bytes()
    for invalid in (7, None, True, "x" * 129):
        with pytest.raises(ValueError, match="data_id"):
            restored.update("experiment", {"data_id": invalid})
        assert restored.path.read_bytes() == before
    restored.update("experiment", {"data_id": ""})
    assert Preferences(tmp_path).values["experiment"]["data_id"] == ""


def test_unreadable_settings_are_reported_and_preserved(tmp_path):
    file = tmp_path / "settings.json"
    file.write_text("not-json", encoding="utf-8")
    store = Preferences(tmp_path)
    assert store.snapshot()["error"] and store.values == {}
    with pytest.raises(ValueError, match="读取失败"):
        store.update("experiment", {"repetitions_per_point": 4})
    assert file.read_text(encoding="utf-8") == "not-json"


def test_local_settings_api_persists_without_connecting_hardware(tmp_path):
    with local_server() as (_, service, url):
        service.preferences = Preferences(tmp_path / "settings-only")
        with urllib.request.urlopen(url + "/api/settings") as response:
            assert json.load(response)["values"] == {}
        assert not service.preferences.path.parent.exists()
        with post_json(url, "/api/settings", {"section": "experiment", "values": {"repetitions_per_point": 8, "data_id": "0008"}}) as response:
            assert json.load(response)["values"]["experiment"]["repetitions_per_point"] == 8
        request = urllib.request.Request(url + "/api/settings", data=b'{}',
                                         headers={"Content-Type": "application/json", "Origin": "https://external.invalid"})
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(request)
        assert info.value.code == 403
        assert service.connects == service.force_connects == 0
        assert Preferences(service.preferences.path.parent).values["experiment"]["repetitions_per_point"] == 8
        assert Preferences(service.preferences.path.parent).values["experiment"]["data_id"] == "0008"
