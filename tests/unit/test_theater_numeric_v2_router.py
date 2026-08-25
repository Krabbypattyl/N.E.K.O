"""验证 Numeric v2 HTTP 纵向链路和失败不提交边界。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from main_routers import numeric_theater_router
from memory.recent import CompressedRecentHistoryManager
from services.theater.numeric_v2_actor import NumericV2ActorError
from services.theater.numeric_v2_archive import (
    NumericV2ArchiveStore,
    build_numeric_v2_memory_messages,
)
from services.theater.numeric_v2_evaluator import NumericV2EvaluationResult
from services.theater.numeric_v2_registry import NumericV2PackageError
from tests.unit.test_theater_numeric_v2_contract import numeric_v2_story
from utils.cloudsave_runtime import MaintenanceModeError
from utils.llm_client import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    THEATER_MEMORY_SOURCE,
    convert_to_messages,
    messages_to_dict,
)
from utils.llm_client.history import SQLChatMessageHistory
from utils.llm_client.messages import _normalize_messages


def test_numeric_v2_router_idle_request_locks_are_reclaimed():
    """请求锁只覆盖并发执行窗口，完成后不能随幂等请求 ID 无限增长。"""  # noqa: DOCSTRING_CJK

    request_id = "router_lock_reclaim_test"
    lock = numeric_theater_router._request_lock(
        numeric_theater_router._speak_request_locks,
        request_id,
    )
    assert numeric_theater_router._request_lock(
        numeric_theater_router._speak_request_locks,
        request_id,
    ) is lock

    # 调用方不再持有锁时，弱引用表应自行删除空闲请求条目。
    del lock
    gc.collect()
    assert request_id not in numeric_theater_router._speak_request_locks


def test_llm_role_dict_normalization_strips_internal_metadata():
    """角色字典发送到模型供应商前必须剥离 N.E.K.O 内部元数据。"""  # noqa: DOCSTRING_CJK
    normalized = _normalize_messages([{
        "role": "user",
        "content": "继续。",
        "metadata": {"source": THEATER_MEMORY_SOURCE},
    }])

    assert normalized == [{"role": "user", "content": "继续。"}]


@pytest.mark.asyncio
async def test_numeric_v2_router_uses_cloudsave_write_fence(tmp_path, monkeypatch):
    """剧场写操作必须在云存档维护态命中共享写栅栏。"""  # noqa: DOCSTRING_CJK
    calls = []

    def _blocked(_config_manager, *, operation: str, target: str):
        calls.append((operation, target))
        raise MaintenanceModeError(
            "applying_snapshot",
            operation=operation,
            target=target,
        )

    monkeypatch.setattr(numeric_theater_router, "assert_cloudsave_writable", _blocked)

    with pytest.raises(MaintenanceModeError):
        await numeric_theater_router._assert_numeric_writable(
            _ConfigManager(tmp_path),
            "sessions",
        )

    assert calls == [("save", "theater/numeric_v2/sessions")]


def test_numeric_v2_story_list_reuses_compiled_summary_intro():
    """列表投影只能消费注册表已经编译的摘要，不能为显示简介再次加载整包。"""  # noqa: DOCSTRING_CJK

    class _SummaryOnlyRegistry:
        def list_packages(self):
            # 故意不提供 load_engine；若列表实现退回二次加载，本测试会直接失败。
            return [{
                "story_id": "summary_only_story",
                "title": "摘要剧本",
                "intro": {
                    "background": "林舟在门口遇见小岚。",
                    "player_identity": "林舟，刚到这里的男主。",
                    "catgirl_identity": "小岚，守在门口的猫娘。",
                },
            }]

    stories = numeric_theater_router._list_story_summaries(
        _SummaryOnlyRegistry(),
        {"catgirl_name": "测试猫娘", "player_address": "哥哥"},
    )

    assert stories[0]["display_intro"]["background"] == "你在门口遇见测试猫娘。"


class _ConfigManager:
    def __init__(self, root: Path):
        self.app_docs_dir = root
        self.config_dir = root / "config"

    def load_characters(self) -> dict:
        return {
            "当前猫娘": "测试猫娘",
            "猫娘": {"测试猫娘": _catgirl_profile("测试猫娘", "安静而认真。")},
            "主人": {"昵称": "哥哥"},
        }

    def load_root_state(self) -> dict:
        # 路由测试默认处于可写态；维护态行为由云存档栅栏测试单独覆盖。
        return {"mode": "normal"}


def _catgirl_profile(name: str, personality: str) -> dict:
    token = "2" if name == "新猫娘" else "1"
    return {
        "昵称": name,
        "人格": personality,
        "_reserved": {
            "character_id": f"character_{token * 32}",
        },
    }


def _performance(text: str, *, opening: bool = False) -> dict:
    if opening:
        return {
            "scene_narration": "风铃轻轻响了一声。",
            "performance": text,
            "suggested_inputs": ["继续听她说"],
        }
    return {
        "performance": f"（风铃轻轻响了一声）{text}",
        "suggested_inputs": ["继续听她说"],
    }


def _client(
    tmp_path: Path,
    monkeypatch,
    config_manager=None,
    *,
    opening_text="你回来了。",
    player_address_known=True,
) -> TestClient:
    packages = tmp_path / "theater" / "numeric_v2" / "packages"
    packages.mkdir(parents=True)
    (packages / "numeric_v2_contract.json").write_text(
        json.dumps(
            numeric_v2_story(player_address_known=player_address_known),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manager = config_manager or _ConfigManager(tmp_path)
    monkeypatch.setattr(numeric_theater_router, "get_config_manager", lambda: manager)
    monkeypatch.setattr(numeric_theater_router, "_validate_local_mutation_request", lambda *args, **kwargs: None)

    async def opening(*args, **kwargs):
        return _performance(opening_text, opening=True)

    async def turn(*args, **kwargs):
        return _performance("我在听。")

    async def evaluate(*args, **kwargs):
        return NumericV2EvaluationResult(metric_changes=(), scene_complete=False)

    monkeypatch.setattr(numeric_theater_router.NumericV2Actor, "generate_opening", opening)
    monkeypatch.setattr(numeric_theater_router.NumericV2Actor, "generate_turn", turn)
    monkeypatch.setattr(numeric_theater_router.NumericV2MetricEvaluator, "evaluate", evaluate)
    app = FastAPI()
    app.include_router(numeric_theater_router.router)
    return TestClient(app)


def test_numeric_v2_router_projects_unknown_player_as_second_person(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, player_address_known=False)
    with client:
        listed = client.get("/api/theater-numeric/stories")
        assert listed.status_code == 200
        assert listed.json()["stories"][0]["display_intro"]["player_identity"].startswith("你，")

        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "unknown_address"},
        )
        body = started.json()
        assert started.status_code == 200
        assert body["story_intro"]["player_identity"].startswith("你，")
        assert body["participants"]["player_name"] == "你"


def test_numeric_v2_router_starts_restores_and_submits_free_input(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        listed = client.get("/api/theater-numeric/stories")
        assert listed.status_code == 200
        assert listed.json()["stories"][0]["story_id"] == "numeric_v2_contract"
        assert listed.json()["stories"][0]["display_intro"]["player_identity"].startswith("你，")

        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "http_v2"},
        )
        body = started.json()
        assert started.status_code == 200
        assert body["session"]["schema"] == "neko.script.session.numeric.v2"
        assert body["session"]["opening_performance"]["performance"] == "你回来了。"
        assert "metrics" not in body["session"]
        assert body["scene"]["min_turns"] == 2
        assert "recommended_turns" not in body["scene"]
        assert body["story_intro"]["player_identity"].startswith("哥哥，")
        assert body["story_intro"]["catgirl_identity"].startswith("测试猫娘，")
        assert body["participants"] == {
            "player_name": "哥哥",
            "catgirl_name": "测试猫娘",
        }
        assert "林舟" not in json.dumps(body, ensure_ascii=False)
        assert "小岚" not in json.dumps(body, ensure_ascii=False)

        resumed_start = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "http_v2_duplicate"},
        )
        assert resumed_start.status_code == 200
        assert resumed_start.json()["resumed"] is True
        assert resumed_start.json()["session"]["session_id"] == "http_v2"

        active = client.get(
            "/api/theater-numeric/session/active",
            params={"story_id": "numeric_v2_contract"},
        )
        assert active.status_code == 200
        assert active.json()["session"]["session_id"] == "http_v2"
        assert len(list((tmp_path / "theater" / "numeric_v2" / "sessions").glob("*.json"))) == 1

        old_turn = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "http_v2",
                "client_turn_id": "old_turn_1",
                "base_revision": 0,
                "message": "这是旧会话的记录。",
            },
        )
        assert old_turn.status_code == 200

        ended = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "http_v2",
                "base_revision": 1,
            },
        )
        assert ended.status_code == 200
        assert ended.json()["session"]["status"] == "ended"
        assert ended.json()["end_receipt_id"].startswith("theater_end_")

        async def regenerated_opening(*args, **kwargs):
            return _performance("这是重新生成的新开场。", opening=True)

        monkeypatch.setattr(
            numeric_theater_router.NumericV2Actor,
            "generate_opening",
            regenerated_opening,
        )

        restarted = client.post(
            "/api/theater-numeric/session/start",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "http_v2_after_restart",
                "replace_existing": True,
            },
        )
        assert restarted.status_code == 200
        assert restarted.json()["session"]["session_id"] == "http_v2_after_restart"
        assert restarted.json()["session"]["status"] == "active"
        assert restarted.json()["session"]["revision"] == 0
        assert restarted.json()["session"]["performance_history"] == []
        assert restarted.json()["session"]["opening_performance"]["performance"] == "这是重新生成的新开场。"
        assert len(list((tmp_path / "theater" / "numeric_v2" / "sessions").glob("*.json"))) == 1

        ended_history = client.get(
            "/api/theater-numeric/session/http_v2",
            params={"story_id": "numeric_v2_contract"},
        )
        assert ended_history.status_code == 404
        assert ended_history.json()["reason"] == "numeric_session_not_found"

        submitted = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "http_v2_after_restart",
                "client_turn_id": "http_turn_1",
                "base_revision": 0,
                "message": "我先听你说。",
            },
        )
        result = submitted.json()
        assert submitted.status_code == 200
        assert result["resolved_turn"] == {"route_status": "waiting_min_turns", "route_changed": False}
        assert result["session"]["performance_history"][0]["input_text"] == "我先听你说。"
        assert result["suggested_inputs"] == ["继续听她说"]

        restored = client.get(
            "/api/theater-numeric/session/http_v2_after_restart",
            params={"story_id": "numeric_v2_contract"},
        )
        assert restored.status_code == 200
        assert restored.json()["session"]["revision"] == 1


def test_numeric_v2_user_exit_can_resume_same_session(tmp_path, monkeypatch):
    """主动退出只离开演绎界面，继续时必须恢复原 Session、revision 和历史。"""  # noqa: DOCSTRING_CJK

    client = _client(tmp_path, monkeypatch)
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "resumable_exit"},
        )
        assert started.status_code == 200
        submitted = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "resumable_exit",
                "client_turn_id": "before_exit",
                "base_revision": 0,
                "message": "我先把信收好。",
            },
        )
        assert submitted.status_code == 200
        exited = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "resumable_exit",
                "base_revision": 1,
            },
        )
        assert exited.status_code == 200
        assert exited.json()["session"]["status"] == "ended"
        assert exited.json()["session"]["ended_reason"] == "user_exit"

        resumed = client.post(
            "/api/theater-numeric/session/resume",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "resumable_exit",
                "base_revision": 1,
            },
        )
        assert resumed.status_code == 200
        resumed_session = resumed.json()["session"]
        assert resumed_session["session_id"] == "resumable_exit"
        assert resumed_session["status"] == "active"
        assert resumed_session["ended_reason"] is None
        assert resumed_session["revision"] == 1
        assert resumed_session["performance_history"][0]["input_text"] == "我先把信收好。"

        continued = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "resumable_exit",
                "client_turn_id": "after_resume",
                "base_revision": 1,
                "message": "我们接着刚才的话说。",
            },
        )
        assert continued.status_code == 200
        assert continued.json()["session"]["revision"] == 2


def test_numeric_v2_ended_retries_rebuild_missing_receipt(tmp_path, monkeypatch):
    """结束状态已提交后，结束重试和回合幂等重放都应补建缺失回执。"""  # noqa: DOCSTRING_CJK
    client = _client(tmp_path, monkeypatch)
    turn_payload = {
        "story_id": "numeric_v2_contract",
        "session_id": "receipt_retry_session",
        "client_turn_id": "receipt_retry_turn",
        "base_revision": 0,
        "message": "先把这句话说完。",
    }
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "receipt_retry_session",
            },
        )
        assert started.status_code == 200
        assert client.post(
            "/api/theater-numeric/session/input",
            json=turn_payload,
        ).status_code == 200
        ended = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "receipt_retry_session",
                "base_revision": 1,
            },
        )
        assert ended.status_code == 200

        receipt_root = tmp_path / "theater" / "numeric_v2" / "end_receipts"
        for path in receipt_root.glob("*.json"):
            path.unlink()
        retried_end = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "receipt_retry_session",
                "base_revision": 1,
            },
        )
        assert retried_end.status_code == 200
        assert retried_end.json()["idempotent_replay"] is True
        assert retried_end.json()["end_receipt_id"].startswith("theater_end_")

        for path in receipt_root.glob("*.json"):
            path.unlink()
        replayed_turn = client.post(
            "/api/theater-numeric/session/input",
            json=turn_payload,
        )

    assert replayed_turn.status_code == 200
    assert replayed_turn.json()["idempotent_replay"] is True
    assert replayed_turn.json()["end_receipt_id"].startswith("theater_end_")


def test_numeric_v2_actor_failure_does_not_commit_half_turn(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "http_actor_fail"},
        )

        async def fail_actor(*args, **kwargs):
            raise NumericV2ActorError("test_actor_failure")

        monkeypatch.setattr(numeric_theater_router.NumericV2Actor, "generate_turn", fail_actor)
        failed = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "http_actor_fail",
                "client_turn_id": "failed_turn",
                "base_revision": 0,
                "message": "这一轮不能留下半回合。",
            },
        )
        assert failed.status_code == 502

        restored = client.get(
            "/api/theater-numeric/session/http_actor_fail",
            params={"story_id": "numeric_v2_contract"},
        )
        assert restored.json()["session"]["revision"] == 0
        assert restored.json()["session"]["performance_history"] == []


def test_numeric_v2_router_delete_story_reports_active_catgirls_and_cascades_sessions(
    tmp_path,
    monkeypatch,
):
    class _MutableConfigManager(_ConfigManager):
        def __init__(self, root: Path):
            super().__init__(root)
            self.current_name = "测试猫娘"

        def load_characters(self) -> dict:
            return {
                "当前猫娘": self.current_name,
                "猫娘": {
                    "测试猫娘": _catgirl_profile("测试猫娘", "安静而认真。"),
                    "新猫娘": _catgirl_profile("新猫娘", "活泼而坦率。"),
                },
                "主人": {"昵称": "哥哥"},
            }

    manager = _MutableConfigManager(tmp_path)
    client = _client(tmp_path, monkeypatch, config_manager=manager)
    with client:
        first = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "delete_story_first"},
        )
        assert first.status_code == 200
        manager.current_name = "新猫娘"
        second = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "delete_story_second"},
        )
        assert second.status_code == 200
        ended = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "delete_story_second",
                "base_revision": 0,
            },
        )
        assert ended.status_code == 200

        preview = client.get(
            "/api/theater-numeric/packages/numeric_v2_contract/delete-preview"
        )
        assert preview.status_code == 200
        assert preview.json()["active_catgirl_names"] == ["测试猫娘"]
        assert preview.json()["session_count"] == 2

        deleted = client.delete(
            "/api/theater-numeric/packages/numeric_v2_contract"
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted_session_count"] == 2
        assert not list(
            (tmp_path / "theater" / "numeric_v2" / "sessions").glob("*.json")
        )
        assert not (
            tmp_path
            / "theater"
            / "numeric_v2"
            / "packages"
            / "numeric_v2_contract.json"
        ).exists()
        assert client.get("/api/theater-numeric/stories").json()["stories"] == []


def test_numeric_v2_story_delete_rolls_back_package_sessions_and_index(
    tmp_path,
    monkeypatch,
):
    client = _client(tmp_path, monkeypatch)
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "delete_rollback"},
        )
        assert started.status_code == 200
        package_path = (
            tmp_path
            / "theater"
            / "numeric_v2"
            / "packages"
            / "numeric_v2_contract.json"
        )
        session_path = (
            tmp_path
            / "theater"
            / "numeric_v2"
            / "sessions"
            / "delete_rollback.json"
        )
        index_path = tmp_path / "theater" / "numeric_v2" / "story_sessions.json"
        original_index = index_path.read_bytes()
        original_delete = numeric_theater_router.NumericV2PackageRegistry.delete_package

        def delete_then_fail(registry, story_id):
            original_delete(registry, story_id)
            raise NumericV2PackageError("forced_delete_failure")

        monkeypatch.setattr(
            numeric_theater_router.NumericV2PackageRegistry,
            "delete_package",
            delete_then_fail,
        )
        failed = client.delete(
            "/api/theater-numeric/packages/numeric_v2_contract"
        )

        assert failed.status_code == 422
        assert package_path.is_file()
        assert session_path.is_file()
        assert index_path.read_bytes() == original_index
        transaction_root = (
            tmp_path / "theater" / "numeric_v2" / "delete_transactions"
        )
        assert not list(transaction_root.glob("*"))


def test_numeric_v2_router_rejects_stale_or_ended_turn_before_evaluator(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Evaluator must not run for a stale or ended session")

    monkeypatch.setattr(numeric_theater_router.NumericV2MetricEvaluator, "evaluate", fail_if_called)
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "router_precheck"},
        )
        assert started.status_code == 200

        stale = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "router_precheck",
                "client_turn_id": "stale_turn",
                "base_revision": 1,
                "message": "旧状态不能调用模型。",
            },
        )
        assert stale.status_code == 409
        assert stale.json()["reason"] == "numeric_base_revision_mismatch"

        ended = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "router_precheck",
                "base_revision": 0,
            },
        )
        assert ended.status_code == 200

        after_end = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "router_precheck",
                "client_turn_id": "after_end",
                "base_revision": 0,
                "message": "已结束状态不能调用模型。",
            },
        )
        assert after_end.status_code == 409
        assert after_end.json()["reason"] == "session_already_ended"


def test_numeric_v2_router_rechecks_catgirl_before_commit(tmp_path, monkeypatch):
    class _MutableConfigManager(_ConfigManager):
        def __init__(self, root: Path):
            super().__init__(root)
            self.current_name = "测试猫娘"

        def load_characters(self) -> dict:
            return {
                "当前猫娘": self.current_name,
                "猫娘": {self.current_name: _catgirl_profile(self.current_name, "测试人格")},
                "主人": {"昵称": "哥哥"},
            }

    manager = _MutableConfigManager(tmp_path)
    client = _client(tmp_path, monkeypatch, config_manager=manager)
    events = []
    original_check = numeric_theater_router._ensure_current_catgirl

    def record_check(session, config_manager):
        events.append("check")
        return original_check(session, config_manager)

    async def change_during_actor(*args, **kwargs):
        events.append("actor")
        manager.current_name = "新猫娘"
        events.append("changed")
        return _performance("这一轮不应提交。")

    monkeypatch.setattr(numeric_theater_router, "_ensure_current_catgirl", record_check)
    monkeypatch.setattr(numeric_theater_router.NumericV2Actor, "generate_turn", change_during_actor)
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "catgirl_commit_guard"},
        )
        assert started.status_code == 200

        blocked = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "catgirl_commit_guard",
                "client_turn_id": "catgirl_changed_during_model",
                "base_revision": 0,
                "message": "这一轮不应提交。",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["reason"] == "catgirl_changed_requires_new_session"
        assert events == ["check", "actor", "changed", "check"]

        manager.current_name = "测试猫娘"
        restored = client.get(
            "/api/theater-numeric/session/catgirl_commit_guard",
            params={"story_id": "numeric_v2_contract"},
        )
        assert restored.status_code == 200
        assert restored.json()["session"]["revision"] == 0


def test_numeric_v2_router_rechecks_catgirl_after_opening(tmp_path, monkeypatch):
    class _MutableConfigManager(_ConfigManager):
        def __init__(self, root: Path):
            super().__init__(root)
            self.current_name = "测试猫娘"

        def load_characters(self) -> dict:
            return {
                "当前猫娘": self.current_name,
                "猫娘": {self.current_name: _catgirl_profile(self.current_name, "测试人格")},
                "主人": {"昵称": "哥哥"},
            }

    manager = _MutableConfigManager(tmp_path)
    client = _client(tmp_path, monkeypatch, config_manager=manager)
    events = []

    async def change_during_opening(*args, **kwargs):
        events.append("actor")
        manager.current_name = "新猫娘"
        events.append("changed")
        return _performance("开场不应写入旧角色。", opening=True)

    monkeypatch.setattr(numeric_theater_router.NumericV2Actor, "generate_opening", change_during_opening)
    with client:
        blocked = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "catgirl_opening_guard"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["reason"] == "catgirl_changed_requires_new_session"
        assert events == ["actor", "changed"]
        assert not list((tmp_path / "theater" / "numeric_v2" / "sessions").glob("*.json"))


def test_numeric_v2_router_preserves_each_catgirls_story_session(tmp_path, monkeypatch):
    class _MutableConfigManager(_ConfigManager):
        def __init__(self, root: Path):
            super().__init__(root)
            self.current_name = "测试猫娘"

        def load_characters(self) -> dict:
            return {
                "当前猫娘": self.current_name,
                "猫娘": {
                    "测试猫娘": _catgirl_profile("测试猫娘", "安静而认真。"),
                    "新猫娘": _catgirl_profile("新猫娘", "活泼而坦率。"),
                },
                "主人": {"昵称": "哥哥"},
            }

    manager = _MutableConfigManager(tmp_path)
    client = _client(tmp_path, monkeypatch, config_manager=manager)

    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "catgirl_before_change"},
        )
        assert started.status_code == 200
        manager.current_name = "新猫娘"
        second = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "catgirl_after_change"},
        )
        assert second.status_code == 200
        assert second.json()["session"]["session_id"] == "catgirl_after_change"
        assert sorted(
            path.stem
            for path in (tmp_path / "theater" / "numeric_v2" / "sessions").glob("*.json")
        ) == ["catgirl_after_change", "catgirl_before_change"]

        stale_tab = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "catgirl_before_change",
                "client_turn_id": "stale_tab_turn",
                "base_revision": 0,
                "message": "旧页面不能推进新演绎。",
            },
        )
        assert stale_tab.status_code == 409
        assert stale_tab.json()["reason"] == "catgirl_changed_requires_new_session"

        manager.current_name = "测试猫娘"
        restored_first = client.get(
            "/api/theater-numeric/session/active",
            params={"story_id": "numeric_v2_contract"},
        )
        assert restored_first.status_code == 200
        assert restored_first.json()["session"]["session_id"] == "catgirl_before_change"

        manager.current_name = "新猫娘"
        restored_second = client.get(
            "/api/theater-numeric/session/active",
            params={"story_id": "numeric_v2_contract"},
        )
        assert restored_second.status_code == 200
        assert restored_second.json()["session"]["session_id"] == "catgirl_after_change"


def test_numeric_v2_router_rejects_reusing_id_when_same_catgirl_profile_changed(
    tmp_path,
    monkeypatch,
):
    class _MutableProfileConfigManager(_ConfigManager):
        def __init__(self, root: Path):
            super().__init__(root)
            self.personality = "安静而认真。"

        def load_characters(self) -> dict:
            return {
                "当前猫娘": "测试猫娘",
                "猫娘": {
                    "测试猫娘": _catgirl_profile("测试猫娘", self.personality)
                },
                "主人": {"昵称": "哥哥"},
            }

    manager = _MutableProfileConfigManager(tmp_path)
    client = _client(tmp_path, monkeypatch, config_manager=manager)
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "same_id_profile"},
        )
        assert started.status_code == 200
        manager.personality = "更新后更活泼。"

        replacement = client.post(
            "/api/theater-numeric/session/start",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "same_id_profile",
                "replace_existing": True,
            },
        )
        active_replacement = client.post(
            "/api/theater-numeric/session/start",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "same_id_profile_new",
                "replace_existing": True,
            },
        )

        assert replacement.status_code == 400
        assert replacement.json()["reason"] == "numeric_replacement_session_id_must_differ"
        assert active_replacement.status_code == 409
        assert active_replacement.json()["reason"] == "numeric_active_session_cannot_restart"
        assert (
            tmp_path
            / "theater"
            / "numeric_v2"
            / "sessions"
            / "same_id_profile.json"
        ).is_file()


def test_numeric_v2_restart_keeps_ended_session_when_new_opening_fails(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "restart_source"},
        )
        assert started.status_code == 200
        ended = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "restart_source",
                "base_revision": 0,
            },
        )
        assert ended.status_code == 200

        async def failed_opening(*args, **kwargs):
            raise NumericV2ActorError("numeric_v2_actor_model_call_failed")

        monkeypatch.setattr(
            numeric_theater_router.NumericV2Actor,
            "generate_opening",
            failed_opening,
        )
        restarted = client.post(
            "/api/theater-numeric/session/start",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "restart_target",
                "replace_existing": True,
            },
        )

        assert restarted.status_code == 502
        assert restarted.json()["reason"] == "numeric_v2_actor_failed"
        active = client.get(
            "/api/theater-numeric/session/active",
            params={"story_id": "numeric_v2_contract"},
        )
        assert active.status_code == 200
        assert active.json()["session"]["session_id"] == "restart_source"
        assert active.json()["session"]["status"] == "ended"
        sessions = tmp_path / "theater" / "numeric_v2" / "sessions"
        assert (sessions / "restart_source.json").is_file()
        assert not (sessions / "restart_target.json").exists()


def test_numeric_tts_merges_committed_dialogue_blocks_without_actions(tmp_path, monkeypatch):
    captured = {}

    async def capture_speech(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return {"audio_queued": True, "speech_id": "speech-1"}

    monkeypatch.setattr(numeric_theater_router, "speak_committed_line", capture_speech)
    client = _client(
        tmp_path,
        monkeypatch,
        opening_text="（她抬起眼睛）你回来了。（她让开门口）先进来吧。",
    )
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "numeric_tts_binding"},
        )
        assert started.status_code == 200

        narration = client.post(
            "/api/theater-numeric/session/speak-block",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "numeric_tts_binding",
                "revision": 0,
                "block_index": 0,
                "playback_request_id": "tts-narration",
            },
        )
        assert narration.status_code == 422
        assert narration.json()["reason"] == "numeric_speak_block_not_dialogue"

        dialogue = client.post(
            "/api/theater-numeric/session/speak-block",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "numeric_tts_binding",
                "revision": 0,
                "block_index": 2,
                "dialogue_block_indexes": [2, 4],
                "playback_request_id": "tts-dialogue",
            },
        )

    assert dialogue.status_code == 200
    assert dialogue.json()["speech_id"] == "speech-1"
    assert dialogue.json()["dialogue_block_count"] == 2
    assert captured["lanlan_name"] == "测试猫娘"
    assert captured["args"][0] == "你回来了。 先进来吧。"
    assert captured["interrupt_audio"] is True


def test_numeric_end_receipt_archives_public_performance_once(tmp_path, monkeypatch):
    captured = {"calls": 0}

    class _MemoryResponse:
        content = b'{"status":"success","count":1}'
        is_success = True

        @staticmethod
        def json():
            return {"status": "success", "count": 1}

    class _MemoryClient:
        async def post(self, url, *, json, timeout):
            captured["calls"] += 1
            captured["character_lock_held"] = (
                numeric_theater_router.character_config_mutation_lock.locked()
            )
            captured["url"] = url
            captured["payload"] = json
            captured["timeout"] = timeout
            return _MemoryResponse()

    monkeypatch.setattr(
        "utils.internal_http_client.get_internal_http_client",
        lambda: _MemoryClient(),
    )
    client = _client(tmp_path, monkeypatch)
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "archive_session"},
        )
        assert started.status_code == 200
        submitted = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "archive_session",
                "client_turn_id": "archive_turn",
                "base_revision": 0,
                "message": "我把信放在桌上。",
            },
        )
        assert submitted.status_code == 200
        ended = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "archive_session",
                "base_revision": 1,
            },
        ).json()
        receipt = {
            "story_id": "numeric_v2_contract",
            "session_id": "archive_session",
            "revision": 1,
            "end_receipt_id": ended["end_receipt_id"],
        }

        archived = client.post(
            "/api/theater-numeric/session/archive",
            json={**receipt, "archive_request_id": ended["archive_request_id"]},
        )
        replay = client.post(
            "/api/theater-numeric/session/archive",
            json={**receipt, "archive_request_id": ended["archive_request_id"]},
        )
        restored = client.get(
            "/api/theater-numeric/session/active",
            params={"story_id": "numeric_v2_contract"},
        )
        restarted = client.post(
            "/api/theater-numeric/session/start",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "archive_session_next_run",
                "replace_existing": True,
            },
        )

    assert archived.status_code == 200
    assert archived.json()["status"] == "written"
    assert replay.status_code == 200
    assert replay.json()["status"] == "already_written"
    assert restored.json()["end_receipt_id"] == receipt["end_receipt_id"]
    assert restored.json()["archive_status"] == "written"
    assert restarted.status_code == 200
    assert restarted.json()["session"]["session_id"] == "archive_session_next_run"
    assert not (
        tmp_path / "theater" / "numeric_v2" / "sessions" / "archive_session.json"
    ).exists()
    assert captured["calls"] == 1
    assert captured["character_lock_held"] is True
    assert captured["url"].endswith("/%E6%B5%8B%E8%AF%95%E7%8C%AB%E5%A8%98")
    assert captured["payload"]["idempotency_key"] == ended["archive_request_id"]
    memory_text = captured["payload"]["input_history"]
    memory_messages = json.loads(memory_text)
    assert "我在听。" in memory_text
    assert "我把信放在桌上。" not in memory_text
    assert [message["role"] for message in memory_messages] == ["system"]
    assert all(
        message["metadata"]["source"] == THEATER_MEMORY_SOURCE
        for message in memory_messages
    )
    assert all(message["metadata"]["episode_status"] == "paused" for message in memory_messages)
    assert memory_messages[0]["metadata"]["memory_tier"] == "episode_summary"
    assert memory_messages[0]["metadata"]["message_kind"] == "episode_summary"
    assert "【" not in memory_text
    assert "metrics" not in memory_text
    assert "suggested_inputs" not in memory_text
    assert "mainline_" not in memory_text

    # 单集胶囊经过统一消息转换后仍是 system，且内部来源元数据不丢失。
    persisted_messages = messages_to_dict(convert_to_messages(memory_messages))
    assert [message["type"] for message in persisted_messages] == ["system"]
    assert all(
        message["data"]["metadata"]["source"] == THEATER_MEMORY_SOURCE
        for message in persisted_messages
    )
    public_archives = list(
        (tmp_path / "theater" / "numeric_v2" / "public_archives").glob("*.json")
    )
    assert len(public_archives) == 1
    public_archive = json.loads(public_archives[0].read_text(encoding="utf-8"))
    assert public_archive["schema"] == "neko.theater.numeric.v2.public-archive"
    assert public_archive["session_id"] == "archive_session"
    assert public_archive["turns"][0]["player_input"] == "我把信放在桌上。"
    assert "我在听。" in public_archive["turns"][0]["performance"]
    assert "metrics" not in json.dumps(public_archive, ensure_ascii=False)
    assert "mainline_" not in json.dumps(public_archive, ensure_ascii=False)


def test_numeric_story_memory_can_be_pinned_and_forgotten(tmp_path, monkeypatch):
    """显式忘记应清理热记忆、冷档案与旧回执，但保留 Session。"""  # noqa: DOCSTRING_CJK

    captured = {"forgotten": False}

    class _MemoryResponse:
        content = b"{}"
        is_success = True

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class _MemoryClient:
        async def post(self, url, *, json, timeout):
            if url.endswith("/theater/forget"):
                captured["forgotten"] = True
                return _MemoryResponse({
                    "ok": True,
                    "removed_recent": 1,
                    "removed_time_index": 3,
                })
            return _MemoryResponse({"status": "cached", "count": 1})

    monkeypatch.setattr(
        "utils.internal_http_client.get_internal_http_client",
        lambda: _MemoryClient(),
    )
    client = _client(tmp_path, monkeypatch)
    with client:
        client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "forget_session"},
        )
        ended = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "forget_session",
                "base_revision": 0,
            },
        ).json()
        archived = client.post(
            "/api/theater-numeric/session/archive",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "forget_session",
                "revision": 0,
                "end_receipt_id": ended["end_receipt_id"],
                "archive_request_id": ended["archive_request_id"],
            },
        )
        listed = client.get(
            "/api/theater-numeric/memory/archives",
            params={"story_id": "numeric_v2_contract"},
        )
        pinned = client.post(
            "/api/theater-numeric/memory/archive/pin",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "forget_session",
                "pinned": True,
            },
        )
        forgotten = client.post(
            "/api/theater-numeric/memory/forget",
            json={"story_id": "numeric_v2_contract"},
        )
        after = client.get(
            "/api/theater-numeric/memory/archives",
            params={"story_id": "numeric_v2_contract"},
        )
        active = client.get(
            "/api/theater-numeric/session/active",
            params={"story_id": "numeric_v2_contract"},
        )

    assert archived.json()["status"] == "written"
    assert len(listed.json()["archives"]) == 1
    assert pinned.json()["archive"]["pinned"] is True
    assert forgotten.json() == {
        "ok": True,
        "removed_recent": 1,
        "removed_time_index": 3,
        "removed_archives": 1,
        "removed_receipts": 2,
    }
    assert captured["forgotten"] is True
    assert after.json()["archives"] == []
    assert active.json()["session"]["session_id"] == "forget_session"
    assert active.json()["archive_status"] == "skipped"


def test_numeric_story_memory_can_be_forgotten_after_package_deletion(
    tmp_path,
    monkeypatch,
):
    """剧本包删除后仍应按稳定 story_id 清理残留记忆。"""  # noqa: DOCSTRING_CJK

    captured = {}

    class _MemoryResponse:
        content = b"{}"
        is_success = True

        @staticmethod
        def json():
            return {
                "ok": True,
                "removed_recent": 1,
                "removed_time_index": 1,
            }

    class _MemoryClient:
        async def post(self, url, *, json, timeout):
            captured["url"] = url
            captured["payload"] = json
            captured["character_lock_held"] = (
                numeric_theater_router.character_config_mutation_lock.locked()
            )
            return _MemoryResponse()

    monkeypatch.setattr(
        "utils.internal_http_client.get_internal_http_client",
        lambda: _MemoryClient(),
    )
    client = _client(tmp_path, monkeypatch)
    with client:
        deleted = client.delete(
            "/api/theater-numeric/packages/numeric_v2_contract",
        )
        forgotten = client.post(
            "/api/theater-numeric/memory/forget",
            json={"story_id": "numeric_v2_contract"},
        )

    assert deleted.status_code == 200
    assert forgotten.status_code == 200
    assert forgotten.json() == {
        "ok": True,
        "removed_recent": 1,
        "removed_time_index": 1,
        "removed_archives": 0,
        "removed_receipts": 0,
    }
    assert captured["payload"] == {"story_id": "numeric_v2_contract"}
    assert captured["character_lock_held"] is True


def test_numeric_memory_projection_builds_one_compact_episode_summary():
    """完整换场留在 Session；日常记忆只接收一条有序单集摘要。"""  # noqa: DOCSTRING_CJK

    session = SimpleNamespace(
        story_package_id="numeric_v2_contract",
        session_id="memory_projection",
        revision=2,
        opening_performance={
            "scene_narration": "雨点敲在窗沿。",
            "performance": "（抬起头）你来了。",
        },
        performance_history=(
            {
                "revision": 1,
                "input_text": "把合同递给她。",
                "performance": "（接过合同）我看看。",
            },
            {
                "revision": 2,
                "input_text": "一起去找中介。",
                "segments": [
                    {
                        "phase": "source_response",
                        "performance": "（站起身）走吧。",
                    },
                    {
                        "phase": "transition_bridge",
                        "scene_narration": "雨停后，两人来到街角。",
                    },
                    {
                        "phase": "target_opening",
                        "scene_narration": "卷帘门已经落锁。",
                        "performance": "（攥紧合同）他们跑了。",
                    },
                ],
            },
        ),
    )

    messages = build_numeric_v2_memory_messages(
        title="雨夜合租",
        session=session,
        ending=None,
    )

    assert [message["role"] for message in messages] == ["system"]
    capsule = messages[0]
    transition_text = json.dumps(capsule["content"], ensure_ascii=False)
    expected_order = [
        "走吧。",
        "雨停后，两人来到街角。",
        "卷帘门已经落锁。",
        "他们跑了。",
    ]
    assert [transition_text.index(text) for text in expected_order] == sorted(
        transition_text.index(text) for text in expected_order
    )
    assert capsule["metadata"]["memory_tier"] == "episode_summary"
    assert capsule["metadata"]["episode_summary"]
    assert "把合同递给她" not in transition_text
    assert "一起去找中介" not in transition_text
    assert "【" not in json.dumps(messages, ensure_ascii=False)


def test_sql_history_serialization_preserves_internal_message_metadata():
    """时间索引必须保留来源元数据，否则虚构事实过滤会在落库后失效。"""  # noqa: DOCSTRING_CJK

    history = SQLChatMessageHistory.__new__(SQLChatMessageHistory)
    serialized = json.loads(history._serialize(HumanMessage(
        content="把合同递过去。",
        metadata={"source": THEATER_MEMORY_SOURCE, "session_id": "memory_projection"},
    )))

    assert serialized == {
        "type": "human",
        "data": {
            "content": "把合同递过去。",
            "metadata": {
                "source": THEATER_MEMORY_SOURCE,
                "session_id": "memory_projection",
            },
        },
    }
    assert HumanMessage(
        content="把合同递过去。",
        metadata={"source": THEATER_MEMORY_SOURCE},
    ).to_openai() == {
        "role": "user",
        "content": "把合同递过去。",
    }


def test_sql_history_replaces_theater_story_event_atomically(tmp_path):
    """同一稳定事件更新后只保留最新周目胶囊。"""  # noqa: DOCSTRING_CJK

    from sqlalchemy import create_engine, text

    database_path = tmp_path / "theater-memory.db"
    connection_string = f"sqlite:///{database_path}"
    history = SQLChatMessageHistory(
        connection_string=connection_string,
        session_id="theater-story-stable",
        table_name="message_store",
    )
    history.add_messages([SystemMessage(content="旧周目摘要")])
    history.replace_messages([
        SystemMessage(content="新周目一"),
        SystemMessage(content="新周目二"),
    ])

    with create_engine(connection_string).connect() as connection:
        rows = connection.execute(
            text(
                "SELECT message FROM message_store "
                "WHERE session_id = :session_id ORDER BY id"
            ),
            {"session_id": "theater-story-stable"},
        ).fetchall()

    assert len(rows) == 2
    assert "旧周目摘要" not in "\n".join(row[0] for row in rows)
    assert "新周目一" in rows[0][0]
    assert "新周目二" in rows[1][0]


def test_recent_compression_prompt_uses_theater_episode_context_only():
    """摘要提示只引用剧场单集上下文，不重新展开完整演绎正文。"""  # noqa: DOCSTRING_CJK

    manager = CompressedRecentHistoryManager.__new__(CompressedRecentHistoryManager)
    manager.name_mapping = {"human": "哥哥"}
    message = AIMessage(
        content="雨点敲在窗沿。\n\n（抬起头）你来了。",
        metadata={
            "source": THEATER_MEMORY_SOURCE,
            "session_id": "memory_projection",
            "archive_from_revision": 1,
            "archive_through_revision": 1,
            "story_title": "雨夜合租",
            "episode_status": "paused",
            "parts": [
                {"kind": "scene_narration", "phase": "opening", "text": "雨点敲在窗沿。"},
                {"kind": "action", "phase": "opening", "text": "（抬起头）"},
                {"kind": "dialogue", "phase": "opening", "text": "你来了。"},
            ],
        },
    )

    rendered = manager._render_messages_to_text([message], "测试猫娘")

    assert "共同演绎小剧场《雨夜合租》" in rendered
    assert "属于虚构剧情，不代表现实经历" in rendered
    assert "雨点敲在窗沿。" not in rendered
    assert "测试猫娘 | （抬起头）你来了。" not in rendered
    assert "【旁白】" not in rendered


@pytest.mark.asyncio
async def test_numeric_end_receipt_concurrent_creation_converges(tmp_path):
    """同一结束事实的并发读取必须得到同一个回执和归档请求 ID。"""  # noqa: DOCSTRING_CJK

    store = NumericV2ArchiveStore(tmp_path)
    session = SimpleNamespace(
        story_package_id="numeric_v2_contract",
        session_id="receipt_concurrent",
        revision=7,
        catgirl_binding={
            "character_id": "character_" + "1" * 32,
            "catgirl_name": "测试猫娘",
        },
    )

    receipts = await asyncio.gather(
        *(store.acreate_or_get(session) for _ in range(8))
    )

    assert len({item["receipt_id"] for item in receipts}) == 1
    assert len({item["archive_request_id"] for item in receipts}) == 1
    assert receipts[0]["archive_request_id"].startswith("theater_archive_")

    # 同一 Session 继续演绎并产生新 revision 后再次退出，必须生成新的记忆回执。
    later_session = SimpleNamespace(
        story_package_id=session.story_package_id,
        session_id=session.session_id,
        revision=8,
        catgirl_binding=session.catgirl_binding,
    )
    later_receipt = await store.acreate_or_get(later_session)
    assert later_receipt["receipt_id"] != receipts[0]["receipt_id"]
    assert later_receipt["revision"] == 8


def test_numeric_archive_receipt_advances_incremental_watermark_after_success(tmp_path):
    """继续演绎再次退出时，只归档上次成功写入之后的新公开回合。"""  # noqa: DOCSTRING_CJK

    store = NumericV2ArchiveStore(tmp_path)
    binding = {
        "character_id": "character_" + "1" * 32,
        "catgirl_name": "测试猫娘",
    }
    first_session = SimpleNamespace(
        story_package_id="numeric_v2_contract",
        session_id="incremental_archive",
        revision=3,
        catgirl_binding=binding,
    )
    first_receipt = store.create_or_get(first_session)
    assert first_receipt["archive_from_revision"] == 1
    assert first_receipt["archive_through_revision"] == 3
    assert first_receipt["include_opening"] is True

    store.update(first_receipt, status="written")
    resumed_session = SimpleNamespace(
        story_package_id=first_session.story_package_id,
        session_id=first_session.session_id,
        revision=6,
        catgirl_binding=binding,
    )
    resumed_receipt = store.create_or_get(resumed_session)

    assert resumed_receipt["archive_from_revision"] == 4
    assert resumed_receipt["archive_through_revision"] == 6
    assert resumed_receipt["include_opening"] is False


def test_numeric_receipt_gc_preserves_legacy_written_receipt_until_cold_archive_exists(tmp_path):
    """升级前已写入记忆但未生成冷档案的回执不能被 GC 提前销毁。"""  # noqa: DOCSTRING_CJK

    store = NumericV2ArchiveStore(tmp_path)
    binding = {
        "character_id": "character_legacy_archive",
        "catgirl_name": "小葵",
    }
    first = SimpleNamespace(
        story_package_id="story_legacy_archive",
        session_id="legacy_archive_session",
        revision=1,
        catgirl_binding=binding,
    )
    first_receipt = store.create_or_get(first)
    store.update(first_receipt, status="written")
    second = SimpleNamespace(
        story_package_id=first.story_package_id,
        session_id=first.session_id,
        revision=2,
        catgirl_binding=binding,
    )
    second_receipt = store.create_or_get(second)
    store.update(second_receipt, status="skipped")

    store.cleanup_receipts({first.session_id})

    assert store.has_written_receipt_for_session(first.session_id) is True
    assert store._receipt_path(first_receipt["receipt_id"]).is_file()
    assert store.load_for_session(first.session_id)["receipt_id"] == second_receipt["receipt_id"]


def test_numeric_public_archives_keep_latest_five_plus_pinned(tmp_path):
    """冷档案默认有界，用户收藏的旧周目不参与自动淘汰。"""  # noqa: DOCSTRING_CJK

    store = NumericV2ArchiveStore(tmp_path)

    def session(index: int):
        return SimpleNamespace(
            story_package_id="story_retention",
            session_id=f"session_{index}",
            revision=index,
            catgirl_binding={
                "character_id": "character_retention",
                "catgirl_name": "小葵",
                "player_address": "哥哥",
            },
            opening_performance={"performance": "你来了。"},
            performance_history=(),
        )

    store.write_public_archive(title="有界剧本", session=session(0), ending=None)
    store.set_public_archive_pinned(
        story_id="story_retention",
        session_id="session_0",
        character_id="character_retention",
        legacy_catgirl_name="小葵",
        pinned=True,
    )
    for index in range(1, 7):
        store.write_public_archive(title="有界剧本", session=session(index), ending=None)

    archives = store.list_public_archives(
        story_id="story_retention",
        character_id="character_retention",
        legacy_catgirl_name="小葵",
    )
    assert len(archives) == 6
    assert {archive["session_id"] for archive in archives} == {
        "session_0", "session_2", "session_3", "session_4", "session_5", "session_6",
    }
    assert next(item for item in archives if item["session_id"] == "session_0")["pinned"] is True

    store.set_public_archive_pinned(
        story_id="story_retention",
        session_id="session_0",
        character_id="character_retention",
        legacy_catgirl_name="小葵",
        pinned=False,
    )
    remaining = store.list_public_archives(
        story_id="story_retention",
        character_id="character_retention",
        legacy_catgirl_name="小葵",
    )
    assert {archive["session_id"] for archive in remaining} == {
        "session_2", "session_3", "session_4", "session_5", "session_6",
    }


def test_numeric_public_archive_stage_can_commit_or_discard(tmp_path):
    """记忆请求失败时只留待提交副本，用户改选不记录后必须可销毁。"""  # noqa: DOCSTRING_CJK

    store = NumericV2ArchiveStore(tmp_path)
    session = SimpleNamespace(
        story_package_id="story_stage",
        session_id="session_stage",
        revision=1,
        catgirl_binding={
            "character_id": "character_stage",
            "catgirl_name": "小葵",
            "player_address": "哥哥",
        },
        opening_performance={"performance": "你来了。"},
        performance_history=(),
    )
    receipt = store.create_or_get(session)

    store.stage_public_archive(
        receipt=receipt,
        title="两阶段归档",
        session=session,
        ending=None,
    )
    assert not store._public_archive_path(session.session_id).exists()
    assert store._staged_archive_path(receipt["receipt_id"]).is_file()
    store.discard_staged_public_archive(receipt["receipt_id"])
    assert not store._staged_archive_path(receipt["receipt_id"]).exists()

    store.stage_public_archive(
        receipt=receipt,
        title="两阶段归档",
        session=session,
        ending=None,
    )
    store.commit_staged_public_archive(receipt)
    assert store._public_archive_path(session.session_id).is_file()
    assert not store._staged_archive_path(receipt["receipt_id"]).exists()


def test_numeric_receipt_gc_keeps_only_active_session_pointer(tmp_path):
    """冷启动回执 GC 只保留仍可恢复 Session 的最新指针。"""  # noqa: DOCSTRING_CJK

    store = NumericV2ArchiveStore(tmp_path)

    def session(index: int):
        return SimpleNamespace(
            story_package_id="story_receipt_gc",
            session_id=f"receipt_session_{index}",
            revision=index,
            catgirl_binding={
                "character_id": "character_receipt_gc",
                "catgirl_name": "小葵",
            },
        )

    for index in range(3):
        store.create_or_get(session(index))

    result = store.cleanup_receipts({"receipt_session_2"})

    assert result == {"receipts_removed": 2, "pointers_removed": 2}
    assert len(list(store.root.glob("theater_end_*.json"))) == 1
    assert len(list(store.root.glob("session-*.json"))) == 1


def test_numeric_session_survives_current_catgirl_rename(tmp_path, monkeypatch):
    """角色改名只改变展示名称，不能让相同 character_id 的进度失效。"""  # noqa: DOCSTRING_CJK

    class _RenamableConfigManager(_ConfigManager):
        def __init__(self, root: Path):
            super().__init__(root)
            self.current_name = "改名前"

        def load_characters(self) -> dict:
            return {
                "当前猫娘": self.current_name,
                "猫娘": {
                    self.current_name: {
                        "昵称": self.current_name,
                        "人格": "安静而认真。",
                        "_reserved": {"character_id": "character_" + "1" * 32},
                    },
                },
                "主人": {"昵称": "哥哥"},
            }

    manager = _RenamableConfigManager(tmp_path)
    client = _client(tmp_path, monkeypatch, manager)
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "rename_session"},
        )
        assert started.status_code == 200

        manager.current_name = "改名后"
        restored = client.get(
            "/api/theater-numeric/session/active",
            params={"story_id": "numeric_v2_contract"},
        )
        resumed = client.post(
            "/api/theater-numeric/session/start",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "rename_session_duplicate_start",
            },
        )

    assert restored.status_code == 200
    assert restored.json()["session"]["session_id"] == "rename_session"
    assert resumed.status_code == 200
    assert resumed.json()["resumed"] is True
    assert resumed.json()["session"]["session_id"] == "rename_session"


def test_numeric_turn_preserves_catgirl_rename_during_model_wait(tmp_path, monkeypatch):
    """模型等待期间的同角色改名必须随本轮提交保留，不能被旧候选状态覆盖。"""  # noqa: DOCSTRING_CJK

    class _RenamableConfigManager(_ConfigManager):
        def __init__(self, root: Path):
            super().__init__(root)
            self.current_name = "改名前"

        def load_characters(self) -> dict:
            return {
                "当前猫娘": self.current_name,
                "猫娘": {
                    self.current_name: {
                        "昵称": self.current_name,
                        "人格": "安静而认真。",
                        "_reserved": {"character_id": "character_" + "1" * 32},
                    },
                },
                "主人": {"昵称": "哥哥"},
            }

    manager = _RenamableConfigManager(tmp_path)
    client = _client(tmp_path, monkeypatch, manager)

    async def rename_during_actor(*args, **kwargs):
        manager.current_name = "改名后"
        return _performance("我在听。")

    monkeypatch.setattr(
        numeric_theater_router.NumericV2Actor,
        "generate_turn",
        rename_during_actor,
    )
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "rename_mid_turn"},
        )
        assert started.status_code == 200

        submitted = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "rename_mid_turn",
                "client_turn_id": "rename_mid_turn_1",
                "base_revision": 0,
                "message": "继续说吧。",
            },
        )

    assert submitted.status_code == 200
    session_path = (
        tmp_path
        / "theater"
        / "numeric_v2"
        / "sessions"
        / "rename_mid_turn.json"
    )
    persisted = json.loads(session_path.read_text(encoding="utf-8"))
    assert persisted["session"]["catgirl_binding"]["catgirl_name"] == "改名后"


def test_numeric_turn_preserves_player_address_fact_during_model_wait(
    tmp_path,
    monkeypatch,
):
    """本轮称呼事实生成后即被冻结，配置并发变化不能破坏 Ledger 重放。"""  # noqa: DOCSTRING_CJK

    class _MutableAddressConfigManager(_ConfigManager):
        def __init__(self, root: Path):
            super().__init__(root)
            self.player_address = "你"

        def load_characters(self) -> dict:
            return {
                "当前猫娘": "测试猫娘",
                "猫娘": {
                    "测试猫娘": _catgirl_profile("测试猫娘", "安静而认真。"),
                },
                "主人": {"昵称": self.player_address},
            }

    manager = _MutableAddressConfigManager(tmp_path)
    client = _client(
        tmp_path,
        monkeypatch,
        manager,
        player_address_known=False,
    )

    async def change_address_during_actor(*args, **kwargs):
        manager.player_address = "哥哥"
        return _performance("我在听。")

    monkeypatch.setattr(
        numeric_theater_router.NumericV2Actor,
        "generate_turn",
        change_address_during_actor,
    )
    with client:
        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "address_mid_turn"},
        )
        assert started.status_code == 200

        submitted = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "address_mid_turn",
                "client_turn_id": "address_mid_turn_1",
                "base_revision": 0,
                "message": "哥哥，我们继续吧。",
            },
        )
        restored = client.get(
            "/api/theater-numeric/session/address_mid_turn",
            params={"story_id": "numeric_v2_contract"},
        )

    assert submitted.status_code == 200
    assert restored.status_code == 200
    assert restored.json()["session"]["player_address_known"] is False
    session_path = (
        tmp_path
        / "theater"
        / "numeric_v2"
        / "sessions"
        / "address_mid_turn.json"
    )
    persisted = json.loads(session_path.read_text(encoding="utf-8"))
    assert persisted["session"]["catgirl_binding"]["player_address"] == "你"
