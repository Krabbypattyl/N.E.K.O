"""覆盖自由模式沙盒合同，并确认它不会写入剧本模式 Session。"""  # noqa: DOCSTRING_CJK

import asyncio
from copy import deepcopy

import pytest

# 测试同时覆盖自由 Runtime 和 Free Seed 合同，确保作者图不会进入模型输入。
from services.theater import (
    free_role_card,
    free_runtime,
    free_seed,
    llm,
    session_store,
    story_loader,
)
from config.prompts.prompts_theater import (
    build_theater_free_turn_messages,
    build_theater_free_turn_prompts,
)
from services.theater.llm_response_contracts import _parse_free_output


THEATER_TEST_STORY_ID = "free_runtime_source"


def _free_source_story() -> dict:
    """自由 Runtime 测试直接构造最小来源，不再保留旧 Story 配置样本。"""

    return {
        "id": THEATER_TEST_STORY_ID,
        "story_revision": "free-source-1",
        "title": "自由模式测试来源",
        "theme": "确认自由 Session 隔离",
        "fact_lifecycle_migration_status": "complete",
        "scenario_card": {
            "player_role": "测试玩家",
            "catgirl_role": "测试猫娘",
            "primary_goal": "开始自由交流",
        },
        "restrictions": [],
        "runtime_guardrails": {},
        "seed": {"forbidden_assumptions": []},
        "initial_scene_id": "free_opening",
        "scenes": [
            {
                "id": "free_opening",
                "title": "测试房间",
                "text": "窗边的灯保持稳定亮起。",
            }
        ],
        "narrative_nodes": [],
    }


def _free_result() -> dict:
    """构造 RP-Hub 风格的纯文本模型替身结果。"""  # noqa: DOCSTRING_CJK
    return {"text": "　窗外的雨声没有停，猫娘把视线落回你身上。\n\n　『我听见了。』"}


def test_free_output_contract_rejects_runtime_identifiers():
    """自由模式接受纯文本，并拒绝旧 JSON 外壳或内部运行时字段。"""  # noqa: DOCSTRING_CJK
    assert _parse_free_output(_free_result()["text"])["text"] == _free_result()["text"]
    assert _parse_free_output('{"dialogue":"旧 JSON"}') is None
    assert _parse_free_output("　正文里不应出现 node_id 这样的内部字段") is None


def test_free_prompt_requires_plain_roleplay_text():
    """自由模式提示要求直接输出 RP-Hub 正文，不再要求临时状态 JSON。"""  # noqa: DOCSTRING_CJK
    system_prompt, user_prompt = build_theater_free_turn_prompts(
        lanlan_name="测试猫娘",
        story={
            "title": "测试",
            "background": "不应作为自由模式固定背景发送",
            "scenario_card": {
                "player_role": "访客",
                "catgirl_role": "邻居",
                "primary_goal": "不应进入自由演绎提示",
            },
        },
        scene={"title": "室内", "text": "窗外有雨声。"},
        user_message="",
        recent_turns=[],
        character_profile="自然说话",
        is_opening=True,
    )
    assert "连续故事正文" in system_prompt
    assert "不要输出 JSON" in system_prompt
    assert "temporary_state" not in system_prompt
    assert "不要输出 JSON 外壳或结构化字段" in user_prompt


def test_free_messages_do_not_inject_fixed_roleplay_prelude():
    """自由消息只保留当前上下文，不注入固定的自问自答预热内容。"""
    messages = build_theater_free_turn_messages(
        lanlan_name="测试猫娘",
        story={"title": "测试故事", "theme": "自由探索"},
        scene={"title": "门廊", "text": "雨水沿着屋檐落下。"},
        user_message="我向她挥手。",
        recent_turns=[],
        character_profile="温柔自然。",
        player_address="哥哥",
        role_card={
            "name": "测试猫娘",
            "description": "当前猫娘。",
            "first_mes": "她回头看向你。",
            "player_address": "哥哥",
            "player_role": "哥哥",
        },
    )
    contents = "\n".join(str(item.get("content") or "") for item in messages)
    # 预热消息属于固定 Prompt 成本，不应混入自由模式的真实对话历史。
    assert "<difficulties>" not in contents
    assert "[RP-Hub READY]" not in contents
    assert messages[-1] == {"role": "user", "content": "我向她挥手。"}


