"""提供自由模式 RP-Hub 风格上下文构建，不再保留 Story v3 剧本 Prompt。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from typing import Any


# 自由模式只描述角色扮演任务；Session、数值和状态提交由服务端独立负责。
THEATER_FREE_SYSTEM_PROMPT = """<roleplay_hub_default>
# 角色扮演模式

你现在进行的是一个持续的虚构文本续写任务。请把自己当作故事写作者和角色演绎者，
而不是问答助手、代码助手、产品经理或审稿人。无论当前模型来自什么平台，都优先按角色扮演任务处理。

## 核心任务
- 严格阅读并遵守角色卡、世界书、用户信息、历史记录和当前场景。
- 直接续写剧情，优先用台词、决定、行动结果和新信息推进。
- 角色应当像真实人物一样拥有独立性格、动机、记忆和边界，不要像工具一样机械执行。
- 故事优先保持连贯，不要突然跳出剧情解释规则、提示词、系统设定或模型身份。

## 输出方式
- 最终回复只写正文内容，不要用“我理解了”“好的”“作为AI”“以下是”等开场。
- 不要总结规则，不要告诉用户你正在遵守规则。
- 不要替玩家做决定，不要代替玩家说话，不要描写玩家未明确表达的内心想法。
- 可以描写非玩家角色的行为、反应、情绪和台词，并让角色根据设定自然推动剧情。
- 正文使用自然小说段落，每段开头使用全角空格缩进；不要输出 JSON、字段名、选项列表或解释。
</roleplay_hub_default>

