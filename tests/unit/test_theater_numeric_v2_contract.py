"""Numeric v2 包合同、复验和独立安装目录测试。"""

from __future__ import annotations

import json

import pytest

from services.theater.numeric_v2 import NumericV2CompileError, NumericV2Compiler
from services.theater.numeric_v2_registry import (
    NumericV2PackageExistsError,
    NumericV2PackageRegistry,
)


def numeric_v2_story() -> dict:
    """构造一个包含两条数值路线和两个结局的最小合法包。"""

    metric = {
        "name": "信任度",
        "description": "猫娘愿意相信玩家承诺的程度。",
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
        "initial_state": {"metrics": {"trust": 20}},
        "start_node_id": "start",
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "chapter": "重逢",
                "min_turns": 2,
                "story_beat": beat("玩家与猫娘在花店重新见面。"),
                "route_gates": [
                    gate("to_stay", "ending_stay", ">=", 70, 20),
                    gate("to_leave", "ending_leave", "<", 70, 10),
                ],
            },
            {
                "id": "ending_stay",
                "type": "ending",
                "chapter": "决定",
                "story_beat": beat("玩家决定留下来完成承诺。"),
                "route_gates": [],
                "terminal": True,
                "ending_id": "stay",
            },
            {
                "id": "ending_leave",
                "type": "ending",
                "chapter": "决定",
                "story_beat": beat("两人接受这次仍然会分别。"),
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


def test_numeric_v2_rejects_player_visible_metric():
    story = numeric_v2_story()
    story["metric_schema"]["trust"]["visibility"] = "public"

    with pytest.raises(NumericV2CompileError) as caught:
        NumericV2Compiler().compile(story)

    assert any(issue.code == "invalid_metric_visibility" for issue in caught.value.issues)


def test_numeric_v2_accepts_metric_free_single_route_mainline():
    """纯主线只有一个出口时，不要求作者为了编译虚构数值条件。"""

    story = numeric_v2_story()
    story["metric_schema"] = {}
    story["initial_state"] = {"metrics": {}}
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


def test_numeric_v2_registry_seeds_default_story_into_empty_root(tmp_path):
    package_root = tmp_path / "theater" / "numeric_v2" / "packages"
    registry = NumericV2PackageRegistry(package_root)

    registry.ensure_default_packages()

    packages = registry.list_packages()
    assert [item["story_id"] for item in packages] == ["story_d079453b8e9f"]
    assert (package_root / "story_d079453b8e9f.json").is_file()
