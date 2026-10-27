"""Staged creation preserves completed scenes and the native v2.2 package contract."""
from copy import deepcopy
import json

import pytest

from services.theater.numeric_v2 import NumericV2Compiler
from services.theater_workshop.sdk.generation.numeric_v2 import NumericV2Generator, NumericV2GenerationError
from services.theater_workshop.sdk.model import LLMCallFailure
from .test_numeric_v2_generation import _idea_outline, _generation_setup


def staged_reply(messages, operation, outline):
    context = json.loads(messages[1]["content"])
    if operation == "numeric_v2_opening_state_extraction":
        index = next((i for i, chapter in enumerate(outline["mainline_chapters"])
                      if chapter["opening_scene"] == context["opening_scene"]), None)
        stage = (outline["character_state_arc"]["ending_stage"] if index is None else
                 outline["character_state_arc"]["stages"][index])
        result = {key: stage[key] for key in ("catgirl_state", "player_state", "environment_state")}
        if "catgirl_situation" in context["output_contract"]:
            result["catgirl_situation"] = outline["mainline_chapters"][index]["catgirl_situation"]
        if "temporal_context" in stage:
            result["temporal_context"] = deepcopy(stage["temporal_context"])
        return json.dumps(result, ensure_ascii=False)
    if operation == "numeric_v2_story_frame":
        frame = deepcopy(outline)
        frame.pop("character_state_arc")
        frame["relationship_arc"].pop("stages")
        frame["mainline_chapters"] = [
            {key: chapter[key] for key in ("title", "narrative")}
            for chapter in outline["mainline_chapters"]
        ]
        frame["ending"] = {key: outline["ending"][key] for key in ("type", "title", "summary")}
        return json.dumps(frame, ensure_ascii=False)
    if operation == "numeric_v2_chapter_generation":
        index = context["chapter_index"] - 1
        return json.dumps({
            "chapter": outline["mainline_chapters"][index],
            "relationship_stage": outline["relationship_arc"]["stages"][index],
            "character_state_stage": outline["character_state_arc"]["stages"][index],
        }, ensure_ascii=False)
    return json.dumps({"ending": outline["ending"],
                       "ending_stage": outline["character_state_arc"]["ending_stage"]}, ensure_ascii=False)


@pytest.mark.parametrize("empty_ending_bridge", [False, True])
def test_staged_generation_compiles_and_passes_previous_scene_without_rewriting_it(empty_ending_bridge):
    outline = _idea_outline(3)
    outline["mainline_chapters"][-1]["opening_scene"] += "此刻是午后。"
    outline["character_state_arc"]["stages"][-1]["temporal_context"] = {
        "anchor": "午后", "relation_to_previous": "same_day"}
    if empty_ending_bridge:
        outline["ending"]["entry_bridge"] = ""
    checkpoints, calls = [], []

    def model(messages, **options):
        context = json.loads(messages[1]["content"])
        calls.append(options["operation"])
        assert "json" in "".join(message["content"] for message in messages).lower()
        if options["operation"] == "numeric_v2_chapter_generation":
            state_contract = context["output_contract"]["character_state_stage"]
            assert "未写的手势、视线和持物省略" in state_contract["catgirl_state"]
            assert "不新增手势、持物" in state_contract["player_state"]
            assert "不能第二遍设计场面" in messages[0]["content"]
            index = context["chapter_index"] - 1
            expected = None if index == 0 else {
                # The source's incoming bridge must not be reused as the next outgoing bridge.
                "chapter": {key: value for key, value in outline["mainline_chapters"][index - 1].items()
                            if key != "entry_bridge"},
                "relationship_stage": outline["relationship_arc"]["stages"][index - 1],
                "character_state_stage": outline["character_state_arc"]["stages"][index - 1],
            }
            assert context["previous_scene"] == expected
        elif options["operation"] == "numeric_v2_ending_generation":
            assert set(context["story_frame"]) == {"world", "story_protagonist", "player_role", "tone"}
            assert context["ending_identity"] == {key: outline["ending"][key] for key in ("type", "title")}
            last = outline["mainline_chapters"][-1]
            assert context["previous_scene"]["chapter"] == {"title": last["title"], "completion_facts": [
                fact for fact in last["completion_facts"] if fact["id"] in last["exit_plan"]["trigger_fact_ids"]]}
            assert context["previous_scene"]["character_state_stage"] == {key: value for key, value in
                outline["character_state_arc"]["stages"][-1].items()
                if key not in ("catgirl_state", "player_state", "environment_state", "continuity_from_previous", "temporal_context")}
            assert "未明确的细节省略，不另设计画面或新道具" in messages[0]["content"]
            assert "不复制前幕初始姿态或持物" in context["output_contract"]["ending_stage"]["catgirl_state"]
        assert options["max_retries"] == 1
        return staged_reply(messages, options["operation"], outline)

    result = NumericV2Generator(model).generate_staged(
        title="逐幕生成", setup=_generation_setup(), save_checkpoint=checkpoints.append)
    NumericV2Compiler().compile_v2_2(result["story"])
    if empty_ending_bridge:
        transition = result["story"]["nodes"][-2]["route_gates"][0]["transition_contract"]
        assert "bridge_scene_narration" not in transition
        assert transition["must_deliver"] == []
    assert calls == ["numeric_v2_story_frame",
                     *(["numeric_v2_chapter_generation", "numeric_v2_opening_state_extraction"] * 3),
                     "numeric_v2_ending_generation", "numeric_v2_opening_state_extraction"]
    assert [state["completed_chapters"] for state in checkpoints] == [0, 0, 1, 1, 2, 2, 3, 3, 3]
    assert checkpoints[2]["candidate"]["mainline_chapters"][0] == outline["mainline_chapters"][0]


