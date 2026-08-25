"""验证 Numeric v2 两个模型角色都只调用一次且不越过职责边界。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.theater import numeric_v2_actor, numeric_v2_evaluator
from services.theater.numeric_v2_actor import (
    NumericV2Actor,
    NumericV2ActorError,
    NumericV2ActorOutputError,
    _parse_output,
)
from services.theater.numeric_v2_cast import NumericV2CastProjection
from services.theater.numeric_v2_evaluator import NumericV2MetricEvaluator
from services.theater.numeric_v2_performance import content_blocks, mixed_performance_blocks
from services.theater.numeric_v2_runtime import (
    NumericV2Engine,
    NumericV2RuntimeError,
    TurnRequestV2,
)
from tests.unit.test_theater_numeric_v2_contract import numeric_v2_story
from utils.tokenize import count_tokens


class _ConfigManager:
    async def aget_model_api_config(self, purpose: str) -> dict:
        return {
            "model": f"test-{purpose}",
            "base_url": "https://model.invalid/v1",
            "api_key": "test-key",
            "provider_type": "openai",
        }

    def load_characters(self) -> dict:
        return {"当前猫娘": "测试猫娘", "主人": {"昵称": "哥哥"}}


class _FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[list] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.content)


def _session(engine: NumericV2Engine):
    return engine.create_session(
        session_id="model_contract",
        catgirl_binding={
            "catgirl_id": "catgirl:test",
            "catgirl_name": "测试猫娘",
            "player_address": "哥哥",
        },
        opening_performance={"narration": "开场", "dialogue": [], "suggested_inputs": []},
    )


def test_numeric_v2_actor_projects_relationship_band_stage_without_raw_values():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())

    assert numeric_v2_actor._band_projection(engine, {"trust": 20}) == {
        "trust": {"label": "戒备", "stage": "lowest"},
    }
    assert numeric_v2_actor._band_projection(engine, {"trust": 50}) == {
        "trust": {"label": "试探", "stage": "middle"},
    }
    assert numeric_v2_actor._band_projection(engine, {"trust": 80}) == {
        "trust": {"label": "信赖", "stage": "highest"},
    }
    assert "20" not in json.dumps(
        numeric_v2_actor._band_projection(engine, {"trust": 20}),
        ensure_ascii=False,
    )


def test_numeric_v2_actor_combines_metric_and_scene_relationship_limits():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["catgirl_situation"] = (
        "她在观察玩家是否可信。关系上限：亲密；称呼已知。"
    )
    engine = NumericV2Engine.from_mapping(story)

    low = numeric_v2_actor._relationship_control(engine, story["nodes"][0], {"trust": 20})
    high = numeric_v2_actor._relationship_control(engine, story["nodes"][0], {"trust": 80})

    assert low["metric_ceiling"] == "guarded"
    assert low["scene_ceiling"] == "intimate"
    assert low["effective_stage"] == "guarded"
    assert high["effective_stage"] == "intimate"
    assert "主动撒娇" in "、".join(low["forbidden_behaviors"])
    assert "无限授权" in "、".join(low["forbidden_behaviors"])
    assert "讨好式乖巧" in low["response_contract"]
    assert "保留判断、条件或边界" in low["response_contract"]
    assert "推荐输入也必须保持戒备边界" in low["response_contract"]


def test_numeric_v2_models_prefer_structured_story_beat_contract():
    """新结构化合同必须覆盖兼容文本解析，并把目标身份与证据原样交给两个模型角色。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat.update({
        "summary": "你最终决定是否留下，这是作者计划。",
        "opening_scene": "雨水沿着花店玻璃缓慢滑落。",
        "relationship_ceiling": "guarded",
        "catgirl_situation": "关系上限：亲密。她仍在检查旧信。",
        "goals": [{
            "id": "confirm_old_letter",
            "owner": "catgirl",
            "description": "女主说明旧信一直由她保管。",
            "evidence": {
                "mode": "exact",
                "anchors": ["旧信一直由我保管"],
            },
        }],
    })
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name="哥哥",
        catgirl_name="测试猫娘",
    )

    acting_beat = numeric_v2_actor._beat_for_actor(cast, engine.nodes["start"]["story_beat"])
    evaluating_beat = numeric_v2_evaluator._story_beat_for_evaluator(
        cast,
        engine.nodes["start"]["story_beat"],
    )
    relationship = numeric_v2_actor._relationship_control(
        engine,
        engine.nodes["start"],
        {"trust": 80},
    )

    expected_goal = {
        "goal_id": "confirm_old_letter",
        "owner": "catgirl",
        # 女主是结构化合同的内部角色标签；Actor 的输出合同继续禁止把它写进可见正文。
        "text": "女主说明旧信一直由她保管。",
        "evidence": {
            "mode": "exact",
            "anchors": ["旧信一直由我保管"],
        },
    }
    assert acting_beat["opening_scene"] == "雨水沿着花店玻璃缓慢滑落。"
    assert acting_beat["pending_goals"] == [expected_goal]
    assert evaluating_beat["pending_goals"] == [expected_goal]
    assert relationship["scene_ceiling"] == "guarded"


def test_numeric_v2_evaluator_enforces_structured_exact_goal_evidence():
    """精确证据必须来自目标 owner 可拥有的已提交内容，玩家提示不能代替猫娘交付。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "confirm_old_letter",
        "owner": "catgirl",
        "description": "女主明确确认旧信保管状态。",
        "evidence": {
            "mode": "exact",
            "anchors": ["旧信一直由我保管"],
        },
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    incomplete = replace(
        _session(engine),
        node_turn_count=1,
        revision=1,
        performance_history=({
            "revision": 1,
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "请确认旧信一直由你保管。",
            "performance": "（按住信封）我还需要核对封口。",
        },),
    )
    complete = replace(
        incomplete,
        performance_history=({
            **incomplete.performance_history[0],
            "performance": "（按住信封）旧信一直由我保管。",
        },),
    )

    downgraded = numeric_v2_evaluator._parse_output(
        json.dumps({"scene_complete": True, "metric_changes": {}}),
        engine,
        "旧信一直由你保管，对吗？",
        incomplete,
    )
    accepted = numeric_v2_evaluator._parse_output(
        json.dumps({
            "scene_complete": True,
            "metric_changes": {},
            "goal_evidence": {"confirm_old_letter": [1]},
        }),
        engine,
        "我明白了。",
        complete,
    )

    assert downgraded.scene_complete is False
    assert accepted.scene_complete is True
    assert accepted.goal_evidence == {"confirm_old_letter": (1,)}


def test_numeric_v2_evaluator_accepts_current_input_for_player_goal():
    """玩家本轮完成自有目标时应使用即将提交的服务端 revision。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "player_promise",
        "owner": "player",
        "description": "玩家明确承诺留下修好设备。",
        "evidence": {"mode": "semantic", "anchors": []},
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)

    result = numeric_v2_evaluator._parse_output(
        json.dumps({
            "scene_complete": True,
            "metric_changes": {},
            "goal_evidence": {"player_promise": [1]},
        }),
        engine,
        "我会留下把设备修好。",
        session,
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("player_goal_turn", 0, "我会留下把设备修好。"),
        (),
        scene_complete=False,
        goal_evidence=result.goal_evidence,
    )

    assert result.scene_complete is True
    assert result.goal_evidence == {"player_promise": (1,)}
    assert outcome.session.scene_goal_evidence == {"player_promise": (1,)}


def test_numeric_v2_evaluator_deterministically_records_complete_exact_goal():
    """精确目标全部出现在正确 owner 的正式内容后，不依赖模型重复抄写证据。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "startup_state",
        "owner": "catgirl",
        "description": "女主报告两项启动状态。",
        "evidence": {
            "mode": "exact",
            "anchors": ["视觉校准完成", "主存储区为空"],
        },
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        _session(engine),
        revision=1,
        node_turn_count=1,
        performance_history=({
            "revision": 1,
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "报告当前状态。",
            "performance": "（电子眼完成聚焦）视觉校准完成。主存储区为空。",
        },),
    )

    result = numeric_v2_evaluator._parse_output(
        json.dumps({"scene_complete": False, "metric_changes": {}, "goal_evidence": {}}),
        engine,
        "继续。",
        session,
    )

    assert result.scene_complete is True
    assert result.goal_evidence == {"startup_state": (1,)}


def test_numeric_v2_evaluator_completes_scene_when_all_structured_goal_evidence_exists():
    """结构化目标证据已经齐全时，模型返回 false 也不能继续拖住当前幕。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [
        {
            "id": "first_fact",
            "owner": "catgirl",
            "description": "女主说明第一项事实。",
            "evidence": {"mode": "semantic", "anchors": []},
        },
        {
            "id": "second_fact",
            "owner": "catgirl",
            "description": "女主说明第二项事实。",
            "evidence": {"mode": "semantic", "anchors": []},
        },
    ]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        _session(engine),
        revision=2,
        node_turn_count=2,
        scene_goal_evidence={"first_fact": (1,)},
        performance_history=(
            {
                "revision": 1,
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "先说第一件事。",
                "performance": "（翻开记录）第一项事实已经说明。",
            },
            {
                "revision": 2,
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "再说第二件事。",
                "performance": "（指向下一行）第二项事实也已经说明。",
            },
        ),
    )

    result = numeric_v2_evaluator._parse_output(
        json.dumps({
            "scene_complete": False,
            "metric_changes": {},
            "goal_evidence": {"second_fact": [2]},
        }),
        engine,
        "继续。",
        session,
    )

    assert result.scene_complete is True
    assert result.goal_evidence == {"second_fact": (2,)}


def test_numeric_v2_evaluator_does_not_guess_structured_semantic_goal_from_keywords():
    """结构化 semantic 目标只由 Evaluator 判断，词面交集不能冒充完成状态。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "observe_tools",
        "owner": "catgirl",
        "description": "女主观察当前可见的魔法修复工具并产生兴趣。",
        "evidence": {"mode": "semantic", "anchors": []},
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        _session(engine),
        revision=1,
        node_turn_count=1,
        performance_history=({
            "revision": 1,
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "看看这些工具。",
            "performance": "（看向魔法修复工具）我还不知道它们有什么用。",
        },),
    )

    result = numeric_v2_evaluator._parse_output(
        json.dumps({"scene_complete": False, "metric_changes": {}, "goal_evidence": {}}),
        engine,
        "继续。",
        session,
    )

    assert result.goal_evidence == {}


def test_numeric_v2_evaluator_omits_committed_structured_goals_from_pending_list():
    """已提交的结构化目标不再占用后续判定和演绎上下文。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [
        {
            "id": "goal_done",
            "owner": "catgirl",
            "description": "女主确认旧信仍在。",
            "evidence": {"mode": "semantic", "anchors": []},
        },
        {
            "id": "goal_pending",
            "owner": "catgirl",
            "description": "女主决定下一步核对方式。",
            "evidence": {"mode": "semantic", "anchors": []},
        },
    ]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), scene_goal_evidence={"goal_done": (0,)})

    messages = numeric_v2_evaluator._build_messages(engine, session, "请继续。")
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert [
        goal["goal_id"]
        for goal in payload["current_story_beat"]["pending_goals"]
    ] == ["goal_pending"]
    assert "即使 scene_complete=false" in messages[0].content
    assert "描述工具能力或限制" in messages[0].content


def test_numeric_v2_evaluator_requires_evidence_for_every_structured_goal_before_completion():
    """模型自报完成不能绕过仍缺失的结构化目标证据。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [
        {
            "id": "goal_done",
            "owner": "catgirl",
            "description": "女主已经观察旧信。",
            "evidence": {"mode": "semantic", "anchors": []},
        },
        {
            "id": "goal_missing",
            "owner": "catgirl",
            "description": "女主说明下一步决定。",
            "evidence": {"mode": "semantic", "anchors": []},
        },
    ]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), scene_goal_evidence={"goal_done": (0,)})

    result = numeric_v2_evaluator._parse_output(
        json.dumps({"scene_complete": True, "metric_changes": {}, "goal_evidence": {}}),
        engine,
        "继续。",
        session,
    )

    assert result.scene_complete is False


def test_numeric_v2_evaluator_does_not_reopen_latched_scene_goals():
    """Runtime 已锁存本幕完成后，证据清理不能让 Evaluator 再次要求旧目标。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "goal_done",
        "owner": "catgirl",
        "description": "女主已经观察旧信。",
        "evidence": {"mode": "semantic", "anchors": []},
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), scene_completion_ready=True)

    messages = numeric_v2_evaluator._build_messages(engine, session, "继续。")
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["scene_completion_latched"] is True
    assert payload["current_story_beat"]["pending_goals"] == []


def test_numeric_v2_actor_relationship_semantics_do_not_use_keyword_regex_guards():
    assert not hasattr(numeric_v2_actor, "_LOW_RELATION_OBEDIENCE_RE")
    assert not hasattr(numeric_v2_actor, "_AUTONOMY_SURRENDER_RE")
    assert not hasattr(numeric_v2_actor, "_LOW_RELATION_SUGGESTION_CONTACT_RE")
    assert not hasattr(numeric_v2_actor, "_validate_relationship_performance")


def test_numeric_v2_actor_drops_duplicate_transition_style_before_fixed_context_fails(monkeypatch):
    prefix = "以下 JSON 是已确定性结算的本回合数据：\n"
    system_prompt = "固定规则"
    data = {
        "route_changed": True,
        "source_story_beat": "来源目标" * 80,
        "style_instruction": "重复风格建议" * 80,
        "recent_openings": [],
        "recent_suggestions": [],
        "recent_context": [],
        "player_input": "继续。",
    }
    essential = dict(data)
    essential.pop("source_story_beat")
    essential.pop("style_instruction")
    limit = count_tokens(system_prompt) + count_tokens(
        prefix + json.dumps(essential, ensure_ascii=False, separators=(",", ":"))
    )
    monkeypatch.setattr(numeric_v2_actor, "NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS", limit)

    fitted = numeric_v2_actor._fit_turn_prompt_data(
        system_prompt=system_prompt,
        human_prefix=prefix,
        data=data,
    )

    assert "source_story_beat" not in fitted
    assert "style_instruction" not in fitted
    assert fitted["player_input"] == "继续。"


