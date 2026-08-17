"""Numeric v2 独立剧本 HTTP 接口。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

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
from services.theater.numeric_v2_performance import performance_dialogue
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


def _numeric_root(config_manager: Any) -> Path:
    """v2 复用统一剧场根目录，但只访问 numeric_v2 私有子目录。"""  # noqa: DOCSTRING_CJK
    return theater_root(config_manager)


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
    engine = (await _registry(config_manager)).load_engine(story_id)
    return NumericV2Runtime(engine, _numeric_root(config_manager))


def _current_catgirl_binding(config_manager: Any) -> dict[str, str]:
    """Session 只绑定服务端当前猫娘，客户端不能伪造人格。"""  # noqa: DOCSTRING_CJK

    return numeric_v2_catgirl_binding(config_manager)


def _ensure_current_catgirl(session: Any, config_manager: Any) -> None:
    if dict(session.catgirl_binding) != _current_catgirl_binding(config_manager):
        raise ValueError("catgirl_changed_requires_new_session")


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


def _scene_projection(runtime: NumericV2Runtime, stored: Any) -> dict[str, Any]:
    node = runtime.engine.nodes[stored.session.current_node_id]
    cast = NumericV2CastProjection.from_story(
        runtime.engine.story,
        player_name=str(stored.session.catgirl_binding.get("player_address") or "玩家"),
        catgirl_name=str(stored.session.catgirl_binding.get("catgirl_name") or "当前猫娘"),
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


def _numeric_payload(runtime: NumericV2Runtime, stored: Any) -> dict[str, Any]:
    latest = stored.session.performance_history[-1] if stored.session.performance_history else stored.session.opening_performance
    cast = NumericV2CastProjection.from_story(
        runtime.engine.story,
        player_name=str(stored.session.catgirl_binding.get("player_address") or "玩家"),
        catgirl_name=str(stored.session.catgirl_binding.get("catgirl_name") or "当前猫娘"),
    )
    return {
        "session": _public_session(stored.session),
        "story_intro": cast.intro(runtime.engine.story),
        "scene": _scene_projection(runtime, stored),
        "suggested_inputs": list(latest.get("suggested_inputs") or []),
    }


async def _speak_dialogue(
    config_manager: Any,
    *,
    session_id: str,
    revision: int,
    lanlan_name: str,
    dialogue: list[Mapping[str, Any]],
) -> None:
    text = "\n".join(str(line.get("text") or "").strip() for line in dialogue if line.get("speaker_id") == "active_catgirl" and str(line.get("text") or "").strip())
    if not text:
        return
    from main_routers.shared_state import get_session_manager

    def current_name() -> str:
        return _current_catgirl_binding(config_manager)["catgirl_name"]

    await speak_committed_line(
        text,
        session_id=session_id,
        state_revision=revision,
        lanlan_name=lanlan_name,
        resolve_current_catgirl=current_name,
        get_session_manager=get_session_manager,
        metadata_kind="theater_numeric_v2_dialogue",
        request_id=f"theater_numeric_v2_tts_{session_id}_{revision}",
    )


@router.get("/stories")
async def list_numeric_stories():
    try:
        registry = await _registry(get_config_manager())
        return {"ok": True, "stories": registry.list_packages()}
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
        summary = registry.import_package(payload)
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
        registry.load_engine(normalized_story_id)
        sessions = list_numeric_v2_sessions(
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
            registry.load_engine(normalized_story_id),
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
                binding_changed = dict(existing.session.catgirl_binding) != binding
                if binding_changed and not replace_existing:
                    _ensure_current_catgirl(existing.session, config_manager)
                if not binding_changed and not (replace_existing and existing.session.status == "ended"):
                    return {"ok": True, "resumed": True, **_numeric_payload(runtime, existing)}
                if session_id == existing.session.session_id:
                    return _error("numeric_replacement_session_id_must_differ", 400)
                # 同一角色卡配置变化时显式重建槽位；已结束的同绑定槽位可复用已提交开场。
                if existing.session.status == "ended" and not binding_changed:
                    # 同一角色重开同一剧本时复用已提交开场，避免一次无必要的模型调用
                    # 让“重新开始”受临时模型故障影响。
                    opening = existing.session.opening_performance
                else:
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
    await _speak_dialogue(
        config_manager,
        session_id=session_id,
        revision=0,
        lanlan_name=str(stored.session.catgirl_binding.get("catgirl_name") or ""),
        dialogue=performance_dialogue(opening),
    )
    return {"ok": True, **_numeric_payload(runtime, stored)}


@router.get("/session/active")
async def get_active_numeric_session(story_id: str):
    config_manager = get_config_manager()
    try:
        runtime = await _runtime_for_story(config_manager, str(story_id or "").strip())
        binding = _current_catgirl_binding(config_manager)
        stored = await runtime.restore_story_session(binding)
        if stored is None:
            return _error("numeric_session_not_found", 404)
        _ensure_current_catgirl(stored.session, config_manager)
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    except NumericV2StoreError as exc:
        return _error(str(exc), 422)
    except ValueError as exc:
        return _error(str(exc), 409)
    return {"ok": True, "resumed": True, **_numeric_payload(runtime, stored)}


@router.get("/session/{session_id}")
async def get_numeric_session(session_id: str, story_id: str):
    config_manager = get_config_manager()
    try:
        runtime = await _runtime_for_story(config_manager, str(story_id or "").strip())
        stored = await runtime.restore_session(session_id)
        if stored is None:
            return _error("numeric_session_not_found", 404)
        _ensure_current_catgirl(stored.session, config_manager)
    except (NumericV2PackageError, NumericV2PackageNotFoundError) as exc:
        return _package_error(exc)
    except NumericV2StoreError as exc:
        return _error(str(exc), 422)
    except ValueError as exc:
        return _error(str(exc), 409)
    return {"ok": True, **_numeric_payload(runtime, stored)}


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
        _ensure_current_catgirl(current.session, config_manager)
        # 幂等重放直接返回已提交快照，不能再次调用模型或重复结算。
        if turn.client_turn_id in current.session.processed_client_turn_ids:
            return {"ok": True, "idempotent_replay": True, **_numeric_payload(runtime, current)}
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
        _ensure_current_catgirl(current.session, config_manager)
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
    await _speak_dialogue(
        config_manager,
        session_id=session_id,
        revision=stored.session.revision,
        lanlan_name=str(stored.session.catgirl_binding.get("catgirl_name") or ""),
        dialogue=performance_dialogue(performance),
    )
    return {
        "ok": True,
        "resolved_turn": {
            "route_status": outcome.route_status,
            "route_changed": outcome.ledger_event["from_node_id"] != outcome.ledger_event["to_node_id"],
        },
        "performance": performance,
        **_numeric_payload(runtime, stored),
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
        _ensure_current_catgirl(current.session, config_manager)
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
    return {"ok": True, **_numeric_payload(runtime, stored)}


__all__ = ["router"]
