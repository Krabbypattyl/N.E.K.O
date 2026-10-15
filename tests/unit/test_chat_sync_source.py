"""Proven new originals use disposable WAL files and synthetic owners only."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.chat_sync_writer_fixture import isolated_chat_sync_writer  # noqa: F401

from services.chat_sync_ledger import ChatDomain, ChatOriginal, ChatSyncLedger
from services.chat_sync_protocol import MAX_ORIGIN_SEQ
from services.chat_sync_runtime import DesktopSyncState
from services.chat_sync_trace import ChatSyncTrace
from services.chat_sync_writer import registered_writer, WriterQuarantined
import services.chat_sync_source as source_module
from services.chat_sync_source import (
    AssistantOriginalBuffer, ChatOriginalSource, known_source_ids, mark_turn_origin,
    purge_source_originals, recover_sources, save_original,
)
from tests.fake_clock import patch_module_clock

OWNER = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
UID = "a" * 32
DOMAIN = ChatDomain(OWNER, UID)
RAW = ChatOriginal("user", "  原文\r\n🙂e\u0301  ", original_time="1234567890123", timezone="UTC")


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    """Keep actual producer tests isolated from the user's telemetry singleton."""
    monkeypatch.setattr("utils.token_tracker.TokenTracker.get_instance", Mock())


def make_runtime(tmp_path, *, bound=True):
    """Exercise real source, consent and ledger transactions without any HTTP."""
    state = DesktopSyncState(tmp_path / "state.sqlite3")
    if bound:
        state.prepare_binding(DOMAIN)
    return SimpleNamespace(state=state, ledger=ChatSyncLedger(tmp_path / "originals.sqlite3"),
                           writer=registered_writer(tmp_path), wake_scheduler=Mock())


def source_for(runtime):
    """Locate only the dedicated synthetic source database."""
    return ChatOriginalSource(Path(runtime.state.path).parent / "sources.sqlite3")