def test_numeric_v2_actor_applies_new_relationship_band_on_next_turn():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["catgirl_situation"] = "关系上限：亲密。"
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("relationship_delay", 0, "我会尊重你的决定。"),
        (),
    )
    outcome = replace(
        outcome,
        session=replace(outcome.session, metrics={"trust": 80}),
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我会尊重你的决定。",
        "温柔但重视自主选择。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["acting_context"]["relationship_control"]["effective_stage"] == "guarded"
    assert "服从性表述或交权" in payload["relationship_guard"]


def test_numeric_v2_actor_keeps_legacy_goal_progress_without_question_recovery():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(
        _session(engine),
        revision=1,
        node_turn_count=1,
        processed_client_turn_ids=("previous",),
        scene_goal_evidence={"goal.1": (1,)},
        performance_history=({
            "revision": 1,
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "把旧信放到桌上。",
            "performance": "（看向旧信）可以先告诉我它从哪里来吗？",
        },),
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("goal_recovery", 1, "外面雨停了吗？"),
        (),
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "外面雨停了吗？",
        "安静而认真。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["current_story_beat"]["goal_progress"]["goal.1"] == {
        "status": "in_progress",
        "evidence_revisions": [1],
    }
    # 最近完整回合已经包含这个问题；额外回收字段会诱导 Actor 无视当前输入反复追问。
    assert "interaction_recovery" not in payload


def test_numeric_v2_actor_omits_settled_structured_goal_immediately():
    """本轮 Evaluator 新确认的结构化目标必须立刻从 Actor 待办中移除。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "confirm_old_letter",
        "owner": "catgirl",
        "description": "女主确认旧信仍在桌上。",
        "evidence": {"mode": "semantic", "anchors": []},
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("settled_goal", 0, "我知道了。"),
        (),
        goal_evidence={"confirm_old_letter": (0,)},
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我知道了。",
        "安静而认真。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert session.scene_goal_evidence == {}
    assert payload["current_story_beat"]["pending_goals"] == []
    # 新包已直接从待办移除已完成目标，不再把同一状态重复展开到 Prompt。
    assert "goal_progress" not in payload["current_story_beat"]


def test_numeric_v2_actor_omits_empty_semantic_evidence_contract():
    """语义证据由 Evaluator 判断，Actor 不重复接收没有锚点的空合同。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "confirm_old_letter",
        "owner": "catgirl",
        "description": "女主确认旧信仍在桌上。",
        "evidence": {"mode": "semantic", "anchors": []},
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("semantic_goal_projection", 0, "请继续。"),
        (),
        scene_complete=False,
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "请继续。",
        "安静而认真。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["current_story_beat"]["pending_goals"] == [{
        "goal_id": "confirm_old_letter",
        "owner": "catgirl",
        "text": "女主确认旧信仍在桌上。",
    }]


