"""Versioned display attachments and model bindings, separate from original/persona bytes."""

import ipaddress
import json
import re
import unicodedata
from urllib.parse import urlsplit

MAX_MEDIA_BYTES = 131_072
MAX_RESOURCE_BYTES = 120 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_MODEL_FILES = 512
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _fail():
    raise ValueError("invalid_media_record")


def obj(value, required, optional=()):
    """Reject unknown fields instead of silently accepting credentials or control payloads."""
    if not isinstance(value, dict) or not set(required) <= value.keys() <= set(required) | set(optional):
        _fail()
    return value


def text(value, maximum=800, *, required=False):
    if not isinstance(value, str) or (required and not value) or len(value.encode("utf-8")) > maximum:
        _fail()
    return value


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail()
    return value


def integer(value, maximum=MAX_SAFE_INTEGER, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def digest(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail()
    return value


def web_url(value):
    """Only portable public web addresses; never resolve or fetch a submitted URL."""
    text(value, 8192, required=True)
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        _fail()
    try:
        url = urlsplit(value)
        host = (url.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        if (url.scheme not in ("http", "https") or not host or url.username is not None
                or url.password is not None or url.port not in (None, 80, 443)
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
                or "." not in host or ":" in host or "%" in url.netloc):
            _fail()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Block noncanonical numeric IPv4 forms, which browsers otherwise resolve locally.
            if re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*", host):
                _fail()
        else:
            a, b, c, _ = map(int, str(address).split("."))
            if (a in (0, 10, 127) or a >= 224 or (a == 169 and b == 254)
                    or (a == 172 and 16 <= b <= 31) or (a == 192 and b in (0, 168))
                    or (a == 100 and 64 <= b <= 127) or (a == 198 and 18 <= b <= 19)
                    or (a == 198 and b == 51 and c == 100) or (a == 203 and b == 0 and c == 113)):
                _fail()
    except (ValueError, TypeError):
        _fail()
    return value


def resource(value, *, nullable=False):
    if nullable and value is None:
        return None
    obj(value, (), ("url", "resourceId"))
    if len(value) != 1:
        _fail()
    if "url" in value:
        web_url(value["url"])
    else:
        identifier(value["resourceId"])
    return value


def relative_path(value):
    text(value, 1024, required=True)
    if (value.startswith("/") or any(char in value for char in '\\:%?#*<>|"')
            or any(ord(char) < 32 for char in value)
            or any(part in ("", ".", "..") or part.endswith((".", " "))
                   or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
                   for part in value.split("/"))
            or value in (".cloud-model.json", ".cloud-model.zip")):
        _fail()
    return value


def validate_media_record(value):
    obj(value, ("schemaVersion", "messageId", "messageOrigin", "proactive", "attachments"))
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        _fail()
    identifier(value["messageId"])
    if value["messageOrigin"] not in (None, "chat", "proactive"):
        _fail()
    if value["proactive"] is not None:
        if value["messageOrigin"] != "proactive":
            _fail()
        proactive = obj(value["proactive"], ("deliveryId", "sourceMode", "triggerReason"))
        text(proactive["deliveryId"], 128, required=True)
        for key in ("sourceMode", "triggerReason"):
            if proactive[key] is not None:
                text(proactive[key], 200, required=True)
    attachments = value["attachments"]
    if not isinstance(attachments, list) or len(attachments) > 16:
        _fail()
    seen = set()
    for item in attachments:
        if not isinstance(item, dict):
            _fail()
        kind = item.get("type")
        fields = {
            "image": ("id", "type", "title", "source", "original", "thumbnail"),
            "music": ("id", "type", "title", "source", "artist", "url", "cover"),
            "link": ("id", "type", "title", "source", "url", "mode"),
        }.get(kind)
        if fields is None:
            _fail()
        obj(item, fields)
        identifier(item["id"])
        if item["id"] in seen:
            _fail()
        seen.add(item["id"])
        text(item["title"], 1600)
        text(item["source"], 400)
        if kind == "image":
            resource(item["original"], nullable=True)
            resource(item["thumbnail"], nullable=True)
            if item["original"] is not None and item["original"] == item["thumbnail"]:
                _fail()
        else:
            web_url(item["url"])
            if kind == "music":
                text(item["artist"], 800)
                resource(item["cover"], nullable=True)
            elif item["mode"] is not None:
                text(item["mode"], 200)
    return value


def validate_model_binding(value):
    obj(value, ("schemaVersion", "model"))
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        _fail()
    model = value["model"]
    if model is None:
        return value
    obj(model, ("modelFile", "package", "files", "compression"))
    if not relative_path(model["modelFile"]).endswith(".model3.json"):
        _fail()
    package = obj(model["package"], ("resource", "size", "sha256"))
    resource(package["resource"])
    integer(package["size"], MAX_RESOURCE_BYTES, 1)
    digest(package["sha256"])
    compression = obj(model["compression"], ("version", "format", "textureMaxEdge"))
    if (type(compression["version"]) is not int or compression["version"] != 1
            or compression["format"] != "zip-deflate"):
        _fail()
    if compression["textureMaxEdge"] is not None:
        integer(compression["textureMaxEdge"], 4096, 1)
    files = model["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_MODEL_FILES:
        _fail()
    paths, total = set(), 0
    for item in files:
        obj(item, ("path", "size", "sha256"))
        path = relative_path(item["path"])
        key = unicodedata.normalize("NFC", path).lower()
        if key in paths:
            _fail()
        paths.add(key)
        total += integer(item["size"], MAX_RESOURCE_BYTES)
        digest(item["sha256"])
    if total > MAX_RESOURCE_BYTES or not any(item["path"] == model["modelFile"] for item in files):
        _fail()
    return value


def resource_ids(value):
    """Collect exact resource-reference objects, not arbitrary strings from display text."""
    if isinstance(value, dict):
        if value.keys() == {"resourceId"}:
            yield value["resourceId"]
        else:
            for item in value.values():
                yield from resource_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from resource_ids(item)


def encode(value, *, model=False):
    """Canonical JSON uses ASCII keys and preserves user text and attachment order."""
    (validate_model_binding if model else validate_media_record)(value)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_MEDIA_BYTES:
        _fail()
    return raw


def decode(raw, *, model=False):
    if not isinstance(raw, bytes) or len(raw) > MAX_MEDIA_BYTES:
        _fail()
    try:
        value = json.loads(raw)
        if encode(value, model=model) != raw:
            _fail()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _fail()
    return value