def test_local_only_reply_is_durable_exact_and_never_an_upload_candidate(tmp_path):
    """Local replies survive reopening without entering any source upload lane."""
    runtime = make_runtime(tmp_path)
    source = source_for(runtime)
    raw = ChatOriginal("assistant", RAW.text, original_time=RAW.original_time, timezone="UTC")
    source.commit_local_reply(UID, "5" * 32, raw, "agent")
    source.commit_local_reply(UID, "5" * 32, raw, "agent")
    source.commit_local_reply("b" * 32, "6" * 32, raw, "tool")
    reopened = ChatOriginalSource(source.path)
    assert reopened.read_local_replies(source.path, UID) == [{
        "messageId": "5" * 32, "characterSyncId": UID, "role": "assistant",
        "text": RAW.text, "originalTime": RAW.original_time,
    }]
    assert reopened.pending(200) == []
    assert reopened.known_ids(OWNER, UID) == []
    with sqlite3.connect(source.path) as conn:
        for table in ("source_originals", "source_unbound", "source_origin_streams", "source_history_members"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert runtime.ledger.pending(DOMAIN) == []


@pytest.mark.parametrize("first_local", [False, True])
@pytest.mark.parametrize("bound", [False, True])
def test_local_only_identity_cannot_be_promoted_to_a_sync_original(tmp_path, first_local, bound):
    """Retries cannot reclassify a fixed identity across local and upload lanes."""
    source = ChatOriginalSource(tmp_path / "sources.sqlite3")
    raw = ChatOriginal("assistant", RAW.text)
    save_sync = lambda: source.commit(OWNER, UID, "5" * 32, raw) if bound else source.commit_unbound(UID, "5" * 32, raw)
    save_local = lambda: source.commit_local_reply(UID, "5" * 32, raw, "agent")
    first, second = (save_local, save_sync) if first_local else (save_sync, save_local)
    first()
    with pytest.raises(ValueError, match="chat_source_identity_conflict"):
        second()


def test_local_only_read_rejects_changed_bytes_and_does_not_initialize_missing_store(tmp_path):
    """Only the saved proof is restored; a read never mints a new source database."""
    missing = tmp_path / "missing.sqlite3"
    assert ChatOriginalSource.read_local_replies(missing, UID) == []
    assert not missing.exists()
    source = ChatOriginalSource(tmp_path / "sources.sqlite3")
    source.commit_local_reply(UID, "5" * 32, ChatOriginal("assistant", "original"), "agent")
    with sqlite3.connect(source.path) as conn:
        conn.execute("UPDATE source_local_replies SET original=replace(original,?,?)", ("original", "changed"))
    with pytest.raises(ValueError, match="chat_source_integrity_error"):
        ChatOriginalSource.read_local_replies(source.path, UID)


def test_local_only_clear_erases_body_and_late_retry_never_revives_it(tmp_path):
    """Clearing one role keeps only tombstones and leaves other roles untouched."""
    source = ChatOriginalSource(tmp_path / "sources.sqlite3")
    raw = ChatOriginal("assistant", RAW.text)
    source.commit_local_reply(UID, "5" * 32, raw, "agent")
    source.commit_local_reply("b" * 32, "6" * 32, raw, "tool")
    assert source.clear_local_replies(UID) == ["5" * 32]
    source.commit_local_reply(UID, "5" * 32, raw, "agent")
    assert source.clear_local_replies(UID) == ["5" * 32]
    assert source.read_local_replies(source.path, UID) == []
    assert len(source.read_local_replies(source.path, "b" * 32)) == 1
    with sqlite3.connect(source.path) as conn:
        assert conn.execute("SELECT original,deleted FROM source_local_replies WHERE uid=?", (UID,)).fetchone() == (None, 1)
    assert source.pending(200) == []


@pytest.mark.asyncio
async def test_local_only_save_and_restore_never_require_a_cloud_binding(tmp_path, monkeypatch):
    """The real save helper commits under the local UID without waking uploads."""
    runtime = make_runtime(tmp_path, bound=False)
    manager = SimpleNamespace(aload_characters=AsyncMock(return_value={"猫娘": {
        "Lan": {"_reserved": {"character_uid": UID}},
    }}))
    monkeypatch.setattr("services.chat_sync_runtime.get_desktop_chat_sync_runtime", AsyncMock(return_value=runtime))
    proof = {"recordId": "5" * 32, "originalTime": RAW.original_time, "origin": "agent"}
    assert await source_module.save_local_reply("Lan", RAW.text, proof, config_manager=manager) == proof["recordId"]
    assert await source_module.local_replies(runtime, UID) == [{
        "messageId": proof["recordId"], "characterSyncId": UID, "role": "assistant",
        "text": RAW.text, "originalTime": RAW.original_time,
    }]
    runtime.wake_scheduler.assert_not_called()
    assert await recover_sources(runtime) == 0
    runtime.state.prepare_binding(DOMAIN)
    assert await recover_sources(runtime) == 0
    assert runtime.ledger.pending(DOMAIN) == []
    assert await source_module.clear_local_replies(runtime, UID) == [proof["recordId"]]
    assert await source_module.local_replies(runtime, UID) == []


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.asyncio
async def test_local_only_restore_keeps_existing_writer_visibility_fence(tmp_path, monkeypatch, trusted):
    """Copied authority cannot expose recovered local replies in a new process."""
    runtime = make_runtime(tmp_path)
    source_for(runtime).commit_local_reply(UID, "5" * 32, ChatOriginal("assistant", RAW.text), "agent")
    monkeypatch.setattr(runtime.writer, "check", Mock(side_effect=WriterQuarantined()))
    monkeypatch.setattr(runtime.writer, "local_visibility_trusted", Mock(return_value=trusted))
    assert len(await source_module.local_replies(runtime, UID)) == (1 if trusted else 0)


@pytest.mark.asyncio
async def test_clear_without_local_replies_does_not_require_a_source_writer(tmp_path, monkeypatch):
    """Canonical outage clearing needs no extra write when this lane is empty."""
    runtime = make_runtime(tmp_path)
    source_for(runtime)
    monkeypatch.setattr(ChatOriginalSource, "_connect", Mock(side_effect=WriterQuarantined()))
    assert await source_module.clear_local_replies(runtime, UID) == []


@pytest.mark.asyncio
async def test_repeated_local_clear_returns_tombstones_without_a_writer_commit(tmp_path, monkeypatch):
    """An already-cleared local lane does not break canonical outage clearing."""
    runtime = make_runtime(tmp_path)
    source = source_for(runtime)
    source.commit_local_reply(UID, "5" * 32, ChatOriginal("assistant", RAW.text), "agent")
    source.clear_local_replies(UID)
    monkeypatch.setattr(ChatOriginalSource, "_connect", Mock(side_effect=WriterQuarantined()))
    assert await source_module.clear_local_replies(runtime, UID, ["5" * 32]) == ["5" * 32]
    assert source.read_local_replies(source.path, UID) == []


@pytest.mark.parametrize("origin", ["agent", "tool"])
@pytest.mark.asyncio
async def test_local_only_buffer_saves_full_reply_without_uploading(monkeypatch, origin):
    """A tool/mixed reply has one durable local identity and exact complete bytes."""
    remote, local = AsyncMock(), AsyncMock(return_value="5" * 32)
    monkeypatch.setattr(source_module, "save_original", remote)
    monkeypatch.setattr(source_module, "save_local_reply", local, raising=False)
    proof = {"recordId": "5" * 32, "originalTime": "123", "origin": origin}
    buffer = AssistantOriginalBuffer()
    for text in ("  first\r\n", "🙂e\u0301  "):
        buffer.append({"chat_original_source": proof, "text": text})
    await buffer.finish("Lan", proof["recordId"])
    local.assert_awaited_once_with("Lan", "  first\r\n🙂e\u0301  ", proof)
    remote.assert_not_awaited()
    assert buffer.records == {}


@pytest.mark.asyncio
async def test_local_only_buffer_discard_terminal_exclusion_and_retry(monkeypatch):
    """Temporary output is discarded, while a failed durable save can be retried."""
    local = AsyncMock(side_effect=[OSError("synthetic fault"), "5" * 32])
    monkeypatch.setattr(source_module, "save_local_reply", local, raising=False)
    proof = {"recordId": "5" * 32, "originalTime": "123", "origin": "agent"}
    buffer = AssistantOriginalBuffer()
    message = {"chat_original_source": proof, "text": RAW.text}
    buffer.append(message)
    buffer.discard()
    await buffer.finish("Lan")
    buffer.append(message)
    await buffer.finish("Lan", proof["recordId"], excluded=True)
    local.assert_not_awaited()
    buffer.append(message)
    await buffer.finish("Lan", proof["recordId"])
    assert proof["recordId"] in buffer.records
    await buffer.finish("Lan", proof["recordId"])
    assert local.await_count == 2
    assert buffer.records == {}


def test_source_commit_retries_keep_raw_bytes_and_fixed_locator(tmp_path, monkeypatch):
    """No matching by normalized text, timestamp or mutable projection is allowed."""
    source = ChatOriginalSource(tmp_path / "source.sqlite3")
    identity = "1" * 32
    patch_module_clock(monkeypatch, source_module, time_ns=lambda: 100_000_000)
    source.commit(OWNER, UID, identity, RAW)
    first, locator = source.pending(200)[0]
    assert first["queued_at_ms"] == 100
    patch_module_clock(monkeypatch, source_module, time_ns=lambda: 900_000_000)
    source.commit(OWNER, UID, identity, RAW)
    reopened = ChatOriginalSource(source.path)
    assert reopened.pending(200)[0] == (first, locator)
    assert ChatOriginal(**json.loads(first["original"])) == RAW
    assert locator.record_id == identity
    with pytest.raises(ValueError, match="chat_source_identity_conflict"):
        source.commit(OWNER, UID, identity, ChatOriginal("user", RAW.text.strip()))
    with pytest.raises(ValueError, match="chat_source_identity_conflict"):
        source.commit(OTHER, UID, identity, RAW)
    assert len(source.pending(200)) == 1


def test_concurrent_commits_keep_one_identity_and_atomic_failure_leaves_no_record(tmp_path):
    """SQLite serialization prevents duplicate raw/pending rows across writers."""
    source = ChatOriginalSource(tmp_path / 'source.sqlite3')
    identity = '8' * 32

    def commit_same_original(_index):
        """Construct independent handles to exercise real SQLite locking."""
        ChatOriginalSource(source.path).commit(OWNER, UID, identity, RAW)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(commit_same_original, range(24)))
    assert len(source.pending(200)) == 1
    with sqlite3.connect(source.path) as connection:
        connection.execute("CREATE TRIGGER fail_source BEFORE INSERT ON source_originals "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic rollback'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic rollback'):
        source.commit(OWNER, UID, '9' * 32, RAW)
    assert source.known_ids(OWNER, UID) == [identity]
    with sqlite3.connect(source.path) as connection:
        # Idempotent retries and rolled-back raw inserts never consume order.
        assert connection.execute("SELECT next_seq FROM source_origin_streams WHERE uid=?", (UID,)).fetchone() == (2,)