def test_numeric_v2_actor_keeps_latched_scene_goals_out_of_pending_list():
    """Runtime 清理完成幕证据后，Actor 仍必须知道整组目标已经结算。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "confirm_old_letter",
        "owner": "catgirl",
        "description": "女主确认旧信仍在桌上。",
        "evidence": {"mode": "semantic", "anchors": []},
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("latched_goal", 0, "我知道了。"),
        (),
        scene_complete=True,
        goal_evidence={"confirm_old_letter": (0,)},
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我知道了。",
        "安静而认真。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert outcome.session.scene_completion_ready is True
    assert outcome.session.scene_goal_evidence == {}
    assert payload["current_story_beat"]["pending_goals"] == []


def test_numeric_v2_actor_inverts_negative_relationship_metric():
    story = numeric_v2_story()
    story["metric_schema"]["trust"]["relationship_effect"] = "negative"
    story["nodes"][0]["story_beat"]["catgirl_situation"] = "关系上限：亲密。"
    engine = NumericV2Engine.from_mapping(story)

    control = numeric_v2_actor._relationship_control(engine, story["nodes"][0], {"trust": 80})

    assert control["effective_stage"] == "guarded"


def test_numeric_v2_actor_uses_target_scene_relationship_limit_after_transition():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["catgirl_situation"] = "关系上限：亲密。"
    story["nodes"][1]["story_beat"]["catgirl_situation"] = "关系上限：戒备。"
    engine = NumericV2Engine.from_mapping(story)
    cast = NumericV2CastProjection.from_story(
        story,
        catgirl_name="测试猫娘",
        player_name="哥哥",
    )

    context = numeric_v2_actor._acting_context(
        engine,
        cast,
        story["nodes"][0],
        {"trust": 80},
        "温柔体贴。",
        target=story["nodes"][1],
    )

    assert context["relationship_control"]["effective_stage"] == "intimate"
    assert context["target_relationship_control"]["effective_stage"] == "guarded"
    # 换场目标关系合同最后交付，确保目标开场不会沿用来源幕的亲密许可。
    assert list(context).index("relationship_control") < list(context).index(
        "target_relationship_control"
    )


def test_numeric_v2_actor_projects_acting_contract_after_persona_before_relationship():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "fresh_boot",
        "memory_state": "empty",
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
        "assertable_self_facts": ["视觉校准完成", "主存储区为空"],
        "allowed_behaviors": ["自检", "观察", "确认环境"],
        "forbidden_behaviors": ["使用角色卡自称", "虚构旧记忆"],
    }
    engine = NumericV2Engine.from_mapping(story)
    cast = NumericV2CastProjection.from_story(
        story,
        catgirl_name="测试猫娘",
        player_name="你",
    )

    context = numeric_v2_actor._acting_context(
        engine,
        cast,
        story["nodes"][0],
        {"trust": 20},
        "自称：人家。甜美治愈。",
    )

    assert context["acting_contract"]["self_reference_mode"] == "system_neutral"
    assert context["acting_contract"]["memory_state"] == "empty"
    assert context["acting_contract"]["assertable_self_facts"] == [
        "视觉校准完成",
        "主存储区为空",
    ]
    assert "自称：" not in context["core_persona"]
    assert "story_identity" not in context
    assert "story_role_context" not in context
    assert "current_scene_state" not in context
    assert "knowledge_scope" not in context
    assert "其余背景、身份、历史和关系均未知" in numeric_v2_actor._story_context_for_actor(
        cast,
        story,
        beats=(story["nodes"][0]["story_beat"],),
    )["knowledge_scope"]
    assert numeric_v2_actor._beat_for_actor(
        cast,
        story["nodes"][0]["story_beat"],
    )["scene_direction"] == ""
    assert list(context).index("core_persona") < list(context).index("acting_contract")
    assert list(context).index("acting_contract") < list(context).index("relationship_control")
    assert "priority_rule" not in context
    assert "modulation_rule" not in context
    assert "内部状态与诊断结论完整清单" not in numeric_v2_actor._system_prompt(
        catgirl_name="测试猫娘",
        player_address="你",
        player_address_known=False,
    )


def test_numeric_v2_actor_style_only_persona_excludes_identity_and_relationship_facts():
    contract = {
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
    }

    projected = numeric_v2_actor._profile_for_acting_contract(
        "\n".join([
            "昵称: 小葵",
            "自称: 人家",
            "核心特质: 甜美治愈,元气满满",
            "行为特点: 听到夸奖会开心地摇尾巴",
            "关系定位: 从小一起长大的邻家妹妹",
            "小葵会照顾哥哥的生活习惯。",
        ]),
        contract,
    )

    assert projected == "\n".join([
        "核心特质: 甜美治愈,元气满满",
        "行为特点: 听到夸奖会开心地摇尾巴",
    ])


def test_numeric_v2_actor_rejects_explicit_persona_self_reference_under_neutral_contract():
    contract = {
        "self_reference_mode": "system_neutral",
    }

    with pytest.raises(NumericV2ActorOutputError, match="acting_contract_violation"):
        numeric_v2_actor._assert_acting_contract_output(
            {"performance": "人家还不能确认自己的身份。"},
            character_profile="自称：人家。甜美治愈。",
            acting_contract=contract,
        )

    numeric_v2_actor._assert_acting_contract_output(
        {"performance": "我还不能确认自己的身份。"},
        character_profile="自称：人家。甜美治愈。",
        acting_contract=contract,
    )


def test_numeric_v2_actor_rejects_transition_bridge_that_acts_for_player():
    performance = {
        "segments": [
            {"phase": "source_response", "performance": "（抬眼观察）先等一下。"},
            {
                "phase": "transition_bridge",
                "scene_narration": "你迅速切断主电源，将小葵转移到隔间。",
            },
            {"phase": "target_opening", "performance": "（耳尖微动）天亮了。"},
        ],
        "suggested_inputs": [],
    }

    with pytest.raises(NumericV2ActorOutputError, match="transition_player_action"):
        numeric_v2_actor._assert_transition_bridge_player_ownership(
            performance,
            player_address="老谢",
        )

    numeric_v2_actor._assert_transition_bridge_player_ownership(
        {
            **performance,
            "segments": [
                performance["segments"][0],
                {
                    "phase": "transition_bridge",
                    "scene_narration": "窗外的警笛渐远，夜色在雨声中退去。",
                },
                performance["segments"][2],
            ],
        },
        player_address="老谢",
    )


def test_numeric_v2_turn_request_rejects_input_over_token_limit():
    with pytest.raises(NumericV2RuntimeError, match="numeric_turn_input_too_long"):
        TurnRequestV2.from_mapping({
            "client_turn_id": "long_input",
            "base_revision": 0,
            "message": "测试 " * 141,
        })


def test_numeric_v2_scene_context_starts_after_latest_node_reentry():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(
        _session(engine),
        current_node_id="start",
        node_turn_count=1,
        performance_history=(
            {"from_node_id": "start", "to_node_id": "start", "input_text": "旧访问"},
            {"from_node_id": "other", "to_node_id": "start", "input_text": "重新进入"},
            {"from_node_id": "start", "to_node_id": "start", "input_text": "本次访问"},
        ),
    )

    context = numeric_v2_evaluator._current_scene_context(session)

    # 重新进入当前节点的那条输入属于上一幕，只保留其新幕可见内容，不重复判定旧输入。
    assert [item["player_input"] for item in context] == ["", "本次访问"]
    assert [item["phase"] for item in context] == ["scene_entry", "turn"]
    assert all(item["player_input"] != "旧访问" for item in context)


def test_numeric_v2_evaluator_budget_preserves_complete_scene_facts():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    performances = {
        index: (
            f"（测试猫娘把第 {index} 件物品收进柜子）"
            + f"这是第 {index} 轮已经发生的完整事实。" * 16
            + "我允许你暂时留在客厅，但只能使用公共区域，不要进入卧室。"
            + "（她把剪刀放进抽屉并关好）"
            + f"第 {index} 轮末尾事实必须完整保留。"
        )
        for index in range(1, 13)
    }
    session = replace(
        _session(engine),
        current_node_id="start",
        node_turn_count=12,
        performance_history=tuple(
            {
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": f"继续处理第 {index} 件事情",
                "performance": performances[index],
            }
            for index in range(1, 13)
        ),
    )
    messages = numeric_v2_evaluator._build_messages(engine, session, "我会遵守这些边界。")
    raw_tokens = sum(count_tokens(message.content) for message in messages)
    bounded = numeric_v2_evaluator.bound_prompt_messages(
        messages,
        max_tokens=numeric_v2_evaluator.NUMERIC_V2_EVALUATOR_INPUT_MAX_TOKENS,
        field_max_tokens=numeric_v2_evaluator.NUMERIC_V2_EVALUATOR_INPUT_MAX_TOKENS,
        system_max_tokens=numeric_v2_evaluator.NUMERIC_V2_EVALUATOR_INPUT_MAX_TOKENS,
    )

    assert raw_tokens <= numeric_v2_evaluator.NUMERIC_V2_EVALUATOR_INPUT_MAX_TOKENS
    assert [message.content for message in bounded] == [message.content for message in messages]
    payload = json.loads(bounded[1].content.split("：\n", 1)[1])
    retained = [*payload["current_scene_context"], *payload["recent_context"]]
    retained_turns = [item for item in retained if item["phase"] == "turn"]

    assert len(retained_turns) < 12
    assert retained_turns[-1]["player_input"] == "继续处理第 12 件事情"
    assert "继续处理第 1 件事情" not in bounded[1].content
    for item in retained_turns:
        index = int(item["player_input"].split()[1])
        expected_blocks = [
            {key: value for key, value in block.items() if key != "speaker_id"}
            for block in mixed_performance_blocks(performances[index])
        ]
        assert item["content"] == expected_blocks
        assert all("speaker_id" not in block for block in item["content"])
        assert f"第 {index} 轮末尾事实必须完整保留。" in item["content"][-1]["text"]


@pytest.mark.asyncio
async def test_numeric_v2_evaluator_keeps_structured_system_prompt_without_silent_truncation(monkeypatch):
    """结构化目标增加系统规则后，仍应完整调用一次 Evaluator。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "startup_memory_check",
        "owner": "catgirl",
        "description": "女主明确说出启动检查结论。",
        "evidence": {"mode": "exact", "anchors": ["主存储区为空"]},
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    client = _FakeClient(json.dumps({
        "scene_complete": False,
        "metric_changes": {},
        "goal_evidence": {},
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_evaluator, "create_chat_llm_async", fake_create)

    await NumericV2MetricEvaluator(_ConfigManager()).evaluate(
        engine=engine,
        session=_session(engine),
        message="先完成自检。",
    )

    expected = numeric_v2_evaluator._build_messages(
        engine,
        _session(engine),
        "先完成自检。",
    )
    assert len(client.calls) == 1
    assert [item.content for item in client.calls[0]] == [item.content for item in expected]


def test_numeric_v2_evaluator_projects_only_target_opening_after_long_transition():
    story = numeric_v2_story()
    story["nodes"][0]["route_gates"][0]["target_node_id"] = "second"
    transition_contract = story["nodes"][0]["route_gates"][0]["transition_contract"]
    story["nodes"].insert(1, {
        "id": "second",
        "type": "scene",
            "chapter": "第二幕",
        "min_turns": 1,
        "recommended_turns": 4,
            "story_beat": {
                "summary": "第二幕的桌面上摊开了新的事实记录。",
                "must_happen": ["测试猫娘说明新线索的来源"],
            "must_not_happen": [],
            "catgirl_situation": "测试猫娘正在检查新线索。",
            "transition_goal": "确认线索后继续前进。",
        },
        "route_gates": [{
            "id": "second_to_stay",
            "target_node_id": "ending_stay",
            "priority": 10,
            "conditions": {"all": []},
            "transition_contract": transition_contract,
        }],
    })
    engine = NumericV2Engine.from_mapping(story)
    source_response = "（收好旧物）上一幕已经结束。" * 80
    transition_bridge = "两人穿过很长的走廊，换场过程持续了很久。" * 80
    target_opening = "第二幕从安静的资料室开始。" * 20
    session = replace(
        _session(engine),
        current_node_id="second",
        node_turn_count=0,
        performance_history=({
            "from_node_id": "start",
            "to_node_id": "second",
            "input_text": "这是触发上一幕完成的玩家输入",
            "segments": [
                {"phase": "source_response", "performance": source_response},
                {"phase": "transition_bridge", "scene_narration": transition_bridge},
                {
                    "phase": "target_opening",
                    "scene_narration": target_opening,
                    "performance": "（翻开资料夹）我们从这条线索开始查。",
                },
            ],
        },),
    )

    messages = numeric_v2_evaluator._build_messages(engine, session, "我先核对线索上的日期。")
    payload = json.loads(messages[1].content.split("：\n", 1)[1])
    context = [*payload["current_scene_context"], *payload["recent_context"]]

    assert sum(count_tokens(message.content) for message in messages) <= (
        numeric_v2_evaluator.NUMERIC_V2_EVALUATOR_INPUT_MAX_TOKENS
    )
    assert context == [{
        "phase": "scene_entry",
        "player_input": "",
        "content": [
            {"type": "narration", "text": target_opening},
            {"type": "narration", "text": "翻开资料夹"},
            {"type": "dialogue", "text": "我们从这条线索开始查。"},
        ],
    }]
    assert source_response not in messages[1].content
    assert transition_bridge not in messages[1].content
    assert "这是触发上一幕完成的玩家输入" not in messages[1].content


def test_numeric_v2_evaluator_keeps_recorded_goal_evidence_outside_recent_window():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["must_happen"] = [
        "测试猫娘摄入能量、理解生命，并提出跟随玩家外出。",
    ]
    engine = NumericV2Engine.from_mapping(story)
    records = []
    for revision in range(1, 7):
        performance = (
            "（接上能量电池）核心温度恢复正常。"
            if revision == 1
            else f"（检查第{revision}项数据）继续确认当前状态。"
        )
        records.append({
            "revision": revision,
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": f"继续第{revision}项检查",
            "performance": performance,
        })
    session = replace(
        _session(engine),
        node_turn_count=6,
        revision=6,
        performance_history=tuple(records),
        scene_goal_evidence={"goal.1": (1,)},
    )

    messages = numeric_v2_evaluator._build_messages(engine, session, "接下来谈谈生命和外出。")
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert [item["revision"] for item in payload["recent_context"]] == [3, 4, 5, 6]
    assert [item["revision"] for item in payload["retained_goal_context"]] == [1]
    assert "接上能量电池" in json.dumps(payload["retained_goal_context"], ensure_ascii=False)


def test_numeric_v2_evaluator_returns_validated_goal_evidence_revisions():
    story = numeric_v2_story()
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        _session(engine),
        node_turn_count=1,
        revision=1,
        performance_history=({
            "revision": 1,
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "先听你说明。",
            "performance": "（取出旧信）这就是我一直保留的证据。",
        },),
    )

    result = numeric_v2_evaluator._parse_output(
        json.dumps({
            "scene_complete": False,
            "metric_changes": {},
            "goal_evidence": {"goal.1": [1]},
        }),
        engine,
        "请继续。",
        session,
    )

    assert result.goal_evidence == {"goal.1": (1,)}


def test_numeric_v2_evaluator_caps_merged_goal_evidence_at_eight_revisions(monkeypatch):
    """确定性证据合并必须逐条限幅，不能让单个目标整批越过八条上限。"""  # noqa: DOCSTRING_CJK
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    monkeypatch.setattr(
        numeric_v2_evaluator,
        "_deterministic_goal_evidence",
        lambda _engine, _session, **_kwargs: {
            "goal.1": (1, 2, 3, 4),
            "goal.2": (5, 6, 7, 8),
            "goal.3": (9, 10, 11),
        },
    )

    result = numeric_v2_evaluator._parse_output(
        json.dumps({
            "scene_complete": False,
            "metric_changes": {},
            "goal_evidence": {},
        }),
        engine,
        "继续。",
        session,
    )

    retained = {
        revision
        for revisions in result.goal_evidence.values()
        for revision in revisions
    }
    assert retained == set(range(1, 9))


def test_numeric_v2_cast_projects_multi_word_source_names():
    story = numeric_v2_story()
    story["intro"]["player_identity"] = "John Smith，回乡整理旧屋的年轻男性。"
    story["intro"]["catgirl_identity"] = "Mary Jane，经营花店、保留旧信的年轻女性。"

    projected = NumericV2CastProjection.from_story(
        story,
        player_name="哥哥",
        catgirl_name="测试猫娘",
    ).intro(story)

    assert projected["player_identity"] == "哥哥，回乡整理旧屋的年轻男性。"
    assert projected["catgirl_identity"] == "测试猫娘，经营花店、保留旧信的年轻女性。"


def test_numeric_v2_cast_defaults_missing_player_name_to_you():
    story = numeric_v2_story()

    projected = NumericV2CastProjection.from_story(
        story,
        player_name="",
        catgirl_name="测试猫娘",
    ).intro(story)

    assert projected["player_identity"].startswith("你，")


def test_numeric_v2_cast_projects_compound_lead_slot_once():
    story = numeric_v2_story()
    story["intro"]["player_identity"] = "男主，回乡整理旧屋的年轻男性。"
    story["intro"]["catgirl_identity"] = "女主，经营花店的年轻女性。"
    cast = NumericV2CastProjection.from_story(
        story,
        player_name="你",
        catgirl_name="小葵",
    )

    projected = cast.text("不得让第三角色取代男女主作出最终决定。")

    assert projected == "不得让第三角色取代你和小葵作出最终决定。"
    assert "男小葵" not in projected


def test_numeric_v2_actor_accepts_one_mixed_performance_with_interleaved_actions():
    text = "（小葵把记录本推到桌边）先看记录，再决定。（她压住被风吹起的纸角）看完再告诉我答案。"
    performance = _parse_output(json.dumps({
        "performance": text,
        "suggested_inputs": [],
    }, ensure_ascii=False))

    assert performance == {
        "performance": text,
        "suggested_inputs": [],
    }


def test_numeric_v2_actor_accepts_dialogue_only_turn():
    """普通回合可以只输出对白，微动作不是强制字段。"""  # noqa: DOCSTRING_CJK
    performance = _parse_output(json.dumps({
        "performance": "先坐下，我们慢慢说。",
        "suggested_inputs": [],
    }, ensure_ascii=False))

    assert performance["performance"] == "先坐下，我们慢慢说。"


def test_numeric_v2_actor_drops_indirect_suggestion_instructions():
    performance = _parse_output(json.dumps({
        "performance": "（把合同推到桌面中央）哥哥，先看看落款日期。",
        "suggested_inputs": [
            "解释自己已经付过房租并要求留下",
            '"全年房租已经付清，合同也写明了这间房的居住权。"',
            '把湿漉漉的合同递过去，"上面确实有我的名字。"',
            "保持距离，询问我目前的身体状态",
        ],
    }, ensure_ascii=False))

    assert performance["suggested_inputs"] == [
        "“全年房租已经付清，合同也写明了这间房的居住权。”",
        "把湿漉漉的合同递过去，“上面确实有我的名字。”",
    ]


def test_numeric_v2_actor_drops_parenthesized_and_placeholder_suggestions():
    """推荐输入必须是可直接发送的动作或台词，不能保留模型编辑占位。"""  # noqa: DOCSTRING_CJK

    performance = _parse_output(json.dumps({
        "performance": "（保持原位）我还不能确认这里是否安全。",
        "suggested_inputs": [
            "（摸摸她的头）先别怕",
            "(检查接口)确认供电",
            "告诉她：[你的名字]",
            "后退一步，给她留出自检空间",
        ],
    }, ensure_ascii=False))

    assert performance["suggested_inputs"] == ["后退一步，给她留出自检空间"]


def test_numeric_v2_actor_filters_questions_from_fresh_boot_opening_suggestions():
    """开机空白态的男主候选只保留陈述和动作，猫娘问句不能倒置进推荐输入。"""  # noqa: DOCSTRING_CJK

    performance = _parse_output(
        json.dumps({
            "scene_narration": "指示灯由红转绿，电子眼缓缓睁开。",
            "performance": "（保持静止）视觉校准完成。主存储区为空。",
            "suggested_inputs": [
                "“这里是哪里？你是谁？”",
                "“这里是我的工作室，我负责修复你。”",
                "后退一步，给她留出观察空间",
            ],
        }, ensure_ascii=False),
        opening_required=True,
        suggestion_questions_allowed=False,
    )

    assert performance["suggested_inputs"] == [
        "“这里是我的工作室，我负责修复你。”",
        "后退一步，给她留出观察空间",
    ]


def test_numeric_v2_actor_filters_questions_from_fresh_boot_normal_turn_suggestions():
    """开机空白节点不能在进入普通回合后重新放回角色倒置问句。"""  # noqa: DOCSTRING_CJK

    performance = _parse_output(
        json.dumps({
            "performance": "（电子耳廓转向门外）警笛声正在靠近。我需要更多现场信息。",
            "suggested_inputs": [
                "“外面警笛声很急，我们需要立刻转移吗？”",
                "“警笛正在靠近，我决定先关闭工作室灯光。”",
                "后退一步检查出口",
            ],
        }, ensure_ascii=False),
        suggestion_questions_allowed=False,
    )

    assert performance["suggested_inputs"] == [
        "“警笛正在靠近，我决定先关闭工作室灯光。”",
        "后退一步检查出口",
    ]


def test_numeric_v2_actor_rejects_repeated_transition_source_only():
    previous = {
        "performance": "（把契约收进抽屉）既然签了字，那现在就是正式员工啦。（指向后厨）先去厨房熟悉一下环境。",
    }
    transition = {
        "segments": [
            {
                "phase": "source_response",
                "performance": "（把契约小心收好）既然签了字，那现在就是正式员工啦。（指向后厨）先去厨房熟悉一下环境。",
            },
            {"phase": "transition_bridge", "scene_narration": "橡木门在身后合上。"},
            {"phase": "target_opening", "performance": "（竖起耳朵）厨房就在前面。"},
        ],
    }

    assert numeric_v2_actor._transition_source_repeats_previous(transition, previous) is True


def test_numeric_v2_actor_keeps_new_transition_source_response():
    previous = {
        "performance": "（把契约收进抽屉）既然签了字，那现在就是正式员工啦。（指向后厨）先去厨房熟悉一下环境。",
    }
    transition = {
        "segments": [{
            "phase": "source_response",
            "performance": "（侧身让开走廊）好，跟人家来吧。",
        }],
    }

    assert numeric_v2_actor._transition_source_repeats_previous(transition, previous) is False


def test_numeric_v2_actor_rejects_narration_only_turn():
    with pytest.raises(NumericV2ActorOutputError, match="numeric_v2_actor_dialogue_required"):
        _parse_output(json.dumps({
            "performance": "（她只是沉默地看着门口）",
            "suggested_inputs": [],
        }, ensure_ascii=False))


def test_numeric_v2_mixed_performance_parser_keeps_arbitrary_interleaving():
    blocks = mixed_performance_blocks(
        "（把咖啡推近）先暖暖手。你刚才说的事……（抬眼看向你）我答应了。再说一遍也可以。"
    )

    assert [block["type"] for block in blocks] == [
        "narration",
        "dialogue",
        "narration",
        "dialogue",
    ]
    assert blocks[1]["text"] == "先暖暖手。你刚才说的事……"
    assert blocks[3]["text"] == "我答应了。再说一遍也可以。"
    assert mixed_performance_blocks("（没有闭合的动作后面对白会静音") == []
    assert mixed_performance_blocks("（动作（不能嵌套））对白") == []


def test_numeric_v2_scene_narration_does_not_drop_final_mixed_block():
    """独立场景旁白不能占用混合正文的内容块上限。"""  # noqa: DOCSTRING_CJK

    performance = "".join(f"（动作{i}）对白{i}" for i in range(8))
    blocks = content_blocks({
        "scene_narration": "换场后的完整场景旁白。",
        "performance": performance,
    })

    assert len(blocks) == 17
    assert blocks[0] == {"type": "narration", "text": "换场后的完整场景旁白。"}
    assert blocks[-1] == {
        "type": "dialogue",
        "speaker_id": "active_catgirl",
        "text": "对白7",
    }


def test_numeric_v2_actor_history_uses_complete_current_scene_visit():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(
        _session(engine),
        current_node_id="start",
        node_turn_count=3,
        performance_history=(
            {
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "上一轮访问，不应继续发送",
                "performance": "（摆手）这是旧访问。",
            },
            {
                "from_node_id": "other",
                "to_node_id": "start",
                "input_text": "重新进入当前场景",
                "performance": "（推开门）我们回来了。",
            },
            *(
                {
                    "from_node_id": "start",
                    "to_node_id": "start",
                    "input_text": f"当前场景第 {index} 轮",
                    "performance": f"（点头）这是当前场景第 {index} 次回应。",
                }
                for index in range(1, 7)
            ),
        ),
    )

    history = numeric_v2_actor._history(session, max_tokens=2200)

    assert [row["player_input"] for row in history] == [
        "重新进入当前场景",
        *(f"当前场景第 {index} 轮" for index in range(1, 7)),
    ]
    assert all(row["player_input"] != "上一轮访问，不应继续发送" for row in history)


def test_numeric_v2_actor_history_excludes_previous_scene_response_after_transition():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(
        _session(engine),
        current_node_id="other",
        node_turn_count=1,
        performance_history=(
            {
                "from_node_id": "start",
                "to_node_id": "other",
                "input_text": "签下上一幕的契约",
                "segments": [
                    {
                        "phase": "source_response",
                        "performance": "（收起契约）契约已经签好了。",
                    },
                    {
                        "phase": "transition_bridge",
                        "scene_narration": "次日清晨，厨房亮起灯。",
                    },
                    {
                        "phase": "target_opening",
                        "performance": "（递出锅铲）来试试早餐吧。",
                    },
                ],
            },
            {
                "from_node_id": "other",
                "to_node_id": "other",
                "input_text": "端出做好的早餐",
                "performance": "（尝了一口）味道不错。",
            },
        ),
    )

    history = numeric_v2_actor._history(session, max_tokens=2200)

    assert history[0]["player_input"] == ""
    assert [segment["phase"] for segment in history[0]["segments"]] == [
        "transition_bridge",
        "target_opening",
    ]
    assert "契约已经签好了" not in json.dumps(history, ensure_ascii=False)
    assert history[-1]["player_input"] == "端出做好的早餐"


def test_numeric_v2_actor_history_drops_old_turns_without_truncating_retained_turn():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    older = {
        "from_node_id": "start",
        "to_node_id": "start",
        "input_text": "较早输入必须整轮抛弃",
        "performance": "（整理旧纸张）这是较早事实。" * 20,
    }
    latest = {
        "from_node_id": "start",
        "to_node_id": "start",
        "input_text": "最新输入必须完整保留",
        "performance": "（把完整记录推到桌边）这是最新事实，不能被截成半句。",
    }
    session = replace(
        _session(engine),
        node_turn_count=2,
        performance_history=(older, latest),
    )
    latest_row = numeric_v2_actor._history_row(latest)
    budget = numeric_v2_actor._json_tokens([latest_row])

    history = numeric_v2_actor._history(session, max_tokens=budget)

    assert history == [latest_row]
    assert "较早输入必须整轮抛弃" not in json.dumps(history, ensure_ascii=False)
    assert "…" not in json.dumps(history, ensure_ascii=False)


def test_numeric_v2_actor_turn_fields_are_not_truncated():
    story = numeric_v2_story()
    role_overlay = "她暂时负责守护花店，但会完整保留每一条剧情身份事实。" * 6
    scene_state = "她正在逐项核对桌上的旧信、钥匙和雨伞，并保持当前角色卡的表达方式。" * 6
    goal = "测试猫娘完整说明旧信的来源、保存过程和当前归属，不得省略任何已经写入剧本的事实。" * 4
    story["catgirl_binding"]["role_overlay"] = role_overlay
    story["nodes"][0]["story_beat"]["catgirl_situation"] = scene_state
    story["nodes"][0]["story_beat"]["must_happen"] = [goal]
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("full_actor_fields", 0, "请继续完整说明。"),
        (),
        scene_complete=False,
    )
    core_persona = "温柔、认真，并且会用完整句子表达自己的边界。" * 12

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "请继续完整说明。",
        core_persona,
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["acting_context"]["story_role_context"] == role_overlay.replace("小岚", "测试猫娘")
    assert payload["acting_context"]["current_scene_state"] == scene_state.replace("小岚", "测试猫娘")
    assert payload["acting_context"]["core_persona"] == core_persona
    assert payload["current_story_beat"]["pending_goals"] == [goal.replace("小岚", "测试猫娘")]


