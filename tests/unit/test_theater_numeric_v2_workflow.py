"""验证小剧场工作流的状态合并和降级边界。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from services.theater.numeric_v2_actor import (
    NumericV2ActorOutputError,
)
from services.theater.numeric_v2_evaluator import NumericV2TransitionOfferReview
from services.theater.numeric_v2_workflow import (
    _generate_actor_turn_with_output_retry,
    _merge_transition_offered,
    _transition_review_failure_context,
    _transition_boundary_repair_context,
)


def test_unclear_runtime_proposal_is_not_cleared_by_actor_false() -> None:
    """Runtime 保留待确认提议时，Actor 没有新提议不能把它清掉。"""

    assert _merge_transition_offered(True, False) is True


def test_actor_can_create_a_new_transition_proposal() -> None:
    """没有旧提议时，合法 Actor true 仍可创建新的可见提议。"""

    assert _merge_transition_offered(False, True) is True


def test_semantic_review_can_recover_an_unflagged_visible_proposal() -> None:
    """正文与推荐已经公开合法提议时，Actor 漏写布尔值不能让玩家接受后死锁。"""

    assert _merge_transition_offered(False, False, True) is True


def test_invalid_actor_transition_value_is_treated_as_no_new_proposal() -> None:
    """旧模型返回非布尔值时不猜测状态。"""

    assert _merge_transition_offered(False, "true") is False


def test_transition_boundary_repair_receives_bridge_and_target_opening() -> None:
    """边界改写必须知道作者桥段和下一幕开场，但只把它们当作停止边界。"""

    engine = SimpleNamespace(
        nodes={
            "current": {"story_beat": {"summary": "当前幕先完成控制台同步。"}},
            "target": {"story_beat": {"opening_scene": "下一幕的警报已经响起。"}},
        },
        preview_route=lambda _node_id, _metrics: {
            "target_node_id": "target",
            "transition_contract": {
                "bridge_scene_narration": "舱门在玩家确认后关闭。",
            },
        },
    )
    session = SimpleNamespace(current_node_id="current", metrics={})

    context = _transition_boundary_repair_context(
        SimpleNamespace(engine=engine),
        SimpleNamespace(session=session),
    )

    assert "只定义停止边界" in context
    assert "仍可在当前幕交付的作者方向" in context
    assert "保留玩家本轮已经实施的当前幕行动及其直接结果" in context
    assert "舱门在玩家确认后关闭" in context
    assert "下一幕的警报已经响起" in context


def test_transition_boundary_retry_receives_specific_failure_reason() -> None:
    """边界改写应携带具体冲突，同时明确它不是可新增的剧情事实。"""

    context = _transition_review_failure_context(
        NumericV2TransitionOfferReview(
            offer_present=False,
            valid=False,
            player_action_preserved=True,
            scene_boundary_preserved=True,
            author_boundaries_preserved=False,
            failure_reason=(
                "上一版声称保护罩能隔绝热信号，但当前幕只确认保护罩可以短时展开。"
            ),
        )
    )

    assert "保护罩能隔绝热信号" in context
    assert "只用于定位并删除上一版问题" in context
    assert "不是剧情事实" in context


@pytest.mark.asyncio
async def test_actor_output_retry_changes_hint_for_each_attempt() -> None:
    """重复正文连续失败时，每次重试都必须收到不同的改写角度。"""

    class RetryActor:
        def __init__(self) -> None:
            self.hints: list[str] = []

        async def generate_turn(self, **kwargs):
            self.hints.append(str(kwargs.get("retry_hint") or ""))
            if len(self.hints) < 4:
                raise NumericV2ActorOutputError("numeric_v2_actor_repeated_output")
            return {"performance": "（抬眼）这次回应加入了新的动作。"}

    actor = RetryActor()
    outcome = SimpleNamespace(
        ledger_event={"from_node_id": "start", "to_node_id": "start"},
    )

    result = await _generate_actor_turn_with_output_retry(
        actor,
        outcome=outcome,
        session=SimpleNamespace(session_id="retry-test", revision=2),
    )

    assert result["performance"] == "（抬眼）这次回应加入了新的动作。"
    assert actor.hints[0] == ""
    assert len(set(actor.hints[1:])) == 3
    assert "第二次重复输出重试" in actor.hints[2]
    assert "最后一次重复输出重试" in actor.hints[3]


@pytest.mark.asyncio
async def test_actor_output_retry_preserves_required_boundary_rewrite() -> None:
    """边界改写稿格式失败后，后续重试不能丢掉原始边界要求。"""

    class RetryActor:
        def __init__(self) -> None:
            self.hints: list[str] = []

        async def generate_turn(self, **kwargs):
            self.hints.append(str(kwargs.get("retry_hint") or ""))
            if len(self.hints) == 1:
                raise NumericV2ActorOutputError("numeric_v2_actor_repeated_output")
            return {"performance": "（撑住门）要继续穿过去吗？"}

    actor = RetryActor()
    outcome = SimpleNamespace(
        ledger_event={"from_node_id": "start", "to_node_id": "start"},
    )

    await _generate_actor_turn_with_output_retry(
        actor,
        outcome=outcome,
        session=SimpleNamespace(session_id="boundary-retry", revision=2),
        retry_hint="必须停在门槛前等待玩家确认。",
    )

    assert actor.hints[0] == "必须停在门槛前等待玩家确认。"
    assert "必须停在门槛前等待玩家确认。" in actor.hints[1]
    assert "上一版与较早回合" in actor.hints[1]
