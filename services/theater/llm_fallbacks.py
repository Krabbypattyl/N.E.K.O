"""提供不提交权威状态的小剧场确定性安全回退。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import re
from typing import Any

from .llm_response_contracts import _FORBIDDEN_OUTPUT_TERMS


def _authored_performance_fallback(
    fallback: dict[str, Any],
    node: dict[str, Any],
    progress_kind: str,
) -> dict[str, Any]:
    """纠错失败时恢复作者台词，避免场景笔记把必要的剧情交接替换成泛化回应。"""  # noqa: DOCSTRING_CJK
    if progress_kind not in {"opening", "graph_progress"}:
        return fallback
    author_fallback = dict(fallback)
    scripted_dialogue = str(node.get("scripted_dialogue") or "").strip()
    if scripted_dialogue:
        author_fallback["dialogue"] = scripted_dialogue
    return author_fallback


def _bounded_public_fallback_anchor(value: Any, *, max_chars: int = 96) -> str:
    """清洗公开短锚点；只做边界保护，不按题材、情绪或关键词推断剧情。"""  # noqa: DOCSTRING_CJK
    normalized = " ".join(str(value or "").strip().split())
    if not normalized:
        return ""
    lowered = normalized.lower()
    if any(term.lower() in lowered for term in _FORBIDDEN_OUTPUT_TERMS):
        return ""
    # 公开回退不应复述看起来像服务端稳定引用的值，即使它来自被篡改的 Story 文本。
    if re.search(r"(?i)\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", normalized):
        return ""
    return normalized[:max_chars]


def _fallback_scene_prefix(
    scene: dict[str, Any] | None = None, *, scene_title: str = ""
) -> str:
    """把当前公开 Scene 标题转成确定性对白前缀；没有安全标题时返回空串。"""  # noqa: DOCSTRING_CJK
    title = _bounded_public_fallback_anchor(
        scene_title or (scene.get("title") if isinstance(scene, dict) else ""),
        max_chars=48,
    )
    return f"我们还在「{title}」这里。" if title else ""


def fallback_turn(
    *,
    lanlan_name: str,
    scene: dict[str, Any],
    node: dict[str, Any],
    user_message: str,
    progress_kind: str,
    callback: str,
    has_scene_notes: bool = False,
    recent_turns: list[dict[str, Any]] | None = None,
    choice_options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """使用作者文本生成离线演绎，确保模型故障时仍能继续游戏。"""  # noqa: DOCSTRING_CJK
    name = str(lanlan_name or "Lan").strip() or "Lan"
    if progress_kind == "roleplay_response":
        message = str(user_message or "").strip()
        if not message:
            dialogue = f"{name}还在这里喵。"
        else:
            scene_prefix = _fallback_scene_prefix(scene)
            if choice_options:
                continuity = "眼前的下一步还没有替你决定；"
            elif recent_turns or has_scene_notes:
                continuity = "我没有忘记我们刚才说到哪里；"
            else:
                continuity = "我听见了；"
            dialogue = scene_prefix + continuity + "先让我理清楚，再好好回应你喵。"
        # 模型不可用时不得猜测玩家是否完成 Choice；保守停留是自然语言推进的安全底线。
        return {
            "narration": "",
            "dialogue": dialogue,
            "choice_rewrites": [],
        }
    narration = str(callback or node.get("summary") or scene.get("text") or "").strip()
    # scripted_dialogue 是作者可播放正文；runtime_generation_guide 只是内部约束，
    # 即使模型故障也不能把第三人称演绎指令或框架固定口癖冒充角色台词。
    dialogue = str(node.get("scripted_dialogue") or "").strip()
    return {
        "narration": narration,
        "dialogue": dialogue,
        "choice_rewrites": [],
    }
