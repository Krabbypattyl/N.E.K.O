"""普通回合不得把目标幕开场或桥接独有的时点演成现在时（问题2.141 B3）。"""

from types import SimpleNamespace

import pytest

from services.theater import numeric_v2_context as context
from services.theater.numeric_v2_context import premature_target_markers

TARGET_OPENING = "19:47，信标预热还剩三分钟，隔壁控制台突然报警。"
SOURCE_BEAT = {
    "summary": "19:12，她将眼前真实回应写入私人日志；六小时预热将在19:50完成。",
    "character_state": {"environment_state": "信标继续在隔壁独立预热，19:50完成。"},
}


def _engine():
    return SimpleNamespace(
        nodes={
            "source": {"id": "source", "story_beat": SOURCE_BEAT, "route_gates": [{"target_node_id": "target"}]},
            "target": {"id": "target", "story_beat": {"opening_scene": TARGET_OPENING, "summary": "她冲进控制台。"}},
        },
        preview_route=lambda node_id, metrics: {
            "target_node_id": "target",
            "transition_contract": {"bridge_scene_narration": "按玩家的等待略去平静时段，来到19:47、预热剩三分钟时。"},
        },
    )


def _session():
    return SimpleNamespace(current_node_id="source", metrics={}, opening_performance={}, performance_history=())


def _outcome(source="source", target="source"):
    return SimpleNamespace(ledger_event={"from_node_id": source, "to_node_id": target})


@pytest.fixture(autouse=True)
def _no_history(monkeypatch):
    monkeypatch.setattr(context, "performance_history_records", lambda session: [])


def test_next_scene_time_marker_in_narration_is_flagged():
    leaked = premature_target_markers(
        _engine(), _session(), _outcome(),
        {"scene_narration": "时间快速流转至19:47，隔壁控制室突然传来警报。"},
    )
    assert leaked == ("19:47",)


def test_source_scene_own_time_marker_is_allowed():
    # 19:12/19:50 属于当前幕作者事实，不能因为出现在下一幕文本里就被判提前演出。
    leaked = premature_target_markers(
        _engine(), _session(), _outcome(),
        {"scene_narration": "她说19:12会把眼前的回应写进日志，预热19:50完成。"},
    )
    assert leaked == ()


def test_player_input_and_dialogue_may_reference_the_exit():
    # 时点由玩家自己说出，或只出现在对白里提出邀请，都不算旁白提前交付。
    leaked = premature_target_markers(
        _engine(), _session(), _outcome(),
        {"performance": "（看向控制台）19:47我们再出发。"},
        player_input="我们19:47再出发吧。",
    )
    assert leaked == ()


def test_transition_turns_may_deliver_the_target_opening():
    # 正式转场本来就要交付目标幕开场，逐段旁白必须放行。
    leaked = premature_target_markers(
        _engine(), _session(), _outcome(source="source", target="target"),
        {"segments": [{"phase": "target_opening", "scene_narration": TARGET_OPENING}]},
    )
    assert leaked == ()


def test_marker_in_segment_narration_is_still_flagged():
    leaked = premature_target_markers(
        _engine(), _session(), _outcome(),
        {"segments": [{"phase": "source_response", "scene_narration": "警报在19:47响起。"}]},
    )
    assert leaked == ("19:47",)
