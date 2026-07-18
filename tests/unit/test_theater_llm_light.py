"""验证单次演绎模型的结构、上下文、世界边界和安全回退。"""  # noqa: DOCSTRING_CJK

import json
from pathlib import Path

import pytest

from config.prompts.prompts_theater import (
    THEATER_ROUTE_SYSTEM_PROMPT,
    THEATER_TURN_SYSTEM_PROMPT,
    build_theater_route_prompts,
    build_theater_turn_prompts,
)
from services.theater import llm


def _prompt_sections(user_prompt: str) -> tuple[dict, dict]:
    """解析提示词分区，确保测试不会把内部规则误当成公开演绎上下文。"""  # noqa: DOCSTRING_CJK
    envelope = json.loads(user_prompt.split("\n", 1)[1])
    return envelope["公开演绎上下文"], envelope["内部规则（只执行，不复述）"]


class _CharacterConfig:
    """为人格文件边界测试提供最小角色配置。"""  # noqa: DOCSTRING_CJK

    def __init__(self, root: Path):
        self.app_docs_dir = root

    def load_characters(self):
        """只声明当前猫娘，其他目录都不属于可读取角色。"""  # noqa: DOCSTRING_CJK
        return {"当前猫娘": "安全猫娘", "猫娘": {"安全猫娘": {}}}


class _ModelConfig:
    """为结构化模型返回测试提供最小可用配置。"""  # noqa: DOCSTRING_CJK

    def get_model_api_config(self, _kind):
        """返回不会访问真实供应商的占位模型配置。"""  # noqa: DOCSTRING_CJK
        return {"model": "fake-model", "base_url": "https://example.invalid"}


def test_fallback_roleplay_responds_to_user_message():
    """离线角色互动必须自然留在当前事件且不复述越界原话。"""  # noqa: DOCSTRING_CJK
    result = llm.fallback_turn(
        lanlan_name="兰兰",
        scene={"text": "雨夜窗边"},
        node={},
        user_message="我有点担心你",
        progress_kind="roleplay_response",
        callback="",
    )
    assert result["narration"] == ""
    assert "我有点担心你" not in result["dialogue"]
    assert "我听见了" in result["dialogue"]
    assert "好好回应" in result["dialogue"]
    assert "放在心上" not in result["dialogue"]


def test_graph_fallback_never_exposes_generation_guide_as_dialogue():
    """内部演绎意图不是作者台词，缺少正式对白时必须保持为空。"""  # noqa: DOCSTRING_CJK
    result = llm.fallback_turn(
        lanlan_name="兰兰",
        scene={"text": "雨夜窗边"},
        node={
            "summary": "双方仍留在窗边。",
            "runtime_generation_guide": {
                "catgirl_raw_intent": "她先确认现场读数，不总结谁救了谁。"
            },
        },
        user_message="继续",
        progress_kind="graph_progress",
        callback="",
        has_scene_notes=True,
    )

    assert result["dialogue"] == ""


@pytest.mark.asyncio
async def test_offline_fallback_does_not_infer_story_semantics_from_choice_wording():
    """通用层不能因某种作者句式猜目的地；离线时只使用正式作者回退与通用回应。"""  # noqa: DOCSTRING_CJK
    expected = llm.fallback_turn(
        lanlan_name="测试猫娘",
        scene={"text": "候车室"},
        node={},
        user_message="接下来去哪里？",
        progress_kind="roleplay_response",
        callback="",
        choice_options=[
            {"choice_id": "choice_fixture", "label": "抵达东门后查看时刻表"}
        ],
    )
    result = await llm.generate_turn_async(
        config_manager=None,
        lanlan_name="测试猫娘",
        story={"background": "一段用户提供的旅途故事"},
        scene={"text": "候车室"},
        node={},
        user_message="接下来去哪里？",
        progress_kind="roleplay_response",
        callback="",
        state={"scene_notes": []},
        recent_turns=[],
        choice_options=[
            {"choice_id": "choice_fixture", "label": "抵达东门后查看时刻表"}
        ],
    )
    assert result == expected
























def test_model_json_loader_accepts_one_wrapped_object_but_rejects_competing_objects():
    """供应商外围说明可被剥离，但多个竞争 JSON 对象必须整体拒绝。"""  # noqa: DOCSTRING_CJK
    wrapped = (
        '下面是结果：\n```json\n{"narration":"灯亮了。","dialogue":"继续吧。"}\n```'
    )
    assert llm._load_unique_model_json_object(wrapped) == {
        "narration": "灯亮了。",
        "dialogue": "继续吧。",
    }
    with pytest.raises(ValueError):
        llm._load_unique_model_json_object(
            '{"route_kind":"idle"}\n{"route_kind":"free_intent"}'
        )










def test_model_output_requires_narration_for_story_progress():
    """剧情推进缺少旁白时必须拒绝模型结果并回退作者文本。"""  # noqa: DOCSTRING_CJK
    assert (
        llm._parse_output(
            '{"narration":"","dialogue":"继续吧喵"}', progress_kind="graph_progress"
        )
        is None
    )
    assert llm._parse_output(
        '{"narration":"灯亮了。","dialogue":"继续吧喵"}', progress_kind="graph_progress"
    ) == {
        "narration": "灯亮了。",
        "dialogue": "继续吧喵",
        "choice_rewrites": [],
    }




def test_model_output_rejects_internal_terms():
    """模型不得把内部节点或提示词字段显示给玩家。"""  # noqa: DOCSTRING_CJK
    assert (
        llm._parse_output(
            '{"narration":"进入 node_id 下一幕","dialogue":"走吧喵"}',
            progress_kind="graph_progress",
        )
        is None
    )








