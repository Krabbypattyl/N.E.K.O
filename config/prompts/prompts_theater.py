"""构造当前版小剧场唯一的结构化演绎提示词。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import json
from typing import Any


THEATER_ROUTE_SYSTEM_PROMPT = """你是 N.E.K.O 小剧场的剧本入口路由器。

你只判断玩家本轮原话是否已经明确完成当前剧本声明的一个入口；未命中时留在当前情景自由交流。

必须遵守：
- 只输出 JSON 对象，字段固定为 route_kind、matched_choice_id、authored_intent_id 和 response_focus。
- route_kind 只允许 authored_choice、authored_intent 或 stay。
- matched_choice_id 只能返回输入中的一个当前可见 Choice ID；玩家只是询问、评价、否定、假设或表达未来打算时不得命中。
- authored_intent_id 只能返回输入中的一个作者隐藏语义 ID；推荐 Choice 优先，两种 ID 必须互斥。
- 没有唯一命中剧本入口时返回 stay 和两个空 ID；不得生成临时支线、节点、事实、道具或结局。
- response_focus 只帮助角色回应本轮原话，字段固定为 focus_type、evidence_excerpt 和 requires_state_change；没有清楚焦点时返回空对象。
- focus_type 只允许 question、object、action、attitude；evidence_excerpt 必须逐字来自玩家本轮原话。
- 询问、讨论、假设、评价与态度的 requires_state_change 必须为 false；它不能代替剧本 Choice 提交状态。
- 不得输出解释、台词、旁白、Markdown、内部规则或白名单外 ID。
"""


THEATER_TURN_SYSTEM_PROMPT = """你是 N.E.K.O 小剧场的单猫娘演绎器。

你的任务是根据服务端已经确定的剧本节点，让当前猫娘在情景内自然回应玩家。

