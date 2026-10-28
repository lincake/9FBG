"""Real temporary-file recycling checks; generated models are not sensor evidence."""
import json
import os
from pathlib import Path

import pytest

import ninefbg.model_history as history_module
from ninefbg.model_history import ModelHistory, model_path
from ninefbg.spectral_model import AXES, UNITS, build_network, save_bundle
from ninefbg.spectral_data import SYNC_RULE
from ninefbg.experiment import coordinate_definition, default_point_grid
from test_live_pilot import load_pilot_archive, load_full_model
from test_history import create_directory_link


def tree_bytes(root):
    return {file.relative_to(root).as_posix(): file.read_bytes()
            for file in root.rglob('*') if file.is_file()}


def model_bytes(root):
    return {name: tree_bytes(root/name) for name in ('models', 'pilot_models', 'spectral_models')}


@pytest.fixture
def assets(load_pilot_archive, load_full_model):
    root = load_pilot_archive['root']
    directory = root/'models'
    directory.mkdir()
    (directory/'Mfixture.json').write_text(json.dumps(load_full_model), encoding='utf-8')
    for name in ('validation_sessions', 'analyses'):
        directory = root/name/'preserved_report'
        directory.mkdir(parents=True)
        (directory/'report.json').write_bytes(b'{"fixture_only":true,"preserve":"report"}\n')
    return root


@pytest.fixture
def spectral_assets(assets):
    pytest.importorskip('torch')
    trial, validation = 'portable_source:T1', 'portable_source:T2'
    metadata = {
        'wavelength_nm': [1500.+i*.1 for i in range(512)], 'preprocessing': {},
        'sample_rate_hz': 25, 'trained_point_ids': ['P01'],
        'input_normalization': {'mean': [0.]*512, 'scale': [1.]*512, 'fitted_split': 'train'},
        'output_normalization': {'mean': [0.]*6, 'scale': [1.]*6, 'fitted_split': 'train'},
        'acquisition_provenance': {
            'physical_definition': {'finger_id': 'generated-untrained-fixture', 'point_grid': default_point_grid(),
                'coordinate_definition': coordinate_definition(), 'axes': AXES, 'units': UNITS,
                'wrench_reference': 'PX6D_native_axes_and_origin', 'conditions': {}},
            'sources': [{'session_id': 'portable_source', 'manifest_sha256': 'a'*64,
                'files': {name: 'b'*64 for name in ('raw_spectra.jsonl', 'raw_force.jsonl', 'results.jsonl', 'events.jsonl')}}]},
        'window_rule': {'version': 'spectral_causal_windows_v1', 'length': 16, 'target': 'last_frame',
                        'hidden_init': 'zero', 'max_gap_ms': 120}, 'sync_rule': SYNC_RULE,
        'split': {'version': 'spectral_group_split_v1', 'assignments': {'fixture': 'train'},
                  'group_mode': 'baseline_component', 'trial_assignments': {trial: 'train', validation: 'validation'},
                  'source_manifests': {'portable_source': 'a'*64}},
        'training_trial_ids': [trial], 'validation_trial_ids': [validation],
        'evaluation': {'independent_validation': True, 'hardware_accuracy_qualified': False}}
    # The network has generated initial weights only; it is a format/IO fixture.
    save_bundle(assets, 'Sfixture', build_network(), metadata)
    extra = assets/'spectral_models'/'Sfixture'/'extra_assets'
    extra.mkdir()
    (extra/'audit.bin').write_bytes(b'preserve every regular package asset')
    return assets


@pytest.mark.parametrize('selected', [['Mfixture'], ['Pfixture'], ['Sfixture'], ['Mfixture', 'Pfixture', 'Sfixture']])
def test_three_model_formats_roundtrip_exact_bytes_and_preserve_sources_and_reports(spectral_assets, selected):
    root = spectral_assets
    before = model_bytes(root)
    preserved = {name: tree_bytes(root/name) for name in ('calibration_sessions', 'validation_sessions', 'analyses')}
    manager = ModelHistory(root)
    removed = manager.delete(selected)
    assert removed['deleted_ids'] == selected
    deleted = {row['model_id']: row for row in removed['deleted_models']}
    for value in selected:
        assert deleted[value]['restorable'] and not deleted[value]['loadable']
        with pytest.raises(ValueError, match='不存在|回收区'):
            model_path(root, value)
    manager.restore(selected)
    assert model_bytes(root) == before
    assert manager.snapshot()['deleted_models'] == []
    assert {name: tree_bytes(root/name) for name in preserved} == preserved


@pytest.mark.parametrize('selected', [[], ['Pfixture', 'Pfixture'], ['Pfixture', 'pfixture'],
                                    ['Pfixture', 'Mmissing'], ['Pfixture', '../Mfixture'],
                                    ['Pfixture', 'C:/outside/Pmodel'], [True], ['S..']])
def test_invalid_or_mixed_batch_never_moves_any_valid_model(assets, selected):
    before = model_bytes(assets)
    with pytest.raises(ValueError):
        ModelHistory(assets).delete(selected)
    assert model_bytes(assets) == before
    assert not (assets/'deleted_models').exists()
    assert not (assets/'model_history_operations').exists()


def test_current_inference_protects_whole_selected_batch(assets):
    before = model_bytes(assets)
    with pytest.raises(ValueError, match='先结束推理'):
        ModelHistory(assets).delete(['Mfixture', 'Pfixture'], in_use_model_id='Pfixture')
    assert model_bytes(assets) == before