def test_roleplay_prompt_includes_story_output_guardrails():
    """剧本输出硬边界必须进入模型上下文，且同时由代码在展示前校验。"""  # noqa: DOCSTRING_CJK
    _, user_prompt = build_theater_turn_prompts(
        lanlan_name="糖糖",
        story={
            "background": "公开测试室",
            "runtime_guardrails": {
                "conditional_output_guards": [
                    {
                        "until_fact": {
                            "subject": "pair",
                            "predicate": "is",
                            "object": "confirmed",
                        },
                        "forbidden_phrases": ["挽住手臂"],
                    }
                ]
            },
        },
        scene={"title": "入口", "text": "两人准备出发。"},
        node={"node_id": "node_depart"},
        user_message="我们先去哪里？",
        progress_kind="roleplay_response",
        callback="",
        public_state={},
        recent_turns=[],
        character_profile="",
        choice_options=[],
    )
    payload, internal_rules = _prompt_sections(user_prompt)
    assert "输出硬边界" not in payload
    assert internal_rules["输出硬边界"]["conditional_output_guards"][0][
        "forbidden_phrases"
    ] == ["挽住手臂"]






def test_turn_prompt_exposes_catgirl_story_role_separately_from_personality():
    """剧本职业身份必须独立进入上下文，不能被角色卡中的日常称呼覆盖。"""  # noqa: DOCSTRING_CJK
    _, user_prompt = build_theater_turn_prompts(
        lanlan_name="糖糖",
        story={
            "background": "两名探空队员滞留半开发星球。",
            "scenario_card": {
                "player_role": "系统工程师",
                "catgirl_role": "负责风暴建模与通讯调制的平等队员",
            },
        },
        scene={"title": "受损舱室", "text": "电池再次冒烟。"},
        node={"title": "检查电池"},
        user_message="电池又冒烟了",
        progress_kind="roleplay_response",
        callback="",
        public_state={},
        recent_turns=[],
        character_profile="习惯把玩家叫作主人；活泼黏人",
        choice_options=[],
    )
    payload, _ = _prompt_sections(user_prompt)

    # 两种输入都保留，由系统规则明确当前故事身份拥有称呼和语域优先级。
    assert payload["猫娘人格摘要"].startswith("习惯把玩家叫作主人")
    assert payload["猫娘故事身份"] == "负责风暴建模与通讯调制的平等队员"






def test_recent_context_includes_assistant_narration_and_dialogue():
    """最近上下文必须独立保留对白，不能被较长旁白截断。"""  # noqa: DOCSTRING_CJK
    turns = llm._recent_public_turns(
        [
            {
                "role": "assistant",
                "narration": "她把合同推回桌面。",
                "text": "这一条需要修改喵。",
            }
        ]
    )
    assert turns == [
        {
            "role": "assistant",
            "dialogue": "这一条需要修改喵。",
            "narration": "她把合同推回桌面。",
        }
    ]


def test_assistant_echo_detection_rejects_player_choice_line():
    """猫娘近似照读玩家 Choice 时必须识别为角色反转。"""  # noqa: DOCSTRING_CJK
    assert (
        llm._assistant_echoes_user(
            "哼，既然数据无误……那就一起打开看看最后的真相吧，别手抖喵！",
            "既然数据无误，那就一起打开保险柜看看最后的真相吧",
        )
        is True
    )
    assert (
        llm._assistant_echoes_user(
            "我还需要一点时间想清楚喵。", "我会坐下来听你慢慢说。"
        )
        is False
    )


@pytest.mark.asyncio
async def test_graph_progress_echo_is_soft_and_keeps_model_dialogue(monkeypatch):
    """模型近似复述玩家时只影响文风，不能再触发 Repair 或作者兜底。"""  # noqa: DOCSTRING_CJK

    class _FakeClient:
        """返回可复现角色反转 JSON 的异步模型客户端。"""  # noqa: DOCSTRING_CJK

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def ainvoke(self, _messages):
            return type(
                "Result",
                (),
                {
                    "content": '{"narration":"保险柜被打开。","dialogue":"既然数据无误，那就一起打开保险柜看看最后的真相吧。","choice_rewrites":[]}'
                },
            )()

    async def _create_fake_client(*_args, **_kwargs):
        """绕过真实网络并返回可控客户端。"""  # noqa: DOCSTRING_CJK
        return _FakeClient()

    monkeypatch.setattr(llm, "create_chat_llm_async", _create_fake_client)
    result = await llm.generate_turn_async(
        config_manager=_ModelConfig(),
        lanlan_name="霜瞳",
        story={"background": "旧录音室"},
        scene={"text": "保险柜在墙角。"},
        node={
            "scripted_dialogue": "钥匙给你一半，我们一起打开它喵。",
            "summary": "共同打开保险柜。",
        },
        user_message="既然数据无误，那就一起打开保险柜看看最后的真相吧",
        progress_kind="graph_progress",
        callback="你们共同打开保险柜。",
        state={"scene_notes": ["刚才有过自由互动"]},
        recent_turns=[],
    )
    assert result["dialogue"] == "既然数据无误，那就一起打开保险柜看看最后的真相吧。"
    assert result["narration"] == "你们共同打开保险柜。"


