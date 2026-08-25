"""验证 Numeric v2 最少回合、确定性路线与原子持久化。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import gc
import json
import os

import pytest

from services.theater import numeric_v2_archive, numeric_v2_maintenance, numeric_v2_store
from services.theater.numeric_v2_maintenance import (
    QUARANTINE_FILE_LIMIT,
    audit_numeric_v2_storage,
)
from services.theater.numeric_v2_registry import NumericV2PackageRegistry
from services.theater.numeric_v2_store import update_numeric_v2_character_bindings
from services.theater.numeric_v2_runtime import (
    MetricChangeV2,
    NumericV2Engine,
    NumericV2Runtime,
    TurnRequestV2,
)
from tests.unit.test_theater_numeric_v2_contract import numeric_v2_story
from utils.config_manager import ensure_catgirl_character_id, get_reserved


def test_numeric_v2_idle_store_and_receipt_locks_are_reclaimed(tmp_path):
    """锁在并发窗口内必须复用，调用方释放后不得按历史 ID 永久积累。"""  # noqa: DOCSTRING_CJK

    session_path = tmp_path / "numeric_v2" / "sessions" / "lock-test.json"
    session_key = str(session_path.resolve())
    session_lock = numeric_v2_store._lock(session_path)
    assert numeric_v2_store._lock(session_path) is session_lock

    receipt_path = tmp_path / "numeric_v2" / "end_receipts" / "lock-test.json"
    receipt_key = str(receipt_path.resolve())
    receipt_lock = numeric_v2_archive._receipt_lock(receipt_path)
    assert numeric_v2_archive._receipt_lock(receipt_path) is receipt_lock

    # 删除测试持有的最后强引用后，弱引用表应自动清除两个空闲条目。
    del session_lock
    del receipt_lock
    gc.collect()
    assert session_key not in numeric_v2_store._LOCKS
    assert receipt_key not in numeric_v2_archive._RECEIPT_LOCKS


def test_existing_character_id_is_persisted_in_canonical_form():
    """已存在的合法 UUID 也要回写统一格式，避免角色绑定出现多种表示。"""  # noqa: DOCSTRING_CJK
    card = {
        "_reserved": {
            "character_id": "character_12345678-1234-5678-9ABC-DEF012345678",
        },
    }

    character_id, changed = ensure_catgirl_character_id(card)

    assert changed is True
    assert character_id == "character_12345678123456789abcdef012345678"
    assert get_reserved(card, "character_id") == character_id


def test_numeric_v2_receipt_path_rejects_parent_directory_escape(tmp_path):
    """结束回执只接受服务端固定格式，不能借路径片段逃逸归档目录。"""  # noqa: DOCSTRING_CJK
    store = numeric_v2_archive.NumericV2ArchiveStore(tmp_path)

    with pytest.raises(
        numeric_v2_archive.NumericV2ArchiveError,
        match="numeric_end_receipt_invalid",
    ):
        store._receipt_path("theater_end_../../outside")


@pytest.mark.asyncio
async def test_numeric_v2_empty_character_id_delete_keeps_other_legacy_names(tmp_path):
    """旧角色卡按名称删除时不能把空 character_id 扩散成全角色删除。"""  # noqa: DOCSTRING_CJK
    session_root = tmp_path / "numeric_v2" / "sessions"
    archive_root = tmp_path / "numeric_v2" / "public_archives"
    session_root.mkdir(parents=True)
    archive_root.mkdir(parents=True)
    for session_id, catgirl_name in (("legacy-a", "小葵"), ("legacy-b", "雪奈")):
        payload = {
            "schema": numeric_v2_store.STORE_SCHEMA,
            "session": {
                "session_id": session_id,
                "story_package_id": "legacy-story",
                "status": "ended",
                "catgirl_binding": {
                    "catgirl_name": catgirl_name,
                    "character_id": "",
                },
            },
        }
        (session_root / f"{session_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        archive_payload = {
            "schema": "neko.theater.numeric.v2.public-archive",
            "session_id": session_id,
            "story_id": "legacy-story",
            "catgirl_name": catgirl_name,
            "character_id": "",
        }
        (archive_root / f"{session_id}.json").write_text(
            json.dumps(archive_payload, ensure_ascii=False),
            encoding="utf-8",
        )

    deleted = await numeric_v2_store.delete_numeric_v2_sessions(
        tmp_path,
        character_id="",
        legacy_catgirl_name="小葵",
    )

    assert [item["session_id"] for item in deleted] == ["legacy-a"]
    assert not (session_root / "legacy-a.json").exists()
    assert (session_root / "legacy-b.json").is_file()
    assert not (archive_root / "legacy-a.json").exists()
    assert (archive_root / "legacy-b.json").is_file()


@pytest.mark.asyncio
async def test_numeric_v2_scoped_delete_preserves_other_story_slots(tmp_path):
    """同时按剧本和角色删除时，只能移除交集槽位。"""  # noqa: DOCSTRING_CJK
    index_path = tmp_path / "numeric_v2" / "story_sessions.json"
    numeric_v2_store._write_story_session_slots(index_path, {
        "story-a": {"character-a": "session-aa", "character-b": "session-ab"},
        "story-b": {"character-a": "session-ba"},
    })

    await numeric_v2_store.delete_numeric_v2_sessions(
        tmp_path,
        story_id="story-a",
        character_id="character-a",
    )

    assert numeric_v2_store._read_story_session_slots(index_path) == {
        "story-a": {"character-b": "session-ab"},
        "story-b": {"character-a": "session-ba"},
    }


def _binding() -> dict[str, str]:
    return {
        "character_id": "character_11111111111111111111111111111111",
        "catgirl_id": "catgirl:character_11111111111111111111111111111111",
        "catgirl_name": "Lan",
        "player_address": "哥哥",
        "profile_revision": "characters:test",
        "profile_hash": "sha256:test",
    }


def _opening() -> dict:
    return {
        "narration": "花店风铃轻响。",
        "dialogue": [{"speaker_id": "active_catgirl", "text": "你回来了。"}],
        "suggested_inputs": ["问她近况"],
    }


def _performance(text: str) -> dict:
    return {
        "narration": "她认真听完。",
        "dialogue": [{"speaker_id": "active_catgirl", "text": text}],
        "suggested_inputs": [],
    }


def _transition_performance(target_node_id: str) -> dict:
    return {
        "suggested_inputs": [],
        "segments": [
            {
                "phase": "source_response",
                "content": [
                    {"type": "narration", "text": "她回应后收住话题。"},
                    {"type": "dialogue", "speaker_id": "active_catgirl", "text": "明天再说。"},
                ],
            },
            {
                "phase": "transition_bridge",
                "content": [{"type": "narration", "text": "夜色过去。"}],
            },
            {
                "phase": "target_opening",
                "content": [{"type": "narration", "text": "第二天清晨，花店重新开门。"}],
            },
        ],
        "transition_delivered": True,
        "visible_node_id": target_node_id,
    }


def _branch_story() -> dict:
    story = numeric_v2_story()
    story["nodes"][0]["route_gates"][0]["conditions"]["all"][0]["value"] = 25
    story["nodes"][0]["route_gates"][1]["conditions"]["all"][0]["value"] = 25
    return story


@pytest.mark.asyncio
async def test_numeric_v2_waits_for_min_turns_then_selects_route(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_wait",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )

    first = runtime.prepare_turn(
        stored,
        TurnRequestV2("turn_1", 0, "我会先听你说。"),
        (),
        scene_complete=False,
    )
    assert first.route is None
    assert first.route_status == "waiting_min_turns"
    assert first.session.current_node_id == "start"
    stored = await runtime.commit_turn(first, _performance("那就先坐一会儿。"))

    change = MetricChangeV2.from_mapping(
        {
            "metric_id": "trust",
            "delta": 5,
            "criterion": "玩家兑现承诺",
            "evidence": "玩家明确表示会留下倾听",
        },
        runtime.engine.metric_schema,
    )
    second = runtime.prepare_turn(
        stored,
        TurnRequestV2("turn_2", 1, "我答应留下来把话说完。"),
        (change,),
        scene_complete=True,
    )

    assert second.route_status == "advanced"
    assert second.session.current_node_id == "ending_stay"
    assert second.session.status == "ended"


@pytest.mark.asyncio
async def test_numeric_v2_player_address_is_committed_only_with_successful_turn(tmp_path):
    runtime = NumericV2Runtime(
        NumericV2Engine.from_mapping(numeric_v2_story(player_address_known=False)),
        tmp_path,
    )
    stored = await runtime.start_session(
        session_id="runtime_player_address_state",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    assert stored.session.player_address_known is False
    assert stored.session.to_dict()["player_address_known"] is False

    disclosed = runtime.prepare_turn(
        stored,
        TurnRequestV2("address_disclosure", 0, "我叫哥哥。"),
        (),
        scene_complete=False,
    )
    assert disclosed.session.player_address_known is True
    assert disclosed.ledger_event["player_address_known"] is True

    with pytest.raises(ValueError, match="numeric_performance_invalid"):
        await runtime.commit_turn(disclosed, {"performance": "（只有动作没有对白）"})

    unchanged = await runtime.restore_session(stored.session.session_id)
    assert unchanged is not None
    assert unchanged.session.player_address_known is False
    assert unchanged.session.revision == 0

    committed = await runtime.commit_turn(
        disclosed,
        {"performance": "我听见了。", "suggested_inputs": []},
    )
    assert committed.session.player_address_known is True
    assert committed.ledger_events[-1]["player_address_known"] is True

    restored = await runtime.restore_session(stored.session.session_id)
    assert restored is not None
    assert restored.session.player_address_known is True


@pytest.mark.asyncio
async def test_numeric_v2_surface_you_fallback_does_not_count_as_disclosed_name(tmp_path):
    runtime = NumericV2Runtime(
        NumericV2Engine.from_mapping(numeric_v2_story(player_address_known=False)),
        tmp_path,
    )
    binding = _binding()
    binding["player_address"] = "你"
    stored = await runtime.start_session(
        session_id="runtime_surface_you_fallback",
        catgirl_binding=binding,
        opening_performance=_opening(),
    )

    prepared = runtime.prepare_turn(
        stored,
        TurnRequestV2("surface_you_fallback", 0, "你好，你先说。"),
        (),
        scene_complete=False,
    )

    assert prepared.session.player_address_known is False


@pytest.mark.asyncio
async def test_numeric_v2_legacy_session_derives_address_state_from_submitted_input(tmp_path):
    story = numeric_v2_story(player_address_known=False)
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_legacy_player_address",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    disclosed = runtime.prepare_turn(
        stored,
        TurnRequestV2("legacy_address_disclosure", 0, "我叫哥哥。"),
        (),
        scene_complete=False,
    )
    await runtime.commit_turn(disclosed, _performance("记住了。"))

    path = tmp_path / "numeric_v2" / "sessions" / "runtime_legacy_player_address.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["session"].pop("player_address_known")
    payload["ledger_events"][0].pop("player_address_known")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    restored = await runtime.restore_session(stored.session.session_id)
    assert restored is not None
    assert restored.session.player_address_known is True


@pytest.mark.asyncio
async def test_numeric_v2_remembers_scene_completion_until_min_turns(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_scene_completion_latch",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )

    first = runtime.prepare_turn(
        stored,
        TurnRequestV2("completion_latch_1", 0, "这一幕的目标已经完成。"),
        (),
        scene_complete=True,
    )
    assert first.route_status == "waiting_min_turns"
    assert first.session.scene_completion_ready is True
    stored = await runtime.commit_turn(first, _performance("我已经把该说明的都说清楚了。"))

    restored = await runtime.restore_session(stored.session.session_id)
    assert restored is not None
    assert restored.session.scene_completion_ready is True

    second = runtime.prepare_turn(
        restored,
        TurnRequestV2("completion_latch_2", 1, "那我们继续。"),
        (),
        scene_complete=False,
    )

    assert second.route_status == "advanced"
    assert second.session.current_node_id == "ending_leave"
    assert second.session.scene_completion_ready is False


@pytest.mark.asyncio
async def test_numeric_v2_restores_goal_evidence_from_ledger(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_goal_evidence_restore",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    stored = await runtime.commit_turn(
        runtime.prepare_turn(
            stored,
            TurnRequestV2("goal_evidence_1", 0, "先听你说明。"),
            (),
            scene_complete=False,
        ),
        _performance("我先说明第一部分。"),
    )
    stored = await runtime.commit_turn(
        runtime.prepare_turn(
            stored,
            TurnRequestV2("goal_evidence_2", 1, "继续说。"),
            (),
            scene_complete=False,
            goal_evidence={"goal.1": (1,)},
        ),
        _performance("还有一部分需要确认。"),
    )

    restored = await runtime.restore_session(stored.session.session_id)

    assert restored is not None
    assert restored.session.current_node_id == "start"
    assert restored.session.scene_goal_evidence == {"goal.1": (1,)}
    assert restored.ledger_events[-1]["scene_goal_evidence"] == {"goal.1": [1]}


def test_numeric_v2_runtime_accepts_structured_goal_ids():
    """Runtime 应直接校验作者目标 ID，避免结构化目标又退回按序号猜测。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["goals"] = [{
        "id": "confirm_old_letter",
        "owner": "catgirl",
        "description": "女主确认旧信保管状态。",
        "evidence": {"mode": "semantic", "anchors": []},
    }]
    beat.pop("must_happen")
    engine = NumericV2Engine.from_mapping(story)
    session = replace(
        engine.create_session(
            session_id="runtime_structured_goal_id",
            catgirl_binding=_binding(),
            opening_performance=_opening(),
        ),
        node_turn_count=1,
        revision=1,
        performance_history=({
            "revision": 1,
            "from_node_id": "start",
            "to_node_id": "start",
            "input_text": "请继续说明。",
            "performance": "（按住信封）这封信一直由我保管。",
        },),
    )

    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("structured_goal_id", 1, "我明白了。"),
        (),
        goal_evidence={"confirm_old_letter": (1,)},
    )

    assert outcome.session.scene_goal_evidence == {"confirm_old_letter": (1,)}