def test_free_prompt_uses_roleplay_seed_instead_of_author_goal():
    """自由 Actor 使用 RP-Hub 风格角色卡层次，不接收剧本目标或当前场景锁定语义。"""  # noqa: DOCSTRING_CJK
    system_prompt, user_prompt = build_theater_free_turn_prompts(
        lanlan_name="测试猫娘",
        story={
            "title": "测试故事",
            "theme": "自由探索",
            "scenario_card": {
                "player_role": "访客",
                "catgirl_role": "邻居",
                "primary_goal": "必须完成的作者目标",
            },
        },
        scene={"title": "门廊", "text": "雨水沿着屋檐落下。"},
        user_message="我转身走向街道，不再停留在门廊。",
        recent_turns=[],
        character_profile="有自己的判断，但愿意继续交流",
        is_opening=True,
    )

    assert "必须完成的作者目标" not in user_prompt
    assert "本剧目标" not in user_prompt
    assert "[User Info]" in user_prompt
    assert "[Character]" in user_prompt
    assert "[Story Seed]" in user_prompt
    assert "[Scenario]" in user_prompt
    assert "[Chat History]" in user_prompt
    assert "[Current User Message]" in user_prompt
    assert "Name: 测试猫娘" in user_prompt
    assert "Personality:\n有自己的判断，但愿意继续交流" in user_prompt
    assert "自由模式是独立沙盒" in system_prompt
    assert "玩家明确写出的动作、对白和决定视为本轮已经发生的输入" in system_prompt
    assert "允许自然离开或改变地点" in system_prompt
    assert "厌恶：下雨天不能出门玩" in system_prompt
    assert "不表示她在下雨天不能出门" in system_prompt
    assert "不要输出 JSON 外壳" in user_prompt


def test_free_prompt_does_not_replay_first_scene_after_opening():
    """自由模式后续回合不重复发送第一幕 Scene，避免把起点误当成持续舞台。"""  # noqa: DOCSTRING_CJK
    _system_prompt, user_prompt = build_theater_free_turn_prompts(
        lanlan_name="测试猫娘",
        story={
            "title": "测试故事",
            "theme": "自由探索",
            "scenario_card": {
                "player_role": "访客",
                "catgirl_role": "邻居",
            },
        },
        scene={"title": "门廊", "text": "雨水沿着屋檐落下。"},
        user_message="我们已经走到街角，继续往前。",
        recent_turns=[
            {
                "role": "assistant",
                "text": "街角的招牌在风里轻轻晃动。",
            }
        ],
        character_profile="有自己的判断，但愿意继续交流",
        is_opening=False,
    )

    assert "第一幕背景" not in user_prompt
    assert "门廊" not in user_prompt
    assert "雨水沿着屋檐落下" not in user_prompt
    assert "街角的招牌在风里轻轻晃动" in user_prompt


