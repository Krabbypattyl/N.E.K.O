"""复核链等待优化：时限位置、改写后定向复检与整回合复核预算。

这些改动只约束等待与第二次判定看到的材料，不改变玩家授权、去向公开、数值结算或原子提交。
"""

from dataclasses import replace
import asyncio
import json
from types import SimpleNamespace

import pytest

from services.theater import numeric_v2_evaluator as evaluator
from services.theater import numeric_v2_workflow
from services.theater.numeric_v2_workflow import NUMERIC_V2_REVIEW_BUDGET_SECONDS
from services.theater.numeric_v2_actor import NumericV2ActorOutputError
from tests.unit.test_theater_numeric_v2_review_capacity import _fixture
from tests.unit.test_theater_numeric_v2_runtime import _binding


def _long_history_session(engine, session, prose_repeats=20):
    history = tuple({
        'revision': index,
        'from_node_id': 'start',
        'to_node_id': 'start',
        'performance_contract_version': 3,
        'input_text': '我听见了。',
        'performance': '（' + '她整理好手边的工具。' * prose_repeats + '）',
    } for index in range(1, 7))
    return replace(session, revision=6, performance_history=history)


def _payload(messages):
    return json.loads(messages[1].content.split('：', 1)[1])


def _judge_payload(engine, session, outcome, candidate, **kwargs):
    messages = evaluator._build_transition_judge_messages(
        engine, session, actor_performance=candidate, player_input='好。',
        transition_outcome=outcome, **kwargs)[0]
    return _payload(messages)


def test_recheck_only_trims_earlier_turns_and_keeps_absence_conservative():
    """改写后复检只保留最新完整回合，并把历史完整性按保守方向置为不完整。"""

    engine, session, outcome, candidate = _fixture(50)
    session = _long_history_session(engine, session)
    full = _judge_payload(engine, session, outcome, candidate)
    narrowed = _judge_payload(engine, session, outcome, candidate, recheck_only=True)

    assert len(full['scene_context']) > 1
    assert len(narrowed['scene_context']) == 1
    assert narrowed['scene_fact_index'] == []
    # 不允许凭缺项断言"从未发生"：裁掉更早回合后必须显式声明历史不完整。
    assert narrowed['current_visit_history_complete'] is False
    # 候选、本轮输入与作者合同不受收窄影响。
    assert narrowed['candidate_segments'] == full['candidate_segments']
    assert narrowed['player_input'] == full['player_input']
    assert narrowed['current_scene'] == full['current_scene']


def test_recheck_only_survives_emptied_index_under_pressure(monkeypatch):
    """收窄后仍超预算时，装箱循环必须能处理已置空的索引而不是崩溃。"""

    from services.theater.numeric_v2_budget import NUMERIC_V2_ACTOR_BUDGET_PROFILES

    engine, session, outcome, candidate = _fixture(950)
    session = _long_history_session(engine, session)
    monkeypatch.setitem(NUMERIC_V2_ACTOR_BUDGET_PROFILES['economy'], 'formal_judge_input_max_tokens', 1500)
    narrowed = _judge_payload(engine, session, outcome, candidate, recheck_only=True)
    assert narrowed['scene_fact_index'] == []
    assert narrowed['current_visit_history_complete'] is False


def test_missed_initiation_recheck_keeps_full_history():
    """补查漏判必须先看完整公开历史，不能套用改写后的定向收窄。"""

    engine, session, outcome, candidate = _fixture(200)
    session = _long_history_session(engine, session)
    full = _judge_payload(engine, session, outcome, candidate, check_missed_initiation=True)
    narrowed = _judge_payload(
        engine, session, outcome, candidate, check_missed_initiation=True, recheck_only=True)
    assert len(narrowed['scene_context']) == len(full['scene_context']) > 1


@pytest.mark.asyncio
async def test_judge_messages_are_built_before_client_and_deadline_covers_close(monkeypatch):
    """消息构造不占用时限，且连接、请求与关闭同属一个时限。"""

    engine, session, outcome, candidate = _fixture()
    built = []
    real_build = evaluator._build_transition_judge_messages

    def spy(*args, **kwargs):
        built.append(True)
        return real_build(*args, **kwargs)

    class SlowCloseClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            # 旧实现里关闭发生在时限之外，这里可以拖过时限而不报超时。
            await asyncio.sleep(0.5)
            return False

        async def ainvoke(self, messages):
            return SimpleNamespace(content=json.dumps(dict(
                offer_present=False, valid=False, body_violations=[],
                unsafe_suggestion_indexes=[], failure_reason='')))

    order = []

    async def config(_):
        return dict(model='test', base_url='http://test.invalid')

    async def factory(*args, **kwargs):
        order.append(bool(built))
        return SlowCloseClient()

    monkeypatch.setattr(evaluator, '_build_transition_judge_messages', spy)
    monkeypatch.setattr(evaluator, '_model_config', config)
    monkeypatch.setattr(evaluator, 'create_chat_llm_async', factory)
    monkeypatch.setattr(evaluator, 'NUMERIC_V2_TRANSITION_JUDGE_TIMEOUT_SECONDS', 0.05)

    worker = evaluator.NumericV2MetricEvaluator(object())
    with pytest.raises(evaluator.NumericV2EvaluatorError, match='transition_judge_timeout'):
        await worker.validate_transition_offer(
            engine=engine, session=session, message='好。',
            actor_performance=candidate, transition_outcome=outcome)

    # 先装配消息再建立连接，且关闭时间计入时限。
    assert order == [True]


