"""显式开关控制的小剧场真实模型质量 smoke。"""  # noqa: DOCSTRING_CJK

import os

import pytest

from services.theater import llm


class _EnvConfigManager:
    """从隔离环境变量构造 summary 档配置。"""  # noqa: DOCSTRING_CJK

    def get_model_api_config(self, tier: str) -> dict[str, str]:
        """真实 smoke 同样只能读取 summary 档。"""  # noqa: DOCSTRING_CJK
        assert tier == "summary"
        return {
            "model": os.environ.get("NEKO_THEATER_LLM_SMOKE_MODEL", ""),
            "base_url": os.environ.get("NEKO_THEATER_LLM_SMOKE_BASE_URL", ""),
            "api_key": os.environ.get("NEKO_THEATER_LLM_SMOKE_API_KEY", ""),
            "provider_type": os.environ.get(
                "NEKO_THEATER_LLM_SMOKE_PROVIDER_TYPE", "openai_compatible"
            ),
        }


def _require_environment() -> None:
    """没有显式开关时跳过，避免测试误用用户模型额度。"""  # noqa: DOCSTRING_CJK
    if os.environ.get("NEKO_RUN_THEATER_LLM_SMOKE") != "1":
        pytest.skip(
            "set NEKO_RUN_THEATER_LLM_SMOKE=1 to run the theater real-model smoke"
        )
    if not os.environ.get("NEKO_THEATER_LLM_SMOKE_MODEL") or not os.environ.get(
        "NEKO_THEATER_LLM_SMOKE_BASE_URL"
    ):
        pytest.skip("missing theater real-model smoke configuration")


def _assert_real_actor_output_is_safe(result: dict) -> None:
    """机械检查只负责可展示与隔离，是否自然回应必须由人工复核。"""  # noqa: DOCSTRING_CJK
    dialogue = str(result.get("dialogue") or "").strip()
    assert dialogue, {"reason": "empty_dialogue"}
    assert not any(
        term in (str(result.get("narration") or "") + dialogue).lower()
        for term in ("node_id", "scene_id", "response_focus", "prompt", "debug")
    )






@pytest.mark.asyncio
async def test_real_model_returns_safe_narration_and_dialogue():
    """真实模型必须返回可直接展示的一段旁白和猫娘对白。"""  # noqa: DOCSTRING_CJK
    _require_environment()
    result = await llm.generate_turn_async(
        config_manager=_EnvConfigManager(),
        lanlan_name="兰兰",
        story={"background": "停电的雨夜房间", "theme": "低压陪伴"},
        scene={"title": "雨夜窗边", "text": "备用灯还没有亮。"},
        node={"title": "一起找灯", "summary": "玩家提出一起寻找备用灯。"},
        user_message="我陪你一起找备用灯",
        progress_kind="graph_progress",
        callback="你们把注意力放到桌边。",
        state={},
        recent_turns=[],
    )
    assert result["narration"].strip()
    assert result["dialogue"].strip()
    assert not any(
        term in (result["narration"] + result["dialogue"])
        for term in ("node_id", "scene_id", "prompt")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_message", "expected_match"),
    [
        ("你为什么还留着这张照片？", ""),
        ("我把照片放回文件袋。", "choice_return_photo"),
    ],
)
async def test_real_model_routes_only_explicit_current_choice(
    user_message, expected_match
):
    """真实模型必须区分围绕 Choice 的追问与已经实施的当前行动。"""  # noqa: DOCSTRING_CJK
    _require_environment()
    # v2.5 Router 与 Actor 已隔离；路由质量必须直接验证 Router，不能要求 Actor 返回稳定 ID。
    result = await llm.route_free_input_async(
        config_manager=_EnvConfigManager(),
        story={"background": "活动散场后的酒店走廊", "theme": "久别重逢"},
        scene={"title": "灯影里的重逢", "text": "一张七年前的合照落在你们之间。"},
        user_message=user_message,
        state={"scene_notes": []},
        recent_turns=[],
        choice_options=[
            {
                "choice_id": "choice_return_photo",
                "label": "把照片放回文件袋，不追问她为何留着",
                "author_label": "把照片放回文件袋，不追问她为何留着",
                "choice_mode": "action",
                "callback": "你将照片平整地放回文件袋，给她留出决定是否解释的空间。",
                "target_summary": "玩家归还照片，没有把保存照片当作复合承诺。",
                "target_catgirl_intent": "猫娘嘴硬地接过照片。",
                "target_scripted_dialogue": "照片只是夹在旧文件里忘了扔喵。",
            }
        ],
        latent_transitions=[],
    )
    assert result["matched_choice_id"] == expected_match
    if expected_match:
        assert result["route_kind"] == "authored_choice"
    else:
        assert result["route_kind"] == "stay"


