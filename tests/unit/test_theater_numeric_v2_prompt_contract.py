"""验证普通 Numeric v2 Actor 使用六块上下文，而不是旧的内部状态树。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from services.theater import numeric_v2_actor
from services.theater.numeric_v2_actor import NumericV2Actor, _turn_messages
from services.theater.numeric_v2_actor_output import (
    NumericV2ActorOutputError,
    _parse_actor_suggestions,
    _parse_output,
)
from services.theater.numeric_v2_cast import NumericV2CastProjection
from services.theater.numeric_v2_evaluator import _build_messages as _build_evaluator_messages
from services.theater.numeric_v2_evaluator import (
    NumericV2EvaluatorOutputError,
    _build_transition_judge_messages,
    _parse_transition_judge_output,
)
from services.theater.numeric_v2_evaluator import _parse_output as _parse_evaluator_output
from services.theater.numeric_v2_context import (
    scene_narrative_focus,
)
from services.theater.numeric_v2_runtime import NumericV2Engine, TurnRequestV2
from tests.unit.test_theater_numeric_v2_contract import numeric_v2_1_story, numeric_v2_story


def _session(engine: NumericV2Engine):
    """构造当前 v2.2 Prompt 合同使用的最小 Session。"""

    return engine.create_session(
        session_id="prompt_contract",
        catgirl_binding={
            "catgirl_id": "catgirl:test",
            "catgirl_name": "测试猫娘",
            "player_address": "哥哥",
        },
        opening_performance={"performance": "（抬眼）开场。", "suggested_inputs": []},
    )


def _payload(messages):
    """读取 Human Prompt 中的六块 JSON，避免测试依赖消息对象的具体实现。"""

    return json.loads(messages[1].content.split("：\n", 1)[1])


def test_numeric_v2_turn_prompt_uses_six_blocks_in_fixed_order():
    # 普通回合只保留产品已确认的六块数据，并把玩家输入放在最后。
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("six_blocks", 0, "我先听你把话说完。"),
        (),
        scene_complete=False,
    )

    messages = _turn_messages(
        engine,
        session,
        outcome,
        "我先听你把话说完。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    )
    payload = _payload(messages)

    assert list(payload) == [
        "role",
        "current_scene",
        "story_so_far",
        "pacing",
        "next_scene",
        "player_input",
    ]
    assert payload["current_scene"] == "雨后的花店门铃轻轻响起。"
    assert "玩家：我先听你把话说完。" not in payload["story_so_far"]
    assert "开场" in payload["story_so_far"]
    assert "当前是第 1 回合" in payload["pacing"]
    assert "推荐 4 回合" in payload["pacing"]
    assert "当前环境、NPC 与行动结果由你依据已知事实演出" in payload["pacing"]
    assert "玩家说‘可能有’" in payload["pacing"]
    assert "不能把被询问的可能性写成猫娘已观测到" in payload["pacing"]
    assert "不得通过一次试插、搜索、观察或角色感觉" in payload["pacing"]
    assert "说完‘不知道’后不得再用" in payload["pacing"]
    assert "不得顺势确认并新增岔路、机关、障碍或检查步骤" in payload["pacing"]
    assert "跑题本身不表示当前互动已经完成" in payload["pacing"]
    assert "不能被反向当成当前转场的因果证据" in payload["pacing"]
    assert "不能成为新转场提议的理由" in payload["pacing"]
    assert "不得反问玩家看见、听见、摸到或发生了什么" in payload["pacing"]
    assert "推荐第一条也应执行这段完整行动" in payload["pacing"]
    assert "动作可省略‘我’" in payload["pacing"]
    assert "不能明写其他人或环境为主体" in payload["pacing"]
    assert payload["player_input"] == "我先听你把话说完。"
    assert "scene_horizon" not in payload
    assert "current_story_beat" not in payload
    assert "先回应玩家最新输入" in messages[0].content
    assert "意图或尝试" in messages[0].content
    assert "幕内调查、试验、取物、修复、休息" in messages[0].content
    assert "假设性的地点、愿望或闲聊答案不自动变成真实计划" in messages[0].content
    assert "不得把跑题地点当成未经 Runtime 换幕的新临时场景" in messages[0].content
    assert "当前关系距离是本轮亲密表现上限" in messages[0].content
    assert "不能用尾巴、耳朵等微动作绕过" in messages[0].content
    assert "不得自行把它具体化为新设施、部件、机制、地点或结果" in messages[0].content
    assert "不能把普通的休息、安静等待或恢复精神自动写成待机、低功耗、充电" in messages[0].content
    assert "作者明确写出的因果先后不能倒置" in messages[0].content
    assert "没有回答猫娘刚提出的问题" in messages[0].content
    assert "不得由猫娘代填答案" in messages[0].content
    assert "不得保留方括号占位符" in messages[0].content
    assert "玩家可以自然补充不与作者硬边界和 story_so_far 冲突的低风险细节" in messages[0].content
    assert "不能仅凭这句话自动成立" in messages[0].content
    assert "不要把连续推进切碎成多轮" in messages[0].content
    assert "推进到下一个真正需要玩家决定的位置" in messages[0].content
    assert "不要反问玩家‘看到了吗、听到了吗’" in messages[0].content
    assert "不要推荐只前进一步、再看一次、再听一次" in messages[0].content
    assert "只要它不会明确违背 next_scene 即可" in messages[0].content
    assert "危险正在迫近时可以提议继续撤离" in messages[0].content
    assert "pacing 的软收束建议不能覆盖真正必要的因果" in messages[0].content
    assert "停在最后可撤回的时点" in messages[0].content
    assert "无论 transition_offered 为 true 还是 false" in messages[0].content


def test_numeric_v2_actor_uses_ephemeral_interaction_intent_in_pacing():
    """交互意图只进入本轮节奏提示，不增加 Prompt 块或 Runtime 状态。"""

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("interaction_intent", 0, "你现在是不是有点害怕？"),
        (),
        scene_complete=False,
    )

    chat_messages = _turn_messages(
        engine,
        session,
        outcome,
        "你现在是不是有点害怕？",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
        interaction_intent="chat",
    )
    chat_payload = _payload(chat_messages)
    action_payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "（推开门）我先看看外面。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
        interaction_intent="scene_action",
    ))
    mixed_payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "你别怕，我现在把门推开。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
        interaction_intent="mixed_or_unclear",
    ))

    assert list(chat_payload) == list(action_payload) == list(mixed_payload)
    assert "纯闲聊回应合同" in chat_messages[0].content
    assert "transition_offered 必须为 false" in chat_messages[0].content
    assert "至多一条当前幕内的回归剧情选项" in chat_messages[0].content
    assert "不得离开当前幕或替玩家决定路线" in chat_messages[0].content
    assert "本轮主要是当前场景内的闲聊" in chat_payload["pacing"]
    assert "不能把闲聊当成转场接受" in chat_payload["pacing"]
    assert "先自然回应一句，再承接" not in chat_payload["pacing"]
    assert "交付一项最关键的作者方向事实" not in chat_payload["pacing"]
    assert chat_payload["next_scene"] == "本轮纯闲聊，不使用下一幕方向。"
    assert action_payload["next_scene"] != chat_payload["next_scene"]
    assert "直接交付这个行动" in action_payload["pacing"]
    assert "先回应其中的对白或情绪" in mixed_payload["pacing"]


@pytest.mark.asyncio
async def test_numeric_v2_actor_accepts_safe_chat_repeat_on_final_retry(monkeypatch):
    """纯闲聊耗尽改写次数时宁可保留安全短回应，也不能整轮发送失败。"""

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(
        _session(engine),
        revision=1,
        node_turn_count=1,
        performance_history=({
            "revision": 1,
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "我想听听你的感受。",
            "performance": "（轻轻摇头）我只是觉得心里空落落的，好像有什么很重要的东西怎么也想不起来。",
        },),
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("chat_repeat", 1, "那你现在是什么感受？"),
        (),
        scene_complete=False,
    )
    actor = NumericV2Actor(object())

    async def fake_invoke(_messages, **_kwargs):
        return {
            "performance": "（轻轻摇头）我只是觉得心里空落落的，好像有什么很重要的东西怎么也想不起来。",
            "suggested_inputs": [
                "（坐在一旁）你愿意再说一点吗？",
                "（轻轻点头）我们也可以安静待一会儿。",
            ],
            "transition_offered": False,
        }

    monkeypatch.setattr(actor, "_invoke", fake_invoke)

    result = await actor.generate_turn(
        engine=engine,
        session=session,
        outcome=outcome,
        player_input="那你现在是什么感受？",
        character_profile="安静克制，习惯用短句回应。",
        retry_hint="这是最后一次重复输出重试。",
        interaction_intent="chat",
    )

    assert "心里空落落的" in result["performance"]
    assert result["transition_offered"] is False


def test_numeric_v2_transition_prompt_preserves_causal_order_and_abstract_boundaries():
    """正式换场也不能跳过目标幕前提或把休息脑补成机体机制。"""  # noqa: DOCSTRING_CJK

    system = numeric_v2_actor._system_prompt(
        catgirl_name="测试猫娘",
        player_address="哥哥",
        phase="transition_compact",
    )

    assert "作者剧情方向是导演信息" in system
    assert "明确的因果先后不能倒置" in system
    assert "target_scene.opening_situation 已明确建立的内容" in system
    assert "不能把普通休息、等待或恢复精神自动写成待机、低功耗、充电" in system


def test_numeric_v2_turn_prompt_keeps_opening_and_complete_scene_direction():
    """当前幕不能只剩开场画面，否则长对话或跑题后会丢失作者因果线。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    engine.nodes["start"]["story_beat"]["opening_scene"] = "雨后的花店门铃轻轻响起。"
    engine.nodes["start"]["story_beat"]["summary"] = (
        "小岚先回应重逢，再围绕仍未拆开的旧信与玩家商量下一步。"
    )
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("complete_scene_direction", 0, "我先听你把话说完。"),
        (),
        scene_complete=False,
    )

    messages = _turn_messages(
        engine,
        session,
        outcome,
        "我先听你把话说完。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    )
    payload = _payload(messages)

    assert "当前已进入的开场处境：雨后的花店门铃轻轻响起。" in payload["current_scene"]
    assert "本幕完整剧情方向（自然演绎，不是任务清单，也不是已发生事实）" in payload["current_scene"]
    assert "围绕仍未拆开的旧信" in payload["current_scene"]


def test_numeric_v2_turn_prompt_keeps_hard_boundaries_without_allowed_checklist():
    """普通 Actor 保留禁演与事实边界，但不每轮复述 allowed 清单。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    engine.nodes["start"]["story_beat"]["acting_contract"] = {
        "allowed_behaviors": ["回应旧信的来历后保留自己的判断。"],
        "forbidden_behaviors": ["不得主动拥抱或把戒备写成依赖。"],
    }
    engine.nodes["start"]["story_beat"]["character_state"] = {
        "scene_boundaries": ["不得在玩家选择前离开花店。"],
    }
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("acting_boundaries", 0, "我会保持距离。"),
        (),
        scene_complete=False,
    )

    payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "我会保持距离。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))

    assert "本幕明确允许的演绎边界" not in payload["role"]
    assert "回应旧信的来历后保留自己的判断" not in payload["role"]
    assert "不得主动拥抱或把戒备写成依赖" not in payload["role"]
    assert "不得在玩家选择前离开花店" not in payload["role"]
    messages = _turn_messages(
        engine,
        session,
        outcome,
        "我会保持距离。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    )
    assert "以下为本轮作者硬边界" in messages[0].content
    assert "不得主动拥抱或把戒备写成依赖" in messages[0].content
    assert "不得在玩家选择前离开花店" in messages[0].content


def test_numeric_v2_structured_persona_traits_are_tone_only():
    """核心特质不得绕过当前关系合同变成亲密动作或既有关系。"""  # noqa: DOCSTRING_CJK

    profile = numeric_v2_actor._profile_for_acting_contract(
        "昵称: 小葵\n自称: 人家\n核心特质: 温柔体贴,粘人撒娇\n行为特点: 喜欢蹭手心",
        {"persona_scope": "style_only"},
    )

    assert "自称: 人家" in profile
    assert "语言氛围参考（只影响措辞" in profile
    assert "温柔体贴,粘人撒娇" in profile
    assert "行为特点" not in profile


def test_numeric_v2_overdue_pacing_requires_new_result_and_concrete_exit():
    # 超过推荐回合两轮后，Prompt 必须阻止重复催促，并要求可见推进与具体出口。
    pacing = numeric_v2_actor._soft_pacing(
        {"min_turns": 1, "recommended_turns": 3},
        5,
        route_changed=False,
    )

    assert pacing["phase"] == "overdue"
    assert "新的可见结果" in pacing["instruction"]
    assert "不要重复上一轮的命令" in pacing["instruction"]
    assert "观察、复述或要求再确认" in pacing["instruction"]
    assert "休息、过夜或等待" in pacing["instruction"]
    assert "具体离幕下一步" in pacing["instruction"]
    assert "同一个连续行动最多用一轮建立风险与选择" in pacing["instruction"]
    assert "不能把靠近、踏一步、再靠近、触碰和拿取拆成连续多轮" in pacing["instruction"]
    assert "观察、搜索、检查、照护、等待或闲聊等低信息动作" in pacing["instruction"]
    assert "已经打开的核心冲突" in pacing["instruction"]


def test_numeric_v2_recommended_turn_is_not_a_forced_offer_countdown():
    """刚到推荐回合只做软聚焦，不能要求 Actor 无条件提出转场。"""  # noqa: DOCSTRING_CJK

    pacing = numeric_v2_actor._soft_pacing(
        {"min_turns": 3, "recommended_turns": 3},
        3,
        route_changed=False,
    )

    assert pacing["phase"] == "closure"
    assert "只是软节奏参考" in pacing["instruction"]
    assert "不是必须提议或换幕的倒计时" in pacing["instruction"]
    assert "已有自然出口时可以提出" in pacing["instruction"]


def test_numeric_v2_soft_boundary_prefers_authored_fact_over_new_task_chain():
    """软收束时核心冲突未建立，应直接演出作者事实，不能不断增设调查步骤。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(_session(engine), node_turn_count=3)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("soft_boundary_focus", 0, "我再检查一下眼前的线索。"),
        (),
        scene_complete=False,
    )

    pacing = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "我再检查一下眼前的线索。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))["pacing"]

    assert "不是逐项目标检查" in pacing
    assert "交付一项最关键的作者方向事实" in pacing
    assert "不得另造需要多轮搜索、试错或解锁" in pacing
    assert "不要再开启新的幕内问题" in pacing


