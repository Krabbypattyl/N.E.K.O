# -*- coding: utf-8 -*-
"""A forgotten theater story must stay forgotten even when an archive write lands late.

The theater gives up on ``/cache`` after a few seconds while the memory server
may still process that request. When the player then forgets the whole story,
the memory server records a story-level tombstone: any write of that story
issued before the forget is dropped, even after a memory-server restart, while
an archive the player starts after the forget still lands.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory.recent import CompressedRecentHistoryManager, TheaterEpisodeRetracted
from utils import recent_file
from utils.llm_client import SystemMessage, messages_from_dict


@pytest.fixture(autouse=True)
def _isolated_recent_state(monkeypatch):
    registries = (
        recent_file._LOCKS,
        recent_file._PENDING,
        recent_file._REDIRECTS,
        recent_file._DELETED,
        recent_file._GENERATIONS,
        recent_file._CONTENT_VERSIONS,
    )
    for registry in registries:
        registry.clear()
    monkeypatch.setattr("memory.recent.assert_cloudsave_writable", lambda *a, **kw: None)
    yield
    for registry in registries:
        registry.clear()


class _FakeConfig:
    def __init__(self, name: str, recent_path: str):
        self._name = name
        self._recent_path = recent_path

    async def aget_character_data(self):
        return (None, None, None, None, {}, None, None, None, {self._name: self._recent_path})


def _manager(root, name="Role"):
    (root / name).mkdir(parents=True, exist_ok=True)
    recent_path = str(root / name / "recent.json")
    mgr = object.__new__(CompressedRecentHistoryManager)
    mgr._config_manager = _FakeConfig(name, recent_path)
    mgr.max_history_length = 4
    mgr.compress_threshold = 5
    mgr.log_file_path = {name: recent_path}
    mgr.name_mapping = {"human": "Master", "ai": name, "system": "SYSTEM_MESSAGE"}
    mgr.user_histories = {}
    return mgr, name, recent_path


def _capsule(story_id="story_rain", session_id="session_rain"):
    return SystemMessage(content="两人保住了共同的住处。", metadata={
        "source": "theater_numeric_v2",
        "memory_tier": "episode_summary",
        "message_kind": "episode_summary",
        "story_id": story_id,
        "session_id": session_id,
        "archive_through_revision": 5,
        "episode_summary": "两人保住了共同的住处。",
    })


def _disk_keys(recent_path):
    try:
        with open(recent_path, encoding="utf-8") as handle:
            messages = messages_from_dict(json.load(handle))
    except FileNotFoundError:
        return []
    return sorted(
        (message.metadata.get("story_id"), message.metadata.get("session_id"))
        for message in messages
    )


def _cache_request(memory_server, message, request_id, *, attempt=1, issued_at=None):
    return memory_server.HistoryRequest(
        input_history=json.dumps([{
            "role": "system",
            "content": message.content,
            "metadata": dict(message.metadata),
        }], ensure_ascii=False),
        idempotency_key=request_id,
        theater_archive_attempt=attempt,
        theater_archive_issued_at=issued_at,
    )


@pytest.mark.unit
def test_story_forget_tombstone_drops_writes_issued_before_it_and_survives_restart(tmp_path):
    mgr, name, recent_path = _manager(tmp_path)
    issued_before = time.time() - 1
    forgotten_at = asyncio.run(mgr.record_theater_story_forget(name, "story_rain"))

    restarted, _, _ = _manager(tmp_path)
    for manager, issued_at in ((mgr, issued_before), (restarted, forgotten_at), (restarted, None)):
        with pytest.raises(TheaterEpisodeRetracted):
            asyncio.run(manager.upsert_theater_episode(
                _capsule(), name, archive_request_id="late", archive_attempt=1,
                archive_issued_at=issued_at,
            ))
    assert _disk_keys(recent_path) == []

    # Other stories are untouched, and an archive issued after the forget lands.
    asyncio.run(restarted.upsert_theater_episode(
        _capsule(story_id="story_other"), name, archive_request_id="other",
        archive_attempt=1, archive_issued_at=issued_before,
    ))
    asyncio.run(restarted.upsert_theater_episode(
        _capsule(session_id="session_new"), name, archive_request_id="new",
        archive_attempt=1, archive_issued_at=forgotten_at + 1,
    ))
    assert _disk_keys(recent_path) == [("story_other", "session_rain"), ("story_rain", "session_new")]


@pytest.mark.unit
def test_story_forget_and_attempt_tombstones_share_the_sidecar(tmp_path, monkeypatch):
    mgr, name, _ = _manager(tmp_path)
    sidecar = tmp_path / name / "theater_retractions.json"

    def read_sidecar():
        with open(sidecar, encoding="utf-8") as handle:
            return json.load(handle)

    now = time.time()
    first = asyncio.run(mgr.record_theater_story_forget(name, "story_rain"))
    # Recording an attempt tombstone keeps the story tombstone, and vice versa.
    asyncio.run(mgr.record_theater_retraction(
        name, story_id="story_rain", session_id="session_rain",
        archive_through_revision=5, archive_request_id="declined", archive_attempt=1,
    ))
    assert read_sidecar()["forgotten_stories"] == [{"story_id": "story_rain", "forgotten_at": first}]
    # A repeated forget never moves the watermark backwards (clock stepped back).
    import memory.recent as recent_module
    from tests.fake_clock import patch_module_clock

    patch_module_clock(monkeypatch, recent_module, time=lambda: first - 60)
    asyncio.run(mgr.record_theater_story_forget(name, "story_rain"))
    payload = read_sidecar()
    assert [entry["archive_request_id"] for entry in payload["entries"]] == ["declined"]
    assert payload["forgotten_stories"] == [{"story_id": "story_rain", "forgotten_at": first}]
    assert first >= now


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forget_endpoint_drops_late_cache_write_and_admits_a_later_archive(tmp_path):
    from app import memory_server

    mgr, name, recent_path = _manager(tmp_path)
    fake_time = MagicMock()
    fake_time.areconcile_theater_conversations = AsyncMock(return_value={"removed": 0, "stored": 1})
    fake_spawn = AsyncMock()
    # The archive request was issued (and timed out on the theater side) before the forget.
    issued_before = time.time()

    with patch.object(memory_server.runtime, "recent_history_manager", mgr), \
         patch.object(memory_server.runtime, "time_manager", fake_time), \
         patch.object(memory_server.post_turn, "_spawn_outbox_post_turn_signals", fake_spawn):
        forgotten = await memory_server.forget_theater_memory(
            name, memory_server.TheaterMemoryForgetRequest(story_id="story_rain"),
        )
        assert forgotten["ok"] is True

        late = await memory_server.cache_conversation(
            _cache_request(memory_server, _capsule(), "timed_out", issued_at=issued_before), name,
        )
        assert late == {"status": "retracted", "count": 0}
        assert _disk_keys(recent_path) == []
        fake_time.areconcile_theater_conversations.assert_awaited_once()
        fake_spawn.assert_not_awaited()

        with open(tmp_path / name / "theater_retractions.json", encoding="utf-8") as handle:
            forgotten_at = json.load(handle)["forgotten_stories"][0]["forgotten_at"]
        chosen = await memory_server.cache_conversation(
            _cache_request(
                memory_server, _capsule(session_id="session_new"), "new_run",
                issued_at=forgotten_at + 0.001,
            ),
            name,
        )
    assert chosen == {"status": "cached", "count": 1}
    assert _disk_keys(recent_path) == [("story_rain", "session_new")]


@pytest.mark.unit
def test_router_forget_after_archive_timeout_fences_the_late_write(tmp_path, monkeypatch):
    """End to end: archive times out, forget completes, the late /cache is dropped."""
    from app import memory_server
    from tests.unit.test_theater_numeric_v2_router import _client, _ended_archive_payload

    catgirl = "测试猫娘"
    mgr, _, recent_path = _manager(tmp_path / "memory", catgirl)
    fake_time = MagicMock()
    fake_time.areconcile_theater_conversations = AsyncMock(return_value={"removed": 0, "stored": 1})
    monkeypatch.setattr(memory_server.runtime, "_settle_locks", {})
    monkeypatch.setattr(memory_server.runtime, "recent_history_manager", mgr)
    monkeypatch.setattr(memory_server.runtime, "time_manager", fake_time)
    monkeypatch.setattr(memory_server.post_turn, "_spawn_outbox_post_turn_signals", AsyncMock())
    calls = []
    in_flight = []
    mode = {"cache": "timeout"}

    async def post(url, **kwargs):
        body = kwargs.get("json")
        calls.append((url, body))
        if "/cache/" in url:
            request = memory_server.HistoryRequest(**body)
            if mode["cache"] == "timeout":
                # The memory server keeps the request; the theater gives up.
                in_flight.append(request)
                raise TimeoutError("memory service slow")
            data = await memory_server.cache_conversation(request, catgirl)
            return SimpleNamespace(is_success=True, content=b"{}", json=lambda: data)
        if url.endswith("/theater/retract"):
            # The per-request fence fails; the story tombstone alone must hold.
            return SimpleNamespace(is_success=False, content=b"{}", json=lambda: {})
        if url.endswith("/theater/forget"):
            data = await memory_server.forget_theater_memory(
                catgirl, memory_server.TheaterMemoryForgetRequest(**body),
            )
            return SimpleNamespace(is_success=True, content=b"{}", json=lambda: data)
        raise AssertionError(url)

    monkeypatch.setattr("utils.internal_http_client.get_internal_http_client", lambda: SimpleNamespace(post=post))
    scope = {"story_id": "numeric_v2_contract", "character_id": "character_" + "1" * 32}
    with _client(tmp_path, monkeypatch) as client:
        payload = _ended_archive_payload(client)
        assert client.post("/api/theater-numeric/session/archive", json=payload).status_code == 502
        forgot = client.post("/api/theater-numeric/memory/forget", json=scope)
        assert forgot.status_code == 200, forgot.text

        # The timed-out write finally reaches the memory server.
        assert len(in_flight) == 1 and in_flight[0].theater_archive_issued_at is not None
        late = asyncio.run(memory_server.cache_conversation(in_flight[0], catgirl))
        assert late == {"status": "retracted", "count": 0}
        assert _disk_keys(recent_path) == []

        # Forget also tried to fence the unresolved attempt by request id.
        retracts = [body for url, body in calls if url.endswith("/theater/retract")]
        assert retracts == [{
            "story_id": "numeric_v2_contract",
            "session_id": "gap_session",
            "archive_through_revision": 0,
            "archive_request_id": payload["archive_request_id"],
            "archive_attempt": 1,
        }]

        # A new run of the story, archived after the forget, is remembered.
        mode["cache"] = "deliver"
        new_scope = {"story_id": "numeric_v2_contract", "session_id": "after_forget"}
        started = client.post("/api/theater-numeric/session/start", json={
            **new_scope, "replace_existing": True,
        })
        assert started.status_code == 200, started.text
        ended = client.post("/api/theater-numeric/session/end", json={
            **new_scope, "base_revision": 0, "base_lifecycle_revision": 0,
        }).json()
        archived = client.post("/api/theater-numeric/session/archive", json={
            **new_scope, "revision": 0, "end_receipt_id": ended["end_receipt_id"],
            "archive_request_id": ended["archive_request_id"],
        })
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "written"
    assert _disk_keys(recent_path) == [("numeric_v2_contract", "after_forget")]