@pytest.mark.asyncio
async def test_numeric_v2_evaluator_calls_once_and_cannot_see_routes(monkeypatch):
    story = numeric_v2_story()
    story["metric_schema"]["trust"]["increase_criteria"] = ["玩家兑现对小岚的承诺"]
    engine = NumericV2Engine.from_mapping(story)
    client = _FakeClient(json.dumps({
        "scene_complete": False,
        "metric_changes": {
            "trust": {
                "strength": "normal",
                "criterion_id": "trust.increase.1",
            },
        }
    }, ensure_ascii=False))
    create_calls = []

    async def fake_create(*args, **kwargs):
        create_calls.append((args, kwargs))
        return client

    monkeypatch.setattr(numeric_v2_evaluator, "create_chat_llm_async", fake_create)
    evaluation = await NumericV2MetricEvaluator(_ConfigManager()).evaluate(
        engine=engine,
        session=_session(engine),
        message="我会留下来把话说完。",
        recent_ledger_events=({
            "result_revision": 1,
            "metric_changes": [{
                "metric_id": "trust",
                "delta": 1,
                "criterion": "玩家兑现对小岚的承诺",
            }],
        },),
    )

    assert evaluation.scene_complete is False
    # 同一关系依据已在最近四回合奖励，模型再次返回时由服务端确定性冷却。
    assert evaluation.metric_changes == ()
    assert len(create_calls) == len(client.calls) == 1
    assert create_calls[0][1]["max_retries"] == 0
    prompt = "\n".join(str(message.content) for message in client.calls[0])
    assert "玩家兑现对测试猫娘的承诺" in prompt
    assert "玩家兑现对小岚的承诺" not in prompt
    assert "trust.increase.1" in prompt
    assert '"relationship_effect":"positive"' in prompt
    assert '"criterion_id":"trust.increase.1","delta":1' in prompt
    assert "只能选择 weak、normal、strong、decisive" in prompt
    assert "每项数值每回合最多一次" in prompt
    assert "只有本幕所有 pending_goals" in prompt
    assert "玩家纠正最近演绎中的错误事实" in prompt
    assert "要求共同决定、核对事实或设置协作边界" in prompt
    assert "笼统要求观察、检查、确认与保证安全" in prompt
    assert '"phase":"opening"' in prompt
    assert '"content":[{"type":"narration","text":"开场"}]' in prompt
    assert '"current_scene_context"' in prompt
    assert "不要求逐字复述目标" in prompt
    assert "已经明确包含全部 pending_goals" in prompt
    assert "尚未达到 recommended_turns" in prompt
    assert '"decision_order"' in prompt
    assert "target_node_id" not in prompt
    assert "route_gates" not in prompt
    assert '"pending_goals"' in prompt
    assert '"summary"' not in prompt
    assert '"phase":"opening"' in prompt
    assert '"content":[{"type":"narration","text":"开场"}]' in prompt
    assert '"metric_change_reasons"' not in prompt


def test_numeric_v2_evaluator_rejects_exact_repeated_input_beyond_recent_cooldown():
    """相同完整输入即使隔开四回合，也不能再次刷同一条非关系数值依据。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    story["metric_schema"]["trust"]["relationship_effect"] = "none"
    engine = NumericV2Engine.from_mapping(story)
    repeated_input = "我会兑现承诺。"
    ledger_events = (
        {
            "result_revision": 1,
            "input_text": repeated_input,
            "metric_changes": [{
                "metric_id": "trust",
                "delta": 1,
                "criterion": "玩家兑现承诺",
            }],
        },
        *(
            {
                "result_revision": revision,
                "input_text": f"无关输入 {revision}",
                "metric_changes": [],
            }
            for revision in range(2, 7)
        ),
    )
    model_output = json.dumps({
        "scene_complete": False,
        "metric_changes": {
            "trust": {
                "strength": "weak",
                "criterion_id": "trust.increase.1",
            },
        },
    })

    repeated = numeric_v2_evaluator._parse_output(
        model_output,
        engine,
        repeated_input,
        _session(engine),
        ledger_events,
    )
    new_evidence = numeric_v2_evaluator._parse_output(
        model_output,
        engine,
        "这一次我已经兑现了新的承诺。",
        _session(engine),
        ledger_events,
    )

    assert repeated.metric_changes == ()
    assert len(new_evidence.metric_changes) == 1


def test_numeric_v2_evaluator_marks_goal_owner_and_separates_player_evidence():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["must_happen"] = [
        "小岚将刻有9月17日的旧雨伞挂回高处，并说明主人等待了一夜。",
    ]
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        _session(engine),
        node_turn_count=1,
        performance_history=({
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "是不是9月17日？",
            "performance": "（把旧雨伞挂回高处）它的主人等了一整夜。",
        },),
    )

    messages = numeric_v2_evaluator._build_messages(engine, session, "就是9月17日吧？")
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["current_story_beat"]["pending_goals"] == [{
        "goal_id": "goal.1",
        "owner": "catgirl",
        "text": "测试猫娘将刻有9月17日的旧雨伞挂回高处，并说明主人等待了一夜。",
    }]
    assert "玩家输入中的请求、猜测、提示或复述不能补齐 catgirl 目标" in messages[0].content
    assert payload["recent_context"][-1]["player_input"] == "是不是9月17日？"
    assert payload["player_input"] == "就是9月17日吧？"
    assert "并列子条件、数量、期限和范围" in messages[0].content
    assert "不能补齐 catgirl 目标" in messages[0].content
    assert "含糊确认" in messages[0].content
    assert "scene_completion_guard" not in payload
    assert "metric_evidence_guard" not in payload


def test_numeric_v2_actor_hides_restricted_chapter_title():
    """章节标题是作者主题锚点，受限认知角色不能把其中地点当成已知事实。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    story["nodes"][0]["chapter"] = "雾谷初遇"
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "limited",
        "memory_state": "partial",
        "self_reference_mode": "persona_allowed",
        "persona_scope": "full",
        "allowed_behaviors": ["观察当前环境"],
        "forbidden_behaviors": ["断言未知地点"],
    }
    engine = NumericV2Engine.from_mapping(story)
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name="你",
        catgirl_name="测试猫娘",
    )

    assert numeric_v2_actor._chapter_title_for_actor(cast, engine.nodes["start"]) == ""


def test_numeric_v2_evaluator_downgrades_missing_explicit_catgirl_phrase():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["must_happen"] = [
        "女主必须在对白中明确说出“试营业期间保留原有经营方式”。",
    ]
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        _session(engine),
        node_turn_count=1,
        performance_history=({
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "试营业期间保留原有经营方式，可以吗？",
            "performance": "（合上账本）那就这么定。",
        },),
    )

    result = numeric_v2_evaluator._parse_output(
        json.dumps({"scene_complete": True, "metric_changes": {}}),
        engine,
        "我会尊重你的决定。",
        session,
    )

    assert result.scene_complete is False


def test_numeric_v2_evaluator_accepts_explicit_catgirl_phrase():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["must_happen"] = [
        "女主必须在对白中明确说出“试营业期间保留原有经营方式”。",
    ]
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        _session(engine),
        node_turn_count=1,
        performance_history=({
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "由你决定是否保留。",
            "performance": "（合上账本）试营业期间保留原有经营方式，就这么定。",
        },),
    )

    result = numeric_v2_evaluator._parse_output(
        json.dumps({"scene_complete": True, "metric_changes": {}}),
        engine,
        "我会尊重你的决定。",
        session,
    )

    assert result.scene_complete is True


def test_numeric_v2_evaluator_downgrades_incomplete_compound_disclosure():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["must_happen"] = [
        "女主向男主展示契约并明确说明：抵债期限三个月、岗位为首席酒保兼主厨、每日工作上限八小时、每周休息两天、损坏物品按实际损失赔偿且没有翻倍违约金。",
    ]
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        _session(engine),
        node_turn_count=2,
        performance_history=(
            {
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "先看看契约。",
                "performance": "（把契约拍在桌上）期限三个月，岗位是首席酒保兼主厨。",
            },
            {
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "还有其他条款吗？",
                "performance": "（点了点赔偿条款）不会有翻倍赔偿。",
            },
        ),
    )

    result = numeric_v2_evaluator._parse_output(
        json.dumps({"scene_complete": True, "metric_changes": {}}),
        engine,
        "那就签字。",
        session,
    )

    assert result.scene_complete is False


def test_numeric_v2_evaluator_accepts_complete_compound_disclosure():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["must_happen"] = [
        "女主向男主展示契约并明确说明：抵债期限三个月、岗位为首席酒保兼主厨、每日工作上限八小时、每周休息两天、损坏物品按实际损失赔偿且没有翻倍违约金。",
    ]
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        _session(engine),
        node_turn_count=2,
        performance_history=(
            {
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "请把条款说清楚。",
                "performance": "（摊开契约）期限三个月，岗位是首席酒保兼主厨，每天最多八小时，每周休息两天。",
            },
            {
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "赔偿怎么算？",
                "performance": "（点向末行）损坏只按实际损失赔偿，不会翻倍。",
            },
        ),
    )

    result = numeric_v2_evaluator._parse_output(
        json.dumps({"scene_complete": True, "metric_changes": {}}),
        engine,
        "条款已经清楚了。",
        session,
    )

    assert result.scene_complete is True


@pytest.mark.asyncio
async def test_numeric_v2_actor_calls_once_and_only_returns_performance(monkeypatch):
    story = numeric_v2_story()
    full_background = story["intro"]["background"] + "雨夜租约与双方的合法居住依据必须完整保留。" * 12
    full_player_identity = story["intro"]["player_identity"] + "男主已经支付全年租金并拥有当前房屋的居住依据。" * 8
    full_catgirl_identity = story["intro"]["catgirl_identity"] + "女主必须在警惕中逐步确认双方都不是非法闯入者。" * 8
    story["intro"] = {
        "background": full_background,
        "player_identity": full_player_identity,
        "catgirl_identity": full_catgirl_identity,
    }
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), node_turn_count=1)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("actor_turn", 0, "我先听你说。"),
        (),
        scene_complete=True,
    )
    client = _FakeClient(json.dumps({
        "segments": [
            {
                "phase": "source_response",
                "performance": "（认真听完）那就先坐一会儿。",
            },
            {
                "phase": "transition_bridge",
                "scene_narration": "夜色渐深，花店终于安静下来。",
            },
            {
                "phase": "target_opening",
                # 旧模型返回的目标开场旁白必须被 Runtime 丢弃并替换为剧本原文。
                "scene_narration": "两人接受这次仍然会分别。测试猫娘把旧信重新收进抽屉。",
                "performance": "（按住旧信一角）这一次，把信读完吧。",
            },
        ],
        "suggested_inputs": ["这些年你过得怎么样？"],
    }, ensure_ascii=False))
    create_calls = []

    async def fake_create(*args, **kwargs):
        create_calls.append((args, kwargs))
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_character_profile", lambda *args, **kwargs: "安静而认真。")
    monkeypatch.setattr(numeric_v2_actor, "_load_player_address", lambda *args, **kwargs: "哥哥")
    performance = await NumericV2Actor(_ConfigManager()).generate_turn(
        engine=engine,
        session=session,
        outcome=outcome,
        player_input="我先听你说。",
    )

    assert performance == {
        "suggested_inputs": ["这些年你过得怎么样？"],
        "segments": [
            {
                "phase": "source_response",
                "performance": "（认真听完）那就先坐一会儿。",
            },
            {
                "phase": "transition_bridge",
                "scene_narration": "夜色渐深，花店终于安静下来。",
            },
            {
                "phase": "target_opening",
                "scene_narration": "雨停后的长街恢复了安静。",
                "performance": "（按住旧信一角）这一次，把信读完吧。",
            },
        ],
        "transition_delivered": True,
        "visible_node_id": "ending_leave",
    }
    assert len(create_calls) == len(client.calls) == 1
    assert create_calls[0][1]["max_retries"] == 0
    prompt = "\n".join(str(message.content) for message in client.calls[0])
    turn_payload = json.loads(client.calls[0][1].content.split("：\n", 1)[1])
    assert {"route_changed", "turn_instruction", "recent_context", "soft_pacing"}.issubset(turn_payload)
    assert "minimum_turns_before_route" not in turn_payload
    assert turn_payload["current_chapter_title"] == "重逢"
    assert turn_payload["target_chapter_title"] == "决定"
    assert turn_payload["soft_pacing"]["phase"] == "transition"
    assert turn_payload["transition_contract"]["reason"] == "当前信任度满足作者路线条件。"
    assert "若它们声明结果来自双方共同选择" in turn_payload["transition_contract"]["source_response_rule"]
    assert "source_response 禁止复用其中任何完整动作或对白" in turn_payload["response_instruction"]
    assert "我先听你说。" in prompt
    assert "不得创造节点、路线、数值、关键剧情事实或结局" in prompt
    assert "氛围和非关键细节" in prompt
    assert "本剧男主不由 Actor 扮演" in prompt
    assert "本剧男主由你扮演" not in prompt
    assert "performance 不得替男主创作或执行身份披露" in prompt
    assert "本剧女主由当前猫娘扮演" in prompt
    assert "performance 的括号微动作只能用第三人称描写猫娘的动作、神态和可见环境" in prompt
    assert "即使玩家明确输入了动作，也不要在括号中复述、补全或改写" in prompt
    assert "不得用“你”“您”或“哥哥”作为动作与状态主体" in prompt
    assert "不得把饮品与食物混成同一物品" in prompt
    assert "冲突发生后必须先回应并化解或延续冲突" in prompt
    assert "动作与对白按实际顺序自然穿插" in prompt
    assert "不得突然追问、回答或引用画面中从未发生的言行" in prompt
    assert "玩家说‘可以、如果、愿意、打算、改天’" in prompt
    assert "不得据此在旁白中替玩家转身、离开、靠近、触碰、站立" in prompt
    assert "换场按 source_response、transition_bridge、target_opening 三段输出" in prompt
    assert "必须先回应 player_input 并收住来源节点" in prompt
    assert "再桥接 transition_contract.must_deliver" in prompt
    assert "目标 opening_scene 由 Runtime 确定性交付" in prompt
    assert "括号外全部是当前猫娘实际说出口的对白" in prompt
    assert "章节标题只作软主题锚点" in prompt
    assert "不得覆盖玩家输入、已发生记录、目标、边界和过渡合同" in prompt
    assert "text inside Chinese full-width parentheses is a visible micro-action" in prompt
    assert "do not target a fixed number of actions" in prompt
    assert "no longer than 18 CJK characters or 12 words" in prompt
    assert "are not subject to the micro-action length rule" in prompt
    assert "36 CJK characters" not in prompt
    assert "Describe motion, not a static emotional explanation" in prompt
    assert "Parentheses must be balanced and cannot be nested" in prompt
    assert "普通回合只输出 performance、suggested_inputs" not in prompt
    assert "不能为了数量机械拆句或插动作" in prompt
    assert "不要照抄其他剧本、人物、地点、物品或推荐语" in prompt
    # 换场调用只接收三段式职责，不再携带普通回合和开场的无关协议。
    assert "普通回合要完整但精简" not in prompt
    assert "通常写 1 到 3 句对白" not in prompt
    assert "可原样发送的直接台词或动作" in prompt
    assert "动作省略男主主语" in prompt
    assert "混合项用中文引号标出台词" in prompt
    assert "第一人称台词或动作" not in prompt
    assert "不得写成“解释、询问、表示、保证、提出、展示、选择”等操作说明" in prompt
    assert "若输入含 suggestion_instruction，必须按其中 mode 组织候选" in prompt
    assert "主线引导时至少第一条直接推动 pending_goals" in prompt
    assert "强收束时全部候选都直接服务 pending_goals" in prompt
    assert "最终回复必须且只能是一个可由 JSON.parse 直接解析的 JSON object" in prompt
    assert "禁止输出 Markdown 代码围栏、JSON 前后解释、标题或任何额外文字" in prompt
    assert "顶层字段必须且只能是 segments:object[]" in prompt
    assert "顶层字段必须且只能是 performance:string" not in prompt
    assert "顶层字段必须且只能是 scene_narration:string" not in prompt
    assert "relationship_control.response_contract 是本轮正文和推荐输入的直接演绎合同" in prompt
    assert "不能在写完越界内容后只用解释、标签或自我声明声称合规" in prompt
    assert "我也是被中介骗来的" not in prompt
    assert turn_payload["story_context"]["background"] == full_background
    assert turn_payload["story_context"]["player_identity"] == full_player_identity.replace("林舟", "哥哥", 1)
    acting_context = turn_payload["acting_context"]
    assert acting_context["core_persona"] == "安静而认真。"
    # JSON 字段顺序也属于单次模型调用的提示优先级：关系合同必须在核心人格之后收口。
    assert list(acting_context).index("core_persona") < list(acting_context).index(
        "relationship_control"
    )
    assert acting_context["story_identity"] == full_catgirl_identity.replace("小岚", "测试猫娘", 1)
    assert acting_context["story_role_context"] == "她既期待重逢，又担心玩家再次离开。"
    assert acting_context["current_scene_state"] == "她在观察玩家是否可信。"
    assert "target_scene_state" not in acting_context
    assert acting_context["relationship_control"]["effective_stage"] == "guarded"
    assert "metric_states" not in acting_context["relationship_control"]
    assert "priority_rule" not in acting_context
    assert "modulation_rule" not in acting_context
    assert "role_overlay" not in turn_payload
    assert "current_metric_bands" not in turn_payload
    assert "catgirl_expression_profile" not in turn_payload
    assert "style_instruction" not in turn_payload
    assert '"runtime_target_opening":"雨停后的长街恢复了安静。"' in prompt
    assert '"opening_scene"' not in json.dumps(turn_payload["target_story_beat"], ensure_ascii=False)
    assert '"source_story_beat"' not in prompt
    assert '"source_boundaries"' in prompt
    assert '"target_story_beat"' in prompt
    assert '"pending_goals"' in prompt
    assert '"summary"' not in prompt
    assert "哥哥，回乡整理旧屋的年轻男性" in prompt
    assert "测试猫娘，经营花店、保留旧信的年轻女性" in prompt
    assert "林舟" not in prompt
    assert "小岚" not in prompt