@pytest.mark.parametrize("reported_issue", [False, True])
def test_failed_scene_resume_keeps_frame_and_completed_scene_without_model_replay(reported_issue):
    outline = _idea_outline(3)
    checkpoints = []

    def fail_second(messages, **options):
        context = json.loads(messages[1]["content"])
        if options["operation"] == "numeric_v2_chapter_generation" and context["chapter_index"] == 2:
            return LLMCallFailure("timeout", error_code="model_timeout", exception_type="TimeoutError")
        return staged_reply(messages, options["operation"], outline)

    with pytest.raises(NumericV2GenerationError) as caught:
        NumericV2Generator(fail_second).generate_staged(
            title="保留前幕", setup=_generation_setup(), save_checkpoint=checkpoints.append)
    checkpoint = caught.value.checkpoint
    assert checkpoint["completed_chapters"] == 1
    assert checkpoint["candidate"]["mainline_chapters"][0] == outline["mainline_chapters"][0]
    assert caught.value.attempts == 6
    if reported_issue:
        # Semantic review feedback cannot be reproduced by the structural validator.
        checkpoint["issues"] = [{"code": "opening_timing", "path": "mainline_chapters[1].opening_scene",
                                 "message": "后续事件尚未发生，开场不能当作已完成。"}]
    before = deepcopy(checkpoint)
    calls = []

    def resume(messages, **options):
        context = json.loads(messages[1]["content"])
        calls.append((options["operation"], context.get("chapter_index")))
        if options["operation"] == "numeric_v2_chapter_generation" and context["chapter_index"] == 2:
            assert context["issues"] == ([{
                **checkpoint["issues"][0], "path": "chapter.opening_scene",
                "source_path": "mainline_chapters[1].opening_scene",
            }] if reported_issue else [])
        return staged_reply(messages, options["operation"], outline)

    result = NumericV2Generator(resume).generate_staged(
        title="保留前幕", setup=_generation_setup(), checkpoint=checkpoint)
    assert checkpoint == before
    assert calls == [("numeric_v2_chapter_generation", 2), ("numeric_v2_opening_state_extraction", None),
                     ("numeric_v2_chapter_generation", 3), ("numeric_v2_opening_state_extraction", None),
                     ("numeric_v2_ending_generation", None), ("numeric_v2_opening_state_extraction", None)]
    NumericV2Compiler().compile_v2_2(result["story"])


def test_staged_current_scene_repair_does_not_change_prior_scene():
    outline = _idea_outline(3)
    calls = []

    def model(messages, **options):
        context = json.loads(messages[1]["content"])
        calls.append((options["operation"], context.get("chapter_index")))
        raw = staged_reply(messages, options["operation"], outline)
        if calls.count(("numeric_v2_chapter_generation", 2)) == 1 and context.get("chapter_index") == 2:
            broken = json.loads(raw)
            broken["character_state_stage"]["chapter_index"] = 9
            return json.dumps(broken, ensure_ascii=False)
        if options["operation"] == "numeric_v2_chapter_generation" and context["chapter_index"] == 2:
            issue = next(issue for issue in context["issues"]
                         if issue["source_path"] == "character_state_arc.stages[1].chapter_index")
            assert issue["path"] == "character_state_stage.chapter_index"
        return raw

    result = NumericV2Generator(model).generate_staged(title="局部修订", setup=_generation_setup())
    assert calls.count(("numeric_v2_chapter_generation", 1)) == 1
    assert calls.count(("numeric_v2_chapter_generation", 2)) == 2
    assert calls.count(("numeric_v2_chapter_generation", 3)) == 1
    NumericV2Compiler().compile_v2_2(result["story"])


