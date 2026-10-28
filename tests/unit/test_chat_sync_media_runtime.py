"""Exercise real witnessed sidecars and the desktop HTTP boundary with synthetic identities."""

import hashlib
import json

import httpx
import pytest

from tests.chat_sync_writer_fixture import isolated_chat_sync_writer  # noqa: F401
from tests.unit.test_chat_sync_runtime import BINDING, DOMAIN, OWNER, UID, SyntheticService, capture, setup_runtime
from services.chat_sync.media_protocol import encode
from services.chat_sync.media_sync import canonical, delivered_record


@pytest.mark.asyncio
async def test_image_only_submission_has_one_empty_original_and_all_sent_images(tmp_path, monkeypatch):
    import base64
    import io
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from PIL import Image
    from services.chat_sync import media_sync, runtime as runtime_module, source as source_module

    runtime, _, _ = setup_runtime(tmp_path, MediaService())
    raw = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(raw, format='JPEG')
    sent = base64.b64encode(raw.getvalue()).decode()
    manager = SimpleNamespace(lanlan_name='Synthetic', _config_manager=object(),
                              _chat_shared_images={'image-only': [sent, sent], 'other': [sent]})
    saved = AsyncMock(return_value='1' * 32)
    monkeypatch.setattr(source_module, 'character_uid_for', AsyncMock(return_value=UID))
    monkeypatch.setattr(source_module, 'save_original', saved)
    monkeypatch.setattr(runtime_module, 'get_desktop_chat_sync_runtime', AsyncMock(return_value=runtime))
    try:
        identity = await media_sync.capture_user_media(manager, None, 'image-only', original_time='1700000000000')
        assert identity == '1' * 32
        saved.assert_awaited_once_with('Synthetic', 'user', '', original_time='1700000000000',
                                      config_manager=manager._config_manager)
        rows = runtime.media.store.rows('local/' + UID)
        assert len(rows) == 1
        record = json.loads(rows[0]['canonical'])
        assert record['messageId'] == identity and len(record['attachments']) == 2
        assert 'image-only' not in manager._chat_shared_images and manager._chat_shared_images['other'] == [sent]
        source = json.loads(rows[0]['source'])
        assert len(source['images']) == 2
        for files in source['images'].values():
            assert __import__('pathlib').Path(files['original']['path']).read_bytes() == raw.getvalue()
    finally:
        await runtime.close()


class MediaService(SyntheticService):
    def __init__(self):
        super().__init__()
        self.media, self.revision, self.operations = {}, 0, {}
        self.model = {"schemaVersion": 1, "model": None}
        self.model_revision = 1

    async def request(self, request):
        path = request.url.path
        if path == "/v1/chat-sync/media/capabilities":
            return httpx.Response(200, json={"schemaVersion": 1, "media": 1, "modelBinding": 1, "resources": False})
        if path.endswith(("/media", "/model-binding")):
            assert request.headers["X-Neko-Chat-Binding"] == BINDING
            if path.endswith("/model-binding"):
                return httpx.Response(200, json={"ownerId": OWNER, "characterSyncId": UID, "revision": self.model_revision,
                    "record": self.model, "recordHash": hashlib.sha256(encode(self.model, model=True)).hexdigest()})
            if request.method == "GET":
                return httpx.Response(200, json={"ownerId": OWNER, "characterSyncId": UID, "cursor": self.revision,
                    "hasMore": False, "records": [{"messageId": identity, "revision": revision, "record": record,
                    "recordHash": hashlib.sha256(encode(record)).hexdigest()} for identity, (revision, record) in self.media.items()]})
            value = json.loads(request.content)
            assert canonical(value) == request.content
            identity = value["record"]["messageId"]
            if value["operationId"] not in self.operations:
                self.revision += 1
                self.media[identity] = self.revision, value["record"]
                self.operations[value["operationId"]] = {"ownerId": OWNER, "characterSyncId": UID,
                    "operationId": value["operationId"], "revision": self.revision,
                    "recordHash": hashlib.sha256(encode(value["record"])).hexdigest()}
            return httpx.Response(200, json=self.operations[value["operationId"]])
        return await super().request(request)


