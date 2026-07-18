"""分离剧本入口路由与猫娘演绎，并为模型调用提供安全回退。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from config import THEATER_TURN_USER_MESSAGE_MAX_TOKENS
from config.prompts.prompts_theater import (
    build_theater_route_prompts,
    build_theater_turn_prompts,
)
from utils.llm_client import HumanMessage, SystemMessage, create_chat_llm_async
from utils.logger_config import get_module_logger
from utils.token_tracker import set_call_type
from utils.tokenize import truncate_to_tokens

from . import model_trace, observability
from .llm_context import (
    _complete_model_text as _complete_model_text,
    _load_character_profile as _load_character_profile,
    _public_state as _public_state,
    _recent_public_turns as _recent_public_turns,
)
from .llm_fallbacks import (
    _authored_performance_fallback as _authored_performance_fallback,
    fallback_turn as fallback_turn,
)
from .llm_performance_guard import (
    _INTERNAL_META_OUTPUT_PATTERNS as _INTERNAL_META_OUTPUT_PATTERNS,
    _PRIVATE_IDENTIFIER_FIELDS as _PRIVATE_IDENTIFIER_FIELDS,
    _SOFT_PERFORMANCE_REASONS as _SOFT_PERFORMANCE_REASONS,
    _active_story_forbidden_phrases as _active_story_forbidden_phrases,
    _assistant_echoes_user as _assistant_echoes_user,
    _claims_uncommitted_choice_result as _claims_uncommitted_choice_result,
    _dialogue_claims_player_completion as _dialogue_claims_player_completion,
    _dialogue_key as _dialogue_key,
    _exposes_internal_runtime_detail as _exposes_internal_runtime_detail,
    _introduces_ungrounded_named_destination as _introduces_ungrounded_named_destination,
    _mirrors_player_question as _mirrors_player_question,
    _narration_claims_player_action as _narration_claims_player_action,
    _performance_clauses as _performance_clauses,
    _performance_repair_reason as _performance_repair_reason,
    _persona_self_name as _persona_self_name,
    _private_runtime_identifiers as _private_runtime_identifiers,
    _reanswers_previous_question as _reanswers_previous_question,
    _repeats_recent_dialogue as _repeats_recent_dialogue,
    _same_narrative_fact as _same_narrative_fact,
    _semantic_text_anchors as _semantic_text_anchors,
    _story_forbidden_output_patterns as _story_forbidden_output_patterns,
    _violates_author_consent_boundary as _violates_author_consent_boundary,
)
from .llm_response_contracts import (
    THEATER_RESPONSE_FOCUS_EVIDENCE_MAX_CHARS as THEATER_RESPONSE_FOCUS_EVIDENCE_MAX_CHARS,
    THEATER_RESPONSE_FOCUS_TYPES as THEATER_RESPONSE_FOCUS_TYPES,
    _FORBIDDEN_OUTPUT_TERMS as _FORBIDDEN_OUTPUT_TERMS,
    _balanced_json_object_fragments as _balanced_json_object_fragments,
    _empty_route_result as _empty_route_result,
    _load_unique_model_json_object as _load_unique_model_json_object,
    _parse_output as _parse_output,
    _parse_route_output as _parse_route_output,
    _technical_route_fallback as _technical_route_fallback,
    verify_response_focus as verify_response_focus,
)


THEATER_TURN_TIMEOUT_SECONDS = 10.0
THEATER_TURN_OUTPUT_MAX_TOKENS = 360
THEATER_CONTEXT_MAX_TOKENS = 500
logger = get_module_logger("services.theater.llm")


def _record_context_incomplete(*, responsibility: str, surface: str) -> None:
    """用固定低基数原因记录关键上下文不完整，不保存被截断正文。"""  # noqa: DOCSTRING_CJK
    observability.record_result(
        responsibility=responsibility,
        surface=surface,
        result_kind="generation",
        outcome="context_incomplete",
    )


async def route_free_input_async(
    *,
    config_manager: Any | None,
    story: dict[str, Any],
    scene: dict[str, Any],
    user_message: str,
    state: dict[str, Any],
    recent_turns: list[dict[str, Any]],
    choice_options: list[dict[str, Any]],
    latent_transitions: list[dict[str, Any]],
) -> dict[str, Any]:
    """用公开语境选择剧本入口；未命中时保守留在当前情景。"""  # noqa: DOCSTRING_CJK
    fallback = _technical_route_fallback()
    prompt_user_message = _complete_model_text(
        user_message,
        THEATER_TURN_USER_MESSAGE_MAX_TOKENS,
    )
    if prompt_user_message is None:
        # 只看玩家原话前缀可能漏掉句尾否定或转折；这种输入不能命中作者边或累计自由意图。
        _record_context_incomplete(
            responsibility="theater_router", surface="free_input"
        )
        return fallback
    api_config = _model_config(config_manager)
    if not api_config:
        logger.info("Theater route stays put: reason=model_config_missing")
        # 缺少配置属于可观察回退，但没有模型调用样本或 token 消耗。
        observability.record_result(
            responsibility="theater_router",
            surface="free_input",
            result_kind="generation",
            outcome="model_config_missing",
        )
        return fallback
    prompt_story = dict(story)
    prompt_story["background"] = truncate_to_tokens(
        str(story.get("background") or story.get("world_seed") or ""),
        THEATER_CONTEXT_MAX_TOKENS,
    )
    system_prompt, user_prompt = build_theater_route_prompts(
        story=prompt_story,
        scene=scene,
        user_message=prompt_user_message,
        public_state=_public_state(story, state),
        recent_turns=_recent_public_turns(recent_turns),
        choice_options=choice_options,
        latent_transitions=latent_transitions,
    )
    try:
        # Router 使用独立标签，避免自由意图判断与角色演绎的成本和失败率混在一起。
        result = await _invoke_model_once(
            api_config,
            system_prompt,
            user_prompt,
            call_type="theater_router",
            surface="free_input",
            timeout_seconds=THEATER_TURN_TIMEOUT_SECONDS,
            max_completion_tokens=THEATER_TURN_OUTPUT_MAX_TOKENS,
        )
    except Exception as exc:
        # 路由失败绝不能靠猜测推进；保留错误类型即可，不记录玩家原话和模型输出。
        logger.warning(
            "Theater route stays put: reason=model_call_failed error=%s",
            type(exc).__name__,
        )
        observability.record_result(
            responsibility="theater_router",
            surface="free_input",
            result_kind="generation",
            outcome="model_call_failed",
        )
        return fallback
    parsed = _parse_route_output(
        getattr(result, "content", ""),
        allowed_choice_ids={
            str(item.get("choice_id") or "") for item in choice_options
        },
        allowed_intent_ids={
            str(item.get("intent_id") or "") for item in latent_transitions
        },
        user_message=user_message,
    )
    if parsed is None:
        repair_prompt = (
            user_prompt
            + "\n格式修复：上一版 Router 输出不是合法 JSON。只返回一个完整 JSON 对象，字段固定为 "
            "route_kind、matched_choice_id、authored_intent_id、response_focus；不得输出解释或 Markdown。"
        )
        try:
            # Router 结果尚未更新任何意图计数，坏格式可以在同一回合提交前修复一次。
            repaired_result = await _invoke_model_once(
                api_config,
                system_prompt,
                repair_prompt,
                call_type="theater_repair",
                surface="free_input",
                timeout_seconds=THEATER_TURN_TIMEOUT_SECONDS,
                max_completion_tokens=THEATER_TURN_OUTPUT_MAX_TOKENS,
            )
        except Exception as exc:
            logger.warning(
                "Theater route stays put: reason=repair_call_failed error=%s",
                type(exc).__name__,
            )
            observability.record_result(
                responsibility="theater_router",
                surface="free_input",
                result_kind="generation",
                outcome="repair_call_failed",
            )
            return fallback
        parsed = _parse_route_output(
            getattr(repaired_result, "content", ""),
            allowed_choice_ids={
                str(item.get("choice_id") or "") for item in choice_options
            },
            allowed_intent_ids={
                str(item.get("intent_id") or "") for item in latent_transitions
            },
            user_message=user_message,
        )
        if parsed is None:
            logger.warning("Theater route stays put: reason=repair_rejected")
            observability.record_result(
                responsibility="theater_router",
                surface="free_input",
                result_kind="generation",
                outcome="repair_rejected",
            )
            return fallback
    # Router 只记录结构化结果是否可用，不记录命中的 Choice 内容。
    observability.record_result(
        responsibility="theater_router",
        surface="free_input",
        result_kind="generation",
        outcome="accepted",
    )
    return parsed


async def generate_turn_async(
    *,
    config_manager: Any | None,
    lanlan_name: str,
    story: dict[str, Any],
    scene: dict[str, Any],
    node: dict[str, Any],
    user_message: str,
    progress_kind: str,
    callback: str,
    state: dict[str, Any],
    recent_turns: list[dict[str, Any]],
    choice_options: list[dict[str, Any]] | None = None,
    response_focus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成一次结构化演绎；配置缺失、超时或坏输出时使用作者文本。"""  # noqa: DOCSTRING_CJK
    fallback = fallback_turn(
        lanlan_name=lanlan_name,
        scene=scene,
        node=node,
        user_message=user_message,
        progress_kind=progress_kind,
        callback=callback,
        has_scene_notes=bool(state.get("scene_notes")),
        recent_turns=recent_turns,
        choice_options=list(choice_options or []),
    )
    prompt_user_message = _complete_model_text(
        user_message,
        THEATER_TURN_USER_MESSAGE_MAX_TOKENS,
    )
    if prompt_user_message is None:
        # 普通 Actor 没有状态提交权，但仍不能基于半句玩家原话生成会被写入公开历史的回应。
        _record_context_incomplete(
            responsibility="theater_actor", surface=progress_kind
        )
        return fallback
    api_config = _model_config(config_manager)
    if not api_config:
        logger.info(
            "Theater turn uses author fallback: reason=model_config_missing progress=%s node=%s catgirl=%s",
            progress_kind,
            str(node.get("node_id") or ""),
            lanlan_name,
        )
        # 配置缺失不产生调用样本，但必须计入该演出场景的回退率。
        observability.record_result(
            responsibility="theater_actor",
            surface=progress_kind,
            result_kind="generation",
            outcome="model_config_missing",
        )
        return fallback

    # Scene Note 只辅助当前情景内的普通演绎；Router 不读取这类可截断笔记。
    public_state = _public_state(story, state, include_scene_notes=True)
    prompt_story = dict(story)
    prompt_story["background"] = truncate_to_tokens(
        str(story.get("background") or story.get("world_seed") or ""),
        THEATER_CONTEXT_MAX_TOKENS,
    )
    character_profile = _load_character_profile(config_manager, lanlan_name)
    system_prompt, user_prompt = build_theater_turn_prompts(
        lanlan_name=lanlan_name,
        story=prompt_story,
        scene=scene,
        node=node,
        user_message=prompt_user_message,
        progress_kind=progress_kind,
        callback=truncate_to_tokens(callback, 120),
        public_state=public_state,
        recent_turns=_recent_public_turns(recent_turns),
        character_profile=character_profile,
        choice_options=list(choice_options or []),
        response_focus=(
            dict(response_focus) if isinstance(response_focus, dict) else {}
        ),
    )
    try:
        # Actor 标签只覆盖首版角色演绎；纠错调用使用单独 Repair 标签。
        result = await _invoke_model_once(
            api_config,
            system_prompt,
            user_prompt,
            call_type="theater_actor",
            surface=progress_kind,
            timeout_seconds=THEATER_TURN_TIMEOUT_SECONDS,
            max_completion_tokens=THEATER_TURN_OUTPUT_MAX_TOKENS,
        )
    except Exception as exc:
        # 不记录提示词、玩家输入或模型原文，只记录可定位的失败类型，避免下一次只能从固定台词反推原因。
        logger.warning(
            "Theater turn uses author fallback: reason=model_call_failed error=%s progress=%s node=%s catgirl=%s",
            type(exc).__name__,
            progress_kind,
            str(node.get("node_id") or ""),
            lanlan_name,
        )
        observability.record_result(
            responsibility="theater_actor",
            surface=progress_kind,
            result_kind="generation",
            outcome="model_call_failed",
        )
        return fallback
    parsed = _parse_output(
        getattr(result, "content", ""),
        progress_kind=progress_kind,
    )
    if parsed is not None:
        parsed["choice_rewrites"] = []
    authored_performance = progress_kind in {"opening", "graph_progress"}
    grounding_text = json.dumps(
        {
            "background": story.get("background") or story.get("world_seed") or "",
            "scene": scene,
            "public_state": public_state,
            "recent_turns": _recent_public_turns(recent_turns),
            "choice_options": list(choice_options or []),
            "user_message": user_message,
        },
        ensure_ascii=False,
    )
    private_identifiers = _private_runtime_identifiers(
        node,
        list(choice_options or []),
        state,
    )
    performance_repair_reason = _performance_repair_reason(
        parsed,
        progress_kind=progress_kind,
        user_message=user_message,
        node=node,
        character_profile=character_profile,
        story=story,
        state=state,
        grounding_text=grounding_text,
        choice_options=list(choice_options or []),
        private_identifiers=private_identifiers,
        recent_turns=recent_turns,
        response_focus=(
            dict(response_focus) if isinstance(response_focus, dict) else {}
        ),
    )
    # 普通 Actor 的软语义只影响观感，不拥有服务端状态，也不触发额外模型调用。
    soft_performance = performance_repair_reason in _SOFT_PERFORMANCE_REASONS
    if parsed is not None and (not performance_repair_reason or soft_performance):
        repair_reason = ""
    else:
        # 只有解析失败、明确抢跑或作者/世界/关系硬边界才获得一次 Repair。
        repair_reason = performance_repair_reason
    if repair_reason:
        # 只对可机械判定的结构与权威边界重试一次；开放式文风和按钮新鲜度不进入循环评判。
        if progress_kind == "roleplay_response":
            correction = (
                "\n纠错重试：上一版输出未通过检查（"
                + repair_reason
                + "）。请重新输出完整 JSON，不得提及纠错过程；choice_rewrites 必须为空数组。"
                "不得使用目标节点列出的禁用对白词。"
                "玩家本轮提出问题时必须先直接回答，不得把同一个问题换种说法反问玩家。"
                "上一轮问题已经回答后，本轮若是评价或态度，只回应当前评价，不得重新解释上一轮主题。"
                "当前推荐项仍是未执行候选，不得感谢玩家完成它、不得把作者回调或目标结果写成既成事实。"
                "不得编造公开上下文中没有出现的命名地点；若去向尚未确定，只回答已经公开的最近目的地。"
                "内部规则只能执行，不能在旁白、对白或推荐项里解释、承诺或换一种说法复述。"
            )
        else:
            correction = (
                "\n纠错重试：上一版输出未通过检查（"
                + repair_reason
                + "）。请重新输出完整 JSON，不得提及纠错过程。优先采用作者对白原文，"
                "不得复述玩家，不得增加口癖、命令、强迫或单方批准，也不得使用目标节点列出的禁用对白词。"
                "故事输出硬边界同时约束旁白和对白；内部规则只能执行，不能由猫娘说给玩家。"
            )
        try:
            # Repair 独立计量，便于区分首版质量问题与供应商调用故障。
            repaired_result = await _invoke_model_once(
                api_config,
                system_prompt,
                user_prompt + correction,
                call_type="theater_repair",
                surface=progress_kind,
                timeout_seconds=THEATER_TURN_TIMEOUT_SECONDS,
                max_completion_tokens=THEATER_TURN_OUTPUT_MAX_TOKENS,
            )
        except Exception as exc:
            logger.warning(
                "Theater turn uses author fallback: reason=repair_call_failed repair=%s error=%s progress=%s node=%s catgirl=%s",
                repair_reason,
                type(exc).__name__,
                progress_kind,
                str(node.get("node_id") or ""),
                lanlan_name,
            )
            observability.record_result(
                responsibility="theater_actor",
                surface=progress_kind,
                result_kind="generation",
                outcome="repair_call_failed",
            )
            return _authored_performance_fallback(fallback, node, progress_kind)
        repaired = _parse_output(
            getattr(repaired_result, "content", ""),
            progress_kind=progress_kind,
        )
        if repaired is not None:
            repaired["choice_rewrites"] = []
        remaining_performance_reason = _performance_repair_reason(
            repaired,
            progress_kind=progress_kind,
            user_message=user_message,
            node=node,
            character_profile=character_profile,
            story=story,
            state=state,
            grounding_text=grounding_text,
            choice_options=list(choice_options or []),
            private_identifiers=private_identifiers,
            recent_turns=recent_turns,
            response_focus=(
                dict(response_focus) if isinstance(response_focus, dict) else {}
            ),
        )
        remaining_hard_reason = (
            remaining_performance_reason
            if remaining_performance_reason not in _SOFT_PERFORMANCE_REASONS
            else ""
        )
        if remaining_hard_reason:
            logger.warning(
                "Theater turn uses author fallback: reason=repair_rejected first=%s second=%s progress=%s node=%s catgirl=%s",
                repair_reason,
                remaining_hard_reason,
                progress_kind,
                str(node.get("node_id") or ""),
                lanlan_name,
            )
            observability.record_result(
                responsibility="theater_actor",
                surface=progress_kind,
                result_kind="generation",
                outcome="repair_rejected",
            )
            return _authored_performance_fallback(fallback, node, progress_kind)
        # Repair 已越过硬边界后直接采用正文；软语义不再触发第三层裁决。
        parsed = dict(repaired or {})
    if parsed and authored_performance and str(callback or "").strip():
        # 开场 Scene 或 Choice callback 都是作者已确认的公开演出；模型只能增强猫娘回应，不能改写或抢跑旁白。
        parsed["narration"] = str(callback).strip()
    if parsed is None:
        logger.warning(
            "Theater turn uses author fallback: reason=invalid_model_output progress=%s node=%s catgirl=%s",
            progress_kind,
            str(node.get("node_id") or ""),
            lanlan_name,
        )
        observability.record_result(
            responsibility="theater_actor",
            surface=progress_kind,
            result_kind="generation",
            outcome="invalid_model_output",
        )
        return fallback
    # 最终演出已通过结构与权威硬边界；文风、复述和人格表达由模型自由完成。
    observability.record_result(
        responsibility="theater_actor",
        surface=progress_kind,
        result_kind="generation",
        outcome="accepted",
    )
    return parsed