def test_source_sequence_exhaustion_keeps_stream_and_rejects_next_raw_commit(tmp_path):
    """The final safe sequence commits once without wrapping or creating a new stream."""
    source = ChatOriginalSource(tmp_path / "sources.sqlite3")
    source.commit_unbound(UID, "1" * 32, RAW)
    with sqlite3.connect(source.path) as connection:
        stream = connection.execute("SELECT stream_id FROM source_origin_streams WHERE uid=?", (UID,)).fetchone()[0]
        connection.execute("UPDATE source_origin_streams SET next_seq=? WHERE uid=?", (MAX_ORIGIN_SEQ, UID))
    source.commit(OWNER, UID, "2" * 32, RAW)
    assert source.pending(1)[0][0]["origin_seq"] == MAX_ORIGIN_SEQ
    with pytest.raises(ValueError, match="^source_sequence_exhausted$"):
        source.commit_unbound(UID, "3" * 32, RAW)
    with sqlite3.connect(source.path) as connection:
        assert connection.execute("SELECT stream_id,next_seq FROM source_origin_streams WHERE uid=?", (UID,)).fetchone() == (stream, MAX_ORIGIN_SEQ + 1)
        assert connection.execute("SELECT count(*) FROM source_unbound WHERE record_id=?", ("3" * 32,)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_restart_recovers_gap_and_capture_retry_does_not_duplicate(tmp_path, monkeypatch):
    """A crash after either commit retains one source ID and one outbox record."""
    runtime = make_runtime(tmp_path)
    source = source_for(runtime)
    identity = "2" * 32
    patch_module_clock(monkeypatch, source_module, time_ns=lambda: 100_000_000)
    source.commit(OWNER, UID, identity, RAW)
    patch_module_clock(monkeypatch, source_module, time_ns=lambda: 900_000_000)
    captured = source_module.ChatOriginalSource.captured

    def crash_after_capture(self, record_id):
        """Simulate process loss before the source pending marker is cleared."""
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(ChatOriginalSource, "captured", crash_after_capture)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        await recover_sources(runtime)
    assert len(runtime.ledger.pending(DOMAIN)) == 1
    monkeypatch.setattr(ChatOriginalSource, "captured", captured)
    reopened = make_runtime(tmp_path)
    assert await recover_sources(reopened) == 1
    assert await recover_sources(reopened) == 0
    payload = json.loads(reopened.ledger.pending(DOMAIN)[0][0])
    assert payload["messageId"] == identity
    assert payload["text"] == RAW.text
    assert payload["originalTime"] == RAW.original_time
    assert len(reopened.ledger.pending(DOMAIN)) == 1
    assert reopened.ledger.queue_state(DOMAIN)["oldestEnqueuedAt"] == 100
    assert reopened.ledger.queue_state(DOMAIN)["lastEnqueuedAt"] == 100


@pytest.mark.asyncio
async def test_legacy_source_recovery_keeps_unknown_queue_age(tmp_path):
    """A pre-clock WAL remains recoverable without pretending it arrived today."""
    runtime = make_runtime(tmp_path)
    path = tmp_path / "sources.sqlite3"
    payload = json.dumps(asdict(RAW), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    identity = "b" * 32
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE source_originals (record_id TEXT PRIMARY KEY, owner TEXT NOT NULL, uid TEXT NOT NULL, "
                     "original TEXT, original_hash TEXT NOT NULL, pending INTEGER NOT NULL DEFAULT 1, "
                     "deleted INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO source_originals VALUES (?,?,?,?,?,1,0)",
                     (identity, OWNER, UID, payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()))
    source = source_for(runtime)
    from services.chat_sync_writer import WriterQuarantined
    with pytest.raises(WriterQuarantined):
        source.pending(1)
    with sqlite3.connect(source.path) as conn:
        row = conn.execute("SELECT queued_at_ms,origin_seq,origin_stream_id FROM source_originals").fetchone()
        assert row == (None, None, None)
        assert conn.execute("SELECT count(*) FROM source_origin_streams").fetchone()[0] == 0
    with pytest.raises(WriterQuarantined):
        await recover_sources(runtime)
    # Unknown historical provenance is retained; a missing writer head never
    # self-authorizes legacy upload or backfills an arrival clock/origin.



@pytest.mark.asyncio
async def test_deleted_pending_and_captured_sources_are_purged_after_restart(tmp_path):
    """The deletion ledger wins even if its subsequent source cleanup never ran."""
    runtime = make_runtime(tmp_path)
    source = source_for(runtime)
    ids = ["3" * 32, "4" * 32]
    source.commit(OWNER, UID, ids[0], RAW)
    await recover_sources(runtime)
    source.commit(OWNER, UID, ids[1], RAW)
    runtime.ledger.queue_deletion(DOMAIN, ids)
    assert await recover_sources(make_runtime(tmp_path)) == 0
    with sqlite3.connect(source.path) as connection:
        rows = connection.execute("SELECT original,pending,deleted FROM source_originals").fetchall()
    assert rows == [(None, 0, 1), (None, 0, 1)]
    # A late producer retry cannot refill a tombstoned source with the old body.
    for identity in ids:
        source.commit(OWNER, UID, identity, RAW)
    assert source.pending(200) == []
    assert runtime.ledger.visible_originals(DOMAIN) == []


@pytest.mark.asyncio
async def test_recovery_rejects_body_corruption_before_ledger_registration(tmp_path):
    """Source identity proof never accepts a valid JSON body with changed bytes."""
    runtime = make_runtime(tmp_path)
    source = source_for(runtime)
    source.commit(OWNER, UID, 'e' * 32, RAW)
    with sqlite3.connect(source.path) as connection:
        connection.execute('UPDATE source_originals SET original=replace(original,?,?)', ('原文', '篡改'))
    with pytest.raises(ValueError, match='chat_source_integrity_error'):
        await recover_sources(runtime)
    assert runtime.ledger.pending(DOMAIN) == []
    assert len(source.pending(200)) == 1


@pytest.mark.asyncio
async def test_known_ids_include_all_pending_over_page_limit_and_owner_isolation(tmp_path):
    """Clear fixes its identity boundary before bounded recovery or upload occurs."""
    runtime = make_runtime(tmp_path)
    source = source_for(runtime)
    identities = [f"{index:032x}" for index in range(205)]
    for identity in identities:
        source.commit(OWNER, UID, identity, RAW)
    source.commit(OTHER, UID, "f" * 32, RAW)
    assert await known_source_ids(runtime, UID) == identities
    assert await recover_sources(runtime, max_records=2) == 2
    assert await known_source_ids(runtime, UID) == identities
    await purge_source_originals(runtime, UID, identities)
    assert [row["record_id"] for row, _ in source.pending(300)] == ["f" * 32]
    assert await recover_sources(runtime) == 0


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5])
@pytest.mark.asyncio
async def test_recovery_rejects_invalid_boundaries(tmp_path, maximum):
    """Invalid limits fail before source or network access."""
    with pytest.raises(ValueError, match="invalid_chat_source_recovery_limit"):
        await recover_sources(make_runtime(tmp_path), max_records=maximum)


