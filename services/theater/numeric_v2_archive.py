"""Numeric v2 结束回执与公开记忆归档。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from .numeric_v2_performance import performance_content_blocks


_RECEIPT_LOCKS: dict[str, threading.Lock] = {}
_RECEIPT_LOCKS_GUARD = threading.Lock()


def _receipt_lock(path: Path) -> threading.Lock:
    """同一进程内按 Session 指针串行创建回执，避免并发刷新生成分叉。"""  # noqa: DOCSTRING_CJK

    key = str(path.resolve())
    with _RECEIPT_LOCKS_GUARD:
        return _RECEIPT_LOCKS.setdefault(key, threading.Lock())


class NumericV2ArchiveError(ValueError):
    """结束回执或归档请求无效。"""  # noqa: DOCSTRING_CJK


class NumericV2ArchiveStore:
    """把归档回执与剧情 Session 分开持久化，避免改写已结束 Ledger。"""  # noqa: DOCSTRING_CJK

    def __init__(self, theater_root: Path):
        self.root = Path(theater_root) / "numeric_v2" / "end_receipts"

    @staticmethod
    def _session_key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _session_path(self, session_id: str) -> Path:
        return self.root / f"session-{self._session_key(session_id)}.json"

    def _receipt_path(self, receipt_id: str) -> Path:
        if not receipt_id.startswith("theater_end_") or len(receipt_id) > 96:
            raise NumericV2ArchiveError("numeric_end_receipt_invalid")
        return self.root / f"{receipt_id}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NumericV2ArchiveError("numeric_end_receipt_read_failed") from exc
        return value if isinstance(value, dict) else None

    @staticmethod
    def _write(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode("utf-8"))
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
        except OSError as exc:
            raise NumericV2ArchiveError("numeric_end_receipt_write_failed") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def create_or_get(self, session: Any) -> dict[str, Any]:
        session_id = str(session.session_id)
        session_path = self._session_path(session_id)
        with _receipt_lock(session_path):
            pointer = self._read(session_path)
            if pointer and pointer.get("receipt_id"):
                existing = self.load(str(pointer["receipt_id"]))
                # 同一 Session 可以多次退出后继续；只有相同 revision 才是同一次退出回执。
                if existing is not None and existing.get("revision") == int(session.revision):
                    return existing
            # 回执和归档请求 ID 都由不可变结束事实确定，进程崩溃或并发创建后仍会收敛。
            seed = "\x1f".join(
                (
                    str(session.story_package_id),
                    session_id,
                    str(int(session.revision)),
                    str(session.catgirl_binding.get("character_id") or ""),
                )
            )
            digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            receipt = {
                "schema": "neko.theater.numeric.v2.end-receipt",
                "receipt_id": f"theater_end_{digest[:40]}",
                "story_id": str(session.story_package_id),
                "session_id": session_id,
                "revision": int(session.revision),
                "character_id": str(session.catgirl_binding.get("character_id") or ""),
                "catgirl_name": str(session.catgirl_binding.get("catgirl_name") or ""),
                "status": "pending",
                "archive_request_id": f"theater_archive_{digest}",
            }
            self._write(self._receipt_path(receipt["receipt_id"]), receipt)
            self._write(session_path, {"receipt_id": receipt["receipt_id"]})
            return receipt

    def load(self, receipt_id: str) -> dict[str, Any] | None:
        return self._read(self._receipt_path(receipt_id))

    def update(self, receipt: Mapping[str, Any], *, status: str, archive_request_id: str = "") -> dict[str, Any]:
        if status not in {"pending", "writing", "written", "skipped"}:
            raise NumericV2ArchiveError("numeric_archive_status_invalid")
        updated = dict(receipt)
        updated["status"] = status
        if archive_request_id:
            updated["archive_request_id"] = archive_request_id
        self._write(self._receipt_path(str(updated.get("receipt_id") or "")), updated)
        return updated

    async def acreate_or_get(self, session: Any) -> dict[str, Any]:
        """在线请求通过线程执行持久化，避免阻塞 FastAPI 事件循环。"""  # noqa: DOCSTRING_CJK

        return await asyncio.to_thread(self.create_or_get, session)

    async def aload(self, receipt_id: str) -> dict[str, Any] | None:
        """异步读取结束回执。"""  # noqa: DOCSTRING_CJK

        return await asyncio.to_thread(self.load, receipt_id)

    async def aupdate(
        self,
        receipt: Mapping[str, Any],
        *,
        status: str,
        archive_request_id: str = "",
    ) -> dict[str, Any]:
        """异步原子更新归档状态。"""  # noqa: DOCSTRING_CJK

        return await asyncio.to_thread(
            self.update,
            receipt,
            status=status,
            archive_request_id=archive_request_id,
        )


def build_numeric_v2_memory_messages(
    *,
    title: str,
    session: Any,
    ending: Mapping[str, Any] | None,
    tail_turns: int = 6,
) -> list[dict[str, Any]]:
    """只用公开演绎构造有限完整尾部和确定性摘要。"""  # noqa: DOCSTRING_CJK

    records = list(session.performance_history)[-max(1, int(tail_turns)):]
    messages: list[dict[str, Any]] = []
    for record in records:
        input_text = str(record.get("input_text") or "").strip()
        if input_text:
            messages.append({"role": "human", "content": [{"type": "text", "text": input_text}]})
        for block in performance_content_blocks(record):
            role = "ai" if block.get("type") == "dialogue" else "system"
            messages.append({"role": role, "content": [{"type": "text", "text": block["text"]}]})
    ending_title = str((ending or {}).get("title") or "").strip()
    ending_summary = str((ending or {}).get("summary") or "").strip()
    if ending_title:
        summary = f"小剧场《{title}》在结局《{ending_title}》落幕。"
    else:
        # 主动退出仍可继续，记忆中不能把暂离误写成剧情终局。
        summary = f"小剧场《{title}》在玩家本次退出的位置暂告一段落。"
    if ending_summary:
        summary += f" 公开结局摘要：{ending_summary}"
    messages.append({"role": "system", "content": [{"type": "text", "text": summary}]})
    return messages


__all__ = [
    "NumericV2ArchiveError",
    "NumericV2ArchiveStore",
    "build_numeric_v2_memory_messages",
]
