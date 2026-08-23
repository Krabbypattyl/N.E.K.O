"""提供 Numeric v2 使用的人格读取与 Prompt 文本预算辅助。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from utils.tokenize import count_tokens, truncate_to_tokens


# 当前猫娘只读取短人格摘要；本地字符上限避免首次 tokenizer 下载阻塞开场。
THEATER_PERSONA_MAX_CHARS = 512
_THEATER_EXCLUDED_PERSONA_FIELDS = frozenset(
    {
        "外貌特征",
        "特殊能力",
        "居住地点",
        "pngtuber",
        "pngtuber_idle_image",
        "pngtuber_talking_image",
        "年龄",
        "档案名",
        "种族",
        "一句话台词",
    }
)


def truncate_prompt_value(value: Any, *, max_tokens: int, max_items: int = 8) -> Any:
    """递归限制会进入剧场 Prompt 的动态文本和集合大小。"""  # noqa: DOCSTRING_CJK
    if isinstance(value, str):
        return truncate_to_tokens(value, max_tokens)
    if isinstance(value, dict):
        items = list(value.items())
        if len(items) > max_items:
            items = items[: max(0, max_items - 1)] + (items[-1:] if max_items else [])
        return {
            str(key): truncate_prompt_value(item, max_tokens=max_tokens, max_items=max_items)
            for key, item in items
        }
    if isinstance(value, list):
        return [
            truncate_prompt_value(item, max_tokens=max_tokens, max_items=max_items)
            for item in value[:max_items]
        ]
    if isinstance(value, tuple):
        return tuple(
            truncate_prompt_value(item, max_tokens=max_tokens, max_items=max_items)
            for item in value[:max_items]
        )
    return value


def _fit_json_content(
    prefix: str,
    payload: dict[str, Any] | list[Any],
    *,
    max_tokens: int,
    field_max_tokens: int,
) -> str:
    """Keep a JSON prompt valid while fitting the complete serialized message."""
    budget = max(0, int(max_tokens))
    if isinstance(payload, (dict, list)):
        item_count = len(payload)
        item_limits = list(dict.fromkeys([max(8, item_count), 16, *range(8, -1, -1)]))
    else:
        item_limits = [0]
    field_limit = max(0, int(field_max_tokens))
    field_budgets = list(dict.fromkeys([
        field_limit,
        *(max(0, field_limit // (2**step)) for step in range(1, 8)),
        0,
    ]))
    encoded_cache: dict[tuple[int, int], tuple[str, int]] = {}
    token_cache: dict[str, int] = {}

    def _count(value: str) -> int:
        if value not in token_cache:
            token_cache[value] = count_tokens(value)
        return token_cache[value]

    def _prefix_budgets(limit: int) -> list[int]:
        if limit <= 0:
            return [0]
        values = [limit, *range(max(0, limit - 16), limit)]
        step = max(1, limit // 8)
        values.extend(limit - step * index for index in range(1, 9))
        values.append(0)
        return list(dict.fromkeys(max(0, value) for value in values))

    for max_items in item_limits:
        for field_budget in field_budgets:
            cache_key = (max_items, field_budget)
            if cache_key not in encoded_cache:
                bounded = truncate_prompt_value(
                    payload,
                    max_tokens=field_budget,
                    max_items=max_items,
                )
                encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
                encoded_cache[cache_key] = (encoded, _count(encoded))
            encoded, encoded_tokens = encoded_cache[cache_key]
            if encoded_tokens > budget:
                continue
            prefix_budget = budget - encoded_tokens
            for retry_budget in _prefix_budgets(prefix_budget):
                candidate = truncate_to_tokens(prefix, retry_budget) + encoded
                if _count(candidate) <= budget:
                    return candidate
    empty = "[]" if isinstance(payload, list) else "{}"
    if _count(empty) > budget:
        return ""
    for prefix_budget in _prefix_budgets(budget):
        candidate = truncate_to_tokens(prefix, prefix_budget) + empty
        if _count(candidate) <= budget:
            return candidate
    return empty


def _bounded_message_content(
    content: str,
    *,
    max_tokens: int,
    field_max_tokens: int,
) -> str:
    """Bound one message without cutting through a structured JSON payload."""
    json_start = content.find("{")
    if json_start >= 0:
        prefix = content[:json_start]
        try:
            payload = json.loads(content[json_start:])
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, (dict, list)):
            return _fit_json_content(
                prefix,
                payload,
                max_tokens=max_tokens,
                field_max_tokens=field_max_tokens,
            )
    return truncate_to_tokens(content, max(0, int(max_tokens)))


def bound_prompt_messages(
    messages: list[Any],
    *,
    max_tokens: int,
    field_max_tokens: int,
    system_max_tokens: int = 1000,
) -> list[Any]:
    """在保留消息结构的前提下限制模型消息中的动态文本。"""  # noqa: DOCSTRING_CJK
    source = list(messages or [])
    if not source:
        return []
    max_budget = max(0, int(max_tokens))
    system_indexes = [
        index
        for index, message in enumerate(source)
        if str(
            message.get("role")
            if isinstance(message, dict)
            else getattr(message, "role", "")
        ) == "system"
    ]
    ordered_indexes = system_indexes + [
        index for index in reversed(range(len(source))) if index not in system_indexes
    ]
    bounded_contents: dict[int, str] = {}
    remaining = max_budget
    for position, index in enumerate(ordered_indexes):
        slots_left = len(ordered_indexes) - position
        if index in system_indexes:
            budget = min(max(0, int(system_max_tokens)), remaining // max(1, slots_left))
        else:
            budget = remaining // max(1, slots_left) if slots_left > 1 else remaining
        content = str(
            source[index].get("content")
            if isinstance(source[index], dict)
            else getattr(source[index], "content", "")
        )
        bounded_content = _bounded_message_content(
            content,
            max_tokens=budget,
            field_max_tokens=field_max_tokens,
        )
        bounded_contents[index] = bounded_content
        remaining = max(0, remaining - count_tokens(bounded_content))

    bounded_messages: list[Any] = []
    for index, message in enumerate(source):
        bounded_content = bounded_contents.get(index, "")
        if isinstance(message, dict):
            replacement = dict(message)
            replacement["content"] = bounded_content
        else:
            try:
                replacement = type(message)(content=bounded_content)
            except TypeError:
                replacement = message
        bounded_messages.append(replacement)
    return bounded_messages


def _load_character_profile(
    config_manager: Any | None,
    lanlan_name: str,
    *,
    max_chars: int | None = None,
) -> str:
    """只读取服务端当前猫娘的短人格摘要。"""  # noqa: DOCSTRING_CJK
    root = getattr(config_manager, "app_docs_dir", None) if config_manager is not None else None
    if not root or not lanlan_name:
        return ""
    name = str(lanlan_name).strip()
    try:
        characters = config_manager.load_characters()
    except Exception:
        return ""
    catgirls = characters.get("猫娘") if isinstance(characters, dict) else None
    current_name = str(characters.get("当前猫娘") or "").strip() if isinstance(characters, dict) else ""
    # 请求参数不能读取其他猫娘的人格，保证 Numeric v2 始终绑定当前用户角色。
    if not isinstance(catgirls, dict) or name != current_name or name not in catgirls:
        return ""
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        return ""
    try:
        memory_root = (Path(root) / "memory").resolve()
        path = (memory_root / name / "persona.json").resolve()
    except (OSError, RuntimeError):
        return ""
    if not path.is_relative_to(memory_root):
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    lines: list[str] = []
    for section_name in ("neko", "relationship"):
        section = payload.get(section_name) if isinstance(payload, dict) else None
        if not isinstance(section, dict):
            continue
        for fact in section.get("facts") or []:
            text = str(fact.get("text") or "").strip() if isinstance(fact, dict) else ""
            if text and not _theater_persona_field_excluded(text):
                lines.append(text)
    profile = "\n".join(dict.fromkeys(lines))
    if max_chars is not None:
        return profile[: max(0, int(max_chars))]
    # Actor 负责按完整事实和完整回合装箱；这里不再从人格事实中间截断文本。
    return profile


def _load_player_address(config_manager: Any | None) -> str:
    """读取当前猫娘对玩家的结构化称呼。"""  # noqa: DOCSTRING_CJK
    if config_manager is None:
        return ""
    try:
        characters = config_manager.load_characters()
    except Exception:
        return ""
    master = characters.get("主人") if isinstance(characters, dict) else None
    if not isinstance(master, dict):
        return ""
    for field in ("昵称", "档案名"):
        value = str(master.get(field) or "").strip()
        if value:
            return value
    return ""


def _theater_persona_field_excluded(text: str) -> bool:
    """只按人格字段标签过滤，不用正文关键词猜测内容。"""  # noqa: DOCSTRING_CJK
    value = str(text or "").strip()
    bracketed = re.match(r"^[【\[]\s*([^】\]]{1,64})\s*[】\]]", value)
    labelled = re.match(r"^([^:：\n]{1,64})\s*[:：]", value)
    match = bracketed or labelled
    if match is None:
        return False
    field_name = re.sub(r"[\s*`\\]+", "", match.group(1)).casefold()
    return field_name in _THEATER_EXCLUDED_PERSONA_FIELDS
