"""Numeric v2 演绎 Actor，只生成表现文本和玩家建议。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import inspect
import json
import logging
import re
import time
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
    content_blocks,
    mixed_performance_blocks,
    performance_content_blocks,
)
from .numeric_v2_runtime import NumericV2Engine, ScriptSessionV2, TurnOutcomeV2


NUMERIC_V2_ACTOR_TIMEOUT_SECONDS = 35.0
NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS = 1600
NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS = 4800
NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS = 180
# Actor 自己负责按完整记录装箱；通用装箱器不得再次裁断背景、身份或历史事实。
NUMERIC_V2_ACTOR_BOUND_FIELD_MAX_TOKENS = NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS
NUMERIC_V2_ACTOR_PLAYER_INPUT_MAX_TOKENS = 140
NUMERIC_V2_ACTOR_HISTORY_MAX_TOKENS = 2200
NUMERIC_V2_ACTOR_REPEAT_SIMILARITY = 0.65
NUMERIC_V2_ACTOR_SUGGESTION_REPEAT_SIMILARITY = 0.8
NUMERIC_V2_ACTOR_SLOW_CALL_SECONDS = 15.0
NUMERIC_V2_ACTOR_NARRATION_BREVITY_INSTRUCTION = (
    "In a performance string, text inside Chinese full-width parentheses is a visible "
    "micro-action and all text outside parentheses is spoken dialogue by the active catgirl. "
    "Actions and dialogue may interleave naturally; do not target a fixed number of actions, "
    "sentences, or dialogue lines, and do not add an action mechanically before every sentence. "
    "Each parenthesized action must be one immediate micro-action by the active catgirl, no "
    "longer than 18 CJK characters or 12 words. Describe motion, not a "
    "static emotional explanation, psychological conclusion, relationship judgment, "
    "future beat, or scene summary. Parentheses must be balanced and cannot be nested. "
    "Opening scene_narration, transition_bridge scene_narration, target_opening "
    "scene_narration, and ending delivery keep their existing scene-narration contracts "
    "and are not subject to the micro-action length rule."
)
NUMERIC_V2_ACTOR_STYLE_INSTRUCTION = (
    "The character card determines wording, sentence length, initiative, and emotional "
    "expression. Story identity only defines the situation and must not flatten different "
    "catgirls into one generic personality. Use recent_openings to avoid repeating the same "
    "opening structure, metaphor, or action pattern, but never vary established personality "
    "or facts merely for novelty. Hearing-response phrases are allowed when natural, but must "
    "not become a fixed opening template. Dialogue carries the main information and emotion. "
    "Use action only to complete the visible moment, and make ordinary action narration a "
    "dynamic micro-action rather than a static emotional explanation."
)
NUMERIC_V2_ACTOR_PERSONA_PRIORITY_RULE = (
    "剧情身份和临时状态不能覆盖核心人格，核心人格决定表达方式。无论剧情要求警惕、敌视、受伤或疏远，都必须先保持角色卡的"
    "核心用词、语气、句长、主动性和情绪表达方式；临时状态只能改变她此刻如何表达核心人格，不能把她改写成另一种人格。"
)
NUMERIC_V2_ACTOR_RELATIONSHIP_RULE = (
    "关系状态只能调整信任、距离、亲密度和主动性，不能改变核心人格。"
    "好感、信任、亲密等关系型 metric 的 stage 是当前可见亲密度上限：lowest 只允许基本礼貌、有限软化和保持距离，"
    "不得主动暧昧、占有、依赖或使用伴侣式亲昵称呼；middle 可以主动关心和靠近，但不能直接演成已经倾心或永久绑定；"
    "highest 仍须由 recent_context 中已发生的事实支撑。甜美、温柔或粘人只决定表达方式，不代表关系已经建立；"
    "低关系阶段也不授权敌视、羞辱或威胁。"
)
NUMERIC_V2_ACTOR_SUGGESTION_CONTRACT = (
    "suggested_inputs 必须是点击后原样发送的玩家自然语言。"
    "动作省略玩家主语并从动词起笔；台词直接写说出口的内容；混合项写成动作加中文引号台词。"
    "对白内可按语义自然使用‘我’，不得机械写成‘我做某事’，也不得使用‘解释、询问、表示、保证、提出、展示、选择、注意到、尝试’等操作说明。"
)
_INDIRECT_SUGGESTION_PREFIX_RE = re.compile(
    r"^(?:请)?(?:解释|询问|表示|保证|提出|展示|选择|注意到|观察|尝试|请求|说明)(?:自己|对方|她|他)?"
)
_INDIRECT_SUGGESTION_QUESTION_RE = re.compile(
    r"(?:^|[，,；;。])(?:再|然后|接着)?问(?:她|他|对方)?(?:是否|为什么|为何|能否|可否|有没有|是不是)"
)
_INDIRECT_SUGGESTION_INTENT_RE = re.compile(
    r"^(?![“\"])[^。！？!?]{1,24}(?:表示|解释|说明)"
    r"(?:自己|没有|来意|原因|情况|身份|意图|想法|立场|诚意|[“\"])"
)
_TIME_ANCHOR_RE = re.compile(
    r"清晨|早晨|上午|中午|午后|下午|傍晚|黄昏|夜晚|深夜|凌晨|午夜|"
    r"次日|翌日|第二天|周末|打烊后|日出|日落"
)
NUMERIC_V2_ACTOR_OUTPUT_SCHEMA_INSTRUCTION = (
    "最终回复必须且只能是一个可由 JSON.parse 直接解析的 JSON object；禁止输出 Markdown 代码围栏、"
    "JSON 前后解释、标题或任何额外文字。所有键和字符串必须使用 JSON 双引号。"
    "普通回合顶层字段必须且只能是 performance:string、suggested_inputs:string[]。"
    "opening_phase=true 时顶层字段必须且只能是 scene_narration:string、performance:string、suggested_inputs:string[]。"
    "route_changed=true 时顶层字段必须且只能是 segments:object[]、suggested_inputs:string[]；segments 必须依次为："
    "source_response，字段只能是 phase、performance；transition_bridge，字段只能是 phase、scene_narration；"
    "target_opening，字段只能是 phase、performance。不要照抄任何其他剧本、人物、地点、物品或推荐语。"
)
logger = logging.getLogger(__name__)


class NumericV2ActorError(RuntimeError):
    """Actor 无法提供可提交的演绎正文。"""  # noqa: DOCSTRING_CJK


class NumericV2ActorUnavailableError(NumericV2ActorError):
    pass


class NumericV2ActorOutputError(NumericV2ActorError):
    pass


def _band_projection(
    engine: NumericV2Engine,
    metrics: Mapping[str, int],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for metric_id, definition in engine.metric_schema.items():
        label = ""
        stage = "only"
        bands = list(definition.get("bands") or [])
        for band_index, band in enumerate(bands):
            if int(band["min"]) <= int(metrics[metric_id]) <= int(band["max"]):
                label = str(band["label"])
                if len(bands) > 1:
                    if band_index == 0:
                        stage = "lowest"
                    elif band_index == len(bands) - 1:
                        stage = "highest"
                    else:
                        stage = "middle"
                break
        # 只投影区间名称和相对阶段，帮助 Actor 控制关系进度，同时继续隐藏真实数值与阈值。
        result[metric_id] = {"label": label, "stage": stage}
    return result


def _story_context_for_actor(
    cast: NumericV2CastProjection,
    story: Mapping[str, Any],
) -> dict[str, Any]:
    """完整投影稳定剧本前提，不能从字段中间裁掉关键居住权或身份事实。"""  # noqa: DOCSTRING_CJK

    intro = cast.intro(story)
    return {
        "background": str(intro.get("background") or ""),
        "player_identity": str(intro.get("player_identity") or ""),
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
    # 临时剧情信息先作为局势输入，核心人格和优先规则最后出现，降低长上下文中被覆盖的概率。
    context: dict[str, Any] = {
        "story_identity": cast.text(engine.story["intro"]["catgirl_identity"]),
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
    }
    if target is not None:
        context["target_scene_state"] = truncate_prompt_value(
            _sentence_safe_text(
                cast.text(target["story_beat"].get("catgirl_situation")),
                max_tokens=80,
            ),
            max_tokens=80,
        )
    context.update({
        "core_persona": truncate_prompt_value(character_profile, max_tokens=160),
        "priority_rule": NUMERIC_V2_ACTOR_PERSONA_PRIORITY_RULE,
        "modulation_rule": NUMERIC_V2_ACTOR_RELATIONSHIP_RULE,
    })
    return context


def _blocks_to_performance(blocks: list[dict[str, str]]) -> str:
    """把旧内容块投影成新 Prompt 使用的混合演绎正文。"""  # noqa: DOCSTRING_CJK

    parts: list[str] = []
    for block in blocks:
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        parts.append(f"（{text}）" if block.get("type") == "narration" else text)
    return "".join(parts)


def _prompt_container(container: Mapping[str, Any], *, phase: str) -> dict[str, str]:
    """把新旧 Session 都投影为场景旁白加混合正文，避免 Prompt 保留块协议。"""  # noqa: DOCSTRING_CJK

    if "scene_narration" in container or "performance" in container:
        result = {}
        scene_narration = str(container.get("scene_narration") or "").strip()
        performance = str(container.get("performance") or "").strip()
        if scene_narration:
            result["scene_narration"] = scene_narration
        if performance:
            result["performance"] = performance
        return result

    blocks = content_blocks(container)
    if phase in {"opening", "transition_bridge", "target_opening"}:
        scene_narration = "".join(
            block["text"] for block in blocks if block["type"] == "narration"
        )
        dialogue = "".join(
            block["text"] for block in blocks if block["type"] == "dialogue"
        )
        result = {}
        if scene_narration:
            result["scene_narration"] = scene_narration
        if dialogue:
            result["performance"] = dialogue
        return result
    performance = _blocks_to_performance(blocks)
    return {"performance": performance} if performance else {}


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


def _normalized_comparison_text(value: Any) -> str:
    """去掉不影响语义比较的空白和标点，保留否定词与正文。"""  # noqa: DOCSTRING_CJK

    return re.sub(r"[^\w\u3400-\u9fff]+", "", str(value or "")).casefold()


def _text_is_covered(
    value: Any,
    references: list[str],
    *,
    similarity: float,
    common_span: int = 0,
    strong_common_span: int = 0,
) -> bool:
    """判断一条文本是否已经被任一完整参考句覆盖。"""  # noqa: DOCSTRING_CJK

    candidate = _normalized_comparison_text(value)
    if not candidate:
        return False
    for reference in references:
        normalized_reference = _normalized_comparison_text(reference)
        if not normalized_reference:
            continue
        if candidate in normalized_reference or normalized_reference in candidate:
            return True
        matcher = SequenceMatcher(None, candidate, normalized_reference)
        ratio = matcher.ratio()
        if ratio >= similarity:
            return True
        if strong_common_span > 0 and matcher.find_longest_match().size >= strong_common_span:
            return True
        if (
            common_span > 0
            and ratio >= 0.32
            and matcher.find_longest_match().size >= common_span
        ):
            return True
    return False


def _shares_time_anchor(value: Any, references: list[str]) -> bool:
    """目标开场拥有具体时段，同一时段不能在桥接段提前再说一次。"""  # noqa: DOCSTRING_CJK

    anchors = set(_TIME_ANCHOR_RE.findall(str(value or "")))
    if not anchors:
        return False
    return any(
        anchors.intersection(_TIME_ANCHOR_RE.findall(reference))
        for reference in references
    )


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
                **_prompt_container(
                    segment,
                    phase=str(segment.get("phase") or ""),
                ),
            }
            for segment in record["segments"]
            if isinstance(segment, Mapping)
        ][:3]
    else:
        row.update(_prompt_container(record, phase="ordinary"))
    return row


def _current_scene_history_row(
    record: Mapping[str, Any],
    *,
    current_node_id: str,
) -> dict[str, Any]:
    """换场记录只保留桥接和目标开场，避免上一幕回应回流成当前待办。"""  # noqa: DOCSTRING_CJK

    row = _history_row(record)
    from_node_id = str(record.get("from_node_id") or "")
    to_node_id = str(record.get("to_node_id") or "")
    if (
        from_node_id == current_node_id
        or to_node_id != current_node_id
        or not isinstance(row.get("segments"), list)
    ):
        return row
    return {
        **row,
        # 玩家输入和来源回应属于上一幕；当前幕只承接换场事实与目标开场。
        "player_input": "",
        "segments": [
            segment
            for segment in row["segments"]
            if segment.get("phase") != "source_response"
        ],
    }


def _compact_performance_text(value: Any, *, text_tokens: int) -> str:
    """按完整动作和完整对白压缩混合正文，不能裁出半个括号。"""  # noqa: DOCSTRING_CJK

    blocks = mixed_performance_blocks(value)
    if not blocks:
        return _sentence_safe_text(value, max_tokens=text_tokens)
    narration_indexes = [
        index for index, block in enumerate(blocks) if block["type"] == "narration"
    ]
    selected = {
        index for index, block in enumerate(blocks) if block["type"] == "dialogue"
    }
    if narration_indexes:
        selected.update({narration_indexes[0], narration_indexes[-1]})
    compacted: list[dict[str, str]] = []
    for index, block in enumerate(blocks):
        if index not in selected:
            continue
        text = _sentence_safe_text(block["text"], max_tokens=text_tokens)
        if text:
            compacted.append({**block, "text": text})
    return _blocks_to_performance(compacted)


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
                segment = {"phase": str(raw_segment.get("phase") or "")}
                scene_narration = _sentence_safe_text(
                    raw_segment.get("scene_narration"),
                    max_tokens=text_tokens,
                )
                performance = _compact_performance_text(
                    raw_segment.get("performance"),
                    text_tokens=text_tokens,
                )
                if scene_narration:
                    segment["scene_narration"] = scene_narration
                if performance:
                    segment["performance"] = performance
                if len(segment) > 1:
                    segments.append(segment)
            candidate["segments"] = segments
        else:
            scene_narration = _sentence_safe_text(
                row.get("scene_narration"),
                max_tokens=text_tokens,
            )
            performance = _compact_performance_text(
                row.get("performance"),
                text_tokens=text_tokens,
            )
            if scene_narration:
                candidate["scene_narration"] = scene_narration
            if performance:
                candidate["performance"] = performance
        if _json_tokens(candidate) <= budget:
            return candidate
    return None


def _history(session: ScriptSessionV2, *, max_tokens: int) -> list[dict[str, Any]]:
    """当前场景开场和前文优先，超预算时从最早完整回合开始降级。"""  # noqa: DOCSTRING_CJK

    budget = max(0, min(int(max_tokens), NUMERIC_V2_ACTOR_HISTORY_MAX_TOKENS))
    opening = {
        "phase": "opening",
        "player_input": "",
        **_prompt_container(session.opening_performance, phase="opening"),
    }
    current_node_id = str(session.current_node_id)
    visit_records: list[Mapping[str, Any]] = []
    entered_current_node = False
    for record in reversed(session.performance_history):
        from_node_id = str(record.get("from_node_id") or "")
        to_node_id = str(record.get("to_node_id") or "")
        if from_node_id == current_node_id and to_node_id == current_node_id:
            visit_records.append(record)
            continue
        if to_node_id == current_node_id and from_node_id != current_node_id:
            visit_records.append(record)
            entered_current_node = True
        break

    rows = ([] if entered_current_node else [opening]) + [
        _current_scene_history_row(record, current_node_id=current_node_id)
        for record in reversed(visit_records)
    ]
    if not rows:
        rows = [opening]
    priorities = [len(rows) - 1]
    if len(rows) > 1:
        priorities.append(0)
        priorities.extend(range(len(rows) - 2, 0, -1))
    selected: dict[int, dict[str, Any]] = {}

    for priority_index, order in enumerate(priorities):
        row = rows[order]
        current_rows = [selected[index] for index in sorted(selected)]
        full_candidate = [*current_rows, row]
        if priority_index == 0 and _json_tokens(full_candidate) <= budget:
            selected[order] = row
            continue

        remaining = budget - _json_tokens(current_rows)
        per_record_limit = remaining if priority_index == 0 else (120 if order == 0 else 180)
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


def _recent_suggestions(session: ScriptSessionV2) -> list[str]:
    """提供近期推荐语用于避重复，不把推荐语当作已经发生的玩家行动。"""  # noqa: DOCSTRING_CJK

    suggestions: list[str] = []
    for record in session.performance_history[-2:]:
        for item in record.get("suggested_inputs") or []:
            text = str(item or "").strip()
            if text and text not in suggestions:
                suggestions.append(text)
    return suggestions[:6]


def _deduplicate_recent_suggestions(
    suggestions: list[str],
    recent_suggestions: list[str],
) -> list[str]:
    """丢弃近期推荐语的近义改写，不让过滤失败整轮演绎。"""  # noqa: DOCSTRING_CJK

    kept: list[str] = []
    references = [*recent_suggestions]
    for suggestion in suggestions:
        if _text_is_covered(
            suggestion,
            references,
            similarity=NUMERIC_V2_ACTOR_SUGGESTION_REPEAT_SIMILARITY,
        ):
            continue
        kept.append(suggestion)
        references.append(suggestion)
    # 推荐输入是可选快捷入口；过滤不能让原本有效的整组候选全部消失。
    return kept or suggestions[:1]


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


def _transition_source_repeats_previous(
    performance: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    """只比较换场来源回应，避免新场景文本掩盖上一轮对白复读。"""  # noqa: DOCSTRING_CJK

    segments = performance.get("segments")
    if not isinstance(segments, list) or not segments or not isinstance(segments[0], Mapping):
        return False
    source_response = segments[0]
    if not isinstance(source_response.get("performance"), str):
        return False
    return _is_repeated_performance(
        {"performance": source_response["performance"]},
        previous,
    )


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
            "如果玩家本轮已经为猫娘目标创造了条件，必须让猫娘现在完成对应的可见动作、说明或决定，不能再次承诺稍后再做；"
            "若玩家明确休息、离开或结束交谈，先收住交流，不再建议继续纠缠。不能替玩家完成行动，不能把 scene_complete 当成 true，也不能自行换节点。"
        )
    elif current_turn >= max(min_turns, recommended_turns - 1):
        phase = "focus"
        instruction = (
            "正在接近建议收束回合。直接回应玩家后，把互动聚焦到仍未发生的 pending_goals，避免开启无关新话题；"
            "玩家本轮已经满足猫娘行动前提时，猫娘要实际推进该目标，不能重复犹豫、重复询问或只说下一次再做；"
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
            _opening_anchor(
                projected.get("summary"),
                projected.get("catgirl_situation"),
            ),
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


_PLAYER_ACTION_SENTENCE = re.compile(
    r"(?:你|玩家|男主|哥哥).{0,16}(?:提议|决定|答应|同意|拒绝|坚持|选择|承诺|要求|"
    r"走向|进入|拿起|触碰|拥抱|离开|留下|巡视|调查|查看|询问|表示)"
)


def _opening_sentences(value: Any) -> list[str]:
    """按完整中文句子拆分节点摘要，避免把半句交给换场开场。"""  # noqa: DOCSTRING_CJK

    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in re.findall(r"[^。！？]+[。！？]?", text) if item.strip()]


def _opening_anchor(value: Any, fallback: Any = "") -> str:
    """优先选择不替玩家做决定的首个可观察场景句。"""  # noqa: DOCSTRING_CJK

    primary = _opening_sentences(value)
    secondary = _opening_sentences(fallback)
    for sentence in [*primary, *secondary]:
        if not _PLAYER_ACTION_SENTENCE.search(sentence):
            return sentence
    return primary[0] if primary else (secondary[0] if secondary else "")


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
        "pending_goals": projected.get("pending_goals") or [],
        "boundaries": projected.get("boundaries") or [],
        "instruction": "本回合只建立本节点的开场局势，不解决或演完整个节点。",
    }


def _transition_contract_for_actor(
    cast: NumericV2CastProjection,
    contract: Mapping[str, Any] | None,
    *,
    target_opening: str,
    target_goals: list[str],
) -> dict[str, Any]:
    """目标开场由 Runtime 交付，Actor 不再接收会造成复述的同项合同。"""  # noqa: DOCSTRING_CJK

    projected = cast.value(contract or {})
    target_references = [*_sentence_units(target_opening), *target_goals]
    must_deliver = []
    for item in projected.get("must_deliver") or []:
        text = str(item).strip()
        if text and not _text_is_covered(
            text,
            target_references,
            similarity=0.55,
            common_span=3,
        ):
            must_deliver.append(text)
    return {
        "reason": str(projected.get("reason") or ""),
        "must_deliver": must_deliver,
        "must_preserve": list(projected.get("must_preserve") or []),
        "tone": str(projected.get("tone") or ""),
        "instruction": (
            "这里只列出来源场景到目标开场之间独有的过渡事实。transition_bridge 只能收住来源现场并交付这些事实；"
            "不得执行 target_story_beat.pending_goals，不得自行补写目标时段、地点、物品或猫娘入场动作。"
        ),
    }


def _system_prompt(
    *,
    catgirl_name: str,
    player_address: str,
) -> str:
    return (
        "你是 N.E.K.O Numeric v2 的演绎 Actor。你扮演当前猫娘在本剧中的剧情身份，"
        "用自然的旁白、动作和对白回应玩家。承接玩家刚才的原话，不要忽略上下文。"
        f"{NUMERIC_V2_ACTOR_OUTPUT_SCHEMA_INSTRUCTION}"
        f"{NUMERIC_V2_ACTOR_NARRATION_BREVITY_INSTRUCTION}"
        "opening_phase 的 scene_narration 保持开场旁白样式且不受微动作字数限制；普通回合只能输出 performance、suggested_inputs，performance 必须同时含括号微动作和猫娘对白。"
        "route_changed 按 source_response、transition_bridge、target_opening 三段输出：先回应玩家，再交代必要过渡，最后只演猫娘入场；"
        "目标 opening_scene 由 Runtime 确定性交付，不要复写。"
        "performance 是一个完整字符串，只能用全角中文括号（……）包裹猫娘或环境的即时微动作；括号外全部是当前猫娘实际说出口的对白。"
        "动作与对白按实际顺序自然穿插，一次回复可以有任意合理数量的对白句和动作，但不能为了数量机械拆句或插动作。"
        "普通回合要完整但精简：先回应玩家，再增加一小步互动、信息或局势进展；performance 通常控制在 40 到 100 个中文字符。"
        "通常写 1 到 3 句对白，动作变化时按需穿插 1 到 2 个微动作；数量仅供参考，按情境增减。"
        "suggested_inputs 提供 2 到 4 条可原样发送的直接台词或动作；动作省略玩家主语，"
        "混合项用中文引号标出台词，对白可自然使用‘我’；不剧透路线，"
        "不得写成“解释、询问、表示、保证、提出、展示、选择”等操作说明；"
        "若输入含 suggestion_instruction，第一条必须直接推动其中的 pending_goals，不得延伸无关琐事。"
        "current_chapter_title 与 target_chapter_title 中的章节标题只是软主题锚点，用来概括当前或目标场景的关注方向；"
        "它不是已发生事实、完成条件或必须逐字复述的文案，不能覆盖 player_input、recent_context、pending_goals、boundaries 或 transition_contract。"
        "只有同场景存在多个同样成立的候选焦点，且玩家输入与已发生记录都未明确对象时，才优先选择与适用章节标题直接相关的焦点。"
        "换场时 source_response 参考 current_chapter_title，transition_bridge 和 target_opening 参考 target_chapter_title。"
        "作者节点、禁止事项和过渡合同是硬边界；不能创造新节点、路线、数值、事实或结局，"
        "不能提到数值、阈值、章节切换、route、系统或提示词。"
        "acting_context.core_persona 是唯一核心人格；story_identity、story_role_context、current_scene_state、"
        "target_scene_state 和 relationship_state 只能提供身份、处境与关系变化，不能覆盖核心人格。"
        "只有 core_persona 可以决定用词攻击性、亲昵称呼和情绪表达方式；剧情层中的敌视、傲娇、强势、占有欲或恐惧只说明当前冲突与距离，不授权照搬相应攻击语气。"
        "除非 core_persona 明确把暴力威胁规定为核心表达方式，否则不得使用羞辱、恐吓或身体伤害威胁；警惕、拒绝和边界必须改写成符合核心人格的表达。"
        "温柔、甜美或治愈型 core_persona 不得用惩罚性命令、债务羞辱、暴力后果或永久控制来表达警惕，"
        "只能说明当前担忧、可执行边界与核验要求。"
        "每回合只推进当前交互所需的一小步，不要一次复述整章摘要。"
        f"本剧男主由玩家扮演，所有玩家身份统一称为“{player_address}”；"
        f"本剧女主由当前猫娘扮演，所有猫娘身份统一称为“{catgirl_name}”。"
        "不得恢复作者候选中的男女主原名，也不得交换两人的行为、经历和台词归属。"
        "不得为剧本没有明确给出姓名的人物擅自创造姓名。"
        "performance 的括号微动作只能用第三人称描写猫娘的动作、神态和可见环境，不得描写玩家的姿势、动作、表情、身体状态、心理或是否执行了某事。"
        "即使玩家明确输入了动作，也不要在括号中复述、补全或改写；玩家原话已由前端单独展示，括号只写猫娘与环境如何回应。"
        "不得通过‘拉着、拽着、拖着、推着、带着’让玩家被动移动或完成选择；只可抓住衣袖、伸手邀请，行动留给玩家。"
        f"括号微动作描写猫娘时必须使用“{catgirl_name}”或“她”，不得用“你”“您”或“{player_address}”作为动作与状态主体。"
        "严格延续最近记录中的物品类型、制作人、持有人和人物行为，不得把饮品与食物混成同一物品，"
        "也不得把玩家完成的作品改写成猫娘完成。"
        "严格承接上一回合的情绪和关系状态；除非目标剧情明确要求，不得擅自宣布决裂、终止合作、离开或其他不可逆变化。"
        "即使玩家态度恶劣，也只能拒绝当前要求、表达受伤、保持距离或要求道歉；"
        "不得自行说‘这是最后一次合作’、‘以后不再见’或作出同义的永久关系决定。"
        "冲突发生后必须先回应并化解或延续冲突，不得下一回合无解释恢复亲密与合作。"
        "performance 中至少一句括号外对白必须直接回应玩家最新输入或紧接前一个括号动作的当下情境，不得突然追问、回答或引用画面中从未发生的言行。"
        "玩家说‘可以、如果、愿意、打算、改天’只是在表达条件、意愿或可能性；"
        "不得据此在旁白中替玩家转身、离开、靠近、触碰、站立或完成其他行动；也不得把猫娘上一回合说过的词归到玩家名下。"
        "recent_context 是唯一已经发生的演绎记录，其中 phase=opening 的开场与普通回合具有同等事实效力；"
        "任何 story_beat 的 pending_goals 都是尚未完成的目标，不能倒写成共同经历。"
        "当输入数据的 route_changed 为 true 时，必须先直接回应 player_input 并收住来源节点的当下互动，"
        "再自然桥接到目标节点和 transition_contract.must_deliver；不能用目标场景盖过玩家本轮要求。"
        "换场回合只建立目标节点开场，不得跳过时间过程或在同一回合解决目标节点的核心危机。"
        "transition_bridge 只负责收住来源现场并连接必要时间或地点，不得复述由 Runtime 确定性交付的目标开场环境。"
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
            "这是玩家输入前的公开开场。使用必要的环境或猫娘可见行动建立当下场景，再由猫娘主动说出第一句；"
            "不得假定玩家已经说话、做出选择或完成剧情摘要中的行动，不得使用‘你刚才说/做’或同义的隐形前史。"
            "若节点摘要包含玩家台词或主动行为，把它们视为后续可发展的剧情边界，不要在开场代替玩家执行。"
            "猫娘对白必须由本段旁白能够直接解释，随后提供第一组玩家建议；不要提前演完本节点。"
        ),
        "suggestion_contract": NUMERIC_V2_ACTOR_SUGGESTION_CONTRACT,
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
    soft_pacing = _soft_pacing(
        source,
        session.node_turn_count + 1,
        route_changed=route_changed,
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
        "soft_pacing": soft_pacing,
        "turn_instruction": (
            "先完成对玩家本轮原话的直接回应，再自然进入目标节点开场；不得跳过未解决的当前互动。"
            "如果 recent_context 已经演出来源节点的关键动作，source_response 只能简短确认，不能再表演一次同一动作；"
            "transition_bridge 只拥有来源场景收束和 transition_contract.must_deliver，不得执行 target_story_beat.pending_goals，"
            "不得复述 runtime_target_opening，也不得自行发明目标开场没有给出的具体时段、地点、物品或猫娘动作；"
            "target_opening.performance 才负责猫娘入场，同一物品交付或决定只能出现一次，目标环境由 Runtime 随后确定性交付。"
            if route_changed
            else "本回合留在当前节点，只推进当前互动所需的一小步。"
        ),
        "continuity_rule": "recent_context 是唯一已发生事实；节点目标不能覆盖或改写其中的时间、物品、行为与关系。",
    }
    if route_changed:
        target_beat = _beat_for_actor(cast, target["story_beat"])
        target_opening = target_beat["opening_scene"]
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
            "runtime_target_opening": truncate_prompt_value(
                target_opening,
                max_tokens=100,
            ),
            "transition_contract": truncate_prompt_value(
                _transition_contract_for_actor(
                    cast,
                    outcome.transition_contract,
                    target_opening=target_opening,
                    target_goals=list(target_beat.get("pending_goals") or []),
                ),
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
        "recent_suggestions": _recent_suggestions(session),
        "acting_context": _acting_context(
            engine,
            cast,
            source,
            outcome.session.metrics,
            character_profile,
            target=target if route_changed else None,
        ),
        "style_instruction": NUMERIC_V2_ACTOR_STYLE_INSTRUCTION,
        "response_instruction": (
            "recent_context 只用于承接事实；本轮必须先回应 player_input，不得复述上一轮来代替回应。"
            "普通回合只能推进 current_story_beat；换场记录中的来源幕已经结束，不得回到来源幕重新签约、交付、相识或重复其核心事件。"
            + (
                "本轮 route_changed=true；如果 recent_context 最后一轮已经完成来源幕目标，source_response 禁止复用其中任何完整动作或对白，"
                "只能用一句新的短回应承接当前 player_input，再进入过渡。"
                if route_changed
                else ""
            )
            + "recent_suggestions 只用于避重复，不能视为玩家已经发送；新推荐语不得只是近期推荐语的同义改写。"
            "玩家要求姓名、日期、编号、地点来历或完整故事时，只有 story_context、recent_context 和当前 story_beat 明确给出的具体值才可回答；"
            "资料只有‘某个名字’‘特定日期’等模糊描述时，必须明确尚不知道或看不清，不能补造姓名、经历、约定、气味或物品。"
        ),
        "suggestion_contract": NUMERIC_V2_ACTOR_SUGGESTION_CONTRACT,
        "player_input": current_player_input,
    }
    if not route_changed and soft_pacing["phase"] in {"focus", "closure"}:
        # 把推荐语的收束目标放在整个输入末尾，只引导玩家下一步，不替 Runtime 强制完成当前幕。
        response_tail["suggestion_instruction"] = {
            "pending_goals": data["current_story_beat"]["pending_goals"],
            "rule": (
                "performance 仍须先直接回应紧邻的 player_input；"
                "第一条 suggested_inputs 必须是玩家点击后可原样发送、并直接推动上述尚未完成目标的台词或即时动作；"
                "把目标改写成玩家此刻能直接说或做的内容，不得写成操作说明，也不得只延伸当前琐事；"
                "对照 recent_context 跳过已经发生的部分，直接触发目标中仍未出现的关键结果，不能只重复或推进复合目标的一小部分；"
                "如果目标需要猫娘许可、表态或决定，就让玩家直接请求该结果，并在台词中主动接受必要边界；"
                "如果剧本只给出期限、金额、编号或条款类别却没有具体值，推荐语必须让玩家主动提出一个方案，不能让玩家追问不存在的标准答案。"
            ),
        }
    # 事实保护必须始终位于输入末尾，避免被节奏或推荐语规则稀释。
    response_tail["factual_guard"] = (
        "最后核对：如果上述已发生事实和当前节点没有明确给出姓名、日期、编号、期限、金额、地点来历或完整经历，"
        "本轮必须直接说明暂时无法确认，禁止用看似合理的细节补空白；‘说明期限、权责或条款’这类类别名称本身不提供任何具体值。"
        "不得给已有物品擅自增加口味、材质、来源或归属。若互动必须确定缺失的具体值，只能请玩家提出方案，或承接玩家本轮明确提出的值。"
        "pending_goals 若用中文引号标出猫娘必须明确说出的内容，猫娘必须在括号外对白中完整说出引号内文字；"
        "玩家提议、猫娘的担忧或‘可以’‘就这么定’等含糊确认都不能代替这项对白交付。"
        "引号只约束对应目标实际交付时的措辞，不要求本轮一次说完全部目标；仍须服从 soft_pacing，normal 阶段每轮最多新交付一项 pending_goal。"
        "生成前逐项检查 recent_context：已经在猫娘对白中出现过的引号内容视为已交付，本轮不得重复，必须改为推进下一项尚未出现的 pending_goal。"
    )
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


def _parse_mixed_performance(value: Any, *, require_narration: bool) -> str:
    """校验模型的单字段混合正文；Session 只保存校验后的原始顺序。"""  # noqa: DOCSTRING_CJK

    if not isinstance(value, str) or not value.strip():
        raise NumericV2ActorOutputError("numeric_v2_actor_performance_invalid")
    text = value.strip()
    blocks = mixed_performance_blocks(text)
    if not blocks:
        raise NumericV2ActorOutputError("numeric_v2_actor_performance_invalid")
    _require_block_types(blocks, narration=require_narration, dialogue=True)
    return text


def _parse_scene_narration(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NumericV2ActorOutputError("numeric_v2_actor_scene_narration_invalid")
    return value.strip()


def _deduplicate_transition_bridge(
    value: Any,
    target_opening: str,
    target_goals: list[str] | None = None,
) -> str:
    """移除与 Runtime 目标开场重复的桥接句，保留来源场景的收束过程。"""  # noqa: DOCSTRING_CJK

    bridge = _parse_scene_narration(value)
    opening = str(target_opening or "").strip()
    if not opening:
        return bridge
    opening_units = _sentence_units(opening)
    goal_units = [str(item).strip() for item in target_goals or [] if str(item).strip()]
    kept = [
        unit
        for unit in _sentence_units(bridge)
        if not _text_is_covered(
            unit,
            opening_units,
            similarity=0.55,
            common_span=3,
            strong_common_span=5,
        )
        and not _shares_time_anchor(unit, opening_units)
        and not _text_is_covered(
            unit,
            goal_units,
            similarity=0.55,
            strong_common_span=3,
        )
    ]
    # 模型只复述目标开场时不制造系统式占位旁白；空桥段仍保留内部段位，前端直接进入确定性目标开场。
    return "".join(kept).strip()


def _suggestions(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise NumericV2ActorOutputError("numeric_v2_actor_collections_invalid")
    suggestions = []
    for item in value[:4]:
        item_text = _normalize_suggestion_quotes(str(item or "").strip())
        # 推荐语会被前端点击后直接发送；模型偶发生成的编辑指令不能进入玩家输入链路。
        is_indirect = (
            _INDIRECT_SUGGESTION_PREFIX_RE.search(item_text)
            or _INDIRECT_SUGGESTION_QUESTION_RE.search(item_text)
            or _INDIRECT_SUGGESTION_INTENT_RE.search(item_text)
        )
        if (
            0 < len(item_text) <= 120
            and not is_indirect
            and item_text not in suggestions
        ):
            suggestions.append(item_text)
    return suggestions


def _normalize_suggestion_quotes(value: str) -> str:
    """把模型常见的成对英文双引号规范成中文引号，避免选项视觉格式漂移。"""  # noqa: DOCSTRING_CJK

    if not value or value.count('"') == 0 or value.count('"') % 2:
        return value
    opening = True
    normalized: list[str] = []
    for char in value:
        if char != '"':
            normalized.append(char)
            continue
        normalized.append("“" if opening else "”")
        opening = not opening
    return "".join(normalized)


def _parse_output(
    content: Any,
    *,
    opening_required: bool = False,
    transition_required: bool = False,
    target_node_id: str = "",
    target_opening: str = "",
    target_goals: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise NumericV2ActorOutputError("numeric_v2_actor_empty_output")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise NumericV2ActorOutputError("numeric_v2_actor_invalid_json") from exc
    if not isinstance(payload, dict):
        raise NumericV2ActorOutputError("numeric_v2_actor_fields_invalid")
    if opening_required:
        if set(payload) != {"scene_narration", "performance", "suggested_inputs"}:
            raise NumericV2ActorOutputError("numeric_v2_actor_fields_invalid")
        return {
            "scene_narration": _parse_scene_narration(payload.get("scene_narration")),
            "performance": _parse_mixed_performance(
                payload.get("performance"),
                require_narration=False,
            ),
            "suggested_inputs": _suggestions(payload.get("suggested_inputs")),
        }
    if transition_required:
        if set(payload) != {"segments", "suggested_inputs"}:
            raise NumericV2ActorOutputError("numeric_v2_actor_transition_required")
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list) or len(raw_segments) != 3:
            raise NumericV2ActorOutputError("numeric_v2_actor_transition_segments_invalid")
        expected_phases = ("source_response", "transition_bridge", "target_opening")
        segments = []
        for index, raw_segment in enumerate(raw_segments):
            if not isinstance(raw_segment, Mapping):
                raise NumericV2ActorOutputError("numeric_v2_actor_transition_segments_invalid")
            # phase 是模型标签，不作为权限依据；固定位置和互斥字段形状共同确定真实段位。
            if not isinstance(raw_segment.get("phase"), str) or not raw_segment["phase"].strip():
                raise NumericV2ActorOutputError("numeric_v2_actor_transition_segments_invalid")
            if index == 0:
                if set(raw_segment) != {"phase", "performance"}:
                    raise NumericV2ActorOutputError("numeric_v2_actor_transition_segments_invalid")
                segments.append({
                    "phase": expected_phases[index],
                    "performance": _parse_mixed_performance(
                        raw_segment.get("performance"),
                        require_narration=False,
                    ),
                })
                continue
            if index == 1:
                if set(raw_segment) != {"phase", "scene_narration"}:
                    raise NumericV2ActorOutputError("numeric_v2_actor_transition_segments_invalid")
                segments.append({
                    "phase": expected_phases[index],
                    "scene_narration": _deduplicate_transition_bridge(
                        raw_segment.get("scene_narration"),
                        target_opening,
                        target_goals,
                    ),
                })
                continue
            # 兼容旧模型偶尔返回 scene_narration，但不采信模型生成的目标开场文本。
            if set(raw_segment) not in (
                {"phase", "performance"},
                {"phase", "scene_narration", "performance"},
            ):
                raise NumericV2ActorOutputError("numeric_v2_actor_transition_segments_invalid")
            segments.append({
                "phase": expected_phases[index],
                "performance": _parse_mixed_performance(
                    raw_segment.get("performance"),
                    require_narration=False,
                ),
            })
        return {
            "suggested_inputs": _suggestions(payload.get("suggested_inputs")),
            "segments": segments,
        }
    if set(payload) != {"performance", "suggested_inputs"}:
        raise NumericV2ActorOutputError("numeric_v2_actor_fields_invalid")
    suggestions = _suggestions(payload.get("suggested_inputs", []))
    return {
        "performance": _parse_mixed_performance(
            payload.get("performance"),
            require_narration=True,
        ),
        "suggested_inputs": suggestions,
    }


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
        ), opening_required=True)
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
        route_changed = (
            outcome.ledger_event["from_node_id"]
            != outcome.ledger_event["to_node_id"]
        )
        cast = NumericV2CastProjection.from_story(
            engine.story,
            player_name=player_address,
            catgirl_name=catgirl_name,
        )
        target_beat = _beat_for_actor(
            cast,
            engine.nodes[str(outcome.ledger_event["to_node_id"])]["story_beat"],
        )
        target_opening = target_beat["opening_scene"]
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
            transition_required=route_changed,
            target_node_id=str(outcome.ledger_event["to_node_id"]),
            target_opening=target_opening,
            target_goals=list(target_beat.get("pending_goals") or []),
        )
        performance["suggested_inputs"] = _deduplicate_recent_suggestions(
            list(performance.get("suggested_inputs") or []),
            _recent_suggestions(session),
        )
        if route_changed:
            performance = engine.finalize_transition_performance(
                outcome,
                performance,
                target_opening=target_opening,
            )
        if (
            session.performance_history
            and route_changed
            and _transition_source_repeats_previous(
                performance,
                session.performance_history[-1],
            )
        ):
            logger.warning(
                "Numeric v2 Actor failed: reason=numeric_v2_actor_repeated_transition_source session_id=%s revision=%s",
                session.session_id,
                session.revision,
            )
            raise NumericV2ActorOutputError("numeric_v2_actor_repeated_output")
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
        opening_required: bool = False,
        transition_required: bool = False,
        target_node_id: str = "",
        target_opening: str = "",
        target_goals: list[str] | None = None,
    ) -> dict[str, Any]:
        set_call_type("theater_numeric_v2_actor")
        started_at = time.monotonic()
        config_finished_at = started_at
        client_finished_at = started_at
        request_finished_at = started_at
        try:
            # 总时限覆盖配置读取、客户端构造、网络请求、输出解析和客户端关闭。
            async with asyncio.timeout(NUMERIC_V2_ACTOR_TIMEOUT_SECONDS):
                config = await _model_config(self.config_manager)
                config_finished_at = time.monotonic()
                client = await create_chat_llm_async(
                    str(config["model"]),
                    str(config["base_url"]),
                    config.get("api_key"),
                    provider_type=config.get("provider_type"),
                    timeout=NUMERIC_V2_ACTOR_TIMEOUT_SECONDS,
                    max_retries=0,
                    max_completion_tokens=NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS,
                )
                client_finished_at = time.monotonic()
                async with client:
                    request_messages = bound_prompt_messages(
                        messages,
                        max_tokens=NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS,
                        # 背景、身份和 performance 已由 Actor 专用装箱控制，不能再按单字段截断。
                        field_max_tokens=NUMERIC_V2_ACTOR_BOUND_FIELD_MAX_TOKENS,
                        # 推荐输入的直接表达规则和完整 JSON 合同都属于不可裁剪规则，总输入预算仍保持 4800 不变。
                        system_max_tokens=1900,
                    )
                    if [item.content for item in request_messages] != [item.content for item in messages]:
                        # 宁可让本轮失败并保留玩家输入，也不能把残缺身份或半句历史交给 Actor。
                        raise NumericV2ActorError("numeric_v2_actor_input_budget_exceeded")
                    response = await client.ainvoke(request_messages)
                    request_finished_at = time.monotonic()
                    parsed = _parse_output(
                        getattr(response, "content", None),
                        opening_required=opening_required,
                        transition_required=transition_required,
                        target_node_id=target_node_id,
                        target_opening=target_opening,
                        target_goals=target_goals,
                    )
        except asyncio.TimeoutError as exc:
            logger.warning(
                "Numeric v2 Actor failed: reason=numeric_v2_actor_timeout elapsed=%.3f",
                time.monotonic() - started_at,
            )
            raise NumericV2ActorError("numeric_v2_actor_timeout") from exc
        except NumericV2ActorError as exc:
            reason = str(exc) if str(exc).startswith("numeric_v2_actor_") else type(exc).__name__
            logger.warning("Numeric v2 Actor failed: reason=%s", reason)
            raise
        except Exception as exc:
            logger.warning("Numeric v2 Actor failed: reason=numeric_v2_actor_model_call_failed error_type=%s", type(exc).__name__)
            raise NumericV2ActorError("numeric_v2_actor_model_call_failed") from exc
        total_seconds = time.monotonic() - started_at
        if total_seconds >= NUMERIC_V2_ACTOR_SLOW_CALL_SECONDS:
            # 只记录阶段耗时，不记录剧本、玩家输入或模型输出。
            logger.warning(
                "Numeric v2 Actor slow call: total=%.3f config=%.3f client=%.3f request=%.3f finalize=%.3f",
                total_seconds,
                config_finished_at - started_at,
                client_finished_at - config_finished_at,
                request_finished_at - client_finished_at,
                time.monotonic() - request_finished_at,
            )
        return parsed


__all__ = [
    "NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS",
    "NUMERIC_V2_ACTOR_TIMEOUT_SECONDS",
    "NumericV2Actor",
    "NumericV2ActorError",
    "NumericV2ActorOutputError",
    "NumericV2ActorUnavailableError",
]