@pytest.mark.asyncio
async def test_graph_progress_repairs_persona_coercion_once(monkeypatch):
    """傲娇人格把共同商量演成不可拒绝命令时，必须纠错一次并保留作者边界。"""  # noqa: DOCSTRING_CJK
    outputs = [
        '{"narration":"星星被扣好。","dialogue":"那今天都听本小姐的，不许有异议喵。","choice_rewrites":[]}',
        '{"narration":"星星被扣好。","dialogue":"本小姐可没打算一个人说了算。想停就停，想换地方也得我们两个都点头喵。","choice_rewrites":[]}',
    ]
    calls = 0
    call_types: list[str] = []

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def ainvoke(self, _messages):
            nonlocal calls
            content = outputs[calls]
            calls += 1
            return type("Result", (), {"content": content})()

    async def _create_fake_client(*_args, **_kwargs):
        return _FakeClient()

    def _record_call_type(value):
        """记录首版演绎与纠错的职责标签，防止两类指标重新混合。"""  # noqa: DOCSTRING_CJK
        call_types.append(value)

    monkeypatch.setattr(llm, "create_chat_llm_async", _create_fake_client)
    monkeypatch.setattr(llm, "set_call_type", _record_call_type)
    result = await llm.generate_turn_async(
        config_manager=_ModelConfig(),
        lanlan_name="霜瞳",
        story={"background": "约会前的家中"},
        scene={"text": "星星挂坠被接住。"},
        node={
            "scripted_dialogue": "今天如果想停或改路线，我们就一起商量。",
            "summary": "两人约定共同商量路线。",
        },
        user_message="接住挂坠",
        progress_kind="graph_progress",
        callback="你把挂坠扣到包上。",
        state={"scene_notes": []},
        recent_turns=[],
    )

    assert calls == 2
    assert call_types == ["theater_actor", "theater_repair"]
    assert "不许有异议" not in result["dialogue"]
    assert "我们两个都点头" in result["dialogue"]
    assert result["narration"] == "你把挂坠扣到包上。"


@pytest.mark.asyncio
async def test_roleplay_drops_unchanged_choice_label_without_repair(monkeypatch):
    """自由对话按钮没有更新时直接恢复作者原文，不能为显示文案额外调用模型。"""  # noqa: DOCSTRING_CJK
    outputs = [
        '{"narration":"","dialogue":"那我们走吧喵。","choice_rewrites":['
        '{"choice_id":"choice_depart","label":"“好，那就一起出发吧。”"}]}',
        '{"narration":"","dialogue":"出发前先把票收好，到了入口再决定第一站喵。",'
        '"choice_rewrites":['
        '{"choice_id":"choice_depart","label":"“好，我收好票，到了入口再和你决定。”"}]}',
    ]
    calls = 0

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def ainvoke(self, _messages):
            nonlocal calls
            content = outputs[calls]
            calls += 1
            return type("Result", (), {"content": content})()

    async def _create_fake_client(*_args, **_kwargs):
        return _FakeClient()

    monkeypatch.setattr(llm, "create_chat_llm_async", _create_fake_client)
    result = await llm.generate_turn_async(
        config_manager=_ModelConfig(),
        lanlan_name="希尔",
        story={"background": "验证开始前的测试室"},
        scene={"text": "两枚测试牌放在透明盒里。"},
        node={"summary": "猫娘邀请玩家共同验证。"},
        user_message="先去哪里呢？",
        progress_kind="roleplay_response",
        callback="",
        state={"scene_notes": []},
        recent_turns=[{"role": "assistant", "text": "你愿意和我一起出发吗？"}],
        choice_options=[
            {
                "choice_id": "choice_depart",
                "label": "“好，那就一起出发吧。”",
                "author_label": "“好，那就一起出发吧。”",
                "choice_mode": "dialogue",
            }
        ],
    )

    assert calls == 1
    assert result["dialogue"] == "那我们走吧喵。"
    assert result["choice_rewrites"] == []


@pytest.mark.asyncio
async def test_roleplay_keeps_first_performance_when_choice_rewrite_is_invalid(
    monkeypatch,
):
    """按钮格式错误只清空显示改写，首版合格演出不得触发 Repair。"""  # noqa: DOCSTRING_CJK
    outputs = iter(
        [
            (
                '{"narration":"电池再次冒出白烟。","dialogue":"压力已经稳住，先处理电池。",'
                '"choice_rewrites":['
                '{"choice_id":"choice_save_power","label":"看着白烟，关闭重复呼叫，把电量留给生命保障"},'
                '{"choice_id":"choice_share_risk","label":"（检查读数）“从现在起，风险都对彼此公开。”"}]}'
            ),
            (
                '{"narration":"电池再次冒出白烟，警报重新亮起。","dialogue":"压力稳定。先断呼叫，别让电池替我们做决定。",'
                '"choice_rewrites":['
                '{"choice_id":"choice_save_power","label":"关闭重复呼叫，把宝贵的电量留给生命保障"},'
                '{"choice_id":"choice_share_risk","label":"“从现在起，风险都对彼此公开。”"}]}'
            ),
        ]
    )
    call_types: list[str] = []

    class _FakeClient:
        """先返回 Choice 类型漂移，再返回只剩核心连续性错误的合格演出。"""  # noqa: DOCSTRING_CJK

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def ainvoke(self, _messages):
            return type("Result", (), {"content": next(outputs)})()

    async def _create_fake_client(*_args, **_kwargs):
        """隔离真实网络，只验证 Actor 与 Repair 的局部降级决策。"""  # noqa: DOCSTRING_CJK
        return _FakeClient()

    def _record_call_type(value):
        """确认一次 Actor 失败后最多进行一次 Repair。"""  # noqa: DOCSTRING_CJK
        call_types.append(value)

    monkeypatch.setattr(llm, "create_chat_llm_async", _create_fake_client)
    monkeypatch.setattr(llm, "set_call_type", _record_call_type)
    result = await llm.generate_turn_async(
        config_manager=_ModelConfig(),
        lanlan_name="星澜",
        story={"background": "两名探空队员困在受损舱室。"},
        scene={"text": "主电池再次冒出白烟。"},
        node={"node_id": "node_power_check"},
        user_message="电池又冒烟了",
        progress_kind="roleplay_response",
        callback="",
        state={"scene_notes": []},
        recent_turns=[],
        choice_options=[
            {
                "choice_id": "choice_save_power",
                "label": "关闭重复呼叫，把电量留给生命保障",
                "author_label": "关闭重复呼叫，把电量留给生命保障",
                "choice_mode": "action",
                "callback": "你关闭重复呼叫，把电量留给生命保障。",
            },
            {
                "choice_id": "choice_share_risk",
                "label": "“从现在起，风险都对彼此公开。”",
                "author_label": "“从现在起，风险都对彼此公开。”",
                "choice_mode": "dialogue",
                "callback": "你提出此后公开所有风险。",
            },
        ],
    )

    assert call_types == ["theater_actor"]
    assert result == {
        "narration": "电池再次冒出白烟。",
        "dialogue": "压力已经稳住，先处理电池。",
        "choice_rewrites": [],
    }