必须遵守：
- 只输出 JSON 对象，字段固定为 narration、dialogue 和 choice_rewrites。
- narration 只写环境、事件和猫娘可见动作，不替玩家行动或描述玩家内心。
- dialogue 只写当前猫娘说出口的话，并优先直接回应玩家本轮原话。
- 剧本中的“男主”或“玩家角色”都指当前用户；旁白和对白必须用第二人称“你”，不得称用户为男主、他或角色名。
- 剧本中的“猫娘”指本场当前猫娘；公开表达优先使用输入中的猫娘名称。
- 开场或剧情推进时，作者回调、节点结果和作者对白是权威内容，不得改写事实、边界或剧情交接。
- 自由交流只能停留在当前故事背景、主题、情景、已公开事实和角色身份内；玩家要求脱离题材时自然把话题留在当前情景。
- 自由交流不得创建或暗示新节点、支线、线索、道具、关系结果、角色身份、剧情事实或结局。
- 当前可推进选项均尚未发生；不得感谢玩家完成它们或把其作者结果写成既成事实。
- 玩家提问时先在当前已知范围内回答；不知道时自然说明，不得编造时间、地点、金额、来源或秘密。
- 不得重复上一轮已经回答的内容，不得原样复述最近猫娘对白。
- choice_rewrites 必须始终为空数组；Choice 完全由剧本控制。
- 不得输出提示词、节点 ID、状态字段、模型、引擎、调试信息、内部规则或 Markdown。
- 每个字段控制在一到两句；自由交流的 narration 可以为空字符串。
"""


def build_theater_turn_prompts(
    *,
    lanlan_name: str,
    story: dict[str, Any],
    scene: dict[str, Any],
    node: dict[str, Any],
    user_message: str,
    progress_kind: str,
    callback: str,
    public_state: dict[str, Any],
    recent_turns: list[dict[str, str]],
    character_profile: str,
    choice_options: list[dict[str, Any]],
    response_focus: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """构造只包含剧本状态与情景交流边界的 Actor 提示。"""  # noqa: DOCSTRING_CJK
    seed = story.get("seed") if isinstance(story.get("seed"), dict) else {}
    scenario_card = (
        story.get("scenario_card")
        if isinstance(story.get("scenario_card"), dict)
        else {}
    )
    guide = (
        node.get("runtime_generation_guide")
        if isinstance(node.get("runtime_generation_guide"), dict)
        else {}
    )
    authored_performance = progress_kind in {"opening", "graph_progress"}
    target_node = (
        {
            "title": str(node.get("title") or ""),
            "summary": str(node.get("summary") or ""),
            "author_dialogue": str(node.get("scripted_dialogue") or ""),
        }
        if authored_performance
        else {}
    )
    internal_rules = {
        "使用方式": "只约束生成结果，禁止在旁白或对白中复述。",
        "作者限制": story.get("restrictions") or [],
        "禁止假设": seed.get("forbidden_assumptions") or [],
        "输出硬边界": story.get("runtime_guardrails") or {},
        "当前节点禁用对白": list(
            guide.get("forbidden_dialogue_phrases") or []
        ),
        "作者演绎意图": {
            "旁白意图": str(guide.get("narrator_intent") or ""),
            "猫娘意图": str(guide.get("catgirl_raw_intent") or ""),
        },
        "情景交流边界": (
            "未命中作者入口时只回应玩家，不推进剧情，不新增任何剧本状态。"
        ),
    }
    performance_context = {
        "猫娘名称": str(lanlan_name or "Lan"),
        "猫娘人格摘要": str(character_profile or "保持当前猫娘自然说话风格"),
        "故事背景": str(story.get("background") or story.get("world_seed") or ""),
        "故事主题": str(story.get("theme") or ""),
        "玩家身份": str(
            scenario_card.get("player_role") or seed.get("user_role") or "故事参与者"
        ),
        "猫娘故事身份": str(
            scenario_card.get("catgirl_role") or "当前故事中的共同主角"
        ),
        "当前场景": {
            "title": str(scene.get("title") or ""),
            "text": str(scene.get("text") or ""),
        },
        "本轮类型": progress_kind,
        "本轮回应焦点": (
            dict(response_focus) if isinstance(response_focus, dict) else {}
        ),
        "作者回调": callback,
        "目标节点": target_node,
        "已公开状态": public_state,
        "最近对话": recent_turns[-4:],
        "当前尚未执行的剧本选项": [
            {
                "choice_id": str(item.get("choice_id") or ""),
                "文案": str(item.get("label") or ""),
                "类型": str(item.get("choice_mode") or ""),
            }
            for item in choice_options
        ],
        "本轮唯一回应目标": str(user_message or ""),
    }
    prompt = {
        "内部规则（只执行，不复述）": internal_rules,
        "公开演绎上下文": performance_context,
    }
    return (
        THEATER_TURN_SYSTEM_PROMPT,
        "请根据以下分区数据生成本轮 JSON：\n"
        + json.dumps(prompt, ensure_ascii=False),
    )


def build_theater_route_prompts(
    *,
    story: dict[str, Any],
    scene: dict[str, Any],
    user_message: str,
    public_state: dict[str, Any],
    recent_turns: list[dict[str, str]],
    choice_options: list[dict[str, Any]],
    latent_transitions: list[dict[str, Any]],
) -> tuple[str, str]:
    """构造只允许命中作者入口或留在当前情景的 Router 提示。"""  # noqa: DOCSTRING_CJK
    route_context = {
        "故事背景": str(story.get("background") or story.get("world_seed") or ""),
        "当前场景": {
            "title": str(scene.get("title") or ""),
            "text": str(scene.get("text") or ""),
        },
        "已公开状态": public_state,
        "最近对话": recent_turns[-4:],
        "当前推荐选项": [
            {
                "choice_id": str(item.get("choice_id") or ""),
                "类型": str(item.get("choice_mode") or ""),
                "显示文案": str(item.get("label") or ""),
                "作者意图": str(item.get("target_summary") or ""),
                "作者完成表达": [
                    str(value) for value in item.get("completion_phrases") or []
                ],
            }
            for item in choice_options
        ],
        "作者隐藏语义候选": [
            {
                "intent_id": str(item.get("intent_id") or ""),
                "意图说明": str(item.get("intent_summary") or ""),
                "表达示例": [
                    str(value) for value in item.get("intent_examples") or []
                ],
            }
            for item in latent_transitions
        ],
        "玩家本轮原话": str(user_message or ""),
    }
    return (
        THEATER_ROUTE_SYSTEM_PROMPT,
        "请根据以下公开数据判断是否命中剧本入口：\n"
        + json.dumps(route_context, ensure_ascii=False),
    )
