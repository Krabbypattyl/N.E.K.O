"""验证 Numeric v2 HTTP 纵向链路和失败不提交边界。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from main_routers import numeric_theater_router
from services.theater.numeric_v2_actor import NumericV2ActorError
from services.theater.numeric_v2_archive import NumericV2ArchiveStore
from services.theater.numeric_v2_evaluator import NumericV2EvaluationResult
from services.theater.numeric_v2_registry import NumericV2PackageError
from tests.unit.test_theater_numeric_v2_contract import numeric_v2_story


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


def _client(tmp_path: Path, monkeypatch, config_manager=None, *, opening_text="你回来了。") -> TestClient:
    packages = tmp_path / "theater" / "numeric_v2" / "packages"
    packages.mkdir(parents=True)
    (packages / "numeric_v2_contract.json").write_text(
        json.dumps(numeric_v2_story(), ensure_ascii=False),
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


def test_numeric_v2_router_starts_restores_and_submits_free_input(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        listed = client.get("/api/theater-numeric/stories")
        assert listed.status_code == 200
        assert listed.json()["stories"][0]["story_id"] == "numeric_v2_contract"
        assert listed.json()["stories"][0]["display_intro"]["player_identity"].startswith("哥哥，")

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


def test_numeric_end_receipt_archives_public_tail_once(tmp_path, monkeypatch):
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

    assert archived.status_code == 200
    assert archived.json()["status"] == "written"
    assert replay.status_code == 200
    assert replay.json()["status"] == "already_written"
    assert restored.json()["end_receipt_id"] == receipt["end_receipt_id"]
    assert restored.json()["archive_status"] == "written"
    assert captured["calls"] == 1
    assert captured["url"].endswith("/%E6%B5%8B%E8%AF%95%E7%8C%AB%E5%A8%98")
    assert captured["payload"]["idempotency_key"] == ended["archive_request_id"]
    memory_text = captured["payload"]["input_history"]
    assert "我把信放在桌上。" in memory_text
    assert "我在听。" in memory_text
    assert "暂告一段落" in memory_text
    assert "metrics" not in memory_text
    assert "suggested_inputs" not in memory_text
    assert "mainline_" not in memory_text


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