@pytest.mark.asyncio
async def test_roleplay_rejects_uncommitted_choice_result_after_one_repair(monkeypatch):
    """未命中 Choice 时，Actor 与 Repair 都不能把待选动作写成玩家已经完成的事实。"""  # noqa: DOCSTRING_CJK
    outputs = iter(
        [
            '{"narration":"她看着你把桌边的信封递到手边。",'
            '"dialogue":"谢谢你把信封递给我，我们现在就走吧。",'
            '"choice_rewrites":[{"choice_id":"choice_letter","label":"把信封交给她后一起离开"}]}',
            '{"narration":"你已经把桌边的信封交到了她手里。",'
            '"dialogue":"信封收好啦，那我们出发吧。",'
            '"choice_rewrites":[{"choice_id":"choice_letter","label":"把信封交给她后一起离开"}]}',
        ]
    )
    call_types: list[str] = []

    class _FakeClient:
        """连续返回两份结构合法但抢跑同一待选动作的演绎。"""  # noqa: DOCSTRING_CJK

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def ainvoke(self, _messages):
            # 首版与 Repair 共享迭代器，确保失败后没有第三次无界模型调用。
            return type("Result", (), {"content": next(outputs)})()

    async def _create_fake_client(*_args, **_kwargs):
        """绕过真实网络并复现公开演绎与权威状态分裂。"""  # noqa: DOCSTRING_CJK
        return _FakeClient()

    def _record_call_type(value):
        """记录普通 Actor 后最多只进行一次 Repair。"""  # noqa: DOCSTRING_CJK
        call_types.append(value)

    monkeypatch.setattr(llm, "create_chat_llm_async", _create_fake_client)
    monkeypatch.setattr(llm, "set_call_type", _record_call_type)
    result = await llm.generate_turn_async(
        config_manager=_ModelConfig(),
        lanlan_name="星遥",
        story={"background": "两人仍在安静的室内。"},
        scene={"text": "一只未拆封的信封仍放在桌边。"},
        node={"summary": "信封尚未交到任何人手里。"},
        user_message="我们现在就走吧",
        progress_kind="roleplay_response",
        callback="",
        state={"scene_notes": []},
        recent_turns=[],
        choice_options=[
            {
                "choice_id": "choice_letter",
                "label": "把桌边的信封递给她",
                "author_label": "把桌边的信封递给她",
                "choice_mode": "action",
                "callback": "你拿起桌边的信封，将它递到她手边。",
            }
        ],
    )
    assert result == {
        "narration": "",
        "dialogue": "眼前的下一步还没有替你决定；先让我理清楚，再好好回应你喵。",
        "choice_rewrites": [],
    }
    assert call_types == ["theater_actor", "theater_repair"]


def test_uncommitted_choice_checker_allows_environment_result_and_imperative_action():
    """环境已经起火不等于玩家已完成 Choice，紧急命令也不能被完成态检查误杀。"""  # noqa: DOCSTRING_CJK
    parsed = {
        "narration": "主电池组冒出刺鼻白烟，火花在裂缝间噼啪作响。",
        "dialogue": "火苗窜出来了！别愣着，快用绝缘扳手切断汇流排，动作要快！",
    }
    options = [
        {
            "choice_id": "choice_isolate_main_bus",
            "label": "用绝缘扳手隔离冒烟的主电池汇流排",
            "author_label": "用绝缘扳手隔离冒烟的主电池汇流排",
            "choice_mode": "action",
            "callback": "你用绝缘扳手断开冒烟的主电池汇流排。",
        }
    ]

    assert (
        llm._claims_uncommitted_choice_result(
            parsed,
            user_message="主电池着火了",
            choice_options=options,
        )
        is False
    )