def test_free_role_card_can_override_current_session_seed_without_author_graph():
    """临时角色卡只替换自由开场和身份，不把作者剧情图带入模型。"""  # noqa: DOCSTRING_CJK
    role_card = free_role_card.validate_role_card(
        {
            "schema_version": free_role_card.FREE_ROLE_CARD_SCHEMA_VERSION,
            "name": "小葵",
            "description": "当前猫娘小葵，与哥哥在架空江湖中自由相遇。",
            "personality": "保留当前猫娘人格摘要。",
            "first_mes": "小葵在山路尽头回头看向哥哥。",
            "scenario": "架空江湖，自由游历",
            "player_address": "哥哥",
            "player_role": "哥哥",
            "story_title": "行路谣",
            "scenario_title": "山路初遇",
            "world_info": ["大晟朝是架空王朝。"],
        },
        expected_name="小葵",
    )
    seed = free_role_card.apply_role_card_to_seed(
        {
            "title": "旧剧本",
            "theme": "旧主题",
            "scenario_card": {
                "player_role": "旧玩家",
                "catgirl_role": "旧猫娘",
                "primary_goal": "旧目标",
            },
            "opening_scene": {
                "id": "old_scene",
                "title": "旧场景",
                "text": "旧开场",
            },
            "narrative_nodes": [{"node_id": "private"}],
            "edges": [{"edge_id": "private"}],
        },
        role_card,
    )
    assert seed["title"] == "行路谣"
    assert seed["scenario_card"]["player_role"] == "哥哥"
    assert seed["scenario_card"]["catgirl_role"].startswith("当前猫娘")
    assert seed["scenario_card"]["primary_goal"] == ""
    assert seed["opening_scene"]["text"] == "小葵在山路尽头回头看向哥哥。"
    assert "narrative_nodes" in seed
    assert "edges" in seed


def test_free_response_projects_temporary_role_card_for_theater_header():
    """自由 Session 的顶部身份卡使用临时角色卡，不回显来源剧本关系。"""  # noqa: DOCSTRING_CJK
    response = free_runtime._public_response(
        session={
            "session_id": "free_card_session",
            "story_id": "story_source",
            "lanlan_name": "小葵",
            "state_revision": 0,
            "role_card": {
                "name": "小葵",
                "description": "当前猫娘，与哥哥同行。",
                "story_title": "行路谣",
                "scenario_title": "嵩阳书院外的清晨",
                "scenario": "架空江湖，自由游历。",
                "player_address": "哥哥",
                "player_role": "哥哥",
                "world_info": ["不应返回前端"],
            },
        },
        performance={"text": "小葵看向哥哥。"},
        ending={"should_end_session": False},
        can_resume=True,
    )
    assert response["free_role_card"] == {
        "name": "小葵",
        "description": "当前猫娘，与哥哥同行。",
        "story_title": "行路谣",
        "scenario_title": "嵩阳书院外的清晨",
        "scenario": "架空江湖，自由游历。",
        "player_address": "哥哥",
        "player_role": "哥哥",
    }
    assert response["free_text"] == "小葵看向哥哥。"
    assert "narration" not in response
    assert "dialogue" not in response
    assert "closing_narration" not in response


def test_free_history_ignores_removed_structured_assistant_fields():
    """自由历史只投影 free_text，不再恢复旧的旁白、对白和收束字段。"""

    history = free_runtime._public_history(
        {
            "turns": [
                {
                    "role": "assistant",
                    "text": "旧对白",
                    "narration": "旧旁白",
                    "closing_narration": "旧收束",
                },
                {"role": "assistant", "free_text": "新的 RP-Hub 正文。"},
            ]
        },
        "小葵",
    )

    assert history == [{"role": "narrator", "text": "新的 RP-Hub 正文。"}]


def test_free_role_card_session_restores_source_scene():
    """临时角色卡开场使用新 ID 时，续写仍从来源 Story 的 Scene 恢复。"""  # noqa: DOCSTRING_CJK
    source_scene = {"id": "source_scene", "title": "来源场景", "text": "来源内容"}
    restored = free_runtime._scene_for_session(
        {"initial_scene_id": "source_scene", "scenes": [source_scene]},
        {"source_scene_id": "source_scene", "scene_id": "free_role_card_opening"},
    )
    assert restored == source_scene


