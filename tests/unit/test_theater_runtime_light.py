"""验证轻量 Runtime 的启动、推进、自由对话和事务能力。"""  # noqa: DOCSTRING_CJK

import asyncio
from copy import deepcopy
import json

import pytest

from services.theater import (
    model_trace,
    observability,
    rules,
    runtime,
    session_store,
    story_graph,
    story_loader,
    turn_service,
)
from tests.utils.theater_story_fixture import (
    THEATER_TEST_ANCHOR_NODE_ID,
    THEATER_TEST_EXCHANGE_NODE_ID,
    THEATER_TEST_START_NODE_ID,
    THEATER_TEST_STORY_ID,
)


@pytest.mark.asyncio
async def test_choice_roleplay_restore_and_exit(tmp_path):
    """一场演出可以推进、自由回应、恢复并主动离场。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(root, lanlan_name="测试猫娘")
    assert started["ok"] is True
    assert started["state_revision"] == 0
    assert started["suggestion_options"]
    saved_start = await session_store.load_session(root, started["session_id"])
    assert saved_start["schema_version"] == session_store.SESSION_SCHEMA_VERSION
    assert saved_start["story_revision"]
    assert saved_start["llm_return_records"] == []
    assert saved_start["turn_causality_records"] == []

    roleplay = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="我想先听听你的心情",
        client_turn_id="turn_roleplay",
        base_revision=0,
    )
    assert roleplay["scenario_trace"]["progress_kind"] == "roleplay_response"
    assert roleplay["suggestion_options"]

    choice = roleplay["suggestion_options"][0]
    progressed = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="choice",
        choice_id=choice["choice_id"],
        client_turn_id="turn_choice",
        base_revision=1,
    )
    assert progressed["scenario_trace"]["progress_kind"] == "graph_progress"
    restored = await runtime.get_state(root, started["session_id"])
    assert restored["state_revision"] == 2
    assert restored["dialogue"] == progressed["dialogue"]

    exited = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="user_exit",
        client_turn_id="turn_exit",
        base_revision=2,
    )
    assert exited["ending"]["reason"] == "user_exit"
    assert exited["can_resume"] is False
    assert exited["session_lifecycle"] == "ended"
    saved_exit = await session_store.load_session(root, started["session_id"])
    assert saved_exit["end_reason"] == "user_exit"


@pytest.mark.asyncio
async def test_successful_turn_persists_private_model_returns_once(
    monkeypatch, tmp_path
):
    """成功回合原子保存全部模型返回，幂等重放不重复追加且公开响应不泄漏。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "model_return_session"
    started = await runtime.start_session(
        root, lanlan_name="测试猫娘", story_id=THEATER_TEST_STORY_ID
    )

    async def _fake_route(**_kwargs):
        """模拟 Router 已经经过统一模型入口并留下原始供应商正文。"""  # noqa: DOCSTRING_CJK
        model_trace.record_model_return(
            call_type="theater_router",
            surface="free_input",
            status="success",
            model="router-model",
            provider_type="openai",
            content='{"route_kind":"idle","private_trace_marker":"router-only"}',
        )
        return {
            "route_kind": "idle",
            "matched_choice_id": "",
            "authored_intent_id": "",
            "free_intent": {},
            "residual_intent": {},
            "response_focus": {
                "focus_type": "object",
                "evidence_excerpt": "今天的云层",
                "requires_state_change": False,
            },
        }

    async def _fake_performance(**_kwargs):
        """模拟 Actor 返回被解析后，Session 仍保留解析前的完整原始正文。"""  # noqa: DOCSTRING_CJK
        model_trace.record_model_return(
            call_type="theater_actor",
            surface="roleplay_response",
            status="success",
            model="actor-model",
            provider_type="openai",
            content='{"dialogue":"公开回应","private_trace_marker":"actor-only"}',
        )
        return {
            "narration": "她顺着你的视线望向窗外。",
            "dialogue": "今天的云确实很适合慢慢看喵。",
            "choice_rewrites": [],
        }

    monkeypatch.setattr(turn_service.llm, "route_free_input_async", _fake_route)
    monkeypatch.setattr(turn_service.llm, "generate_turn_async", _fake_performance)
    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="我想先聊一会儿今天的云层",
        client_turn_id="turn_model_trace_once",
        base_revision=0,
    )

    assert result["ok"] is True
    assert "private_trace_marker" not in json.dumps(result, ensure_ascii=False)
    saved = await session_store.load_session(root, started["session_id"])
    records = saved["llm_return_records"]
    assert [item["call_index"] for item in records] == [0, 1]
    assert [item["call_type"] for item in records] == [
        "theater_router",
        "theater_actor",
    ]
    assert [item["client_turn_id"] for item in records] == [
        "turn_model_trace_once",
        "turn_model_trace_once",
    ]
    assert all(item["session_id"] == started["session_id"] for item in records)
    assert all(item["base_revision"] == 0 for item in records)
    assert all(item["result_revision"] == 1 for item in records)
    assert "router-only" in records[0]["content"]
    assert "actor-only" in records[1]["content"]
    assert "private_trace_marker" not in json.dumps(
        saved["public_snapshot"], ensure_ascii=False
    )
    causal_records = saved["turn_causality_records"]
    assert len(causal_records) == 1
    causal = causal_records[0]
    assert causal["client_turn_id"] == "turn_model_trace_once"
    assert causal["base_revision"] == 0
    assert causal["result_revision"] == 1
    assert causal["input"] == {
        "input_kind": "free_input",
        "message": "我想先聊一会儿今天的云层",
        "choice_id": "",
    }
    assert causal["response_focus"] == {
        "focus_type": "object",
        "evidence_excerpt": "今天的云层",
        "requires_state_change": False,
    }
    assert causal["model_return_refs"] == [
        {
            "call_index": 0,
            "call_type": "theater_router",
            "surface": "free_input",
            "status": "success",
        },
        {
            "call_index": 1,
            "call_type": "theater_actor",
            "surface": "roleplay_response",
            "status": "success",
        },
    ]
    assert causal["final_public_output"] == {
        "narration": result["narration"],
        "dialogue": result["dialogue"],
        "scenario_trace": result["scenario_trace"],
        "ending": result["ending"],
    }
    assert causal["commit_summary"]["narrative_facts_added"] == []
    assert causal["commit_summary"]["session_ended"] is False
    assert causal["commit_summary"]["session_ended"] is False
    assert "turn_causality_records" not in json.dumps(result, ensure_ascii=False)
    assert "turn_causality_records" not in json.dumps(
        saved["public_snapshot"], ensure_ascii=False
    )

    replay = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="我想先聊一会儿今天的云层",
        client_turn_id="turn_model_trace_once",
        base_revision=0,
    )
    assert replay["ok"] is True
    replayed_session = await session_store.load_session(root, started["session_id"])
    assert len(replayed_session["llm_return_records"]) == 2
    assert len(replayed_session["turn_causality_records"]) == 1


@pytest.mark.asyncio
async def test_turn_causality_lazily_migrates_old_session_on_success(tmp_path):
    """旧 Session 缺少私有因果字段时只在下一次成功提交中补齐。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "causality_lazy_migration"
    started = await runtime.start_session(root, lanlan_name="旧档测试猫娘")
    session = await session_store.load_session(root, started["session_id"])
    session.pop("turn_causality_records")
    await session_store.save_session(root, session)

    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="先确认旧存档还能继续",
        client_turn_id="turn_causality_lazy_migration",
        base_revision=0,
    )

    assert result["ok"] is True
    saved = await session_store.load_session(root, started["session_id"])
    assert len(saved["turn_causality_records"]) == 1
    assert saved["turn_causality_records"][0]["result_revision"] == 1


@pytest.mark.asyncio
async def test_turn_causality_rejects_corrupt_private_record_container(tmp_path):
    """私有因果字段类型损坏时保留原文件，不能用新回合静默覆盖证据。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "causality_corrupt_container"
    started = await runtime.start_session(root, lanlan_name="坏档测试猫娘")
    session = await session_store.load_session(root, started["session_id"])
    session["turn_causality_records"] = {"broken": True}
    await session_store.save_session(root, session)

    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="这次输入不应该覆盖坏记录",
        client_turn_id="turn_causality_corrupt_container",
        base_revision=0,
    )

    assert result["ok"] is False
    assert result["reason"] == "session_state_invalid"
    saved = await session_store.load_session(root, started["session_id"])
    assert saved["state_revision"] == 0
    assert saved["turn_causality_records"] == {"broken": True}