@pytest.mark.asyncio
async def test_numeric_v2_clears_goal_evidence_after_scene_completion_is_latched(tmp_path):
    story = _branch_story()
    story["nodes"][0]["min_turns"] = 3
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_goal_evidence_consumed",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    stored = await runtime.commit_turn(
        runtime.prepare_turn(
            stored,
            TurnRequestV2("goal_consumed_1", 0, "先完成第一部分。"),
            (),
            scene_complete=False,
        ),
        _performance("第一部分已经完成。"),
    )
    completed = runtime.prepare_turn(
        stored,
        TurnRequestV2("goal_consumed_2", 1, "这一幕已经说清楚了。"),
        (),
        scene_complete=True,
        goal_evidence={"goal.1": (1,)},
    )

    assert completed.route_status == "waiting_min_turns"
    assert completed.session.scene_completion_ready is True
    assert completed.session.scene_goal_evidence == {}
    assert completed.ledger_event["scene_completion_ready"] is True
    assert completed.ledger_event["scene_goal_evidence"] == {}


def test_numeric_v2_keeps_only_recent_goal_evidence_revisions():
    engine = NumericV2Engine.from_mapping(numeric_v2_story())
    base = engine.create_session(
        session_id="runtime_recent_goal_evidence",
        catgirl_binding={
            "catgirl_id": "catgirl:test",
            "catgirl_name": "测试猫娘",
            "player_address": "哥哥",
        },
        opening_performance={"narration": "开场", "dialogue": [], "suggested_inputs": []},
    )
    records = tuple({
        "revision": revision,
        "from_node_id": "start",
        "to_node_id": "start",
        "input_text": f"第 {revision} 轮。",
        "narration": "事实",
        "dialogue": [],
    } for revision in range(1, 10))
    session = replace(
        base,
        revision=9,
        node_turn_count=9,
        processed_client_turn_ids=tuple(f"turn_{revision}" for revision in range(1, 10)),
        performance_history=records,
        scene_goal_evidence={"goal.1": (1, 2, 3, 4)},
    )

    outcome = engine.resolve_turn(
        session,
        TurnRequestV2("turn_10", 9, "继续。"),
        (),
        goal_evidence={"goal.1": (5, 6, 7, 8, 9)},
    )

    assert outcome.session.scene_goal_evidence == {"goal.1": (6, 7, 8, 9)}


