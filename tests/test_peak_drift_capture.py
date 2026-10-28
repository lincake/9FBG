"""Compact optical records use real temporary writers; no hardware qualification."""
from copy import deepcopy
import hashlib
import json
import time

import pytest

from ninefbg.drift_features import drift_pm
from ninefbg.experiment import _settings, get_session, list_sessions
from ninefbg.peak_drift_capture import OPTICAL_SCHEMA, RECORDING_SCHEMA, optical_source, project
from ninefbg.preferences import Preferences
from ninefbg.storage import SessionWriter
from test_experiment import clock, context, settings, spectrum, sync
from test_force_target_wrench import start_target, tick
from test_drift_capture_integration import fitted


def disk_rows(manager, name):
    return [json.loads(line) for line in (manager.writer.path/(name+'.jsonl')).read_text(encoding='utf-8').splitlines()]


def assert_compact(value):
    if isinstance(value, dict):
        assert not {'intensity', 'S0', 'fit_x', 'fit_y', 'wavelength_nm'} & value.keys()
        for child in value.values():
            assert_compact(child)
    elif isinstance(value, list):
        assert len(value) != 512
        for child in value:
            assert_compact(child)


def record_result(manager, raw, force_values=None, shift=0):
    value = fitted(raw)
    for peak in value['peaks']:
        peak['center_nm'] += shift
    from ninefbg.drift_features import make_drift_frame
    value['drift_features'] = make_drift_frame(value, raw)
    # Fitted curves exist upstream but must not be copied to compact evidence.
    value.update(fit_x=list(range(512)), fit_y=list(range(512)))
    assert manager.on_result(value, sync(raw['frame_id'], raw['frame_id'], force_values or [0,0,-1,0,0,0]))


def test_modes_are_explicit_independent_and_historical_api_retained(tmp_path):
    old = _settings(settings(acquisition_protocol='force_target_wrench_v1', input_kind='delta_lambda_9'))
    assert 'capture_mode' not in old and old['input_kind']=='delta_lambda_9'
    full = _settings(settings(acquisition_protocol='force_target_wrench_v1', capture_mode='full_spectrum'))
    compact = _settings(settings(acquisition_protocol='force_target_wrench_v1', capture_mode='peak_drift'))
    assert full['input_kind']=='delta_S' and compact['input_kind']=='delta_lambda_9'
    for mode, kind in [('full_spectrum','delta_lambda_9'), ('peak_drift','delta_S')]:
        with pytest.raises(ValueError, match='不一致'):
            _settings(settings(capture_mode=mode, input_kind=kind))
    for mode in ['unknown', [], None]:
        with pytest.raises(ValueError, match='模式'):
            _settings(settings(capture_mode=mode))
    with pytest.raises(ValueError, match='手动结束'):
        _settings(settings(capture_mode='peak_drift', acquisition_protocol='timed_wrench_v1'))
    pref=Preferences(tmp_path)
    pref.update('experiment', {'capture_mode':'peak_drift'})
    assert Preferences(tmp_path).snapshot()['values']['experiment']['capture_mode']=='peak_drift'


def test_source_attestation_keeps_original_identity_and_digest_without_optical_vectors():
    raw=spectrum(10_000_000_000, 17);raw['valid']=False
    before=deepcopy(raw)
    value=optical_source(raw, {'trial_id':'T0001', 'baseline_id':'B0001','phase':'preload'})
    assert value['schema']==OPTICAL_SCHEMA and value['source_valid'] is True
    assert value['valid'] is False and len(value['spectrum_sha256'])==64
    assert value['frame_id']==17 and value['trial_id']=='T0001'
    assert_compact(value)
    assert raw==before
    raw['intensity'][0]=float('nan')
    invalid=optical_source(raw, {})
    assert invalid['source_valid'] is False and invalid['spectrum_sha256'] is None


def test_writer_defensively_projects_nested_metadata_events_final_and_rejects_full_rows(tmp_path):
    raw=spectrum(10_000_000_000)
    old_reference={'S0':raw['intensity'], 'wavelength_nm':raw['wavelength_nm'], 'force_zero':[0]*6}
    writer=SessionWriter(tmp_path, {'recording_schema':RECORDING_SCHEMA,
                                 'static_wavelength_nm':raw['wavelength_nm'], 'acquisition_audit':{'nested':old_reference}})
    with pytest.raises(ValueError):writer.submit('raw_spectra',raw)
    with pytest.raises(ValueError):writer.submit('results', {'result':raw})
    writer.submit('events', {'event':'reference', 'nested':old_reference, 'fit_x':[1]*512})
    writer.close({'baseline':old_reference, 'nested':[{'fitted_y':[1]*512}]})
    manifest=json.loads((writer.path/'manifest.json').read_text(encoding='utf-8'))
    axis=manifest.pop('static_wavelength_nm')
    assert axis==raw['wavelength_nm']
    assert_compact(manifest)
    assert_compact(json.loads((writer.path/'events.jsonl').read_text(encoding='utf-8')))
    assert manifest['complete'] and list(manifest['files'])==['events.jsonl']