@pytest.mark.asyncio
async def test_turn_causality_keeps_only_latest_32_successful_turns(tmp_path):
    """私有因果记录与幂等缓存使用同一上限，长演绎不会无限放大 Session。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "causality_retention"
    started = await runtime.start_session(root, lanlan_name="长演绎测试猫娘")

    for revision in range(33):
        result = await runtime.submit_input(
            root,
            session_id=started["session_id"],
            input_kind="free_input",
            message=f"第 {revision + 1} 次普通确认",
            client_turn_id=f"turn_causality_retention_{revision}",
            base_revision=revision,
        )
        assert result["ok"] is True

    saved = await session_store.load_session(root, started["session_id"])
    records = saved["turn_causality_records"]
    assert len(records) == 32
    assert records[0]["result_revision"] == 2
    assert records[-1]["result_revision"] == 33


@pytest.mark.asyncio
async def test_long_free_input_tail_cannot_advance_authority(monkeypatch, tmp_path):
    """长输入句尾否定若无法完整进入模型，节点、意图和权威事实必须保持不变。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "long_input_context"
    started = await runtime.start_session(root, lanlan_name="测试猫娘")
    before = await session_store.load_session(root, started["session_id"])
    before_state = deepcopy(before["story_state"])
    model_calls = 0

    class _ConfiguredModel:
        """提供占位模型配置，确保测试验证的是上下文门禁而非缺少配置。"""  # noqa: DOCSTRING_CJK

        def get_model_api_config(self, _tier):
            """返回不会真正使用的占位配置。"""  # noqa: DOCSTRING_CJK
            return {"model": "fake-model", "base_url": "https://example.invalid"}

    async def _unexpected_model_call(*_args, **_kwargs):
        """Router 和 Actor 都不应收到被截断的本轮原话。"""  # noqa: DOCSTRING_CJK
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("truncated free input must not reach model")

    monkeypatch.setattr(turn_service.llm, "_invoke_model_once", _unexpected_model_call)
    long_message = "我准备执行当前推荐行动，" * 220 + "但是最后决定不要执行"
    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message=long_message,
        client_turn_id="turn_long_context_incomplete",
        base_revision=0,
        config_manager=_ConfiguredModel(),
    )
    after = await session_store.load_session(root, started["session_id"])

    assert result["ok"] is True
    assert result["scenario_trace"]["progress_kind"] == "roleplay_response"
    assert after["story_state"]["current_node_id"] == before_state["current_node_id"]
    assert after["story_state"]["narrative_facts"] == before_state["narrative_facts"]
    assert after["story_state"]["scene_notes"] == before_state["scene_notes"]
    assert all(
        long_message not in str(item)
        for item in turn_service.llm._recent_public_turns(after["turns"])
    )
    assert "最后决定不要执行" not in result["dialogue"]["text"]
    assert model_calls == 0


@pytest.mark.asyncio
async def test_long_input_prefix_cannot_reenter_next_turn_router_context(
    monkeypatch, tmp_path
):
    """首轮长输入的正向前缀不能在下一轮被截断后重新送入权威 Router。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "long_input_two_turns"
    started = await runtime.start_session(root, lanlan_name="测试猫娘")
    before = await session_store.load_session(root, started["session_id"])
    long_message = "执行当前推荐行动，" * 180 + "但是最后决定不要执行"
    prompts: list[str] = []

    class _ConfiguredModel:
        """提供占位配置，第二轮才允许进入本地假模型。"""  # noqa: DOCSTRING_CJK

        def get_model_api_config(self, _tier):
            """返回不会访问网络的占位配置。"""  # noqa: DOCSTRING_CJK
            return {"model": "fake-model", "base_url": "https://example.invalid"}

    async def _fake_model_call(
        _api, _system_prompt, user_prompt, *, call_type, **_kwargs
    ):
        """验证第二轮 Prompt 不含第一轮残缺语义，并返回不推进的合法结构。"""  # noqa: DOCSTRING_CJK
        prompts.append(user_prompt)
        assert "执行当前推荐行动" not in user_prompt
        assert "最后决定不要执行" not in user_prompt
        if call_type == "theater_router":
            return type("Result", (), {"content": '{"route_kind":"idle"}'})()
        return type(
            "Result",
            (),
            {
                "content": (
                    '{"narration":"","dialogue":"我会先确认清楚再继续。",'
                    '"choice_rewrites":[]}'
                )
            },
        )()

    monkeypatch.setattr(turn_service.llm, "_invoke_model_once", _fake_model_call)
    first = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message=long_message,
        client_turn_id="turn_long_prefix_first",
        base_revision=0,
        config_manager=_ConfiguredModel(),
    )
    second = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="继续",
        client_turn_id="turn_long_prefix_second",
        base_revision=first["state_revision"],
        config_manager=_ConfiguredModel(),
    )
    saved = await session_store.load_session(root, started["session_id"])

    assert second["ok"] is True
    assert (
        saved["story_state"]["current_node_id"]
        == before["story_state"]["current_node_id"]
    )
    assert prompts


@pytest.mark.asyncio
async def test_start_uses_author_initial_scene_id(monkeypatch, tmp_path):
    """同一 phase 有多个场景时，开场必须使用作者指定的 initial_scene_id。"""  # noqa: DOCSTRING_CJK
    story = {
        "id": "scene-choice",
        "title": "场景选择测试",
        "initial_scene_id": "scene_selected",
        "opening_dialogue": "从指定场景开始喵。",
        "scenes": [
            {
                "id": "scene_wrong",
                "phase": "setup",
                "title": "错误场景",
                "text": "不应显示",
            },
            {
                "id": "scene_selected",
                "phase": "setup",
                "title": "指定场景",
                "text": "正确开场",
            },
        ],
        "narrative_nodes": [
            {
                "node_id": "node_start",
                "belong_phase": "setup",
                "node_type": "seed",
                "state_diff": {"add": []},
            }
        ],
        "edges": [],
    }

    async def _load_story(_story_id):
        """返回包含同 phase 多场景的可控故事。"""  # noqa: DOCSTRING_CJK
        return story

    monkeypatch.setattr(runtime.story_loader, "load_story", _load_story)
    started = await runtime.start_session(tmp_path / "theater", lanlan_name="测试猫娘")
    assert started["scene"] == {
        "scene_id": "scene_selected",
        "title": "指定场景",
        "text": "正确开场",
    }


@pytest.mark.asyncio
async def test_start_uses_author_opening_dialogue_without_model_rewrite(
    monkeypatch, tmp_path
):
    """正式开场对白必须逐字来自 Story，模型和框架都不能代写。"""  # noqa: DOCSTRING_CJK

    class _CurrentCatgirlConfig:
        """只提供 Runtime 角色归属校验所需的当前猫娘。"""  # noqa: DOCSTRING_CJK

        def load_characters(self):
            return {"当前猫娘": "霜瞳", "猫娘": {"霜瞳": {}}}

    async def _unexpected_opening_model(**_kwargs):
        """开场作者已提供完整对白时，不应再取得模型改写。"""  # noqa: DOCSTRING_CJK
        raise AssertionError("作者开场对白不应交给模型改写")

    # 监控当前回合服务实际使用的模型入口，避免为了旧 runtime 挂点保留无用导入。
    monkeypatch.setattr(
        turn_service.llm, "generate_turn_async", _unexpected_opening_model
    )
    started = await runtime.start_session(
        tmp_path / "theater",
        lanlan_name="霜瞳",
        story_id=THEATER_TEST_STORY_ID,
        config_manager=_CurrentCatgirlConfig(),
    )

    story = await story_loader.load_story_exact(THEATER_TEST_STORY_ID)
    author_opening = story["opening_dialogue"]
    assert started["dialogue"]["text"] == author_opening
    assert author_opening.startswith("测试牌已经放在桌上")
    assert "公开可见的步骤" in author_opening


@pytest.mark.asyncio
async def test_graph_progress_gives_model_the_next_visible_choices(
    monkeypatch, tmp_path
):
    """人格化当前对白时必须提供下一轮按钮，避免模型省略按钮所依赖的剧情邀请。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="霜瞳", story_id=THEATER_TEST_STORY_ID
    )
    captured = {}

    async def _fake_performance(**kwargs):
        captured.update(kwargs)
        return {
            "narration": kwargs["callback"],
            "dialogue": "测试牌的编号已经确认，我们继续核对公开交换步骤。",
            "choice_rewrites": [],
            "matched_choice_id": "",
        }

    monkeypatch.setattr("services.theater.llm.generate_turn_async", _fake_performance)
    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="choice",
        choice_id="choice_confirm_test_token",
        client_turn_id="turn_personalized_handoff",
        base_revision=0,
    )

    assert captured["progress_kind"] == "graph_progress"
    assert [item["choice_id"] for item in captured["choice_options"]] == [
        "choice_complete_public_exchange",
    ]
    assert [item["choice_id"] for item in result["suggestion_options"]] == [
        "choice_complete_public_exchange",
    ]
    assert result["dialogue"]["text"] == captured["node"]["scripted_dialogue"]