async def _invoke_model_once(
    api_config: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    *,
    call_type: str,
    surface: str,
    timeout_seconds: float,
    max_completion_tokens: int,
) -> Any:
    """按职责标签和显式预算执行一次结构化请求；是否再次调用由上层决定。"""  # noqa: DOCSTRING_CJK
    # 标签在创建客户端前写入当前 token 追踪上下文，使每种职责都能独立观测。
    set_call_type(call_type)
    # 单调时钟只用于脱敏耗时统计，不写入 Prompt、用户输入或模型输出。
    started_at = observability.start_timer()
    client = await create_chat_llm_async(
        api_config["model"],
        api_config["base_url"],
        api_config.get("api_key"),
        provider_type=api_config.get("provider_type"),
        timeout=timeout_seconds,
        max_retries=0,
        max_completion_tokens=max_completion_tokens,
    )
    try:
        async with client:
            response = await asyncio.wait_for(
                client.ainvoke(  # noqa: LLM_INPUT_BUDGET  # Router 与 Actor 的用户文本、背景和历史均在各调用方按 THEATER_* token 常量截断；本 helper 只统一发送已构造消息。
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_prompt),
                    ]
                ),
                timeout=timeout_seconds,
            )
    except asyncio.TimeoutError:
        # 超时单独归类，便于区分供应商错误与预算不足；异常仍原样交给上层安全回退。
        model_trace.record_model_return(
            call_type=call_type,
            surface=surface,
            status="timeout",
            model=str(api_config.get("model") or ""),
            provider_type=str(api_config.get("provider_type") or ""),
            error_type="TimeoutError",
        )
        observability.record_model_call(
            call_type=call_type,
            surface=surface,
            started_at=started_at,
            status="timeout",
        )
        raise
    except Exception as exc:
        # 私有诊断记录只保存异常类型，不保存可能夹带请求正文或密钥的异常消息。
        model_trace.record_model_return(
            call_type=call_type,
            surface=surface,
            status="error",
            model=str(api_config.get("model") or ""),
            provider_type=str(api_config.get("provider_type") or ""),
            error_type=type(exc).__name__,
        )
        # 指标观测失败不能遮蔽原始模型异常；这里只发固定 error 状态。
        with suppress(Exception):
            observability.record_model_call(
                call_type=call_type,
                surface=surface,
                started_at=started_at,
                status="error",
            )
        raise
    # 所有职责都经过这个入口，因此 Router、Actor 和 Repair 的原始返回会按调用顺序采集。
    model_trace.record_model_return(
        call_type=call_type,
        surface=surface,
        status="success",
        model=str(api_config.get("model") or ""),
        provider_type=str(api_config.get("provider_type") or ""),
        content=getattr(response, "content", ""),
    )
    # 成功响应只提取供应商 usage 数值，正文不会进入观测样本。
    observability.record_model_call(
        call_type=call_type,
        surface=surface,
        started_at=started_at,
        status="success",
        response=response,
    )
    return response


def _model_config(config_manager: Any | None) -> dict[str, Any]:
    """读取 summary 档模型配置；不完整时返回空配置。"""  # noqa: DOCSTRING_CJK
    if config_manager is None:
        return {}
    try:
        config = dict(config_manager.get_model_api_config("summary") or {})
    except Exception:
        return {}
    if (
        not str(config.get("model") or "").strip()
        or not str(config.get("base_url") or "").strip()
    ):
        return {}
    config["model"] = str(config["model"]).strip()
    config["base_url"] = str(config["base_url"]).strip()
    return config
