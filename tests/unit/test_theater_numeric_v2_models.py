"""验证 Numeric v2 两个模型角色都只调用一次且不越过职责边界。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.theater import numeric_v2_actor, numeric_v2_evaluator
from services.theater.numeric_v2_actor import (
    NumericV2Actor,
    NumericV2ActorOutputError,
    _parse_output,
)
from services.theater.numeric_v2_cast import NumericV2CastProjection
from services.theater.numeric_v2_evaluator import NumericV2MetricEvaluator
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

    assert [item["player_input"] for item in context] == ["重新进入", "本次访问"]
    assert all(item["player_input"] != "旧访问" for item in context)


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


def test_numeric_v2_actor_preserves_interleaved_narration_and_multiple_dialogue_blocks():
    performance = _parse_output(json.dumps({
        "content": [
            {"type": "narration", "text": "小葵把记录本推到桌边。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "先看记录，再决定。"},
            {"type": "narration", "text": "她用指尖压住被风吹起的纸角。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "看完再告诉我答案。"},
        ],
        "suggested_inputs": [],
    }, ensure_ascii=False))

    assert performance == {
        "content": [
            {"type": "narration", "text": "小葵把记录本推到桌边。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "先看记录，再决定。"},
            {"type": "narration", "text": "她用指尖压住被风吹起的纸角。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "看完再告诉我答案。"},
        ],
        "suggested_inputs": [],
    }


def test_numeric_v2_actor_rejects_narration_only_turn():
    with pytest.raises(NumericV2ActorOutputError, match="numeric_v2_actor_dialogue_required"):
        _parse_output(json.dumps({
            "content": [{"type": "narration", "text": "她只是沉默地看着门口。"}],
            "suggested_inputs": [],
        }, ensure_ascii=False))


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
    assert '"content":[{"type":"narration","text":"开场"}]' in prompt
    assert '"current_scene_context"' in prompt
    assert "不要求逐字复述目标" in prompt
    assert "target_node_id" not in prompt
    assert "route_gates" not in prompt
    assert '"pending_goals"' in prompt
    assert '"summary"' not in prompt
    assert '"phase":"opening"' in prompt
    assert '"content":[{"type":"narration","text":"开场"}]' in prompt
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
        "segments": [
            {
                "phase": "source_response",
                "content": [
                    {"type": "narration", "text": "测试猫娘认真听完。"},
                    {"type": "dialogue", "speaker_id": "active_catgirl", "text": "那就先坐一会儿。"},
                ],
            },
            {
                "phase": "transition_bridge",
                "content": [{"type": "narration", "text": "夜色渐深，花店终于安静下来。"}],
            },
            {
                "phase": "target_opening",
                "content": [
                    {"type": "narration", "text": "两人接受这次仍然会分别。测试猫娘把旧信重新收进抽屉。"},
                    {"type": "dialogue", "speaker_id": "active_catgirl", "text": "这一次，把信读完吧。"},
                ],
            },
        ],
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
        "suggested_inputs": ["问她这些年过得怎样"],
        "segments": [
            {
                "phase": "source_response",
                "content": [
                    {"type": "narration", "text": "测试猫娘认真听完。"},
                    {"type": "dialogue", "speaker_id": "active_catgirl", "text": "那就先坐一会儿。"},
                ],
            },
            {
                "phase": "transition_bridge",
                "content": [{"type": "narration", "text": "夜色渐深，花店终于安静下来。"}],
            },
            {
                "phase": "target_opening",
                "content": [
                    {"type": "narration", "text": "两人接受这次仍然会分别。测试猫娘把旧信重新收进抽屉。"},
                    {"type": "dialogue", "speaker_id": "active_catgirl", "text": "这一次，把信读完吧。"},
                ],
            },
        ],
        "transition_delivered": True,
        "visible_node_id": "ending_leave",
    }
    assert len(create_calls) == len(client.calls) == 1
    assert create_calls[0][1]["max_retries"] == 0
    prompt = "\n".join(str(message.content) for message in client.calls[0])
    turn_payload = json.loads(client.calls[0][1].content.split("：\n", 1)[1])
    assert {"route_changed", "turn_instruction", "recent_context", "minimum_turns_before_route", "soft_pacing"}.issubset(turn_payload)
    assert turn_payload["current_chapter_title"] == "重逢"
    assert turn_payload["target_chapter_title"] == "决定"
    assert turn_payload["soft_pacing"]["phase"] == "transition"
    assert "我先听你说。" in prompt
    assert "不能创造新节点、路线、数值、事实或结局" in prompt
    assert "本剧男主由玩家扮演" in prompt
    assert "本剧女主由当前猫娘扮演" in prompt
    assert "narration 类型的 content block 只能用第三人称描写猫娘的动作、神态和可见环境" in prompt
    assert "即使玩家明确输入了动作，也不要在 narration block 中复述、补全或改写" in prompt
    assert "不得用“你”“您”或“哥哥”作为旁白中的动作与状态主体" in prompt
    assert "不得把饮品与食物混成同一物品" in prompt
    assert "冲突发生后必须先回应并化解或延续冲突" in prompt
    assert "content 中的 narration 与 dialogue 必须按实际发生顺序穿插" in prompt
    assert "不得突然追问、回答或引用画面中从未发生的言行" in prompt
    assert "玩家说‘可以、如果、愿意、打算、改天’" in prompt
    assert "不得据此在旁白中替玩家转身、离开、靠近、触碰、站立" in prompt
    assert "route_changed 为 true" in prompt
    assert "必须先直接回应 player_input" in prompt
    assert "再自然桥接到目标节点" in prompt
    assert "source_response、transition_bridge、target_opening" in prompt
    assert "target_opening 的第一个 narration block 必须以 opening_scene 原文开头" in prompt
    assert "dialogue block 的 text 只能放猫娘实际说出口的完整话语" in prompt
    assert "章节标题只是软主题锚点" in prompt
    assert "多个同样成立的候选焦点" in prompt
    assert "不能覆盖 player_input、recent_context" in prompt
    acting_context = turn_payload["acting_context"]
    assert acting_context["core_persona"] == "安静而认真。"
    assert acting_context["story_identity"] == "测试猫娘，经营花店、保留旧信的年轻女性。"
    assert acting_context["story_role_context"] == "她既期待重逢，又担心玩家再次离开。"
    assert acting_context["current_scene_state"] == "她在观察玩家是否可信。"
    assert acting_context["target_scene_state"] == "她在观察玩家是否可信。"
    assert acting_context["relationship_state"] == {"trust": "戒备"}
    assert "核心人格决定表达方式" in acting_context["priority_rule"]
    assert "不能覆盖核心人格" in acting_context["priority_rule"]
    assert "role_overlay" not in turn_payload
    assert "current_metric_bands" not in turn_payload
    assert "catgirl_expression_profile" not in turn_payload
    assert "角色卡决定用词、句长、主动性和情绪外显方式" in turn_payload["style_instruction"]
    assert '"opening_scene":"两人接受这次仍然会分别。"' in prompt
    assert '"source_story_beat"' in prompt
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
        "content": [
            {"type": "narration", "text": "测试猫娘收住话头，把灯调暗了一些。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "那就先休息，明早再说。"},
        ],
        "suggested_inputs": ["安静休息，等到第二天的动静"],
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
    assert "不能把 scene_complete 当成 true" in payload["soft_pacing"]["instruction"]
    assert performance["suggested_inputs"] == ["安静休息，等到第二天的动静"]


@pytest.mark.asyncio
async def test_numeric_v2_actor_working_memory_preserves_latest_turn_facts(monkeypatch):
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    older = tuple({
        "from_node_id": "start",
        "to_node_id": "start",
        "input_text": f"旧回合 {index}",
        "content": [
            {"type": "narration", "text": "这是已经过去的场景细节。" * 18},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "这是较早的对话。" * 8},
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
        "content": [
            {"type": "narration", "text": "测试猫娘看了一眼储物柜。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "那就再想办法。"},
        ],
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
    assert sum(count_tokens(message.content) for message in client.calls[0]) <= 3200
    assert list(payload)[-5:] == [
        "recent_openings",
        "acting_context",
        "style_instruction",
        "response_instruction",
        "player_input",
    ]
    assert payload["player_input"] == "指出毛巾太脏无法使用，再次尝试沟通"
    assert "先回应 player_input" in payload["response_instruction"]
    assert payload["acting_context"]["core_persona"] == "安静而认真。"
    assert payload["acting_context"]["relationship_state"] == {"trust": "戒备"}
    assert payload["recent_openings"][-1].startswith("测试猫娘像是被踩到了尾巴的猫")
    assert "不是禁词" in payload["style_instruction"]
    assert "避免连续复用" in payload["style_instruction"]
    assert latest_memory["player_input"] == "那我得洗澡啊"
    assert latest_memory["content"] == latest["content"]
    prompt = client.calls[0][1].content
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
        "content": [
            {"type": "narration", "text": "测试猫娘缩回角落，目光从筷子上迅速移开。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "谁说我要吃了？别自作多情。"},
            {"type": "narration", "text": "鱼香让她再次悄悄咽了一下口水。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "我只是怕你浪费粮食，才不是我想吃。"},
        ],
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
    assert "剧情身份和临时状态不能覆盖核心人格" in quiet_context["priority_rule"]
    assert "关系状态只能调整信任、距离、亲密度和主动性" in quiet_context["modulation_rule"]
    assert quiet_payload["player_input"] == lively_payload["player_input"] == "你愿意听我解释吗"
    assert list(quiet_payload)[-1] == list(lively_payload)[-1] == "player_input"


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
        "content": [
            {"type": "narration", "text": "测试猫娘抬眼看向门口。"},
            {"type": "dialogue", "speaker_id": "active_catgirl", "text": "哥哥，要进来避一会儿雨吗？"},
        ],
        "suggested_inputs": ["走进花店，问她是否还记得自己"],
    }, ensure_ascii=False))

    async def fake_create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(numeric_v2_actor, "create_chat_llm_async", fake_create)
    monkeypatch.setattr(numeric_v2_actor, "_load_character_profile", lambda *args, **kwargs: "安静而认真。")
    monkeypatch.setattr(numeric_v2_actor, "_load_player_address", lambda *args, **kwargs: "哥哥")

    performance = await NumericV2Actor(_ConfigManager()).generate_opening(engine=engine)

    prompt = "\n".join(str(message.content) for message in client.calls[0])
    assert performance["content"][0] == {"type": "narration", "text": "测试猫娘抬眼看向门口。"}
    assert '"opening_phase":true' in prompt
    assert '"visible_player_history":[]' in prompt
    assert '"current_chapter_title":"重逢"' in prompt
    assert '"core_persona":"安静而认真。"' in prompt
    assert '"relationship_state":{"trust":"戒备"}' in prompt
    assert "角色卡决定用词、句长、主动性和情绪外显方式" in prompt
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
