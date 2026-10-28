"""Synthetic capture lifecycle checks for nine-channel derived-source storage."""
from copy import deepcopy
import json
import time

import pytest

from ninefbg.drift_features import make_drift_frame
from ninefbg.experiment import _settings
from ninefbg.preferences import Preferences
from test_experiment import clock, context, result, spectrum, sync
from test_force_target_wrench import start_target, recover_to_saving, tick


def fitted(raw):
    value=result(raw['host_rx_monotonic_ns'],raw['frame_id'])
    for index,peak in enumerate(sorted(value['peaks'],key=lambda row:row['id'])):
        peak['center_nm']=1510.5+index
        peak['value_frame_id']=raw['frame_id']
    value['drift_features']=make_drift_frame(value,raw)
    return value


def test_input_mode_strict_and_preference_explicit(tmp_path):
    with pytest.raises(ValueError,match='输入'):_settings({'input_kind':'unknown'})
    with pytest.raises(ValueError,match='手动结束'):_settings({'input_kind':'delta_lambda_9','acquisition_protocol':'timed_wrench_v1'})
    preferences=Preferences(tmp_path)
    preferences.update('analysis',{'method':'drift_gru','input_kind':'delta_lambda_9'})
    assert Preferences(tmp_path).snapshot()['values']['analysis']['input_kind']=='delta_lambda_9'
    preferences.update('analysis',{'method':'spectral_gru','input_kind':'delta_S'})
    assert Preferences(tmp_path).snapshot()['values']['analysis']['method']=='spectral_gru'


def test_two_second_raw_baseline_waits_for_real_nine_center_rows(tmp_path,clock):
    manager=start_target(tmp_path,clock,input_kind='delta_lambda_9')
    try:
        manager.trial('start',context(clock.now));manager.training_pause_acknowledged()
        sources=[]
        for number in range(500,551):
            tick(manager,clock,number);sources.append(spectrum(clock.now,number))
        assert manager.phase=='preload'  # 2 s raw alone cannot certify 9-D input.
        for raw in sources[:11]:
            manager.on_result(fitted(raw),sync(raw['frame_id'],raw['frame_id'],[0,0,-1,0,0,0]))
        assert manager.phase=='preload'
        raw=sources[11]
        manager.on_result(fitted(raw),sync(raw['frame_id'],raw['frame_id'],[0,0,-1,0,0,0]))
        assert manager.phase=='waiting_contact'
        prompt=manager._timed['trial']['press_prompt_ns']
        assert prompt>=sources[-1]['host_rx_monotonic_ns']
        assert manager.source_annotation(raw['host_rx_monotonic_ns'])['phase']=='preload'
        assert manager.source_annotation(raw['host_rx_monotonic_ns'])['press_prompt_monotonic_ns']==prompt
    finally:manager.stop()


def test_late_fitted_rows_have_original_trial_and_second_durable_checkpoint(tmp_path,clock):
    manager=start_target(tmp_path,clock,input_kind='delta_lambda_9')
    try:
        manager.trial('start',context(clock.now));manager.training_pause_acknowledged()
        for number in range(500,551):
            tick(manager,clock,number)
            raw=spectrum(clock.now,number)
            manager.on_result(fitted(raw),sync(number,number,[0,0,-1,0,0,0]))
        assert manager.phase=='waiting_contact'
        trial=manager.trial_id
        late=None
        for number in range(551,572):
            tick(manager,clock,number,[4.,0,0,0,0,0])
            late=spectrum(clock.now,number)
        assert manager.snapshot()['guided']['can_finish']
        clock.advance(1)  # End command occurs after the already acquired source.
        manager.trial('finish',context(clock.now))
        number=recover_to_saving(manager,clock,572)
        assert manager.phase=='saving'
        state=manager._timed['trial'];original=state['checkpoint_token']
        assert not manager.drift_results_checkpoint_ready()
        assert not state.get('drift_checkpoint_token')
        # Delayed fit arrives while saving, with its original loading identity.
        manager.on_result(fitted(late),sync(late['frame_id'],late['frame_id'],[4.,0,-1,0,0,0]))
        assert manager.source_annotation(late['host_rx_monotonic_ns'])['trial_id']==trial
        raw=spectrum(clock.now,number-1)
        manager.on_result(fitted(raw),sync(raw['frame_id'],raw['frame_id'],[0,0,-1,0,0,0]))
        manager.drift_results_checkpoint_ready()
        assert not state['frozen_quality']['accepted']
        assert state['frozen_quality']['metrics']['nine_fbg_contact_windows']==0
        assert any('训练窗口' in reason for reason in state['frozen_quality']['reasons'])
        assert state['checkpoint_token']==original+':drift'
        deadline=time.monotonic()+3
        while not manager.drift_results_checkpoint_ready() and time.monotonic()<deadline:time.sleep(.005)
        assert manager.drift_results_checkpoint_ready()
        disk=[json.loads(line) for line in (manager.writer.path/'results.jsonl').read_text(encoding='utf-8').splitlines()]
        saved=next(row for row in disk if row['frame_id']==late['frame_id'])
        assert saved['trial_id']==trial and saved['phase'] in {'loading','release'}
        assert saved['result']['drift_features']['all_valid']
        assert manager.recording_checkpoint_acknowledged()
        assert manager.trial_id is None
        assert manager._trials[trial]['checkpoint_token']==original+':drift'
    finally:manager.stop()