def test_free_prompt_uses_temporary_role_card_context():
    """自由提示能看到临时角色卡的人称和世界资料，后续仍不读取作者图。"""  # noqa: DOCSTRING_CJK
    _system_prompt, user_prompt = build_theater_free_turn_prompts(
        lanlan_name="小葵",
        story={
            "title": "行路谣",
            "theme": "架空江湖，自由游历",
            "scenario_card": {
                "player_role": "哥哥",
                "catgirl_role": "当前猫娘小葵",
            },
        },
        scene={"title": "山路初遇", "text": "小葵回头看向哥哥。"},
        user_message="我们沿着山路继续走。",
        recent_turns=[],
        character_profile="当前猫娘人格摘要",
        is_opening=True,
        role_card={
            "player_address": "哥哥",
            "player_role": "哥哥",
            "description": "当前猫娘小葵，与哥哥自然相处。",
            "world_info": ["大晟朝是架空王朝。"],
        },
    )
    assert "Name: 哥哥" in user_prompt
    assert "Description: 当前猫娘小葵" in user_prompt
    assert "[Character Card World Notes]" in user_prompt
    assert "大晟朝是架空王朝" in user_prompt
    assert "narrative_nodes" not in user_prompt
    assert "edges" not in user_prompt


def test_free_role_card_binds_current_catgirl_and_native_rp_hub_messages():
    """角色卡只提供世界素材，主角、人格和玩家称呼必须改为当前猫娘。"""
    bound = free_role_card.bind_role_card_to_current_catgirl(
        {
            "schema_version": free_role_card.FREE_ROLE_CARD_SCHEMA_VERSION,
            "name": "顾映荷",
            "description": "原角色简介",
            "personality": "原角色性格",
            "first_mes": "顾映荷看向三师兄。",
            "scenario": "架空江湖",
            "player_address": "三师兄",
            "player_role": "三师兄",
            "world_info": ["顾映荷所在的江湖。"],
        },
        expected_name="小葵",
        character_profile="甜美治愈，喜欢黏着哥哥。",
        player_address="哥哥",
    )
    assert bound["name"] == "小葵"
    assert bound["description"] == "小葵是当前猫娘，也是本次自由演绎的主角。"
    assert bound["personality"] == "甜美治愈，喜欢黏着哥哥。"
    assert bound["player_address"] == "哥哥"
    assert bound["player_role"] == "哥哥"
    assert "顾映荷" not in bound["first_mes"]
    assert "三师兄" not in bound["first_mes"]

    messages = build_theater_free_turn_messages(
        lanlan_name="小葵",
        story={"title": "行路谣", "theme": "架空江湖"},
        scene={"title": "山路", "text": "山风穿过竹林。"},
        user_message="你好",
        recent_turns=[
            {"role": "assistant", "free_text": "　小葵回头看你。"},
            {"role": "user", "text": "我向她挥手。"},
        ],
        character_profile="甜美治愈，喜欢黏着哥哥。",
        player_address="哥哥",
        is_opening=True,
        role_card=bound,
    )
    assert messages[0]["role"] == "system"
    assert [item["role"] for item in messages].count("assistant") >= 2
    assert any(item["content"] == bound["first_mes"] for item in messages)
    assert messages[-1] == {"role": "user", "content": "你好"}
    assert "顾映荷" not in "\n".join(item["content"] for item in messages)
    assert "三师兄" not in "\n".join(item["content"] for item in messages)


def test_free_role_card_rejects_oversized_bound_field():
    """临时角色卡字段超过合同上限时必须拒绝，不截断后继续进入 Session。"""  # noqa: DOCSTRING_CJK
    with pytest.raises(free_role_card.FreeRoleCardContractError, match="过长"):
        free_role_card.validate_role_card(
            {
                "schema_version": free_role_card.FREE_ROLE_CARD_SCHEMA_VERSION,
                "name": "小葵",
                "description": "x" * 24001,
                "personality": "当前猫娘人格",
                "first_mes": "小葵看向哥哥。",
                "scenario": "自由场景",
                "player_address": "哥哥",
                "player_role": "哥哥",
            },
            expected_name="小葵",
        )


