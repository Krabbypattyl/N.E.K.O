"""Numeric v2 表现内容块的兼容读取工具。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from typing import Any, Mapping


MAX_CONTENT_BLOCKS = 16


def content_blocks(container: Mapping[str, Any]) -> list[dict[str, str]]:
    """优先读取新有序内容；旧记录退化为“旁白在前、对白在后”。"""  # noqa: DOCSTRING_CJK

    raw_content = container.get("content")
    if isinstance(raw_content, list):
        blocks: list[dict[str, str]] = []
        for raw in raw_content:
            if not isinstance(raw, Mapping):
                continue
            block_type = str(raw.get("type") or "")
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            if block_type == "narration":
                blocks.append({"type": "narration", "text": text})
            elif block_type == "dialogue" and raw.get("speaker_id") == "active_catgirl":
                blocks.append({
                    "type": "dialogue",
                    "speaker_id": "active_catgirl",
                    "text": text,
                })
        return blocks[:MAX_CONTENT_BLOCKS]

    blocks = []
    narration = str(container.get("narration") or "").strip()
    if narration:
        blocks.append({"type": "narration", "text": narration})
    for raw in container.get("dialogue") or []:
        if not isinstance(raw, Mapping) or raw.get("speaker_id") != "active_catgirl":
            continue
        text = str(raw.get("text") or "").strip()
        if text:
            blocks.append({
                "type": "dialogue",
                "speaker_id": "active_catgirl",
                "text": text,
            })
    return blocks[:MAX_CONTENT_BLOCKS]


def performance_content_blocks(performance: Mapping[str, Any]) -> list[dict[str, str]]:
    """按玩家实际看到的顺序展开普通或换场 performance。"""  # noqa: DOCSTRING_CJK

    segments = performance.get("segments")
    if isinstance(segments, list):
        blocks: list[dict[str, str]] = []
        for segment in segments:
            if isinstance(segment, Mapping):
                blocks.extend(content_blocks(segment))
        return blocks
    return content_blocks(performance)


def performance_dialogue(performance: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        block
        for block in performance_content_blocks(performance)
        if block["type"] == "dialogue"
    ]


def valid_ordered_content(
    container: Mapping[str, Any],
    *,
    require_narration: bool = False,
    require_dialogue: bool = False,
) -> bool:
    raw_content = container.get("content")
    if not isinstance(raw_content, list) or not 1 <= len(raw_content) <= MAX_CONTENT_BLOCKS:
        return False
    block_types: set[str] = set()
    for raw in raw_content:
        if not isinstance(raw, Mapping) or not str(raw.get("text") or "").strip():
            return False
        if raw.get("type") == "narration" and set(raw) == {"type", "text"}:
            block_types.add("narration")
            continue
        if (
            raw.get("type") == "dialogue"
            and set(raw) == {"type", "speaker_id", "text"}
            and raw.get("speaker_id") == "active_catgirl"
        ):
            block_types.add("dialogue")
            continue
        return False
    return (
        (not require_narration or "narration" in block_types)
        and (not require_dialogue or "dialogue" in block_types)
    )


__all__ = [
    "MAX_CONTENT_BLOCKS",
    "content_blocks",
    "performance_content_blocks",
    "performance_dialogue",
    "valid_ordered_content",
]