def test_numeric_v2_turn_prompt_keeps_real_current_scene_history():
    # 已提交的玩家输入和猫娘回复必须进入 story_so_far，推荐草稿不能代替真实历史。
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    history_record = {
        "revision": 1,
        "from_node_id": "start",
        "to_node_id": "start",
        "input_text": "我愿意先留下来听你解释。",
        "performance": "（抬眼）那就先坐下。",
    }
    session = replace(
        session,
        revision=1,
        node_turn_count=1,
        performance_history=(history_record,),
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("six_blocks_history", 1, "我坐到了窗边。"),
        (),
        scene_complete=False,
    )

    payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "我坐到了窗边。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))

    assert "玩家：我愿意先留下来听你解释。" in payload["story_so_far"]
    assert "猫娘：（抬眼）那就先坐下。" in payload["story_so_far"]
    assert payload["player_input"] == "我坐到了窗边。"


def test_numeric_v2_first_turn_after_transition_keeps_only_short_source_tail():
    """新幕首回合只承接旧幕末尾的可见余波，不恢复旧输入或旧任务。"""

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    transition_record = {
        "revision": 1,
        "from_node_id": "ending_leave",
        "to_node_id": "start",
        "input_text": "我答应替你继续追查旧信和钥匙。",
        "segments": [
            {
                "phase": "source_response",
                "performance": (
                    "（收起旧信）旧信和钥匙明天继续追查。"
                    "（抹去眼泪）我已经没事了。"
                ),
            },
            {
                "phase": "transition_bridge",
                "scene_narration": "雨声渐渐停下。",
            },
            {
                "phase": "target_opening",
                "scene_narration": "清晨的花店重新亮起灯。",
            },
        ],
    }
    session = replace(
        _session(engine),
        revision=1,
        node_turn_count=0,
        performance_history=(transition_record,),
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("first_turn_after_transition", 1, "你眼睛还红着。"),
        (),
        scene_complete=False,
    )

    payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "你眼睛还红着。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))

    assert "上一幕尾声" in payload["story_so_far"]
    assert "（抹去眼泪）我已经没事了。" in payload["story_so_far"]
    assert "旧信和钥匙明天继续追查" not in payload["story_so_far"]
    assert "我答应替你继续追查旧信和钥匙" not in payload["story_so_far"]
    assert "雨声渐渐停下" in payload["story_so_far"]
    assert "清晨的花店重新亮起灯" in payload["story_so_far"]


