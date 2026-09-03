"""Numeric v2 的应用级回合工作流，不处理 HTTP 请求与响应映射。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, replace
import logging
import time
from typing import Any, Callable, Mapping

from utils.character_memory import character_config_mutation_lock

from .numeric_v2_actor import (
    NumericV2Actor,
    NumericV2ActorOutputError,
)
from .numeric_v2_evaluator import (
    NumericV2EvaluationResult,
    NumericV2EvaluatorError,
    NumericV2MetricEvaluator,
    NumericV2TransitionOfferReview,
)
from .numeric_v2_runtime import NumericV2Runtime, TurnOutcomeV2, TurnRequestV2
from .numeric_v2_store import NumericV2StoredSession


logger = logging.getLogger(__name__)


def _output_retry_hint(
    *,
    last_error_code: str,
    retry_number: int,
    route_changed: bool,
) -> str:
    """为每一次正文重试提供不同的改写角度，避免模型沿用同一采样路径。"""

    if route_changed:
        if retry_number == 1:
            return (
                "这是玩家已接受后的正式换场重试。请先用全新的简短来源回应承接本轮接受动作，"
                "再写新的过渡桥段；不要复用上一幕或上一版的来源对白、动作和收尾。"
            )
        if retry_number == 2:
            return (
                "这是第二次正式换场重试。请改用不同的来源动作和对白回应玩家本轮行动，"
                "重新组织过渡桥段并引入一个当前事实支持的变化；目标开场只需自然接入，"
                "不要复述上一版内容。"
            )
        return (
            "这是最后一次正式换场重试。请用最简短的全新来源动作与对白完成承接，"
            "保留必要的过渡因果但完全改写句式和收尾；不要复制任何较早回合的正文。"
        )

    if "repeated" in last_error_code:
        if retry_number == 1:
            return (
                "上一版与较早回合的完整对白或收尾重复。请基于玩家本轮输入引入新的可见事实或行动，"
                "完全改写动作、对白和收尾，不要只替换形容词。"
            )
        if retry_number == 2:
            return (
                "这是第二次重复输出重试。请换一个新的动作切入点，先回应玩家本轮输入，"
                "再推进当前叙事重心；不得复用上一版的开头、核心句或结尾。"
            )
        return (
            "这是最后一次重复输出重试。请输出一段更短但全新的动作与对白，"
            "至少改变回应角度和可见动作，并避免与历史任何一轮形成近似复述。"
        )

    if route_changed:
        return (
            "这是正式换场重试，请完全改写上一版的来源回应和过渡桥段，"
            "优先回应玩家本轮输入并推进当前叙事重心；不要复用上一版的句式、动作或收尾。"
        )
    return (
        "请完全改写上一版正文，优先回应玩家本轮输入并推进当前叙事重心；"
        "不要复用上一版的句式、动作或收尾。"
    )


def _merge_transition_offered(
    runtime_value: Any,
    actor_value: Any,
    reviewed_value: Any = False,
) -> bool:
    """合并 Runtime 生命周期、Actor 声明与语义复核出的可见提议。"""  # noqa: DOCSTRING_CJK

    runtime_offered = runtime_value if isinstance(runtime_value, bool) else False
    actor_offered = actor_value if isinstance(actor_value, bool) else False
    reviewed_offered = reviewed_value if isinstance(reviewed_value, bool) else False
    return runtime_offered or actor_offered or reviewed_offered


def _transition_boundary_repair_context(
    runtime: NumericV2Runtime,
    current: NumericV2StoredSession,
) -> str:
    """只在边界改写时提供作者桥段与下一幕开场，明确应停止的画面。"""  # noqa: DOCSTRING_CJK

    session = current.session
    node = runtime.engine.nodes.get(session.current_node_id)
    if not isinstance(node, Mapping):
        return ""
    route = runtime.engine.preview_route(session.current_node_id, session.metrics)
    if not isinstance(route, Mapping):
        return ""
    contract = route.get("transition_contract")
    source_beat = node.get("story_beat")
    source_direction = (
        str(
            source_beat.get("narrative_summary")
            or source_beat.get("summary")
            or ""
        ).strip()
        if isinstance(source_beat, Mapping)
        else ""
    )
    bridge = (
        str(contract.get("bridge_scene_narration") or "").strip()
        if isinstance(contract, Mapping)
        else ""
    )
    target = runtime.engine.nodes.get(str(route.get("target_node_id") or ""))
    target_beat = target.get("story_beat") if isinstance(target, Mapping) else None
    opening = (
        str(target_beat.get("opening_scene") or "").strip()
        if isinstance(target_beat, Mapping)
        else ""
    )
    parts = []
    if source_direction:
        parts.append(
            "仍可在当前幕交付的作者方向："
            f"{source_direction[:900]}"
        )
    if bridge:
        parts.append(f"玩家接受后才可播放的作者桥段：{bridge[:600]}")
    if opening:
        parts.append(f"正式换幕后才成立的下一幕开场：{opening[:600]}")
    if not parts:
        return ""
    return (
        "以下内容用于区分当前幕可交付结果与正式换幕边界。"
        "保留玩家本轮已经实施的当前幕行动及其直接结果；只删除桥段或目标幕独有结果。"
        "桥段与下一幕开场只定义停止边界，不能在改写正文中泄露、复述或提前执行。"
        + " ".join(parts)
    )


@dataclass(frozen=True, slots=True)
class NumericV2TurnWorkflowResult:
    """回合模型调用和原子提交完成后交还给接口层的公开工作结果。"""  # noqa: DOCSTRING_CJK

    stored: NumericV2StoredSession
    outcome: TurnOutcomeV2
    performance: dict[str, Any]
    display_binding: Mapping[str, str]
    diagnostics: Mapping[str, Any]


def _add_elapsed_ms(
    diagnostics: dict[str, Any],
    phase: str,
    started_at: float,
) -> None:
    """累计阶段耗时；整回合墙钟时间仍单独记录。"""  # noqa: DOCSTRING_CJK

    elapsed_ms = round((time.monotonic() - started_at) * 1000, 3)
    timings = diagnostics["timings_ms"]
    timings[phase] = round(float(timings.get(phase, 0.0)) + elapsed_ms, 3)


def _increment_actor_attempts(diagnostics: dict[str, Any] | None) -> None:
    """记录 Actor 生成尝试次数；真实供应商请求由 Actor 的调用边界另行统计。"""  # noqa: DOCSTRING_CJK

    if diagnostics is not None:
        diagnostics["actor_generation_attempts"] = int(
            diagnostics.get("actor_generation_attempts", 0)
        ) + 1


async def _generate_actor_turn_with_output_retry(
    actor: NumericV2Actor,
    *,
    diagnostics: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Actor 正文未满足输出合同时最多重新采样四次。"""  # noqa: DOCSTRING_CJK

    last_error_code = ""
    required_retry_hint = str(kwargs.get("retry_hint") or "").strip()
    for attempt in range(4):
        try:
            retry_kwargs = dict(kwargs)
            if attempt:
                # 重复输出重试必须逐次改变模型收到的任务提示，不能原样发送四次相同请求。
                outcome = kwargs.get("outcome")
                ledger_event = getattr(outcome, "ledger_event", {})
                route_changed = (
                    isinstance(ledger_event, Mapping)
                    and str(ledger_event.get("from_node_id") or "")
                    != str(ledger_event.get("to_node_id") or "")
                )
                output_retry_hint = _output_retry_hint(
                    last_error_code=last_error_code,
                    retry_number=attempt,
                    route_changed=route_changed,
                )
                # 场景边界改写属于本次生成的核心任务；即使改写稿另有格式错误，
                # 后续输出重试也必须继续携带它，不能退回普通生成而再次越幕。
                retry_kwargs["retry_hint"] = "\n".join(
                    part for part in (required_retry_hint, output_retry_hint) if part
                )
            _increment_actor_attempts(diagnostics)
            return await actor.generate_turn(**retry_kwargs)
        except NumericV2ActorOutputError as exc:
            last_error_code = str(exc)
            if attempt == 3:
                raise
            session = kwargs.get("session")
            logger.warning(
                "Numeric v2 Actor retrying rejected visible output: reason=%s session_id=%s revision=%s",
                str(exc),
                getattr(session, "session_id", ""),
                getattr(session, "revision", ""),
            )
    raise AssertionError("unreachable")