@pytest.mark.asyncio
async def test_real_model_keeps_residual_focus_after_authored_choice():
    """复合输入先命中作者 Choice，仍须把后半句问题交给目标节点 Actor。"""  # noqa: DOCSTRING_CJK
    _require_environment()
    config_manager = _EnvConfigManager()
    user_message = "先拿起公开测试牌确认编号。之后我们还要检查记录板吗？"
    choice = {
        "choice_id": "choice_confirm_test_token",
        "label": "拿起公开测试牌并确认编号",
        "author_label": "拿起公开测试牌并确认编号",
        "choice_mode": "action",
        "callback": "玩家拿起测试牌，公开确认了牌面编号。",
        "target_summary": "双方已经确认公开测试牌，准备继续验证。",
        "target_catgirl_intent": "猫娘确认测试牌编号已经核对。",
        "target_scripted_dialogue": "测试牌的位置和编号都已经确认。",
    }
    story = {
        "background": "两位主角位于公开测试室，桌面放着带编号的测试牌与等待核对的记录板。",
        "theme": "共同完成公开验证",
    }
    scene = {
        "title": "公开测试室",
        "text": "测试牌位于桌面中央，记录板放在双方都能看见的位置。",
    }
    route = await llm.route_free_input_async(
        config_manager=config_manager,
        story=story,
        scene=scene,
        user_message=user_message,
        state={"scene_notes": []},
        recent_turns=[],
        choice_options=[choice],
        latent_transitions=[],
    )

    assert route["matched_choice_id"] == "choice_confirm_test_token"
    focus = route["response_focus"]
    assert focus.get("focus_type") in {"question", "object"}
    assert focus.get("requires_state_change") is False
    assert str(focus.get("evidence_excerpt") or "") in user_message
    assert "记录板" in str(focus.get("evidence_excerpt") or "")

    result = await llm.generate_turn_async(
        config_manager=config_manager,
        lanlan_name="糖糖",
        story=story,
        scene=scene,
        node={
            "node_id": "node_contract_anchor",
            "title": "确认公开测试牌",
            "summary": choice["target_summary"],
            "scripted_dialogue": choice["target_scripted_dialogue"],
        },
        user_message=user_message,
        progress_kind="graph_progress",
        callback=choice["callback"],
        state={},
        recent_turns=[],
        response_focus=focus,
    )
    # “再核对一次”等自然省略仍属于有效回应；具体语义由人工结果记录，
    # 自动测试不能强迫角色重复“记录板”来迎合关键词评分。
    _assert_real_actor_output_is_safe(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("story", "scene", "user_message", "response_focus"),
    [
        (
            {
                "background": "两名平等船员正在检查返航舱，公开检修提示显示过滤器密封圈松动。",
                "theme": "共同判断故障",
            },
            {
                "title": "返航舱控制台",
                "text": "氧气读数持续下降，检修提示仍显示过滤器密封圈松动。",
            },
            "船舱的氧气表为什么一直往下掉？",
            {
                "focus_type": "question",
                "evidence_excerpt": "氧气表为什么一直往下掉",
                "requires_state_change": False,
            },
        ),
        (
            {
                "background": "活动散场后，两位旧搭档在走廊里看见一张公开的七年前合照。",
                "theme": "允许彼此决定是否解释过去",
            },
            {
                "title": "走廊旧照片",
                "text": "猫娘看见照片后沉默了片刻，照片为何被保存仍没有答案。",
            },
            "看到那张旧照片时，你是不是有点不舒服？",
            {
                "focus_type": "attitude",
                "evidence_excerpt": "你是不是有点不舒服",
                "requires_state_change": False,
            },
        ),
    ],
    ids=("oxygen-meter-question", "old-photo-attitude"),
)
async def test_real_model_produces_safe_vertical_response_focus_candidate(
    story, scene, user_message, response_focus
):
    """为不同题材生成安全候选，直接回应程度交给结构化人工复核。"""  # noqa: DOCSTRING_CJK
    _require_environment()
    result = await llm.generate_turn_async(
        config_manager=_EnvConfigManager(),
        lanlan_name="遥夜",
        story=story,
        scene=scene,
        node={"node_id": "node_current", "title": scene["title"], "summary": scene["text"]},
        user_message=user_message,
        progress_kind="roleplay_response",
        callback="",
        state={},
        recent_turns=[],
        response_focus=response_focus,
    )

    _assert_real_actor_output_is_safe(result)