def test_numeric_v2_previous_scene_tail_disappears_after_first_current_scene_turn():
    """新幕已经产生普通回合后，旧幕尾声不再重复注入 Prompt。"""

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(
        _session(engine),
        revision=2,
        node_turn_count=1,
        performance_history=(
            {
                "revision": 1,
                "from_node_id": "ending_leave",
                "to_node_id": "start",
                "input_text": "我答应替你继续追查旧信。",
                "segments": [
                    {
                        "phase": "source_response",
                        "performance": "（抹去眼泪）我已经没事了。",
                    },
                    {
                        "phase": "transition_bridge",
                        "scene_narration": "雨声渐渐停下。",
                    },
                    {
                        "phase": "target_opening",
                        "scene_narration": "清晨的花店重新亮起灯。",
                    },
                ],
            },
            {
                "revision": 2,
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "我把窗帘拉开了。",
                "performance": "（眯起眼）晨光有点亮。",
            },
        ),
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("second_turn_after_transition", 2, "我替你挡一下光。"),
        (),
        scene_complete=False,
    )

    payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "我替你挡一下光。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))

    assert "上一幕尾声" not in payload["story_so_far"]
    assert "抹去眼泪" not in payload["story_so_far"]
    assert "我答应替你继续追查旧信" not in payload["story_so_far"]
    assert "玩家：我把窗帘拉开了。" in payload["story_so_far"]
    assert "猫娘：（眯起眼）晨光有点亮。" in payload["story_so_far"]


def test_numeric_v2_turn_prompt_separates_opening_facts_from_scene_direction():
    """普通回合保留完整方向，但明确它不是事实或逐项任务。"""

    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["summary"] = (
        "先观察门外，再修复旧信，最后提出离开；这些都是作者方向而非当前事实。"
    )
    story["nodes"][0]["story_beat"]["opening_scene"] = "雨后的花店门口只有一盏昏黄路灯。"
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("opening_projection", 0, "我先看看门外。"),
        (),
        scene_complete=False,
    )

    messages = _turn_messages(
        engine,
        session,
        outcome,
        "我先看看门外。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    )
    payload = _payload(messages)

    assert "当前已进入的开场处境：雨后的花店门口只有一盏昏黄路灯。" in payload["current_scene"]
    assert "本幕完整剧情方向（自然演绎，不是任务清单，也不是已发生事实）" in payload["current_scene"]
    assert "先观察门外" in payload["current_scene"]
    assert "尚未发生的未来内容不能当作事实" in messages[0].content


def test_numeric_v2_turn_prompt_does_not_guess_unresolved_next_scene():
    # 多出口尚未由 Runtime 选定时，下一幕只保留未知提示，不能替玩家猜路线。
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("six_blocks_unresolved", 0, "先看看花店。"),
        (),
        scene_complete=False,
    )

    payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "先看看花店。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))

    assert payload["next_scene"] == (
        "下一幕尚未确定；玩家接受具体转场提议后由 Runtime 决定。"
    )


def test_numeric_v2_turn_prompt_only_exposes_next_scene_direction():
    """普通回合只收到下一幕方向，不能提前读取下一幕完整摘要。"""

    next_scene = numeric_v2_actor._next_scene_summary_text({
        "status": "after_acceptance_only",
        "transition_direction": "沿着长街寻找旧信。",
        "summary_after_acceptance": "旧信与备用钥匙并排放在桌面上。",
    })

    assert next_scene.startswith("接受当前转场提议后，剧情方向是：沿着长街寻找旧信。")
    assert "不是目标清单或固定动作" in next_scene
    assert "语义等价方案" in next_scene
    assert "旧信与备用钥匙并排放在桌面上" not in next_scene


def test_numeric_v2_turn_prompt_allows_same_place_ending_closure():
    """结局节点即使不改变地点，也要给 Actor 一个可执行的收束方向。"""

    story = numeric_v2_story()
    # 只保留一个结局出口，使普通回合能够确定这是结局收束而非未决分支。
    # 选择测试初始信任度可达的“离开”结局出口，避免制造不可达节点。
    story["nodes"][0]["route_gates"] = story["nodes"][0]["route_gates"][1:]
    story["nodes"] = [
        node for node in story["nodes"] if node["id"] != "ending_stay"
    ]
    story["endings"] = [ending for ending in story["endings"] if ending["id"] == "leave"]
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), node_turn_count=5)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("same_place_ending", session.revision, "我先陪你把日志看完。"),
        (),
        scene_complete=False,
    )

    payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "我先陪你把日志看完。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))

    assert payload["next_scene"].startswith("接受当前转场提议后进入结局收束")
    assert "具体收束动作" in payload["pacing"]
    assert "雨停后的长街恢复了安静" not in payload["next_scene"]
    assert "不得提前描写结局独有的地点" in payload["next_scene"]


