"""Cloud account choices and retirement use disposable identities and local-only stores."""

import copy
import json
from contextlib import closing

import httpx
import pytest

from tests.chat_sync_writer_fixture import isolated_chat_sync_writer  # noqa: F401
from tests.unit.test_character_profile_sync import Config, DesktopProfileAdapter, ProfileService
from tests.unit.test_chat_sync_runtime import OWNER, OTHER_UID, UID, setup_runtime


class ManagedService(ProfileService):
    """Model the fixed account metadata contract without contacting a real account."""

    def __init__(self):
        super().__init__()
        self.roles = {UID: 'Synthetic', OTHER_UID: 'Other'}
        self.policy = {'ownerId': OWNER, 'revision': 1, 'characterLimit': 3,
                       'characterCount': 2, 'requiresSelection': False,
                       'selectedCharacterSyncIds': [UID, OTHER_UID]}
        self.retired = set()
        self.delete_receipts = {}
        self.lose_delete_response = False
        self.selection_receipts = {}
        self.lose_selection_response = False

    async def request(self, request):
        if request.url.path.endswith('/character-policy'):
            self.requests.append(request)
            if request.method == 'PUT':
                body = json.loads(request.content)
                receipt = self.selection_receipts.get(body['operationId'])
                if receipt is None:
                    if body['baseRevision'] != self.policy['revision']:
                        return httpx.Response(409, json={'detail': 'character_policy_changed'})
                    selected = body['selectedCharacterSyncIds']
                    removed = set(self.roles) - set(selected)
                    self.retired.update(removed)
                    self.roles = {uid: name for uid, name in self.roles.items() if uid in selected}
                    self.policy = {**self.policy, 'revision': self.policy['revision'] + 1, 'requiresSelection': False,
                                   'characterCount': len(selected), 'selectedCharacterSyncIds': selected}
                    receipt = {**self.policy, 'operationId': body['operationId'], 'deletedCount': len(removed)}
                    self.selection_receipts[body['operationId']] = receipt
                if self.lose_selection_response:
                    self.lose_selection_response = False
                    raise httpx.ReadError('synthetic lost selection response')
                return httpx.Response(200, json=receipt)
            return httpx.Response(200, json=self.policy)
        if request.url.path.endswith('/characters'):
            self.requests.append(request)
            return httpx.Response(200, json={'characters': [
                {'characterSyncId': uid, 'displayName': name} for uid, name in sorted(self.roles.items())],
                'nextCursor': None})
        if request.method == 'DELETE':
            self.requests.append(request)
            uid = request.url.path.rsplit('/', 1)[-1]
            body = json.loads(request.content)
            receipt = self.delete_receipts.get(body['operationId'])
            if receipt is None:
                if body['baseRevision'] != self.policy['revision']:
                    return httpx.Response(409, json={'detail': 'character_policy_changed'})
                self.roles.pop(uid)
                self.retired.add(uid)
                self.policy = {**self.policy, 'revision': self.policy['revision'] + 1,
                               'characterCount': len(self.roles),
                               'selectedCharacterSyncIds': [value for value in self.policy['selectedCharacterSyncIds'] if value != uid]}
                receipt = {'ownerId': OWNER, 'characterSyncId': uid, 'operationId': body['operationId'],
                           'revision': self.policy['revision'], 'deleted': True}
                self.delete_receipts[body['operationId']] = receipt
            if self.lose_delete_response:
                self.lose_delete_response = False
                raise httpx.ReadError('synthetic lost delete response')
            return httpx.Response(200, json=receipt)
        if any('/characters/' + uid in request.url.path for uid in self.retired):
            self.requests.append(request)
            return httpx.Response(410, json={'detail': 'character_deleted'})
        if request.method == 'PUT' and request.url.path.rsplit('/', 1)[-1] not in self.roles and '/characters/' in request.url.path and not request.url.path.endswith('/profile'):
            self.requests.append(request)
            if len(self.roles) >= 3:
                return httpx.Response(409, json={'detail': 'character_quota_exceeded'})
        return await super().request(request)