def _violating_review(**kwargs):
    return evaluator.NumericV2TransitionOfferReview(
        False, False, ('author_boundary',), (), '需要修正初稿。')


async def _false() -> bool:
    return False


async def _none():
    return None


@pytest.mark.asyncio
async def test_dispute_review_switch_off_keeps_fast_review_and_rewrite(tmp_path, monkeypatch):
    """关闭争议复查只跳过第二次思考复查，快检、共享改稿与提交不变。"""

    from services.theater.numeric_v2_runtime import NumericV2Runtime, TurnRequestV2, MetricChangeV2
    from tests.unit.test_theater_numeric_v2_player_transition import initiation_case

    case = initiation_case()
    engine = case['engine']
    runtime = NumericV2Runtime(engine, tmp_path)
    current = await runtime.start_session(
        session_id='dispute_switch', catgirl_binding=_binding(),
        opening_performance=case['session'].opening_performance)

    calls = []
    generations = []

    async def evaluate(self, **kwargs):
        return evaluator.NumericV2EvaluationResult(
            (MetricChangeV2('trust', 2, '玩家兑现承诺', '我来帮你。'),), False)

    async def generate(self, **kwargs):
        generations.append(kwargs)
        return {'performance': '尚未提交的初稿。' if len(generations) == 1 else '修正后的最终回应。',
                'suggested_inputs': []}

    async def review(self, **kwargs):
        # 快检返回违规：开关开启时本应触发一次争议复查。
        calls.append(bool(kwargs.get('dispute_review')))
        return _violating_review(**kwargs)

    monkeypatch.setattr(numeric_v2_workflow.NumericV2MetricEvaluator, 'evaluate', evaluate)
    monkeypatch.setattr(numeric_v2_workflow.NumericV2MetricEvaluator, 'validate_transition_offer', review)
    monkeypatch.setattr(numeric_v2_workflow.NumericV2Actor, 'generate_turn', generate)
    monkeypatch.setattr(numeric_v2_workflow.NumericV2Actor, '_character_profile', lambda self: '温和。')
    monkeypatch.setattr(numeric_v2_workflow, '_dispute_review_enabled', _false)

    diagnostics = {}
    await numeric_v2_workflow.execute_numeric_v2_turn(
        config_manager=object(), runtime=runtime, current=current,
        turn=TurnRequestV2('new', current.session.revision, '我来帮你。'),
        ensure_current_binding=lambda _: _binding(), diagnostics_sink=diagnostics)

    assert diagnostics['dispute_review_enabled'] is False
    assert True not in calls
    assert diagnostics['dispute_review_attempts'] == 0
    # 快检与共享改稿仍然执行：关闭开关不是取消审核。
    assert calls and generations


@pytest.mark.asyncio
async def test_dispute_review_switch_defaults_off_when_unset(monkeypatch):
    """用户未设置或读取失败时默认关闭：争议复查是可选项，不是默认审核步骤。"""

    import utils.preferences as preferences

    monkeypatch.setattr(preferences, 'aload_theater_dispute_review', _none)
    assert await numeric_v2_workflow._dispute_review_enabled() is False


