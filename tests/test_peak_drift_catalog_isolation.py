"""A read-only model listing need not wait behind a device command."""
from pathlib import Path
import threading

import pytest

from ninefbg.service import AcquisitionService
from ninefbg.model_history import _listing_signature


def test_model_listing_completes_while_device_command_is_pending(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    result, errors = [], []
    def pending_device_command():
        with service.command_lock:
            entered.set()
            release.wait(3)
    def read_catalog():
        try: result.append(service.models_snapshot())
        except BaseException as exc: errors.append(exc)
        finally: completed.set()
    command = threading.Thread(target=pending_device_command)
    reader = threading.Thread(target=read_catalog)
    command.start()
    try:
        assert entered.wait(1)
        reader.start()
        assert completed.wait(1), "只读模型列表不应等待设备命令锁"
        assert not errors and result[0] == {"models": [], "deleted_models": []}
    finally:
        release.set(); command.join(3)
        if reader.ident: reader.join(3)
        service.analysis.close()


@pytest.mark.parametrize("name", ["optical_sources.jsonl", "peak_frames.jsonl"])
def test_catalog_fingerprint_tracks_compact_source_file_removal(tmp_path, name):
    session = tmp_path/"calibration_sessions"/"compact_session"
    session.mkdir(parents=True)
    source = session/name
    source.write_text("{}\n", encoding="utf-8")
    before = _listing_signature(tmp_path)
    # Content edits are not catalog attestations; replacement/removal or link
    # identities are. Removing a compact source must invalidate the cache.
    source.unlink()
    assert _listing_signature(tmp_path) != before
