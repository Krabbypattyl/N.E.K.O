"""Production pause skips every model audit without freezing normal generation or facts."""

from copy import deepcopy

import pytest

from services.theater import numeric_v2_options as options, numeric_v2_workflow as workflow
from services.theater.numeric_v2_evaluator import NumericV2EvaluationResult
from services.theater.numeric_v2_runtime import NumericV2Engine, NumericV2Runtime, TurnRequestV2
from tests.unit.test_theater_numeric_v2_runtime import _binding, _opening
from tests.unit.test_theater_numeric_v2_unified_pacing import story_for


@pytest.fixture
def speed_first(monkeypatch):
    from utils import preferences
    stored = {'theaterModule' + ''.join(part.title() for part in key.split('_')): False
              for key in options.default_options()}
    original = deepcopy(stored)
    async def load():
        raise AssertionError('removed theater switches must not read preferences')
    monkeypatch.setattr(preferences, 'aload_global_entry_flags', load)
    monkeypatch.setattr(options, 'MODEL_REVIEWS_ENABLED', False)
    monkeypatch.setattr(workflow, 'aload_theater_module_options', options.aload_theater_module_options)
    async def forbidden(*args, **kwargs):
        raise AssertionError('speed-first runtime must not call a model audit')
    monkeypatch.setattr(workflow.NumericV2MetricEvaluator, 'validate_transition_offer', forbidden)
    monkeypatch.setattr(workflow.NumericV2MetricEvaluator, 'verify_contract_boundaries', forbidden)
    monkeypatch.setattr(workflow.NumericV2NarrativeProposer, 'propose', forbidden)
    monkeypatch.setattr(workflow.NumericV2NarrativeProposer, 'review', forbidden)
    yield forbidden
    assert stored == original  # Temporary pause never overwrites the user's preferences.


@pytest.mark.asyncio
async def test_production_options_pause_audits_even_when_saved_on(speed_first):
    active = await options.aload_theater_module_options()
    assert all(active[key] is False for key in ('review', 'dispute', 'review_contract'))
    assert active['evaluator'] and active['review_delivery']


@pytest.mark.asyncio
async def test_all_modules_default_on_without_legacy_preferences(speed_first, monkeypatch):
    assert all(options.default_options().values())
    monkeypatch.setattr(options, 'MODEL_REVIEWS_ENABLED', True)
    assert await options.aload_theater_module_options() == options.default_options()


def test_removed_settings_page_and_options_api_are_unavailable(tmp_path, monkeypatch):
    from tests.unit.test_theater_numeric_v2_router import _client
    from main_routers.pages_router import router
    with _client(tmp_path, monkeypatch) as client:
        client.app.include_router(router)
        assert client.get('/theater/settings').status_code == 404
        assert client.get('/api/theater-numeric/options').status_code == 404
        assert client.post('/api/theater-numeric/options', json={'modules': {'evaluator': False}}).status_code == 404


@pytest.mark.asyncio
async def test_opening_with_author_boundaries_does_not_call_review(speed_first, monkeypatch):
    story = story_for('山径的避风棚')
    story['nodes'][0]['story_beat']['opening_only_boundaries'] = ['不得在公开开场披露后续身份。']
    async def opening(self, **kwargs):
        return {'performance': '（抬眼）你好。', 'suggested_inputs': []}
    monkeypatch.setattr(workflow.NumericV2Actor, 'generate_opening', opening)
    result = await workflow.generate_validated_opening(
        engine=NumericV2Engine.from_mapping(story), config_manager=object(), session_id='speed-opening',
        catgirl_binding=_binding(), actor_budget_profile='balanced')
    assert result['performance'] == '（抬眼）你好。'


@pytest.mark.asyncio
@pytest.mark.parametrize('fact_source', ['evaluator', 'actor'])
async def test_ordinary_turn_keeps_two_core_calls_and_commits_facts(speed_first, tmp_path, monkeypatch, fact_source):
    engine = NumericV2Engine.from_mapping(story_for('山径的避风棚'))
    runtime = NumericV2Runtime(engine, tmp_path)
    current = await runtime.start_session(session_id='speed-turn', catgirl_binding=_binding(), opening_performance=_opening())
    calls = []
    text = '眼前的事情已经处理完。'
    key = 'scene:start:done'
    async def evaluate(self, **kwargs):
        calls.append('evaluator')
        return NumericV2EvaluationResult((), False, fact_operations=(
            ({'op': 'set', 'key': key, 'value': True, 'visibility': 'public'},)
            if fact_source == 'evaluator' else ()))
    async def generate(self, **kwargs):
        calls.append('actor')
        return {'performance': text, 'suggested_inputs': [], 'fact_candidates': [
            {'key': key, 'value': True, 'evidence_quote': text}]}
    monkeypatch.setattr(workflow.NumericV2MetricEvaluator, 'evaluate', evaluate)
    monkeypatch.setattr(workflow.NumericV2Actor, 'generate_turn', generate)
    monkeypatch.setattr(workflow.NumericV2Actor, '_character_profile', lambda self: '温和克制。')
    diagnostics = {}
    result = await workflow.execute_numeric_v2_turn(
        config_manager=object(), runtime=runtime, current=current,
        turn=TurnRequestV2('one', 0, text), ensure_current_binding=lambda _: _binding(),
        diagnostics_sink=diagnostics)
    assert calls == ['evaluator', 'actor']
    assert result.stored.session.revision == 1
    assert result.stored.session.story_state['facts'][key]['value'] is True
    assert diagnostics['transition_judge_calls'] == 0
    assert diagnostics['completion_fact_review_calls'] == 0


@pytest.mark.asyncio
async def test_reinvitation_uses_author_contract_without_generation_or_review(speed_first, tmp_path, monkeypatch):
    from tests.unit.test_theater_review_round8_invitation import setup_case, OFFER
    runtime, current = await setup_case(tmp_path, monkeypatch, mode='on')
    monkeypatch.setattr(workflow, 'aload_theater_module_options', options.aload_theater_module_options)
    monkeypatch.setattr(workflow.NumericV2Actor, 'generate_turn', speed_first)
    monkeypatch.setattr(workflow.NumericV2MetricEvaluator, 'validate_transition_offer', speed_first)
    result = await workflow.execute_numeric_v2_turn(
        config_manager=object(), runtime=runtime, current=current,
        turn=TurnRequestV2('reinvite', current.session.revision, '', 'reinvite'),
        ensure_current_binding=lambda _: _binding())
    assert result.performance['performance'] == OFFER
    assert result.stored.session.current_node_id == current.session.current_node_id


@pytest.mark.asyncio
async def test_ending_question_keeps_local_guard_without_model_audit(speed_first, tmp_path, monkeypatch):
    from tests.unit.test_theater_numeric_v2_terminal_question_default import _setup, CHOICE
    _, _, calls, diagnostics, execute = await _setup(
        tmp_path, monkeypatch, drafts=(CHOICE, '（点头）故事到这里落幕。'))
    monkeypatch.setattr(workflow, 'aload_theater_module_options', options.aload_theater_module_options)
    monkeypatch.setattr(workflow.NumericV2MetricEvaluator, 'verify_contract_boundaries', speed_first)
    monkeypatch.setattr(workflow.NumericV2MetricEvaluator, 'validate_transition_offer', speed_first)
    result = await execute()
    assert result.stored.session.status == 'ended'
    assert calls['terminal'] == []
    assert diagnostics['terminal_question_review_calls'] == 0