async def execute_numeric_v2_turn(
    *,
    config_manager: Any,
    runtime: NumericV2Runtime,
    current: NumericV2StoredSession,
    turn: TurnRequestV2,
    ensure_current_binding: Callable[[Any], Mapping[str, str]],
    before_commit: Callable[[], Awaitable[None]] | None = None,
    diagnostics_sink: dict[str, Any] | None = None,
) -> NumericV2TurnWorkflowResult:
    """固定执行 Evaluator、Runtime、正文与推荐生成、身份复验和原子提交。"""  # noqa: DOCSTRING_CJK

    workflow_started_at = time.monotonic()
    # 压测器可传入可变容器；即使本轮失败，也能读取已经完成的阶段与模型成本。
    diagnostics = diagnostics_sink if diagnostics_sink is not None else {}
    diagnostics.clear()
    diagnostics.update({
        "timings_ms": {
            "evaluator_work": 0.0,
            "runtime_prepare_work": 0.0,
            "actor_work": 0.0,
            "transition_judge_work": 0.0,
            "commit_work": 0.0,
            "total_wall": 0.0,
        },
        "evaluator_model_attempts": 0,
        "actor_generation_attempts": 0,
        "actor_provider_calls": 0,
        "actor_suggestion_fill_attempts": 0,
        "actor_suggestion_fill_provider_calls": 0,
        "actor_suggestion_fill_reasons": {},
        "actor_base_suggestion_parse_counts": {},
        "transition_judge_calls": 0,
        "transition_judge_degraded": False,
        "transition_ownership_retries": 0,
        "transition_scene_boundary_retries": 0,
        "transition_author_boundary_retries": 0,
        "transition_offer_retries": 0,
        "evaluator_degraded": False,
        "completed": False,
    })
    evaluator = NumericV2MetricEvaluator(config_manager)
    actor = NumericV2Actor(config_manager)
    # 在正文重采样前冻结真实人格输入；推荐失败由内部降级，最终正文仍须属于同一角色世代。
    generation_binding = ensure_current_binding(current.session)
    generation_profile = actor._character_profile()

    async def evaluate_turn() -> NumericV2EvaluationResult:
        """执行一次 Evaluator，并把模型故障保守降级为无状态变化。"""  # noqa: DOCSTRING_CJK

        started_at = time.monotonic()
        diagnostics["evaluator_model_attempts"] += 1
        try:
            return await evaluator.evaluate(
                engine=runtime.engine,
                session=current.session,
                message=turn.message,
                recent_ledger_events=current.ledger_events,
            )
        except NumericV2EvaluatorError as exc:
            # Evaluator 只负责隐藏数值和已有转场态度，不应让一次判定服务抖动阻断玩家的正常演绎。
            # 降级结果不会改变数值，也不会凭空接受转场；下一回合仍可重新判定。
            diagnostics["evaluator_degraded"] = True
            logger.warning(
                "Numeric v2 Evaluator degraded to no-op: reason=%s session_id=%s revision=%s",
                str(exc),
                current.session.session_id,
                current.session.revision,
            )
            return NumericV2EvaluationResult(
                metric_changes=(),
                scene_complete=False,
                transition_intent="unclear",
            )
        finally:
            _add_elapsed_ms(diagnostics, "evaluator_work", started_at)

    def prepare_turn(evaluation: NumericV2EvaluationResult) -> TurnOutcomeV2:
        """执行确定性结算并累计同步 Runtime 耗时。"""  # noqa: DOCSTRING_CJK

        started_at = time.monotonic()
        try:
            return runtime.prepare_turn(
                current,
                turn,
                evaluation.metric_changes,
                scene_complete=evaluation.scene_complete,
                transition_intent=evaluation.transition_intent,
            )
        finally:
            _add_elapsed_ms(diagnostics, "runtime_prepare_work", started_at)

    async def generate_actor_turn(
        outcome: TurnOutcomeV2,
        *,
        retry_hint: str = "",
    ) -> dict[str, Any]:
        """按正式路径生成 Actor 正文；节奏只由同一次调用中的软提示引导。"""  # noqa: DOCSTRING_CJK

        started_at = time.monotonic()
        try:
            generation_kwargs = {
                "engine": runtime.engine,
                "session": current.session,
                "outcome": outcome,
                "player_input": turn.message,
                "character_profile": generation_profile,
                "diagnostics": diagnostics,
            }
            return await _generate_actor_turn_with_output_retry(
                actor,
                **generation_kwargs,
                retry_hint=retry_hint,
            )
        finally:
            # Actor 只可能因格式、重复或明确边界问题重试；这里记录累计调用耗时和真实供应商请求数。
            diagnostics["actor_provider_calls"] = int(
                getattr(actor, "provider_call_count", 0)
            )
            diagnostics["actor_suggestion_fill_attempts"] = int(
                getattr(actor, "suggestion_fill_attempt_count", 0)
            )
            diagnostics["actor_suggestion_fill_provider_calls"] = int(
                getattr(actor, "suggestion_fill_provider_call_count", 0)
            )
            diagnostics["actor_suggestion_fill_reasons"] = dict(
                getattr(actor, "suggestion_fill_reason_counts", {})
            )
            diagnostics["actor_base_suggestion_parse_counts"] = dict(
                getattr(actor, "base_suggestion_parse_counts", {})
            )
            _add_elapsed_ms(diagnostics, "actor_work", started_at)
    async def review_transition_offer(
        candidate: Mapping[str, Any],
    ) -> NumericV2TransitionOfferReview:
        """复核可见提议并累计调用成本；模型故障沿用原有保守撤销语义。"""  # noqa: DOCSTRING_CJK

        transition_judge_started_at = time.monotonic()
        diagnostics["transition_judge_calls"] += 1
        try:
            return await evaluator.validate_transition_offer(
                engine=runtime.engine,
                session=outcome.session,
                message=turn.message,
                actor_performance=candidate,
            )
        except NumericV2EvaluatorError as exc:
            diagnostics["transition_judge_degraded"] = True
            logger.warning(
                "Numeric v2 transition judge degraded to reject: reason=%s session_id=%s revision=%s",
                str(exc),
                current.session.session_id,
                current.session.revision,
            )
            return NumericV2TransitionOfferReview(
                offer_present=candidate.get("transition_offered") is True,
                valid=False,
                # 复核器故障不能据此断言正文侵犯行动所有权，也不应触发额外 Actor 调用。
                player_action_preserved=True,
                scene_boundary_preserved=True,
                author_boundaries_preserved=True,
            )
        finally:
            _add_elapsed_ms(
                diagnostics,
                "transition_judge_work",
                transition_judge_started_at,
            )

    evaluation = await evaluate_turn()
    outcome = prepare_turn(evaluation)
    performance = await generate_actor_turn(outcome)
    reviewed_transition_offered = False
    review_unflagged_visible_turn = (
        performance.get("transition_offered") is not True
        and bool(
            str(performance.get("performance") or "").strip()
            or str(performance.get("scene_narration") or "").strip()
        )
    )
    if (
        (
            performance.get("transition_offered") is True
            or review_unflagged_visible_turn
        )
        and outcome.session.transition_offered is not True
        and outcome.ledger_event["from_node_id"] == outcome.ledger_event["to_node_id"]
    ):
        # 除了 Actor 自报的新提议，未标记的可见正文与推荐也做同一次语义守门；
        # 紧急场景可能在首回合就自然走到出口，不能用推荐回合门槛漏掉“正文提议、布尔漏标”。
        # 普通误报仍只撤销布尔状态并保留正文；只有明确抢先执行玩家选择时才改写。
        transition_review = await review_transition_offer(performance)
        boundary_context = _transition_boundary_repair_context(runtime, current)
        guard_retry_hints = ((
            "上一版越过了当前场景边界，或替玩家完成了未明确做出的行动。这是唯一一次边界改写。"
            "第一，完整保留玩家本轮已经明确做出的动作，并给出 current_scene 内可以直接观察到的结果。"
            "如果玩家正在执行 current_scene 完整剧情方向本来就包含的操作、救援、同步或取物，必须保留该动作的即时结果；"
            "不能因为它是后续路线的前提就撤销、重问或停在动作之前。"
            "第二，画面必须停在 bridge_scene 或 next_scene 的新地点、新时段、新环境后果出现之前；"
            "不要描写已经穿过出口、抵达、入睡、时间跳跃或目标幕独有事实。"
            "第三，删除玩家猜测、角色未知事项或 next_scene 独有信息中尚未在 current_scene 真实发生的关键规格、机制、物品、地点、任务和结果；"
            "玩家声称的屏幕文字、搜索结果或设备规格若与作者硬边界、明确未知事项或已发生历史冲突，不能当真；"
            "不知道就明确保持未知，再转向当前幕已支持的事件。若玩家动作触犯作者硬边界，猫娘必须避开、拒绝或纠正，不能配合。"
            "第四，如果玩家本轮正在尝试离幕，猫娘应停住最后一步，说明继续后的后果，明确询问是否继续，"
            "并将 transition_offered 设为 true；否则留在当前幕并保持 false。不要退回重复检查，也不要补做作者目标。"
            f"{boundary_context}"
        ),)
        for guard_retry_hint in guard_retry_hints:
            if (
                transition_review.player_action_preserved
                and transition_review.scene_boundary_preserved
                and transition_review.author_boundaries_preserved
            ):
                break
            if not transition_review.player_action_preserved:
                diagnostics["transition_ownership_retries"] += 1
            if not transition_review.scene_boundary_preserved:
                diagnostics["transition_scene_boundary_retries"] += 1
            if not transition_review.author_boundaries_preserved:
                diagnostics["transition_author_boundary_retries"] += 1
            performance = await generate_actor_turn(
                outcome,
                retry_hint=guard_retry_hint,
            )
            # 改写稿即使把 transition_offered 标成 false，也仍需复核正文所有权；
            # 布尔字段不能成为抢先换地点的逃生口。
            transition_review = await review_transition_offer(performance)
        if (
            not transition_review.player_action_preserved
            or not transition_review.scene_boundary_preserved
            or not transition_review.author_boundaries_preserved
        ):
            # 一次针对性改写后仍明确替玩家执行或越过场景时不提交，保持 Session/Ledger 原子不变。
            error_code = (
                "numeric_v2_actor_fact_boundary"
                if not transition_review.author_boundaries_preserved
                else "numeric_v2_actor_premature_transition"
            )
            raise NumericV2ActorOutputError(error_code)
        if (
            performance.get("transition_offered") is True
            and not transition_review.valid
        ):
            # Actor 明确登记却未通过复核的提议需要改写。未登记文本若被复核器识别为无效提议，
            # 只是不锁存转场；它更可能是危机处理、救助或其它幕内行动，不应为一次模糊语义判断
            # 额外消耗 Actor 调用，更不能让一整轮合法回应回滚。
            diagnostics["transition_offer_retries"] += 1
            performance = await generate_actor_turn(
                outcome,
                retry_hint=(
                    "上一版的可见离幕邀请不够具体、没有真正结束当前互动阶段，或与 next_scene 明确冲突。"
                    "不要把作者目标、来源引用或路线理由当成待完成清单。请先回应玩家；若当前情境已有自然出口，"
                    "改为一个玩家下一轮可明确接受并亲自执行的具体提议，停在执行前并将 transition_offered 设为 true；"
                    "若没有合适出口，就删除全部离幕邀请，继续交付当前行动的可见结果并保持 transition_offered=false。"
                    "不得发明需要后续多轮搜索、试错或解锁的新物件、机关、工具或障碍。"
                ),
            )
            transition_review = await review_transition_offer(performance)
            if (
                not transition_review.player_action_preserved
                or not transition_review.scene_boundary_preserved
                or not transition_review.author_boundaries_preserved
            ):
                error_code = (
                    "numeric_v2_actor_fact_boundary"
                    if not transition_review.author_boundaries_preserved
                    else "numeric_v2_actor_premature_transition"
                )
                raise NumericV2ActorOutputError(error_code)
            # 一次因果改写后仍输出未通过复核的可见提议时不提交，避免正文或推荐产生失效邀请。
            if (
                performance.get("transition_offered") is True
                or transition_review.offer_present
            ) and not transition_review.valid:
                raise NumericV2ActorOutputError(
                    "numeric_v2_actor_invalid_transition_offer"
                )
        # 正文与推荐同属本轮公开输出。模型若漏写布尔声明，但语义复核确认这组可见内容
        # 已经形成合法提议，Runtime 仍需锁存它，否则玩家下一轮照推荐行动会陷入无待确认状态。
        if (
            performance.get("transition_offered") is not True
            and transition_review.offer_present
            and transition_review.valid
        ):
            reviewed_transition_offered = True
    # Actor 的布尔提议只在正文完成全部校验后锁存；旧模型缺少该字段时按“没有新提议”处理。
    # Runtime 已经根据本轮 Evaluator 结果计算出转场生命周期，尤其是 unclear 时必须保留旧提议；
    # 这里不能再用 Actor 的 false 覆盖 Runtime 的 true，否则下一轮 Evaluator 会失去可见提议。
    transition_offered = _merge_transition_offered(
        outcome.session.transition_offered,
        performance.get("transition_offered"),
        reviewed_transition_offered,
    )
    # Ledger 与 performance 必须保存同一个合并后的生命周期结果，避免存档重放时出现账本不一致。
    performance = {
        **performance,
        "transition_offered": transition_offered,
    }
    outcome = replace(
        outcome,
        session=replace(
            outcome.session,
            transition_offered=transition_offered,
        ),
        ledger_event={
            **outcome.ledger_event,
            "transition_offered": transition_offered,
        },
    )
    # 模型调用不占生命周期锁；仅将身份复验、展示刷新和原子提交与角色改名串行。
    commit_started_at = time.monotonic()
    try:
        async with character_config_mutation_lock:
            # 模型调用期间角色卡可能切换；成功输出不能提交到另一只猫娘的恢复槽位。
            display_binding = ensure_current_binding(current.session)
            current_profile = actor._character_profile()
            same_display_name = str(display_binding.get("catgirl_name") or "") == str(
                generation_binding.get("catgirl_name") or ""
            )
            if current_profile != generation_profile or (
                same_display_name
                and str(display_binding.get("profile_hash") or "")
                != str(generation_binding.get("profile_hash") or "")
            ):
                # 同名角色资料或实际人格文本已改变；旧人格输出不能伪装成新版本提交。
                raise ValueError("catgirl_profile_changed_requires_retry")
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
    finally:
        # 身份复验、写栅栏或存储失败也要留下提交阶段耗时，供失败样本定位。
        _add_elapsed_ms(diagnostics, "commit_work", commit_started_at)
    diagnostics["timings_ms"]["total_wall"] = round(
        (time.monotonic() - workflow_started_at) * 1000,
        3,
    )
    diagnostics["completed"] = True
    logger.info(
        "Numeric v2 workflow timing: session_id=%s revision=%s diagnostics=%s",
        current.session.session_id,
        stored.session.revision,
        diagnostics,
    )
    return NumericV2TurnWorkflowResult(
        stored=stored,
        outcome=outcome,
        performance=performance,
        display_binding=display_binding,
        diagnostics=diagnostics,
    )


__all__ = ["NumericV2TurnWorkflowResult", "execute_numeric_v2_turn"]
