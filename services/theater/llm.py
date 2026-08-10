"""实现自由模式的单次正文生成，不承载 Numeric v2 剧本状态。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from config import THEATER_TURN_USER_MESSAGE_MAX_TOKENS
from config.prompts.prompts_theater import build_theater_free_turn_messages
from utils.llm_client import HumanMessage, SystemMessage, create_chat_llm_async
from utils.logger_config import get_module_logger
from utils.token_tracker import set_call_type

from . import observability
from .llm_context import (
    THEATER_PERSONA_MAX_CHARS,
    _complete_model_text,
    _load_character_profile,
    _load_player_address,
    scoped_story_for_prompt as _scoped_story_for_prompt,
)
from .llm_response_contracts import _parse_free_output


# 自由模式使用较宽的正文预算；Numeric v2 的剧本 Actor 使用自己的独立合同和预算。
THEATER_FREE_TURN_TIMEOUT_SECONDS = 30.0
THEATER_FREE_OUTPUT_MAX_TOKENS = 1400
THEATER_FREE_CONTEXT_MAX_TOKENS = 700
THEATER_FREE_INPUT_MAX_CHARS = 560
logger = get_module_logger("services.theater.llm")


def _record_context_incomplete(*, responsibility: str, surface: str) -> None:
    """记录上下文裁剪失败，不把用户正文写入观测数据。"""  # noqa: DOCSTRING_CJK
    observability.record_result(
        responsibility=responsibility,
        surface=surface,
        result_kind="generation",
        outcome="context_incomplete",
    )


async def generate_free_turn_async(
    *,
    config_manager: Any | None,
    lanlan_name: str,
    story: dict[str, Any],
    scene: dict[str, Any],
    user_message: str,
    recent_turns: list[dict[str, Any]],
    is_opening: bool = False,
    role_card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成自由模式沙盒正文；不读取剧本图、Ledger 或 Numeric v2 状态。"""  # noqa: DOCSTRING_CJK
    api_config = _model_config(config_manager, "conversation")
    if not api_config:
        logger.info("Free theater turn aborted: reason=model_config_missing")
        observability.record_result(
            responsibility="theater_free_actor",
            surface="opening" if is_opening else "free_input",
            result_kind="generation",
            outcome="model_config_missing",
        )
        return {"ok": False, "reason": "free_actor_unavailable"}

    prompt_user_message = _complete_model_text(
        user_message,
        THEATER_TURN_USER_MESSAGE_MAX_TOKENS,
        max_chars=THEATER_FREE_INPUT_MAX_CHARS,
    )
    if prompt_user_message is None:
        _record_context_incomplete(
            responsibility="theater_free_actor",
            surface="opening" if is_opening else "free_input",
        )
        return {"ok": False, "reason": "free_actor_unavailable"}

    prompt_story = _scoped_story_for_prompt(
        story,
        max_background_tokens=THEATER_FREE_CONTEXT_MAX_TOKENS,
    )
    # 只读取当前猫娘本地人格摘要；角色卡 personality 仅作为测试替身的回退。
    current_profile = _load_character_profile(
        config_manager,
        lanlan_name,
        max_chars=THEATER_PERSONA_MAX_CHARS,
    )
    free_messages = build_theater_free_turn_messages(
        lanlan_name=lanlan_name,
        story=prompt_story,
        scene=scene,
        user_message=prompt_user_message,
        recent_turns=list(recent_turns or []),
        character_profile=str(
            current_profile
            or (
                role_card.get("personality")
                if isinstance(role_card, dict)
                else ""
            )
        ),
        player_address=_load_player_address(config_manager),
        is_opening=is_opening,
        role_card=role_card,
    )
    surface = "opening" if is_opening else "free_input"
    try:
        result = await _invoke_model_once(
            api_config,
            "",
            "",
            call_type="theater_free_actor",
            surface=surface,
            timeout_seconds=THEATER_FREE_TURN_TIMEOUT_SECONDS,
            max_completion_tokens=THEATER_FREE_OUTPUT_MAX_TOKENS,
            messages=free_messages,
        )
    except Exception as exc:
        logger.warning(
            "Free theater turn aborted: reason=model_call_failed error=%s surface=%s",
            type(exc).__name__,
            surface,
        )
        observability.record_result(
            responsibility="theater_free_actor",
            surface=surface,
            result_kind="generation",
            outcome="model_call_failed",
        )
        return {"ok": False, "reason": "free_actor_unavailable"}

    parsed = _parse_free_output(getattr(result, "content", ""))
    if parsed is None:
        # 自由模式直接接受正文；合同失败时丢弃本回合，不再启动格式 Repair。
        observability.record_result(
            responsibility="theater_free_actor",
            surface=surface,
            result_kind="generation",
            outcome="invalid_model_output",
        )
        return {"ok": False, "reason": "free_actor_unavailable"}
    observability.record_result(
        responsibility="theater_free_actor",
        surface=surface,
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
    messages: list[dict[str, str]] | None = None,
) -> Any:
    """按职责标签执行一次模型请求；是否重试由上层明确决定。"""  # noqa: DOCSTRING_CJK
    set_call_type(call_type)
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
            request_messages = (
                messages
                if messages is not None
                else [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            )
            response = await asyncio.wait_for(
                client.ainvoke(request_messages),
                timeout=timeout_seconds,
            )
    except asyncio.TimeoutError:
        # 超时只记录职责和错误类型，不保存请求正文或密钥。
        observability.record_model_call(
            call_type=call_type,
            surface=surface,
            started_at=started_at,
            status="timeout",
        )
        raise
    except Exception as exc:
        # 观测失败不能遮蔽真实模型异常；这里仍把异常原样交给自由 Runtime。
        with suppress(Exception):
            observability.record_model_call(
                call_type=call_type,
                surface=surface,
                started_at=started_at,
                status="error",
            )
        raise exc

    observability.record_model_call(
        call_type=call_type,
        surface=surface,
        started_at=started_at,
        status="success",
        response=response,
    )
    return response


def _model_config(config_manager: Any | None, tier: str) -> dict[str, Any]:
    """读取现有模型槽位；缺少模型或地址时返回空配置。"""  # noqa: DOCSTRING_CJK
    if config_manager is None:
        return {}
    try:
        config = dict(config_manager.get_model_api_config(tier) or {})
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
