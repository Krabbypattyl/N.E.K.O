"""验证 Numeric v2 最少回合、确定性路线与原子持久化。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from copy import deepcopy
import json
import os

import pytest

from services.theater import numeric_v2_maintenance, numeric_v2_store
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
    numeric_v2_maintenance._prepare_delete_transaction(
        tmp_path,
        registry,
        story["meta"]["story_id"],
    )
    await numeric_v2_store.delete_numeric_v2_sessions(
        tmp_path,
        story_id=story["meta"]["story_id"],
    )
    registry.delete_package(story["meta"]["story_id"])

    numeric_v2_maintenance.recover_numeric_v2_delete_transactions(tmp_path)

    assert registry.package_path(story["meta"]["story_id"]).is_file()
    assert runtime.store._path(stored.session.session_id).is_file()
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
    restarted = await runtime.restart_session(
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

    restarted_lan = await runtime.restart_session(
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
    original = await runtime.start_session(
        session_id="runtime_character_identity",
        catgirl_binding=_binding(),
        opening_performance=_opening(),
    )
    renamed_binding = {**_binding(), "catgirl_name": "Lan Renamed"}
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
    assert restored_after_rename.session.catgirl_binding == renamed_binding
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
    payload = deepcopy(__import__("json").loads(path.read_text(encoding="utf-8")))
    payload["ledger_events"][0]["after_metrics"]["trust"] = 99
    payload["session"]["metrics"]["trust"] = 99
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")

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