@pytest.mark.asyncio
async def test_numeric_v2_route_change_requires_visible_transition_before_commit(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_transition_guard",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    first = runtime.prepare_turn(
        stored,
        TurnRequestV2("transition_turn_1", 0, "我先听你说。"),
        (),
        scene_complete=False,
    )
    stored = await runtime.commit_turn(first, _performance("那就先坐一会儿。"))
    second = runtime.prepare_turn(
        stored,
        TurnRequestV2("transition_turn_2", 1, "我答应把话说完。"),
        (),
        scene_complete=True,
    )

    with pytest.raises(ValueError, match="numeric_transition_performance_invalid"):
        await runtime.commit_turn(second, _performance("旧场景继续。"))

    committed = await runtime.commit_turn(
        second,
        _transition_performance(second.session.current_node_id),
    )
    restored = await runtime.restore_session(committed.session.session_id)

    assert restored is not None
    assert restored.session.current_node_id == second.session.current_node_id
    assert restored.session.performance_history[-1]["visible_node_id"] == second.session.current_node_id


@pytest.mark.asyncio
async def test_numeric_v2_route_change_accepts_empty_deduplicated_bridge(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_empty_transition_bridge",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    first = runtime.prepare_turn(
        stored,
        TurnRequestV2("empty_bridge_1", 0, "我先听你说。"),
        (),
        scene_complete=False,
    )
    stored = await runtime.commit_turn(first, _performance("那就先坐一会儿。"))
    second = runtime.prepare_turn(
        stored,
        TurnRequestV2("empty_bridge_2", 1, "我答应把话说完。"),
        (),
        scene_complete=True,
    )
    finalized = runtime.engine.finalize_transition_performance(
        second,
        {
            "suggested_inputs": [],
            "segments": [
                {
                    "phase": "source_response",
                    "performance": "（收好旧信）那就明天再说。",
                },
                {
                    "phase": "transition_bridge",
                    "scene_narration": "",
                },
                {
                    "phase": "target_opening",
                    "performance": "（推开店门）早上好。",
                },
            ],
        },
        target_opening="第二天清晨，花店重新开门。",
    )

    committed = await runtime.commit_turn(second, finalized)
    restored = await runtime.restore_session(committed.session.session_id)

    assert restored is not None
    transition = restored.session.performance_history[-1]
    assert transition["segments"][1]["scene_narration"] == ""
    assert transition["segments"][2]["scene_narration"] == "第二天清晨，花店重新开门。"


