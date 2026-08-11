"""Numeric v2 Session 与 Ledger 的原子文件存储。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .numeric_v2_runtime import NumericV2Engine, ScriptSessionV2


STORE_SCHEMA = "neko.script.store.numeric.v2"
STORY_SESSION_INDEX_SCHEMA = "neko.script.story_session_index.numeric.v2"


class NumericV2StoreError(ValueError):
    """Numeric v2 存档无法安全读取或提交。"""


class NumericV2SessionExistsError(NumericV2StoreError):
    pass


class NumericV2SessionNotFoundError(NumericV2StoreError):
    pass


class NumericV2StoreRevisionConflictError(NumericV2StoreError):
    pass


@dataclass(frozen=True, slots=True)
class NumericV2StoredSession:
    session: "ScriptSessionV2"
    ledger_events: tuple[dict[str, Any], ...]


_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(path: Path) -> asyncio.Lock:
    key = str(path.resolve())
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


class NumericV2SessionStore:
    """每个 Session 一个文件，所有提交都先复验 revision 再原子替换。"""

    def __init__(self, root: Path, engine: "NumericV2Engine"):
        self.root = Path(root) / "numeric_v2" / "sessions"
        self.engine = engine

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not session_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in session_id):
            raise NumericV2StoreError("numeric_session_id_invalid")
        return self.root / f"{session_id}.json"

    @property
    def _story_session_index_path(self) -> Path:
        return self.root.parent / "story_sessions.json"

    def _read_story_session_index(self) -> dict[str, str]:
        path = self._story_session_index_path
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("schema") != STORY_SESSION_INDEX_SCHEMA:
            return {}
        stories = payload.get("stories")
        if not isinstance(stories, dict):
            return {}
        return {
            str(story_id): str(session_id)
            for story_id, session_id in stories.items()
            if str(story_id).strip() and str(session_id).strip()
        }

    def _write_story_session_index(self, stories: Mapping[str, str]) -> None:
        path = self._story_session_index_path
        encoded = json.dumps(
            {"schema": STORY_SESSION_INDEX_SCHEMA, "stories": dict(stories)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    async def get_story_session_id(self, story_id: str) -> str:
        async with _lock(self._story_session_index_path):
            return self._read_story_session_index().get(str(story_id or "").strip(), "")

    async def set_story_session_id(self, story_id: str, session_id: str) -> None:
        normalized_story_id = str(story_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        if not normalized_story_id or not normalized_session_id:
            raise NumericV2StoreError("numeric_story_session_index_invalid")
        async with _lock(self._story_session_index_path):
            stories = self._read_story_session_index()
            stories[normalized_story_id] = normalized_session_id
            self._write_story_session_index(stories)

    async def restore_story_session(self, story_id: str) -> NumericV2StoredSession | None:
        """按剧本恢复唯一 Session；索引缺失时兼容扫描已有 Session 文件。"""

        normalized_story_id = str(story_id or "").strip()
        if not normalized_story_id:
            return None
        session_id = await self.get_story_session_id(normalized_story_id)
        candidates: list[NumericV2StoredSession] = []
        if self.root.is_dir():
            for path in self.root.glob("*.json"):
                try:
                    stored = await self.load(path.stem)
                except NumericV2StoreError:
                    continue
                if stored is not None and stored.session.story_package_id == normalized_story_id:
                    candidates.append(stored)
        if not candidates:
            return None

        restored = max(
            candidates,
            key=lambda item: (
                item.session.revision,
                int(item.session.session_id == session_id),
                item.session.session_id,
            ),
        )
        for candidate in candidates:
            if candidate.session.session_id == restored.session.session_id:
                continue
            path = self._path(candidate.session.session_id)
            async with _lock(path):
                if not path.is_file():
                    continue
                try:
                    current = self._read(path)
                    self._validate_chain(current)
                except NumericV2StoreError:
                    continue
                if current.session.story_package_id == normalized_story_id:
                    path.unlink()
        await self.set_story_session_id(normalized_story_id, restored.session.session_id)
        return restored

    async def create(self, session: "ScriptSessionV2") -> NumericV2StoredSession:
        path = self._path(session.session_id)
        async with _lock(path):
            if path.exists():
                raise NumericV2SessionExistsError("numeric_session_exists")
            self.engine.validate_session(session)
            stored = NumericV2StoredSession(session, ())
            self._write(path, stored, exclusive=True)
            return stored

    async def replace(self, session: "ScriptSessionV2") -> NumericV2StoredSession:
        """重开同一剧本时复用唯一 Session 文件，避免产生第二条演绎记录。"""

        path = self._path(session.session_id)
        async with _lock(path):
            if not path.is_file():
                raise NumericV2SessionNotFoundError("numeric_session_not_found")
            self.engine.validate_session(session)
            stored = NumericV2StoredSession(session, ())
            self._write(path, stored)
            return stored

    async def load(self, session_id: str) -> NumericV2StoredSession | None:
        path = self._path(session_id)
        async with _lock(path):
            if not path.is_file():
                return None
            stored = self._read(path)
            self._validate_chain(stored)
            return stored

    async def commit(
        self,
        session: "ScriptSessionV2",
        ledger_event: Mapping[str, Any],
    ) -> NumericV2StoredSession:
        path = self._path(session.session_id)
        async with _lock(path):
            if not path.is_file():
                raise NumericV2SessionNotFoundError("numeric_session_not_found")
            current = self._read(path)
            if current.session.revision != int(ledger_event.get("base_revision", -1)):
                raise NumericV2StoreRevisionConflictError("numeric_base_revision_mismatch")
            if any(event.get("client_turn_id") == ledger_event.get("client_turn_id") for event in current.ledger_events):
                raise NumericV2StoreRevisionConflictError("numeric_duplicate_client_turn_id")
            if session.revision != current.session.revision + 1:
                raise NumericV2StoreError("numeric_revision_not_monotonic")
            stored = NumericV2StoredSession(
                session,
                (*current.ledger_events, deepcopy(dict(ledger_event))),
            )
            self._validate_chain(stored)
            self._write(path, stored)
            return stored

    async def end_session(self, session_id: str, *, base_revision: int, reason: str) -> NumericV2StoredSession:
        path = self._path(session_id)
        async with _lock(path):
            if not path.is_file():
                raise NumericV2SessionNotFoundError("numeric_session_not_found")
            current = self._read(path)
            if current.session.revision != base_revision:
                raise NumericV2StoreRevisionConflictError("numeric_base_revision_mismatch")
            if current.session.status == "ended":
                return current
            ended = replace(current.session, status="ended", ended_reason=str(reason or "user_exit"))
            stored = NumericV2StoredSession(ended, current.ledger_events)
            self._write(path, stored)
            return stored

    def _read(self, path: Path) -> NumericV2StoredSession:
        from .numeric_v2_runtime import ScriptSessionV2

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NumericV2StoreError("numeric_session_read_failed") from exc
        if not isinstance(payload, dict) or payload.get("schema") != STORE_SCHEMA:
            raise NumericV2StoreError("numeric_store_schema_invalid")
        session = ScriptSessionV2.from_mapping(dict(payload.get("session") or {}))
        events = payload.get("ledger_events")
        if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
            raise NumericV2StoreError("numeric_ledger_invalid")
        return NumericV2StoredSession(session, tuple(deepcopy(events)))

    def _validate_chain(self, stored: NumericV2StoredSession) -> None:
        self.engine.validate_session(stored.session)
        events = stored.ledger_events
        if len(events) != stored.session.revision:
            raise NumericV2StoreError("numeric_ledger_revision_mismatch")
        expected_revision = 0
        expected_node = str(self.engine.story["start_node_id"])
        expected_metrics = {str(key): int(value) for key, value in self.engine.story["initial_state"]["metrics"].items()}
        seen_turns: set[str] = set()
        for event in events:
            expected_revision += 1
            if event.get("base_revision") != expected_revision - 1 or event.get("result_revision") != expected_revision:
                raise NumericV2StoreError("numeric_ledger_revision_chain_invalid")
            if event.get("from_node_id") != expected_node:
                raise NumericV2StoreError("numeric_ledger_node_chain_invalid")
            if event.get("before_metrics") != expected_metrics:
                raise NumericV2StoreError("numeric_ledger_metric_chain_invalid")
            expected_node = str(event.get("to_node_id") or "")
            expected_metrics = dict(event.get("after_metrics") or {})
            turn_id = str(event.get("client_turn_id") or "")
            if not turn_id or turn_id in seen_turns:
                raise NumericV2StoreError("numeric_ledger_turn_id_invalid")
            seen_turns.add(turn_id)
        if expected_node != stored.session.current_node_id or expected_metrics != stored.session.metrics:
            raise NumericV2StoreError("numeric_session_not_at_ledger_tail")
        if seen_turns != set(stored.session.processed_client_turn_ids):
            raise NumericV2StoreError("numeric_processed_turn_ids_mismatch")
        if len(stored.session.performance_history) != len(events):
            raise NumericV2StoreError("numeric_performance_history_mismatch")

    def _write(self, path: Path, stored: NumericV2StoredSession, *, exclusive: bool = False) -> None:
        payload = {
            "schema": STORE_SCHEMA,
            "session": stored.session.to_dict(),
            "ledger_events": deepcopy(list(stored.ledger_events)),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp", delete=False) as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            if exclusive:
                try:
                    os.link(temporary_path, path)
                except FileExistsError as exc:
                    raise NumericV2SessionExistsError("numeric_session_exists") from exc
            else:
                os.replace(temporary_path, path)
                temporary_path = None
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


__all__ = [
    "NumericV2SessionExistsError",
    "NumericV2SessionNotFoundError",
    "NumericV2SessionStore",
    "NumericV2StoreError",
    "NumericV2StoreRevisionConflictError",
    "NumericV2StoredSession",
]