@pytest.mark.asyncio
async def test_roleplay_keeps_first_output_without_repair_for_soft_semantic_suspicion(
    monkeypatch,
):
    """低置信语义问题只影响文风，普通 Actor 不得为此调用 Repair。"""  # noqa: DOCSTRING_CJK
    outputs = iter(
        [
            '{"narration":"","dialogue":"你先决定我们去哪里吧？","choice_rewrites":[]}',
            '{"narration":"","dialogue":"那你告诉我第一站要去哪里？","choice_rewrites":[]}',
        ]
    )
    call_types: list[str] = []

    class _FakeClient:
        """连续返回可解析但仍把去向问题抛回玩家的软语义结果。"""  # noqa: DOCSTRING_CJK

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def ainvoke(self, _messages):
            return type("Result", (), {"content": next(outputs)})()

    async def _create_fake_client(*_args, **_kwargs):
        """隔离真实网络，只验证 Repair 后的软护栏降级策略。"""  # noqa: DOCSTRING_CJK
        return _FakeClient()

    def _record_call_type(value):
        """确认软语义问题仍只允许一次 Repair。"""  # noqa: DOCSTRING_CJK
        call_types.append(value)

    monkeypatch.setattr(llm, "create_chat_llm_async", _create_fake_client)
    monkeypatch.setattr(llm, "set_call_type", _record_call_type)
    result = await llm.generate_turn_async(
        config_manager=_ModelConfig(),
        lanlan_name="糖糖",
        story={"background": "两人站在公开测试区入口。"},
        scene={"text": "入口前有两条公开路线。"},
        node={"node_id": "node_route"},
        user_message="我们先去哪里？",
        progress_kind="roleplay_response",
        callback="",
        state={"scene_notes": []},
        recent_turns=[],
        choice_options=[],
    )

    assert call_types == ["theater_actor"]
    assert result == {
        "narration": "",
        "dialogue": "你先决定我们去哪里吧？",
        "choice_rewrites": [],
    }


def test_turn_parser_discards_choice_rewrites_without_authority():
    """模型即使返回合法 Choice ID，也不能让改写进入可提交演绎结果。"""  # noqa: DOCSTRING_CJK
    parsed = llm._parse_output(
        '{"narration":"","dialogue":"我们可以继续聊。","choice_rewrites":'
        '[{"choice_id":"choice_letter","label":"带着信封离开"}]}',
        progress_kind="roleplay_response",
    )
    assert parsed is not None
    assert parsed["choice_rewrites"] == []


def test_roleplay_guard_allows_player_action_explicitly_present_in_current_input():
    """玩家本轮确实实施的动作可以被普通回应承认，护栏不能把所有第二人称旁白一概拦截。"""  # noqa: DOCSTRING_CJK
    reason = llm._performance_repair_reason(
        {
            "narration": "她接过你递来的信封，放在桌角。",
            "dialogue": "谢谢，我会收好它。",
            "choice_rewrites": [],
        },
        progress_kind="roleplay_response",
        user_message="我把桌边的信封递给她",
        node={},
        character_profile="",
        choice_options=[
            {
                "choice_id": "choice_letter",
                "label": "把桌边的信封递给她",
                "author_label": "把桌边的信封递给她",
                "choice_mode": "action",
                "callback": "你拿起桌边的信封，将它递到她手边。",
            }
        ],
    )
    assert reason == ""


@pytest.mark.asyncio
async def test_graph_progress_repairs_authored_forbidden_topic_phrase(monkeypatch):
    """作者声明暂时禁用的话题词被模型擅自补入时，必须纠错后再展示。"""  # noqa: DOCSTRING_CJK
    outputs = [
        '{"narration":"你收下测试牌。","dialogue":"那份内部验证清单就留到下一步再说。","choice_rewrites":[]}',
        '{"narration":"你收下测试牌。","dialogue":"当前只确认公开步骤，到了验证区再核对记录。","choice_rewrites":[]}',
    ]
    calls = 0

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def ainvoke(self, _messages):
            nonlocal calls
            content = outputs[calls]
            calls += 1
            return type("Result", (), {"content": content})()

    async def _create_fake_client(*_args, **_kwargs):
        return _FakeClient()

    monkeypatch.setattr(llm, "create_chat_llm_async", _create_fake_client)
    result = await llm.generate_turn_async(
        config_manager=_ModelConfig(),
        lanlan_name="希尔",
        story={"background": "验证开始前的测试室"},
        scene={"text": "两枚测试牌放在透明盒里。"},
        node={
            "summary": "玩家接受共同验证。",
            "scripted_dialogue": "当前只确认公开步骤，到了验证区再核对记录。",
            "runtime_generation_guide": {
                "forbidden_dialogue_phrases": ["内部验证清单"]
            },
        },
        user_message="开始验证",
        progress_kind="graph_progress",
        callback="你收下测试牌。",
        state={"scene_notes": []},
        recent_turns=[],
    )

    assert calls == 2
    assert "内部验证清单" not in result["dialogue"]
    assert "公开步骤" in result["dialogue"]


def test_authored_performance_requires_declared_self_name_when_speaking_about_self():
    """人格明确声明自称时，作者演出不得退化成无人格的第一人称近义改写。"""  # noqa: DOCSTRING_CJK
    reason = llm._performance_repair_reason(
        {"dialogue": "我本来想把它重缝。"},
        progress_kind="graph_progress",
        user_message="接住挂坠",
        node={"scripted_dialogue": "我本来想把它重缝。"},
        character_profile="自称: 本小姐\n核心特质: 傲娇嘴硬",
    )
    assert reason == "persona_self_name_missing"


def test_roleplay_rejects_mirroring_players_location_question():
    """玩家问第一站时，猫娘不得只把同一个去向问题反问回来。"""  # noqa: DOCSTRING_CJK
    bad_reason = llm._performance_repair_reason(
        {"narration": "", "dialogue": "主人先告诉糖糖，我们第一站要去哪里呀？"},
        progress_kind="roleplay_response",
        user_message="我们先去哪里？",
        node={},
        character_profile="",
    )
    good_reason = llm._performance_repair_reason(
        {"narration": "", "dialogue": "先去公开验证区吧，那里离入口最近。"},
        progress_kind="roleplay_response",
        user_message="我们先去哪里？",
        node={},
        character_profile="",
    )
    assert bad_reason == "current_question_mirrored"
    assert good_reason == ""


