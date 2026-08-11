"""验证 Numeric v2 两个模型角色都只调用一次且不越过职责边界。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.theater import numeric_v2_actor, numeric_v2_evaluator
from services.theater.numeric_v2_actor import NumericV2Actor, _parse_output
from services.theater.numeric_v2_cast import NumericV2CastProjection
from services.theater.numeric_v2_evaluator import NumericV2MetricEvaluator
from services.theater.numeric_v2_runtime import (
    NumericV2Engine,
    NumericV2RuntimeError,
    TurnRequestV2,
)
from tests.unit.test_theater_numeric_v2_contract import numeric_v2_story


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


def test_numeric_v2_turn_request_rejects_input_over_token_limit():
    with pytest.raises(NumericV2RuntimeError, match="numeric_turn_input_too_long"):
        TurnRequestV2.from_mapping({
            "client_turn_id": "long_input",
            "base_revision": 0,
            "message": "测试 " * 141,
        })


def test_numeric_v2_cast_projects_multi_word_source_names():
    story = numeric_v2_story()
    story["intro"]["player_identity"] = "John Smith，回乡整理旧屋的年轻男性。"
    story["intro"]["catgirl_identity"] = "Mary Jane，经营花店、保留旧信的年轻女性。"

    projected = NumericV2CastProjection.from_story(
        story,
        player_name="哥哥",
        catgirl_name="测试猫娘",
    ).intro(story)

    assert projected["player_identity"].startswith("哥哥，")
    assert projected["catgirl_identity"].startswith("测试猫娘，")


def test_numeric_v2_actor_discards_invalid_dialogue_items_without_reassigning_speaker():
    performance = _parse_output(json.dumps({
        "narration": "小葵把记录本推到桌边。",
        "dialogue": [
            {"speaker_id": "active_catgirl", "text": "先看记录，再决定。"},
            {"speaker_id": "哥哥", "text": "玩家不应被改写成猫娘台词。"},
            {"speaker_id": "active_catgirl", "text": ""},
            {"speaker_id": "active_catgirl", "text": "保留这句。", "extra": "ignored"},
        ],
        "suggested_inputs": [],
    }, ensure_ascii=False))

    assert performance == {
        "narration": "小葵把记录本推到桌边。",
        "dialogue": [
            {"speaker_id": "active_catgirl", "text": "先看记录，再决定。"},
            {"speaker_id": "active_catgirl", "text": "保留这句。"},
        ],
        "suggested_inputs": [],
    }


@pytest.mark.asyncio
async def test_numeric_v2_evaluator_calls_once_and_cannot_see_routes(monkeypatch):
    story = numeric_v2_story()
    story["metric_schema"]["trust"]["increase_criteria"] = ["玩家兑现对小岚的承诺"]
    engine = NumericV2Engine.from_mapping(story)
    client = _FakeClient(json.dumps({
        "scene_complete": False,
        "metric_changes": {
            "trust": {
            "delta": 2,
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
    )

    assert evaluation.scene_complete is False
    assert [(item.metric_id, item.delta) for item in evaluation.metric_changes] == [("trust", 2)]
    assert evaluation.metric_changes[0].criterion == "玩家兑现对小岚的承诺"
    assert evaluation.metric_changes[0].evidence == "我会留下来把话说完。"
    assert len(create_calls) == len(client.calls) == 1
    assert create_calls[0][1]["max_retries"] == 0
    prompt = "\n".join(str(message.content) for message in client.calls[0])
    assert "玩家兑现对测试猫娘的承诺" in prompt
    assert "玩家兑现对小岚的承诺" not in prompt
    assert "trust.increase.1" in prompt
    assert "每项数值每回合最多只能出现一次" in prompt
    assert "只有本幕所有 pending_goals" in prompt
    assert "玩家纠正最近演绎中的错误事实" in prompt
    assert "要求共同决定、核对事实或设置协作边界" in prompt
    assert '"phase":"opening"' in prompt
    assert '"narration":"开场"' in prompt
    assert '"current_scene_context"' in prompt
    assert "不要求逐字复述目标" in prompt
    assert "target_node_id" not in prompt
    assert "route_gates" not in prompt
    assert '"pending_goals"' in prompt
    assert '"summary"' not in prompt
    assert '"phase":"opening"' in prompt
    assert '"narration":"开场"' in prompt
    assert '"metric_change_reasons"' not in prompt


@pytest.mark.asyncio
async def test_numeric_v2_actor_calls_once_and_only_returns_performance(monkeypatch):
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(_session(engine), node_turn_count=1)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("actor_turn", 0, "我先听你说。"),
        (),
        scene_complete=True,
    )
    client = _FakeClient(json.dumps({
        "narration": "风铃轻轻响了一声。",
        "dialogue": [{"speaker_id": "active_catgirl", "text": "那就先坐一会儿。"}],
        "suggested_inputs": ["问她这些年过得怎样"],
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
        "narration": "风铃轻轻响了一声。",
        "dialogue": [{"speaker_id": "active_catgirl", "text": "那就先坐一会儿。"}],
        "suggested_inputs": ["问她这些年过得怎样"],
    }
    assert len(create_calls) == len(client.calls) == 1
    assert create_calls[0][1]["max_retries"] == 0
    prompt = "\n".join(str(message.content) for message in client.calls[0])
    turn_payload = json.loads(client.calls[0][1].content.split("：\n", 1)[1])
    assert {"route_changed", "turn_instruction", "recent_context", "minimum_turns_before_route"}.issubset(turn_payload)
    assert "我先听你说。" in prompt
    assert "不能创造新节点、路线、数值、事实或结局" in prompt
    assert "本剧男主由玩家扮演" in prompt
    assert "本剧女主由当前猫娘扮演" in prompt
    assert "narration 只能用第三人称描写猫娘的动作、神态和可见环境" in prompt
    assert "即使玩家明确输入了动作，也不要在 narration 中复述、补全或改写" in prompt
    assert "不得用“你”“您”或“哥哥”作为旁白中的动作与状态主体" in prompt
    assert "不得把饮品与食物混成同一物品" in prompt
    assert "冲突发生后必须先回应并化解或延续冲突" in prompt
    assert "同一回合的 narration 与 dialogue 必须发生在同一时刻和场景" in prompt
    assert "不得突然追问、回答或引用画面中从未发生的言行" in prompt
    assert "玩家说‘可以、如果、愿意、打算、改天’" in prompt
    assert "不得据此在旁白中替玩家转身、离开、靠近、触碰、站立" in prompt
    assert "route_changed 为 true" in prompt
    assert "必须先直接回应 player_input" in prompt
    assert "再自然桥接到目标节点" in prompt
    assert "dialogue.text 只能放猫娘实际说出口的完整话语" in prompt
    assert '"opening_scene":"两人接受这次仍然会分别。"' in prompt
    assert '"source_story_beat"' in prompt
    assert '"target_story_beat"' in prompt
    assert '"pending_goals"' in prompt
    assert '"summary"' not in prompt
    assert "哥哥，回乡整理旧屋的年轻男性" in prompt
    assert "测试猫娘，经营花店、保留旧信的年轻女性" in prompt
    assert "林舟" not in prompt
    assert "小岚" not in prompt


@pytest.mark.asyncio
async def test_numeric_v2_opening_does_not_assume_hidden_player_actions(monkeypatch):
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"] = {
        **story["nodes"][0]["story_beat"],
        "summary": "雨刚停，花店门铃轻轻响起。玩家随后说明自己为什么回来。",
        "must_happen": ["玩家说明自己为什么回来。"],
    }
    engine = NumericV2Engine.from_mapping(story)
    client = _FakeClient(json.dumps({
        "narration": "测试猫娘抬眼看向门口。",
        "dialogue": [{"speaker_id": "active_catgirl", "text": "哥哥，要进来避一会儿雨吗？"}],
        "suggested_inputs": ["走进花店，问她是否还记得自己"],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_character_profile", lambda *args, **kwargs: "安静而认真。")
    monkeypatch.setattr(numeric_v2_actor, "_load_player_address", lambda *args, **kwargs: "哥哥")

    performance = await NumericV2Actor(_ConfigManager()).generate_opening(engine=engine)

    prompt = "\n".join(str(message.content) for message in client.calls[0])
    assert performance["narration"] == "测试猫娘抬眼看向门口。"
    assert '"opening_phase":true' in prompt
    assert '"visible_player_history":[]' in prompt
    assert '"opening_scene":"雨刚停，花店门铃轻轻响起。"' in prompt
    assert "玩家随后说明自己为什么回来" not in prompt
    assert "玩家说明自己为什么回来" not in prompt
    assert '"must_happen"' not in prompt
    assert "这是玩家输入前的公开开场" in prompt
    assert "不得假定玩家已经说话、做出选择或完成剧情摘要中的行动" in prompt
    assert "把它们视为后续可发展的剧情边界" in prompt


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
                "delta": 2,
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