@pytest.mark.parametrize("legacy_excerpt", [False, True])
def test_failed_state_extraction_resumes_without_rewriting_the_written_scene(legacy_excerpt):
    outline = _idea_outline(3)
    for index, chapter in enumerate(outline["mainline_chapters"]):
        chapter["opening_scene"] += f"这是第{index + 1}幕的场景。"
    opening = outline["mainline_chapters"][1]["opening_scene"]

    def fail_state(messages, **options):
        context = json.loads(messages[1]["content"])
        if options["operation"] == "numeric_v2_opening_state_extraction":
            # The extractor sees the current opening, never prior-state or whole-plot anchors.
            assert set(context) == {"opening_scene", "entry_bridge", "cast_names", "output_contract", "current_candidate", "issues"}
            if context["opening_scene"] == opening:
                return LLMCallFailure("timeout", error_code="model_timeout", exception_type="TimeoutError")
        return staged_reply(messages, options["operation"], outline)

    with pytest.raises(NumericV2GenerationError) as caught:
        NumericV2Generator(fail_state).generate_staged(title="状态摘录续写", setup=_generation_setup())
    checkpoint = caught.value.checkpoint
    assert checkpoint["completed_chapters"] == 1
    assert checkpoint["written_scene"]["chapter"] == outline["mainline_chapters"][1]
    if legacy_excerpt:
        checkpoint["phase_candidate"] = {key: outline["character_state_arc"]["stages"][1][key]
                                         for key in ("catgirl_state", "player_state", "environment_state")}
    before = deepcopy(checkpoint)
    calls = []

    def resume(messages, **options):
        calls.append(options["operation"])
        return staged_reply(messages, options["operation"], outline)

    result = NumericV2Generator(resume).generate_staged(
        title="状态摘录续写", setup=_generation_setup(), checkpoint=checkpoint)
    assert checkpoint == before
    assert calls == ["numeric_v2_opening_state_extraction", "numeric_v2_chapter_generation",
                     "numeric_v2_opening_state_extraction", "numeric_v2_ending_generation",
                     "numeric_v2_opening_state_extraction"]
    NumericV2Compiler().compile_v2_2(result["story"])


def test_invented_time_anchor_only_retries_the_current_excerpt():
    outline = _idea_outline(3)
    calls = []

    def model(messages, **options):
        context = json.loads(messages[1]["content"])
        calls.append(options["operation"])
        reply = json.loads(staged_reply(messages, options["operation"], outline))
        if options["operation"] == "numeric_v2_opening_state_extraction":
            if calls.count(options["operation"]) == 1:
                reply["temporal_context"] = {"anchor": "次日清晨", "relation_to_previous": "next_day"}
            elif calls.count(options["operation"]) == 2:
                assert any(issue["code"] == "temporal_anchor_without_source" for issue in context["issues"])
        return json.dumps(reply, ensure_ascii=False)

    result = NumericV2Generator(model).generate_staged(title="时间依据", setup=_generation_setup())
    assert calls.count("numeric_v2_chapter_generation") == 3
    assert calls.count("numeric_v2_opening_state_extraction") == 5
    assert "temporal_context" not in result["story"]["nodes"][0]["story_beat"]["character_state"]



@pytest.mark.parametrize("unknown_time", [False, True])
def test_opening_excerpt_replaces_writer_situation_and_carries_explicit_time(unknown_time):
    outline = _idea_outline(3)
    outline["mainline_chapters"][0]["opening_scene"] = "午后，女主看着封好的信，男主尚未作出决定。"
    good = "女主对信中未确定的事情仍有顾虑，等待当下交流。"
    calls = []

    def model(messages, **options):
        context = json.loads(messages[1]["content"])
        calls.append(options["operation"])
        reply = json.loads(staged_reply(messages, options["operation"], outline))
        if options["operation"] == "numeric_v2_chapter_generation" and context["chapter_index"] == 1:
            reply["chapter"]["catgirl_situation"] = "女主已经收好信，男主已经答应。"
            reply["character_state_stage"]["temporal_context"] = {"anchor": "", "relation_to_previous": "unknown-writer-plan"}
        if options["operation"] == "numeric_v2_opening_state_extraction" and context["opening_scene"] == outline["mainline_chapters"][0]["opening_scene"]:
            reply["catgirl_situation"] = good
            reply["temporal_context"] = {"anchor": "" if unknown_time else "午后", "relation_to_previous": "unspecified"}
        return json.dumps(reply, ensure_ascii=False)

    result = NumericV2Generator(model).generate_staged(title="统一当前处境", setup=_generation_setup())
    node = result["story"]["nodes"][0]
    assert good in node["story_beat"]["catgirl_situation"]
    assert "已经答应" not in node["story_beat"]["catgirl_situation"]
    if unknown_time:
        assert "temporal_context" not in node["story_beat"]["character_state"]
    else:
        assert node["story_beat"]["character_state"]["temporal_context"] == {"anchor": "午后", "relation_to_previous": "unspecified"}
    assert len(calls) == 9
    NumericV2Compiler().compile_v2_2(result["story"])
