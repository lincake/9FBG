"""Generated nine-channel validation evidence, no physical acceptance."""
from copy import deepcopy
import pytest
from ninefbg.drift_features import make_drift_reference, drift_pm
from ninefbg.spectral_validation import validated
from test_drift_runtime import source
from test_spectral_validation import evidence


def drift_evidence():
    value=evidence([.3]*6)
    end=value['host_rx_monotonic_ns']
    reference_samples=[source(100+i,end-3_000_000_000+i*40_000_000)[0] for i in range(51)]
    reference=make_drift_reference(reference_samples,effective_ns=end-1_000_000_000,reference_id='R1')
    value['spectral_reference']=reference
    value['prediction'].update(input_kind='delta_lambda_9',window_rule={'max_gap_ms':80})
    value['window']=[source(1+i,end-(15-i)*40_000_000,.001)[0] for i in range(16)]
    for frame in value['window']:frame['delta_lambda_pm']=drift_pm(frame,reference)
    return value


def test_drift_validation_preserves_actual_centers_and_fixed_reference():
    value=drift_evidence()
    actual=validated(value)
    assert actual['spectral_reference']['lambda0_nm']==[1500.+i for i in range(1,10)]
    assert len(actual['window'])==16
    assert all(frame['delta_lambda_pm']==pytest.approx([1.]*9) for frame in actual['window'])
    assert 'S0' not in actual['spectral_reference'] and 'intensity' not in actual['window'][0]


@pytest.mark.parametrize('field',['drift','config','invalid','gap','reference'])
def test_drift_validation_rejects_tampered_features_or_window(field):
    value=drift_evidence()
    if field=='drift':value['window'][0]['delta_lambda_pm'][0]=.5
    elif field=='config':value['window'][0]['config_version']='changed'
    elif field=='invalid':
        value['window'][0]['centers_nm'][0]=None;value['window'][0]['valid_channels'][0]=False;value['window'][0]['all_valid']=False
    elif field=='gap':value['window'][0]['host_rx_monotonic_ns']-=60_000_000
    elif field=='reference':value['spectral_reference']['trigger']='trial_preload_unloaded'
    with pytest.raises(ValueError):validated(value)
