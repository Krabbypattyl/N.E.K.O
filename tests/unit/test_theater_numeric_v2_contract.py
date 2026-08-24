"""Numeric v2 包合同、复验和独立安装目录测试。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import json

import pytest

from services.theater import numeric_v2_registry
from services.theater.numeric_v2 import NumericV2CompileError, NumericV2Compiler
from services.theater.numeric_v2_registry import (
    NumericV2PackageExistsError,
    NumericV2PackageRegistry,
)


def numeric_v2_story(*, player_address_known: bool = True) -> dict:
    """构造一个包含两条数值路线和两个结局的最小合法包。"""  # noqa: DOCSTRING_CJK

    metric = {
        "name": "信任度",
        "description": "猫娘愿意相信玩家承诺的程度。",
        "relationship_effect": "positive",
        "min": 0,
        "max": 100,
        "initial": 20,
        "visibility": "hidden",
        "per_turn_limit": {"increase": 5, "decrease": 5},
        "increase_criteria": ["玩家兑现承诺"],
        "decrease_criteria": ["玩家故意说谎"],
        "bands": [
            {"min": 0, "max": 29, "label": "戒备"},
            {"min": 30, "max": 69, "label": "试探"},
            {"min": 70, "max": 100, "label": "信赖"},
        ],
    }

    def beat(summary: str) -> dict:
        return {
            "summary": summary,
            "must_happen": [summary],
            "must_not_happen": [],
            "catgirl_situation": "她在观察玩家是否可信。",
            "transition_goal": "围绕承诺和离开继续发展。",
        }

    def gate(gate_id: str, target: str, op: str, value: int, priority: int) -> dict:
        return {
            "id": gate_id,
            "target_node_id": target,
            "priority": priority,
            "conditions": {
                "all": [
                    {
                        "type": "metric_compare",
                        "metric": "trust",
                        "op": op,
                        "value": value,
                    }
                ]
            },
            "transition_contract": {
                "reason": "当前信任度满足作者路线条件。",
                "must_deliver": ["平滑交付目标剧情"],
                "must_preserve": ["不覆盖此前已经发生的内容"],
                "tone": "克制",
            },
        }

    return {
        "schema": "neko.story.numeric.v2",
        "meta": {
            "story_id": "numeric_v2_contract",
            "title": "Numeric v2 合同测试",
            "author": "test",
            "revision": "r1",
            "language": "zh-CN",
        },
        "intro": {
            "background": "玩家多年后回到小镇，在花店遇到旧友。",
            "player_identity": "林舟，回乡整理旧屋的年轻男性。",
            "catgirl_identity": "小岚，经营花店、保留旧信的年轻女性。",
        },
        "characters": {},
        "catgirl_binding": {
            "source": "runtime.current_catgirl",
            "role_overlay": "她既期待重逢，又担心玩家再次离开。",
        },
        "metric_schema": {"trust": metric},
        "initial_state": {
            "metrics": {"trust": 20},
            "player_address_known": player_address_known,
        },
        "start_node_id": "start",
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "chapter": "重逢",
                "min_turns": 2,
                "recommended_turns": 4,
                "story_beat": beat("雨后的花店门铃轻轻响起。"),
                "route_gates": [
                    gate("to_stay", "ending_stay", ">=", 70, 20),
                    gate("to_leave", "ending_leave", "<", 70, 10),
                ],
            },
            {
                "id": "ending_stay",
                "type": "ending",
                "chapter": "决定",
                "story_beat": beat("旧信与备用钥匙并排放在桌面上。"),
                "route_gates": [],
                "terminal": True,
                "ending_id": "stay",
            },
            {
                "id": "ending_leave",
                "type": "ending",
                "chapter": "决定",
                "story_beat": beat("雨停后的长街恢复了安静。"),
                "route_gates": [],
                "terminal": True,
                "ending_id": "leave",
            },
        ],
        "endings": [
            {"id": "stay", "title": "留下", "summary": "玩家留下。", "terminal": True},
            {"id": "leave", "title": "离开", "summary": "玩家离开。", "terminal": True},
        ],
    }


def test_numeric_v2_compiles_canonical_package():
    compiled = NumericV2Compiler().compile(numeric_v2_story())

    assert compiled.story["schema"] == "neko.story.numeric.v2"
    assert compiled.package_hash.startswith("sha256:")
    assert json.loads(compiled.json_bytes)["meta"]["story_id"] == "numeric_v2_contract"


def test_numeric_v2_requires_structured_initial_player_address_state():
    story = numeric_v2_story()
    story["initial_state"]["player_address_known"] = "称呼未知"

    with pytest.raises(NumericV2CompileError) as error:
        NumericV2Compiler().compile(story)

    assert any(
        issue.code == "invalid_initial_player_address_known"
        and issue.path == "initial_state.player_address_known"
        for issue in error.value.issues
    )


def test_numeric_v2_legacy_package_defaults_missing_player_address_state_to_unknown():
    story = numeric_v2_story()
    del story["initial_state"]["player_address_known"]

    compiled = NumericV2Compiler().compile(story)

    assert compiled.story["initial_state"]["player_address_known"] is False


def test_numeric_v2_validates_optional_acting_contract():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["acting_contract"] = {
        "cognition_state": "fresh_boot",
        "memory_state": "empty",
        "self_reference_mode": "system_neutral",
        "persona_scope": "style_only",
        "assertable_self_facts": ["视觉校准完成", "主存储区为空"],
        "allowed_behaviors": ["自检", "观察", "确认环境"],
        "forbidden_behaviors": ["使用角色卡自称", "虚构旧记忆"],
    }

    compiled = NumericV2Compiler().compile(story)

    assert compiled.story["nodes"][0]["story_beat"]["acting_contract"]["cognition_state"] == "fresh_boot"
    assert compiled.story["nodes"][0]["story_beat"]["acting_contract"]["assertable_self_facts"] == [
        "视觉校准完成",
        "主存储区为空",
    ]

    story["nodes"][0]["story_beat"]["acting_contract"]["self_reference_mode"] = "invalid"
    with pytest.raises(NumericV2CompileError) as error:
        NumericV2Compiler().compile(story)
    assert any(
        issue.code == "invalid_acting_contract_value"
        and issue.path.endswith("acting_contract.self_reference_mode")
        for issue in error.value.issues
    )

    story["nodes"][0]["story_beat"]["acting_contract"]["self_reference_mode"] = "system_neutral"
    story["nodes"][0]["story_beat"]["acting_contract"]["assertable_self_facts"] = []
    with pytest.raises(NumericV2CompileError) as error:
        NumericV2Compiler().compile(story)
    assert any(
        issue.path.endswith("acting_contract.assertable_self_facts")
        for issue in error.value.issues
    )


def test_numeric_v2_accepts_structured_story_beat_contract_without_rewriting_legacy_beats():
    """新包可声明明确开场、关系上限和目标证据，未迁移节点仍保持原文与哈希输入。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    beat = story["nodes"][0]["story_beat"]
    beat["summary"] = "你最终决定是否留下，这是本幕的作者计划。"
    beat["opening_scene"] = "雨水沿着花店玻璃缓慢滑落。"
    beat["relationship_ceiling"] = "guarded"
    beat["goals"] = [
        {
            "id": "confirm_old_letter",
            "owner": "catgirl",
            "description": "女主说明旧信一直由她保管。",
            "evidence": {
                "mode": "exact",
                "anchors": ["旧信一直由我保管"],
            },
        }
    ]
    beat.pop("must_happen")

    compiled = NumericV2Compiler().compile(story)
    compiled_beat = compiled.story["nodes"][0]["story_beat"]

    assert compiled_beat["opening_scene"] == "雨水沿着花店玻璃缓慢滑落。"
    assert compiled_beat["relationship_ceiling"] == "guarded"
    assert compiled_beat["goals"][0]["id"] == "confirm_old_letter"
    assert "must_happen" not in compiled_beat
    # 兼容节点不能被编译器静默补入新字段，否则旧安装包哈希和 Session 会失效。
    assert "opening_scene" not in compiled.story["nodes"][1]["story_beat"]
    assert "relationship_ceiling" not in compiled.story["nodes"][1]["story_beat"]
    assert "goals" not in compiled.story["nodes"][1]["story_beat"]