@pytest.mark.asyncio
async def test_management_end_and_legacy_dormancy_have_distinct_lifecycle(
    tmp_path,
):
    """管理结束不可恢复，旧版休眠存档则必须原样保留剧情并允许继续。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    manually_started = await runtime.start_session(root, lanlan_name="手动结束猫娘")
    assert (await runtime.end_session(root, session_id=manually_started["session_id"]))[
        "ok"
    ] is True
    manual_state = await runtime.get_state(root, manually_started["session_id"])
    assert manual_state["phase"] == "ended"
    assert manual_state["can_resume"] is False
    assert manual_state["session_lifecycle"] == "ended"
    saved_manual = await session_store.load_session(
        root, manually_started["session_id"]
    )
    assert saved_manual["end_reason"] == "management_end"

    dormant_started = await runtime.start_session(root, lanlan_name="休眠猫娘")
    dormant_session = await session_store.load_session(
        root, dormant_started["session_id"]
    )
    dormant_session["updated_at"] = 1
    before_phase = dormant_session["phase"]
    before_revision = dormant_session["state_revision"]
    before_story_state = deepcopy(dormant_session["story_state"])
    before_options = deepcopy(dormant_session["public_snapshot"]["suggestion_options"])
    legacy_dormant_at = 86_400_002
    # 方案 A 不再自动生成休眠；这里直接构造旧持久化字段，证明升级后仍能无损恢复。
    dormant_session["dormant_at"] = legacy_dormant_at
    dormant_session["public_snapshot"]["session_lifecycle"] = "dormant"
    await session_store.save_session(root, dormant_session)
    dormant_state = await runtime.get_state(root, dormant_started["session_id"])
    active_state = await runtime.get_active_state(root, lanlan_name="休眠猫娘")
    assert dormant_state["phase"] == before_phase
    assert dormant_state["can_resume"] is True
    assert dormant_state["session_lifecycle"] == "dormant"
    assert dormant_state["suggestion_options"] == before_options
    assert active_state["session_id"] == dormant_started["session_id"]
    assert active_state["session_lifecycle"] == "dormant"
    saved_dormant = await session_store.load_session(
        root, dormant_started["session_id"]
    )
    assert saved_dormant["dormant_at"] == legacy_dormant_at
    assert saved_dormant["ended_at"] is None
    assert saved_dormant["phase"] == before_phase
    assert saved_dormant["state_revision"] == before_revision
    assert saved_dormant["story_state"] == before_story_state
    assert saved_dormant["updated_at"] == 1
    assert saved_dormant["public_snapshot"]["can_resume"] is True
    assert saved_dormant["public_snapshot"]["suggestion_options"] == before_options


@pytest.mark.asyncio
async def test_only_successful_turn_wakes_dormant_session(tmp_path):
    """失败请求和幂等回放不能唤醒旧休眠存档，成功的新回合才可以。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(root, lanlan_name="测试猫娘")
    original = await session_store.load_session(root, started["session_id"])
    original["updated_at"] = 1
    legacy_dormant_at = 86_400_002
    original["dormant_at"] = legacy_dormant_at
    original["public_snapshot"]["session_lifecycle"] = "dormant"
    await session_store.save_session(root, original)

    rejected = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="choice",
        choice_id="choice_that_was_never_authored",
        client_turn_id="turn_dormant_rejected",
        base_revision=0,
    )
    assert rejected == {"ok": False, "reason": "choice_not_available"}
    after_rejected = await session_store.load_session(root, started["session_id"])
    assert after_rejected["dormant_at"] == legacy_dormant_at
    assert after_rejected["state_revision"] == 0

    committed = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="choice",
        choice_id=started["suggestion_options"][0]["choice_id"],
        client_turn_id="turn_dormant_wake",
        base_revision=0,
    )
    assert committed["ok"] is True
    assert committed["session_lifecycle"] == "active"
    assert committed["state_revision"] == 1
    after_committed = await session_store.load_session(root, started["session_id"])
    assert "dormant_at" not in after_committed
    assert after_committed["state_revision"] == 1

    cached_root = tmp_path / "cached"
    cached_started = await runtime.start_session(cached_root, lanlan_name="缓存猫娘")
    cached_request = dict(
        session_id=cached_started["session_id"],
        input_kind="free_input",
        message="这一轮已经提交",
        client_turn_id="turn_before_dormant_cache",
        base_revision=0,
    )
    first = await runtime.submit_input(cached_root, **cached_request)
    cached_session = await session_store.load_session(
        cached_root, cached_started["session_id"]
    )
    cached_session["updated_at"] = 1
    cached_session["dormant_at"] = legacy_dormant_at
    cached_session["public_snapshot"]["session_lifecycle"] = "dormant"
    await session_store.save_session(cached_root, cached_session)

    replay = await runtime.submit_input(cached_root, **cached_request)
    assert replay["state_revision"] == first["state_revision"]
    assert replay["session_lifecycle"] == "dormant"
    after_replay = await session_store.load_session(
        cached_root, cached_started["session_id"]
    )
    assert after_replay["dormant_at"] == legacy_dormant_at


@pytest.mark.asyncio
async def test_idempotency_and_revision_conflict(tmp_path):
    """重复请求回放首次结果，旧 revision 不得覆盖新状态。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(root, lanlan_name="测试猫娘")
    option = started["suggestion_options"][0]
    request = dict(
        session_id=started["session_id"],
        input_kind="choice",
        choice_id=option["choice_id"],
        client_turn_id="turn_same",
        base_revision=0,
    )
    first = await runtime.submit_input(root, **request)
    replay = await runtime.submit_input(root, **request)
    assert replay == first

    conflict = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="继续聊聊",
        client_turn_id="turn_conflict",
        base_revision=0,
    )
    assert conflict == {
        "ok": False,
        "reason": "state_revision_conflict",
        "retryable": True,
        "state_revision": 1,
    }


@pytest.mark.asyncio
async def test_free_input_rejects_oversized_message_before_persisting(tmp_path):
    """超长自由输入必须在调用模型和写入 Session 前被明确拒绝。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_input_cap"
    )

    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="很" * (turn_service.MAX_FREE_INPUT_CHARS + 1),
        client_turn_id="turn_oversized_input",
        base_revision=0,
    )

    assert result == {"ok": False, "reason": "free_input_too_long"}
    saved = await session_store.load_session(root, started["session_id"])
    assert saved["state_revision"] == 0
    assert len(saved["turns"]) == 1


@pytest.mark.asyncio
async def test_cached_nonterminal_turn_cannot_revive_stale_session(tmp_path):
    """旧 Session 被替换后，同一幂等 ID 也不能回放可恢复快照。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    old_session = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_cache_old"
    )
    request = dict(
        session_id=old_session["session_id"],
        input_kind="free_input",
        message="这轮结果会进入幂等缓存",
        client_turn_id="turn_cached_before_replace",
        base_revision=0,
    )
    committed = await runtime.submit_input(root, **request)
    assert committed["ok"] is True
    await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_cache_new"
    )

    replay = await runtime.submit_input(root, **request)

    assert replay == {"ok": False, "reason": "stale_session", "skipped": True}


@pytest.mark.asyncio
async def test_replaced_session_stays_ended_after_replacement_closes(tmp_path):
    """新演出结束并清空 active 后，被替换的旧 Session 也不能重新恢复。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    old_session = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_replaced_old"
    )
    replacement = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_replaced_new"
    )
    await runtime.end_session(root, session_id=replacement["session_id"])

    restored = await runtime.get_state(root, old_session["session_id"])
    submitted = await runtime.submit_input(
        root,
        session_id=old_session["session_id"],
        input_kind="free_input",
        message="旧演出不应复活",
        client_turn_id="turn_replaced_old",
        base_revision=0,
    )

    assert restored["can_resume"] is False
    assert restored["phase"] == "ended"
    assert submitted == {"ok": False, "reason": "session_ended"}
    saved_old = await session_store.load_session(root, old_session["session_id"])
    assert saved_old["end_reason"] == "replaced_by_new_session"


@pytest.mark.asyncio
async def test_cached_terminal_turn_remains_idempotent(tmp_path):
    """主动离场已经提交后，同一幂等 ID 重试仍返回原终局响应。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_exit_cache"
    )
    request = dict(
        session_id=started["session_id"],
        input_kind="user_exit",
        client_turn_id="turn_exit_cached",
        base_revision=0,
    )

    first = await runtime.submit_input(root, **request)
    replay = await runtime.submit_input(root, **request)

    assert replay == first
    assert replay["ending"]["reason"] == "user_exit"


@pytest.mark.asyncio
async def test_concurrent_start_retry_reuses_one_session(tmp_path):
    """同一开场幂等 ID 的并发请求只能创建并返回一个 Session。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    results = await asyncio.gather(
        runtime.start_session(
            root, lanlan_name="测试猫娘", client_start_id="start_same"
        ),
        runtime.start_session(
            root, lanlan_name="测试猫娘", client_start_id="start_same"
        ),
    )
    assert results[0] == results[1]
    assert len(await session_store.list_session_ids(root)) == 1
    saved = await session_store.load_session(root, results[0]["session_id"])
    assert saved["start_client_id"] == "start_same"


