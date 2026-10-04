"""Regression coverage for maintainer review 5979596506."""

from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.unit.test_theater_numeric_v2_router import _client
from tests.unit.theater_workshop.numeric_v2_fixture import numeric_v2_setup, numeric_v2_story
from theater_workshop.host import InProcessPackageGateway
from theater_workshop.sdk.numeric_v2 import NumericV2Compiler
from theater_workshop.sdk.numeric_v2_project_store import NumericV2ProjectStore, _setup_fields


@pytest.mark.parametrize('description', [123, '字' * 2500], ids=['number', 'long'])
@pytest.mark.parametrize('repair', ['brief', 'short', 'delete', 'null', 'empty'])
def test_invalid_old_description_can_be_repaired(tmp_path, description, repair):
    store = NumericV2ProjectStore(tmp_path, transaction=nullcontext,
        compiler=NumericV2Compiler(InProcessPackageGateway()))
    project = store.create()
    project['setup'] = numeric_v2_setup()
    project['setup']['metrics'][0]['bands'][0]['description'] = description
    store._write(project)
    changes = {'brief': '修复旧草稿'}
    if repair != 'brief':
        metrics = deepcopy(project['setup']['metrics'])
        band = metrics[0]['bands'][0]
        if repair == 'short':
            band['description'] = '有效描述'
        elif repair == 'delete':
            band.pop('description')
        elif repair == 'null':
            band['description'] = None
        elif repair == 'empty':
            metrics = []
        changes = {'metrics': metrics}
    saved = store.update(project['project_id'], base_revision=project['revision'], changes={'setup': changes})
    assert saved['revision'] == project['revision'] + 1
    assert saved['project_id'] == project['project_id']
    if repair != 'empty':
        assert saved['setup']['metrics'][0]['bands'][0].get('description') == (
            '有效描述' if repair == 'short' else None)
    incoming = numeric_v2_setup()
    incoming['metrics'][0]['bands'][0]['description'] = description
    with pytest.raises(ValueError, match='invalid_metric_band_description'):
        store.update(saved['project_id'], base_revision=saved['revision'], changes={'setup': incoming})
    assert store.get(saved['project_id'])['revision'] == saved['revision']


@pytest.mark.parametrize('description', [123, {'x': 1}, '字' * 2500], ids=['number', 'object', 'long'])
def test_import_story_discards_invalid_description_only_from_author_projection(tmp_path, description):
    story = numeric_v2_story()
    for definition in story['metric_schema'].values():
        definition['bands'][0]['description'] = description
    compiler = NumericV2Compiler(InProcessPackageGateway())
    store = NumericV2ProjectStore(tmp_path, transaction=nullcontext, compiler=compiler)
    imported = store.import_story(story)
    assert imported['story'] == story
    assert all('description' not in metric['bands'][0] for metric in imported['setup']['metrics'])


def test_legacy_extra_matching_ignores_unknown_null_keys():
    setup = numeric_v2_setup()
    setup['metrics'][0]['bands'][0]['color'] = '#f00'
    incoming = deepcopy(setup)
    incoming['metrics'][0]['bands'][0]['new_key'] = None
    cleaned = _setup_fields(incoming, legacy=setup)
    assert 'color' not in cleaned['metrics'][0]['bands'][0]
    assert 'new_key' not in cleaned['metrics'][0]['bands'][0]
    incoming['metrics'][0]['bands'][0]['new_key'] = 'new'
    with pytest.raises(ValueError, match='unsupported_metric_band_field'):
        _setup_fields(incoming, legacy=setup)


@pytest.mark.parametrize('description', [123, '字' * 2500], ids=['number', 'long'])
def test_legacy_view_roundtrip_and_snapshot_import_preserve_package(tmp_path, description):
    compiler = NumericV2Compiler(InProcessPackageGateway())
    store = NumericV2ProjectStore(tmp_path / 'first', transaction=nullcontext, compiler=compiler)
    project = store.import_story(numeric_v2_story())
    project['setup']['metrics'][0]['bands'][0]['description'] = description
    project['story']['metric_schema'][project['setup']['metrics'][0]['id']]['bands'][0]['description'] = description
    store._write(project)
    view = store.get(project['project_id'])
    assert 'description' not in view['setup']['metrics'][0]['bands'][0]
    assert view['story'] == project['story']
    view['setup']['metrics'][0]['name'] = '修订名称'
    saved = store.update(project['project_id'], base_revision=project['revision'], changes={'setup': view['setup']})
    assert saved['setup']['metrics'][0]['name'] == '修订名称'
    second = NumericV2ProjectStore(tmp_path / 'second', transaction=nullcontext, compiler=compiler)
    imported = second.import_project(project)
    assert imported['project_id'] == project['project_id']
    assert imported['story'] == project['story']
    assert 'description' not in imported['setup']['metrics'][0]['bands'][0]


