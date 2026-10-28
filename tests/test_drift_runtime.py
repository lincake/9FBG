"""Generated nine-center sources only; never physical device qualification."""
from copy import deepcopy
import threading
import time

import pytest

from ninefbg.drift_features import make_drift_frame, make_drift_reference, drift_pm
from ninefbg.spectral_runtime import SpectralRuntime, signature


def source(number, stamp, offset=0., epoch='optical'):
    raw = {'frame_id':number, 'stream_epoch':epoch, 'host_rx_monotonic_ns':stamp,
           'source_kind':'hardware', 'time_quality':'host_rx_approx',
           'wavelength_nm':[1500.+i*.1 for i in range(512)], 'intensity':[10.]*512,
           'preprocessing':{}}
    result = {**{key:raw[key] for key in ('frame_id','stream_epoch','host_rx_monotonic_ns')},
              'algorithm_version':'fixed-test', 'config_version':'configuration-test',
              'peaks':[{'id':f'FBG{i}', 'valid':True, 'stale':False, 'value_frame_id':number,
                        'center_nm':1500.+i+offset} for i in range(1,10)]}
    return make_drift_frame(result,raw),raw


def make_runtime(predictor=None):
    calls=[]
    def predict(frames, reference, model, metadata):
        calls.append((deepcopy(frames),deepcopy(reference)))
        return [1.]+[0.]*24, [.3,0.,0.,0.,0.,0.]
    runtime=SpectralRuntime(predictor=predictor or predict)
    axis=[1500.+i*.1 for i in range(512)]
    metadata={'input_kind':'delta_lambda_9','method':'drift_gru','wavelength_nm':axis,
              'preprocessing':{},'trained_point_ids':['P01'],'model_sha256':'a'*64,
              'peak_definition':{'algorithm_version':'fixed-test','config_version':'configuration-test',
                                 'axis_sha256':signature(axis),'preprocessing':{},'feature_schema':'nine_fbg_centers_v1'}}
    runtime.apply(metadata,object())
    return runtime,calls


def wait_result(runtime,count=1):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        snap=runtime.snapshot()
        if snap['metrics']['completed']>=count:return snap
        time.sleep(.003)
    raise AssertionError(runtime.snapshot())


def test_drift_independent_reference_needs_two_seconds_and_real_fitted_sources():
    runtime,calls=make_runtime()
    now=time.perf_counter_ns()-2_100_000_000
    try:
        runtime.request_reference(True,'optical',requested_ns=now)
        for i in range(50):
            value,raw=source(i,now+i*40_000_000)
            runtime.submit(raw)
            assert len(runtime.frames)==0  # Raw spectra never become 9-D input.
            runtime.submit_result(value)
        assert runtime.snapshot()['reference'] is None
        value,_=source(50,now+2_000_000_000,.001)
        runtime.submit_result(value)
        snap=wait_result(runtime)
        assert snap['reference']['lambda0_nm']==[1500.+i for i in range(1,10)]
        assert len(snap['reference']['source_frame_ids'])==51
        assert snap['prediction']['window_frame_ids']==list(range(35,51))
        assert snap['prediction']['input_kind']=='delta_lambda_9'
        assert calls[-1][0][-1]['delta_lambda_pm']==pytest.approx([1.]*9)
        before=deepcopy(runtime.frames)
        for _ in range(20):runtime.snapshot()
        assert runtime.frames==before
        runtime.submit_result(value)
        assert runtime.snapshot()['metrics']['duplicate_identities']==1
    finally:runtime.close()


def test_trial_reference_is_median_of_source_preload_and_fixed_through_release():
    runtime,calls=make_runtime()
    start=time.perf_counter_ns()-1_000_000_000
    annotation={'session_id':'session','trial_id':'T1','phase':'preload'}
    try:
        for i in range(16):runtime.submit_result(source(i,start+i*40_000_000)[0],annotation)
        boundary=start+16*40_000_000
        runtime.submit_result(source(16,boundary,.002)[0],{**annotation,'phase':'loading','press_prompt_monotonic_ns':boundary})
        snap=wait_result(runtime)
        assert snap['reference']['trigger']=='trial_preload_unloaded'
        assert snap['reference']['effective_ns']==boundary
        assert snap['prediction']['window_frame_ids']==list(range(1,17))
        frozen=deepcopy(snap['reference'])
        runtime.submit_result(source(17,boundary+40_000_000,.003)[0],{**annotation,'phase':'release'})
        wait_result(runtime,2)
        assert runtime.snapshot()['reference']==frozen
        runtime.submit_result(source(18,boundary+80_000_000)[0],{**annotation,'trial_id':'T2','phase':'preload'})
        assert runtime.snapshot()['reference'] is None and len(runtime.frames)==0
    finally:runtime.close()