def test_numeric_v2_prompts_share_a_non_task_narrative_focus():
    """Actor 与 Evaluator 都能看到同一条叙事重心，但不产生目标字段。"""

    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["narrative_focus"] = "先听完她对旧信的解释。"
    engine = NumericV2Engine.from_mapping(story)
    session = _session(engine)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("shared_focus", 0, "我继续听。"),
        (),
        scene_complete=False,
    )

    actor_payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "我继续听。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))
    evaluator_payload = json.loads(
        _build_evaluator_messages(engine, session, "我继续听。")[1]
        .content.split("：\n", 1)[1]
    )

    assert "先听完她对旧信的解释" in actor_payload["pacing"]
    assert evaluator_payload["current_story_beat"]["narrative_focus"] == (
        "先听完她对旧信的解释。"
    )


def test_numeric_v2_legacy_focus_prefers_scene_direction_over_opening():
    """旧剧本没有显式重心时，应避免每回合重复把开场画面当作创作重点。"""

    beat = {
        "opening_scene": "门边的旧灯正在闪烁，桌上放着一枚未开启的徽章。",
        "summary": "两人已经确认灯光来自走廊深处，接下来可以沿着声音寻找出口。",
        "transition_goal": "沿着走廊深处的声音寻找出口，并在玩家同意后离开房间。",
    }

    assert scene_narrative_focus(beat) == beat["transition_goal"]


def test_numeric_v2_pending_transition_is_highlighted_for_recommendations():
    """待确认转场的具体正文应从长历史中单独投影，避免推荐只看到布尔状态。"""

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(
        _session(engine),
        revision=1,
        node_turn_count=1,
        transition_offered=True,
        performance_history=(
            {
                "revision": 1,
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "我想继续听你说。",
                "performance": "（望向门外）我们可以沿着长街去找旧信，你愿意现在出发吗？",
                "transition_offered": True,
            },
        ),
    )
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("pending_transition", 1, "我先看看你的状态。"),
        (),
        scene_complete=False,
        transition_intent="unclear",
    )

    payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "我先看看你的状态。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))

    assert "上一轮具体提议原文" in payload["pacing"]
    assert "沿着长街去找旧信" in payload["pacing"]
    assert "必须包含一条明确接受并亲自执行上一轮具体提议的路径，且必须放在第一条" in payload["pacing"]
    assert "第二条必须是明确拒绝、暂缓或当前幕替代路径" in payload["pacing"]


def test_numeric_v2_simple_prompt_packing_drops_oldest_complete_history():
    # 预算不足时只淘汰最早整条记录，最新回合和玩家输入始终保留。
    history = [
        {"revision": 1, "player_input": "旧回合一", "performance": "（动作）旧回复一"},
        {"revision": 2, "player_input": "旧回合二", "performance": "（动作）旧回复二"},
        {"revision": 3, "player_input": "最新回合", "performance": "（动作）最新回复"},
    ]
    data = {
        "role": "你是测试猫娘。",
        "current_scene": "当前幕剧情方向。",
        "story_so_far": "",
        "pacing": "当前是第 3 回合，本幕推荐 4 回合。",
        "next_scene": "下一幕剧情方向。",
        "player_input": "我继续观察。",
    }

    fitted = numeric_v2_actor._fit_simple_turn_prompt_data(
        system_prompt="只回应玩家。",
        human_prefix="以下 JSON 是本回合六块演绎上下文：\n",
        data=data,
        history_rows=history,
        max_tokens=95,
    )

    assert "旧回合一" not in fitted["story_so_far"]
    assert "最新回合" in fitted["story_so_far"]
    assert fitted["player_input"] == "我继续观察。"


def test_numeric_v2_scene_fact_index_keeps_early_committed_progress() -> None:
    """长幕完整历史被裁剪后，早期已经完成的玩家行动仍须作为可见事实保留。"""

    session = type("Session", (), {
        "current_node_id": "medical",
        "performance_history": (
            {
                "revision": 1,
                "from_node_id": "medical",
                "to_node_id": "medical",
                "input_text": "我已经找到聚在一起的三名平民。",
                "performance": "（守住门口）我看见你们了。",
            },
            {
                "revision": 2,
                "from_node_id": "medical",
                "to_node_id": "medical",
                "input_text": "我先带第一名伤员出去。",
                "performance": "（让出通道）交给我接应。",
            },
        ),
    })()

    fact_index = numeric_v2_actor._current_scene_fact_index_text(session)

    assert "已经找到聚在一起的三名平民" in fact_index
    assert "先带第一名伤员出去" in fact_index


def test_numeric_v2_simple_prompt_packing_drops_previous_scene_tail_first():
    """预算紧张时优先舍弃临时旧幕余波，不删除换场事实或当前输入。"""

    history = [{
        "revision": 1,
        "player_input": "",
        "segments": [
            {
                "phase": "previous_scene_tail",
                "performance": "（眼眶微红）我会慢慢平静下来。",
            },
            {
                "phase": "transition_bridge",
                "scene_narration": "雨声停下。",
            },
            {
                "phase": "target_opening",
                "scene_narration": "清晨的花店重新亮起灯。",
            },
        ],
    }]
    without_tail = [{
        **history[0],
        "segments": history[0]["segments"][1:],
    }]
    data = {
        "role": "你是测试猫娘。",
        "current_scene": "清晨的花店。",
        "story_so_far": "",
        "pacing": "当前是第 1 回合，本幕推荐 3 回合。",
        "next_scene": "下一幕尚未确定。",
        "player_input": "我注意到你眼睛还红着。",
    }
    system_prompt = "只回应玩家。"
    human_prefix = "以下 JSON 是本回合六块演绎上下文：\n"
    expected_data = {
        **data,
        "story_so_far": numeric_v2_actor._story_so_far_text(without_tail),
    }
    exact_budget = (
        numeric_v2_actor.count_tokens(system_prompt)
        + numeric_v2_actor.count_tokens(
            human_prefix
            + json.dumps(expected_data, ensure_ascii=False, separators=(",", ":"))
        )
    )

    fitted = numeric_v2_actor._fit_simple_turn_prompt_data(
        system_prompt=system_prompt,
        human_prefix=human_prefix,
        data=data,
        history_rows=history,
        max_tokens=exact_budget,
    )

    assert "上一幕尾声" not in fitted["story_so_far"]
    assert "雨声停下" in fitted["story_so_far"]
    assert "清晨的花店重新亮起灯" in fitted["story_so_far"]
    assert fitted["player_input"] == "我注意到你眼睛还红着。"


def test_numeric_v2_actor_parses_visible_suggestions_in_same_output():
    # 正文和推荐必须来自同一个 JSON；推荐只保留可直接提交的第一人称动作加对白。
    parsed = _parse_output(json.dumps({
        "performance": "（抬眼）我听到了。",
        "transition_offered": True,
        "suggested_inputs": [
            "（我点头）我先听你说完。",
            "（我后退一步）我想先看看周围。",
        ],
    }, ensure_ascii=False))

    assert parsed["performance"] == "（抬眼）我听到了。"
    assert parsed["transition_offered"] is True
    assert parsed["suggested_inputs"] == [
        "（我点头）我先听你说完。",
        "（我后退一步）我想先看看周围。",
    ]