@pytest.mark.asyncio
async def test_review_budget_skips_later_rechecks_and_uses_existing_fallback(tmp_path, monkeypatch):
    """预算是等待上限：首次快检仍执行，改写后的复检被跳过并走既有末稿兜底。"""

    from services.theater.numeric_v2_runtime import NumericV2Runtime, TurnRequestV2, MetricChangeV2
    from tests.unit.test_theater_numeric_v2_player_transition import initiation_case

    case = initiation_case()
    engine = case['engine']
    runtime = NumericV2Runtime(engine, tmp_path)
    current = await runtime.start_session(
        session_id='review_budget', catgirl_binding=_binding(),
        opening_performance=case['session'].opening_performance)

    reviews = []
    generations = []

    async def evaluate(self, **kwargs):
        return evaluator.NumericV2EvaluationResult(
            (MetricChangeV2('trust', 2, '玩家兑现承诺', '我来帮你。'),), False)

    async def generate(self, **kwargs):
        generations.append(kwargs)
        return {'performance': '尚未提交的初稿。' if len(generations) == 1 else '修正后的最终回应。',
                'suggested_inputs': []}

    async def review(self, **kwargs):
        reviews.append(kwargs.get('recheck_only'))
        return _violating_review(**kwargs)

    monkeypatch.setattr(numeric_v2_workflow.NumericV2MetricEvaluator, 'evaluate', evaluate)
    monkeypatch.setattr(numeric_v2_workflow.NumericV2MetricEvaluator, 'validate_transition_offer', review)
    monkeypatch.setattr(numeric_v2_workflow.NumericV2Actor, 'generate_turn', generate)
    monkeypatch.setattr(numeric_v2_workflow.NumericV2Actor, '_character_profile', lambda self: '温和。')
    monkeypatch.setattr(numeric_v2_workflow, 'NUMERIC_V2_REVIEW_BUDGET_SECONDS', 0.0)

    diagnostics = {}
    result = await numeric_v2_workflow.execute_numeric_v2_turn(
        config_manager=object(), runtime=runtime, current=current,
        turn=TurnRequestV2('new', current.session.revision, '我来帮你。'),
        ensure_current_binding=lambda _: _binding(), diagnostics_sink=diagnostics)

    # 首次快检执行一次，改写后的复检按预算跳过，且走既有末稿兜底而不是无限等待。
    assert len(reviews) == 1
    assert len(generations) == 2
    assert diagnostics['review_budget_skips'] == 1
    assert diagnostics['semantic_review_fallback'] is True
    assert result.stored.session.revision == current.session.revision + 1


@pytest.mark.asyncio
async def test_review_budget_exhaustion_keeps_transition_rollback(tmp_path, monkeypatch):
    """正式转场不能因预算跳过复检而提交：沿用未完成复核的回滚语义。"""

    from services.theater.numeric_v2_runtime import NumericV2Runtime, TurnRequestV2, MetricChangeV2
    from tests.unit.test_theater_numeric_v2_player_transition import initiation_case
    from tests.unit.test_theater_numeric_v2_transition_history import _candidate

    case = initiation_case()
    engine = case['engine']
    runtime = NumericV2Runtime(engine, tmp_path)
    current = await runtime.start_session(
        session_id='review_budget_transition', catgirl_binding=_binding(),
        opening_performance=case['session'].opening_performance)

    generations = []

    async def evaluate(self, **kwargs):
        # 必须真的跨幕，才能覆盖正式转场的"未完成复核不提交"分支。
        return evaluator.NumericV2EvaluationResult(
            (MetricChangeV2('trust', 2, '玩家兑现承诺', '我来帮你。'),), False,
            transition_intent='initiate', interaction_intent='scene_action')

    async def generate(self, **kwargs):
        generations.append(kwargs)
        return _candidate()

    async def review(self, **kwargs):
        return _violating_review(**kwargs)

    monkeypatch.setattr(numeric_v2_workflow.NumericV2MetricEvaluator, 'evaluate', evaluate)
    monkeypatch.setattr(numeric_v2_workflow.NumericV2MetricEvaluator, 'validate_transition_offer', review)
    monkeypatch.setattr(numeric_v2_workflow.NumericV2Actor, 'generate_turn', generate)
    monkeypatch.setattr(numeric_v2_workflow.NumericV2Actor, '_character_profile', lambda self: '温和。')
    monkeypatch.setattr(numeric_v2_workflow, 'NUMERIC_V2_REVIEW_BUDGET_SECONDS', 0.0)

    with pytest.raises(NumericV2ActorOutputError, match='numeric_v2_transition_review_failed'):
        await numeric_v2_workflow.execute_numeric_v2_turn(
            config_manager=object(), runtime=runtime, current=current,
            turn=TurnRequestV2('new', current.session.revision, '我来帮你。'),
            ensure_current_binding=lambda _: _binding())
    # 事务回滚：未提交的换场不能进入存档。
    assert await runtime.restore_session('review_budget_transition') == current


def test_review_budget_is_a_wait_limit_only():
    """预算只限制等待，不替代授权、去向或提交判定。"""

    assert NUMERIC_V2_REVIEW_BUDGET_SECONDS > 0
    assert evaluator.NUMERIC_V2_DISPUTE_JUDGE_TIMEOUT_SECONDS < NUMERIC_V2_REVIEW_BUDGET_SECONDS
    assert evaluator.NUMERIC_V2_DISPUTE_JUDGE_TIMEOUT_SECONDS > evaluator.NUMERIC_V2_TRANSITION_JUDGE_TIMEOUT_SECONDS