def test_free_seed_only_keeps_opening_context():
    """自由种子只保留开场上下文，不携带作者剧情图或正式账本。"""
    story = {
        "id": "story_free_seed",
        "story_revision": "v1",
        "title": "雨夜访客",
        "theme": "重新建立信任",
        "scenario_card": {
            "player_role": "访客",
            "catgirl_role": "等待消息的邻居",
            "primary_goal": "完成第一次交流",
        },
        "restrictions": ["不要替玩家确认未说出口的决定"],
        "runtime_guardrails": {"allow_new_named_places": False},
        "seed": {"forbidden_assumptions": ["不要假设玩家携带武器"]},
        "narrative_nodes": [{"node_id": "private_node"}],
        "edges": [{"edge_id": "private_edge"}],
        "events": [{"event_id": "private_event"}],
    }
    scene = {"id": "scene_setup", "title": "门廊", "text": "雨水沿着屋檐落下。"}

    seed = free_seed.build_free_seed(story, scene)

    assert seed["schema_version"] == free_seed.FREE_SEED_SCHEMA_VERSION
    assert seed["opening_scene"] == scene
    assert seed["scenario_card"]["primary_goal"] == "完成第一次交流"
    assert "narrative_nodes" not in seed
    assert "edges" not in seed
    assert "events" not in seed


def test_free_seed_rejects_author_graph_fields():
    """自由种子合同拒绝把作者图字段伪装成自由模式输入。"""
    seed = {
        "schema_version": free_seed.FREE_SEED_SCHEMA_VERSION,
        "source_story_id": "story_free_seed",
        "source_story_revision": "v1",
        "title": "测试故事",
        "theme": "测试主题",
        "scenario_card": {
            "player_role": "玩家",
            "catgirl_role": "猫娘",
            "primary_goal": "开始交流",
        },
        "opening_scene": {"id": "setup", "title": "房间", "text": "很安静。"},
        "restrictions": [],
        "runtime_guardrails": {},
        "seed": {"forbidden_assumptions": []},
        "edges": [],
    }

    with pytest.raises(free_seed.FreeSeedContractError):
        free_seed.validate_free_seed(seed)


