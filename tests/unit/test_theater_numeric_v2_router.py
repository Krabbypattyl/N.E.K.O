"""验证 Numeric v2 HTTP 纵向链路和失败不提交边界。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from main_routers import numeric_theater_router
from services.theater.numeric_v2_actor import NumericV2ActorError
from services.theater.numeric_v2_evaluator import NumericV2EvaluationResult
from tests.unit.test_theater_numeric_v2_contract import numeric_v2_story


class _ConfigManager:
    def __init__(self, root: Path):
        self.app_docs_dir = root
        self.config_dir = root / "config"

    def load_characters(self) -> dict:
        return {
            "当前猫娘": "测试猫娘",
            "猫娘": {"测试猫娘": {"昵称": "测试猫娘", "人格": "安静而认真。"}},
            "主人": {"昵称": "哥哥"},
        }


def _performance(text: str) -> dict:
    return {
        "narration": "风铃轻轻响了一声。",
        "dialogue": [{"speaker_id": "active_catgirl", "text": text}],
        "suggested_inputs": ["继续听她说"],
    }


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    packages = tmp_path / "theater" / "numeric_v2" / "packages"
    packages.mkdir(parents=True)
    (packages / "numeric_v2_contract.json").write_text(
        json.dumps(numeric_v2_story(), ensure_ascii=False),
        encoding="utf-8",
    )
    manager = _ConfigManager(tmp_path)
    monkeypatch.setattr(numeric_theater_router, "get_config_manager", lambda: manager)
    monkeypatch.setattr(numeric_theater_router, "_validate_local_mutation_request", lambda *args, **kwargs: None)

    async def opening(*args, **kwargs):
        return _performance("你回来了。")

    async def turn(*args, **kwargs):
        return _performance("我在听。")

    async def evaluate(*args, **kwargs):
        return NumericV2EvaluationResult(metric_changes=(), scene_complete=False)

    async def skip_tts(*args, **kwargs):
        return None

    monkeypatch.setattr(numeric_theater_router.NumericV2Actor, "generate_opening", opening)
    monkeypatch.setattr(numeric_theater_router.NumericV2Actor, "generate_turn", turn)
    monkeypatch.setattr(numeric_theater_router.NumericV2MetricEvaluator, "evaluate", evaluate)
    monkeypatch.setattr(numeric_theater_router, "_speak_dialogue", skip_tts)
    app = FastAPI()
    app.include_router(numeric_theater_router.router)
    return TestClient(app)


def test_numeric_v2_router_starts_restores_and_submits_free_input(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        listed = client.get("/api/theater-numeric/stories")
        assert listed.status_code == 200
        assert listed.json()["stories"][0]["story_id"] == "numeric_v2_contract"

        started = client.post(
            "/api/theater-numeric/session/start",
            json={"story_id": "numeric_v2_contract", "session_id": "http_v2"},
        )
        body = started.json()
        assert started.status_code == 200
        assert body["session"]["schema"] == "neko.script.session.numeric.v2"
        assert body["session"]["opening_performance"]["dialogue"][0]["text"] == "你回来了。"
        assert "metrics" not in body["session"]
        assert body["scene"]["min_turns"] == 2
        assert body["story_intro"]["player_identity"].startswith("哥哥，")
        assert body["story_intro"]["catgirl_identity"].startswith("测试猫娘，")
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

        ended = client.post(
            "/api/theater-numeric/session/end",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "http_v2",
                "base_revision": 0,
            },
        )
        assert ended.status_code == 200
        assert ended.json()["session"]["status"] == "ended"

        restarted = client.post(
            "/api/theater-numeric/session/start",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "http_v2_after_restart",
                "replace_existing": True,
            },
        )
        assert restarted.status_code == 200
        assert restarted.json()["session"]["session_id"] == "http_v2"
        assert restarted.json()["session"]["status"] == "active"
        assert len(list((tmp_path / "theater" / "numeric_v2" / "sessions").glob("*.json"))) == 1

        submitted = client.post(
            "/api/theater-numeric/session/input",
            json={
                "story_id": "numeric_v2_contract",
                "session_id": "http_v2",
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
            "/api/theater-numeric/session/http_v2",
            params={"story_id": "numeric_v2_contract"},
        )
        assert restored.status_code == 200
        assert restored.json()["session"]["revision"] == 1


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
