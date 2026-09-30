"""Server-side theater backstop for proactive chat and ordinary voice start.

The frontend already suppresses proactive chat and blocks the ordinary
microphone while a theater performance runs. These tests pin the server-side
backstop: a TTL-bounded in-memory activity signal fed by successful theater
session requests, consulted by the proactive-chat router and by the WebSocket
voice start, failing open once the signal expires.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main_routers.websocket_router as websocket_router
from main_logic.proactive_chat import contracts
from main_routers import numeric_theater_router
from main_routers.system_router import proactive_chat_flow
from tests.unit.test_proactive_service_boundary import _wire_router_dependencies
from tests.unit.test_theater_numeric_v2_router import _client
from tests.unit.test_websocket_binary_audio import (
    _EventWebSocket,
    _install_protocol_endpoint,
    _ProtocolManager,
)
from utils import theater_activity

ROOT = Path(__file__).resolve().parents[2]


def _ok(status: str, name: str = "Lan") -> dict:
    return {"ok": True, "session": {"status": status}, "participants": {"catgirl_name": name}}


def test_activity_signal_expires_and_only_successful_payloads_count():
    """Mark on active payloads, clear on ended ones, ignore failures, and expire after the TTL."""
    ttl = theater_activity.THEATER_ACTIVITY_TTL_SECONDS
    assert theater_activity.is_theater_active("Lan") is False

    theater_activity.mark_theater_activity("Lan", now=1000.0)
    assert theater_activity.is_theater_active("Lan", now=1000.0 + ttl - 1) is True
    assert theater_activity.is_theater_active("Other", now=1000.0) is False
    # A stale signal never outlives its TTL, and expiry drops the entry.
    assert theater_activity.is_theater_active("Lan", now=1000.0 + ttl) is False
    assert "Lan" not in theater_activity._last_activity

    theater_activity.note_theater_session_response(_ok("active"))
    assert theater_activity.is_theater_active("Lan") is True
    for ignored in (
        {"ok": False, "reason": "numeric_session_not_found"},
        {**_ok("ended"), "ok": False},
        {"ok": True},
        _ok("ended", name=""),
        numeric_theater_router._error("numeric_session_not_found", 404),
    ):
        theater_activity.note_theater_session_response(ignored)
        assert theater_activity.is_theater_active("Lan") is True
    theater_activity.note_theater_session_response(_ok("ended"))
    assert theater_activity.is_theater_active("Lan") is False


def test_theater_session_requests_drive_the_activity_signal(tmp_path, monkeypatch):
    """Launch, input and resume mark the character; end and release clear it; browsing does not mark."""
    client = _client(tmp_path, monkeypatch)
    name = numeric_theater_router._current_catgirl_binding(
        numeric_theater_router.get_config_manager()
    )["catgirl_name"]
    story = "numeric_v2_contract"

    def active() -> bool:
        return theater_activity.is_theater_active(name)

    with client:
        started = client.post("/api/theater-numeric/session/start",
                              json={"story_id": story, "session_id": "activity"})
        assert started.status_code == 200
        assert active() is True

        # The selector only browses progress; that must not claim the character.
        theater_activity.clear_theater_activity(name)
        assert client.get(f"/api/theater-numeric/session/active?story_id={story}").status_code == 200
        assert active() is False
        # A failed request neither marks nor clears.
        assert client.get(f"/api/theater-numeric/session/missing?story_id={story}").status_code == 404
        assert active() is False

        assert client.get(f"/api/theater-numeric/session/activity?story_id={story}").status_code == 200
        assert active() is True

        theater_activity.clear_theater_activity(name)
        submitted = client.post("/api/theater-numeric/session/input", json={
            "story_id": story, "session_id": "activity", "client_turn_id": "t1",
            "base_revision": 0, "message": "我先把信收好。",
        })
        assert submitted.status_code == 200
        assert active() is True

        ended = client.post("/api/theater-numeric/session/end", json={
            "story_id": story, "session_id": "activity", "base_revision": 1, "base_lifecycle_revision": 0,
        })
        assert ended.status_code == 200
        assert active() is False

        resumed = client.post("/api/theater-numeric/session/resume", json={
            "story_id": story, "session_id": "activity", "base_revision": 1, "base_lifecycle_revision": 1,
        })
        assert resumed.status_code == 200
        assert active() is True

        released = client.post("/api/theater-numeric/session/release", json={})
        assert released.status_code == 200
        assert released.json() == {"ok": True}
        assert active() is False


@pytest.mark.asyncio
async def test_proactive_chat_passes_while_theater_is_active(monkeypatch):
    """The proactive router declines with a PASS body for the performing character and delegates otherwise."""
    handle = AsyncMock(return_value=contracts.ProactiveChatResult(body={"delegated": True}))
    _wire_router_dependencies(monkeypatch, handle)

    theater_activity.mark_theater_activity("Yui")
    request = SimpleNamespace(json=AsyncMock(return_value={}))
    response = await proactive_chat_flow.proactive_chat(request)
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["reason_code"] == contracts.PROACTIVE_REASON_PASS_ROUTE_ACTIVE
    assert "theater" in body["message"]
    handle.assert_not_awaited()

    # Another character, and the same one once released, are untouched.
    request = SimpleNamespace(json=AsyncMock(return_value={"lanlan_name": "Other"}))
    assert json.loads((await proactive_chat_flow.proactive_chat(request)).body) == {"delegated": True}
    theater_activity.clear_all_theater_activity()
    request = SimpleNamespace(json=AsyncMock(return_value={}))
    assert json.loads((await proactive_chat_flow.proactive_chat(request)).body) == {"delegated": True}
    assert handle.await_count == 2


@pytest.mark.asyncio
async def test_ordinary_voice_start_is_declined_while_theater_is_active(monkeypatch):
    """Decline audio start before claiming the voice lease; text start and expired signals are unaffected."""
    theater_activity.mark_theater_activity("Lan")
    manager = _ProtocolManager()
    websocket = _EventWebSocket([
        {"action": "start_session", "input_type": "audio"},
        {"action": "start_session", "input_type": "text"},
    ])
    _install_protocol_endpoint(monkeypatch, manager=manager, websocket=websocket)

    await websocket_router.websocket_endpoint(websocket, "Lan")

    started_modes = [args for name, args in manager.calls if name == "start_session"]
    assert len(started_modes) == 1, "only the text start may proceed"
    assert not [name for name, _ in manager.calls if name in {"begin", "authorize"}]
    sent = [json.loads(payload) for payload in websocket.sent_text]
    assert {"type": "session_failed", "input_mode": "audio"} in sent
    statuses = [json.loads(item["message"]) for item in sent if item.get("type") == "status"]
    assert statuses == [{"code": "THEATER_SESSION_ACTIVE", "details": {"reason": "theater_session_active"}}]

    # Once the signal is gone the ordinary voice start proceeds unchanged.
    theater_activity.clear_all_theater_activity()
    manager = _ProtocolManager()
    websocket = _EventWebSocket([{"action": "start_session", "input_type": "audio"}])
    _install_protocol_endpoint(monkeypatch, manager=manager, websocket=websocket)
    await websocket_router.websocket_endpoint(websocket, "Lan")
    assert [name for name, _ in manager.calls if name == "start_session"] == ["start_session"]
    assert websocket.sent_text == []


def test_frontend_maps_the_decline_status_to_the_theater_voice_notice():
    """The client shows the existing theater voice notice instead of a raw error token."""
    source = (ROOT / "static" / "app" / "app-websocket.js").read_text(encoding="utf-8")
    branch = source.index("statusCode === 'THEATER_SESSION_ACTIVE'")
    assert "theater.voiceUnavailable" in source[branch:branch + 600]
    assert branch < source.index("var translatedMessage = window.translateStatusMessage")