def test_invalid_delivery_attachment_does_not_drop_other_valid_links():
    record = delivered_record("1" * 32, [
        {"url": "https://news.example/bad", "title": {"unsafe": "object"}},
        {"url": "https://music.example/play", "title": "Track", "artist": "Artist"},
    ])
    assert len(record["attachments"]) == 1
    assert record["attachments"][0]["type"] == "music"


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_purpose', ['image', 'thumbnail'])
async def test_thumbnail_first_and_other_ready_resource_survives_upload_failure(tmp_path, monkeypatch, failed_purpose):
    class Resources(MediaService):
        async def request(self, request):
            if request.url.path.endswith('/capabilities'):
                return httpx.Response(200, json={'schemaVersion': 1, 'media': 1, 'modelBinding': 1, 'resources': True})
            return await super().request(request)

    runtime, service, _ = setup_runtime(tmp_path, Resources())
    uploaded, failed = [], False

    async def upload(_request, _base, file, guard=None):
        nonlocal failed
        assert guard is not None
        await guard()
        uploaded.append(file['purpose'])
        if not failed and file['purpose'] == failed_purpose:
            failed = True
            raise OSError('synthetic_upload_failed')

    monkeypatch.setattr(runtime.media, 'upload_file', upload)
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        identity = capture(runtime)['messageId']
        record = delivered_record(identity, [])
        record['attachments'] = [{'id': 'f' * 32, 'type': 'image', 'title': '', 'source': 'chat',
                                  'original': None, 'thumbnail': None}]
        runtime.media.store.capture(UID, record, {'complete': False, 'images': {'f' * 32: {
            'original': {'resourceId': '3' * 32, 'purpose': 'image', 'uploaded': False},
            'thumbnail': {'resourceId': '4' * 32, 'purpose': 'thumbnail', 'uploaded': False},
        }}})
        with pytest.raises(OSError, match='synthetic_upload_failed'):
            await runtime.media.step(UID, manual=True)
        assert uploaded == ['thumbnail', 'image']
        partial = service.media[identity][1]['attachments'][0]
        assert partial['original'] == (None if failed_purpose == 'image' else {'resourceId': '3' * 32})
        assert partial['thumbnail'] == (None if failed_purpose == 'thumbnail' else {'resourceId': '4' * 32})
        await runtime.media.step(UID, manual=True)
        assert uploaded == ['thumbnail', 'image', failed_purpose]
        complete = service.media[identity][1]['attachments'][0]
        assert complete['original'] == {'resourceId': '3' * 32}
        assert complete['thumbnail'] == {'resourceId': '4' * 32}
        assert len(service.operations) == 3
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('reupload', [False, True])
@pytest.mark.parametrize('revoke', ['pause', 'pause-resume', 'logout', 'session-change'])
async def test_image_upload_stops_after_consent_or_session_changes(tmp_path, monkeypatch, reupload, revoke):
    import io
    from types import SimpleNamespace
    from PIL import Image

    class Resources(MediaService):
        missing = reupload
        completed = 0
        async def request(self, request):
            if request.url.path.endswith('/capabilities'):
                return httpx.Response(200, json={'schemaVersion': 1, 'media': 1, 'modelBinding': 1, 'resources': True})
            if request.method == 'PUT' and request.url.path.endswith('/media') and self.missing:
                self.missing = False
                return httpx.Response(409, json={'detail': 'media_resource_not_ready'})
            if request.method == 'POST' and request.url.path.endswith('/resources'):
                value = json.loads(request.content)
                return httpx.Response(200, json={'resourceId': value['resourceId'], 'ready': False,
                    'upload': {'url': 'https://private-oss.example/upload', 'method': 'PUT',
                               'headers': {'Content-Type': value['contentType']}}})
            if request.url.path.endswith('/complete'):
                self.completed += 1
                return httpx.Response(200, json={'resourceId': request.url.path.split('/')[-2], 'ready': True})
            return await super().request(request)

    runtime, service, session = setup_runtime(tmp_path, Resources())
    payload = io.BytesIO()
    Image.effect_noise((512, 512), 100).convert('RGB').save(payload, format='JPEG', quality=98)
    raw = payload.getvalue()
    assert len(raw) > 65536
    file = tmp_path / 'sent.jpg'
    file.write_bytes(raw)
    sent = []
    revoked = False
    class UploadClient:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        async def put(self, _url, *, headers, content):
            nonlocal revoked
            async for chunk in content:
                sent.append(len(chunk))
                if not revoked:
                    revoked = True
                    if revoke in ('pause', 'pause-resume'):
                        await runtime.pause(UID)
                        if revoke == 'pause-resume':
                            await runtime.enable(UID, 'Neko')
                    elif revoke == 'logout':
                        session['logged_in'] = False
                    else:
                        session['snapshot']['access_token'] = 'synthetic-new-session'
            return SimpleNamespace(status_code=200)
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Neko')
        identity = capture(runtime)['messageId']
        record = delivered_record(identity, [])
        attachment = 'e' * 32
        record['attachments'] = [{'id': attachment, 'type': 'image', 'title': '', 'source': 'chat',
                                  'original': {'resourceId': 'f' * 32} if reupload else None, 'thumbnail': None}]
        runtime.media.store.capture(UID, record, {'complete': False, 'images': {attachment: {'original': {
            'resourceId': 'f' * 32, 'purpose': 'image', 'contentType': 'image/jpeg', 'path': str(file),
            'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'uploaded': False}}}})
        monkeypatch.setattr(httpx, 'AsyncClient', UploadClient)
        with pytest.raises(ValueError, match='chat_sync_(binding|session)_changed'):
            await runtime.media.step(UID, manual=True)
        assert sent == [65536]
        assert service.completed == 0
        source = json.loads(runtime.media.store.rows(DOMAIN.key())[0]['source'])
        assert source['images'][attachment]['original']['uploaded'] is False
        assert file.read_bytes() == raw
        session['logged_in'] = True
        await runtime.enable(UID, 'Neko')
        sent.clear()
        await runtime.media.step(UID, manual=True)
        assert sum(sent) == len(raw)
        assert service.completed == 1
        source = json.loads(runtime.media.store.rows(DOMAIN.key())[0]['source'])
        assert source['complete'] and source['images'][attachment]['original']['uploaded']
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_media_tombstone_suppresses_the_retained_local_source_and_late_pages(tmp_path):
    runtime, _, _ = setup_runtime(tmp_path, MediaService())
    identity = "1" * 32
    record = delivered_record(identity, [{"url": "https://music.example/play", "artist": "Artist"}])
    try:
        runtime.media.store.capture(UID, record)
        runtime.media.store.adopt(DOMAIN.key(), UID, {identity})
        page = {"ownerId": OWNER, "characterSyncId": UID, "cursor": 1, "hasMore": False,
            "records": [{"messageId": identity, "revision": 0, "record": None, "recordHash": None}]}
        runtime.media.store.apply(DOMAIN.key(), page, 0, set())
        assert await runtime.media.records(UID, DOMAIN, {identity}) == {identity: None}
        runtime.media.store.prepare(DOMAIN.key(), identity, record, {"complete": True, "images": {}})
        assert await runtime.media.records(UID, DOMAIN, {identity}) == {identity: None}
        runtime.media.store.adopt(DOMAIN.key(), UID, {identity})
        late = {**page, "cursor": 2, "records": [{"messageId": identity, "revision": 2, "record": record,
            "recordHash": hashlib.sha256(encode(record)).hexdigest()}]}
        runtime.media.store.apply(DOMAIN.key(), late, 1, set())
        assert await runtime.media.records(UID, DOMAIN, {identity}) == {identity: None}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_delivered_links_reuse_original_identity_without_recapture_or_republication(tmp_path):
    runtime, service, _ = setup_runtime(tmp_path, MediaService())
    try:
        await runtime.configure("https://sync.test")
        await runtime.enable(UID, "Synthetic")
        identity = capture(runtime)["messageId"]
        record = delivered_record(identity, [{"title": "Track", "artist": "Artist", "source": "音乐推荐", "url": "https://music.example/play"}])
        runtime.media.store.capture(UID, record)
        await runtime.media.step(UID, manual=True)
        assert service.media[identity][1] == record
        assert len(service.operations) == 1
        await runtime.media.step(UID, manual=True)
        assert len(service.operations) == 1
        assert runtime.media.store.cursor(DOMAIN.key()) == 1
        assert await runtime.media.records(UID, DOMAIN, {identity}) == {identity: record}
        runtime.ledger.queue_deletion(DOMAIN, [identity])
        assert await runtime.media.records(UID, DOMAIN, set()) == {}
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("in_recent,in_ledger,excluded,expected", [
    (True, False, False, True),
    (False, True, False, False),
    (True, True, True, False),
    (True, True, False, True),
])
async def test_media_upload_follows_authorized_recent_memory(tmp_path, monkeypatch, in_recent, in_ledger, excluded, expected):
    from types import SimpleNamespace
    from memory.recent_sync import RecentMemoryAdapter

    monkeypatch.setattr("memory.recent_sync.assert_cloudsave_writable", lambda *args, **kwargs: None)
    memory = tmp_path / "memory"
    recent_path = memory / "Neko" / "recent.json"
    recent_path.parent.mkdir(parents=True)
    recent_path.write_text("[]")
    adapter = RecentMemoryAdapter(SimpleNamespace(memory_dir=memory, project_memory_dir=memory))
    runtime, service, _ = setup_runtime(tmp_path, MediaService(), recent_memory_adapter=adapter)
    try:
        await runtime.configure("https://sync.test")
        await runtime.enable(UID, "Neko")
        identity = capture(runtime)["messageId"] if in_ledger else "e" * 32
        if in_recent:
            recent_path.write_text(json.dumps([{"type": "human", "data": {"content": "Synthetic recent message",
                "metadata": {"chat_original_source": {"recordId": identity}}}}]))
        if excluded:
            runtime.recent_memory.exclude(DOMAIN.key(), [identity])
        record = delivered_record(identity, [
            {"title": "Track", "artist": "Artist", "url": "https://music.example/play"},
            {"title": "GIF", "type": "gif", "url": "https://images.example/sent.gif"},
            {"title": "News", "url": "https://news.example/item"},
        ])
        assert len(record["attachments"]) == 3
        runtime.media.store.capture(UID, record)
        await runtime.media.step(UID, manual=True)
        assert (identity in service.media) is expected
        if expected:
            assert service.media[identity][1] == record
            await runtime.media.step(UID, manual=True)
            assert len(service.operations) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_older_media_receipt_cannot_replace_newer_durable_metadata(tmp_path):
    runtime, _, _ = setup_runtime(tmp_path, MediaService())
    try:
        identity = "1" * 32
        initial = delivered_record(identity, [])
        runtime.media.store.capture(UID, initial)
        runtime.media.store.adopt(DOMAIN.key(), UID, {identity})
        pending = runtime.media.store.seal(DOMAIN.key(), identity)
        newer = delivered_record(identity, [{"url": "https://news.example/item", "title": "Accepted"}])
        page = {"ownerId": OWNER, "characterSyncId": UID, "cursor": 2, "hasMore": False,
            "records": [{"messageId": identity, "revision": 2, "record": newer, "recordHash": hashlib.sha256(encode(newer)).hexdigest()}]}
        runtime.media.store.apply(DOMAIN.key(), page, 0, set())
        assert runtime.media.store.acknowledge(DOMAIN.key(), identity, pending, 1) is False
        assert await runtime.media.records(UID, DOMAIN, {identity}) == {identity: newer}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_model_binding_waits_for_renderer_and_rejects_a_newer_unbind_race(tmp_path):
    runtime, service, _ = setup_runtime(tmp_path, MediaService())
    class Adapter:
        path = "/user_live2d/original/model.model3.json"
        applied = 0
        async def read(self, _uid):
            return {"name": "Synthetic", "kind": "live2d", "path": self.path, "selection": canonical(["live2d", self.path]).decode()}
        async def apply(self, _uid, expected, path, commit):
            assert expected == (await self.read(_uid))["selection"]
            await commit(setattr, self, "path", path)
            self.applied += 1
            return True
    adapter = Adapter()
    runtime.models.adapter = adapter
    try:
        await runtime.configure("https://sync.test")
        await runtime.enable(UID, "Synthetic")
        await runtime.models.step(UID, manual=True)
        status = await runtime.models.status(UID)
        assert adapter.applied == 0 and status["candidate"]["path"] != adapter.path
        service.model_revision = 2
        assert await runtime.models.finish(UID, status["candidate"]["token"], True) is False
        assert adapter.applied == 0
        await runtime.models.step(UID, manual=True)
        latest = await runtime.models.status(UID)
        assert latest["candidate"]["token"] != status["candidate"]["token"]
        assert await runtime.models.finish(UID, latest["candidate"]["token"], True) is True
        assert adapter.applied == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_default_model_candidate_loads_from_the_actual_static_route(tmp_path):
    from urllib.parse import urljoin
    from app.main_server.web_app import app

    runtime, _, _ = setup_runtime(tmp_path, MediaService())

    class Adapter:
        path = '/user_live2d/current/model.model3.json'

        async def read(self, _uid):
            return {'name': 'Synthetic', 'kind': 'live2d', 'path': self.path,
                    'selection': canonical(['live2d', self.path]).decode()}

        async def apply(self, _uid, expected, path, commit):
            assert expected == (await self.read(_uid))['selection']
            await commit(setattr, self, 'path', path)
            return True

    adapter = Adapter()
    runtime.models.adapter = adapter
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        await runtime.models.step(UID, manual=True)
        candidate = (await runtime.models.status(UID))['candidate']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:48911/') as client:
            response = await client.get(candidate['path'])
            assert response.status_code == 200
            moc = await client.get(urljoin(str(response.url), response.json()['FileReferences']['Moc']))
            assert moc.status_code == 200 and moc.content.startswith(b'MOC3')
        assert await runtime.models.finish(UID, candidate['token'], True) is True
        assert adapter.path == candidate['path']
        await runtime.models.step(UID, manual=True)
        assert not (await runtime.models.status(UID)).get('candidate')
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_model_download_failure_exposes_a_durable_manual_retry(tmp_path, monkeypatch):
    from pathlib import Path
    service = MediaService()
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/chat_media_v1.json").read_text())
    service.model = next(item["record"] for item in fixture["valid"] if item["name"] == "complete_model")
    runtime, _, _ = setup_runtime(tmp_path, service)
    class Adapter:
        async def read(self, _uid):
            return {"name": "Synthetic", "kind": "live2d", "path": "/original", "selection": "original"}
    async def unavailable(*_args):
        raise OSError("cloud_model_download_unavailable")
    runtime.models.adapter = Adapter()
    monkeypatch.setattr(runtime.models, "download", unavailable)
    try:
        await runtime.configure("https://sync.test")
        await runtime.enable(UID, "Synthetic")
        with pytest.raises(OSError, match="cloud_model_download_unavailable"):
            await runtime.models.step(UID, manual=True)
        assert (await runtime.models.status(UID))["failed"] is True
        await runtime.models.resolve(UID, "retry")
        assert (await runtime.models.status(UID))["failed"] is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['network', 'hash', 'oversize', 'short', 'guard', 'cancel', 'commit-guard', 'http'])
async def test_model_download_failures_remove_only_their_own_partial_file(tmp_path, monkeypatch, failure):
    import asyncio
    from types import SimpleNamespace
    from services.chat_sync.model_sync import DesktopModelSync

    raw = b'accepted model package'
    package = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
               'resource': {'url': 'https://cdn.example/model.zip'}}
    directory = tmp_path / 'model-packages'
    directory.mkdir()
    other = directory / 'other-flight.part'
    other.write_bytes(b'owned by a different download')
    target = directory / (package['sha256'] + '.zip')
    target.write_bytes(b'previous cached bytes')

    class Response:
        status_code = 503 if failure == 'http' else 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        async def aiter_raw(self):
            yield raw[:1]
            if failure == 'network':
                raise httpx.ReadError('synthetic disconnect')
            yield {'hash': b'x' * (len(raw) - 1), 'oversize': raw,
                   'short': b''}.get(failure, raw[1:])

    class Client:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        def stream(self, *_args):
            return Response()

    guard_calls = 0
    async def guard():
        nonlocal guard_calls
        guard_calls += 1
        if failure == 'cancel' and guard_calls == 2:
            raise asyncio.CancelledError()
        if failure == 'guard':
            raise ValueError('chat_sync_binding_changed')
        if failure == 'commit-guard' and guard_calls == 3:
            raise ValueError('chat_sync_binding_changed')

    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    sync = SimpleNamespace(store=SimpleNamespace(path=tmp_path / 'profiles.sqlite3'))
    error = asyncio.CancelledError if failure == 'cancel' else (httpx.ReadError if failure == 'network'
            else OSError if failure == 'http' else ValueError)
    for _ in range(2):
        guard_calls = 0
        with pytest.raises(error):
            await DesktopModelSync.download(sync, DOMAIN, None, package, guard)
        assert list(directory.glob('*.part')) == [other]
        assert other.read_bytes() == b'owned by a different download'
        assert target.read_bytes() == b'previous cached bytes'


@pytest.mark.asyncio
async def test_model_download_publishes_verified_bytes_and_reuses_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from services.chat_sync.model_sync import DesktopModelSync

    raw = b'accepted model package'
    package = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
               'resource': {'url': 'https://cdn.example/model.zip'}}
    requests = []
    async def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(raw))
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **_kwargs: client)
    async def guard():
        pass
    sync = SimpleNamespace(store=SimpleNamespace(path=tmp_path / 'profiles.sqlite3'))
    path = await DesktopModelSync.download(sync, DOMAIN, None, package, guard)
    assert path.read_bytes() == raw
    assert list(path.parent.glob('*.part')) == []
    assert await DesktopModelSync.download(sync, DOMAIN, None, package, guard) == path
    assert len(requests) == 1 and requests[0].headers.get('Authorization') is None


