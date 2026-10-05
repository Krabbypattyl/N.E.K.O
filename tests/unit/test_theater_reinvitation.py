"""Explicit invitation recovery preserves consent and cold replay."""

from dataclasses import replace

import pytest

from services.theater import numeric_v2_workflow as workflow
from services.theater.numeric_v2_runtime import NumericV2Runtime, NumericV2RuntimeError, TurnRequestV2
from tests.unit.test_theater_numeric_v2_runtime import _binding
from tests.unit.test_theater_review_round8_invitation import ACCEPT, OFFER, setup_case, turn


async def reinvite(runtime, current, key='reinvite'):
    return await workflow.execute_numeric_v2_turn(config_manager=object(), runtime=runtime,
        current=current, turn=TurnRequestV2(key, current.session.revision, '重新邀请', 'reinvite'),
        ensure_current_binding=lambda _: _binding())


@pytest.mark.asyncio
@pytest.mark.parametrize('review,mode', [(False, 'on'), (True, 'off'), (True, 'failure')])
async def test_expired_invitation_can_be_reissued_and_accepted_after_cold_restore(tmp_path, monkeypatch, review, mode):
    runtime, current = await setup_case(tmp_path, monkeypatch, mode=mode, review=review)
    offered = await turn(runtime, current, 'offer', '接下来呢？')
    followed = await turn(runtime, offered.stored, 'followup', '先问个问题。')
    assert not followed.stored.session.transition_offered
    recovered = await reinvite(runtime, followed.stored)
    assert recovered.performance['performance'] == OFFER
    assert recovered.performance['suggested_inputs'] == [ACCEPT]
    assert recovered.stored.session.current_node_id == followed.stored.session.current_node_id
    assert recovered.stored.session.metrics == followed.stored.session.metrics
    assert recovered.stored.session.node_turn_count == followed.stored.session.node_turn_count
    assert recovered.stored.ledger_events[-1]['program_invitation']['offer'] == OFFER
    restored = await NumericV2Runtime(runtime.engine, tmp_path).restore_session(current.session.session_id)
    assert restored == recovered.stored
    accepted = await turn(runtime, restored, 'accept-new', ACCEPT, 'suggestion')
    assert accepted.stored.session.current_node_id == 'ending_leave'


@pytest.mark.asyncio
async def test_dead_authored_quote_recovery_has_no_model_calls(tmp_path, monkeypatch):
    runtime, current = await setup_case(tmp_path, monkeypatch, text=OFFER + '（摇头）先别去了。')
    blocked = await turn(runtime, current, 'quote', '接下来呢？')
    assert not blocked.stored.session.transition_offered

    async def forbidden(*args, **kwargs):
        raise AssertionError('reinvitation must not call a model')

    monkeypatch.setattr(workflow.NumericV2Actor, 'generate_turn', forbidden)
    monkeypatch.setattr(workflow.NumericV2MetricEvaluator, 'evaluate', forbidden)
    recovered = await reinvite(runtime, blocked.stored)
    assert recovered.performance['performance'] == OFFER
    assert not workflow.invitation_recovery_contract(runtime, recovered.stored)


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['incomplete', 'forbidden', 'missing_offer', 'missing_accept', 'terminal'])
async def test_unavailable_reinvitation_does_not_commit(tmp_path, monkeypatch, guard):
    runtime, current = await setup_case(tmp_path, monkeypatch, complete=guard != 'incomplete')
    if guard == 'forbidden':
        current = replace(current, session=replace(current.session, dialogue_policy='forbidden'))
    if guard in {'missing_offer', 'missing_accept'}:
        runtime.engine.nodes['start']['route_gates'][1]['transition_contract'][
            'fallback_offer' if guard == 'missing_offer' else 'accept_input'] = ''
    if guard == 'terminal':
        runtime.engine.nodes['ending_leave']['terminal'] = True
    assert workflow.invitation_recovery_contract(runtime, current) is None
    with pytest.raises(NumericV2RuntimeError, match='numeric_reinvitation_not_available'):
        await reinvite(runtime, current)
    restored = await runtime.restore_session(current.session.session_id)
    assert restored.session.revision == current.session.revision


@pytest.mark.asyncio
async def test_router_reinvitation_replay_and_stale_revision(tmp_path, monkeypatch):
    from main_routers import numeric_theater_router as router

    runtime, current = await setup_case(tmp_path, monkeypatch)

    async def get_runtime(*args):
        return runtime

    async def no_forget(*args):
        return False

    async def commit_allowed(*args):
        return None

    monkeypatch.setattr(router, 'get_config_manager', object)
    monkeypatch.setattr(router, '_runtime_for_story', get_runtime)
    monkeypatch.setattr(router, '_ensure_current_catgirl', lambda *args: _binding())
    monkeypatch.setattr(router, '_forget_pending', no_forget)
    monkeypatch.setattr(router, '_assert_commit_allowed', commit_allowed)
    payload = {'client_turn_id': 'control', 'base_revision': current.session.revision,
               'message': '重新邀请', 'input_source': 'reinvite'}
    first = await router._submit_numeric_input_once(payload, current.session.story_package_id, current.session.session_id)
    assert first['ok'] and first['performance']['suggested_inputs'] == [ACCEPT]
    assert not first['resolved_turn']['route_changed']
    replay = await router._submit_numeric_input_once(payload, current.session.story_package_id, current.session.session_id)
    assert replay['idempotent_replay'] and replay['session']['revision'] == first['session']['revision']
    stale = await router._submit_numeric_input_once({**payload, 'client_turn_id': 'stale'},
        current.session.story_package_id, current.session.session_id)
    assert stale.status_code == 409
    assert (await runtime.restore_session(current.session.session_id)).session.revision == first['session']['revision']

@pytest.mark.asyncio
@pytest.mark.parametrize('review', [False, True])
async def test_reinvitation_respects_required_narration_and_review_switch(tmp_path, monkeypatch, review):
    from main_routers import numeric_theater_router as router
    from tests.unit.test_theater_numeric_v2_fixed_narration import _piece

    runtime, current = await setup_case(tmp_path, monkeypatch, review=review)
    runtime.engine.nodes['start']['story_beat']['fixed_narrations'] = [_piece('pending', '必显原文。')]
    async def options():
        return {'review': review}
    monkeypatch.setattr('services.theater.numeric_v2_options.aload_theater_module_options', options)
    monkeypatch.setattr(workflow, 'aload_theater_module_options', options)
    payload = await router._numeric_payload(runtime, current)
    assert payload['invitation_recovery_available'] is (not review)
    if review:
        with pytest.raises(NumericV2RuntimeError, match='numeric_reinvitation_not_available'):
            await reinvite(runtime, current)
        assert (await runtime.restore_session(current.session.session_id)).session.revision == current.session.revision
        piece = {'node_id': 'start', 'id': 'pending', 'text': '必显原文。', 'bindings': {}, 'position': 'after'}
        current = replace(current, session=replace(current.session,
            performance_history=(*current.session.performance_history, {'fixed_narrations': [piece]})))
        assert (await router._numeric_payload(runtime, current))['invitation_recovery_available']
        # Persisted delivery is checked by the same helper used at submission.
        assert workflow.invitation_recovery_contract(runtime, current)
    else:
        assert (await reinvite(runtime, current)).stored.session.transition_offered