def test_numeric_v2_actor_rejects_route_change_without_structured_transition():
    with pytest.raises(NumericV2ActorOutputError, match="numeric_v2_actor_transition_required"):
        _parse_output(
            json.dumps({
                "content": [
                    {"type": "narration", "text": "仍停留在旧场景。"},
                    {"type": "dialogue", "speaker_id": "active_catgirl", "text": "还没说完。"},
                ],
                "suggested_inputs": [],
            }, ensure_ascii=False),
            transition_required=True,
            target_node_id="next_scene",
            target_opening="第二天清晨，雨已经停了。",
        )


def test_numeric_v2_actor_normalizes_model_transition_phase_labels():
    result = _parse_output(
        json.dumps({
            "segments": [
                {
                    "phase": "normal",
                    "performance": "（她收起残页）那就一起查下去。",
                },
                {
                    "phase": "bridge",
                    "scene_narration": "最后一班慢车驶离，站台安静下来。",
                },
                {
                    "phase": "opening",
                    "performance": "（她拉住衣袖）哥哥，看那盏灯。",
                },
            ],
            "suggested_inputs": ["我顺着她指的方向看去。"],
        }, ensure_ascii=False),
        transition_required=True,
    )

    assert [segment["phase"] for segment in result["segments"]] == [
        "source_response",
        "transition_bridge",
        "target_opening",
    ]


def test_numeric_v2_actor_removes_runtime_opening_from_transition_bridge():
    result = _parse_output(
        json.dumps({
            "segments": [
                {"phase": "source", "performance": "（收起旧伞）我们继续查。"},
                {
                    "phase": "bridge",
                    "scene_narration": (
                        "小葵将旧伞挂回高处。"
                        "最后一班慢车驶离后，整个车站陷入死寂。"
                    ),
                },
                {"phase": "opening", "performance": "（指向灯光）哥哥，看那里。"},
            ],
            "suggested_inputs": [],
        }, ensure_ascii=False),
        transition_required=True,
        target_opening="最后一班慢车驶离后，整个车站陷入死寂。",
    )

    assert result["segments"][1]["scene_narration"] == "小葵将旧伞挂回高处。"


def test_numeric_v2_actor_omits_visible_bridge_when_only_target_opening_remains():
    result = _parse_output(
        json.dumps({
            "segments": [
                {"phase": "source", "performance": "（放下餐盘）说好了，不许反悔。"},
                {
                    "phase": "bridge",
                    "scene_narration": "周末的客厅里，堆积如山的纸箱占据了大半空间。",
                },
                {"phase": "opening", "performance": "（从纸箱里探头）这些都是给人家的吗？"},
            ],
            "suggested_inputs": [],
        }, ensure_ascii=False),
        transition_required=True,
        target_opening="周末的客厅里，堆积如山的纸箱占据了大半空间。",
    )

    assert result["segments"][1] == {
        "phase": "transition_bridge",
        "scene_narration": "",
    }
    assert "时间向前流转" not in json.dumps(result, ensure_ascii=False)


def test_numeric_v2_actor_compares_transition_bridge_with_each_opening_sentence():
    result = _parse_output(
        json.dumps({
            "segments": [
                {"phase": "source", "performance": "（收起账本）那就这么说定了。"},
                {
                    "phase": "bridge",
                    "scene_narration": (
                        "旧幕的谈话终于告一段落。"
                        "清晨的厨房里，新鲜食材已经摆满操作台。"
                    ),
                },
                {"phase": "opening", "performance": "（系好围裙）哥哥，准备开工啦。"},
            ],
            "suggested_inputs": [],
        }, ensure_ascii=False),
        transition_required=True,
        target_opening="清晨的厨房烟雾缭绕。新鲜食材整齐摆在操作台上。",
    )

    assert result["segments"][1]["scene_narration"] == "旧幕的谈话终于告一段落。"


def test_numeric_v2_actor_leaves_target_time_anchor_to_runtime_opening():
    result = _parse_output(
        json.dumps({
            "segments": [
                {"phase": "source", "performance": "（收起剪刀）那就先这样。"},
                {
                    "phase": "bridge",
                    "scene_narration": (
                        "雨声渐渐歇止，旧幕的争执告一段落。"
                        "合租的第二天傍晚，屋内还留着淡淡的肥皂清香。"
                    ),
                },
                {"phase": "opening", "performance": "（揉了揉肚子）冰箱里还有什么呀？"},
            ],
            "suggested_inputs": [],
        }, ensure_ascii=False),
        transition_required=True,
        target_opening="合租第二天傍晚，冰箱里只剩下一条冷冻秋刀鱼。",
    )

    assert result["segments"][1]["scene_narration"] == "雨声渐渐歇止，旧幕的争执告一段落。"


def test_numeric_v2_actor_keeps_explicit_time_progression_before_target_opening():
    """桥段可交代时间推进，但仍由 Runtime 独占目标场景画面。"""  # noqa: DOCSTRING_CJK

    result = _parse_output(
        json.dumps({
            "segments": [
                {"phase": "source", "performance": "（收起检测线）先休息。"},
                {
                    "phase": "bridge",
                    "scene_narration": "雨夜过去，时间推进到次日清晨。",
                },
                {"phase": "opening", "performance": "（抬起视线）这里变亮了。"},
            ],
            "suggested_inputs": [],
        }, ensure_ascii=False),
        transition_required=True,
        target_opening="清晨的阳光透过百叶窗落进工作室。",
    )

    assert result["segments"][1]["scene_narration"] == "雨夜过去，时间推进到次日清晨。"


def test_numeric_v2_actor_leaves_target_goal_objects_out_of_transition_bridge():
    result = _parse_output(
        json.dumps({
            "segments": [
                {"phase": "source", "performance": "（收起残页）我们继续查。"},
                {
                    "phase": "bridge",
                    "scene_narration": (
                        "旧幕的核对暂时告一段落。"
                        "远处信号灯发出微弱红光，在浓雾中忽明忽暗。"
                    ),
                },
                {"phase": "opening", "performance": "（拉住衣袖）哥哥，看铁轨那里。"},
            ],
            "suggested_inputs": [],
        }, ensure_ascii=False),
        transition_required=True,
        target_opening="最后一班慢车驶离后，整个车站陷入死寂。",
        target_goals=["女主在猫形信号灯亮起时，拉住男主衣袖并指出铁轨刻痕。"],
    )

    assert result["segments"][1]["scene_narration"] == "旧幕的核对暂时告一段落。"


def test_numeric_v2_actor_filters_recent_similar_suggestions_without_failing_turn():
    suggestions = numeric_v2_actor._deduplicate_recent_suggestions(
        [
            "小心烫，先喝口味噌汤暖暖胃吧。",
            "我把鱼刺都挑干净了，放心吃吧。",
        ],
        ["小心烫，先喝口汤暖暖胃吧。"],
    )

    assert suggestions == ["我把鱼刺都挑干净了，放心吃吧。"]


def test_numeric_v2_actor_hides_suggestions_when_all_candidates_repeat():
    suggestions = numeric_v2_actor._deduplicate_recent_suggestions(
        ["小心烫，先喝口味噌汤暖暖胃吧。"],
        ["小心烫，先喝口汤暖暖胃吧。"],
    )

    assert suggestions == []


def test_numeric_v2_actor_keeps_target_entry_events_out_of_transition_bridge():
    story = numeric_v2_story()
    cast = NumericV2CastProjection.from_story(
        story,
        player_name="哥哥",
        catgirl_name="测试猫娘",
    )

    projected = numeric_v2_actor._transition_contract_for_actor(
        cast,
        {
            "reason": "信号灯引出退役车厢。",
            "must_deliver": [
                "女主交出旧怀表作为信任凭证。",
                "退役车厢的门在猫形信号灯下显露出来。",
            ],
            "must_preserve": ["信号灯仍然亮着。"],
            "tone": "神秘、庄重",
        },
        target_opening="猫形信号灯指向铁轨尽头的废弃侧线。",
        target_goals=[
            "女主将旧怀表交给男主保管。",
            "退役车厢的门打开，车内隐藏记录清晰可见。",
        ],
    )

    assert projected["must_deliver"] == []