@pytest.mark.asyncio
async def test_start_rechecks_current_catgirl_after_waiting_for_character_lock(
    tmp_path,
):
    """开场等待旧角色锁期间切换猫娘后，不得创建旧角色 Session。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"

    class _MutableConfigManager:
        """模拟角色切换在开场请求排队期间发布新当前猫娘。"""  # noqa: DOCSTRING_CJK

        current_name = "旧猫娘"

        async def aload_characters(self):
            """返回调用时已经发布的当前猫娘。"""  # noqa: DOCSTRING_CJK
            return {"当前猫娘": self.current_name}

    config_manager = _MutableConfigManager()
    async with session_store.character_guard(root, "旧猫娘"):
        start_task = asyncio.create_task(
            runtime.start_session(
                root,
                lanlan_name="旧猫娘",
                client_start_id="start_waiting_character_switch",
                config_manager=config_manager,
            )
        )
        # 让开场任务进入角色锁等待，再模拟切换事务在同一边界内发布新角色。
        await asyncio.sleep(0)
        config_manager.current_name = "新猫娘"

    result = await start_task

    assert result == {"ok": False, "reason": "session_character_mismatch"}
    assert await session_store.list_session_ids(root) == []
    assert await session_store.load_active_sessions(root) == {}


@pytest.mark.asyncio
async def test_active_index_memory_changes_only_after_persistence(
    monkeypatch, tmp_path
):
    """活动索引写盘失败时不能提前发布只存在于内存的新映射。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"

    async def _load_active_sessions(_root):
        """模拟空的磁盘活动索引。"""  # noqa: DOCSTRING_CJK
        return {}

    async def _save_active_sessions(_root, _active):
        """模拟活动索引持久化失败。"""  # noqa: DOCSTRING_CJK
        raise OSError("disk unavailable")

    monkeypatch.setattr(session_store, "load_active_sessions", _load_active_sessions)
    monkeypatch.setattr(session_store, "save_active_sessions", _save_active_sessions)
    with pytest.raises(OSError, match="disk unavailable"):
        await session_store.set_active_session(
            root,
            "持久化失败猫娘",
            "theater_00000000-0000-0000-0000-000000000001",
        )
    cache_key = session_store._active_cache_key(root, "持久化失败猫娘")
    assert cache_key not in session_store._ACTIVE_BY_ROOT_AND_LANLAN


@pytest.mark.asyncio
async def test_failed_active_publication_ends_unpublished_replacement(
    monkeypatch, tmp_path
):
    """新 Session 发布失败后必须终结，索引重建不能让未公开剧情复活。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    original = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_published"
    )

    async def _fail_active_publication(_root, _lanlan_name, _session_id):
        """模拟新 Session 已保存但活动索引无法持久化。"""  # noqa: DOCSTRING_CJK
        raise OSError("active index unavailable")

    monkeypatch.setattr(session_store, "set_active_session", _fail_active_publication)
    with pytest.raises(OSError, match="active index unavailable"):
        await runtime.start_session(
            root, lanlan_name="测试猫娘", client_start_id="start_unpublished"
        )

    session_ids = await session_store.list_session_ids(root)
    unpublished_id = next(
        session_id for session_id in session_ids if session_id != original["session_id"]
    )
    unpublished = await session_store.load_session(root, unpublished_id)
    restored_original = await session_store.load_session(root, original["session_id"])

    assert unpublished["phase"] == "ended"
    assert unpublished["ended_at"]
    assert unpublished["public_snapshot"]["can_resume"] is False
    assert restored_original["ended_at"] is None


@pytest.mark.asyncio
async def test_dialogue_speech_claims_each_revision_once(tmp_path):
    """TTS 只能取得已提交的公开猫娘对白，同一 revision 的重试不得重复播报。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(root, lanlan_name="测试猫娘")
    first = await runtime.claim_dialogue_speech(
        root,
        session_id=started["session_id"],
        state_revision=0,
    )
    assert first["line"] == started["dialogue"]["text"]
    assert first["lanlan_name"] == "测试猫娘"

    replay = await runtime.claim_dialogue_speech(
        root,
        session_id=started["session_id"],
        state_revision=0,
    )
    assert replay["skipped"] == "already_spoken"
    stale = await runtime.claim_dialogue_speech(
        root,
        session_id=started["session_id"],
        state_revision=1,
    )
    assert stale == {"ok": True, "skipped": "stale_revision", "state_revision": 0}


@pytest.mark.asyncio
async def test_dialogue_claim_and_new_start_share_character_boundary(
    monkeypatch, tmp_path
):
    """旧对白认领写盘完成前，同猫娘新开场不能先替换 active Session。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    old_session = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_claim_old"
    )
    real_save_session = session_store.save_session
    claim_save_entered = asyncio.Event()
    release_claim_save = asyncio.Event()

    async def _pause_claim_save(target_root, session):
        """只暂停旧 Session 的已朗读 revision 写盘，制造 active 切换竞争窗口。"""  # noqa: DOCSTRING_CJK
        if session.get("session_id") == old_session["session_id"] and session.get(
            "spoken_dialogue_revisions"
        ) == [0]:
            claim_save_entered.set()
            await release_claim_save.wait()
        await real_save_session(target_root, session)

    monkeypatch.setattr(session_store, "save_session", _pause_claim_save)
    claim_task = asyncio.create_task(
        runtime.claim_dialogue_speech(
            root, session_id=old_session["session_id"], state_revision=0
        )
    )
    await claim_save_entered.wait()
    start_task = asyncio.create_task(
        runtime.start_session(
            root, lanlan_name="测试猫娘", client_start_id="start_claim_new"
        )
    )
    done, _pending = await asyncio.wait({start_task}, timeout=0.05)

    assert not done
    release_claim_save.set()
    claim, replacement = await asyncio.gather(claim_task, start_task)
    assert claim["line"] == old_session["dialogue"]["text"]
    assert replacement["session_id"] != old_session["session_id"]


@pytest.mark.asyncio
async def test_dialogue_playback_submission_holds_character_boundary(tmp_path):
    """对白进入 TTS 管线前，新开场不能越过同一角色原子边界。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    old_session = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_play_old"
    )
    playback_entered = asyncio.Event()
    release_playback = asyncio.Event()

    async def _pause_playback(claim):
        """暂停 TTS 提交，验证角色锁覆盖认领返回后的原竞态窗口。"""  # noqa: DOCSTRING_CJK
        playback_entered.set()
        await release_playback.wait()
        return {"ok": True, "line": claim["line"], "audio_queued": True}

    playback_task = asyncio.create_task(
        runtime.claim_dialogue_speech(
            root,
            session_id=old_session["session_id"],
            state_revision=0,
            play=_pause_playback,
        )
    )
    await playback_entered.wait()
    start_task = asyncio.create_task(
        runtime.start_session(
            root, lanlan_name="测试猫娘", client_start_id="start_play_new"
        )
    )
    done, _pending = await asyncio.wait({start_task}, timeout=0.05)

    assert not done
    release_playback.set()
    played, replacement = await asyncio.gather(playback_task, start_task)
    assert played["audio_queued"] is True
    assert replacement["session_id"] != old_session["session_id"]


