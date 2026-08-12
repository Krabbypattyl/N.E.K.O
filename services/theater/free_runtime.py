"""实现不写入作者剧情图的自由模式沙盒生命周期。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Awaitable, Callable

from utils.tokenize import truncate_to_tokens

# 自由 Runtime 单独依赖 Free Seed 投影，完整 Story 只用于来源校验和 revision 恢复。
from . import (
    fact_lifecycle,
    free_seed,
    free_role_card,
    llm,
    session_store,
    story_loader,
)
from .llm_context import (
    THEATER_PERSONA_MAX_CHARS,
    _load_character_profile,
    _load_player_address,
)


# 自由模式沿用现有原子 Session 存储，但放在独立 free 子目录，避免与剧本模式
# 的 active 索引、Session 文件和状态恢复互相覆盖。
FREE_SESSION_ROOT_NAME = "free"
MAX_FREE_TURNS = 64
MAX_IDEMPOTENT_RESULTS = 32
# 只保留近期自由模式对白的 TTS 认领 revision，避免网络重试造成重复播放。
MAX_SPOKEN_DIALOGUE_REVISIONS = 32


def _free_root(root: Path) -> Path:
    """返回自由模式专属运行根；正式剧本 Session 不会读取该目录。"""  # noqa: DOCSTRING_CJK
    return Path(root) / FREE_SESSION_ROOT_NAME


async def publish_character_switch(
    root: Path,
    *,
    old_lanlan_name: str,
    publish: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    """在自由模式自己的 Session 边界内发布猫娘切换。"""  # noqa: DOCSTRING_CJK
    # Numeric v2 没有共享 active 索引；这里仅处理自由模式沙盒，避免旧剧本 Runtime 借尸还魂。
    free_root = _free_root(root)
    normalized_name = str(old_lanlan_name or "").strip()
    while True:
        active_session_id = await session_store.get_active_session_id(
            free_root, normalized_name
        )
        if not active_session_id:
            async with session_store.character_guard(free_root, normalized_name):
                # 等锁期间可能创建了新的自由 Session，发现变化后按完整锁顺序重试。
                if await session_store.get_active_session_id(free_root, normalized_name):
                    continue
                await publish()
                return {"ok": True, "published": True, "cleared": False}

        async with session_store.session_guard(active_session_id):
            async with session_store.character_guard(free_root, normalized_name):
                if (
                    await session_store.get_active_session_id(free_root, normalized_name)
                    != active_session_id
                ):
                    continue
                await publish()
                try:
                    session = await session_store.load_session(free_root, active_session_id)
                    if session is not None:
                        now = _now_ms()
                        session["ended_at"] = session.get("ended_at") or now
                        session["end_reason"] = session.get("end_reason") or "character_switch"
                        session["updated_at"] = now
                        snapshot = session.get("public_snapshot")
                        if isinstance(snapshot, dict):
                            snapshot["can_resume"] = False
                            snapshot["session_lifecycle"] = "ended"
                            snapshot["ending"] = {
                                "should_offer_ending": False,
                                "should_end_session": True,
                                "reason": "character_switch",
                                "ending_type": "none",
                            }
                        await session_store.save_session(free_root, session)
                    await session_store.clear_active_session(
                        free_root, normalized_name, active_session_id
                    )
                except Exception as exc:
                    # 新猫娘已经发布成功时不能重复写配置，只记录旧自由 Session 清理失败。
                    return {
                        "ok": True,
                        "published": True,
                        "cleared": False,
                        "cleanup_error": type(exc).__name__,
                    }
                return {
                    "ok": True,
                    "published": True,
                    "cleared": True,
                    "session_id": active_session_id,
                }


async def clear_character_session(root: Path, *, lanlan_name: str) -> dict[str, Any]:
    """角色切换时结束自由模式活动 Session，不触碰 Numeric v2。"""  # noqa: DOCSTRING_CJK
    free_root = _free_root(root)
    session_id = await session_store.get_active_session_id(free_root, lanlan_name)
    if not session_id:
        return {"ok": True, "cleared": False}
    async with session_store.session_guard(session_id):
        async with session_store.character_guard(free_root, lanlan_name):
            session = await session_store.load_session(free_root, session_id)
            if session is not None:
                now = _now_ms()
                session["ended_at"] = session.get("ended_at") or now
                session["end_reason"] = session.get("end_reason") or "character_switch"
                session["updated_at"] = now
                snapshot = session.get("public_snapshot")
                if isinstance(snapshot, dict):
                    snapshot["can_resume"] = False
                    snapshot["session_lifecycle"] = "ended"
                await session_store.save_session(free_root, session)
            await session_store.clear_active_session(free_root, lanlan_name, session_id)
    return {"ok": True, "cleared": True, "session_id": session_id}


def _now_ms() -> int:
    """使用毫秒时间戳写入 Session 生命周期字段。"""  # noqa: DOCSTRING_CJK
    return int(time.time() * 1000)


def _scenario_card(story: dict[str, Any]) -> dict[str, str]:
    """只提取自由模式需要的公开身份卡，不把作者内部字段送进沙盒。"""  # noqa: DOCSTRING_CJK
    card = story.get("scenario_card") if isinstance(story.get("scenario_card"), dict) else {}
    return {
        "player_role": str(card.get("player_role") or "故事参与者"),
        "catgirl_role": str(card.get("catgirl_role") or "当前故事中的共同主角"),
        "primary_goal": str(card.get("primary_goal") or ""),
    }


def _build_default_role_card(
    seed: dict[str, Any],
    *,
    lanlan_name: str,
    config_manager: Any | None,
) -> dict[str, Any]:
    """没有额外角色卡时，用 Story 的公开开场生成 RP-Hub 兼容的临时卡。"""  # noqa: DOCSTRING_CJK
    card = seed.get("scenario_card") if isinstance(seed.get("scenario_card"), dict) else {}
    opening = seed.get("opening_scene") if isinstance(seed.get("opening_scene"), dict) else {}
    player_address = _load_player_address(config_manager)
    fallback_player = str(card.get("player_role") or "玩家").strip()
    return {
        "schema_version": free_role_card.FREE_ROLE_CARD_SCHEMA_VERSION,
        "name": str(lanlan_name or "Lan").strip() or "Lan",
        "description": f"{str(lanlan_name or 'Lan').strip() or 'Lan'}是当前猫娘，也是本次自由演绎的主角。",
        # 自由模式也必须避免首次 tokenizer 下载阻塞开场请求。
        "personality": _load_character_profile(
            config_manager,
            lanlan_name,
            max_chars=THEATER_PERSONA_MAX_CHARS,
        ),
        "first_mes": str(opening.get("text") or "").strip(),
        "scenario": str(seed.get("theme") or "").strip(),
        "mes_example": "",
        "world_info": [],
        # 玩家称呼由当前猫娘配置决定；测试环境没有配置时才回退故事身份。
        "player_address": player_address or fallback_player,
        "player_role": player_address or fallback_player,
        "story_title": str(seed.get("title") or "").strip(),
        "scenario_title": str(opening.get("title") or "").strip(),
    }


def _public_role_card(role_card: Any) -> dict[str, str] | None:
    """只投影临时角色卡的展示字段，避免把世界书和内部卡片字段返回前端。"""  # noqa: DOCSTRING_CJK
    if not isinstance(role_card, dict):
        return None
    return {
        "name": str(role_card.get("name") or ""),
        "description": str(role_card.get("description") or ""),
        "story_title": str(role_card.get("story_title") or ""),
        "scenario_title": str(role_card.get("scenario_title") or ""),
        "scenario": str(role_card.get("scenario") or ""),
        "player_address": str(role_card.get("player_address") or ""),
        "player_role": str(role_card.get("player_role") or ""),
    }


def _scene_for_session(story: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """按自由 Session 固定的场景 ID恢复同一故事种子，不猜测后续节点。"""  # noqa: DOCSTRING_CJK
    # 临时角色卡可以替换展示用开场 Scene，但续写必须回到来源 Story 的原始 Scene。
    scene_id = str(
        session.get("source_scene_id")
        or session.get("scene_id")
        or story.get("initial_scene_id")
        or ""
    )
    return story_loader.scene_by_id(story, scene_id)


def _initial_node_context(seed: dict[str, Any]) -> dict[str, Any]:
    """保存自由种子的开场摘要，不读取作者 Node 或剧情边。"""  # noqa: DOCSTRING_CJK
    scene = seed.get("opening_scene")
    scene = scene if isinstance(scene, dict) else {}
    return {
        # 自由模式没有可推进的作者 Node，因此不伪造稳定 Node ID。
        "node_id": "",
        "title": str(scene.get("title") or ""),
        "summary": str(scene.get("text") or ""),
    }


def _ending(*, is_ending: bool, ending_type: str, reason: str = "") -> dict[str, Any]:
    """把模型结局转换成自由模式公开状态，不创建正式作者结局 ID。"""  # noqa: DOCSTRING_CJK
    return {
        "should_offer_ending": bool(is_ending),
        "should_end_session": bool(is_ending),
        "reason": reason if is_ending else "",
        "ending_type": str(ending_type or "none"),
    }


def _public_history(session: dict[str, Any], lanlan_name: str) -> list[dict[str, str]]:
    """投影自由模式公开历史；过滤时间戳和内部字段后供刷新恢复使用。"""  # noqa: DOCSTRING_CJK
    history: list[dict[str, str]] = []
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "")
        if role == "user":
            # Session 中的用户输入已经是运行时正文，不再套用来源故事的占位符投影。
            text = str(turn.get("text") or "").strip()
            if text:
                history.append({"role": "user", "text": text})
            continue
        if role != "assistant":
            continue
        # 自由历史只读取 RP-Hub 正文；用户消息仍使用 text，避免与正文源混淆。
        free_text = str(turn.get("free_text") or "").strip()
        if free_text:
            history.append({"role": "narrator", "text": free_text})
    return history


def _public_response(
    *,
    session: dict[str, Any],
    performance: dict[str, Any],
    ending: dict[str, Any],
    can_resume: bool,
) -> dict[str, Any]:
    """投影自由 Session；不公开作者图、Ledger、Node 或 Edge 字段。"""  # noqa: DOCSTRING_CJK
    lanlan_name = str(session.get("lanlan_name") or "Lan")
    # 模型正文是已持久化的运行时文本；只在来源 Story 投影占位符，避免把“男主角”
    # 等普通词或已经是当前名字的文本再次替换。
    free_text = str(performance.get("text") or "").strip()
    lifecycle = "ended" if session.get("ended_at") else "active"
    return {
        "ok": True,
        "mode": "free",
        "session_id": str(session.get("session_id") or ""),
        "story_id": str(session.get("story_id") or ""),
        "state_revision": session_store.state_revision(session),
        # 自由模式公开协议只保留 RP-Hub 风格正文；旧快照兼容只在离场清理时处理。
        "free_text": free_text,
        "free_history": _public_history(session, lanlan_name),
        # 自由模式临时角色卡只返回展示所需字段，避免页面继续显示来源剧本的旧身份卡。
        "free_role_card": _public_role_card(session.get("role_card")),
        "ending": dict(ending),
        "can_resume": bool(can_resume),
        "session_lifecycle": lifecycle,
        "stale": False,
    }


async def claim_dialogue_speech(
    root: Path,
    *,
    session_id: str,
    state_revision: Any,
    expected_lanlan_name: str = "",
    play: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """在自由模式独立运行根内原子认领对白，避免复用剧本模式 Session。"""  # noqa: DOCSTRING_CJK
    if (
        not isinstance(state_revision, int)
        or isinstance(state_revision, bool)
        or state_revision < 0
    ):
        return {"ok": False, "reason": "invalid_state_revision"}
    free_root = _free_root(root)
    async with session_store.session_guard(session_id):
        session = await session_store.load_session(free_root, session_id)
        if session is None or session.get("mode") != "free":
            return {"ok": False, "reason": "session_not_found"}
        lanlan_name = str(session.get("lanlan_name") or "")
        async with session_store.character_guard(free_root, lanlan_name):
            # 等待角色锁后重新读取，防止并发离场或替换期间认领旧对白。
            session = await session_store.load_session(free_root, session_id)
            if session is None or session.get("mode") != "free":
                return {"ok": False, "reason": "session_not_found"}
            current_revision = session_store.state_revision(session)
            expected_name = str(expected_lanlan_name or "").strip()
            if expected_name and str(session.get("lanlan_name") or "").strip() != expected_name:
                return {
                    "ok": True,
                    "skipped": "character_changed",
                    "state_revision": current_revision,
                }
            snapshot = session.get("public_snapshot")
            if not isinstance(snapshot, dict):
                return {"ok": False, "reason": "session_invalid"}
            if await session_store.is_stale_session(free_root, session):
                return {
                    "ok": True,
                    "skipped": "stale_session",
                    "state_revision": current_revision,
                }
            if state_revision != current_revision:
                return {
                    "ok": True,
                    "skipped": "stale_revision",
                    "state_revision": current_revision,
                }
            # 自由模式 TTS 只认领完整 RP-Hub 正文，不再读取已删除的结构化对白字段。
            free_text = snapshot.get("free_text")
            line = str(free_text or "").strip()
            if not line:
                return {
                    "ok": True,
                    "skipped": "empty_dialogue",
                    "state_revision": current_revision,
                }
            spoken = session.setdefault("spoken_dialogue_revisions", [])
            normalized_spoken = [
                item
                for item in spoken
                if isinstance(item, int) and not isinstance(item, bool)
            ]
            if current_revision in normalized_spoken:
                return {
                    "ok": True,
                    "skipped": "already_spoken",
                    "state_revision": current_revision,
                }
            normalized_spoken.append(current_revision)
            session["spoken_dialogue_revisions"] = normalized_spoken[
                -MAX_SPOKEN_DIALOGUE_REVISIONS:
            ]
            await session_store.save_session(free_root, session)
            claim = {
                "ok": True,
                "line": line,
                "lanlan_name": lanlan_name,
                "session_id": str(session.get("session_id") or session_id),
                "state_revision": current_revision,
            }
            if play is None:
                return claim
            # 播放回调位于同一角色锁内，防止下一轮演出抢占当前对白的提交边界。
            return await play(claim)


def _restore_error(session: dict[str, Any] | None, story: dict[str, Any]) -> str:
    """校验自由 Session 的故事版本和生命周期，不迁移剧本模式状态。"""  # noqa: DOCSTRING_CJK
    if not isinstance(session, dict):
        return "session_not_found"
    if session.get("mode") != "free":
        return "session_invalid"
    if str(session.get("story_revision") or "") != str(story.get("story_revision") or ""):
        return "session_story_revision_mismatch"
    if not isinstance(session.get("turns"), list):
        return "session_invalid"
    if not session_store.lifecycle_fields_valid(session):
        return "session_invalid"
    if not isinstance(session.get("public_snapshot"), dict):
        return "session_invalid"
    return ""


async def start_session(
    root: Path,
    *,
    lanlan_name: str,
    story_id: str | None = None,
    client_start_id: str = "",
    role_card: dict[str, Any] | None = None,
    config_manager: Any | None = None,
) -> dict[str, Any]:
    """创建或幂等恢复自由模式 Session；不触碰剧本模式 active 指针。"""  # noqa: DOCSTRING_CJK
    start_id = str(client_start_id or "").strip()
    if len(start_id) > 160:
        return {"ok": False, "reason": "invalid_client_start_id"}
    name = str(lanlan_name or "Lan").strip() or "Lan"
    free_root = _free_root(root)
    async with session_store.character_guard(free_root, name):
        active_id = await session_store.get_active_session_id(free_root, name)
        active = await session_store.load_session(free_root, active_id) if active_id else None
        if active and not active.get("ended_at"):
            # A catgirl may have only one active free Session. A second window
            # must resume that Session instead of repointing the active index.
            return deepcopy(active.get("public_snapshot") or {})

        try:
            story = await story_loader.load_story(story_id)
        except (FileNotFoundError, ValueError):
            return {"ok": False, "reason": "story_not_found"}
        if not story:
            return {"ok": False, "reason": "story_not_found"}
        # 自由模式同样只能使用 complete Story；自由输入不等于绕过生命周期合同。
        runtime_issues = fact_lifecycle.migration_status_issues(
            story,
            boundary="runtime",
        )
        if runtime_issues:
            return {"ok": False, "reason": runtime_issues[0].code}
        scene = story_loader.scene_by_id(story, str(story.get("initial_scene_id") or ""))
        if not scene:
            return {"ok": False, "reason": "story_has_no_initial_scene"}
        try:
            # 完整 Story 只负责来源校验；模型和自由 Session 只接收最小种子。
            seed = free_seed.build_free_seed(story, scene)
        except free_seed.FreeSeedContractError:
            return {"ok": False, "reason": "free_seed_invalid"}
        try:
            # 无论角色卡来自哪里，主角和玩家称呼都先绑定当前猫娘；外部卡只
            # 提供世界观、开场、示例对白等素材，不得切换 N.E.K.O 人格。
            raw_role_card = role_card or _build_default_role_card(
                seed,
                lanlan_name=name,
                config_manager=config_manager,
            )
            active_role_card = free_role_card.bind_role_card_to_current_catgirl(
                raw_role_card,
                expected_name=name,
                character_profile=_load_character_profile(
                    config_manager,
                    name,
                    max_chars=THEATER_PERSONA_MAX_CHARS,
                ),
                player_address=_load_player_address(config_manager),
            )
            seed = free_role_card.apply_role_card_to_seed(seed, active_role_card)
        except free_role_card.FreeRoleCardContractError:
            return {"ok": False, "reason": "free_role_card_invalid"}

        # RP-Hub 的 first_mes 本身就是首条 assistant 消息，不再额外调用模型
        # 改写一遍。没有 first_mes 的兼容卡才走一次开场生成。
        first_mes = str(active_role_card.get("first_mes") or "").strip()
        if first_mes:
            performance = {"ok": True, "text": first_mes}
        else:
            performance = await llm.generate_free_turn_async(
                config_manager=config_manager,
                lanlan_name=name,
                story=seed,
                scene=seed["opening_scene"],
                user_message="",
                recent_turns=[],
                is_opening=True,
                role_card=active_role_card,
            )
        if performance.get("ok") is False:
            return performance
        now = _now_ms()
        session_id = f"theater_{uuid.uuid4()}"
        session: dict[str, Any] = {
            "schema_version": session_store.SESSION_SCHEMA_VERSION,
            "mode": "free",
            "session_id": session_id,
            "story_id": str(seed.get("source_story_id") or ""),
            "story_revision": str(seed.get("source_story_revision") or ""),
            "lanlan_name": name,
            "start_client_id": start_id,
            # 保存来源 Scene，避免临时角色卡的展示开场 ID污染后续种子恢复。
            "source_scene_id": str(scene.get("id") or ""),
            "scene_id": str(seed["opening_scene"].get("id") or ""),
            "node_context": _initial_node_context(seed),
            "scenario_card": _scenario_card(seed),
            # 临时角色卡随 Free Session 保存，刷新可继续体验，但不会进入全局角色卡目录。
            "role_card": deepcopy(active_role_card) if active_role_card else None,
            # 自由 Session 只保存 RP-Hub 正文；公开 free_history 仍投影为 text 字段。
            "turns": [
                {
                    "role": "assistant",
                    "free_text": str(performance.get("text") or ""),
                    "created_at": now,
                }
            ],
            "state_revision": 0,
            "turn_results_by_client_id": {},
            "started_at": now,
            "updated_at": now,
            "ended_at": None,
        }
        ending = _ending(
            is_ending=False,
            ending_type="none",
            reason="",
        )
        if ending["should_end_session"]:
            session["ended_at"] = now
            session["end_reason"] = "story_complete"
        response = _public_response(
            session=session,
            performance=performance,
            ending=ending,
            can_resume=not bool(session.get("ended_at")),
        )
        session["public_snapshot"] = deepcopy(response)
        await session_store.save_session(free_root, session)
        if session.get("ended_at"):
            await session_store.clear_active_session(free_root, name, session_id)
        else:
            await session_store.set_active_session(free_root, name, session_id)
        return response


async def submit_input(
    root: Path,
    *,
    session_id: str,
    message: str,
    input_kind: str,
    choice_id: str = "",
    client_turn_id: str,
    base_revision: Any,
    config_manager: Any | None = None,
    expected_lanlan_name: str = "",
) -> dict[str, Any]:
    """提交自由模式一回合；模型失败或并发冲突时不写入半回合。"""  # noqa: DOCSTRING_CJK
    normalized_id = str(session_id or "").strip()
    turn_id = str(client_turn_id or "").strip()
    if not normalized_id or not turn_id or len(turn_id) > 160:
        return {"ok": False, "reason": "invalid_free_input"}
    if input_kind not in {"free_input", "choice", "user_exit"}:
        return {"ok": False, "reason": "invalid_free_input"}
    if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 0:
        return {"ok": False, "reason": "invalid_state_revision"}
    free_root = _free_root(root)
    async with session_store.session_guard(normalized_id):
        session = await session_store.load_session(free_root, normalized_id)
        if session is None:
            return {"ok": False, "reason": "session_not_found"}
        if expected_lanlan_name and str(session.get("lanlan_name") or "") != expected_lanlan_name:
            return {"ok": False, "reason": "session_character_mismatch"}
        try:
            story = await story_loader.load_story_exact(str(session.get("story_id") or ""))
        except (FileNotFoundError, ValueError):
            return {"ok": False, "reason": "session_story_revision_mismatch"}
        restore_error = _restore_error(session, story)
        if restore_error:
            return {"ok": False, "reason": restore_error}
        previous = session.get("turn_results_by_client_id") or {}
        if turn_id in previous:
            return deepcopy(previous[turn_id])
        if session.get("ended_at"):
            return {"ok": False, "reason": "session_ended"}
        revision = session_store.state_revision(session)
        if base_revision != revision:
            return {
                "ok": False,
                "reason": "state_revision_conflict",
                "retryable": True,
                "state_revision": revision,
            }
        normalized_message = str(message or "").strip()
        if input_kind == "choice" and not normalized_message:
            for option in session.get("public_snapshot", {}).get("suggestion_options") or []:
                if isinstance(option, dict) and str(option.get("choice_id") or "") == str(choice_id or ""):
                    normalized_message = str(option.get("label") or "").strip()
                    break
        if input_kind == "user_exit":
            now = _now_ms()
            session["ended_at"] = now
            session["end_reason"] = "user_exit"
            session["updated_at"] = now
            response = deepcopy(session.get("public_snapshot") or {})
            # 离场响应清掉旧快照残留的剧本投影，避免自由协议重新暴露空壳字段。
            for legacy_field in (
                "narration",
                "dialogue",
                "closing_narration",
                "phase",
                "scene",
                "scenario_board",
                "scenario_trace",
                "suggestion_options",
            ):
                response.pop(legacy_field, None)
            response.update(
                {
                    "state_revision": revision + 1,
                    "ending": {
                        "should_offer_ending": False,
                        "should_end_session": True,
                        "reason": "user_exit",
                        "ending_type": "none",
                    },
                    "can_resume": False,
                    "session_lifecycle": "ended",
                }
            )
            session["state_revision"] = revision + 1
            session["public_snapshot"] = deepcopy(response)
            session["turn_results_by_client_id"] = {turn_id: deepcopy(response)}
            await session_store.save_session(free_root, session)
            await session_store.clear_active_session(
                free_root, str(session.get("lanlan_name") or ""), normalized_id
            )
            return response
        if not normalized_message:
            return {"ok": False, "reason": "invalid_free_input"}
        if truncate_to_tokens(
            normalized_message,
            llm.THEATER_FREE_CURRENT_MESSAGE_MAX_TOKENS,
        ) != normalized_message:
            return {"ok": False, "reason": "free_input_too_long"}
        source_scene = _scene_for_session(story, session)
        if not source_scene:
            return {"ok": False, "reason": "story_has_no_initial_scene"}
        try:
            # 每轮按已保存的来源 revision 重建同一份最小种子，避免 Session 读取旧投影。
            seed = free_seed.build_free_seed(story, source_scene)
        except free_seed.FreeSeedContractError:
            return {"ok": False, "reason": "free_seed_invalid"}
        active_role_card = session.get("role_card")
        if active_role_card is not None:
            try:
                # 续写继续使用开场时绑定的世界资料，但每轮重新确认主角和玩家
                # 称呼仍属于当前猫娘，防止刷新或切换人格后回到旧角色。
                active_role_card = free_role_card.bind_role_card_to_current_catgirl(
                    active_role_card,
                    expected_name=str(session.get("lanlan_name") or "Lan"),
                    character_profile=_load_character_profile(
                        config_manager,
                        str(session.get("lanlan_name") or "Lan"),
                        max_chars=THEATER_PERSONA_MAX_CHARS,
                    ),
                    player_address=_load_player_address(config_manager),
                )
                seed = free_role_card.apply_role_card_to_seed(
                    seed,
                    active_role_card,
                )
            except free_role_card.FreeRoleCardContractError:
                return {"ok": False, "reason": "free_role_card_invalid"}
        performance = await llm.generate_free_turn_async(
            config_manager=config_manager,
            lanlan_name=str(session.get("lanlan_name") or "Lan"),
            story=seed,
            scene=seed["opening_scene"],
            user_message=normalized_message,
            recent_turns=list(session.get("turns") or []),
            is_opening=False,
            role_card=active_role_card,
        )
        if performance.get("ok") is False:
            return performance
        candidate = deepcopy(session)
        now = _now_ms()
        candidate["turns"].extend(
            [
                {"role": "user", "text": normalized_message, "created_at": now},
                {
                    "role": "assistant",
                    # 新自由回合只把 Actor 的 text 正文写入唯一存储字段 free_text。
                    "free_text": str(performance.get("text") or ""),
                    "created_at": now,
                },
            ]
        )
        candidate["turns"] = candidate["turns"][-MAX_FREE_TURNS:]
        candidate["state_revision"] = revision + 1
        candidate["updated_at"] = now
        ending = _ending(
            is_ending=False,
            ending_type="none",
            reason="",
        )
        if ending["should_end_session"]:
            candidate["ended_at"] = now
            candidate["end_reason"] = "story_complete"
        response = _public_response(
            session=candidate,
            performance=performance,
            ending=ending,
            can_resume=not bool(candidate.get("ended_at")),
        )
        index = candidate.setdefault("turn_results_by_client_id", {})
        index[turn_id] = deepcopy(response)
        while len(index) > MAX_IDEMPOTENT_RESULTS:
            index.pop(next(iter(index)))
        candidate["public_snapshot"] = deepcopy(response)
        await session_store.save_session(free_root, candidate)
        if candidate.get("ended_at"):
            await session_store.clear_active_session(
                free_root, str(candidate.get("lanlan_name") or ""), normalized_id
            )
        return response


async def get_state(root: Path, *, session_id: str, expected_lanlan_name: str = "") -> dict[str, Any]:
    """返回自由模式 Session 的最近公开快照。"""  # noqa: DOCSTRING_CJK
    free_root = _free_root(root)
    session = await session_store.load_session(free_root, str(session_id or ""))
    if session is None:
        return {"ok": False, "reason": "session_not_found"}
    if expected_lanlan_name and str(session.get("lanlan_name") or "") != expected_lanlan_name:
        return {"ok": False, "reason": "session_character_mismatch"}
    try:
        story = await story_loader.load_story_exact(str(session.get("story_id") or ""))
    except (FileNotFoundError, ValueError):
        return {"ok": False, "reason": "session_story_revision_mismatch"}
    restore_error = _restore_error(session, story)
    if restore_error:
        return {"ok": False, "reason": restore_error}
    return deepcopy(session.get("public_snapshot") or {"ok": False, "reason": "session_invalid"})


async def get_active_state(root: Path, *, lanlan_name: str) -> dict[str, Any]:
    """从自由模式独立 active 索引恢复当前猫娘的沙盒演出。"""  # noqa: DOCSTRING_CJK
    free_root = _free_root(root)
    session_id = await session_store.get_active_session_id(free_root, str(lanlan_name or ""))
    if not session_id:
        return {"ok": False, "reason": "session_not_found"}
    return await get_state(
        root,
        session_id=session_id,
        expected_lanlan_name=lanlan_name,
    )