def test_roleplay_rejects_unintroduced_named_destination():
    """回答去向时不得临场发明公开上下文里不存在的命名摊位。"""  # noqa: DOCSTRING_CJK
    bad_reason = llm._performance_repair_reason(
        {"narration": "", "dialogue": "先去入口旁的「星愿风铃」摊吧。"},
        progress_kind="roleplay_response",
        user_message="我们先去哪里？",
        node={},
        character_profile="",
        grounding_text="已经公开的最近目的地是测试区入口。",
    )
    good_reason = llm._performance_repair_reason(
        {"narration": "", "dialogue": "先到测试区入口吧，之后再看公开的验证路线。"},
        progress_kind="roleplay_response",
        user_message="我们先去哪里？",
        node={},
        character_profile="",
        grounding_text="已经公开的最近目的地是测试区入口。",
    )
    assert bad_reason == "ungrounded_named_destination"
    assert good_reason == ""


def test_story_output_guardrails_cover_narration_and_dialogue():
    """关系边界不能只检查对白，旁白中的越界接触也必须拦截。"""  # noqa: DOCSTRING_CJK
    reason = llm._performance_repair_reason(
        {"narration": "糖糖顺势挽住你的手臂。", "dialogue": "我们出发吧喵。"},
        progress_kind="roleplay_response",
        user_message="出发吧。",
        node={},
        character_profile="",
        story={
            "runtime_guardrails": {
                "conditional_output_guards": [
                    {
                        "until_fact": {
                            "subject": "player",
                            "predicate": "chooses",
                            "object": "relationship",
                        },
                        "forbidden_phrases": ["挽住你的手臂"],
                    }
                ]
            }
        },
        state={"narrative_facts": []},
    )
    assert reason == "forbidden_output_phrase_used"

    allowed_after_confirmation = llm._performance_repair_reason(
        {"narration": "糖糖征得同意后挽住你的手臂。", "dialogue": "这样可以吗？"},
        progress_kind="roleplay_response",
        user_message="可以。",
        node={},
        character_profile="",
        story={
            "runtime_guardrails": {
                "conditional_output_guards": [
                    {
                        "until_fact": {
                            "subject": "player",
                            "predicate": "chooses",
                            "object": "relationship",
                        },
                        "forbidden_phrases": ["挽住你的手臂"],
                    }
                ]
            }
        },
        state={
            "narrative_facts": [
                {"subject": "player", "predicate": "chooses", "object": "relationship"}
            ]
        },
    )
    assert allowed_after_confirmation == ""


def test_story_silent_rules_cannot_be_explained_in_dialogue():
    """内部规则只能改变行为，猫娘不能把它们组织成免责声明说给玩家。"""  # noqa: DOCSTRING_CJK
    story = {
        "runtime_guardrails": {
            "forbidden_output_patterns": [
                "中途.{0,16}(?:停|换).{0,20}(?:商量|决定)",
                "测试牌.{0,16}(?:不会|不能|不).{0,16}(?:安排|决定)",
            ]
        }
    }
    rule_dump = llm._performance_repair_reason(
        {"narration": "", "dialogue": "中途想停或者换地方，我们都可以一起商量喵。"},
        progress_kind="graph_progress",
        user_message="出发",
        node={},
        character_profile="",
        story=story,
    )
    natural_reply = llm._performance_repair_reason(
        {"narration": "", "dialogue": "那就开始吧，先去测试区入口。"},
        progress_kind="graph_progress",
        user_message="出发",
        node={},
        character_profile="",
        story=story,
    )
    assert rule_dump == "internal_rule_exposed"
    assert natural_reply == ""


@pytest.mark.asyncio
async def test_choice_rewrite_failure_keeps_first_answer_without_retry(monkeypatch):
    """推荐项不合规时直接保留首版回答和作者按钮，不让第二次调用改变正文。"""  # noqa: DOCSTRING_CJK
    outputs = [
        (
            '{"narration":"","dialogue":"先到测试区入口吧，到了那里再看验证路线。",'
            '"choice_rewrites":['
            '{"choice_id":"choice_route","label":"抵达入口后，把愿意公开的路线面递给她看"}]}'
        ),
        (
            '{"narration":"","dialogue":"主人先告诉糖糖，我们第一站要去哪里呀？",'
            '"choice_rewrites":['
            '{"choice_id":"choice_route","label":"到了入口，把想分享的路线交给她"}]}'
        ),
    ]
    calls = 0

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def ainvoke(self, _messages):
            nonlocal calls
            content = outputs[calls]
            calls += 1
            return type("Result", (), {"content": content})()

    async def _create_fake_client(*_args, **_kwargs):
        return _FakeClient()

    monkeypatch.setattr(llm, "create_chat_llm_async", _create_fake_client)
    result = await llm.generate_turn_async(
        config_manager=_ModelConfig(),
        lanlan_name="糖糖",
        story={"background": "两人正前往测试区入口。"},
        scene={"text": "测试区入口就在前方。"},
        node={"node_id": "node_depart"},
        user_message="我们先去哪里？",
        progress_kind="roleplay_response",
        callback="",
        state={"scene_notes": []},
        recent_turns=[],
        choice_options=[
            {
                "choice_id": "choice_route",
                "label": "抵达入口后，把愿意公开的路线面递给她看",
                "author_label": "抵达入口后，把愿意公开的路线面递给她看",
                "choice_mode": "action",
            }
        ],
    )

    assert calls == 1
    assert result["dialogue"] == "先到测试区入口吧，到了那里再看验证路线。"
    assert result["choice_rewrites"] == []


