"""校验小剧场各模型职责的结构化输出与稳定降级对象。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from typing import Any

from utils.file_utils import robust_json_loads


# 回应焦点只描述本轮应先承接的公开语义，不承担路由或事实提交权。
THEATER_RESPONSE_FOCUS_TYPES = frozenset(
    {"question", "object", "action", "attitude"}
)
THEATER_RESPONSE_FOCUS_EVIDENCE_MAX_CHARS = 240
_FORBIDDEN_OUTPUT_TERMS = (
    "scene_id",
    "node_id",
    "choice_id",
    "goal_id",
    "fact_type",
    "fact_role",
    "fact_object",
    "content_id",
    "content_slot_id",
    "beat_id",
    "branch_id",
    "ending_domain_id",
    "turns_used",
    "nonprogress_turns",
    "turn_delivery",
    "回合预算",
)


def _parse_route_output(
    raw: Any,
    *,
    allowed_choice_ids: set[str],
    allowed_intent_ids: set[str],
    user_message: str = "",
) -> dict[str, Any] | None:
    """只接受作者白名单入口或保守停留。"""  # noqa: DOCSTRING_CJK
    try:
        payload = _load_unique_model_json_object(raw)
    except Exception:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "route_kind",
        "matched_choice_id",
        "authored_intent_id",
        "response_focus",
    }:
        return None
    route_kind = str(payload.get("route_kind") or "").strip()
    candidate_choice = str(payload.get("matched_choice_id") or "").strip()
    candidate_intent = str(payload.get("authored_intent_id") or "").strip()
    response_focus = verify_response_focus(
        payload.get("response_focus"),
        user_message=user_message,
    )
    if route_kind == "authored_choice":
        if candidate_choice not in allowed_choice_ids or candidate_intent:
            return None
        return {
            "route_kind": route_kind,
            "matched_choice_id": candidate_choice,
            "authored_intent_id": "",
            "response_focus": response_focus,
        }
    if route_kind == "authored_intent":
        if candidate_intent not in allowed_intent_ids or candidate_choice:
            return None
        return {
            "route_kind": route_kind,
            "matched_choice_id": "",
            "authored_intent_id": candidate_intent,
            "response_focus": response_focus,
        }
    if route_kind != "stay" or candidate_choice or candidate_intent:
        return None
    return {
        "route_kind": "stay",
        "matched_choice_id": "",
        "authored_intent_id": "",
        "response_focus": response_focus,
    }


def verify_response_focus(value: Any, *, user_message: Any) -> dict[str, Any]:
    """只接受能由本轮完整玩家原话证明的有界回应焦点。"""  # noqa: DOCSTRING_CJK
    if not isinstance(value, dict) or set(value) != {
        "focus_type",
        "evidence_excerpt",
        "requires_state_change",
    }:
        return {}
    focus_type = str(value.get("focus_type") or "").strip()
    evidence_excerpt = " ".join(
        str(value.get("evidence_excerpt") or "").strip().split()
    )
    normalized_message = " ".join(str(user_message or "").strip().split())
    requires_state_change = value.get("requires_state_change")
    if (
        focus_type not in THEATER_RESPONSE_FOCUS_TYPES
        or not 1
        <= len(evidence_excerpt)
        <= THEATER_RESPONSE_FOCUS_EVIDENCE_MAX_CHARS
        or evidence_excerpt not in normalized_message
        or not isinstance(requires_state_change, bool)
    ):
        return {}
    return {
        "focus_type": focus_type,
        "evidence_excerpt": evidence_excerpt,
        "requires_state_change": requires_state_change,
    }


def _empty_route_result() -> dict[str, Any]:
    """返回新的保守停留结果。"""  # noqa: DOCSTRING_CJK
    return {
        "route_kind": "stay",
        "matched_choice_id": "",
        "authored_intent_id": "",
        "response_focus": {},
    }


def _technical_route_fallback() -> dict[str, Any]:
    """标记 Router 基础设施降级，避免调用方把技术故障误算成玩家语义 idle。"""  # noqa: DOCSTRING_CJK
    result = _empty_route_result()
    # 该字段不属于模型合同，只能由服务端失败路径生成，并在回合事务内消费。
    result["route_delivery"] = "technical_degraded"
    return result


def _parse_output(
    raw: Any,
    *,
    progress_kind: str,
) -> dict[str, Any] | None:
    """解析演绎模型 JSON；该阶段不再拥有任何剧情路由字段。"""  # noqa: DOCSTRING_CJK
    try:
        payload = _load_unique_model_json_object(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    narration = str(payload.get("narration") or "").strip()
    dialogue = str(payload.get("dialogue") or "").strip()
    combined = narration + dialogue
    if not dialogue or any(
        term.lower() in combined.lower() for term in _FORBIDDEN_OUTPUT_TERMS
    ):
        return None
    if progress_kind != "roleplay_response" and not narration:
        return None
    return {
        "narration": narration,
        "dialogue": dialogue,
        # 字段仅保留旧模型 JSON 形状兼容；静态 Choice 文案始终由 Story 作者控制。
        "choice_rewrites": [],
    }


def _load_unique_model_json_object(raw: Any) -> dict[str, Any]:
    """读取唯一 JSON 对象；允许外围说明，但拒绝零个或多个竞争对象。"""  # noqa: DOCSTRING_CJK
    text = str(raw or "").strip()
    if text.startswith("```"):
        # 标准 JSON 围栏仍走最快路径；非标准围栏会由下方唯一对象扫描处理。
        text = text.strip("`").removeprefix("json").strip()
    try:
        direct = robust_json_loads(text)
    except Exception:
        direct = None
    if isinstance(direct, dict):
        return direct

    candidates: list[dict[str, Any]] = []
    for fragment in _balanced_json_object_fragments(text):
        try:
            candidate = robust_json_loads(fragment)
        except Exception:
            continue
        if isinstance(candidate, dict):
            candidates.append(candidate)
    if len(candidates) != 1:
        # 多对象输出可能表达互相竞争的路由或事实，不能静默挑第一个。
        raise ValueError("model output must contain exactly one JSON object")
    return candidates[0]


def _balanced_json_object_fragments(text: str) -> list[str]:
    """在不查看字段内容语义的前提下扫描字符串外的平衡花括号片段。"""  # noqa: DOCSTRING_CJK
    fragments: list[str] = []
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(str(text or "")):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                fragments.append(text[start : index + 1])
                start = -1
    return fragments