@pytest.mark.asyncio
async def test_numeric_v2_restore_accepts_legacy_route_performance_without_transition_segments(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_legacy_transition",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    first = runtime.prepare_turn(
        stored,
        TurnRequestV2("legacy_transition_1", 0, "我先听你说。"),
        (),
        scene_complete=False,
    )
    stored = await runtime.commit_turn(first, _performance("那就先坐一会儿。"))
    second = runtime.prepare_turn(
        stored,
        TurnRequestV2("legacy_transition_2", 1, "我答应把话说完。"),
        (),
        scene_complete=True,
    )
    committed = await runtime.commit_turn(
        second,
        _transition_performance(second.session.current_node_id),
    )
    path = runtime.store._path(committed.session.session_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    legacy = payload["session"]["performance_history"][-1]
    legacy.pop("performance_contract_version")
    legacy.pop("segments")
    legacy.pop("transition_delivered")
    legacy.pop("visible_node_id")
    legacy["narration"] = "她回应后收住话题，夜色过去，第二天清晨花店重新开门。"
    legacy["dialogue"] = [{"speaker_id": "active_catgirl", "text": "明天再说。"}]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    restored = await runtime.restore_session(committed.session.session_id)

    assert restored is not None
    assert restored.session.current_node_id == second.session.current_node_id


@pytest.mark.asyncio
async def test_numeric_v2_session_creation_falls_back_without_hardlinks(tmp_path, monkeypatch):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)

    def reject_hardlink(*_args, **_kwargs):
        raise OSError("hard links unsupported")

    monkeypatch.setattr(numeric_v2_store.os, "link", reject_hardlink)

    stored = await runtime.start_session(
        session_id="runtime_no_hardlink",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )

    assert stored.session.session_id == "runtime_no_hardlink"
    assert (tmp_path / "numeric_v2" / "sessions" / "runtime_no_hardlink.json").is_file()


