"""编排当前版单猫娘小剧场的作者状态图回合。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import (
    llm,
    model_trace,
    observability,
    projector,
    rules,
    session_store,
    story_graph,
    story_loader,
    turn_causality,
)
from .turn_history import (
    MAX_RECENT_TURN_MESSAGES as MAX_RECENT_TURN_MESSAGES,
    _append_turns as _append_turns,
    _compose_graph_progress_dialogue as _compose_graph_progress_dialogue,
    _now_ms as _now_ms,
)
from .turn_request_contracts import (
    MAX_FREE_INPUT_CHARS as MAX_FREE_INPUT_CHARS,
    MAX_IDEMPOTENT_RESULTS as MAX_IDEMPOTENT_RESULTS,
    _cached_result as _cached_result,
    _normalize_request as _normalize_request,
    _revision_conflict as _revision_conflict,
    _turn_execution_surface as _turn_execution_surface,
    _turn_submit_outcome as _turn_submit_outcome,
)


async def submit(
    root: Path,
    *,
    session_id: str,
    input_kind: str,
    choice_id: str,
    message: str,
    client_turn_id: str,
    base_revision: Any,
    config_manager: Any | None,
    expected_lanlan_name: str = "",
) -> dict[str, Any]:
    """提交一个完整回合，并保证所有退出路径只记录一次端到端结果。"""  # noqa: DOCSTRING_CJK
    started_at = observability.start_timer()
    timing: dict[str, Any] = {
        "lock_wait_ms": None,
        "execution_surface": "invalid",
        "idempotent_replay": False,
    }
    outcome = "unexpected_error"
    try:
        result = await _submit_impl(
            root,
            session_id=session_id,
            input_kind=input_kind,
            choice_id=choice_id,
            message=message,
            client_turn_id=client_turn_id,
            base_revision=base_revision,
            config_manager=config_manager,
            expected_lanlan_name=expected_lanlan_name,
            timing=timing,
        )
        outcome = (
            "idempotent_replay"
            if timing["idempotent_replay"] is True
            else _turn_submit_outcome(result)
        )
        return result
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        # 指标只接收输入类型、固定结果码和耗时，不能把本轮原话或任何 Session 身份交给观测层。
        observability.record_turn_submit(
            input_kind=str(input_kind or "").strip(),
            surface=str(timing["execution_surface"]),
            outcome=outcome,
            started_at=started_at,
            lock_wait_ms=timing["lock_wait_ms"],
        )


async def _submit_impl(
    root: Path,
    *,
    session_id: str,
    input_kind: str,
    choice_id: str,
    message: str,
    client_turn_id: str,
    base_revision: Any,
    config_manager: Any | None,
    expected_lanlan_name: str = "",
    timing: dict[str, Any],
) -> dict[str, Any]:
    """校验并原子提交候选 Session；完整事务观测由公开入口统一收口。"""  # noqa: DOCSTRING_CJK
    request, error = _normalize_request(
        input_kind=input_kind,
        choice_id=choice_id,
        message=message,
        client_turn_id=client_turn_id,
        base_revision=base_revision,
    )
    if error:
        return {"ok": False, "reason": error}
    timing["execution_surface"] = "unresolved"
    lock_started_at = observability.start_timer()
    async with session_store.session_guard(session_id):
        # 进入锁后立即冻结等待时长；后续持锁模型调用不得混入 lock_wait 指标。
        timing["lock_wait_ms"] = observability.elapsed_ms(lock_started_at)
        session = await session_store.load_session(root, session_id)
        if session is None:
            return {"ok": False, "reason": "session_not_found"}
        expected_name = str(expected_lanlan_name or "").strip()
        if (
            expected_name
            and str(session.get("lanlan_name") or "").strip() != expected_name
        ):
            # Session ID 可能来自旧 localStorage；角色归属不匹配时不能读取幂等结果或继续推进。
            return {"ok": False, "reason": "session_character_mismatch"}
        if not session_store.lifecycle_fields_valid(session):
            # 输入接口不能绕过恢复门禁，把坏休眠时间或任意终止原因当成一次成功唤醒。
            return {
                "ok": False,
                "reason": "session_state_invalid",
                "session_id": str(session_id or ""),
            }
        cached = _cached_result(session, request["client_turn_id"])
        if await session_store.is_stale_session(root, session):
            return {"ok": False, "reason": "stale_session", "skipped": True}
        if session.get("ended_at"):
            ending = cached.get("ending") if isinstance(cached, dict) else None
            if isinstance(ending, dict) and ending.get("should_end_session") is True:
                # 已提交的主动离场/作者结局仍可幂等回放，但普通旧回合不能复活结束态 Session。
                timing["idempotent_replay"] = True
                timing["execution_surface"] = "idempotent_replay"
                cached["session_lifecycle"] = projector.session_lifecycle(session)
                return cached
            return {"ok": False, "reason": "session_ended"}
        if cached:
            timing["idempotent_replay"] = True
            timing["execution_surface"] = "idempotent_replay"
            # 休眠扫描不会改写旧幂等结果；回放时必须覆盖为当前生命周期且不能借重试唤醒。
            cached["session_lifecycle"] = projector.session_lifecycle(session)
            return cached
        revision = session_store.state_revision(session)
        expected = request.get("base_revision")
        if expected is not None and expected != revision:
            return _revision_conflict(revision)

        # 所有业务变化先写候选副本；只有完整公开响应生成后才替换原存档。
        candidate = deepcopy(session)
        stored_model_returns = candidate.get("llm_return_records")
        if stored_model_returns is None:
            # 兼容本字段上线前创建的旧 Session；只在下一次成功回合中补为空列表。
            candidate["llm_return_records"] = []
        elif not isinstance(stored_model_returns, list):
            # 私有诊断数据结构损坏时不能静默覆盖，否则会破坏问题复盘所需证据。
            return {
                "ok": False,
                "reason": "session_state_invalid",
                "session_id": str(session_id or ""),
            }
        stored_causality_records = candidate.get("turn_causality_records")
        if stored_causality_records is None:
            # 旧 Session 在下一次成功回合中懒补；失败候选仍不会修改原文件。
            candidate["turn_causality_records"] = []
        elif not isinstance(stored_causality_records, list):
            # 私有因果记录同样属于 Bug 证据，结构损坏时不能静默覆盖。
            return {
                "ok": False,
                "reason": "session_state_invalid",
                "session_id": str(session_id or ""),
            }
        # 只在候选中清除休眠；后续任一校验或模型失败都不会写盘，只有成功提交才真正唤醒。
        candidate.pop("dormant_at", None)
        try:
            story = await story_loader.load_story_exact(
                str(candidate.get("story_id") or "")
            )
        except (FileNotFoundError, ValueError):
            # 用户 Story 已移除或合同失效时保留 Session，不把旧输入推进到目录中的其他剧本。
            return {
                "ok": False,
                "reason": "session_story_unavailable",
                "session_id": str(session_id or ""),
            }
        stored_story_revision = str(candidate.get("story_revision") or "").strip()
        current_story_revision = str(story.get("story_revision") or "").strip()
        if stored_story_revision and stored_story_revision != current_story_revision:
            # 未刷新页面也必须服从同一 Story 版本门禁，不能用旧按钮推进已改写的作者图。
            return {
                "ok": False,
                "reason": "session_story_revision_mismatch",
                "session_id": str(session_id or ""),
            }
        if not stored_story_revision:
            # 同 schema 早期存档在下一次成功回合中补齐 revision；失败候选仍不会写盘。
            candidate["story_revision"] = current_story_revision
        # 保存提交前快照，供成功提交后的因果记录比较。
        before_state = deepcopy(candidate.get("story_state"))
        if request["input_kind"] == "user_exit":
            timing["execution_surface"] = "user_exit"
        elif request["input_kind"] == "free_input":
            timing["execution_surface"] = "roleplay_response"
        elif request["input_kind"] == "choice":
            timing["execution_surface"] = "graph_progress"
        # 采集上下文覆盖本回合 Router、Actor 与 Repair 调用；失败候选退出后直接丢弃。
        turn_diagnostic: dict[str, Any] = {"response_focus": {}}
        with model_trace.capture_model_returns() as model_return_records:
            response = await _apply_turn(
                candidate,
                story,
                request,
                config_manager=config_manager,
                turn_diagnostic=turn_diagnostic,
            )
        if response.get("ok") is not True:
            return response
        timing["execution_surface"] = _turn_execution_surface(
            request=request,
            response=response,
        )

        if (
            expected_name
            and await _current_catgirl_name(config_manager) != expected_name
        ):
            # 角色切换先写当前猫娘、后等待旧 Session 清理；因此模型返回后必须直接重验配置归属。
            return {"ok": False, "reason": "session_character_mismatch"}

        lanlan_name = str(candidate.get("lanlan_name") or "")
        async with session_store.character_guard(root, lanlan_name):
            # 二次校验、写盘和返回共享开场使用的角色边界，新窗口不能在中途替换 active Session。
            latest = await session_store.load_session(root, session_id)
            if latest is None:
                return {"ok": False, "reason": "session_not_found"}
            if await session_store.is_stale_session(root, latest):
                # 模型生成期间可能已有新窗口替换活动 Session，旧候选状态此时必须直接丢弃。
                return {"ok": False, "reason": "stale_session", "skipped": True}
            if session_store.state_revision(latest) != revision:
                return _revision_conflict(session_store.state_revision(latest))
            next_revision = revision + 1
            candidate["state_revision"] = next_revision
            candidate["updated_at"] = _now_ms()
            response["state_revision"] = next_revision
            # 只有 revision 二次校验通过的回合才绑定身份并落盘；幂等重放在采集前返回，不会重复追加。
            for record in model_return_records:
                candidate["llm_return_records"].append(
                    {
                        **record,
                        "session_id": str(candidate.get("session_id") or ""),
                        "client_turn_id": request["client_turn_id"],
                        "base_revision": revision,
                        "result_revision": next_revision,
                    }
                )
            candidate["turn_causality_records"].append(
                turn_causality.build_record(
                    session_id=str(candidate.get("session_id") or ""),
                    request=request,
                    response_focus=turn_diagnostic["response_focus"],
                    model_return_records=model_return_records,
                    response=response,
                    before_state=before_state,
                    after_state=candidate.get("story_state"),
                    base_revision=revision,
                    result_revision=next_revision,
                    session_ended=bool(candidate.get("ended_at")),
                )
            )
            # 与幂等结果使用同一保留窗口；淘汰私有诊断不影响公开历史或权威状态。
            while len(candidate["turn_causality_records"]) > MAX_IDEMPOTENT_RESULTS:
                candidate["turn_causality_records"].pop(0)
            candidate["public_snapshot"] = deepcopy(response)
            index = candidate.setdefault("turn_results_by_client_id", {})
            index[request["client_turn_id"]] = deepcopy(response)
            # 字典保持提交顺序；超过上限时淘汰最早结果，旧请求仍会被 revision 校验阻止重复推进。
            while len(index) > MAX_IDEMPOTENT_RESULTS:
                index.pop(next(iter(index)))
            await session_store.save_session(root, candidate)
            if candidate.get("ended_at"):
                # 正式结局和主动离场都在同一角色边界内清除恢复索引。
                await session_store.clear_active_session(
                    root,
                    lanlan_name,
                    str(candidate.get("session_id") or ""),
                )
            return deepcopy(response)


async def _current_catgirl_name(config_manager: Any | None) -> str:
    """从同一配置管理器重读当前猫娘，兼容同步与异步加载接口。"""  # noqa: DOCSTRING_CJK
    async_loader = getattr(config_manager, "aload_characters", None)
    if callable(async_loader):
        characters = await async_loader()
    else:
        sync_loader = getattr(config_manager, "load_characters", None)
        characters = sync_loader() if callable(sync_loader) else {}
    if not isinstance(characters, dict):
        characters = {}
    # Router 在没有已选角色时使用 Lan；这里保持同一归一化语义，避免默认角色被误判为切换。
    return str(characters.get("当前猫娘") or "").strip() or "Lan"


async def _apply_turn(
    session: dict[str, Any],
    story: dict[str, Any],
    request: dict[str, Any],
    *,
    config_manager: Any | None,
    turn_diagnostic: dict[str, Any],
) -> dict[str, Any]:
    """按剧本图推进；未命中作者入口的输入只在当前情景内交流。"""  # noqa: DOCSTRING_CJK
    if request["input_kind"] == "user_exit":
        return _apply_exit(session, story)

    state = (
        session.get("story_state")
        if isinstance(session.get("story_state"), dict)
        else {}
    )
    current = story_graph.current_node(story, state)
    choice: dict[str, Any] = {}
    progress_kind = "roleplay_response"
    message = request["message"]
    lanlan_name = str(session.get("lanlan_name") or "猫娘")
    response_focus: dict[str, Any] = {}

    if request["input_kind"] == "choice":
        choice = story_graph.resolve_choice(
            story,
            state,
            request["choice_id"],
            lanlan_name=lanlan_name,
        )
        if not choice:
            return {"ok": False, "reason": "choice_not_available"}
        message = str(choice.get("label") or "")
        progress_kind = "graph_progress"
    elif request["input_kind"] == "free_input":
        choice = story_graph.resolve_authored_completion(
            story,
            state,
            message,
            lanlan_name=lanlan_name,
        )
        if not choice:
            current_phase = str(
                current.get("belong_phase") or session.get("phase") or "setup"
            )
            current_scene = story_loader.scene_for_phase(story, current_phase)
            route_choices = story_graph.suggestion_options(
                story, state, lanlan_name=lanlan_name
            )
            latent_transitions = story_graph.latent_transition_options(story, state)
            route = await llm.route_free_input_async(
                config_manager=config_manager,
                story=story,
                scene=current_scene,
                user_message=message,
                state=state,
                recent_turns=list(session.get("turns") or []),
                choice_options=route_choices,
                latent_transitions=latent_transitions,
            )
            response_focus = llm.verify_response_focus(
                route.get("response_focus"),
                user_message=message,
            )
            matched_choice_id = str(route.get("matched_choice_id") or "")
            if matched_choice_id:
                choice = next(
                    (
                        dict(item)
                        for item in route_choices
                        if item["choice_id"] == matched_choice_id
                    ),
                    {},
                )
            else:
                authored_intent_id = str(
                    route.get("authored_intent_id")
                    or route.get("observed_intent_id")
                    or ""
                )
                latent_transition = story_graph.resolve_latent_transition(
                    latent_transitions,
                    authored_intent_id,
                )
                if latent_transition:
                    choice = {
                        "choice_id": str(
                            latent_transition.get("transition_id") or ""
                        ),
                        "target_node_id": str(
                            latent_transition.get("target_node_id") or ""
                        ),
                        "callback": str(latent_transition.get("callback") or ""),
                        "transition_id": str(
                            latent_transition.get("transition_id") or ""
                        ),
                    }
        if choice:
            choice["label"] = message
            progress_kind = "graph_progress"

    target = current
    if choice:
        target = story_graph.node_by_id(
            story, str(choice.get("target_node_id") or "")
        )
        rules.apply_node(story, state, target)
        if choice.get("transition_id"):
            rules.commit_latent_transition(state, str(choice["transition_id"]))

    phase = str(target.get("belong_phase") or session.get("phase") or "setup")
    scene = story_loader.scene_for_phase(story, phase)
    choice_options = story_graph.suggestion_options(
        story, state, lanlan_name=lanlan_name
    )
    performance = await llm.generate_turn_async(
        config_manager=config_manager,
        lanlan_name=lanlan_name,
        story=story,
        scene=scene,
        node=target,
        user_message=message,
        progress_kind=progress_kind,
        callback=str(choice.get("callback") or ""),
        state=state,
        recent_turns=list(session.get("turns") or []),
        choice_options=choice_options,
        response_focus=response_focus,
    )
    if progress_kind == "graph_progress":
        author_dialogue = str(target.get("scripted_dialogue") or "")
        # 没有独立焦点时维持作者对白完全相等；有焦点时只保留 Actor 的即时补充，再逐字追加作者正文。
        performance["dialogue"] = _compose_graph_progress_dialogue(
            author_dialogue=author_dialogue,
            generated_dialogue=str(performance.get("dialogue") or ""),
            response_focus=response_focus,
        )
    # Actor 输出中的 Choice 改写只为旧模型响应兼容而丢弃；玩家始终看到作者原文。
    performance.pop("choice_rewrites", None)
    # 兼容旧模型返回，但模型不能新增剧本事实或选项。
    performance.pop("fact_candidates", None)
    if progress_kind == "roleplay_response":
        # 未明确命中唯一 Choice 的自由互动只形成非权威笔记，不参与静态可达性和结局判断。
        rules.append_scene_note(state, message)
        # 清除旧 Session 遗留的显示覆盖；自由互动不能取得作者 Choice 文案权。
        state.pop("choice_label_overrides", None)
    outgoing = story_graph.outgoing_nodes(story, state)
    ending = rules.ending_for_state(
        story, state, target, has_outgoing=bool(outgoing)
    )
    if progress_kind == "roleplay_response":
        # 单纯对话不能因为当前节点暂无出口而自动结束，正式结束只发生在剧情推进后。
        ending = {
            "should_offer_ending": False,
            "should_end_session": False,
            "ending_id": "",
        }

    session["phase"] = phase
    session["story_state"] = state
    trace = projector.scenario_trace(
        progress_kind=progress_kind,
        choice=choice,
    )
    _append_turns(session, message=message, performance=performance, trace=trace)
    if ending.get("should_end_session"):
        session["ended_at"] = _now_ms()
        ending_reason = str(ending.get("reason") or "story_complete")
        session["end_reason"] = (
            ending_reason
            if ending_reason in session_store.SESSION_END_REASONS
            else "story_complete"
        )
        session.pop("dormant_at", None)

    response = projector.public_response(
        session=session,
        story=story,
        scene=scene,
        narration=performance["narration"],
        dialogue=performance["dialogue"],
        trace=trace,
        ending=ending,
        can_resume=not bool(session.get("ended_at")),
    )
    # 只在完整候选已经形成后暴露最终有效焦点；失败回合的私有容器不会落盘。
    turn_diagnostic["response_focus"] = deepcopy(response_focus)
    return response


def _apply_exit(session: dict[str, Any], story: dict[str, Any]) -> dict[str, Any]:
    """结束本场演出，但不伪装成作者结局。"""  # noqa: DOCSTRING_CJK
    session["ended_at"] = _now_ms()
    session["end_reason"] = "user_exit"
    session.pop("dormant_at", None)
    state = (
        session.get("story_state")
        if isinstance(session.get("story_state"), dict)
        else {}
    )
    session["story_state"] = state
    node = story_graph.current_node(story, state)
    phase = str(node.get("belong_phase") or session.get("phase") or "setup")
    scene = story_loader.scene_for_phase(story, phase)
    ending = {
        "should_offer_ending": False,
        "should_end_session": True,
        "ending_id": "",
        "reason": "user_exit",
    }
    trace = projector.scenario_trace(progress_kind="user_exit")
    return projector.public_response(
        session=session,
        story=story,
        scene=scene,
        narration="",
        dialogue="",
        trace=trace,
        ending=ending,
        can_resume=False,
    )