@pytest.mark.parametrize('description', [123, '字' * 2500, '有效描述'], ids=['number', 'long', 'valid'])
def test_brief_only_repair_preserves_package_and_publish_receipts(tmp_path, description):
    store = NumericV2ProjectStore(tmp_path, transaction=nullcontext,
        compiler=NumericV2Compiler(InProcessPackageGateway()))
    project = store.import_story(numeric_v2_story())
    project['setup']['metrics'][0]['bands'][0]['description'] = description
    project['story']['metric_schema'][project['setup']['metrics'][0]['id']]['bands'][0]['description'] = description
    compiled = store._compiler.compile_core(project['story'])
    for key in ('compile_result', 'neko_validation', 'install_result'):
        project[key] = {'success': True, 'package_hash': compiled.package_hash,
            'revision': project['revision']}
    store._write(project)
    original = deepcopy(project)
    saved = store.update(project['project_id'], base_revision=project['revision'], changes={'setup': {'brief': '修订简介'}})
    assert saved['story'] == original['story']
    for key in ('compile_result', 'neko_validation', 'install_result'):
        assert saved[key] == {**original[key], 'revision': saved['revision']}


@pytest.mark.parametrize('remaining', [4, 0], ids=['four', 'clear'])
def test_imported_legacy_metrics_can_be_reduced(tmp_path, remaining):
    compiler = NumericV2Compiler(InProcessPackageGateway())
    source_store = NumericV2ProjectStore(tmp_path / 'source', transaction=nullcontext, compiler=compiler)
    project = source_store.import_story(numeric_v2_story())
    original_metric = project['setup']['metrics'][0]
    project['setup']['metrics'] = [{**deepcopy(original_metric), 'id': f'metric_{index}'}
        for index in range(5)]
    store = NumericV2ProjectStore(tmp_path / 'imported', transaction=nullcontext, compiler=compiler)
    imported = store.import_project(project)
    assert len(imported['setup']['metrics']) == 5
    with pytest.raises(ValueError, match='metric_limit_exceeded'):
        store.update(imported['project_id'], base_revision=imported['revision'],
            changes={'setup': {'brief': '仍未修复超限数值'}})
    assert store.get(imported['project_id'])['revision'] == imported['revision']
    saved = store.update(imported['project_id'], base_revision=imported['revision'],
        changes={'setup': {'metrics': imported['setup']['metrics'][:remaining]}})
    assert saved['project_id'] == imported['project_id']
    assert saved['revision'] == imported['revision'] + 1
    assert len(saved['setup']['metrics']) == remaining
    assert list(saved['story']['metric_schema']) == [f'metric_{index}' for index in range(remaining)]
    with pytest.raises(ValueError, match='metric_limit_exceeded'):
        store.update(saved['project_id'], base_revision=saved['revision'],
            changes={'setup': {'metrics': project['setup']['metrics']}})
    assert store.get(saved['project_id'])['revision'] == saved['revision']


@pytest.mark.parametrize('damage', ['text', 'null', 'number', 'effect_list', 'effect_dict'])
@pytest.mark.parametrize('clear', [False, True], ids=['replace', 'clear'])
def test_imported_malformed_metrics_allow_valid_replacement(tmp_path, damage, clear):
    compiler = NumericV2Compiler(InProcessPackageGateway())
    source_store = NumericV2ProjectStore(tmp_path / 'source', transaction=nullcontext, compiler=compiler)
    project = source_store.import_story(numeric_v2_story())
    if damage.startswith('effect_'):
        project['setup']['metrics'][0]['relationship_effect'] = ['x'] if damage == 'effect_list' else {}
    else:
        project['setup']['metrics'].insert(0, {'text': 'x', 'null': None, 'number': 5}[damage])
    store = NumericV2ProjectStore(tmp_path / 'imported', transaction=nullcontext, compiler=compiler)
    imported = store.import_project(project)
    clean = [] if clear else [{**numeric_v2_setup()['metrics'][0], 'id': 'replacement'}]
    saved = store.update(imported['project_id'], base_revision=imported['revision'],
        changes={'setup': {'metrics': clean}})
    assert saved['project_id'] == imported['project_id']
    assert saved['revision'] == imported['revision'] + 1
    assert list(saved['story']['metric_schema']) == ([] if clear else ['replacement'])
    assert store.get(saved['project_id'])['setup'] == saved['setup']