@pytest.mark.asyncio
async def test_save_preserves_unbound_proof_without_upload_and_keeps_paused_owner(tmp_path, monkeypatch):
    """Raw originals survive before consent without guessing an account owner."""
    runtime = make_runtime(tmp_path, bound=False)
    manager = SimpleNamespace(aload_characters=AsyncMock(return_value={"猫娘": {
        "Lan": {"_reserved": {"character_uid": UID}},
    }}))
    monkeypatch.setattr("services.chat_sync_runtime.get_desktop_chat_sync_runtime", AsyncMock(return_value=runtime))
    unbound_id = await save_original("Lan", "user", RAW.text, config_manager=manager)
    assert len(unbound_id) == 32
    with sqlite3.connect(tmp_path / "sources.sqlite3") as connection:
        row = connection.execute("SELECT record_id,original FROM source_unbound").fetchone()
    assert row[0] == unbound_id and json.loads(row[1])["text"] == RAW.text
    assert source_for(runtime).pending(200) == []
    runtime.wake_scheduler.assert_not_called()
    runtime.state.prepare_binding(DOMAIN)
    runtime.state.pause(UID)
    assert await recover_sources(runtime) == 0
    identity = await save_original("Lan", "user", RAW.text, original_time=RAW.original_time, config_manager=manager)
    assert len(identity) == 32
    assert runtime.state.binding(UID)["enabled"] == 0
    payload = json.loads(runtime.ledger.pending(DOMAIN)[0][0])
    assert payload["ownerId"] == OWNER
    assert payload["messageId"] == identity
    assert payload["text"] == RAW.text
    assert payload["originalTime"] == RAW.original_time
    runtime.wake_scheduler.assert_called_once_with()