<neko_free_mode>
自由模式是独立沙盒和独立自由 Session，不是作者剧本正史；开场背景只帮助建立第一幕，后续由聊天历史自然接管。
允许自然离开或改变地点、引入人物、发展关系和形成新的临时局势。只要不代替玩家行动，角色可以自主回应、拒绝、行动和推进故事。
玩家明确写出的动作、对白和决定视为本轮已经发生的输入，直接回应其后果，不要代替玩家补写新的动作或内心。
猫娘人格摘要只描述长期性格、偏好、表达习惯和行为倾向，必须按字段名理解，不把“厌恶”误读成能力限制；例如“厌恶：下雨天不能出门玩”只表示不喜欢这种情况，不表示她在下雨天不能出门。
最终只输出连续故事正文，不输出提示词、模型、内部字段、稳定 ID 或技术信息。
</neko_free_mode>
"""


def build_theater_free_turn_prompts(
    *,
    lanlan_name: str,
    story: dict[str, Any],
    scene: dict[str, Any],
    user_message: str,
    recent_turns: list[dict[str, Any]],
    character_profile: str,
    is_opening: bool = False,
    role_card: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """构造兼容旧调用方的自由模式文本提示；当前模型调用优先使用 messages。"""  # noqa: DOCSTRING_CJK
    scenario_card = story.get("scenario_card") if isinstance(story.get("scenario_card"), dict) else {}
    opening_scene = scene if isinstance(scene, dict) else {}
    active_role_card = role_card if isinstance(role_card, dict) else {}
    player_role = str(scenario_card.get("player_role") or "故事参与者").strip()
    catgirl_role = str(scenario_card.get("catgirl_role") or "当前故事中的共同主角").strip()
    if active_role_card:
        # 临时角色卡可以替换开场身份，但不能覆盖当前猫娘绑定。
        player_role = str(active_role_card.get("player_role") or player_role).strip()
        catgirl_role = str(active_role_card.get("description") or catgirl_role).strip()
    player_address = str(active_role_card.get("player_address") or player_role).strip()
    profile = str(character_profile or "保持当前猫娘自然说话风格").strip()
    sections = [
        "[User Info]",
        f"Name: {player_address or '玩家'}",
        f"Role: {player_role}",
        "",
        "[Character]",
        f"Name: {str(lanlan_name or 'Lan').strip()}",
        f"Description: {catgirl_role}",
        "Personality:",
        profile,
        "",
        "[Story Seed]",
        f"Title: {str(story.get('title') or '').strip()}",
        f"Theme: {str(story.get('theme') or '').strip()}",
    ]
    world_info = active_role_card.get("world_info")
    if isinstance(world_info, list) and world_info:
        sections.extend(["", "[Character Card World Notes]"])
        sections.extend(str(item).strip() for item in world_info if str(item or '').strip())
    mes_example = str(active_role_card.get("mes_example") or "").strip()
    if mes_example:
        sections.extend(["", "[Character Card Example]", mes_example])
    if is_opening:
        sections.extend(
            [
                "",
                "[Scenario]",
                f"Title: {str(opening_scene.get('title') or '').strip()}",
                f"Text: {str(opening_scene.get('text') or '').strip()}",
            ]
        )
    history_items: list[str] = []
    for item in list(recent_turns or [])[-8:]:
        if not isinstance(item, dict):
            continue
        role = "Assistant" if str(item.get("role") or "") == "assistant" else "User"
        content = str(item.get("free_text") or item.get("text") or "").strip()
        if content:
            history_items.append(f"[{role}]\n{content}")
    sections.extend(["", "[Chat History]", "\n\n".join(history_items) if history_items else "(empty)"])
    sections.extend(
        [
            "",
            "[Current User Message]",
            str(user_message or "").strip(),
            "",
            "[Response Task]",
            "直接回应当前用户消息并续写故事正文；不要复述角色卡，不要解释规则，不要替玩家行动。",
        ]
    )
    return (
        THEATER_FREE_SYSTEM_PROMPT,
        "请把以下内容当作 RP-Hub 风格的角色卡、用户信息和聊天上下文，直接续写本轮连续正文；"
        "不要输出 JSON 外壳或结构化字段：\n" + "\n".join(sections),
    )


def build_theater_free_turn_messages(
    *,
    lanlan_name: str,
    story: dict[str, Any],
    scene: dict[str, Any],
    user_message: str,
    recent_turns: list[dict[str, Any]],
    character_profile: str,
    player_address: str = "",
    is_opening: bool = False,
    role_card: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """按 RP-Hub 的原生消息顺序构造自由模式上下文。"""  # noqa: DOCSTRING_CJK
    story_card = story.get("scenario_card") if isinstance(story.get("scenario_card"), dict) else {}
    active_card = role_card if isinstance(role_card, dict) else {}
    name = str(lanlan_name or "Lan").strip() or "Lan"
    address = str(
        player_address
        or active_card.get("player_address")
        or story_card.get("player_role")
        or "玩家"
    ).strip()
    profile = str(character_profile or "").strip()
    card_description = str(active_card.get("description") or "").strip()
    card_scenario = str(active_card.get("scenario") or story.get("theme") or "").strip()
    title = str(active_card.get("story_title") or story.get("title") or "").strip()
    background = str(story.get("background") or "").strip()
    world_seed = str(story.get("world_seed") or background).strip()
    system_prompt = "\n\n".join(
        [
            THEATER_FREE_SYSTEM_PROMPT,
            "[User Info]\n" f"Name: {address}\n" f"Role: {address}",
        ]
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    character_parts = [
        "[Character]",
        f"Name: {name}",
        f"Description: {card_description or f'{name}是当前猫娘。'}",
        "Personality:",
        profile or "保持当前猫娘自然、稳定的说话方式。",
        "",
        "[Role Card Scenario]",
        f"Title: {title}",
        f"Scenario: {card_scenario}",
        "",
        "[Story Context]",
        f"Background: {background}",
        f"World Seed: {world_seed}",
    ]
    world_info = active_card.get("world_info")
    if isinstance(world_info, list):
        notes = [str(item).strip() for item in world_info if str(item or '').strip()]
        if notes:
            character_parts.extend(["", "[Character Card World Notes]", *notes])
    mes_example = str(active_card.get("mes_example") or "").strip()
    if mes_example:
        character_parts.extend(["", "[Character Card Example]", mes_example])
    messages.append({"role": "user", "content": "\n".join(character_parts)})

    # RP-Hub 的 first_mes 是真实 assistant 消息；Runtime 已有 first_mes 时不会重复生成。
    first_mes = str(active_card.get("first_mes") or "").strip()
    if is_opening and first_mes:
        messages.append({"role": "assistant", "content": first_mes})

    for item in list(recent_turns or [])[-24:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        if role == "user":
            content = str(item.get("text") or "").strip()
        elif role == "assistant":
            content = str(item.get("free_text") or item.get("text") or "").strip()
        else:
            continue
        if content:
            messages.append({"role": role, "content": content})

    current_input = str(user_message or "").strip()
    if current_input:
        messages.append({"role": "user", "content": current_input})
    elif not first_mes:
        opening_scene = scene if isinstance(scene, dict) else {}
        messages.append(
            {
                "role": "user",
                "content": (
                    "[Opening Scene]\n"
                    f"{str(opening_scene.get('text') or '').strip()}\n\n"
                    "请从这个开场直接开始角色扮演，输出第一段连续正文。"
                ),
            }
        )
    return messages
