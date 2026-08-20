"""Numeric v2 独立剧本 HTTP 接口。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from main_routers.shared_state import get_config_manager
from main_routers.system_router._shared import _validate_local_mutation_request
from services.theater.numeric_v2_actor import (
    NumericV2Actor,
    NumericV2ActorError,
    NumericV2ActorUnavailableError,
)
from services.theater.numeric_v2_cast import NumericV2CastProjection
from services.theater.numeric_v2_identity import (
    numeric_v2_catgirl_binding,
    numeric_v2_character_ids,
)
from services.theater.numeric_v2_maintenance import (
    delete_numeric_v2_story_transactionally,
    maintain_numeric_v2_storage_once,
)
from services.theater.numeric_v2_performance import MAX_CONTENT_BLOCKS, performance_content_blocks
from services.theater.numeric_v2_archive import (
    NumericV2ArchiveError,
    NumericV2ArchiveStore,
    build_numeric_v2_memory_messages,
)
from services.theater.numeric_v2_evaluator import (
    NumericV2EvaluatorError,
    NumericV2EvaluatorUnavailableError,
    NumericV2MetricEvaluator,
)
from services.theater.numeric_v2_registry import (
    NumericV2PackageError,
    NumericV2PackageExistsError,
    NumericV2PackageNotFoundError,
    NumericV2PackageRegistry,
)
from services.theater.numeric_v2_runtime import (
    NumericV2DuplicateTurnError,
    NumericV2RevisionConflictError,
    NumericV2Runtime,
    NumericV2RuntimeError,
    TurnRequestV2,
)
from services.theater.paths import theater_root
from services.theater.numeric_v2_store import (
    list_numeric_v2_sessions,
    NumericV2SessionExistsError,
    NumericV2SessionNotFoundError,
    NumericV2StoreError,
    NumericV2StoreRevisionConflictError,
)
from services.theater.tts_bridge import speak_committed_line


router = APIRouter(prefix="/api/theater-numeric", tags=["theater-numeric-v2"])
_speak_request_locks: dict[str, asyncio.Lock] = {}
_speak_request_results: dict[str, dict[str, Any]] = {}
_archive_request_locks: dict[str, asyncio.Lock] = {}


def _performance_block_group(
    performance: Mapping[str, Any],
    block_index: int,
) -> tuple[list[dict[str, str]], int] | None:
    """定位内容块所属演绎段，避免把换场前后的对白合并成同一条语音。"""  # noqa: DOCSTRING_CJK

    raw_segments = performance.get("segments")
    containers = raw_segments if isinstance(raw_segments, list) else [performance]
    offset = 0
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        blocks = performance_content_blocks(container)
        if offset <= block_index < offset + len(blocks):
            return blocks, offset
        offset += len(blocks)
    return None


def _numeric_root(config_manager: Any) -> Path:
    """v2 复用统一剧场根目录，但只访问 numeric_v2 私有子目录。"""  # noqa: DOCSTRING_CJK
    return theater_root(config_manager)


def _archive_store(config_manager: Any) -> NumericV2ArchiveStore:
    return NumericV2ArchiveStore(_numeric_root(config_manager))


async def _registry(config_manager: Any) -> NumericV2PackageRegistry:
    numeric_root = _numeric_root(config_manager)
    registry = NumericV2PackageRegistry(numeric_root / "numeric_v2" / "packages")
    character_ids_by_name = await asyncio.to_thread(
        numeric_v2_character_ids,
        config_manager,
    )
    await asyncio.to_thread(
        maintain_numeric_v2_storage_once,
        numeric_root,
        registry,
        character_ids_by_name=character_ids_by_name,
    )
    return registry


async def _runtime_for_story(config_manager: Any, story_id: str) -> NumericV2Runtime:
    registry = await _registry(config_manager)
    engine = await asyncio.to_thread(registry.load_engine, story_id)
    return NumericV2Runtime(engine, _numeric_root(config_manager))


def _current_catgirl_binding(config_manager: Any) -> dict[str, str]:
    """Session 只绑定服务端当前猫娘，客户端不能伪造人格。"""  # noqa: DOCSTRING_CJK

    return numeric_v2_catgirl_binding(config_manager)


def _ensure_current_catgirl(session: Any, config_manager: Any) -> dict[str, str]:
    """只用不可变角色 ID 校验归属，允许同一角色改名或更新角色卡。"""  # noqa: DOCSTRING_CJK

    current_binding = _current_catgirl_binding(config_manager)
    if str(session.catgirl_binding.get("character_id") or "") != str(
        current_binding.get("character_id") or ""
    ):
        raise ValueError("catgirl_changed_requires_new_session")
    return current_binding


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _error(reason: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": reason}, status_code=status_code)


def _package_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, NumericV2PackageNotFoundError):
        return _error("numeric_story_not_found", 404)
    if isinstance(exc, NumericV2PackageError):
        return _error(str(exc), 422)
    return _error("numeric_story_load_failed", 500)


def _scene_projection(
    runtime: NumericV2Runtime,
    stored: Any,
    display_binding: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    node = runtime.engine.nodes[stored.session.current_node_id]
    binding = display_binding or stored.session.catgirl_binding
    cast = NumericV2CastProjection.from_story(
        runtime.engine.story,
        player_name=str(binding.get("player_address") or "玩家"),
        catgirl_name=str(binding.get("catgirl_name") or "当前猫娘"),
    )
    ending = None
    if node.get("type") == "ending" or node.get("terminal") is True:
        ending_id = str(node.get("ending_id") or "")
        raw = next((item for item in runtime.engine.story["endings"] if item["id"] == ending_id), None)
        if raw:
            ending = {
                "id": raw["id"],
                "title": cast.text(raw["title"]),
                "summary": cast.text(raw["summary"]),
            }
    return {
        "id": node["id"],
        "chapter": node["chapter"],
        "summary": cast.text(node["story_beat"]["summary"]),
        "terminal": bool(ending),
        "ending": ending,
        "node_turn_count": stored.session.node_turn_count,
        "min_turns": int(node.get("min_turns") or 0),
    }


def _public_session(session: Any) -> dict[str, Any]:
    """隐藏 metric 原始值，只向页面提供恢复和回放需要的字段。"""  # noqa: DOCSTRING_CJK

    return {
        "schema": session.to_dict()["schema"],
        "session_id": session.session_id,
        "story_package_id": session.story_package_id,
        "story_package_revision": session.story_package_revision,
        "story_package_hash": session.story_package_hash,
        "current_node_id": session.current_node_id,
        "node_turn_count": session.node_turn_count,
        "revision": session.revision,
        "status": session.status,
        "opening_performance": session.opening_performance,
        "performance_history": list(session.performance_history),
        "ended_reason": session.ended_reason,
    }


def _numeric_payload(
    runtime: NumericV2Runtime,
    stored: Any,
    *,
    end_receipt: Mapping[str, Any] | None = None,
    display_binding: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    latest = stored.session.performance_history[-1] if stored.session.performance_history else stored.session.opening_performance
    binding = display_binding or stored.session.catgirl_binding
    cast = NumericV2CastProjection.from_story(
        runtime.engine.story,
        player_name=str(binding.get("player_address") or "玩家"),
        catgirl_name=str(binding.get("catgirl_name") or "当前猫娘"),
    )
    payload = {
        "session": _public_session(stored.session),
        "story_title": str(runtime.engine.story["meta"]["title"]),
        # 历史区署名使用当前展示绑定；不要让前端从本地文案猜玩家或猫娘名称。
        "participants": {
            "player_name": str(binding.get("player_address") or "玩家"),
            "catgirl_name": str(binding.get("catgirl_name") or "当前猫娘"),
        },
        "story_intro": cast.intro(runtime.engine.story),
        "scene": _scene_projection(runtime, stored, binding),
        "suggested_inputs": list(latest.get("suggested_inputs") or []),
    }
    if end_receipt:
        payload["end_receipt_id"] = str(end_receipt.get("receipt_id") or "")
        payload["archive_status"] = str(end_receipt.get("status") or "pending")
        payload["archive_request_id"] = str(end_receipt.get("archive_request_id") or "")
    return payload


def _list_story_summaries(
    registry: NumericV2PackageRegistry,
    binding: Mapping[str, str],
) -> list[dict[str, Any]]:
    """在线程中完成整批剧本读取，避免逐包文件 IO 阻塞事件循环。"""  # noqa: DOCSTRING_CJK

    stories: list[dict[str, Any]] = []
    for summary in registry.list_packages():
        engine = registry.load_engine(str(summary.get("story_id") or ""))
        cast = NumericV2CastProjection.from_story(
            engine.story,
            player_name=binding.get("player_address") or "玩家",
            catgirl_name=binding.get("catgirl_name") or "当前猫娘",
        )
        stories.append({**summary, "display_intro": cast.intro(engine.story)})
    return stories


@router.get("/stories")
async def list_numeric_stories():
    try:
        config_manager = get_config_manager()
        registry = await _registry(config_manager)
        binding = _current_catgirl_binding(config_manager)
        stories = await asyncio.to_thread(_list_story_summaries, registry, binding)
        return {"ok": True, "stories": stories}
    except Exception:
        return _error("numeric_story_list_failed", 500)


@router.post("/packages/import")
async def import_numeric_story(request: Request):
    payload = await _json_object(request)
    validation_error = _validate_local_mutation_request(request, payload=payload, error_defaults={"ok": False, "reason": "csrf_validation_failed"})
    if validation_error is not None:
        return validation_error
    try:
        registry = await _registry(get_config_manager())
        summary = await asyncio.to_thread(registry.import_package, payload)
    except NumericV2PackageExistsError:
        return _error("numeric_story_exists", 409)
    except NumericV2PackageError as exc:
        return _package_error(exc)
    except (OSError, UnicodeError):
        return _error("numeric_story_import_failed", 500)
    return {"ok": True, "package": summary}


@router.get("/packages/{story_id}/delete-preview")
async def preview_numeric_story_delete(story_id: str):
    config_manager = get_config_manager()
    normalized_story_id = str(story_id or "").strip()
    try:
        registry = await _registry(config_manager)
        await asyncio.to_thread(registry.load_engine, normalized_story_id)
        sessions = await asyncio.to_thread(
            list_numeric_v2_sessions,
            _numeric_root(config_manager),
            story_id=normalized_story_id,
        )
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    active_catgirl_names = sorted(
        {
            item["catgirl_name"]
            for item in sessions
            if item["status"] != "ended" and item["catgirl_name"]
        }
    )
    return {
        "ok": True,
        "active_catgirl_names": active_catgirl_names,
        "session_count": len(sessions),
    }


@router.delete("/packages/{story_id}")
async def delete_numeric_story(story_id: str, request: Request):
    validation_error = _validate_local_mutation_request(
        request,
        error_defaults={"ok": False, "reason": "csrf_validation_failed"},
    )
    if validation_error is not None:
        return validation_error
    config_manager = get_config_manager()
    normalized_story_id = str(story_id or "").strip()
    try:
        registry = await _registry(config_manager)
        runtime = NumericV2Runtime(
            await asyncio.to_thread(registry.load_engine, normalized_story_id),
            _numeric_root(config_manager),
        )
        async with runtime.story_session_guard():
            deleted_session_count = await delete_numeric_v2_story_transactionally(
                _numeric_root(config_manager),
                registry,
                normalized_story_id,
            )
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    except NumericV2StoreError as exc:
        return _error(str(exc), 500)
    return {"ok": True, "deleted_session_count": deleted_session_count}


@router.post("/session/start")
async def start_numeric_session(request: Request):
    payload = await _json_object(request)
    validation_error = _validate_local_mutation_request(request, payload=payload, error_defaults={"ok": False, "reason": "csrf_validation_failed"})
    if validation_error is not None:
        return validation_error
    story_id = str(payload.get("story_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not story_id or not session_id:
        return _error("story_id_and_session_id_required", 400)
    config_manager = get_config_manager()
    replace_existing = payload.get("replace_existing") is True
    try:
        runtime = await _runtime_for_story(config_manager, story_id)
        async with runtime.story_session_guard():
            binding = _current_catgirl_binding(config_manager)
            existing = await runtime.restore_story_session_unlocked(binding)
            if existing is not None:
                _ensure_current_catgirl(existing.session, config_manager)
                # 同一 character_id 的改名或角色卡更新只能刷新演绎上下文，不能替换进度。
                if not replace_existing:
                    return {
                        "ok": True,
                        "resumed": True,
                        **_numeric_payload(runtime, existing, display_binding=binding),
                    }
                if session_id == existing.session.session_id:
                    return _error("numeric_replacement_session_id_must_differ", 400)
                if existing.session.status != "ended":
                    return _error("numeric_active_session_cannot_restart", 409)
                # 重新开始必须生成新开场和新推荐输入；生成成功前不替换旧 Session。
                opening = await NumericV2Actor(config_manager).generate_opening(engine=runtime.engine)
                if _current_catgirl_binding(config_manager) != binding:
                    raise ValueError("catgirl_changed_requires_new_session")
                stored = await runtime.replace_active_session(
                    previous_session_id=existing.session.session_id,
                    session_id=session_id,
                    catgirl_binding=binding,
                    opening_performance=opening,
                )
            else:
                # 开场 Actor 成功后才创建 Session，避免空壳 Session 污染恢复指针。
                opening = await NumericV2Actor(config_manager).generate_opening(engine=runtime.engine)
                if _current_catgirl_binding(config_manager) != binding:
                    raise ValueError("catgirl_changed_requires_new_session")
                stored = await runtime.start_session(
                    session_id=session_id,
                    catgirl_binding=binding,
                    opening_performance=opening,
                )
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    except NumericV2SessionExistsError:
        return _error("numeric_session_exists", 409)
    except NumericV2ActorUnavailableError:
        return _error("numeric_v2_actor_unavailable", 503)
    except NumericV2ActorError:
        return _error("numeric_v2_actor_failed", 502)
    except ValueError as exc:
        if str(exc) == "catgirl_changed_requires_new_session":
            return _error(str(exc), 409)
        return _error(str(exc), 400)
    except (NumericV2StoreError, NumericV2RuntimeError) as exc:
        return _error(str(exc), 400)
    return {"ok": True, **_numeric_payload(runtime, stored, display_binding=binding)}


@router.get("/session/active")
async def get_active_numeric_session(story_id: str):
    config_manager = get_config_manager()
    try:
        runtime = await _runtime_for_story(config_manager, str(story_id or "").strip())
        binding = _current_catgirl_binding(config_manager)
        stored = await runtime.restore_story_session(binding)
        if stored is None:
            return _error("numeric_session_not_found", 404)
        binding = _ensure_current_catgirl(stored.session, config_manager)
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    except NumericV2StoreError as exc:
        return _error(str(exc), 422)
    except ValueError as exc:
        return _error(str(exc), 409)
    receipt = (
        await _archive_store(config_manager).acreate_or_get(stored.session)
        if stored.session.status == "ended"
        else None
    )
    return {
        "ok": True,
        "resumed": True,
        **_numeric_payload(
            runtime,
            stored,
            end_receipt=receipt,
            display_binding=binding,
        ),
    }


@router.get("/session/{session_id}")
async def get_numeric_session(session_id: str, story_id: str):
    config_manager = get_config_manager()
    try:
        runtime = await _runtime_for_story(config_manager, str(story_id or "").strip())
        stored = await runtime.restore_session(session_id)
        if stored is None:
            return _error("numeric_session_not_found", 404)
        binding = _ensure_current_catgirl(stored.session, config_manager)
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    except NumericV2StoreError as exc:
        return _error(str(exc), 422)
    except ValueError as exc:
        return _error(str(exc), 409)
    receipt = (
        await _archive_store(config_manager).acreate_or_get(stored.session)
        if stored.session.status == "ended"
        else None
    )
    return {
        "ok": True,
        **_numeric_payload(
            runtime,
            stored,
            end_receipt=receipt,
            display_binding=binding,
        ),
    }


@router.post("/session/input")
async def submit_numeric_input(request: Request):
    payload = await _json_object(request)
    validation_error = _validate_local_mutation_request(request, payload=payload, error_defaults={"ok": False, "reason": "csrf_validation_failed"})
    if validation_error is not None:
        return validation_error
    story_id = str(payload.get("story_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not story_id or not session_id:
        return _error("story_id_and_session_id_required", 400)
    try:
        turn = TurnRequestV2.from_mapping(payload)
        config_manager = get_config_manager()
        runtime = await _runtime_for_story(config_manager, story_id)
        current = await runtime.restore_session(session_id)
        if current is None:
            return _error("numeric_session_not_found", 404)
        current_binding = _ensure_current_catgirl(current.session, config_manager)
        # 幂等重放直接返回已提交快照，不能再次调用模型或重复结算。
        if turn.client_turn_id in current.session.processed_client_turn_ids:
            return {
                "ok": True,
                "idempotent_replay": True,
                **_numeric_payload(
                    runtime,
                    current,
                    display_binding=current_binding,
                ),
            }
        if current.session.status == "ended":
            return _error("session_already_ended", 409)
        if turn.base_revision != current.session.revision:
            return _error("numeric_base_revision_mismatch", 409)
        evaluation = await NumericV2MetricEvaluator(config_manager).evaluate(
            engine=runtime.engine,
            session=current.session,
            message=turn.message,
        )
        outcome = runtime.prepare_turn(
            current,
            turn,
            evaluation.metric_changes,
            scene_complete=evaluation.scene_complete,
        )
        performance = await NumericV2Actor(config_manager).generate_turn(
            engine=runtime.engine,
            session=current.session,
            outcome=outcome,
            player_input=turn.message,
        )
        current_binding = _ensure_current_catgirl(current.session, config_manager)
        # 删除剧本事务与正式提交共享 story guard；模型调用不占锁，删除若先
        # 完成则本次提交安全失败，删除若回滚则提交基于恢复后的文件继续。
        async with runtime.story_session_guard():
            stored = await runtime.commit_turn(outcome, performance)
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    except (NumericV2RevisionConflictError, NumericV2StoreRevisionConflictError) as exc:
        if str(exc) == "session_already_ended":
            return _error("session_already_ended", 409)
        return _error("numeric_base_revision_mismatch", 409)
    except NumericV2DuplicateTurnError:
        return _error("numeric_duplicate_client_turn_id", 409)
    except NumericV2SessionNotFoundError:
        return _error("numeric_session_not_found", 404)
    except NumericV2EvaluatorUnavailableError:
        return _error("numeric_v2_evaluator_unavailable", 503)
    except NumericV2EvaluatorError:
        return _error("numeric_v2_evaluator_failed", 502)
    except NumericV2ActorUnavailableError:
        return _error("numeric_v2_actor_unavailable", 503)
    except NumericV2ActorError:
        return _error("numeric_v2_actor_failed", 502)
    except ValueError as exc:
        if str(exc) == "catgirl_changed_requires_new_session":
            return _error(str(exc), 409)
        return _error(str(exc), 400)
    except (NumericV2RuntimeError, NumericV2StoreError) as exc:
        return _error(str(exc), 400)
    end_receipt = None
    if stored.session.status == "ended":
        end_receipt = await _archive_store(config_manager).acreate_or_get(stored.session)
    return {
        "ok": True,
        "resolved_turn": {
            "route_status": outcome.route_status,
            "route_changed": outcome.ledger_event["from_node_id"] != outcome.ledger_event["to_node_id"],
        },
        "performance": performance,
        **_numeric_payload(
            runtime,
            stored,
            end_receipt=end_receipt,
            display_binding=current_binding,
        ),
    }


@router.post("/session/end")
async def end_numeric_session(request: Request):
    payload = await _json_object(request)
    validation_error = _validate_local_mutation_request(request, payload=payload, error_defaults={"ok": False, "reason": "csrf_validation_failed"})
    if validation_error is not None:
        return validation_error
    story_id = str(payload.get("story_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    base_revision = payload.get("base_revision")
    if not story_id or not session_id or isinstance(base_revision, bool) or not isinstance(base_revision, int):
        return _error("numeric_session_end_request_invalid", 400)
    try:
        config_manager = get_config_manager()
        runtime = await _runtime_for_story(config_manager, story_id)
        current = await runtime.restore_session(session_id)
        if current is None:
            return _error("numeric_session_not_found", 404)
        # 结束操作同样复验当前猫娘，避免角色切换后继续改写旧人格 Session。
        current_binding = _ensure_current_catgirl(current.session, config_manager)
        async with runtime.story_session_guard():
            stored = await runtime.end_session(
                session_id,
                base_revision=base_revision,
                reason="user_exit",
            )
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    except NumericV2StoreRevisionConflictError:
        return _error("numeric_base_revision_mismatch", 409)
    except NumericV2SessionNotFoundError:
        return _error("numeric_session_not_found", 404)
    except ValueError as exc:
        return _error(str(exc), 409)
    except (NumericV2StoreError, NumericV2RuntimeError) as exc:
        return _error(str(exc), 400)
    receipt = await _archive_store(config_manager).acreate_or_get(stored.session)
    return {
        "ok": True,
        **_numeric_payload(
            runtime,
            stored,
            end_receipt=receipt,
            display_binding=current_binding,
        ),
    }


@router.post("/session/resume")
async def resume_numeric_session(request: Request):
    """继续玩家主动退出的演绎；剧情自然结局不能从该入口恢复。"""  # noqa: DOCSTRING_CJK

    payload = await _json_object(request)
    validation_error = _validate_local_mutation_request(
        request,
        payload=payload,
        error_defaults={"ok": False, "reason": "csrf_validation_failed"},
    )
    if validation_error is not None:
        return validation_error
    story_id = str(payload.get("story_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    base_revision = payload.get("base_revision")
    if not story_id or not session_id or isinstance(base_revision, bool) or not isinstance(base_revision, int):
        return _error("numeric_session_resume_request_invalid", 400)
    try:
        config_manager = get_config_manager()
        runtime = await _runtime_for_story(config_manager, story_id)
        current = await runtime.restore_session(session_id)
        if current is None:
            return _error("numeric_session_not_found", 404)
        current_binding = _ensure_current_catgirl(current.session, config_manager)
        async with runtime.story_session_guard():
            stored = await runtime.resume_session(
                session_id,
                base_revision=base_revision,
            )
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    except NumericV2StoreRevisionConflictError:
        return _error("numeric_base_revision_mismatch", 409)
    except NumericV2SessionNotFoundError:
        return _error("numeric_session_not_found", 404)
    except ValueError as exc:
        return _error(str(exc), 409)
    except (NumericV2StoreError, NumericV2RuntimeError) as exc:
        return _error(str(exc), 409)
    return {
        "ok": True,
        "resumed": True,
        **_numeric_payload(runtime, stored, display_binding=current_binding),
    }


@router.post("/session/speak-block")
async def speak_numeric_block(request: Request):
    payload = await _json_object(request)
    validation_error = _validate_local_mutation_request(
        request,
        payload=payload,
        error_defaults={"ok": False, "reason": "csrf_validation_failed"},
    )
    if validation_error is not None:
        return validation_error
    story_id = str(payload.get("story_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    playback_request_id = str(payload.get("playback_request_id") or "").strip()
    revision = payload.get("revision")
    block_index = payload.get("block_index")
    dialogue_block_indexes = payload.get("dialogue_block_indexes")
    if (
        not story_id
        or not session_id
        or not playback_request_id
        or len(playback_request_id) > 160
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or isinstance(block_index, bool)
        or not isinstance(block_index, int)
        or block_index < 0
        or (
            dialogue_block_indexes is not None
            and (
                not isinstance(dialogue_block_indexes, list)
                or not 1 <= len(dialogue_block_indexes) <= MAX_CONTENT_BLOCKS
                or any(
                    isinstance(index, bool) or not isinstance(index, int) or index < 0
                    for index in dialogue_block_indexes
                )
                or dialogue_block_indexes != sorted(set(dialogue_block_indexes))
                or dialogue_block_indexes[0] != block_index
            )
        )
    ):
        return _error("numeric_speak_block_request_invalid", 400)
    lock = _speak_request_locks.setdefault(playback_request_id, asyncio.Lock())
    async with lock:
        previous = _speak_request_results.get(playback_request_id)
        if previous is not None:
            return dict(previous)
        try:
            config_manager = get_config_manager()
            runtime = await _runtime_for_story(config_manager, story_id)
            stored = await runtime.restore_session(session_id)
            if stored is None:
                return _error("numeric_session_not_found", 404)
            current_binding = _ensure_current_catgirl(stored.session, config_manager)
            if stored.session.revision != revision:
                return _error("numeric_base_revision_mismatch", 409)
            if revision == 0:
                performance = stored.session.opening_performance
            else:
                history_index = revision - 1
                if history_index >= len(stored.session.performance_history):
                    return _error("numeric_speak_block_not_found", 404)
                performance = stored.session.performance_history[history_index]
                if int(performance.get("revision") or -1) != revision:
                    return _error("numeric_speak_block_not_found", 404)
            block_group = _performance_block_group(performance, block_index)
            if block_group is None:
                return _error("numeric_speak_block_not_found", 404)
            blocks, block_offset = block_group
            local_block_index = block_index - block_offset
            block = blocks[local_block_index]
            if block.get("type") != "dialogue" or block.get("speaker_id") != "active_catgirl":
                return _error("numeric_speak_block_not_dialogue", 422)
            requested_indexes = dialogue_block_indexes or [block_index]
            dialogue_parts: list[str] = []
            for requested_index in requested_indexes:
                requested_group = _performance_block_group(performance, requested_index)
                if requested_group is None:
                    return _error("numeric_speak_block_not_found", 404)
                requested_blocks, requested_offset = requested_group
                if requested_offset != block_offset:
                    return _error("numeric_speak_blocks_cross_segment", 422)
                requested_block = requested_blocks[requested_index - requested_offset]
                if (
                    requested_block.get("type") != "dialogue"
                    or requested_block.get("speaker_id") != "active_catgirl"
                ):
                    return _error("numeric_speak_block_not_dialogue", 422)
                dialogue_parts.append(str(requested_block.get("text") or "").strip())
            # 括号动作已经被内容块解析器剔除；这里只合并前端声明且后端复验通过的对白块。
            dialogue_text = " ".join(dialogue_parts).strip()
            all_blocks = performance_content_blocks(performance)
            first_dialogue_index = next(
                (index for index, item in enumerate(all_blocks) if item.get("type") == "dialogue"),
                block_index,
            )
            from main_routers.shared_state import get_session_manager

            result = await speak_committed_line(
                dialogue_text,
                session_id=session_id,
                state_revision=revision,
                # TTS 必须使用 character_id 解析出的当前名称；角色改名后旧名称已无语音配置。
                lanlan_name=str(current_binding.get("catgirl_name") or ""),
                resolve_current_catgirl=lambda: _current_catgirl_binding(config_manager)["catgirl_name"],
                get_session_manager=get_session_manager,
                metadata_kind="theater_numeric_v2_dialogue_block",
                request_id=playback_request_id,
                interrupt_audio=block_index == first_dialogue_index,
            )
            response = {
                "ok": True,
                "block_index": block_index,
                "dialogue_block_count": len(dialogue_parts),
                **result,
            }
            if block_index == first_dialogue_index:
                response["first_dialogue"] = True
            _speak_request_results[playback_request_id] = response
            if len(_speak_request_results) > 512:
                expired_request_id = next(iter(_speak_request_results))
                _speak_request_results.pop(expired_request_id)
                _speak_request_locks.pop(expired_request_id, None)
            return response
        except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
            return _package_error(exc)
        except (NumericV2StoreError, NumericV2RuntimeError, ValueError) as exc:
            return _error(str(exc), 409)


async def _validated_receipt(
    store: NumericV2ArchiveStore,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """异步读取并校验客户端只能引用本次已结束 Session 的回执。"""  # noqa: DOCSTRING_CJK

    receipt = await store.aload(str(payload.get("end_receipt_id") or "").strip())
    if receipt is None:
        raise NumericV2ArchiveError("numeric_end_receipt_not_found")
    for field, value in (
        ("story_id", str(payload.get("story_id") or "").strip()),
        ("session_id", str(payload.get("session_id") or "").strip()),
        ("revision", payload.get("revision")),
    ):
        if receipt.get(field) != value:
            raise NumericV2ArchiveError("numeric_end_receipt_mismatch")
    return receipt


@router.post("/session/archive")
async def archive_numeric_session(request: Request):
    payload = await _json_object(request)
    validation_error = _validate_local_mutation_request(
        request,
        payload=payload,
        error_defaults={"ok": False, "reason": "csrf_validation_failed"},
    )
    if validation_error is not None:
        return validation_error
    archive_request_id = str(payload.get("archive_request_id") or "").strip()
    if not archive_request_id or len(archive_request_id) > 160:
        return _error("numeric_archive_request_invalid", 400)
    lock = _archive_request_locks.setdefault(str(payload.get("end_receipt_id") or ""), asyncio.Lock())
    async with lock:
        store: NumericV2ArchiveStore | None = None
        receipt: dict[str, Any] | None = None
        try:
            config_manager = get_config_manager()
            store = _archive_store(config_manager)
            receipt = await _validated_receipt(store, payload)
            if receipt.get("status") == "written":
                return {"ok": True, "status": "already_written"}
            if receipt.get("status") == "skipped":
                return _error("numeric_archive_already_skipped", 409)
            expected_request_id = str(receipt.get("archive_request_id") or "")
            if expected_request_id and archive_request_id != expected_request_id:
                return _error("numeric_archive_request_mismatch", 409)
            runtime = await _runtime_for_story(config_manager, receipt["story_id"])
            stored = await runtime.restore_session(receipt["session_id"])
            if stored is None:
                return _error("numeric_session_not_found", 404)
            current_binding = _ensure_current_catgirl(stored.session, config_manager)
            if receipt.get("character_id") != current_binding.get("character_id"):
                return _error("numeric_end_receipt_character_mismatch", 409)
            if stored.session.status != "ended" or stored.session.revision != receipt["revision"]:
                return _error("numeric_archive_session_not_ended", 409)
            public = _numeric_payload(
                runtime,
                stored,
                display_binding=current_binding,
            )
            title = str(runtime.engine.story["meta"]["title"])
            messages = build_numeric_v2_memory_messages(
                title=title,
                session=stored.session,
                ending=public["scene"].get("ending"),
            )
            from config import MEMORY_SERVER_PORT
            from utils.internal_http_client import get_internal_http_client

            receipt = await store.aupdate(
                receipt,
                status="writing",
                archive_request_id=archive_request_id,
            )
            response = await get_internal_http_client().post(
                f"http://127.0.0.1:{MEMORY_SERVER_PORT}/cache/{quote(current_binding['catgirl_name'], safe='')}",
                # 记忆服务使用同一稳定键收敛未知响应和跨进程重试，不能只在剧场侧去重。
                json={
                    "input_history": json.dumps(messages, ensure_ascii=False),
                    "idempotency_key": archive_request_id,
                },
                timeout=8.0,
            )
            data = response.json() if response.content else {}
            if not response.is_success or data.get("status") == "error":
                await store.aupdate(receipt, status="pending")
                return _error("numeric_archive_memory_failed", 502)
            await store.aupdate(receipt, status="written", archive_request_id=archive_request_id)
            return {"ok": True, "status": "written", "count": data.get("count")}
        except NumericV2ArchiveError as exc:
            return _error(str(exc), 409)
        except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
            return _package_error(exc)
        except Exception:
            if store is not None and receipt is not None and receipt.get("status") == "writing":
                try:
                    await store.aupdate(receipt, status="pending")
                except NumericV2ArchiveError:
                    pass
            return _error("numeric_archive_memory_failed", 502)


@router.post("/session/archive/skip")
async def skip_numeric_session_archive(request: Request):
    payload = await _json_object(request)
    validation_error = _validate_local_mutation_request(
        request,
        payload=payload,
        error_defaults={"ok": False, "reason": "csrf_validation_failed"},
    )
    if validation_error is not None:
        return validation_error
    lock = _archive_request_locks.setdefault(str(payload.get("end_receipt_id") or ""), asyncio.Lock())
    async with lock:
        try:
            store = _archive_store(get_config_manager())
            receipt = await _validated_receipt(store, payload)
            if receipt.get("status") == "written":
                return _error("numeric_archive_already_written", 409)
            await store.aupdate(receipt, status="skipped")
            return {"ok": True, "status": "skipped"}
        except NumericV2ArchiveError as exc:
            return _error(str(exc), 409)


__all__ = ["router"]
