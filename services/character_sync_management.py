"""Account-wide cloud choices and fixed destructive requests, independent of local memory."""

from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
from uuid import uuid4

from services.chat_sync_writer import store_connection


POLICY_ERRORS = {'character_selection_required', 'character_not_selected'}
PUBLIC_ERRORS = POLICY_ERRORS | {'character_policy_changed', 'character_quota_exceeded', 'character_deleted'}


def _canonical(value):
    """Keep approved destructive request bytes immutable after a lost response."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def _policy(value, owner):
    """Validate the complete authoritative account snapshot before changing local admission."""
    from services.chat_sync_ledger import ChatDomain
    fields = {'ownerId', 'revision', 'characterLimit', 'characterCount', 'requiresSelection', 'selectedCharacterSyncIds'}
    if (not isinstance(value, dict) or value.keys() != fields or value['ownerId'] != owner
            or type(value['revision']) is not int or not 0 <= value['revision'] <= 2**53 - 1
            or type(value['characterLimit']) is not int or value['characterLimit'] != 3
            or type(value['characterCount']) is not int or not 0 <= value['characterCount'] <= 2**53 - 1
            or type(value['requiresSelection']) is not bool or not isinstance(value['selectedCharacterSyncIds'], list)):
        raise ValueError('invalid_character_policy_response')
    selected = value['selectedCharacterSyncIds']
    try:
        for uid in selected:
            ChatDomain(owner, uid)
        if (len(selected) > 3 or len(selected) != len(set(selected)) or len(selected) > value['characterCount']
                or (value['requiresSelection'] and selected)
                or (not value['requiresSelection'] and len(selected) != value['characterCount'])):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError('invalid_character_policy_response') from None
    # Selection order carries no authority; fixed request bytes remain untouched.
    return {**value, 'selectedCharacterSyncIds': sorted(selected)}


class DesktopCloudCharacterManagement:
    """Persist only account metadata and approved operations in the existing writer-owned state file."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.path = runtime.state.path
        self.lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.next_at = 0
        self.presence = None
        self.blocked_presence = None
        self.blocked_owner = None
        with closing(self._connect()) as conn, conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS sync_cloud_policies (
                    owner_id TEXT PRIMARY KEY, presence TEXT NOT NULL, snapshot TEXT NOT NULL, directory TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sync_cloud_retired (
                    owner_id TEXT NOT NULL, uid TEXT NOT NULL, PRIMARY KEY(owner_id,uid));
                CREATE TABLE IF NOT EXISTS sync_cloud_operations (
                    owner_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
                    request TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
                    next_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner_id,kind,target));
                CREATE TABLE IF NOT EXISTS sync_cloud_rebinds (
                    old_uid TEXT PRIMARY KEY, new_uid TEXT UNIQUE NOT NULL, owner_id TEXT NOT NULL,
                    request TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0);
            ''')

    def _connect(self):
        conn = store_connection(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=FULL')
        return conn

    def cached(self, *, owner=None, presence=None):
        """Local UI reads never fetch credentials or perform a cloud request."""
        with closing(self._connect()) as conn:
            row = (conn.execute('SELECT snapshot FROM sync_cloud_policies WHERE owner_id=?', (owner,)).fetchone()
                   if owner else conn.execute('SELECT snapshot FROM sync_cloud_policies WHERE presence=?', (presence,)).fetchone())
        return json.loads(row[0]) if row else None

    def directory(self, owner):
        with closing(self._connect()) as conn:
            row = conn.execute('SELECT directory FROM sync_cloud_policies WHERE owner_id=?', (owner,)).fetchone()
        return json.loads(row[0]) if row else []

    def retired(self, owner, uid):
        with closing(self._connect()) as conn:
            return conn.execute('SELECT 1 FROM sync_cloud_retired WHERE owner_id=? AND uid=?', (owner, uid)).fetchone() is not None

    def local_error(self, owner, uid, *, registration=False):
        """Apply global authority to every body/profile/delete lane, including explicit manual passes."""
        if self.retired(owner, uid):
            return 'character_deleted'
        if self.blocked_owner == owner:
            return 'character_policy_unavailable'
        with closing(self._connect()) as conn:
            # An explicit CAS rejection proves this request deleted nothing.
            # Unknown receipts and authentication failures still need a fence.
            if conn.execute("SELECT 1 FROM sync_cloud_operations WHERE owner_id=? AND kind='delete' AND target=? "
                            "AND (status='pending' OR (status='blocked' AND (error IS NULL OR error!='character_policy_changed')))",
                            (owner, uid)).fetchone():
                return 'character_delete_pending'
            selection = conn.execute("SELECT request FROM sync_cloud_operations WHERE owner_id=? AND kind='select' AND status='pending'", (owner,)).fetchone()
        if selection and uid not in json.loads(selection[0])['body']['selectedCharacterSyncIds']:
            return 'character_delete_pending'
        policy = self.cached(owner=owner)
        if policy and policy['requiresSelection']:
            return 'character_selection_required'
        if policy and not registration and uid not in policy['selectedCharacterSyncIds']:
            return 'character_not_selected'
        return None

    def require_local(self, owner, uid, *, registration=False):
        if error := self.local_error(owner, uid, registration=registration):
            raise ValueError(error)

    async def _commit(self, fingerprint, epoch, operation, *args):
        """Metadata commits use the same short account/writer gates as chat response commits."""
        await self.runtime._same_session(fingerprint)
        def apply():
            with self.runtime.session_commit_guard(fingerprint), self.runtime._commit_lock, self.runtime.writer.locked():
                self.runtime.writer.require_epoch(epoch)
                if self.runtime._closing:
                    raise ValueError('chat_sync_closing')
                return operation(*args)
        return await asyncio.to_thread(apply)

    async def _auth(self):
        token, fingerprint = await self.runtime._session()
        owner = await self.runtime._identity(token)
        await self.runtime._same_session(fingerprint)
        return token, fingerprint, owner

    def _retire(self, owner, uid):
        """Retirement revokes old cloud authority without erasing any local original or ledger row."""
        with closing(self._connect()) as conn, conn:
            conn.execute('INSERT OR IGNORE INTO sync_cloud_retired VALUES (?,?)', (owner, uid))
        binding = self.runtime.state.binding(uid)
        if binding and binding['owner_id'] == owner:
            if binding['enabled']:
                self.runtime.state.pause(uid)
            self.runtime.state.save_choice(uid, False)
            self.runtime.state.finish_new_role(uid)

    def _accept(self, owner, fingerprint, policy, roles):
        previous = self.cached(owner=owner)
        if previous:
            previous = _policy(previous, owner)
        if previous and (policy['revision'] < previous['revision'] or
                         (policy['revision'] == previous['revision'] and policy != previous)):
            raise ValueError('invalid_character_policy_response')
        with closing(self._connect()) as conn, conn:
            conn.execute('INSERT OR REPLACE INTO sync_cloud_policies VALUES (?,?,?,?)',
                         (owner, fingerprint, _canonical(policy).decode(), _canonical(roles).decode()))
        available = {role['characterSyncId'] for role in roles}
        # Missing is retirement proof only after a complete, count-checked directory
        # bracketed by the same authoritative policy revision and authenticated owner.
        for binding in self.runtime.state.bindings():
            if binding['owner_id'] == owner and binding['binding_id'] and binding['character_uid'] not in available:
                self._retire(owner, binding['character_uid'])

    async def refresh(self, *, force=False, authenticated=None, owner=None):
        """Read policy, every directory page, then policy again before accepting any deletion proof."""
        from services.character_profile_sync import read_character_directory
        async with self.lock:
            if authenticated is None or owner is None:
                token, fingerprint, owner = await self._auth()
            else:
                token, fingerprint = authenticated
            cached = await asyncio.to_thread(self.cached, owner=owner)
            if self.blocked_presence == fingerprint and not force:
                raise ValueError('character_policy_unavailable')
            if not force and cached and self.presence == fingerprint and self.runtime._clock() < self.next_at:
                return cached
            epoch = await asyncio.to_thread(self.runtime.writer.epoch)
            async def request(method, path, body):
                await self.runtime._same_session(fingerprint)
                result = await self.runtime._request(method, path, token, body=body,
                    before_send=lambda: self.runtime._same_session(fingerprint))
                await self.runtime._same_session(fingerprint)
                return result
            try:
                first = _policy(await request('GET', '/prototype/chat-sync/character-policy', None), owner)
                roles = (await read_character_directory(request, owner))['characters']
                last = _policy(await request('GET', '/prototype/chat-sync/character-policy', None), owner)
                if first != last:
                    # Another device changed the directory while it was read.
                    # Retry a fresh bracket rather than permanently blocking
                    # this account or accepting an incomplete retirement proof.
                    if last['revision'] > first['revision']:
                        raise ValueError('character_policy_changed')
                    # A rollback or conflicting same-version authority is
                    # not a normal concurrent update and remains fail-closed.
                    raise ValueError('invalid_character_policy_response')
                if (len(roles) != last['characterCount']
                        or not set(last['selectedCharacterSyncIds']).issubset({role['characterSyncId'] for role in roles})):
                    raise ValueError('invalid_character_policy_response')
                await self._commit(fingerprint, epoch, self._accept, owner, fingerprint, last, roles)
            except (ValueError, OSError) as exc:
                self.presence = fingerprint
                self.next_at = self.runtime._clock() + 30_000
                if (str(exc) in {'invalid_character_policy_response', 'invalid_character_profile_response'}
                        or getattr(exc, 'status_code', None) in {401, 403, 404}):
                    self.blocked_presence, self.blocked_owner = fingerprint, owner
                raise
            self.presence = fingerprint
            self.blocked_presence = self.blocked_owner = None
            self.next_at = self.runtime._clock() + 120_000
            return last

    async def require(self, owner, uid, authenticated, *, registration=False):
        await self.refresh(authenticated=authenticated, owner=owner)
        await asyncio.to_thread(self.require_local, owner, uid, registration=registration)

    def invalidate(self):
        """A socket frame only schedules authoritative HTTP; it never retires a role or advances state."""
        self.next_at = 0
        self.runtime.wake_scheduler()

    def due(self, presence):
        """Permanent protocol/auth failures wait for changed credentials or an explicit refresh."""
        if self.blocked_presence == presence:
            return False
        if self.presence != presence or self.next_at <= self.runtime._clock():
            return True
        policy = self.cached(presence=presence)
        # Another account's or installation's fixed approval must not turn
        # this login into continuous identity polling or gain fresh authority.
        return bool(policy) and bool(self._pending_operations(policy['ownerId'], ready=True))

    def _pending_operations(self, owner=None, *, ready=False):
        with closing(self._connect()) as conn:
            query = "SELECT * FROM sync_cloud_operations WHERE status='pending' AND (? IS NULL OR owner_id=?)"
            arguments = (owner, owner)
            if ready:
                query += " AND next_at<=?"
                arguments += (self.runtime._clock(),)
            rows = conn.execute(query, arguments).fetchall()
        if not rows:
            return []
        # A checked metadata read is still required, but an empty poll has no
        # approval to validate and must not trigger another all-store head check.
        epoch = self.runtime.writer.epoch()
        operations = [{**dict(row), 'request': json.loads(row['request'])} for row in rows]
        return [row for row in operations if tuple(row['request']['epoch']) == epoch]

    def has_pending_work(self):
        """Approved cloud deletion remains recoverable even with every local role paused."""
        return bool(self._pending_operations())

    def pending_selection(self, owner):
        """Reopening the UI reviews the original approved scope after a lost receipt."""
        operation = self._operation(owner, 'select', '')
        if not operation or operation['status'] != 'pending':
            return None
        request = operation['request']
        return {'baseRevision': request['body']['baseRevision'],
                'selectedCharacterSyncIds': request['body']['selectedCharacterSyncIds'],
                'characters': request['roles']}

    def _operation(self, owner, kind, target):
        with closing(self._connect()) as conn:
            row = conn.execute('SELECT * FROM sync_cloud_operations WHERE owner_id=? AND kind=? AND target=?', (owner, kind, target)).fetchone()
        return {**dict(row), 'request': json.loads(row['request'])} if row else None

    def _save_operation(self, owner, kind, target, request):
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO sync_cloud_operations VALUES (?,?,?,?,'pending',NULL,0)",
                         (owner, kind, target, _canonical(request).decode()))

    def _finish_operation(self, owner, kind, target, status, error=None):
        with closing(self._connect()) as conn, conn:
            conn.execute('UPDATE sync_cloud_operations SET status=?,error=?,next_at=? WHERE owner_id=? AND kind=? AND target=?',
                         (status, error, self.runtime._clock() + 30_000, owner, kind, target))

    def _renew_confirmed_operation(self, owner, kind, target, reviewed, epoch):
        """Renew only the installation proof for an explicitly reconfirmed fixed operation."""
        current = self._operation(owner, kind, target)
        if not current or current['status'] != 'pending' or current['request'] != reviewed:
            raise ValueError('character_policy_changed')
        # The manual entry reviewed the original scope again. Preserve the
        # operation ID, exact body, role snapshot and original revision so a
        # lost cloud receipt cannot turn into a new or expanded deletion.
        renewed = {**reviewed, 'epoch': list(epoch)}
        self._save_operation(owner, kind, target, renewed)
        return renewed

    async def _send_operation(self, owner, kind, target, request, authenticated):
        token, fingerprint = authenticated
        epoch = tuple(request['epoch'])
        await asyncio.to_thread(self.runtime.writer.require_epoch, epoch)
        path = '/prototype/chat-sync/character-policy' if kind == 'select' else '/prototype/chat-sync/characters/' + target
        async def before_send():
            await self.runtime._same_session(fingerprint)
            await asyncio.to_thread(self.runtime.writer.require_epoch, epoch)
        try:
            result = await self.runtime._request('PUT' if kind == 'select' else 'DELETE', path, token,
                body=_canonical(request['body']), before_send=before_send)
            if kind == 'delete':
                expected = {'ownerId': owner, 'characterSyncId': target, 'operationId': request['body']['operationId'],
                            'revision': request['body']['baseRevision'] + 1, 'deleted': True}
                if result != expected:
                    raise ValueError('invalid_character_policy_response')
                await self._commit(fingerprint, epoch, self._retire, owner, target)
            else:
                if not isinstance(result, dict) or result.keys() != set(request['policy']) | {'operationId', 'deletedCount'}:
                    raise ValueError('invalid_character_policy_response')
                policy = _policy({key: result[key] for key in request['policy']}, owner)
                chosen = request['body']['selectedCharacterSyncIds']
                if (result['operationId'] != request['body']['operationId'] or policy['revision'] != request['body']['baseRevision'] + 1
                        or policy['requiresSelection'] or set(policy['selectedCharacterSyncIds']) != set(chosen)
                        or policy['characterCount'] != len(chosen) or type(result['deletedCount']) is not int
                        or result['deletedCount'] != request['policy']['characterCount'] - len(chosen)):
                    raise ValueError('invalid_character_policy_response')
            await self._commit(fingerprint, epoch, self._finish_operation, owner, kind, target, 'ack')
        except (ValueError, OSError) as exc:
            code = str(exc)
            if kind == 'delete' and code == 'character_deleted':
                # A fixed authenticated 410 proves retirement, not acceptance of this operation ID.
                await self._commit(fingerprint, epoch, self._retire, owner, target)
            terminal = (code in {'character_policy_changed', 'character_deleted', 'invalid_character_policy_response'}
                        or getattr(exc, 'status_code', None) in {401, 403, 404})
            await self._commit(fingerprint, epoch, self._finish_operation, owner, kind, target,
                               'blocked' if terminal else 'pending', code if terminal else None)
            raise
        # The ACK is durable even if this optional refresh is offline. A lost receipt
        # is retried with the original operation/body, never against a new directory.
        try:
            await self.refresh(force=True, authenticated=authenticated, owner=owner)
        except (ValueError, OSError):
            self.invalidate()
        return result

    async def _run_operation(self, kind, target, base_revision, selected=None, *, expected_owner=None):
        from services.chat_sync_ledger import ChatDomain
        async with self.operation_lock:
            # Only select/delete's confirmed manual entry can renew approval.
            # Pin before authentication awaits: a concurrent second recovery
            # must reject this confirmation rather than silently promoting it.
            epoch = await asyncio.to_thread(self.runtime.writer.epoch)
            token, fingerprint, owner = await self._auth()
            if expected_owner is not None and expected_owner != owner:
                raise ValueError('chat_sync_account_mismatch')
            authenticated = token, fingerprint
            if kind == 'delete':
                ChatDomain(owner, target)
            existing = await asyncio.to_thread(self._operation, owner, kind, target)
            if existing and existing['status'] == 'pending':
                # A pending destructive approval cannot be replaced by a later page/draft.
                if kind == 'select' and existing['request']['body']['selectedCharacterSyncIds'] != selected:
                    raise ValueError('character_selection_pending')
                request = existing['request']
                if tuple(request['epoch']) != epoch:
                    request = await self._commit(fingerprint, epoch, self._renew_confirmed_operation,
                                                 owner, kind, target, request, epoch)
                # Automatic tick never enters this approval path and still
                # refuses requests pinned to a retired installation epoch.
                return await self._send_operation(owner, kind, target, request, authenticated)
            policy = await self.refresh(force=True, authenticated=authenticated, owner=owner)
            if policy['revision'] != base_revision:
                raise ValueError('character_policy_changed')
            roles = await asyncio.to_thread(self.directory, owner)
            available = {role['characterSyncId'] for role in roles}
            if kind == 'select':
                if (not isinstance(selected, list) or len(selected) > 3 or any(uid not in available for uid in selected)
                        or len(selected) != len(set(selected))):
                    raise ValueError('invalid_character_selection')
                body = {'baseRevision': base_revision, 'operationId': uuid4().hex, 'selectedCharacterSyncIds': selected}
            else:
                if target not in available:
                    raise ValueError('character_deleted')
                body = {'baseRevision': base_revision, 'operationId': uuid4().hex}
            request = {'body': body, 'epoch': list(epoch), 'policy': policy, 'roles': roles}
            await self._commit(fingerprint, epoch, self._save_operation, owner, kind, target, request)
            return await self._send_operation(owner, kind, target, request, authenticated)

    async def select(self, base_revision, selected_uids, *, expected_owner=None):
        """The confirmed choice deletes every unselected cloud role in one fixed CAS operation."""
        return await self._run_operation('select', '', base_revision, selected_uids, expected_owner=expected_owner)

    async def delete(self, uid, base_revision, *, expected_owner=None):
        """Delete only cloud data; the local role, persona and conversations remain usable."""
        return await self._run_operation('delete', uid, base_revision, expected_owner=expected_owner)

    async def retire(self, owner, uid, authenticated):
        """Accept a fixed 410 from a verified role request without touching local content."""
        epoch = await asyncio.to_thread(self.runtime.writer.epoch)
        await self._commit(authenticated[1], epoch, self._retire, owner, uid)

    def reassignment(self, uid):
        """A fixed local rebind is discoverable from either side of its config-file crash gap."""
        with closing(self._connect()) as conn:
            # B can be both the completed target of A and the retired source of C.
            # The exact predecessor always wins over an older inherited mapping.
            row = conn.execute('SELECT * FROM sync_cloud_rebinds WHERE old_uid=? OR new_uid=? ORDER BY (old_uid=?) DESC,completed LIMIT 1', (uid, uid, uid)).fetchone()
        return {**dict(row), 'request': json.loads(row['request'])} if row else None

    def _save_rebind(self, old_uid, new_uid, owner, request):
        with closing(self._connect()) as conn, conn:
            conn.execute('INSERT OR REPLACE INTO sync_cloud_rebinds VALUES (?,?,?,?,0)',
                         (old_uid, new_uid, owner, _canonical(request).decode()))

    def _complete_rebind(self, old_uid):
        with closing(self._connect()) as conn, conn:
            conn.execute('UPDATE sync_cloud_rebinds SET completed=1 WHERE old_uid=?', (old_uid,))

    def cancel_rebind(self, uid):
        """A later explicit Off revokes the saved On without losing its local source mapping."""
        intent = self.reassignment(uid)
        if not intent:
            return
        if not intent['completed']:
            self._save_rebind(intent['old_uid'], intent['new_uid'], intent['owner_id'],
                              {**intent['request'], 'cancelled': True})
        for target in (intent['old_uid'], intent['new_uid']):
            self.runtime.state.finish_new_role(target)
            self.runtime.state.save_choice(target, False)
            if self.runtime.state.binding(target):
                self.runtime.state.pause(target)

    async def rebind(self, old_uid, name, *, include_history=True):
        """Only an explicit saved On creates a new UID after cloud retirement; offline saves remain local."""
        from services.character_profile_protocol import encode_character_profile
        existing = await asyncio.to_thread(self.reassignment, old_uid)
        if existing and existing['new_uid'] == old_uid and not existing['completed']:
            old_uid = existing['old_uid']
        binding = await asyncio.to_thread(self.runtime.state.binding, old_uid)
        if not binding or not await asyncio.to_thread(self.retired, binding['owner_id'], old_uid):
            raise ValueError('chat_sync_binding_changed')
        existing = await asyncio.to_thread(self.reassignment, old_uid)
        new_uid = existing['new_uid'] if existing and not existing['completed'] else uuid4().hex
        try:
            profile = await self.runtime.profiles.adapter.read(old_uid)
        except ValueError as exc:
            if str(exc) != 'chat_sync_character_not_found' or not existing:
                raise
            profile = await self.runtime.profiles.adapter.read(new_uid)
        epoch = await asyncio.to_thread(self.runtime.writer.epoch)
        request = {'epoch': list(epoch), 'name': name, 'includeHistory': include_history,
                   'canonical': encode_character_profile(profile).decode()}
        def prepare():
            with self.runtime._commit_lock, self.runtime.writer.locked():
                self.runtime.writer.require_epoch(epoch)
                self._save_rebind(old_uid, new_uid, binding['owner_id'], request)
        await asyncio.to_thread(prepare)
        await self._advance_rebind(await asyncio.to_thread(self.reassignment, old_uid))
        return {'characterSyncId': new_uid, 'pending': True, 'enabled': False}

    async def _advance_rebind(self, intent):
        """Replay one fixed source/config boundary without changing memory paths or deleting old ledgers."""
        from services.character_profile_protocol import encode_character_profile
        from services.character_profile_sync import export_profile
        from services.chat_sync_source import ChatOriginalSource
        from utils.character_memory import character_config_mutation_lock
        from utils.config_manager.reserved_schema import get_character_uid, set_reserved
        request, old_uid, new_uid, owner = intent['request'], intent['old_uid'], intent['new_uid'], intent['owner_id']
        epoch = tuple(request['epoch'])
        async with character_config_mutation_lock:
            data = await self.runtime.profiles.adapter.config.aload_characters()
            matches = [(name, raw) for name, raw in data.get('猫娘', {}).items()
                       if isinstance(raw, dict) and get_character_uid(raw) in {old_uid, new_uid}]
            if len(matches) != 1:
                raise ValueError('chat_sync_character_not_found')
            name, raw = matches[0]
            if encode_character_profile(export_profile(name, raw)).decode() != request['canonical']:
                raise ValueError('character_profile_changed')
            source = await asyncio.to_thread(ChatOriginalSource, self.runtime.state.path.parent / 'sources.sqlite3')
            def transfer():
                with self.runtime._commit_lock, self.runtime.writer.locked():
                    self.runtime.writer.require_epoch(epoch)
                    if not self.retired(owner, old_uid):
                        raise ValueError('chat_sync_binding_changed')
                    source.reassign_role(owner, old_uid, new_uid, self.runtime.ledger)
                    if get_character_uid(raw) != new_uid:
                        set_reserved(raw, 'character_uid', new_uid)
                        self.runtime.profiles.adapter.config.save_characters(data)
            await asyncio.to_thread(transfer)
        def complete():
            with self.runtime._commit_lock, self.runtime.writer.locked():
                self.runtime.writer.require_epoch(epoch)
                # Re-read the durable intent under the same gate as Off. The local
                # migration may finish after cancellation, but cannot restore its On.
                current = self.reassignment(old_uid)
                if current['request'].get('cancelled'):
                    self.runtime.state.finish_new_role(new_uid)
                    self.runtime.state.save_choice(new_uid, False)
                else:
                    self.runtime._queue_sync_choice(new_uid, name, export_profile(name, raw),
                        self.runtime.state.binding(new_uid), epoch, include_history=request['includeHistory'],
                        replace=True, expected_owner=owner)
                    self.runtime.state.save_choice(new_uid, True)
                self._complete_rebind(old_uid)
        await asyncio.to_thread(complete)
        self.runtime.wake_scheduler()

    async def resume_rebinds(self):
        """Recover local-only rebind work before automatic grants are considered after restart."""
        if self.runtime.profiles.adapter is None:
            return
        def pending():
            with closing(self._connect()) as conn:
                rows = conn.execute('SELECT * FROM sync_cloud_rebinds WHERE completed=0').fetchall()
            return [{**dict(row), 'request': json.loads(row['request'])} for row in rows]
        epoch = None
        for intent in await asyncio.to_thread(pending):
            if intent['request'].get('error') or intent['request'].get('nextAt', 0) > self.runtime._clock():
                continue
            if epoch is None:
                # Share only admission's snapshot. Transfers and retry writes
                # still revalidate their original epoch under the commit locks.
                epoch = await asyncio.to_thread(self.runtime.writer.epoch)
            # Recovery revokes the old installation's approvals. Keep its
            # fixed local intent for explicit review, but never replay it or
            # rewrite its epoch merely to let an automatic worker proceed.
            if tuple(intent['request']['epoch']) != epoch:
                continue
            try:
                await self._advance_rebind(intent)
            except (ValueError, OSError, sqlite3.Error) as exc:
                # One interrupted local rebind cannot stop other characters' sync.
                # Changed definitions need a fresh explicit Save, while disk failures back off.
                request = {**intent['request'], 'nextAt': self.runtime._clock() + 30_000}
                if str(exc) in {'character_profile_changed', 'chat_sync_character_not_found', 'chat_sync_binding_conflict'}:
                    request['error'] = str(exc)
                def retain():
                    with self.runtime._commit_lock, self.runtime.writer.locked():
                        self.runtime.writer.require_epoch(tuple(request['epoch']))
                        self._save_rebind(intent['old_uid'], intent['new_uid'], intent['owner_id'], request)
                try:
                    await asyncio.to_thread(retain)
                except (ValueError, OSError, sqlite3.Error):
                    # A failed per-role retry write must not stop other roles.
                    # A genuinely quarantined installation still fails closed.
                    await asyncio.to_thread(self.runtime.writer.check)

    async def tick(self, presence=None):
        """Retry only previously confirmed fixed operations and refresh global metadata at a bounded cadence."""
        try:
            token, fingerprint, owner = await self._auth()
        except (ValueError, OSError) as exc:
            self.presence = presence
            self.next_at = self.runtime._clock() + 30_000
            if str(exc) == 'chat_sync_login_required' or getattr(exc, 'status_code', None) in {401, 403}:
                self.blocked_presence = presence
            raise
        authenticated = token, fingerprint
        async with self.operation_lock:
            for row in await asyncio.to_thread(self._pending_operations, owner, ready=True):
                try:
                    await self._send_operation(owner, row['kind'], row['target'], row['request'], authenticated)
                except (ValueError, OSError):
                    # Each fixed operation retains its own blocked/retry state.
                    # Epoch changes revoke only the old approval; a quarantined
                    # writer still stops admission for this entire installation.
                    await asyncio.to_thread(self.runtime.writer.check)
        return await self.refresh(authenticated=authenticated, owner=owner)
