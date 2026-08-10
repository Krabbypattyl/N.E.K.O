"""验证 Numeric v2 最少回合、确定性路线与原子持久化。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from services.theater.numeric_v2_runtime import (
    MetricChangeV2,
    NumericV2Engine,
    NumericV2Runtime,
    TurnRequestV2,
)
from tests.unit.test_theater_numeric_v2_contract import numeric_v2_story


def _binding() -> dict[str, str]:
    return {
        "catgirl_id": "catgirl:Lan",
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
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="numeric_session_not_at_ledger_tail"):
        await runtime.restore_session("runtime_tamper")