@pytest.mark.asyncio
async def test_collected_unbound_resources_are_reuploaded_before_replaying_the_same_pending_media(tmp_path, monkeypatch):
    class Resources(MediaService):
        missing = True
        async def request(self, request):
            if request.url.path.endswith('/capabilities'):
                return httpx.Response(200, json={'schemaVersion': 1, 'media': 1, 'modelBinding': 1, 'resources': True})
            if request.method == 'PUT' and request.url.path.endswith('/media') and self.missing:
                self.missing = False
                return httpx.Response(409, json={'detail': 'media_resource_not_ready'})
            return await super().request(request)
    runtime, service, _ = setup_runtime(tmp_path, Resources())
    uploads = []
    async def upload(_request, _base, file, guard=None):
        assert guard is not None
        await guard()
        uploads.append(file['resourceId'])
    monkeypatch.setattr(runtime.media, 'upload_file', upload)
    try:
        await runtime.configure('https://sync.test')
        await runtime.enable(UID, 'Synthetic')
        identity = capture(runtime)['messageId']
        record = delivered_record(identity, [])
        record['attachments'] = [{'id': 'f' * 32, 'type': 'image', 'title': '', 'source': 'chat',
            'original': {'resourceId': '3' * 32}, 'thumbnail': {'resourceId': '4' * 32}}]
        runtime.media.store.capture(UID, record, {'complete': True, 'images': {'f' * 32: {
            'original': {'resourceId': '3' * 32, 'purpose': 'image', 'uploaded': True},
            'thumbnail': {'resourceId': '4' * 32, 'purpose': 'thumbnail', 'uploaded': True}}}})
        await runtime.media.step(UID, manual=True)
        assert set(uploads) == {'3' * 32, '4' * 32}
        assert service.media[identity][1] == record
    finally:
        await runtime.close()