def test_restore_conflict_never_overwrites_active_or_recycled_model(assets):
    manager = ModelHistory(assets)
    original = (assets/'pilot_models'/'Pfixture.json').read_bytes()
    manager.delete(['Pfixture'])
    recycled = tree_bytes(assets/'deleted_models')
    active = assets/'pilot_models'/'Pfixture.json'
    active.write_bytes(original + b' ')
    with pytest.raises(ValueError, match='同名|覆盖'):
        manager.restore(['Pfixture'])
    assert active.read_bytes() == original + b' '
    assert tree_bytes(assets/'deleted_models') == recycled
    row = manager.snapshot()['deleted_models'][0]
    assert not row['restorable'] and '同名' in row['history_error']


def test_tampered_recycled_payload_is_preserved_and_cannot_be_restored(assets):
    manager = ModelHistory(assets)
    manager.delete(['Pfixture'])
    payload = assets/'deleted_models'/'Pfixture'/'payload.json'
    with payload.open('ab') as stream:
        stream.write(b'changed')
    before = tree_bytes(assets/'deleted_models')
    with pytest.raises(ValueError, match='哈希'):
        manager.restore(['Pfixture'])
    assert tree_bytes(assets/'deleted_models') == before
    assert not (assets/'pilot_models'/'Pfixture.json').exists()
    assert not manager.snapshot()['deleted_models'][0]['restorable']


@pytest.mark.parametrize('restore', [False, True])
def test_second_move_failure_rolls_back_entire_batch(assets, monkeypatch, restore):
    manager = ModelHistory(assets)
    ids = ['Mfixture', 'Pfixture']
    if restore:
        manager.delete(ids)
    before, recycled = model_bytes(assets), tree_bytes(assets/'deleted_models')
    original, calls = Path.rename, []
    def interrupted(source, target):
        target = Path(target)
        moving = target.parent.name in {'models', 'pilot_models'} if restore else source.parent.name in {'models', 'pilot_models'}
        if moving:
            calls.append(source)
            if len(calls) == 2:
                raise PermissionError('generated second move failure')
        return original(source, target)
    monkeypatch.setattr(Path, 'rename', interrupted)
    with pytest.raises(ValueError, match='原模型已保留'):
        (manager.restore if restore else manager.delete)(ids)
    assert model_bytes(assets) == before
    assert tree_bytes(assets/'deleted_models') == recycled
    journals = [json.loads(p.read_text(encoding='utf-8')) for p in (assets/'model_history_operations').glob('*.json')]
    assert any(row['status'] == 'rolled_back' for row in journals)


@pytest.mark.parametrize('restore', [False, True])
def test_commit_log_failure_after_restore_container_cleanup_still_rolls_back(assets, monkeypatch, restore):
    manager = ModelHistory(assets)
    ids = ['Mfixture', 'Pfixture']
    if restore:
        manager.delete(ids)
    before, recycled = model_bytes(assets), tree_bytes(assets/'deleted_models')
    write = history_module._write_json
    def reject_commit(path, value):
        if path.parent.name == 'model_history_operations' and value.get('status') == 'committed':
            raise OSError('generated final journal failure')
        return write(path, value)
    monkeypatch.setattr(history_module, '_write_json', reject_commit)
    with pytest.raises(ValueError, match='原模型已保留'):
        (manager.restore if restore else manager.delete)(ids)
    assert model_bytes(assets) == before
    assert tree_bytes(assets/'deleted_models') == recycled


@pytest.mark.parametrize('directory', ['models', 'deleted_models', 'model_history_operations'])
def test_directory_links_cannot_move_assets_outside_userdata(assets, tmp_path, directory):
    external = tmp_path.parent/(tmp_path.name+'-external')
    external.mkdir()
    (external/'sentinel.bin').write_bytes(b'preserve outside userdata')
    link = assets/directory
    if link.exists():
        original = assets/(directory+'_original')
        link.rename(original)
    create_directory_link(link, external)
    try:
        with pytest.raises(ValueError, match='链接|允许'):
            ModelHistory(assets).delete(['Pfixture'])
        assert tree_bytes(external) == {'sentinel.bin': b'preserve outside userdata'}
        assert (assets/'pilot_models'/'Pfixture.json').is_file()
    finally:
        link.rmdir() if os.name == 'nt' else link.unlink()


def test_hardlinked_model_is_neither_loadable_nor_movable(assets):
    file = assets/'pilot_models'/'Pfixture.json'
    other = assets/'outside_model.json'
    os.link(file, other)
    before = file.read_bytes()
    with pytest.raises(ValueError, match='链接|歧义'):
        model_path(assets, 'Pfixture')
    with pytest.raises(ValueError, match='链接|歧义'):
        ModelHistory(assets).delete(['Pfixture'])
    assert file.read_bytes() == other.read_bytes() == before


def test_duplicate_registry_location_is_not_deleted(assets):
    original = assets/'models'/'Mfixture.json'
    wrong = assets/'pilot_models'/'Mfixture.json'
    wrong.write_bytes(original.read_bytes())
    before = model_bytes(assets)
    with pytest.raises(ValueError, match='多个位置'):
        ModelHistory(assets).delete(['Mfixture'])
    assert model_bytes(assets) == before