@pytest.mark.parametrize(
    "performed_dialogue",
    [
        "要是中途本小姐想停或者换地方，我们就当场商量，不许有意见喵。",
        "临时起意的事由我们共同决定，不过得先问过本小姐才行。",
    ],
)
def test_author_consent_boundary_rejects_real_single_party_approval_phrases(
    performed_dialogue,
):
    """真实演绎出现的单方否决或批准句式不能伪装成共同决定。"""  # noqa: DOCSTRING_CJK
    assert (
        llm._violates_author_consent_boundary(
            "中途想停或改路线时一起商量，由我们两个决定。",
            performed_dialogue,
            self_name="本小姐",
        )
        is True
    )


@pytest.mark.asyncio
async def test_graph_progress_uses_author_callback_for_narration(monkeypatch):
    """模型不得把“等待同意”擅自写成已经按下播放键。"""  # noqa: DOCSTRING_CJK

    class _FakeClient:
        """返回会抢跑下一节点动作的可控模型结果。"""  # noqa: DOCSTRING_CJK

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def ainvoke(self, _messages):
            return type(
                "Result",
                (),
                {
                    "content": '{"narration":"她直接按下了播放键。","dialogue":"等我准备好再说喵。","choice_rewrites":[]}'
                },
            )()

    async def _create_fake_client(*_args, **_kwargs):
        """绕过真实网络并返回抢跑剧情的客户端。"""  # noqa: DOCSTRING_CJK
        return _FakeClient()

    monkeypatch.setattr(llm, "create_chat_llm_async", _create_fake_client)
    result = await llm.generate_turn_async(
        config_manager=_ModelConfig(),
        lanlan_name="霜瞳",
        story={"background": "旧档案室"},
        scene={"text": "磁带机还没有启动。"},
        node={"scripted_dialogue": "我还没准备好喵。", "summary": "玩家等待猫娘同意。"},
        user_message="把手停在播放键旁，等她亲自决定",
        progress_kind="graph_progress",
        callback="你没有碰播放键，只把手收回桌边，等猫娘自己作出决定。",
        state={"scene_notes": []},
        recent_turns=[],
    )
    assert result["narration"] == "你没有碰播放键，只把手收回桌边，等猫娘自己作出决定。"
    assert "按下" not in result["narration"]


















def test_near_duplicate_dialogue_ignores_punctuation_and_final_neko_particle():
    """只删除句尾“喵”的上一句复述仍应被识别，避免机械连续对白。"""  # noqa: DOCSTRING_CJK
    recent = [
        {
            "role": "assistant",
            "text": "你还记得……算了，记得也不代表什么。今晚我只是来交设备的喵。",
        }
    ]
    assert (
        llm._repeats_recent_dialogue(
            "你还记得，算了，记得也不代表什么。今晚我只是来交设备的。", recent
        )
        is True
    )
    assert llm._repeats_recent_dialogue("我其实还没想好该怎么面对你。", recent) is False


def test_attitude_response_must_not_reanswer_previous_question():
    """玩家评价上一回答时，Actor 不能再次解释上一轮已经回答的问题。"""  # noqa: DOCSTRING_CJK
    recent = [
        {"role": "user", "text": "甜点的制作灵感是什么"},
        {
            "role": "assistant",
            "text": "灵感来自你品尝时放松下来的那一瞬间，我想把那份温柔藏进奶油里。",
        },
    ]
    assert (
        llm._reanswers_previous_question(
            "灵感就是想让尝到的人感受到被珍视的温暖呀，能得到这样的评价我很开心。",
            current_user_message="确实很有层次",
            recent_turns=recent,
            response_focus={
                "focus_type": "attitude",
                "evidence_excerpt": "确实很有层次",
                "requires_state_change": False,
            },
        )
        is True
    )
    assert (
        llm._performance_repair_reason(
            {
                "narration": "",
                "dialogue": "灵感就是想让尝到的人感受到被珍视的温暖呀。",
            },
            progress_kind="roleplay_response",
            user_message="确实很有层次",
            node={},
            character_profile="",
            recent_turns=recent,
            response_focus={
                "focus_type": "attitude",
                "evidence_excerpt": "确实很有层次",
                "requires_state_change": False,
            },
        )
        == "previous_question_reanswered"
    )
    assert (
        llm._reanswers_previous_question(
            "能被你注意到这些层次，我真的很开心。",
            current_user_message="确实很有层次",
            recent_turns=recent,
            response_focus={
                "focus_type": "attitude",
                "evidence_excerpt": "确实很有层次",
                "requires_state_change": False,
            },
        )
        is False
    )


def test_model_choice_rewrites_never_receive_authority():
    """即使模型命中当前稳定 ID，所有静态 Choice 改写也必须被丢弃。"""  # noqa: DOCSTRING_CJK
    result = llm._parse_output(
        '{"narration":"","dialogue":"我一直留着它喵。","choice_rewrites":['
        '{"choice_id":"choice_keep","label":"收好照片，回应她刚才的坦白"},'
        '{"choice_id":"choice_keep","label":"重复覆盖"},'
        '{"choice_id":"choice_unknown","label":"跳到未知结局"},'
        '{"choice_id":"choice_wait","label":"查看 node_id"}]}',
        progress_kind="roleplay_response",
    )
    assert result == {
        "narration": "",
        "dialogue": "我一直留着它喵。",
        "choice_rewrites": [],
    }


