@pytest.mark.asyncio
async def test_free_session_is_isolated_and_idempotent(monkeypatch, tmp_path):
    """自由回合只写入 free 子目录，并在重复提交或 revision 冲突时保持幂等。"""  # noqa: DOCSTRING_CJK
    story = _free_source_story()

    async def _load_story(_story_id=None, **_kwargs):
        return deepcopy(story)

    async def _load_story_exact(_story_id, **_kwargs):
        return deepcopy(story)

    monkeypatch.setattr(free_runtime.story_loader, "load_story", _load_story)
    monkeypatch.setattr(free_runtime.story_loader, "load_story_exact", _load_story_exact)

    # 记录模型替身实际收到的故事对象，直接断言作者图没有穿透投影边界。
    model_stories: list[dict] = []

    async def _fake_free_turn(**_kwargs):
        model_stories.append(_kwargs["story"])
        return _free_result()

    monkeypatch.setattr(free_runtime.llm, "generate_free_turn_async", _fake_free_turn)
    root = tmp_path / "theater"
    started = await free_runtime.start_session(
        root,
        lanlan_name="测试猫娘",
        story_id=THEATER_TEST_STORY_ID,
        client_start_id="free_start_1",
        role_card={
            "schema_version": free_role_card.FREE_ROLE_CARD_SCHEMA_VERSION,
            "name": "测试猫娘",
            "description": "临时自由角色。",
            "first_mes": "临时角色在门口回头。",
            "scenario": "自由测试场景。",
            "player_address": "哥哥",
            "player_role": "哥哥",
        },
    )
    assert started["ok"] is True
    assert started["mode"] == "free"
    assert started["state_revision"] == 0
    # 自由响应不再携带来源剧本舞台和阶段、Board、Trace、Choice 空壳字段。
    assert not {
        "phase",
        "scene",
        "scenario_board",
        "scenario_trace",
        "suggestion_options",
        "temporary_state",
    }.intersection(started)
    assert started["free_role_card"]["player_role"] == "哥哥"
    assert started["free_history"][-1]["role"] == "narrator"
    # RP-Hub 的 first_mes 直接作为首条 assistant 消息，不再为了开场重复调用模型。
    assert started["free_history"][-1]["text"] == "临时角色在门口回头。"
    assert model_stories == []

    # 刷新恢复必须继续公开当前 Session 的临时角色卡，但不能允许另一只猫娘读取它。
    restored = await free_runtime.get_active_state(root, lanlan_name="测试猫娘")
    assert restored["session_id"] == started["session_id"]
    assert restored["free_role_card"]["name"] == "测试猫娘"
    mismatch = await free_runtime.get_state(
        root,
        session_id=started["session_id"],
        expected_lanlan_name="另一只猫娘",
    )
    assert mismatch == {"ok": False, "reason": "session_character_mismatch"}

    free_session = await session_store.load_session(
        root / "free", started["session_id"]
    )
    assert free_session["mode"] == "free"
    assert free_session["source_scene_id"] == story["initial_scene_id"]
    assert "story_state" not in free_session
    assert "temporary_state" not in free_session
    assert free_session["turns"][0]["free_text"] == "临时角色在门口回头。"
    assert "text" not in free_session["turns"][0]
    assert not (root / "sessions" / f"{started['session_id']}.json").exists()

    played: list[str] = []

    async def _play(claim):
        played.append(claim["line"])
        return {"ok": True, "played": True}

    claimed = await free_runtime.claim_dialogue_speech(
        root,
        session_id=started["session_id"],
        state_revision=0,
        expected_lanlan_name="测试猫娘",
        play=_play,
    )
    duplicate_claim = await free_runtime.claim_dialogue_speech(
        root,
        session_id=started["session_id"],
        state_revision=0,
        expected_lanlan_name="测试猫娘",
        play=_play,
    )
    assert claimed["played"] is True
    assert duplicate_claim["skipped"] == "already_spoken"
    # RP-Hub first_mes 是自由模式首条历史正文，后续语音领取也必须播放这条正文。
    assert played == ["临时角色在门口回头。"]

    submitted = await free_runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="我把伞放到门边。",
        client_turn_id="free_turn_1",
        base_revision=0,
    )
    assert submitted["state_revision"] == 1
    assert model_stories[0]["schema_version"] == free_seed.FREE_SEED_SCHEMA_VERSION
    assert "narrative_nodes" not in model_stories[0]
    assert "edges" not in model_stories[0]
    duplicate = await free_runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="这条输入不应再次生成。",
        client_turn_id="free_turn_1",
        base_revision=0,
    )
    assert duplicate == submitted
    conflict = await free_runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="旧 revision 不应写入。",
        client_turn_id="free_turn_2",
        base_revision=0,
    )
    assert conflict["reason"] == "state_revision_conflict"
    saved = await session_store.load_session(root / "free", started["session_id"])
    assert saved["state_revision"] == 1
    assert saved["turns"][-1]["free_text"] == _free_result()["text"]
    assert "text" not in saved["turns"][-1]

    async def _failed_free_turn(**_kwargs):
        # 模型失败只返回错误，不应让 Runtime 把用户输入或半截回复写入 Session。
        return {"ok": False, "reason": "free_actor_unavailable"}

    monkeypatch.setattr(free_runtime.llm, "generate_free_turn_async", _failed_free_turn)
    failed = await free_runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="这次模型超时，不应进入历史。",
        client_turn_id="free_turn_failed",
        base_revision=1,
    )
    assert failed == {"ok": False, "reason": "free_actor_unavailable"}
    failed_saved = await session_store.load_session(root / "free", started["session_id"])
    assert failed_saved["state_revision"] == 1
    assert failed_saved["turns"] == saved["turns"]

    exited = await free_runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="user_exit",
        message="",
        client_turn_id="free_exit_1",
        base_revision=1,
    )
    assert exited["can_resume"] is False
    assert "scenario_trace" not in exited