def test_projection_retains_exactly_512_trials_events_and_source_identities():
    value={'trials':[{'trial_id':'T%04d'%i} for i in range(512)],
           'events':[{'event':'test','source_frame_ids':list(range(512))} for _ in range(512)],
           'source_stamps_ns':list(range(512)), 'frame_ids':list(range(512)),
           'unknown_optical_vector':[3.2]*512, 'fit_y':[3.2]*512}
    projected=project(value)
    assert len(projected['trials'])==len(projected['events'])==512
    assert projected['source_stamps_ns']==projected['frame_ids']==list(range(512))
    assert projected['events'][0]['source_frame_ids']==list(range(512))
    assert 'unknown_optical_vector' not in projected and 'fit_y' not in projected


def test_real_recorder_saves_nine_curves_fixed_reference_and_direct_zero(tmp_path,clock):
    manager=start_target(tmp_path,clock,capture_mode='peak_drift')
    manager.trial('start',context(clock.now));manager.training_pause_acknowledged()
    trial=manager.trial_id
    try:
        for number in range(500,551):
            tick(manager,clock,number)
            record_result(manager,spectrum(clock.now,number),shift=(number-500)*.000001)
        assert manager.phase=='waiting_contact'
        for number in range(551,576):
            tick(manager,clock,number,[4.,0,0,0,0,0])
            record_result(manager,spectrum(clock.now,number),[4.,0,-1,0,0,0],shift=.002)
        reference=deepcopy(manager._trials[trial]['drift_reference'])
        assert len(reference['source_frame_ids'])>=12
        assert reference['effective_ns']==manager._timed['trial']['press_prompt_ns']
        assert manager.snapshot()['guided']['can_finish']
        clock.advance(1);manager.trial('finish',context(clock.now))
        assert '六轴软件零点' in manager.snapshot()['guided']['message']
        assert '全谱' not in manager.snapshot()['guided']['message']
        tick(manager,clock,576)
        record_result(manager,spectrum(clock.now,576))
        assert manager.phase=='saving'
        deadline=time.monotonic()+5
        while not manager.drift_results_checkpoint_ready() and time.monotonic()<deadline:
            time.sleep(.005)
        assert manager.drift_results_checkpoint_ready()
        assert manager.recording_checkpoint_acknowledged()
        assert manager._trials[trial]['status']=='completed'
        assert manager._trials[trial]['drift_reference']==reference
        assert manager.baseline['force_zero']==[0,0,-1,0,0,0]
        assert '重新记录 2 秒无载九路参考' in manager.snapshot()['guided']['message']
    finally:
        manager.stop()
    manifest=get_session(tmp_path,manager.session_id)
    assert manifest['complete'] and manifest['recording_schema']==RECORDING_SCHEMA
    assert set(manifest['files'])=={'optical_sources.jsonl','raw_force.jsonl','peak_frames.jsonl','events.jsonl'}
    axis=manifest.pop('static_wavelength_nm')
    assert len(axis)==512
    assert_compact(manifest)
    for name, expected in manifest['files'].items():
        data=(manager.writer.path/name).read_bytes()
        assert hashlib.sha256(data).hexdigest()==expected
        for line in data.decode('utf-8').splitlines():assert_compact(json.loads(line))
    peaks=disk_rows(manager,'peak_frames')
    first=next(row for row in peaks if row['frame_id']==500)
    loaded=next(row for row in peaks if row['frame_id']==551)
    assert first['delta_lambda_pm'] is None and first['drift_reference_id'] is None
    assert loaded['drift_reference_id']==reference['reference_id']
    assert loaded['delta_lambda_pm']==pytest.approx(drift_pm(loaded['result']['drift_features'],reference))
    summary=list_sessions(tmp_path)[0]
    assert summary['drift_available'] and not summary['spectral_available']
    assert summary['counts']['peak_frames']==len(peaks)


def test_explicit_full_spectrum_recorder_preserves_original_arrays(tmp_path,clock):
    manager=start_target(tmp_path,clock,capture_mode='full_spectrum')
    tick(manager,clock,900)
    raw=spectrum(clock.now,900)
    record_result(manager,raw)
    manager.stop()
    manifest=get_session(tmp_path,manager.session_id)
    assert 'recording_schema' not in manifest and 'static_wavelength_nm' not in manifest
    assert set(manifest['files'])=={'raw_spectra.jsonl','raw_force.jsonl','results.jsonl','events.jsonl'}
    saved=next(row for row in disk_rows(manager,'raw_spectra') if row['frame_id']==900)
    assert saved['intensity']==raw['intensity'] and saved['wavelength_nm']==raw['wavelength_nm']
    assert len(manifest['final']['baseline_history'][0]['spectral_reference']['S0'])==512
    assert len(disk_rows(manager,'results')[-1]['result']['fit_x'])==512


def test_delayed_preload_uses_final_source_tail_not_first_twelve_fits(tmp_path,clock):
    manager=start_target(tmp_path,clock,capture_mode='peak_drift')
    manager.trial('start',context(clock.now));manager.training_pause_acknowledged()
    try:
        pending=[]
        for number in range(500,551):
            tick(manager,clock,number);pending.append(spectrum(clock.now,number))
        for raw in pending[:12]:record_result(manager,raw)
        assert manager.phase=='waiting_contact'
        assert not manager._trials[manager.trial_id].get('drift_reference')
        for raw in pending[12:]:record_result(manager,raw,shift=.004)
        tick(manager,clock,551)
        record_result(manager,spectrum(clock.now,551),shift=.005)
        reference=manager._trials[manager.trial_id]['drift_reference']
        assert len(reference['source_frame_ids'])>=50
        assert reference['lambda0_nm'][0]==pytest.approx(1510.504)
    finally:manager.stop()