def test_stop_cutoff_preserves_accepted_fits_without_recording_later_sources(tmp_path,clock):
    manager=start_target(tmp_path,clock,input_kind='delta_lambda_9')
    raw=spectrum(clock.advance(40),900)
    manager.on_spectrum(raw)
    try:
        target=manager.begin_result_drain()
        assert target==raw['host_rx_monotonic_ns']
        later=spectrum(clock.advance(40),901)
        assert manager.on_spectrum(later) is False
        assert manager.on_result(fitted(later),{'valid':False}) is False
        assert manager.on_result(fitted(raw),{'valid':False})
        assert manager._drift_result_watermark_ns==target
    finally:manager.stop()


def test_derived_overflow_marks_original_source_trial_bad(tmp_path,clock):
    manager=start_target(tmp_path,clock,input_kind='delta_lambda_9')
    try:
        manager.trial('start',context(clock.now));manager.training_pause_acknowledged()
        raw=spectrum(clock.advance(40),500);manager.on_spectrum(raw)
        trial=manager.trial_id
        manager.drift_processing_overflow(raw)
        assert manager._trials[trial]['bad']
        assert any('队列溢出' in reason for reason in manager._timed['trial']['faults'])
        assert not manager.snapshot()['guided']['can_finish']
    finally:manager.stop()


def test_drain_new_arrivals_never_enter_fit_queue_or_mark_overflow(tmp_path,clock):
    import queue
    from ninefbg.service import AcquisitionService, DisplayWriter
    service=AcquisitionService(tmp_path,tmp_path)
    manager=start_target(tmp_path,clock,input_kind='delta_lambda_9')
    service.experiment=manager
    service.mode='hardware';service.writer=DisplayWriter()
    service.frames=queue.Queue(maxsize=2)
    service.metrics={'received':0,'identical_payloads':0,'display_skipped':0,'max_queue_depth':0,
                     'dropped':0,'duplicates':0}
    accepted=spectrum(clock.advance(40),900)
    manager.on_spectrum(accepted)
    manager.begin_result_drain()
    service.frames.put((accepted,clock.now))
    before=deepcopy(service.frames.queue)
    try:
        for number in range(901,951):service._receive(spectrum(clock.advance(40),number))
        assert list(service.frames.queue)==list(before)
        assert service.metrics['display_skipped']==50
        assert service.metrics['dropped']==0 and manager.error is None
        assert not any('溢出' in row.get('reason','') for row in service.writer.events)
    finally:
        manager.stop();service.analysis.close()