@pytest.mark.asyncio
async def test_selection_required_blocks_all_grants_without_altering_local_cards(tmp_path):
    service = ManagedService()
    service.roles.update({'c' * 32: 'Third', 'd' * 32: 'Fourth'})
    service.policy.update(characterCount=4, requiresSelection=True, selectedCharacterSyncIds=[])
    runtime, _, _ = setup_runtime(tmp_path, service)
    config = Config()
    runtime.profiles.adapter = DesktopProfileAdapter(config)
    before = copy.deepcopy(config.data)
    try:
        await runtime.configure('https://sync.test')
        await runtime.save_character_sync_choice(UID, 'Synthetic', True)
        with pytest.raises(ValueError, match='^character_selection_required$'):
            await runtime.run_character_sync_choice(UID)
        assert config.data == before and not config.saves
        assert not any(request.method == 'PUT' for request in service.requests)
        assert (await runtime.status())['characterPolicy']['requiresSelection']
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_cloud_delete_response_loss_reuses_fixed_request_and_preserves_local_history(tmp_path):
    from services.chat_sync_ledger import ChatDomain, ChatOriginal, SourceLocator
    runtime, service, _ = setup_runtime(tmp_path, ManagedService())
    runtime.profiles.adapter = DesktopProfileAdapter(Config())
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        domain = ChatDomain(OWNER, UID)
        runtime.ledger.capture(domain, SourceLocator('synthetic-original', 'generation', 'e' * 32), ChatOriginal('user', 'kept locally'))
        before = await runtime.history(UID)
        policy = await runtime.cloud.refresh(force=True)
        service.lose_delete_response = True
        with pytest.raises(ValueError, match='^chat_sync_unavailable$'):
            await runtime.cloud.delete(UID, policy['revision'])
        assert await runtime.history(UID) == before
        assert runtime.cloud.local_error(OWNER, UID) == 'character_delete_pending'
        await runtime.cloud.delete(UID, policy['revision'])
        attempts = [request for request in service.requests if request.method == 'DELETE']
        assert len(attempts) == 2 and attempts[0].content == attempts[1].content
        assert runtime.cloud.local_error(OWNER, UID) == 'character_deleted'
        assert not runtime.state.binding(UID)['enabled']
        assert await runtime.history(UID) == before
        with pytest.raises(ValueError, match='^character_deleted$'):
            await runtime.enable(UID, 'Synthetic')
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_inconsistent_directory_snapshot_cannot_retire_a_local_role(tmp_path):
    runtime, service, _ = setup_runtime(tmp_path, ManagedService())
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        service.roles.pop(UID)
        service.policy['selectedCharacterSyncIds'] = [OTHER_UID]
        # Count still advertises two roles: an incomplete directory is not deletion proof.
        with pytest.raises(ValueError, match='^invalid_character_policy_response$'):
            await runtime.cloud.refresh(force=True)
        assert not runtime.cloud.retired(OWNER, UID)
        assert runtime.state.binding(UID)['enabled']
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_fixed_selection_retry_does_not_delete_a_later_cloud_character(tmp_path):
    runtime, service, _ = setup_runtime(tmp_path, ManagedService())
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        service.lose_selection_response = True
        with pytest.raises(ValueError, match='chat_sync_unavailable'):
            await runtime.cloud.select(1, [UID])
        original = runtime.cloud.pending_selection(OWNER)
        assert original['baseRevision'] == 1 and original['selectedCharacterSyncIds'] == [UID]
        service.roles['f' * 32] = 'Later cloud role'
        service.policy.update(revision=3, characterCount=2, selectedCharacterSyncIds=[UID, 'f' * 32])
        with pytest.raises(ValueError, match='character_selection_pending'):
            await runtime.cloud.select(3, [])
        await runtime.cloud.select(1, [UID])
        attempts = [request for request in service.requests if request.method == 'PUT' and request.url.path.endswith('/character-policy')]
        assert len(attempts) == 2 and attempts[0].content == attempts[1].content
        assert 'f' * 32 in service.roles
        assert runtime.cloud.pending_selection(OWNER) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_scheduler_retries_approved_delete_with_all_local_bindings_paused(tmp_path):
    from tests.unit.test_chat_sync_runtime import finish_automatic_flights
    runtime, service, _ = setup_runtime(tmp_path, ManagedService())
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        await runtime.pause(UID)
        service.lose_delete_response = True
        with pytest.raises(ValueError, match='chat_sync_unavailable'):
            await runtime.cloud.delete(UID, 1)
        runtime.test_clock[0] += 30_001
        await runtime.scheduler_tick()
        await finish_automatic_flights(runtime)
        assert len([request for request in service.requests if request.method == 'DELETE']) == 2
        assert runtime.cloud.retired(OWNER, UID)
        assert not runtime.cloud.has_pending_work()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_rebind_config_crash_then_explicit_off_cannot_restore_previous_on(tmp_path):
    from services.chat_sync_ledger import ChatOriginal
    from services.chat_sync_source import ChatOriginalSource
    from services.chat_sync_writer import WriterQuarantined
    from utils.config_manager.reserved_schema import get_character_uid
    runtime, service, _ = setup_runtime(tmp_path, ManagedService())
    config = Config()
    runtime.profiles.adapter = DesktopProfileAdapter(config)
    source = ChatOriginalSource(runtime.state.path.parent / 'sources.sqlite3')
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        source.commit(OWNER, UID, 'e' * 32, ChatOriginal('user', 'Local raw original'))
        await runtime._recover_originals()
        await runtime.cloud.delete(UID, 1)
        saved = config.save_characters
        def disk_failure(_data):
            raise OSError('synthetic config failure')
        config.save_characters = disk_failure
        with pytest.raises(WriterQuarantined):
            await runtime.save_character_sync_choice(UID, 'Synthetic', True)
        await runtime.close()
        config.save_characters = saved
        runtime, _, _ = setup_runtime(tmp_path, service)
        runtime.profiles.adapter = DesktopProfileAdapter(config)
        intent = runtime.cloud.reassignment(UID)
        new_uid = intent['new_uid']
        assert source.reassignment(UID)['newUid'] == new_uid
        assert get_character_uid(config.data['猫娘']['Synthetic']) == UID
        await runtime.save_character_sync_choice(UID, 'Synthetic', False)
        assert runtime.cloud.reassignment(UID)['request']['cancelled']
        await runtime.cloud.resume_rebinds()
        assert get_character_uid(config.data['猫娘']['Synthetic']) == new_uid
        assert runtime.state.choices()[new_uid] is False
        assert runtime.state.new_role(new_uid) is None
        assert [(record['messageId'], record['text']) for record in await runtime.history(new_uid)] == [('e' * 32, 'Local raw original')]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_rebind_mapping_prefers_exact_predecessor_after_two_retirements(tmp_path):
    runtime, _, _ = setup_runtime(tmp_path, ManagedService())
    try:
        runtime.cloud._save_rebind(UID, OTHER_UID, OWNER, {})
        runtime.cloud._complete_rebind(UID)
        runtime.cloud._save_rebind(OTHER_UID, 'f' * 32, OWNER, {})
        runtime.cloud._complete_rebind(OTHER_UID)
        intent = runtime.cloud.reassignment(OTHER_UID)
        assert (intent['old_uid'], intent['new_uid']) == (OTHER_UID, 'f' * 32)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_rebind_quota_failure_keeps_new_uid_history_and_stops_automatic_registration_retry(tmp_path):
    from services.chat_sync_ledger import ChatOriginal
    from services.chat_sync_source import ChatOriginalSource
    from utils.config_manager.reserved_schema import get_character_uid
    runtime, service, _ = setup_runtime(tmp_path, ManagedService())
    config = Config()
    runtime.profiles.adapter = DesktopProfileAdapter(config)
    source = ChatOriginalSource(runtime.state.path.parent / 'sources.sqlite3')
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        source.commit(OWNER, UID, 'e' * 32, ChatOriginal('user', 'Raw remains local'))
        await runtime._recover_originals()
        await runtime.cloud.delete(UID, 1)
        service.roles.update({'c' * 32: 'Third', 'd' * 32: 'Fourth'})
        service.policy.update(revision=3, characterCount=3, selectedCharacterSyncIds=list(service.roles))
        await runtime.save_character_sync_choice(UID, 'Synthetic', True)
        new_uid = get_character_uid(config.data['猫娘']['Synthetic'])
        assert new_uid != UID
        with pytest.raises(ValueError, match='character_quota_exceeded'):
            await runtime.run_character_sync_choice(new_uid)
        assert runtime.state.new_role_errors()[new_uid] == 'character_quota_exceeded'
        assert [record['text'] for record in await runtime.history(new_uid)] == ['Raw remains local']
        attempts = len([request for request in service.requests if request.method == 'PUT'])
        runtime.test_clock[0] += 120_001
        await runtime._start_new_role_sync('synthetic-presence')
        assert len([request for request in service.requests if request.method == 'PUT']) == attempts
        await runtime.save_character_sync_choice(new_uid, 'Synthetic', True)
        assert new_uid not in runtime.state.new_role_errors()
        assert runtime.state.new_role(new_uid)['expectedOwner'] == OWNER
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['select', 'delete'])
async def test_management_stale_account_review_cannot_delete_in_current_account(tmp_path, kind):
    from tests.unit.test_chat_sync_runtime import OTHER_OWNER
    runtime, service, _ = setup_runtime(tmp_path, ManagedService())
    try:
        await runtime.configure('https://sync.test')
        before = copy.deepcopy(service.roles)
        with pytest.raises(ValueError, match='^chat_sync_account_mismatch$'):
            if kind == 'select':
                await runtime.cloud.select(1, [UID], expected_owner=OTHER_OWNER)
            else:
                await runtime.cloud.delete(UID, 1, expected_owner=OTHER_OWNER)
        assert service.roles == before
        assert not any(request.method in ('DELETE', 'PUT') for request in service.requests)
        assert not runtime.cloud.has_pending_work()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_cas_rejected_delete_does_not_keep_role_sync_blocked(tmp_path):
    class ConcurrentService(ManagedService):
        async def request(self, request):
            if request.method == 'DELETE':
                self.policy['revision'] += 1
            return await super().request(request)

    runtime, service, _ = setup_runtime(tmp_path, ConcurrentService())
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        with pytest.raises(ValueError, match='^character_policy_changed$'):
            await runtime.cloud.delete(UID, 1)
        operation = runtime.cloud._operation(OWNER, 'delete', UID)
        assert operation['status'] == 'blocked' and operation['error'] == 'character_policy_changed'
        await runtime.cloud.refresh(force=True)
        assert runtime.cloud.local_error(OWNER, UID) is None
        assert runtime.state.binding(UID)['enabled'] and UID in service.roles
        assert runtime.cloud.local_error(OWNER, OTHER_UID) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['invalid_character_policy_response', 'unauthorized', 'forbidden', 'not_found'])
