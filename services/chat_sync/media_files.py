"""Prepare immutable image copies and previews without modifying sent originals."""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path
from uuid import uuid4

from PIL import Image


def checked_image_type(source, *, preview=False):
    """Inspect cached paths or verified download bytes on a worker thread."""
    size = len(source) if isinstance(source, bytes) else Path(source).stat().st_size
    with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
        if image.format not in {'JPEG', 'PNG', 'GIF', 'WEBP'} or image.width * image.height > 50_000_000:
            raise ValueError('invalid_chat_media_resource')
        if preview and (size > 256 * 1024 or max(image.size) > 1024 or image.width * image.height > 1024 * 1024):
            raise ValueError('invalid_chat_media_resource')
        return Image.MIME.get(image.format, 'application/octet-stream')


def image_files(directory, raw, *, original=True):
    if not raw or len(raw) > 40 * 1024 * 1024:
        raise ValueError("chat_media_image_unavailable")
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 50_000_000 or image.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
            raise ValueError("chat_media_image_unavailable")
        content_type = Image.MIME[image.format]
        image.seek(0)
        format = "JPEG" if image.format == "JPEG" else "WEBP"
        preview = image.convert("RGB" if format == "JPEG" else "RGBA")
        maximum = min(64 * 1024, len(raw))
        thumbnail = None
        thumbnail_type = "image/jpeg" if format == "JPEG" else "image/webp"
        for edge in (320, 192, 128, 64, 32, 16, 8, 1):
            preview.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            preview.save(buffer, format=format, quality=72, optimize=True)
            if 0 < buffer.tell() <= maximum:
                thumbnail = buffer.getvalue()
                break
        if thumbnail is None and max(image.size) <= 320 and len(raw) <= maximum:
            thumbnail, thumbnail_type = raw, content_type
        if thumbnail is None:
            raise ValueError("chat_media_preview_too_large")
    root = Path(directory) / "media-source"
    root.mkdir(parents=True, exist_ok=True)

    def persist(data, purpose, mime):
        identity = uuid4().hex
        path = root / identity
        path.write_bytes(data)
        return {"resourceId": identity, "path": str(path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "purpose": purpose, "contentType": mime, "uploaded": False}

    result = {"thumbnail": persist(thumbnail, "thumbnail", thumbnail_type)}
    if original:
        result["original"] = persist(raw, "image", content_type)
    return result


def sent_image_bytes(value):
    if not isinstance(value, str) or len(value) > 56 * 1024 * 1024:
        raise ValueError("chat_media_image_unavailable")
    payload = value.split(",", 1)[1] if value.startswith("data:image/") else value
    return base64.b64decode(payload, validate=True)
