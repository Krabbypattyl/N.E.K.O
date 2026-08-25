"""Numeric v2 的应用级回合工作流，不处理 HTTP 请求与响应映射。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from utils.character_memory import character_config_mutation_lock

from .numeric_v2_actor import NumericV2Actor
from .numeric_v2_evaluator import NumericV2MetricEvaluator
from .numeric_v2_runtime import NumericV2Runtime, TurnOutcomeV2, TurnRequestV2
from .numeric_v2_store import NumericV2StoredSession


@dataclass(frozen=True, slots=True)
class NumericV2TurnWorkflowResult:
    """回合模型调用和原子提交完成后交还给接口层的公开工作结果。"""  # noqa: DOCSTRING_CJK

    stored: NumericV2StoredSession
    outcome: TurnOutcomeV2
    performance: dict[str, Any]
    display_binding: Mapping[str, str]


async def execute_numeric_v2_turn(
    *,
    config_manager: Any,
    runtime: NumericV2Runtime,
    current: NumericV2StoredSession,
    turn: TurnRequestV2,
    ensure_current_binding: Callable[[Any], Mapping[str, str]],
    before_commit: Callable[[], Awaitable[None]] | None = None,
) -> NumericV2TurnWorkflowResult:
    """固定执行 Evaluator、Runtime、Actor、身份复验和原子提交，不增加任何模型步骤。"""  # noqa: DOCSTRING_CJK

    evaluation = await NumericV2MetricEvaluator(config_manager).evaluate(
        engine=runtime.engine,
        session=current.session,
        message=turn.message,
        recent_ledger_events=current.ledger_events,
    )
    outcome = runtime.prepare_turn(
        current,
        turn,
        evaluation.metric_changes,
        scene_complete=evaluation.scene_complete,
        goal_evidence=evaluation.goal_evidence,
    )
    performance = await NumericV2Actor(config_manager).generate_turn(
        engine=runtime.engine,
        session=current.session,
        outcome=outcome,
        player_input=turn.message,
    )
    # 模型调用不占生命周期锁；仅将身份复验、展示刷新和原子提交与角色改名串行。
    async with character_config_mutation_lock:
        # 模型调用期间角色卡可能切换；成功输出不能提交到另一只猫娘的恢复槽位。
        display_binding = ensure_current_binding(current.session)
        refreshed_binding = {
            str(key): str(value)
            for key, value in display_binding.items()
        }
        # 本轮 Ledger 已按模型调用前的称呼事实计算；只刷新角色展示字段，避免称呼并发变化破坏重放。
        refreshed_binding["player_address"] = str(
            outcome.session.catgirl_binding.get("player_address") or ""
        )
        outcome = replace(
            outcome,
            session=replace(
                outcome.session,
                # 不可变角色 ID 已通过校验；提交前刷新名称和人格版本，避免并发改名被旧候选覆盖。
                catgirl_binding=refreshed_binding,
            ),
        )
        # 角色锁始终先于故事锁，保持与归档、遗忘链路一致的锁顺序。
        async with runtime.story_session_guard():
            if before_commit is not None:
                # 长耗时模型调用结束后再次检查云存档写栅栏，避免请求期间进入维护态仍然提交。
                await before_commit()
            stored = await runtime.commit_turn(outcome, performance)
    return NumericV2TurnWorkflowResult(
        stored=stored,
        outcome=outcome,
        performance=performance,
        display_binding=display_binding,
    )


__all__ = ["NumericV2TurnWorkflowResult", "execute_numeric_v2_turn"]