def test_numeric_v2_actor_keeps_body_when_suggestions_are_malformed():
    # 推荐格式失败只降级为空列表，不能因为推荐脏数据丢弃已经合法的猫娘正文。
    parsed = _parse_output(json.dumps({
        "performance": "（抬眼）我听到了。",
        "transition_offered": False,
        "suggested_inputs": ["这不是第一人称推荐", "（我点头）"],
    }, ensure_ascii=False))

    assert parsed["performance"] == "（抬眼）我听到了。"
    assert parsed["transition_offered"] is False
    assert parsed["suggested_inputs"] == []


def test_numeric_v2_actor_drops_suggestion_template_placeholders():
    """一键发送推荐不能把未替换的姓名占位符展示给玩家。"""  # noqa: DOCSTRING_CJK

    parsed = _parse_output(json.dumps({
        "performance": "（抬眼）你可以先介绍自己。",
        "transition_offered": False,
        "suggested_inputs": [
            "（我保持距离）我叫[你的名字]，是这里的修理工。",
            "（我放下双手）我是这里的修理工。",
            "（我指向工作台）这里是我的工作室。",
        ],
    }, ensure_ascii=False))

    assert parsed["suggested_inputs"] == [
        "（我放下双手）我是这里的修理工。",
        "（我指向工作台）这里是我的工作室。",
    ]


def test_numeric_v2_actor_rejects_non_boolean_transition_flag():
    # 转场状态只能由 Actor 的显式布尔字段交付，字符串不能被当成真值猜测。
    with pytest.raises(NumericV2ActorOutputError, match="transition_offered_invalid"):
        _parse_output(json.dumps({
            "performance": "（抬眼）我听到了。",
            "suggested_inputs": [],
            "transition_offered": "是",
        }, ensure_ascii=False))


def test_numeric_v2_actor_suggestion_fill_is_limited_to_one_lightweight_call(monkeypatch):
    # 初次正文调用缺少推荐时，只允许一次 suggestions_only 补全，且不重写正文。
    actor = NumericV2Actor(object())
    calls = []

    async def fake_invoke(_messages, **kwargs):
        calls.append(kwargs)
        assert kwargs.get("suggestions_only") is True
        return {
            "suggested_inputs": [
                "（我点头）我先听你说完。",
                "（我侧身）我想先看看周围。",
            ]
        }

    monkeypatch.setattr(actor, "_invoke", fake_invoke)

    import asyncio

    suggestions = asyncio.run(actor._ensure_suggestions(
        performance={"performance": "（抬眼）我听到了。", "suggested_inputs": []},
        player_input="我先听你说。",
        catgirl_name="测试猫娘",
        max_input_tokens=900,
        hard_boundaries=["不得主动亲密接触。"],
    ))

    assert len(calls) == 1
    assert suggestions == [
        "（我点头）我先听你说完。",
        "（我侧身）我想先看看周围。",
    ]
    assert actor.suggestion_fill_attempt_count == 1
    assert actor.suggestion_fill_reason_counts == {
        "invalid_or_missing": 1,
        "transition_refresh": 0,
        "scene_refresh": 0,
    }


def test_numeric_v2_actor_suggestion_parser_reports_anonymous_rejection_reasons():
    """推荐观测只记录格式原因计数，不保存模型原文。"""  # noqa: DOCSTRING_CJK

    diagnostics = {}
    suggestions = _parse_actor_suggestions(
        [
            "（点点头）只有这条省略了动作主语。",
            "（猫娘点头）我们继续。",
            "（我点头）那就继续吧。",
        ],
        diagnostics=diagnostics,
    )

    assert suggestions == [
        "（点点头）只有这条省略了动作主语。",
        "（我点头）那就继续吧。",
    ]
    assert diagnostics["mixed_shape_invalid"] == 0
    assert diagnostics["action_owner_invalid"] == 1
    assert diagnostics["accepted_items"] == 2
    assert diagnostics["insufficient_valid_items"] == 0