@pytest.mark.asyncio
async def test_free_active_session_is_scoped_by_catgirl(monkeypatch, tmp_path):
    """不同猫娘的自由 active 指针必须互相独立，不能恢复到对方角色卡。"""  # noqa: DOCSTRING_CJK
    story = _free_source_story()

    async def _load_story(_story_id=None, **_kwargs):
        return deepcopy(story)

    async def _load_story_exact(_story_id, **_kwargs):
        return deepcopy(story)

    monkeypatch.setattr(free_runtime.story_loader, "load_story", _load_story)
    monkeypatch.setattr(free_runtime.story_loader, "load_story_exact", _load_story_exact)

    async def _fake_free_turn(**_kwargs):
        return {"text": "当前猫娘的自由开场。"}

    monkeypatch.setattr(free_runtime.llm, "generate_free_turn_async", _fake_free_turn)
    root = tmp_path / "theater"
    xiaokui = await free_runtime.start_session(
        root,
        lanlan_name="小葵",
        story_id=THEATER_TEST_STORY_ID,
        client_start_id="xiaokui_start",
    )
    tangtang = await free_runtime.start_session(
        root,
        lanlan_name="糖糖",
        story_id=THEATER_TEST_STORY_ID,
        client_start_id="tangtang_start",
    )

    assert xiaokui["session_id"] != tangtang["session_id"]
    assert (await free_runtime.get_active_state(root, lanlan_name="小葵"))["session_id"] == xiaokui["session_id"]
    assert (await free_runtime.get_active_state(root, lanlan_name="糖糖"))["session_id"] == tangtang["session_id"]


@pytest.mark.asyncio
async def test_free_actor_uses_conversation_tier_without_json_repair(monkeypatch):
    """自由 Actor 使用 conversation 槽位，一次调用直接接受纯文本。"""  # noqa: DOCSTRING_CJK
    calls: list[tuple[str, str]] = []
    timeouts: list[float] = []
    outputs = iter([_free_result()["text"]])

    class _Config:
        def get_model_api_config(self, tier):
            return {"model": f"{tier}-model", "base_url": "https://example.invalid"}

    async def _fake_invoke(api_config, *_args, call_type, **_kwargs):
        calls.append((call_type, api_config["model"]))
        timeouts.append(_kwargs["timeout_seconds"])
        return type("Result", (), {"content": next(outputs)})()

    monkeypatch.setattr(llm, "_invoke_model_once", _fake_invoke)
    result = await llm.generate_free_turn_async(
        config_manager=_Config(),
        lanlan_name="测试猫娘",
        story={
            "id": "free_story",
            "title": "自由测试",
            "background": "一间安静的房间",
            "scenario_card": {
                "player_role": "访客",
                "catgirl_role": "邻居",
                "primary_goal": "建立对话",
            },
        },
        scene={"id": "scene_free", "title": "房间", "text": "雨声从窗外传来。"},
        user_message="我先问候她。",
        recent_turns=[],
    )
    assert result["text"] == _free_result()["text"]
    assert calls == [("theater_free_actor", "conversation-model")]
    assert timeouts == [30.0]


@pytest.mark.asyncio
async def test_free_actor_timeout_returns_unavailable_without_repair(monkeypatch):
    """自由 Actor 超时后直接失败，不追加 Repair 或第二次模型调用。"""

    class _SlowClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def ainvoke(self, _messages):
            # 缩短测试预算，模拟供应商迟迟不返回；真实预算仍由生产常量控制。
            await asyncio.sleep(0.01)
            return type("Result", (), {"content": "不应被接受"})()

    async def _slow_client(*_args, **_kwargs):
        return _SlowClient()

    class _Config:
        def get_model_api_config(self, tier):
            return {"model": f"{tier}-model", "base_url": "https://example.invalid"}

    monkeypatch.setattr(llm, "create_chat_llm_async", _slow_client)
    monkeypatch.setattr(llm, "THEATER_FREE_TURN_TIMEOUT_SECONDS", 0.001)
    result = await llm.generate_free_turn_async(
        config_manager=_Config(),
        lanlan_name="测试猫娘",
        story={
            "id": "free_story",
            "title": "自由测试",
            "scenario_card": {"player_role": "访客", "catgirl_role": "邻居"},
        },
        scene={"id": "scene_free", "title": "房间", "text": "雨声从窗外传来。"},
        user_message="我先问候她。",
        recent_turns=[],
    )
    assert result == {"ok": False, "reason": "free_actor_unavailable"}
