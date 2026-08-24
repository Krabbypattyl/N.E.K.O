"""Numeric v2 Actor 的纯文本比较与输出合同校验。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
from typing import Any, Mapping

from .numeric_v2_performance import mixed_performance_blocks


# 推荐输入会被前端原样发送，因此只允许直接台词或即时动作，不接受编辑说明。
_INDIRECT_SUGGESTION_PREFIX_RE = re.compile(
    r"^(?:请)?(?:解释|询问|表示|保证|提出|展示|选择|注意到|观察|尝试|请求|说明)(?:自己|对方|她|他)?"
)
_INDIRECT_SUGGESTION_QUESTION_RE = re.compile(
    r"(?:^|[，,；;。])(?:再|然后|接着)?(?:询问|问)(?:她|他|对方|我)?"
)
_INDIRECT_SUGGESTION_INTENT_RE = re.compile(
    r"^(?![“\"])[^。！？!?]{1,24}(?:表示|解释|说明)"
    r"(?:自己|没有|来意|原因|情况|身份|意图|想法|立场|诚意|[“\"])"
)
# 括号动作和模板占位是模型编辑格式，不是玩家点击后可原样发送的自然输入。
_PARENTHESIZED_SUGGESTION_RE = re.compile(r"^[（(]")
_PLACEHOLDER_SUGGESTION_RE = re.compile(r"\[[^\[\]]+\]|\{[^{}]+\}|<[^<>]+>")
_UNKNOWN_NAME_DISCLOSURE_RE = re.compile(r"(?:我叫|叫我|称呼我)")


class NumericV2ActorError(RuntimeError):
    """Actor 无法提供可提交的演绎正文。"""  # noqa: DOCSTRING_CJK


class NumericV2ActorUnavailableError(NumericV2ActorError):
    """Actor 的模型配置当前不可用。"""  # noqa: DOCSTRING_CJK


class NumericV2ActorOutputError(NumericV2ActorError):
    """Actor 返回内容没有通过确定性输出合同。"""  # noqa: DOCSTRING_CJK


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


def _require_block_types(
    blocks: list[dict[str, str]],
    *,
    narration: bool = False,
    dialogue: bool = False,
) -> None:
    """校验混合正文是否包含当前阶段要求的动作与对白。"""  # noqa: DOCSTRING_CJK

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
    """场景旁白必须是非空字符串，空桥段只允许在去重完成后形成。"""  # noqa: DOCSTRING_CJK

    if not isinstance(value, str) or not value.strip():
        raise NumericV2ActorOutputError("numeric_v2_actor_scene_narration_invalid")
    return value.strip()


def _goal_texts(target_goals: list[Any] | None) -> list[str]:
    """结构化目标只取描述参与桥段去重，内部 owner 和 evidence 不能变成剧情正文。"""  # noqa: DOCSTRING_CJK

    return [
        str(item.get("text") or "").strip() if isinstance(item, Mapping) else str(item or "").strip()
        for item in target_goals or []
        if (str(item.get("text") or "").strip() if isinstance(item, Mapping) else str(item or "").strip())
    ]


def _deduplicate_transition_bridge(
    value: Any,
    target_opening: str,
    target_goals: list[Any] | None = None,
) -> str:
    """移除与 Runtime 目标开场重复的桥接句，保留来源场景的收束过程。"""  # noqa: DOCSTRING_CJK

    bridge = _parse_scene_narration(value)
    opening = str(target_opening or "").strip()
    if not opening:
        return bridge
    opening_units = _sentence_units(opening)
    goal_units = _goal_texts(target_goals)
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
        and not _text_is_covered(
            unit,
            goal_units,
            similarity=0.55,
            strong_common_span=3,
        )
    ]
    # 模型只复述目标开场时不制造系统式占位旁白；前端会直接进入确定性目标开场。
    return "".join(kept).strip()


def _normalize_suggestion_quotes(value: str) -> str:
    """把成对英文双引号规范成中文引号，避免推荐输入视觉格式漂移。"""  # noqa: DOCSTRING_CJK

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


def _suggestions(
    value: Any,
    *,
    allow_questions: bool = True,
    allow_name_disclosure: bool = True,
) -> list[str]:
    """只保留可由玩家点击后直接发送的推荐输入。"""  # noqa: DOCSTRING_CJK

    if not isinstance(value, list):
        raise NumericV2ActorOutputError("numeric_v2_actor_collections_invalid")
    suggestions = []
    for item in value[:4]:
        item_text = _normalize_suggestion_quotes(str(item or "").strip())
        is_indirect = (
            _INDIRECT_SUGGESTION_PREFIX_RE.search(item_text)
            or _INDIRECT_SUGGESTION_QUESTION_RE.search(item_text)
            or _INDIRECT_SUGGESTION_INTENT_RE.search(item_text)
            or _PARENTHESIZED_SUGGESTION_RE.search(item_text)
            or _PLACEHOLDER_SUGGESTION_RE.search(item_text)
        )
        if (
            0 < len(item_text) <= 120
            and not is_indirect
            # 结构化开机空白态下，知道现场背景的是男主；问句留给猫娘正文，避免双方职责倒置。
            and (allow_questions or not any(mark in item_text for mark in ("?", "？")))
            # 未知阶段 Actor 看不到真实配置昵称，也不能用任意新名字替玩家完成自我介绍。
            and (allow_name_disclosure or not _UNKNOWN_NAME_DISCLOSURE_RE.search(item_text))
            and item_text not in suggestions
        ):
            suggestions.append(item_text)
    return suggestions


def _parse_output(
    content: Any,
    *,
    opening_required: bool = False,
    transition_required: bool = False,
    suggestion_questions_allowed: bool = True,
    suggestion_name_disclosure_allowed: bool = True,
    target_node_id: str = "",
    target_opening: str = "",
    target_goals: list[Any] | None = None,
) -> dict[str, Any]:
    """把单次 Actor 返回解析为唯一合法的开场、普通回合或换场形状。"""  # noqa: DOCSTRING_CJK

    # target_node_id 保留在兼容签名中；可见节点仍由 Runtime 提交阶段校验，解析器不采信模型标签。
    _ = target_node_id
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
            "suggested_inputs": _suggestions(
                payload.get("suggested_inputs"),
                allow_questions=suggestion_questions_allowed,
                allow_name_disclosure=suggestion_name_disclosure_allowed,
            ),
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
            "suggested_inputs": _suggestions(
                payload.get("suggested_inputs"),
                allow_questions=suggestion_questions_allowed,
                allow_name_disclosure=suggestion_name_disclosure_allowed,
            ),
            "segments": segments,
        }
    if set(payload) != {"performance", "suggested_inputs"}:
        raise NumericV2ActorOutputError("numeric_v2_actor_fields_invalid")
    return {
        "performance": _parse_mixed_performance(
            payload.get("performance"),
            require_narration=True,
        ),
        "suggested_inputs": _suggestions(
            payload.get("suggested_inputs", []),
            allow_questions=suggestion_questions_allowed,
            allow_name_disclosure=suggestion_name_disclosure_allowed,
        ),
    }


__all__ = [
    "NumericV2ActorError",
    "NumericV2ActorOutputError",
    "NumericV2ActorUnavailableError",
]