async def test_ambiguous_or_authenticated_delete_failure_still_blocks_role(tmp_path, error):
    runtime, _, _ = setup_runtime(tmp_path, ManagedService())
    try:
        await runtime.configure('https://sync.test')
        await runtime.cloud.refresh(force=True)
        runtime.cloud._save_operation(OWNER, 'delete', UID, {'epoch': list(runtime.writer.epoch())})
        runtime.cloud._finish_operation(OWNER, 'delete', UID, 'blocked', error)
        assert runtime.cloud.local_error(OWNER, UID) == 'character_delete_pending'
        assert runtime.cloud.local_error(OWNER, OTHER_UID) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['delete', 'select'])
async def test_old_writer_operation_is_not_automatic_work_after_recovery(tmp_path, kind):
    runtime, service, _ = setup_runtime(tmp_path, ManagedService())
    try:
        await runtime.configure('https://sync.test')
        policy = await runtime.cloud.refresh(force=True)
        body = {'baseRevision': policy['revision'], 'operationId': 'e' * 32}
        if kind == 'select':
            body['selectedCharacterSyncIds'] = [UID]
        target = UID if kind == 'delete' else ''
        request = {'body': body, 'epoch': list(runtime.writer.epoch()),
                   'policy': policy, 'roles': runtime.cloud.directory(OWNER)}
        runtime.cloud._save_operation(OWNER, kind, target, request)
        runtime.writer.recover(lambda: None)
        assert tuple(request['epoch']) != runtime.writer.epoch()
        service.requests.clear()
        assert not runtime.cloud.has_pending_work()
        assert not runtime.cloud.due(runtime.cloud.presence)
        await runtime.scheduler_tick()
        assert service.requests == []
        await runtime.cloud.tick(runtime.cloud.presence)
        # A deliberately invoked tick may revalidate identity after recovery,
        # but never sends an old destructive approval. Automatic ticks above
        # have no work and perform no identity polling at all.
        assert not any(item.method in {'PUT', 'DELETE'} for item in service.requests)
        assert runtime.cloud._operation(OWNER, kind, target)['request'] == request
        assert not runtime.cloud.retired(OWNER, UID) and UID in service.roles
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_old_writer_rebind_cannot_stop_other_roles_scheduler(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from services.character_profile_protocol import encode_character_profile
    from services.chat_sync_ledger import ChatDomain
    from tests.unit.test_chat_sync_runtime import BINDING
    runtime, service, _ = setup_runtime(tmp_path, ManagedService())
    config = Config()
    runtime.profiles.adapter = DesktopProfileAdapter(config)
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        await runtime.cloud.delete(UID, 1)
        request = {'epoch': list(runtime.writer.epoch()), 'name': 'Synthetic', 'includeHistory': True,
                   'canonical': encode_character_profile(await runtime.profiles.adapter.read(UID)).decode()}
        runtime.cloud._save_rebind(UID, 'f' * 32, OWNER, request)
        runtime.writer.recover(lambda: None)
        # The fake service registers only its primary UID. Install the other
        # synthetic already-verified binding directly in the real state store.
        prepared = runtime.state.prepare_binding(ChatDomain(OWNER, OTHER_UID))
        runtime.state.activate(prepared, BINDING)
        before = copy.deepcopy(config.data)
        continued = AsyncMock()
        monkeypatch.setattr(runtime, '_recover_originals', continued)
        monkeypatch.setattr(runtime, '_manage_notifications', AsyncMock())
        monkeypatch.setattr(runtime, '_automatic_channel', AsyncMock())
        monkeypatch.setattr(runtime, '_automatic_profiles', AsyncMock())
        monkeypatch.setattr(runtime, '_current_character_uid', AsyncMock(return_value=OTHER_UID))
        await runtime.scheduler_tick()
        continued.assert_awaited_once()
        assert config.data == before and runtime.cloud.reassignment(UID)['request'] == request
        assert runtime.state.new_role('f' * 32) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revision', 'order', 'cached_order'])
async def test_policy_concurrency_and_selection_order_do_not_permanently_block_owner(tmp_path, change):
    class ConcurrentPolicy(ManagedService):
        reads = 0

        async def request(self, request):
            if request.method == 'GET' and request.url.path.endswith('/character-policy'):
                self.reads += 1
                if self.reads == 2 and change == 'revision':
                    self.policy['revision'] += 1
                if self.reads == 2 and change == 'order':
                    return httpx.Response(200, json={**self.policy,
                        'selectedCharacterSyncIds': list(reversed(self.policy['selectedCharacterSyncIds']))})
            return await super().request(request)

    runtime, service, _ = setup_runtime(tmp_path, ConcurrentPolicy())
    try:
        await runtime.configure('https://sync.test')
        if change == 'cached_order':
            await runtime.cloud.refresh(force=True)
            cached = runtime.cloud.cached(owner=OWNER)
            cached['selectedCharacterSyncIds'].reverse()
            with closing(runtime.cloud._connect()) as conn, conn:
                conn.execute('UPDATE sync_cloud_policies SET snapshot=? WHERE owner_id=?', (json.dumps(cached), OWNER))
        if change == 'revision':
            with pytest.raises(ValueError, match='^character_policy_changed$'):
                await runtime.cloud.refresh(force=True)
        else:
            await runtime.cloud.refresh(force=True)
        assert runtime.cloud.blocked_owner is None and runtime.cloud.blocked_presence is None
        await runtime.cloud.refresh(force=True)
        assert runtime.cloud.cached(owner=OWNER)['revision'] == service.policy['revision']
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('disk_failure', [False, True])
async def test_rebind_retry_rejection_is_local_but_writer_failure_stays_closed(tmp_path, monkeypatch, disk_failure):
    from services.chat_sync_writer import WriterQuarantined
    runtime, _, _ = setup_runtime(tmp_path, ManagedService())
    runtime.profiles.adapter = DesktopProfileAdapter(Config())
    try:
        request = {'epoch': list(runtime.writer.epoch())}
        runtime.cloud._save_rebind(UID, 'e' * 32, OWNER, request)
        runtime.cloud._save_rebind(OTHER_UID, 'f' * 32, OWNER, request)
        seen = []

        async def advance(intent):
            seen.append(intent['old_uid'])
            if intent['old_uid'] == UID:
                raise OSError('synthetic per-role failure')

        saved = runtime.cloud._save_rebind

        def retain(old_uid, *args):
            if old_uid == UID:
                if disk_failure:
                    raise OSError('synthetic disk failure')
                raise ValueError('synthetic per-role retry rejection')
            return saved(old_uid, *args)

        monkeypatch.setattr(runtime.cloud, '_advance_rebind', advance)
        monkeypatch.setattr(runtime.cloud, '_save_rebind', retain)
        if disk_failure:
            # An actual writer disk fault quarantines the whole installation;
            # filtering a stale approval must never override that protection.
            with pytest.raises(WriterQuarantined):
                await runtime.cloud.resume_rebinds()
            with pytest.raises(WriterQuarantined):
                runtime.writer.check()
            assert seen == [UID]
        else:
            await runtime.cloud.resume_rebinds()
            assert seen == [UID, OTHER_UID]
            runtime.writer.check()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_one_blocked_fixed_operation_does_not_stop_another_role(tmp_path):
    class FirstUnauthorized(ManagedService):
        async def request(self, request):
            if request.method == 'DELETE' and request.url.path.endswith('/' + UID):
                self.requests.append(request)
                return httpx.Response(401, json={'detail': 'unauthorized'})
            return await super().request(request)

    runtime, service, _ = setup_runtime(tmp_path, FirstUnauthorized())
    try:
        await runtime.configure('https://sync.test')
        policy = await runtime.cloud.refresh(force=True)
        for index, uid in enumerate((UID, OTHER_UID), 1):
            request = {'epoch': list(runtime.writer.epoch()), 'policy': policy,
                       'roles': runtime.cloud.directory(OWNER),
                       'body': {'baseRevision': 1, 'operationId': f'{index:032x}'}}
            runtime.cloud._save_operation(OWNER, 'delete', uid, request)
        await runtime.cloud.tick(runtime.cloud.presence)
        assert runtime.cloud._operation(OWNER, 'delete', UID)['status'] == 'blocked'
        assert runtime.cloud._operation(OWNER, 'delete', OTHER_UID)['status'] == 'ack'
        assert runtime.cloud.local_error(OWNER, UID) == 'character_delete_pending'
        assert UID in service.roles and OTHER_UID not in service.roles
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_management_async_flows_open_sqlite_only_in_worker_threads(tmp_path, monkeypatch):
    import threading
    runtime, _, _ = setup_runtime(tmp_path, ManagedService())
    runtime.profiles.adapter = DesktopProfileAdapter(Config())
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        loop_thread = threading.get_ident()
        original = runtime.cloud._connect

        def worker_connection():
            assert threading.get_ident() != loop_thread, 'management SQLite opened on the event loop'
            return original()

        monkeypatch.setattr(runtime.cloud, '_connect', worker_connection)
        await runtime.cloud.refresh(force=True)
        await runtime.cloud.require(OWNER, UID, await runtime._session())
        await runtime.cloud.select(1, [UID])
        await runtime.cloud.delete(UID, 2)
        result = await runtime.cloud.rebind(UID, 'Synthetic')
        assert result['characterSyncId'] != UID
        await runtime.cloud.resume_rebinds()
        await runtime.cloud.tick(runtime.cloud.presence)
    finally:
        await runtime.close()


class BeforeCommitFailure(ManagedService):
    """Persist a real approved request, but lose its first send before cloud mutation."""

    failures = 1

    async def request(self, request):
        destructive = request.method == 'DELETE' or (request.method == 'PUT' and request.url.path.endswith('/character-policy'))
        if destructive and self.failures:
            self.failures -= 1
            self.requests.append(request)
            raise httpx.ReadError('synthetic send failure before cloud commit')
        return await super().request(request)


async def pending_cloud_operation(tmp_path, kind, *, committed=False):
    service = ManagedService() if committed else BeforeCommitFailure()
    runtime, _, session = setup_runtime(tmp_path, service)
    await runtime.configure('https://sync.test')
    if committed:
        service.lose_delete_response = kind == 'delete'
        service.lose_selection_response = kind == 'select'
    with pytest.raises(ValueError, match='^chat_sync_unavailable$'):
        if kind == 'delete':
            await runtime.cloud.delete(UID, 1, expected_owner=OWNER)
        else:
            await runtime.cloud.select(1, [UID], expected_owner=OWNER)
    target = UID if kind == 'delete' else ''
    original = copy.deepcopy(runtime.cloud._operation(OWNER, kind, target)['request'])
    runtime.writer.recover(lambda: None)
    return runtime, service, session, original


async def confirm_cloud_operation(runtime, kind):
    if kind == 'delete':
        return await runtime.cloud.delete(UID, 1, expected_owner=OWNER)
    return await runtime.cloud.select(1, [UID], expected_owner=OWNER)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['delete', 'select'])
