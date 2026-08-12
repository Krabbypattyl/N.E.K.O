"""Numeric v2 Session 与 Ledger 的原子文件存储。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
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
    """Numeric v2 存档无法安全读取或提交。"""  # noqa: DOCSTRING_CJK


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
_STORY_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(path: Path) -> asyncio.Lock:
    key = str(path.resolve())
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


def _story_lock(path: Path, story_id: str) -> asyncio.Lock:
    key = f"{path.resolve()}::{story_id}"
    if key not in _STORY_LOCKS:
        _STORY_LOCKS[key] = asyncio.Lock()
    return _STORY_LOCKS[key]


class NumericV2SessionStore:
    """每个 Session 一个文件，所有提交都先复验 revision 再原子替换。"""  # noqa: DOCSTRING_CJK

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

    @asynccontextmanager
    async def story_session_guard(self, story_id: str):
        normalized_story_id = str(story_id or "").strip()
        async with _story_lock(self._story_session_index_path, normalized_story_id):
            yield

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
        """按剧本恢复唯一 Session；索引缺失时兼容扫描已有 Session 文件。"""  # noqa: DOCSTRING_CJK

        normalized_story_id = str(story_id or "").strip()
        if not normalized_story_id:
            return None
        async with self.story_session_guard(normalized_story_id):
            return await self._restore_story_session_unlocked(normalized_story_id)

    async def _restore_story_session_unlocked(self, normalized_story_id: str) -> NumericV2StoredSession | None:
        session_id = await self.get_story_session_id(normalized_story_id)
        candidates: list[NumericV2StoredSession] = []
        if self.root.is_dir():
            for path in self.root.glob("*.json"):
                try:
                    raw_payload = json.loads(path.read_text(encoding="utf-8"))
                    raw_session = raw_payload.get("session") if isinstance(raw_payload, dict) else None
                    if not isinstance(raw_session, dict) or str(raw_session.get("story_package_id") or "") != normalized_story_id:
                        continue
                    stored = await self.load(path.stem)
                except (OSError, ValueError):
                    continue
                if stored is not None and stored.session.story_package_id == normalized_story_id:
                    candidates.append(stored)
        if not candidates:
            return None

        indexed = next(
            (item for item in candidates if item.session.session_id == session_id),
            None,
        )
        active_candidates = [item for item in candidates if item.session.status != "ended"]
        # 结束的索引只代表历史记录；如果同时存在新的 active Session，优先恢复
        # active，避免旧索引阻断剧本继续演绎。
        if indexed is not None and indexed.session.status == "ended" and active_candidates:
            indexed = None
        restored = indexed or max(
            active_candidates or candidates,
            key=lambda item: (item.session.revision, item.session.session_id),
        )
        for candidate in candidates:
            if candidate.session.session_id == restored.session.session_id:
                continue
            # 已结束文件是历史证据，不能因为一次恢复扫描而删除；只收敛
            # 明确属于同一故事的旧 active 重复记录。
            if candidate.session.status == "ended":
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
        """重开同一剧本时复用唯一 Session 文件，避免产生第二条演绎记录。"""  # noqa: DOCSTRING_CJK

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
            if current.session.status == "ended":
                raise NumericV2StoreRevisionConflictError("session_already_ended")
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
        from .numeric_v2_runtime import MetricChangeV2, TurnRequestV2

        replay_session = self.engine.create_session(
            session_id=stored.session.session_id,
            catgirl_binding=stored.session.catgirl_binding,
            opening_performance=stored.session.opening_performance,
        )
        for event_index, event in enumerate(events):
            expected_revision += 1
            if not isinstance(event, Mapping):
                raise NumericV2StoreError("numeric_ledger_event_invalid")
            if event.get("base_revision") != expected_revision - 1 or event.get("result_revision") != expected_revision:
                raise NumericV2StoreError("numeric_ledger_revision_chain_invalid")
            if event.get("from_node_id") != expected_node:
                raise NumericV2StoreError("numeric_ledger_node_chain_invalid")
            if event.get("before_metrics") != expected_metrics:
                raise NumericV2StoreError("numeric_ledger_metric_chain_invalid")
            turn_id = str(event.get("client_turn_id") or "")
            if not turn_id or turn_id in seen_turns:
                raise NumericV2StoreError("numeric_ledger_turn_id_invalid")
            raw_changes = event.get("metric_changes")
            if not isinstance(raw_changes, list):
                raise NumericV2StoreError("numeric_ledger_metric_changes_invalid")
            try:
                changes = tuple(
                    MetricChangeV2.from_mapping(
                        {
                            key: change.get(key)
                            for key in ("metric_id", "delta", "criterion", "evidence")
                        },
                        self.engine.metric_schema,
                    )
                    for change in raw_changes
                    if isinstance(change, Mapping)
                )
                if len(changes) != len(raw_changes):
                    raise ValueError("metric_change_shape")
                scene_complete = event.get("scene_complete")
                if not isinstance(scene_complete, bool):
                    raise ValueError("scene_complete_shape")
                request = TurnRequestV2.from_mapping(
                    {
                        "client_turn_id": turn_id,
                        "base_revision": event.get("base_revision"),
                        "message": event.get("input_text"),
                    }
                )
                replayed = self.engine.resolve_turn(
                    replay_session,
                    request,
                    changes,
                    scene_complete=scene_complete,
                )
            except Exception as exc:
                raise NumericV2StoreError("numeric_ledger_replay_invalid") from exc
            expected_event = replayed.ledger_event
            for field in (
                "schema",
                "session_id",
                "client_turn_id",
                "base_revision",
                "result_revision",
                "from_node_id",
                "to_node_id",
                "route_id",
                "route_status",
                "scene_complete",
                "node_turn_count",
                "status",
                "before_metrics",
                "after_metrics",
                "metric_changes",
            ):
                if event.get(field) != expected_event.get(field):
                    raise NumericV2StoreError("numeric_ledger_replay_mismatch")
            performance = stored.session.performance_history[event_index]
            if not isinstance(performance, Mapping):
                raise NumericV2StoreError("numeric_performance_record_invalid")
            if (
                performance.get("client_turn_id") != turn_id
                or performance.get("revision") != expected_revision
                or performance.get("from_node_id") != expected_event["from_node_id"]
                or performance.get("to_node_id") != expected_event["to_node_id"]
            ):
                raise NumericV2StoreError("numeric_performance_record_mismatch")
            replay_session = replayed.session
            expected_node = str(expected_event["to_node_id"])
            expected_metrics = dict(expected_event["after_metrics"])
            seen_turns.add(turn_id)
        if expected_node != stored.session.current_node_id or expected_metrics != stored.session.metrics:
            raise NumericV2StoreError("numeric_session_not_at_ledger_tail")
        if seen_turns != set(stored.session.processed_client_turn_ids):
            raise NumericV2StoreError("numeric_processed_turn_ids_mismatch")
        if len(stored.session.performance_history) != len(events):
            raise NumericV2StoreError("numeric_performance_history_mismatch")
        if not (
            replay_session.status == stored.session.status
            or (
                replay_session.status == "active"
                and stored.session.status == "ended"
                and stored.session.ended_reason
            )
        ):
            raise NumericV2StoreError("numeric_session_status_mismatch")
        if replay_session.revision != stored.session.revision:
            raise NumericV2StoreError("numeric_session_revision_mismatch")

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
                except OSError:
                    # Some user-selected filesystems do not support hard links.
                    # Preserve exclusive creation semantics with a direct write.
                    try:
                        target_fd = os.open(
                            path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                    except FileExistsError as exc:
                        raise NumericV2SessionExistsError("numeric_session_exists") from exc
                    try:
                        with os.fdopen(target_fd, "wb") as target_file:
                            target_file.write(encoded)
                            target_file.flush()
                            os.fsync(target_file.fileno())
                    except Exception:
                        try:
                            path.unlink()
                        except OSError:
                            pass
                        raise
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
