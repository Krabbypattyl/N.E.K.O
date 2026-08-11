# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""提供本地轻量小剧场页面所需的 HTTP 接口。"""  # noqa: DOCSTRING_CJK
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request

from services.theater import free_runtime, story_loader
from services.theater.paths import theater_root
from services.theater.tts_bridge import speak_committed_line
from .shared_state import get_config_manager, get_session_manager


router = APIRouter(tags=["theater"], prefix="/api/theater")
logger = logging.getLogger(__name__)


def _resolve_lanlan_name() -> str:
    """只从服务端配置解析当前猫娘，不信任调用方传入的角色名。"""  # noqa: DOCSTRING_CJK
    try:
        characters = get_config_manager().load_characters()
        return str(characters.get("当前猫娘") or "").strip()
    except Exception:
        return ""


def _theater_root() -> Path:
    """解析小剧场私有运行目录。"""  # noqa: DOCSTRING_CJK
    return theater_root(get_config_manager())


def _validate_theater_local_mutation(request: Request, data: dict[str, Any]):
    """复用本地 mutation 校验，保护 theater 写接口不被裸 POST 调用。"""  # noqa: DOCSTRING_CJK
    from .system_router import _validate_local_mutation_request

    return _validate_local_mutation_request(
        request,
        payload=data,
        error_defaults={"ok": False, "reason": "csrf_validation_failed"},
    )


async def _speak_dialogue_with_claim(
    response: dict[str, Any],
    *,
    claim_dialogue: Callable[..., Awaitable[dict[str, Any]]],
    metadata_kind: str,
    request_prefix: str,
) -> dict[str, Any]:
    """复用同一 TTS 播放器，但由调用方选择剧本或自由 Session 认领器。"""  # noqa: DOCSTRING_CJK
    if response.get("ok") is not True:
        return {"ok": True, "skipped": "turn_failed"}
    session_id = str(response.get("session_id") or "")
    state_revision = response.get("state_revision")
    async def _play_claimed_dialogue(claim: dict[str, Any]) -> dict[str, Any]:
        """在 Runtime 持有角色边界期间，把已认领对白提交给现有 TTS。"""  # noqa: DOCSTRING_CJK
        lanlan_name = str(claim.get("lanlan_name") or "")
        if (_resolve_lanlan_name() or "Lan") != lanlan_name:
            # 认领后若配置中的当前猫娘已经变化，不再查找旧角色 Manager 或打断新角色音频。
            return {"ok": True, "skipped": "character_changed"}
        return await speak_committed_line(
            str(claim.get("line") or ""),
            session_id=session_id,
            state_revision=int(claim.get("state_revision") or 0),
            lanlan_name=lanlan_name,
            resolve_current_catgirl=_resolve_lanlan_name,
            get_session_manager=get_session_manager,
            metadata_kind=metadata_kind,
            request_id=f"{request_prefix}_{session_id}_{claim.get('state_revision')}",
        )

    return await claim_dialogue(
        _theater_root(),
        session_id=session_id,
        state_revision=state_revision,
        # 先在写入已朗读 revision 前绑定当前猫娘，阻止切换后继续认领旧角色对白。
        expected_lanlan_name=_resolve_lanlan_name() or "Lan",
        # 认领器会把认领、写盘和播放提交保持在同一个角色锁内，消除返回后的过期窗口。
        play=_play_claimed_dialogue,
    )


async def _speak_free_committed_dialogue(response: dict[str, Any]) -> dict[str, Any]:
    """把自由模式对白交给独立认领器，不读取剧本模式 Session。"""  # noqa: DOCSTRING_CJK
    return await _speak_dialogue_with_claim(
        response,
        claim_dialogue=free_runtime.claim_dialogue_speech,
        metadata_kind="theater_free_dialogue",
        request_prefix="theater_free_tts",
    )


@router.get("/stories")
async def list_theater_stories():
    """只读返回自由模式可用的背景种子列表，不暴露 Story v3 剧本运行接口。"""  # noqa: DOCSTRING_CJK
    # 自由模式仍可使用旧故事文件生成最小背景种子；这里不再把它当作剧本 Session 目录。
    stories = await story_loader.list_stories(lanlan_name=_resolve_lanlan_name() or "Lan")
    return {"ok": True, "stories": stories}


@router.post("/free/session/start")
async def start_free_theater_session(request: Request):
    """启动独立自由模式 Session，不触碰剧本模式 active 索引或账本。"""  # noqa: DOCSTRING_CJK
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    validation_error = _validate_theater_local_mutation(request, data)
    if validation_error is not None:
        return validation_error
    lanlan_name = _resolve_lanlan_name() or "Lan"
    result = await free_runtime.start_session(
        _theater_root(),
        lanlan_name=lanlan_name,
        story_id=data.get("story_id"),
        client_start_id=str(data.get("client_start_id") or ""),
        # role_card 只绑定当前 Free Session；普通剧本和全局角色卡目录不会读取它。
        role_card=data.get("role_card") if isinstance(data.get("role_card"), dict) else None,
        config_manager=get_config_manager(),
    )
    # 自由模式使用独立认领器接入现有播放器，不跨模式读取剧本 Session。
    try:
        await _speak_free_committed_dialogue(result)
    except Exception:
        logger.exception("Free theater TTS claim failed during session start")
    return result


@router.post("/free/session/input")
async def submit_free_theater_input(request: Request):
    """提交自由模式输入；服务端只保存沙盒 Session，不推进 Story v3。"""  # noqa: DOCSTRING_CJK
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    validation_error = _validate_theater_local_mutation(request, data)
    if validation_error is not None:
        return validation_error
    result = await free_runtime.submit_input(
        _theater_root(),
        session_id=str(data.get("session_id") or ""),
        message=str(data.get("message") or ""),
        input_kind=str(data.get("input_kind") or ""),
        choice_id=str(data.get("choice_id") or ""),
        client_turn_id=str(data.get("client_turn_id") or ""),
        base_revision=data.get("base_revision"),
        config_manager=get_config_manager(),
        expected_lanlan_name=_resolve_lanlan_name() or "Lan",
    )
    # 认领器只消费已提交的公开对白；失败时保留文字结果，不阻断自由回合。
    try:
        await _speak_free_committed_dialogue(result)
    except Exception:
        logger.exception("Free theater TTS claim failed during input")
    return result


@router.get("/free/session/state")
async def get_free_theater_session_state(session_id: str):
    """返回自由模式 Session 的公开快照，不读取剧本模式状态。"""  # noqa: DOCSTRING_CJK
    return await free_runtime.get_state(
        _theater_root(),
        session_id=str(session_id or ""),
        expected_lanlan_name=_resolve_lanlan_name() or "Lan",
    )


@router.get("/free/session/active")
async def get_active_free_theater_session_state():
    """恢复当前猫娘的自由模式 Session；独立于剧本模式 active 指针。"""  # noqa: DOCSTRING_CJK
    return await free_runtime.get_active_state(
        _theater_root(),
        lanlan_name=_resolve_lanlan_name() or "Lan",
    )