@pytest.mark.asyncio
async def test_numeric_v2_actor_timeout_covers_model_config_resolution(monkeypatch):
    async def slow_model_config(_config_manager):
        await asyncio.sleep(0.05)
        return {}

    monkeypatch.setattr(numeric_v2_actor, "_model_config", slow_model_config)
    monkeypatch.setattr(numeric_v2_actor, "NUMERIC_V2_ACTOR_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(NumericV2ActorError, match="numeric_v2_actor_timeout"):
        await NumericV2Actor(_ConfigManager())._invoke([])


@pytest.mark.asyncio
async def test_numeric_v2_actor_receives_soft_pacing_closure_without_forcing_route(monkeypatch):
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(_session(engine), node_turn_count=3)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("pacing_turn", 0, "我准备休息了。"),
        (),
        scene_complete=False,
    )
    client = _FakeClient(json.dumps({
        "performance": "（收住话头，把灯调暗）那就先休息，明早再说。",
        "suggested_inputs": ["我先安静休息，明天再说。"],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_character_profile", lambda *args, **kwargs: "安静而认真。")
    monkeypatch.setattr(numeric_v2_actor, "_load_player_address", lambda *args, **kwargs: "哥哥")

    performance = await NumericV2Actor(_ConfigManager()).generate_turn(
        engine=engine,
        session=session,
        outcome=outcome,
        player_input="我准备休息了。",
    )

    payload = json.loads(client.calls[0][1].content.split("：\n", 1)[1])
    assert outcome.route is None
    assert outcome.route_status == "scene_incomplete"
    assert payload["current_chapter_title"] == "重逢"
    assert "target_chapter_title" not in payload
    assert "source_story_beat" not in payload
    assert "target_story_beat" not in payload
    assert "transition_contract" not in payload
    assert payload["soft_pacing"] == {
        "recommended_turns": 4,
        "current_turn": 4,
        "phase": "closure",
        "instruction": payload["soft_pacing"]["instruction"],
    }
    assert "不得替男主行动、判定 scene_complete 或自行换节点" in payload["soft_pacing"]["instruction"]
    assert "条件已具备就在本轮完成" in payload["soft_pacing"]["instruction"]
    assert "只能提供可忽略的选择" in payload["soft_pacing"]["instruction"]
    assert "不得编造老师催促、截止日期" in payload["soft_pacing"]["instruction"]
    assert payload["turn_instruction"] == "本回合留在当前节点，只推进当前互动所需的一小步。"
    assert payload["response_instruction"].startswith(
        "先回应 player_input；recent_context 是唯一已发生事实"
    )
    assert "新推荐不得只是近期候选的同义改写" in payload["response_instruction"]
    assert "不补具体身份、经历、约定或关键物品" in payload["response_instruction"]
    assert "来源幕已结束" in payload["response_instruction"]
    assert "不得回去重新签约" in payload["response_instruction"]
    assert "缺具体值就说明无法确认" in payload["factual_guard"]
    assert "suggested_inputs 是未发生候选" in payload["factual_guard"]
    assert "非关键氛围与细节可即兴" in payload["factual_guard"]
    assert "锁定关键物品" in payload["factual_guard"]
    assert "未确认外观、状态、名称、用途、能力、效果、反应、来历保持未知" in payload["factual_guard"]
    assert "未明示的模块" in payload["factual_guard"]
    assert "exact anchors 中 catgirl 只在括号外对白" in payload["factual_guard"]
    assert "每回合只推进当前交互所需的一小步" in client.calls[0][0].content
    assert "乖巧地点头" in payload["relationship_guard"]
    assert "已从 pending_goals 移除的目标" in payload["factual_guard"]
    assert payload["suggestion_instruction"]["goal_source"] == "current_story_beat.pending_goals"
    assert payload["suggestion_instruction"]["mode"] == "mainline_closure"
    assert "全部 suggested_inputs" in payload["suggestion_instruction"]["rule"]
    assert "点击后可原样发送" in payload["suggestion_instruction"]["rule"]
    assert "不得延伸琐事或开启新话题" in payload["suggestion_instruction"]["rule"]
    assert "跳过 recent_context 已发生部分" in payload["suggestion_instruction"]["rule"]
    assert "owner=catgirl/environment 由 performance 推进" in payload["suggestion_instruction"]["rule"]
    assert "候选只让男主回应或披露" in payload["suggestion_instruction"]["rule"]
    assert "不得照抄提问或反问猫娘" in payload["suggestion_instruction"]["rule"]
    assert "owner=player/shared" in payload["suggestion_instruction"]["rule"]
    assert "关键物品服从 factual_guard" in payload["suggestion_instruction"]["rule"]
    assert "不补未知答案" in payload["suggestion_instruction"]["rule"]
    assert "不得催迫男主本回合作决定" in payload["suggestion_instruction"]["rule"]
    assert "不得编造截止日期或外部催促" in payload["suggestion_instruction"]["rule"]
    assert "不得把关键物品与其他场景资料混为一物" in payload["relationship_guard"]
    assert performance["suggested_inputs"] == ["我先安静休息，明天再说。"]


def test_numeric_v2_actor_keeps_divergent_suggestions_on_focus_turn():
    """第七回合只预热主线焦点，推荐输入仍保留发散空间。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    story["nodes"][0]["min_turns"] = 8
    story["nodes"][0]["recommended_turns"] = 15
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), node_turn_count=6)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("pacing_focus_turn", 0, "我想先看看窗外。"),
        (),
        scene_complete=False,
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我想先看看窗外。",
        "安静而认真。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["soft_pacing"]["phase"] == "focus"
    assert payload["soft_pacing"]["current_turn"] == 7
    assert "下一回合将达到最短推进回合" in payload["soft_pacing"]["instruction"]
    assert payload["suggestion_instruction"]["mode"] == "diverge"
    assert "goal_source" not in payload["suggestion_instruction"]
    assert "不补未知属性、功能或结果" in payload["suggestion_instruction"]["rule"]
    assert "不要为了提前换幕" in payload["suggestion_instruction"]["rule"]


def test_numeric_v2_actor_closes_completed_scene_on_focus_turn():
    """目标提前完成时，第七回合收束当前话题，不再因待办为空继续无关发散。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    story["nodes"][0]["min_turns"] = 8
    story["nodes"][0]["recommended_turns"] = 15
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), node_turn_count=6)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("completed_focus_turn", 0, "我先把灯关上。"),
        (),
        scene_complete=True,
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我先把灯关上。",
        "安静而认真。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["soft_pacing"]["phase"] == "focus"
    assert "本幕目标已经完成" in payload["soft_pacing"]["instruction"]
    assert payload["suggestion_instruction"]["mode"] == "close_current"
    assert "不重演已完成目标" in payload["suggestion_instruction"]["rule"]
    assert "不开启新支线" in payload["suggestion_instruction"]["rule"]


def test_numeric_v2_actor_guides_mainline_from_min_turn_until_closure():
    """第八至十四回合保留自由输入，同时每回合提供一个明确主线入口。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    story["nodes"][0]["min_turns"] = 8
    story["nodes"][0]["recommended_turns"] = 15
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), node_turn_count=7)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("pacing_guided_turn", 0, "我还想聊聊别的。"),
        (),
        scene_complete=False,
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我还想聊聊别的。",
        "安静而认真。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert [
        numeric_v2_actor._soft_pacing(
            story["nodes"][0],
            turn,
            route_changed=False,
        )["phase"]
        for turn in (6, 7, 8, 14, 15)
    ] == ["normal", "focus", "guided", "guided", "closure"]
    assert payload["soft_pacing"]["phase"] == "guided"
    assert payload["soft_pacing"]["current_turn"] == 8
    assert "玩家仍可忽略推荐并自由输入" in payload["soft_pacing"]["instruction"]
    assert payload["suggestion_instruction"]["mode"] == "mainline_pull"
    assert payload["suggestion_instruction"]["goal_source"] == "current_story_beat.pending_goals"
    assert "第一条 suggested_inputs" in payload["suggestion_instruction"]["rule"]
    assert "其余候选可以探索相邻方向" in payload["suggestion_instruction"]["rule"]


@pytest.mark.asyncio
async def test_numeric_v2_actor_working_memory_preserves_latest_turn_facts(monkeypatch):
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    older = tuple({
        "from_node_id": "start",
        "to_node_id": "start",
        "input_text": f"旧回合 {index}",
        "content": [
            {"type": "narration", "text": "这是已经过去的场景细节。" * 32},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "这是较早的对话。" * 14},
        ],
    } for index in range(6))
    latest = {
        "from_node_id": "start",
        "to_node_id": "start",
        "input_text": "那我得洗澡啊",
        "content": [
            {
                "type": "narration",
                "text": "测试猫娘像是被踩到了尾巴的猫，原本慵懒蜷缩在沙发角落的身体瞬间紧绷。",
            },
            {
                "type": "dialogue",
                "speaker_id": "active_catgirl",
                "text": "你是听不懂人话，还是脑子被雨淋坏了？我刚才已经说过——不行！",
            },
            {
                "type": "narration",
                "text": "她猛地从沙发上站起来，双手抱胸，刻意后退了两步拉开距离。",
            },
            {
                "type": "dialogue",
                "speaker_id": "active_catgirl",
                "text": "浴室是我的私人领域，绝对、绝对不允许外人踏入半步。你想都别想。",
            },
            {
                "type": "narration",
                "text": "她深吸了一口气，视线在客厅角落里扫过，最终定格在一个落满灰尘的储物柜上。",
            },
            {
                "type": "dialogue",
                "speaker_id": "active_catgirl",
                "text": "那里有一条旧毛巾，虽然是干的，但肯定有霉味。你自己拿去擦擦，别指望我会给你找换洗的衣服。",
            },
        ],
        # 防重复候选故意占满辅助预算，用来验证裁剪顺序，而不放大必须保留的最新事实。
        "suggested_inputs": [
            (f"近期候选 {index}：继续围绕当前分歧提出一种不同说法。" * 30)
            for index in range(6)
        ],
    }
    session = replace(
        _session(engine),
        node_turn_count=7,
        performance_history=(*older, latest),
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("memory_turn", 0, "指出毛巾太脏无法使用，再次尝试沟通"),
        (),
        scene_complete=False,
    )
    client = _FakeClient(json.dumps({
        "performance": "（看了一眼储物柜）那就再想办法。",
        "suggested_inputs": [],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_character_profile", lambda *args, **kwargs: "安静而认真。")
    monkeypatch.setattr(numeric_v2_actor, "_load_player_address", lambda *args, **kwargs: "哥哥")

    await NumericV2Actor(_ConfigManager()).generate_turn(
        engine=engine,
        session=session,
        outcome=outcome,
        player_input="指出毛巾太脏无法使用，再次尝试沟通",
    )

    payload = json.loads(client.calls[0][1].content.split("：\n", 1)[1])
    latest_memory = payload["recent_context"][-1]
    assert sum(count_tokens(message.content) for message in client.calls[0]) <= 4800
    assert list(payload)[-1] == "relationship_guard"
    assert list(payload).index("factual_guard") < list(payload).index("relationship_guard")
    assert "interaction_recovery" not in payload
    assert payload["player_input"] == "指出毛巾太脏无法使用，再次尝试沟通"
    assert "先回应 player_input" in payload["response_instruction"]
    assert payload["acting_context"]["core_persona"] == "安静而认真。"
    assert payload["acting_context"]["relationship_control"]["effective_stage"] == "guarded"
    assert "保留判断、条件或边界" in payload["acting_context"]["relationship_control"]["response_contract"]
    assert "metric_states" not in payload["acting_context"]["relationship_control"]
    # 超预算时防重复辅助信息先整组舍弃，不能挤占最新完整演绎事实。
    assert payload["recent_openings"] == []
    assert "style_instruction" not in payload
    assert "suggestion_contract" not in payload
    assert latest_memory["player_input"] == "那我得洗澡啊"
    assert latest_memory["performance"] == (
        "（测试猫娘像是被踩到了尾巴的猫，原本慵懒蜷缩在沙发角落的身体瞬间紧绷。）"
        "你是听不懂人话，还是脑子被雨淋坏了？我刚才已经说过——不行！"
        "（她猛地从沙发上站起来，双手抱胸，刻意后退了两步拉开距离。）"
        "浴室是我的私人领域，绝对、绝对不允许外人踏入半步。你想都别想。"
        "（她深吸了一口气，视线在客厅角落里扫过，最终定格在一个落满灰尘的储物柜上。）"
        "那里有一条旧毛巾，虽然是干的，但肯定有霉味。你自己拿去擦擦，别指望我会给你找换洗的衣服。"
    )
    prompt = client.calls[0][1].content
    assert "旧回合 0" not in prompt
    for retained in payload["recent_context"][:-1]:
        index = int(retained["player_input"].split()[-1])
        assert retained == numeric_v2_actor._history_row(older[index])
    assert "落满灰尘的储物柜" in prompt
    assert "虽然是干的，但肯定有霉味" in prompt
    assert "你自己拿去擦擦" in prompt
    assert "绝对不允许外人踏入半步" in prompt


@pytest.mark.asyncio
async def test_numeric_v2_actor_rejects_near_duplicate_latest_performance(monkeypatch):
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    previous = {
        "from_node_id": "start",
        "to_node_id": "start",
        "input_text": "要一起尝尝吗",
        "content": [
            {"type": "narration", "text": "测试猫娘看向桌边的筷子，又迅速移开了视线。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "谁说我要吃了？别自作多情。"},
            {"type": "narration", "text": "鱼香让她悄悄咽了一下口水。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "我只是怕你浪费粮食，才不是我想吃。"},
        ],
    }
    session = replace(
        _session(engine),
        node_turn_count=1,
        performance_history=(previous,),
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("repeat_turn", 0, "那我都吃咯"),
        (),
        scene_complete=False,
    )
    client = _FakeClient(json.dumps({
        "performance": (
            "（测试猫娘缩回角落，目光从筷子上迅速移开。）"
            "谁说我要吃了？别自作多情。"
            "（鱼香让她再次悄悄咽了一下口水。）"
            "我只是怕你浪费粮食，才不是我想吃。"
        ),
        "suggested_inputs": [],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_character_profile", lambda *args, **kwargs: "安静而认真。")
    monkeypatch.setattr(numeric_v2_actor, "_load_player_address", lambda *args, **kwargs: "哥哥")

    with pytest.raises(NumericV2ActorOutputError, match="numeric_v2_actor_repeated_output"):
        await NumericV2Actor(_ConfigManager()).generate_turn(
            engine=engine,
            session=session,
            outcome=outcome,
            player_input="那我都吃咯",
        )

    assert len(client.calls) == 1


def test_numeric_v2_actor_rejects_same_dialogue_hidden_by_different_actions():
    """模型不能只替换动作，就把上一轮整组对白伪装成新的回应。"""  # noqa: DOCSTRING_CJK

    previous = {
        "performance": "（看向工作台角落）这里没有厨房。（尾巴轻扫地面）你要在这里吃？",
    }
    current = {
        "performance": "（盯着整理导线的动作）这里没有厨房。（身体后仰）你要在这里吃？",
    }

    assert numeric_v2_actor._is_repeated_performance(current, previous) is True


@pytest.mark.asyncio
async def test_numeric_v2_actor_rejects_first_turn_repeating_opening_performance(monkeypatch):
    """首个普通回合也要与已展示的开场正文比较，不能让玩家第一句输入看似失效。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    opening_text = "（电子瞳孔缓慢聚焦）视觉校准完成。主存储区为空。"
    session = replace(
        _session(engine),
        opening_performance={
            "scene_narration": "暴雨敲打着工作室屋顶，工作台指示灯由红转绿。",
            "performance": opening_text,
            "suggested_inputs": [],
        },
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("repeat_opening_turn", 0, "这里是工作室，我是你的修复师。"),
        (),
        scene_complete=False,
    )
    client = _FakeClient(json.dumps({
        "performance": opening_text,
        "suggested_inputs": ["“这里是我的工作室，我刚刚修复了你。”"],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_character_profile", lambda *args, **kwargs: "安静而认真。")
    monkeypatch.setattr(numeric_v2_actor, "_load_player_address", lambda *args, **kwargs: "哥哥")

    with pytest.raises(NumericV2ActorOutputError, match="numeric_v2_actor_repeated_output"):
        await NumericV2Actor(_ConfigManager()).generate_turn(
            engine=engine,
            session=session,
            outcome=outcome,
            player_input="这里是工作室，我是你的修复师。",
        )

    prompt_payload = json.loads(client.calls[0][1].content.split("：\n", 1)[1])
    assert "公开开场后的首个普通回合" in prompt_payload["response_instruction"]
    assert len(client.calls) == 1


def test_numeric_v2_actor_keeps_core_persona_above_story_personality_language():
    story = numeric_v2_story()
    story["intro"]["catgirl_identity"] = "小岚，经营花店、曾被背叛的年轻女性；剧本候选性格毒舌冷漠。"
    story["catgirl_binding"]["role_overlay"] = "当前极度敌视玩家，并把玩家视为入侵者。"
    story["nodes"][0]["story_beat"]["catgirl_situation"] = "她正处于高度应激和防备状态。"
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("style_turn", 0, "你愿意听我解释吗"),
        (),
        scene_complete=False,
    )

    quiet_messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "你愿意听我解释吗",
        "寡言克制，习惯用短句，情绪很少直接外露。",
        "测试猫娘",
        "哥哥",
    )
    lively_messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "你愿意听我解释吗",
        "活泼外向，语速快，习惯主动追问并直接表达好奇。",
        "测试猫娘",
        "哥哥",
    )

    quiet_payload = json.loads(quiet_messages[1].content.split("：\n", 1)[1])
    lively_payload = json.loads(lively_messages[1].content.split("：\n", 1)[1])
    quiet_context = quiet_payload["acting_context"]
    lively_context = lively_payload["acting_context"]
    assert quiet_context["core_persona"] != lively_context["core_persona"]
    assert quiet_context["story_identity"] == lively_context["story_identity"]
    assert quiet_context["story_role_context"] == lively_context["story_role_context"]
    assert quiet_context["current_scene_state"] == "她正处于高度应激和防备状态。"
    assert quiet_context["relationship_control"]["effective_stage"] == "guarded"
    assert "trust" not in quiet_context["capability_state"]
    assert "priority_rule" not in quiet_context
    assert "modulation_rule" not in quiet_context
    assert quiet_payload["player_input"] == lively_payload["player_input"] == "你愿意听我解释吗"
    assert list(quiet_payload)[-1] == list(lively_payload)[-1] == "relationship_guard"
    system_prompt = quiet_messages[0].content
    assert "core_persona 决定用词、语气和情绪表达方式" in system_prompt
    assert "suggested_inputs 也必须服从相同的关系上限" in system_prompt
    assert "可以提出有创意的即时行动" in system_prompt
    assert "只是可选的未来输入" in system_prompt
    assert "不得使用羞辱、恐吓或身体伤害威胁" in system_prompt
    assert "惩罚性命令、债务羞辱、暴力后果或永久控制" in system_prompt
    assert "把地板舔干净" not in system_prompt
    assert "不得为剧本没有明确给出姓名的人物擅自创造姓名" in system_prompt
    assert "不得通过‘拉着、拽着、拖着、推着、带着’" in system_prompt


def test_numeric_v2_actor_puts_direct_input_contract_at_prompt_tail():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    opening_payload = json.loads(numeric_v2_actor._opening_messages(
        engine,
        "温柔体贴。",
        "测试猫娘",
        "哥哥",
    )[1].content)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("direct_suggestion", 0, "我先把合同放在桌上。"),
        (),
        scene_complete=False,
    )
    turn_messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我先把合同放在桌上。",
        "温柔体贴。",
        "测试猫娘",
        "哥哥",
    )
    turn_payload = json.loads(turn_messages[1].content.split("：\n", 1)[1])

    assert "suggestion_contract" not in opening_payload
    assert "suggestion_contract" not in turn_payload
    for messages in (
        numeric_v2_actor._opening_messages(engine, "温柔体贴。", "测试猫娘", "哥哥"),
        turn_messages,
    ):
        contract = messages[0].content
        assert "可原样发送的直接台词或动作" in contract
        assert "解释、询问、表示、保证、提出、展示、选择" in contract
        assert "动作省略男主主语" in contract
        assert "混合项用中文引号标出台词" in contract
    assert list(turn_payload)[-1] == "relationship_guard"


