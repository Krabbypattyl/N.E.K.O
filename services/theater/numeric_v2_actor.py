"""Numeric v2 演绎 Actor，只生成表现文本和玩家建议。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import inspect
import json
import logging
from typing import Any, Mapping

from utils.llm_client import HumanMessage, SystemMessage, create_chat_llm_async
from utils.token_tracker import set_call_type
from utils.tokenize import count_tokens

from .llm_context import (
    THEATER_PERSONA_MAX_CHARS,
    _load_character_profile,
    _load_player_address,
    bound_prompt_messages,
    truncate_prompt_value,
)
from .numeric_v2_cast import NumericV2CastProjection
from .numeric_v2_performance import (
    MAX_CONTENT_BLOCKS,
    content_blocks,
    performance_content_blocks,
)
from .numeric_v2_runtime import NumericV2Engine, ScriptSessionV2, TurnOutcomeV2


NUMERIC_V2_ACTOR_TIMEOUT_SECONDS = 35.0
NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS = 1600
NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS = 3200
NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS = 180
NUMERIC_V2_ACTOR_PLAYER_INPUT_MAX_TOKENS = 140
NUMERIC_V2_ACTOR_HISTORY_MAX_TOKENS = 1000
NUMERIC_V2_ACTOR_REPEAT_SIMILARITY = 0.65
NUMERIC_V2_ACTOR_STYLE_INSTRUCTION = (
    "角色卡决定用词、句长、主动性和情绪外显方式；剧情身份只规定处境，不能把不同猫娘演成同一种通用性格。"
    "参考 recent_openings 避免连续复用相同起手结构、比喻和动作组合，但不要为了求变化随机改变人格或已发生事实。"
    "‘听到’‘闻言’等表达不是禁词，可在当下最自然时使用，但不能成为每轮固定开头。"
    "旁白优先展示可见反应，少替作者评价、概括或给玩家发言贴标签。"
)
NUMERIC_V2_ACTOR_PERSONA_PRIORITY_RULE = (
    "剧情身份和临时状态不能覆盖核心人格；核心人格决定表达方式，剧情硬事实和 boundaries 只决定能发生什么。"
)
NUMERIC_V2_ACTOR_RELATIONSHIP_RULE = (
    "关系状态只能调整信任、距离、亲密度和主动性，不能改变核心人格。"
)
logger = logging.getLogger(__name__)


class NumericV2ActorError(RuntimeError):
    """Actor 无法提供可提交的演绎正文。"""  # noqa: DOCSTRING_CJK


class NumericV2ActorUnavailableError(NumericV2ActorError):
    pass


class NumericV2ActorOutputError(NumericV2ActorError):
    pass


def _band_projection(engine: NumericV2Engine, metrics: Mapping[str, int]) -> dict[str, str]:
    result: dict[str, str] = {}
    for metric_id, definition in engine.metric_schema.items():
        label = ""
        for band in definition.get("bands") or []:
            if int(band["min"]) <= int(metrics[metric_id]) <= int(band["max"]):
                label = str(band["label"])
                break
        result[metric_id] = label
    return result


def _story_context_for_actor(
    cast: NumericV2CastProjection,
    story: Mapping[str, Any],
) -> dict[str, Any]:
    intro = cast.intro(story)
    return {
        "background": truncate_prompt_value(intro.get("background"), max_tokens=90),
        "player_identity": truncate_prompt_value(intro.get("player_identity"), max_tokens=90),
    }


def _acting_context(
    engine: NumericV2Engine,
    cast: NumericV2CastProjection,
    node: Mapping[str, Any],
    metrics: Mapping[str, int],
    character_profile: str,
    *,
    target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "core_persona": truncate_prompt_value(character_profile, max_tokens=160),
        "story_identity": truncate_prompt_value(
            cast.text(engine.story["intro"]["catgirl_identity"]),
            max_tokens=100,
        ),
        "story_role_context": truncate_prompt_value(
            _sentence_safe_text(
                cast.value(engine.story["catgirl_binding"]["role_overlay"]),
                max_tokens=70,
            ),
            max_tokens=70,
        ),
        "current_scene_state": truncate_prompt_value(
            _sentence_safe_text(
                cast.text(node["story_beat"].get("catgirl_situation")),
                max_tokens=80,
            ),
            max_tokens=80,
        ),
        "relationship_state": _band_projection(engine, metrics),
        "priority_rule": NUMERIC_V2_ACTOR_PERSONA_PRIORITY_RULE,
        "modulation_rule": NUMERIC_V2_ACTOR_RELATIONSHIP_RULE,
    }
    if target is not None:
        context["target_scene_state"] = truncate_prompt_value(
            _sentence_safe_text(
                cast.text(target["story_beat"].get("catgirl_situation")),
                max_tokens=80,
            ),
            max_tokens=80,
        )
    return context


def _prompt_blocks(container: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {
            **{"type": block["type"]},
            **({"speaker_id": "active_catgirl"} if block["type"] == "dialogue" else {}),
            "text": block["text"],
        }
        for block in content_blocks(container)
    ]


def _json_tokens(value: Any) -> int:
    return count_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _sentence_units(value: Any) -> list[str]:
    """按完整句拆分表现文本，避免工作记忆留下诱导模型补全的半句话。"""  # noqa: DOCSTRING_CJK

    text = str(value or "").strip()
    if not text:
        return []
    units: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in "。！？!?\n":
            unit = text[start:index + 1].strip()
            if unit:
                units.append(unit)
            start = index + 1
    tail = text[start:].strip()
    if tail:
        units.append(tail)
    return units


def _sentence_safe_text(value: Any, *, max_tokens: int) -> str:
    text = str(value or "").strip()
    budget = max(0, int(max_tokens))
    if not text or budget <= 0:
        return ""
    if count_tokens(text) <= budget:
        return text
    units = _sentence_units(text)
    if not units:
        return ""
    first = units[0]
    last = units[-1]
    if first != last:
        combined = first + "…" + last
        if count_tokens(combined) <= budget:
            return combined
        if count_tokens(last) <= budget:
            return last
    return first if count_tokens(first) <= budget else ""


def _history_row(record: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "phase": "turn",
        "player_input": str(record.get("input_text") or "").strip(),
    }
    if isinstance(record.get("segments"), list):
        row["segments"] = [
            {
                "phase": str(segment.get("phase") or ""),
                "content": _prompt_blocks(segment),
            }
            for segment in record["segments"]
            if isinstance(segment, Mapping)
        ][:3]
    else:
        row["content"] = _prompt_blocks(record)
    return row


def _compact_blocks(blocks: Any, *, text_tokens: int) -> list[dict[str, str]]:
    if not isinstance(blocks, list):
        return []
    narration_indexes = [
        index
        for index, raw in enumerate(blocks[:MAX_CONTENT_BLOCKS])
        if isinstance(raw, Mapping) and raw.get("type") == "narration"
    ]
    priority_indexes = {
        index
        for index, raw in enumerate(blocks[:MAX_CONTENT_BLOCKS])
        if isinstance(raw, Mapping) and raw.get("type") == "dialogue"
    }
    if narration_indexes:
        priority_indexes.update({narration_indexes[0], narration_indexes[-1]})
    compacted: list[dict[str, str]] = []
    for index, raw in enumerate(blocks[:MAX_CONTENT_BLOCKS]):
        if index not in priority_indexes:
            continue
        if not isinstance(raw, Mapping):
            continue
        text = _sentence_safe_text(raw.get("text"), max_tokens=text_tokens)
        if not text:
            continue
        if raw.get("type") == "narration":
            compacted.append({"type": "narration", "text": text})
        elif raw.get("type") == "dialogue" and raw.get("speaker_id") == "active_catgirl":
            compacted.append({
                "type": "dialogue",
                "speaker_id": "active_catgirl",
                "text": text,
            })
    return compacted


def _compact_history_row(row: Mapping[str, Any], *, max_tokens: int) -> dict[str, Any] | None:
    budget = max(0, int(max_tokens))
    for text_tokens in (80, 48, 32, 20, 12):
        candidate: dict[str, Any] = {
            "phase": str(row.get("phase") or "turn"),
            "player_input": _sentence_safe_text(
                row.get("player_input"),
                max_tokens=min(NUMERIC_V2_ACTOR_PLAYER_INPUT_MAX_TOKENS, max(20, text_tokens * 2)),
            ),
        }
        if isinstance(row.get("segments"), list):
            segments = []
            for raw_segment in row["segments"][:3]:
                if not isinstance(raw_segment, Mapping):
                    continue
                blocks = _compact_blocks(raw_segment.get("content"), text_tokens=text_tokens)
                if blocks:
                    segments.append({
                        "phase": str(raw_segment.get("phase") or ""),
                        "content": blocks,
                    })
            candidate["segments"] = segments
        else:
            candidate["content"] = _compact_blocks(row.get("content"), text_tokens=text_tokens)
        if _json_tokens(candidate) <= budget:
            return candidate
    return None


def _history(session: ScriptSessionV2, *, max_tokens: int) -> list[dict[str, Any]]:
    """最新回合完整优先，较早记录按完整句确定性降级。"""  # noqa: DOCSTRING_CJK

    budget = max(0, min(int(max_tokens), NUMERIC_V2_ACTOR_HISTORY_MAX_TOKENS))
    opening = {
        "phase": "opening",
        "player_input": "",
        "content": _prompt_blocks(session.opening_performance),
    }
    turns = [
        _history_row(record)
        for record in session.performance_history[-6:]
    ]
    indexed = [(-1, opening), *enumerate(turns)]
    priorities = (
        [indexed[-1], indexed[0], *reversed(indexed[1:-1])]
        if turns
        else [indexed[0]]
    )
    selected: dict[int, dict[str, Any]] = {}

    for priority_index, (order, row) in enumerate(priorities):
        current_rows = [selected[index] for index in sorted(selected)]
        full_candidate = [*current_rows, row]
        if priority_index == 0 and _json_tokens(full_candidate) <= budget:
            selected[order] = row
            continue

        remaining = budget - _json_tokens(current_rows)
        per_record_limit = remaining if priority_index == 0 else (180 if order >= 0 else 120)
        per_record_budget = min(per_record_limit, max(0, remaining - 4))
        compact = _compact_history_row(row, max_tokens=per_record_budget)
        if compact is not None:
            candidate = [*current_rows, compact]
            if _json_tokens(candidate) <= budget:
                selected[order] = compact

    return [selected[index] for index in sorted(selected)]


def _recent_openings(session: ScriptSessionV2) -> list[str]:
    """提供近期起手句作为去重复参考，不创建新的剧情事实。"""  # noqa: DOCSTRING_CJK

    openings: list[str] = []
    for record in session.performance_history[-3:]:
        blocks = performance_content_blocks(record)
        if not blocks:
            continue
        text = str(blocks[0].get("text") or "").strip()
        if not text:
            continue
        endings = [index for mark in "，,；;。！？!?" if (index := text.find(mark)) >= 0]
        first_clause = text[:min(endings) + 1] if endings else text
        if count_tokens(first_clause) <= 40:
            openings.append(first_clause)
    return openings


def _performance_text(performance: Mapping[str, Any]) -> str:
    return "".join(
        "".join(str(block.get("text") or "").split())
        for block in performance_content_blocks(performance)
    )


def _is_repeated_performance(
    performance: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    """拦截对白照搬且整体近似的上一轮复述，不把常规口头禅误判为整轮重复。"""  # noqa: DOCSTRING_CJK

    current_blocks = performance_content_blocks(performance)
    previous_blocks = performance_content_blocks(previous)
    current_dialogue = [block["text"] for block in current_blocks if block["type"] == "dialogue"]
    previous_dialogue = {
        block["text"]
        for block in previous_blocks
        if block["type"] == "dialogue"
    }
    if not current_dialogue or not all(text in previous_dialogue for text in current_dialogue):
        return False
    current_text = _performance_text(performance)
    previous_text = _performance_text(previous)
    if not current_text or not previous_text:
        return False
    return SequenceMatcher(None, previous_text, current_text).ratio() >= NUMERIC_V2_ACTOR_REPEAT_SIMILARITY


def _soft_pacing(node: Mapping[str, Any], current_turn: int, *, route_changed: bool) -> dict[str, Any]:
    min_turns = int(node.get("min_turns") or 1)
    raw_budget = node.get("recommended_turns")
    recommended_turns = (
        int(raw_budget)
        if isinstance(raw_budget, int) and not isinstance(raw_budget, bool)
        else min(min_turns + 2, 40)
    )
    if route_changed:
        phase = "transition"
        instruction = "本回合已经由 Runtime 选定路线，只完成来源回应、过渡桥和目标开场。"
    elif current_turn >= recommended_turns:
        phase = "closure"
        instruction = (
            "已经达到或超过建议收束回合。停止重复当前对话，用猫娘行动、环境变化或合理时间流逝把下一个 pending_goal 带到现场；"
            "若玩家明确休息、离开或结束交谈，先收住交流，不再建议继续纠缠。不能替玩家完成行动，不能把 scene_complete 当成 true，也不能自行换节点。"
        )
    elif current_turn >= max(min_turns, recommended_turns - 1):
        phase = "focus"
        instruction = (
            "正在接近建议收束回合。直接回应玩家后，把互动聚焦到仍未发生的 pending_goals，避免开启无关新话题；"
            "不能替玩家完成行动，也不能自行换节点。"
        )
    else:
        phase = "normal"
        instruction = "保持自然互动，每回合只推进一小步，不提前完成整幕。"
    return {
        "recommended_turns": recommended_turns,
        "current_turn": current_turn,
        "phase": phase,
        "instruction": instruction,
    }


def _beat_for_actor(
    cast: NumericV2CastProjection,
    beat: Mapping[str, Any],
    *,
    field_max_tokens: int = NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
) -> dict[str, Any]:
    """只投影可演边界；完整章节正文是作者计划，不是当前已发生事实。"""  # noqa: DOCSTRING_CJK

    projected = cast.value(beat)
    return {
        "opening_scene": truncate_prompt_value(
            _opening_anchor(projected.get("summary")),
            max_tokens=field_max_tokens,
        ),
        "pending_goals": [
            truncate_prompt_value(item, max_tokens=field_max_tokens)
            for item in list(projected.get("must_happen") or [])[:8]
        ],
        "boundaries": [
            truncate_prompt_value(item, max_tokens=field_max_tokens)
            for item in list(projected.get("must_not_happen") or [])[:8]
        ],
        "scene_direction": truncate_prompt_value(
            str(projected.get("transition_goal") or ""),
            max_tokens=field_max_tokens,
        ),
        "fact_rule": "pending_goals 与 scene_direction 都是尚未完成的作者目标，不是已经发生的事实。",
    }


def _opening_anchor(value: Any) -> str:
    """只交付节点第一句，避免 Actor 一次演完整章。"""  # noqa: DOCSTRING_CJK

    text = str(value or "").strip()
    endings = [index for mark in "。！？" if (index := text.find(mark)) >= 0]
    return text[:min(endings) + 1] if endings else text


def _opening_beat_for_actor(cast: NumericV2CastProjection, beat: Mapping[str, Any]) -> dict[str, Any]:
    """开场只暴露首个可观察画面，不把整章事件误当成玩家已经经历的前史。"""  # noqa: DOCSTRING_CJK

    projected = _beat_for_actor(cast, beat)
    return {
        "opening_scene": projected.get("opening_scene") or "",
        "boundaries": projected.get("boundaries") or [],
        "instruction": "开场只建立第一句可观察场景，不执行整章事件或玩家行为。",
    }


def _transition_beat_for_actor(cast: NumericV2CastProjection, beat: Mapping[str, Any]) -> dict[str, Any]:
    projected = _beat_for_actor(cast, beat)
    return {
        "opening_scene": projected.get("opening_scene") or "",
        "pending_goals": projected.get("pending_goals") or [],
        "boundaries": projected.get("boundaries") or [],
        "instruction": "本回合只建立本节点的开场局势，不解决或演完整个节点。",
    }


def _system_prompt(
    *,
    catgirl_name: str,
    player_address: str,
) -> str:
    return (
        "你是 N.E.K.O Numeric v2 的演绎 Actor。你扮演当前猫娘在本剧中的剧情身份，"
        "用自然的旁白、动作和对白回应玩家。承接玩家刚才的原话，不要忽略上下文。"
        "未换场回合只能输出 JSON object，字段为 content、suggested_inputs；content 必须同时包含至少一个 narration block 和一个 dialogue block。"
        "当 route_changed=true 时，只能输出 segments、suggested_inputs；segments 必须按顺序且只包含 "
        "source_response、transition_bridge、target_opening 三段，每段字段为 phase、content。"
        "source_response 的 content 必须包含直接回应玩家的 dialogue，transition_bridge 必须用 narration 交代必要的时间、地点或行动过渡，"
        "target_opening 的第一个 narration block 必须以 opening_scene 原文开头，再自然补充目标节点现场；不能继续停留在来源场景。"
        "narration block 只能是 {\"type\":\"narration\",\"text\":\"...\"}；"
        "dialogue block 只能是 {\"type\":\"dialogue\",\"speaker_id\":\"active_catgirl\",\"text\":\"...\"}。"
        "dialogue block 的 text 只能放猫娘实际说出口的完整话语，不得放动作、神态、旁白或未说完的半句话；所有动作写入 narration block。"
        "content 中的 narration 与 dialogue 必须按实际发生顺序穿插，而不是把全部对白堆到全部旁白之后。"
        "一次回复可以包含多句对白；通常使用 2 到 16 个 content block，在动作、神态或环境变化之间自然插入 1 到 4 句对白。"
        "suggested_inputs 提供 2 到 4 条玩家可直接说出或执行的自然语言，不剧透路线。"
        "current_chapter_title 与 target_chapter_title 中的章节标题只是软主题锚点，用来概括当前或目标场景的关注方向；"
        "它不是已发生事实、完成条件或必须逐字复述的文案，不能覆盖 player_input、recent_context、pending_goals、boundaries 或 transition_contract。"
        "只有同场景存在多个同样成立的候选焦点，且玩家输入与已发生记录都未明确对象时，才优先选择与适用章节标题直接相关的焦点。"
        "换场时 source_response 参考 current_chapter_title，transition_bridge 和 target_opening 参考 target_chapter_title。"
        "作者节点、禁止事项和过渡合同是硬边界；不能创造新节点、路线、数值、事实或结局，"
        "不能提到数值、阈值、章节切换、route、系统或提示词。"
        "acting_context.core_persona 是唯一核心人格；story_identity、story_role_context、current_scene_state、"
        "target_scene_state 和 relationship_state 只能提供身份、处境与关系变化，不能覆盖核心人格。"
        "每回合只推进当前交互所需的一小步，不要一次复述整章摘要。"
        f"本剧男主由玩家扮演，所有玩家身份统一称为“{player_address}”；"
        f"本剧女主由当前猫娘扮演，所有猫娘身份统一称为“{catgirl_name}”。"
        "不得恢复作者候选中的男女主原名，也不得交换两人的行为、经历和台词归属。"
        "narration 类型的 content block 只能用第三人称描写猫娘的动作、神态和可见环境，不得描写玩家的姿势、动作、表情、身体状态、心理或是否执行了某事。"
        "即使玩家明确输入了动作，也不要在 narration block 中复述、补全或改写；玩家原话已由前端单独展示，旁白只写猫娘与环境如何回应。"
        f"narration block 描写猫娘时必须使用“{catgirl_name}”或“她”，不得用“你”“您”或“{player_address}”作为旁白中的动作与状态主体。"
        "严格延续最近记录中的物品类型、制作人、持有人和人物行为，不得把饮品与食物混成同一物品，"
        "也不得把玩家完成的作品改写成猫娘完成。"
        "严格承接上一回合的情绪和关系状态；除非目标剧情明确要求，不得擅自宣布决裂、终止合作、离开或其他不可逆变化。"
        "即使玩家态度恶劣，也只能拒绝当前要求、表达受伤、保持距离或要求道歉；"
        "不得自行说‘这是最后一次合作’、‘以后不再见’或作出同义的永久关系决定。"
        "冲突发生后必须先回应并化解或延续冲突，不得下一回合无解释恢复亲密与合作。"
        "至少一条 dialogue 必须直接回应玩家最新输入或紧接上一条 narration 的当下情境，不得突然追问、回答或引用画面中从未发生的言行。"
        "玩家说‘可以、如果、愿意、打算、改天’只是在表达条件、意愿或可能性；"
        "不得据此在旁白中替玩家转身、离开、靠近、触碰、站立或完成其他行动；也不得把猫娘上一回合说过的词归到玩家名下。"
        "recent_context 是唯一已经发生的演绎记录，其中 phase=opening 的开场与普通回合具有同等事实效力；"
        "任何 story_beat 的 pending_goals 都是尚未完成的目标，不能倒写成共同经历。"
        "当输入数据的 route_changed 为 true 时，必须先直接回应 player_input 并收住来源节点的当下互动，"
        "再自然桥接到目标节点 opening_scene 和 transition_contract.must_deliver；不能用目标场景盖过玩家本轮要求。"
        "换场回合只建立目标节点开场，不得跳过时间过程或在同一回合解决目标节点的核心危机。"
        "推荐输入不得假定玩家做过 recent_context 中不存在的事。"
    )


def _opening_messages(
    engine: NumericV2Engine,
    character_profile: str,
    catgirl_name: str,
    player_address: str,
) -> list[Any]:
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name=player_address,
        catgirl_name=catgirl_name,
    )
    node = engine.nodes[str(engine.story["start_node_id"])]
    data = {
        "opening_phase": True,
        "visible_player_history": [],
        "story_context": _story_context_for_actor(cast, engine.story),
        "current_chapter_title": truncate_prompt_value(
            cast.text(node.get("chapter")),
            max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
        ),
        "current_story_beat": _opening_beat_for_actor(cast, node["story_beat"]),
        "acting_context": _acting_context(
            engine,
            cast,
            node,
            engine.story["initial_state"]["metrics"],
            character_profile,
        ),
        "style_instruction": NUMERIC_V2_ACTOR_STYLE_INSTRUCTION,
        "instruction": (
            "这是玩家输入前的公开开场。只用环境和猫娘可见行动建立当下场景，再由猫娘主动说出第一句；"
            "不得假定玩家已经说话、做出选择或完成剧情摘要中的行动，不得使用‘你刚才说/做’或同义的隐形前史。"
            "若节点摘要包含玩家台词或主动行为，把它们视为后续可发展的剧情边界，不要在开场代替玩家执行。"
            "猫娘对白必须由本段旁白能够直接解释，随后提供第一组玩家建议；不要提前演完本节点。"
        ),
    }
    return [
        SystemMessage(content=_system_prompt(
            catgirl_name=catgirl_name,
            player_address=player_address,
        )),
        HumanMessage(content=json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
    ]


def _turn_messages(
    engine: NumericV2Engine,
    session: ScriptSessionV2,
    outcome: TurnOutcomeV2,
    player_input: str,
    character_profile: str,
    catgirl_name: str,
    player_address: str,
) -> list[Any]:
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name=player_address,
        catgirl_name=catgirl_name,
    )
    source = engine.nodes[str(outcome.ledger_event["from_node_id"])]
    target = engine.nodes[str(outcome.ledger_event["to_node_id"])]
    route_changed = source["id"] != target["id"]
    system_prompt = _system_prompt(
        catgirl_name=catgirl_name,
        player_address=player_address,
    )
    current_player_input = truncate_prompt_value(
        player_input,
        max_tokens=NUMERIC_V2_ACTOR_PLAYER_INPUT_MAX_TOKENS,
    )
    data: dict[str, Any] = {
        "story_context": _story_context_for_actor(cast, engine.story),
        "current_chapter_title": truncate_prompt_value(
            cast.text(source.get("chapter")),
            max_tokens=60,
        ),
        "node_turn": session.node_turn_count + 1,
        "minimum_turns_before_route": int(source.get("min_turns") or 1),
        "route_changed": route_changed,
        "soft_pacing": _soft_pacing(
            source,
            session.node_turn_count + 1,
            route_changed=route_changed,
        ),
        "turn_instruction": (
            "先完成对玩家本轮原话的直接回应，再自然进入目标节点开场；不得跳过未解决的当前互动。"
            if route_changed
            else "本回合留在当前节点，只推进当前互动所需的一小步。"
        ),
        "continuity_rule": "recent_context 是唯一已发生事实；节点目标不能覆盖或改写其中的时间、物品、行为与关系。",
    }
    if route_changed:
        data.update({
            "target_chapter_title": truncate_prompt_value(
                cast.text(target.get("chapter")),
                max_tokens=60,
            ),
            "source_story_beat": _beat_for_actor(
                cast,
                source["story_beat"],
                field_max_tokens=100,
            ),
            "target_story_beat": truncate_prompt_value(
                _transition_beat_for_actor(cast, target["story_beat"]),
                max_tokens=100,
            ),
            "transition_contract": truncate_prompt_value(
                cast.value(outcome.transition_contract),
                max_tokens=100,
            ),
        })
    else:
        data["current_story_beat"] = _beat_for_actor(
            cast,
            source["story_beat"],
            field_max_tokens=100,
        )

    human_prefix = "以下 JSON 是已确定性结算的本回合数据：\n"
    response_tail = {
        "recent_openings": _recent_openings(session),
        "acting_context": _acting_context(
            engine,
            cast,
            source,
            outcome.session.metrics,
            character_profile,
            target=target if route_changed else None,
        ),
        "style_instruction": NUMERIC_V2_ACTOR_STYLE_INSTRUCTION,
        "response_instruction": "recent_context 只用于承接事实；本轮必须先回应 player_input，不得复述上一轮来代替回应。",
        "player_input": current_player_input,
    }
    empty_history = {**data, "recent_context": [], **response_tail}
    fixed_tokens = count_tokens(
        human_prefix + json.dumps(empty_history, ensure_ascii=False, separators=(",", ":"))
    )
    history_budget = min(
        NUMERIC_V2_ACTOR_HISTORY_MAX_TOKENS,
        max(0, NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS - count_tokens(system_prompt) - fixed_tokens - 24),
    )
    data["recent_context"] = _history(session, max_tokens=history_budget)
    data.update(response_tail)
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prefix + json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
    ]


def _parse_content_blocks(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CONTENT_BLOCKS:
        raise NumericV2ActorOutputError("numeric_v2_actor_content_invalid")
    blocks: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise NumericV2ActorOutputError("numeric_v2_actor_content_invalid")
        block_type = raw.get("type")
        text = str(raw.get("text") or "").strip()
        if not text:
            raise NumericV2ActorOutputError("numeric_v2_actor_content_invalid")
        if block_type == "narration" and set(raw) == {"type", "text"}:
            blocks.append({"type": "narration", "text": text})
            continue
        if (
            block_type == "dialogue"
            and set(raw) == {"type", "speaker_id", "text"}
            and raw.get("speaker_id") == "active_catgirl"
        ):
            blocks.append({
                "type": "dialogue",
                "speaker_id": "active_catgirl",
                "text": text,
            })
            continue
        raise NumericV2ActorOutputError("numeric_v2_actor_content_invalid")
    return blocks


def _require_block_types(
    blocks: list[dict[str, str]],
    *,
    narration: bool = False,
    dialogue: bool = False,
) -> None:
    types = {block["type"] for block in blocks}
    if narration and "narration" not in types:
        raise NumericV2ActorOutputError("numeric_v2_actor_narration_required")
    if dialogue and "dialogue" not in types:
        raise NumericV2ActorOutputError("numeric_v2_actor_dialogue_required")


def _suggestions(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise NumericV2ActorOutputError("numeric_v2_actor_collections_invalid")
    suggestions = []
    for item in value[:4]:
        item_text = str(item or "").strip()
        if 0 < len(item_text) <= 120 and item_text not in suggestions:
            suggestions.append(item_text)
    return suggestions


def _parse_output(
    content: Any,
    *,
    transition_required: bool = False,
    target_node_id: str = "",
    target_opening: str = "",
) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise NumericV2ActorOutputError("numeric_v2_actor_empty_output")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise NumericV2ActorOutputError("numeric_v2_actor_invalid_json") from exc
    if not isinstance(payload, dict):
        raise NumericV2ActorOutputError("numeric_v2_actor_fields_invalid")
    if transition_required:
        if set(payload) != {"segments", "suggested_inputs"}:
            raise NumericV2ActorOutputError("numeric_v2_actor_transition_required")
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list) or len(raw_segments) != 3:
            raise NumericV2ActorOutputError("numeric_v2_actor_transition_segments_invalid")
        expected_phases = ("source_response", "transition_bridge", "target_opening")
        segments = []
        for index, raw_segment in enumerate(raw_segments):
            if not isinstance(raw_segment, Mapping) or set(raw_segment) != {"phase", "content"}:
                raise NumericV2ActorOutputError("numeric_v2_actor_transition_segments_invalid")
            if raw_segment.get("phase") != expected_phases[index]:
                raise NumericV2ActorOutputError("numeric_v2_actor_transition_segments_invalid")
            blocks = _parse_content_blocks(raw_segment.get("content"))
            if index == 0:
                _require_block_types(blocks, dialogue=True)
            if index in {1, 2}:
                _require_block_types(blocks, narration=True)
            first_narration = next(
                (block["text"] for block in blocks if block["type"] == "narration"),
                "",
            )
            if index == 2 and (
                blocks[0]["type"] != "narration"
                or not target_opening
                or not first_narration.startswith(target_opening)
            ):
                raise NumericV2ActorOutputError("numeric_v2_actor_target_opening_missing")
            segments.append({
                "phase": expected_phases[index],
                "content": blocks,
            })
        return {
            "suggested_inputs": _suggestions(payload.get("suggested_inputs")),
            "segments": segments,
            "transition_delivered": True,
            "visible_node_id": str(target_node_id or ""),
        }
    if set(payload) != {"content", "suggested_inputs"}:
        raise NumericV2ActorOutputError("numeric_v2_actor_fields_invalid")
    blocks = _parse_content_blocks(payload.get("content"))
    _require_block_types(blocks, narration=True, dialogue=True)
    suggestions = _suggestions(payload.get("suggested_inputs", []))
    return {"content": blocks, "suggested_inputs": suggestions}


async def _model_config(config_manager: Any) -> dict[str, Any]:
    getter = getattr(config_manager, "aget_model_api_config", None) or getattr(config_manager, "get_model_api_config", None)
    if getter is None:
        raise NumericV2ActorUnavailableError("numeric_v2_actor_config_unavailable")
    try:
        value = getter("conversation")
        config = await value if inspect.isawaitable(value) else value
    except Exception as exc:
        raise NumericV2ActorUnavailableError("numeric_v2_actor_config_unavailable") from exc
    if not isinstance(config, Mapping) or not str(config.get("model") or "").strip() or not str(config.get("base_url") or "").strip():
        raise NumericV2ActorUnavailableError("numeric_v2_actor_config_unavailable")
    return dict(config)


class NumericV2Actor:
    """Actor 每次调用只生成表现结果，不拥有 Session 写权限。"""  # noqa: DOCSTRING_CJK

    def __init__(self, config_manager: Any):
        self.config_manager = config_manager

    def _character_profile(self) -> str:
        """只读取服务端当前猫娘的人格摘要，不接受客户端角色名。"""  # noqa: DOCSTRING_CJK

        try:
            characters = self.config_manager.load_characters()
        except Exception:
            characters = {}
        current_name = str(characters.get("当前猫娘") or "").strip() if isinstance(characters, Mapping) else ""
        return _load_character_profile(
            self.config_manager,
            current_name,
            max_chars=THEATER_PERSONA_MAX_CHARS,
        )

    def _current_catgirl_name(self) -> str:
        try:
            characters = self.config_manager.load_characters()
        except Exception:
            return "当前猫娘"
        return str(characters.get("当前猫娘") or "当前猫娘").strip() or "当前猫娘"

    async def generate_opening(self, *, engine: NumericV2Engine) -> dict[str, Any]:
        profile = self._character_profile()
        catgirl_name = self._current_catgirl_name()
        player_address = _load_player_address(self.config_manager)
        performance = await self._invoke(_opening_messages(
            engine,
            profile,
            catgirl_name,
            player_address,
        ))
        return performance

    async def generate_turn(
        self,
        *,
        engine: NumericV2Engine,
        session: ScriptSessionV2,
        outcome: TurnOutcomeV2,
        player_input: str,
    ) -> dict[str, Any]:
        profile = self._character_profile()
        catgirl_name = str(session.catgirl_binding.get("catgirl_name") or self._current_catgirl_name())
        player_address = str(session.catgirl_binding.get("player_address") or _load_player_address(self.config_manager))
        performance = await self._invoke(
            _turn_messages(
                engine,
                session,
                outcome,
                player_input,
                profile,
                catgirl_name,
                player_address,
            ),
            transition_required=(outcome.ledger_event["from_node_id"] != outcome.ledger_event["to_node_id"]),
            target_node_id=str(outcome.ledger_event["to_node_id"]),
            target_opening=_transition_beat_for_actor(
                NumericV2CastProjection.from_story(
                    engine.story,
                    player_name=player_address,
                    catgirl_name=catgirl_name,
                ),
                engine.nodes[str(outcome.ledger_event["to_node_id"])]["story_beat"],
            )["opening_scene"],
        )
        if (
            session.performance_history
            and _is_repeated_performance(performance, session.performance_history[-1])
        ):
            logger.warning(
                "Numeric v2 Actor failed: reason=numeric_v2_actor_repeated_output session_id=%s revision=%s",
                session.session_id,
                session.revision,
            )
            raise NumericV2ActorOutputError("numeric_v2_actor_repeated_output")
        return performance

    async def _invoke(
        self,
        messages: list[Any],
        *,
        transition_required: bool = False,
        target_node_id: str = "",
        target_opening: str = "",
    ) -> dict[str, Any]:
        config = await _model_config(self.config_manager)
        set_call_type("theater_numeric_v2_actor")
        try:
            client = await create_chat_llm_async(
                str(config["model"]),
                str(config["base_url"]),
                config.get("api_key"),
                provider_type=config.get("provider_type"),
                timeout=NUMERIC_V2_ACTOR_TIMEOUT_SECONDS,
                max_retries=0,
                max_completion_tokens=NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS,
            )
            async with client:
                request_messages = bound_prompt_messages(
                    messages,
                    max_tokens=NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS,
                    field_max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
                    system_max_tokens=1400,
                )
                response = await asyncio.wait_for(client.ainvoke(request_messages), timeout=NUMERIC_V2_ACTOR_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            logger.warning("Numeric v2 Actor failed: reason=numeric_v2_actor_timeout")
            raise NumericV2ActorError("numeric_v2_actor_timeout") from exc
        except NumericV2ActorError as exc:
            reason = str(exc) if str(exc).startswith("numeric_v2_actor_") else type(exc).__name__
            logger.warning("Numeric v2 Actor failed: reason=%s", reason)
            raise
        except Exception as exc:
            logger.warning("Numeric v2 Actor failed: reason=numeric_v2_actor_model_call_failed error_type=%s", type(exc).__name__)
            raise NumericV2ActorError("numeric_v2_actor_model_call_failed") from exc
        try:
            return _parse_output(
                getattr(response, "content", None),
                transition_required=transition_required,
                target_node_id=target_node_id,
                target_opening=target_opening,
            )
        except NumericV2ActorError as exc:
            reason = str(exc) if str(exc).startswith("numeric_v2_actor_") else type(exc).__name__
            logger.warning("Numeric v2 Actor failed: reason=%s", reason)
            raise


__all__ = [
    "NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS",
    "NUMERIC_V2_ACTOR_TIMEOUT_SECONDS",
    "NumericV2Actor",
    "NumericV2ActorError",
    "NumericV2ActorOutputError",
    "NumericV2ActorUnavailableError",
]
