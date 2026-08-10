"""提供自由模式和 Numeric v2 共用的最小人格、文本预算辅助。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from utils.tokenize import truncate_to_tokens


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


def scoped_story_for_prompt(
    story: dict[str, Any], *, max_background_tokens: int | None = None
) -> dict[str, Any]:
    """只把自由模式种子投影给模型，并按调用方预算裁剪背景。"""  # noqa: DOCSTRING_CJK
    projected = dict(story)
    background = str(story.get("background") or story.get("world_seed") or "")
    if max_background_tokens is not None:
        projected["background"] = truncate_to_tokens(background, max_background_tokens)
    return projected


def _complete_model_text(
    value: Any, max_tokens: int, *, max_chars: int | None = None
) -> str | None:
    """文本只有完整进入预算时才返回，避免把截断句交给模型。"""  # noqa: DOCSTRING_CJK
    text = str(value or "")
    if max_chars is not None:
        # 自由输入只需要本地字符预算，不因 tokenizer 缺失触发远程下载。
        return text if len(text) <= max(0, int(max_chars)) else None
    bounded = truncate_to_tokens(text, max_tokens)
    return text if bounded == text else None


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
    # 请求参数不能读取其他猫娘的人格，保证自由模式和剧本模式都绑定当前用户角色。
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
    return truncate_to_tokens(profile, 320)


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
