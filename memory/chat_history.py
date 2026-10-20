"""Read complete conversation records shared by Memory Browser and history sync.

The chronological archive is independent of recent-memory review/compression.
Legacy row identities include the character and session, never matching by text.
"""

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from uuid import NAMESPACE_URL, uuid5

from memory.message_sources import is_theater_memory_message


_ID = re.compile(r"[0-9a-f]{32}")
_STAMP = re.compile(r"^\[\d{8} [^\]\s]+ \d{2}:\d{2}\] ?")


def read_chat_history(path: Path, uid: str) -> list[dict]:
    """Read original text only, without creating a database or reading recent.json."""
    path = Path(path)
    if not path.exists():
        return []
    records = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='time_indexed_original'").fetchone():
            return []
        columns = {row[1] for row in conn.execute("PRAGMA table_info(time_indexed_original)")}
        stamp_column = "timestamp" if "timestamp" in columns else "NULL"
        rows = conn.execute(f"SELECT id,session_id,message,{stamp_column} FROM time_indexed_original ORDER BY id")
        for row_id, session_id, payload, saved_at in rows:
            item = json.loads(payload)
            role = {"human": "user", "ai": "assistant"}.get(item.get("type"))
            if role is None or is_theater_memory_message(item):
                continue
            data = item.get("data", {})
            metadata = data.get("metadata") or {}
            proof = metadata.get("chat_original_source") or {}
            origin = proof.get("origin", "legacy")
            body = data.get("content", "")
            if isinstance(body, list):
                body = "".join(part.get("text", "") for part in body if isinstance(part, dict) and part.get("type") == "text")
            if not isinstance(body, str) or not body:
                continue
            if role == "assistant":
                body = _STAMP.sub("", body, count=1)
            identity = proof.get("recordId")
            if not isinstance(identity, str) or not _ID.fullmatch(identity):
                identity = uuid5(NAMESPACE_URL, f"neko-chat-history:{uid}:{session_id}:{row_id}").hex
            original_time = proof.get("originalTime")
            if original_time is None and saved_at:
                original_time = str(int(datetime.fromisoformat(saved_at).timestamp() * 1000))
            record = {"messageId": identity, "characterSyncId": uid, "role": role, "text": body,
                      "originalTime": original_time, "timezone": "UTC" if original_time else None,
                      "conversationId": None, "replyToMessageId": None, "origin": origin}
            if identity in records and records[identity] != record:
                raise ValueError("chat_history_identity_conflict")
            records[identity] = record
    return list(records.values())