def test_numeric_v2_suggestion_fill_boundaries_come_from_authored_scene():
    """补推荐必须取得事实边界与禁演边界，不能只看到已经生成的正文。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    beat = engine.nodes["start"]["story_beat"]
    beat["must_not_happen"] = ["不得把旧信交给猫娘。"]
    beat["summary"] = "先回应玩家，再围绕仍未拆开的旧信自然发展。"
    beat["character_state"] = {
        "scene_boundaries": ["不得在玩家选择前离开花店。"],
    }
    beat["acting_contract"] = {
        "forbidden_behaviors": ["不得主动拥抱。"],
    }
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name="哥哥",
        catgirl_name="测试猫娘",
    )

    boundaries = numeric_v2_actor._suggestion_hard_boundaries(
        cast,
        beat,
        relationship_boundary="不要主动发起肢体接触。",
    )

    assert boundaries == [
        "不要主动发起肢体接触。",
        "不得在玩家选择前离开花店。",
        "不得主动拥抱。",
        "不得把旧信交给猫娘。",
    ]
    assert numeric_v2_actor._beat_for_actor(cast, beat)["boundaries"] == [
        "不得在玩家选择前离开花店。",
        "不得主动拥抱。",
        "不得把旧信交给猫娘。",
    ]
    assert "围绕仍未拆开的旧信" in numeric_v2_actor._beat_for_actor(
        cast,
        beat,
    )["scene_direction"]


def test_numeric_v2_actor_refreshes_suggestions_after_transition_offer(monkeypatch):
    """正文提出转场时，即使原推荐合法，也要单独生成接受与替代路径。"""

    actor = NumericV2Actor(object())
    calls = []

    async def fake_invoke(messages, **kwargs):
        calls.append(messages)
        assert kwargs.get("suggestions_only") is True
        assert kwargs.get("transition_suggestions_only") is True
        assert "accept_input 必须明确接受并亲自执行该提议" in messages[0].content
        assert "accept_input:string" in messages[0].content
        assert "hard_boundaries 是作者硬边界" in messages[0].content
        assert "- 不得主动亲密接触。" in messages[0].content
        payload = json.loads(messages[1].content)
        assert payload["hard_boundaries"] == ["不得主动亲密接触。"]
        return {
            "suggested_inputs": [
                "（我推开门）好，我们现在就离开这里。",
                "（我按住门把手）先别急，我想再确认一次周围。",
            ]
        }

    monkeypatch.setattr(actor, "_invoke", fake_invoke)

    import asyncio

    suggestions = asyncio.run(actor._ensure_suggestions(
        performance={
            "performance": "（望向门外）我们现在离开这里，好吗？",
            "transition_offered": True,
            "suggested_inputs": [
                "（我看看门外）我先观察一下。",
                "（我后退一步）我暂时不走。",
            ],
        },
        player_input="我先看看门外。",
        catgirl_name="测试猫娘",
        max_input_tokens=900,
        hard_boundaries=["不得主动亲密接触。"],
    ))

    assert len(calls) == 1
    assert suggestions[0].startswith("（我推开门）")
    assert actor.suggestion_fill_reason_counts["transition_refresh"] == 1


def test_numeric_v2_transition_suggestion_source_uses_final_target_opening_only():
    """换场补推荐不能继续承接已经结束的来源幕行动。"""  # noqa: DOCSTRING_CJK

    visible = numeric_v2_actor._suggestion_source_text({
        "segments": [
            {
                "phase": "source_response",
                "performance": "（起身）那我们现在离开教室。",
            },
            {
                "phase": "transition_bridge",
                "scene_narration": "第二天放学后，空教室只剩窗外雪光。",
            },
            {
                "phase": "target_opening",
                "scene_narration": "手账翻到第三项约定。",
                "performance": "（翻开手账）你愿意和我谈谈真正想要的未来吗？",
            },
        ],
    })

    assert "真正想要的未来" in visible
    assert "离开教室" not in visible
    assert "第二天放学后" not in visible


def test_numeric_v2_actor_force_refreshes_suggestions_after_route_change(monkeypatch):
    """正式换幕后即使主调用已返回合法按钮，也要只根据目标幕开场刷新。"""  # noqa: DOCSTRING_CJK

    actor = NumericV2Actor(object())
    calls = []

    async def fake_invoke(messages, **kwargs):
        calls.append(messages)
        assert kwargs.get("suggestions_only") is True
        payload = json.loads(messages[1].content)
        assert "真正想要的未来" in payload["visible_performance"]
        assert "离开教室" not in payload["visible_performance"]
        return {
            "suggested_inputs": [
                "（我指向手账）我想先说说自己的选择。",
                "（我稍作思考）你能先解释第三项约定吗？",
            ]
        }

    monkeypatch.setattr(actor, "_invoke", fake_invoke)

    import asyncio

    suggestions = asyncio.run(actor._ensure_suggestions(
        performance={
            "segments": [
                {"phase": "source_response", "performance": "（起身）那我们离开教室。"},
                {
                    "phase": "target_opening",
                    "scene_narration": "手账翻到第三项约定。",
                    "performance": "（翻开手账）你愿意和我谈谈真正想要的未来吗？",
                },
            ],
            "suggested_inputs": [
                "（我推开门）好，我们离开教室。",
                "（我留在原地）先等等。",
            ],
        },
        player_input="好，我们走。",
        catgirl_name="测试猫娘",
        max_input_tokens=900,
        force_refresh=True,
    ))

    assert len(calls) == 1
    assert suggestions[0].startswith("（我指向手账）")
    assert actor.suggestion_fill_reason_counts["scene_refresh"] == 1


def test_numeric_v2_actor_prompt_preserves_its_own_committed_proposals():
    """连续演绎不能否认或偷换猫娘上一轮已经说过的方案。"""  # noqa: DOCSTRING_CJK

    turn_prompt = numeric_v2_actor._system_prompt(
        catgirl_name="测试猫娘",
        player_address="你",
        phase="turn",
    )
    transition_prompt = numeric_v2_actor._system_prompt(
        catgirl_name="测试猫娘",
        player_address="你",
        phase="transition",
    )

    assert "必须承认猫娘自己在 story_so_far 中已经说过" in turn_prompt
    assert "刚才只是打比方/开玩笑" in turn_prompt
    assert "必须承认猫娘自己在 recent_context 中已经说过" in transition_prompt
    assert "suggested_inputs 只承接最终可见的目标" in transition_prompt
    assert "猫娘单方面宣布自己要走" in turn_prompt
    assert "不是目标清单或固定动作" in turn_prompt
    assert "语义等价方案可以承接" in turn_prompt


def test_numeric_v2_hard_boundary_prompt_requires_refusal_or_safe_alternative():
    """玩家诱导越界时，Actor 不能靠交换行动主体来顺从。"""  # noqa: DOCSTRING_CJK

    instruction = numeric_v2_actor._hard_boundary_system_instruction([
        "玩家进入医疗站救人，猫娘留在门口接应。",
    ])

    assert "猫娘必须在正文直接拒绝或提出符合边界的替代做法" in instruction
    assert "不得顺从越界要求、交换玩家与猫娘的行动职责" in instruction


def test_numeric_v2_transition_suggestion_fill_keeps_acceptance_first():
    """转场补推荐由结构化字段确定顺序，不依赖模型自行排列数组。"""  # noqa: DOCSTRING_CJK

    parsed = _parse_output(
        json.dumps({
            "alternative_inputs": [
                "（我按住门把手）先等等，我想再确认一次。",
                "（我退回房间）我决定先留在这里。",
            ],
            "accept_input": "（我推开门）好，我们现在就出发。",
        }, ensure_ascii=False),
        transition_suggestions_only=True,
    )

    assert parsed["suggested_inputs"] == [
        "（我推开门）好，我们现在就出发。",
        "（我按住门把手）先等等，我想再确认一次。",
        "（我退回房间）我决定先留在这里。",
    ]


def test_numeric_v2_actor_counts_real_provider_requests(monkeypatch):
    """供应商计数只在真正调用 ainvoke 时增加，供推测成本报告使用。"""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def ainvoke(self, _messages):
            return type("Response", (), {
                "content": json.dumps({
                    "suggested_inputs": [
                        "（我点头）我愿意继续。",
                        "（我摆手）我想先等等。",
                    ],
                }, ensure_ascii=False),
            })()

    async def fake_model_config(_config_manager):
        return {"model": "test", "base_url": "http://test.invalid"}

    async def fake_create_client(*_args, **_kwargs):
        return FakeClient()

    monkeypatch.setattr(numeric_v2_actor, "_model_config", fake_model_config)
    monkeypatch.setattr(
        numeric_v2_actor,
        "create_chat_llm_async",
        fake_create_client,
    )
    actor = NumericV2Actor(object())

    import asyncio

    suggestions = asyncio.run(actor._ensure_suggestions(
        performance={"performance": "（抬眼）我听到了。", "suggested_inputs": []},
        player_input="我先听你说。",
        catgirl_name="测试猫娘",
        max_input_tokens=900,
    ))

    assert len(suggestions) == 2
    assert actor.provider_call_count == 1
    assert actor.suggestion_fill_provider_call_count == 1


def test_numeric_v2_v22_evaluator_does_not_project_legacy_evidence():
    """v2.2 判定器不再把目标证据字段送入 Prompt 或结果。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_1_story()
    story["meta"]["contract_version"] = "v2.2"
    engine = NumericV2Engine.from_mapping(story)
    session = engine.create_session(
        session_id="v22_evaluator_contract",
        catgirl_binding={"catgirl_name": "测试猫娘", "player_address": "哥哥"},
        opening_performance={"performance": "（抬眼）你回来了。", "suggested_inputs": []},
    )
    messages = _build_evaluator_messages(
        engine,
        session,
        "我先听你说。",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])
    assert "pending_goals" not in payload["current_story_beat"]
    assert '"goal_evidence"' not in messages[0].content

    with pytest.raises(Exception, match="numeric_v2_evaluator_fields_invalid"):
        _parse_evaluator_output(
            json.dumps({
                "scene_complete": True,
                "metric_changes": {},
                "goal_evidence": {"start": [1]},
                "goal_progress": {"start": [1]},
            }, ensure_ascii=False),
            engine,
            "我先听你说。",
            session,
        )


