"""保留自由模式正文边界，不再解析 Story v3 Router 或 Actor JSON。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from typing import Any


# 自由模式只拦截明显的内部协议泄漏；正文内容和语言风格不在这里重写。
_FORBIDDEN_OUTPUT_TERMS = (
    "scene_id",
    "node_id",
    "choice_id",
    "state_diff",
    "ending_domain_id",
    "turns_used",
    "nonprogress_turns",
    "turn_delivery",
    "回合预算",
)


def _plain_model_text(raw: Any) -> str:
    """提取模型正文，拒绝旧 JSON 外壳和 Markdown 代码围栏。"""  # noqa: DOCSTRING_CJK
    if isinstance(raw, str):
        text = raw.strip("\r\n")
        probe = text.strip()
        if (probe.startswith("{") and probe.endswith("}")) or probe.startswith("```"):
            return ""
        return text if probe else ""
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").lower() not in {"", "text", "output_text"}:
                continue
            value = item.get("text")
            if value is None:
                value = item.get("content")
            if isinstance(value, str):
                parts.append(value)
        text = "".join(parts).strip("\r\n")
        return text if text.strip() else ""
    # 少数兼容客户端会把一个文本块包成 {type, text}，其余对象一律不当正文。
    if isinstance(raw, dict) and set(raw).issubset({"type", "text"}):
        value = raw.get("text")
        text = str(value or "").strip("\r\n") if isinstance(value, str) else ""
        return text if text.strip() else ""
    return ""


def _parse_free_output(raw: Any) -> dict[str, Any] | None:
    """解析 RP-Hub 风格自由正文；失败时由调用方丢弃本回合。"""  # noqa: DOCSTRING_CJK
    text = _plain_model_text(raw)
    if not text:
        return None
    if any(term.lower() in text.lower() for term in _FORBIDDEN_OUTPUT_TERMS):
        return None
    return {"text": text}
