"""Temporary synthetic records and real CPU/HTTP jobs; no physical qualification."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request

import pytest

from ninefbg.service import AcquisitionService
from ninefbg.spectral_model import load_bundle
from test_history_api import request as history_request
from test_live_validation_api import http_service
from test_spectral_pipeline import spectral_archive


def request(url, path, body=None, *, error=None):
    try:
        return history_request(url, path, body, error=error)
    except urllib.error.HTTPError as caught:
        detail = caught.read().decode('utf-8')
        caught.close()
        raise AssertionError(f'Unexpected HTTP {caught.code} for {path}: {detail}') from None


def hashes(path):
    return {item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in path.rglob('*') if item.is_file()}


def wait_training(url, *, status=None, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = request(url, '/api/state')
        training = state['analysis']['training']
        if (training.get('status') == status if status else not training.get('busy')):
            return state
        time.sleep(.03)
    raise AssertionError('CPU fixture training timeout: ' + repr(training))


def reject(url, body):
    req = urllib.request.Request(url + '/api/training/start', data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req, timeout=10)
    assert caught.value.code == 400
    result = json.load(caught.value)
    assert result['error'] and any('\u4e00' <= char <= '\u9fff' for char in result['error'])
    return result['error']


@pytest.fixture(scope='module')
def parent_assets(tmp_path_factory):
    pytest.importorskip('torch')
    root = tmp_path_factory.mktemp('synthetic-parent-api')
    old = spectral_archive(root, 'old-source', trials_count=6).name
    service = AcquisitionService(Path(__file__).resolve().parents[1], root)
    try:
        with http_service(service) as url:
            reply = request(url, '/api/training/start', {
                'session_ids': [old], 'method': 'spectral_gru', 'epochs': 1, 'batch_size': 64,
            })
            assert reply['busy']
            state = wait_training(url)
            training = state['analysis']['training']
            assert training['status'] == 'completed', training
            parent_id = training['model_id']
            assert state['analysis']['model_id'] is None
            assert not state['analysis']['inference_active']
            assert not service.running and not service.sources
    finally:
        service.close()
    new = spectral_archive(root, 'new-source', sample_offset=7., trials_count=6).name
    next_source = spectral_archive(root, 'next-source', sample_offset=9., trials_count=6).name
    return root, parent_id, old, new, next_source


@pytest.fixture
def api_assets(tmp_path, parent_assets):
    source, parent_id, old, new, next_source = parent_assets
    root = tmp_path / 'isolated-copy'
    shutil.copytree(source, root)
    service = AcquisitionService(Path(__file__).resolve().parents[1], root)
    try:
        with http_service(service) as url:
            yield service, url, parent_id, old, new, next_source
    finally:
        service.close()


def test_http_weight_inheritance_new_sources_grandchild_and_live_parent_preserved(api_assets):
    service, url, parent_id, old, new, next_source = api_assets
    root = service.userdata
    parent_path = root / 'spectral_models' / parent_id
    parent, _ = load_bundle(parent_path)
    original = hashes(root / 'calibration_sessions')
    parent_files = hashes(parent_path)
    loaded = request(url, '/api/analysis/load', {'model_id': parent_id, 'unloaded_confirmed': True})
    assert loaded['analysis']['model_id'] == parent_id and loaded['analysis']['inference_active']

    reply = request(url, '/api/training/start', {
        'session_ids': [old, new], 'method': 'spectral_gru',
        'training_mode': 'finetune', 'base_model_id': parent_id, 'epochs': 1, 'batch_size': 64,
    })
    assert reply['training']['training_mode'] == 'finetune'
    assert reply['training']['base_model_id'] == parent_id
    assert reply['training']['new_session_ids'] == [new]
    assert set(reply['training']['merged_session_ids']) == {old, new}
    assert len(reply['training']['session_ids']) == 2
    assert reply['training']['options']['learning_rate'] == .0001
    state = wait_training(url)
    training = state['analysis']['training']
    assert training['status'] == 'completed', training
    child_id = training['model_id']
    assert child_id != parent_id
    assert state['analysis']['model_id'] == parent_id and state['analysis']['inference_active']
    child_path = root / 'spectral_models' / child_id
    child, _ = load_bundle(child_path)
    assert child['training_mode'] == 'finetune' and child['base_model_id'] == parent_id
    assert child['parent_model_sha256'] == parent['model_sha256']
    lineage = child['training_lineage']
    assert lineage['weights_only'] and not lineage['checkpoint_resume']
    assert lineage['normalization_source'] == 'parent_model'
    assert lineage['normalization_parent_sha256'] == parent['model_sha256']
    assert child['input_normalization'] == parent['input_normalization']
    assert child['output_normalization'] == parent['output_normalization']
    assert {source['session_id'] for source in child['acquisition_provenance']['sources']} == {old, new}
    assert set(parent['training_trial_ids']) <= set(child['training_trial_ids'])
    assert set(parent['validation_trial_ids']) <= set(child['validation_trial_ids'])
    assert not set(parent['validation_trial_ids']) & set(child['training_trial_ids'])
    assert any(value.startswith(new + ':') for value in child['training_trial_ids'])
    assert child['weights_sha256'] != parent['weights_sha256']
    assert not child['formal_ready'] and not child['evaluation']['hardware_accuracy_qualified']
    comparison = child['evaluation']['finetuning_comparison']
    assert comparison['available']
    assert set(comparison['validation_trial_ids']) == set(parent['validation_trial_ids'])
    assert comparison['window_count'] == comparison['subset_count'] > 0
    assert len(comparison['window_identity_sha256']) == 64
    assert comparison['parent']['window_count'] == comparison['child']['window_count'] > 0
    assert comparison['parent']['wrench']['count'] == comparison['child']['wrench']['count']
    assert hashes(parent_path) == parent_files
    child_files = hashes(child_path)

    request(url, '/api/training/start', {
        'session_ids': [next_source], 'method': 'spectral_gru', 'training_mode': 'finetune',
        'base_model_id': child_id, 'epochs': 1, 'batch_size': 64,
    })
    state = wait_training(url)
    training = state['analysis']['training']
    assert training['status'] == 'completed', training
    grandchild, _ = load_bundle(root / 'spectral_models' / training['model_id'])
    assert grandchild['base_model_id'] == child_id
    assert grandchild['parent_model_sha256'] == child['model_sha256']
    assert {source['session_id'] for source in grandchild['acquisition_provenance']['sources']} == {old, new, next_source}
    assert grandchild['input_normalization'] == parent['input_normalization']
    assert grandchild['output_normalization'] == parent['output_normalization']
    assert set(child['validation_trial_ids']) <= set(grandchild['validation_trial_ids'])
    assert not set(child['validation_trial_ids']) & set(grandchild['training_trial_ids'])
    assert hashes(parent_path) == parent_files and hashes(child_path) == child_files
    assert hashes(root / 'calibration_sessions') == original
    assert state['analysis']['model_id'] == parent_id and state['analysis']['inference_active']
    assert not service.running and not service.force_source and not service.sources


@pytest.mark.parametrize('extra', [
    {'training_mode': 'unknown'}, {'training_mode': True}, {'training_mode': []},
    {'training_mode': 'finetune'}, {'training_mode': 'finetune', 'base_model_id': '../Soutside'},
    {'training_mode': 'finetune', 'base_model_id': 'Pfixture'},
    {'training_mode': 'finetune', 'base_model_id': 'Snot-saved'},
    {'training_mode': 'scratch', 'base_model_id': 'BASE'},
    {'training_mode': 'finetune', 'base_model_id': 'BASE', 'method': 'linear'},
    {'training_mode': 'finetune', 'base_model_id': 'BASE', 'method': 'mlp'},
    {'training_mode': 'finetune', 'base_model_id': 'BASE', 'unloaded_confirmed': True},
])
def test_http_invalid_finetuning_contract_rejected_before_job_or_file_write(api_assets, extra):
    service, url, parent_id, old, new, _ = api_assets
    before = hashes(service.userdata)
    body = {'session_ids': [new], 'method': 'spectral_gru', 'epochs': 1, **deepcopy(extra)}
    if body.get('base_model_id') == 'BASE':
        body['base_model_id'] = parent_id
    reject(url, body)
    state = request(url, '/api/state')
    assert not state['analysis']['busy']
    assert not state['analysis']['inference_active']
    assert hashes(service.userdata) == before
    assert not service.running and not service.sources


def test_http_unchanged_or_deleted_parent_sources_do_not_silently_reduce_replay(api_assets):
    service, url, parent_id, old, new, _ = api_assets
    body = {'session_ids': [old], 'method': 'spectral_gru', 'training_mode': 'finetune',
            'base_model_id': parent_id, 'epochs': 1}
    before = hashes(service.userdata)
    reject(url, body)
    assert hashes(service.userdata) == before
    old_path = service.userdata / 'calibration_sessions' / old
    archive = service.userdata / 'deleted_sources_fixture'
    old_path.rename(archive)
    body['session_ids'] = [new]
    before = hashes(service.userdata)
    reject(url, body)
    assert hashes(service.userdata) == before
    assert not request(url, '/api/state')['analysis']['busy']


def test_http_paused_finetune_blocks_second_job_and_parent_recycle_cancel_preserves_assets(api_assets):
    service, url, parent_id, old, new, _ = api_assets
    source_files = hashes(service.userdata / 'calibration_sessions')
    parent_files = hashes(service.userdata / 'spectral_models' / parent_id)
    body = {'session_ids': [new], 'method': 'spectral_gru', 'training_mode': 'finetune',
            'base_model_id': parent_id, 'epochs': 1000, 'patience': 1000}
    request(url, '/api/training/start', body)
    request(url, '/api/training/pause', {})
    state = wait_training(url, status='paused')
    assert state['analysis']['busy']
    reject(url, body)
    request(url, '/api/analysis/models/delete', {'model_ids': [parent_id]}, error='进行')
    request(url, '/api/training/cancel', {})
    state = wait_training(url)
    assert state['analysis']['training']['status'] == 'cancelled'
    assert hashes(service.userdata / 'calibration_sessions') == source_files
    assert hashes(service.userdata / 'spectral_models' / parent_id) == parent_files
    assert not state['analysis']['model_id'] and not state['analysis']['inference_active']