def test_late_invalid_preload_is_rejected_before_saved_round_can_be_accepted(tmp_path,clock):
    manager=start_target(tmp_path,clock,input_kind='delta_lambda_9')
    try:
        manager.trial('start',context(clock.now));manager.training_pause_acknowledged()
        pending=[]
        for number in range(500,551):
            tick(manager,clock,number);pending.append(spectrum(clock.now,number))
        for raw in pending[:12]:manager.on_result(fitted(raw),sync(raw['frame_id'],raw['frame_id'],[0,0,-1,0,0,0]))
        assert manager.phase=='waiting_contact'
        trial=manager.trial_id
        for raw in pending[12:]:
            value=fitted(raw)
            if raw is pending[-1]:
                value['drift_features'].update(all_valid=False,centers_nm=[None]*9,valid_channels=[False]*9)
            manager.on_result(value,sync(raw['frame_id'],raw['frame_id'],[0,0,-1,0,0,0]))
        assert manager._drift_preload_frames[trial]==[]
        for number in range(551,573):
            tick(manager,clock,number,[4.,0,0,0,0,0]);raw=spectrum(clock.now,number)
            manager.on_result(fitted(raw),sync(number,number,[4.,0,-1,0,0,0]))
        clock.advance(1);manager.trial('finish',context(clock.now))
        number=recover_to_saving(manager,clock,573)
        raw=spectrum(clock.now,number-1);manager.on_result(fitted(raw),sync(raw['frame_id'],raw['frame_id'],[0,0,-1,0,0,0]))
        manager.drift_results_checkpoint_ready()
        assert not manager._timed['trial']['frozen_quality']['accepted']
        assert any('最终采前' in reason for reason in manager._timed['trial']['frozen_quality']['reasons'])
        assert manager._drift_contact_windows[trial]>0
    finally:manager.stop()


def test_first_inference_reference_initializes_real_fixed_peak_ids_after_connect(tmp_path,clock,monkeypatch):
    from ninefbg.service import AcquisitionService,DisplayWriter
    from ninefbg.tracking import PeakTracker
    from ninefbg.drift_features import make_drift_frame
    from ninefbg.spectral_runtime import signature
    from test_tracking import make_frame
    service=AcquisitionService(tmp_path,tmp_path)
    service.tracker=PeakTracker({'stable_frames':4})
    service.running=True;service.mode='hardware';service.writer=DisplayWriter()
    raw=make_frame(0,source_kind='hardware',noise=.05)
    raw.update(time_quality='host_rx_approx',host_rx_monotonic_ns=clock.now)
    first=service.tracker.process(raw)
    assert not service.tracker.status()['initialized']
    assert any(peak.get('id') is None for peak in first['peaks'])
    metadata={'model_kind':'spectral_gru','method':'drift_gru','input_kind':'delta_lambda_9',
              'wavelength_nm':raw['wavelength_nm'],'preprocessing':raw['preprocessing'],
              'trained_point_ids':['P01'],'model_sha256':'a'*64,
              'peak_definition':{'algorithm_version':service.tracker.status()['algorithm_version'],
                                 'config_version':service.tracker.config_version,
                                 'axis_sha256':signature(raw['wavelength_nm']),
                                 'preprocessing':raw['preprocessing'],'feature_schema':'nine_fbg_centers_v1'}}
    runtime=service.analysis.spectral_runtime
    runtime._predictor=lambda *args:([1.]+[0.]*24,[.3,0.,0.,0.,0.,0.])
    runtime.apply(metadata,object());service.analysis.bundle=metadata;service.analysis.inference_active=True
    def state():
        return {'frame':raw,'tracker':service.tracker.status(),'force':{},'force_connected':False}
    monkeypatch.setattr(service,'snapshot',state)
    try:
        service.analysis_reference(True)
        assert service.tracker.status()['reference_pending']
        assert runtime.snapshot()['reference_pending']
        for number in range(1,65):
            raw=make_frame(number,source_kind='hardware',noise=.05)
            raw.update(time_quality='host_rx_approx',host_rx_monotonic_ns=clock.advance(40))
            result=service.tracker.process(raw)
            service._freeze_independent_reference(result,raw)
            service.analysis.on_result(make_drift_frame(result,raw))
        snapshot=runtime.snapshot()
        assert service.tracker.status()['initialized'] and service.tracker.status()['reference_frozen']
        assert snapshot['reference'] is not None and not snapshot['reference_pending']
        assert snapshot['reference']['fbg_ids']==[f'FBG{i}' for i in range(1,10)]
        assert snapshot['reference']['source_timestamps_ns'][-1]-snapshot['reference']['source_timestamps_ns'][0]>=2_000_000_000
    finally:service.analysis.close()