@pytest.mark.asyncio
async def test_numeric_v2_session_creation_rolls_back_when_index_write_fails(
    tmp_path,
    monkeypatch,
):
    """恢复索引发布失败时不能遗留不可达的 Session 文件。"""  # noqa: DOCSTRING_CJK
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)

    def _reject_index(_stories):
        raise OSError("index write failed")

    monkeypatch.setattr(runtime.store, "_write_story_session_index", _reject_index)

    with pytest.raises(OSError, match="index write failed"):
        await runtime.start_session(
            session_id="runtime_index_failure",
            catgirl_binding=_binding(),
            opening_performance=_opening(),
        )

    assert not runtime.store._path("runtime_index_failure").exists()


@pytest.mark.asyncio
async def test_numeric_v2_story_session_index_survives_runtime_recreation(tmp_path):
    story = _branch_story()
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_story_resume",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )

    restarted_runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), tmp_path)
    restored = await restarted_runtime.restore_story_session(_binding())

    assert restored is not None
    assert restored.session.session_id == stored.session.session_id
    assert restored.session.revision == 0
    index = (tmp_path / "numeric_v2" / "story_sessions.json").read_text(encoding="utf-8")
    assert "runtime_story_resume" in index
    assert '"character_11111111111111111111111111111111":"runtime_story_resume"' in index


