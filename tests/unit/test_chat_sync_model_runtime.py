"""Live2D binding, renderer admission and verified package transfer."""
import hashlib
import json
import httpx
import pytest
from tests.chat_sync_writer_fixture import isolated_chat_sync_writer  # noqa: F401
from tests.unit.test_chat_sync_runtime import BINDING, DOMAIN, OWNER, UID, SyntheticService, setup_runtime
from services.chat_sync.model_protocol import canonical, encode

class ModelService(SyntheticService):
    def __init__(self):
        super().__init__()
        self.model = {"schemaVersion": 1, "model": None}
        self.model_revision = 1

    async def request(self, request):
        if request.url.path == "/v1/chat-sync/models/capabilities":
            return httpx.Response(200, json={"schemaVersion": 1, "modelBinding": 1, "resources": False})
        if request.url.path.endswith("/model-binding"):
            assert request.headers["X-Neko-Chat-Binding"] == BINDING
            return httpx.Response(200, json={"ownerId": OWNER, "characterSyncId": UID, "revision": self.model_revision,
                "record": self.model, "recordHash": hashlib.sha256(encode(self.model)).hexdigest()})
        return await super().request(request)

@pytest.mark.asyncio
async def test_model_binding_waits_for_renderer_and_rejects_a_newer_unbind_race(tmp_path):
    runtime, service, _ = setup_runtime(tmp_path, ModelService())
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

    runtime, _, _ = setup_runtime(tmp_path, ModelService())

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
    service = ModelService()
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/cloud_model_v1.json").read_text(encoding='utf-8'))
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
