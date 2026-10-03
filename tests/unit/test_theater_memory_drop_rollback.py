# -*- coding: utf-8 -*-
"""Story forget and episode retract share one index-first removal with rollback.

Both endpoints drop the recallable time-index rows before the recent capsules.
When the recent drop fails, the index must be rebuilt from what recent actually
holds, not from the snapshot read before the drop: the drop may already be on
disk, and the snapshot would put removed capsules back into recall.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from memory.recent import CompressedRecentHistoryManager
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


def _capsule(story_id, session_id):
    return SystemMessage(content=f"{story_id} 的单集摘要", metadata={
        "source": "theater_numeric_v2",
        "memory_tier": "episode_summary",
        "message_kind": "episode_summary",
        "story_id": story_id,
        "session_id": session_id,
        "archive_through_revision": 5,
        "episode_summary": f"{story_id} 的单集摘要",
    })


def _disk_stories(recent_path):
    with open(recent_path, encoding="utf-8") as handle:
        messages = messages_from_dict(json.load(handle))
    return sorted(message.metadata.get("story_id") for message in messages)


def _call_forget(memory_server, name):
    return memory_server.forget_theater_memory(
        name, memory_server.TheaterMemoryForgetRequest(story_id="story_rain"),
    )


def _call_retract(memory_server, name):
    return memory_server.retract_theater_episode(
        name,
        memory_server.TheaterEpisodeRetractRequest(
            story_id="story_rain",
            session_id="session_rain",
            archive_through_revision=5,
        ),
    )


_ENDPOINTS = [
    pytest.param("forget_theater_story", _call_forget, "theater_memory_forget_failed", id="forget"),
    pytest.param("retract_theater_episode", _call_retract, "theater_memory_retract_failed", id="retract"),
]


async def _seed(mgr, name):
    for story_id, session_id in (("story_rain", "session_rain"), ("story_sun", "session_sun")):
        await mgr.upsert_theater_episode(_capsule(story_id, session_id), name)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("drop_method", "call", "detail"), _ENDPOINTS)
async def test_failed_recent_drop_restores_index_from_unchanged_recent(
    tmp_path, drop_method, call, detail,
):
    from app import memory_server

    mgr, name, recent_path = _manager(tmp_path)
    await _seed(mgr, name)
    fake_time = MagicMock()
    fake_time.areconcile_theater_conversations = AsyncMock(return_value={"removed": 2, "stored": 1})

    with patch.object(memory_server.runtime, "recent_history_manager", mgr), \
         patch.object(memory_server.runtime, "time_manager", fake_time), \
         patch.object(mgr, drop_method, AsyncMock(side_effect=OSError("disk full"))):
        with pytest.raises(HTTPException) as raised:
            await call(memory_server, name)

    assert raised.value.status_code == 500
    assert raised.value.detail == detail
    first, rollback = fake_time.areconcile_theater_conversations.await_args_list
    assert sorted(first.args[0]) == ["story_sun"]
    # Nothing reached disk, so the rollback re-indexes the capsule it removed.
    assert sorted(rollback.args[0]) == ["story_rain", "story_sun"]
    assert _disk_stories(recent_path) == ["story_rain", "story_sun"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("drop_method", "call", "detail"), _ENDPOINTS)
async def test_partly_persisted_recent_drop_keeps_removed_capsule_out_of_index(
    tmp_path, drop_method, call, detail,
):
    from app import memory_server

    mgr, name, recent_path = _manager(tmp_path)
    await _seed(mgr, name)
    fake_time = MagicMock()
    fake_time.areconcile_theater_conversations = AsyncMock(return_value={"removed": 2, "stored": 1})
    real_drop = getattr(mgr, drop_method)

    async def drop_then_fail(*args, **kwargs):
        await real_drop(*args, **kwargs)
        raise OSError("failed after the write")

    with patch.object(memory_server.runtime, "recent_history_manager", mgr), \
         patch.object(memory_server.runtime, "time_manager", fake_time), \
         patch.object(mgr, drop_method, drop_then_fail):
        with pytest.raises(HTTPException) as raised:
            await call(memory_server, name)

    assert raised.value.detail == detail
    _, rollback = fake_time.areconcile_theater_conversations.await_args_list
    # The rollback follows recent's actual content, not the pre-drop snapshot.
    assert sorted(rollback.args[0]) == ["story_sun"]
    assert _disk_stories(recent_path) == ["story_sun"]