@pytest.mark.asyncio
async def test_save_keeps_pending_when_ledger_temporarily_fails(tmp_path, monkeypatch):
    """Local raw persistence does not depend on HTTP or a successful ledger pass."""
    runtime = make_runtime(tmp_path)
    manager = SimpleNamespace(aload_characters=AsyncMock(return_value={"猫娘": {
        "Lan": {"_reserved": {"character_uid": UID}},
    }}))
    monkeypatch.setattr("services.chat_sync_runtime.get_desktop_chat_sync_runtime", AsyncMock(return_value=runtime))
    capture = runtime.ledger.capture
    runtime.ledger.capture = Mock(side_effect=RuntimeError("synthetic unavailable"))
    identity = await save_original("Lan", "user", RAW.text, config_manager=manager)
    assert source_for(runtime).pending(1)[0][0]["record_id"] == identity
    runtime.wake_scheduler.assert_called_once_with()
    runtime.ledger.capture = capture
    assert await recover_sources(runtime) == 1


@pytest.mark.asyncio
async def test_save_does_not_wake_before_original_commit_succeeds(tmp_path, monkeypatch):
    """An unsuccessful raw commit creates no work for the automatic worker."""
    runtime = make_runtime(tmp_path)
    manager = SimpleNamespace(aload_characters=AsyncMock(return_value={"猫娘": {
        "Lan": {"_reserved": {"character_uid": UID}},
    }}))
    monkeypatch.setattr("services.chat_sync_runtime.get_desktop_chat_sync_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(ChatOriginalSource, "commit", Mock(side_effect=sqlite3.OperationalError("synthetic disk failure")))
    with pytest.raises(sqlite3.OperationalError, match="synthetic disk failure"):
        await save_original("Lan", "user", RAW.text, config_manager=manager)
    runtime.wake_scheduler.assert_not_called()
    assert source_for(runtime).pending(1) == []
    assert runtime.ledger.pending(DOMAIN) == []


@pytest.mark.asyncio
async def test_assistant_buffer_keeps_exact_chunks_and_independent_interleaved_ids(monkeypatch):
    """A late completion only seals its own message and leaves another reply intact."""
    save = AsyncMock()
    monkeypatch.setattr(source_module, "save_original", save)
    manager = SimpleNamespace()
    old = mark_turn_origin(manager, "old", "chat")
    newer = mark_turn_origin(manager, "new", "proactive")
    assert mark_turn_origin(manager, "old", "agent") == old
    buffer = AssistantOriginalBuffer()
    for proof, text in [(old, "  old\n"), (newer, "new"), (old, "🙂  ")]:
        buffer.append({"chat_original_source": proof, "text": text})
    await buffer.finish("Lan", old["recordId"])
    save.assert_awaited_once_with("Lan", "assistant", "  old\n🙂  ",
                                  record_id=old["recordId"], original_time=old["originalTime"])
    assert list(buffer.records) == [newer["recordId"]]
    await buffer.finish("Lan", newer["recordId"])
    assert save.await_count == 2


@pytest.mark.parametrize("origin", ["agent", "avatar", "tool", "mirror", None])
@pytest.mark.asyncio
async def test_explicit_nonchat_and_unknown_assistant_sources_are_not_uploaded(monkeypatch, origin):
    """Only ordinary/private output enters upload lanes; tools stay local."""
    save, local = AsyncMock(), AsyncMock()
    monkeypatch.setattr(source_module, "save_original", save)
    monkeypatch.setattr(source_module, "save_local_reply", local)
    buffer = AssistantOriginalBuffer()
    proof = {"recordId": "5" * 32, "originalTime": "123", "origin": origin}
    buffer.append({"chat_original_source": proof, "text": "hidden tool reply"})
    buffer.append({"text": "legacy projection"})
    await buffer.finish("Lan")
    save.assert_not_awaited()
    assert local.await_count == (1 if origin in ("agent", "tool") else 0)


@pytest.mark.asyncio
async def test_discarded_or_avatar_terminal_output_is_not_saved(monkeypatch):
    """Discard follows existing chat persistence and never keeps temporary tokens."""
    save = AsyncMock()
    monkeypatch.setattr(source_module, "save_original", save)
    proof = mark_turn_origin(SimpleNamespace(), "reply", "chat")
    buffer = AssistantOriginalBuffer()
    message = {"chat_original_source": proof, "text": "temporary"}
    buffer.append(message)
    buffer.discard()
    await buffer.finish("Lan")
    buffer.append(message)
    await buffer.finish("Lan", proof["recordId"], excluded=True)
    save.assert_not_awaited()
    assert buffer.records == {}


def test_source_diagnostic_records_first_fixed_origin_only(monkeypatch):
    """A later ordinary send cannot relabel an excluded callback reply."""
    trace = Mock(enabled=True)
    monkeypatch.setattr(source_module, "get_trace", lambda: trace)
    manager = SimpleNamespace()
    proof = mark_turn_origin(manager, "synthetic-turn", "agent")
    assert mark_turn_origin(manager, "synthetic-turn", "chat") == proof
    trace.emit.assert_called_once_with(
        "assistant_source_selected", message_id=proof["recordId"], source_origin="agent",
    )


@pytest.mark.asyncio
async def test_source_diagnostic_exclusion_is_once_per_reply_and_never_uploads(monkeypatch):
    """Actual collector exclusions identify one reply without logging every chunk."""
    trace, save = Mock(enabled=True), AsyncMock()
    monkeypatch.setattr(source_module, "get_trace", lambda: trace)
    monkeypatch.setattr(source_module, "save_original", save)
    monkeypatch.setattr(source_module, "save_local_reply", AsyncMock(return_value="d" * 32))
    proof = {"recordId": "d" * 32, "originalTime": "123", "origin": "agent"}
    buffer = AssistantOriginalBuffer()
    for _ in range(10):
        buffer.append({"chat_original_source": proof, "text": "private-original"})
    trace.emit.assert_called_once_with(
        "assistant_source_excluded", message_id=proof["recordId"], source_origin="agent",
        source_reason="nonchat_origin",
    )
    await buffer.finish("private-role", proof["recordId"])
    save.assert_not_awaited()
    trace.emit.assert_called_with(
        "assistant_source_finished", message_id=proof["recordId"], source_origin="agent",
        source_reason="local_saved", outcome="success",
    )
    buffer.append({"chat_original_source": proof, "text": "private-original"})
    assert sum(call.args[0] == "assistant_source_excluded" for call in trace.emit.call_args_list) == 2
    assert "private-" not in str(trace.emit.call_args_list)


@pytest.mark.asyncio
async def test_source_diagnostic_distinguishes_finish_and_preserves_retry(monkeypatch):
    """Optional evidence distinguishes missing, failed and accepted raw saves."""
    trace, save = Mock(enabled=True), AsyncMock(side_effect=OSError("private-error"))
    monkeypatch.setattr(source_module, "get_trace", lambda: trace)
    monkeypatch.setattr(source_module, "save_original", save)
    proof = {"recordId": "e" * 32, "originalTime": "123", "origin": "chat"}
    buffer = AssistantOriginalBuffer()
    buffer.append({"chat_original_source": proof, "text": "private-original"})
    await buffer.finish("private-role", proof["recordId"])
    assert proof["recordId"] in buffer.records
    trace.emit.assert_called_with(
        "assistant_source_finished", message_id=proof["recordId"], source_origin="chat",
        source_reason="save_failed", outcome="error",
    )
    save.side_effect = None
    save.return_value = None
    await buffer.finish("private-role", proof["recordId"])
    assert buffer.records == {}
    trace.emit.assert_called_with(
        "assistant_source_finished", message_id=proof["recordId"], source_origin="chat",
        source_reason="save_unavailable", outcome="error",
    )
    buffer.append({"chat_original_source": proof, "text": "private-original"})
    save.return_value = proof["recordId"]
    await buffer.finish("private-role", proof["recordId"])
    trace.emit.assert_called_with(
        "assistant_source_finished", message_id=proof["recordId"], source_origin="chat",
        source_reason="saved", outcome="success",
    )
    buffer.append({"chat_original_source": proof, "text": "private-original"})
    await buffer.finish("private-role", proof["recordId"], excluded=True)
    trace.emit.assert_called_with(
        "assistant_source_finished", message_id=proof["recordId"], source_origin="chat",
        source_reason="terminal_excluded", outcome="success",
    )
    assert save.await_count == 3
    assert "private-" not in str(trace.emit.call_args_list)


@pytest.mark.parametrize("kind,reason", [
    ("mirror", "mirror"), ("unknown", "unknown_origin"),
    ("invalid_id", "invalid_identity"), ("invalid_text", "invalid_text"),
])
def test_source_diagnostic_does_not_infer_unknown_or_mirror_sources(monkeypatch, kind, reason):
    """Diagnostic identities never come from text or arbitrary untrusted values."""
    trace = Mock(enabled=True)
    monkeypatch.setattr(source_module, "get_trace", lambda: trace)
    proof = {"recordId": "d" * 32, "originalTime": "123", "origin": "chat"}
    message = {"chat_original_source": proof, "text": "private-original"}
    if kind == "mirror":
        message["metadata"] = {"mirror": {"text": "private-mirror"}}
    elif kind == "unknown":
        message.pop("chat_original_source")
    elif kind == "invalid_id":
        proof["recordId"] = "private-id"
    else:
        message["text"] = {"body": "private-original"}
    buffer = AssistantOriginalBuffer()
    buffer.append(message)
    assert buffer.records == {}
    trace.emit.assert_called_once_with(
        "assistant_source_excluded", message_id=None if kind in ("unknown", "invalid_id") else "d" * 32,
        source_origin=None if kind == "unknown" else "chat", source_reason=reason,
    )
    assert "private-" not in str(trace.emit.call_args_list)


@pytest.mark.asyncio
async def test_disabled_or_failed_source_diagnostic_does_not_change_behavior(tmp_path, monkeypatch):
    """Unavailable observation files cannot change source choice or consume a save."""
    for path in (None, tmp_path / "absent-directory" / "trace.jsonl"):
        trace = ChatSyncTrace(path)
        save = AsyncMock(return_value="e" * 32)
        monkeypatch.setattr(source_module, "get_trace", lambda: trace)
        monkeypatch.setattr(source_module, "save_original", save)
        proof = mark_turn_origin(SimpleNamespace(), "synthetic-turn", "chat")
        buffer = AssistantOriginalBuffer()
        buffer.append({"chat_original_source": proof, "text": "private-original"})
        await buffer.finish("private-role", proof["recordId"])
        save.assert_awaited_once_with("private-role", "assistant", "private-original",
                                      record_id=proof["recordId"], original_time=proof["originalTime"])
        assert buffer.records == {}
        if path is None:
            assert trace._sequence == 0
        else:
            assert trace._sequence == 2
            assert not path.exists()


@pytest.mark.parametrize('composer_source', ['', 'react-chat-window', 'legacy-text-button'])
@pytest.mark.asyncio
async def test_real_turn_input_preserves_full_text_without_waiting_for_reply(monkeypatch, composer_source):
    """The real accepted input pipeline saves a standalone user and publishes its ID."""
    from tests.unit.test_core_game_route_memory_contract import _make_transcript_manager
    from main_logic.core.turn import TurnMixin

    manager = _make_transcript_manager()
    manager._config_manager = object()
    manager._active_text_request_id = "later-request"
    manager.websocket = SimpleNamespace(send_json=AsyncMock())
    save = AsyncMock(return_value="6" * 32)
    monkeypatch.setattr("main_logic.core.turn.save_original", save)
    assert await TurnMixin.handle_input_transcript(
        manager, RAW.text, is_voice_source=False,
        chat_original_text=RAW.text, chat_request_id="accepted-request",
        source=composer_source,
    )
    assert save.await_args.args == ("Lan", "user", RAW.text)
    assert save.await_args.kwargs["original_time"].isdigit()
    assert manager.sync_message_queue.messages[-1]["data"]["data"] == RAW.text.strip()
    manager.websocket.send_json.assert_awaited_once_with({
        "type": "chat_original_identity", "role": "user",
        "request_id": "accepted-request", "message_id": "6" * 32,
    })


@pytest.mark.asyncio
async def test_real_turn_final_asr_and_explicit_nonchat_source(monkeypatch):
    """Only admitted ordinary final ASR gets raw storage; synthetic input stays out."""
    from tests.unit.test_core_game_route_memory_contract import _make_transcript_manager
    from main_logic.core.turn import TurnMixin

    manager = _make_transcript_manager()
    manager._config_manager = object()
    manager._inject_pending_user_directives = AsyncMock()
    manager._dispatch_mini_game_invite_keyword = AsyncMock()
    manager._broadcast_voice_transcript_observed = AsyncMock()
    manager._fire_task = lambda coroutine: coroutine.close()
    save = AsyncMock(return_value="7" * 32)
    monkeypatch.setattr("main_logic.core.turn.save_original", save)
    assert await TurnMixin.handle_input_transcript(manager, "  最终转写内容。  ", source_game_route_identity=None)
    assert save.await_args.args == ("Lan", "user", "最终转写内容。")
    save.reset_mock()
    await TurnMixin.handle_input_transcript(manager, "avatar description", is_voice_source=False, source="avatar_drop")
    save.assert_not_awaited()
    await TurnMixin.handle_input_transcript(manager, "synthetic scaffold", is_voice_source=False, chat_input_source="avatar-drop")
    save.assert_not_awaited()
    assert 'source' not in manager.sync_message_queue.messages[-1]['data']


@pytest.mark.asyncio
async def test_real_output_and_late_turn_end_share_frozen_message_identity(monkeypatch):
    """Actual chunk publication carries one identity through an interleaved close."""
    from tests.unit.test_core_game_route_memory_contract import _make_manager
    from main_logic.core.turn import TurnMixin

    manager = _make_manager()
    save = AsyncMock()
    monkeypatch.setattr(source_module, "save_original", save)
    await TurnMixin.send_lanlan_response(manager, "  完整\n", turn_id="old-speech")
    manager.current_speech_id = "new-speech"
    await TurnMixin.send_lanlan_response(manager, "新回复", turn_id="new-speech")
    await TurnMixin.send_lanlan_response(manager, "🙂  ", turn_id="old-speech")
    messages = [item["data"] for item in manager.sync_message_queue.messages]
    assert messages[0]["chat_original_id"] == messages[2]["chat_original_id"]
    assert messages[1]["chat_original_id"] != messages[0]["chat_original_id"]
    reply = SimpleNamespace(speech_id="old-speech", meta=None, turn_ended=False)
    terminal = TurnMixin._queue_turn_end(manager, "old-request", reply_turn=reply)
    assert terminal["chat_original_id"] == messages[0]["chat_original_id"]
    buffer = AssistantOriginalBuffer()
    for message in messages:
        buffer.append(message)
    await buffer.finish("Lan", terminal["chat_original_id"])
    assert save.await_args.args == ("Lan", "assistant", "  完整\n🙂  ")
    assert list(buffer.records) == [messages[1]["chat_original_id"]]


@pytest.mark.asyncio
async def test_actual_connector_saves_exact_reply_and_keeps_memory_projection_separate(monkeypatch):
    """Run the real consumer with fake memory I/O, never a listener or real account."""
    from main_logic import cross_server

    save = AsyncMock()
    memory = AsyncMock(return_value=(True, "", {}))
    monkeypatch.setattr(source_module, "save_original", save)
    monkeypatch.setattr(cross_server, "_post_memory_server", memory)
    monkeypatch.setattr(cross_server, "_publish_analyze_request_with_fallback", AsyncMock(return_value=True))
    queue = asyncio.Queue()
    ordinary = mark_turn_origin(SimpleNamespace(), "ordinary", "chat")
    excluded = mark_turn_origin(SimpleNamespace(), "avatar", "avatar")
    completed = asyncio.get_running_loop().create_future()
    for proof, text in [(ordinary, "  完整\n"), (ordinary, "🙂  ")]:
        queue.put_nowait({"type": "json", "data": {
            "type": "gemini_response", "text": text, "chat_original_source": proof,
            "chat_original_id": proof["recordId"],
        }})
    queue.put_nowait({"type": "system", "data": "turn end", "chat_original_id": ordinary["recordId"]})
    queue.put_nowait({"type": "json", "data": {
        "type": "gemini_response", "text": "mirror with memory enabled", "chat_original_source": ordinary,
        "metadata": {"mirror": {"event": {"hasUserText": True}}},
    }})
    queue.put_nowait({"type": "json", "data": {
        "type": "gemini_response", "text": "avatar tool reply", "chat_original_source": excluded,
        "chat_original_id": excluded["recordId"],
    }})
    queue.put_nowait({"type": "system", "data": "session end", "_memory_settlement_done": completed})
    connector = asyncio.create_task(cross_server.run_sync_connector(queue, "Lan", config={"monitor": False, "bullet": False}))
    try:
        await asyncio.wait_for(completed, 1)
    finally:
        connector.cancel()
        await asyncio.gather(connector, return_exceptions=True)
    save.assert_awaited_once_with("Lan", "assistant", "  完整\n🙂  ",
                                  record_id=ordinary["recordId"], original_time=ordinary["originalTime"])
    assert memory.await_count >= 1
    projected = str(memory.await_args.args[2])
    assert "avatar tool reply" in projected
    assert "🙂" not in projected
