"""Cloud account choices and retirement use disposable identities and local-only stores."""

import copy
import json

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