@pytest.mark.asyncio
async def test_character_publication_waits_for_dialogue_playback(tmp_path):
    """当前猫娘配置不能在旧对白仍向 TTS 提交时提前发布。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="旧猫娘", client_start_id="start_before_publish"
    )
    playback_entered = asyncio.Event()
    release_playback = asyncio.Event()
    published_names = []

    async def _pause_playback(claim):
        """暂停旧对白提交，暴露角色发布与 TTS 的竞争窗口。"""  # noqa: DOCSTRING_CJK
        playback_entered.set()
        await release_playback.wait()
        return {"ok": True, "line": claim["line"], "audio_queued": True}

    async def _publish_new_character():
        """记录配置发布时点，不读写真实角色文件。"""  # noqa: DOCSTRING_CJK
        published_names.append("新猫娘")

    playback_task = asyncio.create_task(
        runtime.claim_dialogue_speech(
            root,
            session_id=started["session_id"],
            state_revision=0,
            play=_pause_playback,
        )
    )
    await playback_entered.wait()
    switch_task = asyncio.create_task(
        runtime.publish_character_switch(
            root,
            old_lanlan_name="旧猫娘",
            publish=_publish_new_character,
        )
    )
    done, _pending = await asyncio.wait({switch_task}, timeout=0.05)

    assert not done
    assert published_names == []
    release_playback.set()
    played, switched = await asyncio.gather(playback_task, switch_task)
    assert played["audio_queued"] is True
    assert switched["cleared"] is True
    assert published_names == ["新猫娘"]
    saved = await session_store.load_session(root, started["session_id"])
    assert saved["ended_at"]
    assert saved["end_reason"] == "character_switch"


@pytest.mark.asyncio
async def test_stale_session_dialogue_cannot_claim_tts(tmp_path):
    """被新开场替代的旧 Session 不得抢播对白或中断当前演出。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    old_session = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_old"
    )
    await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_new"
    )

    claim = await runtime.claim_dialogue_speech(
        root,
        session_id=old_session["session_id"],
        state_revision=0,
    )
    assert claim == {"ok": True, "skipped": "stale_session", "state_revision": 0}


@pytest.mark.asyncio
async def test_ended_session_dialogue_cannot_claim_tts(tmp_path):
    """角色切换结束并清空 active 索引后，旧 Session 仍不得认领对白。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="旧猫娘", client_start_id="start_before_switch"
    )
    assert (await runtime.end_session(root, session_id=started["session_id"]))[
        "ok"
    ] is True

    claim = await runtime.claim_dialogue_speech(
        root,
        session_id=started["session_id"],
        state_revision=0,
    )

    assert claim == {"ok": True, "skipped": "stale_session", "state_revision": 0}
    saved = await session_store.load_session(root, started["session_id"])
    assert saved["spoken_dialogue_revisions"] == []


@pytest.mark.asyncio
async def test_user_exit_does_not_create_character_dialogue_for_tts(tmp_path):
    """主动离场只显示管理态提示，不伪造角色对白或占用 TTS revision。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_terminal_tts"
    )
    exited = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="user_exit",
        client_turn_id="turn_terminal_tts",
        base_revision=0,
    )

    claim = await runtime.claim_dialogue_speech(
        root,
        session_id=started["session_id"],
        state_revision=exited["state_revision"],
    )

    assert exited["dialogue"]["text"] == ""
    assert claim == {
        "ok": True,
        "skipped": "empty_dialogue",
        "state_revision": exited["state_revision"],
    }


@pytest.mark.asyncio
async def test_switched_character_dialogue_cannot_claim_tts(tmp_path):
    """当前猫娘变化后，旧角色对白不得写入已朗读 revision。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="旧猫娘", client_start_id="start_old_tts_character"
    )

    claim = await runtime.claim_dialogue_speech(
        root,
        session_id=started["session_id"],
        state_revision=0,
        expected_lanlan_name="新猫娘",
    )

    assert claim == {"ok": True, "skipped": "character_changed", "state_revision": 0}
    saved = await session_store.load_session(root, started["session_id"])
    assert saved["spoken_dialogue_revisions"] == []


@pytest.mark.asyncio
async def test_turn_rechecks_stale_session_after_llm_returns(monkeypatch, tmp_path):
    """模型等待期间被新开场替换的旧 Session 不得再提交候选状态。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    old_session = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_old"
    )
    llm_entered = asyncio.Event()
    release_llm = asyncio.Event()

    async def _wait_for_replacement(**_kwargs):
        """暂停模型结果，给另一个窗口留下替换活动 Session 的确定窗口。"""  # noqa: DOCSTRING_CJK
        llm_entered.set()
        await release_llm.wait()
        return {
            "narration": "旧演绎不应提交。",
            "dialogue": "这句也不应保存喵。",
            "choice_rewrites": [],
        }

    monkeypatch.setattr(
        "services.theater.llm.generate_turn_async", _wait_for_replacement
    )
    pending_turn = asyncio.create_task(
        runtime.submit_input(
            root,
            session_id=old_session["session_id"],
            input_kind="free_input",
            message="等你回应时我打开了新窗口",
            client_turn_id="turn_waiting_llm",
            base_revision=0,
        )
    )
    await llm_entered.wait()
    replacement = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_new"
    )
    release_llm.set()

    assert (await pending_turn) == {
        "ok": False,
        "reason": "stale_session",
        "skipped": True,
    }
    assert replacement["session_id"] != old_session["session_id"]
    saved_old = await session_store.load_session(root, old_session["session_id"])
    assert saved_old["state_revision"] == 0
    assert len(saved_old["turns"]) == 1
    assert saved_old["turns"][0]["text"] == old_session["dialogue"]["text"]


@pytest.mark.asyncio
async def test_turn_commit_blocks_replacement_start_until_save_finishes(
    monkeypatch, tmp_path
):
    """旧回合从 stale 校验到写盘结束前，新开场不能替换 active Session。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_before_commit"
    )
    real_save_session = session_store.save_session
    candidate_save_entered = asyncio.Event()
    release_candidate_save = asyncio.Event()

    async def _pause_candidate_save(target_root, session):
        """只暂停 revision 1 的候选写盘，确定性暴露 stale 校验后的替换窗口。"""  # noqa: DOCSTRING_CJK
        if (
            session.get("session_id") == started["session_id"]
            and session.get("state_revision") == 1
        ):
            candidate_save_entered.set()
            await release_candidate_save.wait()
        await real_save_session(target_root, session)

    monkeypatch.setattr(session_store, "save_session", _pause_candidate_save)
    turn_task = asyncio.create_task(
        runtime.submit_input(
            root,
            session_id=started["session_id"],
            input_kind="free_input",
            message="这轮正在提交",
            client_turn_id="turn_atomic_commit",
            base_revision=0,
        )
    )
    await candidate_save_entered.wait()
    start_task = asyncio.create_task(
        runtime.start_session(
            root, lanlan_name="测试猫娘", client_start_id="start_after_commit"
        )
    )
    done, _pending = await asyncio.wait({start_task}, timeout=0.05)

    assert not done
    release_candidate_save.set()
    committed, replacement = await asyncio.gather(turn_task, start_task)
    assert committed["ok"] is True
    assert committed["state_revision"] == 1
    assert replacement["session_id"] != started["session_id"]


@pytest.mark.asyncio
async def test_turn_rechecks_current_catgirl_after_llm_returns(monkeypatch, tmp_path):
    """模型等待期间切换猫娘时，旧角色候选回合不得提交。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    old_session = await runtime.start_session(
        root, lanlan_name="旧猫娘", client_start_id="start_old_character"
    )
    llm_entered = asyncio.Event()
    release_llm = asyncio.Event()

    class _MutableConfigManager:
        """模拟角色切换先更新当前猫娘、后异步清理旧 Session 的时序。"""  # noqa: DOCSTRING_CJK

        current_name = "旧猫娘"

        async def aload_characters(self):
            """返回调用时的当前猫娘配置。"""  # noqa: DOCSTRING_CJK
            return {"当前猫娘": self.current_name}

    async def _wait_for_character_switch(**_kwargs):
        """暂停模型结果，暴露角色配置已经切换但 Session 尚未结束的窗口。"""  # noqa: DOCSTRING_CJK
        llm_entered.set()
        await release_llm.wait()
        return {
            "narration": "旧角色结果不应提交。",
            "dialogue": "这句也不应播放喵。",
            "choice_rewrites": [],
        }

    config_manager = _MutableConfigManager()
    monkeypatch.setattr(
        "services.theater.llm.generate_turn_async", _wait_for_character_switch
    )
    pending_turn = asyncio.create_task(
        runtime.submit_input(
            root,
            session_id=old_session["session_id"],
            input_kind="free_input",
            message="你回应时我切换了猫娘",
            client_turn_id="turn_waiting_character_switch",
            base_revision=0,
            config_manager=config_manager,
            expected_lanlan_name="旧猫娘",
        )
    )
    await llm_entered.wait()
    config_manager.current_name = "新猫娘"
    release_llm.set()

    assert (await pending_turn) == {"ok": False, "reason": "session_character_mismatch"}
    saved_old = await session_store.load_session(root, old_session["session_id"])
    assert saved_old["state_revision"] == 0
    assert len(saved_old["turns"]) == 1