def test_numeric_v2_actor_separates_runtime_opening_from_transition_bridge():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(_session(engine), node_turn_count=1)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("transition_contract", 0, "我愿意继续听。"),
        (),
        scene_complete=True,
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我愿意继续听。",
        "温柔体贴。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["runtime_target_opening"] == "雨停后的长街恢复了安静。"
    assert "opening_scene" not in payload["target_story_beat"]
    assert "雨停后的长街恢复了安静" not in json.dumps(
        payload["transition_contract"],
        ensure_ascii=False,
    )
    assert "不得复述 runtime_target_opening" in payload["turn_instruction"]


def test_numeric_v2_actor_deduplicates_exact_transition_boundaries():
    """来源幕与目标幕相同的硬边界只发送一次，差异边界仍各自保留。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["must_not_happen"] = ["公共边界", "仅来源边界"]
    story["nodes"][2]["story_beat"]["must_not_happen"] = ["公共边界", "仅目标边界"]
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), node_turn_count=1)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("transition_boundary_dedup", 0, "我愿意继续听。"),
        (),
        scene_complete=True,
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我愿意继续听。",
        "温柔体贴。",
        "测试猫娘",
        "哥哥",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["shared_boundaries"] == ["公共边界"]
    assert payload["source_boundaries"] == ["仅来源边界"]
    assert payload["target_story_beat"]["boundaries"] == ["仅目标边界"]


def test_numeric_v2_actor_uses_compact_transition_context_without_losing_hard_facts():
    story = numeric_v2_story(player_address_known=False)
    story["intro"]["background"] += "小镇的居住权、旧信与花店归属都是不可改写的稳定事实。" * 12
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "fresh_boot",
        "memory_state": "empty",
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
        "allowed_behaviors": ["自检", "观察", "确认环境"],
        "forbidden_behaviors": ["使用角色卡自称", "虚构旧记忆"],
    }
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), node_turn_count=1)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("compact_transition", 0, "我们继续吧。"),
        (),
        scene_complete=True,
    )
    profile = "\n".join([
        "昵称: 测试猫娘",
        "自称: 人家",
        "核心特质: 温柔、认真、克制",
        "行为特点: 会先观察再回应",
        "关系定位: 从小一起长大的青梅竹马",
        "她熟悉哥哥的所有生活习惯。",
    ])

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我们继续吧。",
        profile,
        "测试猫娘",
        "你",
        False,
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert sum(count_tokens(message.content) for message in messages) <= 4200
    assert payload["story_context"] == {
        "knowledge_scope": (
            "只把 current_story_beat.opening_scene、recent_context、player_input 与 acting_contract "
            "明确允许的可观察状态当作已知事实；其余背景、身份、历史和关系均未知，不得推断。"
        ),
    }
    assert payload["player_address_state"] == {"known": False, "projection": "你"}
    assert payload["runtime_target_opening"] == "雨停后的长街恢复了安静。"
    assert payload["target_story_beat"]["pending_goals"]
    assert payload["transition_contract"]["must_preserve"]
    assert "source_story_beat" not in payload
    assert "target_scene_state" not in payload["acting_context"]
    assert "style_instruction" not in payload
    assert "suggestion_contract" not in payload
    assert "relationship_guard" not in payload
    assert "priority_rule" not in payload["acting_context"]
    assert "modulation_rule" not in payload["acting_context"]
    assert "story_identity" not in payload["acting_context"]
    assert "story_role_context" not in payload["acting_context"]
    assert "current_scene_state" not in payload["acting_context"]
    assert "关系定位" not in payload["acting_context"]["core_persona"]
    assert "哥哥" not in payload["acting_context"]["core_persona"]
    assert list(payload)[-1] == "transition_contract"
    assert list(payload).index("factual_guard") < list(payload).index("acting_context")
    assert list(payload).index("acting_context") < list(payload).index("transition_contract")


def test_numeric_v2_actor_applies_question_boundary_to_transition_target_without_weakening_contract_order():
    """换场推荐服从目标节点，同时过渡合同仍保持最后交付。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story(player_address_known=False)
    story["nodes"][2]["story_beat"]["acting_contract"] = {
        "cognition_state": "fresh_boot",
        "memory_state": "empty",
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
        "allowed_behaviors": ["自检", "观察", "询问现场信息"],
        "forbidden_behaviors": ["虚构旧记忆"],
    }
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), node_turn_count=1)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("fresh_boot_transition_target", 0, "我们继续吧。"),
        (),
        scene_complete=True,
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我们继续吧。",
        "核心特质：安静而认真。",
        "测试猫娘",
        "你",
        False,
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert outcome.ledger_event["to_node_id"] == "ending_leave"
    assert payload["suggestion_instruction"]["question_policy"] == "statements_and_actions_only"
    assert "目标节点仍是结构化开机且记忆空白状态" in payload["suggestion_instruction"]["rule"]
    assert list(payload).index("suggestion_instruction") < list(payload).index("factual_guard")
    assert list(payload)[-1] == "transition_contract"


def test_numeric_v2_actor_rejects_source_exact_goal_repeated_in_target_opening():
    """换场后的目标开场不能把来源幕精确交付再次演成新一轮启动。"""  # noqa: DOCSTRING_CJK

    source_beat = {
        "goals": [{
            "id": "startup_check",
            "owner": "catgirl",
            "description": "女主完成启动检查。",
            "evidence": {"mode": "exact", "anchors": ["视觉校准完成"]},
        }],
    }
    target_beat = {
        "goals": [{
            "id": "eat_breakfast",
            "owner": "catgirl",
            "description": "女主尝试摄入能量。",
            "evidence": {"mode": "semantic", "anchors": []},
        }],
    }
    performance = {
        "segments": [
            {"phase": "source_response", "performance": "（收回视线）检查结束。"},
            {"phase": "transition_bridge", "scene_narration": "雨夜过去，天色转亮。"},
            {
                "phase": "target_opening",
                "performance": "（在晨光中睁眼）视觉校准完成。",
            },
        ],
        "suggested_inputs": [],
    }

    with pytest.raises(
        NumericV2ActorOutputError,
        match="numeric_v2_actor_transition_repeats_source_goal",
    ):
        numeric_v2_actor._assert_transition_target_excludes_source_exact_anchors(
            performance,
            source_beat=source_beat,
            target_beat=target_beat,
        )


def test_numeric_v2_opening_anchor_skips_player_owned_first_sentence():
    assert numeric_v2_actor._opening_anchor(
        "为了验证日志上的线索，你提议在午夜后巡视站台。最后一班慢车驶离后，站台陷入死寂。",
        "女主正警惕地望向站台尽头。",
    ) == "最后一班慢车驶离后，站台陷入死寂。"


