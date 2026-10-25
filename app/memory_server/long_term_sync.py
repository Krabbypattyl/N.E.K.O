"""Memory-owner capture and replay of completed learned-memory snapshots."""

import asyncio
from contextlib import asynccontextmanager, closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from memory.long_term_sync import DesktopLongTermAdapter, FILES, export_document, import_document, read_views
from memory.recent import _await_recent_mutation_to_completion as _complete_file_commit
from services.chat_sync.long_term_protocol import head
from services.chat_sync.long_term_events import changed
from services.chat_sync.writer import ChatSyncWriter, registered_writer, store_connection
from utils.config_manager.reserved_schema import get_character_uid
from utils.file_utils import atomic_write_json, atomic_write_text
from utils.cloudsave_runtime import assert_cloudsave_writable

from . import runtime
from ._shared import logger, validate_lanlan_name


_EVENT = 'memory.long_term_replaced'
_capture_tasks = {}
_capture_requests = {}


def _authority():
    config = runtime._config_manager
    if not getattr(config, 'app_docs_dir', None) or not getattr(config, 'local_state_dir', None):
        return None
    directory = Path(config.app_docs_dir) / 'chat_sync'
    witness = Path(config.local_state_dir) / 'chat_sync_writer.json'
    if not (directory / 'state.sqlite3').is_file() or not witness.is_file():
        return None
    return registered_writer(directory) or ChatSyncWriter(directory, witness)


def _uid(name):
    profile = runtime._config_manager.load_characters().get('猫娘', {}).get(name)
    return get_character_uid(profile) if isinstance(profile, dict) else None


def _binding(uid):
    from services.chat_sync.runtime import _BUILTIN_SERVICE_URL

    writer = _authority()
    if writer is None:
        return None
    with closing(store_connection(writer.directory / 'state.sqlite3')) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM sync_bindings WHERE character_uid=?', (uid,)).fetchone()
        service = conn.execute('SELECT service_url FROM sync_settings WHERE singleton=1').fetchone()
    service_url = (service[0] or _BUILTIN_SERVICE_URL) if service else None
    return {**dict(row), 'service_url': service_url} if row and row['enabled'] and row['binding_id'] and service_url else None


@asynccontextmanager
async def _views_locked(name):
    admission = runtime._begin_character_request(name)
    if admission is None:
        raise ValueError('character_release_in_progress')
    try:
        async with runtime._get_settle_lock(name), runtime._reload_lock:
            async with runtime.fact_store._get_persist_alock(name), runtime.reflection_engine._get_alock(name), runtime.persona_manager._get_alock(name):
                yield
    finally:
        runtime._end_character_request(name, admission)