def test_character_profile_only_reads_current_configured_catgirl(tmp_path):
    """人格摘要只能读取当前已配置猫娘，路径片段和其他猫娘都必须被拒绝。"""  # noqa: DOCSTRING_CJK
    safe_path = tmp_path / "memory" / "安全猫娘" / "persona.json"
    safe_path.parent.mkdir(parents=True)
    safe_path.write_text(
        json.dumps({"neko": {"facts": [{"text": "喜欢雨天散步"}]}}),
        encoding="utf-8",
    )
    escaped_path = tmp_path / "private" / "persona.json"
    escaped_path.parent.mkdir(parents=True)
    escaped_path.write_text(
        json.dumps({"neko": {"facts": [{"text": "不应泄露的秘密"}]}}),
        encoding="utf-8",
    )
    config = _CharacterConfig(tmp_path)

    assert llm._load_character_profile(config, "安全猫娘") == "喜欢雨天散步"
    assert llm._load_character_profile(config, "../private") == ""
    assert llm._load_character_profile(config, "其他猫娘") == ""








def test_internal_identifier_guard_includes_machine_fact_values():
    """模型可见的事实三元组值若是机器 token，也不能被原样说给玩家。"""  # noqa: DOCSTRING_CJK
    identifiers = llm._private_runtime_identifiers(
        {
            "narrative_facts": [
                {
                    "subject": "player",
                    "predicate": "follows",
                    "object": "seven_item_date_list",
                }
            ],
            "catalog_items": [
                {
                    "content_id": "content_hot_cocoa",
                    "fact_object": "hot_cocoa_machine_value",
                }
            ],
        }
    )

    assert "seven_item_date_list" in identifiers
    assert "content_hot_cocoa" in identifiers
    assert "hot_cocoa_machine_value" in identifiers
    assert (
        llm._exposes_internal_runtime_detail(
            "我们继续 seven_item_date_list 的安排。",
            identifiers,
        )
        is True
    )




def test_historical_long_user_turn_is_omitted_instead_of_truncated():
    """历史玩家原话超过预算时整条退出模型上下文，不能只留下正向前缀。"""  # noqa: DOCSTRING_CJK
    long_message = "继续执行当前行动，" * 80 + "但是最后决定不要执行"

    result = llm._recent_public_turns(
        [
            {"role": "user", "text": long_message},
            {"role": "assistant", "text": "我会先停下来确认。", "narration": ""},
        ]
    )

    assert result == [
        {"role": "assistant", "dialogue": "我会先停下来确认。", "narration": ""}
    ]


@pytest.mark.parametrize(
    ("dialogue", "narration"),
    [
        (
            "我们继续按原计划前进。" * 80 + "不过最后不要执行这个计划。",
            "她看向前方。",
        ),
        (
            "我先确认一下喵。",
            "她伸手准备打开舱门。" * 80 + "但她最后没有打开舱门。",
        ),
    ],
)
def test_historical_long_assistant_turn_is_omitted_instead_of_truncated(
    dialogue,
    narration,
):
    """猫娘对白或旁白句尾无法完整保留时，整回合退出上下文而不是留下相反前缀。"""  # noqa: DOCSTRING_CJK
    result = llm._recent_public_turns(
        [
            {"role": "assistant", "text": dialogue, "narration": narration},
            {"role": "assistant", "text": "我会完整确认后再行动。", "narration": ""},
        ]
    )

    assert result == [
        {
            "role": "assistant",
            "dialogue": "我会完整确认后再行动。",
            "narration": "",
        }
    ]


def test_simplified_prompts_keep_free_talk_inside_authored_world():
    """自由交流可以自然回应，但不得临时生成剧情结构或脱离世界设定。"""  # noqa: DOCSTRING_CJK
    assert "只能停留在当前故事背景" in THEATER_TURN_SYSTEM_PROMPT
    assert "不得创建或暗示新节点" in THEATER_TURN_SYSTEM_PROMPT
    assert "必须用第二人称“你”" in THEATER_TURN_SYSTEM_PROMPT
    assert "authored_choice" in THEATER_ROUTE_SYSTEM_PROMPT
    assert "authored_intent" in THEATER_ROUTE_SYSTEM_PROMPT
    assert "stay" in THEATER_ROUTE_SYSTEM_PROMPT


def test_route_contract_accepts_only_current_authored_targets():
    """Router 只能选择当前剧本提供的 Choice 或隐藏意图。"""  # noqa: DOCSTRING_CJK
    choice = llm._parse_route_output(
        '{"route_kind":"authored_choice","matched_choice_id":"choice_now",'
        '"authored_intent_id":"","response_focus":{}}',
        allowed_choice_ids={"choice_now"},
        allowed_intent_ids={"intent_now"},
        user_message="继续",
    )
    assert choice["matched_choice_id"] == "choice_now"

    latent = llm._parse_route_output(
        '{"route_kind":"authored_intent","matched_choice_id":"",'
        '"authored_intent_id":"intent_now","response_focus":{}}',
        allowed_choice_ids={"choice_now"},
        allowed_intent_ids={"intent_now"},
        user_message="谈谈另一件事",
    )
    assert latent["authored_intent_id"] == "intent_now"
    assert llm._parse_route_output(
        '{"route_kind":"authored_choice","matched_choice_id":"unknown",'
        '"authored_intent_id":"","response_focus":{}}',
        allowed_choice_ids={"choice_now"},
        allowed_intent_ids={"intent_now"},
        user_message="继续",
    ) is None


@pytest.mark.asyncio
async def test_route_without_model_stays_in_current_scene():
    """模型未配置时，自由交流不应推进任何作者节点。"""  # noqa: DOCSTRING_CJK
    result = await llm.route_free_input_async(
        config_manager=None,
        story={"background": "室内故事"},
        scene={"title": "当前房间", "text": "两位主角正在交谈。"},
        user_message="我们再聊一会儿",
        state={"scene_notes": []},
        recent_turns=[],
        choice_options=[],
        latent_transitions=[],
    )
    assert result["route_kind"] == "stay"
    assert result["matched_choice_id"] == ""
    assert result["authored_intent_id"] == ""