@pytest.mark.asyncio
async def test_numeric_v2_opening_only_projects_public_scene_anchor(monkeypatch):
    story = numeric_v2_story()
    full_background = story["intro"]["background"] + "租约、付款与合法居住依据必须作为开场前事实保留。" * 12
    full_player_identity = story["intro"]["player_identity"] + "林舟已经支付全年租金并拥有当前房屋的居住依据。" * 8
    full_catgirl_identity = story["intro"]["catgirl_identity"] + "小岚仍需在警惕中确认双方的合法居住依据。" * 8
    story["intro"] = {
        "background": full_background,
        "player_identity": full_player_identity,
        "catgirl_identity": full_catgirl_identity,
    }
    story["nodes"][0]["story_beat"] = {
        **story["nodes"][0]["story_beat"],
        "summary": "雨刚停，花店门铃轻轻响起。测试猫娘尚未提起旧日来信。",
        "must_happen": ["测试猫娘向来客说明旧信仍被妥善保存。"],
    }
    engine = NumericV2Engine.from_mapping(story)
    client = _FakeClient(json.dumps({
        "scene_narration": "雨刚停，花店门铃轻轻响起。",
        "performance": "（抬眼看向门口）哥哥，要进来避一会儿雨吗？",
        "suggested_inputs": ["走进花店，问她是否还记得自己"],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_character_profile", lambda *args, **kwargs: "安静而认真。")
    monkeypatch.setattr(numeric_v2_actor, "_load_player_address", lambda *args, **kwargs: "哥哥")

    performance = await NumericV2Actor(_ConfigManager()).generate_opening(engine=engine)

    prompt = "\n".join(str(message.content) for message in client.calls[0])
    opening_payload = json.loads(client.calls[0][1].content)
    assert performance["scene_narration"] == "雨刚停，花店门铃轻轻响起。"
    assert performance["performance"] == "（抬眼看向门口）哥哥，要进来避一会儿雨吗？"
    assert performance["suggested_inputs"] == []
    assert opening_payload["story_context"]["background"] == full_background
    assert opening_payload["story_context"]["player_identity"] == full_player_identity.replace("林舟", "哥哥")
    assert opening_payload["acting_context"]["story_identity"] == full_catgirl_identity.replace("小岚", "测试猫娘")
    assert '"opening_phase":true' in prompt
    assert '"visible_player_history":[]' in prompt
    assert '"current_chapter_title":"重逢"' in prompt
    assert '"core_persona":"安静而认真。"' in prompt
    assert opening_payload["acting_context"]["relationship_control"]["effective_stage"] == "guarded"
    assert "保留判断、条件或边界" in opening_payload["acting_context"]["relationship_control"]["response_contract"]
    assert "metric_states" not in opening_payload["acting_context"]["relationship_control"]
    # 开场与普通回合使用同一优先级，不能让角色卡关系词覆盖低关系合同。
    assert list(opening_payload["acting_context"]).index("core_persona") < list(
        opening_payload["acting_context"]
    ).index("relationship_control")
    assert "core_persona 决定用词、语气和情绪表达方式" in prompt
    assert '"opening_scene":"雨刚停，花店门铃轻轻响起。"' in prompt
    assert "测试猫娘尚未提起旧日来信" not in prompt
    assert "测试猫娘向来客说明旧信仍被妥善保存" not in prompt
    assert '"must_happen"' not in prompt
    assert "这是玩家输入前的公开开场" in prompt
    assert "不得假定玩家已经说话、做出选择或完成剧情摘要中的行动" in prompt
    assert "把它们视为后续可发展的剧情边界" in prompt


def test_numeric_v2_opening_projects_only_exact_startup_deliverables():
    """开场只接收结构化 exact 启动锚点，不提前看到其他本幕目标。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [
        {
            "id": "visual_ready",
            "owner": "catgirl",
            "description": "女主在括号外明确说出视觉校准结果。",
            "evidence": {"mode": "exact", "anchors": ["视觉校准完成"]},
        },
        {
            "id": "observe_room",
            "owner": "catgirl",
            "description": "女主观察陌生房间。",
            "evidence": {"mode": "semantic", "anchors": []},
        },
    ]
    beat.pop("must_happen")
    beat["acting_contract"] = {
        "cognition_state": "fresh_boot",
        "memory_state": "empty",
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
        "allowed_behaviors": ["完成自检"],
        "forbidden_behaviors": ["虚构旧记忆"],
    }
    engine = NumericV2Engine.from_mapping(story)

    payload = json.loads(numeric_v2_actor._opening_messages(
        engine,
        "核心特质：安静而认真。",
        "测试猫娘",
        "你",
        False,
    )[1].content)

    assert payload["current_story_beat"]["opening_deliverables"] == [{
        "goal_id": "visual_ready",
        "owner": "catgirl",
        "text": "女主在括号外明确说出视觉校准结果。",
        "evidence": {"mode": "exact", "anchors": ["视觉校准完成"]},
    }]
    assert "观察陌生房间" not in json.dumps(payload, ensure_ascii=False)
    assert "视觉校准完成" in payload["current_story_beat"]["instruction"]
    assert "旧信" not in payload["story_context"].get("background", "")


def test_numeric_v2_opening_suggestions_reply_as_player_instead_of_copying_catgirl_tasks():
    """开场推荐必须承接可见猫娘正文，不能把猫娘的信息需求倒置成男主提问。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story(player_address_known=False)
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "fresh_boot",
        "memory_state": "empty",
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
        "allowed_behaviors": ["保持静止并询问当前地点或眼前之人的身份"],
        "forbidden_behaviors": ["虚构旧记忆"],
    }
    engine = NumericV2Engine.from_mapping(story)

    messages = numeric_v2_actor._opening_messages(
        engine,
        "核心特质：安静而认真。",
        "测试猫娘",
        "你",
        False,
    )
    system_prompt = str(messages[0].content)
    payload = json.loads(messages[1].content)

    assert list(payload)[-1] == "suggestion_instruction"
    assert payload["suggestion_instruction"]["mode"] == "opening_player_reply"
    assert payload["suggestion_instruction"]["question_policy"] == "statements_and_actions_only"
    suggestion_rule = payload["suggestion_instruction"]["rule"]
    assert "只能是男主" in suggestion_rule
    assert "直接回应已显示的 performance" in suggestion_rule
    assert "属于猫娘的提问、观察、自检、动作或台词必须由 performance 执行" in suggestion_rule
    assert "候选让男主回答或主动披露" in suggestion_rule
    assert "不能把同一信息需求倒置成男主提问" in suggestion_rule
    assert "本次 suggested_inputs 禁止问句" in suggestion_rule
    assert "即使句末没有问号" in suggestion_rule
    assert "一条直接承接当前对白，一条写男主自己的下一步" in suggestion_rule
    assert "不把玩家刚说的用途、对象或决定改成另一件事" in suggestion_rule
    assert "缺少依据时只表达意图，不写已经成立的结果" in suggestion_rule
    assert "男主接下来" in system_prompt
    assert "必须先在 performance 中由猫娘问出" in system_prompt
    assert "不能把同一信息需求倒置成男主的问题" in system_prompt
    # 开场专属规则不进入普通回合，避免为长 Session 重复占用固定预算。
    assert "男主接下来" not in numeric_v2_actor._system_prompt(
        catgirl_name="测试猫娘",
        player_address="你",
        player_address_known=False,
        phase="turn",
    )


def test_numeric_v2_opening_keeps_player_questions_outside_fresh_boot_empty_memory():
    """确定性问句保护只作用于开机空白态，普通剧本开场仍可推荐男主提问。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "normal",
        "memory_state": "available",
        "self_reference_mode": "persona_allowed",
        "persona_scope": "full",
        "allowed_behaviors": ["自然回应"],
        "forbidden_behaviors": [],
    }
    engine = NumericV2Engine.from_mapping(story)

    payload = json.loads(numeric_v2_actor._opening_messages(
        engine,
        "核心特质：安静而认真。",
        "测试猫娘",
        "你",
        True,
    )[1].content)
    parsed = _parse_output(
        json.dumps({
            "scene_narration": "雨停后，花店门铃轻轻响起。",
            "performance": "（抬眼看向门口）你来了。",
            "suggested_inputs": ["“你找我有什么事？”"],
        }, ensure_ascii=False),
        opening_required=True,
        suggestion_questions_allowed=True,
    )

    assert payload["suggestion_instruction"]["question_policy"] == "allowed"
    assert "本次 suggested_inputs 禁止问句" not in payload["suggestion_instruction"]["rule"]
    assert parsed["suggested_inputs"] == ["“你找我有什么事？”"]


def test_numeric_v2_turn_keeps_fresh_boot_player_role_boundary_for_visible_node():
    """推荐职责跟随当前可见节点，不能只在 opening 调用期间生效。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story(player_address_known=False)
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "fresh_boot",
        "memory_state": "empty",
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
        "assertable_self_facts": ["视觉校准完成", "主存储区为空"],
        "allowed_behaviors": ["询问当前地点或眼前之人的身份"],
        "forbidden_behaviors": ["虚构旧记忆"],
    }
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("fresh_boot_visible_turn", 0, "这里是安全屋，我是你的修复师。"),
        (),
        scene_complete=False,
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "这里是安全屋，我是你的修复师。",
        "核心特质：安静而认真。",
        "测试猫娘",
        "你",
        False,
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    instruction = payload["suggestion_instruction"]
    assert instruction["question_policy"] == "statements_and_actions_only"
    assert "当前可见节点仍是结构化开机且记忆空白状态" in instruction["rule"]
    assert "不得用祈使句让猫娘提供地点、身份、状态、判断或行动方案" in instruction["rule"]
    assert "不得把男主已知的地点、身份、风险判断或行动决定反问给猫娘" in instruction["rule"]
    assert "候选只写男主自己的台词或动作，不命令猫娘配合" in instruction["rule"]
    assert "不主动扩展机体内部、诊断设备、监控系统或检查结论" in instruction["rule"]
    assert list(payload).index("suggestion_instruction") < list(payload).index("factual_guard")
    assert "可以确认的自身状态结论只有：视觉校准完成、主存储区为空" in payload["factual_guard"]
    assert "玩家的猜测、命令或提问不能增加清单" in payload["factual_guard"]


def test_numeric_v2_turn_keeps_player_questions_for_normal_cognition_node():
    """普通认知节点仍允许男主自然提问，开机保护不能污染其他剧本。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "normal",
        "memory_state": "available",
        "self_reference_mode": "persona_allowed",
        "persona_scope": "full",
        "allowed_behaviors": ["自然回应"],
        "forbidden_behaviors": [],
    }
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("normal_visible_turn", 0, "我想听你说。"),
        (),
        scene_complete=False,
    )

    messages = numeric_v2_actor._turn_messages(
        engine,
        session,
        outcome,
        "我想听你说。",
        "核心特质：安静而认真。",
        "测试猫娘",
        "哥哥",
        True,
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])
    parsed = _parse_output(
        json.dumps({
            "performance": "（把旧信推近些）你想先知道哪一段？",
            "suggested_inputs": ["“这封信是什么时候写的？”"],
        }, ensure_ascii=False),
        suggestion_questions_allowed=True,
    )

    assert "question_policy" not in payload["suggestion_instruction"]
    assert parsed["suggested_inputs"] == ["“这封信是什么时候写的？”"]


@pytest.mark.asyncio
async def test_numeric_v2_actor_applies_fresh_boot_question_filter_during_opening(monkeypatch):
    """真实开场入口必须把结构化开机状态传给输出解析器。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story(player_address_known=False)
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "fresh_boot",
        "memory_state": "empty",
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
        "allowed_behaviors": ["询问当前地点或眼前之人的身份"],
        "forbidden_behaviors": ["虚构旧记忆"],
    }
    engine = NumericV2Engine.from_mapping(story)
    client = _FakeClient(json.dumps({
        "scene_narration": "雨停后，花店门铃轻轻响起。",
        "performance": "（保持静止）视觉校准完成。主存储区为空。",
        "suggested_inputs": [
            "“这里是哪里？你是谁？”",
            "“这里是我的花店，我刚刚修复了你。”",
            "“我叫阿哲，是这里的技师。”",
        ],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)

    opening = await NumericV2Actor(_ConfigManager()).generate_opening(engine=engine)

    assert opening["suggested_inputs"] == ["“这里是我的花店，我刚刚修复了你。”"]


@pytest.mark.asyncio
async def test_numeric_v2_actor_applies_visible_node_question_filter_during_normal_turn(monkeypatch):
    """普通回合入口也必须把当前可见节点的结构化认知状态交给输出解析器。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story(player_address_known=False)
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "fresh_boot",
        "memory_state": "empty",
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
        "allowed_behaviors": ["询问当前地点或眼前之人的身份"],
        "forbidden_behaviors": ["虚构旧记忆"],
    }
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("fresh_boot_actor_turn", 0, "这里是安全屋，我是你的修复师。"),
        (),
        scene_complete=False,
    )
    client = _FakeClient(json.dumps({
        "performance": "（电子耳廓转向门外）警笛声正在靠近。我需要更多现场信息。",
        "suggested_inputs": [
            "“外面警笛声很急，我们需要立刻转移吗？”",
            "“警笛正在靠近，我决定先关闭工作室灯光。”",
            "后退一步检查出口",
            "“你可以叫我阿良。”",
        ],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_character_profile", lambda *args, **kwargs: "核心特质：安静而认真。")

    performance = await NumericV2Actor(_ConfigManager()).generate_turn(
        engine=engine,
        session=session,
        outcome=outcome,
        player_input="这里是安全屋，我是你的修复师。",
    )

    assert performance["suggested_inputs"] == [
        "“警笛正在靠近，我决定先关闭工作室灯光。”",
        "后退一步检查出口",
    ]


def test_numeric_v2_actor_allows_recommended_name_after_address_is_known():
    """称呼完成原子提交后，推荐输入才可继续复用已知名字。"""  # noqa: DOCSTRING_CJK

    performance = _parse_output(
        json.dumps({
            "performance": "（轻轻点头）老谢，名字已记录。",
            "suggested_inputs": ["“你可以叫我老谢。”"],
        }, ensure_ascii=False),
        suggestion_questions_allowed=False,
        suggestion_name_disclosure_allowed=True,
    )

    assert performance["suggested_inputs"] == ["“你可以叫我老谢。”"]


@pytest.mark.asyncio
async def test_numeric_v2_actor_does_not_send_unknown_player_address_to_model(monkeypatch):
    story = numeric_v2_story(player_address_known=False)
    engine = NumericV2Engine.from_mapping(story)
    client = _FakeClient(json.dumps({
        "scene_narration": "雨刚停，花店门铃轻轻响起。",
        "performance": "（抬眼看向门口）你来了。",
        "suggested_inputs": ["走进花店"],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_player_address", lambda *args, **kwargs: "哥哥")

    performance = await NumericV2Actor(_ConfigManager()).generate_opening(engine=engine)

    system_prompt = str(client.calls[0][0].content)
    opening_payload = json.loads(client.calls[0][1].content)
    assert performance["performance"] == "（抬眼看向门口）你来了。"
    assert "哥哥" not in system_prompt
    assert "哥哥" not in client.calls[0][1].content
    assert "男主称呼尚未确认，正文只用“你”指代男主" in system_prompt
    assert "不写内部身份标签“男主”" in system_prompt
    assert opening_payload["player_address_state"] == {
        "known": False,
        "projection": "你",
    }
    assert opening_payload["story_context"]["player_identity"].startswith("你，")


@pytest.mark.asyncio
async def test_numeric_v2_actor_rejects_unknown_player_address_leak_and_allows_exact_disclosure(monkeypatch):
    story = numeric_v2_story(player_address_known=False)
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("address_leak", 0, "我先听你说。"),
        (),
    )
    leaking_client = _FakeClient(json.dumps({
        "performance": "（抬眼）哥哥，你来了。",
        "suggested_inputs": ["继续说"],
    }, ensure_ascii=False))

    async def fake_leak_create(*_args, **_kwargs):
        return leaking_client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_leak_create)
    with pytest.raises(NumericV2ActorOutputError, match="player_address_leak"):
        await NumericV2Actor(_ConfigManager()).generate_turn(
            engine=engine,
            session=session,
            outcome=outcome,
            player_input="我先听你说。",
        )

    disclosed_client = _FakeClient(json.dumps({
        "performance": "（抬眼）哥哥，你好。",
        "suggested_inputs": ["继续说"],
    }, ensure_ascii=False))

    async def fake_disclosed_create(*_args, **_kwargs):
        return disclosed_client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_disclosed_create)
    performance = await NumericV2Actor(_ConfigManager()).generate_turn(
        engine=engine,
        session=session,
        outcome=outcome,
        player_input="我叫哥哥。",
    )
    assert performance["performance"] == "（抬眼）哥哥，你好。"
    assert json.loads(disclosed_client.calls[0][1].content.split("：\n", 1)[1])["player_address_state"] == {
        "known": False,
        "projection": "你",
    }


@pytest.mark.asyncio
async def test_numeric_v2_evaluator_rejects_criterion_id_from_another_metric(monkeypatch):
    story = numeric_v2_story()
    story["metric_schema"]["affection"] = {
        **story["metric_schema"]["trust"],
        "name": "好感度",
        "increase_criteria": ["玩家尊重小岚的创作节奏"],
        "decrease_criteria": ["玩家贬低小岚的创作"],
    }
    story["initial_state"]["metrics"]["affection"] = 20
    engine = NumericV2Engine.from_mapping(story)
    client = _FakeClient(json.dumps({
        "scene_complete": False,
        "metric_changes": {
            "trust": {
                "strength": "normal",
                "criterion_id": "affection.increase.1",
            },
        }
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_evaluator, "create_chat_llm_async", fake_create)
    with pytest.raises(numeric_v2_evaluator.NumericV2EvaluatorOutputError, match="metric_change_criterion_id_invalid"):
        await NumericV2MetricEvaluator(_ConfigManager()).evaluate(
            engine=engine,
            session=_session(engine),
            message="我尊重你的创作节奏。",
        )


@pytest.mark.asyncio
async def test_numeric_v2_evaluator_rejects_old_list_contract(monkeypatch):
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    client = _FakeClient(json.dumps({"scene_complete": False, "metric_changes": []}, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_evaluator, "create_chat_llm_async", fake_create)
    with pytest.raises(numeric_v2_evaluator.NumericV2EvaluatorOutputError, match="numeric_v2_evaluator_changes_invalid"):
        await NumericV2MetricEvaluator(_ConfigManager()).evaluate(
            engine=engine,
            session=_session(engine),
            message="我先听你说。",
        )


def test_numeric_v2_evaluator_ignores_unknown_metric_and_keeps_scene_result():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())

    result = numeric_v2_evaluator._parse_output(
        json.dumps({
            "scene_complete": True,
            "metric_changes": {
                "unknown_metric": {
                    "strength": "strong",
                    "criterion_id": "unknown_metric.increase.1",
                },
            },
        }),
        engine,
        "我已经把话说清楚了。",
    )

    assert result.scene_complete is True
    assert result.metric_changes == ()


def test_numeric_v2_evaluator_rejects_model_supplied_delta():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())

    with pytest.raises(
        numeric_v2_evaluator.NumericV2EvaluatorOutputError,
        match="numeric_v2_evaluator_changes_invalid",
    ):
        numeric_v2_evaluator._parse_output(
            json.dumps({
                "scene_complete": False,
                "metric_changes": {
                    "trust": {
                        "delta": 5,
                        "criterion_id": "trust.increase.1",
                    },
                },
            }),
            engine,
            "我会留下来。",
        )


def test_numeric_v2_evaluator_requires_strong_goal_anchors():
    goal = "女主通过无线脉冲干扰无人机，使其短暂悬停或偏离轨迹。"

    assert numeric_v2_evaluator._strong_goal_match(
        goal,
        "无人机正在重新校准，出口仍被封锁。",
    ) is False
    assert numeric_v2_evaluator._strong_goal_match(
        goal,
        "无线脉冲命中后，无人机短暂悬停。",
    ) is True
