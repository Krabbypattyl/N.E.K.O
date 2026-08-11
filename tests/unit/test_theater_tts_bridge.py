"""验证自由模式只通过现有 Project TTS 朗读公开正文。"""  # noqa: DOCSTRING_CJK

import pytest
from starlette.websockets import WebSocketState

from main_routers import theater_router
from services.theater import tts_bridge


class _FakeManager:
    """记录自由模式 TTS 调用参数。"""  # noqa: DOCSTRING_CJK

    def __init__(self):
        self.websocket = type("WebSocket", (), {"client_state": WebSocketState.CONNECTED})()
        self.calls = []

    async def mirror_assistant_speech(self, line, **kwargs):
        """保存公开正文和镜像参数。"""  # noqa: DOCSTRING_CJK
        self.calls.append((line, kwargs))
        return {"ok": True, "audio_queued": True}


class _FakeRegistry:
    """只返回当前测试猫娘的播放器。"""  # noqa: DOCSTRING_CJK

    def __init__(self, manager):
        self.manager = manager

    def get(self, lanlan_name):
        """角色名匹配时提供播放器。"""  # noqa: DOCSTRING_CJK
        return self.manager if lanlan_name == "测试猫娘" else None


@pytest.mark.asyncio
async def test_free_tts_bridge_uses_free_claim_and_never_old_script_runtime(monkeypatch):
    """自由正文通过 Free claim 进入 TTS，路由不再依赖旧 Runtime。"""  # noqa: DOCSTRING_CJK
    manager = _FakeManager()

    async def claim_dialogue(*args, **kwargs):
        """模拟自由 Runtime 已原子认领正文。"""  # noqa: DOCSTRING_CJK
        return await kwargs["play"](
            {
                "ok": True,
                "line": "我会陪你把这一幕演完喵。",
                "lanlan_name": "测试猫娘",
                "session_id": "free_test",
                "state_revision": 1,
            }
        )

    monkeypatch.setattr(theater_router.free_runtime, "claim_dialogue_speech", claim_dialogue)
    monkeypatch.setattr(theater_router, "get_session_manager", lambda: _FakeRegistry(manager))
    monkeypatch.setattr(theater_router, "_theater_root", lambda: None)
    monkeypatch.setattr(theater_router, "_resolve_lanlan_name", lambda: "测试猫娘")

    result = await theater_router._speak_free_committed_dialogue(
        {"ok": True, "session_id": "free_test", "state_revision": 1}
    )
    assert result["audio_queued"] is True
    assert len(manager.calls) == 1
    assert manager.calls[0][1]["metadata"]["kind"] == "theater_free_dialogue"
    assert manager.calls[0][1]["mirror_text"] is False


@pytest.mark.asyncio
async def test_free_tts_bridge_skips_when_current_catgirl_changed(monkeypatch):
    """角色切换后不查找旧猫娘播放器，也不朗读旧自由正文。"""  # noqa: DOCSTRING_CJK
    manager = _FakeManager()

    async def claim_dialogue(*args, **kwargs):
        """返回旧角色认领结果，交给桥接层做当前角色复核。"""  # noqa: DOCSTRING_CJK
        return await kwargs["play"](
            {
                "ok": True,
                "line": "旧角色正文",
                "lanlan_name": "旧猫娘",
                "session_id": "free_test",
                "state_revision": 1,
            }
        )

    monkeypatch.setattr(theater_router.free_runtime, "claim_dialogue_speech", claim_dialogue)
    monkeypatch.setattr(theater_router, "get_session_manager", lambda: _FakeRegistry(manager))
    monkeypatch.setattr(theater_router, "_theater_root", lambda: None)
    monkeypatch.setattr(theater_router, "_resolve_lanlan_name", lambda: "测试猫娘")

    result = await theater_router._speak_free_committed_dialogue(
        {"ok": True, "session_id": "free_test", "state_revision": 1}
    )
    assert result["skipped"] == "character_changed"
    assert manager.calls == []


@pytest.mark.asyncio
async def test_shared_tts_bridge_speaks_committed_numeric_dialogue(monkeypatch):
    """Numeric v2 可以复用现有播放器桥接而不读取自由 Session。"""  # noqa: DOCSTRING_CJK

    manager = _FakeManager()
    result = await tts_bridge.speak_committed_line(
        "谢谢你陪着我。",
        session_id="numeric_test",
        state_revision=1,
        lanlan_name="测试猫娘",
        resolve_current_catgirl=lambda: "测试猫娘",
        get_session_manager=lambda: _FakeRegistry(manager),
        metadata_kind="theater_numeric_dialogue",
        request_id="numeric_tts_test",
    )

    assert result["audio_queued"] is True
    assert manager.calls[0][0] == "谢谢你陪着我。"
    assert manager.calls[0][1]["metadata"]["kind"] == "theater_numeric_dialogue"