@pytest.mark.asyncio
async def test_numeric_v2_story_restore_ignores_sessions_from_other_stories(tmp_path):
    story = _branch_story()
    other_story = deepcopy(story)
    other_story["meta"]["story_id"] = "numeric_other_story"
    registry = NumericV2PackageRegistry(tmp_path / "numeric_v2" / "packages")
    registry.import_package(story)
    registry.import_package(other_story)
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), tmp_path)
    other_runtime = NumericV2Runtime(NumericV2Engine.from_mapping(other_story), tmp_path)

    current = await runtime.start_session(
        session_id="runtime_current_story",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    await other_runtime.start_session(
        session_id="runtime_other_story",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )

    index_path = tmp_path / "numeric_v2" / "story_sessions.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["stories"].pop(story["meta"]["story_id"])
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    audit_numeric_v2_storage(
        tmp_path,
        registry,
        character_ids_by_name={"Lan": _binding()["character_id"]},
    )

    restored = await runtime.restore_story_session(_binding())

    assert restored is not None
    assert restored.session.session_id == current.session.session_id


@pytest.mark.asyncio
async def test_numeric_v2_commit_rejects_session_ended_during_model_wait(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_ended_during_turn",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    outcome = runtime.prepare_turn(
        stored,
        TurnRequestV2("turn_after_end", 0, "这轮不应覆盖结束状态。"),
        (),
        scene_complete=False,
    )
    await runtime.end_session(
        stored.session.session_id,
        base_revision=0,
        reason="user_exit",
    )

    with pytest.raises(numeric_v2_store.NumericV2StoreRevisionConflictError, match="session_already_ended"):
        await runtime.commit_turn(outcome, _performance("不应提交。"))

    restored = await runtime.restore_session(stored.session.session_id)
    assert restored is not None
    assert restored.session.status == "ended"
    assert restored.session.revision == 0


@pytest.mark.asyncio
async def test_numeric_v2_story_restore_prunes_legacy_duplicate_sessions(tmp_path):
    story = _branch_story()
    registry = NumericV2PackageRegistry(tmp_path / "numeric_v2" / "packages")
    registry.import_package(story)
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), tmp_path)
    await runtime.start_session(
        session_id="runtime_story_old",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    newer = await runtime.start_session(
        session_id="runtime_story_new",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    (tmp_path / "numeric_v2" / "story_sessions.json").unlink()
    old_path = tmp_path / "numeric_v2" / "sessions" / "runtime_story_old.json"
    new_path = tmp_path / "numeric_v2" / "sessions" / "runtime_story_new.json"
    os.utime(old_path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new_path, ns=(2_000_000_000, 2_000_000_000))

    audit_numeric_v2_storage(
        tmp_path,
        registry,
        character_ids_by_name={"Lan": _binding()["character_id"]},
    )

    restored = await runtime.restore_story_session(_binding())

    assert restored is not None
    assert restored.session.session_id == newer.session.session_id
    session_files = list((tmp_path / "numeric_v2" / "sessions").glob("*.json"))
    assert [path.stem for path in session_files] == ["runtime_story_new"]


@pytest.mark.asyncio
async def test_numeric_v2_startup_audit_migrates_legacy_name_slot(tmp_path):
    story = _branch_story()
    registry = NumericV2PackageRegistry(tmp_path / "numeric_v2" / "packages")
    registry.import_package(story)
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), tmp_path)
    legacy_binding = {
        key: value
        for key, value in _binding().items()
        if key != "character_id"
    }
    legacy_binding["catgirl_id"] = "catgirl:Lan"
    legacy_session = runtime.engine.create_session(
        session_id="runtime_legacy_name_slot",
        catgirl_binding=legacy_binding,
        opening_performance=_opening(),
    )
    await runtime.store.create(legacy_session)

    audit_numeric_v2_storage(
        tmp_path,
        registry,
        character_ids_by_name={"Lan": _binding()["character_id"]},
    )
    restored = await runtime.restore_story_session(_binding())

    assert restored is not None
    assert restored.session.session_id == "runtime_legacy_name_slot"
    assert restored.session.catgirl_binding == _binding()