def _capture(name, force):
    writer = _authority()
    uid = _uid(name)
    if writer is None or uid is None:
        return False
    with writer.locked():
        binding = _binding(uid)
        if binding is None:
            return False
        epoch = writer.epoch()
        adapter = DesktopLongTermAdapter(runtime._config_manager)
        previous = adapter.read_local(binding, epoch)
        with runtime.fact_store._get_lock(name):
            views = read_views(Path(runtime._config_manager.memory_dir) / name)
        completed = max(time.time_ns() // 1_000_000, (previous['document']['completedAt'] if previous else 0) + 1)
        document = export_document(views, completed)
        if not force and ((not previous and not document['entries']) or (previous and previous['nativeEntries'] == document['entries'])):
            return False
        # The name-to-UID mapping and current grant are still protected at publication.
        if _uid(name) != uid or _binding(uid) != binding:
            return False
        try:
            atomic_write_json(adapter.local_path(binding), {
                'ownerId': binding['owner_id'], 'characterSyncId': uid, 'serviceUrl': binding['service_url'],
                'epoch': list(epoch), 'operationId': uuid4().hex, 'document': document, 'nativeEntries': document['entries'],
            }, ensure_ascii=False)
        except OSError as error:
            raise ValueError('long_term_memory_save_failed') from error
    changed()
    return True


def queue_capture(name, *, force=False):
    """Queue after owner locks are released; never await network from organization."""
    _capture_requests[name] = force or _capture_requests.get(name, False)
    if name in _capture_tasks:
        return

    async def run():
        try:
            while name in _capture_requests:
                await asyncio.sleep(0.2)
                requested = _capture_requests.pop(name)
                try:
                    async with _views_locked(name):
                        await _complete_file_commit(_capture, name, requested)
                except Exception:
                    logger.warning('[LongTermSync] local snapshot staging deferred')
        finally:
            _capture_tasks.pop(name, None)
    _capture_tasks[name] = runtime._spawn_background_task(run())


def _write_snapshot(name, payload):
    writer = _authority()
    if writer is None:
        return False
    binding, epoch = payload['binding'], tuple(payload['epoch'])
    uid = binding['character_uid']
    remote = head(payload['head'], binding['owner_id'], uid)
    if remote.get('document') is None:
        raise ValueError('invalid_long_term_memory')
    with writer.locked():
        try:
            writer.require_epoch(epoch)
        except ValueError:
            return False
        if _uid(name) != uid or _binding(uid) != binding:
            return False
        adapter = DesktopLongTermAdapter(runtime._config_manager)
        local = adapter.read_local(binding, epoch)
        if local and local['document']['completedAt'] > remote['completedAt']:
            return False
        operation = f'download:{remote["revision"]}'
        if local and local['operationId'] == operation and local['document'] == remote['document']:
            return True
        directory = Path(runtime._config_manager.memory_dir) / name
        assert_cloudsave_writable(runtime._config_manager, operation='sync', target=f'memory/{name}')
        with runtime.fact_store._get_lock(name):
            views = import_document(remote['document'], read_views(directory))
            before = {directory / filename: (directory / filename).read_text(encoding='utf-8')
                      if (directory / filename).is_file() else None for filename in FILES.values()}
            try:
                for key, filename in FILES.items():
                    atomic_write_json(directory / filename, views[key], ensure_ascii=False, indent=2)
                atomic_write_json(adapter.local_path(binding), {
                    'ownerId': binding['owner_id'], 'characterSyncId': uid, 'serviceUrl': binding['service_url'],
                    'epoch': list(epoch), 'operationId': operation, 'document': remote['document'],
                    'nativeEntries': export_document(views, remote['completedAt'])['entries'],
                }, ensure_ascii=False)
            except BaseException as error:
                # Restore exact prior files; the appended event/inbox retains the retry.
                for path, original in before.items():
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write_text(path, original)
                if isinstance(error, OSError):
                    raise ValueError('long_term_memory_save_failed') from error
                raise
            finally:
                runtime.fact_store._facts.pop(name, None)
                runtime.persona_manager._personas.pop(name, None)
        return True


def register_replay(reconciler, persona_manager=None, fact_store=None):
    def replay(name, payload):
        result = _write_snapshot(name, payload)
        if persona_manager is not None:
            persona_manager._personas.pop(name, None)
        if fact_store is not None:
            fact_store._facts.pop(name, None)
        return result
    reconciler.register(_EVENT, replay)


async def consume(uid):
    adapter = DesktopLongTermAdapter(runtime._config_manager)
    path = adapter.incoming_path(uid)
    if not path.is_file():
        return False
    if _authority() is None:
        return False
    data = await runtime._config_manager.aload_characters()
    name = next((name for name, profile in data.get('猫娘', {}).items()
                 if isinstance(profile, dict) and get_character_uid(profile) == uid), None)
    if name is None:
        return False
    name = validate_lanlan_name(name)
    if name in _capture_tasks or name in _capture_requests:
        return False
    from . import review
    if any(name in tasks and not tasks[name].done() for tasks in (review.correction_tasks, review.compress_backup_tasks)):
        return False
    async with _views_locked(name):
        await runtime.reconciler.areconcile(name)
        def commit():
            payload = json.loads(path.read_text(encoding='utf-8'))
            applied = [False]
            def save(_name, frozen):
                applied[0] = _write_snapshot(_name, frozen)
            runtime.event_log.record_and_save(name, _EVENT, payload,
                sync_load_view=lambda _: deepcopy(payload), sync_mutate_view=lambda _: None, sync_save_view=save)
            # Never delete a replacement inbox installed while this event was committing.
            writer = _authority()
            if writer is not None:
                with writer.locked():
                    if path.is_file() and json.loads(path.read_text(encoding='utf-8')) == payload:
                        path.unlink()
            return applied[0]
        return await _complete_file_commit(commit)


async def consume_for_name(name):
    try:
        if not getattr(runtime._config_manager, 'app_docs_dir', None):
            return False
        uid = _uid(name)
        if uid and DesktopLongTermAdapter(runtime._config_manager).incoming_path(uid).is_file():
            return await consume(uid)
    except Exception:
        logger.warning('[LongTermSync] incoming snapshot deferred')
    return False


async def close():
    tasks = list(_capture_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _capture_requests.clear()