@pytest.mark.parametrize('fault',['invalid','gap','config','epoch'])
def test_invalid_or_gap_never_fills_zeros_or_keeps_old_prediction(fault):
    runtime,_=make_runtime()
    now=time.perf_counter_ns()-1_000_000_000
    preload=[source(i,now+i*40_000_000)[0] for i in range(16)]
    reference=make_drift_reference(preload,effective_ns=preload[-1]['host_rx_monotonic_ns'],reference_id='reference')
    try:
        runtime.set_reference(reference)
        for i in range(16,32):runtime.submit_result(source(i,now+i*40_000_000)[0])
        wait_result(runtime)
        value=source(32,now+32*40_000_000)[0]
        if fault=='invalid':
            value['centers_nm'][0]=None;value['valid_channels'][0]=False;value['all_valid']=False
        elif fault=='gap':value['host_rx_monotonic_ns']+=200_000_000
        elif fault=='config':value['config_version']='changed'
        elif fault=='epoch':value['stream_epoch']='changed'
        runtime.submit_result(value)
        snap=runtime.snapshot()
        assert not snap['prediction']['valid']
        assert len(runtime.frames)<=1
        assert all(row['all_valid'] for row in runtime.frames)
        if fault in {'config','epoch'}:assert snap['reference'] is None
    finally:runtime.close()


def test_reference_or_stop_cancels_inflight_drift_prediction():
    entered,release=threading.Event(),threading.Event()
    def predict(*args):
        entered.set();assert release.wait(2)
        return [1.]+[0.]*24,[.3]*6
    runtime,_=make_runtime(predict)
    now=time.perf_counter_ns()-1_000_000_000
    preload=[source(i,now+i*40_000_000)[0] for i in range(16)]
    try:
        runtime.set_reference(make_drift_reference(preload,effective_ns=preload[-1]['host_rx_monotonic_ns'],reference_id='R1'))
        for i in range(16,32):runtime.submit_result(source(i,now+i*40_000_000)[0])
        assert entered.wait(1)
        runtime.stop_inference();release.set();time.sleep(.02)
        assert runtime.snapshot()['reference'] is None
        assert not runtime.snapshot()['prediction']['valid']
        assert runtime.snapshot()['metrics']['completed']==0
    finally:release.set();runtime.close()


def test_analysis_routes_drift_training_through_spectral_manager_and_controls(tmp_path,monkeypatch):
    from ninefbg.analysis import AnalysisController
    class Manager:
        def __init__(self,path):self.value={'busy':True,'method':'drift_gru','status':'running','job_id':'JD'};self.seen=[]
        def start(self,ids,options):self.seen.append(('start',ids,options));return deepcopy(self.value)
        def status(self):return deepcopy(self.value)
        def pause(self,reason):self.seen.append(('pause',reason));self.value['status']='paused'
        def resume(self,reason):self.seen.append(('resume',reason));self.value['status']='running'
        def cancel(self):self.seen.append(('cancel',));self.value.update(status='cancelled',busy=False)
        def close(self):pass
    monkeypatch.setattr('ninefbg.spectral_training.SpectralTrainingManager',Manager)
    analysis=AnalysisController(tmp_path)
    try:
        response=analysis.start_job('train',['session'],{'method':'drift_gru','training_mode':'new'})
        assert response['training']['method']=='drift_gru'
        assert analysis.spectral_training.seen[0][2]['method']=='drift_gru'
        assert analysis.training_control('pause')['status']=='paused'
        assert analysis.training_control('resume')['status']=='running'
        assert analysis.training_control('cancel')['status']=='cancelled'
    finally:analysis.close()


def test_model_gap_rule_applies_to_preload_seed_and_later_frames():
    runtime,_=make_runtime()
    runtime.metadata['window_rule']={'max_gap_ms':80}
    now=time.perf_counter_ns()-2_000_000_000
    annotation={'session_id':'session','trial_id':'trial','phase':'preload'}
    try:
        for i in range(16):runtime.submit_result(source(i,now+i*100_000_000)[0],annotation)
        assert len(runtime.trial_preload)==16  # Reference continuity accepts <=120 ms.
        runtime.submit_result(source(16,now+1_600_000_000)[0],{**annotation,'phase':'loading'})
        assert len(runtime.frames)==1  # Window continuity is the model's stricter 80 ms.
        assert not runtime.snapshot()['prediction']['valid']
        for i in range(17,32):runtime.submit_result(source(i,now+1_600_000_000+(i-16)*40_000_000)[0])
        wait_result(runtime)
        runtime.submit_result(source(32,now+2_300_000_000)[0])
        assert len(runtime.frames)==1 and not runtime.snapshot()['prediction']['valid']
    finally:runtime.close()