@pytest.mark.asyncio
async def test_numeric_v2_startup_audit_bounds_corrupt_quarantine(tmp_path):
    story = _branch_story()
    registry = NumericV2PackageRegistry(tmp_path / "numeric_v2" / "packages")
    registry.import_package(story)
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), tmp_path)
    valid = await runtime.start_session(
        session_id="runtime_valid_after_audit",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    session_root = tmp_path / "numeric_v2" / "sessions"
    for index in range(QUARANTINE_FILE_LIMIT + 3):
        (session_root / f"corrupt_{index}.json").write_text("{", encoding="utf-8")

    result = audit_numeric_v2_storage(
        tmp_path,
        registry,
        character_ids_by_name={"Lan": _binding()["character_id"]},
    )

    assert result["quarantined"] == QUARANTINE_FILE_LIMIT + 3
    assert len(list((tmp_path / "numeric_v2" / "quarantine").glob("*"))) == QUARANTINE_FILE_LIMIT
    assert [path.stem for path in session_root.glob("*.json")] == [
        valid.session.session_id
    ]
    restored = await runtime.restore_story_session(_binding())
    assert restored is not None
    assert restored.session.session_id == valid.session.session_id


@pytest.mark.asyncio
async def test_numeric_v2_recovers_prepared_story_delete_after_interruption(tmp_path):
    story = _branch_story()
    registry = NumericV2PackageRegistry(tmp_path / "numeric_v2" / "packages")
    registry.import_package(story)
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(story), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_delete_interrupted",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    public_archive = tmp_path / "numeric_v2" / "public_archives" / "archive.json"
    public_archive.parent.mkdir(parents=True)
    public_archive.write_text(json.dumps({
        "schema": "neko.theater.numeric.v2.public-archive",
        "story_id": story["meta"]["story_id"],
        "session_id": stored.session.session_id,
        "character_id": _binding()["character_id"],
        "catgirl_name": _binding()["catgirl_name"],
    }), encoding="utf-8")
    archive_store = numeric_v2_archive.NumericV2ArchiveStore(tmp_path)
    receipt = archive_store.create_or_get(stored.session)
    numeric_v2_maintenance._prepare_delete_transaction(
        tmp_path,
        registry,
        story["meta"]["story_id"],
    )
    await numeric_v2_store.delete_numeric_v2_sessions(
        tmp_path,
        story_id=story["meta"]["story_id"],
    )
    archive_store.delete_receipts(story_id=story["meta"]["story_id"])
    registry.delete_package(story["meta"]["story_id"])

    numeric_v2_maintenance.recover_numeric_v2_delete_transactions(tmp_path)

    assert registry.package_path(story["meta"]["story_id"]).is_file()
    assert runtime.store._path(stored.session.session_id).is_file()
    assert public_archive.is_file()
    assert archive_store.load(receipt["receipt_id"]) is not None
    restored = await runtime.restore_story_session(_binding())
    assert restored is not None
    assert restored.session.session_id == stored.session.session_id


@pytest.mark.asyncio
async def test_numeric_v2_restart_replaces_ended_session_in_same_catgirl_slot(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    old = await runtime.start_session(
        session_id="runtime_story_ended",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    ended = await runtime.end_session(
        old.session.session_id,
        base_revision=0,
        reason="user_exit",
    )
    # 重开必须显式指出被替换的旧 Session，测试与生产接口保持同一条原子替换链。
    restarted = await runtime.replace_active_session(
        previous_session_id=old.session.session_id,
        session_id="runtime_story_reopened",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )

    assert ended.session.status == "ended"
    assert restarted.session.session_id == "runtime_story_reopened"
    assert restarted.session.status == "active"
    assert not (tmp_path / "numeric_v2" / "sessions" / "runtime_story_ended.json").exists()
    restored = await runtime.restore_story_session(_binding())
    assert restored is not None
    assert restored.session.session_id == "runtime_story_reopened"


@pytest.mark.asyncio
async def test_numeric_v2_preserves_one_session_per_story_and_catgirl(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    lan = await runtime.start_session(
        session_id="runtime_lan",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    other_binding = {
        **_binding(),
        "character_id": "character_22222222222222222222222222222222",
        "catgirl_id": "catgirl:character_22222222222222222222222222222222",
        "catgirl_name": "Mio",
        "profile_revision": "characters:mio",
        "profile_hash": "sha256:mio",
    }
    mio = await runtime.start_session(
        session_id="runtime_mio",
        catgirl_binding=other_binding,
        opening_performance=_opening(),
    )

    assert (await runtime.restore_story_session(_binding())).session.session_id == lan.session.session_id
    assert (await runtime.restore_story_session(other_binding)).session.session_id == mio.session.session_id

    # 只替换当前猫娘的恢复槽位，另一只猫娘的 Session 必须保持不变。
    restarted_lan = await runtime.replace_active_session(
        previous_session_id=lan.session.session_id,
        session_id="runtime_lan_restarted",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )

    session_files = sorted(
        path.stem
        for path in (tmp_path / "numeric_v2" / "sessions").glob("*.json")
    )
    assert session_files == ["runtime_lan_restarted", "runtime_mio"]
    assert (await runtime.restore_story_session(_binding())).session == restarted_lan.session
    assert (await runtime.restore_story_session(other_binding)).session == mio.session


@pytest.mark.asyncio
async def test_numeric_v2_indexed_restore_does_not_scan_unrelated_session_files(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_indexed_only",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    corrupt_path = tmp_path / "numeric_v2" / "sessions" / "unrelated_corrupt.json"
    corrupt_path.write_text("{", encoding="utf-8")

    restored = await runtime.restore_story_session(_binding())

    assert restored is not None
    assert restored.session.session_id == stored.session.session_id
    assert corrupt_path.read_text(encoding="utf-8") == "{"


@pytest.mark.asyncio
async def test_numeric_v2_character_id_survives_rename_but_blocks_same_name_reuse(
    tmp_path,
):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    original_binding = {**_binding(), "player_address": "旧称呼"}
    original = await runtime.start_session(
        session_id="runtime_character_identity",
        catgirl_binding=original_binding,
        opening_performance=_opening(),
    )
    renamed_binding = {
        **_binding(),
        "catgirl_name": "Lan Renamed",
        "player_address": "新称呼",
    }
    await update_numeric_v2_character_bindings(
        tmp_path,
        character_id=_binding()["character_id"],
        legacy_catgirl_name="Lan",
        catgirl_binding=renamed_binding,
    )

    restored_after_rename = await runtime.restore_story_session(renamed_binding)
    reused_name_binding = {
        **_binding(),
        "character_id": "character_33333333333333333333333333333333",
        "catgirl_id": "catgirl:character_33333333333333333333333333333333",
    }

    assert restored_after_rename is not None
    assert restored_after_rename.session.session_id == original.session.session_id
    assert restored_after_rename.session.catgirl_binding == {
        **renamed_binding,
        "player_address": "旧称呼",
    }
    assert await runtime.restore_story_session(reused_name_binding) is None


@pytest.mark.asyncio
async def test_numeric_v2_does_not_advance_when_min_turns_reached_but_scene_is_incomplete(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_scene_incomplete",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    stored = await runtime.commit_turn(
        runtime.prepare_turn(
            stored,
            TurnRequestV2("turn_1", 0, "先把眼前的误会说清楚。"),
            (),
            scene_complete=False,
        ),
        _performance("我们先说清楚。"),
    )

    second = runtime.prepare_turn(
        stored,
        TurnRequestV2("turn_2", 1, "这件事还没有解决。"),
        (),
        scene_complete=False,
    )

    assert second.route is None
    assert second.route_status == "scene_incomplete"
    assert second.session.current_node_id == "start"
    assert second.ledger_event["scene_complete"] is False


def test_numeric_v2_rejects_model_invented_metric_criterion():
    engine = NumericV2Engine.from_mapping(_branch_story())

    with pytest.raises(ValueError, match="metric_change_criterion_invalid"):
        MetricChangeV2.from_mapping(
            {
                "metric_id": "trust",
                "delta": 1,
                "criterion": "模型自行补充的依据",
                "evidence": "玩家说会留下",
            },
            engine.metric_schema,
        )


@pytest.mark.asyncio
async def test_numeric_v2_uncommitted_candidate_does_not_change_session(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_atomic",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    runtime.prepare_turn(
        stored,
        TurnRequestV2("turn_not_committed", 0, "这轮模拟 Actor 失败。"),
        (),
    )

    restored = await runtime.restore_session("runtime_atomic")
    assert restored is not None
    assert restored.session.revision == 0
    assert restored.session.performance_history == ()
    assert restored.ledger_events == ()


@pytest.mark.asyncio
async def test_numeric_v2_restore_rejects_tampered_ledger(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_tamper",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    outcome = runtime.prepare_turn(
        stored,
        TurnRequestV2("turn_1", 0, "先聊聊。"),
        (),
    )
    committed = await runtime.commit_turn(outcome, _performance("好。"))
    path = runtime.store._path(committed.session.session_id)
    payload = deepcopy(json.loads(path.read_text(encoding="utf-8")))
    payload["ledger_events"][0]["after_metrics"]["trust"] = 99
    payload["session"]["metrics"]["trust"] = 99
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="numeric_ledger_replay_mismatch"):
        await runtime.restore_session("runtime_tamper")


@pytest.mark.asyncio
async def test_numeric_v2_restore_rejects_truncated_performance_history(tmp_path):
    runtime = NumericV2Runtime(NumericV2Engine.from_mapping(_branch_story()), tmp_path)
    stored = await runtime.start_session(
        session_id="runtime_truncated_performance",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    outcome = runtime.prepare_turn(
        stored,
        TurnRequestV2("turn_1", 0, "先聊聊。"),
        (),
    )
    committed = await runtime.commit_turn(outcome, _performance("好。"))
    path = runtime.store._path(committed.session.session_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["session"]["performance_history"] = []
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(
        numeric_v2_store.NumericV2StoreError,
        match="numeric_performance_history_mismatch",
    ):
        await runtime.restore_session("runtime_truncated_performance")