@pytest.mark.parametrize('format', ['JPEG', 'PNG', 'GIF', 'WEBP'])
def test_thumbnail_is_compressed_and_original_bytes_are_preserved(tmp_path, format):
    import io
    import random
    from pathlib import Path
    from PIL import Image
    from services.chat_sync.media_files import image_files

    raw = io.BytesIO()
    image = Image.frombytes('RGB', (684, 875), random.Random(1024).randbytes(684 * 875 * 3))
    image.save(raw, format=format, quality=20)
    original = raw.getvalue()
    files = image_files(tmp_path, original)
    assert Path(files['original']['path']).read_bytes() == original
    assert files['thumbnail']['size'] <= min(len(original), 64 * 1024)
    with Image.open(files['thumbnail']['path']) as preview:
        assert max(preview.size) <= 320
        assert preview.format == ('JPEG' if format == 'JPEG' else 'WEBP')


def test_thumbnail_preserves_transparency_without_changing_sent_image(tmp_path):
    import io
    from PIL import Image
    from services.chat_sync.media_files import image_files

    image = Image.new('RGBA', (640, 480), (255, 0, 0, 0))
    image.paste((255, 0, 0, 255), (160, 120, 480, 360))
    raw = io.BytesIO()
    image.save(raw, format='PNG')
    files = image_files(tmp_path, raw.getvalue(), original=False)
    assert set(files) == {'thumbnail'}
    assert files['thumbnail']['size'] <= min(len(raw.getvalue()), 64 * 1024)
    with Image.open(files['thumbnail']['path']) as preview:
        assert preview.convert('RGBA').getpixel((0, 0))[3] == 0
        assert preview.convert('RGBA').getpixel((160, 120))[3] == 255


def test_tiny_image_does_not_grow_due_to_thumbnail_encoder_overhead(tmp_path):
    import io
    from PIL import Image
    from services.chat_sync.media_files import image_files

    raw = io.BytesIO()
    Image.new('P', (1, 1)).save(raw, format='GIF')
    files = image_files(tmp_path, raw.getvalue())
    assert files['thumbnail']['size'] <= len(raw.getvalue())