def test_numeric_v2_evaluator_marks_pending_visible_transition_for_acceptance():
    """Evaluator 应看到已提交的具体提议，而不是把未选择的推荐当成历史。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    session = replace(
        session,
        revision=1,
        node_turn_count=1,
        transition_offered=True,
        performance_history=(
            {
                "revision": 1,
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "我先听你说。",
                "performance": "（抬手指向门口）我们现在去外面看看，好吗？",
                "suggested_inputs": ["（我站起身）好，我现在就去外面看看。", "（我摇头）先留在这里。"],
                "transition_offered": True,
            },
        ),
    )
    messages = _build_evaluator_messages(
        engine,
        session,
        "（我站起身）好，我现在就去外面看看。",
    )
    payload = json.loads(messages[1].content.split("：\n", 1)[1])

    assert payload["pending_transition"]["visible_performance"] == (
        "（抬手指向门口）我们现在去外面看看，好吗？"
    )
    assert payload["pending_transition"]["suggested_inputs"] == [
        "（我站起身）好，我现在就去外面看看。",
        "（我摇头）先留在这里。",
    ]
    assert "即使没有出现‘同意’或‘接受’二字，也必须判为 accept" in messages[0].content


def test_numeric_v2_evaluator_distinguishes_followup_topic_shift_and_action():
    """三态合同要区分追问、完全转题与亲自实施，不增加第四种内部状态。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(
        _session(engine),
        revision=1,
        node_turn_count=1,
        transition_offered=True,
        performance_history=(
            {
                "revision": 1,
                "from_node_id": "start",
                "to_node_id": "start",
                "input_text": "我先听你说。",
                "performance": "（抬手指向门口）我们现在去外面看看，好吗？",
                "transition_offered": True,
            },
        ),
    )

    messages = _build_evaluator_messages(engine, session, "外面安全吗？")
    system_prompt = messages[0].content

    assert '"transition_intent":"accept|reject|unclear"' in system_prompt
    assert "追问提议的细节、条件或风险" in system_prompt
    assert "必须判为 unclear；unclear 表示仍保留旧提议" in system_prompt
    assert "完全转向与该提议和当前处境都无关的独立新话题" in system_prompt
    assert "也必须判为 reject" in system_prompt
    assert "reject 只表示撤下并清除旧提议" in system_prompt
    assert "亲自开始实施同方向的下一步是 accept" in system_prompt
    assert '"interaction_intent":"chat|scene_action|mixed_or_unclear"' in system_prompt
    assert "interaction_intent 不参与数值、路线或换幕" in system_prompt
    assert "只要回答依赖环境、设备、物品、路线、风险或可观察物理状态" in system_prompt
    assert "‘读数正常吗’" in system_prompt


def test_numeric_v2_evaluator_parses_ephemeral_interaction_intent():
    """交互意图允许旧输出降级，但拒绝未知分类。"""

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    parsed = _parse_evaluator_output(
        json.dumps({
            "scene_complete": False,
            "transition_intent": "unclear",
            "interaction_intent": "chat",
            "metric_changes": {},
        }, ensure_ascii=False),
        engine,
        "你现在是不是有点害怕？",
        session,
    )
    compatible = _parse_evaluator_output(
        json.dumps({
            "scene_complete": False,
            "transition_intent": "unclear",
            "metric_changes": {},
        }, ensure_ascii=False),
        engine,
        "我先看看。",
        session,
    )

    assert parsed.interaction_intent == "chat"
    assert compatible.interaction_intent == "mixed_or_unclear"
    with pytest.raises(NumericV2EvaluatorOutputError):
        _parse_evaluator_output(
            json.dumps({
                "scene_complete": False,
                "transition_intent": "unclear",
                "interaction_intent": "advance_story",
                "metric_changes": {},
            }, ensure_ascii=False),
            engine,
            "继续。",
            session,
        )


def test_numeric_v2_actor_marks_unresolved_transition_in_pacing():
    """待确认转场仍留在当前幕时，六块 Prompt 要明确禁止提前抵达目标地点。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = replace(_session(engine), transition_offered=True)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("pending_transition_pacing", session.revision, "我先看看周围。"),
        (),
        scene_complete=False,
        transition_intent="unclear",
    )
    payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "我先看看周围。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))

    assert "本回合尚未完成换幕" in payload["pacing"]
    assert "不得把目标地点写成已经抵达" in payload["pacing"]
    assert "亲自执行上一轮具体提议的路径" in payload["pacing"]


def test_numeric_v2_evaluator_ignores_unknown_optional_metric_candidate():
    """未知数值依据只丢弃可选变化，不能阻断本回合其它合法判定。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    parsed = _parse_evaluator_output(
        json.dumps({
            "scene_complete": False,
            "transition_intent": "unclear",
            "metric_changes": {
                "trust": {
                    "strength": "normal",
                    "criterion_id": "trust.increase.999",
                },
            },
        }, ensure_ascii=False),
        engine,
        "我先听你说。",
        session,
    )

    assert parsed.metric_changes == ()
    assert parsed.scene_complete is False