@pytest.mark.parametrize(
    ("mutate", "issue_code"),
    [
        (
            lambda beat: beat.update({
                "goals": [{
                    "id": "duplicate_source",
                    "owner": "catgirl",
                    "description": "女主说明旧信来历。",
                    "evidence": {"mode": "semantic", "anchors": []},
                }],
            }),
            "conflicting_story_goal_contracts",
        ),
        (
            lambda beat: beat.update({"relationship_ceiling": "very_close"}),
            "invalid_relationship_ceiling",
        ),
        (
            lambda beat: (
                beat.pop("must_happen"),
                beat.update({
                    "goals": [{
                        "id": "missing_anchor",
                        "owner": "catgirl",
                        "description": "女主明确说出旧信来历。",
                        "evidence": {"mode": "exact", "anchors": []},
                    }],
                }),
            ),
            "goal_evidence_anchors_required",
        ),
    ],
)
def test_numeric_v2_rejects_invalid_structured_story_beat_contract(mutate, issue_code):
    """结构化字段必须在编译边界失败，不能把不完整合同留给模型猜测。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    mutate(story["nodes"][0]["story_beat"])

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(issue.code == issue_code for issue in caught.value.issues)


def test_numeric_v2_soft_pacing_budget_is_optional_and_validated():
    story = numeric_v2_story()
    story["nodes"][0].pop("recommended_turns")

    assert "recommended_turns" not in NumericV2Compiler().compile(story).story["nodes"][0]

    story["nodes"][0]["recommended_turns"] = 1
    with pytest.raises(NumericV2CompileError) as below_minimum:
        NumericV2Compiler().compile(story)
    assert any(issue.code == "invalid_node_recommended_turns" for issue in below_minimum.value.issues)

    story["nodes"][0]["recommended_turns"] = 41
    with pytest.raises(NumericV2CompileError) as over_limit:
        NumericV2Compiler().compile(story)
    assert any(issue.code == "invalid_node_recommended_turns" for issue in over_limit.value.issues)


def test_numeric_v2_rejects_unknown_relationship_effect():
    story = numeric_v2_story()
    story["metric_schema"]["trust"]["relationship_effect"] = "guess"

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(
        issue.code == "invalid_metric_relationship_effect"
        for issue in caught.value.issues
    )


def test_numeric_v2_accepts_legacy_metric_without_relationship_effect():
    story = numeric_v2_story()
    story["metric_schema"]["trust"].pop("relationship_effect")

    compiled = NumericV2Compiler().compile(story)

    assert "relationship_effect" not in compiled.story["metric_schema"]["trust"]


def test_numeric_v2_rejects_conflicting_identity_source_names():
    story = numeric_v2_story()
    story["intro"]["player_identity"] = "同名，玩家身份。"
    story["intro"]["catgirl_identity"] = "同名，猫娘身份。"

    with pytest.raises(NumericV2CompileError) as error:
        NumericV2Compiler().compile(story)

    assert any(issue.code == "intro_identity_names_conflict" for issue in error.value.issues)


@pytest.mark.parametrize(
    "summary",
    [
        "周末，男主开始清理堆积如山的快递纸箱。",
        "连续几晚，你都在整理堆积如山的遗失物。",
        "在决定离开后，两人登上了前往城市的列车。",
    ],
)
def test_numeric_v2_rejects_target_opening_that_executes_player_action(summary):
    story = numeric_v2_story()
    story["nodes"][1]["story_beat"]["summary"] = summary

    with pytest.raises(NumericV2CompileError) as error:
        NumericV2Compiler().compile(story)

    assert any(
        issue.code == "player_owned_opening_forbidden"
        and issue.path == "nodes[1].story_beat.summary"
        for issue in error.value.issues
    )


def test_numeric_v2_rejects_transition_that_delivers_player_action():
    story = numeric_v2_story()
    story["nodes"][0]["route_gates"][0]["transition_contract"]["must_deliver"] = [
        "男主在打烊后展示一份商业改革计划书。",
    ]

    with pytest.raises(NumericV2CompileError) as error:
        NumericV2Compiler().compile(story)

    assert any(
        issue.code == "player_owned_transition_forbidden"
        and issue.path == "nodes[0].route_gates[0].transition_contract.must_deliver[0]"
        for issue in error.value.issues
    )


def test_numeric_v2_rejects_player_owned_scene_goal():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["must_happen"] = [
        "男主展示合同并承诺留下。",
    ]

    with pytest.raises(NumericV2CompileError) as error:
        NumericV2Compiler().compile(story)

    assert any(
        issue.code == "player_owned_goal_forbidden"
        and issue.path == "nodes[0].story_beat.must_happen[0]"
        for issue in error.value.issues
    )


def test_numeric_v2_rejects_scene_goal_that_forces_player_action():
    story = numeric_v2_story()
    story["nodes"][0]["story_beat"]["must_happen"] = [
        "女主强迫男主在契约上签字。",
    ]

    with pytest.raises(NumericV2CompileError) as error:
        NumericV2Compiler().compile(story)

    assert any(
        issue.code == "player_owned_goal_forbidden"
        and issue.path == "nodes[0].story_beat.must_happen[0]"
        for issue in error.value.issues
    )


def test_numeric_v2_rejects_legacy_interaction_fields_and_band_gaps():
    story = numeric_v2_story()
    story["interaction_rules"] = []
    story["metric_schema"]["trust"]["bands"][1]["min"] = 31

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    codes = {issue.code for issue in caught.value.issues}
    assert "legacy_field_forbidden" in codes
    assert "metric_bands_not_contiguous" in codes


def test_numeric_v2_rejects_route_threshold_outside_metric_range():
    story = numeric_v2_story()
    story["nodes"][0]["route_gates"][0]["conditions"]["all"][0]["value"] = 101

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(
        issue.code == "route_threshold_out_of_range"
        and issue.path == "nodes[0].route_gates[0].conditions.all[0].value"
        for issue in caught.value.issues
    )


def test_numeric_v2_rejects_impossible_route_condition():
    story = numeric_v2_story()
    story["nodes"][0]["route_gates"][0]["conditions"]["all"][0].update({"op": ">", "value": 100})

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(issue.code == "route_condition_impossible" for issue in caught.value.issues)


def test_numeric_v2_rejects_unsatisfiable_compound_route_condition():
    story = numeric_v2_story()
    story["nodes"][0]["route_gates"][0]["conditions"]["all"].append({
        "type": "metric_compare",
        "metric": "trust",
        "op": "<",
        "value": 30,
    })

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(
        issue.code == "route_condition_impossible"
        and issue.path == "nodes[0].route_gates[0].conditions"
        for issue in caught.value.issues
    )


def test_numeric_v2_rejects_empty_any_route_condition():
    story = numeric_v2_story()
    story["nodes"][0]["route_gates"][0]["conditions"] = {"any": []}

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(issue.code == "route_condition_required" for issue in caught.value.issues)


def test_numeric_v2_rejects_mismatched_metric_initial_state():
    story = numeric_v2_story()
    story["initial_state"]["metrics"]["trust"] = 21

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(issue.code == "initial_metric_value_mismatch" for issue in caught.value.issues)


def test_numeric_v2_rejects_overlapping_equal_priority_routes():
    story = numeric_v2_story()
    story["nodes"][0]["route_gates"][0]["priority"] = 10
    story["nodes"][0]["route_gates"][1]["priority"] = 10
    story["nodes"][0]["route_gates"][1]["conditions"]["all"][0]["op"] = ">="
    story["nodes"][0]["route_gates"][1]["conditions"]["all"][0]["value"] = 60

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(issue.code == "overlapping_route_priority" for issue in caught.value.issues)


def test_numeric_v2_rejects_player_visible_metric():
    story = numeric_v2_story()
    story["metric_schema"]["trust"]["visibility"] = "public"

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(issue.code == "invalid_metric_visibility" for issue in caught.value.issues)


def test_numeric_v2_accepts_metric_free_single_route_mainline():
    """纯主线只有一个出口时，不要求作者为了编译虚构数值条件。"""  # noqa: DOCSTRING_CJK

    story = numeric_v2_story()
    story["metric_schema"] = {}
    story["initial_state"] = {
        "metrics": {},
        "player_address_known": True,
    }
    story["nodes"] = [
        {
            "id": "start",
            "type": "start",
            "chapter": "重逢",
            "min_turns": 2,
            "story_beat": story["nodes"][0]["story_beat"],
            "route_gates": [
                {
                    "id": "to_scene",
                    "target_node_id": "scene",
                    "priority": 10,
                    "conditions": {"all": []},
                    "transition_contract": story["nodes"][0]["route_gates"][0]["transition_contract"],
                }
            ],
        },
        {
            "id": "scene",
            "type": "scene",
            "chapter": "坦白",
            "min_turns": 2,
            "story_beat": story["nodes"][0]["story_beat"],
            "route_gates": [
                {
                    "id": "to_normal",
                    "target_node_id": "ending_normal",
                    "priority": 10,
                    "conditions": {"all": []},
                    "transition_contract": story["nodes"][0]["route_gates"][0]["transition_contract"],
                }
            ],
        },
        {
            "id": "ending_normal",
            "type": "ending",
            "chapter": "带着理解分别",
            "story_beat": story["nodes"][1]["story_beat"],
            "route_gates": [],
            "terminal": True,
            "ending_id": "normal",
        },
    ]
    story["endings"] = [
        {
            "id": "normal",
            "title": "带着理解分别",
            "summary": "两人解开误会，并接受暂时分别。",
            "terminal": True,
        }
    ]

    compiled = NumericV2Compiler().compile(story)

    assert compiled.story["metric_schema"] == {}
    assert compiled.story["nodes"][0]["route_gates"][0]["conditions"] == {"all": []}


def test_numeric_v2_requires_conditions_when_one_node_has_multiple_routes():
    story = numeric_v2_story()
    for route in story["nodes"][0]["route_gates"]:
        route["conditions"] = {"all": []}

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert {issue.code for issue in caught.value.issues} == {"route_condition_required"}


def test_numeric_v2_rejects_reachable_scene_that_cannot_reach_an_ending():
    story = numeric_v2_story()
    story["nodes"][0]["route_gates"] = [story["nodes"][0]["route_gates"][0]]
    story["nodes"][0]["route_gates"][0]["target_node_id"] = "scene_loop"
    story["nodes"].insert(
        1,
        {
            "id": "scene_loop",
            "type": "scene",
            "chapter": "无法收束的支线",
            "min_turns": 2,
            "story_beat": story["nodes"][0]["story_beat"],
            "route_gates": [
                {
                    **story["nodes"][0]["route_gates"][0],
                    "id": "loop_forever",
                    "target_node_id": "scene_loop",
                }
            ],
        },
    )

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(
        issue.code == "node_cannot_reach_ending" and issue.path == "nodes.scene_loop"
        for issue in caught.value.issues
    )


def test_numeric_v2_registry_imports_once_without_touching_sessions(tmp_path):
    package_root = tmp_path / "theater" / "numeric_v2" / "packages"
    registry = NumericV2PackageRegistry(package_root)

    result = registry.import_package(numeric_v2_story())

    assert result["story_id"] == "numeric_v2_contract"
    assert (package_root / "numeric_v2_contract.json").is_file()
    assert not (tmp_path / "theater" / "numeric_v2" / "sessions").exists()
    with pytest.raises(NumericV2PackageExistsError):
        registry.import_package(numeric_v2_story())


def test_numeric_v2_registry_marks_empty_defaults_initialized_without_bundled_story(tmp_path):
    package_root = tmp_path / "theater" / "numeric_v2" / "packages"
    registry = NumericV2PackageRegistry(package_root)

    registry.ensure_default_packages()

    assert registry.list_packages() == []
    assert (package_root / ".defaults_initialized").is_file()

    registry.ensure_default_packages()

    assert registry.list_packages() == []


def test_numeric_v2_registry_falls_back_to_exclusive_creation(tmp_path, monkeypatch):
    package_root = tmp_path / "theater" / "numeric_v2" / "packages"
    registry = NumericV2PackageRegistry(package_root)

    def no_hard_links(*_args, **_kwargs):
        raise OSError("hard links unavailable")

    monkeypatch.setattr(numeric_v2_registry.os, "link", no_hard_links)
    result = registry.import_package(numeric_v2_story())

    assert result["story_id"] == "numeric_v2_contract"
    assert (package_root / "numeric_v2_contract.json").is_file()
    with pytest.raises(NumericV2PackageExistsError):
        registry.import_package(numeric_v2_story())