@pytest.mark.asyncio
async def test_concurrent_turns_only_commit_one_revision(tmp_path):
    """同一 revision 的并发回合只有一个可以提交。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(root, lanlan_name="测试猫娘")

    async def submit(suffix: str):
        return await runtime.submit_input(
            root,
            session_id=started["session_id"],
            input_kind="free_input",
            message=f"这是并发输入{suffix}",
            client_turn_id=f"turn_{suffix}",
            base_revision=0,
        )

    results = await asyncio.gather(submit("a"), submit("b"))
    assert sum(result.get("ok") is True for result in results) == 1
    assert (
        sum(result.get("reason") == "state_revision_conflict" for result in results)
        == 1
    )


@pytest.mark.asyncio
async def test_active_session_restores_after_memory_index_reset(tmp_path):
    """进程内索引清空后仍可从文件恢复当前演出。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(root, lanlan_name="测试猫娘")
    session_store.reset_active_sessions_for_tests()
    restored = await runtime.get_active_state(root, lanlan_name="测试猫娘")
    assert restored["ok"] is True
    assert restored["session_id"] == started["session_id"]








@pytest.mark.asyncio
async def test_session_state_and_input_reject_another_catgirl(tmp_path):
    """本地旧 Session ID 不能恢复或推进其他猫娘的私有演绎。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    old_session = await runtime.start_session(
        root, lanlan_name="旧猫娘", client_start_id="start_old_catgirl"
    )

    restored = await runtime.get_state(
        root,
        old_session["session_id"],
        expected_lanlan_name="当前猫娘",
    )
    submitted = await runtime.submit_input(
        root,
        session_id=old_session["session_id"],
        input_kind="free_input",
        message="继续上一位猫娘的剧情",
        client_turn_id="turn_wrong_catgirl",
        base_revision=0,
        expected_lanlan_name="当前猫娘",
    )

    assert restored == {"ok": False, "reason": "session_character_mismatch"}
    assert submitted == {"ok": False, "reason": "session_character_mismatch"}
    saved = await session_store.load_session(root, old_session["session_id"])
    assert saved["state_revision"] == 0
    assert len(saved["turns"]) == 1


@pytest.mark.asyncio
async def test_active_session_cache_is_scoped_by_theater_root(tmp_path):
    """同名猫娘在不同数据根中必须分别读取各自的活动 Session。"""  # noqa: DOCSTRING_CJK
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    session_a = "theater_00000000-0000-0000-0000-000000000001"
    session_b = "theater_00000000-0000-0000-0000-000000000002"
    await session_store.save_active_sessions(root_a, {"同名猫娘": session_a})
    await session_store.save_active_sessions(root_b, {"同名猫娘": session_b})
    session_store.reset_active_sessions_for_tests()

    assert await session_store.get_active_session_id(root_a, "同名猫娘") == session_a
    assert await session_store.get_active_session_id(root_b, "同名猫娘") == session_b


@pytest.mark.asyncio
async def test_corrupt_active_session_index_recovers_as_empty(tmp_path):
    """损坏的活动索引不得阻断读取，并应能被下一次正常写入重建。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    path = session_store.active_sessions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"测试猫娘":', encoding="utf-8")
    session_store.reset_active_sessions_for_tests()

    assert await session_store.load_active_sessions(root) == {}

    session_id = "theater_00000000-0000-0000-0000-000000000009"
    await session_store.set_active_session(root, "测试猫娘", session_id)
    assert await session_store.load_active_sessions(root) == {"测试猫娘": session_id}


@pytest.mark.asyncio
async def test_corrupt_active_index_rebuilds_latest_unended_session(tmp_path):
    """索引损坏后必须恢复最新未结束演出，并继续把被替换 Session 判为 stale。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    old_session = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_rebuild_old"
    )
    replacement = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_rebuild_new"
    )
    path = session_store.active_sessions_path(root)
    path.write_text('{"测试猫娘":', encoding="utf-8")
    session_store.reset_active_sessions_for_tests()

    rebuilt = await session_store.load_active_sessions(root)
    old_saved = await session_store.load_session(root, old_session["session_id"])

    assert rebuilt == {"测试猫娘": replacement["session_id"]}
    assert old_saved["ended_at"]
    assert await session_store.is_stale_session(root, old_saved) is True


@pytest.mark.asyncio
async def test_invalid_active_index_payload_rebuilds_current_session(tmp_path):
    """合法 JSON 的错误顶层结构也必须重建，不能按空索引放行历史 Session。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="测试猫娘", client_start_id="start_invalid_index"
    )
    path = session_store.active_sessions_path(root)
    path.write_text("[]", encoding="utf-8")
    session_store.reset_active_sessions_for_tests()

    rebuilt = await session_store.load_active_sessions(root)

    assert rebuilt == {"测试猫娘": started["session_id"]}
    assert json.loads(path.read_text(encoding="utf-8")) == rebuilt


@pytest.mark.asyncio
async def test_active_session_index_serializes_updates_across_characters(
    monkeypatch, tmp_path
):
    """不同猫娘并发更新共享索引时必须保留双方映射。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    stored: dict[str, str] = {}
    load_count = 0
    first_save_entered = asyncio.Event()
    release_first_save = asyncio.Event()

    async def _load_active_sessions(_root):
        """返回当前测试索引副本，并记录并发读取次数。"""  # noqa: DOCSTRING_CJK
        nonlocal load_count
        load_count += 1
        return dict(stored)

    async def _save_active_sessions(_root, active):
        """暂停第一位猫娘的写入，制造可复现的读改写竞争窗口。"""  # noqa: DOCSTRING_CJK
        if "猫娘甲" in active and "猫娘乙" not in active:
            first_save_entered.set()
            await release_first_save.wait()
        stored.clear()
        stored.update(active)

    monkeypatch.setattr(session_store, "load_active_sessions", _load_active_sessions)
    monkeypatch.setattr(session_store, "save_active_sessions", _save_active_sessions)

    first = asyncio.create_task(
        session_store.set_active_session(
            root, "猫娘甲", "theater_00000000-0000-0000-0000-000000000001"
        )
    )
    await first_save_entered.wait()
    second = asyncio.create_task(
        session_store.set_active_session(
            root, "猫娘乙", "theater_00000000-0000-0000-0000-000000000002"
        )
    )
    await asyncio.sleep(0)
    # 第二次读必须等待第一轮完整写入，不能在旧索引上独立计算。
    assert load_count == 1
    release_first_save.set()
    await asyncio.gather(first, second)
    assert stored == {
        "猫娘甲": "theater_00000000-0000-0000-0000-000000000001",
        "猫娘乙": "theater_00000000-0000-0000-0000-000000000002",
    }


@pytest.mark.asyncio
async def test_free_input_matching_current_choice_advances_author_node(
    monkeypatch, tmp_path
):
    """自由输入明确完成当前 Choice 时复用稳定 ID 推进，不要求玩家重复点击。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="测试猫娘", story_id=THEATER_TEST_STORY_ID
    )
    selected = started["suggestion_options"][0]
    before = await session_store.load_session(root, started["session_id"])

    async def _fake_performance(**kwargs):
        """路由提交后，演绎模型只读取目标节点，不再决定是否推进。"""  # noqa: DOCSTRING_CJK
        return {
            "narration": kwargs["callback"],
            "dialogue": "我会从这一刻的选择继续回应你喵。",
            "choice_rewrites": [],
        }

    async def _fake_route(**kwargs):
        """模拟独立路由器从当前作者白名单中选中唯一推荐边。"""  # noqa: DOCSTRING_CJK
        return {
            "matched_choice_id": kwargs["choice_options"][0]["choice_id"],
            "observed_intent_id": "",
        }

    monkeypatch.setattr("services.theater.llm.generate_turn_async", _fake_performance)
    monkeypatch.setattr("services.theater.llm.route_free_input_async", _fake_route)
    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message=selected["label"],
        client_turn_id="turn_repeat_choice_label",
        base_revision=0,
    )
    assert result["scenario_trace"] == {
        "progress_kind": "graph_progress",
        "action_label": selected["label"],
    }
    saved = await session_store.load_session(root, started["session_id"])
    assert (
        saved["story_state"]["current_node_id"]
        != before["story_state"]["current_node_id"]
    )
    assert selected["choice_id"] not in [
        item["choice_id"] for item in result["suggestion_options"]
    ]