def test_numeric_v2_actor_does_not_force_transition_offer_when_route_is_unresolved():
    """超过软节奏且路线未定时仍只按当前因果收束，不能无条件逼出提议。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    # 测试故事保留两个出口，使 Actor 收到 runtime_unresolved 而不是替 Runtime 选路。
    engine = NumericV2Engine.from_mapping(story)
    session = replace(_session(engine), node_turn_count=5)
    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("unresolved_transition_pacing", session.revision, "我继续观察。"),
        (),
        scene_complete=False,
        transition_intent="unclear",
    )
    payload = _payload(_turn_messages(
        engine,
        session,
        outcome,
        "我继续观察。",
        "安静克制，习惯用短句回应。",
        "测试猫娘",
        "哥哥",
    ))

    assert "必须在正文中提出一个基于当前已知事实的离开当前幕的具体下一步" not in payload["pacing"]
    assert "若当前互动已经自然形成出口" in payload["pacing"]
    assert "尚未形成出口时" in payload["pacing"]


def test_numeric_v2_transition_judge_receives_visible_offer_and_scene_context():
    """转场复核必须看到正文、推荐、当前历史和下一幕方向，而不是只看布尔值。"""

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    messages = _build_transition_judge_messages(
        engine,
        session,
        actor_performance={
            "performance": "（望向门外）我们沿着长街去找旧信，好吗？",
            "scene_narration": "两人仍站在门边，等待玩家决定。",
            "suggested_inputs": [
                "（我点头）我现在就和你一起出发。",
                "（我摇头）先留在这里。",
            ],
        },
        player_input="我想听听你的建议。",
    )
    payload = json.loads(messages[1].content.split("：", 1)[1])

    assert payload["actor_performance"] == "（望向门外）我们沿着长街去找旧信，好吗？"
    assert payload["scene_update"] == "两人仍站在门边，等待玩家决定。"
    assert payload["suggested_inputs"][0].startswith("（我点头）")
    assert "next_scene_direction" in payload
    assert payload["next_scene_direction"]["is_ending"] is True
    assert payload["next_scene_direction"]["direction"] == "当前信任度满足作者路线条件。"
    assert "scene_context" in payload
    assert "scene_fact_index" in payload
    assert "结局可以与当前地点连续" in messages[0].content
    assert "不要求正文提前演出目标结局的地点" in messages[0].content
    assert "不要求正文提前说出下一幕尚未发生的事件" in messages[0].content
    assert "player_action_preserved" in messages[0].content
    assert "scene_boundary_preserved" in messages[0].content
    assert "author_boundaries_preserved" in messages[0].content
    assert "必须先判 author_boundaries_preserved" in messages[0].content
    assert "failure_reason" in messages[0].content
    assert "指出上一版哪项具体表述违反了哪条现有边界" in messages[0].content
    assert "不提出替代剧情、不补充新事实" in messages[0].content
    assert "已明确将接口或设备规格保持未知" in messages[0].content
    assert "offer_present" in messages[0].content
    assert "只要正文仍在操作当前幕对象" in messages[0].content
    assert "不能把字面移动误判成换幕" in messages[0].content
    assert "可点击推荐中出现这种行动" in messages[0].content
    assert "由当前因果线自然导向" in messages[0].content
    assert "不要求本轮 player_input 已经接受" in messages[0].content
    assert "玩家是否接受由下一回合 Evaluator 另行判断" in messages[0].content
    assert "关键词规则" in messages[0].content


def test_numeric_v2_transition_judge_uses_same_nonending_direction_as_actor():
    """普通换幕复核使用路线理由，不能要求 Actor 提前泄露目标幕剧情。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    # 只改变已编译测试引擎中的目标类型，以覆盖普通换幕 Prompt；不改变故事路由条件。
    engine.nodes["ending_leave"]["type"] = "scene"
    engine.nodes["ending_leave"]["terminal"] = False
    messages = _build_transition_judge_messages(
        engine,
        session,
        actor_performance={
            "performance": "（望向门外）我们沿着长街去看看，好吗？",
            "suggested_inputs": ["（我点头）我现在就和你一起出发。"],
        },
        player_input="我想听听你的建议。",
    )
    payload = json.loads(messages[1].content.split("：", 1)[1])

    assert payload["next_scene_direction"]["is_ending"] is False
    assert payload["next_scene_direction"]["direction"] == "当前信任度满足作者路线条件。"
    assert payload["current_scene"]["story_direction"]
    assert "opening_boundary" in payload["next_scene_direction"]
    assert "bridge_boundary" in payload["next_scene_direction"]
    assert "causal_prerequisites" not in payload["next_scene_direction"]
    assert "不与 next_scene_direction 明确冲突" in messages[0].content
    assert "正文与推荐组合" in messages[0].content
    assert "漏写布尔声明" in messages[0].content
    assert "推荐本身已是公开邀请" in messages[0].content
    assert "不是要求 Actor 照说的行动模板" in messages[0].content
    assert "继续撤离危险地点" in messages[0].content
    assert "opening_boundary 与 bridge_boundary 只用于判断正文是否已经偷跑" in messages[0].content
    assert "只用于排除提议与后续方向的明确冲突" in messages[0].content
    assert "不用于检查作者目标、来源引用或路线理由是否逐项完成" in messages[0].content
    assert "不能只因为两者都包含离开、出发或等待" in messages[0].content
    assert "采购、闲逛或临时旁支去向" in messages[0].content
    assert "不得代替 Runtime 判断路线条件或剧情完成度" in messages[0].content
    assert "只出现在 next_scene_direction 或 bridge_boundary" in messages[0].content
    assert "就是泄漏未发生事实" in messages[0].content
    assert "不得用 next_scene_direction 本身" in messages[0].content
    assert "程序就绪仍不等于完成一个离幕动作" in messages[0].content
    assert "只出现这种重复事实时仍是当前幕" in messages[0].content
    assert "停在最后可撤回时点" in messages[0].content
    assert "即使 player_input 已尝试该动作" in messages[0].content
    assert "是否越幕只由 scene_boundary_preserved 单独判断" in messages[0].content
    assert "任何不在该范围内的新地点或新时段" in messages[0].content
    assert "即使它不是 next_scene 的目标地" in messages[0].content
    assert "接口、搜索或救援产生了即时环境结果" in messages[0].content
    assert "入睡、过夜或等待到特定时点" in messages[0].content


def test_numeric_v2_transition_judge_keeps_compact_facts_from_early_scene_turns():
    """长幕的转场复核不能因最近六轮窗口丢掉早期已完成前因。"""  # noqa: DOCSTRING_CJK

    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    session = _session(engine)
    history = tuple(
        {
            "revision": revision,
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": (
                "我已经把三名平民全部带到安全走廊。"
                if revision == 1
                else f"我继续处理当前场景第 {revision} 步。"
            ),
            "performance": (
                "（点头）三人都已安全撤出。"
                if revision == 1
                else f"（观察）第 {revision} 步有了新结果。"
            ),
        }
        for revision in range(1, 10)
    )
    session = replace(
        session,
        revision=9,
        node_turn_count=9,
        performance_history=history,
    )

    messages = _build_transition_judge_messages(
        engine,
        session,
        actor_performance={
            "performance": "（望向门外）我们现在出发，好吗？",
            "suggested_inputs": ["（我点头）好，现在出发。"],
        },
        player_input="现在呢？",
    )
    payload = json.loads(messages[1].content.split("：", 1)[1])

    assert len(payload["scene_context"]) == 6
    # 开场本身也是当前幕的第一个可见事实，因此索引包含开场与九轮对话。
    assert len(payload["scene_fact_index"]) == 10
    assert "三名平民" in payload["scene_fact_index"][1]["player_input"]
    assert "安全撤出" in payload["scene_fact_index"][1]["visible_response"]


def test_numeric_v2_transition_judge_requires_strict_review_fields():
    """复核器必须返回严格布尔字段与受限的具体失败原因。"""

    accepted = _parse_transition_judge_output(
        '{"offer_present":true,"valid":true,"player_action_preserved":true,"scene_boundary_preserved":true,"author_boundaries_preserved":true,"failure_reason":""}'
    )
    premature = _parse_transition_judge_output(
        '{"offer_present":true,"valid":true,"player_action_preserved":false,"scene_boundary_preserved":false,"author_boundaries_preserved":false,"failure_reason":"正文提前进入检修走廊，并补造舱壁可以屏蔽扫描。"}'
    )

    assert accepted.offer_present is True
    assert accepted.valid is True
    assert accepted.player_action_preserved is True
    assert accepted.scene_boundary_preserved is True
    assert accepted.author_boundaries_preserved is True
    assert premature.valid is True
    assert premature.player_action_preserved is False
    assert premature.scene_boundary_preserved is False
    assert premature.author_boundaries_preserved is False
    assert "检修走廊" in premature.failure_reason
    with pytest.raises(NumericV2EvaluatorOutputError):
        _parse_transition_judge_output(
            '{"offer_present":true,"valid":"true","player_action_preserved":true,"scene_boundary_preserved":true,"author_boundaries_preserved":true,"failure_reason":""}'
        )
    with pytest.raises(NumericV2EvaluatorOutputError):
        _parse_transition_judge_output('{"valid":true}')
    no_reason = _parse_transition_judge_output(
        '{"offer_present":true,"valid":false,"player_action_preserved":true,"scene_boundary_preserved":true,"author_boundaries_preserved":false,"failure_reason":""}'
    )
    assert no_reason.author_boundaries_preserved is False
    assert no_reason.failure_reason == ""
    long_reason = _parse_transition_judge_output(
        json.dumps(
            {
                "offer_present": True,
                "valid": False,
                "player_action_preserved": True,
                "scene_boundary_preserved": True,
                "author_boundaries_preserved": False,
                "failure_reason": "越界" * 200,
            },
            ensure_ascii=False,
        )
    )
    assert long_reason.author_boundaries_preserved is False
    assert 0 < len(long_reason.failure_reason) < 400