@pytest.mark.parametrize('committed', [False, True])
async def test_manual_reconfirmation_renews_only_epoch_and_keeps_exact_original_scope(tmp_path, kind, committed):
    runtime, service, _, original = await pending_cloud_operation(tmp_path, kind, committed=committed)
    try:
        first = [item.content for item in service.requests if item.method in {'PUT', 'DELETE'}][-1]
        if committed:
            service.roles['f' * 32] = 'Later cloud character'
            service.policy.update(revision=3, characterCount=len(service.roles), selectedCharacterSyncIds=list(service.roles))
        service.requests.clear()
        await runtime.scheduler_tick()
        assert service.requests == []
        if kind == 'select':
            pending = runtime.cloud.pending_selection(OWNER)
            assert pending['characters'] == original['roles']
            assert pending['selectedCharacterSyncIds'] == original['body']['selectedCharacterSyncIds']
        epoch = runtime.writer.epoch()
        await confirm_cloud_operation(runtime, kind)
        operation = runtime.cloud._operation(OWNER, kind, UID if kind == 'delete' else '')
        assert operation['status'] == 'ack'
        assert operation['request'] == {**original, 'epoch': list(epoch)}
        sent = [item.content for item in service.requests if item.method in {'PUT', 'DELETE'}]
        assert sent == [first]
        if committed:
            assert 'f' * 32 in service.roles
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['delete', 'select'])
async def test_reconfirmation_does_not_reauthorize_the_previous_account(tmp_path, kind):
    from tests.unit.test_chat_sync_runtime import OTHER_OWNER
    runtime, service, session, original = await pending_cloud_operation(tmp_path, kind)
    try:
        service.owner = OTHER_OWNER
        session['snapshot']['access_token'] = 'synthetic-other-account'
        service.requests.clear()
        with pytest.raises(ValueError, match='^chat_sync_account_mismatch$'):
            await confirm_cloud_operation(runtime, kind)
        assert runtime.cloud._operation(OWNER, kind, UID if kind == 'delete' else '')['request'] == original
        assert not any(item.method in {'PUT', 'DELETE'} for item in service.requests)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', [[], [UID, OTHER_UID]])