@pytest.mark.asyncio
async def test_idle_response_focus_reaches_same_node_actor_without_committing_fact(
    monkeypatch, tmp_path
):
    """普通纵向追问只增加公开回应和场景笔记，不能借焦点提交剧情事实。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="糖糖", story_id=THEATER_TEST_STORY_ID
    )
    before = await session_store.load_session(root, started["session_id"])
    before_node_id = before["story_state"]["current_node_id"]
    before_facts = list(before["story_state"]["narrative_facts"])
    message = "为什么测试牌必须放在双方都能看见的位置？"
    focus = {
        "focus_type": "question",
        "evidence_excerpt": "为什么测试牌必须放在双方都能看见的位置",
        "requires_state_change": False,
    }

    async def _fake_route(**_kwargs):
        """普通追问不命中作者边，只返回有玩家原话证据的回应焦点。"""  # noqa: DOCSTRING_CJK
        return {
            "route_kind": "idle",
            "matched_choice_id": "",
            "authored_intent_id": "",
            "free_intent": {},
            "residual_intent": {},
            "response_focus": focus,
        }

    async def _fake_performance(**kwargs):
        """同节点 Actor 必须接收焦点，但没有任何事实提交接口。"""  # noqa: DOCSTRING_CJK
        assert kwargs["progress_kind"] == "roleplay_response"
        assert kwargs["response_focus"] == focus
        return {
            "narration": "",
            "dialogue": "因为公开可见的测试牌才能让双方确认同一个结果。",
            "choice_rewrites": [],
        }

    monkeypatch.setattr("services.theater.llm.route_free_input_async", _fake_route)
    monkeypatch.setattr("services.theater.llm.generate_turn_async", _fake_performance)
    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message=message,
        client_turn_id="turn_vertical_drilling_token_question",
        base_revision=0,
    )

    assert result["scenario_trace"]["progress_kind"] == "roleplay_response"
    assert "双方确认" in result["dialogue"]["text"]
    saved = await session_store.load_session(root, started["session_id"])
    assert saved["story_state"]["current_node_id"] == before_node_id
    assert saved["story_state"]["narrative_facts"] == before_facts
    assert saved["story_state"]["scene_notes"][-1] == message






















@pytest.mark.asyncio
async def test_natural_language_match_regenerates_performance_from_target_node(
    monkeypatch, tmp_path
):
    """自然语言命中后必须立刻演目标节点，不能把旧节点台词延迟显示一轮。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="糖糖", story_id=THEATER_TEST_STORY_ID
    )
    first_choice = next(
        item
        for item in started["suggestion_options"]
        if item["choice_mode"] == "dialogue"
    )
    routed_calls = []

    async def _fake_performance(**kwargs):
        """记录演绎调用，验证旧节点不再生成一份会被丢弃的台词。"""  # noqa: DOCSTRING_CJK
        if kwargs["node"]["node_id"] == THEATER_TEST_EXCHANGE_NODE_ID:
            routed_calls.append(
                (
                    kwargs["node"]["node_id"],
                    kwargs["progress_kind"],
                    kwargs["choice_options"],
                )
            )
            return {
                "narration": kwargs["callback"],
                "dialogue": "公开交换已经完成，我们可以继续记录结果。",
                "choice_rewrites": [],
            }
        return {
            "narration": kwargs["callback"],
            "dialogue": "测试牌的编号已经确认，可以继续下一步。",
            "choice_rewrites": [],
        }

    async def _fake_route(**_kwargs):
        """把复合自然表达映射到当前稳定 Choice。"""  # noqa: DOCSTRING_CJK
        return {
            "matched_choice_id": "choice_complete_public_exchange",
            "observed_intent_id": "",
        }

    monkeypatch.setattr("services.theater.llm.generate_turn_async", _fake_performance)
    monkeypatch.setattr("services.theater.llm.route_free_input_async", _fake_route)
    progressed = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="choice",
        choice_id=first_choice["choice_id"],
        client_turn_id="turn_confirm_test_plan",
        base_revision=0,
    )
    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="完成公开交换并继续记录",
        client_turn_id="turn_exchange_by_natural_language",
        base_revision=progressed["state_revision"],
    )

    assert [(node_id, kind) for node_id, kind, _options in routed_calls] == [
        (THEATER_TEST_EXCHANGE_NODE_ID, "graph_progress"),
    ]
    assert [item["choice_id"] for item in routed_calls[0][2]] == [
        "choice_finish_contract_story",
    ]
    story = await story_loader.load_story_exact(THEATER_TEST_STORY_ID)
    target = story_graph.node_by_id(story, THEATER_TEST_EXCHANGE_NODE_ID)
    assert result["dialogue"]["text"] == target["scripted_dialogue"]
    assert result["scenario_trace"]["progress_kind"] == "graph_progress"
    assert result["suggestion_options"][0]["choice_id"] == "choice_finish_contract_story"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["完成公开交换", "完成公开交换。", "完成，公开交换", "完成公开交换！"],
)
async def test_authored_completion_advances_before_old_node_generation(
    monkeypatch, tmp_path, message
):
    """作者声明的完成表达必须直接演目标节点，不能先生成一次旧邀请。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="糖糖", story_id=THEATER_TEST_STORY_ID
    )
    first_choice = next(
        item
        for item in started["suggestion_options"]
        if item["choice_mode"] == "dialogue"
    )
    calls = []

    async def _fake_performance(**kwargs):
        """记录实际演绎节点，确保确定性路由没有先请求旧节点。"""  # noqa: DOCSTRING_CJK
        calls.append((kwargs["node"]["node_id"], kwargs["progress_kind"]))
        return {
            "narration": kwargs["callback"],
            "dialogue": "公开交换已经完成，可以写入记录板。",
            "choice_rewrites": [],
            "matched_choice_id": "",
        }

    monkeypatch.setattr("services.theater.llm.generate_turn_async", _fake_performance)
    progressed = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="choice",
        choice_id=first_choice["choice_id"],
        client_turn_id="turn_confirm_plan_before_authored_completion",
        base_revision=0,
    )
    calls.clear()

    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message=message,
        client_turn_id=f"turn_authored_exchange_{message}",
        base_revision=progressed["state_revision"],
    )

    assert calls == [(THEATER_TEST_EXCHANGE_NODE_ID, "graph_progress")]
    assert result["scenario_trace"] == {
        "progress_kind": "graph_progress",
        "action_label": message,
    }
    assert result["suggestion_options"][0]["choice_id"] == "choice_finish_contract_story"
    saved = await session_store.load_session(root, started["session_id"])
    assert saved["story_state"]["current_node_id"] == THEATER_TEST_EXCHANGE_NODE_ID
    assert message not in saved["story_state"]["scene_notes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["先别完成公开交换", "为什么完成公开交换"])
async def test_authored_completion_does_not_consume_negation_or_question(
    monkeypatch, tmp_path, message
):
    """含否定或疑问的长句不是作者完成表达，必须保留为当前节点互动。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="糖糖", story_id=THEATER_TEST_STORY_ID
    )
    first_choice = next(
        item
        for item in started["suggestion_options"]
        if item["choice_mode"] == "dialogue"
    )
    calls = []

    async def _fake_performance(**kwargs):
        """明确返回未命中，隔离确定性匹配与模型自由路由。"""  # noqa: DOCSTRING_CJK
        calls.append((kwargs["node"]["node_id"], kwargs["progress_kind"]))
        return {
            "narration": kwargs["callback"]
            if kwargs["progress_kind"] == "graph_progress"
            else "",
            "dialogue": "糖糖先回答你眼前的问题喵。",
            "choice_rewrites": [],
            "matched_choice_id": "",
        }

    monkeypatch.setattr("services.theater.llm.generate_turn_async", _fake_performance)
    progressed = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="choice",
        choice_id=first_choice["choice_id"],
        client_turn_id="turn_confirm_plan_before_negated_completion",
        base_revision=0,
    )
    calls.clear()

    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message=message,
        client_turn_id=f"turn_hold_exchange_{message}",
        base_revision=progressed["state_revision"],
    )

    assert calls == [(THEATER_TEST_ANCHOR_NODE_ID, "roleplay_response")]
    assert result["scenario_trace"]["progress_kind"] == "roleplay_response"
    saved = await session_store.load_session(root, started["session_id"])
    assert saved["story_state"]["current_node_id"] == THEATER_TEST_ANCHOR_NODE_ID
    assert saved["story_state"]["scene_notes"][-1] == message