@pytest.mark.parametrize('setup', ['broken', ['broken'], 5], ids=['text', 'list', 'number'])
def test_malformed_setup_does_not_block_workspace_recovery(tmp_path, setup):
    store = NumericV2ProjectStore(tmp_path, transaction=nullcontext,
        compiler=NumericV2Compiler(InProcessPackageGateway()))
    damaged = store.create()
    damaged['setup'] = setup
    store._write(damaged)
    healthy = store.create()
    healthy['generation_state'] = 'running'
    store._write(healthy)
    assert {row['project_id'] for row in store.list()} == {damaged['project_id'], healthy['project_id']}
    assert store.get(damaged['project_id'])['setup'] == {}
    store.recover_interrupted()
    assert store.get(healthy['project_id'])['generation_state'] == 'interrupted'
    assert store._read_path(store._path(damaged['project_id']))['setup'] == setup
    clean = numeric_v2_setup()
    saved = store.update(damaged['project_id'], base_revision=damaged['revision'],
        changes={'setup': clean})
    assert saved['project_id'] == damaged['project_id']
    assert saved['revision'] == damaged['revision'] + 1
    assert saved['setup']['brief'] == clean['brief']
    assert saved['setup']['metrics'][0]['id'] == clean['metrics'][0]['id']
    assert store._read_path(store._path(damaged['project_id']))['setup'] == saved['setup']
    with pytest.raises(ValueError, match='unsupported_setup_field'):
        store.update(saved['project_id'], base_revision=saved['revision'],
            changes={'setup': {'unknown_new': 'still rejected'}})
    assert store.get(saved['project_id'])['revision'] == saved['revision']


@pytest.mark.parametrize('edit', [False, True], ids=['unchanged', 'real_edit'])
def test_cleaned_view_autosave_only_stales_quality_on_real_change(tmp_path, edit):
    store = NumericV2ProjectStore(tmp_path, transaction=nullcontext,
        compiler=NumericV2Compiler(InProcessPackageGateway()))
    project = store.import_story(numeric_v2_story())
    project['setup']['metrics'][0]['bands'][0]['description'] = 123
    project['setup']['metrics'][0]['bands'][0]['color'] = '#f00'
    project['authoring']['quality_assessment'] = {'scope': 'full_story_simple', 'stale': False}
    project['authoring']['pacing_diagnostics'] = {'note': 'keep'}
    store._write(project)
    view = store.get(project['project_id'])
    if edit:
        view['setup']['metrics'][0]['name'] = '实际修改'
    saved = store.update(project['project_id'], base_revision=project['revision'],
        changes={'setup': view['setup']})
    assert saved['authoring']['quality_assessment']['stale'] is edit
    assert saved['authoring']['pacing_diagnostics'] == (None if edit else {'note': 'keep'})


@pytest.mark.parametrize('cancelled', [False, True])
def test_forget_preserves_no_receipt_for_cancelled_start(tmp_path, monkeypatch, cancelled):
    class MemoryClient:
        async def post(self, url, **kwargs):
            return SimpleNamespace(is_success=True, content=b'{}',
                json=lambda: {'ok': True, 'forget_marker': 'round7_marker'})

    monkeypatch.setattr('utils.internal_http_client.get_internal_http_client', lambda: MemoryClient())
    scope = {'story_id': 'numeric_v2_contract', 'session_id': 'round7_forget'}
    owner = {'X-Neko-Theater-Activity': 'round7-owner'}
    with _client(tmp_path, monkeypatch) as client:
        assert client.post('/api/theater-numeric/session/start', headers=owner, json=scope).status_code == 200
        ended = client.post('/api/theater-numeric/session/end', headers=owner, json={**scope,
            'base_revision': 0, 'base_lifecycle_revision': 0, 'cancelled_start': cancelled})
        assert ended.status_code == 200, ended.text
        forgotten = client.post('/api/theater-numeric/memory/forget', json={
            'story_id': scope['story_id'], 'character_id': 'character_' + '1' * 32})
        assert forgotten.status_code == 200, forgotten.text
    receipts = list(tmp_path.rglob('theater_end_*.json'))
    assert bool(receipts) is not cancelled
