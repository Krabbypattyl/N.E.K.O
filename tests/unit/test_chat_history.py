"""Complete chat records are independent of reviewed recent memory."""

import json
import sqlite3

from memory.chat_history import read_chat_history


UID = "a" * 32


def archive(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE time_indexed_original(id INTEGER PRIMARY KEY,session_id TEXT,message TEXT,timestamp DATETIME)")
        conn.executemany("INSERT INTO time_indexed_original VALUES(?,?,?,?)", [
            (index + 1, "synthetic-session", json.dumps(row), "2026-10-20 12:43:15.123456")
            for index, row in enumerate(rows)])


def test_complete_archive_keeps_identical_records_and_never_reads_reviewed_memory(tmp_path):
    path = tmp_path / "time_indexed.db"
    archive(path, [{"type": "human", "data": {"content": "same"}}] * 2 + [
        {"type": "ai", "data": {"content": [{"type": "text", "text": "[20261020 Tue 12:43]original"}]}},
        {"type": "system", "data": {"content": "compressed memo"}},
        {"type": "human", "data": {"content": "camera scaffold", "metadata": {"sources": ["avatar-drop"]}}},
        {"type": "ai", "data": {"content": "theater", "metadata": {"source": "theater_numeric_v2"}}}])
    records = read_chat_history(path, UID)
    assert [row["text"] for row in records] == ["same", "same", "original", "camera scaffold"]
    assert len({row["messageId"] for row in records}) == 4
    (tmp_path / "recent.json").write_text('[{"type":"system","data":{"content":"reviewed"}}]')
    assert read_chat_history(path, UID) == records
    assert {row["messageId"] for row in read_chat_history(path, "b" * 32)}.isdisjoint(row["messageId"] for row in records)


def test_archive_reuses_producer_ids_and_keeps_tool_provenance(tmp_path):
    path = tmp_path / "time_indexed.db"
    proof = {"recordId": "1" * 32, "originalTime": "1234567890123", "origin": "tool"}
    archive(path, [{"type": "ai", "data": {"content": "tool reply", "metadata": {"chat_original_source": proof}}}])
    row, = read_chat_history(path, UID)
    assert row["messageId"] == proof["recordId"] and row["origin"] == "tool"
    assert row["originalTime"] == proof["originalTime"]
    assert read_chat_history(tmp_path / "missing.db", UID) == []
    assert not (tmp_path / "missing.db").exists()


def test_actual_memory_projection_preserves_ids_outside_model_messages(tmp_path):
    from main_logic.cross_server import _assistant_history_message, _append_user_input_cache_to_history
    from utils.llm_client import convert_to_messages, messages_to_dict

    user = {"recordId": "1" * 32, "originalTime": "1234567890123", "origin": "chat"}
    reply = {"recordId": "2" * 32, "originalTime": "1234567890124", "origin": "chat"}
    projected = []
    _append_user_input_cache_to_history(projected, 'user', set(), user)
    projected.append(_assistant_history_message('[20261020 Tue 12:43]reply', reply))
    messages = convert_to_messages(projected)
    assert all('metadata' not in message.to_openai() for message in messages)
    path = tmp_path / 'time_indexed.db'
    archive(path, messages_to_dict(messages))
    records = read_chat_history(path, UID)
    assert [row['messageId'] for row in records] == [user['recordId'], reply['recordId']]
    assert [row['text'] for row in records] == ['user', 'reply']