@pytest.mark.asyncio
async def test_free_input_without_valid_model_match_stays_on_current_node(tmp_path):
    """模型不可用时即使玩家复述按钮也不得由服务端猜测推进。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="测试猫娘", story_id=THEATER_TEST_STORY_ID
    )
    before = await session_store.load_session(root, started["session_id"])

    result = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message=started["suggestion_options"][0]["label"],
        client_turn_id="turn_model_unavailable_hold",
        base_revision=0,
    )

    after = await session_store.load_session(root, started["session_id"])
    assert result["scenario_trace"]["progress_kind"] == "roleplay_response"
    assert (
        after["story_state"]["current_node_id"]
        == before["story_state"]["current_node_id"]
    )


@pytest.mark.asyncio
async def test_natural_language_and_click_commit_same_author_state(
    monkeypatch, tmp_path
):
    """自然语言只是 Choice 的第二入口，提交后的作者权威状态必须与点击完全一致。"""  # noqa: DOCSTRING_CJK
    click_root = tmp_path / "click" / "theater"
    natural_root = tmp_path / "natural" / "theater"
    clicked_start = await runtime.start_session(
        click_root,
        lanlan_name="测试猫娘",
        story_id=THEATER_TEST_STORY_ID,
    )
    natural_start = await runtime.start_session(
        natural_root,
        lanlan_name="测试猫娘",
        story_id=THEATER_TEST_STORY_ID,
    )
    selected = clicked_start["suggestion_options"][0]

    async def _fake_performance(**kwargs):
        """点击和自然语言都只演绎已经由服务端提交的目标节点。"""  # noqa: DOCSTRING_CJK
        return {
            "narration": kwargs.get("callback") or "作者回调会在自然语言命中后补入。",
            "dialogue": "这一步由你决定喵。",
            "choice_rewrites": [],
        }

    async def _fake_route(**kwargs):
        """自然语言入口选择与点击相同的稳定 Choice。"""  # noqa: DOCSTRING_CJK
        return {
            "matched_choice_id": kwargs["choice_options"][0]["choice_id"],
            "observed_intent_id": "",
        }

    monkeypatch.setattr("services.theater.llm.generate_turn_async", _fake_performance)
    monkeypatch.setattr("services.theater.llm.route_free_input_async", _fake_route)
    await runtime.submit_input(
        click_root,
        session_id=clicked_start["session_id"],
        input_kind="choice",
        choice_id=selected["choice_id"],
        client_turn_id="turn_click_choice",
        base_revision=0,
    )
    await runtime.submit_input(
        natural_root,
        session_id=natural_start["session_id"],
        input_kind="free_input",
        message=selected["label"],
        client_turn_id="turn_natural_choice",
        base_revision=0,
    )

    clicked = await session_store.load_session(click_root, clicked_start["session_id"])
    natural = await session_store.load_session(
        natural_root, natural_start["session_id"]
    )
    assert natural["story_state"] == clicked["story_state"]


@pytest.mark.asyncio
async def test_free_dialogue_cannot_rewrite_author_choice_label(monkeypatch, tmp_path):
    """模型即使返回 Choice 改写，玩家仍只能看到并点击作者原文。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    started = await runtime.start_session(
        root, lanlan_name="测试猫娘", story_id=THEATER_TEST_STORY_ID
    )

    async def _fake_performance(**kwargs):
        """用当前约会剧情复现“自由追问后按钮应承接”的更新链。"""  # noqa: DOCSTRING_CJK
        if kwargs["progress_kind"] == "roleplay_response":
            current = kwargs["choice_options"][0]
            return {
                "narration": "她把测试记录折到只剩当前步骤的一面。",
                "dialogue": "真正想说的话，我希望你留到看着我的时候再决定喵。",
                "matched_choice_id": "",
                "choice_rewrites": [
                    {
                        "choice_id": current["choice_id"],
                        # 上下文化完整保留作者表达，只在同一对白内追加“保留真心话”的当前语境。
                        "label": "“好，正好我也打算出门，这就出发吧。真正想说的话留到你面前。”",
                    }
                ],
            }
        return {
            "narration": "清单只留下路线，背面的答案被重新折起。",
            "dialogue": "好，那我等你亲口告诉我喵。",
            "choice_rewrites": [],
        }

    monkeypatch.setattr("services.theater.llm.generate_turn_async", _fake_performance)
    first_choice = next(
        item
        for item in started["suggestion_options"]
        if item["choice_mode"] == "action"
    )
    recognized = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="choice",
        choice_id=first_choice["choice_id"],
        client_turn_id="turn_keep_handmade_charm",
        base_revision=0,
    )
    static_choice_id = recognized["suggestion_options"][0]["choice_id"]
    authored_options = recognized["suggestion_options"]

    roleplay = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="free_input",
        message="那你希望我今天哪件事不要提前计划？",
        client_turn_id="turn_ask_what_to_leave_open",
        base_revision=1,
    )
    assert roleplay["suggestion_options"] == authored_options

    progressed = await runtime.submit_input(
        root,
        session_id=started["session_id"],
        input_kind="choice",
        choice_id=static_choice_id,
        client_turn_id="turn_leave_words_unplanned",
        base_revision=2,
    )
    assert progressed["suggestion_options"][0]["choice_id"] != static_choice_id
    saved = await session_store.load_session(root, started["session_id"])
    story = await story_loader.load_story_exact(THEATER_TEST_STORY_ID)
    authored_node = story_graph.node_by_id(
        story, saved["story_state"]["current_node_id"]
    )
    assert progressed["dialogue"]["text"] == authored_node["scripted_dialogue"]
    assert "choice_label_overrides" not in saved["story_state"]


@pytest.mark.asyncio
async def test_story_graph_ignores_legacy_choice_label_override():
    """旧 Session 即使残留模型覆盖，也必须投影当前 Story 的作者 Choice 原文。"""  # noqa: DOCSTRING_CJK
    story = await story_loader.load_story_exact(THEATER_TEST_STORY_ID)
    state = rules.initial_state(
        story, initial_node_id=story_loader.initial_node_id(story)
    )
    rules.apply_node(story, state, story_graph.current_node(story, state))
    authored = story_graph.suggestion_options(story, state, lanlan_name="霜瞳")
    state["choice_label_overrides"] = {authored[0]["choice_id"]: "模型试图替换作者按钮"}

    assert story_graph.suggestion_options(story, state, lanlan_name="霜瞳") == authored


@pytest.mark.asyncio
async def test_legacy_session_returns_upgrade_result_without_discarding_active_index(
    tmp_path,
):
    """旧 Session 不得误读，但恢复接口必须明确提示升级且保留原索引。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    session_id = "theater_00000000-0000-0000-0000-000000000001"
    path = session_store.session_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 模拟瘦身前存档：保留合法 ID，但故意没有 schema_version。
    path.write_text(
        json.dumps({"session_id": session_id, "lanlan_name": "旧猫娘"}),
        encoding="utf-8",
    )
    await session_store.save_active_sessions(root, {"旧猫娘": session_id})
    session_store.reset_active_sessions_for_tests()

    restored = await runtime.get_active_state(root, lanlan_name="旧猫娘")
    assert restored == {
        "ok": False,
        "reason": "session_upgrade_required",
        "session_id": session_id,
    }
    assert await session_store.load_active_sessions(root) == {"旧猫娘": session_id}
    assert await session_store.load_session(root, session_id) is None

    # 普通开场请求不能把旧恢复入口静默覆盖；只有玩家看到提示后的显式替换才允许新开场。
    blocked_start = await runtime.start_session(
        root,
        lanlan_name="旧猫娘",
        client_start_id="start_without_consent",
    )
    assert blocked_start == {
        "ok": False,
        "reason": "session_upgrade_required",
        "session_id": session_id,
    }
    assert await session_store.load_active_sessions(root) == {"旧猫娘": session_id}

    replacement = await runtime.start_session(
        root,
        lanlan_name="旧猫娘",
        client_start_id="start_after_consent",
        replace_incompatible_session=True,
    )
    assert replacement["ok"] is True
    assert replacement["session_id"] != session_id
    assert session_store.session_path(root, session_id).is_file()
    assert await session_store.load_active_sessions(root) == {
        "旧猫娘": replacement["session_id"]
    }

    # 角色切换再次遇到旧索引时也只清理索引，不尝试迁移未知私有状态。
    await session_store.save_active_sessions(root, {"旧猫娘": session_id})
    session_store.reset_active_sessions_for_tests()
    cleared = await runtime.clear_character_session(root, lanlan_name="旧猫娘")
    assert cleared == {"ok": True, "cleared": True, "session_id": session_id}
    assert await session_store.load_active_sessions(root) == {}


@pytest.mark.asyncio
async def test_framework_story_completes_through_structured_runtime(tmp_path):
    """中性测试 Story 通过真实 Runtime 连续推进后必须正式落幕。"""  # noqa: DOCSTRING_CJK
    root = tmp_path / "theater"
    result = await runtime.start_session(
        root, lanlan_name="测试猫娘", story_id=THEATER_TEST_STORY_ID
    )
    path = [
        "choice_confirm_test_token",
        "choice_complete_public_exchange",
        "choice_finish_contract_story",
    ]
    for revision, choice_id in enumerate(path):
        assert choice_id in {
            option["choice_id"] for option in result["suggestion_options"]
        }
        result = await runtime.submit_input(
            root,
            session_id=result["session_id"],
            input_kind="choice",
            choice_id=choice_id,
            client_turn_id=f"turn_framework_contract_{revision}",
            base_revision=revision,
        )
        assert result["ok"] is True
    assert result["state_revision"] == len(path)
    assert result["ending"]["ending_id"] == "ending_contract_complete"
    assert result["can_resume"] is False
    assert result["phase"] == "ending"
    completed_session = await session_store.load_session(root, result["session_id"])
    assert completed_session["end_reason"] == "story_complete"