async def test_reconfirmation_cannot_replace_the_original_selection(tmp_path, selected):
    runtime, service, _, original = await pending_cloud_operation(tmp_path, 'select')
    try:
        service.requests.clear()
        with pytest.raises(ValueError, match='^character_selection_pending$'):
            await runtime.cloud.select(1, selected, expected_owner=OWNER)
        assert runtime.cloud._operation(OWNER, 'select', '')['request'] == original
        assert not any(item.method in {'PUT', 'DELETE'} for item in service.requests)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['delete', 'select'])
@pytest.mark.parametrize('stage', ['authentication', 'approval', 'send', 'replacement'])
async def test_reconfirmation_epoch_or_operation_races_never_send_stale_approval(tmp_path, monkeypatch, kind, stage):
    from services.chat_sync_writer import WriterQuarantined
    runtime, service, _, original = await pending_cloud_operation(tmp_path, kind)
    target = UID if kind == 'delete' else ''
    observed = []
    try:
        if stage == 'authentication':
            auth = runtime.cloud._auth

            async def authenticate():
                authenticated = await auth()
                runtime.writer.recover(lambda: None)
                return authenticated

            monkeypatch.setattr(runtime.cloud, '_auth', authenticate)
        elif stage in {'approval', 'replacement'}:
            commit = runtime.cloud._commit

            async def approve(fingerprint, epoch, operation, *args):
                if operation.__name__ == '_renew_confirmed_operation':
                    observed.append(stage)
                    if stage == 'approval':
                        runtime.writer.recover(lambda: None)
                    else:
                        changed = copy.deepcopy(original)
                        changed['body']['operationId'] = 'd' * 32
                        runtime.cloud._save_operation(OWNER, kind, target, changed)
                return await commit(fingerprint, epoch, operation, *args)

            monkeypatch.setattr(runtime.cloud, '_commit', approve)
        else:
            send = runtime.cloud._send_operation

            async def recover_before_send(*args, **kwargs):
                runtime.writer.recover(lambda: None)
                return await send(*args, **kwargs)

            monkeypatch.setattr(runtime.cloud, '_send_operation', recover_before_send)
        service.requests.clear()
        expected = ValueError if stage == 'replacement' else WriterQuarantined
        expected_code = '^character_policy_changed$' if stage == 'replacement' else '^chat_sync_writer_recovery_required$'
        with pytest.raises(expected, match=expected_code):
            await confirm_cloud_operation(runtime, kind)
        assert not any(item.method in {'PUT', 'DELETE'} for item in service.requests)
        if stage in {'approval', 'replacement'}:
            assert observed == [stage]
        saved = runtime.cloud._operation(OWNER, kind, target)['request']
        if stage == 'replacement':
            assert saved['body']['operationId'] == 'd' * 32
        else:
            assert saved['body'] == original['body']
            assert tuple(saved['epoch']) != runtime.writer.epoch()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['delete', 'select'])
@pytest.mark.parametrize('failure', ['network', 'cas'])
async def test_reconfirmed_operation_keeps_fixed_scope_on_new_failure(tmp_path, kind, failure):
    runtime, service, _, original = await pending_cloud_operation(tmp_path, kind)
    try:
        epoch = runtime.writer.epoch()
        if failure == 'network':
            service.failures = 1
        else:
            service.policy['revision'] += 1
        expected = '^chat_sync_unavailable$' if failure == 'network' else '^character_policy_changed$'
        with pytest.raises(ValueError, match=expected):
            await confirm_cloud_operation(runtime, kind)
        operation = runtime.cloud._operation(OWNER, kind, UID if kind == 'delete' else '')
        assert operation['request'] == {**original, 'epoch': list(epoch)}
        assert operation['status'] == ('pending' if failure == 'network' else 'blocked')
        assert set(service.roles) == {UID, OTHER_UID}
        if failure == 'network':
            await confirm_cloud_operation(runtime, kind)
            assert runtime.cloud._operation(OWNER, kind, UID if kind == 'delete' else '')['status'] == 'ack'
        requests = [item.content for item in service.requests if item.method in {'PUT', 'DELETE'}]
        assert len(set(requests)) == 1
    finally:
        await runtime.close()
