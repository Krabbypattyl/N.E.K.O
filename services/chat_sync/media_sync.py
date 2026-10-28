"""Passive attachment records, fenced by the existing original and binding ledgers."""

from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import logging
from pathlib import Path
from uuid import uuid4

from services.chat_sync.media_protocol import decode as decode_chat_media, encode as encode_chat_media
from services.chat_sync.writer import store_connection


class _PrivateMediaLogFilter(logging.Filter):
    def filter(self, record):
        return "x-oss-signature=" not in record.getMessage()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class MediaStore:
    """Use the existing witnessed database; downloads never become new local shares."""

    def __init__(self, path):
        self.path = Path(path)
        with closing(store_connection(self.path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS media_state (
                    domain TEXT NOT NULL, ref TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    canonical BLOB NOT NULL, source TEXT, pending BLOB, remote BLOB, remote_revision INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(domain,ref));
                CREATE TABLE IF NOT EXISTS media_checkpoint (
                    domain TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0);
            """)

    def capture(self, uid, record, source=None):
        raw = encode_chat_media(record)
        with closing(store_connection(self.path)) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO media_state(domain,ref,canonical,source) VALUES (?,?,?,?)",
                         ("local/" + uid, record["messageId"], raw, json.dumps(source or {"complete": True, "images": {}})))

    def rows(self, domain):
        with closing(store_connection(self.path)) as conn:
            conn.row_factory = __import__("sqlite3").Row
            return [dict(row) for row in conn.execute("SELECT * FROM media_state WHERE domain=?", (domain,))]

    def adopt(self, domain, uid, approved):
        with closing(store_connection(self.path)) as conn, conn:
            for row in self.rows("local/" + uid):
                if row["ref"] in approved:
                    conn.execute("INSERT OR IGNORE INTO media_state(domain,ref,canonical,source) VALUES (?,?,?,?)",
                                 (domain, row["ref"], row["canonical"], row["source"]))

    def seal(self, domain, identity):
        with closing(store_connection(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT canonical,revision,pending FROM media_state WHERE domain=? AND ref=?", (domain, identity)).fetchone()
            if row[2]:
                return bytes(row[2])
            payload = canonical({"operationId": uuid4().hex, "baseRevision": row[1], "record": decode_chat_media(bytes(row[0]))})
            conn.execute("UPDATE media_state SET pending=? WHERE domain=? AND ref=?", (payload, domain, identity))
            return payload

    def acknowledge(self, domain, identity, pending, revision):
        with closing(store_connection(self.path)) as conn, conn:
            row = conn.execute("SELECT remote,remote_revision FROM media_state WHERE domain=? AND ref=? AND pending=?", (domain, identity, pending)).fetchone()
            if row and row[0] and row[1] > revision:
                conn.execute("UPDATE media_state SET revision=remote_revision,canonical=remote,source=NULL,pending=NULL,remote=NULL,remote_revision=0 "
                             "WHERE domain=? AND ref=? AND pending=?", (domain, identity, pending))
                return False
            # A downloaded newer revision must never be rolled back by a late receipt.
            result = conn.execute("UPDATE media_state SET revision=MAX(revision,?),pending=NULL,remote=NULL,remote_revision=0 "
                                  "WHERE domain=? AND ref=? AND pending=?", (revision, domain, identity, pending))
            return result.rowcount > 0

    def accept_conflict(self, domain, identity, pending):
        with closing(store_connection(self.path)) as conn, conn:
            result = conn.execute("UPDATE media_state SET revision=remote_revision,canonical=remote,source=NULL,pending=NULL,remote=NULL,remote_revision=0 "
                                  "WHERE domain=? AND ref=? AND pending=? AND remote IS NOT NULL AND remote_revision>?",
                                  (domain, identity, pending, json.loads(pending)["baseRevision"]))
            return result.rowcount > 0

    def prepare(self, domain, identity, record, source):
        with closing(store_connection(self.path)) as conn, conn:
            conn.execute("UPDATE media_state SET canonical=?,source=? WHERE domain=? AND ref=? AND pending IS NULL AND length(canonical)>0",
                         (encode_chat_media(record), json.dumps(source), domain, identity))

    def discard(self, domain, uid, identity):
        with closing(store_connection(self.path)) as conn, conn:
            # Empty canonical bytes are a durable local tombstone, never a wire record.
            conn.execute("UPDATE media_state SET canonical=X'',source=NULL,pending=NULL,remote=NULL,remote_revision=0 "
                         "WHERE domain IN (?,?) AND ref=?", (domain, "local/" + uid, identity))

    def cursor(self, domain):
        with closing(store_connection(self.path)) as conn:
            row = conn.execute("SELECT cursor FROM media_checkpoint WHERE domain=?", (domain,)).fetchone()
            return row[0] if row else 0

    def version(self, domain, uid):
        with closing(store_connection(self.path)) as conn:
            row = conn.execute("SELECT COUNT(*),COALESCE(SUM(revision),0),COALESCE(SUM(length(canonical)),0) "
                               "FROM media_state WHERE domain IN (?,?)", (domain, "local/" + uid)).fetchone()
            return str(self.cursor(domain)) + ":" + ":".join(map(str, row))

    def apply(self, domain, page, after, deleted):
        if (not isinstance(page, dict) or set(page) != {"ownerId", "characterSyncId", "records", "cursor", "hasMore"}
                or type(page["cursor"]) is not int or page["cursor"] < after or type(page["hasMore"]) is not bool
                or json.loads(domain)[:2] != [page["ownerId"], page["characterSyncId"]]
                or not isinstance(page["records"], list) or len(page["records"]) > 50):
            raise ValueError("invalid_chat_media_response")
        accepted = []
        for item in page["records"]:
            if (not isinstance(item, dict) or set(item) != {"messageId", "revision", "record", "recordHash"}
                    or type(item["revision"]) is not int or item["revision"] < 0):
                raise ValueError("invalid_chat_media_response")
            raw = encode_chat_media(item["record"]) if item["record"] is not None else None
            if raw is not None and (item["record"]["messageId"] != item["messageId"]
                                   or hashlib.sha256(raw).hexdigest() != item["recordHash"] or item["revision"] <= 0):
                raise ValueError("invalid_chat_media_response")
            accepted.append((item, raw))
        with closing(store_connection(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            saved = conn.execute("SELECT cursor FROM media_checkpoint WHERE domain=?", (domain,)).fetchone()
            if (saved[0] if saved else 0) != after:
                raise ValueError("chat_media_cursor_changed")
            for item, raw in accepted:
                identity = item["messageId"]
                if raw is None or identity in deleted:
                    conn.execute("INSERT INTO media_state(domain,ref,revision,canonical) VALUES (?,?,?,X'') "
                                 "ON CONFLICT(domain,ref) DO UPDATE SET canonical=X'',source=NULL,pending=NULL,remote=NULL,remote_revision=0",
                                 (domain, identity, item["revision"]))
                else:
                    conn.execute("INSERT INTO media_state(domain,ref,revision,canonical) VALUES (?,?,?,?) "
                                 "ON CONFLICT(domain,ref) DO UPDATE SET "
                                 "remote=CASE WHEN pending IS NOT NULL THEN excluded.canonical ELSE NULL END,"
                                 "remote_revision=CASE WHEN pending IS NOT NULL THEN excluded.revision ELSE 0 END,"
                                 "revision=CASE WHEN pending IS NULL THEN excluded.revision ELSE revision END,"
                                 "canonical=CASE WHEN pending IS NULL THEN excluded.canonical ELSE canonical END,"
                                 "source=CASE WHEN pending IS NULL THEN NULL ELSE source END WHERE length(media_state.canonical)>0 "
                                 "AND excluded.revision>MAX(media_state.revision,media_state.remote_revision)",
                                 (domain, identity, item["revision"], raw))
            conn.execute("INSERT INTO media_checkpoint VALUES (?,?) ON CONFLICT(domain) DO UPDATE SET cursor=excluded.cursor",
                         (domain, page["cursor"]))


def delivered_record(identity, links, *, delivery_id=None, source_mode=None):
    """Map only the public delivery contract; no model context or provider candidates."""
    attachments = []
    from services.chat_sync.media_protocol import web_url as validate_public_url
    for link in (links or [])[:16]:
        if not isinstance(link, dict):
            continue
        try:
            url = validate_public_url(link.get("url"))
        except ValueError:
            continue
        common = {"id": uuid4().hex, "title": link.get("title") or "", "source": link.get("source") or ""}
        if "artist" in link or link.get("source") == "音乐推荐":
            cover = None
            try:
                cover = {"url": validate_public_url(link.get("cover"))}
            except ValueError:
                pass
            attachments.append({**common, "type": "music", "artist": link.get("artist") or "",
                                "url": url, "cover": cover})
        elif is_meme(link):
            # The producer may later supply a real small preview. Never use the full URL as one.
            attachments.append({**common, "type": "image", "original": {"url": url}, "thumbnail": None})
        else:
            attachments.append({**common, "type": "link", "url": url, "mode": link.get("mode") or None})
    record = {"schemaVersion": 1, "messageId": identity, "messageOrigin": "proactive" if delivery_id else "chat",
              "proactive": {"deliveryId": str(delivery_id), "sourceMode": source_mode, "triggerReason": None} if delivery_id else None,
              "attachments": attachments}
    accepted = []
    for attachment in attachments:
        try:
            encode_chat_media({**record, "attachments": [attachment]})
            accepted.append(attachment)
        except ValueError:
            continue
    record["attachments"] = accepted
    return decode_chat_media(encode_chat_media(record))


def is_meme(link):
    from urllib.parse import urlsplit
    if link.get("type") in {"meme", "gif"}:
        return True
    source = str(link.get("source") or "").lower()
    if any(key in source for key in ("表情包", "斗图吧", "发表情", "imgflip", "meme", "sticker")):
        return True
    host = (urlsplit(str(link.get("url") or "")).hostname or "").lower()
    return any(host == key or host.endswith("." + key) for key in
               ("img.soutula.com", "i.imgflip.com", "fabiaoqing.com", "soutula.com", "img.doutupk.com", "doutupk.com"))


async def capture_proactive_media(manager, turn_id, links, source_mode):
    """Called only after the real text publication guard accepted this exact turn."""
    proof = getattr(manager, "_chat_original_turns", {}).get(str(turn_id or ""))
    if not proof or proof.get("origin") != "proactive":
        return
    from services.chat_sync.source import character_uid_for
    from services.chat_sync.runtime import get_desktop_chat_sync_runtime
    cm = getattr(manager, "_config_manager", None)
    if cm is None:
        return
    uid = await character_uid_for(manager.lanlan_name, cm)
    if not uid:
        return
    runtime = await get_desktop_chat_sync_runtime(cm)
    record = delivered_record(proof["recordId"], links, delivery_id=turn_id, source_mode=source_mode)
    source = {"complete": not any(item["type"] == "image" for item in record["attachments"]), "images": {}}
    await asyncio.to_thread(runtime.media.store.capture, uid, record, source)
    await wake_media(runtime, uid)
    runtime.wake_scheduler()


async def capture_user_media(manager, identity, request_id, *, original_time=None):
    shared = getattr(manager, "_chat_shared_images", {}).get(str(request_id or ""), [])
    if not shared:
        return
    from services.chat_sync.source import character_uid_for
    from services.chat_sync.runtime import get_desktop_chat_sync_runtime
    from services.chat_sync.media_files import image_files, sent_image_bytes
    cm = manager._config_manager
    uid = await character_uid_for(manager.lanlan_name, cm)
    if not uid:
        return
    if identity is None:
        from services.chat_sync.source import save_original
        identity = await save_original(manager.lanlan_name, "user", "", original_time=original_time, config_manager=cm)
        if identity is None:
            return
    runtime = await get_desktop_chat_sync_runtime(cm)
    record = delivered_record(identity, [])
    source = {"complete": False, "images": {}}
    for value in shared[:16]:
        files = await asyncio.to_thread(image_files, runtime.state.path.parent, sent_image_bytes(value))
        attachment_id = uuid4().hex
        record["attachments"].append({"id": attachment_id, "type": "image", "title": "", "source": "chat", "original": None, "thumbnail": None})
        source["images"][attachment_id] = files
    await asyncio.to_thread(runtime.media.store.capture, uid, record, source)
    getattr(manager, "_chat_shared_images", {}).pop(str(request_id or ""), None)
    await wake_media(runtime, uid)
    runtime.wake_scheduler()
    return identity


async def wake_media(runtime, uid):
    from services.chat_sync.ledger import ChatDomain
    binding = await asyncio.to_thread(runtime.state.binding, uid)
    if binding and binding["enabled"] and binding["binding_id"]:
        await asyncio.to_thread(runtime.scheduler.request_check, ChatDomain(binding["owner_id"], uid).key(), "media")


class DesktopMediaSync:
    def __init__(self, runtime, path):
        self.runtime, self.store = runtime, MediaStore(path)
        self.lock = asyncio.Lock()
        logger = logging.getLogger("httpx")
        if not any(isinstance(item, _PrivateMediaLogFilter) for item in logger.filters):
            logger.addFilter(_PrivateMediaLogFilter())

    async def step(self, uid, *, manual=False):
        runtime = self.runtime
        async with self.lock:
            authenticated = await runtime._session()
            binding = await asyncio.to_thread(runtime.state.binding, uid)
            domain, request, commit = await runtime._bound_transport(uid, authenticated=authenticated,
                expected_binding=binding, automatic_only=not manual)
            async def guard():
                await runtime._guard(binding, authenticated[1])
            key = domain.key()
            caps = await request("GET", "/v1/chat-sync/media/capabilities", None)
            if caps.get("schemaVersion") != 1 or caps.get("media") != 1:
                return
            if runtime.recent_memory is not None:
                name = await runtime.recent_character_name(uid)
                approved = set()
                if name is not None:
                    local = await commit(runtime.recent_memory.adapter.read, name)
                    visible = await commit(runtime.recent_memory.visible, key, local["entries"])
                    approved = {row["id"] for row in visible if row["role"] != "memo"}
            else:
                originals = await asyncio.to_thread(runtime.ledger.visible_originals, domain)
                approved = {json.loads(raw)["messageId"] for raw in originals}
            deleted = set(await asyncio.to_thread(runtime.ledger.deleted_message_ids, domain))
            await commit(self.store.adopt, key, uid, approved - deleted)
            base = f"/v1/chat-sync/characters/{uid}"
            # Pull first: one failed local resource must not starve remote display records.
            for _ in range(10):
                after = await asyncio.to_thread(self.store.cursor, key)
                page = await request("GET", f"{base}/media?after={after}&limit=20", None)
                await commit(self.store.apply, key, page, after, deleted)
                if not page["hasMore"]:
                    break
            local_error = None
            for row in await asyncio.to_thread(self.store.rows, key):
                if (row["source"] is None and row["pending"] is None) or row["ref"] not in approved - deleted:
                    continue
                try:
                    async def publish():
                        pending = await commit(self.store.seal, key, row["ref"])
                        try:
                            receipt = await request("PUT", base + "/media", pending)
                        except ValueError as exc:
                            if getattr(exc, 'media_resource_missing', False):
                                source = json.loads(row['source']) if row['source'] else {}
                                files = [file for image in source.get('images', {}).values() for file in image.values()]
                                if not files:
                                    raise
                                for file in files:
                                    await self.upload_file(request, base, file, guard=guard)
                                receipt = await request('PUT', base + '/media', pending)
                            elif getattr(exc, "media_deleted", False):
                                await commit(self.store.discard, key, uid, row["ref"])
                                return False
                            elif getattr(exc, "media_conflict", False):
                                if await commit(self.store.accept_conflict, key, row["ref"], pending):
                                    return False
                                raise OSError("media_revision_changed") from None
                            else:
                                raise
                        value = json.loads(pending)
                        expected = hashlib.sha256(encode_chat_media(value["record"])).hexdigest()
                        if (receipt.get("ownerId") != domain.owner_id or receipt.get("characterSyncId") != uid
                                or receipt.get("operationId") != value["operationId"] or receipt.get("recordHash") != expected
                                or receipt.get("revision") != value["baseRevision"] + 1):
                            raise ValueError("invalid_chat_media_response")
                        return await commit(self.store.acknowledge, key, row["ref"], pending, receipt["revision"])
                    source = json.loads(row["source"] or '{"complete":true,"images":{}}')
                    if row["pending"] or row["revision"] == 0:
                        if not await publish():
                            continue
                    if not source["complete"] and caps.get("resources"):
                        record = decode_chat_media(bytes(row["canonical"]))
                        resource_error = None
                        owns_head = True
                        for item in record["attachments"]:
                            if item["type"] != "image":
                                continue
                            files = source["images"].get(item["id"])
                            if not files:
                                from services.chat_sync import host
                                from services.chat_sync.media_files import image_files
                                raw = await host.shared_meme_bytes(item["original"]["url"])
                                files = await asyncio.to_thread(image_files, self.store.path.parent, raw, original=False)
                                source["images"][item["id"]] = files
                                await commit(self.store.prepare, key, row["ref"], record, source)
                            for purpose, file in sorted(files.items(), key=lambda pair: pair[0] != 'thumbnail'):
                                try:
                                    if not file["uploaded"]:
                                        await self.upload_file(request, base, file, guard=guard)
                                        file["uploaded"] = True
                                        await commit(self.store.prepare, key, row["ref"], record, source)
                                except (OSError, ValueError) as exc:
                                    if getattr(exc, "status_code", None) in {401, 403, 410}:
                                        raise
                                    resource_error = exc
                                    continue
                                reference = {"resourceId": file["resourceId"]}
                                if item.get(purpose) != reference:
                                    item[purpose] = reference
                                    await commit(self.store.prepare, key, row["ref"], record, source)
                                    owns_head = await publish()
                                    if not owns_head:
                                        break
                            if not owns_head:
                                break
                        if not owns_head:
                            continue
                        source["complete"] = all(file["uploaded"] for files in source["images"].values() for file in files.values())
                        await commit(self.store.prepare, key, row["ref"], record, source)
                        if resource_error:
                            raise resource_error
                except (OSError, ValueError) as exc:
                    if getattr(exc, "status_code", None) in {401, 403, 410}:
                        raise
                    local_error = exc
            if runtime.history_changed:
                await runtime.history_changed(uid)
            if local_error:
                raise local_error

    async def upload_file(self, request, base, file, guard=None):
        import httpx
        metadata = {key: file[key] for key in ("resourceId", "purpose", "size", "sha256", "contentType")}
        result = await request("POST", base + "/resources", canonical(metadata))
        if result.get("resourceId") != file["resourceId"] or type(result.get("ready")) is not bool:
            raise ValueError("invalid_chat_media_response")
        if result["ready"]:
            return
        signed = result["upload"]
        from services.chat_sync.media_protocol import web_url
        if not web_url(signed["url"]).startswith("https://") or signed["method"] != "PUT" or any(
                key.lower() not in {"content-type", "content-length"} for key in signed["headers"]):
            raise ValueError("invalid_chat_media_response")
        raw = await asyncio.to_thread(Path(file["path"]).read_bytes)
        if len(raw) != file["size"] or hashlib.sha256(raw).hexdigest() != file["sha256"]:
            raise ValueError("invalid_chat_media_resource")
        async with httpx.AsyncClient(follow_redirects=False, timeout=60) as client:
            async def chunks():
                for offset in range(0, len(raw), 65536):
                    if guard:
                        await guard()
                    yield raw[offset:offset + 65536]
            try:
                response = await client.put(signed["url"], headers=signed["headers"], content=chunks())
            except httpx.HTTPError:
                raise OSError("chat_media_upload_unavailable") from None
            if response.status_code not in {200, 201, 204}:
                raise OSError("chat_media_upload_unavailable")
        receipt = await request("POST", base + "/resources/" + file["resourceId"] + "/complete", None)
        if receipt.get("resourceId") != file["resourceId"] or receipt.get("ready") is not True:
            raise ValueError("invalid_chat_media_response")

    async def records(self, uid, domain, identities):
        rows = await asyncio.to_thread(self.store.rows, "local/" + uid)
        if domain:
            rows.extend(await asyncio.to_thread(self.store.rows, domain.key()))
        result = {}
        for row in {row["ref"]: row for row in rows}.values():
            if row["ref"] in identities:
                if not row["canonical"]:
                    result[row["ref"]] = None
                    continue
                try:
                    result[row["ref"]] = decode_chat_media(bytes(row["canonical"]))
                except ValueError:
                    continue
        return result

    async def local_images(self, uid, domain, identities):
        rows = await asyncio.to_thread(self.store.rows, "local/" + uid)
        if domain:
            rows.extend(await asyncio.to_thread(self.store.rows, domain.key()))
        result = {}
        for row in {row["ref"]: row for row in rows}.values():
            if row["ref"] not in identities or not row["source"]:
                continue
            source = json.loads(row["source"])
            for identity, files in source.get("images", {}).items():
                for purpose, file in files.items():
                    if purpose in {"original", "thumbnail"} and Path(file["path"]).is_file():
                        result.setdefault(identity, {})[purpose] = f"/api/chat-sync/characters/{uid}/local-media/{row['ref']}/{identity}/{purpose}"
        return result

    async def local_image_path(self, uid, message_id, attachment_id, purpose):
        from services.chat_sync.ledger import ChatDomain
        from services.chat_sync.media_protocol import identifier
        identifier(message_id); identifier(attachment_id)
        if purpose not in {"original", "thumbnail"}:
            raise ValueError("chat_media_resource_unavailable")
        binding = await asyncio.to_thread(self.runtime.state.binding, uid)
        domain = ChatDomain(binding["owner_id"], uid) if binding else None
        identities = {item["messageId"] for item in await self.runtime.history(uid)}
        if message_id not in identities:
            raise ValueError("chat_media_resource_unavailable")
        rows = await asyncio.to_thread(self.store.rows, "local/" + uid)
        if domain:
            rows.extend(await asyncio.to_thread(self.store.rows, domain.key()))
        rows = {row["ref"]: row for row in rows}
        for row in rows.values():
            if row["ref"] == message_id and row["source"]:
                file = json.loads(row["source"]).get("images", {}).get(attachment_id, {}).get(purpose)
                if file:
                    path = Path(file["path"]).resolve()
                    if path.is_relative_to((self.store.path.parent / "media-source").resolve()) and path.is_file():
                        return path
        raise ValueError("chat_media_resource_unavailable")

    async def resource(self, uid, identity):
        """Only an explicitly referenced private object can be fetched, without bearer forwarding."""
        from services.chat_sync.media_protocol import identifier
        from services.chat_sync.ledger import ChatDomain
        import httpx
        identifier(identity)
        runtime = self.runtime
        binding = await asyncio.to_thread(runtime.state.binding, uid)
        if not binding:
            raise ValueError("chat_sync_not_enabled")
        domain = ChatDomain(binding["owner_id"], uid)
        visible = {item["messageId"] for item in await runtime.history(uid)}
        records = {key: value for key, value in (await self.records(uid, domain, visible)).items() if value is not None}
        previews = {value["resourceId"] for record in records.values() for item in record["attachments"]
                    for field in ("thumbnail", "cover") if isinstance(value := item.get(field), dict) and "resourceId" in value}
        preview = identity in previews
        from services.chat_sync.media_protocol import resource_ids
        if identity not in {item for record in records.values() for item in resource_ids(record)}:
            raise ValueError("chat_media_resource_unavailable")
        cache = self.store.path.parent / "media-files" / binding["owner_id"] / identity
        # Cache is display-only and never republishes a downloaded attachment.
        if cache.is_file():
            if preview:
                from PIL import Image
                with Image.open(cache) as image:
                    if cache.stat().st_size > 256 * 1024 or max(image.size) > 1024 or image.width * image.height > 1024 * 1024:
                        raise ValueError("invalid_chat_media_resource")
            return cache
        _, request, _ = await runtime._bound_transport(uid)
        value = await request("GET", f"/v1/chat-sync/characters/{uid}/resources/{identity}/download", None)
        from services.chat_sync.media_protocol import digest, integer, web_url
        url = web_url(value.get("download", {}).get("url"))
        size = integer(value.get("size"), 256 * 1024 if preview else 40 * 1024 * 1024, 1)
        digest(value.get("sha256"))
        if value.get("contentType") not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            raise ValueError("chat_media_resource_unavailable")
        chunks, count = [], 0
        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise OSError("chat_media_resource_unavailable")
                async for chunk in response.aiter_bytes():
                    count += len(chunk)
                    if count > size:
                        raise ValueError("invalid_chat_media_resource")
                    chunks.append(chunk)
        raw = b"".join(chunks)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != value["sha256"]:
            raise ValueError("invalid_chat_media_resource")
        from PIL import Image
        import io
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"JPEG", "PNG", "GIF", "WEBP"} or image.width * image.height > 50_000_000:
                raise ValueError("invalid_chat_media_resource")
            if preview and (max(image.size) > 1024 or image.width * image.height > 1024 * 1024):
                raise ValueError("invalid_chat_media_resource")
        # Recheck the exact account/binding before installing private bytes.
        if await asyncio.to_thread(runtime.state.binding, uid) != binding:
            raise ValueError("chat_sync_binding_changed")
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix("." + uuid4().hex + ".part")
        temporary.write_bytes(raw)
        temporary.replace(cache)
        return cache
