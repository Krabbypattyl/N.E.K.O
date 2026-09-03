"""Numeric v2 的共享叙事上下文投影工具。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from typing import Any, Mapping

from utils.tokenize import truncate_to_tokens


def current_scene_records(
    session: Any,
) -> tuple[list[Mapping[str, Any]], bool]:
    """返回最近一次进入当前节点后的完整回合记录，并标记是否包含入幕记录。

    Actor 与 Evaluator 必须基于同一段当前幕历史工作；历史记录的格式化方式可以不同，
    但不能再各自实现一套节点回溯规则，避免一个模块看到上一幕残留而另一个模块看不到。
    """  # noqa: DOCSTRING_CJK

    performance_history = tuple(getattr(session, "performance_history", ()) or ())
    if not performance_history:
        return [], False

    current_node_id = str(getattr(session, "current_node_id", "") or "")
    visit_records: list[Mapping[str, Any]] = []
    entered_current_node = False
    for record in reversed(performance_history):
        if not isinstance(record, Mapping):
            continue
        from_node_id = str(record.get("from_node_id") or "")
        to_node_id = str(record.get("to_node_id") or "")
        if from_node_id == current_node_id and to_node_id == current_node_id:
            visit_records.append(record)
            continue
        if to_node_id == current_node_id and from_node_id != current_node_id:
            visit_records.append(record)
            entered_current_node = True
        # 遇到最近一次进入当前节点的边界后，不再把更早场景混入当前幕。
        break
    return visit_records, entered_current_node


def scene_narrative_focus(beat: Mapping[str, Any]) -> str:
    """提取一条非任务化叙事重心，供两个模型共享，不参与完成判定。

    新剧本可以显式提供 narrative_focus；旧剧本优先使用作者给出的当前推进方向，
    再回退到自然叙事摘要，最后才使用开场处境。这样不会把初始画面反复误当成每回合
    都要继续观察的重点。该值只帮助模型选择当前因果线，不能被 Runtime 当作目标或门槛。
    """  # noqa: DOCSTRING_CJK

    # 旧包没有 narrative_focus 时，transition_goal 比 opening_scene 更能表达“接下来
    # 如何自然推进”；summary 仍只作叙事方向，opening_scene 仅作为最后兼容回退。
    for key in ("narrative_focus", "transition_goal", "narrative_summary", "summary", "opening_scene"):
        value = str(beat.get(key) or "").strip()
        if value:
            return value
    return ""


def scene_narrative_summary(beat: Mapping[str, Any]) -> str:
    """投影普通 Actor 的当前处境，避免把整幕动作清单当成待办事项。

    opening_scene 负责交付开场可观察事实，当前幕后续进展由 story_so_far 承接；
    只有剧本尚未提供开场字段时才回退到 summary。这样不会丢失作者事实，
    但能阻止模型在每轮重新扫描整幕计划并逐项执行。
    """

    for key in ("narrative_summary", "opening_scene", "summary"):
        value = str(beat.get(key) or "").strip()
        if value:
            return value
    return ""


def pending_transition_performance(
    session: Any,
    *,
    max_tokens: int = 180,
) -> str:
    """返回当前幕最近一次已提交的可见转场提议，供 Actor 和判定器复用。

    转场提议属于已经展示给玩家的正文事实；把它从长历史中单独投影出来，
    只是在上下文中提升可见性，不新增独立状态，也不依据关键词猜测玩家意图。
    """

    if not bool(getattr(session, "transition_offered", False)):
        return ""
    current_node_id = str(getattr(session, "current_node_id", "") or "")
    for record in reversed(tuple(getattr(session, "performance_history", ()) or ())):
        if not isinstance(record, Mapping):
            continue
        if (
            record.get("transition_offered") is True
            and str(record.get("to_node_id") or "") == current_node_id
        ):
            performance = str(record.get("performance") or "").strip()
            if performance:
                return truncate_to_tokens(performance, max_tokens=max_tokens)
    return ""


__all__ = [
    "current_scene_records",
    "pending_transition_performance",
    "scene_narrative_focus",
    "scene_narrative_summary",
]
