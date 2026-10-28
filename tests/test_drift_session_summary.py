"""Manifest-only drift eligibility; no raw/result scan or accuracy claim."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from ninefbg.experiment import _session_summary, list_sessions


def manifest(input_kind='delta_S'):
    return {'complete':True,'settings':{'finger_id':'sample','acquisition_protocol':'force_target_wrench_v1',
            'guided_acquisition':True,'input_kind':input_kind,
            'force_target_wrench_criteria':{'criteria_version':'force_target_wrench_quality_v2','preload_duration_s':2}},
            'final':{'trials':[{'trial_id':'T0001','point_id':'P01','status':'completed','bad':False,'manual_mark':[],
                 'quality':{'accepted':True,'criteria_version':'force_target_wrench_quality_v2','metrics':{
                     'preload_duration_s':2,'preload_recorded_duration_s':2.,'preload_contact_detected':False,
                     'press_prompt_monotonic_ns':10_000_000_000,
                     'nine_fbg_preload_valid_frames':51,'nine_fbg_contact_windows':3}}}]}}


def test_old_v2_full_spectrum_is_only_a_manifest_drift_candidate():
    value=manifest()
    value['final']['trials'][0]['quality']['metrics'].pop('nine_fbg_preload_valid_frames')
    value['final']['trials'][0]['quality']['metrics'].pop('nine_fbg_contact_windows')
    summary=_session_summary(value)
    assert summary['has_training_data'] and summary['drift_available']
    assert summary['drift_unavailable_reason'] is None
    assert summary['drift_candidate_trial_count']==1
    assert summary['drift_availability_scope']=='manifest_preload_candidate_only'
    assert not summary['formal_training_qualified']


@pytest.mark.parametrize('protocol,version',[('force_target_wrench_v1','force_target_wrench_quality_v1'),
                                         ('timed_wrench_v1','timed_wrench_quality_v1'),
                                         ('guided_round_v1','guided_round_v1'),(None,'guided_round_v1')])
def test_legacy_sources_are_not_default_drift_candidates(protocol,version):
    value=manifest();value['settings']['acquisition_protocol']=protocol
    value['final']['trials'][0]['quality']['criteria_version']=version
    value['final']['trials'][0]['quality'].pop('metrics')
    summary=_session_summary(value)
    assert summary['has_training_data']
    assert not summary['drift_available'] and summary['drift_unavailable_reason']
    assert summary['drift_candidate_trial_count']==0


@pytest.mark.parametrize('key,bad',[('preload_duration_s',1),('preload_recorded_duration_s',1.99),
                                   ('preload_recorded_duration_s',True),('preload_recorded_duration_s',math.inf),
                                   ('preload_contact_detected',True),('preload_contact_detected',None),
                                   ('press_prompt_monotonic_ns',True),('press_prompt_monotonic_ns',0)])
def test_missing_or_invalid_manifest_preload_evidence_is_unavailable(key,bad):
    value=manifest();value['final']['trials'][0]['quality']['metrics'][key]=bad
    summary=_session_summary(value)
    assert summary['has_training_data'] and not summary['drift_available']
    assert '采前基线' in summary['drift_unavailable_reason']


@pytest.mark.parametrize('key,bad',[('nine_fbg_preload_valid_frames',11),('nine_fbg_preload_valid_frames',True),
                                   ('nine_fbg_contact_windows',0),('nine_fbg_contact_windows',None)])
def test_new_drift_mode_requires_saved_final_nine_channel_gate(key,bad):
    value=manifest('delta_lambda_9')
    assert _session_summary(value)['drift_available']
    value['final']['trials'][0]['quality']['metrics'][key]=bad
    assert not _session_summary(value)['drift_available']


def test_mixed_accepted_incompatible_trial_excludes_whole_session_but_rejected_trial_does_not():
    value=manifest()
    old=deepcopy(value['final']['trials'][0]);old.update(trial_id='T0002')
    old['quality']['metrics'].pop('press_prompt_monotonic_ns')
    value['final']['trials'].append(old)
    summary=_session_summary(value)
    assert summary['accepted_trial_count']==2 and summary['drift_candidate_trial_count']==1
    assert not summary['drift_available']
    old['bad']=True
    assert _session_summary(value)['drift_available']


@pytest.mark.parametrize('change',[{'complete':False},{'diagnostic':True}])
def test_incomplete_or_diagnostic_is_unavailable(change):
    value=manifest();value.update(change)
    assert not _session_summary(value)['drift_available']


def test_listing_reads_only_manifest_and_preserves_original_bytes(tmp_path,monkeypatch):
    folder=tmp_path/'calibration_sessions'/'session'
    folder.mkdir(parents=True)
    path=folder/'manifest.json';path.write_text(json.dumps(manifest()),encoding='utf-8')
    before=path.read_bytes()
    original_read_text=Path.read_text
    def guarded_read_text(file,*args,**kwargs):
        assert file.name=='manifest.json', 'A session summary must not scan source spectra or fitted results'
        return original_read_text(file,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',guarded_read_text)
    summary,=list_sessions(tmp_path)
    assert summary['drift_available'] and path.read_bytes()==before
